/** Native Console/Go model API mappings from the current OpenCode endpoint tables.
 * Account discovery decides which IDs are available; this map decides only
 * whether the bundled engine has a verified wire adapter for that ID.
 */
export type OpenCodeFamily = "opencode-console" | "opencode-go"
export type OpenCodeProtocol = "openai-responses" | "anthropic-messages" | "openai-chat" | "google-generative-ai"

export type OpenCodeProtocolDescriptor = {
  protocol: OpenCodeProtocol
  sdkPackage: "@ai-sdk/openai" | "@ai-sdk/anthropic" | "@ai-sdk/openai-compatible" | "@ai-sdk/google"
  apiSuffix: "/openai/v1" | "/anthropic/v1" | "/google/v1beta"
}

const descriptor = {
  "openai-responses": { protocol: "openai-responses", sdkPackage: "@ai-sdk/openai", apiSuffix: "/openai/v1" },
  "anthropic-messages": { protocol: "anthropic-messages", sdkPackage: "@ai-sdk/anthropic", apiSuffix: "/anthropic/v1" },
  "openai-chat": { protocol: "openai-chat", sdkPackage: "@ai-sdk/openai-compatible", apiSuffix: "/openai/v1" },
  "google-generative-ai": { protocol: "google-generative-ai", sdkPackage: "@ai-sdk/google", apiSuffix: "/google/v1beta" },
} as const satisfies Record<OpenCodeProtocol, OpenCodeProtocolDescriptor>

const consoleModels: Readonly<Record<OpenCodeProtocol, readonly string[]>> = {
  "openai-responses": [
    "gpt-6-astra", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-luna", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-pro", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.3-codex",
    "gpt-5.3-codex-spark", "gpt-5.2", "gpt-5.2-codex", "gpt-5.1", "gpt-5.1-codex", "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini", "gpt-5", "gpt-5-codex", "grok-4.7", "grok-4.6", "grok-4.5", "grok-build-0.1",
    "muse-wonder-1.0", "muse-spark-1.3", "muse-spark-1.2",
  ],
  "anthropic-messages": [
    "claude-fable-5-1", "claude-fable-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
    "claude-opus-4-6", "claude-opus-4-5", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6",
    "claude-sonnet-4-5", "claude-haiku-5-5", "claude-haiku-4-5", "qwen3.8-flash", "qwen3.6-plus", "qwen3.5-plus",
  ],
  "openai-chat": [
    "qwen3.8-max", "deepseek-v4.1-flash", "deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "glm-5.1", "glm-5", "kimi-k2.5", "kimi-k2.6", "kimi-k2.7-code",
    "kimi-k3", "mistral-large-4",
  ],
  "google-generative-ai": [
    "gemini-4-argon", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash",
    "gemini-3.5-flash-lite", "gemini-3.1-pro", "gemini-3-flash",
  ],
}

const goModels: Readonly<Record<OpenCodeProtocol, readonly string[]>> = {
  "openai-responses": ["grok-4.7", "gpt-6-luna", "gpt-5.6-luna", "muse-spark-1.3-contributor", "muse-spark-1.2-contributor"],
  "anthropic-messages": ["claude-haiku-5-5", "minimax-m3", "qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus"],
  "openai-chat": [
    "glm-5.3-flash", "glm-5.3", "glm-5.2", "kimi-k3.1-pro", "kimi-k3", "kimi-k2.7-code", "longcat-2.0",
    "deepseek-v4.1-flash", "deepseek-v4-pro", "mimo-v2.6-flash", "mimo-v2.6-pro", "mimo-v2.5", "mimo-v2.5-pro",
    "minimax-m3.1-flash", "hy4-preview", "hy3", "step-5-preview", "beam-free", "iceman-contributor-free",
  ],
  "google-generative-ai": ["gemini-4-argon"],
}

function index(models: Readonly<Record<OpenCodeProtocol, readonly string[]>>) {
  const result = new Map<string, OpenCodeProtocolDescriptor>()
  for (const protocol of Object.keys(models) as OpenCodeProtocol[]) {
    for (const id of models[protocol]) result.set(id, descriptor[protocol])
  }
  return result
}

const byFamily = {
  "opencode-console": index(consoleModels),
  "opencode-go": index(goModels),
} satisfies Record<OpenCodeFamily, Map<string, OpenCodeProtocolDescriptor>>

export function openCodeProtocol(familyID: string, modelID: string): OpenCodeProtocolDescriptor | undefined {
  if (familyID !== "opencode-console" && familyID !== "opencode-go") return undefined
  return byFamily[familyID].get(modelID)
}

export function openCodeApiBaseURL(endpoint: string, protocol: OpenCodeProtocol): string {
  const suffix = descriptor[protocol].apiSuffix
  return `${endpoint.replace(/\/+$/, "")}${suffix}`
}
