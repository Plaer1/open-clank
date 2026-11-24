/** Private exact native-grep result witness and bounded row view. */
import {createHash} from 'node:crypto'
import {toolViewProducerCurrent} from './tool-view-producer-admission'
type Result={output:string;metadata?:Record<string,unknown>;attachments?:unknown[]}
type Row={path:string;line:number;text:string}
const digest=(text:string)=>createHash('sha256').update(text,'utf8').digest('hex')
const record=(v:unknown):v is Record<string,any>=>!!v && typeof v==='object' && !Array.isArray(v)
let proofs:WeakMap<object,{output:string;metadata:string;strict:boolean}>|undefined
/** Reconstruct only the exact complete output actually emitted by native grep. */
function rowsFor(value:Result,strict=false):Row[]|undefined {
 try {
  const m=value.metadata, f=m?.file
  if(strict) {
   const only=(value:unknown,allowed:string[])=>record(value) && Object.keys(value).every(key=>allowed.includes(key))
   if(!only(m,['matches','items','search_mode','path','file','page','truncated']) || !only(f,['contract','operation','path','kind','range','page','bytes_considered','lines_considered','truncation_reason','encoding','newline','media_type','fingerprint','search_mode','items','diagnostics']) ||
      !only(m?.page,['cursor','next_cursor','has_more','total']) || !only(record(f) ? f.page : undefined,['unit','cursor','next_cursor','has_more','returned','total']) ||
      !record(f) || f.path!==m?.path || f.search_mode!==m?.search_mode || (f.range!==null && !only(f.range,['unit','start','end']))) return
  }
  if(typeof value.output!=='string' || Buffer.byteLength(value.output,'utf8')>51200 || value.attachments?.length || !record(m) || m.truncated!==false || m.outputPath!==undefined || !record(f) || f.contract!=='open-clank.file-result/v1' || f.operation!=='grep' || f.kind!=='search' || f.truncation_reason!==null || !Array.isArray(f.diagnostics) || f.diagnostics.length!==0 || !Array.isArray(m.items) || !Array.isArray(f.items) || JSON.stringify(m.items)!==JSON.stringify(f.items) || !m.items.length || m.items.length>200 || !Number.isSafeInteger(m.matches) || m.matches!==m.items.length || !record(f.page) || f.page.cursor!==0 || f.page.next_cursor!==null || f.page.has_more!==false || f.page.returned!==m.items.length || f.page.total!==m.items.length || !record(m.page) || m.page.cursor!==0 || m.page.has_more!==false || m.page.next_cursor!==undefined || m.page.total!==m.items.length) return
  const rows:Row[]=[]
  for(const row of m.items){
   if(!record(row) || Object.keys(row).sort().join(',')!=='line,path,text' || typeof row.path!=='string' || !row.path || typeof row.text!=='string' || row.text.length>2000 || !Number.isSafeInteger(row.line) || row.line<1 || /[\x00-\x09\x0b-\x1f\x7f`<>]/.test(row.path+row.text) || Buffer.from(row.path+row.text,'utf8').toString('utf8')!==row.path+row.text) return
   rows.push({path:row.path,line:row.line,text:row.text})
  }
  const output=[`Found ${rows.length} matches`];let current=''
  for(const row of rows){if(current!==row.path){if(current!=='')output.push('');current=row.path;output.push(`${row.path}:`)}output.push(`  Line ${row.line}: ${row.text}`)}
  if(output.join('\n')!==value.output)return
  return rows
 }catch{return}
}
export function markGrepRows<T extends Result>(value:T,context:object):T {
 const strict=toolViewProducerCurrent(context,'line-cap-v1')
 if(!strict && !toolViewProducerCurrent(context,'grep-rows-v1'))return value
 try{if(rowsFor(value,strict))(proofs??=new WeakMap()).set(value,{output:digest(value.output),metadata:JSON.stringify(value.metadata),strict})}catch{}
 return value
}
export function transferGrepRows(from:object,to:Result):void {
 const p=proofs?.get(from);if(!p)return
 try{if(rowsFor(to,p.strict) && p.output===digest(to.output) && p.metadata===JSON.stringify(to.metadata))proofs!.set(to,p)}catch{}
}
export function readGrepRows(value:object):boolean {
 const p=proofs?.get(value);if(!p)return false
 try{const v=value as Result;return !!rowsFor(v,p.strict) && p.output===digest(v.output) && p.metadata===JSON.stringify(v.metadata)}catch{return false}
}
export function framedGrepRows(input:string,metadata:unknown,ref:{messageID:string;partID:string;revision:number;contentHash:string}):string|undefined {
 if(!Number.isSafeInteger(ref.revision)||ref.revision<1||!/^[a-f0-9]{64}$/.test(ref.contentHash)||[ref.messageID,ref.partID].some(v=>!v||v.length>256))return
 const rows=rowsFor({output:input,metadata:metadata as Record<string,unknown>});if(!rows)return
 const groups:Array<{path:string;rows:Array<[number,string]>}>=[]
 for(const row of rows){let group=groups.at(-1);if(!group || group.path!==row.path){group={path:row.path,rows:[]};groups.push(group)}group.rows.push([row.line,row.text])}
 const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
 const frame=`[Grep row view; JSON groups contain exact paths and ordered [line,text] rows. Original: history(${invocation}); follow nextOffset for further pages.]\n${JSON.stringify({groups})}`
 return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}

/** A disclosed ordered prefix; the canonical output remains in History. */
export function framedGrepLineCap(input:string,metadata:unknown,ref:{messageID:string;partID:string;revision:number;contentHash:string},cap:number):string|undefined {
 if(!Number.isSafeInteger(cap)||cap<1||cap>100 || !Number.isSafeInteger(ref.revision)||ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) || [ref.messageID,ref.partID].some(v=>!v||v.length>256))return
 const rows=rowsFor({output:input,metadata:metadata as Record<string,unknown>},true);if(!rows || rows.length<=cap)return
 const groups:Array<{path:string;rows:Array<[number,string]>}>=[]
 for(const row of rows.slice(0,cap)){let group=groups.at(-1);if(!group || group.path!==row.path){group={path:row.path,rows:[]};groups.push(group)}group.rows.push([row.line,row.text])}
 const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
 const frame=`[Grep line cap: retained ${cap}, omitted ${rows.length-cap}, total ${rows.length} matches; omitted matches are not shown. Original: history(${invocation}); follow nextOffset for further pages.]\n${JSON.stringify({groups})}`
 return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}
