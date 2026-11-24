import { createHash } from "node:crypto"
import type { Window } from "./overflow"
import type { ImageViewReceipt } from "./model-image-view"

/** Source references are evidence only and never authorize a later request. */
export type ContextSource = { kind: "message" | "part" | "fork_snapshot" | "generated_artifact" | "media_input"; id: string; revision?: string; digest?: string; artifactSHA256?:string; sourceReceiptSHA256?:string; mediaStage?:"prepared"|"omitted"|"native_accepted"; providerDelivery?:"unknown"; messageID?:string; partID?:string; mediaReason?:string|null; derivedView?:ImageViewReceipt }
export type GeneratedContextSource = {messageID:string;partSHA256:string;source:ContextSource}

/** Caller supplies the live finalized original-input check; this is not wire evidence. */
export function mediaSourceEvidence(offer:import("@/acp/openclank-protocol").ManagedTurnStartResult["mediaSourcesV1"], original:{info:{id:string};parts:readonly any[]}, current:()=>boolean):ContextSource[] {
  try {
    if (!offer || !current() || !Array.isArray(offer.items) || offer.items.length>32) return []
    const files=new Map<string,string[]>();let budget=0
    for (const part of original.parts) {
      if (part.type!=='file' || typeof part.mime!=='string' || !part.mime.startsWith('image/') || typeof part.url!=='string') continue
      const match=/^data:image\/[^;,]+;base64,([A-Za-z0-9+/]+={0,2})$/.exec(part.url)
      if (!match) continue
      budget+=match[1].length; if (budget>200*1024*1024) return []
      const bytes=Buffer.from(match[1],'base64'); if (bytes.toString('base64')!==match[1]) continue
      const hash=createHash('sha256').update(bytes).digest('hex');files.set(hash,[...(files.get(hash) ?? []),part.id])
    }
    const counts=new Map<string,number>();for (const item of offer.items) if(item.sourceSHA256) counts.set(item.sourceSHA256,(counts.get(item.sourceSHA256) ?? 0)+1)
    const result=offer.items.map(item=>{
      const matches=item.sourceSHA256 ? files.get(item.sourceSHA256) : undefined
      const accepted=item.stage==='prepared' && item.sourceSHA256 && counts.get(item.sourceSHA256)===1 && matches?.length===1
      return {kind:'media_input' as const,id:item.uploadID,mediaStage:accepted ? 'native_accepted' as const : item.stage,
        providerDelivery:'unknown' as const,mediaReason:item.reason,
        ...(item.sourceSHA256 ? {digest:item.sourceSHA256}:{}),...(accepted ? {messageID:original.info.id,partID:matches![0]}:{})}
    })
    return current() ? result : []
  } catch { return [] }
}
/** A candidate only: host verification must bind its origin to this turn. */
export function generatedAttachmentCandidate(resource:unknown):{uploadID:string;originSHA256:string}|undefined {
  try {
    if (!resource || typeof resource!=='object') return
    const value=resource as {text?:unknown;_meta?:Record<string,unknown>}, candidate=value._meta?.openclankGeneratedSummary
    if (typeof value.text!=='string' || !value.text.startsWith('[Model-generated interpretation; untrusted]\n') || typeof candidate!=='string' || Buffer.byteLength(candidate,'utf8')>16384) return
    const uploadID=JSON.parse(candidate)?.payload?.upload_id
    if (typeof uploadID!=='string' || !uploadID || uploadID.length>256) return
    return {uploadID,originSHA256:createHash('sha256').update(candidate,'utf8').digest('hex')}
  } catch { return }
}
export type ContextFrame = {
  version: 1
  stage: "provider_frame_pre_dispatch" | "model_view_dispatch_observed"
  observedAt: number
  sequence: { attempt: number; transform: number }
  identity: { sessionID: string; actorID?: string; executionRevision?: number; rootOperationID?: string; innerTurnID?: string }
  model: { providerID: string; modelID: string; window: Window }
  sources: ContextSource[]
  omittedSourceCount: number
  sourceMapping: "unknown"
  components: { prompt: Component; instructions: Component; tools: Component }
  tokens: { basis: "unavailable" }
}
type Component = { state: "present" | "absent" | "unavailable"; digest?: string }
const MAX_SOURCES = 128
const MAX_FRAMES = 16

export type NativeMembershipSource = {messageID:string;partID:string;role:'user'|'assistant';digest:string;sourceRevision:'unavailable';messageIndex:number;contentIndex:0}
export type NativeMembershipIdentity = {sessionID:string;rootOperationID:string;actorID:'main';executionRevision:number;innerTurnID:string}
export type NativeMembershipCapture = {sessionID:string;identity:NativeMembershipIdentity; shapes:Array<{message:object;role:'user'|'assistant';parts:Array<{ref:object;id:string;type:string}>}>; entries:Map<number,{part:object;messageID:string;partID:string;role:'user'|'assistant';digest:string}>; sources?:NativeMembershipSource[]}
export type NativeMembershipFrame = {version:1;identity:NativeMembershipIdentity;profile:'openai-chat-native-text-v1';coverage:'partial';messageCount:number;roles:string[];sources:NativeMembershipSource[];model:object}
export type NativeMembershipDelivery = {version:1;identity:NativeMembershipIdentity;profile:'openai-chat-native-text-v1';dispatchID:string;bodyDigest:string;state:'acknowledged';coverage:'partial';sources:NativeMembershipSource[]}

export function sameNativeMembershipIdentity(left:NativeMembershipIdentity, right:any):boolean {
  return !!right && left.sessionID===right.sessionID && left.rootOperationID===right.rootOperationID && left.actorID===right.actorID && left.executionRevision===right.executionRevision && left.innerTurnID===right.innerTurnID
}

function validNativeMembership(value:unknown):value is NativeMembershipDelivery {
  if (!value || typeof value!=='object' || Array.isArray(value)) return false
  const d=value as NativeMembershipDelivery, i=d.identity
  const text=(v:unknown)=>typeof v==='string' && !!v && Buffer.byteLength(v,'utf8')<=256
  if (Object.keys(d).sort().join(',')!=='bodyDigest,coverage,dispatchID,identity,profile,sources,state,version' || d.version!==1 || d.profile!=='openai-chat-native-text-v1' || d.state!=='acknowledged' || d.coverage!=='partial' || !text(d.dispatchID) || !/^[a-f0-9]{64}$/.test(d.bodyDigest) || !i || Object.keys(i).sort().join(',')!=='actorID,executionRevision,innerTurnID,rootOperationID,sessionID' || !text(i.sessionID) || !text(i.rootOperationID) || !text(i.innerTurnID) || i.actorID!=='main' || !Number.isSafeInteger(i.executionRevision) || i.executionRevision<0 || !Array.isArray(d.sources) || !d.sources.length || d.sources.length>32) return false
  const ids=new Set<string>(), positions=new Set<number>();let last=-1
  for (const source of d.sources) {
    if (!source || Object.keys(source).sort().join(',')!=='contentIndex,digest,messageID,messageIndex,partID,role,sourceRevision' || !text(source.messageID) || !text(source.partID) || source.messageID===source.partID || !['user','assistant'].includes(source.role) || !/^[a-f0-9]{64}$/.test(source.digest) || source.sourceRevision!=='unavailable' || !Number.isSafeInteger(source.messageIndex) || source.messageIndex<=last || source.messageIndex>=544 || source.contentIndex!==0 || ids.has(source.messageID) || ids.has(source.partID) || positions.has(source.messageIndex)) return false
    ids.add(source.messageID);ids.add(source.partID);positions.add(source.messageIndex);last=source.messageIndex
  }
  return true
}

function membershipText(item:any):string | undefined {
  const value=item?.content
  if (typeof value==='string') return value
  if (!Array.isArray(value) || value.length!==1 || value[0]?.type!=='text' || typeof value[0].text!=='string') return
  const keys=Object.keys(value[0]).sort().join(',')
  // Pinned SDK prompt conversion may retain an explicitly undefined option;
  // it carries no provider directive and JSON transport cannot serialize it.
  if (keys==='text,type' || (keys==='providerOptions,text,type' && value[0].providerOptions===undefined)) return value[0].text
  return
}

function membershipDigestCounts(messages:readonly any[]):Map<string,number> | undefined {
  const counts=new Map<string,number>();let bytes=0
  for (const item of messages) {
    if (item?.role==='system') continue
    const text=membershipText(item)
    if (typeof text!=='string') return
    if (text.length>1_000_000) return
    bytes+=Buffer.byteLength(text,'utf8');if (bytes>1_000_000) return
    const digest=createHash('sha256').update(text,'utf8').digest('hex');counts.set(digest,(counts.get(digest) ?? 0)+1)
  }
  return counts
}

export function nativeMembershipFrame(capture:NativeMembershipCapture | undefined, messages:unknown, model:object):NativeMembershipFrame | undefined {
  const sources=capture?.sources
  if (!sources?.length || sources.length>32 || !Array.isArray(messages) || messages.length>544) return
  const offset=messages.length-(capture?.shapes.length ?? 0)
  if (offset<0 || messages.slice(0,offset).some(item=>item?.role!=='system' || typeof item.content!=='string')) return
  if (!capture || !capture.shapes.every((shape,index)=>messages[offset+index]?.role===shape.role && typeof membershipText(messages[offset+index])==='string')) return
  const mapped=sources.map(source=>({...source,messageIndex:source.messageIndex+offset}))
  const counts=membershipDigestCounts(messages)
  if (!counts || !mapped.every(source=>{const item=messages[source.messageIndex], text=membershipText(item);return counts.get(source.digest)===1 && item?.role===source.role && typeof text==='string' && createHash('sha256').update(text,'utf8').digest('hex')===source.digest})) return
  return {version:1,identity:capture.identity,profile:'openai-chat-native-text-v1',coverage:'partial',messageCount:messages.length,roles:messages.map(item=>item.role),sources:mapped,model}
}

export function nativeMembershipWire(frame:NativeMembershipFrame | undefined, body:unknown):NativeMembershipFrame | undefined {
  if (!frame || !body || typeof body!=='object' || Array.isArray(body)) return
  const value=body as Record<string,unknown>
  if (value.previous_response_id!=null || value.conversation!=null || value.truncation!=null || value.prompt!=null || !Array.isArray(value.messages) || value.messages.length!==frame.messageCount) return
  const messages=value.messages
  if (messages.some((item:any,index:number)=>!item || typeof item!=='object' || Array.isArray(item) || Object.keys(item).sort().join(',')!=='content,role' || item.role!==frame.roles[index] || (item.role==='system' ? typeof item.content!=='string' : typeof membershipText(item)!=='string'))) return
  const counts=membershipDigestCounts(messages)
  if (!counts || !frame.sources.every(source=>{const item=messages[source.messageIndex], text=membershipText(item);return counts.get(source.digest)===1 && item?.role===source.role && typeof text==='string' && createHash('sha256').update(text,'utf8').digest('hex')===source.digest})) return
  return frame
}

/** Original objects are invocation-local, bounded and never serialized. */
export function captureNativeMembership(identity:NativeMembershipIdentity, messages:readonly any[]):NativeMembershipCapture | undefined {
  const {sessionID}=identity
  if (identity.actorID!=='main' || !identity.rootOperationID || !identity.innerTurnID || !Number.isSafeInteger(identity.executionRevision) || identity.executionRevision<0) return
  if (!sessionID || !Array.isArray(messages) || !messages.length || messages.length>512) return
  const shapes:NativeMembershipCapture['shapes']=[], candidates:Array<{index:number;part:any;messageID:string;role:'user'|'assistant';size:number}>=[], ids=new Set<string>()
  for (let index=0;index<messages.length;index++) {
    const message=messages[index], id=message?.info?.id, role=message?.info?.role
    if (!message || message.info?.sessionID!==sessionID || !['user','assistant'].includes(role) || !Array.isArray(message.parts) || (role==='user' ? message.parts.length!==1 : message.parts.length<1 || message.parts.length>3) || typeof id!=='string' || !id || Buffer.byteLength(id,'utf8')>256 || ids.has(id)) return
    const types=message.parts.map((part:any)=>part?.type).join(',')
    if (role==='user' ? types!=='text' : !['text','step-start,text','text,step-finish','step-start,text,step-finish'].includes(types)) return
    ids.add(id)
    for (const part of message.parts) {
      if (!part || part.sessionID!==sessionID || part.messageID!==id || typeof part.id!=='string' || !part.id || Buffer.byteLength(part.id,'utf8')>256 || ids.has(part.id)) return
      ids.add(part.id)
    }
    const part=message.parts.find((part:any)=>part.type==='text')
    if (!part || part.ignored || part.synthetic || typeof part.text!=='string') return
    shapes.push({message,role,parts:message.parts.map((item:any)=>({ref:item,id:item.id,type:item.type}))})
    const size=part.text.length>8192 ? 8193 : Buffer.byteLength(part.text,'utf8')
    if (size>0 && size<=8192) candidates.push({index,part,messageID:id,role,size})
  }
  const entries:NativeMembershipCapture['entries']=new Map();let bytes=0
  for (let n=candidates.length-1;n>=0 && entries.size<32;n--) {
    const item=candidates[n];if (bytes+item.size>65536) continue
    bytes+=item.size;entries.set(item.index,{part:item.part,messageID:item.messageID,partID:item.part.id,role:item.role,digest:createHash('sha256').update(item.part.text,'utf8').digest('hex')})
  }
  return entries.size ? {sessionID,identity:Object.freeze({...identity}),shapes,entries} : undefined
}

function component(value: unknown): Component {
  if (value === undefined || value === null) return { state: "absent" }
  try {
    const encoded = JSON.stringify(value)
    if (encoded === undefined) return { state: "unavailable" }
    return { state: "present", digest: createHash("sha256").update(encoded).digest("hex") }
  } catch {
    return { state: "unavailable" }
  }
}

export function contextFrame(input: {
  providerID: string; modelID: string; window: Window; sources?: ContextSource[]; omittedSourceCount?: number
  sequence: ContextFrame["sequence"]
  identity: ContextFrame["identity"]
  prompt: unknown; instructions: unknown; tools: unknown
}): ContextFrame {
  const sources = input.sources ?? []
  return {
    version: 1, stage: "provider_frame_pre_dispatch", observedAt: Date.now(),
    identity: { ...input.identity }, sequence: { ...input.sequence },
    model: { providerID: input.providerID, modelID: input.modelID, window: { ...input.window } },
    sources: sources.slice(0, MAX_SOURCES).map((source) => ({ ...source })),
    omittedSourceCount: (input.omittedSourceCount ?? 0) + Math.max(0, sources.length - MAX_SOURCES), sourceMapping: "unknown",
    components: { prompt: component(input.prompt), instructions: component(input.instructions), tools: component(input.tools) },
    tokens: { basis: "unavailable" },
  }
}

/** Build bounded source evidence without materializing the whole transcript's references. */
export function sourceEvidence(messages: readonly { info: { id: string }; parts: readonly { id: string;type?:string;text?:string }[] }[], fork?: ContextSource, generated?:ReadonlyMap<string,GeneratedContextSource>, media:readonly ContextSource[] = []) {
  const sources: ContextSource[] = []
  let omittedSourceCount = 0
  const add = (kind: ContextSource["kind"], item: { id: string }, supplied?: ContextSource) => {
    if (sources.length >= MAX_SOURCES) { omittedSourceCount++; return }
    const digest = supplied?.digest ?? component(item).digest
    sources.push({ kind, id: item.id, ...(digest ? { digest } : {}), ...(supplied?.revision ? { revision: supplied.revision } : {}), ...(supplied?.artifactSHA256 ? {artifactSHA256:supplied.artifactSHA256}:{}), ...(supplied?.sourceReceiptSHA256 ? {sourceReceiptSHA256:supplied.sourceReceiptSHA256}:{}) })
  }
  for (const item of media) { if (sources.length >= MAX_SOURCES) omittedSourceCount++; else sources.push({...item}) }
  if (fork) add("fork_snapshot", fork, fork)
  for (const message of messages) {
    add("message", message.info)
    for (const part of message.parts) {
      add("part", part)
      const proof=generated?.get(part.id)
      if (proof && proof.messageID===message.info.id && part.type==='text' && typeof part.text==='string' && createHash('sha256').update(part.text,'utf8').digest('hex')===proof.partSHA256)
        add('generated_artifact',proof.source,proof.source)
    }
  }
  return { sources, omittedSourceCount }
}

/** Unknown or malformed existing receipts are preserved, not interpreted or overwritten. */
export function appendContextFrame(metadata: Record<string, unknown> | undefined, frame: ContextFrame) {
  const previous = metadata?.contextInspection
  if (previous !== undefined) {
    if (!previous || typeof previous !== "object" || Array.isArray(previous)) return undefined
    const value = previous as Record<string, unknown>
    if (value.version !== 1 || !Array.isArray(value.frames) || value.frames.length > MAX_FRAMES ||
        !Number.isSafeInteger(value.omittedFrameCount) || (value.omittedFrameCount as number) < 0 ||
        value.frames.some((item) => {
          if (!item || typeof item !== "object" || Array.isArray(item)) return true
          const frame = item as Partial<ContextFrame>
          return frame.version !== 1 || !["provider_frame_pre_dispatch","model_view_dispatch_observed"].includes(frame.stage ?? "") ||
            !frame.identity || typeof frame.identity.sessionID !== "string" || !frame.model || !frame.components ||
            !frame.sequence || !Number.isSafeInteger(frame.sequence.attempt) || !Number.isSafeInteger(frame.sequence.transform) ||
            !Array.isArray(frame.sources) || frame.sources.length > MAX_SOURCES ||
            !Number.isSafeInteger(frame.omittedSourceCount) || (frame.omittedSourceCount ?? -1) < 0 ||
            frame.sourceMapping !== "unknown" || frame.tokens?.basis !== "unavailable"
        })) return undefined
  }
  const value = previous as { frames: ContextFrame[]; omittedFrameCount: number } | undefined
  const frames = [...(value?.frames ?? []), frame]
  const limited=frames.slice(-MAX_FRAMES)
  const comparable=({stage: _stage,observedAt: _observedAt,...rest}:ContextFrame)=>JSON.stringify(rest)
  const kept=limited.filter((item,index)=>item.stage!=='model_view_dispatch_observed' ||
    limited.slice(0,index).some(pre=>pre.stage==='provider_frame_pre_dispatch' && comparable(pre)===comparable(item)))
  const omittedFrameCount = (value?.omittedFrameCount ?? 0) + frames.length - kept.length
  if (!Number.isSafeInteger(omittedFrameCount)) return undefined
  return { ...metadata, contextInspection: { ...(previous as object ?? {}), version: 1, frames: kept, omittedFrameCount } }
}

// One assistant owns both writers; never let async saves overwrite sibling evidence.
const evidenceTails = new WeakMap<object, Promise<void>>()
function serializeEvidence(owner: object, work: () => Promise<void>): Promise<void> {
  const previous = evidenceTails.get(owner) ?? Promise.resolve()
  const current = previous.catch(() => {}).then(work)
  evidenceTails.set(owner, current)
  void current.finally(() => { if (evidenceTails.get(owner) === current) evidenceTails.delete(owner) }).catch(() => {})
  return current
}

function adoptEvidenceMetadata<T extends {metadata?:Record<string,unknown>}>(assistant:T,saved:unknown,fallback:Record<string,unknown>) {
  const metadata=saved && typeof saved==='object' && 'metadata' in saved ? (saved as T).metadata : fallback
  if (metadata?.contextInspection !== undefined) assistant.metadata={...assistant.metadata,contextInspection:metadata.contextInspection}
}

/** Actual processor callback: persist first, then publish the saved assistant metadata. */
export function contextFrameWriter<T extends { metadata?: Record<string, unknown> }>(assistant: T, save: (message: T) => Promise<unknown>, unavailable: () => void, current:()=>boolean=()=>true) {
  return (frame: ContextFrame) => serializeEvidence(assistant, async () => {
    try {
      if (frame.stage === "model_view_dispatch_observed") {
        if (!current() || !frame.sources.some(source=>source.kind==='media_input' && source.derivedView)) { unavailable(); return }
        const previous=(assistant.metadata?.contextInspection as {frames?:ContextFrame[]} | undefined)?.frames
        const comparable=({stage: _stage,observedAt: _observedAt,...rest}:ContextFrame)=>JSON.stringify(rest)
        if (!Array.isArray(previous) || !previous.some(item=>item.stage==='provider_frame_pre_dispatch' && comparable(item)===comparable(frame))) { unavailable(); return }
      }
      const metadata = appendContextFrame(assistant.metadata, frame)
      if (!metadata) { unavailable(); return }
      const saved = await save({ ...assistant, metadata })
      if (!current()) { unavailable(); return }
      adoptEvidenceMetadata(assistant,saved,metadata)
    } catch { unavailable() }
  })
}

/** Validate persisted receipt at the read boundary and drop all unknown fields. */
export function projectContextInspection(value: unknown) {
  const record = value as { version?: unknown; frames?: unknown; omittedFrameCount?: unknown } | null
  if (!record || record.version !== 1 || !Array.isArray(record.frames) || record.frames.length > MAX_FRAMES ||
      !Number.isSafeInteger(record.omittedFrameCount) || (record.omittedFrameCount as number) < 0) return undefined
  const projected: ContextFrame[] = []
  const text = (v: unknown) => typeof v === "string" && v.length > 0 && v.length <= 512
  const num = (v: unknown) => Number.isSafeInteger(v) && (v as number) >= 0
  const componentValue = (v: unknown): Component | undefined => {
    if (!v || typeof v !== "object") return undefined
    const c = v as Component
    if (c.state === "present" && typeof c.digest === "string" && /^[a-f0-9]{64}$/.test(c.digest)) return { state: c.state, digest: c.digest }
    if (c.state === "absent" || c.state === "unavailable") return { state: c.state }
    return undefined
  }
  for (const item of record.frames) {
    if (!item || typeof item !== "object") return undefined
    const f = item as ContextFrame
    if (f.version !== 1 || !["provider_frame_pre_dispatch","model_view_dispatch_observed"].includes(f.stage) || !num(f.observedAt) ||
        !f.identity || !text(f.identity.sessionID) || !f.model || !text(f.model.providerID) || !text(f.model.modelID) ||
        !f.model.window || !num(f.model.window.hard) || !num(f.model.window.effective) || !num(f.model.window.usable) ||
        !["model", "config"].includes(f.model.window.source) || !f.sequence || !num(f.sequence.attempt) || !num(f.sequence.transform) ||
        !Array.isArray(f.sources) || f.sources.length > MAX_SOURCES || !num(f.omittedSourceCount) ||
        f.sourceMapping !== "unknown" || f.tokens?.basis !== "unavailable" || !f.components) return undefined
    const prompt = componentValue(f.components.prompt), instructions = componentValue(f.components.instructions), tools = componentValue(f.components.tools)
    if (!prompt || !instructions || !tools) return undefined
    const sources: ContextSource[] = []
    for (const source of f.sources) {
      if (!source || !["message", "part", "fork_snapshot", "generated_artifact", "media_input"].includes(source.kind) || !text(source.id) ||
          (source.revision !== undefined && !text(source.revision)) ||
          (source.digest !== undefined && !/^[a-f0-9]{64}$/.test(source.digest)) ||
          (source.kind==='generated_artifact' && (!source.digest || !source.revision || !/^[a-f0-9]{64}$/.test(source.artifactSHA256??'') || !/^[a-f0-9]{64}$/.test(source.sourceReceiptSHA256??'')))) return undefined
      if (source.kind==='media_input') {
        const reasons=[null,'too_large_bytes','too_many_pixels','side_too_long','unsupported_type','type_mismatch','unreadable_header','route_without_vision']
        if (!['prepared','omitted','native_accepted'].includes(source.mediaStage ?? '') || source.providerDelivery!=='unknown' || !reasons.includes(source.mediaReason ?? null) ||
            (source.mediaStage==='native_accepted' ? (!text(source.messageID) || !text(source.partID) || !source.digest) : (source.messageID!==undefined || source.partID!==undefined))) return undefined
        const view=source.derivedView
        if (view !== undefined && (source.mediaStage!=='native_accepted' || !source.digest ||
            !view || Object.keys(view).sort().join(',')!=='derivedLength,derivedMime,derivedSHA256,height,originalSHA256,profile,providerDelivery,stage,width' ||
            view.profile!=='image-downscale-v1' || view.originalSHA256!==source.digest || !/^[a-f0-9]{64}$/.test(view.derivedSHA256) ||
            view.derivedMime!=='image/png' || view.stage!=='model_view_pre_dispatch' || view.providerDelivery!=='unknown' ||
            !Number.isSafeInteger(view.width) || view.width<1 || view.width>2048 || !Number.isSafeInteger(view.height) || view.height<1 || view.height>2048 ||
            !Number.isSafeInteger(view.derivedLength) || view.derivedLength<1 || view.derivedLength>256*1024)) return undefined
        sources.push({kind:'media_input',id:source.id,mediaStage:source.mediaStage,providerDelivery:'unknown',mediaReason:source.mediaReason ?? null,
          ...(source.digest ? {digest:source.digest}:{}),...(source.messageID ? {messageID:source.messageID}:{}),...(source.partID ? {partID:source.partID}:{}),...(view ? {derivedView:{...view}}:{})})
        continue
      }
      sources.push({kind: source.kind, id: source.id, ...(source.revision ? {revision: source.revision} : {}), ...(source.digest ? {digest: source.digest} : {}), ...(source.artifactSHA256 ? {artifactSHA256:source.artifactSHA256}:{}), ...(source.sourceReceiptSHA256 ? {sourceReceiptSHA256:source.sourceReceiptSHA256}:{})})
    }
    const identity = f.identity
    if ((identity.actorID !== undefined && !text(identity.actorID)) || (identity.rootOperationID !== undefined && !text(identity.rootOperationID)) ||
        (identity.innerTurnID !== undefined && !text(identity.innerTurnID)) || (identity.executionRevision !== undefined && !num(identity.executionRevision))) return undefined
    projected.push({version:1, stage:f.stage, observedAt:f.observedAt, sequence:{attempt:f.sequence.attempt, transform:f.sequence.transform},
      identity:{sessionID:identity.sessionID, ...(identity.actorID ? {actorID:identity.actorID} : {}),
        ...(identity.executionRevision !== undefined ? {executionRevision:identity.executionRevision} : {}),
        ...(identity.rootOperationID ? {rootOperationID:identity.rootOperationID} : {}), ...(identity.innerTurnID ? {innerTurnID:identity.innerTurnID} : {})},
      model:{providerID:f.model.providerID, modelID:f.model.modelID, window:{hard:f.model.window.hard, effective:f.model.window.effective, usable:f.model.window.usable, source:f.model.window.source}},
      sources, omittedSourceCount:f.omittedSourceCount, sourceMapping:"unknown", components:{prompt,instructions,tools}, tokens:{basis:"unavailable"}})
  }
  const comparable=({stage: _stage,observedAt: _observedAt,...rest}:ContextFrame)=>JSON.stringify(rest)
  if (projected.some((frame,index)=>frame.stage==='model_view_dispatch_observed' &&
      (!frame.sources.some(source=>source.kind==='media_input' && source.derivedView) ||
      !projected.slice(0,index).some(item=>item.stage==='provider_frame_pre_dispatch' && comparable(item)===comparable(frame))))) return undefined
  const rawDeliveries = (record as {nativeDeliveries?: unknown}).nativeDeliveries
  const deliveries: NativeTextDelivery[] = []
  if (rawDeliveries !== undefined) {
    if (!Array.isArray(rawDeliveries) || rawDeliveries.length > MAX_FRAMES) return undefined
    for (const value of rawDeliveries) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
      const d = value as NativeTextDelivery
      const i = d.identity, source = d.source
      if (Object.keys(d).sort().join(",") !== "bodyDigest,coverage,dispatchID,identity,profile,source,state,version" ||
          d.version !== 1 || d.profile !== "openai-chat-single-user-text-v1" || d.state !== "acknowledged" || d.coverage !== "partial" ||
          !text(d.dispatchID) || !/^[a-f0-9]{64}$/.test(d.bodyDigest) || !i || !text(i.sessionID) || !text(i.rootOperationID) ||
          Object.keys(i).sort().join(",") !== "actorID,executionRevision,innerTurnID,rootOperationID,sessionID" ||
          i.actorID !== "main" || !num(i.executionRevision) || !text(i.innerTurnID) || !source ||
          Object.keys(source).sort().join(",") !== "digest,messageID,partID,sourceRevision" || !text(source.messageID) || !text(source.partID) ||
          !/^[a-f0-9]{64}$/.test(source.digest) || source.sourceRevision !== "unavailable") return undefined
      deliveries.push({version:1, identity:{sessionID:i.sessionID,rootOperationID:i.rootOperationID,actorID:"main",executionRevision:i.executionRevision,innerTurnID:i.innerTurnID}, profile:d.profile,dispatchID:d.dispatchID,bodyDigest:d.bodyDigest,source:{messageID:source.messageID,partID:source.partID,digest:source.digest,sourceRevision:"unavailable"},state:"acknowledged",coverage:"partial"})
    }
  }
  const rawMembership=(record as {nativeMemberships?:unknown}).nativeMemberships
  if (rawMembership!==undefined && (!Array.isArray(rawMembership) || rawMembership.length>MAX_FRAMES || rawMembership.some(item=>!validNativeMembership(item)))) return undefined
  return {version:1, frames:projected, omittedFrameCount:record.omittedFrameCount as number, ...(rawDeliveries !== undefined ? {nativeDeliveries:deliveries} : {}), ...(rawMembership!==undefined ? {nativeMemberships:rawMembership as NativeMembershipDelivery[]} : {})}
}

/** Content-free native-version evidence. IDs are not host-source authority. */
export type NativeTextEvidence = { messageID: string; partID: string; digest: string; sourceRevision: "unavailable" }
export type NativeTextSnapshot = { version: 1; sources: NativeTextEvidence[]; coverage: "partial"; omitted: number }
type NativeTextMessage = { info: { id: string; role: string }; parts: readonly { id: string; type: string; text?: string; ignored?: boolean; synthetic?: boolean }[] }

/** Snapshot before untrusted message transforms; never retain prompt text. */
export function snapshotNativeText(messages: readonly NativeTextMessage[]): NativeTextSnapshot {
  const sources: NativeTextEvidence[] = [], messageIDs = new Set<string>(), partIDs = new Set<string>()
  let omitted = 0
  for (const message of messages) {
    const duplicateMessage = messageIDs.has(message.info.id)
    messageIDs.add(message.info.id)
    for (const part of message.parts) {
      const duplicate = duplicateMessage || partIDs.has(part.id)
      partIDs.add(part.id)
      if (duplicate || message.info.role !== "user" || part.type !== "text" || part.ignored || part.synthetic || typeof part.text !== "string" || sources.length >= MAX_SOURCES) { omitted++; continue }
      sources.push({messageID: message.info.id, partID: part.id, digest: createHash("sha256").update(part.text, "utf8").digest("hex"), sourceRevision: "unavailable"})
    }
  }
  // A duplicate must invalidate its first occurrence too.
  const ids = new Set<string>()
  const duplicateIDs = new Set<string>()
  for (const message of messages) for (const part of message.parts) { if (ids.has(part.id)) duplicateIDs.add(part.id); ids.add(part.id) }
  const users = messages.filter((message) => message.info.role === "user")
  const visible = users.length === 1 ? users[0].parts.filter((part) => !part.ignored && !part.synthetic) : []
  const firstCut = users.length === 1 && visible.length === 1 && visible[0].type === "text"
  const retained = firstCut ? sources.filter((source) => !duplicateIDs.has(source.partID) && messages.filter((message) => message.info.id === source.messageID).length === 1) : []
  return {version:1, sources:retained, coverage:"partial", omitted:omitted + sources.length - retained.length}
}

/** Forward only unchanged ordered identities after plugin processing. */
export function reconcileNativeText(before: NativeTextSnapshot, messages: readonly NativeTextMessage[]): NativeTextSnapshot {
  const after = snapshotNativeText(messages)
  const retained: NativeTextEvidence[] = []
  let cursor = 0
  for (const source of before.sources) {
    const index = after.sources.findIndex((candidate, position) => position >= cursor && candidate.messageID === source.messageID && candidate.partID === source.partID && candidate.digest === source.digest)
    if (index < 0) continue
    retained.push(source)
    cursor = index + 1
  }
  return {version:1, sources:retained, coverage:"partial", omitted:before.omitted + after.omitted + before.sources.length - retained.length}
}

/** Known first cut: one user text source, at the first and only user position. */
export function nativeSingleUserWire(snapshot: NativeTextSnapshot | undefined, messages: unknown): NativeTextEvidence | undefined {
  if (!snapshot || snapshot.sources.length !== 1 || !Array.isArray(messages)) return undefined
  const users = messages.filter((message) => message && typeof message === "object" && message.role === "user")
  if (users.length !== 1) return undefined
  const content = users[0].content
  const text = typeof content === "string" ? content : Array.isArray(content) && content.length === 1 && content[0]?.type === "text" ? content[0].text : undefined
  if (typeof text !== "string") return undefined
  return createHash("sha256").update(text, "utf8").digest("hex") === snapshot.sources[0].digest ? snapshot.sources[0] : undefined
}

/** Known two-user compaction frame. Any additional role or part is unknown. */
export type MaintenanceWireCandidate = { source:NativeTextEvidence; promptSHA256:string; attestation:string; wireProfile?:"openai-chat"|"codex-responses"; instructionsSHA256?:string; trustedInstructionsSHA256?:string }
export function maintenanceCompactionWire(candidate:MaintenanceWireCandidate | undefined, messages:unknown): boolean {
  if (!candidate || !Array.isArray(messages) || messages.length !== 2) return false
  const texts = messages.map((message) => {
    if (!message || typeof message !== "object" || message.role !== "user") return undefined
    const content = message.content
    return typeof content === "string" ? content : Array.isArray(content) && content.length === 1 &&
      content[0]?.type === "text" && typeof content[0].text === "string" ? content[0].text : undefined
  })
  return typeof texts[0] === "string" && typeof texts[1] === "string" &&
    createHash("sha256").update(texts[0],"utf8").digest("hex") === candidate.source.digest &&
    createHash("sha256").update(texts[1],"utf8").digest("hex") === candidate.promptSHA256
}

/** Chat body may contain no unclassified prompt-bearing controls. */
export function maintenanceChatWire(candidate:MaintenanceWireCandidate | undefined, body:unknown): boolean {
  if (!body || typeof body !== "object" || Array.isArray(body)) return false
  const value=body as Record<string,unknown>
  if (value.tools != null || value.functions != null || value.function_call != null || value.response_format != null ||
      value.prediction != null || value.audio != null) return false
  return maintenanceCompactionWire(candidate,value.messages)
}

/** Exact installed Responses SDK grammar; unknown history or media is unsupported. */
export function maintenanceResponsesWire(candidate:MaintenanceWireCandidate | undefined, body:unknown): boolean {
  if (!candidate || candidate.wireProfile !== "codex-responses" || !candidate.instructionsSHA256 ||
      !body || typeof body !== "object" || Array.isArray(body)) return false
  const value=body as Record<string,unknown>
  if (typeof value.instructions !== "string" || createHash("sha256").update(value.instructions,"utf8").digest("hex") !== candidate.instructionsSHA256 ||
      value.previous_response_id != null || value.conversation != null || value.truncation != null || value.prompt != null ||
      value.text != null || value.metadata != null || value.background != null ||
      (value.tools != null && (!Array.isArray(value.tools) || value.tools.length !== 0)) || !Array.isArray(value.input) || value.input.length !== 2) return false
  const text=value.input.map((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item) || Object.keys(item).sort().join(",") !== "content,role" || item.role !== "user" ||
        !Array.isArray(item.content) || item.content.length !== 1) return undefined
    const part=item.content[0]
    if (!part || typeof part !== "object" || Array.isArray(part) || Object.keys(part).sort().join(",") !== "text,type" ||
        part.type !== "input_text" || typeof part.text !== "string") return undefined
    return part.text
  })
  return typeof text[0] === "string" && typeof text[1] === "string" &&
    createHash("sha256").update(text[0],"utf8").digest("hex") === candidate.source.digest &&
    createHash("sha256").update(text[1],"utf8").digest("hex") === candidate.promptSHA256
}

/** Turn-local evidence; text is never persisted in an observation receipt. */
export type RecallToolEvidence = { messageID: string; toolCallID: string; attestationID: string; resultSHA256: string; text: string }

/** Ephemeral host-attested data, tracked by its constructed position only. */
export type ContextPrefaceEvidence = { contextPartID:string; attestationID:string; textSHA256:string; text:string; index:number; count:number; wireProfile?:"openai-chat"|"codex-responses"|"compatible-chat"; expectedURL?:string }

/** Bounded text-only comparison input; unknown or multimodal frames are unavailable. */
export function boundedContextSnapshot(messages: unknown, deadline: number): string | undefined {
  if (!Array.isArray(messages) || messages.length > 512 || !Number.isFinite(deadline)) return
  const fragments: {role:string;text:string}[] = []
  let encodedBytes = 2
  for (const message of messages) {
    if (performance.now() >= deadline || !message || !['user','assistant'].includes(message.role)) return
    const parts = typeof message.content === 'string' ? [message.content] : Array.isArray(message.content) ? message.content : undefined
    if (!parts || !parts.length || parts.length > 32 - fragments.length) return
    for (const part of parts) {
      const text = typeof part === 'string' ? part : part?.type === 'text' && typeof part.text === 'string' ? part.text : undefined
      if (typeof text !== 'string' || text.length > 16000 || Buffer.byteLength(text,'utf8') > 16000) return
      // Bound escaped JSON before materializing an encoded fragment.
      let escaped = 0
      for (const char of text) {
        if (performance.now() >= deadline) return
        escaped += Buffer.byteLength(JSON.stringify(char),'utf8') - 2
        if (escaped + encodedBytes + 32 > 16000) return
      }
      const fragment = {role:message.role as string,text}
      encodedBytes += Buffer.byteLength(JSON.stringify(fragment),'utf8') + (fragments.length ? 1 : 0)
      if (encodedBytes > 16000) return
      fragments.push(fragment)
    }
  }
  if (!fragments.length || performance.now() >= deadline) return
  return JSON.stringify(fragments)
}
export type StandingGuidanceEvidence = {attestationID:string;textSHA256:string;text:string;wireProfile:'openai-chat'|'codex-responses';systemSHA256?:string;systemText?:string;messageCount?:number;systemIndex?:number;blockOffset?:number}

/** Prove the exact block survives once in one system frame. */
export function standingGuidanceFrame(candidate:StandingGuidanceEvidence | undefined, messages:unknown):StandingGuidanceEvidence | undefined {
  if (!candidate || !Array.isArray(messages) || createHash('sha256').update(candidate.text,'utf8').digest('hex') !== candidate.textSHA256) return
  if (messages.some(item => item?.role === 'developer')) return
  const systems = messages.map((item,index)=>({item,index})).filter(({item})=>item?.role === 'system')
  if (systems.some(({item})=>typeof item.content !== 'string')) return
  const matches = systems.filter(({item})=>item.content.includes(candidate.text))
  if (matches.length !== 1 || matches[0].item.content.indexOf(candidate.text) !== matches[0].item.content.lastIndexOf(candidate.text)) return
  const systemText = matches[0].item.content, blockOffset = systemText.indexOf(candidate.text)
  return {...candidate,systemText,systemSHA256:createHash('sha256').update(systemText,'utf8').digest('hex'),messageCount:messages.length,systemIndex:matches[0].index,blockOffset}
}

export function standingGuidanceWire(candidate:StandingGuidanceEvidence | undefined, body:unknown):StandingGuidanceEvidence | undefined {
  if (!candidate?.systemText || !candidate.systemSHA256 || !Number.isInteger(candidate.messageCount) || !Number.isInteger(candidate.systemIndex) || !Number.isInteger(candidate.blockOffset) || !body || typeof body !== 'object' || Array.isArray(body)) return
  if (createHash('sha256').update(candidate.systemText,'utf8').digest('hex') !== candidate.systemSHA256) return
  const value=body as Record<string,unknown>
  if (candidate.wireProfile === 'codex-responses') {
    if (candidate.messageCount !== 1 || candidate.systemIndex !== 0 || value.previous_response_id != null || value.conversation != null || value.truncation != null || value.prompt != null || value.instructions !== candidate.systemText || !Array.isArray(value.input) || value.input.some((item:any)=>item?.role==='system' || item?.role==='developer')) return
  } else {
    if (!Array.isArray(value.messages) || value.messages.length !== candidate.messageCount) return
    const system=value.messages[candidate.systemIndex!]
    if (!system || typeof system !== 'object' || Array.isArray(system) || Object.keys(system).sort().join(',') !== 'content,role' || system.role !== 'system' || system.content !== candidate.systemText || value.messages.some((item:any,index:number)=>item?.role==='developer' || (item?.role==='system' && (typeof item.content !== 'string' || (index !== candidate.systemIndex && item.content.includes(candidate.text)))))) return
  }
  if (candidate.systemText.indexOf(candidate.text)!==candidate.blockOffset || candidate.systemText.lastIndexOf(candidate.text)!==candidate.blockOffset) return
  return candidate
}

export function contextPrefaceFrame(candidate: ContextPrefaceEvidence | undefined, messages: unknown): ContextPrefaceEvidence | undefined {
  if (!candidate || !Array.isArray(messages) || messages.length !== candidate.count) return
  const item = messages[candidate.index]
  if (!item || item.role !== 'user') return
  const content = item.content
  const text = typeof content === 'string' ? content : Array.isArray(content) && content.length === 1 && content[0]?.type === 'text' ? content[0].text : undefined
  return text === candidate.text && createHash('sha256').update(text, 'utf8').digest('hex') === candidate.textSHA256 ? candidate : undefined
}

/** Installed Responses SDK text grammar; no server-side history is inferred. */
export function contextResponsesWire(candidate:ContextPrefaceEvidence | undefined, body:unknown): ContextPrefaceEvidence | undefined {
  if (!candidate || candidate.wireProfile !== "codex-responses" || !body || typeof body !== "object" || Array.isArray(body)) return
  const value=body as Record<string,unknown>
  if (value.previous_response_id != null || value.conversation != null || value.truncation != null || value.prompt != null ||
      !Array.isArray(value.input) || value.input.length !== candidate.count) return
  for (const item of value.input) {
    if (!item || typeof item !== "object" || Array.isArray(item) || Object.keys(item).sort().join(",") !== "content,role" ||
        (item.role !== "user" && item.role !== "assistant") || !Array.isArray(item.content) || item.content.length !== 1) return
    const part=item.content[0], expected=item.role === "user" ? "input_text" : "output_text"
    if (!part || typeof part !== "object" || Array.isArray(part) || Object.keys(part).sort().join(",") !== "text,type" ||
        part.type !== expected || typeof part.text !== "string") return
  }
  const selected=value.input[candidate.index]
  if (selected?.role !== "user" || selected.content[0].text !== candidate.text ||
      createHash("sha256").update(candidate.text,"utf8").digest("hex") !== candidate.textSHA256) return
  return candidate
}

/** Forward one exact completed source part to its unique converted call ID. */
export function recallToolConverted(source: unknown, converted: unknown, entries: ReadonlyMap<string, RecallToolEvidence>): RecallToolEvidence | undefined {
  if (entries.size !== 1 || !Array.isArray(source) || !Array.isArray(converted)) return
  const candidate = [...entries.values()][0]
  const parts = source.flatMap((message: any) => Array.isArray(message?.parts)
    ? message.parts.filter((part: any) => part?.type === "tool" && part.callID === candidate.toolCallID)
      .map((part: any) => ({ messageID: message.info?.id, part })) : [])
  if (parts.length !== 1 || parts[0].messageID !== candidate.messageID) return
  const part = parts[0].part
  if (part.state?.status !== "completed" || part.state.output !== candidate.text || part.state.time?.compacted ||
      part.state.providerOutput !== undefined || part.contextSelection?.action === "trim" || (part.state.attachments?.length ?? 0) !== 0) return
  const results = converted.flatMap((message: any) => message?.role === "tool" && Array.isArray(message.content)
    ? message.content.filter((item: any) => item?.type === "tool-result") : [])
  const matching=results.filter((item:any)=>item.toolCallId === candidate.toolCallID)
  if (matching.length !== 1) return
  const output = matching[0].output
  if (!(typeof output === "string" ? output === candidate.text : output?.type === "text" && output.value === candidate.text)) return
  return candidate
}

export function recallToolFrame(candidate: RecallToolEvidence | undefined, messages: unknown): RecallToolEvidence | undefined {
  if (!candidate || !Array.isArray(messages)) return
  const results = messages.flatMap((message: any) => message?.role === "tool" && Array.isArray(message.content)
    ? message.content.filter((item: any) => item?.type === "tool-result") : [])
  const matching=results.filter((item:any)=>item.toolCallId === candidate.toolCallID)
  if (matching.length !== 1) return
  const output = matching[0].output
  return (typeof output === "string" ? output === candidate.text : output?.type === "text" && output.value === candidate.text) ? candidate : undefined
}

/** A known OpenAI Chat wire result must retain the exact call ID and whole text. */
export function recallToolWire(candidate: RecallToolEvidence | undefined, messages: unknown): RecallToolEvidence | undefined {
  if (!candidate || !Array.isArray(messages)) return
  const tools = messages.filter((message: any) => message?.role === "tool" && message.tool_call_id === candidate.toolCallID)
  if (tools.length !== 1 || tools[0].content !== candidate.text) return
  return candidate
}

export type NativeDeliveryIdentity = { sessionID: string; rootOperationID: string; actorID: "main"; executionRevision: number; innerTurnID: string }
export type NativeTextDelivery = { version: 1; identity: NativeDeliveryIdentity; profile: "openai-chat-single-user-text-v1"; dispatchID: string; bodyDigest: string; source: NativeTextEvidence; state: "acknowledged"; coverage: "partial" }
/** Persist only an acknowledged bounded receipt in the existing assistant metadata. */
export function nativeTextDeliveryWriter<T extends { metadata?: Record<string, unknown> }>(assistant: T, save: (message: T) => Promise<unknown>, unavailable: () => void, current:()=>boolean=()=>true) {
  return (receipt: NativeTextDelivery) => serializeEvidence(assistant, async () => {
    try {
      const inspection = assistant.metadata?.contextInspection
      if (!inspection || typeof inspection !== "object" || Array.isArray(inspection)) { unavailable(); return }
      const value = inspection as Record<string, unknown>
      const existing = value.nativeDeliveries
      if (value.version !== 1 || !Array.isArray(value.frames) || (existing !== undefined && (!Array.isArray(existing) || existing.length >= MAX_FRAMES))) { unavailable(); return }
      const metadata = {...assistant.metadata, contextInspection: {...value, nativeDeliveries: [...(existing ?? []), receipt]}}
      const saved=await save({...assistant, metadata})
      if (!current()) { unavailable(); return }
      adoptEvidenceMetadata(assistant,saved,metadata)
    } catch { unavailable() }
  })
}

export function nativeMembershipDeliveryWriter<T extends {metadata?:Record<string,unknown>}>(assistant:T,save:(message:T)=>Promise<unknown>,unavailable:()=>void,current:()=>boolean=()=>true) {
  return (receipt:NativeMembershipDelivery)=>serializeEvidence(assistant,async()=>{
    try {
      const inspection=assistant.metadata?.contextInspection
      if (!validNativeMembership(receipt) || !inspection || typeof inspection!=='object' || Array.isArray(inspection)) {unavailable();return}
      const value=inspection as Record<string,unknown>, existing=value.nativeMemberships
      if (value.version!==1 || !Array.isArray(value.frames) || (existing!==undefined && (!Array.isArray(existing) || existing.length>=MAX_FRAMES || existing.some(item=>!validNativeMembership(item))))) {unavailable();return}
      const metadata={...assistant.metadata,contextInspection:{...value,nativeMemberships:[...(existing ?? []),receipt]}}
      const saved=await save({...assistant,metadata});if (!current()) {unavailable();return};adoptEvidenceMetadata(assistant,saved,metadata)
    } catch {unavailable()}
  })
}
