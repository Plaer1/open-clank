import { describe, expect, test } from "bun:test"
import { Deferred, Effect, Fiber } from "effect"
import { make } from "../../src/actor/execution-lock"

describe("actor execution lock registry", () => {
  test("serializes a holder and waiter, then retires the idle entry", async () => {
    const registry = make()
    const firstEntered = await Effect.runPromise(Deferred.make<void>())
    const releaseFirst = await Effect.runPromise(Deferred.make<void>())
    const secondEntered = await Effect.runPromise(Deferred.make<void>())
    const order: string[] = []

    const first = Effect.runFork(
      registry.withLock(
        "session:actor",
        Effect.gen(function* () {
          order.push("first")
          yield* Deferred.succeed(firstEntered, undefined)
          yield* Deferred.await(releaseFirst)
        }),
      ),
    )
    await Effect.runPromise(Deferred.await(firstEntered))

    const second = Effect.runFork(
      registry.withLock(
        "session:actor",
        Effect.gen(function* () {
          order.push("second")
          yield* Deferred.succeed(secondEntered, undefined)
        }),
      ),
    )
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(order).toEqual(["first"])
    expect(registry.size()).toBe(1)

    await Effect.runPromise(Deferred.succeed(releaseFirst, undefined))
    await Effect.runPromise(Fiber.join(first))
    await Effect.runPromise(Deferred.await(secondEntered))
    await Effect.runPromise(Fiber.join(second))

    expect(order).toEqual(["first", "second"])
    expect(registry.size()).toBe(0)
  })

  test("retires an unknown-id cancellation lane immediately", async () => {
    const registry = make()

    await Promise.all(
      Array.from({ length: 8 }, (_, index) =>
        Effect.runPromise(registry.withLock(`unknown-session:unknown-actor-${index}`, Effect.void)),
      ),
    )

    expect(registry.size()).toBe(0)
  })

  test("retires an interrupted waiter without releasing the holder's lane", async () => {
    const registry = make()
    const entered = await Effect.runPromise(Deferred.make<void>())
    const release = await Effect.runPromise(Deferred.make<void>())

    const holder = Effect.runFork(
      registry.withLock(
        "session:actor",
        Effect.gen(function* () {
          yield* Deferred.succeed(entered, undefined)
          yield* Deferred.await(release)
        }),
      ),
    )
    await Effect.runPromise(Deferred.await(entered))

    const waiter = Effect.runFork(registry.withLock("session:actor", Effect.void))
    await new Promise((resolve) => setTimeout(resolve, 10))
    await Effect.runPromise(Fiber.interrupt(waiter))
    expect(registry.size()).toBe(1)

    await Effect.runPromise(Deferred.succeed(release, undefined))
    await Effect.runPromise(Fiber.join(holder))
    expect(registry.size()).toBe(0)
  })
})
