import type { Auth } from "@/auth"
import type { AccountSelection } from "@/provider/account-selection"

// Search callback payloads use the existing executor method; this is a type-only
// re-export so consumers discover the fixed recipe with the managed protocol.
export type {
  SearchArtifactResult,
  SearchCallbackRequest,
  SearchCancelRequest,
  SearchClaims,
  SearchDelegate,
  SearchDispatchRequest,
  SearchFinalizeRequest,
  SearchInput,
  SearchResponseArtifact,
  SearchSource,
  TrustedSearchCallbackAuthority,
} from "./managed-search-contract"
export { SEARCH_CONTRACT_VERSION, SEARCH_EXECUTOR_ID, validateSearchArtifactResult, validateSearchCallbackRequest } from "./managed-search-contract"

import {
  OPERATION_METHODS,
  MANAGED_METHODS,
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
  readonly providerAuthContract: 1
  readonly providerMethodPolicyContract: 1
  readonly privateRuntimeContract?: 1
}

export const capabilities: CapabilityDeclaration = Object.freeze({
  protocolVersion: PROTOCOL_VERSION,
  providerStoreVersion: PROVIDER_STORE_VERSION,
  operationRouterVersion: OPERATION_ROUTER_VERSION,
  schemaID: SCHEMA_ID,
  schemaVersion: SCHEMA_VERSION,
  schemaHash: SCHEMA_HASH,
  methods: MANAGED_METHODS,
  operations: OPERATIONS,
  artifactTransfer: true,
  localExecutor: true,
  providerAuthContract: 1,
  providerMethodPolicyContract: 1,
})

export const initializeMeta = Object.freeze({ openclankManaged: capabilities })
export function initializeMetaFor(privateRuntime = false) {
  return { openclankManaged: privateRuntime ? { ...capabilities, privateRuntimeContract: 1 as const } : capabilities }
}

function exactStringSet(value: unknown, expected: readonly string[]): boolean {
  if (!Array.isArray(value) || value.some((item) => typeof item !== "string")) return false
  return value.length === expected.length && new Set(value).size === expected.length && expected.every((item) => value.includes(item))
}

/** Fail closed unless the peer offers the exact pinned managed contract. */
export function validatePeerCapabilities(value: unknown, privateRuntime = false): CapabilityDeclaration {
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
    "providerAuthContract",
    "providerMethodPolicyContract",
  ]
  if (privateRuntime) keys.push("privateRuntimeContract")
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
    input.providerAuthContract !== 1 ||
    input.providerMethodPolicyContract !== 1 ||
    (privateRuntime && input.privateRuntimeContract !== 1) ||
    !exactStringSet(input.methods, capabilities.methods) ||
    !exactStringSet(input.operations, capabilities.operations)
  ) {
    throw new Error("Open Clank managed capability offer is incompatible")
  }
  return input as unknown as CapabilityDeclaration
}

export interface CrossModelAlternativeIdentity {
  readonly version:1; readonly turn:ManagedTurnIdentity; readonly primaryBindingID:string; readonly primaryBindingRevision:number
  readonly alternativeBindingID:string; readonly alternativeBindingRevision:1; readonly attemptID:string; readonly attemptGeneration:2; readonly expectedCoordinatorRevision:number
}
export interface CrossModelAlternativeReceipt {
  readonly version:1; readonly primaryBindingID:string; readonly primaryBindingRevision:number; readonly coordinatorRevision:number
  readonly alternativeBindingID:string; readonly alternativeBindingRevision:number; readonly attemptID:string; readonly attemptGeneration:2
}
export interface CrossModelAlternativeRoute {
  readonly route_id:string; readonly route_revision:number; readonly connection_id:string; readonly connection_revision:number
  readonly provider_id:'anthropic'; readonly model_id:string; readonly lane:'metered_api'; readonly grant_id:null; readonly grant_revision:null
  readonly options:{readonly route_id:string;readonly catalog_revision:number;readonly options:{readonly reasoning_effort?:string}}
}
export interface CrossModelAlternativeIssueResult {
  readonly version:1; readonly binding:AccountSelection.Binding; readonly route:CrossModelAlternativeRoute
  readonly lease:CredentialLeaseResult; readonly attempt:CrossModelAlternativeReceipt
}
export type CrossModelAlternativeReadyResult = CrossModelAlternativeReceipt & {readonly state:'ready'}
export type CrossModelAlternativeCommittedResult = CrossModelAlternativeReceipt & {readonly state:'committed'}
export interface CrossModelDispatchRequest { readonly bindingID:string; readonly expectedRevision:number; readonly crossModelDispatch:CrossModelAlternativeIdentity }

export interface CredentialLeaseRequest {
  readonly bindingID: string
  readonly expectedBindingRevision: number
  readonly expectedCredentialRevision: number
  readonly crossModelAdmission?: { readonly version:1; readonly turn:ManagedTurnIdentity; readonly expectedCoordinatorRevision:number }
  readonly crossModelAlternative?: { readonly version:1; readonly turn:ManagedTurnIdentity; readonly terminalAttemptID:string; readonly expectedCoordinatorRevision:number }
}

export interface CredentialLeaseResult {
  readonly crossModelAttempt?: { readonly version:1; readonly attemptID:string; readonly attemptGeneration:number; readonly coordinatorRevision:number }
  readonly methodPolicy?: ProviderMethodPolicy
  readonly leaseID: string
  readonly bindingID: string
  readonly scopeKind: "actor_execution" | "auxiliary_operation"
  readonly rootOperationID: string
  readonly sessionID?: string
  readonly actorID?: string
  readonly executionRevision?: number
  readonly operationID?: string
  readonly modelRouteID?: string
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
  readonly bindingID?: string
  readonly methodPolicy?: ProviderMethodPolicy
  readonly connectionID: string
  readonly accountID: string
  readonly expectedRevision: number
  readonly ttlMs?: number
}

export interface RefreshRenewRequest {
  readonly bindingID?: string
  readonly methodPolicy?: ProviderMethodPolicy
  readonly leaseID: string
  readonly ttlMs?: number
}

export interface RefreshCommitRequest extends CredentialReplaceRequest {
  readonly bindingID?: string
  readonly methodPolicy?: ProviderMethodPolicy
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
  readonly acquisitionMethod?: "manual-key" | "browser" | "device"
  readonly credentialKinds?: readonly ("api" | "oauth")[]
  readonly billingLanes?: readonly Auth.BillingLane[]
  readonly prompts?: readonly ProviderPrompt[]
}

export interface ProviderFamily {
  readonly id: string
  readonly displayName: string
  readonly adapters: readonly string[]
  readonly kinds: readonly ConnectionKind[]
  readonly billingLanes: readonly Auth.BillingLane[]
  readonly endpointPolicies?: Readonly<Record<string, string>>
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
  readonly provenance: ProviderRouteProvenance
}

export interface ProviderRouteProvenance {
  readonly authority: "managed-engine"
  readonly origin?: "account-discovery"
  readonly catalog?: string
  readonly familyID?: string
  readonly metadataCatalog?: string
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
  readonly allowCredentialRefresh?: boolean
  readonly connection: ConnectionValidationRequest
  readonly authMethod: "api_key" | "oauth"
  readonly acquisitionMethod?: "manual-key" | "browser" | "device"
  readonly credentialKind?: "api" | "oauth"
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
  readonly retryable?: boolean
  readonly retryAfterMs?: number
}

export interface AccountValidationResult {
  readonly credentialRefreshed?: boolean
  readonly authMethod: "api_key" | "oauth"
  readonly acquisitionMethod: "manual-key" | "browser" | "device" | null
  readonly credentialKind: "api" | "oauth"
  readonly endpointPolicy?: "mimo-metered-api-v1" | "mimo-token-plan-v1"
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
      readonly authMethod: "api_key" | "oauth"
      readonly acquisitionMethod: "browser" | "device"
      readonly credentialKind: "api" | "oauth"
      readonly endpointPolicy?: "mimo-metered-api-v1" | "mimo-token-plan-v1"
      readonly authClass: string
      readonly safeIdentity: Record<string, string>
    })
  | (Omit<OAuthFlowIdentity, "nonce"> & {
      readonly status: "pending" | "running" | "failed" | "cancelled" | "expired"
      readonly errorCode?: "oauth_exchange_failed" | "oauth_credential_mismatch" | "oauth_endpoint_mismatch" | "oauth_expired" | "oauth_cancelled"
    })

export interface OAuthCancelResult {
  readonly flowID: string
  readonly status: "cancelled"
}

export interface AccountCommitRequest {
  readonly bindingID: string
  readonly expectedRevision: number
  readonly crossModelControl?: { readonly version:1; readonly turn:ManagedTurnIdentity; readonly attemptID:string; readonly attemptGeneration:number; readonly expectedCoordinatorRevision:number }
  readonly crossModelAlternativeCommit?: CrossModelAlternativeIdentity
}

export interface AccountAttemptRequest extends AccountCommitRequest {
  readonly accountID?: string
  readonly outcome: AccountSelection.AttemptOutcome
  readonly evidence?: AccountSelection.AttemptEvidence
  readonly dispatchObservation?: AccountSelection.DispatchObservation
  readonly refreshContinuity?: {readonly version:1;readonly admissionID:string;readonly previousCredentialRevision:number;readonly credentialRevision:number}
  readonly successContinuity?: {readonly version:1;readonly admissionID:string;readonly credentialRevision:number} | {readonly version:2;readonly admissionID:string;readonly previousCredentialRevision:number;readonly credentialRevision:number}
  readonly retryAfterMs?: number
  readonly modelEligible?: boolean
}

export interface AccountTerminalRequest {
  readonly bindingID:string
  readonly expectedRevision:number
  readonly crossModelTerminal:{readonly version:1;readonly turn:ManagedTurnIdentity;readonly attemptID:string;readonly attemptGeneration:number;readonly expectedCoordinatorRevision:number;readonly profile:'anthropic.messages.401-auth-no-output.v1'}
}
export interface AccountTerminalResult {
  readonly version:1;readonly state:'auth_no_output_closed';readonly bindingID:string;readonly bindingRevision:number
  readonly attemptID:string;readonly attemptGeneration:number;readonly coordinatorRevision:number
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

export interface ProviderMethodPolicy {
  readonly version: 1
  readonly methodID: "anthropic.claude_subscription"
  readonly credentialOwner: string
  readonly policyRevision: number
}

export interface OperationRouteContext {
  readonly methodPolicy?: ProviderMethodPolicy
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

export interface PrivateAuthority { readonly kind: "private"; readonly generation: string }
export type SessionBindingReadResult = {
  readonly engineSessionID: string
  readonly stableChatID: string
  readonly owner: string
  readonly canonicalCwd: string
  readonly authorityWorkspaceID: string
  readonly memoryWorkspaceID: string
  readonly copalWorkspace: string
  readonly memoryEnabled: boolean
  readonly engineAliases: readonly string[]
} & ({ readonly authority: PrivateAuthority; readonly workspaceRevision?: number; readonly mapRevision?: never; readonly mappingRevision?: never } |
     { readonly authority?: never; readonly workspaceRevision: number; readonly mapRevision: number; readonly mappingRevision: number })

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

export interface SessionCodeContextRequest {
  readonly sessionID: string
  readonly turn: ManagedTurnIdentity
  readonly requestID: string
  readonly action?: "read" | "cancel"
  readonly bindingRevision: number
  readonly requestFingerprint: string
  readonly query: string
  readonly availableByteTokens: number
  readonly retainedMessages: Record<string, unknown>[]
}
export interface SessionCodeContextResult {
  readonly requestID: string
  readonly bindingRevision: number
  readonly requestFingerprint: string
  readonly card: string | null
  readonly reason: string | null
}

export interface SessionSettingsEffectiveResult {
  readonly providerID: string
  readonly modelID: string
  readonly context: { readonly hard: number; readonly effective: number; readonly usable: number; readonly source: "model" | "config"; readonly input: number; readonly output: number }
  readonly compaction: { readonly auto: boolean; readonly prune: boolean; readonly tailTurns: number; readonly preserveRecentTokens: number; readonly reserved: number }
  readonly checkpoint: { readonly thresholds: readonly number[]; readonly reserved: number; readonly fork: boolean; readonly pushCaps: Record<string, number> }
}

export type HistorySourceRef = {messageID:string; partID:string; revision:number; contentHash:string; outputSHA256:string; outputBytes:number}
export type HistoryQueryRequest =
  | { sessionID:string; operation:"check_sources"; rootOperationID:string; actorID:string; executionRevision:number; innerTurnID:string; sources:HistorySourceRef[]; rowViewAdmission?:{version:1; settingsRevision:string}; sessionViewAdmission?:{version:1; settingsRevision:string}; browserSnapshotRepeat?:{version:1;settingsRevision:string;targetID:string;connectionGeneration:string;navigationGeneration:number;latestObservationGeneration:number} }
  | { sessionID: string; operation: "exact_output"; rootOperationID: string; actorID: string; executionRevision: number; innerTurnID: string; messageID: string; partID: string; revision: number; contentHash: string; offset: number; limit: number }
  | { sessionID: string; operation: "search"; query: string; scope?: "chat" | "global"; kind?: Array<"user_text" | "assistant_text" | "tool_input" | "tool_error" | "reasoning" | "tool_output">; toolName?: string; timeAfter?: number; timeBefore?: number; limit?: number; chatID?: string }
  | { sessionID: string; operation: "around"; messageID: string; before?: number; after?: number; chatID?: string }
  | { sessionID: string; operation: "get"; messageID: string; partID: string; offset?: number; length?: number; chatID?: string }
  | { sessionID: string; operation: "media"; assetID: string; messageID?: string; partID?: string; chatID?: string }

export type HistoryMutationEvent = {
  toolTiming?: import("../session/tool-timing").ToolTimingProof | import("../session/tool-timing").ToolActivityProof
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
  | { ok:boolean; operation:"check_sources"; result:{state:"ready"|"unavailable"; sources:HistorySourceRef[]} }
  | { ok: boolean; operation: "exact_output"; result: { state: "ready" | "unavailable"; page: { revision: number; contentHash: string; outputSHA256: string; outputBytes: number; offset: number; nextOffset: number | null; text: string } | null } }
  | { ok: boolean; operation: "search"; result: { ok: boolean; hits: Array<{ part_id: string; session_id: string; message_id: string; project_id: string; kind: string; tool_name: string | null; snippet: string; score: number; time_created: number }>; limit: number; more: boolean; error?: string } }
  | { ok: boolean; operation: "around"; result: { ok: boolean; session_id: string; messages: Array<{ message_id: string; matched: boolean; time_created: number; parts: Array<{ part_id: string; type: string; role: "user" | "assistant"; tool_name: string | null; text: string }> }>; error?: string } }
  | { ok: boolean; operation: "get"; result: { ok: boolean; part?: { part_id: string; message_id: string; session_id: string; type: string; role: "user" | "assistant"; tool_name: string | null; text: string; has_more: boolean; next_offset: number | null; attachments: HistoryAttachment[]; time_created: number }; error?: string } }
  | { ok: boolean; operation: "media"; result: { ok: boolean; attachments: HistoryAttachment[]; error?: string } }

export type HistorySourceAck = {version:1; accountID:string; chatID:string; actorID:string; sessionID:string; messageID:string; partID:string; revision:number; contentHash:string; outputSHA256:string; outputBytes:number}
export type HistoryMutationResult = { ok: true; operation: "upsert" | "tombstone" | "replay"; accepted: number; duplicate: number; enqueued: number; sourceAck?: HistorySourceAck }

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
  diagnosticOnly?: {version:1;kind:"malformed_success";profile:"openai.codex.responses.v1";httpStatus:number;upstreamDispatchID:string} | {version:2;kind:"malformed_success";profile:"openai.codex.responses.v1";httpStatus:number;upstreamDispatchID:string;previousCredentialRevision:number;credentialRevision:number}
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

export interface ActorOptionSource { readonly kind: "parent_execution" | "default" | "receipt"; readonly requestID?: string; readonly sessionID?: string; readonly actorID?: string; readonly executionRevision?: number; readonly rootOperationID?: string; readonly messageID?: string }
export interface ActorOptionCandidate { readonly version: 1; readonly source: ActorOptionSource; readonly model: { readonly providerID: string; readonly modelID: string }; readonly variant: string | null }
export interface ActorOptionSnapshot { readonly version: 1; readonly routeID: string; readonly optionRouteID: string; readonly catalogRevision: number; readonly model: { readonly providerID: string; readonly modelID: string }; readonly options: { readonly reasoning_effort?: string }; readonly source: ActorOptionSource }

export interface ActorRoutePreflightRequest {
  readonly optionCandidate?: ActorOptionCandidate
  readonly savedOptionSnapshot?: ActorOptionSnapshot
  readonly sessionID: string
  readonly rootOperationID: string
  readonly actorID: string
  readonly parentActorID?: string
  readonly expectedExecutionRevision: number
  readonly model: { readonly providerID: string; readonly modelID: string }
  readonly modelSource: "inherited" | "explicit"
  readonly grantID?: string
  readonly inheritedAccountID?: string
}

export type ActorRouteBootstrap = {
  readonly modelRequestOptions?: ActorOptionSnapshot
  readonly stableChatID: string
  readonly owner: string
  readonly rootSessionID: string
  readonly sessionID: string
  readonly actorID: string
  readonly rootOperationID: string
  readonly expectedExecutionRevision: number
  readonly modelRouteID: string
  readonly handoffID?: string
} & ({ readonly authority: PrivateAuthority; readonly workspaceRevision?: number; readonly mapRevision?: never; readonly mappingRevision?: never } |
     { readonly authority?: never; readonly mapRevision: number; readonly mappingRevision: number; readonly workspaceRevision: number })

export interface ActorRoutePreflightResult {
  readonly applicability?: { readonly version:1; readonly methodPolicy:"required" | "not_applicable"; readonly grant:"shared" | "none" }
  readonly modelRequestOptions?: ActorOptionSnapshot
  readonly methodPolicy?: ProviderMethodPolicy
  readonly bootstrap: ActorRouteBootstrap
  readonly connectionID: string
  readonly providerID: string
  readonly billingLane: Auth.BillingLane
  readonly modelID: string
  readonly modelRouteID: string
  readonly grantID?: string
  readonly grantRevision?: number
}

export interface ManagedExecutionIdentity {
  readonly sessionID: string
  readonly rootOperationID: string
  readonly actorID: string
  readonly executionRevision: number
}

export interface ManagedActorFinalEvidence {
  readonly version: 1
  readonly bindingID: string
  readonly bindingRevision: number
  readonly outcome: "success" | "failure" | "cancelled"
  readonly usage: Readonly<Record<string, number>>
  readonly metricCoverage: LoggingMetricCoverage
  readonly coveredDispatchIDs: readonly string[]
  readonly lossReasons: NonNullable<LoggingEventRequest["lossReasons"]>
  readonly identityCoverage: "complete" | "partial"
  readonly normalizationProfile: string
  readonly lastDispatch?: { readonly dispatchID: string; readonly complete: true }
}

export interface ManagedExecutionFinishRequest extends ManagedExecutionIdentity {
  readonly evidence?: ManagedActorFinalEvidence
  readonly executionOutcome?: "success"|"failure"|"cancelled"
  readonly recovery?: { readonly rootSessionID:string; readonly innerTurnID:string; readonly turnOutcome:"completed"|"cancelled"|"error"; readonly executionOutcome:"success"|"failure"|"cancelled" }
}

export interface ManagedExecutionFinishResult extends ManagedTurnFinishResult {
  readonly captureState: "recorded" | "absent" | "unavailable"
}

export interface ManagedTurnIdentity extends ManagedExecutionIdentity {
  readonly sessionID: string
  readonly rootOperationID: string
  readonly actorID: string
  readonly executionRevision: number
  readonly innerTurnID: string
}

export type ShellRewriteAdmission = {readonly version:1; readonly profile:"git-diff-no-color-v1"; readonly settingsRevision:string}
export interface ManagedToolAdmitRequest extends ManagedTurnIdentity {
  readonly shellRewriteAdmission?: ShellRewriteAdmission
  readonly toolName: string
  readonly toolCallID?: string
  readonly toolOperation?: "status" | "discover" | "download" | "cancel"
  readonly kind: "tool" | "mcp_tool" | "project_policy_read" | "memory_read" | "skill_use"
}

export type ManagedPreparedReadRequest = ManagedTurnIdentity & (
  | { readonly kind:"knowledge"|"skill"; readonly action:"page"; readonly limit:number; readonly cursor:string|null; readonly token:null }
  | { readonly kind:"knowledge"|"skill"; readonly action:"hydrate"; readonly limit:null; readonly cursor:null; readonly token:string }
  | { readonly kind:"knowledge"; readonly action:"snapshot_read"; readonly limit:null; readonly cursor:null; readonly token:string }
)
export type SkillMetadataCurrentRequest = ManagedTurnIdentity & {readonly skillID:string;readonly revision:number;readonly contentHash:string;readonly snapshotSHA256:string;readonly metadataReceiptSHA256:string}
export type ManagedPreparedReadResult =
  | { readonly state:"unavailable"|"stale" }
  | { readonly state:"ready"|"partial"; readonly kind:"knowledge"|"skill"; readonly items:readonly {readonly token:string;readonly name:string|null;readonly trust:number|"verified"|"waived"|"legacy";readonly trustBasis:"skill_publication"|"knowledge_trust_score";readonly display:{readonly id:string;readonly type:string}}[]; readonly cursor:string|null; readonly complete:boolean }
  | { readonly state:"ready"; readonly kind:"knowledge"|"skill"; readonly body:string; readonly expansion?:{readonly token:string;readonly sha256:string;readonly sizeBytes:number;readonly captureOrigin:"verified_capture"|"imported_unverified"} }
  | { readonly state:"ready"; readonly kind:"knowledge"; readonly fragment:string; readonly startByte:number;readonly endByte:number;readonly totalBytes:number;readonly sha256:string;readonly captureOrigin:"verified_capture"|"imported_unverified";readonly operation:"functions"|"info"|"strings"|"disassembly"|"xrefs_to_entry"|"decompile"|"xrefs_from_instruction";readonly representation:"json_text_fragment";readonly complete:boolean;readonly nextToken:string|null }

export interface ManagedMaintenanceStageRequest extends ManagedTurnIdentity { readonly actorID:"main";readonly summaryMessageID:string;readonly summaryPartIDs:readonly string[] }
export type ManagedMcpHealthResult = {readonly status:"observed"|"unavailable";readonly omittedServers:number;readonly servers:readonly {readonly serverKey:string;readonly status:"connected"|"degraded"|"disabled"|"failed"|"needs_auth"|"needs_client_registration"|"disconnected"|"unknown";readonly connectionGeneration:number|null;readonly observedAt:number|null;readonly discoveryMs:number|null;readonly lastCallAt:number|null;readonly lastCallMs:number|null;readonly lastCallCategory:"tool_error"|"transport"|"cancelled"|null;readonly discoveredCount:number|null;readonly offeredToPromptCount:number|null;readonly modelOfferedCount:null;readonly callAdmissibleCount:null}[]}
export interface ManagedMaintenanceSummaryReadRequest { readonly directory:string;readonly rootSessionID:string;readonly sessionID:string;readonly summaryMessageID:string }
export type ManagedMaintenanceSummaryReadResult = {readonly state:"unavailable"}|{readonly state:"ready";readonly sessionID:string;readonly summaryMessageID:string;readonly providerID:string;readonly modelID:string;readonly parts:readonly {readonly partID:string;readonly text:string;readonly sha256:string}[]}
export interface ManagedMaintenanceAckRequest extends ManagedTurnIdentity { readonly actorID:"main"; readonly summaryMessageID:string; readonly attestation:string; readonly sourceMessageID:string; readonly sourcePartID:string; readonly sourceTextSHA256:string; readonly dispatchID:string; readonly bodySHA256:string; readonly promptSHA256:string; readonly wireProfile:"openai-chat"|"codex-responses"; readonly instructionsSHA256?:string; readonly instructionsClass?:"builtin-compaction-v1" }
export type ManagedMaintenanceSourceRequest = ManagedTurnIdentity & { readonly actorID:"main"; readonly messageID:string; readonly partID:string; readonly textSHA256:string } & ({readonly version?:never; readonly layout?:never; readonly sourcePosition?:never} | {readonly version:2; readonly layout:readonly {readonly position:number;readonly sha256:string;readonly synthetic:boolean}[];readonly sourcePosition:number})
export type ManagedMaintenanceSourceResult = {readonly state:"unavailable"}|{readonly state:"ready";readonly attestation:string}
export interface ManagedAttachmentSourceRequest extends ManagedTurnIdentity {readonly actorID:'main';readonly messageID:string;readonly partID:string;readonly textSHA256:string;readonly uploadID:string;readonly originSHA256:string}
export type ManagedAttachmentSourceResult = {readonly state:'unavailable'}|{readonly state:'ready';readonly messageID:string;readonly partID:string;readonly textSHA256:string;readonly uploadID:string;readonly artifactID:string;readonly artifactSHA256:string;readonly outputSHA256:string;readonly previewHash:string;readonly sourceReceiptSHA256:string}
export type ManagedMediaOperation = "image.generate" | "mesh.generate" | "mesh.rig" | "material.pack" | "material.bake" | "material.generate" | "image.render" | "audio.generate" | "image.pack" | "image.pack-crops"
export type ManagedMediaInput = {readonly kind:"workflow_recipe";readonly resource_ref:string} | {readonly kind:"text";readonly text:string} | {readonly kind:"image";readonly resource_ref:string} | {readonly kind:"mesh";readonly resource_ref:string} | {readonly kind:"texture_map";readonly role:"baseColor"|"roughness"|"metallic"|"normal";readonly resource_ref:string}
export type ManagedSpriteAnnotations = {readonly fps:number;readonly facing:string|null;readonly pivots:readonly {readonly x:number;readonly y:number}[];readonly palette:string|null;readonly style:string|null}
export type ManagedSpriteRect = {readonly x:number;readonly y:number;readonly width:number;readonly height:number}
export type ManagedMediaJobArgs = {readonly action:"inspect";readonly operation:"image.render";readonly inputs:readonly [{readonly kind:"mesh";readonly resource_ref:string}];readonly parameters:{readonly variant:"rig_clip"}} | {readonly action:"profiles";readonly operation?:ManagedMediaOperation} | {readonly action:"submit";readonly operation:ManagedMediaOperation;readonly profile_id:string;readonly inputs:readonly ManagedMediaInput[];readonly parameters:Record<string,string|number|boolean> | ManagedSpriteAnnotations | {readonly annotations:ManagedSpriteAnnotations;readonly rectangles:readonly ManagedSpriteRect[]};readonly destination:{readonly resource_ref:string;readonly name:string}} | {readonly action:"status"|"collect";readonly job_id:string}
export interface ManagedMediaJobRequest extends ManagedTurnIdentity {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly mode:"issue"|"status";readonly argsJSON:string}
export type ManagedRigJoint = {readonly nodeIndex:number;readonly jointOrdinal:number;readonly jointName:string;readonly parentNode:number|null}
export type ManagedRenderLayout = {readonly version:1;readonly coordinateFrame:"imported-world-z";readonly cellWidth:512;readonly cellHeight:512;readonly sheetWidth:1024;readonly sheetHeight:1024;readonly sha256:string;readonly views:readonly {readonly degrees:number;readonly x:number;readonly y:number;readonly width:512;readonly height:512}[]} | {readonly version:1;readonly coordinateFrame:"imported-blender-bone-local-x";readonly cellWidth:512;readonly cellHeight:512;readonly sheetWidth:1024;readonly sheetHeight:1024;readonly sha256:string;readonly fps:6;readonly frames:readonly {readonly frameIndex:number;readonly degrees:number;readonly x:number;readonly y:number;readonly width:512;readonly height:512}[];readonly rig:{readonly sourceSHA256:string;readonly skinIndex:0;readonly hierarchy:readonly ManagedRigJoint[];readonly selected:ManagedRigJoint};readonly importer:{readonly blenderVersion:string;readonly operator:"IMPORT_SCENE_OT_gltf";readonly module:string;readonly moduleVersion:string;readonly sourceFileSHA256:string|null;readonly settings:"factory-default-gltf-v1"}}
export type ManagedSpriteSource = {readonly resourceID:string;readonly revision:{readonly kind:string;readonly value:string};readonly sha256:string}
export type ManagedSpriteLayout = {readonly version:1|2;readonly source?:ManagedSpriteSource;readonly sourceWidth?:number;readonly sourceHeight?:number;readonly cellWidth:number;readonly cellHeight:number;readonly sheetWidth:number;readonly sheetHeight:number;readonly frames:readonly {readonly index:number;readonly source?:ManagedSpriteSource;readonly sourceRect?:ManagedSpriteRect;readonly x:number;readonly y:number;readonly width:number;readonly height:number;readonly pivot:{readonly x:number;readonly y:number}}[];readonly fps:number;readonly facing:string|null;readonly palette:string|null;readonly style:string|null;readonly sha256:string}
export type ManagedAudioMetadata = {readonly sampleRate:number;readonly channels:1|2;readonly bitsPerSample:16|24;readonly declaredSamples:number;readonly payloadVerified:false}
export interface ManagedMediaResult {readonly kind:"image"|"mesh"|"archive"|"audio";readonly mime_type:string;readonly sha256:string;readonly size_bytes:number;readonly resource_ref:string;readonly partial:boolean;readonly follow_up_operations:readonly ManagedMediaOperation[];readonly metadata?:Record<string,never> | {readonly layout:ManagedRenderLayout|ManagedSpriteLayout} | {readonly audio:ManagedAudioMetadata}}
export type ManagedMediaJobResult = {readonly ok:false} | {readonly ok:true;readonly inspection:{readonly kind:"rig";readonly source_sha256:string;readonly joints:readonly ManagedRigJoint[]}} | {readonly ok:true;readonly profiles:readonly {readonly profile_id:string;readonly revision:number;readonly label:string;readonly operations:readonly {readonly id:ManagedMediaOperation;readonly input_kinds:readonly ("workflow_recipe"|"text"|"image"|"texture_map"|"mesh")[];readonly output_kinds:readonly ("image"|"mesh"|"archive"|"audio")[];readonly render_variants?:readonly ("front"|"four_heading"|"rig_clip")[]}[]}[];readonly partial:boolean} | {readonly ok:true;readonly job_id:string;readonly revision:number;readonly state:string;readonly reason?:string;readonly result?:ManagedMediaResult}
export interface ManagedSharedTextRequest extends ManagedTurnIdentity { readonly messageID:string; readonly toolCallID:string; readonly toolName:string; readonly argsSHA256:string; readonly argsJSON:string; readonly mode:"issue"|"status" }
export interface ManagedSharedTextRevision { readonly kind:"hostFingerprint"; readonly value:string }
export interface ManagedSharedTextInitiator extends ManagedTurnIdentity { readonly version:1; readonly messageID:string; readonly toolCallID:string; readonly stableChatID:string; readonly argsSHA256:string }
export type ManagedSharedTextResult = {readonly ok:false} | {readonly ok:true; readonly result:
  {readonly version:1; readonly resource_id:string; readonly role:"viewer"|"commenter"|"editor"; readonly revision:ManagedSharedTextRevision; readonly text:string} |
  {readonly version:1; readonly state:"pending"|"unknown"|"complete"|"conflict"|"unavailable"; readonly operation_id:string; readonly client_digest:string; readonly revision?:ManagedSharedTextRevision; readonly text?:string; readonly current?:{readonly revision:ManagedSharedTextRevision;readonly text:string}; readonly review?:{readonly initiator:ManagedSharedTextInitiator;readonly history:{readonly state:"complete";readonly actionId:string}}}}
export interface ManagedThemeEmojiRequest extends ManagedTurnIdentity { readonly operation:"status"|"discover"|"download"|"cancel" }
export interface ManagedThemeEmojiResult { readonly operation:ManagedThemeEmojiRequest["operation"]; readonly available:boolean; readonly consent:string; readonly ready:boolean; readonly message?:string; readonly runID:string|null; readonly catalogueGeneration:number|null; readonly expected:number; readonly readyCount:number; readonly pendingCount:number; readonly failedCount:number; readonly bytes:number; readonly cancellable:boolean; readonly nextDue:string|null; readonly bySource:Record<string, {readonly expected:number;readonly readyCount:number;readonly pendingCount:number;readonly failedCount:number;readonly bytes:number}> }
export type ManagedContextAugmentRequest = (ManagedTurnIdentity & { readonly query:string; readonly context:string; readonly remainingMs:number }) | (ManagedTurnIdentity & {readonly kind:'standing_guidance';readonly remainingMs:number}) | (ManagedTurnIdentity & {readonly kind:'deliverable_appearance';readonly remainingMs:number})
export type ManagedContextAugmentResult = {readonly state:"found"; readonly text:string} | {readonly state:"unavailable"|"empty"|"skipped"|"timeout"} | {readonly state:"ready";readonly text:string;readonly attestationID:string;readonly textSHA256:string} | {readonly kind:'standing_guidance';readonly state:'unavailable'} | {readonly kind:'standing_guidance';readonly state:'ready';readonly text:string;readonly attestationID:string;readonly textSHA256:string;readonly coverage:'complete'|'partial'} | {readonly kind:'deliverable_appearance';readonly state:'unavailable'} | {readonly kind:'deliverable_appearance';readonly state:'ready';readonly appearance:Record<string,unknown>}
export type ManagedContextDeliveredRequest = (ManagedTurnIdentity & { readonly attestationID:string; readonly textSHA256:string; readonly contextPartID:string; readonly dispatchID:string; readonly bodySHA256:string }) | (ManagedTurnIdentity & {readonly kind:'standing_guidance';readonly attestationID:string;readonly textSHA256:string;readonly dispatchID:string;readonly bodySHA256:string;readonly systemSHA256:string;readonly wireProfile:'openai-chat'|'codex-responses'})
export interface ManagedToolAdmitResult { readonly shellRewriteProof?: ShellRewriteAdmission; readonly allowed: boolean; readonly reason?: string; readonly invocationID?: string }
export interface ManagedRecallAdmittedRequest extends ManagedTurnIdentity {
  readonly actorID: "main"
  readonly toolCallID: string
  readonly attestationID: string
  readonly resultSHA256: string
  readonly dispatchID: string
  readonly bodySHA256: string
}
export interface ManagedRecallAdmittedResult { readonly accepted: boolean }

export interface ManagedTurnStartResult extends ManagedTurnIdentity {
  readonly nativeToolPolicy?: Readonly<Record<string, boolean>>
  readonly originalIngressRequired: boolean
  readonly workerNamingEnabled?: true
  readonly accepted: true
  readonly browserSnapshotRepeatAdmission?: {readonly version:1;readonly sessionID:string;readonly rootOperationID:string;readonly actorID:string;readonly executionRevision:number;readonly innerTurnID:string;readonly hostAccountID:string;readonly holderID:string;readonly settingsRevision:string;readonly connectionID:string;readonly providerID:string;readonly modelID:string;readonly modelRouteID:string}
  readonly replayed: boolean
  readonly forwardPairV1?: true
  readonly mediaSourcesV1?: {readonly version:1;readonly rootOperationID:string;readonly items:readonly {readonly uploadID:string;readonly sourceSHA256:string|null;readonly stage:"prepared"|"omitted";readonly reason:"too_large_bytes"|"too_many_pixels"|"side_too_long"|"unsupported_type"|"type_mismatch"|"unreadable_header"|"route_without_vision"|null;readonly providerDelivery:"unknown"}[]}
  readonly mediaDerivativesV1?: {readonly version:1;readonly rootOperationID:string;readonly items:readonly {readonly version:1;readonly profile:'image-downscale-v1';readonly uploadID:string;readonly originalSHA256:string;readonly originalMime:string;readonly originalWidth:number;readonly originalHeight:number;readonly derivedSHA256:string;readonly derivedMime:'image/png';readonly derivedWidth:number;readonly derivedHeight:number;readonly derivedLength:number;readonly transformVersion:'pillow-lanczos-v1';readonly derivedBase64:string}[]}
  readonly sessionEnvironment?: ({readonly constructor:"host-session-env-reminder.v1";readonly directory:string;readonly platform:"linux"|"darwin"|"win32";readonly textSHA256:string} & ({readonly version:1;readonly gitRoot:string}|{readonly version:2;readonly gitRoot:null})) | {readonly version:3;readonly constructor:"host-session-personal-env.v1";readonly directory:string;readonly platform:"linux"|"darwin"|"win32";readonly repositoryStatus:"unobserved";readonly textSHA256:string}
  readonly factoryPersona?: {readonly version:1;readonly constructor:"host.factory-persona.v1";readonly textSHA256:string} | {readonly version:2;readonly constructor:"host.factory-persona-general.v1";readonly textSHA256:string;readonly factorySHA256:string;readonly defaultsSHA256:string;readonly snapshotSHA256:string} | import("../session/closed-source-provenance").StyledPersonaEvidence
  readonly displayOwnerProof?: { readonly version: 1; readonly accountID: string }
  readonly themeStatusOnly?: boolean
  readonly codeActivityEnabled?: boolean
}

export interface ManagedTurnStatusResult extends ManagedTurnIdentity {
  readonly state: "active" | "finished" | "unknown"
  readonly outcome?: "completed" | "cancelled" | "error"
  readonly reason?: string
}

export interface ManagedTurnFinishRequest extends ManagedTurnIdentity {
  readonly terminalErrorOutput?: {readonly version:1; readonly messageID:string}
  readonly outcome: "completed" | "cancelled" | "error"
  readonly forwardPairOutput?: {readonly version:1; readonly messageID:string; readonly partID:string; readonly textSHA256:string}
  readonly sourceClosure?: {readonly version:1; readonly wire:import("../session/closed-source-provenance").SourceWireObservation; readonly output:import("../session/closed-source-provenance").SourceOutputObservation}
}

export interface ManagedTurnFinishResult {
  readonly accepted: boolean
  readonly replayed: boolean
}

export interface GitIndexRecheckRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string
  readonly phase:'before_lock'|'before_object'|'before_publish'
}
export interface GitIndexFinishRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string
  readonly result:{readonly state:'applied'|'refused'|'unknown'; readonly object:'none'|'written_or_present'|'unknown'; readonly candidateRetained:boolean; readonly candidateIdentity?:{readonly token:string;readonly device:number;readonly inode:number}}
}
export interface GitCommitRecheckRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string
  readonly phase:'before_lock'|'before_tree'|'before_commit'|'before_publish'
}
export interface GitCommitFinishRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string
  readonly result:{readonly state:'applied'|'refused'|'unknown'; readonly tree:'none'|'written_or_present'|'unknown'; readonly commit:'none'|'written_or_present'|'unknown'; readonly ref:'not_issued'|'applied'|'unknown'; readonly commitOID?:string; readonly candidateRetained:boolean; readonly candidateIdentity?:{readonly token:string;readonly device:number;readonly inode:number}; readonly cleanup:'complete'|'unknown'}
}
export type GitFetchRecheckRequest = Omit<GitCommitRecheckRequest,'phase'> & {readonly phase:'before_transfer'|'before_publish'}
export type GitWorktreeObjectIdentity = {readonly device:string;readonly inode:string}
export type GitWorktreeRegistrationFileWitness = GitWorktreeObjectIdentity & {readonly size:number;readonly sha256:string}
export type GitWorktreeIndexWitness = GitWorktreeObjectIdentity & {readonly size:number;readonly sha256:string}
export type GitWorktreeRegistrationWitness = {readonly privateIdentity:GitWorktreeObjectIdentity;readonly marker:GitWorktreeRegistrationFileWitness & {readonly bytesBase64:string};readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly backlink:GitWorktreeRegistrationFileWitness;readonly head:GitWorktreeRegistrationFileWitness}
export type GitWorktreePhaseRequest = Omit<GitCommitRecheckRequest,'phase'> & {readonly gitPlanDigest:string;readonly event:
  | {readonly kind:'begin';readonly targetOID:string;readonly treeOID:string;readonly entries:readonly {readonly path:string;readonly mode:33188|33261;readonly oid:string;readonly size:number}[];readonly directories:readonly string[];readonly totalBytes:number}
  | {readonly kind:'blob_chunk';readonly ordinal:number;readonly offset:number;readonly contentBase64:string}
  | {readonly kind:'blob_end';readonly ordinal:number;readonly gitOID:string;readonly sha256:string}
  | {readonly kind:'seal_target'}
  | {readonly kind:'before_backlink_lock';readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly backlink:GitWorktreeRegistrationFileWitness;readonly index:GitWorktreeIndexWitness}
  | {readonly kind:'publication_lock_ready';readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly lockIdentity:GitWorktreeObjectIdentity;readonly candidateSHA256:string;readonly backlink:GitWorktreeRegistrationFileWitness}
  | {readonly kind:'before_backlink_commit';readonly adminIdentity:GitWorktreeObjectIdentity;readonly lockIdentity:GitWorktreeObjectIdentity;readonly candidateSHA256:string;readonly index:GitWorktreeIndexWitness}
  | {readonly kind:'backlink_committed';readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly backlink:GitWorktreeRegistrationFileWitness;readonly head:GitWorktreeRegistrationFileWitness;readonly index:GitWorktreeIndexWitness}
  | {readonly kind:'before_registration';readonly privateIdentity:GitWorktreeObjectIdentity}
  | ({readonly kind:'before_worktree_index';readonly targetOID:string} & GitWorktreeRegistrationWitness)
  | ({readonly kind:'registered';readonly index:GitWorktreeIndexWitness} & GitWorktreeRegistrationWitness)
  | {readonly kind:'before_quarantine'|'before_admin_relocation'|'admin_relocated';readonly originalOperationID:string;readonly originalVersionID:string;readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly worktreeIdentity:GitWorktreeObjectIdentity;readonly commonGitIdentity:GitWorktreeObjectIdentity;readonly worktreesIdentity:GitWorktreeObjectIdentity;readonly targetOID:string;readonly treeOID:string;readonly marker:GitWorktreeRegistrationWitness['marker'];readonly backlink:GitWorktreeRegistrationFileWitness;readonly head:GitWorktreeRegistrationFileWitness;readonly index:GitWorktreeIndexWitness;readonly locks:{readonly head:GitWorktreeObjectIdentity;readonly backlink:GitWorktreeObjectIdentity;readonly index:GitWorktreeObjectIdentity}}
  | {readonly kind:'before_admin_restore'|'before_tree_restore'|'redo_restored';readonly inverseOperationID:string;readonly inverseVersionID:string;readonly adminName:string;readonly adminIdentity:GitWorktreeObjectIdentity;readonly worktreeIdentity:GitWorktreeObjectIdentity;readonly commonGitIdentity:GitWorktreeObjectIdentity;readonly worktreesIdentity:GitWorktreeObjectIdentity;readonly targetOID:string;readonly treeOID:string;readonly marker:GitWorktreeRegistrationWitness['marker'];readonly backlink:GitWorktreeRegistrationFileWitness;readonly head:GitWorktreeRegistrationFileWitness;readonly index:GitWorktreeIndexWitness;readonly locks:{readonly head:GitWorktreeObjectIdentity;readonly backlink:GitWorktreeObjectIdentity;readonly index:GitWorktreeObjectIdentity}}
  | {readonly kind:'status'}}
export type GitWorktreePhaseResult =
  | {readonly state:'proceed'}
  | {readonly state:'published'}
  | {readonly state:'quarantined'|'admin_relocated'|'admin_restored'|'tree_restored'}
  | {readonly state:'completed'}
  | {readonly state:'registered'}
  | {readonly state:'accepted';readonly nextOrdinal:number;readonly nextOffset:number}
  | {readonly state:'sealed';readonly targetManifestSHA256:string}
  | {readonly state:'status';readonly stage:'pending'|'unknown'}
  | {readonly state:'status';readonly stage:'sealed';readonly targetManifestSHA256:string}
  | {readonly state:'refused'|'unknown'}
export type GitCheckoutObservation = {readonly indexSHA256:string;readonly headRef:string|null;readonly headOID:string}
export type GitCheckoutPhaseRequest = Omit<GitCommitRecheckRequest,'phase'> & {readonly gitPlanDigest:string;readonly filesRequestDigest:string;readonly event:
  | {readonly kind:'prepare'}
  | {readonly kind:'step';readonly ordinal:number}
  | {readonly kind:'before_ref';readonly indexSHA256:string}
  | ({readonly kind:'failure';readonly index:'unchanged'|'restored';readonly ref:'unchanged'|'restored'} & GitCheckoutObservation)
  | {readonly kind:'rollback';readonly ordinal:number}
  | ({readonly kind:'finalize'} & GitCheckoutObservation)}
export type GitCheckoutPhaseResult = {readonly state:'ready'|'applied'|'accepted'|'restored'|'complete'|'refused'|'unknown'}
export type GitPushRecheckRequest = Omit<GitCommitRecheckRequest,'phase'> & {readonly phase:'before_push'}
export type GitPushFinishRequest = Omit<GitOperationFinishRequest,'result'> & {readonly result:{readonly state:'applied'|'refused'|'unknown';readonly remote:'not_issued'|'accepted'|'unknown';readonly cleanup:'complete'|'unknown'}}
export type GitFetchFinishRequest = Omit<GitOperationFinishRequest,'result'> & {readonly result:{readonly state:'applied'|'refused'|'unknown';readonly transfer:'not_issued'|'completed'|'unknown';readonly ref:'not_issued'|'applied'|'unknown';readonly cleanup:'complete'|'unknown'}}
export type GitOperationRecheckRequest = Omit<GitCommitRecheckRequest,'phase'> & {readonly phase:'before_lock'|'before_publish'}
export type GitOperationFinishRequest = Omit<GitCommitFinishRequest,'result'> & {readonly result:{readonly state:'applied'|'refused'|'unknown';readonly ref:'not_issued'|'applied'|'unknown';readonly cleanup:'complete'|'unknown'}}
export interface GitCleanupRecheckRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string; readonly phase:'before_close'|'before_retire'|'before_remove'
}
export interface GitCleanupFinishRequest {
  readonly version:1; readonly operationID:string; readonly requestDigest:string; readonly capability:string
  readonly invocationID:string; readonly actionSHA256:string
  readonly result:{readonly state:'applied'|'refused'|'unknown';readonly retirement:'not_issued'|'applied'|'unknown';readonly removal:'not_issued'|'applied'|'unknown';readonly child:'not_issued'|'settled'|'unknown';readonly branch:'present'|'unknown'}
}

export interface EffectiveToolProjection {
  readonly blockAllToolCalls: boolean
  readonly disabledTools: readonly string[]
  readonly hiddenTools: readonly string[]
  readonly allowedTools: readonly string[] | null
}

export interface SessionInputPolicyReceipt {
  readonly requestID: string
  readonly payloadHash: string
  readonly executionRevision: number
  readonly mapRevision: number
  readonly mappingRevision: number
  readonly workspaceRevision: number
  readonly text: string
  readonly turnPolicy: {
    readonly version: 1
    readonly mode: "agent" | "chat" | "plan"
    readonly allowBash: boolean | null
    readonly allowWebSearch: boolean | null
    readonly useWeb: boolean
    readonly useResearch: boolean
    readonly compareMode: boolean
    readonly incognito: boolean
    readonly noMemory: boolean
  } | null
  readonly admissionCeiling: EffectiveToolProjection | null
  readonly queuedSourceProof?: string
}

export interface SessionInputPolicyDecision extends Omit<SessionInputPolicyReceipt, "text" | "turnPolicy" | "admissionCeiling" | "queuedSourceProof"> {
  readonly state: "ready" | "denied" | "unavailable"
  readonly reason?: string
  readonly choiceSnapshot: SessionInputPolicyReceipt["turnPolicy"]
  readonly guideOnly: boolean
  readonly toolProjection: EffectiveToolProjection
}

export interface SessionInputPolicyResolveRequest extends ManagedTurnIdentity {
  readonly receipts: readonly SessionInputPolicyReceipt[]
}

export interface SessionInputPolicyResolveResult extends ManagedTurnIdentity {
  readonly decisions: readonly SessionInputPolicyDecision[]
  readonly batchDisposition: "ready" | "unavailable"
  readonly batchMode: "agent" | "chat" | "plan" | null
  readonly batchGuideOnly: boolean
  readonly toolProjection: EffectiveToolProjection
  /** Aggregate canonical host-to-native alias/pattern projection. */
  readonly nativeToolPolicy: Readonly<Record<string, boolean>>
}

export interface InputOriginReceipt {
  readonly owner:string; readonly stableChatID:string; readonly sessionID:string; readonly actorID:string; readonly requestID:string
  readonly payloadHash:string; readonly mapRevision:number; readonly mappingRevision:number; readonly workspaceRevision:number; readonly executionRevision:number
  readonly messageID:string; readonly queueReceiptID:string; readonly deliveryMessageID:string; readonly firstIngressNonce?:string
}
export type InputOriginSyncRequest = ManagedTurnIdentity & ({readonly phase:"invalidate";readonly sourceSetGeneration:number} |
  {readonly phase:"publish";readonly sourceSetGeneration:number;readonly sourceSetSHA256:string;readonly receipts:readonly InputOriginReceipt[]} & ({readonly rootMessageID:string;readonly rootPartID:string;readonly rootTextSHA256:string}|{readonly rootMessageID?:never;readonly rootPartID?:never;readonly rootTextSHA256?:never}))
export interface InputOriginSyncResult {readonly accepted:boolean;readonly sourceSetGeneration:number;readonly sourceSetSHA256?:string}
export type EvolveOperateRequest = ManagedTurnIdentity & {readonly toolCallID:string;readonly projectID:string;readonly sourceSetGeneration:number;readonly sourceSetSHA256:string} & (
  {readonly operation:"preview"|"apply";readonly modeRevision:number;readonly policyRevision:number;readonly expectedContractHash:string;
   readonly candidateText:string;readonly candidateSHA256:string;readonly reason:string;readonly trigger:{readonly kind:"agent_proposal"}|{readonly kind:"owner_request"|"tool_outcome";readonly ref:string}} |
  {readonly operation:"reconcile"|"rollback"|"rollback-reconcile";readonly operationID:string})
export interface EvolveOperateResult {
  readonly state:"preview_only"|"complete"|"rolled_back"|"incomplete"|"unavailable";readonly operationID?:string;readonly transitionID?:string;
  readonly historyActionID?:string;readonly contractHash?:string;readonly candidateSHA256?:string;readonly modeRevision?:number;readonly policyRevision?:number;
  readonly diff?:string;readonly reason?:string;readonly triggerEvidence?:"agent_proposal_unverified"
}

export type MemoryInferredIdentity = ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly argsSHA256:string}
export type MemoryInferredProposalRequest = MemoryInferredIdentity & {readonly kind:"fact"|"contact"|"project"|"goal"|"identity"|"preference"|"task"|"unknown";readonly text:string}
export type MemoryInferredStatusRequest = MemoryInferredIdentity & {readonly operationID:string}
export type MemoryInferredProposalResult = {readonly state:"pending"|"duplicate";readonly operationID:string;readonly candidateID:string} |
  {readonly state:"conflict";readonly operationID:string;readonly candidateID:string;readonly conflictID:string} |
  {readonly state:"unavailable"} | {readonly state:"outcome_unknown";readonly operationID:string}
export type MemoryInferredStatusResult = {readonly state:"committed";readonly operationID:string;readonly bodySHA256:string;readonly candidateID:string;readonly disposition:"staged"|"duplicate"|"conflict";readonly conflictID?:string} |
  {readonly state:"terminal"|"unknown";readonly operationID:string} | {readonly state:"unavailable"}

export type WorkflowArtifactRevision = {readonly kind:"hostFingerprint";readonly value:string}
export type WorkflowArtifactRole = "metaplan"|"ledger"|"concern"|"checkpoint"|"progress"|"slice"
export type WorkflowArtifactIdentity = ManagedTurnIdentity & {readonly sourceToken:string;readonly messageID:string;readonly toolCallID:string;readonly argsSHA256:string;readonly operationID:string;readonly role:WorkflowArtifactRole;readonly name:string|null}
export type WorkflowArtifactRequest = WorkflowArtifactIdentity & {readonly expectedRevision:WorkflowArtifactRevision|null;readonly text:string}
export type WorkflowArtifactReadRequest = Omit<WorkflowArtifactIdentity,"operationID">
export type WorkflowSnapshotItem = ({readonly state:"found";readonly revision:WorkflowArtifactRevision;readonly text:string;readonly textSHA256:string} | {readonly state:"not_found"|"unknown"|"unavailable"}) & {readonly operation?:{readonly operationID:string;readonly state:"committed"|"not_applied"|"unknown"}}
export type WorkflowSnapshotTaskRef = {readonly sessionID:string;readonly taskID:string;readonly assignmentEventID:number}
export type WorkflowSnapshotReadResult = {readonly checkpoint:WorkflowSnapshotItem;readonly notes:WorkflowSnapshotItem;readonly progress:{readonly items:readonly (WorkflowSnapshotItem & {readonly taskRef:WorkflowSnapshotTaskRef})[];readonly complete:boolean}}
export type WorkflowArtifactReadResult = {readonly state:"found";readonly resourceRef:string;readonly revision:WorkflowArtifactRevision;readonly text:string} | {readonly state:"not_found"|"unknown"|"unavailable"}
export type WorkflowArtifactResult = {readonly state:"complete";readonly operationID:string;readonly resourceRef:string;readonly revision:WorkflowArtifactRevision} | {readonly state:"conflict"|"unavailable"|"unknown"|"partial";readonly operationID:string}

export interface MethodRequestMap {
  "_openclank/session/v1/memory/manage": {identity: ManagedTurnIdentity; toolCallID:string; messageID:string; arguments:Record<string,unknown>}
  "_openclank/session/v1/memory/manage-rag": {identity:ManagedTurnIdentity;messageID:string;toolCallID:string;args:Record<string,string>}
  "_openclank/session/v1/memory/search": {identity: ManagedTurnIdentity; invocationID:string|null; search:Record<string,unknown>}
  "_openclank/session/v1/git-worktree/phase": GitWorktreePhaseRequest
  "_openclank/session/v1/git-checkout/phase": GitCheckoutPhaseRequest
  "_openclank/session/v1/git-push/recheck": GitPushRecheckRequest
  "_openclank/session/v1/git-push/finish": GitPushFinishRequest
  "_openclank/session/v1/git-fetch/recheck": GitFetchRecheckRequest
  "_openclank/session/v1/git-fetch/finish": GitFetchFinishRequest
  "_openclank/session/v1/workflow/snapshot/read": ManagedTurnIdentity
  "_openclank/session/v1/git-operation/recheck": GitOperationRecheckRequest
  "_openclank/session/v1/git-operation/finish": GitOperationFinishRequest
  "_openclank/session/v1/workflow/artifact/read": WorkflowArtifactReadRequest
  "_openclank/session/v1/workflow/artifact": WorkflowArtifactRequest
  "_openclank/session/v1/workflow/artifact/status": WorkflowArtifactIdentity
  "_openclank/session/v1/git-index/recheck": GitIndexRecheckRequest
  "_openclank/session/v1/git-commit/recheck": GitCommitRecheckRequest
  "_openclank/session/v1/git-commit/finish": GitCommitFinishRequest
  "_openclank/session/v1/git-cleanup/recheck": GitCleanupRecheckRequest
  "_openclank/session/v1/git-cleanup/finish": GitCleanupFinishRequest
  "_openclank/session/v1/git-index/finish": GitIndexFinishRequest
  "_openclank/provider-store/v1/actor-route/preflight": ActorRoutePreflightRequest
  "_openclank/session/v1/execution/finish": ManagedExecutionFinishRequest
  "_openclank/session/v1/tool/admit": ManagedToolAdmitRequest
  "_openclank/session/v1/input-origin/sync": InputOriginSyncRequest
  "_openclank/session/v1/memory/inferred-proposal": MemoryInferredProposalRequest
  "_openclank/session/v1/memory/inferred-proposal/status": MemoryInferredStatusRequest
  "_openclank/session/v1/evolve/operate": EvolveOperateRequest
  "_openclank/session/v1/recall/admitted": ManagedRecallAdmittedRequest
  "_openclank/session/v1/dispatch/admit": ManagedTurnIdentity
  "_openclank/session/v1/worker-naming/request": ManagedTurnIdentity & {readonly version:1;readonly actorID:"main";readonly messageID:string;readonly toolCallID:string;readonly childSessionID:string;readonly childActorID:string;readonly childExecutionRevision:number;readonly displayRevision:number;readonly sourceToken:string}
  "_openclank/session/v1/turn/start": ManagedTurnIdentity
  "_openclank/session/v1/turn/status": ManagedTurnIdentity & { readonly rootSessionID?: string }
  "_openclank/session/v1/turn/finish": ManagedTurnFinishRequest
  "_openclank/session/v1/maintenance/stage": ManagedMaintenanceStageRequest
  "_openclank/session/v1/mcp/health": ManagedTurnIdentity
  "_openclank/session/v1/maintenance/summary-read": ManagedMaintenanceSummaryReadRequest
  "_openclank/session/v1/maintenance/ack": ManagedMaintenanceAckRequest
  "_openclank/session/v1/maintenance/source": ManagedMaintenanceSourceRequest
  "_openclank/session/v1/attachment/source": ManagedAttachmentSourceRequest
  "_openclank/session/v1/code-activity/observe": ManagedTurnIdentity & {readonly toolCallID:string;readonly resultSHA256:string;readonly resource:Record<string,unknown>|null}
  "_openclank/session/v1/native-analysis/source": ManagedTurnIdentity & {readonly callID:string} & ({readonly operation:'eligibility';readonly messageID:string} | {readonly operation:'capture';readonly sourceRef:string;readonly expectedRevision:{readonly kind:string;readonly value:string};readonly messageID?:string;readonly inputSHA256?:string;readonly analysisOperation?:'functions'|'info'|'strings'|'disassembly'|'xrefs_to_entry'|'decompile'|'xrefs_from_instruction';readonly maxFunctions?:number;readonly maxStrings?:number;readonly functionTarget?:{readonly dossier_id:string;readonly expected_dossier_revision:{readonly kind:string;readonly value:string};readonly selector:{readonly name:string;readonly address:number;readonly size:number;readonly addressSpace:'provider_virtual'};readonly max_instructions:number};readonly xrefTarget?:{readonly dossier_id:string;readonly expected_dossier_revision:{readonly kind:string;readonly value:string};readonly selector:{readonly name:string;readonly address:number;readonly size:number;readonly addressSpace:'provider_virtual'};readonly max_references:number};readonly decompileTarget?:{readonly dossier_id:string;readonly expected_dossier_revision:{readonly kind:string;readonly value:string};readonly selector:{readonly name:string;readonly address:number;readonly size:number;readonly addressSpace:'provider_virtual'}};readonly decompileProfile?:{readonly providerSHA256:string;readonly pluginSHA256:string;readonly rootSelectorSHA256:string;readonly manifestSHA256:string|null};readonly instructionTarget?:{readonly dossier_id:string;readonly expected_dossier_revision:{readonly kind:string;readonly value:string};readonly selector:{readonly address:number;readonly size:number;readonly opcode:string;readonly addressSpace:'provider_virtual'};readonly max_references:number}} | {readonly operation:'finalize';readonly captureToken:string;readonly sourceSHA256:string;readonly messageID?:string;readonly inputSHA256?:string} | {readonly operation:'stage';readonly captureToken:string;readonly sourceSHA256:string;readonly messageID:string;readonly inputSHA256:string;readonly analysisOperation:'functions'|'info'|'strings'|'disassembly'|'xrefs_to_entry'|'decompile'|'xrefs_from_instruction';readonly providerSHA256:string;readonly providerVersion:string;readonly outputSHA256:string;readonly outputBase64:string})
  "_openclank/session/v1/queued-source/fence": { readonly source:Record<string,unknown>; readonly preimage:Record<string,unknown>; readonly mutation:"part_edit"|"part_delete"|"holder_retire" }
  "_openclank/session/v1/python-kernel/call": ManagedTurnIdentity & { readonly callID:string; readonly request: { readonly operation:string; readonly [key:string]:unknown } }
  "_openclank/session/v1/files/mutate": ManagedTurnIdentity & { readonly messageID:string; readonly toolCallID:string; readonly toolName:string; readonly argsSHA256:string } & ({ readonly args: { readonly action:"move"; readonly path:string; readonly destination:string; readonly expectedFingerprint:string|null } | { readonly action:"trash"; readonly path:string; readonly destination:null; readonly expectedFingerprint:string|null }; readonly phase?:never } | { readonly args: { readonly action:"write"; readonly path:string; readonly content:string; readonly expectedFingerprint:string|null } | { readonly action:"edit"; readonly path:string; readonly oldText:string; readonly newText:string; readonly replaceAll:boolean; readonly expectedFingerprint:string|null }; readonly phase:"prepare"|"apply"|"status"|"abort" })
  "_openclank/session/v1/background-job/start": ManagedTurnIdentity & { readonly messageID:string; readonly toolCallID:string; readonly toolName:string; readonly argsSHA256:string; readonly args:{readonly action:"start"; readonly command:string; readonly max_runtime_s:number} }
  "_openclank/session/v1/task/create": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"create";readonly task_type:"llm";readonly name:string;readonly prompt:string} & ({readonly schedule:"daily";readonly scheduled_time:string}|{readonly schedule:"weekly"|"monthly";readonly scheduled_time:string;readonly scheduled_day:number}|{readonly schedule:"once";readonly scheduled_date:string})}
  "_openclank/session/v1/task/edit": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly mode:"apply"|"status";readonly args:{readonly action:"edit";readonly task_id:string;readonly expected_revision:string;readonly name?:string;readonly prompt?:string}}
  "_openclank/session/v1/task/run": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly mode:"apply"|"status";readonly args:{readonly action:"run";readonly task_id:string;readonly expected_revision:string}}
  "_openclank/session/v1/task/control": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly mode:"apply"|"status";readonly args:{readonly action:"pause"|"resume"|"delete";readonly task_id:string;readonly expected_revision:string}}
  "_openclank/session/v1/task/read": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"list"|"inspect";readonly task_id:string|null;readonly limit:number;readonly cursor:string|null}}
  "_openclank/session/v1/task/status": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"create";readonly task_type:"llm";readonly name:string;readonly prompt:string} & ({readonly schedule:"daily";readonly scheduled_time:string}|{readonly schedule:"weekly"|"monthly";readonly scheduled_time:string;readonly scheduled_day:number}|{readonly schedule:"once";readonly scheduled_date:string})}
  "_openclank/session/v1/background-job/read": ManagedTurnIdentity & { readonly messageID:string; readonly toolCallID:string; readonly toolName:string; readonly argsSHA256:string; readonly args:{readonly action:"list"|"poll"|"tail"|"wait";readonly job_id:string|null;readonly cursor:number;readonly limit:number;readonly timeout_s:number} }
  "_openclank/session/v1/background-job/terminate": ManagedTurnIdentity & { readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"terminate";readonly job_id:string} }
  "_openclank/session/v1/background-job/write": ManagedTurnIdentity & { readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"write";readonly job_id:string;readonly data:string} }
  "_openclank/session/v1/background-job/retire": ManagedTurnIdentity & { readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly args:{readonly action:"retire";readonly job_id:string} }
  "_openclank/session/v1/shared-text/call": ManagedSharedTextRequest
  "_openclank/session/v1/media-job/call": ManagedMediaJobRequest
  "_openclank/session/v1/actor-frame/call": ManagedTurnIdentity & {readonly messageID:string;readonly toolCallID:string;readonly toolName:string;readonly argsSHA256:string;readonly argsJSON:string;readonly mode:"issue"|"status"|"cancel"}
  "_openclank/session/v1/theme-emoji/call": ManagedThemeEmojiRequest
  "_openclank/session/v1/context/augment": ManagedContextAugmentRequest
  "_openclank/session/v1/memory/prepared": ManagedPreparedReadRequest
  "_openclank/session/v1/skill/metadata-current": SkillMetadataCurrentRequest
  "_openclank/session/v1/context/delivered": ManagedContextDeliveredRequest
  "_openclank/session/v1/queued-source/read": {readonly owner:string;readonly stableChatID:string;readonly sessionID:string;readonly requestID:string}
  "_openclank/session/v1/queued-source/forward": ManagedTurnIdentity & {readonly sources:readonly {readonly requestID:string;readonly payloadHash:string;readonly proofSHA256:string;readonly bodySHA256:string;readonly messageID:string;readonly partID:string}[]}
  "_openclank/session/v1/input-policy/resolve": SessionInputPolicyResolveRequest
  "_openclank/session/v1/code-context/read": SessionCodeContextRequest
  "_openclank/session/v1/goal/completed": GoalCompletionRequest
  "_openclank/logging/v1/admit": LoggingAdmissionRequest
  "_openclank/logging/v1/events": LoggingEventRequest
  "_openclank/history/v1/query": HistoryQueryRequest
  "_openclank/history/v1/mutate": HistoryMutationRequest
  "_openclank/provider-store/v1/account/bind": AccountSelection.SelectionRequest
  "_openclank/provider-store/v1/account/commit": AccountCommitRequest
  "_openclank/provider-store/v1/account/attempt": AccountAttemptRequest | AccountTerminalRequest | CrossModelDispatchRequest
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
  "_openclank/session/v1/memory/manage": {state:"complete"|"rejected"|"unavailable"|"outcome_unknown"|"review_required"; text:string; operationID?:string}
  "_openclank/session/v1/memory/manage-rag": {state:"complete"|"refused"|"unknown";output:string}
  "_openclank/session/v1/memory/search": {qualified_text:string; admission_attestation:{attestationID:string;resultSHA256:string}|null}
  "_openclank/session/v1/git-worktree/phase": GitWorktreePhaseResult
  "_openclank/session/v1/git-checkout/phase": GitCheckoutPhaseResult
  "_openclank/session/v1/git-push/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-push/finish": {readonly accepted:boolean}
  "_openclank/session/v1/git-fetch/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-fetch/finish": {readonly accepted:boolean}
  "_openclank/session/v1/workflow/snapshot/read": WorkflowSnapshotReadResult
  "_openclank/session/v1/git-operation/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-operation/finish": {readonly accepted:boolean}
  "_openclank/session/v1/workflow/artifact/read": WorkflowArtifactReadResult
  "_openclank/session/v1/workflow/artifact": WorkflowArtifactResult
  "_openclank/session/v1/worker-naming/request": {readonly version:1;readonly state:"named";readonly summary:string} | {readonly version:1;readonly state:"skipped"|"unknown"}
  "_openclank/session/v1/workflow/artifact/status": WorkflowArtifactResult
  "_openclank/session/v1/git-index/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-commit/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-commit/finish": {readonly accepted:boolean}
  "_openclank/session/v1/git-cleanup/recheck": {readonly valid:boolean}
  "_openclank/session/v1/git-cleanup/finish": {readonly accepted:boolean}
  "_openclank/session/v1/git-index/finish": {readonly accepted:boolean}
  "_openclank/provider-store/v1/actor-route/preflight": ActorRoutePreflightResult
  "_openclank/session/v1/execution/finish": ManagedExecutionFinishResult
  "_openclank/session/v1/tool/admit": ManagedToolAdmitResult
  "_openclank/session/v1/recall/admitted": ManagedRecallAdmittedResult
  "_openclank/session/v1/dispatch/admit": { readonly state: "current" | "stale" }
  "_openclank/session/v1/turn/start": ManagedTurnStartResult
  "_openclank/session/v1/turn/status": ManagedTurnStatusResult
  "_openclank/session/v1/turn/finish": ManagedTurnFinishResult
  "_openclank/session/v1/maintenance/stage": ManagedRecallAdmittedResult
  "_openclank/session/v1/mcp/health": ManagedMcpHealthResult
  "_openclank/session/v1/maintenance/summary-read": ManagedMaintenanceSummaryReadResult
  "_openclank/session/v1/maintenance/ack": ManagedRecallAdmittedResult
  "_openclank/session/v1/maintenance/source": ManagedMaintenanceSourceResult
  "_openclank/session/v1/attachment/source": ManagedAttachmentSourceResult
  "_openclank/session/v1/code-activity/observe": {readonly state:"unavailable"} | Record<string,unknown>
  "_openclank/session/v1/native-analysis/source": {readonly state:'unavailable'|'current'|'eligible'|'legacy'} | {readonly state:'captured';readonly captureToken:string;readonly sourceSHA256:string;readonly revision:{readonly kind:string;readonly value:string};readonly size:number;readonly bytesBase64:string;readonly functionTarget?:{readonly dossierID:string;readonly dossierRevision:{readonly kind:string;readonly value:string};readonly selector:{readonly name:string;readonly address:number;readonly size:number;readonly addressSpace:'provider_virtual'};readonly maxInstructions?:number;readonly maxReferences?:number;readonly resultSHA256:string};readonly instructionTarget?:{readonly dossierID:string;readonly dossierRevision:{readonly kind:string;readonly value:string};readonly instruction:{readonly address:number;readonly size:number;readonly opcode:string;readonly addressSpace:'provider_virtual'};readonly maxReferences:number;readonly resultSHA256:string}} | {readonly state:'staged';readonly provisional:string}
  "_openclank/session/v1/queued-source/fence": { readonly state:"unavailable" } | { readonly state:"fenced"; readonly source:Record<string,unknown>; readonly preimage:Record<string,unknown>; readonly fence:Record<string,unknown> }
  "_openclank/session/v1/python-kernel/call": { readonly ok:boolean; readonly result:Record<string,unknown> | null }
  "_openclank/session/v1/files/mutate": {readonly state:"applied";readonly action:"move";readonly source:string;readonly destination:string} | {readonly state:"applied";readonly action:"trash";readonly entry:{readonly id:string;readonly rootID:string;readonly originalPath:string;readonly trashedPath:string;readonly fingerprint:string|null}} | {readonly state:"applied";readonly action:"write"|"edit";readonly actionRef:string;readonly fingerprint:string;readonly file:{readonly path:string;readonly mediaType:string;readonly bytes:number;readonly lines:number;readonly newline:"lf"|"crlf"|"cr"}} | {readonly state:"prepared";readonly operationID:string;readonly actionID:string;readonly source:{readonly accountID:string;readonly authorityWorkspaceID:string;readonly hostChatID:string;readonly sessionID:string;readonly actorID:string;readonly executionRevision:number;readonly rootOperationID:string;readonly innerTurnID:string;readonly toolCallID:string;readonly assistantMessageID:string};readonly capture:{readonly historyActorID:string;readonly historyWorkspaceID:string}} | {readonly state:"refused"|"unknown"}
  "_openclank/session/v1/background-job/start": { readonly ok:boolean; readonly result:{readonly output:string; readonly exit_code:number; readonly session:{readonly id:string;readonly state:string}} | null }
  "_openclank/session/v1/task/create": {readonly ok:boolean;readonly result:{readonly taskID:string;readonly state:"created"|"already_created"|"unknown"}|null}
  "_openclank/session/v1/task/edit": {readonly ok:boolean;readonly result:{readonly original:"applied"|"refused"|"unknown";readonly changed:ReadonlyArray<"name"|"prompt">}|null}
  "_openclank/session/v1/task/run": {readonly ok:boolean;readonly result:{readonly original:"queued"|"refused"|"unknown";readonly runID:string|null;readonly state:string|null}|null}
  "_openclank/session/v1/task/control": {readonly ok:boolean;readonly result:{readonly original:"applied"|"refused"|"unknown";readonly action:"pause"|"resume"|"delete";readonly currentStatus:string|null}|null}
  "_openclank/session/v1/task/read": {readonly ok:boolean;readonly result:{readonly state:"partial";readonly items:ReadonlyArray<{readonly taskID:string;readonly name:string;readonly status:string|null;readonly taskType:string|null;readonly schedule:string|null;readonly scheduledTime:string|null;readonly scheduledDay:number|null;readonly scheduledDate:string|null;readonly prompt:string|null;readonly expected_revision:string|null}>;readonly cursor:string|null}|null}
  "_openclank/session/v1/task/status": {readonly ok:boolean;readonly result:{readonly taskID:string;readonly state:"created"|"already_created"|"unknown"}|null}
  "_openclank/session/v1/background-job/read": {readonly ok:boolean;readonly result:{readonly action:"list"|"poll"|"tail"|"wait";readonly items:ReadonlyArray<{readonly jobID:string;readonly status:string}>|null;readonly job:{readonly jobID:string;readonly status:string}|null;readonly frame:{readonly jobID:string;readonly status:string;readonly cursor:number;readonly nextCursor:number;readonly eof:boolean;readonly output:string}|null;readonly timed_out:boolean|null;readonly partial:boolean|null}|null}
  "_openclank/session/v1/background-job/terminate": {readonly ok:boolean;readonly result:{readonly state:"requested"|"already_requested"|"unknown"|"unavailable"}|null}
  "_openclank/session/v1/background-job/write": {readonly ok:boolean;readonly result:{readonly state:"accepted"|"unknown"|"unavailable"}|null}
  "_openclank/session/v1/background-job/retire": {readonly ok:boolean;readonly result:{readonly state:"retired"|"already_retired"|"unknown"|"unavailable"}|null}
  "_openclank/session/v1/shared-text/call": ManagedSharedTextResult
  "_openclank/session/v1/media-job/call": ManagedMediaJobResult
  "_openclank/session/v1/actor-frame/call": {readonly ok:boolean;readonly result?:{readonly state:"captured"|"unknown"|"unavailable"|"no_frame";readonly operation_id:string;readonly resource_ref?:string;readonly sha256?:string;readonly pixel_size?:readonly number[]}|null}
  "_openclank/session/v1/theme-emoji/call": ManagedThemeEmojiResult
  "_openclank/session/v1/context/augment": ManagedContextAugmentResult
  "_openclank/session/v1/memory/prepared": ManagedPreparedReadResult
  "_openclank/session/v1/skill/metadata-current": {readonly state:'ready'|'unavailable'}
  "_openclank/session/v1/context/delivered": { readonly accepted:boolean }
  "_openclank/session/v1/queued-source/read": {readonly state:"unavailable"}|{readonly state:"ready";readonly requestID:string;readonly text:string;readonly proof:string;readonly payloadHash:string;readonly executionRevision:number;readonly mappingRevision:number;readonly workspaceRevision:number;readonly turnPolicy:SessionInputPolicyReceipt["turnPolicy"];readonly messageID:string|null;readonly partID:string|null;readonly mutationPreimage?:{readonly version:1;readonly owner:string;readonly accountID:string;readonly stableChatID:string;readonly sessionID:string;readonly requestID:string;readonly messageID:string;readonly partID:string;readonly receiptSHA256:string;readonly messageSHA256:string;readonly partSHA256:string;readonly enrollmentSHA256:string}}
  "_openclank/session/v1/queued-source/forward": {readonly accepted:boolean}
  "_openclank/session/v1/input-policy/resolve": SessionInputPolicyResolveResult
  "_openclank/session/v1/input-origin/sync": InputOriginSyncResult
  "_openclank/session/v1/memory/inferred-proposal": MemoryInferredProposalResult
  "_openclank/session/v1/memory/inferred-proposal/status": MemoryInferredStatusResult
  "_openclank/session/v1/evolve/operate": EvolveOperateResult
  "_openclank/session/v1/code-context/read": SessionCodeContextResult
  "_openclank/session/v1/goal/completed": { accepted: true; replayed: boolean }
  "_openclank/logging/v1/admit": LoggingAdmissionResult
  "_openclank/logging/v1/events": { accepted: boolean; replayed: boolean; captureState?: string }
  "_openclank/history/v1/query": HistoryQueryResult
  "_openclank/history/v1/mutate": HistoryMutationResult
  "_openclank/provider-store/v1/account/bind": AccountSelection.Binding
  "_openclank/provider-store/v1/account/commit": AccountSelection.Binding | CrossModelAlternativeCommittedResult
  "_openclank/provider-store/v1/account/attempt": AccountSelection.Binding | AccountTerminalResult | CrossModelAlternativeReadyResult
  "_openclank/provider-store/v1/credential/lease": CredentialLeaseResult | CrossModelAlternativeIssueResult
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
  "_openclank/session/v1/queued-source/markers": {readonly owner:string;readonly sessionID:string;readonly after:readonly [string,string,string]|null;readonly limit:number}
  "_openclank/session/v1/queued-source/retire-marker": {readonly sessionID:string;readonly messageID:string;readonly partID:string}
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
  "_openclank/session/v1/queued-source/markers": {readonly state:"unavailable"|"legacy_empty"}|{readonly state:"ready";readonly items:readonly Record<string,unknown>[];readonly nextAfter:readonly [string,string,string]|null}
  "_openclank/session/v1/queued-source/retire-marker": {readonly state:"unavailable"|"fenced"}
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
