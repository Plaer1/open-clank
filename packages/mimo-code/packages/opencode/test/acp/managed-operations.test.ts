import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import {
  MockEmbeddingModelV3,
  MockImageModelV3,
  MockLanguageModelV3,
  MockSpeechModelV3,
  MockTranscriptionModelV3,
} from "ai/test"
import { ACP } from "../../src/acp/agent"
import { ManagedOperations } from "../../src/acp/managed-operations"
import { ManagedProvider } from "../../src/acp/managed-provider"
import type { OpenClankManagedProtocol } from "../../src/acp/openclank-protocol"
import { ProviderTest } from "../fake/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"

type Descriptor = OpenClankManagedProtocol.ArtifactDescriptor
type Journal = OpenClankManagedProtocol.OperationJournalResult

const media = {
  result: "application/vnd.openclank.operation-result+json",
}

function digest(data: Uint8Array): string {
  return createHash("sha256").update(data).digest("hex")
}

class Host {
  readonly calls: Array<{ method: string; params: Record<string, unknown> }> = []
  readonly artifacts = new Map<string, { descriptor: Descriptor; data: Uint8Array }>()
  rotateAccountOnAttemptTo: string | undefined
  private journal: Journal | undefined
  private bindingRevision = 0
  private artifactSequence = 0

  addArtifact(data: Uint8Array, mediaType: string): Descriptor {
    const artifactID = `artifact-${++this.artifactSequence}`
    const descriptor: Descriptor = {
      artifactID,
      contentSHA256: digest(data),
      sizeBytes: data.byteLength,
      mediaType,
      state: "staged",
    }
    this.artifacts.set(artifactID, { descriptor, data })
    return descriptor
  }

  async extMethod(method: string, params: Record<string, unknown>): Promise<unknown> {
    this.calls.push({ method, params })
    if (method.endsWith("/account/bind")) {
      this.bindingRevision = 1
      const local = params.billingLane === "local"
      return {
        bindingID: "binding-1",
        bindingRevision: this.bindingRevision,
        rootOperationID: params.rootOperationID,
        connectionID: params.connectionID,
        providerID: params.providerID,
        billingLane: params.billingLane,
        modelID: params.modelID,
        credentialRequired: !local,
        ...(!local ? { accountID: "account-1", credentialRevision: 1 } : {}),
        source: local ? "keyless" : "round_robin",
        attempt: 1,
        committed: false,
      }
    }
    if (method.endsWith("/credential/lease")) {
      return {
        leaseID: "lease-1",
        connectionID: params.connectionID,
        accountID: params.accountID,
        credentialRevision: params.expectedCredentialRevision,
        expiresAt: Date.now() + 30_000,
        credential: { type: "api", key: "leased-test-secret" },
      }
    }
    if (method.endsWith("/account/attempt")) {
      this.bindingRevision += 1
      const route = this.calls.find((call) => call.method.endsWith("/account/bind"))!.params
      const local = route.billingLane === "local"
      const accountID = this.rotateAccountOnAttemptTo ?? "account-1"
      return {
        bindingID: "binding-1",
        bindingRevision: this.bindingRevision,
        rootOperationID: route.rootOperationID,
        connectionID: route.connectionID,
        providerID: route.providerID,
        billingLane: route.billingLane,
        modelID: route.modelID,
        credentialRequired: !local,
        ...(!local ? { accountID, credentialRevision: 1 } : {}),
        source: local ? "keyless" : "round_robin",
        attempt: this.bindingRevision,
        committed: false,
      }
    }
    if (method.endsWith("/account/commit")) {
      this.bindingRevision += 1
      const route = this.calls.find((call) => call.method.endsWith("/account/bind"))!.params
      const local = route.billingLane === "local"
      return {
        bindingID: "binding-1",
        bindingRevision: this.bindingRevision,
        rootOperationID: route.rootOperationID,
        connectionID: route.connectionID,
        providerID: route.providerID,
        billingLane: route.billingLane,
        modelID: route.modelID,
        credentialRequired: !local,
        ...(!local ? { accountID: "account-1", credentialRevision: 1 } : {}),
        source: local ? "keyless" : "round_robin",
        attempt: this.bindingRevision,
        committed: true,
      }
    }
    if (method.endsWith("/journal/cas")) {
      if (params.action === "begin") {
        this.journal = {
          operationID: "operation-1",
          rootOperationID: String(params.rootOperationID),
          operation: params.operation as Journal["operation"],
          requestHash: "request-hash",
          connectionID: String(params.connectionID),
          billingLane: params.billingLane as Journal["billingLane"],
          modelRouteID: String(params.modelRouteID),
          state: "pending",
          committed: false,
          attempts: [],
          artifactIDs: [],
          revision: 1,
          replayed: false,
        }
        return this.journal
      }
      if (!this.journal || params.expectedRevision !== this.journal.revision) {
        throw new Error("stale test journal revision")
      }
      this.journal = {
        ...this.journal,
        ...(typeof params.state === "string" ? { state: params.state as Journal["state"] } : {}),
        ...(typeof params.bindingID === "string" ? { bindingID: params.bindingID } : {}),
        ...(typeof params.selectedAccountID === "string" ? { selectedAccountID: params.selectedAccountID } : {}),
        ...(typeof params.commitReason === "string"
          ? { commitReason: params.commitReason, committed: true }
          : {}),
        attempts: params.attempt ? [...this.journal.attempts, params.attempt as Record<string, unknown>] : this.journal.attempts,
        artifactIDs:
          typeof params.artifactID === "string"
            ? [...this.journal.artifactIDs, params.artifactID]
            : this.journal.artifactIDs,
        revision: this.journal.revision + 1,
      }
      return this.journal
    }
    if (method.endsWith("/artifact/write")) {
      if (params.action === "acknowledge") {
        const row = this.artifacts.get(String(params.artifactID))
        if (!row) throw new Error("unknown test artifact")
        row.descriptor = { ...row.descriptor, state: "acknowledged" }
        return row.descriptor
      }
      const chunks = (params.chunks as Array<{ index: number; dataBase64: string }>)
        .sort((a, b) => a.index - b.index)
        .map((chunk) => Buffer.from(chunk.dataBase64, "base64"))
      return this.addArtifact(Uint8Array.from(Buffer.concat(chunks)), String(params.mediaType))
    }
    if (method.endsWith("/artifact/read")) {
      const row = this.artifacts.get(String(params.artifactID))
      if (!row) throw new Error("unknown test artifact")
      const offset = Number(params.offset)
      const chunk = row.data.slice(offset, offset + Number(params.limit))
      return {
        artifactID: row.descriptor.artifactID,
        contentSHA256: row.descriptor.contentSHA256,
        sizeBytes: row.descriptor.sizeBytes,
        mediaType: row.descriptor.mediaType,
        offset,
        dataBase64: Buffer.from(chunk).toString("base64"),
        chunkSHA256: digest(chunk),
        eof: offset + chunk.byteLength >= row.data.byteLength,
      }
    }
    if (method.endsWith("/executor/invoke")) {
      return this.addArtifact(Uint8Array.from([7, 7, 7]), "image/png")
    }
    throw new Error(`unexpected host callback: ${method}`)
  }
}

function unavailable(name: string): () => Promise<never> {
  return async () => {
    throw new Error(`${name} was not expected`)
  }
}

function runtime(overrides: Partial<ManagedOperations.Runtime>): ManagedOperations.Runtime {
  const model = ProviderTest.model({
    id: ModelID.make("route-model"),
    providerID: ProviderID.make("connection-1"),
  })
  return {
    modelForRoute: async () => model,
    getLanguage: unavailable("language"),
    getImage: unavailable("image"),
    getEmbedding: unavailable("embedding"),
    getSpeech: unavailable("speech"),
    getTranscription: unavailable("transcription"),
    ...overrides,
  }
}

function request(
  operation: OpenClankManagedProtocol.Operation,
  input: Record<string, unknown>,
  artifactInputs: OpenClankManagedProtocol.OperationArtifactInput[] = [],
  modelID = "route-model",
  billingLane: OpenClankManagedProtocol.OperationRouteContext["billingLane"] = "metered_api",
  providerID = "provider-1",
): OpenClankManagedProtocol.OperationExecuteRequest {
  return {
    rootOperationID: "root-operation",
    idempotencyKey: `idempotent-${operation}-000000000000`,
    operation,
    routes: [
      {
        connectionID: "connection-1",
        providerID,
        billingLane,
        modelRouteID: "model-route-1",
        // This is not a registered host-executor model ID.
        modelID,
      },
    ],
    input,
    artifactInputs,
    options: {},
  }
}

async function execute(
  operation: OpenClankManagedProtocol.Operation,
  input: Record<string, unknown>,
  runtimeValue: ManagedOperations.Runtime,
  configure?: (host: Host) => OpenClankManagedProtocol.OperationArtifactInput[],
  modelID?: string,
  billingLane?: OpenClankManagedProtocol.OperationRouteContext["billingLane"],
  providerID?: string,
) {
  const host = new Host()
  ManagedProvider.installHostConnection(host as any)
  const artifactInputs = configure?.(host) ?? []
  const result = await new ManagedOperations.Router(host as any, runtimeValue).handle(
    request(operation, input, artifactInputs, modelID, billingLane, providerID),
  )
  return { host, result }
}

beforeEach(() => {
  process.env.OPEN_CLANK_MANAGED = "1"
  ManagedProvider.resetForTest()
})

afterEach(() => {
  ManagedProvider.resetForTest()
  delete process.env.OPEN_CLANK_MANAGED
})

describe("managed non-stream operation router", () => {
  test("rejects cross-model route fallback lists before opening a journal", async () => {
    const host = new Host()
    ManagedProvider.installHostConnection(host as any)
    const base = request("chat.complete", {
      messages: [{ role: "user", content: "hello" }],
    })
    const payload = {
      ...base,
      routes: [
        ...base.routes,
        {
          connectionID: "connection-2",
          providerID: "provider-2",
          billingLane: "metered_api" as const,
          modelRouteID: "model-route-2",
          modelID: "different-model",
        },
      ],
    }

    await expect(
      new ManagedOperations.Router(host as any, runtime({})).handle(payload),
    ).rejects.toThrow("exactly one selected model")
    expect(host.calls).toHaveLength(0)
  })

  test("ACP execute extension dispatches to the managed router", async () => {
    const params = { marker: "request" }
    const handled: unknown[] = []
    const receiver = {
      managedOperations: {
        async handle(value: unknown) {
          handled.push(value)
          return { marker: "result" }
        },
      },
    }
    const result = await ACP.Agent.prototype.extMethod.call(
      receiver as any,
      "_openclank/operations/v1/execute",
      params,
    )
    expect(result).toEqual({ marker: "result" })
    expect(handled).toEqual([params])
  })

  test("chat.complete calls the leased language model with retries disabled", async () => {
    const language = new MockLanguageModelV3({
      doGenerate: {
        content: [{ type: "text", text: "managed reply" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage: {
          inputTokens: { total: 2, noCache: 2, cacheRead: 0, cacheWrite: 0 },
          outputTokens: { total: 3, text: 3, reasoning: 0 },
        },
        warnings: [],
      },
    })
    const { result } = await execute(
      "chat.complete",
      { messages: [{ role: "user", content: "hello" }] },
      runtime({ getLanguage: async () => language }),
    )
    expect(result.output).toEqual({ text: "managed reply" })
    expect(result.committed).toBe(true)
    expect(language.doGenerateCalls).toHaveLength(1)
  })

  test("vision.describe verifies its image artifact before calling the language model", async () => {
    const language = new MockLanguageModelV3({
      doGenerate: {
        content: [{ type: "text", text: "a blue square" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage: {
          inputTokens: { total: 2, noCache: 2, cacheRead: 0, cacheWrite: 0 },
          outputTokens: { total: 3, text: 3, reasoning: 0 },
        },
        warnings: [],
      },
    })
    const { host, result } = await execute(
      "vision.describe",
      { prompt: "what is this?" },
      runtime({ getLanguage: async () => language }),
      (target) => {
        const descriptor = target.addArtifact(Uint8Array.from([137, 80, 78, 71]), "image/png")
        return [{ name: "image", ...descriptor }]
      },
    )
    expect(result.output).toEqual({ text: "a blue square" })
    expect(language.doGenerateCalls).toHaveLength(1)
    expect(host.calls.some((call) => call.method.endsWith("/artifact/read"))).toBe(true)
  })

  test("image.generate calls the image model and returns a hashed artifact", async () => {
    const image = new MockImageModelV3({
      doGenerate: async () => ({
        images: [Uint8Array.from([1, 2, 3, 4])],
        warnings: [],
        response: { timestamp: new Date(), modelId: "image-model", headers: {} },
      }),
    })
    const { host, result } = await execute(
      "image.generate",
      { prompt: "a tiny robot" },
      runtime({ getImage: async () => image }),
    )
    expect(result.artifacts).toHaveLength(1)
    expect(result.artifacts[0].contentSHA256).toBe(digest(Uint8Array.from([1, 2, 3, 4])))
    expect(host.calls.filter((call) => call.method.endsWith("/artifact/write"))).toHaveLength(3)
  })

  test("embeddings.create calls the embedding model and fingerprints its dimension", async () => {
    const embedding = new MockEmbeddingModelV3({
      maxEmbeddingsPerCall: 10,
      doEmbed: {
        embeddings: [[0.1, 0.2], [0.3, 0.4]],
        usage: { tokens: 4 },
        warnings: [],
      },
    })
    const { result } = await execute(
      "embeddings.create",
      { texts: ["one", "two"] },
      runtime({ getEmbedding: async () => embedding }),
    )
    expect(result.output).toEqual({ embeddings: [[0.1, 0.2], [0.3, 0.4]] })
    expect(result.dimension).toBe(2)
    expect(result.modelFingerprint).toBe("connection-1:route-model:2")
    expect(embedding.doEmbedCalls).toHaveLength(1)
  })

  test("audio synthesis and transcription use their modality models and artifact transport", async () => {
    const speech = new MockSpeechModelV3({
      doGenerate: async () => ({
        audio: Uint8Array.from([9, 8, 7]),
        warnings: [],
        response: { timestamp: new Date(), modelId: "speech-model", headers: {} },
      }),
    })
    const synthesized = await execute(
      "audio.synthesize",
      { text: "hello" },
      runtime({ getSpeech: async () => speech }),
    )
    expect(synthesized.result.artifacts).toHaveLength(1)

    ManagedProvider.resetForTest()
    const transcription = new MockTranscriptionModelV3({
      doGenerate: async () => ({
        text: "hello from audio",
        segments: [],
        language: "en",
        durationInSeconds: 1,
        warnings: [],
        response: { timestamp: new Date(), modelId: "transcription-model", headers: {} },
      }),
    })
    const transcribed = await execute(
      "audio.transcribe",
      {},
      runtime({ getTranscription: async () => transcription }),
      (host) => {
        const descriptor = host.addArtifact(Uint8Array.from([3, 2, 1]), "audio/wav")
        return [{ name: "audio", ...descriptor }]
      },
    )
    expect(transcribed.result.output).toEqual({ text: "hello from audio" })
  })

  test("registered local model operations use only the fixed host executor", async () => {
    const { host, result } = await execute(
      "image.upscale",
      {},
      runtime({}),
      (target) => {
        const descriptor = target.addArtifact(Uint8Array.from([1, 1, 1]), "image/png")
        return [{ name: "image", ...descriptor }]
      },
      "realesrgan/x4plus",
      "local",
      "local-executor",
    )
    expect(result.artifacts).toHaveLength(1)
    const invoke = host.calls.find((call) => call.method.endsWith("/executor/invoke"))
    expect(invoke?.params).toMatchObject({
      executorID: "openclank.realesrgan.v1",
      operation: "image.upscale",
    })
  })

  test("local diffusion operations use the fixed diffusion broker recipe", async () => {
    const { host, result } = await execute(
      "image.inpaint",
      { prompt: "repair" },
      runtime({}),
      (target) => {
        const source = target.addArtifact(Uint8Array.from([1, 2, 3]), "image/png")
        const mask = target.addArtifact(Uint8Array.from([4, 5, 6]), "image/png")
        return [
          { name: "image", ...source },
          { name: "mask", ...mask },
        ]
      },
      "diffusion/default",
      "local",
      "local-executor",
    )
    expect(result.state).toBe("complete")
    expect(host.calls.find((call) => call.method.endsWith("/executor/invoke"))?.params).toMatchObject({
      executorID: "openclank.diffusion.v1",
      operation: "image.inpaint",
    })
  })

  test("an unregistered local-executor model fails closed before provider resolution", async () => {
    let providerLookups = 0
    const { host, result } = await execute(
      "image.generate",
      { prompt: "must not dispatch" },
      runtime({
        modelForRoute: async () => {
          providerLookups += 1
          return ProviderTest.model()
        },
      }),
      undefined,
      "unregistered-local-model",
      "local",
      "local-executor",
    )
    expect(providerLookups).toBe(0)
    expect(result).toMatchObject({
      state: "failed",
      committed: false,
      output: { errorCode: "local_executor_unregistered" },
    })
    expect(host.calls.some((call) => call.method.endsWith("/executor/invoke"))).toBe(false)
  })

  test("a local-server family such as Ollama still uses its projected SDK adapter", async () => {
    const language = new MockLanguageModelV3({
      doGenerate: {
        content: [{ type: "text", text: "local server reply" }],
        finishReason: { unified: "stop", raw: "stop" },
        usage: {
          inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
          outputTokens: { total: 2, text: 2, reasoning: 0 },
        },
        warnings: [],
      },
    })
    const { host, result } = await execute(
      "chat.complete",
      { messages: [{ role: "user", content: "hello" }] },
      runtime({ getLanguage: async () => language }),
      undefined,
      "qwen3:latest",
      "local",
      "ollama",
    )
    expect(result.output).toEqual({ text: "local server reply" })
    expect(host.calls.some((call) => call.method.endsWith("/executor/invoke"))).toBe(false)
  })

  test("transient failures retry the same account twice, then fail uncommitted", async () => {
    let attempts = 0
    const { host, result } = await execute(
      "embeddings.create",
      { texts: ["one"] },
      runtime({
        getEmbedding: async () => {
          attempts += 1
          throw Object.assign(new Error("network error"), { code: "ECONNRESET" })
        },
      }),
    )
    expect(attempts).toBe(3)
    expect(result).toMatchObject({
      state: "failed",
      committed: false,
      output: { errorCode: "provider_transient" },
    })
    expect(host.calls.filter((call) => call.method.endsWith("/account/attempt"))).toHaveLength(3)
  })

  test("a local model lookup miss fails without recording a provider attempt", async () => {
    const { host, result } = await execute(
      "chat.complete",
      { messages: [{ role: "user", content: "hello" }] },
      runtime({
        modelForRoute: async () => {
          throw Object.assign(new Error("projected model is unavailable"), { code: "model_not_found" })
        },
      }),
    )
    expect(result).toMatchObject({
      state: "failed",
      committed: false,
      output: { errorCode: "model_not_found" },
    })
    expect(host.calls.filter((call) => call.method.endsWith("/account/attempt"))).toHaveLength(0)
  })

  test("a provider 404 still rotates accounts for the selected model", async () => {
    let attempts = 0
    const language = new MockLanguageModelV3({
      doGenerate: async () => {
        attempts += 1
        throw Object.assign(new Error("provider rejected this account for the model"), { status: 404 })
      },
    })
    const { host, result } = await execute(
      "chat.complete",
      { messages: [{ role: "user", content: "hello" }] },
      runtime({ getLanguage: async () => language }),
      (target) => {
        target.rotateAccountOnAttemptTo = "account-2"
        return []
      },
    )
    expect(attempts).toBe(2)
    expect(result).toMatchObject({
      state: "failed",
      committed: false,
      output: { errorCode: "model_ineligible" },
    })
    expect(
      host.calls
        .filter((call) => call.method.endsWith("/account/attempt"))
        .map((call) => call.params.accountID),
    ).toEqual(["account-1", "account-2"])
  })

  test("an ambiguous provider outcome commits the binding and is never replayed", async () => {
    const { host, result } = await execute(
      "embeddings.create",
      { texts: ["one"] },
      runtime({
        getEmbedding: async () => {
          throw new Error("ambiguous provider outcome")
        },
      }),
    )
    expect(result).toMatchObject({
      state: "failed",
      committed: true,
      commitReason: "outcome_unknown",
      output: { errorCode: "provider_unknown" },
    })
    expect(host.calls.filter((call) => call.method.endsWith("/account/commit"))).toHaveLength(1)
  })
})
