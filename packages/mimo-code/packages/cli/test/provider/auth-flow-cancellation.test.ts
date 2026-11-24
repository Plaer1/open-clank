import { expect, test } from "bun:test"
import { Effect } from "effect"

import { commitFlowAuth } from "../../src/provider/auth"
import type { Auth } from "../../src/auth"

function authWriter(initial?: Auth.Info) {
  let current = initial
  const calls: string[] = []
  return {
    calls,
    current: () => current,
    replace: (next: Auth.Info | undefined) => {
      current = next
    },
    writer: {
      get: (_providerID: string) => Effect.succeed(current),
      set: (_providerID: string, next: Auth.Info) =>
        Effect.sync(() => {
          calls.push(`set:${next.type}`)
          current = next
        }),
      remove: (_providerID: string) =>
        Effect.sync(() => {
          calls.push("remove")
          current = undefined
        }),
    },
  }
}

test("cancel during provider auth write removes a newly-created credential", async () => {
  const store = authWriter()
  let cancelled = false
  const originalSet = store.writer.set
  store.writer.set = (providerID, next) =>
    originalSet(providerID, next).pipe(
      Effect.tap(() => Effect.sync(() => {
        cancelled = true
      })),
    )

  const committed = await Effect.runPromise(
    commitFlowAuth(
      store.writer,
      "xiaomi",
      { type: "api", key: "late-write" },
      () => cancelled,
    ),
  )

  expect(committed).toBe(false)
  expect(store.current()).toBeUndefined()
  expect(store.calls).toEqual(["set:api", "remove"])
})

test("cancel during provider auth write restores the prior credential", async () => {
  const prior: Auth.Info = { type: "api", key: "prior" }
  const store = authWriter(prior)
  let checks = 0

  const committed = await Effect.runPromise(
    commitFlowAuth(
      store.writer,
      "xiaomi",
      { type: "api", key: "late-write" },
      () => ++checks > 1,
    ),
  )

  expect(committed).toBe(false)
  expect(store.current()).toEqual(prior)
  expect(store.calls).toEqual(["set:api", "set:api"])
})

test("cancelled flow does not overwrite a newer independent credential", async () => {
  const store = authWriter()
  let cancelled = false
  const newer: Auth.Info = { type: "api", key: "newer" }
  store.writer.set = (_providerID, _next) =>
    Effect.sync(() => {
      store.calls.push("set:api")
      // Simulate another credential mutation winning before the fence runs.
      store.replace(newer)
      cancelled = true
    })

  const committed = await Effect.runPromise(
    commitFlowAuth(
      store.writer,
      "xiaomi",
      { type: "api", key: "late-write" },
      () => cancelled,
    ),
  )

  expect(committed).toBe(false)
  expect(store.current()).toEqual(newer)
  expect(store.calls).toEqual(["set:api"])
})
