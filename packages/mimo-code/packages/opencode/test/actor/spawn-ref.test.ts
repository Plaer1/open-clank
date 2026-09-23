import { describe, expect, test } from "bun:test"
import type { Interface as ActorInterface } from "../../src/actor/spawn"
import { registerBinding, spawnRef } from "../../src/actor/spawn-ref"

describe("actor spawn bindings", () => {
  test("restores the newest live binding after out-of-order disposal", () => {
    const previous = spawnRef.current
    const sentinel = {} as ActorInterface
    const actorA = {} as ActorInterface
    const actorB = {} as ActorInterface
    spawnRef.current = sentinel
    let disposeA: (() => void) | undefined
    let disposeB: (() => void) | undefined
    try {
      disposeA = registerBinding(actorA)
      disposeB = registerBinding(actorB)

      expect(spawnRef.current).toBe(actorB)
      disposeA()
      expect(spawnRef.current).toBe(actorB)

      disposeB()
      expect(spawnRef.current).toBe(sentinel)

      disposeB()
      expect(spawnRef.current).toBe(sentinel)
    } finally {
      disposeB?.()
      disposeA?.()
      spawnRef.current = previous
    }
  })
})
