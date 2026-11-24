import { createHash } from "node:crypto"
import { and, Database, eq, inArray, lte, or, sql } from "@/storage"
import { ActorRegistryTable } from "@/actor/actor.sql"
import { MessageV2 } from "@/session/message-v2"
import { MessageID, PartID, SessionID } from "@/session/schema"
import { MessageTable, PartTable } from "@/session/session.sql"
import { SyncEvent } from "@/sync"
import { currentObjects } from "@/storage/current-schema"
import { QuestionRequestTable, UserInputHandoffTable, UserInputReceiptTable, type UserInputReceiptRow, type UserInputTurnPolicy, type UserInputEffectivePolicyCeiling } from "@/inbox/inbox.sql"

export type DeliveryMode = "steer" | "after-turn"
export type ReceiptState = "persisted" | "pending-boundary" | "consumed" | "cancelled" | "failed"
export type InputContentPart =
  | { type: "text"; text: string }
  | { type: "file"; mime: string; filename?: string; url: string }

export interface FrozenModelRequestOptions {
  [key: string]: unknown
  present: boolean
  routeID?: string
  optionRouteID?: string
  catalogRevision?: number
  model?: { providerID: string; modelID: string }
  options?: Record<string, string>
}

export interface Binding {
  owner: string
  stableChatID: string
  mapRevision: number
  mappingRevision: number
  workspaceRevision: number
}

export interface Admission extends Binding {
  requestID: string
  sessionID: SessionID
  actorID: string
  executionRevision: number
  originTurnID?: string
  text: string
  attachmentRefs: string[]
  attachmentUploadIDs?: string[]
  attachmentParts?: InputContentPart[]
  questionRefs: string[]
  questionAnswers?: QuestionAnswer[]
  modelRequestOptions?: FrozenModelRequestOptions
  turnPolicy?: UserInputTurnPolicy | null
  effectivePolicyCeiling?: UserInputEffectivePolicyCeiling | null
  mode: DeliveryMode
  queuedSourceProof?: string
}

/** Stable snapshot sent to C2 before the synchronous consume transaction. */
export interface BoundaryReceipt {
  requestID: string
  payloadHash: string
  contentHash?: string
  executionRevision: number
  mapRevision: number
  mappingRevision: number
  workspaceRevision: number
  text: string
  modelRequestOptions: FrozenModelRequestOptions
  turnPolicy: UserInputTurnPolicy | null
  admissionCeiling: UserInputEffectivePolicyCeiling | null
  queuedSourceProof?: string
}

export interface BoundaryReceiptIdentity extends Pick<BoundaryReceipt,
  "requestID" | "payloadHash" | "contentHash" | "executionRevision" | "mapRevision" | "mappingRevision" | "workspaceRevision"> {}

export interface PolicyBatchEvidence {
  version: 1
  receipts: BoundaryReceiptIdentity[]
  choices: Array<{ requestID: string; choiceSnapshot: UserInputTurnPolicy | null; guideOnly: boolean }>
  batchMode: "agent" | "chat" | "plan" | null
  batchGuideOnly: boolean
  toolProjection: {
    blockAllToolCalls: boolean
    disabledTools: string[]
    hiddenTools: string[]
    allowedTools: string[] | null
  }
}

export interface QuestionAnswer {
  requestID: string
  questionRevision: number
  disposition: "answered" | "dismissed"
  answers?: string[][]
}

export class UserInputConflict extends Error {
  override readonly name = "UserInputConflict"
}

export class UserInputUnavailable extends Error {
  override readonly name = "UserInputUnavailable"
}

function normalizeTurnPolicy(value: UserInputTurnPolicy | null | undefined): UserInputTurnPolicy | null {
  if (value == null) return null
  if (typeof value !== "object" || Array.isArray(value)) throw new UserInputConflict("invalid turn policy")
  const expected = ["allowBash", "allowWebSearch", "compareMode", "incognito", "mode", "noMemory", "useResearch", "useWeb", "version"]
  const keys = Object.keys(value).sort()
  if (keys.length !== expected.length || keys.some((key, index) => key !== expected[index])) {
    throw new UserInputConflict("turn policy fields are unsupported")
  }
  if (value.version !== 1 || !["agent", "chat", "plan"].includes(value.mode)) {
    throw new UserInputConflict("turn policy version or mode is unsupported")
  }
  if ((value.allowBash !== null && typeof value.allowBash !== "boolean")
      || (value.allowWebSearch !== null && typeof value.allowWebSearch !== "boolean")
      || [value.useWeb, value.useResearch, value.compareMode, value.incognito, value.noMemory].some((item) => typeof item !== "boolean")) {
    throw new UserInputConflict("turn policy choices must be booleans")
  }
  return {
    version: 1,
    mode: value.mode,
    allowBash: value.allowBash,
    allowWebSearch: value.allowWebSearch,
    useWeb: value.useWeb,
    useResearch: value.useResearch,
    compareMode: value.compareMode,
    incognito: value.incognito,
    noMemory: value.noMemory,
  }
}

function normalizeEffectivePolicyCeiling(value: UserInputEffectivePolicyCeiling | null | undefined): UserInputEffectivePolicyCeiling | null {
  if (value == null) return null
  const expected = ["allowedTools", "blockAllToolCalls", "disabledTools", "hiddenTools", "version"]
  if (typeof value !== "object" || Array.isArray(value) || Object.keys(value).sort().some((key, index) => key !== expected[index])
      || Object.keys(value).length !== expected.length || value.version !== 1 || typeof value.blockAllToolCalls !== "boolean"
      || !Array.isArray(value.disabledTools) || !Array.isArray(value.hiddenTools)
      || (value.allowedTools !== null && !Array.isArray(value.allowedTools))) throw new UserInputConflict("invalid effective policy ceiling")
  const clean = (items: unknown[]) => {
    if (items.length > 512 || items.some((item) => typeof item !== "string" || !item || item.length > 128)) throw new UserInputConflict("invalid effective policy tool list")
    return [...new Set(items as string[])].sort()
  }
  return { version: 1, blockAllToolCalls: value.blockAllToolCalls, disabledTools: clean(value.disabledTools),
    hiddenTools: clean(value.hiddenTools), allowedTools: value.allowedTools === null ? null : clean(value.allowedTools) }
}

function payloadHash(input: Admission) {
  // Request ID retries are bound to immutable client content and target slice.
  // Execution revision and origin turn are frozen server evidence on the first
  // admission; recomputing those from a later transcript must not create a
  // second receipt or reject the same client request.
  const turnPolicy = normalizeTurnPolicy(input.turnPolicy)
  const payload = JSON.stringify({
    sessionID: input.sessionID,
    actorID: input.actorID,
    text: input.text,
    attachmentRefs: input.attachmentRefs,
    attachmentUploadIDs: input.attachmentUploadIDs ?? [],
    questionRefs: input.questionRefs,
    ...(input.questionAnswers?.length ? { questionAnswers: input.questionAnswers } : {}),
    modelRequestOptions: input.modelRequestOptions ?? { present: false },
    ...(turnPolicy ? { turnPolicy } : {}),
  })
  return createHash("sha256").update(payload).digest("hex")
}

/** Startup admission has already verified the exact old or additive profile. */
function hasQueuedSourceProfile(db: Database.TxOrDb): boolean {
  const columns = db.all<{name:string}>(sql`PRAGMA table_info('user_input_receipt')`).map((row)=>row.name)
  return ["source_proof","source_message_id","source_part_id"].every((name)=>columns.includes(name))
}

/** Content-free exact-row evidence; the host must independently authorize it. */
function enrolledMutationPreimage(db: Database.TxOrDb, input: {owner:string;stableChatID:string;sessionID:string;requestID:string}) {
  const columns = db.all<{name:string}>(sql`PRAGMA table_info('user_input_receipt')`).map((row) => row.name)
  if (!columns.includes('source_enrollment')) return undefined
  const receipt = db.get<{owner:string;stable_chat_id:string;session_id:string;actor_id:string;request_id:string;
    state:string;execution_revision:number;map_revision:number;mapping_revision:number;workspace_revision:number;
    payload_hash:string;text:string;source_proof:string|null;source_message_id:string|null;source_part_id:string|null;source_enrollment:string|null}>(sql`
    SELECT owner,stable_chat_id,session_id,actor_id,request_id,state,execution_revision,map_revision,mapping_revision,workspace_revision,
      payload_hash,text,source_proof,source_message_id,source_part_id,source_enrollment FROM user_input_receipt
    WHERE owner=${input.owner} AND stable_chat_id=${input.stableChatID} AND session_id=${input.sessionID} AND actor_id='main' AND request_id=${input.requestID}`)
  if (!receipt || receipt.state !== 'consumed' || !receipt.source_proof || !receipt.source_message_id || !receipt.source_part_id
      || !receipt.source_enrollment || Buffer.byteLength(receipt.source_enrollment,'utf8') > 16384) return undefined
  let marker: unknown
  try { marker = JSON.parse(receipt.source_enrollment) } catch { return undefined }
  if (!marker || typeof marker !== 'object' || Array.isArray(marker)) return undefined
  const envelope = marker as Record<string,unknown>; const source = envelope.source
  if (Object.keys(envelope).sort().join(',') !== 'source,state,version' || envelope.version !== 1 || envelope.state !== 'enrolled'
      || !source || typeof source !== 'object' || Array.isArray(source)) return undefined
  const ref = source as Record<string,unknown>
  const refKeys = ['version','kind','account_id','chat_id','workspace_id','request_id','native_session_id','message_id','part_id',
    'archive_revision','archive_content_hash','text_sha256','ingress_sha256','root_operation_id','execution_revision','inner_turn_id']
  if (Object.keys(ref).sort().join(',') !== refKeys.sort().join(',') || ref.version !== 1 || ref.kind !== 'queued_user_text'
      || ['account_id','chat_id','workspace_id','request_id','native_session_id','message_id','part_id','root_operation_id','inner_turn_id']
        .some((key) => typeof ref[key] !== 'string' || !(ref[key] as string).trim() || (ref[key] as string).length > 512)
      || ['archive_content_hash','text_sha256','ingress_sha256'].some((key) => typeof ref[key] !== 'string' || !/^[0-9a-f]{64}$/.test(ref[key] as string))
      || !Number.isSafeInteger(ref.archive_revision) || (ref.archive_revision as number) < 1
      || !Number.isSafeInteger(ref.execution_revision) || (ref.execution_revision as number) < 0
      || createHash('sha256').update(receipt.text,'utf8').digest('hex') !== ref.text_sha256
      || createHash('sha256').update(receipt.source_proof,'utf8').digest('hex') !== ref.ingress_sha256
      || ref.chat_id !== input.stableChatID || ref.native_session_id !== input.sessionID || ref.request_id !== input.requestID
      || ref.message_id !== receipt.source_message_id || ref.part_id !== receipt.source_part_id) return undefined
  const message = db.get<{id:string;session_id:string;agent_id:string;time_created:number;time_updated:number;data:string}>(sql`
    SELECT id,session_id,agent_id,time_created,time_updated,data FROM message WHERE id=${receipt.source_message_id} AND session_id=${input.sessionID}`)
  const part = db.get<{id:string;message_id:string;session_id:string;time_created:number;time_updated:number;data:string}>(sql`
    SELECT id,message_id,session_id,time_created,time_updated,data FROM part WHERE id=${receipt.source_part_id}
      AND message_id=${receipt.source_message_id} AND session_id=${input.sessionID}`)
  if (!message || !part || message.agent_id !== 'main' || typeof message.data !== 'string' || typeof part.data !== 'string'
      || Buffer.byteLength(message.data,'utf8') > 1000000 || Buffer.byteLength(part.data,'utf8') > 1000000) return undefined
  const digest = (values:unknown[]) => createHash('sha256').update(JSON.stringify(values),'utf8').digest('hex')
  return {version:1 as const,owner:input.owner,accountID:ref.account_id,stableChatID:input.stableChatID,sessionID:input.sessionID,
    requestID:input.requestID,messageID:receipt.source_message_id,partID:receipt.source_part_id,
    receiptSHA256:digest(['openclank.queued.receipt.v1',receipt.owner,receipt.stable_chat_id,receipt.session_id,receipt.actor_id,
      receipt.request_id,receipt.state,receipt.execution_revision,receipt.map_revision,receipt.mapping_revision,receipt.workspace_revision,
      receipt.payload_hash,receipt.text,receipt.source_proof,receipt.source_message_id,receipt.source_part_id]),
    messageSHA256:digest(['openclank.queued.message.v1',message.id,message.session_id,message.agent_id,message.time_created,message.time_updated,message.data]),
    partSHA256:digest(['openclank.queued.part.v1',part.id,part.message_id,part.session_id,part.time_created,part.time_updated,part.data]),
    enrollmentSHA256:createHash('sha256').update(receipt.source_enrollment,'utf8').digest('hex')}
}

function key(input: Pick<Admission, "owner" | "stableChatID" | "requestID">) {
  return and(
    eq(UserInputReceiptTable.owner, input.owner),
    eq(UserInputReceiptTable.stable_chat_id, input.stableChatID),
    eq(UserInputReceiptTable.request_id, input.requestID),
  )
}

function rowKey(row: Pick<UserInputReceiptRow, "owner" | "stable_chat_id" | "map_revision" | "request_id">) {
  return key({ owner: row.owner, stableChatID: row.stable_chat_id, requestID: row.request_id })
}

function requireNonempty(value: string, label: string) {
  if (!value.trim() || value !== value.trim()) throw new UserInputConflict(`${label} must be nonempty and trimmed`)
}

function validateAdmission(input: Admission) {
  requireNonempty(input.owner, "owner")
  requireNonempty(input.stableChatID, "stableChatID")
  requireNonempty(input.requestID, "requestID")
  requireNonempty(input.actorID, "actorID")
  if (!Number.isSafeInteger(input.mapRevision) || input.mapRevision < 0) throw new UserInputConflict("invalid mapRevision")
  if (!Number.isSafeInteger(input.mappingRevision) || input.mappingRevision < 0) throw new UserInputConflict("invalid mappingRevision")
  if (!Number.isSafeInteger(input.workspaceRevision) || input.workspaceRevision < 0) throw new UserInputConflict("invalid workspaceRevision")
  if (!Number.isSafeInteger(input.executionRevision) || input.executionRevision < 0) throw new UserInputConflict("invalid executionRevision")
  if (input.mode !== "steer" && input.mode !== "after-turn") throw new UserInputConflict("invalid delivery mode")
  if (!input.text.trim() && input.attachmentRefs.length === 0 && (input.attachmentUploadIDs?.length ?? 0) === 0 && (input.questionAnswers?.length ?? 0) === 0) throw new UserInputConflict("input requires text, an attachment, or a structured question answer")
  if (new Set(input.attachmentRefs).size !== input.attachmentRefs.length) throw new UserInputConflict("duplicate attachment reference")
  const uploadIDs = input.attachmentUploadIDs ?? []
  const attachmentParts = input.attachmentParts ?? []
  const modelRequestOptions = input.modelRequestOptions ?? { present: false }
  normalizeTurnPolicy(input.turnPolicy)
  normalizeEffectivePolicyCeiling(input.effectivePolicyCeiling)
  if (new Set(uploadIDs).size !== uploadIDs.length) throw new UserInputConflict("duplicate upload ID")
  if (uploadIDs.length > 100 || attachmentParts.length > 200) throw new UserInputConflict("input has too many attachment parts")
  let aggregateEncodedBytes = 0
  for (const part of attachmentParts) {
    if (part.type === "text") {
      if (typeof part.text !== "string" || part.text.length > 200_000) throw new UserInputConflict("invalid attachment text part")
      continue
    }
    if (part.type !== "file" || !/^[\w.+-]+\/[\w.+-]+$/.test(part.mime) || part.mime === "application/octet-stream") {
      throw new UserInputConflict("unsupported attachment file part")
    }
    if (part.filename !== undefined && (typeof part.filename !== "string" || part.filename.length > 512 || /[\\/\0]/.test(part.filename))) {
      throw new UserInputConflict("invalid attachment filename")
    }
    const match = /^data:([\w.+-]+\/[\w.+-]+);base64,([A-Za-z0-9+/]*={0,2})$/.exec(part.url)
    if (!match || match[1].toLowerCase() !== part.mime.toLowerCase() || match[2].length % 4 !== 0 || match[2].length > 50 * 1024 * 1024) {
      throw new UserInputConflict("attachment must be a bounded supported data URL")
    }
    aggregateEncodedBytes += match[2].length
    if (aggregateEncodedBytes > 50 * 1024 * 1024) throw new UserInputConflict("combined attachment payload exceeds the supported size")
  }
  if (modelRequestOptions.present || modelRequestOptions.routeID || modelRequestOptions.optionRouteID || modelRequestOptions.model) {
    const selection = modelRequestOptions
    if (!selection.routeID || !selection.optionRouteID || !Number.isSafeInteger(selection.catalogRevision) || !selection.model?.providerID || !selection.model.modelID || !selection.options) {
      throw new UserInputConflict("frozen model options are incomplete")
    }
  }
  if (new Set(input.questionRefs).size !== input.questionRefs.length) throw new UserInputConflict("duplicate question reference")
}

function verifyAttachmentRefs(input: Admission) {
  if (input.originTurnID) {
    const origin = Database.use((db) =>
      db
        .select({ role: MessageTable.data })
        .from(MessageTable)
        .where(
          and(
            eq(MessageTable.session_id, input.sessionID),
            eq(MessageTable.agent_id, input.actorID),
            eq(MessageTable.id, input.originTurnID! as MessageID),
          ),
        )
        .get(),
    )
    if (!origin || origin.role.role !== "user") throw new UserInputConflict("origin turn is not a user message in this actor slice")
  }
  for (const ref of input.attachmentRefs) {
    const [messageID, partID, extra] = ref.split("/")
    if (!messageID || !partID || extra !== undefined) throw new UserInputConflict(`invalid attachment reference: ${ref}`)
    const file = Database.use((db) =>
      db
        .select({ part: PartTable.data, message: MessageTable.data })
        .from(PartTable)
        .innerJoin(MessageTable, eq(PartTable.message_id, MessageTable.id))
        .where(
          and(
            eq(PartTable.session_id, input.sessionID),
            eq(PartTable.message_id, messageID as MessageID),
            eq(PartTable.id, partID as PartID),
            eq(MessageTable.agent_id, input.actorID),
          ),
        )
        .get(),
    )
    if (!file || file.part.type !== "file") throw new UserInputConflict(`attachment is not available in this actor slice: ${ref}`)
  }
}

function validateQuestionAnswer(db: Database.TxOrDb, input: Admission, answer: QuestionAnswer) {
  const question = db.select().from(QuestionRequestTable).where(and(
    eq(QuestionRequestTable.owner, input.owner),
    eq(QuestionRequestTable.stable_chat_id, input.stableChatID),
    eq(QuestionRequestTable.request_id, answer.requestID),
    eq(QuestionRequestTable.session_id, input.sessionID),
    eq(QuestionRequestTable.actor_id, input.actorID),
    eq(QuestionRequestTable.question_revision, answer.questionRevision),
  )).get()
  if (!question || question.blocking || question.state !== "pending") throw new UserInputConflict("question is unavailable or requires a blocking approval")
  if (answer.disposition === "dismissed") {
    if (answer.answers !== undefined && answer.answers.length !== 0) throw new UserInputConflict("dismissed question cannot include answers")
    return question
  }
  const prompts = question.questions as Array<{ question?: string; options?: Array<{ label?: string }>; multiple?: boolean; custom?: boolean }>
  if (!Array.isArray(answer.answers) || answer.answers.length !== prompts.length) throw new UserInputConflict("question answer count does not match the request")
  for (const [index, selected] of answer.answers.entries()) {
    const prompt = prompts[index]
    if (!prompt || !Array.isArray(selected) || selected.length === 0 || new Set(selected).size !== selected.length) throw new UserInputConflict("question answer selection is invalid")
    if (prompt.multiple !== true && selected.length !== 1) throw new UserInputConflict("question allows one selection")
    const labels = new Set((prompt.options ?? []).map((option) => option.label).filter((label): label is string => typeof label === "string"))
    if (selected.some((value) => typeof value !== "string" || !value.trim() || value.length > 2_048 || (!labels.has(value) && prompt.custom === false))) {
      throw new UserInputConflict("question answer contains an unavailable option")
    }
  }
  return question
}

function renderQuestionAnswer(questions: Array<{ question?: string }>, answer: QuestionAnswer) {
  if (answer.disposition === "dismissed") return questions.map((question) => `"${question.question ?? "Question"}"=Dismissed`).join(", ")
  return questions.map((question, index) => `"${question.question ?? "Question"}"="${(answer.answers?.[index] ?? []).join(", ")}"`).join(", ")
}

/** Persist once. An identical retry returns its receipt; a reused key with any
 * changed content or execution identity is rejected. No runner is started. */
export function admit(input: Admission, validQuestionRefs?: ReadonlySet<string>): UserInputReceiptRow {
  validateAdmission(input)
  if ((input.questionAnswers?.length ?? 0) > 0 && input.text !== "") throw new UserInputConflict("structured question answers cannot include caller-rendered text")
  if (new Set((input.questionAnswers ?? []).map((answer) => answer.requestID)).size !== (input.questionAnswers ?? []).length) throw new UserInputConflict("duplicate question answer")
  const expectedRefs = (input.questionAnswers ?? []).map((answer) => `${answer.requestID}@${answer.questionRevision}`)
  if (expectedRefs.length && (expectedRefs.length !== input.questionRefs.length || expectedRefs.some((ref) => !input.questionRefs.includes(ref)))) throw new UserInputConflict("question references do not match structured answers")
  const hash = payloadHash(input)
  return Database.transaction((db) => {
    const prior = db.select().from(UserInputReceiptTable).where(key(input)).get()
    if (prior) {
      if (
        prior.payload_hash !== hash ||
        prior.session_id !== input.sessionID ||
        prior.actor_id !== input.actorID
      ) {
        throw new UserInputConflict("requestID is already bound to a different input")
      }
      if (input.mode === "steer" && prior.mode === "after-turn" && ["persisted", "pending-boundary"].includes(prior.state)) {
        db.update(UserInputReceiptTable)
          .set({ mode: "steer", state: "pending-boundary", updated_at: Date.now() })
          .where(key(input))
          .run()
        return db.select().from(UserInputReceiptTable).where(key(input)).get()!
      }
      if (input.mode !== prior.mode && !(input.mode === "steer" && prior.mode === "after-turn")) {
        throw new UserInputConflict("delivery mode conflicts with the existing receipt")
      }
      return prior
    }
    const actor = db
      .select()
      .from(ActorRegistryTable)
      .where(and(eq(ActorRegistryTable.session_id, input.sessionID), eq(ActorRegistryTable.actor_id, input.actorID)))
      .get()
    if (!actor) throw new UserInputUnavailable("actor membership is unavailable")
    if (actor.execution_revision !== input.executionRevision) throw new UserInputConflict("actor execution revision changed")
    if (actor.chat_id !== input.stableChatID) throw new UserInputConflict("actor is not bound to this host chat")
    if (input.questionRefs.some((ref) => !validQuestionRefs?.has(ref))) {
      if ((input.questionAnswers?.length ?? 0) === 0) throw new UserInputConflict("question reference is not pending for this actor")
    }
    const answeredQuestions = (input.questionAnswers ?? []).map((answer) => ({ answer, question: validateQuestionAnswer(db, input, answer) }))
    verifyAttachmentRefs(input)
    const now = Date.now()
    const row: typeof UserInputReceiptTable.$inferInsert = {
      owner: input.owner,
      stable_chat_id: input.stableChatID,
      map_revision: input.mapRevision,
      mapping_revision: input.mappingRevision,
      workspace_revision: input.workspaceRevision,
      request_id: input.requestID,
      session_id: input.sessionID,
      actor_id: input.actorID,
      admission_status: actor.status,
      execution_revision: input.executionRevision,
      origin_turn_id: input.originTurnID ?? null,
      payload_hash: hash,
      text: answeredQuestions.length
        ? answeredQuestions.map(({ answer, question }) => renderQuestionAnswer(question.questions as Array<{ question?: string }>, answer)).join("; ")
        : input.text,
      attachment_refs: input.attachmentRefs,
      attachment_upload_ids: input.attachmentUploadIDs ?? [],
      attachment_parts: input.attachmentParts ?? [],
      question_refs: input.questionRefs,
      model_request_options: input.modelRequestOptions ?? { present: false },
      turn_policy: normalizeTurnPolicy(input.turnPolicy),
      effective_policy_ceiling: normalizeEffectivePolicyCeiling(input.effectivePolicyCeiling),
      mode: input.mode,
      state: "persisted" as const,
      failure_reason: null,
      message_id: null,
      created_at: now,
      updated_at: now,
      consumed_at: null,
    }
    db.insert(UserInputReceiptTable).values(row).run()
    // The signed host envelope is optional metadata, never part of the legacy
    // receipt hash. An old-profile DB or old receipt is never upgraded here.
    if (hasQueuedSourceProfile(db) && typeof input.queuedSourceProof === "string" && input.queuedSourceProof.length <= 16384 &&
        input.text && !input.attachmentRefs.length && !(input.attachmentUploadIDs?.length) && !(input.attachmentParts?.length) &&
        !input.questionRefs.length && !(input.questionAnswers?.length)) {
      db.run(sql`UPDATE user_input_receipt SET source_proof=${input.queuedSourceProof} WHERE owner=${input.owner} AND stable_chat_id=${input.stableChatID} AND request_id=${input.requestID}`)
    }
    for (const { answer } of answeredQuestions) {
      const changed = db.update(QuestionRequestTable).set({
        state: "answer-queued",
        answer_payload: answer.disposition === "answered" ? { answers: answer.answers } : null,
        answer_disposition: answer.disposition,
        answer_request_id: input.requestID,
        updated_at: now,
      }).where(and(
        eq(QuestionRequestTable.owner, input.owner),
        eq(QuestionRequestTable.stable_chat_id, input.stableChatID),
        eq(QuestionRequestTable.request_id, answer.requestID),
        eq(QuestionRequestTable.session_id, input.sessionID),
        eq(QuestionRequestTable.actor_id, input.actorID),
        eq(QuestionRequestTable.question_revision, answer.questionRevision),
        eq(QuestionRequestTable.state, "pending"),
      )).returning({ request_id: QuestionRequestTable.request_id }).get()
      if (!changed) throw new UserInputConflict("question changed while its answer was being admitted")
    }
    return db.select().from(UserInputReceiptTable).where(key(input)).get()!
  }, { behavior: "immediate" })
}

export function hasPending(sessionID: SessionID, actorID: string) {
  return Database.use((db) =>
    !!db
      .select({ request_id: UserInputReceiptTable.request_id })
      .from(UserInputReceiptTable)
      .where(
        and(
          eq(UserInputReceiptTable.session_id, sessionID),
          eq(UserInputReceiptTable.actor_id, actorID),
          inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
        ),
      )
      .limit(1)
      .get(),
  )
}

export function hasPendingAfterTurn(sessionID: SessionID, actorID: string, binding?: Binding) {
  if (!binding) return false
  return Database.use((db) => {
    const actor = db
      .select()
      .from(ActorRegistryTable)
      .where(and(eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID)))
      .get()
    if (!actor || actor.status !== "running") return false
    return !!db
      .select({ request_id: UserInputReceiptTable.request_id })
      .from(UserInputReceiptTable)
      .where(
        and(
          eq(UserInputReceiptTable.session_id, sessionID),
          eq(UserInputReceiptTable.actor_id, actorID),
          eq(UserInputReceiptTable.owner, binding.owner),
          eq(UserInputReceiptTable.stable_chat_id, binding.stableChatID),
          lte(UserInputReceiptTable.map_revision, binding.mapRevision),
          eq(UserInputReceiptTable.mapping_revision, binding.mappingRevision),
          eq(UserInputReceiptTable.workspace_revision, binding.workspaceRevision),
          or(
            and(
              eq(UserInputReceiptTable.admission_status, "running"),
              eq(UserInputReceiptTable.execution_revision, actor.execution_revision),
            ),
            eq(UserInputReceiptTable.claimed_execution_revision, actor.execution_revision),
          ),
          eq(UserInputReceiptTable.mode, "after-turn"),
          inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
        ),
      )
      .limit(1)
      .get()
  })
}

/** Recheck the safe finalization seam for input admitted to this exact live
 * execution. A true result lets the same runner process the receipt before it
 * settles; idle receipts require an explicit handoff and do not qualify. */
export function hasDeliverable(sessionID: SessionID, actorID: string, binding: Binding) {
  return Database.use((db) => {
    const actor = db.select().from(ActorRegistryTable).where(and(
      eq(ActorRegistryTable.session_id, sessionID), eq(ActorRegistryTable.actor_id, actorID),
    )).get()
    if (!actor || actor.status !== "running") return false
    return !!db.select({ request_id: UserInputReceiptTable.request_id }).from(UserInputReceiptTable).where(and(
      eq(UserInputReceiptTable.session_id, sessionID),
      eq(UserInputReceiptTable.actor_id, actorID),
      eq(UserInputReceiptTable.owner, binding.owner),
      eq(UserInputReceiptTable.stable_chat_id, binding.stableChatID),
      lte(UserInputReceiptTable.map_revision, binding.mapRevision),
      eq(UserInputReceiptTable.mapping_revision, binding.mappingRevision),
      eq(UserInputReceiptTable.workspace_revision, binding.workspaceRevision),
      or(
        and(eq(UserInputReceiptTable.admission_status, "running"), eq(UserInputReceiptTable.execution_revision, actor.execution_revision)),
        eq(UserInputReceiptTable.claimed_execution_revision, actor.execution_revision),
      ),
      inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
    )).limit(1).get()
  })
}

/** Hash every frozen delivery field, including server-derived attachment payloads. */
export function boundaryContentHash(row: UserInputReceiptRow) {
  return createHash("sha256").update(JSON.stringify({
    text: row.text, attachmentRefs: row.attachment_refs, attachmentUploadIDs: row.attachment_upload_ids,
    attachmentParts: row.attachment_parts, questionRefs: row.question_refs, modelRequestOptions: row.model_request_options,
    turnPolicy: row.turn_policy, admissionCeiling: row.effective_policy_ceiling, mode: row.mode,
  })).digest("hex")
}

/** Snapshot exactly the receipt rows eligible for one current execution boundary. */
export function snapshotBoundary(input: {
  sessionID: SessionID
  actorID: string
  binding: Binding
  afterTurnReady: boolean
  prepared?: { expectedExecutionRevision: number; requestID: string }
}): BoundaryReceipt[] {
  return Database.use((db) => {
    const actor = db.select().from(ActorRegistryTable).where(and(
      eq(ActorRegistryTable.session_id, input.sessionID), eq(ActorRegistryTable.actor_id, input.actorID),
    )).get()
    if (!actor || (input.prepared
      ? actor.status !== "idle" || actor.execution_revision !== input.prepared.expectedExecutionRevision
      : actor.status !== "running")) return []
    const rows = db.select().from(UserInputReceiptTable).where(and(
      eq(UserInputReceiptTable.session_id, input.sessionID),
      eq(UserInputReceiptTable.actor_id, input.actorID),
      eq(UserInputReceiptTable.owner, input.binding.owner),
      eq(UserInputReceiptTable.stable_chat_id, input.binding.stableChatID),
      inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
    )).orderBy(UserInputReceiptTable.created_at).all()
    // Stop at the first unavailable or incompatible row: later inputs cannot
    // overtake an earlier receipt, nor inherit its route or workflow choices.
    if (input.prepared && rows[0]?.request_id !== input.prepared.requestID) return []
    const eligible: typeof rows = []
    let compatibility: string | undefined
    for (const row of rows) {
      if (eligible.length === 32) break
      const priorAttempts = input.prepared && row.execution_revision !== actor.execution_revision
        ? db.select().from(UserInputHandoffTable).where(and(
            eq(UserInputHandoffTable.owner, row.owner), eq(UserInputHandoffTable.stable_chat_id, row.stable_chat_id),
            eq(UserInputHandoffTable.request_id, row.request_id),
          )).all()
        : []
      const preparedEligible = row.execution_revision === actor.execution_revision && row.claimed_execution_revision === null ||
        row.execution_revision < actor.execution_revision && priorAttempts.length > 0 &&
          priorAttempts.every((attempt) => attempt.outcome === "settled-before-consumption")
      if (row.map_revision > input.binding.mapRevision ||
        row.mapping_revision !== input.binding.mappingRevision ||
        row.workspace_revision !== input.binding.workspaceRevision ||
        (row.mode === "after-turn" && !input.afterTurnReady) ||
        !(input.prepared
          ? preparedEligible
          : (row.admission_status === "running" && row.execution_revision === actor.execution_revision) ||
            row.claimed_execution_revision === actor.execution_revision)) break
      const policy = row.turn_policy
      const key = JSON.stringify({
        options: row.model_request_options,
        mode: policy?.mode ?? null,
        compare: policy?.compareMode ?? false,
        research: policy?.useResearch ?? false,
        incognito: policy?.incognito ?? false,
        noMemory: policy?.noMemory ?? false,
      })
      if (compatibility !== undefined && compatibility !== key) break
      compatibility = key
      eligible.push(row)
    }
    const queuedProfile = hasQueuedSourceProfile(db)
    return eligible.map((row) => ({
      requestID: row.request_id,
      payloadHash: row.payload_hash,
      contentHash: boundaryContentHash(row),
      executionRevision: row.execution_revision,
      mapRevision: row.map_revision,
      mappingRevision: row.mapping_revision,
      workspaceRevision: row.workspace_revision,
      text: row.text,
      modelRequestOptions: row.model_request_options as FrozenModelRequestOptions,
      turnPolicy: row.turn_policy ?? null,
      admissionCeiling: row.effective_policy_ceiling ?? null,
      ...(queuedProfile ? (() => {
        const extra = db.get<{source_proof:string|null}>(sql`SELECT source_proof FROM user_input_receipt WHERE owner=${row.owner} AND stable_chat_id=${row.stable_chat_id} AND request_id=${row.request_id}`)
        return typeof extra?.source_proof === "string" && extra.source_proof.length <= 16384 ? {queuedSourceProof:extra.source_proof} : {}
      })() : {}),
    }))
  })
}

/** Record a policy resolver refusal without changing immutable receipt identity or payload. */
export function markBoundaryUnavailable(input: {
  sessionID: SessionID
  actorID: string
  receipts: readonly BoundaryReceiptIdentity[]
  reason: string
}) {
  const reason = input.reason.trim().slice(0, 1000) || "turn policy is unavailable"
  return Database.transaction((db) => {
    for (const receipt of input.receipts) {
      db.update(UserInputReceiptTable).set({ failure_reason: reason, updated_at: Date.now() }).where(and(
        eq(UserInputReceiptTable.session_id, input.sessionID),
        eq(UserInputReceiptTable.actor_id, input.actorID),
        eq(UserInputReceiptTable.request_id, receipt.requestID),
        eq(UserInputReceiptTable.payload_hash, receipt.payloadHash),
        eq(UserInputReceiptTable.execution_revision, receipt.executionRevision),
        eq(UserInputReceiptTable.map_revision, receipt.mapRevision),
        eq(UserInputReceiptTable.mapping_revision, receipt.mappingRevision),
        eq(UserInputReceiptTable.workspace_revision, receipt.workspaceRevision),
        inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
      )).run()
    }
  }, { behavior: "immediate" })
}

export function questionReferenceOwned(input: {
  sessionID: SessionID
  actorID: string
  messageID: string
  callID: string
}) {
  const found = Database.use((db) =>
    db
      .select({ agentID: MessageTable.agent_id, part: PartTable.data })
      .from(PartTable)
      .innerJoin(MessageTable, eq(PartTable.message_id, MessageTable.id))
      .where(
        and(
          eq(PartTable.session_id, input.sessionID),
          eq(PartTable.message_id, input.messageID as MessageID),
          eq(MessageTable.agent_id, input.actorID),
        ),
      )
      .all(),
  )
  return found.some((item) => {
    const part = item.part as MessageV2.Part
    return item.agentID === input.actorID && part.type === "tool" && part.callID === input.callID
  })
}

export function consumeBoundary(input: {
  sessionID: SessionID
  actorID: string
  binding: Binding
  afterTurnReady: boolean
  expectedReceipts?: readonly BoundaryReceiptIdentity[]
  policyBatch?: PolicyBatchEvidence
}) {
  return Database.transaction((db) => {
    const queuedProfile = hasQueuedSourceProfile(db)
    const actor = db
      .select()
      .from(ActorRegistryTable)
      .where(and(eq(ActorRegistryTable.session_id, input.sessionID), eq(ActorRegistryTable.actor_id, input.actorID)))
      .get()
    if (!actor) throw new UserInputUnavailable("actor membership is unavailable at delivery boundary")
    const rows = db
      .select()
      .from(UserInputReceiptTable)
      .where(
        and(
          eq(UserInputReceiptTable.session_id, input.sessionID),
          eq(UserInputReceiptTable.actor_id, input.actorID),
          eq(UserInputReceiptTable.owner, input.binding.owner),
          eq(UserInputReceiptTable.stable_chat_id, input.binding.stableChatID),
          inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"]),
        ),
      )
      .orderBy(UserInputReceiptTable.created_at)
      .all()
    const expected = input.expectedReceipts
    const expectedByID = expected ? new Map(expected.map((item) => [item.requestID, item])) : undefined
    if (expectedByID && (expectedByID.size !== expected!.length || expected!.length > 32)) {
      throw new UserInputConflict("boundary receipt snapshot is invalid")
    }
    if (expectedByID) {
      for (const item of expected!) {
        const row = rows.find((candidate) => candidate.request_id === item.requestID)
        if (!row || row.payload_hash !== item.payloadHash || (item.contentHash && boundaryContentHash(row) !== item.contentHash) || row.execution_revision !== item.executionRevision ||
          row.map_revision !== item.mapRevision || row.mapping_revision !== item.mappingRevision ||
          row.workspace_revision !== item.workspaceRevision || row.map_revision > input.binding.mapRevision ||
          row.mapping_revision !== input.binding.mappingRevision || row.workspace_revision !== input.binding.workspaceRevision ||
          actor.status !== "running" || (row.mode === "after-turn" && !input.afterTurnReady) ||
          !((row.admission_status === "running" && row.execution_revision === actor.execution_revision) ||
            row.claimed_execution_revision === actor.execution_revision) ||
          row.state === "consumed" || row.state === "cancelled" || row.state === "failed") {
          throw new UserInputConflict("receipt changed while its turn policy was being resolved")
        }
      }
    }
    let count = 0
    for (const row of rows) {
      if (expectedByID && !expectedByID.has(row.request_id)) continue
      const bindingMatches = !(
        row.owner !== input.binding.owner ||
        row.stable_chat_id !== input.binding.stableChatID ||
        row.map_revision > input.binding.mapRevision ||
        row.mapping_revision !== input.binding.mappingRevision ||
        row.workspace_revision !== input.binding.workspaceRevision
      )
      if (!bindingMatches) {
        db.update(UserInputReceiptTable)
          .set({ state: "failed", failure_reason: "managed session binding changed; explicit export or cancellation is required", updated_at: Date.now() })
          .where(and(rowKey(row), inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"])))
          .run()
        continue
      }
      if (row.mode === "after-turn" && !input.afterTurnReady) {
        if (row.state !== "pending-boundary") {
          db.update(UserInputReceiptTable)
            .set({ state: "pending-boundary", updated_at: Date.now() })
            .where(and(rowKey(row), eq(UserInputReceiptTable.state, row.state)))
            .run()
        }
        continue
      }
      // An idle/pending actor receipt needs a deliberate O08/O02 execution CAS
      // handoff. Revision arithmetic alone cannot prove which request started it.
      const claimedForThisRun = row.claimed_execution_revision === actor.execution_revision
      const admittedForThisRun = row.admission_status === "running" && row.execution_revision === actor.execution_revision
      if ((!claimedForThisRun && !admittedForThisRun) || actor.status !== "running") {
        if (row.state !== "pending-boundary") {
          db.update(UserInputReceiptTable).set({ state: "pending-boundary", updated_at: Date.now() }).where(rowKey(row)).run()
        }
        continue
      }
      const model = actor.effective_model ? JSON.parse(actor.effective_model) as { providerID?: string; modelID?: string } : undefined
      if (!model?.providerID || !model.modelID) throw new UserInputUnavailable("actor has no effective model for safe receipt delivery")
      const frozenOptions = row.model_request_options as FrozenModelRequestOptions
      if (frozenOptions.model && (frozenOptions.model.providerID !== model.providerID || frozenOptions.model.modelID !== model.modelID)) {
        throw new UserInputUnavailable("receipt model binding changed; export or cancel the receipt")
      }
      const reasoningEffort = frozenOptions.options?.reasoning_effort
      if (expectedByID && actor.model_request_options && !(frozenOptions.routeID && frozenOptions.optionRouteID && frozenOptions.catalogRevision && frozenOptions.model && frozenOptions.options))
        throw new UserInputUnavailable("receipt model options lack frozen descriptor evidence; refresh required")
      if (expectedByID && frozenOptions.routeID && frozenOptions.optionRouteID && frozenOptions.catalogRevision && frozenOptions.model && frozenOptions.options) {
        const persisted = db.update(ActorRegistryTable).set({ model_request_options: {
          version: 1, routeID: frozenOptions.routeID, optionRouteID: frozenOptions.optionRouteID,
          catalogRevision: frozenOptions.catalogRevision, model: frozenOptions.model,
          options: frozenOptions.options, source: { kind: "receipt", requestID: row.request_id },
        } }).where(and(eq(ActorRegistryTable.session_id, input.sessionID), eq(ActorRegistryTable.actor_id, input.actorID),
          eq(ActorRegistryTable.status, "running"), eq(ActorRegistryTable.execution_revision, actor.execution_revision)))
          .returning({ actor_id: ActorRegistryTable.actor_id }).get()
        if (!persisted) throw new UserInputConflict("actor model options changed during receipt delivery")
      }
      const refs = row.attachment_refs
      const fileParts = refs.map((ref) => {
        const [messageID, partID] = ref.split("/")
        const found = db
          .select({ part: PartTable.data })
          .from(PartTable)
          .innerJoin(MessageTable, eq(PartTable.message_id, MessageTable.id))
          .where(
            and(
              eq(PartTable.session_id, input.sessionID),
              eq(PartTable.message_id, messageID! as MessageID),
              eq(PartTable.id, partID! as PartID),
              eq(MessageTable.agent_id, input.actorID),
            ),
          )
          .get()
        if (!found || found.part.type !== "file") throw new UserInputConflict(`attachment reference unavailable at delivery: ${ref}`)
        return found.part as MessageV2.FilePart
      })
      const messageID = MessageID.ascending()
      const now = Date.now()
      const info: MessageV2.User = {
        id: messageID,
        sessionID: input.sessionID,
        agentID: input.actorID,
        role: "user",
        time: { created: now },
        agent: input.policyBatch?.batchMode === "chat" || input.policyBatch?.batchMode === "plan"
          ? input.policyBatch.batchMode : actor.agent,
        model: { providerID: model.providerID as never, modelID: model.modelID as never, ...(reasoningEffort ? { variant: reasoningEffort } : {}) },
        metadata: {
          model_request_options: row.model_request_options,
          ...(row.turn_policy ? { turn_policy: row.turn_policy } : {}),
          ...(row.effective_policy_ceiling ? { effective_policy_ceiling: row.effective_policy_ceiling } : {}),
          ...(input.policyBatch ? { turn_policy_batch: input.policyBatch } : {}),
        },
      }
      const parts: MessageV2.Part[] = []
      if (row.text) parts.push({ id: PartID.ascending(), sessionID: input.sessionID, messageID, type: "text", text: row.text })
      for (const file of fileParts) {
        parts.push({ ...file, id: PartID.ascending(), sessionID: input.sessionID, messageID })
      }
      for (const attachment of row.attachment_parts) {
        if (attachment.type === "text") {
          if (attachment.text) parts.push({ id: PartID.ascending(), sessionID: input.sessionID, messageID, type: "text", text: attachment.text })
        } else {
          parts.push({ id: PartID.ascending(), sessionID: input.sessionID, messageID, type: "file", mime: attachment.mime, filename: attachment.filename, url: attachment.url })
        }
      }
      SyncEvent.run(MessageV2.Event.Updated, { sessionID: input.sessionID, info })
      for (const part of parts) SyncEvent.run(MessageV2.Event.PartUpdated, { sessionID: input.sessionID, part, time: now })
      const changed = db
        .update(UserInputReceiptTable)
        .set({ state: "consumed", failure_reason: null, message_id: messageID, consumed_at: now, updated_at: now })
        .where(and(rowKey(row), inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary"])))
        .returning({ request_id: UserInputReceiptTable.request_id })
        .get()
      if (!changed) throw new UserInputConflict("receipt was consumed concurrently")
      if (queuedProfile && parts.length === 1 && parts[0]?.type === "text" && row.text === parts[0].text) {
        db.run(sql`UPDATE user_input_receipt SET source_message_id=${messageID}, source_part_id=${parts[0].id}
          WHERE owner=${row.owner} AND stable_chat_id=${row.stable_chat_id} AND request_id=${row.request_id}
            AND source_proof IS NOT NULL AND source_message_id IS NULL AND source_part_id IS NULL`)
      }
      const answerRefs = row.question_refs.map((ref) => {
        const separator = ref.lastIndexOf("@")
        if (separator <= 0) return undefined
        const revision = Number(ref.slice(separator + 1))
        return Number.isSafeInteger(revision) && revision > 0
          ? { requestID: ref.slice(0, separator), revision }
          : undefined
      })
      if (answerRefs.some((ref) => ref !== undefined)) {
        if (answerRefs.some((ref) => ref === undefined)) throw new UserInputConflict("receipt has an invalid versioned question reference")
        const expected = answerRefs as Array<{ requestID: string; revision: number }>
        const linked = db.select().from(QuestionRequestTable).where(and(
          eq(QuestionRequestTable.owner, row.owner),
          eq(QuestionRequestTable.stable_chat_id, row.stable_chat_id),
          eq(QuestionRequestTable.session_id, row.session_id),
          eq(QuestionRequestTable.actor_id, row.actor_id),
          eq(QuestionRequestTable.answer_request_id, row.request_id),
        )).all()
        if (linked.length !== expected.length || expected.some((ref) => !linked.some((item) => item.request_id === ref.requestID && item.question_revision === ref.revision && item.state === "answer-queued"))) {
          throw new UserInputConflict("linked question answer is unavailable at delivery")
        }
        for (const item of linked) {
          if (item.answer_disposition !== "answered" && item.answer_disposition !== "dismissed") throw new UserInputConflict("question answer disposition is unavailable at delivery")
          db.update(QuestionRequestTable).set({
            state: item.answer_disposition,
            answer_message_id: messageID,
            updated_at: now,
          }).where(and(
            eq(QuestionRequestTable.owner, row.owner),
            eq(QuestionRequestTable.stable_chat_id, row.stable_chat_id),
            eq(QuestionRequestTable.request_id, item.request_id),
            eq(QuestionRequestTable.state, "answer-queued"),
            eq(QuestionRequestTable.answer_request_id, row.request_id),
          )).run()
        }
      }
      db.update(UserInputHandoffTable).set({ outcome: "consumed", updated_at: now }).where(and(
        eq(UserInputHandoffTable.owner, row.owner),
        eq(UserInputHandoffTable.stable_chat_id, row.stable_chat_id),
        eq(UserInputHandoffTable.request_id, row.request_id),
        eq(UserInputHandoffTable.claimed_execution_revision, actor.execution_revision),
        inArray(UserInputHandoffTable.outcome, ["claimed", "running"]),
      )).run()
      count++
    }
    if (expected && count !== expected.length) throw new UserInputConflict("frozen input batch was not consumed exactly")
    return count
  }, { behavior: "immediate" })
}

/** Host-private lookup of one exact original ingress; old profiles stay unavailable. */
export function readQueuedSource(input: {owner:string;stableChatID:string;sessionID:string;requestID:string}) {
  return Database.transaction((db) => {
    if (!hasQueuedSourceProfile(db)) return {state:"unavailable" as const}
    const row=db.get<{source_proof:string|null;source_message_id:string|null;source_part_id:string|null}>(sql`
      SELECT source_proof,source_message_id,source_part_id FROM user_input_receipt
      WHERE owner=${input.owner} AND stable_chat_id=${input.stableChatID} AND session_id=${input.sessionID} AND actor_id='main' AND request_id=${input.requestID}`)
    const receipt=db.select().from(UserInputReceiptTable).where(and(eq(UserInputReceiptTable.owner,input.owner),
      eq(UserInputReceiptTable.stable_chat_id,input.stableChatID),eq(UserInputReceiptTable.session_id,input.sessionID as SessionID),
      eq(UserInputReceiptTable.actor_id,"main"),eq(UserInputReceiptTable.request_id,input.requestID))).get()
    if (!row?.source_proof || !receipt || row.source_proof.length > 16384 || !receipt.text ||
        receipt.attachment_refs.length || receipt.attachment_upload_ids.length || receipt.attachment_parts.length || receipt.question_refs.length ||
        !["persisted","pending-boundary","consumed"].includes(receipt.state) ||
        (row.source_message_id === null) !== (row.source_part_id === null) ||
        (receipt.state === "consumed" && (!row.source_message_id || !row.source_part_id || receipt.message_id !== row.source_message_id)) ||
        (receipt.state !== "consumed" && (row.source_message_id || row.source_part_id))) return {state:"unavailable" as const}
    if (receipt.state === "consumed") {
      const part=db.select({data:PartTable.data}).from(PartTable).where(and(
        eq(PartTable.session_id,input.sessionID as SessionID),eq(PartTable.message_id,row.source_message_id as MessageID),
        eq(PartTable.id,row.source_part_id as PartID))).get()
      const message=db.select({data:MessageTable.data,agentID:MessageTable.agent_id}).from(MessageTable).where(and(
        eq(MessageTable.session_id,input.sessionID as SessionID),eq(MessageTable.id,row.source_message_id as MessageID))).get()
      if (!part || part.data.type !== "text" || (part.data as MessageV2.TextPart).text !== receipt.text ||
          !message || message.agentID !== "main" || message.data.role !== "user") return {state:"unavailable" as const}
    }
    const mutationPreimage = receipt.state === 'consumed' ? enrolledMutationPreimage(db,input) : undefined
    return {state:"ready" as const,requestID:receipt.request_id,text:receipt.text,proof:row.source_proof,payloadHash:receipt.payload_hash,
      executionRevision:receipt.execution_revision,mappingRevision:receipt.mapping_revision,workspaceRevision:receipt.workspace_revision,
      turnPolicy:receipt.turn_policy ?? null, messageID:row.source_message_id,partID:row.source_part_id,
      ...(mutationPreimage ? {mutationPreimage} : {})}
  }, {behavior:'deferred'})
}

const queuedMutationSeal = Symbol('queued source fenced mutation')
const sameClosed = (left: unknown, right: unknown) => {
  if (!left || !right || typeof left !== 'object' || typeof right !== 'object' || Array.isArray(left) || Array.isArray(right)) return false
  const a=left as Record<string,unknown>, b=right as Record<string,unknown>
  const keys=Object.keys(a).sort()
  return keys.join(',')===Object.keys(b).sort().join(',') && keys.every((key)=>a[key]===b[key])
}

function validateFencedQueuedMarker(marker: {source?:unknown;fence?:unknown}, scope:{stableChatID:string;sessionID:string;requestID:string;messageID:string;partID:string}) {
  const source=marker.source as Record<string,unknown>
  const keys=['version','kind','account_id','chat_id','workspace_id','request_id','native_session_id','message_id','part_id',
    'archive_revision','archive_content_hash','text_sha256','ingress_sha256','root_operation_id','execution_revision','inner_turn_id']
  if (Object.keys(source).sort().join(',')!==keys.sort().join(',') || source.version!==1 || source.kind!=='queued_user_text'
      || source.chat_id!==scope.stableChatID || source.native_session_id!==scope.sessionID || source.request_id!==scope.requestID
      || source.message_id!==scope.messageID || source.part_id!==scope.partID
      || ['account_id','workspace_id','root_operation_id','inner_turn_id'].some((name)=>typeof source[name]!=='string' || !(source[name] as string).trim())
      || ['archive_content_hash','text_sha256','ingress_sha256'].some((name)=>typeof source[name]!=='string' || !/^[0-9a-f]{64}$/.test(source[name] as string))
      || !Number.isSafeInteger(source.archive_revision) || (source.archive_revision as number)<1
      || !Number.isSafeInteger(source.execution_revision) || (source.execution_revision as number)<0)
    throw new UserInputUnavailable('queued source marker unavailable')
  const terminal=(marker as {fence?:Record<string,unknown>}).fence
  const sourceID='queuedpart_'+createHash('sha256').update(JSON.stringify(['openclank.queued-source.v1',source.account_id,
    source.chat_id,source.native_session_id,source.request_id,source.message_id,source.part_id]),'utf8').digest('hex')
  const originID='cs_s04queued_'+createHash('sha256').update(JSON.stringify([sourceID,source.archive_revision]),'utf8').digest('hex')
  if (!terminal || Object.keys(terminal).sort().join(',')!=='covered,origin_id,source,source_id,state,version,visibility'
      || terminal.version!==1 || terminal.source_id!==sourceID || terminal.origin_id!==originID
      || !sameClosed(terminal.source,source) || !['fenced','already_fenced'].includes(String(terminal.state))
      || terminal.visibility!=='closed' || !Array.isArray(terminal.covered) || terminal.covered.length!==1
      || terminal.covered[0]!=='queued_origin_only') throw new UserInputUnavailable('queued source fence unavailable')
  return {source, fence:terminal}
}

/** Private, bounded holder inventory; host must revalidate every returned ref. */
export function pageQueuedSourceMarkers(input:{owner:string;after?:readonly [string,string,string]|null;limit:number}) {
  if (typeof input.owner!=='string' || !input.owner.trim() || input.owner.length>512 || !Number.isInteger(input.limit)
      || input.limit<1 || input.limit>128 || (input.after!=null && (!Array.isArray(input.after) || input.after.length!==3
      || input.after.some((value)=>typeof value!=='string' || !value || value.length>512)))) return {state:'unavailable' as const}
  return Database.transaction((db)=>{
    const rawColumns=db.all<Record<string,unknown>>(sql`PRAGMA table_info('user_input_receipt')`)
    const columns=rawColumns.map((column)=>column.name)
    if (!columns.includes('source_enrollment')) {
      const canonical=currentObjects.find((object)=>object.type==='table' && object.name==='user_input_receipt')
      const actual=rawColumns.map(({cid,...rest})=>rest).sort((a,b)=>String(a.name).localeCompare(String(b.name)))
      const expected=[...(canonical?.columns ?? []),...['source_proof','source_message_id','source_part_id']
        .map((name)=>({name,type:'TEXT',notnull:0,dflt_value:null,pk:0}))].sort((a,b)=>String(a.name).localeCompare(String(b.name)))
      const guards=db.get<{count:number}>(sql`SELECT count(*) AS count FROM sqlite_schema WHERE name GLOB 'queued_guard_*'`)
      if (input.after==null && canonical && JSON.stringify(actual)===JSON.stringify(expected) && guards?.count===0)
        return {state:'legacy_empty' as const}
      return {state:'unavailable' as const}
    }
    const index=db.get<{sql:string}>(sql`SELECT sql FROM sqlite_master WHERE type='index' AND name='queued_guard_receipt_page'`)
    const expected="CREATE INDEX queued_guard_receipt_page ON user_input_receipt(owner,stable_chat_id,session_id,request_id) WHERE source_enrollment IS NOT NULL"
    if (!columns.includes('source_enrollment') || !index || index.sql.replace(/\s+/g,' ').trim()!==expected)
      return {state:'unavailable' as const}
    const after=input.after
    const cursor=after ? sql`AND (stable_chat_id,session_id,request_id)>(${after[0]},${after[1]},${after[2]})` : sql``
    const rows=db.all<{stable_chat_id:string;session_id:string;request_id:string;source_message_id:string|null;source_part_id:string|null;source_enrollment:string}>(sql`
      SELECT stable_chat_id,session_id,request_id,source_message_id,source_part_id,source_enrollment
      FROM user_input_receipt INDEXED BY queued_guard_receipt_page WHERE owner=${input.owner} AND source_enrollment IS NOT NULL
      ${cursor}
      ORDER BY stable_chat_id,session_id,request_id LIMIT ${input.limit+1}`)
    const items=rows.slice(0,input.limit).map((row)=>{
      if (!row.source_message_id || !row.source_part_id || typeof row.source_enrollment!=='string' || Buffer.byteLength(row.source_enrollment,'utf8')>16384)
        throw new UserInputUnavailable('queued marker inventory unavailable')
      let marker: {version?:unknown;state?:unknown;source?:unknown;fence?:unknown}
      try { marker=JSON.parse(row.source_enrollment) } catch { throw new UserInputUnavailable('queued marker inventory unavailable') }
      const scope={stableChatID:row.stable_chat_id,sessionID:row.session_id,requestID:row.request_id,messageID:row.source_message_id,partID:row.source_part_id}
      if (!marker || marker.version!==1 || typeof marker.source!=='object' || !marker.source || Array.isArray(marker.source))
        throw new UserInputUnavailable('queued marker inventory unavailable')
      if (marker.state==='fenced' && Object.keys(marker).sort().join(',')==='fence,source,state,version') {
        const terminal=validateFencedQueuedMarker(marker,scope)
        return {state:'fenced' as const,source:terminal.source,fence:terminal.fence}
      }
      if (marker.state!=='enrolled' || Object.keys(marker).sort().join(',')!=='source,state,version')
        throw new UserInputUnavailable('queued marker inventory unavailable')
      const current=readQueuedSource({owner:input.owner,stableChatID:row.stable_chat_id,sessionID:row.session_id,requestID:row.request_id})
      if (current.state!=='ready' || !current.mutationPreimage || current.messageID!==row.source_message_id || current.partID!==row.source_part_id)
        throw new UserInputUnavailable('queued marker inventory unavailable')
      return {state:'enrolled' as const,source:marker.source,preimage:current.mutationPreimage}
    })
    const last=rows[Math.min(input.limit,rows.length)-1]
    return {state:'ready' as const,items,nextAfter:rows.length>input.limit && last
      ? [last.stable_chat_id,last.session_id,last.request_id] as const : null}
  }, {behavior:'deferred'})
}

/** Fixed host-fence → exact preimage CAS → part removal, with no authority from caller fields. */
export async function fenceAndRemoveQueuedPart(input: {sessionID:SessionID;messageID:MessageID;partID:PartID}): Promise<boolean> {
  return fenceQueuedPart(input, 'part_delete')
}

/** Holder withdrawal fences the exact source but preserves the chat part. */
export async function fenceQueuedPartForHolderRetirement(input: {sessionID:SessionID;messageID:MessageID;partID:PartID}): Promise<boolean> {
  return fenceQueuedPart(input, 'holder_retire')
}

async function fenceQueuedPart(input: {sessionID:SessionID;messageID:MessageID;partID:PartID}, mutation:'part_delete'|'holder_retire'): Promise<boolean> {
  const selected = Database.transaction((db) => {
    const columns=db.all<{name:string}>(sql`PRAGMA table_info('user_input_receipt')`).map((column)=>column.name)
    if (!columns.includes('source_enrollment')) return undefined
    const rows=db.all<{owner:string;stable_chat_id:string;request_id:string;source_enrollment:string}>(sql`
      SELECT owner,stable_chat_id,request_id,source_enrollment FROM user_input_receipt
      WHERE session_id=${input.sessionID} AND source_message_id=${input.messageID} AND source_part_id=${input.partID}
        AND source_enrollment IS NOT NULL LIMIT 2`)
    if (rows.length>1) throw new UserInputConflict('queued source identity is ambiguous')
    if (!rows.length) return undefined
    let marker: {version?:unknown;state?:unknown;source?:unknown}
    try { marker=JSON.parse(rows[0]!.source_enrollment) } catch { throw new UserInputUnavailable('queued source marker unavailable') }
    if (!marker || typeof marker!=='object' || !['source,state,version','fence,source,state,version'].includes(Object.keys(marker).sort().join(','))
        || marker.version!==1 || !marker.source || typeof marker.source!=='object' || Array.isArray(marker.source))
      throw new UserInputUnavailable('queued source marker unavailable')
    if (marker.state==='fenced') {
      validateFencedQueuedMarker(marker,{stableChatID:rows[0]!.stable_chat_id,sessionID:input.sessionID,requestID:rows[0]!.request_id,messageID:input.messageID,partID:input.partID})
      return undefined
    }
    if (marker.state!=='enrolled') throw new UserInputUnavailable('queued source marker unavailable')
    const binding={owner:rows[0]!.owner,stableChatID:rows[0]!.stable_chat_id,sessionID:input.sessionID,requestID:rows[0]!.request_id}
    const current=readQueuedSource(binding)
    if (current.state!=='ready' || !current.mutationPreimage || current.messageID!==input.messageID || current.partID!==input.partID)
      throw new UserInputUnavailable('queued source preimage unavailable')
    return {binding:Object.freeze(binding),source:Object.freeze({...marker.source as Record<string,unknown>}),
      preimage:Object.freeze({...current.mutationPreimage}),marker:rows[0]!.source_enrollment}
  }, {behavior:'deferred'})
  if (!selected) return false
  const hostCall = (async () => {
    const { managedHostCall } = await import('@/acp/managed-provider')
    return managedHostCall('_openclank/session/v1/queued-source/fence',{
      source:selected.source,preimage:selected.preimage,mutation,
    })
  })()
  let response: Awaited<typeof hostCall>
  if (mutation === 'holder_retire') {
    // The host owns the exact outstanding RPC. A timeout here would return
    // while its authenticated fence callback could still commit later.
    response=await hostCall
  } else {
    let timer: ReturnType<typeof setTimeout> | undefined
    const deadline = new Promise<never>((_,reject) => {
      timer=setTimeout(()=>reject(new UserInputUnavailable('queued source fence outcome unavailable')),4000)
    })
    try { response=await Promise.race([hostCall,deadline]) }
    finally { if (timer) clearTimeout(timer) }
  }
  if (response.state!=='fenced' || !sameClosed(response.source,selected.source) || !sameClosed(response.preimage,selected.preimage))
    throw new UserInputUnavailable('queued source fence unavailable')
  const sourceID='queuedpart_'+createHash('sha256').update(JSON.stringify(['openclank.queued-source.v1',selected.source.account_id,
    selected.source.chat_id,selected.source.native_session_id,selected.source.request_id,selected.source.message_id,selected.source.part_id]),'utf8').digest('hex')
  const originID='cs_s04queued_'+createHash('sha256').update(JSON.stringify([sourceID,selected.source.archive_revision]),'utf8').digest('hex')
  const fence=response.fence
  if (fence.version!==1 || fence.source_id!==sourceID || fence.origin_id!==originID || !sameClosed(fence.source,selected.source)
      || !['fenced','already_fenced'].includes(String(fence.state)) || fence.visibility!=='closed'
      || !Array.isArray(fence.covered) || fence.covered.length!==1 || fence.covered[0]!=='queued_origin_only')
    throw new UserInputUnavailable('queued source fence unavailable')
  // The private seal is created only after the fixed authenticated host call;
  // its one synchronous transaction never yields or accepts a caller permit.
  const permit={seal:queuedMutationSeal,consumed:false}
  return Database.transaction((db) => {
    if (permit.seal!==queuedMutationSeal || permit.consumed) throw new UserInputUnavailable('queued source permit unavailable')
    permit.consumed=true
    const current=readQueuedSource(selected.binding)
    if (current.state!=='ready' || !current.mutationPreimage || !sameClosed(current.mutationPreimage,selected.preimage))
      throw new UserInputConflict('queued source changed after fence')
    const updated=db.get<{source_enrollment:string}>(sql`UPDATE user_input_receipt
      SET source_enrollment=json_set(source_enrollment,'$.state','fenced','$.fence',json(${JSON.stringify(fence)}))
      WHERE owner=${selected.binding.owner} AND stable_chat_id=${selected.binding.stableChatID}
        AND session_id=${input.sessionID} AND request_id=${selected.binding.requestID} AND source_enrollment=${selected.marker}
      RETURNING source_enrollment`)
    if (!updated) throw new UserInputConflict('queued source changed after fence')
    if (mutation==='part_delete') SyncEvent.run(MessageV2.Event.PartRemoved,{sessionID:input.sessionID,messageID:input.messageID,partID:input.partID})
    return true
  }, {behavior:'immediate'})
}

/** Read the exact consumed receipt association; never search messages by text. */
export function consumedQueuedSources(input: { owner:string;stableChatID:string;sessionID:string;requestIDs:readonly string[] }) {
  if (input.requestIDs.length > 32) return []
  return input.requestIDs.flatMap((requestID) => {
    const row=readQueuedSource({...input,requestID})
    return row.state === "ready" && row.messageID && row.partID
      ? [{requestID,proof:row.proof,messageID:row.messageID,partID:row.partID}] : []
  })
}

export function cancel(input: {
  owner: string
  stableChatID: string
  sessionID: SessionID
  actorID: string
  requestID: string
}) {
  return Database.transaction((db) =>
    db
      .update(UserInputReceiptTable)
      .set({ state: "cancelled", updated_at: Date.now() })
      .where(
        and(
          key(input),
          eq(UserInputReceiptTable.session_id, input.sessionID),
          eq(UserInputReceiptTable.actor_id, input.actorID),
          inArray(UserInputReceiptTable.state, ["persisted", "pending-boundary", "failed"]),
        ),
      )
      .returning({ request_id: UserInputReceiptTable.request_id })
      .get() !== undefined,
  { behavior: "immediate" })
}

export function exportReceipts(input: {
  owner: string
  stableChatID: string
  sessionID: SessionID
  actorID: string
}): UserInputReceiptRow[] {
  return Database.use((db) =>
    db
      .select()
      .from(UserInputReceiptTable)
      .where(
        and(
          eq(UserInputReceiptTable.owner, input.owner),
          eq(UserInputReceiptTable.stable_chat_id, input.stableChatID),
          eq(UserInputReceiptTable.session_id, input.sessionID),
          eq(UserInputReceiptTable.actor_id, input.actorID),
        ),
      )
      .orderBy(UserInputReceiptTable.created_at)
      .all(),
  )
}

/** Passive selected-actor projection. Never loads receipt text or attachments. */
export function observeReceipts(input: Binding & { sessionID: SessionID; actorID: string; after?: string; limit: number }) {
  if (!Number.isSafeInteger(input.limit) || input.limit < 1 || input.limit > 20 ||
      (input.after !== undefined && (!input.after || Buffer.byteLength(input.after, "utf8") > 256)))
    throw new Error("invalid receipt observation page")
  return Database.use((db) => {
    const rows = db.select({ requestID: UserInputReceiptTable.request_id, state: UserInputReceiptTable.state,
      mode: UserInputReceiptTable.mode, executionRevision: UserInputReceiptTable.execution_revision,
      claimedExecutionRevision: UserInputReceiptTable.claimed_execution_revision })
      .from(UserInputReceiptTable).where(and(
        eq(UserInputReceiptTable.owner, input.owner), eq(UserInputReceiptTable.stable_chat_id, input.stableChatID),
        eq(UserInputReceiptTable.session_id, input.sessionID), eq(UserInputReceiptTable.actor_id, input.actorID),
        lte(UserInputReceiptTable.map_revision, input.mapRevision),
        eq(UserInputReceiptTable.mapping_revision, input.mappingRevision),
        eq(UserInputReceiptTable.workspace_revision, input.workspaceRevision),
        ...(input.after ? [sql`${UserInputReceiptTable.request_id} > ${input.after}`] : []),
        sql`length(CAST(${UserInputReceiptTable.request_id} AS BLOB)) BETWEEN 1 AND 256`,
      )).orderBy(UserInputReceiptTable.request_id).limit(input.limit + 1).all()
    const items = rows.slice(0, input.limit)
    return { items, next: rows.length > input.limit ? items.at(-1)?.requestID : undefined,
      coverage: "bounded_receipt_states" as const }
  })
}
