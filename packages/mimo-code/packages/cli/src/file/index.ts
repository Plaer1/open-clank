import { BusEvent } from "@/bus/bus-event"
import { InstanceState } from "@/effect"

import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Git } from "@/git"
import { Effect, Layer, Context } from "effect"
import * as Stream from "effect/Stream"
import { formatPatch, structuredPatch } from "diff"
import fuzzysort from "fuzzysort"
import ignore from "ignore"
import path from "path"
import { opendir } from "node:fs/promises"
import z from "zod"
import { Global } from "../global"
import { Instance } from "../project/instance"
import { Log } from "../util"
import { Protected } from "./protected"
import { Ripgrep } from "./ripgrep"

export const Info = z
  .object({
    path: z.string(),
    added: z.number().int(),
    removed: z.number().int(),
    status: z.enum(["added", "deleted", "modified"]),
  })
  .meta({
    ref: "File",
  })

export type Info = z.infer<typeof Info>

export const Node = z
  .object({
    name: z.string(),
    path: z.string(),
    absolute: z.string(),
    type: z.enum(["file", "directory"]),
    ignored: z.boolean(),
  })
  .meta({
    ref: "FileNode",
  })
export type Node = z.infer<typeof Node>

export const Content = z
  .object({
    type: z.enum(["text", "binary"]),
    content: z.string(),
    diff: z.string().optional(),
    patch: z
      .object({
        oldFileName: z.string(),
        newFileName: z.string(),
        oldHeader: z.string().optional(),
        newHeader: z.string().optional(),
        hunks: z.array(
          z.object({
            oldStart: z.number(),
            oldLines: z.number(),
            newStart: z.number(),
            newLines: z.number(),
            lines: z.array(z.string()),
          }),
        ),
        index: z.string().optional(),
      })
      .optional(),
    encoding: z.literal("base64").optional(),
    mimeType: z.string().optional(),
  })
  .meta({
    ref: "FileContent",
  })
export type Content = z.infer<typeof Content>

export const Event = {
  Edited: BusEvent.define(
    "file.edited",
    z.object({
      file: z.string(),
    }),
  ),
}

const log = Log.create({ service: "file" })

const binary = new Set([
  "exe",
  "dll",
  "pdb",
  "bin",
  "so",
  "dylib",
  "o",
  "a",
  "lib",
  "wav",
  "mp3",
  "ogg",
  "oga",
  "ogv",
  "ogx",
  "flac",
  "aac",
  "wma",
  "m4a",
  "weba",
  "mp4",
  "avi",
  "mov",
  "wmv",
  "flv",
  "webm",
  "mkv",
  "zip",
  "tar",
  "gz",
  "gzip",
  "bz",
  "bz2",
  "bzip",
  "bzip2",
  "7z",
  "rar",
  "xz",
  "lz",
  "z",
  "pdf",
  "doc",
  "docx",
  "ppt",
  "pptx",
  "xls",
  "xlsx",
  "dmg",
  "iso",
  "img",
  "vmdk",
  "ttf",
  "otf",
  "woff",
  "woff2",
  "eot",
  "sqlite",
  "db",
  "mdb",
  "apk",
  "ipa",
  "aab",
  "xapk",
  "app",
  "pkg",
  "deb",
  "rpm",
  "snap",
  "flatpak",
  "appimage",
  "msi",
  "msp",
  "jar",
  "war",
  "ear",
  "class",
  "kotlin_module",
  "dex",
  "vdex",
  "odex",
  "oat",
  "art",
  "wasm",
  "wat",
  "bc",
  "ll",
  "s",
  "ko",
  "sys",
  "drv",
  "efi",
  "rom",
  "com",
])

const image = new Set([
  "png",
  "jpg",
  "jpeg",
  "gif",
  "bmp",
  "webp",
  "ico",
  "tif",
  "tiff",
  "svg",
  "svgz",
  "avif",
  "apng",
  "jxl",
  "heic",
  "heif",
  "raw",
  "cr2",
  "nef",
  "arw",
  "dng",
  "orf",
  "raf",
  "pef",
  "x3f",
])

const text = new Set([
  "ts",
  "tsx",
  "mts",
  "cts",
  "mtsx",
  "ctsx",
  "js",
  "jsx",
  "mjs",
  "cjs",
  "sh",
  "bash",
  "zsh",
  "fish",
  "ps1",
  "psm1",
  "cmd",
  "bat",
  "json",
  "jsonc",
  "json5",
  "yaml",
  "yml",
  "toml",
  "md",
  "mdx",
  "txt",
  "xml",
  "html",
  "htm",
  "css",
  "scss",
  "sass",
  "less",
  "graphql",
  "gql",
  "sql",
  "ini",
  "cfg",
  "conf",
  "env",
])

const textName = new Set([
  "dockerfile",
  "makefile",
  ".gitignore",
  ".gitattributes",
  ".editorconfig",
  ".npmrc",
  ".nvmrc",
  ".prettierrc",
  ".eslintrc",
])

const mime: Record<string, string> = {
  png: "image/png",
  jpg: "image/jpeg",
  jpeg: "image/jpeg",
  gif: "image/gif",
  bmp: "image/bmp",
  webp: "image/webp",
  ico: "image/x-icon",
  tif: "image/tiff",
  tiff: "image/tiff",
  svg: "image/svg+xml",
  svgz: "image/svg+xml",
  avif: "image/avif",
  apng: "image/apng",
  jxl: "image/jxl",
  heic: "image/heic",
  heif: "image/heif",
}

const ext = (file: string) => path.extname(file).toLowerCase().slice(1)
const name = (file: string) => path.basename(file).toLowerCase()
const isImageByExtension = (file: string) => image.has(ext(file))
const isTextByExtension = (file: string) => text.has(ext(file))
const isTextByName = (file: string) => textName.has(name(file))
const isBinaryByExtension = (file: string) => binary.has(ext(file))
const isImage = (mimeType: string) => mimeType.startsWith("image/")
const getImageMimeType = (file: string) => mime[ext(file)] || "image/" + ext(file)

function shouldEncode(mimeType: string) {
  const type = mimeType.toLowerCase()
  log.debug("shouldEncode", { type })
  if (!type) return false
  if (type.startsWith("text/")) return false
  if (type.includes("charset=")) return false
  const top = type.split("/", 2)[0]
  return ["image", "audio", "video", "font", "model", "multipart"].includes(top)
}

const hidden = (item: string) => {
  const normalized = item.replaceAll("\\", "/").replace(/\/+$/, "")
  return normalized.split("/").some((part) => part.startsWith(".") && part.length > 1)
}

// Stream both levels of the global home view. Opening a directory handle keeps
// large user directories from materializing in JS merely to show suggestions.
async function* homeDirectories(root: string, signal?: AbortSignal): AsyncGenerator<string> {
  const excluded = new Set(["node_modules", "dist", "build", "target", "vendor"])
  const protectedNames = Protected.names()
  const top = await opendir(root)
  for await (const entry of top) {
    signal?.throwIfAborted()
    if (!entry.isDirectory() || entry.name.startsWith(".") || protectedNames.has(entry.name)) continue
    yield entry.name + "/"
    const children = await opendir(path.join(root, entry.name)).catch(() => null)
    if (!children) continue
    for await (const child of children) {
      signal?.throwIfAborted()
      if (child.isDirectory() && !child.name.startsWith(".") && !excluded.has(child.name)) {
        yield entry.name + "/" + child.name + "/"
      }
    }
  }
}

interface SearchInput {
  query: string
  signal?: AbortSignal
  limit?: number
  dirs?: boolean
  type?: "file" | "directory"
}

export interface Interface {
  readonly init: () => Effect.Effect<void>
  readonly status: () => Effect.Effect<Info[]>
  readonly read: (file: string) => Effect.Effect<Content>
  readonly list: (dir?: string) => Effect.Effect<Node[]>
  readonly search: (input: SearchInput) => Effect.Effect<string[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/File") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const appFs = yield* AppFileSystem.Service
    const rg = yield* Ripgrep.Service
    const git = yield* Git.Service
    const gitText = Effect.fnUntraced(function* (args: string[]) {
      return (yield* git.run(args, { cwd: (yield* InstanceState.context).directory })).text()
    })

    const status = Effect.fn("File.status")(function* () {
      const ctx = yield* InstanceState.context
      if (ctx.project.vcs !== "git") return []

      const diffOutput = yield* gitText([
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "diff",
        "--numstat",
        "HEAD",
      ])

      const changed: Info[] = []

      if (diffOutput.trim()) {
        for (const line of diffOutput.trim().split("\n")) {
          const [added, removed, file] = line.split("\t")
          changed.push({
            path: file,
            added: added === "-" ? 0 : parseInt(added, 10),
            removed: removed === "-" ? 0 : parseInt(removed, 10),
            status: "modified",
          })
        }
      }

      const untrackedOutput = yield* gitText([
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "ls-files",
        "--others",
        "--exclude-standard",
      ])

      if (untrackedOutput.trim()) {
        for (const file of untrackedOutput.trim().split("\n")) {
          const content = yield* appFs
            .readFileString(path.join(ctx.directory, file))
            .pipe(Effect.catch(() => Effect.succeed<string | undefined>(undefined)))
          if (content === undefined) continue
          changed.push({
            path: file,
            added: content.split("\n").length,
            removed: 0,
            status: "added",
          })
        }
      }

      const deletedOutput = yield* gitText([
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.quotepath=false",
        "diff",
        "--name-only",
        "--diff-filter=D",
        "HEAD",
      ])

      if (deletedOutput.trim()) {
        for (const file of deletedOutput.trim().split("\n")) {
          changed.push({
            path: file,
            added: 0,
            removed: 0,
            status: "deleted",
          })
        }
      }

      return changed.map((item) => {
        const full = path.isAbsolute(item.path) ? item.path : path.join(ctx.directory, item.path)
        return {
          ...item,
          path: path.relative(ctx.directory, full),
        }
      })
    })

    const read: Interface["read"] = Effect.fn("File.read")(function* (file: string) {
      using _ = log.time("read", { file })
      const ctx = yield* InstanceState.context
      const full = path.join(ctx.directory, file)

      if (!Instance.containsPath(full, ctx)) {
        throw new Error("Access denied: path escapes project directory")
      }

      if (isImageByExtension(file)) {
        const exists = yield* appFs.existsSafe(full)
        if (exists) {
          const bytes = yield* appFs.readFile(full).pipe(Effect.catch(() => Effect.succeed(new Uint8Array())))
          return {
            type: "text" as const,
            content: Buffer.from(bytes).toString("base64"),
            mimeType: getImageMimeType(file),
            encoding: "base64" as const,
          }
        }
        return { type: "text" as const, content: "" }
      }

      const knownText = isTextByExtension(file) || isTextByName(file)

      if (isBinaryByExtension(file) && !knownText) return { type: "binary" as const, content: "" }

      const exists = yield* appFs.existsSafe(full)
      if (!exists) return { type: "text" as const, content: "" }

      const mimeType = AppFileSystem.mimeType(full)
      const encode = knownText ? false : shouldEncode(mimeType)

      if (encode && !isImage(mimeType)) return { type: "binary" as const, content: "", mimeType }

      if (encode) {
        const bytes = yield* appFs.readFile(full).pipe(Effect.catch(() => Effect.succeed(new Uint8Array())))
        return {
          type: "text" as const,
          content: Buffer.from(bytes).toString("base64"),
          mimeType,
          encoding: "base64" as const,
        }
      }

      const content = yield* appFs.readFileString(full).pipe(
        Effect.map((s) => s.trim()),
        Effect.catch(() => Effect.succeed("")),
      )

      if (ctx.project.vcs === "git") {
        let diff = yield* gitText(["-c", "core.fsmonitor=false", "diff", "--", file])
        if (!diff.trim()) {
          diff = yield* gitText(["-c", "core.fsmonitor=false", "diff", "--staged", "--", file])
        }
        if (diff.trim()) {
          const original = yield* git.show(ctx.directory, "HEAD", file)
          const patch = structuredPatch(file, file, original, content, "old", "new", {
            context: Infinity,
            ignoreWhitespace: true,
          })
          return { type: "text" as const, content, patch, diff: formatPatch(patch) }
        }
        return { type: "text" as const, content }
      }

      return { type: "text" as const, content }
    })

    const list = Effect.fn("File.list")(function* (dir?: string) {
      const ctx = yield* InstanceState.context
      const exclude = [".git", ".DS_Store"]
      let ignored = (_: string) => false
      if (ctx.project.vcs === "git") {
        const ig = ignore()
        const gitignore = path.join(ctx.worktree, ".gitignore")
        const gitignoreText = yield* appFs.readFileString(gitignore).pipe(Effect.catch(() => Effect.succeed("")))
        if (gitignoreText) ig.add(gitignoreText)
        const ignoreFile = path.join(ctx.worktree, ".ignore")
        const ignoreText = yield* appFs.readFileString(ignoreFile).pipe(Effect.catch(() => Effect.succeed("")))
        if (ignoreText) ig.add(ignoreText)
        ignored = ig.ignores.bind(ig)
      }

      const resolved = dir ? path.join(ctx.directory, dir) : ctx.directory
      if (!Instance.containsPath(resolved, ctx)) {
        throw new Error("Access denied: path escapes project directory")
      }

      const entries = yield* appFs.readDirectoryEntries(resolved).pipe(Effect.orElseSucceed(() => []))

      const nodes: Node[] = []
      for (const entry of entries) {
        if (exclude.includes(entry.name)) continue
        const absolute = path.join(resolved, entry.name)
        const file = path.relative(ctx.directory, absolute)
        const type = entry.type === "directory" ? "directory" : "file"
        nodes.push({
          name: entry.name,
          path: file,
          absolute,
          type,
          ignored: ignored(type === "directory" ? file + "/" : file),
        })
      }
      return nodes.sort((a, b) => {
        if (a.type !== b.type) return a.type === "directory" ? -1 : 1
        return a.name.localeCompare(b.name)
      })
    })

    const search = Effect.fn("File.search")(function* (input: SearchInput) {
      const ctx = yield* InstanceState.context
      const query = input.query.trim()
      const limit = Math.max(0, Math.min(200, Math.floor(input.limit ?? 100)))
      if (!limit || ctx.directory === path.parse(ctx.directory).root) return []
      const kind = input.type ?? (input.dirs === false ? "file" : "all")
      const home = ctx.directory === Global.Path.home && ctx.project.id === "global"
      if (home && kind === "file") return []
      const files = rg.files({ cwd: ctx.directory, signal: input.signal })
      if (!query && kind === "file") {
        return yield* files.pipe(Stream.take(limit), Stream.runCollect, Effect.map(rows => [...rows]), Effect.orElseSucceed(() => []))
      }

      const hideLast = (!query || kind === "directory") && !(query.startsWith(".") || query.includes("/."))
      type Candidate = { value: string; score: number; hidden: boolean }
      const best: Candidate[] = []
      const retained = new Set<string>()
      const compare = (a: Candidate, b: Candidate) =>
        Number(a.hidden) - Number(b.hidden) || b.score - a.score || (a.value < b.value ? -1 : a.value > b.value ? 1 : 0)
      function consider(value: string) {
        if (retained.has(value)) return
        // Passing a prepared value bypasses fuzzysort's process-wide target cache.
        const match = query ? fuzzysort.single(query, fuzzysort.prepare(value)) : null
        if (query && !match) return
        const candidate = { value, score: match?.score ?? 0, hidden: hideLast && hidden(value) }
        if (best.length === limit && compare(candidate, best[limit - 1]) >= 0) return
        let low = 0, high = best.length
        while (low < high) {
          const mid = (low + high) >>> 1
          if (compare(candidate, best[mid]) < 0) high = mid
          else low = mid + 1
        }
        best.splice(low, 0, candidate)
        retained.add(value)
        if (best.length > limit) retained.delete(best.pop()!.value)
      }

      if (home) {
        yield* Stream.fromAsyncIterable(homeDirectories(ctx.directory, input.signal), cause => cause).pipe(
          Stream.runForEach(dir => Effect.sync(() => consider(dir))), Effect.ignore,
        )
      } else {
        let lastParent = ""
        yield* files.pipe(Stream.runForEach(file => Effect.sync(() => {
          if (query && kind !== "directory") consider(file)
          if (kind === "file") return
          let dir = path.dirname(file)
          if (dir === lastParent) return
          lastParent = dir
          while (dir !== ".") {
            consider(dir + "/")
            const parent = path.dirname(dir)
            if (parent === dir) break
            dir = parent
          }
        })), Effect.ignore)
      }
      return best.map(item => item.value)
    })

    log.info("init")
    return Service.of({ init: () => Effect.void, status, read, list, search })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Ripgrep.defaultLayer),
  Layer.provide(AppFileSystem.defaultLayer),
  Layer.provide(Git.defaultLayer),
)

export * as File from "."
