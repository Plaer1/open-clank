/**
 * v7/v8 index rebuild batch — pure DB path, no Log/Global side effects.
 * Re-extracts parts so `history_fts.kind` + `body_idx` are filled (CJK-unigram).
 * Scheduler (startIndexMigration) lives in `./migration`.
 *
 * Phases: clean → repair → finalize → done.
 * - clean: drop orphan / chunk-residue rows from history_fts
 * - repair: extract → upsertHistoryBody for every part (content only)
 * - finalize: FTS5 `rebuild` + restore triggers (index aligned to content in one shot)
 * Triggers are dropped for clean/repair so deletes on an empty external-content
 * FTS cannot undefined-behavior; they are recreated only after rebuild.
 */
import { and, asc, eq, gt, lte, sql } from "drizzle-orm"
import type { Database } from "../storage"
import type { PartID } from "../session/schema"
import { PartTable } from "../session/session.sql"
import { HistoryFtsTable, HistoryIndexMigrationTable as State } from "./fts.sql"
import { indexImportedParts } from "./import"
import { basePartId } from "./chunk"
import { deleteHistoryRows } from "./chunk-write"

/** v8: re-extract parts so bodies are CJK-unigram segmented (short-word FTS). */
export const MIGRATION_VERSION = 8
const batch = 32
const budgetMs = 8

const DROP_FTS_TRIGGERS = [
  "DROP TRIGGER IF EXISTS history_fts_ai",
  "DROP TRIGGER IF EXISTS history_fts_ad",
  "DROP TRIGGER IF EXISTS history_fts_au",
]

const CREATE_FTS_TRIGGERS = [
  `CREATE TRIGGER history_fts_ai AFTER INSERT ON history_fts BEGIN
    INSERT INTO history_fts_idx(rowid, body_idx) VALUES (NEW.rowid, NEW.body_idx);
  END`,
  `CREATE TRIGGER history_fts_ad AFTER DELETE ON history_fts BEGIN
    INSERT INTO history_fts_idx(history_fts_idx, rowid, body_idx) VALUES('delete', OLD.rowid, OLD.body_idx);
  END`,
  `CREATE TRIGGER history_fts_au AFTER UPDATE ON history_fts BEGIN
    INSERT INTO history_fts_idx(history_fts_idx, rowid, body_idx) VALUES('delete', OLD.rowid, OLD.body_idx);
    INSERT INTO history_fts_idx(rowid, body_idx) VALUES (NEW.rowid, NEW.body_idx);
  END`,
]

function rawExec(tx: { run: (query: ReturnType<typeof sql.raw>) => unknown }, statements: readonly string[]) {
  // 必须走 drizzle tx.run：client.exec 会在已开启的事务里再抢锁，直接死锁。
  for (const text of statements) tx.run(sql.raw(text))
}

/** One bounded, atomic batch. Other processes reread the cursor under the lock. */
export function migrateIndexBatch(db: ReturnType<typeof Database.Client>) {
  const started = performance.now()
  return db.transaction(
    (tx) => {
      const state = tx.select().from(State).where(eq(State.version, MIGRATION_VERSION)).get()
      if (!state || state.phase === "done") return false

      if (state.phase === "finalize") {
        rawExec(tx, DROP_FTS_TRIGGERS)
        // external-content FTS：从 content 全量重建，一次对齐 body_idx。
        rawExec(tx, [`INSERT INTO history_fts_idx(history_fts_idx) VALUES('rebuild')`])
        rawExec(tx, CREATE_FTS_TRIGGERS)
        tx.update(State).set({ phase: "done" }).where(eq(State.version, MIGRATION_VERSION)).run()
        return false
      }

      // clean/repair 期间不维护 FTS trigger（避免对空索引 delete）。
      rawExec(tx, DROP_FTS_TRIGGERS)

      if (state.phase === "clean") {
        const rowid = sql<number>`history_fts.rowid`
        const rows = tx
          .select({ rowid, id: HistoryFtsTable.part_id })
          .from(HistoryFtsTable)
          .where(and(gt(rowid, state.cursor), lte(rowid, state.fts_end)))
          .orderBy(asc(rowid))
          .limit(batch)
          .all()
        let cursor = state.cursor
        for (const row of rows) {
          if (cursor !== state.cursor && performance.now() - started >= budgetMs) break
          cursor = row.rowid
          const base = basePartId(row.id)
          if (base !== row.id) {
            deleteHistoryRows(tx, base)
            continue
          }
          const part = tx.select({ id: PartTable.id }).from(PartTable)
            .where(eq(PartTable.id, base as PartID)).get()
          if (!part) deleteHistoryRows(tx, base)
        }
        if (!rows.length) {
          tx.update(State).set({ phase: "repair", cursor: 0 }).where(eq(State.version, MIGRATION_VERSION)).run()
          return true
        }
        tx.update(State).set({ cursor }).where(eq(State.version, MIGRATION_VERSION)).run()
        if (cursor >= state.fts_end) {
          tx.update(State).set({ phase: "repair", cursor: 0 }).where(eq(State.version, MIGRATION_VERSION)).run()
        }
        return true
      }

      // repair
      const rowid = sql<number>`part.rowid`
      const rows = tx
        .select({ rowid, id: PartTable.id })
        .from(PartTable)
        .where(and(gt(rowid, state.cursor), lte(rowid, state.part_end)))
        .orderBy(asc(rowid))
        .limit(batch)
        .all()
      let cursor = state.cursor
      for (const row of rows) {
        if (cursor !== state.cursor && performance.now() - started >= budgetMs) break
        indexImportedParts(tx, [row.id])
        cursor = row.rowid
      }
      tx.update(State)
        .set(rows.length ? { cursor } : { phase: "finalize" })
        .where(eq(State.version, MIGRATION_VERSION))
        .run()
      // rows 空也返回 true：下一批跑 finalize（rebuild+trigger），不能在此收工。
      return true
    },
    { behavior: "immediate" },
  )
}
