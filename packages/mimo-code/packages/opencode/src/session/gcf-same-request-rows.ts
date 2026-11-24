/** Pure wire derivation; callers must independently prove source and dispatch currentness. */
import { parseJsonInput, parseJsonInputWithSpans, serializeOrderedJson, type Node } from '../util/toon-subset'
import { encodeRowDelta, applyRowDelta } from './gcf-row-delta'
import { applyGraphDelta, canonicalGraphJson, encodeGraphDelta, parseGraphWrapper } from './gcf-graph-delta'
import type { GcfArtifact } from './gcf-native'

export type SameRequestFrame = Readonly<{callID:string; frame:string; sourceJSON:string}>
type ObjectNode = Extract<Node, {k:'o'}>
type WireItem = {callID:string; content:string; node:Node; position:number}
const atom = (node:Node | undefined) => node?.k === 'a' ? node.v : undefined
const field = (node:ObjectNode, key:string) => node.v.find(([name]) => name === key)?.[1]
const bytes = (value:string) => Buffer.byteLength(value, 'utf8')

function wire(root:Node): WireItem[] | undefined {
  if (root.k !== 'o') return
  const messages = field(root, 'messages')
  if (messages?.k !== 'l') return
  const calls = new Set<string>(), items:WireItem[] = []
  for (let position=0; position<messages.v.length; position++) {
    const message = messages.v[position]!
    if (message.k !== 'o' || atom(field(message,'role')) !== 'tool') continue
    if (message.v.length !== 3 || message.v.some(([key]) => !['role','tool_call_id','content'].includes(key))) return
    const callID = atom(field(message,'tool_call_id')), contentNode = field(message,'content'), content = atom(contentNode)
    if (typeof callID !== 'string' || !callID || bytes(callID)>256 || typeof content !== 'string' || !contentNode || calls.has(callID)) return
    calls.add(callID)
    items.push({callID,content,node:contentNode,position})
  }
  return items
}

function rowTable(source:string): string | undefined {
  const node = parseJsonInput(source)
  if (node.k !== 'l' || node.v.length < 2 || node.v.length > 200) return
  let keys:string[] | undefined
  const ids = new Set<string>()
  for (const row of node.v) {
    if (row.k !== 'o' || !row.v.length || row.v.length > 32 || row.v.some(([,v]) => v.k !== 'a')) return
    const fields = row.v.map(([key]) => key)
    if (keys && (keys.length !== fields.length || keys.some((key,i) => key !== fields[i]))) return
    keys ??= fields
    const id = atom(field(row,'id'))
    if (typeof id !== 'string' || !id || bytes(id)>256 || ids.has(id)) return
    ids.add(id)
  }
  return serializeOrderedJson(node)
}

function verifiedFrame(frame:SameRequestFrame, artifact:GcfArtifact): boolean {
  // A witnessed full raw row array is as self-contained as a full GCF frame.
  if (frame.frame === frame.sourceJSON) return rowTable(frame.sourceJSON) !== undefined
  const prefix = '\nGCF profile=generic\n', index = frame.frame.indexOf(prefix)
  if (index < 0 || index !== frame.frame.lastIndexOf(prefix) || !frame.frame.endsWith('\n')) return false
  const decoded = artifact.decode(Buffer.from(frame.frame.slice(index+1),'utf8'), 'gcf-generic-v3-safe-integers-v1')
  if (decoded.state !== 'decoded' || artifact.availability() !== 'available' || artifact.decodeAvailability() !== 'available') return false
  const output = new TextDecoder('utf-8', {fatal:true}).decode(decoded.output)
  return serializeOrderedJson(parseJsonInput(output)) === rowTable(frame.sourceJSON)
}

type Prepared = {body:string; parsed:ReturnType<typeof parseJsonInputWithSpans>; items:WireItem[]; byID:Map<string,SameRequestFrame>}

function prepare(body:string, frames:readonly SameRequestFrame[], artifact:GcfArtifact, modelID:string): Prepared | undefined {
  if (typeof body !== 'string' || !Array.isArray(frames) || frames.length < 2 || frames.length > 32 ||
      artifact.availability() !== 'available' || artifact.decodeAvailability() !== 'available') return
  const ids = new Set<string>()
  for (const frame of frames) {
    if (!frame || typeof frame.callID !== 'string' || !frame.callID || bytes(frame.callID)>256 ||
        typeof frame.frame !== 'string' || typeof frame.sourceJSON !== 'string' || ids.has(frame.callID)) return
    ids.add(frame.callID)
  }
  const parsed = parseJsonInputWithSpans(body)
  const root = parsed.root
  const allowed = new Set(['model','messages','stream','stream_options','max_tokens','max_completion_tokens','temperature','top_p','seed','store','tools','tool_choice','prompt_cache_key'])
  if (typeof modelID !== 'string' || !modelID || root.k !== 'o' || root.v.some(([key]) => !allowed.has(key)) ||
      atom(field(root,'model')) !== modelID || atom(field(root,'stream')) !== true) return
  const items = wire(root)
  if (!items) return
  return {body,parsed,items,byID:new Map(frames.map(frame => [frame.callID,frame]))}
}

function deriveRows(p:Prepared, artifact:GcfArtifact): {body:string;baseCallID:string;targetCallID:string} | undefined {
  const {body,parsed,items,byID} = p
  const selected: {item:WireItem;frame:SameRequestFrame}[] = []
  for (const item of items) {
    const frame = byID.get(item.callID)
    if (!frame) continue
    if (item.content !== frame.frame) return
    if (!rowTable(frame.sourceJSON)) continue
    if (!verifiedFrame(frame,artifact)) return
    selected.push({item,frame})
  }
  if (selected.length < 2) return
  const base = selected[selected.length-2]!, target = selected[selected.length-1]!
  const delta = encodeRowDelta(base.frame.sourceJSON,target.frame.sourceJSON,artifact)
  if (delta.state !== 'success') return
  const applied = applyRowDelta(base.frame.sourceJSON,delta.output,artifact)
  if (applied.state !== 'success' || applied.output !== rowTable(target.frame.sourceJSON) ||
      applied.baseSHA256 !== delta.baseSHA256 || applied.resultSHA256 !== delta.resultSHA256) return
  const content = `[Experimental same-request row delta. Decode this GCF payload as open-clank.gcf-row-delta/v1 and apply its changed whole rows to the earlier full tool result with call ID ${JSON.stringify(base.item.callID)}. Verify base SHA-256 ${delta.baseSHA256} and result SHA-256 ${delta.resultSHA256}; if the earlier full base is missing or mismatched, this value is unavailable. The canonical source remains unchanged.]\n${delta.output}`
  const span = parsed.spanOf(target.item.node)
  if (!span || body.slice(span.start,span.end)[0] !== '"') return
  const replacement = JSON.stringify(content)
  const next = body.slice(0,span.start) + replacement + body.slice(span.end)
  if (bytes(next) >= bytes(body)) return
  const rescanned = wire(parseJsonInputWithSpans(next).root)
  if (!rescanned || rescanned.length !== items.length || rescanned.some((item,i) => item.callID !== items[i]!.callID || item.position !== items[i]!.position || item.content !== (item.callID === target.item.callID ? content : items[i]!.content))) return
  if (artifact.availability() !== 'available' || artifact.decodeAvailability() !== 'available') return
  return {body:next,baseCallID:base.item.callID,targetCallID:target.item.callID}
}

const POLICY = 'gcf-generic-v3-safe-integers-v1'
type GraphItem = {item:WireItem; frame:SameRequestFrame; wrapper:NonNullable<ReturnType<typeof parseGraphWrapper>>}

/** A witnessed frame is a complete graph wrapper: raw, or a GCF frame whose decoded body is exactly the inner JSON. */
function verifiedGraphFrame(frame:SameRequestFrame, inner:string, artifact:GcfArtifact): boolean {
  if (frame.frame === frame.sourceJSON) return true
  const prefix = '\nGCF profile=generic\n', index = frame.frame.indexOf(prefix)
  if (index < 0 || index !== frame.frame.lastIndexOf(prefix) || !frame.frame.endsWith('\n')) return false
  const decoded = artifact.decode(Buffer.from(frame.frame.slice(index+1),'utf8'), POLICY)
  return decoded.state === 'decoded' && new TextDecoder('utf-8', {fatal:true}).decode(decoded.output) === inner
}

function graphFrame(base:GraphItem, target:GraphItem, artifact:GcfArtifact): string | undefined {
  const enc = encodeGraphDelta(base.wrapper.value, target.wrapper.value)
  if (enc.state !== 'ok') return
  const input = Buffer.from(enc.text,'utf8')
  const encoded = artifact.previewEncodeAvailability?.() === 'available' && artifact.previewEncode ? artifact.previewEncode(input,POLICY) : artifact.encode(input,POLICY)
  if (encoded.state !== 'encoded') return
  const decoded = artifact.decode(encoded.output,POLICY)
  if (decoded.state !== 'decoded') return
  const text = new TextDecoder('utf-8', {fatal:true}).decode(decoded.output)
  if (canonicalGraphJson(JSON.parse(text)) !== enc.text) return
  const applied = applyGraphDelta(base.wrapper.value, JSON.parse(text))
  if (applied.state !== 'ok' || applied.root !== enc.delta.result_root || applied.canonical !== canonicalGraphJson(target.wrapper.value)) return
  const frame = new TextDecoder('utf-8', {fatal:true}).decode(encoded.output)
  if (!frame.startsWith('GCF profile=generic\n') || !frame.endsWith('\n')) return
  return `[Experimental same-request graph delta. Decode this GCF payload as open-clank.gcf-graph-delta/v1 and apply it to the earlier full tool result with call ID ${JSON.stringify(base.item.callID)}: take the open-clank.code-query/v1 JSON that is the decoded value of its output field (decode its GCF frame first if it has one). Verify that the SHA-256 of its canonical form (compact JSON, object keys sorted, array order kept) is ${enc.delta.base_root}. Then: delete the hits and edges listed in removed (a hit by id; an edge by the JSON array text [occurrence_id, from, to, relation_kind]); replace each hit or edge in changed with the entry of the same identity; append added in order; if changed.order lists hits or edges, reorder that list to the given identities; set each field in changed.envelope and delete each field named in changed.envelope_removed. The result, with canonical SHA-256 ${enc.delta.result_root}, is the decoded value of this result's output field; its wrapper fields are in this order: ${target.wrapper.order.join(', ')}; exit_code is 0. If the earlier full base is missing or mismatched, this value is unavailable. The canonical source remains unchanged.]\n${frame}`
}

/** Later graph frames may delta only against an earlier witnessed full frame of this body, never against a replaced one. */
function deriveGraph(p:Prepared, artifact:GcfArtifact): {body:string;baseCallID:string;targetCallID:string} | undefined {
  const {body,parsed,items,byID} = p
  const graphs: GraphItem[] = []
  for (const item of items) {
    const frame = byID.get(item.callID)
    if (!frame) continue
    if (item.content !== frame.frame) return
    const wrapper = parseGraphWrapper(frame.sourceJSON)
    if (wrapper && verifiedGraphFrame(frame,wrapper.inner,artifact)) graphs.push({item,frame,wrapper})
  }
  if (graphs.length < 2) return
  const replaced = new Map<string,{content:string;base:string;span:{start:number;end:number}}>()
  for (let t=1; t<graphs.length; t++) {
    const target = graphs[t]!, span = parsed.spanOf(target.item.node)
    if (!span || body.slice(span.start,span.end)[0] !== '"') continue
    let best: {content:string;base:string} | undefined
    for (const base of graphs.slice(0,t)) {
      if (replaced.has(base.item.callID)) continue
      const content = graphFrame(base,target,artifact)
      if (content && (!best || bytes(content) < bytes(best.content))) best = {content,base:base.item.callID}
    }
    if (best && bytes(JSON.stringify(best.content)) < span.end-span.start) replaced.set(target.item.callID,{...best,span})
  }
  if (!replaced.size) return
  let next = body
  for (const r of [...replaced.values()].sort((a,b) => b.span.start-a.span.start)) next = next.slice(0,r.span.start) + JSON.stringify(r.content) + next.slice(r.span.end)
  if (bytes(next) >= bytes(body)) return
  const rescanned = wire(parseJsonInputWithSpans(next).root)
  if (!rescanned || rescanned.length !== items.length || rescanned.some((item,i) => item.callID !== items[i]!.callID || item.position !== items[i]!.position || item.content !== (replaced.get(item.callID)?.content ?? items[i]!.content))) return
  if (artifact.availability() !== 'available' || artifact.decodeAvailability() !== 'available') return
  const last = [...replaced.entries()].pop()!
  return {body:next,baseCallID:last[1].base,targetCallID:last[0]}
}

/** Returns only a strictly smaller body or undefined; the input is never mutated. Graph deltas need the independent graphReferences bit. */
export function deriveSameRequestRows(body:string, frames:readonly SameRequestFrame[], artifact:GcfArtifact, modelID:string, options?:{graphReferences?:boolean; rows?:boolean}): {body:string;baseCallID:string;targetCallID:string} | undefined {
  try {
    const p = prepare(body,frames,artifact,modelID)
    if (!p) return
    return (options?.rows === false ? undefined : deriveRows(p,artifact)) ?? (options?.graphReferences === true ? deriveGraph(p,artifact) : undefined)
  } catch { return }
}
