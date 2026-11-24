import { Cause, Context, Effect, Exit } from "effect"
import { ActorRegistry } from "@/actor/registry"
import type { SessionID } from "@/session/schema"
import type { ActorRoutePreflightResult } from "@/acp/openclank-protocol"
import { InstanceState } from "@/effect"
import { realpath } from "node:fs/promises"

export interface RunTurnOptions {
  /** The top-level fork execution already owns the durable lifecycle. */
  readonly executionRevision?: number
  /** Trusted root lineage copied from ACP or the admitted receipt handoff. */
  readonly rootOperationID?: string
  /** Route-only host preflight; credential leases are acquired after the CAS. */
  readonly preflightRoute?: ActorRoutePreflightResult
  readonly frozenReceipts?: readonly import("@/session/user-input").BoundaryReceipt[]
  readonly retirement?: { unknown: boolean; lastInnerTurnID?: string; lastTurnOutcome?: "completed" | "cancelled" | "error" }
}

export type ActorExecutionIdentity = {
  readonly sessionID: SessionID
  readonly actorID: string
  readonly executionRevision: number
  readonly rootOperationID?: string
  /** Typed native-only route evidence, never persisted or exposed to prompts. */
  readonly preflightRoute?: ActorRoutePreflightResult
  /** Distinguishes prompt iterations within one actor execution for C2 policy. */
  readonly innerTurnID?: string
  readonly frozenReceiptBatch?: { current?: readonly import("@/session/user-input").BoundaryReceipt[] }
  readonly retirement?: { unknown: boolean; lastInnerTurnID?: string; lastTurnOutcome?: "completed" | "cancelled" | "error" }
  readonly preparedTurn?: { innerTurnID: string; entered: boolean; finishAttempted: boolean }
}

/** Internal trusted context; never populated from model arguments or hostContext. */
export class ActorExecutionContext extends Context.Service<ActorExecutionContext, ActorExecutionIdentity>()("@opencode/ActorExecutionContext") {}

/** Run one turn without settling the enclosing actor execution. */
const runAdmittedTurn = <A, E, R>(
  sessionID: SessionID,
  actorID: string,
  work: Effect.Effect<A, E, R>,
  options?: RunTurnOptions,
): Effect.Effect<A, E, Exclude<R, ActorExecutionContext> | ActorRegistry.Service> =>
  // Wrap the entire turn in Effect.uninterruptible so that status cleanup
  // always runs even when the fiber is externally interrupted (Fiber.interrupt).
  // The actual work is re-marked interruptible inside so it can be cancelled.
  Effect.uninterruptible(
    Effect.gen(function* () {
      const reg = yield* ActorRegistry.Service
      if (options?.executionRevision !== undefined) {
        const identity: ActorExecutionIdentity = {
          sessionID,
          actorID,
          executionRevision: options.executionRevision,
          ...(options.rootOperationID ? { rootOperationID: options.rootOperationID } : {}),
          ...(options.preflightRoute ? { preflightRoute: options.preflightRoute } : {}),
          ...(options.frozenReceipts ? { frozenReceiptBatch: { current: options.frozenReceipts } } : {}),
          ...(options.retirement ? { retirement: options.retirement } : {}),
        }
        yield* reg.updateTurn(sessionID, actorID).pipe(Effect.ignore)
        const exit: Exit.Exit<A, E> = yield* work.pipe(
          Effect.provideService(ActorExecutionContext, identity),
          Effect.interruptible,
          Effect.exit,
        )
        if (Exit.isSuccess(exit)) return exit.value
        return yield* Effect.failCause(exit.cause) as Effect.Effect<A, E>
      }
      // Compatibility path for direct single-turn callers/tests. Production
      // forks always pass their top-level execution revision.
      const executionRevision = yield* reg.beginExecution(sessionID, actorID)
      if (executionRevision === undefined) return yield* Effect.interrupt
      const identity: ActorExecutionIdentity = {
        sessionID,
        actorID,
        executionRevision,
        ...(options?.rootOperationID ? { rootOperationID: options.rootOperationID } : {}),
        ...(options?.preflightRoute ? { preflightRoute: options.preflightRoute } : {}),
        ...(options?.frozenReceipts ? { frozenReceiptBatch: { current: options.frozenReceipts } } : {}),
        ...(options?.retirement ? { retirement: options.retirement } : {}),
      }
      const exit: Exit.Exit<A, E> = yield* work.pipe(
        Effect.provideService(ActorExecutionContext, identity),
        Effect.interruptible,
        Effect.exit,
      )
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
  ) as Effect.Effect<A, E, Exclude<R, ActorExecutionContext> | ActorRegistry.Service>

export const runTurn = <A, E, R>(sessionID:SessionID, actorID:string, work:Effect.Effect<A,E,R>, options?:RunTurnOptions): Effect.Effect<A,E,Exclude<R,ActorExecutionContext>|ActorRegistry.Service> =>
  Effect.uninterruptibleMask((restore)=>Effect.gen(function* () {
    const registry=yield* ActorRegistry.Service
    if (!(yield* registry.assertDirectoryActive(sessionID,actorID))) return yield* Effect.die(new Error('actor directory scope retired'))
    const directory=yield* InstanceState.directory
    const canonical=yield* Effect.tryPromise({try:()=>realpath(directory),catch:()=>new Error('actor directory unavailable')}).pipe(Effect.orDie)
    const lease=yield* registry.admitExecutionDirectory(canonical)
    if (!lease) return yield* Effect.die(new Error('actor directory unavailable'))
    return yield* restore(runAdmittedTurn(sessionID,actorID,work,options)).pipe(Effect.ensuring(Effect.sync(()=>{if (!options?.retirement?.unknown) lease.release()})))
  })) as Effect.Effect<A,E,Exclude<R,ActorExecutionContext>|ActorRegistry.Service>

function extractErrorString(cause: Cause.Cause<unknown>): string {
  return Cause.pretty(cause)
}

export * as ActorTurn from "./turn"
