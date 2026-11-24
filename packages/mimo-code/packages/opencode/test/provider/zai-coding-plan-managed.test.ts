import { expect, test } from "bun:test"
import { ControlPlane, validateAccount, validateConnection } from "../../src/acp/provider-control"

const planConnection = {
  familyID: "zai-coding-plan",
  adapterID: "models-dev-openai-compatible",
  kind: "subscription",
  billingLane: "subscription",
  url: "https://api.z.ai/api/coding/paas/v4",
  settings: {},
}

test("Z.AI Coding Plan is a separate subscription family with a manual plan-key method", async () => {
  expect(validateConnection(planConnection).normalizedURL).toBe("https://api.z.ai/api/coding/paas/v4")
  expect(() => validateConnection({ ...planConnection, url: "https://api.z.ai/api/paas/v4" })).toThrow(
    "provider endpoint does not match its declared billing lane",
  )

  const account = validateAccount({
    connection: planConnection,
    authMethod: "api_key",
    acquisitionMethod: "manual-key",
    credentialKind: "api",
    credential: { type: "api", key: "synthetic-zai-coding-plan-key" },
    accountID: "synthetic-plan-account",
    credentialRevision: 1,
  })
  expect(account.authMethod).toBe("api_key")
  expect(account.endpointPolicy).toBe("zai-coding-plan-openai-v1")
  expect(account.credential.type).toBe("api")

  const catalog = await new ControlPlane({
    methods: async () => ({}),
    authorize: async () => undefined,
    exchange: async () => undefined,
    cancel: async () => undefined,
    modelCatalog: async () => ({
      "zai-coding-plan": {
        id: "zai-coding-plan", name: "Z.AI Coding Plan", npm: "@ai-sdk/openai-compatible",
        api: "https://api.z.ai/api/coding/paas/v4", env: [], models: { "glm-5.3": { id: "glm-5.3", name: "GLM-5.3" } },
      },
      zai: {
        id: "zai", name: "Z.AI", npm: "@ai-sdk/openai-compatible", api: "https://api.z.ai/api/paas/v4", env: [], models: {},
      },
    } as any),
  } as any).catalog()
  const codingPlan = catalog.families.find((family) => family.id === "zai-coding-plan")!
  const generic = catalog.families.find((family) => family.id === "zai")!
  expect(codingPlan.kinds).toEqual(["subscription"])
  expect(codingPlan.authMethods).toEqual([
    { id: "api_key", type: "api", label: "Z.AI Coding Plan API key", acquisitionMethod: "manual-key", credentialKinds: ["api"], billingLanes: ["subscription"] },
  ])
  expect(generic.billingLanes).toEqual(["metered_api"])
})

test("engine API-key subscription lanes are explicit and exclude generic OpenAI and Anthropic keys", async () => {
  const catalog = await new ControlPlane({
    methods: async () => ({}),
    authorize: async () => undefined,
    exchange: async () => undefined,
    cancel: async () => undefined,
    modelCatalog: async () => ({}),
  } as any).catalog()
  const subscriptionKeys = catalog.families
    .map((family) => ({ id: family.id, method: family.authMethods.find((method) => method.id === "api_key") }))
    .filter(({ method }) => method?.type !== "none" && method?.billingLanes.includes("subscription"))
    .map(({ id }) => id)
    .sort()
  expect(subscriptionKeys).toEqual([
    "kimi-code-plan-cn",
    "kimi-code-plan-global",
    "llmgateway-devpass",
    "opencode-go",
    "xiaomi",
    "zai-coding-plan",
  ])
  for (const id of ["openai", "anthropic"]) {
    const family = catalog.families.find((item) => item.id === id)!
    const apiKey = family.authMethods.find((method) => method.id === "api_key")!
    if (apiKey.type === "none") throw new Error(`${id} API-key method is missing`)
    expect(apiKey.billingLanes).toEqual(["metered_api", "custom"])
  }
})
