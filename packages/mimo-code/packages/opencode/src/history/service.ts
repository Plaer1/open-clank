import { Context, Effect, Layer } from "effect"
import { and, asc, desc, eq, sql } from "drizzle-orm"
import { Database } from "../storage"
import { MessageTable, PartTable } from "../session/session.sql"
import type { MessageID, PartID } from "../session/schema"
import { Config } from "../config"
import { Bus } from "../bus"
import { Instance } from "../project/instance"
import { buildFtsQuery } from "./fts-query"
import type { Kind } from "./extract"
import { layer as writerLayer, Service as WriterService } from "./writer"
import { layer as backfillLayer, Service as BackfillService } from "./backfill"

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
    message_id: string
    before?: number
    after?: number
  }) => Effect.Effect<{ session_id: string; messages: MessageContext[] }>

  /** Full-part get. Reads the canonical part body, not an FTS preview. */
  readonly get: (input: {
    message_id: string
    part_id: string
    length?: number
    offset?: number
  }) => Effect.Effect<FullPart | null>

  /** Explicit single-attachment locator metadata (owned assets only). */
  readonly media: (input: {
    message_id: string
    part_id: string
  }) => Effect.Effect<FullPart["attachments"]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/History") {}

const HARD_CAP = 50

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
      const before = input.before ?? 5
      const after = input.after ?? 5
      const anchor = Database.use((db) =>
        db
          .select({
            id: MessageTable.id,
            session_id: MessageTable.session_id,
            time_created: MessageTable.time_created,
          })
          .from(MessageTable)
          .where(eq(MessageTable.id, input.message_id as MessageID))
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

    // Full-part get: canonical body with UTF-16 paging. Never returns an FTS
    // preview. Media-safe: attachment bytes are not inlined.
    const GET_LENGTH_MAX = 8000
    const get = Effect.fn("History.get")(function* (input: Parameters<Interface["get"]>[0]) {
      const row = Database.use((db) =>
        db
          .select()
          .from(PartTable)
          .where(
            and(
              eq(PartTable.message_id, input.message_id as MessageID),
              eq(PartTable.id, input.part_id as PartID),
            ),
          )
          .get(),
      )
      if (!row) return null
      const d = row.data as {
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
      // Python/JS string slicing is code-point safe; surrogate pairs stay whole.
      const slice = raw.slice(offset, offset + budget)
      const hasMore = offset + slice.length < raw.length
      const attachments: FullPart["attachments"] = []
      if (d.type === "file") {
        attachments.push({
          asset_id: row.id,
          mime_type: d.mime ?? null,
          filename: d.filename ?? null,
          byte_size: raw.length,
        })
      }
      const role: FullPart["role"] =
        (row.data as { role?: "user" | "assistant" })?.role === "user" ? "user" : "assistant"
      return {
        part_id: String(row.id),
        message_id: String(row.message_id),
        session_id: String(row.session_id),
        type: d.type,
        role,
        tool_name: d.type === "tool" ? (d.tool ?? null) : null,
        text: slice,
        has_more: hasMore,
        next_offset: hasMore ? offset + slice.length : null,
        attachments,
        time_created: 0,
      }
    })

    const media = Effect.fn("History.media")(function* (input: Parameters<Interface["media"]>[0]) {
      const part = yield* get({ message_id: input.message_id, part_id: input.part_id })
      return part?.attachments ?? []
    })

    return Service.of({ search, around, get, media })
  }),
)
