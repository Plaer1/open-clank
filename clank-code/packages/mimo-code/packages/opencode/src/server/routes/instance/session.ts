import { sharedText } from "@/mcp/shared-text"
import { frameCapture } from "@/mcp/frame-capture"
import { mediaJob } from "@/mcp/media-job"
import { nativeAnalysisInputSHA } from '@/tool/native-analysis-input'
import { catalogObservedSteps, readObservedStep, rangeObservedSteps, observedStepHead, scanObservedTurn } from '@/session/observed-step'
import { selectContext, type SelectionPreview } from "@/session/context-selection"
import { SessionCheckpoint } from "@/session/checkpoint"
import { Git } from "@/git"
import { Worktree, type CleanupResult } from "@/worktree"
import type { IndexResult, CommitResult } from "@/git/index-physical"
import { backgroundStart, backgroundRead, backgroundTerminate, backgroundRetire, backgroundWrite } from "@/mcp/background-start"
import { taskCreate, taskRead, taskControl, taskRun, taskEdit } from "@/mcp/task-create"
import { Instance } from "@/project/instance"
import { managedSessionBinding } from "@/memory/session-scope"
import { Database, and, desc, eq, lt, or } from "@/storage"
import { sql } from "drizzle-orm"
import { MessageTable, PartTable, SessionTable } from "@/session/session.sql"
import { projectContextInspection } from "@/session/context-inspection"
import { fitInspection, inspectionBindingCurrent } from "@/session/actor-inspection"
import { Hono } from "hono"
import { createHash, randomBytes } from "node:crypto"
import { lstat } from "node:fs/promises"
import path from "node:path"
import { stream } from "hono/streaming"
import { describeRoute, validator, resolver } from "hono-openapi"
import { SessionID, MessageID, PartID } from "@/session/schema"
import z from "zod"
import { Session } from "@/session"
import { MessageV2 } from "@/session/message-v2"
import { SessionPrompt } from "@/session/prompt"
import { SessionRunState } from "@/session/run-state"
import { SessionCompaction } from "@/session/compaction"
import { SessionRevert } from "@/session/revert"
import { SessionShare } from "@/share"
import { SessionStatus } from "@/session/status"
import { SessionSummary } from "@/session/summary"
import { Goal } from "@/session/goal"
import * as GoalStore from "@/session/goal-store"
import * as GoalState from "@/session/goal-state"
import { Todo } from "@/session/todo"
import { Effect } from "effect"
import { Agent } from "@/agent/agent"
import { Snapshot } from "@/snapshot"
import { Command } from "@/command"
import { Log } from "@/util"
import { ActorRegistry } from "@/actor/registry"
import { TaskRegistry } from "@/task/registry"
import { Task } from "@/task/schema"
import { ToolID } from "@/tool/schema"
import * as Truncate from "@/tool/truncate"
import { Permission } from "@/permission"
import { PermissionID } from "@/permission/schema"
import { ModelID, ProviderID } from "@/provider/schema"
import { Provider } from "@/provider"
import { forkQuery } from "@/tool/session"
import { spawnRef } from "@/actor/spawn-ref"
import { errors } from "../../error"
import { lazy } from "@/util/lazy"
import { Bus } from "@/bus"
import { NamedError } from "@mimo-ai/shared/util/error"
import { jsonRequest, runRequest } from "./trace"
import { RateLimitMiddleware } from "../../rate-limit"
import { ManagedProvider } from "@/acp/managed-provider"
import { capabilities as managedCapabilities } from "@/acp/openclank-protocol"
import * as UserInput from "@/session/user-input"
import { Question } from "@/question"
import { HTTPException } from "hono/http-exception"

const log = Log.create({ service: "server" })
const gitIndexHostGeneration=()=>{
  const required=['_openclank/session/v1/git-index/recheck','_openclank/session/v1/git-index/finish']
  return required.every(name=>managedCapabilities.methods.some(method=>(method as string)===name)) ? ManagedProvider.historySourceGeneration() : undefined
}
const gitCommitHostGeneration=()=>{
  const required=['_openclank/session/v1/git-commit/recheck','_openclank/session/v1/git-commit/finish']
  return required.every(name=>managedCapabilities.methods.some(method=>(method as string)===name)) ? ManagedProvider.historySourceGeneration() : undefined
}
const gitCleanupHostGeneration=()=>{
  const required=['_openclank/session/v1/git-cleanup/recheck','_openclank/session/v1/git-cleanup/finish']
  return required.every(name=>managedCapabilities.methods.some(method=>(method as string)===name)) ? ManagedProvider.historySourceGeneration() : undefined
}
const cleanupText=z.string().min(1).max(4096).refine(v=>Buffer.from(v).toString('utf8')===v && !v.includes('\0'))
const cleanupSafe=z.number().int().nonnegative().safe()
const cleanupDigest=z.string().regex(/^[0-9a-f]{64}$/)
const cleanupRetired=cleanupDigest.nullable()
const cleanupIdentity=z.object({device:cleanupSafe,inode:cleanupSafe.positive(),sha256:cleanupDigest}).strict()
const cleanupScopes=z.object({directory:cleanupText,sessions:z.array(z.object({id:cleanupText,directory:cleanupText,retired:cleanupRetired}).strict()).max(512),actors:z.array(z.object({sessionID:cleanupText,actorID:cleanupText,cwd:cleanupText.nullable(),revision:cleanupSafe,status:z.string().max(32),unsettled:cleanupSafe,retired:cleanupRetired,ownerRetired:cleanupRetired,ownerDirectory:cleanupText.nullable(),oversized:cleanupSafe}).strict()).max(512)}).strict()
const cleanupWitness=z.object({state:z.literal('ready'),project:cleanupText,directory:cleanupText,branch:cleanupText,headOID:z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/),trackedSHA256:cleanupDigest,device:cleanupSafe,inode:cleanupSafe.positive(),commonDir:cleanupText,commonDevice:cleanupSafe,commonInode:cleanupSafe.positive(),adminDir:cleanupText,adminDevice:cleanupSafe,adminInode:cleanupSafe.positive(),gitfile:cleanupIdentity,reciprocal:cleanupIdentity,config:cleanupIdentity,executable:z.object({path:cleanupText,device:cleanupSafe,inode:cleanupSafe.positive(),size:cleanupSafe,sha256:cleanupDigest}).strict(),scopes:cleanupScopes}).strict().refine(v=>Buffer.byteLength(JSON.stringify(v),'utf8')<=4194304)
const canonicalCleanupJSON=(value:unknown):string=>{
  if (Array.isArray(value)) return '['+value.map(canonicalCleanupJSON).join(',')+']'
  if (value && typeof value==='object') return '{'+Object.entries(value).sort(([a],[b])=>a<b?-1:a>b?1:0).map(([k,v])=>JSON.stringify(k)+':'+canonicalCleanupJSON(v)).join(',')+'}'
  const encoded=JSON.stringify(value)
  if (encoded===undefined) throw new Error('cleanup encoding unavailable')
  return encoded
}
const gitCommitDirectorySchema=z.object({device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe()}).strict()
const gitCommitFileSchema=z.object({device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe(),size:z.number().int().nonnegative().safe(),sha256:z.string().regex(/^[0-9a-f]{64}$/)}).strict()
const gitCommitPhysicalSchema=z.object({version:z.literal(1),sha256:z.string().regex(/^[0-9a-f]{64}$/),device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe(),size:z.number().int().min(32).max(4194304),objectFormat:z.enum(['sha1','sha256']),root:gitCommitDirectorySchema,git:gitCommitDirectorySchema,objects:gitCommitDirectorySchema}).strict()
const gitCommitEntrySchema=z.object({path:z.string().min(1).max(4096),mode:z.union([z.literal(0o100644),z.literal(0o100755)]),oid:z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/)}).strict()
const gitCommitPreviewSchema=z.object({version:z.literal(1),physicalIndex:gitCommitPhysicalSchema,indexEntries:z.array(gitCommitEntrySchema).max(65536),headEntries:z.array(gitCommitEntrySchema).max(65536),headOID:z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/),headRef:z.string().min(1).max(4096),config:gitCommitFileSchema,executable:gitCommitFileSchema.extend({path:z.string().min(1).max(4096),version:z.string().min(1).max(256)}).strict()}).strict().refine(value=>Buffer.byteLength(JSON.stringify(value),'utf8')<=4194304)
const gitCommitIdentitySchema=z.object({name:z.string().min(1).refine(value=>Buffer.byteLength(value)<=256 && !/[\x00-\x1f\x7f<>]/.test(value) && Buffer.from(value).toString('utf8')===value),email:z.string().min(1).refine(value=>Buffer.byteLength(value)<=256 && !/[\x00-\x1f\x7f<>]/.test(value) && Buffer.from(value).toString('utf8')===value),seconds:z.number().int().min(0).max(253402300799).safe(),timezone:z.string().regex(/^[+-](?:0\d|1[0-4])[0-5]\d$/)}).strict()

function managedReceiptBinding(sessionID: string): Effect.Effect<UserInput.Binding, Error> {
  return Effect.gen(function* () {
    const authority = yield* Effect.tryPromise({
      try: () => ManagedProvider.readManagedSessionBinding(sessionID),
      catch: (error) => new HTTPException(503, { message: `managed binding unavailable: ${String(error)}` }),
    })
    const local = yield* Effect.tryPromise({
      try: () => ManagedProvider.ensureManagedSessionBinding(sessionID),
      catch: (error) => new HTTPException(503, { message: `managed registration unavailable: ${String(error)}` }),
    })
    if (authority.authority || local?.authority) return yield* Effect.fail(new HTTPException(409, { message: "private input receipts are unavailable" }))
    if (!local || local.transition || local.engineSessionID !== sessionID ||
      local.owner !== authority.owner || local.stableChatID !== authority.stableChatID ||
      local.mapRevision > authority.mapRevision || local.mappingRevision !== authority.mappingRevision ||
      local.bindingRevision !== authority.workspaceRevision || local.authorityWorkspaceID !== authority.authorityWorkspaceID) {
      return yield* Effect.fail(new HTTPException(409, { message: "managed host binding changed or is unsettled" }))
    }
    return {
      owner: authority.owner,
      stableChatID: authority.stableChatID,
      mapRevision: authority.mapRevision,
      mappingRevision: authority.mappingRevision,
      workspaceRevision: authority.workspaceRevision,
    }
  })
}
const GoalView = z.object({
  state: GoalState.Envelope,
  replay: GoalState.Envelope.optional(),
  analytics: z.record(z.string(), z.number()),
})
const GoalTarget = z
  .object({
    goalID: z.string().min(1),
    expectedRevision: z.number().int().positive(),
  })
  .strict()
const GoalEvidenceInput = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("command"), subject: z.string().min(1), sourceRef: z.string().min(1) }).strict(),
  z.object({ kind: z.literal("file"), subject: z.string().min(1), sourceRef: z.string().min(1) }).strict(),
  z.object({ kind: z.literal("user"), subject: z.string().min(1), observation: z.string().min(1) }).strict(),
])

// Cadence of the keep-alive whitespace written on the POST /:sessionID/message
// stream while a turn is in flight. Matches the 10s SSE heartbeat in
// event.ts/global.ts. Read per-request (not memoized) so it can be tuned at
// runtime; smaller values are useful in tests.
function promptHeartbeatIntervalMs() {
  return Number(process.env["MIMOCODE_PROMPT_HEARTBEAT_INTERVAL_MS"]) || 10_000
}

function taskToTodo(t: Task) {
  const status =
    t.status === "in_progress"
      ? "in_progress"
      : t.status === "done"
        ? "completed"
        : t.status === "abandoned"
          ? "cancelled"
          : "pending"
  return { content: t.summary, status }
}

/**
 * Pick the agent identity that should drive a session-level compact:
 * scan the main slice for the most recent user message and use its agent
 * field; fall back to defaultAgent if no main user message exists.
 */
export const resolveCurrentAgent = (sessionID: SessionID, defaultAgent: string) =>
  Effect.gen(function* () {
    const session = yield* Session.Service
    const msgs = yield* session.messages({ sessionID, agentID: "main" })
    for (let i = msgs.length - 1; i >= 0; i--) {
      const info = msgs[i].info
      if (info.role === "user") return info.agent || defaultAgent
    }
    return defaultAgent
  })

export const SessionRoutes = lazy(() =>
  new Hono()
    .get(
      "/",
      describeRoute({
        summary: "List sessions",
        description: "Get a list of all OpenCode sessions, sorted by most recently updated.",
        operationId: "session.list",
        responses: {
          200: {
            description: "List of sessions",
            content: {
              "application/json": {
                schema: resolver(Session.Info.array()),
              },
            },
          },
        },
      }),
      validator(
        "query",
        z.object({
          directory: z.string().optional().meta({ description: "Filter sessions by project directory" }),
          roots: z.coerce.boolean().optional().meta({ description: "Only return root sessions (no parentID)" }),
          start: z.coerce
            .number()
            .optional()
            .meta({ description: "Filter sessions updated on or after this timestamp (milliseconds since epoch)" }),
          search: z.string().optional().meta({ description: "Filter sessions by title (case-insensitive)" }),
          limit: z.coerce.number().optional().meta({ description: "Maximum number of sessions to return" }),
        }),
      ),
      async (c) => {
        const query = c.req.valid("query")
        const sessions: Session.Info[] = []
        for await (const session of Session.list({
          directory: query.directory,
          roots: query.roots,
          start: query.start,
          search: query.search,
          limit: query.limit,
        })) {
          sessions.push(session)
        }
        return c.json(sessions)
      },
    )
    .get(
      "/status",
      describeRoute({
        summary: "Get session status",
        description: "Retrieve the current status of all sessions, including active, idle, and completed states.",
        operationId: "session.status",
        responses: {
          200: {
            description: "Get session status",
            content: {
              "application/json": {
                schema: resolver(z.record(z.string(), SessionStatus.Info)),
              },
            },
          },
          ...errors(400),
        },
      }),
      async (c) =>
        jsonRequest("SessionRoutes.status", c, function* () {
          const svc = yield* SessionStatus.Service
          return Object.fromEntries(yield* svc.list())
        }),
    )
    .post(
      "/:sessionID/actors/:actorID/input",
      describeRoute({
        summary: "Persist actor user input",
        description: "Persist an idempotent user-input receipt for delivery at a safe actor boundary; does not start or resume a runner.",
        operationId: "session.actor.input.persist",
        responses: { 202: { description: "Receipt persisted" }, ...errors(400, 404, 409, 503) },
      }),
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().min(1) })),
      validator("json", z.object({
        requestID: z.string().trim().min(1).max(200),
        executionRevision: z.number().int().nonnegative(),
        originTurnID: MessageID.zod.optional(),
        text: z.string(),
        attachmentRefs: z.string().array().default([]),
        attachmentUploadIDs: z.string().trim().min(1).max(200).array().max(100).default([]),
        attachmentParts: z.discriminatedUnion("type", [
          z.object({ type: z.literal("text"), text: z.string().max(200_000) }).strict(),
          z.object({ type: z.literal("file"), mime: z.string().max(160), filename: z.string().max(512).optional(), url: z.string().max(70_000_000) }).strict(),
        ]).array().max(200).default([]),
        questionRefs: z.string().array().default([]),
        questionAnswers: z.array(z.object({
          requestID: z.string().trim().min(1).max(200),
          questionRevision: z.number().int().positive(),
          disposition: z.enum(["answered", "dismissed"]),
          answers: z.array(z.string().array()).optional(),
        }).strict()).max(32).optional(),
        modelRequestOptions: z.object({
          present: z.boolean(),
          routeID: z.string().min(1).optional(),
          optionRouteID: z.string().min(1).optional(),
          catalogRevision: z.number().int().nonnegative().optional(),
          model: z.object({ providerID: z.string().min(1), modelID: z.string().min(1) }).strict().optional(),
          options: z.record(z.string(), z.string()).optional(),
        }).strict().optional(),
        turnPolicy: z.object({
          version: z.literal(1),
          mode: z.enum(["agent", "chat", "plan"]),
          allowBash: z.boolean().nullable(),
          allowWebSearch: z.boolean().nullable(),
          useWeb: z.boolean(),
          useResearch: z.boolean(),
          compareMode: z.boolean(),
          incognito: z.boolean(),
          noMemory: z.boolean(),
        }).strict().nullable().optional(),
        effectivePolicyCeiling: z.object({
          version: z.literal(1), blockAllToolCalls: z.boolean(),
          disabledTools: z.array(z.string().min(1).max(128)).max(512),
          hiddenTools: z.array(z.string().min(1).max(128)).max(512),
          allowedTools: z.array(z.string().min(1).max(128)).max(512).nullable(),
        }).strict().nullable().optional(),
        queuedSourceProof: z.string().min(1).max(16384).optional(),
        mode: z.enum(["steer", "after-turn"]),
      }).strict()),
      async (c) => {
        const receipt = await runRequest("SessionRoutes.actor.input.persist", c, Effect.gen(function* () {
        const { sessionID, actorID } = c.req.valid("param")
        const body = c.req.valid("json")
        const binding = yield* managedReceiptBinding(sessionID)
        const questions = yield* (yield* Question.Service).list()
        const validQuestionRefs = new Set<string>(questions.flatMap((question) =>
          question.sessionID === sessionID && question.tool && UserInput.questionReferenceOwned({
            sessionID,
            actorID,
            messageID: question.tool.messageID,
            callID: question.tool.callID,
          }) ? [String(question.id)] : [],
        ))
        const receipt = yield* Effect.try({ try: () => UserInput.admit({
          ...binding,
          requestID: body.requestID,
          sessionID,
          actorID,
          executionRevision: body.executionRevision,
          originTurnID: body.originTurnID,
          text: body.text,
          attachmentRefs: body.attachmentRefs,
          attachmentUploadIDs: body.attachmentUploadIDs,
          attachmentParts: body.attachmentParts,
          questionRefs: body.questionRefs,
          questionAnswers: body.questionAnswers,
          modelRequestOptions: body.modelRequestOptions,
          turnPolicy: body.turnPolicy,
          effectivePolicyCeiling: body.effectivePolicyCeiling,
          mode: body.mode,
          queuedSourceProof: body.queuedSourceProof,
        }, validQuestionRefs), catch: (error) => error instanceof UserInput.UserInputUnavailable
          ? new HTTPException(503, { message: error.message })
          : new HTTPException(409, { message: error instanceof Error ? error.message : String(error) }) })
        return { requestID: receipt.request_id, state: receipt.state, mode: receipt.mode, createdAt: receipt.created_at }
        }))
        return c.json(receipt, 202)
      },
    )
    .get(
      "/:sessionID/actors/:actorID/questions",
      describeRoute({
        summary: "Read persisted optional questions for an authenticated actor",
        operationId: "session.actor.questions.list",
        responses: { 200: { description: "Owner-qualified question projection" }, ...errors(400, 404, 409, 503) },
      }),
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().trim().min(1).max(200) })),
      validator("query", z.object({ limit: z.coerce.number().int().min(1).max(100).default(50), before: z.string().trim().min(1).max(200).optional(), requestID: z.string().trim().min(1).max(200).optional() })),
      async (c) => jsonRequest("SessionRoutes.actor.questions.list", c, function* () {
        const { sessionID, actorID } = c.req.valid("param")
        const query = c.req.valid("query")
        const binding = yield* managedReceiptBinding(sessionID)
          return yield* Effect.try({
          try: () => Question.listForActor({ ...binding, sessionID, actorID, limit: query.limit, before: query.before, requestID: query.requestID }),
          catch: (error) => new HTTPException(404, { message: error instanceof Error ? error.message : String(error) }),
        })
      }),
    )
    .get(
      "/:sessionID/actors/:actorID/input",
      describeRoute({ summary: "Export actor input receipts", operationId: "session.actor.input.export", responses: { 200: { description: "Durable receipt export" }, ...errors(400, 503) } }),
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().min(1) })),
      async (c) => jsonRequest("SessionRoutes.actor.input.export", c, function* () {
        const { sessionID, actorID } = c.req.valid("param")
        const binding = yield* managedReceiptBinding(sessionID)
        const receipts = UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID, actorID })
        return receipts.map((receipt) => ({
          requestID: receipt.request_id,
          owner: receipt.owner,
          stableChatID: receipt.stable_chat_id,
          sessionID: receipt.session_id,
          actorID: receipt.actor_id,
          mapRevision: receipt.map_revision,
          mappingRevision: receipt.mapping_revision,
          workspaceRevision: receipt.workspace_revision,
          executionRevision: receipt.execution_revision,
          originTurnID: receipt.origin_turn_id,
          text: receipt.text,
          attachmentRefs: receipt.attachment_refs,
          attachmentUploadIDs: receipt.attachment_upload_ids,
          attachmentCount: receipt.attachment_parts.length,
          attachmentDigests: receipt.attachment_parts.map((part) => part.type === "file" ? createHash("sha256").update(part.url).digest("hex") : createHash("sha256").update(part.text).digest("hex")),
          questionRefs: receipt.question_refs,
          questionAnswers: Question.answersForReceipt({ owner: binding.owner, stableChatID: binding.stableChatID, requestID: receipt.request_id }),
          modelRequestOptions: receipt.model_request_options,
          ...(receipt.turn_policy ? { turnPolicy: receipt.turn_policy } : {}),
          ...(receipt.effective_policy_ceiling ? { effectivePolicyCeiling: receipt.effective_policy_ceiling } : {}),
          mode: receipt.mode,
          state: receipt.state,
          failureReason: receipt.failure_reason,
          createdAt: receipt.created_at,
          updatedAt: receipt.updated_at,
          consumedAt: receipt.consumed_at,
        }))
      }),
    )
    .post(
      "/:sessionID/actors/:actorID/input/:requestID/resume",
      describeRoute({
        summary: "Deliberately resume an idle actor with a persisted input receipt",
        description: "Uses an exact idle execution revision and request-bound durable claim; duplicate handoff IDs only acknowledge the prior claim.",
        operationId: "session.actor.input.resume",
        responses: { 202: { description: "Receipt claim started" }, 200: { description: "Existing claim acknowledged" }, ...errors(400, 404, 409, 503) },
      }),
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().min(1), requestID: z.string().min(1) })),
      validator("json", z.object({
        handoffID: z.string().trim().min(1).max(200),
        expectedExecutionRevision: z.number().int().nonnegative(),
        rootOperationID: z.string().trim().min(1).max(256),
        managedRoute: z.object({
          rootOperationID: z.string().trim().min(1).max(256),
          connectionID: z.string().min(1), providerID: z.string().min(1),
          billingLane: z.enum(["subscription", "metered_api", "local", "custom", "legacy"]),
          modelID: z.string().min(1), modelRouteID: z.string().min(1),
          operationID: z.string().min(1).optional(), grantID: z.string().min(1).optional(),
          grantRevision: z.number().int().nonnegative().optional(),
          preferredAccountID: z.string().min(1).optional(), inheritedAccountID: z.string().min(1).optional(),
        }).strict().optional(),
      }).strict()),
      async (c) => {
        const result = await runRequest("SessionRoutes.actor.input.resume", c, Effect.gen(function* () {
          const { sessionID, actorID, requestID } = c.req.valid("param")
          const body = c.req.valid("json")
          const binding = yield* managedReceiptBinding(sessionID)
          const actor = spawnRef.current
          if (!actor?.resumeInput) return yield* Effect.fail(new HTTPException(503, { message: "Actor input resume is unavailable" }))
          const disposition = yield* actor.resumeInput({ sessionID, actorID, claim: {
            ...binding,
            requestID,
            handoffID: body.handoffID,
            rootOperationID: body.rootOperationID,
            expectedExecutionRevision: body.expectedExecutionRevision,
          }, ...(body.managedRoute ? { managedRoute: body.managedRoute } : {}) })
          if (disposition.kind === "unavailable")
            return yield* Effect.fail(new HTTPException(409, { message: disposition.reason }))
          return disposition
        }))
        return c.json(result, result.kind === "fresh-claim" ? 202 : 200)
      },
    )
    .delete(
      "/:sessionID/actors/:actorID/input/:requestID",
      describeRoute({ summary: "Cancel pending actor input", operationId: "session.actor.input.cancel", responses: { 200: { description: "Pending receipt cancelled" }, ...errors(400, 404, 409, 503) } }),
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().min(1), requestID: z.string().min(1) })),
      async (c) => jsonRequest("SessionRoutes.actor.input.cancel", c, function* () {
        const { sessionID, actorID, requestID } = c.req.valid("param")
        const binding = yield* managedReceiptBinding(sessionID)
        const cancelled = yield* Effect.sync(() => UserInput.cancel({
          owner: binding.owner,
          stableChatID: binding.stableChatID,
          sessionID,
          actorID,
          requestID,
        }))
        if (!cancelled) return yield* Effect.fail(new HTTPException(404, { message: "pending receipt not found or already terminal" }))
        return { requestID, state: "cancelled" as const }
      }),
    )
    .post(
      "/:sessionID/observed-step",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.union([z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),
        messageID:MessageID.zod,stepID:PartID.zod,membershipRevision:z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER)}).strict(),
        z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),messageID:MessageID.zod,actionRef:z.string().min(1).max(4096)}).strict(),
        z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),fromMessageID:MessageID.zod,toMessageID:MessageID.zod,cursor:z.string().min(1).max(4096).optional()}).strict(),
        z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),head:z.literal(true)}).strict(),
        z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),rootOperationID:z.string().min(1).max(256),headMessageID:MessageID.zod,anchorMessageID:MessageID.zod}).strict()])),
      async (c) => jsonRequest("SessionRoutes.observedStep",c,function* () {
        const {sessionID}=c.req.valid("param"), body=c.req.valid("json")
        if (!ManagedProvider.enabled()) throw new HTTPException(503,{message:"observed step unavailable"})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"observed step unavailable"})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if (!local || local.authority || local.transition || before.authority || local.engineSessionID!==sessionID
            || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner
            || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision
            || local.authorityWorkspaceID!==before.authorityWorkspaceID || local.physicalCwd!==Instance.directory
            || before.canonicalCwd!==Instance.directory || session.directory!==Instance.directory || !inspectionBindingCurrent(sessionID,local))
          throw new HTTPException(409,{message:"observed step unavailable"})
        const result=yield* Effect.try({try:()=> "rootOperationID" in body
          ? scanObservedTurn({sessionID,...body})
          : "head" in body ? observedStepHead({sessionID,stableChatID:body.stableChatID})
          : "fromMessageID" in body ? rangeObservedSteps({sessionID,...body})
          : "actionRef" in body ? catalogObservedSteps({sessionID,messageID:body.messageID,stableChatID:body.stableChatID,actionRef:body.actionRef})
          : readObservedStep({sessionID,messageID:body.messageID,stepID:body.stepID,
            membershipRevision:body.membershipRevision,stableChatID:body.stableChatID}),catch:()=>new HTTPException(404,{message:"observed step unavailable"})})
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"observed step unavailable"})})
        if (JSON.stringify(before)!==JSON.stringify(after) || !inspectionBindingCurrent(sessionID,local)
            || ("steps" in result ? !Array.isArray(result.steps) || result.steps.some((item: unknown) => !item || typeof item !== "object" || !("workspaceID" in item) || item.workspaceID !== before.authorityWorkspaceID) : "head" in result ? false : "candidates" in result ? result.candidates.some((item)=>item.workspaceID!==before.authorityWorkspaceID) : result.workspaceID!==before.authorityWorkspaceID)) throw new HTTPException(409,{message:"observed step changed"})
        return result
      }),
    )
    .get(
      "/:sessionID/goal-observation",
      validator("param", z.object({ sessionID: SessionID.zod })),
      async (c) => jsonRequest("SessionRoutes.goalObservation", c, function* () {
        const sessionID = c.req.valid("param").sessionID
        const local = managedSessionBinding(sessionID)
        const before = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(sessionID),
          catch: () => new HTTPException(503, { message: "goal observation unavailable" }) })
        const session = yield* (yield* Session.Service).get(sessionID)
        if (!local || local.authority || before.authority || local.transition || local.engineSessionID !== sessionID ||
            local.owner !== before.owner || local.stableChatID !== before.stableChatID || local.mapRevision > before.mapRevision ||
            local.mappingRevision !== before.mappingRevision || local.bindingRevision !== before.workspaceRevision ||
            local.authorityWorkspaceID !== before.authorityWorkspaceID || local.physicalCwd !== Instance.directory ||
            before.canonicalCwd !== Instance.directory || session.directory !== Instance.directory || !inspectionBindingCurrent(sessionID, local))
          throw new HTTPException(409, { message: "goal observation binding changed" })
        const goalScope = yield* GoalStore.scope(sessionID)
        if (goalScope.owner !== local.owner || goalScope.sessionID !== sessionID) throw new HTTPException(409, { message: "goal scope changed" })
        const result = yield* Effect.try({ try: () => GoalStore.observe(goalScope),
          catch: () => new HTTPException(503, { message: "goal observation unavailable" }) })
        const after = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(sessionID),
          catch: () => new HTTPException(503, { message: "goal observation unavailable" }) })
        const currentScope = yield* GoalStore.scope(sessionID)
        if (JSON.stringify(before) !== JSON.stringify(after) || !inspectionBindingCurrent(sessionID, local) ||
            JSON.stringify(goalScope) !== JSON.stringify(currentScope)) throw new HTTPException(409, { message: "goal observation changed" })
        return result
      }),
    )
    .get(
      "/:sessionID/goal",
      describeRoute({
        summary: "Inspect session goal",
        description:
          "Read the tenant-scoped durable goal, privacy-safe journal replay, queue, history, and outcome counts.",
        operationId: "session.goal.get",
        responses: {
          200: {
            description: "Goal state",
            content: { "application/json": { schema: resolver(GoalView) } },
          },
          ...errors(400, 404),
        },
      }),
      validator("param", z.object({ sessionID: SessionID.zod })),
      async (c) =>
        jsonRequest("SessionRoutes.goal.get", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const goal = yield* Goal.Service
          return {
            state: yield* goal.inspect(sessionID),
            replay: yield* goal.replay(sessionID),
            analytics: yield* goal.analytics(sessionID),
          }
        }),
    )
    .get(
      "/:sessionID/goal/journal",
      describeRoute({
        summary: "List session goal journal",
        description: "Read the append-only tenant-scoped goal lifecycle journal.",
        operationId: "session.goal.journal",
        responses: {
          200: {
            description: "Goal journal",
            content: { "application/json": { schema: resolver(GoalState.JournalEvent.array()) } },
          },
          ...errors(400, 404),
        },
      }),
      validator("param", z.object({ sessionID: SessionID.zod })),
      async (c) =>
        jsonRequest("SessionRoutes.goal.journal", c, function* () {
          const goal = yield* Goal.Service
          return yield* goal.journal(c.req.valid("param").sessionID)
        }),
    )
    .post(
      "/:sessionID/goal",
      describeRoute({
        summary: "Control session goal",
        description: "Create, pause, resume, cancel, edit, clear history, or attach typed evidence.",
        operationId: "session.goal.update",
        responses: {
          200: {
            description: "Updated goal state",
            content: { "application/json": { schema: resolver(GoalView) } },
          },
          ...errors(400, 404, 409),
        },
      }),
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator(
        "json",
        z.discriminatedUnion("action", [
          z.object({
            action: z.literal("create"),
            objective: z.string().min(1),
            budget: GoalState.Budget.partial().optional(),
            requiredEvidence: GoalState.EvidenceKind.array().optional(),
          }),
          z.object({ action: z.literal("pause"), target: GoalTarget }),
          z.object({ action: z.literal("resume"), target: GoalTarget }),
          z.object({ action: z.literal("cancel"), target: GoalTarget }),
          z.object({
            action: z.literal("clear_history"),
            expectedEnvelopeRevision: z.number().int().nonnegative(),
          }),
          z.object({
            action: z.literal("edit"),
            target: GoalTarget,
            objective: z.string().min(1),
          }),
          z.object({
            action: z.literal("evidence"),
            target: GoalTarget,
            evidence: GoalEvidenceInput,
          }),
        ]),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.goal.update", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const goal = yield* Goal.Service
          if (body.action === "create") {
            yield* goal.set(sessionID, body.objective, {
              budget: body.budget,
              requiredEvidence: body.requiredEvidence,
            })
          } else if (body.action === "pause") {
            yield* goal.pause(sessionID, body.target)
          } else if (body.action === "resume") {
            yield* goal.resume(sessionID, body.target)
          } else if (body.action === "cancel") {
            yield* goal.clear(sessionID, body.target)
          } else if (body.action === "clear_history") {
            yield* goal.clearHistory(sessionID, body.expectedEnvelopeRevision)
          } else if (body.action === "edit") {
            yield* goal.edit(sessionID, body.target, body.objective)
          } else {
            yield* goal.addEvidence(sessionID, body.target, body.evidence)
          }
          return {
            state: yield* goal.inspect(sessionID),
            replay: yield* goal.replay(sessionID),
            analytics: yield* goal.analytics(sessionID),
          }
        }),
    )
    .get(
      "/:sessionID",
      describeRoute({
        summary: "Get session",
        description: "Retrieve detailed information about a specific OpenCode session.",
        tags: ["Session"],
        operationId: "session.get",
        responses: {
          200: {
            description: "Get session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: Session.GetInput,
        }),
      ),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        return jsonRequest("SessionRoutes.get", c, function* () {
          const session = yield* Session.Service
          return yield* session.get(sessionID)
        })
      },
    )
    .get(
      "/:sessionID/children",
      describeRoute({
        summary: "Get session children",
        tags: ["Session"],
        description: "Retrieve all child sessions that were forked from the specified parent session.",
        operationId: "session.children",
        responses: {
          200: {
            description: "List of children",
            content: {
              "application/json": {
                schema: resolver(Session.Info.array()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: Session.ChildrenInput,
        }),
      ),
      validator(
        "query",
        z.object({
          visible: z.coerce
            .boolean()
            .optional()
            .meta({ description: "Only return user-visible children (peer sessions); hides internal subagent hosts" }),
        }),
      ),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        const visible = c.req.valid("query").visible
        return jsonRequest("SessionRoutes.children", c, function* () {
          const session = yield* Session.Service
          return yield* session.children(sessionID, { visible })
        })
      },
    )
    .get(
      "/:sessionID/recovery",
      describeRoute({
        summary: "List recoverable session turns",
        operationId: "session.recovery",
        responses: {
          200: { description: "Recoverable interrupted turns", content: { "application/json": { schema: resolver(z.array(z.object({ assistantMessageID: z.string(), parentMessageID: z.string(), created: z.number() }))) } } },
          ...errors(400, 404),
        },
      }),
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("query", z.object({ agentID: z.string().optional() })),
      async (c) =>
        jsonRequest("SessionRoutes.recovery", c, function* () {
          const params = c.req.valid("param")
          const query = c.req.valid("query")
          const session = yield* Session.Service
          yield* session.get(params.sessionID)
          return yield* (yield* SessionPrompt.Service).recovery({ sessionID: params.sessionID, agentID: query.agentID })
        }),
    )
    .post(
      "/:sessionID/turn/:assistantMessageID/resume",
      describeRoute({
        summary: "Resume an interrupted session turn",
        operationId: "session.resume",
        responses: { 202: { description: "Resume started" }, ...errors(400, 404, 409) },
      }),
      validator("param", z.object({ sessionID: SessionID.zod, assistantMessageID: MessageID.zod })),
      validator("query", z.object({ agentID: z.string().optional(), task_id: z.string().optional(), titleLocale: z.string().optional(), modelProviderID: ProviderID.zod.optional(), modelID: ModelID.zod.optional() }).refine((q) => (q.modelProviderID === undefined) === (q.modelID === undefined), { message: "modelProviderID and modelID must be provided together" })),
      async (c) => {
        const params = c.req.valid("param")
        const query = c.req.valid("query")
        const body = await c.req.json().catch(() => undefined)
        const recoveryEnvelope = z.object({ managedRecovery: z.object({
          rootOperationID: z.string().trim().min(1).max(256), handoffID: z.string().trim().min(1).max(200),
          expectedExecutionRevision: z.number().int().nonnegative(),
          turnPolicy: z.object({ version: z.literal(1), mode: z.enum(["agent", "chat", "plan"]),
            allowBash: z.boolean().nullable(), allowWebSearch: z.boolean().nullable(), useWeb: z.boolean(),
            useResearch: z.boolean(), compareMode: z.boolean(), incognito: z.boolean(), noMemory: z.boolean() }).strict(),
          managedRoute: z.object({
            rootOperationID: z.string().trim().min(1).max(256), connectionID: z.string().min(1), providerID: z.string().min(1),
            billingLane: z.enum(["subscription", "metered_api", "local", "custom", "legacy"]), modelID: z.string().min(1),
            modelRouteID: z.string().min(1), grantID: z.string().min(1).optional(), grantRevision: z.number().int().nonnegative().optional(),
            preferredAccountID: z.string().min(1).optional(), inheritedAccountID: z.string().min(1).optional(),
          }).strict(),
        }).strict() }).strict().safeParse(body)
        if (ManagedProvider.enabled() && !recoveryEnvelope.success)
          throw new HTTPException(400, { message: "managed recovery reservation is required" })
        if (body !== undefined && !recoveryEnvelope.success)
          throw new HTTPException(400, { message: "invalid recovery reservation" })
        await runRequest("SessionRoutes.resume", c, Effect.gen(function* () {
          yield* (yield* SessionPrompt.Service).resumeBackground({
            sessionID: params.sessionID,
            assistantMessageID: params.assistantMessageID,
            agentID: query.agentID,
            task_id: query.task_id,
            titleLocale: query.titleLocale,
            model: query.modelProviderID && query.modelID ? { providerID: query.modelProviderID, modelID: query.modelID } : undefined,
            ...(recoveryEnvelope.success ? { managedRecovery: recoveryEnvelope.data.managedRecovery } : {}),
          })
        }))
        return c.json({ resumed: true }, 202)
      },
    )
    .get(
      "/:sessionID/todo",
      describeRoute({
        summary: "Get session todos",
        description: "Retrieve the todo list associated with a specific session, showing tasks and action items.",
        operationId: "session.todo",
        responses: {
          200: {
            description: "Todo list",
            content: {
              "application/json": {
                schema: resolver(Todo.Info.array()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        return jsonRequest("SessionRoutes.todo", c, function* () {
          const reg = yield* TaskRegistry.Service
          const tasks = yield* reg.list({ session_id: sessionID, include_terminal: true })
          if (tasks.length > 0) return tasks.map(taskToTodo)
          const todo = yield* Todo.Service
          return yield* todo.get(sessionID)
        })
      },
    )
    .get(
      "/:sessionID/observation-page/:source",
      describeRoute({ summary: "Read a bounded actor, task, context, peer or recorded workflow page", operationId: "session.observationPage",
        responses: { 200: { description: "Source-native live page" }, ...errors(400, 404) } }),
      validator("param", z.object({ sessionID: SessionID.zod, source: z.enum(["task", "actor", "context", "peer", "workflow"]) })),
      validator("query", z.object({ limit: z.coerce.number().int().min(1).max(100).default(50),
        after: z.string().min(1).max(512).optional(), target: z.string().min(1).max(256).optional(), directory: z.string().optional() }).strict()),
      async (c) => jsonRequest("SessionRoutes.observationPage", c, function* () {
        const { sessionID, source } = c.req.valid("param")
        const query = c.req.valid("query")
        yield* (yield* Session.Service).get(sessionID)
        if (source !== "peer" && query.target !== undefined) throw new HTTPException(400,{message:"invalid observation target"})
        if (source === "workflow") {
          if (query.limit > 32 || (query.after !== undefined && !/^[1-9][0-9]{0,15}$/.test(query.after)))
            throw new HTTPException(400,{message:"invalid workflow cursor"})
          const after=query.after === undefined ? 0 : Number(query.after)
          if (!Number.isSafeInteger(after)) throw new HTTPException(400,{message:"invalid workflow cursor"})
          const rows=Database.use(db=>db.all<{rowid:number;id:string;status:string;running:number;succeeded:number;failed:number;created:number;updated:number}>(sql`
            SELECT rowid,CASE WHEN length(CAST(id AS BLOB))<=67 THEN id ELSE NULL END AS id,
              CASE WHEN length(CAST(status AS BLOB))<=9 THEN status ELSE NULL END AS status,
              running,succeeded,failed,time_created AS created,time_updated AS updated
            FROM workflow_run INDEXED BY workflow_run_session_idx WHERE session_id=${sessionID} AND rowid>${after}
            ORDER BY rowid LIMIT 33`))
          const items=rows.slice(0,query.limit)
          for (const row of items) if (!Number.isSafeInteger(row.rowid) || row.rowid<=after ||
            typeof row.id!=="string" || !/^wf_(?:[0-9A-Za-z-]{26}|[0-9a-f]{64})$/.test(row.id) ||
            !["running","completed","failed","cancelled"].includes(row.status) ||
            ![row.running,row.succeeded,row.failed,row.created,row.updated].every(value=>Number.isSafeInteger(value) && value>=0))
              throw new HTTPException(503,{message:"workflow observation unavailable"})
          return {items:items.map(({rowid,...item})=>item),...(rows.length>items.length && items.length ? {nextCursor:String(items[items.length-1].rowid)} : {}),observedAt:Date.now()}
        }
        if (source === "peer") {
          const canonicalPeer=(id:SessionID)=>Database.use(db=>db.get<{mode:string}>(sql`
            SELECT mode FROM actor_registry WHERE session_id=${id} AND actor_id=${id}`))?.mode === "peer"
          if (query.target !== undefined) {
            if (query.after !== undefined || query.limit !== 1) throw new HTTPException(400,{message:"invalid peer proof"})
            const parsed=SessionID.zod.safeParse(query.target)
            if (!parsed.success) throw new HTTPException(400,{message:"invalid peer proof"})
            const child=yield* (yield* Session.Service).get(parsed.data)
            if (child.parentID!==sessionID || !canonicalPeer(child.id)) throw new HTTPException(404,{message:"peer proof unavailable"})
            return {items:[{sessionId:child.id,parentSessionId:sessionID,directory:child.directory}],observedAt:Date.now()}
          }
          if (query.limit > 4 || (query.after !== undefined && !/^[1-9][0-9]{0,15}$/.test(query.after)))
            throw new HTTPException(400, {message:"peer observation cursor unavailable"})
          const after=query.after === undefined ? 0 : Number(query.after)
          if (!Number.isSafeInteger(after)) throw new HTTPException(400,{message:"peer observation cursor unavailable"})
          const rows=Database.use(db=>db.all<{rowid:number; id:SessionID}>(sql`
            SELECT rowid,id FROM session INDEXED BY session_parent_idx
            WHERE parent_id=${sessionID} AND rowid>${after} ORDER BY rowid LIMIT 33`))
          const items:{sessionId:SessionID;parentSessionId:SessionID}[]=[]
          let last=0, examined=0
          for (const row of rows.slice(0,32)) {
            if (!Number.isSafeInteger(row.rowid) || row.rowid<=after || typeof row.id!=="string" || !row.id || row.id.length>256)
              throw new HTTPException(503,{message:"peer observation unavailable"})
            last=row.rowid;examined++
            if (canonicalPeer(row.id)) items.push({sessionId:row.id,parentSessionId:sessionID})
            if (items.length===query.limit) break
          }
          return {items,...(examined<rows.length && last ? {nextCursor:String(last)} : {}),observedAt:Date.now()}
        }
        if (source === "context") {
          if (query.limit > 20) throw new HTTPException(400, { message: "context page limit exceeded" })
          let before: ReturnType<typeof MessageV2.cursor.decode> | undefined
          try { before = query.after ? MessageV2.cursor.decode(query.after) : undefined }
          catch { throw new HTTPException(400, { message: "invalid context cursor" }) }
          const rows = Database.use((db) => db.select({ id: MessageTable.id, time: MessageTable.time_created,
            receipt: sql<string | null>`CASE WHEN length(json_extract(${MessageTable.data}, '$.metadata.contextInspection')) <= 524288 THEN json_extract(${MessageTable.data}, '$.metadata.contextInspection') ELSE NULL END` })
            .from(MessageTable).where(and(eq(MessageTable.session_id, sessionID), eq(MessageTable.agent_id, "main"),
              sql`json_extract(${MessageTable.data}, '$.role') = 'assistant'`,
              ...(before ? [or(lt(MessageTable.time_created, before.time), and(eq(MessageTable.time_created, before.time), lt(MessageTable.id, before.id)))] : [])))
            .orderBy(desc(MessageTable.time_created), desc(MessageTable.id)).limit(query.limit + 1).all())
          const slice = rows.slice(0, query.limit), tail = slice.at(-1)
          return {items: slice.map((row) => {
            let receipt: ReturnType<typeof projectContextInspection>
            try { receipt = row.receipt ? projectContextInspection(JSON.parse(row.receipt)) : undefined } catch { receipt = undefined }
            return { messageID: row.id, availability: receipt ? "available" : "unavailable", ...(receipt ? { receipt } : {}) }
          }), ...(rows.length > query.limit && tail ? {nextCursor: MessageV2.cursor.encode({id:tail.id,time:tail.time})} : {}), observedAt: Date.now() }
        }
        const page = source === "task"
          ? yield* Effect.gen(function* () {
              const registry = yield* TaskRegistry.Service
              if (!registry.pageBySession) return yield* Effect.fail(new HTTPException(503, { message: "task paging is unavailable" }))
              return yield* registry.pageBySession({ session_id: sessionID, limit: query.limit, after: query.after })
            })
          : yield* Effect.gen(function* () {
              const registry = yield* ActorRegistry.Service
              if (!registry.pageBySession) return yield* Effect.fail(new HTTPException(503, { message: "actor paging is unavailable" }))
              return yield* registry.pageBySession({ sessionID, limit: query.limit, after: query.after })
            })
        return { ...page, observedAt: Date.now() }
      }),
    )
    .get(
      "/:sessionID/actor-inspection/:actorID",
      validator("param", z.object({ sessionID: SessionID.zod, actorID: z.string().min(1).max(200) })),
      validator("query", z.object({ rootSessionID: SessionID.zod, executionRevision: z.coerce.number().int().nonnegative(),
        messageBefore: z.string().min(1).max(256).optional(), receiptAfter: z.string().min(1).max(256).optional(), directory: z.string().optional() }).strict()),
      async (c) => jsonRequest("SessionRoutes.actorInspection", c, function* () {
        const { sessionID, actorID } = c.req.valid("param"), query = c.req.valid("query")
        const local = managedSessionBinding(query.rootSessionID)
        const before = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(query.rootSessionID), catch: () => new HTTPException(503, { message: "inspection unavailable" }) })
        if (!local || local.authority || before.authority || local.transition || local.engineSessionID !== query.rootSessionID ||
            local.owner !== before.owner || local.stableChatID !== before.stableChatID || local.mapRevision > before.mapRevision ||
            local.mappingRevision !== before.mappingRevision || local.bindingRevision !== before.workspaceRevision ||
            local.authorityWorkspaceID !== before.authorityWorkspaceID) throw new HTTPException(409, { message: "inspection binding changed" })
        const registry = yield* ActorRegistry.Service
        const sessions = yield* Session.Service
        const target = yield* sessions.get(sessionID)
        if (sessionID !== query.rootSessionID && target.parentID !== query.rootSessionID)
          throw new HTTPException(404, { message: "inspection peer unavailable" })
        const actor = yield* registry.get(sessionID, actorID)
        if (!actor || actor.executionRevision !== query.executionRevision) throw new HTTPException(409, { message: "actor changed" })
        const messages = Database.use((db) => db.all<{ id:string; text:string | null }>(sql`
          SELECT p.id, CASE WHEN length(CAST(p.data AS BLOB)) <= 4096 THEN
            CASE WHEN json_valid(p.data) AND json_extract(p.data,'$.type')='text'
              AND length(CAST(json_extract(p.data,'$.text') AS BLOB)) <= 2048
              THEN json_extract(p.data,'$.text') ELSE NULL END ELSE NULL END AS text
          FROM message m JOIN part p ON p.message_id=m.id AND p.session_id=m.session_id
          WHERE m.session_id=${sessionID} AND m.agent_id=${actorID}
            AND length(CAST(p.id AS BLOB)) BETWEEN 1 AND 256
            AND (${query.messageBefore ?? null} IS NULL OR p.id < ${query.messageBefore ?? null})
          ORDER BY p.id DESC LIMIT 21`))
        const receipts = UserInput.observeReceipts({ owner:before.owner, stableChatID:before.stableChatID,
          mapRevision:before.mapRevision, mappingRevision:before.mappingRevision, workspaceRevision:before.workspaceRevision,
          sessionID, actorID, limit:20, after:query.receiptAfter })
        const after = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(query.rootSessionID), catch: () => new HTTPException(503, { message: "inspection unavailable" }) })
        const current = yield* registry.get(sessionID, actorID)
        const currentTarget = yield* sessions.get(sessionID)
        if (JSON.stringify(before) !== JSON.stringify(after) || !inspectionBindingCurrent(query.rootSessionID, local) ||
            !current || current.executionRevision !== query.executionRevision || current.status !== actor.status || currentTarget.parentID !== target.parentID)
          throw new HTTPException(409, { message: "inspection changed" })
        return fitInspection({ actorID, sessionID, executionRevision:actor.executionRevision, status:actor.status,
          messages:messages.slice(0,20), ...(messages.length > 20 ? {messageNext:messages[19]!.id} : {}), receipts, coverage:"partial" })
      }),
    )
    .post(
      "/:sessionID/background-start-source",
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("json", z.object({ stableChatID:z.string().min(1).max(512), messageID:z.string().min(1).max(256),
        actorID:z.string().min(1).max(200), rootOperationID:z.string().min(1).max(256), executionRevision:z.number().int().nonnegative(),
        innerTurnID:z.string().min(1).max(128), toolCallID:z.string().min(1).max(256), toolName:z.string().min(1).max(256),
        argsSHA256:z.string().regex(/^[0-9a-f]{64}$/), sourceSessionID:SessionID.zod.optional() }).strict()),
      async (c) => jsonRequest("SessionRoutes.backgroundStartSource", c, function* () {
        const { sessionID } = c.req.valid("param"), body = c.req.valid("json")
        const mode = c.req.query('mode')
        const shared = mode === 'shared-text'
        const selectorAllowed = shared || ['media','frame','task-read','task-create','task-control','task-run','task-edit','start','read','terminate','write','retire'].includes(mode ?? '')
        const selected = body.sourceSessionID !== undefined
        if ((shared && !selected) || (selected && !selectorAllowed)) throw new HTTPException(409,{message:"background source unavailable"})
        const sourceSessionID = body.sourceSessionID ?? sessionID
        if (!ManagedProvider.enabled()) throw new HTTPException(503, {message:"background source unavailable"})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"background source unavailable"})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if (!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID ||
          local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner ||
          local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || session.directory!==Instance.directory ||
          local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision)
          throw new HTTPException(409,{message:"background source unavailable"})
        // Only approved modes may identify an actor session distinct from the mapped root.
        // Re-read the bounded native ancestry and actor on both sides of the source proof.
        const sessions = yield* Session.Service
        const lineage = Effect.gen(function* () {
          if (!selected) return undefined
          const registry = yield* ActorRegistry.Service
          const seen = new Set<string>()
          const chain: {id:string;parentID?:string;directory:string;projectID:string}[] = []
          let id = sourceSessionID
          for (let depth = 0; depth < 32; depth++) {
            if (seen.has(id)) return undefined
            seen.add(id)
            const scopedBinding = managedSessionBinding(id)
            if (scopedBinding?.authority || scopedBinding?.transition) return undefined
            const item = yield* sessions.get(id).pipe(Effect.orElseSucceed(() => undefined))
            if (!item || item.directory !== Instance.directory || item.projectID !== session.projectID) return undefined
            chain.push({id:item.id,parentID:item.parentID,directory:item.directory,projectID:item.projectID})
            if (id === sessionID) {
              const actor = yield* registry.get(sourceSessionID, body.actorID)
              if (!actor || actor.status !== 'running' || actor.executionRevision !== body.executionRevision) return undefined
              return {chain, actor:{status:actor.status,executionRevision:actor.executionRevision}}
            }
            if (!item.parentID) return undefined
            id = item.parentID
          }
          return undefined
        })
        const originalLineage = yield* lineage
        if (selected && !originalLineage) throw new HTTPException(409,{message:"background source unavailable"})
        const readMatches=()=>Database.use((db)=>{
          const message=db.select({ data:MessageTable.data }).from(MessageTable).where(and(eq(MessageTable.session_id,sourceSessionID),eq(MessageTable.id,MessageID.make(body.messageID)),sql`length(CAST(${MessageTable.data} AS BLOB)) <= 262144`)).get()
          const meta=(message?.data as any)?.metadata?.managedExecution
          if (!meta || meta.sessionID!==sourceSessionID || meta.actorID!==body.actorID || meta.rootOperationID!==body.rootOperationID ||
              meta.executionRevision!==body.executionRevision || meta.innerTurnID!==body.innerTurnID) return []
          return db.select({data:sql<string | null>`CASE WHEN length(CAST(${PartTable.data} AS BLOB)) <= 262144 THEN ${PartTable.data} ELSE NULL END`}).from(PartTable).where(and(eq(PartTable.session_id,sourceSessionID),
            eq(PartTable.message_id,MessageID.make(body.messageID)),
            sql`json_extract(${PartTable.data}, '$.type') = 'tool'`,sql`json_extract(${PartTable.data}, '$.callID') = ${body.toolCallID}`)).limit(2).all()
        })
        const valid=()=>{
          const matches=readMatches()
          let part: any
          try { part=matches.length===1 && matches[0].data ? JSON.parse(matches[0].data) : undefined } catch { return false }
          const normalized=part?.tool===body.toolName && part.state?.status==="running"
            ? (c.req.query('mode') === 'frame' ? frameCapture(part.state.input) : c.req.query('mode') === 'media' ? mediaJob(part.state.input) : c.req.query('mode') === 'shared-text' ? sharedText(part.state.input) : c.req.query('mode') === 'task-edit' ? taskEdit(part.state.input) : c.req.query('mode') === 'task-run' ? taskRun(part.state.input) : c.req.query('mode') === 'task-control' ? taskControl(part.state.input) : c.req.query('mode') === 'task-create' ? taskCreate(part.state.input) : c.req.query('mode') === 'task-read' ? taskRead(part.state.input) : c.req.query('mode') === 'read' ? backgroundRead(part.state.input) :
              c.req.query('mode') === 'terminate' ? backgroundTerminate(part.state.input) :
              c.req.query('mode') === 'write' ? backgroundWrite(part.state.input) :
              c.req.query('mode') === 'retire' ? backgroundRetire(part.state.input) : backgroundStart(part.state.input)) : undefined
          if ((c.req.query('mode') === 'frame' || c.req.query('mode') === 'media' || c.req.query('mode') === 'shared-text' || c.req.query('mode') === 'task-create' || c.req.query('mode') === 'task-control' || c.req.query('mode') === 'task-run' || c.req.query('mode') === 'task-edit') && (typeof part?.id !== 'string' ||
              !ManagedProvider.isClaimedFreshTaskCall({ sessionID:sourceSessionID, rootOperationID:body.rootOperationID,
                actorID:body.actorID, executionRevision:body.executionRevision, innerTurnID:body.innerTurnID,
                messageID:body.messageID, partID:part.id, toolCallID:body.toolCallID, toolName:body.toolName }))) return false
          return normalized?.argsSHA256===body.argsSHA256
        }
        if (!valid()) throw new HTTPException(409,{message:"background source unavailable"})
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"background source unavailable"})})
        const currentLineage = yield* lineage
        if (JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local) ||
            (selected && JSON.stringify(currentLineage)!==JSON.stringify(originalLineage)) || !valid()) throw new HTTPException(409,{message:"background source unavailable"})
        return {state:"current" as const,argsSHA256:body.argsSHA256}
      }),
    )
    .post(
      "/:sessionID/native-analysis-candidates",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({stableChatID:z.string().min(1).max(512),cursor:z.string().max(1024).optional()}).strict()),
      async (c) => jsonRequest("SessionRoutes.nativeAnalysisCandidates",c,function* () {
        const {sessionID}=c.req.valid('param'), body=c.req.valid('json')
        if (!ManagedProvider.enabled()) throw new HTTPException(503,{message:'analysis candidates unavailable'})
        let beforeID:string|undefined
        if (body.cursor) {
          try {
            const decoded=JSON.parse(Buffer.from(body.cursor,'base64url').toString('utf8'))
            if (!Array.isArray(decoded) || decoded.length!==2 || decoded[0]!==sessionID || typeof decoded[1]!=='string' || !/^prt_[A-Za-z0-9]+$/.test(decoded[1]) || Buffer.from(JSON.stringify(decoded)).toString('base64url')!==body.cursor) throw new Error()
            beforeID=decoded[1]
          } catch { throw new HTTPException(400,{message:'invalid candidate cursor'}) }
        }
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'analysis candidates unavailable'})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if (!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || session.directory!==Instance.directory || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision) throw new HTTPException(409,{message:'analysis candidates unavailable'})
        const rows=Database.use(db=>db.select({id:PartTable.id, messageID:PartTable.message_id,
          part:sql<string|null>`CASE WHEN length(CAST(${PartTable.data} AS BLOB)) <= 262144 THEN ${PartTable.data} ELSE NULL END`,
          message:sql<string|null>`CASE WHEN length(CAST(${MessageTable.data} AS BLOB)) <= 262144 THEN ${MessageTable.data} ELSE NULL END`})
          .from(PartTable).innerJoin(MessageTable,and(eq(MessageTable.id,PartTable.message_id),eq(MessageTable.session_id,PartTable.session_id)))
          .where(and(eq(PartTable.session_id,sessionID),eq(MessageTable.agent_id,'main'),...(beforeID?[lt(PartTable.id,PartID.make(beforeID))]:[])))
          .orderBy(desc(PartTable.id)).limit(50).all())
        const items:Array<{messageID:string,callID:string,operation:string,provisional:string,actorID:string,rootOperationID:string,innerTurnID:string,executionRevision:number,inputSHA256:string}> = []
        let ambiguityBudget=100-rows.length
        const messageParts=new Map<string,Array<any>>()
        let last:string|undefined, scanned=0, partial=false
        for (const row of rows) {
          if (scanned>=100 || items.length>=20) {partial=true;break}
          let part:any, message:any
          try {part=row.part?JSON.parse(row.part):undefined; message=row.message?JSON.parse(row.message):undefined} catch {part=undefined}
          const meta=message?.metadata?.managedExecution, seal=part?.state?.metadata?.analysisProvisional
          const valid=message?.role==='assistant' && part?.type==='tool' && part.tool==='native_analysis' && part.state?.status==='completed' && part.state?.metadata?.truncated===false && typeof part.callID==='string' && part.callID.length>0 && part.callID.length<=256 && typeof seal==='string' && /^ma2r1\.[A-Za-z0-9_-]+={0,2}$/.test(seal) && seal.length<=16384 && meta?.sessionID===sessionID && meta.actorID==='main' && typeof meta.rootOperationID==='string' && meta.rootOperationID.length>0 && meta.rootOperationID.length<=256 && typeof meta.innerTurnID==='string' && meta.innerTurnID.length>0 && meta.innerTurnID.length<=128 && Number.isSafeInteger(meta.executionRevision) && meta.executionRevision>=0 && nativeAnalysisInputSHA(part.state?.input)
          let unique=false
          if(valid) {
            const messageID=String(row.messageID)
            let siblings=messageParts.get(messageID)
            if(!siblings) {
              if(ambiguityBudget<=0)throw new HTTPException(409,{message:'candidate ambiguity budget exceeded'})
              const raw=Database.use(db=>db.select({data:sql<string|null>`CASE WHEN length(CAST(${PartTable.data} AS BLOB)) <= 262144 THEN ${PartTable.data} ELSE NULL END`}).from(PartTable).where(and(eq(PartTable.session_id,sessionID),eq(PartTable.message_id,row.messageID))).limit(ambiguityBudget).all())
              if(raw.length>=ambiguityBudget)throw new HTTPException(409,{message:'candidate ambiguity budget exceeded'})
              ambiguityBudget-=raw.length
              try {siblings=raw.map(value=>{if(!value.data)throw new Error();return JSON.parse(value.data)})} catch {siblings=[]}
              messageParts.set(messageID,siblings)
            }
            unique=siblings.filter(value=>value?.type==='tool'&&value.callID===part.callID).length===1
          }
          const item=unique?{messageID:String(row.messageID),callID:part.callID,operation:String(part.state.input.operation ?? 'functions'),provisional:seal,actorID:meta.actorID,rootOperationID:meta.rootOperationID,innerTurnID:meta.innerTurnID,executionRevision:meta.executionRevision,inputSHA256:nativeAnalysisInputSHA(part.state.input)!}:undefined
          if (item && Buffer.byteLength(JSON.stringify({items:[...items,item],partial:true,cursor:'x'.repeat(1024)}),'utf8')>65536) {partial=true;break}
          scanned++;last=String(row.id);if(item)items.push(item)
        }
        if (scanned===50) partial=true
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'analysis candidates unavailable'})})
        if (JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local)) throw new HTTPException(409,{message:'analysis candidates unavailable'})
        return {items,partial,...(partial && last?{cursor:Buffer.from(JSON.stringify([sessionID,last])).toString('base64url')}:{})}
      }),
    )
    .post(
      "/:sessionID/native-analysis-proof",
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("json", z.object({stableChatID:z.string().min(1).max(512),messageID:z.string().min(1).max(256),
        actorID:z.string().min(1).max(200),rootOperationID:z.string().min(1).max(256),executionRevision:z.number().int().nonnegative(),
        innerTurnID:z.string().min(1).max(128),callID:z.string().min(1).max(256),inputSHA256:z.string().regex(/^[0-9a-f]{64}$/),
        phase:z.enum(['running','completed']),outputSHA256:z.string().regex(/^[0-9a-f]{64}$/).optional(),
        outputSize:z.number().int().min(1).max(51200).optional(),provisional:z.string().min(8).max(16384).optional()}).strict()),
      async (c) => jsonRequest("SessionRoutes.nativeAnalysisProof", c, function* () {
        const {sessionID}=c.req.valid('param'), body=c.req.valid('json')
        if (!ManagedProvider.enabled()) throw new HTTPException(503,{message:'analysis proof unavailable'})
        if (body.phase==='running' ? body.outputSHA256!==undefined || body.outputSize!==undefined || body.provisional!==undefined
          : body.outputSHA256===undefined || body.outputSize===undefined || body.provisional===undefined)
          throw new HTTPException(409,{message:'analysis proof unavailable'})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'analysis proof unavailable'})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if (!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID
          || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner
          || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || session.directory!==Instance.directory
          || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision)
          throw new HTTPException(409,{message:'analysis proof unavailable'})
        const valid=()=>Database.use(db=>{
          const message=db.select({data:MessageTable.data}).from(MessageTable).where(and(eq(MessageTable.session_id,sessionID),eq(MessageTable.id,MessageID.make(body.messageID)),sql`length(CAST(${MessageTable.data} AS BLOB)) <= 262144`)).get()
          const meta=(message?.data as any)?.metadata?.managedExecution
          if (!meta || meta.sessionID!==sessionID || meta.actorID!==body.actorID || meta.rootOperationID!==body.rootOperationID || meta.executionRevision!==body.executionRevision || meta.innerTurnID!==body.innerTurnID) return false
          const matches=db.select({data:sql<string | null>`CASE WHEN length(CAST(${PartTable.data} AS BLOB)) <= 262144 THEN ${PartTable.data} ELSE NULL END`}).from(PartTable).where(and(eq(PartTable.session_id,sessionID),eq(PartTable.message_id,MessageID.make(body.messageID)),sql`json_extract(${PartTable.data}, '$.type') = 'tool'`,sql`json_extract(${PartTable.data}, '$.callID') = ${body.callID}`)).limit(2).all()
          let part:any
          try { part=matches.length===1 && matches[0].data ? JSON.parse(matches[0].data) : undefined } catch { return false }
          if (!part || part.tool!=='native_analysis' || nativeAnalysisInputSHA(part.state?.input)!==body.inputSHA256 || part.state?.status!==body.phase) return false
          if (body.phase==='running') return true
          const output=part.state?.output, metadata=part.state?.metadata
          return typeof output==='string' && Buffer.byteLength(output,'utf8')===body.outputSize
            && createHash('sha256').update(output,'utf8').digest('hex')===body.outputSHA256
            && metadata?.truncated===false && metadata?.analysisProvisional===body.provisional
        })
        if (!valid()) throw new HTTPException(409,{message:'analysis proof unavailable'})
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'analysis proof unavailable'})})
        if (JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local) || !valid())
          throw new HTTPException(409,{message:'analysis proof unavailable'})
        return {state:'current' as const}
      }),
    )
    .post(
      "/:sessionID/rating-source",
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("json", z.union([z.object({ stableChatID:z.string().min(1).max(512) }).strict(), z.object({ stableChatID:z.string().min(1).max(512), version:z.literal(2) }).strict()])),
      async (c) => jsonRequest("SessionRoutes.ratingSource", c, function* () {
        const { sessionID } = c.req.valid("param"), body=c.req.valid("json")
        if (!ManagedProvider.enabled()) throw new HTTPException(503, {message:"rating source unavailable"})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"rating source unavailable"})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if (!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID || local.stableChatID!==body.stableChatID
          || before.stableChatID!==body.stableChatID || local.owner!==before.owner || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory
          || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision || session.directory!==Instance.directory)
          throw new HTTPException(409,{message:"rating source unavailable"})
        const runState=yield* SessionRunState.Service, checkpoint=yield* SessionCheckpoint.Service
        if(!runState.busyGuard || !checkpoint.busyGuard) throw new HTTPException(503,{message:"rating source unavailable"})
        const runnerBusy=yield* runState.busyGuard(sessionID,"main"), checkpointBusy=checkpoint.busyGuard(sessionID)
        if(runnerBusy() || checkpointBusy()) throw new HTTPException(409,{message:"rating source unavailable"})
        const snapshot = Database.transaction((db) => {
          const sha=(value:string)=>createHash("sha256").update(value,"utf8").digest("hex")
          const sessionRow=db.select({id:SessionTable.id,revert:sql<string | null>`${SessionTable.revert}`,size:sql<number>`length(CAST(${SessionTable.revert} AS BLOB))`}).from(SessionTable).where(eq(SessionTable.id,sessionID)).get()
          if (!sessionRow || (sessionRow.size!==null && sessionRow.size>16384)) throw new Error("rating source unavailable")
          const revert=sessionRow.revert ? JSON.parse(sessionRow.revert) as {messageID?:string,partID?:string} : null
          if (revert && (typeof revert.messageID!=="string" || (revert.partID!==undefined && typeof revert.partID!=="string"))) throw new Error("rating source unavailable")
          const rows=db.select({id:MessageTable.id,agent:MessageTable.agent_id,created:MessageTable.time_created,updated:MessageTable.time_updated,size:sql<number>`length(CAST(${MessageTable.data} AS BLOB))`}).from(MessageTable).where(eq(MessageTable.session_id,sessionID)).orderBy(MessageTable.time_created,MessageTable.id).limit(257).all()
          if(rows.length>256) throw new Error("rating source unavailable")
          const items:Array<Record<string,unknown>>=[], coverage:Array<Record<string,unknown>>=[], observations:Array<unknown>=[], excluded={nonMain:0,reverted:0,unfinished:0,nonText:0,synthetic:0}
          const v2="version" in body && body.version===2
          let total=0,partCount=0
          for (const row of rows) {
            if (!Number.isSafeInteger(row.size) || row.size>65536) throw new Error("rating source unavailable")
            const raw=db.select({data:sql<string>`${MessageTable.data}`}).from(MessageTable).where(and(eq(MessageTable.session_id,sessionID),eq(MessageTable.id,row.id))).get()?.data
            if(typeof raw!=="string" || Buffer.byteLength(raw)!==row.size) throw new Error("rating source unavailable")
            const message=JSON.parse(raw) as {role?:string,time?:{completed?:number},summary?:boolean,parentID?:unknown}
            if(message.role!=="user" && message.role!=="assistant") throw new Error("rating source unavailable")
            if(message.summary!==undefined && typeof message.summary!=="boolean") throw new Error("rating source unavailable")
            if(message.role==="assistant" && message.time?.completed!==undefined && !Number.isSafeInteger(message.time.completed)) throw new Error("rating source unavailable")
            if(v2 && message.role==="assistant" && (typeof message.parentID!=="string" || !message.parentID || Buffer.byteLength(message.parentID,"utf8")>256)) throw new Error("rating source unavailable")
            const unfinished=message.role==="assistant" && message.time?.completed===undefined
            const parts=db.select({id:PartTable.id,created:PartTable.time_created,updated:PartTable.time_updated,size:sql<number>`length(CAST(${PartTable.data} AS BLOB))`}).from(PartTable).where(and(eq(PartTable.session_id,sessionID),eq(PartTable.message_id,row.id))).orderBy(PartTable.id).limit(257).all()
            partCount+=parts.length; if(partCount>256) throw new Error("rating source unavailable")
            const messageHash=sha(JSON.stringify(["openclank.rating.message.v1",row.id,sessionID,row.agent,row.created,row.updated,raw]))
            for(const part of parts) {
              if(!Number.isSafeInteger(part.size) || part.size>262144) throw new Error("rating source unavailable")
              total+=part.size; if(total>1048576) throw new Error("rating source unavailable")
              const rawPart=db.select({data:sql<string>`${PartTable.data}`}).from(PartTable).where(and(eq(PartTable.session_id,sessionID),eq(PartTable.message_id,row.id),eq(PartTable.id,part.id))).get()?.data
              if(typeof rawPart!=="string" || Buffer.byteLength(rawPart)!==part.size) throw new Error("rating source unavailable")
              const content=JSON.parse(rawPart) as {type?:string,text?:string,synthetic?:boolean,ignored?:boolean}
              if(typeof content.type!=="string") throw new Error("rating source unavailable")
              if (!new Set(["text","tool","reasoning","file","agent","checkpoint","compaction","json_schema","patch","resource","retry","snapshot","step-finish","step-start","subtask","symbol"]).has(content.type)
                || (content.synthetic!==undefined && typeof content.synthetic!=="boolean") || (content.ignored!==undefined && typeof content.ignored!=="boolean")) throw new Error("rating source unavailable")
              const partHash=sha(JSON.stringify(["openclank.rating.part.v1",part.id,row.id,sessionID,part.created,part.updated,rawPart]))
              let classification="text"
              if(row.agent!=="main") {classification="nonMain";excluded.nonMain++}
              else if(revert && (row.id>revert.messageID! || (row.id===revert.messageID && (!revert.partID || part.id>=revert.partID)))) {classification="reverted";excluded.reverted++}
              else if(unfinished) {classification="unfinished";excluded.unfinished++}
              else if(v2 && (content.type==="step-start" || content.type==="step-finish")) {
                const shape=content.type==="step-start" ? MessageV2.StepStartPart : MessageV2.StepFinishPart
                const candidate={...content,id:part.id,messageID:row.id,sessionID}
                if(message.role!=="assistant" || message.summary===true || !z.strictObject(shape.shape).safeParse(candidate).success) throw new Error("rating source unavailable")
                classification=content.type
              }
              else if(content.type!=="text") {classification="nonText";excluded.nonText++}
              else if(v2 && (content.synthetic===true || content.ignored===true)) {
                const candidate={...content,id:part.id,messageID:row.id,sessionID}
                if(message.role!=="user" || message.summary===true || !z.strictObject(MessageV2.TextPart.shape).safeParse(candidate).success) throw new Error("rating source unavailable")
                classification=content.ignored===true ? "ignored-user" : "context-text"
              }
              else if(message.summary===true || content.synthetic===true || content.ignored===true) {classification="synthetic";excluded.synthetic++}
              else if(typeof content.text!=="string") throw new Error("rating source unavailable")
              observations.push([row.id,part.id,row.created,row.updated,part.created,part.updated,messageHash,partHash,classification])
              if(classification==="text") items.push({messageID:row.id,partID:part.id,role:message.role,...(v2 && message.role==="assistant" ? {parentID:message.parentID} : {}),messageCreated:row.created,messageUpdated:row.updated,partCreated:part.created,partUpdated:part.updated,messageHash,partHash,textSHA256:sha(content.text!),text:content.text})
              if(v2 && (classification==="text" || classification==="context-text" || classification==="ignored-user" || classification==="step-start" || classification==="step-finish")) coverage.push({position:coverage.length,kind:classification,role:message.role,...(message.role==="assistant" ? {parentID:message.parentID} : {}),messageID:row.id,partID:part.id,messageCreated:row.created,messageUpdated:row.updated,partCreated:part.created,partUpdated:part.updated,messageHash,partHash,...(classification==="text" || classification==="context-text" || classification==="ignored-user" ? {textSHA256:sha(content.text!),synthetic:content.synthetic===true,ignored:content.ignored===true} : {})})
            }
            if(v2 && parts.length===0) throw new Error("rating source unavailable")
            if(parts.length===0) { if (row.agent!=="main") excluded.nonMain++; else if(revert && row.id>=revert.messageID!) excluded.reverted++; else if(unfinished) excluded.unfinished++; observations.push([row.id,null,row.created,row.updated,messageHash,"empty"]) }
          }
          const fingerprint=sha(JSON.stringify(["openclank.rating.snapshot.v1",sessionID,sessionRow.revert,observations,excluded]))
          if(v2) return {version:2,scope:"native_visible_text_with_structural_steps",sessionID,snapshotSHA256:fingerprint,items,exclusions:excluded,coverage}
          return {version:1,scope:"native_visible_text",sessionID,snapshotSHA256:fingerprint,items,exclusions:excluded}
        },{behavior:"deferred"})
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"rating source unavailable"})})
        if(runnerBusy() || checkpointBusy() || JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local)) throw new HTTPException(409,{message:"rating source unavailable"})
        return snapshot
      }),
    )
    .post(
      "/:sessionID/git-cleanup-preview",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),directory:cleanupText}).strict()),
      async (c) => jsonRequest("SessionRoutes.gitCleanupPreview",c,function* () {
        const {sessionID}=c.req.valid('param'),body=c.req.valid('json'),host=gitCleanupHostGeneration()
        if(!ManagedProvider.enabled() || process.platform!=='linux' || !host)throw new HTTPException(503,{message:'git cleanup unavailable'})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'git cleanup unavailable'})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if(!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision || session.directory!==Instance.directory || session.parentID)throw new HTTPException(409,{message:'git cleanup unavailable'})
        const witness=yield* (yield* Worktree.Service).previewCleanup(body.directory)
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'git cleanup unavailable'})})
        if(!witness || !cleanupWitness.safeParse(witness).success || gitCleanupHostGeneration()!==host || JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))throw new HTTPException(409,{message:'git cleanup unavailable'})
        return {version:1 as const,state:'ready' as const,gitCleanupControlV1:true as const,witness}
      }),
    )
    .post(
      "/:sessionID/git-cleanup",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),operationID:z.string().regex(/^[A-Za-z0-9_-]{16,80}$/),requestDigest:cleanupDigest,capability:cleanupDigest,cleanupDigest,witness:cleanupWitness}).strict().refine(v=>Buffer.byteLength(JSON.stringify(v),'utf8')<=4194304+131072)),
      async (c) => jsonRequest("SessionRoutes.gitCleanup",c,function* () {
        const {sessionID}=c.req.valid('param'),body=c.req.valid('json'),host=gitCleanupHostGeneration(),invocationID=randomBytes(16).toString('hex')
        const actionSHA256=createHash('sha256').update(canonicalCleanupJSON(['openclank.git-cleanup.native-action.v1',sessionID,body.stableChatID,body.operationID,body.requestDigest,body.cleanupDigest,body.witness]),'utf8').digest('hex')
        if(!ManagedProvider.enabled() || process.platform!=='linux' || !host)throw new HTTPException(503,{message:'git cleanup unavailable'})
        return yield* Effect.uninterruptible(Effect.gen(function* () {
          const local=managedSessionBinding(sessionID)
          const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new Error('binding unavailable')}).pipe(Effect.orElseSucceed(()=>undefined))
          const session=yield* (yield* Session.Service).get(sessionID).pipe(Effect.orElseSucceed(()=>undefined))
          let result:CleanupResult={state:'refused',retirement:'not_issued',removal:'not_issued',child:'not_issued',branch:'unknown'}
          if(local && before && session && !session.parentID && !local.transition && !local.authority && !before.authority && local.engineSessionID===sessionID && local.stableChatID===body.stableChatID && before.stableChatID===body.stableChatID && local.owner===before.owner && local.physicalCwd===Instance.directory && before.canonicalCwd===Instance.directory && local.mappingRevision===before.mappingRevision && local.bindingRevision===before.workspaceRevision && session.directory===Instance.directory && gitCleanupHostGeneration()===host) {
            const proof={version:1 as const,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256}
            const recheck=(phase:'before_close'|'before_retire'|'before_remove')=>Effect.tryPromise({try:async()=>{
              if(gitCleanupHostGeneration()!==host || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))return false
              const response=await ManagedProvider.managedHostCall('_openclank/session/v1/git-cleanup/recheck',{...proof,phase})
              if(response.valid!==true || gitCleanupHostGeneration()!==host)return false
              const after=await ManagedProvider.readManagedSessionBinding(sessionID)
              return gitCleanupHostGeneration()===host && JSON.stringify(after)===JSON.stringify(before) && JSON.stringify(managedSessionBinding(sessionID))===JSON.stringify(local)
            },catch:()=>new Error('git cleanup unavailable')}).pipe(Effect.orElseSucceed(()=>false))
            result=yield* (yield* Worktree.Service).removeReviewed(body.witness,body.cleanupDigest,recheck)
          }
          if(gitCleanupHostGeneration()!==host)throw new HTTPException(503,{message:'git cleanup outcome unavailable'})
          const finished=yield* Effect.tryPromise({try:()=>ManagedProvider.managedHostCall('_openclank/session/v1/git-cleanup/finish',{version:1,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256,result}),catch:()=>new HTTPException(503,{message:'git cleanup outcome unavailable'})})
          if(finished.accepted!==true || gitCleanupHostGeneration()!==host)throw new HTTPException(503,{message:'git cleanup outcome unavailable'})
          return result
        }))
      }),
    )
    .post(
      "/:sessionID/git-commit-preview",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512)}).strict()),
      async (c) => jsonRequest("SessionRoutes.gitCommitPreview",c,function* () {
        const {sessionID}=c.req.valid('param'),body=c.req.valid('json'),host=gitCommitHostGeneration()
        if(!ManagedProvider.enabled() || process.platform!=='linux' || !host)throw new HTTPException(503,{message:'git commit unavailable'})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'git commit unavailable'})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if(!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision || session.directory!==Instance.directory)throw new HTTPException(409,{message:'git commit unavailable'})
        const preview=yield* (yield* Git.Service).previewPhysicalCommit(Instance.directory)
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:'git commit unavailable'})})
        if(!preview || !gitCommitPreviewSchema.safeParse(preview).success || gitCommitHostGeneration()!==host || JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))throw new HTTPException(409,{message:'git commit unavailable'})
        return {version:1 as const,state:'ready' as const,gitCommitControlV1:true as const,preview}
      }),
    )
    .post(
      "/:sessionID/git-commit",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512),operationID:z.string().regex(/^[A-Za-z0-9_-]{16,80}$/),requestDigest:z.string().regex(/^[0-9a-f]{64}$/),capability:z.string().regex(/^[0-9a-f]{64}$/),candidateToken:z.string().regex(/^[0-9a-f]{32}$/),expectedPreview:gitCommitPreviewSchema,action:z.object({message:z.string().min(1).refine(value=>value.endsWith('\n') && !value.includes('\0') && Buffer.byteLength(value)<=16384 && Buffer.from(value).toString('utf8')===value),author:gitCommitIdentitySchema,committer:gitCommitIdentitySchema}).strict()}).strict().refine(value=>Buffer.byteLength(JSON.stringify(value),'utf8')<=4194304+131072)),
      async (c) => jsonRequest("SessionRoutes.gitCommit",c,function* () {
        const {sessionID}=c.req.valid('param'),body=c.req.valid('json'),host=gitCommitHostGeneration(),invocationID=randomBytes(16).toString('hex')
        const e=body.expectedPreview,p=e.physicalIndex,a=body.action
        const actionSHA256=createHash('sha256').update(JSON.stringify(['openclank.git-commit.native-action.v1',sessionID,body.stableChatID,body.operationID,body.requestDigest,body.candidateToken,
          [p.version,p.sha256,p.device,p.inode,p.size,p.objectFormat,p.root.device,p.root.inode,p.git.device,p.git.inode,p.objects.device,p.objects.inode],
          [e.indexEntries.map(v=>[v.path,v.mode,v.oid]),e.headEntries.map(v=>[v.path,v.mode,v.oid]),e.headOID,e.headRef],
          [e.config.device,e.config.inode,e.config.size,e.config.sha256],[e.executable.path,e.executable.device,e.executable.inode,e.executable.size,e.executable.sha256,e.executable.version],
          [a.message,[a.author.name,a.author.email,a.author.seconds,a.author.timezone],[a.committer.name,a.committer.email,a.committer.seconds,a.committer.timezone]]])).digest('hex')
        if(!ManagedProvider.enabled() || process.platform!=='linux' || !host)throw new HTTPException(503,{message:'git commit unavailable'})
        return yield* Effect.uninterruptible(Effect.gen(function* () {
          const local=managedSessionBinding(sessionID)
          const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new Error('binding unavailable')}).pipe(Effect.orElseSucceed(()=>undefined))
          const session=yield* (yield* Session.Service).get(sessionID).pipe(Effect.orElseSucceed(()=>undefined))
          let result:CommitResult={state:'refused',tree:'none',commit:'none',ref:'not_issued',candidateRetained:false,cleanup:'complete'}
          if(local && before && session && !local.transition && !local.authority && !before.authority && local.engineSessionID===sessionID && local.stableChatID===body.stableChatID && before.stableChatID===body.stableChatID && local.owner===before.owner && local.physicalCwd===Instance.directory && before.canonicalCwd===Instance.directory && local.mappingRevision===before.mappingRevision && local.bindingRevision===before.workspaceRevision && session.directory===Instance.directory && gitCommitHostGeneration()===host) {
            const proof={version:1 as const,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256}
            const recheck=async (phase:'before_lock'|'before_tree'|'before_commit'|'before_publish')=>{
              try {
                if(gitCommitHostGeneration()!==host || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))return false
                const response=await ManagedProvider.managedHostCall('_openclank/session/v1/git-commit/recheck',{...proof,phase})
                if(response.valid!==true || gitCommitHostGeneration()!==host)return false
                const after=await ManagedProvider.readManagedSessionBinding(sessionID)
                return gitCommitHostGeneration()===host && JSON.stringify(after)===JSON.stringify(before) && JSON.stringify(managedSessionBinding(sessionID))===JSON.stringify(local)
              } catch {return false}
            }
            result=yield* (yield* Git.Service).applyPhysicalCommit(Instance.directory,e,a,body.candidateToken,recheck)
          }
          if(gitCommitHostGeneration()!==host)throw new HTTPException(503,{message:'git commit outcome unavailable'})
          const finished=yield* Effect.tryPromise({try:()=>ManagedProvider.managedHostCall('_openclank/session/v1/git-commit/finish',{version:1,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256,result}),catch:()=>new HTTPException(503,{message:'git commit outcome unavailable'})})
          if(finished.accepted!==true || gitCommitHostGeneration()!==host)throw new HTTPException(503,{message:'git commit outcome unavailable'})
          return result
        }))
      }),
    )
    .post(
      "/:sessionID/git-index-preview",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({version:z.literal(1),stableChatID:z.string().min(1).max(512)}).strict()),
      async (c) => jsonRequest("SessionRoutes.gitIndexPreview",c,function* () {
        const {sessionID}=c.req.valid("param"),body=c.req.valid("json"),host=gitIndexHostGeneration()
        if(!ManagedProvider.enabled() || process.platform!=="linux" || !host)throw new HTTPException(503,{message:"git index unavailable"})
        const local=managedSessionBinding(sessionID)
        const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"git index unavailable"})})
        const session=yield* (yield* Session.Service).get(sessionID)
        if(!local || local.transition || local.authority || before.authority || local.engineSessionID!==sessionID || local.stableChatID!==body.stableChatID || before.stableChatID!==body.stableChatID || local.owner!==before.owner || local.physicalCwd!==Instance.directory || before.canonicalCwd!==Instance.directory || local.mappingRevision!==before.mappingRevision || local.bindingRevision!==before.workspaceRevision || session.directory!==Instance.directory)
          throw new HTTPException(409,{message:"git index unavailable"})
        const git=yield* Git.Service
        const physicalIndex=yield* git.previewPhysicalIndex(Instance.directory)
        if(!physicalIndex)throw new HTTPException(503,{message:"git index unavailable"})
        const root=yield* git.readWorkbench({cwd:Instance.directory,operation:"root"})
        const format=yield* git.readWorkbench({cwd:Instance.directory,operation:"objectFormat"})
        const rootLines=root.text().split("\n")
        if(root.exitCode!==0 || format.exitCode!==0 || rootLines.length!==3 || rootLines[2]!=="" || rootLines[0]!==Instance.directory || path.resolve(Instance.directory,rootLines[1]!)!==path.join(Instance.directory,".git") || format.text()!==physicalIndex.objectFormat+"\n")
          throw new HTTPException(503,{message:"git index unavailable"})
        const physicalAgain=yield* git.previewPhysicalIndex(Instance.directory)
        if(JSON.stringify(physicalAgain)!==JSON.stringify(physicalIndex))throw new HTTPException(409,{message:"git index unavailable"})
        const after=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new HTTPException(503,{message:"git index unavailable"})})
        if(gitIndexHostGeneration()!==host || JSON.stringify(before)!==JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))throw new HTTPException(409,{message:"git index unavailable"})
        return {version:1 as const,state:'ready' as const,gitIndexControlV1:true as const,physicalIndex}
      }),
    )
    .post(
      "/:sessionID/git-index",
      validator("param", z.object({sessionID:SessionID.zod})),
      validator("json", z.object({
        version:z.literal(1),stableChatID:z.string().min(1).max(512),operationID:z.string().regex(/^[A-Za-z0-9_-]{16,80}$/),
        requestDigest:z.string().regex(/^[0-9a-f]{64}$/),capability:z.string().regex(/^[0-9a-f]{64}$/),candidateToken:z.string().regex(/^[0-9a-f]{32}$/),
        physicalIndex:z.object({version:z.literal(1),sha256:z.string().regex(/^[0-9a-f]{64}$/),device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe(),size:z.number().int().min(32).max(4194304),objectFormat:z.enum(['sha1','sha256']),
          root:z.object({device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe()}).strict(),git:z.object({device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe()}).strict(),objects:z.object({device:z.number().int().nonnegative().safe(),inode:z.number().int().positive().safe()}).strict()}).strict(),
        action:z.discriminatedUnion('operation',[
          z.object({operation:z.literal('stage'),path:z.string().min(1).max(4096),mode:z.union([z.literal(0o100644),z.literal(0o100755)]),bytesBase64:z.string().max(349528).refine(value=>{const bytes=Buffer.from(value,'base64');return bytes.length<=262144 && bytes.toString('base64')===value})}).strict(),
          z.object({operation:z.literal('unstage'),path:z.string().min(1).max(4096),mode:z.union([z.literal(0o100644),z.literal(0o100755)]),headOID:z.union([z.string().regex(/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/),z.null()])}).strict(),
        ]),
      }).strict()),
      async (c) => jsonRequest("SessionRoutes.gitIndex",c,function* () {
        const {sessionID}=c.req.valid('param'),body=c.req.valid('json'),host=gitIndexHostGeneration(),invocationID=randomBytes(16).toString('hex')
        const p=body.physicalIndex,a=body.action
        const actionSHA256=createHash('sha256').update(JSON.stringify(['openclank.git-index.native-action.v1',sessionID,body.stableChatID,body.operationID,body.requestDigest,body.candidateToken,
          [p.version,p.sha256,p.device,p.inode,p.size,p.objectFormat,p.root.device,p.root.inode,p.git.device,p.git.inode,p.objects.device,p.objects.inode],
          [a.operation,a.path,a.mode,a.operation==='stage'?createHash('sha256').update(Buffer.from(a.bytesBase64,'base64')).digest('hex'):null,a.operation==='unstage'?a.headOID:null]])).digest('hex')
        if(!ManagedProvider.enabled() || process.platform!=='linux' || !host)throw new HTTPException(503,{message:'git index unavailable'})
        return yield* Effect.uninterruptible(Effect.gen(function* () {
          const local=managedSessionBinding(sessionID)
          const before=yield* Effect.tryPromise({try:()=>ManagedProvider.readManagedSessionBinding(sessionID),catch:()=>new Error('binding unavailable')}).pipe(Effect.orElseSucceed(()=>undefined))
          const session=yield* (yield* Session.Service).get(sessionID).pipe(Effect.orElseSucceed(()=>undefined))
          let result:IndexResult={state:'refused',object:'none',candidateRetained:false}
          if(local && before && session && !local.transition && !local.authority && !before.authority && local.engineSessionID===sessionID && local.stableChatID===body.stableChatID && before.stableChatID===body.stableChatID && local.owner===before.owner && local.physicalCwd===Instance.directory && before.canonicalCwd===Instance.directory && local.mappingRevision===before.mappingRevision && local.bindingRevision===before.workspaceRevision && session.directory===Instance.directory && gitIndexHostGeneration()===host) {
            const proof={version:1 as const,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256}
            const recheck=async (phase:'before_lock'|'before_object'|'before_publish')=>{
              try {
                if(gitIndexHostGeneration()!==host || JSON.stringify(managedSessionBinding(sessionID))!==JSON.stringify(local))return false
                const response=await ManagedProvider.managedHostCall('_openclank/session/v1/git-index/recheck',{...proof,phase})
                if(response.valid!==true || gitIndexHostGeneration()!==host)return false
                const after=await ManagedProvider.readManagedSessionBinding(sessionID)
                return gitIndexHostGeneration()===host && JSON.stringify(after)===JSON.stringify(before) && JSON.stringify(managedSessionBinding(sessionID))===JSON.stringify(local)
              } catch {return false}
            }
            const action=body.action.operation==='stage' ? {operation:'stage' as const,path:body.action.path,mode:body.action.mode,bytes:Buffer.from(body.action.bytesBase64,'base64')} : body.action
            const git=yield* Git.Service
            result=yield* git.applyPhysicalIndex(Instance.directory,body.physicalIndex,action,body.candidateToken,recheck)
          }
          if(gitIndexHostGeneration()!==host)throw new HTTPException(503,{message:'git index outcome unavailable'})
          const finished=yield* Effect.tryPromise({try:()=>ManagedProvider.managedHostCall('_openclank/session/v1/git-index/finish',{version:1,operationID:body.operationID,requestDigest:body.requestDigest,capability:body.capability,invocationID,actionSHA256,result}),catch:()=>new HTTPException(503,{message:'git index outcome unavailable'})})
          if(finished.accepted!==true || gitIndexHostGeneration()!==host)throw new HTTPException(503,{message:'git index outcome unavailable'})
          return result
        }))
      }),
    )
    .post(
      "/:sessionID/git-review",
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("json", z.object({ stableChatID: z.string().min(1).max(512), operation: z.enum(["root", "head", "index", "tree", "indexEntry", "treeEntry", "blob", "others", "branches", "log", "worktrees"]), path: z.string().min(1).max(4096).optional() }).strict()),
      async (c) => jsonRequest("SessionRoutes.gitReview", c, function* () {
        const { sessionID } = c.req.valid("param"), body = c.req.valid("json")
        if (!ManagedProvider.enabled() || process.platform !== "linux") throw new HTTPException(503, { message: "git review unavailable" })
        const local = managedSessionBinding(sessionID)
        const before = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(sessionID), catch: () => new HTTPException(503, { message: "git review unavailable" }) })
        const session = yield* (yield* Session.Service).get(sessionID)
        if (!local || local.transition || local.authority || before.authority || local.engineSessionID !== sessionID
          || local.stableChatID !== body.stableChatID || before.stableChatID !== body.stableChatID
          || local.owner !== before.owner || local.physicalCwd !== Instance.directory || before.canonicalCwd !== Instance.directory
          || local.mappingRevision !== before.mappingRevision || local.bindingRevision !== before.workspaceRevision
          || session.directory !== Instance.directory)
          throw new HTTPException(409, { message: "git review unavailable" })
        const git = yield* Git.Service
        let indexExists: boolean | null = null
        if (body.operation === "index") {
          const indexPath = yield* git.readWorkbench({ cwd: Instance.directory, operation: "indexPath" })
          if (indexPath.exitCode !== 0 || path.resolve(Instance.directory, indexPath.text().trim()) !== path.join(Instance.directory, ".git", "index"))
            throw new HTTPException(503, { message: "git review unavailable" })
          indexExists = yield* Effect.tryPromise({ try: async () => {
            try { const info = await lstat(path.join(Instance.directory, ".git", "index")); if (!info.isFile()) throw new Error("unsupported"); return true }
            catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return false; throw error }
          }, catch: () => new HTTPException(503, { message: "git review unavailable" }) })
        }
        const result = yield* git.readWorkbench({ cwd: Instance.directory, operation: body.operation, path: body.path })
        const after = yield* Effect.tryPromise({ try: () => ManagedProvider.readManagedSessionBinding(sessionID), catch: () => new HTTPException(503, { message: "git review unavailable" }) })
        if (JSON.stringify(before) !== JSON.stringify(after) || JSON.stringify(managedSessionBinding(sessionID)) !== JSON.stringify(local)) throw new HTTPException(409, { message: "git review unavailable" })
        const bytes = result.exitCode === 0 ? result.stdout : Buffer.alloc(0)
        const prefix = ["index", "tree", "others"].includes(body.operation) ? Git.inventoryPrefix(bytes) : {data:bytes, truncated:false}
        return { state: result.exitCode === 0 ? "ready" as const : "unavailable" as const,
          data: prefix.data.toString("base64"), digest: createHash("sha256").update(bytes).digest("hex"),
          truncated: prefix.truncated, indexExists }
      }),
    )
    .post(
      "/:sessionID/context-selection",
      validator("param", z.object({ sessionID: SessionID.zod })),
      validator("json", z.object({ stableChatID: z.string().min(1).max(512), action: z.enum(["omit", "trim", "restore"]), partIDs: z.array(z.string().min(1).max(512)).min(1).max(16),
        expected: z.object({ version: z.literal(1), sessionID: z.string(), executionRevision: z.number().int().nonnegative(), watermark: z.string().max(1024),
          action: z.enum(["omit", "trim", "restore"]), parts: z.array(z.object({ id: z.string(), messageID: z.string(), digest: z.string().regex(/^[a-f0-9]{64}$/), type: z.string() }).strict()).min(1).max(16) }).strict().optional() }).strict()),
      async (c) => jsonRequest("SessionRoutes.contextSelection", c, function* () {
        const { sessionID } = c.req.valid("param"), body = c.req.valid("json")
        if (!ManagedProvider.enabled()) throw new HTTPException(503, {message:"managed context mutation authority unavailable"})
        yield* (yield* Session.Service).get(sessionID)
        const state = yield* SessionRunState.Service, checkpoint = yield* SessionCheckpoint.Service
        if (!state.busyGuard || !checkpoint.busyGuard) throw new HTTPException(503, {message:"context guard unavailable"})
        const runnerBusy = yield* state.busyGuard(sessionID, "main"), checkpointBusy = checkpoint.busyGuard(sessionID)
        try { return selectContext({ sessionID, stableChatID:body.stableChatID, action:body.action, partIDs:body.partIDs, runnerBusy, checkpointBusy,
          expected:body.expected as SelectionPreview | undefined }) }
        catch { throw new HTTPException(409, {message:"context selection unavailable or stale"}) }
      }),
    )
    .get(
      "/:sessionID/visible-peer/:childID",
      describeRoute({ summary: "Verify one visible direct peer", operationId: "session.visiblePeer",
        responses: { 200: { description: "Verified direct peer" }, ...errors(400, 404) } }),
      validator("param", z.object({ sessionID: SessionID.zod, childID: SessionID.zod })),
      async (c) => jsonRequest("SessionRoutes.visiblePeer", c, function* () {
        const { sessionID, childID } = c.req.valid("param")
        const session = yield* Session.Service
        yield* session.get(sessionID)
        const child = yield* session.get(childID)
        const registry = yield* ActorRegistry.Service
        if (!registry.isPeerSession) return yield* Effect.fail(new HTTPException(503, { message: "peer verification is unavailable" }))
        if (child.parentID !== sessionID || !(yield* registry.isPeerSession(childID)))
          return yield* Effect.fail(new HTTPException(404, { message: "visible direct peer not found" }))
        return { id: child.id, parentID: child.parentID, directory: child.directory }
      }),
    )
    .get(
      "/:sessionID/task",
      describeRoute({
        summary: "List session tasks",
        description: "List tasks registered for a session (the work-item registry).",
        operationId: "session.task",
        responses: {
          200: {
            description: "Task list",
            content: {
              "application/json": {
                schema: resolver(Task.array()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        const tasks = await runRequest(
          "SessionRoutes.task",
          c,
          Effect.gen(function* () {
            const reg = yield* TaskRegistry.Service
            const session = yield* Session.Service
            yield* session.get(sessionID)
            return yield* reg.list({ session_id: sessionID, include_terminal: true })
          }),
        )
        return c.json(tasks)
      },
    )
    .post(
      "/",
      describeRoute({
        summary: "Create session",
        description: "Create a new OpenCode session for interacting with AI assistants and managing conversations.",
        operationId: "session.create",
        responses: {
          ...errors(400),
          200: {
            description: "Successfully created session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
        },
      }),
      validator("json", Session.CreateInput),
      async (c) =>
        jsonRequest("SessionRoutes.create", c, function* () {
          const body = c.req.valid("json") ?? {}
          const svc = yield* SessionShare.Service
          return yield* svc.create(body)
        }),
    )
    .delete(
      "/:sessionID/tool-output/:outputID",
      describeRoute({
        summary: "Delete retained tool output",
        description: "Delete one retained tool-output spill owned by this session.",
        operationId: "session.deleteToolOutput",
        responses: {
          200: {
            description: "Whether the retained output existed in this session scope",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          outputID: ToolID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.deleteToolOutput", c, function* () {
          const params = c.req.valid("param")
          const sessions = yield* Session.Service
          yield* sessions.get(params.sessionID)
          const truncation = yield* Truncate.Service
          return yield* truncation.remove(params.outputID, {
            owner: process.env.OPEN_CLANK_OWNER ?? "",
            sessionID: params.sessionID,
          })
        }),
    )
    .delete(
      "/:sessionID",
      describeRoute({
        summary: "Delete session",
        description: "Delete a session and permanently remove all associated data, including messages and history.",
        operationId: "session.delete",
        responses: {
          200: {
            description: "Successfully deleted session",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: Session.RemoveInput,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.delete", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const svc = yield* Session.Service
          const truncation = yield* Truncate.Service
          const pending = [sessionID]
          const ownedSessions: SessionID[] = []
          while (pending.length) {
            const current = pending.pop()!
            ownedSessions.push(current)
            const children = yield* svc.children(current)
            pending.push(...children.map((child) => child.id))
          }
          for (const ownedSession of ownedSessions) {
            yield* truncation.removeSession(ownedSession, process.env.OPEN_CLANK_OWNER ?? "")
          }
          yield* svc.remove(sessionID)
          return true
        }),
    )
    .patch(
      "/:sessionID",
      describeRoute({
        summary: "Update session",
        description: "Update properties of an existing session, such as title or other metadata.",
        operationId: "session.update",
        responses: {
          200: {
            description: "Successfully updated session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          409: { description: "Title changed by another writer", content: { "application/json": { schema: resolver(Session.TitleConflict) } } },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator(
        "json",
        z.object({
          title: Session.SetTitleInput.shape.title.optional(),
          expectedRevision: Session.Info.shape.titleRevision.optional(),
          permission: Permission.Ruleset.zod.optional(),
          time: z
            .object({
              archived: z.number().optional(),
            })
            .optional(),
        }).strict().refine((input) => input.title === undefined || input.expectedRevision !== undefined, { message: "expectedRevision is required when renaming", path: ["expectedRevision"] }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.update", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const updates = c.req.valid("json")
          const session = yield* Session.Service
          const current = yield* session.get(sessionID)

          if (updates.title !== undefined) {
            yield* session.setTitle({ sessionID, title: updates.title, expectedRevision: updates.expectedRevision! })
          }
          if (updates.permission !== undefined) {
            yield* session.setPermission({
              sessionID,
              permission: Permission.merge(current.permission ?? [], updates.permission),
            })
          }
          if (updates.time?.archived !== undefined) {
            yield* session.setArchived({ sessionID, time: updates.time.archived })
          }

          return yield* session.get(sessionID)
        }),
    )
    // TODO(v2): remove this dedicated route and rely on the normal `/init` command flow.
    .post(
      "/:sessionID/init",
      describeRoute({
        summary: "Initialize session",
        description:
          "Analyze the current application and create an AGENTS.md file with project-specific agent configurations.",
        operationId: "session.init",
        responses: {
          200: {
            description: "200",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator(
        "json",
        z.object({
          modelID: ModelID.zod,
          providerID: ProviderID.zod,
          messageID: MessageID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.init", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const svc = yield* SessionPrompt.Service
          yield* svc.command({
            sessionID,
            messageID: body.messageID,
            model: body.providerID + "/" + body.modelID,
            command: Command.Default.INIT,
            arguments: "",
          })
          return true
        }),
    )
    .post(
      "/:sessionID/fork",
      describeRoute({
        summary: "Fork session",
        description: "Create a new session by forking an existing session at a specific message point.",
        operationId: "session.fork",
        responses: {
          200: {
            description: "200",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: Session.ForkInput.shape.sessionID,
        }),
      ),
      validator("json", Session.ForkInput.omit({ sessionID: true })),
      async (c) =>
        jsonRequest("SessionRoutes.fork", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const svc = yield* Session.Service
          return yield* svc.fork({ ...body, sessionID })
        }),
    )
    .post(
      "/:sessionID/abort",
      describeRoute({
        summary: "Abort session",
        description: "Abort an active session and stop any ongoing AI processing or command execution.",
        operationId: "session.abort",
        responses: {
          200: {
            description: "Aborted session",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.abort", c, function* () {
          const svc = yield* SessionPrompt.Service
          yield* svc.cancel(c.req.valid("param").sessionID)
          return true
        }),
    )
    .post(
      "/:sessionID/share",
      describeRoute({
        summary: "Share session",
        description: "Create a shareable link for a session, allowing others to view the conversation.",
        operationId: "session.share",
        responses: {
          200: {
            description: "Successfully shared session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.share", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const share = yield* SessionShare.Service
          const session = yield* Session.Service
          yield* share.share(sessionID)
          return yield* session.get(sessionID)
        }),
    )
    .get(
      "/:sessionID/diff",
      describeRoute({
        summary: "Get message diff",
        description: "Get the file changes (diff) that resulted from a specific user message in the session.",
        operationId: "session.diff",
        responses: {
          200: {
            description: "Successfully retrieved diff",
            content: {
              "application/json": {
                schema: resolver(Snapshot.FileDiff.array()),
              },
            },
          },
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionSummary.DiffInput.shape.sessionID,
        }),
      ),
      validator(
        "query",
        z.object({
          messageID: SessionSummary.DiffInput.shape.messageID,
        }),
      ),
      async (c) => {
        const result = await runRequest("SessionRoutes.diff", c, Effect.gen(function* () {
          const query = c.req.valid("query")
          const params = c.req.valid("param")
          const summary = yield* SessionSummary.Service
          const input = { sessionID: params.sessionID, messageID: query.messageID }
          return yield* summary.diffResult(input)
        }))
        return "coverage" in result ? c.json(result.coverage, 409) : c.json(result.diffs, 200)
      },
    )
    .delete(
      "/:sessionID/share",
      describeRoute({
        summary: "Unshare session",
        description: "Remove the shareable link for a session, making it private again.",
        operationId: "session.unshare",
        responses: {
          200: {
            description: "Successfully unshared session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.unshare", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const share = yield* SessionShare.Service
          const session = yield* Session.Service
          yield* share.unshare(sessionID)
          return yield* session.get(sessionID)
        }),
    )
    .post(
      "/:sessionID/summarize",
      describeRoute({
        summary: "Summarize session",
        description: "Generate a concise summary of the session using AI compaction to preserve key information.",
        operationId: "session.summarize",
        responses: {
          200: {
            description: "Summarized session",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator(
        "json",
        z.object({
          providerID: ProviderID.zod,
          modelID: ModelID.zod,
          auto: z.boolean().optional().default(false),
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.summarize", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const session = yield* Session.Service
          const revert = yield* SessionRevert.Service
          const compact = yield* SessionCompaction.Service
          const prompt = yield* SessionPrompt.Service
          const agent = yield* Agent.Service

          yield* revert.cleanup(yield* session.get(sessionID))
          const currentAgent = yield* resolveCurrentAgent(sessionID, yield* agent.defaultAgent())

          yield* compact.create({
            sessionID,
            agent: currentAgent,
            model: {
              providerID: body.providerID,
              modelID: body.modelID,
            },
            auto: body.auto,
            agentID: "main",
          })
          yield* prompt.loop({ sessionID })
          return true
        }),
    )
    .post(
      "/:sessionID/ask",
      describeRoute({
        summary: "Ask session a side question",
        description:
          "Ask the session a one-shot, read-only side question over a frozen snapshot of its history and return the answer text. Does NOT inject a message into the conversation or disturb the session's turn.",
        operationId: "session.ask",
        responses: {
          200: {
            description: "Side question answer",
            content: {
              "application/json": {
                schema: resolver(z.object({ answer: z.string() })),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator(
        "json",
        z.object({
          question: z.string().min(1),
          providerID: ProviderID.zod.optional(),
          modelID: ModelID.zod.optional(),
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.ask", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const sessions = yield* Session.Service
          const provider = yield* Provider.Service
          const actorRegistry = yield* ActorRegistry.Service
          // Resolve the Actor through the late-bound spawnRef rather than a layer
          // dependency, mirroring tool/session.ts to avoid an Actor → SessionPrompt
          // → ToolRegistry → tool/session → Actor layer cycle.
          const actor = spawnRef.current
          if (!actor)
            return yield* Effect.fail(
              new Error("Actor service unavailable — Actor.defaultLayer must be running to ask a side question"),
            )
          const selectedModel =
            body.providerID && body.modelID ? { providerID: body.providerID, modelID: body.modelID } : undefined
          const answer = yield* forkQuery({ sessions, actorRegistry, provider, actor }, sessionID, body.question, selectedModel)
          return { answer }
        }),
    )
    .get(
      "/:sessionID/message",
      describeRoute({
        summary: "Get session messages",
        description: "Retrieve all messages in a session, including user prompts and AI responses.",
        operationId: "session.messages",
        responses: {
          200: {
            description: "List of messages",
            content: {
              "application/json": {
                schema: resolver(MessageV2.WithParts.array()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator(
        "query",
        z
          .object({
            limit: z.coerce
              .number()
              .int()
              .min(0)
              .max(1000)
              .optional()
              .meta({ description: "Maximum number of messages to return (max 1000)" }),
            before: z
              .string()
              .optional()
              .meta({ description: "Opaque cursor for loading older messages" })
              .refine(
                (value) => {
                  if (!value) return true
                  try {
                    MessageV2.cursor.decode(value)
                    return true
                  } catch {
                    return false
                  }
                },
                { message: "Invalid cursor" },
              ),
            agent_id: z.string().optional().meta({
              description:
                "Filter by message slice. Omitted = main-agent slice only (default). Pass a subagent's actor id to fetch its slice. Pass `*` to return every message regardless of slice.",
            }),
          })
          .refine((value) => !value.before || value.limit !== undefined, {
            message: "before requires limit",
            path: ["before"],
          }),
      ),
      async (c) => {
        const query = c.req.valid("query")
        const sessionID = c.req.valid("param").sessionID
        // Default to main-agent slice when omitted; `"*"` and explicit
        // actorIDs forward verbatim to Session.messages, which now owns the
        // slice contract.
        const agentID = query.agent_id ?? "main"

        if (query.limit === undefined || query.limit === 0) {
          const messages = await runRequest(
            "SessionRoutes.messages",
            c,
            Effect.gen(function* () {
              const session = yield* Session.Service
              yield* session.get(sessionID)
              return yield* session.messages({ sessionID, agentID, limit: 1000 })
            }),
          )
          return c.json(messages)
        }

        const page = await MessageV2.page({
          sessionID,
          limit: query.limit,
          before: query.before,
          agentID,
        })
        if (page.cursor) {
          const url = new URL(c.req.url)
          url.searchParams.set("limit", query.limit.toString())
          url.searchParams.set("before", page.cursor)
          c.header("Access-Control-Expose-Headers", "Link, X-Next-Cursor")
          c.header("Link", `<${url.toString()}>; rel="next"`)
          c.header("X-Next-Cursor", page.cursor)
        }
        return c.json(page.items)
      },
    )
    .get(
      "/:sessionID/message/:messageID",
      describeRoute({
        summary: "Get message",
        description: "Retrieve a specific message from a session by its message ID.",
        operationId: "session.message",
        responses: {
          200: {
            description: "Message",
            content: {
              "application/json": {
                schema: resolver(
                  z.object({
                    info: MessageV2.Info,
                    parts: MessageV2.Part.array(),
                  }),
                ),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          messageID: MessageID.zod,
        }),
      ),
      async (c) => {
        const params = c.req.valid("param")
        const message = await MessageV2.get({
          sessionID: params.sessionID,
          messageID: params.messageID,
        })
        return c.json(message)
      },
    )
    .delete(
      "/:sessionID/message/:messageID",
      describeRoute({
        summary: "Delete message",
        description:
          "Permanently delete a specific message (and all of its parts) from a session. This does not revert any file changes that may have been made while processing the message.",
        operationId: "session.deleteMessage",
        responses: {
          200: {
            description: "Successfully deleted message",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          messageID: MessageID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.deleteMessage", c, function* () {
          const params = c.req.valid("param")
          const state = yield* SessionRunState.Service
          const session = yield* Session.Service
          yield* state.assertNotBusy(params.sessionID)
          yield* session.removeMessage({
            sessionID: params.sessionID,
            messageID: params.messageID,
          })
          return true
        }),
    )
    .delete(
      "/:sessionID/message/:messageID/part/:partID",
      describeRoute({
        description: "Delete a part from a message",
        operationId: "part.delete",
        responses: {
          200: {
            description: "Successfully deleted part",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          messageID: MessageID.zod,
          partID: PartID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.deletePart", c, function* () {
          const params = c.req.valid("param")
          const svc = yield* Session.Service
          yield* svc.removePart({
            sessionID: params.sessionID,
            messageID: params.messageID,
            partID: params.partID,
          })
          return true
        }),
    )
    .patch(
      "/:sessionID/message/:messageID/part/:partID",
      describeRoute({
        description: "Update a part in a message",
        operationId: "part.update",
        responses: {
          200: {
            description: "Successfully updated part",
            content: {
              "application/json": {
                schema: resolver(MessageV2.Part),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          messageID: MessageID.zod,
          partID: PartID.zod,
        }),
      ),
      validator("json", MessageV2.Part),
      async (c) => {
        const params = c.req.valid("param")
        const body = c.req.valid("json")
        if (body.id !== params.partID || body.messageID !== params.messageID || body.sessionID !== params.sessionID) {
          throw new Error(
            `Part mismatch: body.id='${body.id}' vs partID='${params.partID}', body.messageID='${body.messageID}' vs messageID='${params.messageID}', body.sessionID='${body.sessionID}' vs sessionID='${params.sessionID}'`,
          )
        }
        return jsonRequest("SessionRoutes.updatePart", c, function* () {
          const svc = yield* Session.Service
          return yield* svc.updatePart(body)
        })
      },
    )
    .post(
      "/:sessionID/message",
      describeRoute({
        summary: "Send message",
        description: "Create and send a new message to a session, streaming the AI response.",
        operationId: "session.prompt",
        responses: {
          200: {
            description: "Created message",
            content: {
              "application/json": {
                schema: resolver(
                  z.object({
                    info: MessageV2.Assistant,
                    parts: MessageV2.Part.array(),
                  }),
                ),
              },
            },
          },
          ...errors(400, 404, 409),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator("json", SessionPrompt.PromptInput.omit({ sessionID: true })),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID

        // Pre-check: bail with 409 Conflict if the session's main runner is busy
        // with another request. Without this guard, ensureRunning silently queues
        // the new work behind the existing runner (which may be a zombie from a
        // SIGKILL'd previous client), causing the new client to hang for the
        // duration of the old runner's retry envelope. Caller's recovery path:
        // POST /:sessionID/abort to free the runner, then retry this POST.
        await runRequest(
          "SessionRoutes.prompt.assertNotBusy",
          c,
          SessionRunState.Service.use((svc) => svc.assertNotBusy(sessionID)),
        )

        c.status(200)
        c.header("Content-Type", "application/json")
        return stream(c, async (stream) => {
          const body = c.req.valid("json")
          // If the HTTP client gives up (TUI exits, driver kills its `mimo run`
          // client on its own per-turn timeout, network drop), we have to drive
          // the server-side runner to Idle ourselves. Otherwise the prompt
          // fiber keeps running with no consumer, and any next POST attaches
          // to the same dead Deferred via SessionRunState.ensureRunning's
          // `Running` branch — every subsequent turn then hangs waiting on a
          // result that will never arrive. SessionPrompt.cancel interrupts
          // the fiber, which lets the runner transition Running -> Idle
          // through Runner.cancel, freeing the next POST to start a fresh run.
          const signal = c.req.raw.signal
          const onClientDisconnect = () => {
            void runRequest(
              "SessionRoutes.prompt.disconnect",
              c,
              SessionPrompt.Service.use((svc) => svc.cancel(sessionID)),
            ).catch(() => {})
          }
          if (signal.aborted) {
            onClientDisconnect()
            return
          }
          signal.addEventListener("abort", onClientDisconnect, { once: true })
          // Keep the response alive while the turn is in flight. A turn can sit
          // silent for a long time — most notably while the `question` tool
          // blocks on an un-timed Deferred waiting for a human reply (the Bun
          // server itself never times out: adapter.bun.ts idleTimeout:0). A
          // client with its own request timeout (e.g. the external `mimo run`
          // driver's per-turn budget) would otherwise see a dead connection and
          // abort with "error sending request for url". Periodic whitespace
          // resets the client's idle timer; whitespace is JSON-insignificant,
          // so the trailing JSON.stringify(msg) still parses as the whole body
          // (clients JSON.parse the full body, which tolerates leading
          // whitespace). Mirrors the 10s SSE heartbeat in event.ts/global.ts.
          const heartbeat = setInterval(() => {
            void stream.write(" ")
          }, promptHeartbeatIntervalMs())
          try {
            const msg = await runRequest(
              "SessionRoutes.prompt",
              c,
              SessionPrompt.Service.use((svc) => svc.prompt({ ...body, sessionID })),
            )
            // Safety invariant: no await/yield between this write and the
            // clearInterval below (reached synchronously via finally) — else the
            // interval could fire and append a stray space AFTER the JSON,
            // breaking the "JSON is the whole body" contract. Leading spaces are
            // JSON-insignificant; a trailing one would not be.
            void stream.write(JSON.stringify(msg))
          } finally {
            clearInterval(heartbeat)
            signal.removeEventListener("abort", onClientDisconnect)
          }
        })
      },
    )
    .post(
      "/:sessionID/prompt_async",
      RateLimitMiddleware({ windowMs: 60_000, max: 20, keyPrefix: "prompt_async" }),
      describeRoute({
        summary: "Send async message",
        description:
          "Create and send a new message to a session asynchronously, starting the session if needed and returning immediately.",
        operationId: "session.prompt_async",
        responses: {
          204: {
            description: "Prompt accepted",
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator("json", SessionPrompt.PromptInput.omit({ sessionID: true })),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        const body = c.req.valid("json")
        void runRequest(
          "SessionRoutes.prompt_async",
          c,
          SessionPrompt.Service.use((svc) => svc.prompt({ ...body, sessionID })),
        ).catch((err) => {
          log.error("prompt_async failed", { sessionID, error: err })
          void Bus.publish(Session.Event.Error, {
            sessionID,
            error: new NamedError.Unknown({ message: err instanceof Error ? err.message : String(err) }).toObject(),
          })
        })

        return c.body(null, 204)
      },
    )
    .post(
      "/:sessionID/command",
      describeRoute({
        summary: "Send command",
        description: "Send a new command to a session for execution by the AI assistant.",
        operationId: "session.command",
        responses: {
          200: {
            description: "Created message",
            content: {
              "application/json": {
                schema: resolver(
                  z.object({
                    info: MessageV2.Assistant,
                    parts: MessageV2.Part.array(),
                  }),
                ),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator("json", SessionPrompt.CommandInput.omit({ sessionID: true })),
      async (c) =>
        jsonRequest("SessionRoutes.command", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const svc = yield* SessionPrompt.Service
          return yield* svc.command({ ...body, sessionID })
        }),
    )
    .post(
      "/:sessionID/predict",
      describeRoute({
        summary: "Predict next prompt",
        description:
          "Predict the user's most likely next prompt based on the latest user message and the assistant's result. Returns an empty string when disabled or unavailable.",
        operationId: "session.predict",
        responses: {
          200: {
            description: "Predicted next prompt",
            content: {
              "application/json": {
                schema: resolver(
                  z.object({
                    prediction: z.string(),
                  }),
                ),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.predict", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const svc = yield* SessionPrompt.Service
          const prediction = yield* svc.predict({ sessionID })
          return { prediction }
        }),
    )
    .post(
      "/:sessionID/shell",
      RateLimitMiddleware({ windowMs: 60_000, max: 20, keyPrefix: "shell" }),
      describeRoute({
        summary: "Run shell command",
        description: "Execute a shell command within the session context and return the AI's response.",
        operationId: "session.shell",
        responses: {
          200: {
            description: "Created message",
            content: {
              "application/json": {
                schema: resolver(MessageV2.WithParts),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator("json", SessionPrompt.ShellInput.omit({ sessionID: true })),
      async (c) =>
        jsonRequest("SessionRoutes.shell", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const body = c.req.valid("json")
          const svc = yield* SessionPrompt.Service
          return yield* svc.shell({ ...body, sessionID })
        }),
    )
    .post(
      "/:sessionID/revert",
      describeRoute({
        summary: "Revert message",
        description: "Revert a specific message in a session, undoing its effects and restoring the previous state.",
        operationId: "session.revert",
        responses: {
          200: {
            description: "Updated session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      validator("json", SessionRevert.RevertInput.omit({ sessionID: true })),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        log.info("revert", c.req.valid("json"))
        return jsonRequest("SessionRoutes.revert", c, function* () {
          const svc = yield* SessionRevert.Service
          return yield* svc.revert({
            sessionID,
            ...c.req.valid("json"),
          })
        })
      },
    )
    .post(
      "/:sessionID/unrevert",
      describeRoute({
        summary: "Restore reverted messages",
        description: "Restore all previously reverted messages in a session.",
        operationId: "session.unrevert",
        responses: {
          200: {
            description: "Updated session",
            content: {
              "application/json": {
                schema: resolver(Session.Info),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) =>
        jsonRequest("SessionRoutes.unrevert", c, function* () {
          const sessionID = c.req.valid("param").sessionID
          const svc = yield* SessionRevert.Service
          return yield* svc.unrevert({ sessionID })
        }),
    )
    .post(
      "/:sessionID/permissions/:permissionID",
      describeRoute({
        summary: "Respond to permission",
        deprecated: true,
        description: "Approve or deny a permission request from the AI assistant.",
        operationId: "permission.respond",
        responses: {
          200: {
            description: "Permission processed successfully",
            content: {
              "application/json": {
                schema: resolver(z.boolean()),
              },
            },
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
          permissionID: PermissionID.zod,
        }),
      ),
      validator("json", z.object({ response: Permission.Reply.zod })),
      async (c) =>
        jsonRequest("SessionRoutes.permissionRespond", c, function* () {
          const params = c.req.valid("param")
          const svc = yield* Permission.Service
          yield* svc.reply({
            requestID: params.permissionID,
            reply: c.req.valid("json").response,
          })
          return true
        }),
    )
    .post(
      "/:sessionID/actors/:actorID/display-name",
      validator("param", z.object({sessionID:SessionID.zod,actorID:z.string().min(1).max(256)})),
      validator("json", z.object({accountID:z.string().min(1).max(256),chatID:z.string().min(1).max(512),
        workspaceID:z.string().min(1).max(512),executionRevision:z.number().int().nonnegative().safe(),
        displayRevision:z.number().int().nonnegative().safe(),label:z.string().min(1).max(200)}).strict()),
      async (c) => {
        const {sessionID,actorID}=c.req.valid("param"), input=c.req.valid("json")
        const applied=await runRequest("SessionRoutes.actorDisplayName",c,Effect.gen(function*(){
          const registry=yield* ActorRegistry.Service
          const sessions=yield* Session.Service
          yield* sessions.get(sessionID)
          if (!registry.renameDisplay || !registry.getDisplayOwner || !(yield* registry.getDisplayOwner(sessionID,actorID))) return "owner_unknown" as const
          return (yield* registry.renameDisplay({sessionID,actorID,...input})) ? "applied" as const : "conflict" as const
        }))
        return applied === "applied" ? c.json({state:applied}) : c.json({state:applied},409)
      },
    )
    .get(
      "/:sessionID/actors",
      describeRoute({
        summary: "List session actors",
        description: "List actors registered for a session.",
        operationId: "session.actors",
        responses: {
          200: {
            description: "Actor list",
          },
          ...errors(400, 404),
        },
      }),
      validator(
        "param",
        z.object({
          sessionID: SessionID.zod,
        }),
      ),
      async (c) => {
        const sessionID = c.req.valid("param").sessionID
        const actors = await runRequest(
          "SessionRoutes.actors",
          c,
          Effect.gen(function* () {
            const reg = yield* ActorRegistry.Service
            const session = yield* Session.Service
            yield* session.get(sessionID)
            return yield* reg.listBySession(sessionID)
          }),
        )
        return c.json(actors)
      },
    ),
)
