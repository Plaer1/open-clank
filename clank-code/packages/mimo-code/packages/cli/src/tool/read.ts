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
import { ModelCapability, Provider } from "@/provider"
import { shrinkAttachment } from "@/provider/image"
import { builtinSkillRoot } from "@/skill/builtin/extract"
import {
  classifyAttachment,
  fitsMediaBase64,
  isReadAudioMime,
  isReadImageMime,
  isReadPdfMime,
  isReadVideoMime,
  looksLikeMediaMime,
  oversizedAttachmentNotice,
  oversizedMediaNotice,
  READ_AUDIO_MIMES,
  READ_IMAGE_MIMES,
  READ_VIDEO_MIMES,
  readMimeAllowlist,
  sniffAttachmentMime,
} from "@/util/media"
import { fileResult, type FileResult } from "./file-contract"
import { markReadView } from "../session/tool-view-read"
import { toolViewProducerCurrent } from "../session/tool-view-producer-admission"

const DEFAULT_READ_LIMIT = 2000

// Format names for the finite read allowlists — what describeMedia advertises
// and what the attachment branch will actually attach.
const IMAGE_FORMAT_NAMES: Record<string, string> = {
  "image/jpeg": "jpeg",
  "image/png": "png",
  "image/webp": "webp",
  "image/gif": "gif",
}
const AUDIO_FORMAT_NAMES: Record<string, string> = {
  "audio/wav": "wav",
  "audio/x-wav": "wav",
  "audio/mp3": "mp3",
  "audio/mpeg": "mp3",
}
const VIDEO_FORMAT_NAMES: Record<string, string> = {
  "video/mp4": "mp4",
}

function mediaFormatNames(kind: "image" | "audio" | "video") {
  const names = kind === "image" ? IMAGE_FORMAT_NAMES : kind === "audio" ? AUDIO_FORMAT_NAMES : VIDEO_FORMAT_NAMES
  const mimes = kind === "image" ? READ_IMAGE_MIMES : kind === "audio" ? READ_AUDIO_MIMES : READ_VIDEO_MIMES
  return [...new Set([...mimes].map((mime) => names[mime] ?? mime))]
}

/**
 * Model-dependent media paragraph for the tool description. Appended by the
 * tool registry so a text-only model is never told it can attach media.
 * Formats listed are the finite read allowlist, not the full adapter surface.
 * PDF stays in the static read.txt line with a model-support caveat.
 */
export function describeMedia(model: Provider.Model | undefined) {
  if (!model) return undefined
  const image = model.capabilities.input.image ? `image (${mediaFormatNames("image").join(", ")})` : undefined
  const audio = model.capabilities.input.audio ? `audio (${mediaFormatNames("audio").join(", ")})` : undefined
  const video = model.capabilities.input.video ? `video (${mediaFormatNames("video").join(", ")})` : undefined
  const kinds = [image, audio, video].filter((kind): kind is string => kind !== undefined)
  if (kinds.length === 0) return undefined
  return [
    `- You can read and understand ${kinds.join(" and ")} files directly: this tool returns them as file attachments, and you can then describe, transcribe, summarize, or answer questions about their content yourself, without an external transcription or vision service.`,
    `- Media is inlined when small enough (about 37 MB on disk, 50 MB once base64-encoded); otherwise the tool explains why the file was not read. Read the file first, then analyze its content in the same turn.`,
  ].join("\n")
}
const MAX_LINE_LENGTH = 2000
const MAX_LINE_SUFFIX = `... (line truncated to ${MAX_LINE_LENGTH} chars)`
const MAX_BYTES = 50 * 1024
const MAX_BYTES_LABEL = `${MAX_BYTES / 1024} KB`
const SAMPLE_BYTES = 4096

const parameters = z.object({
  file_path: z.string().describe("Path to the file or directory"),
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
  truncation_reason?: "byte_limit" | "line_limit" | "character_limit"
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
      // Finite allowlist only. mime-types maps code extensions such as .ts/.mts
      // to video/mp2t; prefix matching must not send source files down the
      // media path. Outside the list: binary media-like files still refuse with
      // a convert hint; text-like payloads fall through to the text reader.
      const inImage = isReadImageMime(mime)
      const inPdf = isReadPdfMime(mime)
      const inAudio = isReadAudioMime(mime)
      const inVideo = isReadVideoMime(mime)
      const inMedia = inImage || inPdf || inAudio || inVideo

      if (!inMedia && looksLikeMediaMime(mime) && isBinaryFile(filepath, sample)) {
        const allowlist = readMimeAllowlist(mime)
        const kind = mime.startsWith("audio/") ? "audio" : mime.startsWith("video/") ? "video" : mime === "application/pdf" ? "PDF" : "image"
        const accepted = allowlist ? [...allowlist].join(", ") : ""
        const hintExt = kind === "audio" ? "wav" : kind === "video" ? "mp4" : kind === "PDF" ? "pdf" : "png"
        const warning = [
          `Cannot attach ${kind} "${path.basename(filepath)}" (${mime}) — read only attaches ${accepted || "a finite media MIME list"}, so the file was not read.`,
          `Convert it first (e.g. ffmpeg -i "${filepath}" /tmp/example.${hintExt}) and read the converted file.`,
        ].join("\n")
        return {
          title,
          output: warning,
          metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
        }
      }

      // Size gate on stat, before any bytes are read (see classifyAttachment):
      // a rejected PDF or image is never read, an oversized image within the
      // source ceiling is read and recompressed below. Either way nothing over
      // the limit becomes base64 or reaches the session DB. Audio and video are
      // not gated here: the provider bounds their ENCODED size, which the media
      // branch below checks with fitsMediaBase64.
      const verdict = inImage || inPdf ? classifyAttachment(mime, Number(stat.size)) : "fits"
      if (verdict === "reject") {
        const warning = oversizedAttachmentNotice({
          label: `"${path.basename(filepath)}" (${mime})`,
          size: Number(stat.size),
          hint: "It was not read.",
        })
        return {
          title,
          output: warning,
          metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
        }
      }
      // The active model is carried on ctx.extra.model (set on both the
      // agent-call path and the @file resolution path, which passes messages: []).
      // Fall back to resolving the last user message's model for any caller that
      // doesn't populate extra.
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

      if (inPdf && !(model?.capabilities.input.pdf ?? false)) {
        // Same shape as the image gate below: the bytes are never read, so a
        // PDF the model cannot take never becomes base64 and never reaches the
        // session DB. The model is pointed at the bundled pdf skill instead.
        const warning = [
          `Cannot attach PDF "${path.basename(filepath)}" — the current model has no PDF input support, so the file was not read.`,
          `To work with its contents, extract text (or render pages to images) with the bundled pdf skill: read ${path.join(builtinSkillRoot(), "pdf-official", "SKILL.md")} and follow it.`,
        ].join("\n")
        return {
          title,
          output: warning,
          metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
        }
      }
      if (inImage) {
        const supportsImage = model?.capabilities.input.image ?? false
        if (!supportsImage) {
          const preferred = yield* provider.getVisionModel(model).pipe(Effect.orElseSucceed(() => undefined))
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
        const bytes = Buffer.from(yield* fs.readFile(filepath))
        const fitted = verdict === "shrink" ? shrinkAttachment(mime, bytes) : { mime, base64: bytes.toString("base64") }
        if (!fitted) {
          const warning = oversizedAttachmentNotice({
            label: `"${path.basename(filepath)}" (${mime})`,
            size: bytes.byteLength,
            compressed: true,
            hint: "It was not attached.",
          })
          return {
            title,
            output: warning,
            metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
          }
        }
        const output =
          verdict === "shrink"
            ? `Image read successfully (recompressed from ${bytes.byteLength} bytes to ${fitted.mime} to fit the attachment limit)`
            : "Image read successfully"
        return {
          title,
          output,
          metadata: {
            preview: output,
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
              mime: fitted.mime,
              url: `data:${fitted.mime};base64,${fitted.base64}`,
            },
          ],
        }
      }

      if (inPdf) {
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

      if (inAudio || inVideo) {
        // Audio and video are opaque to the read tool: the bytes go to the model
        // as an inline `data:` attachment, or nowhere. Gate on the model first,
        // then on the encoded size, so a file the model cannot take is never
        // read and an oversized one never becomes base64.
        const kind = inAudio ? "audio" : "video"
        const supported = model?.capabilities.input[kind] ?? false
        if (!supported) {
          const warning = [
            `Cannot attach ${kind} "${path.basename(filepath)}" — the current model has no ${kind} input support, so the file was not read.`,
            `Ask the user to switch to a model with ${kind} input, or use a shell tool (e.g. ffprobe) to inspect its metadata instead.`,
          ].join("\n")
          return {
            title,
            output: warning,
            metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
          }
        }
        // The model may take the media kind while the finite read allowlist is still
        // the outer gate. After that, the adapter declaration can narrow further
        // (rare); refuse rather than attaching bytes tool-attachment.ts would
        // later replace with a placeholder.
        const declared = model ? ModelCapability.modelDeclaration(model, kind) : undefined
        if (declared?.support === "supported" && declared.mimeTypes !== "any" && !declared.mimeTypes.includes(mime)) {
          const warning = [
            `Cannot attach ${kind} "${path.basename(filepath)}" (${mime}) — the current provider only accepts ${declared.mimeTypes.join(", ")}, so the file was not read.`,
            `Convert it first (e.g. ffmpeg -i "${filepath}" /tmp/example.${kind === "audio" ? "wav" : "mp4"}) and read the converted file.`,
          ].join("\n")
          return {
            title,
            output: warning,
            metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
          }
        }
        if (!fitsMediaBase64(Number(stat.size))) {
          const warning = oversizedMediaNotice({
            label: `"${path.basename(filepath)}" (${mime})`,
            size: Number(stat.size),
            hint: "It was not read.",
          })
          return {
            title,
            output: warning,
            metadata: { preview: warning, truncated: false, loaded: loaded.map((item) => item.filepath) },
          }
        }
        const bytes = yield* fs.readFile(filepath)
        const output = `${kind === "audio" ? "Audio" : "Video"} read successfully and attached for the model to analyze`
        return {
          title,
          output,
          metadata: { preview: output, truncated: false, loaded: loaded.map((item) => item.filepath) },
          attachments: [
            {
              type: "file" as const,
              mime,
              filename: path.basename(filepath),
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
      const hasMore = file.more || file.cut
      const truncated = hasMore || file.shortened
      if (file.cut) {
        output += `\n\n(Output capped at ${MAX_BYTES_LABEL}. Showing lines ${file.offset}-${last}. Use offset=${next} to continue.)`
      } else if (file.more) {
        output += `\n\n(Showing lines ${file.offset}-${last} of ${file.count}. Use offset=${next} to continue.)`
      } else {
        output += `\n\n(End of file - total ${file.count} lines)`
      }
      if (file.shortened) output += "\n\n(Some displayed lines were shortened at 2000 UTF-16 code units; the missing characters cannot be recovered by changing the line offset.)"
      output += "\n</content>"

      yield* warm(filepath)

      if (loaded.length > 0) {
        output += `\n\n<system-reminder>\n${loaded.map((item) => item.content).join("\n\n")}\n</system-reminder>`
      }

      const result = {
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
          truncation_reason: file.cut ? "byte_limit" : file.more ? "line_limit" : file.shortened ? "character_limit" : undefined,
          file: fileResult({
            operation: "read",
            path: filepath,
            kind: "text",
            range: file.raw.length ? { unit: "line", start: file.offset, end: last } : null,
            page: {
              unit: "line",
              cursor: file.offset,
              next_cursor: hasMore ? next : null,
              has_more: hasMore,
              returned: file.raw.length,
              total: file.count,
            },
            bytes_considered: snapshot.size,
            lines_considered: file.raw.length,
            truncation_reason: file.cut ? "byte_limit" : file.more ? "line_limit" : file.shortened ? "character_limit" : null,
            encoding: snapshot.encoding,
            newline: snapshot.newline === "\n" ? "lf" : snapshot.newline === "\r\n" ? "crlf" : "cr",
            media_type: mime,
            fingerprint: snapshot.fingerprint,
            diagnostics: [
              ...(hasMore ? [{ code: "truncated", message: `Continue at line cursor ${next}.` }] : []),
              ...(file.shortened ? [{ code: "character_limit", message: "Some displayed lines were shortened; changing the line offset cannot recover their missing characters." }] : []),
            ],
          }),
          page: {
            cursor: file.offset,
            next_cursor: hasMore ? next : undefined,
            has_more: hasMore,
            total: file.count,
          },
        } as ReadMetadata,
      }
      if (toolViewProducerCurrent(ctx,"read-unnumbered-v1") || toolViewProducerCurrent(ctx,"npm-lockfile-versions-v1") || toolViewProducerCurrent(ctx,"read-json-whitespace-v1") || toolViewProducerCurrent(ctx,"read-repeated-lines-v1"))
        return markReadView(result,ctx,{snapshotText:snapshot.text,raw:file.raw,offset:file.offset,count:file.count,cut:file.cut,more:file.more,loaded:result.metadata.loaded})
      return result
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
  let shortened = false
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
    if (textLine.length > MAX_LINE_LENGTH) shortened = true
    bytes += size
  }

  return { raw, count, cut, more, shortened, offset: opts.offset }
}
