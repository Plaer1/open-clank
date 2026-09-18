import { and, asc, count, Database, eq } from "@/storage"
import { InstanceState } from "@/effect"
import { memorySessionScope } from "@/memory/session-scope"
import { Effect } from "effect"
import { SessionID } from "./schema"
import { GoalJournalTable, GoalStateTable } from "./goal.sql"
import * as GoalState from "./goal-state"

export type Scope = {
  owner: string
  workspace: string
  project: string
  sessionID: SessionID
}

export function scope(sessionID: SessionID) {
  return Effect.gen(function* () {
    const context = yield* InstanceState.context
    const workspaceContext = yield* InstanceState.workspaceID
    const memoryScope = memorySessionScope(sessionID)
    const managed = process.env.OPEN_CLANK_MANAGED === "1"
    const owner =
      memoryScope?.owner ||
      process.env.OPEN_CLANK_OWNER?.trim() ||
      process.env.FM_OWNER?.trim() ||
      (managed ? "" : "local")
    const workspace =
      memoryScope?.workspaceId || workspaceContext || process.env.FM_WORKSPACE_ID?.trim() || (managed ? "" : "local")
    if (!owner || !workspace || !context.project.id) throw new Error("Goal scope is incomplete")
    return {
      owner,
      workspace,
      project: context.project.id,
      sessionID,
    } satisfies Scope
  })
}

function whereScope(input: Scope) {
  return and(
    eq(GoalStateTable.owner, input.owner),
    eq(GoalStateTable.workspace, input.workspace),
    eq(GoalStateTable.project, input.project),
    eq(GoalStateTable.session_id, input.sessionID),
  )
}

function emptyEnvelope(): GoalState.Envelope {
  return { revision: 0, active: undefined, queue: [], history: [] }
}

function assertScoped(input: Scope, envelope: GoalState.Envelope) {
  for (const record of [...(envelope.active ? [envelope.active] : []), ...envelope.queue, ...envelope.history]) {
    if (
      record.owner !== input.owner ||
      record.workspace !== input.workspace ||
      record.project !== input.project ||
      record.sessionID !== input.sessionID
    ) {
      throw new Error("Goal record does not match the authenticated scope")
    }
  }
  return envelope
}

export function read(input: Scope) {
  return Database.use((db) => {
    const row = db.select().from(GoalStateTable).where(whereScope(input)).get()
    if (!row) return emptyEnvelope()
    return assertScoped(input, GoalState.Envelope.parse(row.data))
  })
}

export type Mutation<T> = {
  envelope: GoalState.Envelope
  events?: GoalState.JournalEvent[]
  changed?: boolean
  value: T
}

/**
 * One immediate SQLite transaction is the CAS boundary for state and journal.
 * A second worker either observes this revision or conflicts; it cannot run a
 * continuation from a half-written lifecycle mutation.
 */
export function mutate<T>(input: Scope, fn: (current: GoalState.Envelope) => Mutation<T>) {
  const result = Database.transaction(
    (db) => {
      const currentRow = db.select().from(GoalStateTable).where(whereScope(input)).get()
      const current = currentRow ? assertScoped(input, GoalState.Envelope.parse(currentRow.data)) : emptyEnvelope()
      const mutation = fn(current)
      if (mutation.changed === false) return mutation.value as never
      if (!mutation.events?.length) throw new Error("Goal state mutations require a journal event")
      const next = GoalState.Envelope.parse({
        ...mutation.envelope,
        revision: current.revision + 1,
      })
      const now = Date.now()

      if (currentRow) {
        db.update(GoalStateTable)
          .set({
            revision: next.revision,
            data: next,
            time_updated: now,
          })
          .where(and(whereScope(input), eq(GoalStateTable.revision, current.revision)))
          .run()
        const written = db
          .select({ revision: GoalStateTable.revision })
          .from(GoalStateTable)
          .where(whereScope(input))
          .get()
        if (written?.revision !== next.revision) {
          throw new GoalState.ConflictError(current.revision, written?.revision ?? 0)
        }
      } else {
        db.insert(GoalStateTable)
          .values({
            owner: input.owner,
            workspace: input.workspace,
            project: input.project,
            session_id: input.sessionID,
            revision: next.revision,
            data: next,
            time_updated: now,
          })
          .run()
      }

      for (const draft of mutation.events) {
        const event = GoalState.withSnapshot(draft, next)
        db.insert(GoalJournalTable)
          .values({
            id: event.id,
            owner: input.owner,
            workspace: input.workspace,
            project: input.project,
            session_id: input.sessionID,
            goal_id: event.goalID,
            revision: event.revision,
            event_type: event.type,
            reason_code: event.reasonCode,
            data: event,
            time_created: event.createdAt,
          })
          .run()
      }
      return mutation.value as never
    },
    { behavior: "immediate" },
  )
  return result as T
}

export function journal(input: Scope) {
  return Database.use((db) =>
    db
      .select({ data: GoalJournalTable.data })
      .from(GoalJournalTable)
      .where(
        and(
          eq(GoalJournalTable.owner, input.owner),
          eq(GoalJournalTable.workspace, input.workspace),
          eq(GoalJournalTable.project, input.project),
          eq(GoalJournalTable.session_id, input.sessionID),
        ),
      )
      .orderBy(asc(GoalJournalTable.time_created), asc(GoalJournalTable.revision))
      .all()
      .map((row) => GoalState.JournalEvent.parse(row.data))
      .sort(
        (left, right) =>
          (left.envelopeRevision ?? 0) - (right.envelopeRevision ?? 0) ||
          left.createdAt - right.createdAt ||
          left.revision - right.revision,
      ),
  )
}

export function replay(input: Scope) {
  const events = journal(input)
  const replayed = GoalState.replayJournal(events)
  if (!replayed) return undefined
  const current = GoalState.privacySafeEnvelope(read(input))
  if (GoalState.envelopeHash(replayed) !== GoalState.envelopeHash(current)) {
    throw new Error("Goal journal replay diverges from the durable goal state")
  }
  return replayed
}

export function analytics(input: Scope) {
  return Database.use((db) =>
    Object.fromEntries(
      db
        .select({
          type: GoalJournalTable.event_type,
          total: count(),
        })
        .from(GoalJournalTable)
        .where(
          and(
            eq(GoalJournalTable.owner, input.owner),
            eq(GoalJournalTable.workspace, input.workspace),
            eq(GoalJournalTable.project, input.project),
            eq(GoalJournalTable.session_id, input.sessionID),
          ),
        )
        .groupBy(GoalJournalTable.event_type)
        .all()
        .map((row) => [row.type, row.total]),
    ),
  )
}
