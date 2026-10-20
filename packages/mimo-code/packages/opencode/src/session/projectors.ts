import { NotFoundError, eq, and } from "../storage"
import { SyncEvent } from "@/sync"
import * as Session from "./session"
import { MessageV2 } from "./message-v2"
import { SessionTable, MessageTable, PartTable } from "./session.sql"
import { Log } from "../util"

const log = Log.create({ service: "session.projector" })

function foreign(err: unknown) {
  if (typeof err !== "object" || err === null) return false
  if ("code" in err && err.code === "SQLITE_CONSTRAINT_FOREIGNKEY") return true
  return "message" in err && typeof err.message === "string" && err.message.includes("FOREIGN KEY constraint failed")
}

export type DeepPartial<T> = T extends object ? { [K in keyof T]?: DeepPartial<T[K]> | null } : T

function grab<T extends object, K1 extends keyof T, X>(
  obj: T,
  field1: K1,
  cb?: (val: NonNullable<T[K1]>) => X,
): X | undefined {
  if (obj == undefined || !(field1 in obj)) return undefined

  const val = obj[field1]
  if (val && typeof val === "object" && cb) {
    return cb(val)
  }
  if (val === undefined) {
    throw new Error(
      "Session update failure: pass `null` to clear a field instead of `undefined`: " + JSON.stringify(obj),
    )
  }
  return val as X | undefined
}

export function toPartialRow(info: DeepPartial<Session.Info>) {
  const obj = {
    id: grab(info, "id"),
    project_id: grab(info, "projectID"),
    workspace_id: grab(info, "workspaceID"),
    parent_id: grab(info, "parentID"),
    context_from: grab(info, "contextFrom"),
    context_watermark: grab(info, "contextWatermark"),
    slug: grab(info, "slug"),
    directory: grab(info, "directory"),
    title: grab(info, "title"),
    title_source: grab(info, "titleSource"),
    title_revision: grab(info, "titleRevision"),
    version: grab(info, "version"),
    share_url: grab(info, "share", (v) => grab(v, "url")),
    summary_additions: grab(info, "summary", (v) => grab(v, "additions")),
    summary_deletions: grab(info, "summary", (v) => grab(v, "deletions")),
    summary_files: grab(info, "summary", (v) => grab(v, "files")),
    summary_diffs: grab(info, "summary", (v) => grab(v, "diffs")),
    revert: grab(info, "revert"),
    permission: grab(info, "permission"),
    time_created: grab(info, "time", (v) => grab(v, "created")),
    time_updated: grab(info, "time", (v) => grab(v, "updated")),
    time_compacting: grab(info, "time", (v) => grab(v, "compacting")),
    time_archived: grab(info, "time", (v) => grab(v, "archived")),
  }

  return Object.fromEntries(Object.entries(obj).filter(([_, val]) => val !== undefined))
}

export default [
  SyncEvent.project(Session.Event.Created, (db, data) => {
    db.insert(SessionTable).values(Session.toRow(data.info)).run()
  }),

  SyncEvent.project(Session.LegacyUpdated, (db, data) => {
    const current = db.select().from(SessionTable).where(eq(SessionTable.id, data.sessionID)).get()
    if (!current) throw new NotFoundError({ message: `Session not found: ${data.sessionID}` })
    const { title, ...rest } = data.info
    // Schema migration initializes old data at revision zero. Rebuilding the
    // legacy prefix must produce that same baseline before any v2 transition.
    // Once a revisioned title command exists, no v1 title can follow it.
    if (title !== undefined && current.title_revision !== 0) throw new Error("Legacy title event after revisioned title command")
    db.update(SessionTable).set({
      ...toPartialRow(rest),
      ...(title !== undefined ? { title: title || "Untitled", title_source: "user" as const, title_revision: 0 } : {}),
    }).where(eq(SessionTable.id, data.sessionID)).run()
  }),

  SyncEvent.project(Session.Event.Updated, (db, data) => {
    const info = data.info
    if ("title" in info || "titleSource" in info || "titleRevision" in info) {
      const current = db.select().from(SessionTable).where(eq(SessionTable.id, data.sessionID)).get()
      if (!current) throw new NotFoundError({ message: `Session not found: ${data.sessionID}` })
      const next = Session.TitleSnapshot.parse({ sessionID: data.sessionID, ...info })
      if (data.previousRevision !== current.title_revision || next.titleRevision !== current.title_revision + 1)
        throw new Error("Title event revision mismatch")
      if (current.title_source === "user" && next.titleSource !== "user") throw new Error("Protected title cannot be unlocked")
      if (next.titleSource === "fallback" && current.title_source !== "fallback") throw new Error("Generated title cannot return to fallback")
    } else if (data.previousRevision !== undefined) throw new Error("Title event is missing its snapshot")
    const row = db
      .update(SessionTable)
      .set(toPartialRow(info))
      .where(eq(SessionTable.id, data.sessionID))
      .returning()
      .get()
    if (!row) throw new NotFoundError({ message: `Session not found: ${data.sessionID}` })
  }),

  SyncEvent.project(Session.Event.Deleted, (db, data) => {
    db.delete(SessionTable).where(eq(SessionTable.id, data.sessionID)).run()
  }),

  SyncEvent.project(MessageV2.Event.Updated, (db, data) => {
    const time_created = data.info.time.created
    const { id, sessionID, agentID, ...rest } = data.info

    try {
      db.insert(MessageTable)
        .values({
          id,
          session_id: sessionID,
          agent_id: agentID ?? "main",
          time_created,
          data: rest,
        })
        .onConflictDoUpdate({ target: MessageTable.id, set: { agent_id: agentID ?? "main", data: rest } })
        .run()
    } catch (err) {
      if (!foreign(err)) throw err
      log.warn("ignored late message update", { messageID: id, sessionID })
    }
  }),

  SyncEvent.project(MessageV2.Event.Removed, (db, data) => {
    db.delete(MessageTable)
      .where(and(eq(MessageTable.id, data.messageID), eq(MessageTable.session_id, data.sessionID)))
      .run()
  }),

  SyncEvent.project(MessageV2.Event.PartRemoved, (db, data) => {
    db.delete(PartTable)
      .where(and(eq(PartTable.id, data.partID), eq(PartTable.session_id, data.sessionID)))
      .run()
  }),

  SyncEvent.project(MessageV2.Event.PartUpdated, (db, data) => {
    const { id, messageID, sessionID, ...rest } = data.part

    try {
      db.insert(PartTable)
        .values({
          id,
          message_id: messageID,
          session_id: sessionID,
          time_created: data.time,
          data: rest,
        })
        .onConflictDoUpdate({ target: PartTable.id, set: { data: rest } })
        .run()
    } catch (err) {
      if (!foreign(err)) throw err
      log.warn("ignored late part update", { partID: id, messageID, sessionID })
    }
  }),
]
