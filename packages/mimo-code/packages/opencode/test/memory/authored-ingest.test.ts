import { afterEach, beforeEach, describe, expect, mock, test } from "bun:test"
import * as fs from "fs/promises"
import os from "os"
import path from "path"
import { Database } from "../../src/storage"
import { MemoryFtsTable } from "../../src/memory/fts.sql"
import { splitSections } from "../../src/memory/authored-ingest"
import { reconcileMemory } from "../../src/memory/reconcile"

type ToolCall = { name: string; arguments: Record<string, unknown> }
const toolCalls: ToolCall[] = []

mock.module("../../src/memory/mcp-client", () => ({
  getSharedMcpClient: async () => ({
    callTool: async (req: ToolCall) => {
      toolCalls.push(req)
      return { content: [{ type: "text", text: "{}" }] }
    },
  }),
  closeSharedMcpClient: async () => {},
}))

beforeEach(() => {
  toolCalls.length = 0
})

afterEach(() => {
  Database.use((db) => db.delete(MemoryFtsTable).run())
})

const SCOPE = { owner: "alice", workspaceId: "global" }

async function tmpRoot() {
  const base = await fs.mkdtemp(path.join(os.tmpdir(), "authored-ingest-"))
  return path.join(base, "memory")
}

function ingestCalls() {
  return toolCalls.filter((c) => c.name === "ingest_authored")
}

describe("splitSections", () => {
  test("splits on ## headings, heading carried into content", () => {
    const body = "preamble line\n\n## Preferences\ngreen tea\n\n## Build\nneeds FM_DB_PATH"
    expect(splitSections(body)).toEqual([
      { anchor: "", content: "preamble line" },
      { anchor: "Preferences", content: "Preferences\ngreen tea" },
      { anchor: "Build", content: "Build\nneeds FM_DB_PATH" },
    ])
  })

  test("whole file becomes one section when no headings", () => {
    expect(splitSections("just one fact")).toEqual([{ anchor: "", content: "just one fact" }])
  })

  test("empty and whitespace-only bodies yield no sections", () => {
    expect(splitSections("")).toEqual([])
    expect(splitSections("\n\n  \n")).toEqual([])
    expect(splitSections("## Empty Section\n\n")).toEqual([])
  })
})

describe("reconcileMemory fm ingest", () => {
  test("projects MEMORY.md files only; checkpoints and free notes stay out", async () => {
    const root = await tmpRoot()
    const globalDir = path.join(root, "global")
    const sessionDir = path.join(root, "sessions", "ses_1")
    await fs.mkdir(globalDir, { recursive: true })
    await fs.mkdir(sessionDir, { recursive: true })
    await fs.writeFile(path.join(globalDir, "MEMORY.md"), "## Prefs\ne prefers green tea")
    await fs.writeFile(path.join(sessionDir, "checkpoint.md"), "operational state")
    await fs.writeFile(path.join(globalDir, "scratch.md"), "free note")

    await reconcileMemory({ mimo: root }, SCOPE)

    const calls = ingestCalls()
    expect(calls.length).toBe(1)
    const args = calls[0].arguments
    expect(args.source_path).toBe(path.join(globalDir, "MEMORY.md"))
    expect(args.owner).toBe("alice")
    expect(args.workspace_id).toBe("global")
    expect(args.sections).toEqual([{ anchor: "Prefs", content: "Prefs\ne prefers green tea" }])
  })

  test("no scope, no ingest calls (vanilla reconcile untouched)", async () => {
    const root = await tmpRoot()
    const dir = path.join(root, "global")
    await fs.mkdir(dir, { recursive: true })
    await fs.writeFile(path.join(dir, "MEMORY.md"), "fact")

    await reconcileMemory({ mimo: root })
    expect(ingestCalls().length).toBe(0)
  })

  test("re-reconcile re-projects (engine hash-dedup owns idempotency)", async () => {
    const root = await tmpRoot()
    const dir = path.join(root, "global")
    await fs.mkdir(dir, { recursive: true })
    await fs.writeFile(path.join(dir, "MEMORY.md"), "fact")

    await reconcileMemory({ mimo: root }, SCOPE)
    await reconcileMemory({ mimo: root }, SCOPE)
    expect(ingestCalls().length).toBe(2)
  })

  test("deleted MEMORY.md wipes its projection with empty sections", async () => {
    const root = await tmpRoot()
    const dir = path.join(root, "global")
    const file = path.join(dir, "MEMORY.md")
    await fs.mkdir(dir, { recursive: true })
    await fs.writeFile(file, "fact to forget")
    await reconcileMemory({ mimo: root }, SCOPE)
    toolCalls.length = 0

    await fs.rm(file)
    await reconcileMemory({ mimo: root }, SCOPE)

    const calls = ingestCalls()
    expect(calls.length).toBe(1)
    expect(calls[0].arguments.source_path).toBe(file)
    expect(calls[0].arguments.sections).toEqual([])
  })
})
