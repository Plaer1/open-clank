/** Explicit managed-only transport observation; never discovers external traffic. */
import { AsyncLocalStorage } from "node:async_hooks"
import type { AccountSelection } from "./account-selection"
import {
  OpenClankManagedProtocol,
  type ExtensionConnection,
  type LoggingAdmissionResult,
  type LoggingEventRequest,
  type LoggingHeader,
  type LoggingHttp,
  type LoggingMetricCoverage,
} from "@/acp/openclank-protocol"

const BODY_LIMIT = 256 * 1024
const FRAME_LIMIT = 64 * 1024
const EVENT_LIMIT = 256
const QUEUE_LIMIT = 32
type SdkEvidence = {
  usage: Record<string, number>
  metricCoverage: LoggingMetricCoverage
  normalizationProfile: string
  coveredDispatchIDs: string[]
  lossReasons: NonNullable<LoggingEventRequest["lossReasons"]>
  identityCoverage: "complete" | "partial"
}
type CaptureContext = {
  binding: AccountSelection.Binding
  connection: ExtensionConnection
  admission: LoggingAdmissionResult | null
  dispatches: string[]
  dispatchBase: number
  pendingRetry?: string
  lastSdk?: SdkEvidence
  losses: Set<NonNullable<LoggingEventRequest["lossReasons"]>[number]>
}
const contexts = new WeakMap<object, CaptureContext>()
const sdkDispatchScope = new AsyncLocalStorage<{ scope: object; ids: string[] }>()

/** One provider SDK step, including its transport retries, never a shared cursor. */
export function withSdkDispatches<T>(scope: object | undefined, ids: string[], run: () => T): T {
  return scope ? sdkDispatchScope.run({ scope, ids }, run) : run()
}
let pending = 0
const writes = new Set<Promise<unknown>>()

export function installCaptureScope<T extends object>(
  scope: T,
  binding: AccountSelection.Binding,
  connection: ExtensionConnection,
  admission: LoggingAdmissionResult | null,
): T {
  contexts.set(scope, {
    binding,
    connection,
    admission,
    dispatches: [],
    dispatchBase: 0,
    losses: new Set(admission ? [] : ["admission_unavailable"]),
  })
  return scope
}

export function captureScopeEvidence(
  scope: object | undefined,
): Pick<SdkEvidence, "coveredDispatchIDs" | "lossReasons" | "identityCoverage"> {
  const context = scope ? contexts.get(scope) : undefined
  return {
    coveredDispatchIDs: [...(context?.dispatches ?? [])],
    lossReasons: [...(context?.losses ?? [])],
    identityCoverage: context?.admission?.context.identityCoverage ?? ("partial" as const),
  }
}

/** Called only by the provider retry owner after it decides to retry. */
export function markCaptureRetry(scope: object, previousScope: object = scope) {
  const context = contexts.get(scope),
    previous = contexts.get(previousScope)
  if (!context || !previous) return
  context.pendingRetry = previous.dispatches.at(-1)
  if (scope !== previousScope) context.dispatchBase = previous.dispatchBase + previous.dispatches.length
}

export function sdkUsageEvidence(
  raw: unknown,
  scope?: object,
  normalizationProfile = "managed-sdk-inclusive-v1",
  dispatchOffset = 0,
): SdkEvidence {
  const value = object(raw)
  const input = object(value.inputTokenDetails ?? value.inputTokensDetails)
  const output = object(value.outputTokenDetails ?? value.outputTokensDetails)
  const fields: Record<string, unknown> = {
    inputTokens: value.inputTokens ?? value.tokens,
    outputTokens: value.outputTokens,
    totalTokens: value.totalTokens ?? value.tokens,
    cacheReadTokens: value.cachedInputTokens ?? input.cacheReadTokens,
    cacheWriteTokens: input.cacheWriteTokens,
    reasoningTokens: value.reasoningTokens ?? output.reasoningTokens,
    audioInputTokens: value.audioInputTokens,
    audioOutputTokens: value.audioOutputTokens,
    imageInputTokens: value.imageInputTokens,
    imageOutputTokens: value.imageOutputTokens,
    searchRequests: value.searchRequests,
  }
  const usage = Object.fromEntries(
    Object.entries(fields).flatMap(([key, raw]) => (integer(raw) === undefined ? [] : [[key, integer(raw)!]])),
  )
  const evidence = captureScopeEvidence(scope)
  evidence.coveredDispatchIDs = evidence.coveredDispatchIDs.slice(dispatchOffset)
  const names: Record<string, string> = {
    inputTokens: "input_tokens",
    outputTokens: "output_tokens",
    cacheReadTokens: "cache_read_tokens",
    cacheWriteTokens: "cache_write_tokens",
    reasoningTokens: "reasoning_tokens",
  }
  const metricCoverage = Object.fromEntries(
    Object.keys(usage).map((key) => [
      names[key] ?? key,
      { state: "reported" as const, coverage: "complete" as const, source: "sdk" },
    ]),
  )
  return { usage, metricCoverage, normalizationProfile, ...evidence }
}

export function captureSdkEvidence(scope: object | undefined) {
  const context = scope ? contexts.get(scope) : undefined
  return context?.lastSdk
}

export function recordSdkUsage(scope: object | undefined, raw: unknown, dispatchIDs: string[], mainReply = true) {
  const authority = scope ? contexts.get(scope) : undefined
  if (!authority) return
  const evidence = sdkUsageEvidence(raw, scope)
  evidence.coveredDispatchIDs = [...new Set(dispatchIDs)].filter((id) => authority.dispatches.includes(id))
  if (mainReply) authority.lastSdk = evidence
  const admission = authority.admission
  // A step snapshot may cover several fetches. Do not assign that aggregate
  // arbitrarily to the last request; the ACP operation result carries coverage.
  if (!admission?.context.persistence.numeric || evidence.coveredDispatchIDs.length !== 1) return
  const names: Record<string, string> = {
    inputTokens: "input_tokens",
    outputTokens: "output_tokens",
    cacheReadTokens: "cache_read_tokens",
    cacheWriteTokens: "cache_write_tokens",
    reasoningTokens: "reasoning_tokens",
  }
  queue(authority, {
    admissionID: admission.admissionID,
    dispatchID: evidence.coveredDispatchIDs[0],
    source: "sdk",
    sequence: 1,
    terminal: true,
    outcome: "completed",
    observationKind: "final_snapshot",
    metrics: Object.fromEntries(Object.entries(evidence.usage).map(([name, count]) => [names[name] ?? name, count])),
    metricCoverage: evidence.metricCoverage,
    normalizationProfile: evidence.normalizationProfile,
    coveredDispatchIDs: evidence.coveredDispatchIDs,
    lossReasons: evidence.lossReasons,
    identityCoverage: evidence.identityCoverage,
  })
}

function deadline<T>(promise: Promise<T>, timeout: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("logging deadline")), timeout)
    promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

export async function flushCaptures() {
  await deadline(Promise.allSettled([...writes]), 2100).catch(() => {})
}

function rpc<T>(invoke: () => Promise<T>): Promise<T> {
  // Timeout limits provider waiting, not the real connection resource lease.
  if (pending >= QUEUE_LIMIT) return Promise.reject(new Error("logging RPC capacity"))
  pending++
  let underlying: Promise<T>
  try {
    underlying = invoke()
  } catch (error) {
    pending--
    return Promise.reject(error)
  }
  void underlying
    .finally(() => {
      pending--
    })
    .catch(() => {})
  return underlying
}

function queue(authority: CaptureContext, event: LoggingEventRequest) {
  if (pending >= QUEUE_LIMIT) {
    authority.losses.add("queue_full")
    return
  }
  const underlying = rpc(() =>
    OpenClankManagedProtocol.callHost(authority.connection, "_openclank/logging/v1/events", event),
  )
  writes.add(underlying)
  void underlying.then(
    (result) => {
      if (!result.accepted) authority.losses.add("write_failed")
    },
    () => authority.losses.add("write_failed"),
  )
  void underlying
    .finally(() => {
      writes.delete(underlying)
    })
    .catch(() => {})
  void deadline(underlying, 2000).catch(() => {
    authority.losses.add("delivery_timeout")
  })
  return underlying
}

const SECRET_HEADER =
  /^(?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|api-key|x-goog-api-key|x-amz-security-token|x-auth-token|x-access-token|.*(?:api[-_]?key|auth[-_]?token|access[-_]?token|secret|credential).*)$/i
export function sanitizedHeaders(raw: HeadersInit | undefined): LoggingHeader[] {
  if (!raw) return []
  const headers = new Headers(raw)
  const grouped = new Map<string, string[]>()
  if (Array.isArray(raw)) {
    for (const [name, value] of raw) {
      const key = name.toLowerCase()
      grouped.set(key, [...(grouped.get(key) ?? []), value])
    }
  } else headers.forEach((value, name) => grouped.set(name.toLowerCase(), [value]))
  // getSetCookie is the only repeated collection exposed by Fetch Headers.
  const cookies = headers.getSetCookie?.()
  if (cookies?.length) grouped.set("set-cookie", cookies)
  let bytes = 0
  const result: LoggingHeader[] = []
  for (const [name, values] of grouped) {
    if (result.length >= 64) {
      result[63].state = "truncated"
      break
    }
    if (SECRET_HEADER.test(name)) {
      result.push({ name, values: [], state: "redacted" })
      continue
    }
    const safe: string[] = []
    let truncated = values.length > 8
    for (const value of values.slice(0, 8)) {
      const bounded = Buffer.from(value).subarray(0, 2048).toString()
      if (bytes + Buffer.byteLength(name) + Buffer.byteLength(bounded) > 32768) {
        truncated = true
        break
      }
      safe.push(bounded)
      bytes += Buffer.byteLength(name) + Buffer.byteLength(bounded)
      truncated ||= bounded !== value
    }
    result.push({ name, values: safe, state: truncated ? "truncated" : "reported" })
  }
  return result
}

function safeEndpoint(raw: string) {
  try {
    const url = new URL(raw)
    return {
      origin: url.origin.slice(0, 512),
      path: url.pathname
        .split("/")
        .map((segment, index, segments) =>
          /^(?:sk-|Bearer|eyJ)|[a-zA-Z0-9_-]{48,}/i.test(segment) ||
          /^(?:key|token|secret|credential|auth)$/i.test(segments[index - 1] ?? "")
            ? "[redacted]"
            : segment,
        )
        .join("/")
        .slice(0, 2048),
    }
  } catch {
    return { origin: "unknown", path: "/" }
  }
}

async function boundedRequestBody(request: Request): Promise<unknown> {
  const clone = request.clone()
  const reader = clone.body?.getReader()
  if (!reader) return undefined
  const decoder = new TextDecoder()
  let result = "",
    bytes = 0
  const end = performance.now() + 100
  try {
    while (true) {
      const remaining = end - performance.now()
      if (remaining <= 0) return { omitted: "unsupported" }
      const next = await deadline(reader.read(), Math.min(50, remaining))
      if (next.done) break
      bytes += next.value.length
      if (bytes > BODY_LIMIT) return { omitted: "truncated", received_bytes: bytes }
      result += decoder.decode(next.value, { stream: true })
    }
    result += decoder.decode()
    try {
      return JSON.parse(result)
    } catch {
      return { text: result }
    }
  } catch {
    return { omitted: "unsupported" }
  } finally {
    void reader.cancel().catch(() => {})
  }
}

export function passiveQuota(
  response: unknown,
  providerID: string,
): OpenClankManagedProtocol.OperationExecuteResult["quota"] | undefined {
  if (providerID !== "openai" && providerID !== "anthropic") return undefined
  const headers = (response as { response?: { headers?: Headers | Record<string, string> } })?.response?.headers
  if (!headers) return undefined
  const get = (name: string) =>
    (headers instanceof Headers ? headers.get(name) : (headers[name] ?? headers[name.toLowerCase()])) ?? undefined
  const integer = (name: string) => {
    const raw = get(name)
    if (raw === undefined || !/^\d+$/.test(raw)) return undefined
    const value = Number(raw)
    return Number.isSafeInteger(value) ? value : undefined
  }
  const resetAt = (name: string, format: "duration" | "rfc3339") => {
    const raw = get(name)
    if (!raw || raw.length > 64) return undefined
    if (format === "rfc3339") {
      const parsed = Date.parse(raw)
      return Number.isFinite(parsed) ? new Date(parsed).toISOString() : undefined
    }
    let total = 0
    const matches = [...raw.matchAll(/(\d+)(ms|s|m|h)/g)]
    if (!matches.length || matches.map((match) => match[0]).join("") !== raw) return undefined
    for (const match of matches) {
      const amount = Number(match[1])
      const multiplier = match[2] === "ms" ? 1 : match[2] === "s" ? 1000 : match[2] === "m" ? 60_000 : 3_600_000
      total += amount * multiplier
      if (!Number.isSafeInteger(total) || total > 86_400_000 * 30) return undefined
    }
    return new Date(Date.now() + total).toISOString()
  }
  const metric = (kind: "requests" | "tokens" | "inputTokens" | "outputTokens") => {
    const anthroName = kind === "inputTokens" ? "input-tokens" : kind === "outputTokens" ? "output-tokens" : kind
    const prefix =
      providerID === "anthropic"
        ? `anthropic-ratelimit-${anthroName}`
        : `x-ratelimit-${kind === "inputTokens" || kind === "outputTokens" ? "limit-tokens" : kind}`
    const resetName =
      providerID === "anthropic"
        ? `${prefix}-reset`
        : `x-ratelimit-reset-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const limitName =
      providerID === "anthropic"
        ? `${prefix}-limit`
        : `x-ratelimit-limit-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const remainingName =
      providerID === "anthropic"
        ? `${prefix}-remaining`
        : `x-ratelimit-remaining-${kind === "inputTokens" || kind === "outputTokens" ? "tokens" : kind}`
    const reset = resetAt(resetName, providerID === "anthropic" ? "rfc3339" : "duration")
    return {
      ...(integer(limitName) === undefined ? {} : { limit: integer(limitName) }),
      ...(integer(remainingName) === undefined ? {} : { remaining: integer(remainingName) }),
      ...(reset === undefined ? {} : { resetAt: reset }),
    }
  }
  const requests = metric("requests")
  const tokens = metric("tokens")
  const inputTokens = providerID === "anthropic" ? metric("inputTokens") : {}
  const outputTokens = providerID === "anthropic" ? metric("outputTokens") : {}
  const hasMetric = (value: { limit?: number; remaining?: number; resetAt?: string }) =>
    value.limit !== undefined || value.remaining !== undefined || value.resetAt !== undefined
  const result: {
    transport: "documented"
    adapterRevision: string
    requests?: { limit?: number; remaining?: number; resetAt?: string }
    tokens?: { limit?: number; remaining?: number; resetAt?: string }
    inputTokens?: { limit?: number; remaining?: number; resetAt?: string }
    outputTokens?: { limit?: number; remaining?: number; resetAt?: string }
  } = { transport: "documented", adapterRevision: `${providerID}-capacity-v1` }
  if (hasMetric(requests)) result.requests = requests
  if (hasMetric(tokens)) result.tokens = tokens
  if (hasMetric(inputTokens)) result.inputTokens = inputTokens
  if (hasMetric(outputTokens)) result.outputTokens = outputTokens
  return result.requests || result.tokens || result.inputTokens || result.outputTokens ? result : undefined
}

function captureBody(value: unknown, binary: boolean, depth = 0): unknown {
  if (depth > 20) return "[depth limit]"
  if (typeof value === "string")
    return value.startsWith("data:") && !binary
      ? {
          omitted: "embedded_media",
          encoded_bytes: Buffer.byteLength(value),
          media_type: value.split(";", 1)[0].slice(5, 80),
        }
      : value
  if (Array.isArray(value)) return value.slice(0, EVENT_LIMIT).map((child) => captureBody(child, binary, depth + 1))
  if (value === null || typeof value !== "object") return value
  const source = object(value)
  return Object.fromEntries(
    Object.entries(source).flatMap(([key, child]) => {
      const name = key.toLowerCase().replace(/[_-]/g, "")
      if (
        [
          "authorization",
          "proxyauthorization",
          "cookie",
          "setcookie",
          "apikey",
          "credential",
          "credentials",
          "accesstoken",
          "refreshtoken",
          "password",
          "admissionid",
          "leaseid",
        ].includes(name)
      )
        return []
      const media =
        typeof child === "string" &&
        (["b64json", "base64", "imagedata", "audiodata"].includes(name) ||
          (name === "data" && (source.type === "base64" || ["wav", "mp3", "pcm16"].includes(String(source.format)))))
      return [
        [
          key,
          media && !binary
            ? { omitted: "embedded_media", encoded_bytes: Buffer.byteLength(child as string) }
            : captureBody(child, binary, depth + 1),
        ],
      ]
    }),
  )
}

function object(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {}
}

function integer(value: unknown): number | undefined {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : undefined
}

export function captureFetch(fetchFn: typeof fetch, scope: AccountSelection.Scope | undefined): typeof fetch {
  const authority = scope ? contexts.get(scope) : undefined
  // A standalone CLI can share provider accounts but has no managed authority.
  if (process.env.OPEN_CLANK_MANAGED !== "1" || !authority) return fetchFn
  const captured = async (input: Parameters<typeof fetch>[0], init?: Parameters<typeof fetch>[1]) => {
    const url = input instanceof Request ? input.url : String(input)
    const wireFormat = /\/messages(?:\?|$)/.test(url)
      ? "anthropic-messages.v1"
      : /\/responses(?:\?|$)/.test(url)
        ? "responses.v1"
        : /\/chat\/completions(?:\?|$)/.test(url)
          ? "chat-completions.v1"
          : "unsupported"
    const attemptID = crypto.randomUUID()
    const origin = performance.now()
    const timing: Record<string, number | string | null> = {
      clock_domain: "engine",
      queued_ms: 0,
      admitted_ms: null,
      dispatch_ms: null,
      headers_ms: null,
      first_byte_ms: null,
      first_output_ms: null,
      last_output_ms: null,
      terminal_ms: null,
    }
    const admission = authority.admission
    const enabled = Boolean(admission?.admissionID && admission.context.persistence.numeric)
    const parserSupported = wireFormat !== "unsupported"
    const advanced =
      enabled &&
      admission?.context.transportMode === "advanced_proxy" &&
      admission.policy.advanced_enabled &&
      admission.context.persistence.content
    authority.dispatches.push(attemptID)
    const sdkStep = sdkDispatchScope.getStore()
    if (sdkStep && sdkStep.scope === scope) sdkStep.ids.push(attemptID)
    const dispatchIndex = authority.dispatchBase + authority.dispatches.length
    const retryOfDispatchID = authority.pendingRetry
    authority.pendingRetry = undefined
    const normalizationProfile =
      wireFormat === "anthropic-messages.v1"
        ? "anthropic-wire-separated-v1"
        : wireFormat === "responses.v1"
          ? "openai-wire-inclusive-v1"
          : wireFormat === "chat-completions.v1"
            ? "openai-wire-inclusive-v1"
            : null
    const http: LoggingHttp | undefined = advanced
      ? {
          method: init?.method ?? (input instanceof Request ? input.method : "GET"),
          endpoint: safeEndpoint(url),
          requestHeaders: sanitizedHeaders(init?.headers ?? (input instanceof Request ? input.headers : undefined)),
          coverage: {
            request_headers: "reported",
            request_body: admission!.policy.request_body_enabled ? "pending" : "disabled",
            response_body: admission!.policy.response_body_enabled ? "pending" : "disabled",
          },
        }
      : undefined
    let requestBody: unknown
    if (advanced && admission?.policy.request_body_enabled && typeof init?.body === "string") {
      if (Buffer.byteLength(init.body) <= BODY_LIMIT) {
        try {
          requestBody = JSON.parse(init.body)
        } catch {
          requestBody = { text: init.body }
        }
      } else
        requestBody = { omitted: "request exceeds sanitized parser limit", size_bytes: Buffer.byteLength(init.body) }
    }
    if (
      http?.coverage &&
      admission!.policy.request_body_enabled &&
      init?.body !== undefined &&
      typeof init.body !== "string"
    )
      http.coverage.request_body = "unsupported"
    const requestBodyRead =
      advanced && admission?.policy.request_body_enabled && init?.body === undefined && input instanceof Request
        ? boundedRequestBody(input).then((value) => {
            requestBody = value
            if (http?.coverage) http.coverage.request_body = "reported"
          })
        : undefined
    const events: unknown[] = []
    const metrics: Record<string, number> = {}
    const losses = new Set<NonNullable<LoggingEventRequest["lossReasons"]>[number]>()
    const decoder = new TextDecoder()
    let buffer = ""
    let jsonBuffer = ""
    let bodyBytes = 0
    let eventBytes = 0
    let finished = false
    let actualModel: string | undefined
    let responseText = ""
    let structuredResponse: unknown
    let observedOutcome: LoggingEventRequest["outcome"]
    let quota: OpenClankManagedProtocol.OperationExecuteResult["quota"] | undefined
    let skipFrame = false
    let status = 0
    let sequence = 0
    if (
      advanced &&
      admission?.policy.request_body_enabled &&
      typeof init?.body === "string" &&
      Buffer.byteLength(init.body) > BODY_LIMIT
    )
      losses.add("truncated")

    function parse(value: unknown) {
      const event = object(value)
      const response = object(event.response)
      const message = object(event.message)
      const usage = object(response.usage ?? event.usage ?? message.usage ?? event.usageMetadata)
      const inputDetail = object(usage.input_tokens_details ?? usage.prompt_tokens_details)
      const outputDetail = object(usage.output_tokens_details ?? usage.completion_tokens_details)
      const fields: Record<string, unknown> = {
        input_tokens: usage.input_tokens ?? usage.prompt_tokens ?? usage.inputTokens ?? usage.promptTokenCount,
        output_tokens:
          usage.output_tokens ?? usage.completion_tokens ?? usage.outputTokens ?? usage.candidatesTokenCount,
        cache_read_tokens:
          usage.cache_read_input_tokens ??
          inputDetail.cached_tokens ??
          usage.cacheReadTokens ??
          usage.cachedContentTokenCount,
        cache_write_tokens:
          usage.cache_creation_input_tokens ?? inputDetail.cache_write_tokens ?? usage.cacheWriteTokens,
        audioInputTokens: inputDetail.audio_tokens,
        audioOutputTokens: outputDetail.audio_tokens,
        imageInputTokens: inputDetail.image_tokens,
        imageOutputTokens: outputDetail.image_tokens,
        searchRequests: usage.web_search_requests ?? object(usage.server_tool_use).web_search_requests,
        reasoning_tokens: outputDetail.reasoning_tokens ?? usage.reasoningTokens ?? usage.thoughtsTokenCount,
      }
      let changed = false
      for (const [key, raw] of Object.entries(fields)) {
        const count = integer(raw)
        if (count !== undefined) {
          changed ||= metrics[key] !== count
          metrics[key] = count
        }
      }
      if (changed && enabled && admission)
        queue(authority!, {
          admissionID: admission.admissionID,
          dispatchID: attemptID,
          source: "wire",
          sequence: ++sequence,
          observationKind: "cumulative_snapshot",
          metrics: { ...metrics },
          metricCoverage: Object.fromEntries(
            Object.keys(metrics).map((key) => [key, { state: "reported", coverage: "partial", source: "wire" }]),
          ),
          normalizationProfile,
          identityCoverage: admission.context.identityCoverage,
          dispatchIndex,
          retryOfDispatchID,
        })
      const model = event.model ?? response.model ?? message.model
      if (typeof model === "string") actualModel = model
      const delta = object(event.delta)
      const firstChoice = Array.isArray(event.choices) ? object(event.choices[0]) : {}
      const choiceDelta = object(firstChoice.delta)
      const choiceMessage = object(firstChoice.message)
      if (
        event.type === "response.completed" ||
        event.type === "message_stop" ||
        (firstChoice.finish_reason && firstChoice.finish_reason !== "error")
      )
        observedOutcome = "completed"
      if (event.type === "response.failed" || event.type === "error" || firstChoice.finish_reason === "error")
        observedOutcome = "upstream_error"
      const text =
        typeof event.delta === "string" ? event.delta : (delta.text ?? choiceDelta.content ?? choiceMessage.content)
      const meaningful =
        (typeof text === "string" && text.length > 0) ||
        Boolean(choiceDelta.tool_calls) ||
        Boolean(delta.partial_json) ||
        Boolean(event.content_block)
      if (meaningful && timing.first_output_ms === null) timing.first_output_ms = performance.now() - origin
      if (advanced && admission?.policy.response_body_enabled) {
        const eventSize = Buffer.byteLength(JSON.stringify(value))
        if (events.length < EVENT_LIMIT && eventBytes + eventSize <= BODY_LIMIT) {
          events.push(value)
          eventBytes += eventSize
        } else losses.add("truncated")
        if (typeof text === "string") {
          const remaining = Math.max(0, BODY_LIMIT / 4 - responseText.length)
          responseText += text.slice(0, remaining)
          if (text.length > remaining) losses.add("truncated")
        }
      }
    }

    function chunk(bytes: Uint8Array, streaming: boolean) {
      if (!enabled) return
      const now = performance.now() - origin
      if (timing.first_byte_ms === null && bytes.length) timing.first_byte_ms = now
      if (bytes.length) timing.last_output_ms = now
      bodyBytes += bytes.length
      const text = decoder.decode(bytes, { stream: true })
      if (!streaming) {
        if (jsonBuffer.length + text.length <= BODY_LIMIT) jsonBuffer += text
        else losses.add("parse_degraded")
        return
      }
      buffer = (buffer + text).replace(/\r\n/g, "\n")
      let boundary = buffer.indexOf("\n\n")
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary)
        buffer = buffer.slice(boundary + 2)
        if (!skipFrame && frame.length <= FRAME_LIMIT) {
          const data = frame
            .split("\n")
            .filter((line) => line.startsWith("data:"))
            .map((line) => line.slice(5).trimStart())
            .join("\n")
          if (data === "[DONE]") observedOutcome = "completed"
          if (data && data !== "[DONE]") {
            try {
              parse(JSON.parse(data))
            } catch {
              losses.add("parse_degraded")
            }
          }
        } else losses.add("parse_degraded")
        skipFrame = false
        boundary = buffer.indexOf("\n\n")
      }
      if (buffer.length > FRAME_LIMIT) {
        skipFrame = true
        buffer = buffer.slice(-2)
        losses.add("parse_degraded")
      }
    }

    function finish(outcome: NonNullable<LoggingEventRequest["outcome"]>, streaming: boolean) {
      if (finished) return
      finished = true
      timing.terminal_ms = performance.now() - origin
      if (!enabled || !admission) return
      if (!streaming && jsonBuffer && !losses.has("parse_degraded")) {
        try {
          structuredResponse = JSON.parse(jsonBuffer + decoder.decode())
          parse(structuredResponse)
        } catch {
          losses.add("parse_degraded")
        }
      }
      if (!parserSupported) losses.add("parse_degraded")
      if (streaming && buffer.trim()) losses.add("parse_degraded")
      if (observedOutcome) outcome = observedOutcome
      else if (streaming && outcome === "completed") {
        outcome = "unknown"
        losses.add("parse_degraded")
      }
      for (const reason of authority!.losses) losses.add(reason)
      const payload: LoggingEventRequest = {
        admissionID: admission.admissionID,
        dispatchID: attemptID,
        source: "wire",
        sequence: ++sequence,
        terminal: true,
        observationKind: "final_snapshot",
        dispatchIndex,
        retryOfDispatchID,
        identityCoverage: admission.context.identityCoverage,
        metricCoverage: Object.fromEntries(
          Object.keys(metrics).map((key) => [
            key,
            {
              state: "reported",
              coverage: losses.has("parse_degraded") || outcome !== "completed" ? "partial" : "complete",
              source: "wire",
            },
          ]),
        ),
        outcome,
        metrics,
        actualModel,
        ...(quota ? { quota } : {}),
        normalizationProfile,
        timing,
        ...(http ? { http } : {}),
        ...(requestBody !== undefined ? { requestBody } : {}),
        ...(advanced && admission.policy.response_body_enabled
          ? {
              responseBody:
                structuredResponse !== undefined
                  ? { structured: structuredResponse, received_bytes: bodyBytes }
                  : { text: responseText, received_bytes: bodyBytes },
              events,
            }
          : {}),
        lossReasons: [...losses],
        billable: null,
      }
      const binary = Boolean(admission.policy.binary_body_enabled)
      if (payload.requestBody !== undefined) payload.requestBody = captureBody(payload.requestBody, binary)
      if (payload.responseBody !== undefined) payload.responseBody = captureBody(payload.responseBody, binary)
      if (payload.events) payload.events = payload.events.map((event) => captureBody(event, binary))
      if (Buffer.byteLength(JSON.stringify(payload)) > 900 * 1024) {
        delete payload.events
        delete payload.requestBody
        delete payload.responseBody
        losses.add("truncated")
        payload.lossReasons = [...new Set([...losses, "truncated" as const])]
      }
      if (http?.coverage) {
        if (requestBody !== undefined)
          http.coverage.request_body = object(requestBody).omitted ? String(object(requestBody).omitted) : "reported"
        if (admission.policy.response_body_enabled)
          http.coverage.response_body = losses.has("truncated")
            ? "truncated"
            : parserSupported
              ? "reported"
              : "unsupported"
      }
      for (const reason of losses) authority!.losses.add(reason)
      // Request clone observation never blocks upstream dispatch or response.
      if (requestBodyRead) {
        const deferred = requestBodyRead.then(() => {
          if (requestBody !== undefined) payload.requestBody = captureBody(requestBody, binary)
          if (http?.coverage)
            http.coverage.request_body = object(requestBody).omitted ? String(object(requestBody).omitted) : "reported"
          return queue(authority!, payload)
        })
        writes.add(deferred)
        void deferred.finally(() => writes.delete(deferred)).catch(() => {})
      } else queue(authority!, payload)
    }

    timing.dispatch_ms = performance.now() - origin
    if (enabled && admission)
      queue(authority, {
        admissionID: admission.admissionID,
        dispatchID: attemptID,
        source: "wire",
        sequence: 0,
        observationKind: "cumulative_snapshot",
        dispatchIndex,
        retryOfDispatchID,
        identityCoverage: admission.context.identityCoverage,
        timing,
        ...(http ? { http } : {}),
      })
    let res: Response
    try {
      res = await fetchFn(input, init)
      status = res.status
      timing.headers_ms = performance.now() - origin
      if (http) {
        http.status = res.status
        http.responseHeaders = sanitizedHeaders(res.headers)
        http.coverage!.response_headers = "reported"
      }
      quota = passiveQuota({ response: { headers: res.headers } }, String(admission?.context.providerID ?? ""))
    } catch (error) {
      finish(
        (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
          ? "cancelled"
          : "upstream_error",
        true,
      )
      throw error
    }
    if (!enabled) return res
    const streaming = res.headers.get("content-type")?.includes("text/event-stream") ?? false
    if (!res.body) {
      finish(status >= 400 ? "upstream_error" : "completed", streaming)
      return res
    }
    const reader = res.body.getReader()
    const wrapped = new Response(
      new ReadableStream<Uint8Array>({
        async pull(controller) {
          try {
            const next = await reader.read()
            if (next.done) {
              finish(status >= 400 ? "upstream_error" : "completed", streaming)
              controller.close()
              return
            }
            // Parser failures never alter the response byte ordering/status.
            try {
              chunk(next.value, streaming)
            } catch {
              losses.add("parse_degraded")
            }
            controller.enqueue(next.value)
          } catch (error) {
            finish(
              (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
                ? "cancelled"
                : "upstream_error",
              streaming,
            )
            controller.error(error)
          }
        },
        async cancel(reason) {
          finish(
            (init?.signal ?? (input instanceof Request ? input.signal : undefined))?.aborted
              ? "cancelled"
              : "disconnected",
            streaming,
          )
          await reader.cancel(reason)
        },
      }),
      { status: res.status, statusText: res.statusText, headers: res.headers },
    )
    Object.defineProperty(wrapped, "url", { value: res.url })
    Object.defineProperty(wrapped, "redirected", { value: res.redirected })
    return wrapped
  }
  return Object.assign(captured, { preconnect: fetchFn.preconnect })
}
