/** Shared conservative provider failure interpretation; no retry policy here. */
export type FailureKind = "throttle" | "exhausted" | "auth" | "entitlement" | "transient" | "malformed_success" | "unknown"
export type Failure = { kind: FailureKind; status?: number; retryAfterMs?: number }

const exhaustion = [/limit exhausted/i, /insufficient_quota/i, /exceeded your current quota/i, /credit balance is too low/i, /out of credits/i, /quota exceeded/i, /SubscriptionUsageLimitError/i]
export function isExhaustion(text: string): boolean { return exhaustion.some((pattern) => pattern.test(text)) }
function header(headers: unknown, name: string): string | undefined {
  if (!headers || typeof headers !== "object") return
  if (typeof (headers as Headers).get === "function") return (headers as Headers).get(name) ?? undefined
  const entry = Object.entries(headers).find(([key]) => key.toLowerCase() === name)
  return typeof entry?.[1] === "string" || typeof entry?.[1] === "number" ? String(entry[1]).trim() : undefined
}
function numeric(raw: string | undefined): number | undefined {
  if (raw === undefined || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(raw)) return
  const value = Number(raw)
  return Number.isFinite(value) ? Math.min(value, Number.MAX_SAFE_INTEGER) : Number.MAX_SAFE_INTEGER
}
export function retryAfter(headers: unknown, now = Date.now()): number | undefined {
  const ms = numeric(header(headers, "retry-after-ms"))
  if (ms !== undefined) return Math.ceil(ms)
  const raw = header(headers, "retry-after")
  const seconds = numeric(raw)
  if (seconds !== undefined) return Math.min(Number.MAX_SAFE_INTEGER, Math.ceil(seconds * 1000))
  if (!raw || !/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun), \d{2} (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{4} \d{2}:\d{2}:\d{2} GMT$/.test(raw)) return
  const date = Date.parse(raw)
  if (!Number.isFinite(date) || new Date(date).toUTCString() !== raw || date <= now) return
  return Math.ceil(date - now)
}
export function classifyFailure(error: unknown, now = Date.now()): Failure {
  const value = error && typeof error === "object" ? error as any : {}
  const raw = value.status ?? value.statusCode ?? value.response?.status ?? value.data?.statusCode
  const status = typeof raw === "number" ? raw : typeof raw === "string" && /^\d{3}$/.test(raw) ? Number(raw) : undefined
  const safeStatus = typeof status === "number" && Number.isInteger(status) && status >= 100 && status <= 599 ? status : undefined
  const detail = [value.message, value.responseBody, value.data?.message, value.data?.responseBody].filter((v) => typeof v === "string").join(" ")
  const headers = value.response?.headers ?? value.responseHeaders ?? value.data?.responseHeaders
  const after = retryAfter(headers, now)
  const evidence = { ...(safeStatus === undefined ? {} : { status: safeStatus }), ...(after === undefined ? {} : { retryAfterMs: after }) }
  if (safeStatus === 401 || /invalid authentication|invalid_api_key/i.test(detail)) return { kind: "auth", ...evidence }
  // Free-form provider text is not evidence of an exhausted account window.
  // Provider-specific typed evidence must supply that classification separately.
  if (safeStatus === 429 || /rate.limit|too.many.requests/i.test(detail)) return { kind: "throttle", ...evidence }
  if (safeStatus === 403 || safeStatus === 404) return { kind: "entitlement", ...evidence }
  if ((safeStatus !== undefined && safeStatus >= 500) || /ECONNRESET|EPIPE|ETIMEDOUT/.test(String(value.code ?? "")) || /sse read timed out|network error/i.test(detail)) return { kind: "transient", ...evidence }
  return { kind: "unknown", ...evidence }
}
