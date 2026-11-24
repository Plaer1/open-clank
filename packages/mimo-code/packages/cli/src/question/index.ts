import { createHash } from "node:crypto"
import { Deferred, Effect, Layer, Schema, Context } from "effect"
import { Bus } from "@/bus"
import { BusEvent } from "@/bus/bus-event"
import { InstanceState } from "@/effect"
import { SessionID, MessageID } from "@/session/schema"
import { zod } from "@/util/effect-zod"
import { Flag } from "@/flag/flag"
import { Log } from "@/util"
import { withStatics } from "@/util/schema"
import { QuestionID } from "./schema"
import { Database, and, count, desc, eq, inArray, lt, or } from "@/storage"
import { ActorRegistryTable } from "@/actor/actor.sql"
import { MessageTable, PartTable } from "@/session/session.sql"
import { QuestionRequestTable, UserInputReceiptTable } from "@/inbox/inbox.sql"
import { ManagedProvider } from "@/acp/managed-provider"

const log = Log.create({ service: "question" })

// Schemas

export class Option extends Schema.Class<Option>("QuestionOption")({
  label: Schema.String.annotate({
    description: "Display text (1-5 words, concise)",
  }),
  description: Schema.String.annotate({
    description: "Explanation of choice",
  }),
}) {
  static readonly zod = zod(this)
}

const base = {
  question: Schema.String.annotate({
    description: "Complete question",
  }),
  header: Schema.String.annotate({
    description: "Very short label (max 30 chars)",
  }),
  options: Schema.Array(Option).annotate({
    description: "Available choices",
  }),
  multiple: Schema.optional(Schema.Boolean).annotate({
    description: "Allow selecting multiple choices",
  }),
}

export class Info extends Schema.Class<Info>("QuestionInfo")({
  ...base,
  custom: Schema.optional(Schema.Boolean).annotate({
    description: "Allow typing a custom answer (default: true)",
  }),
  key: Schema.optional(Schema.String).annotate({
    description: "i18n key for client-side translation (e.g. plan_exit). When set, clients may translate question/options text using this key as a namespace.",
  }),
  params: Schema.optional(Schema.Record(Schema.String, Schema.String)).annotate({
    description: "Template parameters for i18n interpolation (e.g. { plan: '.openclank/plans/...' })",
  }),
}) {
  static readonly zod = zod(this)
}

export class Prompt extends Schema.Class<Prompt>("QuestionPrompt")(base) {
  static readonly zod = zod(this)
}

export class Tool extends Schema.Class<Tool>("QuestionTool")({
  messageID: MessageID,
  callID: Schema.String,
}) {
  static readonly zod = zod(this)
}

export class Request extends Schema.Class<Request>("QuestionRequest")({
  id: QuestionID,
  sessionID: SessionID,
  actorID: Schema.optional(Schema.String),
  originTurnID: Schema.optional(MessageID),
  questionRevision: Schema.optional(Schema.Number),
  blocking: Schema.optional(Schema.Boolean),
  questions: Schema.Array(Info).annotate({
    description: "Questions to ask",
  }),
  tool: Schema.optional(Tool),
}) {
  static readonly zod = zod(this)
}

export const Answer = Schema.Array(Schema.String)
  .annotate({ identifier: "QuestionAnswer" })
  .pipe(withStatics((s) => ({ zod: zod(s) })))
export type Answer = Schema.Schema.Type<typeof Answer>

export class Reply extends Schema.Class<Reply>("QuestionReply")({
  answers: Schema.Array(Answer).annotate({
    description: "User answers in order of questions (each answer is an array of selected labels)",
  }),
}) {
  static readonly zod = zod(this)
}

class Replied extends Schema.Class<Replied>("QuestionReplied")({
  sessionID: SessionID,
  requestID: QuestionID,
  answers: Schema.Array(Answer),
}) {}

class Rejected extends Schema.Class<Rejected>("QuestionRejected")({
  sessionID: SessionID,
  requestID: QuestionID,
}) {}

export const Event = {
  Asked: BusEvent.define("question.asked", Request.zod),
  Replied: BusEvent.define("question.replied", zod(Replied)),
  Rejected: BusEvent.define("question.rejected", zod(Rejected)),
}

export class RejectedError extends Schema.TaggedErrorClass<RejectedError>()("QuestionRejectedError", {}) {
  override get message() {
    return "The user dismissed this question"
  }
}

interface PendingEntry {
  info: Request
  deferred: Deferred.Deferred<ReadonlyArray<Answer>, RejectedError>
}

interface State {
  pending: Map<QuestionID, PendingEntry>
  // When true the question tool stays visible but ask() is never reached:
  // the tool returns a [Never-Ask] directive so the model re-picks the best
  // option for headless execution itself. Toggleable at runtime.
  neverAsk: boolean
}

// Service

export interface Interface {
  readonly ask: (input: {
    sessionID: SessionID
    questions: ReadonlyArray<Info>
    tool?: Tool
    abortSignal?: AbortSignal
  }) => Effect.Effect<ReadonlyArray<Answer>, RejectedError>
  readonly askOptional: (input: {
    sessionID: SessionID
    actorID: string
    executionRevision?: number
    originTurnID: MessageID
    questions: ReadonlyArray<Info>
    tool: Tool
  }) => Effect.Effect<Request, Error>
  readonly reply: (input: { requestID: QuestionID; answers: ReadonlyArray<Answer> }) => Effect.Effect<void>
  readonly reject: (requestID: QuestionID) => Effect.Effect<void>
  readonly list: () => Effect.Effect<ReadonlyArray<Request>>
  readonly neverAsk: () => Effect.Effect<boolean>
  readonly setNeverAsk: (enabled: boolean) => Effect.Effect<void>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Question") {}

/** Persist after the caller has read and matched the authenticated managed binding. */
export function persistOptionalQuestion(input: {
  owner: string
  stableChatID: string
  sessionID: SessionID
  actorID: string
  originTurnID: MessageID
  questions: ReadonlyArray<Info>
  tool: Tool
}): Request {
  const id = QuestionID.ascending()
  const revision = 1
  if (input.questions.length < 1 || input.questions.length > 16) throw new Error("optional question request exceeds the 1–16 question limit")
  const questions = input.questions.map((question) => ({ ...question }))
  const payloadHash = createHash("sha256").update(JSON.stringify({
    sessionID: input.sessionID,
    actorID: input.actorID,
    originTurnID: input.originTurnID,
    tool: input.tool,
    questionRevision: revision,
    questions,
  })).digest("hex")
  Database.transaction((db) => {
    const actor = db.select().from(ActorRegistryTable).where(and(
      eq(ActorRegistryTable.session_id, input.sessionID),
      eq(ActorRegistryTable.actor_id, input.actorID),
    )).get()
    if (!actor || actor.chat_id !== input.stableChatID) throw new Error("question actor is not bound to the authenticated chat")
    const outstanding = db.select({ n: count() }).from(QuestionRequestTable).where(and(
      eq(QuestionRequestTable.owner, input.owner),
      eq(QuestionRequestTable.stable_chat_id, input.stableChatID),
      eq(QuestionRequestTable.session_id, input.sessionID),
      eq(QuestionRequestTable.actor_id, input.actorID),
      inArray(QuestionRequestTable.state, ["pending", "answer-queued", "stale"]),
    )).get()?.n ?? 0
    if (outstanding >= 32) throw new Error("optional question request limit reached for this actor")
    const origin = db.select({ data: MessageTable.data }).from(MessageTable).where(and(
      eq(MessageTable.session_id, input.sessionID),
      eq(MessageTable.agent_id, input.actorID),
      eq(MessageTable.id, input.originTurnID),
    )).get()
    if (!origin || origin.data.role !== "user") throw new Error("question origin is not a user turn in this actor slice")
    const toolPart = db.select({ part: PartTable.data }).from(PartTable).innerJoin(
      MessageTable,
      eq(PartTable.message_id, MessageTable.id),
    ).where(and(
      eq(PartTable.session_id, input.sessionID),
      eq(PartTable.message_id, input.tool.messageID),
      eq(MessageTable.agent_id, input.actorID),
    )).all().some(({ part }) => part.type === "tool" && (part as { callID?: string }).callID === input.tool.callID)
    if (!toolPart) throw new Error("question origin tool call is unavailable")
    const now = Date.now()
    const row: typeof QuestionRequestTable.$inferInsert = {
      owner: input.owner,
      stable_chat_id: input.stableChatID,
      request_id: String(id),
      session_id: input.sessionID,
      actor_id: input.actorID,
      origin_turn_id: input.originTurnID,
      tool_message_id: input.tool.messageID,
      tool_call_id: input.tool.callID,
      question_revision: revision,
      plan_revision: null,
      blocking: 0,
      questions: questions as Array<Record<string, unknown>>,
      payload_hash: payloadHash,
      state: "pending",
      answer_payload: null,
      answer_disposition: null,
      answer_request_id: null,
      answer_message_id: null,
      created_at: now,
      updated_at: now,
    }
    db.insert(QuestionRequestTable).values(row).run()
  }, { behavior: "immediate" })
  return Schema.decodeUnknownSync(Request)({
    id,
    sessionID: input.sessionID,
    actorID: input.actorID,
    originTurnID: input.originTurnID,
    questionRevision: revision,
    blocking: false,
    questions,
    tool: input.tool,
  })
}

export function listForActor(input: {
  owner: string
  stableChatID: string
  sessionID: SessionID
  actorID: string
  limit: number
  before?: string
  requestID?: string
}) {
  if (!Number.isSafeInteger(input.limit) || input.limit < 1 || input.limit > 100) throw new Error("question page limit is invalid")
  return Database.use((db) => {
    const actor = db.select().from(ActorRegistryTable).where(and(
      eq(ActorRegistryTable.session_id, input.sessionID),
      eq(ActorRegistryTable.actor_id, input.actorID),
    )).get()
    if (!actor || actor.chat_id !== input.stableChatID) throw new Error("question actor is not bound to the authenticated chat")
    const baseScope = and(
      eq(QuestionRequestTable.owner, input.owner),
      eq(QuestionRequestTable.stable_chat_id, input.stableChatID),
      eq(QuestionRequestTable.session_id, input.sessionID),
      eq(QuestionRequestTable.actor_id, input.actorID),
      ...(input.requestID ? [eq(QuestionRequestTable.request_id, input.requestID)] : []),
    )
    const scope = and(baseScope, inArray(QuestionRequestTable.state, ["pending", "answer-queued", "stale"]))
    let before
    if (input.before) {
      const cursor = db.select({ request_id: QuestionRequestTable.request_id, created_at: QuestionRequestTable.created_at })
        .from(QuestionRequestTable).where(and(baseScope, eq(QuestionRequestTable.request_id, input.before))).get()
      if (!cursor) throw new Error("question cursor is unavailable in this actor scope")
      before = and(scope, or(
        lt(QuestionRequestTable.created_at, cursor.created_at),
        and(eq(QuestionRequestTable.created_at, cursor.created_at), lt(QuestionRequestTable.request_id, cursor.request_id)),
      ))
    }
    const rows = db.select().from(QuestionRequestTable).where(before ?? scope)
      .orderBy(desc(QuestionRequestTable.created_at), desc(QuestionRequestTable.request_id)).limit(input.limit).all()
    return {
      items: rows.map((row) => ({
        requestID: row.request_id,
        questionRef: `${row.request_id}@${row.question_revision}`,
        sessionID: row.session_id,
        actorID: row.actor_id,
        originTurnID: row.origin_turn_id,
        tool: { messageID: row.tool_message_id, callID: row.tool_call_id },
        questionRevision: row.question_revision,
        planRevision: row.plan_revision,
        blocking: row.blocking === 1,
        questions: row.questions,
        state: row.state,
        answerDisposition: row.answer_disposition,
        createdAt: row.created_at,
        updatedAt: row.updated_at,
      })),
      nextCursor: rows.length === input.limit ? rows[rows.length - 1]?.request_id ?? null : null,
    }
  })
}

export function answersForReceipt(input: { owner: string; stableChatID: string; requestID: string }) {
  return Database.use((db) => {
    const receipt = db.select({ question_refs: UserInputReceiptTable.question_refs }).from(UserInputReceiptTable).where(and(
      eq(UserInputReceiptTable.owner, input.owner),
      eq(UserInputReceiptTable.stable_chat_id, input.stableChatID),
      eq(UserInputReceiptTable.request_id, input.requestID),
    )).get()
    if (!receipt) return []
    const rows = db.select().from(QuestionRequestTable).where(and(
      eq(QuestionRequestTable.owner, input.owner),
      eq(QuestionRequestTable.stable_chat_id, input.stableChatID),
      eq(QuestionRequestTable.answer_request_id, input.requestID),
    )).all()
    const byRef = new Map(rows.map((row) => [`${row.request_id}@${row.question_revision}`, row]))
    return receipt.question_refs.flatMap((ref) => {
      const row = byRef.get(ref)
      return row ? [{
        requestID: row.request_id,
        questionRevision: row.question_revision,
        disposition: row.answer_disposition,
        ...(row.answer_payload?.answers ? { answers: row.answer_payload.answers } : {}),
      }] : []
    })
  })
}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const state = yield* InstanceState.make<State>(
      Effect.fn("Question.state")(function* () {
        const state = {
          pending: new Map<QuestionID, PendingEntry>(),
          neverAsk: Flag.OPENCLANK_NEVER_ASK,
        }

        yield* Effect.addFinalizer(() =>
          Effect.gen(function* () {
            for (const item of state.pending.values()) {
              yield* Deferred.fail(item.deferred, new RejectedError())
            }
            state.pending.clear()
          }),
        )

        return state
      }),
    )

    const ask = Effect.fn("Question.ask")(function* (input: {
      sessionID: SessionID
      questions: ReadonlyArray<Info>
      tool?: Tool
      abortSignal?: AbortSignal
    }) {
      const pending = (yield* InstanceState.get(state)).pending
      if (input.abortSignal?.aborted) return yield* Effect.fail(new RejectedError())
      const id = QuestionID.ascending()
      log.info("asking", { id, questions: input.questions.length })

      const deferred = yield* Deferred.make<ReadonlyArray<Answer>, RejectedError>()
      const info = Schema.decodeUnknownSync(Request)({
        id,
        sessionID: input.sessionID,
        questions: input.questions,
        tool: input.tool,
      })
      pending.set(id, { info, deferred })
      const signal = input.abortSignal
      const awaiting = signal
        ? Effect.raceFirst(
            Deferred.await(deferred),
            Effect.callback<ReadonlyArray<Answer>, RejectedError>((resume) => {
              // Reuse request ownership: a reply already in flight must win over a late abort.
              const onAbort = () => resume(reject(id).pipe(Effect.andThen(Deferred.await(deferred))))
              if (signal.aborted) {
                onAbort()
                return Effect.void
              }
              signal.addEventListener("abort", onAbort, { once: true })
              return Effect.sync(() => signal.removeEventListener("abort", onAbort))
            }),
          )
        : Deferred.await(deferred)
      return yield* Effect.ensuring(
        bus.publish(Event.Asked, info).pipe(Effect.andThen(awaiting)),
        Effect.gen(function* () {
          // A caller interrupt (e.g. MCP cancellation) must also withdraw the UI.
          if (!pending.delete(id)) return
          yield* bus.publish(Event.Rejected, { sessionID: input.sessionID, requestID: id })
        }),
      )
    })

    const askOptional = Effect.fn("Question.askOptional")(function* (input: {
      sessionID: SessionID
      actorID: string
      executionRevision?: number
      originTurnID: MessageID
      questions: ReadonlyArray<Info>
      tool: Tool
    }) {
      const hostContext = input.executionRevision === undefined ? undefined :
        ManagedProvider.currentActorHostContext(input.sessionID, input.actorID, input.executionRevision)
      if (!hostContext?.chatID) throw new Error("optional questions require an authenticated managed chat context")
      const binding = yield* Effect.tryPromise({
        try: () => ManagedProvider.readManagedSessionBinding(input.sessionID),
        catch: (error) => error instanceof Error ? error : new Error(String(error)),
      })
      if (binding.stableChatID !== hostContext.chatID || binding.engineSessionID !== input.sessionID) throw new Error("optional question managed binding changed")
      const info = persistOptionalQuestion({ ...input, owner: binding.owner, stableChatID: binding.stableChatID })
      log.info("persisted optional question", { id: info.id, sessionID: info.sessionID, actorID: input.actorID })
      yield* bus.publish(Event.Asked, info)
      return info
    })

    const reply = Effect.fn("Question.reply")(function* (input: {
      requestID: QuestionID
      answers: ReadonlyArray<Answer>
    }) {
      const pending = (yield* InstanceState.get(state)).pending
      const existing = pending.get(input.requestID)
      if (!existing) {
        log.warn("reply for unknown request", { requestID: input.requestID })
        return
      }
      pending.delete(input.requestID)
      log.info("replied", { requestID: input.requestID, answers: input.answers })
      yield* bus.publish(Event.Replied, {
        sessionID: existing.info.sessionID,
        requestID: existing.info.id,
        answers: input.answers,
      })
      yield* Deferred.succeed(existing.deferred, input.answers)
    })

    const reject = Effect.fn("Question.reject")(function* (requestID: QuestionID) {
      const pending = (yield* InstanceState.get(state)).pending
      const existing = pending.get(requestID)
      if (!existing) {
        log.warn("reject for unknown request", { requestID })
        return
      }
      pending.delete(requestID)
      log.info("rejected", { requestID })
      yield* bus.publish(Event.Rejected, {
        sessionID: existing.info.sessionID,
        requestID: existing.info.id,
      })
      yield* Deferred.fail(existing.deferred, new RejectedError())
    })

    const list = Effect.fn("Question.list")(function* () {
      const pending = (yield* InstanceState.get(state)).pending
      return Array.from(pending.values(), (x) => x.info)
    })

    const neverAsk = Effect.fn("Question.neverAsk")(function* () {
      return (yield* InstanceState.get(state)).neverAsk
    })

    const setNeverAsk = Effect.fn("Question.setNeverAsk")(function* (enabled: boolean) {
      const s = yield* InstanceState.get(state)
      s.neverAsk = enabled
      log.info("never-ask", { enabled })
    })

    return Service.of({ ask, askOptional, reply, reject, list, neverAsk, setNeverAsk })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(Bus.layer))

export * as Question from "."
