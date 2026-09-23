import { createHash, timingSafeEqual } from "node:crypto"
import { isIP } from "node:net"
import { Effect } from "effect"
import { Auth } from "@/auth"
import { AppRuntime } from "@/effect/app-runtime"
import { ModelsDev, Provider, ProviderAuth } from "@/provider"
import { ProviderID } from "@/provider/schema"
import type { OpenClankManagedProtocol } from "./openclank-protocol"
import { parseCodexCatalog, parseCopilotCatalog, parseGenericCatalog, type ParsedDiscovery } from "./provider-discovery"

const FLOW_TTL_MS = 10 * 60 * 1000
const DISCOVERY_TIMEOUT_MS = 5_000
const MAX_DISCOVERY_BYTES = 2 * 1024 * 1024
const MAX_DISCOVERED_MODELS = 4_096
const SECRET_SETTING_NAMES = new Set([
  "api_key",
  "apikey",
  "access_token",
  "refresh_token",
  "token",
  "secret",
  "client_secret",
  "password",
  "credential",
  "credentials",
  "authorization",
])

type BillingLane = Auth.BillingLane
type ConnectionKind = "official" | "subscription" | "custom_gateway" | "local"

type FamilyDefinition = {
  id: string
  displayName: string
  adapters: readonly string[]
  kinds: readonly ConnectionKind[]
  billingLanes: readonly BillingLane[]
  apiKey: boolean
  keyless: boolean
  defaultURL?: string
}

const FAMILY_DEFINITIONS: readonly FamilyDefinition[] = [
  {
    id: "openai",
    displayName: "OpenAI",
    adapters: ["openai-responses", "openai-chat"],
    kinds: ["official", "subscription", "custom_gateway"],
    billingLanes: ["metered_api", "subscription", "custom"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "anthropic",
    displayName: "Anthropic",
    adapters: ["anthropic-messages"],
    kinds: ["official", "subscription", "custom_gateway"],
    billingLanes: ["metered_api", "subscription", "custom"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "github-copilot",
    displayName: "GitHub Copilot",
    adapters: ["copilot-chat"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: false,
    keyless: false,
  },
  {
    id: "xiaomi",
    displayName: "Xiaomi",
    adapters: ["mimo-native"],
    kinds: ["official", "subscription"],
    billingLanes: ["metered_api", "subscription"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "google",
    displayName: "Google",
    adapters: ["google-generative-ai", "google-vertex"],
    kinds: ["official", "custom_gateway"],
    billingLanes: ["metered_api", "custom"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "xai",
    displayName: "xAI",
    adapters: ["xai-responses"],
    kinds: ["official", "subscription", "custom_gateway"],
    billingLanes: ["metered_api", "subscription", "custom"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "openrouter",
    displayName: "OpenRouter",
    adapters: ["openai-chat"],
    kinds: ["official", "custom_gateway"],
    billingLanes: ["metered_api", "custom"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "deepseek",
    displayName: "DeepSeek",
    adapters: ["openai-chat"],
    kinds: ["official"],
    billingLanes: ["metered_api"],
    apiKey: true,
    keyless: false,
  },
  {
    id: "ollama",
    displayName: "Ollama",
    adapters: ["ollama"],
    kinds: ["local"],
    billingLanes: ["local"],
    apiKey: true,
    keyless: true,
  },
  {
    id: "openai-compatible",
    displayName: "OpenAI-compatible gateway",
    adapters: ["openai-chat", "openai-responses"],
    kinds: ["custom_gateway", "local"],
    billingLanes: ["custom", "local"],
    apiKey: true,
    keyless: true,
  },
  {
    id: "local-executor",
    displayName: "Open Clank local executor",
    adapters: ["openclank-local-executor"],
    kinds: ["local"],
    billingLanes: ["local"],
    apiKey: false,
    keyless: true,
  },
] as const

const FAMILIES = new Map(FAMILY_DEFINITIONS.map((family) => [family.id, family]))

export type AuthServices = {
  methods(): Promise<ProviderAuth.Methods>
  authorize(input: {
    providerID: ProviderID
    method: number
    inputs?: Record<string, string>
    flowID: string
    redirectURI: string
    state: string
  }): Promise<ProviderAuth.Authorization | undefined>
  exchange(input: {
    providerID: ProviderID
    method: number
    code?: string
    flowID: string
  }): Promise<Auth.Info | undefined>
  cancel(input: { providerID: ProviderID; flowID: string }): Promise<void>
  modelCatalog(): Promise<Record<string, ModelsDev.Provider>>
}

const services: AuthServices = {
  methods: () => AppRuntime.runPromise(ProviderAuth.Service.use((service) => service.methods())),
  authorize: (input) => AppRuntime.runPromise(ProviderAuth.Service.use((service) => service.authorize(input))),
  exchange: (input) => AppRuntime.runPromise(ProviderAuth.Service.use((service) => service.exchange(input))),
  cancel: (input) => AppRuntime.runPromise(ProviderAuth.Service.use((service) => service.cancel(input))),
  modelCatalog: () => ModelsDev.get(),
}

type ProviderModelRoute = OpenClankManagedProtocol.ConnectionValidationResult["modelRoutes"][number]
type Fetcher = typeof globalThis.fetch
type DiscoveryStatus = "complete" | "unavailable" | "partial" | "reauth_required"
type DiscoveryFreshness = "fresh" | "stale" | "unknown"

type OAuthMode = "add" | "reauth"
type OAuthState = "pending" | "running" | "complete" | "failed" | "cancelled" | "expired"

type PendingOAuth = {
  flowID: string
  connectionID: string
  providerID: string
  billingLane: BillingLane
  mode: OAuthMode
  targetAccountID?: string
  expectedRevision?: number
  method: number
  nonce: string
  verifierChallenge: string
  expiresAt: number
  state: OAuthState
  credential?: Auth.Info
  errorCode?: string
  task?: Promise<void>
}

export class ProviderControlError extends Error {}

function object(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new ProviderControlError(`${label} must be an object`)
  }
  return value as Record<string, unknown>
}

function text(value: unknown, label: string, max = 2048): string {
  if (typeof value !== "string" || value.length === 0 || value.length > max || value.includes("\0")) {
    throw new ProviderControlError(`${label} is invalid`)
  }
  return value
}

function integer(value: unknown, label: string, minimum = 0): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) {
    throw new ProviderControlError(`${label} is invalid`)
  }
  return value
}

function assertNoSecretSettings(value: unknown, depth = 0): void {
  if (depth > 12) throw new ProviderControlError("provider settings are too deeply nested")
  if (Array.isArray(value)) {
    for (const item of value) assertNoSecretSettings(item, depth + 1)
    return
  }
  if (typeof value !== "object" || value === null) return
  for (const [key, item] of Object.entries(value)) {
    const normalized = key.trim().toLowerCase().replaceAll("-", "_")
    if (SECRET_SETTING_NAMES.has(normalized)) {
      throw new ProviderControlError("secret-bearing provider settings are not allowed")
    }
    assertNoSecretSettings(item, depth + 1)
  }
}

function unsafeDiscoveryHost(value: string): boolean {
  const host = value.replace(/^\[|\]$/g, "").toLowerCase()
  if (
    host === "metadata.google.internal" ||
    host === "metadata.goog" ||
    host === "kubernetes.default.svc" ||
    host === "fd00:ec2::254"
  )
    return true
  const version = isIP(host)
  if (version === 4) {
    const octets = host.split(".").map(Number)
    const [first, second] = octets
    return (
      first === 0 ||
      (first === 169 && second === 254) ||
      (first === 100 && second >= 64 && second <= 127) ||
      first >= 224
    )
  }
  if (version === 6) {
    if (host === "::" || host.startsWith("ff")) return true
    const first = Number.parseInt(host.split(":", 1)[0] || "0", 16)
    if (first >= 0xfe80 && first <= 0xfebf) return true
    const mappedDecimal = host.match(/(?:^|:)ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1]
    if (mappedDecimal && unsafeDiscoveryHost(mappedDecimal)) return true
    const mappedHex = host.match(/(?:^|:)ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/)
    if (mappedHex) {
      const high = Number.parseInt(mappedHex[1], 16)
      const low = Number.parseInt(mappedHex[2], 16)
      const mapped = `${high >> 8}.${high & 255}.${low >> 8}.${low & 255}`
      if (unsafeDiscoveryHost(mapped)) return true
    }
  }
  return false
}

function normalizedURL(value: unknown, kind: ConnectionKind, allowEmpty = false): string | undefined {
  if (value === undefined || value === null || value === "") {
    if (!allowEmpty && (kind === "local" || kind === "custom_gateway")) {
      throw new ProviderControlError("this provider connection requires a URL")
    }
    return undefined
  }
  const raw = text(value, "provider URL")
  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    throw new ProviderControlError("provider URL must be absolute HTTP(S)")
  }
  if (!parsed.hostname || !["http:", "https:"].includes(parsed.protocol)) {
    throw new ProviderControlError("provider URL must be absolute HTTP(S)")
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new ProviderControlError("provider URL cannot contain credentials, query parameters, or fragments")
  }
  if (parsed.protocol === "http:" && kind !== "local") {
    throw new ProviderControlError("plain HTTP is restricted to local provider connections")
  }
  if (unsafeDiscoveryHost(parsed.hostname)) {
    throw new ProviderControlError("provider URL host is reserved or unsafe")
  }
  if (isIP(parsed.hostname)) {
    const host = parsed.hostname.toLowerCase()
    if (host === "0.0.0.0" || host === "::" || host.startsWith("224.")) {
      throw new ProviderControlError("provider URL host is not routable")
    }
  }
  parsed.pathname = parsed.pathname.replace(/\/+$/, "")
  return parsed.toString().replace(/\/$/, "")
}

function normalizedEnterpriseHost(value: unknown): string {
  const raw = text(value, "provider enterprise URL", 2048)
  const candidate = raw.includes("://") ? raw : `https://${raw}`
  const normalized = normalizedURL(candidate, "official")
  if (!normalized) throw new ProviderControlError("provider enterprise URL is invalid")
  const parsed = new URL(normalized)
  if (parsed.pathname !== "/" || parsed.search || parsed.hash || parsed.username || parsed.password) {
    throw new ProviderControlError("provider enterprise URL must contain only a host")
  }
  return parsed.host
}

function localCatalogHost(value: string): boolean {
  const host = value.replace(/^\[|\]$/g, "").toLowerCase()
  if (host === "localhost" || host.endsWith(".localhost") || host.endsWith(".local")) return true
  const version = isIP(host)
  if (version === 4) {
    const [first, second] = host.split(".").map(Number)
    return (
      first === 10 ||
      first === 127 ||
      (first === 172 && second >= 16 && second <= 31) ||
      (first === 192 && second === 168)
    )
  }
  if (version === 6) {
    if (host === "::1") return true
    const first = Number.parseInt(host.split(":", 1)[0] || "0", 16)
    if (first >= 0xfc00 && first <= 0xfdff) return true
    const mapped = host.match(/(?:^|:)ffff:(\d+\.\d+\.\d+\.\d+)$/)?.[1]
    if (mapped) return localCatalogHost(mapped)
  }
  return false
}

function modelsDevFamilies(catalog: Record<string, ModelsDev.Provider>): FamilyDefinition[] {
  const result: FamilyDefinition[] = []
  for (const [catalogID, provider] of Object.entries(catalog)) {
    if (catalogID !== provider.id) continue
    if (FAMILIES.has(provider.id)) continue
    if (
      !provider.id ||
      provider.id.length > 128 ||
      /[\s\0]/u.test(provider.id) ||
      !provider.name?.trim() ||
      provider.name.trim().length > 256
    )
      continue

    const npm = provider.npm ?? "@ai-sdk/openai-compatible"
    const adapterID = Provider.managedModelsDevAdapterID(npm)
    if (!adapterID) continue
    if (Object.values(provider.models).some((model) => (model.provider?.npm ?? npm) !== npm)) continue

    const sourceURL = provider.api?.trim()
    if (!sourceURL || sourceURL.includes("${")) continue
    let defaultURL: string | undefined
    try {
      defaultURL = normalizedURL(sourceURL, "official")
    } catch {
      continue
    }
    if (!defaultURL) continue
    const parsed = new URL(defaultURL)
    if (parsed.protocol !== "https:" || localCatalogHost(parsed.hostname)) continue

    result.push({
      id: provider.id,
      displayName: provider.name.trim(),
      adapters: [adapterID],
      kinds: ["official"],
      billingLanes: ["metered_api"],
      apiKey: true,
      keyless: false,
      defaultURL,
    })
  }
  return result.sort(
    (left, right) => left.displayName.localeCompare(right.displayName) || left.id.localeCompare(right.id),
  )
}

function familyDefinitions(catalog: Record<string, ModelsDev.Provider>): readonly FamilyDefinition[] {
  return [...FAMILY_DEFINITIONS, ...modelsDevFamilies(catalog)]
}

function familyMap(catalog: Record<string, ModelsDev.Provider>): ReadonlyMap<string, FamilyDefinition> {
  return new Map(familyDefinitions(catalog).map((family) => [family.id, family]))
}

function operationsForModalities(model: ModelsDev.Model): ProviderModelRoute["operations"] {
  const input = new Set(model.modalities?.input ?? ["text"])
  const output = new Set(model.modalities?.output ?? ["text"])
  const operations = new Set<ProviderModelRoute["operations"][number]>()
  if (output.has("text")) {
    operations.add("chat.stream")
    operations.add("chat.complete")
  }
  if (input.has("image") && output.has("text")) operations.add("vision.describe")
  if (output.has("image")) operations.add("image.generate")
  if (output.has("audio")) operations.add("audio.synthesize")
  if (input.has("audio") && output.has("text")) operations.add("audio.transcribe")
  if (operations.size === 0) {
    operations.add("chat.stream")
    operations.add("chat.complete")
  }
  return [...operations]
}

function frozenModelRoutes(familyID: string, provider: ModelsDev.Provider | undefined): ProviderModelRoute[] {
  if (!provider) return []
  return Object.values(provider.models)
    .slice(0, MAX_DISCOVERED_MODELS)
    .map((model) => ({
      modelID: model.id,
      displayName: model.name || model.id,
      operations: operationsForModalities(model),
      capabilities: {
        ...(model.family === undefined ? {} : { family: model.family }),
        release_date: model.release_date,
        attachment: model.attachment,
        reasoning: model.reasoning,
        temperature: model.temperature,
        tool_call: model.tool_call,
        ...(model.interleaved === undefined ? {} : { interleaved: model.interleaved }),
        ...(model.cost === undefined ? {} : { cost: model.cost }),
        limit: model.limit,
        ...(model.modalities === undefined ? {} : { modalities: model.modalities }),
        ...(model.status === undefined ? {} : { status: model.status }),
      },
      provenance: {
        authority: "managed-engine",
        catalog: "models.dev-snapshot",
        familyID,
      },
    }))
}

function discoveryURLs(connection: ReturnType<typeof validateConnection>): string[] {
  if (!connection.normalizedURL) return []
  const base = new URL(connection.normalizedURL)
  const path = base.pathname.replace(/\/+$/, "")
  const build = (pathname: string) => {
    const target = new URL(base)
    target.pathname = pathname || "/"
    return target.toString().replace(/\/$/, "")
  }
  if (connection.familyID === "ollama") {
    const root = path.endsWith("/api") || path.endsWith("/v1") ? path.slice(0, path.lastIndexOf("/")) : path
    return [build(`${root}/api/tags`), build(`${root}/v1/models`)]
  }
  return [build(path.endsWith("/v1") ? `${path}/models` : `${path}/v1/models`)]
}

function credentialToken(credential: Auth.Info | undefined): string | undefined {
  if (!credential) return undefined
  if (credential.type === "api") return credential.key
  if (credential.type === "oauth") return credential.access
  if (credential.type === "wellknown") return credential.token
  return undefined
}

async function boundedJSON(response: Response): Promise<unknown> {
  const declared = Number(response.headers.get("content-length") ?? 0)
  if (declared > MAX_DISCOVERY_BYTES) throw new ProviderControlError("provider model catalog is too large")
  if (!response.body) return undefined
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let bytes = 0
  let source = ""
  while (true) {
    const part = await reader.read()
    if (part.done) break
    bytes += part.value.byteLength
    if (bytes > MAX_DISCOVERY_BYTES) {
      await reader.cancel()
      throw new ProviderControlError("provider model catalog is too large")
    }
    source += decoder.decode(part.value, { stream: true })
  }
  source += decoder.decode()
  return JSON.parse(source)
}

async function liveModelRoutes(
  connection: ReturnType<typeof validateConnection>,
  credential: Auth.Info | undefined,
  fetcher: Fetcher,
): Promise<ProviderModelRoute[]> {
  const headers = new Headers({ Accept: "application/json" })
  const token =
    credentialToken(credential)
  if (token) headers.set("Authorization", `Bearer ${token}`)
  for (const url of discoveryURLs(connection)) {
    try {
      const response = await fetcher(url, {
        headers,
        redirect: "error",
        signal: AbortSignal.timeout(DISCOVERY_TIMEOUT_MS),
      })
      if (!response.ok) continue
      const parsed = parseGenericCatalog(await boundedJSON(response))
      if (parsed.models.length === 0) continue
      return parsed.models.slice(0, MAX_DISCOVERED_MODELS).map((model) => ({
        ...model,
        capabilities: { ...model.capabilities, family: connection.familyID },
        provenance: { ...model.provenance, familyID: connection.familyID },
      })) as ProviderModelRoute[]
    } catch {
      // Connection creation remains possible while a local server is stopped
      // or while a protected endpoint still needs its optional account.
    }
  }
  return []
}

function subscriptionDiscoveryURLs(connection: ReturnType<typeof validateConnection>, credential: Auth.Info): string[] {
  if (connection.familyID === "openai" && connection.adapterID === "openai-responses") {
    return ["https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"]
  }
  if (connection.familyID === "github-copilot" && connection.adapterID === "copilot-chat") {
    const enterprise = credential.type === "oauth" ? credential.enterpriseUrl : undefined
    if (enterprise) {
      const domain = normalizedEnterpriseHost(enterprise)
      return [`https://copilot-api.${domain}/models`]
    }
    return ["https://api.githubcopilot.com/models"]
  }
  if (connection.familyID === "xai" && connection.adapterID === "xai-responses") {
    return ["https://api.x.ai/v1/models"]
  }
  if (connection.familyID === "anthropic" && connection.adapterID === "anthropic-messages") {
    return ["https://api.anthropic.com/v1/models"]
  }
  return []
}

function parseAccountCatalog(
  connection: ReturnType<typeof validateConnection>,
  value: unknown,
): ParsedDiscovery {
  if (connection.familyID === "openai" && connection.adapterID === "openai-responses") return parseCodexCatalog(value)
  if (connection.familyID === "github-copilot" && connection.adapterID === "copilot-chat") return parseCopilotCatalog(value)
  return parseGenericCatalog(value)
}

function discoverySource(connection: ReturnType<typeof validateConnection>): string {
  if (connection.familyID === "openai" && connection.adapterID === "openai-responses") return "codex-account-models"
  if (connection.familyID === "github-copilot" && connection.adapterID === "copilot-chat") return "copilot-account-models"
  return "provider-account-models"
}

function discoveryResult(
  accountID: string,
  credentialRevision: number,
  status: DiscoveryStatus,
  source: string,
  observedAt: number,
  models: ProviderModelRoute[] = [],
  freshness: DiscoveryFreshness = "unknown",
  errorCode?: "discovery_unavailable" | "discovery_partial" | "reauth_required",
) {
  return {
    status,
    accountID,
    credentialRevision,
    models,
    authoritative: status === "complete",
    provenance: { source, observedAt },
    freshness,
    ...(errorCode ? { errorCode } : {}),
  }
}

async function accountDiscovery(
  connection: ReturnType<typeof validateConnection>,
  credential: Auth.Info,
  accountID: string,
  credentialRevision: number,
  fetcher: Fetcher,
  now: () => number,
  timeoutMs: number,
): Promise<ReturnType<typeof discoveryResult>> {
  const source = discoverySource(connection)
  const urls = connection.kind === "subscription" ? subscriptionDiscoveryURLs(connection, credential) : discoveryURLs(connection)
  if (urls.length === 0) {
    return discoveryResult(accountID, credentialRevision, "unavailable", source, now(), [], "unknown", "discovery_unavailable")
  }

  const headers = new Headers({ Accept: "application/json" })
  const token =
    connection.familyID === "github-copilot" && credential.type === "oauth"
      ? credential.refresh
      : credentialToken(credential)
  if (token) headers.set("Authorization", `Bearer ${token}`)
  if (connection.familyID === "openai" && connection.kind === "subscription") {
    headers.set("Origin", "https://chatgpt.com")
    headers.set("Referer", "https://chatgpt.com/codex")
    if (credential.type === "oauth" && credential.accountId) {
      headers.set("ChatGPT-Account-Id", credential.accountId)
    }
  }

  let sawResponse = false
  for (const url of urls) {
    try {
      const response = await fetcher(url, {
        headers,
        redirect: "error",
        signal: AbortSignal.timeout(timeoutMs),
      })
      sawResponse = true
      if (response.status === 401 || response.status === 403) {
        return discoveryResult(accountID, credentialRevision, "reauth_required", source, now(), [], "fresh", "reauth_required")
      }
      if (response.status === 429 || !response.ok) continue
      const parsed = parseAccountCatalog(connection, await boundedJSON(response))
      const overflow = parsed.models.length > MAX_DISCOVERED_MODELS
      const models = parsed.models.slice(0, MAX_DISCOVERED_MODELS).map((model) => ({
        ...model,
        provenance: { ...model.provenance, familyID: connection.familyID },
      })) as ProviderModelRoute[]
      if (parsed.invalidRows > 0 || overflow) {
        return discoveryResult(accountID, credentialRevision, "partial", source, now(), models, "fresh", "discovery_partial")
      }
      return discoveryResult(accountID, credentialRevision, "complete", source, now(), models, "fresh")
    } catch {
      // The caller retains S06's last-good inventory on transport or parse failure.
    }
  }
  return discoveryResult(
    accountID,
    credentialRevision,
    "unavailable",
    source,
    now(),
    [],
    sawResponse ? "stale" : "unknown",
    "discovery_unavailable",
  )
}

function validateConnectionWithFamilies(value: unknown, families: ReadonlyMap<string, FamilyDefinition>) {
  const input = object(value, "provider connection")
  const familyID = text(input.familyID, "provider family", 128)
  const family = families.get(familyID)
  if (!family) throw new ProviderControlError("unknown managed provider family")
  const adapterID = text(input.adapterID, "provider adapter", 128)
  const kind = text(input.kind, "provider connection kind", 64) as ConnectionKind
  const billingLane = text(input.billingLane, "provider billing lane", 64) as BillingLane
  if (!family.adapters.includes(adapterID)) throw new ProviderControlError("provider adapter is not supported")
  if (!family.kinds.includes(kind)) throw new ProviderControlError("provider connection kind is not supported")
  if (!family.billingLanes.includes(billingLane))
    throw new ProviderControlError("provider billing lane is not supported")
  if (kind === "subscription" && billingLane !== "subscription") {
    throw new ProviderControlError("subscription connections require the subscription billing lane")
  }
  if (kind === "local" && billingLane !== "local") {
    throw new ProviderControlError("local connections require the local billing lane")
  }
  const settings = input.settings === undefined ? {} : object(input.settings, "provider settings")
  assertNoSecretSettings(settings)
  if (familyID === "github-copilot" && settings.enterpriseUrl !== undefined) {
    normalizedEnterpriseHost(settings.enterpriseUrl)
  }
  const isLocalExecutor = familyID === "local-executor" && adapterID === "openclank-local-executor"
  const requestedURL = input.url === "" ? undefined : input.url
  const url = normalizedURL(requestedURL ?? family.defaultURL, kind, isLocalExecutor)
  return {
    familyID,
    adapterID,
    kind,
    billingLane,
    ...(url ? { normalizedURL: url } : {}),
    settings,
    credentialRequired: !(kind === "local" && family.keyless),
    modelRoutes: isLocalExecutor
      ? [
          {
            modelID: "fastembed/default",
            displayName: "FastEmbed (local)",
            operations: ["embeddings.create"],
            capabilities: {
              localExecutorID: "openclank.fastembed.v1",
              batch: true,
              normalized: true,
            },
            provenance: {
              authority: "managed-engine",
              catalog: "openclank-local-executors-v1",
            },
          },
          {
            modelID: "diffusion/default",
            displayName: "Diffusion (local)",
            operations: ["image.generate", "image.edit", "image.inpaint", "image.img2img"],
            capabilities: {
              localExecutorID: "openclank.diffusion.v1",
              artifacts: true,
              normalized: true,
            },
            provenance: {
              authority: "managed-engine",
              catalog: "openclank-local-executors-v1",
            },
          },
          {
            modelID: "realesrgan/x4plus",
            displayName: "Real-ESRGAN x4plus (local)",
            operations: ["image.upscale", "image.denoise"],
            capabilities: {
              localExecutorID: "openclank.realesrgan.v1",
              artifacts: true,
              normalized: true,
            },
            provenance: {
              authority: "managed-engine",
              catalog: "openclank-local-executors-v1",
            },
          },
          {
            modelID: "briaai/RMBG-1.4",
            displayName: "RMBG 1.4 (local)",
            operations: ["image.segment", "image.remove_background"],
            capabilities: {
              localExecutorID: "openclank.rmbg.v1",
              artifacts: true,
              normalized: true,
            },
            provenance: {
              authority: "managed-engine",
              catalog: "openclank-local-executors-v1",
            },
          },
          {
            modelID: "gfpgan/clean-v1",
            displayName: "GFPGAN clean (local)",
            operations: ["image.restore_face"],
            capabilities: {
              localExecutorID: "openclank.gfpgan.v1",
              artifacts: true,
              normalized: true,
            },
            provenance: {
              authority: "managed-engine",
              catalog: "openclank-local-executors-v1",
            },
          },
        ]
      : [],
  }
}

export function validateConnection(value: unknown) {
  return validateConnectionWithFamilies(value, FAMILIES)
}

function authClass(lane: BillingLane): string {
  if (lane === "metered_api") return "metered"
  if (lane === "subscription") return "subscription"
  return lane
}

function validateAccountWithFamilies(value: unknown, families: ReadonlyMap<string, FamilyDefinition>) {
  const input = object(value, "provider account")
  const connection = validateConnectionWithFamilies(input.connection, families)
  const authMethod = text(input.authMethod, "provider auth method", 32)
  const credential = object(input.credential, "provider credential")
  const accountID = text(input.accountID, "provider account ID", 256)
  const credentialRevision = integer(input.credentialRevision, "provider credential revision", 1)
  const family = families.get(connection.familyID)!
  if (authMethod === "api_key") {
    if (!family.apiKey) throw new ProviderControlError("this provider connection does not accept API-key accounts")
    if (connection.billingLane === "subscription") {
      throw new ProviderControlError("API keys cannot use the subscription billing lane")
    }
    if (credential.type !== "api") throw new ProviderControlError("provider credential type is not supported")
    const key = text(credential.key, "provider API key", 131_072)
    const metadata = credential.metadata
    if (metadata !== undefined) {
      const values = object(metadata, "provider credential metadata")
      for (const [name, item] of Object.entries(values)) {
        text(name, "provider credential metadata name", 128)
        text(item, "provider credential metadata value", 2048)
      }
    }
    const normalizedCredential: Auth.Info = {
      type: "api",
      key,
      ...(metadata === undefined ? {} : { metadata: metadata as Record<string, string> }),
    }
    return {
      authMethod: "api_key" as const,
      authClass: authClass(connection.billingLane),
      credential: normalizedCredential,
      accountID,
      credentialRevision,
      safeIdentity: safeIdentity(normalizedCredential),
      modelRoutes: [],
      connection,
    }
  }
  if (authMethod !== "oauth") throw new ProviderControlError("provider auth method is not supported")
  if (connection.billingLane !== "subscription" || credential.type !== "oauth") {
    throw new ProviderControlError("OAuth accounts require a subscription connection")
  }
  const refresh = text(credential.refresh, "provider OAuth refresh token", 131_072)
  const access = text(credential.access, "provider OAuth access token", 131_072)
  if (typeof credential.expires !== "number" || !Number.isFinite(credential.expires)) {
    throw new ProviderControlError("provider OAuth expiry is invalid")
  }
  const normalizedCredential: Auth.Info = {
    type: "oauth",
    refresh,
    access,
    expires: credential.expires,
    ...(credential.accountId === undefined ? {} : { accountId: text(credential.accountId, "provider OAuth account", 256) }),
    ...(credential.enterpriseUrl === undefined
      ? {}
      : { enterpriseUrl: normalizedEnterpriseHost(credential.enterpriseUrl) }),
  }
  if (connection.familyID === "github-copilot" && connection.settings.enterpriseUrl !== undefined) {
    const configuredEnterprise = normalizedEnterpriseHost(connection.settings.enterpriseUrl)
    if (configuredEnterprise !== normalizedCredential.enterpriseUrl) {
      throw new ProviderControlError("provider enterprise URL does not match the OAuth credential")
    }
  }
  return {
    authMethod: "oauth" as const,
    authClass: authClass(connection.billingLane),
    credential: normalizedCredential,
    accountID,
    credentialRevision,
    safeIdentity: safeIdentity(normalizedCredential),
    modelRoutes: [],
    connection,
  }
}

export function validateAccount(value: unknown) {
  return validateAccountWithFamilies(value, FAMILIES)
}

function challenge(verifier: string): string {
  return createHash("sha256").update(verifier, "utf8").digest("base64url")
}

function equal(left: string, right: string): boolean {
  const a = Buffer.from(left)
  const b = Buffer.from(right)
  return a.length === b.length && timingSafeEqual(a, b)
}

function flowIdentity(input: Record<string, unknown>) {
  const mode = text(input.mode, "OAuth mode", 16) as OAuthMode
  if (mode !== "add" && mode !== "reauth") throw new ProviderControlError("OAuth mode is invalid")
  const targetAccountID =
    input.targetAccountID === undefined ? undefined : text(input.targetAccountID, "OAuth target account", 128)
  const expectedRevision =
    input.expectedRevision === undefined ? undefined : integer(input.expectedRevision, "OAuth expected revision", 1)
  if (mode === "add" && (targetAccountID !== undefined || expectedRevision !== undefined)) {
    throw new ProviderControlError("OAuth add flow cannot target an existing account")
  }
  if (mode === "reauth" && (targetAccountID === undefined || expectedRevision === undefined)) {
    throw new ProviderControlError("OAuth reconnect requires a target account and revision")
  }
  return {
    flowID: text(input.flowID, "OAuth flow", 128),
    connectionID: text(input.connectionID, "OAuth connection", 128),
    providerID: text(input.providerID, "OAuth provider", 128),
    billingLane: text(input.billingLane, "OAuth billing lane", 64) as BillingLane,
    mode,
    ...(targetAccountID ? { targetAccountID } : {}),
    ...(expectedRevision ? { expectedRevision } : {}),
    nonce: text(input.nonce, "OAuth nonce", 128),
  }
}

function sameFlow(flow: PendingOAuth, input: Record<string, unknown>): string {
  const identity = flowIdentity(input)
  if (
    identity.flowID !== flow.flowID ||
    identity.connectionID !== flow.connectionID ||
    identity.providerID !== flow.providerID ||
    identity.billingLane !== flow.billingLane ||
    identity.mode !== flow.mode ||
    identity.targetAccountID !== flow.targetAccountID ||
    identity.expectedRevision !== flow.expectedRevision ||
    !equal(identity.nonce, flow.nonce)
  ) {
    throw new ProviderControlError("OAuth flow context does not match")
  }
  const verifier = text(input.codeVerifier, "OAuth verifier", 128)
  if (!equal(challenge(verifier), flow.verifierChallenge)) {
    throw new ProviderControlError("OAuth verifier does not match")
  }
  return verifier
}

function safeIdentity(credential: Auth.Info): Record<string, string> {
  if (credential.type === "oauth") {
    return {
      ...(credential.accountId ? { provider_display_identity: credential.accountId } : {}),
      ...(credential.enterpriseUrl ? { enterprise_host: credential.enterpriseUrl } : {}),
    }
  }
  if (credential.type === "api" && credential.metadata?.uid) {
    return { provider_display_identity: credential.metadata.uid }
  }
  return {}
}

export class ControlPlane {
  private readonly flows = new Map<string, PendingOAuth>()

  constructor(
    private readonly auth: AuthServices = services,
    private readonly now = () => Date.now(),
    private readonly fetcher: Fetcher = globalThis.fetch,
    private readonly discoveryTimeoutMs = DISCOVERY_TIMEOUT_MS,
  ) {}

  private async models(
    connection: ReturnType<typeof validateConnection>,
    credential?: Auth.Info,
    modelCatalog?: Record<string, ModelsDev.Provider>,
  ): Promise<ProviderModelRoute[]> {
    if (connection.modelRoutes.length > 0) return connection.modelRoutes as ProviderModelRoute[]
    const live =
      connection.familyID === "ollama" ||
      connection.familyID === "openai-compatible" ||
      connection.kind === "custom_gateway"
    if (live) {
      return liveModelRoutes(connection, credential, this.fetcher)
    }
    const catalog: Record<string, ModelsDev.Provider> =
      modelCatalog ?? (await this.auth.modelCatalog().catch(() => ({})))
    return frozenModelRoutes(connection.familyID, catalog[connection.familyID])
  }

  async connectionValidate(value: unknown) {
    const modelCatalog = await this.auth.modelCatalog().catch(() => ({}))
    const connection = validateConnectionWithFamilies(value, familyMap(modelCatalog))
    return { ...connection, modelRoutes: await this.models(connection, undefined, modelCatalog) }
  }

  async accountValidate(value: unknown) {
    const input = object(value, "provider account")
    const modelCatalog = await this.auth.modelCatalog().catch(() => ({}))
    const families = familyMap(modelCatalog)
    const account = validateAccountWithFamilies(input, families)
    const connection = account.connection
    const live =
      connection.familyID === "ollama" ||
      connection.familyID === "openai-compatible" ||
      connection.kind === "custom_gateway"
    const discovery =
      connection.kind === "subscription" || live
        ? await accountDiscovery(
            connection,
            account.credential,
            account.accountID,
            account.credentialRevision,
            this.fetcher,
            this.now,
            this.discoveryTimeoutMs,
          )
        : discoveryResult(
            account.accountID,
            account.credentialRevision,
            "complete",
            "managed-static-catalog",
            this.now(),
            await this.models(connection, account.credential, modelCatalog),
            "fresh",
          )
    return {
      authMethod: account.authMethod,
      authClass: account.authClass,
      credential: account.credential,
      accountID: account.accountID,
      credentialRevision: account.credentialRevision,
      safeIdentity: account.safeIdentity,
      modelRoutes: discovery.models,
      discovery,
    }
  }

  private expire(): void {
    const now = this.now()
    for (const flow of this.flows.values()) {
      if (flow.expiresAt <= now && flow.state !== "complete") {
        flow.state = "expired"
        flow.errorCode = "oauth_expired"
        void this.auth.cancel({ providerID: ProviderID.make(flow.providerID), flowID: flow.flowID })
      }
      if (flow.expiresAt + FLOW_TTL_MS <= now) this.flows.delete(flow.flowID)
    }
  }

  private result(flow: PendingOAuth) {
    return {
      flowID: flow.flowID,
      connectionID: flow.connectionID,
      providerID: flow.providerID,
      billingLane: flow.billingLane,
      mode: flow.mode,
      ...(flow.targetAccountID ? { targetAccountID: flow.targetAccountID } : {}),
      ...(flow.expectedRevision ? { expectedRevision: flow.expectedRevision } : {}),
      status: flow.state,
      ...(flow.credential
        ? {
            credential: flow.credential,
            authMethod: "oauth",
            authClass: authClass(flow.billingLane),
            safeIdentity: safeIdentity(flow.credential),
          }
        : {}),
      ...(flow.errorCode ? { errorCode: flow.errorCode } : {}),
    }
  }

  private async exchange(flow: PendingOAuth, code?: string): Promise<void> {
    if (flow.state === "cancelled" || flow.state === "expired" || flow.state === "complete") return
    flow.state = "running"
    try {
      const credential = await this.auth.exchange({
        providerID: ProviderID.make(flow.providerID),
        method: flow.method,
        ...(code === undefined ? {} : { code }),
        flowID: flow.flowID,
      })
      if (!credential) throw new ProviderControlError("provider OAuth did not return a credential")
      // A cancellation may race the awaited provider exchange.  TypeScript's
      // local narrowing cannot observe that external mutation, so re-read the
      // shared flow state explicitly after the await.
      const currentState = flow.state as PendingOAuth["state"]
      if (currentState === "cancelled" || flow.expiresAt <= this.now()) {
        flow.state = currentState === "cancelled" ? "cancelled" : "expired"
        flow.errorCode = flow.state === "expired" ? "oauth_expired" : "oauth_cancelled"
        return
      }
      flow.credential = credential
      flow.state = "complete"
    } catch {
      if (flow.state !== "cancelled" && flow.state !== "expired") {
        flow.state = "failed"
        flow.errorCode = "oauth_exchange_failed"
      }
    }
  }

  async catalog() {
    const [methods, modelCatalog] = await Promise.all([this.auth.methods(), this.auth.modelCatalog()])
    return {
      schemaVersion: 1,
      families: familyDefinitions(modelCatalog).map((family) => {
        const providerMethods = FAMILIES.has(family.id) ? (methods[family.id] ?? []) : []
        const authMethods = [
          ...(family.apiKey ? [{ id: "api_key", type: "api" as const, label: "API key" }] : []),
          ...providerMethods.map((method, index) => ({
            id: `oauth:${index}`,
            type: method.type,
            label: method.label,
            ...(method.prompts ? { prompts: method.prompts } : {}),
          })),
          ...(family.keyless ? [{ id: "none", type: "none" as const, label: "No credential" }] : []),
        ]
        return {
          id: family.id,
          displayName: family.displayName,
          adapters: [...family.adapters],
          kinds: [...family.kinds],
          billingLanes: [...family.billingLanes],
          authMethods,
          modelCount: Object.keys(modelCatalog[family.id]?.models ?? {}).length,
        }
      }),
    }
  }

  async oauthStart(value: unknown) {
    this.expire()
    const input = object(value, "OAuth start request")
    const identity = flowIdentity(input)
    if (this.flows.has(identity.flowID)) throw new ProviderControlError("OAuth flow already exists")
    const family = FAMILIES.get(identity.providerID)
    if (!family) throw new ProviderControlError("OAuth provider family is unknown")
    if (!family.billingLanes.includes(identity.billingLane))
      throw new ProviderControlError("OAuth billing lane is not supported")
    const method = integer(input.method, "OAuth method")
    const methodCatalog = (await this.auth.methods())[identity.providerID] ?? []
    if (methodCatalog[method]?.type !== "oauth") throw new ProviderControlError("OAuth method is not supported")
    const verifierChallenge = text(input.codeVerifierChallenge, "OAuth verifier challenge", 128)
    if (!/^[A-Za-z0-9_-]{43}$/.test(verifierChallenge))
      throw new ProviderControlError("OAuth verifier challenge is invalid")
    const expiresAt = integer(input.expiresAt, "OAuth expiry", 1)
    const now = this.now()
    if (expiresAt <= now || expiresAt > now + FLOW_TTL_MS) throw new ProviderControlError("OAuth expiry is invalid")
    const flow: PendingOAuth = {
      ...identity,
      method,
      verifierChallenge,
      expiresAt,
      state: "pending",
    }
    this.flows.set(flow.flowID, flow)
    let authorization: ProviderAuth.Authorization | undefined
    try {
      authorization = await this.auth.authorize({
        providerID: ProviderID.make(flow.providerID),
        method,
        inputs: (input.inputs ?? {}) as Record<string, string>,
        flowID: flow.flowID,
        redirectURI: text(input.redirectURI, "OAuth redirect URI", 2048),
        state: text(input.state, "OAuth state", 256),
      })
    } catch (error) {
      this.flows.delete(flow.flowID)
      throw error
    }
    if (!authorization) {
      this.flows.delete(flow.flowID)
      throw new ProviderControlError("OAuth authorization did not start")
    }
    if (authorization.method === "auto") {
      flow.task = this.exchange(flow)
    }
    return {
      flowID: flow.flowID,
      url: authorization.url,
      method: authorization.method,
      instructions: authorization.instructions,
      expiresAt: flow.expiresAt,
    }
  }

  async oauthPoll(value: unknown) {
    this.expire()
    const input = object(value, "OAuth poll request")
    const flowID = text(input.flowID, "OAuth flow", 128)
    const flow = this.flows.get(flowID)
    if (!flow) throw new ProviderControlError("OAuth flow was not found")
    sameFlow(flow, input)
    return this.result(flow)
  }

  async oauthCallback(value: unknown) {
    this.expire()
    const input = object(value, "OAuth callback request")
    const flowID = text(input.flowID, "OAuth flow", 128)
    const flow = this.flows.get(flowID)
    if (!flow) throw new ProviderControlError("OAuth flow was not found")
    sameFlow(flow, input)
    if (flow.state === "pending") {
      await this.exchange(flow, input.code === undefined ? undefined : text(input.code, "OAuth code", 131_072))
    } else if (flow.state === "running" && flow.task) {
      await flow.task
    }
    return this.result(flow)
  }

  async oauthCancel(value: unknown) {
    this.expire()
    const input = object(value, "OAuth cancel request")
    const flowID = text(input.flowID, "OAuth flow", 128)
    const flow = this.flows.get(flowID)
    if (!flow) return { flowID, status: "cancelled" as const }
    sameFlow(flow, input)
    flow.state = "cancelled"
    flow.errorCode = "oauth_cancelled"
    flow.credential = undefined
    await this.auth.cancel({ providerID: ProviderID.make(flow.providerID), flowID })
    this.flows.delete(flowID)
    return { flowID, status: "cancelled" as const }
  }

  async handle<M extends keyof OpenClankManagedProtocol.EngineMethodRequestMap>(
    method: M,
    params: OpenClankManagedProtocol.EngineMethodRequestMap[M],
  ): Promise<OpenClankManagedProtocol.EngineMethodResultMap[M]> {
    let result: unknown
    switch (method) {
      case "_openclank/provider-control/v1/catalog":
        result = await this.catalog()
        break
      case "_openclank/provider-control/v1/connection/validate":
        result = await this.connectionValidate(params)
        break
      case "_openclank/provider-control/v1/account/validate":
        result = await this.accountValidate(params)
        break
      case "_openclank/provider-control/v1/oauth/start":
        result = await this.oauthStart(params)
        break
      case "_openclank/provider-control/v1/oauth/poll":
        result = await this.oauthPoll(params)
        break
      case "_openclank/provider-control/v1/oauth/callback":
        result = await this.oauthCallback(params)
        break
      case "_openclank/provider-control/v1/oauth/cancel":
        result = await this.oauthCancel(params)
        break
      default:
        throw new ProviderControlError("unsupported managed provider control method")
    }
    return result as OpenClankManagedProtocol.EngineMethodResultMap[M]
  }
}

export * as ManagedProviderControl from "./provider-control"
