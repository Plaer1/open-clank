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
import {
  MAX_BYTES,
  MAX_LINES,
  previewToolOutput,
  type PreviewOptions,
  type PreviewResult,
} from "./preview"

export { MAX_BYTES, MAX_LINES, previewToolOutput }
export type { PreviewResult }
export type Options = PreviewOptions

const log = Log.create({ service: "truncation" })
const RETENTION = Duration.days(7)
const META_SUFFIX = ".meta.json"
// v1 identifiers were 12 hex timestamp characters followed by 14 base62
// characters. Current v2 identifiers add a g/- marker, 16 hex timestamp
// characters, and 9 base62 characters. Cleanup and ownership operations must
// recognize both while refusing arbitrary paths.
const OUTPUT_NAME = /^tool_(?:[0-9a-f]{12}[A-Za-z0-9]{14}|[g-][0-9a-f]{16}[A-Za-z0-9]{9})$/

export const DIR = TRUNCATION_DIR
export const GLOB = path.join(TRUNCATION_DIR, "*")

export type Result = { content: string; truncated: false } | { content: string; truncated: true; outputPath: string }

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

export function formatToolTruncationHint(file: string, outcome: "success" | "error", agent?: Agent.Info): string {
  const result = outcome === "error" ? "failed" : "succeeded"
  return hasActorTool(agent)
    ? `The tool call ${result} but the output was truncated. Full output saved to: ${file}\nUse the actor tool to have explore agent process this file with \`grep\` and \`read\` (with offset/limit). Do NOT read the full file yourself - delegate to save context.`
    : `The tool call ${result} but the output was truncated. Full output saved to: ${file}\nUse \`grep\` to search the full content or \`read\` with offset/limit to view specific sections.`
}

export interface Interface {
  readonly cleanup: () => Effect.Effect<void>
  readonly write: (text: string, ownership?: Ownership) => Effect.Effect<string>
  readonly remove: (output: string, ownership: Pick<Ownership, "owner" | "sessionID"> & {
    workspace?: string
  }) => Effect.Effect<boolean>
  readonly removeSession: (sessionID: string, owner?: string) => Effect.Effect<number>
  /**
   * Same preview as `previewToolOutput`; when truncated, writes the full text
   * to the truncation directory and appends the tool-result file-path hint.
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
      const preview = previewToolOutput(text, options)
      if (!preview.truncated) {
        return { content: preview.content, truncated: false } as const
      }
      const file = yield* write(text, ownership)
      const hint = formatToolTruncationHint(file, options.outcome ?? "success", agent)
      return {
        content: `${preview.content}\n\n${hint}`,
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
