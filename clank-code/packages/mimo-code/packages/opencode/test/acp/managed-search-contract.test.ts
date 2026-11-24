import { expect, test } from "bun:test"
import {
  SEARCH_EXECUTOR_ID,
  validateSearchArtifactResult,
  validateSearchCallbackRequest,
} from "../../src/acp/managed-search-contract"

const claims = {
  sessionID: "session-1",
  correlationID: "tool-1",
  rootOperationID: "root-1",
    idempotencyKey: "idempotency-key-1",
} as const

test("search callback accepts the three bounded actions and preserves claims", () => {
  const dispatch = validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "open clank", count: 5, intent: "web", deadlineAt: 1000 },
  })
  expect(dispatch.action).toBe("dispatch")
  expect(dispatch.claims).toEqual(claims)

  const cancel = validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "cancel",
    claims,
    handoffID: "handoff-1",
    reason: "aborted",
  })
  expect(cancel.action).toBe("cancel")
})

test("search claims are not authority and unknown/provider fields are rejected", () => {
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims: { ...claims, ownerID: "attacker" },
    input: { query: "x", count: 1, deadlineAt: 1000 },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 1, deadlineAt: 1000, accountID: "secret" },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: "other.executor",
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 1, deadlineAt: 1000 },
  })).toThrow()
})

test("search dispatch bounds query/count/deadline and retains supported options", () => {
  const request = validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 10, mode: "answer", intent: "web", freshness: "week", locale: "en-US", extraction: "text", deadlineAt: 1234 },
  })
  expect(request.action === "dispatch" ? request.input : undefined).toMatchObject({ mode: "answer", freshness: "week", locale: "en-US" })
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 51, deadlineAt: 1000 },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 1, deadlineAt: 0 },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims,
    input: { query: "x", count: 1, intent: "general", deadlineAt: 1000 },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "cancel",
    claims,
    handoffID: "handoff-1",
    input: { query: "x" },
  })).toThrow()
  expect(() => validateSearchCallbackRequest({
    executorID: SEARCH_EXECUTOR_ID,
    contractVersion: 1,
    action: "dispatch",
    claims: { ...claims, idempotencyKey: "short" },
    input: { query: "x", count: 1, deadlineAt: 1000 },
  })).toThrow()
})

test("artifact validation keeps URL and provider-metadata authority boundaries", () => {
  const response = {
    schema_version: 1, query: "x", caller: "chat", mode: "results", selected_provider: "brave", actual_provider: "brave",
    account_scope: "deployment-service", account_id: "", status: "complete", sources: [{ url: "https://example.test/a", title: "A", snippet: "s" }],
    total: 1, cache: { hit: false }, elapsed_ms: 1, warnings: [], search_queries: [], support_links: [], web_search_usage: {},
  }
  expect(validateSearchArtifactResult({ kind: "external_result", response }).kind).toBe("external_result")
  expect(validateSearchArtifactResult({ kind: "external_result", response: { ...response, sources: [{ url: "https://example.test/a", title: "", snippet: "", provider_metadata: { nested: { rank: 1 }, labels: ["x"] } }] } }).kind).toBe("external_result")
  expect(() => validateSearchArtifactResult({ kind: "external_result", response: { ...response, sources: [{ url: "ftp://example.test", title: "A", snippet: "s" }] } })).toThrow()
  expect(() => validateSearchArtifactResult({ kind: "external_result", response: { ...response, sources: [{ url: "https://example.test", title: "A", snippet: "s", provider_metadata: { accountID: "leak" } }] } })).toThrow()
  expect(() => validateSearchArtifactResult({ kind: "external_result", response: { ...response, total: "1" } })).toThrow()
  expect(() => validateSearchArtifactResult({ kind: "external_result", response: { ...response, unexpected: true } })).toThrow()
  expect(() => validateSearchArtifactResult({ kind: "finalized", response, terminal: {} })).toThrow()
})

test("artifact decoder accepts the actual managed router prepared search shape", () => {
  const request = {
    rootOperationID: "root-1", idempotencyKey: "idempotency-key-1", operation: "web.search",
    routes: [{ connectionID: "conn", providerID: "mimo", billingLane: "subscription", modelRouteID: "route", modelID: "mimo-search" }],
    input: { query: "open clank", count: 10, freshness: "week", answer: true },
    artifactInputs: [], options: { deadlineMs: 30000 },
  }
  expect(validateSearchArtifactResult({ kind: "managed_request", handoffID: "handoff-1", request }).kind).toBe("managed_request")
  expect(() => validateSearchArtifactResult({ kind: "managed_request", handoffID: "handoff-1", request: { ...request, input: { query: "x", deadlineAt: 1 } } })).toThrow()
})
