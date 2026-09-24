import { afterEach, beforeEach, expect, test } from "bun:test"
import { ManagedProvider } from "../../src/acp/managed-provider"
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
  ])
  expect(JSON.stringify(calls)).not.toContain("leased-secret")
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
    "_openclank/provider-store/v1/account/attempt",
    "_openclank/provider-store/v1/credential/lease",
    "_openclank/provider-store/v1/account/commit",
  ])
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
  expect(calls).toEqual(["_openclank/provider-store/v1/account/bind"])
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
