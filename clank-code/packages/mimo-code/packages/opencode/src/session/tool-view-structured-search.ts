/** Private evidence and bounded projection of the built-in MCP catalog search. */
import { createHash } from 'node:crypto'
import { toolViewProducerCurrent } from './tool-view-producer-admission'
let proofs: WeakMap<object,{sha:string;bytes:number}> | undefined
type Result = {output:string;metadata?:Record<string,unknown>;attachments?:unknown[]}
const sha=(text:string)=>createHash('sha256').update(text,'utf8').digest('hex')
const eligible=(v:Result)=>typeof v.output==='string' && Buffer.byteLength(v.output,'utf8')<=51200 && !v.attachments?.length && v.metadata?.truncated!==true && v.metadata?.outputPath===undefined
export function markStructuredSearch<T extends Result>(v:T,context:object):T {
  if (toolViewProducerCurrent(context,'structured-search-v1') && eligible(v)) (proofs ??= new WeakMap()).set(v,{sha:sha(v.output),bytes:Buffer.byteLength(v.output,'utf8')})
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
  try {
    const value=JSON.parse(input)
    if (!value || typeof value!=='object' || Array.isArray(value) || Object.keys(value).sort().join(',')!=='results,status' ||
        !['matched','no_match'].includes(value.status) || !Array.isArray(value.results) || value.results.length>20 ||
        (value.status==='matched') !== (value.results.length>0)) return
    const results=value.results.map((row:unknown)=>{
      if (!row || typeof row!=='object' || Array.isArray(row) || Object.keys(row).sort().join(',')!=='description,name,score') throw new Error('unsupported')
      const item=row as Record<string,unknown>
      if (typeof item.name!=='string' || !item.name || typeof item.description!=='string' || typeof item.score!=='number' || !Number.isFinite(item.score)) throw new Error('unsupported')
      return {name:item.name,description:item.description}
    })
    const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
    const frame=`[Search results view; scores omitted. Original: history(${invocation}); follow nextOffset for further pages.]\n${JSON.stringify({status:value.status,results})}`
    return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
  } catch { return }
}
