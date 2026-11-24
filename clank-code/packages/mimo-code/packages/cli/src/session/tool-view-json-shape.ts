/** Lossy structural projection of the existing strict TOON JSON input domain. */
import { parseJsonInput, type Node } from '../util/toon-subset'

type Shape = 'null' | 'boolean' | 'number' | 'string'
  | ['object', Array<[string, Shape]>]
  | ['array', number, Shape[]]

/** Keys remain visible; scalar values are omitted. This is not redaction. */
export function jsonShape(input: string): { text: string; omittedScalars: number } | undefined {
  try {
    const parsed = parseJsonInput(input)
    let omittedScalars = 0
    const shape = (node: Node): Shape => {
      if (node.k === 'o') return ['object', node.v.map(([key, value]) => [key, shape(value)])]
      if (node.k === 'l') return ['array', node.v.length, node.v.map(shape)]
      omittedScalars++
      return node.v === null ? 'null' : typeof node.v as 'boolean' | 'number' | 'string'
    }
    const text = JSON.stringify(shape(parsed))
    if (Buffer.byteLength(text, 'utf8') > 51200) return
    return { text, omittedScalars }
  } catch {
    return
  }
}
