import { describe, expect, test } from "bun:test"
import { ACP, parseCommandText, sourceMessageAnchors } from "../../src/acp/agent"
import { createHash } from "node:crypto"
import { SessionID, MessageID } from "../../src/session/schema"
import { installManagedSessionBinding, unregisterMemorySessionScope } from "../../src/memory/session-scope"
import type { Agent as ACPAgent } from "@agentclientprotocol/sdk"

/**
 * Type-level test: This line will fail to compile if ACP.Agent
 * doesn't properly implement the ACPAgent interface.
 *
 * The SDK checks for methods like `agent.unstable_setSessionModel` at runtime
 * and throws "Method not found" if they're missing. TypeScript allows optional
 * interface methods to be omitted, but the SDK still expects them.
 *
 * @see https://github.com/agentclientprotocol/typescript-sdk/commit/7072d3f
 */
type _AssertAgentImplementsACPAgent = ACP.Agent extends ACPAgent ? true : never
const _typeCheck: _AssertAgentImplementsACPAgent = true

/**
 * Runtime verification that optional methods the SDK expects are actually implemented.
 * The SDK's router checks `if (!agent.methodName)` and throws MethodNotFound if missing.
 */
describe("acp.agent interface compliance", () => {
  // Extract method names from the ACPAgent interface type
  type ACPAgentMethods = keyof ACPAgent

  // Methods that the SDK's router explicitly checks for at runtime
  const sdkCheckedMethods: ACPAgentMethods[] = [
    // Required
    "initialize",
    "newSession",
    "prompt",
    "cancel",
    // Optional but checked by SDK router
    "loadSession",
    "setSessionMode",
    "authenticate",
    // Unstable - SDK checks these with unstable_ prefix
    "listSessions",
    "unstable_forkSession",
    "unstable_resumeSession",
    "unstable_setSessionModel",
  ]

  test("Agent implements all SDK-checked methods", () => {
    for (const method of sdkCheckedMethods) {
      expect(typeof ACP.Agent.prototype[method as keyof typeof ACP.Agent.prototype], `Missing method: ${method}`).toBe(
        "function",
      )
    }
  })
})


describe("ACP native command argument transport", () => {
  test("preserves multiline, repeated whitespace and trailing argument bytes", () => {
    expect(parseCommandText("/review  first\n  second\tthird ")).toEqual({ name: "review", args: " first\n  second\tthird " })
    expect(parseCommandText("/review\nline one\nline two")).toEqual({ name: "review", args: "line one\nline two" })
    expect(parseCommandText("/goal")).toEqual({ name: "goal", args: "" })
    expect(parseCommandText("ordinary /review text")).toBeUndefined()
  })
})


describe("managed effective settings target and merge", () => {
  test("uses target directory and actual config provider options after public redaction", async () => {
    const previous = process.env.OPEN_CLANK_MANAGED
    process.env.OPEN_CLANK_MANAGED = "1"
    const calls: string[] = []
    let configuredHelpers: { small_model?: string; vision_model?: string } = { small_model: "selected/helper", vision_model: "selected/vision" }
    const agent = {
      runtimeStateFor: async () => ({ neverAsk: false, defaultAgent: "build" }),
      sessionManager: { get: () => ({ cwd: "/workspace/selected" }) },
      sdk: { app: { agents: async () => ({ data: [{ name: "build", mode: "primary" }] }) }, config: {
        providers: async ({ directory }: { directory: string }) => { calls.push(directory); return { data: { providers: [{ id: "selected", options: {}, models: { model: { id: "model", providerID: "selected", limit: { context: 100_000, output: 10_000 }, options: {} } } }] } } },
        get: async ({ directory }: { directory: string }) => { calls.push(directory); return { data: { ...configuredHelpers, compaction: { tail_turns: 4 }, checkpoint: { reserved: 13_000 }, provider: { selected: { options: { _openclankAgentSettings: { compaction: { max_context: { "selected/*": "50%" }, preserve_recent_tokens: 0 }, checkpoint: { thresholds: ["25%", "90%"] } } } } } } } },
      } },
    } as unknown as ACP.Agent
    try {
      const result = await ACP.Agent.prototype.extMethod.call(agent, "_openclank/session/v1/settings/effective", { providerID: "selected", modelID: "model", directory: "/workspace/selected", sessionID: "owned-session" })
      expect(calls).toEqual(["/workspace/selected", "/workspace/selected"])
      expect(result).toMatchObject({ profile: { small_model: "selected/helper", vision_model: "selected/vision", model_policy: { helper: "explicit_config", vision: "explicit_config" } } })
      expect(result).toMatchObject({ target: { directory: "/workspace/selected", sessionID: "owned-session" }, timing: "next_admission", context: { effective: 50_000, source: "config" }, compaction: { tailTurns: 4, preserveRecentTokens: 0, prune: true }, checkpoint: { thresholds: [12_500, 37_000] } })
      await expect(ACP.Agent.prototype.extMethod.call(agent, "_openclank/session/v1/settings/effective", { providerID: "selected", modelID: "model", directory: "/workspace/other", sessionID: "owned-session" })).rejects.toMatchObject({ code: -32602, data: "session directory does not match settings target" })
      expect(calls).toHaveLength(2)
      await expect(ACP.Agent.prototype.extMethod.call(agent, "_openclank/session/v1/settings/effective", { providerID: "selected", modelID: "model" })).rejects.toMatchObject({ code: -32602 })
      configuredHelpers = {}
      const inherited = await ACP.Agent.prototype.extMethod.call(agent, "_openclank/session/v1/settings/effective", { providerID: "selected", modelID: "model", directory: "/workspace/selected", sessionID: "owned-session" })
      expect(inherited).toMatchObject({ profile: { small_model: null, vision_model: null, model_policy: { helper: "inherit_caller", vision: "inherit_caller_capability_required" } } })
    } finally {
      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
      else process.env.OPEN_CLANK_MANAGED = previous
    }
  })
})


describe("durable native source boundary anchors", () => {
  test("forked user parts retain host source IDs while native IDs change", () => {
    const original = [{ info: { id: "native-old", role: "user" }, parts: [{ type: "text", metadata: { hostSourceMessageID: "root-host" } }] }]
    expect(sourceMessageAnchors([{ info: { id: "native-legacy", role: "user" }, parts: [{ type: "text", text: "[turn_source_id=root-legacy]\nlegacy body" }] }])).toEqual([{ nativeMessageID: "native-legacy", hostSourceMessageID: "root-legacy" }])
    expect(sourceMessageAnchors(original)).toEqual([{ nativeMessageID: "native-old", hostSourceMessageID: "root-host" }])
    const forked = structuredClone(original)
    forked[0].info.id = "native-forked"
    expect(sourceMessageAnchors(forked)).toEqual([{ nativeMessageID: "native-forked", hostSourceMessageID: "root-host" }])
    expect(() => sourceMessageAnchors([...original, ...forked])).toThrow("Multiple native messages")
    expect(sourceMessageAnchors([{ info: { id: "assistant", role: "assistant" }, parts: original[0].parts }])).toEqual([])
  })
})


test("reserved managed forks reject cross-owner, unreserved and changed-receipt requests before native writes", async () => {
  const previous = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  const sourceSessionID = SessionID.descending()
  const targetSessionID = SessionID.descending()
  const bind = (id: string, owner: string, chat: string) => installManagedSessionBinding(id, { owner, stableChatID: chat, engineSessionID: id, engineAliases: [], memoryWorkspaceID: "workspace", authorityWorkspaceID: "workspace", copalWorkspace: "/workspace/fork", physicalCwd: "/workspace/fork", bindingRevision: 1, mapRevision: 1, mappingRevision: 1, memoryEnabled: false, transition: null })
  const receiver = { sessionManager: { get: () => ({ cwd: "/workspace/fork" }) }, reservedForkTargets: new Set<string>(), forkSources: new Set<string>(), forkReceipts: new Map<string, { fingerprint: string; owner: string; sourceChatID: string; targetChatID: string; result: Promise<Record<string, unknown>> }>() }
  const request = { sourceSessionID, targetSessionID, directory: "/workspace/fork", hostSourceMap: { "root-source": "root-target" } }
  const invoke = (params: Record<string, unknown>) => ACP.Agent.prototype.extMethod.call(receiver as unknown as ACP.Agent, "_openclank/session/v1/fork/create", params)
  try {
    bind(sourceSessionID, "owner-a", "source-chat")
    bind(targetSessionID, "owner-b", "target-chat")
    await expect(invoke(request)).rejects.toMatchObject({ data: { code: "FORK_AUTHORITY_MISMATCH" } })
    unregisterMemorySessionScope(targetSessionID)
    bind(targetSessionID, "owner-a", "target-chat")
    await expect(invoke(request)).rejects.toMatchObject({ data: { code: "FORK_TARGET_UNRESERVED" } })
    const receipt = { created: true, targetSessionID }
    receiver.forkReceipts.set(targetSessionID, { fingerprint: createHash("sha256").update(JSON.stringify([request.sourceSessionID, request.targetSessionID, request.directory, Object.entries(request.hostSourceMap), null, null])).digest("hex"), owner: "owner-a", sourceChatID: "source-chat", targetChatID: "target-chat", result: Promise.resolve(receipt) })
    expect(await invoke(request)).toEqual(receipt)
    await expect(invoke({ ...request, hostSourceMap: { "root-source": "changed-target" } })).rejects.toMatchObject({ data: { code: "FORK_REQUEST_CONFLICT" } })
  } finally {
    unregisterMemorySessionScope(sourceSessionID)
    unregisterMemorySessionScope(targetSessionID)
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
  }
})


test("managed Agent profiles reach the native selector while Plan, Chat and eligibility fences remain", async () => {
  const previous = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  type Selector = { agentForPrompt(directory: string, currentMode: string | undefined, context?: { mode?: string; lane?: string; agent_profile?: string }): Promise<string>; loadAvailableModes(directory: string): Promise<Array<{ id: string; name: string }>> }
  const methods = ACP.Agent.prototype as unknown as Selector
  const receiver = { runtimeStateFor: async () => ({ defaultAgent: "custom" }), loadAvailableModes: methods.loadAvailableModes, config: { sdk: { app: { agents: async () => ({ data: [{ name: "build", mode: "primary" }, { name: "plan", mode: "primary" }, { name: "custom", mode: "all" }, { name: "hidden", mode: "primary", hidden: true }, { name: "helper", mode: "subagent" }, { name: "dream", mode: "primary" }] }) } } } }
  const select = (context?: { mode?: string; lane?: string; agent_profile?: string }) => methods.agentForPrompt.call(receiver as unknown as Selector, "/workspace/selected", "build", context)
  try {
    expect(await select({ mode: "agent" })).toBe("custom")
    expect(await select({ mode: "agent", agent_profile: "custom" })).toBe("custom")
    expect(await select({ mode: "plan", agent_profile: "custom" })).toBe("plan")
    expect(await select({ mode: "agent", lane: "auxiliary", agent_profile: "missing" })).toBe("chat")
    for (const name of ["hidden", "helper", "dream", "plan", "missing"]) await expect(select({ mode: "agent", agent_profile: name })).rejects.toMatchObject({ data: { code: "AGENT_PROFILE_UNAVAILABLE" } })
    process.env.OPEN_CLANK_MANAGED = "0"
    expect(await select({ mode: "agent", agent_profile: "custom" })).toBe("build")
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
  }
})


test("source-map counts the full native actor transcript and honors an exact fork cutoff", async () => {
  const previous = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  const first = MessageID.ascending()
  const assistant = MessageID.ascending()
  const cutoff = MessageID.ascending()
  const calls: Array<Record<string, unknown>> = []
  const receiver = { sessionManager: { get: () => ({ cwd: "/workspace/source" }) }, sdk: { session: {
    messages: async (params: Record<string, unknown>) => { calls.push(params); return { data: [{ info: { id: first, role: "user", agentID: "main" }, parts: [{ type: "text", metadata: { hostSourceMessageID: "root-first" } }] }, { info: { id: assistant, role: "assistant", agentID: "child" }, parts: [] }, { info: { id: cutoff, role: "user", agentID: "main" }, parts: [{ type: "text", metadata: { hostSourceMessageID: "root-second" } }] }] } },
    get: async () => ({ data: { revert: { messageID: cutoff } } }),
  } } } as unknown as ACP.Agent
  try {
    const result = await ACP.Agent.prototype.extMethod.call(receiver, "_openclank/session/v1/messages/source-map", { sessionID: "owned", directory: "/workspace/source", messageID: cutoff })
    expect(calls[0]).toMatchObject({ agent_id: "*" })
    expect(result).toMatchObject({ sourceMessageCount: 2, anchors: [{ nativeMessageID: first, hostSourceMessageID: "root-first" }], revert: { messageID: cutoff }, forkReceipt: null })
    await expect(ACP.Agent.prototype.extMethod.call(receiver, "_openclank/session/v1/messages/source-map", { sessionID: "owned", directory: "/workspace/source", messageID: "invalid" })).rejects.toMatchObject({ code: -32602 })
    expect(calls).toHaveLength(1)
  } finally {
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
  }
})
