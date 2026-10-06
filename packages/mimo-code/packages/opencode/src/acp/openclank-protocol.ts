import type { Auth } from "@/auth"
import type { AccountSelection } from "@/provider/account-selection"

import {
  OPERATION_METHODS,
  OPERATIONS,
  PROVIDER_CONTROL_METHODS,
  PROVIDER_STORE_METHODS,
  SESSION_METHODS,
  LOGGING_METHODS,
  OPERATION_ROUTER_VERSION,
  PROTOCOL_VERSION,
  PROVIDER_STORE_VERSION,
  METHOD_DIRECTIONS,
  SCHEMA_HASH,
  SCHEMA_ID,
  SCHEMA_VERSION,
  type ManagedMethod,
  type Operation,
  type ProviderControlMethod,
} from "./generated/openclank-managed-contract"
export * from "./generated/openclank-managed-contract"

export interface CapabilityDeclaration {
  readonly protocolVersion: typeof PROTOCOL_VERSION
  readonly providerStoreVersion: typeof PROVIDER_STORE_VERSION
  readonly operationRouterVersion: typeof OPERATION_ROUTER_VERSION
  readonly schemaID: typeof SCHEMA_ID
  readonly schemaVersion: typeof SCHEMA_VERSION
  readonly schemaHash: string
  readonly methods: readonly ManagedMethod[]
  readonly operations: readonly Operation[]
  readonly artifactTransfer: true
  readonly localExecutor: true
}

export const capabilities: CapabilityDeclaration = Object.freeze({
  protocolVersion: PROTOCOL_VERSION,
  providerStoreVersion: PROVIDER_STORE_VERSION,
  operationRouterVersion: OPERATION_ROUTER_VERSION,
  schemaID: SCHEMA_ID,
  schemaVersion: SCHEMA_VERSION,
  schemaHash: SCHEMA_HASH,
  methods: [...PROVIDER_STORE_METHODS, ...PROVIDER_CONTROL_METHODS, ...OPERATION_METHODS, ...SESSION_METHODS, ...LOGGING_METHODS],
  operations: OPERATIONS,
  artifactTransfer: true,
  localExecutor: true,
})

export const initializeMeta = Object.freeze({ openclankManaged: capabilities })

function exactStringSet(value: unknown, expected: readonly string[]): boolean {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) return false
  return value.length === expected.length && new Set(value).size === expected.length && expected.every((item) => value.includes(item))
}

/** Fail closed unless the peer offers the exact pinned managed contract. */
export function validatePeerCapabilities(value: unknown): CapabilityDeclaration {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("Open Clank managed capability offer is missing")
  }
  const input = value as Record<string, unknown>
  const keys = [
    "protocolVersion",
    "providerStoreVersion",
    "operationRouterVersion",
    "schemaID",
    "schemaVersion",
    "schemaHash",
    "methods",
    "operations",
    "artifactTransfer",
    "localExecutor",
  ]
  if (Object.keys(input).length !== keys.length || keys.some((key) => !(key in input))) {
    throw new Error("Open Clank managed capability offer has an invalid shape")
  }
  if (
    input.protocolVersion !== PROTOCOL_VERSION ||
    input.providerStoreVersion !== PROVIDER_STORE_VERSION ||
    input.operationRouterVersion !== OPERATION_ROUTER_VERSION ||
    input.schemaID !== SCHEMA_ID ||
    input.schemaVersion !== SCHEMA_VERSION ||
    input.schemaHash !== SCHEMA_HASH ||
    input.artifactTransfer !== true ||
    input.localExecutor !== true ||
    !exactStringSet(input.methods, capabilities.methods) ||
    !exactStringSet(input.operations, capabilities.operations)
  ) {
    throw new Error("Open Clank managed capability offer is incompatible")
  }
  return input as unknown as CapabilityDeclaration
}

export interface CredentialLeaseRequest {
  readonly rootOperationID: string
  readonly connectionID: string
  readonly accountID: string
  readonly modelID: string
  readonly grantID?: string
  readonly expectedCredentialRevision: number
}

export interface CredentialLeaseResult {
  readonly leaseID: string
  readonly connectionID: string
  readonly accountID: string
  readonly credentialRevision: number
  readonly expiresAt: number
  readonly credential: Auth.Info
}

export interface CredentialReplaceRequest {
  readonly connectionID: string
  readonly accountID: string
  readonly expectedRevision: number
  readonly credential: Auth.Info
}

export interface RefreshAcquireRequest {
  readonly connectionID: string
  readonly accountID: string
  readonly expectedRevision: number
  readonly ttlMs?: number
}

export interface RefreshRenewRequest {
  readonly leaseID: string
  readonly ttlMs?: number
}

export interface RefreshCommitRequest extends CredentialReplaceRequest {
  readonly leaseID: string
}

export interface RefreshAbortRequest {
  readonly leaseID: string
}

export type ConnectionKind = "official" | "subscription" | "custom_gateway" | "local"

export type ProviderPrompt =
  | {
      readonly type: "text"
      readonly key: string
      readonly message: string
      readonly placeholder?: string
      readonly when?: { readonly key: string; readonly op: "eq" | "neq"; readonly value: string }
    }
  | {
      readonly type: "select"
      readonly key: string
      readonly message: string
      readonly options: readonly { readonly label: string; readonly value: string; readonly hint?: string }[]
      readonly when?: { readonly key: string; readonly op: "eq" | "neq"; readonly value: string }
    }

export interface ProviderAuthMethod {
  readonly id: string
  readonly type: "api" | "oauth" | "none"
  readonly label: string
  readonly prompts?: readonly ProviderPrompt[]
}

export interface ProviderFamily {
  readonly id: string
  readonly displayName: string
  readonly adapters: readonly string[]
  readonly kinds: readonly ConnectionKind[]
  readonly billingLanes: readonly Auth.BillingLane[]
  readonly authMethods: readonly ProviderAuthMethod[]
  readonly modelCount: number
}

export interface FamilyCatalogResult {
  readonly schemaVersion: 1
  readonly families: readonly ProviderFamily[]
}

export interface ConnectionValidationRequest {
  readonly familyID: string
  readonly adapterID: string
  readonly kind: ConnectionKind
  readonly billingLane: Auth.BillingLane
  readonly url?: string | null
  readonly settings: Record<string, unknown>
}

export interface ProviderModelRoute {
  readonly modelID: string
  readonly displayName: string
  readonly operations: readonly Operation[]
  readonly capabilities: Record<string, unknown>
  readonly provenance: Record<string, unknown>
}

export interface ConnectionValidationResult {
  readonly familyID: string
  readonly adapterID: string
  readonly kind: ConnectionKind
  readonly billingLane: Auth.BillingLane
  readonly normalizedURL?: string
  readonly settings: Record<string, unknown>
  readonly credentialRequired: boolean
  readonly modelRoutes: readonly ProviderModelRoute[]
}

export interface AccountValidationRequest {
  readonly connection: ConnectionValidationRequest
  readonly authMethod: "api_key" | "oauth"
  readonly credential: Auth.Info
  readonly accountID: string
  readonly credentialRevision: number
}

export interface AccountDiscovery {
  readonly status: "complete" | "unavailable" | "partial" | "reauth_required"
  readonly accountID: string
  readonly credentialRevision: number
  readonly models: readonly ProviderModelRoute[]
  readonly authoritative: boolean
  readonly provenance: {
    readonly source: string
    readonly observedAt: number
    readonly adapterVersion?: string
    readonly requestID?: string
  }
  readonly freshness: "fresh" | "stale" | "unknown"
  readonly errorCode?: "discovery_unavailable" | "discovery_partial" | "reauth_required"
}

export interface AccountValidationResult {
  readonly authMethod: "api_key" | "oauth"
  readonly authClass: string
  readonly credential: Auth.Info
  readonly accountID: string
  readonly credentialRevision: number
  readonly safeIdentity: Record<string, string>
  readonly modelRoutes: readonly ProviderModelRoute[]
  readonly discovery: AccountDiscovery
}

export type OAuthMode = "add" | "reauth"

interface OAuthFlowIdentity {
  readonly flowID: string
  readonly connectionID: string
  readonly providerID: string
  readonly billingLane: Auth.BillingLane
  readonly mode: OAuthMode
  readonly targetAccountID?: string
  readonly expectedRevision?: number
  readonly nonce: string
}

export interface OAuthStartRequest extends OAuthFlowIdentity {
  readonly method: number
  readonly redirectURI: string
  readonly state: string
  readonly codeVerifierChallenge: string
  readonly expiresAt: number
  readonly inputs: Record<string, string>
}

export interface OAuthStartResult {
  readonly flowID: string
  readonly url: string
  readonly method: "auto" | "code"
  readonly instructions: string
  readonly userCode?: string
  readonly expiresAt: number
}

export interface OAuthCompletionRequest extends OAuthFlowIdentity {
  readonly codeVerifier: string
  readonly code?: string
}

export type OAuthCompletionResult =
  | (Omit<OAuthFlowIdentity, "nonce"> & {
      readonly status: "complete"
      readonly credential: Auth.Info
      readonly authMethod: "oauth"
      readonly authClass: string
      readonly safeIdentity: Record<string, string>
    })
  | (Omit<OAuthFlowIdentity, "nonce"> & {
      readonly status: "pending" | "running" | "failed" | "cancelled" | "expired"
      readonly errorCode?: "oauth_exchange_failed" | "oauth_expired" | "oauth_cancelled"
    })

export interface OAuthCancelResult {
  readonly flowID: string
  readonly status: "cancelled"
}

export interface AccountCommitRequest {
  readonly bindingID: string
  readonly expectedRevision: number
}

export interface AccountAttemptRequest extends AccountCommitRequest {
  readonly accountID?: string
  readonly outcome: AccountSelection.AttemptOutcome
  readonly retryAfterMs?: number
  readonly modelEligible?: boolean
}

export type OperationJournalRequest =
  | {
      readonly action: "begin"
      readonly rootOperationID: string
      readonly operation: Operation
      readonly idempotencyKey: string
      readonly request: Record<string, unknown>
      readonly connectionID: string
      readonly billingLane: Auth.BillingLane
      readonly modelRouteID: string
    }
  | {
      readonly action: "cas"
      readonly operationID: string
      readonly expectedRevision: number
      readonly state?: "pending" | "running" | "complete" | "failed" | "cancelled"
      readonly bindingID?: string
      readonly selectedAccountID?: string
      readonly attempt?: Record<string, unknown>
      readonly commitReason?: string
      readonly artifactID?: string
    }

export interface OperationJournalResult {
  readonly operationID: string
  readonly rootOperationID: string
  readonly operation: Operation
  readonly requestHash: string
  readonly connectionID: string
  readonly billingLane: Auth.BillingLane
  readonly modelRouteID: string
  readonly bindingID?: string
  readonly selectedAccountID?: string
  readonly state: "pending" | "running" | "complete" | "failed" | "cancelled"
  readonly committed: boolean
  readonly commitReason?: string
  readonly attempts: readonly Record<string, unknown>[]
  readonly artifactIDs: readonly string[]
  readonly revision: number
  readonly replayed: boolean
}

export interface ArtifactDescriptor {
  readonly artifactID: string
  readonly contentSHA256: string
  readonly sizeBytes: number
  readonly mediaType: string
  readonly state: "staged" | "acknowledged"
  readonly expiresAt?: number
}

export interface ArtifactReadRequest {
  readonly artifactID: string
  readonly offset: number
  readonly limit: number
}

export interface ArtifactReadResult extends Omit<ArtifactDescriptor, "state" | "expiresAt"> {
  readonly offset: number
  readonly dataBase64: string
  readonly chunkSHA256: string
  readonly eof: boolean
}

export type ArtifactWriteRequest =
  | {
      readonly action: "put"
      readonly mediaType: string
      readonly contentSHA256: string
      readonly sizeBytes: number
      readonly chunks: readonly {
        readonly index: number
        readonly dataBase64: string
        readonly sha256: string
      }[]
    }
  | { readonly action: "acknowledge"; readonly artifactID: string }

export interface ExecutorInvokeRequest {
  readonly executorID: string
  readonly operation: Operation
  readonly artifactIDs: readonly string[]
  readonly options: Record<string, unknown>
}

export interface OperationRouteContext {
  readonly connectionID: string
  readonly providerID: string
  readonly billingLane: Auth.BillingLane
  readonly modelRouteID: string
  readonly modelID: string
  readonly grantID?: string
  readonly preferredAccountID?: string
  readonly inheritedAccountID?: string
}

export interface OperationArtifactInput {
  readonly name: string
  readonly artifactID: string
  readonly contentSHA256: string
  readonly sizeBytes: number
  readonly mediaType: string
}

export interface OperationExecuteRequest {
  readonly rootOperationID: string
  readonly idempotencyKey: string
  readonly operation: Operation
  readonly routes: readonly OperationRouteContext[]
  readonly input: Record<string, unknown>
  readonly artifactInputs: readonly OperationArtifactInput[]
  readonly options: Record<string, unknown>
}

export interface OperationExecuteResult {
  readonly operationID: string
  readonly rootOperationID: string
  readonly operation: Operation
  readonly modelRouteID: string
  readonly connectionID: string
  readonly billingLane: Auth.BillingLane
  readonly state: "complete" | "failed" | "cancelled"
  readonly committed: boolean
  readonly commitReason?: string
  readonly bindingID?: string
  readonly selectedAccountID?: string
  readonly output: Record<string, unknown>
  readonly artifacts: readonly ArtifactDescriptor[]
  readonly usage?: { readonly inputTokens?: number; readonly outputTokens?: number; readonly totalTokens?: number; readonly cacheReadTokens?: number; readonly cacheWriteTokens?: number; readonly reasoningTokens?: number; readonly audioInputTokens?: number; readonly audioOutputTokens?: number; readonly imageInputTokens?: number; readonly imageOutputTokens?: number; readonly searchRequests?: number }
  readonly metricCoverage?: LoggingMetricCoverage
  readonly coveredDispatchIDs?: string[]
  readonly lossReasons?: LoggingLossReason[]
  readonly identityCoverage?: "complete" | "partial"
  readonly normalizationProfile?: string
  /** Bounded passive API capacity; never raw headers or provider bodies. */
      readonly quota?: {
        readonly transport?: "documented"
        readonly adapterRevision?: string
        readonly requests?: { readonly limit?: number; readonly remaining?: number; readonly resetAt?: string }
        readonly tokens?: { readonly limit?: number; readonly remaining?: number; readonly resetAt?: string }
        readonly inputTokens?: { readonly limit?: number; readonly remaining?: number; readonly resetAt?: string }
        readonly outputTokens?: { readonly limit?: number; readonly remaining?: number; readonly resetAt?: string }
  }
  readonly modelFingerprint?: string
  readonly dimension?: number
  readonly replayed: boolean
}

export interface SessionCwdChangeRequest {
  readonly sessionID: string
  readonly requestedCwd: string
  readonly expectedWorkspaceRevision: number
  readonly transitionID: string
}

export type SessionCwdChangeResult =
  | {
      readonly outcome: "accepted"
      readonly canonicalCwd: string
      readonly workspaceRevision: number
      readonly changed: boolean
      readonly transitionID: string
    }
  | {
      readonly outcome: "rejected"
      readonly transitionID: string
      readonly committed: false
      readonly code: "invalid_cwd" | "unknown_session" | "stale_engine_session" | "owner_mismatch" | "workspace_rejected" | "workspace_revision_conflict" | "binding_unavailable"
    }

export interface SessionBindingReadRequest {
  readonly sessionID: string
}

export interface SessionBindingReadResult {
  readonly engineSessionID: string
  readonly stableChatID: string
  readonly owner: string
  readonly canonicalCwd: string
  readonly workspaceRevision: number
  readonly authorityWorkspaceID: string
  readonly memoryWorkspaceID: string
  readonly copalWorkspace: string
  readonly memoryEnabled: boolean
  readonly engineAliases: readonly string[]
  readonly mapRevision: number
  readonly mappingRevision: number
}

export interface OperationCancelRequest {
  readonly rootOperationID: string
  readonly idempotencyKey: string
}

export interface OperationCancelResult {
  readonly operationID: string
  readonly rootOperationID: string
  readonly state: "pending" | "cancelled" | "complete" | "failed"
}

export interface SessionSettingsEffectiveRequest {
  readonly providerID: string
  readonly modelID: string
}

export interface SessionSettingsEffectiveResult {
  readonly providerID: string
  readonly modelID: string
  readonly context: { readonly hard: number; readonly effective: number; readonly usable: number; readonly source: "model" | "config"; readonly input: number; readonly output: number }
  readonly compaction: { readonly auto: boolean; readonly prune: boolean; readonly tailTurns: number; readonly preserveRecentTokens: number; readonly reserved: number }
  readonly checkpoint: { readonly thresholds: readonly number[]; readonly reserved: number; readonly maxWriterFailures: number; readonly fork: boolean; readonly pushCaps: Record<string, number> }
}

export type HistoryQueryRequest =
  | { sessionID: string; operation: "search"; query: string; scope?: "chat" | "global"; kind?: Array<"user_text" | "assistant_text" | "tool_input" | "tool_error" | "reasoning" | "tool_output">; toolName?: string; timeAfter?: number; timeBefore?: number; limit?: number; chatID?: string }
  | { sessionID: string; operation: "around"; messageID: string; before?: number; after?: number; chatID?: string }
  | { sessionID: string; operation: "get"; messageID: string; partID: string; offset?: number; length?: number; chatID?: string }
  | { sessionID: string; operation: "media"; assetID: string; messageID?: string; partID?: string; chatID?: string }

export type HistoryMutationEvent = {
  messageID: string
  partID: string
  revision?: number
  actorID?: string
  role: string
  partType: string
  content: unknown
  timeCreated?: number
  timeUpdated?: number
  eventSequence?: number
}

export type HistoryTombstoneEvent = {
  messageID: string
  partID: string
  revision?: number
  actorID?: string
  tombstoneReason?: string
}

export type HistoryMutationRequest =
  | { sessionID: string; operation: "upsert" | "replay"; events: HistoryMutationEvent[] }
  | { sessionID: string; operation: "tombstone"; events: HistoryTombstoneEvent[] }

export type HistoryAttachment = { asset_id: string; mime_type: string | null; filename: string | null; byte_size: number | null }
export type HistoryQueryResult =
  | { ok: boolean; operation: "search"; result: { ok: boolean; hits: Array<{ part_id: string; session_id: string; message_id: string; project_id: string; kind: string; tool_name: string | null; snippet: string; score: number; time_created: number }>; limit: number; more: boolean; error?: string } }
  | { ok: boolean; operation: "around"; result: { ok: boolean; session_id: string; messages: Array<{ message_id: string; matched: boolean; time_created: number; parts: Array<{ part_id: string; type: string; role: "user" | "assistant"; tool_name: string | null; text: string }> }>; error?: string } }
  | { ok: boolean; operation: "get"; result: { ok: boolean; part?: { part_id: string; message_id: string; session_id: string; type: string; role: "user" | "assistant"; tool_name: string | null; text: string; has_more: boolean; next_offset: number | null; attachments: HistoryAttachment[]; time_created: number }; error?: string } }
  | { ok: boolean; operation: "media"; result: { ok: boolean; attachments: HistoryAttachment[]; error?: string } }

export type HistoryMutationResult = { ok: true; operation: "upsert" | "tombstone" | "replay"; accepted: number; duplicate: number; enqueued: number }

export type LoggingMetricCoverage = Record<string, {
  state: "reported" | "estimated" | "unavailable" | "not_applicable"
  coverage: "complete" | "partial" | "unknown"
  source: string
  reason?: string
}>
export type LoggingLossReason = "dropped" | "truncated" | "parse_degraded" | "write_failed" | "interrupted" | "admission_unavailable" | "queue_full" | "delivery_timeout"
export interface LoggingAdmissionRequest {
  bindingID: string
  rootOperationID: string
  operationID?: string
}
export interface LoggingAdmissionResult {
  admissionID: string
  policy: { advanced_enabled: boolean; request_body_enabled: boolean; response_body_enabled: boolean; binary_body_enabled: boolean; revision: number }
  context: {
    providerID: string; operationID: string | null; rootOperationID: string; instanceID: string
    bindingID: string; bindingRevision: number; identityCoverage: "complete" | "partial"
    persistence: { numeric: boolean; content: boolean; reason: "normal" | "incognito" | "temporary" | "auxiliary" }
    transportMode: "direct" | "advanced_proxy"
  }
}
export interface LoggingHeader {
  name: string
  values: string[]
  state: "reported" | "redacted" | "omitted" | "truncated"
}
export interface LoggingHttp {
  method?: string
  endpoint?: { origin: string; path: string }
  status?: number
  requestHeaders?: LoggingHeader[]
  responseHeaders?: LoggingHeader[]
  coverage?: Record<string, string>
}
export interface LoggingEventRequest {
  admissionID: string
  dispatchID: string
  source: "wire" | "sdk" | "acp" | "executor"
  sequence: number
  terminal?: boolean
  outcome?: "completed" | "upstream_error" | "cancelled" | "disconnected" | "interrupted" | "unknown"
  observationKind?: "delta" | "cumulative_snapshot" | "final_snapshot"
  metrics?: Record<string, number>
  metricCoverage?: LoggingMetricCoverage
  normalizationProfile?: string | null
  actualModel?: string
  timing?: Record<string, number | string | null>
  billable?: boolean | null
  dispatchIndex?: number
  retryOfDispatchID?: string
  coveredDispatchIDs?: string[]
  identityCoverage?: "complete" | "partial"
  quota?: OperationExecuteResult["quota"]
  http?: LoggingHttp
  requestBody?: unknown
  responseBody?: unknown
  events?: unknown[]
  lossReasons?: LoggingLossReason[]
}
export interface GoalCompletionRequest {
  sessionID: string
  journalID: string
  goalID: string
  goalRevision: number
  evidenceRefs: string[]
  verifiedAt: number
}

export interface MethodRequestMap {
  "_openclank/session/v1/goal/completed": GoalCompletionRequest
  "_openclank/logging/v1/admit": LoggingAdmissionRequest
  "_openclank/logging/v1/events": LoggingEventRequest
  "_openclank/history/v1/query": HistoryQueryRequest
  "_openclank/history/v1/mutate": HistoryMutationRequest
  "_openclank/provider-store/v1/account/bind": AccountSelection.SelectionRequest
  "_openclank/provider-store/v1/account/commit": AccountCommitRequest
  "_openclank/provider-store/v1/account/attempt": AccountAttemptRequest
  "_openclank/provider-store/v1/credential/lease": CredentialLeaseRequest
  "_openclank/provider-store/v1/credential/replace": CredentialReplaceRequest
  "_openclank/provider-store/v1/refresh/acquire": RefreshAcquireRequest
  "_openclank/provider-store/v1/refresh/renew": RefreshRenewRequest
  "_openclank/provider-store/v1/refresh/commit": RefreshCommitRequest
  "_openclank/provider-store/v1/refresh/abort": RefreshAbortRequest
  "_openclank/operations/v1/journal/cas": OperationJournalRequest
  "_openclank/operations/v1/artifact/read": ArtifactReadRequest
  "_openclank/operations/v1/artifact/write": ArtifactWriteRequest
  "_openclank/operations/v1/executor/invoke": ExecutorInvokeRequest
  "_openclank/operations/v1/cancel": OperationCancelRequest
  "_openclank/session/v1/cwd/change": SessionCwdChangeRequest
  "_openclank/session/v1/binding/read": SessionBindingReadRequest
}

export interface MethodResultMap {
  "_openclank/session/v1/goal/completed": { accepted: true; replayed: boolean }
  "_openclank/logging/v1/admit": LoggingAdmissionResult
  "_openclank/logging/v1/events": { accepted: boolean; replayed: boolean; captureState?: string }
  "_openclank/history/v1/query": HistoryQueryResult
  "_openclank/history/v1/mutate": HistoryMutationResult
  "_openclank/provider-store/v1/account/bind": AccountSelection.Binding
  "_openclank/provider-store/v1/account/commit": AccountSelection.Binding
  "_openclank/provider-store/v1/account/attempt": AccountSelection.Binding
  "_openclank/provider-store/v1/credential/lease": CredentialLeaseResult
  "_openclank/provider-store/v1/credential/replace": Auth.Account
  "_openclank/provider-store/v1/refresh/acquire": Auth.RefreshLease
  "_openclank/provider-store/v1/refresh/renew": Auth.RefreshLease
  "_openclank/provider-store/v1/refresh/commit": Auth.Account
  "_openclank/provider-store/v1/refresh/abort": Record<string, never>
  "_openclank/operations/v1/journal/cas": OperationJournalResult
  "_openclank/operations/v1/artifact/read": ArtifactReadResult
  "_openclank/operations/v1/artifact/write": ArtifactDescriptor
  "_openclank/operations/v1/executor/invoke": ArtifactDescriptor
  "_openclank/operations/v1/cancel": OperationCancelResult
  "_openclank/session/v1/cwd/change": SessionCwdChangeResult
  "_openclank/session/v1/binding/read": SessionBindingReadResult
}

export interface EngineMethodRequestMap {
  "_openclank/provider-control/v1/catalog": Record<string, never>
  "_openclank/provider-control/v1/connection/validate": ConnectionValidationRequest
  "_openclank/provider-control/v1/account/validate": AccountValidationRequest
  "_openclank/provider-control/v1/oauth/start": OAuthStartRequest
  "_openclank/provider-control/v1/oauth/poll": OAuthCompletionRequest
  "_openclank/provider-control/v1/oauth/callback": OAuthCompletionRequest
  "_openclank/provider-control/v1/oauth/cancel": OAuthCompletionRequest
  "_openclank/operations/v1/execute": OperationExecuteRequest
  "_openclank/operations/v1/cancel": OperationCancelRequest
  "_openclank/session/v1/settings/effective": SessionSettingsEffectiveRequest
}

export interface EngineMethodResultMap {
  "_openclank/provider-control/v1/catalog": FamilyCatalogResult
  "_openclank/provider-control/v1/connection/validate": ConnectionValidationResult
  "_openclank/provider-control/v1/account/validate": AccountValidationResult
  "_openclank/provider-control/v1/oauth/start": OAuthStartResult
  "_openclank/provider-control/v1/oauth/poll": OAuthCompletionResult
  "_openclank/provider-control/v1/oauth/callback": OAuthCompletionResult
  "_openclank/provider-control/v1/oauth/cancel": OAuthCancelResult
  "_openclank/operations/v1/execute": OperationExecuteResult
  "_openclank/operations/v1/cancel": OperationCancelResult
  "_openclank/session/v1/settings/effective": SessionSettingsEffectiveResult
}

const ENGINE_METHOD_SET: ReadonlySet<string> = new Set(
  Object.entries(METHOD_DIRECTIONS)
    .filter(([, direction]) => direction === "host_to_engine")
    .map(([method]) => method),
)
const PROVIDER_CONTROL_METHOD_SET: ReadonlySet<string> = new Set(PROVIDER_CONTROL_METHODS)

export function isProviderControlMethod(value: string): value is ProviderControlMethod {
  return PROVIDER_CONTROL_METHOD_SET.has(value)
}

export function isEngineMethod(value: string): value is keyof EngineMethodRequestMap {
  return ENGINE_METHOD_SET.has(value)
}

export interface ExtensionConnection {
  extMethod(method: string, params: Record<string, unknown>): Promise<unknown>
}

/** Typed seam for managed host callbacks; callers still validate secret-bearing results. */
export async function callHost<M extends keyof MethodRequestMap>(
  connection: ExtensionConnection,
  method: M,
  params: MethodRequestMap[M],
): Promise<MethodResultMap[M]> {
  return (await connection.extMethod(method, params as unknown as Record<string, unknown>)) as MethodResultMap[M]
}

export * as OpenClankManagedProtocol from "./openclank-protocol"
