import z from "zod"
import { Effect, Option, Scope } from "effect"
import * as path from "path"
import * as Tool from "./tool"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { LSP } from "../lsp"
import DESCRIPTION from "./read.txt"
import { Instance } from "../project/instance"
import { assertExternalDirectoryEffect } from "./external-directory"
import { SessionCwd } from "./session-cwd"
import { Instruction } from "../session/instruction"
import { Provider } from "@/provider"
import { isImageAttachment, isPdfAttachment, sniffAttachmentMime } from "@/util/media"
import { fileResult, type FileResult } from "./file-contract"

const DEFAULT_READ_LIMIT = 2000
const MAX_LINE_LENGTH = 2000
const MAX_LINE_SUFFIX = `... (line truncated to ${MAX_LINE_LENGTH} chars)`
const MAX_BYTES = 50 * 1024
const MAX_BYTES_LABEL = `${MAX_BYTES / 1024} KB`
const SAMPLE_BYTES = 4096

const parameters = z.object({
  file_path: z.string().describe("The absolute path to the file or directory to read"),
  offset: z.coerce.number().describe("The line number to start reading from (1-indexed)").optional(),
  limit: z.coerce.number().describe("The maximum number of lines to read (defaults to 2000)").optional(),
})

type ReadMetadata = {
  preview: string
  truncated: boolean
  loaded: string[]
  fingerprint?: string
  encoding?: string
  newline?: string
  path: string
  media_type: string
  bytes_considered?: number
  lines_considered?: number
  truncation_reason?: "byte_limit" | "line_limit"
  file: FileResult
  page: {
    cursor: number
    next_cursor?: number
    has_more: boolean
    total: number
  }
}

export const ReadTool = Tool.define(
  "read",
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service
    const instruction = yield* Instruction.Service
    const lsp = yield* LSP.Service
    const provider = yield* Provider.Service
    const scope = yield* Scope.Scope

    const miss = Effect.fn("ReadTool.miss")(function* (filepath: string) {
      const dir = path.dirname(filepath)
      const base = path.basename(filepath)
      const items = yield* fs.readDirectory(dir).pipe(
        Effect.map((items) =>
          items
            .filter(
              (item) =>
                item.toLowerCase().includes(base.toLowerCase()) || base.toLowerCase().includes(item.toLowerCase()),
            )
            .map((item) => path.join(dir, item))
            .slice(0, 3),
        ),
        Effect.catch(() => Effect.succeed([] as string[])),
      )

      if (items.length > 0) {
        return yield* Effect.fail(
          new Error(`File not found: ${filepath}\n\nDid you mean one of these?\n${items.join("\n")}`),
        )
      }

      return yield* Effect.fail(new Error(`File not found: ${filepath}`))
    })

    const list = Effect.fn("ReadTool.list")(function* (filepath: string) {
      const items = yield* fs.readDirectoryEntries(filepath)
      return yield* Effect.forEach(
        items,
        Effect.fnUntraced(function* (item) {
          if (item.type === "directory") return item.name + "/"
          if (item.type !== "symlink") return item.name

          const target = yield* fs.stat(path.join(filepath, item.name)).pipe(Effect.catch(() => Effect.void))
          if (target?.type === "Directory") return item.name + "/"
          return item.name
        }),
        { concurrency: "unbounded" },
      ).pipe(Effect.map((items: string[]) => items.sort((a, b) => a.localeCompare(b))))
    })

    const warm = Effect.fn("ReadTool.warm")(function* (filepath: string) {
      yield* lsp.touchFile(filepath, false).pipe(Effect.ignore, Effect.forkIn(scope))
    })

    const readSample = Effect.fn("ReadTool.readSample")(function* (
      filepath: string,
      fileSize: number,
      sampleSize: number,
    ) {
      if (fileSize === 0) return new Uint8Array()

      return yield* Effect.scoped(
        Effect.gen(function* () {
          const file = yield* fs.open(filepath, { flag: "r" })
          return Option.getOrElse(yield* file.readAlloc(Math.min(sampleSize, fileSize)), () => new Uint8Array())
        }),
      )
    })

    const isBinaryFile = (filepath: string, bytes: Uint8Array) => {
      const ext = path.extname(filepath).toLowerCase()
      switch (ext) {
        case ".zip":
        case ".tar":
        case ".gz":
        case ".exe":
        case ".dll":
        case ".so":
        case ".class":
        case ".jar":
        case ".war":
        case ".7z":
        case ".doc":
        case ".docx":
        case ".xls":
        case ".xlsx":
        case ".ppt":
        case ".pptx":
        case ".odt":
        case ".ods":
        case ".odp":
        case ".bin":
        case ".dat":
        case ".obj":
        case ".o":
        case ".a":
        case ".lib":
        case ".wasm":
        case ".pyc":
        case ".pyo":
          return true
      }

      if (bytes.length === 0) return false

      let nonPrintableCount = 0
      for (let i = 0; i < bytes.length; i++) {
        if (bytes[i] === 0) return true
        if (bytes[i] < 9 || (bytes[i] > 13 && bytes[i] < 32)) {
          nonPrintableCount++
        }
      }

      return nonPrintableCount / bytes.length > 0.3
    }

    const run = Effect.fn("ReadTool.execute")(function* (params: z.infer<typeof parameters>, ctx: Tool.Context) {
      if (params.offset !== undefined && params.offset < 1) {
        return yield* Effect.fail(new Error("offset must be greater than or equal to 1"))
      }

      let requested = params.file_path
      if (!path.isAbsolute(requested)) {
        requested = path.resolve(SessionCwd.get(ctx.sessionID), requested)
      }
      if (process.platform === "win32") {
        requested = AppFileSystem.normalizePath(requested)
      }
      const filepath = AppFileSystem.resolve(requested)
      const title = path.relative(Instance.worktree, filepath)

      const stat = yield* fs.stat(filepath).pipe(
        Effect.catchIf(
          (err) => "reason" in err && err.reason._tag === "NotFound",
          () => Effect.succeed(undefined),
        ),
      )

      yield* assertExternalDirectoryEffect(ctx, filepath, {
        bypass: Boolean(ctx.extra?.["bypassCwdCheck"]),
        kind: stat?.type === "Directory" ? "directory" : "file",
      })

      yield* ctx.ask({
        permission: "read",
        patterns: [filepath],
        always: ["*"],
        metadata: {},
      })

      if (!stat) return yield* miss(filepath)

      if (stat.type === "Directory") {
        const items = yield* list(filepath)
        const limit = params.limit ?? DEFAULT_READ_LIMIT
        const offset = params.offset ?? 1
        const start = offset - 1
        const sliced = items.slice(start, start + limit)
        const truncated = start + sliced.length < items.length
        const contractItems = sliced.map((item) => ({
          path: AppFileSystem.resolve(path.join(filepath, item.replace(/\/$/, ""))),
          kind: item.endsWith("/") ? "directory" : "file",
        }))

        return {
          title,
          output: [
            `<path>${filepath}</path>`,
            `<type>directory</type>`,
            `<entries>`,
            sliced.join("\n"),
            truncated
              ? `\n(Showing ${sliced.length} of ${items.length} entries. Use 'offset' parameter to read beyond entry ${offset + sliced.length})`
              : `\n(${items.length} entries)`,
            `</entries>`,
          ].join("\n"),
          metadata: {
            preview: sliced.slice(0, 20).join("\n"),
            truncated,
            loaded: [] as string[],
            fingerprint: undefined as string | undefined,
            encoding: undefined as string | undefined,
            newline: undefined as string | undefined,
            path: filepath,
            media_type: "inode/directory",
            file: fileResult({
              operation: "list",
              path: filepath,
              kind: "directory",
              range: sliced.length
                ? { unit: "entry", start: offset, end: offset + sliced.length - 1 }
                : null,
              page: {
                unit: "entry",
                cursor: offset,
                next_cursor: truncated ? offset + sliced.length : null,
                has_more: truncated,
                returned: sliced.length,
                total: items.length,
              },
              truncation_reason: truncated ? "result_limit" : null,
              media_type: "inode/directory",
              items: contractItems,
            }),
            page: {
              cursor: offset,
              next_cursor: truncated ? offset + sliced.length : undefined,
              has_more: truncated,
              total: items.length,
            },
          } as ReadMetadata,
        }
      }

      const loaded = yield* instruction.resolve(ctx.messages, filepath, ctx.messageID)
      const sample = yield* readSample(filepath, Number(stat.size), SAMPLE_BYTES)

      const mime = sniffAttachmentMime(sample, AppFileSystem.mimeType(filepath))
      if (isImageAttachment(mime)) {
        // The active model is carried on ctx.extra.model (set on both the
        // agent-call path and the @file resolution path, which passes messages: []).
        // Fall back to resolving the last user message's model for any caller that
        // doesn't populate extra. Mirrors tool/websearch/index.ts.
        const extraModel = (ctx.extra as { model?: Provider.Model } | undefined)?.model
        const messageModelRef = extraModel
          ? undefined
          : [...ctx.messages]
              .reverse()
              .map((m) => m.info)
              .find((i): i is Extract<typeof i, { role: "user" }> => i.role === "user")?.model
        const model =
          extraModel ??
          (messageModelRef
            ? yield* provider
                .getModel(messageModelRef.providerID, messageModelRef.modelID)
                .pipe(Effect.catchDefect(() => Effect.succeed(undefined)))
            : undefined)
        const supportsImage = model?.capabilities.input.image ?? false
        if (!supportsImage) {
          const preferred = yield* provider.getVisionModel().pipe(Effect.orElseSucceed(() => undefined))
          const preferredRef = preferred ? `${preferred.providerID}/${preferred.id}` : undefined
          const dispatch = preferredRef
            ? `dispatch a vision-capable subagent: actor run <type> "<desc>" "analyze the image at ${filepath}" --model ${preferredRef} (run \`actor models --vision\` for the full list)`
            : `no vision-capable model is configured — ask the user to configure one or use an OCR tool`
          const warning = [
            `Cannot read image "${path.basename(filepath)}" — the current model has no vision support, so its visual content is unavailable.`,
            `If you need to understand the image visually, ${dispatch}.`,
            `If you instead need the file's raw binary structure, use a shell tool such as \`hexdump -C ${filepath}\` — do not use the read tool for that.`,
          ].join("\n")
          return {
            title,
            output: warning,
            metadata: {
              preview: warning,
              truncated: false,
              loaded: [] as string[],
              fingerprint: undefined as string | undefined,
              encoding: undefined as string | undefined,
              newline: undefined as string | undefined,
              path: filepath,
              media_type: mime,
              file: fileResult({
                operation: "read",
                path: filepath,
                kind: "image",
                range: sample.byteLength ? { unit: "byte", start: 0, end: sample.byteLength - 1 } : null,
                page: {
                  unit: "byte",
                  cursor: 0,
                  next_cursor: null,
                  has_more: false,
                  returned: 0,
                  total: Number(stat.size),
                },
                bytes_considered: sample.byteLength,
                media_type: mime,
                diagnostics: [{ code: "vision_unavailable", message: warning }],
              }),
              page: {
                cursor: 0,
                next_cursor: undefined,
                has_more: false,
                total: Number(stat.size),
              },
            } as ReadMetadata,
          }
        }
        const bytes = yield* fs.readFile(filepath)
        return {
          title,
          output: "Image read successfully",
          metadata: {
            preview: "Image read successfully",
            truncated: false,
            loaded: loaded.map((item) => item.filepath),
            fingerprint: AppFileSystem.fingerprintBytes(bytes),
            encoding: undefined as string | undefined,
            newline: undefined as string | undefined,
            path: filepath,
            media_type: mime,
            bytes_considered: bytes.byteLength,
            file: fileResult({
              operation: "read",
              path: filepath,
              kind: "image",
              range: bytes.byteLength ? { unit: "byte", start: 0, end: bytes.byteLength - 1 } : null,
              page: {
                unit: "byte",
                cursor: 0,
                next_cursor: null,
                has_more: false,
                returned: bytes.byteLength,
                total: bytes.byteLength,
              },
              bytes_considered: bytes.byteLength,
              media_type: mime,
              fingerprint: AppFileSystem.fingerprintBytes(bytes),
            }),
            page: {
              cursor: 0,
              next_cursor: undefined,
              has_more: false,
              total: Number(stat.size),
            },
          } as ReadMetadata,
          attachments: [
            {
              type: "file" as const,
              mime,
              url: `data:${mime};base64,${Buffer.from(bytes).toString("base64")}`,
            },
          ],
        }
      }

      if (isPdfAttachment(mime)) {
        const bytes = yield* fs.readFile(filepath)
        return {
          title,
          output: "PDF read successfully",
          metadata: {
            preview: "PDF read successfully",
            truncated: false,
            loaded: loaded.map((item) => item.filepath),
            fingerprint: AppFileSystem.fingerprintBytes(bytes),
            encoding: undefined as string | undefined,
            newline: undefined as string | undefined,
            path: filepath,
            media_type: mime,
            bytes_considered: bytes.byteLength,
            file: fileResult({
              operation: "read",
              path: filepath,
              kind: "pdf",
              range: bytes.byteLength ? { unit: "byte", start: 0, end: bytes.byteLength - 1 } : null,
              page: {
                unit: "byte",
                cursor: 0,
                next_cursor: null,
                has_more: false,
                returned: bytes.byteLength,
                total: bytes.byteLength,
              },
              bytes_considered: bytes.byteLength,
              media_type: mime,
              fingerprint: AppFileSystem.fingerprintBytes(bytes),
            }),
            page: {
              cursor: 0,
              next_cursor: undefined,
              has_more: false,
              total: Number(stat.size),
            },
          } as ReadMetadata,
          attachments: [
            {
              type: "file" as const,
              mime,
              url: `data:${mime};base64,${Buffer.from(bytes).toString("base64")}`,
            },
          ],
        }
      }

      if (isBinaryFile(filepath, sample)) {
        const warning = `Cannot read binary file: ${filepath}`
        return {
          title,
          output: warning,
          metadata: {
            preview: warning,
            truncated: false,
            loaded: loaded.map((item) => item.filepath),
            fingerprint: undefined as string | undefined,
            encoding: undefined as string | undefined,
            newline: undefined as string | undefined,
            path: filepath,
            media_type: mime,
            bytes_considered: sample.byteLength,
            file: fileResult({
              operation: "read",
              path: filepath,
              kind: "binary",
              page: {
                unit: "byte",
                cursor: 0,
                next_cursor: null,
                has_more: false,
                returned: 0,
                total: Number(stat.size),
              },
              bytes_considered: sample.byteLength,
              media_type: mime,
              diagnostics: [
                {
                  code: "unsupported_media",
                  message: "The read tool accepts supported text, image, and PDF files only.",
                },
              ],
            }),
            page: {
              cursor: 0,
              next_cursor: undefined,
              has_more: false,
              total: Number(stat.size),
            },
          } as ReadMetadata,
        }
      }

      const snapshot = yield* fs.readTextSnapshot(filepath)
      const file = lines(snapshot.text, { limit: params.limit ?? DEFAULT_READ_LIMIT, offset: params.offset ?? 1 })
      if (file.count < file.offset && !(file.count === 0 && file.offset === 1)) {
        return yield* Effect.fail(
          new Error(`Offset ${file.offset} is out of range for this file (${file.count} lines)`),
        )
      }

      let output = [`<path>${filepath}</path>`, `<type>file</type>`, "<content>\n"].join("\n")
      output += file.raw.map((line, i) => `${i + file.offset}: ${line}`).join("\n")

      const last = file.offset + file.raw.length - 1
      const next = last + 1
      const truncated = file.more || file.cut
      if (file.cut) {
        output += `\n\n(Output capped at ${MAX_BYTES_LABEL}. Showing lines ${file.offset}-${last}. Use offset=${next} to continue.)`
      } else if (file.more) {
        output += `\n\n(Showing lines ${file.offset}-${last} of ${file.count}. Use offset=${next} to continue.)`
      } else {
        output += `\n\n(End of file - total ${file.count} lines)`
      }
      output += "\n</content>"

      yield* warm(filepath)

      if (loaded.length > 0) {
        output += `\n\n<system-reminder>\n${loaded.map((item) => item.content).join("\n\n")}\n</system-reminder>`
      }

      return {
        title,
        output,
        metadata: {
          preview: file.raw.slice(0, 20).join("\n"),
          truncated,
          loaded: loaded.map((item) => item.filepath),
          fingerprint: snapshot.fingerprint,
          encoding: snapshot.encoding,
          newline: snapshot.newline === "\n" ? "lf" : snapshot.newline === "\r\n" ? "crlf" : "cr",
          path: filepath,
          media_type: mime,
          bytes_considered: snapshot.size,
          lines_considered: file.raw.length,
          truncation_reason: file.cut ? "byte_limit" : file.more ? "line_limit" : undefined,
          file: fileResult({
            operation: "read",
            path: filepath,
            kind: "text",
            range: file.raw.length ? { unit: "line", start: file.offset, end: last } : null,
            page: {
              unit: "line",
              cursor: file.offset,
              next_cursor: truncated ? next : null,
              has_more: truncated,
              returned: file.raw.length,
              total: file.count,
            },
            bytes_considered: snapshot.size,
            lines_considered: file.raw.length,
            truncation_reason: file.cut ? "byte_limit" : file.more ? "line_limit" : null,
            encoding: snapshot.encoding,
            newline: snapshot.newline === "\n" ? "lf" : snapshot.newline === "\r\n" ? "crlf" : "cr",
            media_type: mime,
            fingerprint: snapshot.fingerprint,
            diagnostics: truncated
              ? [{ code: "truncated", message: `Continue at line cursor ${next}.` }]
              : [],
          }),
          page: {
            cursor: file.offset,
            next_cursor: truncated ? next : undefined,
            has_more: truncated,
            total: file.count,
          },
        } as ReadMetadata,
      }
    })

    return {
      description: DESCRIPTION,
      parameters,
      resources: (params: z.infer<typeof parameters>, ctx: Tool.Context) => ({
        reads: [
          path.isAbsolute(params.file_path)
            ? params.file_path
            : path.resolve(SessionCwd.get(ctx.sessionID), params.file_path),
        ],
      }),
      execute: (params: z.infer<typeof parameters>, ctx: Tool.Context) => run(params, ctx).pipe(Effect.orDie),
    }
  }),
)

function lines(text: string, opts: { limit: number; offset: number }) {
  const start = opts.offset - 1
  const raw: string[] = []
  let bytes = 0
  let count = 0
  let cut = false
  let more = false
  const source = text ? text.split(/\r\n|\n|\r/) : []
  if (source.length && source.at(-1) === "") source.pop()
  for (const textLine of source) {
    count += 1
    if (count <= start) continue

    if (raw.length >= opts.limit) {
      more = true
      continue
    }
    if (cut) continue

    const line =
      textLine.length > MAX_LINE_LENGTH ? textLine.substring(0, MAX_LINE_LENGTH) + MAX_LINE_SUFFIX : textLine
    const size = Buffer.byteLength(line, "utf-8") + (raw.length > 0 ? 1 : 0)
    if (bytes + size > MAX_BYTES) {
      cut = true
      more = true
      continue
    }

    raw.push(line)
    bytes += size
  }

  return { raw, count, cut, more, offset: opts.offset }
}
