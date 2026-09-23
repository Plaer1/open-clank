import { describe, expect, test } from "bun:test"
import type { Interface as ActorInterface } from "../../src/actor/spawn"
import { makeBindingRegistry } from "../../src/actor/spawn-ref"

describe("actor spawn bindings", () => {
  test("restores the newest live binding after out-of-order disposal", () => {
    const sentinel = {} as ActorInterface
    const actorA = {} as ActorInterface
    const actorB = {} as ActorInterface
    const actorC = {} as ActorInterface
    const registry = makeBindingRegistry(sentinel)
    const disposeA = registry.register(actorA)
    const disposeB = registry.register(actorB)
    const disposeC = registry.register(actorC)

    expect(registry.ref.current).toBe(actorC)
    disposeB()
    expect(registry.ref.current).toBe(actorC)

    disposeC()
    expect(registry.ref.current).toBe(actorA)

    disposeA()
    expect(registry.ref.current).toBe(sentinel)

    disposeB()
    expect(registry.ref.current).toBe(sentinel)
  })
})
