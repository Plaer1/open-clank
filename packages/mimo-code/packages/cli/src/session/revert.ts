import z from "zod"
import { Effect, Layer, Context } from "effect"
import { Snapshot } from "../snapshot"
import { SyncEvent } from "../sync"
import { Log } from "../util"
import * as Session from "./session"
import { MessageV2 } from "./message-v2"
import { SessionID, MessageID, PartID } from "./schema"
import { SessionRunState } from "./run-state"
import { ActorRegistry } from "@/actor/registry"
import { InstanceState } from "@/effect"

const log = Log.create({ service: "session.revert" })

export const RevertInput = z.object({
  sessionID: SessionID.zod,
  messageID: MessageID.zod,
  partID: PartID.zod.optional(),
})
export type RevertInput = z.infer<typeof RevertInput>

export interface Interface {
  readonly revert: (input: RevertInput) => Effect.Effect<Session.Info, Session.BusyError>
  readonly unrevert: (input: { sessionID: SessionID }) => Effect.Effect<Session.Info, Session.BusyError>
  readonly cleanup: (session: Session.Info) => Effect.Effect<void>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionRevert") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const sessions = yield* Session.Service
    const state = yield* SessionRunState.Service
    const registry = yield* ActorRegistry.Service

    // A child that does not reach its exit observation retains the lease.
    const guarded = <A, E, R>(sessionID: SessionID, work: (snapshot: <B, F, Q>(op: Effect.Effect<B, F, Q>) => Effect.Effect<B, F, Q>) => Effect.Effect<A, E, R>) => {
      let outstanding = 0
      const observer = { begin: () => { outstanding++ }, settled: () => { outstanding-- } }
      const snapshot = <B, F, Q>(op: Effect.Effect<B, F, Q>) => op.pipe(
        Effect.provideService(Snapshot.ChildSettlement, observer),
        Effect.uninterruptible,
      )
      return Effect.acquireUseRelease(
        Effect.gen(function* () {
          if (!(yield* registry.assertDirectoryActive(sessionID))) return yield* Effect.die(new Error("Execution directory is retired"))
          const cwd = yield* InstanceState.directory
          const directory = yield* Effect.tryPromise({ try: async () => (await import("node:fs/promises")).realpath(cwd), catch: () => new Error("Execution directory unavailable") }).pipe(Effect.orDie)
          const lease = yield* registry.admitExecutionDirectory(directory)
          if (!lease) return yield* Effect.die(new Error("Execution directory unavailable"))
          return lease
        }),
        () => Effect.gen(function* () {
          if (!(yield* registry.assertDirectoryActive(sessionID))) return yield* Effect.die(new Error("Execution directory is retired"))
          return yield* work(snapshot)
        }),
        (lease) => Effect.sync(() => { if (outstanding === 0) lease.release() }),
      )
    }

    const revert = Effect.fn("SessionRevert.revert")((input: RevertInput) => guarded(input.sessionID, () => Effect.gen(function* () {
      yield* state.assertNotBusy(input.sessionID)
      const all = yield* sessions.messages({ sessionID: input.sessionID, agentID: "*" })
      let lastUser: MessageV2.User | undefined
      const session = yield* sessions.get(input.sessionID)

      let rev: Session.Info["revert"]
      for (const msg of all) {
        if (msg.info.role === "user") lastUser = msg.info
        const remaining = []
        for (const part of msg.parts) {
          if (rev) {
            continue
          }

          if (!rev) {
            if ((msg.info.id === input.messageID && !input.partID) || part.id === input.partID) {
              const partID = remaining.some((item) => ["text", "tool"].includes(item.type)) ? input.partID : undefined
              rev = {
                messageID: !partID && lastUser ? lastUser.id : msg.info.id,
                partID,
              }
            }
            remaining.push(part)
          }
        }
      }

      if (!rev) return session

      // Transcript position is independent of file recovery. Historical Git
      // references remain readable, but are never applied by this operation.
      yield* sessions.setRevert({ sessionID: input.sessionID, revert: rev, summary: undefined })
      return yield* sessions.get(input.sessionID)
    })))

    const unrevert = Effect.fn("SessionRevert.unrevert")((input: { sessionID: SessionID }) => guarded(input.sessionID, () => Effect.gen(function* () {
      log.info("unreverting", input)
      yield* state.assertNotBusy(input.sessionID)
      const session = yield* sessions.get(input.sessionID)
      if (!session.revert) return session
      yield* sessions.clearRevert(input.sessionID)
      return yield* sessions.get(input.sessionID)
    })))

    const cleanup = Effect.fn("SessionRevert.cleanup")(function* (session: Session.Info) {
      if (!session.revert) return
      const sessionID = session.id
      const msgs = yield* sessions.messages({ sessionID, agentID: "*" })
      const messageID = session.revert.messageID
      const remove = [] as MessageV2.WithParts[]
      let target: MessageV2.WithParts | undefined
      for (const msg of msgs) {
        if (msg.info.id < messageID) continue
        if (msg.info.id > messageID) {
          remove.push(msg)
          continue
        }
        if (session.revert.partID) {
          target = msg
          continue
        }
        remove.push(msg)
      }
      for (const msg of remove) {
        SyncEvent.run(MessageV2.Event.Removed, {
          sessionID,
          messageID: msg.info.id,
        })
      }
      if (session.revert.partID && target) {
        const partID = session.revert.partID
        const idx = target.parts.findIndex((part) => part.id === partID)
        if (idx >= 0) {
          const removeParts = target.parts.slice(idx)
          target.parts = target.parts.slice(0, idx)
          for (const part of removeParts) {
            SyncEvent.run(MessageV2.Event.PartRemoved, {
              sessionID,
              messageID: target.info.id,
              partID: part.id,
            })
          }
        }
      }
      yield* sessions.clearRevert(sessionID)
    })

    return Service.of({ revert, unrevert, cleanup })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(SessionRunState.defaultLayer),
    Layer.provide(ActorRegistry.defaultLayer),
    Layer.provide(Session.defaultLayer),
    Layer.provide(Snapshot.defaultLayer),
  ),
)

export * as SessionRevert from "./revert"
