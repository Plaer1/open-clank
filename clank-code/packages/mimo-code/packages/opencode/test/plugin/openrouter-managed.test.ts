import { createHash } from "node:crypto"
import { expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenRouter } from "@openrouter/ai-sdk-provider"
import { OpenRouterBrowserAuthPlugin } from "../../src/plugin/openrouter-browser"
import { ControlPlane } from "../../src/acp/provider-control"
import { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"

const connection = {
  familyID: "openrouter", adapterID: "openai-chat", kind: "official", billingLane: "metered_api",
  url: "https://openrouter.ai/api/v1", settings: {},
}

test("OpenRouter browser PKCE yields a managed API key and account-scoped discovery", async () => {
  const originalFetch = globalThis.fetch
  let exchange: { url: string; headers: Headers; body: Record<string, unknown> } | undefined
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = input instanceof Request ? input.url : String(input)
    const body = JSON.parse(String(init?.body ?? "{}"))
    exchange = { url, headers: new Headers(init?.headers), body }
    return new Response(JSON.stringify({ key: "sk-or-synthetic-managed-key" }), {
      headers: { "content-type": "application/json" },
    })
  }) as unknown as typeof fetch
  try {
    const hooks = await OpenRouterBrowserAuthPlugin({} as any)
    const method = hooks.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("OpenRouter browser method was not registered")
    expect(method.acquisitionMethod).toBe("browser")
    expect(method.credentialKinds).toEqual(["api"])
    expect(method.billingLanes).toEqual(["metered_api"])
    const authorization = await method.authorize!({
      __openclank_flow_id: "flow-openrouter-fixture",
      __openclank_redirect_uri: "http://127.0.0.1:49123/provider/oauth/callback",
      __openclank_state: "state-openrouter-synthetic-123456",
    })
    const authURL = new URL(authorization.url)
    expect(authURL.origin + authURL.pathname).toBe("https://openrouter.ai/auth")
    expect(authURL.searchParams.get("callback_url")).toBe("http://127.0.0.1:49123/provider/oauth/callback")
    expect(authURL.searchParams.get("code_challenge_method")).toBe("S256")
    expect(authURL.searchParams.get("state")).toBe("state-openrouter-synthetic-123456")
    expect(authURL.searchParams.get("key_label")).toBe("Open Clank")

    const result = await authorization.callback("synthetic-auth-code", new AbortController().signal)
    expect(result).toEqual({ type: "success", key: "sk-or-synthetic-managed-key" })
    expect(exchange?.url).toBe("https://openrouter.ai/api/v1/auth/keys")
    expect(exchange?.headers.has("authorization")).toBe(false)
    expect(exchange?.body).toMatchObject({ code: "synthetic-auth-code", code_challenge_method: "S256" })
    expect(typeof exchange?.body.code_verifier).toBe("string")
    const challenge = createHash("sha256").update(String(exchange?.body.code_verifier)).digest("base64url")
    expect(authURL.searchParams.get("code_challenge")).toBe(challenge)

    const control = new ControlPlane({
      methods: async () => ({ openrouter: [method] }), authorize: async () => undefined, exchange: async () => undefined,
      cancel: async () => undefined, modelCatalog: async () => ({}),
    } as any, Date.now, (async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(input instanceof Request ? input.url : String(input)).toBe("https://openrouter.ai/api/v1/models")
      expect(new Headers(init?.headers).get("authorization")).toBe("Bearer sk-or-synthetic-managed-key")
      return new Response(JSON.stringify({ data: [{ id: "openai/gpt-4o-mini" }] }), {
        headers: { "content-type": "application/json" },
      })
    }) as unknown as typeof fetch)
    const account = await control.accountValidate({
      connection, authMethod: "api_key", acquisitionMethod: "browser", credentialKind: "api",
      credential: { type: "api", key: String(result.type === "success" && "key" in result ? result.key : "") },
      accountID: "openrouter-synthetic-account", credentialRevision: 1,
    })
    expect(account).toMatchObject({ authMethod: "api_key", acquisitionMethod: "browser", credentialKind: "api" })
    expect(account.discovery.status).toBe("complete")
    expect(account.credential.type).toBe("api")

    const provider = {
      id: ProviderID.make("openrouter-connection"),
      options: {
        _openclankConnectionID: "openrouter-connection", _openclankFamilyID: "openrouter",
        _openclankAdapterID: "openai-chat", _openclankBillingLane: "metered_api",
      },
    }
    const model = {
      id: ModelID.make("openai/gpt-4o-mini"), providerID: ProviderID.make("openrouter-connection"),
      options: { _openclankAdapterID: "openai-chat" },
      api: { id: "openrouter", npm: "@openrouter/ai-sdk-provider", url: "https://openrouter.ai/api/v1" },
    }
    const requestMetadata = Provider.managedAdapterMetadata(provider as any, model as any)
    let request: { url: string; headers: Headers } | undefined
    const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
      request = { url: input instanceof Request ? input.url : String(input), headers: new Headers(init?.headers) }
      return new Response(JSON.stringify({ error: { message: "synthetic capture" } }), { status: 400 })
    }) as typeof fetch
    const options = Provider.applyManagedAdapterOptions(requestMetadata, account.credential, {
      baseURL: "https://openrouter.ai/api/v1", apiKey: account.credential.type === "api" ? account.credential.key : "",
      fetch: fetcher,
    })
    const sdk = createOpenRouter(options as any)
    const selected = Provider.selectManagedLanguageModel(sdk as any, requestMetadata, "openai/gpt-4o-mini")
    await generateText({ model: selected, prompt: "synthetic request", maxRetries: 0 }).catch(() => undefined)
    expect(request?.url).toBe("https://openrouter.ai/api/v1/chat/completions")
    expect(request?.headers.get("authorization")).toBe("Bearer sk-or-synthetic-managed-key")
    expect(request?.headers.get("http-referer")).toBe("https://openclank.app/")
    expect(request?.headers.get("x-title")).toBe("Open Clank")
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("OpenRouter browser key flow rejects untrusted callbacks and honors cancellation", async () => {
  const originalFetch = globalThis.fetch
  let requests = 0
  globalThis.fetch = (async () => {
    requests++
    return new Response(JSON.stringify({ key: "unexpected" }))
  }) as unknown as typeof fetch
  try {
    const hooks = await OpenRouterBrowserAuthPlugin({} as any)
    const method = hooks.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("OpenRouter browser method was not registered")
    await expect(method.authorize!({
      __openclank_redirect_uri: "https://attacker.invalid/callback", __openclank_state: "state-openrouter-synthetic-123456",
    })).rejects.toThrow("valid managed callback")
    const authorization = await method.authorize!({
      __openclank_redirect_uri: "http://localhost:49123/provider/oauth/callback", __openclank_state: "state-openrouter-synthetic-123456",
    })
    const controller = new AbortController()
    controller.abort()
    expect((await authorization.callback("synthetic-code", controller.signal)).type).toBe("failed")
    expect(requests).toBe(0)

    globalThis.fetch = (async (_input: RequestInfo | URL, init?: RequestInit) => {
      requests++
      const body = new ReadableStream<Uint8Array>({
        start(stream) {
          init?.signal?.addEventListener("abort", () => stream.error(new DOMException("Aborted", "AbortError")), { once: true })
        },
      })
      return new Response(body, { headers: { "content-type": "application/json" } })
    }) as unknown as typeof fetch
    const heldAuthorization = await method.authorize!({
      __openclank_redirect_uri: "http://localhost:49123/provider/oauth/callback", __openclank_state: "state-openrouter-synthetic-123456",
    })
    const heldController = new AbortController()
    const heldExchange = heldAuthorization.callback("single-use-code", heldController.signal)
    await new Promise((resolve) => setTimeout(resolve, 10))
    heldController.abort()
    expect((await heldExchange).type).toBe("failed")
    expect(requests).toBe(1)
  } finally {
    globalThis.fetch = originalFetch
  }
})
