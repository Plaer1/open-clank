import { describe, expect, test } from "bun:test"
import { resolveManagedCapabilities } from "@/acp/provider-capabilities"

describe("managed capability recipes", () => {
  test.each([
    ["text-embedding-3-small", "openai-responses"],
    ["text-embedding-3-large", "openai-chat"],
    ["text-embedding-ada-002", "openai-responses"],
  ])("resolves OpenAI %s as embeddings only", (modelID, adapterID) => {
    expect(resolveManagedCapabilities({ familyID: "openai", adapterID, billingLane: "metered_api", modelID }))
      .toEqual({ operations: ["embeddings.create"] })
  })

  test("resolves Google's exact embedding identity", () => {
    expect(resolveManagedCapabilities({
      familyID: "google", adapterID: "google-generative-ai", billingLane: "metered_api",
      modelID: "gemini-embedding-001",
    })).toEqual({ operations: ["embeddings.create"] })
  })

  test("preserves ordinary chat and vision without inventing media operations", () => {
    expect(resolveManagedCapabilities({
      familyID: "openai", adapterID: "openai-responses", billingLane: "metered_api",
      modelID: "vision-chat", model: { modalities: { input: ["image", "audio"], output: ["audio", "text"] } },
    }).operations).toEqual(["chat.stream", "chat.complete", "vision.describe"])
  })

  test("adds Xiaomi search only for current documented candidates", () => {
    expect(resolveManagedCapabilities({
      familyID: "xiaomi", adapterID: "mimo-native", billingLane: "metered_api", modelID: "mimo-v2.6-flash",
    })).toEqual({
      operations: ["chat.stream", "chat.complete", "web.search"], searchRecipe: "xiaomi-chat-completions-v2.6",
    })
    expect(resolveManagedCapabilities({
      familyID: "xiaomi", adapterID: "mimo-native", billingLane: "metered_api", modelID: "mimo-v2.5",
    })).toEqual({ operations: ["chat.stream", "chat.complete"] })
  })

  test("requires account evidence for ultraspeed and rejects unrelated families", () => {
    expect(resolveManagedCapabilities({
      familyID: "xiaomi", adapterID: "mimo-native", billingLane: "metered_api",
      modelID: "mimo-v2.6-pro-ultraspeed",
    }).reason).toBe("ultraspeed-access-unverified")
    expect(resolveManagedCapabilities({
      familyID: "openrouter", adapterID: "openai-chat", billingLane: "metered_api", modelID: "mimo-v2.6-flash",
    }).operations).not.toContain("web.search")
  })

  test("does not infer embeddings, chat, or media from arbitrary names", () => {
    const result = resolveManagedCapabilities({
      familyID: "openai", adapterID: "openai-responses", billingLane: "metered_api",
      modelID: "my-embedding-ish-model", model: { modalities: { output: ["image", "audio"] } },
    })
    expect(result.operations).toEqual([])
  })

  test("does not invent chat or media operations for pure media output", () => {
    expect(resolveManagedCapabilities({
      familyID: "openai", adapterID: "openai-responses", billingLane: "metered_api",
      modelID: "image-only", model: { modalities: { input: ["text"], output: ["image"] } },
    }).operations).toEqual([])
    expect(resolveManagedCapabilities({
      familyID: "openai", adapterID: "openai-responses", billingLane: "metered_api",
      modelID: "audio-only", model: { modalities: { input: ["text"], output: ["audio"] } },
    }).operations).toEqual([])
  })
})

