import { index, integer, primaryKey, sqliteTable, text } from "drizzle-orm/sqlite-core"
import type * as GoalState from "./goal-state"

export const GoalStateTable = sqliteTable(
  "goal_state",
  {
    owner: text().notNull(),
    workspace: text().notNull(),
    project: text().notNull(),
    session_id: text().notNull(),
    revision: integer().notNull(),
    data: text({ mode: "json" }).$type<GoalState.Envelope>().notNull(),
    time_updated: integer().notNull(),
  },
  (table) => [
    primaryKey({ columns: [table.owner, table.workspace, table.project, table.session_id] }),
    index("goal_state_active_scope_idx").on(table.owner, table.workspace, table.project),
  ],
)

export const GoalJournalTable = sqliteTable(
  "goal_journal",
  {
    id: text().primaryKey(),
    owner: text().notNull(),
    workspace: text().notNull(),
    project: text().notNull(),
    session_id: text().notNull(),
    goal_id: text().notNull(),
    revision: integer().notNull(),
    event_type: text().notNull(),
    reason_code: text(),
    data: text({ mode: "json" }).$type<GoalState.JournalEvent>().notNull(),
    time_created: integer().notNull(),
  },
  (table) => [
    index("goal_journal_scope_idx").on(table.owner, table.workspace, table.project, table.session_id, table.time_created),
    index("goal_journal_goal_idx").on(table.goal_id, table.revision),
  ],
)

export * as GoalSql from "./goal.sql"
