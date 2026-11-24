import { expect, test } from "bun:test"
import { generateText, streamText, tool } from "ai"
import { createAnthropic } from "@ai-sdk/anthropic"
import { createGoogleGenerativeAI } from "@ai-sdk/google"
import { createOpenAI } from "@ai-sdk/openai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { z } from "zod"
import type { LanguageModelV3 } from "@ai-sdk/provider"
import { Auth } from "../../src/auth"
import { Provider } from "../../src/provider"
import { createManagedOpenCodeFetch } from "../../src/provider/provider"
import { retainSupportedOpenCodeModels, validateAccount, validateConnection } from "../../src/acp/provider-control"
import { openCodeApiBaseURL, openCodeProtocol } from "../../src/provider/opencode-protocol"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { CLAUDE_PROFILE, ClaudeMinimumVersionError, claudeMinimumVersion } from "../../src/plugin/claude"
import { CLAUDE_SUBSCRIPTION_REQUEST_PROFILE } from "../../src/plugin/claude-subscription-profile"

const claudeRequestContext = {
  accountUUID: "provider-account-uuid",
  sessionID: "openclank-claude-session-v1:session-fixture",
  deviceID: "installation-fixture-id",
  platform: "Linux",
  architecture: "x64",
  runtime: "bun",
  runtimeVersion: "1.3.14",
}

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
    id: ModelID.make("fixture-model"),
    providerID: ProviderID.make("conn-1"),
    options: { _openclankAdapterID: "openai-responses" },
    api: { id: "openai-responses", npm: "@ai-sdk/openai", url: "https://api.openai.com/v1" },
  }
  expect(
    Provider.managedAdapterMetadata(
      provider as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
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
      model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
    ),
  ).toThrow("does not match its connection ID")
  expect(() =>
    Provider.managedAdapterMetadata(
      {
        ...provider,
        options: { ...provider.options, _openclankFamilyID: "anthropic" },
      } as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
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
    ["grok", "grok-cli-chat", "@ai-sdk/openai-compatible"],
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

test("Grok subscription uses the documented CLI chat proxy with leased OAuth, tools and image payloads", async () => {
  const credential = oauth({ access: "grok-leased-access", refresh: "grok-leased-refresh", expires: Date.now() + 3_600_000 })
  const scope = {
    connectionID: "grok-connection",
    billingLane: "subscription",
    credentialRequired: true,
    accountID: "grok-account",
    credentialRevision: 9,
    credential,
  }
  const provider = {
    id: ProviderID.make("grok-connection"),
    options: {
      _openclankConnectionID: "grok-connection",
      _openclankFamilyID: "grok",
      _openclankAdapterID: "grok-cli-chat",
      _openclankBillingLane: "subscription",
    },
  }
  const model = {
    id: ModelID.make("grok-build"),
    providerID: ProviderID.make("grok-connection"),
    options: { _openclankAdapterID: "grok-cli-chat" },
    api: { id: "grok-cli-chat", npm: "@ai-sdk/openai-compatible", url: "https://cli-chat-proxy.grok.com/v1" },
  }
  const metadata = Provider.managedAdapterMetadata(
    provider as Pick<Provider.Info, "id" | "options">,
    model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
  )
  let captured: { url: URL; headers: Headers; body: any; redirect?: RequestRedirect } | undefined
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input : input.url)
    captured = {
      url,
      headers: new Headers(init?.headers),
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
      redirect: init?.redirect,
    }
    return new Response(JSON.stringify({
      id: "fixture-completion",
      object: "chat.completion",
      created: 1,
      model: "grok-build",
      choices: [{ index: 0, message: { role: "assistant", content: "ok" }, finish_reason: "stop" }],
      usage: { prompt_tokens: 2, completion_tokens: 1, total_tokens: 3 },
    }), { headers: { "content-type": "application/json" } })
  }) as unknown as typeof fetch
  const originalFetch = globalThis.fetch
  globalThis.fetch = fetcher
  try {
    const options = Provider.applyManagedAdapterOptions(metadata, credential, {
      baseURL: "https://cli-chat-proxy.grok.com/v1",
    }, scope as any)
    const sdk = createOpenAICompatible({ ...options, baseURL: "https://cli-chat-proxy.grok.com/v1", name: "grok-fixture" })
    const selected = Provider.selectManagedLanguageModel(sdk, metadata, "grok-build")
    await generateText({
      model: selected,
      messages: [{
        role: "user",
        content: [
          { type: "text", text: "Describe this image and use a tool if useful." },
          { type: "image", image: new Uint8Array([1, 2, 3]), mediaType: "image/png" },
        ],
      }],
      tools: { lookup: tool({ description: "Fixture lookup", inputSchema: z.object({ query: z.string() }), execute: async () => "fixture" }) },
      maxRetries: 0,
    })
    expect(captured?.url.href).toBe("https://cli-chat-proxy.grok.com/v1/chat/completions")
    expect(captured?.headers.get("authorization")).toBe("Bearer grok-leased-access")
    expect(captured?.headers.get("x-xai-token-auth")).toBe("xai-grok-cli")
    expect(captured?.headers.get("x-grok-model-override")).toBe("grok-build")
    expect(captured?.headers.has("x-api-key")).toBe(false)
    expect(captured?.redirect).toBe("error")
    expect(captured?.body?.tools?.[0]?.function?.name).toBe("lookup")
    expect(captured?.body?.messages?.[0]?.content?.some((item: any) => item.type === "image_url")).toBe(true)

    await expect(options.fetch("https://api.x.ai/v1/chat/completions", {
      method: "POST",
      body: JSON.stringify({ model: "grok-build", messages: [] }),
    })).rejects.toThrow("escaped its declared Chat Completions endpoint")
  } finally {
    globalThis.fetch = originalFetch
  }
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

test("MiMo managed options keep the leased connection and scrub projected auth", () => {
  const result = Provider.applyManagedAdapterOptions(
    metadata("xiaomi", "mimo-native"),
    { ...api(), metadata: { base_url: "https://api.xiaomimimo.com/v1" } },
    {
      baseURL: "https://api.xiaomimimo.com/v1",
      headers: { Authorization: "Bearer projected-secret", "x-api-key": "projected-secret" },
    },
  )
  expect(result.baseURL).toBe("https://api.xiaomimimo.com/v1")
  expect(result.headers.Authorization).toBeUndefined()
  expect(result.headers["x-api-key"]).toBeUndefined()
  expect(result.headers["X-Mimo-Source"]).toBe("openclank")
})

test("MiMo subscription routing preserves explicit pins and ignores only inherited catalog defaults", () => {
  const planCredential: Auth.Api = {
    ...api(),
    metadata: { base_url: "https://token-plan-cn.xiaomimimo.com/v1" },
  }
  expect(() => Provider.applyManagedAdapterOptions(
    metadata("xiaomi", "mimo-native", "subscription"),
    planCredential,
    { baseURL: "https://api.xiaomimimo.com/v1" },
  )).toThrow("does not match its billing lane")

  const inherited = Provider.applyManagedAdapterOptions(
    metadata("xiaomi", "mimo-native", "subscription"),
    planCredential,
    { baseURL: "https://api.xiaomimimo.com/v1" },
    undefined,
    undefined,
    true,
  )
  expect(inherited.baseURL).toBe("https://token-plan-cn.xiaomimimo.com/v1")
  expect(() => Provider.applyManagedAdapterOptions(
    metadata("xiaomi", "mimo-native", "subscription"),
    { ...planCredential, metadata: { base_url: 42 as any } },
    {},
  )).toThrow("does not match its billing lane")
})

test("managed provider factory rejects invalid explicit base URLs before catalog fallback", () => {
  expect(Provider.resolveManagedBaseURL(undefined, "https://api.xiaomimimo.com/v1")).toEqual({
    baseURL: "https://api.xiaomimimo.com/v1",
    catalogFallback: true,
  })
  expect(Provider.resolveManagedBaseURL(null, "https://api.xiaomimimo.com/v1").catalogFallback).toBe(true)
  expect(Provider.resolveManagedBaseURL("https://token-plan-cn.xiaomimimo.com/v1", "https://api.xiaomimimo.com/v1"))
    .toMatchObject({ baseURL: "https://token-plan-cn.xiaomimimo.com/v1", catalogFallback: false })
  expect(() => Provider.resolveManagedBaseURL(123, "https://api.xiaomimimo.com/v1"))
    .toThrow("invalid base URL")
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
  }) as unknown as typeof fetch
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
  }) as unknown as typeof fetch
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
  }) as unknown as typeof fetch
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

test("Claude subscription managed fetch pins origin, bearer, OAuth headers, and request identity", async () => {
  const seen: { input?: RequestInfo | URL; init?: RequestInit } = {}
  const upstream = (async (input: RequestInfo | URL, init?: RequestInit) => {
    seen.input = input
    seen.init = init
    return new Response("ok")
  }) as unknown as typeof fetch
  const claudeCredential = oauth({ access: "claude-access", refresh: "claude-refresh" })
  const claudeScope = {
    connectionID: "conn-1",
    billingLane: "subscription" as const,
    credentialRequired: true,
    accountID: "acct-claude",
    credentialRevision: 1,
    credential: claudeCredential,
  }
  const options = Provider.applyManagedAdapterOptions(
    metadata("anthropic", "anthropic-messages", "subscription"),
    claudeCredential,
    { baseURL: CLAUDE_PROFILE.apiBaseURL, headers: { "anthropic-beta": "interleaved-thinking-2025-05-14" } },
    claudeScope,
  )
  expect(typeof options.fetch).toBe("function")
  const managedFetch = Provider.createManagedClaudeFetch(
    oauth({ access: "claude-access", refresh: "claude-refresh" }),
    (async (input: RequestInfo | URL, init?: RequestInit) => {
      seen.input = input
      seen.init = init
      return new Response("ok")
    }) as unknown as typeof fetch,
    undefined,
    () => claudeRequestContext,
  )
  await managedFetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    headers: { "x-api-key": "wrong", ...options.headers },
    body: JSON.stringify({ messages: [{ role: "user", content: "hello" }] }),
  })
  const headers = new Headers(seen.init?.headers)
  expect(String(seen.input)).toBe("https://api.anthropic.com/v1/messages")
  expect(headers.get("authorization")).toBe("Bearer claude-access")
  expect(headers.has("x-api-key")).toBe(false)
  expect(headers.get("anthropic-version")).toBe(CLAUDE_PROFILE.anthropicVersion)
  expect(headers.get("anthropic-beta")).toContain(CLAUDE_PROFILE.oauthBeta)
  expect(headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.codeBeta)
  expect(headers.get("anthropic-beta")).not.toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.thinkingBeta)
  expect(headers.get("user-agent")).toBe(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.userAgent)
  expect(headers.get("x-request-id")).toBeTruthy()
  await managedFetch("https://api.anthropic.com/v1/models")
  expect(new Headers(seen.init?.headers).get("anthropic-beta")).toContain(CLAUDE_PROFILE.oauthBeta)
  await expect(managedFetch("https://proxy.example/v1/messages")).rejects.toThrow("origin is not allowed")
  expect(() => Provider.applyManagedAdapterOptions(
    metadata("anthropic", "anthropic-messages", "subscription"),
    claudeCredential,
    { baseURL: CLAUDE_PROFILE.apiBaseURL },
  )).toThrow("matching leased OAuth scope")
})

test("Claude managed fetch refreshes once on 401 and never retries non-auth failures", async () => {
  let calls = 0
  let refreshes = 0
  const seen: string[] = []
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    calls += 1
    seen.push(new Headers(init?.headers).get("authorization") ?? "")
    return new Response(calls === 1 ? "expired" : "ok", { status: calls === 1 ? 401 : 200 })
  }) as unknown as typeof fetch
  const managedFetch = Provider.createManagedClaudeFetch(
    oauth({ access: "old", refresh: "old-refresh", expires: Date.now() + 600_000 }),
    upstream,
    async (current) => {
      refreshes += 1
      return { ...current, access: "new", refresh: "new-refresh", expires: Date.now() + 3_600_000 }
    },
  )
  expect((await managedFetch("https://api.anthropic.com/v1/messages")).status).toBe(200)
  expect(refreshes).toBe(1)
  expect(seen).toEqual(["Bearer old", "Bearer new"])
  expect((await managedFetch("https://api.anthropic.com/v1/messages", { headers: { "x-api-key": "bad" } })).status).toBe(200)
})

test("Claude subscription recognizes only bounded structured minimum-version errors without nested retry", async () => {
  let calls = 0
  let rejectedResponse: Response | undefined
  const managedFetch = Provider.createManagedClaudeFetch(
    oauth(),
    (async () => {
      calls += 1
      const payload = JSON.stringify({
        type: "error",
        error: {
          type: "invalid_request_error",
          code: "claude_code_version_too_old",
          message: "Claude Code version 2.1.300 or newer is required",
        },
      })
      rejectedResponse = new Response(new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(new TextEncoder().encode(payload))
          controller.close()
        },
      }), { status: 400, headers: { "content-type": "application/json" } })
      return rejectedResponse
    }) as unknown as typeof fetch,
    undefined,
    () => claudeRequestContext,
  )
  await expect(managedFetch("https://api.anthropic.com/v1/messages", {
    method: "POST",
    body: JSON.stringify({ messages: [{ role: "user", content: "hello" }] }),
  })).rejects.toBeInstanceOf(ClaudeMinimumVersionError)
  expect(calls).toBe(1)
  const reader = rejectedResponse?.body?.getReader()
  await expect(reader?.read()).resolves.toMatchObject({ done: true })
  reader?.releaseLock()

  const malformed = new Response(JSON.stringify({ error: { code: "some_other_error", message: "Claude Code version 9.9.9 or newer" } }), { status: 400 })
  await expect(claudeMinimumVersion(malformed)).resolves.toBeUndefined()
})

test("Claude bounded error inspection preserves an oversized original body", async () => {
  const payload = "x".repeat(5001)
  const response = new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(payload))
      setTimeout(() => controller.close(), 300)
    },
  }), { status: 400 })
  const started = Date.now()
  await expect(claudeMinimumVersion(response)).resolves.toBeUndefined()
  expect(Date.now() - started).toBeLessThan(200)
  await expect(response.text()).resolves.toBe(payload)
})

test("Claude subscription request context is taken from each trusted host session", async () => {
  const bodies: any[] = []
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    bodies.push(JSON.parse(String(init?.body)))
    return new Response("ok")
  }) as unknown as typeof fetch
  const contexts = [
    claudeRequestContext,
    { ...claudeRequestContext, sessionID: "host-session-two" },
  ]
  let index = 0
  const managedFetch = Provider.createManagedClaudeFetch(
    oauth(), upstream, undefined, () => contexts[index++],
  )
  for (let attempt = 0; attempt < 2; attempt++) {
    await managedFetch("https://api.anthropic.com/v1/messages", {
      method: "POST",
      body: JSON.stringify({ messages: [{ role: "user", content: "hello" }] }),
    })
  }
  const firstID = JSON.parse(bodies[0].metadata.user_id)
  const secondID = JSON.parse(bodies[1].metadata.user_id)
  expect(firstID.session_id).toBe(claudeRequestContext.sessionID)
  expect(secondID.session_id).toBe("host-session-two")
  expect(firstID.device_id).toBe(secondID.device_id)
  expect(firstID.account_uuid).toBe(secondID.account_uuid)
})

test("Anthropic API-key transport remains outside the subscription request profile", () => {
  const options = Provider.applyManagedAdapterOptions(
    metadata("anthropic", "anthropic-messages", "metered_api"),
    api(),
    { baseURL: CLAUDE_PROFILE.apiBaseURL, apiKey: "api-key-normal" },
  )
  expect(options.fetch).toBeUndefined()
  expect(options.apiKey).toBe("api-key-normal")
})

test("Claude managed transport reaches the real Anthropic SDK boundary for text, image, and tools", async () => {
  const requests: Array<{ body: any; headers: Headers }> = []
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ body: JSON.parse(String(init?.body)), headers: new Headers(init?.headers) })
    return new Response(JSON.stringify({
      id: "msg_fixture",
      type: "message",
      role: "assistant",
      content: [{ type: "text", text: "fixture answer" }],
      model: "claude-fixture",
      stop_reason: "end_turn",
      stop_sequence: null,
      usage: { input_tokens: 3, output_tokens: 2 },
    }), { status: 200, headers: { "content-type": "application/json" } })
  }) as unknown as typeof fetch
  const managedFetch = Provider.createManagedClaudeFetch(oauth(), upstream, undefined, () => claudeRequestContext)
  const model = createAnthropic({
    apiKey: "",
    baseURL: CLAUDE_PROFILE.apiBaseURL,
    fetch: managedFetch,
  }).languageModel("claude-fixture")
  await generateText({ model, prompt: "hello" })
  await generateText({
    model,
    prompt: [{ role: "user", content: [
      { type: "text", text: "inspect this" },
      { type: "image", image: Buffer.from("png-fixture"), mimeType: "image/png" },
    ] }] as any,
    tools: { lookup: tool({ description: "lookup", inputSchema: z.object({ query: z.string() }) }) },
  })
  expect(requests).toHaveLength(2)
  expect(requests[0].body.messages[0].content).toContainEqual({ type: "text", text: "hello" })
  expect(requests[1].body.messages[0].content.some((part: any) => part.type === "image")).toBe(true)
  expect(requests[1].body.tools[0].name).toBe("lookup")
  expect(requests[1].body.tools[0].input_schema.properties.query.type).toBe("string")
  expect(requests[0].headers.get("authorization")).toBe("Bearer leased-access")
  expect(requests[0].headers.has("x-api-key")).toBe(false)
  expect(requests[0].headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.codeBeta)
  expect(requests[1].headers.get("anthropic-beta")).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.toolsBeta)
  expect(requests[0].headers.get("user-agent")).toBe(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.userAgent)
  expect(requests[0].body.system[0].text).toContain(CLAUDE_SUBSCRIPTION_REQUEST_PROFILE.billingPrefix)
  expect(JSON.parse(requests[0].body.metadata.user_id)).toEqual({
    device_id: claudeRequestContext.deviceID,
    session_id: claudeRequestContext.sessionID,
    account_uuid: claudeRequestContext.accountUUID,
  })
})

test("Claude streamed provider errors do not replay after output begins", async () => {
  let calls = 0
  const upstream = (async () => {
    calls += 1
    const sse = [
      `event: message_start\ndata: ${JSON.stringify({ type: "message_start", message: { id: "m", type: "message", role: "assistant", content: [], model: "fixture", usage: { input_tokens: 1, output_tokens: 0 } } })}\n\n`,
      `event: content_block_start\ndata: ${JSON.stringify({ type: "content_block_start", index: 0, content_block: { type: "text", text: "" } })}\n\n`,
      `event: content_block_delta\ndata: ${JSON.stringify({ type: "content_block_delta", index: 0, delta: { type: "text_delta", text: "partial" } })}\n\n`,
      `event: error\ndata: ${JSON.stringify({ type: "error", error: { type: "api_error", message: "fixture failure" } })}\n\n`,
    ].join("")
    return new Response(sse, { status: 200, headers: { "content-type": "text/event-stream" } })
  }) as unknown as typeof fetch
  const model = createAnthropic({
    apiKey: "",
    baseURL: CLAUDE_PROFILE.apiBaseURL,
    fetch: Provider.createManagedClaudeFetch(oauth(), upstream, undefined, () => claudeRequestContext),
  }).languageModel("claude-fixture")
  const result = streamText({ model, prompt: "stream" })
  let streamed = ""
  try {
    for await (const chunk of result.textStream) streamed += chunk
  } catch {}
  expect(streamed).toContain("partial")
  expect(calls).toBe(1)
})

test("Claude refresh failure and 403/429 never dispatch a rotated credential", async () => {
  let failedCasCalls = 0
  const failedCasFetch = Provider.createManagedClaudeFetch(
    oauth({ expires: Date.now() + 1_000 }),
    (async () => { failedCasCalls += 1; return new Response("should not dispatch") }) as unknown as typeof fetch,
    async () => { throw new Error("OPENCLANK_REFRESH_PERSISTENCE_FAILED") },
  )
  await expect(failedCasFetch("https://api.anthropic.com/v1/messages")).rejects.toThrow("PERSISTENCE_FAILED")
  expect(failedCasCalls).toBe(0)

  for (const status of [403, 429]) {
    let refreshes = 0
    let calls = 0
    const managedFetch = Provider.createManagedClaudeFetch(
      oauth({ expires: Date.now() + 600_000 }),
      (async () => { calls += 1; return new Response("denied", { status }) }) as unknown as typeof fetch,
      async (current) => { refreshes += 1; return { ...current, access: "rotated" } },
    )
    expect((await managedFetch("https://api.anthropic.com/v1/messages")).status).toBe(status)
    expect(calls).toBe(1)
    expect(refreshes).toBe(0)
  }
})

test("Copilot managed fetch uses the leased device token and request classification", async () => {
  let seen: RequestInit | undefined
  const upstream = (async (_input: RequestInfo | URL, init?: RequestInit) => {
    seen = init
    return new Response("ok")
  }) as unknown as typeof fetch
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


test("OpenCode native models select only their declared SDK protocol and route", () => {
  const cases = [
    ["opencode-console", "gpt-6-luna", "openai-responses", "@ai-sdk/openai", "responses"],
    ["opencode-console", "claude-haiku-5-5", "anthropic-messages", "@ai-sdk/anthropic", "language"],
    ["opencode-console", "glm-5.3", "openai-chat", "@ai-sdk/openai-compatible", "chat"],
    ["opencode-console", "gemini-4-argon", "google-generative-ai", "@ai-sdk/google", "language"],
    ["opencode-go", "gpt-6-luna", "openai-responses", "@ai-sdk/openai", "responses"],
    ["opencode-go", "claude-haiku-5-5", "anthropic-messages", "@ai-sdk/anthropic", "language"],
    ["opencode-go", "glm-5.3", "openai-chat", "@ai-sdk/openai-compatible", "chat"],
    ["opencode-go", "gemini-4-argon", "google-generative-ai", "@ai-sdk/google", "language"],
  ] as const
  const calls: string[] = []
  const sdk = {
    languageModel(id: string) { calls.push(`language:${id}`); return {} as LanguageModelV3 },
    chat(id: string) { calls.push(`chat:${id}`); return {} as LanguageModelV3 },
    responses(id: string) { calls.push(`responses:${id}`); return {} as LanguageModelV3 },
  }
  for (const [familyID, modelID, protocol, npm, method] of cases) {
    const declared = openCodeProtocol(familyID, modelID)!
    expect(declared.protocol).toBe(protocol)
    expect(declared.sdkPackage).toBe(npm)
    expect(Provider.managedAdapterPackage({ familyID, adapterID: "opencode-native", apiPackage: npm })).toBe(npm)
    Provider.selectManagedLanguageModel(sdk, { adapterID: "opencode-native", apiProtocol: protocol }, modelID)
    const provider = {
      id: ProviderID.make("conn-opencode"),
      options: {
        _openclankConnectionID: "conn-opencode",
        _openclankFamilyID: familyID,
        _openclankAdapterID: "opencode-native",
        _openclankBillingLane: familyID === "opencode-go" ? "subscription" : "metered_api",
      },
    }
    const root = familyID === "opencode-go" ? "https://opencode.ai/inference/go" : "https://opencode.ai/inference"
    const model = {
      id: ModelID.make(modelID),
      providerID: ProviderID.make("conn-opencode"),
      options: { _openclankAdapterID: "opencode-native", _openclankApiProtocol: protocol },
      api: { id: protocol, npm, url: openCodeApiBaseURL(root, protocol) },
    }
    const metadata = Provider.managedAdapterMetadata(
      provider as Pick<Provider.Info, "id" | "options">,
      model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
    )
    expect(metadata.apiBaseURL).toBe(openCodeApiBaseURL(root, protocol))
    const options = Provider.applyManagedAdapterOptions(
      metadata,
      api(),
      { baseURL: metadata.apiBaseURL },
      undefined,
      undefined,
      false,
      { sessionID: "native-session", hostChatID: "host-chat-7" },
    )
    expect(options.headers["x-opencode-session"]).toBe("host-chat-7")
    expect(options.headers["User-Agent"]).toMatch(/^openclank\//)
    expect(method).toBe(protocol === "openai-responses" ? "responses" : protocol === "openai-chat" ? "chat" : "language")
  }
  expect(calls).toEqual(cases.map(([, model, , , method]) => `${method}:${model}`))
  expect(openCodeProtocol("opencode-go", "unknown-model")).toBeUndefined()
  expect(() => Provider.applyManagedAdapterOptions(
    { familyID: "opencode-go", adapterID: "opencode-native", billingLane: "subscription", connectionID: "conn-opencode", apiProtocol: "openai-responses", apiPackage: "@ai-sdk/openai", apiBaseURL: "https://opencode.ai/inference/go/openai/v1" },
    api(),
    { baseURL: "https://opencode.ai/inference/go/openai/v1" },
    undefined,
    undefined,
    false,
    { sessionID: "native-session" },
  )).toThrow("trusted host chat identity")
})


test("OpenCode account lane and endpoint policy remain paired", () => {
  const consoleConnection = {
    familyID: "opencode-console",
    adapterID: "opencode-native",
    kind: "official",
    billingLane: "metered_api",
    url: "https://opencode.ai/inference",
  }
  const goConnection = {
    familyID: "opencode-go",
    adapterID: "opencode-native",
    kind: "subscription",
    billingLane: "subscription",
    url: "https://opencode.ai/inference/go",
  }
  expect(validateConnection(consoleConnection).normalizedURL).toBe(consoleConnection.url)
  expect(validateConnection(goConnection).normalizedURL).toBe(goConnection.url)
  expect(() => validateConnection({ ...goConnection, billingLane: "metered_api" })).toThrow(
    "provider billing lane is not supported",
  )
  expect(() => validateConnection({ ...goConnection, url: "https://opencode.ai/inference" })).toThrow(
    "provider endpoint does not match its declared billing lane",
  )
  const validated = validateAccount({
    connection: goConnection,
    authMethod: "api_key",
    acquisitionMethod: "manual-key",
    credentialKind: "api",
    credential: { type: "api", key: "synthetic-key" },
    accountID: "fixture-account",
    credentialRevision: 1,
  })
  expect(validated.endpointPolicy).toBe("opencode-go-inference-v1")
  expect(validated.credential.type).toBe("api")
})


test("OpenCode bundled SDK requests use bearer auth, stable host session, and protocol paths", async () => {
  const captured: Array<{ url: URL; headers: Headers; body: any; redirect?: RequestRedirect }> = []
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input))
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value))
    captured.push({ url, headers, body: JSON.parse(String(init?.body ?? "{}")), redirect: init?.redirect })
    return new Response(JSON.stringify({ error: { message: "synthetic stop after request capture" } }), {
      status: 400,
      headers: { "content-type": "application/json" },
    })
  }) as typeof fetch
  try {
    const cases = [
      { family: "opencode-console", modelID: "gpt-6-luna", protocol: "openai-responses", npm: "@ai-sdk/openai", path: "/inference/openai/v1/responses", payload: "input" },
      { family: "opencode-console", modelID: "claude-haiku-5-5", protocol: "anthropic-messages", npm: "@ai-sdk/anthropic", path: "/inference/anthropic/v1/messages", payload: "messages" },
      { family: "opencode-console", modelID: "glm-5.3", protocol: "openai-chat", npm: "@ai-sdk/openai-compatible", path: "/inference/openai/v1/chat/completions", payload: "messages" },
      { family: "opencode-console", modelID: "gemini-4-argon", protocol: "google-generative-ai", npm: "@ai-sdk/google", path: "/inference/google/v1beta/models/gemini-4-argon:generateContent", payload: "contents" },
    ] as const
    for (const item of cases) {
      const descriptor = openCodeProtocol(item.family, item.modelID)!
      const root = "https://opencode.ai/inference"
      const provider = {
        id: ProviderID.make("conn-wire"),
        options: {
          _openclankConnectionID: "conn-wire",
          _openclankFamilyID: item.family,
          _openclankAdapterID: "opencode-native",
          _openclankBillingLane: "metered_api",
        },
      }
      const model = {
        id: ModelID.make(item.modelID),
        providerID: ProviderID.make("conn-wire"),
        options: { _openclankAdapterID: "opencode-native", _openclankApiProtocol: item.protocol },
        api: { id: item.protocol, npm: item.npm, url: openCodeApiBaseURL(root, item.protocol) },
      }
      const metadata = Provider.managedAdapterMetadata(
        provider as Pick<Provider.Info, "id" | "options">,
        model as Pick<Provider.Model, "id" | "providerID" | "options" | "api">,
      )
      const accountCredential = api()
      const managedOptions = Provider.applyManagedAdapterOptions(
        metadata,
        accountCredential,
        { apiKey: accountCredential.key, baseURL: metadata.apiBaseURL },
        undefined,
        undefined,
        false,
        { sessionID: "native-session-remapped", hostChatID: "stable-host-chat-42" },
      )
      let sdk: any
      if (item.npm === "@ai-sdk/openai") sdk = createOpenAI({ ...managedOptions, name: "fixture" })
      else if (item.npm === "@ai-sdk/anthropic") sdk = createAnthropic(managedOptions)
      else if (item.npm === "@ai-sdk/openai-compatible") sdk = createOpenAICompatible({ ...managedOptions, baseURL: metadata.apiBaseURL!, name: "fixture" })
      else sdk = createGoogleGenerativeAI(managedOptions)
      const selected = Provider.selectManagedLanguageModel(sdk, metadata, item.modelID)
      await generateText({ model: selected, prompt: "wire fixture", maxRetries: 0 }).catch(() => undefined)
      const request = captured.at(-1)!
      expect(request.url.origin).toBe("https://opencode.ai")
      expect(request.url.pathname).toBe(item.path)
      expect(request.headers.get("authorization")).toBe("Bearer leased-api-key")
      expect(request.headers.has("x-api-key")).toBe(false)
      expect(request.headers.has("api-key")).toBe(false)
      expect(request.headers.has("x-goog-api-key")).toBe(false)
      expect(request.headers.get("x-opencode-session")).toBe("stable-host-chat-42")
      expect(request.headers.get("user-agent")).toMatch(/^openclank\//)
      expect(request.redirect).toBe("error")
      expect(request.url.href).not.toContain("leased-api-key")
      expect(request.body).toHaveProperty(item.payload)
      if (item.protocol === "anthropic-messages") expect(request.body.max_tokens).toBeGreaterThan(0)
    }
    expect(captured).toHaveLength(4)

    const googleProtocol = "google-generative-ai" as const
    const googleRoot = "https://opencode.ai/inference"
    const googleMetadata: Provider.ManagedAdapterMetadata = {
      familyID: "opencode-console",
      adapterID: "opencode-native",
      billingLane: "metered_api",
      connectionID: "conn-wire",
      apiProtocol: googleProtocol,
      apiPackage: "@ai-sdk/google",
      apiBaseURL: openCodeApiBaseURL(googleRoot, googleProtocol),
    }
    const passthrough: string[] = []
    const scopedFetch = createManagedOpenCodeFetch(googleMetadata, api(), "stable-host-chat-42", (async (input, init) => {
      passthrough.push(`${new URL(String(input)).searchParams.toString()}|${init?.redirect}`)
      return new Response("ok")
    }) as typeof fetch)
    await expect(scopedFetch("https://attacker.invalid/inference/google/v1beta/models/x:generateContent")).rejects.toThrow("escaped its declared API route")
    await scopedFetch("https://opencode.ai/inference/google/v1beta/models/gemini-4-argon:streamGenerateContent?alt=sse&key=synthetic-key")
    expect(passthrough).toEqual(["alt=sse|error"])
  } finally {
    globalThis.fetch = originalFetch
  }
})


test("OpenCode discovery excludes unknown protocol rows before host projection", () => {
  const rows = [
    { modelID: "gpt-6-luna", displayName: "known" },
    { modelID: "future-model-unknown", displayName: "unknown" },
  ]
  expect(retainSupportedOpenCodeModels("opencode-console", rows).map((row) => row.modelID)).toEqual(["gpt-6-luna"])
  expect(retainSupportedOpenCodeModels("opencode-go", rows).map((row) => row.modelID)).toEqual(["gpt-6-luna"])
  expect(retainSupportedOpenCodeModels("openai", rows)).toEqual(rows)
})
