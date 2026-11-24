import { AsyncLocalStorage } from "node:async_hooks"
import { APICallError } from "@ai-sdk/provider"
import { isPinnedAnthropicModel } from "./anthropic-origin"

// Preliminary transport evidence only. No production admission installs a
// context and this object is not a terminal receipt or an authority grant.
export type AnthropicFailureProbe = { sends: number; state: "pending" | "observed" | "unavailable"; closed?: boolean; originalEOF?: boolean; adapterError?: object; callbacks?: Set<Promise<unknown>>; signal?: AbortSignal; onAbort?: () => void; localOutcome?: {readonly error:object} }
const active = new AsyncLocalStorage<AnthropicFailureProbe>()
const MAX_ERROR_BYTES = 16_384

export function withAnthropicFailureProbe<T>(probe: AnthropicFailureProbe, run: () => T): T {
  return active.run(probe, run)
}

export type FetchFunction = (...args: Parameters<typeof fetch>) => ReturnType<typeof fetch>

export async function observeAnthropicFailure(fetcher: FetchFunction, input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const probe = active.getStore()
  if (!probe) return fetcher(input, init)
  if (probe.closed) { probe.state = "unavailable"; throw new Error("attempt closed") }
  probe.sends++
  if (probe.sends !== 1) probe.state = "unavailable"
  const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
  if (signal && !probe.signal) {
    probe.signal = signal
    probe.onAbort = () => { probe.state = "unavailable" }
    signal.addEventListener("abort", probe.onAbort, { once: true })
    if (signal.aborted) probe.state = "unavailable"
  } else if (signal && probe.signal !== signal) probe.state = "unavailable"
  let response: Response
  try { response = await fetcher(input, init) }
  catch (error) { probe.state = "unavailable"; throw error }
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined
  try {
    const requestURL = new URL(input instanceof Request ? input.url : String(input))
    const responseURL = new URL(response.url)
    const exact = (url: URL) => url.protocol === "https:" && url.hostname === "api.anthropic.com" && !url.port && !url.username && !url.password && url.pathname === "/v1/messages" && !url.search && !url.hash
    if (probe.state !== "pending" || (init?.method ?? (input instanceof Request ? input.method : "GET")) !== "POST" || !exact(requestURL) || !exact(responseURL) || response.redirected || response.status !== 401 || !response.body || signal?.aborted) {
      probe.state = "unavailable"
      return response
    }
    reader = response.clone().body!.getReader()
    const chunks: Uint8Array[] = []
    let size = 0
    const deadline = performance.now() + 250
    for (;;) {
      const remaining = deadline - performance.now()
      if (remaining <= 0) { probe.state = "unavailable"; void reader.cancel().catch(() => {}); return response }
      let timer: ReturnType<typeof setTimeout> | undefined
      const result = await Promise.race([reader.read(), new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Error("probe timeout")), remaining) })]).finally(() => { if (timer) clearTimeout(timer) })
      if (result.done) break
      size += result.value.byteLength
      if (size > MAX_ERROR_BYTES) {
        probe.state = "unavailable"
        void reader.cancel().catch(() => {})
        return response
      }
      chunks.push(result.value)
    }
    const body = new Uint8Array(size)
    let offset = 0
    for (const chunk of chunks) { body.set(chunk, offset); offset += chunk.byteLength }
    const value: unknown = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(body))
    if (!value || typeof value !== "object" || (value as any).type !== "error" || !(value as any).error || (value as any).error.type !== "authentication_error" || typeof (value as any).error.message !== "string" || !(value as any).error.message || signal?.aborted || probe.sends !== 1 || probe.closed || probe.state !== "pending") probe.state = "unavailable"
    else probe.state = "observed"
  } catch {
    probe.state = "unavailable"
    if (reader) void reader.cancel().catch(() => {})
  } finally {
    try { reader?.releaseLock() } catch { probe.state = "unavailable" }
  }
  if (probe.state !== "observed" || !response.body) return response
  const original = response.body.getReader()
  const body = new ReadableStream<Uint8Array>({
    async pull(controller) {
      try {
        if (signal?.aborted || probe.closed) probe.state = "unavailable"
        const result = await original.read()
        if (signal?.aborted || probe.closed) probe.state = "unavailable"
        if (result.done) { probe.originalEOF = true; original.releaseLock(); controller.close() }
        else controller.enqueue(result.value)
      } catch (error) { probe.state = "unavailable"; try { original.releaseLock() } catch {} controller.error(error) }
    },
    async cancel(reason) { probe.state = "unavailable"; try { await original.cancel(reason) } finally { original.releaseLock() } },
  })
  const wrapped = new Response(body, { status: response.status, statusText: response.statusText, headers: response.headers })
  Object.defineProperty(wrapped, "url", { value: response.url })
  Object.defineProperty(wrapped, "redirected", { value: response.redirected })
  return wrapped
}

// Unregistered adapter/lifetime seam: callers must still provide a pinned
// adapter and a host-owned admission. Nothing in this module grants either.
export async function observeAnthropicDoStream<T>(model: object, run: () => PromiseLike<T>): Promise<T> {
  const probe = active.getStore()
  if (!probe) return run()
  if (!isPinnedAnthropicModel(model)) { probe.state = "unavailable"; return run() }
  try {
    const value = await run()
    probe.state = "unavailable" // a successful stream is never eligible
    return value
  } catch (error) {
    if (probe.state === "observed" && probe.originalEOF && !probe.closed && !probe.signal?.aborted && APICallError.isInstance(error) && error.statusCode === 401) probe.adapterError = error
    else probe.state = "unavailable"
    throw error
  }
}

export function issueAnthropicCallback<T>(probe: AnthropicFailureProbe, issue: () => Promise<T>): Promise<T> {
  if (probe.closed) { probe.state = "unavailable"; return Promise.reject(new Error("attempt closed")) }
  const callbacks = probe.callbacks ??= new Set()
  if (callbacks.size >= 32) { probe.state = "unavailable"; return Promise.reject(new Error("attempt callback limit")) }
  let resolve!: (value: T | PromiseLike<T>) => void
  let reject!: (reason?: unknown) => void
  const work = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  callbacks.add(work)
  void work.then(() => callbacks.delete(work), () => { probe.state = "unavailable"; callbacks.delete(work) })
  // The placeholder is owned before invocation, including synchronous throws
  // and reentrant closure; normal invocation still begins synchronously.
  try { resolve(issue()) } catch (error) { reject(error) }
  return work
}

// Tracking only: this does not attest reader, SDK or scope finalization.
export async function drainAnthropicCallbacks(probe: AnthropicFailureProbe, timeout = 250): Promise<boolean> {
  probe.closed = true
  let timer: ReturnType<typeof setTimeout> | undefined
  const drained = await Promise.race([
    Promise.allSettled([...(probe.callbacks ?? [])]).then(() => true),
    new Promise<false>((resolve) => { timer = setTimeout(() => resolve(false), timeout) }),
  ]).finally(() => { if (timer) clearTimeout(timer) })
  if (!drained || probe.callbacks?.size || probe.state === "unavailable") { probe.state = "unavailable"; return false }
  return true
}

export async function closeAnthropicProbe(probe: AnthropicFailureProbe, exactError: unknown, scopeFinalized: boolean): Promise<boolean> {
  probe.closed = true // reject registrations before draining already-owned callbacks
  const pending = [...(probe.callbacks ?? [])]
  let timer: ReturnType<typeof setTimeout> | undefined
  const drained = await Promise.race([Promise.allSettled(pending).then(() => true), new Promise<false>((resolve) => { timer = setTimeout(() => resolve(false), 250) })]).finally(() => { if (timer) clearTimeout(timer) })
  const okay = drained && scopeFinalized && !probe.signal?.aborted && probe.state === "observed" && probe.sends === 1 && probe.originalEOF === true && probe.adapterError === exactError && pending.length <= 32
  if (probe.signal && probe.onAbort) probe.signal.removeEventListener("abort", probe.onAbort)
  probe.state = "unavailable" // one-use, including denial
  return okay
}
