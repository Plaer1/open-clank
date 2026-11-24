/** Private evidence and bounded projection of the built-in MCP catalog search. */
import { createHash } from 'node:crypto'
import { toolViewProducerCurrent } from './tool-view-producer-admission'
let proofs: WeakMap<object,{sha:string;bytes:number}> | undefined
type Result = {output:string;metadata?:Record<string,unknown>;attachments?:unknown[]}
const sha=(text:string)=>createHash('sha256').update(text,'utf8').digest('hex')
const eligible=(v:Result)=>typeof v.output==='string' && Buffer.byteLength(v.output,'utf8')<=51200 && !v.attachments?.length && v.metadata?.truncated!==true && v.metadata?.outputPath===undefined
export function markStructuredSearch<T extends Result>(v:T,context:object):T {
  if (toolViewProducerCurrent(context,'structured-search-v2') && eligible(v)) (proofs ??= new WeakMap()).set(v,{sha:sha(v.output),bytes:Buffer.byteLength(v.output,'utf8')})
  return v
}
export function transferStructuredSearch(from:Result,to:Result):void {
  const p=proofs?.get(from)
  if (p && eligible(to) && from.output===to.output && p.sha===sha(to.output) && p.bytes===Buffer.byteLength(to.output,'utf8')) proofs!.set(to,p)
}
export function readStructuredSearch(v:Result):boolean {
  const p=proofs?.get(v)
  return !!p && eligible(v) && p.sha===sha(v.output) && p.bytes===Buffer.byteLength(v.output,'utf8')
}
export function framedStructuredSearch(input:string,ref:{messageID:string;partID:string;revision:number;contentHash:string}):string | undefined {
  if (Buffer.byteLength(input,'utf8')>51200 || !Number.isSafeInteger(ref.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) || [ref.messageID,ref.partID].some(v=>typeof v!=='string'||!v||v.length>256)) return
  const hint='Call these tools through mcp_tool_use with {"name": "<tool name>", "arguments": {...}}; they are not callable by name directly.'
  const lines=input.split('\n')
  if (lines.length<4 || lines.length>23 || lines[0]!=='<functions>' || lines.at(-2)!=='</functions>' || lines.at(-1)!==hint) return
  try {
    const rows=lines.slice(1,-2).map(line=>{
      if (!line.startsWith('<function>') || !line.endsWith('</function>')) throw new Error('unsupported')
      const payload=line.slice(10,-11), value=JSON.parse(payload)
      if (!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).join(',')!=='description,name,parameters' ||
          typeof value.description!=='string' || typeof value.name!=='string' || !value.name || !value.parameters || typeof value.parameters!=='object' || Array.isArray(value.parameters) || JSON.stringify(value)!==payload || /<(\/?)(system-reminder|available-deferred-tools|functions|function)\b/i.test(payload)) throw new Error('unsupported')
      return value
    })
    if (`<functions>\n${rows.map(row=>`<function>${JSON.stringify(row)}</function>`).join('\n')}\n</functions>\n${hint}`!==input) return
    const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
    const frame=`[Search results; original: history(${invocation}); follow nextOffset for further pages.]\n${JSON.stringify(rows)}\n${hint}`
    return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
  } catch { return }
}
