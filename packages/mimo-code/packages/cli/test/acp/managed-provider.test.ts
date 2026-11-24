import { afterEach, beforeEach, expect, test } from "bun:test"
import { Effect, Fiber } from "effect"
import { createOpenAI } from "@ai-sdk/openai"
import { JSONParseError } from "@ai-sdk/provider"
import { notePinnedResponsesSDK, notePinnedResponsesModel } from "../../src/provider/malformed-origin"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { captureFetch, withSdkDispatches, observeCodexInnerSend, recordSdkFailure, markSdkSuccessStep, takeSdkSuccessStep, markMalformedResponse, hasMalformedResponse, hasObservedMalformedResponse, emitMalformedDiagnostic } from "../../src/provider/logging-transport"
import { requestManagedCwdForTest } from "../../src/tool/change-directory"
import { beginManagedSessionTransition, managedSessionBinding, markManagedSessionReconciling, registerMemorySessionScope, unregisterMemorySessionScope } from "../../src/memory/session-scope"

const route = {
  rootOperationID: "root-1",
  connectionID: "openai-api",
  providerID: "openai",
  billingLane: "metered_api" as const,
  modelID: "gpt-test",
  modelRouteID: "route-gpt-test",
}

beforeEach(() => {
  process.env.OPEN_CLANK_MANAGED = "1"
  ManagedProvider.resetForTest()
})

afterEach(() => {
  ManagedProvider.resetForTest()
  delete process.env.OPEN_CLANK_MANAGED
})

test("managed turns bind and lease through the host and discard scope afterward", async () => {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  const host = {
    async extMethod(method: string, params: Record<string, unknown>) {
      calls.push({ method, params })
      if (method.endsWith("/account/bind")) {
        return {
          bindingID: "binding-1",
          bindingRevision: 1,
          rootOperationID: "root-1",
          connectionID: "openai-api",
          providerID: "openai",
          billingLane: "metered_api",
          modelID: "gpt-test",
          credentialRequired: true,
          accountID: "account-a",
          credentialRevision: 7,
          source: "round_robin",
          attempt: 1,
          committed: false,
        }
      }
      if (method.endsWith("/credential/lease")) {
        return {
          leaseID: "lease-1",
          connectionID: "openai-api",
          accountID: "account-a",
          credentialRevision: 7,
          expiresAt: Date.now() + 30_000,
          credential: { type: "api", key: "leased-secret" },
        }
      }
      throw new Error("unexpected callback")
    },
  }
  ManagedProvider.installHostConnection(host as any)

  const result = await ManagedProvider.withOperation(
    "session-1",
    route,
    { providerID: "openai-api", modelID: "gpt-test" },
    async () => {
      const scope = ManagedProvider.requireScope("session-1")
      expect(scope.accountID).toBe("account-a")
      expect(scope.credential).toEqual({ type: "api", key: "leased-secret" })
      return "done"
    },
  )

  expect(result).toBe("done")
  expect(ManagedProvider.currentScope("session-1")).toBeUndefined()
  expect(calls.map((call) => call.method)).toEqual([
    "_openclank/provider-store/v1/account/bind",
    "_openclank/provider-store/v1/credential/lease",
    "_openclank/logging/v1/admit",
  ])
  expect(calls.at(-1)?.params).toEqual({ bindingID: "binding-1", rootOperationID: "root-1" })
  expect(JSON.stringify(calls)).not.toContain("leased-secret")
})

test("managed operation delegate is fail-closed and uninstallable", async () => {
  expect(() => ManagedProvider.managedOperationDelegate()).toThrow("unavailable")
  const delegate = {
    execute: async (request: any) => ({ operationID: "op", rootOperationID: request.rootOperationID, operation: request.operation, modelRouteID: "route", connectionID: "conn", billingLane: "subscription", state: "complete", committed: true, output: {}, artifacts: [], replayed: false }),
    cancel: async (request: any) => ({ operationID: "op", rootOperationID: request.rootOperationID, state: "pending" }),
  }
  const dispose = ManagedProvider.installManagedOperationDelegate(delegate as any)
  expect(ManagedProvider.managedOperationDelegate()).toBe(delegate as any)
  expect(() => ManagedProvider.installManagedOperationDelegate(delegate as any)).toThrow("already installed")
  dispose()
  expect(() => ManagedProvider.managedOperationDelegate()).toThrow("unavailable")
})

test("fresh operation lease reserves one session and releases after transfer", async () => {
  let entered!: () => void
  let continueBind!: () => void
  const bindingStarted = new Promise<void>((resolve) => { entered = resolve })
  const bindingGate = new Promise<void>((resolve) => { continueBind = resolve })
  const localRoute = {
    rootOperationID: "root-1", connectionID: "ollama-local", providerID: "ollama",
    billingLane: "local" as const, modelID: "qwen-local", modelRouteID: "route-qwen-local",
  }
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (method.endsWith("/account/bind")) {
        entered()
        await bindingGate
        return {
          bindingID: "binding-fresh", bindingRevision: 1, rootOperationID: "root-1",
          connectionID: "ollama-local", providerID: "ollama", billingLane: "local",
          modelID: "qwen-local", credentialRequired: false, source: "keyless", attempt: 1, committed: false,
        }
      }
      if (method.endsWith("/logging/admit")) return { bindingID: "binding-fresh", rootOperationID: "root-1" }
      throw new Error(`unexpected callback ${method}`)
    },
  } as any)
  const pending = Effect.runFork(ManagedProvider.acquireOperationEffect({
    sessionID: "session-fresh", route: localRoute, model: { providerID: "ollama-local", modelID: "qwen-local" },
    hostContext: { accountID: "account-a", chatID: "chat-a" },
  }))
  await bindingStarted
  expect(ManagedProvider.operationScopeAvailable("session-fresh")).toBe(false)
  await expect(Effect.runPromise(ManagedProvider.acquireOperationEffect({
    sessionID: "session-fresh", route: localRoute, model: { providerID: "ollama-local", modelID: "qwen-local" },
    hostContext: { accountID: "account-a", chatID: "chat-a" },
  }))).rejects.toThrow("already active")
  continueBind()
  const lease = await Effect.runPromise(Fiber.join(pending))
  expect(ManagedProvider.requireScope("session-fresh").accountID).toBeUndefined()
  await Effect.runPromise(lease.release)
  expect(ManagedProvider.operationScopeAvailable("session-fresh")).toBe(true)
})

test("host context is isolated by session and cleaned up after overlap or failure", async () => {
  const contextA = { accountID: "account-a", chatID: "chat-a", cwd: "/work/a" }
  const contextB = { accountID: "account-b", chatID: "chat-b", cwd: "/work/b" }
  const entered: string[] = []
  await Promise.all([
    ManagedProvider.withHostContext("session-a", contextA, async () => {
      entered.push(ManagedProvider.currentHostContext("session-a")!.chatID!)
      await new Promise((resolve) => setTimeout(resolve, 5))
      expect(ManagedProvider.currentHostContext("session-b")).toBeUndefined()
    }),
    ManagedProvider.withHostContext("session-b", contextB, async () => {
      entered.push(ManagedProvider.currentHostContext("session-b")!.chatID!)
    }),
  ])
  expect(entered.toSorted()).toEqual(["chat-a", "chat-b"])
  expect(ManagedProvider.currentHostContext("session-a")).toBeUndefined()
  expect(ManagedProvider.currentHostContext("session-b")).toBeUndefined()

  await expect(
    ManagedProvider.withHostContext("session-failure", contextA, async () => {
      throw new Error("prompt failed")
    }),
  ).rejects.toThrow("prompt failed")
  expect(ManagedProvider.currentHostContext("session-failure")).toBeUndefined()

  await ManagedProvider.withHostContext("session-nested", contextA, async () => {
    await ManagedProvider.withHostContext("session-nested", { cwd: "/work/a", chatID: "chat-a", accountID: "account-a" }, async () => {
      expect(ManagedProvider.currentHostContext("session-nested")?.chatID).toBe("chat-a")
    })
    expect(ManagedProvider.currentHostContext("session-nested")?.chatID).toBe("chat-a")
  })
  await expect(
    ManagedProvider.withHostContext("session-conflict", contextA, async () =>
      ManagedProvider.withHostContext("session-conflict", contextB, async () => undefined),
    ),
  ).rejects.toThrow("different managed host context")
  expect(ManagedProvider.currentHostContext("session-conflict")).toBeUndefined()
})

test("pre-commit quota failover re-leases the next account and commit fences it", async () => {
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  const binding = (accountID: string, revision: number, committed = false) => ({
    bindingID: "binding-rotate",
    bindingRevision: revision,
    rootOperationID: "root-1",
    connectionID: "openai-api",
    providerID: "openai",
    billingLane: "metered_api",
    modelID: "gpt-test",
    credentialRequired: true,
    accountID,
    credentialRevision: accountID === "account-a" ? 7 : 11,
    source: accountID === "account-a" ? "round_robin" : "failover",
    attempt: accountID === "account-a" ? 1 : 2,
    committed,
  })
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, unknown>) {
      calls.push({ method, params })
      if (method.endsWith("/account/bind")) return binding("account-a", 1)
      if (method.endsWith("/credential/lease")) {
        const accountID = String(params.accountID)
        return {
          leaseID: `lease-${accountID}`,
          connectionID: "openai-api",
          accountID,
          credentialRevision: accountID === "account-a" ? 7 : 11,
          expiresAt: Date.now() + 30_000,
          credential: { type: "api", key: `secret-${accountID}` },
        }
      }
      if (method.endsWith("/account/attempt")) {
        expect(params).toMatchObject({
          bindingID: "binding-rotate",
          expectedRevision: 1,
          accountID: "account-a",
          outcome: "quota",
          retryAfterMs: 3000,
        })
        return binding("account-b", 2)
      }
      if (method.endsWith("/account/commit")) {
        expect(params).toEqual({ bindingID: "binding-rotate", expectedRevision: 2 })
        return binding("account-b", 3, true)
      }
      throw new Error(`unexpected callback ${method}`)
    },
  } as any)

  await ManagedProvider.withOperation(
    "session-rotate",
    route,
    { providerID: "openai-api", modelID: "gpt-test" },
    async () => {
      expect(ManagedProvider.requireScope("session-rotate").accountID).toBe("account-a")
      const decision = await ManagedProvider.recordSessionAttempt("session-rotate", {
        status: 429,
        message: "rate limit",
        responseHeaders: { "retry-after": "3" },
      })
      expect(decision).toEqual({ outcome: "quota", retry: true, rotated: true, retryAfterMs: 3000 })
      const next = ManagedProvider.requireScope("session-rotate")
      expect(next.accountID).toBe("account-b")
      expect(next.credential).toEqual({ type: "api", key: "secret-account-b" })
      await ManagedProvider.commitSessionOperation("session-rotate")
    },
  )

  expect(calls.map((call) => call.method)).toEqual([
    "_openclank/provider-store/v1/account/bind",
    "_openclank/provider-store/v1/credential/lease",
    "_openclank/logging/v1/admit",
    "_openclank/provider-store/v1/account/attempt",
    "_openclank/provider-store/v1/credential/lease",
    "_openclank/logging/v1/admit",
    "_openclank/provider-store/v1/account/commit",
  ])
  expect(calls.filter((call) => call.method === "_openclank/logging/v1/admit").map((call) => call.params))
    .toEqual(Array(2).fill({ bindingID: "binding-rotate", rootOperationID: "root-1" }))
  expect(JSON.stringify(calls)).not.toContain("secret-account")
})

test("managed transient failures retry the same account at most twice", async () => {
  let revision = 1
  const sameBinding = () => ({
    bindingID: "binding-transient",
    bindingRevision: revision,
    rootOperationID: "root-1",
    connectionID: "openai-api",
    providerID: "openai",
    billingLane: "metered_api",
    modelID: "gpt-test",
    credentialRequired: true,
    accountID: "account-a",
    credentialRevision: 7,
    source: "round_robin",
    attempt: revision,
    committed: false,
  })
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, unknown>) {
      if (method.endsWith("/account/bind")) return sameBinding()
      if (method.endsWith("/credential/lease")) {
        return {
          leaseID: "lease-a",
          connectionID: "openai-api",
          accountID: "account-a",
          credentialRevision: 7,
          expiresAt: Date.now() + 30_000,
          credential: { type: "api", key: "secret-a" },
        }
      }
      if (method.endsWith("/account/attempt")) {
        expect(params.accountID).toBe("account-a")
        expect(params.outcome).toBe("transient")
        revision += 1
        return sameBinding()
      }
      throw new Error(`unexpected callback ${method}`)
    },
  } as any)

  await ManagedProvider.withOperation(
    "session-transient",
    route,
    { providerID: "openai-api", modelID: "gpt-test" },
    async () => {
      const error = Object.assign(new Error("network error"), { code: "ECONNRESET" })
      expect((await ManagedProvider.recordSessionAttempt("session-transient", error)).retry).toBe(true)
      expect((await ManagedProvider.recordSessionAttempt("session-transient", error)).retry).toBe(true)
      const exhausted = await ManagedProvider.recordSessionAttempt("session-transient", error)
      expect(exhausted.retry).toBe(false)
      expect(exhausted.rotated).toBe(false)
      expect(ManagedProvider.requireScope("session-transient").accountID).toBe("account-a")
    },
  )
})

test("managed binding mismatches fail before a credential request", async () => {
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      calls.push(method)
      return {
        bindingID: "binding-1",
        bindingRevision: 1,
        rootOperationID: "other-root",
        connectionID: "openai-api",
        providerID: "openai",
        billingLane: "metered_api",
        modelID: "gpt-test",
        credentialRequired: true,
        accountID: "account-a",
        credentialRevision: 1,
        source: "round_robin",
        attempt: 1,
        committed: false,
      }
    },
  } as any)

  await expect(
    ManagedProvider.beginOperation(route, {
      providerID: "openai-api",
      modelID: "gpt-test",
    }),
  ).rejects.toThrow("escaped its requested route")
  expect(calls).toHaveLength(1)
})

test("keyless local turns bind without requesting a credential lease", async () => {
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      calls.push(method)
      return {
        bindingID: "binding-local",
        bindingRevision: 1,
        rootOperationID: "root-local",
        connectionID: "ollama-local",
        providerID: "ollama",
        billingLane: "local",
        modelID: "qwen-local",
        credentialRequired: false,
        source: "keyless",
        attempt: 1,
        committed: false,
      }
    },
  } as any)

  const scope = await ManagedProvider.beginOperation(
    {
      rootOperationID: "root-local",
      connectionID: "ollama-local",
      providerID: "ollama",
      billingLane: "local",
      modelID: "qwen-local",
      modelRouteID: "route-qwen-local",
    },
    { providerID: "ollama-local", modelID: "qwen-local" },
  )
  expect(scope).toEqual({
    connectionID: "ollama-local",
    billingLane: "local",
    credentialRequired: false,
  })
  expect(calls).toEqual(["_openclank/provider-store/v1/account/bind", "_openclank/logging/v1/admit"])
})

test("refresh lifecycle uses account-scoped lease and credential revision CAS", async () => {
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, unknown>) {
      calls.push(method)
      if (method.endsWith("/refresh/acquire") || method.endsWith("/refresh/renew")) {
        return {
          leaseID: "refresh-lease",
          connectionID: "openai-api",
          accountID: "account-a",
          credentialRevision: 3,
          expiresAt: Date.now() + 60_000,
          renewable: method.endsWith("/refresh/acquire"),
        }
      }
      if (method.endsWith("/refresh/commit")) {
        return {
          id: "account-a",
          label: "A",
          enabled: true,
          order: 0,
          revision: 4,
          credentialRevision: 4,
          credential: params.credential,
        }
      }
      if (method.endsWith("/refresh/abort")) return {}
      throw new Error("unexpected callback")
    },
  } as any)

  const acquired = await ManagedProvider.acquireRefresh({
    connectionID: "openai-api",
    accountID: "account-a",
    expectedRevision: 3,
  })
  await ManagedProvider.renewRefresh({ leaseID: acquired.leaseID })
  const committed = await ManagedProvider.commitRefresh({
    leaseID: acquired.leaseID,
    connectionID: "openai-api",
    accountID: "account-a",
    expectedRevision: 3,
    credential: { type: "api", key: "rotated" },
  })
  await ManagedProvider.abortRefresh({ leaseID: "unused-lease" })
  expect(committed.credentialRevision).toBe(4)
  expect(calls).toEqual([
    "_openclank/provider-store/v1/refresh/acquire",
    "_openclank/provider-store/v1/refresh/renew",
    "_openclank/provider-store/v1/refresh/commit",
    "_openclank/provider-store/v1/refresh/abort",
  ])
})

test("OAuth refresh exchanges only under a host lease and commits durably", async () => {
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, any>) {
      calls.push(method)
      if (method.endsWith("/refresh/acquire")) {
        return {
          leaseID: "refresh-managed",
          connectionID: "openai-subscription",
          accountID: "account-a",
          credentialRevision: 5,
          expiresAt: Date.now() + 60_000,
          renewable: true,
        }
      }
      if (method.endsWith("/refresh/commit")) {
        return {
          id: "account-a",
          label: "A",
          enabled: true,
          order: 0,
          revision: 6,
          credentialRevision: 6,
          credential: params.credential,
        }
      }
      throw new Error("unexpected callback")
    },
  } as any)

  const refreshed = await ManagedProvider.refreshOAuthCredential({
    connectionID: "openai-subscription",
    accountID: "account-a",
    expectedRevision: 5,
    credential: { type: "oauth", access: "old", refresh: "old-r", expires: 1 },
    exchange: async (current) => ({
      ...current,
      access: "new",
      refresh: "new-r",
      expires: Date.now() + 3_600_000,
    }),
  })

  expect(refreshed.credentialRevision).toBe(6)
  expect(refreshed.credential.access).toBe("new")
  expect(calls).toEqual([
    "_openclank/provider-store/v1/refresh/acquire",
    "_openclank/provider-store/v1/refresh/commit",
  ])
})

test("an upstream-rotated token that cannot persist becomes reauth-required", async () => {
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      calls.push(method)
      if (method.endsWith("/refresh/acquire")) {
        return {
          leaseID: "refresh-fail",
          connectionID: "xai-subscription",
          accountID: "account-x",
          credentialRevision: 2,
          expiresAt: Date.now() + 60_000,
          renewable: true,
        }
      }
      if (method.endsWith("/refresh/commit")) throw new Error("disk unavailable")
      if (method.endsWith("/refresh/abort")) return {}
      throw new Error("unexpected callback")
    },
  } as any)

  const failure = await ManagedProvider.refreshOAuthCredential({
    connectionID: "xai-subscription",
    accountID: "account-x",
    expectedRevision: 2,
    credential: { type: "oauth", access: "old", refresh: "old-r", expires: 1 },
    exchange: async (current) => ({
      ...current,
      access: "rotated",
      refresh: "rotated-r",
      expires: 9_999_999,
    }),
  }).catch((error) => error)

  expect(failure).toBeInstanceOf(ManagedProvider.ManagedRefreshPersistenceError)
  expect(failure.status).toBe(401)
  expect(calls).toEqual([
    "_openclank/provider-store/v1/refresh/acquire",
    "_openclank/provider-store/v1/refresh/commit",
    "_openclank/provider-store/v1/refresh/abort",
  ])
})

test("standalone mode keeps the callback-free compatibility path", async () => {
  delete process.env.OPEN_CLANK_MANAGED
  const result = await ManagedProvider.withOperation(
    "standalone",
    undefined,
    { providerID: "openai", modelID: "gpt-test" },
    async () => 42,
  )
  expect(result).toBe(42)
})

function bindingDescriptor(sessionID: string, cwd = "/work", mapRevision = 4, mappingRevision = 2) {
  return {
    name: "lifetools_test",
    command: "python",
    args: [],
    env: [
      { name: "FM_OWNER", value: "alice" },
      { name: "FM_WORKSPACE_ID", value: "global" },
      { name: "SESSION_ID", value: "chat-1" },
      { name: "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", value: "authority" },
      { name: "COPAL_WORKSPACE", value: "copal" },
      { name: "WORKSPACE", value: cwd },
      { name: "OPEN_CLANK_ENGINE_SESSION_ALIASES", value: "[]" },
      { name: "OPEN_CLANK_SESSION_BINDING_REVISION", value: "1" },
      { name: "OPEN_CLANK_SESSION_MAP_REVISION", value: String(mapRevision) },
      { name: "OPEN_CLANK_SESSION_MAPPING_REVISION", value: String(mappingRevision) },
      { name: "FM_MEMORY_ENABLED", value: "1" },
      { name: "OPEN_CLANK_TEST_SESSION", value: sessionID },
    ],
  } as any
}

test("binding admission reads authoritative state after marker invalidation", async () => {
  const sessionID = "engine-binding-read"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  let reads = 0
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      expect(method).toBe("_openclank/session/v1/binding/read")
      reads += 1
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: [],
        canonicalCwd: "/work/child",
        memoryWorkspaceID: "global",
        authorityWorkspaceID: "authority",
        copalWorkspace: "copal",
        workspaceRevision: 1,
        mapRevision: 5,
        mappingRevision: 2,
        memoryEnabled: true,
      }
    },
  } as any)
  const [first, second] = await Promise.all([
    ManagedProvider.ensureManagedSessionBinding(sessionID),
    ManagedProvider.ensureManagedSessionBinding(sessionID),
  ])
  expect(reads).toBe(1)
  expect(first?.physicalCwd).toBe("/work/child")
  expect(second?.physicalCwd).toBe("/work/child")
  expect(managedSessionBinding(sessionID)?.mapRevision).toBe(5)
  unregisterMemorySessionScope(sessionID)
})

test("managed history uses only bound host callbacks for queries, media, mutations, and outages", async () => {
  const sessionID = "engine-history"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  const calls: Array<{ method: string; params: Record<string, unknown> }> = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, unknown>) {
      calls.push({ method, params })
      if (method === "_openclank/history/v1/query" && params.operation === "search") {
        if (params.query === "outage") throw new Error("host history unavailable")
        return { ok: true, operation: "search", result: { ok: true, hits: [], limit: 10, more: false } }
      }
      if (method === "_openclank/history/v1/query" && params.operation === "media") {
        return { ok: true, operation: "media", result: { ok: true, attachments: [{ asset_id: "asset-1", mime_type: "image/png", filename: null, byte_size: 3 }] } }
      }
      if (method === "_openclank/history/v1/mutate") {
        return { ok: true, operation: params.operation, accepted: 1, duplicate: 0, enqueued: 1 }
      }
      throw new Error(`unexpected callback ${method}`)
    },
  } as any)

  await expect(ManagedProvider.historyCall(sessionID, "search", { query: "host only", limit: 10 })).resolves.toEqual({ ok: true, hits: [], limit: 10, more: false })
  await expect(ManagedProvider.historyCall(sessionID, "media", { assetID: "asset-1" })).resolves.toEqual({ ok: true, attachments: [{ asset_id: "asset-1", mime_type: "image/png", filename: null, byte_size: 3 }] })
  await expect(ManagedProvider.historyMutate(sessionID, "replay", [{ messageID: "m", partID: "p", role: "user", partType: "text", content: "replayed" }])).resolves.toMatchObject({ accepted: 1 })
  await expect(ManagedProvider.historyCall(sessionID, "search", { query: "outage" })).rejects.toThrow("host history unavailable")
  expect(calls.map((call) => call.method)).toEqual([
    "_openclank/history/v1/query",
    "_openclank/history/v1/query",
    "_openclank/history/v1/mutate",
    "_openclank/history/v1/query",
  ])
  expect(calls.every((call) => !("owner" in call.params) && !("stableChatID" in call.params))).toBe(true)
  unregisterMemorySessionScope(sessionID)
})

test("session cwd accepts exact revisions and preserves explicit rejection", async () => {
  let response: unknown = {
    outcome: "accepted",
    canonicalCwd: "/work/child",
    workspaceRevision: 2,
    changed: true,
    transitionID: "transition-1",
  }
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      expect(method).toBe("_openclank/session/v1/cwd/change")
      return response
    },
  } as any)
  const accepted = await ManagedProvider.requestSessionCwdChange("engine-cwd", "/work/child", 1, "transition-1")
  expect(accepted.outcome).toBe("accepted")
  response = { outcome: "accepted", canonicalCwd: "/work/child", workspaceRevision: 3, changed: true, transitionID: "transition-extra", forged: true }
  await expect(ManagedProvider.requestSessionCwdChange("engine-cwd", "/work/child", 2, "transition-extra")).rejects.toThrow("invalid accepted")
  response = { outcome: "rejected", committed: false, code: "workspace_rejected", transitionID: "transition-2" }
  const rejected = await ManagedProvider.requestSessionCwdChange("engine-cwd", "/work/other", 2, "transition-2")
  expect(rejected.outcome).toBe("rejected")
  response = { outcome: "accepted", canonicalCwd: "/work/child", workspaceRevision: 4, changed: false, transitionID: "transition-3" }
  await expect(ManagedProvider.requestSessionCwdChange("engine-cwd", "/work/child", 3, "transition-3")).rejects.toThrow("invalid accepted")
  response = { outcome: "rejected", committed: false, code: "not-a-contract-code", transitionID: "transition-4" }
  await expect(ManagedProvider.requestSessionCwdChange("engine-cwd", "/work/other", 3, "transition-4")).rejects.toThrow("invalid session cwd result")
})

test("ambiguous cwd response settles unchanged authority and fails the requested transition", async () => {
  const sessionID = "engine-cwd-ambiguous-old"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (method.endsWith("/cwd/change")) throw new Error("ambiguous host response")
      if (method.endsWith("/binding/read")) {
        return {
          engineSessionID: sessionID,
          stableChatID: "chat-1",
          owner: "alice",
          engineAliases: [],
          canonicalCwd: "/work",
          memoryWorkspaceID: "global",
          authorityWorkspaceID: "authority",
          copalWorkspace: "copal",
          workspaceRevision: 1,
          mapRevision: 4,
          mappingRevision: 2,
          memoryEnabled: true,
        }
      }
      throw new Error(`unexpected ${method}`)
    },
  } as any)
  await expect(requestManagedCwdForTest(sessionID, "/work/child")).rejects.toThrow("ambiguous host response")
  expect(managedSessionBinding(sessionID)?.physicalCwd).toBe("/work")
  expect(managedSessionBinding(sessionID)?.transition).toBeNull()
  unregisterMemorySessionScope(sessionID)
})

test("ambiguous cwd response accepts a durably advanced authority", async () => {
  const sessionID = "engine-cwd-ambiguous-new"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (method.endsWith("/cwd/change")) throw new Error("ambiguous host response")
      if (method.endsWith("/binding/read")) {
        return {
          engineSessionID: sessionID,
          stableChatID: "chat-1",
          owner: "alice",
          engineAliases: [],
          canonicalCwd: "/work/child",
          memoryWorkspaceID: "global",
          authorityWorkspaceID: "authority",
          copalWorkspace: "copal",
          workspaceRevision: 2,
          mapRevision: 5,
          mappingRevision: 2,
          memoryEnabled: true,
        }
      }
      throw new Error(`unexpected ${method}`)
    },
  } as any)
  const result = await requestManagedCwdForTest(sessionID, "/work/child")
  expect(result).toMatchObject({ outcome: "accepted", canonicalCwd: "/work/child", workspaceRevision: 2 })
  expect(managedSessionBinding(sessionID)?.transition).toBeNull()
  unregisterMemorySessionScope(sessionID)
})

test("binding admission rejects a changed transition while its read is pending", async () => {
  const sessionID = "engine-binding-transition-race"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  let release!: (value: unknown) => void
  const pending = new Promise((resolve) => { release = resolve })
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return pending
    },
  } as any)
  const admitted = ManagedProvider.ensureManagedSessionBinding(sessionID)
  beginManagedSessionTransition(sessionID, "/work/other", "transition-race", 1)
  release({
    engineSessionID: sessionID,
    stableChatID: "chat-1",
    owner: "alice",
    engineAliases: [],
    canonicalCwd: "/work",
    memoryWorkspaceID: "global",
    authorityWorkspaceID: "authority",
    copalWorkspace: "copal",
    workspaceRevision: 1,
    mapRevision: 4,
    mappingRevision: 2,
    memoryEnabled: true,
  })
  await expect(admitted).rejects.toThrow("binding changed during reconciliation")
  expect(managedSessionBinding(sessionID)?.transition?.transitionID).toBe("transition-race")
  unregisterMemorySessionScope(sessionID)
})

test("binding admission rejects authority evidence that regresses map revisions", async () => {
  const sessionID = "engine-binding-map-regression"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID, "/work", 4, 2)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: [],
        canonicalCwd: "/work",
        memoryWorkspaceID: "global",
        authorityWorkspaceID: "authority",
        copalWorkspace: "copal",
        workspaceRevision: 1,
        mapRevision: 3,
        mappingRevision: 2,
        memoryEnabled: true,
      }
    },
  } as any)
  await expect(ManagedProvider.ensureManagedSessionBinding(sessionID)).rejects.toThrow("map evidence regressed")
  expect(managedSessionBinding(sessionID)?.physicalCwd).toBe("/work")
  unregisterMemorySessionScope(sessionID)
})

test("managed owner admission ignores a conflicting process environment", () => {
  const sessionID = "engine-owner-authority"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  process.env.OPEN_CLANK_OWNER = "forged-owner"
  expect(ManagedProvider.managedSessionOwner(sessionID)).toBe("alice")
  delete process.env.OPEN_CLANK_OWNER
  unregisterMemorySessionScope(sessionID)
})

test("generic admission refuses reconciling state while the explicit retry can settle it", async () => {
  const sessionID = "engine-binding-reconciling"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  beginManagedSessionTransition(sessionID, "/work/child", "reconcile-1", 1)
  await expect(ManagedProvider.ensureManagedSessionBinding(sessionID)).rejects.toThrow("in_flight")
  markManagedSessionReconciling(sessionID, "reconcile-1", 1)
  await expect(ManagedProvider.ensureManagedSessionBinding(sessionID)).rejects.toThrow("reconciling")
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: [],
        canonicalCwd: "/work/child",
        memoryWorkspaceID: "global",
        authorityWorkspaceID: "authority",
        copalWorkspace: "copal",
        workspaceRevision: 2,
        mapRevision: 4,
        mappingRevision: 2,
        memoryEnabled: true,
      }
    },
  } as any)
  await expect(ManagedProvider.admitManagedSessionBinding(sessionID)).rejects.toThrow("reconciling")
  const settled = await ManagedProvider.admitManagedSessionBinding(sessionID)
  expect(settled?.physicalCwd).toBe("/work/child")
  expect(settled?.transition).toBeNull()
  unregisterMemorySessionScope(sessionID)
})

test("explicit reconciliation clears an uncommitted transition when authority remains unchanged", async () => {
  const sessionID = "engine-binding-reconciling-unchanged"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  beginManagedSessionTransition(sessionID, "/work/child", "reconcile-unchanged", 1)
  markManagedSessionReconciling(sessionID, "reconcile-unchanged", 1)
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: [],
        canonicalCwd: "/work",
        memoryWorkspaceID: "global",
        authorityWorkspaceID: "authority",
        copalWorkspace: "copal",
        workspaceRevision: 1,
        mapRevision: 4,
        mappingRevision: 2,
        memoryEnabled: true,
      }
    },
  } as any)
  await expect(ManagedProvider.admitManagedSessionBinding(sessionID)).rejects.toThrow("reconciling")
  expect(managedSessionBinding(sessionID)?.physicalCwd).toBe("/work")
  expect(managedSessionBinding(sessionID)?.transition).toBeNull()
  await expect(ManagedProvider.admitManagedSessionBinding(sessionID)).resolves.toMatchObject({
    physicalCwd: "/work",
    transition: null,
  })
  unregisterMemorySessionScope(sessionID)
})

test("explicit reconciliation replaces every authority descriptor axis", async () => {
  const sessionID = "engine-binding-reconciling-axes"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  beginManagedSessionTransition(sessionID, "/work/child", "reconcile-axes", 1)
  markManagedSessionReconciling(sessionID, "reconcile-axes", 1)
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: ["prior-engine"],
        canonicalCwd: "/work",
        memoryWorkspaceID: "memory-new",
        authorityWorkspaceID: "authority-new",
        copalWorkspace: "copal-new",
        workspaceRevision: 1,
        mapRevision: 4,
        mappingRevision: 2,
        memoryEnabled: false,
      }
    },
  } as any)
  await expect(ManagedProvider.admitManagedSessionBinding(sessionID)).rejects.toThrow("reconciling")
  expect(managedSessionBinding(sessionID)).toMatchObject({
    engineAliases: ["prior-engine"],
    memoryWorkspaceID: "memory-new",
    authorityWorkspaceID: "authority-new",
    copalWorkspace: "copal-new",
    memoryEnabled: false,
    transition: null,
  })
  unregisterMemorySessionScope(sessionID)
})

test("binding read rejects extra authority fields", async () => {
  const sessionID = "engine-binding-extra-field"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return {
        engineSessionID: sessionID,
        stableChatID: "chat-1",
        owner: "alice",
        engineAliases: [],
        canonicalCwd: "/work",
        memoryWorkspaceID: "global",
        authorityWorkspaceID: "authority",
        copalWorkspace: "copal",
        workspaceRevision: 1,
        mapRevision: 4,
        mappingRevision: 2,
        memoryEnabled: true,
        forged: true,
      }
    },
  } as any)
  await expect(ManagedProvider.ensureManagedSessionBinding(sessionID)).rejects.toThrow("invalid managed session binding")
  unregisterMemorySessionScope(sessionID)
})

test("a pending binding read cannot install after unregister and re-registration", async () => {
  const sessionID = "engine-binding-generation-race"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  let release!: (value: unknown) => void
  const pending = new Promise((resolve) => { release = resolve })
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return pending
    },
  } as any)
  const admitted = ManagedProvider.ensureManagedSessionBinding(sessionID)
  unregisterMemorySessionScope(sessionID)
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID, "/work/new")], "/work/new")
  release({
    engineSessionID: sessionID,
    stableChatID: "chat-1",
    owner: "alice",
    engineAliases: [],
    canonicalCwd: "/work",
    memoryWorkspaceID: "global",
    authorityWorkspaceID: "authority",
    copalWorkspace: "copal",
    workspaceRevision: 1,
    mapRevision: 4,
    mappingRevision: 2,
    memoryEnabled: true,
  })
  await expect(admitted).rejects.toThrow("registration changed")
  expect(managedSessionBinding(sessionID)?.physicalCwd).toBe("/work/new")
  unregisterMemorySessionScope(sessionID)
})

test("a pending binding read rejects a same-generation re-registration with changed authority axes", async () => {
  const sessionID = "engine-binding-auxiliary-generation-race"
  registerMemorySessionScope(sessionID, [bindingDescriptor(sessionID)], "/work")
  ManagedProvider.invalidateManagedSessionBindingMarkerForTest(sessionID)
  let release!: (value: unknown) => void
  const pending = new Promise((resolve) => { release = resolve })
  ManagedProvider.installHostConnection({
    async extMethod(method: string) {
      if (!method.endsWith("/binding/read")) throw new Error(`unexpected ${method}`)
      return pending
    },
  } as any)
  const admitted = ManagedProvider.ensureManagedSessionBinding(sessionID)
  unregisterMemorySessionScope(sessionID)
  const replacement = bindingDescriptor(sessionID)
  replacement.env = replacement.env.map((item: { name: string; value: string }) =>
    item.name === "COPAL_WORKSPACE" ? { ...item, value: "copal-new" } : item,
  )
  registerMemorySessionScope(sessionID, [replacement], "/work")
  release({
    engineSessionID: sessionID,
    stableChatID: "chat-1",
    owner: "alice",
    engineAliases: [],
    canonicalCwd: "/work",
    memoryWorkspaceID: "global",
    authorityWorkspaceID: "authority",
    copalWorkspace: "copal",
    workspaceRevision: 1,
    mapRevision: 4,
    mappingRevision: 2,
    memoryEnabled: true,
  })
  await expect(admitted).rejects.toThrow("registration changed")
  expect(managedSessionBinding(sessionID)?.copalWorkspace).toBe("copal-new")
  unregisterMemorySessionScope(sessionID)
})

test("Codex failure retains only the initiating capture across an accepted refresh CAS", async () => {
  const bootstrap = { stableChatID: "chat", owner: "alice", rootSessionID: "session", sessionID: "session", actorID: "main", rootOperationID: "root", expectedExecutionRevision: 0, mapRevision: 1, mappingRevision: 1, workspaceRevision: 1, modelRouteID: "route" }
  const preflight = { bootstrap, connectionID: "conn", providerID: "openai", billingLane: "subscription" as const, modelID: "model", modelRouteID: "route" }
  const identity = { sessionID: "session", actorID: "main", executionRevision: 1, rootOperationID: "root" }
  const base = { ...preflight, ...identity, bindingID: "binding", bindingRevision: 1, scopeKind: "actor_execution", credentialRequired: true, accountID: "account", source: "round_robin", attempt: 1, committed: false }
  let revision = 1
  let attempt: any
  const credential = { type: "oauth" as const, access: "synthetic", refresh: "synthetic", expires: Date.now()+600_000 }
  ManagedProvider.installHostConnection({ async extMethod(method: string, params: any) {
    if (method.endsWith('/account/bind')) return { ...base, credentialRevision: revision }
    if (method.endsWith('/credential/lease')) return { leaseID:'lease',connectionID:'conn',accountID:'account',credentialRevision:revision,expiresAt:Date.now()+60_000,credential }
    if (method.endsWith('/logging/v1/admit')) return { admissionID:'admission', policy:{revision:1,advanced_enabled:true,request_body_enabled:false,response_body_enabled:false,binary_body_enabled:false}, context:{instanceID:'holder',providerID:'openai',operationID:'operation',rootOperationID:'root',bindingID:'binding',bindingRevision:1,identityCoverage:'complete',transportMode:'advanced_proxy',persistence:{numeric:true,content:true,reason:'normal'}} }
    if (method.endsWith('/refresh/acquire')) return { leaseID:'refresh',connectionID:'conn',accountID:'account',credentialRevision:revision,expiresAt:Date.now()+60_000,renewable:true }
    if (method.endsWith('/refresh/commit')) { revision++; return {id:'account',label:'A',enabled:true,order:0,revision:2,credentialRevision:revision,credential:params.credential} }
    if (method.endsWith('/account/attempt')) { attempt=params; return {...base,bindingRevision:2,credentialRevision:revision} }
    if (method.includes('/logging/v1/')) return {accepted:true,replayed:false,captureState:'complete'}
    throw new Error('unexpected callback')
  }} as any)
  const active = await ManagedProvider.activateActorOperation(identity, preflight, {providerID:'conn',modelID:'model'}, {chatID:'chat'})
  const scope=active.scope, ids:string[]=[], error=Object.assign(new Error('synthetic'),{statusCode:429})
  const endpoint=new URL('https://chatgpt.com/backend-api/codex/responses'), request={method:'POST',body:'{}'}
  const response=(status:number)=>{const value=new Response(null,{status});Object.defineProperty(value,'url',{value:endpoint.href});return value}
  await withSdkDispatches(scope,ids,()=>captureFetch((async()=>{
    await observeCodexInnerSend(endpoint,request,async()=>response(401))
    await ManagedProvider.refreshOAuthCredential({connectionID:'conn',accountID:'account',expectedRevision:1,credential,bindingID:'binding',captureScope:scope,exchange:async()=>credential})
    return observeCodexInnerSend(endpoint,request,async()=>response(429))
  }) as unknown as typeof fetch,scope)('https://example.invalid/v1/responses',request))
  recordSdkFailure(error,scope,ids)
  await ManagedProvider.recordSessionAttempt('session',error,'main',1)
  expect(attempt.refreshContinuity).toEqual({version:1,admissionID:'admission',previousCredentialRevision:1,credentialRevision:2})
  expect(attempt.dispatchObservation).toMatchObject({profile:'openai.codex.responses.v1',captureDispatchID:ids[0],httpStatus:429})
  expect(attempt.dispatchObservation.upstreamDispatchID).not.toBe(ids[0])
  await active.release()
})

test("Claude actor refresh preserves scope authority for a subsequent dispatch", async () => {
  const proof = { version: 1 as const, methodID: "anthropic.claude_subscription" as const, credentialOwner: "alice", policyRevision: 1 }
  const bootstrap = { stableChatID: "chat", owner: "bob", rootSessionID: "session", sessionID: "session", actorID: "main", rootOperationID: "root", expectedExecutionRevision: 0, mapRevision: 1, mappingRevision: 1, workspaceRevision: 1, modelRouteID: "route" }
  const preflight = { bootstrap, connectionID: "conn", providerID: "anthropic", billingLane: "subscription" as const, modelID: "model", modelRouteID: "route", methodPolicy: proof }
  const identity = { sessionID: "session", actorID: "main", executionRevision: 1, rootOperationID: "root" }
  let credential = { type: "oauth" as const, access: "old", refresh: "old-refresh", expires: Date.now() + 600_000 }
  let revision = 1
  const calls: string[] = []
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: any) {
      calls.push(method)
      if (method.endsWith("/account/bind")) return { ...preflight, ...identity, bindingID: "binding", bindingRevision: 1, scopeKind: "actor_execution", credentialRequired: true, accountID: "account", credentialRevision: revision, source: "round_robin", attempt: 1, committed: false }
      if (method.endsWith("/credential/lease")) return { leaseID: "lease", connectionID: "conn", accountID: "account", credentialRevision: revision, expiresAt: Date.now() + 60_000, credential, methodPolicy: proof }
      if (method.endsWith("/refresh/acquire")) return { leaseID: "refresh", connectionID: "conn", accountID: "account", credentialRevision: revision, expiresAt: Date.now() + 60_000, renewable: true, methodPolicy: proof }
      if (method.endsWith("/refresh/commit")) { credential = params.credential; revision++; return { id: "account", label: "A", enabled: true, order: 0, revision: 2, credentialRevision: revision, credential } }
      throw new Error("unavailable")
    },
  } as any)
  const active = await ManagedProvider.activateActorOperation(identity, preflight, { providerID: "conn", modelID: "model" }, { chatID: "chat" })
  const refreshed = await ManagedProvider.refreshOAuthCredential({ connectionID: "conn", accountID: "account", expectedRevision: 1, credential, bindingID: "binding", methodPolicy: proof, exchange: async () => ({ type: "oauth", access: "new", refresh: "new-refresh", expires: Date.now() + 600_000 }) })
  const nextScope = ManagedProvider.requireScope("session", "main", 1)
  expect(nextScope.credentialRevision).toBe(2)
  await ManagedProvider.admitClaudeDispatch(nextScope, identity, refreshed.credential)
  expect(calls.filter((item) => item.endsWith("/credential/lease"))).toHaveLength(2)
  await active.release()
})

test("managed throttle retains its account and respects the interactive wait budget", async () => {
  const { classifyFailure } = await import("../../src/provider/failure-classification")
  const bootstrap = { stableChatID: "chat", owner: "alice", rootSessionID: "session", sessionID: "session", actorID: "main", rootOperationID: "root", expectedExecutionRevision: 0, mapRevision: 1, mappingRevision: 1, workspaceRevision: 1, modelRouteID: "route" }
  const preflight = { bootstrap, connectionID: "conn", providerID: "openai", billingLane: "metered_api" as const, modelID: "model", modelRouteID: "route" }
  const identity = { sessionID: "session", actorID: "main", executionRevision: 1, rootOperationID: "root" }
  const binding = { ...identity, connectionID: "conn", providerID: "openai", billingLane: "metered_api", modelID: "model", modelRouteID: "route", bindingID: "binding", bindingRevision: 1, scopeKind: "actor_execution", credentialRequired: true, accountID: "account", credentialRevision: 1, source: "round_robin", attempt: 1, committed: false }
  const attempts: any[] = []
  ManagedProvider.installHostConnection({ async extMethod(method: string, params: any) {
    if (method.endsWith("/account/bind")) return binding
    if (method.endsWith("/account/attempt")) { attempts.push(params); return binding }
    if (method.endsWith("/credential/lease")) return { leaseID: "lease", connectionID: "conn", accountID: "account", credentialRevision: 1, expiresAt: Date.now() + 60_000, credential: { type: "api", key: "fake" } }
    throw new Error("unavailable")
  } } as any)
  const active = await ManagedProvider.activateActorOperation(identity, preflight, { providerID: "conn", modelID: "model" }, { chatID: "chat" })
  const error = { status: 429, responseHeaders: new Headers({ "Retry-After": "10" }) }
  const decision = await ManagedProvider.recordSessionAttempt("session", error, "main", 1)
  expect(decision).toMatchObject({ outcome: "transient", retry: true, rotated: false, retryAfterMs: 10_000 })
  expect(attempts[0].outcome).toBe("transient")
  expect(ManagedProvider.reserveRetryWait("session", "main", 1, 29_000)).toBe(true)
  expect(ManagedProvider.reserveRetryWait("session", "main", 1, 1_001)).toBe(false)
  expect(classifyFailure({ status: 429, message: "insufficient_quota" }).kind).toBe("throttle")
  expect(classifyFailure({ status: 429, message: "quota exceeded" }).kind).toBe("throttle")
  expect(classifyFailure({ status: 429, message: "SubscriptionUsageLimitError" }).kind).toBe("throttle")
  expect(classifyFailure({ message: "limit exhausted" }).kind).toBe("unknown")
  expect(classifyFailure({ status: 429, responseHeaders: { "retry-after": "999999999999999999999999" } }).retryAfterMs).toBeGreaterThan(30_000)
  expect(classifyFailure({ status: "429oops", responseHeaders: { "retry-after": "12seconds" } })).toEqual({ kind: "unknown" })
  await active.release()
})


test("refreshed Codex success retains only its original owned capture", async () => {
  const bootstrap = { stableChatID:"chat", owner:"alice", rootSessionID:"session", sessionID:"session", actorID:"main", rootOperationID:"root", expectedExecutionRevision:0, mapRevision:1, mappingRevision:1, workspaceRevision:1, modelRouteID:"route" }
  const preflight = { bootstrap, connectionID:"conn", providerID:"openai", billingLane:"subscription" as const, modelID:"model", modelRouteID:"route" }
  const identity = {sessionID:"session",actorID:"main",executionRevision:1,rootOperationID:"root"}
  const base = {...preflight,...identity,bindingID:"binding",bindingRevision:1,scopeKind:"actor_execution",credentialRequired:true,accountID:"account",source:"round_robin",attempt:1,committed:false}
  let revision=1, attempt:any, diagnostic:any
  const credential={type:"oauth" as const,access:"synthetic",refresh:"synthetic",expires:Date.now()+600000}
  ManagedProvider.installHostConnection({async extMethod(method:string,params:any){
    if(method.endsWith('/account/bind'))return {...base,credentialRevision:revision}
    if(method.endsWith('/credential/lease'))return {leaseID:'lease',connectionID:'conn',accountID:'account',credentialRevision:revision,expiresAt:Date.now()+60000,credential}
    if(method.endsWith('/logging/v1/admit'))return {admissionID:'admission',policy:{revision:1,advanced_enabled:true,request_body_enabled:false,response_body_enabled:false,binary_body_enabled:false},context:{instanceID:'holder',providerID:'openai',operationID:'operation',rootOperationID:'root',bindingID:'binding',bindingRevision:1,identityCoverage:'complete',transportMode:'advanced_proxy',persistence:{numeric:true,content:true,reason:'normal'}}}
    if(method.endsWith('/refresh/acquire'))return {leaseID:'refresh',connectionID:'conn',accountID:'account',credentialRevision:revision,expiresAt:Date.now()+60000,renewable:true}
    if(method.endsWith('/refresh/commit')){revision++;return {id:'account',label:'A',enabled:true,order:0,revision:2,credentialRevision:revision,credential:params.credential}}
    if(method.endsWith('/account/commit'))return {...base,bindingRevision:2,credentialRevision:revision,committed:true}
    if(method.endsWith('/account/attempt')){attempt=params;return {...base,bindingRevision:3,credentialRevision:revision,committed:true}}
    if(method.includes('/logging/v1/')){if(params.diagnosticOnly)diagnostic=params.diagnosticOnly;return {accepted:true,replayed:false,captureState:'complete'}}
    throw new Error('unexpected callback')
  }} as any)
  const active=await ManagedProvider.activateActorOperation(identity,preflight,{providerID:'conn',modelID:'model'},{chatID:'chat'})
  const scope=active.scope, ids:string[]=[], endpoint=new URL('https://chatgpt.com/backend-api/codex/responses'), request={method:'POST',body:'{}'}
  const response=(status:number)=>{const v=new Response(null,{status});Object.defineProperty(v,'url',{value:endpoint.href});return v}
  await withSdkDispatches(scope,ids,()=>captureFetch((async()=>{
    await observeCodexInnerSend(endpoint,request,async()=>response(401))
    await ManagedProvider.refreshOAuthCredential({connectionID:'conn',accountID:'account',expectedRevision:1,credential,bindingID:'binding',captureScope:scope,exchange:async()=>credential})
    return observeCodexInnerSend(endpoint,request,async()=>response(200))
  }) as unknown as typeof fetch,scope)('https://example.invalid/v1/responses',request))
  expect(markSdkSuccessStep(scope,[...ids,'extra'],{})).toEqual({})
  expect(ManagedProvider.originalRefreshObservationScope('session','other',1)).toBeUndefined()
  const metadata=markSdkSuccessStep(scope,ids,{})
  const sdk=createOpenAI({apiKey:'synthetic',baseURL:'https://unused.invalid/v1'}), model=sdk.responses('test')
  notePinnedResponsesSDK(sdk as object);notePinnedResponsesModel(sdk as object,model as object)
  const malformed=new JSONParseError({text:'{',cause:new Error('synthetic')}), signal=new AbortController().signal
  markMalformedResponse(malformed,scope,ids,signal,model as object)
  expect(hasMalformedResponse(malformed,ManagedProvider.currentScope('session','main',1))).toBe(false)
  expect(hasMalformedResponse(malformed,ManagedProvider.originalRefreshObservationScope('session','main',1))).toBe(true)
  await ManagedProvider.commitSessionOperation('session','main',1)
  emitMalformedDiagnostic(malformed,scope,ManagedProvider.committedRefreshDiagnostic('session','main',1))
  await new Promise(resolve=>setTimeout(resolve,20))
  expect(diagnostic).toMatchObject({version:2,previousCredentialRevision:1,credentialRevision:2,httpStatus:200})
  const invalidated = new AbortController(), second = new JSONParseError({text:'{',cause:new Error('synthetic')})
  markMalformedResponse(second,scope,ids,invalidated.signal,model as object)
  invalidated.abort()
  expect(hasMalformedResponse(second,scope)).toBe(false)
  expect(hasObservedMalformedResponse(second,ManagedProvider.originalRefreshObservationScope('session','main',1))).toBe(true)
  const selected=takeSdkSuccessStep(ManagedProvider.successObservationScope('session','main',1),metadata)
  expect(selected.observation?.httpStatus).toBe(200)
  await ManagedProvider.recordSessionCompletion('session','success','main',1,selected.observation)
  expect(attempt.successContinuity).toEqual({version:2,admissionID:'admission',previousCredentialRevision:1,credentialRevision:2})
  expect(attempt.dispatchObservation).toEqual(selected.observation)
  await active.release()
})

