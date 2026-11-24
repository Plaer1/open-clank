import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { ulid } from "ulid"
import { Inbox } from "../../src/inbox"
import { gcInboxRows } from "../../src/inbox/inbox"
import { InboxTable } from "../../src/inbox/inbox.sql"
import { Database, eq } from "../../src/storage"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"

const it = testEffect(Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer, Bus.defaultLayer, CrossSpawnSpawner.defaultLayer))
const boot = () => Effect.scoped(Inbox.Service.use(() => Effect.void).pipe(Effect.provide(Inbox.defaultLayer)))

// inbox (g), (l), (m)
describe("Inbox startup conversion and GC", () => {
  it.live("GC retains pending inputs and uses terminal update time", provideTmpdirInstance(() => Effect.gen(function* () {
    const sessions = yield* Session.Service
    const session = yield* sessions.create()
    const now = Date.now()
    const old = now - 8 * 24 * 60 * 60 * 1000
    const states = ["accepted", "claimed", "settled", "cancelled", "rejected"] as const
    yield* Effect.sync(() => Database.use((db) => {
      for (const state of states) db.insert(InboxTable).values({
        id: state, receiver_session_id: session.id, receiver_actor_id: "main", state,
        content: null, created_at: old, time_updated: old,
      }).run()
      db.insert(InboxTable).values({
        id: "recently-settled", receiver_session_id: session.id, receiver_actor_id: "main", state: "settled",
        content: null, created_at: old, time_updated: now,
      }).run()
    }))
    yield* gcInboxRows(now - 7 * 24 * 60 * 60 * 1000)
    const remaining = () => Database.use((db) => db.select().from(InboxTable)
      .where(eq(InboxTable.receiver_session_id, session.id)).all().map((row) => row.id).sort())
    expect(remaining()).toEqual(["accepted", "claimed", "recently-settled"])
    yield* gcInboxRows(now - 7 * 24 * 60 * 60 * 1000)
    expect(remaining()).toEqual(["accepted", "claimed", "recently-settled"])
  })))

  it.live("startup does not import legacy content into the fresh queue", provideTmpdirInstance(() => Effect.gen(function* () {
    const sessions = yield* Session.Service
    const session = yield* sessions.create()
    const id = ulid()
    yield* Effect.sync(() => Database.use((db) => db.insert(InboxTable).values({
      id, receiver_session_id: session.id, receiver_actor_id: "main", type: "actor_notification",
      content: { text: "legacy text" }, created_at: Date.now(), time_updated: Date.now(),
    }).run()))
    yield* boot()
    const row = Database.use((db) => db.select().from(InboxTable).where(eq(InboxTable.id, id)).get())!
    expect(row.content).toEqual({ text: "legacy text" })
    expect(row.input_message_id).toBeNull()
    expect(yield* sessions.messages({ sessionID: session.id, agentID: "main" })).toHaveLength(0)
  })))
})
