import path from "path"
import z from "zod"
import { Effect, Option } from "effect"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Ripgrep } from "../file/ripgrep"
import { assertExternalDirectoryEffect } from "./external-directory"
import { SessionCwd } from "./session-cwd"
import DESCRIPTION from "./grep.txt"
import * as Tool from "./tool"
import { fileResult } from "./file-contract"

const MAX_LINE_LENGTH = 2000
const Parameters = z.object({
  pattern: z.string().describe("The pattern to search for in file contents"),
  path: z.string().optional().describe("The directory to search in. Defaults to the current working directory."),
  include: z.string().optional().describe('File pattern to include in the search (e.g. "*.js", "*.{ts,tsx}")'),
  mode: z.enum(["regex", "literal"]).optional().describe("Interpret pattern as regex (default) or literal text"),
  cursor: z.number().int().nonnegative().optional(),
  limit: z.number().int().min(1).max(200).optional(),
})

export const GrepTool = Tool.define(
  "grep",
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service
    const rg = yield* Ripgrep.Service

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      resources: (params: z.infer<typeof Parameters>, ctx: Tool.Context) => {
        const cwd = SessionCwd.get(ctx.sessionID)
        const search = params.path ?? cwd
        return { reads: [path.isAbsolute(search) ? search : path.join(cwd, search)] }
      },
      execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const cursor = params.cursor ?? 0
          const limit = params.limit ?? 100
          const mode = params.mode ?? "regex"
          const empty = (search?: string) => {
            const canonical = AppFileSystem.resolve(
              search ?? path.resolve(SessionCwd.get(ctx.sessionID), params.path ?? "."),
            )
            return {
              title: params.pattern,
              metadata: {
                matches: 0,
                items: [] as Array<{ path: string; line: number; text: string }>,
                search_mode: mode,
                path: canonical,
                file: fileResult({
                  operation: "grep",
                  path: canonical,
                  kind: "search",
                  page: {
                    unit: "result",
                    cursor,
                    next_cursor: null,
                    has_more: false,
                    returned: 0,
                    total: 0,
                  },
                  search_mode: mode,
                }),
                page: {
                  cursor,
                  next_cursor: undefined as number | undefined,
                  has_more: false,
                  total: 0,
                },
                truncated: false,
              },
              output: "No files found",
            }
          }
          if (!params.pattern) {
            throw new Error("pattern is required")
          }

          yield* ctx.ask({
            permission: "grep",
            patterns: [params.pattern],
            always: ["*"],
            metadata: {
              pattern: params.pattern,
              path: params.path,
              include: params.include,
            },
          })

          const effectiveCwd = SessionCwd.get(ctx.sessionID)
          const search = AppFileSystem.resolve(
            path.isAbsolute(params.path ?? effectiveCwd)
              ? (params.path ?? effectiveCwd)
              : path.join(effectiveCwd, params.path ?? "."),
          )
          const info = yield* fs.stat(search).pipe(Effect.catch(() => Effect.succeed(undefined)))
          const cwd = info?.type === "Directory" ? search : path.dirname(search)
          const file = info?.type === "Directory" ? undefined : [path.relative(cwd, search)]
          yield* assertExternalDirectoryEffect(ctx, search, {
            kind: info?.type === "Directory" ? "directory" : "file",
          })

          const result = yield* rg.search({
            cwd,
            pattern:
              mode === "literal"
                ? params.pattern.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
                : params.pattern,
            glob: params.include ? [params.include] : undefined,
            file,
            signal: ctx.abort,
          })
          if (result.items.length === 0) return empty(search)

          const rows = result.items.map((item) => ({
            path: AppFileSystem.resolve(
              path.isAbsolute(item.path.text) ? item.path.text : path.join(cwd, item.path.text),
            ),
            line: item.line_number,
            text: item.lines.text,
          }))
          const times = new Map(
            (yield* Effect.forEach(
              [...new Set(rows.map((row) => row.path))],
              Effect.fnUntraced(function* (file) {
                const info = yield* fs.stat(file).pipe(Effect.catch(() => Effect.succeed(undefined)))
                if (!info || info.type === "Directory") return undefined
                return [
                  file,
                  info.mtime.pipe(
                    Option.map((time) => time.getTime()),
                    Option.getOrElse(() => 0),
                  ) ?? 0,
                ] as const
              }),
              { concurrency: 16 },
            )).filter((entry): entry is readonly [string, number] => Boolean(entry)),
          )
          const matches = rows.flatMap((row) => {
            const mtime = times.get(row.path)
            if (mtime === undefined) return []
            return [{ ...row, mtime }]
          })

          matches.sort((a, b) => {
            const pathOrder = a.path < b.path ? -1 : a.path > b.path ? 1 : 0
            const textOrder = a.text < b.text ? -1 : a.text > b.text ? 1 : 0
            return b.mtime - a.mtime || pathOrder || a.line - b.line || textOrder
          })

          const final = matches.slice(cursor, cursor + limit)
          const next = cursor + final.length < matches.length ? cursor + final.length : undefined
          const truncated = next !== undefined

          const total = matches.length
          if (final.length === 0) {
            return {
              title: params.pattern,
              metadata: {
                matches: total,
                items: [],
                search_mode: mode,
                path: search,
                file: fileResult({
                  operation: "grep",
                  path: search,
                  kind: "search",
                  page: {
                    unit: "result",
                    cursor,
                    next_cursor: null,
                    has_more: false,
                    returned: 0,
                    total,
                  },
                  search_mode: mode,
                }),
                page: { cursor, next_cursor: undefined, has_more: false, total },
                truncated: false,
              },
              output: `No matches on page starting at cursor ${cursor}`,
            }
          }
          const output = [
            `Found ${total} matches${truncated || cursor ? ` (showing ${cursor + 1}-${cursor + final.length})` : ""}`,
          ]

          let current = ""
          for (const match of final) {
            if (current !== match.path) {
              if (current !== "") output.push("")
              current = match.path
              output.push(`${match.path}:`)
            }
            const text =
              match.text.length > MAX_LINE_LENGTH ? match.text.substring(0, MAX_LINE_LENGTH) + "..." : match.text
            output.push(`  Line ${match.line}: ${text}`)
          }

          if (truncated) {
            output.push("")
            output.push(
              `(Results truncated: showing ${cursor + 1}-${cursor + final.length} of ${total} matches. Continue at cursor ${next}.)`,
            )
          }

          if (result.partial) {
            output.push("")
            output.push("(Some paths were inaccessible and skipped)")
          }

          return {
            title: params.pattern,
            metadata: {
              matches: total,
              items: final.map(({ mtime: _, ...match }) => match),
              search_mode: mode,
              path: search,
              file: fileResult({
                operation: "grep",
                path: search,
                kind: "search",
                range: final.length
                  ? { unit: "result", start: cursor, end: cursor + final.length - 1 }
                  : null,
                page: {
                  unit: "result",
                  cursor,
                  next_cursor: next ?? null,
                  has_more: next !== undefined,
                  returned: final.length,
                  total,
                },
                truncation_reason: next !== undefined ? "result_limit" : null,
                search_mode: mode,
                items: final.map(({ mtime: _, ...match }) => match),
                diagnostics: result.partial
                  ? [{ code: "inaccessible_paths", message: "Some paths were inaccessible and skipped." }]
                  : [],
              }),
              page: {
                cursor,
                next_cursor: next,
                has_more: next !== undefined,
                total,
              },
              truncated,
            },
            output: output.join("\n"),
          }
        }).pipe(Effect.orDie),
    }
  }),
)
