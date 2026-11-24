import { createHash, randomBytes } from "node:crypto"
import type { Hooks, PluginInput } from "@mimo-ai/plugin"

const AUTH_URL = "https://llmgateway.io/connect/cli"
const EXCHANGE_URL = "https://internal.llmgateway.io/public/cli-auth/token"
const MAX_RESPONSE_BYTES = 16 * 1024
const EXCHANGE_TIMEOUT_MS = 15_000

function loopbackCallback(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false
  try {
    const url = new URL(value)
    return url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname) && !url.username && !url.password && !url.hash
  } catch {
    return false
  }
}

async function boundedJSON(response: Response): Promise<unknown> {
  const length = Number(response.headers.get("content-length"))
  if (Number.isFinite(length) && length > MAX_RESPONSE_BYTES) {
    void response.body?.cancel().catch(() => {})
    throw new Error("LLM Gateway key response exceeded its limit")
  }
  if (!response.body) throw new Error("LLM Gateway key response is missing")
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let size = 0
  let body = ""
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      size += chunk.value.byteLength
      if (size > MAX_RESPONSE_BYTES) {
        void reader.cancel().catch(() => {})
        throw new Error("LLM Gateway key response exceeded its limit")
      }
      body += decoder.decode(chunk.value, { stream: true })
    }
    body += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  return JSON.parse(body)
}

function validKey(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 131_072 && !/[\u0000-\u001f\u007f]/.test(value)
}

function validExpiry(value: unknown): value is string | null {
  return value === null || (typeof value === "string" && value.length <= 128 && Number.isFinite(Date.parse(value)))
}

function createMethod(org: "default" | "devpass") {
  const credentialKinds: ("api" | "oauth")[] = ["api"]
  const billingLanes: ("custom" | "local" | "metered_api" | "subscription")[] = org === "devpass" ? ["subscription"] : ["metered_api"]
  return {
    type: "oauth" as const,
    acquisitionMethod: "browser" as const,
    credentialKinds,
    billingLanes,
    label: org === "devpass" ? "Connect DevPass" : "Create Gateway key (choose project)",
    async authorize(inputs: Record<string, string> = {}) {
      const redirectURI = inputs["__openclank_redirect_uri"]
      const state = inputs["__openclank_state"]
      if (!loopbackCallback(redirectURI) || typeof state !== "string" || state.length < 16 || state.length > 256) {
        throw new Error("LLM Gateway sign-in requires a valid managed callback and state")
      }
      const verifier = randomBytes(32).toString("base64url")
      const challenge = createHash("sha256").update(verifier).digest("base64url")
      const url = new URL(AUTH_URL)
      url.searchParams.set("callback_url", redirectURI)
      url.searchParams.set("state", state)
      url.searchParams.set("code_challenge", challenge)
      url.searchParams.set("code_challenge_method", "S256")
      url.searchParams.set("source", "mimocode")
      url.searchParams.set("name", "Mimo Code")
      url.searchParams.set("org", org)
      return {
        url: url.toString(),
        method: "auto" as const,
        instructions: org === "devpass"
          ? "Connect the LLM Gateway DevPass project. Reconnecting may rotate its single existing API key and affect other uses of that key."
          : "Connect LLM Gateway and choose the organization and project. This creates a new Gateway key billed to that credits project.",
        async callback(code?: string, signal?: AbortSignal) {
          if (!signal || signal.aborted || typeof code !== "string" || code.length < 1 || code.length > 4096 || /[\u0000-\u001f\u007f]/.test(code)) {
            return { type: "failed" as const }
          }
          try {
            const response = await fetch(EXCHANGE_URL, {
              method: "POST",
              headers: { Accept: "application/json", "Content-Type": "application/json" },
              body: JSON.stringify({ code, code_verifier: verifier }),
              redirect: "error",
              signal: AbortSignal.any([signal, AbortSignal.timeout(EXCHANGE_TIMEOUT_MS)]),
            })
            if (!response.ok) return { type: "failed" as const }
            const result = await boundedJSON(response) as { api_key?: unknown; expires_at?: unknown }
            if (!validKey(result.api_key) || !validExpiry(result.expires_at)) return { type: "failed" as const }
            return {
              type: "success" as const,
              key: result.api_key,
              ...(typeof result.expires_at === "string" ? { metadata: { expires_at: result.expires_at } } : {}),
            }
          } catch {
            // The exchange code is single-use, including on failed attempts.
            return { type: "failed" as const }
          }
        },
      }
    },
  }
}

export async function LLMGatewayBrowserAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "llmgateway",
      methods: [createMethod("default")],
    },
  }
}

export async function LLMGatewayDevPassAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "llmgateway-devpass",
      methods: [createMethod("devpass")],
    },
  }
}
