import path from "path"
import z from "zod"
import { Effect, Option } from "effect"
import * as Stream from "effect/Stream"
import { InstanceState } from "@/effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Ripgrep } from "../file/ripgrep"
import { assertExternalDirectoryEffect } from "./external-directory"
import { SessionCwd } from "./session-cwd"
import DESCRIPTION from "./glob.txt"
import * as Tool from "./tool"
import { fileResult } from "./file-contract"

export const GlobTool = Tool.define(
  "glob",
  Effect.gen(function* () {
    const rg = yield* Ripgrep.Service
    const fs = yield* AppFileSystem.Service

    return {
      description: DESCRIPTION,
      parameters: z.object({
        pattern: z.string().describe("The glob pattern to match files against"),
        path: z
          .string()
          .optional()
          .describe(
            `The directory to search in. If not specified, the current working directory will be used. IMPORTANT: Omit this field to use the default directory. DO NOT enter "undefined" or "null" - simply omit it for the default behavior. Must be a valid directory path if provided.`,
          ),
        cursor: z.number().int().nonnegative().optional(),
        limit: z.number().int().min(1).max(200).optional(),
      }),
      resources: (
        params: { pattern: string; path?: string; cursor?: number; limit?: number },
        ctx: Tool.Context,
      ) => {
        const cwd = SessionCwd.get(ctx.sessionID)
        const search = params.path ?? cwd
        return { reads: [path.isAbsolute(search) ? search : path.resolve(cwd, search)] }
      },
      execute: (params: { pattern: string; path?: string; cursor?: number; limit?: number }, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const ins = yield* InstanceState.context
          yield* ctx.ask({
            permission: "glob",
            patterns: [params.pattern],
            always: ["*"],
            metadata: {
              pattern: params.pattern,
              path: params.path,
            },
          })

          let search = params.path ?? SessionCwd.get(ctx.sessionID)
          search = path.isAbsolute(search) ? search : path.resolve(SessionCwd.get(ctx.sessionID), search)
          search = AppFileSystem.resolve(search)
          const info = yield* fs.stat(search).pipe(Effect.catch(() => Effect.succeed(undefined)))
          if (info?.type === "File") {
            throw new Error(`glob path must be a directory: ${search}`)
          }
          yield* assertExternalDirectoryEffect(ctx, search, { kind: "directory" })

          const cursor = params.cursor ?? 0
          const limit = params.limit ?? 100
          const files = yield* rg.files({ cwd: search, glob: [params.pattern], signal: ctx.abort }).pipe(
            Stream.mapEffect((file) =>
              Effect.gen(function* () {
                const full = path.resolve(search, file)
                const info = yield* fs.stat(full).pipe(Effect.catch(() => Effect.succeed(undefined)))
                const mtime =
                  info?.mtime.pipe(
                    Option.map((date) => date.getTime()),
                    Option.getOrElse(() => 0),
                  ) ?? 0
                return { path: full, mtime }
              }),
            ),
            Stream.runCollect,
            Effect.map((chunk) => [...chunk]),
          )

          files.sort(
            (a, b) => b.mtime - a.mtime || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0),
          )
          const page = files.slice(cursor, cursor + limit)
          const next = files.length > cursor + page.length ? cursor + page.length : undefined
          const truncated = next !== undefined

          const output = []
          if (page.length === 0) output.push("No files found")
          if (page.length > 0) {
            output.push(...page.map((file) => file.path))
            if (truncated) {
              output.push("")
              output.push(
                `(Results are truncated: showing ${cursor + 1}-${cursor + page.length}. Continue at cursor ${next}.)`,
              )
            }
          }

          const items = page.map((file) => ({ path: file.path, kind: "path" }))
          return {
            title: path.relative(ins.worktree, search),
            metadata: {
              count: page.length,
              paths: page.map((file) => file.path),
              path: AppFileSystem.resolve(search),
              file: fileResult({
                operation: "glob",
                path: AppFileSystem.resolve(search),
                kind: "search",
                range: page.length
                  ? { unit: "result", start: cursor, end: cursor + page.length - 1 }
                  : null,
                page: {
                  unit: "result",
                  cursor,
                  next_cursor: next ?? null,
                  has_more: next !== undefined,
                  returned: page.length,
                  total: files.length,
                },
                truncation_reason: next !== undefined ? "result_limit" : null,
                items,
              }),
              page: {
                cursor,
                next_cursor: next,
                has_more: next !== undefined,
                result_count: page.length,
                total: files.length,
              },
              truncated,
            },
            output: output.join("\n"),
          }
        }).pipe(Effect.orDie),
    }
  }),
)
