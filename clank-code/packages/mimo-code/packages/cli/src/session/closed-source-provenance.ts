/** Process-private constructor evidence. This is not a dispatch or capture grant. */
export type EnvironmentProvenanceHandle = Readonly<{readonly __environmentProvenance: unique symbol}>
type EnvironmentIdentity = Readonly<{sessionID:string;actorID:string;executionRevision:number;rootOperationID:string;innerTurnID:string}>
type EnvironmentScope = Readonly<{ownerAccountID:string;chatID:string;authorityWorkspaceID:string;workspaceRevision:number;mappingRevision:number;mapRevision:number;cwdSHA256:string;sessionCreatedMs:number;providerID:string;modelID:string}>
type EnvironmentRuntime = Readonly<{kind:"runtime_environment";constructorID:"system.environment.v1";outputSHA256:string}>
type EnvironmentLanguage = Readonly<{kind:"bundled_static";constructorID:"system.language.v1";outputSHA256:string}>
type EnvironmentNoVision = Readonly<{kind:"vision_guidance";constructorID:"system.no-vision.v1";outputSHA256:string}>
export type EnvironmentProvenance = Readonly<{version:1;identity:EnvironmentIdentity;scope:EnvironmentScope;fragments:readonly [EnvironmentRuntime,EnvironmentLanguage]}> |
  Readonly<{version:2;identity:EnvironmentIdentity;scope:EnvironmentScope & Readonly<{worktreeSHA256:string}>;scopeKind:"account-chat"|"workspace";imageInput:boolean;fragments:readonly [EnvironmentRuntime,EnvironmentLanguage] | readonly [EnvironmentRuntime,EnvironmentLanguage,EnvironmentNoVision]}>

/** Assembly evidence is partial; it does not qualify a request or grant capture. */
export type AgentProvenanceHandle = Readonly<{readonly __agentProvenance: unique symbol}>
export type PrefixProvenanceHandle = Readonly<{readonly __prefixProvenance: unique symbol}>
export type LegacyPersonaEvidence = Readonly<{constructor:"host.factory-persona.v1";textSHA256:string}>
export type ComposedPersonaEvidence = Readonly<{version:2;constructor:"host.factory-persona-general.v1";textSHA256:string;factorySHA256:string;defaultsSHA256:string;snapshotSHA256:string}>
export type StyledPersonaEvidence = Readonly<{version:3;constructor:"host.factory-persona-style.v1";textSHA256:string;factorySHA256:string;styleSHA256:string;styleMode:"concise"|"brief"|"minimal";styleRevision:number;styleSnapshotSHA256:string;general:Readonly<{defaultsSHA256:string;snapshotSHA256:string}>|null}>
type PrefixBase = Readonly<{coverage:"partial";environment:EnvironmentProvenance;agentSHA256:readonly string[];unknown:readonly string[]}>
type PrefixLegacySkill = Readonly<{kind:"absent"}|{kind:"bundled_static";outputSHA256:string}>
type PrefixSkill = PrefixLegacySkill | Readonly<{kind:"catalog";outputSHA256:string}>
export type AdaptiveHintEvidence = Readonly<{constructor:"native.adaptive-concise-hint.v1";hintSHA256:string;admissionSHA256:string;snapshotSHA256:string;modelWindowSHA256:string;pressureLevel:2|3}>
export type PrefixAssemblyEvidence = (PrefixBase & Readonly<{version:1;skill?:PrefixLegacySkill;persona?:LegacyPersonaEvidence}>) |
  (PrefixBase & Readonly<{version:2;skill?:PrefixSkill;persona?:LegacyPersonaEvidence|ComposedPersonaEvidence|StyledPersonaEvidence;instructions:Readonly<{constructor:"instruction.local-snapshot.v1";manifest:readonly import("./prefix-provenance").InstructionSource[];contentSHA256:readonly string[]}>;managedMemory?:Readonly<{constructor:"managed.memory-guidance.v1";outputSHA256:string}>;adaptiveHint?:AdaptiveHintEvidence}>)

/** Original-loop observation only. No acknowledgement or capture proof is issued. */
import { createHash } from "node:crypto"
import { Log } from "@/util"
const sourceLog = Log.create({service:"source-attempt"})
function refused(reason:string):false { try { sourceLog.debug("source_current_refused", {reason}) } catch {} return false }
import type { ActorExecutionIdentity } from "../actor/turn"
import { ManagedProvider } from "../acp/managed-provider"
import { isAdmittedToolTimingTurn } from "./tool-timing"
import { MessageV2 } from "./message-v2"
import { snapshotSourceParts, layoutHash, type SourceLayout, type SourceGenerated } from "./source-layout"
import { buildSkillSearchReminderV1 } from "./skill-search-reminder"
import type { MessageID, PartID } from "./schema"

export type SourceAttemptHandle = Readonly<{readonly __sourceAttempt: unique symbol}>
type SourceAttempt = {layout?:SourceLayout; generated?:(SourceGenerated & {partID:PartID})[];identity:ActorExecutionIdentity; messageID:MessageID;partID:PartID;digest:string;scope:object;issuer:NonNullable<ReturnType<typeof ManagedProvider.currentActorToolTimingIssuer>>;attempts:number;invalid:boolean;signal?:AbortSignal;signalSettled?:boolean;dispatchID?:string;observation?:SourceWireObservation;currentPrefix?:()=>PrefixAssemblyEvidence | undefined;output?:SourceOutput;outputInvalid?:boolean;wireSettlement?:Promise<void>}
const sourceAttempts = new WeakMap<object,SourceAttempt>()
function sourceAttemptCurrent(state:SourceAttempt):boolean {
  try {
    const id=state.identity
    if (state.invalid) return refused("invalidated")
    if (state.signal?.aborted && !state.signalSettled) return refused("signal_aborted")
    if (!id.rootOperationID || !id.innerTurnID) return refused("turn_identity_missing")
    if (!isAdmittedToolTimingTurn(id,{sessionID:id.sessionID,actorID:id.actorID,executionRevision:id.executionRevision,rootOperationID:id.rootOperationID,innerTurnID:id.innerTurnID})) return refused("turn_not_admitted")
    if (ManagedProvider.currentScope(id.sessionID,id.actorID,id.executionRevision)!==state.scope) return refused("scope_changed")
    if (!state.issuer.isAlive()) return refused("issuer_retired")
    if (ManagedProvider.currentActorToolTimingIssuer(id.sessionID,id.actorID,id.executionRevision,id.rootOperationID)!==state.issuer) return refused("issuer_changed")
    const source=MessageV2.get({sessionID:id.sessionID,messageID:state.messageID})
    if(state.layout) {
      const generated=state.generated ?? []
      const ids=new Set(generated.map(item=>item.partID))
      const original=source.parts.filter(part=>!ids.has(part.id))
      const snapshot=snapshotSourceParts(original,state.partID)
      if(source.info.role!=="user" || source.info.sessionID!==id.sessionID || !snapshot || JSON.stringify(snapshot)!==JSON.stringify(state.layout) ||
          state.layout.layout[state.layout.sourcePosition]?.sha256!==state.digest || state.layout.layout[state.layout.sourcePosition]?.synthetic!==false)
        return refused(source.parts.length!==state.layout.ordered.length+generated.length ? "unregistered_or_missing_part" : "original_layout_mismatch")
      const added=source.parts.slice(state.layout.ordered.length)
      if(added.length!==generated.length || source.parts.length!==state.layout.ordered.length+generated.length)return refused("unregistered_or_missing_part")
      if (!added.every((part,index)=>part.id===generated[index].partID && part.type==="text" && part.synthetic && !part.ignored &&
        createHash("sha256").update(part.text,"utf8").digest("hex")===generated[index].sha256)) return refused("registered_part_mismatch")
      return true
    }
    const visible=source.parts.filter(part=>part.type==="text" && !part.ignored)
    return source.info.role==="user" && source.info.sessionID===id.sessionID && visible.length===1 && visible[0]?.type==="text" &&
      !visible[0].synthetic && visible[0].id===state.partID && source.parts.every(part=>part.type==="text") &&
      createHash("sha256").update(visible[0].text,"utf8").digest("hex")===state.digest
  } catch { return refused("source_read_exception") }
}
/** Only called after the original C2 source admission; cannot be minted from JSON. */
export function createSourceAttempt(identity:ActorExecutionIdentity,source:{messageID:MessageID;partID:PartID;digest:string;layout?:SourceLayout}):SourceAttemptHandle | undefined {
  if (identity.actorID!=="main" || !identity.rootOperationID || !identity.innerTurnID || !source.messageID || !source.partID || !/^[a-f0-9]{64}$/.test(source.digest)) return
  const scope=ManagedProvider.currentScope(identity.sessionID,identity.actorID,identity.executionRevision)
  const issuer=ManagedProvider.currentActorToolTimingIssuer(identity.sessionID,identity.actorID,identity.executionRevision,identity.rootOperationID)
  if (!scope || !issuer) return
  const state:SourceAttempt={identity,messageID:source.messageID,partID:source.partID,digest:source.digest,...(source.layout?{layout:source.layout}:{}),scope,issuer,attempts:0,invalid:false}
  if (!sourceAttemptCurrent(state)) return
  const handle=Object.freeze({}) as SourceAttemptHandle
  sourceAttempts.set(handle,state)
  return handle
}
/** Register only a newly persisted constructor part on the exact admitted root. */
export function registerSourceReminder(handle:SourceAttemptHandle | undefined,part:MessageV2.Part,constructor:SourceGenerated["constructor"]):void {
  const state=handle && sourceAttempts.get(handle)
  if(!state) { refused("registration_handle_missing"); return }
  if(!state.layout) { refused("registration_layout_absent"); return }
  try {
    const generated=state.generated ?? []
    if(generated.length>=4 || generated.some(item=>item.constructor===constructor) ||
       (constructor!=="host-session-env-reminder.v1" && constructor!=="host-session-personal-env.v1" && constructor!=="skill-search-first.v1" && constructor!=="skill-search-refresh.v1") ||
       (constructor.startsWith("skill-search-") && generated.some(item=>item.constructor.startsWith("skill-search-"))) ||
       part.type!=="text" || !part.synthetic || part.ignored || part.messageID!==state.messageID || part.sessionID!==state.identity.sessionID) {refused("registration_part_rejected");state.invalid=true;return}
    state.generated=[...generated,{constructor,position:state.layout.layout.length+generated.length,
      sha256:createHash("sha256").update(part.text,"utf8").digest("hex"),partID:part.id}]
    if(!sourceAttemptCurrent(state))state.invalid=true
    else try { sourceLog.debug("source_registration_accepted", {constructor}) } catch {}
  } catch {refused("registration_exception");state.invalid=true}
}
export function registerSkillReminder(handle:SourceAttemptHandle | undefined,part:MessageV2.Part):void {
  if(part.type!=="text")return registerSourceReminder(handle,part,"skill-search-first.v1")
  const kind=part.text===buildSkillSearchReminderV1("first") ? "skill-search-first.v1" :
    part.text===buildSkillSearchReminderV1("refresh") ? "skill-search-refresh.v1" : undefined
  if(!kind) {invalidateSourceAttempt(handle);return}
  registerSourceReminder(handle,part,kind)
}
export function sourceAttemptRoot(handle:SourceAttemptHandle | undefined) {
  const state=handle && sourceAttempts.get(handle)
  return state && sourceAttemptCurrent(state) ? {messageID:state.messageID,partID:state.partID,digest:state.digest} : undefined
}
export function sourceAttemptLayout(handle:SourceAttemptHandle | undefined) {
  const state=handle && sourceAttempts.get(handle)
  if (!state) { refused("layout_handle_missing"); return }
  if (!state.layout) { refused("layout_absent"); return }
  if (!sourceAttemptCurrent(state)) return
  const generated:SourceGenerated[]=(state.generated ?? []).map(({constructor,position,sha256})=>({constructor,position,sha256}))
  const full=[...state.layout.layout,...generated.map(item=>({position:item.position,sha256:item.sha256,synthetic:true}))]
  return {layout:state.layout.layout,sourcePosition:state.layout.sourcePosition,layoutSHA256:state.layout.layoutSHA256,fullLayoutSHA256:layoutHash(full),generated}
}
export function invalidateSourceAttempt(handle:SourceAttemptHandle | undefined):void { const state=handle && sourceAttempts.get(handle); if (state) state.invalid=true }
export function noteSourceStep(handle:SourceAttemptHandle | undefined,step:number):void {
  const state=handle && sourceAttempts.get(handle)
  if (state && (step!==1 || !sourceAttemptCurrent(state))) state.invalid=true
}
export function registerSourceDispatch(handle:SourceAttemptHandle,identity:unknown,signal:AbortSignal,dispatchID:string):void {
  const state=sourceAttempts.get(handle)
  if (!state) return
  if (state.identity!==identity || state.attempts!==0 || !sourceAttemptCurrent(state) || signal.aborted || !/^[a-f0-9-]{36}$/.test(dispatchID)) { state.invalid=true; return }
  state.attempts=1;state.signal=signal;state.dispatchID=dispatchID
  signal.addEventListener("abort",()=>{if (!state.signalSettled) state.invalid=true},{once:true})
}
/** Pending is an observation only; no acknowledged-body claim exists yet. */
export function sourceAttemptState(handle:SourceAttemptHandle):"pending" | "unavailable" {
  const state=sourceAttempts.get(handle)
  if (!state || state.attempts!==1 || !sourceAttemptCurrent(state)) { if (state) state.invalid=true; return "unavailable" }
  return "pending"
}

/** An acknowledged original wire observation, still not source eligibility. */
type SourceWireBase = Readonly<{dispatchID:string;bodySHA256:string;root:Readonly<{messageID:string;partID:string;digest:string}>;prefix:PrefixAssemblyEvidence}>
export type SourceWireObservation = SourceWireBase & (Readonly<{version:1}> | Readonly<{version:2;profile:'compatible-chat-text-layout-v2';sourcePosition:number;layoutSHA256:string;fullLayoutSHA256:string;generated:readonly SourceGenerated[]}>)
export type SourceFinalFrame = Readonly<{frame:import("./context-inspection").NativeMembershipFrame;system:string[];current:()=>PrefixAssemblyEvidence | undefined}> | Readonly<{version:2;model:object;endpoint:string;system:string[];layout:NonNullable<ReturnType<typeof sourceAttemptLayout>>;current:()=>PrefixAssemblyEvidence | undefined}>
export function acknowledgeSourceWire(handle:SourceAttemptHandle,identity:unknown,dispatchID:string,bodySHA256:string,
    root:{messageID:string;partID:string;digest:string},currentPrefix:()=>PrefixAssemblyEvidence | undefined,layout?:NonNullable<ReturnType<typeof sourceAttemptLayout>>):void {
  const state=sourceAttempts.get(handle)
  if (!state) return
  try {
    const prefix=currentPrefix()
    if (state.identity!==identity || state.attempts!==1 || state.dispatchID!==dispatchID || state.observation || !sourceAttemptCurrent(state) ||
        !/^[a-f0-9]{64}$/.test(bodySHA256) || root.messageID!==state.messageID || root.partID!==state.partID || root.digest!==state.digest ||
        !prefix || prefix.unknown.length!==0 || prefix.environment.identity.sessionID!==state.identity.sessionID ||
        prefix.environment.identity.actorID!==state.identity.actorID || prefix.environment.identity.executionRevision!==state.identity.executionRevision ||
        prefix.environment.identity.rootOperationID!==state.identity.rootOperationID || prefix.environment.identity.innerTurnID!==state.identity.innerTurnID) { state.invalid=true; return }
    const currentLayout=layout ? sourceAttemptLayout(handle) : undefined
    if(layout && (!currentLayout || JSON.stringify(currentLayout)!==JSON.stringify(layout))) {state.invalid=true;return}
    state.currentPrefix=currentPrefix
    state.observation=layout ? Object.freeze({version:2,profile:'compatible-chat-text-layout-v2',dispatchID,bodySHA256,root:Object.freeze({...root}),prefix,sourcePosition:layout.sourcePosition,layoutSHA256:layout.layoutSHA256,fullLayoutSHA256:layout.fullLayoutSHA256,generated:layout.generated}) : Object.freeze({version:1,dispatchID,bodySHA256,root:Object.freeze({...root}),prefix})
  } catch { state.invalid=true }
}
/** Read only while original C2, source and constructor handles still qualify. */
export function readSourceWire(handle:SourceAttemptHandle):SourceWireObservation | undefined {
  const state=sourceAttempts.get(handle)
  if (!state || !state.observation || !state.currentPrefix) return
  if (!sourceAttemptCurrent(state)) {state.invalid=true;return}
  try {
    const prefix=state.currentPrefix()
    if (!prefix || prefix.unknown.length || JSON.stringify(prefix)!==JSON.stringify(state.observation.prefix)) { state.invalid=true; return }
    return state.observation
  } catch { state.invalid=true; return }
}

type SourceOutput = {messageID:MessageID;partID:PartID;streamID:string;text:string;ended:boolean;finished:boolean;stepFinished:boolean;streamEndSeen:boolean;hookCurrent?:()=>boolean;digest?:string}
export type SourceOutputObservation = Readonly<{version:1;dispatchID:string;messageID:string;partID:string;textSHA256:string}>
export function invalidateSourceOutput(handle:SourceAttemptHandle | undefined):void { const state=handle && sourceAttempts.get(handle);if (state) state.outputInvalid=true }
export function startSourceOutput(handle:SourceAttemptHandle | undefined,identity:ActorExecutionIdentity | undefined,messageID:MessageID,partID:PartID,streamID:string):void {
  const state=handle && sourceAttempts.get(handle)
  if (!state) return
  if (state.outputInvalid || state.output || identity!==state.identity || sourceAttemptState(handle!)!=="pending" || typeof streamID!=="string" || !streamID || Buffer.byteLength(streamID,"utf8")>256) {state.outputInvalid=true;return}
  state.output={messageID,partID,streamID,text:"",ended:false,finished:false,stepFinished:false,streamEndSeen:false}
}
export function appendSourceOutput(handle:SourceAttemptHandle | undefined,streamID:string,delta:string):void {
  const state=handle && sourceAttempts.get(handle), out=state?.output
  if (!state || !out || state.outputInvalid) return
  if (out.ended || streamID!==out.streamID || typeof delta!=="string" || out.text.length+delta.length>8192 || !sourceAttemptCurrent(state)) {state.outputInvalid=true;return}
  out.text+=delta
}
export function endSourceOutput(handle:SourceAttemptHandle | undefined,streamID:string,text:string,hookCurrent:()=>boolean):void {
  const state=handle && sourceAttempts.get(handle), out=state?.output
  if (!state || !out || state.outputInvalid) return
  try {
    if (out.ended || streamID!==out.streamID || text!==out.text || !text || Buffer.byteLength(text,"utf8")>8192 || hookCurrent()!==true || !sourceAttemptCurrent(state)) {state.outputInvalid=true;return}
    out.ended=true;out.hookCurrent=hookCurrent;out.digest=createHash("sha256").update(text,"utf8").digest("hex")
  } catch {state.outputInvalid=true}
}
function storedOutputCurrent(state:SourceAttempt,out:SourceOutput):boolean {
  try {
    const message=MessageV2.get({sessionID:state.identity.sessionID,messageID:out.messageID})
    const text=message.parts.filter(part=>part.type==="text")
    return message.info.role==="assistant" && text.length===1 && text[0]?.type==="text" && text[0].id===out.partID &&
      !text[0].ignored && !text[0].synthetic && !!text[0].time?.end && text[0].text===out.text &&
      createHash("sha256").update(text[0].text,"utf8").digest("hex")===out.digest
  } catch {return false}
}
export function finishSourceOutputStep(handle:SourceAttemptHandle | undefined,reason:string):void {
  const state=handle && sourceAttempts.get(handle),out=state?.output
  if (!state || !out || state.outputInvalid) return
  if (!out.ended || out.stepFinished || reason!=="stop" || !out.hookCurrent?.() || !storedOutputCurrent(state,out)) {state.outputInvalid=true;return}
  out.stepFinished=true
}
export function noteSourceOutputFinish(handle:SourceAttemptHandle | undefined):void {
  const state=handle && sourceAttempts.get(handle),out=state?.output
  if (!state || !out || state.outputInvalid) return
  if (!out.stepFinished || out.streamEndSeen) {state.outputInvalid=true;return}
  out.streamEndSeen=true
}
/** Only the owned successful SDK scope may distinguish its own teardown abort. */
export function settleSourceStreamBeforeTeardown(handle:SourceAttemptHandle | undefined, signal:AbortSignal, success:boolean):void {
  const state=handle && sourceAttempts.get(handle),out=state?.output
  if (!state || !out || !success || state.signal!==signal || signal.aborted || state.invalid || state.outputInvalid ||
      !out.ended || !out.stepFinished || !out.streamEndSeen || !sourceAttemptCurrent(state)) return
  try { if (out.hookCurrent?.()===true) state.signalSettled=true } catch { /* unavailable */ }
}
export function finishSourceOutputStream(handle:SourceAttemptHandle | undefined):void {
  const state=handle && sourceAttempts.get(handle),out=state?.output
  if (!state || !out || state.outputInvalid) return
  if (!out.stepFinished || !out.streamEndSeen || out.finished || !out.hookCurrent?.() || !storedOutputCurrent(state,out) || !sourceAttemptCurrent(state)) {state.outputInvalid=true;return}
  out.finished=true
}
export function readSourceOutput(handle:SourceAttemptHandle):SourceOutputObservation | undefined {
  const state=sourceAttempts.get(handle),out=state?.output
  if (!state || !out || state.outputInvalid || !out.finished || !out.digest) return
  if (!out.hookCurrent?.() || !storedOutputCurrent(state,out)) {state.outputInvalid=true;return}
  const wire=readSourceWire(handle)
  if (!wire) {if (state.invalid || !sourceAttemptCurrent(state)) state.outputInvalid=true;return}
  return Object.freeze({version:1,dispatchID:wire.dispatchID,messageID:out.messageID,partID:out.partID,textSHA256:out.digest})
}

/** Optional settlement runs outside ordinary response delivery and expires once. */
export function registerSourceWireSettlement(handle:SourceAttemptHandle, work:Promise<void>):void {
  const state=sourceAttempts.get(handle)
  if (!state || state.wireSettlement) { if (state) state.invalid=true; return }
  state.wireSettlement=work
}
/** Future finish owner may await this bounded observation, never an execution grant. */
export async function readSettledSourceOutput(handle:SourceAttemptHandle):Promise<SourceOutputObservation | undefined> {
  const state=sourceAttempts.get(handle)
  if (!state) return
  if (state.wireSettlement) await state.wireSettlement.catch(()=>{})
  return readSourceOutput(handle)
}
