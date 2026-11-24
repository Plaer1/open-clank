import { Context, Effect, Layer } from "effect"
import { asc, desc, eq, gt, and } from "drizzle-orm"
import { Database } from "../storage"
import { PartTable, SessionTable } from "../session/session.sql"
import { makeResolver } from "./resolve"
import { ManagedProvider } from "@/acp/managed-provider"
import { Log } from "../util"

const log = Log.create({ service: "history.backfill" })

// Managed History belongs to the Host. Replay canonical parts, never local FTS.
export function backfillManaged() {
  return Effect.gen(function* () {
    if (!ManagedProvider.enabled()) return { sessions: 0, failed: 0, acknowledged: 0 }
    let failed = 0
    let acknowledged = 0
    const resolver = makeResolver()
    const sessions = Database.use((db) => db.select({ id: SessionTable.id }).from(SessionTable).orderBy(desc(SessionTable.time_updated)).all())
    for (const session of sessions) {
      yield* Effect.gen(function* () {
      let cursor = ""
      while (true) {
        const parts = Database.use((db) => db.select().from(PartTable).where(and(eq(PartTable.session_id, session.id as any), gt(PartTable.id, cursor as any))).orderBy(asc(PartTable.id)).limit(500).all())
        if (!parts.length) break
        const events: import("@/acp/openclank-protocol").HistoryMutationEvent[] = []
        for (const part of parts) {
          const role = yield* resolver.role(part.message_id)
          events.push({ messageID: part.message_id, partID: part.id, role,
            partType: String((part.data as { type?: unknown }).type ?? "unknown"),
            content: { id: part.id, sessionID: part.session_id, messageID: part.message_id, ...(part.data as object) },
            timeCreated: Math.max(0, Math.floor(part.time_created)), timeUpdated: Math.max(0, Math.floor(part.time_created)), eventSequence: Math.max(0, Math.floor(part.time_created)) })
        }
        yield* Effect.tryPromise(() => ManagedProvider.historyMutate(session.id, "replay", events))
        acknowledged += events.length
        cursor = parts[parts.length - 1]!.id
        yield* Effect.sleep("10 millis")
      }
      }).pipe(Effect.catchCause(() => Effect.sync(() => { failed++; log.warn("session scan failed", { session: session.id }) })))
      yield* Effect.sleep("50 millis")
    }
    log.info("managed backfill complete", { sessions: sessions.length, failed, acknowledged })
    return { sessions: sessions.length, failed, acknowledged }
  })
}
export class Service extends Context.Service<Service, { readonly init: () => Effect.Effect<void> }>()("@opencode/History.Backfill") {}
export const layer = Layer.succeed(Service, Service.of({ init: () => backfillManaged().pipe(
  Effect.catchCause(() => Effect.sync(() => log.warn("managed backfill aborted"))),
  Effect.forkDetach, Effect.asVoid,
) }))
