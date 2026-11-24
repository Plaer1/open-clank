import { beforeEach, afterEach, describe, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { ActorRegistryTable } from "../../src/actor/actor.sql"
import { runTurn } from "../../src/actor/turn"
import type { SessionID } from "../../src/session/schema"
import { Database } from "../../src/storage"
import { tmpdir } from "../fixture/fixture"

const layer = Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer)

beforeEach(() => {
  Database.use((db) => db.delete(ActorRegistryTable).run())
})

afterEach(async () => {
  await Instance.disposeAll()
})

async function withActor(fn: (rt: ManagedRuntime.ManagedRuntime<Session.Service | ActorRegistry.Service, never>, sessionID: SessionID, actorID: string) => Promise<void>) {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({
    directory: tmp.path,
    fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((svc) => svc.create()))
        const actorID = "race-1"
        await rt.runPromise(
          ActorRegistry.Service.use((reg) =>
            reg.register({
              sessionID: session.id,
              actorID,
              mode: "subagent",
              agent: "general",
              description: "race",
              contextMode: "none",
              background: true,
              lifecycle: "persistent",
            }),
          ),
        )
        await fn(rt, session.id, actorID)
      } finally {
        await rt.dispose()
      }
    },
  })
}

describe("actor terminal execution CAS", () => {
  test("concurrent starts have one revision owner", async () => {
    await withActor(async (rt, sessionID, actorID) => {
      const revisions = await Promise.all([
        rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID))),
        rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID))),
      ])
      expect(revisions.filter((revision) => revision !== undefined)).toEqual([1])
      expect(revisions.filter((revision) => revision === undefined)).toHaveLength(1)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, 1, "success")))).toBe(true)
    })
  })

  test("completion and cancellation have one winner, and notification claim is at-most-once", async () => {
    await withActor(async (rt, sessionID, actorID) => {
      const revision = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID)))
      expect(revision).toBe(1)

      const [success, cancelled] = await Promise.all([
        rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, revision!, "success"))),
        rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, revision!, "cancelled"))),
      ])
      expect(Number(success) + Number(cancelled)).toBe(1)

      const winner = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.get(sessionID, actorID)))
      const outcome = winner!.lastOutcome!
      const firstClaim = await rt.runPromise(
        ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, revision!, outcome)),
      )
      const retryAfterSendFailure = await rt.runPromise(
        ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, revision!, outcome)),
      )
      expect(firstClaim).toBe(true)
      // A transport failure after the durable claim is intentionally best-effort
      // at-most-once in this bounded correction; no retry can duplicate it.
      expect(retryAfterSendFailure).toBe(false)
    })
  })

  test("a late cancellation cannot overwrite success, while a later execution gets a new claim", async () => {
    await withActor(async (rt, sessionID, actorID) => {
      const first = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID)))
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, first!, "success")))).toBe(true)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, first!, "cancelled")))).toBe(false)
      expect((await rt.runPromise(ActorRegistry.Service.use((reg) => reg.get(sessionID, actorID))))!.lastOutcome).toBe("success")

      const second = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID)))
      expect(second).toBe(2)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, second!, "success")))).toBe(true)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, second!, "success")))).toBe(true)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, second!, "success")))).toBe(false)
    })
  })

  test("cancellation wins over a later failure exactly once", async () => {
    await withActor(async (rt, sessionID, actorID) => {
      const revision = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID)))
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, revision!, "cancelled")))).toBe(true)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, revision!, "failure", "late failure")))).toBe(false)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, revision!, "cancelled")))).toBe(true)
      expect((await rt.runPromise(ActorRegistry.Service.use((reg) => reg.get(sessionID, actorID))))!.lastOutcome).toBe("cancelled")
    })
  })

  test("turn re-entry keeps the top-level execution running until one final settle", async () => {
    await withActor(async (rt, sessionID, actorID) => {
      const revision = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.beginExecution(sessionID, actorID)))
      await rt.runPromise(runTurn(sessionID, actorID, Effect.succeed("pre"), { executionRevision: revision! }))
      await rt.runPromise(runTurn(sessionID, actorID, Effect.succeed("gate"), { executionRevision: revision! }))
      const during = await rt.runPromise(ActorRegistry.Service.use((reg) => reg.get(sessionID, actorID)))
      expect(during?.status).toBe("running")
      expect(during?.executionRevision).toBe(revision)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.settleExecution(sessionID, actorID, revision!, "success")))).toBe(true)
      expect(await rt.runPromise(ActorRegistry.Service.use((reg) => reg.claimTerminalNotification(sessionID, actorID, revision!, "success")))).toBe(true)
    })
  })
})
