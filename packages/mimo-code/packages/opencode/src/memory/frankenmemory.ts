import { Effect, Layer } from "effect"
import { callBoundMemoryTool } from "./mcp-client"
import { Service, type Interface, type SearchRow } from "./service"
import { CHAT_WORKSPACE, memorySessionScope } from "./session-scope"

const FLOOR_RATIO = 0.15

export type MemoryRetentionPolicy = {
  raw_days: number
  candidate_days: number
  curated_days?: number
  graph_days?: number
  recovery_seconds: number
  clear_curated_days?: boolean
  clear_graph_days?: boolean
}

export type ForgetSelector =
  | { kind: "record_id"; value: string }
  | { kind: "source_uri"; value: string }
  | { kind: "source_message_id"; value: string }

async function callScopedTool(
  name: string,
  sessionID: string,
  args: Record<string, unknown>,
): Promise<Record<string, unknown>> {
  const scope = memorySessionScope(sessionID)
  if (!scope) throw new Error(`frankenmemory scope missing for session ${sessionID}`)
  const result = await callBoundMemoryTool(sessionID, name, args)
  const content = result.content as Array<{ type: string; text?: string }> | undefined
  const text = content?.[0]?.text ?? "{}"
  // A malformed payload is an empty result, not a thrown SyntaxError escaping
  // into the caller (same idiom as callSearch below).
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return {}
  }
  return parsed && typeof parsed === "object" && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : {}
}

// STAGED — no production consumer yet. These are the typed candidate-review,
// retention and forget verbs for the planned memory review surface (only
// test/memory/mcp-client.test.ts exercises them today). Kept exported so the
// contract stays compiled and pinned by tests; wire the review UI/tool here
// when it lands.
export const lifecycle = {
  listCandidates: (sessionID: string, status = "pending", limit = 100) =>
    callScopedTool("list_candidates", sessionID, { status, limit }),
  updateCandidate: (
    sessionID: string,
    id: string,
    content: string,
    category?: string,
  ) =>
    callScopedTool("update_candidate", sessionID, {
      id,
      content,
      ...(category ? { category } : {}),
      reason: "edited_by_mimo",
    }),
  reviewCandidate: (
    sessionID: string,
    id: string,
    accept: boolean,
    reason = accept ? "approved_by_mimo_user" : "rejected_by_mimo_user",
  ) => callScopedTool("review_candidate", sessionID, { id, accept, reason }),
  resolveQuestion: (
    sessionID: string,
    id: string,
    answer: string,
    expectedRevision?: number,
  ) =>
    callScopedTool("resolve_memory", sessionID, {
      id,
      answer,
      ...(expectedRevision === undefined
        ? {}
        : { expected_revision: expectedRevision }),
    }),
  reopenQuestion: (sessionID: string, id: string, expectedRevision: number) =>
    callScopedTool("reopen_memory", sessionID, {
      id,
      expected_revision: expectedRevision,
    }),
  retention: (
    sessionID: string,
    action: "get" | "set" | "expire",
    policy: Partial<MemoryRetentionPolicy> = {},
  ) => callScopedTool("memory_retention", sessionID, { action, ...policy }),
  previewRetentionExpiry: (sessionID: string) =>
    callScopedTool("memory_retention", sessionID, {
      action: "preview_expire",
    }),
  commitRetentionExpiry: (
    sessionID: string,
    previewToken: string,
    operationID: string,
  ) =>
    callScopedTool("memory_retention", sessionID, {
      action: "expire",
      preview_token: previewToken,
      operation_id: operationID,
    }),
  retentionStatus: (sessionID: string, operationID: string) =>
    callScopedTool("memory_retention", sessionID, {
      action: "status",
      operation_id: operationID,
    }),
  previewForget: (sessionID: string, selector: ForgetSelector) =>
    callScopedTool("memory_forget", sessionID, {
      action: "preview",
      selector_kind: selector.kind,
      selector: selector.value,
    }),
  commitForget: (
    sessionID: string,
    selector: ForgetSelector,
    previewToken: string,
    operationID?: string,
  ) =>
    callScopedTool("memory_forget", sessionID, {
      action: "commit",
      selector_kind: selector.kind,
      selector: selector.value,
      preview_token: previewToken,
      ...(operationID ? { operation_id: operationID } : {}),
    }),
  forgetStatus: (sessionID: string, operationID: string) =>
    callScopedTool("memory_forget", sessionID, {
      action: "status",
      operation_id: operationID,
    }),
  restoreForget: (sessionID: string, tombstoneID: string) =>
    callScopedTool("memory_forget", sessionID, {
      action: "restore",
      tombstone_id: tombstoneID,
    }),
  exportScope: (sessionID: string) => callScopedTool("memory_export", sessionID, {}),
  explain: (sessionID: string, id: string) =>
    callScopedTool("memory_explain", sessionID, { id }),
}

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
): Promise<SearchRow[]> {
  const args: Record<string, unknown> = {
    query,
    tier: "curated",
    limit,
  }
  if (!sessionID) throw new Error("frankenmemory search requires an active session")
  const scope = memorySessionScope(sessionID)
  if (!scope) throw new Error(`frankenmemory scope missing for session ${sessionID}`)
  const result = await callBoundMemoryTool(sessionID, "search", args)
  const content = result.content as Array<{ type: string; text?: string }> | undefined
  const text = content?.[0]?.text ?? ""
  let parsed: { results?: Array<{ record: Record<string, unknown>; score: number; source_label: string }>; total?: number }
  try {
    parsed = JSON.parse(text)
  } catch {
    return []
  }

  const rows: SearchRow[] = []
  for (const r of parsed.results ?? []) {
    const rec = r.record ?? {}
    const metadata =
      rec.metadata && typeof rec.metadata === "object" && !Array.isArray(rec.metadata)
        ? (rec.metadata as Record<string, unknown>)
        : {}
    const explanation =
      metadata.recall_explanation &&
      typeof metadata.recall_explanation === "object" &&
      !Array.isArray(metadata.recall_explanation)
        ? (metadata.recall_explanation as Record<string, unknown>)
        : {
            strategy: r.source_label || "unknown",
            lexical: null,
            vector: null,
            graph: 0,
            final_score: r.score ?? 0,
          }
    const row = {
      path: (rec.id as string) ?? "",
      snippet: (rec.content as string) ?? "",
      score: r.score ?? 0,
      scope: (rec.workspace_id as string) ?? CHAT_WORKSPACE,
      scope_id: (rec.session_id as string) ?? "",
      type: mapKindToType((rec.kind as string) ?? "episodic"),
      source: r.source_label || ((rec.source as string) ?? "unknown"),
      trust: (rec.source_type as string) ?? "unknown",
      source_uri: (metadata.source_uri as string) || undefined,
      source_revision: (metadata.source_revision as string | number) ?? undefined,
      content_hash: (metadata.content_hash as string) || undefined,
      authored_path: (metadata.authored_path as string) || undefined,
      explanation,
      provenance_conflict: metadata.provenance_conflict === true,
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
