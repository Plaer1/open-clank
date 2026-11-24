import { expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { KiloDeviceAuthPlugin } from "../../src/plugin/kilo"
import { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"

const modelCatalog = {
  kilo: {
    id: "kilo", name: "Kilo Gateway", npm: "@ai-sdk/openai-compatible",
    api: "https://ai-gateway.kilo.ai/api/v1", env: ["KILO_API_KEY"], models: {},
  },
}
const connection = {
  familyID: "kilo", adapterID: "models-dev-openai-compatible", kind: "official", billingLane: "metered_api",
  url: "https://ai-gateway.kilo.ai/api/v1", settings: {},
}

test("Kilo device method stores an API credential and reuses the selected Gateway route", async () => {
  const originalFetch = globalThis.fetch
  const requests: Array<{ url: string; method: string; headers: Headers }> = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    const method = init?.method ?? (input instanceof Request ? input.method : "GET")
    requests.push({ url, method, headers: new Headers(init?.headers) })
    if (method === "POST") {
      return new Response(JSON.stringify({
        code: "ABCD-1234", verificationUrl: "https://kilo.ai/auth/device?code=ABCD-1234", expiresIn: 300,
      }), { headers: { "content-type": "application/json" } })
    }
    return new Response(JSON.stringify({ status: "approved", token: "synthetic-kilo-gateway-token", userEmail: "fixture@example.invalid" }), {
      headers: { "content-type": "application/json" },
    })
  }) as unknown as typeof fetch
  try {
    const plugin = await KiloDeviceAuthPlugin({} as any)
    const method = plugin.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Kilo device method was not registered")
    expect(method.acquisitionMethod).toBe("device")
    expect(method.credentialKinds).toEqual(["api"])
    expect(method.billingLanes).toEqual(["metered_api"])
    const authorization = await method.authorize!({})
    expect(authorization.url).toBe("https://kilo.ai/auth/device?code=ABCD-1234")
    expect(authorization.userCode).toBe("ABCD-1234")
    const result = await authorization.callback("", new AbortController().signal)
    expect(result).toEqual({ type: "success", key: "synthetic-kilo-gateway-token" })
    expect(requests.map((item) => [item.method, item.url])).toEqual([
      ["POST", "https://api.kilo.ai/api/device-auth/codes"],
      ["GET", "https://api.kilo.ai/api/device-auth/codes/ABCD-1234"],
    ])
    expect(requests.every((item) => !item.headers.has("authorization"))).toBe(true)

    const control = new (await import("../../src/acp/provider-control")).ControlPlane({
      methods: async () => ({ kilo: [method] }), authorize: async () => undefined, exchange: async () => undefined,
      cancel: async () => undefined, modelCatalog: async () => modelCatalog,
    } as any)
    const listed = (await control.catalog()).families.find((family) => family.id === "kilo")
    expect(listed).toMatchObject({
      displayName: "Kilo Gateway",
      adapters: ["models-dev-openai-compatible"],
      kinds: ["official"],
      billingLanes: ["metered_api"],
      authMethods: [
        { id: "api_key", type: "api", label: "API key" },
        { id: "oauth:0", type: "oauth", label: "Kilo Gateway device sign-in", credentialKinds: ["api"], billingLanes: ["metered_api"] },
      ],
    })
    const newConnection = await control.connectionValidate({
      familyID: "kilo", adapterID: "models-dev-openai-compatible", kind: "official", billingLane: "metered_api",
    })
    expect(newConnection.normalizedURL).toBe("https://ai-gateway.kilo.ai/api/v1")
    const legacyConnection = await control.connectionValidate({
      familyID: "kilo", adapterID: "models-dev-openai-compatible", kind: "official", billingLane: "metered_api",
      url: "https://api.kilo.ai/api/gateway",
    })
    expect(legacyConnection.normalizedURL).toBe("https://api.kilo.ai/api/gateway")
    const account = await control.accountValidate({
      connection, authMethod: "api_key", acquisitionMethod: "device", credentialKind: "api",
      credential: { type: "api", key: String(result.type === "success" && "key" in result ? result.key : "") },
      accountID: "kilo-synthetic-account", credentialRevision: 1,
    })
    expect(account).toMatchObject({ authMethod: "api_key", acquisitionMethod: "device", credentialKind: "api" })
    expect(account.credential.type).toBe("api")

    const provider = {
      id: ProviderID.make("kilo-connection"),
      options: {
        _openclankConnectionID: "kilo-connection", _openclankFamilyID: "kilo",
        _openclankAdapterID: "models-dev-openai-compatible", _openclankBillingLane: "metered_api",
      },
    }
    const model = {
      id: ModelID.make("fixture/model"), providerID: ProviderID.make("kilo-connection"),
      options: { _openclankAdapterID: "models-dev-openai-compatible" },
      api: { id: "kilo", npm: "@ai-sdk/openai-compatible", url: connection.url },
    }
    const metadata = Provider.managedAdapterMetadata(provider as any, model as any)
    let captured: { url: string; headers: Headers; body: any } | undefined
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      captured = {
        url: input instanceof Request ? input.url : String(input),
        headers: new Headers(init?.headers), body: JSON.parse(String(init?.body ?? "{}")),
      }
      return new Response(JSON.stringify({ error: { message: "synthetic capture" } }), { status: 400 })
    }) as typeof fetch
    const options = Provider.applyManagedAdapterOptions(metadata, account.credential, {
      baseURL: connection.url, apiKey: account.credential.type === "api" ? account.credential.key : "", fetch: fetcher,
    })
    const sdk = createOpenAICompatible({ ...options, baseURL: connection.url, name: "kilo-fixture" })
    const selected = Provider.selectManagedLanguageModel(sdk, metadata, "fixture/model")
    await generateText({ model: selected, prompt: "synthetic request", maxRetries: 0 }).catch(() => undefined)
    expect(captured?.url).toBe("https://ai-gateway.kilo.ai/api/v1/chat/completions")
    expect(captured?.headers.get("authorization")).toBe("Bearer synthetic-kilo-gateway-token")
    expect(captured?.body?.messages?.[0]?.content).toBe("synthetic request")
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("Kilo device poll cancellation stops pending sleeps and terminal codes fail closed", async () => {
  const originalFetch = globalThis.fetch
  let polls = 0
  globalThis.fetch = (async (input: RequestInfo | URL) => {
    const url = input instanceof Request ? input.url : String(input)
    if (url.endsWith("/codes")) return new Response(JSON.stringify({
      code: "ABCD-1234", verificationUrl: "https://kilo.ai/auth/device?code=ABCD-1234", expiresIn: 60,
    }), { headers: { "content-type": "application/json" } })
    polls++
    return new Response(null, { status: 202 })
  }) as unknown as typeof fetch
  try {
    const plugin = await KiloDeviceAuthPlugin({} as any)
    const method = plugin.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Kilo device method was not registered")
    const authorization = await method.authorize!({})
    const controller = new AbortController()
    const pending = authorization.callback("", controller.signal)
    await new Promise((resolve) => setTimeout(resolve, 10))
    controller.abort()
    expect((await pending).type).toBe("failed")
    expect(polls).toBe(1)

    globalThis.fetch = (async (input: RequestInfo | URL) => {
      const url = input instanceof Request ? input.url : String(input)
      if (url.endsWith("/codes")) return new Response(JSON.stringify({
        code: "ABCD-1234", verificationUrl: "https://kilo.ai/auth/device?code=ABCD-1234", expiresIn: 60,
      }), { headers: { "content-type": "application/json" } })
      return new Response(null, { status: 410 })
    }) as unknown as typeof fetch
    const expired = await method.authorize!({})
    expect((await expired.callback("", new AbortController().signal)).type).toBe("failed")
  } finally {
    globalThis.fetch = originalFetch
  }
})
