import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import path from "node:path"
import { OpenClankManagedProtocol } from "../../src/acp/openclank-protocol"
import { ControlPlane, type AuthServices, validateConnection } from "../../src/acp/provider-control"

const contractPath = path.resolve(
  import.meta.dir,
  "../../../../../../contracts/openclank/managed-provider-v2.schema.json",
)

test("managed capability declaration is pinned to the checked-in contract", async () => {
  const source = await Bun.file(contractPath).text()
  const schema = JSON.parse(source)
  const digest = createHash("sha256").update(source).digest("hex")

  expect(digest).toBe(OpenClankManagedProtocol.SCHEMA_HASH)
  expect(OpenClankManagedProtocol.capabilities.schemaID).toBe(schema.$id)
  expect(OpenClankManagedProtocol.capabilities.methods).toEqual(schema.$defs.managedMethod.enum)
  expect(OpenClankManagedProtocol.capabilities.operations).toEqual(schema.$defs.operation.enum)
})

test("ACP initialize metadata declares all managed protocol versions and capabilities", () => {
  const declaration = OpenClankManagedProtocol.initializeMeta.openclankManaged
  expect(declaration.protocolVersion).toBe(1)
  expect(declaration.providerStoreVersion).toBe(1)
  expect(declaration.operationRouterVersion).toBe(1)
  expect(declaration.schemaVersion).toBe(2)
  expect(declaration.artifactTransfer).toBe(true)
  expect(declaration.localExecutor).toBe(true)
  expect(declaration.methods).toContain("_openclank/provider-store/v1/account/bind")
  expect(declaration.methods).toContain("_openclank/provider-store/v1/refresh/commit")
  expect(declaration.methods).toContain("_openclank/operations/v1/executor/invoke")
})

test("managed peer negotiation requires the exact callback and operation sets", () => {
  const exact = structuredClone(OpenClankManagedProtocol.capabilities)
  expect(OpenClankManagedProtocol.validatePeerCapabilities(exact).schemaHash).toBe(OpenClankManagedProtocol.SCHEMA_HASH)
  expect(() =>
    OpenClankManagedProtocol.validatePeerCapabilities({
      ...exact,
      methods: [...exact.methods, exact.methods[0]],
    }),
  ).toThrow("incompatible")
  expect(() =>
    OpenClankManagedProtocol.validatePeerCapabilities({
      ...exact,
      operations: exact.operations.slice(1),
    }),
  ).toThrow("incompatible")
  expect(() =>
    OpenClankManagedProtocol.validatePeerCapabilities({
      ...exact,
      unexpected: true,
    }),
  ).toThrow("invalid shape")
})

const emptyAuth: AuthServices = {
  methods: async () => ({}),
  authorize: async () => undefined,
  exchange: async () => undefined,
  cancel: async () => {},
  modelCatalog: async () => ({}),
}

function catalogModel(id: string, npm?: string) {
  return {
    id,
    name: id,
    release_date: "2026-01-01",
    attachment: false,
    reasoning: false,
    temperature: true,
    tool_call: true,
    limit: { context: 32_000, output: 4_096 },
    ...(npm ? { provider: { npm } } : {}),
  }
}

test("managed catalog adds only uniform safe models.dev API families", async () => {
  const modelCatalog = {
    compatible: {
      id: "compatible",
      name: "Zeta Compatible",
      env: ["ZETA_API_KEY"],
      npm: "@ai-sdk/openai-compatible",
      api: "https://api.zeta.example/v1/",
      models: { zeta: catalogModel("zeta") },
    },
    anthropic_proxy: {
      id: "anthropic_proxy",
      name: "Alpha Anthropic",
      env: ["ALPHA_API_KEY"],
      npm: "@ai-sdk/anthropic",
      api: "https://api.alpha.example/anthropic/v1",
      models: { alpha: catalogModel("alpha") },
    },
    mixed: {
      id: "mixed",
      name: "Mixed Packages",
      env: [],
      npm: "@ai-sdk/openai-compatible",
      api: "https://mixed.example/v1",
      models: { mixed: catalogModel("mixed", "@ai-sdk/anthropic") },
    },
    templated: {
      id: "templated",
      name: "Templated Route",
      env: [],
      npm: "@ai-sdk/openai-compatible",
      api: "https://api.example/accounts/${ACCOUNT_ID}/v1",
      models: { templated: catalogModel("templated") },
    },
    local: {
      id: "local-catalog-entry",
      name: "Local Catalog Entry",
      env: [],
      npm: "@ai-sdk/openai-compatible",
      api: "http://127.0.0.1:1234/v1",
      models: { local: catalogModel("local") },
    },
    unsupported: {
      id: "unsupported",
      name: "Unsupported Package",
      env: [],
      npm: "@ai-sdk/groq",
      api: "https://api.unsupported.example/v1",
      models: { unsupported: catalogModel("unsupported") },
    },
  }
  const control = new ControlPlane({
    ...emptyAuth,
    modelCatalog: async () => modelCatalog,
  })

  const result = await control.catalog()
  expect(result.families.slice(0, 3).map((family) => family.id)).toEqual(["openai", "anthropic", "github-copilot"])
  expect(
    result.families
      .filter((family) => family.id.endsWith("proxy") || family.id === "compatible")
      .map((family) => family.id),
  ).toEqual(["anthropic_proxy", "compatible"])
  expect(result.families.find((family) => family.id === "compatible")).toMatchObject({
    adapters: ["models-dev-openai-compatible"],
    kinds: ["official"],
    billingLanes: ["metered_api"],
    authMethods: [{ id: "api_key", type: "api", label: "API key" }],
    modelCount: 1,
  })
  for (const familyID of ["mixed", "templated", "local-catalog-entry", "unsupported"]) {
    expect(result.families.some((family) => family.id === familyID)).toBe(false)
  }

  const validated = await control.connectionValidate({
    familyID: "compatible",
    adapterID: "models-dev-openai-compatible",
    kind: "official",
    billingLane: "metered_api",
    settings: {},
  })
  expect(validated.normalizedURL).toBe("https://api.zeta.example/v1")
  expect(validated.modelRoutes.map((route) => route.modelID)).toEqual(["zeta"])

  const account = await control.accountValidate({
    connection: {
      familyID: "anthropic_proxy",
      adapterID: "models-dev-anthropic",
      kind: "official",
      billingLane: "metered_api",
      url: "",
      settings: {},
    },
    authMethod: "api_key",
    credential: { type: "api", key: "alpha-key" },
  })
  expect(account.authClass).toBe("metered")
  expect(account.modelRoutes.map((route) => route.modelID)).toEqual(["alpha"])

  await expect(
    control.connectionValidate({
      familyID: "compatible",
      adapterID: "models-dev-anthropic",
      kind: "official",
      billingLane: "metered_api",
      settings: {},
    }),
  ).rejects.toThrow("provider adapter is not supported")
})

test("provider model discovery rejects literal metadata and link-local destinations", () => {
  for (const url of [
    "http://169.254.169.254",
    "http://[fe80::1]",
    "http://[::ffff:a9fe:a9fe]",
    "https://metadata.google.internal",
    "https://metadata.goog",
    "https://kubernetes.default.svc",
    "http://100.100.100.200",
  ]) {
    expect(() =>
      validateConnection({
        familyID: "openai-compatible",
        adapterID: "openai-chat",
        kind: "local",
        billingLane: "local",
        url,
        settings: {},
      }),
    ).toThrow("reserved or unsafe")
  }
})

test("Ollama connection validation discovers keyless local models", async () => {
  const requested: string[] = []
  const fetcher = (async (input: URL | RequestInfo) => {
    requested.push(String(input))
    return new Response(JSON.stringify({ models: [{ name: "llama3.2" }] }), {
      headers: { "Content-Type": "application/json" },
    })
  }) as typeof fetch
  const control = new ControlPlane(emptyAuth, () => Date.now(), fetcher)

  const result = await control.connectionValidate({
    familyID: "ollama",
    adapterID: "ollama",
    kind: "local",
    billingLane: "local",
    url: "http://127.0.0.1:11434",
    settings: {},
  })

  expect(requested).toEqual(["http://127.0.0.1:11434/api/tags"])
  expect(result.modelRoutes.map((item) => item.modelID)).toEqual(["llama3.2"])
})

test("official cloud connections publish frozen catalog capabilities", async () => {
  const control = new ControlPlane({
    ...emptyAuth,
    modelCatalog: async () => ({
      openai: {
        id: "openai",
        name: "OpenAI",
        env: [],
        models: {
          "gpt-test": {
            id: "gpt-test",
            name: "GPT Test",
            family: "gpt",
            release_date: "2026-01-01",
            attachment: true,
            reasoning: true,
            temperature: true,
            tool_call: true,
            limit: { context: 128_000, output: 16_384 },
            modalities: { input: ["text", "image"], output: ["text"] },
          },
        },
      },
    }),
  })

  const result = await control.connectionValidate({
    familyID: "openai",
    adapterID: "openai-responses",
    kind: "official",
    billingLane: "metered_api",
    settings: {},
  })

  expect(result.modelRoutes[0]?.operations).toContain("vision.describe")
  expect(result.modelRoutes[0]?.capabilities.limit).toEqual({ context: 128_000, output: 16_384 })
  expect(result.modelRoutes[0]?.provenance.catalog).toBe("models.dev-snapshot")
})

test("migrated DeepSeek and Xiaomi API lanes are canonical managed families", async () => {
  const control = new ControlPlane(emptyAuth)
  const catalog = await control.catalog()
  for (const familyID of ["deepseek", "xiaomi"]) {
    const family = catalog.families.find((item) => item.id === familyID)
    expect(family?.authMethods.map((item) => item.id)).toContain("api_key")
    expect(family?.billingLanes).toContain("metered_api")
  }

  const deepseek = await control.connectionValidate({
    familyID: "deepseek",
    adapterID: "openai-chat",
    kind: "official",
    billingLane: "metered_api",
    settings: {},
  })
  expect(deepseek.familyID).toBe("deepseek")

  const xiaomi = await control.accountValidate({
    connection: {
      familyID: "xiaomi",
      adapterID: "mimo-native",
      kind: "official",
      billingLane: "metered_api",
      settings: {},
    },
    authMethod: "api_key",
    credential: { type: "api", key: "migrated-xiaomi-key" },
  })
  expect(xiaomi.authClass).toBe("metered")
})

test("protected local account discovery uses an authorization header", async () => {
  const secret = "protected-local-secret"
  let observedAuthorization = ""
  const fetcher = (async (_input: URL | RequestInfo, init?: RequestInit) => {
    observedAuthorization = new Headers(init?.headers).get("Authorization") ?? ""
    return new Response(JSON.stringify({ data: [{ id: "private-model" }] }), {
      headers: { "Content-Type": "application/json" },
    })
  }) as typeof fetch
  const control = new ControlPlane(emptyAuth, () => Date.now(), fetcher)

  const result = await control.accountValidate({
    connection: {
      familyID: "openai-compatible",
      adapterID: "openai-chat",
      kind: "local",
      billingLane: "local",
      url: "http://127.0.0.1:8080",
      settings: {},
    },
    authMethod: "api_key",
    credential: { type: "api", key: secret },
  })

  expect(observedAuthorization).toBe(`Bearer ${secret}`)
  expect(result.authClass).toBe("local")
  expect(result.modelRoutes.map((item) => item.modelID)).toEqual(["private-model"])
})
