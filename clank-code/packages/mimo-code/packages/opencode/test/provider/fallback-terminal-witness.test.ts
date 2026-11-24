import { expect, test } from "bun:test"
import { createAnthropic } from "@ai-sdk/anthropic"
import { streamText, wrapLanguageModel } from "ai"
import { observeAnthropicFailure, withAnthropicFailureProbe, observeAnthropicDoStream, closeAnthropicProbe, issueAnthropicCallback, type AnthropicFailureProbe, type FetchFunction } from "../../src/provider/fallback-terminal-witness"
import { notePinnedAnthropicSDK, notePinnedAnthropicModel } from "../../src/provider/anthropic-origin"

test("unregistered transport probe remains preliminary and bounded", async () => {
  let calls = 0
  const fake: FetchFunction = async () => {
    calls++
    const r = new Response(JSON.stringify({ type: "error", error: { type: "authentication_error", message: "invalid key" } }), { status: 401 })
    Object.defineProperty(r, "url", { value: "https://api.anthropic.com/v1/messages" })
    return r
  }
  const wrapped = Object.assign((...args: Parameters<typeof fetch>) => observeAnthropicFailure(fake, ...args), { preconnect: fetch.preconnect })
  const sdk = createAnthropic({ apiKey: "fixture", fetch: wrapped })
  const probe: AnthropicFailureProbe = { sends: 0, state: "pending" }
  const result = withAnthropicFailureProbe(probe, () => streamText({ model: sdk("fixture"), prompt: "fixture", maxRetries: 0, onError: () => {} }))
  await withAnthropicFailureProbe(probe, async () => { for await (const _ of result.fullStream) {} })
  expect(calls).toBe(1)
  expect(probe).toEqual({ sends: 1, state: "observed", originalEOF: true })
  const off = await wrapped("https://api.anthropic.com/v1/messages", { method: "POST" })
  expect(off.status).toBe(401)
  expect(await off.text()).toContain("authentication_error")
  const rejected: AnthropicFailureProbe = { sends: 0, state: "pending" }
  await withAnthropicFailureProbe(rejected, () => wrapped("https://example.com/v1/messages", { method: "POST" }))
  expect(rejected.state).toBe("unavailable")
  for (const body of ["{", "x".repeat(16_385)]) {
    const invalid: AnthropicFailureProbe = { sends: 0, state: "pending" }
    const bad: FetchFunction = async () => {
      const r = new Response(body, { status: 401 })
      Object.defineProperty(r, "url", { value: "https://api.anthropic.com/v1/messages" })
      return r
    }
    await withAnthropicFailureProbe(invalid, () => observeAnthropicFailure(bad, "https://api.anthropic.com/v1/messages", { method: "POST" }))
    expect(invalid.state).toBe("unavailable")
  }
  const joined: AnthropicFailureProbe = { sends: 0, state: "pending" }
  let adapterError: unknown
  const language = sdk("fixture")
  notePinnedAnthropicSDK(sdk); notePinnedAnthropicModel(sdk, language)
  const model = wrapLanguageModel({ model: language, middleware: [{ specificationVersion: "v3", async wrapStream({ doStream }) {
    return observeAnthropicDoStream(language, doStream)
  } }] })
  const stream = withAnthropicFailureProbe(joined, () => streamText({ model, prompt: "fixture", maxRetries: 0, onError: () => {} }))
  await withAnthropicFailureProbe(joined, async () => { for await (const part of stream.fullStream) if (part.type === "error") adapterError = part.error })
  expect(adapterError).toBe(joined.adapterError)
  let finish!: () => void
  const callback = new Promise<void>((resolve) => { finish = resolve })
  void issueAnthropicCallback(joined, () => callback)
  const closed = closeAnthropicProbe(joined, adapterError, true)
  finish()
  expect(await closed).toBe(true)
  expect(await closeAnthropicProbe(joined, adapterError, true)).toBe(false)
  const late: AnthropicFailureProbe = { sends: 1, state: "observed", originalEOF: true, adapterError: {} }
  let release!: () => void
  const pending = new Promise<void>((resolve) => { release = resolve })
  void issueAnthropicCallback(late, () => pending)
  const closing = closeAnthropicProbe(late, late.adapterError, true)
  let issued = false
  await issueAnthropicCallback(late, async () => { issued = true }).catch(() => {})
  expect(issued).toBe(false)
  release()
  expect(await closing).toBe(false)
  const changing: AnthropicFailureProbe = { sends: 0, state: "pending" }
  let deliver!: () => void
  const delayed: FetchFunction = async () => {
    const r = new Response(new ReadableStream({ start(controller) { deliver = () => { controller.enqueue(new TextEncoder().encode('{"type":"error","error":{"type":"authentication_error","message":"x"}}')); controller.close() } } }), { status: 401 })
    Object.defineProperty(r, "url", { value: "https://api.anthropic.com/v1/messages" })
    return r
  }
  const observed = withAnthropicFailureProbe(changing, () => observeAnthropicFailure(delayed, "https://api.anthropic.com/v1/messages", { method: "POST" }))
  await Promise.resolve(); changing.state = "unavailable"; deliver()
  await observed
  expect(changing.state).toBe("unavailable")
  const abortProbe: AnthropicFailureProbe = { sends: 0, state: "pending" }
  const aborted = new AbortController(); aborted.abort()
  await withAnthropicFailureProbe(abortProbe, () => wrapped(new Request("https://api.anthropic.com/v1/messages", { method: "POST", signal: aborted.signal })))
  expect(abortProbe.state).toBe("unavailable")
})
