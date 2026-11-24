import { getRuntimeEnvironmentUserAgent } from '@ai-sdk/provider-utils'
import { sameRequestRoute } from './same-request-route'
import { verifyGcfRequestFrames, type GcfRequestFramesHandle } from '@/session/gcf-request-frames'
import { deriveSameRequestRows } from '@/session/gcf-same-request-rows'
import type { GcfArtifact } from '@/session/gcf-native'
import { sourceLayoutMessages } from "@/session/source-layout"
import { observeFinalToolWire } from "@/session/tool-definition-provenance"
import { sourceAttemptState, sourceAttemptLayout, sourceAttemptRoot, registerSourceWireSettlement } from "@/session/closed-source-provenance"
import { registerSourceDispatch, invalidateSourceAttempt, acknowledgeSourceWire, type SourceAttemptHandle, type SourceFinalFrame } from "@/session/closed-source-provenance"
import { JSONParseError, TypeValidationError } from "@ai-sdk/provider"
import { isPinnedResponsesModel, isPinnedChatModel, pinnedCompatibleChatEndpoint } from "./malformed-origin"
import { createHash } from "node:crypto"
import { nativeSingleUserWire, maintenanceCompactionWire, maintenanceChatWire, maintenanceResponsesWire, recallToolWire, contextPrefaceFrame, contextResponsesWire, standingGuidanceWire, nativeMembershipWire, sameNativeMembershipIdentity, type NativeMembershipFrame, type NativeMembershipDelivery, type StandingGuidanceEvidence, type MaintenanceWireCandidate, type ContextPrefaceEvidence, type NativeTextEvidence, type NativeTextDelivery, type NativeDeliveryIdentity, type RecallToolEvidence } from "@/session/context-inspection"
/** Explicit managed-only transport observation; never discovers external traffic. */
import { AsyncLocalStorage } from "node:async_hooks"
import { observeAnthropicFailure, issueAnthropicCallback, type AnthropicFailureProbe } from "./fallback-terminal-witness"
import type { AccountSelection } from "./account-selection"
import {
  OpenClankManagedProtocol,
  type ExtensionConnection,
  type LoggingAdmissionResult,
  type LoggingEventRequest,
  type LoggingHeader,
  type LoggingHttp,
  type LoggingMetricCoverage,
} from "@/acp/openclank-protocol"

const BODY_LIMIT = 256 * 1024
const FRAME_LIMIT = 64 * 1024
const EVENT_LIMIT = 256
const QUEUE_LIMIT = 32
type SdkEvidence = {
  usage: Record<string, number>
  metricCoverage: LoggingMetricCoverage
  normalizationProfile: string
  coveredDispatchIDs: string[]
  lossReasons: NonNullable<LoggingEventRequest["lossReasons"]>
  identityCoverage: "complete" | "partial"
}
type CaptureContext = {
  attempt?: AnthropicFailureProbe
  started?: boolean
  binding: AccountSelection.Binding
  connection: ExtensionConnection
  admission: LoggingAdmissionResult | null
  dispatches: string[]
  writes: Set<Promise<unknown>>
  dispatchBase: number
  pendingRetry?: string
  lastSdk?: SdkEvidence
  losses: Set<NonNullable<LoggingEventRequest["lossReasons"]>[number]>
  codexWindows: Map<string, NonNullable<AccountSelection.AttemptEvidence["codexWindows"]>>
  requestRateResets: Map<string, { at: string; source: string; observedAt: string; dispatchID: string }>
  dispatchObservations: Map<string, AccountSelection.DispatchObservation>
  successSteps: Map<string, AccountSelection.DispatchObservation>
}
const contexts = new WeakMap<object, CaptureContext>()

/** Private optional tracking arm; does not grant or attest an attempt. */
export function attachCaptureAttempt(scope: object, attempt: AnthropicFailureProbe): boolean {
  const context = contexts.get(scope)
  if (!context || context.attempt || attempt.closed || context.started || context.dispatches.length || context.writes.size) return false
  context.attempt = attempt
  return true
}

/** Retire only this closed arm after every already-issued callback settles. */
export function retireCaptureAttempt(scope: object, attempt: AnthropicFailureProbe): void {
  const context=contexts.get(scope)
  if (!context || context.attempt!==attempt || !attempt.closed) return
  const release=()=>{ if (contexts.get(scope)===context && context.attempt===attempt && !attempt.callbacks?.size) context.attempt=undefined }
  if (!attempt.callbacks?.size) { release(); return }
  void Promise.allSettled([...attempt.callbacks]).then(release)
}

function issueCapture<T>(authority: CaptureContext, invoke: () => Promise<T>): Promise<T> {
  authority.started = true
  return authority.attempt ? issueAnthropicCallback(authority.attempt, invoke) : invoke()
}
const sdkFailureResets = new WeakMap<object, { scope: object; reset: { at: string; source: string; observedAt: string; dispatchID: string } }>()
const sdkFailureDispatch = new WeakMap<object, { scope: object; observation: AccountSelection.DispatchObservation }>()
type RecallDelivery = { toolCallID: string; attestationID: string; resultSHA256: string; dispatchID: string; bodySHA256: string }
type ContextDelivery = { contextPartID:string; attestationID:string; textSHA256:string; dispatchID:string; bodySHA256:string }
type StandingDelivery = {attestationID:string;textSHA256:string;dispatchID:string;bodySHA256:string;systemSHA256:string;wireProfile:"openai-chat"|"codex-responses"}
export type SameRequestRowsCarrier = { handle:GcfRequestFramesHandle; model:object; artifact:GcfArtifact; current:()=>boolean; graphReferences?:boolean; rows?:boolean }
const sdkDispatchScope = new AsyncLocalStorage<{ rowView?:SameRequestRowsCarrier; sourceAttempt?:SourceAttemptHandle; sourceAbort?:AbortSignal; sourceFrame?:SourceFinalFrame; membership?:NativeMembershipFrame; deliverMembership?:(receipt:NativeMembershipDelivery)=>Promise<void>; standing?:StandingGuidanceEvidence; deliverStanding?:(receipt:StandingDelivery)=>Promise<void>; scope: object; ids: string[]; candidate?: NativeTextEvidence; deliver?: (receipt: NativeTextDelivery) => Promise<void>; identity?: { sessionID: string; rootOperationID?: string; actorID: string; executionRevision: number; innerTurnID?: string }; recall?: RecallToolEvidence; deliverRecall?: (receipt: RecallDelivery) => Promise<void>; context?:ContextPrefaceEvidence; deliverContext?:(receipt:ContextDelivery)=>Promise<void>; maintenance?:MaintenanceWireCandidate; deliverMaintenance?:(receipt:{dispatchID:string;bodySHA256:string;wireProfile:"openai-chat"|"codex-responses";instructionsSHA256?:string;instructionsClass?:"builtin-compaction-v1"})=>Promise<void> }>()

/** One provider SDK step, including its transport retries, never a shared cursor. */
export function withSdkDispatches<T>(scope: object | undefined, ids: string[], run: () => T, candidate?: NativeTextEvidence, deliver?: (receipt: NativeTextDelivery) => Promise<void>, identity?: { sessionID: string; rootOperationID?: string; actorID: string; executionRevision: number; innerTurnID?: string }, recall?: RecallToolEvidence, deliverRecall?: (receipt: RecallDelivery) => Promise<void>, context?:ContextPrefaceEvidence, deliverContext?:(receipt:ContextDelivery)=>Promise<void>, maintenance?:MaintenanceWireCandidate, deliverMaintenance?:(receipt:{dispatchID:string;bodySHA256:string;wireProfile:"openai-chat"|"codex-responses";instructionsSHA256?:string;instructionsClass?:"builtin-compaction-v1"})=>Promise<void>, standing?:StandingGuidanceEvidence, deliverStanding?:(receipt:StandingDelivery)=>Promise<void>, membership?:NativeMembershipFrame, deliverMembership?:(receipt:NativeMembershipDelivery)=>Promise<void>, sourceAttempt?:SourceAttemptHandle, sourceAbort?:AbortSignal, sourceFrame?:SourceFinalFrame, rowView?:SameRequestRowsCarrier): T {
  if (!scope && sourceAttempt) invalidateSourceAttempt(sourceAttempt)
  return scope ? sdkDispatchScope.run({ scope, ids, candidate, deliver, identity, recall, deliverRecall, context, deliverContext, maintenance, deliverMaintenance, standing, deliverStanding, membership, deliverMembership, sourceAttempt, sourceAbort, sourceFrame, rowView }, run) : run()
}
type CodexInnerObservation = { response: Response; dispatchID: string; body: string }
const codexInnerScope = new AsyncLocalStorage<{ final?: CodexInnerObservation }>()
/** Observe the actual Codex upstream send, including any post-401 replay. */
export async function observeCodexInnerSend(url: URL, init: RequestInit, send: () => Promise<Response>): Promise<Response> {
  const scope = codexInnerScope.getStore()
  const body = init.body
  const method = init.method
  const endpoint = url.href
  const dispatchID = scope ? crypto.randomUUID() : undefined
  if (scope) scope.final = undefined
  const response = await send()
  if (scope && dispatchID && method === "POST" && endpoint === "https://chatgpt.com/backend-api/codex/responses" &&
      typeof body === "string" && Buffer.byteLength(body, "utf8") <= 1_000_000 &&
      !response.redirected && response.url === endpoint) {
    scope.final = { response, dispatchID, body }
  }
  return response
}
let pending = 0
const writes = new Set<Promise<unknown>>()

export function installCaptureScope<T extends object>(
  scope: T,
  binding: AccountSelection.Binding,
  connection: ExtensionConnection,
  admission: LoggingAdmissionResult | null,
): T {
  contexts.set(scope, {
    binding,
    connection,
    admission,
    dispatches: [],
    writes: new Set(),
    dispatchBase: 0,
    losses: new Set(admission ? [] : ["admission_unavailable"]),
    requestRateResets: new Map(),
    dispatchObservations: new Map(),
    successSteps: new Map(),
    codexWindows: new Map(),
  })
  return scope
}

/** Bind a private marker to one exact completed SDK step and captured send. */
export function markSdkSuccessStep(scope:object | undefined, ids:string[], metadata:Record<string, any> | undefined) {
  const context=scope ? contexts.get(scope) : undefined
  const clean=metadata && Object.fromEntries(Object.entries(metadata).filter(([key])=>key!=='_openclankSuccessStep'))
  if (!context || ids.length!==1 || context.successSteps.size>=32) return clean
  const observation=context.dispatchObservations.get(ids[0]!)
  if (!observation || observation.httpStatus<200 || observation.httpStatus>299) return clean
  const marker=crypto.randomUUID()
  context.successSteps.set(marker,observation)
  return {...clean,_openclankSuccessStep:{marker}}
}

export function takeSdkSuccessStep(scope:object | undefined, metadata:Record<string, any> | undefined) {
  const entry=metadata?._openclankSuccessStep
  const clean=metadata && Object.fromEntries(Object.entries(metadata).filter(([key])=>key!=='_openclankSuccessStep'))
  const context=scope ? contexts.get(scope) : undefined
  const marker=entry && typeof entry==='object' && Object.keys(entry).length===1 ? entry.marker : undefined
  const observation=typeof marker==='string' ? context?.successSteps.get(marker) : undefined
  if (typeof marker==='string') context?.successSteps.delete(marker)
  return {metadata:clean,observation}
}

/** Parse a bounded RFC3339 calendar value without Date.parse normalization. */
function strictRfc3339(raw: string, maxFractionDigits: number): number | undefined {
  if (!raw || raw.length > 64) return
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$/.exec(raw)
  if (!m) return
  const [,y,mo,d,h,mi,s,fraction,zone] = m
  const year=Number(y), month=Number(mo), day=Number(d), hour=Number(h), minute=Number(mi), second=Number(s)
  const leap=year%4===0 && (year%100!==0 || year%400===0)
  const days=[31,leap?29:28,31,30,31,30,31,31,30,31,30,31]
  if (month<1 || month>12 || day<1 || day>days[month-1]! || hour>23 || minute>59 || second>59 || (fraction && fraction.length>maxFractionDigits)) return
  if (zone === "-00:00" || (zone !== "Z" && (Number(zone.slice(1,3)) > 23 || Number(zone.slice(4,6)) > 59))) return
  const at=Date.parse(raw)
  return Number.isFinite(at) ? at : undefined
}

function anthropicRequestRateReset(headers: Headers, now: number) {
  const raw = headers.get("anthropic-ratelimit-requests-reset")
  if (!raw || raw.length > 40 || Number(raw.slice(0,4)) < 2000) return
  const at = strictRfc3339(raw,3)
  if (at === undefined || at < now || at > now + 31 * 86_400_000) return
  return { at: new Date(at).toISOString(), source: "anthropic.requests.rate-limit.v1", observedAt: new Date(now).toISOString() }
}

const sdkCodexWindows = new WeakMap<object, { scope: object; evidence: NonNullable<AccountSelection.AttemptEvidence["codexWindows"]> }>()
function codexWindowEvidence(headers: Headers, dispatchID: string, now: number): AccountSelection.AttemptEvidence["codexWindows"] {
  const windows: NonNullable<AccountSelection.AttemptEvidence["codexWindows"]>["windows"] = []
  for (const window of ["primary", "secondary"] as const) {
    const prefix = `x-codex-${window}-`
    const used = headers.get(prefix + "used-percent"), duration = headers.get(prefix + "window-minutes"), reset = headers.get(prefix + "reset-at")
    if (used === null && duration === null && reset === null) continue
    if (used === null || !/^(?:0|[1-9]\d{0,4})(?:\.\d{1,6})?$/.test(used)) return
    const usedPercent = Number(used)
    if (!Number.isFinite(usedPercent) || usedPercent > 10000) return
    if (duration !== null && (!/^[1-9]\d{0,5}$/.test(duration) || Number(duration) > 525600)) return
    if (reset !== null && (!/^[1-9]\d{0,11}$/.test(reset) || !Number.isSafeInteger(Number(reset)))) return
    const at = reset === null ? undefined : Number(reset) * 1000
    if (at !== undefined && (!Number.isSafeInteger(at) || at < now || at > now + 366 * 86400000)) return
    windows.push({window,usedPercent,...(duration === null ? {} : {windowMinutes:Number(duration)}),...(at === undefined ? {} : {resetAt:new Date(at).toISOString()})})
  }
  return windows.length ? {source:"openai.codex.subscription.headers.v1",observedAt:new Date(now).toISOString(),dispatchID,windows} : undefined
}

const malformedBindingKeys = ["bindingID","bindingRevision","rootOperationID","scopeKind","operationID","sessionID","actorID","executionRevision","connectionID","providerID","billingLane","modelID","modelRouteID","accountID","credentialRevision","credentialRequired"] as const
function malformedBindingIdentity(binding:AccountSelection.Binding):string {
  return JSON.stringify(malformedBindingKeys.map(key=>binding[key]))
}
function malformedAdmissionIdentity(admission:LoggingAdmissionResult):string {
  const c=admission.context, p=admission.policy
  return JSON.stringify([admission.admissionID,c.providerID,c.operationID,c.rootOperationID,c.instanceID,c.bindingID,c.bindingRevision,c.identityCoverage,c.persistence.numeric,c.persistence.content,c.persistence.reason,p.revision,p.advanced_enabled])
}
const malformedResponses = new WeakMap<object, {scope:object; context:CaptureContext; binding:AccountSelection.Binding; bindingIdentity:string; admission:LoggingAdmissionResult; admissionID:string; admissionIdentity:string; connection:ExtensionConnection; ids:string[]; signal:AbortSignal; observation:AccountSelection.DispatchObservation}>()
/** Direct pinned-adapter parse failure plus one already-observed 2xx send. */
export function markMalformedResponse(error:unknown, scope:object | undefined, ids:string[], signal:AbortSignal, model:object) {
  if (!error || typeof error !== "object" || !scope || !signal || signal.aborted || ids.length !== 1 || !isPinnedResponsesModel(model)
      || !(JSONParseError.isInstance(error) || TypeValidationError.isInstance(error))) return
  const context=contexts.get(scope), observed=context?.dispatchObservations.get(ids[0]!)
  if (!context?.admission || !observed || observed.profile!=="openai.codex.responses.v1" || observed.httpStatus<200 || observed.httpStatus>299) return
  malformedResponses.set(error,{scope,context,binding:context.binding,bindingIdentity:malformedBindingIdentity(context.binding),admission:context.admission,admissionID:context.admission.admissionID,admissionIdentity:malformedAdmissionIdentity(context.admission),connection:context.connection,ids,signal,observation:{...observed}})
}
/** A previously witnessed exact error is sufficient to refuse replay, even if attribution later expires. */
export function hasObservedMalformedResponse(error:unknown, scope:object | undefined):boolean {
  if (!error || typeof error !== "object" || !scope) return false
  return malformedResponses.get(error)?.scope===scope
}

/** A witnessed malformed 2xx is never safe to retry, even before a step. */
export function hasMalformedResponse(error:unknown, scope:object | undefined):boolean {
  if (!error || typeof error !== "object" || !scope) return false
  const proof=malformedResponses.get(error)
  return !!proof && proof.scope===scope && contexts.get(scope)===proof.context && proof.context.binding===proof.binding && malformedBindingIdentity(proof.binding)===proof.bindingIdentity && proof.context.admission===proof.admission && proof.admission.admissionID===proof.admissionID && malformedAdmissionIdentity(proof.admission)===proof.admissionIdentity && proof.context.connection===proof.connection && !proof.signal.aborted && proof.ids.length===1 && proof.ids[0]===proof.observation.captureDispatchID
}

/** Best-effort post-commit annotation; it never controls retry or health. */
export function emitMalformedDiagnostic(error:unknown, scope:object | undefined, refresh?:{previousCredentialRevision:number;credentialRevision:number}) {
  if (!error || typeof error !== "object" || !scope) return
  const proof=malformedResponses.get(error); malformedResponses.delete(error)
  if (!proof || proof.scope!==scope || contexts.get(scope)!==proof.context || proof.context.binding!==proof.binding || malformedBindingIdentity(proof.binding)!==proof.bindingIdentity || proof.context.admission!==proof.admission || proof.admission.admissionID!==proof.admissionID || malformedAdmissionIdentity(proof.admission)!==proof.admissionIdentity || proof.context.connection!==proof.connection || proof.signal.aborted || proof.ids.length!==1 || proof.ids[0]!==proof.observation.captureDispatchID) return
  const context=proof.context, admission=proof.admission
  if (!admission.policy.advanced_enabled) return
  const observation=proof.observation
  queue(context, {
    admissionID:admission.admissionID, dispatchID:observation.captureDispatchID, source:"sdk", sequence:0,
    diagnosticOnly:refresh ? {version:2,kind:"malformed_success",profile:"openai.codex.responses.v1",httpStatus:observation.httpStatus,upstreamDispatchID:observation.upstreamDispatchID,...refresh} : {version:1,kind:"malformed_success",profile:"openai.codex.responses.v1",httpStatus:observation.httpStatus,upstreamDispatchID:observation.upstreamDispatchID},
  }, {bestEffort:true})
}

/** Associate only one actual SDK dispatch with this exact error object. */
export function recordSdkFailure(error: unknown, scope: object | undefined, dispatchIDs: string[]) {
  if (!error || typeof error !== "object") return
  sdkFailureResets.delete(error)
  sdkCodexWindows.delete(error)
  sdkFailureDispatch.delete(error)
  if (!scope || dispatchIDs.length !== 1) return
  const reset = contexts.get(scope)?.requestRateResets.get(dispatchIDs[0])
  if (reset) sdkFailureResets.set(error, { scope, reset })
  const evidence = contexts.get(scope)?.codexWindows.get(dispatchIDs[0])
  if (evidence) sdkCodexWindows.set(error, {scope,evidence})
  const observation = contexts.get(scope)?.dispatchObservations.get(dispatchIDs[0])
  if (observation && observation.httpStatus>=400) sdkFailureDispatch.set(error, {scope,observation})
}

export function takeSdkFailureDispatch(error: unknown, scope: object | undefined) {
  if (!scope || !error || typeof error !== 'object') return
  const item=sdkFailureDispatch.get(error); sdkFailureDispatch.delete(error)
  return item?.scope===scope ? item.observation : undefined
}

export function takeSdkFailureReset(error: unknown, scope: object | undefined) {
  if (!scope || !error || typeof error !== "object") return
  const evidence = sdkFailureResets.get(error)
  sdkFailureResets.delete(error)
  return evidence?.scope === scope ? evidence.reset : undefined
}

export function takeSdkCodexWindows(error: unknown, scope: object | undefined) {
  if (!scope || !error || typeof error !== "object") return
  const item = sdkCodexWindows.get(error)
  sdkCodexWindows.delete(error)
  return item?.scope === scope ? item.evidence : undefined
}

/** Return only the binding identity already attached to this exact scope. */
export function scopeBinding(scope: object): AccountSelection.Binding | undefined {
  const binding = contexts.get(scope)?.binding
  return binding ? Object.freeze({ ...binding, ...(binding.methodPolicy ? { methodPolicy: Object.freeze({ ...binding.methodPolicy }) } : {}) }) as AccountSelection.Binding : undefined
}

/** Read an existing host capture admission; never register a replacement scope. */
export function refreshCaptureIdentity(scope: object, binding: AccountSelection.Binding, host: ExtensionConnection) {
  const context=contexts.get(scope), admission=context?.admission, current=context?.binding
  const keys=['bindingID','bindingRevision','rootOperationID','scopeKind','operationID','sessionID','actorID','executionRevision',
    'connectionID','providerID','billingLane','modelID','modelRouteID','accountID','credentialRevision','credentialRequired'] as const
  if (!context || context.connection!==host || !current || keys.some((key)=>current[key]!==binding[key])
      || current.methodPolicy!==undefined || binding.methodPolicy!==undefined
      || typeof admission?.admissionID!=='string' || !admission.admissionID || admission.admissionID.length>256 || admission.context.persistence.numeric!==true
      || admission.context.bindingID!==binding.bindingID || admission.context.bindingRevision!==binding.bindingRevision
      || admission.context.rootOperationID!==binding.rootOperationID || admission.context.providerID!==binding.providerID
      || !admission.context.instanceID) return undefined
  return {admissionID:admission.admissionID,holderID:admission.context.instanceID,registration:admission}
}

/** Preserve capture authority across a credential CAS for this exact binding. */
export function replaceCapturedScope<T extends object>(
  oldScope: object, nextScope: T, previous: AccountSelection.Binding, next: AccountSelection.Binding, retireOld = false,
): T {
  const context = contexts.get(oldScope)
  const current = context?.binding
  const samePolicy = (a: AccountSelection.Binding["methodPolicy"], b: AccountSelection.Binding["methodPolicy"]) =>
    !a || !b ? a === b : a.version === b.version && a.methodID === b.methodID && a.credentialOwner === b.credentialOwner && a.policyRevision === b.policyRevision
  const keys = ["bindingID", "rootOperationID", "scopeKind", "operationID", "sessionID", "actorID", "executionRevision", "connectionID", "accountID", "modelRouteID"] as const
  if (!context || !current || keys.some((key) => current[key] !== previous[key] || next[key] !== previous[key]) ||
      current.bindingRevision !== previous.bindingRevision || next.bindingRevision !== previous.bindingRevision ||
      current.credentialRevision !== previous.credentialRevision || next.credentialRevision !== (previous.credentialRevision ?? -1) + 1 ||
      !samePolicy(current.methodPolicy, previous.methodPolicy) || !samePolicy(next.methodPolicy, previous.methodPolicy))
    throw new Error("Managed provider refreshed scope identity changed")
  context.binding = next
  if (retireOld) contexts.delete(oldScope)
  contexts.set(nextScope, context)
  return nextScope
}

export function captureScopeEvidence(
  scope: object | undefined,
): Pick<SdkEvidence, "coveredDispatchIDs" | "lossReasons" | "identityCoverage"> {
  const context = scope ? contexts.get(scope) : undefined
  return {
    coveredDispatchIDs: [...(context?.dispatches ?? [])],
    lossReasons: [...(context?.losses ?? [])],
    identityCoverage: context?.admission?.context.identityCoverage ?? ("partial" as const),
  }
}

/** Called only by the provider retry owner after it decides to retry. */
export function markCaptureRetry(scope: object, previousScope: object = scope) {
  const context = contexts.get(scope),
    previous = contexts.get(previousScope)
  if (!context || !previous) return
  context.pendingRetry = previous.dispatches.at(-1)
  if (scope !== previousScope) context.dispatchBase = previous.dispatchBase + previous.dispatches.length
}

export function sdkUsageEvidence(
  raw: unknown,
  scope?: object,
  normalizationProfile = "managed-sdk-inclusive-v1",
  dispatchOffset = 0,
): SdkEvidence {
  const value = object(raw)
  const input = object(value.inputTokenDetails ?? value.inputTokensDetails)
  const output = object(value.outputTokenDetails ?? value.outputTokensDetails)
  const fields: Record<string, unknown> = {
    inputTokens: value.inputTokens ?? value.tokens,
    outputTokens: value.outputTokens,
    totalTokens: value.totalTokens ?? value.tokens,
    cacheReadTokens: value.cachedInputTokens ?? input.cacheReadTokens,
    cacheWriteTokens: input.cacheWriteTokens,
    reasoningTokens: value.reasoningTokens ?? output.reasoningTokens,
    audioInputTokens: value.audioInputTokens,
    audioOutputTokens: value.audioOutputTokens,
    imageInputTokens: value.imageInputTokens,
    imageOutputTokens: value.imageOutputTokens,
    searchRequests: value.searchRequests,
  }
  const usage = Object.fromEntries(
    Object.entries(fields).flatMap(([key, raw]) => (integer(raw) === undefined ? [] : [[key, integer(raw)!]])),
  )
  const evidence = captureScopeEvidence(scope)
  evidence.coveredDispatchIDs = evidence.coveredDispatchIDs.slice(dispatchOffset)
  const names: Record<string, string> = {
    inputTokens: "input_tokens",
    outputTokens: "output_tokens",
    cacheReadTokens: "cache_read_tokens",
    cacheWriteTokens: "cache_write_tokens",
    reasoningTokens: "reasoning_tokens",
  }
  const metricCoverage = Object.fromEntries(
    Object.keys(usage).map((key) => [
      names[key] ?? key,
      { state: "reported" as const, coverage: "complete" as const, source: "sdk" },
    ]),
  )
  return { usage, metricCoverage, normalizationProfile, ...evidence }
}

export function captureSdkEvidence(scope: object | undefined) {
  const context = scope ? contexts.get(scope) : undefined
  return context?.lastSdk
}

export function recordSdkUsage(scope: object | undefined, raw: unknown, dispatchIDs: string[], mainReply = true) {
  const authority = scope ? contexts.get(scope) : undefined
  if (!authority) return
  const evidence = sdkUsageEvidence(raw, scope)
  evidence.coveredDispatchIDs = [...new Set(dispatchIDs)].filter((id) => authority.dispatches.includes(id))
  if (mainReply) authority.lastSdk = evidence
  const admission = authority.admission
  // A step snapshot may cover several fetches. Do not assign that aggregate
  // arbitrarily to the last request; the ACP operation result carries coverage.
  if (!admission?.context.persistence.numeric || evidence.coveredDispatchIDs.length !== 1) return
  const names: Record<string, string> = {
    inputTokens: "input_tokens",
    outputTokens: "output_tokens",
    cacheReadTokens: "cache_read_tokens",
    cacheWriteTokens: "cache_write_tokens",
    reasoningTokens: "reasoning_tokens",
  }
  queue(authority, {
    admissionID: admission.admissionID,
    dispatchID: evidence.coveredDispatchIDs[0],
    source: "sdk",
    sequence: 1,
    terminal: true,
    outcome: "completed",
    observationKind: "final_snapshot",
    metrics: Object.fromEntries(Object.entries(evidence.usage).map(([name, count]) => [names[name] ?? name, count])),
    metricCoverage: evidence.metricCoverage,
    normalizationProfile: evidence.normalizationProfile,
    coveredDispatchIDs: evidence.coveredDispatchIDs,
    lossReasons: evidence.lossReasons,
    identityCoverage: evidence.identityCoverage,
  })
}

function deadline<T>(promise: Promise<T>, timeout: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("logging deadline")), timeout)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

export async function flushCaptures() {
  await deadline(Promise.allSettled([...writes]), 2100).catch(() => {})
}

/** Completeness is scoped to this exact admitted execution, not global capture. */
export async function flushCaptureScope(scope: object | undefined): Promise<boolean> {
  const context = scope ? contexts.get(scope) : undefined
  if (!context) return false
  try {
    await deadline(Promise.allSettled([...context.writes]), 2100)
    return context.writes.size === 0 && context.losses.size === 0
  } catch {
    context.losses.add("delivery_timeout")
    return false
  }
}

function rpc<T>(invoke: () => Promise<T>): Promise<T> {
  // Timeout limits provider waiting, not the real connection resource lease.
  if (pending >= QUEUE_LIMIT) return Promise.reject(new Error("logging RPC capacity"))
  pending++
  let underlying: Promise<T>
  try {
    underlying = invoke()
  } catch (error) {
    pending--
    return Promise.reject(error)
  }
  void underlying
    .finally(() => {
      pending--
    })
    .catch(() => {})
  return underlying
}

function queue(authority: CaptureContext, event: LoggingEventRequest, options?: {bestEffort?:boolean}) {
  if (pending >= QUEUE_LIMIT) {
    if (!options?.bestEffort) authority.losses.add("queue_full")
    return
  }
  const underlying = issueCapture(authority, () => rpc(() =>
    OpenClankManagedProtocol.callHost(authority.connection, "_openclank/logging/v1/events", event),
  ))
  // Optional diagnostics share the bounded RPC queue but cannot delay or
  // downgrade the ordinary capture/response when the host is unavailable.
  if (options?.bestEffort) { void underlying.catch(() => {}); return }
  writes.add(underlying)
  authority.writes.add(underlying)
  void underlying.then(
    (result) => {
      if (!result.accepted) authority.losses.add("write_failed")
    },
    () => authority.losses.add("write_failed"),
  )
  void underlying
    .finally(() => {
      writes.delete(underlying)
      authority.writes.delete(underlying)
    })
    .catch(() => {})
  void deadline(underlying, 2000).catch(() => {
    authority.losses.add("delivery_timeout")
  })
  return underlying
}

const SECRET_HEADER =
  /^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|x-goog-api-key|x-amz-security-token|x-auth-token|x-access-token|.*(?:api[-_]?key|auth[-_]?token|access[-_]?token|secret|credential).*)$/i
export function sanitizedHeaders(raw: HeadersInit | undefined): LoggingHeader[] {
  if (!raw) return []
  const headers = new Headers(raw)
  const grouped = new Map<string, string[]>()
  if (Array.isArray(raw)) {
    for (const [name, value] of raw) {
      const key = name.toLowerCase()
      grouped.set(key, [...(grouped.get(key) ?? []), value])
    }
  } else headers.forEach((value, name) => grouped.set(name.toLowerCase(), [value]))
  // getSetCookie is the only repeated collection exposed by Fetch Headers.
  const cookies = headers.getSetCookie?.()
  if (cookies?.length) grouped.set("set-cookie", cookies)
  let bytes = 0
  const result: LoggingHeader[] = []
  for (const [name, values] of grouped) {
    if (result.length >= 64) {
      result[63].state = "truncated"
      break
    }
    if (SECRET_HEADER.test(name)) {
      result.push({ name, values: [], state: "redacted" })
      continue
    }
    const safe: string[] = []
    let truncated = values.length > 8
    for (const value of values.slice(0, 8)) {
      const bounded = Buffer.from(value).subarray(0, 2048).toString()
      if (bytes + Buffer.byteLength(name) + Buffer.byteLength(bounded) > 32768) {
        truncated = true
        break
      }
      safe.push(bounded)
      bytes += Buffer.byteLength(name) + Buffer.byteLength(bounded)
      truncated ||= bounded !== value
    }
    result.push({ name, values: safe, state: truncated ? "truncated" : "reported" })
  }
  return result
}

function safeEndpoint(raw: string) {
  try {
    const url = new URL(raw)
    return {
      origin: url.origin.slice(0, 512),
      path: url.pathname
        .split("/")
        .map((segment, index, segments) =>
          /^(?:sk-|Bearer|eyJ)|[a-zA-Z0-9_-]{48,}/i.test(segment) ||
          /^(?:key|token|secret|credential|auth)$/i.test(segments[index - 1] ?? "")
            ? "[redacted]"
            : segment,
        )
        .join("/")
        .slice(0, 2048),
    }
  } catch {
    return { origin: "unknown", path: "/" }
  }
}

async function boundedRequestBody(request: Request): Promise<unknown> {
  const clone = request.clone()
  const reader = clone.body?.getReader()
  if (!reader) return undefined
  const decoder = new TextDecoder()
  let result = "",
    bytes = 0
  const end = performance.now() + 100
  try {
    while (true) {
      const remaining = end - performance.now()
      if (remaining <= 0) return { omitted: "unsupported" }
      const next = await deadline(reader.read(), Math.min(50, remaining))
      if (next.done) break
      bytes += next.value.length
      if (bytes > BODY_LIMIT) return { omitted: "truncated", received_bytes: bytes }
      result += decoder.decode(next.value, { stream: true })
    }
    result += decoder.decode()
    try {
      return JSON.parse(result)
    } catch {
      return { text: result }
    }
  } catch {
    return { omitted: "unsupported" }
  } finally {
    void reader.cancel().catch(() => {})
  }
}

export function passiveQuota(
  response: unknown,
  providerID: string,
): OpenClankManagedProtocol.OperationExecuteResult["quota"] | undefined {
  if (providerID !== "openai" && providerID !== "anthropic") return undefined
  const headers = (response as { response?: { headers?: Headers | Record<string, string> } })?.response?.headers
  if (!headers) return undefined
  const get = (name: string) =>
    (headers instanceof Headers ? headers.get(name) : (headers[name] ?? headers[name.toLowerCase()])) ?? undefined
  const integer = (name: string) => {
    const raw = get(name)
    if (raw === undefined || !/^\d+$/.test(raw)) return undefined
    const value = Number(raw)
    return Number.isSafeInteger(value) ? value : undefined
  }
  const resetAt = (name: string, format: "duration" | "rfc3339") => {
    const raw = get(name)
    if (!raw || raw.length > 64) return undefined
    if (format === "rfc3339") {
      const parsed = strictRfc3339(raw, 56)
      return parsed === undefined ? undefined : new Date(parsed).toISOString()
    }
    let total = 0
    const matches = [...raw.matchAll(/(\d+)(ms|s|m|h)/g)]
    if (!matches.length || matches.map((match) => match[0]).join("") !== raw) return undefined
    for (const match of matches) {
      const amount = Number(match[1])
      const multiplier = match[2] === "ms" ? 1 : match[2] === "s" ? 1000 : match[2] === "m" ? 60_000 : 3_600_000
      total += amount * multiplier
      if (!Number.isSafeInteger(total) || total > 86_400_000 * 30) return undefined
    }
    return new Date(Date.now() + total).toISOString()
  }
  const metric = (kind: "requests" | "tokens" | "inputTokens" | "outputTokens") => {
    const anthroName = kind === "inputTokens" ? "input-tokens" : kind === "outputTokens" ? "output-tokens" : kind
    const prefix =
      providerID === "anthropic"
        ? `anthropic-ratelimit-${anthroName}`
        : `x-ratelimit-${kind === "inputTokens" || kind === "outputTokens" ? "limit-tokens" : kind}`
    const resetName =
      providerID === "anthropic"
        ? `${prefix}-reset`
        : `x-ratelimit-reset-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const limitName =
      providerID === "anthropic"
        ? `${prefix}-limit`
        : `x-ratelimit-limit-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const remainingName =
      providerID === "anthropic"
        ? `${prefix}-remaining`
        : `x-ratelimit-remaining-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const reset = resetAt(resetName, providerID === "anthropic" ? "rfc3339" : "duration")
    return {
      ...(integer(limitName) === undefined ? {} : { limit: integer(limitName) }),
      ...(integer(remainingName) === undefined ? {} : { remaining: integer(remainingName) }),
      ...(reset === undefined ? {} : { resetAt: reset }),
    }
  }
  const requests = metric("requests")
  const tokens = metric("tokens")
  const inputTokens = providerID === "anthropic" ? metric("inputTokens") : {}
  const outputTokens = providerID === "anthropic" ? metric("outputTokens") : {}
  const hasMetric = (value: { limit?: number; remaining?: number; resetAt?: string }) =>
    value.limit !== undefined || value.remaining !== undefined || value.resetAt !== undefined
  const result: {
    transport: "documented"
    adapterRevision: string
    requests?: { limit?: number; remaining?: number; resetAt?: string }
    tokens?: { limit?: number; remaining?: number; resetAt?: string }
    inputTokens?: { limit?: number; remaining?: number; resetAt?: string }
    outputTokens?: { limit?: number; remaining?: number; resetAt?: string }
  } = { transport: "documented", adapterRevision: `${providerID}-capacity-v1` }
  if (hasMetric(requests)) result.requests = requests
  if (hasMetric(tokens)) result.tokens = tokens
  if (hasMetric(inputTokens)) result.inputTokens = inputTokens
  if (hasMetric(outputTokens)) result.outputTokens = outputTokens
  return result.requests || result.tokens || result.inputTokens || result.outputTokens ? result : undefined
}

function captureBody(value: unknown, binary: boolean, depth = 0): unknown {
  if (depth > 20) return "[depth limit]"
  if (typeof value === "string")
    return value.startsWith("data:") && !binary
      ? {
          omitted: "embedded_media",
          encoded_bytes: Buffer.byteLength(value),
          media_type: value.split(";", 1)[0].slice(5, 80),
        }
      : value
  if (Array.isArray(value)) return value.slice(0, EVENT_LIMIT).map((child) => captureBody(child, binary, depth + 1))
  if (value === null || typeof value !== "object") return value
  const source = object(value)
  return Object.fromEntries(
    Object.entries(source).flatMap(([key, child]) => {
      const name = key.toLowerCase().replace(/[_-]/g, "")
      if (
        [
          "authorization",
          "proxyauthorization",
          "cookie",
          "setcookie",
          "apikey",
          "credential",
          "credentials",
          "accesstoken",
          "refreshtoken",
          "password",
          "admissionid",
          "leaseid",
        ].includes(name)
      )
        return []
      const media =
        typeof child === "string" &&
        (["b64json", "base64", "imagedata", "audiodata"].includes(name) ||
          (name === "data" && (source.type === "base64" || ["wav", "mp3", "pcm16"].includes(String(source.format)))))
      return [
        [
          key,
          media && !binary
            ? { omitted: "embedded_media", encoded_bytes: Buffer.byteLength(child as string) }
            : captureBody(child, binary, depth + 1),
        ],
      ]
    }),
  )
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function integer(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

export function captureFetch(fetchFn: typeof fetch, scope: AccountSelection.Scope | undefined): typeof fetch {
  const authority = scope ? contexts.get(scope) : undefined
  // A standalone CLI can share provider accounts but has no managed authority.
  if (process.env.OPEN_CLANK_MANAGED !== "1" || !authority) return fetchFn
  const captured = async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    authority.started = true
    const url = input instanceof Request ? input.url : String(input)
    const wireFormat = /\/messages(?:\?|$)/.test(url)
      ? "anthropic-messages.v1"
      : /\/responses(?:\?|$)/.test(url)
        ? "responses.v1"
        : /\/chat\/completions(?:\?|$)/.test(url)
          ? "chat-completions.v1"
          : "unsupported"
    const attemptID = crypto.randomUUID()
    const origin = performance.now()
    const timing: Record<string, number | string | null> = {
      clock_domain: "engine",
      queued_ms: 0,
      admitted_ms: null,
      dispatch_ms: null,
      headers_ms: null,
      first_byte_ms: null,
      first_output_ms: null,
      last_output_ms: null,
      terminal_ms: null,
    }
    const admission = authority.admission
    const enabled = Boolean(admission?.admissionID && admission.context.persistence.numeric)
    const parserSupported = wireFormat !== "unsupported"
    const advanced =
      enabled &&
      admission?.context.transportMode === "advanced_proxy" &&
      admission.policy.advanced_enabled &&
      admission.context.persistence.content
    authority.dispatches.push(attemptID)
    const sdkStep = sdkDispatchScope.getStore()
    if (sdkStep && sdkStep.scope === scope) {
      sdkStep.ids.push(attemptID)
      if (sdkStep.sourceAttempt && sdkStep.sourceAbort)
        registerSourceDispatch(sdkStep.sourceAttempt,sdkStep.identity,sdkStep.sourceAbort,attemptID)
      else if (sdkStep.sourceAttempt) invalidateSourceAttempt(sdkStep.sourceAttempt)
    } else if (sdkStep?.sourceAttempt) invalidateSourceAttempt(sdkStep.sourceAttempt)
    // Choose one effective init before any request logging or wire observation.
    // This witness is the app-captured default fetch, not a native-intrinsic attestation.
    const rowView = sdkStep?.rowView
    const rowModel = rowView ? sameRequestRoute(rowView.model, fetchFn, url) : undefined
    if (rowView && rowModel && sdkStep?.scope === scope && sdkStep.ids.length === 1 && sdkStep.identity?.rootOperationID &&
        sdkStep.identity.innerTurnID && typeof input === 'string' && init?.method === 'POST' &&
        typeof init.body === 'string' && Buffer.byteLength(init.body, 'utf8') <= 51200 && !init.signal?.aborted &&
        sameRequestRoute(rowView.model, fetchFn, url) === rowModel) {
      try {
        const originalInit=init, originalBody=init.body, originalHeaders=init.headers, originalSignal=init.signal
        const keys=Object.keys(init).sort(), allowed=new Set(['method','body','headers','signal','timeout'])
        const expectedAgent = `ai-sdk/openai-compatible/2.0.41 ai-sdk/provider-utils/4.0.23 ${getRuntimeEnvironmentUserAgent()}`
        const eligible=()=>{
          if (input!==url || originalInit.method!=='POST' || originalInit.body!==originalBody ||
              originalInit.headers!==originalHeaders || originalInit.signal!==originalSignal || originalSignal?.aborted ||
              Reflect.ownKeys(originalInit).length!==keys.length || Object.keys(originalInit).sort().join(',')!==keys.join(',') || keys.some(key=>!allowed.has(key)) ||
              (originalInit as RequestInit & {timeout?:unknown}).timeout!==false ||
              (Object.getPrototypeOf(originalInit)!==Object.prototype && Object.getPrototypeOf(originalInit)!==null) ||
              keys.some(key=>{ const descriptor=Object.getOwnPropertyDescriptor(originalInit,key); return !descriptor || !descriptor.enumerable || !('value' in descriptor) })) return false
          const headers=new Headers(originalInit.headers), names=[...headers.keys()].sort()
          return names.join(',')==='content-type,user-agent' && headers.get('content-type')==='application/json' &&
            headers.get('user-agent')===expectedAgent
        }
        if (eligible() && rowView.current()) {
          // If the caller mutates its init during the asynchronous proof, the
          // raw fallback uses the call-time values and the original abort signal.
          const baselineInit = { ...originalInit, headers:new Headers(originalHeaders) }
          init = baselineInit
          const frames = await verifyGcfRequestFrames(rowView.handle, rowView.artifact)
          if (eligible()) init = originalInit
          if (frames && eligible() && rowView.current() && sdkDispatchScope.getStore() === sdkStep &&
              sdkStep.scope === scope && sdkStep.ids.length === 1 && sameRequestRoute(rowView.model, fetchFn, url) === rowModel) {
            const derived = deriveSameRequestRows(originalBody, frames, rowView.artifact, rowModel, { graphReferences: rowView.graphReferences === true, rows: rowView.rows !== false })
            if (derived && eligible() && rowView.current()) init = { ...baselineInit, body: derived.body }
          }
        }
      } catch { /* Optional transform fails closed to the original request. */ }
    }
    const dispatchIndex = authority.dispatchBase + authority.dispatches.length
    const retryOfDispatchID = authority.pendingRetry
    authority.pendingRetry = undefined
    const normalizationProfile =
      wireFormat === "anthropic-messages.v1"
        ? "anthropic-wire-separated-v1"
        : wireFormat === "responses.v1"
          ? "openai-wire-inclusive-v1"
          : wireFormat === "chat-completions.v1"
            ? "openai-wire-inclusive-v1"
            : null
    const http: LoggingHttp | undefined = advanced
      ? {
          method: init?.method ?? (input instanceof Request ? input.method : "GET"),
          endpoint: safeEndpoint(url),
          requestHeaders: sanitizedHeaders(init?.headers ?? (input instanceof Request ? input.headers : undefined)),
          coverage: {
            request_headers: "reported",
            request_body: admission!.policy.request_body_enabled ? "pending" : "disabled",
            response_body: admission!.policy.response_body_enabled ? "pending" : "disabled",
          },
        }
      : undefined
    let requestBody: unknown
    if (advanced && admission?.policy.request_body_enabled && typeof init?.body === "string") {
      if (Buffer.byteLength(init.body) <= BODY_LIMIT) {
        try {
          requestBody = JSON.parse(init.body)
        } catch {
          requestBody = { text: init.body }
        }
      } else
        requestBody = { omitted: "request exceeds sanitized parser limit", size_bytes: Buffer.byteLength(init.body) }
    }
    if (
      http?.coverage &&
      admission!.policy.request_body_enabled &&
      init?.body !== undefined &&
      typeof init.body !== "string"
    )
      http.coverage.request_body = "unsupported"
    const requestBodyRead =
      advanced && admission?.policy.request_body_enabled && init?.body === undefined && input instanceof Request
        ? issueCapture(authority, () => boundedRequestBody(input)).then((value) => {
            requestBody = value
            if (http?.coverage) http.coverage.request_body = "reported"
          })
        : undefined
    const events: unknown[] = []
    const metrics: Record<string, number> = {}
    const losses = new Set<NonNullable<LoggingEventRequest["lossReasons"]>[number]>()
    const decoder = new TextDecoder()
    let buffer = ""
    let jsonBuffer = ""
    let bodyBytes = 0
    let eventBytes = 0
    let finished = false
    let actualModel: string | undefined
    let responseText = ""
    let structuredResponse: unknown
    let observedOutcome: LoggingEventRequest["outcome"]
    let quota: OpenClankManagedProtocol.OperationExecuteResult["quota"] | undefined
    let skipFrame = false
    let status = 0
    let sequence = 0
    if (
      advanced &&
      admission?.policy.request_body_enabled &&
      typeof init?.body === "string" &&
      Buffer.byteLength(init.body) > BODY_LIMIT
    )
      losses.add("truncated")

    function parse(value: unknown) {
      const event = object(value)
      const response = object(event.response)
      const message = object(event.message)
      const usage = object(response.usage ?? event.usage ?? message.usage ?? event.usageMetadata)
      const inputDetail = object(usage.input_tokens_details ?? usage.prompt_tokens_details)
      const outputDetail = object(usage.output_tokens_details ?? usage.completion_tokens_details)
      const fields: Record<string, unknown> = {
        input_tokens: usage.input_tokens ?? usage.prompt_tokens ?? usage.inputTokens ?? usage.promptTokenCount,
        output_tokens:
          usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens ?? usage.candidatesTokenCount,
        cache_read_tokens:
          usage.cache_read_input_tokens ??
          inputDetail.cached_tokens ??
          usage.cacheReadTokens ??
          usage.cachedContentTokenCount,
        cache_write_tokens:
          usage.cache_creation_input_tokens ?? inputDetail.cache_write_tokens ?? usage.cacheWriteTokens,
        audioInputTokens: inputDetail.audio_tokens,
        audioOutputTokens: outputDetail.audio_tokens,
        imageInputTokens: inputDetail.image_tokens,
        imageOutputTokens: outputDetail.image_tokens,
        searchRequests: usage.web_search_requests ?? object(usage.server_tool_use).web_search_requests,
        reasoning_tokens: outputDetail.reasoning_tokens ?? usage.reasoningTokens ?? usage.thoughtsTokenCount,
      }
      let changed = false
      for (const [key, raw] of Object.entries(fields)) {
        const count = integer(raw)
        if (count !== undefined) {
          changed ||= metrics[key] !== count
          metrics[key] = count
        }
      }
      if (changed && enabled && admission)
        queue(authority!, {
          admissionID: admission.admissionID,
          dispatchID: attemptID,
          source: "wire",
          sequence: ++sequence,
          observationKind: "cumulative_snapshot",
          metrics: { ...metrics },
          metricCoverage: Object.fromEntries(
            Object.keys(metrics).map((key) => [key, { state: "reported", coverage: "partial", source: "wire" }]),
          ),
          normalizationProfile,
          identityCoverage: admission.context.identityCoverage,
          dispatchIndex,
          retryOfDispatchID,
        })
      const model = event.model ?? response.model ?? message.model
      if (typeof model === "string") actualModel = model
      const delta = object(event.delta)
      const firstChoice = Array.isArray(event.choices) ? object(event.choices[0]) : {}
      const choiceDelta = object(firstChoice.delta)
      const choiceMessage = object(firstChoice.message)
      if (
        event.type === "response.completed" ||
        event.type === "message_stop" ||
        (firstChoice.finish_reason && firstChoice.finish_reason !== "error")
      )
        observedOutcome = "completed"
      if (event.type === "response.failed" || event.type === "error" || firstChoice.finish_reason === "error")
        observedOutcome = "upstream_error"
      const text =
        typeof event.delta === "string" ? event.delta : (delta.text ?? choiceDelta.content ?? choiceMessage.content)
      const meaningful =
        (typeof text === "string" && text.length > 0) ||
        Boolean(choiceDelta.tool_calls) ||
        Boolean(delta.partial_json) ||
        Boolean(event.content_block)
      if (meaningful && timing.first_output_ms === null) timing.first_output_ms = performance.now() - origin
      if (advanced && admission?.policy.response_body_enabled) {
        const eventSize = Buffer.byteLength(JSON.stringify(value))
        if (events.length < EVENT_LIMIT && eventBytes + eventSize <= BODY_LIMIT) {
          events.push(value)
          eventBytes += eventSize
        } else losses.add("truncated")
        if (typeof text === "string") {
          const remaining = Math.max(0, BODY_LIMIT / 4 - responseText.length)
          responseText += text.slice(0, remaining)
          if (text.length > remaining) losses.add("truncated")
        }
      }
    }

    function chunk(bytes: Uint8Array, streaming: boolean) {
      if (!enabled) return
      const now = performance.now() - origin
      if (timing.first_byte_ms === null && bytes.length) timing.first_byte_ms = now
      if (bytes.length) timing.last_output_ms = now
      bodyBytes += bytes.length
      const text = decoder.decode(bytes, { stream: true })
      if (!streaming) {
        if (jsonBuffer.length + text.length <= BODY_LIMIT) jsonBuffer += text
        else losses.add("parse_degraded")
        return
      }
      buffer = (buffer + text).replace(/\r\n/g, "\n")
      let boundary = buffer.indexOf("\n\n")
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        if (!skipFrame && frame.length <= FRAME_LIMIT) {
          const data = frame
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n")
          if (data === "[DONE]") observedOutcome = "completed"
          if (data && data !== "[DONE]") {
            try {
              parse(JSON.parse(data))
            } catch {
              losses.add("parse_degraded")
            }
          }
        } else losses.add("parse_degraded")
        skipFrame = false
        boundary = buffer.indexOf("\n\n")
      }
      if (buffer.length > FRAME_LIMIT) {
        skipFrame = true
        buffer = buffer.slice(-2)
        losses.add("parse_degraded")
      }
    }

    function finish(outcome: NonNullable<LoggingEventRequest["outcome"]>, streaming: boolean) {
      if (finished) return
      finished = true
      timing.terminal_ms = performance.now() - origin
      if (!enabled || !admission) return
      if (!streaming && jsonBuffer && !losses.has("parse_degraded")) {
        try {
          structuredResponse = JSON.parse(jsonBuffer + decoder.decode())
          parse(structuredResponse)
        } catch {
          losses.add("parse_degraded")
        }
      }
      if (!parserSupported) losses.add("parse_degraded")
      if (streaming && buffer.trim()) losses.add("parse_degraded")
      if (observedOutcome) outcome = observedOutcome
      else if (streaming && outcome === "completed") {
        outcome = "unknown"
        losses.add("parse_degraded")
      }
      for (const reason of authority!.losses) losses.add(reason)
      const payload: LoggingEventRequest = {
        admissionID: admission.admissionID,
        dispatchID: attemptID,
        source: "wire",
        sequence: ++sequence,
        terminal: true,
        observationKind: "final_snapshot",
        dispatchIndex,
        retryOfDispatchID,
        identityCoverage: admission.context.identityCoverage,
        metricCoverage: Object.fromEntries(
          Object.keys(metrics).map((key) => [
            key,
            {
              state: "reported",
              coverage: losses.has("parse_degraded") || outcome !== "completed" ? "partial" : "complete",
              source: "wire",
            },
          ]),
        ),
        outcome,
        metrics,
        actualModel,
        ...(quota ? { quota } : {}),
        normalizationProfile,
        timing,
        ...(http ? { http } : {}),
        ...(requestBody !== undefined ? { requestBody } : {}),
        ...(advanced && admission.policy.response_body_enabled
          ? {
              responseBody:
                structuredResponse !== undefined
                  ? { structured: structuredResponse, received_bytes: bodyBytes }
                  : { text: responseText, received_bytes: bodyBytes },
              events,
            }
          : {}),
        lossReasons: [...losses],
        billable: null,
      }
      const binary = Boolean(admission.policy.binary_body_enabled)
      if (payload.requestBody !== undefined) payload.requestBody = captureBody(payload.requestBody, binary)
      if (payload.responseBody !== undefined) payload.responseBody = captureBody(payload.responseBody, binary)
      if (payload.events) payload.events = payload.events.map((event) => captureBody(event, binary))
      if (Buffer.byteLength(JSON.stringify(payload)) > 900 * 1024) {
        delete payload.events
        delete payload.requestBody
        delete payload.responseBody
        losses.add("truncated")
        payload.lossReasons = [...new Set([...losses, "truncated" as const])]
      }
      if (http?.coverage) {
        if (requestBody !== undefined)
          http.coverage.request_body = object(requestBody).omitted ? String(object(requestBody).omitted) : "reported"
        if (admission.policy.response_body_enabled)
          http.coverage.response_body = losses.has("truncated")
            ? "truncated"
            : parserSupported
              ? "reported"
              : "unsupported"
      }
      for (const reason of losses) authority!.losses.add(reason)
      // Request clone observation never blocks upstream dispatch or response.
      if (requestBodyRead) {
        const deferred = issueCapture(authority!, () => requestBodyRead.then(() => {
          if (requestBody !== undefined) payload.requestBody = captureBody(requestBody, binary)
          if (http?.coverage)
            http.coverage.request_body = object(requestBody).omitted ? String(object(requestBody).omitted) : "reported"
          return queue(authority!, payload)
        }))
        writes.add(deferred)
        void deferred.finally(() => writes.delete(deferred)).catch(() => {})
      } else queue(authority!, payload)
    }

    timing.dispatch_ms = performance.now() - origin
    if (enabled && admission)
      queue(authority, {
        admissionID: admission.admissionID,
        dispatchID: attemptID,
        source: "wire",
        sequence: 0,
        observationKind: "cumulative_snapshot",
        dispatchIndex,
        retryOfDispatchID,
        identityCoverage: admission.context.identityCoverage,
        timing,
        ...(http ? { http } : {}),
      })
    let res: Response
    const inner: { final?: CodexInnerObservation } = {}
    try {
      res = await codexInnerScope.run(inner, () => observeAnthropicFailure(fetchFn, input, init))
      const codex = inner.final?.response === res ? inner.final : undefined
      status = res.status
      // Prove the already-forwarded one-user position against this exact encoded body.
      if (res.ok && !res.redirected && wireFormat === "chat-completions.v1" && sdkStep && sdkStep.scope === scope && sdkStep.candidate && sdkStep.deliver && sdkStep.identity?.actorID === "main" && sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && typeof init?.body === "string" && Buffer.byteLength(init.body, "utf8") <= 1_000_000) {
        try {
          const body = JSON.parse(init.body)
          const proven = nativeSingleUserWire({version:1, sources:[sdkStep.candidate], coverage:"partial", omitted:0}, body.messages)
          if (proven) {
            const identity: NativeDeliveryIdentity = {sessionID:sdkStep.identity.sessionID, rootOperationID:sdkStep.identity.rootOperationID, actorID:"main", executionRevision:sdkStep.identity.executionRevision, innerTurnID:sdkStep.identity.innerTurnID}
            const receipt: NativeTextDelivery = {version:1, identity, profile:"openai-chat-single-user-text-v1", dispatchID:attemptID,
              bodyDigest:createHash("sha256").update(init.body, "utf8").digest("hex"), source:proven, state:"acknowledged", coverage:"partial"}
            const delivery = issueCapture(authority, () => sdkStep.deliver!(receipt))
            writes.add(delivery); authority.writes.add(delivery)
            void delivery.finally(() => { writes.delete(delivery); authority.writes.delete(delivery) }).catch(() => { authority.losses.add("write_failed") })
          }
        } catch { authority.losses.add("parse_degraded") }
      }
      if (res.ok && !res.redirected && (wireFormat === "chat-completions.v1" || wireFormat === "responses.v1") && sdkStep && sdkStep.scope === scope &&
          sdkStep.maintenance && sdkStep.deliverMaintenance && sdkStep.identity?.actorID === "main" &&
          sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && typeof init?.body === "string" &&
          Buffer.byteLength(init.body,"utf8") <= 1_000_000) {
        try {
          const maintenance=sdkStep.maintenance
          const deliverMaintenance=sdkStep.deliverMaintenance
          const profile=maintenance.wireProfile
          const observedBody = profile === "codex-responses" ? codex?.body : init.body
          const observedID = profile === "codex-responses" ? codex?.dispatchID : attemptID
          const body = typeof observedBody === "string" ? JSON.parse(observedBody) : undefined
          const proven = profile === "openai-chat" && wireFormat === "chat-completions.v1" ? maintenanceChatWire(maintenance,body)
            : profile === "codex-responses" && wireFormat === "responses.v1" && codex ? maintenanceResponsesWire(maintenance,body) : false
          if (proven && profile && observedID && typeof observedBody === "string") {
            const delivery = issueCapture(authority, () => deliverMaintenance({dispatchID:observedID,bodySHA256:createHash("sha256").update(observedBody,"utf8").digest("hex"),wireProfile:profile,
              ...(maintenance.instructionsSHA256 ? {instructionsSHA256:maintenance.instructionsSHA256} : {}),
              ...(profile === "codex-responses" && maintenance.trustedInstructionsSHA256 &&
                  maintenance.instructionsSHA256 === maintenance.trustedInstructionsSHA256
                  ? {instructionsClass:"builtin-compaction-v1" as const} : {})}))
            writes.add(delivery); authority.writes.add(delivery)
            void delivery.finally(() => { writes.delete(delivery); authority.writes.delete(delivery) }).catch(() => {})
          }
        } catch {}
      }
      if (res.ok && !res.redirected && wireFormat === "chat-completions.v1" && sdkStep && sdkStep.scope === scope && sdkStep.recall && sdkStep.deliverRecall && sdkStep.identity?.actorID === "main" && sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && typeof init?.body === "string" && Buffer.byteLength(init.body, "utf8") <= 1_000_000) {
        try {
          const requestBody=init.body
          const deliverRecall=sdkStep.deliverRecall
          const body = JSON.parse(requestBody)
          const proven = recallToolWire(sdkStep.recall, body.messages)
          if (proven) {
            const delivery = issueCapture(authority, () => deliverRecall({toolCallID:proven.toolCallID, attestationID:proven.attestationID,
              resultSHA256:proven.resultSHA256, dispatchID:attemptID,
              bodySHA256:createHash("sha256").update(requestBody, "utf8").digest("hex")}))
            writes.add(delivery); authority.writes.add(delivery)
            void delivery.finally(() => { writes.delete(delivery); authority.writes.delete(delivery) }).catch(() => {})
          }
        } catch {}
      }
      if (res.ok && !res.redirected && (wireFormat === "chat-completions.v1" || wireFormat === "responses.v1") && sdkStep && sdkStep.scope === scope && sdkStep.context && sdkStep.deliverContext && sdkStep.identity?.actorID === "main" && sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && typeof init?.body === "string" && Buffer.byteLength(init.body, "utf8") <= 1_000_000) {
        try {
          const observedBody = sdkStep.context.wireProfile === "codex-responses" ? codex?.body : init.body
          const observedID = sdkStep.context.wireProfile === "codex-responses" ? codex?.dispatchID : attemptID
          const body = typeof observedBody === "string" ? JSON.parse(observedBody) : undefined
          const proven = sdkStep.context.wireProfile === "codex-responses" && wireFormat === "responses.v1" && codex
            ? contextResponsesWire(sdkStep.context,body)
            : (sdkStep.context.wireProfile === "openai-chat" || (sdkStep.context.wireProfile === "compatible-chat" &&
                typeof sdkStep.context.expectedURL === "string" && url === sdkStep.context.expectedURL && res.url === url &&
                (init?.method ?? (input instanceof Request ? input.method : "GET")) === "POST")) && wireFormat === "chat-completions.v1"
              ? contextPrefaceFrame(sdkStep.context,body?.messages) : undefined
          if (proven && observedID && typeof observedBody === "string") {
            const delivery = issueCapture(authority, () => sdkStep.deliverContext!({contextPartID:proven.contextPartID,attestationID:proven.attestationID,
              textSHA256:proven.textSHA256,dispatchID:observedID,bodySHA256:createHash('sha256').update(observedBody,'utf8').digest('hex')}))
            writes.add(delivery); authority.writes.add(delivery)
            void delivery.finally(() => { writes.delete(delivery); authority.writes.delete(delivery) }).catch(() => {})
          }
        } catch {}
      }
      if (res.ok && !res.redirected && sdkStep && sdkStep.scope === scope && sdkStep.standing && sdkStep.deliverStanding && sdkStep.identity?.actorID === 'main' && sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && typeof init?.body === 'string' && Buffer.byteLength(init.body,'utf8') <= 1_000_000) {
        try {
          const standing=sdkStep.standing, deliver=sdkStep.deliverStanding
          const officialChat = url === 'https://api.openai.com/v1/chat/completions' && res.url === url && (init?.method ?? (input instanceof Request ? input.method : 'GET')) === 'POST'
          const observedBody=standing.wireProfile==='codex-responses' && wireFormat==='responses.v1' ? codex?.body : standing.wireProfile==='openai-chat' && wireFormat==='chat-completions.v1' && officialChat ? init.body : undefined
          const observedID=standing.wireProfile==='codex-responses' ? codex?.dispatchID : attemptID
          const proven=typeof observedBody==='string' && Buffer.byteLength(observedBody,'utf8')<=1_000_000 ? standingGuidanceWire(standing,JSON.parse(observedBody)) : undefined
          if (proven?.systemSHA256 && observedID && typeof observedBody==='string') {
            const receipt={attestationID:proven.attestationID,textSHA256:proven.textSHA256,dispatchID:observedID,bodySHA256:createHash('sha256').update(observedBody,'utf8').digest('hex'),systemSHA256:proven.systemSHA256,wireProfile:proven.wireProfile}
            const delivery=issueCapture(authority,()=>deliver(receipt));writes.add(delivery);authority.writes.add(delivery)
            void delivery.finally(()=>{writes.delete(delivery);authority.writes.delete(delivery)}).catch(()=>{})
          }
        } catch {}
      }
      // Exact compatible-chat source layout; no content is persisted by this observation.
      if (sdkStep?.sourceAttempt && sdkStep.sourceFrame && 'version' in sdkStep.sourceFrame && sdkStep.sourceFrame.version===2 &&
          res.ok && !res.redirected && !(init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted &&
          sdkStep.scope===scope && sdkStep.ids.length===1 && wireFormat==='chat-completions.v1' &&
          pinnedCompatibleChatEndpoint(sdkStep.sourceFrame.model)===sdkStep.sourceFrame.endpoint && url===sdkStep.sourceFrame.endpoint && res.url===url &&
          (init?.method ?? (input instanceof Request ? input.method : 'GET'))==='POST' && typeof init?.body==='string' && Buffer.byteLength(init.body,'utf8')<=1_000_000) {
        try {
          const final=sdkStep.sourceFrame, sourceAttempt=sdkStep.sourceAttempt, sourceIdentity=sdkStep.identity
          const body=JSON.parse(init.body), prefix=final.current(), currentLayout=sourceAttemptLayout(sourceAttempt), root=sourceAttemptRoot(sourceAttempt)
          const allowed=new Set(['model','messages','stream','stream_options','max_tokens','max_completion_tokens','temperature','top_p','seed','store','tools','tool_choice','prompt_cache_key'])
          const opts=body?.stream_options, systems=Array.isArray(body?.messages) ? body.messages.filter((item:any)=>item?.role==='system') : []
          const full=[...final.layout.layout,...final.layout.generated.map(g=>({position:g.position,sha256:g.sha256,synthetic:true}))]
          if(body && typeof body==='object' && !Array.isArray(body) && Object.keys(body).every(key=>allowed.has(key)) &&
              (body.tool_choice===undefined || body.tool_choice==='auto') && (body.prompt_cache_key===undefined || body.prompt_cache_key===sourceIdentity?.sessionID) &&
              (opts===undefined || (opts && typeof opts==='object' && !Array.isArray(opts) && Object.keys(opts).every(key=>key==='include_usage') && typeof opts.include_usage==='boolean')) &&
              prefix && prefix.unknown.length===0 && body.model===prefix.environment.scope.modelID && currentLayout && root &&
              JSON.stringify(currentLayout)===JSON.stringify(final.layout) && sourceLayoutMessages(full,body.messages,true) &&
              systems.length===final.system.length && systems.every((item:any,index:number)=>typeof item.content==='string' && item.content===final.system[index])) {
            const digest=createHash('sha256').update(init.body,'utf8').digest('hex')
            if(body.tools===undefined) acknowledgeSourceWire(sourceAttempt,sourceIdentity,attemptID,digest,root,final.current,final.layout)
            else {
              let expired=false
              const current=()=>!expired && sdkStep.sourceAttempt===sourceAttempt && sdkStep.identity===sourceIdentity && sdkStep.sourceFrame===final &&
                sdkStep.scope===scope && sdkStep.ids.length===1 && sourceAttemptState(sourceAttempt)==='pending' && !!final.current() &&
                JSON.stringify(sourceAttemptLayout(sourceAttempt))===JSON.stringify(final.layout)
              const work=(async()=>{let timer:ReturnType<typeof setTimeout>|undefined
                try {const qualified=await Promise.race([observeFinalToolWire(sourceAttempt,attemptID,digest,body,current),new Promise<false>(resolve=>{timer=setTimeout(()=>{expired=true;resolve(false)},200)})])
                  if(qualified===true && current())acknowledgeSourceWire(sourceAttempt,sourceIdentity,attemptID,digest,root,final.current,final.layout)
                  else invalidateSourceAttempt(sourceAttempt)
                } finally {expired=true;if(timer)clearTimeout(timer)}
              })()
              registerSourceWireSettlement(sourceAttempt,work)
            }
          }
        } catch { invalidateSourceAttempt(sdkStep.sourceAttempt) }
      }
      // A separate private observation; D01 persisted partial membership is not closure.
      if (sdkStep?.sourceAttempt && res.ok && !res.redirected && !(init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted &&
          sdkStep.scope===scope && sdkStep.ids.length===1 && sdkStep.sourceFrame && 'frame' in sdkStep.sourceFrame && sdkStep.membership===sdkStep.sourceFrame.frame &&
          isPinnedChatModel(sdkStep.sourceFrame.frame.model) && url==='https://api.openai.com/v1/chat/completions' && res.url===url &&
          (init?.method ?? (input instanceof Request ? input.method : 'GET'))==='POST' && typeof init?.body==='string' && Buffer.byteLength(init.body,'utf8')<=1_000_000) {
        try {
          const body=JSON.parse(init.body), final=sdkStep.sourceFrame, prefix=final.current()
          const sourceAttempt=sdkStep.sourceAttempt, sourceIdentity=sdkStep.identity, sourceFrame=sdkStep.sourceFrame
          const allowed=new Set(['model','messages','stream','stream_options','max_tokens','max_completion_tokens','temperature','top_p','seed','store','tools','tool_choice','prompt_cache_key'])
          const streamOptions=body?.stream_options
          const systems=Array.isArray(body?.messages) ? body.messages.filter((item:any)=>item?.role==='system') : []
          const sources=final.frame.sources
          if (body && typeof body==='object' && !Array.isArray(body) && Object.keys(body).every(key=>allowed.has(key)) &&
              (body.tool_choice===undefined || body.tool_choice==='auto') &&
              (body.prompt_cache_key===undefined || body.prompt_cache_key===sourceIdentity?.sessionID) &&
              (streamOptions===undefined || (streamOptions && typeof streamOptions==='object' && !Array.isArray(streamOptions) && Object.keys(streamOptions).every(key=>key==='include_usage') && typeof streamOptions.include_usage==='boolean')) &&
              prefix && prefix.unknown.length===0 && body.model===prefix.environment.scope.modelID && nativeMembershipWire(final.frame,body) &&
              systems.length===final.system.length && systems.every((item:any,index:number)=>typeof item.content==='string' && item.content===final.system[index]) &&
              final.frame.roles.filter(role=>role!=='system').length===1 && sources.length===1 && sources[0]?.role==='user') {
            const digest=createHash('sha256').update(init.body,'utf8').digest('hex')
            const root={messageID:sources[0].messageID,partID:sources[0].partID,digest:sources[0].digest}
            if (body.tools===undefined) acknowledgeSourceWire(sourceAttempt,sourceIdentity,attemptID,digest,root,final.current)
            else {
              let expired=false
              const current=()=>!expired && sdkStep.sourceAttempt===sourceAttempt && sdkStep.identity===sourceIdentity && sdkStep.sourceFrame===sourceFrame &&
                sdkStep.scope===scope && sdkStep.ids.length===1 && sourceAttemptState(sourceAttempt)==='pending' &&
                !!sourceFrame.current()
              const work=(async()=>{
                let timer:ReturnType<typeof setTimeout> | undefined
                try {
                  const qualified=await Promise.race([observeFinalToolWire(sourceAttempt,attemptID,digest,body,current),
                    new Promise<false>(resolve=>{timer=setTimeout(()=>{expired=true;resolve(false)},200)})])
                  if (qualified===true && current()) acknowledgeSourceWire(sourceAttempt,sourceIdentity,attemptID,digest,root,final.current)
                  else invalidateSourceAttempt(sourceAttempt)
                } finally { expired=true; if (timer) clearTimeout(timer) }
              })()
              registerSourceWireSettlement(sourceAttempt,work)
            }
          }
        } catch { invalidateSourceAttempt(sdkStep.sourceAttempt) }
      }
      if (res.ok && !res.redirected && !(init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted && sdkStep && sdkStep.scope===scope && sdkStep.ids.length===1 && sdkStep.membership && sameNativeMembershipIdentity(sdkStep.membership.identity,sdkStep.identity) && isPinnedChatModel(sdkStep.membership.model) && sdkStep.deliverMembership && sdkStep.identity?.actorID==='main' && sdkStep.identity.rootOperationID && sdkStep.identity.innerTurnID && url==='https://api.openai.com/v1/chat/completions' && res.url===url && (init?.method ?? (input instanceof Request ? input.method : 'GET'))==='POST' && typeof init?.body==='string' && Buffer.byteLength(init.body,'utf8')<=1_000_000) {
        try {
          const body=init.body, deliver=sdkStep.deliverMembership, identity=sdkStep.identity
          const proven=nativeMembershipWire(sdkStep.membership,JSON.parse(body))
          if (proven) {
            const receipt:NativeMembershipDelivery={version:1,identity:{sessionID:identity.sessionID,rootOperationID:identity.rootOperationID!,actorID:'main',executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID!},profile:'openai-chat-native-text-v1',dispatchID:attemptID,bodyDigest:createHash('sha256').update(body,'utf8').digest('hex'),state:'acknowledged',coverage:'partial',sources:proven.sources}
            const delivery=issueCapture(authority,()=>deliver(receipt));writes.add(delivery);authority.writes.add(delivery)
            void delivery.finally(()=>{writes.delete(delivery);authority.writes.delete(delivery)}).catch(()=>{})
          }
        } catch {}
      }
      if (status === 429 && codex && wireFormat === "responses.v1" && authority.binding.providerID === "openai" && authority.binding.billingLane === "subscription") {
        const evidence = codexWindowEvidence(res.headers, codex.dispatchID, Date.now())
        if (evidence) {
          authority.codexWindows.set(attemptID,evidence)
          while (authority.codexWindows.size > 32) authority.codexWindows.delete(authority.codexWindows.keys().next().value!)
        }
      }
      let officialAnthropic = false
      try {
        const endpoint = new URL(url)
        const responseEndpoint = new URL(res.url)
        const official = (value: URL) => value.protocol === "https:" && value.hostname === "api.anthropic.com" && !value.port && !value.username && !value.password && value.pathname === "/v1/messages"
        officialAnthropic = !res.redirected && official(endpoint) && official(responseEndpoint)
      } catch {}
      if (((status >= 400 && status <= 599) || (status >= 200 && status <= 299)) && !res.redirected) {
        let observation: AccountSelection.DispatchObservation | undefined
        if (officialAnthropic && (init?.method ?? (input instanceof Request ? input.method : 'GET')) === 'POST' && wireFormat === 'anthropic-messages.v1' && authority.binding.providerID === 'anthropic' && authority.binding.billingLane === 'metered_api')
          observation={version:1,profile:'anthropic.messages.v1',captureDispatchID:attemptID,upstreamDispatchID:attemptID,httpStatus:status}
        else if (codex && wireFormat === 'responses.v1' && authority.binding.providerID === 'openai' && authority.binding.billingLane === 'subscription' && codex.dispatchID!==attemptID)
          observation={version:1,profile:'openai.codex.responses.v1',captureDispatchID:attemptID,upstreamDispatchID:codex.dispatchID,httpStatus:status}
        if (observation) {
          authority.dispatchObservations.set(attemptID,observation)
          while (authority.dispatchObservations.size>32) authority.dispatchObservations.delete(authority.dispatchObservations.keys().next().value!)
        }
      }
      if (status === 429 && officialAnthropic && wireFormat === "anthropic-messages.v1" && authority.binding.providerID === "anthropic" && authority.binding.billingLane === "metered_api") {
        const reset = anthropicRequestRateReset(res.headers, Date.now())
        if (reset) {
          authority.requestRateResets.set(attemptID, { ...reset, dispatchID: attemptID })
          while (authority.requestRateResets.size > 32) authority.requestRateResets.delete(authority.requestRateResets.keys().next().value!)
        }
      }
      timing.headers_ms = performance.now() - origin
      if (http) {
        http.status = res.status
        http.responseHeaders = sanitizedHeaders(res.headers)
        http.coverage!.response_headers = "reported"
      }
      quota = passiveQuota({ response: { headers: res.headers } }, String(admission?.context.providerID ?? ""))
    } catch (error) {
      finish(
        (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
          ? "cancelled"
          : "upstream_error",
        true,
      )
      throw error
    }
    if (!enabled) return res
    const streaming = res.headers.get("content-type")?.includes("text/event-stream") ?? false
    if (!res.body) {
      finish(status >= 400 ? "upstream_error" : "completed", streaming)
      return res
    }
    const reader = res.body.getReader()
    const wrapped = new Response(
      new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            const next = await reader.read()
            if (next.done) {
              finish(status >= 400 ? "upstream_error" : "completed", streaming)
              controller.close()
              return
            }
            // Parser failures never alter the response byte ordering/status.
            try {
              chunk(next.value, streaming)
            } catch {
              losses.add("parse_degraded")
            }
            controller.enqueue(next.value)
          } catch (error) {
            finish(
              (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
                ? "cancelled"
                : "upstream_error",
              streaming,
            )
            controller.error(error)
          }
        },
        async cancel(reason) {
          finish(
            (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
              ? "cancelled"
              : "disconnected",
            streaming,
          )
          await reader.cancel(reason)
        },
      }),
      { status: res.status, statusText: res.statusText, headers: res.headers },
    )
    Object.defineProperty(wrapped, "url", { value: res.url })
    Object.defineProperty(wrapped, "redirected", { value: res.redirected })
    return wrapped
  }
  return Object.assign(captured, { preconnect: fetchFn.preconnect })
}
