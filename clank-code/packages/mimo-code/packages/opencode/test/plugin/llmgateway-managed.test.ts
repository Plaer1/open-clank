import { expect, test } from "bun:test"
import { LLMGatewayBrowserAuthPlugin, LLMGatewayDevPassAuthPlugin } from "../../src/plugin/llmgateway-browser"
import { ControlPlane } from "../../src/acp/provider-control"

test("LLM Gateway credits and DevPass auth methods stay on their declared billing lanes", async () => {
  const hooks = await LLMGatewayBrowserAuthPlugin({} as any)
  const devpassHooks = await LLMGatewayDevPassAuthPlugin({} as any)
  const methods = hooks.auth!.methods
  const devpassMethods = devpassHooks.auth!.methods
  expect(methods).toHaveLength(1)
  expect(methods.map((method) => method.billingLanes)).toEqual([["metered_api"]])
  expect(devpassMethods.map((method) => method.billingLanes)).toEqual([["subscription"]])
  expect([...methods, ...devpassMethods].map((method) => method.credentialKinds)).toEqual([["api"], ["api"]])

  const control = new ControlPlane({
    methods: async () => ({ llmgateway: methods, "llmgateway-devpass": devpassMethods }),
    authorize: async () => undefined,
    exchange: async () => undefined,
    cancel: async () => undefined,
    modelCatalog: async () => ({
      llmgateway: {
        id: "llmgateway", name: "LLM Gateway", npm: "@ai-sdk/openai-compatible",
        api: "https://api.llmgateway.io/v1", env: [], models: { "fixture-model": { id: "fixture-model", name: "Fixture" } },
      },
    } as any),
  } as any)
  const catalog = await control.catalog()
  const creditFamily = catalog.families.find((family) => family.id === "llmgateway")!
  const devpassFamily = catalog.families.find((family) => family.id === "llmgateway-devpass")!
  expect(creditFamily.authMethods.filter((method) => method.type !== "none").map((method) => method.billingLanes)).toEqual([["metered_api"], ["metered_api"]])
  expect(devpassFamily.authMethods.filter((method) => method.type !== "none").map((method) => method.billingLanes)).toEqual([["subscription"], ["subscription"]])
})

test("LLM Gateway browser exchange preserves server expiry metadata without replaying codes", async () => {
  const originalFetch = globalThis.fetch
  const requests: Array<{ url: string; init?: RequestInit }> = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requests.push({ url: input instanceof Request ? input.url : String(input), init })
    return new Response(JSON.stringify({ api_key: "synthetic-gateway-key", expires_at: "2027-01-01T00:00:00Z" }), {
      headers: { "content-type": "application/json" },
    })
  }) as unknown as typeof fetch
  try {
    const hooks = await LLMGatewayBrowserAuthPlugin({} as any)
    const method = hooks.auth!.methods[0]
    if (method.type !== "oauth") throw new Error("Gateway browser method was not registered")
    const authorization = await method.authorize!({
      __openclank_redirect_uri: "http://127.0.0.1:49123/provider/oauth/callback",
      __openclank_state: "gateway-state-synthetic-123456",
    })
    const authURL = new URL(authorization.url)
    expect(authURL.origin + authURL.pathname).toBe("https://llmgateway.io/connect/cli")
    expect(authURL.searchParams.get("org")).toBe("default")
    expect(authURL.searchParams.get("source")).toBe("mimocode")
    expect(authURL.searchParams.get("code_challenge_method")).toBe("S256")
    expect(authorization.instructions).toContain("creates a new Gateway key")

    const result = await authorization.callback("synthetic-single-use-code", new AbortController().signal)
    expect(result).toEqual({ type: "success", key: "synthetic-gateway-key", metadata: { expires_at: "2027-01-01T00:00:00Z" } })
    expect(requests).toHaveLength(1)
    expect(requests[0].url).toBe("https://internal.llmgateway.io/public/cli-auth/token")
    expect(requests[0].init?.redirect).toBe("error")
    expect(JSON.parse(String(requests[0].init?.body))).toMatchObject({ code: "synthetic-single-use-code" })
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("LLM Gateway DevPass copy discloses the shared-key rotation", async () => {
  const hooks = await LLMGatewayDevPassAuthPlugin({} as any)
  const method = hooks.auth!.methods[0]
  if (method.type !== "oauth") throw new Error("DevPass browser method was not registered")
  const authorization = await method.authorize!({
    __openclank_redirect_uri: "http://127.0.0.1:49123/provider/oauth/callback",
    __openclank_state: "devpass-state-synthetic-123456",
  })
  expect(new URL(authorization.url).searchParams.get("org")).toBe("devpass")
  expect(authorization.instructions).toContain("may rotate its single existing API key")
})
