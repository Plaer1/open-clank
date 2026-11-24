// B5-A: provider x failure-scenario matrix through the REAL SDK model objects and a stub fetch.
// No network: the injected stub is the only transport and records every request.
// SDK construction mirrors src/provider/provider.ts:1102 (@ai-sdk/openai-compatible), :1103
// (@openrouter/ai-sdk-provider) and :1116 (@ai-sdk/github-copilot -> ./sdk/copilot createOpenaiCompatible),
// with maxRetries 0 as the managed path does (src/session/llm.ts:974). The product's per-provider fetch wrappers
// (Copilot/Grok custom fetch) are NOT applied: only the SDK error shape is under test.
// Claude subscription (@ai-sdk/anthropic, lane subscription) is deliberately excluded: Beta, Off.
import { afterEach, beforeEach, describe, expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { createOpenRouter } from "@openrouter/ai-sdk-provider"
import { createOpenaiCompatible as createCopilot } from "../../src/provider/sdk/copilot"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { classifyFailure, type FailureKind } from "../../src/provider/failure-classification"
import { isRetryableTransientError } from "../../src/session/retry"

type Stub = (url: string) => Response
const KEY = "dummy-not-a-real-key"
const rows: Array<[string, (fetch: typeof globalThis.fetch) => any]> = [
  ["grok", (fetch) => createOpenAICompatible({ name: "grok", baseURL: "https://cli-chat-proxy.grok.com/v1", apiKey: KEY, fetch }).chatModel("m")],
  ["kimi-code", (fetch) => createOpenAICompatible({ name: "kimi", baseURL: "https://api.kimi.com/coding/v1", apiKey: KEY, fetch }).chatModel("m")],
  ["kilo", (fetch) => createOpenAICompatible({ name: "kilo", baseURL: "https://api.kilo.ai/api/gateway", apiKey: KEY, fetch }).chatModel("m")],
  ["llm-gateway", (fetch) => createOpenAICompatible({ name: "llmgateway", baseURL: "https://api.llmgateway.io/v1", apiKey: KEY, fetch }).chatModel("m")],
  ["zai", (fetch) => createOpenAICompatible({ name: "zai", baseURL: "https://api.z.ai/api/coding/paas/v4", apiKey: KEY, fetch }).chatModel("m")],
  ["mimo", (fetch) => createOpenAICompatible({ name: "xiaomi", baseURL: "https://api.xiaomimimo.com/v1", apiKey: KEY, fetch }).chatModel("m")],
  ["openrouter", (fetch) => createOpenRouter({ apiKey: KEY, fetch }).chat("m")],
  ["copilot", (fetch) => createCopilot({ baseURL: "https://api.githubcopilot.com", apiKey: KEY, fetch }).chat("m")],
]
// Claude subscription: excluded on purpose (Beta, Off). No cell is run for it.
test.skip("claude-subscription is intentionally excluded (Beta, Off)", () => {})

beforeEach(() => { process.env.OPEN_CLANK_MANAGED = "1"; ManagedProvider.resetForTest() })
afterEach(() => { ManagedProvider.resetForTest(); delete process.env.OPEN_CLANK_MANAGED })

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } })
const errBody = (message: string, code?: string) => ({ error: { message, type: "x", code } })
const sse = (frames: string[], tail?: Error) => {
  const enc = new TextEncoder()
  let i = 0 // pull-based: c.error() in start() would discard the queued frames
  return new Response(new ReadableStream({
    pull(c) { if (i < frames.length) c.enqueue(enc.encode(frames[i++])); else if (tail) c.error(tail); else c.close() },
  }), { headers: { "content-type": "text/event-stream" } })
}
const chunk = `data: ${JSON.stringify({ id: "c1", object: "chat.completion.chunk", created: 1, model: "m", choices: [{ index: 0, delta: { role: "assistant", content: "hi" }, finish_reason: null }] })}\n\n`

function stub(respond: Stub) {
  const urls: string[] = []
  const fetch = (async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input)
    urls.push(url)
    return respond(url)
  }) as typeof globalThis.fetch
  return { fetch, urls }
}

/** Real managed attempt decision (managed-provider.ts recordSessionAttempt) against a fake Host. */
async function decide(error: unknown, committed: boolean) {
  ManagedProvider.resetForTest() // decide() may run several times per test
  const identity = { sessionID: "s", actorID: "main", executionRevision: 1, rootOperationID: "root" }
  const bootstrap = { stableChatID: "chat", owner: "alice", rootSessionID: "s", sessionID: "s", actorID: "main", rootOperationID: "root", expectedExecutionRevision: 0, mapRevision: 1, mappingRevision: 1, workspaceRevision: 1, modelRouteID: "route" }
  const preflight = { bootstrap, connectionID: "conn", providerID: "openai", billingLane: "metered_api" as const, modelID: "model", modelRouteID: "route" }
  let isCommitted = false
  const binding = () => ({ ...identity, connectionID: "conn", providerID: "openai", billingLane: "metered_api", modelID: "model", modelRouteID: "route", bindingID: "b", bindingRevision: isCommitted ? 2 : 1, scopeKind: "actor_execution", credentialRequired: true, accountID: "a", credentialRevision: 1, source: "round_robin", attempt: 1, committed: isCommitted })
  ManagedProvider.installHostConnection({ async extMethod(method: string) {
    if (method.endsWith("/account/commit")) { isCommitted = true; return binding() }
    if (method.endsWith("/account/bind") || method.endsWith("/account/attempt")) return binding()
    if (method.endsWith("/credential/lease")) return { leaseID: "l", connectionID: "conn", accountID: "a", credentialRevision: 1, expiresAt: Date.now() + 60_000, credential: { type: "api", key: "fake" } }
    throw new Error("unavailable")
  } } as any)
  const active = await ManagedProvider.activateActorOperation(identity, preflight, { providerID: "conn", modelID: "model" }, { chatID: "chat" })
  try {
    if (committed) await ManagedProvider.commitSessionOperation("s", "main", 1)
    return await ManagedProvider.recordSessionAttempt("s", error, "main", 1)
  } finally { await active.release() }
}

async function callError(model: any, s: ReturnType<typeof stub>) {
  try { await generateText({ model, prompt: "hi", maxRetries: 0 }) } catch (e) { return e }
  return undefined // success: no error
}

const RA = { "retry-after": "2" }
type Cell = { name: string; respond: Stub; kind: FailureKind; retryAfterMs?: number; retry: boolean; committed?: boolean; transientRetryable: boolean }
const cells: Cell[] = [
  { name: "429 + Retry-After:2", respond: () => json(429, errBody("Rate limit reached"), RA), kind: "throttle", retryAfterMs: 2000, retry: true, transientRetryable: true },
  { name: "429 no Retry-After", respond: () => json(429, errBody("Rate limit reached")), kind: "throttle", retry: true, transientRetryable: true },
  { name: "500 uncommitted", respond: () => json(500, errBody("server error")), kind: "transient", retry: true, transientRetryable: true },
  { name: "503 uncommitted", respond: () => json(503, errBody("unavailable")), kind: "transient", retry: true, transientRetryable: true },
  { name: "500 committed", respond: () => json(500, errBody("server error")), kind: "transient", retry: false, committed: true, transientRetryable: true },
  { name: "503 committed", respond: () => json(503, errBody("unavailable")), kind: "transient", retry: false, committed: true, transientRetryable: true },
  { name: "401 auth", respond: () => json(401, errBody("bad key")), kind: "auth", retry: false, transientRetryable: false },
]

describe("shared retry classification across SDK families", () => {
  for (const [provider, make] of rows) {
    describe(provider, () => {
      for (const c of cells) {
        test(c.name, async () => {
          const s = stub(c.respond)
          const error = await callError(make(s.fetch), s)
          expect(error).toBeDefined()
          const f = classifyFailure(error)
          expect(f.kind).toBe(c.kind)
          expect(f.retryAfterMs).toBe(c.retryAfterMs)
          expect(isRetryableTransientError(error)).toBe(c.transientRetryable)
          const d = await decide(error, c.committed === true)
          expect(d.retry).toBe(c.retry)
          expect(d.rotated).toBe(false)
          if (c.retryAfterMs !== undefined) expect(d.retryAfterMs).toBe(c.retryAfterMs)
          expect(s.urls.length).toBe(1)
          expect(s.urls.every((u) => u.startsWith("https://"))).toBe(true) // recorded by stub only; nothing else sent
        })
      }

      test("200 malformed body: not success, not retried (unknown)", async () => {
        for (const body of ["", "{}", "not json"]) {
          const s = stub(() => new Response(body, { status: 200, headers: { "content-type": "application/json" } }))
          const error = await callError(make(s.fetch), s)
          expect(error).toBeDefined() // not silently treated as success
          expect(classifyFailure(error).kind).toBe("unknown")
          expect(isRetryableTransientError(error)).toBe(false)
          expect((await decide(error, false)).retry).toBe(false)
          expect(s.urls.length).toBe(1)
        }
      })

      test("stream fails after first content: no retry once committed", async () => {
        const mid = Object.assign(new Error("network error"), { code: "ECONNRESET" })
        const run = async () => {
          const s = stub(() => sse([chunk], mid))
          const { stream } = await make(s.fetch).doStream({ prompt: [{ role: "user", content: [{ type: "text", text: "hi" }] }], includeRawChunks: false })
          let sawText = false, error: unknown
          const r = stream.getReader()
          try {
            for (;;) {
              const { done, value } = await r.read()
              if (done) break
              if (value.type === "text-delta") sawText = true
              if (value.type === "error") error = value.error
            }
          } catch (e) { error = e }
          return { sawText, error, n: s.urls.length }
        }
        const { sawText, error, n } = await run()
        expect(sawText).toBe(true)
        expect(n).toBe(1)
        expect(classifyFailure(error).kind).toBe("transient")
        expect((await decide(error, true)).retry).toBe(false) // committed guard
        expect((await decide(error, false)).retry).toBe(true) // control: same error, nothing committed
      })
    })
  }
})
