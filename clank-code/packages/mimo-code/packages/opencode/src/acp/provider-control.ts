import { createHash, timingSafeEqual } from "node:crypto"
import { isIP } from "node:net"
import { Effect } from "effect"
import { Auth } from "@/auth"
import { AppRuntime } from "@/effect/app-runtime"
import { ModelsDev, Provider, ProviderAuth } from "@/provider"
import { ProviderID } from "@/provider/schema"
import type { OpenClankManagedProtocol } from "./openclank-protocol"
import { parseClaudeCatalog, parseCodexCatalog, parseCopilotCatalog, parseGenericCatalog, parseGrokCatalog, type ParsedDiscovery } from "./provider-discovery"
import { claudeDiscoveryRequest } from "@/plugin/claude"
import { resolveManagedCapabilities, type CapabilityModel } from "./provider-capabilities"
import { resolveMimoEndpoint, type MimoBillingLane } from "@/provider/mimo-endpoint-policy"
import { openCodeProtocol } from "@/provider/opencode-protocol"
import { InstallationVersion } from "@/installation/version"

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
  apiKeyLabel?: string
  apiKeyBillingLanes?: readonly BillingLane[]
  keyless: boolean
  defaultURL?: string
  endpointPolicy?: Readonly<Record<string, string>>
}

const COMFY_JOB_FAMILY: FamilyDefinition = {id:"comfyui", displayName:"ComfyUI jobs", adapters:["comfyui-workflow-v1"], kinds:["local"], billingLanes:["local"], apiKey:false, keyless:true}

const SPRIXEN_JOB_FAMILY: FamilyDefinition = {id:"sprixen", displayName:"Sprixen sprite jobs", adapters:["sprixen-sprite-v1"], kinds:["official"], billingLanes:["metered_api"], apiKey:true, keyless:false, defaultURL:"https://api.sprixen.com"}
const TRIPO_JOB_FAMILY: FamilyDefinition = {id:"tripo", displayName:"Tripo text-to-3D jobs", adapters:["tripo-text3d-v1"], kinds:["official"], billingLanes:["metered_api"], apiKey:true, keyless:false, defaultURL:"https://openapi.tripo3d.ai"}
const FAL_JOB_FAMILY: FamilyDefinition = {id:"fal", displayName:"Fal media API", adapters:["fal-flux-schnell-v1"], kinds:["official"], billingLanes:["metered_api"], apiKey:true, keyless:false, defaultURL:"https://queue.fal.run"}
const VOIDLESS_JOB_FAMILY: FamilyDefinition = {id:"voidless", displayName:"Voidless 64px image jobs", adapters:["voidless-image-v1"], kinds:["official"], billingLanes:["metered_api"], apiKey:true, keyless:false, defaultURL:"https://api.voidless.dev"}
const MESHY_JOB_FAMILY: FamilyDefinition = {id:"meshy", displayName:"Meshy preview jobs", adapters:["meshy-preview-v1"], kinds:["official"], billingLanes:["metered_api"], apiKey:true, keyless:false, defaultURL:"https://api.meshy.ai"}
const TRELLIS_JOB_FAMILY: FamilyDefinition = {id:"trellis", displayName:"TRELLIS image-to-3D jobs", adapters:["trellis-image3d-v1"], kinds:["local"], billingLanes:["local"], apiKey:false, keyless:true}
const HUNYUAN_SHAPE_FAMILY: FamilyDefinition = {id:"hunyuan-shape", displayName:"Hunyuan local shape jobs", adapters:["hunyuan3d-v2-shape-v1"], kinds:["local"], billingLanes:["local"], apiKey:false, keyless:true}

const BLENDER_JOB_FAMILY: FamilyDefinition = {id:"blender", displayName:"Blender jobs", adapters:["blender-render-v1"], kinds:["local"], billingLanes:["local"], apiKey:false, keyless:true}

const FAMILY_DEFINITIONS: readonly FamilyDefinition[] = [
  COMFY_JOB_FAMILY,
  BLENDER_JOB_FAMILY,
  TRELLIS_JOB_FAMILY, HUNYUAN_SHAPE_FAMILY,
  SPRIXEN_JOB_FAMILY,
  MESHY_JOB_FAMILY,
  VOIDLESS_JOB_FAMILY,
  TRIPO_JOB_FAMILY,
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
    apiKeyBillingLanes: ["metered_api", "subscription"],
    endpointPolicy: { metered_api: "mimo-metered-api-v1", subscription: "mimo-token-plan-v1" },
  },
  {
    id: "opencode-console",
    displayName: "OpenCode Console",
    adapters: ["opencode-native"],
    kinds: ["official"],
    billingLanes: ["metered_api"],
    apiKey: true,
    keyless: false,
    defaultURL: "https://opencode.ai/inference",
    endpointPolicy: { metered_api: "opencode-console-inference-v1" },
  },
  {
    id: "opencode-go",
    displayName: "OpenCode Go",
    adapters: ["opencode-native"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: true,
    keyless: false,
    defaultURL: "https://opencode.ai/inference/go",
    apiKeyBillingLanes: ["subscription"],
    endpointPolicy: { subscription: "opencode-go-inference-v1" },
  },
  {
    id: "google",
    displayName: "Google",
    adapters: ["google-generative-ai", "google-vertex"],
    kinds: ["official", "subscription", "custom_gateway"],
    billingLanes: ["metered_api", "subscription", "custom"],
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
    id: "grok",
    displayName: "Grok subscription",
    adapters: ["grok-cli-chat"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: false,
    keyless: false,
    defaultURL: "https://cli-chat-proxy.grok.com/v1",
    endpointPolicy: { subscription: "grok-cli-proxy-v1" },
  },
  {
    id: "zai-coding-plan",
    displayName: "Z.AI Coding Plan",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: true,
    apiKeyLabel: "Z.AI Coding Plan API key",
    apiKeyBillingLanes: ["subscription"],
    keyless: false,
    defaultURL: "https://api.z.ai/api/coding/paas/v4",
    endpointPolicy: { subscription: "zai-coding-plan-openai-v1" },
  },
  {
    id: "kimi-code-plan-cn",
    displayName: "Kimi Code Plan (China)",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: true,
    apiKeyLabel: "Kimi Code plan key",
    apiKeyBillingLanes: ["subscription"],
    keyless: false,
    defaultURL: "https://api.kimi.com/coding/v1",
    endpointPolicy: { subscription: "kimi-code-plan-cn-v1" },
  },
  {
    id: "kimi-code-plan-global",
    displayName: "Kimi Code Plan (Global)",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: true,
    apiKeyLabel: "Kimi Code plan key",
    apiKeyBillingLanes: ["subscription"],
    keyless: false,
    defaultURL: "https://api.kimi.ai/coding/v1",
    endpointPolicy: { subscription: "kimi-code-plan-global-v1" },
  },
  {
    id: "kilo",
    displayName: "Kilo Gateway",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["official"],
    billingLanes: ["metered_api"],
    apiKey: true,
    keyless: false,
    defaultURL: "https://ai-gateway.kilo.ai/api/v1",
  },
  {
    id: "llmgateway",
    displayName: "LLM Gateway",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["official"],
    billingLanes: ["metered_api"],
    apiKey: true,
    keyless: false,
    defaultURL: "https://api.llmgateway.io/v1",
  },
  {
    id: "llmgateway-devpass",
    displayName: "LLM Gateway DevPass",
    adapters: ["models-dev-openai-compatible"],
    kinds: ["subscription"],
    billingLanes: ["subscription"],
    apiKey: true,
    apiKeyLabel: "DevPass API key",
    apiKeyBillingLanes: ["subscription"],
    keyless: false,
    defaultURL: "https://api.llmgateway.io/v1",
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
// Kept private until the async job adapter and Files commit guard are joined.

export type AuthServices = {
  methods(): Promise<ProviderAuth.Methods>
  authorize(input: {
    providerID: ProviderID
    method: number
    inputs?: Record<string, string>
    flowID: string
    redirectURI: string
    state: string
    expiresAt: number
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
  acquisitionMethod: "browser" | "device"
  expectedCredentialKinds: readonly ("api" | "oauth")[]
  nonce: string
  verifierChallenge: string
  expiresAt: number
  state: OAuthState
  credential?: Auth.Info
  errorCode?: string
  task?: Promise<void>
}

export class ProviderControlError extends Error {
  constructor(message: string, readonly safeCode = "oauth_exchange_failed") {
    super(message)
  }
}

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

function userCode(value: unknown): string | undefined {
  if (value === undefined) return undefined
  if (typeof value !== "string" || !/^[A-Za-z0-9-]{4,64}$/.test(value)) {
    throw new ProviderControlError("OAuth user code is invalid")
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

function capabilityModelFromDiscovery(model: ParsedDiscovery["models"][number]): CapabilityModel {
  const modalities = model.capabilities.modalities
  if (typeof modalities !== "object" || modalities === null || Array.isArray(modalities)) {
    return { id: model.modelID }
  }
  const input = (modalities as Record<string, unknown>).input
  const output = (modalities as Record<string, unknown>).output
  return {
    id: model.modelID,
    modalities: {
      ...(Array.isArray(input) ? { input: input.filter((item): item is string => typeof item === "string") } : {}),
      ...(Array.isArray(output) ? { output: output.filter((item): item is string => typeof item === "string") } : {}),
    },
  }
}

export function retainSupportedOpenCodeModels<T extends { modelID: string }>(familyID: string, models: readonly T[]): T[] {
  if (familyID !== "opencode-console" && familyID !== "opencode-go") return [...models]
  return models.filter((model) => openCodeProtocol(familyID, model.modelID) !== undefined)
}

function frozenModelRoutes(familyID: string, provider: ModelsDev.Provider | undefined, adapterID = "", billingLane: BillingLane = "metered_api"): ProviderModelRoute[] {
  const models = Object.values(provider?.models ?? {})
  if (familyID === "xiaomi" && adapterID === "mimo-native") {
    for (const modelID of ["mimo-v2.6-flash", "mimo-v2.6-pro"]) {
      if (!models.some((model) => model.id === modelID)) models.push({ id: modelID, name: modelID, modalities: { input: ["text"], output: ["text"] } } as ModelsDev.Model)
    }
  }
  if (!models.length) return []
  return models
    .filter((model) => {
      if (familyID !== "opencode-console" && familyID !== "opencode-go") return true
      return openCodeProtocol(familyID, model.id) !== undefined
    })
    .slice(0, MAX_DISCOVERED_MODELS)
    .map((model) => ({
      modelID: model.id,
      displayName: model.name || model.id,
      operations: resolveRouteOperations(familyID, adapterID, billingLane, model.id, model).operations,
      capabilities: {
        ...(openCodeProtocol(familyID, model.id) ? { apiProtocol: openCodeProtocol(familyID, model.id)!.protocol } : {}),
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
        authority: "managed-engine" as const,
          catalog: familyID === "xiaomi" && ["mimo-v2.6-flash", "mimo-v2.6-pro"].includes(model.id)
            ? "xiaomi-official-v2.6-overlay"
            : "models.dev-snapshot",
        familyID,
      },
    }))
    .filter((route) => route.operations.length > 0)
}

type LocalRouteTuple = [string, string, ProviderModelRoute["operations"], string]
const LOCAL_EXECUTOR_RECIPES: LocalRouteTuple[] = [
  ["fastembed/default", "FastEmbed (local)", ["embeddings.create"], "openclank.fastembed.v1"],
  ["kokoro/Kokoro-82M", "Kokoro 82M (local)", ["audio.synthesize"], "openclank.kokoro.v1"],
  ["faster-whisper/default", "Faster Whisper (local)", ["audio.transcribe"], "openclank.whisper.v1"],
  ["diffusion/default", "Diffusion (local)", ["image.generate", "image.edit", "image.inpaint", "image.img2img"], "openclank.diffusion.v1"],
  ["realesrgan/x4plus", "Real-ESRGAN x4plus (local)", ["image.upscale", "image.denoise"], "openclank.realesrgan.v1"],
  ["briaai/RMBG-1.4", "RMBG 1.4 (local)", ["image.segment", "image.remove_background"], "openclank.rmbg.v1"],
  ["gfpgan/clean-v1", "GFPGAN clean (local)", ["image.restore_face"], "openclank.gfpgan.v1"],
]
const LOCAL_EXECUTOR_ROUTES: ProviderModelRoute[] = LOCAL_EXECUTOR_RECIPES.map(([modelID, displayName, operations, localExecutorID]) => ({
  modelID,
  displayName,
  operations,
  capabilities: {
    localExecutorID,
    runtimeStatus: "unknown",
    ...(operations.includes("embeddings.create") ? { batch: true, normalized: true } : {}),
    ...(operations.some((operation) => operation.startsWith("image.") || operation.startsWith("audio.")) ? { artifacts: true } : {}),
    ...(modelID === "kokoro/Kokoro-82M" ? { format: "wav", voice: "af_heart", speed: 1 } : {}),
  },
  provenance: { authority: "managed-engine", catalog: "openclank-local-executors-v1" },
})) as ProviderModelRoute[]

function resolveRouteOperations(
  familyID: string,
  adapterID: string,
  billingLane: BillingLane,
  modelID: string,
  model: CapabilityModel | undefined,
  accountModelAccess = false,
) {
  return resolveManagedCapabilities({
    familyID,
    adapterID,
    billingLane,
    modelID,
    model,
    accountModelAccess,
  })
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
  if (connection.familyID === "opencode-console" || connection.familyID === "opencode-go") {
    return [build(`${path}/v1/models`)]
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
      return retainSupportedOpenCodeModels(connection.familyID, parsed.models).slice(0, MAX_DISCOVERED_MODELS).flatMap((model) => {
        const opencode = connection.familyID === "opencode-console" || connection.familyID === "opencode-go"
          ? openCodeProtocol(connection.familyID, model.modelID)
          : undefined
        const operations = resolveRouteOperations(connection.familyID, connection.adapterID, connection.billingLane, model.modelID, capabilityModelFromDiscovery(model), true).operations
        if (!operations.length) return []
        return [{
          ...model,
          operations,
          capabilities: { ...model.capabilities, ...(opencode ? { apiProtocol: opencode.protocol } : {}), family: connection.familyID },
          provenance: {
            ...model.provenance,
            authority: "managed-engine" as const,
            origin: "account-discovery" as const,
            familyID: connection.familyID,
          },
        }]
      }) as ProviderModelRoute[]
    } catch {
      // Connection creation remains possible while a local server is stopped
      // or while a protected endpoint still needs its optional account.
    }
  }
  return []
}

function subscriptionDiscoveryURLs(connection: ReturnType<typeof validateConnection>, credential: Auth.Info): string[] {
  if (connection.familyID === "kimi-code-plan-cn" || connection.familyID === "kimi-code-plan-global") {
    return connection.normalizedURL ? [`${connection.normalizedURL.replace(/\/+$/, "")}/models`] : []
  }
  if (connection.familyID === "opencode-go" && connection.adapterID === "opencode-native") {
    return ["https://opencode.ai/inference/go/v1/models"]
  }
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
  if (connection.familyID === "grok" && connection.adapterID === "grok-cli-chat") {
    return ["https://cli-chat-proxy.grok.com/v1/models"]
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
  if (connection.familyID === "grok" && connection.adapterID === "grok-cli-chat") return parseGrokCatalog(value)
  if (connection.familyID === "anthropic" && connection.adapterID === "anthropic-messages") return parseClaudeCatalog(value)
  return parseGenericCatalog(value)
}

function discoverySource(connection: ReturnType<typeof validateConnection>): string {
  if (connection.familyID === "openai" && connection.adapterID === "openai-responses") return "codex-account-models"
  if (connection.familyID === "github-copilot" && connection.adapterID === "copilot-chat") return "copilot-account-models"
  return "provider-account-models"
}

function enrichDiscoveryModels(
  connection: ReturnType<typeof validateConnection>,
  discovered: ParsedDiscovery["models"],
  modelCatalog: Record<string, ModelsDev.Provider>,
): ProviderModelRoute[] {
  const metadata = new Map(
    frozenModelRoutes(connection.familyID, modelCatalog[connection.familyID], connection.adapterID, connection.billingLane).map((route) => [route.modelID, route]),
  )
  return retainSupportedOpenCodeModels(connection.familyID, discovered).slice(0, MAX_DISCOVERED_MODELS).map((model) => {
    const known = metadata.get(model.modelID)
    const discoveryProvenance = {
      ...model.provenance,
      authority: "managed-engine" as const,
      origin: "account-discovery" as const,
      familyID: connection.familyID,
    }
    const resolved = resolveRouteOperations(connection.familyID, connection.adapterID, connection.billingLane, model.modelID, capabilityModelFromDiscovery(model), true)
    if (!known) {
      return {
        ...model,
        operations: resolved.operations,
        provenance: discoveryProvenance,
      } as ProviderModelRoute
    }
    const operations = new Set(known.operations)
    if (resolved.operations.includes("embeddings.create")) {
      operations.clear()
      operations.add("embeddings.create")
    }
    if (resolved.operations.includes("web.search")) operations.add("web.search")
    if (model.capabilities.vision === true) operations.add("vision.describe")
    return {
      ...model,
      displayName: known.displayName,
      operations: [...operations],
      capabilities: { ...known.capabilities, ...model.capabilities },
      provenance: { ...discoveryProvenance, metadataCatalog: "models.dev-snapshot" },
    } as ProviderModelRoute
  }).filter((route) => route.operations.length > 0)
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
  retryable = false,
  retryAfterMs?: number,
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
    ...(retryable ? { retryable: true } : {}),
    ...(retryAfterMs === undefined ? {} : { retryAfterMs }),
  }
}

function parseRetryAfter(value: string | null, nowMs: number): number | undefined {
  if (value === null || value.trim() === "") return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds) && seconds >= 0) return Math.min(24 * 60 * 60 * 1000, Math.round(seconds * 1000))
  const date = Date.parse(value)
  if (!Number.isFinite(date)) return undefined
  return Math.min(24 * 60 * 60 * 1000, Math.max(0, date - nowMs))
}

async function accountDiscovery(
  connection: ReturnType<typeof validateConnection>,
  credential: Auth.Info,
  accountID: string,
  credentialRevision: number,
  modelCatalog: Record<string, ModelsDev.Provider>,
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
  if (connection.familyID === "grok" && connection.adapterID === "grok-cli-chat") {
    if (credential.type !== "oauth") {
      return discoveryResult(accountID, credentialRevision, "reauth_required", source, now(), [], "fresh", "reauth_required")
    }
    headers.set("X-XAI-Token-Auth", "xai-grok-cli")
  }
  if (connection.familyID === "openai" && connection.kind === "subscription") {
    headers.set("Origin", "https://chatgpt.com")
    headers.set("Referer", "https://chatgpt.com/codex")
    if (credential.type === "oauth" && credential.accountId) {
      headers.set("ChatGPT-Account-Id", credential.accountId)
    }
  }
  if ((connection.familyID === "kimi-code-plan-cn" || connection.familyID === "kimi-code-plan-global") && credential.type === "oauth") {
    const region = connection.familyID === "kimi-code-plan-cn" ? "cn" : "global"
    if (credential.metadata?.credential_profile !== "kimi-code-device-v1" || credential.metadata.kimi_region !== region) {
      return discoveryResult(accountID, credentialRevision, "reauth_required", source, now(), [], "fresh", "reauth_required")
    }
    headers.set("X-Msh-Platform", "openclank")
    headers.set("X-Msh-Version", String(InstallationVersion))
    headers.set("X-Msh-Device-Id", credential.metadata.device_id)
    headers.set("User-Agent", `openclank/${InstallationVersion}`)
  }

  let sawResponse = false
  let retryable = false
  let retryAfterMs: number | undefined
  for (const url of urls) {
    let response: Response
    try {
      const claudeRequest = connection.familyID === "anthropic" && connection.adapterID === "anthropic-messages"
        && credential.type === "oauth" ? claudeDiscoveryRequest(credential.access) : undefined
      const requestURL = claudeRequest?.url ?? url
      const requestInit = claudeRequest?.init ?? { headers, redirect: "error" as const }
      response = await fetcher(requestURL, { ...requestInit, signal: AbortSignal.timeout(timeoutMs) })
    } catch (error) {
      if (error instanceof DOMException && error.name === "TimeoutError") retryable = true
      else if (error instanceof TypeError) retryable = true
      continue
    }
    try {
      sawResponse = true
      if (response.status === 401 || response.status === 403) {
        return discoveryResult(accountID, credentialRevision, "reauth_required", source, now(), [], "fresh", "reauth_required")
      }
      if (response.status === 429 || response.status >= 500) {
        retryable = true
        retryAfterMs = parseRetryAfter(response.headers.get("retry-after"), now())
        continue
      }
      if (!response.ok) continue
      let rawCatalog: unknown
      let parsed: ParsedDiscovery
      try {
        rawCatalog = await boundedJSON(response)
        parsed = parseAccountCatalog(connection, rawCatalog)
      } catch {
        return discoveryResult(accountID, credentialRevision, "unavailable", source, now(), [], "fresh", "discovery_unavailable")
      }
      const overflow = parsed.models.length > MAX_DISCOVERED_MODELS
      const models = enrichDiscoveryModels(connection, parsed.models, modelCatalog)
      if (connection.familyID === "anthropic" && connection.adapterID === "anthropic-messages") {
        const claude = parseClaudeCatalog(rawCatalog)
        if (claude.status === "malformed") {
          return discoveryResult(accountID, credentialRevision, "unavailable", source, now(), [], "fresh", "discovery_unavailable")
        }
        if (claude.status === "partial") {
          return discoveryResult(accountID, credentialRevision, "partial", source, now(), models, "fresh", "discovery_partial")
        }
      }
      if (parsed.invalidRows > 0 || overflow) {
        return discoveryResult(accountID, credentialRevision, "partial", source, now(), models, "fresh", "discovery_partial")
      }
      return discoveryResult(accountID, credentialRevision, "complete", source, now(), models, "fresh")
    } catch {
      return discoveryResult(accountID, credentialRevision, "unavailable", source, now(), [], "fresh", "discovery_unavailable")
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
    retryable,
    retryAfterMs,
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
  const isLocalExecutor = (familyID === "local-executor" && adapterID === "openclank-local-executor") || familyID === "blender" || ["trellis", "hunyuan-shape"].includes(familyID)
  const requestedURL = input.url === "" ? undefined : input.url
  const defaultURL = familyID === "xiaomi" && billingLane === "subscription" ? undefined : family.defaultURL
  const url = normalizedURL(requestedURL ?? defaultURL, kind, isLocalExecutor)
  if (familyID === "tripo") {
    if (url !== "https://openapi.tripo3d.ai" || Object.keys(settings).some((key) => key !== "interface") ||
        (settings.interface !== undefined && settings.interface !== "tripo-text3d-v1"))
      throw new ProviderControlError("unsupported Tripo profile")
    return {familyID,adapterID,kind,billingLane,normalizedURL:url,settings:{interface:"tripo-text3d-v1"},credentialRequired:true,modelRoutes:[]}
  }
  if (familyID === "fal") {
    if (url !== "https://queue.fal.run" || Object.keys(settings).some((key) => key !== "interface") ||
        (settings.interface !== undefined && settings.interface !== "fal-flux-schnell-v1"))
      throw new ProviderControlError("unsupported fal profile")
    return {familyID,adapterID,kind,billingLane,normalizedURL:url,settings:{interface:"fal-flux-schnell-v1"},credentialRequired:true,modelRoutes:[]}
  }
  if (familyID === "voidless") {
    if (url !== "https://api.voidless.dev" || Object.keys(settings).some((key) => key !== "interface") ||
        (settings.interface !== undefined && settings.interface !== "voidless-image-v1"))
      throw new ProviderControlError("unsupported Voidless profile")
    return {familyID,adapterID,kind,billingLane,normalizedURL:url,settings:{interface:"voidless-image-v1"},credentialRequired:true,modelRoutes:[]}
  }
  if (familyID === "meshy") {
    if (url !== "https://api.meshy.ai" || Object.keys(settings).some((key) => key !== "interface") ||
        (settings.interface !== undefined && settings.interface !== "meshy-preview-v1"))
      throw new ProviderControlError("unsupported Meshy profile")
    return {familyID,adapterID,kind,billingLane,normalizedURL:url,settings:{interface:"meshy-preview-v1"},credentialRequired:true,modelRoutes:[]}
  }
  if (familyID === "sprixen") {
    if (url !== "https://api.sprixen.com" || Object.keys(settings).some((key) => key !== "interface") ||
        (settings.interface !== undefined && settings.interface !== "sprixen-sprite-v1"))
      throw new ProviderControlError("unsupported Sprixen profile")
    return { familyID, adapterID, kind, billingLane, normalizedURL:url,
      settings:{interface:"sprixen-sprite-v1"}, credentialRequired:true, modelRoutes:[] }
  }
  if (familyID === "trellis") {
    const exact=(x:unknown, keys:string[]): x is Record<string,unknown> => !!x && typeof x === "object" && !Array.isArray(x) && Object.keys(x).sort().join(",") === [...keys].sort().join(",")
    const short=(x:unknown,n:number) => typeof x === "string" && x.isWellFormed() && x.length>0 && Buffer.byteLength(x,"utf8")<=n && !/[\u0000-\u001f\u007f]/u.test(x)
    const rel=(x:unknown) => short(x,4096) && !(x as string).startsWith("/") && !(x as string).split("/").some(part=>!part || part === "." || part === "..") && !(x as string).includes("\\")
    const hash=(x:unknown)=>typeof x === "string" && /^[a-f0-9]{64}$/.test(x)
    const i=settings.interpreter, m=settings.manifest
    if (url || !exact(settings,["interface","recipe","installationRoot","interpreter","manifest"]) || settings.interface!=="trellis-image3d-v1" || settings.recipe!=="trellis-image-glb-v1" || !short(settings.installationRoot,4096) || !(settings.installationRoot as string).startsWith("/") || !exact(i,["relativePath","sha256","version"]) || !rel(i.relativePath) || !hash(i.sha256) || !short(i.version,128) || !exact(m,["version","sourceRevision","modelRevision","dinoRevision","components"]) || m.version!==1 || !short(m.sourceRevision,256) || !short(m.modelRevision,256) || !short(m.dinoRevision,256) || !Array.isArray(m.components) || m.components.length<1 || m.components.length>64 || m.components.some(c=>!exact(c,["role","relativePath","size","sha256","acquisitionOperationID"]) || !short(c.role,128) || !rel(c.relativePath) || !Number.isSafeInteger(c.size) || (c.size as number)<1 || !hash(c.sha256) || !(c.acquisitionOperationID===null || short(c.acquisitionOperationID,256)))) throw new ProviderControlError("unsupported TRELLIS profile")
    return {familyID,adapterID,kind,billingLane,settings,credentialRequired:false,modelRoutes:[]}
  }
  if (familyID === "hunyuan-shape") {
    const exact=(v:unknown,keys:string[])=>!!v && typeof v==="object" && !Array.isArray(v) && Object.keys(v).sort().join(",")===keys.sort().join(",")
    const short=(v:unknown,n:number)=>typeof v==="string" && v.isWellFormed() && !!v && new TextEncoder().encode(v).length<=n && !/[\x00-\x1f\x7f]/.test(v)
    const rel=(v:unknown)=>short(v,4096) && !(v as string).startsWith("/") && !(v as string).includes("\\") && !(v as string).split("/").some(x=>!x || x==="." || x==="..")
    const hash=(v:unknown)=>typeof v==="string" && /^[a-f0-9]{64}$/.test(v)
    const i=settings.interpreter as Record<string,unknown>, m=settings.manifest as Record<string,unknown>
    if (url || !exact(settings,["interface","recipe","installationRoot","device","interpreter","manifest"]) || settings.interface!=="hunyuan3d-v2-shape-v1" || settings.recipe!=="hunyuan3d-v2-image-glb-v1" || !["cpu-fp32","cuda-fp16"].includes(String(settings.device)) || !short(settings.installationRoot,4096) || !(settings.installationRoot as string).startsWith("/") || !exact(i,["relativePath","sha256","version"]) || !rel(i.relativePath) || !hash(i.sha256) || !short(i.version,128) || !exact(m,["version","sourceRevision","modelRevision","components"]) || m.version!==1 || !short(m.sourceRevision,256) || !short(m.modelRevision,256) || !Array.isArray(m.components) || m.components.length!==6 || m.components.some(c=>!exact(c,["role","relativePath","size","sha256","acquisitionOperationID"]) || !short(c.role,128) || !rel(c.relativePath) || !Number.isSafeInteger(c.size) || (c.size as number)<1 || !hash(c.sha256) || !(c.acquisitionOperationID===null || short(c.acquisitionOperationID,256)))) throw new ProviderControlError("unsupported Hunyuan profile")
    return {familyID,adapterID,kind,billingLane,settings,credentialRequired:false,modelRoutes:[]}
  }
  if (familyID === "blender") {
    if (url || Object.keys(settings).some((key) => !["interface", "executablePath", "executableSHA256", "executableVersion"].includes(key)) ||
        (settings.interface !== undefined && settings.interface !== "blender-render-v1") ||
        typeof settings.executablePath !== "string" || !settings.executablePath.startsWith("/") || settings.executablePath.length > 4096 ||
        typeof settings.executableSHA256 !== "string" || !/^[0-9a-f]{64}$/.test(settings.executableSHA256) ||
        typeof settings.executableVersion !== "string" || !/^[0-9]+\.[0-9]+\.[0-9]+$/.test(settings.executableVersion))
      throw new ProviderControlError("unsupported Blender profile")
    return { familyID, adapterID, kind, billingLane, settings:{interface:"blender-render-v1", executablePath:settings.executablePath,
      executableSHA256:settings.executableSHA256, executableVersion:settings.executableVersion}, credentialRequired:false, modelRoutes:[] }
  }
  if (familyID === "comfyui") {
    if (!url) throw new ProviderControlError("ComfyUI origin is required")
    const origin = new URL(url)
    if (!["127.0.0.1", "[::1]"].includes(origin.hostname) || origin.pathname !== "/" || url !== origin.origin)
      throw new ProviderControlError("ComfyUI requires an explicit loopback origin")
    if (Object.keys(settings).some((key) => key !== "interface") || (settings.interface !== undefined && settings.interface !== "comfyui-workflow-v1"))
      throw new ProviderControlError("unsupported ComfyUI profile")
    return { familyID, adapterID, kind, billingLane, normalizedURL:url,
      settings:{interface:"comfyui-workflow-v1"}, credentialRequired:false, modelRoutes:[] }
  }
  if (family.endpointPolicy) {
    const expectedPolicy = family.endpointPolicy[billingLane]
    if (!expectedPolicy || !url || url !== family.defaultURL) {
      throw new ProviderControlError("provider endpoint does not match its declared billing lane", "oauth_endpoint_mismatch")
    }
  }
  if (familyID === "kimi-code-plan-cn" || familyID === "kimi-code-plan-global") {
    const expected = familyID === "kimi-code-plan-cn" ? "https://api.kimi.com/coding/v1" : "https://api.kimi.ai/coding/v1"
    if (adapterID !== "models-dev-openai-compatible" || kind !== "subscription" || billingLane !== "subscription" || url !== expected) {
      throw new ProviderControlError("Kimi Code endpoint does not match its selected region", "oauth_endpoint_mismatch")
    }
  }
  return {
    familyID,
    adapterID,
    kind,
    billingLane,
    ...(url ? { normalizedURL: url } : {}),
    settings,
    credentialRequired: !(kind === "local" && family.keyless),
    modelRoutes: isLocalExecutor
      ? LOCAL_EXECUTOR_ROUTES
      : [],
  }
}

export function validateConnection(value: unknown) {
  return validateConnectionWithFamilies(value, FAMILIES)
}

export function validateComfyJobConnection(value: unknown) {
  return validateConnectionWithFamilies(value, new Map([[COMFY_JOB_FAMILY.id, COMFY_JOB_FAMILY], [BLENDER_JOB_FAMILY.id, BLENDER_JOB_FAMILY], [TRELLIS_JOB_FAMILY.id, TRELLIS_JOB_FAMILY], [SPRIXEN_JOB_FAMILY.id, SPRIXEN_JOB_FAMILY], [MESHY_JOB_FAMILY.id, MESHY_JOB_FAMILY], [VOIDLESS_JOB_FAMILY.id, VOIDLESS_JOB_FAMILY], [TRIPO_JOB_FAMILY.id, TRIPO_JOB_FAMILY]]))
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
  const acquisitionMethod = input.acquisitionMethod === undefined || input.acquisitionMethod === null
    ? undefined
    : text(input.acquisitionMethod, "provider acquisition method", 32)
  const credentialKind = input.credentialKind === undefined ? undefined : text(input.credentialKind, "provider credential kind", 16)
  if (authMethod === "api_key") {
    if (!family.apiKey) throw new ProviderControlError("this provider connection does not accept API-key accounts")
    if (connection.billingLane === "subscription" && !(
      (connection.familyID === "xiaomi" && connection.adapterID === "mimo-native") ||
      (connection.familyID === "opencode-go" && connection.adapterID === "opencode-native") ||
      (connection.familyID === "llmgateway-devpass" && connection.adapterID === "models-dev-openai-compatible") ||
      (connection.familyID === "zai-coding-plan" && connection.adapterID === "models-dev-openai-compatible") ||
      ((connection.familyID === "kimi-code-plan-cn" || connection.familyID === "kimi-code-plan-global") && connection.adapterID === "models-dev-openai-compatible")
    )) {
      throw new ProviderControlError("API keys cannot use the subscription billing lane")
    }
    if (credential.type !== "api") throw new ProviderControlError("provider credential type is not supported")
    if (credentialKind !== undefined && credentialKind !== "api") throw new ProviderControlError("credential kind does not match the credential")
    if (acquisitionMethod !== undefined && !["manual-key", "browser", "device"].includes(acquisitionMethod)) throw new ProviderControlError("provider acquisition method is not supported")
    const key = text(credential.key, "provider API key", 131_072)
    const metadata = credential.metadata
    if (metadata !== undefined) {
      const values = object(metadata, "provider credential metadata")
      for (const [name, item] of Object.entries(values)) {
        text(name, "provider credential metadata name", 128)
        text(item, "provider credential metadata value", 2048)
      }
    }
    let normalizedCredential: Auth.Info = {
      type: "api",
      key,
      ...(metadata === undefined ? {} : { metadata: metadata as Record<string, string> }),
    }
    if (connection.familyID === "kimi-code-plan-cn" || connection.familyID === "kimi-code-plan-global") {
      if (acquisitionMethod !== undefined && acquisitionMethod !== "manual-key") {
        throw new ProviderControlError("Kimi Code plan keys require manual key entry")
      }
    }
    if (connection.familyID === "xiaomi" && connection.adapterID === "mimo-native") {
      const endpoint = resolveMimoEndpoint({
        lane: connection.billingLane as MimoBillingLane,
        credentialURL: normalizedCredential.metadata?.base_url,
        connectionURL: connection.normalizedURL,
      })
      const expected = family.endpointPolicy?.[connection.billingLane]
      if (!endpoint || endpoint.policy !== expected) throw new ProviderControlError("MiMo credential endpoint does not match its billing lane", "oauth_endpoint_mismatch")
      normalizedCredential = {
        ...normalizedCredential,
        metadata: { ...normalizedCredential.metadata, base_url: endpoint.url },
      }
    }
    return {
      authMethod: "api_key" as const,
      ...(acquisitionMethod === undefined ? {} : { acquisitionMethod: acquisitionMethod as "manual-key" | "browser" | "device" }),
      credentialKind: "api" as const,
      ...(connection.familyID === "xiaomi" && connection.adapterID === "mimo-native"
        ? { endpointPolicy: family.endpointPolicy?.[connection.billingLane] as "mimo-metered-api-v1" | "mimo-token-plan-v1" | undefined }
        : {}),
      ...(family.endpointPolicy ? { endpointPolicy: family.endpointPolicy[connection.billingLane] } : {}),
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
  if (connection.familyID === "xiaomi" && connection.adapterID === "mimo-native") {
    throw new ProviderControlError("MiMo Token Plan requires an API credential")
  }
  if (connection.familyID === "grok" && acquisitionMethod !== "device") {
    throw new ProviderControlError("Grok subscription accounts require device authorization")
  }
  const kimiRegion = connection.familyID === "kimi-code-plan-cn" ? "cn" : connection.familyID === "kimi-code-plan-global" ? "global" : undefined
  if (kimiRegion !== undefined) {
    const metadata = credential.metadata === undefined ? undefined : object(credential.metadata, "Kimi Code OAuth metadata")
    if (
      connection.adapterID !== "models-dev-openai-compatible" ||
      acquisitionMethod !== "device" ||
      metadata?.kimi_region !== kimiRegion ||
      metadata?.credential_profile !== "kimi-code-device-v1" ||
      typeof metadata?.device_id !== "string" ||
      !/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(String(metadata?.device_id ?? "")) ||
      Object.keys(metadata ?? {}).some((key) => !["device_id", "kimi_region", "credential_profile"].includes(key))
    ) {
      throw new ProviderControlError("Kimi Code OAuth metadata does not match its selected family", "oauth_endpoint_mismatch")
    }
  }
  if (credentialKind !== undefined && credentialKind !== "oauth") throw new ProviderControlError("credential kind does not match the credential")
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
    ...(kimiRegion === undefined ? {} : { metadata: credential.metadata as Auth.Oauth["metadata"] }),
  }
  if (connection.familyID === "github-copilot" && connection.settings.enterpriseUrl !== undefined) {
    const configuredEnterprise = normalizedEnterpriseHost(connection.settings.enterpriseUrl)
    if (configuredEnterprise !== normalizedCredential.enterpriseUrl) {
      throw new ProviderControlError("provider enterprise URL does not match the OAuth credential")
    }
  }
  return {
    authMethod: "oauth" as const,
    ...(acquisitionMethod === undefined ? {} : { acquisitionMethod: acquisitionMethod as "manual-key" | "browser" | "device" }),
    credentialKind: "oauth" as const,
    ...(family.endpointPolicy ? { endpointPolicy: family.endpointPolicy[connection.billingLane] } : {}),
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
    if (connection.familyID === "comfyui" || connection.familyID === "blender" || ["trellis", "hunyuan-shape"].includes(connection.familyID) || connection.familyID === "sprixen" || connection.familyID === "meshy" || connection.familyID === "tripo" || connection.familyID === "fal" || connection.familyID === "voidless") return []
    if (connection.kind === "subscription") return []
    if (connection.familyID === "google" && connection.adapterID === "google-vertex") return []
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
    return frozenModelRoutes(connection.familyID, catalog[connection.familyID], connection.adapterID, connection.billingLane)
  }

  async connectionValidate(value: unknown) {
    if (["comfyui", "blender", "trellis", "hunyuan-shape", "sprixen", "meshy", "tripo", "fal", "voidless"].includes(String(object(value, "provider connection").familyID))) return validateConnection(value)
    const modelCatalog = await this.auth.modelCatalog().catch(() => ({}))
    const connection = validateConnectionWithFamilies(value, familyMap(modelCatalog))
    return { ...connection, modelRoutes: await this.models(connection, undefined, modelCatalog) }
  }

  async accountValidate(value: unknown) {
    const input = object(value, "provider account")
    const jobFamily = object(input.connection, "provider connection").familyID
    const jobDefinition = jobFamily === "sprixen" ? SPRIXEN_JOB_FAMILY : jobFamily === "meshy" ? MESHY_JOB_FAMILY : jobFamily === "tripo" ? TRIPO_JOB_FAMILY : jobFamily === "fal" ? FAL_JOB_FAMILY : jobFamily === "voidless" ? VOIDLESS_JOB_FAMILY : undefined
    const modelCatalog = jobDefinition ? {} : await this.auth.modelCatalog().catch(() => ({}))
    const families = jobDefinition ? new Map([[jobDefinition.id, jobDefinition]]) : familyMap(modelCatalog)
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
            modelCatalog,
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
      acquisitionMethod: account.acquisitionMethod ?? null,
      credentialKind: account.credentialKind,
      ...(account.endpointPolicy ? { endpointPolicy: account.endpointPolicy } : {}),
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
            authMethod: flow.credential.type === "api" ? "api_key" : "oauth",
            acquisitionMethod: flow.acquisitionMethod,
            credentialKind: flow.credential.type,
            ...(flow.providerID === "xiaomi" && flow.credential.type === "api"
              ? { endpointPolicy: resolveMimoEndpoint({
                  lane: flow.billingLane as MimoBillingLane,
                  credentialURL: flow.credential.metadata?.base_url,
                })?.policy }
              : {}),
            ...(FAMILIES.get(flow.providerID)?.endpointPolicy
              ? { endpointPolicy: FAMILIES.get(flow.providerID)!.endpointPolicy![flow.billingLane] }
              : {}),
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
      if ((credential.type !== "api" && credential.type !== "oauth") || !flow.expectedCredentialKinds.includes(credential.type)) {
        throw new ProviderControlError("provider credential kind does not match the selected auth method", "oauth_credential_mismatch")
      }
      let validatedCredential = credential
      if (credential.type === "api" && flow.providerID === "xiaomi") {
        const endpoint = resolveMimoEndpoint({
          lane: flow.billingLane as MimoBillingLane,
          credentialURL: credential.metadata?.base_url,
        })
        const expected = FAMILIES.get("xiaomi")?.endpointPolicy?.[flow.billingLane]
        if (!endpoint || endpoint.policy !== expected) throw new ProviderControlError("MiMo credential endpoint does not match its billing lane", "oauth_endpoint_mismatch")
        validatedCredential = { ...credential, metadata: { ...credential.metadata, base_url: endpoint.url } }
      }
      // A cancellation may race the awaited provider exchange.  TypeScript's
      // local narrowing cannot observe that external mutation, so re-read the
      // shared flow state explicitly after the await.
      const currentState = flow.state as PendingOAuth["state"]
      if (currentState === "cancelled" || flow.expiresAt <= this.now()) {
        flow.state = currentState === "cancelled" ? "cancelled" : "expired"
        flow.errorCode = flow.state === "expired" ? "oauth_expired" : "oauth_cancelled"
        return
      }
      flow.credential = validatedCredential
      flow.state = "complete"
    } catch (error) {
      if (flow.state !== "cancelled" && flow.state !== "expired") {
        flow.state = "failed"
        flow.errorCode = error instanceof ProviderControlError ? error.safeCode : "oauth_exchange_failed"
      }
    }
  }

  async catalog() {
    const [methods, modelCatalog] = await Promise.all([this.auth.methods(), this.auth.modelCatalog()])
    return {
      schemaVersion: 1,
      families: familyDefinitions(modelCatalog).map((family) => {
        const providerMethods = FAMILIES.has(family.id) ? (methods[family.id] ?? []) : []
        const compatibleProviderMethods = providerMethods.flatMap((method, index) => {
          const billingLanes = (method.billingLanes ?? [...family.billingLanes]).filter((lane) => family.billingLanes.includes(lane))
          return billingLanes.length > 0 ? [{ method, index, billingLanes }] : []
        })
        const authMethods = [
          ...(family.apiKey ? [{
            id: "api_key",
            type: "api" as const,
            label: family.apiKeyLabel ?? "API key",
            acquisitionMethod: "manual-key" as const,
            credentialKinds: ["api"] as const,
            billingLanes: [...(family.apiKeyBillingLanes ?? family.billingLanes.filter((lane) => lane !== "subscription"))],
          }] : []),
          ...compatibleProviderMethods.map(({ method, index, billingLanes }) => ({
            id: `oauth:${index}`,
            type: method.type,
            label: method.label,
            acquisitionMethod: method.acquisitionMethod ?? (method.type === "oauth" ? "browser" as const : "manual-key" as const),
            credentialKinds: method.credentialKinds ?? [method.type === "oauth" ? "oauth" as const : "api" as const],
            billingLanes,
            ...(method.prompts ? { prompts: method.prompts } : {}),
          })),
          ...(family.keyless ? [{ id: "none", type: "none" as const, label: "No credential" }] : []),
        ]
        return {
          id: family.id,
          displayName: family.displayName,
          adapters: family.adapters.filter((adapter) => adapter !== "google-vertex"),
          kinds: [...family.kinds],
          billingLanes: [...family.billingLanes],
          ...(family.endpointPolicy ? { endpointPolicies: { ...family.endpointPolicy } } : {}),
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
    if ((identity.providerID === "kimi-code-plan-cn" || identity.providerID === "kimi-code-plan-global") && methodCatalog[method]?.acquisitionMethod !== "device") {
      throw new ProviderControlError("Kimi Code plan requires its device authorization method")
    }
    if (methodCatalog[method]?.billingLanes && !methodCatalog[method]!.billingLanes!.includes(identity.billingLane as Exclude<BillingLane, "legacy">)) {
      throw new ProviderControlError("OAuth method does not support this billing lane")
    }
    if (identity.providerID === "grok" && methodCatalog[method]?.acquisitionMethod !== "device") {
      throw new ProviderControlError("Grok subscription requires device authorization")
    }
    const verifierChallenge = text(input.codeVerifierChallenge, "OAuth verifier challenge", 128)
    if (!/^[A-Za-z0-9_-]{43}$/.test(verifierChallenge))
      throw new ProviderControlError("OAuth verifier challenge is invalid")
    const expiresAt = integer(input.expiresAt, "OAuth expiry", 1)
    const now = this.now()
    if (expiresAt <= now || expiresAt > now + FLOW_TTL_MS) throw new ProviderControlError("OAuth expiry is invalid")
    const flow: PendingOAuth = {
      ...identity,
      method,
      acquisitionMethod: methodCatalog[method]?.acquisitionMethod === "device" ? "device" : "browser",
      expectedCredentialKinds: methodCatalog[method]?.credentialKinds ?? ["oauth"],
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
        expiresAt: flow.expiresAt,
      })
    } catch (error) {
      this.flows.delete(flow.flowID)
      throw error
    }
    if (!authorization) {
      this.flows.delete(flow.flowID)
      throw new ProviderControlError("OAuth authorization did not start")
    }
    const publicUserCode = userCode(authorization.userCode)
    if (authorization.method === "auto") {
      flow.task = this.exchange(flow)
    }
    return {
      flowID: flow.flowID,
      url: authorization.url,
      method: authorization.method,
      instructions: authorization.instructions,
      ...(publicUserCode === undefined ? {} : { userCode: publicUserCode }),
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
      flow.task = this.exchange(flow, input.code === undefined ? undefined : text(input.code, "OAuth code", 131_072))
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
