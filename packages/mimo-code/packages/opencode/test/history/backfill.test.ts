import { afterEach, beforeEach, describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { Database } from "../../src/storage"
import { HistoryFtsTable } from "../../src/history/fts.sql"
import { MessageTable, PartTable, SessionTable } from "../../src/session/session.sql"
import { ProjectTable } from "../../src/project/project.sql"
import { backfillAll } from "../../src/history/backfill"
import { History } from "../../src/history"
import { Instance } from "../../src/project/instance"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { installManagedSessionBinding } from "../../src/memory/session-scope"

// The test process shares a single in-memory SQLite DB (test/preload sets
// MIMOCODE_DB=:memory:), so other suites' SessionTable/PartTable rows are visible
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
  wipe()
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
          .values({ id: projectID as any, worktree: "/tmp", sandboxes: [] as any, time_created: now, time_updated: now } as any)
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

  it.live("is idempotent (NOT EXISTS skips already-indexed parts)", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        seed([
          { session_id: "ses_x", message_id: "m1", part_id: "p1", role: "user", type: "text", text: "first" },
        ])
        yield* backfillAll()
        seed([
          { session_id: "ses_x", message_id: "m2", part_id: "p2", role: "user", type: "text", text: "second" },
        ])
        yield* backfillAll()

        const rows = Database.use((db) => db.select().from(HistoryFtsTable).all())
        expect(rows.map((r) => r.part_id).sort()).toEqual(["p1", "p2"])
      }),
    ),
  )
})
