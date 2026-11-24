import { afterEach, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { managedSearch } from "../../src/acp/managed-search"
import { ManagedProvider } from "../../src/acp/managed-provider"

const route = { rootOperationID: "root-search", connectionID: "local", providerID: "local", billingLane: "local" as const, modelID: "mimo-search", modelRouteID: "route-search" }
const binding = { bindingID: "binding", bindingRevision: 1, rootOperationID: route.rootOperationID, connectionID: "local", providerID: "local", billingLane: "local", modelID: route.modelID, credentialRequired: false, source: "keyless", attempt: 1, committed: false }

function response(scope: "deployment-service" | "model-account" | "subscription-unbound" = "deployment-service") {
  return { schema_version: 1, query: "query", caller: "chat", mode: "results", selected_provider: "brave", actual_provider: "brave", account_scope: scope, account_id: "", status: "complete", sources: [{ url: "https://example.test", title: "title", snippet: "snippet" }], total: 1, cache: { hit: false }, elapsed_ms: 1, warnings: [], search_queries: [], support_links: [], web_search_usage: {} }
}

function artifact(value: unknown) {
  const bytes = new TextEncoder().encode(JSON.stringify(value))
  const hash = createHash("sha256").update(bytes).digest("hex")
  return { artifactID: `artifact-${hash.slice(0, 8)}`, contentSHA256: hash, sizeBytes: bytes.length, mediaType: "application/json", state: "acknowledged" as const, bytes }
}

function hostFor(sequence: unknown[], onInvoke?: () => void) {
  let current: ReturnType<typeof artifact> | undefined
  const calls: string[] = []
  return {
    calls,
    async extMethod(method: string, params: any) {
      calls.push(method)
      if (method.endsWith("/account/bind")) return binding
      if (method.endsWith("/session/v1/binding/read")) return { engineSessionID: params.sessionID, stableChatID: "chat", owner: "owner", canonicalCwd: "/tmp", workspaceRevision: 1, authorityWorkspaceID: "workspace", memoryWorkspaceID: "memory", copalWorkspace: "copal", memoryEnabled: true, engineAliases: [], mapRevision: 1, mappingRevision: 1 }
      if (method.endsWith("/credential/lease")) throw new Error("keyless must not lease")
      if (method.endsWith("/logging/admit")) return { accepted: true }
      if (method.endsWith("/executor/invoke")) { onInvoke?.(); current = artifact(sequence.shift()); return current }
      if (method.endsWith("/artifact/read")) {
        if (!current || params.artifactID !== current.artifactID) throw new Error("artifact mismatch")
        return { artifactID: current.artifactID, contentSHA256: current.contentSHA256, sizeBytes: current.sizeBytes, mediaType: current.mediaType, offset: 0, dataBase64: Buffer.from(current.bytes).toString("base64"), chunkSHA256: current.contentSHA256, eof: true }
      }
      throw new Error(`unexpected callback ${method}`)
    },
  }
}

afterEach(() => { ManagedProvider.resetForTest(); delete process.env.OPEN_CLANK_MANAGED })

test("managed search external result uses callback artifact and real validator", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const host = hostFor([{ kind: "external_result", response: response() }])
  ManagedProvider.installHostConnection(host as any)
  const result = await ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-1", abort: new AbortController().signal }, { query: "query", count: 1 }))
  expect(result.account_scope).toBe("deployment-service")
  expect(host.calls.filter((method) => method.endsWith("/executor/invoke")).length).toBe(1)
})

test("managed search returns an early finalized unavailable response without Router execution", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const host = hostFor([{ kind: "finalized", response: response("subscription-unbound") }])
  ManagedProvider.installHostConnection(host as any)
  let executes = 0
  const dispose = ManagedProvider.installManagedOperationDelegate({ execute: async () => { executes++; throw new Error("must not execute") }, cancel: async () => ({ operationID: "pending", rootOperationID: "child-root", state: "pending" }) })
  const result = await ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-early", abort: new AbortController().signal }, { query: "query" }))
  dispose()
  expect(result.account_scope).toBe("subscription-unbound")
  expect(executes).toBe(0)
})

test("managed search executes and finalizes one same-router request", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const prepared = { kind: "managed_request", handoffID: "handoff", request: { rootOperationID: "child-root", idempotencyKey: "child-idempotency", operation: "web.search", routes: [{ connectionID: "local", providerID: "local", billingLane: "local", modelRouteID: "route-search", modelID: "mimo-search" }], input: { query: "query", count: 1, answer: false }, artifactInputs: [], options: { deadlineMs: 30000 } } }
  const terminal = { operationID: "operation", rootOperationID: "child-root", operation: "web.search", modelRouteID: "route-search", connectionID: "local", billingLane: "local", state: "complete", committed: true, output: {}, artifacts: [], replayed: false }
  const host = hostFor([prepared, { kind: "finalized", response: response("model-account") }])
  ManagedProvider.installHostConnection(host as any)
  let executes = 0
  const dispose = ManagedProvider.installManagedOperationDelegate({ execute: async () => { executes++; return terminal as any }, cancel: async () => ({ operationID: "operation", rootOperationID: "child-root", state: "pending" }) })
  const result = await ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-2", abort: new AbortController().signal }, { query: "query", count: 1 }))
  dispose()
  expect(executes).toBe(1)
  expect(result.account_scope).toBe("model-account")
  expect(host.calls.filter((method) => method.endsWith("/executor/invoke")).length).toBe(2)
})

test("managed search abort before execution cancels handoff without Router execute", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const prepared = { kind: "managed_request", handoffID: "handoff", request: { rootOperationID: "child-root", idempotencyKey: "child-idempotency", operation: "web.search", routes: [{ connectionID: "local", providerID: "local", billingLane: "local", modelRouteID: "route-search", modelID: "mimo-search" }], input: { query: "query", count: 1 }, artifactInputs: [], options: { deadlineMs: 30000 } } }
  const host = hostFor([prepared, { kind: "cancelled", reason: "aborted" }])
  ManagedProvider.installHostConnection(host as any)
  const controller = new AbortController(); controller.abort()
  let executes = 0
  const dispose = ManagedProvider.installManagedOperationDelegate({ execute: async () => { executes++; throw new Error("must not execute") }, cancel: async () => ({ operationID: "pending", rootOperationID: "child-root", state: "pending" }) })
  await expect(ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-3", abort: controller.signal }, { query: "query", count: 1 }))).rejects.toThrow("aborted")
  dispose()
  expect(executes).toBe(0)
  expect(host.calls.filter((method) => method.endsWith("/executor/invoke")).length).toBe(0)
})

test("managed search abort during callback dispatch cancels handoff without Router execute", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const controller = new AbortController()
  const prepared = { kind: "managed_request", handoffID: "handoff", request: { rootOperationID: "child-root", idempotencyKey: "child-idempotency", operation: "web.search", routes: [{ connectionID: "local", providerID: "local", billingLane: "local", modelRouteID: "route-search", modelID: "mimo-search" }], input: { query: "query", count: 1 }, artifactInputs: [], options: { deadlineMs: 30000 } } }
  const host = hostFor([prepared, { kind: "cancelled", reason: "aborted" }], () => controller.abort())
  ManagedProvider.installHostConnection(host as any)
  let executes = 0
  const dispose = ManagedProvider.installManagedOperationDelegate({ execute: async () => { executes++; throw new Error("must not execute") }, cancel: async () => ({ operationID: "pending", rootOperationID: "child-root", state: "pending" }) })
  await expect(ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-dispatch-abort", abort: controller.signal }, { query: "query", count: 1 }))).rejects.toThrow("aborted")
  dispose()
  expect(executes).toBe(0)
  expect(host.calls.filter((method) => method.endsWith("/executor/invoke")).length).toBe(2)
})

test("managed search abort during execution still finalizes the Router completion winner", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const prepared = { kind: "managed_request", handoffID: "handoff", request: { rootOperationID: "child-root", idempotencyKey: "child-idempotency", operation: "web.search", routes: [{ connectionID: "local", providerID: "local", billingLane: "local", modelRouteID: "route-search", modelID: "mimo-search" }], input: { query: "query", count: 1 }, artifactInputs: [], options: { deadlineMs: 30000 } } }
  const terminal = { operationID: "operation", rootOperationID: "child-root", operation: "web.search", modelRouteID: "route-search", connectionID: "local", billingLane: "local", state: "complete", committed: true, output: {}, artifacts: [], replayed: false }
  const host = hostFor([prepared, { kind: "finalized", response: response("model-account") }])
  ManagedProvider.installHostConnection(host as any)
  const controller = new AbortController(); let cancels = 0
  const dispose = ManagedProvider.installManagedOperationDelegate({ execute: async () => { controller.abort(); await new Promise((resolve) => setTimeout(resolve, 1)); return terminal as any }, cancel: async () => { cancels++; return { operationID: "operation", rootOperationID: "child-root", state: "pending" } } })
  const result = await ManagedProvider.withOperation("session-search", route, { providerID: "local", modelID: route.modelID }, () => managedSearch({ sessionID: "session-search", callID: "call-4", abort: controller.signal }, { query: "query", count: 1 }))
  dispose()
  expect(result.account_scope).toBe("model-account")
  expect(cancels).toBe(1)
  expect(host.calls.filter((method) => method.endsWith("/executor/invoke")).length).toBe(2)
})

