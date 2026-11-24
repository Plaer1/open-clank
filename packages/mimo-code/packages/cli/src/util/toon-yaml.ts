/** Explicit, bounded YAML value normalization for the existing TOON JSON codec. */
import { isAlias, isMap, isScalar, isSeq, parseAllDocuments } from 'yaml'

export function yamlToBoundedJson(input: string): string | undefined {
  if (!input.isWellFormed() || Buffer.byteLength(input, 'utf8') > 51200) return
  try {
    const documents = parseAllDocuments(input, { version: '1.2', schema: 'core', strict: true, uniqueKeys: true, intAsBigInt: true, customTags: [] })
    if (documents.length !== 1) return
    const doc = documents[0]!
    if (doc.errors.length || doc.warnings.length || doc.directives?.yaml.version !== '1.2') return
    let nodes = 0, bytes = 0
    const chunks: string[] = []
    const put = (s: string) => {
      bytes += Buffer.byteLength(s, 'utf8')
      if (bytes > 51200) throw new Error('output bound')
      chunks.push(s)
    }
    const walk = (node: unknown, depth: number): void => {
      if (++nodes > 16384 || depth > 32 || !node || typeof node !== 'object' || isAlias(node)) throw new Error('unsupported node')
      if ('anchor' in node && node.anchor !== undefined || 'tag' in node && node.tag !== undefined) throw new Error('explicit property')
      if (isScalar(node)) {
        const v = node.value
        if (typeof v === 'string') { if (!v.isWellFormed()) throw new Error('unicode'); put(JSON.stringify(v)); return }
        if (typeof v === 'boolean' || v === null) { put(JSON.stringify(v)); return }
        if (typeof v === 'bigint' && v >= BigInt(Number.MIN_SAFE_INTEGER) && v <= BigInt(Number.MAX_SAFE_INTEGER)) {
          if (v === 0n && node.source?.startsWith('-')) throw new Error('negative zero')
          put(v.toString()); return
        }
        throw new Error('unsupported scalar')
      }
      if (isSeq(node)) {
        put('['); node.items.forEach((item, i) => { if (i) put(','); walk(item, depth + 1) }); put(']'); return
      }
      if (isMap(node)) {
        const keys = new Set<string>()
        put('{')
        node.items.forEach((pair, i) => {
          const key = pair.key
          if (!isScalar(key) || typeof key.value !== 'string' || !key.value.isWellFormed() || key.anchor !== undefined || key.tag !== undefined || keys.has(key.value)) throw new Error('unsupported key')
          if (++nodes > 16384 || depth + 1 > 32) throw new Error('node bound')
          keys.add(key.value); if (i) put(','); put(JSON.stringify(key.value)); put(':'); walk(pair.value, depth + 1)
        })
        put('}'); return
      }
      throw new Error('unsupported node')
    }
    walk(doc.contents, 0)
    return chunks.join('')
  } catch { return }
}
