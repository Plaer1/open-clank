import { describe, expect } from "bun:test"
import fs from "fs/promises"
import path from "path"
import { Cause, Effect, Exit, Layer } from "effect"
import { GlobTool } from "../../src/tool/glob"
import { SessionID, MessageID } from "../../src/session/schema"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Ripgrep } from "../../src/file/ripgrep"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Truncate } from "../../src/tool"
import { Agent } from "../../src/agent/agent"
import { provideTmpdirInstance } from "../fixture/fixture"
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

describe("tool.glob", () => {
  it.live("matches files from a directory path", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        yield* Effect.promise(() => Bun.write(path.join(dir, "a.ts"), "export const a = 1\n"))
        yield* Effect.promise(() => Bun.write(path.join(dir, "b.txt"), "hello\n"))
        const info = yield* GlobTool
        const glob = yield* info.init()
        const result = yield* glob.execute(
          {
            pattern: "*.ts",
            path: dir,
          },
          ctx,
        )
        expect(result.metadata.count).toBe(1)
        expect(result.output).toContain(path.join(dir, "a.ts"))
        expect(result.output).not.toContain(path.join(dir, "b.txt"))
      }),
    ),
  )

  it.live("rejects exact file paths", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        const file = path.join(dir, "a.ts")
        yield* Effect.promise(() => Bun.write(file, "export const a = 1\n"))
        const info = yield* GlobTool
        const glob = yield* info.init()
        const exit = yield* glob
          .execute(
            {
              pattern: "*.ts",
              path: file,
            },
            ctx,
          )
          .pipe(Effect.exit)
        expect(Exit.isFailure(exit)).toBe(true)
        if (Exit.isFailure(exit)) {
          const err = Cause.squash(exit.cause)
          expect(err instanceof Error ? err.message : String(err)).toContain("glob path must be a directory")
        }
      }),
    ),
  )

  it.live("paginates without dropping paths", () =>
    provideTmpdirInstance((dir) =>
      Effect.gen(function* () {
        for (const name of ["a.ts", "b.ts", "c.ts"]) {
          const file = path.join(dir, name)
          yield* Effect.promise(() => Bun.write(file, name))
          yield* Effect.promise(() => fs.utimes(file, 1_700_000_000, 1_700_000_000))
        }
        const info = yield* GlobTool
        const glob = yield* info.init()
        const first = yield* glob.execute({ pattern: "*.ts", path: dir, limit: 2 }, ctx)
        const second = yield* glob.execute(
          { pattern: "*.ts", path: dir, limit: 2, cursor: first.metadata.page.next_cursor },
          ctx,
        )

        expect(first.metadata.page.has_more).toBe(true)
        expect(first.metadata.file.contract).toBe("open-clank.file-result/v1")
        expect(first.metadata.file.page.returned).toBe(2)
        expect(first.metadata.file.truncation_reason).toBe("result_limit")
        expect(second.metadata.page.has_more).toBe(false)
        expect(new Set([...first.metadata.paths, ...second.metadata.paths]).size).toBe(3)
        expect([...first.metadata.paths, ...second.metadata.paths]).toEqual(
          ["a.ts", "b.ts", "c.ts"].map((name) => path.join(dir, name)),
        )
      }),
    ),
  )
})
