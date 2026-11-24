import { afterEach, describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import { Database } from "../../src/storage"
import { HistoryFtsTable } from "../../src/history/fts.sql"
import { MessageTable, PartTable, SessionTable } from "../../src/session/session.sql"
import { ProjectTable } from "../../src/project/project.sql"
import { HistoryTool } from "../../src/tool/history"
import { History } from "../../src/history"
import { Truncate } from "../../src/tool"
import { Agent } from "../../src/agent/agent"
import { Instance } from "../../src/project/instance"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { SessionID, MessageID } from "../../src/session/schema"
import { ManagedProvider } from "../../src/acp/managed-provider"
import { installManagedSessionBinding } from "../../src/memory/session-scope"

afterEach(async () => {
  Database.use((db) => {
    db.delete(HistoryFtsTable).run()
    db.delete(PartTable).run()
    db.delete(MessageTable).run()
    db.delete(SessionTable).run()
    db.delete(ProjectTable).run()
  })
  await Instance.disposeAll()
  ManagedProvider.resetForTest()
  delete process.env.OPEN_CLANK_MANAGED
})

const it = testEffect(
  Layer.mergeAll(History.defaultLayer, Truncate.defaultLayer, Agent.defaultLayer, CrossSpawnSpawner.defaultLayer),
)

const ctx = {
  sessionID: SessionID.make("ses_test"),
  messageID: MessageID.make(""),
  callID: "",
  agent: "build",
  abort: AbortSignal.any([]),
  messages: [],
  metadata: () => Effect.void,
  ask: () => Effect.void,
}

describe("HistoryTool", () => {
  it.live("managed history rejects unsupported project scope instead of relabeling it as the bound chat", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        process.env.OPEN_CLANK_MANAGED = "1"
        installManagedSessionBinding("ses_test", {
          owner: "alice",
          stableChatID: "current-chat",
          engineSessionID: "ses_test",
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
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const exit = yield* Effect.exit(tool.execute({ operation: "search", query: "private", scope: "project" }, ctx as any))
        expect(exit._tag).toBe("Failure")
        expect(exit._tag === "Failure" ? String(exit.cause) : "").toContain("does not support project scope")
      }),
    ),
  )

  it.live("managed legacy hits retain the engine binding while get, around, and media target the returned chat", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        process.env.OPEN_CLANK_MANAGED = "1"
        installManagedSessionBinding("ses_test", {
          owner: "alice",
          stableChatID: "current-chat",
          engineSessionID: "ses_test",
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
        const calls: Array<Record<string, unknown>> = []
        ManagedProvider.installHostConnection({
          async extMethod(method: string, params: Record<string, unknown>) {
            expect(method).toBe("_openclank/history/v1/query")
            calls.push(params)
            if (params.operation === "search") return { ok: true, operation: "search", result: { ok: true, hits: [{ part_id: "legacy-p", session_id: "legacy-chat", message_id: "legacy-m", project_id: "", kind: "tool_output", tool_name: "Bash", snippet: "legacy", score: 1, time_created: 1 }], limit: 10, more: false } }
            if (params.operation === "get") return { ok: true, operation: "get", result: { ok: true, part: { part_id: "legacy-p", message_id: "legacy-m", session_id: "legacy-chat", type: "tool", role: "assistant", tool_name: "Bash", text: "tool: Bash\ninput: {}\noutput: ok", has_more: false, next_offset: null, attachments: [{ asset_id: "asset-1", mime_type: "image/png", filename: null, byte_size: 3 }], time_created: 1 } } }
            if (params.operation === "around") return { ok: true, operation: "around", result: { ok: true, session_id: "legacy-chat", messages: [{ message_id: "legacy-m", matched: true, time_created: 1, parts: [{ part_id: "legacy-p", type: "tool", role: "assistant", tool_name: "Bash", text: "tool: Bash\ninput: {}\noutput: ok" }] }] } }
            if (params.operation === "media") return { ok: true, operation: "media", result: { ok: true, attachments: [{ asset_id: "asset-1", mime_type: "image/png", filename: null, byte_size: 3 }] } }
            throw new Error("unexpected history operation")
          },
        } as any)
        const info = yield* HistoryTool
        const tool = yield* info.init()
        yield* tool.execute({ operation: "search", query: "legacy", scope: "global", kind: ["tool_output"], tool_name: "Bash", time_after: 1, time_before: 2 }, ctx as any)
        yield* tool.execute({ operation: "get", session_id: "legacy-chat", message_id: "legacy-m", part_id: "legacy-p" }, ctx as any)
        yield* tool.execute({ operation: "around", session_id: "legacy-chat", message_id: "legacy-m" }, ctx as any)
        yield* tool.execute({ operation: "media", session_id: "legacy-chat", message_id: "legacy-m", part_id: "legacy-p" }, ctx as any)
        expect(calls.every((params) => params.sessionID === "ses_test")).toBe(true)
        expect(calls[0]).toMatchObject({ kind: ["tool_output"], toolName: "Bash", timeAfter: 1, timeBefore: 2 })
        expect(calls.filter((params) => params.operation !== "search").every((params) => params.chatID === "legacy-chat")).toBe(true)
      }),
    ),
  )

  it.live("operation=search returns markdown with hits", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        Database.use((db) => {
          db.insert(HistoryFtsTable)
            .values({
              part_id: "p1",
              session_id: "ses_a",
              message_id: "msg_a",
              project_id: "proj_a",
              kind: "user_text",
              tool_name: null,
              body: "JWT signing test",
              time_created: 1000,
            })
            .run()
        })
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const result = yield* tool.execute(
          { operation: "search", query: "JWT", scope: "global" },
          ctx as any,
        )
        expect(result.output).toContain("msg_a")
        expect(result.output).toContain("JWT")
        expect(result.metadata.count).toBe(1)
      }),
    ),
  )

  it.live("operation=search with no hits returns empty message", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const result = yield* tool.execute(
          { operation: "search", query: "nothing", scope: "global" },
          ctx as any,
        )
        expect(result.metadata.count).toBe(0)
        expect(result.output).toContain("0 matches")
      }),
    ),
  )

  it.live("operation=around returns marked anchor message", () =>
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const now = Date.now()
        Database.use((db) => {
          db.insert(ProjectTable)
            .values({
              id: Instance.project.id,
              worktree: "/tmp",
              sandboxes: [] as any,
              time_created: now,
              time_updated: now,
            } as any)
            .onConflictDoNothing()
            .run()
          db.insert(SessionTable)
            .values({
              id: ctx.sessionID,
              project_id: Instance.project.id,
              slug: "x",
              directory: "/tmp",
              title: "t",
              version: "1",
              time_created: now,
              time_updated: now,
            })
            .run()
          for (let i = 0; i < 3; i++) {
            db.insert(MessageTable)
              .values({
                id: `m${i}` as any,
                session_id: ctx.sessionID,
                agent_id: "main",
                data: { role: "user" } as any,
                time_created: now + i,
                time_updated: now + i,
              })
              .run()
            db.insert(PartTable)
              .values({
                id: `pt${i}` as any,
                message_id: `m${i}` as any,
                session_id: ctx.sessionID,
                data: { type: "text", text: `body ${i}` } as any,
                time_created: now + i,
                time_updated: now + i,
              })
              .run()
          }
        })
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const result = yield* tool.execute(
          { operation: "around", message_id: "m1", before: 1, after: 1 },
          ctx as any,
        )
        expect(result.output).toContain(">>> m1")
        expect(result.output).toContain("m0")
        expect(result.output).toContain("m2")
      }),
    ),
  )
})
