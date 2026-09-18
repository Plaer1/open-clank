import { describe, expect } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { Effect, Layer } from "effect"
import { GrepTool } from "../../src/tool/grep"
import { provideInstance, provideTmpdirInstance } from "../fixture/fixture"
import { SessionID, MessageID } from "../../src/session/schema"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Truncate } from "../../src/tool"
import { Agent } from "../../src/agent/agent"
import { Ripgrep } from "../../src/file/ripgrep"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { testEffect } from "../lib/effect"

const it = testEffect(
  Layer.mergeAll(
    CrossSpawnSpawner.defaultLayer,
    AppFileSystem.defaultLayer,
    Ripgrep.defaultLayer,
    Truncate.defaultLayer,
    Agent.defaultLayer,
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

const root = path.join(__dirname, "../..")

describe("tool.grep", () => {
  it.live("basic search", () =>
    Effect.gen(function* () {
      const info = yield* GrepTool
      const grep = yield* info.init()
      const result = yield* provideInstance(root)(
        grep.execute(
          {
            pattern: "export",
            path: path.join(root, "src/tool"),
            include: "*.ts",
          },
          ctx,
        ),
      )
      expect(result.metadata.matches).toBeGreaterThan(0)
      expect(result.output).toContain("Found")
    }),
  )

  it.live("no matches returns correct output", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        yield* Effect.promise(() => Bun.write(path.join(dir, "test.txt"), "hello world"))
        const info = yield* GrepTool
        const grep = yield* info.init()
        const result = yield* grep.execute(
          {
            pattern: "xyznonexistentpatternxyz123",
            path: dir,
          },
          ctx,
        )
        expect(result.metadata.matches).toBe(0)
        expect(result.output).toBe("No files found")
      }),
    ),
  )

  it.live("finds matches in tmp instance", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        yield* Effect.promise(() => Bun.write(path.join(dir, "test.txt"), "line1\nline2\nline3"))
        const info = yield* GrepTool
        const grep = yield* info.init()
        const result = yield* grep.execute(
          {
            pattern: "line",
            path: dir,
          },
          ctx,
        )
        expect(result.metadata.matches).toBeGreaterThan(0)
      }),
    ),
  )

  it.live("supports exact file paths", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "test.txt")
        yield* Effect.promise(() => Bun.write(file, "line1\nline2\nline3"))
        const info = yield* GrepTool
        const grep = yield* info.init()
        const result = yield* grep.execute(
          {
            pattern: "line2",
            path: file,
          },
          ctx,
        )
        expect(result.metadata.matches).toBe(1)
        expect(result.output).toContain(file)
        expect(result.output).toContain("Line 2: line2")
      }),
    ),
  )

  it.live("supports literal search and cursor pagination", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        yield* Effect.promise(() => Bun.write(path.join(dir, "test.txt"), "a+b\nab\na+b\n"))
        const info = yield* GrepTool
        const grep = yield* info.init()
        const first = yield* grep.execute(
          { pattern: "a+b", path: dir, mode: "literal", limit: 1 },
          ctx,
        )
        const second = yield* grep.execute(
          {
            pattern: "a+b",
            path: dir,
            mode: "literal",
            limit: 1,
            cursor: first.metadata.page.next_cursor,
          },
          ctx,
        )

        expect(first.metadata.search_mode).toBe("literal")
        expect(first.metadata.items).toHaveLength(1)
        expect(first.metadata.page.has_more).toBe(true)
        expect(first.metadata.file.contract).toBe("open-clank.file-result/v1")
        expect(first.metadata.file.items).toEqual(first.metadata.items)
        expect(first.metadata.file.truncation_reason).toBe("result_limit")
        expect(second.metadata.items).toHaveLength(1)
        expect(second.metadata.page.has_more).toBe(false)
      }),
    ),
  )

  it.live("uses path order to stabilize pages with equal mtimes", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        for (const name of ["a.txt", "b.txt", "c.txt"]) {
          const file = path.join(dir, name)
          yield* Effect.promise(() => Bun.write(file, "needle\n"))
          yield* Effect.promise(() => fs.utimes(file, 1_700_000_000, 1_700_000_000))
        }
        const info = yield* GrepTool
        const grep = yield* info.init()
        const first = yield* grep.execute({ pattern: "needle", path: dir, limit: 2 }, ctx)
        const second = yield* grep.execute(
          { pattern: "needle", path: dir, limit: 2, cursor: first.metadata.page.next_cursor },
          ctx,
        )

        expect([...first.metadata.items, ...second.metadata.items].map((item) => item.path)).toEqual(
          ["a.txt", "b.txt", "c.txt"].map((name) => path.join(dir, name)),
        )
      }),
    ),
  )
})
