import { Context, Effect, Layer } from "effect"
import { and, asc, desc, eq, sql } from "drizzle-orm"
import { Database } from "../storage"
import { MessageTable, PartTable, SessionTable } from "../session/session.sql"
import type { MessageID, PartID, SessionID } from "../session/schema"
import { Config } from "../config"
import { Bus } from "../bus"
import { Instance } from "../project/instance"
import { buildFtsQuery } from "./fts-query"
import type { Kind } from "./extract"
import { layer as writerLayer, Service as WriterService } from "./writer"
import { layer as backfillLayer, Service as BackfillService } from "./backfill"
import { ManagedProvider } from "@/acp/managed-provider"

export type SearchHit = {
  part_id: string
  session_id: string
  message_id: string
  project_id: string
  kind: Kind
  tool_name: string | null
  snippet: string
  score: number
  time_created: number
}

export type MessagePart = {
  part_id: string
  type: string
  role: "user" | "assistant"
  tool_name: string | null
  text: string
}

export type FullPart = {
  part_id: string
  message_id: string
  session_id: string
  type: string
  role: "user" | "assistant"
  tool_name: string | null
  /** Full canonical body text — never an FTS preview. */
  text: string
  has_more: boolean
  next_offset: number | null
  attachments: Array<{
    asset_id: string
    mime_type: string | null
    filename: string | null
    byte_size: number | null
  }>
  time_created: number
}

export type MessageContext = {
  message_id: string
  matched: boolean
  time_created: number
  parts: MessagePart[]
}

export interface Interface {
  readonly search: (input: {
    query: string
    scope?: "project" | "global"
    session_id?: string
    kind?: Kind | Kind[]
    tool_name?: string
    time_after?: number
    time_before?: number
    limit?: number
  }) => Effect.Effect<SearchHit[]>

  readonly around: (input: {
    session_id: string
    chat_id?: string
    message_id: string
    before?: number
    after?: number
  }) => Effect.Effect<{ session_id: string; messages: MessageContext[] }>

  /** Full-part get. Reads the canonical part body, not an FTS preview. */
  readonly get: (input: {
    session_id: string
    chat_id?: string
    message_id: string
    part_id: string
    length?: number
    offset?: number
  }) => Effect.Effect<FullPart | null>

  /** Explicit single-attachment locator metadata (owned assets only). */
  readonly media: (input: {
    session_id: string
    chat_id?: string
    message_id: string
    part_id: string
    asset_id?: string
  }) => Effect.Effect<FullPart["attachments"]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/History") {}

const HARD_CAP = 50

/** Code-point-safe UTF-16 paging. Never splits a surrogate pair. */
function pageUtf16(
  raw: string,
  offset: number,
  budget: number,
): { text: string; end: number; hasMore: boolean } {
  const skip = Math.max(0, offset)
  const limit = Math.max(0, budget)
  let index = 0
  let skipped = 0
  while (index < raw.length && skipped < skip) {
    const cp = raw.codePointAt(index)!
    const size = cp > 0xffff ? 2 : 1
    // A skip cursor that lands mid-pair snaps to the pair start (never begins
    // a slice on a lone surrogate).
    if (skipped + size > skip) break
    skipped += size
    index += size
  }
  let end = index
  let taken = 0
  while (end < raw.length && taken < limit) {
    const cp = raw.codePointAt(end)!
    const size = cp > 0xffff ? 2 : 1
    if (taken + size > limit) break
    taken += size
    end += size
  }
  // Guarantee forward progress and whole code points: a budget that cannot
  // hold the next char still returns that char, never a lone surrogate.
  if (taken === 0 && index < raw.length) {
    const cp = raw.codePointAt(index)!
    const size = cp > 0xffff ? 2 : 1
    return {
      text: raw.slice(index, index + size),
      end: index + size,
      hasMore: index + size < raw.length,
    }
  }
  return {
    text: raw.slice(index, end),
    // Absolute snapped end index — not offset+consumed — so a mid-pair
    // backward snap cannot skip the next BMP character on the following page.
    end,
    hasMore: end < raw.length,
  }
}

type Row = {
  part_id: string
  session_id: string
  message_id: string
  project_id: string
  kind: string
  tool_name: string | null
  snippet: string
  score: number
  time_created: number
}

export const defaultLayer: Layer.Layer<Service | WriterService | BackfillService, never, never> = Layer.suspend(() =>
  Layer.mergeAll(layer, writerLayer, backfillLayer).pipe(
    Layer.provide(Config.defaultLayer),
    Layer.provide(Bus.defaultLayer),
  ),
)

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const search = Effect.fn("History.search")(function* (input: Parameters<Interface["search"]>[0]) {
      if (ManagedProvider.enabled()) {
        if (!input.session_id) throw new Error("managed history search requires bound session")
        if (input.scope === "project") {
          throw new Error("managed history does not support project scope; omit scope for the bound chat or use global")
        }
        const remote = (yield* Effect.tryPromise(() => ManagedProvider.historyCall(input.session_id!, "search", {
          query: input.query,
          scope: input.scope === "global" ? "global" : "chat",
          ...(input.kind ? { kind: Array.isArray(input.kind) ? input.kind : [input.kind] } : {}),
          ...(input.tool_name ? { toolName: input.tool_name } : {}),
          ...(input.time_after !== undefined ? { timeAfter: input.time_after } : {}),
          ...(input.time_before !== undefined ? { timeBefore: input.time_before } : {}),
          limit: input.limit,
        })).pipe(Effect.orDie)) as { hits?: SearchHit[] }
        return (remote.hits ?? []) as SearchHit[]
      }
      const ftsQuery = buildFtsQuery(input.query)
      if (!ftsQuery) return []

      const limit = Math.min(input.limit ?? 10, HARD_CAP)
      const conditions: string[] = []
      const params: (string | number)[] = []

      const scope = input.scope ?? "project"
      if (scope === "project") {
        conditions.push("history_fts.project_id = ?")
        params.push(Instance.project.id)
      }

      if (input.session_id) {
        conditions.push("history_fts.session_id = ?")
        params.push(input.session_id)
      }
      if (input.kind) {
        const kinds = Array.isArray(input.kind) ? input.kind : [input.kind]
        conditions.push(`history_fts.kind IN (${kinds.map(() => "?").join(",")})`)
        for (const k of kinds) params.push(k)
      }
      if (input.tool_name) {
        conditions.push("history_fts.tool_name = ?")
        params.push(input.tool_name)
      }
      if (input.time_after !== undefined) {
        conditions.push("history_fts.time_created >= ?")
        params.push(input.time_after)
      }
      if (input.time_before !== undefined) {
        conditions.push("history_fts.time_created <= ?")
        params.push(input.time_before)
      }

      const whereClause = conditions.length > 0 ? `AND ${conditions.join(" AND ")}` : ""
      const sqlText = `
        SELECT history_fts.part_id, history_fts.session_id, history_fts.message_id,
               history_fts.project_id, history_fts.kind, history_fts.tool_name,
               history_fts.time_created,
               snippet(history_fts_idx, 0, '<<', '>>', '...', 32) AS snippet,
               bm25(history_fts_idx) AS score
        FROM history_fts_idx
        JOIN history_fts ON history_fts.rowid = history_fts_idx.rowid
        WHERE history_fts_idx MATCH ?
        ${whereClause}
        ORDER BY score
        LIMIT ?
      `
      const rows = Database.Client().$client.query(sqlText).all(ftsQuery, ...params, limit) as Row[]
      return rows.map((r) => ({
        part_id: r.part_id,
        session_id: r.session_id,
        message_id: r.message_id,
        project_id: r.project_id,
        kind: r.kind as Kind,
        tool_name: r.tool_name,
        snippet: r.snippet,
        score: -r.score,
        time_created: r.time_created,
      }))
    })

    const around = Effect.fn("History.around")(function* (input: Parameters<Interface["around"]>[0]) {
      if (ManagedProvider.enabled()) {
        const remote = (yield* Effect.tryPromise(() => ManagedProvider.historyCall(input.session_id, "around", { messageID: input.message_id, before: input.before, after: input.after, ...(input.chat_id ? { chatID: input.chat_id } : {}) })).pipe(Effect.orDie)) as { session_id?: string; messages?: MessageContext[] }
        return { session_id: remote.session_id ?? "", messages: (remote.messages ?? []) as MessageContext[] }
      }
      const before = input.before ?? 5
      const after = input.after ?? 5
      // Scope the anchor to the caller's authorized session/project. Guessed
      // ids from another chat must not bypass scope through around/get.
      const anchor = Database.use((db) =>
        db
          .select({
            id: MessageTable.id,
            session_id: MessageTable.session_id,
            time_created: MessageTable.time_created,
          })
          .from(MessageTable)
          .innerJoin(SessionTable, eq(MessageTable.session_id, SessionTable.id))
          .where(
            and(
              eq(MessageTable.id, input.message_id as MessageID),
              eq(MessageTable.session_id, input.session_id as SessionID),
              eq(SessionTable.project_id, Instance.project.id),
            ),
          )
          .get(),
      )
      if (!anchor) return { session_id: "", messages: [] }

      const beforeRows = Database.use((db) =>
        db
          .select()
          .from(MessageTable)
          .where(
            and(
              eq(MessageTable.session_id, anchor.session_id),
              sql`(${MessageTable.time_created} < ${anchor.time_created} OR (${MessageTable.time_created} = ${anchor.time_created} AND ${MessageTable.id} <= ${anchor.id}))`,
            ),
          )
          .orderBy(desc(MessageTable.time_created), desc(MessageTable.id))
          .limit(before + 1)
          .all(),
      )
      const afterRows = Database.use((db) =>
        db
          .select()
          .from(MessageTable)
          .where(
            and(
              eq(MessageTable.session_id, anchor.session_id),
              sql`(${MessageTable.time_created} > ${anchor.time_created} OR (${MessageTable.time_created} = ${anchor.time_created} AND ${MessageTable.id} > ${anchor.id}))`,
            ),
          )
          .orderBy(asc(MessageTable.time_created), asc(MessageTable.id))
          .limit(after)
          .all(),
      )

      const messages = [...beforeRows.reverse(), ...afterRows]
      if (messages.length === 0) return { session_id: anchor.session_id, messages: [] }
      const parts = Database.use((db) =>
        db
          .select()
          .from(PartTable)
          .where(
            and(
              eq(PartTable.session_id, anchor.session_id),
              sql`${PartTable.message_id} IN (${sql.join(
                messages.map((m) => sql`${m.id}`),
                sql`, `,
              )})`,
            ),
          )
          .orderBy(asc(PartTable.message_id), asc(PartTable.id))
          .all(),
      )

      const byMessage = new Map<string, typeof parts>()
      for (const p of parts) {
        const list = byMessage.get(p.message_id) ?? []
        list.push(p)
        byMessage.set(p.message_id, list)
      }

      const out: MessageContext[] = messages.map((m) => {
        const role: "user" | "assistant" =
          (m.data as { role?: "user" | "assistant" })?.role === "user" ? "user" : "assistant"
        const partsHere = (byMessage.get(m.id) ?? []).map((p) => {
          const d = p.data as {
            type: string
            text?: string
            tool?: string
            state?: { input?: unknown; output?: unknown; error?: string }
          }
          const text =
            d.type === "text" || d.type === "reasoning"
              ? (d.text ?? "")
              : d.type === "tool"
                ? `tool: ${d.tool ?? ""}\ninput: ${JSON.stringify(d.state?.input ?? {})}\n${d.state?.error ? `error: ${d.state.error}` : `output: ${JSON.stringify(d.state?.output ?? "")}`}`
                : `[${d.type}]`
          return {
            part_id: p.id,
            type: d.type,
            role,
            tool_name: d.type === "tool" ? (d.tool ?? null) : null,
            text,
          }
        })
        return {
          message_id: m.id,
          matched: m.id === input.message_id,
          time_created: m.time_created,
          parts: partsHere,
        }
      })

      return { session_id: anchor.session_id, messages: out }
    })

    // Full-part get: canonical body with code-point-safe UTF-16 paging. Never
    // returns an FTS preview. Media-safe: attachment bytes are not inlined.
    // Reads are scoped to the caller's authorized chat/session/owner.
    const GET_LENGTH_MAX = 8000
    const get = Effect.fn("History.get")(function* (input: Parameters<Interface["get"]>[0]) {
      if (ManagedProvider.enabled()) {
        const remote = (yield* Effect.tryPromise(() => ManagedProvider.historyCall(input.session_id, "get", { messageID: input.message_id, partID: input.part_id, length: input.length, offset: input.offset, ...(input.chat_id ? { chatID: input.chat_id } : {}) })).pipe(Effect.orDie)) as { ok?: boolean; part?: FullPart }
        return remote.ok ? (remote.part as FullPart) : null
      }
      const row = Database.use((db) =>
        db
          .select({
            id: PartTable.id,
            message_id: PartTable.message_id,
            session_id: PartTable.session_id,
            data: PartTable.data,
            time_created: PartTable.time_created,
          })
          .from(PartTable)
          .innerJoin(SessionTable, eq(PartTable.session_id, SessionTable.id))
          .where(
            and(
              eq(PartTable.message_id, input.message_id as MessageID),
              eq(PartTable.id, input.part_id as PartID),
              eq(PartTable.session_id, input.session_id as SessionID),
              eq(SessionTable.project_id, Instance.project.id),
            ),
          )
          .get(),
      )
      if (!row) return null
      const partRow = row
      const d = partRow.data as {
        type: string
        text?: string
        tool?: string
        state?: { input?: unknown; output?: unknown; error?: string }
        filename?: string
        mime?: string
        url?: string
        source?: unknown
      }
      const raw =
        d.type === "text" || d.type === "reasoning"
          ? (d.text ?? "")
          : d.type === "tool"
            ? `tool: ${d.tool ?? ""}\ninput: ${JSON.stringify(d.state?.input ?? {})}\n${
                d.state?.error ? `error: ${d.state.error}` : `output: ${JSON.stringify(d.state?.output ?? "")}`
              }`
            : JSON.stringify(d)
      const budget = Math.min(input.length ?? GET_LENGTH_MAX, GET_LENGTH_MAX)
      const offset = Math.max(0, input.offset ?? 0)
      // Code-point-safe: never slice a surrogate pair in half.
      const page = pageUtf16(raw, offset, budget)
      const attachments: FullPart["attachments"] = []
      if (d.type === "file") {
        attachments.push({
          // Owned attachment identity from the part payload (not the part id).
          asset_id: d.filename ?? d.url ?? String(partRow.id),
          mime_type: d.mime ?? null,
          filename: d.filename ?? null,
          byte_size: Buffer.byteLength(raw, "utf8"),
        })
      }
      const role: FullPart["role"] =
        (partRow.data as { role?: "user" | "assistant" })?.role === "user" ? "user" : "assistant"
      return {
        part_id: String(partRow.id),
        message_id: String(partRow.message_id),
        session_id: String(partRow.session_id),
        type: d.type,
        role,
        tool_name: d.type === "tool" ? (d.tool ?? null) : null,
        text: page.text,
        has_more: page.hasMore,
        next_offset: page.hasMore ? page.end : null,
        attachments,
        time_created: Number(partRow.time_created ?? 0),
      }
    })

    const media = Effect.fn("History.media")(function* (input: Parameters<Interface["media"]>[0]) {
      if (ManagedProvider.enabled()) {
        const part = yield* get({
          session_id: input.session_id,
          chat_id: input.chat_id,
          message_id: input.message_id,
          part_id: input.part_id,
        })
        const assetID = input.asset_id ?? part?.attachments[0]?.asset_id
        if (!assetID) return []
        const remote = (yield* Effect.tryPromise(() => ManagedProvider.historyCall(input.session_id, "media", {
          assetID,
          messageID: input.message_id,
          partID: input.part_id,
          ...(input.chat_id ? { chatID: input.chat_id } : {}),
        })).pipe(Effect.orDie)) as { ok?: boolean; attachments?: FullPart["attachments"] }
        return remote.ok ? (remote.attachments ?? []) : []
      }
      const part = yield* get({
        session_id: input.session_id,
        message_id: input.message_id,
        part_id: input.part_id,
      })
      return part?.attachments ?? []
    })

    return Service.of({ search, around, get, media })
  }),
)
