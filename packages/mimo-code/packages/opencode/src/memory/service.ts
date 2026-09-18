import { Context, Effect, Layer } from "effect"
import path from "path"
import os from "os"
import { Global } from "../global"
import { Database } from "../storage"
import { Config } from "../config"
import { Log } from "../util"
import { reconcileMemory } from "./reconcile"
import { buildFtsQuery } from "./fts-query"
import { resolveProjectId } from "./paths"
import { memorySessionScope, uniqueMemorySessionScope } from "./session-scope"

const log = Log.create({ service: "memory.service" })

export type SearchRow = {
  path: string
  scope: string
  scope_id: string
  type: string
  snippet: string
  score: number
  source?: string
  trust?: string
  backend?: "mimo" | "frankenmemory"
  source_uri?: string
  source_revision?: string | number
  content_hash?: string
  authored_path?: string
  explanation?: Record<string, unknown>
  provenance_conflict?: boolean
}

export function mergeSearchRows(fmRows: SearchRow[], nativeRows: SearchRow[], limit: number): SearchRow[] {
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
    ...normalize(fmRows, "frankenmemory"),
    ...normalize(nativeRows, "mimo"),
  ]
  // Cross-backend identity is the AUTHORED FILE, not source_uri: FM projects
  // one row per section (source_uri file://path#anchor + a section content
  // hash, with metadata.authored_path = path) while native emits one row per
  // file (source_uri file://path + a size-mtime fingerprint). The two hash
  // schemes never collide, so keying on source_uri + content_hash never
  // matched across backends. FM's section content hash stays FM-internal
  // identity; only the file path is shared.
  const fileOf = (row: SearchRow) => (row.backend === "frankenmemory" ? row.authored_path : row.path)
  const fmHashesByPath = new Map<string, Set<string>>()
  for (const row of combined) {
    if (row.backend !== "frankenmemory" || !row.authored_path || !row.content_hash) continue
    const hashes = fmHashesByPath.get(row.authored_path) ?? new Set<string>()
    hashes.add(row.content_hash)
    fmHashesByPath.set(row.authored_path, hashes)
  }
  // A file surfaced by both backends whose native fingerprint matches no FM
  // section hash is a provenance conflict: both rows stay visible, flagged.
  const conflictPaths = new Set<string>()
  for (const row of combined) {
    if (row.backend !== "mimo" || !row.content_hash) continue
    const hashes = fmHashesByPath.get(row.path)
    if (hashes && !hashes.has(row.content_hash)) conflictPaths.add(row.path)
  }
  const dedupKey = (row: SearchRow) => {
    const file = fileOf(row)
    if (!file || !row.content_hash) return `${row.backend}:${row.path}`
    if (row.backend === "frankenmemory") return `source:${file}\u001f${row.content_hash}`
    // A native row only collapses onto FM's key when its fingerprint equals a
    // section hash; otherwise it is a distinct (possibly conflicting) row.
    if (fmHashesByPath.get(file)?.has(row.content_hash)) return `source:${file}\u001f${row.content_hash}`
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
      (row.backend === "frankenmemory" && prior.backend !== "frankenmemory") ||
      (row.backend === prior.backend && row.score > prior.score)
    ) {
      canonical.set(key, row)
    }
  }
  return [...canonical.values()]
    .sort((a, b) => {
      const score = b.score - a.score
      if (score !== 0) return score
      if (a.backend !== b.backend) return a.backend === "frankenmemory" ? -1 : 1
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
    scope?: string
    scope_id?: string
    type?: string
    limit?: number
  }) => Effect.Effect<SearchRow[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Memory") {}

export const make: Effect.Effect<Interface, never, Config.Service> = Effect.gen(function* () {
    const config = yield* Config.Service
    const root = path.join(Global.Path.data, "memory")
    const ccBase = path.join(os.homedir(), ".claude", "projects")

    const rootEff = Effect.fn("Memory.root")(function* () {
      return root
    })

    // fm projection scope for reconcile-time ingest: only in fm mode and
    // only when an authenticated session scope exists (same rule as
    // capture.ts — no owner, no writes).
    const fmIngestScope = (cfg: { memory?: { provider?: string } }) => {
      if (cfg.memory?.provider !== "frankenmemory") return undefined
      const scope = uniqueMemorySessionScope()
      return scope?.owner ? { owner: scope.owner, workspaceId: scope.workspaceId } : undefined
    }

    const reconcile = Effect.fn("Memory.reconcile")(function* () {
      const cfg = yield* config.get()
      const cc = cfg.memory?.cc_index ? ccBase : undefined
      return yield* Effect.promise(() => reconcileMemory({ mimo: root, cc }, fmIngestScope(cfg)))
    })

    const search = Effect.fn("Memory.search")(function* (input: {
      query: string
      sessionID?: string
      scope?: string
      scope_id?: string
      type?: string
      limit?: number
    }) {
      // Lazy reconcile before search (covers off-tool writes); honour config flag.
      const cfg = yield* config.get()
      if (cfg.checkpoint?.memory_reconcile_on_search ?? true) {
        const cc = cfg.memory?.cc_index ? ccBase : undefined
        // Non-fatal like authored-ingest / compaction-capture: reconcile is a
        // maintenance mirror, never a search blocker. A scoped-ingest failure
        // (e.g. a mixed-tenant process tripping uniqueMemorySessionScope)
        // degrades to serving the last good index instead of failing search.
        yield* Effect.promise(() => reconcileMemory({ mimo: root, cc }, fmIngestScope(cfg))).pipe(
          Effect.catchCause((cause) =>
            Effect.sync(() =>
              log.warn("reconcile-on-search failed; serving last index", { cause: String(cause) }),
            ),
          ),
        )
      }

      const limit = input.limit ?? 10
      // Build a token-level FTS5 query: punctuation becomes separators,
      // each alphanumeric run becomes a phrase-quoted literal, OR-joined.
      // See packages/opencode/src/memory/fts-query.ts for the rationale.
      const ftsQuery = buildFtsQuery(input.query)
      if (!ftsQuery) return []

      // OR-join means a doc matching only a common word (e.g. every
      // checkpoint.md matches "checkpoint") still matches, but BM25 ranks it
      // far below a doc matching several rare query words. We drop the
      // common-word noise with a RELATIVE floor: keep results scoring at
      // least `ratio` of the top hit's score. Relative (not absolute)
      // because BM25 magnitudes are corpus-size-dependent — in a tiny corpus
      // every score collapses toward 0 (low IDF), so any fixed absolute floor
      // would wrongly wipe real hits. The #1 result is ALWAYS kept (a match
      // is a match even when BM25 can't discriminate). Default 0.15.
      // Configurable; 0 disables (keep all matches).
      const floorRatio = cfg.checkpoint?.memory_search_score_floor ?? 0.15

      // Construct WHERE clauses for scope/scope_id/type filtering
      const conditions: string[] = []
      const params: string[] = []
      if (input.scope) {
        conditions.push("memory_fts.scope = ?")
        params.push(input.scope)
      }
      if (input.scope_id) {
        conditions.push("memory_fts.scope_id = ?")
        params.push(input.scope_id)
      }
      if (input.type) {
        conditions.push("memory_fts.type = ?")
        params.push(input.type)
      }
      const whereClause = conditions.length > 0 ? `AND ${conditions.join(" AND ")}` : ""

      const sql = `
        SELECT memory_fts.path, memory_fts.scope, memory_fts.scope_id, memory_fts.type,
               memory_fts.fingerprint,
               snippet(memory_fts_idx, 0, '<<', '>>', '...', 32) AS snippet,
               bm25(memory_fts_idx) AS score
        FROM memory_fts_idx
        JOIN memory_fts ON memory_fts.id = memory_fts_idx.rowid
        WHERE memory_fts_idx MATCH ?
        ${whereClause}
        ORDER BY score
        LIMIT ?
      `

      // Over-fetch (3x, capped) so the relative floor can trim common-word
      // noise without starving the list when there ARE enough real hits.
      const fetchLimit = Math.min(limit * 3, 50)
      const rows = Database.Client().$client.query(sql).all(ftsQuery, ...params, fetchLimit) as SearchRow[]

      // FTS5 bm25() returns lower = better; convert to higher = better for caller
      const mapped = rows.map((r) => ({
        path: r.path,
        snippet: r.snippet,
        score: -r.score,
        scope: r.scope,
        scope_id: r.scope_id,
        type: r.type,
        source: "markdown",
        trust: "authored",
        source_uri: `file://${r.path}`,
        source_revision: (r as SearchRow & { fingerprint?: string }).fingerprint,
        content_hash: (r as SearchRow & { fingerprint?: string }).fingerprint,
        explanation: {
          strategy: "fts_bm25",
          lexical: -r.score,
          vector: null,
          graph: 0,
        },
      }))
      if (mapped.length === 0) return []
      // Rows are ORDER BY score (best first), so mapped[0] is the top hit.
      // Always keep it; drop trailing rows below `floorRatio` of its score.
      const topScore = mapped[0].score
      const cutoff = floorRatio > 0 ? topScore * floorRatio : -Infinity
      return mapped.filter((r, i) => i === 0 || r.score >= cutoff).slice(0, limit)
    })

    return Service.of({
      root: rootEff,
      reconcile,
      search,
    })
  })

export const layer: Layer.Layer<Service, never, Config.Service> = Layer.effect(Service, make)

export const defaultLayer = Layer.suspend(() =>
  Layer.effect(
    Service,
    Effect.gen(function* () {
      const config = yield* Config.Service
      const native = yield* make
      // Backend is picked PER CALL, not at layer materialization: Config state
      // is instance-scoped (AsyncLocalStorage) since upstream v0.1.4+, so a
      // config.get() while the layer is being built runs outside any instance
      // context and throws. Method calls always run inside one.
      let fm: Interface | undefined
      const backend = Effect.fn("Memory.backend")(function* () {
        const cfg = yield* config.get()
        if (cfg.memory?.provider !== "frankenmemory") return native
        if (!fm) {
          const mod = yield* Effect.promise(() => import("./frankenmemory")).pipe(Effect.orDie)
          fm = yield* mod.make
        }
        return fm
      })
      return Service.of({
        root: () => native.root(),
        reconcile: () => native.reconcile(),
        search: (input) =>
          Effect.gen(function* () {
            const selected = yield* backend()
            if (selected === native) return yield* native.search(input)

            const limit = input.limit ?? 10
            const scope = input.sessionID ? memorySessionScope(input.sessionID) : undefined
            const nativeQueries: Parameters<Interface["search"]>[0][] = input.scope
              ? [{ ...input, limit }]
              : scope
                ? [
                    { ...input, scope: "global", scope_id: undefined, limit },
                    {
                      ...input,
                      scope: "projects",
                      scope_id: resolveProjectId(scope.workspacePath),
                      limit,
                    },
                    { ...input, scope: "sessions", scope_id: scope.sessionId, limit },
                  ]
                : []
            const nativeRows = yield* Effect.all(
              nativeQueries.map((query) => native.search(query)),
              { concurrency: 3 },
            ).pipe(Effect.map((pages) => pages.flat()))
            const fmRows = yield* selected.search(input).pipe(
              Effect.catchCause(() => Effect.succeed([] as SearchRow[])),
            )

            return mergeSearchRows(fmRows, nativeRows, limit)
          }),
      })
    }),
  ).pipe(Layer.provide(Config.defaultLayer)),
)
