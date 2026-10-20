import { eq, inArray } from "drizzle-orm"
import type { SQLiteBunDatabase } from "drizzle-orm/bun-sqlite"
import type { NodeSQLiteDatabase } from "drizzle-orm/node-sqlite"
import type { SQLiteTransaction } from "drizzle-orm/sqlite-core"
import { HistoryFtsTable } from "./fts.sql"
import { extract, DEFAULT_KINDS } from "./extract"
import type { MessageV2 } from "../session/message-v2"
import { SessionTable, MessageTable, PartTable } from "../session/session.sql"

// Any drizzle handle that can read parts and write the FTS index: the app
// database client, an open transaction, or the json-migration db handle.
type ImportIndexDb = SQLiteBunDatabase<any, any> | NodeSQLiteDatabase<any, any> | SQLiteTransaction<"sync", void>

/**
 * Index parts that were inserted outside the Bus history writer (JSON import,
 * json-migration) so they stay searchable even when the background index
 * migration is already done.
 *
 * Returns per-part indexing errors instead of throwing, so a single bad part
 * cannot abort the surrounding import transaction.
 */
export function indexImportedParts(db: ImportIndexDb, ids: readonly string[]): string[] {
  // Union overloads on select/insert resolve to the relational signature, not
  // the column-selection one; run the body against a single handle type.
  const handle = db as SQLiteBunDatabase<any, any>
  const errs: string[] = []
  if (ids.length === 0) return errs
  const enabled = new Set(DEFAULT_KINDS)
  for (let offset = 0; offset < ids.length; offset += 128) {
    const batch = ids.slice(offset, offset + 128)
    if (batch.length === 0) continue
    const rows = handle
      .select({
        id: PartTable.id,
        session_id: PartTable.session_id,
        message_id: PartTable.message_id,
        data: PartTable.data,
        time_created: PartTable.time_created,
        message_data: MessageTable.data,
        project_id: SessionTable.project_id,
      })
      .from(PartTable)
      .innerJoin(MessageTable, eq(MessageTable.id, PartTable.message_id))
      .innerJoin(SessionTable, eq(SessionTable.id, PartTable.session_id))
      .where(inArray(PartTable.id, batch as never))
      .all()
    for (const row of rows) {
      const role = (row.message_data as { role?: string } | undefined)?.role === "user" ? "user" : "assistant"
      const extracted = extract(
        {
          id: row.id,
          sessionID: row.session_id,
          messageID: row.message_id,
          ...(row.data as object),
        } as MessageV2.Part,
        role,
        enabled,
      )
      if (!extracted) continue
      try {
        handle
          .insert(HistoryFtsTable)
          .values({
            part_id: row.id,
            session_id: row.session_id,
            message_id: row.message_id,
            project_id: row.project_id,
            kind: extracted.kind,
            tool_name: extracted.tool_name,
            body: extracted.body,
            time_created: row.time_created,
          })
          .onConflictDoUpdate({
            target: HistoryFtsTable.part_id,
            set: {
              kind: extracted.kind,
              tool_name: extracted.tool_name,
              body: extracted.body,
              time_created: row.time_created,
            },
          })
          .run()
      } catch (e) {
        errs.push(`failed to index part ${row.id}: ${e}`)
      }
    }
  }
  return errs
}
