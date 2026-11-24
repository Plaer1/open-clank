import { eq, inArray, sql } from "drizzle-orm"
import type { BaseSQLiteDatabase } from "drizzle-orm/sqlite-core"
import type { MessageV2 } from "../session/message-v2"
import { MessageTable, PartTable, SessionTable } from "../session/session.sql"
import type { PartID } from "../session/schema"
import { extract } from "./extract"
import { projection } from "./projection"
import { deleteHistoryRows, upsertHistoryBody } from "./chunk-write"

// Importers bypass the Bus writer. Call inside the transaction that writes parts.
// Read persisted rows so conflict/no-op imports cannot index a rejected value.
export function indexImportedParts<T>(
  db: Pick<BaseSQLiteDatabase<"sync", T>, "select" | "insert" | "delete">,
  ids: readonly string[],
): string[] {
  // Open Clank: a part that cannot be indexed is reported, not thrown, so one bad part
  // cannot abort the surrounding import transaction.
  const errs: string[] = []
  for (let offset = 0; offset < ids.length; offset += 128) {
    const batch = ids.slice(offset, offset + 128) as PartID[]
    if (batch.length === 0) continue
    const rows = db
      .select({
        ...projection(),
        project: SessionTable.project_id,
        role: sql<string>`json_extract(${MessageTable.data}, '$.role')`,
        summary: sql<number>`json_extract(${MessageTable.data}, '$.summary')`,
      })
      .from(PartTable)
      .innerJoin(MessageTable, eq(MessageTable.id, PartTable.message_id))
      .innerJoin(SessionTable, eq(SessionTable.id, PartTable.session_id))
      .where(inArray(PartTable.id, batch))
      .all()
    for (const row of rows) try {
      const value = extract(
        {
          ...row.data,
          id: row.id,
          messageID: row.message_id,
          sessionID: row.session_id,
        } as MessageV2.Part,
        row.role === "user" ? "user" : "assistant",
        { summary: Boolean(row.summary) },
      )
      if (!value) {
        deleteHistoryRows(db, row.id)
        continue
      }
      // Truncation is applied inside upsertHistoryBody (tool-result preview path).
      upsertHistoryBody(db, {
        part_id: row.id,
        session_id: row.session_id,
        message_id: row.message_id,
        project_id: row.project,
        kind: value.kind,
        tool_name: value.tool_name,
        body: value.body,
        body_idx: value.body_idx,
        time_created: row.time_created,
      })
    } catch (error) {
      errs.push(`failed to index part ${row.id}: ${error}`)
    }
  }
  return errs
}
