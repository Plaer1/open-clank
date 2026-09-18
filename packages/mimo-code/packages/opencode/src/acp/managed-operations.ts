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
type Route = OpenClankManagedProtocol.OperationRouteContext
type Artifact = OpenClankManagedProtocol.ArtifactDescriptor
type Journal = OpenClankManagedProtocol.OperationJournalResult
type ExecutionOutput = {
  output: Record<string, unknown>
  artifacts: Artifact[]
  usage?: ExecuteResult["usage"]
  modelFingerprint?: string
  dimension?: number
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
  return {
    rootOperationID,
    idempotencyKey,
    operation: operation as ExecuteRequest["operation"],
    routes,
    input: record(input.input, "operation input"),
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
): Promise<ExecutionOutput> {
  const model = await runtime.modelForRoute(route)
  const inputArtifacts = new Map<string, { data: Uint8Array; mediaType: string }>()
  for (const descriptor of request.artifactInputs) {
    inputArtifacts.set(descriptor.name, {
      data: await readArtifact(connection, descriptor),
      mediaType: descriptor.mediaType,
    })
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
    return {
      output: { text: response.text },
      artifacts: [],
      usage: {
        ...(Number.isFinite(usage?.inputTokens) ? { inputTokens: Number(usage.inputTokens) } : {}),
        ...(Number.isFinite(usage?.outputTokens) ? { outputTokens: Number(usage.outputTokens) } : {}),
        ...(Number.isFinite(usage?.totalTokens) ? { totalTokens: Number(usage.totalTokens) } : {}),
      },
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
    return {
      output: {},
      artifacts,
      usage: {
        ...(Number.isFinite(usage?.inputTokens) ? { inputTokens: Number(usage.inputTokens) } : {}),
        ...(Number.isFinite(usage?.outputTokens) ? { outputTokens: Number(usage.outputTokens) } : {}),
        ...(Number.isFinite(usage?.totalTokens) ? { totalTokens: Number(usage.totalTokens) } : {}),
      },
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

async function replayResult(connection: AgentSideConnection, journal: Journal): Promise<ExecuteResult> {
  if (journal.state !== "complete") {
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
  constructor(
    private readonly connection: AgentSideConnection,
    private readonly runtime: Runtime = DEFAULT_RUNTIME,
  ) {}

  async handle(value: unknown): Promise<ExecuteResult> {
    const request = validateRequest(value)
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
    if (journal.replayed) return replayResult(this.connection, journal)

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
            : await executeRemote(this.connection, request, route, bound.scope, this.runtime)
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
        return result
      }
      if (committed) break
    }

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
    return {
      operationID: journal.operationID,
      rootOperationID: request.rootOperationID,
      operation: request.operation,
      modelRouteID: lastRoute.modelRouteID,
      connectionID: lastRoute.connectionID,
      billingLane: lastRoute.billingLane,
      state: "failed",
      committed,
      ...(committed ? { commitReason: "outcome_unknown" } : {}),
      ...(lastBinding ? { bindingID: lastBinding.bindingID } : {}),
      ...(lastBinding?.accountID ? { selectedAccountID: lastBinding.accountID } : {}),
      output: { errorCode: lastError.code },
      artifacts: [],
      replayed: false,
    }
  }
}

export * as ManagedOperations from "./managed-operations"
