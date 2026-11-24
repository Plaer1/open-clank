import type { OpenClankManagedProtocol } from "./openclank-protocol"

/** The only executor recipe allowed to perform managed web search. */
export const SEARCH_EXECUTOR_ID = "openclank.search.v1" as const
export const SEARCH_CONTRACT_VERSION = 1 as const

type ExecuteRequest = OpenClankManagedProtocol.OperationExecuteRequest
type ExecuteResult = OpenClankManagedProtocol.OperationExecuteResult

export interface SearchClaims {
  /** Claims only; callback authority is supplied out of band. */
  readonly sessionID: string
  readonly correlationID: string
  readonly rootOperationID: string
  readonly idempotencyKey: string
}

export interface SearchInput {
  readonly query: string
  readonly count?: number
  readonly mode?: "results" | "answer" | "read"
  readonly intent?: "web" | "image"
  readonly freshness?: "day" | "week" | "month" | "year"
  readonly locale?: string
  readonly extraction?: "none" | "text"
  readonly deadlineAt: number
}

export interface SearchDispatchRequest {
  readonly executorID: typeof SEARCH_EXECUTOR_ID
  readonly contractVersion: typeof SEARCH_CONTRACT_VERSION
  readonly action: "dispatch"
  readonly claims: SearchClaims
  readonly input: SearchInput
}

export interface SearchFinalizeRequest {
  readonly executorID: typeof SEARCH_EXECUTOR_ID
  readonly contractVersion: typeof SEARCH_CONTRACT_VERSION
  readonly action: "finalize"
  readonly claims: SearchClaims
  readonly handoffID: string
  readonly terminal: ExecuteResult
}

export interface SearchCancelRequest {
  readonly executorID: typeof SEARCH_EXECUTOR_ID
  readonly contractVersion: typeof SEARCH_CONTRACT_VERSION
  readonly action: "cancel"
  readonly claims: SearchClaims
  readonly handoffID?: string
  readonly reason?: "aborted" | "deadline" | "replaced"
}

export type SearchCallbackRequest = SearchDispatchRequest | SearchFinalizeRequest | SearchCancelRequest

export interface SearchSource {
  readonly url: string
  readonly title: string
  readonly snippet: string
  readonly content?: string | null
  readonly provider_metadata?: Record<string, unknown>
  readonly fetched_status?: string
  readonly image_url?: string | null
  readonly thumbnail_url?: string | null
}

/** Exact safe SearchResponse.as_dict() field names, with no authority fields added. */
export interface SearchResponseArtifact {
  readonly schema_version: number
  readonly query: string
  readonly caller: string
  readonly mode: string
  readonly selected_provider: string
  readonly actual_provider: string
  readonly account_scope: "deployment-service" | "model-account" | "subscription-unbound"
  readonly account_id: string
  readonly model_route?: string | null
  readonly status: string
  readonly sources: readonly SearchSource[]
  readonly total: number
  readonly cache: { readonly hit: boolean }
  readonly elapsed_ms: number
  readonly answer?: string | null
  readonly warnings: readonly string[]
  readonly error?: { readonly code: string; readonly message: string } | null
  readonly search_queries: readonly string[]
  readonly support_links: readonly string[]
  readonly web_search_usage: Readonly<Record<string, number>>
}

export type SearchArtifactResult =
  | { readonly kind: "external_result"; readonly response: SearchResponseArtifact }
  | { readonly kind: "managed_request"; readonly handoffID: string; readonly request: ExecuteRequest }
  | { readonly kind: "finalized"; readonly response: SearchResponseArtifact }
  | { readonly kind: "finalized"; readonly terminal: ExecuteResult }
  | { readonly kind: "cancelled"; readonly reason: "aborted" | "deadline" | "replaced" | "already_finalized" }

/** Trusted callback authority is deliberately not serializable or model-controlled. */
export interface TrustedSearchCallbackAuthority {
  readonly __trustedSearchCallbackAuthority: unique symbol
}

/** No-reentry seam: implementations must not call a supervisor or create another Router. */
export interface SearchDelegate {
  dispatch(request: SearchDispatchRequest, authority: TrustedSearchCallbackAuthority): Promise<SearchArtifactResult>
  finalize(request: SearchFinalizeRequest, authority: TrustedSearchCallbackAuthority): Promise<SearchArtifactResult>
  cancel(request: SearchCancelRequest, authority: TrustedSearchCallbackAuthority): Promise<SearchArtifactResult>
}

const MAX_ID = 256
const MAX_QUERY = 2000
const MAX_LOCALE = 64
const MAX_COUNT = 50

function boundedString(value: unknown, name: string, max = MAX_ID): asserts value is string {
  if (typeof value !== "string" || value.length < 1 || value.length > max) throw new Error(`invalid ${name}`)
}

function boundedText(value: unknown, name: string, max: number): asserts value is string {
  if (typeof value !== "string" || value.length > max) throw new Error(`invalid ${name}`)
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): void {
  if (Object.keys(value).some((key) => !keys.includes(key))) throw new Error("unexpected search contract field")
}

function claims(value: unknown): SearchClaims {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid search claims")
  const input = value as Record<string, unknown>
  exactKeys(input, ["sessionID", "correlationID", "rootOperationID", "idempotencyKey"])
  boundedString(input.sessionID, "sessionID")
  boundedString(input.correlationID, "correlationID")
  boundedString(input.rootOperationID, "rootOperationID")
  boundedString(input.idempotencyKey, "idempotencyKey", 128)
  if ((input.idempotencyKey as string).length < 16) throw new Error("invalid idempotencyKey")
  return input as unknown as SearchClaims
}

export function validateSearchCallbackRequest(value: unknown): SearchCallbackRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid search callback")
  const input = value as Record<string, unknown>
  exactKeys(input, ["executorID", "contractVersion", "action", "claims", "input", "handoffID", "terminal", "reason"])
  if (input.executorID !== SEARCH_EXECUTOR_ID || input.contractVersion !== SEARCH_CONTRACT_VERSION) throw new Error("invalid search recipe")
  const action = input.action
  const base = { executorID: SEARCH_EXECUTOR_ID, contractVersion: SEARCH_CONTRACT_VERSION, action, claims: claims(input.claims) } as const
  if (action === "dispatch") {
    if (["handoffID", "terminal", "reason"].some((key) => key in input)) throw new Error("dispatch has inappropriate fields")
    if (!input.input || typeof input.input !== "object" || Array.isArray(input.input)) throw new Error("invalid search input")
    const raw = input.input as Record<string, unknown>
    exactKeys(raw, ["query", "count", "mode", "intent", "freshness", "locale", "extraction", "deadlineAt"])
    boundedString(raw.query, "query", MAX_QUERY)
    if (raw.count !== undefined && (!Number.isSafeInteger(raw.count) || (raw.count as number) < 1 || (raw.count as number) > MAX_COUNT)) throw new Error("invalid count")
    if (!Number.isFinite(raw.deadlineAt) || (raw.deadlineAt as number) <= 0) throw new Error("invalid deadline")
    if (raw.mode !== undefined && !["results", "answer", "read"].includes(raw.mode as string)) throw new Error("invalid mode")
    if (raw.intent !== undefined && !["web", "image"].includes(raw.intent as string)) throw new Error("invalid intent")
    if (raw.freshness !== undefined && !["day", "week", "month", "year"].includes(raw.freshness as string)) throw new Error("invalid freshness")
    if (raw.extraction !== undefined && !["none", "text"].includes(raw.extraction as string)) throw new Error("invalid extraction")
    if (raw.locale !== undefined) boundedString(raw.locale, "locale", MAX_LOCALE)
    if (raw.extract !== undefined) throw new Error("invalid extraction")
    return { ...base, action: "dispatch", input: raw as unknown as SearchInput }
  }
  if (action === "cancel") {
    if (["input", "terminal"].some((key) => key in input)) throw new Error("cancel has inappropriate fields")
    if (input.reason !== undefined && !["aborted", "deadline", "replaced"].includes(input.reason as string)) throw new Error("invalid cancel reason")
    if (input.handoffID !== undefined) boundedString(input.handoffID, "handoffID")
    return { ...base, action: "cancel", ...(input.handoffID === undefined ? {} : { handoffID: input.handoffID }), ...(input.reason === undefined ? {} : { reason: input.reason }) } as SearchCancelRequest
  }
  if (action === "finalize") {
    if (["input", "reason"].some((key) => key in input)) throw new Error("finalize has inappropriate fields")
    boundedString(input.handoffID, "handoffID")
    if (!input.terminal || typeof input.terminal !== "object" || Array.isArray(input.terminal)) throw new Error("invalid terminal result")
    return { ...base, action: "finalize", handoffID: input.handoffID, terminal: input.terminal as ExecuteResult }
  }
  throw new Error("invalid search action")
}

function httpURL(value: unknown, name: string): asserts value is string {
  boundedString(value, name, 2048)
  if (!/^https?:\/\//i.test(value)) throw new Error(`invalid ${name}`)
}

function object(value: unknown, name: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`invalid ${name}`)
  return value as Record<string, unknown>
}

function safeInt(value: unknown, name: string, min = 0, max = Number.MAX_SAFE_INTEGER): asserts value is number {
  if (!Number.isSafeInteger(value) || (value as number) < min || (value as number) > max) throw new Error(`invalid ${name}`)
}

function safeArray(value: unknown, name: string, max: number): unknown[] {
  if (!Array.isArray(value) || value.length > max) throw new Error(`invalid ${name}`)
  return value
}

function safeJSON(value: unknown, name: string, depth = 0): void {
  if (depth > 3) throw new Error(`invalid ${name} depth`)
  if (value === null || typeof value === "string" || typeof value === "boolean") return
  if (typeof value === "number") { if (!Number.isFinite(value)) throw new Error(`invalid ${name}`); return }
  if (Array.isArray(value)) { if (value.length > 64) throw new Error(`invalid ${name}`); value.forEach((item) => safeJSON(item, name, depth + 1)); return }
  const record = object(value, name)
  if (Object.keys(record).length > 32) throw new Error(`invalid ${name}`)
  for (const [key, item] of Object.entries(record)) { if (key.length > 128 || /account|credential|secret|token|endpoint|owner/i.test(key)) throw new Error(`unsafe ${name}`); safeJSON(item, name, depth + 1) }
}

function validatePreparedRequest(value: unknown): ExecuteRequest {
  const request = object(value, "prepared request")
  exactKeys(request, ["rootOperationID", "idempotencyKey", "operation", "routes", "input", "artifactInputs", "options"])
  boundedString(request.rootOperationID, "rootOperationID", 256)
  boundedString(request.idempotencyKey, "idempotencyKey", 128)
  if ((request.idempotencyKey as string).length < 16) throw new Error("invalid prepared idempotency key")
  if (request.operation !== "web.search") throw new Error("prepared operation mismatch")
  const routes = safeArray(request.routes, "routes", 1)
  if (routes.length !== 1) throw new Error("prepared route count")
  const route = object(routes[0], "route")
  exactKeys(route, ["connectionID", "providerID", "billingLane", "modelRouteID", "modelID", "grantID", "preferredAccountID", "inheritedAccountID"])
  for (const key of ["connectionID", "providerID", "modelRouteID", "modelID"]) boundedString(route[key], key, 256)
  for (const key of ["grantID", "preferredAccountID", "inheritedAccountID"]) if (route[key] !== undefined) boundedString(route[key], key, 256)
  if (!["subscription", "metered_api", "local", "custom", "legacy"].includes(route.billingLane as string)) throw new Error("invalid billing lane")
  const input = object(request.input, "prepared input")
  if (Object.keys(input).some((key) => !["query", "count", "freshness", "answer"].includes(key))) throw new Error("prepared input authority")
  boundedString(input.query, "query", MAX_QUERY)
  safeInt(input.count, "count", 1, MAX_COUNT)
  if (input.freshness !== undefined && !["day", "week", "month", "year"].includes(input.freshness as string)) throw new Error("invalid freshness")
  if (input.answer !== undefined && typeof input.answer !== "boolean") throw new Error("invalid answer")
  if (request.artifactInputs instanceof Array && request.artifactInputs.length !== 0) throw new Error("prepared artifacts are not allowed")
  if (!Array.isArray(request.artifactInputs)) throw new Error("invalid prepared artifacts")
  const options = object(request.options, "prepared options")
  exactKeys(options, ["deadlineMs"])
  if (options.deadlineMs !== undefined) safeInt(options.deadlineMs, "deadlineMs", 10, 120000)
  return request as unknown as ExecuteRequest
}

function validateTerminal(value: unknown): ExecuteResult {
  const terminal = object(value, "terminal")
  const allowed = ["operationID", "rootOperationID", "operation", "modelRouteID", "connectionID", "billingLane", "state", "committed", "commitReason", "bindingID", "selectedAccountID", "output", "artifacts", "usage", "metricCoverage", "coveredDispatchIDs", "lossReasons", "identityCoverage", "normalizationProfile", "quota", "modelFingerprint", "dimension", "replayed"]
  exactKeys(terminal, allowed)
  for (const key of ["operationID", "rootOperationID", "operation", "modelRouteID", "connectionID"]) boundedString(terminal[key], key, 256)
  if (terminal.operation !== "web.search") throw new Error("terminal operation mismatch")
  if (!["complete", "failed", "cancelled"].includes(terminal.state as string) || typeof terminal.committed !== "boolean" || typeof terminal.replayed !== "boolean") throw new Error("invalid terminal state")
  object(terminal.output, "terminal output")
  safeJSON(terminal.output, "terminal output")
  const artifacts = safeArray(terminal.artifacts, "terminal artifacts", 256)
  for (const artifactValue of artifacts) {
    const artifact = object(artifactValue, "terminal artifact")
    exactKeys(artifact, ["artifactID", "contentSHA256", "sizeBytes", "mediaType", "state", "expiresAt"])
    boundedString(artifact.artifactID, "artifactID", 256); boundedString(artifact.contentSHA256, "contentSHA256", 64)
    if (!/^[a-f0-9]{64}$/.test(artifact.contentSHA256 as string)) throw new Error("invalid artifact hash")
    safeInt(artifact.sizeBytes, "artifact size"); boundedString(artifact.mediaType, "media type", 255)
    if (artifact.state !== "staged" && artifact.state !== "acknowledged") throw new Error("invalid artifact state")
  }
  if (terminal.usage !== undefined) {
    const usage = object(terminal.usage, "usage")
    for (const [key, item] of Object.entries(usage)) { if (!/^(inputTokens|outputTokens|totalTokens|cacheReadTokens|cacheWriteTokens|reasoningTokens|audioInputTokens|audioOutputTokens|imageInputTokens|imageOutputTokens|searchRequests)$/.test(key)) throw new Error("unknown usage field"); safeInt(item, key) }
  }
  if (terminal.metricCoverage !== undefined) safeJSON(terminal.metricCoverage, "metric coverage")
  if (terminal.quota !== undefined) safeJSON(terminal.quota, "quota")
  if (terminal.coveredDispatchIDs !== undefined) { const ids = safeArray(terminal.coveredDispatchIDs, "covered dispatch IDs", 256); ids.forEach((id) => boundedString(id, "dispatch ID", 256)) }
  if (terminal.lossReasons !== undefined) safeJSON(terminal.lossReasons, "loss reasons")
  if (terminal.identityCoverage !== undefined && !["complete", "partial"].includes(terminal.identityCoverage as string)) throw new Error("invalid identity coverage")
  return terminal as unknown as ExecuteResult
}

/** Strictly validates the host artifact before a trusted consumer uses it. */
export function validateSearchArtifactResult(value: unknown): SearchArtifactResult {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("invalid search artifact")
  const input = value as Record<string, unknown>
  boundedString(input.kind, "kind")
  if (input.kind === "managed_request") {
    exactKeys(input, ["kind", "handoffID", "request"])
    boundedString(input.handoffID, "handoffID")
    return { ...input, request: validatePreparedRequest(input.request) } as unknown as SearchArtifactResult
  }
  if (input.kind === "cancelled") {
    exactKeys(input, ["kind", "reason"])
    if (!["aborted", "deadline", "replaced", "already_finalized"].includes(input.reason as string)) throw new Error("invalid cancellation")
    return input as unknown as SearchArtifactResult
  }
  if (input.kind !== "external_result" && input.kind !== "finalized") throw new Error("invalid artifact kind")
  exactKeys(input, input.kind === "external_result" ? ["kind", "response"] : ["kind", "response", "terminal"])
  if (input.kind === "finalized" && input.terminal !== undefined) {
    if (input.response !== undefined) throw new Error("finalized has both result forms")
    return { ...input, terminal: validateTerminal(input.terminal) } as unknown as SearchArtifactResult
  }
  const response = input.response
  if (!response || typeof response !== "object" || Array.isArray(response)) throw new Error("invalid search response")
  const body = response as Record<string, unknown>
  exactKeys(body, ["schema_version", "query", "caller", "mode", "selected_provider", "actual_provider", "account_scope", "account_id", "model_route", "status", "sources", "total", "cache", "elapsed_ms", "answer", "warnings", "error", "search_queries", "support_links", "web_search_usage"])
  for (const key of ["schema_version", "query", "caller", "mode", "selected_provider", "actual_provider", "account_scope", "account_id", "status", "sources", "total", "cache", "elapsed_ms", "warnings", "search_queries", "support_links", "web_search_usage"]) {
    if (!(key in body)) throw new Error(`missing response field: ${key}`)
  }
  if (input.kind === "external_result" && body.account_scope !== "deployment-service") throw new Error("external scope must be deployment-service")
  if (!["deployment-service", "model-account", "subscription-unbound"].includes(body.account_scope as string)) throw new Error("invalid account scope")
  safeInt(body.schema_version, "schema_version", 1)
  boundedString(body.query, "query", MAX_QUERY)
  for (const key of ["caller", "mode", "selected_provider", "account_scope", "status"]) boundedString(body[key], key, 256)
  for (const key of ["actual_provider", "account_id"]) { if (typeof body[key] !== "string" || (body[key] as string).length > 256) throw new Error(`invalid ${key}`) }
  safeInt(body.total, "total")
  safeInt(body.elapsed_ms, "elapsed_ms")
  const cache = object(body.cache, "cache"); exactKeys(cache, ["hit"]); if (typeof cache.hit !== "boolean") throw new Error("invalid cache")
  const sources = safeArray(body.sources, "sources", 100)
  const warnings = safeArray(body.warnings, "warnings", 32); warnings.forEach((item) => boundedText(item, "warning", 1024))
  const queries = safeArray(body.search_queries, "search_queries", 32); queries.forEach((item) => boundedText(item, "search query", MAX_QUERY))
  const supportLinks = safeArray(body.support_links, "support_links", 32); supportLinks.forEach((item) => httpURL(item, "support link"))
  const usage = object(body.web_search_usage, "usage"); if (Object.keys(usage).length > 32) throw new Error("usage too large"); for (const item of Object.values(usage)) safeInt(item, "usage value")
  if (body.answer != null) boundedText(body.answer, "answer", 65536)
  if (body.model_route != null) boundedText(body.model_route, "model route", 256)
  for (const source of sources) {
    if (!source || typeof source !== "object" || Array.isArray(source)) throw new Error("invalid source")
    const item = source as Record<string, unknown>
    exactKeys(item, ["url", "title", "snippet", "content", "provider_metadata", "fetched_status", "image_url", "thumbnail_url"])
    httpURL(item.url, "source URL")
    boundedText(item.title, "title", 1024); boundedText(item.snippet, "snippet", 8192)
    if (item.content != null) boundedText(item.content, "content", 65536)
    if (item.fetched_status !== undefined) boundedString(item.fetched_status, "fetched status", 64)
    if (item.image_url != null) httpURL(item.image_url, "image URL")
    if (item.thumbnail_url != null) httpURL(item.thumbnail_url, "thumbnail URL")
    if (item.provider_metadata !== undefined) {
      safeJSON(item.provider_metadata, "provider metadata")
    }
  }
  if (body.error != null) { const error = object(body.error, "search error"); exactKeys(error, ["code", "message"]); boundedString(error.code, "error code", 128); boundedString(error.message, "error message", 1024) }
  return input as unknown as SearchArtifactResult
}
