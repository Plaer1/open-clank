/** Lexical Bash JSON, JSON lines or fenced JSON view; never a source authority. */
import { compactJsonTokens, framedJsonView } from './tool-view-json'

type Ref = {messageID:string;partID:string;revision:number;contentHash:string}

/** Exact whole-line JSON fences only; every outside byte remains unchanged. */
function fencedJsonView(input:string):string|undefined {
  if (!input.endsWith('\n') || input.includes('\r') || /[\x00-\x08\x0b-\x1f\x7f]/.test(input)) return
  for (let i=0;i<input.length;i++) {
    const c=input.charCodeAt(i)
    if(c>=0xd800 && c<=0xdbff){const n=input.charCodeAt(++i);if(!(n>=0xdc00 && n<=0xdfff))return}
    else if(c>=0xdc00 && c<=0xdfff)return
  }
  const lines=input.slice(0,-1).split('\n')
  if(lines.length>200)return
  const output:string[]=[]
  let count=0
  for(let i=0;i<lines.length;i++) {
    const line=lines[i]!
    if(line!=='```json') {if(line.includes('```'))return;output.push(line);continue}
    if(++count>8)return
    output.push(line)
    const body:string[]=[]
    while(++i<lines.length && lines[i]!=='```') {
      if(lines[i]!.includes('```'))return
      body.push(lines[i]!)
    }
    if(i>=lines.length || !body.length)return
    const compacted=compactJsonTokens(body.join('\n'))
    if(compacted===undefined)return
    output.push(compacted,'```')
  }
  return count ? output.join('\n')+'\n' : undefined
}

export function bashJsonView(input:string): {kind:'json'|'ndjson'|'fenced-json';body:string} | undefined {
  if (typeof input !== 'string' || Buffer.byteLength(input,'utf8') > 51200) return
  const single=compactJsonTokens(input)
  if (single !== undefined) return {kind:'json',body:single}
  const fenced=fencedJsonView(input)
  if (fenced !== undefined) return {kind:'fenced-json',body:fenced}
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
  const description=view.kind==='fenced-json' ? 'JSON-fence whitespace view; outside text and fence lines preserved' : 'Lossless JSON-lines whitespace view; LF record boundaries preserved'
  const frame=`[${description}. Original: history(${invocation}); follow nextOffset for further pages.]\n${view.body}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}
