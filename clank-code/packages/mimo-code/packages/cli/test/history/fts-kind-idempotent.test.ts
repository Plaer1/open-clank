// [fix] replaying history kind migrations must not abort on a desynced journal.
// Schema still comes from migration/*.sql; only session.run swallows replay-safe DDL.
import { describe, expect, test } from "bun:test"
import { Database } from "bun:sqlite"
import { drizzle } from "drizzle-orm/bun-sqlite"
import { migrateReplaySafe, isReplaySafeDdl } from "../../src/storage/migrate-replay-safe"

const KIND_SQL = `
ALTER TABLE history_fts ADD COLUMN kind text NOT NULL DEFAULT '';
--> statement-breakpoint
CREATE INDEX IF NOT EXISTS history_fts_kind_idx ON history_fts (kind);
--> statement-breakpoint
INSERT OR IGNORE INTO history_index_migration
SELECT 7, 'clean', 0, 0, 0;
`

const CJK_SQL = `
ALTER TABLE history_fts ADD COLUMN body_idx text NOT NULL DEFAULT '';
`

function openDb() {
  const sqlite = new Database(":memory:")
  sqlite.exec(`
    CREATE TABLE history_fts (
      part_id text PRIMARY KEY NOT NULL,
      session_id text NOT NULL,
      message_id text NOT NULL,
      project_id text NOT NULL,
      tool_name text,
      body text NOT NULL,
      time_created integer NOT NULL
    );
    CREATE TABLE history_index_migration (
      version integer PRIMARY KEY,
      phase text NOT NULL,
      cursor integer NOT NULL,
      fts_end integer NOT NULL,
      part_end integer NOT NULL
    );
    CREATE TABLE part (id text PRIMARY KEY);
  `)
  return { sqlite, db: drizzle({ client: sqlite }) }
}

function cols(sqlite: Database): Set<string> {
  return new Set(
    (sqlite.prepare(`PRAGMA table_info(history_fts)`).all() as Array<{ name: string }>).map((r) => r.name),
  )
}

describe("migrateReplaySafe", () => {
  test("swallow only replay-safe DDL", () => {
    expect(isReplaySafeDdl("ALTER TABLE t ADD COLUMN k int", new Error("duplicate column name: k"))).toBe(true)
    expect(isReplaySafeDdl("ALTER TABLE t DROP COLUMN k", new Error("no such column: k"))).toBe(true)
    expect(isReplaySafeDdl("CREATE INDEX i ON t (k)", new Error("index i already exists"))).toBe(true)
    expect(isReplaySafeDdl("CREATE TABLE t (id int)", new Error("table t already exists"))).toBe(false)
    expect(isReplaySafeDdl("SELECT 1", new Error("duplicate column name: k"))).toBe(false)
  })

  test("stuck client: kind exists, migration unjournaled → replay succeeds", () => {
    const { sqlite, db } = openDb()
    sqlite.exec(`ALTER TABLE history_fts ADD COLUMN kind text NOT NULL DEFAULT ''`)
    expect(() =>
      migrateReplaySafe(db as never, [
        { sql: KIND_SQL, timestamp: 1, name: "20261011000000_history_fts_kind" },
        { sql: CJK_SQL, timestamp: 2, name: "20261012000000_history_cjk_unigram" },
      ]),
    ).not.toThrow()
    const c = cols(sqlite)
    expect(c.has("kind")).toBe(true)
    expect(c.has("body_idx")).toBe(true)
    const n = sqlite.prepare(`SELECT count(*) AS n FROM __drizzle_migrations`).get() as { n: number }
    expect(n.n).toBe(2)
    sqlite.close()
  })

  test("fresh: DDL runs once from migration SQL", () => {
    const { sqlite, db } = openDb()
    migrateReplaySafe(db as never, [{ sql: KIND_SQL, timestamp: 1, name: "k" }])
    expect(cols(sqlite).has("kind")).toBe(true)
    // second call is a no-op (journaled)
    expect(() => migrateReplaySafe(db as never, [{ sql: KIND_SQL, timestamp: 1, name: "k" }])).not.toThrow()
    sqlite.close()
  })

  test("non-replay-safe error still aborts and rolls back", () => {
    const { sqlite, db } = openDb()
    const bad = `
      ALTER TABLE history_fts ADD COLUMN kind text NOT NULL DEFAULT '';
      --> statement-breakpoint
      CREATE TABLE history_fts (id int);
    `
    expect(() => migrateReplaySafe(db as never, [{ sql: bad, timestamp: 1, name: "bad" }])).toThrow()
    const n = sqlite.prepare(`SELECT count(*) AS n FROM __drizzle_migrations`).get() as { n: number }
    expect(n.n).toBe(0)
    sqlite.close()
  })
})
