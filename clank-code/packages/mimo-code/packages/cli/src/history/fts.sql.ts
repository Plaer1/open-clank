import { sqliteTable, text, integer, index } from "drizzle-orm/sqlite-core"

export const HistoryFtsTable = sqliteTable(
  "history_fts",
  {
    part_id: text().primaryKey(),
    session_id: text().notNull(),
    message_id: text().notNull(),
    project_id: text().notNull(),
    /** Content classification (`extract` Kind). Consumers may filter; History.search does not accept kind. */
    kind: text().notNull(),
    /** Original preview text — display / highlight source (lossless). */
    body: text().notNull(),
    /** Search representation: Han isolated for unicode61 phrases (D5/D7). */
    body_idx: text().notNull(),
    tool_name: text(),
    time_created: integer().notNull(),
  },
  (t) => [
    index("history_fts_session_idx").on(t.session_id, t.time_created),
    index("history_fts_project_idx").on(t.project_id, t.time_created),
    index("history_fts_message_idx").on(t.message_id),
    index("history_fts_kind_idx").on(t.kind),
  ],
)

// Versioned content migration. Cursor and index writes commit together.
export const HistoryIndexMigrationTable = sqliteTable("history_index_migration", {
  version: integer().primaryKey(),
  phase: text({ enum: ["clean", "repair", "finalize", "done"] }).notNull(),
  cursor: integer().notNull(),
  fts_end: integer().notNull(),
  part_end: integer().notNull(),
})
