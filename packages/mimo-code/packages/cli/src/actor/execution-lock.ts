import { Effect, Semaphore } from "effect"

type Entry = {
  semaphore: Semaphore.Semaphore
  holders: number
  waiters: number
}

export interface ExecutionLockRegistry {
  readonly withLock: <A, E, R>(key: string, effect: Effect.Effect<A, E, R>) => Effect.Effect<A, E, R>
  readonly size: () => number
}

/**
 * A keyed execution lane that drops idle semaphores. The entry lookup happens
 * when the effect starts, rather than when it is assembled, so a delayed
 * caller cannot retain an entry that another caller has already retired.
 */
export const make = (): ExecutionLockRegistry => {
  const entries = new Map<string, Entry>()

  const retire = (key: string, entry: Entry) => {
    if (entry.holders !== 0 || entry.waiters !== 0) return
    if (entries.get(key) === entry) entries.delete(key)
  }

  const withLock = <A, E, R>(key: string, effect: Effect.Effect<A, E, R>) =>
    Effect.suspend(() => {
      const entry = entries.get(key) ?? {
        semaphore: Semaphore.makeUnsafe(1),
        holders: 0,
        waiters: 0,
      }
      entries.set(key, entry)
      entry.waiters++
      let acquired = false
      return entry.semaphore
        .withPermits(1)(
          Effect.gen(function* () {
            acquired = true
            entry.waiters--
            entry.holders++
            return yield* effect
          }),
        )
        .pipe(
          Effect.ensuring(
            Effect.sync(() => {
              if (acquired) entry.holders--
              else entry.waiters--
              retire(key, entry)
            }),
          ),
        )
    })

  return {
    withLock,
    size: () => entries.size,
  }
}
