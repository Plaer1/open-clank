import { sqliteTable, text, integer, index, primaryKey, foreignKey } from "drizzle-orm/sqlite-core"
import { MessageTable, SessionTable } from "../session/session.sql"
import type { SessionID, MessageID } from "../session/schema"

// 所有来源的输入均引用受理时写入的 user 消息；content 仅供旧格式启动转换。
export const InboxTable = sqliteTable(
  "inbox",
  {
    id: text().primaryKey(),
    receiver_session_id: text()
      .$type<SessionID>()
      .notNull()
      .references(() => SessionTable.id, { onDelete: "cascade" }),
    receiver_actor_id: text().notNull().default("main"),
    sender_session_id: text().$type<SessionID>(),
    sender_actor_id: text(),
    type: text().notNull().default("text"),
    /** Legacy-format rows only; new accepts never store the body here. */
    content: text({ mode: "json" }).$type<{ text?: string } | null>(),
    input_message_id: text().$type<MessageID>(),
    state: text()
      .$type<"accepted" | "claimed" | "settled" | "cancelled" | "rejected">()
      .notNull()
      .default("accepted"),
    epoch: integer().notNull().default(0),
    run_id: integer(),
    claim_frontier: text().$type<MessageID>(),
    consumed: integer({ mode: "boolean" }).notNull().default(false),
    outcome: text().$type<"success" | "assistant_error" | "interrupted" | "never_ran">(),
    delivery_message_id: text().$type<MessageID>(),
    error: text(),
    idempotency_key: text().notNull().default(""),
    created_at: integer().notNull(),
    time_updated: integer().notNull(),
  },
  (t) => [
    index("inbox_receiver_idx").on(t.receiver_session_id, t.receiver_actor_id, t.id),
    index("inbox_state_idx").on(t.receiver_session_id, t.receiver_actor_id, t.state),
    index("inbox_created_idx").on(t.created_at),
    // Partial unique (non-empty keys only) lives in migration SQL — empty
    // string means "no idempotency key" and must not collide.
  ],
)

/** Per-lane consumption frontier + input revision. */
export const InboxLaneStateTable = sqliteTable(
  "inbox_lane_state",
  {
    session_id: text()
      .$type<SessionID>()
      .notNull()
      .references(() => SessionTable.id, { onDelete: "cascade" }),
    agent_id: text().notNull().default("main"),
    consumed_frontier: text().$type<MessageID>(),
    input_revision: integer().notNull().default(0),
    time_updated: integer().notNull(),
  },
  (t) => [primaryKey({ columns: [t.session_id, t.agent_id] })],
)

/** Session-wide cancel epoch — abort increments; old-epoch rows are never claimed. */
export const InboxSessionEpochTable = sqliteTable("inbox_session_epoch", {
  session_id: text()
    .$type<SessionID>()
    .primaryKey()
    .references(() => SessionTable.id, { onDelete: "cascade" }),
  epoch: integer().notNull().default(0),
  time_updated: integer().notNull(),
})

export type InboxRow = typeof InboxTable.$inferSelect

export interface UserInputTurnPolicy {
  version: 1
  mode: "agent" | "chat" | "plan"
  allowBash: boolean | null
  allowWebSearch: boolean | null
  useWeb: boolean
  useResearch: boolean
  compareMode: boolean
  incognito: boolean
  noMemory: boolean
}

/** Server-derived admission evidence. A null allowedTools means no positive
 * allowlist was applied; disabledTools/hiddenTools remain explicit denies. */
export interface UserInputEffectivePolicyCeiling {
  version: 1
  blockAllToolCalls: boolean
  disabledTools: string[]
  hiddenTools: string[]
  allowedTools: string[] | null
}

export const UserInputReceiptTable = sqliteTable(
  "user_input_receipt",
  {
    owner: text().notNull(),
    stable_chat_id: text().notNull(),
    map_revision: integer().notNull(),
    mapping_revision: integer().notNull(),
    workspace_revision: integer().notNull(),
    request_id: text().notNull(),
    session_id: text()
      .$type<SessionID>()
      .notNull()
      .references(() => SessionTable.id, { onDelete: "cascade" }),
    actor_id: text().notNull(),
    admission_status: text().$type<"pending" | "running" | "idle">().notNull(),
    execution_revision: integer().notNull(),
    claimed_execution_revision: integer(),
    origin_turn_id: text(),
    payload_hash: text().notNull(),
    text: text().notNull(),
    attachment_refs: text({ mode: "json" }).$type<string[]>().notNull(),
    attachment_upload_ids: text({ mode: "json" }).$type<string[]>().notNull().default([]),
    attachment_parts: text({ mode: "json" }).$type<Array<{ type: "text"; text: string } | { type: "file"; mime: string; filename?: string; url: string }>>().notNull().default([]),
    question_refs: text({ mode: "json" }).$type<string[]>().notNull(),
    model_request_options: text({ mode: "json" }).$type<Record<string, unknown>>().notNull().default({}),
    turn_policy: text({ mode: "json" }).$type<UserInputTurnPolicy | null>(),
    effective_policy_ceiling: text({ mode: "json" }).$type<UserInputEffectivePolicyCeiling | null>(),
    mode: text().$type<"steer" | "after-turn">().notNull(),
    state: text()
      .$type<"persisted" | "pending-boundary" | "consumed" | "cancelled" | "failed">()
      .notNull(),
    failure_reason: text(),
    message_id: text().references(() => MessageTable.id, { onDelete: "set null" }),
    created_at: integer().notNull(),
    updated_at: integer().notNull(),
    consumed_at: integer(),
  },
  (table) => [
    primaryKey({ columns: [table.owner, table.stable_chat_id, table.request_id] }),
    index("user_input_receipt_pending_idx").on(table.session_id, table.actor_id, table.state, table.created_at),
  ],
)

/** Durable, request-bound deliberate resume attempts. A retry of a handoff ID
 * acknowledges its original claim; a new ID is allowed only after native
 * execution settlement proves the earlier attempt ended before consumption. */
export const UserInputHandoffTable = sqliteTable(
  "user_input_handoff",
  {
    owner: text().notNull(),
    stable_chat_id: text().notNull(),
    request_id: text().notNull(),
    handoff_id: text().notNull(),
    session_id: text().$type<SessionID>().notNull().references(() => SessionTable.id, { onDelete: "cascade" }),
    actor_id: text().notNull(),
    root_operation_id: text().notNull(),
    expected_execution_revision: integer().notNull(),
    claimed_execution_revision: integer().notNull(),
    outcome: text().$type<"claimed" | "running" | "settled-before-consumption" | "consumed">().notNull(),
    created_at: integer().notNull(),
    updated_at: integer().notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.owner, table.stable_chat_id, table.request_id, table.handoff_id] }),
    index("user_input_handoff_actor_idx").on(table.session_id, table.actor_id, table.claimed_execution_revision),
    foreignKey({
      columns: [table.owner, table.stable_chat_id, table.request_id],
      foreignColumns: [UserInputReceiptTable.owner, UserInputReceiptTable.stable_chat_id, UserInputReceiptTable.request_id],
    }).onDelete("cascade"),
  ],
)

/** Durable optional clarifications. Blocking questions continue to use the
 * existing Deferred flow; this row is the recovery authority for questions
 * whose originating model call has already continued. */
export const QuestionRequestTable = sqliteTable(
  "question_request",
  {
    owner: text().notNull(),
    stable_chat_id: text().notNull(),
    request_id: text().notNull(),
    session_id: text().$type<SessionID>().notNull().references(() => SessionTable.id, { onDelete: "cascade" }),
    actor_id: text().notNull(),
    origin_turn_id: text().references(() => MessageTable.id, { onDelete: "set null" }),
    tool_message_id: text().references(() => MessageTable.id, { onDelete: "set null" }),
    tool_call_id: text(),
    question_revision: integer().notNull(),
    plan_revision: integer(),
    blocking: integer().notNull().default(0),
    questions: text({ mode: "json" }).$type<Array<Record<string, unknown>>>().notNull(),
    payload_hash: text().notNull(),
    state: text().$type<"pending" | "answer-queued" | "answered" | "dismissed" | "stale" | "cancelled">().notNull(),
    answer_payload: text({ mode: "json" }).$type<{ answers?: string[][] } | null>(),
    answer_disposition: text().$type<"answered" | "dismissed" | null>(),
    answer_request_id: text(),
    answer_message_id: text().references(() => MessageTable.id, { onDelete: "set null" }),
    created_at: integer().notNull(),
    updated_at: integer().notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.owner, table.stable_chat_id, table.request_id] }),
    index("question_request_actor_state_idx").on(table.session_id, table.actor_id, table.state, table.created_at),
    foreignKey({
      columns: [table.owner, table.stable_chat_id, table.answer_request_id],
      foreignColumns: [UserInputReceiptTable.owner, UserInputReceiptTable.stable_chat_id, UserInputReceiptTable.request_id],
    }),
  ],
)

export type UserInputReceiptRow = typeof UserInputReceiptTable.$inferSelect
export type InboxLaneStateRow = typeof InboxLaneStateTable.$inferSelect
export type InboxSessionEpochRow = typeof InboxSessionEpochTable.$inferSelect
