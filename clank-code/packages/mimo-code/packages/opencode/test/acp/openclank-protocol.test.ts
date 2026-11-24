import { expect, test } from "bun:test"
import { createHash } from "node:crypto"
import path from "node:path"
import { OpenClankManagedProtocol } from "../../src/acp/openclank-protocol"
import { ControlPlane, ProviderControlError, type AuthServices, validateConnection } from "../../src/acp/provider-control"

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

test("provider-control handle publishes the seven truthful local recipes", async () => {
  const control = new ControlPlane(emptyAuth)
  const validated = await control.handle("_openclank/provider-control/v1/connection/validate", {
    familyID: "local-executor",
    adapterID: "openclank-local-executor",
    kind: "local",
    billingLane: "local",
    url: "",
    settings: {},
  } as any)
  expect(validated.modelRoutes.map((route) => route.modelID)).toEqual([
    "fastembed/default", "kokoro/Kokoro-82M", "faster-whisper/default",
    "diffusion/default", "realesrgan/x4plus", "briaai/RMBG-1.4", "gfpgan/clean-v1",
  ])
  expect(validated.modelRoutes.every((route) => route.capabilities?.runtimeStatus === "unknown")).toBe(true)
})

test("Vertex remains visible as an unavailable adapter without new routes", async () => {
  const control = new ControlPlane(emptyAuth)
  const result = await control.connectionValidate({
    familyID: "google", adapterID: "google-vertex", kind: "official", billingLane: "metered_api",
    url: "", settings: {},
  })
  expect(result.modelRoutes).toEqual([])
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

test("MiMo browser completion classifies an API key and retains its selected subscription lane", async () => {
  const codeVerifier = "synthetic-code-verifier"
  const connectionID = "mimo-subscription-connection"
  const flowID = "synthetic-mimo-flow-0001"
  const nonce = "synthetic-mimo-nonce"
  const expiresAt = Date.now() + 60_000
  const control = new ControlPlane({
    ...emptyAuth,
    methods: async () => ({
      xiaomi: [{
        type: "oauth",
        acquisitionMethod: "browser",
        credentialKinds: ["api"],
        billingLanes: ["metered_api", "subscription"],
        label: "MiMo browser login",
      }],
    } as any),
    authorize: async () => ({ url: "https://login.example/authorize", method: "code", instructions: "Paste the returned code" }),
    exchange: async () => ({
      type: "api",
      key: "synthetic-token-plan-key",
      metadata: { base_url: "https://token-plan-cn.xiaomimimo.com/v1" },
    } as any),
  })
  const started = await control.oauthStart({
    flowID,
    connectionID,
    providerID: "xiaomi",
    billingLane: "subscription",
    mode: "add",
    nonce,
    method: 0,
    redirectURI: "https://openclank.test/oauth/callback",
    state: "synthetic-state",
    codeVerifierChallenge: createHash("sha256").update(codeVerifier).digest("base64url"),
    expiresAt,
    inputs: {},
  })
  const identity = {
    flowID: started.flowID,
    connectionID,
    providerID: "xiaomi",
    billingLane: "subscription",
    mode: "add",
    nonce,
    codeVerifier,
  }
  await control.oauthCallback({ ...identity, code: "synthetic-code" })
  await new Promise((resolve) => setTimeout(resolve, 0))
  const result = await control.oauthPoll(identity)
  expect(result).toMatchObject({
    status: "complete",
    authMethod: "api_key",
    acquisitionMethod: "browser",
    credentialKind: "api",
    endpointPolicy: "mimo-token-plan-v1",
    billingLane: "subscription",
  })
})

test("MiMo exchange failures retain only their bounded safe error code", async () => {
  const codeVerifier = "synthetic-failing-code-verifier"
  const connectionID = "mimo-failing-connection"
  const control = new ControlPlane({
    ...emptyAuth,
    methods: async () => ({ xiaomi: [{ type: "oauth", acquisitionMethod: "browser", credentialKinds: ["api"], billingLanes: ["subscription"], label: "MiMo browser login" }] } as any),
    authorize: async () => ({ url: "https://login.example/authorize", method: "code", instructions: "Continue" }),
    exchange: async () => { throw new ProviderControlError("private upstream details", "oauth_endpoint_mismatch") },
  })
  const started = await control.oauthStart({
    flowID: "synthetic-mimo-failing-flow",
    connectionID,
    providerID: "xiaomi",
    billingLane: "subscription",
    mode: "add",
    nonce: "synthetic-failing-nonce",
    method: 0,
    redirectURI: "https://openclank.test/oauth/callback",
    state: "synthetic-failing-state",
    codeVerifierChallenge: createHash("sha256").update(codeVerifier).digest("base64url"),
    expiresAt: Date.now() + 60_000,
    inputs: {},
  })
  const identity = {
    flowID: started.flowID,
    connectionID,
    providerID: "xiaomi",
    billingLane: "subscription",
    mode: "add",
    nonce: "synthetic-failing-nonce",
    codeVerifier,
  }
  await control.oauthCallback({ ...identity, code: "synthetic-code" })
  await new Promise((resolve) => setTimeout(resolve, 0))
  expect(await control.oauthPoll(identity)).toMatchObject({ status: "failed", errorCode: "oauth_endpoint_mismatch" })
})

function catalogModel(id: string, npm?: string) {
  return {
    id,
    name: id,
    release_date: "2026-01-01",
    attachment: false,
    reasoning: false,
    temperature: true,
    tool_call: true,
    voice_design: false,
    voice_clone: false,
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
    accountID: "account-anthropic",
    credentialRevision: 1,
    authMethod: "api_key",
    credential: { type: "api", key: "alpha-key" },
  })
  expect(account.authClass).toBe("metered")
  expect(account.modelRoutes.map((route) => route.modelID)).toEqual(["alpha"])
  expect(account.modelRoutes).toEqual(account.discovery.models)
  expect(account.discovery).toMatchObject({ status: "complete", authoritative: true, freshness: "fresh" })

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

test("Copilot enterprise discovery uses the normalized trusted host", async () => {
  let requested = ""
  const fetcher = (async (input: URL | RequestInfo) => {
    requested = String(input)
    return new Response(JSON.stringify({ data: [] }))
  }) as unknown as typeof fetch
  const request = subscriptionRequest("github-copilot", "copilot-chat")
  request.connection.settings = { enterpriseUrl: "https://ghe.example.com/" }
  request.credential = { ...request.credential, enterpriseUrl: "ghe.example.com" } as typeof request.credential
  await new ControlPlane(emptyAuth, () => 1_700_000_000_008, fetcher).accountValidate(request)
  expect(requested).toBe("https://copilot-api.ghe.example.com/models")
  await expect(
    new ControlPlane(emptyAuth, () => 1_700_000_000_009, fetcher).accountValidate({
      ...request,
      connection: { ...request.connection, settings: { enterpriseUrl: "https://ghe.example.com/path" } },
    }),
  ).rejects.toThrow("must contain only a host")
  await expect(
    new ControlPlane(emptyAuth, () => 1_700_000_000_010, fetcher).accountValidate({
      ...request,
      connection: { ...request.connection, settings: { enterpriseUrl: "other.example.com" } },
    }),
  ).rejects.toThrow("does not match the OAuth credential")
})

test("hostile Copilot enterprise hosts are rejected before credentialed fetch", async () => {
  let calls = 0
  let authorization = ""
  const fetcher = (async (_input: URL | RequestInfo, init?: RequestInit) => {
    calls++
    authorization = new Headers(init?.headers).get("Authorization") ?? ""
    return new Response(JSON.stringify({ data: [] }))
  }) as unknown as typeof fetch
  const request = subscriptionRequest("github-copilot", "copilot-chat")
  request.connection.settings = { enterpriseUrl: "https://169.254.169.254" }
  await expect(new ControlPlane(emptyAuth, () => 1_700_000_000_011, fetcher).accountValidate(request)).rejects.toThrow(
    "reserved or unsafe",
  )
  expect(calls).toBe(0)
  expect(authorization).toBe("")
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
    voice_design: false,
    voice_clone: false,
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

test("subscription connection validation publishes no global entitlement routes", async () => {
  let fetchCalls = 0
  const control = new ControlPlane(
    {
      ...emptyAuth,
      modelCatalog: async () => ({
        openai: {
          id: "openai",
          name: "OpenAI",
          env: [],
          models: { "global-only": catalogModel("global-only") },
        },
      }),
    },
    () => 1_700_000_000_012,
    (async () => {
      fetchCalls++
      return new Response(JSON.stringify({ models: [{ slug: "should-not-fetch" }] }))
    }) as unknown as typeof fetch,
  )

  const result = await control.connectionValidate({
    familyID: "openai",
    adapterID: "openai-responses",
    kind: "subscription",
    billingLane: "subscription",
    settings: {},
  })

  expect(result.modelRoutes).toEqual([])
  expect(fetchCalls).toBe(0)
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
    accountID: "account-xiaomi",
    credentialRevision: 1,
    authMethod: "api_key",
    credential: { type: "api", key: "migrated-xiaomi-key", metadata: { base_url: "https://api.xiaomimimo.com/v1" } },
  })
  expect(xiaomi.authClass).toBe("metered")
  expect(xiaomi.acquisitionMethod).toBeNull()
  expect(xiaomi.endpointPolicy).toBe("mimo-metered-api-v1")

  const tokenPlan = await control.accountValidate({
    connection: {
      familyID: "xiaomi",
      adapterID: "mimo-native",
      kind: "subscription",
      billingLane: "subscription",
      settings: {},
    },
    accountID: "account-xiaomi-plan",
    credentialRevision: 1,
    authMethod: "api_key",
    acquisitionMethod: "browser",
    credentialKind: "api",
    credential: { type: "api", key: "synthetic-token-plan-key", metadata: { base_url: "https://token-plan-cn.xiaomimimo.com/v1" } },
  })
  expect(tokenPlan).toMatchObject({ acquisitionMethod: "browser", credentialKind: "api", endpointPolicy: "mimo-token-plan-v1" })
  await expect(control.accountValidate({
    connection: {
      familyID: "xiaomi",
      adapterID: "mimo-native",
      kind: "subscription",
      billingLane: "subscription",
      settings: {},
    },
    accountID: "account-xiaomi-wrong-lane",
    credentialRevision: 1,
    authMethod: "api_key",
    credential: { type: "api", key: "synthetic-metered-key", metadata: { base_url: "https://api.xiaomimimo.com/v1" } },
  })).rejects.toThrow("MiMo credential endpoint does not match its billing lane")
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
    accountID: "account-local",
    credentialRevision: 1,
    authMethod: "api_key",
    credential: { type: "api", key: secret },
  })

  expect(observedAuthorization).toBe(`Bearer ${secret}`)
  expect(result.authClass).toBe("local")
  expect(result.modelRoutes.map((item) => item.modelID)).toEqual(["private-model"])
})

function oauthCredential() {
  return {
    type: "oauth" as const,
    refresh: "refresh-secret",
    access: "access-secret",
    expires: Date.now() + 60_000,
    accountId: "provider-account-a",
  }
}

function subscriptionRequest(familyID: string, adapterID: string, url?: string) {
  return {
    connection: {
      familyID,
      adapterID,
      kind: "subscription" as const,
      billingLane: "subscription" as const,
      ...(url === undefined ? {} : { url }),
      settings: {},
    },
    authMethod: "oauth" as const,
    ...(familyID === "grok" ? { acquisitionMethod: "device" as const } : {}),
    credential: oauthCredential(),
    accountID: "host-account-a",
    credentialRevision: 7,
  }
}

test("subscription discovery is credential-aware, excludes hidden rows, and accepts new Codex IDs", async () => {
  let authorization = ""
  let accountHeader = ""
  const fetcher = (async (_input: URL | RequestInfo, init?: RequestInit) => {
    authorization = new Headers(init?.headers).get("Authorization") ?? ""
    accountHeader = new Headers(init?.headers).get("ChatGPT-Account-Id") ?? ""
    return new Response(
      JSON.stringify({
        models: [
          { slug: "account-model-a", visibility: "show" },
          { slug: "hidden-model", visibility: "hidden" },
          { slug: "new-account-model" },
        ],
      }),
    )
  }) as unknown as typeof fetch
  const control = new ControlPlane(
    {
      ...emptyAuth,
      modelCatalog: async () => ({
        openai: {
          id: "openai",
          name: "OpenAI",
          env: [],
          models: {
            "account-model-a": {
              ...catalogModel("account-model-a"),
              name: "Entitled GPT",
              family: "gpt",
              attachment: true,
              reasoning: true,
              tool_call: true,
    voice_design: false,
    voice_clone: false,
              limit: { context: 128_000, output: 16_384 },
              modalities: { input: ["text", "image"], output: ["text"] },
            },
            "global-only": catalogModel("global-only"),
          },
        },
      }),
    },
    () => 1_700_000_000_000,
    fetcher,
  )

  const result = await control.accountValidate(subscriptionRequest("openai", "openai-responses"))

  expect(authorization).toBe("Bearer access-secret")
  expect(accountHeader).toBe("provider-account-a")
  expect(result.accountID).toBe("host-account-a")
  expect(result.credentialRevision).toBe(7)
  expect(result.discovery).toMatchObject({
    status: "complete",
    accountID: "host-account-a",
    credentialRevision: 7,
    authoritative: true,
    freshness: "fresh",
    provenance: { source: "codex-account-models", observedAt: 1_700_000_000_000 },
  })
  expect(result.modelRoutes.map((item) => item.modelID)).toEqual(["account-model-a", "new-account-model"])
  expect(result.modelRoutes.map((item) => item.modelID)).not.toContain("global-only")
  expect(result.modelRoutes[0]).toMatchObject({
    displayName: "Entitled GPT",
    capabilities: {
      family: "gpt",
      attachment: true,
      reasoning: true,
      tool_call: true,
    voice_design: false,
    voice_clone: false,
      limit: { context: 128_000, output: 16_384 },
    },
    provenance: {
      authority: "managed-engine",
      origin: "account-discovery",
      catalog: "codex-account-models",
      familyID: "openai",
      metadataCatalog: "models.dev-snapshot",
    },
  })
  expect(result.modelRoutes[0]?.operations).toContain("vision.describe")
})

test("Claude subscription discovery uses the pinned OAuth catalog request and preserves account routes", async () => {
  let requestedURL = ""
  let headers = new Headers()
  const fetcher = (async (input: URL | RequestInfo, init?: RequestInit) => {
    requestedURL = String(input)
    headers = new Headers(init?.headers)
    return new Response(JSON.stringify({
      data: [{ id: "claude-sonnet-4-6", display_name: "Claude Sonnet 4.6" }],
      has_more: false,
    }))
  }) as unknown as typeof fetch
  const control = new ControlPlane(emptyAuth, () => 1_700_000_000_000, fetcher)
  const result = await control.handle(
    "_openclank/provider-control/v1/account/validate",
    subscriptionRequest("anthropic", "anthropic-messages"),
  )
  expect(requestedURL).toBe("https://api.anthropic.com/v1/models?limit=1000")
  expect(headers.get("authorization")).toBe("Bearer access-secret")
  expect(headers.get("anthropic-version")).toBe("2023-06-01")
  expect(headers.get("anthropic-beta")).toBe("oauth-2025-04-20")
  expect(result.discovery.status).toBe("complete")
  expect(result.modelRoutes).toEqual(result.discovery.models)
  expect(result.modelRoutes[0]?.modelID).toBe("claude-sonnet-4-6")
  expect(result.modelRoutes[0]?.provenance).toMatchObject({
    authority: "managed-engine",
    origin: "account-discovery",
    catalog: "claude-account-models",
  })
})

test("Claude discovery reports pagination as partial and malformed payloads unavailable", async () => {
  const partial = new ControlPlane(emptyAuth, () => 1_700_000_000_000, (async () => new Response(JSON.stringify({
    data: [{ id: "claude-haiku-4-5", display_name: "Claude Haiku" }], has_more: true,
  }))) as unknown as typeof fetch)
  const partialResult = await partial.accountValidate(subscriptionRequest("anthropic", "anthropic-messages"))
  expect(partialResult.discovery.status).toBe("partial")
  expect(partialResult.discovery.errorCode).toBe("discovery_partial")

  const malformed = new ControlPlane(emptyAuth, () => 1_700_000_000_000, (async () => new Response("{}")) as unknown as typeof fetch)
  const malformedResult = await malformed.accountValidate(subscriptionRequest("anthropic", "anthropic-messages"))
  expect(malformedResult.discovery.status).toBe("unavailable")
  expect(malformedResult.discovery.errorCode).toBe("discovery_unavailable")
  expect(malformedResult.modelRoutes).toEqual([])
})

test("subscription discovery marks malformed mixed Copilot rows partial and uses refresh auth", async () => {
  let authorization = ""
  const fetcher = (async (_input: URL | RequestInfo, init?: RequestInit) => {
    authorization = new Headers(init?.headers).get("Authorization") ?? ""
    return new Response(
      JSON.stringify({
        data: [
          { id: "copilot-new", name: "Copilot New", capabilities: {} },
          { id: 42 },
          { id: "hidden", model_picker_enabled: false, capabilities: {} },
        ],
      }),
    )
  }) as unknown as typeof fetch
  const control = new ControlPlane(emptyAuth, () => 1_700_000_000_001, fetcher)

  const result = await control.accountValidate(subscriptionRequest("github-copilot", "copilot-chat"))

  expect(authorization).toBe("Bearer refresh-secret")
  expect(result.discovery).toMatchObject({ status: "partial", errorCode: "discovery_partial", authoritative: false })
  expect(result.discovery.models.map((item) => item.modelID)).toEqual(["copilot-new"])
  expect(result.discovery.models[0]?.capabilities).toMatchObject({ subscription: true })
})

test("Copilot discovery retains known metadata and maps authoritative capabilities", async () => {
  const control = new ControlPlane(
    {
      ...emptyAuth,
      modelCatalog: async () => ({
        "github-copilot": {
          id: "github-copilot",
          name: "GitHub Copilot",
          env: [],
          models: {
            "copilot-known": {
              ...catalogModel("copilot-known"),
              name: "Frozen Copilot Label",
              family: "copilot",
              attachment: true,
              reasoning: true,
              tool_call: true,
    voice_design: false,
    voice_clone: false,
              limit: { context: 64_000, output: 8_192 },
              modalities: { input: ["text", "image"], output: ["text"] },
            },
          },
        },
      }),
    },
    () => 1_700_000_000_013,
    (async () =>
      new Response(
        JSON.stringify({
          data: [
            {
              id: "copilot-known",
              name: "Provider Label",
              capabilities: {
                family: "copilot-authoritative",
                limits: {
                  max_context_window_tokens: 64_000,
                  max_prompt_tokens: 56_000,
                  max_output_tokens: 8_192,
                  vision: { supported_media_types: ["image/png"] },
                },
                supports: {
                  adaptive_thinking: true,
                  streaming: true,
                  tool_calls: true,
                  vision: true,
                },
              },
            },
            { id: "copilot-new", capabilities: {} },
          ],
        }),
      )) as unknown as typeof fetch,
  )

  const result = await control.accountValidate(subscriptionRequest("github-copilot", "copilot-chat"))
  const known = result.discovery.models.find((model) => model.modelID === "copilot-known")
  expect(known).toMatchObject({
    displayName: "Frozen Copilot Label",
    capabilities: {
      family: "copilot-authoritative",
      limit: { context: 64_000, input: 56_000, output: 8_192 },
      reasoning: true,
      tool_call: true,
    voice_design: false,
    voice_clone: false,
      vision: true,
      vision_media_types: ["image/png"],
    },
  })
  expect(known?.operations).toContain("vision.describe")
  expect(result.discovery.models.map((model) => model.modelID)).toEqual(["copilot-known", "copilot-new"])
})

test("a valid empty subscription inventory is authoritative and distinct from failure", async () => {
  const fetcher = (async () => new Response(JSON.stringify({ data: [] }))) as unknown as typeof fetch
  const control = new ControlPlane(emptyAuth, () => 1_700_000_000_002, fetcher)

  const result = await control.accountValidate(subscriptionRequest("grok", "grok-cli-chat"))

  expect(result.discovery).toMatchObject({ status: "complete", authoritative: true, models: [] })
  expect("errorCode" in result.discovery).toBe(false)
  expect(result.modelRoutes).toEqual([])
})

test("new Grok subscription binds only its proxy while legacy xAI subscription routes remain unchanged", () => {
  const grok = validateConnection({
    familyID: "grok",
    adapterID: "grok-cli-chat",
    kind: "subscription",
    billingLane: "subscription",
    settings: {},
  })
  expect(grok.normalizedURL).toBe("https://cli-chat-proxy.grok.com/v1")
  expect(() => validateConnection({
    familyID: "grok",
    adapterID: "grok-cli-chat",
    kind: "subscription",
    billingLane: "subscription",
    url: "https://api.x.ai/v1",
    settings: {},
  })).toThrow("endpoint does not match")

  const legacyXai = validateConnection({
    familyID: "xai",
    adapterID: "xai-responses",
    kind: "subscription",
    billingLane: "subscription",
    url: "https://api.x.ai/v1",
    settings: {},
  })
  expect(legacyXai.normalizedURL).toBe("https://api.x.ai/v1")
})

test("Grok discovery uses the leased token, CLI auth header, redirect fence, and account capabilities", async () => {
  let requestURL = ""
  let requestHeaders = new Headers()
  let requestRedirect: RequestRedirect | undefined
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    requestURL = String(input)
    requestHeaders = new Headers(init?.headers)
    requestRedirect = init?.redirect
    return new Response(JSON.stringify({
      data: [{
        id: "account-grok-model",
        capabilities: {
          tool_call: true,
          modalities: { input: ["text", "image"], output: ["text"] },
        },
      }],
    }))
  }) as unknown as typeof fetch
  const result = await new ControlPlane(emptyAuth, () => 1_700_000_000_008, fetcher)
    .accountValidate(subscriptionRequest("grok", "grok-cli-chat"))
  expect(requestURL).toBe("https://cli-chat-proxy.grok.com/v1/models")
  expect(requestHeaders.get("authorization")).toBe("Bearer access-secret")
  expect(requestHeaders.get("x-xai-token-auth")).toBe("xai-grok-cli")
  expect(requestRedirect).toBe("error")
  expect(result.endpointPolicy).toBe("grok-cli-proxy-v1")
  expect(result.modelRoutes.map((model) => model.modelID)).toEqual(["account-grok-model"])
  expect(result.modelRoutes[0]?.operations).toContain("vision.describe")
  expect(result.modelRoutes[0]?.capabilities.tool_call).toBe(true)
})

test("subscription discovery never falls back to the frozen global catalog", async () => {
  const fetcher = (async () => new Response(JSON.stringify({ data: [] }), { status: 503 })) as unknown as typeof fetch
  const control = new ControlPlane(
    {
      ...emptyAuth,
      modelCatalog: async () => ({
        grok: {
          id: "grok",
          name: "Grok subscription",
          env: [],
          models: { frozen: catalogModel("frozen") },
        },
      }),
    },
    () => 1_700_000_000_003,
    fetcher,
  )

  const result = await control.accountValidate(subscriptionRequest("grok", "grok-cli-chat"))

  expect(result.discovery.status).toBe("unavailable")
  expect(result.discovery.errorCode).toBe("discovery_unavailable")
  expect(result.modelRoutes).toEqual([])
})

test("discovery classifies malformed JSON, auth failure, throttling and oversized inventories without echoing secrets", async () => {
  const cases = [
    { status: 200, body: "not-json", expected: "discovery_unavailable" },
    { status: 401, body: "access-secret", expected: "reauth_required" },
    { status: 403, body: "refresh-secret", expected: "reauth_required" },
    { status: 429, body: "access-secret", expected: "discovery_unavailable" },
  ] as const
  for (const item of cases) {
    const fetcher = (async () => new Response(item.body, { status: item.status })) as unknown as typeof fetch
    const control = new ControlPlane(emptyAuth, () => 1_700_000_000_004, fetcher)
    const result = await control.accountValidate(subscriptionRequest("grok", "grok-cli-chat"))
    expect(result.discovery.status === "reauth_required" ? "reauth_required" : result.discovery.errorCode).toBe(item.expected)
    expect(JSON.stringify(result.discovery)).not.toContain("secret")
  }
})

test("discovery caps account model inventories at 4096 routes", async () => {
  const models = Array.from({ length: 4_097 }, (_, index) => ({ id: `new-model-${index}` }))
  const fetcher = (async () => new Response(JSON.stringify({ data: models }))) as unknown as typeof fetch
  const control = new ControlPlane(emptyAuth, () => 1_700_000_000_005, fetcher)

    const result = await control.accountValidate(subscriptionRequest("grok", "grok-cli-chat"))

  expect(result.discovery.status).toBe("partial")
  expect(result.discovery.errorCode).toBe("discovery_partial")
  expect(result.discovery.models).toHaveLength(4_096)
})

test("discovery timeout and bounded response bodies are unavailable without secrets", async () => {
  let aborted = false
  const timeoutFetcher = (async (_input: URL | RequestInfo, init?: RequestInit) => {
    return await new Promise<Response>((_resolve, reject) => {
      const signal = init?.signal
      signal?.addEventListener(
        "abort",
        () => {
          aborted = true
          reject(new DOMException("timed out", "TimeoutError"))
        },
        { once: true },
      )
    })
  }) as unknown as typeof fetch
  const timedOut = await new ControlPlane(emptyAuth, () => 1_700_000_000_006, timeoutFetcher, 1).accountValidate(
    subscriptionRequest("grok", "grok-cli-chat"),
  )
  expect(aborted).toBe(true)
  expect(timedOut.discovery).toMatchObject({ status: "unavailable", errorCode: "discovery_unavailable", freshness: "unknown" })
  expect(JSON.stringify(timedOut.discovery)).not.toContain("secret")

  const oversized = "{" + "x".repeat(2 * 1024 * 1024) + "}"
  const bodyFetcher = (async () => new Response(oversized)) as unknown as typeof fetch
  const tooLarge = await new ControlPlane(emptyAuth, () => 1_700_000_000_007, bodyFetcher).accountValidate(
    subscriptionRequest("grok", "grok-cli-chat"),
  )
  expect(tooLarge.discovery).toMatchObject({ status: "unavailable", errorCode: "discovery_unavailable" })
})

test("discovery preserves long Retry-After values and ignores missing or malformed headers", async () => {
  const longDelay = (async () => new Response("busy", { status: 429, headers: { "Retry-After": "45" } })) as unknown as typeof fetch
  const delayed = await new ControlPlane(emptyAuth, () => 1_700_000_000_000, longDelay).accountValidate(
    subscriptionRequest("grok", "grok-cli-chat"),
  )
  expect(delayed.discovery).toMatchObject({ retryable: true, retryAfterMs: 45_000 })

  const malformed = (async () => new Response("busy", { status: 429, headers: { "Retry-After": "not-a-delay" } })) as unknown as typeof fetch
  const result = await new ControlPlane(emptyAuth, () => 1_700_000_000_000, malformed).accountValidate(
    subscriptionRequest("grok", "grok-cli-chat"),
  )
  expect(result.discovery.retryable).toBe(true)
  expect(result.discovery.retryAfterMs).toBeUndefined()
})
