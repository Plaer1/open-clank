import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { Auth } from "../../src/auth"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const it = testEffect(Layer.mergeAll(Auth.defaultLayer, CrossSpawnSpawner.defaultLayer))

describe("Auth account pools", () => {
  it.live("persists multiple ordered accounts without overwriting compatibility behavior", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const auth = yield* Auth.Service
        yield* auth.remove("test-openai-api-pool")
        yield* auth.putAccount({
          connectionID: "test-openai-api-pool",
          providerID: "openai",
          billingLane: "metered_api",
          accountID: "account-b",
          label: "B",
          order: 20,
          expectedRevision: 0,
          credential: { type: "api", key: "key-b" },
        })
        yield* auth.putAccount({
          connectionID: "test-openai-api-pool",
          providerID: "openai",
          billingLane: "metered_api",
          accountID: "account-a",
          label: "A",
          order: 10,
          expectedRevision: 0,
          credential: { type: "api", key: "key-a" },
        })

        expect((yield* auth.listAccounts("test-openai-api-pool")).map((account) => account.id)).toEqual([
          "account-a",
          "account-b",
        ])
        expect(yield* auth.get("test-openai-api-pool")).toEqual({ type: "api", key: "key-a" })
        const snapshot = yield* auth.snapshot()
        expect(snapshot.version).toBe(2)
        expect(Object.keys(snapshot.pools["test-openai-api-pool"]!.accounts)).toHaveLength(2)
        yield* auth.remove("test-openai-api-pool")
      }),
    ),
  )

  it.live("fences credential replacement with the credential revision", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const auth = yield* Auth.Service
        yield* auth.remove("test-chatgpt-subscription-pool")
        const created = yield* auth.putAccount({
          connectionID: "test-chatgpt-subscription-pool",
          providerID: "openai",
          billingLane: "subscription",
          accountID: "chatgpt-one",
          label: "Personal",
          expectedRevision: 0,
          credential: { type: "oauth", access: "access-1", refresh: "refresh-1", expires: 1 },
        })
        const refreshed = yield* auth.replaceCredential({
          connectionID: "test-chatgpt-subscription-pool",
          accountID: "chatgpt-one",
          expectedRevision: created.credentialRevision,
          credential: { type: "oauth", access: "access-2", refresh: "refresh-2", expires: 2 },
        })

        expect(refreshed.revision).toBe(created.revision + 1)
        expect(refreshed.credentialRevision).toBe(created.credentialRevision + 1)
        const conflict = yield* auth
          .replaceCredential({
            connectionID: "test-chatgpt-subscription-pool",
            accountID: "chatgpt-one",
            expectedRevision: created.credentialRevision,
            credential: { type: "oauth", access: "stale", refresh: "stale", expires: 3 },
          })
          .pipe(Effect.flip)
        expect(conflict._tag).toBe("AuthRevisionConflict")
        expect((yield* auth.getAccount("test-chatgpt-subscription-pool", "chatgpt-one"))?.credential).toEqual(
          refreshed.credential,
        )
        yield* auth.remove("test-chatgpt-subscription-pool")
      }),
    ),
  )

  it.live("keeps subscription and metered API accounts in separate rotation boundaries", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const auth = yield* Auth.Service
        yield* auth.remove("test-openai-subscription-pool")
        yield* auth.remove("test-openai-metered-pool")
        yield* auth.putAccount({
          connectionID: "test-openai-subscription-pool",
          providerID: "openai",
          billingLane: "subscription",
          accountID: "subscription-one",
          label: "Subscription",
          expectedRevision: 0,
          credential: { type: "oauth", access: "access", refresh: "refresh", expires: 1 },
        })
        yield* auth.putAccount({
          connectionID: "test-openai-metered-pool",
          providerID: "openai",
          billingLane: "metered_api",
          accountID: "api-one",
          label: "API",
          expectedRevision: 0,
          credential: { type: "api", key: "paid-api-key" },
        })

        expect((yield* auth.listAccounts("test-openai-subscription-pool")).map((account) => account.id)).toEqual([
          "subscription-one",
        ])
        expect((yield* auth.listAccounts("test-openai-metered-pool")).map((account) => account.id)).toEqual(["api-one"])
        const snapshot = yield* auth.snapshot()
        expect(snapshot.pools["test-openai-subscription-pool"]?.billingLane).toBe("subscription")
        expect(snapshot.pools["test-openai-metered-pool"]?.billingLane).toBe("metered_api")
        yield* auth.remove("test-openai-subscription-pool")
        yield* auth.remove("test-openai-metered-pool")
      }),
    ),
  )
})
