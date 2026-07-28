import { Database } from "bun:sqlite"
import { drizzle } from "drizzle-orm/bun-sqlite"
import { chmodSync } from "node:fs"

export function init(path: string) {
  const sqlite = new Database(path, { create: true })
  if (path !== ":memory:") chmodSync(path, 0o600)
  const db = drizzle({ client: sqlite })
  return db
}
