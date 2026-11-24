import { transferGuardedToolDefinition } from "./tool-definition-provenance"
import { startSourceOutput, appendSourceOutput, endSourceOutput, finishSourceOutputStep, noteSourceOutputFinish, finishSourceOutputStream, invalidateSourceOutput, type SourceAttemptHandle } from "./closed-source-provenance"
import { createStepRecoveryRecorder } from "./step-recovery-recorder"
import { createStepRecoveryCapability, publishStepRecovery } from "./step-recovery-owner"
import { emitMalformedDiagnostic, hasMalformedResponse, hasObservedMalformedResponse } from "@/provider/logging-transport"
import { captureToolTiming, isAdmittedToolTimingTurn, type ToolTimingCapture } from "./tool-timing"
import { contextFrameWriter, nativeTextDeliveryWriter, nativeMembershipDeliveryWriter } from "./context-inspection"
import type { ActorExecutionIdentity } from "@/actor/turn"
import { Cause, Deferred, Duration, Effect, Exit, Fiber, Layer, Context, Scope } from "effect"
import * as Stream from "effect/Stream"
import { Agent } from "@/agent/agent"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { Bus } from "@/bus"
import { Metrics } from "@/metrics"
import { Config } from "@/config"
import { Permission } from "@/permission"
import { Plugin } from "@/plugin"
import { Snapshot } from "@/snapshot"
import * as Session from "./session"
import { LLM } from "./llm"
import { MessageV2 } from "./message-v2"
import { gcfCandidate } from "./gcf-source-proof"
import { captureToolView, selectedToolViewProfile } from "./tool-view-source"
import { readTerminalAnsi } from "./tool-view-terminal-ansi"
import { readBashView } from "./tool-view-bash-proof"
import { readWebfetchHtml, readWebfetchMarkdown } from "../tool/webfetch-view"
import { readGrepRows } from './tool-view-grep'
import { readReadViewProfile } from './tool-view-read'
import { readStructuredSearch } from "./tool-view-structured-search"
import { isOverflow } from "./overflow"
import { PartID } from "./schema"
import type { SessionID } from "./schema"
import { SessionRetry } from "./retry"
import { SessionStatus } from "./status"
import { SessionSummary } from "./summary"
import type { Provider } from "@/provider"
import { Question } from "@/question"
import { errorMessage } from "@/util/error"
import { isRecoverableError } from "@/tool/recoverable"
import { getToolResultAttachments, getToolResultMetadata } from "@/tool/result-error"
import { Log } from "@/util"
import { isRecord } from "@/util/record"
import { createTextNgramMonitor, type TextNgramMonitor } from "./prompt/text-ngram-detection"
import { Flag } from "@/flag/flag"
import { ManagedProvider } from "@/acp/managed-provider"
import { takeSdkSuccessStep } from "@/provider/logging-transport"
import type { AccountSelection } from "@/provider/account-selection"
import { EffectBridge } from "@/effect"
import type { AppFileSystem } from "@mimo-ai/shared/filesystem"
import type { CaptureMembership } from "./capture-membership"
import { monitor as tryBestMonitor, type TryBestIncident } from "./try-best-detector"

const DOOM_LOOP_THRESHOLD = 3
const log = Log.create({ service: "session.processor" })

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

export type Result = "overflow" | "stop" | "continue" | "text-repeat"

export type Event = LLM.Event

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
  readonly beginArgumentResolution: (callID:string, expectedInput:unknown, target:'glob'|'grep', schemaSHA256:string, binding:object) => Effect.Effect<boolean>
  readonly checkArgumentResolution: (callID:string, expectedInput:unknown, binding:object, claim:boolean) => Effect.Effect<boolean>
  readonly recordGcfCandidate: (toolCallID: string, output: string, toonCurrent?:()=>boolean, outline?:{sidecar:string;current:()=>boolean}) => void
  readonly stepRecovery?: () => ReturnType<typeof createStepRecoveryRecorder> | undefined
  readonly observedCaptureMembership?: (toolCallID: string, expectedStepID?: string) => NonNullable<AppFileSystem.HistoryContext["observedCaptureMembership"]> | undefined
  readonly process: (streamInput: LLM.StreamInput, rebuildAlternative?: (context: ManagedProvider.OwnedAlternativeRebuild) => Effect.Effect<LLM.StreamInput, unknown>) => Effect.Effect<Result>
  /**
   * Replay a pre-selected candidate (max mode): synthesize the stream events a
   * real model call would have produced for the winner — reasoning, text, then
   * each tool call (input-start → call → execute → result) — and run them
   * through the same event handler used by `process`. Tool execution, snapshot
   * tracking, permission asks and metrics are all reused. Returns the same
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
  /** Immutable configuration captured by the admitting turn. */
  cfg?: Config.Info
}

export interface Interface {
  readonly create: (input: Input) => Effect.Effect<Handle>
}

type ToolCall = {
  partID: MessageV2.ToolPart["id"]
  messageID: MessageV2.ToolPart["messageID"]
  sessionID: MessageV2.ToolPart["sessionID"]
  done: Deferred.Deferred<void>
  freshTask?: boolean
}

interface ProcessorContext extends Input {
  toolcalls: Record<string, ToolCall>
  shouldBreak: boolean
  snapshot: string | undefined
  snapshotAttempted: boolean
  lazySnapshot: boolean
  snapshotRecoveryNotices: Set<string>
  stepStartPartID: PartID | undefined
  blocked: boolean
  needsOverflowHandling: boolean
  currentText: MessageV2.TextPart | undefined
  reasoningMap: Record<string, MessageV2.ReasoningPart>
  stepStartedAt: number | undefined
  firstTokenAt: number | undefined
  stepPartIds: PartID[]
  textNgramMonitor: TextNgramMonitor | undefined
  textNgramRepeat: boolean
}

type StreamEvent = Event

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionProcessor") {}

export const layer: Layer.Layer<
  Service,
  never,
  | Session.Service
  | Config.Service
  | Bus.Service
  | Snapshot.Service
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
    const snapshot = yield* Snapshot.Service
    const agents = yield* Agent.Service
    const llm = yield* LLM.Service
    const permission = yield* Permission.Service
    const plugin = yield* Plugin.Service
    const summary = yield* SessionSummary.Service
    const scope = yield* Scope.Scope
    const status = yield* SessionStatus.Service
    const bridge = yield* EffectBridge.make()

    const create = Effect.fn("SessionProcessor.create")(function* (input: Input) {
      const timingCaptures = new Map<string, ToolTimingCapture>()
      const gcfCandidates = new Map<string, {sha256:string;bytes:number;toonCurrent?:()=>boolean;outline?:{sidecar:string;current:()=>boolean}}>()
      const invalidGcfCalls = new Set<string>()
      let stepRecovery: ReturnType<typeof createStepRecoveryRecorder> | undefined
      let observedStep: { id: PartID; closed: boolean; admitted: boolean;
        binding?: Omit<CaptureMembership,"revision"|"coverage"|"members"> } | undefined
      const observedCaptureMembership: NonNullable<Handle["observedCaptureMembership"]> = (toolCallID, expectedStepID) => {
        const actor = input.actorExecution
        const step = observedStep
        if (!actor || !step || step.closed || (expectedStepID !== undefined && step.id !== expectedStepID) || !toolCallID || !actor.rootOperationID || !actor.innerTurnID || !session.changeCaptureMembership) return
        const host = ManagedProvider.currentActorHostContext(input.sessionID, actor.actorID, actor.executionRevision)
        const issuer = ManagedProvider.currentActorToolTimingIssuer(input.sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID)
        if (!host || !issuer) return
        let admittedAction: string | undefined
        const current = (allowClosed: boolean) => {
          const actual = ManagedProvider.currentActorHostContext(input.sessionID, actor.actorID, actor.executionRevision)
          return observedStep === step && (allowClosed || !step.closed) && !!actual
            && actual.accountID === host.accountID && actual.workspaceID === host.workspaceID && actual.chatID === host.chatID
            && ManagedProvider.currentActorToolTimingIssuer(input.sessionID, actor.actorID, actor.executionRevision, actor.rootOperationID!) === issuer
        }
        return Object.freeze({
          begin: async (history: AppFileSystem.HistoryContext, actionID: string) => {
            const origin = history.managedToolOrigin
            if (admittedAction || !current(false) || !origin || history.accountId !== host.accountID || history.workspaceId !== host.workspaceID
                || origin.host_chat_id !== host.chatID || origin.engine_session_id !== input.sessionID || origin.actor_id !== actor.actorID
                || origin.execution_revision !== actor.executionRevision || origin.root_operation_id !== actor.rootOperationID
                || origin.inner_turn_id !== actor.innerTurnID || origin.tool_call_id !== toolCallID || origin.assistant_message_id !== input.assistantMessage.id)
              throw new Error("capture membership authority unavailable")
            const binding = {version:1 as const, sessionID:input.sessionID, messageID:input.assistantMessage.id, stepID:step.id,
              accountID:host.accountID, hostChatID:host.chatID, workspaceID:host.workspaceID, actorID:actor.actorID,
              executionRevision:actor.executionRevision, rootOperationID:actor.rootOperationID!, innerTurnID:actor.innerTurnID!}
            if (step.binding && JSON.stringify(step.binding) !== JSON.stringify(binding)) throw new Error("capture membership scope changed")
            step.binding = binding
            await bridge.promise(session.changeCaptureMembership!(binding,{kind:"begin",actionID,toolCallID},() => current(false)))
            admittedAction = actionID
            step.admitted = true
            if (step.closed) {
              await bridge.promise(session.changeCaptureMembership!(binding,{kind:"seal"},() => current(true))).catch(() => undefined)
              throw new Error("capture membership step closed")
            }
          },
          complete: async (_history: AppFileSystem.HistoryContext, actionID: string, actionRef: string) => {
            if (admittedAction !== actionID || !step.binding || !current(true)) throw new Error("capture membership settlement unavailable")
            await bridge.promise(session.changeCaptureMembership!(step.binding,{kind:"complete",actionID,toolCallID,actionRef},() => current(true)))
          },
        })
      }
      const recordGcfCandidate = (toolCallID:string, output:string, toonCurrent?:()=>boolean, outline?:{sidecar:string;current:()=>boolean}) => {
        if (!toolCallID || invalidGcfCalls.has(toolCallID)) return
        if (gcfCandidates.has(toolCallID) || gcfCandidates.size >= 128) {
          gcfCandidates.delete(toolCallID); invalidGcfCalls.add(toolCallID); return
        }
        const candidate = gcfCandidate(output)
        if (candidate) gcfCandidates.set(toolCallID,{...candidate,toonCurrent,outline})
      }
      if (ManagedProvider.enabled() && !input.actorExecution) throw new Error("managed actor execution identity is unavailable")
      const lazySnapshot = ManagedProvider.enabled() && input.actorExecution !== undefined &&
        ManagedProvider.currentActorHostContext(input.sessionID, input.actorExecution.actorID, input.actorExecution.executionRevision) !== undefined
      const initialSnapshot = lazySnapshot ? undefined : yield* snapshot.track()
      const ctx: ProcessorContext = {
        assistantMessage: input.assistantMessage,
        sessionID: input.sessionID,
        model: input.model,
        agentMetrics: input.agentMetrics,
        actorExecution: input.actorExecution,
        toolcalls: {},
        shouldBreak: false,
        snapshot: initialSnapshot,
        snapshotAttempted: !lazySnapshot,
        lazySnapshot,
        snapshotRecoveryNotices: new Set(),
        stepStartPartID: undefined,
        blocked: false,
        needsOverflowHandling: false,
        currentText: undefined,
        reasoningMap: {},
        stepStartedAt: undefined,
        firstTokenAt: undefined,
        stepPartIds: [],
        textNgramMonitor: undefined,
        textNgramRepeat: false,
      }
      let aborted = false
      // Only the main agent owns session-level status. Subagents (explore,
      // general, checkpoint-writer, etc.) share the parent sessionID but their
      // run-state onIdle deliberately does NOT reset status (run-state.ts) — so
      // if a subagent's stream sets session status here, nothing ever clears it
      // and the TUI spinner stays spinning after the main agent has finished.
      const isMain = !ctx.assistantMessage.agentID || ctx.assistantMessage.agentID === "main"
      const slog = log.clone().tag("session.id", input.sessionID).tag("messageID", input.assistantMessage.id)

      const parse = (e: unknown) =>
        MessageV2.fromError(e, {
          providerID: ctx.model.providerID,
          aborted,
        })

      const tryBestConfig = (input.cfg ?? (yield* config.get())).experimental?.try_best
      const tryBest = Flag.MIMOCODE_ENABLE_TRY_BEST_HANDOFF
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

      const snapshotRecoveryNotice = Effect.fn("SessionProcessor.snapshotRecoveryNotice")(function* (
        phase: "step_finish" | "cleanup",
        reason: "final_capture_failed" | "patch_failed",
      ) {
        if (!ctx.lazySnapshot || ctx.snapshotRecoveryNotices.has(phase)) return
        ctx.snapshotRecoveryNotices.add(phase)
        yield* session.updatePart({
          id: PartID.ascending(),
          messageID: ctx.assistantMessage.id,
          sessionID: ctx.sessionID,
          type: "text",
          text: "The follow-up recovery snapshot could not be saved. Tool results already recorded are preserved; undo and diff coverage may be incomplete.",
          synthetic: true,
          metadata: { origin: { kind: "snapshot_recovery", phase, reason } },
          time: { start: Date.now(), end: Date.now() },
        })
      })

      const argumentResolutions = new Map<string,{partID:string;input:string;binding:object;claimed:boolean}>()
      const settleToolCall = Effect.fn("SessionProcessor.settleToolCall")(function* (toolCallID: string) {
        argumentResolutions.delete(toolCallID)
        const done = ctx.toolcalls[toolCallID]?.done
        delete ctx.toolcalls[toolCallID]
        if (done) yield* Deferred.succeed(done, undefined).pipe(Effect.ignore)
      })

      const readToolCall = Effect.fn("SessionProcessor.readToolCall")(function* (toolCallID: string) {
        const call = ctx.toolcalls[toolCallID]
        if (!call) return
        const part = yield* session.getPart({
          partID: call.partID,
          messageID: call.messageID,
          sessionID: call.sessionID,
        })
        if (!part || part.type !== "tool") {
          delete ctx.toolcalls[toolCallID]
          return
        }
        return { call, part }
      })

      const beginArgumentResolution = Effect.fn("SessionProcessor.beginArgumentResolution")(function* (callID:string, expectedInput:unknown, target:'glob'|'grep', schemaSHA256:string, binding:object) {
        const match = yield* readToolCall(callID)
        if (!match || !match.call.freshTask || match.part.tool !== 'gcf_arguments' || match.part.state.status !== 'running' || match.part.metadata?.providerExecuted || argumentResolutions.has(callID) || !/^[a-f0-9]{64}$/.test(schemaSHA256)) return false
        let encoded:string
        try { encoded=JSON.stringify(expectedInput); if (encoded!==JSON.stringify(match.part.state.input) || Buffer.byteLength(encoded,'utf8')>60000) return false } catch {return false}
        argumentResolutions.set(callID,{partID:match.part.id,input:encoded,binding,claimed:false})
        stepRecovery?.invalidate()
        const metadata={...(match.part.metadata ?? {})}
        delete metadata.openclankArgumentResolution
        metadata.openclankArgumentResolution={version:1,target,schemaSHA256,policy:'gcf-generic-v3-safe-integers-v1'}
        yield* session.updatePart({...match.part,metadata})
        return true
      })
      const checkArgumentResolution = Effect.fn("SessionProcessor.checkArgumentResolution")(function* (callID:string, expectedInput:unknown, binding:object, claim:boolean) {
        const relation=argumentResolutions.get(callID), match=yield* readToolCall(callID)
        if (!relation || relation.binding!==binding || (claim && relation.claimed) || !match || !match.call.freshTask || match.part.id!==relation.partID || match.part.tool!=='gcf_arguments' || match.part.state.status!=='running') return false
        try {if (JSON.stringify(expectedInput)!==relation.input || JSON.stringify(match.part.state.input)!==relation.input) return false} catch {return false}
        if (claim) relation.claimed=true
        return true
      })

      const updateToolCall = Effect.fn("SessionProcessor.updateToolCall")(function* (
        toolCallID: string,
        update: (part: MessageV2.ToolPart) => MessageV2.ToolPart,
      ) {
        const match = yield* readToolCall(toolCallID)
        if (!match) return
        const part = yield* session.updatePart(update(match.part))
        ctx.toolcalls[toolCallID] = {
          ...match.call,
          partID: part.id,
          messageID: part.messageID,
          sessionID: part.sessionID,
        }
        return part
      })

      const completeToolCall = Effect.fn("SessionProcessor.completeToolCall")(function* (
        toolCallID: string,
        output: unknown,
        providerMetadata?: Record<string, any>,
      ) {
        const match = yield* readToolCall(toolCallID)
        if (!match || match.part.state.status !== "running") return
        const candidate = gcfCandidates.get(toolCallID)
        gcfCandidates.delete(toolCallID)
        const result = isToolExecutionResult(output) ? output : undefined
        const structured = !result && match.part.metadata?.providerExecuted
        const completed: MessageV2.ToolPart = {
          ...match.part,
          state: {
            status: "completed",
            input: match.part.state.input,
            output: result?.output ?? displayToolOutput(output),
            ...(structured ? { providerOutput: jsonToolOutput(output) } : {}),
            ...(providerMetadata ? { providerMetadata } : {}),
            metadata: result?.metadata ?? {},
            title: result?.title ?? "",
            time: { start: match.part.state.time.start, end: Date.now() },
            attachments: result?.attachments,
          },
        }
        const final = result ? gcfCandidate(result.output) : undefined
        const qualify = candidate && final && candidate.sha256 === final.sha256 && candidate.bytes === final.bytes &&
          !match.part.metadata?.providerExecuted && result?.metadata?.truncated === false &&
          result.metadata.outputPath === undefined && result.metadata.mcp?.isError === false &&
          match.part.contextSelection?.action !== "trim" && match.part.contextSelection?.action !== "omit" &&
          (!result.attachments || result.attachments.length === 0)
        const timing=timingCaptures.get(toolCallID); timingCaptures.delete(toolCallID)
        const actor=input.actorExecution
        const current=actor && ManagedProvider.currentActorHostContext(input.sessionID,actor.actorID,actor.executionRevision)
        const issuer=actor?.rootOperationID && ManagedProvider.currentActorToolTimingIssuer(input.sessionID,actor.actorID,actor.executionRevision,actor.rootOperationID)
        const admission=current?.toolViewAdmission ?? current?.jsonWhitespaceAdmission
        const verified = qualify && candidate.outline ? 'source-outline-v1' as const : qualify && candidate.toonCurrent?.() === true ? 'toon-v1' as const : result && timing && completed.tool==='bash' ? (readBashView(result) ?? (readTerminalAnsi(result) ? 'terminal-ansi-v1' as const : undefined))
          : result && timing && completed.tool==='webfetch' ? (readWebfetchMarkdown(result,{sessionID:input.sessionID,messageID:completed.messageID,callID:completed.callID}) ? 'markdown-slim-v1' as const : readWebfetchHtml(result,{sessionID:input.sessionID,messageID:completed.messageID,callID:completed.callID}) ? 'html-markdown-v1' as const : undefined)
          : result && timing && completed.tool==='read' ? readReadViewProfile(result)
          : result && timing && completed.tool==='grep' && readGrepRows(result) ? selectedToolViewProfile(admission,'grep')
          : result && timing && completed.tool==='mcp_tool_search' && readStructuredSearch(result) ? 'structured-search-v1' as const : undefined
        const view=match.call.freshTask && admission && timing && issuer===timing.owner && issuer.isAlive() && actor
          ? captureToolView(admission,timing,completed,
              ManagedProvider.currentToolViewRoute(input.sessionID,actor.actorID,actor.executionRevision),verified) : undefined
        const part = qualify ? yield* session.updateGcfCompleted(completed,timing,view,verified==='source-outline-v1' ? (candidate.outline?.current() === true ? candidate.outline.sidecar : 'null') : undefined) : yield* session.updateTimedTool(completed,timing,view)
        yield* detectTryBest(part)
        yield* settleToolCall(toolCallID)
      })

      const failToolCall = Effect.fn("SessionProcessor.failToolCall")(function* (toolCallID: string, error: unknown) {
        const match = yield* readToolCall(toolCallID)
        if (!match || match.part.state.status !== "running") return false
        // Agent-recoverable failures (bad args, malformed call, unknown task/actor
        // id) carry a marker the TUI reads to render them muted instead of as a red
        // error block. The full actionable message still flows to the model.
        const recoverable = isRecoverableError(error)
        const metadata = {
          ...match.part.state.metadata,
          ...getToolResultMetadata(error),
          ...(recoverable ? { recoverable: true } : {}),
        }
        const attachments = getToolResultAttachments(error)?.flatMap((attachment) => {
          const parsed = MessageV2.FilePart.safeParse(attachment)
          return parsed.success ? [parsed.data] : []
        })
        const timing=timingCaptures.get(toolCallID); timingCaptures.delete(toolCallID)
        const part = yield* session.updateTimedTool({
          ...match.part,
          state: {
            status: "error",
            input: match.part.state.input,
            error: errorMessage(error),
            ...(Object.keys(metadata).length > 0 ? { metadata } : {}),
            ...(attachments && attachments.length > 0 ? { attachments } : {}),
            time: { start: match.part.state.time.start, end: Date.now() },
          },
        }, timing)
        yield* detectTryBest(part)
        if (error instanceof Permission.RejectedError || error instanceof Question.RejectedError) {
          ctx.blocked = ctx.blocked || ctx.shouldBreak
        }
        yield* settleToolCall(toolCallID)
        return true
      })

      const checkTextNgram = (text: string) => {
        if (ctx.textNgramRepeat || !ctx.textNgramMonitor) return
        if (ctx.textNgramMonitor.append(text)) ctx.textNgramRepeat = true
      }

      let attemptCommitted = false
      const commitManagedAttempt = () => Effect.suspend(() => {
        // Set first: a host commit failure leaves the actual outcome unknown.
        attemptCommitted = true
        return ManagedProvider.enabled()
          ? Effect.promise(() => ManagedProvider.commitSessionOperation(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision))
          : Effect.void
      })

      const recordManagedSuccess = (observation?:AccountSelection.DispatchObservation) =>
        ManagedProvider.enabled()
          ? Effect.promise(() => ManagedProvider.recordSessionSuccess(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision, observation))
          : Effect.void

      // Capture lazily, but before the first actual hook/tool callback. The
      // shared promise is owned by this processor; interrupting an individual
      // waiter only stops that wait, while processor interruption aborts Git.
      let captureFiber: Fiber.Fiber<void, unknown> | undefined
      const ensureSnapshot = (): Effect.Effect<void, unknown> =>
        Effect.gen(function* () {
          if (!ctx.lazySnapshot) return
          if (ctx.snapshot || ctx.snapshotAttempted) return
          if (!captureFiber) {
            captureFiber = bridge.fork(
              Effect.gen(function* () {
                const captured = yield* snapshot.track({ strict: true })
                if (captured !== undefined) {
                  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(captured)) {
                    throw new Error("Snapshot capture failed: Git returned an invalid tree reference")
                  }
                  ctx.snapshot = captured
                  if (ctx.stepStartPartID) {
                    yield* session.updatePart({
                      id: ctx.stepStartPartID,
                      messageID: ctx.assistantMessage.id,
                      sessionID: ctx.sessionID,
                      snapshot: captured,
                      type: "step-start",
                    })
                  }
                }
                // Undefined is the existing explicit disabled/non-Git result.
                ctx.snapshotAttempted = true
              }),
            )
          }
          yield* Fiber.join(captureFiber)
        })

      let replaying = false
      let outputAttempt:SourceAttemptHandle | undefined
      const handleEvent = Effect.fnUntraced(function* (value: StreamEvent) {
        if (outputAttempt && (replaying || !["start","start-step","text-start","text-delta","text-end","finish-step","finish"].includes(value.type)))
          invalidateSourceOutput(outputAttempt)
        switch (value.type) {
          case "start":
            if (isMain) yield* status.set(ctx.sessionID, { type: "busy" })
            return

          case "reasoning-start":
            yield* commitManagedAttempt()
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
            if (!replaying) stepRecovery?.emit(value.id)
            yield* commitManagedAttempt()
            if (ctx.assistantMessage.summary) {
              throw new Error(`Tool call not allowed while generating summary: ${value.toolName}`)
            }
            const newlyAllocated = !ctx.toolcalls[value.id]
            const part = yield* session.updatePart({
              id: ctx.toolcalls[value.id]?.partID ?? PartID.ascending(),
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "tool",
              tool: value.toolName,
              callID: value.id,
              state: { status: "pending", input: {}, raw: "" },
              metadata: value.providerExecuted ? { providerExecuted: true } : undefined,
            } satisfies MessageV2.ToolPart)
            ctx.stepPartIds.push(part.id)
            ctx.toolcalls[value.id] = {
              done: yield* Deferred.make<void>(),
              partID: part.id,
              messageID: part.messageID,
              sessionID: part.sessionID,
              freshTask: !replaying && newlyAllocated && !value.providerExecuted,
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
            yield* updateToolCall(value.toolCallId, (match) => ({
              ...match,
              tool: value.toolName,
              state: {
                ...match.state,
                status: "running",
                input: value.input,
                time: { start: Date.now() },
              },
              metadata: (() => {
                const meta={...(value.providerMetadata ?? {})}
                delete meta.openclankArgumentResolution
                return match.metadata?.providerExecuted ? {...meta,providerExecuted:true} : meta
              })(),
            }))

            const timingPart = yield* readToolCall(value.toolCallId)
            if (timingPart?.call.freshTask && timingPart.part.state.status === "running" && input.actorExecution?.rootOperationID && input.actorExecution.innerTurnID) {
              const part = timingPart.part
              ManagedProvider.markFreshTaskCall({ sessionID: part.sessionID, rootOperationID: input.actorExecution.rootOperationID,
                actorID: input.actorExecution.actorID, executionRevision: input.actorExecution.executionRevision,
                innerTurnID: input.actorExecution.innerTurnID, messageID: part.messageID, partID: part.id,
                toolCallID: part.callID, toolName: part.tool })
            }
            if (timingPart && input.actorExecution && ManagedProvider.currentActorHostContext(input.sessionID,input.actorExecution.actorID,input.actorExecution.executionRevision)) {
              const timing=captureToolTiming(ManagedProvider.currentActorToolTimingIssuer(input.sessionID,input.actorExecution.actorID,input.actorExecution.executionRevision,input.actorExecution.rootOperationID ?? ""),input.actorExecution,timingPart.part)
              if (timing) timingCaptures.set(value.toolCallId,timing)
            }

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

            const agent = yield* agents.get(ctx.assistantMessage.agent)
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
            yield* completeToolCall(value.toolCallId, value.output, value.providerMetadata)
            return
          }

          case "tool-error": {
            stepRecovery?.invalidate()
            yield* failToolCall(value.toolCallId, value.error)
            yield* bus
              .publish(Metrics.ToolCall, {
                sessionID: ctx.sessionID,
                tool_name: value.toolName,
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
              const scope=ManagedProvider.currentScope(ctx.sessionID,ctx.actorExecution?.actorID,ctx.actorExecution?.executionRevision)
              const original=ManagedProvider.originalRefreshObservationScope(ctx.sessionID,ctx.actorExecution?.actorID,ctx.actorExecution?.executionRevision)
              const observedScope=hasObservedMalformedResponse(value.error,scope) ? scope :
                hasObservedMalformedResponse(value.error,original) ? original : undefined
              if (observedScope) {
                if (attemptCommitted && hasMalformedResponse(value.error,observedScope)) emitMalformedDiagnostic(value.error,observedScope, ManagedProvider.committedRefreshDiagnostic(ctx.sessionID,ctx.actorExecution?.actorID,ctx.actorExecution?.executionRevision))
                // The provider returned 2xx; missing step does not prove no work.
                attemptCommitted=true
              }
            } catch {}
            throw value.error
          }

          case "start-step":
            // A provider start-step means the remote request was accepted and
            // may already be billable, even before a visible token arrives.
            yield* commitManagedAttempt()
            ctx.stepStartedAt = Date.now()
            ctx.firstTokenAt = undefined
            if (!ctx.lazySnapshot && !ctx.snapshot) ctx.snapshot = yield* snapshot.track()
            const stepStartPartId = PartID.ascending()
            ctx.stepStartPartID = stepStartPartId
            observedStep = {id:stepStartPartId, closed:false, admitted:false}
            yield* session.updatePart({
              id: stepStartPartId,
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.sessionID,
              snapshot: ctx.snapshot,
              type: "step-start",
            })
            ctx.stepPartIds.push(stepStartPartId)
            if (!replaying) stepRecovery?.start(stepStartPartId)
            return

          case "finish-step": {
            if (!replaying) finishSourceOutputStep(outputAttempt,value.finishReason)
            if (!replaying) stepRecovery?.close()
            if (observedStep) {
              const step = observedStep
              step.closed = true
              if (step.admitted && step.binding && session.changeCaptureMembership) {
                const bound = step.binding
                yield* session.changeCaptureMembership(bound,{kind:"seal"},() => {
                  const current = ManagedProvider.currentActorHostContext(bound.sessionID,bound.actorID,bound.executionRevision)
                  return observedStep === step && !!current && current.accountID === bound.accountID && current.chatID === bound.hostChatID && current.workspaceID === bound.workspaceID
                    && !!ManagedProvider.currentActorToolTimingIssuer(bound.sessionID,bound.actorID,bound.executionRevision,bound.rootOperationID)
                }).pipe(Effect.ignore)
              }
            }
            let scoped:ReturnType<typeof takeSdkSuccessStep>={metadata:undefined,observation:undefined}
            try { scoped=takeSdkSuccessStep(ManagedProvider.successObservationScope(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision),value.providerMetadata) } catch {}
            yield* commitManagedAttempt()
            const usage = Session.getUsage({
              model: ctx.model,
              usage: value.usage,
              captureScope: ManagedProvider.currentScope(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision),
              metadata: scoped.metadata,
            })
            ctx.assistantMessage.finish = value.finishReason
            ctx.assistantMessage.cost += usage.cost
            ctx.assistantMessage.tokens = usage.tokens
            const finalSnapshot = ctx.lazySnapshot
              ? ctx.snapshot
                ? yield* snapshot.track({ strict: true }).pipe(
                    Effect.flatMap((tree) =>
                      tree === undefined || /^(?:[0-9a-f]{40}|[0-9a-f]{64})$/i.test(tree)
                        ? Effect.succeed(tree)
                        : Effect.fail(new Error("Git returned an invalid tree reference")),
                    ),
                    Effect.catchCause((cause) =>
                      Cause.hasInterruptsOnly(cause)
                        ? Effect.failCause(cause)
                        : Effect.gen(function* () {
                            yield* snapshotRecoveryNotice("step_finish", "final_capture_failed")
                            return undefined
                          }),
                    ),
                  )
                : undefined
              : yield* snapshot.track()
            yield* session.updatePart({
              id: PartID.ascending(),
              reason: value.finishReason,
              snapshot: finalSnapshot,
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "step-finish",
              tokens: usage.tokens,
              cost: usage.cost,
            })
            yield* session.updateMessage(ctx.assistantMessage)
            let stepFilesChanged = 0
            if (ctx.snapshot) {
              const patch = ctx.lazySnapshot
                ? yield* snapshot.patch(ctx.snapshot, { strict: true }).pipe(
                    Effect.catchCause((cause) =>
                      Cause.hasInterruptsOnly(cause)
                        ? Effect.failCause(cause)
                        : Effect.gen(function* () {
                            yield* snapshotRecoveryNotice("step_finish", "patch_failed")
                            return undefined
                          }),
                    ),
                  )
                : yield* snapshot.patch(ctx.snapshot)
              if (!patch) {
                ctx.snapshot = undefined
              } else {
                stepFilesChanged = patch.files.length
                if (patch.files.length) {
                  yield* session.updatePart({
                    id: PartID.ascending(),
                    messageID: ctx.assistantMessage.id,
                    sessionID: ctx.sessionID,
                    type: "patch",
                    hash: patch.hash,
                    files: patch.files,
                  })
                }
                ctx.snapshot = undefined
              }
            }
            ctx.snapshotAttempted = false
            ctx.stepStartPartID = undefined
            captureFiber = undefined
            const stepTokensIn = usage.tokens.input + usage.tokens.cache.read + usage.tokens.cache.write
            const stepTokensOut = usage.tokens.output + usage.tokens.reasoning
            if (ctx.agentMetrics) {
              ctx.agentMetrics.tokens_in += stepTokensIn
              ctx.agentMetrics.tokens_out += stepTokensOut
              ctx.agentMetrics.files_changed += stepFilesChanged
            }
            yield* bus
              .publish(Metrics.ModelCall, {
                sessionID: ctx.sessionID,
                finish_reason: value.finishReason,
                ttft_ms:
                  ctx.firstTokenAt && ctx.stepStartedAt ? ctx.firstTokenAt - ctx.stepStartedAt : undefined,
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
            yield* recordManagedSuccess(scoped.observation)
            if (
              !ctx.assistantMessage.summary &&
              isOverflow({ cfg: ctx.cfg ?? (yield* config.get()), tokens: usage.tokens, model: ctx.model })
            ) {
              ctx.needsOverflowHandling = true
            }
            return
          }

          case "text-start":
            yield* commitManagedAttempt()
            ctx.currentText = {
              id: PartID.ascending(),
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.assistantMessage.sessionID,
              type: "text",
              text: "",
              time: { start: Date.now() },
              metadata: value.providerMetadata,
            }
            yield* session.updatePart(ctx.currentText)
            ctx.stepPartIds.push(ctx.currentText.id)
            if (!replaying) startSourceOutput(outputAttempt,ctx.actorExecution,ctx.currentText.messageID,ctx.currentText.id,value.id)
            return

          case "text-delta":
            if (!ctx.firstTokenAt) ctx.firstTokenAt = Date.now()
            if (!ctx.currentText) return
            if (!replaying) appendSourceOutput(outputAttempt,value.id,value.text)
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
            const textIdentity = {sessionID:ctx.sessionID,messageID:ctx.assistantMessage.id,partID:ctx.currentText.id}
            if (outputAttempt && !replaying) plugin.requestEmptyTextTransform?.(textTransform)
            ctx.currentText.text = (yield* plugin.trigger(
              "experimental.text.complete",
              textIdentity,
              textTransform,
            )).text
            if (!replaying) endSourceOutput(outputAttempt,value.id,ctx.currentText.text,
              () => plugin.readEmptyTextTransform?.(textTransform,textIdentity) === true)
            {
              const end = Date.now()
              ctx.currentText.time = { start: ctx.currentText.time?.start ?? end, end }
            }
            if (value.providerMetadata) ctx.currentText.metadata = value.providerMetadata
            yield* session.updatePart(ctx.currentText)
            ctx.currentText = undefined
            return

          case "finish":
            if (!replaying) noteSourceOutputFinish(outputAttempt)
            yield* commitManagedAttempt()
            return

          default:
            if (!Flag.OPEN_CLANK_PRIVATE_RUNTIME) slog.info("unhandled", { event: value.type, value })
            return
        }
      })

      const cleanup = Effect.fn("SessionProcessor.cleanup")(function* () {
        if (observedStep) observedStep.closed = true
        if (ctx.snapshot) {
          const patch = ctx.lazySnapshot
            ? yield* snapshot.patch(ctx.snapshot, { strict: true }).pipe(
                Effect.catchCause((cause) =>
                  Cause.hasInterruptsOnly(cause)
                    ? Effect.failCause(cause)
                    : Effect.gen(function* () {
                        yield* snapshotRecoveryNotice("cleanup", "patch_failed")
                        return undefined
                      }),
                ),
              )
            : yield* snapshot.patch(ctx.snapshot)
          if (patch?.files.length) {
            yield* session.updatePart({
              id: PartID.ascending(),
              messageID: ctx.assistantMessage.id,
              sessionID: ctx.sessionID,
              type: "patch",
              hash: patch.hash,
              files: patch.files,
            })
          }
          ctx.snapshot = undefined
        }

        if (ctx.currentText) {
          const end = Date.now()
          ctx.currentText.time = { start: ctx.currentText.time?.start ?? end, end }
          yield* session.updatePart(ctx.currentText)
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

        yield* Effect.forEach(
          Object.values(ctx.toolcalls),
          (call) => Deferred.await(call.done).pipe(Effect.timeout("250 millis"), Effect.ignore),
          { concurrency: "unbounded" },
        )

        for (const toolCallID of Object.keys(ctx.toolcalls)) {
          const match = yield* readToolCall(toolCallID)
          if (!match) continue
          const part = match.part
          const end = Date.now()
          const metadata = "metadata" in part.state && isRecord(part.state.metadata) ? part.state.metadata : {}
          yield* session.updatePart({
            ...part,
            state: {
              ...part.state,
              status: "error",
              error: "Tool execution aborted",
              metadata: { ...metadata, interrupted: true },
              time: { start: "time" in part.state ? part.state.time.start : end, end },
            },
          })
        }
        ctx.toolcalls = {}
        ctx.assistantMessage.time.completed = Date.now()
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
        if (isMain) yield* status.set(ctx.sessionID, { type: "idle" })
      })

      const process = Effect.fn("SessionProcessor.process")(function* (streamInput: LLM.StreamInput,
        rebuildAlternative?: (context: ManagedProvider.OwnedAlternativeRebuild) => Effect.Effect<LLM.StreamInput, unknown>) {
        slog.info("process")
        outputAttempt=streamInput.sourceAttempt
        const sourceActor=ctx.actorExecution
        const originalTurn=isMain && sourceActor?.actorID === "main" && sourceActor.sessionID === ctx.sessionID && sourceActor.rootOperationID && sourceActor.innerTurnID
          ? {sessionID:sourceActor.sessionID,rootOperationID:sourceActor.rootOperationID,actorID:sourceActor.actorID,executionRevision:sourceActor.executionRevision,innerTurnID:sourceActor.innerTurnID}
          : undefined
        ctx.needsOverflowHandling = false
        ctx.shouldBreak = (ctx.cfg ?? (yield* config.get())).experimental?.continue_loop_on_deny !== true

        let contextAttempt = 0
        const run = Effect.gen(function* () {
          const oneAttempt = Effect.gen(function* () {
            const attemptNumber = ++contextAttempt
            stepRecovery?.abort()
            stepRecovery = createStepRecoveryRecorder()
            const attemptRecorder = stepRecovery
            if (attemptNumber !== 1) attemptRecorder.invalidate()
            attemptCommitted = false
            ctx.currentText = undefined
            ctx.reasoningMap = {}
            ctx.stepPartIds = []
            ctx.toolcalls = {}
            gcfCandidates.clear(); invalidGcfCalls.clear()
            ctx.textNgramRepeat = false
            ctx.textNgramMonitor = createTextNgramMonitor()
            if (ctx.lazySnapshot) {
              ctx.snapshot = undefined
              ctx.snapshotAttempted = false
            }
            ctx.stepStartPartID = undefined
            captureFiber = undefined
            ctx.snapshotRecoveryNotices.clear()
            const guardedTools = ManagedProvider.enabled() ? Object.fromEntries(
              Object.entries(streamInput.tools).map(([name, tool]) => {
                if (!tool.execute) return [name, tool]
                const execute = tool.execute
                const guarded = {
                    ...tool,
                    execute: (...args: Parameters<typeof execute>) => {
                      const id = (args[1] as {toolCallId?:string} | undefined)?.toolCallId ?? ""
                      const token = attemptRecorder.enter(id)
                      return (async () => {
                      let succeeded = false
                      try {
                      const signal = (args[1] as { abortSignal?: AbortSignal } | undefined)?.abortSignal
                      const capture = ctx.lazySnapshot ? bridge.promise(ensureSnapshot()) : Promise.resolve()
                      if (!signal) await capture
                      else {
                        await new Promise<void>((resolve, reject) => {
                          const onAbort = () => {
                            signal.removeEventListener("abort", onAbort)
                            reject(signal.reason ?? new DOMException("Aborted", "AbortError"))
                          }
                          signal.addEventListener("abort", onAbort, { once: true })
                          if (signal.aborted) onAbort()
                          capture.then(
                            () => {
                              signal.removeEventListener("abort", onAbort)
                              resolve()
                            },
                            (error) => {
                              signal.removeEventListener("abort", onAbort)
                              reject(error)
                            },
                          )
                        })
                      }
                      if (signal?.aborted) throw signal.reason ?? new DOMException("Aborted", "AbortError")
                      if (token && !attemptRecorder.isCurrent(token)) attemptRecorder.invalidate()
                      const output = await execute.apply(tool, args)
                      succeeded = !signal?.aborted
                      return output
                      } finally { attemptRecorder.finish(token,succeeded) }
                      })()
                    },
                  }
                transferGuardedToolDefinition(tool,guarded)
                return [name,guarded]
              }),
            ) as LLM.StreamInput["tools"] : streamInput.tools
            const evidenceActor=ctx.actorExecution, managedEvidence=ManagedProvider.enabled()
            const evidenceTurn=evidenceActor?.rootOperationID && evidenceActor.innerTurnID ? {sessionID:evidenceActor.sessionID,rootOperationID:evidenceActor.rootOperationID,actorID:evidenceActor.actorID,executionRevision:evidenceActor.executionRevision,innerTurnID:evidenceActor.innerTurnID} : undefined
            const evidenceScope=evidenceActor ? ManagedProvider.currentScope(ctx.sessionID,evidenceActor.actorID,evidenceActor.executionRevision) : undefined
            const evidenceCurrent=()=>!managedEvidence || (!!evidenceTurn && !!evidenceScope && ctx.actorExecution===evidenceActor && isAdmittedToolTimingTurn(evidenceActor,evidenceTurn) &&
              ManagedProvider.currentScope(ctx.sessionID,evidenceTurn.actorID,evidenceTurn.executionRevision)===evidenceScope &&
              ManagedProvider.currentToolViewRoute(ctx.sessionID,evidenceTurn.actorID,evidenceTurn.executionRevision)?.rootOperationID===evidenceTurn.rootOperationID)
            const evidenceOwner={id:ctx.assistantMessage.id,sessionID:ctx.assistantMessage.sessionID,parentID:ctx.assistantMessage.parentID,created:ctx.assistantMessage.time.created,actorID:evidenceActor?.actorID ?? ctx.assistantMessage.agentID ?? 'main',...(managedEvidence ? {executionRevision:evidenceTurn?.executionRevision ?? -1} : {})}
            const evidenceSave=(message:MessageV2.Assistant)=>bridge.promise(session.updateContextEvidence(message,evidenceOwner,evidenceCurrent))
            const stream = llm.stream({ ...streamInput, tools: guardedTools, contextSequence: { attempt: attemptNumber, transform: 0 },
              onNativeTextDelivery: nativeTextDeliveryWriter(ctx.assistantMessage,
                evidenceSave,
                () => slog.warn("native text delivery evidence unavailable"),evidenceCurrent),
              onNativeMembership: nativeMembershipDeliveryWriter(ctx.assistantMessage,
                evidenceSave,
                () => slog.warn("native membership evidence unavailable"),evidenceCurrent),
              onContextFrame: contextFrameWriter(ctx.assistantMessage,
                evidenceSave,
                () => slog.warn("context inspection persistence unavailable"),evidenceCurrent),
            })

            let completedSteps = 0
            yield* stream.pipe(
              Stream.tap((event) => handleEvent(event).pipe(
                Effect.tap(() => Effect.sync(() => { if (event.type === "finish-step") completedSteps++ })),
              )),
              Stream.takeUntil(() => ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked),
              Stream.runDrain,
              Effect.onExit(exit => Effect.sync(() => { if (Exit.isFailure(exit)) attemptRecorder.abort() })),
            )
            if (ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked || ctx.assistantMessage.error || !ctx.actorExecution) {
              attemptRecorder.abort()
            } else {
              const declarations = attemptRecorder.drain()
              for (const declaration of declarations) {
                try {
                  if (!(yield* Effect.promise(() => Promise.all(declaration.current.map(check=>check())).catch(()=>[false]))).every(Boolean)) continue
                  const capability=createStepRecoveryCapability(declaration.binding,ctx.actorExecution)
                  if (capability) publishStepRecovery(capability,declaration.members)
                } catch { /* Recovery evidence is optional; Git remains authoritative. */ }
              }
            }
            if (completedSteps === 0 && !ctx.needsOverflowHandling && !ctx.textNgramRepeat && !ctx.blocked) {
              return yield* Effect.fail(new Error("Provider stream ended without a completed step"))
            }
          }).pipe(
            Effect.onInterrupt(() =>
              Effect.gen(function* () {
                stepRecovery?.abort()
                aborted = true
                if (captureFiber) yield* Fiber.interrupt(captureFiber).pipe(Effect.ignore)
                if (!ctx.assistantMessage.error) {
                  yield* halt(new DOMException("Aborted", "AbortError"))
                }
              }),
            ),
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.fail(Cause.squash(cause)),
            ),
          )

          const managedRetry = (attempt: number): Effect.Effect<void, unknown> =>
            oneAttempt.pipe(
              Effect.catch((error: unknown) => {
                if (attemptCommitted) return Effect.fail(error)
                return Effect.promise(() => originalTurn ? ManagedProvider.recordOwnedAnthropicTerminal(originalTurn,error) : Promise.resolve('not_eligible' as const)).pipe(
                  Effect.flatMap((terminal) => terminal === 'stopped' && originalTurn && rebuildAlternative
                    ? Effect.gen(function* () {
                        const context=yield* Effect.promise(() => ManagedProvider.prepareOwnedAlternative(originalTurn))
                        if (!context || !context.current()) return yield* Effect.fail(error)
                        const rebuilt=yield* rebuildAlternative(context)
                        if (!context.current()) return yield* Effect.fail(error)
                        const previous={model:ctx.model,providerID:ctx.assistantMessage.providerID,modelID:ctx.assistantMessage.modelID,variant:ctx.assistantMessage.variant}
                        ctx.model=rebuilt.model;ctx.assistantMessage.providerID=rebuilt.model.providerID;ctx.assistantMessage.modelID=rebuilt.model.id
                        ctx.assistantMessage.variant=rebuilt.user.model.variant
                        yield* session.updateMessage(ctx.assistantMessage)
                        if (!(yield* Effect.promise(() => ManagedProvider.activateOwnedAlternative(context)))) {
                          ctx.model=previous.model;ctx.assistantMessage.providerID=previous.providerID;ctx.assistantMessage.modelID=previous.modelID
                          ctx.assistantMessage.variant=previous.variant
                          yield* session.updateMessage(ctx.assistantMessage)
                          return yield* Effect.fail(error)
                        }
                        yield* session.updatePart({
                          id: PartID.ascending(),
                          messageID: ctx.assistantMessage.id,
                          sessionID: ctx.sessionID,
                          type: "text",
                          text: `Switched from ${previous.providerID}/${previous.modelID} to ${rebuilt.model.providerID}/${rebuilt.model.id}: the first model's API key was rejected before any output.`,
                          synthetic: true,
                          metadata: { origin: { kind: "model_switch" } },
                          time: { start: Date.now(), end: Date.now() },
                        })
                        streamInput=rebuilt
                        return yield* oneAttempt
                      })
                    : terminal !== 'not_eligible' ? Effect.fail(error) : Effect.promise(() => ManagedProvider.recordSessionAttempt(ctx.sessionID, error, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision)).pipe(
                  Effect.flatMap((decision) => {
                    if (!decision.retry) return Effect.fail(error)
                    const nextAttempt = attempt + 1
                    const baseDelay = decision.rotated ? 0 : Math.min(500 * 2 ** attempt, 5_000)
                    const localDelay = baseDelay === 0 ? 0 : Math.max(1, Math.round(baseDelay * (0.75 + Math.random() * 0.5)))
                    const delayMs = decision.rotated ? 0 : Math.max(localDelay, decision.retryAfterMs ?? 0)
                    if (!ManagedProvider.reserveRetryWait(ctx.sessionID, ctx.actorExecution?.actorID, ctx.actorExecution?.executionRevision, delayMs)) return Effect.fail(error)
                    const update = isMain
                      ? status.set(ctx.sessionID, {
                          type: "retry",
                          attempt: nextAttempt,
                          message: decision.rotated
                            ? "Trying another eligible provider account"
                            : "Retrying the same provider account",
                          next: Date.now() + delayMs,
                        })
                      : Effect.void
                    const discardProvisional = Effect.gen(function* () {
                      for (const partId of ctx.stepPartIds) {
                        yield* session.removePart({ sessionID: ctx.sessionID, messageID: ctx.assistantMessage.id, partID: partId })
                      }
                      ctx.stepPartIds = []
                    })
                    return discardProvisional.pipe(
                      Effect.andThen(update),
                      Effect.andThen(Effect.sleep(Duration.millis(delayMs))),
                      Effect.andThen(managedRetry(nextAttempt)),
                    )
                  }),
                  )),
                )
              }),
            )

          const execution = ManagedProvider.enabled()
            ? managedRetry(0)
            : oneAttempt.pipe(
                Effect.retry(
                  SessionRetry.policy({
                    parse,
                    canRetry: () => !attemptCommitted,
                    set: (info) =>
                      isMain
                        ? status.set(ctx.sessionID, {
                            type: "retry",
                            attempt: info.attempt,
                            message: info.message,
                            next: info.next,
                          })
                        : Effect.void,
                  }),
                ),
              )

          yield* execution.pipe(
            Effect.catch(halt),
            Effect.ensuring(cleanup()),
          )

          if (ctx.needsOverflowHandling || ctx.textNgramRepeat || ctx.blocked || ctx.assistantMessage.error)
            invalidateSourceOutput(outputAttempt)
          else finishSourceOutputStream(outputAttempt)
          if (ctx.needsOverflowHandling) return "overflow"
          if (ctx.textNgramRepeat) return "text-repeat"
          if (ctx.blocked || ctx.assistantMessage.error) return "stop"
          return "continue"
        })
        return yield* (ctx.lazySnapshot ? run.pipe(Effect.provideService(Snapshot.CaptureBarrier, ensureSnapshot)) : run)
      })

      const replay = Effect.fn("SessionProcessor.replay")(function* (input: ReplayInput) {
        replaying = true
        invalidateSourceOutput(outputAttempt)
        slog.info("replay", { toolCalls: input.toolCalls.length, finish: input.finishReason })
        ctx.needsOverflowHandling = false
        ctx.shouldBreak = (ctx.cfg ?? (yield* config.get())).experimental?.continue_loop_on_deny !== true

        const ctrl = new AbortController()

        const emptyUsage = {
          inputTokens: 0,
          outputTokens: 0,
          totalTokens: 0,
        }

        const run = Effect.gen(function* () {
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

              yield* ensureSnapshot()

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
              providerMetadata: input.providerMetadata,
            } as unknown as StreamEvent)

            // Account for ensemble overhead (losing candidates + judge): real
            // spend that consumed no context. Add it to cost + agent metrics +
            // a supplementary ModelCall metric, but NOT to message.tokens (set
            // by the finish-step above from the winner only) so context
            // estimators stay honest.
            if (input.overhead && (input.overhead.cost > 0 || input.overhead.tokensIn > 0 || input.overhead.tokensOut > 0)) {
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
          }).pipe(
            Effect.onInterrupt(() =>
              Effect.gen(function* () {
                aborted = true
                // Propagate cancellation into the currently-executing tool so a
                // long-running winner tool call is actually interrupted.
                ctrl.abort()
                if (captureFiber) yield* Fiber.interrupt(captureFiber).pipe(Effect.ignore)
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
        try {
          return yield* (ctx.lazySnapshot ? run.pipe(Effect.provideService(Snapshot.CaptureBarrier, ensureSnapshot)) : run)
        } finally {
          replaying = false
        }
      })

      return {
        get message() {
          return ctx.assistantMessage
        },
        updateToolCall,
        completeToolCall,
        beginArgumentResolution,
        checkArgumentResolution,
        recordGcfCandidate,
        observedCaptureMembership,
        stepRecovery: () => stepRecovery,
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
    Layer.provide(Snapshot.defaultLayer),
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
