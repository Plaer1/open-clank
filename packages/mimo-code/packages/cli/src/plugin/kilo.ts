import type { Hooks, PluginInput } from "@mimo-ai/plugin"

const KILO_API = "https://api.kilo.ai"
const VERIFY_ORIGIN = "https://kilo.ai"
const POLL_INTERVAL_MS = 3_000
const FLOW_TIMEOUT_MS = 10 * 60 * 1_000
const REQUEST_TIMEOUT_MS = 15_000
const MAX_RESPONSE_BYTES = 32 * 1024

type DeviceStart = { code?: unknown; verificationUrl?: unknown; expiresIn?: unknown }
type DevicePoll = { status?: unknown; token?: unknown; userEmail?: unknown }

function positiveSeconds(value: unknown): number | undefined {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) return undefined
  return Math.min(FLOW_TIMEOUT_MS / 1_000, Math.ceil(value))
}

function validDeviceCode(value: unknown): value is string {
  return typeof value === "string" && value.length >= 4 && value.length <= 128 && /^[A-Za-z0-9-]+$/.test(value)
}

function validVerificationURL(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false
  try {
    const url = new URL(value)
    return url.origin === VERIFY_ORIGIN && url.pathname === "/auth/device" && !url.username && !url.password && !url.port && !url.hash
  } catch {
    return false
  }
}

async function boundedJSON(response: Response): Promise<unknown> {
  const contentLength = Number(response.headers.get("content-length"))
  if (Number.isFinite(contentLength) && contentLength > MAX_RESPONSE_BYTES) {
    void response.body?.cancel().catch(() => {})
    throw new Error("Kilo device authorization response exceeded its limit")
  }
  if (!response.body) throw new Error("Kilo device authorization response is missing")
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let size = 0
  let text = ""
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      size += chunk.value.byteLength
      if (size > MAX_RESPONSE_BYTES) {
        void reader.cancel().catch(() => {})
        throw new Error("Kilo device authorization response exceeded its limit")
      }
      text += decoder.decode(chunk.value, { stream: true })
    }
    text += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  return JSON.parse(text)
}

function requestSignal(signal: AbortSignal, deadline: number): AbortSignal {
  return AbortSignal.any([signal, AbortSignal.timeout(Math.max(1, Math.min(REQUEST_TIMEOUT_MS, deadline - Date.now())))])
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
    if (signal.aborted) abort()
  })
}

async function startDeviceAuth(signal: AbortSignal): Promise<{ code: string; verificationUrl: string; deadline: number }> {
  const deadline = Date.now() + FLOW_TIMEOUT_MS
  const response = await fetch(`${KILO_API}/api/device-auth/codes`, {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    redirect: "error",
    signal: requestSignal(signal, deadline),
  })
  if (response.status === 429) throw new Error("Kilo device sign-in is rate limited; try again later")
  if (!response.ok) throw new Error(`Kilo device sign-in could not start (${response.status})`)
  const value = await boundedJSON(response) as DeviceStart
  const lifetime = positiveSeconds(value.expiresIn)
  if (!validDeviceCode(value.code) || !validVerificationURL(value.verificationUrl) || lifetime === undefined) {
    throw new Error("Kilo returned invalid device sign-in instructions")
  }
  return { code: value.code, verificationUrl: value.verificationUrl, deadline: Math.min(deadline, Date.now() + lifetime * 1_000) }
}

async function pollDeviceAuth(code: string, deadline: number, signal: AbortSignal): Promise<string | undefined> {
  while (!signal.aborted && Date.now() < deadline) {
    const response = await fetch(`${KILO_API}/api/device-auth/codes/${encodeURIComponent(code)}`, {
      headers: { Accept: "application/json" },
      redirect: "error",
      signal: requestSignal(signal, deadline),
    })
    if (response.status === 202) {
      if (!(await sleep(Math.min(POLL_INTERVAL_MS, Math.max(0, deadline - Date.now())), signal))) return undefined
      continue
    }
    if (response.status === 403 || response.status === 410) return undefined
    if (!response.ok) throw new Error(`Kilo device sign-in polling failed (${response.status})`)
    const value = await boundedJSON(response) as DevicePoll
    if (value.status !== "approved" || typeof value.token !== "string" || value.token.length < 11 || value.token.length > 131_072) {
      if (value.status === "denied" || value.status === "expired") return undefined
      throw new Error("Kilo device sign-in returned an invalid approval")
    }
    return value.token
  }
  return undefined
}

export async function KiloDeviceAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "kilo",
      methods: [{
        type: "oauth",
        acquisitionMethod: "device",
        credentialKinds: ["api"],
        billingLanes: ["metered_api"],
        label: "Kilo Gateway device sign-in",
        async authorize() {
          const flow = new AbortController()
          const timeout = AbortSignal.timeout(FLOW_TIMEOUT_MS)
          const flowSignal = AbortSignal.any([flow.signal, timeout])
          const device = await startDeviceAuth(flowSignal)
          return {
            url: device.verificationUrl,
            userCode: device.code,
            method: "auto" as const,
            instructions: `Open ${device.verificationUrl} to approve Kilo Gateway access.`,
            async callback(_code?: string, signal?: AbortSignal) {
              if (!signal || signal.aborted || flowSignal.aborted) return { type: "failed" as const }
              const active = AbortSignal.any([signal, flowSignal, AbortSignal.timeout(Math.max(1, device.deadline - Date.now()))])
              try {
                const key = await pollDeviceAuth(device.code, device.deadline, active)
                return key ? { type: "success" as const, key } : { type: "failed" as const }
              } catch {
                return { type: "failed" as const }
              } finally {
                flow.abort()
              }
            },
            dispose() { flow.abort() },
          }
        },
      }],
    },
  }
}
