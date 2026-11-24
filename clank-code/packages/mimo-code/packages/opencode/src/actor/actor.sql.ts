import { sqliteTable, text, integer, index, primaryKey, foreignKey } from "drizzle-orm/sqlite-core"
import { SessionTable } from "../session/session.sql"
import type { SessionID, MessageID } from "../session/schema"
import { Timestamps } from "../storage/schema.sql"

export const ActorRegistryTable = sqliteTable(
  "actor_registry",
  {
    session_id: text()
      .$type<SessionID>()
      .notNull()
      .references(() => SessionTable.id, { onDelete: "cascade" }),
    actor_id: text().notNull(),
    mode: text().$type<"peer" | "subagent" | "main">().notNull(),
    parent_actor_id: text(),
    status: text().$type<"pending" | "running" | "idle">().notNull(),
    last_outcome: text().$type<"success" | "failure" | "cancelled">(),
    // Monotonic execution identity. Actor rows are reusable across resumes,
    // so terminal state must always be compared with the revision that wrote it.
    execution_revision: integer().notNull().default(0),
    terminal_notification_revision: integer(),
    terminal_notification_outcome: text().$type<"success" | "failure" | "cancelled">(),
    lifecycle: text().$type<"ephemeral" | "persistent">().notNull(),
    agent: text().notNull(),
    description: text().notNull(),
    context_mode: text().$type<"none" | "state" | "full">().notNull(),
    context_watermark: text().$type<MessageID>(),
    background: integer({ mode: "boolean" }).notNull(),
    tools: text({ mode: "json" }).$type<readonly string[] | "INHERIT">(),
    // The requested ref is retained separately from the resolved route so the
    // UI can explain inheritance/group resolution without guessing from IDs.
    requested_model: text(),
    effective_model: text(),
    model_request_options: text({ mode: "json" }).$type<import("./schema").ActorModelOptions>(),
    host_account_id: text(),
    provider_grant_id: text(),
    grant_revision: integer(),
    credential_revision: integer(),
    chat_id: text(),
    workspace_id: text(),
    cwd: text(),
    goal_id: text(),
    last_turn_time: integer().notNull(),
    turn_count: integer().notNull().default(0),
    last_error: text(),
    instance_id: text().notNull(),
    time_completed: integer(),
    ...Timestamps,
  },
  (table) => [
    primaryKey({ columns: [table.session_id, table.actor_id] }),
    index("actor_registry_session_agent_idx").on(table.session_id, table.agent),
    index("actor_registry_session_parent_idx").on(table.session_id, table.parent_actor_id),
    index("actor_registry_status_idx").on(table.status),
    index("actor_registry_status_last_turn_idx").on(table.status, table.last_turn_time),
  ],
)

export const ActorForkContextTable = sqliteTable(
  "actor_fork_context",
  {
    session_id: text().$type<SessionID>().notNull(),
    actor_id: text().notNull(),
    format_version: integer().notNull(),
    source_session_id: text().$type<SessionID>().notNull(),
    source_actor_id: text().notNull(),
    source_watermark_message_id: text().$type<MessageID>().notNull(),
    model_provider_id: text().notNull(),
    model_id: text().notNull(),
    snapshot_json: text().notNull(),
    snapshot_sha256: text().notNull(),
    snapshot_bytes: integer().notNull(),
    time_created: integer().notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.session_id, table.actor_id] }),
    foreignKey({ columns: [table.session_id, table.actor_id], foreignColumns: [ActorRegistryTable.session_id, ActorRegistryTable.actor_id] }).onDelete("cascade"),
    foreignKey({ columns: [table.source_session_id, table.source_actor_id], foreignColumns: [ActorRegistryTable.session_id, ActorRegistryTable.actor_id] }).onDelete("cascade"),
    foreignKey({ columns: [table.source_session_id], foreignColumns: [SessionTable.id] }).onDelete("cascade"),
    index("actor_fork_context_source_idx").on(table.source_session_id, table.source_actor_id),
  ],
)
