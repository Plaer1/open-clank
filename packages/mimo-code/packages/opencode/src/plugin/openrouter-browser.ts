import { createHash, randomBytes } from "node:crypto"
import type { Hooks, PluginInput } from "@mimo-ai/plugin"

const AUTH_URL = "https://openrouter.ai/auth"
const EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
const MAX_RESPONSE_BYTES = 16 * 1024
const FLOW_TIMEOUT_MS = 10 * 60 * 1000
const EXCHANGE_TIMEOUT_MS = 15_000

function loopbackCallback(value: unknown): value is string {
  if (typeof value !== "string" || value.length > 2048) return false
  try {
    const url = new URL(value)
    return url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname) &&
      !url.username && !url.password && !url.hash
  } catch {
    return false
  }
}

async function boundedJSON(response: Response): Promise<unknown> {
  const contentLength = Number(response.headers.get("content-length"))
  if (Number.isFinite(contentLength) && contentLength > MAX_RESPONSE_BYTES) {
    void response.body?.cancel().catch(() => {})
    throw new Error("OpenRouter key exchange response exceeded its limit")
  }
  if (!response.body) throw new Error("OpenRouter key exchange response is missing")
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
        throw new Error("OpenRouter key exchange response exceeded its limit")
      }
      text += decoder.decode(chunk.value, { stream: true })
    }
    text += decoder.decode()
  } finally {
    reader.releaseLock()
  }
  return JSON.parse(text)
}

export async function OpenRouterBrowserAuthPlugin(_input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "openrouter",
      methods: [{
        type: "oauth",
        acquisitionMethod: "browser",
        credentialKinds: ["api"],
        billingLanes: ["metered_api"],
        label: "Sign in with OpenRouter",
        async authorize(inputs = {}) {
          const redirectURI = inputs["__openclank_redirect_uri"]
          const state = inputs["__openclank_state"]
          if (!loopbackCallback(redirectURI) || typeof state !== "string" || state.length < 16 || state.length > 256) {
            throw new Error("OpenRouter sign-in requires a valid managed callback and state")
          }
          const verifier = randomBytes(32).toString("base64url")
          const challenge = createHash("sha256").update(verifier).digest("base64url")
          const url = new URL(AUTH_URL)
          url.searchParams.set("callback_url", redirectURI)
          url.searchParams.set("code_challenge", challenge)
          url.searchParams.set("code_challenge_method", "S256")
          url.searchParams.set("state", state)
          url.searchParams.set("key_label", "Open Clank")
          const flowSignal = AbortSignal.timeout(FLOW_TIMEOUT_MS)
          return {
            url: url.toString(),
            method: "auto" as const,
            instructions: "Sign in to OpenRouter in your browser and approve the Open Clank API key.",
            async callback(code?: string, signal?: AbortSignal) {
              if (!signal || signal.aborted || flowSignal.aborted || typeof code !== "string" || code.length < 1 || code.length > 4096 || /[\u0000-\u001f\u007f]/.test(code)) {
                return { type: "failed" as const }
              }
              const requestSignal = AbortSignal.any([signal, flowSignal, AbortSignal.timeout(EXCHANGE_TIMEOUT_MS)])
              try {
                const response = await fetch(EXCHANGE_URL, {
                  method: "POST",
                  headers: { Accept: "application/json", "Content-Type": "application/json" },
                  body: JSON.stringify({ code, code_verifier: verifier, code_challenge_method: "S256" }),
                  redirect: "error",
                  signal: requestSignal,
                })
                if (!response.ok) return { type: "failed" as const }
                const value = await boundedJSON(response) as { key?: unknown }
                if (typeof value.key !== "string" || value.key.length < 1 || value.key.length > 131_072) {
                  return { type: "failed" as const }
                }
                return { type: "success" as const, key: value.key }
              } catch {
                return { type: "failed" as const }
              }
            },
          }
        },
      }],
    },
  }
}
