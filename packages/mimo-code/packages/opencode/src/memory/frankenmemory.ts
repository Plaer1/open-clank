import { Effect, Layer } from "effect"
import { getSharedMcpClient } from "./mcp-client"
import { Service, type Interface } from "./service"
import { memorySessionScope } from "./session-scope"

const FLOOR_RATIO = 0.15

function mapKindToType(kind: string): string {
  switch (kind) {
    case "persona":
      return "pinned"
    case "episodic":
      return "snapshot"
    case "instruction":
      return "learning"
    case "fact":
      return "free"
    case "fabric":
      return "progress"
    case "wiki":
      return "reference"
    default:
      return "free"
  }
}

async function callSearch(
  query: string,
  limit: number,
  sessionID?: string,
  type?: string,
): Promise<
  Array<{
    path: string
    snippet: string
    score: number
    scope: string
    scope_id: string
    type: string
    source?: string
    trust?: string
  }>
> {
  const args: Record<string, unknown> = {
    query,
    tier: "curated",
    limit,
  }
  if (!sessionID) throw new Error("frankenmemory search requires an active session")
  const scope = memorySessionScope(sessionID)
  if (!scope) throw new Error(`frankenmemory scope missing for session ${sessionID}`)
  args.owner = scope.owner
  args.workspace_id = scope.workspaceId

  const client = await getSharedMcpClient()
  const result = await client.callTool({ name: "search", arguments: args })
  const content = result.content as Array<{ type: string; text?: string }> | undefined
  const text = content?.[0]?.text ?? ""
  let parsed: { results?: Array<{ record: Record<string, unknown>; score: number; source_label: string }>; total?: number }
  try {
    parsed = JSON.parse(text)
  } catch {
    return []
  }

  const rows: Array<{
    path: string
    snippet: string
    score: number
    scope: string
    scope_id: string
    type: string
    source?: string
    trust?: string
  }> = []
  for (const r of parsed.results ?? []) {
    const rec = r.record ?? {}
    const row = {
      path: (rec.id as string) ?? "",
      snippet: (rec.content as string) ?? "",
      score: r.score ?? 0,
      scope: (rec.workspace_id as string) ?? "global",
      scope_id: (rec.session_id as string) ?? "",
      type: mapKindToType((rec.kind as string) ?? "episodic"),
      source: r.source_label || ((rec.source as string) ?? "unknown"),
      trust: (rec.source_type as string) ?? "unknown",
    }
    if (type && row.type !== type) continue
    rows.push(row)
  }

  // Relative score floor (same semantics as native FTS service.ts:128-133)
  if (rows.length > 0) {
    const topScore = rows[0].score
    const cutoff = FLOOR_RATIO > 0 ? topScore * FLOOR_RATIO : -Infinity
    return rows.filter((r, i) => i === 0 || r.score >= cutoff).slice(0, limit)
  }
  return rows
}

export const make: Effect.Effect<Interface> = Effect.gen(function* () {
    const root = Effect.fn("Frankenmemory.root")(function* () {
      const { Global } = yield* Effect.promise(() => import("../global"))
      const path = yield* Effect.promise(() => import("path"))
      return path.join(Global.Path.data, "memory")
    })

    const reconcile = Effect.fn("Frankenmemory.reconcile")(function* () {
      return { indexed: 0, pruned: 0 }
    })

    const search = Effect.fn("Frankenmemory.search")(function* (input: {
      query: string
      sessionID?: string
      scope?: string
      scope_id?: string
      type?: string
      limit?: number
    }) {
      const limit = input.limit ?? 10
      if (!input.query) return []

      return yield* Effect.promise(() =>
        callSearch(input.query, limit, input.sessionID, input.type),
      )
    })

    return Service.of({
      root,
      reconcile,
      search,
    })
  })

export const frankenmemoryLayer: Layer.Layer<Service> = Layer.effect(Service, make)
