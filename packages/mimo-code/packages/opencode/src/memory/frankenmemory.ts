import { Effect, Layer } from "effect"
import { getSharedMcpClient } from "./mcp-client"
import { Service } from "./service"

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
  scope?: string,
  scope_id?: string,
  type?: string,
): Promise<Array<{ path: string; snippet: string; score: number; scope: string; scope_id: string; type: string }>> {
  const args: Record<string, unknown> = {
    query,
    tier: "curated",
    limit,
  }
  if (scope) args.kind = scope
  if (scope_id) args.scene = scope_id

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

  const rows: Array<{ path: string; snippet: string; score: number; scope: string; scope_id: string; type: string }> = []
  for (const r of parsed.results ?? []) {
    const rec = r.record ?? {}
    const row = {
      path: (rec.id as string) ?? "",
      snippet: (rec.content as string) ?? "",
      score: r.score ?? 0,
      scope: (rec.workspace_id as string) ?? "global",
      scope_id: (rec.session_id as string) ?? "",
      type: mapKindToType((rec.kind as string) ?? "episodic"),
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

export const frankenmemoryLayer: Layer.Layer<Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
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
      scope?: string
      scope_id?: string
      type?: string
      limit?: number
    }) {
      const limit = input.limit ?? 10
      if (!input.query) return []

      return yield* Effect.promise(() =>
        callSearch(input.query, limit, input.scope, input.scope_id, input.type),
      )
    })

    return Service.of({
      root,
      reconcile,
      search,
    })
  }),
)
