import { expect, test } from "bun:test"
import type { LanguageModelV3 } from "@ai-sdk/provider"
import { Auth } from "../../src/auth"
import { Provider } from "../../src/provider"
import { ProviderID } from "../../src/provider/schema"

function metadata(
  familyID: string,
  adapterID: string,
  billingLane: Auth.BillingLane = "metered_api",
): Provider.ManagedAdapterMetadata {
  return { familyID, adapterID, billingLane, connectionID: "conn-1" }
}

function oauth(overrides: Partial<Auth.Oauth> = {}): Auth.Oauth {
  return {
    type: "oauth",
    access: "leased-access",
    refresh: "leased-refresh",
    expires: Date.now() + 60_000,
    ...overrides,
  }
}

function api(): Auth.Api {
  return { type: "api", key: "leased-api-key" }
}

test("managed metadata binds connection, lane, family, adapter, and model route", () => {
  const provider = {
    id: ProviderID.make("conn-1"),
    options: {
      _openclankFamilyID: "openai",
      _openclankAdapterID: "openai-responses",
      _openclankBillingLane: "subscription",
      _openclankConnectionID: "conn-1",
    },
  }
  const model = {
    providerID: ProviderID.make("conn-1"),
    options: { _openclankAdapterID: "openai-responses" },
  }
  expect(
    Provider.managedAdapterMetadata(
      provider as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "providerID" | "options">,
    ),
  ).toEqual({
    familyID: "openai",
    adapterID: "openai-responses",
    billingLane: "subscription",
    connectionID: "conn-1",
  })

  expect(() =>
    Provider.managedAdapterMetadata(
      { ...provider, id: ProviderID.make("conn-2") } as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "providerID" | "options">,
    ),
  ).toThrow("does not match its connection ID")
  expect(() =>
    Provider.managedAdapterMetadata(
      {
        ...provider,
        options: { ...provider.options, _openclankFamilyID: "anthropic" },
      } as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "providerID" | "options">,
    ),
  ).toThrow("invalid family/adapter pairing")
})

test("managed adapters map only to bundled native packages", () => {
  const cases = [
    ["anthropic", "anthropic-messages", "@ai-sdk/anthropic"],
    ["openai", "openai-responses", "@ai-sdk/openai"],
    ["openai-compatible", "openai-responses", "@ai-sdk/openai-compatible"],
    ["deepseek", "openai-chat", "@ai-sdk/openai-compatible"],
    ["openrouter", "openai-chat", "@openrouter/ai-sdk-provider"],
    ["github-copilot", "copilot-chat", "@ai-sdk/github-copilot"],
    ["xiaomi", "mimo-native", "@ai-sdk/openai-compatible"],
    ["google", "google-generative-ai", "@ai-sdk/google"],
    ["google", "google-vertex", "@ai-sdk/google-vertex"],
    ["xai", "xai-responses", "@ai-sdk/xai"],
    ["ollama", "ollama", "@ai-sdk/openai-compatible"],
    ["zeta", "models-dev-openai-compatible", "@ai-sdk/openai-compatible"],
    ["alpha", "models-dev-anthropic", "@ai-sdk/anthropic"],
  ] as const
  for (const [familyID, adapterID, expected] of cases) {
    expect(Provider.managedAdapterPackage({ familyID, adapterID })).toBe(expected)
  }
  expect(Provider.managedModelsDevAdapterID("@ai-sdk/openai-compatible")).toBe("models-dev-openai-compatible")
  expect(Provider.managedModelsDevAdapterID("@ai-sdk/anthropic")).toBe("models-dev-anthropic")
  expect(Provider.managedModelsDevAdapterID("@ai-sdk/groq")).toBeUndefined()
  expect(() => Provider.managedAdapterPackage({ familyID: "zeta", adapterID: "models-dev-groq" })).toThrow(
    "unsupported adapter",
  )
  expect(() =>
    Provider.managedAdapterPackage({ familyID: "openai", adapterID: "models-dev-openai-compatible" }),
  ).toThrow("invalid family/adapter pairing")
})

test("models.dev adapters cannot shadow a curated managed family", () => {
  expect(() =>
    Provider.managedAdapterMetadata({
      id: ProviderID.make("conn-1"),
      options: {
        _openclankFamilyID: "anthropic",
        _openclankAdapterID: "models-dev-anthropic",
        _openclankBillingLane: "metered_api",
        _openclankConnectionID: "conn-1",
      },
    }),
  ).toThrow("invalid family/adapter pairing")
})

test("normalized connection IDs still select responses, chat, and Copilot transports", () => {
  const called: string[] = []
  const sdk = {
    languageModel(id: string) {
      called.push(`language:${id}`)
      return {} as LanguageModelV3
    },
    chat(id: string) {
      called.push(`chat:${id}`)
      return {} as LanguageModelV3
    },
    responses(id: string) {
      called.push(`responses:${id}`)
      return {} as LanguageModelV3
    },
  }
  Provider.selectManagedLanguageModel(sdk, { adapterID: "openai-responses" }, "gpt-5.6")
  Provider.selectManagedLanguageModel(sdk, { adapterID: "openai-chat" }, "router-model")
  Provider.selectManagedLanguageModel(sdk, { adapterID: "copilot-chat" }, "gpt-5.6")
  Provider.selectManagedLanguageModel(sdk, { adapterID: "copilot-chat" }, "gpt-4.1")
  expect(called).toEqual(["responses:gpt-5.6", "chat:router-model", "responses:gpt-5.6", "chat:gpt-4.1"])
})

test("managed options keep secrets out of projection metadata and restore native headers", () => {
  const anthropic = Provider.applyManagedAdapterOptions(metadata("anthropic", "anthropic-messages"), api(), {
    _openclankFamilyID: "anthropic",
    _openclankAdapterID: "anthropic-messages",
    _openclankBillingLane: "metered_api",
    _openclankConnectionID: "conn-1",
    _openclankSettings: { timeout: 1 },
    headers: { Authorization: "Bearer projection-secret", "x-api-key": "projection-secret" },
  })
  expect(anthropic._openclankFamilyID).toBeUndefined()
  expect(anthropic._openclankSettings).toBeUndefined()
  expect(anthropic.headers.Authorization).toBeUndefined()
  expect(anthropic.headers["x-api-key"]).toBeUndefined()
  expect(anthropic.headers["anthropic-beta"]).toContain("interleaved-thinking")

  const openrouter = Provider.applyManagedAdapterOptions(metadata("openrouter", "openai-chat"), api(), {})
  expect(openrouter.headers["X-Title"]).toBe("Open Clank")
})

test("managed state rejects plugin family placeholders without a host connection marker", () => {
  expect(Provider.isManagedProjectedProviderEntry("xiaomi", { options: {} })).toBe(false)
  expect(
    Provider.isManagedProjectedProviderEntry("connection-123", {
      options: { _openclankConnectionID: "connection-123" },
    }),
  ).toBe(true)
})

test("ChatGPT managed fetch uses only the leased OAuth account and fixed Codex route", async () => {
  let seen: { input?: RequestInfo | URL; init?: RequestInit } = {}
  const upstream = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen = { input, init }
    return new Response("ok")
  }) as typeof fetch
  const credential = oauth({ accountId: "acct-leased" })
  const managedFetch = Provider.createManagedCodexFetch(credential, upstream)

  await managedFetch("https://api.openai.com/v1/responses", {
    method: "POST",
    headers: { Authorization: "Bearer projection-secret" },
  })

  expect(String(seen.input)).toBe("https://chatgpt.com/backend-api/codex/responses")
  const headers = new Headers(seen.init?.headers)
  expect(headers.get("authorization")).toBe("Bearer leased-access")
  expect(headers.get("ChatGPT-Account-Id")).toBe("acct-leased")
  expect(headers.get("authorization")).not.toContain("projection-secret")

  expect(() =>
    Provider.applyManagedAdapterOptions(metadata("openai", "openai-responses", "subscription"), credential, {
      baseURL: "https://proxy.example/v1",
    }),
  ).toThrow("cannot use a different upstream route")
})

test("managed ChatGPT refreshes once on 401 and replays with the durable credential", async () => {
  const seen: string[] = []
  let calls = 0
  let refreshes = 0
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    calls += 1
    seen.push(new Headers(init?.headers).get("authorization") ?? "")
    return new Response(calls === 1 ? "expired" : "ok", { status: calls === 1 ? 401 : 200 })
  }) as typeof fetch
  const managedFetch = Provider.createManagedCodexFetch(
    oauth({ access: "old-access", expires: Date.now() + 600_000 }),
    upstream,
    async (current) => {
      refreshes += 1
      return { ...current, access: "new-access", refresh: "new-refresh", expires: Date.now() + 3_600_000 }
    },
  )

  const response = await managedFetch("https://api.openai.com/v1/responses", { method: "POST", body: "{}" })
  expect(response.status).toBe(200)
  expect(refreshes).toBe(1)
  expect(seen).toEqual(["Bearer old-access", "Bearer new-access"])
})

test("managed xAI proactively refreshes an expiring credential once", async () => {
  const seen: string[] = []
  let refreshes = 0
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    seen.push(new Headers(init?.headers).get("authorization") ?? "")
    return new Response("ok")
  }) as typeof fetch
  const managedFetch = Provider.createManagedXaiFetch(
    oauth({ access: "expiring-access", expires: Date.now() + 1_000 }),
    upstream,
    async (current) => {
      refreshes += 1
      return { ...current, access: "fresh-access", refresh: "fresh-refresh", expires: Date.now() + 3_600_000 }
    },
  )

  expect((await managedFetch("https://api.x.ai/v1/responses")).status).toBe(200)
  expect(refreshes).toBe(1)
  expect(seen).toEqual(["Bearer fresh-access"])
})

test("Copilot managed fetch uses the leased device token and request classification", async () => {
  let seen: RequestInit | undefined
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    seen = init
    return new Response("ok")
  }) as typeof fetch
  const managedFetch = Provider.createManagedCopilotFetch(oauth(), upstream)
  await managedFetch("https://api.githubcopilot.com/chat/completions", {
    method: "POST",
    headers: { Authorization: "Bearer wrong", "x-api-key": "wrong" },
    body: JSON.stringify({
      messages: [
        { role: "user", content: [{ type: "image_url", image_url: { url: "data:image/png;base64,AA==" } }] },
        { role: "assistant", content: [{ type: "text", text: "continuing" }] },
      ],
    }),
  })
  const headers = new Headers(seen?.headers)
  expect(headers.get("authorization")).toBe("Bearer leased-refresh")
  expect(headers.has("x-api-key")).toBe(false)
  expect(headers.get("x-initiator")).toBe("agent")
  expect(headers.get("Copilot-Vision-Request")).toBe("true")
})

test("managed Vertex fails closed instead of consulting ambient ADC", () => {
  expect(() => Provider.applyManagedAdapterOptions(metadata("google", "google-vertex"), api(), {})).toThrow(
    "requires a service-account adapter",
  )
})
