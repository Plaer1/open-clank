import { expect, test } from "bun:test"
import { tool } from "ai"
import z from "zod"
import { appendFinalCodeContext } from "../../src/session/llm"
import { LLM } from "../../src/session/llm"
import { Effect, Layer, Stream } from "effect"
import { MockLanguageModelV3 } from "ai/test"
import { ProviderTest } from "../fake/provider"
import { ProviderID, ModelID } from "../../src/provider/schema"
import { Auth } from "../../src/auth"
import { Config } from "../../src/config"
import { Plugin } from "../../src/plugin"
import { ActorRegistry } from "../../src/actor/registry"
import { Memory } from "../../src/memory"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { installManagedSessionBinding } from "../../src/memory/session-scope"
import { testEffect } from "../lib/effect"

function input() {
  return { sessionID: "engine-a", messages: [{ role: "user" as const, content: "Explain function target" }],
    system: ["final persona", "final Hex"], options: { instructions: "final instructions" },
    tools: { search_code: tool({ description: "read", inputSchema: z.object({ query: z.string() }) }) },
    activeTools: ["search_code"], usable: 32000, abort: new AbortController().signal, agentID: "main" }
}
const card = "<untrusted_context>\n[code-evidence-v1 workspace file hash 0:4]\nsource\n</untrusted_context>"

test("final actual frame resolves schemas and inserts only a request-local user card", async () => {
  const frame = input()
  const calls: any[] = []
  const result = await appendFinalCodeContext(frame, async (request) => { calls.push(request); return card })
  expect(calls).toHaveLength(1)
  expect(calls[0].retainedMessages).toEqual(frame.messages)
  expect(calls[0].requestFingerprint).toMatch(/^[a-f0-9]{64}$/)
  expect(calls[0]).not.toHaveProperty("owner")
  expect(calls[0]).not.toHaveProperty("root")
  expect(result[0]).toEqual({ role: "user", content: card })
  expect(result[1]).toEqual(frame.messages[0])
  expect(frame.messages).toHaveLength(1)
  const changed = input(); changed.system.push("another final instruction")
  await appendFinalCodeContext(changed, async (request) => { expect(request.requestFingerprint).not.toBe(calls[0].requestFingerprint); return null })
})

test("tools and final system consume actual capacity before any retrieval", async () => {
  const frame = input(); frame.usable = 1200
  let calls = 0
  expect(await appendFinalCodeContext(frame, async () => { calls++; return card })).toBe(frame.messages)
  expect(calls).toBe(0)
})

test("unknown media, ephemeral actors, abort and frame drift insert zero", async () => {
  for (const alter of [
    (f: any) => { f.messages = [{ role: "user", content: [{ type: "image", image: "https://not-fetched.invalid" }] }] },
    (f: any) => { f.ephemeral = true }, (f: any) => { f.agentID = "worker" },
    (f: any) => { f.abort = AbortSignal.abort() },
  ]) {
    const frame = input(); alter(frame); let calls = 0
    expect(await appendFinalCodeContext(frame, async () => { calls++; return card })).toBe(frame.messages)
    expect(calls).toBe(0)
  }
  const frame = input()
  expect(await appendFinalCodeContext(frame, async () => { frame.messages[0].content = "changed"; return card })).toBe(frame.messages)
  const aborted = input(); const ctrl = new AbortController(); aborted.abort = ctrl.signal
  expect(await appendFinalCodeContext(aborted, async () => { ctrl.abort(); return card })).toBe(aborted.messages)
})

test("reset and compaction pass only the actually retained messages to dedup owner", async () => {
  const frame = input(); const seen: unknown[] = []
  const read = async (request: any) => { seen.push(request.retainedMessages); return request.retainedMessages.some((m: any) => m.content === card) ? null : card }
  const first = await appendFinalCodeContext(frame, read)
  const repeated = await appendFinalCodeContext({ ...frame, messages: first }, read)
  expect(repeated).toBe(first)
  const reset = await appendFinalCodeContext(input(), read)
  expect(reset).toHaveLength(2)
  expect(seen).toHaveLength(3)
  expect(seen[2]).toEqual(input().messages)
})

const language = new MockLanguageModelV3({ doStream: async () => ({
  stream: new ReadableStream({ start(controller) {
    controller.enqueue({ type: "stream-start", warnings: [] })
    controller.enqueue({ type: "finish", finishReason: { unified: "stop", raw: "stop" }, usage: { inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 }, outputTokens: { total: 0, text: 0, reasoning: 0 } } })
    controller.close()
  } }),
}) })
const model = ProviderTest.model({ providerID: ProviderID.make("conn-code"), id: ModelID.make("text-only"), limit: { context: 32000, output: 1024 } })
const provider = ProviderTest.fake({ model, info: ProviderTest.info({ id: model.providerID, options: {
  _openclankFamilyID: "deepseek", _openclankAdapterID: "openai-chat", _openclankBillingLane: "metered_api", _openclankConnectionID: "conn-code",
} }, model), getLanguage: () => Effect.succeed(language) })
const hookLayer = LLM.layer.pipe(Layer.provide(Layer.mergeAll(
  provider.layer,
  Layer.succeed(Auth.Service, {} as any),
  Layer.succeed(Config.Service, { get: () => Effect.succeed({ compaction: { reserved: 0 } }) } as any),
  Layer.succeed(Plugin.Service, { trigger: (name: string, _: unknown, output: any) => Effect.sync(() => name === "chat.params" ? { ...output, options: { finalInstructions: "plugin-replaced-options" } } : output) } as any),
  Layer.succeed(ActorRegistry.Service, { servesCheckpoint: () => Effect.succeed(false) } as any),
  Layer.succeed(Memory.Service, {} as any),
)))

testEffect(hookLayer).live("actual LLM.stream captures final transformed provider frame; large description skips callback", Effect.gen(function* () {
  const llm = yield* LLM.Service
  yield* Effect.promise(async () => {
    process.env.OPEN_CLANK_MANAGED = "1"
    ManagedProvider.resetForTest()
    const calls: any[] = []
    const binding = { engineSessionID: "engine-final", stableChatID: "chat-final", owner: "alice", canonicalCwd: "/work", workspaceRevision: 1,
      authorityWorkspaceID: "workspace-final", memoryWorkspaceID: "global", copalWorkspace: "default", memoryEnabled: true, engineAliases: [], mapRevision: 1, mappingRevision: 1 }
    ManagedProvider.installHostConnection({ async extMethod(method: string, params: any) {
      calls.push({ method, params })
      if (method.endsWith("/binding/read")) return binding
      if (method.endsWith("/code-context/read")) return { bindingRevision: 1, requestID: params.requestID, requestFingerprint: params.requestFingerprint, card, reason: null }
      if (method.endsWith("/account/bind")) return { bindingID: "bind", bindingRevision: 1, rootOperationID: "root-final", connectionID: "conn-code", providerID: "deepseek", billingLane: "metered_api", modelID: "text-only", credentialRequired: true, accountID: "account-synthetic", credentialRevision: 1, source: "round_robin", attempt: 1, committed: false }
      if (method.endsWith("/credential/lease")) return { leaseID: "lease", connectionID: "conn-code", accountID: "account-synthetic", credentialRevision: 1, expiresAt: Date.now() + 30000, credential: { type: "api", key: "synthetic-not-sent" } }
      throw new Error("unexpected synthetic callback")
    } } as any)
    installManagedSessionBinding("engine-final", { owner: "alice", stableChatID: "chat-final", engineSessionID: "engine-final", engineAliases: [], memoryWorkspaceID: "global", authorityWorkspaceID: "workspace-final", copalWorkspace: "default", physicalCwd: "/work", bindingRevision: 1, mapRevision: 1, mappingRevision: 1, memoryEnabled: true, transition: null })
    const run = async (description: string) => ManagedProvider.withHostContext("engine-final", { chatID: "chat-final", workspaceID: "workspace-final" }, () => ManagedProvider.withOperation("engine-final",
      { rootOperationID: "root-final", connectionID: "conn-code", providerID: "deepseek", billingLane: "metered_api", modelID: "text-only" },
      { providerID: "conn-code", modelID: "text-only" }, async () => {
        await Effect.runPromise(llm.stream({ sessionID: "engine-final", model, agentID: "main", agent: { name: "chat", mode: "primary", options: {}, permission: [{ permission: "*", pattern: "*", action: "allow" }] },
          user: { id: "user-final", sessionID: "engine-final", role: "user", time: { created: 1 }, agent: "chat", model: { providerID: model.providerID, modelID: model.id } },
          system: ["assembled final persona and Hex"], prebuiltSystem: ["assembled final persona and Hex"], messages: [{ role: "user", content: "Explain function target" }],
          tools: { search_code: tool({ description, inputSchema: z.object({ query: z.string() }) }) },
        } as any).pipe(Stream.runDrain))
      }))
    try {
      await run("actual full provider-visible description")
      expect(language.doStreamCalls).toHaveLength(1)
      const actual = language.doStreamCalls[0]
      expect(JSON.stringify(actual.prompt)).toContain(card.replaceAll("\n", "\\n"))
      expect(JSON.stringify(actual.providerOptions)).toContain("plugin-replaced-options")
      const request = calls.find((c) => c.method.endsWith("/code-context/read")).params
      const retained = request.retainedMessages
      expect(JSON.stringify(retained)).toContain("assembled final persona and Hex")
      expect(JSON.stringify(retained)).not.toContain("code-evidence-v1")
      expect(actual.tools?.[0].type).toBe("function")
      if (actual.tools?.[0].type === "function") expect(actual.tools[0].description).toBe("actual full provider-visible description")
      await run("x".repeat(40000))
      expect(language.doStreamCalls).toHaveLength(2)
      expect(calls.filter((c) => c.method.endsWith("/code-context/read"))).toHaveLength(1)
      expect(JSON.stringify(language.doStreamCalls[1].prompt)).not.toContain("code-evidence-v1")
    } finally {
      ManagedProvider.resetForTest()
      delete process.env.OPEN_CLANK_MANAGED
    }
  })
}))

test("managed callback abort admission, nonce and normal listener cleanup", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  ManagedProvider.resetForTest()
  const binding = { engineSessionID: "engine-abort", stableChatID: "chat-abort", owner: "alice", canonicalCwd: "/work", workspaceRevision: 1, authorityWorkspaceID: "workspace-abort", memoryWorkspaceID: "global", copalWorkspace: "default", memoryEnabled: true, engineAliases: [], mapRevision: 1, mappingRevision: 1 }
  const request = { sessionID: "engine-abort", requestFingerprint: "a".repeat(64), query: "function target", retainedMessages: [], availableByteTokens: 2048 }
  let release!: (value: any) => void
  let bindingStarted!: () => void
  const entered = new Promise<void>((r) => { bindingStarted = r })
  const calls: any[] = []
  let holdBinding = true, holdRead = false, wrongNonce = false
  let pendingRead: ((v: any) => void) | undefined
  ManagedProvider.installHostConnection({ async extMethod(method: string, params: any) {
    calls.push({ method, params })
    if (method.endsWith("/binding/read")) {
      if (holdBinding) { bindingStarted(); return await new Promise((r) => { release = r }) }
      return binding
    }
    if (params.action === "cancel") { pendingRead?.({ ...params, card: null, reason: "cancelled" }); return { ...params, card: null, reason: "cancelled" } }
    if (holdRead) return await new Promise((r) => { pendingRead = r })
    return { bindingRevision: 1, requestID: wrongNonce ? "0".repeat(32) : params.requestID, requestFingerprint: params.requestFingerprint, card, reason: null }
  } } as any)
  try {
    await ManagedProvider.withHostContext("engine-abort", { chatID: "chat-abort", workspaceID: "workspace-abort" }, async () => {
      const ctrl = new AbortController()
      const first = ManagedProvider.readCodeContext(request, ctrl.signal)
      await entered; ctrl.abort(); holdBinding = false; release(binding)
      expect(await first).toBeNull()
      expect(calls.filter((c) => c.method.endsWith("/code-context/read"))).toHaveLength(0)
      const normal = new AbortController(); let added = 0, removed = 0
      const add = normal.signal.addEventListener.bind(normal.signal), remove = normal.signal.removeEventListener.bind(normal.signal)
      normal.signal.addEventListener = ((...args: any[]) => { added++; return (add as any)(...args) }) as any
      normal.signal.removeEventListener = ((...args: any[]) => { removed++; return (remove as any)(...args) }) as any
      expect(await ManagedProvider.readCodeContext(request, normal.signal)).toBe(card)
      expect(added).toBe(1); expect(removed).toBe(1)
      expect(await ManagedProvider.readCodeContext(request, normal.signal)).toBe(card)
      const ids = calls.filter((c) => c.params.action === "read").map((c) => c.params.requestID)
      expect(new Set(ids).size).toBe(2)
      wrongNonce = true
      expect(await ManagedProvider.readCodeContext(request, normal.signal)).toBeNull()
      wrongNonce = false; holdRead = true
      const pendingCtrl = new AbortController()
      const active = ManagedProvider.readCodeContext(request, pendingCtrl.signal)
      while (!pendingRead) await Promise.resolve()
      pendingCtrl.abort()
      expect(await active).toBeNull()
      const cancel = calls.find((c) => c.params.action === "cancel")
      expect(cancel.params.requestID).toBe(calls.filter((c) => c.params.action === "read").at(-1).params.requestID)
    })
    const before = calls.length
    await ManagedProvider.withHostContext("engine-abort", { chatID: "another-chat", workspaceID: "workspace-abort" }, async () => {
      expect(await ManagedProvider.readCodeContext(request, new AbortController().signal)).toBeNull()
    })
    expect(calls.length).toBe(before)
  } finally { ManagedProvider.resetForTest(); delete process.env.OPEN_CLANK_MANAGED }
})
