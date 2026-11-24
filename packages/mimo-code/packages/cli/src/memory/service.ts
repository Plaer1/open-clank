import type { ActorExecutionIdentity } from "@/actor/turn"
import { Cause, Context, Effect, Layer } from "effect"
import { RecoverableError } from "@/tool/recoverable"
import { Config } from "../config"
import { Flag } from "@/flag/flag"
import { recallCarrier, bindRecallCarrier } from "./mcp-client"


export type SearchRow = {
  path: string
  scope: string
  scope_id: string
  type: string
  snippet: string
  body?: string
  score: number
  source?: string
  trust?: string
  backend?: "mimo" | "memery"
  source_uri?: string
  source_revision?: string | number
  content_hash?: string
  authored_path?: string
  explanation?: Record<string, unknown>
  provenance_conflict?: boolean
}

export function mergeSearchRows(memeryRows: SearchRow[], nativeRows: SearchRow[], limit: number): SearchRow[] {
  const normalize = (rows: SearchRow[], backend: SearchRow["backend"]) => {
    const top = Math.max(...rows.map((row) => row.score), 0)
    return rows.map((row) => ({
      ...row,
      score: top > 0 ? row.score / top : row.score,
      source: row.source ?? (backend === "mimo" ? "markdown" : "unknown"),
      trust: row.trust ?? (backend === "mimo" ? "authored" : "unknown"),
      backend,
    }))
  }
  const combined = [
    ...normalize(memeryRows, "memery"),
    ...normalize(nativeRows, "mimo"),
  ]
  // Cross-backend identity is the AUTHORED FILE, not source_uri: Memery projects
  // one row per section (source_uri file://path#anchor + a section content
  // hash, with metadata.authored_path = path) while native emits one row per
  // file (source_uri file://path + a size-mtime fingerprint). The two hash
  // schemes never collide, so keying on source_uri + content_hash never
  // matched across backends. Memery's section content hash stays Memery-internal
  // identity; only the file path is shared.
  const fileOf = (row: SearchRow) => (row.backend === "memery" ? row.authored_path : row.path)
  const memeryHashesByPath = new Map<string, Set<string>>()
  for (const row of combined) {
    if (row.backend !== "memery" || !row.authored_path || !row.content_hash) continue
    const hashes = memeryHashesByPath.get(row.authored_path) ?? new Set<string>()
    hashes.add(row.content_hash)
    memeryHashesByPath.set(row.authored_path, hashes)
  }
  // A file surfaced by both backends whose native fingerprint matches no Memery
  // section hash is a provenance conflict: both rows stay visible, flagged.
  const conflictPaths = new Set<string>()
  for (const row of combined) {
    if (row.backend !== "mimo" || !row.content_hash) continue
    const hashes = memeryHashesByPath.get(row.path)
    if (hashes && !hashes.has(row.content_hash)) conflictPaths.add(row.path)
  }
  const dedupKey = (row: SearchRow) => {
    const file = fileOf(row)
    if (!file || !row.content_hash) return `${row.backend}:${row.path}`
    if (row.backend === "memery") return `source:${file}\u001f${row.content_hash}`
    // A native row only collapses onto Memery's key when its fingerprint equals a
    // section hash; otherwise it is a distinct (possibly conflicting) row.
    if (memeryHashesByPath.get(file)?.has(row.content_hash)) return `source:${file}\u001f${row.content_hash}`
    return `${row.backend}:${row.path}`
  }
  const canonical = new Map<string, SearchRow>()
  for (const item of combined) {
    const file = fileOf(item)
    const row = {
      ...item,
      provenance_conflict: item.provenance_conflict || (file !== undefined && conflictPaths.has(file)),
    }
    const key = dedupKey(row)
    const prior = canonical.get(key)
    if (
      !prior ||
      (row.backend === "memery" && prior.backend !== "memery") ||
      (row.backend === prior.backend && row.score > prior.score)
    ) {
      canonical.set(key, row)
    }
  }
  return [...canonical.values()]
    .sort((a, b) => {
      const score = b.score - a.score
      if (score !== 0) return score
      if (a.backend !== b.backend) return a.backend === "memery" ? -1 : 1
      return a.path.localeCompare(b.path)
    })
    .slice(0, limit)
}

export interface Interface {
  readonly root: () => Effect.Effect<string>
  readonly reconcile: () => Effect.Effect<{ indexed: number; pruned: number }>
  readonly search: (input: {
    query: string
    sessionID?: string
    actorExecution?: ActorExecutionIdentity
    toolCallID?: string
    scope?: string
    scope_id?: string
    type?: string
    limit?: number
  }) => Effect.Effect<SearchRow[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Memory") {}

/** Memery is the only semantic-memory authority in every runtime mode. */
export const make: Effect.Effect<Interface, never, Config.Service> = Effect.gen(function* () {
  const config = yield* Config.Service
  let memery: Interface | undefined
  const selected = Effect.fn("Memory.authority")(function* () {
    const cfg = yield* config.get()
    if (Flag.OPEN_CLANK_PRIVATE_RUNTIME)
      return yield* Effect.die(new RecoverableError("Memory is unavailable in this private session"))
    if (cfg.memory?.provider !== "memery")
      return yield* Effect.die(new RecoverableError("Memory is unavailable: configure an authorized Memery connection"))
    if (!memery) {
      const mod = yield* Effect.promise(() => import("./memery")).pipe(Effect.orDie)
      memery = yield* mod.make
    }
    return memery
  })
  return Service.of({
    root: () => Effect.die(new RecoverableError("Memory has no filesystem root; use the scoped memory authority")),
    reconcile: () => Effect.die(new RecoverableError("File-memory indexing is unavailable; use the scoped memory authority")),
    search: (input) => Effect.gen(function* () {
      if (input.scope !== undefined || input.scope_id !== undefined)
        return yield* Effect.die(new RecoverableError("Model-selected memory scope is unavailable; use the admitted session authority"))
      const authority = yield* selected()
      const rows = yield* authority.search(input).pipe(Effect.catchCause((cause) =>
        Cause.hasInterrupts(cause) ? Effect.failCause(cause) : Effect.die(new RecoverableError(`Scoped memory search unavailable: ${String(cause)}`))))
      const merged=mergeSearchRows(rows, [], input.limit ?? 10)
      const carrier=recallCarrier(rows)
      if (carrier && merged.length === rows.length && merged.every((row,index) => {
        const original=rows[index]
        return row.path === original.path && row.snippet === original.snippet && row.source_uri === original.source_uri &&
          row.content_hash === original.content_hash && row.authored_path === original.authored_path &&
          row.provenance_conflict === original.provenance_conflict
      })) bindRecallCarrier(merged,carrier)
      return merged
    }),
  })
})

export const defaultLayer = Layer.suspend(() => Layer.effect(Service, make).pipe(Layer.provide(Config.defaultLayer)))
