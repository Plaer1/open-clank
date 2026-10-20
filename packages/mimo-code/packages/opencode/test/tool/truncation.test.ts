import { describe, test, expect } from "bun:test"
import { NodeFileSystem } from "@effect/platform-node"
import { Effect, FileSystem, Layer } from "effect"
import { Truncate } from "../../src/tool"
import { Identifier } from "../../src/id/id"
import { Process } from "../../src/util"
import { Filesystem } from "../../src/util"
import path from "path"
import { testEffect } from "../lib/effect"
import { writeFileStringScoped } from "../lib/filesystem"

const FIXTURES_DIR = path.join(import.meta.dir, "fixtures")
const ROOT = path.resolve(import.meta.dir, "..", "..")

const it = testEffect(Layer.mergeAll(Truncate.defaultLayer, NodeFileSystem.layer))

describe("Truncate", () => {
  describe("output", () => {
    it.live("truncates large json file by bytes", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = yield* Effect.promise(() => Filesystem.readText(path.join(FIXTURES_DIR, "models-api.json")))
        const result = yield* svc.output(content)

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("truncated...")
        if (result.truncated) expect(result.outputPath).toBeDefined()
      }),
    )

    it.live("returns content unchanged when under limits", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = "line1\nline2\nline3"
        const result = yield* svc.output(content)

        expect(result.truncated).toBe(false)
        expect(result.content).toBe(content)
      }),
    )

    it.live("truncates by line count", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 100 }, (_, i) => `line${i}`).join("\n")
        const result = yield* svc.output(lines, { maxLines: 10 })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("...90 lines truncated...")
      }),
    )

    it.live("truncates by byte count", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = "a".repeat(1000)
        const result = yield* svc.output(content, { maxBytes: 100 })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("truncated...")
      }),
    )

    it.live("truncates from head by default", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 10 }, (_, i) => `line${i}`).join("\n")
        const result = yield* svc.output(lines, { maxLines: 3 })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("line0")
        expect(result.content).toContain("line1")
        expect(result.content).toContain("line2")
        expect(result.content).not.toContain("line9")
      }),
    )

    it.live("truncates from tail when direction is tail", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 10 }, (_, i) => `line${i}`).join("\n")
        const result = yield* svc.output(lines, { maxLines: 3, direction: "tail" })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("line7")
        expect(result.content).toContain("line8")
        expect(result.content).toContain("line9")
        expect(result.content).not.toContain("line0")
      }),
    )

    test("uses default MAX_LINES and MAX_BYTES", () => {
      expect(Truncate.MAX_LINES).toBe(2000)
      expect(Truncate.MAX_BYTES).toBe(50 * 1024)
    })

    it.live("large single-line file truncates with byte message", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = yield* Effect.promise(() => Filesystem.readText(path.join(FIXTURES_DIR, "models-api.json")))
        const result = yield* svc.output(content)

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("bytes truncated...")
        expect(Buffer.byteLength(content, "utf-8")).toBeGreaterThan(Truncate.MAX_BYTES)
      }),
    )

    it.live("writes full output to file when truncated", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 100 }, (_, i) => `line${i}`).join("\n")
        const result = yield* svc.output(lines, { maxLines: 10 })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("The tool call succeeded but the output was truncated")
        expect(result.content).toContain("Grep")
        if (!result.truncated) throw new Error("expected truncated")
        expect(result.outputPath).toBeDefined()
        expect(result.outputPath).toContain("tool_")

        const written = yield* Effect.promise(() => Filesystem.readText(result.outputPath!))
        expect(written).toBe(lines)
      }),
    )

    it.live("labels truncated error output as failed", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = Array.from({ length: 100 }, (_, i) => `error line ${i}`).join("\n")
        const result = yield* svc.output(content, { maxLines: 10, outcome: "error" })

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("The tool call failed but the output was truncated")
        expect(result.content).not.toContain("The tool call succeeded")
      }),
    )

    it.live("suggests actor tool when agent has actor permission", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 100 }, (_, i) => `line${i}`).join("\n")
        const agent = { permission: [{ permission: "actor", pattern: "*", action: "allow" as const }] }
        const result = yield* svc.output(lines, { maxLines: 10 }, agent as any)

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("Grep")
        expect(result.content).toContain("actor tool")
      }),
    )

    it.live("omits actor tool hint when agent lacks actor permission", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const lines = Array.from({ length: 100 }, (_, i) => `line${i}`).join("\n")
        const agent = { permission: [{ permission: "actor", pattern: "*", action: "deny" as const }] }
        const result = yield* svc.output(lines, { maxLines: 10 }, agent as any)

        expect(result.truncated).toBe(true)
        expect(result.content).toContain("Grep")
        expect(result.content).not.toContain("actor tool")
      }),
    )

    it.live("does not write file when not truncated", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const content = "short content"
        const result = yield* svc.output(content)

        expect(result.truncated).toBe(false)
        if (result.truncated) throw new Error("expected not truncated")
        expect("outputPath" in result).toBe(false)
      }),
    )

    test("loads truncate effect in a fresh process", async () => {
      const out = await Process.run([process.execPath, "run", path.join(ROOT, "src", "tool", "truncate.ts")], {
        cwd: ROOT,
      })

      expect(out.code).toBe(0)
    }, 20000)

    it.live("head+tail with errors in tail shows head and tail sections", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        // Build a large output: many normal lines + error lines at the end
        const normalLines = Array.from({ length: 100 }, (_, i) => `normal line ${i}`)
        const errorLines = ["Error: something went wrong", "exit code 1"]
        const allLines = [...normalLines, ...errorLines]
        const text = allLines.join("\n")

        const result = yield* svc.output(text, { maxLines: 10, direction: "head+tail" })

        expect(result.truncated).toBe(true)
        // Should contain head content
        expect(result.content).toContain("normal line 0")
        // Should contain tail error content
        expect(result.content).toContain("Error: something went wrong")
        expect(result.content).toContain("exit code 1")
        // Should contain omission marker
        expect(result.content).toContain("lines omitted — showing head and tail")
      }),
    )

    it.live("head+tail without errors in tail degrades to head mode", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        // Build output with no error keywords at the end
        const lines = Array.from({ length: 100 }, (_, i) => `normal line ${i}`)
        const text = lines.join("\n")

        const result = yield* svc.output(text, { maxLines: 10, direction: "head+tail" })

        expect(result.truncated).toBe(true)
        // Should behave like head: contains first lines
        expect(result.content).toContain("normal line 0")
        // Should NOT contain head+tail omission marker (degraded to head)
        expect(result.content).not.toContain("lines omitted — showing head and tail")
        // Should contain normal truncation marker
        expect(result.content).toContain("truncated...")
      }),
    )

    it.live("pressureCaps halves maxLines before size check", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        // With maxLines=20 and pressureCaps=true, effective limit becomes 10
        const lines = Array.from({ length: 15 }, (_, i) => `line${i}`).join("\n")

        // Without pressureCaps: 15 lines fits within maxLines=20, so not truncated
        const resultNoPressure = yield* svc.output(lines, { maxLines: 20 })
        expect(resultNoPressure.truncated).toBe(false)

        // With pressureCaps: effective maxLines=10, so 15 lines gets truncated
        const resultWithPressure = yield* svc.output(lines, { maxLines: 20, pressureCaps: true })
        expect(resultWithPressure.truncated).toBe(true)
      }),
    )
  })

  describe("cleanup", () => {
    const DAY_MS = 24 * 60 * 60 * 1000

    it.live("deletes expired and orphaned files while preserving recent owned files", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const fs = yield* FileSystem.FileSystem

        yield* fs.makeDirectory(Truncate.DIR, { recursive: true })

        const old = path.join(Truncate.DIR, Identifier.create("tool", "ascending", Date.now() - 10 * DAY_MS))
        const orphan = path.join(Truncate.DIR, Identifier.create("tool", "ascending", Date.now() - 3 * DAY_MS))
        const corrupt = path.join(Truncate.DIR, Identifier.create("tool", "ascending", Date.now()))
        const recent = yield* svc.write("recent content", {
          owner: "local",
          workspace: ROOT,
          sessionID: "recent-session",
        })

        yield* writeFileStringScoped(old, "old content")
        yield* writeFileStringScoped(orphan, "orphan content")
        yield* writeFileStringScoped(corrupt, "corrupt pair")
        yield* writeFileStringScoped(corrupt + ".meta.json", "{not-json")
        yield* svc.cleanup()

        expect(yield* fs.exists(old)).toBe(false)
        expect(yield* fs.exists(orphan)).toBe(false)
        expect(yield* fs.exists(recent)).toBe(true)
        expect(yield* fs.exists(corrupt)).toBe(false)
        expect(yield* fs.exists(corrupt + ".meta.json")).toBe(false)
      }),
    )
  })

  describe("ownership and deletion", () => {
    it.live("writes atomic owner, workspace, and session metadata", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const fs = yield* FileSystem.FileSystem
        const workspace = path.join(ROOT, "test-workspace")
        const output = yield* svc.write("retained output", {
          owner: "Alice",
          workspace,
          sessionID: "session-owned",
          callID: "call-owned",
        })

        expect(yield* fs.exists(output)).toBe(true)
        expect(yield* fs.exists(output + ".meta.json")).toBe(true)
        const metadata = JSON.parse(yield* fs.readFileString(output + ".meta.json"))
        expect(metadata).toMatchObject({
          version: 1,
          output: path.basename(output),
          owner: "alice",
          workspace: path.resolve(workspace),
          sessionID: "session-owned",
          callID: "call-owned",
        })

        expect(
          yield* svc.remove(output, {
            owner: "bob",
            sessionID: "session-owned",
          }),
        ).toBe(false)
        expect(yield* fs.exists(output)).toBe(true)
        expect(
          yield* svc.remove(output, {
            owner: "alice",
            sessionID: "session-owned",
          }),
        ).toBe(true)
        expect(yield* fs.exists(output)).toBe(false)
        expect(yield* fs.exists(output + ".meta.json")).toBe(false)
      }),
    )

    it.live("session cleanup removes only the exact owner scope", () =>
      Effect.gen(function* () {
        const svc = yield* Truncate.Service
        const fs = yield* FileSystem.FileSystem
        const alice = yield* svc.write("alice output", {
          owner: "alice",
          workspace: ROOT,
          sessionID: "session-cleanup",
        })
        const bob = yield* svc.write("bob output", {
          owner: "bob",
          workspace: ROOT,
          sessionID: "session-cleanup",
        })

        expect(yield* svc.removeSession("session-cleanup", "alice")).toBe(1)
        expect(yield* fs.exists(alice)).toBe(false)
        expect(yield* fs.exists(alice + ".meta.json")).toBe(false)
        expect(yield* fs.exists(bob)).toBe(true)
        expect(
          yield* svc.remove(bob, {
            owner: "bob",
            sessionID: "session-cleanup",
          }),
        ).toBe(true)
      }),
    )
  })
})
