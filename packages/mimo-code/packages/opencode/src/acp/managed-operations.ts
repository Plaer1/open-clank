import type { AgentSideConnection } from "@agentclientprotocol/sdk"
import type {
  EmbeddingModelV3,
  ImageModelV3,
  LanguageModelV3,
  SpeechModelV3,
  TranscriptionModelV3,
} from "@ai-sdk/provider"
import { createHash } from "node:crypto"
import {
  embedMany,
  experimental_generateSpeech,
  experimental_transcribe,
  generateImage,
  generateText,
} from "ai"
import { AppRuntime } from "@/effect/app-runtime"
import { Provider } from "@/provider"
import { ModelID, ProviderID } from "@/provider/schema"
import { ManagedProvider } from "./managed-provider"
import { OpenClankManagedProtocol } from "./openclank-protocol"

type ExecuteRequest = OpenClankManagedProtocol.OperationExecuteRequest
type ExecuteResult = OpenClankManagedProtocol.OperationExecuteResult
type OperationCancelResult = OpenClankManagedProtocol.OperationCancelResult
type Route = OpenClankManagedProtocol.OperationRouteContext
type Artifact = OpenClankManagedProtocol.ArtifactDescriptor
type Journal = OpenClankManagedProtocol.OperationJournalResult
type ExecutionOutput = {
  output: Record<string, unknown>
  artifacts: Artifact[]
  usage?: ExecuteResult["usage"]
  quota?: ExecuteResult["quota"]
  modelFingerprint?: string
  dimension?: number
}

function passiveQuota(response: unknown, providerID: string): ExecuteResult["quota"] | undefined {
  if (providerID !== "openai" && providerID !== "anthropic") return undefined
  const headers = (response as { response?: { headers?: Headers | Record<string, string> } })?.response?.headers
  if (!headers) return undefined
  const get = (name: string) => (headers instanceof Headers ? headers.get(name) : headers[name] ?? headers[name.toLowerCase()]) ?? undefined
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
      const parsed = Date.parse(raw)
      return Number.isFinite(parsed) ? new Date(parsed).toISOString() : undefined
    }
    let total = 0
    const matches = [...raw.matchAll(/(\d+)(ms|s|m|h)/g)]
    if (!matches.length || matches.map(match => match[0]).join("") !== raw) return undefined
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
    const prefix = providerID === "anthropic" ? `anthropic-ratelimit-${anthroName}` : `x-ratelimit-${kind === "inputTokens" || kind === "outputTokens" ? "limit-tokens" : kind}`
    const resetName = providerID === "anthropic" ? `${prefix}-reset` : `x-ratelimit-reset-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const limitName = providerID === "anthropic" ? `${prefix}-limit` : `x-ratelimit-limit-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const remainingName = providerID === "anthropic" ? `${prefix}-remaining` : `x-ratelimit-remaining-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
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
  const hasMetric = (value: { limit?: number; remaining?: number; resetAt?: string }) => value.limit !== undefined || value.remaining !== undefined || value.resetAt !== undefined
  const result: { transport: "documented"; adapterRevision: string; requests?: { limit?: number; remaining?: number; resetAt?: string }; tokens?: { limit?: number; remaining?: number; resetAt?: string }; inputTokens?: { limit?: number; remaining?: number; resetAt?: string }; outputTokens?: { limit?: number; remaining?: number; resetAt?: string } } = { transport: "documented", adapterRevision: `${providerID}-capacity-v1` }
  if (hasMetric(requests)) result.requests = requests
  if (hasMetric(tokens)) result.tokens = tokens
  if (hasMetric(inputTokens)) result.inputTokens = inputTokens
  if (hasMetric(outputTokens)) result.outputTokens = outputTokens
  return result.requests || result.tokens || result.inputTokens || result.outputTokens ? result : undefined
}

/**
 * Provider resolution is injectable so the managed router can be exercised
 * against the AI SDK's conformance models without booting the complete app
 * runtime. Production always uses DEFAULT_RUNTIME below.
 */
export interface Runtime {
  readonly modelForRoute: (route: Route) => Promise<Provider.Model>
  readonly getLanguage: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
  ) => Promise<LanguageModelV3>
  readonly getSearchLanguage?: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
    count: number,
  ) => Promise<LanguageModelV3>
  readonly getImage: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
  ) => Promise<ImageModelV3>
  readonly getEmbedding: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
  ) => Promise<EmbeddingModelV3>
  readonly getSpeech: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
  ) => Promise<SpeechModelV3>
  readonly getTranscription: (
    model: Provider.Model,
    scope: ManagedProvider.BoundOperation["scope"],
  ) => Promise<TranscriptionModelV3>
}

const RESULT_MEDIA_TYPE = "application/vnd.openclank.operation-result+json"
const EMBEDDING_MEDIA_TYPE = "application/vnd.openclank.embeddings+json"
const TRANSCRIPT_MEDIA_TYPE = "application/vnd.openclank.transcript+json"
const CHUNK_SIZE = 4 * 1024 * 1024
const MAX_ARTIFACT_BYTES = 256 * 1024 * 1024

const OPERATIONS = new Set<string>(OpenClankManagedProtocol.OPERATIONS)
const BILLING_LANES = new Set(["subscription", "metered_api", "local", "custom", "legacy"])
const LOCAL_EXECUTOR_FAMILIES = new Set([
  "local-executor",
  "openclank-local-executor",
  "openclank-local",
  "local",
])
const LOCAL_EXECUTORS: ReadonlyArray<{
  operations: ReadonlySet<string>
  models: ReadonlySet<string>
  executorID: string
}> = [
  {
    operations: new Set(["embeddings.create"]),
    models: new Set(["fastembed/default"]),
    executorID: "openclank.fastembed.v1",
  },
  {
    operations: new Set(["audio.synthesize"]),
    models: new Set(["kokoro/Kokoro-82M"]),
    executorID: "openclank.kokoro.v1",
  },
  {
    operations: new Set(["audio.transcribe"]),
    models: new Set(["faster-whisper/default"]),
    executorID: "openclank.whisper.v1",
  },
  {
    operations: new Set(["image.generate", "image.edit", "image.inpaint", "image.img2img"]),
    models: new Set(["diffusion/default", "diffusion-local"]),
    executorID: "openclank.diffusion.v1",
  },
  {
    operations: new Set(["image.upscale", "image.denoise"]),
    models: new Set(["realesrgan/x4plus"]),
    executorID: "openclank.realesrgan.v1",
  },
  {
    operations: new Set(["image.segment", "image.remove_background"]),
    models: new Set(["briaai/RMBG-1.4"]),
    executorID: "openclank.rmbg.v1",
  },
  {
    operations: new Set(["image.restore_face"]),
    models: new Set(["gfpgan/clean-v1"]),
    executorID: "openclank.gfpgan.v1",
  },
]

type SearchAnnotation = {
  url: string
  title?: string
  snippet?: string
  publishedAt?: string
}

/** The only request-body mutation permitted for the MiMo hosted search lane. */
export function transformMimoSearchRequestBody(
  body: Record<string, unknown>,
  input: { count: number; answer: boolean },
): Record<string, unknown> {
  return {
    ...body,
    tools: [{ type: "web_search", max_keyword: 1, force_search: true, limit: input.count }],
    ...(input.answer ? { max_completion_tokens: 512 } : { max_completion_tokens: 256 }),
  }
}

function rawResponseBody(value: unknown): unknown {
  const response = (value as { response?: { body?: unknown } })?.response
  return response?.body ?? (value as { body?: unknown })?.body
}

function searchEnvelope(value: unknown): { annotations: SearchAnnotation[]; queries: string[]; usage?: Record<string, number>; supportLinks: string[] } {
  let body = value
  if (typeof body === "string") {
    try { body = JSON.parse(body) } catch { return { annotations: [], queries: [], supportLinks: [] } }
  }
  if (!body || typeof body !== "object") return { annotations: [], queries: [], supportLinks: [] }
  const root = body as Record<string, unknown>
  const choices = Array.isArray(root.choices) ? root.choices : []
  const annotations = extractMimoSearchAnnotations(root)
  const queries = Array.isArray(root.web_search_queries) ? root.web_search_queries.filter((item): item is string => typeof item === "string") : []
  const supportLinks = Array.isArray(root.support_links)
    ? root.support_links.filter((item): item is string => typeof item === "string" && /^https?:\/\//.test(item))
    : []
  const usageValue = root.web_search_usage
  const usage = usageValue && typeof usageValue === "object" && !Array.isArray(usageValue)
    ? Object.fromEntries(Object.entries(usageValue).filter((entry): entry is [string, number] => typeof entry[1] === "number" && Number.isSafeInteger(entry[1]) && entry[1] >= 0))
    : undefined
  void choices
  return { annotations, queries, ...(usage && Object.keys(usage).length ? { usage } : {}), supportLinks }
}

export function extractMimoSearchAnnotations(value: unknown): SearchAnnotation[] {
  let body = value
  if (typeof body === "string") {
    try { body = JSON.parse(body) } catch { return [] }
  }
  if (!body || typeof body !== "object") return []
  const root = body as Record<string, unknown>
  const choices = Array.isArray(root.choices) ? root.choices : []
  const annotations = choices.flatMap((choice) => {
    if (!choice || typeof choice !== "object") return []
    const item = choice as Record<string, unknown>
    const message = item.message && typeof item.message === "object" ? item.message as Record<string, unknown> : undefined
    const delta = item.delta && typeof item.delta === "object" ? item.delta as Record<string, unknown> : undefined
    const values = message?.annotations ?? delta?.annotations ?? item.annotations
    return Array.isArray(values) ? values : []
  })
  return annotations.flatMap((item) => {
    if (!item || typeof item !== "object") return []
    const annotation = item as Record<string, unknown>
    const url = annotation.url
    if (typeof url !== "string" || !/^https?:\/\//.test(url)) return []
    return [{
      url,
      ...(typeof annotation.title === "string" ? { title: annotation.title } : {}),
      ...(typeof annotation.summary === "string" ? { snippet: annotation.summary } : {}),
      ...(typeof annotation.publish_time === "string" ? { publishedAt: annotation.publish_time } : {}),
    }]
  })
}

function webSearchOutput(response: any, provider: string, answerWanted: boolean): Record<string, unknown> {
  const envelope = searchEnvelope(rawResponseBody(response))
  const rows = envelope.annotations.map((row) => ({ ...row, provider }))
  return {
    results: rows,
    status: rows.length ? "complete" : "ungrounded",
    ...(envelope.queries.length ? { searchQueries: envelope.queries } : {}),
    ...(envelope.supportLinks.length ? { supportLinks: envelope.supportLinks } : {}),
    ...(envelope.usage ? { webSearchUsage: envelope.usage } : {}),
    ...(answerWanted && typeof response.text === "string" && response.text ? { answer: response.text } : {}),
  }
}

class ManagedOperationError extends Error {
  constructor(
    readonly code: string,
    message: string,
    readonly outcome: "auth" | "quota" | "entitlement" | "transient" | "unknown" = "unknown",
    readonly retryAfterMs?: number,
    readonly recordProviderAttempt = true,
  ) {
    super(message)
  }
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ManagedOperationError("invalid_request", `${label} must be an object`)
  }
  return value as Record<string, unknown>
}

function text(value: unknown, label: string): string {
  if (typeof value !== "string" || !value || value.includes("\0")) {
    throw new ManagedOperationError("invalid_request", `${label} is required`)
  }
  return value
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (!Number.isSafeInteger(value) || Number(value) < minimum) {
    throw new ManagedOperationError("invalid_request", `${label} is invalid`)
  }
  return Number(value)
}

function sha256(value: Uint8Array): string {
  return createHash("sha256").update(value).digest("hex")
}

function bytes(value: unknown): Uint8Array {
  if (value instanceof Uint8Array) return value
  if (value instanceof ArrayBuffer) return new Uint8Array(value)
  if (ArrayBuffer.isView(value)) return new Uint8Array(value.buffer, value.byteOffset, value.byteLength)
  throw new ManagedOperationError("invalid_provider_result", "provider returned invalid binary data")
}

function validateRequest(value: unknown): ExecuteRequest {
  const input = record(value, "managed operation request")
  const rootOperationID = text(input.rootOperationID, "rootOperationID")
  const idempotencyKey = text(input.idempotencyKey, "idempotencyKey")
  if (idempotencyKey.length < 16 || idempotencyKey.length > 128) {
    throw new ManagedOperationError("invalid_request", "idempotencyKey has an invalid length")
  }
  const operation = text(input.operation, "operation")
  if (!OPERATIONS.has(operation) || operation === "chat.stream") {
    throw new ManagedOperationError("unsupported_operation", "operation is not available through execute")
  }
  if (!Array.isArray(input.routes) || input.routes.length !== 1) {
    throw new ManagedOperationError("invalid_request", "routes must contain exactly one selected model")
  }
  const routes = input.routes.map((candidate) => {
    const route = record(candidate, "operation route")
    const billingLane = text(route.billingLane, "billingLane")
    if (!BILLING_LANES.has(billingLane)) {
      throw new ManagedOperationError("invalid_request", "billingLane is invalid")
    }
    return {
      connectionID: text(route.connectionID, "connectionID"),
      providerID: text(route.providerID, "providerID"),
      billingLane: billingLane as Route["billingLane"],
      modelRouteID: text(route.modelRouteID, "modelRouteID"),
      modelID: text(route.modelID, "modelID"),
      ...(route.grantID !== undefined ? { grantID: text(route.grantID, "grantID") } : {}),
      ...(route.preferredAccountID !== undefined
        ? { preferredAccountID: text(route.preferredAccountID, "preferredAccountID") }
        : {}),
      ...(route.inheritedAccountID !== undefined
        ? { inheritedAccountID: text(route.inheritedAccountID, "inheritedAccountID") }
        : {}),
    }
  })
  if (!Array.isArray(input.artifactInputs)) {
    throw new ManagedOperationError("invalid_request", "artifactInputs must be an array")
  }
  const artifactInputs = input.artifactInputs.map((item) => {
    const artifact = record(item, "artifact input")
    const sizeBytes = integer(artifact.sizeBytes, "artifact size")
    if (sizeBytes > MAX_ARTIFACT_BYTES) {
      throw new ManagedOperationError("invalid_request", "artifact input exceeds the size limit")
    }
    return {
      name: text(artifact.name, "artifact name"),
      artifactID: text(artifact.artifactID, "artifactID"),
      contentSHA256: text(artifact.contentSHA256, "artifact content hash"),
      sizeBytes,
      mediaType: text(artifact.mediaType, "artifact media type"),
    }
  })
  const operationInput = record(input.input, "operation input")
  if (operation === "web.search") {
    const allowed = new Set(["query", "count", "freshness", "answer"])
    if (Object.keys(operationInput).some((key) => !allowed.has(key))) {
      throw new ManagedOperationError("invalid_request", "web.search input contains unsupported fields")
    }
    const query = operationInput.query
    const count = Number(operationInput.count ?? 10)
    if (typeof query !== "string" || !query.trim() || query.length > 2000 || !Number.isSafeInteger(count) || count < 1 || count > 50) {
      throw new ManagedOperationError("invalid_request", "web.search input is invalid")
    }
    if (operationInput.freshness !== undefined && !["day", "week", "month", "year"].includes(String(operationInput.freshness))) {
      throw new ManagedOperationError("invalid_request", "web.search freshness is invalid")
    }
    if (operationInput.answer !== undefined && typeof operationInput.answer !== "boolean") {
      throw new ManagedOperationError("invalid_request", "web.search answer flag is invalid")
    }
  }
  return {
    rootOperationID,
    idempotencyKey,
    operation: operation as ExecuteRequest["operation"],
    routes,
    input: operationInput,
    artifactInputs,
    options: record(input.options, "operation options"),
  }
}

async function readArtifact(
  connection: AgentSideConnection,
  descriptor: Pick<OpenClankManagedProtocol.OperationArtifactInput, "artifactID" | "contentSHA256" | "sizeBytes" | "mediaType">,
): Promise<Uint8Array> {
  if (descriptor.sizeBytes > MAX_ARTIFACT_BYTES) {
    throw new ManagedOperationError("artifact_too_large", "artifact exceeds the managed size limit")
  }
  const chunks: Uint8Array[] = []
  let offset = 0
  while (offset < descriptor.sizeBytes || (descriptor.sizeBytes === 0 && offset === 0)) {
    const result = await OpenClankManagedProtocol.callHost(
      connection,
      "_openclank/operations/v1/artifact/read",
      { artifactID: descriptor.artifactID, offset, limit: CHUNK_SIZE },
    )
    if (
      result.artifactID !== descriptor.artifactID ||
      result.contentSHA256 !== descriptor.contentSHA256 ||
      result.sizeBytes !== descriptor.sizeBytes ||
      result.mediaType !== descriptor.mediaType ||
      result.offset !== offset
    ) {
      throw new ManagedOperationError("artifact_mismatch", "artifact read escaped its descriptor")
    }
    const data = Uint8Array.from(Buffer.from(result.dataBase64, "base64"))
    if (sha256(data) !== result.chunkSHA256) {
      throw new ManagedOperationError("artifact_mismatch", "artifact chunk failed its content hash")
    }
    chunks.push(data)
    offset += data.byteLength
    if (result.eof) break
    if (data.byteLength === 0) {
      throw new ManagedOperationError("artifact_mismatch", "artifact read made no progress")
    }
  }
  const output = Uint8Array.from(Buffer.concat(chunks.map((chunk) => Buffer.from(chunk))))
  if (output.byteLength !== descriptor.sizeBytes || sha256(output) !== descriptor.contentSHA256) {
    throw new ManagedOperationError("artifact_mismatch", "artifact failed its complete content hash")
  }
  return output
}

async function writeArtifact(
  connection: AgentSideConnection,
  data: Uint8Array,
  mediaType: string,
): Promise<Artifact> {
  if (data.byteLength > MAX_ARTIFACT_BYTES) {
    throw new ManagedOperationError("artifact_too_large", "operation output exceeds the managed size limit")
  }
  const chunks = []
  for (let offset = 0, index = 0; offset < data.byteLength; offset += CHUNK_SIZE, index += 1) {
    const chunk = data.slice(offset, Math.min(data.byteLength, offset + CHUNK_SIZE))
    chunks.push({
      index,
      dataBase64: Buffer.from(chunk).toString("base64"),
      sha256: sha256(chunk),
    })
  }
  return OpenClankManagedProtocol.callHost(
    connection,
    "_openclank/operations/v1/artifact/write",
    {
      action: "put",
      mediaType,
      contentSHA256: sha256(data),
      sizeBytes: data.byteLength,
      chunks,
    },
  )
}

async function acknowledgeArtifact(connection: AgentSideConnection, artifactID: string): Promise<void> {
  await OpenClankManagedProtocol.callHost(
    connection,
    "_openclank/operations/v1/artifact/write",
    { action: "acknowledge", artifactID },
  )
}

function statusFrom(error: unknown): number | undefined {
  let current: any = error
  for (let index = 0; current && index < 4; index += 1) {
    for (const key of ["statusCode", "status", "responseStatusCode"]) {
      const value = Number(current?.[key] ?? current?.response?.[key])
      if (Number.isInteger(value) && value >= 100 && value <= 599) return value
    }
    current = current.lastError ?? current.cause
  }
  return undefined
}

function retryAfterFrom(error: unknown): number | undefined {
  const headers = (error as any)?.responseHeaders ?? (error as any)?.response?.headers
  const milliseconds = Number(headers?.["retry-after-ms"])
  if (Number.isFinite(milliseconds) && milliseconds >= 0) {
    return Math.min(24 * 60 * 60 * 1000, Math.ceil(milliseconds))
  }
  const raw = headers?.["retry-after"]
  if (raw === undefined) return undefined
  const seconds = Number(raw)
  if (Number.isFinite(seconds) && seconds >= 0) {
    return Math.min(24 * 60 * 60 * 1000, Math.ceil(seconds * 1000))
  }
  const date = Date.parse(String(raw))
  if (!Number.isFinite(date)) return undefined
  return Math.min(24 * 60 * 60 * 1000, Math.max(0, date - Date.now()))
}

function classify(error: unknown): ManagedOperationError {
  if (error instanceof ManagedOperationError) return error
  const status = statusFrom(error)
  const code = String((error as any)?.code ?? "").toLowerCase()
  if (status === 401) return new ManagedOperationError("provider_auth", "provider authentication failed", "auth")
  if (status === 429) {
    return new ManagedOperationError("provider_quota", "provider quota is unavailable", "quota", retryAfterFrom(error))
  }
  if (status === 403 || status === 404) {
    return new ManagedOperationError("model_ineligible", "provider account cannot use this model", "entitlement")
  }
  if (status === undefined && code === "model_not_found") {
    return new ManagedOperationError(
      "model_not_found",
      "managed route model is unavailable",
      "entitlement",
      undefined,
      false,
    )
  }
  const detail = String((error as any)?.message ?? (error as any)?.cause?.message ?? "").toLowerCase()
  if (
    (status !== undefined && status >= 500) ||
    /(?:timeout|timedout|econn|network|fetch|socket|epipe)/.test(`${code} ${detail}`)
  ) {
    return new ManagedOperationError("provider_transient", "provider is temporarily unavailable", "transient")
  }
  return new ManagedOperationError("provider_unknown", "provider operation outcome is unknown", "unknown")
}

async function modelForRoute(route: Route) {
  const candidates = [...new Set([route.connectionID, route.providerID])]
  let last: unknown
  for (const providerID of candidates) {
    try {
      return await AppRuntime.runPromise(
        Provider.Service.use((service) => service.getModel(ProviderID.make(providerID), ModelID.make(route.modelID))),
      )
    } catch (error) {
      last = error
    }
  }
  void last
  throw new ManagedOperationError(
    "model_not_found",
    "managed route model is unavailable",
    "entitlement",
    undefined,
    false,
  )
}

const DEFAULT_RUNTIME: Runtime = {
  modelForRoute,
  getLanguage: (model, scope) =>
    AppRuntime.runPromise(Provider.Service.use((service) => service.getLanguage(model, scope))),
  getSearchLanguage: (model, scope, count) =>
    AppRuntime.runPromise(
      Provider.Service.use((service) =>
        service.getSearchLanguage ? service.getSearchLanguage(model, scope, count) : service.getLanguage(model, scope),
      ),
    ),
  getImage: (model, scope) =>
    AppRuntime.runPromise(Provider.Service.use((service) => service.getImage(model, scope))),
  getEmbedding: (model, scope) =>
    AppRuntime.runPromise(Provider.Service.use((service) => service.getEmbedding(model, scope))),
  getSpeech: (model, scope) =>
    AppRuntime.runPromise(Provider.Service.use((service) => service.getSpeech(model, scope))),
  getTranscription: (model, scope) =>
    AppRuntime.runPromise(Provider.Service.use((service) => service.getTranscription(model, scope))),
}

function localExecutor(request: ExecuteRequest, route: Route): string | undefined {
  if (!LOCAL_EXECUTOR_FAMILIES.has(route.providerID)) return undefined
  if (route.billingLane !== "local") {
    throw new ManagedOperationError(
      "local_executor_lane_mismatch",
      "registered local executor must use the local billing lane",
      "entitlement",
    )
  }
  const executorID = LOCAL_EXECUTORS.find(
    (entry) => entry.operations.has(request.operation) && entry.models.has(route.modelID),
  )?.executorID
  if (!executorID) {
    throw new ManagedOperationError(
      "local_executor_unregistered",
      "local operation has no registered executor/model recipe",
      "entitlement",
    )
  }
  return executorID
}

async function executeLocal(
  connection: AgentSideConnection,
  request: ExecuteRequest,
  executorID: string,
): Promise<ExecutionOutput> {
  const artifact = await OpenClankManagedProtocol.callHost(
    connection,
    "_openclank/operations/v1/executor/invoke",
    {
      executorID,
      operation: request.operation,
      artifactIDs: request.artifactInputs.map((item) => item.artifactID),
      options: { ...request.input, ...request.options },
    },
  )
  if (artifact.mediaType === EMBEDDING_MEDIA_TYPE || artifact.mediaType === TRANSCRIPT_MEDIA_TYPE) {
    const raw = await readArtifact(connection, artifact)
    const parsed = record(JSON.parse(new TextDecoder().decode(raw)), "local executor result")
    if (artifact.mediaType === EMBEDDING_MEDIA_TYPE) {
      return {
        output: { embeddings: parsed.embeddings },
        artifacts: [],
        modelFingerprint: text(parsed.modelFingerprint, "embedding model fingerprint"),
        dimension: integer(parsed.dimension, "embedding dimension", 1),
      }
    }
    return { output: { text: text(parsed.text, "transcription text") }, artifacts: [] }
  }
  return { output: {}, artifacts: [artifact] }
}

async function executeRemote(
  connection: AgentSideConnection,
  request: ExecuteRequest,
  route: Route,
  scope: ManagedProvider.BoundOperation["scope"],
  runtime: Runtime,
  callerAbortSignal?: AbortSignal,
): Promise<ExecutionOutput> {
  const model = await runtime.modelForRoute(route)
  const inputArtifacts = new Map<string, { data: Uint8Array; mediaType: string }>()
  for (const descriptor of request.artifactInputs) {
    inputArtifacts.set(descriptor.name, {
      data: await readArtifact(connection, descriptor),
      mediaType: descriptor.mediaType,
    })
  }

  if (request.operation === "web.search") {
    if (route.providerID !== "xiaomi" && route.providerID !== "mimo") {
      throw new ManagedOperationError("model_ineligible", "web.search requires the bound MiMo provider", "entitlement")
    }
    const query = String(request.input.query)
    const count = Number(request.input.count ?? 10)
    const answer = request.input.answer === true
    const timeoutMs = Number(request.options.deadlineMs ?? 30_000)
    if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 10 || timeoutMs > 120_000) {
      throw new ManagedOperationError("invalid_request", "web.search deadline is invalid")
    }
    const abortController = new AbortController()
    const abortFromCaller = () => abortController.abort(new Error("web.search cancelled by caller"))
    if (callerAbortSignal?.aborted) abortFromCaller()
    else callerAbortSignal?.addEventListener("abort", abortFromCaller, { once: true })
    const timeout = setTimeout(() => abortController.abort(new Error("web.search deadline exceeded")), timeoutMs)
    try {
      const language = await (runtime.getSearchLanguage
        ? runtime.getSearchLanguage(model, scope, count)
        : runtime.getLanguage(model, scope))
      const response = await generateText({
        model: language,
        messages: [{ role: "user", content: query }],
        maxRetries: 0,
        abortSignal: abortController.signal,
      } as any)
      return {
        output: webSearchOutput(response, route.providerID, answer),
        artifacts: [],
        usage: response.usage ? {
          ...(Number.isFinite((response.usage as any).inputTokens) ? { inputTokens: Number((response.usage as any).inputTokens) } : {}),
          ...(Number.isFinite((response.usage as any).outputTokens) ? { outputTokens: Number((response.usage as any).outputTokens) } : {}),
          ...(Number.isFinite((response.usage as any).totalTokens) ? { totalTokens: Number((response.usage as any).totalTokens) } : {}),
        } : undefined,
      }
    } catch (error) {
      if (callerAbortSignal?.aborted) {
        throw new ManagedOperationError("cancelled", "web.search cancelled by caller", "transient", undefined, false)
      }
      if (abortController.signal.aborted) {
        throw new ManagedOperationError("deadline_exceeded", "web.search deadline exceeded", "transient", undefined, false)
      }
      throw error
    } finally {
      clearTimeout(timeout)
      callerAbortSignal?.removeEventListener("abort", abortFromCaller)
    }
  }

  if (request.operation === "chat.complete" || request.operation === "vision.describe") {
    const language = await runtime.getLanguage(model, scope)
    let messages: any
    if (request.operation === "vision.describe") {
      const image = inputArtifacts.get("image") ?? [...inputArtifacts.values()][0]
      if (!image) throw new ManagedOperationError("missing_artifact", "vision operation requires an image")
      messages = [
        {
          role: "user",
          content: [
            { type: "text", text: String(request.input.prompt ?? "Describe this image in detail") },
            { type: "image", image: image.data, mediaType: image.mediaType },
          ],
        },
      ]
    } else {
      if (!Array.isArray(request.input.messages)) {
        throw new ManagedOperationError("invalid_request", "chat.complete requires messages")
      }
      messages = request.input.messages
    }
    const response = await generateText({
      model: language,
      messages,
      maxRetries: 0,
      ...(Number.isSafeInteger(request.input.maxOutputTokens)
        ? { maxOutputTokens: Number(request.input.maxOutputTokens) }
        : {}),
      ...(typeof request.input.temperature === "number" ? { temperature: request.input.temperature } : {}),
    })
    const usage: any = response.usage
    const quota = passiveQuota(response, route.providerID)
    return {
      output: { text: response.text },
      artifacts: [],
      usage: {
        ...(Number.isFinite(usage?.inputTokens) ? { inputTokens: Number(usage.inputTokens) } : {}),
        ...(Number.isFinite(usage?.outputTokens) ? { outputTokens: Number(usage.outputTokens) } : {}),
        ...(Number.isFinite(usage?.totalTokens) ? { totalTokens: Number(usage.totalTokens) } : {}),
      },
      ...(quota ? { quota } : {}),
    }
  }

  if (request.operation.startsWith("image.")) {
    const imageModel = await runtime.getImage(model, scope)
    const sourceImages = [...inputArtifacts.entries()]
      .filter(([name]) => name !== "mask")
      .map(([, item]) => item.data)
    const mask = inputArtifacts.get("mask")?.data
    const promptText = typeof request.input.prompt === "string" ? request.input.prompt : undefined
    const prompt = sourceImages.length || mask
      ? { images: sourceImages, ...(promptText ? { text: promptText } : {}), ...(mask ? { mask } : {}) }
      : (promptText ?? "Generate an image")
    const size = typeof request.input.size === "string" && /^\d+x\d+$/.test(request.input.size)
      ? (request.input.size as `${number}x${number}`)
      : undefined
    const response = await generateImage({
      model: imageModel,
      prompt,
      n: 1,
      maxRetries: 0,
      ...(size ? { size } : {}),
      ...(Number.isSafeInteger(request.input.seed) ? { seed: Number(request.input.seed) } : {}),
      ...(typeof request.input.quality === "string"
        ? { providerOptions: { [route.providerID]: { quality: request.input.quality } } }
        : {}),
    })
    const artifacts: Artifact[] = []
    for (const image of response.images) {
      artifacts.push(await writeArtifact(connection, bytes(image.uint8Array), image.mediaType || "image/png"))
    }
    const usage: any = response.usage
    const quota = passiveQuota(response, route.providerID)
    return {
      output: {},
      artifacts,
      usage: {
        ...(Number.isFinite(usage?.inputTokens) ? { inputTokens: Number(usage.inputTokens) } : {}),
        ...(Number.isFinite(usage?.outputTokens) ? { outputTokens: Number(usage.outputTokens) } : {}),
        ...(Number.isFinite(usage?.totalTokens) ? { totalTokens: Number(usage.totalTokens) } : {}),
      },
      ...(quota ? { quota } : {}),
    }
  }

  if (request.operation === "embeddings.create") {
    if (!Array.isArray(request.input.texts) || request.input.texts.some((item) => typeof item !== "string")) {
      throw new ManagedOperationError("invalid_request", "embeddings.create requires texts")
    }
    const embeddingModel = await runtime.getEmbedding(model, scope)
    const response = await embedMany({
      model: embeddingModel,
      values: request.input.texts,
      maxRetries: 0,
      maxParallelCalls: 1,
    })
    const dimension = response.embeddings[0]?.length ?? 0
    if (!dimension || response.embeddings.some((vector) => vector.length !== dimension)) {
      throw new ManagedOperationError("invalid_provider_result", "provider returned inconsistent embeddings")
    }
    return {
      output: { embeddings: response.embeddings },
      artifacts: [],
      dimension,
      modelFingerprint: `${route.connectionID}:${route.modelID}:${dimension}`,
    }
  }

  if (request.operation === "audio.synthesize") {
    const speechModel = await runtime.getSpeech(model, scope)
    const response = await experimental_generateSpeech({
      model: speechModel,
      text: text(request.input.text, "speech text"),
      maxRetries: 0,
      ...(typeof request.input.voice === "string" ? { voice: request.input.voice } : {}),
      ...(typeof request.input.speed === "number" ? { speed: request.input.speed } : {}),
      ...(typeof request.input.language === "string" ? { language: request.input.language } : {}),
      ...(typeof request.input.responseFormat === "string" ? { outputFormat: request.input.responseFormat } : {}),
    })
    return {
      output: {},
      artifacts: [await writeArtifact(connection, bytes(response.audio.uint8Array), response.audio.mediaType)],
    }
  }

  if (request.operation === "audio.transcribe") {
    const audio = inputArtifacts.get("audio") ?? [...inputArtifacts.values()][0]
    if (!audio) throw new ManagedOperationError("missing_artifact", "audio.transcribe requires audio")
    const transcriptionModel = await runtime.getTranscription(model, scope)
    const response = await experimental_transcribe({
      model: transcriptionModel,
      audio: audio.data,
      maxRetries: 0,
    })
    return { output: { text: response.text }, artifacts: [] }
  }

  throw new ManagedOperationError("unsupported_operation", "managed operation adapter is unavailable", "entitlement")
}

async function replayResult(connection: AgentSideConnection, journal: Journal, allowPending = false): Promise<ExecuteResult> {
  if (journal.state !== "complete" && journal.state !== "cancelled" && !(allowPending && (journal.state === "pending" || journal.state === "running"))) {
    throw new ManagedOperationError("operation_in_progress", "idempotent operation has not completed")
  }
  for (const artifactID of [...journal.artifactIDs].reverse()) {
    const first = await OpenClankManagedProtocol.callHost(
      connection,
      "_openclank/operations/v1/artifact/read",
      { artifactID, offset: 0, limit: CHUNK_SIZE },
    )
    if (first.mediaType !== RESULT_MEDIA_TYPE) continue
    const raw = await readArtifact(connection, first)
    const parsed = record(JSON.parse(new TextDecoder().decode(raw)), "operation replay result") as unknown as ExecuteResult
    return { ...parsed, replayed: true }
  }
  throw new ManagedOperationError("replay_unavailable", "completed operation result is unavailable")
}

export class Router {
  private readonly inFlight = new Map<string, { operation: string; operationID?: string; controller: AbortController }>()
  private readonly terminal = new Map<string, ExecuteResult>()
  private static readonly terminalLimit = 128

  private rememberTerminal(key: string, result: ExecuteResult): void {
    this.terminal.set(key, result)
    while (this.terminal.size > Router.terminalLimit) {
      const oldest = this.terminal.keys().next().value
      if (typeof oldest !== "string") break
      this.terminal.delete(oldest)
    }
  }

  constructor(
    private readonly connection: AgentSideConnection,
    private readonly runtime: Runtime = DEFAULT_RUNTIME,
  ) {}

  async cancel(value: unknown): Promise<OperationCancelResult> {
    const input = record(value, "operation cancellation")
    const rootOperationID = text(input.rootOperationID, "rootOperationID")
    const idempotencyKey = text(input.idempotencyKey, "idempotencyKey")
    const key = `${rootOperationID}\0${idempotencyKey}`
    const completed = this.terminal.get(key)
    if (completed) return { operationID: completed.operationID, rootOperationID, state: completed.state }
    const active = this.inFlight.get(key)
    if (!active) throw new ManagedOperationError("operation_not_found", "operation is not active")
    if (active.operation !== "web.search") throw new ManagedOperationError("unsupported_operation", "cancellation is only supported for web.search")
    active.controller.abort(new Error("web.search cancelled by caller"))
    // The provider task still owns the durable terminal transition. Until it
    // settles, report pending so a completion winner is never misreported as
    // cancelled by the notification response.
    return { operationID: active.operationID ?? "pending", rootOperationID, state: "pending" }
  }

  async handle(value: unknown): Promise<ExecuteResult> {
    const request = validateRequest(value)
    const operationKey = `${request.rootOperationID}\0${request.idempotencyKey}`
    if (this.inFlight.has(operationKey)) {
      throw new ManagedOperationError("operation_in_progress", "an identical managed operation is already running")
    }
    const callerController = new AbortController()
    this.inFlight.set(operationKey, { operation: request.operation, controller: callerController })
    try {
      let journal = await OpenClankManagedProtocol.callHost(
      this.connection,
      "_openclank/operations/v1/journal/cas",
      {
        action: "begin",
        rootOperationID: request.rootOperationID,
        operation: request.operation,
        idempotencyKey: request.idempotencyKey,
        request: {
          operation: request.operation,
          routes: request.routes,
          input: request.input,
          artifactInputs: request.artifactInputs,
          options: request.options,
        },
        connectionID: request.routes[0].connectionID,
        billingLane: request.routes[0].billingLane,
        modelRouteID: request.routes[0].modelRouteID,
      },
    )
    const activeEntry = this.inFlight.get(operationKey)
    if (activeEntry) activeEntry.operationID = journal.operationID
    if (journal.replayed) {
      if (journal.state === "pending" || journal.state === "running") {
        if (journal.artifactIDs.length === 0) {
          throw new ManagedOperationError("operation_in_progress", "idempotent operation is still active")
        }
        const recovered = await replayResult(this.connection, journal, true)
        journal = await OpenClankManagedProtocol.callHost(
          this.connection,
          "_openclank/operations/v1/journal/cas",
          {
            action: "cas",
            operationID: journal.operationID,
            expectedRevision: journal.revision,
            state: recovered.state,
            artifactID: journal.artifactIDs[journal.artifactIDs.length - 1],
          },
        )
        this.rememberTerminal(operationKey, recovered)
        return { ...recovered, replayed: true }
      }
      const replayed = await replayResult(this.connection, journal)
      this.rememberTerminal(operationKey, replayed)
      return replayed
    }

    let lastError = new ManagedOperationError("no_route_succeeded", "no managed route succeeded")
    let lastRoute = request.routes[0]
    let committed = false
    let lastBinding: ManagedProvider.BoundOperation["binding"] | undefined

    for (const route of request.routes) {
      lastRoute = route
      const modelIdentity = { providerID: route.connectionID, modelID: route.modelID }
      let bound: ManagedProvider.BoundOperation
      try {
        bound = await ManagedProvider.beginBoundOperation(
          { ...route, rootOperationID: request.rootOperationID },
          modelIdentity,
        )
      } catch (error) {
        lastError = classify(error)
        continue
      }
      lastBinding = bound.binding
      const attemptedAccounts = new Set<string>()
      let transientRetries = 0

      while (true) {
        const accountKey = bound.binding.accountID ?? "keyless"
        attemptedAccounts.add(accountKey)
        journal = await OpenClankManagedProtocol.callHost(
          this.connection,
          "_openclank/operations/v1/journal/cas",
          {
            action: "cas",
            operationID: journal.operationID,
            expectedRevision: journal.revision,
            state: "running",
            bindingID: bound.binding.bindingID,
            ...(bound.binding.accountID ? { selectedAccountID: bound.binding.accountID } : {}),
          },
        )
        let executed: ExecutionOutput
        try {
          const executorID = localExecutor(request, route)
          executed = executorID
            ? await executeLocal(this.connection, request, executorID)
            : await executeRemote(this.connection, request, route, bound.scope, this.runtime, callerController.signal)
        } catch (error) {
          lastError = classify(error)

          // Model lookup is local topology resolution. No provider request
          // occurred, so do not report an account attempt or mutate provider
          // health. Genuine provider 403/404 responses remain recordable
          // entitlement failures and may rotate to another account below.
          if (!lastError.recordProviderAttempt) break

          // An unknown provider outcome may already be billable or have
          // dispatched a side effect. Fence the binding before any health or
          // journal callback that could itself fail.
          if (lastError.outcome === "unknown") {
            let binding = await ManagedProvider.commitOperation(bound.binding)
            committed = true
            lastBinding = binding
            try {
              const recorded = await ManagedProvider.recordAttempt(binding, "unknown")
              if (recorded.accountID === binding.accountID && recorded.committed) {
                binding = recorded
                lastBinding = recorded
              }
            } catch {
              // Commitment is authoritative. The operation journal below also
              // records the unknown attempt if the health callback is lost.
            }
            journal = await OpenClankManagedProtocol.callHost(
              this.connection,
              "_openclank/operations/v1/journal/cas",
              {
                action: "cas",
                operationID: journal.operationID,
                expectedRevision: journal.revision,
                bindingID: binding.bindingID,
                ...(binding.accountID ? { selectedAccountID: binding.accountID } : {}),
                attempt: { accountID: binding.accountID ?? "keyless", outcome: "unknown" },
                commitReason: "outcome_unknown",
              },
            )
            break
          }

          journal = await OpenClankManagedProtocol.callHost(
            this.connection,
            "_openclank/operations/v1/journal/cas",
            {
              action: "cas",
              operationID: journal.operationID,
              expectedRevision: journal.revision,
              attempt: { accountID: bound.binding.accountID ?? "keyless", outcome: lastError.outcome },
            },
          )

          // Hosted search is a billed, provider-side side effect. Once its
          // transport was dispatched, account rotation or retry could repeat
          // that side effect, so one selected helper/account gets one call.
          if (request.operation === "web.search") break

          if (lastError.outcome === "transient" && transientRetries < 2) {
            let next: ManagedProvider.BoundOperation["binding"]
            try {
              next = await ManagedProvider.recordAttempt(bound.binding, "transient")
            } catch {
              break
            }
            if (next.accountID !== bound.binding.accountID) {
              throw new ManagedOperationError("invalid_binding", "transient retry changed provider account")
            }
            bound = { ...bound, binding: next }
            lastBinding = next
            transientRetries += 1
            await new Promise((resolve) =>
              setTimeout(resolve, 100 * 2 ** (transientRetries - 1) + Math.floor(Math.random() * 50)),
            )
            continue
          }

          if (lastError.outcome === "transient") {
            let next: ManagedProvider.BoundOperation["binding"]
            try {
              next = await ManagedProvider.recordAttempt(bound.binding, "transient")
            } catch {
              break
            }
            if (next.accountID !== bound.binding.accountID) {
              throw new ManagedOperationError("invalid_binding", "transient retry changed provider account")
            }
            bound = { ...bound, binding: next }
            lastBinding = next
          }

          if (["auth", "quota", "entitlement"].includes(lastError.outcome)) {
            let next: ManagedProvider.BoundOperation["binding"]
            try {
              next = await ManagedProvider.recordAttempt(
                bound.binding,
                lastError.outcome as "auth" | "quota" | "entitlement",
                {
                  ...(lastError.retryAfterMs !== undefined ? { retryAfterMs: lastError.retryAfterMs } : {}),
                  ...(lastError.outcome === "entitlement" ? { modelEligible: false } : {}),
                },
              )
            } catch {
              // Exhaustion is intentionally raised after the host commits the
              // final health/attempt update. The selected model has no more
              // eligible accounts; do not switch to another model.
              break
            }
            lastBinding = next
            if ((next.accountID ?? "keyless") !== accountKey && !attemptedAccounts.has(next.accountID ?? "keyless")) {
              bound = await ManagedProvider.leaseBinding(
                { ...route, rootOperationID: request.rootOperationID },
                next,
              )
              transientRetries = 0
              continue
            }
          }

          break
        }

        // A completed provider response or returned artifact is already a
        // commit event. Fence first; health and journal bookkeeping cannot be
        // allowed to reclassify this call or enter account retry logic.
        let binding = await ManagedProvider.commitOperation(bound.binding)
        committed = true
        lastBinding = binding
        try {
          const recorded = await ManagedProvider.recordAttempt(binding, "success")
          if (recorded.accountID === binding.accountID && recorded.committed) {
            binding = recorded
            lastBinding = recorded
          }
        } catch {
          // The durable commit remains valid and the journal records success.
        }
        const reason = executed.artifacts.length > 0 ? "artifact_returned" : "response_returned"
        journal = await OpenClankManagedProtocol.callHost(
          this.connection,
          "_openclank/operations/v1/journal/cas",
          {
            action: "cas",
            operationID: journal.operationID,
            expectedRevision: journal.revision,
            bindingID: binding.bindingID,
            ...(binding.accountID ? { selectedAccountID: binding.accountID } : {}),
            attempt: { accountID: binding.accountID ?? "keyless", outcome: "success" },
            commitReason: reason,
          },
        )
        for (const artifact of executed.artifacts) {
          journal = await OpenClankManagedProtocol.callHost(
            this.connection,
            "_openclank/operations/v1/journal/cas",
            {
              action: "cas",
              operationID: journal.operationID,
              expectedRevision: journal.revision,
              artifactID: artifact.artifactID,
            },
          )
        }
        const result: ExecuteResult = {
          operationID: journal.operationID,
          rootOperationID: request.rootOperationID,
          operation: request.operation,
          modelRouteID: route.modelRouteID,
          connectionID: route.connectionID,
          billingLane: route.billingLane,
          state: "complete",
          committed: true,
          commitReason: reason,
          bindingID: binding.bindingID,
          ...(binding.accountID ? { selectedAccountID: binding.accountID } : {}),
          output: executed.output,
          artifacts: executed.artifacts,
          ...(executed.usage ? { usage: executed.usage } : {}),
          ...(executed.quota ? { quota: executed.quota } : {}),
          ...(executed.modelFingerprint ? { modelFingerprint: executed.modelFingerprint } : {}),
          ...(executed.dimension !== undefined ? { dimension: executed.dimension } : {}),
          replayed: false,
        }
        const envelope = await writeArtifact(
          this.connection,
          new TextEncoder().encode(JSON.stringify(result)),
          RESULT_MEDIA_TYPE,
        )
        await acknowledgeArtifact(this.connection, envelope.artifactID)
        journal = await OpenClankManagedProtocol.callHost(
          this.connection,
          "_openclank/operations/v1/journal/cas",
          {
            action: "cas",
            operationID: journal.operationID,
            expectedRevision: journal.revision,
            state: "complete",
            artifactID: envelope.artifactID,
          },
        )
        this.rememberTerminal(operationKey, result)
        this.inFlight.delete(operationKey)
        return result
      }
      if (committed) break
    }

    const cancelled = lastError.code === "cancelled"
    const result: ExecuteResult = {
      operationID: journal.operationID,
      rootOperationID: request.rootOperationID,
      operation: request.operation,
      modelRouteID: lastRoute.modelRouteID,
      connectionID: lastRoute.connectionID,
      billingLane: lastRoute.billingLane,
      state: cancelled ? "cancelled" : "failed",
      committed,
      ...(committed ? { commitReason: "outcome_unknown" } : {}),
      ...(lastBinding ? { bindingID: lastBinding.bindingID } : {}),
      ...(lastBinding?.accountID ? { selectedAccountID: lastBinding.accountID } : {}),
      output: { errorCode: lastError.code },
      artifacts: [],
      replayed: false,
    }
    if (cancelled) {
      // Publish the bounded result before the sole terminal CAS. If publication
      // fails, the journal remains nonterminal and can be retried safely.
      const envelope = await writeArtifact(
        this.connection,
        new TextEncoder().encode(JSON.stringify(result)),
        RESULT_MEDIA_TYPE,
      )
      await acknowledgeArtifact(this.connection, envelope.artifactID)
      // Attach the acknowledged artifact while the journal is still
      // nonterminal. A retry after a terminal CAS failure can then finish the
      // one terminal transition without redispatching the provider call.
      journal = await OpenClankManagedProtocol.callHost(
        this.connection,
        "_openclank/operations/v1/journal/cas",
        {
          action: "cas",
          operationID: journal.operationID,
          expectedRevision: journal.revision,
          artifactID: envelope.artifactID,
        },
      )
      journal = await OpenClankManagedProtocol.callHost(
        this.connection,
        "_openclank/operations/v1/journal/cas",
        {
          action: "cas",
          operationID: journal.operationID,
          expectedRevision: journal.revision,
          state: "cancelled",
          artifactID: envelope.artifactID,
        },
      )
    } else {
      journal = await OpenClankManagedProtocol.callHost(
        this.connection,
        "_openclank/operations/v1/journal/cas",
        {
          action: "cas",
          operationID: journal.operationID,
          expectedRevision: journal.revision,
          state: "failed",
        },
      )
    }
    this.rememberTerminal(operationKey, result)
    return result
  } finally {
    const active = this.inFlight.get(operationKey)
    if (active?.controller === callerController) this.inFlight.delete(operationKey)
  }
}
}

export * as ManagedOperations from "./managed-operations"
