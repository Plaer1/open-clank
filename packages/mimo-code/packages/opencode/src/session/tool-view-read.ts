/** Exact completed ReadTool output witness; never a filesystem authority. */
import { createHash } from 'node:crypto'
import { toolViewProducerCurrent } from './tool-view-producer-admission'
import { npmLockfileVersions } from './tool-view-lockfile-versions'
import {selectConfigRead,framedConfigRead} from './tool-view-config-read'

type Result = {output:string; metadata?:Record<string,unknown>; attachments?:unknown[]}
type Facts = {snapshotText:string; raw:string[]; offset:number; count:number; cut:boolean; more:boolean; loaded:string[]}
type Ref = {messageID:string; partID:string; revision:number; contentHash:string}
const digest=(s:string)=>createHash('sha256').update(s,'utf8').digest('hex')
const record=(v:unknown):v is Record<string,any>=>!!v && typeof v==='object' && !Array.isArray(v)
const only=(v:unknown, keys:string[])=>record(v) && Object.keys(v).every(k=>keys.includes(k))
const safe=(s:string)=>Buffer.from(s,'utf8').toString('utf8')===s && !/[\x00-\x1f\x7f`<>]/.test(s)
type ReadProfile='read-unnumbered-v1'|'npm-lockfile-versions-v1'|'read-json-whitespace-v1'|'read-repeated-lines-v1'
let proofs:WeakMap<object,{output:string; metadata:string;profile:ReadProfile}> | undefined

function readRows(value:Result):{path:string; lines:string[]} | undefined {
  try {
    const m=value.metadata, f=m?.file
    if (typeof value.output!=='string' || Buffer.byteLength(value.output,'utf8')>51200 || value.attachments?.length ||
        !only(m,['preview','truncated','loaded','fingerprint','encoding','newline','path','media_type','bytes_considered','lines_considered','truncation_reason','file','page']) ||
        !only(f,['contract','operation','path','kind','range','page','bytes_considered','lines_considered','truncation_reason','encoding','newline','media_type','fingerprint','search_mode','items','diagnostics']) ||
        !only(m?.page,['cursor','next_cursor','has_more','total']) || !only(record(f) ? f.page : undefined,['unit','cursor','next_cursor','has_more','returned','total']) ||
        !record(m) || !record(m.page) || !record(f) || !record(f.page) || !only(f.range,['unit','start','end']) || m.truncated!==false || m.truncation_reason!==undefined ||
        !Array.isArray(m.loaded) || m.loaded.length!==0 || typeof m.path!=='string' || !m.path || !safe(m.path) ||
        f.contract!=='open-clank.file-result/v1' || f.operation!=='read' || f.kind!=='text' || f.path!==m.path ||
        f.truncation_reason!==null || f.search_mode!==null || !Array.isArray(f.items) || f.items.length!==0 ||
        !Array.isArray(f.diagnostics) || f.diagnostics.length!==0 ||
        f.encoding!==m.encoding || f.newline!==m.newline || f.media_type!==m.media_type || f.fingerprint!==m.fingerprint ||
        f.bytes_considered!==m.bytes_considered || f.lines_considered!==m.lines_considered ||
        !Number.isSafeInteger(f.lines_considered) || f.lines_considered<1 || f.lines_considered>2000 ||
        f.range.unit!=='line' || f.range.start!==1 || f.range.end!==f.lines_considered ||
        f.page.unit!=='line' || f.page.cursor!==1 || f.page.next_cursor!==null || f.page.has_more!==false ||
        f.page.returned!==f.lines_considered || f.page.total!==f.lines_considered ||
        m.page.cursor!==1 || m.page.next_cursor!==undefined || m.page.has_more!==false || m.page.total!==f.lines_considered) return
    const prefix=`<path>${m.path}</path>\n<type>file</type>\n<content>\n`
    const suffix=`\n\n(End of file - total ${f.lines_considered} lines)\n</content>`
    if (!value.output.startsWith(prefix) || !value.output.endsWith(suffix)) return
    const body=value.output.slice(prefix.length,value.output.length-suffix.length)
    const rows=body.split('\n')
    if (rows.length!==f.lines_considered) return
    const lines=rows.map((row,index)=>{
      const gutter=`${index+1}: `
      if (!row.startsWith(gutter)) throw new Error('read witness unavailable')
      const line=row.slice(gutter.length)
      if (!safe(line) || line.length>2000) throw new Error('read witness unavailable')
      return line
    })
    if (m.preview!==lines.slice(0,20).join('\n')) return
    return {path:m.path,lines}
  } catch { return }
}

export function selectReadView(value:Result, enabled:readonly string[]):ReadProfile | undefined {
  const rows=readRows(value); if (!rows) return
  if (enabled.includes('npm-lockfile-versions-v1') && rows.path.split(/[\\/]/).at(-1)==='package-lock.json' && npmLockfileVersions(rows.lines.join('\n'))) return 'npm-lockfile-versions-v1'
  const config=selectConfigRead(rows.path,rows.lines.join('\n'),{jsonWhitespace:enabled.includes('read-json-whitespace-v1'),repeatedLines:enabled.includes('read-repeated-lines-v1')})
  if (config) return config
  if (enabled.includes('read-unnumbered-v1')) return 'read-unnumbered-v1'
}
export function markReadView<T extends Result>(value:T, context:object, facts:Facts):T {
  const enabled:ReadProfile[]=[]
  if (toolViewProducerCurrent(context,'npm-lockfile-versions-v1')) enabled.push('npm-lockfile-versions-v1')
  if (toolViewProducerCurrent(context,'read-json-whitespace-v1')) enabled.push('read-json-whitespace-v1')
  if (toolViewProducerCurrent(context,'read-repeated-lines-v1')) enabled.push('read-repeated-lines-v1')
  if (toolViewProducerCurrent(context,'read-unnumbered-v1')) enabled.push('read-unnumbered-v1')
  if (!enabled.length) return value
  try {
    const result=readRows(value)
    if (!result || typeof facts.snapshotText!=='string' || Buffer.byteLength(facts.snapshotText,'utf8')>55200 ||
        !Array.isArray(facts.raw) || facts.offset!==1 || facts.count!==result.lines.length ||
        facts.cut!==false || facts.more!==false || !Array.isArray(facts.loaded) || facts.loaded.length!==0) return value
    const source=facts.snapshotText ? facts.snapshotText.split(/\r\n|\n|\r/) : []
    if (source.length && source.at(-1)==='') source.pop()
    if (source.length!==result.lines.length || facts.raw.length!==source.length ||
        source.some((line,i)=>line!==facts.raw[i] || line!==result.lines[i])) return value
    const profile=selectReadView(value,enabled); if (!profile) return value
    ;(proofs??=new WeakMap()).set(value,{output:digest(value.output),metadata:JSON.stringify(value.metadata),profile})
  } catch { /* Optional model view never changes the original result. */ }
  return value
}
export function transferReadView(from:object,to:Result):void {
  const proof=proofs?.get(from); if (!proof) return
  try { if (readRows(to) && proof.output===digest(to.output) && proof.metadata===JSON.stringify(to.metadata)) proofs!.set(to,proof) } catch {}
}
export function readReadViewProfile(value:object):ReadProfile | undefined {
  const proof=proofs?.get(value); if (!proof) return
  try { const v=value as Result; if (readRows(v) && proof.output===digest(v.output) && proof.metadata===JSON.stringify(v.metadata)) return proof.profile } catch {}
}
export function readReadView(value:object):boolean { return readReadViewProfile(value)!==undefined }
export function framedReadView(input:string, metadata:unknown, ref:Ref):string | undefined {
  if (!Number.isSafeInteger(ref.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) ||
      [ref.messageID,ref.partID].some(x=>typeof x!=='string' || !x || x.length>256)) return
  const rows=readRows({output:input,metadata:metadata as Record<string,unknown>}); if (!rows) return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[Read view; path ${JSON.stringify(rows.path)}, lines 1-${rows.lines.length}. Line endings were normalized by the original read. Original tool output: history(${invocation}); follow nextOffset for further pages.]\n${rows.lines.join('\n')}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}

export function framedLockfileView(input:string, metadata:unknown, ref:Ref):string | undefined {
  if (!Number.isSafeInteger(ref.revision) || ref.revision<1 || !/^[a-f0-9]{64}$/.test(ref.contentHash) ||
      [ref.messageID,ref.partID].some(x=>typeof x!=='string' || !x || x.length>256)) return
  const rows=readRows({output:input,metadata:metadata as Record<string,unknown>})
  if (!rows || rows.path.split(/[\\/]/).at(-1)!=='package-lock.json') return
  const view=npmLockfileVersions(rows.lines.join('\n')); if (!view) return
  const invocation=JSON.stringify({operation:'exact_output',message_id:ref.messageID,part_id:ref.partID,revision:ref.revision,content_hash:ref.contentHash,byte_offset:0,byte_limit:8192})
  const frame=`[npm lockfile version view; path ${JSON.stringify(rows.path)}, lockfile version ${view.lockfileVersion}, ${view.rowCount} path/version rows. Integrity hashes, resolved URLs, dependency edges, scripts, names, flags and other metadata are omitted; not a security audit or dependency graph. Original tool output: history(${invocation}); follow nextOffset for further pages.]\n${view.text}`
  return Buffer.byteLength(frame,'utf8')<Buffer.byteLength(input,'utf8') ? frame : undefined
}

export function framedConfigReadView(input:string, metadata:unknown, ref:Ref, profile:'read-json-whitespace-v1'|'read-repeated-lines-v1'):string | undefined {
  const rows=readRows({output:input,metadata:metadata as Record<string,unknown>}); if (!rows) return
  return framedConfigRead(rows.path,rows.lines.join('\n'),rows.lines.length,profile,ref,input)
}
