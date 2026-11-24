/** Conservative lossy view; originals and disclosure remain with the caller. */
export function markdownSlim(input: string): { text: string; removedCommentLines: number; removedBlankLines: number } | undefined {
  if (typeof input !== 'string' || Buffer.byteLength(input, 'utf8') > 51200) return
  for (let i = 0; i < input.length; i++) {
    const c = input.charCodeAt(i)
    if (c === 13 && input.charCodeAt(i + 1) !== 10) return
    if ((c < 32 && c !== 10 && c !== 13) || (c >= 0x7f && c <= 0x9f)) return
    if (c >= 0xd800 && c <= 0xdbff) {
      const next = input.charCodeAt(++i)
      if (!(next >= 0xdc00 && next <= 0xdfff)) return
    } else if (c >= 0xdc00 && c <= 0xdfff) return
  }
  const lines = input.match(/[^\r\n]*(?:\r\n|\n|$)/g) || []
  const output: string[] = []
  let fence: { marker: string; length: number } | undefined
  let blankRun = 0, removedCommentLines = 0, removedBlankLines = 0
  for (const line of lines) {
    if (!line) continue
    const body = line.replace(/(?:\r\n|\n)$/, '')
    if (fence) {
      const closing = body.match(/^ {0,3}(`+|~+) *$/)
      if (closing && closing[1]![0] === fence.marker && closing[1]!.length >= fence.length) fence = undefined
      output.push(line)
      blankRun = 0
      continue
    }
    const opening = body.match(/^ {0,3}(`{3,}|~{3,})(.*)$/)
    if (opening) {
      if (opening[2]!.includes('`') || opening[2]!.includes('<')) return
      fence = { marker: opening[1]![0]!, length: opening[1]!.length }
      output.push(line); blankRun = 0
      continue
    }
    if (/^ {4,}\S/.test(body) || body.includes('`')) return
    if (body.includes('<') || body.includes('-->')) {
      const comment = body.match(/^ {0,3}<!--(.*?)--> *$/)
      if (!comment || comment[1]!.includes('<!--') || comment[1]!.includes('--') || comment[1]!.includes('<')) return
      removedCommentLines++
      continue
    }
    if (/^ *$/.test(body)) {
      if (++blankRun > 2) { removedBlankLines++; continue }
    } else blankRun = 0
    output.push(line)
  }
  if (fence || (!removedCommentLines && !removedBlankLines)) return
  const text = output.join('')
  if (Buffer.byteLength(text, 'utf8') >= Buffer.byteLength(input, 'utf8')) return
  return { text, removedCommentLines, removedBlankLines }
}
