import { randomUUID } from "node:crypto"
import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import { Auth } from "@/auth"
import { InstallationVersion } from "@/installation/version"

const CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
const DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
const FLOW_TIMEOUT_MS = 10 * 60 * 1000
const REQUEST_TIMEOUT_MS = 15_000
const MAX_BODY_BYTES = 32 * 1024
const SLOW_DOWN_MS = 5_000

type Region = "cn" | "global"
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
  error?: unknown
  error_description?: unknown
}
type CredentialMetadata = {
  device_id: string
  kimi_region: Region
  credential_profile: "kimi-code-device-v1"
}

function profile(region: Region) {
  return region === "cn"
    ? { region, oauthHost: "https://auth.kimi.com", apiBase: "https://api.kimi.com/coding/v1" }
    : { region, oauthHost: "https://auth.kimi.ai", apiBase: "https://api.kimi.ai/coding/v1" }
}

function identityHeaders(deviceID: string) {
  return {
    Accept: "application/json",
    "Content-Type": "application/x-www-form-urlencoded",
    "User-Agent": `openclank/${InstallationVersion}`,
    "X-Msh-Platform": "openclank",
    "X-Msh-Version": String(InstallationVersion),
    "X-Msh-Device-Id": deviceID,
  }
}

function boundedInt(value: unknown, fallback: number, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isInteger(value)) return fallback
  return Math.min(max, Math.max(min, value))
}

function positiveSeconds(value: unknown, fallback: number): number {
  if (value === undefined) return fallback
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) throw new Error("Kimi returned an invalid device-code expiry")
  return Math.min(FLOW_TIMEOUT_MS / 1000, Math.ceil(value))
}

async function readJSON(response: Response): Promise<unknown> {
  const declared = Number(response.headers.get("content-length"))
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) throw new Error("Kimi OAuth response exceeded its size limit")
  if (!response.body) throw new Error("Kimi OAuth response body is missing")
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let bytes = 0
  let text = ""
  try {
    while (true) {
      const part = await reader.read()
      if (part.done) break
      bytes += part.value.byteLength
      if (bytes > MAX_BODY_BYTES) {
        void reader.cancel().catch(() => {})
        throw new Error("Kimi OAuth response exceeded its size limit")
      }
      text += decoder.decode(part.value, { stream: true })
    }
    text += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  return JSON.parse(text)
}

function validVerificationURL(value: unknown, oauthHost: string): value is string {
  if (typeof value !== "string" || value.length > 2048) return false
  try {
    const url = new URL(value)
    if (url.origin !== oauthHost || url.pathname !== "/verify" || url.username || url.password || url.port || url.hash) return false
    for (const key of url.searchParams.keys()) if (key !== "user_code") return false
    return true
  } catch {
    return false
  }
}

function requestSignal(signal?: AbortSignal, timeoutMs = REQUEST_TIMEOUT_MS): AbortSignal {
  return signal ? AbortSignal.any([signal, AbortSignal.timeout(timeoutMs)]) : AbortSignal.timeout(timeoutMs)
}

async function postForm(url: string, body: Record<string, string>, headers: HeadersInit, signal?: AbortSignal): Promise<Response> {
  return fetch(url, {
    method: "POST",
    headers,
    body: new URLSearchParams(body),
    redirect: "error",
    signal: requestSignal(signal),
  })
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

async function requestDeviceCode(region: Region, metadata: CredentialMetadata, signal: AbortSignal) {
  const config = profile(region)
  const response = await postForm(
    `${config.oauthHost}/api/oauth/device_authorization`,
    { client_id: CLIENT_ID },
    identityHeaders(metadata.device_id),
    signal,
  )
  if (!response.ok) throw new Error(`Kimi device authorization failed (${response.status})`)
  const value = (await readJSON(response)) as DeviceReply
  const complete = value.verification_uri_complete
  const ordinary = value.verification_uri
  const verification = validVerificationURL(ordinary, config.oauthHost)
    ? ordinary
    : validVerificationURL(complete, config.oauthHost)
      ? (() => {
          const url = new URL(complete)
          url.searchParams.delete("user_code")
          return url.toString()
        })()
      : undefined
  if (
    typeof value.device_code !== "string" || value.device_code.length < 8 || value.device_code.length > 4096 ||
    typeof value.user_code !== "string" || !/^[A-Za-z0-9-]{4,64}$/.test(value.user_code) ||
    !verification ||
    (ordinary !== undefined && !validVerificationURL(ordinary, config.oauthHost) && !validVerificationURL(complete, config.oauthHost)) ||
    (complete !== undefined && !validVerificationURL(complete, config.oauthHost))
  ) throw new Error("Kimi returned invalid device authorization instructions")
  return {
    deviceCode: value.device_code,
    userCode: value.user_code,
    verificationURI: verification,
    verificationURIComplete: validVerificationURL(complete, config.oauthHost) ? complete : verification,
    deadline: Date.now() + positiveSeconds(value.expires_in, 600) * 1000,
    interval: boundedInt(value.interval, 5, 1, 600) * 1000,
  }
}

async function pollDeviceCode(region: Region, metadata: CredentialMetadata, device: Awaited<ReturnType<typeof requestDeviceCode>>, signal: AbortSignal) {
  const config = profile(region)
  let interval = device.interval
  while (!signal.aborted && Date.now() < device.deadline) {
    if (!(await sleep(Math.min(interval, Math.max(0, device.deadline - Date.now())), signal))) return undefined
    if (Date.now() >= device.deadline || signal.aborted) return undefined
    const remaining = device.deadline - Date.now()
    const request = requestSignal(signal, Math.min(REQUEST_TIMEOUT_MS, remaining))
    const response = await postForm(
      `${config.oauthHost}/api/oauth/token`,
      { client_id: CLIENT_ID, device_code: device.deviceCode, grant_type: DEVICE_GRANT },
      identityHeaders(metadata.device_id),
      request,
    )
    const value = (await readJSON(response)) as TokenReply
    if (response.ok && typeof value.access_token === "string" && value.access_token && typeof value.refresh_token === "string" && value.refresh_token) {
      return {
        type: "success" as const,
        access: value.access_token,
        refresh: value.refresh_token,
        expires: Date.now() + boundedInt(value.expires_in, 3600, 1, 31_536_000) * 1000,
        credentialMetadata: metadata,
      }
    }
    if (value.error === "authorization_pending") continue
    if (value.error === "slow_down") {
      interval += SLOW_DOWN_MS
      continue
    }
    if (value.error === "expired_token" || value.error === "access_denied") return undefined
    if (!response.ok) throw new Error(`Kimi device token polling failed (${response.status})`)
    throw new Error("Kimi device token response did not contain a credential")
  }
  return undefined
}

export async function refreshKimiCodeOAuthCredential(credential: Auth.Oauth): Promise<Auth.Oauth> {
  const metadata = credential.metadata
  if (!metadata || metadata.credential_profile !== "kimi-code-device-v1") throw new Error("Kimi device credential metadata is missing")
  const config = profile(metadata.kimi_region)
  const response = await postForm(
    `${config.oauthHost}/api/oauth/token`,
    { client_id: CLIENT_ID, grant_type: "refresh_token", refresh_token: credential.refresh },
    identityHeaders(metadata.device_id),
  )
  if (!response.ok) throw new Error(`Kimi OAuth refresh failed (${response.status})`)
  const value = (await readJSON(response)) as TokenReply
  if (typeof value.access_token !== "string" || !value.access_token) throw new Error("Kimi OAuth refresh returned no access token")
  return {
    ...credential,
    access: value.access_token,
    refresh: typeof value.refresh_token === "string" && value.refresh_token ? value.refresh_token : credential.refresh,
    expires: Date.now() + boundedInt(value.expires_in, 3600, 1, 31_536_000) * 1000,
  }
}

async function createAuthorization(region: Region) {
  const deviceID = randomUUID()
  const metadata: CredentialMetadata = { device_id: deviceID, kimi_region: region, credential_profile: "kimi-code-device-v1" }
  const controller = new AbortController()
  const timeout = AbortSignal.timeout(FLOW_TIMEOUT_MS)
  const flowSignal = AbortSignal.any([controller.signal, timeout])
  const device = await requestDeviceCode(region, metadata, flowSignal)
  return {
    url: device.verificationURIComplete,
    instructions: `Open ${device.verificationURI} and enter code ${device.userCode}`,
    userCode: device.userCode,
    method: "auto" as const,
    async callback(_code?: string, signal?: AbortSignal) {
      if (!signal || signal.aborted) return { type: "failed" as const }
      const abort = () => controller.abort(signal.reason)
      signal.addEventListener("abort", abort, { once: true })
      try {
        return (await pollDeviceCode(region, metadata, device, flowSignal)) ?? { type: "failed" as const }
      } catch {
        return { type: "failed" as const }
      } finally {
        signal.removeEventListener("abort", abort)
        controller.abort()
      }
    },
    dispose() { controller.abort() },
  }
}

async function plugin(region: Region): Promise<Hooks> {
  const provider = region === "cn" ? "kimi-code-plan-cn" : "kimi-code-plan-global"
  return {
    auth: {
      provider,
      methods: [{
        type: "oauth",
        acquisitionMethod: "device",
        credentialKinds: ["oauth"],
        billingLanes: ["subscription"],
        label: `Kimi Code ${region === "cn" ? "China" : "Global"} device sign-in`,
        authorize: async () => createAuthorization(region),
      }],
    },
  }
}

export async function KimiCodeChinaAuthPlugin(_input: PluginInput): Promise<Hooks> { return plugin("cn") }
export async function KimiCodeGlobalAuthPlugin(_input: PluginInput): Promise<Hooks> { return plugin("global") }
