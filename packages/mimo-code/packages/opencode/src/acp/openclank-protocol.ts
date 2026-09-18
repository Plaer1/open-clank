import type { Auth } from "@/auth"
import type { AccountSelection } from "@/provider/account-selection"

export const PROTOCOL_VERSION = 1 as const
export const PROVIDER_STORE_VERSION = 1 as const
export const OPERATION_ROUTER_VERSION = 1 as const
export const SCHEMA_VERSION = 1 as const
export const SCHEMA_ID = "https://openclank.dev/contracts/managed-provider/v1" as const
// SHA-256 of contracts/openclank/managed-provider-v1.schema.json.
export const SCHEMA_HASH = "f9b9c7eb4dd50fa5d00f65f3662c9e9aa2702de63de94ada1e13321d131d0bbb" as const

export const PROVIDER_STORE_METHODS = [
  "_openclank/provider-store/v1/account/bind",
  "_openclank/provider-store/v1/account/commit",
  "_openclank/provider-store/v1/account/attempt",
  "_openclank/provider-store/v1/credential/lease",
  "_openclank/provider-store/v1/credential/replace",
  "_openclank/provider-store/v1/refresh/acquire",
  "_openclank/provider-store/v1/refresh/renew",
  "_openclank/provider-store/v1/refresh/commit",
  "_openclank/provider-store/v1/refresh/abort",
] as const

export const PROVIDER_CONTROL_METHODS = [
  "_openclank/provider-control/v1/catalog",
  "_openclank/provider-control/v1/connection/validate",
  "_openclank/provider-control/v1/account/validate",
  "_openclank/provider-control/v1/oauth/start",
  "_openclank/provider-control/v1/oauth/poll",
  "_openclank/provider-control/v1/oauth/callback",
  "_openclank/provider-control/v1/oauth/cancel",
] as const

export const OPERATION_METHODS = [
  "_openclank/operations/v1/journal/cas",
  "_openclank/operations/v1/artifact/read",
  "_openclank/operations/v1/artifact/write",
  "_openclank/operations/v1/executor/invoke",
  "_openclank/operations/v1/execute",
] as const

export const OPERATIONS = [
  "chat.stream",
  "chat.complete",
  "vision.describe",
  "image.generate",
  "image.edit",
  "image.inpaint",
  "image.img2img",
  "image.upscale",
  "image.denoise",
  "image.segment",
  "image.remove_background",
  "image.restore_face",
  "audio.synthesize",
  "audio.transcribe",
  "embeddings.create",
] as const

export type ProviderStoreMethod = (typeof PROVIDER_STORE_METHODS)[number]
export type ProviderControlMethod = (typeof PROVIDER_CONTROL_METHODS)[number]
export type OperationMethod = (typeof OPERATION_METHODS)[number]
export type ManagedMethod = ProviderStoreMethod | ProviderControlMethod | OperationMethod
export type Operation = (typeof OPERATIONS)[number]

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
  methods: [...PROVIDER_STORE_METHODS, ...PROVIDER_CONTROL_METHODS, ...OPERATION_METHODS],
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
  readonly authMethod: "api_key"
  readonly credential: Auth.Info
}

export interface AccountValidationResult {
  readonly authMethod: "api_key" | "oauth"
  readonly authClass: string
  readonly credential: Auth.Info
  readonly safeIdentity: Record<string, string>
  readonly modelRoutes: readonly ProviderModelRoute[]
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
  readonly usage?: { readonly inputTokens?: number; readonly outputTokens?: number; readonly totalTokens?: number }
  readonly modelFingerprint?: string
  readonly dimension?: number
  readonly replayed: boolean
}

export interface MethodRequestMap {
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
}

export interface MethodResultMap {
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
}

const ENGINE_METHOD_SET: ReadonlySet<string> = new Set([...PROVIDER_CONTROL_METHODS, "_openclank/operations/v1/execute"])
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
