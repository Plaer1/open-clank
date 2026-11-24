import { Database } from "../../../src/storage"

// Production open path: Client() runs pragma + migrate() + ensureHistorySearchColumns.
const db = Database.Client()
const cols = db.$client
  .prepare("PRAGMA table_info(history_fts)")
  .all()
  .map((r) => String((r as { name?: string }).name ?? ""))
const journal = db.$client
  .prepare("SELECT name FROM __drizzle_migrations")
  .all()
  .map((r) => String((r as { name?: string }).name ?? ""))
console.log(JSON.stringify({ ok: true, cols, journal: journal.filter((n) => n.includes("history")) }))
Database.close()
