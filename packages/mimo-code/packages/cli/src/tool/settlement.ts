import { Context, Effect, Exit } from "effect"

export const ResultRef = Context.Reference<{
  callID: string
  result?: Effect.Effect<unknown, unknown>
} | undefined>("~opencode/ToolSettlement", { defaultValue: () => undefined })

export const record = (callID: string | undefined, result: Effect.Effect<unknown, unknown>) =>
  Effect.gen(function* () {
    const current = yield* ResultRef
    if (current && current.callID === callID) current.result = result
  })

export function promise<A, B, E, R>(
  start: (signal: AbortSignal) => PromiseLike<A>,
  consume: (exit: Exit.Exit<A, unknown>) => Effect.Effect<B, E, R>,
): Effect.Effect<B, E | unknown, R> {
  return Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
    const controller = new AbortController()
    const pending = yield* Effect.try({
      try: () => Promise.resolve(start(controller.signal)).then(Exit.succeed, Exit.fail),
      catch: (error) => error,
    })
    const wait = Effect.promise(() => pending)
    const outcome = yield* restore(wait).pipe(Effect.exit)
    if (Exit.isSuccess(outcome)) return yield* consume(outcome.value)

    controller.abort()
    const settled = yield* wait
    yield* consume(settled)
    return yield* Effect.failCause(outcome.cause)
  }))
}
