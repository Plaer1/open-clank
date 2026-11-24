import { startSourceOutput, appendSourceOutput, endSourceOutput, finishSourceOutputStep, noteSourceOutputFinish, finishSourceOutputStream, invalidateSourceOutput, type SourceAttemptHandle } from "./closed-source-provenance"
import { contextFrameWriter, nativeTextDeliveryWriter, nativeMembershipDeliveryWriter } from "./context-inspection"
import { transferGuardedToolDefinition } from "./tool-definition-provenance"
import { createStepRecoveryRecorder } from "./step-recovery-recorder"
import { createStepRecoveryCapability, publishStepRecovery } from "./step-recovery-owner"
import { takeSdkSuccessStep } from "@/provider/logging-transport"
import { EffectBridge } from "@/effect"
import type { AppFileSystem } from "@mimo-ai/shared/filesystem"
import type { CaptureMembership } from "./capture-membership"
import { gcfCandidate } from "./gcf-source-proof"
import { captureToolView, selectedToolViewProfile } from "./tool-view-source"
import { readTerminalAnsi } from "./tool-view-terminal-ansi"
import { readBashView } from "./tool-view-bash-proof"
import { readWebfetchHtml, readWebfetchMarkdown } from "../tool/webfetch-view"
import { readGrepRows } from "./tool-view-grep"
import { readReadViewProfile } from "./tool-view-read"
import type { ActorExecutionIdentity } from "@/actor/turn"
import { captureToolTiming, isAdmittedToolTimingTurn, type ToolTimingCapture } from "./tool-timing"
import { ManagedProvider } from "@/acp/managed-provider"
import { deferredFilesMutate, deferredOrdinaryFilesMutate } from "@/mcp/files-mutate"
import { emitMalformedDiagnostic, hasMalformedResponse, hasObservedMalformedResponse } from "@/provider/logging-transport"
import { Cause, Deferred, Effect, Exit, Fiber, Layer, Context, Scope } from "effect"
import * as Stream from "effect/Stream"
import { Agent, resolveStoredAgent } from "@/agent/agent"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { Bus } from "@/bus"
import { Metrics } from "@/metrics"
import { Config } from "@/config"
import { Permission } from "@/permission"
import { Plugin } from "@/plugin"
import * as Session from "./session"
import { classifyAssistantStep, isTerminalFinish } from "./classify"
import { LLM } from "./llm"
import { MessageV2 } from "./message-v2"
import { isOverflow } from "./overflow"
import { PartID } from "./schema"
import type { SessionID } from "./schema"
import { SessionRetry } from "./retry"
import { SessionStatus } from "./status"
import { SessionSummary } from "./summary"
import { ProviderError } from "@/provider"
import type { Provider } from "@/provider"
import { Question } from "@/question"
import { errorMessage } from "@/util/error"
import { ToolGate, FAIL_CASCADE_MESSAGE } from "@/tool/gate"
import { ResultRef } from "@/tool/settlement"
import { isRecoverableError } from "@/tool/recoverable"
import { getToolResultAttachments, getToolResultMetadata } from "@/tool/result-error"
import { Log } from "@/util"
import { isRecord } from "@/util/record"
import { createTextNgramMonitor, type TextNgramMonitor } from "./prompt/text-ngram-detection"
import { Flag } from "@/flag/flag"
import { MCP_TOOL_USE_ID } from "@/tool/mcp-tool-search"
import { monitor as tryBestMonitor, type TryBestIncident } from "./try-best-detector"

const DOOM_LOOP_THRESHOLD = 3
const log = Log.create({ service: "session.processor" })

// A dispatched MCP call reports under the MCP tool it ran, matching the
// success metric published by the MCP execution pipeline.
function metricToolName(toolName: string, input: unknown) {
  if (toolName !== MCP_TOOL_USE_ID || !isRecord(input) || typeof input.name !== "string") return toolName
  return input.name
}

function isToolExecutionResult(output: unknown): output is {
  title: string
  metadata: Record<string, any>
  output: string
  attachments?: MessageV2.FilePart[]
} {
  return (
    isRecord(output) &&
    typeof output.title === "string" &&
    isRecord(output.metadata) &&
    typeof output.output === "string"
  )
}

function displayToolOutput(output: unknown) {
  if (typeof output === "string") return output
  return JSON.stringify(output, null, 2) ?? String(output)
}

function jsonToolOutput(output: unknown): MessageV2.ToolStateCompleted["providerOutput"] {
  const serialized = JSON.stringify(output)
  return serialized === undefined ? null : JSON.parse(serialized)
}

function describeTryBest(incident: TryBestIncident) {
  if (incident.reason === "edit_repeat") {
    return `A near-identical edit to ${incident.evidence.path ?? "the same file"} repeated ${incident.evidence.count} times.`
  }
  if (incident.reason === "bash_retry") {
    return `The same failing command was retried ${incident.evidence.count} times without an intervening successful edit.`
  }
  return `${incident.evidence.count} consecutive ${incident.evidence.action ?? "same-kind"} actions made no observable progress.`
}

export type Result = "overflow" | "stop" | "continue" | "text-repeat" | "superseded"

export type Event = LLM.Event

/**
 * Codex-style auto-resume gate: after tools have fully completed, a retryable
 * transport failure should continue the outer step (tool results stay in
 * history) instead of stamping a terminal error. Replaying this stream is
 * unsafe; the next sampling call is not.
 */
export function shouldAutoResumeAfterTools(input: {
  retrySafe: boolean
  decision: { retryable: boolean }
  toolParts: readonly { state: { status: string } }[]
  /** User/agent abort — never reopen the step. */
  aborted?: boolean
  /** Current assistant finish reason (provider). Terminal reasons must not be rewritten. */
  finish?: string | undefined
  /** Provider raw finish reason. Raw-less `other` (stream incomplete) is not terminal. */
  rawFinish?: string | undefined
  /** Non-empty user-visible final text already on this assistant message. */
  hasFinalText?: boolean
}): boolean {
  if (input.aborted) return false
  if (input.retrySafe) return false
  if (!input.decision.retryable) return false
  // Already-delivered answer: a terminal finish (stop, or other with a raw reason;
  // see isTerminalFinish) or content-filter/error. Overwriting it to "tool-calls"
  // reopens a finished turn and the runLoop never idles (classify keeps returning
  // continue on the completed tools). Trailing transport noise after a good final
  // must leave the answer intact.
  if (isTerminalFinish({ finish: input.finish, rawFinish: input.rawFinish }) && input.hasFinalText) return false
  if (input.finish === "content-filter" || input.finish === "error") return false
  const hasCompleted = input.toolParts.some((p) => p.state.status === "completed")
  const hasInFlight = input.toolParts.some((p) => p.state.status === "running" || p.state.status === "pending")
  return hasCompleted && !hasInFlight
}

/**
 * A proposed tool call captured from a candidate stream (max mode), before
 * any execution. `input` is the parsed tool arguments.
 */
export type ProposedToolCall = {
  toolCallId: string
  toolName: string
  input: Record<string, any>
  providerMetadata?: Record<string, any>
}

/**
 * The winning candidate selected by the judge in max mode. Captures the full
 * reasoning + text + proposed tool calls of one candidate stream so the
 * processor can replay it: lay down the reasoning/text parts and actually
 * execute the tool calls via the execute-bearing tools.
 */
export type ReplayInput = {
  reasoning?: string
  reasoningMetadata?: Record<string, any>
  text?: string
  textMetadata?: Record<string, any>
  toolCalls: ProposedToolCall[]
  finishReason: string
  rawFinishReason?: string
  usage?: any
  providerMetadata?: Record<string, any>
  /** Execute-bearing tools (from resolveTools) used to run the winner's calls. */
  tools: Record<string, { execute?: (input: any, options: any) => Promise<any> }>
  /** Model messages passed to tool execute contexts. */
  messages: any[]
  /**
   * Max-mode selection metadata. When set, a short note is prepended to the
   * replayed reasoning so the user can see the ensemble happened and which
   * candidate won. `winner` is 0-based.
   */
  selection?: { winner: number; total: number }
  /**
   * Real wall-clock duration (ms) the winning candidate spent thinking, so the
   * replayed reasoning part shows a meaningful duration instead of the ~1ms
   * synthetic replay time. Optional.
   */
  thinkingMs?: number
  /**
   * Max-mode ensemble overhead: the cost and token counts of the losing
   * candidates + the judge call. These are real spend but consume NO context,
   * so they are added to `cost` and the ModelCall metric ONLY — never to the
   * message's `tokens`, which must stay the winner's real footprint so context
   * overflow / prune estimation stays correct.
   */
  overhead?: { cost: number; tokensIn: number; tokensOut: number }
}

export interface Handle {
  readonly stepRecovery: () => ReturnType<typeof createStepRecoveryRecorder> | undefined
  readonly toolGate: ToolGate
  readonly registerTool: (name: string) => void
  readonly executeTool: <A, E, R>(
    tool: string,
    callID: string,
    body: Effect.Effect<A, E, R>,
    options?: { signal?: AbortSignal; gateTool?: string; delegationKey?: object; delegationTarget?: object },
  ) => Effect.Effect<A, E | Error, R>
  readonly observedCaptureMembership?: (toolCallID: string, expectedStepID?: string) => NonNullable<AppFileSystem.HistoryContext["observedCaptureMembership"]> | undefined
  readonly recordBrowserSnapshotCandidate: (toolCallID:string, output:string, current:()=>boolean) => void
  readonly recordGcfCandidate: (toolCallID: string, output: string, toonCurrent?: () => boolean, outline?: { sidecar: string; current: () => boolean }) => void
  readonly beginArgumentResolution: (callID: string, expectedInput: unknown, target: "glob" | "grep", schemaSHA256: string, binding: object) => Effect.Effect<boolean>
  readonly checkArgumentResolution: (callID: string, expectedInput: unknown, binding: object, claim: boolean) => Effect.Effect<boolean>
  readonly bindDelegationSignal: (key: object, callID: string, target: object, signal: AbortSignal) => boolean
  readonly delegationSignal: (key: object, callID: string, target: object) => AbortSignal | undefined
  readonly executeDelegatedTool: <A, E, R>(
    wrapper: string, callID: string, target: string, targetIdentity: object, optionsKey: object,
    body: Effect.Effect<A, E, R>, options?: { signal?: AbortSignal },
  ) => Effect.Effect<A, E | Error, R>
  readonly message: MessageV2.Assistant
  readonly updateToolCall: (
    toolCallID: string,
    update: (part: MessageV2.ToolPart) => MessageV2.ToolPart,
  ) => Effect.Effect<MessageV2.ToolPart | undefined>
  readonly completeToolCall: (
    toolCallID: string,
    output: unknown,
    providerMetadata?: Record<string, any>,
  ) => Effect.Effect<void>
  readonly process: (streamInput: LLM.StreamInput, rebuildAlternative?: (context: ManagedProvider.OwnedAlternativeRebuild) => Effect.Effect<LLM.StreamInput, unknown>) => Effect.Effect<Result>
  /**
   * Replay a pre-selected candidate (max mode): synthesize the stream events a
   * real model call would have produced for the winner — reasoning, text, then
   * each tool call (input-start → call → execute → result) — and run them
   * through the same event handler used by `process`. Tool execution, History capture, permission asks and metrics are all reused. Returns the same
   * Result contract as `process`.
   */
  readonly replay: (input: ReplayInput) => Effect.Effect<Result>
}

export type AgentMetrics = {
  tokens_in: number
  tokens_out: number
  files_changed: number
}

type Input = {
  assistantMessage: MessageV2.Assistant
  sessionID: SessionID
  model: Provider.Model
  agentMetrics?: AgentMetrics
  actorExecution?: ActorExecutionIdentity
  cfg?: Config.Info
  /**
   * Completes once a newer input waits in this agent's inbox lane. While the step
   * is retrying a failure, that input supersedes the step so the run loop can
   * claim it instead of holding it behind a persistent retry.
   */
  awaitNewInput?: Effect.Effect<void>
}

export interface Interface {
  readonly create: (input: Input) => Effect.Effect<Handle>
}

type ToolCall = {
  partID: MessageV2.ToolPart["id"]
  messageID: MessageV2.ToolPart["messageID"]
  sessionID: MessageV2.ToolPart["sessionID"]
  freshTask?: boolean
}

interface ProcessorContext extends Input {
  toolcalls: Record<string, ToolCall>
  shouldBreak: boolean
  blocked: boolean
  needsOverflowHandling: boolean
  currentText: MessageV2.TextPart | undefined
  reasoningMap: Record<string, MessageV2.ReasoningPart>
  stepStartedAt: number | undefined
  firstTokenAt: number | undefined
  stepPartIds: PartID[]
  textNgramMonitor: TextNgramMonitor | undefined
  textNgramRepeat: boolean
  textPartPersisted: boolean
  retrySafe: boolean
  /** First provider frame of this process() already marked busy; later retry frames must not clobber session.status{retry}. */
  startedBusy: boolean
  /** Durable retry record of this assistant message; survives retry cleanup, never part of stepPartIds. */
  retryPart: MessageV2.RetryPart | undefined
  /** A retry was recorded in this process() and no attempt has produced output since. */
  retrying: boolean
  /** Completed by the next recorded retry; set while a newer input waits for the step to start failing. */
  retryWaiter: Deferred.Deferred<void> | undefined
  /** The step gave way to a newer input; its interruption is not a user abort. */
  superseded: boolean
}

type StreamEvent = Event

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionProcessor") {}

export const layer: Layer.Layer<
  Service,
  never,
  | Session.Service
  | Config.Service
  | Bus.Service
  | Agent.Service
  | LLM.Service
  | Permission.Service
  | Plugin.Service
  | SessionSummary.Service
  | SessionStatus.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const session = yield* Session.Service
    const config = yield* Config.Service
    const bus = yield* Bus.Service
    const agents = yield* Agent.Service
    const llm = yield* LLM.Service
    const permission = yield* Permission.Service
    const plugin = yield* Plugin.Service
    const summary = yield* SessionSummary.Service
    const scope = yield* Scope.Scope
    const status = yield* SessionStatus.Service
    const bridge = yield* EffectBridge.make()

    const create = Effect.fn("SessionProcessor.create")(function* (input: Input) {
      if (ManagedProvider.enabled() && !input.actorExecution) throw new Error("managed actor execution identity is unavailable")
      const toolGate = new ToolGate()
      let toolScope = yield* Scope.make()
      let toolsClosed = false
      const closeTools = () => Effect.gen(function* () {
        toolsClosed = true
        yield* Scope.close(toolScope, Exit.void)
      })
      let observedStep: { id: PartID; closed: boolean; admitted: boolean;
        binding?: Omit<CaptureMembership,"revision"|"coverage"|"members"> } | undefined
      const observedCaptureMembership: NonNullable<Handle["observedCaptureMembership"]> = (toolCallID, expectedStepID) => {
        const actor = input.actorExecution
        const step = observedStep
        if (!actor || !step || step.closed || (expectedStepID !== undefined && step.id !== expectedStepID) || !toolCallID
            || !input.sessionID || !input.assistantMessage.id || !step.id || !actor.sessionID || actor.sessionID !== input.sessionID || !actor.actorID
            || !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0
            || !actor.rootOperationID || !actor.innerTurnID || !session.changeCaptureMembership) return
        const changeCaptureMembership = session.changeCaptureMembership
        const sessionID = input.sessionID, messageID = input.assistantMessage.id, stepID = step.id
        const actorID = actor.actorID, executionRevision = actor.executionRevision
        const rootOperationID = actor.rootOperationID, innerTurnID = actor.innerTurnID
        const host = ManagedProvider.currentActorHostContext(sessionID, actorID, executionRevision)
        const issuer = ManagedProvider.currentActorToolTimingIssuer(sessionID, actorID, executionRevision, rootOperationID)
        if (!host || typeof host.ownerAccountID !== "string" || !host.ownerAccountID || typeof host.chatID !== "string" || !host.chatID
            || typeof host.workspaceID !== "string" || !host.workspaceID || !issuer
            || !isAdmittedToolTimingTurn(actor, {sessionID,actorID,executionRevision,rootOperationID,innerTurnID})) return
        const accountID = host.ownerAccountID, hostChatID = host.chatID, workspaceID = host.workspaceID
        let admittedAction: string | undefined
        const current = (allowClosed: boolean) => {
          const actual = ManagedProvider.currentActorHostContext(sessionID, actorID, executionRevision)
          return observedStep === step && session.changeCaptureMembership === changeCaptureMembership && input.sessionID === sessionID && input.assistantMessage.id === messageID && step.id === stepID && (allowClosed || !step.closed) && isAdmittedToolTimingTurn(actor, {sessionID,actorID,executionRevision,rootOperationID,innerTurnID}) && !!actual
            && actual.ownerAccountID === accountID && actual.workspaceID === workspaceID && actual.chatID === hostChatID
            && ManagedProvider.currentActorToolTimingIssuer(sessionID, actorID, executionRevision, rootOperationID) === issuer
        }
        const begin = async (actionID: string) => {
            if (admittedAction || !current(false)) throw new Error("capture membership authority unavailable")
            const binding = {version:1 as const, sessionID, messageID, stepID,
              accountID, hostChatID, workspaceID, actorID,
              executionRevision, rootOperationID, innerTurnID}
            if (step.binding && JSON.stringify(step.binding) !== JSON.stringify(binding)) throw new Error("capture membership scope changed")
            step.binding = binding
            await bridge.promise(changeCaptureMembership(binding,{kind:"begin",actionID,toolCallID},() => current(false)))
            admittedAction = actionID
            step.admitted = true
            if (step.closed) {
              await bridge.promise(changeCaptureMembership(binding,{kind:"seal"},() => current(true))).catch(() => undefined)
              throw new Error("capture membership step closed")
            }
        }
        const complete = async (actionID: string, actionRef: string) => {
          if (admittedAction !== actionID || !step.binding || !current(true)) throw new Error("capture membership settlement unavailable")
          await bridge.promise(changeCaptureMembership(step.binding,{kind:"complete",actionID,toolCallID,actionRef},() => current(true)))
        }
        return Object.freeze({
          begin: async (history: AppFileSystem.HistoryContext, actionID: string) => {
            const origin = history.managedToolOrigin
            if (!origin || history.accountId !== accountID || history.workspaceId !== workspaceID
                || origin.host_chat_id !== hostChatID || origin.engine_session_id !== sessionID || origin.actor_id !== actorID
                || origin.execution_revision !== executionRevision || origin.root_operation_id !== rootOperationID
                || origin.inner_turn_id !== innerTurnID || origin.tool_call_id !== toolCallID || origin.assistant_message_id !== messageID)
              throw new Error("capture membership authority unavailable")
            await begin(actionID)
          },
          complete: async (_history: AppFileSystem.HistoryContext, actionID: string, actionRef: string) => complete(actionID, actionRef),
          beginManaged: async (fact: AppFileSystem.ManagedCaptureFact, actionID: string) => {
            const source = fact?.source, capture = fact?.capture
            if (!source || !capture || source.accountID !== accountID || source.authorityWorkspaceID !== workspaceID
                || source.hostChatID !== hostChatID || source.sessionID !== sessionID || source.actorID !== actorID
                || source.executionRevision !== executionRevision || source.rootOperationID !== rootOperationID
                || source.innerTurnID !== innerTurnID || source.toolCallID !== toolCallID || source.assistantMessageID !== messageID
                || typeof capture.historyActorID !== "string" || !capture.historyActorID || typeof capture.historyWorkspaceID !== "string" || !capture.historyWorkspaceID)
              throw new Error("capture membership authority unavailable")
            await begin(actionID)
          },
          completeManaged: complete,
          currentManaged: () => !!admittedAction && current(true),
        })
      }
      const timingCaptures = new Map<string, ToolTimingCapture>()
      const gcfCandidates = new Map<string, { sha256: string; bytes: number; toonCurrent?: () => boolean; outline?: { sidecar: string; current: () => boolean } }>()
      const invalidGcfCalls = new Set<string>()
      const browserCandidates = new Map<string,{output:string;current:()=>boolean}>()
      const invalidBrowserCalls = new Set<string>()
      const recordBrowserSnapshotCandidate: Handle["recordBrowserSnapshotCandidate"] = (callID,output,current) => {
        if (!callID || invalidBrowserCalls.has(callID)) return
        if (browserCandidates.has(callID) || browserCandidates.size>=32) {browserCandidates.delete(callID);invalidBrowserCalls.add(callID);return}
        if (typeof output==='string' && Buffer.byteLength(output,'utf8')<=51200 && typeof current==='function') browserCandidates.set(callID,{output,current})
      }
      const recordGcfCandidate: Handle["recordGcfCandidate"] = (callID, output, toonCurrent, outline) => {
        if (!callID || invalidGcfCalls.has(callID)) return
        if (gcfCandidates.has(callID) || gcfCandidates.size >= 128) { gcfCandidates.delete(callID); invalidGcfCalls.add(callID); return }
        const candidate = gcfCandidate(output)
        if (candidate) gcfCandidates.set(callID, { ...candidate, toonCurrent, outline })
      }
      const localTools = new Set<string>()
      const ready = new Map<string, Deferred.Deferred<void>>()
      const toolReady = (callID: string) => {
        let value = ready.get(callID)
        if (!value) {
          value = Deferred.makeUnsafe<void>()
          ready.set(callID, value)
        }
        return value
      }
      const ctx: ProcessorContext = {
        assistantMessage: input.assistantMessage,
        sessionID: input.sessionID,
        model: input.model,
        agentMetrics: input.agentMetrics,
        actorExecution: input.actorExecution,
        cfg: input.cfg,
        toolcalls: {},
        shouldBreak: false,
        blocked: false,
        needsOverflowHandling: false,
        currentText: undefined,
        reasoningMap: {},
        stepStartedAt: undefined,
        firstTokenAt: undefined,
        stepPartIds: [],
        textNgramMonitor: undefined,
        textNgramRepeat: false,
        textPartPersisted: false,
        retrySafe: true,
        startedBusy: false,
        retryPart: undefined,
        retrying: false,
        retryWaiter: undefined,
        superseded: false,
      }
      let stepRecovery: ReturnType<typeof createStepRecoveryRecorder> | undefined
      let aborted = false
      // Only the main agent owns session-level status. Subagents (explore,
      // general, checkpoint-writer, etc.) share the parent sessionID but their
      // run-state onIdle deliberately does NOT reset status (run-state.ts) — so
      // if a subagent's stream sets session status here, nothing ever clears it
      // and the TUI spinner stays spinning after the main agent has finished.
      const isMain = !ctx.assistantMessage.agentID || ctx.assistantMessage.agentID === "main"
      const systemAgent = SYSTEM_SPAWNED_AGENT_TYPES.has(ctx.assistantMessage.agent)
      const slog = log.clone().tag("session.id", input.sessionID).tag("messageID", input.assistantMessage.id)

      // Every actor records its retries on its own assistant message, so the
      // record is visible for subagents too and survives reloads until the turn
      // succeeds. Max-mode candidate/judge retries are not the user-visible step.
      const recordRetry = Effect.fn("SessionProcessor.recordRetry")(function* (info: {
        phase: MessageV2.RetryPart["phase"]
        scope: SessionRetry.RetryScope
        kind: SessionRetry.RetryKind
        message: string
        hostCode?: string
        next: number
      }) {
        if (info.scope !== "request" && info.scope !== "live-step") return
        if (info.kind === "terminal") return
        const existing = ctx.retryPart ??
          MessageV2.parts(ctx.assistantMessage.id).find((part): part is MessageV2.RetryPart => part.type === "retry")
        const now = Date.now()
        ctx.retryPart = yield* session.updatePart({
          id: existing?.id ?? PartID.ascending(),
          messageID: ctx.assistantMessage.id,
          sessionID: ctx.sessionID,
          type: "retry",
          attempt: (existing?.attempt ?? 0) + 1,
          phase: info.phase,
          scope: info.scope,
          kind: info.kind,
          message: info.message,
          ...(info.hostCode ? { hostCode: info.hostCode } : {}),
          next: info.next,
          time: { created: existing?.time.created ?? now, updated: now },
        } satisfies MessageV2.RetryPart)
        ctx.retrying = true
        if (ctx.retryWaiter) yield* Deferred.succeed(ctx.retryWaiter, undefined)
      })

      const parse = (e: unknown) =>
        MessageV2.fromLiveError(e, {
          providerID: input.model.providerID,
          aborted,
          allow404Retry: ProviderError.allowsModelNotFoundRetry(input.model),
        })

      const tryBestConfig = (yield* config.get()).experimental?.try_best
      const tryBest = Flag.OPENCLANK_ENABLE_TRY_BEST_HANDOFF
        ? tryBestMonitor(input.sessionID, input.assistantMessage.agentID, tryBestConfig)
        : undefined

      const detectTryBest = Effect.fn("SessionProcessor.detectTryBest")(function* (part: MessageV2.ToolPart) {
        if (ctx.blocked) return
        const incident = tryBest?.consume(part)
        if (!incident) return
        tryBest?.reset()
        ctx.blocked = true
        const detail = describeTryBest(incident)
        yield* session.updatePart({
          id: PartID.ascending(),
          messageID: ctx.assistantMessage.id,
          sessionID: ctx.sessionID,
          type: "text",
          text: `Try-best loop detected; this turn was paused. ${detail}`,
          synthetic: true,
          metadata: {
            origin: {
              kind: "try_best",
              providerID: input.model.providerID,
              modelID: input.model.id,
              incident,
            },
          },
          time: { start: Date.now(), end: Date.now() },
        })
        yield* bus.publish(Session.Event.TryBestDetected, {
          sessionID: ctx.sessionID,
          agentID: ctx.assistantMessage.agentID,
          providerID: input.model.providerID,
          modelID: input.model.id,
          ...incident,
        })
        yield* bus
          .publish(Metrics.TryBestDetected, {
            sessionID: ctx.sessionID,
            reason: incident.reason,
            provider: input.model.providerID,
            model_id: input.model.id,
            count: incident.evidence.count,
            similarity: incident.evidence.similarity,
            action: incident.evidence.action,
          })
          .pipe(Effect.ignore)
      })

      const updateToolCall = Effect.fn("SessionProcessor.updateToolCall")(function* (
        toolCallID: string,
        update: (part: MessageV2.ToolPart) => MessageV2.ToolPart,
        publication?: (previous: MessageV2.ToolPart | undefined, next: MessageV2.ToolPart) => { timing?: ToolTimingCapture; view?: import("./tool-view-source").ToolViewCapture; gcf?: { outline?: string }; browserSnapshot?: true } | undefined,
      ) {
        const call = ctx.toolcalls[toolCallID]
        if (!call) return
        const activityPublication = (previous: MessageV2.ToolPart | undefined, next: MessageV2.ToolPart) => {
          const timing=timingCaptures.get(toolCallID), actor=ctx.actorExecution
          const issuer=actor?.rootOperationID && ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID,actor.actorID,actor.executionRevision,actor.rootOperationID)
          if (previous?.state.status === "running" && next.state.status === "running" && next.execution === "started" && timing && actor &&
              issuer === timing.owner && issuer.isAlive() && isAdmittedToolTimingTurn(actor,timing.identity)) return {timing}
          return undefined
        }
        return yield* session.mutateToolPart(call, (part) => part ? update(part) : undefined, publication ?? activityPublication)
      })

      const completeToolCall = Effect.fn("SessionProcessor.completeToolCall")(function* (
        toolCallID: string,
        output: unknown,
        providerMetadata?: Record<string, any>,
      ) {
        const result = isToolExecutionResult(output) ? output : undefined
        const candidate = gcfCandidates.get(toolCallID); gcfCandidates.delete(toolCallID)
        const timing = timingCaptures.get(toolCallID); timingCaptures.delete(toolCallID)
        let changed = false
        const part = yield* updateToolCall(toolCallID, (current) => {
          if (current.state.status !== "running") return current
          changed = true
          const structured = !result && current.metadata?.providerExecuted
          return {
            ...current,
            state: {
              status: "completed",
              input: current.state.input,
              output: result?.output ?? displayToolOutput(output),
              ...(structured ? { providerOutput: jsonToolOutput(output) } : {}),
              ...(providerMetadata ? { providerMetadata } : {}),
              metadata: result?.metadata ?? {},
              title: result?.title ?? "",
              time: { start: current.state.time.start, end: Date.now() },
              attachments: result?.attachments,
            },
          }
        }, (previous, completed) => {
          if (!changed || !previous || previous.state.status !== "running" || completed.state.status !== "completed") return
          const browserCandidate=browserCandidates.get(toolCallID)
          browserCandidates.delete(toolCallID)
          const actor = ctx.actorExecution
          const issuer = actor?.rootOperationID && ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID)
          const validTiming = timing && actor && issuer === timing.owner && issuer.isAlive() && isAdmittedToolTimingTurn(actor, timing.identity) ? timing : undefined
          const host = actor && ManagedProvider.currentActorHostContext(ctx.sessionID, actor.actorID, actor.executionRevision)
          const admission = host?.toolViewAdmission ?? host?.jsonWhitespaceAdmission
          const final = result ? gcfCandidate(result.output) : undefined
          const qualify = !!(candidate && final && candidate.sha256 === final.sha256 && candidate.bytes === final.bytes &&
            !previous.metadata?.providerExecuted && result?.metadata?.truncated === false && result.metadata.outputPath === undefined &&
            result.metadata.mcp?.isError === false && previous.contextSelection?.action !== "trim" && previous.contextSelection?.action !== "omit" &&
            (!result.attachments || result.attachments.length === 0))
          let outline: string | undefined
          let verified: Parameters<typeof captureToolView>[4]
          try {
            if (qualify && candidate?.outline?.current()) { outline = candidate.outline.sidecar; verified = "source-outline-v1" }
            else if (qualify && candidate?.toonCurrent?.()) verified = "toon-v1"
            else if (result && validTiming && completed.tool === "bash") verified = readBashView(result) ?? (readTerminalAnsi(result) ? "terminal-ansi-v1" : undefined)
            else if (result && validTiming && completed.tool === "webfetch") verified = readWebfetchMarkdown(result, {sessionID:ctx.sessionID,messageID:completed.messageID,callID:completed.callID}) ? "markdown-slim-v1" : readWebfetchHtml(result,{sessionID:ctx.sessionID,messageID:completed.messageID,callID:completed.callID}) ? "html-markdown-v1" : undefined
            else if (result && validTiming && completed.tool === "read") verified = readReadViewProfile(result)
            else if (result && validTiming && completed.tool === "grep" && readGrepRows(result)) verified = selectedToolViewProfile(admission, "grep")
          } catch { verified = undefined; outline = undefined }
          const view = ctx.toolcalls[toolCallID]?.freshTask && admission && validTiming && actor
            ? captureToolView(admission, validTiming, completed, ManagedProvider.currentToolViewRoute(ctx.sessionID,actor.actorID,actor.executionRevision), verified) : undefined
          let browserSnapshot=false
          try { browserSnapshot=!!(browserCandidate && browserCandidate.current() && validTiming && actor && previous.execution==='started' &&
            result && browserCandidate.output===result.output && !previous.metadata?.providerExecuted && result.metadata?.truncated===false &&
            result.metadata.outputPath===undefined && result.metadata.mcp?.isError===false && (!result.attachments || result.attachments.length===0)) } catch {}
          return { timing: validTiming, view, ...(qualify ? { gcf: { outline } } : {}), ...(browserSnapshot ? {browserSnapshot:true as const} : {}) }
        })
        if (!part || !changed) return
        yield* detectTryBest(part)
      })

      const failToolCall = Effect.fn("SessionProcessor.failToolCall")(function* (toolCallID: string, error: unknown) {
        const timing = timingCaptures.get(toolCallID); timingCaptures.delete(toolCallID)
        gcfCandidates.delete(toolCallID)
        let changed = false
        // Agent-recoverable failures (bad args, malformed call, unknown task/actor
        // id) carry a marker the TUI reads to render them muted instead of as a red
        // error block. The full actionable message still flows to the model.
        const recoverable = isRecoverableError(error)
        const metadata = {
          ...getToolResultMetadata(error),
          ...(recoverable ? { recoverable: true } : {}),
        }
        const attachments = getToolResultAttachments(error)?.flatMap((attachment) => {
          const parsed = MessageV2.FilePart.safeParse(attachment)
          return parsed.success ? [parsed.data] : []
        })
        const part = yield* updateToolCall(toolCallID, (current) => {
          if (current.state.status !== "running") return current
          changed = true
          return {
            ...current,
            state: {
              status: "error",
              input: current.state.input,
              inputComplete: true,
              error: errorMessage(error),
              metadata: { ...current.state.metadata, ...metadata },
              ...(attachments && attachments.length > 0 ? { attachments } : {}),
              time: { start: current.state.time.start, end: Date.now() },
            },
          }
        }, (previous, failed) => {
          const actor = ctx.actorExecution
          const issuer = actor?.rootOperationID && ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID)
          if (!changed || previous?.state.status !== "running" || failed.state.status !== "error" || !timing || !actor ||
              issuer !== timing.owner || !issuer.isAlive() || !isAdmittedToolTimingTurn(actor, timing.identity)) return
          return { timing }
        })
        if (!part || !changed) return false
        yield* detectTryBest(part)
        if (error instanceof Permission.RejectedError || error instanceof Question.RejectedError) {
          ctx.blocked = ctx.blocked || ctx.shouldBreak
        }
        return true
      })

      type Delegation = { wrapper: string; target: string; targetIdentity: object; callID: string; partID: string;
        actor: ActorExecutionIdentity; expected: {sessionID:string; actorID:string; executionRevision:number; rootOperationID:string; innerTurnID:string};
        scope: NonNullable<ReturnType<typeof ManagedProvider.currentScope>>; signal?: AbortSignal; cancellation?: AbortSignal; active: boolean; consumed: boolean }
      const delegations = new WeakMap<object, Delegation>()
      const activeDelegations = new Map<string, Delegation>()
      const liveDelegation = (key: object, callID: string, target: object) => {
        const grant = delegations.get(key)
        if (!grant || !grant.active || grant.consumed || grant.callID !== callID || grant.targetIdentity !== target ||
            grant.signal?.aborted || grant.cancellation?.aborted || toolsClosed || ctx.actorExecution !== grant.actor ||
            !isAdmittedToolTimingTurn(grant.actor, grant.expected) ||
            ManagedProvider.currentScope(ctx.sessionID, grant.actor.actorID, grant.actor.executionRevision) !== grant.scope) return
        return grant
      }
      const bindDelegationSignal: Handle["bindDelegationSignal"] = (key, callID, target, signal) => {
        const grant = liveDelegation(key, callID, target)
        if (!grant || grant.cancellation || signal.aborted) return false
        grant.cancellation = signal
        return true
      }
      const delegationSignal: Handle["delegationSignal"] = (key, callID, target) =>
        liveDelegation(key, callID, target)?.cancellation
      const executeTool: Handle["executeTool"] = (tool, callID, body, options) => Effect.gen(function* () {
        if (toolsClosed || options?.signal?.aborted) return yield* Effect.fail(new Error("Tool execution is closed"))
        if (options?.delegationKey !== undefined) {
          const grant = delegations.get(options.delegationKey)
          if (!grant && activeDelegations.has(callID)) return yield* Effect.fail(new Error("Delegated tool capability unavailable"))
          if (grant && (!grant.active || grant.consumed || (grant.signal?.aborted || grant.cancellation?.aborted) || grant.target !== tool ||
              grant.callID !== callID || grant.targetIdentity !== options.delegationTarget ||
              ctx.actorExecution !== grant.actor || !isAdmittedToolTimingTurn(grant.actor, grant.expected) ||
              ManagedProvider.currentScope(ctx.sessionID, grant.actor.actorID, grant.actor.executionRevision) !== grant.scope))
            return yield* Effect.fail(new Error("Delegated tool capability unavailable"))
          if (grant) {
          const call = ctx.toolcalls[callID]
          if (!call || call.partID !== grant.partID) return yield* Effect.fail(new Error("Delegated tool call changed"))
          const part = yield* session.getPart({ partID: call.partID, messageID: call.messageID, sessionID: call.sessionID })
          if (!part || part.type !== "tool" || part.tool !== grant.wrapper || part.callID !== callID ||
              part.state.status !== "running" || part.execution !== "started" || toolsClosed ||
              options.signal?.aborted || (grant.signal?.aborted || grant.cancellation?.aborted) || !grant.active || grant.consumed ||
              ctx.actorExecution !== grant.actor || !isAdmittedToolTimingTurn(grant.actor, grant.expected) ||
              ManagedProvider.currentScope(ctx.sessionID, grant.actor.actorID, grant.actor.executionRevision) !== grant.scope)
            return yield* Effect.fail(new Error("Delegated tool boundary changed"))
          grant.consumed = true
          return yield* body
          }
        }
        const execution = Effect.gen(function* () {
          yield* Deferred.await(toolReady(callID))
          return yield* toolGate.run(options?.gateTool ?? tool, callID, Effect.gen(function* () {
            const call = ctx.toolcalls[callID]
            if (!call) return yield* Effect.fail(new Error("Tool call is not registered"))
            yield* session.mutateToolPart(call, (part) => {
              if (toolsClosed || options?.signal?.aborted) throw new Error("Tool execution is closed")
              if (!part || part.tool !== tool || part.callID !== callID || part.state.status !== "running" ||
                part.execution !== "not-started" || part.metadata?.providerExecuted) {
                throw new Error("Tool execution has no trusted input boundary")
              }
              return { ...part, execution: "started" }
            }, (previous, next) => {
              const timing=timingCaptures.get(callID), actor=ctx.actorExecution
              const issuer=actor?.rootOperationID && ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID,actor.actorID,actor.executionRevision,actor.rootOperationID)
              if (previous?.execution === "not-started" && next.execution === "started" && timing && actor &&
                  issuer === timing.owner && issuer.isAlive() && isAdmittedToolTimingTurn(actor,timing.identity)) {
                timing.activityStartAllowed=true
                return {timing}
              }
              return undefined
            })
            const settled: { callID: string; result?: Effect.Effect<unknown, unknown> } = { callID }
            return yield* Effect.uninterruptibleMask((restore) => restore(body.pipe(
              Effect.provideService(ResultRef, settled),
            )).pipe(
              Effect.tap((output) => completeToolCall(callID, output)),
              Effect.onExit((exit) => Exit.isFailure(exit) && settled.result
                ? settled.result.pipe(Effect.matchEffect({
                    onSuccess: (output) => completeToolCall(callID, output),
                    onFailure: (error) => failToolCall(callID, error).pipe(Effect.asVoid),
                  }))
                : Effect.void),
              Effect.tapError((error) => failToolCall(callID, error)),
            ))
          }), { signal: options?.signal })
        })
        const fiber = yield* execution.pipe(Effect.forkIn(toolScope))
        // Unwrap Exit after notification; join can skip concurrent scope waiters on interruption in Effect beta.48.
        const exit = yield* Fiber.await(fiber)
        return yield* exit
      })

      const executeDelegatedTool: Handle["executeDelegatedTool"] = (wrapper, callID, target, targetIdentity, optionsKey, body, options) =>
        executeTool(wrapper, callID, Effect.gen(function* () {
          const actor = ctx.actorExecution
          if (!actor || !actor.rootOperationID || !actor.innerTurnID || !localTools.has(target) || options?.signal?.aborted)
            return yield* Effect.fail(new Error("Delegated tool capability unavailable"))
          const expected = { sessionID: actor.sessionID, actorID: actor.actorID, executionRevision: actor.executionRevision,
            rootOperationID: actor.rootOperationID, innerTurnID: actor.innerTurnID }
          const scope = ManagedProvider.currentScope(ctx.sessionID, actor.actorID, actor.executionRevision)
          const call = ctx.toolcalls[callID]
          if (!scope || actor.sessionID !== ctx.sessionID || !call || delegations.has(optionsKey) || activeDelegations.has(callID) ||
              !isAdmittedToolTimingTurn(actor, expected)) return yield* Effect.fail(new Error("Delegated tool capability unavailable"))
          const grant: Delegation = { wrapper, target, targetIdentity, callID, partID: call.partID, actor, expected, scope,
            signal: options?.signal, active: true, consumed: false }
          delegations.set(optionsKey, grant)
          activeDelegations.set(callID, grant)
          return yield* body.pipe(Effect.ensuring(Effect.sync(() => {
            grant.active = false
            if (delegations.get(optionsKey) === grant) delegations.delete(optionsKey)
            if (activeDelegations.get(callID) === grant) activeDelegations.delete(callID)
            argumentResolutions.delete(callID)
          })))
        }), { signal: options?.signal, gateTool: target })

      type ArgumentResolution = { partID: string; encoded: string; binding: object; claimed: boolean; grant: Delegation }
      const argumentResolutions = new Map<string, ArgumentResolution>()
      const argumentCurrent = (callID: string, entry: ArgumentResolution) => {
        const grant = activeDelegations.get(callID)
        return grant === entry.grant && grant.active && !grant.consumed && !(grant.signal?.aborted || grant.cancellation?.aborted) && !toolsClosed &&
          ctx.actorExecution === grant.actor && isAdmittedToolTimingTurn(grant.actor, grant.expected) &&
          ManagedProvider.currentScope(ctx.sessionID, grant.actor.actorID, grant.actor.executionRevision) === grant.scope
      }
      const beginArgumentResolution: Handle["beginArgumentResolution"] = (callID, expectedInput, target, schemaSHA256, binding) => Effect.gen(function* () {
        stepRecovery?.invalidate()
        const grant = activeDelegations.get(callID), call = ctx.toolcalls[callID]
        if (!grant || grant.target !== target || !call?.freshTask || call.partID !== grant.partID || argumentResolutions.has(callID) ||
            !/^[a-f0-9]{64}$/.test(schemaSHA256)) return false
        const candidate: ArgumentResolution = { partID: call.partID, encoded: "", binding, claimed: false, grant }
        if (!argumentCurrent(callID, candidate)) return false
        const part = yield* session.getPart({ partID: call.partID, messageID: call.messageID, sessionID: call.sessionID })
        if (!part || part.type !== "tool" || part.tool !== grant.wrapper || part.state.status !== "running" ||
            part.execution !== "started" || part.metadata?.providerExecuted || !argumentCurrent(callID, candidate)) return false
        try {
          candidate.encoded = JSON.stringify(expectedInput)
          if (candidate.encoded !== JSON.stringify(part.state.input) || Buffer.byteLength(candidate.encoded, "utf8") > 60_000) return false
        } catch { return false }
        const updated = yield* session.mutateToolPart(call, (current) => {
          if (!current || current.type !== "tool" || current.tool !== grant.wrapper || current.state.status !== "running" ||
              current.execution !== "started" || JSON.stringify(current.state.input) !== candidate.encoded || !argumentCurrent(callID, candidate))
            throw new Error("Argument resolution boundary changed")
          const metadata = { ...current.metadata }
          delete metadata.openclankArgumentResolution
          metadata.openclankArgumentResolution = { version: 1, target, schemaSHA256, policy: "gcf-generic-v3-safe-integers-v1" }
          return { ...current, metadata }
        })
        if (!updated || !argumentCurrent(callID, candidate)) return false
        argumentResolutions.set(callID, candidate)
        return true
      })
      const checkArgumentResolution: Handle["checkArgumentResolution"] = (callID, expectedInput, binding, claim) => Effect.gen(function* () {
        const entry = argumentResolutions.get(callID), call = ctx.toolcalls[callID]
        if (!entry || entry.binding !== binding || (claim && entry.claimed) || !call?.freshTask || call.partID !== entry.partID || !argumentCurrent(callID, entry)) return false
        const part = yield* session.getPart({ partID: call.partID, messageID: call.messageID, sessionID: call.sessionID })
        if (!part || part.type !== "tool" || part.tool !== entry.grant.wrapper || part.state.status !== "running" || part.execution !== "started" || !argumentCurrent(callID, entry)) return false
        try { if (JSON.stringify(expectedInput) !== entry.encoded || JSON.stringify(part.state.input) !== entry.encoded) return false } catch { return false }
        if (claim) entry.claimed = true
        return true
      })

      const checkTextNgram = (text: string) => {
        if (ctx.textNgramRepeat || !ctx.textNgramMonitor) return
        if (ctx.textNgramMonitor.append(text)) ctx.textNgramRepeat = true
      }

      const commitManagedAttempt = () => Effect.suspend(() => {
        // Set the replay fence before Host commit: failure means unknown outcome.
        ctx.retrySafe = false
        return ManagedProvider.enabled()
          ? Effect.promise(() => ManagedProvider.commitSessionOperation(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision))
          : Effect.void
      })

      let handledFinishStep = false
      let completionSettled = false
      let pendingFinishObservation: ReturnType<typeof takeSdkSuccessStep>["observation"]
      const settleManagedCompletion = Effect.fnUntraced(function* (lastUser: MessageV2.User | undefined) {
        if (!ManagedProvider.enabled() || !handledFinishStep || completionSettled) return
        completionSettled = true
        const parts = MessageV2.parts(ctx.assistantMessage.id)
        const classification = lastUser ? classifyAssistantStep({ phase: "after-process", lastUser,
          assistant: ctx.assistantMessage, parts, processResult: "stop" }) : undefined
        const usableTool = parts.some((part) => part.type === "tool" && part.state.status !== "error")
        const complete = !!classification && !ctx.needsOverflowHandling && !ctx.textNgramRepeat && !ctx.blocked && !ctx.assistantMessage.error &&
          !(ctx.assistantMessage.finish === "other" && !ctx.assistantMessage.rawFinish)
        let outcome: "success" | "invalid_output" | "unknown" = "unknown"
        if (complete && classification) {
          if (classification.type === "invalid") outcome = "invalid_output"
          else if (classification.type === "final" || (classification.type === "continue" && usableTool)) outcome = "success"
        }
        yield* Effect.promise(() => ManagedProvider.recordSessionCompletion(ctx.sessionID, outcome, ctx.actorExecution?.actorID,
          ctx.actorExecution?.executionRevision, pendingFinishObservation))
        pendingFinishObservation = undefined
      })
      let outputAttempt: SourceAttemptHandle | undefined
      let replaying = false
      const handleEvent = Effect.fnUntraced(function* (value: StreamEvent) {
        if (outputAttempt && (replaying || !["start", "start-step", "text-start", "text-delta", "text-end", "finish-step", "finish"].includes(value.type))) invalidateSourceOutput(outputAttempt)
        switch (value.type) {
          case "start":
            // First provider frame of this process() marks busy. Later frames belong
            // to request-ladder retries of the same attempt and must not clobber
            // session.status{retry} (the reconnect row is the only user-visible wait).
            if (isMain && !ctx.startedBusy) {
              ctx.startedBusy = true
              yield* status.set(ctx.sessionID, { type: "busy" })
            }
            return

          case "reasoning-start":
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            ctx.retrying = false
            if (isMain) yield* status.set(ctx.sessionID, { type: "busy" })
            if (value.id in ctx.reasoningMap) return
            ctx.reasoningMap[value.id] = {
              id: PartID.ascending(),
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "reasoning",
              text: "",
              // Replayed candidates (max mode) carry a backdated start so the
              // displayed thinking duration reflects the real candidate latency
              // instead of the ~1ms synthetic replay. Live streams omit `time`.
              time: { start: (value as { time?: { start: number } }).time?.start ?? Date.now() },
              metadata: value.providerMetadata,
            }
            yield* session.updatePart(ctx.reasoningMap[value.id])
            ctx.stepPartIds.push(ctx.reasoningMap[value.id].id)
            return

          case "reasoning-delta":
            if (!ctx.firstTokenAt) ctx.firstTokenAt = Date.now()
            if (!(value.id in ctx.reasoningMap)) return
            ctx.reasoningMap[value.id].text += value.text
            checkTextNgram(value.text)
            if (value.providerMetadata) ctx.reasoningMap[value.id].metadata = value.providerMetadata
            yield* session.updatePartDelta({
              sessionID: ctx.reasoningMap[value.id].sessionID,
              messageID: ctx.reasoningMap[value.id].messageID,
              partID: ctx.reasoningMap[value.id].id,
              field: "text",
              delta: value.text,
            })
            return

          case "reasoning-end":
            if (!(value.id in ctx.reasoningMap)) return
            // oxlint-disable-next-line no-self-assign -- reactivity trigger
            ctx.reasoningMap[value.id].text = ctx.reasoningMap[value.id].text
            ctx.reasoningMap[value.id].time = { ...ctx.reasoningMap[value.id].time, end: Date.now() }
            if (value.providerMetadata) ctx.reasoningMap[value.id].metadata = value.providerMetadata
            yield* session.updatePart(ctx.reasoningMap[value.id])
            delete ctx.reasoningMap[value.id]
            return

          case "tool-input-start":
            stepRecovery?.emit(value.id)
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            ctx.retrying = false
            if (isMain) yield* status.set(ctx.sessionID, { type: "busy" })
            if (ctx.assistantMessage.summary) {
              throw new Error(`Tool call not allowed while generating summary: ${value.toolName}`)
            }
            if (toolsClosed) return
            const partID = ctx.toolcalls[value.id]?.partID ?? PartID.ascending()
            let freshTask = false
            const part = yield* session.mutateToolPart({
              partID, messageID: ctx.assistantMessage.id, sessionID: ctx.sessionID,
            }, (current) => current ? {
              ...current,
              metadata: value.providerExecuted ? { ...current.metadata, providerExecuted: true } : current.metadata,
            } : (freshTask = true, {
              id: partID,
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "tool",
              tool: value.toolName,
              callID: value.id,
              execution: !value.providerExecuted && localTools.has(value.toolName) ? "not-started" : undefined,
              state: { status: "pending", input: {}, raw: "" },
              metadata: value.providerExecuted ? { providerExecuted: true } : undefined,
            }))
            if (!part) throw new Error("Tool registration was not persisted")
            ctx.stepPartIds.push(part.id)
            ctx.toolcalls[value.id] = {
              partID: part.id,
              messageID: part.messageID,
              sessionID: part.sessionID,
              freshTask,
            }
            return

          case "tool-input-delta":
            return

          case "tool-input-end":
            return

          case "tool-call": {
            yield* commitManagedAttempt()
            if (ctx.assistantMessage.summary) {
              throw new Error(`Tool call not allowed while generating summary: ${value.toolName}`)
            }
            // A tool call may already have caused an external side effect before
            // the provider stream fails. Replaying the whole model step is unsafe.
            ctx.retrySafe = false
            yield* updateToolCall(value.toolCallId, (match) => ({
              ...match,
              tool: value.toolName,
              execution: match.execution ?? (match.state.status === "pending" && localTools.has(value.toolName) &&
                !match.metadata?.providerExecuted && !value.providerExecuted ? "not-started" : undefined),
              state: match.state.status !== "pending" ? match.state : {
                status: "running",
                input: value.input,
                time: { start: Date.now() },
              },
              metadata: match.metadata?.providerExecuted || value.providerExecuted
                ? { ...value.providerMetadata, providerExecuted: true }
                : value.providerMetadata,
            }))
            const call = ctx.toolcalls[value.toolCallId]
            const timingPart = call && (yield* session.getPart({ partID: call.partID, messageID: call.messageID, sessionID: call.sessionID }))
            const actor = ctx.actorExecution
            if (call?.freshTask && timingPart?.type === "tool" && actor?.rootOperationID && actor.innerTurnID && isAdmittedToolTimingTurn(actor, { sessionID: actor.sessionID, actorID: actor.actorID, executionRevision: actor.executionRevision, rootOperationID: actor.rootOperationID, innerTurnID: actor.innerTurnID })) {
              ManagedProvider.markFreshTaskCall({ sessionID: timingPart.sessionID, rootOperationID: actor.rootOperationID, actorID: actor.actorID,
                executionRevision: actor.executionRevision, innerTurnID: actor.innerTurnID, messageID: timingPart.messageID, partID: timingPart.id,
                toolCallID: timingPart.callID, toolName: timingPart.tool === "mcp_tool_use" && timingPart.execution === "not-started" && !timingPart.metadata?.providerExecuted && timingPart.state.status === "running"
                  ? (deferredFilesMutate(timingPart.state.input)?.toolName ?? deferredOrdinaryFilesMutate(timingPart.state.input)?.toolName ?? timingPart.tool) : timingPart.tool })
              const issuer = ManagedProvider.currentActorToolTimingIssuer(ctx.sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID)
              const timing = captureToolTiming(issuer, actor, timingPart)
              if (timing) timingCaptures.set(value.toolCallId, timing)
            }
            yield* Deferred.succeed(toolReady(value.toolCallId), undefined)

            const parts = MessageV2.parts(ctx.assistantMessage.id)
            const recentParts = parts.slice(-DOOM_LOOP_THRESHOLD)

            if (
              recentParts.length !== DOOM_LOOP_THRESHOLD ||
              !recentParts.every(
                (part) =>
                  part.type === "tool" &&
                  part.tool === value.toolName &&
                  part.state.status !== "pending" &&
                  JSON.stringify(part.state.input) === JSON.stringify(value.input),
              )
            ) {
              return
            }

            const agent = yield* resolveStoredAgent(agents, ctx.assistantMessage.agent)
            yield* permission.ask({
              permission: "doom_loop",
              patterns: [value.toolName],
              sessionID: ctx.assistantMessage.sessionID,
              metadata: { tool: value.toolName, input: value.input },
              always: [value.toolName],
              ruleset: agent.permission,
              // System-spawned background agents have no human to answer — fail clean.
              interactive: !SYSTEM_SPAWNED_AGENT_TYPES.has(ctx.assistantMessage.agent),
            })
            return
          }

          case "tool-result": {
            if (value.providerExecuted) yield* updateToolCall(value.toolCallId, (part) => ({
              ...part, metadata: { ...part.metadata, providerExecuted: true },
            }))
            yield* completeToolCall(value.toolCallId, value.output, value.providerMetadata)
            return
          }

          case "tool-error": {
            stepRecovery?.invalidate()
            if (value.providerExecuted) yield* updateToolCall(value.toolCallId, (part) => ({
              ...part, metadata: { ...part.metadata, providerExecuted: true },
            }))
            yield* failToolCall(value.toolCallId, value.error)
            yield* bus
              .publish(Metrics.ToolCall, {
                sessionID: ctx.sessionID,
                tool_name: metricToolName(value.toolName, value.input),
                input_bytes: Metrics.jsonByteLength(value.input),
                output_bytes: 0,
                tool_call_id: value.toolCallId,
                tool_call_status: "error",
              })
              .pipe(Effect.ignore)
            return
          }

          case "error": {
            try {
              const actor = ctx.actorExecution
              const scope = ManagedProvider.currentScope(ctx.sessionID, actor?.actorID, actor?.executionRevision)
              const original = ManagedProvider.originalRefreshObservationScope(ctx.sessionID, actor?.actorID, actor?.executionRevision)
              const observed = hasObservedMalformedResponse(value.error, scope) ? scope :
                hasObservedMalformedResponse(value.error, original) ? original : undefined
              if (observed) {
                if (!ctx.retrySafe && hasMalformedResponse(value.error, observed))
                  emitMalformedDiagnostic(value.error, observed, ManagedProvider.committedRefreshDiagnostic(ctx.sessionID, actor?.actorID, actor?.executionRevision))
                // A malformed successful response does not prove the provider did no work.
                ctx.retrySafe = false
              }
            } catch { if (ManagedProvider.enabled()) ctx.retrySafe = false }
            throw value.error
          }

          case "start-step":
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            ctx.stepStartedAt = Date.now()
            ctx.firstTokenAt = undefined
            const stepStartPartId = PartID.ascending()
            observedStep = { id: stepStartPartId, closed: false, admitted: false }
            yield* session.updatePart({
              id: stepStartPartId,
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.sessionID,
              type: "step-start",
            })
            ctx.stepPartIds.push(stepStartPartId)
            stepRecovery?.start(stepStartPartId)
            return

          case "finish-step": {
            handledFinishStep = true
            if (!replaying) finishSourceOutputStep(outputAttempt, value.finishReason)
            stepRecovery?.close()
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            if (observedStep) {
              const step = observedStep
              step.closed = true
              if (step.admitted && step.binding && session.changeCaptureMembership) {
                const bound = step.binding
                yield* session.changeCaptureMembership(bound, {kind:"seal"}, () => {
                  const current = ManagedProvider.currentActorHostContext(bound.sessionID,bound.actorID,bound.executionRevision)
                  return observedStep === step && !!current && current.ownerAccountID === bound.accountID && current.chatID === bound.hostChatID && current.workspaceID === bound.workspaceID &&
                    isAdmittedToolTimingTurn(ctx.actorExecution, { sessionID:bound.sessionID, actorID:bound.actorID, executionRevision:bound.executionRevision, rootOperationID:bound.rootOperationID, innerTurnID:bound.innerTurnID }) && !!ManagedProvider.currentActorToolTimingIssuer(bound.sessionID,bound.actorID,bound.executionRevision,bound.rootOperationID)
                }).pipe(Effect.ignore)
              }
            }
            let scoped: ReturnType<typeof takeSdkSuccessStep> = { metadata: undefined, observation: undefined }
            try { scoped = takeSdkSuccessStep(ManagedProvider.successObservationScope(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision), value.providerMetadata) } catch {}
            pendingFinishObservation = scoped.observation
            const usage = Session.getUsage({
              model: ctx.model,
              usage: value.usage,
              captureScope: ManagedProvider.currentScope(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision),
              metadata: scoped.metadata,
            })
            ctx.assistantMessage.finish = value.finishReason
            ctx.assistantMessage.rawFinish = value.rawFinishReason
            ctx.assistantMessage.cost += usage.cost
            ctx.assistantMessage.tokens = usage.tokens
            yield* session.updatePart({
              id: PartID.ascending(),
              reason: value.finishReason,
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "step-finish",
              tokens: usage.tokens,
              cost: usage.cost,
            })
            yield* session.updateMessage(ctx.assistantMessage)
            const stepTokensIn = usage.tokens.input + usage.tokens.cache.read + usage.tokens.cache.write
            const stepTokensOut = usage.tokens.output + usage.tokens.reasoning
            if (ctx.agentMetrics) {
              ctx.agentMetrics.tokens_in += stepTokensIn
              ctx.agentMetrics.tokens_out += stepTokensOut
            }
            yield* bus
              .publish(Metrics.ModelCall, {
                sessionID: ctx.sessionID,
                finish_reason: value.finishReason,
                ttft_ms: ctx.firstTokenAt && ctx.stepStartedAt ? ctx.firstTokenAt - ctx.stepStartedAt : undefined,
                latency_ms: ctx.stepStartedAt ? Date.now() - ctx.stepStartedAt : 0,
                cached_read_tokens: usage.tokens.cache.read,
                model_id: ctx.model.id,
                provider: ctx.model.providerID,
                total_tokens_in: stepTokensIn,
                total_tokens_out: stepTokensOut,
              })
              .pipe(Effect.ignore)
            yield* summary
              .summarize({
                sessionID: ctx.sessionID,
                messageID: ctx.assistantMessage.parentID,
              })
              .pipe(Effect.ignore, Effect.forkIn(scope))
            if (
              !ctx.assistantMessage.summary &&
              isOverflow({ cfg: ctx.cfg ?? (yield* config.get()), tokens: usage.tokens, model: ctx.model })
            ) {
              ctx.needsOverflowHandling = true
            }
            return
          }

          case "text-start":
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            ctx.retrying = false
            if (isMain) yield* status.set(ctx.sessionID, { type: "busy" })
            ctx.currentText = {
              id: PartID.ascending(),
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "text",
              text: "",
              time: { start: Date.now() },
              metadata: value.providerMetadata,
            }
            ctx.textPartPersisted = false
            if (!replaying) startSourceOutput(outputAttempt, ctx.actorExecution, ctx.currentText.messageID, ctx.currentText.id, value.id)
            return

          case "text-delta":
            if (!ctx.firstTokenAt) ctx.firstTokenAt = Date.now()
            if (!ctx.currentText) return
            if (!value.text) return
            if (!replaying) appendSourceOutput(outputAttempt, value.id, value.text)
            if (!ctx.textPartPersisted) {
              ctx.textPartPersisted = true
              yield* session.updatePart(ctx.currentText)
              ctx.stepPartIds.push(ctx.currentText.id)
            }
            ctx.currentText.text += value.text
            checkTextNgram(value.text)
            if (value.providerMetadata) ctx.currentText.metadata = value.providerMetadata
            yield* session.updatePartDelta({
              sessionID: ctx.currentText.sessionID,
              messageID: ctx.currentText.messageID,
              partID: ctx.currentText.id,
              field: "text",
              delta: value.text,
            })
            return

          case "text-end":
            if (!ctx.currentText) return
            // oxlint-disable-next-line no-self-assign -- reactivity trigger
            ctx.currentText.text = ctx.currentText.text
            const textTransform = { text: ctx.currentText.text }
            const textIdentity = { sessionID: ctx.sessionID, messageID: ctx.assistantMessage.id, partID: ctx.currentText.id }
            if (outputAttempt && !replaying) plugin.requestEmptyTextTransform?.(textTransform)
            ctx.currentText.text = (yield* plugin.trigger("experimental.text.complete", textIdentity, textTransform)).text
            if (!replaying) endSourceOutput(outputAttempt, value.id, ctx.currentText.text,
              () => plugin.readEmptyTextTransform?.(textTransform, textIdentity) === true)
            {
              const end = Date.now()
              ctx.currentText.time = { start: ctx.currentText.time?.start ?? end, end }
            }
            if (value.providerMetadata) ctx.currentText.metadata = value.providerMetadata
            if (ctx.currentText.text) {
              yield* session.updatePart(ctx.currentText)
            }
            ctx.currentText = undefined
            return

          case "finish":
            if (!replaying) noteSourceOutputFinish(outputAttempt)
            ctx.retrySafe = false
            yield* commitManagedAttempt()
            return

          default:
            slog.info("unhandled", { event: value.type, value })
            return
        }
      })

      const cleanup = Effect.fn("SessionProcessor.cleanup")(function* () {
        if (observedStep) observedStep.closed = true
        yield* closeTools()
        if (ctx.currentText) {
          if (ctx.currentText.text) {
            const end = Date.now()
            ctx.currentText.time = { start: ctx.currentText.time?.start ?? end, end }
            yield* session.updatePart(ctx.currentText)
          }
          ctx.currentText = undefined
        }

        for (const part of Object.values(ctx.reasoningMap)) {
          const end = Date.now()
          yield* session.updatePart({
            ...part,
            time: { start: part.time.start ?? end, end },
          })
        }
        ctx.reasoningMap = {}

        for (const toolCallID of Object.keys(ctx.toolcalls)) {
          yield* updateToolCall(toolCallID, (part) => {
            if (part.state.status !== "pending" && part.state.status !== "running") return part
            return { ...part, state: MessageV2.abortedToolState(
              part.state, toolGate.wasCancelled(toolCallID) ? FAIL_CASCADE_MESSAGE : undefined,
            ) }
          })
        }
        ctx.toolcalls = {}
        // Second pass, DB-driven. The loop above can only see calls this process
        // still holds in `ctx.toolcalls`, so a call whose registration lost the race
        // with teardown, or that arrived after the map was cleared, or whose
        // `readToolCall` lookup missed, keeps its persisted `running` status forever
        // — the transcript then shows a tool call that will never finish. Every tool
        // part of THIS assistant message belongs to the turn being torn down here,
        // so any part still `pending`/`running` is unfinalized by definition.
        // Idempotent: the pass above already rewrote the tracked ones.
        for (const part of yield* Effect.sync(() => MessageV2.parts(ctx.assistantMessage.id))) {
          if (part.type !== "tool") continue
          if (part.state.status !== "pending" && part.state.status !== "running") continue
          yield* session.mutateToolPart({ sessionID: part.sessionID, messageID: part.messageID, partID: part.id }, (current) => {
            if (!current || (current.state.status !== "pending" && current.state.status !== "running")) return current
            return { ...current, state: MessageV2.abortedToolState(
              current.state, toolGate.wasCancelled(current.callID) ? FAIL_CASCADE_MESSAGE : undefined,
            ) }
          })
        }
        // 有 error = 没完成 = 留在 /recovery 候选集里。不管错误类型(瞬态/终态/用户中止),
        // 只要消息带 error,就不写 completed,让 resume 端点能找到它。
        // 正常完成(无 error)才标记 completed。
        if (!ctx.assistantMessage.error) {
          ctx.assistantMessage.time.completed = Date.now()
        }
        yield* session.updateMessage(ctx.assistantMessage)
      })

      const halt = Effect.fn("SessionProcessor.halt")(function* (e: unknown) {
        slog.error("process", { error: errorMessage(e), stack: e instanceof Error ? e.stack : undefined })
        const error = parse(e)
        if (MessageV2.ContextOverflowError.isInstance(error)) {
          ctx.needsOverflowHandling = true
          yield* bus.publish(Session.Event.Error, { sessionID: ctx.sessionID, error })
          return
        }
        ctx.assistantMessage.error = error
        yield* bus.publish(Session.Event.Error, {
          sessionID: ctx.assistantMessage.sessionID,
          error: ctx.assistantMessage.error,
        })
        // Do NOT status.set(idle) here. halt runs before cleanup and the outer
        // work ensuring (orphan sweep). Publishing idle first lets the client
        // finish/unsubscribe, then the sweep's part.updated lands on the NEXT
        // turn's subscription (RL-ORPHAN-D01). Idle is published by
        // SessionRunState onIdle after the work Effect (including ensuring)
        // exits.
      })

      const process = Effect.fn("SessionProcessor.process")(function* (streamInput: LLM.StreamInput, rebuildAlternative?: (context: ManagedProvider.OwnedAlternativeRebuild) => Effect.Effect<LLM.StreamInput, unknown>) {
        slog.info("process")
        replaying = false
        outputAttempt = streamInput.sourceAttempt
        const actor = ctx.actorExecution
        const originalTurn = isMain && actor?.actorID === "main" && actor.sessionID === ctx.sessionID && actor.rootOperationID && actor.innerTurnID
          ? { sessionID: actor.sessionID, rootOperationID: actor.rootOperationID, actorID: actor.actorID, executionRevision: actor.executionRevision, innerTurnID: actor.innerTurnID } : undefined
        ctx.needsOverflowHandling = false
        const cfg = input.cfg ?? (yield* config.get())
        ctx.shouldBreak = cfg.experimental?.continue_loop_on_deny !== true
        const retryConfig = SessionRetry.resolve(cfg, streamInput.model.providerID)
        const interactiveRetryBudget = streamInput.interactiveRetryBudget ?? SessionRetry.createInteractiveBudget()
        ctx.retrying = false
        ctx.retryWaiter = undefined
        ctx.superseded = false
        // A newer input supersedes the step only while it is failing: once the input
        // waits, the step gives way at its current retry or at the next one.
        const supersede = (awaitNewInput: Effect.Effect<void>) =>
          Effect.gen(function* () {
            yield* awaitNewInput
            if (!ctx.retrying) {
              const waiter = yield* Deferred.make<void>()
              ctx.retryWaiter = waiter
              yield* Deferred.await(waiter)
            }
            ctx.superseded = true
          })

        let cleanIncomplete = false
        let recoveryAttempt = 0
        const managedRetry = (oneAttempt: Effect.Effect<void, unknown>, attempt: number): Effect.Effect<void, unknown> =>
          oneAttempt.pipe(Effect.catch((error: unknown) => Effect.gen(function* () {
            if (!ctx.retrySafe) return yield* Effect.fail(error)
            const terminal = yield* Effect.promise(() => originalTurn
              ? ManagedProvider.recordOwnedAnthropicTerminal(originalTurn, error)
              : Promise.resolve("not_eligible" as const))
            if (terminal === "stopped" && originalTurn && rebuildAlternative) {
              const alternative = yield* Effect.promise(() => ManagedProvider.prepareOwnedAlternative(originalTurn))
              if (!alternative || !alternative.current()) return yield* Effect.fail(error)
              const rebuilt = yield* rebuildAlternative(alternative)
              if (!alternative.current()) return yield* Effect.fail(error)
              const previous = { model: ctx.model, providerID: ctx.assistantMessage.providerID, modelID: ctx.assistantMessage.modelID, variant: ctx.assistantMessage.variant }
              ctx.model = rebuilt.model
              ctx.assistantMessage.providerID = rebuilt.model.providerID
              ctx.assistantMessage.modelID = rebuilt.model.id
              ctx.assistantMessage.variant = rebuilt.user.model.variant
              yield* session.updateMessage(ctx.assistantMessage)
              if (!(yield* Effect.promise(() => ManagedProvider.activateOwnedAlternative(alternative)))) {
                ctx.model = previous.model
                ctx.assistantMessage.providerID = previous.providerID
                ctx.assistantMessage.modelID = previous.modelID
                ctx.assistantMessage.variant = previous.variant
                yield* session.updateMessage(ctx.assistantMessage)
                return yield* Effect.fail(error)
              }
              yield* session.updatePart({ id: PartID.ascending(), messageID: ctx.assistantMessage.id, sessionID: ctx.sessionID,
                type: "text", text: `Switched from ${previous.providerID}/${previous.modelID} to ${rebuilt.model.providerID}/${rebuilt.model.id}: the first model's API key was rejected before any output.`,
                synthetic: true, metadata: { origin: { kind: "model_switch" } }, time: { start: Date.now(), end: Date.now() } })
              streamInput = rebuilt
              return yield* oneAttempt
            }
            if (terminal !== "not_eligible") return yield* Effect.fail(error)
            const classification = SessionRetry.decide(parse(error), "stream", "live-step")
            if (!classification.retryable) return yield* Effect.fail(error)
            const decision = yield* Effect.promise(() => ManagedProvider.recordSessionAttempt(ctx.sessionID, error, actor?.actorID, actor?.executionRevision))
            if (!decision.retry) return yield* Effect.fail(error)
            const base = decision.rotated ? 0 : Math.min(500 * 2 ** attempt, 5_000)
            const delay = decision.rotated ? 0 : Math.max(Math.max(1, Math.round(base * (0.75 + Math.random() * 0.5))), decision.retryAfterMs ?? 0)
            if (!ManagedProvider.reserveRetryWait(ctx.sessionID, actor?.actorID, actor?.executionRevision, delay)) return yield* Effect.fail(error)
            yield* recordRetry({ phase: "stream", scope: "live-step", kind: classification.kind, message: classification.message, hostCode: classification.hostCode, next: Date.now() + delay })
            if (isMain) yield* status.setRetry(ctx.sessionID, { type: "retry", attempt: attempt + 1, phaseAttempt: attempt + 1,
              message: decision.rotated ? "Trying another eligible provider account" : "Retrying the same provider account", next: Date.now() + delay,
              phase: "stream", scope: "live-step", hostCode: classification.hostCode })
            yield* Effect.sleep(delay)
            return yield* managedRetry(oneAttempt, attempt + 1)
          })))

        return yield* Effect.gen(function* () {
          yield* Effect.gen(function* () {
            stepRecovery?.abort()
            stepRecovery = createStepRecoveryRecorder()
            const attemptRecorder = stepRecovery
            if (++recoveryAttempt !== 1) attemptRecorder.invalidate()
            ctx.currentText = undefined
            ctx.reasoningMap = {}
            ctx.stepPartIds = []
            ctx.toolcalls = {}
            timingCaptures.clear(); gcfCandidates.clear(); invalidGcfCalls.clear()
            if (toolsClosed) {
              toolScope = yield* Scope.make()
              toolsClosed = false
              ready.clear()
            }
            ctx.retrySafe = true
            handledFinishStep = false
            completionSettled = false
            pendingFinishObservation = undefined
            ctx.startedBusy = false
            ctx.textNgramRepeat = false
            ctx.textNgramMonitor = createTextNgramMonitor()
            const guardedTools = ManagedProvider.enabled() ? Object.fromEntries(Object.entries(streamInput.tools).map(([name, tool]) => {
              if (!tool.execute) return [name, tool]
              const execute = tool.execute
              const guarded = { ...tool, execute: (...args: Parameters<typeof execute>) => {
                const callID = (args[1] as { toolCallId?: string } | undefined)?.toolCallId ?? ""
                const token = attemptRecorder.enter(callID)
                return (async () => {
                  let succeeded = false
                  try {
                    const signal = (args[1] as { abortSignal?: AbortSignal } | undefined)?.abortSignal
                    if (signal?.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError")
                    if (token && !attemptRecorder.isCurrent(token)) attemptRecorder.invalidate()
                    const result = await execute.apply(tool, args)
                    succeeded = !signal?.aborted
                    return result
                  } finally { attemptRecorder.finish(token, succeeded) }
                })()
              } }
              transferGuardedToolDefinition(tool, guarded)
              return [name, guarded]
            })) as LLM.StreamInput["tools"] : streamInput.tools
            const evidenceActor = ctx.actorExecution, managedEvidence = ManagedProvider.enabled()
            const evidenceTurn = evidenceActor?.rootOperationID && evidenceActor.innerTurnID
              ? { sessionID: evidenceActor.sessionID, rootOperationID: evidenceActor.rootOperationID, actorID: evidenceActor.actorID, executionRevision: evidenceActor.executionRevision, innerTurnID: evidenceActor.innerTurnID } : undefined
            const evidenceScope = evidenceActor ? ManagedProvider.currentScope(ctx.sessionID, evidenceActor.actorID, evidenceActor.executionRevision) : undefined
            const evidenceCurrent = () => !managedEvidence || (!!evidenceTurn && !!evidenceScope && ctx.actorExecution === evidenceActor &&
              isAdmittedToolTimingTurn(evidenceActor, evidenceTurn) && ManagedProvider.currentScope(ctx.sessionID, evidenceTurn.actorID, evidenceTurn.executionRevision) === evidenceScope &&
              ManagedProvider.currentToolViewRoute(ctx.sessionID, evidenceTurn.actorID, evidenceTurn.executionRevision)?.rootOperationID === evidenceTurn.rootOperationID)
            const evidenceOwner = { id: ctx.assistantMessage.id, sessionID: ctx.assistantMessage.sessionID, parentID: ctx.assistantMessage.parentID,
              created: ctx.assistantMessage.time.created, actorID: evidenceActor?.actorID ?? ctx.assistantMessage.agentID ?? "main",
              ...(managedEvidence ? { executionRevision: evidenceTurn?.executionRevision ?? -1 } : {}) }
            const evidenceSave = (message: MessageV2.Assistant) => bridge.promise(session.updateContextEvidence(message, evidenceOwner, evidenceCurrent))
            const stream = llm.stream({
              ...streamInput,
              tools: guardedTools,
              contextSequence: { attempt: recoveryAttempt, transform: 0 },
              onNativeTextDelivery: nativeTextDeliveryWriter(ctx.assistantMessage, evidenceSave, () => slog.warn("native text delivery evidence unavailable"), evidenceCurrent),
              onNativeMembership: nativeMembershipDeliveryWriter(ctx.assistantMessage, evidenceSave, () => slog.warn("native membership evidence unavailable"), evidenceCurrent),
              onContextFrame: contextFrameWriter(ctx.assistantMessage, evidenceSave, () => slog.warn("context inspection persistence unavailable"), evidenceCurrent),
              interactiveRetryBudget,
              assistantMessageID: ctx.assistantMessage.id,
              // Request-ladder retries must write SessionStatus (not just bus events):
              // GET /session/status serves the same map after renderer reload.
              onRetryStatus: (info) =>
                Effect.gen(function* () {
                  yield* recordRetry({
                    phase: info.phase ?? "request",
                    scope: info.scope ?? "request",
                    kind: info.kind,
                    message: info.message,
                    hostCode: info.hostCode,
                    next: info.next,
                  })
                  if (!isMain) return
                  yield* status.setRetry(ctx.sessionID, {
                    type: "retry",
                    attempt: info.attempt,
                    phaseAttempt: info.attempt,
                    message: info.message,
                    next: info.next,
                    phase: info.phase,
                    scope: info.scope,
                    hostCode: info.hostCode,
                  })
                }),
            })

            yield* stream.pipe(
              Stream.tap(handleEvent),
              Stream.takeUntil(() => ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked),
              Stream.runDrain,
              Effect.onExit((exit) => Effect.sync(() => { if (Exit.isFailure(exit)) attemptRecorder.abort() })),
              Effect.ensuring(closeTools()),
            )
            if (ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked || ctx.assistantMessage.error || !ctx.actorExecution) {
              attemptRecorder.abort()
            } else {
              for (const declaration of attemptRecorder.drain()) {
                try {
                  if (!(yield* Effect.promise(() => Promise.all(declaration.current.map((check) => check())).catch(() => [false]))).every(Boolean)) continue
                  const capability = createStepRecoveryCapability(declaration.binding, ctx.actorExecution)
                  if (capability) publishStepRecovery(capability, declaration.members)
                } catch { /* Optional proof cannot change the tool result. */ }
              }
            }

            // Reset the step's content but keep the message identity: removing the
            // message and rebuilding it under the same ID is indistinguishable, on
            // the wire, from a late frame of an already-deleted entity, so consumers
            // that order by events discard the retried content for good.
            const resetStep = Effect.fnUntraced(function* (parts: MessageV2.Part[]) {
              for (const part of parts) {
                if (part.type === "retry") continue
                yield* session.removePart({
                  sessionID: ctx.sessionID,
                  messageID: ctx.assistantMessage.id,
                  partID: part.id,
                })
              }
              ctx.assistantMessage.finish = undefined
              ctx.assistantMessage.rawFinish = undefined
              ctx.assistantMessage.error = undefined
              ctx.assistantMessage.time.completed = undefined
              ctx.stepPartIds = []
              yield* session.updateMessage(ctx.assistantMessage)
            })

            yield* settleManagedCompletion(streamInput.user)
            if (!ctx.needsOverflowHandling && !ctx.textNgramRepeat && !ctx.blocked && !ctx.assistantMessage.error) {
              const parts = MessageV2.parts(ctx.assistantMessage.id)
              // A clean drain without a handled finish has an unknown outcome.
              // Preserve partial output and fence retry/failover before Host callbacks.
              if (!handledFinishStep) {
                cleanIncomplete = true
                ctx.retrySafe = false
                slog.warn("stream ended without finish signal; outcome unknown")
                return yield* Effect.fail(
                  new MessageV2.StreamIncompleteError({
                    message: "The model stream ended before the provider sent a finish signal",
                  }),
                )
              }
              // Empty output is a failed step. Raise InvalidOutputError so
              // SessionRetry.policy (decide → persistent server budget) owns the
              // retry instead of a parallel counter. Clear the empty turn's parts so
              // it never sits in session history, keeping the assistant identity the
              // retry writes into.
              // Length finish is owned by autoContinueOutputLength; json_schema
              // empty output is owned by structured-output retry.
              // Only replay when the stream is retrySafe (no tool side effects) and
              // the turn has no tool residue — otherwise the runLoop's
              // dropEmptyAssistant starts a fresh step instead of replaying.
              const classification = classifyAssistantStep({
                phase: "after-process",
                lastUser: streamInput.user,
                assistant: ctx.assistantMessage,
                parts,
                processResult: "stop",
              })
              const hasToolParts = parts.some((part) => part.type === "tool")
              if (
                classification.type === "invalid" &&
                ctx.assistantMessage.finish !== "length" &&
                streamInput.user.format?.type !== "json_schema" &&
                ctx.retrySafe &&
                !hasToolParts
              ) {
                slog.info("invalid output, dropping empty turn", {
                  reason: classification.reason,
                  finish: ctx.assistantMessage.finish,
                })
                yield* resetStep(parts)
                return yield* Effect.fail(new MessageV2.InvalidOutputError({ message: classification.reason }))
              }
            }
          }).pipe(
            Effect.onInterrupt(() =>
              Effect.gen(function* () {
                stepRecovery?.abort()
                invalidateSourceOutput(outputAttempt)
                if (ctx.superseded) return
                aborted = true
                if (!ctx.assistantMessage.error) {
                  yield* halt(new DOMException("Aborted", "AbortError"))
                }
              }),
            ),
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.fail(Cause.squash(cause)),
            ),
            Effect.tapError(() =>
              Effect.gen(function* () {
                if (!ctx.retrySafe) return
                for (const partId of ctx.stepPartIds) {
                  yield* session.removePart({
                    sessionID: ctx.sessionID,
                    messageID: ctx.assistantMessage.id,
                    partID: partId,
                  })
                }
                ctx.stepPartIds = []
              }),
            ),
            (attemptEffect) => ManagedProvider.enabled() ? managedRetry(attemptEffect, 0) : attemptEffect.pipe(Effect.retry(
              SessionRetry.policy({
                parse,
                phase: "stream",
                interactiveBudget: interactiveRetryBudget,
                // System-spawned agents run in the background where no user can
                // cancel them, so their empty output gets a bounded budget instead
                // of the persistent server budget.
                budget: (decision, error) =>
                  systemAgent && MessageV2.InvalidOutputError.isInstance(error)
                    ? retryConfig.unknown
                    : SessionRetry.budgetFor(retryConfig, decision),
                jitterRatio: retryConfig.jitterRatio,
                // The Host owns managed retry/failover; until that join is
                // restored, surface the failure instead of replaying natively.
                replaySafe: () => !ManagedProvider.enabled() && ctx.retrySafe,
                silentRetry: (error) => isMain && SessionRetry.isGptServerOverloadedError(error),
                onTerminal: (decision) =>
                  isMain && decision.uiMessage
                    ? status.set(ctx.sessionID, { type: "notice", message: decision.uiMessage })
                    : Effect.void,
                set: (info) =>
                  Effect.gen(function* () {
                    yield* recordRetry({
                      phase: info.phase,
                      scope: info.scope,
                      kind: info.kind,
                      message: info.message,
                      hostCode: info.hostCode,
                      next: info.next,
                    })
                    let attempt = info.attempt
                    if (isMain) {
                      attempt = yield* status.setRetry(ctx.sessionID, {
                        type: "retry",
                        attempt: info.attempt,
                        phaseAttempt: info.attempt,
                        message: info.message,
                        next: info.next,
                        phase: info.phase,
                        scope: info.scope,
                        hostCode: info.hostCode,
                      })
                    }
                    yield* bus
                      .publish(Session.Event.RetryAttempt, {
                        sessionID: ctx.sessionID,
                        messageID: ctx.assistantMessage.id,
                        attempt,
                        phaseAttempt: info.attempt,
                        maxAttempts: info.maxAttempts,
                        phase: info.phase,
                        kind: info.kind,
                        scope: info.scope,
                        reason: info.message,
                        nextDelayMs: Math.max(0, info.next - Date.now()),
                        hostCode: info.hostCode,
                      })
                      .pipe(Effect.ignore)
                  }),
              }),
            )),
            (retried) => (input.awaitNewInput ? Effect.raceFirst(retried, supersede(input.awaitNewInput)) : retried),
            Effect.catch((e) =>
              Effect.gen(function* () {
                // Codex-style auto-resume: after tools have fully completed, a
                // retryable transport failure (timeout / disconnect / 5xx) must NOT
                // stamp a terminal ErrorCard. Replaying this stream is unsafe
                // (tools already ran), but the outer runLoop can start the next
                // step from session history that already carries tool results —
                // equivalent to an automatic Resume of the next sampling call.
                // User abort (cancel / interrupt) is terminal: never auto-resume.
                if (aborted) {
                  yield* halt(e)
                  return
                }
                if (!ctx.retrySafe) {
                  const decision = SessionRetry.decide(parse(e), "stream", "live-step")
                  const parts = MessageV2.parts(ctx.assistantMessage.id)
                  const toolParts = parts.filter((p) => p.type === "tool")
                  const hasFinalText = parts.some(
                    (p) => p.type === "text" && !p.synthetic && !p.ignored && p.text.trim().length > 0,
                  )
                  if (
                    !cleanIncomplete && shouldAutoResumeAfterTools({
                      retrySafe: ctx.retrySafe,
                      decision,
                      toolParts,
                      aborted,
                      finish: ctx.assistantMessage.finish,
                      rawFinish: ctx.assistantMessage.rawFinish,
                      hasFinalText,
                    })
                  ) {
                    slog.info("auto-resume after tools", {
                      kind: decision.kind,
                      message: decision.message,
                      tools: toolParts.length,
                    })
                    yield* recordRetry({
                      phase: "stream",
                      scope: "live-step",
                      kind: decision.kind,
                      message: decision.message,
                      hostCode: decision.hostCode,
                      next: Date.now(),
                    })
                    if (isMain) {
                      yield* status
                        .setRetry(ctx.sessionID, {
                          type: "retry",
                          attempt: 1,
                          phaseAttempt: 1,
                          message: decision.message,
                          next: Date.now(),
                          phase: "stream",
                          scope: "live-step",
                          hostCode: decision.hostCode,
                        })
                        .pipe(Effect.ignore)
                    }
                    // Keep tool parts; mark the step as tool-calls so classify
                    // continues. Do NOT write assistant.error (that is terminal).
                    ctx.assistantMessage.finish = "tool-calls"
                    ctx.assistantMessage.rawFinish = undefined
                    ctx.assistantMessage.error = undefined
                    yield* session.updateMessage(ctx.assistantMessage)
                    return
                  }
                  // Trailing transport failure after a completed answer (terminal
                  // finish + final text): keep the answer, do not stamp an ErrorCard,
                  // do not reopen. Fall through to a clean "stop" via the tail below.
                  // Raw-less `other` (stream incomplete) is not terminal.
                  if (isTerminalFinish(ctx.assistantMessage) && hasFinalText) {
                    slog.info("ignore trailing transport error after finished turn", {
                      kind: decision.kind,
                      message: decision.message,
                      finish: ctx.assistantMessage.finish,
                    })
                    ctx.assistantMessage.error = undefined
                    yield* session.updateMessage(ctx.assistantMessage)
                    return
                  }
                }
                yield* halt(e)
              }),
            ),
            Effect.ensuring(cleanup()),
          )

          if (ctx.superseded) {
            // The superseded step keeps only its retry record, which stays visible
            // until the turn succeeds; it is complete so no orphan sweep aborts it.
            for (const partId of ctx.stepPartIds) {
              yield* session.removePart({
                sessionID: ctx.sessionID,
                messageID: ctx.assistantMessage.id,
                partID: partId,
              })
            }
            ctx.stepPartIds = []
            ctx.assistantMessage.finish = undefined
            ctx.assistantMessage.rawFinish = undefined
            ctx.assistantMessage.error = undefined
            ctx.assistantMessage.time.completed = Date.now()
            yield* session.updateMessage(ctx.assistantMessage)
            if (isMain) yield* status.set(ctx.sessionID, { type: "busy" })
            slog.info("retry superseded by new input", { attempt: ctx.retryPart?.attempt })
            invalidateSourceOutput(outputAttempt)
            return "superseded"
          }
          if (ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked || ctx.assistantMessage.error || ctx.superseded)
            invalidateSourceOutput(outputAttempt)
          else finishSourceOutputStream(outputAttempt)
          if (ctx.needsOverflowHandling) return "overflow"
          if (ctx.textNgramRepeat) return "text-repeat"
          if (ctx.blocked || ctx.assistantMessage.error) return "stop"
          return "continue"
        })
      })

      const replay = Effect.fn("SessionProcessor.replay")(function* (input: ReplayInput) {
        replaying = true
        handledFinishStep = false
        completionSettled = false
        pendingFinishObservation = undefined
        invalidateSourceOutput(outputAttempt)
        slog.info("replay", { toolCalls: input.toolCalls.length, finish: input.finishReason })
        ctx.needsOverflowHandling = false
        ctx.shouldBreak = (yield* config.get()).experimental?.continue_loop_on_deny !== true

        const ctrl = new AbortController()

        const emptyUsage = {
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
        }

        return yield* Effect.gen(function* () {
          yield* Effect.gen(function* () {
            ctx.currentText = undefined
            ctx.reasoningMap = {}

            // Mirror a real model call: start → start-step → reasoning → text →
            // (tool-input-start → tool-call → execute → tool-result)* → finish-step
            yield* handleEvent({ type: "start" } as StreamEvent)
            yield* handleEvent({
              type: "start-step",
              request: {},
              warnings: [],
            } as unknown as StreamEvent)

            const selectionNote = input.selection
              ? `[max mode] selected candidate ${input.selection.winner + 1} of ${input.selection.total}`
              : undefined
            const reasoningText = [selectionNote, input.reasoning].filter(Boolean).join("\n\n")

            if (reasoningText) {
              const rid = "reasoning-replay"
              const backdatedStart = input.thinkingMs ? Date.now() - input.thinkingMs : undefined
              yield* handleEvent({
                type: "reasoning-start",
                id: rid,
                providerMetadata: input.reasoningMetadata,
                ...(backdatedStart ? { time: { start: backdatedStart } } : {}),
              } as unknown as StreamEvent)
              yield* handleEvent({
                type: "reasoning-delta",
                id: rid,
                text: reasoningText,
                providerMetadata: input.reasoningMetadata,
              } as unknown as StreamEvent)
              yield* handleEvent({
                type: "reasoning-end",
                id: rid,
                providerMetadata: input.reasoningMetadata,
              } as unknown as StreamEvent)
            }

            if (input.text) {
              const tid = "text-replay"
              yield* handleEvent({
                type: "text-start",
                id: tid,
                providerMetadata: input.textMetadata,
              } as unknown as StreamEvent)
              yield* handleEvent({
                type: "text-delta",
                id: tid,
                text: input.text,
                providerMetadata: input.textMetadata,
              } as unknown as StreamEvent)
              yield* handleEvent({
                type: "text-end",
                id: tid,
                providerMetadata: input.textMetadata,
              } as unknown as StreamEvent)
            }

            for (const call of input.toolCalls) {
              if (ctx.needsOverflowHandling || ctx.blocked) break
              yield* handleEvent({
                type: "tool-input-start",
                id: call.toolCallId,
                toolName: call.toolName,
                providerMetadata: call.providerMetadata,
              } as unknown as StreamEvent)
              yield* handleEvent({
                type: "tool-call",
                toolCallId: call.toolCallId,
                toolName: call.toolName,
                input: call.input,
                providerMetadata: call.providerMetadata,
              } as unknown as StreamEvent)

              const t = input.tools[call.toolName]
              if (!t || !t.execute) {
                yield* handleEvent({
                  type: "tool-error",
                  toolCallId: call.toolCallId,
                  toolName: call.toolName,
                  input: call.input,
                  error: new Error(`Tool "${call.toolName}" has no executor`),
                } as unknown as StreamEvent)
                continue
              }

              const outcome = yield* Effect.tryPromise({
                try: () =>
                  t.execute!(call.input, {
                    toolCallId: call.toolCallId,
                    messages: input.messages,
                    abortSignal: ctrl.signal,
                  }),
                catch: (e) => e,
              }).pipe(
                Effect.map((output) => ({ ok: true as const, output })),
                Effect.catch((error) => Effect.succeed({ ok: false as const, error })),
              )

              if (!outcome.ok) {
                yield* handleEvent({
                  type: "tool-error",
                  toolCallId: call.toolCallId,
                  toolName: call.toolName,
                  input: call.input,
                  error: outcome.error,
                } as unknown as StreamEvent)
              } else {
                // The execute closure resolves with { title, metadata, output, attachments }.
                // Feeding it through the tool-result handler completes the part
                // (unless execute already completed it on abort — completeToolCall
                // is a no-op once the part is no longer "running").
                yield* handleEvent({
                  type: "tool-result",
                  toolCallId: call.toolCallId,
                  toolName: call.toolName,
                  input: call.input,
                  output: outcome.output,
                } as unknown as StreamEvent)
              }
            }

            yield* handleEvent({
              type: "finish-step",
              usage: input.usage ?? emptyUsage,
              finishReason: input.finishReason,
              rawFinishReason: input.rawFinishReason,
              providerMetadata: input.providerMetadata,
            } as unknown as StreamEvent)
            // Account for ensemble overhead (losing candidates + judge): real
            // spend that consumed no context. Add it to cost + agent metrics +
            // a supplementary ModelCall metric, but NOT to message.tokens (set
            // by the finish-step above from the winner only) so context
            // estimators stay honest.
            if (
              input.overhead &&
              (input.overhead.cost > 0 || input.overhead.tokensIn > 0 || input.overhead.tokensOut > 0)
            ) {
              ctx.assistantMessage.cost += input.overhead.cost
              yield* session.updateMessage(ctx.assistantMessage)
              if (ctx.agentMetrics) {
                ctx.agentMetrics.tokens_in += input.overhead.tokensIn
                ctx.agentMetrics.tokens_out += input.overhead.tokensOut
              }
              yield* bus
                .publish(Metrics.ModelCall, {
                  sessionID: ctx.sessionID,
                  finish_reason: "max-mode-overhead",
                  latency_ms: 0,
                  cached_read_tokens: 0,
                  model_id: ctx.model.id,
                  provider: ctx.model.providerID,
                  total_tokens_in: input.overhead.tokensIn,
                  total_tokens_out: input.overhead.tokensOut,
                })
                .pipe(Effect.ignore)
            }
            const parent = yield* Effect.sync(() => {
              try { return MessageV2.get({ sessionID: ctx.sessionID, messageID: ctx.assistantMessage.parentID })?.info }
              catch { return undefined }
            })
            yield* settleManagedCompletion(parent?.role === "user" && parent.sessionID === ctx.sessionID ? parent : undefined)
          }).pipe(
            Effect.onInterrupt(() =>
              Effect.gen(function* () {
                aborted = true
                // Propagate cancellation into the currently-executing tool so a
                // long-running winner tool call is actually interrupted.
                ctrl.abort()
                if (!ctx.assistantMessage.error) {
                  yield* halt(new DOMException("Aborted", "AbortError"))
                }
              }),
            ),
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.fail(Cause.squash(cause)),
            ),
            Effect.catch(halt),
            Effect.ensuring(cleanup()),
          )

          if (ctx.needsOverflowHandling) return "overflow"
          if (ctx.blocked || ctx.assistantMessage.error) return "stop"
          return "continue"
        })
      })

      return {
        toolGate,
        stepRecovery: () => stepRecovery,
        registerTool: (name) => { localTools.add(name) },
        executeTool,
        executeDelegatedTool,
        bindDelegationSignal,
        delegationSignal,
        recordBrowserSnapshotCandidate,
        recordGcfCandidate,
        observedCaptureMembership,
        beginArgumentResolution,
        checkArgumentResolution,
        get message() {
          return ctx.assistantMessage
        },
        updateToolCall,
        completeToolCall,
        process,
        replay,
      } satisfies Handle
    })

    return Service.of({ create })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Session.defaultLayer),
    Layer.provide(Agent.defaultLayer),
    Layer.provide(LLM.defaultLayer),
    Layer.provide(Permission.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(SessionSummary.defaultLayer),
    Layer.provide(SessionStatus.defaultLayer),
    Layer.provide(Bus.layer),
    Layer.provide(Config.defaultLayer),
  ),
)

export * as SessionProcessor from "./processor"
