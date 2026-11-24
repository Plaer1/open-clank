/** Reviewed, conservative operation recipes for managed model routes.
 *
 * This module is deliberately pure. Wiring into discovery/validation belongs
 * to the provider-control integration packet so recipes cannot become a
 * second provider authority.
 */

export type CapabilityOperation =
  | "chat.stream"
  | "chat.complete"
  | "vision.describe"
  | "embeddings.create"
  | "web.search"
  | "image.generate"
  | "audio.synthesize"
  | "audio.transcribe"

export type CapabilityModel = {
  id?: string
  modalities?: { input?: readonly string[]; output?: readonly string[] }
}

export type CapabilityInput = {
  familyID: string
  adapterID: string
  billingLane: string
  modelID: string
  model?: CapabilityModel
  /** Existing account/inventory evidence for customized Xiaomi access. */
  accountModelAccess?: boolean
}

export type CapabilityResolution = {
  operations: readonly CapabilityOperation[]
  searchRecipe?: "xiaomi-chat-completions-v2.6"
  reason?: "unknown-model" | "ultraspeed-access-unverified"
}

export function transformXiaomiSearchRequestBody(
  body: Record<string, unknown>,
  input: { count: number; answer: boolean },
): Record<string, unknown> {
  return {
    ...body,
    tools: [{ type: "web_search", max_keyword: 1, force_search: true, limit: input.count }],
    thinking: { type: "disabled" },
    ...(input.answer ? { max_completion_tokens: 512 } : { max_completion_tokens: 256 }),
  }
}

const EMBEDDING_RECIPES = new Set([
  "openai:text-embedding-3-small",
  "openai:text-embedding-3-large",
  "openai:text-embedding-ada-002",
  "google:gemini-embedding-001",
])

const XIAOMI_SEARCH_MODELS = new Set(["mimo-v2.6-flash", "mimo-v2.6-pro"])
const XIAOMI_ULTRASPEED = "mimo-v2.6-pro-ultraspeed"

function ordinaryOperations(model: CapabilityModel | undefined): CapabilityOperation[] {
  if (!model?.modalities) return ["chat.stream", "chat.complete"]
  const input = new Set(model.modalities.input ?? ["text"])
  const output = new Set(model.modalities.output ?? ["text"])
  const operations: CapabilityOperation[] = []
  if (output.has("text")) {
    operations.push("chat.stream", "chat.complete")
  }
  if (input.has("image") && output.has("text")) operations.push("vision.describe")
  return operations
}

export function resolveManagedCapabilities(input: CapabilityInput): CapabilityResolution {
  const modelID = input.modelID || input.model?.id || ""
  const recipeKey = `${input.familyID}:${modelID}`
  if ((input.adapterID === "openai-responses" || input.adapterID === "openai-chat")
    && input.familyID === "openai"
    && EMBEDDING_RECIPES.has(recipeKey)) {
    return { operations: ["embeddings.create"] }
  }
  if (input.familyID === "google" && input.adapterID === "google-generative-ai"
    && modelID === "gemini-embedding-001") {
    return { operations: ["embeddings.create"] }
  }

  const ordinary = ordinaryOperations(input.model)
  if (input.familyID === "xiaomi" && input.adapterID === "mimo-native"
    && (input.billingLane === "metered_api" || input.billingLane === "subscription")) {
    if (XIAOMI_SEARCH_MODELS.has(modelID)) {
      return {
        operations: [...ordinary, "web.search"],
        searchRecipe: "xiaomi-chat-completions-v2.6",
      }
    }
    if (modelID === XIAOMI_ULTRASPEED) {
      if (!input.accountModelAccess) {
        return { operations: ordinary, reason: "ultraspeed-access-unverified" }
      }
      return {
        operations: [...ordinary, "web.search"],
        searchRecipe: "xiaomi-chat-completions-v2.6",
      }
    }
  }

  // Ordinary modality declarations are descriptive only. Provider-specific
  // media operations require a reviewed recipe and are never inferred here.
  return { operations: ordinary }
}

