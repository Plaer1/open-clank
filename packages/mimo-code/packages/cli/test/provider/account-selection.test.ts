import { expect, test } from "bun:test"
import { AccountSelection } from "../../src/provider/account-selection"
import { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"

const model = {
  id: ModelID.make("gpt-test"),
  providerID: ProviderID.make("openai"),
  api: {
    id: "gpt-test",
    npm: "@ai-sdk/openai",
    url: "https://api.openai.example/v1",
  },
} as Pick<Provider.Model, "id" | "providerID" | "api">

const account = (accountID: string, credentialRevision: number, connectionID = "openai-api") => ({
  connectionID,
  credentialRequired: true,
  accountID,
  billingLane: "metered_api" as const,
  credentialRevision,
  credential: { type: "api" as const, key: `secret-${accountID}` },
})

test("language model cache identity is account and credential-revision scoped", () => {
  const first = AccountSelection.languageCacheKey(model, account("one", 1))
  expect(first).toBe(AccountSelection.languageCacheKey(model, account("one", 1)))
  expect(first).not.toBe(AccountSelection.languageCacheKey(model, account("two", 1)))
  expect(first).not.toBe(AccountSelection.languageCacheKey(model, account("one", 2)))
  expect(first).not.toBe(AccountSelection.languageCacheKey(model, account("one", 1, "openai-proxy")))
})

test("provider SDK clients are isolated by account scope", () => {
  const options = { baseURL: "https://api.openai.example/v1" }
  const first = Provider.sdkCacheKey(model, options, account("one", 1))
  expect(first).toBe(Provider.sdkCacheKey(model, options, account("one", 1)))
  expect(first).not.toBe(Provider.sdkCacheKey(model, options, account("two", 1)))
  expect(first).not.toBe(Provider.sdkCacheKey(model, options, account("one", 2)))
})

test("cache identity excludes leased credential material", () => {
  const scope = account("one", 7)
  const identity = AccountSelection.cacheIdentity(scope)
  expect(identity).toBe("openai-api/one@7")
  expect(identity).not.toContain(scope.credential.key)
  expect(AccountSelection.cacheIdentity()).toBe("legacy")
})

test("keyless cache identity is isolated by connection without inventing an account", () => {
  const local = (connectionID: string) => ({
    connectionID,
    credentialRequired: false,
  })
  expect(AccountSelection.cacheIdentity(local("ollama-one"))).toBe("ollama-one/keyless")
  expect(AccountSelection.cacheIdentity(local("ollama-one"))).not.toBe(
    AccountSelection.cacheIdentity(local("ollama-two")),
  )
})
