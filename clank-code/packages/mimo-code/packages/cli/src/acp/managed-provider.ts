import { clearGcfSessionViews } from '../session/gcf-session-view'
import { toolTimingIssuerFor, isAdmittedToolTimingTurn, admittedToolTimingGeneration } from "../session/tool-timing"
import type { ActorExecutionIdentity } from "../actor/turn"
import { classifyFailure } from "@/provider/failure-classification"
import { RETRY_MAX_DELAY_NO_HEADERS } from "@/session/retry"
import type { AgentSideConnection } from "@agentclientprotocol/sdk"
import { Effect, Schema } from "effect"
import path from "node:path"
import { createHash, randomUUID } from "node:crypto"
import { Auth } from "@/auth"
import { installCaptureScope, attachCaptureAttempt, retireCaptureAttempt, flushCaptures, flushCaptureScope, markCaptureRetry, captureSdkEvidence, captureScopeEvidence, scopeBinding, replaceCapturedScope, takeSdkFailureReset, takeSdkCodexWindows, takeSdkFailureDispatch, refreshCaptureIdentity } from "@/provider/logging-transport"
import { isPinnedAnthropicModel } from "@/provider/anthropic-origin"
import type { AnthropicFailureProbe } from "@/provider/fallback-terminal-witness"
import { AccountSelection } from "@/provider/account-selection"
import { OpenClankManagedProtocol } from "./openclank-protocol"
import {
  installManagedSessionBinding,
  invalidateManagedSessionMarkerForTest,
  managedSessionBinding,
  markerMatches,
  registerManagedSessionAdmissionReset,
  registrationGeneration,
  resetManagedSessionScopesForTest,
  replaceManagedSessionBinding,
} from "@/memory/session-scope"

export const MANAGED_ENV = "OPEN_CLANK_MANAGED" as const

export function enabled(): boolean {
  return process.env[MANAGED_ENV] === "1"
}

export const RouteContext = Schema.Struct({
  modelRequestOptions: Schema.optional(AccountSelection.ActorOptions),
  methodPolicy: Schema.optional(Schema.Struct({
    version: Schema.Literal(1),
    methodID: Schema.Literal("anthropic.claude_subscription"),
    credentialOwner: Schema.String,
    policyRevision: Schema.Number,
  })),
  rootOperationID: Schema.String,
  operationID: Schema.optional(Schema.String),
  connectionID: Schema.String,
  providerID: Schema.String,
  billingLane: Auth.BillingLane,
  modelID: Schema.String,
  modelRouteID: Schema.optional(Schema.String),
  preferredAccountID: Schema.optional(Schema.String),
  inheritedAccountID: Schema.optional(Schema.String),
  grantID: Schema.optional(Schema.String),
  grantRevision: Schema.optional(Schema.Number),
})
export type RouteContext = Schema.Schema.Type<typeof RouteContext>

/**
 * Process-local host identity for the current authenticated ACP operation.
 * This never crosses the public session.prompt request body. The ACP agent
 * installs it only after ManagedProvider has established the real lease, and
 * restores the previous value when the SDK call completes.
 */
export type ManagedHostContext = {
  accountID?: string
  /** Original host user identity; never the provider billing account. */
  ownerAccountID?: string
  grantID?: string
  grantRevision?: number
  credentialRevision?: number
  chatID?: string
  workspaceID?: string
  cwd?: string
  goalID?: string
  /** Host-admitted, immutable preference identity; never model input. */
  gcfAdmission?: string
  gcfArgumentAdmission?: string
  /** Per-Agent revocable capability; process-local and never serialized. */
  gcfArtifact?: import("../session/gcf-native").GcfArtifact
  jsonWhitespaceAdmission?: string
  toolViewAdmission?: string
  adaptiveStyleAdmission?: string
  shellRewriteAdmission?: string
}

export function actorOwnerFromPrompt(value: unknown, sessionID: string, rootOperationID: unknown, hostChatID: unknown): string | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return
  const v=value as Record<string,unknown>
  if (Object.keys(v).sort().join(",")!=="hostChatID,ownerAccountID,rootOperationID,sessionID,version" || v.version!==1 ||
      v.sessionID!==sessionID || typeof rootOperationID!=="string" || !rootOperationID || v.rootOperationID!==rootOperationID ||
      typeof hostChatID!=="string" || !hostChatID || v.hostChatID!==hostChatID ||
      typeof v.ownerAccountID!=="string" || !v.ownerAccountID || Buffer.byteLength(v.ownerAccountID,"utf8")>256) return
  return v.ownerAccountID
}

type OriginalDelivery = {rootOperationID:string; host:AgentSideConnection; current:()=>boolean}
const hostContexts = new Map<string, { context: ManagedHostContext; depth: number; original?:OriginalDelivery }>()

function sameHostContext(left: ManagedHostContext, right: ManagedHostContext): boolean {
  const keys = new Set([...Object.keys(left), ...Object.keys(right)])
  for (const key of keys) {
    if (left[key as keyof ManagedHostContext] !== right[key as keyof ManagedHostContext]) return false
  }
  return true
}

export function currentHostContext(sessionID: string): ManagedHostContext | undefined {
  const entry = hostContexts.get(sessionID)
  if (!entry) return
  try {
    if (entry.original && (!entry.original.rootOperationID || Buffer.byteLength(entry.original.rootOperationID,"utf8")>256 || connection() !== entry.original.host || !toolTimingIssuerFor(entry.original.host)?.isAlive() || !entry.original.current())) return
    return { ...entry.context }
  } catch { return }
}

function enterHostContext(sessionID: string, context: ManagedHostContext, original?:OriginalDelivery) {
  const active = hostContexts.get(sessionID)
  if (active) {
    if (original || active.original || !sameHostContext(active.context, context)) throw new ManagedProviderError("A different managed host context is already active for this session")
    active.depth += 1
  } else {
    hostContexts.set(sessionID, { context: { ...context }, depth: 1, ...(original ? {original} : {}) })
  }
  let released = false
  return () => {
    if (released) return
    released = true
    const current = hostContexts.get(sessionID)
    if (current) {
      current.depth -= 1
      if (current.depth <= 0) hostContexts.delete(sessionID)
    }
  }
}

export async function withHostContext<A>(
  sessionID: string,
  context: ManagedHostContext,
  run: () => Promise<A>,
): Promise<A> {
  const leave = enterHostContext(sessionID, context)
  try {
    return await run()
  } finally {
    leave()
  }
}

/** The actual ACP delivery owns this context; this is not a provider lease or grant. */
export async function withOriginalPromptHostContext<A>(sessionID:string,rootOperationID:string,hostConnection:AgentSideConnection,
    deliveryCurrent:()=>boolean, makeContext:()=>ManagedHostContext,run:()=>Promise<A>):Promise<A> {
  // Missing optional evidence must not block the ordinary prompt. The bound
  // entry still fences it from unrelated session contexts until delivery ends.
  let context:ManagedHostContext = {}
  try { if (rootOperationID && Buffer.byteLength(rootOperationID,"utf8")<=256 && connection() === hostConnection &&
      toolTimingIssuerFor(hostConnection)?.isAlive() && deliveryCurrent()) context=makeContext() } catch { /* source evidence unavailable */ }
  const leave=enterHostContext(sessionID,context,{rootOperationID,host:hostConnection,current:deliveryCurrent})
  try { return await run() } finally { leave() }
}
/** null: no delivery-bound context; undefined: present but no longer the exact original. */
export function currentOriginalPromptHostContext(sessionID:string,rootOperationID:string):ManagedHostContext | null | undefined {
  const entry=hostContexts.get(sessionID)
  if (!entry?.original) return null
  try {
    if (!rootOperationID || Buffer.byteLength(rootOperationID,"utf8")>256 || entry.original.rootOperationID !== rootOperationID || connection() !== entry.original.host ||
        !toolTimingIssuerFor(entry.original.host)?.isAlive() || !entry.original.current()) return
    return {...entry.context}
  } catch { return }
}

/** Opaque currentness for the exact ACP-owned original delivery entry. */
export function captureOriginalPromptDelivery(sessionID:string, rootOperationID:string): (()=>boolean) | undefined {
  const entry=hostContexts.get(sessionID), original=entry?.original
  if (!entry || !original || original.rootOperationID!==rootOperationID) return
  let issuer:ReturnType<typeof toolTimingIssuerFor>
  try { issuer=toolTimingIssuerFor(original.host) } catch { return }
  if (!issuer) return
  const current=()=>{
    try {return hostContexts.get(sessionID)===entry && entry.original===original && original.rootOperationID===rootOperationID &&
      connection()===original.host && toolTimingIssuerFor(original.host)===issuer && issuer.isAlive() && original.current()}
    catch {return false}
  }
  return current() ? current : undefined
}

const CredentialLease = Schema.Struct({
  crossModelAttempt: Schema.optional(Schema.Struct({version:Schema.Literal(1),attemptID:Schema.String,attemptGeneration:Schema.Literal(1),coordinatorRevision:Schema.Number})),
  methodPolicy: Schema.optional(AccountSelection.MethodPolicy),
  leaseID: Schema.String,
  connectionID: Schema.String,
  accountID: Schema.String,
  credentialRevision: Schema.Number,
  expiresAt: Schema.Number,
  credential: Auth.Info,
})

const RefreshLease = Schema.Struct({
  methodPolicy: Schema.optional(AccountSelection.MethodPolicy),
  leaseID: Schema.String,
  connectionID: Schema.String,
  accountID: Schema.String,
  credentialRevision: Schema.Number,
  expiresAt: Schema.Number,
  renewable: Schema.Boolean,
})

let host: AgentSideConnection | undefined

export interface ManagedOperationDelegate {
  readonly execute: (request: OpenClankManagedProtocol.OperationExecuteRequest) => Promise<OpenClankManagedProtocol.OperationExecuteResult>
  readonly cancel: (request: OpenClankManagedProtocol.OperationCancelRequest) => Promise<OpenClankManagedProtocol.OperationCancelResult>
}

let operationDelegate: ManagedOperationDelegate | undefined

/** Install the already-admitted Router; fail closed until the Agent owns one. */
export function installManagedOperationDelegate(delegate: ManagedOperationDelegate): () => void {
  if (operationDelegate) throw new ManagedProviderError("managed operation delegate is already installed")
  operationDelegate = delegate
  return () => {
    if (operationDelegate === delegate) operationDelegate = undefined
  }
}

export function uninstallManagedOperationDelegate(): void {
  operationDelegate = undefined
}

export function managedOperationDelegate(): ManagedOperationDelegate {
  if (!operationDelegate) throw new ManagedProviderError("managed operation delegate is unavailable")
  return operationDelegate
}

export function currentRootOperationID(sessionID: string, actorID?: string, executionRevision?: number): string {
  const active = activeOperation(sessionID, actorID, executionRevision)
  const route = active?.routeValue
  if (!route || typeof route !== "object" || typeof (route as Record<string, unknown>).rootOperationID !== "string" || !(route as Record<string, unknown>).rootOperationID) {
    throw new ManagedProviderError("managed root operation is unavailable")
  }
  return (route as Record<string, unknown>).rootOperationID as string
}

export function managedHostCall(method: "_openclank/session/v1/context/augment", params: Extract<OpenClankManagedProtocol.ManagedContextAugmentRequest, {query:string}>): Promise<Exclude<OpenClankManagedProtocol.ManagedContextAugmentResult, {kind:string}>>
export function managedHostCall(method: "_openclank/session/v1/context/augment", params: Extract<OpenClankManagedProtocol.ManagedContextAugmentRequest, {kind:"standing_guidance"}>): Promise<Extract<OpenClankManagedProtocol.ManagedContextAugmentResult, {kind:"standing_guidance"}>>
export function managedHostCall(method: "_openclank/session/v1/context/augment", params: Extract<OpenClankManagedProtocol.ManagedContextAugmentRequest, {kind:"deliverable_appearance"}>): Promise<Extract<OpenClankManagedProtocol.ManagedContextAugmentResult, {kind:"deliverable_appearance"}>>
export function managedHostCall<M extends keyof OpenClankManagedProtocol.MethodRequestMap>(method: M, params: OpenClankManagedProtocol.MethodRequestMap[M]): Promise<OpenClankManagedProtocol.MethodResultMap[M]>
export async function managedHostCall<M extends keyof OpenClankManagedProtocol.MethodRequestMap>(
  method: M,
  params: OpenClankManagedProtocol.MethodRequestMap[M],
): Promise<OpenClankManagedProtocol.MethodResultMap[M]> {
  const result = await OpenClankManagedProtocol.callHost(connection(), method, params)
  if (method === "_openclank/session/v1/turn/finish" && (result as { accepted?: boolean }).accepted === true) {
    const turn = params as OpenClankManagedProtocol.ManagedTurnIdentity
    const operation = activeOperation(turn.sessionID, turn.actorID, turn.executionRevision)
    if (operation?.ingressTurn && sameIngressTurn(operation.ingressTurn.identity, turn)) operation.ingressTurn = undefined
    releaseFreshTaskTurn(turn)
  }
  return result
}

/** Ask the authenticated host immediately before a private tool invocation. */
export async function admitManagedTool(
  identity: OpenClankManagedProtocol.ManagedTurnIdentity,
  toolName: string,
  kind: OpenClankManagedProtocol.ManagedToolAdmitRequest["kind"] = "tool",
  toolCallID?: string,
  toolOperation?: OpenClankManagedProtocol.ManagedToolAdmitRequest["toolOperation"],
): Promise<string | undefined> {
  if (!identity.sessionID || !identity.rootOperationID || !identity.actorID || !identity.innerTurnID ||
      !Number.isSafeInteger(identity.executionRevision) || identity.executionRevision < 0 || !toolName)
    throw new ManagedProviderError("Managed tool admission identity is incomplete")
  const safeCallID = typeof toolCallID === "string" && toolCallID.length > 0 && toolCallID.length <= 256 ? toolCallID : undefined
  const result = await managedHostCall("_openclank/session/v1/tool/admit", { ...identity, toolName, kind, ...(safeCallID ? {toolCallID:safeCallID} : {}), ...(toolOperation ? {toolOperation} : {}) })
  if (result.allowed !== true) throw new ManagedProviderError("Managed tool is unavailable under current authority")
  return (kind === "memory_read" && toolName === "recall_memory") || kind === "project_policy_read" || (kind === "skill_use" && toolName === "skill") ? result.invocationID : undefined
}

/** Private original marker; not a tool permission or a model-controlled argument. */
export function encodeShellRewriteAdmission(value:unknown):string | undefined {
  if (!value || typeof value!=="object" || Array.isArray(value)) return
  const v=value as Record<string,unknown>
  if (Object.keys(v).sort().join(",")!=="profile,settingsRevision,version" || v.version!==1 || v.profile!=="git-diff-no-color-v1" || typeof v.settingsRevision!=="string" || !/^[a-f0-9]{64}$/.test(v.settingsRevision)) return
  return JSON.stringify({version:1,profile:"git-diff-no-color-v1",settingsRevision:v.settingsRevision})
}
type ShellRewriteContext = {sessionID:string;callID?:string;abort:AbortSignal;extra?:{[key:string]:unknown}}
function shellRewriteSnapshot(ctx:ShellRewriteContext) {
  try {
    const id=ctx.extra?.actorExecution as import("../actor/turn").ActorExecutionIdentity | undefined
    if (!enabled() || ctx.abort.aborted || !ctx.callID || ctx.callID.length>256 || !id || id.sessionID!==ctx.sessionID || !id.actorID || !id.rootOperationID || !id.innerTurnID || !Number.isSafeInteger(id.executionRevision) || id.executionRevision<0) return
    const identity={sessionID:id.sessionID,actorID:id.actorID,executionRevision:id.executionRevision,rootOperationID:id.rootOperationID,innerTurnID:id.innerTurnID}
    const host=currentActorHostContext(id.sessionID,id.actorID,id.executionRevision), marker=host?.shellRewriteAdmission
    const scope=currentScope(id.sessionID,id.actorID,id.executionRevision), issuer=currentActorToolTimingIssuer(id.sessionID,id.actorID,id.executionRevision,id.rootOperationID)
    if (!marker || encodeShellRewriteAdmission(JSON.parse(marker))!==marker || !scope || !issuer?.isAlive() || !isAdmittedToolTimingTurn(id,identity)) return
    return {identity,marker,scope,issuer,host}
  } catch { return }
}
export function shellRewriteCandidate(ctx:ShellRewriteContext):boolean { return !!shellRewriteSnapshot(ctx) }
/** Policy refusal throws; optional transport/proof failures only decline the rewrite. */
export async function shellRewriteCurrent(ctx:ShellRewriteContext):Promise<boolean> {
  const initial=shellRewriteSnapshot(ctx); if (!initial) return false
  let result:OpenClankManagedProtocol.ManagedToolAdmitResult
  try { result=await managedHostCall("_openclank/session/v1/tool/admit",{...initial.identity,toolName:"bash",kind:"tool",toolCallID:ctx.callID!,shellRewriteAdmission:JSON.parse(initial.marker)}) } catch { return false }
  if (result.allowed!==true) throw new ManagedProviderError("Managed tool is unavailable under current authority")
  const current=shellRewriteSnapshot(ctx)
  return !!current && current.marker===initial.marker && current.scope===initial.scope && current.issuer===initial.issuer && encodeShellRewriteAdmission(result.shellRewriteProof)===initial.marker
}

type AlternativeOriginalInput = Readonly<{user:unknown; messages:readonly unknown[]}>
type AlternativeRoute = Readonly<{connectionID:string; providerID:string; modelID:string; modelRouteID:string;
  catalogRevision:number; options:Readonly<{reasoning_effort?:string}>}>
type AlternativeReceipt = Readonly<{version:1; primaryBindingID:string; primaryBindingRevision:number; coordinatorRevision:number;
  alternativeBindingID:string; alternativeBindingRevision:number; attemptID:string; attemptGeneration:2}>
type AlternativePrepared = {phase:'prepared'|'active'|'retired'; readonly host:AgentSideConnection; readonly turn:OpenClankManagedProtocol.ManagedTurnIdentity;
  readonly originalBinding:AccountSelection.Binding; readonly originalScope:AccountSelection.Scope; readonly binding:AccountSelection.Binding;
  readonly scope:AccountSelection.Scope; readonly route:AlternativeRoute; readonly receipt:AlternativeReceipt}

export type AlternativeExecutionProof = Readonly<{version:1; sessionID:string; rootOperationID:string; actorID:'main';
  executionRevision:number; innerTurnID:string; primaryBindingID:string; primaryBindingRevision:number; coordinatorRevision:number;
  alternativeBindingID:string; alternativeBindingRevision:number; attemptID:string; attemptGeneration:2;
  routeID:string; connectionID:string; providerID:string; modelID:string; billingLane:string; catalogRevision:number;
  options:Readonly<{reasoning_effort?:string}>}>

export type OwnedAlternativeRebuild = Readonly<{turn:OpenClankManagedProtocol.ManagedTurnIdentity; route:AlternativeRoute;
  original:AlternativeOriginalInput; hostContext:ManagedHostContext; current:()=>boolean}>

type ActiveOperation = {
  ingressTurn?: {readonly identity: OpenClankManagedProtocol.ManagedTurnIdentity; readonly required: boolean; readonly workerNamingEnabled:boolean; readonly host: AgentSideConnection; readonly scope: AccountSelection.Scope}
  originalRebuildInput?: AlternativeOriginalInput
  terminalReceipt?: OpenClankManagedProtocol.AccountTerminalResult
  preparedAlternative?: AlternativePrepared
  alternativeCommitted?: AlternativeExecutionProof
  alternativeIssueState?: 'issuing'|'unknown'|'prepared'
  rebuildContext?: OwnedAlternativeRebuild
  rebuildRetired?: boolean
  ownedAttempt?: OwnedAnthropicAttempt
  readonly qualifiedTurn?: OpenClankManagedProtocol.ManagedTurnIdentity
  qualifiedAttempt?: {readonly version:1;readonly attemptID:string;readonly attemptGeneration:number;readonly coordinatorRevision:number;readonly accounting?:'unavailable'}
  qualifiedControlUnknown?:boolean
  qualifiedRetired?:boolean
  readonly timingHost?: AgentSideConnection
  readonly timingIssuer?: ReturnType<typeof toolTimingIssuerFor>
  binding: AccountSelection.Binding
  scope: AccountSelection.Scope
  readonly routeValue: unknown
  transientRetries: number
  retryWaitMs: number
  hostContext?: ManagedHostContext
  refreshContinuity?: RefreshContinuity
  malformedObservationScope?: AccountSelection.Scope
  successCommit?: {before:AccountSelection.Binding;scope:AccountSelection.Scope;host:AgentSideConnection;admissionID:string;holderID:string;registration:object;refresh?:RefreshContinuity}
}

type RefreshContinuity = {operation:ActiveOperation;key:string;oldScope:AccountSelection.Scope;nextScope:AccountSelection.Scope;
  before:AccountSelection.Binding; host:AgentSideConnection; admissionID:string; holderID:string; registration:object; grantID?:string; grantRevision?:number}

type PendingOperation = { state:"pending"|"issuing"|"unknown"; binding:AccountSelection.Binding; routeValue:unknown;
  readonly timingHost:AgentSideConnection; readonly timingIssuer?:ReturnType<typeof toolTimingIssuerFor>; readonly hostContext:ManagedHostContext; leased?:ActiveOperation }
type OperationSlot = ActiveOperation | PendingOperation | { readonly state: "acquiring" }
const operations = new Map<string, OperationSlot>()

function refreshBindingMatches(before:AccountSelection.Binding,after:AccountSelection.Binding,revision:number):boolean {
  const keys=['bindingID','bindingRevision','rootOperationID','scopeKind','operationID','sessionID','actorID','executionRevision',
    'connectionID','providerID','billingLane','modelID','modelRouteID','accountID','credentialRequired'] as const
  return keys.every((key)=>before[key]===after[key]) && after.credentialRevision===revision && after.committed===false
    && sameMethodPolicy(before.methodPolicy,after.methodPolicy)
}

function freezeRefreshContinuity(scope?:AccountSelection.Scope) {
  if (!scope) return undefined
  const matches=[...operations.entries()].filter(([,value])=>!('state' in value) && value.scope===scope)
  if (matches.length!==1) return undefined
  const [key,value]=matches[0]!, operation=value as ActiveOperation
  operation.refreshContinuity=undefined
  // Preserve a conservative original error witness even if optional continuity later fails.
  operation.malformedObservationScope ??= scope
  const binding=operation.binding
  if (binding.scopeKind!=='actor_execution' || binding.providerID!=='openai' || binding.billingLane!=='subscription'
      || binding.committed || !binding.accountID || !Number.isSafeInteger(binding.credentialRevision) || (binding.credentialRevision ?? 0)<1) return undefined
  const host=connection(), capture=refreshCaptureIdentity(scope,binding,host)
  if (!capture) return undefined
  const route=validateRouteContext(operation.routeValue,binding.providerID,binding.modelID)
  return {operation,key,oldScope:scope,before:{...binding},host,...capture,grantID:route.grantID,grantRevision:route.grantRevision}
}

function refreshContinuityCurrent(candidate:RefreshContinuity):boolean {
  try {
    const operation=candidate.operation, before=candidate.before, next=(before.credentialRevision ?? -2)+1
    if (operations.get(candidate.key)!==operation || connection()!==candidate.host || operation.scope!==candidate.nextScope
        || candidate.nextScope===candidate.oldScope || candidate.nextScope.connectionID!==before.connectionID
        || candidate.nextScope.accountID!==before.accountID || candidate.nextScope.billingLane!==before.billingLane
        || candidate.nextScope.credentialRevision!==next || !refreshBindingMatches(before,operation.binding,next)) return false
    const capture=refreshCaptureIdentity(candidate.oldScope,before,candidate.host)
    const route=validateRouteContext(operation.routeValue,before.providerID,before.modelID)
    return !!capture && capture.admissionID===candidate.admissionID && capture.holderID===candidate.holderID && capture.registration===candidate.registration
      && route.grantID===candidate.grantID && route.grantRevision===candidate.grantRevision
      && route.rootOperationID===before.rootOperationID && route.connectionID===before.connectionID
      && route.providerID===before.providerID && route.billingLane===before.billingLane && route.modelID===before.modelID
      && route.modelRouteID===before.modelRouteID
  } catch { return false }
}
function refreshedCommitCurrent(candidate:RefreshContinuity, operation:ActiveOperation):boolean {
  try {
    const prior=candidate.before, binding=operation.binding, route=validateRouteContext(operation.routeValue,prior.providerID,prior.modelID)
    return operations.get(candidate.key)===operation && candidate.operation===operation && connection()===candidate.host && operation.scope===candidate.nextScope
      && binding.committed===true && binding.bindingRevision===prior.bindingRevision+1
      && binding.credentialRevision===(prior.credentialRevision ?? -2)+1
      && refreshBindingMatches({...prior,bindingRevision:binding.bindingRevision},{...binding,committed:false},binding.credentialRevision!)
      && route.grantID===candidate.grantID && route.grantRevision===candidate.grantRevision
      && route.rootOperationID===prior.rootOperationID && route.connectionID===prior.connectionID
      && route.providerID===prior.providerID && route.billingLane===prior.billingLane && route.modelID===prior.modelID && route.modelRouteID===prior.modelRouteID
  } catch { return false }
}
const finalCapture = new Map<string, { sessionID: string; actorID: string; rootOperationID: string; executionRevision: number; modelRequestOptions?: OpenClankManagedProtocol.ActorOptionSnapshot; alternativeExecutionProof?: AlternativeExecutionProof; grantID?: string; binding: Pick<AccountSelection.Binding, "bindingID" | "bindingRevision" | "connectionID" | "providerID" | "billingLane" | "modelID" | "modelRouteID" | "accountID" | "credentialRevision">; sdk: ReturnType<typeof captureSdkEvidence>; scope: ReturnType<typeof captureScopeEvidence> }>()

/** Consume nonsecret evidence for the exact completed main actor execution. */
export function consumeActorCapture(sessionID: string, actorID: string, executionRevision: number) {
  const key = operationKey(sessionID, actorID, executionRevision)
  const evidence = finalCapture.get(key)
  finalCapture.delete(key)
  return evidence
}

/** Resolve only a unique frozen-root completion; ambiguity is never guessed. */
export function consumeActorCaptureForRoot(sessionID: string, actorID: string, rootOperationID: string) {
  const matches = [...finalCapture.entries()].filter(([, value]) => value.sessionID === sessionID && value.actorID === actorID && value.rootOperationID === rootOperationID)
  if (matches.length !== 1) return undefined
  finalCapture.delete(matches[0][0])
  return matches[0][1]
}
const operationAcquiring = { state: "acquiring" } as const

function operationKey(sessionID: string, actorID?: string, executionRevision?: number): string {
  if (actorID && executionRevision === undefined) throw new ManagedProviderError("Managed actor execution revision is required")
  return actorID ? `${sessionID}\0${actorID}\0${executionRevision}` : sessionID
}

function activeOperation(sessionID: string, actorID?: string, executionRevision?: number): ActiveOperation | undefined {
  const value = operations.get(operationKey(sessionID, actorID, executionRevision))
  return value && ("state" in value || value.qualifiedControlUnknown || value.qualifiedRetired) ? undefined : value
}

/** Capture only actual original MAIN inputs before dispatch; the caller owns their source admission. */
export function freezeOriginalAlternativeInput(turn:OpenClankManagedProtocol.ManagedTurnIdentity,
  input:{user:unknown;messages:readonly unknown[]}):boolean {
  const operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision), original=operation?.qualifiedTurn
  if (!operation || operation.rebuildRetired || operation.originalRebuildInput || !operation.qualifiedAttempt || !original || turn.actorID!=='main' ||
      operation.binding.committed || operation.timingHost!==connection() ||
      original.sessionID!==turn.sessionID || original.rootOperationID!==turn.rootOperationID || original.actorID!==turn.actorID ||
      original.executionRevision!==turn.executionRevision || original.innerTurnID!==turn.innerTurnID || !Array.isArray(input.messages)) return false
  try {
    // Bound the admitted data before allocating a clone; no hashes or work on Off paths.
    const source={user:input.user,messages:input.messages}, scan:unknown[]=[source], seen=new Set<object>()
    let sourceCount=0, sourceBytes=0
    while (scan.length) {
      const value=scan.pop()
      if (++sourceCount>200000) return false
      if (typeof value==='string') { sourceBytes+=Buffer.byteLength(value,'utf8'); if (sourceBytes>16*1024*1024) return false; continue }
      if (value===null || value===undefined || typeof value==='boolean' || (typeof value==='number' && Number.isFinite(value))) continue
      if (typeof value!=='object') return false
      if (seen.has(value)) continue
      const proto=Object.getPrototypeOf(value)
      if (!Array.isArray(value) && proto!==Object.prototype && proto!==null) return false
      seen.add(value); for (const child of Object.values(value)) scan.push(child)
    }
    const copy=structuredClone(source)
    const pending:unknown[]=[copy], visited=new Set<object>(); let count=0, bytes=0
    while (pending.length) {
      const value=pending.pop()
      if (++count>200000) return false
      if (typeof value==='string') { bytes+=Buffer.byteLength(value,'utf8'); if (bytes>16*1024*1024) return false; continue }
      if (value===null || value===undefined || typeof value==='boolean' || (typeof value==='number' && Number.isFinite(value))) continue
      if (typeof value!=='object') return false
      if (visited.has(value)) continue
      const proto=Object.getPrototypeOf(value)
      if (!Array.isArray(value) && proto!==Object.prototype && proto!==null) return false
      visited.add(value); for (const child of Object.values(value)) pending.push(child); Object.freeze(value)
    }
    if (activeOperation(turn.sessionID,turn.actorID,turn.executionRevision)!==operation || operation.originalRebuildInput || operation.timingHost!==connection()) return false
    operation.originalRebuildInput=copy
    return true
  } catch { return false }
}

/** One private rebuild view for the same positively stopped operation and prepared host attempt. */
export function captureOwnedAlternativeRebuild(turn:OpenClankManagedProtocol.ManagedTurnIdentity):OwnedAlternativeRebuild|undefined {
  const key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision), slot=operations.get(key), host=connection()
  if (!slot || 'state' in slot) return
  const operation=slot, prepared=operation.preparedAlternative, terminal=operation.terminalReceipt, original=operation.qualifiedTurn
  const input=operation.originalRebuildInput, context=operation.hostContext
  if (!prepared || prepared.phase!=='prepared' || operation.rebuildRetired || operation.rebuildContext || !terminal || !original || !input || !context ||
      !operation.qualifiedRetired || !operation.qualifiedControlUnknown || operation.timingHost!==host || prepared.host!==host ||
      prepared.originalBinding!==operation.binding || prepared.originalScope!==operation.scope || turn.actorID!=='main' ||
      original.sessionID!==turn.sessionID || original.rootOperationID!==turn.rootOperationID || original.actorID!==turn.actorID ||
      original.executionRevision!==turn.executionRevision || original.innerTurnID!==turn.innerTurnID ||
      prepared.turn.sessionID!==turn.sessionID || prepared.turn.rootOperationID!==turn.rootOperationID || prepared.turn.actorID!==turn.actorID ||
      prepared.turn.executionRevision!==turn.executionRevision || prepared.turn.innerTurnID!==turn.innerTurnID ||
      prepared.receipt.version!==1 || !/^[0-9a-f]{64}$/.test(prepared.receipt.attemptID) || prepared.receipt.attemptID===terminal.attemptID ||
      prepared.receipt.primaryBindingID!==terminal.bindingID || prepared.receipt.primaryBindingRevision!==terminal.bindingRevision ||
      prepared.receipt.coordinatorRevision!==terminal.coordinatorRevision+1 || prepared.receipt.attemptGeneration!==2 ||
      prepared.receipt.alternativeBindingID!==prepared.binding.bindingID || prepared.receipt.alternativeBindingRevision!==prepared.binding.bindingRevision || prepared.binding.bindingRevision!==1 ||
      prepared.binding.connectionID!==prepared.route.connectionID || prepared.binding.providerID!==prepared.route.providerID ||
      prepared.binding.modelID!==prepared.route.modelID || prepared.binding.modelRouteID!==prepared.route.modelRouteID ||
      prepared.binding.scopeKind!=='actor_execution' || prepared.binding.sessionID!==turn.sessionID || prepared.binding.actorID!=='main' ||
      prepared.binding.executionRevision!==turn.executionRevision || prepared.binding.rootOperationID!==turn.rootOperationID ||
      prepared.binding.committed || !prepared.binding.credentialRequired || !prepared.binding.accountID ||
      prepared.scope===prepared.originalScope || prepared.scope.connectionID!==prepared.binding.connectionID ||
      prepared.scope.accountID!==prepared.binding.accountID || prepared.scope.billingLane!==prepared.binding.billingLane ||
      prepared.scope.credentialRevision!==prepared.binding.credentialRevision || prepared.scope.credential?.type!=='api' ||
      !Number.isSafeInteger(prepared.binding.credentialRevision) || (prepared.binding.credentialRevision ?? 0)<1 ||
      prepared.binding.billingLane!=='metered_api' || prepared.route.providerID!=='anthropic' ||
      !Number.isSafeInteger(prepared.route.catalogRevision) || prepared.route.catalogRevision<1 ||
      !prepared.route.options || Object.keys(prepared.route.options).some(key=>key!=='reasoning_effort') ||
      (prepared.route.options.reasoning_effort!==undefined && (typeof prepared.route.options.reasoning_effort!=='string' ||
        !prepared.route.options.reasoning_effort || prepared.route.options.reasoning_effort.trim()!==prepared.route.options.reasoning_effort || Buffer.byteLength(prepared.route.options.reasoning_effort,'utf8')>128))) return
  const routeIdentity=JSON.stringify(prepared.route), contextIdentity=JSON.stringify(context)
  const frozenRoute=Object.freeze({...prepared.route,options:Object.freeze({...prepared.route.options})})
  let result:OwnedAlternativeRebuild
  const current=():boolean=>{ try { return operations.get(key)===operation && operation.preparedAlternative===prepared && prepared.phase==='prepared' &&
    !operation.rebuildRetired && operation.terminalReceipt===terminal && operation.originalRebuildInput===input && operation.qualifiedTurn===original &&
    operation.binding===prepared.originalBinding && operation.scope===prepared.originalScope && operation.hostContext===context &&
    operation.rebuildContext===result && JSON.stringify(prepared.route)===routeIdentity && JSON.stringify(context)===contextIdentity &&
    operation.qualifiedRetired===true && operation.qualifiedControlUnknown===true && operation.timingHost===host && connection()===host
  } catch { return false } }
  const {grantID:_grantID,grantRevision:_grantRevision,...baseContext}=context
  const alternativeContext=Object.freeze({...baseContext,accountID:prepared.binding.accountID,credentialRevision:prepared.binding.credentialRevision})
  result=Object.freeze({turn:Object.freeze({...turn}),route:frozenRoute,original:input,hostContext:alternativeContext,current})
  operation.rebuildContext=result
  return current() ? result : undefined
}

/** Exact private identity check; a caller-shaped object is not a rebuild capability. */
export function currentOwnedAlternativeRebuild(context:OwnedAlternativeRebuild):boolean {
  const turn=context.turn, slot=operations.get(operationKey(turn.sessionID,turn.actorID,turn.executionRevision))
  return !!slot && !('state' in slot) && slot.rebuildContext===context && context.current()
}

export function isOwnedAlternativeActive(turn:OpenClankManagedProtocol.ManagedTurnIdentity, scope:AccountSelection.Scope):boolean {
  const operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision), prepared=operation?.preparedAlternative, original=operation?.qualifiedTurn
  if (!operation || !prepared || prepared.phase!=='active' || !original || operation.scope!==scope || prepared.scope!==scope) return false
  try { return original.sessionID===turn.sessionID && original.rootOperationID===turn.rootOperationID && original.actorID===turn.actorID &&
    original.executionRevision===turn.executionRevision && original.innerTurnID===turn.innerTurnID && operation.timingHost===connection()
  } catch { return false }
}

/** Private bookkeeping only: this handle cannot seal or authorize a retry. */
export type OwnedAnthropicAttempt = Readonly<{ probe: AnthropicFailureProbe; current: () => boolean; deny: () => void; take: () => {readonly error:object} | undefined }>

export function beginOwnedAnthropicAttempt(turn: OpenClankManagedProtocol.ManagedTurnIdentity, model: object, expectedScope: AccountSelection.Scope): OwnedAnthropicAttempt | undefined {
  if (!isPinnedAnthropicModel(model) || turn.actorID !== "main") return
  const key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision), operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision)
  if (!operation || operation.scope!==expectedScope || operation.ownedAttempt || operation.binding.committed || !operation.qualifiedTurn || !operation.qualifiedAttempt || operation.binding.scopeKind!=="actor_execution" || operation.binding.billingLane!=="metered_api") return
  const originalTurn=operation.qualifiedTurn, binding=operation.binding, scope=operation.scope, attempt=operation.qualifiedAttempt
  if (originalTurn.sessionID!==turn.sessionID || originalTurn.rootOperationID!==turn.rootOperationID || originalTurn.actorID!==turn.actorID || originalTurn.executionRevision!==turn.executionRevision || originalTurn.innerTurnID!==turn.innerTurnID) return
  let host:AgentSideConnection
  try { host=connection(); if (host!==operation.timingHost) return; qualifiedControl(operation) } catch { return }
  const fingerprint=JSON.stringify([originalTurn.sessionID,originalTurn.rootOperationID,originalTurn.actorID,originalTurn.executionRevision,originalTurn.innerTurnID,binding.bindingID,binding.bindingRevision,binding.connectionID,binding.providerID,binding.modelID,binding.modelRouteID,binding.accountID,binding.credentialRevision,attempt.attemptID,attempt.attemptGeneration,attempt.coordinatorRevision])
  const probe:AnthropicFailureProbe={sends:0,state:"pending"}
  if (!attachCaptureAttempt(scope,probe)) return
  let denied=false
  const handle:OwnedAnthropicAttempt=Object.freeze({probe,deny:()=>{denied=true;probe.state="unavailable";delete probe.localOutcome;retireCaptureAttempt(scope,probe)},take:()=>{
    if (!handle.current() || probe.state!=="observed" || !probe.localOutcome) return
    const result=probe.localOutcome;delete probe.localOutcome;denied=true;probe.state="unavailable";retireCaptureAttempt(scope,probe);return result
  },current:()=>{
    if (denied || operations.get(key)!==operation || operation.ownedAttempt!==handle || operation.qualifiedRetired || operation.qualifiedControlUnknown || operation.binding!==binding || operation.scope!==scope || operation.qualifiedTurn!==originalTurn || operation.qualifiedAttempt!==attempt || binding.committed) return false
    try { return connection()===host && operation.timingHost===host && isPinnedAnthropicModel(model) && fingerprint===JSON.stringify([originalTurn.sessionID,originalTurn.rootOperationID,originalTurn.actorID,originalTurn.executionRevision,originalTurn.innerTurnID,binding.bindingID,binding.bindingRevision,binding.connectionID,binding.providerID,binding.modelID,binding.modelRouteID,binding.accountID,binding.credentialRevision,attempt.attemptID,attempt.attemptGeneration,attempt.coordinatorRevision]) } catch { return false }
  }})
  operation.ownedAttempt=handle
  return handle
}

/** Local-only one-use observation; never a host terminal or retry grant. */
export function takeOwnedAnthropicOutcome(turn: OpenClankManagedProtocol.ManagedTurnIdentity, error: unknown): boolean {
  const operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision), handle=operation?.ownedAttempt, original=operation?.qualifiedTurn
  if (!handle || !original || !error || typeof error!=="object" || !handle.probe.localOutcome || original.sessionID!==turn.sessionID || original.rootOperationID!==turn.rootOperationID || original.actorID!==turn.actorID || original.executionRevision!==turn.executionRevision || original.innerTurnID!==turn.innerTurnID || handle.probe.localOutcome.error!==error) return false
  const result=handle.take()
  return !!result && result.error===error
}

/** Only the original local H1 observation can issue this terminal arm. */
export async function recordOwnedAnthropicTerminal(turn:OpenClankManagedProtocol.ManagedTurnIdentity,error:unknown):Promise<'not_eligible'|'stopped'|'unknown'> {
  const key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision), operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision)
  if (!operation || operation.preparedAlternative?.phase === 'active' || !operation.qualifiedAttempt || !operation.qualifiedTurn || !operation.ownedAttempt) return 'not_eligible'
  const binding=operation.binding, attempt=operation.qualifiedAttempt, scope=operation.scope, host=connection(), original=operation.qualifiedTurn
  const control=qualifiedControl(operation), bindingID=binding.bindingID, bindingRevision=binding.bindingRevision
  const snapshot=()=>JSON.stringify([binding.bindingID,binding.bindingRevision,binding.rootOperationID,binding.scopeKind,binding.operationID,binding.sessionID,binding.actorID,binding.executionRevision,binding.connectionID,binding.providerID,binding.billingLane,binding.modelID,binding.modelRouteID,binding.accountID,binding.credentialRevision,binding.credentialRequired,binding.source,binding.attempt,binding.committed,binding.methodPolicy,original.sessionID,original.rootOperationID,original.actorID,original.executionRevision,original.innerTurnID,attempt.attemptID,attempt.attemptGeneration,attempt.coordinatorRevision])
  const identity=snapshot()
  if (!takeOwnedAnthropicOutcome(turn,error)) return 'not_eligible'
  operation.qualifiedControlUnknown=true
  try {
    const raw:unknown=await OpenClankManagedProtocol.callHost(host,'_openclank/provider-store/v1/account/attempt',{
      bindingID,expectedRevision:bindingRevision,
      crossModelTerminal:{version:1,turn:control.turn,attemptID:control.attemptID,attemptGeneration:control.attemptGeneration,
        expectedCoordinatorRevision:control.expectedCoordinatorRevision,profile:'anthropic.messages.401-auth-no-output.v1'},
    })
    if (!raw || typeof raw!=='object' || Object.keys(raw).sort().join(',')!==['attemptGeneration','attemptID','bindingID','bindingRevision','coordinatorRevision','state','version'].sort().join(',')) return 'unknown'
    const receipt=raw as Record<string,unknown>
    if (receipt.version!==1 || receipt.state!=='auth_no_output_closed' || receipt.bindingID!==bindingID || receipt.bindingRevision!==bindingRevision+1 ||
        receipt.attemptID!==control.attemptID || receipt.attemptGeneration!==control.attemptGeneration || receipt.coordinatorRevision!==control.expectedCoordinatorRevision+1 ||
        operations.get(key)!==operation || connection()!==host || operation.timingHost!==host || operation.qualifiedRetired || operation.scope!==scope ||
        operation.binding!==binding || operation.qualifiedAttempt!==attempt || operation.qualifiedTurn!==original ||
        identity!==snapshot()) return 'unknown'
    operation.terminalReceipt=Object.freeze({version:1,state:'auth_no_output_closed',bindingID,bindingRevision:bindingRevision+1,
      attemptID:control.attemptID,attemptGeneration:1,coordinatorRevision:control.expectedCoordinatorRevision+1})
    operation.qualifiedRetired=true
    return 'stopped'
  } catch { return 'unknown' }
}

/** One host-issued alternate stays private until the exact original rebuild succeeds. */
export async function prepareOwnedAlternative(turn:OpenClankManagedProtocol.ManagedTurnIdentity):Promise<OwnedAlternativeRebuild|undefined> {
  const key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision), slot=operations.get(key), host=connection()
  if (!slot || 'state' in slot || slot.rebuildRetired || slot.alternativeIssueState || slot.preparedAlternative || !slot.originalRebuildInput ||
      !slot.terminalReceipt || !slot.qualifiedTurn || !slot.qualifiedAttempt || !slot.qualifiedRetired || !slot.qualifiedControlUnknown ||
      slot.timingHost!==host || turn.actorID!=='main' || JSON.stringify(slot.qualifiedTurn)!==JSON.stringify(turn)) return
  const operation=slot, terminal=operation.terminalReceipt, originalBinding=operation.binding, originalScope=operation.scope
  if (!terminal) return
  operation.alternativeIssueState='issuing'
  const current=()=>operations.get(key)===operation && operation.alternativeIssueState==='issuing' && !operation.rebuildRetired &&
    operation.terminalReceipt===terminal && operation.binding===originalBinding && operation.scope===originalScope &&
    operation.qualifiedRetired===true && operation.qualifiedControlUnknown===true && operation.timingHost===host && connection()===host
  try {
    if (!originalBinding.credentialRevision) throw new ManagedProviderError('Original credential identity unavailable')
    const result=await OpenClankManagedProtocol.callHost(host,'_openclank/provider-store/v1/credential/lease',{
      bindingID:terminal.bindingID,expectedBindingRevision:terminal.bindingRevision,expectedCredentialRevision:originalBinding.credentialRevision,
      crossModelAlternative:{version:1,turn:{...turn},terminalAttemptID:terminal.attemptID,expectedCoordinatorRevision:terminal.coordinatorRevision}})
    if (!current() || !('version' in result) || result.version!==1) throw new ManagedProviderError('Alternative result unavailable')
    const binding=decode(AccountSelection.Binding,result.binding,'alternative binding'), lease=decode(CredentialLease,result.lease,'alternative credential')
    const raw=result.lease, wire=result.route, receipt=result.attempt
    if (receipt.version!==1 || receipt.primaryBindingID!==terminal.bindingID || receipt.primaryBindingRevision!==terminal.bindingRevision ||
        receipt.coordinatorRevision!==terminal.coordinatorRevision+1 || receipt.alternativeBindingID!==binding.bindingID ||
        receipt.alternativeBindingRevision!==binding.bindingRevision || receipt.attemptGeneration!==2 || !/^[0-9a-f]{64}$/.test(receipt.attemptID) ||
        binding.rootOperationID!==turn.rootOperationID || binding.sessionID!==turn.sessionID || binding.actorID!=='main' ||
        binding.executionRevision!==turn.executionRevision || binding.scopeKind!=='actor_execution' || binding.committed ||
        binding.connectionID!==wire.connection_id || binding.providerID!==wire.provider_id || binding.modelID!==wire.model_id ||
        binding.modelRouteID!==wire.route_id || binding.billingLane!==wire.lane || wire.provider_id!=='anthropic' || wire.lane!=='metered_api' ||
        wire.grant_id!==null || wire.grant_revision!==null || wire.options.route_id!==wire.route_id ||
        raw.bindingID!==binding.bindingID || raw.rootOperationID!==turn.rootOperationID || raw.sessionID!==turn.sessionID ||
        raw.actorID!=='main' || raw.executionRevision!==turn.executionRevision || raw.modelRouteID!==binding.modelRouteID ||
        lease.connectionID!==binding.connectionID || lease.accountID!==binding.accountID || lease.credentialRevision!==binding.credentialRevision ||
        lease.expiresAt<=Date.now() || lease.credential.type!=='api') throw new ManagedProviderError('Alternative identity changed')
    const scope=installCaptureScope({connectionID:lease.connectionID,accountID:lease.accountID,billingLane:binding.billingLane,
      credentialRequired:true,credentialRevision:lease.credentialRevision,credential:lease.credential},binding,host,null)
    if (!current()) throw new ManagedProviderError('Alternative operation changed')
    const route:AlternativeRoute=Object.freeze({connectionID:wire.connection_id,providerID:wire.provider_id,modelID:wire.model_id,
      modelRouteID:wire.route_id,catalogRevision:wire.options.catalog_revision,options:Object.freeze({...wire.options.options})})
    operation.preparedAlternative={phase:'prepared',host,turn:Object.freeze({...turn}),originalBinding,originalScope,binding,scope,route,receipt:Object.freeze({...receipt})}
    operation.alternativeIssueState='prepared'
    return captureOwnedAlternativeRebuild(turn)
  } catch { if (operations.get(key)===operation && operation.alternativeIssueState==='issuing') operation.alternativeIssueState='unknown'; return }
}

/** Recheck live authority, then install only the already rebuilt private attempt. */
export async function activateOwnedAlternative(context:OwnedAlternativeRebuild):Promise<boolean> {
  const turn=context.turn,key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision),slot=operations.get(key)
  if (!slot || 'state' in slot || slot.rebuildContext!==context || !context.current()) return false
  const operation=slot, prepared=operation.preparedAlternative
  if (!prepared || prepared.phase!=='prepared') return false
  const receipt=prepared.receipt
  const identity:OpenClankManagedProtocol.CrossModelAlternativeIdentity={version:1,turn:{...turn},
    primaryBindingID:receipt.primaryBindingID,primaryBindingRevision:receipt.primaryBindingRevision,
    alternativeBindingID:receipt.alternativeBindingID,alternativeBindingRevision:1,attemptID:receipt.attemptID,
    attemptGeneration:2,expectedCoordinatorRevision:receipt.coordinatorRevision}
  try {
    const result=await OpenClankManagedProtocol.callHost(prepared.host,'_openclank/provider-store/v1/account/attempt',{
      bindingID:receipt.primaryBindingID,expectedRevision:receipt.primaryBindingRevision,crossModelDispatch:identity})
    if (!context.current() || !('state' in result) || result.state!=='ready' || result.version!==1 ||
        result.primaryBindingID!==receipt.primaryBindingID || result.primaryBindingRevision!==receipt.primaryBindingRevision ||
        result.alternativeBindingID!==receipt.alternativeBindingID || result.alternativeBindingRevision!==receipt.alternativeBindingRevision ||
        result.attemptID!==receipt.attemptID || result.attemptGeneration!==2 || result.coordinatorRevision!==receipt.coordinatorRevision)
      throw new ManagedProviderError('Alternative authority changed')
    operation.binding=prepared.binding;operation.scope=prepared.scope;operation.hostContext={...context.hostContext}
    operation.qualifiedAttempt={version:1,attemptID:receipt.attemptID,attemptGeneration:2,coordinatorRevision:receipt.coordinatorRevision}
    operation.qualifiedRetired=false;operation.qualifiedControlUnknown=false;prepared.phase='active'
    return true
  } catch { prepared.phase='retired';operation.rebuildRetired=true;return false }
}

/** Snapshot nonsecret evidence while the exact actor lease still owns it. */
export async function snapshotActorCapture(sessionID: string, actorID: string, executionRevision: number) {
  const operation = activeOperation(sessionID, actorID, executionRevision)
  if (!operation) throw new ManagedProviderError("Managed actor capture is unavailable")
  const deliveryComplete = await flushCaptureScope(operation.scope)
  if (activeOperation(sessionID, actorID, executionRevision) !== operation)
    throw new ManagedProviderError("Managed actor capture changed during settlement")
  return {
    binding: {
      bindingID: operation.binding.bindingID, bindingRevision: operation.binding.bindingRevision,
      connectionID: operation.binding.connectionID, providerID: operation.binding.providerID,
      billingLane: operation.binding.billingLane, modelID: operation.binding.modelID,
      modelRouteID: operation.binding.modelRouteID, accountID: operation.binding.accountID,
      credentialRevision: operation.binding.credentialRevision,
    },
    sdk: structuredClone(captureSdkEvidence(operation.scope)), scope: captureScopeEvidence(operation.scope), deliveryComplete,
  }
}

/** Finish remains authoritative even when optional nonsecret capture is unavailable. */
export async function finishActorExecution(
  identity: OpenClankManagedProtocol.ManagedExecutionIdentity,
  outcome?: "success" | "failure" | "cancelled",
  recovery?: {rootSessionID:string;innerTurnID:string;turnOutcome:"completed"|"cancelled"|"error";executionOutcome:"success"|"failure"|"cancelled"},
) {
  let evidence: OpenClankManagedProtocol.ManagedActorFinalEvidence | undefined
  if (outcome) {
    try {
      const capture = await snapshotActorCapture(identity.sessionID, identity.actorID, identity.executionRevision)
      const sdk = capture.sdk
      if (sdk) {
        const names = new Set(["inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "audioInputTokens", "audioOutputTokens", "imageInputTokens", "imageOutputTokens", "searchRequests"])
        const losses = [...new Set([...sdk.lossReasons, ...capture.scope.lossReasons])]
        const lossNames = new Set(["dropped", "truncated", "parse_degraded", "write_failed", "interrupted", "admission_unavailable", "queue_full", "delivery_timeout"])
        const coverage = Object.entries(sdk.metricCoverage)
        const ids = sdk.coveredDispatchIDs
        const valid = Object.entries(sdk.usage).length <= 11 && Object.entries(sdk.usage).every(([key, value]) => names.has(key) && Number.isSafeInteger(value) && value >= 0) &&
          coverage.length <= 16 && coverage.every(([, value]) => ["reported", "estimated", "unavailable", "not_applicable"].includes(value.state) && ["complete", "partial", "unknown"].includes(value.coverage) && typeof value.source === "string" && value.source.length > 0 && value.source.length <= 64 && (value.reason === undefined || (typeof value.reason === "string" && value.reason.length > 0 && value.reason.length <= 128))) &&
          ids.length <= 1000 && new Set(ids).size === ids.length && ids.every((id) => typeof id === "string" && id.length > 0 && id.length <= 128) &&
          losses.length <= 16 && losses.every((item) => lossNames.has(item)) && sdk.normalizationProfile.length > 0 && sdk.normalizationProfile.length <= 128
        if (valid) evidence = { version: 1, bindingID: capture.binding.bindingID, bindingRevision: capture.binding.bindingRevision,
          outcome, usage: sdk.usage, metricCoverage: sdk.metricCoverage, coveredDispatchIDs: ids,
          lossReasons: losses, identityCoverage: capture.scope.identityCoverage, normalizationProfile: sdk.normalizationProfile,
          ...(identity.actorID === "main" && outcome === "success" && capture.deliveryComplete && capture.scope.identityCoverage === "complete" && capture.scope.coveredDispatchIDs.length > 0 ? { lastDispatch: { dispatchID: capture.scope.coveredDispatchIDs.at(-1)!, complete: true as const } } : {}) }
      }
    } catch {
      // Missing capture is not an execution-control failure.
    }
  }
  return managedHostCall("_openclank/session/v1/execution/finish", { ...identity, ...(outcome ? {executionOutcome:outcome} : {}), ...(evidence ? { evidence } : {}), ...(recovery ? {recovery} : {}) })
}

/** Context belongs to one admitted actor execution; never consult the session fallback. */
export function currentActorToolTimingIssuer(sessionID: string, actorID: string, executionRevision: number, rootOperationID: string) {
  const operation=activeOperation(sessionID,actorID,executionRevision)
  return operation && operation.timingHost===connection() && operation.binding.rootOperationID===rootOperationID ? operation.timingIssuer : undefined
}

export function currentActorHostContext(sessionID: string, actorID: string, executionRevision: number): ManagedHostContext | undefined {
  const context = activeOperation(sessionID, actorID, executionRevision)?.hostContext
  return context ? { ...context } : undefined
}

type DisplayOwnerCapability = { readonly operation: ActiveOperation; readonly host: AgentSideConnection;
  readonly identity: Readonly<OpenClankManagedProtocol.ManagedTurnIdentity> }

/** Freeze the original operation and connection before requesting host admission. */
export function captureActorDisplayOwner(identity: OpenClankManagedProtocol.ManagedTurnIdentity): DisplayOwnerCapability | undefined {
  const operation = activeOperation(identity.sessionID, identity.actorID, identity.executionRevision)
  const host = connection()
  if (!operation || !operation.hostContext || operation.timingHost !== host || operation.binding.rootOperationID !== identity.rootOperationID) return
  return { operation, host, identity: Object.freeze({ ...identity }) }
}

/** Apply only to the same operation and connection that issued the exact turn request. */
export function bindActorDisplayOwner(capability: DisplayOwnerCapability, identity: OpenClankManagedProtocol.ManagedTurnIdentity, accountID: string): void {
  const operation = activeOperation(identity.sessionID, identity.actorID, identity.executionRevision)
  const captured = capability.identity
  if (operation !== capability.operation || !operation?.hostContext || capability.host !== connection() || operation.timingHost !== capability.host ||
      captured.sessionID !== identity.sessionID || captured.actorID !== identity.actorID || captured.rootOperationID !== identity.rootOperationID ||
      captured.executionRevision !== identity.executionRevision || captured.innerTurnID !== identity.innerTurnID || operation.binding.rootOperationID !== identity.rootOperationID ||
      !accountID || Buffer.byteLength(accountID, 'utf8') > 256 ||
      (operation.hostContext.ownerAccountID && operation.hostContext.ownerAccountID !== accountID))
    throw new ManagedProviderError('Managed display owner authority changed')
  operation.hostContext.ownerAccountID = accountID
}

export function sameAuthority(authority: OpenClankManagedProtocol.SessionBindingReadResult, bootstrap: OpenClankManagedProtocol.ActorRouteBootstrap): boolean {
  if (authority.authority || bootstrap.authority)
    return !!authority.authority && !!bootstrap.authority && authority.authority.generation === bootstrap.authority.generation &&
      authority.workspaceRevision === bootstrap.workspaceRevision
  return authority.mapRevision >= bootstrap.mapRevision && authority.mappingRevision === bootstrap.mappingRevision && authority.workspaceRevision === bootstrap.workspaceRevision
}

/** Project post-C2 current binding into this exact actor's nonsecret context. */
export function bindActorHostContext(
  sessionID: string, actorID: string, executionRevision: number,
  authority: OpenClankManagedProtocol.SessionBindingReadResult,
): void {
  const operation = activeOperation(sessionID, actorID, executionRevision)
  const route = operation?.routeValue as OpenClankManagedProtocol.ActorRoutePreflightResult | undefined
  const bootstrap = route?.bootstrap
  if (!operation || !bootstrap || authority.engineSessionID !== sessionID ||
      authority.owner !== bootstrap.owner || authority.stableChatID !== bootstrap.stableChatID ||
      !sameAuthority(authority, bootstrap))
    throw new ManagedProviderError("Managed actor binding changed after admission")
  if (operation.hostContext?.workspaceID && operation.hostContext.workspaceID !== authority.authorityWorkspaceID)
    throw new ManagedProviderError("Managed actor workspace changed after admission")
  operation.hostContext = {
    ...operation.hostContext, chatID: authority.stableChatID,
    ...(authority.authorityWorkspaceID ? { workspaceID: authority.authorityWorkspaceID } : {}),
    cwd: authority.canonicalCwd,
  }
}

export type ManagedOperationLease = {
  readonly scope: AccountSelection.Scope
  readonly release: Effect.Effect<void>
}

/** Acquire the same authenticated provider lease used by ACP and transfer its
 * release action to an Effect-owned actor fiber. This is used only when a host
 * explicitly resumes an idle main actor after the prior ACP scope has ended. */
export function acquireOperationEffect(input: {
  sessionID: string
  route: unknown
  model: { providerID: string; modelID: string }
  hostContext: ManagedHostContext
}): Effect.Effect<ManagedOperationLease, ManagedProviderError> {
  return Effect.uninterruptibleMask((restore) => Effect.tryPromise({
    try: async () => {
      if (!enabled()) throw new ManagedProviderError("Managed provider callbacks are not enabled")
      if (operations.has(input.sessionID) || hostContexts.has(input.sessionID)) throw new ManagedProviderError("A managed provider operation is already active for this session")
      operations.set(input.sessionID, operationAcquiring)
      let bound: BoundOperation
      let leave: () => void
      try {
        bound = await beginBoundOperation(input.route, input.model)
        leave = enterHostContext(input.sessionID, input.hostContext)
      } catch (error) {
        if (operations.get(input.sessionID) === operationAcquiring) operations.delete(input.sessionID)
        await flushCaptures().catch(() => {})
        throw error
      }
      const operation: ActiveOperation = { ...bound, routeValue: input.route, transientRetries: 0, retryWaitMs: 0 }
      if (operations.get(input.sessionID) !== operationAcquiring) {
        leave()
        await flushCaptures().catch(() => {})
        throw new ManagedProviderError("Managed provider operation reservation was lost")
      }
      operations.set(input.sessionID, operation)
      let released = false
      const release = Effect.uninterruptible(Effect.promise(async () => {
        if (released) return
        released = true
        try {
          await flushCaptures()
        } finally {
          if (operations.get(input.sessionID) === operation) operations.delete(input.sessionID)
          leave()
        }
      }))
      return { scope: bound.scope, release }
    },
    catch: (error) => error instanceof ManagedProviderError ? error : new ManagedProviderError(String(error)),
  }).pipe(
    Effect.flatMap((lease) => restore(Effect.succeed(lease)).pipe(
      Effect.onInterrupt(() => lease.release),
    )),
  ))
}

export function operationScopeAvailable(sessionID: string): boolean {
  return enabled() && !operations.has(sessionID) && !hostContexts.has(sessionID)
}

export class ManagedProviderError extends Error {}

export interface BoundOperation {
  readonly binding: AccountSelection.Binding
  readonly scope: AccountSelection.Scope
  readonly qualifiedAttempt?: {readonly version:1;readonly attemptID:string;readonly attemptGeneration:number;readonly coordinatorRevision:number}
}

/** Private custody of one auxiliary Claude binding, never a chat identity. */
export function ownAuxiliaryClaude(bound: BoundOperation, signal: AbortSignal) {
  const host = connection(), initial = scopeBinding(bound.scope)
  if (!initial || initial.scopeKind !== 'auxiliary_operation' || initial.providerID !== 'anthropic' ||
      initial.billingLane !== 'subscription' || !initial.operationID || !initial.methodPolicy ||
      initial.bindingID !== bound.binding.bindingID || initial.bindingRevision !== bound.binding.bindingRevision)
    throw new ManagedProviderError('Claude auxiliary binding is unavailable')
  const hash = createHash('sha256')
  for (const value of ['openclank-claude-aux-v1', initial.rootOperationID, initial.operationID, initial.bindingID]) {
    const bytes = Buffer.from(value, 'utf8'), length = Buffer.alloc(4)
    length.writeUInt32BE(bytes.length); hash.update(length); hash.update(bytes)
  }
  const profileID = `openclank-aux-v1-${hash.digest('hex')}`
  let current = bound, retired = false, sends = 0, refreshPending = false, refreshUnknown = false
  const valid = () => !retired && !signal.aborted && connection() === host &&
    scopeBinding(current.scope)?.bindingID === current.binding.bindingID &&
    scopeBinding(current.scope)?.bindingRevision === current.binding.bindingRevision &&
    scopeBinding(current.scope)?.credentialRevision === current.binding.credentialRevision
  return {
    profileID,
    host, signal,
    get bound() { if (!valid()) throw new ManagedProviderError('Claude auxiliary operation retired'); return current },
    get lastKnownBound() { return current },
    get issued() { return sends > 0 },
    get uncertain() { return refreshPending || refreshUnknown },
    assertCurrent() { if (!valid()) throw new ManagedProviderError('Claude auxiliary operation retired') },
    beginRefresh() { if (!valid() || refreshPending) throw new ManagedProviderError('Claude auxiliary refresh unavailable'); refreshPending = true },
    failRefresh() { refreshPending = false; refreshUnknown = true; retired = true },
    beforeUpstream(retry: boolean) {
      if (!valid() || (retry ? sends !== 1 : sends !== 0)) throw new ManagedProviderError('Claude auxiliary send unavailable')
      sends++
    },
    replace(before: BoundOperation, account: Auth.Oauth, revision: number) {
      if (!valid() || current !== before || revision !== (before.binding.credentialRevision ?? -2) + 1)
        throw new ManagedProviderError('Claude auxiliary refresh changed')
      const binding = {...before.binding, credentialRevision:revision}
      const next = {...before.scope, credential:account, credentialRevision:revision}
      current = {binding, scope:replaceCapturedScope(before.scope,next,before.binding,binding,true)}
      refreshPending = false
    },
    retire() { retired = true },
  }
}
export type AuxiliaryClaudeOwner = ReturnType<typeof ownAuxiliaryClaude>
type PendingBoundOperation = { readonly pending:true; readonly binding:AccountSelection.Binding }

function decode(schema: any, value: unknown, label: string): any {
  try {
    return Schema.decodeUnknownSync(schema)(value) as any
  } catch {
    throw new ManagedProviderError(`Managed provider host returned an invalid ${label}`)
  }
}

function exactKeys(value: Record<string, unknown>, expected: readonly string[]): boolean {
  return Object.keys(value).length === expected.length && expected.every((key) => key in value)
}

function connection(): AgentSideConnection {
  if (!enabled()) throw new ManagedProviderError("Managed provider callbacks are not enabled")
  if (!host) throw new ManagedProviderError("Managed provider host connection is unavailable")
  return host
}

/** Private history-writer generation, invalidated with the issuer on Agent close. */
export function historySourceGeneration(): object | undefined {
  if (!enabled() || !host || !toolTimingIssuerFor(host)?.isAlive()) return
  return host
}

// Ephemeral proof only: neither restored parts nor another ACP connection can
// mint a current-generation task-create invocation. The source reader still
// verifies the exact persisted message/part and live actor separately.
type FreshTaskCall = { sessionID: string; rootOperationID: string; actorID: string; executionRevision: number;
  innerTurnID: string; messageID: string; partID: string; toolCallID: string; toolName: string }
type FreshTaskSelector = Omit<FreshTaskCall, "partID">
type FreshTaskRecord = { selector: FreshTaskSelector; partID?: string; state: "waiting" | "fresh" | "claimed" | "expired"; wake?: () => void }
const freshTaskCalls = new WeakMap<AgentSideConnection, Map<string, FreshTaskRecord>>()
function freshTaskKey(call: FreshTaskSelector): string {
  return JSON.stringify([call.sessionID, call.rootOperationID, call.actorID, call.executionRevision,
    call.innerTurnID, call.messageID, call.toolCallID, call.toolName])
}
export function markFreshTaskCall(call: FreshTaskCall): void {
  if (!enabled() || !call.rootOperationID || !call.innerTurnID || !(call.toolName === "manage_rag" || call.toolName === "memory" || call.toolName === "workflow_artifact" || call.toolName.endsWith("_manage_tasks") || call.toolName.endsWith("_media_job") || call.toolName.endsWith("_shared_text") || call.toolName.endsWith("_capture_desktop") || call.toolName.endsWith("_manage_files") || call.toolName.endsWith("_write_file") || call.toolName.endsWith("_edit_file")) ||
      !currentActorHostContext(call.sessionID, call.actorID, call.executionRevision)) return
  const host = connection()
  let calls = freshTaskCalls.get(host)
  if (!calls) { calls = new Map(); freshTaskCalls.set(host, calls) }
  const key = freshTaskKey(call)
  const prior = calls.get(key)
  if (prior?.state === "waiting") { prior.partID = call.partID; prior.state = "fresh"; prior.wake?.(); return }
  if (!prior && calls.size < 1024) calls.set(key, { selector: call, partID: call.partID, state: "fresh" })
}
export async function claimFreshTaskCall(call: FreshTaskSelector): Promise<boolean> {
  if (!enabled()) return false
  const host = connection(), key = freshTaskKey(call)
  let calls = freshTaskCalls.get(host)
  if (!calls) { calls = new Map(); freshTaskCalls.set(host, calls) }
  let record = calls.get(key)
  if (!record) {
    if (calls.size >= 1024) return false
    record = { selector: call, state: "waiting" }; calls.set(key, record)
    await new Promise<void>((resolve) => {
      const timer = setTimeout(resolve, 2000)
      record!.wake = () => { clearTimeout(timer); resolve() }
    })
    record.wake = undefined
    if (record.state === "waiting") record.state = "expired"
  }
  if (connection() !== host || record.state !== "fresh" || !currentActorHostContext(call.sessionID, call.actorID, call.executionRevision)) return false
  record.state = "claimed"
  return true
}
export function isClaimedFreshTaskCall(call: FreshTaskCall): boolean {
  if (!enabled() || !currentActorHostContext(call.sessionID, call.actorID, call.executionRevision)) return false
  const record = freshTaskCalls.get(connection())?.get(freshTaskKey(call))
  return record?.state === "claimed" && record.partID === call.partID
}
function releaseFreshTaskTurn(turn: OpenClankManagedProtocol.ManagedTurnIdentity): void {
  if (!host) return
  const calls = freshTaskCalls.get(host)
  if (!calls) return
  for (const [key, record] of calls) {
    const c = record.selector
    if (c.sessionID === turn.sessionID && c.rootOperationID === turn.rootOperationID && c.actorID === turn.actorID &&
        c.executionRevision === turn.executionRevision && c.innerTurnID === turn.innerTurnID) {
      record.state = "expired"; record.wake?.(); calls.delete(key)
    }
  }
}

/** Install the one private ACP connection. It carries no owner selector. */
export function installHostConnection(value: AgentSideConnection): void {
  if (!enabled()) return
  if (host && host !== value) {
    throw new ManagedProviderError("Managed provider host connection is already installed")
  }
  host = value
}

/** Ask the authenticated host to approve and persist a managed chat cwd. */
export async function requestSessionCwdChange(
  sessionID: string,
  requestedCwd: string,
  expectedWorkspaceRevision: number,
  transitionID: string,
): Promise<OpenClankManagedProtocol.SessionCwdChangeResult> {
  const raw = await OpenClankManagedProtocol.callHost(connection(), "_openclank/session/v1/cwd/change", {
    sessionID,
    requestedCwd,
    expectedWorkspaceRevision,
    transitionID,
  })
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) {
    throw new ManagedProviderError("Managed provider host returned an invalid session cwd result")
  }
  const result = raw as Record<string, unknown>
  if (result.outcome === "accepted") {
    if (!exactKeys(result, ["outcome", "canonicalCwd", "workspaceRevision", "changed", "transitionID"]) || typeof result.canonicalCwd !== "string" || !path.isAbsolute(result.canonicalCwd) || path.normalize(result.canonicalCwd) !== result.canonicalCwd || typeof result.workspaceRevision !== "number" || !Number.isSafeInteger(result.workspaceRevision) || typeof result.changed !== "boolean" || result.workspaceRevision !== expectedWorkspaceRevision + (result.changed ? 1 : 0) || result.transitionID !== transitionID) {
      throw new ManagedProviderError("Managed provider host returned an invalid accepted session cwd result")
    }
    return result as unknown as OpenClankManagedProtocol.SessionCwdChangeResult
  }
  const rejectionCodes = new Set(["invalid_cwd", "unknown_session", "stale_engine_session", "owner_mismatch", "workspace_rejected", "workspace_revision_conflict", "binding_unavailable"])
  if (!exactKeys(result, ["outcome", "transitionID", "committed", "code"]) || result.outcome !== "rejected" || result.transitionID !== transitionID || result.committed !== false || typeof result.code !== "string" || !rejectionCodes.has(result.code)) {
    throw new ManagedProviderError("Managed provider host returned an invalid session cwd result")
  }
  return result as unknown as OpenClankManagedProtocol.SessionCwdChangeResult
}

export async function readManagedSessionBinding(
  sessionID: string,
): Promise<OpenClankManagedProtocol.SessionBindingReadResult> {
  const raw = await OpenClankManagedProtocol.callHost(connection(), "_openclank/session/v1/binding/read", { sessionID })
  const decoded = decode(Schema.Unknown, raw, "managed session binding")
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) {
    throw new ManagedProviderError("Managed provider host returned an invalid managed session binding")
  }
  const result = decoded as Record<string, unknown>
  if (result.authority !== undefined) {
    const authority = result.authority
    if (!exactKeys(result, ["authority", "engineSessionID", "stableChatID", "owner", "canonicalCwd", "authorityWorkspaceID", "memoryWorkspaceID", "copalWorkspace", "memoryEnabled", "engineAliases"]) ||
        typeof authority !== "object" || authority === null || Array.isArray(authority) || !exactKeys(authority as Record<string, unknown>, ["kind", "generation"]) ||
        (authority as Record<string, unknown>).kind !== "private" || typeof (authority as Record<string, unknown>).generation !== "string" ||
        !/^[0-9a-f]{64}$/.test((authority as Record<string, string>).generation!) || result.engineSessionID !== sessionID ||
        [result.stableChatID, result.owner, result.canonicalCwd, result.memoryWorkspaceID, result.copalWorkspace].some((v) => typeof v !== "string" || !v.trim() || v !== v.trim()) ||
        typeof result.authorityWorkspaceID !== "string" || result.memoryEnabled !== false || !Array.isArray(result.engineAliases) || result.engineAliases.length ||
        !path.isAbsolute(result.canonicalCwd as string) || path.normalize(result.canonicalCwd as string) !== result.canonicalCwd)
      throw new ManagedProviderError("Managed private session authority is invalid")
    return { authority: { kind: "private", generation: (authority as Record<string, string>).generation! }, engineSessionID: sessionID,
      stableChatID: result.stableChatID as string, owner: result.owner as string, canonicalCwd: result.canonicalCwd as string,
      authorityWorkspaceID: result.authorityWorkspaceID, memoryWorkspaceID: result.memoryWorkspaceID as string,
      copalWorkspace: result.copalWorkspace as string, memoryEnabled: false, engineAliases: [] }
  }
  const requiredStrings = ["engineSessionID", "stableChatID", "owner", "canonicalCwd", "memoryWorkspaceID", "copalWorkspace"]
  const revisions = ["workspaceRevision", "mapRevision", "mappingRevision"]
  const aliases = result.engineAliases
  if (!exactKeys(result, ["engineSessionID", "stableChatID", "owner", "canonicalCwd", "workspaceRevision", "authorityWorkspaceID", "memoryWorkspaceID", "copalWorkspace", "memoryEnabled", "engineAliases", "mapRevision", "mappingRevision"]) || result.engineSessionID !== sessionID || requiredStrings.some((key) => typeof result[key] !== "string" || !(result[key] as string).trim() || (result[key] as string) !== (result[key] as string).trim()) || typeof result.authorityWorkspaceID !== "string" || result.authorityWorkspaceID !== result.authorityWorkspaceID.trim() || !path.isAbsolute(result.canonicalCwd as string) || path.normalize(result.canonicalCwd as string) !== result.canonicalCwd || revisions.some((key) => typeof result[key] !== "number" || !Number.isSafeInteger(result[key]) || (result[key] as number) < 0) || typeof result.memoryEnabled !== "boolean" || !Array.isArray(aliases) || aliases.length > 16 || aliases.some((item) => typeof item !== "string" || !item.trim() || item !== item.trim()) || new Set(aliases).size !== aliases.length || aliases.includes(result.engineSessionID)) {
    throw new ManagedProviderError("Managed provider host returned an invalid managed session binding")
  }
  return result as unknown as OpenClankManagedProtocol.SessionBindingReadResult
}

/** Request-local untrusted source card, scoped to the exact active turn. */
export async function readCodeContext(input: Omit<OpenClankManagedProtocol.SessionCodeContextRequest, "bindingRevision" | "requestID" | "action" | "turn">, signal: AbortSignal, actor?: { sessionID: string; rootOperationID?: string; actorID: string; executionRevision: number; innerTurnID?: string }) {
  if (!enabled() || signal.aborted || !actor || actor.sessionID !== input.sessionID || !actor.rootOperationID || !actor.innerTurnID || !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0) return null
  const turn: OpenClankManagedProtocol.ManagedTurnIdentity = { sessionID: actor.sessionID, rootOperationID: actor.rootOperationID, actorID: actor.actorID, executionRevision: actor.executionRevision, innerTurnID: actor.innerTurnID }
  const context = currentActorHostContext(input.sessionID, actor.actorID, actor.executionRevision)
  const binding = await ensureManagedSessionBinding(input.sessionID)
  if (signal.aborted) return null
  if (!context?.chatID || !binding || binding.authority || binding.transition || !binding.memoryEnabled || !binding.authorityWorkspaceID || context.chatID !== binding.stableChatID || context.workspaceID !== binding.authorityWorkspaceID) return null
  const revision = binding.bindingRevision
  const requestID = randomUUID().replaceAll("-", "")
  const request = { ...input, turn, bindingRevision: revision, requestID }
  let aborted = () => {}
  const abort = new Promise<null>((resolve) => { aborted = () => {
    void OpenClankManagedProtocol.callHost(connection(), "_openclank/session/v1/code-context/read", { ...request, action: "cancel", query: "", retainedMessages: [], availableByteTokens: 0 }).catch(() => {})
    resolve(null)
  }; signal.addEventListener("abort", aborted, { once: true }) })
  let result: OpenClankManagedProtocol.SessionCodeContextResult | null
  try {
    if (signal.aborted) return null
    const pending = OpenClankManagedProtocol.callHost(connection(), "_openclank/session/v1/code-context/read", { ...request, action: "read" })
    result = await Promise.race([pending, abort])
  } finally {
    signal.removeEventListener("abort", aborted)
  }
  if (!result || signal.aborted || result.requestID !== requestID || result.bindingRevision !== revision || result.requestFingerprint !== input.requestFingerprint || (result.card !== null && typeof result.card !== "string")) return null
  const current = await readManagedSessionBinding(input.sessionID)
  if (signal.aborted || current.workspaceRevision !== revision || current.stableChatID !== binding.stableChatID || current.owner !== binding.owner || current.authorityWorkspaceID !== binding.authorityWorkspaceID || !current.memoryEnabled || JSON.stringify(currentActorHostContext(input.sessionID, actor.actorID, actor.executionRevision)) !== JSON.stringify(context)) return null
  return result.card
}

const bindingReads = new Map<string, Promise<OpenClankManagedProtocol.SessionBindingReadResult>>()

function admissionReadKey(sessionID: string): string {
  return `${sessionID}::${registrationGeneration(sessionID)}`
}

export async function ensureManagedSessionBinding(sessionID: string, options: { allowReconciling?: boolean } = {}) {
  if (!enabled()) return managedSessionBinding(sessionID)
  const initial = managedSessionBinding(sessionID)
  if (initial?.transition?.phase === "in_flight" || (initial?.transition?.phase === "reconciling" && !options.allowReconciling)) {
    throw new ManagedProviderError(`managed session binding is ${initial.transition.phase}`)
  }
  if (initial && !initial.transition && markerMatches(sessionID, initial)) return initial
  const initialToken = initial?.registrationMarker.token
  const initialTransition = initial?.transition ? { ...initial.transition } : undefined
  const readKey = admissionReadKey(sessionID)
  let pending = bindingReads.get(readKey)
  if (!pending) {
    pending = readManagedSessionBinding(sessionID)
    bindingReads.set(readKey, pending)
  }
  try {
    const authority = await pending
    const latest = managedSessionBinding(sessionID)
    if (authority.authority) {
      if (latest && (!latest.authority || latest.authority.generation !== authority.authority.generation || latest.owner !== authority.owner || latest.stableChatID !== authority.stableChatID))
        throw new ManagedProviderError("managed private session authority changed")
      if (latest) return latest
      installManagedSessionBinding(sessionID, { authority: authority.authority, owner: authority.owner,
        stableChatID: authority.stableChatID, engineSessionID: authority.engineSessionID, engineAliases: [],
        memoryWorkspaceID: authority.memoryWorkspaceID, authorityWorkspaceID: authority.authorityWorkspaceID,
        copalWorkspace: authority.copalWorkspace, physicalCwd: authority.canonicalCwd, memoryEnabled: false, transition: null })
      return managedSessionBinding(sessionID)
    }
    if (latest?.authority || initial?.authority) throw new ManagedProviderError("managed authority kind changed")
    if (latest?.transition?.phase === "in_flight" || (latest?.transition?.phase === "reconciling" && !options.allowReconciling)) {
      throw new ManagedProviderError("managed session binding changed during reconciliation")
    }
    const peerSettled = latest && latest.transition === null && latest.engineSessionID === authority.engineSessionID && latest.owner === authority.owner && latest.stableChatID === authority.stableChatID && latest.engineAliases.length === authority.engineAliases.length && latest.engineAliases.every((alias, index) => alias === authority.engineAliases[index]) && latest.memoryWorkspaceID === authority.memoryWorkspaceID && latest.authorityWorkspaceID === authority.authorityWorkspaceID && latest.copalWorkspace === authority.copalWorkspace && latest.physicalCwd === path.normalize(authority.canonicalCwd) && latest.bindingRevision === authority.workspaceRevision && latest.mapRevision === authority.mapRevision && latest.mappingRevision === authority.mappingRevision && latest.memoryEnabled === authority.memoryEnabled && markerMatches(sessionID, latest)
    if (!peerSettled && ((initialToken && latest?.registrationMarker.token !== initialToken) || (!initialToken && latest))) {
      throw new ManagedProviderError("managed session registration changed during reconciliation")
    }
    if (initialTransition) {
      if (!latest?.transition || latest.transition.transitionID !== initialTransition.transitionID || latest.transition.phase !== initialTransition.phase) {
        throw new ManagedProviderError("managed session transition changed during reconciliation")
      }
    } else if (latest?.transition) {
      throw new ManagedProviderError("managed session binding changed during reconciliation")
    }
    if (latest && (authority.owner !== latest.owner || authority.stableChatID !== latest.stableChatID || authority.workspaceRevision < latest.bindingRevision)) {
      throw new ManagedProviderError("managed session binding authority regressed during reconciliation")
    }
    if (peerSettled) {
      return latest
    }
    if (initial && latest && (initial.engineSessionID !== latest.engineSessionID || initial.owner !== latest.owner || initial.stableChatID !== latest.stableChatID || initial.mapRevision !== latest.mapRevision || initial.mappingRevision !== latest.mappingRevision || initial.bindingRevision !== latest.bindingRevision || initial.physicalCwd !== latest.physicalCwd)) {
      throw new ManagedProviderError("managed session binding generation changed during reconciliation")
    }
    if (initial && (authority.mapRevision < initial.mapRevision || authority.mappingRevision < initial.mappingRevision)) {
      throw new ManagedProviderError("managed session binding map evidence regressed during reconciliation")
    }
    const replacement = {
      owner: authority.owner,
      stableChatID: authority.stableChatID,
      engineSessionID: authority.engineSessionID,
      engineAliases: [...authority.engineAliases],
      memoryWorkspaceID: authority.memoryWorkspaceID,
      authorityWorkspaceID: authority.authorityWorkspaceID,
      copalWorkspace: authority.copalWorkspace,
      physicalCwd: path.normalize(authority.canonicalCwd),
      bindingRevision: authority.workspaceRevision,
      mapRevision: authority.mapRevision,
      mappingRevision: authority.mappingRevision,
      memoryEnabled: authority.memoryEnabled,
      transition: null,
    } as const
    if (latest) replaceManagedSessionBinding(sessionID, replacement, { expectedTransitionID: initialTransition?.transitionID ?? null, expectedWorkspaceRevision: initial?.bindingRevision ?? latest.bindingRevision })
    else installManagedSessionBinding(sessionID, replacement)
    const admitted = managedSessionBinding(sessionID)
    if (!admitted || !markerMatches(sessionID, admitted)) throw new ManagedProviderError("managed session binding admission did not settle")
    return admitted
  } finally {
    if (bindingReads.get(readKey) === pending) bindingReads.delete(readKey)
  }
}

export async function reconcileManagedSessionBinding(sessionID: string) {
  return ensureManagedSessionBinding(sessionID, { allowReconciling: true })
}

/** Generic tools may retry a quarantined binding, but never run while it is unsettled. */
export async function admitManagedSessionBinding(sessionID: string) {
  const binding = managedSessionBinding(sessionID)
  if (binding?.transition?.phase === "reconciling") {
    try {
      await reconcileManagedSessionBinding(sessionID)
    } catch {
      // Keep the transition quarantined; the next admission attempt retries.
    }
    throw new ManagedProviderError("managed session binding is reconciling")
  }
  return ensureManagedSessionBinding(sessionID)
}

export function resetManagedSessionAdmission(sessionID: string) {
  for (const key of [...bindingReads.keys()]) {
    if (key.startsWith(`${sessionID}::`)) bindingReads.delete(key)
  }
}

export function managedSessionOwner(sessionID: string, fallback = "") {
  if (!enabled()) return fallback
  const binding = managedSessionBinding(sessionID)
  if (!binding || binding.transition || !markerMatches(sessionID, binding)) {
    throw new ManagedProviderError("managed session binding is unavailable")
  }
  return binding.owner
}

export function invalidateManagedSessionBindingMarkerForTest(sessionID: string) {
  invalidateManagedSessionMarkerForTest(sessionID)
}

registerManagedSessionAdmissionReset(resetManagedSessionAdmission)

export function currentScope(sessionID: string, actorID?: string, executionRevision?: number): AccountSelection.Scope | undefined {
  const operation=activeOperation(sessionID, actorID, executionRevision)
  return operation?.qualifiedControlUnknown ? undefined : operation?.scope
}

/** Original observation scope of this exact operation, for a conservative no-retry fence only. */
export function originalRefreshObservationScope(sessionID: string, actorID?: string, executionRevision?: number): AccountSelection.Scope | undefined {
  return activeOperation(sessionID, actorID, executionRevision)?.malformedObservationScope
}

/** Resolve only the exact accepted refresh's original capture for a success marker. */
export function successObservationScope(sessionID: string, actorID?: string, executionRevision?: number): AccountSelection.Scope | undefined {
  const operation=activeOperation(sessionID,actorID,executionRevision)
  const candidate=operation?.refreshContinuity
  if (candidate && (refreshContinuityCurrent(candidate) || (operation?.successCommit?.refresh===candidate && refreshedCommitCurrent(candidate,operation)))) return candidate.oldScope
  return operation?.scope
}

/** Optional diagnostic lineage, never authority to retry or send. */
export function committedRefreshDiagnostic(sessionID: string, actorID?: string, executionRevision?: number) {
  const operation=activeOperation(sessionID,actorID,executionRevision), proof=operation?.successCommit, refresh=proof?.refresh
  if (!operation || !proof || !refresh || !refreshedCommitCurrent(refresh,operation)) return undefined
  const capture=refreshCaptureIdentity(refresh.oldScope,refresh.before,refresh.host)
  if (!capture || capture.registration!==proof.registration || capture.admissionID!==proof.admissionID || capture.holderID!==proof.holderID) return undefined
  return {previousCredentialRevision:refresh.before.credentialRevision!,credentialRevision:operation.binding.credentialRevision!}
}

/** Nonsecret evidence from the already admitted operation; never returns credentials. */
export function currentBindingEvidence(sessionID: string, actorID?: string, executionRevision?: number) {
  const binding = activeOperation(sessionID, actorID, executionRevision)?.binding
  if (!binding) return undefined
  return { bindingID:binding.bindingID, bindingRevision:binding.bindingRevision, committed:binding.committed,
    connectionID:binding.connectionID, providerID:binding.providerID, modelID:binding.modelID,
    accountID:binding.accountID ?? null, credentialRevision:binding.credentialRevision ?? null }
}

/** Content-free route identity for the original optional tool-view admission. */
export function currentToolViewRoute(sessionID:string, actorID:string, executionRevision:number) {
  const operation=activeOperation(sessionID,actorID,executionRevision)
  const binding=operation?.binding
  if (!binding || operation?.timingHost!==connection()) return
  return {rootOperationID:binding.rootOperationID, connectionID:binding.connectionID,
    providerID:binding.providerID, modelID:binding.modelID, modelRouteID:binding.modelRouteID}
}

export function requireScope(sessionID: string, actorID?: string, executionRevision?: number): AccountSelection.Scope {
  const scope = currentScope(sessionID, actorID, executionRevision)
  if (!scope) {
    throw new ManagedProviderError("No managed provider lease is active for this operation")
  }
  return scope
}

function sameMethodPolicy(left: OpenClankManagedProtocol.ProviderMethodPolicy | undefined, right: OpenClankManagedProtocol.ProviderMethodPolicy | undefined): boolean {
  if (!left || !right) return left === right
  return left.version === right.version && left.methodID === right.methodID &&
    left.credentialOwner === right.credentialOwner && left.policyRevision === right.policyRevision
}

function validateRouteContext(value: unknown, providerID: string, modelID: string): RouteContext {
  const route = decode(RouteContext, value, "route context")
  if (!route.rootOperationID || !route.connectionID || !route.providerID || !route.modelID) {
    throw new ManagedProviderError("Managed provider route context is incomplete")
  }
  if (route.providerID === "anthropic" && route.billingLane === "subscription" &&
      (!route.methodPolicy || !route.methodPolicy.credentialOwner.trim() ||
       !Number.isSafeInteger(route.methodPolicy.policyRevision) || route.methodPolicy.policyRevision < 1)) {
    throw new ManagedProviderError("Claude subscription method policy proof is unavailable")
  }
  // A connection ID is the preferred collision-free runtime provider ID.  An
  // official family ID remains accepted only because the host already proved
  // that the family/model pair resolved to exactly one connection.
  if (providerID !== route.connectionID && providerID !== route.providerID) {
    throw new ManagedProviderError("Managed provider route does not match the selected provider")
  }
  if (modelID !== route.modelID) {
    throw new ManagedProviderError("Managed provider route does not match the selected model")
  }
  return route
}

function sameActorOptions(a: OpenClankManagedProtocol.ActorOptionSnapshot | undefined, b: OpenClankManagedProtocol.ActorOptionSnapshot | undefined): boolean {
  if (!a || !b) return a === b
  const source = (v: OpenClankManagedProtocol.ActorOptionSource) => [v.kind, v.requestID, v.sessionID, v.actorID, v.executionRevision, v.rootOperationID, v.messageID]
  return a.version === b.version && a.routeID === b.routeID && a.optionRouteID === b.optionRouteID &&
    a.catalogRevision === b.catalogRevision && a.model.providerID === b.model.providerID && a.model.modelID === b.model.modelID &&
    a.options.reasoning_effort === b.options.reasoning_effort && JSON.stringify(source(a.source)) === JSON.stringify(source(b.source))
}

export async function preflightActorRoute(input: OpenClankManagedProtocol.ActorRoutePreflightRequest & { preferredAccountID?: string }): Promise<OpenClankManagedProtocol.ActorRoutePreflightResult & { inheritedAccountID?: string; preferredAccountID?: string }> {
  const { preferredAccountID, ...request } = input
  const route = await OpenClankManagedProtocol.callHost(connection(), "_openclank/provider-store/v1/actor-route/preflight", request)
  if (!sameActorOptions(route.modelRequestOptions, route.bootstrap.modelRequestOptions) ||
      route.modelID !== input.model.modelID || route.bootstrap.sessionID !== input.sessionID ||
      route.bootstrap.actorID !== input.actorID || route.bootstrap.rootOperationID !== input.rootOperationID ||
      route.bootstrap.expectedExecutionRevision !== input.expectedExecutionRevision || route.bootstrap.modelRouteID !== route.modelRouteID)
    throw new ManagedProviderError("Managed actor preflight identity changed")
  return { ...route, ...(input.inheritedAccountID ? { inheritedAccountID: input.inheritedAccountID } : {}), ...(preferredAccountID ? { preferredAccountID } : {}) }
}

export async function activateActorOperation(
  identity: { sessionID: string; actorID: string; executionRevision: number; rootOperationID?: string },
  preflight: OpenClankManagedProtocol.ActorRoutePreflightResult & { inheritedAccountID?: string; preferredAccountID?: string },
  model: { providerID: string; modelID: string },
  hostContext: ManagedHostContext,
): Promise<{ scope: AccountSelection.Scope; release: () => Promise<void> }> {
  const bootstrap = preflight.bootstrap
  if (identity.sessionID !== bootstrap.sessionID || identity.actorID !== bootstrap.actorID ||
      identity.rootOperationID !== bootstrap.rootOperationID || identity.executionRevision !== bootstrap.expectedExecutionRevision + 1)
    throw new ManagedProviderError("Managed actor activation identity changed")
  if (hostContext.chatID && hostContext.chatID !== bootstrap.stableChatID)
    throw new ManagedProviderError("Managed actor host context changed")
  const key = operationKey(identity.sessionID, identity.actorID, identity.executionRevision)
  if (operations.has(key)) throw new ManagedProviderError("A managed provider actor operation is already active")
  operations.set(key, operationAcquiring)
  const route = { ...preflight, rootOperationID: bootstrap.rootOperationID }
  let bound: BoundOperation | PendingBoundOperation
  const timingHost=connection()
  try {
    bound = await beginBoundOperation(route, model, { sessionID: identity.sessionID, actorID: identity.actorID, executionRevision: identity.executionRevision, bootstrap })
  } catch (error) {
    if (operations.get(key) === operationAcquiring) operations.delete(key)
    throw error
  }
  const operation: ActiveOperation | PendingOperation = 'pending' in bound
    ? {state:'pending',binding:bound.binding,routeValue:route,timingHost,timingIssuer:connection()===timingHost ? toolTimingIssuerFor(timingHost) : undefined,hostContext:{...hostContext,chatID:bootstrap.stableChatID}}
    : {...bound, routeValue: route, transientRetries: 0, retryWaitMs: 0, timingHost, timingIssuer: connection()===timingHost ? toolTimingIssuerFor(timingHost) : undefined, hostContext: { ...hostContext, chatID: bootstrap.stableChatID } }
  if (operations.get(key) !== operationAcquiring || connection() !== timingHost) throw new ManagedProviderError('Managed actor operation changed during binding')
  operations.set(key, operation)
  let released = false
  return { get scope() { const current=operations.get(key); if (!current || 'state' in current) throw new ManagedProviderError('Managed actor credential is unavailable'); return current.scope }, release: async () => {
    if (released) return
    released = true
    if ('state' in operation) operation.state='unknown'
    const prior=operations.get(key)
    if (prior && !('state' in prior) && prior.qualifiedAttempt && (prior===operation || ('state' in operation && prior===operation.leased))) { prior.rebuildRetired=true; if (prior.preparedAlternative) prior.preparedAlternative.phase='retired'; prior.ownedAttempt?.deny(); prior.qualifiedRetired=true }
    try { await flushCaptures() } finally {
      const current=operations.get(key)
      if (identity.actorID === "main" && current && !('state' in current) && !current.qualifiedControlUnknown) {
        // ACP normally consumes this immediately; bounded retention also covers failed consumers.
        if (finalCapture.size >= 64) finalCapture.delete(finalCapture.keys().next().value!)
        finalCapture.set(key, {
        sessionID: identity.sessionID, actorID: identity.actorID, rootOperationID: bootstrap.rootOperationID,
        executionRevision: identity.executionRevision,
        ...(current.alternativeCommitted && current.binding.committed && current.alternativeCommitted.alternativeBindingID === current.binding.bindingID &&
          current.alternativeCommitted.alternativeBindingRevision === current.binding.bindingRevision ? { alternativeExecutionProof: current.alternativeCommitted } : {}),
        ...(!current.preparedAlternative && preflight.modelRequestOptions ? { modelRequestOptions: preflight.modelRequestOptions } : {}),
        ...(!current.preparedAlternative && preflight.grantID ? { grantID: preflight.grantID } : {}),
        binding: {
          bindingID: current.binding.bindingID, bindingRevision: current.binding.bindingRevision,
          connectionID: current.binding.connectionID, providerID: current.binding.providerID,
          billingLane: current.binding.billingLane, modelID: current.binding.modelID,
          modelRouteID: current.binding.modelRouteID, accountID: current.binding.accountID,
          credentialRevision: current.binding.credentialRevision,
        },
        sdk: captureSdkEvidence(current.scope), scope: captureScopeEvidence(current.scope),
        })
      }
      if (current && !('state' in current) && !current.qualifiedControlUnknown && (current === operation || ('state' in operation && current === operation.leased))) { clearGcfSessionViews(identity.sessionID,identity.actorID); operations.delete(key) }
      else if (current === operation && 'state' in operation) operation.state='unknown'
    }
  } }
}

export function beginBoundOperation(routeValue:unknown,model:{providerID:string;modelID:string}):Promise<BoundOperation>
export function beginBoundOperation(routeValue:unknown,model:{providerID:string;modelID:string},identity:{sessionID:string;actorID:string;executionRevision:number;bootstrap:OpenClankManagedProtocol.ActorRoutePreflightResult['bootstrap']}):Promise<BoundOperation|PendingBoundOperation>
export async function beginBoundOperation(
  routeValue: unknown,
  model: { providerID: string; modelID: string },
  identity?: { sessionID: string; actorID: string; executionRevision: number; bootstrap: OpenClankManagedProtocol.ActorRoutePreflightResult["bootstrap"] },
): Promise<BoundOperation | PendingBoundOperation> {
  const route = validateRouteContext(routeValue, model.providerID, model.modelID)
  const bindRequest: AccountSelection.SelectionRequest = {
    rootOperationID: route.rootOperationID,
    ...(identity ? { scopeKind: "actor_execution" as const, ...identity, modelRouteID: route.modelRouteID! } : { scopeKind: "auxiliary_operation" as const, operationID: route.operationID!, modelRouteID: route.modelRouteID! }),
    connectionID: route.connectionID,
    providerID: route.providerID,
    billingLane: route.billingLane,
    modelID: route.modelID,
    ...(route.modelRequestOptions ? { modelRequestOptions: route.modelRequestOptions } : {}),
    ...(route.methodPolicy ? { methodPolicy: route.methodPolicy } : {}),
    ...(route.preferredAccountID ? { preferredAccountID: route.preferredAccountID } : {}),
    ...(route.inheritedAccountID ? { inheritedAccountID: route.inheritedAccountID } : {}),
    ...(route.grantID ? { grantID: route.grantID } : {}),
  }
  const rawBinding = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/bind",
    bindRequest,
  )
  const binding = decode(AccountSelection.Binding, rawBinding, "account binding")
  if (
    !sameMethodPolicy(binding.methodPolicy, route.methodPolicy) ||
    binding.rootOperationID !== route.rootOperationID ||
    binding.scopeKind !== (identity ? "actor_execution" : "auxiliary_operation") ||
    binding.modelRouteID !== route.modelRouteID ||
    (identity && (binding.sessionID !== identity.sessionID || binding.actorID !== identity.actorID || binding.executionRevision !== identity.executionRevision)) ||
    (!identity && binding.operationID !== route.operationID) ||
    binding.providerID !== route.providerID ||
    binding.connectionID !== route.connectionID ||
    binding.billingLane !== route.billingLane ||
    binding.modelID !== route.modelID
  ) {
    throw new ManagedProviderError("Managed provider binding escaped its requested route")
  }

  if (binding.pendingCrossModel) {
    if (!identity || identity.actorID !== 'main' || !binding.credentialRequired || !binding.accountID || !binding.credentialRevision)
      throw new ManagedProviderError('Managed pending binding is unavailable')
    return {pending:true,binding}
  }
  return leaseBinding(route, binding)
}

async function captureAdmission(route: RouteContext, binding: AccountSelection.Binding) {
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      OpenClankManagedProtocol.callHost(connection(), "_openclank/logging/v1/admit", {
        bindingID: binding.bindingID, rootOperationID: binding.rootOperationID,
        ...(route.operationID ? { operationID: route.operationID } : {}),
      }),
      new Promise<null>(resolve => { timer = setTimeout(() => resolve(null), 250) }),
    ])
  } catch {
    // Registration failure is a logging gap, never a provider retry.
    return null
  } finally {
    if (timer) clearTimeout(timer)
  }
}

export async function leaseBinding(
  routeValue: unknown,
  binding: AccountSelection.Binding,
  qualification?: OpenClankManagedProtocol.CredentialLeaseRequest['crossModelAdmission'],
  current?: () => boolean,
): Promise<BoundOperation> {
  const route = validateRouteContext(routeValue, binding.providerID, binding.modelID)
  if (!!binding.pendingCrossModel !== !!qualification || (qualification && !current))
    throw new ManagedProviderError('Managed pending credential requires exact turn admission')
  if (
    binding.rootOperationID !== route.rootOperationID ||
    binding.connectionID !== route.connectionID ||
    binding.billingLane !== route.billingLane ||
    binding.modelID !== route.modelID
  ) {
    throw new ManagedProviderError("Managed provider binding escaped its requested route")
  }
  if (!binding.credentialRequired) {
    if (
      binding.source !== "keyless" ||
      binding.accountID !== undefined ||
      binding.credentialRevision !== undefined ||
      binding.billingLane !== "local"
    ) {
      throw new ManagedProviderError("Managed keyless binding is internally inconsistent")
    }
    return {
      binding,
      scope: installCaptureScope({
        connectionID: binding.connectionID,
        billingLane: binding.billingLane,
        credentialRequired: false,
      }, binding, connection(), await captureAdmission(route, binding)),
    }
  }
  if (!binding.accountID || binding.credentialRevision === undefined) {
    throw new ManagedProviderError("Managed credential binding is incomplete")
  }

  const rawLease = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/credential/lease",
    {
      bindingID: binding.bindingID,
      expectedBindingRevision: binding.bindingRevision,
      expectedCredentialRevision: binding.credentialRevision,
      ...(qualification ? {crossModelAdmission:qualification} : {}),
    },
  )
  if ('version' in rawLease) throw new ManagedProviderError('Unexpected alternative credential result')
  const lease = decode(CredentialLease, rawLease, "credential lease")
  if (qualification && (!rawLease || rawLease.bindingID!==binding.bindingID || rawLease.scopeKind!=='actor_execution' ||
      rawLease.rootOperationID!==qualification.turn.rootOperationID || rawLease.sessionID!==qualification.turn.sessionID ||
      rawLease.actorID!==qualification.turn.actorID || rawLease.executionRevision!==qualification.turn.executionRevision ||
      rawLease.modelRouteID!==binding.modelRouteID))
    throw new ManagedProviderError('Managed provider credential identity changed')
  if (
    !sameMethodPolicy(lease.methodPolicy, binding.methodPolicy) ||
    lease.connectionID !== binding.connectionID ||
    lease.accountID !== binding.accountID ||
    lease.credentialRevision !== binding.credentialRevision ||
    lease.expiresAt <= Date.now() ||
    (qualification ? !lease.crossModelAttempt || !/^[0-9a-f]{64}$/.test(lease.crossModelAttempt.attemptID) ||
      lease.crossModelAttempt.coordinatorRevision !== qualification.expectedCoordinatorRevision + 1 : !!lease.crossModelAttempt) ||
    (current && !current())
  ) {
    throw new ManagedProviderError("Managed provider credential lease is stale or mismatched")
  }
  const capture = await captureAdmission(route, binding)
  if (current && !current()) throw new ManagedProviderError('Managed provider operation changed during lease')
  return {
    binding,
    ...(lease.crossModelAttempt ? {qualifiedAttempt:lease.crossModelAttempt} : {}),
    scope: installCaptureScope({
      connectionID: lease.connectionID,
      accountID: lease.accountID,
      billingLane: binding.billingLane,
      credentialRequired: true,
      credentialRevision: lease.credentialRevision,
      credential: lease.credential,
    }, binding, connection(), capture),
  }
}

type PersonaEvidence = import("../session/closed-source-provenance").LegacyPersonaEvidence | import("../session/closed-source-provenance").ComposedPersonaEvidence | import("../session/closed-source-provenance").StyledPersonaEvidence
type BrowserStartProof={operation:ActiveOperation;host:AgentSideConnection;scope:AccountSelection.Scope;turn:OpenClankManagedProtocol.ManagedTurnIdentity;admission:NonNullable<OpenClankManagedProtocol.ManagedTurnStartResult['browserSnapshotRepeatAdmission']>}
const browserSnapshotStarts=new WeakMap<object,BrowserStartProof>()
export function readBrowserSnapshotStart(result:OpenClankManagedProtocol.ManagedTurnStartResult,identity:ActorExecutionIdentity) {
  const proof=browserSnapshotStarts.get(result)
  if(!proof || !identity.rootOperationID || !identity.innerTurnID)return
  const turn=proof.turn, generation=admittedToolTimingGeneration(identity,turn)
  if(!generation)return
  const current=()=>{try {
    const op=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision), route=currentToolViewRoute(turn.sessionID,turn.actorID,turn.executionRevision)
    const admission=proof.admission, issuer=currentActorToolTimingIssuer(turn.sessionID,turn.actorID,turn.executionRevision,turn.rootOperationID)
    return op===proof.operation && op?.scope===proof.scope && connection()===proof.host && op.timingHost===proof.host &&
      identity.sessionID===turn.sessionID && identity.actorID===turn.actorID && identity.executionRevision===turn.executionRevision &&
      identity.rootOperationID===turn.rootOperationID && identity.innerTurnID===turn.innerTurnID && issuer?.isAlive()===true &&
      admittedToolTimingGeneration(identity,turn)===generation && op.hostContext?.ownerAccountID===admission.hostAccountID &&
      !!route && route.rootOperationID===turn.rootOperationID && route.connectionID===admission.connectionID && route.providerID===admission.providerID &&
      route.modelID===admission.modelID && route.modelRouteID===admission.modelRouteID
  }catch{return false}}
  return current() ? Object.freeze({admission:proof.admission,current}) : undefined
}
const factoryPersonaStarts = new WeakMap<object,{operation:object;host:AgentSideConnection;scope:object;turn:OpenClankManagedProtocol.ManagedTurnIdentity;evidence:PersonaEvidence;owner?:string;chat?:string}>()
/** Read a factory component only from the original authenticated, live turn. */
export function readFactoryPersonaStart(result:OpenClankManagedProtocol.ManagedTurnStartResult, identity:ActorExecutionIdentity, text:string):PersonaEvidence | undefined {
  const proof=factoryPersonaStarts.get(result)
  if (!proof || !identity.rootOperationID || !identity.innerTurnID || typeof text!=="string" || Buffer.byteLength(text,"utf8")>16384) return
  const turn=proof.turn, operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision)
  const issuer=currentActorToolTimingIssuer(turn.sessionID,turn.actorID,turn.executionRevision,turn.rootOperationID)
  if (!operation || operation!==proof.operation || operation.scope!==proof.scope || operation.binding.rootOperationID!==turn.rootOperationID || connection()!==proof.host || operation.timingHost!==proof.host ||
      identity.sessionID!==turn.sessionID || identity.actorID!==turn.actorID || identity.executionRevision!==turn.executionRevision ||
      identity.rootOperationID!==turn.rootOperationID || identity.innerTurnID!==turn.innerTurnID || !issuer?.isAlive() ||
      !isAdmittedToolTimingTurn(identity,turn) || createHash("sha256").update(text,"utf8").digest("hex")!==proof.evidence.textSHA256 ||
      (proof.evidence.constructor !== "host.factory-persona.v1" && (!proof.owner || !proof.chat || operation.hostContext?.ownerAccountID!==proof.owner || operation.hostContext?.chatID!==proof.chat))) return
  return proof.evidence
}

function sameIngressTurn(a: OpenClankManagedProtocol.ManagedTurnIdentity, b: OpenClankManagedProtocol.ManagedTurnIdentity): boolean {
  return a.sessionID === b.sessionID && a.rootOperationID === b.rootOperationID && a.actorID === b.actorID &&
    a.executionRevision === b.executionRevision && a.innerTurnID === b.innerTurnID
}

/** Current exact Host turn proof, checked immediately before the managed model send. */
export async function admitOriginalIngress(identity: OpenClankManagedProtocol.ManagedTurnIdentity, scope: AccountSelection.Scope, mandatoryHost = false): Promise<boolean> {
  const operation = activeOperation(identity.sessionID, identity.actorID, identity.executionRevision)
  const proof = operation?.ingressTurn
  if (!operation || !proof) return false
  const sameTurn = () => activeOperation(identity.sessionID, identity.actorID, identity.executionRevision) === operation &&
    operation.ingressTurn === proof && proof.host === connection() && operation.timingHost === proof.host &&
    sameIngressTurn(proof.identity, identity)
  if (!sameTurn()) return false
  if (!proof.required && !mandatoryHost) return true
  const current = () => sameTurn() && proof.scope === scope && operation.scope === scope
  if (!current()) return false
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    const result = await Promise.race([
      OpenClankManagedProtocol.callHost(proof.host, "_openclank/session/v1/dispatch/admit", proof.identity),
      new Promise<undefined>((resolve) => { timer = setTimeout(() => resolve(undefined), 5000) }),
    ])
    return current() && !!result && Object.keys(result).length === 1 && result.state === "current"
  } catch { return false }
  finally { if (timer) clearTimeout(timer) }
}

/** Off unless this exact live root turn received the Host's positive offer. */
export function workerNamingEnabled(identity: import("@/actor/turn").ActorExecutionIdentity | undefined): boolean {
  if (!identity || identity.actorID!=="main" || !identity.rootOperationID || !identity.innerTurnID) return false
  const turn={sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,
    executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID}
  const operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision), ingress=operation?.ingressTurn
  return !!operation && !!ingress?.workerNamingEnabled && sameIngressTurn(ingress.identity,turn) &&
    ingress.host===connection() && ingress.scope===operation.scope && !!operation.timingIssuer?.isAlive() &&
    isAdmittedToolTimingTurn(identity,turn)
}

/** Capture only the current transport; parent-turn retirement is not transport retirement. */
export function captureWorkerNamingTransport(identity: import("@/actor/turn").ActorExecutionIdentity | undefined): (() => boolean) | undefined {
  if (!workerNamingEnabled(identity)) return
  const original=connection()
  return () => { try { return connection()===original } catch { return false } }
}

/** Consume a positive result from the owned start call, never a caller-supplied result. */
export async function startManagedActorTurn(identity:OpenClankManagedProtocol.ManagedTurnIdentity) {
  const turn={...identity}
  const originalHost=connection()
  try {
    const result=await managedHostCall('_openclank/session/v1/turn/start',turn)
    if (!result.accepted || typeof result.originalIngressRequired !== 'boolean' || result.sessionID!==turn.sessionID || result.rootOperationID!==turn.rootOperationID ||
        result.actorID!==turn.actorID || result.executionRevision!==turn.executionRevision || result.innerTurnID!==turn.innerTurnID)
      throw new ManagedProviderError('Managed actor turn admission changed')
    await activatePendingActorTurn(turn)
    const operation=activeOperation(turn.sessionID,turn.actorID,turn.executionRevision)
    if (!operation || operation.timingHost !== originalHost || connection() !== originalHost || !operation.scope ||
        operation.binding.rootOperationID !== turn.rootOperationID)
      throw new ManagedProviderError('Managed ingress operation unavailable')
    operation.ingressTurn = Object.freeze({identity:Object.freeze({...turn}), required:result.originalIngressRequired,
      workerNamingEnabled:result.workerNamingEnabled === true && !result.replayed && turn.actorID === "main", host:originalHost, scope:operation.scope})
    try {
      const admission=result.browserSnapshotRepeatAdmission
      if (operation && admission && admission.version===1 && turn.actorID==='main' &&
          ['sessionID','rootOperationID','actorID','executionRevision','innerTurnID'].every(k => (admission as any)[k]===(turn as any)[k]) &&
          admission.hostAccountID===operation.hostContext?.ownerAccountID && admission.holderID && /^[a-f0-9]{64}$/.test(admission.settingsRevision) &&
          operation.timingHost===originalHost && operation.scope && typeof admission.connectionID==='string' && typeof admission.providerID==='string' &&
          typeof admission.modelID==='string' && typeof admission.modelRouteID==='string')
        browserSnapshotStarts.set(result,{operation,host:originalHost,scope:operation.scope,turn:Object.freeze({...turn}),admission:Object.freeze({...admission})})
    } catch { /* optional admission unavailable */ }
    // Optional source evidence never alters turn admission on malformed input.
    try {
      const persona=result.factoryPersona
      let evidence:PersonaEvidence | undefined
      if (persona?.version===1 && persona.constructor==="host.factory-persona.v1" && /^[a-f0-9]{64}$/.test(persona.textSHA256))
        evidence=Object.freeze({constructor:"host.factory-persona.v1" as const,textSHA256:persona.textSHA256})
      else if (persona?.version===2 && persona.constructor==="host.factory-persona-general.v1" &&
          exactKeys(persona as unknown as Record<string,unknown>,["version","constructor","textSHA256","factorySHA256","defaultsSHA256","snapshotSHA256"]) &&
          [persona.textSHA256,persona.factorySHA256,persona.defaultsSHA256,persona.snapshotSHA256].every(value=>typeof value==="string" && /^[a-f0-9]{64}$/.test(value)))
        evidence=Object.freeze({...persona})
      const owner=operation?.hostContext?.ownerAccountID,chat=operation?.hostContext?.chatID
      if (persona?.version===3 && persona.constructor==="host.factory-persona-style.v1" && owner && chat &&
          exactKeys(persona as unknown as Record<string,unknown>,["version","constructor","textSHA256","factorySHA256","styleSHA256","styleMode","styleRevision","styleSnapshotSHA256","general"]) &&
          [persona.textSHA256,persona.factorySHA256,persona.styleSHA256,persona.styleSnapshotSHA256].every(value=>typeof value==="string" && /^[a-f0-9]{64}$/.test(value)) &&
          ["concise","brief","minimal"].includes(persona.styleMode) && Number.isSafeInteger(persona.styleRevision) && persona.styleRevision>=0 &&
          (persona.general===null || (typeof persona.general==="object" && exactKeys(persona.general as unknown as Record<string,unknown>,["defaultsSHA256","snapshotSHA256"]) &&
            [persona.general.defaultsSHA256,persona.general.snapshotSHA256].every(value=>typeof value==="string" && /^[a-f0-9]{64}$/.test(value)))) &&
          createHash("sha256").update(JSON.stringify({accountID:owner,chatID:chat,mode:persona.styleMode,revision:persona.styleRevision,rootOperationID:turn.rootOperationID,version:1}),"utf8").digest("hex")===persona.styleSnapshotSHA256)
        evidence=Object.freeze({...persona,general:persona.general===null ? null : Object.freeze({...persona.general})})
      if (!result.replayed && evidence && operation?.scope && operation.timingHost===originalHost && connection()===originalHost &&
          (evidence.constructor==="host.factory-persona.v1" || (!!owner && !!chat)))
        factoryPersonaStarts.set(result,{operation,host:originalHost,scope:operation.scope,turn:Object.freeze({...turn}),evidence,
          ...(evidence.constructor!=="host.factory-persona.v1" ? {owner,chat} : {})})
    } catch { /* optional source evidence unavailable */ }
    return result
  } catch (error) {
    const pending=operations.get(operationKey(turn.sessionID,turn.actorID,turn.executionRevision))
    if (pending && 'state' in pending && pending.state!=='acquiring') pending.state='unknown'
    throw error
  }
}

async function activatePendingActorTurn(turn:OpenClankManagedProtocol.ManagedTurnIdentity):Promise<void> {
  const key=operationKey(turn.sessionID,turn.actorID,turn.executionRevision)
  const pending=operations.get(key)
  if (!pending) throw new ManagedProviderError('Managed actor operation is unavailable')
  if (!('state' in pending)) {
    if (pending.qualifiedControlUnknown || pending.qualifiedRetired) throw new ManagedProviderError('Managed actor operation is unavailable')
    if (pending.qualifiedTurn && (Object.keys(turn) as (keyof typeof turn)[]).some(k=>pending.qualifiedTurn![k]!==turn[k]))
      throw new ManagedProviderError('Managed actor turn changed')
    return
  }
  if (pending.state !== 'pending' || turn.actorID !== 'main' || pending.binding.sessionID !== turn.sessionID ||
      pending.binding.rootOperationID !== turn.rootOperationID || pending.binding.actorID !== turn.actorID ||
      pending.binding.executionRevision !== turn.executionRevision || connection() !== pending.timingHost)
    throw new ManagedProviderError('Managed pending turn is unavailable')
  const frozen={...turn}
  pending.state='issuing'
  const current=()=>operations.get(key)===pending && pending.state==='issuing' && connection()===pending.timingHost
  try {
    const leased=await leaseBinding(pending.routeValue,pending.binding,
      {version:1,turn:frozen,expectedCoordinatorRevision:pending.binding.pendingCrossModel!.coordinatorRevision},current)
    if (!current() || !leased.qualifiedAttempt) throw new ManagedProviderError('Managed pending operation changed')
    const operation:ActiveOperation={...leased,qualifiedTurn:frozen,routeValue:pending.routeValue,transientRetries:0,retryWaitMs:0,
      timingHost:pending.timingHost,timingIssuer:pending.timingIssuer,hostContext:pending.hostContext}
    pending.leased=operation;operations.set(key,operation)
  } catch (error) {
    if (operations.get(key)===pending) pending.state='unknown'
    throw error
  }
}

/** Fresh host admission immediately before a Claude upstream send. */
export async function admitClaudeDispatch(
  scope: AccountSelection.Scope,
  identity: { sessionID: string; actorID: string; executionRevision: number } | undefined,
  current: { access: string; refresh: string },
): Promise<void> {
  const initial = scopeBinding(scope)
  if (!initial || initial.providerID !== "anthropic" || initial.billingLane !== "subscription" || !initial.methodPolicy)
    throw new ManagedProviderError("Claude subscription scope authority is unavailable")
  let binding = initial
  if (initial.scopeKind === "actor_execution") {
    if (!identity) throw new ManagedProviderError("Claude actor execution identity is unavailable")
    const operation = requireOperation(identity.sessionID, identity.actorID, identity.executionRevision)
    if (operation.binding.bindingID !== initial.bindingID || operation.binding.accountID !== initial.accountID ||
        !sameMethodPolicy(operation.binding.methodPolicy, initial.methodPolicy))
      throw new ManagedProviderError("Claude actor execution scope changed")
    binding = operation.binding
  } else if (identity || !initial.operationID) {
    throw new ManagedProviderError("Claude auxiliary operation authority is unavailable")
  }
  if (!binding.accountID || binding.credentialRevision === undefined || !current.access || !current.refresh)
    throw new ManagedProviderError("Claude subscription credential scope is unavailable")
  const lease = decode(CredentialLease, await OpenClankManagedProtocol.callHost(
    connection(), "_openclank/provider-store/v1/credential/lease", {
      bindingID: binding.bindingID, expectedBindingRevision: binding.bindingRevision,
      expectedCredentialRevision: binding.credentialRevision,
    }), "credential lease")
  if (!sameMethodPolicy(lease.methodPolicy, binding.methodPolicy) || lease.accountID !== binding.accountID ||
      lease.connectionID !== binding.connectionID || lease.credentialRevision !== binding.credentialRevision ||
      lease.expiresAt <= Date.now() || lease.credential.type !== "oauth" ||
      lease.credential.access !== current.access || lease.credential.refresh !== current.refresh)
    throw new ManagedProviderError("Claude subscription dispatch authority changed")
}

export async function beginOperation(
  routeValue: unknown,
  model: { providerID: string; modelID: string },
): Promise<AccountSelection.Scope> {
  return (await beginBoundOperation(routeValue, model)).scope
}

export async function commitOperation(binding: AccountSelection.Binding): Promise<AccountSelection.Binding> {
  const raw = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/commit",
    { bindingID: binding.bindingID, expectedRevision: binding.bindingRevision },
  )
  const committed = decode(AccountSelection.Binding, raw, "committed account binding")
  if (
    committed.bindingID !== binding.bindingID ||
    committed.connectionID !== binding.connectionID ||
    committed.billingLane !== binding.billingLane ||
    committed.modelID !== binding.modelID ||
    committed.accountID !== binding.accountID ||
    committed.committed !== true
  ) {
    throw new ManagedProviderError("Managed provider commitment escaped its account binding")
  }
  return committed
}

export async function recordAttempt(
  binding: AccountSelection.Binding,
  outcome: AccountSelection.AttemptOutcome,
  options: { retryAfterMs?: number; modelEligible?: boolean; evidence?: AccountSelection.AttemptEvidence; dispatchObservation?: AccountSelection.DispatchObservation; refreshContinuity?: OpenClankManagedProtocol.AccountAttemptRequest['refreshContinuity']; successContinuity?: OpenClankManagedProtocol.AccountAttemptRequest['successContinuity'] } = {},
): Promise<AccountSelection.Binding> {
  const raw = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/attempt",
    {
      bindingID: binding.bindingID,
      expectedRevision: binding.bindingRevision,
      ...(binding.accountID ? { accountID: binding.accountID } : {}),
      outcome,
      ...(options.evidence ? { evidence: options.evidence } : {}),
      ...(options.dispatchObservation ? { dispatchObservation: options.dispatchObservation } : {}),
      ...(options.refreshContinuity ? { refreshContinuity: options.refreshContinuity } : {}),
      ...(options.successContinuity ? { successContinuity: options.successContinuity } : {}),
      ...(options.retryAfterMs !== undefined ? { retryAfterMs: options.retryAfterMs } : {}),
      ...(options.modelEligible !== undefined ? { modelEligible: options.modelEligible } : {}),
    },
  )
  const next = decode(AccountSelection.Binding, raw, "account attempt result")
  if (
    next.bindingID !== binding.bindingID ||
    next.connectionID !== binding.connectionID ||
    next.billingLane !== binding.billingLane ||
    next.modelID !== binding.modelID
  ) {
    throw new ManagedProviderError("Managed provider attempt escaped its account binding")
  }
  return next
}

/** Keep leased credential material only for the duration of one server turn. */
export async function withOperation<A>(
  sessionID: string,
  routeValue: unknown,
  model: { providerID: string; modelID: string },
  run: () => Promise<A>,
): Promise<A> {
  if (!enabled()) return run()
  if (operations.has(sessionID)) {
    throw new ManagedProviderError("A managed provider operation is already active for this session")
  }
  const reservation = operationAcquiring
  operations.set(sessionID, reservation)
  let bound: BoundOperation
  try {
    bound = await beginBoundOperation(routeValue, model)
  } catch (error) {
    if (operations.get(sessionID) === reservation) operations.delete(sessionID)
    throw error
  }
  const operation: ActiveOperation = {
    ...bound,
    routeValue,
    transientRetries: 0, retryWaitMs: 0,
  }
  if (operations.get(sessionID) !== reservation) throw new ManagedProviderError("Managed provider operation reservation was lost")
  operations.set(sessionID, operation)
  try {
    return await run()
  } finally {
    await flushCaptures()
    if (operations.get(sessionID) === operation) operations.delete(sessionID)
  }
}

function requireOperation(sessionID: string, actorID?: string, executionRevision?: number): ActiveOperation {
  const operation = activeOperation(sessionID, actorID, executionRevision)
  if (!operation || operation.qualifiedControlUnknown) throw new ManagedProviderError("No managed provider operation is active")
  return operation
}

function classifyAttempt(error: unknown, providerReset?: AccountSelection.AttemptEvidence["providerReset"], codexWindows?: AccountSelection.AttemptEvidence["codexWindows"]): {
  outcome: AccountSelection.AttemptOutcome
  retryAfterMs?: number
  httpStatus?: number
  modelEligible?: boolean
  evidence: AccountSelection.AttemptEvidence
} {
  const failure = classifyFailure(error)
  const outcome: AccountSelection.AttemptOutcome = failure.kind === "exhausted" ? "quota" :
    failure.kind === "throttle" ? "transient" : failure.kind === "malformed_success" ? "unknown" : failure.kind
  const notBefore = failure.retryAfterMs === undefined ? undefined : Date.now() + failure.retryAfterMs
  const evidence: AccountSelection.AttemptEvidence = { version: 1, kind: failure.kind,
    ...(failure.status === undefined ? {} : { httpStatus: failure.status }),
    ...(failure.kind === "throttle" && failure.status === 429 && providerReset ? { providerReset } : {}),
    ...(failure.kind === "throttle" && failure.status === 429 && codexWindows ? { codexWindows } : {}),
    ...(notBefore !== undefined && notBefore <= 253_402_300_799_999 ? { localNotBefore: new Date(notBefore).toISOString() } : {}) }
  return { outcome, evidence, ...(failure.status === undefined ? {} : { httpStatus: failure.status }),
    ...(failure.retryAfterMs === undefined ? {} : { retryAfterMs: failure.retryAfterMs }),
    ...(failure.kind === "entitlement" ? { modelEligible: false } : {}) }
}

export type AttemptDecision = {
  httpStatus?: number
  outcome: AccountSelection.AttemptOutcome
  retry: boolean
  rotated: boolean
  retryAfterMs?: number
}

function qualifiedControl(operation:ActiveOperation) {
  const turn=operation.qualifiedTurn, attempt=operation.qualifiedAttempt, binding=operation.binding
  if (!turn || !attempt || connection()!==operation.timingHost || turn.actorID!=='main' ||
      turn.sessionID!==binding.sessionID || turn.rootOperationID!==binding.rootOperationID ||
      turn.actorID!==binding.actorID || turn.executionRevision!==binding.executionRevision ||
      !/^[0-9a-f]{64}$/.test(attempt.attemptID) || attempt.attemptGeneration!==1 ||
      !Number.isSafeInteger(attempt.coordinatorRevision) || attempt.coordinatorRevision<2)
    throw new ManagedProviderError('Managed qualified control is unavailable')
  return {version:1 as const,turn:{...turn},attemptID:attempt.attemptID,attemptGeneration:attempt.attemptGeneration,
    expectedCoordinatorRevision:attempt.coordinatorRevision}
}

async function controlQualified(operation:ActiveOperation, outcome?:AccountSelection.AttemptOutcome) {
  const before=operation.binding, scope=operation.scope, host=connection(), attempt=operation.qualifiedAttempt
  const control=qualifiedControl(operation), key=operationKey(control.turn.sessionID,control.turn.actorID,control.turn.executionRevision)
  operation.ownedAttempt?.deny()
  operation.qualifiedControlUnknown=true
  const base={bindingID:before.bindingID,expectedRevision:before.bindingRevision,crossModelControl:control}
  const raw=outcome===undefined
    ? await OpenClankManagedProtocol.callHost(host,'_openclank/provider-store/v1/account/commit',base)
    : await OpenClankManagedProtocol.callHost(host,'_openclank/provider-store/v1/account/attempt',
        {...base,outcome,...(before.accountID ? {accountID:before.accountID} : {})})
  const next=decode(AccountSelection.Binding,raw,'qualified account control')
  const updated=next.crossModelAttempt
  if (operations.get(key)!==operation ||
      connection()!==host || operation.qualifiedRetired || operation.scope!==scope || operation.binding!==before || operation.qualifiedAttempt!==attempt ||
      !updated || updated.attemptID!==control.attemptID || updated.attemptGeneration!==control.attemptGeneration ||
      updated.coordinatorRevision!==control.expectedCoordinatorRevision+1 || next.bindingRevision!==before.bindingRevision+1 ||
      next.bindingID!==before.bindingID || next.rootOperationID!==before.rootOperationID || next.connectionID!==before.connectionID ||
      next.scopeKind!==before.scopeKind || next.sessionID!==before.sessionID || next.actorID!==before.actorID || next.executionRevision!==before.executionRevision ||
      next.providerID!==before.providerID || next.billingLane!==before.billingLane || next.modelID!==before.modelID ||
      next.modelRouteID!==before.modelRouteID || next.accountID!==before.accountID || next.credentialRevision!==before.credentialRevision ||
      !sameMethodPolicy(next.methodPolicy,before.methodPolicy) ||
      (outcome===undefined ? next.committed!==true : updated.accounting!=='unavailable' || next.committed!==before.committed))
    throw new ManagedProviderError('Managed qualified control changed')
  operation.binding=next;operation.qualifiedAttempt=updated;operation.qualifiedControlUnknown=false
  return next
}

export async function commitSessionOperation(sessionID: string, actorID?: string, executionRevision?: number): Promise<void> {
  if (!enabled()) return
  const operation = requireOperation(sessionID, actorID, executionRevision)
  if (operation.binding.committed) return
  if (operation.preparedAlternative?.phase==='active') {
    const prepared=operation.preparedAlternative, receipt=prepared.receipt, turn=operation.qualifiedTurn, before=operation.binding, scope=operation.scope
    if (!turn || connection()!==prepared.host || prepared.binding!==before || prepared.scope!==scope) throw new ManagedProviderError('Alternative commitment unavailable')
    const identity:OpenClankManagedProtocol.CrossModelAlternativeIdentity={version:1,turn:{...turn},primaryBindingID:receipt.primaryBindingID,
      primaryBindingRevision:receipt.primaryBindingRevision,alternativeBindingID:receipt.alternativeBindingID,alternativeBindingRevision:1,
      attemptID:receipt.attemptID,attemptGeneration:2,expectedCoordinatorRevision:receipt.coordinatorRevision}
    operation.qualifiedControlUnknown=true
    const result=await OpenClankManagedProtocol.callHost(prepared.host,'_openclank/provider-store/v1/account/commit',{
      bindingID:receipt.primaryBindingID,expectedRevision:receipt.primaryBindingRevision,crossModelAlternativeCommit:identity})
    if (operations.get(operationKey(sessionID,actorID,executionRevision))!==operation || connection()!==prepared.host ||
        operation.preparedAlternative!==prepared || operation.scope!==scope || operation.binding!==before || !('state' in result) || result.state!=='committed' ||
        result.attemptID!==receipt.attemptID || result.attemptGeneration!==2 || result.primaryBindingID!==receipt.primaryBindingID ||
        result.primaryBindingRevision!==receipt.primaryBindingRevision || result.alternativeBindingID!==before.bindingID ||
        result.alternativeBindingRevision!==before.bindingRevision+1 || result.coordinatorRevision!==receipt.coordinatorRevision+1)
      throw new ManagedProviderError('Alternative commitment changed')
    operation.binding=decode(AccountSelection.Binding,{...before,bindingRevision:result.alternativeBindingRevision,committed:true},'alternative commitment')
    operation.alternativeCommitted=Object.freeze({version:1, sessionID:turn.sessionID, rootOperationID:turn.rootOperationID,
      actorID:'main', executionRevision:turn.executionRevision, innerTurnID:turn.innerTurnID,
      primaryBindingID:receipt.primaryBindingID, primaryBindingRevision:receipt.primaryBindingRevision,
      coordinatorRevision:result.coordinatorRevision, alternativeBindingID:result.alternativeBindingID,
      alternativeBindingRevision:result.alternativeBindingRevision, attemptID:receipt.attemptID, attemptGeneration:2,
      routeID:prepared.route.modelRouteID, connectionID:prepared.route.connectionID, providerID:prepared.route.providerID,
      modelID:prepared.route.modelID, billingLane:operation.binding.billingLane, catalogRevision:prepared.route.catalogRevision,
      options:Object.freeze({...prepared.route.options})})
    operation.qualifiedControlUnknown=false
    return
  }
  if (operation.qualifiedAttempt) { await controlQualified(operation); return }
  const before=operation.binding, scope=operation.scope, host=connection()
  let capture:ReturnType<typeof refreshCaptureIdentity>, refresh:RefreshContinuity | undefined
  try {
    const candidate=operation.refreshContinuity
    if (candidate && refreshContinuityCurrent(candidate)) refresh=candidate
    capture=before.scopeKind==='actor_execution' && before.providerID==='openai' && before.billingLane==='subscription' && before.accountID && Number.isSafeInteger(before.credentialRevision) && (before.credentialRevision ?? 0)>0
      ? refreshCaptureIdentity(refresh?.oldScope ?? scope,refresh?.before ?? before,host) : undefined
  } catch { capture=undefined; refresh=undefined }
  const binding = await commitOperation(operation.binding)
  operation.binding = binding
  try {
    if (capture && activeOperation(sessionID,actorID,executionRevision)===operation && connection()===host && operation.scope===scope &&
        binding.bindingRevision===before.bindingRevision+1 && binding.committed && binding.credentialRevision===before.credentialRevision &&
        refreshBindingMatches({...before,bindingRevision:binding.bindingRevision}, {...binding,committed:false}, before.credentialRevision!))
      operation.successCommit={before,scope,host,...capture,...(refresh ? {refresh} : {})}
  } catch {}
}

async function recordSessionOutcome(
  sessionID: string,
  classification: {
    outcome: AccountSelection.AttemptOutcome
    httpStatus?: number
    retryAfterMs?: number
    modelEligible?: boolean
    evidence?: AccountSelection.AttemptEvidence
    dispatchObservation?: AccountSelection.DispatchObservation
    refreshContinuity?: OpenClankManagedProtocol.AccountAttemptRequest['refreshContinuity']
    successContinuity?: OpenClankManagedProtocol.AccountAttemptRequest['successContinuity']
  },
  actorID?: string,
  executionRevision?: number,
): Promise<AttemptDecision> {
  const operation = requireOperation(sessionID, actorID, executionRevision)
  if (operation.preparedAlternative?.phase==='active') {
    operation.qualifiedControlUnknown=true;operation.qualifiedRetired=true;operation.preparedAlternative.phase='retired';operation.rebuildRetired=true
    return {outcome:classification.outcome,retry:false,rotated:false,
      ...(classification.httpStatus===undefined ? {} : {httpStatus:classification.httpStatus})}
  }
  if (operation.qualifiedAttempt) {
    await controlQualified(operation,classification.outcome === "invalid_output" ? "unknown" : classification.outcome)
    return {outcome:classification.outcome,retry:false,rotated:false,
      ...(classification.httpStatus===undefined ? {} : {httpStatus:classification.httpStatus})}
  }
  const previous = operation.binding
  const previousScope = operation.scope
  const next = await recordAttempt(previous, classification.outcome, classification)
  const rotated =
    previous.accountID !== next.accountID || previous.credentialRevision !== next.credentialRevision
  if (rotated) {
    const leased = await leaseBinding(operation.routeValue, next)
    operation.binding = leased.binding
    operation.scope = leased.scope
    operation.transientRetries = 0
  } else {
    operation.binding = next
  }
  if (classification.outcome === "transient" && !next.committed && !rotated) {
    operation.transientRetries += 1
  }
  const retry =
    !next.committed &&
    ((rotated && ["auth", "quota", "entitlement"].includes(classification.outcome)) ||
      (classification.outcome === "transient" && operation.transientRetries <= 2))
  if (retry) markCaptureRetry(operation.scope, previousScope)
  return {
    outcome: classification.outcome,
    retry,
    rotated,
    ...(classification.httpStatus === undefined ? {} : { httpStatus: classification.httpStatus }),
    ...(classification.retryAfterMs === undefined ? {} : { retryAfterMs: classification.retryAfterMs }),
  }
}

/** Reserve a cumulative local interactive wait without shortening provider hints. */
export function reserveRetryWait(sessionID: string, actorID: string | undefined, revision: number | undefined, waitMs: number): boolean {
  const operation = requireOperation(sessionID, actorID, revision)
  if (operation.qualifiedAttempt) return false
  if (!Number.isFinite(waitMs) || waitMs < 0 || operation.retryWaitMs + waitMs > RETRY_MAX_DELAY_NO_HEADERS) return false
  operation.retryWaitMs += waitMs
  return true
}

export async function recordSessionAttempt(sessionID: string, error: unknown, actorID?: string, executionRevision?: number): Promise<AttemptDecision> {
  if (!enabled()) return { outcome: "unknown", retry: false, rotated: false }
  const operation = requireOperation(sessionID, actorID, executionRevision)
  const continuity=operation.refreshContinuity
  operation.refreshContinuity=undefined
  const classification=classifyAttempt(error, takeSdkFailureReset(error, operation.scope), takeSdkCodexWindows(error, operation.scope))
  const valid=!!continuity && refreshContinuityCurrent(continuity)
  const observed=takeSdkFailureDispatch(error, valid ? continuity!.oldScope : operation.scope)
  return recordSessionOutcome(sessionID, {...classification,...(observed && classification.httpStatus===observed.httpStatus
    && classification.evidence.kind!=='malformed_success' ? {dispatchObservation:observed,
      ...(valid ? {refreshContinuity:{version:1 as const,admissionID:continuity!.admissionID,
        previousCredentialRevision:continuity!.before.credentialRevision!,credentialRevision:operation.binding.credentialRevision!}} : {})} : {})}, actorID, executionRevision)
}

export async function recordSessionCompletion(sessionID: string, outcome: "success" | "invalid_output" | "unknown", actorID?: string, executionRevision?: number, observation?:AccountSelection.DispatchObservation): Promise<void> {
  if (!enabled()) return
  const operation=requireOperation(sessionID,actorID,executionRevision), proof=operation.successCommit
  if (operation.preparedAlternative?.phase==='active') {
    if (!operation.binding.committed) throw new ManagedProviderError('Alternative success is not committed')
    return
  }
  operation.successCommit=undefined
  let qualified=false
  try {
    const capture=proof && refreshCaptureIdentity(proof.refresh?.oldScope ?? proof.scope,proof.refresh?.before ?? proof.before,proof.host)
    qualified=!!proof && (!proof.refresh || refreshedCommitCurrent(proof.refresh,operation)) && !!capture && capture.registration===proof.registration && capture.admissionID===proof.admissionID && capture.holderID===proof.holderID &&
      connection()===proof.host && operation.scope===proof.scope && operation.binding.committed && operation.binding.bindingRevision===proof.before.bindingRevision+1 &&
      operation.binding.credentialRevision===proof.before.credentialRevision && observation?.profile==='openai.codex.responses.v1' && observation.httpStatus>=200 && observation.httpStatus<=299 &&
      refreshBindingMatches({...proof.before,bindingRevision:operation.binding.bindingRevision},{...operation.binding,committed:false},proof.before.credentialRevision!)
  } catch {}
  await recordSessionOutcome(sessionID, { outcome, ...(outcome !== "unknown" && qualified && proof && observation ? {dispatchObservation:observation,successContinuity:proof.refresh ? {version:2,admissionID:proof.admissionID,previousCredentialRevision:proof.refresh.before.credentialRevision!,credentialRevision:proof.before.credentialRevision!} : {version:1,admissionID:proof.admissionID,credentialRevision:proof.before.credentialRevision!}} : {}) }, actorID, executionRevision)
}

export async function replaceCredential(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Info
}): Promise<Auth.Account> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/credential/replace",
    input,
  )
  return decode(Auth.Account, result, "credential replacement")
}

export async function acquireRefresh(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  ttlMs?: number
  bindingID?: string
  methodPolicy?: OpenClankManagedProtocol.ProviderMethodPolicy
}, host?: AgentSideConnection): Promise<Auth.RefreshLease> {
  if (input.bindingID && [...operations.values()].some(operation => !('state' in operation) &&
      operation.preparedAlternative?.phase === 'active' && operation.binding.bindingID === input.bindingID))
    throw new ManagedProviderError('Alternative credential refresh is unavailable')
  const result = await OpenClankManagedProtocol.callHost(
    host ?? connection(),
    "_openclank/provider-store/v1/refresh/acquire",
    input,
  )
  const lease = decode(RefreshLease, result, "refresh lease")
  if (!sameMethodPolicy(lease.methodPolicy, input.methodPolicy)) throw new ManagedProviderError("Refresh policy proof changed")
  return lease
}

export async function renewRefresh(input: { leaseID: string; ttlMs?: number; bindingID?: string; methodPolicy?: OpenClankManagedProtocol.ProviderMethodPolicy }): Promise<Auth.RefreshLease> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/refresh/renew",
    input,
  )
  const lease = decode(RefreshLease, result, "renewed refresh lease")
  if (!sameMethodPolicy(lease.methodPolicy, input.methodPolicy)) throw new ManagedProviderError("Refresh policy proof changed")
  return lease
}

export async function commitRefresh(input: {
  leaseID: string
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Info
  bindingID?: string
  methodPolicy?: OpenClankManagedProtocol.ProviderMethodPolicy
}, host?: AgentSideConnection): Promise<Auth.Account> {
  const result = await OpenClankManagedProtocol.callHost(
    host ?? connection(),
    "_openclank/provider-store/v1/refresh/commit",
    input,
  )
  const account = decode(Auth.Account, result, "refresh commit") as Auth.Account
  if (
    account.id !== input.accountID ||
    account.credentialRevision !== input.expectedRevision + 1
  ) {
    throw new ManagedProviderError("Managed provider refresh commit escaped its credential CAS")
  }
  // Refresh happens during an active attempt.  Keep every in-process scope
  // for this exact account/revision aligned with the host's atomic binding
  // advance; unrelated connections/accounts and already-newer scopes are
  // untouched.
  for (const operation of operations.values()) {
    if ("state" in operation) continue
    if (
      operation.binding.connectionID !== input.connectionID ||
      operation.binding.accountID !== input.accountID ||
      operation.binding.credentialRevision !== input.expectedRevision ||
      (operation.binding.providerID === "anthropic" && operation.binding.billingLane === "subscription" && operation.binding.bindingID !== input.bindingID)
    ) {
      continue
    }
    const previous = operation.binding
    const next = { ...previous, credentialRevision: account.credentialRevision }
    const nextScope = {
      connectionID: input.connectionID,
      accountID: input.accountID,
      billingLane: next.billingLane,
      credentialRequired: true,
      credentialRevision: account.credentialRevision,
      credential: account.credential,
    }
    operation.scope = previous.providerID === "anthropic" && previous.billingLane === "subscription"
      ? replaceCapturedScope(operation.scope, nextScope, previous, next)
      : nextScope
    operation.binding = next
  }
  return account
}

export async function abortRefresh(input: { leaseID: string }, host?: AgentSideConnection): Promise<void> {
  const result = await OpenClankManagedProtocol.callHost(
    host ?? connection(),
    "_openclank/provider-store/v1/refresh/abort",
    input,
  )
  if (typeof result !== "object" || result === null || Object.keys(result).length !== 0) {
    throw new ManagedProviderError("Managed provider host returned an invalid refresh abort")
  }
}

export class ManagedRefreshPersistenceError extends Error {
  readonly status = 401
  readonly statusCode = 401
  readonly code = "OPENCLANK_REFRESH_PERSISTENCE_FAILED"
}

/**
 * Run one OAuth refresh under the host's cross-process lease and credential
 * revision CAS.  The provider-specific token exchange is injected so this
 * authority remains independent of any adapter.  A token that rotated
 * upstream but could not be durably committed is surfaced as an auth failure;
 * the normal pre-commit attempt path then marks the account reauth-required
 * and may select another account without ever resurrecting the stale token.
 */
export async function refreshOAuthCredential(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Oauth
  bindingID?: string
  methodPolicy?: OpenClankManagedProtocol.ProviderMethodPolicy
  exchange: (credential: Auth.Oauth) => Promise<Auth.Oauth>
  /** Existing initiating scope; optional observation only, never sent to host. */
  captureScope?: AccountSelection.Scope
  auxiliary?: AuxiliaryClaudeOwner
}): Promise<{ credential: Auth.Oauth; credentialRevision: number }> {
  if ([...operations.values()].some(operation => !('state' in operation) && operation.preparedAlternative?.phase === 'active' &&
      (operation.scope === input.captureScope || (!!input.bindingID && operation.binding.bindingID === input.bindingID))))
    throw new ManagedProviderError('Alternative credential refresh is unavailable')
  let frozen:ReturnType<typeof freezeRefreshContinuity>
  try { frozen=freezeRefreshContinuity(input.captureScope) } catch { frozen=undefined }
  input.auxiliary?.assertCurrent()
  const lease = await acquireRefresh({
    connectionID: input.connectionID,
    accountID: input.accountID,
    expectedRevision: input.expectedRevision,
    ...(input.bindingID ? { bindingID: input.bindingID, methodPolicy: input.methodPolicy } : {}),
  }, input.auxiliary?.host)
  let exchanged = false
  try {
    input.auxiliary?.assertCurrent()
    const credential = await input.exchange(input.credential)
    exchanged = true
    input.auxiliary?.assertCurrent()
    if (
      credential?.type !== "oauth" ||
      !credential.access ||
      !credential.refresh ||
      !Number.isFinite(credential.expires)
    ) {
      throw new ManagedProviderError("Provider returned an invalid OAuth refresh result")
    }
    input.auxiliary?.assertCurrent()
    const account = await commitRefresh({
      leaseID: lease.leaseID,
      connectionID: input.connectionID,
      accountID: input.accountID,
      expectedRevision: input.expectedRevision,
      credential,
      ...(input.bindingID ? { bindingID: input.bindingID, methodPolicy: input.methodPolicy } : {}),
    }, input.auxiliary?.host)
    input.auxiliary?.assertCurrent()
    if (account.credential.type !== "oauth") {
      throw new ManagedRefreshPersistenceError(
        "Refreshed provider credential changed authentication class",
      )
    }
    if (frozen && account.id===frozen.before.accountID && account.credentialRevision===(frozen.before.credentialRevision ?? -2)+1) {
      const candidate={...frozen,nextScope:frozen.operation.scope}
      if (refreshContinuityCurrent(candidate)) frozen.operation.refreshContinuity=candidate
    }
    return {
      credential: account.credential,
      credentialRevision: account.credentialRevision,
    }
  } catch (error) {
    try {
      await abortRefresh({ leaseID: lease.leaseID }, input.auxiliary?.host)
    } catch {}
    if (exchanged) {
      if (error instanceof ManagedRefreshPersistenceError) throw error
      throw new ManagedRefreshPersistenceError(
        "Provider token rotated but durable credential commit failed",
      )
    }
    throw error
  }
}

/** Test-only process-state reset; production never swaps a live ACP host. */
export function resetForTest(): void {
  if (host) freshTaskCalls.delete(host)
  host = undefined
  operationDelegate = undefined
  operations.clear()
  hostContexts.clear()
  bindingReads.clear()
  resetManagedSessionScopesForTest()
}

/** Canonical history is host-owned in managed mode; no SQLite fallback. */
export async function historyCall(sessionID: string, operation: string, input: Record<string, unknown>) {
  if (!enabled()) throw new ManagedProviderError("managed history is unavailable outside managed mode")
  if (!sessionID) throw new ManagedProviderError("managed history requires a bound session")
  await ensureManagedSessionBinding(sessionID)
  const raw = await OpenClankManagedProtocol.callHost(connection(), "_openclank/history/v1/query", {
    sessionID,
    operation,
    ...input,
  } as OpenClankManagedProtocol.HistoryQueryRequest)
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new ManagedProviderError("managed history host returned invalid result")
  const value = raw as OpenClankManagedProtocol.HistoryQueryResult
  if (value.operation !== operation || typeof value.ok !== "boolean" || !value.result || typeof value.result !== "object" || Array.isArray(value.result)) {
    throw new ManagedProviderError("managed history host returned invalid result")
  }
  return value.result
}

/** Positive acknowledgement only; the ordinary history API keeps its legacy result shape. */
export async function historySourcesCurrent(input: Extract<OpenClankManagedProtocol.HistoryQueryRequest,{operation:'check_sources'}>) {
  if (!enabled() || !input.sessionID) return false
  await ensureManagedSessionBinding(input.sessionID)
  const result=await OpenClankManagedProtocol.callHost(connection(), '_openclank/history/v1/query', input)
  return !!result && result.ok===true && result.operation==='check_sources' && result.result.state==='ready' &&
    JSON.stringify(result.result.sources)===JSON.stringify(input.sources)
}

/** Forward a bounded canonical event to the host archive; no local history authority exists in managed mode. */
export async function historyMutate(
  sessionID: string,
  operation: "upsert" | "replay" | "tombstone",
  events: OpenClankManagedProtocol.HistoryMutationEvent[] | OpenClankManagedProtocol.HistoryTombstoneEvent[],
) {
  if (!enabled()) throw new ManagedProviderError("managed history is unavailable outside managed mode")
  if (!sessionID) throw new ManagedProviderError("managed history requires a bound session")
  await ensureManagedSessionBinding(sessionID)
  const raw = await OpenClankManagedProtocol.callHost(connection(), "_openclank/history/v1/mutate", {
    sessionID,
    operation,
    events,
  } as OpenClankManagedProtocol.HistoryMutationRequest)
  if (raw.operation !== operation || raw.ok !== true || !Number.isSafeInteger(raw.accepted) || !Number.isSafeInteger(raw.duplicate) || !Number.isSafeInteger(raw.enqueued)) {
    throw new ManagedProviderError("managed history host returned invalid mutation result")
  }
  return raw
}

export * as ManagedProvider from "./managed-provider"
