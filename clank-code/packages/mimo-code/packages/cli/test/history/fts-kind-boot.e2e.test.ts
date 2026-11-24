// E2E: real Database.Client() boot after a desynced journal — the production
// failure shape. A child process runs the actual open path (pragma + migrate +
// ensure) and must exit 0 both before and after we drop the 20261011 journal row.
import { expect, test } from "bun:test"
import { Database } from "bun:sqlite"
import { mkdtempSync } from "node:fs"
import { join } from "node:path"
import { tmpdir } from "node:os"
import { fileURLToPath } from "node:url"

const BOOT = () => fileURLToPath(new URL("./fixtures/boot-client.ts", import.meta.url))

async function boot(file: string) {
  const child = Bun.spawn([process.execPath, BOOT()], {
    cwd: process.cwd(),
    env: { ...process.env, OPENCLANK_DB: file },
    stdout: "pipe",
    stderr: "pipe",
  })
  const [code, stdout, stderr] = await Promise.all([
    child.exited,
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
  ])
  return { code, stdout, stderr, result: code === 0 ? JSON.parse(stdout.trim().split("\n").at(-1)!) : null }
}

test("Client() boots after journal loses 20261011 while kind exists", async () => {
  const dir = mkdtempSync(join(tmpdir(), "mimo-kind-e2e-"))
  const file = join(dir, "openclank.db")

  // 1) Clean boot: full migrate + ensure.
  const first = await boot(file)
  expect(first.code, first.stderr).toBe(0)
  expect(first.result.cols).toContain("kind")
  expect(first.result.cols).toContain("body_idx")

  // 2) Desync the journal the way SKIP_MIGRATIONS / name-mismatch does:
  //    schema already has kind, but 20261011 is no longer recorded as applied.
  const db = new Database(file)
  db.run(`DELETE FROM __drizzle_migrations WHERE name = '20261011000000_history_fts_kind'`)
  const before = db.prepare(`SELECT count(*) AS n FROM __drizzle_migrations WHERE name LIKE '%history_fts_kind%'`).get() as { n: number }
  expect(before.n).toBe(0)
  db.close()

  // 3) Second boot must recover without duplicate-column abort.
  const second = await boot(file)
  expect(second.code, second.stderr).toBe(0)
  expect(second.result.cols).toContain("kind")
  expect(second.result.cols).toContain("body_idx")
  expect(second.result.journal).toContain("20261011000000_history_fts_kind")

  // 4) Third boot is a clean no-op.
  const third = await boot(file)
  expect(third.code, third.stderr).toBe(0)
}, 60_000)
