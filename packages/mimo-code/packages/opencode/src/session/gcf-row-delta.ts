/** Explicit bounded typed-row delta; never a source authority or session cache. */
import { createHash } from 'node:crypto'
import { parseJsonInput, serializeOrderedJson, type Node } from '../util/toon-subset'
import type { GcfArtifact } from './gcf-native'

const POLICY='gcf-generic-v3-safe-integers-v1'
const SCHEMA='open-clank.gcf-row-delta/v1'
type Row=Extract<Node,{k:'o'}>
type Table={node:Extract<Node,{k:'l'}>;rows:Row[];keys:string[];ids:string[];canonical:string;hash:string}
type Result={state:'success';output:string;baseSHA256:string;resultSHA256:string;count:number;changes:number} |
  {state:'bypass'|'refused';reason:string}
const sha=(value:string)=>createHash('sha256').update(value,'utf8').digest('hex')
const closed=(node:Node,keys:string[]): node is Row => node.k==='o' && node.v.length===keys.length && node.v.every(([key],i)=>key===keys[i])
const scalar=(node:Node):boolean=>node.k==='a'
const atom=(node:Node | undefined)=>node?.k==='a' ? node.v : undefined

function table(text:string):Table {
  const node=parseJsonInput(text)
  if (node.k!=='l' || node.v.length<2 || node.v.length>200) throw new Error('table unavailable')
  const rows:Row[]=[], ids:string[]=[]
  let keys:string[] | undefined
  for (const entry of node.v) {
    if (entry.k!=='o' || entry.v.length<1 || entry.v.length>32 || !entry.v.every(([,value])=>scalar(value))) throw new Error('row unavailable')
    const fields=entry.v.map(([key])=>key)
    if (keys && (fields.length!==keys.length || fields.some((key,i)=>key!==keys![i]))) throw new Error('schema changed')
    keys ??= fields
    const id=atom(entry.v.find(([key])=>key==='id')?.[1])
    if (typeof id!=='string' || !id || Buffer.byteLength(id,'utf8')>256 || ids.includes(id)) throw new Error('identity unavailable')
    ids.push(id); rows.push(entry)
  }
  const canonical=serializeOrderedJson(node)
  if (Buffer.byteLength(canonical,'utf8')>51200) throw new Error('table unavailable')
  return {node,rows,keys:keys!,ids,canonical,hash:sha(canonical)}
}
function compatible(base:Table,next:Table):boolean {
  return base.rows.length===next.rows.length && base.keys.length===next.keys.length &&
    base.keys.every((key,i)=>key===next.keys[i]) && base.ids.every((id,i)=>id===next.ids[i])
}
function decodedText(data:Uint8Array):string { return new TextDecoder('utf-8',{fatal:true}).decode(data) }
function ready(artifact:GcfArtifact):boolean { return artifact.availability()==='available' && artifact.decodeAvailability()==='available' }

function applyPayload(baseText:string,payloadText:string):{output:string;baseSHA256:string;resultSHA256:string;count:number;changes:number} {
  const base=table(baseText), payload=parseJsonInput(payloadText)
  if (!closed(payload,['schema','base_sha256','result_sha256','count','changes'])) throw new Error('payload unavailable')
  const [schema,baseHash,resultHash,count,changes]=payload.v.map(([,value])=>value)
  const resultSHA256=atom(resultHash)
  if (atom(schema)!==SCHEMA || atom(baseHash)!==base.hash || typeof resultSHA256!=='string' || !/^[a-f0-9]{64}$/.test(resultSHA256) ||
      atom(count)!==base.rows.length || changes?.k!=='l' || changes.v.length>base.rows.length) throw new Error('base mismatch')
  let previous=-1
  for (const change of changes.v) {
    if (!closed(change,['index','row'])) throw new Error('change unavailable')
    const index=atom(change.v[0]![1]), row=change.v[1]![1]
    if (typeof index!=='number' || !Number.isSafeInteger(index) || index<=previous || index>=base.rows.length ||
        row.k!=='o' || row.v.length!==base.keys.length || !row.v.every(([key,value],i)=>key===base.keys[i] && scalar(value)) ||
        atom(row.v.find(([key])=>key==='id')?.[1])!==base.ids[index]) throw new Error('change unavailable')
    if (serializeOrderedJson(row)===serializeOrderedJson(base.rows[index]!)) throw new Error('unchanged row')
    base.node.v[index]=row
    previous=index
  }
  const output=serializeOrderedJson(base.node)
  if (Buffer.byteLength(output,'utf8')>51200 || sha(output)!==resultSHA256) throw new Error('result mismatch')
  return {output,baseSHA256:base.hash,resultSHA256,count:base.rows.length,changes:changes.v.length}
}

export function encodeRowDelta(baseText:string,nextText:string,artifact:GcfArtifact):Result {
  try {
    if (typeof baseText!=='string' || typeof nextText!=='string' || Buffer.byteLength(baseText,'utf8')+Buffer.byteLength(nextText,'utf8')>102400 || !ready(artifact)) return {state:'refused',reason:'unavailable'}
    const base=table(baseText), next=table(nextText)
    if (!compatible(base,next)) return {state:'refused',reason:'reanchor_required'}
    const changes=next.rows.flatMap((row,index)=>serializeOrderedJson(row)===serializeOrderedJson(base.rows[index]!) ? [] : [`{"index":${index},"row":${serializeOrderedJson(row)}}`])
    const payload=`{"schema":${JSON.stringify(SCHEMA)},"base_sha256":"${base.hash}","result_sha256":"${next.hash}","count":${base.rows.length},"changes":[${changes.join(',')}]}`
    parseJsonInput(payload)
    const input=Buffer.from(payload,'utf8')
    const encoded=artifact.previewEncodeAvailability?.()==='available' && artifact.previewEncode
      ? artifact.previewEncode(input,POLICY) : artifact.encode(input,POLICY)
    if (encoded.state!=='encoded') return {state:'bypass',reason:encoded.reason}
    if (!ready(artifact)) return {state:'refused',reason:'unavailable'}
    const decoded=artifact.decode(encoded.output,POLICY)
    if (decoded.state!=='decoded' || !ready(artifact)) return {state:'refused',reason:'decoder_unavailable'}
    const decodedPayload=decodedText(decoded.output)
    if (serializeOrderedJson(parseJsonInput(decodedPayload))!==payload) return {state:'refused',reason:'roundtrip_mismatch'}
    const applied=applyPayload(baseText,decodedPayload)
    if (applied.output!==next.canonical) return {state:'refused',reason:'roundtrip_mismatch'}
    return {state:'success',...applied,output:decodedText(encoded.output)}
  } catch { return {state:'refused',reason:'invalid'} }
}

export function applyRowDelta(baseText:string,encodedText:string,artifact:GcfArtifact):Result {
  try {
    if (typeof baseText!=='string' || typeof encodedText!=='string' || Buffer.byteLength(baseText,'utf8')>51200 || Buffer.byteLength(encodedText,'utf8')>51200 ||
        Buffer.byteLength(baseText,'utf8')+Buffer.byteLength(encodedText,'utf8')>102400 || !ready(artifact)) return {state:'refused',reason:'unavailable'}
    const encodedBytes=Buffer.from(encodedText,'utf8')
    if (encodedBytes.toString('utf8')!==encodedText) return {state:'refused',reason:'invalid'}
    const decoded=artifact.decode(encodedBytes,POLICY)
    if (decoded.state!=='decoded' || !ready(artifact)) return {state:'refused',reason:'decoder_unavailable'}
    return {state:'success',...applyPayload(baseText,decodedText(decoded.output))}
  } catch { return {state:'refused',reason:'invalid'} }
}
