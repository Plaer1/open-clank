import { describe, expect, test } from "bun:test"
import { ACP } from "../../src/acp/agent"
import { ModelsDev, Provider } from "../../src/provider"

const source = {
  id: "openai",
  name: "OpenAI",
  env: [],
  api: "https://api.openai.com/v1",
  npm: "@ai-sdk/openai",
  models: {
    "gpt-5.6-luna": {
      id: "gpt-5.6-luna",
      name: "Luna",
      family: "gpt",
      release_date: "2026-07-21",
      attachment: true,
      reasoning: true,
      temperature: false,
      tool_call: true,
      limit: { context: 1_050_000, input: 922_000, output: 128_000 },
      experimental: {
        modes: {
          fast: { provider: { body: { service_tier: "priority" } } },
          pro: { provider: { body: { reasoning: { mode: "pro" } } } },
        },
      },
    },
  },
} as unknown as ModelsDev.Provider

describe("ACP model relationship metadata", () => {
  test("models.dev modes retain their real base model and preset", () => {
    const models = Provider.fromModelsDevProvider(source).models

    expect(models["gpt-5.6-luna"]).toMatchObject({ baseModelId: "openai/gpt-5.6-luna" })
    expect(models["gpt-5.6-luna"]).not.toHaveProperty("preset")
    expect(models["gpt-5.6-luna-fast"]).toMatchObject({
      baseModelId: "openai/gpt-5.6-luna",
      preset: "fast",
    })
    expect(models["gpt-5.6-luna-pro"]).toMatchObject({
      baseModelId: "openai/gpt-5.6-luna",
      preset: "pro",
    })
  })

  test("available models distinguish presets from reasoning variants", () => {
    const available = ACP.buildAvailableModels([Provider.fromModelsDevProvider(source)], { includeVariants: true })

    expect(available.find((model) => model.modelId === "openai/gpt-5.6-luna")).toEqual({
      modelId: "openai/gpt-5.6-luna",
      name: "OpenAI/Luna",
      baseModelId: "openai/gpt-5.6-luna",
    })
    expect(available.find((model) => model.modelId === "openai/gpt-5.6-luna-fast")).toEqual({
      modelId: "openai/gpt-5.6-luna-fast",
      name: "OpenAI/Luna Fast",
      baseModelId: "openai/gpt-5.6-luna",
      preset: "fast",
    })
    const standardHigh = available.find((model) => model.modelId === "openai/gpt-5.6-luna/high")
    expect(standardHigh).toMatchObject({
      baseModelId: "openai/gpt-5.6-luna",
      variant: "high",
    })
    expect(standardHigh).not.toHaveProperty("preset")
    expect(available.find((model) => model.modelId === "openai/gpt-5.6-luna-pro/high")).toMatchObject({
      baseModelId: "openai/gpt-5.6-luna",
      preset: "pro",
      variant: "high",
    })
  })
})
