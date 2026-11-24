import { afterEach, beforeEach, describe, expect, spyOn, test } from "bun:test"
import { Effect, Layer } from "effect"
import { eq } from "drizzle-orm"
import { Database } from "../../src/storage"
import { HistoryFtsTable, HistoryIndexMigrationTable } from "../../src/history/fts.sql"
import { MessageTable, PartTable, SessionTable } from "../../src/session/session.sql"
import { ProjectTable } from "../../src/project/project.sql"
import { backfillAll } from "./fixtures/seed-index"
import { backfillManaged } from "../../src/history/backfill"
import { migrateIndexBatch, startIndexMigration, stopIndexMigration, MIGRATION_VERSION } from "../../src/history/migration"
import { fileURLToPath } from "node:url"
import { History } from "../../src/history"
import { Instance } from "../../src/project/instance"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { installManagedSessionBinding } from "../../src/memory/session-scope"

// The test process shares a single in-memory SQLite DB (test/preload sets
// OPENCLANK_DB=:memory:), so other suites' SessionTable/PartTable rows are visible
// here. backfillAll() walks ALL sessions in the DB and would index those rows,
// so wipe the relevant tables both before AND after each test.
const wipe = () =>
  Database.use((db) => {
    db.delete(HistoryFtsTable).run()
    db.delete(PartTable).run()
    db.delete(MessageTable).run()
    db.delete(SessionTable).run()
    db.delete(ProjectTable).run()
  })

beforeEach(() => {
  Database.close()
  wipe()
  stopIndexMigration(Database.Client())
})

afterEach(async () => {
  wipe()
  await Instance.disposeAll()
  ManagedProvider.resetForTest()
  delete process.env.OPEN_CLANK_MANAGED
})

const it = testEffect(Layer.mergeAll(History.defaultLayer, CrossSpawnSpawner.defaultLayer))

function seed(
  parts: Array<{
    session_id: string
    message_id: string
    part_id: string
    role: "user" | "assistant"
    type: string
    text?: string
    tool?: string
    state?: any
    filename?: string
    mime?: string
    url?: string
  }>,
) {
  const now = Date.now()
  const seenProjects = new Set<string>()
  const seenSessions = new Set<string>()
  const seenMessages = new Set<string>()
  Database.use((db) => {
    for (const p of parts) {
      const projectID = "proj_" + p.session_id
      if (!seenProjects.has(projectID)) {
        db.insert(ProjectTable)
          .values({
            id: projectID as any,
            worktree: "/tmp",
            sandboxes: [] as any,
            time_created: now,
            time_updated: now,
          } as any)
          .onConflictDoNothing()
          .run()
        seenProjects.add(projectID)
      }
      if (!seenSessions.has(p.session_id)) {
        db.insert(SessionTable)
          .values({
            id: p.session_id as any,
            project_id: projectID as any,
            slug: "x",
            directory: "/tmp",
            title: "t",
            version: "1",
            time_created: now,
            time_updated: now,
          })
          .onConflictDoNothing()
          .run()
        seenSessions.add(p.session_id)
      }
      if (!seenMessages.has(p.message_id)) {
        db.insert(MessageTable)
          .values({
            id: p.message_id as any,
            session_id: p.session_id as any,
            agent_id: "main",
            data: { role: p.role } as any,
            time_created: now,
            time_updated: now,
          })
          .run()
        seenMessages.add(p.message_id)
      }
      const data: any = { type: p.type }
      if (p.text !== undefined) data.text = p.text
      if (p.tool) data.tool = p.tool
      if (p.state) data.state = p.state
      if (p.filename) data.filename = p.filename
      if (p.mime) data.mime = p.mime
      if (p.url) data.url = p.url
      db.insert(PartTable)
        .values({
          id: p.part_id as any,
          message_id: p.message_id as any,
          session_id: p.session_id as any,
          data,
          time_created: now,
          time_updated: now,
        })
        .run()
    }
  })
}

describe("History.backfill", () => {
  it.live("managed backfill replays canonical parts to the host without populating native FTS", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        process.env.OPEN_CLANK_MANAGED = "1"
        installManagedSessionBinding("ses_managed", {
          owner: "alice",
          stableChatID: "chat-managed",
          engineSessionID: "ses_managed",
          engineAliases: [],
          memoryWorkspaceID: "global",
          authorityWorkspaceID: "authority",
          copalWorkspace: "copal",
          physicalCwd: "/work",
          bindingRevision: 1,
          mapRevision: 1,
          mappingRevision: 1,
          memoryEnabled: true,
          transition: null,
        })
        const calls: Array<{ operation: unknown; events: unknown }> = []
        ManagedProvider.installHostConnection({
          async extMethod(method: string, params: Record<string, unknown>) {
            expect(method).toBe("_openclank/history/v1/mutate")
            calls.push({ operation: params.operation, events: params.events })
            return { ok: true, operation: params.operation, accepted: 1, duplicate: 0, enqueued: 1 }
          },
        } as any)
        seed([{ session_id: "ses_managed", message_id: "m", part_id: "p", role: "user", type: "file", filename: "capture.png", mime: "image/png", url: "https://example.test/capture.png" }])
        yield* backfillAll()
        expect(calls.map((call) => call.operation)).toEqual(["replay"])
        expect((calls[0]!.events as Array<{ content: { type: string; filename: string; mime: string; url: string } }>)[0]!.content).toMatchObject({ type: "file", filename: "capture.png", mime: "image/png", url: "https://example.test/capture.png" })
        expect(Database.use((db) => db.select().from(HistoryFtsTable).all())).toEqual([])
      }),
    ),
  )

  it.live("managed replay continues after one session fails and reports coverage", () =>
    provideTmpdirInstance(() => Effect.gen(function* () {
      process.env.OPEN_CLANK_MANAGED = "1"
      for (const id of ["ses_first", "ses_second"]) installManagedSessionBinding(id, {
        owner: "alice", stableChatID: `chat-${id}`, engineSessionID: id, engineAliases: [],
        memoryWorkspaceID: "global", authorityWorkspaceID: "authority", copalWorkspace: "copal",
        physicalCwd: "/work", bindingRevision: 1, mapRevision: 1, mappingRevision: 1,
        memoryEnabled: true, transition: null,
      })
      const calls: string[] = []
      ManagedProvider.installHostConnection({ async extMethod(_method: string, params: Record<string, unknown>) {
        calls.push(String(params.sessionID))
        if (calls.length === 1) throw new Error("synthetic host failure")
        return { ok: true, operation: "replay", accepted: 1, duplicate: 0, enqueued: 1 }
      } } as any)
      seed([
        { session_id: "ses_first", message_id: "m1", part_id: "p1", role: "user", type: "text", text: "first" },
        { session_id: "ses_second", message_id: "m2", part_id: "p2", role: "user", type: "text", text: "second" },
      ])
      const coverage = yield* backfillManaged()
      expect(calls).toHaveLength(2)
      expect(new Set(calls).size).toBe(2)
      expect(coverage).toEqual({ sessions: 2, failed: 1, acknowledged: 1 })
      expect(Database.use((db) => db.select().from(HistoryFtsTable).all())).toEqual([])
    })),
  )

  it.live("indexes existing text and tool parts", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        seed([
          { session_id: "ses_1", message_id: "m1", part_id: "p1", role: "user", type: "text", text: "hello" },
          {
            session_id: "ses_1",
            message_id: "m2",
            part_id: "p2",
            role: "assistant",
            type: "tool",
            tool: "Bash",
            state: { status: "completed", input: { command: "ls" } },
          },
          {
            session_id: "ses_1",
            message_id: "m3",
            part_id: "p3",
            role: "assistant",
            type: "step-start",
          },
        ])

        yield* backfillAll()

        const rows = Database.use((db) => db.select().from(HistoryFtsTable).all())
        expect(rows.map((r) => r.part_id).sort()).toEqual(["p1", "p2"])
      }),
    ),
  )

  // [TP-SE-R5-01] 重复扫描幂等：已索引 part 不重复写。
  it.live("is idempotent (NOT EXISTS skips already-indexed parts)", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        seed([{ session_id: "ses_x", message_id: "m1", part_id: "p1", role: "user", type: "text", text: "first" }])
        yield* backfillAll()
        seed([{ session_id: "ses_x", message_id: "m2", part_id: "p2", role: "user", type: "text", text: "second" }])
        yield* backfillAll()

        const rows = Database.use((db) => db.select().from(HistoryFtsTable).all())
        expect(rows.map((r) => r.part_id).sort()).toEqual(["p1", "p2"])
      }),
    ),
  )
})

async function prepareMigration() {
  const db = Database.Client()
  db.$client.exec("DROP TABLE history_index_migration; DROP TRIGGER history_part_ad")
  db.$client.exec(
    await Bun.file(
      new URL("../../migration/20260914010000_history_index_version/migration.sql", import.meta.url),
    ).text(),
  )
  db.$client.exec(
    await Bun.file(new URL("../../migration/20260914020000_history_all_content/migration.sql", import.meta.url)).text(),
  )
  db.$client.exec(
    await Bun.file(
      new URL("../../migration/20260914040000_history_part_content/migration.sql", import.meta.url),
    ).text(),
  )
  db.$client.exec(
    await Bun.file(
      new URL("../../migration/20260915010000_history_chunk_bodies/migration.sql", import.meta.url),
    ).text(),
  )
  db.$client.exec(
    await Bun.file(new URL("../../migration/20260916000000_history_single_row_index/migration.sql", import.meta.url)).text(),
  )
  // v7 kind column is already applied by Database.Client migrations; only seed rebuild state.
  db.$client.exec(
    `INSERT INTO history_index_migration
     SELECT 8, 'clean', 0,
       COALESCE((SELECT MAX(rowid) FROM history_fts), 0),
       COALESCE((SELECT MAX(rowid) FROM part), 0);
     UPDATE history_index_migration SET phase='done' WHERE version < 8;`,
  )
  return db
}
function finish(db: ReturnType<typeof Database.Client>) {
  let count = 0
  while (migrateIndexBatch(db)) {
    if (++count > 100) throw new Error("migration failed to terminate")
  }
}
function textParts(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    session_id: "ses_compat",
    message_id: "msg_compat",
    part_id: `part_${i}`,
    role: "user" as const,
    type: "text",
    text: `searchable${i}`,
  }))
}

test.each(["clean", "repair"] as const)("%s yields at its time budget and resumes without losing original content", async (phase) => {
  const parts = textParts(6)
  parts[0]!.text = "sourceword\n" + ("x".repeat(100) + "\n").repeat(1000)
  seed(parts)
  const db = Database.Client()
  for (const part of parts) {
    for (let index = 0; index < 2; index++) {
      db.insert(HistoryFtsTable).values({
        part_id: `${part.part_id}#${index}`, session_id: part.session_id,
        message_id: part.message_id, project_id: "proj_ses_compat",
      kind: "assistant_text", body: "stalechunk", body_idx: "stalechunk", time_created: 1,
      }).run()
    }
  }
  await prepareMigration()
  db.update(HistoryIndexMigrationTable).set({ phase, cursor: 0 })
    .where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).run()
  let clock = 0
  const now = spyOn(performance, "now").mockImplementation(() => clock += 10)
  try {
    expect(migrateIndexBatch(db)).toBe(true)
  } finally {
    now.mockRestore()
  }
  const state = db.select().from(HistoryIndexMigrationTable)
    .where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()!
  expect(state.phase).toBe(phase)
  expect(state.cursor).toBe(1)
  finish(db)
  const rows = db.select().from(HistoryFtsTable).all()
  expect(rows).toHaveLength(parts.length)
  expect(rows.map((row) => row.part_id).sort()).toEqual(parts.map((part) => part.part_id).sort())
  expect(rows.find((row) => row.part_id === "part_0")!.body).toContain("sourceword")
  expect(rows.every((row) => !row.body.includes("stalechunk"))).toBe(true)
  expect(Buffer.byteLength(rows.find((row) => row.part_id === "part_0")!.body)).toBeLessThanOrEqual(51_600)
  expect(db.$client.prepare("SELECT data FROM part WHERE id=?").get("part_0"))
    .toEqual({ data: JSON.stringify({ type: "text", text: parts[0]!.text }) })
})

test("background migration delays startup, rests after expensive work and cancels on close", () => {
  Database.close()
  const scheduled: { run: () => void; delay: number }[] = []
  const timeout = spyOn(globalThis, "setTimeout").mockImplementation(((run: () => void, delay: number) => {
    scheduled.push({ run, delay })
    return { unref() { return this } }
  }) as typeof setTimeout)
  let clock = 0
  const now = spyOn(performance, "now").mockImplementation(() => clock += 10)
  try {
    const db = Database.Client()
    seed(textParts(6))
    db.update(HistoryIndexMigrationTable).set({ phase: "repair", cursor: 0, part_end: 6 })
      .where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).run()
    expect(scheduled).toHaveLength(1)
    expect(scheduled[0]!.delay).toBeGreaterThanOrEqual(1000)
    scheduled[0]!.run()
    expect(scheduled).toHaveLength(2)
    // 30ms measured work -> at least 570ms rest; a fixed 10ms retry fails this.
    expect(scheduled[1]!.delay).toBeGreaterThanOrEqual(570)
    expect(db.select().from(HistoryIndexMigrationTable)
      .where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()!.cursor).toBe(1)
    Database.close()
    expect(() => scheduled[1]!.run()).not.toThrow()
    expect(scheduled).toHaveLength(2)
  } finally {
    now.mockRestore()
    timeout.mockRestore()
  }
})

// R6/N1: 失败退避后**慢速续试**，不永久清空 job（否则「索引整理中」卡到下次进程启动）。
test("index migration keeps slow-retrying after five failed attempts", () => {
  process.env.OPENCLANK_SKIP_MIGRATIONS = "1"
  Database.close()
  const scheduled: { run: () => void; delay: number }[] = []
  const timeout = spyOn(globalThis, "setTimeout").mockImplementation(((run: () => void, delay: number) => {
    scheduled.push({ run, delay })
    return { unref() { return this } }
  }) as typeof setTimeout)
  try {
    const db = Database.Client()
    stopIndexMigration(db)
    scheduled.length = 0
    seed(textParts(3))
    db.update(HistoryIndexMigrationTable).set({ phase: "repair", cursor: 0, part_end: 3 })
      .where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).run()
    let attempts = 0
    const boom = () => {
      attempts += 1
      throw new Error("test failure")
    }
    startIndexMigration(db, { migrate: boom })
    expect(scheduled).toHaveLength(1)
    // attempt 0..5：快速退避；attempt>=5 转慢速重试，job 不丢
    for (let i = 0; i < 6; i++) {
      const next = scheduled[scheduled.length - 1]
      expect(next).toBeDefined()
      next!.run()
    }
    expect(attempts).toBe(6)
    const afterQuick = scheduled.length
    expect(afterQuick).toBe(7) // 1 start + 5 quick retries + 1 slow retry
    expect(scheduled[scheduled.length - 1]!.delay).toBeGreaterThanOrEqual(120_000)
    // 慢速重试仍会继续调度
    scheduled[scheduled.length - 1]!.run()
    expect(attempts).toBe(7)
    expect(scheduled.length).toBe(afterQuick + 1)
  } finally {
    delete process.env.OPENCLANK_SKIP_MIGRATIONS
    timeout.mockRestore()
  }
})

// A partially populated index still needs recovery.
test("repairs missing rows and replaces stale content from original parts", async () => {
  seed(textParts(300))
  const db = Database.Client()
  db.insert(HistoryFtsTable)
    .values({
      part_id: "part_0",
      session_id: "ses_compat",
      message_id: "msg_compat",
      project_id: "proj_ses_compat",
      kind: "assistant_text",

      body: "keepword data:image/png;base64,aGVsbG8= tailword", body_idx: "keepword data:image/png;base64,aGVsbG8= tailword", time_created: 1,
    })
    .run()
  await prepareMigration()
  finish(db)
  const rows = db.select().from(HistoryFtsTable).all()
  expect(rows).toHaveLength(300)
  expect(rows.find((r) => r.part_id === "part_0")?.body).not.toContain("aGVsbG8=")
  expect(
    db.$client.prepare("SELECT count(*) AS n FROM history_fts_idx WHERE history_fts_idx MATCH 'searchable0'").get(),
  ).toEqual({ n: 1 })
  expect(
    db.$client.prepare("SELECT count(*) AS n FROM history_fts_idx WHERE history_fts_idx MATCH 'searchable299'").get(),
  ).toEqual({ n: 1 })
  expect(
    db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()?.phase,
  ).toBe("done")
  expect(migrateIndexBatch(db)).toBe(false)
})

test("failed batch rolls back index writes and cursor; resumes without revisiting prior batch", async () => {
  seed(textParts(300))
  const db = await prepareMigration()
  migrateIndexBatch(db) // clean -> repair
  migrateIndexBatch(db) // commit the first time-bounded batch
  const before = db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()
  db.$client.exec(
    `CREATE TRIGGER fail_history BEFORE INSERT ON history_fts WHEN NEW.part_id = 'part_${before!.cursor + 1}' BEGIN SELECT RAISE(ABORT, 'test failure'); END`,
  )
  const now = spyOn(performance, "now").mockReturnValue(0)
  try {
    expect(() => migrateIndexBatch(db)).toThrow("test failure")
  } finally {
    now.mockRestore()
  }
  expect(db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()).toEqual(
    before,
  )
  expect(db.select().from(HistoryFtsTable).all()).toHaveLength(before!.cursor)
  db.$client.exec("DROP TRIGGER fail_history")
  // If the committed prefix were visited again, corrupt JSON would fail extraction.
  db.$client.exec("DELETE FROM history_fts WHERE part_id = 'part_0'")
  db.$client.exec("UPDATE part SET data = 'invalid json' WHERE id = 'part_0'")
  finish(db)
  expect(db.select().from(HistoryFtsTable).all()).toHaveLength(299)
})

for (const count of [0, 300]) {
  test(`durable progress across real processes, ${count} parts`, async () => {
    seed(textParts(count))
    const db = await prepareMigration()
    const { tmpdir } = await import("../fixture/fixture")
    await using dir = await tmpdir()
    const file = `${dir.path}/index with spaces.db`
    db.$client.prepare("VACUUM INTO ?").run(file)
    const run = async (limit = Infinity) => {
      const child = Bun.spawn(
        [process.execPath, fileURLToPath(new URL("./fixtures/backfill-restart.ts", import.meta.url))],
        {
          cwd: process.cwd(),
          env: { ...process.env, OPENCLANK_DB: file, HISTORY_BATCH_LIMIT: String(limit) },
          stdout: "pipe",
          stderr: "pipe",
        },
      )
      const [code, stdout, stderr] = await Promise.all([
        child.exited,
        new Response(child.stdout).text(),
        new Response(child.stderr).text(),
      ])
      expect(code, stderr).toBe(0)
      return JSON.parse(stdout.trim().split("\n").at(-1)!)
    }
    const first = await run(2)
    const second = await run()
    expect(second.before).toEqual(first.after)
    expect(second.after.phase).toBe("done")
    expect((await run()).batches).toBe(0)
    if (count) {
      expect(first.after.cursor).toBeGreaterThan(0)
      expect(first.after.cursor).toBeLessThanOrEqual(32)
    }
  })
}

// Opening directories must not restart a completed migration.
it.live("database startup finishes once and directory initialization does not reopen migration", () =>
  provideTmpdirInstance(() =>
    Effect.gen(function* () {
      Database.close()
      const db = Database.Client()
      for (let i = 0; i < 20; i++) startIndexMigration(db)
      yield* Effect.sleep("1500 millis")
      expect(
        db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()?.phase,
      ).toBe("done")
      // Deliberately bypass all normal writers to detect an unwanted historical scan.
      seed(textParts(1))
      for (let i = 0; i < 3; i++) {
        yield* provideTmpdirInstance(() =>
          Effect.gen(function* () {
            const history = yield* History.Service
            yield* history.search({ query: "searchable0", scope: "global" })
          }),
        )
        startIndexMigration(db)
      }
      yield* Effect.sleep("30 millis")
      expect(db.select().from(HistoryFtsTable).all()).toHaveLength(0)
      expect(
        db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()?.phase,
      ).toBe("done")
    }),
  ),
)

test("upgrade removes search entries whose original parts were deleted", async () => {
  seed(textParts(1))
  const db = Database.Client()
  db.insert(HistoryFtsTable)
    .values({
      part_id: "removed-part",
      session_id: "ses_compat",
      message_id: "msg_compat",
      project_id: "proj_ses_compat",
      kind: "assistant_text",

      body: "ghostneedle", body_idx: "ghostneedle", time_created: 1,
    })
    .run()
  await prepareMigration()
  finish(db)
  expect(
    db.$client.prepare("SELECT count(*) AS n FROM history_fts_idx WHERE history_fts_idx MATCH 'ghostneedle'").get(),
  ).toEqual({ n: 0 })
  expect(
    db.$client.prepare("SELECT count(*) AS n FROM history_fts_idx WHERE history_fts_idx MATCH 'searchable0'").get(),
  ).toEqual({ n: 1 })
})

test("missing migration metadata does not prevent use of the database", async () => {
  const { init } = await import("../../src/storage/db.bun")
  const db = init(":memory:")
  try {
    expect(() => startIndexMigration(db)).not.toThrow()
    expect(db.$client.prepare("SELECT 1 AS available").get()).toEqual({ available: 1 })
  } finally {
    stopIndexMigration(db)
    db.$client.close()
  }
})

test("completed version 3 indexes gain omitted content in version 4 only once", async () => {
  seed([
    {
      session_id: "ses_expand",
      message_id: "msg_expand",
      part_id: "prt_tool",
      role: "assistant",
      type: "tool",
      tool: "read",
      state: { status: "completed", input: { path: "inputneedle" }, output: "outputneedle" },
    },
    {
      session_id: "ses_expand",
      message_id: "msg_expand",
      part_id: "prt_reason",
      role: "assistant",
      type: "reasoning",
      text: "reasonneedle",
    },
  ])
  const db = Database.Client()
  db.insert(HistoryFtsTable)
    .values({
      part_id: "prt_tool",
      session_id: "ses_expand",
      message_id: "msg_expand",
      project_id: "proj_ses_expand",
      kind: "assistant_text",

      body: "read inputneedle", body_idx: "read inputneedle", time_created: 1,
    })
    .run()
  db.$client.exec(
    "UPDATE history_index_migration SET phase='done' WHERE version=3; DELETE FROM history_index_migration WHERE version=4 OR version=5 OR version=6 OR version=7 OR version=8",
  )
  db.$client.exec(
    await Bun.file(
      new URL("../../migration/20260914040000_history_part_content/migration.sql", import.meta.url),
    ).text(),
  )
  db.$client.exec(
    await Bun.file(
      new URL("../../migration/20260915010000_history_chunk_bodies/migration.sql", import.meta.url),
    ).text(),
  )
  db.$client.exec(
    await Bun.file(new URL("../../migration/20260916000000_history_single_row_index/migration.sql", import.meta.url)).text(),
  )
  db.$client.exec(
    `INSERT INTO history_index_migration
     SELECT 8, 'clean', 0,
       COALESCE((SELECT MAX(rowid) FROM history_fts), 0),
       COALESCE((SELECT MAX(rowid) FROM part), 0);
     UPDATE history_index_migration SET phase='done' WHERE version < 8;`,
  )
  finish(db)
  for (const word of ["inputneedle", "outputneedle", "reasonneedle"]) {
    expect(
      db.$client.prepare("SELECT count(*) AS n FROM history_fts_idx WHERE history_fts_idx MATCH ?").get(word),
    ).toEqual({ n: 1 })
  }
  expect(
    db.select().from(HistoryIndexMigrationTable).where(eq(HistoryIndexMigrationTable.version, MIGRATION_VERSION)).get()?.phase,
  ).toBe("done")
  expect(migrateIndexBatch(db)).toBe(false)
})

test("removing classification preserves existing FTS rows, progress and update triggers", async () => {
  const { Database: SQLite } = await import("bun:sqlite")
  const db = new SQLite(":memory:")
  try {
    db.exec(await Bun.file(new URL("../../migration/20260609000000_history_fts/migration.sql", import.meta.url)).text())
    db.exec(
      "INSERT INTO history_fts(rowid,part_id,session_id,message_id,project_id,kind,body,time_created) VALUES(42,'p','s','m','project','tool_input','originalneedle',1)",
    )
    db.exec("CREATE TABLE part(id TEXT); INSERT INTO part VALUES('p')")
    db.exec(
      await Bun.file(
        new URL("../../migration/20260914010000_history_index_version/migration.sql", import.meta.url),
      ).text(),
    )
    db.exec(
      await Bun.file(
        new URL("../../migration/20260914020000_history_all_content/migration.sql", import.meta.url),
      ).text(),
    )
    db.exec("UPDATE history_index_migration SET phase='done', cursor=42")
    const progress = db.prepare("SELECT * FROM history_index_migration").all()
    const before = db.prepare("SELECT rowid,part_id,body FROM history_fts").all()
    db.exec(
      await Bun.file(
        new URL("../../migration/20260914030000_history_remove_kind/migration.sql", import.meta.url),
      ).text(),
    )
    expect(
      db
        .prepare("PRAGMA table_info(history_fts)")
        .all()
        .map((row) => (row as { name: string }).name),
    ).not.toContain("kind")
    expect(db.prepare("SELECT rowid,part_id,body FROM history_fts").all()).toEqual(before)
    expect(db.prepare("SELECT * FROM history_index_migration").all()).toEqual(progress)
    const hits = (word: string) =>
      db.prepare("SELECT rowid FROM history_fts_idx WHERE history_fts_idx MATCH ?").all(word)
    expect(hits("originalneedle")).toEqual([{ rowid: 42 }])
    db.exec("UPDATE history_fts SET body='updatedneedle' WHERE part_id='p'")
    expect(hits("originalneedle")).toEqual([])
    expect(hits("updatedneedle")).toEqual([{ rowid: 42 }])
    db.exec(
      "INSERT INTO history_fts(part_id,session_id,message_id,project_id,body,time_created) VALUES('q','s','m','project','newneedle',2)",
    )
    expect(hits("newneedle")).toHaveLength(1)
    db.exec("DELETE FROM part WHERE id='p'")
    expect(hits("updatedneedle")).toEqual([])
  } finally {
    db.close()
  }
})
