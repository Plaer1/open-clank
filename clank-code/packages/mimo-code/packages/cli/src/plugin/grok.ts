import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import { Auth } from "@/auth"

const ISSUER = "https://auth.x.ai"
const DISCOVERY_URL = `${ISSUER}/.well-known/openid-configuration`
const DEVICE_URL = `${ISSUER}/oauth2/device/code`
const TOKEN_URL = `${ISSUER}/oauth2/token`
const CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
const SCOPE = "openid profile email offline_access grok-cli:access api:access"
const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
const MAX_OAUTH_JSON_BYTES = 32 * 1024
const FLOW_TIMEOUT_MS = 10 * 60 * 1000
const SLOW_DOWN_MS = 5_000

type OAuthMetadata = {
  issuer?: unknown
  token_endpoint?: unknown
  device_authorization_endpoint?: unknown
}

type DeviceReply = {
  device_code?: unknown
  user_code?: unknown
  verification_uri?: unknown
  verification_uri_complete?: unknown
  expires_in?: unknown
  interval?: unknown
}

type TokenReply = {
  access_token?: unknown
  refresh_token?: unknown
  expires_in?: unknown
  scope?: unknown
  error?: unknown
}

function validOAuthEndpoint(value: unknown, path: string): value is string {
  if (typeof value !== "string") return false
  try {
    const url = new URL(value)
    return url.origin === ISSUER && url.pathname === path && !url.username && !url.password && !url.port && !url.search && !url.hash
  } catch {
    return false
  }
}

async function boundedJSON(response: Response): Promise<unknown> {
  const declared = Number(response.headers.get("content-length"))
  if (Number.isFinite(declared) && declared > MAX_OAUTH_JSON_BYTES) throw new Error("OAuth response too large")
  if (!response.body) throw new Error("OAuth response body is missing")
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let size = 0
  let source = ""
  try {
    while (true) {
      const part = await reader.read()
      if (part.done) break
      size += part.value.byteLength
      if (size > MAX_OAUTH_JSON_BYTES) {
        void reader.cancel().catch(() => {})
        throw new Error("OAuth response too large")
      }
      source += decoder.decode(part.value, { stream: true })
    }
    source += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  return JSON.parse(source)
}

export async function resolveGrokOAuthMetadata(fetcher: typeof fetch = fetch): Promise<{
  tokenURL: string
  deviceURL: string
}> {
  const response = await fetcher(DISCOVERY_URL, {
    headers: { Accept: "application/json" },
    redirect: "error",
    signal: AbortSignal.timeout(5_000),
  })
  if (!response.ok) throw new Error("Grok OAuth discovery is unavailable")
  const value = (await boundedJSON(response)) as OAuthMetadata
  if (
    value.issuer !== ISSUER ||
    !validOAuthEndpoint(value.token_endpoint, "/oauth2/token") ||
    !validOAuthEndpoint(value.device_authorization_endpoint, "/oauth2/device/code")
  ) {
    throw new Error("Grok OAuth discovery does not match the pinned issuer contract")
  }
  return { tokenURL: value.token_endpoint, deviceURL: value.device_authorization_endpoint }
}

async function requestDeviceCode(fetcher: typeof fetch, urls: { tokenURL: string; deviceURL: string }, signal: AbortSignal) {
  const response = await fetcher(urls.deviceURL, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ client_id: CLIENT_ID, scope: SCOPE }),
    redirect: "error",
    signal,
  })
  if (!response.ok) throw new Error("Grok device authorization could not be started")
  const value = (await boundedJSON(response)) as DeviceReply
  if (
    typeof value.device_code !== "string" || value.device_code.length < 8 || value.device_code.length > 4096 ||
    typeof value.user_code !== "string" || !/^[A-Za-z0-9-]{4,64}$/.test(value.user_code) ||
    typeof value.verification_uri !== "string" || !validVerificationURL(value.verification_uri) ||
    (value.verification_uri_complete !== undefined &&
      (typeof value.verification_uri_complete !== "string" || !validVerificationURL(value.verification_uri_complete)))
  ) {
    throw new Error("Grok device authorization returned invalid instructions")
  }
  const expiresIn = positiveBounded(value.expires_in, 600, 1, 600)
  const intervalSeconds = positiveBounded(value.interval, 5, 1, 60)
  return {
    deviceCode: value.device_code,
    userCode: value.user_code,
    verificationURI: value.verification_uri,
    verificationURIComplete: value.verification_uri_complete as string | undefined,
    expiresAt: Date.now() + expiresIn * 1000,
    intervalMs: intervalSeconds * 1000,
  }
}

function validVerificationURL(value: string): boolean {
  try {
    const url = new URL(value)
    return url.protocol === "https:" && ["auth.x.ai", "grok.com"].includes(url.hostname) &&
      !url.username && !url.password && !url.port
  } catch {
    return false
  }
}

function positiveBounded(value: unknown, fallback: number, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value)) return fallback
  return Math.min(max, Math.max(min, value))
}

function sleep(ms: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false)
  return new Promise((resolve) => {
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort)
      resolve(true)
    }, ms)
    const abort = () => {
      clearTimeout(timer)
      signal.removeEventListener("abort", abort)
      resolve(false)
    }
    signal.addEventListener("abort", abort, { once: true })
  })
}

export async function pollGrokDeviceCode(
  fetcher: typeof fetch,
  tokenURL: string,
  device: Awaited<ReturnType<typeof requestDeviceCode>>,
  signal: AbortSignal,
  wait: typeof sleep = sleep,
  now: () => number = Date.now,
) {
  const deadline = Math.min(device.expiresAt, now() + FLOW_TIMEOUT_MS)
  let interval = device.intervalMs
  while (!signal.aborted && now() < deadline) {
    if (!(await wait(Math.min(interval, Math.max(0, deadline - now())), signal))) return undefined
    if (signal.aborted || now() >= deadline) break
    const remainingMs = Math.max(1, deadline - now())
    const requestSignal = AbortSignal.any([signal, AbortSignal.timeout(Math.min(15_000, remainingMs))])
    try {
      const response = await fetcher(tokenURL, {
        method: "POST",
        headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          grant_type: DEVICE_GRANT,
          device_code: device.deviceCode,
          client_id: CLIENT_ID,
        }),
        redirect: "error",
        signal: requestSignal,
      })
      const body = (await boundedJSON(response)) as TokenReply
      if (response.ok && typeof body.access_token === "string" && typeof body.refresh_token === "string") {
        if (typeof body.scope === "string") {
          const granted = new Set(body.scope.split(/\s+/).filter(Boolean))
          if (SCOPE.split(" ").some((scope) => !granted.has(scope))) {
            throw new Error("Grok OAuth grant did not include the requested scopes")
          }
        }
        const expiresIn = positiveBounded(body.expires_in, 3600, 1, 31_536_000)
        return {
          type: "success" as const,
          access: body.access_token,
          refresh: body.refresh_token,
          expires: now() + expiresIn * 1000,
        }
      }
      if (body.error === "authorization_pending") continue
      if (body.error === "slow_down") {
        interval = Math.min(interval + SLOW_DOWN_MS, 60_000)
        continue
      }
      if (body.error === "access_denied" || body.error === "expired_token") return undefined
      throw new Error("Grok OAuth token polling failed")
    } catch (error) {
      if (signal.aborted) return undefined
      if (now() >= deadline) return undefined
      throw error
    }
  }
  return undefined
}

export async function refreshGrokOAuthCredential(credential: Auth.Oauth): Promise<Auth.Oauth> {
  const urls = await resolveGrokOAuthMetadata()
  const response = await fetch(urls.tokenURL, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: credential.refresh, client_id: CLIENT_ID }),
    redirect: "error",
    signal: AbortSignal.timeout(10_000),
  })
  if (!response.ok) throw new Error("Grok OAuth refresh failed")
  const value = (await boundedJSON(response)) as TokenReply
  if (typeof value.access_token !== "string") throw new Error("Grok OAuth refresh returned no access token")
  return {
    ...credential,
    access: value.access_token,
    refresh: typeof value.refresh_token === "string" && value.refresh_token ? value.refresh_token : credential.refresh,
    expires: Date.now() + positiveBounded(value.expires_in, 3600, 1, 31_536_000) * 1000,
  }
}

export async function createGrokDeviceAuthorization(fetcher: typeof fetch = fetch, wait: typeof sleep = sleep) {
  const urls = await resolveGrokOAuthMetadata(fetcher)
  const controller = new AbortController()
  const device = await requestDeviceCode(
    fetcher,
    urls,
    AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]),
  )
  return {
    url: device.verificationURIComplete ?? device.verificationURI,
    instructions: `Enter code ${device.userCode} at ${device.verificationURI}`,
    userCode: device.userCode,
    method: "auto" as const,
    async callback(_code?: string, signal?: AbortSignal) {
      if (!signal) return { type: "failed" as const }
      if (signal.aborted) {
        controller.abort(signal.reason)
        return { type: "failed" as const }
      }
      const abort = () => controller.abort(signal.reason)
      signal.addEventListener("abort", abort, { once: true })
      try {
        return (await pollGrokDeviceCode(fetcher, urls.tokenURL, device, controller.signal, wait)) ?? { type: "failed" as const }
      } finally {
        signal.removeEventListener("abort", abort)
        controller.abort()
      }
    },
    dispose() {
      controller.abort()
    },
  }
}

export async function GrokAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "grok",
      methods: [
        {
          type: "oauth",
          acquisitionMethod: "device",
          credentialKinds: ["oauth"],
          billingLanes: ["subscription"],
          label: "Grok subscription device sign-in",
          authorize: async () => createGrokDeviceAuthorization(),
        },
      ],
    },
  }
}
