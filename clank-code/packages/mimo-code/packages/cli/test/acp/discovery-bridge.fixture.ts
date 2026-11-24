import { ControlPlane, type AuthServices } from "../../src/acp/provider-control"

const auth: AuthServices = {
  methods: async () => ({}),
  authorize: async () => undefined,
  exchange: async () => undefined,
  cancel: async () => {},
  modelCatalog: async () => ({}),
}

const control = new ControlPlane(
  auth,
  () => 1_700_000_000_000,
  (async () => new Response(JSON.stringify(
    process.env.BRIDGE_CASE === "local"
      ? { models: [] }
      : { data: [{ id: "claude-sonnet-4-6", display_name: "Claude Sonnet 4.6" }], has_more: false },
  ))) as unknown as typeof fetch,
)

const result = process.env.BRIDGE_CASE === "local"
  ? await control.handle("_openclank/provider-control/v1/connection/validate", {
      familyID: "local-executor",
      adapterID: "openclank-local-executor",
      kind: "local",
      billingLane: "local",
      url: "",
      settings: {},
    } as any)
  : await control.handle("_openclank/provider-control/v1/account/validate", {
      connection: {
        familyID: "anthropic",
        adapterID: "anthropic-messages",
        kind: "subscription",
        billingLane: "subscription",
        settings: {},
      },
      authMethod: "oauth",
      credential: {
        type: "oauth",
        refresh: "bridge-refresh",
        access: "bridge-access",
        expires: 2_000_000_000_000,
        accountId: "provider-account-bridge",
      },
      accountID: "host-account-bridge",
      credentialRevision: 7,
    })

process.stdout.write(JSON.stringify(result))
