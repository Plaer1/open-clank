import type { Hooks, PluginInput } from "@mimo-ai/plugin"
import { Log } from "../util"
import { Installation } from "../installation"
import { InstallationVersion } from "../installation/version"
import { Auth, OAUTH_DUMMY_KEY } from "../auth"
import os from "os"
import { setTimeout as sleep } from "node:timers/promises"

const log = Log.create({ service: "plugin.codex" })

const CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
const ISSUER = "https://auth.openai.com"
const CODEX_API_BASE = "https://chatgpt.com/backend-api/codex"
const CODEX_API_ENDPOINT = `${CODEX_API_BASE}/responses`
const CODEX_MODELS_ENDPOINT = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
const OAUTH_POLLING_SAFETY_MARGIN_MS = 3000
// Hard prompt capacity of the ChatGPT Codex backend for gpt-* models, lower than what
// models.dev reports for the raw OpenAI API. OpenAI's Codex model registry declares
// context_window = max_context_window = 372000 for the gpt-5.6 variants
// (openai/codex#31860 quotes the served catalog), and a direct Codex request with
// 350,317 input tokens completes (can1357/oh-my-pi#5705), so 372K is capacity rather
// than a billing boundary.
//
// Not to be confused with 272K: OpenAI prices prompts above 272K input at 2x input /
// 1.5x output for the whole request, and Codex's bundled metadata was lowered to 272000
// (openai/codex#33972) to keep default sessions under that line. That is a spending
// policy, not a capacity limit, so it belongs in `compaction.max_context` — see the
// Compaction section of the config docs.
//
// Applied as a clamp, so models whose real window is already smaller keep it.
const CODEX_GPT_CONTEXT_CAP = 372_000

type CodexModelCatalogEntry = {
  slug?: unknown
  visibility?: unknown
}

function usesCodexBackend(
  model: { providerID: string; api: { npm: string } },
  provider: { options?: Record<string, unknown> },
): boolean {
  if (model.providerID === "openai") return true
  const baseURL = provider.options?.baseURL
  return (
    model.api.npm === "@ai-sdk/openai" &&
    typeof baseURL === "string" &&
    baseURL.replace(/\/+$/, "") === CODEX_API_BASE
  )
}

/**
 * Return the account-entitled model slugs from the live Codex catalog.
 * There is intentionally no source-code model allowlist here: the account
 * endpoint is the authority for both newly-added models and plan visibility.
 */
export async function fetchCodexModelCatalog(
  accessToken: string,
  accountId?: string,
): Promise<Set<string> | undefined> {
  if (!accessToken) return undefined
  try {
    const headers = new Headers({
      Accept: "application/json",
      Authorization: `Bearer ${accessToken}`,
      Origin: "https://chatgpt.com",
      Referer: "https://chatgpt.com/codex",
      "User-Agent": `mimo-code/${InstallationVersion}`,
    })
    if (accountId) headers.set("ChatGPT-Account-Id", accountId)
    const response = await fetch(CODEX_MODELS_ENDPOINT, { headers })
    if (!response.ok) {
      log.warn("codex model catalog request failed", { status: response.status })
      return undefined
    }
    const payload = (await response.json()) as { models?: unknown }
    if (!Array.isArray(payload.models)) return undefined
    const models = new Set<string>()
    for (const entry of payload.models as CodexModelCatalogEntry[]) {
      if (typeof entry?.slug !== "string" || !entry.slug.trim()) continue
      const visibility = typeof entry.visibility === "string" ? entry.visibility.trim().toLowerCase() : ""
      if (visibility === "hide" || visibility === "hidden") continue
      models.add(entry.slug.trim())
    }
    return models
  } catch (error) {
    log.warn("codex model catalog request failed", { error: String(error) })
    return undefined
  }
}

export function modelMatchesCodexEntitlement(
  modelId: string,
  apiId: string,
  entitledModels: Set<string>,
): boolean {
  const candidates = new Set([modelId, apiId, modelId.split("/").pop() ?? "", apiId.split("/").pop() ?? ""])
  return [...candidates].some((candidate) => candidate.length > 0 && entitledModels.has(candidate))
}

export interface IdTokenClaims {
  chatgpt_account_id?: string
  organizations?: Array<{ id: string }>
  email?: string
  "https://api.openai.com/auth"?: {
    chatgpt_account_id?: string
  }
}

export function parseJwtClaims(token: string): IdTokenClaims | undefined {
  const parts = token.split(".")
  if (parts.length !== 3) return undefined
  try {
    return JSON.parse(Buffer.from(parts[1], "base64url").toString())
  } catch {
    return undefined
  }
}

export function extractAccountIdFromClaims(claims: IdTokenClaims): string | undefined {
  return (
    claims.chatgpt_account_id ||
    claims["https://api.openai.com/auth"]?.chatgpt_account_id ||
    claims.organizations?.[0]?.id
  )
}

export function extractAccountId(tokens: TokenResponse): string | undefined {
  if (tokens.id_token) {
    const claims = parseJwtClaims(tokens.id_token)
    const accountId = claims && extractAccountIdFromClaims(claims)
    if (accountId) return accountId
  }
  if (tokens.access_token) {
    const claims = parseJwtClaims(tokens.access_token)
    return claims ? extractAccountIdFromClaims(claims) : undefined
  }
  return undefined
}

interface TokenResponse {
  id_token: string
  access_token: string
  refresh_token: string
  expires_in?: number
}

async function refreshAccessToken(refreshToken: string, signal?: AbortSignal | null): Promise<TokenResponse> {
  const response = await fetch(`${ISSUER}/oauth/token`, {
    method: "POST",
    signal,
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "refresh_token",
      refresh_token: refreshToken,
      client_id: CLIENT_ID,
    }).toString(),
  })
  if (!response.ok) {
    throw new Error(`Token refresh failed: ${response.status}`)
  }
  return response.json()
}

/** Managed Open Clank refresh exchange. Persistence and concurrency fencing
 * are deliberately owned by the ACP host lease, not this adapter. */
export async function refreshCodexOAuthCredential(
  credential: Auth.Oauth,
  signal?: AbortSignal | null,
): Promise<Auth.Oauth> {
  const tokens = await refreshAccessToken(credential.refresh, signal)
  return {
    ...credential,
    access: tokens.access_token,
    refresh: tokens.refresh_token || credential.refresh,
    expires: Date.now() + (tokens.expires_in ?? 3600) * 1000,
    accountId: extractAccountId(tokens) ?? credential.accountId,
  }
}

export async function CodexAuthPlugin(input: PluginInput): Promise<Hooks> {
  return {
    auth: {
      provider: "openai",
      async loader(getAuth, provider) {
        const auth = await getAuth()
        if (auth.type !== "oauth") return {}

        // The account's live Codex catalog is the entitlement authority.
        // Never replace it with a source-code allowlist or a model-name
        // substring escape hatch: both hide new entitled models and expose
        // API-only models unpredictably.
        const authWithAccount = auth as typeof auth & { accountId?: string }
        const entitledModels = await fetchCodexModelCatalog(auth.access, authWithAccount.accountId)
        for (const [modelId, model] of Object.entries(provider.models)) {
          if (!entitledModels || !modelMatchesCodexEntitlement(modelId, model.api.id, entitledModels)) {
            delete provider.models[modelId]
          }
        }

        // Zero out costs for Codex (included with ChatGPT subscription)
        for (const [modelID, model] of Object.entries(provider.models)) {
          model.cost = {
            input: 0,
            output: 0,
            cache: { read: 0, write: 0 },
          }
          // The Codex backend accepts a smaller prompt than the raw OpenAI API for
          // gpt-* models. Clamp, never raise: models whose real window is already
          // below the cap (gpt-4o at 128K) must keep it, and limit.context === 0 is
          // the sentinel that disables overflow handling entirely.
          // limit.input is what Overflow.usable() reads when present, so it must be
          // clamped too — but only when the catalog already publishes it. Introducing
          // one would switch usable() to the input branch and drop the output reserve.
          // The v1 SDK model type predates limit.input; the runtime object carries it.
          const limit = model.limit as { context: number; output: number; input?: number }
          if (modelID.startsWith("gpt-") && limit.context > 0) {
            limit.context = Math.min(limit.context, CODEX_GPT_CONTEXT_CAP)
            if (limit.input) limit.input = Math.min(limit.input, CODEX_GPT_CONTEXT_CAP)
          }
        }

        return {
          apiKey: OAUTH_DUMMY_KEY,
          async fetch(requestInput: RequestInfo | URL, init?: RequestInit) {
            // Remove dummy API key authorization header
            if (init?.headers) {
              if (init.headers instanceof Headers) {
                init.headers.delete("authorization")
                init.headers.delete("Authorization")
              } else if (Array.isArray(init.headers)) {
                init.headers = init.headers.filter(([key]) => key.toLowerCase() !== "authorization")
              } else {
                delete init.headers["authorization"]
                delete init.headers["Authorization"]
              }
            }

            const currentAuth = await getAuth()
            if (currentAuth.type !== "oauth") return fetch(requestInput, init)

            // Cast to include accountId field
            const authWithAccount = currentAuth as typeof currentAuth & { accountId?: string }

            // Check if token needs refresh
            if (!currentAuth.access || currentAuth.expires < Date.now()) {
              log.info("refreshing codex access token")
              const tokens = await refreshAccessToken(currentAuth.refresh, init?.signal)
              const newAccountId = extractAccountId(tokens) || authWithAccount.accountId
              await input.client.auth.set({
                path: { id: "openai" },
                body: {
                  type: "oauth",
                  refresh: tokens.refresh_token,
                  access: tokens.access_token,
                  expires: Date.now() + (tokens.expires_in ?? 3600) * 1000,
                  ...(newAccountId && { accountId: newAccountId }),
                },
              })
              currentAuth.access = tokens.access_token
              authWithAccount.accountId = newAccountId
            }

            // Build headers
            const headers = new Headers()
            if (init?.headers) {
              if (init.headers instanceof Headers) {
                init.headers.forEach((value, key) => headers.set(key, value))
              } else if (Array.isArray(init.headers)) {
                for (const [key, value] of init.headers) {
                  if (value !== undefined) headers.set(key, String(value))
                }
              } else {
                for (const [key, value] of Object.entries(init.headers)) {
                  if (value !== undefined) headers.set(key, String(value))
                }
              }
            }

            // Set authorization header with access token
            headers.set("authorization", `Bearer ${currentAuth.access}`)

            // Set ChatGPT-Account-Id header for organization subscriptions
            if (authWithAccount.accountId) {
              headers.set("ChatGPT-Account-Id", authWithAccount.accountId)
            }

            // Rewrite URL to Codex endpoint
            const parsed =
              requestInput instanceof URL
                ? requestInput
                : new URL(typeof requestInput === "string" ? requestInput : requestInput.url)
            const url =
              parsed.pathname.includes("/v1/responses") || parsed.pathname.includes("/chat/completions")
                ? new URL(CODEX_API_ENDPOINT)
                : parsed

            return fetch(url, {
              ...init,
              headers,
            })
          },
        }
      },
      methods: [
        {
          label: "ChatGPT Pro/Plus (browser device login)",
          type: "oauth",
          authorize: async () => {
            const deviceResponse = await fetch(`${ISSUER}/api/accounts/deviceauth/usercode`, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "User-Agent": `opencode/${InstallationVersion}`,
              },
              body: JSON.stringify({ client_id: CLIENT_ID }),
            })

            if (!deviceResponse.ok) throw new Error("Failed to initiate device authorization")

            const deviceData = (await deviceResponse.json()) as {
              device_auth_id: string
              user_code: string
              interval: string
            }
            const interval = Math.max(parseInt(deviceData.interval) || 5, 1) * 1000

            return {
              url: `${ISSUER}/codex/device`,
              instructions: `Enter code: ${deviceData.user_code}`,
              method: "auto" as const,
              async callback() {
                while (true) {
                  const response = await fetch(`${ISSUER}/api/accounts/deviceauth/token`, {
                    method: "POST",
                    headers: {
                      "Content-Type": "application/json",
                      "User-Agent": `opencode/${InstallationVersion}`,
                    },
                    body: JSON.stringify({
                      device_auth_id: deviceData.device_auth_id,
                      user_code: deviceData.user_code,
                    }),
                  })

                  if (response.ok) {
                    const data = (await response.json()) as {
                      authorization_code: string
                      code_verifier: string
                    }

                    const tokenResponse = await fetch(`${ISSUER}/oauth/token`, {
                      method: "POST",
                      headers: { "Content-Type": "application/x-www-form-urlencoded" },
                      body: new URLSearchParams({
                        grant_type: "authorization_code",
                        code: data.authorization_code,
                        redirect_uri: `${ISSUER}/deviceauth/callback`,
                        client_id: CLIENT_ID,
                        code_verifier: data.code_verifier,
                      }).toString(),
                    })

                    if (!tokenResponse.ok) {
                      throw new Error(`Token exchange failed: ${tokenResponse.status}`)
                    }

                    const tokens: TokenResponse = await tokenResponse.json()

                    return {
                      type: "success" as const,
                      refresh: tokens.refresh_token,
                      access: tokens.access_token,
                      expires: Date.now() + (tokens.expires_in ?? 3600) * 1000,
                      accountId: extractAccountId(tokens),
                    }
                  }

                  if (response.status !== 403 && response.status !== 404) {
                    return { type: "failed" as const }
                  }

                  await sleep(interval + OAUTH_POLLING_SAFETY_MARGIN_MS)
                }
              },
            }
          },
        },
        {
          label: "Manually enter API Key",
          type: "api",
        },
      ],
    },
    "chat.headers": async (input, output) => {
      if (!usesCodexBackend(input.model, input.provider)) return
      output.headers.originator = "opencode"
      output.headers["User-Agent"] = `opencode/${InstallationVersion} (${os.platform()} ${os.release()}; ${os.arch()})`
      output.headers.session_id = input.sessionID
    },
    "chat.params": async (input, output) => {
      if (!usesCodexBackend(input.model, input.provider)) return
      // Match codex cli
      output.maxOutputTokens = undefined
    },
  }
}
