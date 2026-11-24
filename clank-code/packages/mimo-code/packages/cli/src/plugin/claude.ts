import { createHash, randomBytes, randomUUID } from "node:crypto"
import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import {
  CLAUDE_SUBSCRIPTION_REQUEST_PROFILE,
  profileClaudeSubscriptionRequest,
  type ClaudeSubscriptionRequestContext,
} from "@/plugin/claude-subscription-profile"

export const CLAUDE_PROFILE = {
  authorizeURL: "https://claude.ai/oauth/authorize",
  tokenURL: "https://platform.claude.com/v1/oauth/token",
  clientID: "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
  defaultRedirectURI: "http://localhost:54545/callback",
  scopes: ["user:inference", "user:profile", "user:sessions:claude_code"],
  apiBaseURL: "https://api.anthropic.com/v1",
  anthropicVersion: "2023-06-01",
  oauthBeta: "oauth-2025-04-20",
} as const

export const CLAUDE_DISCOVERY_REQUEST = {
  url: `${CLAUDE_PROFILE.apiBaseURL}/models?limit=1000`,
  method: "GET",
  headers: {
    "anthropic-version": CLAUDE_PROFILE.anthropicVersion,
    "anthropic-beta": CLAUDE_PROFILE.oauthBeta,
    "User-Agent": "OpenClank/managed-anthropic",
    accept: "application/json",
  },
} as const

export function claudeDiscoveryRequest(access: string): { url: string; init: RequestInit } {
  if (!access) throw new Error("Claude discovery requires a leased access token")
  return {
    url: CLAUDE_DISCOVERY_REQUEST.url,
    init: {
      method: CLAUDE_DISCOVERY_REQUEST.method,
      headers: { ...CLAUDE_DISCOVERY_REQUEST.headers, authorization: `Bearer ${access}` },
      redirect: "error",
    },
  }
}

export type ClaudePKCE = {
  state: string
  verifier: string
  challenge: string
}

export type ClaudeCallback = {
  code: string
  state: string
  error?: string
}

export class ClaudeOAuthError extends Error {
  constructor(readonly category: "denied" | "timeout" | "token", readonly status?: number) {
    super(`Claude OAuth ${category}${status === undefined ? "" : ` (${status})`}`)
    this.name = "ClaudeOAuthError"
  }
}

export type ClaudeCredential = {
  access: string
  refresh: string
  expires: number
  accountId?: string
}

export type ClaudeManagedCredential = Pick<ClaudeCredential, "access" | "refresh" | "expires" | "accountId">

export class ClaudeMinimumVersionError extends Error {
  constructor(readonly minimumVersion: string) {
    super(`Claude subscription requires a newer compatible request profile (minimum ${minimumVersion})`)
    this.name = "ClaudeMinimumVersionError"
  }
}

async function boundedErrorJSON(
  response: Response,
  signal?: AbortSignal,
): Promise<Record<string, unknown> | undefined> {
  if (!response.body) return undefined
  let reader: ReadableStreamDefaultReader<Uint8Array>
  try {
    const body = response.clone().body
    if (!body) return undefined
    reader = body.getReader()
  } catch {
    return undefined
  }
  const chunks: Uint8Array[] = []
  let total = 0
  let pending: ReturnType<ReadableStreamDefaultReader<Uint8Array>["read"]> | undefined
  let abandoned = false
  let stop!: (value: "timeout" | "aborted") => void
  const stopped = new Promise<"timeout" | "aborted">((resolve) => { stop = resolve })
  const timer = setTimeout(() => stop("timeout"), 200)
  const onAbort = () => stop("aborted")
  signal?.addEventListener("abort", onAbort, { once: true })
  if (signal?.aborted) stop("aborted")
  const release = () => { try { reader.releaseLock() } catch {} }
  const abandon = () => {
    abandoned = true
    // Do not await cancellation of a clone tee: it can wait for the untouched
    // response branch. The outstanding read settles when that branch advances
    // or the request aborts, at which point its reader lock is released.
    void reader.cancel().catch(() => undefined)
    if (pending) void pending.then(release, release)
    else release()
  }
  try {
    while (total <= 4096) {
      pending = reader.read()
      const current = pending
      const result = await Promise.race([
        current.then((value) => ({ kind: "read" as const, value }), () => ({ kind: "read-error" as const })),
        stopped.then((kind) => ({ kind })),
      ])
      if (result.kind === "timeout" || result.kind === "aborted") {
        abandon()
        return undefined
      }
      if (result.kind !== "read") return undefined
      const next = result.value
      pending = undefined
      if (next.done) break
      total += next.value.byteLength
      if (total > 4096) {
        abandon()
        return undefined
      }
      chunks.push(next.value)
    }
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener("abort", onAbort)
    if (!abandoned) release()
  }
  const bytes = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) {
    bytes.set(chunk, offset)
    offset += chunk.byteLength
  }
  let value: unknown
  try {
    value = JSON.parse(new TextDecoder().decode(bytes))
  } catch {
    return undefined
  }
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined
  return value as Record<string, unknown>
}

export async function claudeMinimumVersion(response: Response, signal?: AbortSignal): Promise<string | undefined> {
  if (response.status !== 400) return undefined
  const payload = await boundedErrorJSON(response, signal)
  const error = payload?.error
  if (!error || typeof error !== "object" || Array.isArray(error)) return undefined
  const detail = error as Record<string, unknown>
  if (detail.code !== "claude_code_version_too_old") return undefined
  if (typeof detail.message !== "string" || detail.message.length > 2048) return undefined
  return /\bClaude Code version (\d{1,3}\.\d{1,3}\.\d{1,3}) or newer\b/i.exec(detail.message)?.[1]
}

async function closeClaudeResponse(response: Response): Promise<void> {
  if (!response.body) return
  try {
    // A Response clone tees its body; never wait for cancellation of the
    // untouched branch. The classifier already bounded and read its clone.
    void response.body.cancel().catch(() => undefined)
  } catch {
    // Preserve the bounded compatibility error if cancellation is unavailable.
  }
}

function compareClaudeVersion(left: string, right: string): number {
  const a = left.split(".").map(Number)
  const b = right.split(".").map(Number)
  for (let index = 0; index < 3; index++) {
    if (a[index] !== b[index]) return a[index] < b[index] ? -1 : 1
  }
  return 0
}

export function createManagedClaudeFetch(
  credential: ClaudeManagedCredential,
  upstream: typeof fetch = globalThis.fetch,
  refresh?: (credential: ClaudeManagedCredential) => Promise<ClaudeManagedCredential>,
  requestContext?: (credential: ClaudeManagedCredential) => Promise<ClaudeSubscriptionRequestContext> | ClaudeSubscriptionRequestContext,
  admitSend?: (credential: ClaudeManagedCredential) => Promise<void>,
  beforeUpstream?: (retryAfter401: boolean) => void,
): typeof fetch {
  let current = credential
  let inFlightRefresh: Promise<ClaudeManagedCredential> | undefined
  const refreshOnce = async () => {
    if (!refresh) return current
    if (!inFlightRefresh) {
      inFlightRefresh = refresh(current)
        .then((next) => {
          current = next
          return next
        })
        .finally(() => {
          inFlightRefresh = undefined
        })
    }
    return inFlightRefresh
  }
  return (async (input: RequestInfo | URL, init?: RequestInit) => {
    const requestURL = new URL(input instanceof Request ? input.url : input instanceof URL ? input : input)
    if (requestURL.username || requestURL.password || requestURL.origin !== new URL(CLAUDE_PROFILE.apiBaseURL).origin) {
      throw new Error("Managed Claude subscription request origin is not allowed")
    }
    const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
    const assertNotAborted = () => {
      if (signal?.aborted) throw signal.reason ?? new DOMException("The operation was aborted", "AbortError")
    }
    assertNotAborted()
    const send = async (retryAfter401 = false) => {
      let headers = new Headers(input instanceof Request ? input.headers : undefined)
      new Headers(init?.headers).forEach((value, key) => headers.set(key, value))
      headers.delete("x-api-key")
      headers.delete("api-key")
      headers.delete("proxy-authorization")
      headers.set("authorization", `Bearer ${current.access}`)
      headers.set("anthropic-version", CLAUDE_PROFILE.anthropicVersion)
      headers.set("User-Agent", CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.userAgent)
      headers.set("x-request-id", randomUUID())
      const beta = headers.get("anthropic-beta")?.split(",").map((item) => item.trim()).filter(Boolean) ?? []
      if (!beta.includes(CLAUDE_PROFILE.oauthBeta)) beta.push(CLAUDE_PROFILE.oauthBeta)
      headers.set("anthropic-beta", [...new Set(beta)].join(","))
      let body = typeof init?.body === "string" ? init.body : undefined
      if (body === undefined && input instanceof Request && input.body) body = await input.clone().text()
      const isMessagesRequest = requestURL.pathname === "/v1/messages" && body !== undefined
      if (isMessagesRequest) {
        if (!requestContext) throw new Error("Managed Claude subscription request context is unavailable")
        const context = await requestContext(current)
        assertNotAborted()
        const profiled = profileClaudeSubscriptionRequest(body!, headers, context)
        headers = profiled.headers
        body = profiled.body
        headers.set("content-type", "application/json")
        headers.delete("content-length")
      }
      assertNotAborted()
      const requestInit: RequestInit = { ...init, method: init?.method ?? (input instanceof Request ? input.method : undefined), headers, redirect: "error" }
      if (body !== undefined) requestInit.body = body
      const outgoing = input instanceof Request
        ? new Request(input, requestInit)
        : input
      if (admitSend) {
        try { await admitSend(current) } catch (error) {
          assertNotAborted()
          if (error instanceof Error && error.name === "AbortError") throw error
          // Admission failed before dispatch. Do not expose Host details or retry
          // the SDK-wrapped failure as a transient provider/network error.
          const refused = new Error("Claude subscription admission is unavailable")
          refused.name = "ClaudeAdmissionRefusedError"
          throw refused
        }
      }
      assertNotAborted()
      beforeUpstream?.(retryAfter401)
      return upstream(outgoing, input instanceof Request ? { redirect: "error" } : requestInit)
    }
    let refreshedForRequest = false
    if (refresh && Number.isFinite(current.expires) && current.expires <= Date.now() + 120_000) {
      await refreshOnce()
      assertNotAborted()
      refreshedForRequest = true
    }
    let response = await send()
    assertNotAborted()
    const minimumVersion = await claudeMinimumVersion(response, signal)
    assertNotAborted()
    if (minimumVersion && compareClaudeVersion(minimumVersion, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.cliVersion) > 0) {
      await closeClaudeResponse(response)
      throw new ClaudeMinimumVersionError(minimumVersion)
    }
    if (refresh && !refreshedForRequest && response.status === 401) {
      await response.body?.cancel()
      assertNotAborted()
      await refreshOnce()
      assertNotAborted()
      response = await send(true)
      assertNotAborted()
      const retryMinimumVersion = await claudeMinimumVersion(response, signal)
      assertNotAborted()
      if (retryMinimumVersion && compareClaudeVersion(retryMinimumVersion, CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.cliVersion) > 0) {
        await closeClaudeResponse(response)
        throw new ClaudeMinimumVersionError(retryMinimumVersion)
      }
    }
    return response
  }) as typeof fetch
}

export type ClaudeOAuthResult = {
  url: string
  method: "auto" | "code"
  instructions: string
  callback(code?: string, signal?: AbortSignal): Promise<ClaudeCredential | undefined>
  /** Internal flow cleanup; the registry will adapt this to its official hook. */
  dispose?: () => void | Promise<void>
}

export async function ClaudeAuthPlugin(_input: PluginInput): Promise<Hooks> {
  const authorize = (method: "auto" | "code") => async (inputs: Record<string, string> = {}) => {
    const result = await createClaudeOAuthResult({
      method,
      state: inputs.__openclank_state,
    })
    return {
      url: result.url,
      method: result.method,
      instructions: result.instructions,
      callback: async (code?: string, signal?: AbortSignal) => {
        try {
          const credential = await result.callback(code, signal)
          if (!credential) return { type: "failed" as const }
          return { type: "success" as const, ...credential }
        } catch {
          return { type: "failed" as const }
        }
      },
      dispose: result.dispose,
    }
  }
  return {
    auth: {
      provider: "anthropic",
      methods: [{
        type: "oauth" as const,
        credentialKinds: ["oauth"] as const,
        billingLanes: ["subscription"] as const,
        label: "Claude subscription (browser login)",
        authorize: authorize("auto"),
      }, {
        type: "oauth" as const,
        credentialKinds: ["oauth"] as const,
        billingLanes: ["subscription"] as const,
        label: "Claude subscription (paste callback)",
        authorize: authorize("code"),
      }],
    },
  }
}

type FetchLike = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>

export type ClaudeListener = {
  redirectURI: string
  close(): void | Promise<void>
}

export type ClaudeListenerFactory = (input: {
  preferredPort: number
  onCallback(value: string): boolean | "denied"
}) => Promise<ClaudeListener>

const randomURLSafe = (bytes = 32) => randomBytes(bytes).toString("base64url")

export function createPKCE(random = randomURLSafe): ClaudePKCE {
  const verifier = random(32)
  const state = random(32)
  const challenge = createHash("sha256").update(verifier).digest("base64url")
  return { state, verifier, challenge }
}

export function buildClaudeAuthorizeURL(input: {
  pkce: ClaudePKCE
  redirectURI: string
}): string {
  const url = new URL(CLAUDE_PROFILE.authorizeURL)
  url.search = new URLSearchParams({
    client_id: CLAUDE_PROFILE.clientID,
    redirect_uri: input.redirectURI,
    response_type: "code",
    scope: CLAUDE_PROFILE.scopes.join(" "),
    state: input.pkce.state,
    code_challenge: input.pkce.challenge,
    code_challenge_method: "S256",
    code: "true",
  }).toString()
  return url.toString()
}

function oneParam(params: URLSearchParams, name: string): string | undefined {
  const values = params.getAll(name)
  if (values.length !== 1 || !values[0]) return undefined
  return values[0]
}

/** Parse a complete callback URL or the explicit `code#state` manual form. */
export function parseClaudeCallback(value: string, expected: { redirectURI: string; state: string }): ClaudeCallback {
  if (typeof value !== "string" || value.length === 0 || value.length > 8192) {
    throw new Error("callback input is empty or too large")
  }

  let code: string | undefined
  let state: string | undefined
  if (!value.includes("://") && value.includes("#")) {
    const parts = value.split("#")
    if (parts.length !== 2) throw new Error("callback code/state is malformed")
    code = parts[0]
    state = parts[1]
  } else {
    let url: URL
    try {
      url = new URL(value)
    } catch {
      throw new Error("callback URL is malformed")
    }
    const expectedURL = new URL(expected.redirectURI)
    if (url.origin !== expectedURL.origin || url.pathname !== expectedURL.pathname || url.username || url.password) {
      throw new Error("callback URL does not match the authorization redirect")
    }
    code = oneParam(url.searchParams, "code")
    state = oneParam(url.searchParams, "state")
    if (url.hash) throw new Error("callback contains an unexpected fragment")
    const error = oneParam(url.searchParams, "error")
    if (error) {
      if (!state || state !== expected.state) throw new Error("callback state is missing or mismatched")
      return { code: "", state, error }
    }
  }

  if (!state || state !== expected.state) throw new Error("callback state is missing or mismatched")
  if (!code) throw new Error("callback code is missing")
  return { code, state }
}

function tokenExpiry(expiresIn: unknown, receivedAt: number): number {
  if (typeof expiresIn !== "number" || !Number.isFinite(expiresIn) || expiresIn <= 0) {
    throw new Error("provider returned an invalid token expiry")
  }
  const expires = receivedAt + expiresIn * 1000
  if (!Number.isSafeInteger(expires) || expires <= receivedAt) throw new Error("provider returned an invalid token expiry")
  return expires
}

async function readTokenResponse(
  response: Response,
  receivedAt: number,
  priorRefresh?: string,
  priorAccountId?: string,
): Promise<ClaudeCredential> {
  if (!response.ok) throw new ClaudeOAuthError("token", response.status)
  const body = await response.json().catch(() => undefined) as unknown
  if (!body || typeof body !== "object" || Array.isArray(body)) throw new Error("provider returned an invalid token body")
  const tokens = body as Record<string, unknown>
  if (typeof tokens.access_token !== "string" || !tokens.access_token) throw new Error("provider returned no access token")
  const refresh = typeof tokens.refresh_token === "string" && tokens.refresh_token ? tokens.refresh_token : priorRefresh
  if (!refresh) throw new Error("provider returned no refresh token")
  return {
    access: tokens.access_token,
    refresh,
    expires: tokenExpiry(tokens.expires_in, receivedAt),
    ...(typeof tokens.account_id === "string" && tokens.account_id ? { accountId: tokens.account_id } : priorAccountId ? { accountId: priorAccountId } : {}),
  }
}

function requestSignal(signal: AbortSignal | undefined, timeoutMs: number): { signal: AbortSignal; dispose: () => void } {
  const controller = new AbortController()
  const abort = () => controller.abort(signal?.reason)
  if (signal?.aborted) abort()
  else signal?.addEventListener("abort", abort, { once: true })
  const timer = setTimeout(() => controller.abort(new Error("Claude OAuth request timed out")), timeoutMs)
  return { signal: controller.signal, dispose: () => { clearTimeout(timer); signal?.removeEventListener("abort", abort) } }
}

export async function exchangeClaudeCode(input: {
  callback: ClaudeCallback
  pkce: ClaudePKCE
  redirectURI: string
  fetch?: FetchLike
  signal?: AbortSignal
  now?: () => number
  timeoutMs?: number
}): Promise<ClaudeCredential> {
  const request = requestSignal(input.signal, Math.min(input.timeoutMs ?? 15_000, 15_000))
  try {
  let response: Response
  try {
    response = await (input.fetch ?? fetch)(CLAUDE_PROFILE.tokenURL, {
    method: "POST",
    signal: request.signal,
    redirect: "error",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      grant_type: "authorization_code",
      client_id: CLAUDE_PROFILE.clientID,
      code: input.callback.code,
      state: input.callback.state,
      redirect_uri: input.redirectURI,
      code_verifier: input.pkce.verifier,
    }),
    })
  } catch (error) {
    if (request.signal.aborted) throw new ClaudeOAuthError("timeout")
    throw error
  }
  return await readTokenResponse(response, (input.now ?? Date.now)(), undefined)
  } finally { request.dispose() }
}

export async function refreshClaudeCredential(input: {
  refresh: string
  fetch?: FetchLike
  signal?: AbortSignal
  now?: () => number
  accountId?: string
  timeoutMs?: number
}): Promise<ClaudeCredential> {
  const request = requestSignal(input.signal, Math.min(input.timeoutMs ?? 15_000, 15_000))
  try {
  let response: Response
  try {
    response = await (input.fetch ?? fetch)(CLAUDE_PROFILE.tokenURL, {
    method: "POST",
    signal: request.signal,
    redirect: "error",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify({
      grant_type: "refresh_token",
      client_id: CLAUDE_PROFILE.clientID,
      refresh_token: input.refresh,
    }),
    })
  } catch (error) {
    if (request.signal.aborted) throw new ClaudeOAuthError("timeout")
    throw error
  }
  return await readTokenResponse(response, (input.now ?? Date.now)(), input.refresh, input.accountId)
  } finally { request.dispose() }
}

const defaultListenerFactory: ClaudeListenerFactory = async ({ preferredPort, onCallback }) => {
  if (typeof Bun === "undefined") throw new Error("Claude auto login requires the Bun runtime")
  const options = {
    hostname: "localhost",
    port: preferredPort,
    fetch(request: Request) {
      if (request.method !== "GET") return new Response("Method not allowed", { status: 405 })
      const url = new URL(request.url)
      if (url.pathname !== "/callback") return new Response("Not found", { status: 404 })
      const accepted = onCallback(url.toString())
      if (accepted !== true) return new Response("Callback rejected.", { status: 400 })
      const nonce = crypto.randomUUID()
      return new Response(`<!doctype html><meta charset="utf-8"><title>Authorization received</title><p>Authorization received. You can close this tab and return to Open Clank.</p><script nonce="${nonce}">setTimeout(() => window.close(), 2500)</script>`, {
        headers: {
          "content-type": "text/html; charset=utf-8",
          "cache-control": "no-store",
          "referrer-policy": "no-referrer",
          "content-security-policy": `default-src 'none'; script-src 'nonce-${nonce}'; base-uri 'none'`,
        },
      })
    },
  } as const
  let server: Bun.Server<unknown>
  try {
    server = Bun.serve(options)
  } catch (error) {
    if ((error as { code?: string })?.code !== "EADDRINUSE") throw error
    server = Bun.serve({ ...options, port: 0 })
  }
  return {
    redirectURI: `http://localhost:${server.port}/callback`,
    close: () => server.stop(false),
  }
}

export async function createClaudeOAuthResult(input: {
  method: "auto" | "code"
  state?: string
  expiresAt?: number
  signal?: AbortSignal
  fetch?: FetchLike
  listenerFactory?: ClaudeListenerFactory
  now?: () => number
}): Promise<ClaudeOAuthResult> {
  const pkce = createPKCE()
  if (input.state) pkce.state = input.state
  let callbackResolve: ((value: string) => void) | undefined
  const callbackWait = new Promise<string>((resolve) => { callbackResolve = resolve })
  let redirectURI: string | undefined = input.method === "code" ? CLAUDE_PROFILE.defaultRedirectURI : undefined
  const now = input.now ?? Date.now
  const expiresAt = Math.min(input.expiresAt ?? now() + 5 * 60 * 1000, now() + 5 * 60 * 1000)
  if (!Number.isFinite(expiresAt) || expiresAt <= now()) throw new Error("Claude OAuth flow has expired")
  const listener = input.method === "auto"
    ? await (input.listenerFactory ?? defaultListenerFactory)({
        preferredPort: 54545,
        onCallback: (value) => {
          try {
            const parsed = parseClaudeCallback(value, { redirectURI: listener?.redirectURI ?? "", state: pkce.state })
            callbackResolve?.(value)
            return parsed.error ? "denied" : true
          } catch {
            // Do not let a foreign, malformed, or wrong-state request consume the waiter.
            return false
          }
        },
      })
    : undefined
  redirectURI = listener?.redirectURI ?? redirectURI
  if (!redirectURI) throw new Error("Claude OAuth flow has no redirect URI")
  const controller = new AbortController()
  let disposed = false
  let callbackStarted = false
  let deadline: ReturnType<typeof setTimeout> | undefined
  let externalAbort: (() => void) | undefined
  const dispose = async () => {
    if (disposed) return
    disposed = true
    if (deadline) clearTimeout(deadline)
    if (input.signal && externalAbort) input.signal.removeEventListener("abort", externalAbort)
    controller.abort()
    callbackResolve?.("")
    await listener?.close()
  }
  deadline = setTimeout(() => { void dispose() }, Math.max(0, expiresAt - now()))
  if (input.signal) {
    if (input.signal.aborted) void dispose()
    else {
      externalAbort = () => { void dispose() }
      input.signal.addEventListener("abort", externalAbort, { once: true })
    }
  }
  const callback = async (manualCode?: string, callbackSignal?: AbortSignal) => {
    if (disposed || callbackStarted) return undefined
    callbackStarted = true
    let callbackAbort: (() => void) | undefined
    if (callbackSignal) {
      callbackAbort = () => {
        controller.abort(callbackSignal.reason)
        void dispose()
      }
      if (callbackSignal.aborted) callbackAbort()
      else callbackSignal.addEventListener("abort", callbackAbort, { once: true })
    }
    try {
      const raw = input.method === "auto" ? await callbackWait : manualCode
      if (!raw && disposed) return undefined
      if (!raw) throw new Error("callback code is missing")
      const parsed = parseClaudeCallback(raw, { redirectURI, state: pkce.state })
      if (parsed.error) throw new ClaudeOAuthError("denied")
      return await exchangeClaudeCode({ callback: parsed, pkce, redirectURI, fetch: input.fetch, signal: controller.signal, now: input.now, timeoutMs: Math.max(1, expiresAt - now()) })
    } finally {
      if (callbackSignal && callbackAbort) callbackSignal.removeEventListener("abort", callbackAbort)
      await dispose()
    }
  }
  return {
    url: buildClaudeAuthorizeURL({ pkce, redirectURI }),
    method: input.method,
    instructions: input.method === "auto"
      ? "Sign in in the browser. After approval, return to Open Clank."
      : "Paste the complete localhost callback URL or code#state into Open Clank.",
    callback,
    dispose,
  }
}
