import { NodePath } from "@effect/platform-node"
import { Cause, Duration, Effect, Layer, Schedule, Context } from "effect"
import path from "path"
import type { Agent } from "../agent/agent"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { evaluate } from "@/permission/evaluate"
import { Identifier } from "../id/id"
import { Log } from "../util"
import { ToolID } from "./schema"
import { TRUNCATION_DIR } from "./truncation-dir"

const log = Log.create({ service: "truncation" })
const RETENTION = Duration.days(7)
const META_SUFFIX = ".meta.json"
// v1 identifiers were 12 hex timestamp characters followed by 14 base62
// characters. Current v2 identifiers add a g/- marker, 16 hex timestamp
// characters, and 9 base62 characters. Cleanup and ownership operations must
// recognize both while refusing arbitrary paths.
const OUTPUT_NAME = /^tool_(?:[0-9a-f]{12}[A-Za-z0-9]{14}|[g-][0-9a-f]{16}[A-Za-z0-9]{9})$/

export const MAX_LINES = 2000
export const MAX_BYTES = 50 * 1024
export const DIR = TRUNCATION_DIR
export const GLOB = path.join(TRUNCATION_DIR, "*")

const ERROR_PATTERN = /error|exception|failed|fatal|traceback|panic|exit code/i
const TAIL_SCAN_CHARS = 2048

export type Result = { content: string; truncated: false } | { content: string; truncated: true; outputPath: string }

export interface Options {
  maxLines?: number
  maxBytes?: number
  direction?: "head" | "tail" | "head+tail"
  pressureCaps?: boolean
  outcome?: "success" | "error"
}

export interface Ownership {
  owner: string
  workspace: string
  sessionID: string
  callID?: string
}

interface Metadata extends Ownership {
  version: 1
  output: string
  createdAt: number
}

function hasActorTool(agent?: Agent.Info) {
  if (!agent?.permission) return false
  return evaluate("actor", "*", agent.permission).action !== "deny"
}

export interface Interface {
  readonly cleanup: () => Effect.Effect<void>
  readonly write: (text: string, ownership?: Ownership) => Effect.Effect<string>
  readonly remove: (output: string, ownership: Pick<Ownership, "owner" | "sessionID"> & {
    workspace?: string
  }) => Effect.Effect<boolean>
  readonly removeSession: (sessionID: string, owner?: string) => Effect.Effect<number>
  /**
   * Returns output unchanged when it fits within the limits, otherwise writes the full text
   * to the truncation directory and returns a preview plus a hint to inspect the saved file.
   */
  readonly output: (
    text: string,
    options?: Options,
    agent?: Agent.Info,
    ownership?: Ownership,
  ) => Effect.Effect<Result>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Truncate") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const fs = yield* AppFileSystem.Service

    const runtimeOwner = () => process.env.OPEN_CLANK_OWNER?.trim().toLowerCase() || "local"
    const normalizeOwnership = (ownership?: Ownership): Ownership => ({
      owner: ownership?.owner?.trim().toLowerCase() || runtimeOwner(),
      workspace: path.resolve(ownership?.workspace || process.cwd()),
      sessionID: ownership?.sessionID?.trim() || "unscoped",
      ...(ownership?.callID ? { callID: ownership.callID } : {}),
    })
    const metaPath = (file: string) => file + META_SUFFIX
    const outputPath = (value: string) => {
      const name = path.basename(value)
      return OUTPUT_NAME.test(name) ? path.join(TRUNCATION_DIR, name) : undefined
    }
    const readMetadata = (file: string) =>
      fs.readJson(metaPath(file)).pipe(
        Effect.map((raw): Metadata | undefined => {
          if (!raw || typeof raw !== "object") return
          const value = raw as Record<string, unknown>
          if (
            value.version !== 1 ||
            typeof value.output !== "string" ||
            path.basename(value.output) !== path.basename(file) ||
            typeof value.createdAt !== "number" ||
            !Number.isFinite(value.createdAt) ||
            typeof value.owner !== "string" ||
            typeof value.workspace !== "string" ||
            typeof value.sessionID !== "string"
          )
            return
          return value as unknown as Metadata
        }),
        Effect.catchCause(() => Effect.succeed(undefined)),
      )
    const removePair = (file: string) =>
      fs
        .atomicBatch([
          { path: file, content: null },
          { path: metaPath(file), content: null },
        ])
        .pipe(Effect.orDie)

    const cleanup = Effect.fn("Truncate.cleanup")(function* () {
      const cutoff = Identifier.timestamp(
        Identifier.create("tool", "ascending", Date.now() - Duration.toMillis(RETENTION)),
      )
      const entries = yield* fs.readDirectory(TRUNCATION_DIR).pipe(
        Effect.map((all) => all.filter((name) => name.startsWith("tool_"))),
        Effect.catch(() => Effect.succeed([])),
      )
      const names = new Set(entries)
      for (const entry of entries.filter((name) => OUTPUT_NAME.test(name))) {
        const file = path.join(TRUNCATION_DIR, entry)
        const hasMetadata = names.has(entry + META_SUFFIX)
        const metadata = hasMetadata ? yield* readMetadata(file) : undefined
        if (!hasMetadata || metadata === undefined) {
          yield* removePair(file)
          continue
        }
        const expired =
          Identifier.timestamp(entry) < cutoff ||
          metadata.createdAt < Date.now() - Duration.toMillis(RETENTION)
        if (!expired) continue
        yield* removePair(file)
      }
      for (const entry of entries.filter((name) => name.endsWith(META_SUFFIX))) {
        const output = entry.slice(0, -META_SUFFIX.length)
        if (OUTPUT_NAME.test(output) && names.has(output)) continue
        yield* fs.remove(path.join(TRUNCATION_DIR, entry)).pipe(Effect.catch(() => Effect.void))
      }
    })

    const write = Effect.fn("Truncate.write")(function* (text: string, requested?: Ownership) {
      const file = path.join(TRUNCATION_DIR, ToolID.ascending())
      const ownership = normalizeOwnership(requested)
      const metadata: Metadata = {
        version: 1,
        output: path.basename(file),
        createdAt: Date.now(),
        ...ownership,
      }
      yield* fs.ensureDir(TRUNCATION_DIR).pipe(Effect.orDie)
      yield* fs
        .atomicBatch([
          { path: file, content: text, requireMissing: true, mode: 0o600 },
          {
            path: metaPath(file),
            content: JSON.stringify(metadata, null, 2),
            requireMissing: true,
            mode: 0o600,
          },
        ])
        .pipe(Effect.orDie)
      return file
    })

    const remove = Effect.fn("Truncate.remove")(function* (
      requested: string,
      ownership: Pick<Ownership, "owner" | "sessionID"> & { workspace?: string },
    ) {
      const file = outputPath(requested)
      if (!file) return false
      const metadata = yield* readMetadata(file)
      if (!metadata) return false
      if (
        metadata.owner !== ownership.owner.trim().toLowerCase() ||
        metadata.sessionID !== ownership.sessionID ||
        (ownership.workspace !== undefined && metadata.workspace !== path.resolve(ownership.workspace))
      )
        return false
      yield* removePair(file)
      return true
    })

    const removeSession = Effect.fn("Truncate.removeSession")(function* (
      sessionID: string,
      requestedOwner?: string,
    ) {
      const owner = requestedOwner?.trim().toLowerCase() || runtimeOwner()
      const entries = yield* fs.readDirectory(TRUNCATION_DIR).pipe(
        Effect.map((all) => all.filter((name) => name.endsWith(META_SUFFIX))),
        Effect.catch(() => Effect.succeed([])),
      )
      let removed = 0
      for (const entry of entries) {
        const output = entry.slice(0, -META_SUFFIX.length)
        if (!OUTPUT_NAME.test(output)) continue
        const file = path.join(TRUNCATION_DIR, output)
        const metadata = yield* readMetadata(file)
        if (!metadata || metadata.owner !== owner || metadata.sessionID !== sessionID) continue
        yield* removePair(file)
        removed++
      }
      return removed
    })

    const output = Effect.fn("Truncate.output")(function* (
      text: string,
      options: Options = {},
      agent?: Agent.Info,
      ownership?: Ownership,
    ) {
      let maxLines = options.maxLines ?? MAX_LINES
      let maxBytes = options.maxBytes ?? MAX_BYTES
      const direction = options.direction ?? "head+tail"
      const pressureCaps = options.pressureCaps ?? false
      const outcome = options.outcome ?? "success"

      const hint = (file: string) => {
        const result = outcome === "error" ? "failed" : "succeeded"
        return hasActorTool(agent)
          ? `The tool call ${result} but the output was truncated. Full output saved to: ${file}\nUse the actor tool to have explore agent process this file with Grep and Read (with offset/limit). Do NOT read the full file yourself - delegate to save context.`
          : `The tool call ${result} but the output was truncated. Full output saved to: ${file}\nUse Grep to search the full content or Read with offset/limit to view specific sections.`
      }

      if (pressureCaps) {
        maxLines = Math.floor(maxLines / 2)
        maxBytes = Math.floor(maxBytes / 2)
      }

      const lines = text.split("\n")
      const totalBytes = Buffer.byteLength(text, "utf-8")

      if (lines.length <= maxLines && totalBytes <= maxBytes) {
        return { content: text, truncated: false } as const
      }

      if (direction === "head+tail") {
        // Check if the last TAIL_SCAN_CHARS contain an error pattern
        const tailScan = text.length > TAIL_SCAN_CHARS ? text.slice(-TAIL_SCAN_CHARS) : text
        const hasErrors = ERROR_PATTERN.test(tailScan)

        if (hasErrors) {
          // Allocate 70% of budget to head, 30% to tail
          const headMaxLines = Math.floor(maxLines * 0.7)
          const headMaxBytes = Math.floor(maxBytes * 0.7)
          const tailMaxLines = maxLines - headMaxLines
          const tailMaxBytes = maxBytes - headMaxBytes

          // Collect head lines
          const headOut: string[] = []
          let headBytes = 0
          for (let i = 0; i < lines.length && headOut.length < headMaxLines; i++) {
            const size = Buffer.byteLength(lines[i], "utf-8") + (i > 0 ? 1 : 0)
            if (headBytes + size > headMaxBytes) break
            headOut.push(lines[i])
            headBytes += size
          }

          // Collect tail lines
          const tailOut: string[] = []
          let tailBytes = 0
          for (let i = lines.length - 1; i >= 0 && tailOut.length < tailMaxLines; i--) {
            const size = Buffer.byteLength(lines[i], "utf-8") + (tailOut.length > 0 ? 1 : 0)
            if (tailBytes + size > tailMaxBytes) break
            tailOut.unshift(lines[i])
            tailBytes += size
          }

          const omitted = lines.length - headOut.length - tailOut.length
          const file = yield* write(text, ownership)

          return {
            content: `${headOut.join("\n")}\n\n... ${omitted} lines omitted — showing head and tail ...\n\n${tailOut.join("\n")}\n\n${hint(file)}`,
            truncated: true,
            outputPath: file,
          } as const
        }
        // No errors in tail: degrade to head behavior
      }

      const out: string[] = []
      let i = 0
      let bytes = 0
      let hitBytes = false

      if (direction === "head" || direction === "head+tail") {
        for (i = 0; i < lines.length && i < maxLines; i++) {
          const size = Buffer.byteLength(lines[i], "utf-8") + (i > 0 ? 1 : 0)
          if (bytes + size > maxBytes) {
            hitBytes = true
            break
          }
          out.push(lines[i])
          bytes += size
        }
      } else {
        for (i = lines.length - 1; i >= 0 && out.length < maxLines; i--) {
          const size = Buffer.byteLength(lines[i], "utf-8") + (out.length > 0 ? 1 : 0)
          if (bytes + size > maxBytes) {
            hitBytes = true
            break
          }
          out.unshift(lines[i])
          bytes += size
        }
      }

      const removed = hitBytes ? totalBytes - bytes : lines.length - out.length
      const unit = hitBytes ? "bytes" : "lines"
      const preview = out.join("\n")
      const file = yield* write(text, ownership)

      return {
        content:
          direction === "head" || direction === "head+tail"
            ? `${preview}\n\n...${removed} ${unit} truncated...\n\n${hint(file)}`
            : `...${removed} ${unit} truncated...\n\n${hint(file)}\n\n${preview}`,
        truncated: true,
        outputPath: file,
      } as const
    })

    const safeCleanup = cleanup().pipe(
      Effect.catchCause((cause) => {
        log.error("truncation cleanup failed", { cause: Cause.pretty(cause) })
        return Effect.void
      }),
    )
    // Run once during layer startup so stale pairs and orphan sidecars do not
    // survive until the first hourly maintenance tick.
    yield* safeCleanup
    yield* safeCleanup.pipe(
      Effect.repeat(Schedule.spaced(Duration.hours(1))),
      Effect.delay(Duration.hours(1)),
      Effect.forkScoped,
    )

    return Service.of({ cleanup, write, remove, removeSession, output })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(AppFileSystem.defaultLayer), Layer.provide(NodePath.layer))
