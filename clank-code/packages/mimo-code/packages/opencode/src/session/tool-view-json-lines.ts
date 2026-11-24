/** Lexical Bash JSON or complete object-per-line view; never a source authority. */
import { compactJsonTokens, framedJsonView } from './tool-view-json'

type Ref = {messageID:string;partID:string;revision:number;contentHash:string}

export function bashJsonView(input:string): {kind:'json'|'ndjson';body:string} | undefined {
  if (typeof input !== 'string' || Buffer.byteLength(input,'utf8') > 51200) return
  const single=compactJsonTokens(input)
  if (single !== undefined) return {kind:'json',body:single}
  if (!input.endsWith('\n') || input.includes('\r')) return
  const records=input.slice(0,-1).split('\n')
  if (records.length < 2 || records.length > 200) return
  const compacted:string[]=[]
  for (const record of records) {
    if (!record || !record.trim()) return
    const body=compactJsonTokens(record)
    if (body===undefined || !body.startsWith('{') || !body.endsWith('}')) return
    compacted.push(body)
  }
  return {kind:'ndjson',body:compacted.join('\n')+'\n'}
}

export function framedBashJsonView(input:string, ref:Ref): string | undefined {
  const view=bashJsonView(input)
  if (!view) return
  if (view.kind==='json') return framedJsonView(input,ref)
  if (!Number.isSafeInteger(ref.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) ||
      [ref.messageID,ref.partID].some(x=>typeof x!=='string' || !x || x.length>256)) return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[Lossless JSON-lines whitespace view; LF record boundaries preserved. Original: history(${invocation}); follow nextOffset for further pages.]\n${view.body}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}
