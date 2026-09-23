import { afterEach, beforeEach, expect, test } from "bun:test"
import { ManagedProvider } from "../../src/acp/managed-provider"

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
