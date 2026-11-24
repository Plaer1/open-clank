import { afterEach, describe, expect, spyOn } from "bun:test"
import { Effect, Layer } from "effect"
import { Database } from "../../src/storage"
import { HistoryFtsTable } from "../../src/history/fts.sql"
import { MessageTable, PartTable, SessionTable } from "../../src/session/session.sql"
import { ProjectTable } from "../../src/project/project.sql"
import { HistoryTool } from "../../src/tool/history"
import { History } from "../../src/history"
import { Truncate } from "../../src/tool"
import { Provider } from "../../src/provider"
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
  Layer.mergeAll(
    History.defaultLayer,
    Provider.defaultLayer,
    Truncate.defaultLayer,
    Agent.defaultLayer,
    CrossSpawnSpawner.defaultLayer,
  ),
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
        const service = yield* History.Service
        const localGet = spyOn(service, "get")
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const denied = yield* tool.execute({ operation: "get", session_id: "legacy-chat", message_id: "legacy-m", part_id: "legacy-p", attachment: "file:0" }, ctx as any)
        expect(denied.output).toContain("Host-owned attachment locators")
        expect(localGet).not.toHaveBeenCalled()
        localGet.mockRestore()
        yield* tool.execute({ operation: "search", query: "legacy", scope: "global", kind: ["tool_output"], tool_name: "Bash", time_after: 1, time_before: 2 }, ctx as any)
        yield* tool.execute({ operation: "get", session_id: "legacy-chat", message_id: "legacy-m", part_id: "legacy-p" }, ctx as any)
        yield* tool.execute({ operation: "around", session_id: "legacy-chat", message_id: "legacy-m" }, ctx as any)
        yield* tool.execute({ operation: "media", session_id: "legacy-chat", message_id: "legacy-m", part_id: "legacy-p" }, ctx as any)
        expect(calls.every((params) => params.sessionID === "ses_test")).toBe(true)
        expect(calls.find((params) => params.operation === "search")).toMatchObject({ kind: ["tool_output"], toolName: "Bash", timeAfter: 1, timeBefore: 2 })
        expect(calls.filter((params) => params.operation !== "search").every((params) => params.chatID === "legacy-chat")).toBe(true)
        delete process.env.OPEN_CLANK_MANAGED
        const scopedPart = spyOn(service, "part").mockReturnValue(Effect.succeed({ part_id: "p", message_id: "m", session_id: "ses_test", type: "file", role: "user", tool_name: null, text: "file", has_more: false, next_offset: null, attachments: [], time_created: 1 }))
        const localFile = spyOn(service, "get").mockReturnValue(Effect.succeed({ text: "file", offset: 0, next_offset: null, total_length: 4, has_more: false, attachments: [{ id: "file:0", mime: "image/png", url: "file:///private/secret.png" }] } as any))
        const file = yield* tool.execute({ operation: "get", message_id: "m", part_id: "p", attachment: "file:0" }, ctx as any)
        expect(file.output).toContain("file URLs require normal read permission")
        expect(file.attachments).toBeUndefined()
        scopedPart.mockRestore()
        localFile.mockRestore()
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
      kind: "assistant_text",

              tool_name: null,
              body: "JWT signing test", body_idx: "JWT signing test", time_created: 1000,
            })
            .run()
        })
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const result = yield* tool.execute({ operation: "search", query: "JWT", scope: "global" }, ctx as any)
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
        const result = yield* tool.execute({ operation: "search", query: "nothing", scope: "global" }, ctx as any)
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
        const result = yield* tool.execute({ operation: "around", message_id: "m1", before: 1, after: 1 }, ctx as any)
        expect(result.output).toContain(">>> message_id=m1")
        expect(result.output).toContain("m0")
        expect(result.output).toContain("m2")
      }),
    ),
  )

  it.live("operation=around keeps a huge anchor partially instead of dropping it", () =>
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
              id: "ses_huge" as any,
              project_id: Instance.project.id,
              slug: "x",
              directory: "/tmp",
              title: "t",
              version: "1",
              time_created: now,
              time_updated: now,
            })
            .run()
          db.insert(MessageTable)
            .values({
              id: "m_huge" as any,
              session_id: "ses_huge" as any,
              agent_id: "main",
              data: { role: "user" } as any,
              time_created: now,
              time_updated: now,
            })
            .run()
          // Each part summary is capped ~1KB by history.summary(); 25 parts
          // exceed the 19500 around budget so the full anchor block cannot fit.
          for (let i = 0; i < 25; i++) {
            db.insert(PartTable)
              .values({
                id: `pt_huge_${i}` as any,
                message_id: "m_huge" as any,
                session_id: "ses_huge" as any,
                data: { type: "text", text: `PART-${i} ` + "x".repeat(2000) } as any,
                time_created: now + i,
                time_updated: now + i,
              })
              .run()
          }
          db.insert(MessageTable)
            .values({
              id: "m_small" as any,
              session_id: "ses_huge" as any,
              agent_id: "main",
              data: { role: "user" } as any,
              time_created: now + 100,
              time_updated: now + 100,
            })
            .run()
          db.insert(PartTable)
            .values({
              id: "pt_small" as any,
              message_id: "m_small" as any,
              session_id: "ses_huge" as any,
              data: { type: "text", text: "small neighbor" } as any,
              time_created: now + 100,
              time_updated: now + 100,
            })
            .run()
        })
        const info = yield* HistoryTool
        const tool = yield* info.init()
        const result = yield* tool.execute(
          { operation: "around", message_id: "m_huge", before: 1, after: 1 },
          { ...ctx, sessionID: "ses_huge" } as any,
        )
        // Anchor header must survive even when the full block cannot fit.
        // Part ids sort as strings (pt_huge_0, pt_huge_1, pt_huge_10, …), so
        // later numeric parts are the ones dropped by the partial accept.
        expect(result.output).toContain(">>> message_id=m_huge")
        expect(result.output).toContain("part_id=pt_huge_0")
        expect(result.output).toContain("Anchor truncated")
        expect(result.output).not.toContain("part_id=pt_huge_9 ")
        expect(result.metadata.truncated).toBe(true)
      }),
    ),
  )
})

it.live("search schema has no content-type filter", () =>
  provideTmpdirInstance(() =>
    Effect.gen(function* () {
      const info = yield* HistoryTool
      const tool = yield* info.init()
      expect(tool.parameters.shape).not.toHaveProperty("kind")
      expect(tool.parameters.safeParse({ operation: "search", query: "needle" }).success).toBe(true)
      for (const kind of ["user_text", "assistant_text", "reasoning", "tool_output", "tool_error", "file"]) {
        expect(tool.parameters.safeParse({ operation: "search", query: "needle", kind: [kind] }).success).toBe(false)
      }
    }),
  ),
)
