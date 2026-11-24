import { createHash } from 'node:crypto'

/** Query only complete, independently qualified pages from the existing History reader. */
export async function queryHistoryOutput(input: {
  revision: number
  contentHash: string
  literal: string
  start: number
  limit: number
  readPage: (offset: number, limit: number) => Promise<unknown>
  current: () => boolean
}): Promise<{ outputSHA256: string; outputBytes: number; matches: { start: number; end: number }[]; nextOffset: number | null } | undefined> {
  try {
    if (!Number.isSafeInteger(input.revision) || input.revision < 0 || !/^[a-f0-9]{64}$/.test(input.contentHash) ||
      typeof input.literal !== 'string' || !input.literal.isWellFormed() ||
      !Number.isSafeInteger(input.start) || input.start < 0 ||
      !Number.isSafeInteger(input.limit) || input.limit < 1 || input.limit > 50 || !input.current()) return
    const needle = Buffer.from(input.literal, 'utf8')
    if (needle.length < 1 || needle.length > 256) return
    type Page = { text: string; revision: number; contentHash: string; outputSHA256: string; outputBytes: number; offset: number; nextOffset: number | null }
    const read = async (offset: number): Promise<Page | undefined> => {
      const result = await input.readPage(offset, 8192)
      if (!input.current() || !result || typeof result !== 'object') return
      const value = result as { state?: unknown; page?: unknown }
      if (value.state !== 'ready' || !value.page || typeof value.page !== 'object') return
      const page = value.page as Page
      if (typeof page.text !== 'string' || !page.text.isWellFormed() || page.revision !== input.revision ||
        page.contentHash !== input.contentHash || typeof page.outputSHA256 !== 'string' || !/^[a-f0-9]{64}$/.test(page.outputSHA256) ||
        !Number.isSafeInteger(page.outputBytes) || page.outputBytes < 0 || page.outputBytes > 51200 || page.offset !== offset) return
      const bytes = Buffer.byteLength(page.text, 'utf8')
      if (bytes > 8192 || offset + bytes > page.outputBytes || (offset < page.outputBytes && bytes === 0) ||
        page.nextOffset !== (offset + bytes < page.outputBytes ? offset + bytes : null)) return
      return page
    }
    const chunks: Buffer[] = []
    let first: Page | undefined
    let offset = 0
    for (let count = 0; count < 7; count++) {
      const page = await read(offset)
      if (!page) return
      if (!first) first = page
      else if (page.outputSHA256 !== first.outputSHA256 || page.outputBytes !== first.outputBytes) return
      const bytes = Buffer.from(page.text, 'utf8')
      chunks.push(bytes)
      if (page.nextOffset === null) break
      offset = page.nextOffset
    }
    if (!first) return
    const source = Buffer.concat(chunks)
    if (source.length !== first.outputBytes || createHash('sha256').update(source).digest('hex') !== first.outputSHA256 ||
      input.start > source.length || (input.start < source.length && (source[input.start]! & 0xc0) === 0x80)) return
    const matches: { start: number; end: number }[] = []
    let cursor = input.start
    let nextOffset: number | null = null
    while (cursor <= source.length - needle.length) {
      const start = source.indexOf(needle, cursor)
      if (start < 0) break
      const end = start + needle.length
      if (matches.length === input.limit) { nextOffset = start; break }
      matches.push({ start, end })
      cursor = end
    }
    const fresh = await read(0)
    if (!fresh || fresh.outputSHA256 !== first.outputSHA256 || fresh.outputBytes !== first.outputBytes ||
      fresh.text !== first.text || fresh.nextOffset !== first.nextOffset || !input.current()) return
    return { outputSHA256: first.outputSHA256, outputBytes: first.outputBytes, matches, nextOffset }
  } catch {
    return
  }
}
