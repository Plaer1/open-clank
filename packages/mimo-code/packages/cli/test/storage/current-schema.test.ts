import { describe, expect, test } from "bun:test"
import path from "path"
import { is } from "drizzle-orm"
import { SQLiteTable, getTableConfig } from "drizzle-orm/sqlite-core"
import { init } from "#db"
import { Database } from "../../src/storage"
import { currentStamp } from "../../src/storage/current-schema"
import { migrateIndexBatch, MIGRATION_VERSION } from "../../src/history/migration-batch"

// The engine runs no migrations: a fresh store is created from current-schema.ts (generated from migration/*).
// Every table, column and index the engine's table definitions name must exist in that store.
function fresh() {
  const db = init(":memory:")
  Database.admit(db)
  return db
}
const columns = (db: ReturnType<typeof init>, table: string) =>
  db.all<{ name: string }>(`PRAGMA table_info('${table}')`).map((row) => row.name)
const objects = (db: ReturnType<typeof init>) =>
  new Set(db.all<{ name: string }>("SELECT name FROM sqlite_schema").map((row) => row.name))

describe("fresh store layout", () => {
  test("has every table, column and index of the engine's table definitions", async () => {
    const db = fresh()
    const names = objects(db)
    const src = path.join(import.meta.dir, "../../src")
    const missing: string[] = []
    let tables = 0
    for await (const file of new Bun.Glob("**/*.sql.ts").scan(src)) {
      for (const value of Object.values(await import(path.join(src, file)))) {
        if (!is(value, SQLiteTable)) continue
        const config = getTableConfig(value)
        tables++
        if (!names.has(config.name)) {
          missing.push(`table ${config.name}`)
          continue
        }
        const actual = columns(db, config.name)
        for (const column of config.columns)
          if (!actual.includes(column.name)) missing.push(`column ${config.name}.${column.name}`)
        for (const index of config.indexes)
          if (!names.has(index.config.name)) missing.push(`index ${index.config.name}`)
      }
    }
    expect(tables).toBeGreaterThan(20)
    expect(missing).toEqual([])
  })

  test("has what upstream's migrations added after our fork base", () => {
    const db = fresh()
    const names = objects(db)
    for (const name of [
      "inbox_lane_state",
      "inbox_session_epoch",
      "history_index_migration",
      "memory_index_migration",
      "session_prefix_snapshot",
      "history_part_ad",
      "history_fts_kind_idx",
      "inbox_state_idx",
      "inbox_created_idx",
      "inbox_idem_idx",
    ])
      expect(names.has(name), name).toBe(true)
    const expected: Record<string, string[]> = {
      actor_registry: ["result_message_id", "last_activity_time", "execution_revision", "requested_model"],
      history_fts: ["kind", "body", "body_idx", "tool_name"],
      memory_fts: ["body_idx"],
      session: ["prompt", "next_display_ordinal"],
      session_prefix_snapshot: ["tools", "provenance"],
      inbox: ["input_message_id", "state", "epoch", "run_id", "claim_frontier", "consumed", "idempotency_key"],
      user_input_receipt: ["claimed_execution_revision", "source_proof"],
    }
    for (const [table, list] of Object.entries(expected))
      for (const column of list) expect(columns(db, table), `${table}.${column}`).toContain(column)
    expect(db.all<{ created_at: number }>("SELECT created_at FROM __drizzle_migrations")[0]?.created_at).toBe(currentStamp)
    // No index rebuild is pending on a fresh store.
    expect(db.all("SELECT 1 FROM history_index_migration")).toEqual([])
    db.$client.close()
  })

  test("stays admissible during and after upstream's history index rebuild", () => {
    const db = fresh()
    Database.admit(db)
    db.run(`INSERT INTO history_index_migration VALUES (${MIGRATION_VERSION}, 'clean', 0, 0, 0)`)
    expect(migrateIndexBatch(db)).toBe(true) // triggers dropped for the rebuild
    expect(objects(db).has("history_fts_ai")).toBe(false)
    Database.admit(db)
    while (migrateIndexBatch(db)) {}
    expect(objects(db).has("history_fts_ai")).toBe(true)
    Database.admit(db)
    db.run("DROP TRIGGER history_fts_ai")
    expect(() => Database.admit(db)).toThrow()
    db.$client.close()
  })
})
