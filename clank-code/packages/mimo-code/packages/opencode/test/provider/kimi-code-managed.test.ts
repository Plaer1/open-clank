import { expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { KimiCodeChinaAuthPlugin, KimiCodeGlobalAuthPlugin, refreshKimiCodeOAuthCredential } from "../../src/plugin/kimi-code"
import { Auth } from "../../src/auth"
import { ControlPlane, validateConnection } from "../../src/acp/provider-control"
import { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"

const DEVICE_ID = "f3e1245d-8260-4ee8-b2f6-a18e7be1c44a"
const metadata: NonNullable<Auth.Oauth["metadata"]> = {
  device_id: DEVICE_ID,
  kimi_region: "cn",
  credential_profile: "kimi-code-device-v1",
}

const family = (familyID = "kimi-code-plan-cn") => ({
  familyID,
  adapterID: "models-dev-openai-compatible",
  kind: "subscription",
  billingLane: "subscription",
  url: familyID.endsWith("-cn") ? "https://api.kimi.com/coding/v1" : "https://api.kimi.ai/coding/v1",
  settings: {},
})

test("Kimi device OAuth uses truthful identity, bounded grant states, and persists typed metadata", async () => {
  const originalFetch = globalThis.fetch
  const requests: Array<{ url: string; headers: Headers; body: URLSearchParams }> = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    const headers = new Headers(input instanceof Request ? input.headers : undefined)
    new Headers(init?.headers).forEach((value, key) => headers.set(key, value))
    const body = new URLSearchParams(String(init?.body ?? ""))
    requests.push({ url, headers, body })
    if (url.endsWith("/api/oauth/device_authorization")) {
      return new Response(JSON.stringify({
        device_code: "synthetic-device-code-123",
        user_code: "ABCD-EFGH",
        verification_uri_complete: "https://auth.kimi.com/verify?user_code=ABCD-EFGH",
        expires_in: 5,
        interval: 1,
      }), { headers: { "content-type": "application/json" } })
    }
    return new Response(JSON.stringify({ access_token: "synthetic-access", refresh_token: "synthetic-refresh", expires_in: 3600 }), {
      headers: { "content-type": "application/json" },
    })
  }) as typeof fetch
  try {
    const hook = await KimiCodeChinaAuthPlugin({} as any)
    const method = hook.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Kimi device method was not registered")
    expect(method.acquisitionMethod).toBe("device")
    const started = await method.authorize!({})
    if (started.method !== "auto") throw new Error("Kimi device flow did not start automatically")
    const result = await started.callback(undefined, new AbortController().signal)
    expect(result.type).toBe("success")
    if (result.type !== "success") throw new Error("synthetic device exchange failed")
    const returnedMetadata = result.credentialMetadata
    if (!returnedMetadata) throw new Error("Kimi device metadata was not returned")
    expect(returnedMetadata).toMatchObject({ kimi_region: "cn", credential_profile: "kimi-code-device-v1" })
    expect(returnedMetadata.device_id).toMatch(/^[0-9a-f-]{36}$/i)
    expect(requests.map((item) => item.url)).toEqual([
      "https://auth.kimi.com/api/oauth/device_authorization",
      "https://auth.kimi.com/api/oauth/token",
    ])
    expect(requests[0].body.get("client_id")).toBe("17e5f671-d194-4dfb-9706-5516cb48c098")
    expect(requests[1].body.get("grant_type")).toBe("urn:ietf:params:oauth:grant-type:device_code")
    expect(requests[0].headers.get("x-msh-platform")).toBe("openclank")
    expect(requests[0].headers.get("user-agent")).toMatch(/^openclank\//)
    expect(requests[0].headers.get("x-msh-device-id")).toBe(returnedMetadata.device_id)

    const refreshed = await refreshKimiCodeOAuthCredential({
      type: "oauth", access: "synthetic-access", refresh: "synthetic-refresh", expires: Date.now() - 1, metadata: returnedMetadata,
    })
    expect(refreshed.access).toBe("synthetic-access")
    expect(refreshed.metadata).toEqual(returnedMetadata)
    expect(requests.at(-1)?.url).toBe("https://auth.kimi.com/api/oauth/token")
    expect(requests.at(-1)?.body.get("grant_type")).toBe("refresh_token")
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("Kimi global device OAuth aborts cleanly and manual plan keys use the global subscription endpoint", async () => {
  const originalFetch = globalThis.fetch
  let calls = 0
  globalThis.fetch = (async () => {
    calls++
    return new Response(JSON.stringify({
      device_code: "synthetic-device-code-123", user_code: "ABCD-EFGH",
      verification_uri: "https://auth.kimi.ai/verify", expires_in: 60, interval: 60,
    }), { headers: { "content-type": "application/json" } })
  }) as unknown as typeof fetch
  try {
    const hook = await KimiCodeGlobalAuthPlugin({} as any)
    const method = hook.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Kimi device method was not registered")
    const started = await method.authorize!({})
    if (started.method !== "auto") throw new Error("Kimi device flow did not start automatically")
    const controller = new AbortController()
    const pending = started.callback(undefined, controller.signal)
    controller.abort()
    expect((await pending).type).toBe("failed")
    expect(calls).toBe(1)
  } finally {
    globalThis.fetch = originalFetch
  }

  let requestedURL = ""
  const control = new ControlPlane({
    methods: async () => ({}), authorize: async () => undefined, exchange: async () => undefined,
    cancel: async () => undefined, modelCatalog: async () => ({}),
  } as any, Date.now, (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestedURL = input instanceof Request ? input.url : String(input)
    expect(new Headers(init?.headers).get("authorization")).toBe("Bearer kimi-plan-key")
    return new Response(JSON.stringify({ data: [{ id: "kimi-plan-model", name: "Kimi plan model" }] }))
  }) as typeof fetch)
  const result = await control.accountValidate({
    connection: family("kimi-code-plan-global"), authMethod: "api_key", acquisitionMethod: "manual-key", credentialKind: "api",
    credential: { type: "api", key: "kimi-plan-key" }, accountID: "account-kimi-key", credentialRevision: 1,
  })
  expect(result.authMethod).toBe("api_key")
  expect(result.acquisitionMethod).toBe("manual-key")
  expect(result.endpointPolicy).toBe("kimi-code-plan-global-v1")
  expect(requestedURL).toBe("https://api.kimi.ai/coding/v1/models")
  expect(() => validateConnection({ ...family("kimi-code-plan-global"), url: "https://api.kimi.com/coding/v1" })).toThrow("declared billing lane")
})

test("Kimi discovery and Chat Completions stay on the selected regional route", async () => {
  let discovery: { url?: string; headers?: Headers } = {}
  const auth = {
    methods: async () => ({}),
    authorize: async () => undefined,
    exchange: async () => undefined,
    cancel: async () => undefined,
    modelCatalog: async () => ({}),
  }
  const control = new ControlPlane(auth as any, Date.now, (async (input: RequestInfo | URL, init?: RequestInit) => {
    discovery = { url: input instanceof Request ? input.url : String(input), headers: new Headers(init?.headers) }
    return new Response(JSON.stringify({ data: [{ id: "kimi-fixture", name: "Kimi fixture" }] }), {
      headers: { "content-type": "application/json" },
    })
  }) as typeof fetch)
  const credential: Auth.Oauth = {
    type: "oauth", access: "leased-access", refresh: "leased-refresh", expires: Date.now() + 600_000, metadata,
  }
  const account = await control.accountValidate({
    connection: family(), authMethod: "oauth", acquisitionMethod: "device", credentialKind: "oauth",
    credential, accountID: "account-kimi-fixture", credentialRevision: 1,
  })
  expect(discovery.url).toBe("https://api.kimi.com/coding/v1/models")
  expect(discovery.headers?.get("authorization")).toBe("Bearer leased-access")
  expect(discovery.headers?.get("x-msh-device-id")).toBe(DEVICE_ID)
  expect(account.discovery.status).toBe("complete")
  expect(account.credential.metadata).toEqual(metadata)
  expect(account.endpointPolicy).toBe("kimi-code-plan-cn-v1")
  expect(() => validateConnection({ ...family(), url: "https://api.kimi.ai/coding/v1" })).toThrow("declared billing lane")

  const model = {
    id: ModelID.make("kimi-fixture"), providerID: ProviderID.make("kimi-connection"),
    options: { _openclankAdapterID: "models-dev-openai-compatible" },
    api: { id: "models-dev-openai-compatible", npm: "@ai-sdk/openai-compatible", url: "https://api.kimi.com/coding/v1" },
  }
  const provider = {
    id: ProviderID.make("kimi-connection"),
    options: {
      _openclankConnectionID: "kimi-connection", _openclankFamilyID: "kimi-code-plan-cn",
      _openclankAdapterID: "models-dev-openai-compatible", _openclankBillingLane: "subscription",
    },
  }
  const requestMetadata = Provider.managedAdapterMetadata(
    provider as any, model as any,
  )
  const scope = {
    connectionID: "kimi-connection", billingLane: "subscription" as const, credentialRequired: true,
    accountID: "account-kimi-fixture", credentialRevision: 1, credential,
  }
  let captured: { url?: URL; headers?: Headers; body?: any; redirect?: RequestRedirect } = {}
  const originalFetch = globalThis.fetch
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    captured = {
      url: new URL(input instanceof Request ? input.url : String(input)),
      headers: new Headers(init?.headers),
      body: JSON.parse(String(init?.body ?? "{}")),
      redirect: init?.redirect,
    }
    return new Response(JSON.stringify({ error: { message: "synthetic capture stop" } }), { status: 400 })
  }) as typeof fetch
  try {
    const options = Provider.applyManagedAdapterOptions(
      requestMetadata, credential, { baseURL: "https://api.kimi.com/coding/v1" }, scope as any,
    )
    const sdk = createOpenAICompatible({ ...options, baseURL: "https://api.kimi.com/coding/v1", name: "kimi-fixture" })
    const selected = Provider.selectManagedLanguageModel(sdk, requestMetadata, "kimi-fixture")
    await generateText({ model: selected, prompt: "synthetic request", maxRetries: 0 }).catch(() => undefined)
    expect(captured.url?.href).toBe("https://api.kimi.com/coding/v1/chat/completions")
    expect(captured.headers?.get("authorization")).toBe("Bearer leased-access")
    expect(captured.headers?.get("x-msh-platform")).toBe("openclank")
    expect(captured.headers?.get("x-msh-device-id")).toBe(DEVICE_ID)
    expect(captured.headers?.get("user-agent")).toMatch(/^openclank\//)
    expect(captured.headers?.has("x-api-key")).toBe(false)
    expect(captured.body?.messages?.[0]?.content).toBe("synthetic request")
    expect(captured.redirect).toBe("error")
    await expect((options.fetch as typeof fetch)("https://proxy.invalid/coding/v1/chat/completions", { method: "POST" })).rejects.toThrow("escaped its declared endpoint")
    await expect((options.fetch as typeof fetch)("https://api.kimi.com/coding/v1/v1/chat/completions", { method: "POST" })).rejects.toThrow("escaped its declared endpoint")
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("Kimi OAuth retries one replayable 401 through managed refresh, but never retries API or streaming bodies", async () => {
  const provider = {
    id: ProviderID.make("kimi-connection"),
    options: {
      _openclankConnectionID: "kimi-connection", _openclankFamilyID: "kimi-code-plan-cn",
      _openclankAdapterID: "models-dev-openai-compatible", _openclankBillingLane: "subscription",
    },
  }
  const model = {
    id: ModelID.make("kimi-fixture"), providerID: ProviderID.make("kimi-connection"),
    options: { _openclankAdapterID: "models-dev-openai-compatible" },
    api: { id: "models-dev-openai-compatible", npm: "@ai-sdk/openai-compatible", url: "https://api.kimi.com/coding/v1" },
  }
  const route = Provider.managedAdapterMetadata(provider as any, model as any)
  const oauth: Auth.Oauth = { type: "oauth", access: "expired-access", refresh: "refresh", expires: Date.now() + 600_000, metadata }
  let refreshes = 0
  let calls = 0
  const fetcher = Provider.createManagedKimiCodeFetch(route, oauth, (async (_input: RequestInfo | URL, init?: RequestInit) => {
    calls++
    expect(new Headers(init?.headers).get("authorization")).toBe(calls === 1 ? "Bearer expired-access" : "Bearer fresh-access")
    return calls === 1 ? new Response("unauthorized", { status: 401 }) : new Response("ok")
  }) as typeof fetch, async (current) => {
    refreshes++
    return { ...current, access: "fresh-access", expires: Date.now() + 600_000 }
  })
  const response = await fetcher("https://api.kimi.com/coding/v1/chat/completions", { method: "POST", body: "{}" })
  expect(response.status).toBe(200)
  expect([calls, refreshes]).toEqual([2, 1])

  let expiredCalls = 0
  let expiredRefreshes = 0
  const expired = Provider.createManagedKimiCodeFetch(route, { ...oauth, expires: Date.now() - 1 }, (async () => {
    expiredCalls++
    return new Response("still unauthorized", { status: 401 })
  }) as unknown as typeof fetch, async (current) => {
    expiredRefreshes++
    return { ...current, access: "fresh-access", expires: Date.now() + 600_000 }
  })
  expect((await expired("https://api.kimi.com/coding/v1/chat/completions", { method: "POST", body: "{}" })).status).toBe(401)
  expect([expiredCalls, expiredRefreshes]).toEqual([1, 1])

  let streamCalls = 0
  const streaming = Provider.createManagedKimiCodeFetch(route, oauth, (async () => {
    streamCalls++
    return new Response("unauthorized", { status: 401 })
  }) as unknown as typeof fetch, async () => {
    refreshes++
    return oauth
  })
  const stream = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode("{}")); controller.close() } })
  const streamResponse = await streaming("https://api.kimi.com/coding/v1/chat/completions", {
    method: "POST", body: stream, duplex: "half",
  } as RequestInit)
  expect(streamResponse.status).toBe(401)
  expect(streamCalls).toBe(1)
  expect(refreshes).toBe(1)

  let apiCalls = 0
  const api = Provider.createManagedKimiCodeFetch(route, { type: "api", key: "plan-key" }, (async () => {
    apiCalls++
    return new Response("unauthorized", { status: 401 })
  }) as unknown as typeof fetch, async () => {
    refreshes++
    return oauth
  })
  expect((await api("https://api.kimi.com/coding/v1/chat/completions", { method: "POST", body: "{}" })).status).toBe(401)
  expect([apiCalls, refreshes]).toEqual([1, 1])
})

test("Kimi device polling honors pending and slow_down intervals and rejects nonpositive expiry", async () => {
  const originalFetch = globalThis.fetch
  const pollTimes: number[] = []
  let tokenPoll = 0
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input)
    if (url.endsWith("/api/oauth/device_authorization")) {
      return new Response(JSON.stringify({
        device_code: "synthetic-device-code-123", user_code: "ABCD-EFGH",
        verification_uri_complete: "https://auth.kimi.com/verify?user_code=ABCD-EFGH", expires_in: 30, interval: 1,
      }), { headers: { "content-type": "application/json" } })
    }
    pollTimes.push(Date.now())
    tokenPoll++
    const result = tokenPoll === 1 ? { error: "authorization_pending" } : tokenPoll === 2
      ? { error: "slow_down" }
      : { access_token: "synthetic-access", refresh_token: "synthetic-refresh", expires_in: 60 }
    return new Response(JSON.stringify(result), { headers: { "content-type": "application/json" } })
  }) as typeof fetch
  try {
    const plugin = await KimiCodeChinaAuthPlugin({} as any)
    const method = plugin.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Kimi device method was not registered")
    const authorization = await method.authorize!({})
    const result = await authorization.callback("", new AbortController().signal)
    expect(result.type).toBe("success")
    expect(pollTimes).toHaveLength(3)
    expect(pollTimes[2]! - pollTimes[1]!).toBeGreaterThanOrEqual(5_800)

    globalThis.fetch = (async () => new Response(JSON.stringify({
      device_code: "synthetic-device-code-123", user_code: "ABCD-EFGH",
      verification_uri_complete: "https://auth.kimi.com/verify?user_code=ABCD-EFGH", expires_in: 0, interval: 1,
    }), { headers: { "content-type": "application/json" } })) as unknown as typeof fetch
    await expect(method.authorize!({})).rejects.toThrow("invalid device-code expiry")
  } finally {
    globalThis.fetch = originalFetch
  }
}, 12_000)
