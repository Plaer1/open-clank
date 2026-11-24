import { migrate } from "drizzle-orm/bun-sqlite/migrator"

/**
 * Replay-safe DDL errors: the statement is a no-op against the current schema.
 * Only these pairs are swallowed — every other failure still aborts migrate().
 */
export function isReplaySafeDdl(statement: string, error: unknown): boolean {
  const s = statement
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("--"))
    .join("\n")
    .trimStart()
  const msg = [
    error instanceof Error ? error.message : String(error),
    error instanceof Error && (error as { cause?: unknown }).cause instanceof Error
      ? ((error as { cause: Error }).cause as Error).message
      : "",
  ]
    .filter(Boolean)
    .join(" | ")
  if (/^alter\s+table/i.test(s)) {
    return /duplicate column name/i.test(msg) || /no such column/i.test(msg)
  }
  if (/^create\s+(unique\s+)?index/i.test(s)) {
    return /already exists/i.test(msg)
  }
  return false
}

type MigrateDb = Parameters<typeof migrate>[0]
type MigrateConfig = Parameters<typeof migrate>[1]

/**
 * drizzle migrate() unchanged — schema, journal, upgradeSyncIfNeeded, and
 * transaction semantics stay in the library. Only `session.run` treats
 * replay-safe DDL no-ops as success so a desynced journal cannot abort startup.
 */
export function migrateReplaySafe(db: MigrateDb, entries: MigrateConfig): void {
  const session = (db as unknown as { session: { run: (query: unknown) => unknown } }).session
  const dialect = (db as unknown as { dialect: { sqlToQuery: (query: unknown) => { sql: string } } }).dialect
  const originalRun = session.run.bind(session)
  session.run = (query: unknown) => {
    try {
      return originalRun(query)
    } catch (error) {
      const statement = dialect.sqlToQuery(query).sql
      if (isReplaySafeDdl(statement, error)) return undefined
      throw error
    }
  }
  try {
    migrate(db, entries)
  } finally {
    session.run = originalRun
  }
}
