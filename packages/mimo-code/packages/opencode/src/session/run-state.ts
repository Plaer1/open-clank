import { EffectLogger, InstanceState } from "@/effect"
import { Runner } from "@/effect"
import { Effect, Layer, Scope, Context } from "effect"
import * as Session from "./session"
import { MessageV2 } from "./message-v2"
import { SessionID } from "./schema"
import { SessionStatus } from "./status"
import { ActorRegistry } from "@/actor/registry"

export interface Interface {
  readonly busyGuard?: (sessionID: SessionID, agentID?: string) => Effect.Effect<() => boolean>
  readonly assertNotBusy: (sessionID: SessionID, agentID?: string) => Effect.Effect<void>
  readonly cancel: (sessionID: SessionID) => Effect.Effect<void>
  readonly cancelActor: (sessionID: SessionID, agentID: string) => Effect.Effect<void>
  readonly ensureRunning: (
    sessionID: SessionID,
    agentID: string,
    onInterrupt: Effect.Effect<MessageV2.WithParts>,
    work: Effect.Effect<MessageV2.WithParts>,
  ) => Effect.Effect<MessageV2.WithParts>
  readonly start: (
    sessionID: SessionID,
    agentID: string,
    onInterrupt: Effect.Effect<MessageV2.WithParts>,
    work: Effect.Effect<MessageV2.WithParts>,
  ) => Effect.Effect<void>
  readonly startAndWait: (
    sessionID: SessionID,
    agentID: string,
    onInterrupt: Effect.Effect<MessageV2.WithParts>,
    work: Effect.Effect<MessageV2.WithParts>,
  ) => Effect.Effect<MessageV2.WithParts>
  readonly startShell: (
    sessionID: SessionID,
    onInterrupt: Effect.Effect<MessageV2.WithParts>,
    work: Effect.Effect<MessageV2.WithParts>,
  ) => Effect.Effect<MessageV2.WithParts>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionRunState") {}

const runnerKey = (sessionID: SessionID, agentID: string) => `${sessionID}:${agentID}`

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const status = yield* SessionStatus.Service
    const registry = yield* ActorRegistry.Service
    const elog = EffectLogger.create({ service: "SessionRunState" })

    const state = yield* InstanceState.make(
      Effect.fn("SessionRunState.state")(function* () {
        const scope = yield* Scope.Scope
        const runners = new Map<string, Runner.Runner<MessageV2.WithParts>>()
        yield* Effect.addFinalizer(
          Effect.fnUntraced(function* () {
            yield* Effect.forEach(runners.values(), (runner) => runner.cancel, {
              concurrency: "unbounded",
              discard: true,
            })
            runners.clear()
          }),
        )
        return { runners, scope }
      }),
    )

    const runner = Effect.fn("SessionRunState.runner")(function* (
      sessionID: SessionID,
      agentID: string,
      onInterrupt: Effect.Effect<MessageV2.WithParts>,
    ) {
      const key = runnerKey(sessionID, agentID)
      const data = yield* InstanceState.get(state)
      const existing = data.runners.get(key)
      if (existing) return existing
      const isMain = agentID === "main"
      const next = Runner.make<MessageV2.WithParts>(data.scope, {
        label: key,
        onReentryWarn: (info) => elog.warn("runner-reentry", info),
        onIdle: isMain
          ? Effect.gen(function* () {
              data.runners.delete(key)
              yield* status.set(sessionID, { type: "idle" })
            })
          : Effect.sync(() => {
              data.runners.delete(key)
            }),
        onBusy: isMain ? status.set(sessionID, { type: "busy" }) : Effect.void,
        onInterrupt,
        busy: () => {
          throw new Session.BusyError(sessionID)
        },
      })
      data.runners.set(key, next)
      return next
    })

    const busyGuard = Effect.fn("SessionRunState.busyGuard")(function* (sessionID: SessionID, agentID = "main") {
      const data = yield* InstanceState.get(state)
      return () => data.runners.has(runnerKey(sessionID, agentID))
    })

    const assertNotBusy = Effect.fn("SessionRunState.assertNotBusy")(function* (sessionID: SessionID, agentID = "main") {
      const data = yield* InstanceState.get(state)
      const existing = data.runners.get(runnerKey(sessionID, agentID))
      if (existing?.busy) throw new Session.BusyError(sessionID)
    })

    const cancel = Effect.fn("SessionRunState.cancel")(function* (sessionID: SessionID) {
      const key = runnerKey(sessionID, "main")
      const data = yield* InstanceState.get(state)
      const existing = data.runners.get(key)
      if (!existing || !existing.busy) {
        yield* status.set(sessionID, { type: "idle" })
        return
      }
      yield* existing.cancel
    })

    const cancelActor = Effect.fn("SessionRunState.cancelActor")(function* (
      sessionID: SessionID,
      agentID: string,
    ) {
      const key = runnerKey(sessionID, agentID)
      const data = yield* InstanceState.get(state)
      const existing = data.runners.get(key)
      if (!existing || !existing.busy) return
      yield* existing.cancel
    })

    // The lease is acquired by the owned work fiber, not by its caller: start()
    // may return before that fiber and its children have actually settled.
    const guardedWork = (sessionID: SessionID, agentID: string, work: Effect.Effect<MessageV2.WithParts>) =>
      Effect.acquireUseRelease(
        Effect.gen(function* () {
          if (!(yield* registry.assertDirectoryActive(sessionID))) return yield* Effect.die(new Error("Execution directory is retired"))
          const actor = yield* registry.get(sessionID, agentID)
          if (actor && !(yield* registry.assertDirectoryActive(sessionID, agentID))) return yield* Effect.die(new Error("Execution directory is retired"))
          const cwd = yield* InstanceState.directory
          const directory = yield* Effect.tryPromise({try: async () => (await import("node:fs/promises")).realpath(cwd), catch: () => new Error("Execution directory unavailable")}).pipe(Effect.orDie)
          const lease = yield* registry.admitExecutionDirectory(directory)
          if (!lease) return yield* Effect.die(new Error("Execution directory unavailable"))
          return lease
        }),
        (lease) => Effect.gen(function* () {
          if (!(yield* registry.assertDirectoryActive(sessionID))) return yield* Effect.die(new Error("Execution directory is retired"))
          const actor = yield* registry.get(sessionID, agentID)
          if (actor && !(yield* registry.assertDirectoryActive(sessionID, agentID))) return yield* Effect.die(new Error("Execution directory is retired"))
          return yield* work
        }),
        (lease) => Effect.sync(() => { lease.release() }),
      )

    const ensureRunning = Effect.fn("SessionRunState.ensureRunning")(function* (
      sessionID: SessionID,
      agentID: string,
      onInterrupt: Effect.Effect<MessageV2.WithParts>,
      work: Effect.Effect<MessageV2.WithParts>,
    ) {
      return yield* (yield* runner(sessionID, agentID, onInterrupt)).ensureRunning(guardedWork(sessionID, agentID, work))
    })

    const start = Effect.fn("SessionRunState.start")(function* (
      sessionID: SessionID,
      agentID: string,
      onInterrupt: Effect.Effect<MessageV2.WithParts>,
      work: Effect.Effect<MessageV2.WithParts>,
    ) {
      yield* (yield* runner(sessionID, agentID, onInterrupt)).start(guardedWork(sessionID, agentID, work))
    })

    const startAndWait = Effect.fn("SessionRunState.startAndWait")(function* (
      sessionID: SessionID,
      agentID: string,
      onInterrupt: Effect.Effect<MessageV2.WithParts>,
      work: Effect.Effect<MessageV2.WithParts>,
    ) {
      return yield* (yield* runner(sessionID, agentID, onInterrupt)).startAndWait(guardedWork(sessionID, agentID, work))
    })

    const startShell = Effect.fn("SessionRunState.startShell")(function* (
      sessionID: SessionID,
      onInterrupt: Effect.Effect<MessageV2.WithParts>,
      work: Effect.Effect<MessageV2.WithParts>,
    ) {
      return yield* (yield* runner(sessionID, "main", onInterrupt)).startShell(guardedWork(sessionID, "main", work))
    })

    return Service.of({ busyGuard, assertNotBusy, cancel, cancelActor, ensureRunning, start, startAndWait, startShell })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(SessionStatus.defaultLayer), Layer.provide(ActorRegistry.defaultLayer))

export * as SessionRunState from "./run-state"
