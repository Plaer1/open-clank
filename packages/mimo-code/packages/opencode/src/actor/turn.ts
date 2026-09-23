import { Cause, Effect, Exit } from "effect"
import { ActorRegistry } from "@/actor/registry"
import type { SessionID } from "@/session/schema"

export interface RunTurnOptions {
  /** The top-level fork execution already owns the durable lifecycle. */
  readonly executionRevision?: number
}

/** Run one turn without settling the enclosing actor execution. */
export const runTurn = <A, E>(
  sessionID: SessionID,
  actorID: string,
  work: Effect.Effect<A, E>,
  options?: RunTurnOptions,
): Effect.Effect<A, E, ActorRegistry.Service> =>
  // Wrap the entire turn in Effect.uninterruptible so that status cleanup
  // always runs even when the fiber is externally interrupted (Fiber.interrupt).
  // The actual work is re-marked interruptible inside so it can be cancelled.
  Effect.uninterruptible(
    Effect.gen(function* () {
      const reg = yield* ActorRegistry.Service
      if (options?.executionRevision !== undefined) {
        yield* reg.updateTurn(sessionID, actorID).pipe(Effect.ignore)
        const exit: Exit.Exit<A, E> = yield* work.pipe(Effect.interruptible, Effect.exit)
        if (Exit.isSuccess(exit)) return exit.value
        return yield* Effect.failCause(exit.cause) as Effect.Effect<A, E>
      }
      // Compatibility path for direct single-turn callers/tests. Production
      // forks always pass their top-level execution revision.
      const executionRevision = yield* reg.beginExecution(sessionID, actorID)
      if (executionRevision === undefined) yield* Effect.interrupt
      const exit: Exit.Exit<A, E> = yield* work.pipe(Effect.interruptible, Effect.exit)
      if (Exit.isSuccess(exit)) {
        yield* reg.settleExecution(sessionID, actorID, executionRevision, "success").pipe(Effect.ignore)
        return exit.value
      }
      const cause = exit.cause
      const cancelled = Cause.hasInterruptsOnly(cause)
      yield* reg
        .settleExecution(
          sessionID,
          actorID,
          executionRevision,
          cancelled ? "cancelled" : "failure",
          cancelled ? undefined : extractErrorString(cause),
        )
        .pipe(Effect.ignore)
      return yield* Effect.failCause(cause) as Effect.Effect<A, E>
    }),
  ) as Effect.Effect<A, E, ActorRegistry.Service>

function extractErrorString(cause: Cause.Cause<unknown>): string {
  return Cause.pretty(cause)
}

export * as ActorTurn from "./turn"
