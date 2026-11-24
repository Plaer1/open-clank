import { sourceAttemptLayout, registerSkillReminder, createSourceAttempt, invalidateSourceAttempt, noteSourceStep, readSourceWire, readSourceOutput, readSettledSourceOutput, type SourceAttemptHandle } from "./closed-source-provenance"
import { snapshotSourceParts, type SourceLayout } from "./source-layout"
import { globalEventRecoveryWitness } from "../bus/global"
import { requestProjectPolicyRecoveryEvidence, projectPolicyRecoveryEvidence } from "../tool/project-policy"
import { qualifyOriginalCapture } from "./step-recovery-recorder"
import { stepRecoveryProfileSupported, type Binding as StepRecoveryBinding } from "./step-recovery-owner"
import { markAdmittedToolTimingTurn, retireAdmittedToolTimingTurn, isAdmittedToolTimingTurn } from "./tool-timing"
import { evidence as recoveryEvidence, type ToolWitness } from "./recovery-profile"
import { currentToolViewContext, toolViewContextCurrent, prepareToolViews, restoreContextPreface, type ToolViewContext } from './tool-view-assembly'
import { transferTerminalAnsi } from './tool-view-terminal-ansi'
import { transferWebfetchHtml } from '../tool/webfetch-view'
import { transferGrepRows } from './tool-view-grep'
import { transferReadView } from './tool-view-read'
import { transferStructuredSearch } from './tool-view-structured-search'
import { enabledToolViewProfiles, toonViewEnabled, outlineViewEnabled } from './tool-view-source'
import { sourceOutlineAdmission } from './source-outline-admission'
import { recordMcpJsonCandidate } from './mcp-json-candidate'
import { transferBashView } from './tool-view-bash-proof'
import type { Profile as ProducerViewProfile } from './tool-view-producer-admission'
import { GCF_ARGUMENT_TOOL, GCF_ARGUMENT_POLICY } from "./gcf-arguments"
import { bindToolViewProducer } from './tool-view-producer-admission'
import { WriterService as HistoryWriterService } from '@/history'
import { sourceEvidence, snapshotNativeText, reconcileNativeText, nativeSingleUserWire, captureNativeMembership, recallToolConverted, boundedContextSnapshot, type GeneratedContextSource } from "./context-inspection"
import path from "path"
import { isDeepStrictEqual } from "node:util"
import os from "os"
import z from "zod"
import { SessionID, MessageID, PartID } from "./schema"
import { MessageV2 } from "./message-v2"
import { classifyAssistantStep } from "./classify"
import { Log, Token } from "../util"
import { SessionRevert } from "./revert"
import * as Session from "./session"
import { Agent } from "../agent/agent"
import { decideAskRouting, resolveInvalidOutputPolicy, SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { parseReturnHeader } from "@/actor/return-header"
import { Provider } from "../provider"
import { ModelID, ProviderID } from "../provider/schema"
import {
  type Tool as AITool,
  type ModelMessage,
  tool,
  jsonSchema,
  type ToolExecutionOptions,
  asSchema,
  generateText,
  wrapLanguageModel,
} from "ai"
import { InstallationVersion } from "@/installation/version"
import type { JSONObject, JSONSchema7 } from "@ai-sdk/provider"
import { SessionPrune } from "./prune"
import { SessionCheckpoint } from "./checkpoint"
import { SessionCompaction } from "./compaction"
import { computeLastMessageInfo } from "./last-message-info"
import { contextPressureLevel, pressureLevel, usable, isOverflow as overflowCheck } from "./overflow"
import { Config } from "@/config"
import { Global } from "@/global"
import { Bus } from "../bus"
import { ProviderTransform } from "../provider"
import { SystemPrompt } from "./system"
import { Instruction } from "./instruction"
import { TuiEvent } from "@/cli/cmd/tui/event"
import { Plugin } from "../plugin"
import BUILD_SWITCH from "../session/prompt/build-switch.txt"
import MAX_STEPS from "../session/prompt/max-steps.txt"
import PROMPT_COMPOSE from "../session/prompt/compose.txt"
import {
  RECOVERY_PROMPT_MILD,
  RECOVERY_PROMPT_STRONG,
  TEXT_LOOP_BUFFER_SIZE,
  TEXT_LOOP_TRIGGER_COUNT,
  TEXT_LOOP_MAX_RECOVERY,
  normalizeForLoopDetection,
  detectTextLoop,
} from "../session/prompt/text-loop-recovery"
import {
  TEXT_NGRAM_MAX_RECOVERY,
  TEXT_NGRAM_RECOVERY_REMIND,
  TEXT_NGRAM_RECOVERY_REPLAN,
} from "../session/prompt/text-ngram-detection"
import {
  EMPTY_STEP_MAX_RECOVERY,
  EMPTY_STEP_RECOVERY_REMIND,
  EMPTY_STEP_RECOVERY_REPLAN,
  isEmptyStep,
} from "../session/prompt/empty-step-detection"
import { applyPersistedCrops, cropMetadata, detectStreak, extractAllCrops, streakKey } from "../session/prompt/loop-streak"
import { builtinSkillRoot, matchDocumentSkills } from "@/skill/builtin/extract"
import { Skill } from "@/skill"
import { ToolRegistry } from "../tool"
import { MCP } from "../mcp"
import { normalizeToolResult } from "../mcp/tool-result"
import { LSP } from "../lsp"
import { Flag } from "../flag/flag"
import { isMemoryWriteEnabled } from "@/memory/write-gate"
import { ulid } from "ulid"
import { ChildProcess, ChildProcessSpawner } from "effect/unstable/process"
import * as CrossSpawnSpawner from "@/effect/cross-spawn-spawner"
import * as Stream from "effect/Stream"
import { Command } from "../command"
import { pathToFileURL, fileURLToPath } from "url"
import { ConfigMarkdown, ConfigCompose } from "../config"
import { SessionSummary } from "./summary"
import { NamedError } from "@mimo-ai/shared/util/error"
import { SessionProcessor } from "./processor"
import { buildLLMRequestPrefix } from "./llm-request-prefix"
import { gcfViewIdentity, gcfMemoScope, gcfSameRequestRowsAdmission, gcfGraphReferencesAdmission } from "./gcf-policy"
import { createGcfRequestFrames } from "./gcf-request-frames"
import {
  serializeTrajectoryMessages,
  withAssistantParts,
  userQueryText,
  assistantFinalText,
  sessionErrorText,
} from "./trajectory"
import { prefixCaptureRef } from "./prefix-capture-ref"
import { SessionPrefixSnapshot } from "./prefix-snapshot"
import { bindConstructedTool } from "./tool-definition-provenance"
import * as PrefixProvenance from "./prefix-provenance"
import { spawnRef } from "@/actor/spawn-ref"
import { Inbox } from "@/inbox"
import * as UserInput from "@/session/user-input"
import { sessionPromptRef, defaultModelRef } from "@/inbox/inbox-ref"
import { Tool } from "@/tool"
import { Permission } from "@/permission"
import { SessionStatus } from "./status"
import { LLM } from "./llm"
import { GenTitle, type GenTitlePart } from "./gen-title"
import { NotFoundError } from "@/storage"

export const normalizeTitleInput = GenTitle.normalizeTitleInput
export const sanitizeGeneratedTitle = GenTitle.sanitizeGeneratedTitle
export const titlePromptText = GenTitle.titlePromptText
import { MaxMode } from "./max-mode"
import { Shell } from "@/shell/shell"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { Truncate } from "@/tool"
import { decodeDataUrl } from "@/util/data-url"
import { Process } from "@/util"
import { Cause, Effect, Exit, Layer, Option, Scope, Context } from "effect"
import { EffectLogger } from "@/effect"
import { InstanceState } from "@/effect"
import { ActorTool, type ActorPromptOps } from "@/tool/actor"
import { SessionRunState } from "./run-state"
import { Goal } from "./goal"
import * as GoalState from "./goal-state"
import { TaskRegistry } from "@/task/registry"
import { EffectBridge } from "@/effect"
import { Team } from "@/team"
import { ActorRegistry } from "@/actor/registry"
import { ActorExecutionContext, type ActorExecutionIdentity } from "@/actor/turn"
import { ManagedProvider, type ManagedHostContext } from "@/acp/managed-provider"
import { SessionCwd } from "@/tool/session-cwd"
import { Metrics } from "@/metrics"
import { resolveInvocationStyle, type ToolStyleConfig } from "../tool/invocation-style"
import { ToolResultError } from "../tool/result-error"
import { RecoverableError } from "../tool/recoverable"
import {
  shouldAutoDream,
  shouldAutoDistill,
  DREAM_TASK,
  DISTILL_TASK,
  AUTO_DREAM_TITLE,
  AUTO_DISTILL_TITLE,
} from "./auto-dream"
import { randomUUID, createHash } from "node:crypto"
import { skillSearchReminderForSession } from "./skill-search-reminder"
import {
  createMcpToolSearchCatalog,
  mcpToolCatalogBudget,
  MCP_TOOL_SEARCH_ID,
  MCP_TOOL_SEARCH_MAX_LOADED,
  mcpToolSearchDescription,
  type McpToolSearchEntry,
  type McpToolSearchMetadata,
} from "@/tool/mcp-tool-search"
import { isMcpToolSearchEnabled } from "@/tool/gpt"
import { projectExecCapabilities } from "@/tool/tool-script"

// @ts-ignore
globalThis.AI_SDK_LOG_WARNINGS = false

// Recall-reminder hints, rendered in each tool's configured invocation style so
// shell-mode sessions never see a JSON-shaped example (which primes models to
// emit JSON and crash the shell parser). `memory` has no shell form, so it is
// always JSON. Exported for unit testing.
export function recallHintLines(toolCfg: ToolStyleConfig | undefined): string[] {
  const taskHint = resolveInvocationStyle(toolCfg, "task") === "shell" ? "- task list" : `- task({ operation: "list" })`
  const actorHint =
    resolveInvocationStyle(toolCfg, "actor") === "shell"
      ? "- actor status <actor_id>"
      : `- actor({ operation: "status", actor_id: "<id>" })`
  // memory has no shell form (no shell.parse) → always JSON.
  return [`- memory({ operation: "search", query: "<keyword>" })`, taskHint, actorHint]
}

// The orchestrator root session is PERSISTENT and coordinates many tasks over
// its lifetime, so its title must be stable and task-independent — it must not
// be renamed by the per-first-message auto-title generator as tasks come and
// go. Any root session driven by the orchestrator agent keeps this fixed name.
export const ORCHESTRATOR_TITLE = "Orchestrator"

// Returns the stable, task-independent title a root session should keep instead
// of a per-message auto-generated one, or undefined when normal auto-titling
// applies. Pure + exported for unit testing. `agent` is the triggering agent's
// name (e.g. "orchestrator"); `parentID` distinguishes root from child sessions.
export function stableRootTitle(input: {
  agent: string | undefined
  parentID: string | undefined
}): string | undefined {
  if (input.parentID) return undefined
  if (input.agent === "orchestrator") return ORCHESTRATOR_TITLE
  return undefined
}

/**
 * Cap on goal-driven main-loop re-entries per turn — the safety valve against
 * a never-satisfiable condition burning tokens forever. Higher than spawned
 * actors' MAX_PRE_REACT (=3) because main-session goals are usually larger.
 * TODO: lift to mimocode.json config (e.g. session.maxGoalReact).
 */

/**
 * Number of consecutive finished assistant steps with an identical action
 * signature that trips the repeated-step nudge. Three in a row is a strong
 * signal the model is stuck repeating itself rather than making progress.
 */
const REPEATED_STEP_THRESHOLD = 3

/**
 * Deterministic JSON serialization with sorted object keys, so that two
 * semantically-identical tool inputs produce the same string regardless of the
 * order the model happened to emit the keys in. `JSON.stringify` preserves
 * insertion order, and models routinely re-emit the same arguments with keys in
 * a different order (e.g. {url,format} vs {format,url}) — without this the
 * signatures would differ and the repeated-step check would miss real loops.
 */
function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null"
  if (Array.isArray(value)) return "[" + value.map(stableStringify).join(",") + "]"
  const keys = Object.keys(value as Record<string, unknown>).sort()
  return (
    "{" +
    keys.map((k) => JSON.stringify(k) + ":" + stableStringify((value as Record<string, unknown>)[k])).join(",") +
    "}"
  )
}

/**
 * Stable signature for an assistant step's *action* — the tool calls it made
 * (name + key-order-independent input). Text and reasoning are excluded on
 * purpose: in a ReAct loop the model narrates each step in slightly different
 * words while taking the exact same action, and some models emit their
 * reasoning as plain text parts — counting either would mask the repeated
 * action we want to catch. Returns undefined when a step makes no tool calls
 * (e.g. a pure-text turn), since there is no repeated *action* to compare.
 */
function stepSignature(parts: MessageV2.Part[]): string | undefined {
  const segments: string[] = []
  for (const part of parts) {
    if (part.type === "tool") {
      segments.push("tool:" + part.tool + ":" + stableStringify(part.state.input ?? {}))
    }
  }
  if (segments.length === 0) return undefined
  return segments.join("\n")
}

/**
 * Debounce decision for the high-context-pressure memory-flush nudge.
 *
 * Returns true if a nudge (a text part containing `marker`) has already been
 * injected within the *current high-pressure episode*, where the episode is the
 * message window since the last checkpoint boundary.
 *
 * Keying off the checkpoint boundary rather than a fixed message count is
 * deliberate: a single sustained high-pressure turn can emit many tool-call
 * steps — each its own message — so a fixed-size tail would let the
 * already-nudged message slide out of the window and re-fire the nudge
 * mid-turn. The boundary only advances when a checkpoint/rebuild actually
 * discards context, which is exactly when a fresh nudge becomes useful again.
 *
 * When `boundaryID` is undefined (no checkpoint yet) or is not found in `msgs`,
 * the whole conversation is treated as the current episode.
 */
export function nudgedSinceBoundary(
  msgs: readonly MessageV2.WithParts[],
  boundaryID: string | undefined,
  marker: string,
): boolean {
  const boundaryIdx = boundaryID ? msgs.findIndex((m) => m.info.id === boundaryID) : -1
  const episode = boundaryIdx >= 0 ? msgs.slice(boundaryIdx) : msgs
  return episode.some((m) => m.parts.some((p) => p.type === "text" && p.text?.includes(marker)))
}

const STRUCTURED_OUTPUT_DESCRIPTION = `Use this tool to return your final response in the requested structured format.

IMPORTANT:
- You MUST call this tool exactly once at the end of your response
- The input must be valid JSON matching the required schema
- Complete all necessary research and tool calls BEFORE calling this tool
- This tool provides your final answer - no further actions are taken after calling it`

const STRUCTURED_OUTPUT_SYSTEM_PROMPT = `IMPORTANT: The user has requested structured output. You MUST use the StructuredOutput tool to provide your final response. Do NOT respond with plain text - you MUST call the StructuredOutput tool with your answer formatted according to the schema.`

const PREDICT_SYSTEM = `You predict the single most likely next message a user will send to a coding assistant, based on the conversation so far. Output only that next message as one short, natural first-person request (what the user would type). No preamble, no quotes, no explanation, no markdown. Keep it under 100 characters.`

const PREDICT_NUDGE = `Based on the conversation above, write the user's most likely next message:`

const OUTPUT_LENGTH_CONTINUATION_LIMIT = Flag.MIMOCODE_OUTPUT_LENGTH_CONTINUATION_LIMIT
const INVALID_OUTPUT_CONTINUATION_LIMIT = Flag.MIMOCODE_INVALID_OUTPUT_CONTINUATION_LIMIT
const TEXT_TOOL_CALL_RETRY_LIMIT = Flag.MIMOCODE_TEXT_TOOL_CALL_RETRY_LIMIT

type GeneratedInputCandidate = {partID:PartID;position:number;digest:string;uploadID:string;originSHA256:string}
type UserSourceCandidate = {messageID:MessageID;partID:PartID | undefined;digest:string | undefined;layout?:SourceLayout;generated?:GeneratedInputCandidate[]}
// Process-local forward evidence only; the message object owns its lifetime.
const userSourceCandidates = new WeakMap<object,UserSourceCandidate>()
function userSourceSnapshot(messageID:MessageID, parts:MessageV2.Part[]) {
  if (!parts.length || parts.length > 128 || parts.some((part) => part.type !== "text")) return undefined
  const text = parts as MessageV2.TextPart[]
  const visible = text.filter((part) => !part.ignored && !part.synthetic)
  if (visible.length !== 1 || new Set(text.map((part)=>part.id)).size !== text.length) return undefined
  return {messageID, partID:visible[0].id, digest:createHash("sha256").update(visible[0].text,"utf8").digest("hex"),
    ordered:text.map((part)=>({id:part.id,ignored:!!part.ignored,synthetic:!!part.synthetic,digest:createHash("sha256").update(part.text,"utf8").digest("hex")}))}
}
function generatedInputSnapshot(parts:MessageV2.Part[]):GeneratedInputCandidate[] {
  if (parts.length>128 || new Set(parts.map(part=>part.id)).size!==parts.length) return []
  return parts.flatMap((part,position)=>{
    if (part.type!=='text' || part.synthetic || part.ignored) return []
    const candidate=part.metadata?.openclankGeneratedSummaryCandidate as {uploadID?:unknown;originSHA256?:unknown}|undefined
    if (!candidate || typeof candidate.uploadID!=='string' || typeof candidate.originSHA256!=='string') return []
    return [{partID:part.id,position,digest:createHash('sha256').update(part.text,'utf8').digest('hex'),uploadID:candidate.uploadID,originSHA256:candidate.originSHA256}]
  }).slice(0,10)
}

const log = Log.create({ service: "session.prompt" })

/** Existing actors keep their durable host binding across parent turns. */
export function hostContextForPrompt(
  actorHostContext: ManagedHostContext | undefined,
  managedHostContext: ManagedHostContext | undefined,
): ManagedHostContext | undefined {
  const selected = actorHostContext ?? managedHostContext
  if (!selected) return undefined
  const { ownerAccountID: _privateOwner, gcfArtifact: _privateGcfArtifact, shellRewriteAdmission: _privateRewrite, ...toolContext } = selected
  return toolContext
}

// Hooks are NOT listed here: the plugin layer detects hook file changes
// itself via mtime staleness checks (covers external editors too), so only
// tools and skills need the write/edit-triggered registry reload.
function isExtensionPath(filePath: string): boolean {
  return /\/\.mimocode\/(tools?|skills?)\//.test(filePath)
}
const elog = EffectLogger.create({ service: "session.prompt" })

export interface Interface {
  readonly cancel: (sessionID: SessionID) => Effect.Effect<void>
  readonly prompt: (input: PromptInput) => Effect.Effect<MessageV2.WithParts>
  readonly recovery: (input: { sessionID: SessionID; agentID?: string; allowBusy?: boolean }) => Effect.Effect<RecoveryCandidate[]>
  readonly resume: (input: ResumeTurnInput) => Effect.Effect<MessageV2.WithParts, unknown>
  readonly resumeBackground: (input: ResumeTurnInput) => Effect.Effect<void, unknown>
  readonly loop: (input: z.infer<typeof LoopInput>, sourceCandidate?: UserSourceCandidate) => Effect.Effect<MessageV2.WithParts>
  readonly shell: (input: ShellInput) => Effect.Effect<MessageV2.WithParts>
  readonly command: (input: CommandInput) => Effect.Effect<MessageV2.WithParts>
  readonly resolvePromptParts: (template: string) => Effect.Effect<PromptInput["parts"]>
  readonly sweepOrphanAssistants: (sessionID: SessionID, immediate?: boolean) => Effect.Effect<void>
  /** Compatibility hook for cron integrations that provide tool-part cleanup. */
  readonly sweepOrphanToolParts?: (sessionID: SessionID, immediate?: boolean) => Effect.Effect<void>
  /** Legacy cron fixture hook; title generation is owned by the prompt implementation. */
  readonly genTitle?: (...args: any[]) => Effect.Effect<{ title: string; status: string }>
  readonly predict: (input: { sessionID: SessionID }) => Effect.Effect<string>
}

export interface RecoveryCandidate {
  assistantMessageID: MessageID
  parentMessageID: MessageID
  created: number
}

export interface ResumeTurnInput {
  sessionID: SessionID
  assistantMessageID: MessageID
  agentID?: string
  task_id?: string
  titleLocale?: string
  model?: { providerID: string; modelID: string }
  managedRecovery?: { rootOperationID: string; handoffID: string; expectedExecutionRevision: number;
    turnPolicy: import("@/inbox/inbox.sql").UserInputTurnPolicy; managedRoute: ManagedProvider.RouteContext }
}

/** Persist only nonsecret evidence against an assistant created by this exact execution. */
export const persistManagedActorCapture = (
  sessions: Pick<Session.Interface, "messages" | "updateMessage">,
  identity: { sessionID: SessionID; actorID: string; executionRevision: number; rootOperationID?: string },
) => Effect.gen(function* () {
  const capture = yield* Effect.tryPromise({
    try: () => ManagedProvider.snapshotActorCapture(identity.sessionID, identity.actorID, identity.executionRevision),
    catch: (cause) => new Error(`managed capture snapshot failed: ${String(cause)}`),
  })
  const messages = yield* sessions.messages({ sessionID: identity.sessionID, agentID: identity.actorID })
  const target = messages.findLast((item) => {
    if (item.info.role !== "assistant") return false
    const execution = item.info.metadata?.managedExecution
    return execution?.sessionID === identity.sessionID && execution?.actorID === identity.actorID &&
      execution?.executionRevision === identity.executionRevision && execution?.rootOperationID === identity.rootOperationID
  })
  if (!target || target.info.role !== "assistant") return false
  yield* sessions.updateMessage({ ...target.info, metadata: { ...target.info.metadata, managedCapture: capture } })
  return true
})

/** Explicitly reconcile a prior exact managed execution; never replay work. */
export const reconcileManagedActorExecution = (
  registry: ActorRegistry.Interface,
  sessionID: SessionID,
  actorID: string,
  assertNotBusy: Effect.Effect<void>,
  hasOuter: (revision: number) => Effect.Effect<boolean>,
) => Effect.gen(function* () {
  yield* assertNotBusy
  const uncertain = yield* registry.uncertainManagedExecution(sessionID, actorID)
  if (uncertain) {
    if (yield* hasOuter(uncertain.executionRevision)) return yield* Effect.die(new Error("managed actor runner is still active"))
    const turn = { sessionID, actorID, executionRevision: uncertain.executionRevision,
      rootOperationID: uncertain.rootOperationID, innerTurnID: uncertain.innerTurnID }
    const read = () => ManagedProvider.managedHostCall("_openclank/session/v1/turn/status", {
      ...turn, ...(uncertain.rootSessionID ? { rootSessionID: uncertain.rootSessionID } : {}) })
    let status = yield* Effect.tryPromise({ try: read, catch: (cause) => new Error(`managed turn status unavailable: ${String(cause)}`) }).pipe(Effect.orDie)
    const same = (value: typeof status) => value.sessionID === turn.sessionID && value.actorID === turn.actorID &&
      value.executionRevision === turn.executionRevision && value.rootOperationID === turn.rootOperationID && value.innerTurnID === turn.innerTurnID
    if (!same(status) || status.state === "unknown") return yield* Effect.die(new Error("managed turn outcome remains unknown; reconciliation is required"))
    if (status.state === "active") {
      if (!(yield* registry.markExecutionUncertain(sessionID, actorID, uncertain.executionRevision,
        { rootOperationID:uncertain.rootOperationID, rootSessionID:uncertain.rootSessionID,
          innerTurnID:uncertain.innerTurnID, turnOutcome:"cancelled" })))
        return yield* Effect.die(new Error("managed turn recovery marker changed"))
      const finish = yield* Effect.tryPromise({
        try: () => ManagedProvider.managedHostCall("_openclank/session/v1/turn/finish", { ...turn, outcome: "cancelled" }),
        catch: (cause) => new Error(`managed turn cancellation outcome is unknown: ${String(cause)}`),
      }).pipe(Effect.exit)
      if (Exit.isFailure(finish) || !finish.value.accepted) {
        status = yield* Effect.tryPromise({ try: read, catch: (cause) => new Error(`managed turn status unavailable: ${String(cause)}`) }).pipe(Effect.orDie)
        if (!same(status) || status.state !== "finished") return yield* Effect.die(new Error("managed turn outcome remains unknown; reconciliation is required"))
      }
    }
    const identity = { rootOperationID: uncertain.rootOperationID, rootSessionID:uncertain.rootSessionID,
      innerTurnID: uncertain.innerTurnID, turnOutcome:status.state === "finished" ? status.outcome : "cancelled" as const }
    if (!(yield* registry.settleExecution(sessionID, actorID, uncertain.executionRevision, "cancelled", undefined, identity)))
      return yield* Effect.die(new Error("managed actor execution changed during reconciliation"))
  }
  const pending = yield* registry.pendingManagedSettlement(sessionID, actorID)
  if (!pending) return
  const reply = yield* Effect.tryPromise({
    try: () => ManagedProvider.finishActorExecution({
      sessionID, actorID, executionRevision: pending.executionRevision, rootOperationID: pending.rootOperationID,
    }, undefined, pending.rootSessionID && pending.innerTurnID && pending.turnOutcome && pending.executionOutcome
      ? {rootSessionID:pending.rootSessionID,innerTurnID:pending.innerTurnID,turnOutcome:pending.turnOutcome,executionOutcome:pending.executionOutcome} : undefined),
    catch: (cause) => new Error(`managed settlement remains uncertain: ${String(cause)}`),
  }).pipe(Effect.orDie)
  if (!reply.accepted || !(yield* registry.clearManagedSettlement(sessionID, actorID, pending.executionRevision, pending)))
    return yield* Effect.die(new Error("managed settlement remains uncertain"))
})

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionPrompt") {}

/** Resolve explicit command/profile references before falling back to the caller. */
export const resolveCommandTaskModel = Effect.fn("SessionPrompt.resolveCommandTaskModel")(function* (
  command: { model?: string; agent?: string },
  caller: { providerID: ProviderID; modelID: ModelID } | undefined,
  fallback: () => Effect.Effect<{ providerID: ProviderID; modelID: ModelID }>,
  services: { provider: Pick<Provider.Interface, "resolveModelRef">; agents: Pick<Agent.Interface, "get"> },
) {
  const { provider, agents } = services
  if (command.model) {
    const resolved = yield* provider.resolveModelRef(command.model, caller?.providerID)
    return { providerID: resolved.providerID, modelID: resolved.id }
  }
  if (command.agent) {
    const agent = yield* agents.get(command.agent)
    if (agent?.modelRef) {
      const resolved = yield* provider.resolveModelRef(agent.modelRef, caller?.providerID)
      return { providerID: resolved.providerID, modelID: resolved.id }
    }
    if (agent?.model) return agent.model
  }
  return caller ?? (yield* fallback())
})

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const bus = yield* Bus.Service
    const status = yield* SessionStatus.Service
    const sessions = yield* Session.Service
    const agents = yield* Agent.Service
    const provider = yield* Provider.Service
    const processor = yield* SessionProcessor.Service
    const prune = yield* SessionPrune.Service
    const checkpoint = yield* SessionCheckpoint.Service
    const compaction = yield* SessionCompaction.Service
    const config = yield* Config.Service
    const plugin = yield* Plugin.Service
    const commands = yield* Command.Service
    const permission = yield* Permission.Service
    const fsys = yield* AppFileSystem.Service
    const mcp = yield* MCP.Service
    const lsp = yield* LSP.Service
    const registry = yield* ToolRegistry.Service
    const truncate = yield* Truncate.Service
    const spawner = yield* ChildProcessSpawner.ChildProcessSpawner
    const scope = yield* Scope.Scope
    const instruction = yield* Instruction.Service
    const state = yield* SessionRunState.Service
    const goal = yield* Goal.Service

    const revert = yield* SessionRevert.Service
    const summary = yield* SessionSummary.Service
    const sys = yield* SystemPrompt.Service
    const llm = yield* LLM.Service
    const actorRegistry = yield* ActorRegistry.Service
    const inbox = yield* Inbox.Service
    const assertExecutionActive = (sessionID: SessionID, actorID = "main") => Effect.gen(function* () {
      if (!(yield* actorRegistry.assertDirectoryActive(sessionID))) return yield* Effect.die(new Error("Execution directory is retired"))
      const actor = yield* actorRegistry.get(sessionID, actorID)
      if (actor && !(yield* actorRegistry.assertDirectoryActive(sessionID, actorID))) return yield* Effect.die(new Error("Execution directory is retired"))
    })

    // The result is ephemeral: canonical parts and the retained archive stay raw.
    const prepareViews = (context:ToolViewContext | undefined, messages:MessageV2.WithParts[]) => Effect.gen(function* () {
      if (!context) return {views:new Map<string,string>(),reason:undefined as string | undefined}
      const writer=Option.getOrUndefined(yield* Effect.serviceOption(HistoryWriterService))
      const services=yield* Effect.context()
      return yield* Effect.promise(() => prepareToolViews({
        ...context,messages,current:()=>toolViewContextCurrent(context),
        ack:(scope,hash)=>writer ? Effect.runPromiseWith(services)(writer.sourceAck(scope.sessionID,scope.actorID,scope.partID,hash)) : Promise.resolve(undefined),
        check:async (sources)=>{
          const identity=context.identity
          if (!identity.rootOperationID || !identity.innerTurnID) return false
          let timer:ReturnType<typeof setTimeout> | undefined
          try {
            const result=await Promise.race([
              ManagedProvider.historySourcesCurrent({sessionID:identity.sessionID,operation:'check_sources',rootOperationID:identity.rootOperationID,actorID:identity.actorID,
                executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID,sources}),
              new Promise<false>((resolve)=>{timer=setTimeout(()=>resolve(false),750)}),
            ])
            return result===true
          } finally {if(timer) clearTimeout(timer)}
        },
      }))
    })

    // Track sessions that have already shown the "loaded instructions" toast so we
    // surface it once per primary session rather than on every run-loop turn.
    const instructionsNotified = new Set<SessionID>()

    // Late-bind prefix-capture helper so SessionCheckpoint.tryStartCheckpointWriter
    // can call buildLLMRequestPrefix without forming a layer cycle
    // (ToolRegistry → SessionCheckpoint → ToolRegistry). See prefix-capture-ref.ts.
    // The closure resolves Agent.Info and Provider.Model internally so checkpoint.ts
    // only needs to pass string IDs.
    const capture: typeof prefixCaptureRef.current = (input) =>
      Effect.gen(function* () {
        const ag = yield* agents.get(input.agentName)
        if (!ag) return yield* Effect.fail(new Error(`prefix capture agent is unavailable: ${input.agentName}`))
        const model = yield* provider
          .getModel(input.providerID as ProviderID, input.modelID as ModelID)
        if (!model) return yield* Effect.fail(new Error(`prefix capture model is unavailable: ${input.providerID}/${input.modelID}`))
        // Anchor the env date to the session's creation time so the captured prefix is
        // byte-identical to the runLoop's (which uses session.time.created), preserving
        // Anthropic cache parity. If the session can't be loaded we cannot assert that
        // parity, so full-context capture fails rather than emitting an empty prefix.
        const captureSession = yield* sessions.get(input.sessionID)
        const captureMessages = input.msgs as Parameters<typeof buildLLMRequestPrefix>[0]["msgs"]
        const captureViews = yield* prepareViews(input.toolViewContext, captureMessages)
        const captureReanchor = !input.toolViewContext || !!captureViews.reason
        if (captureReanchor) log.info('checkpoint prefix uses a raw reanchor', {reason:captureViews.reason ?? 'no_live_admission'})
        const captureViewIdentity = captureViews.views.size ? PrefixProvenance.digest(JSON.stringify([...captureViews.views]))
          : captureViews.reason ? `raw:${captureViews.reason}` : undefined
        const captureUser = captureMessages.findLast((message) => message.info.role === "user")
        if (!captureUser || captureUser.info.role !== "user")
          return yield* Effect.fail(new Error("prefix capture requires an existing user message"))
        const runtimePermission = Agent.runtimePermission(ag, captureSession.permission)
        const captureRoot = captureUser.parts.find((part) => part.type === 'text' && typeof part.metadata?.hostSourceMessageID === 'string')
        const captureGcfContext = ManagedProvider.currentHostContext(input.sessionID)
        const captureArtifact = model.capabilities.input.text && captureGcfContext?.gcfArtifact?.availability() === "available" ? captureGcfContext.gcfArtifact : undefined
        const captureArtifactIdentity = captureArtifact?.identity()
        const captureGcf = captureArtifactIdentity ? gcfViewIdentity(captureGcfContext?.gcfAdmission,
          input.sessionID, captureRoot?.type === 'text' ? captureRoot.metadata?.hostSourceMessageID as string | undefined : undefined,
          {providerID:model.providerID,modelID:model.id},captureArtifactIdentity) : undefined
        const captureGcfCurrent = () => !captureGcf || (captureArtifact?.identity() === captureArtifactIdentity &&
          ManagedProvider.currentHostContext(input.sessionID)?.gcfArtifact === captureArtifact &&
          ManagedProvider.currentHostContext(input.sessionID)?.gcfAdmission === captureGcfContext?.gcfAdmission)
        const profileKey = SessionPrefixSnapshot.profileKey({
          providerID: model.providerID,
          modelID: model.id,
          agent: ag.name,
          agentID: captureUser.info.agentID ?? "main",
          harness: "default",
          systemMode: "append",
          system: captureUser.info.system ?? "",
          format: captureUser.info.format ?? { type: "text" },
          permission: runtimePermission,
          ...(captureGcf ? { gcfViewIdentity:captureGcf } : {}),
          ...(captureViewIdentity ? {toolViewIdentity:captureViewIdentity} : {}),
        })
        // Fork capture preserves the historical parent prefix as evidence;
        // it does not authorize a later child model or tool admission.
        const frozen = captureReanchor || captureGcf ? undefined : yield* SessionPrefixSnapshot.get(input.sessionID, profileKey)
        const [skills, env, instructions] = frozen
          ? [undefined, [] as string[], { content: [] as string[] }]
          : yield* Effect.all([
              sys.skills(ag, model, captureSession.permission),
              sys.environment(model, captureSession.time.created),
              instruction.system().pipe(Effect.orDie),
            ])
        // (checkpoint-writer never requests json_schema output, so STRUCTURED_OUTPUT_SYSTEM_PROMPT
        // is not included; parent's runLoop adds it conditionally based on user.format)
        const additions = [...env, ...(skills ? [skills] : []), ...instructions.content]
        const prefix = yield* buildLLMRequestPrefix({
          sessionID: input.sessionID,
          agent: ag,
          model,
          msgs: captureMessages,
          additions,
          gcfViewIdentity: captureGcf,
          gcfArtifact: captureArtifact,
          toolViews: captureViews.views,
        }).pipe(
          Effect.provideService(LLM.Service, llm),
          Effect.provideService(ToolRegistry.Service, registry),
        )
        if (!captureGcfCurrent()) log.info('checkpoint prefix uses a raw reanchor', {reason:'gcf_unavailable'})
        const captureCurrent = captureGcfCurrent() && (!captureViews.views.size || (!!input.toolViewContext && toolViewContextCurrent(input.toolViewContext)))
        if (!captureCurrent && input.toolViewContext) {
          input.toolViewContext.control.disabled=true
          input.toolViewContext.control.reason ??= 'scope_unavailable'
          log.warn('checkpoint prefix uses a raw reanchor', {reason:'scope_unavailable'})
        }
        const capturedMessages = captureCurrent ? prefix.inheritedMessages : yield* MessageV2.toModelMessagesEffect(captureMessages,model,{gcfViewIdentity:captureGcf})
        return {
          ...prefix,
          inheritedMessages: capturedMessages,
          system: captureCurrent ? (frozen?.system ?? prefix.system) : prefix.system,
          // A checkpoint fork must advertise the same schemas as the durable
          // prefix it inherited. These restored definitions deliberately have
          // no execute closures, so capture cannot create another authority.
          tools: captureCurrent && frozen?.tools ? SessionPrefixSnapshot.restoreTools(frozen.tools) : prefix.tools,
          parentPermission: ag.permission,
        }
      }).pipe(Effect.catchCause((cause) => Effect.fail(new Error(`prefix capture failed: ${String(Cause.squash(cause))}`))))
    prefixCaptureRef.current = capture
    yield* Effect.addFinalizer(() =>
      Effect.sync(() => {
        if (prefixCaptureRef.current === capture) prefixCaptureRef.current = undefined
      }),
    )

    const runner = Effect.fn("SessionPrompt.runner")(function* () {
      return yield* EffectBridge.make()
    })
    const ops = Effect.fn("SessionPrompt.ops")(function* () {
      const run = yield* runner()
      return {
        cancel: (sessionID: SessionID) => run.fork(cancel(sessionID)),
        resolvePromptParts: (template: string) => resolvePromptParts(template),
        prompt: (input: PromptInput) => prompt(input),
      } satisfies ActorPromptOps
    })

    const cancel = Effect.fn("SessionPrompt.cancel")(function* (sessionID: SessionID) {
      yield* elog.info("cancel", { sessionID })
      const main = yield* actorRegistry.get(sessionID, "main").pipe(Effect.orElseSucceed(() => undefined))
      if (main?.status === "running" && spawnRef.current) {
        // Main receipt runs are owned by Actor.forkWork so hooks and completion
        // gates share the same cancellation/settlement boundary.
        yield* spawnRef.current.cancel(sessionID, "main", "forced")
      } else {
        yield* state.cancel(sessionID)
      }
    })

    // Shared rebuild-from-checkpoint step used by BOTH the automatic overflow
    // path in runLoop and the manual `/rebuild` command, so the two can never
    // drift in logic or boundary conditions. Inserts a checkpoint boundary
    // marker (never deletes DB messages) at the current watermark so the next
    // runLoop iteration rebuilds context from the on-disk checkpoint while the
    // live message tail after the watermark is preserved verbatim. Does NOT
    // block on an in-flight writer (same policy as the auto path — a slightly
    // stale checkpoint now beats a fresh one that never arrives). Returns true
    // iff a boundary was inserted (i.e. a usable checkpoint existed); callers
    // fall back to compaction when it returns false.
    const rebuildFromCheckpoint = Effect.fn("SessionPrompt.rebuildFromCheckpoint")(function* (input: {
      sessionID: SessionID
      msgs: MessageV2.WithParts[]
      agentID?: string
      agent: string
      model: { providerID: string; id: string }
    }) {
      const hasCP = yield* checkpoint.hasCheckpoint(input.sessionID).pipe(Effect.catch(() => Effect.succeed(false)))
      if (!hasCP) return false

      const boundary = yield* checkpoint
        .lastBoundary(input.sessionID)
        .pipe(Effect.catch(() => Effect.succeed(undefined)))
      if (!boundary) return false

      const boundaryMsg = input.msgs.find((m) => m.info.id === boundary)
      const inserted = yield* checkpoint
        .insertRebuildBoundary({
          sessionID: input.sessionID,
          boundary,
          lastMessageInfo: computeLastMessageInfo(input.msgs.map((m) => m.info)),
          // Freeze the exact tail represented by the activity digest. Messages
          // created after this insert remain live in the next model request.
          digestUpTo: input.msgs.at(-1)?.info.id,
          agentID: input.agentID,
          agent: input.agent,
          model: { providerID: input.model.providerID, modelID: input.model.id },
          boundaryCreatedAt: boundaryMsg?.info.time.created,
        })
        .pipe(Effect.catch(() => Effect.succeed(false)))

      if (inserted) yield* prune.resetThresholds(input.sessionID)
      return inserted
    })

    type RebuildAttempt = "rebuilt" | "writer-failed" | "insert-failed" | "memory-write-off"
    const MANUAL_WRITER_WAIT_MS = 300_000

    // Shared by the manual /rebuild command and the auto overflow path: try the
    // on-disk checkpoint first, then start a writer and wait (bounded) when none
    // exists yet. Returns the single outcome every caller reports.
    const rebuildEnsuringCheckpoint = Effect.fn("SessionPrompt.rebuildEnsuringCheckpoint")(function* (input: {
      sessionID: SessionID
      msgs: MessageV2.WithParts[]
      agentID?: string
      agent: string
      model: { providerID: string; id: string }
      writerWaitMs: number
      onWaitingForWriter?: Effect.Effect<void>
    }) {
      if (!isMemoryWriteEnabled(yield* config.get())) return "memory-write-off" as const

      if (yield* rebuildFromCheckpoint(input).pipe(Effect.catch(() => Effect.succeed(false))))
        return "rebuilt" as const

      const hasCP = yield* checkpoint.hasCheckpoint(input.sessionID).pipe(Effect.catch(() => Effect.succeed(false)))
      const boundary = hasCP
        ? yield* checkpoint.lastBoundary(input.sessionID).pipe(Effect.catch(() => Effect.succeed(undefined)))
        : undefined
      if (hasCP && boundary) return "insert-failed" as const

      const writerRunning = yield* checkpoint
        .isWriterRunning(input.sessionID)
        .pipe(Effect.catch(() => Effect.succeed(false)))
      if (!writerRunning) {
        yield* checkpoint
          .tryStartCheckpointWriter({
            sessionID: input.sessionID,
            model: { providerID: input.model.providerID, modelID: input.model.id },
            promptOps: {} as never,
          })
          .pipe(Effect.catch(() => Effect.succeed<"started" | "queued" | "skipped">("skipped")))
      }

      if (input.onWaitingForWriter) yield* input.onWaitingForWriter

      const writerOutcome = yield* checkpoint
        .waitForWriter(input.sessionID)
        .pipe(
          Effect.timeout(input.writerWaitMs),
          Effect.catch(() => Effect.succeed<"success" | "failure" | "no-writer">("failure")),
        )
      if (writerOutcome !== "success") return "writer-failed" as const

      if (yield* rebuildFromCheckpoint(input).pipe(Effect.catch(() => Effect.succeed(false))))
        return "rebuilt" as const
      return "insert-failed" as const
    })

    const resolvePromptParts = Effect.fn("SessionPrompt.resolvePromptParts")(function* (template: string) {
      const ctx = yield* InstanceState.context
      const parts: PromptInput["parts"] = [{ type: "text", text: template }]
      const files = ConfigMarkdown.files(template)
      const seen = new Set<string>()
      yield* Effect.forEach(
        files,
        Effect.fnUntraced(function* (match) {
          const name = match[1]
          if (seen.has(name)) return
          seen.add(name)
          const filepath = name.startsWith("~/")
            ? path.join(os.homedir(), name.slice(2))
            : path.resolve(ctx.worktree, name)

          const info = yield* fsys.stat(filepath).pipe(Effect.option)
          if (Option.isNone(info)) {
            const found = yield* agents.get(name)
            if (found) parts.push({ type: "agent", name: found.name })
            return
          }
          const stat = info.value
          parts.push({
            type: "file",
            url: pathToFileURL(filepath).href,
            filename: name,
            mime: stat.type === "Directory" ? "application/x-directory" : "text/plain",
          })
        }),
        { concurrency: "unbounded", discard: true },
      )
      return parts
    })

    const title = Effect.fn("SessionPrompt.ensureTitle")(function* (input: {
      session: Session.Info
      agent: string | undefined
      history: MessageV2.WithParts[]
      providerID: ProviderID
      modelID: ModelID
      titleLocale?: string
      titleText?: string
    }) {
      if (input.session.parentID) return

      // Persistent orchestrator root session: keep a stable, task-independent
      // title. Set it once (if still the default) and SKIP the per-first-message
      // LLM title generation so later tasks never rename it.
      const stable = stableRootTitle({ agent: input.agent, parentID: input.session.parentID })
      if (stable) {
        if (Session.isDefaultTitle(input.session.title))
          yield* sessions
            .setTitleIfDefault({
              sessionID: input.session.id,
              title: stable,
              expectedRevision: input.session.titleRevision,
              source: "fallback",
            })
            .pipe(
              Effect.catchCause((cause) => elog.error("failed to set stable title", { error: Cause.squash(cause) })),
            )
        return
      }

      if (!Session.isDefaultTitle(input.session.title) && input.session.titleSource !== "fallback") return

      const real = (m: MessageV2.WithParts) =>
        m.info.role === "user" && !m.parts.every((p) => "synthetic" in p && p.synthetic)
      const idx = input.history.findIndex(real)
      if (idx === -1) return

      const firstUser = input.history[idx]
      if (!firstUser || firstUser.info.role !== "user") return
      const parts: GenTitlePart[] = firstUser.parts.flatMap((part): GenTitlePart[] => {
        if (part.type === "text" && !part.ignored && !part.synthetic) return [{ type: "text", text: part.text }]
        if (part.type === "subtask") return [{ type: "text", text: part.prompt }]
        if (part.type === "file" && !("synthetic" in part && part.synthetic))
          return [{ type: "image", data: "", mime: part.mime, filename: part.filename }]
        return []
      })
      const source = input.titleText?.trim()
        ? GenTitle.titleInputText(input.titleText, [])
        : GenTitle.titleInputText(
        undefined,
        parts.map((part) =>
          part.type === "text"
            ? { ...part, text: part.text.replace(/(^|\n)\/[A-Za-z0-9][A-Za-z0-9:_-]*[ \t]+/g, "$1") }
            : part,
        ),
          )
      const fallbackLine = source.split("\n").map((line) => line.trim()).find(Boolean) ?? "Untitled"
      const fallback = Array.from(fallbackLine).length <= GenTitle.TITLE_MAX_LENGTH
        ? fallbackLine
        : Array.from(fallbackLine).slice(0, GenTitle.TITLE_MAX_LENGTH - 1).join("").trimEnd() + "…"
      const current = yield* sessions.get(input.session.id)
      if (current.titleSource !== "fallback") return
      const result = yield* GenTitle.genTitle({
        text: source,
        parts: [],
        sessionID: current.id,
        providerID: input.providerID,
        model: { providerID: input.providerID, modelID: input.modelID },
        locale: input.titleLocale,
      }).pipe(
        Effect.provideService(Agent.Service, agents),
        Effect.provideService(Provider.Service, provider),
        Effect.provideService(LLM.Service, llm),
        Effect.provideService(Config.Service, config),
        Effect.catchCause((cause) =>
          Cause.hasInterrupts(cause)
            ? Effect.interrupt
            : elog.warn("failed to generate title", { error: Cause.squash(cause) }).pipe(Effect.as({ title: fallback, status: "fallback" as const })),
        ),
      )
      if (result.status !== "generated") return
      yield* sessions.setTitleIfDefault({ sessionID: current.id, title: result.title, expectedRevision: current.titleRevision, source: "generated" })
    })

    const predict = Effect.fn("SessionPrompt.predict")(function* (input: { sessionID: SessionID }) {
      const cfg = yield* config.get()
      // Optional extra model call: opt-in only (Off unless explicitly enabled).
      if (cfg.experimental?.predict_next_prompt !== true) return ""

      const history = yield* sessions.messages({ sessionID: input.sessionID, agentID: "main" })
      const real = (m: MessageV2.WithParts) =>
        m.info.role === "user" && !m.parts.every((p) => "synthetic" in p && p.synthetic)
      const userIdx = history.findLastIndex(real)
      if (userIdx === -1) return ""
      const lastUser = history[userIdx]
      if (lastUser.info.role !== "user") return ""

      // Only the assistant turn that actually answered this user message counts.
      // Bail if that turn is still running (an incomplete assistant after it),
      // so we never pair the newest prompt with a stale/older result.
      const assistants = history
        .slice(userIdx + 1)
        .filter((m): m is MessageV2.WithParts & { info: MessageV2.Assistant } => m.info.role === "assistant")
      if (assistants.length === 0) return ""
      if (assistants.some((m) => m.info.time.completed === undefined)) return ""
      const lastAssistant = assistants[assistants.length - 1]

      // Context fed to the prediction: up to 3 most recent user queries
      // (chronological) plus the latest assistant turn (which carries tool
      // outputs + final assistant text). Earlier assistant turns are dropped
      // to keep the prompt small.
      const recentUsers = history.filter(real).slice(-3)
      const contextMsgs = [...recentUsers, lastAssistant]

      const base = yield* agents.get("title")
      if (!base) return ""
      const mdl = base.modelRef
        ? yield* provider.resolveModelRef(base.modelRef, lastAssistant.info.providerID)
        : base.model
          ? yield* provider.getModel(base.model.providerID, base.model.modelID)
          : ((yield* provider.getSmallModel(lastAssistant.info.providerID, yield* provider.getModel(lastAssistant.info.providerID, lastAssistant.info.modelID))) ??
            (yield* provider.getModel(lastAssistant.info.providerID, lastAssistant.info.modelID)))

      // Side-channel call: bypass llm.stream so prediction stays out of the
      // session trajectory and never triggers session-coupled plugin hooks
      // (chat.params, chat.headers, system.transform, memory instructions,
      // x-session-affinity). Still publishes Metrics.ModelCall so the
      // prediction cost shows up in analytics.
      const msgs = yield* MessageV2.toModelMessagesEffect(contextMsgs, mdl, { stripMedia: true })
      const language = yield* provider.getLanguage(mdl)
      const wrapped = wrapLanguageModel({
        model: language,
        middleware: [
          {
            specificationVersion: "v3" as const,
            async transformParams(args) {
              if (args.type === "generate" || args.type === "stream") {
                // @ts-expect-error
                args.params.prompt = ProviderTransform.message(args.params.prompt, mdl, {})
              }
              return args.params
            },
          },
        ],
      })
      const started = Date.now()
      const result = yield* Effect.tryPromise(() =>
        generateText({
          model: wrapped,
          system: PREDICT_SYSTEM,
          messages: [...msgs, { role: "user", content: PREDICT_NUDGE }],
          maxOutputTokens: ProviderTransform.maxOutputTokens(mdl),
          temperature: mdl.capabilities.temperature ? 0.7 : undefined,
          providerOptions: ProviderTransform.providerOptions(mdl, ProviderTransform.smallOptions(mdl)),
          headers: {
            ...mdl.headers,
            "User-Agent": `mimocode/${InstallationVersion}`,
          },
          maxRetries: 1,
        }),
      ).pipe(
        Effect.catchCause((cause) =>
          elog.warn("predict failed", { error: Cause.pretty(cause) }).pipe(Effect.as(undefined)),
        ),
      )
      if (!result) return ""

      const u = Session.getUsage({ model: mdl, usage: result.usage, metadata: result.providerMetadata })
      yield* bus
        .publish(Metrics.ModelCall, {
          sessionID: input.sessionID,
          finish_reason: result.finishReason,
          latency_ms: Date.now() - started,
          cached_read_tokens: u.tokens.cache.read,
          model_id: mdl.id,
          provider: mdl.providerID,
          total_tokens_in: u.tokens.input + u.tokens.cache.read + u.tokens.cache.write,
          total_tokens_out: u.tokens.output + u.tokens.reasoning,
        })
        .pipe(Effect.ignore)

      const cleaned = result.text
        .replace(/<think>[\s\S]*?<\/think>\s*/g, "")
        .split("\n")
        .map((line) => line.trim())
        .find((line) => line.length > 0)
      if (!cleaned) return ""
      const stripped = cleaned.replace(quoteTrimRegex, "")
      return stripped.length > 120 ? stripped.substring(0, 117) + "..." : stripped
    })

    const insertReminders = Effect.fn("SessionPrompt.insertReminders")(function* (input: {
      messages: MessageV2.WithParts[]
      agent: Agent.Info
      model: Provider.Model
      session: Session.Info
      sourceAttempt?:SourceAttemptHandle
    }) {
      const userMessage = input.messages.findLast((msg) => msg.info.role === "user")
      if (!userMessage) return input.messages

      // Search reminders apply only to eligible direct user sessions and models.
      // They advise the primary agent when to search; the model still decides whether to call.
      const reminder = skillSearchReminderForSession(input)
      if (reminder) {
        const part = yield* sessions.updatePart({
          id: PartID.ascending(),
          messageID: userMessage.info.id,
          sessionID: userMessage.info.sessionID,
          type: "text",
          text: reminder,
          synthetic: true,
        })
        userMessage.parts.push(part)
        registerSkillReminder(input.sourceAttempt,part)
      }

      const composeModeMsg = input.messages.find((msg) => msg.info.role === "user" && msg.info.agent === "compose")
      if (composeModeMsg) {
        const ctx = yield* InstanceState.context
        const composeCfg = (yield* config.get()).compose
        const docsDir = ConfigCompose.resolveDocsDir(ctx.worktree, composeCfg)
        const text = PROMPT_COMPOSE.replace(
          "{{compose_docs_dir}}",
          `Save compose skill outputs: specs in \`${path.join(docsDir, "specs")}\`, plans in \`${path.join(docsDir, "plans")}\`, reports in \`${path.join(docsDir, "reports")}\`.`,
        )
        composeModeMsg.parts.unshift({
          id: PartID.ascending(),
          messageID: composeModeMsg.info.id,
          sessionID: composeModeMsg.info.sessionID,
          type: "text",
          text,
          synthetic: true,
        })
      }

      const assistantMessage = input.messages.findLast((msg) => msg.info.role === "assistant")
      if (!Flag.MIMOCODE_DISABLE_BUILTIN_SKILLS && !Flag.MIMOCODE_DISABLE_OFFICIAL_SKILLS) {
        const fileCandidates = userMessage.parts.flatMap((p) => {
          if (p.type !== "file") return []
          const filenameFromSource =
            p.source?.type === "file" && p.source.path ? path.basename(p.source.path) : undefined
          return [{ mime: p.mime, filename: p.filename ?? filenameFromSource }]
        })
        const skills = matchDocumentSkills(fileCandidates)
        if (skills.length > 0) {
          const root = builtinSkillRoot()
          const entries = skills.map((skill) => `- ${skill}: ${path.join(root, skill, "SKILL.md")}`).join("\n")
          const part = yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: userMessage.info.id,
            sessionID: userMessage.info.sessionID,
            type: "text",
            text: `<system-reminder>
The user's message attaches office document file(s). The following built-in skill(s) may be relevant for producing, reading, or transforming these files. You are recommended to consult the SKILL.md when it fits the task — prefer using these skills over ad-hoc approaches when applicable:
${entries}
</system-reminder>`,
            synthetic: true,
          })
          userMessage.parts.push(part)
        }
      }

      // Sole injection point for skill bodies — free-text mentions ("/foo ... /bar") and slash-command
      // invocations alike. Runs every step, so the guard keeps step 2+ from restacking step 1's blocks.
      // User slash/mention is a real user-originated explicit signal: gate every injection through
      // evaluateInvocation/getForInvocation("explicit"). Skill.all is NOT a trust filter.
      const alreadyWrapped = userMessage.parts.some(
        (p) => p.type === "text" && p.text.startsWith('<skill_content name="'),
      )
      if (!alreadyWrapped) {
        // Same synthetic-filtered extraction as isUserExplicitRequest / userSlashMentions:
        // a synthetic /name is not a user-typed mention and must not drive body injection.
        const mentioned: string[] = []
        const seen = new Set<string>()
        for (const name of Skill.userSlashMentions(input.messages)) {
          if (seen.has(name)) continue
          seen.add(name)
          const resolved = yield* sys.getForInvocation(name, "explicit")
          if (!resolved.ok) continue
          mentioned.push(name)
        }

        if (mentioned.length > 0) {
          const MAX_AUTOLOAD = 3
          const toLoad = mentioned.slice(0, MAX_AUTOLOAD)
          const overflow = mentioned.slice(MAX_AUTOLOAD)
          const loaded: string[] = []
          for (const name of toLoad) {
            const resolved = yield* sys.getForInvocation(name, "explicit")
            if (!resolved.ok) continue
            // An explicit mention is not permission to bypass the canonical
            // skill rule or to use a catalogue body whose source has changed.
            if (Permission.evaluate("skill", resolved.info.name, Agent.runtimePermission(input.agent, input.session.permission)).action !== "allow") continue
            const execution = Option.getOrUndefined(yield* Effect.serviceOption(ActorExecutionContext))
            if (!(yield* Effect.promise(() => Skill._prepareBody(resolved.info, input.session.id, execution)))) continue
            const part = yield* sessions.updatePart({
              id: PartID.ascending(),
              messageID: userMessage.info.id,
              sessionID: userMessage.info.sessionID,
              type: "text",
              text: `<skill_content name="${name}">\n${resolved.info.content}\n</skill_content>`,
              synthetic: true,
            })
            userMessage.parts.push(part)
            loaded.push(name)
          }

          if (mentioned.length >= 2) {
            const loadedHint =
              loaded.length > 0 ? `SKILL.md for [${loaded.join(", ")}] has been auto-loaded above.` : ""
            const overflowHint =
              overflow.length > 0 ? `For [${overflow.join(", ")}], use the Skill tool to load them on demand.` : ""
            const part = yield* sessions.updatePart({
              id: PartID.ascending(),
              messageID: userMessage.info.id,
              sessionID: userMessage.info.sessionID,
              type: "text",
              text: `<system-reminder>
The user has explicitly referenced multiple skills in this message: ${mentioned.join(", ")}.
${loadedHint} ${overflowHint}

Before starting work, complete an orchestration plan:
1. Read the SKILL.md of every referenced skill FIRST, then plan (never plan from skill descriptions alone — the full SKILL.md may contain constraints that invalidate an imagined workflow)
2. Classify the composition relationship: pipeline (A's output → B's input) / parallel (each handles a separate part) / constraint overlay (one does the work, the other provides rules or standards)
3. If pipeline: define the interface contract for intermediate artifacts — format and file path
4. If two skills give instructions on the same dimension (output format / style / process), explicitly declare a conflict resolution rule: which skill takes precedence on which dimension
5. Output a concise workflow (phase → skill used → artifact), then execute according to it

Keep planning proportional to task complexity: for simple combinations, two or three sentences suffice.
</system-reminder>`,
              synthetic: true,
            })
            userMessage.parts.push(part)
          }
        }
      }

      if (input.agent.name !== "plan" && assistantMessage?.info.agent === "plan") {
        const plan = Session.plan(input.session)
        if (!(yield* fsys.existsSafe(plan))) return input.messages
        const part = yield* sessions.updatePart({
          id: PartID.ascending(),
          messageID: userMessage.info.id,
          sessionID: userMessage.info.sessionID,
          type: "text",
          text: `${BUILD_SWITCH}\n\nA plan file exists at ${plan}. You should execute on the plan defined within it`,
          synthetic: true,
        })
        userMessage.parts.push(part)
        return input.messages
      }

      if (input.agent.name !== "plan") return input.messages

      if (assistantMessage?.info.agent === "plan") {
        // Only on a fresh user turn: at step 1 the user message is the last
        // message; at step 2+ this turn's own assistant message follows it.
        // insertReminders runs every step and updatePart persists, so
        // injecting past step 1 would stack duplicate reminders.
        if (input.messages.at(-1) !== userMessage) return input.messages
        const plan = Session.plan(input.session)
        const part = yield* sessions.updatePart({
          id: PartID.ascending(),
          messageID: userMessage.info.id,
          sessionID: userMessage.info.sessionID,
          type: "text",
          text: `<system-reminder>Plan mode is still active (read-only; only writable file: ${plan}). Do NOT implement. End your turn with the question tool or plan_exit.</system-reminder>`,
          synthetic: true,
        })
        userMessage.parts.push(part)
        return input.messages
      }

      const plan = Session.plan(input.session)
      const exists = yield* fsys.existsSafe(plan)
      if (!exists) yield* fsys.ensureDir(path.dirname(plan)).pipe(Effect.catch(Effect.die))
      const part = yield* sessions.updatePart({
        id: PartID.ascending(),
        messageID: userMessage.info.id,
        sessionID: userMessage.info.sessionID,
        type: "text",
        text: `<system-reminder>
Plan mode is active. The user wants you to research and design, NOT to execute yet. This supersedes any other instructions you have received.

## What you SHOULD do (recommended)
- Prefer the dedicated read-only tools for everything they cover — \`read\` (view files), \`grep\` (search contents), \`glob\` (find files), and the \`lsp\` tools (definitions, references, diagnostics). These are the right way to explore the code.
- Spawn \`explore\`/\`general\` subagents for parallel research.
- Only when those tools genuinely can't get what you need, you MAY use \`bash\` for the gap — but ONLY for commands you are certain are a pure read with NO side effects (e.g. \`git status\`/\`log\`/\`diff\`, listing dependencies). Do NOT reach for \`bash\` to do what \`read\`/\`grep\`/\`glob\` already do.

## What you MUST NOT do
- Do NOT edit or create any file other than the plan file below. Writes to non-plan files are blocked outright and will fail — do not attempt them and do not ask the user to approve them.
- Do NOT run \`test\`, \`lint\`, \`typecheck\`, \`build\`, or similar project commands. These are NOT safe by default: \`lint\` is often configured with \`--fix\`, \`test\` may write snapshots or touch a database, \`build\` writes artifacts, and scripts behind them can do anything. The ONLY exception is if you have explicitly verified — by reading the exact command/config — that this specific invocation has no side effects (no \`--fix\`/\`--write\`, no file/state/db mutation). If you cannot verify that, treat it as forbidden and note it in the plan instead.
- Do NOT run any other side-effecting \`bash\`: no commits, no \`git push\`, no installing/removing packages, no writing/moving/deleting files, no changing configs, no \`change_directory\`, no \`workflow\`.
- If you find yourself wanting to mutate something to make progress, that's a signal to write it into the plan instead and continue researching read-only.

Use good judgment: take the read-only action yourself rather than pushing avoidable confirmation prompts onto the user. Only the plan file is writable.

## Plan File Info:
${exists ? `A plan file already exists at ${plan}. You can read it and make incremental edits using the edit tool.` : `No plan file exists yet. You should create your plan at ${plan} using the write tool.`}
You should build your plan incrementally by writing to or editing this file. NOTE that this is the only file you are allowed to edit - other than this you are only allowed to take READ-ONLY actions.

## Plan Workflow

### Phase 1: Initial Understanding
Goal: Gain a comprehensive understanding of the user's request by reading through code and asking them questions. Critical: In this phase you should only use the explore subagent type.

1. Focus on understanding the user's request and the code associated with their request

2. **Launch up to 3 explore agents IN PARALLEL** (single message, multiple tool calls) to efficiently explore the codebase.
 - Use 1 agent when the task is isolated to known files, the user provided specific file paths, or you're making a small targeted change.
 - Use multiple agents when: the scope is uncertain, multiple areas of the codebase are involved, or you need to understand existing patterns before planning.
 - Quality over quantity - 3 agents maximum, but you should try to use the minimum number of agents necessary (usually just 1)
 - If using multiple agents: Provide each agent with a specific search focus or area to explore. Example: One agent searches for existing implementations, another explores related components, a third investigates testing patterns

3. After exploring the code, use the question tool to clarify ambiguities in the user request up front.

### Phase 2: Design
Goal: Design an implementation approach.

Launch general agent(s) to design the implementation based on the user's intent and your exploration results from Phase 1.

You can launch up to 1 agent(s) in parallel.

**Guidelines:**
- **Default**: Launch at least 1 Plan agent for most tasks - it helps validate your understanding and consider alternatives
- **Skip agents**: Only for truly trivial tasks (typo fixes, single-line changes, simple renames)

Examples of when to use multiple agents:
- The task touches multiple parts of the codebase
- It's a large refactor or architectural change
- There are many edge cases to consider
- You'd benefit from exploring different approaches

Example perspectives by task type:
- New feature: simplicity vs performance vs maintainability
- Bug fix: root cause vs workaround vs prevention
- Refactoring: minimal change vs clean architecture

In the agent prompt:
- Provide comprehensive background context from Phase 1 exploration including filenames and code path traces
- Describe requirements and constraints
- Request a detailed implementation plan

### Phase 3: Review
Goal: Review the plan(s) from Phase 2 and ensure alignment with the user's intentions.
1. Read the critical files identified by agents to deepen your understanding
2. Ensure that the plans align with the user's original request
3. Use question tool to clarify any remaining questions with the user

### Phase 4: Final Plan
Goal: Write your final plan to the plan file (the only file you can edit).
- Include only your recommended approach, not all alternatives
- Ensure that the plan file is concise enough to scan quickly, but detailed enough to execute effectively
- Include the paths of critical files to be modified
- Include a verification section describing how to test the changes end-to-end (run the code, use MCP tools, run tests)

### Phase 5: Call plan_exit tool
At the very end of your turn, once you have asked the user questions and are happy with your final plan file - you should always call plan_exit to indicate to the user that you are done planning.
This is critical - your turn should only end with either asking the user a question or calling plan_exit. Do not stop unless it's for these 2 reasons.

**Important:** Use question tool to clarify requirements/approach, use plan_exit to request plan approval. Do NOT use question tool to ask "Is this plan okay?" - that's what plan_exit does.

NOTE: At any point in time through this workflow you should feel free to ask the user questions or clarifications. Don't make large assumptions about user intent. The goal is to present a well researched plan to the user, and tie any loose ends before implementation begins.
</system-reminder>`,
        synthetic: true,
      })
      userMessage.parts.push(part)
      return input.messages
    })

    const resolveTools = Effect.fn("SessionPrompt.resolveTools")(function* (input: {
      agent: Agent.Info
      model: Provider.Model
      session: Session.Info
      tools?: Record<string, boolean>
      processor: Pick<SessionProcessor.Handle, "message" | "updateToolCall" | "completeToolCall" | "recordGcfCandidate" | "observedCaptureMembership" | "stepRecovery" | "beginArgumentResolution" | "checkArgumentResolution">
      bypassAgentCheck: boolean
      messages: MessageV2.WithParts[]
      agentID?: string
      task_id?: string
      mcpContext?: MCP.TurnContext
      recallAttestations?: Map<string, { messageID: string; toolCallID: string; attestationID: string; resultSHA256: string; text: string }>
      invalidRecallCalls?: Set<string>
      gcfViewIdentity?: string
      permissionRulesets?: Permission.Ruleset[]
      actorExecution?: ActorExecutionIdentity
      alternativeRebuild?: ManagedProvider.OwnedAlternativeRebuild
    }) {
      using _ = log.time("resolveTools")
      const actorExecution = input.actorExecution ?? Option.getOrUndefined(yield* Effect.serviceOption(ActorExecutionContext))
      const tools: Record<string, AITool> = {}
      const activeTools = new Set<string>()
      const loadedMcpTools = new Set<string>()
      const mcpSearchEntries: McpToolSearchEntry[] = []
      const mcpCatalog = { current: createMcpToolSearchCatalog([]) }
      // exec's request-scoped MCP view. Holder object (same pattern as
      // mcpCatalog above): referenced by the context() closure below, filled
      // at the end of this pass once activeTools is settled. Travels through
      // ctx.extra — NOT a module-level ref, which concurrent sessions in the
      // same process would overwrite (request state must never live in a
      // global; see toolWhitelist/mcpToolSearch precedent).
      const execMcp: { current: Record<string, AITool> } = { current: {} }
      const useMcpToolSearch = isMcpToolSearchEnabled(
        Flag.MIMOCODE_EXPERIMENTAL_MCP_TOOL_SEARCH,
        input.model.id,
        input.model.api.id,
        input.model.family,
      )
      const run = yield* runner()
      const promptOps = yield* ops()
      if (input.alternativeRebuild && !ManagedProvider.currentOwnedAlternativeRebuild(input.alternativeRebuild))
        throw new Error('Alternative rebuild authority unavailable')
      const managedHostContext = input.alternativeRebuild ? input.alternativeRebuild.hostContext : actorExecution
        ? ManagedProvider.currentActorHostContext(input.session.id, actorExecution.actorID, actorExecution.executionRevision)
        : ManagedProvider.enabled() ? undefined : ManagedProvider.currentHostContext(input.session.id)

      // Capture optional annotation sources only under the original live main-turn admission.
      const schemaContext = !input.alternativeRebuild && actorExecution?.actorID==='main' &&
        managedHostContext?.toolViewAdmission?.includes('"schemaAnnotations":true')
        ? currentToolViewContext(actorExecution,true) : undefined
      const schemaSources = schemaContext ? new Map<string,{item:Tool.Def;parameters:Tool.Def['parameters'];execute:Tool.Def['execute'];description:string;pre:string;post:string}>() : undefined

      // Per-tool runtime whitelist: when the LLM call is being made on behalf
      // of a registered actor (subagent or peer), look up the actor row and,
      // if `actor.tools` is an array, reject calls to tools not in the
      // whitelist. `INHERIT` and a missing actor row both mean full access.
      const whitelistFor = Effect.fn("SessionPrompt.whitelistFor")(function* () {
        if (!input.agentID) return undefined
        const actor = yield* actorRegistry.get(input.session.id, input.agentID)
        if (!actor || !Array.isArray(actor.tools)) return undefined
        return new Set(actor.tools)
      })
      let whitelist = yield* whitelistFor()
      // Whether a permission ask must be non-interactive (fail clean, never hang):
      // true for system-spawned actors (checkpoint-writer/dream/distill) AND any
      // background actor such as compose workflow subagents (spawned as "general"
      // + background:true). Scoped to THIS permission decision on purpose — not
      // folded into the shared isSystemSpawned, which also gates memory
      // instructions and checkpoint self-triggering for user background actors.
      // Fall back to the agent-name check if the actor row is missing (race /
      // unregistered) so a system actor can't slip through as interactive.
      // Tool execution needs the registry identity used by managed ownership.
      // `actor:agent:...` below is history provenance for unregistered paths,
      // not an ActorRegistry key. Main is registered under the literal `main`.
      const inputActor = input.agentID ? yield* actorRegistry.get(input.session.id, input.agentID) : undefined
      const mainActor = inputActor ? undefined : yield* actorRegistry.get(input.session.id, "main")
      const registeredActorID = inputActor ? input.agentID : mainActor ? "main" : undefined
      const needsForkSnapshot = inputActor?.contextMode === "full" && (inputActor.mode === "subagent" || inputActor.mode === "peer")
      const forkProjection = needsForkSnapshot
        ? yield* (spawnRef.current?.getForkContext(input.session.id, inputActor!.actorID) ?? Effect.succeed(undefined))
        : undefined
      if (needsForkSnapshot && !forkProjection) return yield* Effect.fail(new Error("current fork context authority is unavailable"))
      if (needsForkSnapshot && forkProjection?.effectiveToolWhitelist !== undefined)
        whitelist = new Set(forkProjection.effectiveToolWhitelist)
      const inheritedRulesets = [ ...(forkProjection?.permissionRulesets ?? []), ...(input.permissionRulesets ?? []) ]
      const managedTurnRuleset: Permission.Ruleset | undefined = ManagedProvider.enabled() && input.tools
        ? Object.entries(input.tools).map(([permission, enabled]) => ({ permission, pattern: "*", action: enabled ? "allow" : "deny" }))
        : undefined
      const permissionRulesets = ManagedProvider.enabled()
        ? [Agent.runtimePermission(input.agent, input.session.permission), ...inheritedRulesets,
            ...(managedTurnRuleset ? [managedTurnRuleset] : [])]
        : inheritedRulesets.length ? inheritedRulesets : undefined
      // Keep permission and host-context routing exactly as before; the main
      // registry lookup above only supplies the question ownership identity.
      const askActor = input.agentID ? inputActor : undefined
      // Three-way permission-ask routing (see decideAskRouting): system agent ->
      // auto-deny; orchestrator peer -> FORWARD for approval; ordinary background
      // subagent -> INHERIT the parent's held grants; normal -> interactive.
      const askRouting = decideAskRouting({
        askActor: askActor
          ? {
              agent: askActor.agent,
              background: askActor.background,
              mode: askActor.mode,
              parentActorID: askActor.parentActorID,
            }
          : undefined,
        sessionParentID: input.session.parentID,
        agentName: input.agent.name,
        orchestratorEnabled: Flag.MIMOCODE_EXPERIMENTAL_ORCHESTRATOR,
      })
      const askInteractive = askRouting.interactive
      const askForward = askRouting.forward
      const askInherit = askRouting.inherit
      const rejectionFor = (toolID: string) => ({
        title: "Tool not permitted",
        output: `The "${toolID}" tool is not in this actor's whitelist. Allowed tools: ${
          whitelist ? [...whitelist].join(", ") : "(none)"
        }.`,
        metadata: { rejected: true, reason: "tool-whitelist" as const },
      })

      const context = (args: any, options: ToolExecutionOptions, callingTool: string): Tool.Context => ({
        sessionID: input.session.id,
        abort: options.abortSignal!,
        messageID: input.processor.message.id,
        callID: options.toolCallId,
        extra: {
          model: input.model,
          callingTool,
          bypassAgentCheck: input.bypassAgentCheck,
          promptOps,
          ...(actorExecution ? { actorExecution } : {}),
          ...(whitelist ? { toolWhitelist: [...whitelist] } : {}),
          ...(registeredActorID ? { registeredActorID } : {}),
          ...(hostContextForPrompt(askActor?.hostContext, managedHostContext)
            ? { hostContext: hostContextForPrompt(askActor?.hostContext, managedHostContext) }
            : {}),
          mcpToolSearch: mcpCatalog.current,
          execMcp,
          ...(() => {
            const raw = process.env.OPENCLANK_MIMO_HISTORY_CONTEXT
            if (!raw) return {}
            try {
              const value = JSON.parse(raw)
              if (!value || typeof value !== "object") return {}

              // The worker environment supplies the account credential and its
              // installation-authorized root.  A session's directory/workspace
              // are server-owned records, so narrow that binding here rather
              // than allowing a tool argument (or model text) to select it.
              const sessionRoot = path.isAbsolute(input.session.directory)
                ? path.resolve(input.session.directory)
                : undefined
              const bounded = (value: unknown): value is string => typeof value === "string" && value.length > 0 && new TextEncoder().encode(value).length <= 256 && !/[\u0000-\u001f\u007f-\u009f\ud800-\udfff]/.test(value)
              const origin = ManagedProvider.enabled() && actorExecution?.sessionID === input.session.id &&
                bounded(input.session.id) &&
                bounded(managedHostContext?.chatID) && bounded(managedHostContext?.accountID) && bounded(managedHostContext?.workspaceID) &&
                value.accountId === managedHostContext.accountID && (input.session.workspaceID ?? value.workspaceId) === managedHostContext.workspaceID &&
                bounded(actorExecution.rootOperationID) && bounded(actorExecution.actorID) && bounded(actorExecution.innerTurnID) &&
                Number.isSafeInteger(actorExecution.executionRevision) && actorExecution.executionRevision >= 0 &&
                bounded(options.toolCallId) && bounded(input.processor.message.id)
                ? { version: 1, host_chat_id: managedHostContext.chatID, engine_session_id: input.session.id,
                    root_operation_id: actorExecution.rootOperationID, actor_id: actorExecution.actorID,
                    execution_revision: actorExecution.executionRevision, inner_turn_id: actorExecution.innerTurnID,
                    tool_call_id: options.toolCallId, assistant_message_id: input.processor.message.id }
                : null
              const scoped = {
                ...(value as Record<string, unknown>),
                ...(sessionRoot ? { workspaceRoot: sessionRoot } : {}),
                ...(input.session.workspaceID ? { workspaceId: input.session.workspaceID } : {}),
                managedToolOrigin: origin,
              }
              return { historyCapture: scoped }
            } catch {
              return {}
            }
          })(),
        },
        agent: input.agent.name,
        // Main/primary turns do not always carry input.agentID.  Use the
        // trusted session and agent registry values to keep capture provenance
        // concrete; never derive identity from tool arguments.
        actorID: input.agentID || `agent:${input.agent.name}:${input.session.id}`,
        taskId: input.task_id,
        messages: input.messages,
        metadata: (val) =>
          input.processor.updateToolCall(options.toolCallId, (match) => {
            if (!["running", "pending"].includes(match.state.status)) return match
            return {
              ...match,
              state: {
                title: val.title,
                metadata: val.metadata,
                status: "running",
                input: argumentCalls.has(options.toolCallId) ? match.state.input : args,
                time: { start: argumentCalls.has(options.toolCallId) && match.state.status==='running' ? match.state.time.start : Date.now() },
              },
            }
          }),
        ask: (req) =>
          permission
            .ask(
              {
                ...req,
                sessionID: input.session.id,
                tool: { messageID: input.processor.message.id, callID: options.toolCallId },
                ruleset: Agent.runtimePermission(input.agent, input.session.permission),
                ...(permissionRulesets?.length ? { rulesets: permissionRulesets } : {}),
                // System-spawned + non-peer background agents have no human to answer
                // → fail clean, don't hang. Orchestrator peers FORWARD for approval;
                // ordinary background subagents INHERIT the parent's held grants.
                interactive: askInteractive,
                ...(askForward ? { forward: askForward } : {}),
                ...(askInherit ? { inherit: askInherit } : {}),
              },
              options.abortSignal,
            )
            .pipe(Effect.orDie),
      })

      const argumentCalls = new Set<string>()
      const argumentTargets = new Map<string,{item:Tool.Def;witness:NonNullable<ReturnType<NonNullable<typeof registry.argumentDefinitionWitness>>>;execute:NonNullable<AITool['execute']>}>()
      const recoveryTools = new Map<string, ToolWitness>()
      const registryDefs = yield* registry.tools({
        modelID: input.model.id,
        providerID: input.model.providerID,
        agent: input.agent,
        ...(managedHostContext?.gcfArgumentAdmission && managedHostContext.gcfArtifact?.decodeAvailability()==='available' ? {argumentWitness:true} : {}),
      })
      for (const item of registryDefs) {
        const witness = registry.recoveryWitness?.(item)
        if (witness) recoveryTools.set(item.id, witness)
        const definitionWitness = registry.memoryDefinitionWitness?.(item)
        const originalSchema = z.toJSONSchema(item.parameters)
        let originalSchemaJSON:string | undefined
        if (schemaSources) try {const value=JSON.stringify(originalSchema);if (Buffer.byteLength(value,'utf8')<=65536) originalSchemaJSON=value} catch { /* optional source unavailable */ }
        const schema = ProviderTransform.schema(input.model, originalSchema)
        if (schemaSources && originalSchemaJSON) try {
          const post=JSON.stringify(schema)
          if (Buffer.byteLength(post,'utf8')<=65536) schemaSources.set(item.id,{item,parameters:item.parameters,execute:item.execute,description:item.description,pre:originalSchemaJSON,post})
        } catch { /* optional source unavailable */ }
        tools[item.id] = tool({
          description: item.description,
          inputSchema: jsonSchema(schema),
          execute(args, options) {
            return run.promise(
              Effect.gen(function* () {
                const startTs = Date.now()
                const callID = options?.toolCallId ?? "?"
                log.debug("tool execute start", {
                  tool: item.id,
                  callID,
                  sessionID: input.session.id,
                })
                const ctx = context(args, options, item.id)
                const tracker=input.processor.stepRecovery?.()
                const token=tracker?.invocation(options.toolCallId)
                // Capture attribution is independent of full-step purity. Unassociated callbacks
                // remain explicit partial History rather than being guessed into a step.
                const observed = !argumentCalls.has(options.toolCallId) && token && tracker?.isCurrent(token)
                  ? input.processor.observedCaptureMembership?.(options.toolCallId,token.stepID) : undefined
                if (observed && ctx.extra) ctx.extra.observedCaptureMembership = observed
                let recovery: {binding:StepRecoveryBinding;current:()=>Promise<boolean>;capture?:{history:any;actionID:string;actionRef?:string}} | undefined
                const managed = yield* Effect.tryPromise({
                  try: () => ManagedProvider.admitManagedSessionBinding(ctx.sessionID),
                  catch: () => new RecoverableError("managed session binding is unavailable or reconciling"),
                }).pipe(Effect.orDie)
                if (ManagedProvider.enabled() && (!managed || managed.transition)) {
                  throw new Error("managed session binding is unavailable or reconciling")
                }
                if (whitelist && !whitelist.has(item.id) && item.id !== MCP_TOOL_SEARCH_ID) {
                  const output = rejectionFor(item.id)
                  log.debug("tool execute rejected", {
                    tool: item.id,
                    callID,
                    durationMs: Date.now() - startTs,
                  })
                  yield* input.processor.completeToolCall(options.toolCallId, output)
                  return output
                }
                const historyConfig=ctx.extra?.historyCapture
                if (tracker && token && tracker.isCurrent(token) && observed && witness && actorExecution?.rootOperationID && actorExecution.innerTurnID &&
                    managedHostContext?.accountID && managedHostContext.chatID && managedHostContext.workspaceID &&
                    registry.recoveryDirectFile?.(item) && registry.recoveryWitness?.(item)===witness && item.execute===witness.effective &&
                    historyConfig && typeof historyConfig === "object" &&
                    (historyConfig as any).accountId === managedHostContext.accountID &&
                    (historyConfig as any).workspaceId === managedHostContext.workspaceID &&
                    typeof (historyConfig as any).workspaceRoot === "string" && path.isAbsolute((historyConfig as any).workspaceRoot) &&
                    typeof (historyConfig as any).socketPath === "string" && path.isAbsolute((historyConfig as any).socketPath) &&
                    typeof (historyConfig as any).token === "string" && !!(historyConfig as any).token &&
                    stepRecoveryProfileSupported() && registry.recoveryEnvironment && registry.provenanceGeneration && plugin.recoveryWitness && bus.recoveryWitness) {
                  const identity={sessionID:actorExecution.sessionID,actorID:actorExecution.actorID,executionRevision:actorExecution.executionRevision,
                    rootOperationID:actorExecution.rootOperationID,innerTurnID:actorExecution.innerTurnID}
                  const binding:StepRecoveryBinding={...identity,messageID:input.processor.message.id,stepID:token.stepID,
                    accountID:managedHostContext.accountID,hostChatID:managedHostContext.chatID,workspaceID:managedHostContext.workspaceID}
                  yield* Effect.gen(function* () {
                  const global=globalEventRecoveryWitness()
                  if (!global.eligible) return
                  const promptBus=yield* bus.recoveryWitness!()
                  if (!promptBus.eligible) return
                  const environment=yield* registry.recoveryEnvironment!()
                  if (!environment?.formatter.explicitlyDisabled || !environment.lsp.eligible || !environment.bus.eligible) return
                  const hooks=yield* plugin.recoveryWitness!()
                  if (!hooks.noUnclassifiedHandlers) return
                  const generation=yield* registry.provenanceGeneration!()
                  const current=async()=>{
                    try {
                      if (ctx.abort?.aborted || !isAdmittedToolTimingTurn(actorExecution,identity) || !global.current() ||
                          !registry.recoveryDirectFile?.(item) || registry.recoveryWitness?.(item)!==witness || item.execute!==witness.effective) return false
                      const host=ManagedProvider.currentActorHostContext(identity.sessionID,identity.actorID,identity.executionRevision)
                      if (!host || host.accountID!==binding.accountID || host.chatID!==binding.hostChatID || host.workspaceID!==binding.workspaceID ||
                          host.cwd!==input.session.directory) return false
                      return await run.promise(Effect.gen(function* () {
                        return (yield* registry.provenanceGeneration!())===generation && (yield* hooks.current()) &&
                          (yield* environment!.formatter.current()) && (yield* environment!.lsp.current()) &&
                          (yield* environment!.bus.current()) && (yield* promptBus.current())
                      }))
                    } catch { return false }
                  }
                  if (environment?.formatter.explicitlyDisabled && environment.lsp.eligible && environment.bus.eligible &&
                      promptBus.eligible && global.eligible && hooks.noUnclassifiedHandlers &&
                      (yield* Effect.promise(current))) {
                    recovery={binding,current}
                    const owned=recovery
                    requestProjectPolicyRecoveryEvidence(ctx)
                    ctx.extra!.observedCaptureMembership = {
                      begin:async (history:any,actionID:string)=>{
                        await observed.begin(history,actionID)
                        if (owned.capture || !await current()) { tracker.invalidate(); return }
                        owned.capture={history,actionID}
                      },
                      complete:async (history:any,actionID:string,actionRef:string)=>{
                        await observed.complete(history,actionID,actionRef)
                        if (!owned.capture || owned.capture.history!==history || owned.capture.actionID!==actionID || owned.capture.actionRef) {
                          tracker.invalidate(); return
                        }
                        owned.capture.actionRef=actionRef
                      },
                    }
                  }
                  }).pipe(Effect.catchCause(cause => Cause.hasInterruptsOnly(cause) ? Effect.failCause(cause) : Effect.void))
                }
                const beforeOutput: { args: any; cancel?: boolean; cancelReason?: string } = { args }
                yield* plugin.trigger(
                  "tool.execute.before",
                  { tool: item.id, sessionID: ctx.sessionID, callID: ctx.callID },
                  beforeOutput,
                )
                if (beforeOutput.cancel) {
                  const cancelOutput = {
                    title: "Cancelled",
                    output: beforeOutput.cancelReason || "Tool call cancelled by hook",
                    metadata: { cancelled: true },
                  }
                  yield* bus
                    .publish(Metrics.ToolCall, {
                      sessionID: ctx.sessionID,
                      tool_name: argumentCalls.has(options.toolCallId) ? GCF_ARGUMENT_TOOL : item.id,
                      input_bytes: Metrics.jsonByteLength(beforeOutput.args),
                      output_bytes: 0,
                      tool_call_id: options.toolCallId,
                      tool_call_status: "cancelled",
                    })
                    .pipe(Effect.ignore)
                  yield* input.processor.completeToolCall(options.toolCallId, cancelOutput)
                  return cancelOutput
                }
                if (actorExecution && (item.id === 'bash' || item.id === 'webfetch' || item.id === 'mcp_tool_search' || item.id === 'grep' || item.id === 'read')) {
                  const host=ManagedProvider.currentActorHostContext(actorExecution.sessionID,actorExecution.actorID,actorExecution.executionRevision)
                  const encoded=host?.toolViewAdmission ?? host?.jsonWhitespaceAdmission
                  const profiles=enabledToolViewProfiles(encoded,item.id).filter(profile=>profile!=='json-whitespace-v1' || item.id==='bash') as ProducerViewProfile[]
                  if (profiles.length) {
                    const admitted=currentToolViewContext(actorExecution,true)
                    if (admitted && admitted.encoded===encoded && toolViewContextCurrent(admitted))
                      bindToolViewProducer(ctx,profiles,()=>!ctx.abort.aborted && toolViewContextCurrent(admitted))
                  }
                }
                const result = yield* item.execute(beforeOutput.args, ctx)
                log.debug("tool execute done", {
                  tool: item.id,
                  callID,
                  durationMs: Date.now() - startTs,
                  ok: true,
                })
                const output = {
                  ...result,
                  attachments: result.attachments?.map((attachment) => ({
                    ...attachment,
                    id: PartID.ascending(),
                    sessionID: ctx.sessionID,
                    messageID: input.processor.message.id,
                  })),
                }
                yield* plugin.trigger(
                  "tool.execute.after",
                  { tool: item.id, sessionID: ctx.sessionID, callID: ctx.callID, args: beforeOutput.args },
                  output,
                )
                // No native or extension tool can supply reserved activity provenance.
                if (output.metadata && typeof output.metadata === "object") {
                  output.metadata = { ...output.metadata }
                  delete output.metadata.codeActivity
                  delete output.metadata.openclankArgumentResolution
                }
                if (!options.abortSignal?.aborted) {
                  if (item.id === 'bash') {transferBashView(result,output);transferTerminalAnsi(result,output)}
                  if (item.id === 'webfetch') transferWebfetchHtml(result,output)
                  if (item.id === 'mcp_tool_search') transferStructuredSearch(result,output)
                  if (item.id === 'grep') transferGrepRows(result,output)
                  if (item.id === 'read') transferReadView(result,output)
                }
                if (
                  (item.id === "write" || item.id === "edit") &&
                  beforeOutput.args?.file_path &&
                  isExtensionPath(beforeOutput.args.file_path)
                ) {
                  yield* registry.reload().pipe(
                    Effect.tapError((err) => Effect.sync(() => log.warn("extension reload failed", { error: err }))),
                    Effect.ignore,
                  )
                }
                yield* bus
                  .publish(Metrics.ToolCall, {
                    sessionID: ctx.sessionID,
                    tool_name: argumentCalls.has(options.toolCallId) ? GCF_ARGUMENT_TOOL : item.id,
                    input_bytes: Metrics.jsonByteLength(beforeOutput.args),
                    output_bytes: Buffer.byteLength(output.output ?? "", "utf8"),
                    tool_call_id: options.toolCallId,
                    tool_call_status: "success",
                  })
                  .pipe(Effect.ignore)
                if (options.abortSignal?.aborted) {
                  yield* input.processor.completeToolCall(options.toolCallId, output)
                }
                if (recovery?.capture?.actionRef && tracker && token && !options.abortSignal?.aborted) {
                  const saved=recovery
                  const capture=saved.capture
                  yield* Effect.promise(async()=>{
                    try {
                      const policy=projectPolicyRecoveryEvidence(ctx)
                      if (!policy || !capture?.actionRef) return
                      const proof=await qualifyOriginalCapture({binding:saved.binding,history:capture.history,actionID:capture.actionID,
                        toolCallID:options.toolCallId,actionRef:capture.actionRef,policy,current:saved.current})
                      if (proof) tracker.offer(token,proof)
                    } catch { /* Optional evidence cannot change tool behavior. */ }
                  })
                }
                return output
              }),
            )
          },
        })
        if ((item.id==='glob' || item.id==='grep') && managedHostContext?.gcfArgumentAdmission) {
          const witness=registry.argumentDefinitionWitness?.(item), execute=tools[item.id]?.execute
          if (witness && execute) argumentTargets.set(item.id,{item,witness,execute})
        }
        if (definitionWitness) bindConstructedTool(tools[item.id], schema, definitionWitness, () => registry.memoryDefinitionWitness?.(item) === definitionWitness)
        if (item.id !== MCP_TOOL_SEARCH_ID) activeTools.add(item.id)
      }

      const localToolNames = new Set(Object.keys(tools))
      // Managed MCP calls are admitted by the host immediately before dispatch.
      const mcpTools = ManagedProvider.enabled()
        ? actorExecution?.preflightRoute?.bootstrap.rootSessionID && actorExecution.rootOperationID && actorExecution.innerTurnID
          ? Object.entries(yield* mcp.tools(input.mcpContext ? { ...input.mcpContext, backgroundMessageID: input.processor.message.id } : undefined, {
              managedPrivateRootSessionID: actorExecution.preflightRoute.bootstrap.rootSessionID,
              admit: (toolName, kind, toolCallID, operation) => ManagedProvider.admitManagedTool({
                sessionID: input.session.id, actorID: actorExecution.actorID,
                executionRevision: actorExecution.executionRevision, rootOperationID: actorExecution.rootOperationID!,
                innerTurnID: actorExecution.innerTurnID!,
              }, toolName, kind, toolCallID, operation),
            }))
          : []
        : Object.entries(yield* mcp.tools(input.mcpContext))
      if (ManagedProvider.enabled() && actorExecution?.rootOperationID && actorExecution.innerTurnID && mcp.observeToolsCapabilities) {
        yield* mcp.observeToolsCapabilities({
          sessionID: input.session.id, actorID: actorExecution.actorID,
          executionRevision: actorExecution.executionRevision, rootOperationID: actorExecution.rootOperationID,
          innerTurnID: actorExecution.innerTurnID,
        }, projectExecCapabilities(registryDefs))
      }
      const agentToolAllowlist = input.agent.toolAllowlist ? new Set(input.agent.toolAllowlist) : undefined
      const currentRuleset = Agent.runtimePermission(input.agent, input.session.permission)
      const disabledMcpTools = permissionRulesets?.length
        ? Permission.disabledIntersection(mcpTools.map(([key]) => key), permissionRulesets)
        : Permission.disabled(mcpTools.map(([key]) => key), currentRuleset)
      for (const [key, item] of mcpTools) {
        const execute = item.execute
        if (!execute) continue

        if (localToolNames.has(key)) {
          log.warn("MCP tool conflicts with a local tool and was ignored", { tool: key })
          continue
        }

        const schema = yield* Effect.promise(() => Promise.resolve(asSchema(item.inputSchema).jsonSchema))
        const transformed = ProviderTransform.schema(input.model, schema)
        item.inputSchema = jsonSchema(transformed)
        const available =
          input.tools?.[key] !== false &&
          !disabledMcpTools.has(key) &&
          (!agentToolAllowlist || agentToolAllowlist.has(key))
        const searchable = available && (!whitelist || whitelist.has(key))
        if (searchable && useMcpToolSearch) {
          mcpSearchEntries.push({
            name: key,
            description: item.description ?? "",
            parameters: transformed as unknown as JSONObject,
          })
        }
        if (searchable && !useMcpToolSearch && input.model.capabilities.toolcall) activeTools.add(key)
        item.execute = (args, opts) =>
          run.promise(
            Effect.gen(function* () {
              const startTs = Date.now()
              const callID = opts?.toolCallId ?? "?"
              log.debug("tool execute start (mcp)", {
                tool: key,
                callID,
                sessionID: input.session.id,
              })
              const ctx = context(args, opts, key)
              const managed = yield* Effect.tryPromise({
                try: () => ManagedProvider.admitManagedSessionBinding(ctx.sessionID),
                catch: () => new RecoverableError("managed session binding is unavailable or reconciling"),
              }).pipe(Effect.orDie)
              if (ManagedProvider.enabled() && (!managed || managed.transition)) {
                throw new Error("managed session binding is unavailable or reconciling")
              }
              if (!useMcpToolSearch && (!available || !input.model.capabilities.toolcall)) {
                return yield* Effect.fail(
                  new RecoverableError(`The MCP tool "${key}" is unavailable for this request.`),
                )
              }
              if (useMcpToolSearch && !loadedMcpTools.has(key)) {
                return yield* Effect.fail(
                  new RecoverableError(
                    `The MCP tool "${key}" is not loaded for this request. Call ${MCP_TOOL_SEARCH_ID} first, then retry on the next step.`,
                  ),
                )
              }
              if (whitelist && !whitelist.has(key)) {
                const rejection = rejectionFor(key)
                const output = {
                  title: rejection.title,
                  metadata: rejection.metadata,
                  output: rejection.output,
                  attachments: [],
                  content: [{ type: "text" as const, text: rejection.output }],
                }
                log.debug("tool execute rejected (mcp)", {
                  tool: key,
                  callID,
                  durationMs: Date.now() - startTs,
                })
                yield* input.processor.completeToolCall(opts.toolCallId, output)
                return output
              }
              const mcpBeforeOutput: { args: any; cancel?: boolean; cancelReason?: string } = { args }
              yield* plugin.trigger(
                "tool.execute.before",
                { tool: key, sessionID: ctx.sessionID, callID: opts.toolCallId },
                mcpBeforeOutput,
              )
              if (mcpBeforeOutput.cancel) {
                const cancelResult = {
                  content: [
                    { type: "text" as const, text: mcpBeforeOutput.cancelReason || "Tool call cancelled by hook" },
                  ],
                }
                yield* bus
                  .publish(Metrics.ToolCall, {
                    sessionID: ctx.sessionID,
                    tool_name: key,
                    input_bytes: Metrics.jsonByteLength(mcpBeforeOutput.args),
                    output_bytes: 0,
                    tool_call_id: opts.toolCallId,
                    tool_call_status: "cancelled",
                  })
                  .pipe(Effect.ignore)
                return cancelResult
              }
              yield* ctx.ask({ permission: key, metadata: /^lifetools_.+_control_desktop$/.test(key) ? { args: mcpBeforeOutput.args } : {}, patterns: ["*"], always: ["*"] })
              if (ManagedProvider.enabled()) {
                if (!actorExecution?.innerTurnID || !actorExecution.rootOperationID)
                  return yield* Effect.fail(new RecoverableError("managed MCP turn identity is unavailable"))
                const currentActor = yield* actorRegistry.get(input.session.id, actorExecution.actorID)
                const currentSession = yield* sessions.get(input.session.id)
                const currentAgent = yield* agents.get(input.agent.name)
                const currentFork = needsForkSnapshot
                  ? yield* (spawnRef.current?.getForkContext(input.session.id, actorExecution.actorID) ?? Effect.succeed(undefined))
                  : undefined
                if (!currentActor || currentActor.status !== "running" || currentActor.executionRevision !== actorExecution.executionRevision ||
                    !currentAgent || JSON.stringify(Agent.runtimePermission(currentAgent, currentSession.permission)) !== JSON.stringify(currentRuleset) ||
                    JSON.stringify(currentActor.tools ?? null) !== JSON.stringify((inputActor ?? mainActor)?.tools ?? null) ||
                    (needsForkSnapshot && (!currentFork || JSON.stringify(currentFork.permissionRulesets) !== JSON.stringify(forkProjection?.permissionRulesets) ||
                      JSON.stringify(currentFork.effectiveToolWhitelist) !== JSON.stringify(forkProjection?.effectiveToolWhitelist))))
                  return yield* Effect.fail(new RecoverableError("managed MCP authority changed; retry under the current policy"))
              }
              const toonContext = currentToolViewContext(actorExecution,true)
              const toonCurrent = toonContext && toonViewEnabled(toonContext.encoded) && toolViewContextCurrent(toonContext)
                ? () => !opts.abortSignal?.aborted && toolViewContextCurrent(toonContext) : undefined
              const result: Awaited<ReturnType<NonNullable<typeof execute>>> = yield* Effect.promise(() =>
                execute(mcpBeforeOutput.args, opts),
              )
              const outlineAttestation=MCP.takeSourceOutlineAttestation(result)
              yield* plugin.trigger(
                "tool.execute.after",
                { tool: key, sessionID: ctx.sessionID, callID: opts.toolCallId, args },
                result,
              )

              const normalized = normalizeToolResult(result)
              log.debug("tool execute done (mcp)", {
                tool: key,
                callID,
                durationMs: Date.now() - startTs,
                ok: !normalized.isError,
              })

              const truncated = yield* truncate.output(
                normalized.output,
                { outcome: normalized.isError ? "error" : "success" },
                input.agent,
                {
                  owner: managed?.owner ?? process.env.OPEN_CLANK_OWNER ?? "",
                  workspace: SessionCwd.get(ctx.sessionID),
                  sessionID: input.session.id,
                  ...(ctx.callID ? { callID: ctx.callID } : {}),
                },
              )
              const metadata: Record<string, unknown> = {
                ...normalized.metadata,
                truncated: truncated.truncated,
                ...(truncated.truncated && { outputPath: truncated.outputPath }),
              }
              delete metadata.codeActivity
              const codeActivity = MCP.takeCodeActivityAttestation(result)
              if (codeActivity && codeActivity.toolCallID === opts.toolCallId && !normalized.isError && !truncated.truncated &&
                  normalized.attachments.length === 0 && normalized.output === codeActivity.text && truncated.content === codeActivity.text)
                metadata.codeActivity = codeActivity.event
              const attachments = normalized.attachments.map((attachment) => ({
                type: "file" as const,
                ...attachment,
                id: PartID.ascending(),
                sessionID: ctx.sessionID,
                messageID: input.processor.message.id,
              }))

              if (normalized.isError) {
                return yield* Effect.fail(
                  new ToolResultError(truncated.content.trim() || "MCP tool execution failed", metadata, attachments),
                )
              }

              yield* bus
                .publish(Metrics.ToolCall, {
                  sessionID: ctx.sessionID,
                  tool_name: key,
                  input_bytes: Metrics.jsonByteLength(args),
                  output_bytes: Metrics.jsonByteLength({
                    content: normalized.content,
                    structuredContent: normalized.structuredContent,
                  }),
                  tool_call_id: opts.toolCallId,
                  tool_call_status: "success",
                })
                .pipe(Effect.ignore)

              const output = {
                title: "",
                metadata,
                output: truncated.content,
                attachments,
              }
              recordMcpJsonCandidate({callID:opts.toolCallId,output:output.output,normalizedOutput:normalized.output,
                gcfOn:!!input.gcfViewIdentity,toonCurrent,outline:outlineAttestation,aborted:!!opts.abortSignal?.aborted,truncated:truncated.truncated,
                attachmentCount:attachments.length,isError:normalized.isError,content:result.content,
                hasStructuredContent:result.structuredContent!==undefined,record:input.processor.recordGcfCandidate})
              if (opts.abortSignal?.aborted) {
                yield* input.processor.completeToolCall(opts.toolCallId, output)
              }
              return output
            }),
          )
        tools[key] = item
      }
      mcpCatalog.current = createMcpToolSearchCatalog(mcpSearchEntries.toSorted((a, b) => a.name.localeCompare(b.name)))
      if (useMcpToolSearch && tools[MCP_TOOL_SEARCH_ID]) {
        const cfg = yield* config.get()
        const usableTokens = usable({ cfg, model: input.model })
        const lastFinished = input.messages.findLast(
          (message): message is MessageV2.WithParts & { info: MessageV2.Assistant } =>
            message.info.role === "assistant" && !!message.info.finish,
        )
        const lastFinishedIndex = lastFinished
          ? input.messages.findIndex((message) => message.info.id === lastFinished.info.id)
          : -1
        tools[MCP_TOOL_SEARCH_ID] = {
          ...tools[MCP_TOOL_SEARCH_ID],
          description: mcpToolSearchDescription(mcpCatalog.current.entries, {
            rich:
              contextPressureLevel({
                cfg,
                tokens: lastFinished?.info.tokens ?? {
                  input: 0,
                  output: 0,
                  reasoning: 0,
                  cache: { read: 0, write: 0 },
                },
                model: input.model,
                additionalTokens: Token.estimate(JSON.stringify(input.messages.slice(lastFinishedIndex + 1))),
              }) < 2,
            budget: mcpToolCatalogBudget({ usable: usableTokens, context: input.model.limit.context }),
          }),
        }
      }
      const searchable = new Set(mcpCatalog.current.entries.map((entry) => entry.name))
      const currentUser = input.messages.findLast((message) => message.info.role === "user")
      if (currentUser && useMcpToolSearch) {
        for (const message of input.messages) {
          if (message.info.role !== "assistant" || message.info.parentID !== currentUser.info.id) continue
          for (const part of message.parts) {
            if (part.type !== "tool" || part.tool !== MCP_TOOL_SEARCH_ID || part.state.status !== "completed") continue
            const metadata = part.state.metadata as Partial<McpToolSearchMetadata>
            if (metadata.catalogKey !== mcpCatalog.current.key || !Array.isArray(metadata.matchedTools)) continue
            for (const name of metadata.matchedTools) {
              if (typeof name !== "string" || !searchable.has(name)) continue
              loadedMcpTools.add(name)
              if (loadedMcpTools.size >= MCP_TOOL_SEARCH_MAX_LOADED) break
            }
            if (loadedMcpTools.size >= MCP_TOOL_SEARCH_MAX_LOADED) break
          }
          if (loadedMcpTools.size >= MCP_TOOL_SEARCH_MAX_LOADED) break
        }
      }

      if (
        useMcpToolSearch &&
        input.model.capabilities.toolcall &&
        mcpCatalog.current.entries.length > 0 &&
        tools[MCP_TOOL_SEARCH_ID]
      ) {
        activeTools.add(MCP_TOOL_SEARCH_ID)
      }
      loadedMcpTools.forEach((name) => activeTools.add(name))

      // Fill exec's request-scoped MCP view (holder declared at the top of
      // this pass, delivered via ctx.extra.execMcp): exactly the MCP tools
      // active for this request. Under mcp_tool_search gating that means only
      // search-loaded tools — exec must not bypass the discovery gate.
      for (const [key] of mcpTools) {
        if (!tools[key] || !activeTools.has(key)) continue
        if (key === MCP_TOOL_SEARCH_ID) continue
        execMcp.current[key] = tools[key]
      }

      const bounded = (value: unknown): value is string => typeof value === "string" && value.length > 0 && Buffer.byteLength(value, "utf8") <= 512
      const binding = ManagedProvider.enabled() && actorExecution?.sessionID === input.session.id
        && bounded(actorExecution.actorID) && Number.isSafeInteger(actorExecution.executionRevision)
        && actorExecution.executionRevision >= 0 && bounded(actorExecution.rootOperationID) && bounded(actorExecution.innerTurnID)
        && bounded(managedHostContext?.accountID) && bounded(managedHostContext?.chatID)
        && bounded(managedHostContext?.workspaceID) && path.isAbsolute(input.session.directory)
        ? Object.freeze({sessionID: input.session.id, actorID: actorExecution.actorID,
            executionRevision: actorExecution.executionRevision, rootOperationID: actorExecution.rootOperationID,
            innerTurnID: actorExecution.innerTurnID, accountID: managedHostContext.accountID,
            chatID: managedHostContext.chatID, workspaceID: managedHostContext.workspaceID, directory: input.session.directory})
        : undefined
      const recoveryDispatch = Object.entries(tools).map(([id, tool]) => Object.freeze({id, tool: recoveryTools.get(id), dispatch: tool.execute}))
      // No hook/formatter/registry snapshots were acquired at resolution. A
      // later read cannot establish what existed before it, so an explicitly
      // requested witness remains fallback instead of backdating observations.
      const recovery = () => Effect.sync(() => recoveryEvidence({
        tools: recoveryDispatch, hooks: undefined, formatter: undefined,
        registryGeneration: undefined, registryCurrent: undefined, binding,
        bindingCurrent: binding ? () => {
          const current = ManagedProvider.currentActorHostContext(binding.sessionID, binding.actorID, binding.executionRevision)
          return !!current && current.accountID === binding.accountID && current.chatID === binding.chatID
            && current.workspaceID === binding.workspaceID && input.session.directory === binding.directory
        } : undefined,
        toolsCurrent: () => Object.keys(tools).length === recoveryDispatch.length
          && recoveryDispatch.every((item) => tools[item.id]?.execute === item.dispatch),
      }))
      return {
        tools,
        argumentTargets, argumentCalls,
        activeTools: [...activeTools].filter((name) => tools[name]),
        historyWhitelisted: !whitelist || whitelist.has('history'),
        mcpPresent: mcpTools.some(([name]) => activeTools.has(name)) || (useMcpToolSearch && mcpCatalog.current.entries.length > 0),
        permissionRulesets,
        ...(schemaContext && schemaSources ? {schemaContext,schemaSources} : {}),
        recoveryEvidence: recovery,
      }
    })

    const handleSubtask = Effect.fn("SessionPrompt.handleSubtask")(function* (input: {
      task: MessageV2.SubtaskPart
      model: Provider.Model
      lastUser: MessageV2.User
      sessionID: SessionID
      session: Session.Info
      msgs: MessageV2.WithParts[]
    }) {
      const { task, model, lastUser, sessionID, session, msgs } = input
      const ctx = yield* InstanceState.context
      const promptOps = yield* ops()
      const { actor: actorTool } = yield* registry.named()
      const managed = yield* Effect.tryPromise({
        try: () => ManagedProvider.admitManagedSessionBinding(sessionID),
        catch: () => new RecoverableError("managed session binding is unavailable or reconciling"),
      }).pipe(Effect.orDie)
      if (ManagedProvider.enabled() && (!managed || managed.transition)) {
        throw new Error("managed session binding is unavailable or reconciling")
      }
      const taskModel = task.model ? yield* getModel(task.model.providerID, task.model.modelID, sessionID) : model
      const assistantMessage: MessageV2.Assistant = yield* sessions.updateMessage({
        id: MessageID.ascending(),
        role: "assistant",
        parentID: lastUser.id,
        sessionID,
        agentID: lastUser.agentID,
        mode: task.agent,
        agent: task.agent,
        variant: lastUser.model.variant,
        path: { cwd: ctx.directory, root: ctx.worktree },
        cost: 0,
        tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        modelID: taskModel.id,
        providerID: taskModel.providerID,
        time: { created: Date.now() },
      })
      const taskArgs = {
        operation: {
          action: "run" as const,
          prompt: task.prompt,
          description: task.description,
          subagent_type: task.agent,
          command: task.command,
        },
      }
      let part: MessageV2.ToolPart = yield* sessions.updatePart({
        id: PartID.ascending(),
        messageID: assistantMessage.id,
        sessionID: assistantMessage.sessionID,
        type: "tool",
        callID: ulid(),
        tool: ActorTool.id,
        state: {
          status: "running",
          input: taskArgs,
          time: { start: Date.now() },
        },
      })
      yield* plugin.trigger(
        "tool.execute.before",
        { tool: ActorTool.id, sessionID, callID: part.id },
        { args: taskArgs },
      )

      const taskAgent = yield* agents.get(task.agent)
      if (!taskAgent) {
        const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
        const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
        const error = new NamedError.Unknown({ message: `Agent not found: "${task.agent}".${hint}` })
        yield* bus.publish(Session.Event.Error, { sessionID, error: error.toObject() })
        throw error
      }

      let error: Error | undefined
      const taskAbort = new AbortController()
      const result = yield* actorTool
        .execute(taskArgs, {
          agent: task.agent,
          messageID: assistantMessage.id,
          sessionID,
          abort: taskAbort.signal,
          callID: part.callID,
          extra: { bypassAgentCheck: true, promptOps },
          messages: msgs,
          metadata: (val: { title?: string; metadata?: Record<string, any> }) =>
            Effect.gen(function* () {
              part = yield* sessions.updatePart({
                ...part,
                type: "tool",
                state: { ...part.state, ...val },
              } satisfies MessageV2.ToolPart)
            }),
          ask: (req: any) =>
            permission
              .ask({
                ...req,
                sessionID,
                ruleset: Agent.runtimePermission(taskAgent, session.permission),
              })
              .pipe(Effect.orDie),
        })
        .pipe(
          Effect.catchCause((cause) => {
            const defect = Cause.squash(cause)
            error = defect instanceof Error ? defect : new Error(String(defect))
            log.error("subtask execution failed", { error, agent: task.agent, description: task.description })
            return Effect.void
          }),
          Effect.onInterrupt(() =>
            Effect.gen(function* () {
              taskAbort.abort()
              assistantMessage.finish = "tool-calls"
              assistantMessage.time.completed = Date.now()
              yield* sessions.updateMessage(assistantMessage)
              if (part.state.status === "running") {
                yield* sessions.updatePart({
                  ...part,
                  state: {
                    status: "error",
                    error: "Cancelled",
                    time: { start: part.state.time.start, end: Date.now() },
                    metadata: part.state.metadata,
                    input: part.state.input,
                  },
                } satisfies MessageV2.ToolPart)
              }
            }),
          ),
        )

      const attachments = result?.attachments?.map((attachment) => ({
        ...attachment,
        id: PartID.ascending(),
        sessionID,
        messageID: assistantMessage.id,
      }))

      yield* plugin.trigger(
        "tool.execute.after",
        { tool: ActorTool.id, sessionID, callID: part.id, args: taskArgs },
        result,
      )

      assistantMessage.finish = "tool-calls"
      assistantMessage.time.completed = Date.now()
      yield* sessions.updateMessage(assistantMessage)

      if (result && part.state.status === "running") {
        yield* sessions.updatePart({
          ...part,
          state: {
            status: "completed",
            input: part.state.input,
            title: result.title,
            metadata: result.metadata,
            output: result.output,
            attachments,
            time: { ...part.state.time, end: Date.now() },
          },
        } satisfies MessageV2.ToolPart)
      }

      if (!result) {
        yield* sessions.updatePart({
          ...part,
          state: {
            status: "error",
            error: error ? `Tool execution failed: ${error.message}` : "Tool execution failed",
            time: {
              start: part.state.status === "running" ? part.state.time.start : Date.now(),
              end: Date.now(),
            },
            metadata: part.state.status === "pending" ? undefined : part.state.metadata,
            input: part.state.input,
          },
        } satisfies MessageV2.ToolPart)
      }

      if (!task.command) return

      const summaryUserMsg: MessageV2.User = {
        id: MessageID.ascending(),
        sessionID,
        role: "user",
        agentID: lastUser.agentID,
        time: { created: Date.now() },
        agent: lastUser.agent,
        model: lastUser.model,
      }
      yield* sessions.updateMessage(summaryUserMsg)
      yield* sessions.updatePart({
        id: PartID.ascending(),
        messageID: summaryUserMsg.id,
        sessionID,
        type: "text",
        text: "Summarize the actor tool output above and continue with your task.",
        synthetic: true,
      } satisfies MessageV2.TextPart)
    })

    const shellImpl = Effect.fn("SessionPrompt.shellImpl")(function* (input: ShellInput) {
      const ctx = yield* InstanceState.context
      const run = yield* runner()
      const session = yield* sessions.get(input.sessionID)
      if (session.revert) {
        yield* revert.cleanup(session)
      }
      const agent = yield* agents.get(input.agent)
      if (!agent) {
        const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
        const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
        const error = new NamedError.Unknown({ message: `Agent not found: "${input.agent}".${hint}` })
        yield* bus.publish(Session.Event.Error, { sessionID: input.sessionID, error: error.toObject() })
        throw error
      }
      const inputModel = input.modelRef
        ? yield* provider
            .resolveModelRef(input.modelRef)
            .pipe(Effect.map((m) => ({ providerID: m.providerID, modelID: m.id })))
        : input.model
      const agentModel = agent.modelRef
        ? yield* provider
            .resolveModelRef(agent.modelRef)
            .pipe(Effect.map((m) => ({ providerID: m.providerID, modelID: m.id })))
        : agent.model
      const model = inputModel ?? agentModel ?? (yield* lastModel(input.sessionID))
      const userMsg: MessageV2.User = {
        id: input.messageID ?? MessageID.ascending(),
        sessionID: input.sessionID,
        time: { created: Date.now() },
        role: "user",
        agent: input.agent,
        model: { providerID: model.providerID, modelID: model.modelID },
      }
      yield* sessions.updateMessage(userMsg)
      const userPart: MessageV2.Part = {
        type: "text",
        id: PartID.ascending(),
        messageID: userMsg.id,
        sessionID: input.sessionID,
        text: "The following tool was executed by the user",
        synthetic: true,
      }
      yield* sessions.updatePart(userPart)

      const msg: MessageV2.Assistant = {
        id: MessageID.ascending(),
        sessionID: input.sessionID,
        parentID: userMsg.id,
        agentID: userMsg.agentID,
        mode: input.agent,
        agent: input.agent,
        cost: 0,
        path: { cwd: ctx.directory, root: ctx.worktree },
        time: { created: Date.now() },
        role: "assistant",
        tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        modelID: model.modelID,
        providerID: model.providerID,
      }
      yield* sessions.updateMessage(msg)
      const part: MessageV2.ToolPart = {
        type: "tool",
        id: PartID.ascending(),
        messageID: msg.id,
        sessionID: input.sessionID,
        tool: "bash",
        callID: ulid(),
        state: {
          status: "running",
          time: { start: Date.now() },
          input: { command: input.command },
        },
      }
      yield* sessions.updatePart(part)

      const sh = Shell.preferred()
      const shellName = (
        process.platform === "win32" ? path.win32.basename(sh, ".exe") : path.basename(sh)
      ).toLowerCase()
      const invocations: Record<string, { args: string[] }> = {
        nu: { args: ["-c", input.command] },
        fish: { args: ["-c", input.command] },
        zsh: {
          args: [
            "-l",
            "-c",
            `
              __oc_cwd=$PWD
              [[ -f ~/.zshenv ]] && source ~/.zshenv >/dev/null 2>&1 || true
              [[ -f "\${ZDOTDIR:-$HOME}/.zshrc" ]] && source "\${ZDOTDIR:-$HOME}/.zshrc" >/dev/null 2>&1 || true
              cd "$__oc_cwd"
              eval ${JSON.stringify(input.command)}
            `,
          ],
        },
        bash: {
          args: [
            "-l",
            "-c",
            `
              __oc_cwd=$PWD
              shopt -s expand_aliases
              [[ -f ~/.bashrc ]] && source ~/.bashrc >/dev/null 2>&1 || true
              cd "$__oc_cwd"
              eval ${JSON.stringify(input.command)}
            `,
          ],
        },
        cmd: { args: ["/c", `${Shell.CMD_UTF8_PREFIX}${input.command}`] },
        powershell: {
          args: ["-NoProfile", "-Command", `${Shell.POWERSHELL_UTF8_PREFIX}${input.command}`],
        },
        pwsh: {
          args: ["-NoProfile", "-Command", `${Shell.POWERSHELL_UTF8_PREFIX}${input.command}`],
        },
        "": { args: ["-c", input.command] },
      }

      const args = (invocations[shellName] ?? invocations[""]).args
      const cwd = ctx.directory
      const shellEnv = yield* plugin.trigger(
        "shell.env",
        { cwd, sessionID: input.sessionID, callID: part.callID },
        { env: {} },
      )

      const cmd = ChildProcess.make(sh, args, {
        cwd,
        extendEnv: true,
        env: {
          ...shellEnv.env,
          ...(process.platform === "win32" ? { PYTHONIOENCODING: "utf-8" } : {}),
          TERM: "dumb",
        },
        stdin: "ignore",
        forceKillAfter: "3 seconds",
      })

      let output = ""
      let aborted = false

      const finish = Effect.uninterruptible(
        Effect.gen(function* () {
          if (aborted) {
            output += "\n\n" + ["<metadata>", "User aborted the command", "</metadata>"].join("\n")
          }
          if (!msg.time.completed) {
            msg.time.completed = Date.now()
            yield* sessions.updateMessage(msg)
          }
          if (part.state.status === "running") {
            part.state = {
              status: "completed",
              time: { ...part.state.time, end: Date.now() },
              input: part.state.input,
              title: "",
              metadata: { output, description: "" },
              output,
            }
            yield* sessions.updatePart(part)
          }
        }),
      )

      const exit = yield* Effect.gen(function* () {
        const handle = yield* spawner.spawn(cmd)
        yield* Stream.runForEach(Stream.decodeText(handle.all), (chunk) =>
          Effect.sync(() => {
            output += chunk
            if (part.state.status === "running") {
              part.state.metadata = { output, description: "" }
              void run.fork(sessions.updatePart(part))
            }
          }),
        )
        yield* handle.exitCode
      }).pipe(
        Effect.scoped,
        Effect.onInterrupt(() =>
          Effect.sync(() => {
            aborted = true
          }),
        ),
        Effect.orDie,
        Effect.ensuring(finish),
        Effect.exit,
      )

      if (Exit.isFailure(exit) && !Cause.hasInterruptsOnly(exit.cause)) {
        return yield* Effect.failCause(exit.cause)
      }

      return { info: msg, parts: [part] }
    })

    const getModel = Effect.fn("SessionPrompt.getModel")(function* (
      providerID: ProviderID,
      modelID: ModelID,
      sessionID: SessionID,
    ) {
      const exit = yield* provider.getModel(providerID, modelID).pipe(Effect.exit)
      if (Exit.isSuccess(exit)) return exit.value
      const err = Cause.squash(exit.cause)
      if (Provider.ModelNotFoundError.isInstance(err)) {
        const hint = err.data.suggestions?.length ? ` Did you mean: ${err.data.suggestions.join(", ")}?` : ""
        yield* bus.publish(Session.Event.Error, {
          sessionID,
          error: new NamedError.Unknown({
            message: `Model not found: ${err.data.providerID}/${err.data.modelID}.${hint}`,
          }).toObject(),
        })
      }
      return yield* Effect.failCause(exit.cause)
    })

    const lastModel = Effect.fnUntraced(function* (sessionID: SessionID) {
      const match = yield* sessions.findMessage(sessionID, (m) => m.info.role === "user" && !!m.info.model, {
        agentID: "*",
      })
      if (Option.isSome(match) && match.value.info.role === "user") return match.value.info.model
      return yield* provider.defaultModel()
    })

    const createUserMessage = Effect.fn("SessionPrompt.createUserMessage")(function* (input: PromptInput) {
      if (input.source === "spawn" && !input.agentID) {
        return yield* Effect.die(new Error("Spawn input requires an agentID"))
      }
      if (
        input.source === "hook" &&
        !input.provenance &&
        input.parts?.some((part) => part.type !== "text")
      ) {
        return yield* Effect.die(new Error("non-text parts requires provenance"))
      }
      const agentName = input.agent || (yield* agents.defaultAgent())
      const ag = yield* agents.get(agentName)
      if (!ag) {
        const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
        const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
        const error = new NamedError.Unknown({ message: `Agent not found: "${agentName}".${hint}` })
        yield* bus.publish(Session.Event.Error, { sessionID: input.sessionID, error: error.toObject() })
        throw error
      }

      const inputModel = input.modelRef
        ? yield* provider
            .resolveModelRef(input.modelRef)
            .pipe(Effect.map((m) => ({ providerID: m.providerID, modelID: m.id })))
        : input.model
      const agentModel = ag.modelRef
        ? yield* provider
            .resolveModelRef(ag.modelRef)
            .pipe(Effect.map((m) => ({ providerID: m.providerID, modelID: m.id })))
        : ag.model
      const model = inputModel ?? agentModel ?? (yield* lastModel(input.sessionID))
      const same = agentModel && model.providerID === agentModel.providerID && model.modelID === agentModel.modelID
      const full =
        input.variant === undefined && ag.variant && same
          ? yield* provider.getModel(model.providerID, model.modelID).pipe(Effect.catchDefect(() => Effect.void))
          : undefined
      // null is an internal explicit-clear sentinel from managed ACP. Keep it
      // out of the durable string-only User.model.variant field.
      const variant = input.variant === null
        ? undefined
        : input.variant ?? (ag.variant && full?.variants?.[ag.variant] ? ag.variant : undefined)

      const info: MessageV2.User = {
        id: input.messageID ?? MessageID.ascending(),
        role: "user",
        sessionID: input.sessionID,
        agentID: input.agentID,
        time: { created: Date.now() },
        tools: input.tools,
        agent: ag.name,
        model: {
          providerID: model.providerID,
          modelID: model.modelID,
          variant,
        },
        system: input.system,
        format: input.format,
        provenance: input.provenance,
      }

      yield* Effect.addFinalizer(() => instruction.clear(info.id))

      type Draft<T> = T extends MessageV2.Part ? Omit<T, "id"> & { id?: string } : never
      const assign = (part: Draft<MessageV2.Part>): MessageV2.Part => ({
        ...part,
        id: part.id ? PartID.make(part.id) : PartID.ascending(),
      })

      const resolvePart: (part: PromptInput["parts"][number]) => Effect.Effect<Draft<MessageV2.Part>[]> = Effect.fn(
        "SessionPrompt.resolveUserPart",
      )(function* (part) {
        if (part.type === "file") {
          if (part.source?.type === "resource") {
            const { clientName, uri } = part.source
            log.info("mcp resource", { clientName, uri, mime: part.mime })
            const pieces: Draft<MessageV2.Part>[] = [
              {
                messageID: info.id,
                sessionID: input.sessionID,
                type: "text",
                synthetic: true,
                text: `Reading MCP resource: ${part.filename} (${uri})`,
              },
            ]
            const exit = yield* mcp.readResource(clientName, uri).pipe(Effect.exit)
            if (Exit.isSuccess(exit)) {
              const content = exit.value
              if (!content) throw new Error(`Resource not found: ${clientName}/${uri}`)
              const items = Array.isArray(content.contents) ? content.contents : [content.contents]
              for (const c of items) {
                if ("text" in c && c.text) {
                  pieces.push({
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: c.text,
                  })
                } else if ("blob" in c && c.blob) {
                  const mime = "mimeType" in c ? c.mimeType : part.mime
                  pieces.push({
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `[Binary content: ${mime}]`,
                  })
                }
              }
              pieces.push({ ...part, messageID: info.id, sessionID: input.sessionID })
            } else {
              const error = Cause.squash(exit.cause)
              log.error("failed to read MCP resource", { error, clientName, uri })
              const message = error instanceof Error ? error.message : String(error)
              pieces.push({
                messageID: info.id,
                sessionID: input.sessionID,
                type: "text",
                synthetic: true,
                text: `Failed to read MCP resource ${part.filename}: ${message}`,
              })
            }
            return pieces
          }
          const url = new URL(part.url)
          switch (url.protocol) {
            case "data:":
              if (part.mime === "text/plain") {
                return [
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `Called the Read tool with the following input: ${JSON.stringify({ file_path: part.filename })}`,
                  },
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: decodeDataUrl(part.url),
                  },
                  { ...part, messageID: info.id, sessionID: input.sessionID },
                ]
              }
              break
            case "file:": {
              log.info("file", { mime: part.mime })
              const filepath = fileURLToPath(part.url)
              if (yield* fsys.isDir(filepath)) part.mime = "application/x-directory"

              const { read } = yield* registry.named()
              const execRead = (args: Parameters<typeof read.execute>[0], extra?: Tool.Context["extra"]) => {
                const controller = new AbortController()
                return read
                  .execute(args, {
                    sessionID: input.sessionID,
                    abort: controller.signal,
                    agent: input.agent!,
                    messageID: info.id,
                    extra: { bypassCwdCheck: true, ...extra },
                    messages: [],
                    metadata: () => Effect.void,
                    ask: () => Effect.void,
                  })
                  .pipe(Effect.onInterrupt(() => Effect.sync(() => controller.abort())))
              }

              if (part.mime === "text/plain") {
                let offset: number | undefined
                let limit: number | undefined
                const range = { start: url.searchParams.get("start"), end: url.searchParams.get("end") }
                if (range.start != null) {
                  const filePathURI = part.url.split("?")[0]
                  let start = parseInt(range.start)
                  let end = range.end ? parseInt(range.end) : undefined
                  if (start === end) {
                    const symbols = yield* lsp.documentSymbol(filePathURI).pipe(Effect.catch(() => Effect.succeed([])))
                    for (const symbol of symbols) {
                      let r: LSP.Range | undefined
                      if ("range" in symbol) r = symbol.range
                      else if ("location" in symbol) r = symbol.location.range
                      if (r?.start?.line && r?.start?.line === start) {
                        start = r.start.line
                        end = r?.end?.line ?? start
                        break
                      }
                    }
                  }
                  offset = Math.max(start, 1)
                  if (end) limit = end - (offset - 1)
                }
                const args = { file_path: filepath, offset, limit }
                const pieces: Draft<MessageV2.Part>[] = [
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `Called the Read tool with the following input: ${JSON.stringify(args)}`,
                  },
                ]
                const exit = yield* provider.getModel(info.model.providerID, info.model.modelID).pipe(
                  Effect.flatMap((mdl) => execRead(args, { model: mdl })),
                  Effect.exit,
                )
                if (Exit.isSuccess(exit)) {
                  const result = exit.value
                  pieces.push({
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: result.output,
                  })
                  if (result.attachments?.length) {
                    pieces.push(
                      ...result.attachments.map((a) => ({
                        ...a,
                        synthetic: true,
                        filename: a.filename ?? part.filename,
                        messageID: info.id,
                        sessionID: input.sessionID,
                      })),
                    )
                  } else {
                    pieces.push({ ...part, messageID: info.id, sessionID: input.sessionID })
                  }
                } else {
                  const error = Cause.squash(exit.cause)
                  log.error("failed to read file", { error })
                  const message = error instanceof Error ? error.message : String(error)
                  yield* bus.publish(Session.Event.Error, {
                    sessionID: input.sessionID,
                    error: new NamedError.Unknown({ message }).toObject(),
                  })
                  pieces.push({
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `Read tool failed to read ${filepath} with the following error: ${message}`,
                  })
                }
                return pieces
              }

              if (part.mime === "application/x-directory") {
                const args = { file_path: filepath }
                const exit = yield* execRead(args).pipe(Effect.exit)
                if (Exit.isFailure(exit)) {
                  const error = Cause.squash(exit.cause)
                  log.error("failed to read directory", { error })
                  const message = error instanceof Error ? error.message : String(error)
                  yield* bus.publish(Session.Event.Error, {
                    sessionID: input.sessionID,
                    error: new NamedError.Unknown({ message }).toObject(),
                  })
                  return [
                    {
                      messageID: info.id,
                      sessionID: input.sessionID,
                      type: "text",
                      synthetic: true,
                      text: `Read tool failed to read ${filepath} with the following error: ${message}`,
                    },
                  ]
                }
                return [
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `Called the Read tool with the following input: ${JSON.stringify(args)}`,
                  },
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: exit.value.output,
                  },
                  { ...part, messageID: info.id, sessionID: input.sessionID },
                ]
              }

              // Preserve unsupported local files as references. Hydrating an
              // opaque package/archive into a giant data URL both bloats the
              // persisted turn and prevents the host from deciding how to open
              // it later. Native media is handled by the provider wire path;
              // everything else remains a file URL.
              if (!/^(?:image|audio|video)\//.test(part.mime)) {
                return [
                  {
                    messageID: info.id,
                    sessionID: input.sessionID,
                    type: "text",
                    synthetic: true,
                    text: `Called the Read tool with the following input: {"file_path":"${filepath}"}`,
                  },
                  { ...part, messageID: info.id, sessionID: input.sessionID },
                ]
              }

              return [
                {
                  messageID: info.id,
                  sessionID: input.sessionID,
                  type: "text",
                  synthetic: true,
                  text: `Called the Read tool with the following input: {"file_path":"${filepath}"}`,
                },
                {
                  id: part.id,
                  messageID: info.id,
                  sessionID: input.sessionID,
                  type: "file",
                  url:
                    `data:${part.mime};base64,` +
                    Buffer.from(yield* fsys.readFile(filepath).pipe(Effect.catch(Effect.die))).toString("base64"),
                  mime: part.mime,
                  filename: part.filename!,
                  source: part.source,
                },
              ]
            }
          }
        }

        if (part.type === "agent") {
          const perm = Permission.evaluate("task", part.name, ag.permission)
          const hint = perm.action === "deny" ? " . Invoked by user; guaranteed to exist." : ""
          return [
            { ...part, messageID: info.id, sessionID: input.sessionID },
            {
              messageID: info.id,
              sessionID: input.sessionID,
              type: "text",
              synthetic: true,
              text:
                " Use the above message and context to generate a prompt and call the actor tool with subagent: " +
                part.name +
                hint,
            },
          ]
        }

        return [
          {
            ...part,
            ...(input.source === "hook" && part.type === "text" ? { synthetic: true } : {}),
            messageID: info.id,
            sessionID: input.sessionID,
          },
        ]
      })

      const parts = yield* Effect.forEach(input.parts, resolvePart, { concurrency: "unbounded" }).pipe(
        Effect.map((x) => x.flat().map(assign)),
      )

      // Guard: reject the message if no resolved part carries substantive content.
      // A message with only empty/ignored text or only droppable file types
      // (text/plain, application/x-directory) would become an empty-content user
      // message after the send-side filter (message-v2.ts), which Bedrock/Anthropic
      // reject with 400.  Catch it here so no empty row is ever persisted.
      if (!hasSubstantiveContent(parts as MessageV2.Part[])) {
        log.info("dropping empty-content user message (no substantive parts)", {
          sessionID: input.sessionID,
          messageID: info.id,
          partCount: parts.length,
          partTypes: parts.map((p) => p.type),
        })
        return { info, parts: [] }
      }

      const sourceBeforePlugin = ManagedProvider.enabled() ? userSourceSnapshot(info.id,parts) : undefined
      const layoutBefore = sourceBeforePlugin ? snapshotSourceParts(parts,sourceBeforePlugin.partID) : undefined
      const generatedBeforePlugin = ManagedProvider.enabled() ? generatedInputSnapshot(parts) : []
      yield* plugin.trigger(
        "chat.message",
        {
          sessionID: input.sessionID,
          agent: input.agent,
          model: input.model,
          messageID: input.messageID,
          variant: input.variant,
        },
        { message: info, parts },
      )

      const parsed = MessageV2.Info.safeParse(info)
      if (!parsed.success) {
        log.error("invalid user message before save", {
          sessionID: input.sessionID,
          messageID: info.id,
          agent: info.agent,
          model: info.model,
          issues: parsed.error.issues,
        })
      }
      parts.forEach((part, index) => {
        const p = MessageV2.Part.safeParse(part)
        if (p.success) return
        log.error("invalid user part before save", {
          sessionID: input.sessionID,
          messageID: info.id,
          partID: part.id,
          partType: part.type,
          index,
          issues: p.error.issues,
          part,
        })
      })

      yield* sessions.updateMessage(info)
      for (const part of parts) yield* sessions.updatePart(part)

      const result = { info, parts }
      const sourceAfterPlugin = sourceBeforePlugin && userSourceSnapshot(info.id,parts)
      const generatedAfterPlugin=generatedInputSnapshot(parts)
      const generated=generatedBeforePlugin.filter(before=>generatedAfterPlugin.some(after=>JSON.stringify(after)===JSON.stringify(before)))
      const source=sourceBeforePlugin && sourceAfterPlugin && JSON.stringify(sourceBeforePlugin) === JSON.stringify(sourceAfterPlugin) ? sourceBeforePlugin : undefined
      const layoutAfter=source ? snapshotSourceParts(parts,source.partID) : undefined
      const layout=layoutBefore && layoutAfter && JSON.stringify(layoutBefore)===JSON.stringify(layoutAfter) ? layoutBefore : undefined
      if (ManagedProvider.enabled() && (input.agentID ?? 'main')==='main') {
        try { log.debug('paired_source_candidate', {before:!!sourceBeforePlugin,after:!!sourceAfterPlugin,unchanged:!!source,layoutBefore:!!layoutBefore,layoutAfter:!!layoutAfter,layout:!!layout}) } catch { /* Diagnostic only. */ }
      }
      if (source || generated.length) userSourceCandidates.set(result,{messageID:info.id,partID:source?.partID,digest:source?.digest,...(layout?{layout}:{}),...(generated.length?{generated}:{})})
      return result
    }, Effect.scoped)

    const sweepOrphanAssistants = Effect.fn("SessionPrompt.sweepOrphanAssistants")(function* (
      sessionID: SessionID,
      // When true, sweep dangling assistants regardless of age. The caller sets
      // this when the session is idle (no active runner), meaning any assistant
      // without time.completed is definitively orphaned — left behind by a hard
      // interruption (process crash / kill / disconnect) that skipped the normal
      // `finish` effect, not an in-flight retry chain. Sweeping immediately
      // matters because the TUI derives its "pending" marker from the newest
      // incomplete assistant (routes/session/index.tsx `pending`): a stale
      // orphan otherwise makes EVERY newly submitted message on an idle session
      // render as stuck QUEUED for up to ORPHAN_AGE_MS (an hour). Defaults to
      // false so background callers (spawn/hook) keep the age guard.
      immediate = false,
    ) {
      const msgs = yield* sessions.messages({ sessionID, agentID: "*" })
      const now = Date.now()
      // 1 hour — must exceed Task 1's chunkMs (300s) plus Task 2's
      // PERSISTENT_RETRY worst-case backoff (10 attempts × 5 min cap =
      // 50 min) so a still-active in-flight request is never falsely
      // swept while its retry chain is making progress.
      const ORPHAN_AGE_MS = 3_600_000
      for (const m of msgs) {
        if (m.info.role !== "assistant") continue
        if (m.info.time?.completed) continue
        const created = m.info.time?.created ?? 0
        if (!immediate && now - created < ORPHAN_AGE_MS) continue
        m.info.time = { ...m.info.time, completed: now }
        m.info.error =
          m.info.error ??
          new MessageV2.AbortedError({
            message: "Abandoned: previous request interrupted before completion",
          }).toObject()
        yield* sessions.updateMessage(m.info).pipe(
          Effect.catchCause((cause) =>
            elog.warn("orphan-update-failed", {
              sessionID,
              messageID: m.info.id,
              cause,
            }),
          ),
        )
        yield* elog.info("orphan-assistant-cleared", {
          sessionID,
          messageID: m.info.id,
        })
      }
    })

    const prompt: (input: PromptInput) => Effect.Effect<MessageV2.WithParts> = Effect.fn("SessionPrompt.prompt")(
      function* (input: PromptInput) {
        yield* assertExecutionActive(input.sessionID, input.agentID ?? "main")
        const session = yield* sessions.get(input.sessionID)
        if (input.source !== "spawn" && input.source !== "hook") {
          yield* revert.cleanup(session)
          // An idle session has no active runner, so any dangling assistant is a
          // true orphan from a hard interruption — sweep it now (age-independent)
          // so a fresh message is not rendered as stuck QUEUED behind it.
          const idle = (yield* status.get(input.sessionID)).type === "idle"
          yield* sweepOrphanAssistants(input.sessionID, idle)
        }
        const message = yield* createUserMessage(input)
        yield* sessions.touch(input.sessionID)
        const titleMessages = session.titleRevision === 0
          ? yield* sessions.messages({ sessionID: input.sessionID, agentID: input.agentID ?? "main" })
          : []
        const genuineUsers = titleMessages.filter(
          (item) =>
            item.info.role === "user" &&
            !item.info.provenance?.hookPhase &&
            !item.parts.some((part) => "metadata" in part && part.metadata?.origin) &&
            !item.parts.every((part) => "synthetic" in part && part.synthetic),
        )
        const firstTitleMessage = genuineUsers.find(
          (item): item is MessageV2.WithParts & { info: MessageV2.User } => item.info.role === "user",
        )

        // Reserve the deterministic first-turn title before callers that do
        // not start a model loop (notably noReply and command inputs).
        if (
          input.source !== "spawn" &&
          input.source !== "hook" &&
          message.info.role === "user" &&
          Session.isDefaultTitle(session.title) &&
          firstTitleMessage
        ) {
          const firstText = firstTitleMessage.parts.find(
            (part): part is MessageV2.TextPart => part.type === "text" && !part.synthetic && !part.ignored,
          )?.text
          const attachment = firstTitleMessage.parts.find(
            (part): part is MessageV2.FilePart => part.type === "file" && !("synthetic" in part && part.synthetic),
          )?.filename
          const commandOnly = Boolean(input.command) || firstTitleMessage.parts.some(
            (part) => part.type === "text" && part.text.trim().startsWith("/"),
          )
          const fallback = GenTitle.titleInputText(commandOnly || !firstText ? attachment ?? firstText : firstText, [])
          const fallbackTitle = fallback.split("\n").map((line) => line.trim()).find(Boolean) || "Untitled"
          const reserved = yield* sessions.setTitleIfDefault({
            sessionID: session.id,
            title: Array.from(fallbackTitle).slice(0, GenTitle.TITLE_MAX_LENGTH).join(""),
            expectedRevision: session.titleRevision,
            source: "fallback",
          }).pipe(
            Effect.catchCause((cause) =>
              elog.warn("initial title reservation failed; preserving user turn", { error: Cause.squash(cause) }).pipe(
                Effect.as(false),
              ),
            ),
          )
          if (reserved && !(commandOnly && attachment))
            yield* title({
              session,
              agent: firstTitleMessage.info.agent,
              modelID: firstTitleMessage.info.model.modelID,
              providerID: firstTitleMessage.info.model.providerID,
              history: [firstTitleMessage],
              titleLocale: input.titleLocale,
              titleText: input.command && firstTitleMessage.info.id === message.info.id && input.arguments?.trim()
                ? input.arguments
                : undefined,
            }).pipe(Effect.forkDetach({ startImmediately: true }), Effect.asVoid)
        }

        // The caller's tool map is a complete per-turn policy REVISION
        // (identity Slice 05: replace, never accumulate). `undefined` means
        // "no statement — keep the session's permissions"; a provided map
        // REPLACES them, and an EMPTY map clears a previous turn's rules —
        // an answer-only turn's {"*": false} must not leak into the next
        // tool-bearing turn as a persisted deny-all.
        if (input.tools !== undefined && !ManagedProvider.enabled()) {
          const permissions: Permission.Ruleset = Object.entries(input.tools).map(([t, enabled]) => ({
            permission: t,
            action: enabled ? "allow" : "deny",
            pattern: "*",
          }))
          session.permission = permissions
          yield* sessions.setPermission({ sessionID: session.id, permission: permissions })
        }

        if (input.noReply === true) return message
        // Short-circuit: when the message was dropped for being empty-content
        // (hasSubstantiveContent returned false → parts: []), skip the model
        // turn entirely. Running loop() here would produce a spurious assistant
        // response with no user turn.
        if (message.parts.length === 0) return message
        return yield* loop({
          sessionID: input.sessionID,
          agentID: input.agentID ?? "main",
          task_id: input.task_id,
          model: message.info.model,
          actorAgent: message.info.agent,
          manageExecution: input.source !== "spawn" && input.source !== "hook",
        }, userSourceCandidates.get(message))
      },
    )

    const lastAssistant = Effect.fnUntraced(function* (sessionID: SessionID, agentID?: string) {
      if (agentID !== undefined) {
        // Agent-scoped: return THIS agent's newest message (assistant preferred).
        // Critical for concurrent same-session subagents — a session-wide lookup
        // collapses concurrent actors' return values onto whichever finished last.
        // messages() yields oldest-first/newest-last, so findLast picks the newest
        // assistant and the last element is the newest message overall.
        const own = yield* sessions.messages({ sessionID, agentID })
        const lastAsst = own.findLast((m) => m.info.role === "assistant")
        if (lastAsst) return lastAsst
        if (own.length > 0) return own[own.length - 1]
        // fall through to session-wide if this agent has no messages yet
      }
      const match = yield* sessions.findMessage(sessionID, (m) => m.info.role !== "user", { agentID: "*" })
      if (Option.isSome(match)) return match.value
      const msgs = yield* sessions.messages({ sessionID, limit: 1, agentID: "*" })
      if (msgs.length > 0) return msgs[0]
      throw new Error("Impossible")
    })

    const runLoop: (
      sessionID: SessionID,
      agentID?: string,
      task_id?: string,
      notifyParentOnComplete?: boolean,
      verifyGoalTarget?: Goal.GoalTarget,
      resumeFrom?: MessageID,
      modelOverride?: { providerID: string; modelID: string },
      userRedispatch?: boolean,
      afterTurnReady?: boolean,
      sourceCandidate?: UserSourceCandidate,
    ) => Effect.Effect<MessageV2.WithParts> = Effect.fn("SessionPrompt.run")(function* (
      sessionID: SessionID,
      agentID?: string,
      task_id?: string,
      notifyParentOnComplete?: boolean,
      verifyGoalTarget?: Goal.GoalTarget,
      resumeFrom?: MessageID,
      modelOverride?: { providerID: string; modelID: string },
      userRedispatch?: boolean,
      afterTurnReady?: boolean,
      sourceCandidate?: UserSourceCandidate,
    ) {
      const ctx = yield* InstanceState.context
      const slog = elog.with({ sessionID })
      let structured: unknown | undefined
      const recallAttestations = new Map<string, { messageID: string; toolCallID: string; attestationID: string; resultSHA256: string; text: string }>()
      const generatedSources = new Map<string,GeneratedContextSource>()
      const invalidRecallCalls = new Set<string>()
      const admittedRecallCalls = new Set<string>()
      const pendingRecallCalls = new Set<string>()
      let step = 0
      let sourceAttempt:SourceAttemptHandle | undefined
      let factoryPersonaStart:import("../acp/openclank-protocol").OpenClankManagedProtocol.ManagedTurnStartResult | undefined
      const session = yield* sessions.get(sessionID)
      let lastFinishedForPrune: MessageV2.Assistant | undefined
      let lastModelForPrune: Provider.Model | undefined
      // Capture managed settings once, after the admitted turn resolves its
      // model. Every subsequent compaction/prune decision uses this immutable
      // snapshot; a host save cannot mutate an in-flight turn.
      let admittedConfig: Config.Info | undefined
      let outputLengthContinuations = 0
      // Shared local counter for "model finished but produced nothing usable"
      // (think-only / empty). T04's generic-invalid retries reuse this same
      // counter — do not add a second one. Local to runLoop so a fresh user
      // turn resets it (no cross-message pollution), same as outputLengthContinuations.
      let invalidContinuations = 0
      // structured-output 专用 retry：上限来自 lastUser.format.retryCount（默认 2），
      // 与 invalidContinuations（generic invalid）分离，互不污染。局部于 runLoop，
      // 新一轮用户 turn 自动归零。
      let structuredRetries = 0
      // Bounded retries for text-form tool calls (model wrote a tool call as
      // prose text instead of a structured tool_use). Local to runLoop so each
      // fresh user turn starts clean.
      let textToolCallRetries = 0
      // Consecutive empty/no-op tool-call steps in this turn. Counts steps
      // where the model "called a tool" with empty/invalid input, or produced
      // no valid tool part and no substantive output at all (see isEmptyStep).
      // A single non-empty step resets it. Escalates soft (remind → replan)
      // then hard-halts once it exceeds EMPTY_STEP_MAX_RECOVERY, mirroring the
      // text-ngram ladder. Local to runLoop so a fresh user turn starts clean.
      let emptyStepStreak = 0
      // Set true when a guard hard-halts the turn (currently the empty-step
      // guard). A hard halt is terminal: it must break out immediately and
      // NOT be re-entered by the goalGate ReAct gate, which would
      // otherwise inject a fresh user turn and re-drive a still-degraded model
      // into the same loop.
      let hardHalt = false
      const resolvedAgentID = agentID ?? "main"
      const actorExecution = Option.getOrUndefined(yield* Effect.serviceOption(ActorExecutionContext))
      const preparedTurn = actorExecution?.preparedTurn && !actorExecution.preparedTurn.entered ? actorExecution.preparedTurn : undefined
      const innerTurnID = preparedTurn?.innerTurnID ?? ulid()
      if (preparedTurn) preparedTurn.entered = true
      if (actorExecution?.retirement) actorExecution.retirement.lastInnerTurnID = innerTurnID
      if (actorExecution?.retirement) actorExecution.retirement.lastTurnOutcome = undefined
      const turnExecution: ActorExecutionIdentity | undefined = actorExecution
        ? { ...actorExecution, innerTurnID }
        : undefined
      let lastToolViewContext: ToolViewContext | undefined
      const mcpContext: MCP.TurnContext = {
        sessionId: sessionID, turnId: innerTurnID, actorId: resolvedAgentID,
        ...(turnExecution?.rootOperationID ? {
          sessionID, rootOperationID: turnExecution.rootOperationID, actorID: resolvedAgentID,
          executionRevision: turnExecution.executionRevision, innerTurnID,
        } : {}),
      }
      const managedTurn = ManagedProvider.enabled() && turnExecution?.rootOperationID
        ? { sessionID, rootOperationID: turnExecution.rootOperationID, actorID: resolvedAgentID,
            executionRevision: turnExecution.executionRevision, innerTurnID }
        : undefined
      let forwardPairOffered = false
      if (ManagedProvider.enabled() && !managedTurn)
        return yield* Effect.die(new Error("managed turn has no admitted actor execution identity"))
      if (managedTurn) {
        const displayOwnerCapability = ManagedProvider.captureActorDisplayOwner(managedTurn)
        const startResult = yield* Effect.tryPromise({
          try: () => ManagedProvider.startManagedActorTurn(managedTurn),
          catch: (cause) => new Error(`managed turn admission outcome is unknown: ${String(cause)}`),
        }).pipe(Effect.exit)
        if (Exit.isFailure(startResult)) {
          if (turnExecution?.retirement) turnExecution.retirement.unknown = true
          return yield* Effect.failCause(startResult.cause).pipe(Effect.orDie)
        }
        const started = startResult.value
        if (!started.accepted || started.sessionID !== managedTurn.sessionID ||
          started.rootOperationID !== managedTurn.rootOperationID || started.actorID !== managedTurn.actorID ||
          started.executionRevision !== managedTurn.executionRevision || started.innerTurnID !== managedTurn.innerTurnID) {
          if (turnExecution?.retirement) turnExecution.retirement.unknown = true
          return yield* Effect.die(new Error("managed turn admission returned a different identity"))
        }
        forwardPairOffered = started.forwardPairV1 === true
        if (!started.replayed && started.factoryPersona) factoryPersonaStart=started
        if (started.displayOwnerProof && actorRegistry.getDisplayOwner) {
          const storedOwner = yield* actorRegistry.getDisplayOwner(sessionID, resolvedAgentID)
          if (started.displayOwnerProof.version === 1 && storedOwner && storedOwner === started.displayOwnerProof.accountID) {
            if (!displayOwnerCapability) return yield* Effect.die(new Error('managed display owner capability unavailable'))
            yield* Effect.sync(() => ManagedProvider.bindActorDisplayOwner(displayOwnerCapability, managedTurn, storedOwner))
          }
        }
        if (turnExecution) markAdmittedToolTimingTurn(turnExecution, managedTurn)
        const outlineContext=currentToolViewContext(turnExecution,true)
        if (outlineContext && outlineViewEnabled(outlineContext.encoded) && toolViewContextCurrent(outlineContext))
          Object.assign(mcpContext,{[sourceOutlineAdmission]:{current:()=>{try{return toolViewContextCurrent(outlineContext)}catch{return false}}}})
        if (started.themeStatusOnly === true) mcpContext.themeStatusOnly = true
        if (started.codeActivityEnabled === true) mcpContext.codeActivityEnabled = true
      }
      // The ID comes directly from createUserMessage, after chat.message and
      // persistence. Only the exact single visible text path can be attested.
      let maintenanceCalled=false,maintenanceReady=false
      if (managedTurn && managedTurn.actorID === "main" && sourceCandidate) {
        const current = yield* Effect.sync(() => {
          try { return MessageV2.get({ sessionID, messageID: sourceCandidate.messageID }) } catch { return undefined }
        })
        const visible = current?.parts.filter((part) => part.type === "text" && !part.ignored && !part.synthetic)
        try { log.debug('paired_source_match', {offered:forwardPairOffered,user:current?.info.role==='user',oneVisible:visible?.length===1,
          sourceID:visible?.length===1 && visible[0].id===sourceCandidate.partID,layout:!!sourceCandidate.layout,
          sameLayout:!!current && !!sourceCandidate.partID && !!sourceCandidate.layout && JSON.stringify(snapshotSourceParts(current.parts,sourceCandidate.partID))===JSON.stringify(sourceCandidate.layout)}) } catch { /* Diagnostic only. */ }
        if (current?.info.role === "user" && visible?.length === 1 && visible[0].type === "text" && !visible[0].synthetic &&
            visible[0].id === sourceCandidate.partID && createHash("sha256").update(visible[0].text,"utf8").digest("hex") === sourceCandidate.digest &&
            current.parts.every((part) => part.type === "text") && sourceCandidate.layout &&
            JSON.stringify(snapshotSourceParts(current.parts,sourceCandidate.partID!))===JSON.stringify(sourceCandidate.layout)) {
          const part = visible[0]
          const textSHA256 = createHash("sha256").update(part.text,"utf8").digest("hex")
          maintenanceCalled=true
          const result = yield* Effect.tryPromise({
            try: () => ManagedProvider.managedHostCall("_openclank/session/v1/maintenance/source", {
              ...managedTurn, actorID:"main", messageID:current.info.id, partID:part.id, textSHA256,
              version:2,layout:sourceCandidate.layout!.layout,sourcePosition:sourceCandidate.layout!.sourcePosition,
            }), catch: () => new Error("maintenance source unavailable"),
          }).pipe(Effect.catch(() => Effect.succeed({state:"unavailable" as const})))
          maintenanceReady=result.state === "ready"
          if (result.state === "ready") {
            if (turnExecution && sourceCandidate.partID && sourceCandidate.digest)
              sourceAttempt=createSourceAttempt(turnExecution,{messageID:sourceCandidate.messageID,partID:sourceCandidate.partID,digest:sourceCandidate.digest,layout:sourceCandidate.layout})
            yield* sessions.updatePart({ ...part, metadata: { ...(part.metadata ?? {}),
              openclankMaintenanceSource: result.attestation } }).pipe(Effect.catch(() => Effect.void))
          }
        }
      }

      if (managedTurn?.actorID === 'main') {
        try { log.debug('paired_source_maintenance', {offered:forwardPairOffered,candidate:!!sourceCandidate,called:maintenanceCalled,ready:maintenanceReady,attempt:!!sourceAttempt}) } catch { /* Diagnostic only. */ }
      }

      // Tracks plugin-driven cancellation (session.pre OR any session.userQuery.pre)
      // so session.post reports outcome="cancelled" instead of "error".
      let cancelled = false
      let cancelReason: string | undefined
      let lastSystemPrompt: string[] | undefined = undefined

      // Fires session.post exactly once via Effect.onExit on the body below.
      // Without this wrapper any yielded failure inside the while loop (provider
      // error, network error, thrown defect) would skip the hook entirely.
      //
      // Trajectory parity: uses MessageV2.filterCompactedEffect with the session's
      // contextFrom / contextWatermark so compaction boundaries trim history to
      // what the agent actually saw, and child-session parent prefixes are
      // included — matching session.userQuery.post semantics.
      let managedOutcome: "completed" | "cancelled" | "error" = "error"
      const readPostSession = (exit: Exit.Exit<MessageV2.WithParts, unknown>) =>
        Effect.gen(function* () {
          const sliceMsgs = yield* MessageV2.filterCompactedEffect(sessionID, {
            contextFrom: session.contextFrom,
            contextWatermark: session.contextWatermark,
            agentID: resolvedAgentID,
          }).pipe(Effect.catch(() => Effect.succeed([] as MessageV2.WithParts[])))
          const lastSlice = sliceMsgs.findLast((m) => m.info.role === "assistant")
          const finalAsst = lastSlice && lastSlice.info.role === "assistant" ? lastSlice.info : undefined
          const finalParts = lastSlice?.parts ?? []
          const failed = Exit.isFailure(exit)
          const finalIsError = !!finalAsst?.error
          const outcome: "completed" | "error" | "cancelled" = cancelled
            ? "cancelled"
            : failed || finalIsError
              ? "error"
              : "completed"
          managedOutcome = Exit.isFailure(exit) && Cause.hasInterruptsOnly(exit.cause) ? "cancelled" : outcome
          const error = cancelled
            ? cancelReason
            : failed
              ? Cause.pretty(exit.cause)
              : finalAsst
                ? sessionErrorText(finalAsst.error)
                : undefined
          return {sliceMsgs,finalAsst,finalParts,failed,finalIsError,outcome,error}
        })
      const firePostSession = (exit: Exit.Exit<MessageV2.WithParts, unknown>, snapshot: Effect.Success<ReturnType<typeof readPostSession>>) =>
        Effect.gen(function* () {
          const {sliceMsgs,finalAsst,finalParts,failed,finalIsError,outcome,error}=snapshot
          yield* Effect.all(
            [
              plugin
                .trigger(
                  "session.post",
                  {
                    sessionID,
                    agentID: resolvedAgentID,
                    task_id,
                    outcome,
                    error,
                    finalText: finalAsst ? assistantFinalText(finalAsst, finalParts) : undefined,
                    assistantMessageID: finalAsst?.id,
                    trajectory: serializeTrajectoryMessages(sliceMsgs),
                    systemPrompt: lastSystemPrompt,
                  },
                  {},
                )
                .pipe(Effect.ignore),
              mcp.clients().pipe(
                Effect.flatMap((clients) =>
                  MCP.notifyTurnLifecycle(
                    clients,
                    mcpContext,
                    cancelled || (Exit.isFailure(exit) && Cause.hasInterrupts(exit.cause))
                      ? "cancelled"
                      : failed || finalIsError
                        ? "error"
                        : "completed",
                  ),
                ),
                Effect.ignore,
              ),
            ],
            { concurrency: "unbounded", discard: true },
          )
        }).pipe(Effect.ignore)

      return yield* Effect.gen(function* () {
        if (managedTurn && turnExecution?.preflightRoute) {
          const bootstrap = turnExecution.preflightRoute.bootstrap
          const authority = yield* Effect.tryPromise({
            try: () => ManagedProvider.readManagedSessionBinding(sessionID),
            catch: (cause) => new Error(`managed binding unavailable after turn admission: ${String(cause)}`),
          }).pipe(Effect.orDie)
          const local = yield* Effect.tryPromise({
            try: () => ManagedProvider.ensureManagedSessionBinding(sessionID),
            catch: (cause) => new Error(`managed registration unavailable after turn admission: ${String(cause)}`),
          }).pipe(Effect.orDie)
          const actorHostContext = ManagedProvider.currentActorHostContext(sessionID, managedTurn.actorID, managedTurn.executionRevision)
          if (!actorHostContext || (actorHostContext.chatID && actorHostContext.chatID !== authority.stableChatID) ||
              (actorHostContext.workspaceID && actorHostContext.workspaceID !== authority.authorityWorkspaceID))
            return yield* Effect.die(new Error("managed actor context changed after turn admission"))
          if (!local || local.transition || local.engineSessionID !== sessionID ||
              authority.owner !== bootstrap.owner || authority.stableChatID !== bootstrap.stableChatID ||
              !ManagedProvider.sameAuthority(authority, bootstrap) || local.owner !== authority.owner ||
              local.stableChatID !== authority.stableChatID ||
              (authority.authority ? local.authority?.generation !== authority.authority.generation :
                (!!local.authority || local.mapRevision > authority.mapRevision || local.mappingRevision !== authority.mappingRevision || local.bindingRevision !== authority.workspaceRevision)))
            return yield* Effect.die(new Error("managed binding changed after turn admission"))
          yield* Effect.sync(() => ManagedProvider.bindActorHostContext(sessionID, managedTurn.actorID, managedTurn.executionRevision, authority))
          const boundContext = ManagedProvider.currentActorHostContext(sessionID, managedTurn.actorID, managedTurn.executionRevision)
          const scope = ManagedProvider.currentScope(sessionID, managedTurn.actorID, managedTurn.executionRevision)
          if (!boundContext || !scope || !(yield* actorRegistry.updateHostContextForExecution(sessionID, managedTurn.actorID, managedTurn.executionRevision, {
            ...boundContext, accountID: scope.accountID, credentialRevision: scope.credentialRevision,
            grantID: turnExecution.preflightRoute.grantID, grantRevision: turnExecution.preflightRoute.grantRevision,
          }))) return yield* Effect.die(new Error("managed actor binding changed before execution"))
        }
        const preSession = { cancel: undefined as boolean | undefined, cancelReason: undefined as string | undefined }
        yield* plugin.trigger("session.pre", { sessionID, agentID: resolvedAgentID, task_id }, preSession)
        if (preSession.cancel) {
          cancelled = true
          cancelReason = preSession.cancelReason
          return yield* Effect.fail(
            new NamedError.Unknown({
              message: preSession.cancelReason ?? "Session cancelled by plugin",
            }),
          )
        }
        const agentMetrics = { tokens_in: 0, tokens_out: 0, files_changed: 0 }
        const trajectoryForStep = (currentMsgs: MessageV2.WithParts[], assistant: MessageV2.Assistant) =>
          serializeTrajectoryMessages(withAssistantParts(currentMsgs, assistant, MessageV2.parts(assistant.id)))

        const publishAgentRequest = (phase: string, taskType: string) =>
          bus
            .publish(Metrics.AgentRequest, {
              sessionID,
              phase,
              task_type: taskType,
              surface: Flag.MIMOCODE_CLIENT,
              total_tokens_in: agentMetrics.tokens_in,
              total_tokens_out: agentMetrics.tokens_out,
              files_changed: agentMetrics.files_changed,
              validation_status: "skipped",
            })
            .pipe(Effect.ignore)
        // Trim freed space but `lastFinished.tokens` still reflects pre-trim state.
        // Skip one overflow check so the model can respond on the trimmed context;
        // its new assistant message will carry accurate tokens for the next check.
        let skipOverflowCheck = false

        const textLoopBuffer: string[] = []
        let textLoopRecoveryAttempts = 0
        let textNgramRecoveryAttempts = 0
        const goalWorker = `${process.pid}:${sessionID}:${randomUUID()}`

        // Contract (T05): on finish="length", inject a continuation nudge ONLY for
        // plain text. If any non-providerExecuted client tool part exists we bail
        // (return false) and let classify route the normal tool-observation re-loop.
        // This guarantees "no output-length continuation when a tool is involved" —
        // it does NOT guarantee a stream-time-truncated tool never executed, since
        // the AI SDK runs tools mid-stream before the finish reason is known.
        const autoContinueOutputLength = Effect.fn("SessionPrompt.autoContinueOutputLength")(function* (input: {
          lastUser: MessageV2.User
          assistant: MessageV2.Assistant
        }) {
          if (input.assistant.finish !== "length" || input.assistant.error || input.assistant.summary) return false
          if (
            MessageV2.parts(input.assistant.id).some((part) => part.type === "tool" && !part.metadata?.providerExecuted)
          ) {
            return false
          }
          if (outputLengthContinuations >= OUTPUT_LENGTH_CONTINUATION_LIMIT) {
            input.assistant.error = new MessageV2.OutputLengthError({}).toObject()
            yield* sessions.updateMessage(input.assistant)
            yield* bus.publish(Session.Event.Error, {
              sessionID: input.assistant.sessionID,
              error: input.assistant.error,
            })
            return false
          }

          outputLengthContinuations++
          yield* slog.info("auto-continuing output length", { attempt: outputLengthContinuations })
          const msg = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID: input.lastUser.sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: msg.sessionID,
            type: "text",
            synthetic: true,
            text: [
              "<system-reminder>",
              "The previous assistant response hit the model output token limit before completing.",
              "Continue the same task from the exact point where it stopped.",
              "Do not restart, recap, or repeat prior reasoning. Keep reasoning concise, prefer concrete tool calls or final output, and only stop when the user's task is complete or genuinely blocked.",
              "</system-reminder>",
            ].join("\n"),
          } satisfies MessageV2.TextPart)
          return true
        })

        // Goal stop-condition gate (main agent only). Before honoring a stop,
        // an independent judge model reads the transcript and decides whether
        // the active goal is satisfied. Not satisfied → inject the judge's
        // reason as a synthetic user turn and signal the caller to keep working
        // (return true). The verifier is lease-protected and failure-degraded:
        // only a validated affirmative verdict completes the durable goal.
        const goalGate = Effect.fn("SessionPrompt.goalGate")(function* (lastUser: MessageV2.User) {
          if ((agentID ?? "main") !== "main") return false
          let active = yield* goal.get(sessionID)
          if (!active) return false
          if (
            verifyGoalTarget &&
            (active.id !== verifyGoalTarget.goalID || active.revision !== verifyGoalTarget.expectedRevision)
          ) {
            throw new Error("Goal target changed before verification")
          }
          if (!["active", "verification_degraded", "awaiting_verification"].includes(active.status)) return false

          const transcriptMsgs = yield* MessageV2.filterCompactedEffect(sessionID, {
            contextFrom: session.contextFrom,
            contextWatermark: session.contextWatermark,
            agentID: "main",
          })
          const judgedAssistant = transcriptMsgs.findLast(
            (message) => message.info.role === "assistant" && message.info.finish,
          )
          if (judgedAssistant?.info.role === "assistant") {
            const tokens = judgedAssistant.info.tokens
            const totalTokens = tokens.input + tokens.output + tokens.reasoning + tokens.cache.read + tokens.cache.write
            const accounted = yield* goal.recordUsage(
              sessionID,
              Goal.target(active),
              judgedAssistant.info.id,
              totalTokens,
              judgedAssistant.parts.filter((part) => part.type === "tool").length,
            )
            if (
              !accounted ||
              !["active", "verification_degraded", "awaiting_verification"].includes(accounted.status)
            ) {
              return false
            }
            active = accounted
          }

          const initialLease = yield* goal.beginVerification(sessionID, Goal.target(active), goalWorker)
          if (!initialLease) {
            yield* slog.info("goal verification already leased", { sessionID })
            return false
          }
          let lease: Goal.VerificationHandle = initialLease

          // Anchor the verdict to the assistant turn the judge just evaluated, so
          // the TUI can render a per-turn marker the user can trace back to.
          const judgedMessageID = transcriptMsgs.findLast((m) => m.info.role === "assistant")?.info.id
          let outcome: { verdict: Goal.Verdict } | { failure: Goal.GoalVerificationFailure } = {
            failure: "judge_provider_failure",
          }
          for (let attempt = 0; attempt < 3; attempt++) {
            const leaseAlive = yield* goal.heartbeatVerification(sessionID, lease)
            if (!leaseAlive) return false
            lease = leaseAlive
            outcome = yield* goal
              .evaluate({
                condition: active.condition,
                msgs: transcriptMsgs,
                model: lastUser.model,
              })
              .pipe(
                Effect.timeout(45_000),
                Effect.map((verdict) => ({ verdict })),
                Effect.catch((error) =>
                  Effect.succeed({
                    failure: Goal.goalJudgeFailureCode(error),
                  }),
                ),
              )
            if ("verdict" in outcome) break
            if (attempt < 2) yield* Effect.sleep(500 * 2 ** attempt)
          }

          if ("failure" in outcome) {
            const reason = "Goal verification is temporarily unavailable. The goal remains active."
            const degraded = yield* goal.verificationDegraded(sessionID, lease, outcome.failure)
            yield* slog.warn("goal verification degraded", {
              sessionID,
              reasonCode: outcome.failure,
            })
            yield* bus.publish(Goal.Event.Updated, {
              sessionID,
              goal: {
                id: degraded.id,
                condition: degraded.condition,
                revision: degraded.revision,
                status: degraded.status,
                react: degraded.react,
              },
              lastVerdict: {
                ok: false,
                reason,
                reasonCode: outcome.failure,
                attempt: active.react,
                messageID: judgedMessageID,
                error: true,
              },
            })
            return false
          }

          const verdict = {
            ...outcome.verdict,
            reason: Goal.safeVerdictDisplay(outcome.verdict.reason),
          }
          if (verdict.ok) {
            yield* slog.info("goal verified; allowing stop", {
              sessionID,
            })
            const completed = yield* goal.verificationCompleted(
              sessionID,
              lease,
              verdict.reason,
              {
                kind: "model",
                subject: active.id,
                sourceRef: judgedMessageID ?? `session:${sessionID}`,
                observation: verdict.reason,
                producer: `${lastUser.model.providerID}/${lastUser.model.modelID}`,
                verifier: "goal-judge",
              },
              {
                owner: goalWorker,
                intentID: `goal-continuation:${active.id}:${judgedMessageID ?? randomUUID()}:evidence`,
              },
            )
            if (completed.kind === "evidence_missing") {
              const display = `Goal still needs ${completed.missing.join(", ")} evidence before it can complete.`
              const count = completed.goal.react
              yield* bus.publish(Goal.Event.Updated, {
                sessionID,
                goal: {
                  id: completed.goal.id,
                  condition: completed.goal.condition,
                  revision: completed.goal.revision,
                  status: completed.goal.status,
                  react: completed.goal.react,
                },
                lastVerdict: {
                  ok: false,
                  reason: display,
                  attempt: count,
                  messageID: judgedMessageID,
                  reasonCode: "evidence_policy_not_satisfied",
                },
              })
              if (!completed.continuation || completed.goal.status !== "active") return false
              const reentry = yield* sessions.updateMessage({
                id: MessageID.ascending(),
                role: "user" as const,
                sessionID,
                agentID: lastUser.agentID,
                agent: lastUser.agent,
                model: lastUser.model,
                tools: lastUser.tools,
                format: lastUser.format,
                time: { created: Date.now() },
              })
              yield* sessions.updatePart({
                id: PartID.ascending(),
                messageID: reentry.id,
                sessionID,
                type: "text",
                synthetic: true,
                text: "GOAL_VERIFICATION_EVIDENCE_MISSING",
                metadata: {
                  goalVerification: {
                    status: "not_met",
                    reasonCode: "evidence_policy_not_satisfied",
                    display,
                    intentID: completed.continuation.intentID,
                  },
                },
              } satisfies MessageV2.TextPart)
              return true
            }
            yield* bus.publish(Goal.Event.Updated, {
              sessionID,
              goal: completed.next
                ? {
                    id: completed.next.id,
                    condition: completed.next.condition,
                    revision: completed.next.revision,
                    status: completed.next.status,
                    react: completed.next.react,
                  }
                : undefined,
              lastVerdict: {
                ...verdict,
                attempt: active.react,
                messageID: judgedMessageID,
                reasonCode: "met",
              },
            })
            return false
          }

          if (verdict.impossible) {
            const blocked = yield* goal.verificationBlocked(sessionID, lease, verdict.reason, {
              kind: "model",
              subject: active.id,
              sourceRef: judgedMessageID ?? `session:${sessionID}`,
              observation: verdict.reason,
              producer: `${lastUser.model.providerID}/${lastUser.model.modelID}`,
              verifier: "goal-judge",
            })
            yield* slog.info("goal blocked by verifier", { sessionID })
            yield* bus.publish(Goal.Event.Updated, {
              sessionID,
              goal: blocked
                ? {
                    id: blocked.id,
                    condition: blocked.condition,
                    revision: blocked.revision,
                    status: blocked.status,
                    react: blocked.react,
                  }
                : undefined,
              lastVerdict: {
                ...verdict,
                attempt: active.react,
                messageID: judgedMessageID,
                reasonCode: "impossible",
              },
            })
            return false
          }

          const rejected = yield* goal.verificationRejected(sessionID, lease, verdict.reason, "not_met", {
            owner: goalWorker,
            intentID: `goal-continuation:${active.id}:${judgedMessageID ?? randomUUID()}:not-met`,
          })
          const count = rejected.goal.react
          yield* slog.info("goal not satisfied; re-entering", { sessionID, attempt: count })
          yield* bus.publish(Goal.Event.Updated, {
            sessionID,
            goal: {
              id: rejected.goal.id,
              condition: rejected.goal.condition,
              revision: rejected.goal.revision,
              status: rejected.goal.status,
              react: rejected.goal.react,
            },
            lastVerdict: { ...verdict, reasonCode: "not_met", attempt: count, messageID: judgedMessageID },
          })
          if (!rejected.continuation || rejected.goal.status !== "active") return false
          const reentry = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID,
            agentID: lastUser.agentID,
            agent: lastUser.agent,
            model: lastUser.model,
            tools: lastUser.tools,
            format: lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: reentry.id,
            sessionID,
            type: "text",
            synthetic: true,
            text: "GOAL_VERIFICATION_NOT_MET",
            metadata: {
              goalVerification: {
                status: "not_met",
                reasonCode: "not_met",
                display: verdict.reason,
                intentID: rejected.continuation.intentID,
              },
            },
          } satisfies MessageV2.TextPart)
          return true
        })

        // think-only (reasoning only) / empty (nothing at all) steps finish with
        // a non-tool stop but carry no usable answer. Without intervention the loop
        // breaks and hands the user an assistant with no final text. Nudge the model
        // to produce a final answer or call a real tool; give up (write a terminal
        // error) once the shared counter is exhausted so we never loop forever.
        const autoContinueInvalidOutput = Effect.fn("SessionPrompt.autoContinueInvalidOutput")(function* (input: {
          lastUser: MessageV2.User
          assistant: MessageV2.Assistant
          reason: string
        }) {
          if (input.assistant.error || input.assistant.summary || input.assistant.structured !== undefined) return false
          if (invalidContinuations >= INVALID_OUTPUT_CONTINUATION_LIMIT) {
            input.assistant.error = new MessageV2.InvalidOutputError({ message: input.reason }).toObject()
            yield* sessions.updateMessage(input.assistant)
            yield* bus.publish(Session.Event.Error, {
              sessionID: input.assistant.sessionID,
              error: input.assistant.error,
            })
            return false
          }

          invalidContinuations++
          yield* slog.info("auto-continuing invalid output", { attempt: invalidContinuations, reason: input.reason })
          const policy = resolveInvalidOutputPolicy({
            agentName: input.lastUser.agent,
            agentID: input.lastUser.agentID,
          })
          const reminder =
            policy === "checkpoint"
              ? [
                  "Your checkpoint writer turn ended without a completion signal.",
                  "Do not answer or continue the parent session's task. Work only on the authorized checkpoint and memory paths already provided to you.",
                  "If any authorized edits remain, finish them now. If they are complete, call no more tools and reply exactly CHECKPOINT_COMPLETE.",
                ]
              : policy === "actor"
                ? [
                    "Your previous response contained no usable result for the parent agent (it had only reasoning, or was empty).",
                    "Provide a final result to the parent agent now, or call a valid tool to complete the delegated task.",
                    "Do not respond with only reasoning/thinking.",
                  ]
                : [
                    "Your previous response contained no usable answer (it had only reasoning, or was empty).",
                    "Provide a final answer to the user now, or call a valid tool to make progress on the task.",
                    "Do not respond with only reasoning/thinking.",
                  ]
          const msg = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID: input.lastUser.sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: msg.sessionID,
            type: "text",
            synthetic: true,
            text: ["<system-reminder>", ...reminder, "</system-reminder>"].join("\n"),
          } satisfies MessageV2.TextPart)
          return true
        })

        // Text-form tool call recovery. The model serialized a tool call as prose
        // text instead of a structured tool_use (a degraded state under large
        // context). The bad assistant turn is DISCARDED from history by setting
        // assistant.error (toModelMessages skips a message whose info.error is
        // set, message-v2.ts), so it can neither strand the conversation on an
        // assistant turn (provider prefill rejection) nor poison later context.
        // We then retry the request (caller does `continue`, no new message). On
        // exhaustion the error stays terminal. Returns true ⇒ continue; false ⇒ break.
        const autoRetryTextToolCall = Effect.fn("SessionPrompt.autoRetryTextToolCall")(function* (input: {
          lastUser: MessageV2.User
          assistant: MessageV2.Assistant
        }) {
          // Already discarded on a prior pass — let classify fall through to
          // `failed` instead of re-detecting and burning another retry.
          if (input.assistant.error) return false
          // Discard the bad turn from request history: toModelMessages skips a
          // message whose info.error is set, so it can neither strand the
          // conversation on an assistant turn nor poison later context.
          input.assistant.error = new MessageV2.TextToolCallError({
            message: "Model emitted a tool call as text instead of a structured tool call.",
          }).toObject()
          yield* sessions.updateMessage(input.assistant)
          if (textToolCallRetries >= TEXT_TOOL_CALL_RETRY_LIMIT) {
            yield* bus.publish(Session.Event.Error, {
              sessionID: input.assistant.sessionID,
              error: input.assistant.error,
            })
            return false
          }
          textToolCallRetries++
          yield* slog.info("retrying text-form tool call", { attempt: textToolCallRetries })
          // Append a synthetic user turn so the discarded assistant becomes stale
          // (classify staleness guard) AND the loop reaches generation — mirrors
          // autoRetryStructuredOutput. Without this the loop re-enters, re-detects
          // the same turn, and burns retries with zero model calls.
          const msg = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID: input.lastUser.sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: msg.sessionID,
            type: "text",
            synthetic: true,
            text: [
              "<system-reminder>",
              "Your previous response wrote a tool call as plain text instead of invoking the tool.",
              "Re-issue it through the real tool channel — emit a structured tool call, not text.",
              "Do not paste the tool call as text again.",
              "</system-reminder>",
            ].join("\n"),
          } satisfies MessageV2.TextPart)
          return true
        })

        // json_schema mode but the model never produced structured output (plain
        // text stop, empty, think-only, or any other non-tool terminal). Retry up
        // to lastUser.format.retryCount with a repair nudge; on exhaustion write a
        // StructuredOutputError carrying the *real* retry count. Separate from
        // invalidContinuations: structured retries are bounded by the per-request
        // retryCount, not the generic invalid-output limit.
        const autoRetryStructuredOutput = Effect.fn("SessionPrompt.autoRetryStructuredOutput")(function* (input: {
          lastUser: MessageV2.User
          assistant: MessageV2.Assistant
        }) {
          if (input.assistant.error || input.assistant.summary || input.assistant.structured !== undefined) return false
          const limit = input.lastUser.format?.type === "json_schema" ? input.lastUser.format.retryCount : 0
          if (structuredRetries >= limit) {
            input.assistant.error = new MessageV2.StructuredOutputError({
              message: "Model did not produce structured output",
              retries: structuredRetries,
            }).toObject()
            yield* sessions.updateMessage(input.assistant)
            yield* bus.publish(Session.Event.Error, {
              sessionID: input.assistant.sessionID,
              error: input.assistant.error,
            })
            return false
          }

          structuredRetries++
          yield* slog.info("retrying structured output", { attempt: structuredRetries })
          const msg = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID: input.lastUser.sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            // Must carry format so the next iteration re-registers the StructuredOutput tool.
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: msg.id,
            sessionID: msg.sessionID,
            type: "text",
            synthetic: true,
            text: [
              "<system-reminder>",
              "Your previous response did not produce valid structured output via the StructuredOutput tool",
              "(it was plain text, empty, or only reasoning).",
              "You MUST call the StructuredOutput tool now, passing JSON that matches the requested schema.",
              "Do not reply with plain text and do not respond with only reasoning/thinking.",
              "</system-reminder>",
            ].join("\n"),
          } satisfies MessageV2.TextPart)
          return true
        })

        // Sliding-window n-gram repetition recovery. Symmetric across main and
        // fork branches: 1st hit injects REMIND, 2nd hit injects REPLAN, 3rd
        // hit (>= TEXT_NGRAM_MAX_RECOVERY) writes an error and signals break.
        const handleTextRepeat = Effect.fn("SessionPrompt.handleTextRepeat")(function* (input: {
          lastUser: MessageV2.User
        }) {
          if (textNgramRecoveryAttempts >= TEXT_NGRAM_MAX_RECOVERY) {
            yield* slog.info("text n-gram: max recovery exceeded, terminating")
            yield* bus.publish(Session.Event.Error, {
              sessionID,
              error: new NamedError.Unknown({
                message: `Text repetition detected: repeated n-grams after ${TEXT_NGRAM_MAX_RECOVERY} recovery attempts. Session terminated.`,
              }).toObject(),
            })
            return false
          }
          const recoveryText = textNgramRecoveryAttempts === 0 ? TEXT_NGRAM_RECOVERY_REMIND : TEXT_NGRAM_RECOVERY_REPLAN
          const reentry = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: reentry.id,
            sessionID,
            type: "text",
            synthetic: true,
            text: recoveryText,
          } satisfies MessageV2.TextPart)
          textNgramRecoveryAttempts++
          yield* slog.info("text n-gram: recovery injected", { attempt: textNgramRecoveryAttempts })
          return true
        })

        // Empty/no-op tool-call loop guard. Symmetric across main and fork
        // branches, mirroring handleTextRepeat's soft→hard ladder but keyed on
        // *empty steps* (empty/invalid tool input, or a fully empty terminal)
        // rather than repeated text n-grams — the gap TEXT_NGRAM and
        // stepSignature both miss (an empty tool call has no text to match and
        // is dropped by stepSignature's undefined path).
        //
        // Returns:
        //   "none"     — the step was NOT empty; streak reset, caller continues
        //                normal classification.
        //   "continue" — empty step, still within the soft-nudge budget; a
        //                remind/replan reminder was injected, caller should loop.
        //   "halt"     — empty streak exceeded EMPTY_STEP_MAX_RECOVERY; a
        //                terminal error was published, caller must break.
        const handleEmptyStep = Effect.fn("SessionPrompt.handleEmptyStep")(function* (input: {
          lastUser: MessageV2.User
          assistant: MessageV2.Assistant
        }) {
          // Never mask a genuine terminal outcome as an "empty loop": an errored
          // step, a content-filter/error finish, or an already-resolved
          // structured/summary step must fall through to its own classifier
          // handler (writeContentFilterError / writeModelError / final). Those
          // are terminal safety/error events, not a spinning no-op.
          if (
            input.assistant.error ||
            input.assistant.summary ||
            input.assistant.structured !== undefined ||
            input.assistant.finish === "content-filter" ||
            input.assistant.finish === "error"
          ) {
            return "none" as const
          }
          const parts = MessageV2.parts(input.assistant.id)
          if (!isEmptyStep(parts)) {
            emptyStepStreak = 0
            return "none" as const
          }
          emptyStepStreak++
          if (emptyStepStreak > EMPTY_STEP_MAX_RECOVERY) {
            yield* slog.info("empty step: max recovery exceeded, terminating", { streak: emptyStepStreak })
            hardHalt = true
            // Discard the empty turn from request history so it can neither
            // strand the conversation on an assistant prefill nor poison later
            // context (toModelMessages skips a message whose info.error is set).
            if (!input.assistant.error) {
              input.assistant.error = new NamedError.Unknown({
                message: `Empty tool call loop detected: ${emptyStepStreak} consecutive empty/no-op steps after ${EMPTY_STEP_MAX_RECOVERY} recovery attempts. Session terminated.`,
              }).toObject()
              yield* sessions.updateMessage(input.assistant)
            }
            yield* bus.publish(Session.Event.Error, {
              sessionID,
              error: new NamedError.Unknown({
                message: `Empty tool call loop detected: ${emptyStepStreak} consecutive empty/no-op steps after ${EMPTY_STEP_MAX_RECOVERY} recovery attempts. Session terminated.`,
              }).toObject(),
            })
            return "halt" as const
          }
          const recoveryText = emptyStepStreak === 1 ? EMPTY_STEP_RECOVERY_REMIND : EMPTY_STEP_RECOVERY_REPLAN
          const reentry = yield* sessions.updateMessage({
            id: MessageID.ascending(),
            role: "user" as const,
            sessionID,
            agentID: input.lastUser.agentID,
            agent: input.lastUser.agent,
            model: input.lastUser.model,
            tools: input.lastUser.tools,
            format: input.lastUser.format,
            time: { created: Date.now() },
          })
          yield* sessions.updatePart({
            id: PartID.ascending(),
            messageID: reentry.id,
            sessionID,
            type: "text",
            synthetic: true,
            text: recoveryText,
          } satisfies MessageV2.TextPart)
          yield* slog.info("empty step: recovery injected", { streak: emptyStepStreak })
          return "continue" as const
        })

        // content-filter is terminal on first occurrence: re-sending the same
        // turn would just get filtered again, so there is no nudge / counter.
        // Write a user-visible error (rendered via the session.error toast) and
        // let the caller break.
        const writeContentFilterError = Effect.fn("SessionPrompt.writeContentFilterError")(function* (input: {
          assistant: MessageV2.Assistant
        }) {
          if (input.assistant.error) return
          input.assistant.error = new MessageV2.ContentFilterError({
            message: "The response was withheld by the model provider's content safety filter.",
          }).toObject()
          yield* sessions.updateMessage(input.assistant)
          yield* bus.publish(Session.Event.Error, {
            sessionID: input.assistant.sessionID,
            error: input.assistant.error,
          })
        })

        // A `failed` classification (model "error" finish, or an error already set
        // by the stream-error path) is terminal. If the step already carries an
        // error (e.g. APIError written when the stream threw, processor.ts:581),
        // keep it; otherwise write a ModelError so the loop never breaks silently
        // without a user-visible failure.
        const writeModelError = Effect.fn("SessionPrompt.writeModelError")(function* (input: {
          assistant: MessageV2.Assistant
          reason: string
        }) {
          if (input.assistant.error) return
          input.assistant.error = new MessageV2.ModelError({ message: input.reason }).toObject()
          yield* sessions.updateMessage(input.assistant)
          yield* bus.publish(Session.Event.Error, {
            sessionID: input.assistant.sessionID,
            error: input.assistant.error,
          })
        })

        let afterTurnDeliveryReady = afterTurnReady ?? false
        let frozenReceipts = turnExecution?.frozenReceiptBatch?.current
        let receiptPolicy: Permission.Ruleset | undefined
        while (true) {
          // F55: only main agent sets session status to busy; subagent runners
          // must not touch session-level status (Runner.onBusy is Effect.void
          // for non-main actors per F47).
          if (!agentID || agentID === "main") yield* status.set(sessionID, { type: "busy" })
          const receiverActorID = agentID ?? "main"
          let receiptBinding: UserInput.Binding | undefined
          if (ManagedProvider.enabled() && UserInput.hasPending(sessionID, receiverActorID)) {
            const authority = yield* Effect.promise(() => ManagedProvider.readManagedSessionBinding(sessionID))
            const local = yield* Effect.promise(() => ManagedProvider.ensureManagedSessionBinding(sessionID))
            if (authority.authority || local?.authority) return yield* Effect.die(new Error("private input receipts are unavailable"))
            if (!local || local.transition || local.engineSessionID !== sessionID ||
              local.owner !== authority.owner || local.stableChatID !== authority.stableChatID ||
              local.mapRevision > authority.mapRevision || local.mappingRevision !== authority.mappingRevision ||
              local.bindingRevision !== authority.workspaceRevision || local.authorityWorkspaceID !== authority.authorityWorkspaceID) {
              return yield* Effect.die(new Error("managed binding changed before user-input delivery"))
            }
            receiptBinding = {
              owner: authority.owner,
              stableChatID: authority.stableChatID,
              mapRevision: authority.mapRevision,
              mappingRevision: authority.mappingRevision,
              workspaceRevision: authority.workspaceRevision,
            }
            const current = yield* Effect.sync(() => UserInput.snapshotBoundary({
              sessionID, actorID: receiverActorID, binding: receiptBinding!, afterTurnReady: afterTurnDeliveryReady,
            }))
            const pending = frozenReceipts ?? current
            if (frozenReceipts && (current.length < frozenReceipts.length || frozenReceipts.some((receipt, index) => {
              const actual = current[index]
              return !actual || actual.requestID !== receipt.requestID || actual.payloadHash !== receipt.payloadHash || actual.contentHash !== receipt.contentHash ||
                actual.executionRevision !== receipt.executionRevision || actual.mapRevision !== receipt.mapRevision ||
                actual.mappingRevision !== receipt.mappingRevision || actual.workspaceRevision !== receipt.workspaceRevision
            }))) return yield* Effect.die(new Error("frozen input batch changed before delivery"))
            if (pending.length) {
              if (!managedTurn) return yield* Effect.die(new Error("queued input has no admitted managed turn"))
              const unsupported = pending.find((receipt) => receipt.turnPolicy?.incognito)
              if (unsupported) {
                yield* Effect.sync(() => UserInput.markBoundaryUnavailable({
                  sessionID, actorID: receiverActorID, receipts: pending,
                  reason: "queued incognito is unavailable for this actor execution",
                }))
                return yield* Effect.die(new Error("queued incognito is unavailable; input remains pending"))
              }
              const result = yield* Effect.tryPromise({
                try: () => ManagedProvider.managedHostCall("_openclank/session/v1/input-policy/resolve", {
                  ...managedTurn,
                  receipts: pending.map((receipt) => ({
                    requestID: receipt.requestID, payloadHash: receipt.payloadHash, executionRevision: receipt.executionRevision,
                    mapRevision: receipt.mapRevision, mappingRevision: receipt.mappingRevision, workspaceRevision: receipt.workspaceRevision,
                    text: receipt.text, turnPolicy: receipt.turnPolicy,
                    ...(receipt.queuedSourceProof ? {queuedSourceProof:receipt.queuedSourceProof} : {}),
                    admissionCeiling: receipt.admissionCeiling ? {
                      blockAllToolCalls: receipt.admissionCeiling.blockAllToolCalls,
                      disabledTools: receipt.admissionCeiling.disabledTools, hiddenTools: receipt.admissionCeiling.hiddenTools,
                      allowedTools: receipt.admissionCeiling.allowedTools,
                    } : null,
                  })),
                }),
                catch: (cause) => new Error(`queued input policy unavailable: ${String(cause)}`),
              }).pipe(Effect.orDie)
              if (result.sessionID !== managedTurn.sessionID || result.rootOperationID !== managedTurn.rootOperationID ||
                  result.actorID !== managedTurn.actorID || result.executionRevision !== managedTurn.executionRevision ||
                  result.innerTurnID !== managedTurn.innerTurnID || result.batchDisposition !== "ready" ||
                  result.decisions.length !== pending.length || result.decisions.some((decision, index) => {
                    const receipt = pending[index]
                    return !receipt || decision.state !== "ready" || decision.requestID !== receipt.requestID ||
                      decision.payloadHash !== receipt.payloadHash || JSON.stringify(decision.choiceSnapshot) !== JSON.stringify(receipt.turnPolicy) ||
                      decision.executionRevision !== receipt.executionRevision ||
                      decision.mapRevision !== receipt.mapRevision || decision.mappingRevision !== receipt.mappingRevision ||
                      decision.workspaceRevision !== receipt.workspaceRevision
                  })) {
                yield* Effect.sync(() => UserInput.markBoundaryUnavailable({
                  sessionID, actorID: receiverActorID, receipts: pending,
                  reason: result.decisions.find((decision) => decision.reason)?.reason ?? "queued input policy is unavailable",
                }))
                return yield* Effect.die(new Error("queued input policy is unavailable; input remains pending"))
              }
              const currentActor = yield* actorRegistry.get(sessionID, receiverActorID)
              if (result.batchMode === "agent" && (!currentActor || ["chat", "plan"].includes(currentActor.agent))) {
                yield* Effect.sync(() => UserInput.markBoundaryUnavailable({
                  sessionID, actorID: receiverActorID, receipts: pending,
                  reason: "the requested Agent profile is unavailable for this execution",
                }))
                return yield* Effect.die(new Error("queued Agent profile is unavailable; input remains pending"))
              }
              const checkedAuthority = yield* Effect.tryPromise({
                try: () => ManagedProvider.readManagedSessionBinding(sessionID),
                catch: (cause) => new Error(`managed binding revalidation failed: ${String(cause)}`),
              }).pipe(Effect.orDie)
              if (checkedAuthority.owner !== receiptBinding.owner || checkedAuthority.stableChatID !== receiptBinding.stableChatID ||
                  checkedAuthority.authority || checkedAuthority.mapRevision < receiptBinding.mapRevision || checkedAuthority.mappingRevision !== receiptBinding.mappingRevision ||
                  checkedAuthority.workspaceRevision !== receiptBinding.workspaceRevision)
                return yield* Effect.die(new Error("managed binding changed while resolving queued input"))
              receiptPolicy = Object.entries(result.nativeToolPolicy).map(([permission, enabled]) => ({
                permission, pattern: "*", action: enabled ? "allow" : "deny",
              }))
              yield* Effect.sync(() => UserInput.consumeBoundary({
                sessionID, actorID: receiverActorID, binding: receiptBinding!, afterTurnReady: afterTurnDeliveryReady,
                expectedReceipts: pending,
                policyBatch: {
                  version: 1, receipts: pending.map(({ requestID, payloadHash, contentHash, executionRevision, mapRevision, mappingRevision, workspaceRevision }) =>
                    ({ requestID, payloadHash, contentHash, executionRevision, mapRevision, mappingRevision, workspaceRevision })),
                  choices: result.decisions.map((decision) => ({ requestID: decision.requestID, choiceSnapshot: decision.choiceSnapshot, guideOnly: decision.guideOnly })),
                  batchMode: result.batchMode, batchGuideOnly: result.batchGuideOnly,
                  toolProjection: { ...result.toolProjection, disabledTools: [...result.toolProjection.disabledTools],
                    hiddenTools: [...result.toolProjection.hiddenTools], allowedTools: result.toolProjection.allowedTools ? [...result.toolProjection.allowedTools] : null },
                },
              }))
              // Forward only the IDs retained by the same consume transaction.
              // This optional observation does not authorize capture or affect steering.
              if (managedTurn.actorID === "main") {
                const consumed = UserInput.consumedQueuedSources({owner:receiptBinding!.owner,stableChatID:receiptBinding!.stableChatID,sessionID,
                  requestIDs:pending.map((item)=>item.requestID)})
                const sources = consumed.flatMap((item) => {
                  const receipt=pending.find((candidate)=>candidate.requestID===item.requestID)
                  if (!receipt?.queuedSourceProof || receipt.queuedSourceProof !== item.proof) return []
                  return [{requestID:item.requestID,payloadHash:receipt.payloadHash,
                    proofSHA256:createHash("sha256").update(item.proof,"utf8").digest("hex"),
                    bodySHA256:createHash("sha256").update(receipt.text,"utf8").digest("hex"),
                    messageID:item.messageID,partID:item.partID}]
                })
                if (sources.length) yield* Effect.tryPromise({try:async()=>{
                  const call=ManagedProvider.managedHostCall("_openclank/session/v1/queued-source/forward",{...managedTurn,sources}).catch(()=>({accepted:false}))
                  await Promise.race([call,new Promise((resolve)=>setTimeout(resolve,200))])
                },catch:()=>new Error("queued source observation unavailable")}).pipe(Effect.catch(()=>Effect.void))
              }
              frozenReceipts = undefined
              if (turnExecution?.frozenReceiptBatch) turnExecution.frozenReceiptBatch.current = undefined
              afterTurnDeliveryReady = false
            }
          }
          yield* inbox.drain(sessionID, receiverActorID).pipe(Effect.ignore)
          yield* slog.info("loop", { step })

          // F37: filter by agentID so subagent slices stay isolated from the
          // main agent's slice within the same session. Without this, an actor
          // (explore/general/etc) spawned via mimocode's shared-sessionID
          // design would see the parent's full conversation here and drift
          // off-task. agentID === "main" => main agent slice (agent_id = 'main'
          // in DB), agentID === "explore-1" => only explore-1's slice.
          let msgs = yield* MessageV2.filterCompactedEffect(sessionID, {
            contextFrom: session.contextFrom,
            contextWatermark: session.contextWatermark,
            agentID: agentID ?? "main",
          })
          // Persisted crop spans are request-view projections: the durable
          // transcript stays intact while poisoned assistant steps cannot re-enter.
          const persistedCrops = extractAllCrops(msgs)
          if (persistedCrops.length) msgs = applyPersistedCrops(msgs, persistedCrops).kept as typeof msgs

          let lastUser: MessageV2.User | undefined
          let lastAssistant: MessageV2.Assistant | undefined
          let lastFinished: MessageV2.Assistant | undefined
          let tasks: MessageV2.SubtaskPart[] = []
          for (let i = msgs.length - 1; i >= 0; i--) {
            const msg = msgs[i]
            if (!lastUser && msg.info.role === "user") lastUser = msg.info
            if (!lastAssistant && msg.info.role === "assistant") lastAssistant = msg.info
            if (!lastFinished && msg.info.role === "assistant" && msg.info.finish) lastFinished = msg.info
            if (lastUser && lastFinished) break
            const task = msg.parts.filter((part): part is MessageV2.SubtaskPart => part.type === "subtask")
            if (task && !lastFinished) tasks.push(...task)
          }

          if (!lastUser) throw new Error("No user message found in stream. This should never happen.")

          // Only the exact current, post-plugin native text part may receive a
          // host-confirmed generated-artifact sidecar. Historical metadata is
          // not authority and no source-to-wire delivery is inferred here.
          if (managedTurn && managedTurn.actorID==='main' && sourceCandidate?.generated?.length && sourceCandidate.messageID===lastUser.id) {
            const current=msgs.findLast((message)=>message.info.role==='user' && message.info.id===lastUser!.id)
            const candidates=current?.parts.filter((part)=>part.type==='text' && !part.synthetic && !part.ignored && part.metadata?.openclankGeneratedSummaryCandidate) ?? []
            if (current && candidates.length<=10) for (const part of candidates) {
              if (part.type!=='text' || generatedSources.has(part.id)) continue
              const candidate=part.metadata?.openclankGeneratedSummaryCandidate as {uploadID?:unknown;originSHA256?:unknown} | undefined
              const textSHA256=createHash('sha256').update(part.text,'utf8').digest('hex')
              if (!candidate || typeof candidate.uploadID!=='string' || typeof candidate.originSHA256!=='string' ||
                  !sourceCandidate.generated.some(item=>item.partID===part.id && item.digest===textSHA256 && item.uploadID===candidate.uploadID && item.originSHA256===candidate.originSHA256) ||
                  current.parts.filter((other)=>other.type==='text' && other.text===part.text).length!==1) continue
              const {uploadID,originSHA256}=candidate as {uploadID:string;originSHA256:string}
              const value=yield* Effect.tryPromise({try:()=>ManagedProvider.managedHostCall('_openclank/session/v1/attachment/source',{
                ...managedTurn,actorID:'main',messageID:current.info.id,partID:part.id,textSHA256,uploadID,originSHA256,
              }),catch:()=>new Error('generated attachment unavailable')}).pipe(Effect.catch(()=>Effect.succeed({state:'unavailable' as const})))
              const reread=yield* Effect.sync(()=>{try{return MessageV2.get({sessionID,messageID:current.info.id})}catch{return undefined}})
              const exact=reread?.parts.find((item)=>item.id===part.id)
              if (value.state==='ready' && value.messageID===current.info.id && value.partID===part.id && value.textSHA256===textSHA256 && value.uploadID===uploadID && exact?.type==='text' && !exact.synthetic && !exact.ignored &&
                  createHash('sha256').update(exact.text,'utf8').digest('hex')===textSHA256)
                generatedSources.set(part.id,{messageID:current.info.id,partSHA256:textSHA256,source:{kind:'generated_artifact',id:value.artifactID,
                  revision:value.previewHash,digest:value.outputSHA256,artifactSHA256:value.artifactSHA256,sourceReceiptSHA256:value.sourceReceiptSHA256}})
            }
          }

          // Per-user-message active recall reminder. Once the session has
          // any memory artifacts (memory dir populated OR tasks recorded),
          // append a brief recall protocol so the agent's reflex to query
          // memory.search / task / actor / Read stays warm across many
          // post-rebuild turns. Cost ~120 tokens per turn, conditional on
          // hasMemoryOrTasks.
          const lastUserMsgForRecall = msgs.findLast((m) => m.info.role === "user")
          if (lastUserMsgForRecall) {
            const hasRecallTarget = yield* checkpoint
              .hasMemoryOrTasks(sessionID)
              .pipe(Effect.catch(() => Effect.succeed(false)))
            if (hasRecallTarget) {
              const sessMemDir = path.join(Global.Path.data, "memory", "sessions", sessionID)
              const hints = recallHintLines((yield* config.get()).tool)
              lastUserMsgForRecall.parts.push({
                id: PartID.ascending(),
                messageID: lastUserMsgForRecall.info.id,
                sessionID,
                type: "text" as const,
                synthetic: true,
                text: [
                  "<system-reminder>",
                  `This session has memory at ${sessMemDir}/. Recall content`,
                  "not in your context with:",
                  hints[0],
                  `- Read(file_path="${sessMemDir}/...")`,
                  hints[1],
                  hints[2],
                  "",
                  "Don't ask the user about something memory may already record.",
                  "</system-reminder>",
                ].join("\n"),
              })
            }
          }

          const lastAssistantMsg = msgs.findLast(
            (msg) => msg.info.role === "assistant" && msg.info.id === lastAssistant?.id,
          )
          // Some providers return "stop" even when the assistant message contains tool calls.
          // Keep the loop running so tool results can be sent back to the model.
          // Skip provider-executed tool parts — those were fully handled within the
          // provider's stream (e.g. DWS Agent Platform) and don't need a re-loop.
          const hasToolCalls =
            lastAssistantMsg?.parts.some((part) => part.type === "tool" && !part.metadata?.providerExecuted) ?? false

          if (
            lastAssistant?.finish === "length" &&
            !hasToolCalls &&
            lastUser.id < lastAssistant.id &&
            (yield* autoContinueOutputLength({ lastUser, assistant: lastAssistant }))
          ) {
            continue
          }

          if (lastAssistant && lastAssistant.id !== resumeFrom && !(userRedispatch && step === 0)) {
            const classification = classifyAssistantStep({
              phase: "existing-assistant",
              lastUser,
              assistant: lastAssistant,
              parts: lastAssistantMsg?.parts ?? [],
            })
            if (classification.type === "filtered") {
              yield* writeContentFilterError({ assistant: lastAssistant })
              yield* slog.info("exiting loop", { classification: classification.type })
              break
            }
            if (classification.type === "failed") {
              yield* writeModelError({ assistant: lastAssistant, reason: classification.reason })
              yield* slog.info("exiting loop", { classification: classification.type, reason: classification.reason })
              break
            }
            if (classification.type === "text-tool-call") {
              if (yield* autoRetryTextToolCall({ lastUser, assistant: lastAssistant })) continue
              yield* slog.info("exiting loop", { classification: classification.type })
              break
            }
            if (classification.type === "think-only" || classification.type === "invalid") {
              const reason = classification.type === "invalid" ? classification.reason : "think-only"
              if (yield* autoContinueInvalidOutput({ lastUser, assistant: lastAssistant, reason })) continue
              yield* slog.info("exiting loop", { classification: classification.type })
              break
            }
            if (classification.type === "final" && classification.degraded)
              yield* slog.warn("degraded final on abnormal finish", { finish: lastAssistant.finish })
            if (classification.type !== "continue") {
              if (yield* goalGate(lastUser)) continue
              yield* slog.info("exiting loop", { classification: classification.type })
              break
            }
          }

          if ((agentID ?? "main") === "main") {
            const runningGoal = yield* goal.get(sessionID)
            if (
              runningGoal &&
              !["active", "verification_degraded", "awaiting_verification"].includes(runningGoal.status)
            ) {
              yield* slog.info("goal preflight stopped work", {
                sessionID,
                status: runningGoal.status,
              })
              break
            }
            if (runningGoal?.continuation) {
              const continuation = yield* goal.claimContinuation(sessionID, Goal.target(runningGoal), goalWorker)
              if (!continuation) {
                yield* slog.info("goal continuation already leased", { sessionID })
                break
              }
            }
          }

          const model = yield* getModel(
            modelOverride ? ProviderID.make(modelOverride.providerID) : lastUser.model.providerID,
            modelOverride ? ModelID.make(modelOverride.modelID) : lastUser.model.modelID,
            sessionID,
          )
          admittedConfig ??= Config.withManagedModelSettings(yield* config.get(), model)
          const gcfContext = resolvedAgentID === 'main'
            ? ManagedProvider.currentHostContext(sessionID)
            : actorExecution ? ManagedProvider.currentActorHostContext(sessionID,resolvedAgentID,actorExecution.executionRevision) : undefined
          const gcfArtifact = model.capabilities.input.text && gcfContext?.gcfArtifact?.availability() === "available" ? gcfContext.gcfArtifact : undefined
          const gcfArtifactIdentity = gcfArtifact?.identity()
          const admittedGcfView = gcfArtifactIdentity ? gcfViewIdentity(gcfContext?.gcfAdmission,
            turnExecution?.preflightRoute?.bootstrap.rootSessionID ?? sessionID, turnExecution?.rootOperationID,
            {providerID:model.providerID,modelID:model.id},gcfArtifactIdentity) : undefined
          const memoScope = admittedGcfView && turnExecution?.actorID === resolvedAgentID ? gcfMemoScope(gcfContext?.gcfAdmission,gcfContext?.ownerAccountID,sessionID,turnExecution?.rootOperationID,turnExecution) : undefined
          const gcfCurrent = () => {
            if (!admittedGcfView) return true
            const current = resolvedAgentID === 'main' ? ManagedProvider.currentHostContext(sessionID)
              : actorExecution ? ManagedProvider.currentActorHostContext(sessionID,resolvedAgentID,actorExecution.executionRevision) : undefined
            return current?.gcfArtifact === gcfArtifact && current?.gcfAdmission === gcfContext?.gcfAdmission && (!memoScope || current?.ownerAccountID === gcfContext?.ownerAccountID) &&
              gcfArtifact?.identity() === gcfArtifactIdentity
          }

          step++
          noteSourceStep(sourceAttempt,step)
          // Per-step turn heartbeat: only writer of turn_count; advances last_turn_time/time_updated so the orchestrator can tell progressing children from stalled ones. Safe 0-row no-op when no registry row exists.
          yield* actorRegistry.updateTurn(sessionID, resolvedAgentID).pipe(Effect.ignore)
          if (step === 1 && !session.parentID) {
            const cfg = admittedConfig
            const dreamTrigger = yield* shouldAutoDream(cfg, session.projectID).pipe(
              Effect.catch(() => Effect.succeed(false)),
            )
            const distillTrigger = yield* shouldAutoDistill(cfg, session.projectID).pipe(
              Effect.catch(() => Effect.succeed(false)),
            )
            const mdl = { providerID: lastUser.model.providerID, modelID: lastUser.model.modelID }
            // AppRuntime is imported dynamically (not at module top level) to keep
            // the session layer out of the app-runtime module-init cycle
            // (prompt → app-runtime → AppLayer → SessionPrompt). Only loaded when a
            // trigger actually fires. Detached fire-and-forget on the full runtime.
            const needAppRuntime = dreamTrigger || distillTrigger || Flag.MIMOCODE_EXPERIMENTAL_CRON
            if (needAppRuntime) {
              const { AppRuntime } = yield* Effect.promise(() => import("@/effect/app-runtime"))
              if (dreamTrigger) {
                AppRuntime.runPromise(
                  Session.Service.use((svc) =>
                    Effect.gen(function* () {
                      const s = yield* svc.create({ title: AUTO_DREAM_TITLE })
                      const sp = yield* Service
                      yield* sp.prompt({
                        sessionID: s.id,
                        agent: "dream",
                        model: mdl,
                        parts: [{ type: "text", text: DREAM_TASK }],
                      })
                    }),
                  ),
                ).catch((err) => log.error("auto-dream prompt failed", { error: String(err) }))
              }
              if (distillTrigger) {
                AppRuntime.runPromise(
                  Session.Service.use((svc) =>
                    Effect.gen(function* () {
                      const s = yield* svc.create({ title: AUTO_DISTILL_TITLE })
                      const sp = yield* Service
                      yield* sp.prompt({
                        sessionID: s.id,
                        agent: "distill",
                        model: mdl,
                        parts: [{ type: "text", text: DISTILL_TASK }],
                      })
                    }),
                  ),
                ).catch((err) => log.error("auto-distill prompt failed", { error: String(err) }))
              }
              // T18-bridge mount: fire CronBridge.start(sessionID, workspaceRoot)
              // once per new top-level session boot. The bridge itself no-ops when
              // MIMOCODE_EXPERIMENTAL_CRON is unset; the outer gate just skips the
              // resolve cost in the common case. Mirrors auto-dream's detached
              // dynamic-import pattern so prompt.ts stays out of the app-runtime
              // module-init cycle. Bridge.start is idempotent via its `started`
              // guard, and its Layer finalizer handles teardown on scope close.
              if (Flag.MIMOCODE_EXPERIMENTAL_CRON) {
                const workspaceRoot = (yield* InstanceState.context).worktree
                const { CronBridge } = yield* Effect.promise(() => import("@/session/cron-bridge"))
                AppRuntime.runPromise(CronBridge.use((b) => b.start(sessionID, workspaceRoot))).catch((err) =>
                  log.error("cron-bridge start failed", { sessionID, error: String(err) }),
                )
              }
            }
          }

          lastModelForPrune = model
          lastFinishedForPrune = lastFinished
          const task = tasks.pop()

          if (task?.type === "subtask") {
            yield* handleSubtask({ task, model, lastUser, sessionID, session, msgs })
            continue
          }

          // Detect compaction boundary: if the last user message has a compaction
          // part, route to compact.process() instead of the normal LLM flow.
          const lastUserMsgForCompaction = msgs.findLast((m) => m.info.role === "user")
          if (lastUserMsgForCompaction?.parts.some((p) => p.type === "compaction")) {
            const compactionPart = lastUserMsgForCompaction.parts.find(
              (p): p is MessageV2.CompactionPart => p.type === "compaction",
            )
            const allMsgs = yield* sessions.messages({ sessionID, agentID: lastUser.agentID ?? "main" })
            const result = yield* compaction.process({
              parentID: lastUser.id,
              messages: allMsgs,
              sessionID,
              auto: compactionPart?.auto ?? false,
              overflow: compactionPart?.overflow,
              agentID: lastUser.agentID,
              cfg: admittedConfig,
              ...(turnExecution ? { requestContext: { actorExecution:turnExecution } } : {}),
            })
            // cron-sentinel cache is invalidated via a SessionCompaction.Event
            // .Compacted bus subscription inside cron-bridge — see
            // `compaction.ts:468` publish + `cron-bridge.ts` subscribe pair.
            // Covers this user-`/compact` path plus the overflow-boundary
            // path in compaction.create.
            if (result === "stop") break
            continue
          }

          // Memory flush nudge at high context pressure.
          //
          // Purpose: at high context fill, the session may soon checkpoint and
          // discard old context, so remind the model to externalize durable
          // learnings to memory BEFORE that happens. This is a *save-your-work*
          // reminder, NOT a signal to wrap up.
          //
          // Two failure modes this guards against (both observed in prod):
          //   1. Wording that reads as "we're about to reset — wind down" made
          //      models prematurely end their turn and hand control back to the
          //      user mid-task. The text below is explicit: persist memory, then
          //      KEEP GOING; do not end the turn.
          //   2. Re-injecting the nudge on every user turn while pressure stays
          //      high turned a one-time heads-up into per-turn nagging. We now
          //      dedup across the recent conversation window, not just the
          //      current user message.
          if (lastFinished && lastFinished.summary !== true && model) {
            const cfg = admittedConfig ?? (yield* config.get())
            const pressure = pressureLevel({ cfg, tokens: lastFinished.tokens, model })
            if (pressure >= 2) {
              // De-bounce: nudge at most once per high-pressure episode (the
              // window since the last checkpoint boundary). See
              // nudgedSinceBoundary for why the boundary — not a fixed message
              // count — is the right anchor.
              const NUDGE_MARKER = "Context is filling up"
              const boundaryID = yield* checkpoint
                .lastBoundary(sessionID)
                .pipe(Effect.catch(() => Effect.succeed(undefined)))
              const alreadyNudged = nudgedSinceBoundary(msgs, boundaryID, NUDGE_MARKER)
              const lastUserMsg = msgs.findLast((m) => m.info.role === "user")
              if (lastUserMsg && !alreadyNudged) {
                lastUserMsg.parts.push({
                  id: PartID.ascending(),
                  messageID: lastUserMsg.info.id,
                  sessionID,
                  type: "text",
                  synthetic: true,
                  text: [
                    "<system-reminder>",
                    `Context is filling up (${pressure >= 3 ? ">85%" : ">70%"}).`,
                    "If you have important learnings or decisions from this session that are",
                    "not yet in memory, write them now (they may be summarized on the next",
                    "checkpoint). This is a save-your-work reminder only.",
                    "IMPORTANT: After writing to memory, CONTINUE with the current task in the",
                    "same turn. Do NOT stop, wrap up, or hand control back to the user because",
                    "of this reminder — only finish when the actual work is done.",
                    "</system-reminder>",
                  ].join("\n"),
                })
              }
            }
          }

          // Repeated-step nudge: if the last REPEATED_STEP_THRESHOLD finished
          // assistant steps made an identical tool call, the model is likely
          // stuck looping. Inject a reminder on the last user message asking it
          // to change approach. Mirrors the memory-flush nudge above (synthetic
          // text part, deduped per build).
          if (lastFinished) {
            const recentSignatures: string[] = []
            for (let i = msgs.length - 1; i >= 0 && recentSignatures.length < REPEATED_STEP_THRESHOLD; i--) {
              const m = msgs[i]
              if (m.info.role !== "assistant" || !m.info.finish) continue
              const sig = stepSignature(m.parts)
              if (sig === undefined) break
              recentSignatures.push(sig)
            }
            const repeating =
              recentSignatures.length === REPEATED_STEP_THRESHOLD &&
              recentSignatures.every((sig) => sig === recentSignatures[0])
            if (repeating) {
              const lastUserMsg = msgs.findLast((m) => m.info.role === "user")
              if (
                lastUserMsg &&
                !lastUserMsg.parts.some((p) => p.type === "text" && p.text?.includes("repeating the same action"))
              ) {
                lastUserMsg.parts.push({
                  id: PartID.ascending(),
                  messageID: lastUserMsg.info.id,
                  sessionID,
                  type: "text",
                  synthetic: true,
                  text: [
                    "<system-reminder>",
                    `Your last ${REPEATED_STEP_THRESHOLD} steps have been identical — you appear to be`,
                    "repeating the same action without making progress. Stop and reconsider:",
                    "the current approach is not working. Try a different strategy, use a",
                    "different tool, or if you are blocked, explain the blocker to the user",
                    "instead of repeating the same step again.",
                    "</system-reminder>",
                  ].join("\n"),
                })
              }
            }
          }

          // Resolve the agent for this iteration once. Both the management
          // hooks below (fireCheckpoints, overflow handler) and the existing
          // agent-not-found check later in the iteration reuse this binding.
          // Bounded computation agents (native + hidden — currently title,
          // summary, checkpoint-writer) are exempt from context management;
          // see docs/superpowers/specs/2026-04-28-bounded-computation-agents-design.md
          const agent = yield* agents.get(lastUser.agent)
          const isBoundedComputation = agent?.native === true && agent?.hidden === true

          // Fire background checkpoint writers for any newly-crossed thresholds
          // based on the latest completed assistant message's tokens. Must run
          // BEFORE the overflow/maxThreshold check below so maxCrossed flag is
          // set in time to trigger rebuild on this same iteration.
          if (!skipOverflowCheck && !isBoundedComputation && lastFinished && lastFinished.tokens) {
            const fireOps = yield* ops()
            yield* prune
              .fireCheckpoints({
                sessionID,
                model,
                tokens: lastFinished.tokens,
                promptOps: fireOps,
                agentID: lastUser.agentID,
                cfg: admittedConfig,
                toolViewContext: lastToolViewContext,
              })
              .pipe(Effect.ignore)
          }

          if (
            !skipOverflowCheck &&
            !isBoundedComputation &&
            lastFinished &&
            lastFinished.summary !== true &&
            (overflowCheck({ cfg: admittedConfig ?? (yield* config.get()), tokens: lastFinished.tokens, model }) ||
              (yield* prune.maxThresholdCrossed(sessionID)))
          ) {
            // Subagent overflow → per-actor compaction (lossy LLM summarization
            // scoped to the actor's (sessionID, agent_id) slice). Subagents
            // don't have checkpoints, so checkpoint+discard does not apply.
            // Gate must exclude agentID="main" — F49+F50 made main carry
            // agentID="main", so a bare `if (lastUser.agentID)` would route
            // main to this subagent path and skip the checkpoint rebuild
            // below. See checkpoint.ts:715 for the matching gate.
            if (lastUser.agentID && lastUser.agentID !== "main") {
              yield* compaction
                .create({
                  sessionID,
                  agent: lastUser.agent,
                  model: { providerID: model.providerID, modelID: model.id },
                  auto: true,
                  agentID: lastUser.agentID,
                })
                .pipe(Effect.ignore)
              // After inserting the boundary, the actor's filterCompactedEffect
              // slice begins at the boundary marker — context is freed for the
              // next iteration's stream. Skip the next overflow check so the
              // model can respond on the trimmed context.
              skipOverflowCheck = true
              continue
            }

            // Main-agent overflow: insert a checkpoint boundary marker (never
            // deletes DB messages) so the next iteration rebuilds from the
            // freshest checkpoint. Shared with the manual `/rebuild` command via
            // rebuildFromCheckpoint so logic/boundary conditions can't drift.
            // Falls back to compaction only when no boundary can be produced.
            const inserted = yield* rebuildFromCheckpoint({
              sessionID,
              msgs,
              agentID: lastUser.agentID,
              agent: lastUser.agent,
              model: { providerID: model.providerID, id: model.id },
            })
            if (inserted) {
              skipOverflowCheck = true
              continue
            }

            // F39: no checkpoint — fall back to compaction (LLM-driven lossy summary).
            // Better than mechanical trim: preserves semantic content via summary.
            yield* compaction
              .create({
                sessionID,
                agent: lastUser.agent,
                model: { providerID: model.providerID, modelID: model.id },
                auto: true,
                agentID: lastUser.agentID,
              })
              .pipe(Effect.ignore)
            skipOverflowCheck = true
            continue
          }
          skipOverflowCheck = false

          // `agent` resolved at iteration start; reuse here for the
          // agent-not-found user-visible error.
          if (!agent) {
            const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
            const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
            const error = new NamedError.Unknown({ message: `Agent not found: "${lastUser.agent}".${hint}` })
            yield* bus.publish(Session.Event.Error, { sessionID, error: error.toObject() })
            throw error
          }
          const maxSteps = agent.steps ?? Infinity
          const isLastStep = step >= maxSteps
          msgs = yield* insertReminders({ messages: msgs, agent, model, session, sourceAttempt })

          const actualMainOptions = turnExecution?.actorID === "main" ? turnExecution.preflightRoute?.modelRequestOptions : undefined
          if (actualMainOptions && (actualMainOptions.model.providerID !== model.providerID ||
            actualMainOptions.model.modelID !== model.id ||
            (actualMainOptions.options.reasoning_effort ?? null) !== (lastUser.model.variant ?? null) ||
            (lastUser.model.variant !== undefined && !Object.hasOwn(model.variants ?? {}, lastUser.model.variant))))
            return yield* Effect.die(new Error("managed main execution model options changed before dispatch"))

          const msg: MessageV2.Assistant = {
            id: MessageID.ascending(),
            parentID: lastUser.id,
            role: "assistant",
            agentID: lastUser.agentID,
            mode: agent.name,
            agent: agent.name,
            variant: lastUser.model.variant,
            path: { cwd: ctx.directory, root: ctx.worktree },
            cost: 0,
            tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
            modelID: model.id,
            providerID: model.providerID,
            time: { created: Date.now() },
            sessionID,
            ...(ManagedProvider.enabled() && turnExecution ? { metadata: { managedExecution: {
              sessionID, actorID: turnExecution.actorID, executionRevision: turnExecution.executionRevision,
              rootOperationID: turnExecution.rootOperationID,
              innerTurnID: turnExecution.innerTurnID,
            } } } : {}),
          }
          yield* sessions.updateMessage(msg)
          const handle = yield* processor.create({
            assistantMessage: msg,
            sessionID,
            model,
            cfg: admittedConfig,
            agentMetrics,
            actorExecution: turnExecution,
          })

          const outcome: "break" | "continue" = yield* Effect.gen(function* () {
            const lastUserMsg = msgs.findLast((m) => m.info.role === "user")
            const bypassAgentCheck = lastUserMsg?.parts.some((p) => p.type === "agent") ?? false

            const registryBefore = registry.provenanceGeneration ? yield* registry.provenanceGeneration() : undefined
            const pluginBefore = plugin.provenanceGeneration ? yield* plugin.provenanceGeneration() : undefined
            const resolvedTools = yield* resolveTools({
              agent,
              session,
              model,
              tools: lastUser.tools,
              processor: handle,
              bypassAgentCheck,
              messages: msgs,
              agentID: lastUser.agentID,
              task_id,
              mcpContext,
              recallAttestations,
              invalidRecallCalls,
              gcfViewIdentity: admittedGcfView,
              actorExecution: turnExecution,
              ...(receiptPolicy ? { permissionRulesets: [receiptPolicy] } : {}),
            })
            const mcpBefore = resolvedTools.mcpPresent && managedTurn && mcp.prefixGeneration ? yield* mcp.prefixGeneration(managedTurn) : undefined
            const tools = resolvedTools.tools
            const deniedByCurrentAuthority = resolvedTools.permissionRulesets?.length
              ? Permission.disabledIntersection(Object.keys(tools), resolvedTools.permissionRulesets)
              : new Set<string>()
            const activeTools = resolvedTools.activeTools.filter((name) => !deniedByCurrentAuthority.has(name))

            // Optional explicit wrapper; the captured SDK target remains the sole execution owner.
            const argumentHost = turnExecution ? ManagedProvider.currentActorHostContext(sessionID,turnExecution.actorID,turnExecution.executionRevision) : undefined
            const argumentArtifact = argumentHost?.gcfArtifact, argumentAdmission=argumentHost?.gcfArgumentAdmission
            const argumentIdentity=argumentArtifact?.identity()
            const wrapperDisabled=resolvedTools.permissionRulesets?.length ? Permission.disabledIntersection([GCF_ARGUMENT_TOOL],resolvedTools.permissionRulesets).has(GCF_ARGUMENT_TOOL) : false
            if (turnExecution?.actorID==='main' && argumentAdmission && argumentArtifact?.decodeAvailability()==='available' && argumentIdentity && !wrapperDisabled && registryBefore && pluginBefore && resolvedTools.argumentTargets.size) {
              const argumentRun=yield* runner()
              const allowedTargets=LLM.selectFinalTools({tools,activeTools,agent,permission:session.permission,user:lastUser}).activeTools
              const targetDigests=[...resolvedTools.argumentTargets.entries()].filter(([name])=>allowedTargets.includes(name)).map(([name,target])=>`${name}:${createHash('sha256').update(target.witness.schemaJSON).digest('hex')}`)
              const argumentSchema=z.object({version:z.literal(1),policy:z.literal(GCF_ARGUMENT_POLICY),target:z.enum(['glob','grep']),schemaSHA256:z.string().regex(/^[a-f0-9]{64}$/),payload:z.string()}).strict()
              tools[GCF_ARGUMENT_TOOL]=tool({description:`Explicitly decode bounded canonical GCF arguments for an available glob or grep tool. Target schema digests: ${targetDigests.join(', ')}. Ordinary calls remain unchanged.`,inputSchema:jsonSchema(z.toJSONSchema(argumentSchema,{target:'draft-7'}) as JSONSchema7),
                execute:async (raw,options)=>{
                  const parsed=argumentSchema.safeParse(raw), callID=options.toolCallId
                  if (!parsed.success || !callID || options.abortSignal?.aborted || Buffer.byteLength(parsed.data.payload,'utf8')>51200) throw new Error('GCF argument wrapper unavailable')
                  const a=parsed.data, target=resolvedTools.argumentTargets.get(a.target)
                  if (!target || createHash('sha256').update(target.witness.schemaJSON).digest('hex')!==a.schemaSHA256) throw new Error('GCF argument target unavailable')
                  const binding=Object.freeze({target:target.item,execute:target.execute,witness:target.witness,artifact:argumentArtifact,identity:argumentIdentity,schemaSHA256:a.schemaSHA256})
                  const fastCurrent=()=>{
                    const host=ManagedProvider.currentActorHostContext(sessionID,turnExecution.actorID,turnExecution.executionRevision)
                    const delivery=ManagedProvider.currentOriginalPromptHostContext(sessionID,turnExecution.rootOperationID!)
                    const original=delivery === null ? (ManagedProvider.currentRootOperationID(sessionID)===turnExecution.rootOperationID ? ManagedProvider.currentHostContext(sessionID) : undefined) : delivery
                    if (original?.gcfArgumentAdmission!==argumentAdmission || original?.gcfArtifact!==argumentArtifact) return false
                    if (!host || host.gcfArgumentAdmission!==argumentAdmission || host.gcfArtifact!==argumentArtifact || argumentArtifact.identity()!==argumentIdentity || argumentArtifact.decodeAvailability()!=='available' || options.abortSignal?.aborted || !isAdmittedToolTimingTurn(turnExecution,{sessionID,actorID:turnExecution.actorID,executionRevision:turnExecution.executionRevision,rootOperationID:turnExecution.rootOperationID!,innerTurnID:turnExecution.innerTurnID!}) || tools[a.target]?.execute!==target.execute) return false
                    const selected=LLM.selectFinalTools({tools,activeTools,agent,permission:session.permission,user:lastUser}).activeTools
                    return selected.includes(GCF_ARGUMENT_TOOL) && selected.includes(a.target)
                  }
                  const current=async()=>{
                    if (!fastCurrent()) return false
                    try {
                      const valid=await argumentRun.promise(target.witness.current()) && await argumentRun.promise(registry.provenanceGeneration!())===registryBefore && JSON.stringify(await argumentRun.promise(plugin.provenanceGeneration!()))===JSON.stringify(pluginBefore)
                      return valid && fastCurrent()
                    } catch {return false}
                  }
                  if (!await current() || !await argumentRun.promise(handle.beginArgumentResolution(callID,raw,a.target,a.schemaSHA256,binding))) throw new Error('GCF argument call unavailable')
                  const decoded=argumentArtifact.decode(Buffer.from(a.payload,'utf8'),GCF_ARGUMENT_POLICY)
                  if (decoded.state!=='decoded') throw new Error('GCF argument decoding unavailable')
                  let args:unknown
                  try {args=JSON.parse(decoded.output.toString('utf8')); args=target.item.parameters.parse(args)} catch {throw new Error('GCF argument validation failed')}
                  const identity={sessionID,actorID:turnExecution.actorID,executionRevision:turnExecution.executionRevision,rootOperationID:turnExecution.rootOperationID!,innerTurnID:turnExecution.innerTurnID!}
                  await ManagedProvider.admitManagedTool(identity,GCF_ARGUMENT_TOOL,'tool',callID)
                  await ManagedProvider.admitManagedTool(identity,a.target,'tool',callID)
                  if (!await current() || !await argumentRun.promise(handle.checkArgumentResolution(callID,raw,binding,true)) || !await current() || !await argumentRun.promise(handle.checkArgumentResolution(callID,raw,binding,false)) || !fastCurrent()) throw new Error('GCF argument scope changed')
                  resolvedTools.argumentCalls.add(callID)
                  try {return await target.execute(args,options)} finally {resolvedTools.argumentCalls.delete(callID)}
                }})
              activeTools.push(GCF_ARGUMENT_TOOL)
            }

            const structuredDenied = resolvedTools.permissionRulesets?.length
              ? Permission.disabledIntersection(["StructuredOutput"], resolvedTools.permissionRulesets).has("StructuredOutput")
              : Permission.disabled(["StructuredOutput"], Agent.runtimePermission(agent, session.permission)).has("StructuredOutput")
            if (lastUser.format?.type === "json_schema" && !structuredDenied) {
              tools["StructuredOutput"] = createStructuredOutputTool({
                schema: lastUser.format.schema,
                onSuccess(output) {
                  structured = output
                },
              })
              activeTools.push("StructuredOutput")
            }

            if (step === 1)
              yield* summary.summarize({ sessionID, messageID: lastUser.id }).pipe(Effect.ignore, Effect.forkIn(scope))

            if (step > 1 && lastFinished) {
              for (const m of msgs) {
                if (m.info.role !== "user" || m.info.id <= lastFinished.id) continue
                for (const p of m.parts) {
                  if (p.type !== "text" || p.ignored || p.synthetic) continue
                  if (!p.text.trim()) continue
                  p.text = [
                    "<system-reminder>",
                    "The user sent the following message:",
                    p.text,
                    "",
                    "Please address this message and continue with your tasks.",
                    "</system-reminder>",
                  ].join("\n")
                }
              }
            }

            const nativeMembership = step === 1 && ManagedProvider.enabled() && turnExecution?.actorID === 'main' && turnExecution.rootOperationID && turnExecution.innerTurnID ? captureNativeMembership({sessionID,rootOperationID:turnExecution.rootOperationID,actorID:'main',executionRevision:turnExecution.executionRevision,innerTurnID:turnExecution.innerTurnID},msgs) : undefined
            const nativeTextBeforePlugin = snapshotNativeText(msgs)
            yield* plugin.trigger("experimental.chat.messages.transform", {}, { messages: msgs })
            const nativeTextAfterPlugin = reconcileNativeText(nativeTextBeforePlugin, msgs)

            const format = lastUser.format ?? { type: "text" as const }
            lastToolViewContext = currentToolViewContext(turnExecution,
              resolvedTools.historyWhitelisted && activeTools.includes('history') && !isLastStep && format.type !== 'json_schema')

            // Determine if this iteration is for a fork agent (contextMode === "full").
            // Fork agents use the frozen ForkContext snapshot captured at spawn time
            // (system + inheritedMessages) rather than recomputing from their own
            // agent identity — which would diverge from the parent and break the
            // prefix cache.
            const actorRecord = lastUser.agentID
              ? yield* actorRegistry.get(sessionID, lastUser.agentID).pipe(Effect.orElseSucceed(() => undefined))
              : undefined
            // v9 registers main as `mode: "main"` with `contextMode: "full"`.
            // Only spawned actors (subagent/peer) carry a frozen ForkContext;
            // main is the captor, never the captured.
            const isForkAgent =
              actorRecord?.contextMode === "full" && (actorRecord.mode === "subagent" || actorRecord.mode === "peer")

            // Fork path: read frozen ForkContext from Actor service (late-bound via
            // spawnRef to break the Actor → SessionPrompt → Actor layer cycle).
            // If forkCtx is missing (race / cleanup bug / spawn skipped), fail the
            // actor so the next prune turn can spawn a fresh fork.
            if (isForkAgent) {
              const forkCtxEffect = spawnRef.current?.getForkContext(sessionID, lastUser.agentID!)
              const forkProjection = forkCtxEffect ? yield* forkCtxEffect : undefined
              const forkCtx = forkProjection?.context
              if (!forkCtx || !forkProjection) {
                yield* slog.warn("fork agent runLoop: missing forkContext, failing actor", {
                  sessionID,
                  agentID: lastUser.agentID,
                })
                // The enclosing Actor.runTurn/forkWork owns terminal lifecycle
                // settlement. Failing here lets its revision-aware CAS record
                // the outcome; a direct updateStatus would race cancellation
                // and could overwrite a newer execution.
                return yield* Effect.fail(new Error("missing fork context"))
              }
              const ownNew = msgs.filter(
                (m) => m.info.id > forkCtx.watermarkMsgID && m.info.agentID === lastUser.agentID,
              )
              const ownViews = yield* prepareViews(lastToolViewContext, ownNew)
              if (ownViews.reason) yield* slog.warn('fork output uses a raw reanchor', {reason:ownViews.reason})
              const ownNewModelMsgs = yield* MessageV2.toModelMessagesEffect(ownNew, model, {gcfViewIdentity:admittedGcfView,gcfArtifact,gcfMemoScope:memoScope,toolViews:ownViews.views})
              const prebuiltSystem = forkCtx.system
              lastSystemPrompt = prebuiltSystem
              const modelMsgs: ModelMessage[] = [...forkCtx.inheritedMessages, ...ownNewModelMsgs]
              // additions is empty for fork agents: system is taken verbatim from
              // forkCtx.system. Passed as `system` to handle.process for logging/replay.
              const additions: string[] = []
              // Note: fork uses `tools` from resolveTools (not `forkCtx.tools`) — runtime
              // tool dispatch needs execute closures, which `forkCtx.tools` does not carry.
              // Schema parity with parent is currently a consequence of checkpoint-writer
              // having no toolAllowlist (Task 2.6 + agent.test.ts guard). See ForkContext.tools
              // JSDoc in packages/opencode/src/actor/spawn.ts for the full contract.
              const queryParts = msgs.findLast((m) => m.info.role === "user" && m.info.id === lastUser.id)?.parts ?? []
              const query = userQueryText(queryParts)
              const preQuery = {
                cancel: undefined as boolean | undefined,
                cancelReason: undefined as string | undefined,
              }
              yield* plugin.trigger(
                "session.userQuery.pre",
                { sessionID, agentID: resolvedAgentID, step, messageID: lastUser.id, query },
                preQuery,
              )
              if (preQuery.cancel) {
                cancelled = true
                cancelReason = preQuery.cancelReason
                handle.message.error = new MessageV2.AbortedError({
                  message: preQuery.cancelReason ?? "Step cancelled by plugin",
                }).toObject()
                handle.message.finish = "cancelled"
                yield* sessions.updateMessage(handle.message)
                yield* plugin.trigger(
                  "session.userQuery.post",
                  {
                    sessionID,
                    agentID: resolvedAgentID,
                    step,
                    messageID: lastUser.id,
                    query,
                    assistantMessageID: handle.message.id,
                    finish: handle.message.finish,
                    error: preQuery.cancelReason,
                    trajectory: trajectoryForStep(msgs, handle.message),
                    systemPrompt: lastSystemPrompt,
                  },
                  {},
                )
                return "break" as const
              }
              if ((ownViews.views.size && (!lastToolViewContext || !toolViewContextCurrent(lastToolViewContext))) || !gcfCurrent()) {
                if (lastToolViewContext) {lastToolViewContext.control.disabled=true;lastToolViewContext.control.reason ??= 'scope_unavailable'}
                yield* slog.warn('fork output uses a raw reanchor', {reason:'scope_unavailable'})
                const raw = yield* MessageV2.toModelMessagesEffect(ownNew,model,{})
                modelMsgs.splice(forkCtx.inheritedMessages.length,modelMsgs.length, ...raw)
              }
              const result = yield* handle
                .process({
                  user: lastUser,
                  agent,
                  // Visibility is already filtered by each current source and
                  // destination ruleset independently. Keep the destination's
                  // ordinary current rules here as a final fail-closed filter;
                  // captured permissions are evidence, never current authority.
                  permission: Agent.runtimePermission(agent, session.permission),
                  sessionID,
                  parentSessionID: session.parentID,
                  system: additions,
                  prebuiltSystem,
                  contextSources: sourceEvidence(ownNew, { kind: "fork_snapshot", id: forkProjection.encoded.sourceWatermarkMessageID,
                    digest: forkProjection.encoded.snapshotSHA256 }),
                  ...(turnExecution ? { requestContext: { actorExecution: turnExecution } } : {}),
                  messages: [...modelMsgs, ...(isLastStep ? [{ role: "user" as const, content: MAX_STEPS }] : [])],
                  tools,
                  activeTools,
                  model,
                  toolChoice: isLastStep ? "none" : format.type === "json_schema" ? "required" : undefined,
                  agentID: lastUser.agentID,
                })
                .pipe(
                  Effect.onExit((exit) =>
                    plugin
                      .trigger(
                        "session.userQuery.post",
                        {
                          sessionID,
                          agentID: resolvedAgentID,
                          step,
                          messageID: lastUser.id,
                          query,
                          assistantMessageID: handle.message.id,
                          finish: handle.message.finish,
                          error: Exit.isFailure(exit)
                            ? Cause.pretty(exit.cause)
                            : sessionErrorText(handle.message.error),
                          finalText: assistantFinalText(handle.message, MessageV2.parts(handle.message.id)),
                          trajectory: trajectoryForStep(msgs, handle.message),
                          systemPrompt: lastSystemPrompt,
                        },
                        {},
                      )
                      .pipe(Effect.ignore),
                  ),
                )

              if (result === "continue" && (yield* autoContinueOutputLength({ lastUser, assistant: handle.message }))) {
                return "continue" as const
              }

              if (result === "text-repeat") {
                if (yield* handleTextRepeat({ lastUser })) return "continue" as const
                return "break" as const
              }
              if (result === "stop") return "break" as const

              if (structured !== undefined) {
                handle.message.structured = structured
                handle.message.finish = handle.message.finish ?? "stop"
                yield* sessions.updateMessage(handle.message)
                return "break" as const
              }

              // Empty/no-op tool-call loop guard (fork branch). Intercept before
              // classify would `continue` an empty tool-calls step: soft-nudge
              // within budget, hard-halt once exceeded. A non-empty step returns
              // "none" and falls through to normal classification.
              const forkEmptyStep = yield* handleEmptyStep({ lastUser, assistant: handle.message })
              if (forkEmptyStep === "halt") return "break" as const
              if (forkEmptyStep === "continue") return "continue" as const

              const forkClassification = classifyAssistantStep({
                phase: "after-process",
                lastUser,
                assistant: handle.message,
                parts: MessageV2.parts(handle.message.id),
                processResult: result,
              })
              if (forkClassification.type === "filtered") {
                yield* writeContentFilterError({ assistant: handle.message })
                return "break" as const
              }
              if (forkClassification.type === "failed") {
                yield* writeModelError({ assistant: handle.message, reason: forkClassification.reason })
                return "break" as const
              }
              if (forkClassification.type === "text-tool-call") {
                if (yield* autoRetryTextToolCall({ lastUser, assistant: handle.message })) return "continue" as const
                return "break" as const
              }
              if (forkClassification.type !== "continue" && !handle.message.error && format.type === "json_schema") {
                if (yield* autoRetryStructuredOutput({ lastUser, assistant: handle.message }))
                  return "continue" as const
                return "break" as const
              }

              if (
                (forkClassification.type === "think-only" || forkClassification.type === "invalid") &&
                format.type !== "json_schema"
              ) {
                const reason = forkClassification.type === "invalid" ? forkClassification.reason : "think-only"
                if (yield* autoContinueInvalidOutput({ lastUser, assistant: handle.message, reason }))
                  return "continue" as const
                return "break" as const
              }

              if (forkClassification.type === "final" && forkClassification.degraded)
                yield* slog.warn("degraded final on abnormal finish", { finish: handle.message.finish })
              // Fork agents are always subagents (lastUser.agentID is set); use
              // per-actor compaction on overflow (same as non-fork subagent path).
              if (!isBoundedComputation && result === "overflow") {
                yield* compaction
                  .create({
                    sessionID,
                    agent: lastUser.agent,
                    model: { providerID: model.providerID, modelID: model.id },
                    auto: true,
                    overflow: true,
                    agentID: lastUser.agentID,
                  })
                  .pipe(Effect.ignore)
              }
              return "continue" as const
            }

            let environmentMethodOffered=false
            const [skillResult, environmentResult, instructions] = yield* Effect.all([
              turnExecution && sys.skillsWithProvenance
                ? sys.skillsWithProvenance(agent,model,session.permission)
                : sys.skills(agent,model,session.permission).pipe(Effect.map(value => ({value}))),
              turnExecution && sys.environmentWithProvenance
                ? (environmentMethodOffered=true, sys.environmentWithProvenance(model, session.time.created, turnExecution))
                : sys.environment(model, session.time.created).pipe(Effect.map((strings):{strings:string[];handle?:import("./closed-source-provenance").EnvironmentProvenanceHandle} => ({strings}))),
              (turnExecution && instruction.systemWithProvenance ? instruction.systemWithProvenance() : instruction.system()).pipe(Effect.orDie),
            ])
            const env = environmentResult.strings
            try { log.debug("source_environment_offered",{turn:!!turnExecution,method:environmentMethodOffered,handle:"handle" in environmentResult && !!environmentResult.handle}) } catch { /* diagnostic only */ }
            const skills = skillResult.value
            // Surface which instruction files (CLAUDE.md, AGENTS.md, ...) were loaded.
            // Only for primary sessions (subagents would be noisy) and once per session.
            if (!session.parentID && !instructionsNotified.has(sessionID)) {
              instructionsNotified.add(sessionID)
              const worktree = (yield* InstanceState.context).worktree
              const files = Array.from(instructions.paths, (p) => Instruction.display(p, worktree))
              if (files.length > 0) {
                yield* bus.publish(TuiEvent.InstructionsLoaded, { files }).pipe(Effect.ignore)
              }
            }
            const additions = [
              ...env,
              ...(skills ? [skills] : []),
              ...instructions.content,
              ...(format.type === "json_schema" ? [STRUCTURED_OUTPUT_SYSTEM_PROMPT] : []),
            ]
            // Note: `buildLLMRequestPrefix` also returns a `tools` field, but we
            // intentionally don't use it here — the `tools` variable from `resolveTools`
            // (set earlier via `handle.process({tools: ...})`) carries `execute` closures
            // the AI SDK needs for runtime tool dispatch, while `buildLLMRequestPrefix`
            // produces schema-only tools for capture. The live advertisement below
            // is frozen from the executable tools actually selected for this turn.
            // Main runLoop: no watermark — LLM must see the full msgs list,
            // including this turn's intermediate assistant turns (tool reads,
            // task creates, etc.) so each step doesn't replay from the bare
            // user prompt. The watermark is for fork capture only (frozen
            // snapshot of parent-view at spawn time).
            // Current session schema has no harness/systemMode; these defaults are stable.
            const preparedViews = yield* prepareViews(lastToolViewContext, msgs)
            if (preparedViews.reason) yield* slog.warn('tool view uses a raw reanchor', {reason:preparedViews.reason})
            const toolViewIdentity = preparedViews.views.size ? PrefixProvenance.digest(JSON.stringify([...preparedViews.views]))
              : preparedViews.reason ? `raw:${preparedViews.reason}` : undefined
            const requestGcfView = gcfCurrent() ? admittedGcfView : undefined
            const gcfWasReanchored = !!admittedGcfView && !requestGcfView
            const schemaViewIdentity = (()=>{
              const context=resolvedTools.schemaContext
              if(!context || !toolViewContextCurrent(context)) return
              try {const value=JSON.parse(context.encoded);return value?.profiles?.schemaAnnotations===true && /^[a-f0-9]{64}$/.test(value.settingsRevision) ? `schema-annotations-v1:${value.settingsRevision}` : undefined} catch {return}
            })()
            const profileKey = SessionPrefixSnapshot.profileKey({
              providerID: model.providerID,
              modelID: model.id,
              agent: agent.name,
              agentID: lastUser.agentID ?? "main",
              harness: "default",
              systemMode: "append",
              system: lastUser.system ?? "",
              format,
              permission: Agent.runtimePermission(agent, session.permission),
              ...(requestGcfView ? { gcfViewIdentity:requestGcfView } : {}),
              ...(toolViewIdentity ? {toolViewIdentity} : {}),
              ...(schemaViewIdentity ? {schemaViewIdentity} : {}),
            })
            const frozen = yield* SessionPrefixSnapshot.get(sessionID, profileKey)
            // Opaque, request-local evidence only; canonical messages remain untouched.
            const rowId=turnExecution
            const rowsAdmitted=rowId?.rootOperationID ? gcfSameRequestRowsAdmission(gcfContext?.gcfAdmission,sessionID,rowId.rootOperationID,{providerID:model.providerID,modelID:model.id}) : undefined
            const graphAdmitted=rowId?.rootOperationID ? gcfGraphReferencesAdmission(gcfContext?.gcfAdmission,sessionID,rowId.rootOperationID,{providerID:model.providerID,modelID:model.id}) : undefined
            const rowSelector=rowsAdmitted ?? graphAdmitted
            const rowHost=rowId ? ManagedProvider.currentActorHostContext(sessionID,rowId.actorID,rowId.executionRevision) : undefined
            const rowScope=rowId ? ManagedProvider.currentScope(sessionID,rowId.actorID,rowId.executionRevision) : undefined
            const rowIssuer=rowId?.rootOperationID ? ManagedProvider.currentActorToolTimingIssuer(sessionID,rowId.actorID,rowId.executionRevision,rowId.rootOperationID) : undefined
            const rowRoute=rowId ? ManagedProvider.currentToolViewRoute(sessionID,rowId.actorID,rowId.executionRevision) : undefined
            const rowCurrent=()=>{ try {
              if (!rowId || !rowId.rootOperationID || !rowId.innerTurnID || rowId.actorID!=='main' || !rowScope || !rowIssuer?.isAlive() || !rowHost?.ownerAccountID || !rowRoute || rowRoute.rootOperationID!==rowId.rootOperationID || rowRoute.modelID!==model.id || (rowRoute.providerID!==model.providerID && rowRoute.connectionID!==model.providerID) || !gcfArtifact || gcfArtifact.availability()!=='available' || !admittedGcfView || !rowSelector || !gcfCurrent()) return false
              const host=ManagedProvider.currentActorHostContext(sessionID,rowId.actorID,rowId.executionRevision), route=ManagedProvider.currentToolViewRoute(sessionID,rowId.actorID,rowId.executionRevision)
              return isAdmittedToolTimingTurn(rowId,{sessionID,actorID:'main',executionRevision:rowId.executionRevision,rootOperationID:rowId.rootOperationID,innerTurnID:rowId.innerTurnID}) &&
                ManagedProvider.currentScope(sessionID,rowId.actorID,rowId.executionRevision)===rowScope && host?.ownerAccountID===rowHost.ownerAccountID && host.chatID===rowHost.chatID &&
                host.gcfAdmission===gcfContext?.gcfAdmission && host.gcfArtifact===gcfArtifact && !!route && route.rootOperationID===rowRoute.rootOperationID &&
                route.connectionID===rowRoute.connectionID && route.providerID===rowRoute.providerID && route.modelID===rowRoute.modelID && route.modelRouteID===rowRoute.modelRouteID
              } catch {return false}
            }
            let rowFrames:ReturnType<typeof createGcfRequestFrames> | undefined
            if (rowId?.rootOperationID && rowId.innerTurnID && rowHost?.ownerAccountID && gcfArtifact && rowSelector && rowCurrent()) {
              const writer=Option.getOrUndefined(yield* Effect.serviceOption(HistoryWriterService))
              const services=yield* Effect.context()
              try { if (writer) rowFrames=createGcfRequestFrames({identity:{sessionID,rootOperationID:rowId.rootOperationID,actorID:rowId.actorID,executionRevision:rowId.executionRevision,innerTurnID:rowId.innerTurnID},accountID:rowHost.ownerAccountID,artifact:gcfArtifact,current:rowCurrent,
                sourceAck:(sid,actor,part,hash)=>Effect.runPromiseWith(services)(writer.sourceAck(sid,actor,part,hash)),
                checkSources:async sources=>{
                  if (!rowCurrent()) return false
                  let timer:ReturnType<typeof setTimeout> | undefined
                  try { const value=await Promise.race([ManagedProvider.historySourcesCurrent({sessionID,operation:'check_sources',rootOperationID:rowId.rootOperationID!,actorID:rowId.actorID,executionRevision:rowId.executionRevision,innerTurnID:rowId.innerTurnID!,sources,rowViewAdmission:rowSelector}),new Promise<false>(resolve=>{timer=setTimeout(()=>resolve(false),750)})]); return value===true && rowCurrent() }
                  catch {return false} finally {if(timer) clearTimeout(timer)}
                }}) } catch {rowFrames=undefined}
            }
            const built = yield* buildLLMRequestPrefix({
              sessionID,
              agent,
              model,
              msgs,
              additions,
              // The boundary already records the historical assistant tail as
              // bounded Recent activity. Keep only later live turns here.
              collapseCheckpointTail: true,
              gcfViewIdentity: requestGcfView,
              gcfArtifact,
              gcfMemoScope: requestGcfView ? memoScope : undefined,
              ...(rowFrames ? {gcfFrameRecord:rowFrames.record} : {}),
              toolViews: preparedViews.views,
              nativeMembership,
              ...("handle" in environmentResult && environmentResult.handle ? {sourceWitness:{environment:environmentResult.handle,environmentStrings:env,skills:skillResult,instructions,
                ...(factoryPersonaStart && turnExecution ? {persona:{result:factoryPersonaStart,identity:turnExecution}} : {})}} : {}),
            }).pipe(Effect.provideService(LLM.Service, llm), Effect.provideService(ToolRegistry.Service, registry))
            rowFrames?.seal()
            let lateRawReanchor = false
            const annotationContext=resolvedTools.schemaContext
            const annotationSources=resolvedTools.schemaSources
            const annotationNames=annotationContext && annotationSources && toolViewContextCurrent(annotationContext)
              ? LLM.selectFinalTools({tools,activeTools,agent,permission:session.permission,user:lastUser}).activeTools : undefined
            // Native source snapshots only; unknown/MCP/structured branches remain canonical.
            const annotationReady=!!annotationNames?.length && annotationNames.every(name=>annotationSources?.has(name))
            const resolvedPrefixTools = yield* Effect.promise(() => SessionPrefixSnapshot.resolveTools(tools, annotationNames ?? activeTools,
              annotationReady ? {eligible:name=>name!=='memory' && name!=='question' && name!=='StructuredOutput'} : undefined))
            let currentTools = resolvedPrefixTools.snapshots
            let toolsHash = SessionPrefixSnapshot.toolsHash(currentTools)
            Object.assign(tools, resolvedPrefixTools.tools)
            const route = turnExecution?.preflightRoute
            const providerScope = turnExecution ? ManagedProvider.currentScope(sessionID, turnExecution.actorID, turnExecution.executionRevision) : undefined
            const providerBinding = turnExecution ? ManagedProvider.currentBindingEvidence(sessionID, turnExecution.actorID, turnExecution.executionRevision) : undefined
            const routeEvidence: PrefixProvenance.RouteEvidence | null = route && providerScope && providerBinding && providerScope.connectionID === route.connectionID && providerScope.billingLane === route.billingLane
              && route.modelID === model.id && (route.providerID === model.providerID || route.connectionID === model.providerID)
              && providerBinding.connectionID === route.connectionID && providerBinding.providerID === route.providerID && providerBinding.modelID === route.modelID
              && providerBinding.accountID === (providerScope.accountID ?? null) && providerBinding.credentialRevision === (providerScope.credentialRevision ?? null)
              ? {providerID:route.providerID, modelID:route.modelID, connectionID:route.connectionID, billingLane:route.billingLane,
                modelRouteID:route.modelRouteID, accountID:providerScope.accountID ?? null, credentialRevision:providerScope.credentialRevision ?? null,
                grantID:route.grantID ?? null, grantRevision:route.grantRevision ?? null, methodID:route.methodPolicy?.methodID ?? null,
                methodPolicyRevision:route.methodPolicy?.policyRevision ?? null, catalogRevision:route.modelRequestOptions?.catalogRevision ?? null,
                mappingRevision:route.bootstrap.mappingRevision ?? null, workspaceRevision:route.bootstrap.workspaceRevision ?? null,
                bindingID:providerBinding.bindingID, bindingRevision:providerBinding.bindingRevision, committed:providerBinding.committed,
                methodApplicability:route.applicability?.version === 1 ? route.applicability.methodPolicy : "unknown",
                grantApplicability:route.applicability?.version === 1 ? route.applicability.grant : "unknown"} : null
            const registryAfter = registry.provenanceGeneration ? yield* registry.provenanceGeneration() : undefined
            const pluginAfter = plugin.provenanceGeneration ? yield* plugin.provenanceGeneration() : undefined
            const mcpAfter = resolvedTools.mcpPresent && managedTurn && mcp.prefixGeneration ? yield* mcp.prefixGeneration(managedTurn) : undefined
            let sourceToolsCurrent:(()=>Promise<boolean>) | undefined
            if(sourceAttempt && registryBefore && registryBefore===registryAfter && pluginBefore && JSON.stringify(pluginBefore)===JSON.stringify(pluginAfter) &&
                (!resolvedTools.mcpPresent || (managedTurn && mcpBefore && JSON.stringify(mcpBefore)===JSON.stringify(mcpAfter)))) {
              const services=yield* Effect.context(), permission=JSON.stringify(Agent.runtimePermission(agent,session.permission))
              const names=LLM.selectFinalTools({tools,activeTools,agent,permission:session.permission,user:lastUser}).activeTools
              sourceToolsCurrent=async()=>{try {
                if(!registry.provenanceGeneration || !plugin.provenanceGeneration || !sourceAttemptLayout(sourceAttempt))return false
                const freshRegistry=await Effect.runPromiseWith(services)(registry.provenanceGeneration())
                const freshPlugin=await Effect.runPromiseWith(services)(plugin.provenanceGeneration())
                const freshAgent=await Effect.runPromiseWith(services)(agents.get(agent.name))
                const freshSession=await Effect.runPromiseWith(services)(sessions.get(sessionID))
                if(!freshAgent || freshRegistry!==registryBefore || JSON.stringify(freshPlugin)!==JSON.stringify(pluginBefore) ||
                    JSON.stringify(Agent.runtimePermission(freshAgent,freshSession.permission))!==permission ||
                    JSON.stringify(LLM.selectFinalTools({tools,activeTools,agent:freshAgent,permission:freshSession.permission,user:lastUser}).activeTools)!==JSON.stringify(names))return false
                if(resolvedTools.mcpPresent) {
                  if(!managedTurn || !mcp.prefixGeneration || JSON.stringify(await Effect.runPromiseWith(services)(mcp.prefixGeneration(managedTurn)))!==JSON.stringify(mcpBefore))return false
                }
                return !!sourceAttemptLayout(sourceAttempt)
              } catch {return false}}
            }
            const gcfBuildCurrent = !requestGcfView || gcfCurrent()
            let annotationView:LLM.StreamInput['schemaAnnotationView'] | undefined
            let annotationTransform:string = ProviderTransform.schemaTransformProfile
            if (annotationContext && annotationSources && annotationNames && resolvedPrefixTools.canonicalSnapshots && resolvedPrefixTools.canonicalTools) {
              try {
                const sources=annotationNames.map(name=>{const source=annotationSources.get(name);if(!source)throw new Error('source missing');return {name,pre:source.pre,post:source.post}})
                const canonical=JSON.stringify({version:1,sources,snapshots:resolvedPrefixTools.canonicalSnapshots})
                if (Buffer.byteLength(canonical,'utf8')>524288 || !registryBefore || registryBefore!==registryAfter || !pluginBefore || JSON.stringify(pluginBefore)!==JSON.stringify(pluginAfter) || (resolvedTools.mcpPresent && (!mcpBefore || JSON.stringify(mcpBefore)!==JSON.stringify(mcpAfter))) || !toolViewContextCurrent(annotationContext)) throw new Error('source unavailable')
                annotationTransform=`provider-schema-transform-v1+schema-annotations-v1:${PrefixProvenance.digest(canonical)}`
                const services=yield* Effect.context()
                const permission=JSON.stringify(Agent.runtimePermission(agent,session.permission))
                const schemaNames=[...annotationNames]
                const check=async()=>{
                  try {
                    if(!toolViewContextCurrent(annotationContext) || !registry.provenanceGeneration || !plugin.provenanceGeneration)return false
                    const freshRegistry=await Effect.runPromiseWith(services)(registry.provenanceGeneration())
                    const freshPlugin=await Effect.runPromiseWith(services)(plugin.provenanceGeneration())
                    const freshAgent=await Effect.runPromiseWith(services)(agents.get(agent.name))
                    const freshSession=await Effect.runPromiseWith(services)(sessions.get(sessionID))
                    if(!freshAgent || freshRegistry!==registryBefore || JSON.stringify(freshPlugin)!==JSON.stringify(pluginBefore) ||
                        JSON.stringify(Agent.runtimePermission(freshAgent,freshSession.permission))!==permission || !toolViewContextCurrent(annotationContext))return false
                    if(resolvedTools.mcpPresent) {
                      if(!managedTurn || !mcp.prefixGeneration)return false
                      const freshMcp=await Effect.runPromiseWith(services)(mcp.prefixGeneration(managedTurn))
                      if(JSON.stringify(freshMcp)!==JSON.stringify(mcpBefore))return false
                    }
                    for(const name of schemaNames) {
                      const source=annotationSources.get(name)
                      if(!source || source.item.parameters!==source.parameters || source.item.execute!==source.execute || source.item.description!==source.description)return false
                      const original=z.toJSONSchema(source.item.parameters)
                      if(JSON.stringify(original)!==source.pre || JSON.stringify(ProviderTransform.schema(model,original))!==source.post)return false
                    }
                    return toolViewContextCurrent(annotationContext)
                  } catch {return false}
                }
                annotationView={names:schemaNames,canonicalTools:resolvedPrefixTools.canonicalTools,
                  projectedSchemas:Object.fromEntries(schemaNames.map(name=>[name,resolvedPrefixTools.tools[name]!.inputSchema])),
                  current:check,reanchor:()=>{lateRawReanchor=true}}
              } catch {
                Object.assign(tools,resolvedPrefixTools.canonicalTools)
                currentTools=resolvedPrefixTools.canonicalSnapshots
                toolsHash=SessionPrefixSnapshot.toolsHash(currentTools)
                lateRawReanchor=true
              }
            }
            const provenance = PrefixProvenance.build({ system: built.system, toolsHash, instructions: instructions.manifest ?? [],
              agent:{name:agent.name,id:lastUser.agentID ?? "main"},
              policy: JSON.stringify({agent:Agent.runtimePermission(agent, session.permission), receipts:resolvedTools.permissionRulesets ?? null}), route:routeEvidence,
              catalog:skills === undefined ? null : skills,
              registryGeneration:registryBefore && registryBefore === registryAfter ? registryBefore : null,
              pluginGeneration:pluginBefore && JSON.stringify(pluginBefore) === JSON.stringify(pluginAfter) ? pluginBefore : null,
              mcpPresent:resolvedTools.mcpPresent, mcpClients:mcpBefore && JSON.stringify(mcpBefore) === JSON.stringify(mcpAfter) ? mcpBefore : null,
              transformProfile:annotationTransform,
              systemHook:"fresh_built", sourceComplete:instructions.discoveryComplete === true && instructions.manifest !== undefined,
              toolViewReanchor:!!preparedViews.reason || gcfWasReanchored || !gcfBuildCurrent || lateRawReanchor })
            const mayReuse = gcfBuildCurrent && !!frozen && PrefixProvenance.reusable(frozen.provenance, provenance) && SessionPrefixSnapshot.isCurrent(frozen, built.system, toolsHash)
            const snapshot = !gcfBuildCurrent ? undefined : !frozen
              ? yield* SessionPrefixSnapshot.pin({ sessionID, profileKey, system: built.system, toolsHash, tools: currentTools, provenance, watermarkMessageID: lastUser.id })
              : SessionPrefixSnapshot.isCurrent(frozen, built.system, toolsHash) && PrefixProvenance.sameReceipt(frozen.provenance, provenance, frozen.provenanceSupported) ? frozen
                : yield* SessionPrefixSnapshot.rotate({ sessionID, profileKey, system: built.system, toolsHash, tools: currentTools, provenance, watermarkMessageID: lastUser.id })
            let prebuiltSystem = mayReuse ? snapshot!.system : built.system
            const modelMsgs = gcfBuildCurrent ? built.inheritedMessages : yield* MessageV2.toModelMessagesEffect(msgs,model,{collapseCheckpointTail:true})
            const queryParts = msgs.findLast((m) => m.info.role === "user" && m.info.id === lastUser.id)?.parts ?? []
            const query = userQueryText(queryParts)
            const messagesWithContext = [...modelMsgs]
            let contextPreface: import('./context-inspection').ContextPrefaceEvidence | undefined
            let contextWireProfile: "openai-chat" | "codex-responses" | "compatible-chat" | undefined
            if (ManagedProvider.enabled() && ['@ai-sdk/openai','@ai-sdk/openai-compatible'].includes(model.api.npm) && turnExecution) {
              const selected = Option.getOrUndefined(yield* provider.getProvider(model.providerID).pipe(Effect.option))
              try {
                const metadata = selected && Provider.managedAdapterMetadata(selected, model)
                const scope = ManagedProvider.currentScope(sessionID,turnExecution.actorID,turnExecution.executionRevision)
                if (selected && selected.options?.fetch === undefined && selected.options?.transformRequestBody === undefined
                    && selected.options?.queryParams === undefined && model.api.npm === '@ai-sdk/openai-compatible'
                    && metadata?.familyID === 'openai-compatible' && metadata.adapterID === 'openai-chat'
                    && metadata.billingLane === 'local' && scope?.connectionID === metadata.connectionID
                    && scope.billingLane === 'local' && scope.credentialRequired === false) contextWireProfile = 'compatible-chat'
                if (selected && selected.options?.fetch === undefined && model.api.npm === '@ai-sdk/openai' && metadata?.familyID === 'openai' && scope) {
                  if (metadata.billingLane === 'metered_api' && metadata.adapterID === 'openai-chat' &&
                      scope.credential?.type === 'api') contextWireProfile = 'openai-chat'
                  if (metadata.billingLane === 'subscription' && metadata.adapterID === 'openai-responses' &&
                      scope.credential?.type === 'oauth') contextWireProfile = 'codex-responses'
                }
              } catch { /* Optional retrieval stays unavailable on an unclassified adapter. */ }
            }
            const contextDeadline = performance.now() + 200
            let standingGuidance: import('./context-inspection').StandingGuidanceEvidence | undefined
            if (step === 1 && ManagedProvider.enabled() && turnExecution?.actorID === 'main' && turnExecution.rootOperationID && turnExecution.innerTurnID
                && query && Buffer.byteLength(query,'utf8') <= 8192) {
              let context: string | undefined
              try { context = boundedContextSnapshot(modelMsgs, contextDeadline) } catch { /* optional context remains unavailable */ }
              const position = messagesWithContext.findLastIndex((message) => message.role === 'user')
              if (position >= 0 && context && performance.now() < contextDeadline) {
                const deadline = contextDeadline
                const remainingMs = Math.floor(deadline-performance.now())
                const pending = remainingMs > 0 ? ManagedProvider.managedHostCall('_openclank/session/v1/context/augment', {
                  sessionID, rootOperationID:turnExecution.rootOperationID, actorID:'main', executionRevision:turnExecution.executionRevision,
                  innerTurnID:turnExecution.innerTurnID, query, context, remainingMs,
                }).catch(() => ({state:'unavailable' as const})) : Promise.resolve({state:'unavailable' as const})
                let timer: ReturnType<typeof setTimeout> | undefined
                const value = yield* Effect.promise(() => Promise.race([pending,new Promise<{state:'unavailable'}>((resolve) => {
                  timer = setTimeout(() => resolve({state:'unavailable'}),Math.max(0,deadline-performance.now()))
                })]).finally(() => { if (timer) clearTimeout(timer) }))
                if (value.state === 'found' && performance.now() < deadline && Buffer.byteLength(value.text,'utf8') <= 16000) {
                  messagesWithContext.splice(position,0,{role:'user',content:[{type:'text',text:value.text}]})
                } else if (value.state === 'ready' && performance.now() < deadline && Buffer.byteLength(value.text,'utf8') <= 16000
                    && createHash('sha256').update(value.text,'utf8').digest('hex') === value.textSHA256) {
                  const contextPartID = PartID.ascending()
                  const contextPart = {type:'text' as const,text:value.text}
                  // The ephemeral part's identity is a sidecar, never SDK/model content.
                  Object.defineProperty(contextPart,'id',{value:contextPartID,enumerable:false})
                  messagesWithContext.splice(position,0,{role:'user',content:[contextPart]})
                  if (contextWireProfile) contextPreface = {contextPartID,attestationID:value.attestationID,textSHA256:value.textSHA256,
                    text:value.text,index:position,count:messagesWithContext.length,wireProfile:contextWireProfile}
                }
              }
            }
            if (step === 1 && ManagedProvider.enabled() && turnExecution?.actorID === 'main' && turnExecution.rootOperationID && turnExecution.innerTurnID) {
              const remainingMs = Math.floor(contextDeadline-performance.now())
              if (remainingMs > 0) {
                let timer: ReturnType<typeof setTimeout> | undefined
                const pending = ManagedProvider.managedHostCall('_openclank/session/v1/context/augment', {
                  kind:'standing_guidance',sessionID,rootOperationID:turnExecution.rootOperationID,actorID:'main',
                  executionRevision:turnExecution.executionRevision,innerTurnID:turnExecution.innerTurnID,remainingMs,
                }).catch(() => ({kind:'standing_guidance' as const,state:'unavailable' as const}))
                const value = yield* Effect.promise(() => Promise.race([pending,new Promise<{kind:'standing_guidance';state:'unavailable'}>(resolve => {
                  timer=setTimeout(() => resolve({kind:'standing_guidance',state:'unavailable'}),Math.max(0,contextDeadline-performance.now()))
                })]).finally(() => {if (timer) clearTimeout(timer)}))
                if ('kind' in value && value.kind==='standing_guidance' && value.state==='ready' && performance.now()<contextDeadline &&
                    Buffer.byteLength(value.text,'utf8')<=16384 && createHash('sha256').update(value.text,'utf8').digest('hex')===value.textSHA256 && prebuiltSystem.length) {
                  prebuiltSystem=[...prebuiltSystem];prebuiltSystem[0]+='\n'+value.text
                  if (contextWireProfile && contextWireProfile !== "compatible-chat") standingGuidance={text:value.text,textSHA256:value.textSHA256,attestationID:value.attestationID,wireProfile:contextWireProfile}
                }
              }
            }
            // Turn-local guidance is never captured for a fork or later rebuild.
            lastSystemPrompt = mayReuse ? snapshot!.system : built.system
            const maxModeCfg = (yield* config.get()).experimental?.maxMode
            const useMaxMode =
              agent.name === MaxMode.MAX_MODE_AGENT && maxModeCfg !== undefined && format.type !== "json_schema"

            const adaptiveStyle = (() => {
              if (useMaxMode || session.parentID || turnExecution?.actorID!=='main' || !turnExecution.rootOperationID || !turnExecution.innerTurnID || !admittedConfig) return
              const id=turnExecution, host=ManagedProvider.currentActorHostContext(sessionID,id.actorID,id.executionRevision), encoded=host?.adaptiveStyleAdmission
              const scope=ManagedProvider.currentScope(sessionID,id.actorID,id.executionRevision), issuer=ManagedProvider.currentActorToolTimingIssuer(sessionID,id.actorID,id.executionRevision,turnExecution.rootOperationID)
              if (!encoded || !host?.ownerAccountID || !scope || !issuer?.isAlive()) return
              let admission:Record<string,unknown>; try { admission=JSON.parse(encoded) } catch { return }
              const route=ManagedProvider.currentToolViewRoute(sessionID,id.actorID,id.executionRevision)
              if (!route || admission.hostAccountID!==host.ownerAccountID || admission.rootOperationID!==id.rootOperationID || admission.sessionID!==sessionID ||
                  admission.connectionID!==route.connectionID || admission.providerID!==route.providerID || admission.modelID!==route.modelID || admission.modelRouteID!==route.modelRouteID ||
                  admission.modelID!==model.id || (admission.providerID!==model.providerID && admission.connectionID!==model.providerID)) return
              const config=admittedConfig
              return {admission:encoded,config,current:()=>{
                const now=ManagedProvider.currentActorHostContext(sessionID,id.actorID,id.executionRevision), currentRoute=ManagedProvider.currentToolViewRoute(sessionID,id.actorID,id.executionRevision)
                return !!issuer.isAlive() && isAdmittedToolTimingTurn(id,{sessionID,actorID:'main',executionRevision:id.executionRevision,rootOperationID:id.rootOperationID!,innerTurnID:id.innerTurnID!}) && ManagedProvider.currentScope(sessionID,id.actorID,id.executionRevision)===scope &&
                  now?.adaptiveStyleAdmission===encoded && now.ownerAccountID===host.ownerAccountID && now.chatID===host.chatID &&
                  !!currentRoute && currentRoute.rootOperationID===id.rootOperationID && currentRoute.connectionID===route.connectionID && currentRoute.providerID===route.providerID && currentRoute.modelID===route.modelID && currentRoute.modelRouteID===route.modelRouteID
              }}
            })()
            const processArgs = {
              ...(adaptiveStyle ? {adaptiveStyle} : {}),
              ...(rowFrames && gcfBuildCurrent && !lateRawReanchor && gcfArtifact && rowCurrent() ? {sameRequestRows:{handle:rowFrames.handle,artifact:gcfArtifact,current:rowCurrent,...(graphAdmitted ? {graphReferences:true} : {}),...(rowsAdmitted ? {} : {rows:false})}} : {}),
              user: lastUser,
              agent,
              permission: session.permission,
              sessionID,
              parentSessionID: session.parentID,
              // system: additions is preserved for non-LLM consumers of StreamInput (e.g.,
              // MessageV2.User.system for logging/replay); llm.stream itself uses prebuiltSystem.
              system: additions,
              prebuiltSystem,
              ...(sourceAttempt ? {sourceAttempt,sourceToolsCurrent} : {}),
              ...(annotationView ? {schemaAnnotationView:annotationView} : {}),
              contextSources: sourceEvidence(msgs,undefined,generatedSources),
              nativeMembership: nativeMembership?.sources ? nativeMembership : undefined,
              nativeTextSources: step === 1 && nativeSingleUserWire(nativeTextAfterPlugin, modelMsgs) ? nativeTextAfterPlugin : undefined,
              recallToolSource: recallToolConverted(msgs, modelMsgs, recallAttestations),
              contextPreface,
              standingGuidance,
              onStandingGuidance: async (evidence: {attestationID:string;textSHA256:string;dispatchID:string;bodySHA256:string;systemSHA256:string;wireProfile:'openai-chat'|'codex-responses'}) => {
                if (!turnExecution?.rootOperationID || !turnExecution.innerTurnID) return
                try { await ManagedProvider.managedHostCall('_openclank/session/v1/context/delivered', {kind:'standing_guidance',sessionID,
                  rootOperationID:turnExecution.rootOperationID,actorID:'main',executionRevision:turnExecution.executionRevision,innerTurnID:turnExecution.innerTurnID,...evidence}) } catch {}
              },
              onContextDelivery: async (evidence: {contextPartID:string;attestationID:string;textSHA256:string;dispatchID:string;bodySHA256:string}) => {
                if (!turnExecution?.rootOperationID || !turnExecution.innerTurnID) return
                try { await ManagedProvider.managedHostCall('_openclank/session/v1/context/delivered', {
                  sessionID,rootOperationID:turnExecution.rootOperationID,actorID:'main',executionRevision:turnExecution.executionRevision,
                  innerTurnID:turnExecution.innerTurnID,...evidence,
                }) } catch {}
              },
              onRecallAdmission: async (evidence: { toolCallID: string; attestationID: string; resultSHA256: string; dispatchID: string; bodySHA256: string }) => {
                if (!turnExecution || turnExecution.actorID !== "main" || !turnExecution.rootOperationID || !turnExecution.innerTurnID ||
                    admittedRecallCalls.has(evidence.toolCallID) || pendingRecallCalls.has(evidence.toolCallID)) return
                pendingRecallCalls.add(evidence.toolCallID)
                try {
                  const result = await ManagedProvider.managedHostCall("_openclank/session/v1/recall/admitted", {
                    sessionID, rootOperationID:turnExecution.rootOperationID, actorID:"main",
                    executionRevision:turnExecution.executionRevision, innerTurnID:turnExecution.innerTurnID, ...evidence,
                  })
                  if (result.accepted === true) admittedRecallCalls.add(evidence.toolCallID)
                } catch {} finally { pendingRecallCalls.delete(evidence.toolCallID) }
              },
              messages: [...messagesWithContext, ...(isLastStep ? [{ role: "user" as const, content: MAX_STEPS }] : [])],
              tools,
              activeTools,
              model,
              ...(turnExecution ? { requestContext: { actorExecution: turnExecution } } : {}),
              toolChoice: isLastStep
                ? ("none" as const)
                : format.type === "json_schema"
                  ? ("required" as const)
                  : undefined,
              agentID: lastUser.agentID,
            }

            // The alternative is rebuilt only from the original frozen C2 inputs.
            // This callback is private to the same processor/operation, never model data.
            const rebuildAlternative = (alternative:ManagedProvider.OwnedAlternativeRebuild) => Effect.gen(function* () {
              if (!ManagedProvider.currentOwnedAlternativeRebuild(alternative)) throw new Error('Alternative rebuild unavailable')
              const originalUser=alternative.original.user as MessageV2.User
              const originalMessages=alternative.original.messages.map(message=>message as MessageV2.WithParts)
              const alternateModel=yield* getModel(ProviderID.make(alternative.route.connectionID),ModelID.make(alternative.route.modelID),sessionID)
              const effort=alternative.route.options.reasoning_effort
              if (alternateModel.providerID!==alternative.route.connectionID || alternateModel.id!==alternative.route.modelID ||
                  alternateModel.api.npm!=='@ai-sdk/anthropic' || (effort!==undefined && !Object.hasOwn(alternateModel.variants ?? {},effort)) ||
                  !ManagedProvider.currentOwnedAlternativeRebuild(alternative)) throw new Error('Alternative model unavailable')
              const alternateUser={...originalUser,model:{providerID:originalUser.model.providerID,modelID:originalUser.model.modelID,
                ...(effort!==undefined ? {variant:effort} : {})}}
              const alternateTools=yield* resolveTools({agent,session,model:alternateModel,tools:originalUser.tools,processor:handle,
                bypassAgentCheck,messages:originalMessages,agentID:originalUser.agentID,task_id,mcpContext,recallAttestations,
                invalidRecallCalls,actorExecution:turnExecution,alternativeRebuild:alternative,
                ...(receiptPolicy ? {permissionRulesets:[receiptPolicy]} : {})})
              const denied=alternateTools.permissionRulesets?.length
                ? Permission.disabledIntersection(Object.keys(alternateTools.tools),alternateTools.permissionRulesets) : new Set<string>()
              const active=alternateTools.activeTools.filter(name=>!denied.has(name))
              const structuredDenied=alternateTools.permissionRulesets?.length
                ? Permission.disabledIntersection(['StructuredOutput'],alternateTools.permissionRulesets).has('StructuredOutput')
                : Permission.disabled(['StructuredOutput'],Agent.runtimePermission(agent,session.permission)).has('StructuredOutput')
              if (originalUser.format?.type==='json_schema' && !structuredDenied) {
                alternateTools.tools['StructuredOutput']=createStructuredOutputTool({schema:originalUser.format.schema,onSuccess(output){structured=output}})
                active.push('StructuredOutput')
              }
              const [alternateSkills,alternateEnv]=yield* Effect.all([sys.skills(agent,alternateModel,session.permission),sys.environment(alternateModel,session.time.created)])
              if (!ManagedProvider.currentOwnedAlternativeRebuild(alternative)) throw new Error('Alternative rebuild changed')
              const alternateAdditions=[...alternateEnv,...(alternateSkills ? [alternateSkills] : []),...instructions.content,
                ...(format.type==='json_schema' ? [STRUCTURED_OUTPUT_SYSTEM_PROMPT] : [])]
              const alternateBuilt=yield* buildLLMRequestPrefix({sessionID,agent,model:alternateModel,msgs:originalMessages,
                additions:alternateAdditions,collapseCheckpointTail:true}).pipe(Effect.provideService(LLM.Service,llm),Effect.provideService(ToolRegistry.Service,registry))
              const prefixTools=yield* Effect.promise(()=>SessionPrefixSnapshot.resolveTools(alternateTools.tools,active))
              Object.assign(alternateTools.tools,prefixTools.tools)
              if (!ManagedProvider.currentOwnedAlternativeRebuild(alternative)) throw new Error('Alternative rebuild changed')
              yield* slog.warn('alternative uses a fresh model prefix', {reason:standingGuidance ? 'standing_guidance_omitted' : 'explicit_alternative'})
              return {...processArgs,schemaAnnotationView:undefined,user:alternateUser,model:alternateModel,tools:alternateTools.tools,activeTools:active,
                system:alternateAdditions,prebuiltSystem:alternateBuilt.system,standingGuidance:undefined,onStandingGuidance:undefined,nativeMembership:undefined,messages:[...alternateBuilt.inheritedMessages,
                  ...(isLastStep ? [{role:'user' as const,content:MAX_STEPS}] : [])],contextPreface:undefined,
                nativeTextSources:step===1 && nativeSingleUserWire(nativeTextAfterPlugin,alternateBuilt.inheritedMessages) ? nativeTextAfterPlugin : undefined,
                recallToolSource:recallToolConverted(originalMessages,alternateBuilt.inheritedMessages,recallAttestations),
                contextSources:sourceEvidence(originalMessages,undefined,generatedSources)}
            })

            const preQuery = {
              cancel: undefined as boolean | undefined,
              cancelReason: undefined as string | undefined,
            }
            yield* plugin.trigger(
              "session.userQuery.pre",
              { sessionID, agentID: resolvedAgentID, step, messageID: lastUser.id, query },
              preQuery,
            )
            if (preQuery.cancel) {
              cancelled = true
              cancelReason = preQuery.cancelReason
              handle.message.error = new MessageV2.AbortedError({
                message: preQuery.cancelReason ?? "Step cancelled by plugin",
              }).toObject()
              handle.message.finish = "cancelled"
              yield* sessions.updateMessage(handle.message)
              yield* plugin.trigger(
                "session.userQuery.post",
                {
                  sessionID,
                  agentID: resolvedAgentID,
                  step,
                  messageID: lastUser.id,
                  query,
                  assistantMessageID: handle.message.id,
                  finish: handle.message.finish,
                  error: preQuery.cancelReason,
                  trajectory: trajectoryForStep(msgs, handle.message),
                  systemPrompt: lastSystemPrompt,
                },
                {},
              )
              return "break" as const
            }

            if ((preparedViews.views.size && (!lastToolViewContext || !toolViewContextCurrent(lastToolViewContext))) || !gcfCurrent()) {
              if (lastToolViewContext) {lastToolViewContext.control.disabled=true;lastToolViewContext.control.reason ??= 'scope_unavailable'}
              lateRawReanchor = true
              yield* slog.warn('tool view uses a raw reanchor', {reason:'scope_unavailable'})
              const raw = yield* MessageV2.toModelMessagesEffect(msgs,model,{collapseCheckpointTail:true})
              const restored=restoreContextPreface(raw,contextPreface)
              if (restored.reason) yield* slog.warn('context preface unavailable during raw reanchor', {reason:restored.reason})
              processArgs.messages = [...restored.messages,...(isLastStep ? [{role:'user' as const,content:MAX_STEPS}] : [])]
              processArgs.prebuiltSystem = built.system
              processArgs.standingGuidance = undefined
              processArgs.nativeMembership = undefined
              processArgs.contextPreface = restored.preface
              processArgs.nativeTextSources = step===1 && nativeSingleUserWire(nativeTextAfterPlugin,raw) ? nativeTextAfterPlugin : undefined
              processArgs.recallToolSource = recallToolConverted(msgs,raw,recallAttestations)
              // The logged reanchor is sufficient here: never add a second
              // database wait after an optional decision has already failed.
            }
            if (!useMaxMode && ManagedProvider.enabled() && turnExecution?.actorID==='main' && turnExecution.rootOperationID && turnExecution.innerTurnID) {
              ManagedProvider.freezeOriginalAlternativeInput({sessionID,rootOperationID:turnExecution.rootOperationID,actorID:'main',
                executionRevision:turnExecution.executionRevision,innerTurnID:turnExecution.innerTurnID},{user:lastUser,messages:msgs})
            }
            const stepEffect = useMaxMode
              ? MaxMode.runMaxStep({
                  // runMaxStep reuses the identical per-step args as handle.process,
                  // plus the orchestration handles it needs.
                  ...processArgs,
                  handle,
                  llm,
                  candidates: maxModeCfg?.candidates,
                  setStatus: (message) => status.set(sessionID, message ? { type: "busy", message } : { type: "busy" }),
                })
              : handle.process(processArgs,rebuildAlternative)

            const result = yield* stepEffect.pipe(
              Effect.onExit((exit) =>
                plugin
                  .trigger(
                    "session.userQuery.post",
                    {
                      sessionID,
                      agentID: resolvedAgentID,
                      step,
                      messageID: lastUser.id,
                      query,
                      assistantMessageID: handle.message.id,
                      finish: handle.message.finish,
                      error: Exit.isFailure(exit) ? Cause.pretty(exit.cause) : sessionErrorText(handle.message.error),
                      finalText: assistantFinalText(handle.message, MessageV2.parts(handle.message.id)),
                      trajectory: trajectoryForStep(msgs, handle.message),
                      systemPrompt: lastSystemPrompt,
                    },
                    {},
                  )
                  .pipe(Effect.ignore),
              ),
            )
            if (!lateRawReanchor && snapshot && SessionPrefixSnapshot.isCurrent(snapshot, built.system, toolsHash) && PrefixProvenance.sameReceipt(snapshot.provenance, provenance, snapshot.provenanceSupported) && handle.message.time.completed && !handle.message.error)
              yield* SessionPrefixSnapshot.advance({ sessionID, profileKey, revision: snapshot.revision, watermarkMessageID: handle.message.id })

            if (result === "continue" && (yield* autoContinueOutputLength({ lastUser, assistant: handle.message }))) {
              return "continue" as const
            }

            if (result === "text-repeat") {
              if (yield* handleTextRepeat({ lastUser })) return "continue" as const
              return "break" as const
            }
            if (result === "stop") return "break" as const

            if (structured !== undefined) {
              handle.message.structured = structured
              handle.message.finish = handle.message.finish ?? "stop"
              yield* sessions.updateMessage(handle.message)
              return "break" as const
            }

            // Empty/no-op tool-call loop guard (main branch). Intercept before
            // classify would `continue` an empty tool-calls step: soft-nudge
            // within budget, hard-halt once exceeded. A non-empty step returns
            // "none" and falls through to normal classification.
            const emptyStep = yield* handleEmptyStep({ lastUser, assistant: handle.message })
            if (emptyStep === "halt") return "break" as const
            if (emptyStep === "continue") return "continue" as const

            const classification = classifyAssistantStep({
              phase: "after-process",
              lastUser,
              assistant: handle.message,
              parts: MessageV2.parts(handle.message.id),
              processResult: result,
            })
            if (classification.type === "filtered") {
              yield* writeContentFilterError({ assistant: handle.message })
              return "break" as const
            }
            if (classification.type === "failed") {
              yield* writeModelError({ assistant: handle.message, reason: classification.reason })
              return "break" as const
            }
            if (classification.type === "text-tool-call") {
              if (yield* autoRetryTextToolCall({ lastUser, assistant: handle.message })) return "continue" as const
              return "break" as const
            }
            if (classification.type !== "continue" && !handle.message.error && format.type === "json_schema") {
              if (yield* autoRetryStructuredOutput({ lastUser, assistant: handle.message })) return "continue" as const
              return "break" as const
            }

            if (
              (classification.type === "think-only" || classification.type === "invalid") &&
              format.type !== "json_schema"
            ) {
              const reason = classification.type === "invalid" ? classification.reason : "think-only"
              if (yield* autoContinueInvalidOutput({ lastUser, assistant: handle.message, reason }))
                return "continue" as const
              return "break" as const
            }

            if (classification.type === "final" && classification.degraded)
              yield* slog.warn("degraded final on abnormal finish", { finish: handle.message.finish })
            if (!isBoundedComputation && result === "overflow") {
              // Subagent overflow → per-actor compaction. Insert a boundary
              // tagged with the subagent's agent_id; the next runLoop iteration
              // will see a trimmed context (filterCompactedEffect stops at
              // the boundary).
              // Gate must exclude "main" — see comment at the matching gate
              // earlier in this file (~line 1716) and at checkpoint.ts:715.
              if (lastUser.agentID && lastUser.agentID !== "main") {
                yield* compaction
                  .create({
                    sessionID,
                    agent: lastUser.agent,
                    model: { providerID: model.providerID, modelID: model.id },
                    auto: true,
                    overflow: true,
                    agentID: lastUser.agentID,
                  })
                  .pipe(Effect.ignore)
                return "continue" as const
              }

              // Main-agent provider-signalled overflow: prefer rebuild over
              // compaction. Shared with the manual `/rebuild` command via
              // rebuildFromCheckpoint (does not block on the writer; uses the
              // on-disk checkpoint). Fall back to compaction only when no
              // boundary can be produced.
              const inserted2 = yield* rebuildFromCheckpoint({
                sessionID,
                msgs,
                agentID: lastUser.agentID,
                agent: lastUser.agent,
                model: { providerID: model.providerID, id: model.id },
              })
              if (inserted2) return "continue" as const

              // F39: no checkpoint — fall back to compaction (LLM-driven lossy summary).
              yield* compaction
                .create({
                  sessionID,
                  agent: lastUser.agent,
                  model: { providerID: model.providerID, modelID: model.id },
                  auto: true,
                  overflow: true,
                  agentID: lastUser.agentID,
                })
                .pipe(Effect.ignore)
            }
            return "continue" as const
          }).pipe(Effect.ensuring(instruction.clear(handle.message.id)))

          // --- Text Loop Detection (cross-step) ---
          const completedParts = MessageV2.parts(handle.message.id)
          const crop = detectStreak(
            [...msgs, { info: handle.message, parts: completedParts }]
              .filter(
                (message) =>
                  message.info.role === "assistant" &&
                  !!message.info.finish &&
                  !message.info.error,
              )
              .map((message) => ({ id: message.info.id, key: streakKey(message.parts) })),
          )
          const croppedStreak = !!crop && crop.toId === handle.message.id
          if (croppedStreak) {
            yield* sessions.updatePart({ id: PartID.ascending(), messageID: lastUser.id, sessionID, type: "text", synthetic: true, ignored: true, text: "", metadata: cropMetadata(crop) } satisfies MessageV2.TextPart)
          }
          const stepText = completedParts
            .filter((p): p is MessageV2.TextPart => p.type === "text" && !p.synthetic)
            .map((p) => p.text)
            .join(" ")
          if (stepText.trim()) {
            // Include tool call signatures in the key so same text + different tools ≠ loop
            const toolSig = completedParts
              .filter((p): p is MessageV2.ToolPart => p.type === "tool")
              .map((p) => `${p.tool}:${JSON.stringify(p.state && "input" in p.state ? p.state.input : "")}`)
              .join("|")
            const normalized = normalizeForLoopDetection(stepText) + (toolSig ? `\0${toolSig}` : "")
            textLoopBuffer.push(normalized)
            if (textLoopBuffer.length > TEXT_LOOP_BUFFER_SIZE) textLoopBuffer.shift()

            if (textLoopBuffer.length >= TEXT_LOOP_TRIGGER_COUNT) {
              const isTextLoop = detectTextLoop(textLoopBuffer, TEXT_LOOP_TRIGGER_COUNT)

              if (isTextLoop && !croppedStreak) {
                if (textLoopRecoveryAttempts >= TEXT_LOOP_MAX_RECOVERY) {
                  yield* slog.info("text loop: max recovery exceeded, terminating")
                  yield* bus.publish(Session.Event.Error, {
                    sessionID,
                    error: new NamedError.Unknown({
                      message: `Text loop detected: model repeated the same output ${TEXT_LOOP_TRIGGER_COUNT} times after ${TEXT_LOOP_MAX_RECOVERY} recovery attempts. Session terminated.`,
                    }).toObject(),
                  })
                  break
                }
                const recoveryText = textLoopRecoveryAttempts === 0 ? RECOVERY_PROMPT_MILD : RECOVERY_PROMPT_STRONG
                // Create a NEW user message at the end of conversation (not append to original)
                const reentry = yield* sessions.updateMessage({
                  id: MessageID.ascending(),
                  role: "user" as const,
                  sessionID,
                  agentID: lastUser.agentID,
                  agent: lastUser.agent,
                  model: lastUser.model,
                  tools: lastUser.tools,
                  format: lastUser.format,
                  time: { created: Date.now() },
                })
                yield* sessions.updatePart({
                  id: PartID.ascending(),
                  messageID: reentry.id,
                  sessionID,
                  type: "text",
                  synthetic: true,
                  text: recoveryText,
                } satisfies MessageV2.TextPart)
                textLoopRecoveryAttempts++
                textLoopBuffer.length = 0
                yield* slog.info("text loop: recovery injected", { attempt: textLoopRecoveryAttempts })
                continue
              }
            }
          }

          if (outcome === "break") {
            // A hard halt is terminal — skip the ReAct re-entry gates so a
            // degraded model can't be re-driven into the same empty loop.
            if (hardHalt) break
            if (receiptBinding && UserInput.hasDeliverable(sessionID, receiverActorID, receiptBinding)) {
              afterTurnDeliveryReady = UserInput.hasPendingAfterTurn(sessionID, receiverActorID, receiptBinding)
              continue
            }
            if (yield* goalGate(lastUser)) continue
            break
          }
          continue
        }

        const promptOps = yield* ops()
        if (lastModelForPrune && lastFinishedForPrune) {
          yield* prune
            .prune({
              sessionID,
              model: lastModelForPrune,
              tokens: lastFinishedForPrune.tokens,
              lastAssistantTime: lastFinishedForPrune.time.completed,
              promptOps,
              cfg: admittedConfig,
            })
            .pipe(Effect.ignore, Effect.forkIn(scope))
        }
        const final = yield* lastAssistant(sessionID, agentID)
        const finalIsError = final.info.role === "assistant" && !!final.info.error
        const lastUserForMetrics = yield* sessions.findMessage(sessionID, (m) => m.info.role === "user", {
          agentID: "*",
        })
        yield* publishAgentRequest(
          finalIsError ? "error" : "completed",
          Option.isSome(lastUserForMetrics) ? lastUserForMetrics.value.info.agent : final.info.agent,
        )
        // Woken-peer completion signal. forkWork.notify only wraps the FIRST
        // (spawn) turn; a persistent background peer that finishes a later,
        // inbox-driven turn would otherwise go idle silently and force the
        // orchestrator to poll. When this loop was woken via the inbox path
        // (notifyParentOnComplete), mirror forkWork's actor_notification to the
        // parent so the event-driven model holds. Gated to background peers and
        // excludes system subagents (checkpoint-writer/dream/distill). The flag
        // is never set on the spawn turn, so turn 1 is not double-notified.
        if (notifyParentOnComplete && agentID && session.parentID) {
          const actor = yield* actorRegistry.get(sessionID, agentID)
          if (actor && actor.mode === "peer" && actor.background && !SYSTEM_SPAWNED_AGENT_TYPES.has(actor.agent)) {
            const finalText = final.info.role === "assistant" ? assistantFinalText(final.info, final.parts) : undefined
            const parsed = parseReturnHeader(finalText)
            const status = finalIsError ? "failed" : "completed"
            yield* inbox
              .send({
                receiverSessionID: session.parentID,
                receiverActorID: actor.parentActorID ?? "main",
                senderSessionID: sessionID,
                senderActorID: agentID,
                type: "actor_notification",
                notification: {
                  actorID: agentID,
                  description: actor.description,
                  status,
                  ...(actor.requestedModel ? { requestedModel: actor.requestedModel } : {}),
                  ...(actor.effectiveModel ? { effectiveModel: actor.effectiveModel } : {}),
                  ...(actor.hostContext ? { hostContext: actor.hostContext } : {}),
                  ...(status === "completed"
                    ? {
                        result: finalText ?? "(no output)",
                        ...(parsed.status ? { reportedStatus: parsed.status } : {}),
                        ...(parsed.summary ? { reportedSummary: parsed.summary } : {}),
                      }
                    : { error: final.info.role === "assistant" ? sessionErrorText(final.info.error) : "unknown" }),
                },
              })
              .pipe(Effect.ignore)
          }
        }
        return final
      }).pipe(Effect.onExit((exit) => Effect.gen(function* () {
        const postSession=yield* readPostSession(exit)
        if (Exit.isFailure(exit) || managedOutcome !== "completed") invalidateSourceAttempt(sourceAttempt)
        let forwardPairOutput: {version:1;messageID:string;partID:string;textSHA256:string} | undefined
        if (forwardPairOffered && managedTurn && managedTurn.actorID === 'main' && !preparedTurn &&
            sourceCandidate && step === 1 && Exit.isSuccess(exit)) {
          try {
            const value = exit.value, info = value.info, parts = value.parts
            const scope = ManagedProvider.currentScope(sessionID, managedTurn.actorID, managedTurn.executionRevision)
            const stored = MessageV2.get({sessionID, messageID:info.id})
            const text = parts.filter(part => part.type === 'text')
            const types = parts.map(part => part.type).join(',')
            if (scope && !ManagedProvider.isOwnedAlternativeActive(managedTurn,scope) && info.role === 'assistant' &&
                info.sessionID === sessionID && info.parentID === sourceCandidate.messageID && !info.error && info.time.completed &&
                ['text','step-start,text','text,step-finish','step-start,text,step-finish'].includes(types) &&
                text.length === 1 && text[0].type === 'text' && !text[0].synthetic && !text[0].ignored &&
                text[0].text.length <= 8192 && Buffer.byteLength(text[0].text,'utf8') <= 8192 &&
                isDeepStrictEqual(stored.info,info) && isDeepStrictEqual(stored.parts,parts)) {
              forwardPairOutput = {version:1,messageID:info.id,partID:text[0].id,textSHA256:createHash('sha256').update(text[0].text,'utf8').digest('hex')}
            }
          } catch { /* Optional witness is unavailable. */ }
        }
        let sourceWireSeen=false,sourceOutputSeen=false
        let sourceClosure: import("../acp/openclank-protocol").OpenClankManagedProtocol.ManagedTurnFinishRequest["sourceClosure"]
        if (managedOutcome === "completed" && forwardPairOutput && sourceAttempt && sourceCandidate &&
            sourceCandidate.partID && sourceCandidate.digest && managedTurn?.actorID === "main" && !preparedTurn) {
          const output = yield* Effect.tryPromise({try:()=>readSettledSourceOutput(sourceAttempt!),catch:()=>new Error("source observation unavailable")}).pipe(Effect.catch(()=>Effect.succeed(undefined)))
          const wire=readSourceWire(sourceAttempt)
          sourceWireSeen=!!wire;sourceOutputSeen=!!output
          if (wire && output && wire.root.messageID===sourceCandidate.messageID && wire.root.partID===sourceCandidate.partID &&
              wire.root.digest===sourceCandidate.digest && output.dispatchID===wire.dispatchID &&
              output.messageID===forwardPairOutput.messageID && output.partID===forwardPairOutput.partID &&
              output.textSHA256===forwardPairOutput.textSHA256) sourceClosure={version:1,wire,output}
        }
        if (managedTurn?.actorID === "main" && !sourceClosure) log.debug("source_closure_unavailable", {
          attempt:!!sourceAttempt, candidate:!!sourceCandidate, forwardPair:!!forwardPairOutput,
        })
        yield* firePostSession(exit,postSession)
        if (sourceClosure && sourceAttempt) {
          const currentWire=readSourceWire(sourceAttempt), currentOutput=readSourceOutput(sourceAttempt)
          if (!currentWire || !currentOutput || !isDeepStrictEqual(currentWire,sourceClosure.wire) ||
              !isDeepStrictEqual(currentOutput,sourceClosure.output)) sourceClosure=undefined
        }
        if (managedTurn?.actorID === 'main') {
          try { log.debug('paired_source_final', {completed:managedOutcome==='completed',candidate:!!sourceCandidate,offered:forwardPairOffered,forward:!!forwardPairOutput,attempt:!!sourceAttempt,wire:sourceWireSeen,output:sourceOutputSeen,closure:!!sourceClosure}) } catch { /* Diagnostic only. */ }
        }
        retireAdmittedToolTimingTurn(turnExecution)
        if (managedTurn) {
          // Drain only this execution's already-issued capture callbacks while its turn is live.
          const finishingTurn = managedTurn
          yield* Effect.promise(async () => {
            try {
              const scope = ManagedProvider.currentScope(sessionID, finishingTurn.actorID, finishingTurn.executionRevision)
              if (scope) {
                const { flushCaptureScope } = await import("@/provider/logging-transport")
                await flushCaptureScope(scope)
              }
            } catch { /* Optional capture does not block turn finish. */ }
          })
          if (preparedTurn) preparedTurn.finishAttempted = true
          if (turnExecution?.retirement) turnExecution.retirement.lastTurnOutcome = managedOutcome
          const finished = yield* Effect.tryPromise({
            try: () => ManagedProvider.managedHostCall("_openclank/session/v1/turn/finish", {
              ...managedTurn, outcome: managedOutcome,
              ...(managedOutcome === 'completed' && forwardPairOutput ? {forwardPairOutput} : {}),
              ...(sourceClosure ? {sourceClosure} : {}),
            }),
            catch: (cause) => new Error(`managed turn retirement outcome is unknown: ${String(cause)}`),
          }).pipe(Effect.exit)
          if (Exit.isFailure(finished) || !finished.value.accepted) {
            if (turnExecution?.retirement) turnExecution.retirement.unknown = true
            if (Exit.isFailure(finished)) return yield* Effect.failCause(finished.cause).pipe(Effect.orDie)
            return yield* Effect.die(new Error("managed turn retirement was not accepted"))
          }
        }
      })), Effect.orDie)
    })

    const hasUsefulAssistantParts = (parts: readonly MessageV2.Part[]) =>
      parts.some((part) =>
        (part.type === "text" && !part.synthetic && !part.ignored && part.text.trim().length > 0) ||
        part.type === "tool" ||
        (part.type === "reasoning" && part.text.trim().length > 0),
      )

    const isRecoveryWorthyAssistant = (info: MessageV2.Assistant) => {
      if ("completed" in info.time && !info.error && info.finish && info.finish !== "tool-calls" && info.finish !== "length") return false
      if (info.finish === "stop" && !info.error) return false
      return true
    }

    const recovery = Effect.fn("SessionPrompt.recovery")(function* (input: {
      sessionID: SessionID
      agentID?: string
      allowBusy?: boolean
    }) {
      if (!input.allowBusy && (yield* status.get(input.sessionID)).type !== "idle") return []
      const msgs = yield* sessions.messages({ sessionID: input.sessionID, agentID: input.agentID ?? "main" })
      const candidates: RecoveryCandidate[] = []
      for (const [index, msg] of msgs.entries()) {
        if (msg.info.role !== "assistant" || !isRecoveryWorthyAssistant(msg.info)) continue
        const parentID = msg.info.parentID
        if (!msgs.some((parent) => parent.info.role === "user" && parent.info.id === parentID)) continue
        if (msgs.slice(index + 1).some((later) => later.info.role === "user" || later.info.role === "assistant")) continue
        candidates.push({ assistantMessageID: msg.info.id, parentMessageID: parentID, created: msg.info.time.created })
      }
      return candidates
    })

    const recoveryWork = (input: ResumeTurnInput) => Effect.gen(function* () {
      const agentID = input.agentID ?? "main"
      const candidates = yield* recovery({ sessionID: input.sessionID, agentID, allowBusy: true })
      const candidate = candidates.find((item) => item.assistantMessageID === input.assistantMessageID)
      if (!candidate) return yield* Effect.fail(new NotFoundError({ message: `No resumable interrupted turn found for assistant message ${input.assistantMessageID}` }))
      const msgs = yield* sessions.messages({ sessionID: input.sessionID, agentID })
      const target = msgs.find((item) => item.info.id === input.assistantMessageID)
      if (!target || target.info.role !== "assistant" || target.info.parentID !== candidate.parentMessageID)
        return yield* Effect.fail(new NotFoundError({ message: "Recovery candidate disappeared" }))
      const targetInfo = target.info
      const parentID = targetInfo.parentID
      const useful = hasUsefulAssistantParts(target.parts)
      if (targetInfo.error && !("completed" in targetInfo.time)) {
        yield* sessions.updateMessage({ ...targetInfo, time: { ...targetInfo.time, completed: Date.now() } })
      }
      if (!useful) {
        for (const item of msgs) {
          if (item.info.role === "assistant" && item.info.parentID === parentID && !item.info.error && !hasUsefulAssistantParts(item.parts))
            yield* sessions.removeMessage({ sessionID: input.sessionID, messageID: item.info.id })
        }
      } else if (!targetInfo.error) {
        yield* sessions.updateMessage({
          ...targetInfo,
          time: { ...targetInfo.time, completed: targetInfo.time.completed ?? Date.now() },
          error: new MessageV2.AbortedError({ message: "Abandoned: resumed as a new assistant turn" }).toObject(),
        })
      }
      return yield* runLoop(input.sessionID, agentID, input.task_id, undefined, undefined, useful ? targetInfo.id : undefined, input.model, !useful)
    })

    const managedRecoveryWork = (input: ResumeTurnInput) => Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
      if (!ManagedProvider.enabled()) return yield* restore(recoveryWork(input))
      const reservation = input.managedRecovery
      const agentID = input.agentID ?? "main"
      if (!reservation || agentID !== "main") return yield* Effect.die(new Error("managed recovery requires a fresh main-actor reservation"))
      const actor = yield* actorRegistry.get(input.sessionID, agentID)
      if (!actor || actor.status !== "idle" || actor.executionRevision !== reservation.expectedExecutionRevision || !actor.effectiveModel)
        return yield* Effect.die(new Error("managed recovery actor or revision changed"))
      const candidates = yield* recovery({ sessionID: input.sessionID, agentID, allowBusy: true })
      const candidate = candidates.find((item) => item.assistantMessageID === input.assistantMessageID)
      if (!candidate) return yield* Effect.die(new Error("managed recovery candidate changed"))
      const messages = yield* sessions.messages({ sessionID: input.sessionID, agentID })
      const parent = messages.find((item) => item.info.id === candidate.parentMessageID && item.info.role === "user")
      if (!parent || parent.info.role !== "user") return yield* Effect.die(new Error("managed recovery parent message is unavailable"))
      const mode = actor.agent === "chat" ? "chat" : actor.agent === "plan" ? "plan" : "agent"
      if (parent.info.agent !== actor.agent || reservation.turnPolicy.mode !== mode ||
          reservation.turnPolicy.incognito || reservation.turnPolicy.noMemory || reservation.turnPolicy.useResearch)
        return yield* Effect.die(new Error("managed recovery interaction profile or requested policy is unavailable"))
      const model = actor.effectiveModel
      const route = reservation.managedRoute
      if (route.rootOperationID !== reservation.rootOperationID || route.modelID !== model.modelID ||
          (route.providerID !== model.providerID && route.connectionID !== model.providerID) ||
          parent.info.model.providerID !== model.providerID || parent.info.model.modelID !== model.modelID ||
          (input.model && (input.model.providerID !== model.providerID || input.model.modelID !== model.modelID)))
        return yield* Effect.die(new Error("managed recovery model or route changed"))
      const preflight = yield* restore(Effect.tryPromise({
        try: () => ManagedProvider.preflightActorRoute({ sessionID: input.sessionID, actorID: agentID,
          rootOperationID: reservation.rootOperationID, expectedExecutionRevision: actor.executionRevision,
          model: { providerID: model.providerID, modelID: model.modelID }, modelSource: "explicit",
          ...(route.grantID ? { grantID: route.grantID } : {}),
          ...(route.preferredAccountID ? { preferredAccountID: route.preferredAccountID } : {}),
          ...(route.inheritedAccountID ? { inheritedAccountID: route.inheritedAccountID } : {}),
        }),
        catch: (cause) => new Error(`managed recovery preflight failed: ${String(cause)}`),
      }).pipe(Effect.orDie))
      if (preflight.bootstrap.handoffID !== reservation.handoffID || preflight.modelRouteID !== route.modelRouteID ||
          preflight.connectionID !== route.connectionID || (route.grantID && preflight.grantID !== route.grantID))
        return yield* Effect.die(new Error("managed recovery reservation or route changed"))
      const revision = yield* actorRegistry.beginExecution(input.sessionID, agentID, undefined, actor.executionRevision)
      if (revision === undefined) return yield* Effect.die(new Session.BusyError(input.sessionID))
      const retirement: { unknown: boolean; lastInnerTurnID?: string; lastTurnOutcome?: "completed" | "cancelled" | "error" } = { unknown: false }
      const preparedTurn = { innerTurnID: ulid(), entered: false, finishAttempted: false }
      const identity: ActorExecutionIdentity = { sessionID: input.sessionID, actorID: agentID, executionRevision: revision,
        rootOperationID: reservation.rootOperationID, preflightRoute: preflight, retirement, preparedTurn }
      const activated = yield* restore(Effect.tryPromise({
        try: () => ManagedProvider.activateActorOperation(identity, preflight, model, { chatID: preflight.bootstrap.stableChatID }),
        catch: (cause) => new Error(`managed recovery activation failed: ${String(cause)}`),
      }).pipe(Effect.exit))
      if (Exit.isFailure(activated)) {
        yield* actorRegistry.settleExecution(input.sessionID, agentID, revision, "failure", Cause.pretty(activated.cause)).pipe(Effect.ignore)
        return yield* Effect.failCause(activated.cause).pipe(Effect.orDie)
      }
      retirement.lastInnerTurnID = preparedTurn.innerTurnID
      const turn = { sessionID: input.sessionID, actorID: agentID, executionRevision: revision,
        rootOperationID: reservation.rootOperationID, innerTurnID: preparedTurn.innerTurnID }
      let started = false
      const work = Effect.gen(function* () {
        const start = yield* Effect.tryPromise({
          try: () => ManagedProvider.startManagedActorTurn(turn),
          catch: (cause) => new Error(`managed recovery admission outcome is unknown: ${String(cause)}`),
        }).pipe(Effect.exit)
        if (Exit.isFailure(start)) { retirement.unknown = true; return yield* Effect.failCause(start.cause).pipe(Effect.orDie) }
        if (!start.value.accepted || start.value.sessionID !== turn.sessionID || start.value.actorID !== turn.actorID ||
            start.value.rootOperationID !== turn.rootOperationID || start.value.executionRevision !== turn.executionRevision ||
            start.value.innerTurnID !== turn.innerTurnID) { retirement.unknown = true; return yield* Effect.die(new Error("managed recovery admission identity changed")) }
        started = true
        const authority = yield* Effect.tryPromise({
          try: () => ManagedProvider.readManagedSessionBinding(input.sessionID),
          catch: (cause) => new Error(`managed recovery binding unavailable: ${String(cause)}`),
        }).pipe(Effect.orDie)
        const local = yield* Effect.tryPromise({
          try: () => ManagedProvider.ensureManagedSessionBinding(input.sessionID),
          catch: (cause) => new Error(`managed recovery registration unavailable: ${String(cause)}`),
        }).pipe(Effect.orDie)
        if (authority.authority || preflight.bootstrap.authority || local?.authority) return yield* Effect.die(new Error("private recovery is unavailable"))
        if (!local || local.transition || local.engineSessionID !== input.sessionID ||
            authority.owner !== preflight.bootstrap.owner || authority.stableChatID !== preflight.bootstrap.stableChatID ||
            authority.mapRevision < preflight.bootstrap.mapRevision || authority.mappingRevision !== preflight.bootstrap.mappingRevision ||
            authority.workspaceRevision !== preflight.bootstrap.workspaceRevision || local.owner !== authority.owner ||
            local.stableChatID !== authority.stableChatID || local.mappingRevision !== authority.mappingRevision ||
            local.bindingRevision !== authority.workspaceRevision)
          return yield* Effect.die(new Error("managed recovery binding changed"))
        yield* Effect.sync(() => ManagedProvider.bindActorHostContext(input.sessionID, agentID, revision, authority))
        return yield* recoveryWork(input).pipe(Effect.provideService(ActorExecutionContext, identity))
      })
      const result = yield* restore(work).pipe(Effect.exit)
      if (started && !preparedTurn.finishAttempted && !retirement.unknown) {
        retirement.lastTurnOutcome = Exit.isFailure(result) && Cause.hasInterruptsOnly(result.cause) ? "cancelled" : "error"
        const finished = yield* Effect.tryPromise({
          try: () => ManagedProvider.managedHostCall("_openclank/session/v1/turn/finish", { ...turn,
            outcome: Exit.isFailure(result) && Cause.hasInterruptsOnly(result.cause) ? "cancelled" : "error" }),
          catch: (cause) => new Error(`managed recovery retirement outcome is unknown: ${String(cause)}`),
        }).pipe(Effect.exit)
        if (Exit.isFailure(finished) || !finished.value.accepted) retirement.unknown = true
      }
      const outcome = Exit.isSuccess(result) ? "success" : Cause.hasInterruptsOnly(result.cause) ? "cancelled" : "failure"
      if (retirement.unknown) yield* actorRegistry.markExecutionUncertain(input.sessionID, agentID, revision, { rootOperationID: reservation.rootOperationID, rootSessionID: preflight.bootstrap.rootSessionID, innerTurnID: retirement.lastInnerTurnID, turnOutcome: retirement.lastTurnOutcome }).pipe(Effect.ignore)
      const settlement = { rootOperationID: reservation.rootOperationID, innerTurnID: retirement.lastInnerTurnID }
      const settled = retirement.unknown ? false : yield* actorRegistry.settleExecution(input.sessionID, agentID, revision, outcome,
        Exit.isFailure(result) ? Cause.pretty(result.cause) : undefined, settlement).pipe(Effect.orElseSucceed(() => false))
      if (settled) {
        const finished = yield* Effect.tryPromise({
          try: () => ManagedProvider.finishActorExecution({
            sessionID: input.sessionID, actorID: agentID, executionRevision: revision, rootOperationID: reservation.rootOperationID,
          }, outcome),
          catch: (cause) => new Error(`managed recovery execution retirement failed: ${String(cause)}`),
        }).pipe(Effect.exit)
        if (Exit.isSuccess(finished) && finished.value.accepted)
          yield* actorRegistry.clearManagedSettlement(input.sessionID, agentID, revision, settlement).pipe(Effect.ignore)
      }
      const capturePersisted = yield* persistManagedActorCapture(sessions, identity).pipe(Effect.orElseSucceed(() => false))
      if (!capturePersisted) yield* Effect.sync(() => log.warn("managed recovery capture was not persisted", { sessionID: input.sessionID, actorID: agentID, executionRevision: revision }))
      yield* Effect.promise(() => activated.value.release()).pipe(Effect.orDie)
      yield* Effect.sync(() => ManagedProvider.consumeActorCapture(input.sessionID, agentID, revision))
      if (Exit.isFailure(result)) return yield* Effect.failCause(result.cause).pipe(Effect.orDie)
      return result.value
    }))

    const resume = Effect.fn("SessionPrompt.resume")(function* (input: ResumeTurnInput) {
      const agentID = input.agentID ?? "main"
      yield* assertExecutionActive(input.sessionID, agentID)
      if (ManagedProvider.enabled() && input.managedRecovery) {
        const check = spawnRef.current?.hasOuterExecution
        yield* reconcileManagedActorExecution(actorRegistry, input.sessionID, agentID,
          state.assertNotBusy(input.sessionID, agentID),
          (revision) => check ? check(input.sessionID, agentID, revision) : Effect.succeed(true))
      } else yield* state.assertNotBusy(input.sessionID, agentID)
      const candidates = yield* recovery({ sessionID: input.sessionID, agentID })
      if (!candidates.some((item) => item.assistantMessageID === input.assistantMessageID))
        return yield* Effect.fail(new NotFoundError({ message: `No resumable interrupted turn found for assistant message ${input.assistantMessageID}` }))
      if (input.model) yield* getModel(ProviderID.make(input.model.providerID), ModelID.make(input.model.modelID), input.sessionID)
      return yield* state.startAndWait(input.sessionID, agentID, lastAssistant(input.sessionID, agentID), managedRecoveryWork(input).pipe(Effect.orDie))
    })

    const resumeBackground = Effect.fn("SessionPrompt.resumeBackground")(function* (input: ResumeTurnInput) {
      const agentID = input.agentID ?? "main"
      yield* assertExecutionActive(input.sessionID, agentID)
      if (ManagedProvider.enabled() && input.managedRecovery) {
        const check = spawnRef.current?.hasOuterExecution
        yield* reconcileManagedActorExecution(actorRegistry, input.sessionID, agentID,
          state.assertNotBusy(input.sessionID, agentID),
          (revision) => check ? check(input.sessionID, agentID, revision) : Effect.succeed(true))
      } else yield* state.assertNotBusy(input.sessionID, agentID)
      const candidates = yield* recovery({ sessionID: input.sessionID, agentID: input.agentID })
      if (!candidates.some((item) => item.assistantMessageID === input.assistantMessageID))
        return yield* Effect.fail(new NotFoundError({ message: `No resumable interrupted turn found for assistant message ${input.assistantMessageID}` }))
      if (input.model) yield* getModel(ProviderID.make(input.model.providerID), ModelID.make(input.model.modelID), input.sessionID)
      yield* state.start(input.sessionID, agentID, lastAssistant(input.sessionID, agentID), managedRecoveryWork(input).pipe(Effect.orDie))
    })

    const loop: (input: z.infer<typeof LoopInput>, sourceCandidate?: UserSourceCandidate) => Effect.Effect<MessageV2.WithParts> = Effect.fn(
      "SessionPrompt.loop",
    )(function* (input: z.infer<typeof LoopInput>, sourceCandidate?: UserSourceCandidate) {
      const agentID = input.agentID ?? "main"
      yield* assertExecutionActive(input.sessionID, agentID)
      const work = runLoop(
        input.sessionID,
        agentID,
        input.task_id,
        input.notifyParentOnComplete,
        input.verifyGoalTarget,
        undefined,
        input.model ? { providerID: input.model.providerID, modelID: input.model.modelID } : undefined,
        undefined,
        input.afterTurnReady,
        sourceCandidate,
      )
      const managedWork = Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
        let executionRevision = input.executionRevision
        let ownsExecution = false
        let operationRelease: (() => Promise<void>) | undefined
        let executionIdentity: ActorExecutionIdentity | undefined = Option.getOrUndefined(
          yield* Effect.serviceOption(ActorExecutionContext),
        )
        const actor = yield* actorRegistry.get(input.sessionID, agentID)
        const runWithTailInputs = Effect.gen(function* () {
          let result = yield* restore(work).pipe(Effect.exit)
          while (Exit.isSuccess(result) && ManagedProvider.enabled()) {
            const authority = yield* Effect.tryPromise({
              try: () => ManagedProvider.readManagedSessionBinding(input.sessionID),
              catch: (cause) => new Error(`managed session binding unavailable: ${String(cause)}`),
            }).pipe(Effect.orDie)
            const local = yield* Effect.tryPromise({
              try: () => ManagedProvider.ensureManagedSessionBinding(input.sessionID),
              catch: (cause) => new Error(`managed session registration unavailable: ${String(cause)}`),
            }).pipe(Effect.orDie)
            if (authority.authority) break
            if (local?.authority) return yield* Effect.die(new Error("managed authority kind changed"))
            if (!local || local.transition || local.engineSessionID !== input.sessionID ||
              local.owner !== authority.owner || local.stableChatID !== authority.stableChatID ||
              local.mapRevision > authority.mapRevision || local.mappingRevision !== authority.mappingRevision ||
              local.bindingRevision !== authority.workspaceRevision)
              return yield* Effect.die(new Error("managed host binding changed or is unsettled"))
            const binding: UserInput.Binding = {
              owner: authority.owner,
              stableChatID: authority.stableChatID,
              mapRevision: authority.mapRevision,
              mappingRevision: authority.mappingRevision,
              workspaceRevision: authority.workspaceRevision,
            }
            if (!UserInput.hasDeliverable(input.sessionID, agentID, binding)) break
            result = yield* restore(runLoop(
              input.sessionID,
              agentID,
              input.task_id,
              input.notifyParentOnComplete,
              input.verifyGoalTarget,
              undefined,
              input.model ? { providerID: input.model.providerID, modelID: input.model.modelID } : undefined,
              undefined,
              true,
            )).pipe(Effect.exit)
          }
          if (Exit.isFailure(result)) return yield* Effect.failCause(result.cause).pipe(Effect.orDie)
          return result.value
        })
        if (!actor) return yield* restore(runWithTailInputs)
        if (input.manageExecution !== false && executionRevision === undefined) {
          let preflightRoute: import("@/acp/openclank-protocol").ActorRoutePreflightResult | undefined
          let preflightModel: { providerID: string; modelID: string } | undefined
          let hostContext: ManagedProvider.ManagedHostContext | undefined
          if (agentID === "main" && ManagedProvider.enabled()) {
            const messages = yield* sessions.messages({ sessionID: input.sessionID, agentID })
            const latestUser = messages.findLast((message) => message.info.role === "user")
            const rootPart = latestUser?.parts.find((part): part is MessageV2.TextPart =>
              part.type === "text" && typeof part.metadata?.hostSourceMessageID === "string",
            )
            const rootOperationID = rootPart?.metadata?.hostSourceMessageID as string | undefined
            if (!rootOperationID) return yield* Effect.die(new Error("managed actor root operation is unavailable"))
            const model = input.model ?? actor?.effectiveModel
            if (!model || !latestUser || latestUser.info.role !== "user" ||
                latestUser.info.model.providerID !== model.providerID || latestUser.info.model.modelID !== model.modelID)
              return yield* Effect.die(new Error("managed actor model differs from the current persisted user prompt"))
            preflightModel = { providerID: model.providerID, modelID: model.modelID }
            if (!actorRegistry.prepareMainPreflightModel ||
                !(yield* actorRegistry.prepareMainPreflightModel(input.sessionID, actor.executionRevision, preflightModel)))
              return yield* Effect.die(new Error("managed actor model changed before route preflight"))
            preflightRoute = yield* restore(Effect.tryPromise({
              try: () => ManagedProvider.preflightActorRoute({
                sessionID: input.sessionID,
                rootOperationID,
                actorID: agentID,
                expectedExecutionRevision: actor.executionRevision,
                model: { providerID: model.providerID, modelID: model.modelID },
                modelSource: "explicit",
                ...(typeof rootPart?.metadata?.grantID === "string" ? { grantID: rootPart.metadata.grantID } : {}),
                ...(typeof rootPart?.metadata?.preferredAccountID === "string" ? { preferredAccountID: rootPart.metadata.preferredAccountID } : {}),
              }),
              catch: (cause) => new Error(`managed actor route preflight failed: ${String(cause)}`),
            }).pipe(Effect.orDie))
            const selectedSnapshot = preflightRoute.modelRequestOptions
            if (selectedSnapshot && (!latestUser || latestUser.info.role !== "user" ||
              selectedSnapshot.model.providerID !== model.providerID || selectedSnapshot.model.modelID !== model.modelID ||
              (selectedSnapshot.options.reasoning_effort ?? null) !== (latestUser.info.model.variant ?? null)))
              return yield* Effect.die(new Error("managed main model options do not match the admitted prompt"))
            if ((typeof rootPart?.metadata?.modelRouteID === "string" && rootPart.metadata.modelRouteID !== preflightRoute.modelRouteID) ||
              (typeof rootPart?.metadata?.connectionID === "string" && rootPart.metadata.connectionID !== preflightRoute.connectionID) ||
              (typeof rootPart?.metadata?.grantID === "string" && rootPart.metadata.grantID !== preflightRoute.grantID))
              return yield* Effect.die(new Error("managed actor route changed from the admitted prompt"))
            let originalRootMatched=false
            const originalContext = (() => {
              try {
                const delivery = ManagedProvider.currentOriginalPromptHostContext(input.sessionID,rootOperationID)
                if (delivery !== null) {
                  originalRootMatched = !!delivery && delivery.chatID === preflightRoute.bootstrap.stableChatID
                  return originalRootMatched ? delivery : undefined
                }
                // Existing command path has a real session operation and its original root check.
                originalRootMatched = ManagedProvider.currentRootOperationID(input.sessionID) === rootOperationID
                return originalRootMatched ? ManagedProvider.currentHostContext(input.sessionID) : undefined
              } catch { return undefined }
            })()
            const originalOwner = originalContext?.ownerAccountID
            hostContext = {
              ...(originalContext?.gcfAdmission && originalContext.gcfArtifact ? {gcfAdmission:originalContext.gcfAdmission} : {}),
              ...(originalContext?.shellRewriteAdmission ? {shellRewriteAdmission:originalContext.shellRewriteAdmission} : {}),
              ...(originalContext?.gcfArgumentAdmission && originalContext.gcfArtifact ? {gcfArgumentAdmission:originalContext.gcfArgumentAdmission} : {}),
              ...(originalContext?.gcfArtifact ? {gcfArtifact:originalContext.gcfArtifact} : {}),
              ...(originalContext?.jsonWhitespaceAdmission ? {jsonWhitespaceAdmission:originalContext.jsonWhitespaceAdmission} : {}),
              ...(originalContext?.toolViewAdmission ? {toolViewAdmission:originalContext.toolViewAdmission} : {}),
              ...(originalContext?.adaptiveStyleAdmission ? {adaptiveStyleAdmission:originalContext.adaptiveStyleAdmission} : {}),
              chatID: preflightRoute.bootstrap.stableChatID,
              ...(originalOwner ? { ownerAccountID: originalOwner } : {}),
              ...(actor.hostContext?.goalID ? { goalID: actor.hostContext.goalID } : {}),
            }
            try { log.debug("source_owner_activation_input",{root:originalRootMatched,context:!!originalContext,ownerCopied:!!originalOwner,ownerPassed:!!hostContext.ownerAccountID}) } catch { /* diagnostic only */ }
            executionIdentity = {
              sessionID: input.sessionID,
              actorID: agentID,
              executionRevision: actor?.executionRevision ?? 0,
              rootOperationID,
              preflightRoute,
            }
          }
          executionRevision = yield* actorRegistry.beginExecution(
            input.sessionID,
            agentID,
            undefined,
            actor.executionRevision,
            { ...(input.actorAgent ? { agent: input.actorAgent } : {}),
              ...(preflightModel ? { effectiveModel: preflightModel, expectedEffectiveModel: preflightModel }
                : input.model ? { effectiveModel: input.model } : {}) },
          )
          if (executionRevision === undefined) return yield* Effect.die(new Session.BusyError(input.sessionID))
          ownsExecution = true
          if (preflightRoute) {
            const identity: ActorExecutionIdentity = {
              sessionID: input.sessionID,
              actorID: agentID,
              executionRevision,
              rootOperationID: executionIdentity?.rootOperationID,
              preflightRoute,
              retirement: { unknown: false },
            }
            const activated = yield* restore(Effect.tryPromise({
              try: () => ManagedProvider.activateActorOperation(
                identity,
                preflightRoute!,
                input.model ?? actor?.effectiveModel ?? { providerID: preflightRoute.providerID, modelID: preflightRoute.modelID },
                hostContext ?? {},
              ),
              catch: (cause) => new Error(`managed actor activation failed: ${String(cause)}`),
            }).pipe(Effect.exit))
            if (Exit.isFailure(activated)) {
              yield* actorRegistry.settleExecution(
                input.sessionID,
                agentID,
                executionRevision,
                "failure",
                Cause.pretty(activated.cause),
              ).pipe(Effect.ignore)
              return yield* Effect.failCause(activated.cause).pipe(Effect.orDie)
            }
            try { log.debug("source_owner_activation_result",{accepted:true,ownerPassed:!!hostContext?.ownerAccountID}) } catch { /* diagnostic only */ }
            operationRelease = activated.value.release
            executionIdentity = identity
          } else if (executionIdentity) {
            executionIdentity = { ...executionIdentity, executionRevision }
          }
        }
        const run = executionIdentity
          ? runWithTailInputs.pipe(Effect.provideService(ActorExecutionContext, executionIdentity))
          : runWithTailInputs
        const result = yield* restore(run).pipe(Effect.exit)
        if (ownsExecution && executionRevision !== undefined) {
          const outcome = Exit.isSuccess(result) ? "success" : Cause.hasInterruptsOnly(result.cause) ? "cancelled" : "failure"
          if (executionIdentity?.retirement?.unknown)
            yield* actorRegistry.markExecutionUncertain(input.sessionID, agentID, executionRevision, { rootOperationID: executionIdentity.rootOperationID!, rootSessionID: executionIdentity.preflightRoute?.bootstrap.rootSessionID, innerTurnID: executionIdentity.retirement.lastInnerTurnID, turnOutcome: executionIdentity.retirement.lastTurnOutcome }).pipe(Effect.ignore)
          const settled = executionIdentity?.retirement?.unknown ? false : yield* actorRegistry.settleExecution(
            input.sessionID,
            agentID,
            executionRevision,
            outcome,
            Exit.isFailure(result) ? Cause.pretty(result.cause) : undefined,
            executionIdentity?.rootOperationID ? { rootOperationID: executionIdentity.rootOperationID, innerTurnID: executionIdentity.retirement?.lastInnerTurnID } : undefined,
          ).pipe(Effect.orElseSucceed(() => false))
          if (settled && executionIdentity?.rootOperationID && !executionIdentity.retirement?.unknown) {
            const settlement = { rootOperationID: executionIdentity.rootOperationID, innerTurnID: executionIdentity.retirement?.lastInnerTurnID }
            const finished = yield* Effect.tryPromise({
              try: () => ManagedProvider.finishActorExecution({
                sessionID: input.sessionID, actorID: agentID, executionRevision,
                rootOperationID: executionIdentity!.rootOperationID!,
              }, outcome),
              catch: (cause) => new Error(`managed execution retirement failed: ${String(cause)}`),
            }).pipe(Effect.exit)
            if (Exit.isSuccess(finished) && finished.value.accepted)
              yield* actorRegistry.clearManagedSettlement(input.sessionID, agentID, executionRevision, settlement).pipe(Effect.ignore)
          }
        }
        if (operationRelease) {
          if (executionIdentity) {
            const persisted = yield* persistManagedActorCapture(sessions, executionIdentity).pipe(Effect.orElseSucceed(() => false))
            if (!persisted) yield* Effect.sync(() => log.warn("managed execution capture was not persisted", { sessionID: input.sessionID, actorID: agentID, executionRevision }))
          }
          yield* Effect.promise(operationRelease).pipe(Effect.orDie)
          operationRelease = undefined
        }
        if (Exit.isFailure(result)) return yield* Effect.failCause(result.cause).pipe(Effect.orDie)
        return result.value
      }))
        return yield* state.ensureRunning(
        input.sessionID,
        agentID,
        lastAssistant(input.sessionID, agentID),
        managedWork,
        )
    })

    const shell: (input: ShellInput) => Effect.Effect<MessageV2.WithParts> = Effect.fn("SessionPrompt.shell")(
      function* (input: ShellInput) {
        yield* assertExecutionActive(input.sessionID)
        return yield* state.startShell(input.sessionID, lastAssistant(input.sessionID), shellImpl(input))
      },
    )

    const command = Effect.fn("SessionPrompt.command")(function* (input: CommandInput) {
      yield* assertExecutionActive(input.sessionID)
      yield* elog.info("command", { sessionID: input.sessionID, command: input.command, agent: input.agent })
      const cmd = yield* commands.get(input.command)
      if (!cmd) {
        const available = (yield* commands.list()).map((c) => c.name)
        const hint = available.length ? ` Available commands: ${available.join(", ")}` : ""
        const error = new NamedError.Unknown({ message: `Command not found: "${input.command}".${hint}` })
        yield* bus.publish(Session.Event.Error, { sessionID: input.sessionID, error: error.toObject() })
        throw error
      }
      // Validate the actual user-turn overrides before rendering templates or
      // calling prompt(). A delegated command may have a valid task agent/model
      // while its caller supplies an invalid actor; that input must have no
      // message or title side effects.
      const requestedAgentName = input.agent ?? cmd.agent ?? (yield* agents.defaultAgent())
      const requestedAgent = yield* agents.get(requestedAgentName)
      if (!requestedAgent) {
        const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
        const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
        throw new NamedError.Unknown({ message: `Agent not found: "${requestedAgentName}".${hint}` })
      }
      if (input.model) {
        const requestedModel = Provider.parseModel(input.model)
        yield* getModel(requestedModel.providerID, requestedModel.modelID, input.sessionID)
      }
      const agentName = cmd.agent ?? input.agent ?? (yield* agents.defaultAgent())

      // /goal — set or clear a session-level stop-condition goal. The condition
      // text itself becomes the prompt for this turn (the working agent starts
      // pursuing it immediately); the main runLoop then refuses to stop until
      // the judge says it's satisfied. See session/goal.ts.
      if (input.command === Command.Default.GOAL) {
        const condition = input.arguments.trim()
        const notice = (text: string) =>
          prompt({
            sessionID: input.sessionID,
            messageID: input.messageID,
            agent: agentName,
            parts: [{ type: "text", text, synthetic: true }],
            noReply: true,
          })

        if (condition === "" || condition === "clear" || condition === "reset" || condition === "cancel") {
          const current = yield* goal.get(input.sessionID)
          if (!current) return yield* notice("No active goal.")
          yield* goal.clear(input.sessionID, Goal.target(current))
          return yield* notice("Goal cancelled.")
        }
        if (condition === "pause") {
          const current = yield* goal.get(input.sessionID)
          if (!current) return yield* notice("No active goal.")
          const paused = yield* goal.pause(input.sessionID, Goal.target(current))
          return yield* notice(`Goal paused at revision ${paused.revision}.`)
        }
        if (condition === "resume") {
          const current = yield* goal.get(input.sessionID)
          if (!current) return yield* notice("No resumable goal.")
          const resumed = yield* goal.resume(input.sessionID, Goal.target(current))
          return yield* notice(`Goal resumed at revision ${resumed.revision}.`)
        }
        if (condition === "status" || condition === "list") {
          const state = yield* goal.inspect(input.sessionID)
          const analytics = yield* goal.analytics(input.sessionID)
          return yield* notice(GoalState.summaryLines(state, analytics).join("\n"))
        }
        if (condition === "clear-history") {
          const state = yield* goal.inspect(input.sessionID)
          yield* goal.clearHistory(input.sessionID, state.revision)
          return yield* notice("Completed goal history cleared.")
        }
        if (condition.startsWith("edit ")) {
          const match = /^edit\s+(\d+)\s+([\s\S]+)$/.exec(condition)
          if (!match) return yield* notice("Usage: /goal edit <revision> <new objective>")
          const current = yield* goal.get(input.sessionID)
          if (!current) return yield* notice("No active goal.")
          const edited = yield* goal.edit(
            input.sessionID,
            { goalID: current.id, expectedRevision: Number(match[1]) },
            match[2],
          )
          return yield* notice(`Goal updated to revision ${edited.revision}.`)
        }
        const verify = /^(?:verify|verify-now)(?:\s+(\S+)\s+(\d+))?$/.exec(condition)
        if (verify) {
          const current = yield* goal.get(input.sessionID)
          if (!current) return yield* notice("No active goal.")
          const requested = verify[1]
            ? {
                goalID: verify[1],
                expectedRevision: Number(verify[2]),
              }
            : Goal.target(current)
          if (current.id !== requested.goalID || current.revision !== requested.expectedRevision) {
            throw new Error("Goal target is stale; reload before verifying")
          }
          if (current.status === "paused" || current.status === "blocked") {
            return yield* notice(`Goal is ${current.status}; resume it before verification.`)
          }
          return yield* prompt({
            sessionID: input.sessionID,
            messageID: input.messageID,
            agent: agentName,
            parts: [{ type: "text", text: "Verify the active goal now.", synthetic: true }],
            verifyGoalTarget: requested,
          })
        }
        if (condition.startsWith("verify ") || condition.startsWith("verify-now ")) {
          return yield* notice("Usage: /goal verify [goal-id revision]")
        }

        const created = yield* goal.set(input.sessionID, condition)
        if (created.queued) {
          return yield* notice(`Goal queued (${created.goal.id}). It will start after the active goal finishes.`)
        }
      }

      // /rebuild — manually rebuild the conversation context now, from the
      // latest checkpoint. Reuses the SAME rebuildEnsuringCheckpoint step as the
      // automatic overflow path (identical logic + boundary conditions), so a
      // user-triggered rebuild behaves exactly like an auto one: it inserts a
      // checkpoint boundary at the watermark (recent messages after it are kept
      // verbatim; earlier ones collapse to the checkpoint summary on the next
      // turn). On a cold session it starts a writer and waits (bounded) rather
      // than silently doing nothing. Outcomes are reported to the user.
      if (input.command === Command.Default.REBUILD) {
        const msgs = yield* sessions.messages({ sessionID: input.sessionID, agentID: "main" })
        const lastUser = msgs.findLast((m) => m.info.role === "user")
        const model = yield* lastModel(input.sessionID)
        const attempt: RebuildAttempt = yield* rebuildEnsuringCheckpoint({
          sessionID: input.sessionID,
          msgs,
          agentID: lastUser?.info.agentID ?? "main",
          agent: agentName,
          model: { providerID: model.providerID, id: model.modelID },
          writerWaitMs: MANUAL_WRITER_WAIT_MS,
          onWaitingForWriter: status
            .set(input.sessionID, { type: "busy", message: "Writing checkpoint\u2026" })
            .pipe(Effect.catch(() => Effect.void)),
        }).pipe(Effect.catch(() => Effect.succeed("insert-failed" as const)))
        if (attempt === "writer-failed" || attempt === "memory-write-off") {
          yield* compaction
            .create({
              sessionID: input.sessionID,
              agent: agentName,
              model: { providerID: model.providerID, modelID: model.modelID },
              auto: true,
              agentID: lastUser?.info.agentID ?? "main",
            })
            .pipe(Effect.ignore)
        }
        const text =
          attempt === "rebuilt"
            ? "Context rebuilt from the latest checkpoint. Recent messages are preserved; earlier context is now summarized."
            : attempt === "insert-failed"
              ? "A checkpoint was written but the context could not be rebuilt from it. Context is unchanged and nothing was compacted — retry /rebuild, or report this if it repeats."
              : "No checkpoint could be written (the checkpoint writer failed), so the context was compacted instead — earlier messages were dropped rather than rebuilt from a checkpoint."
        return yield* prompt({
          sessionID: input.sessionID,
          messageID: input.messageID,
          agent: agentName,
          parts: [
            {
              type: "text",
              text,
              synthetic: true,
            },
          ],
          noReply: true,
        })
      }

      const raw = input.arguments.match(argsRegex) ?? []
      const args = raw.map((arg) => arg.replace(quoteTrimRegex, ""))
      const templateCommand = yield* Effect.promise(async () => cmd.template)

      let template: string
      if (cmd.source === "skill") {
        template = input.arguments
      } else {
        const placeholders = templateCommand.match(placeholderRegex) ?? []
        let last = 0
        for (const item of placeholders) {
          const value = Number(item.slice(1))
          if (value > last) last = value
        }

        const withArgs = templateCommand.replaceAll(placeholderRegex, (_, index) => {
          const position = Number(index)
          const argIndex = position - 1
          if (argIndex >= args.length) return ""
          if (position === last) return args.slice(argIndex).join(" ")
          return args[argIndex]
        })
        const usesArgumentsPlaceholder = templateCommand.includes("$ARGUMENTS")
        template = withArgs.replaceAll("$ARGUMENTS", input.arguments)

        if (placeholders.length === 0 && !usesArgumentsPlaceholder && input.arguments.trim()) {
          template = template + "\n\n" + input.arguments
        }
      }

      const shellMatches = ConfigMarkdown.shell(template)
      if (shellMatches.length > 0) {
        const sh = Shell.preferred()
        const results = yield* Effect.promise(() =>
          Promise.all(
            shellMatches.map(async ([, cmd]) => (await Process.text([cmd], { shell: sh, nothrow: true })).text),
          ),
        )
        let index = 0
        template = template.replace(bashRegex, () => results[index++])
      }
      template = template.trim()

      const taskModel = yield* resolveCommandTaskModel(
        cmd,
        input.model ? Provider.parseModel(input.model) : undefined,
        () => lastModel(input.sessionID),
        { provider, agents },
      )

      yield* getModel(taskModel.providerID, taskModel.modelID, input.sessionID)

      const agent = yield* agents.get(agentName)
      if (!agent) {
        const available = (yield* agents.list()).filter((a) => !a.hidden).map((a) => a.name)
        const hint = available.length ? ` Available agents: ${available.join(", ")}` : ""
        const error = new NamedError.Unknown({ message: `Agent not found: "${agentName}".${hint}` })
        yield* bus.publish(Session.Event.Error, { sessionID: input.sessionID, error: error.toObject() })
        throw error
      }

      const templateParts = yield* resolvePromptParts(template)
      const isSubtask = (agent.mode === "subagent" && cmd.subtask !== false) || cmd.subtask === true

      let parts: PromptInput["parts"]
      if (isSubtask) {
        const promptText =
          cmd.source === "skill"
            ? templateCommand + (input.arguments.trim() ? "\n\n" + input.arguments : "")
            : (templateParts.find((y): y is typeof y & { type: "text"; text: string } => y.type === "text")?.text ?? "")
        parts = [
          {
            type: "subtask" as const,
            agent: agent.name,
            description: cmd.description ?? "",
            command: input.command,
            model: { providerID: taskModel.providerID, modelID: taskModel.modelID },
            prompt: promptText,
          },
        ]
      } else if (cmd.source === "skill") {
        // Body injection belongs to the mention scan in insertReminders, which keys off this leading token.
        const visibleText = input.arguments.trim() ? `/${input.command} ${input.arguments}` : `/${input.command}`
        const attachments = templateParts.filter((p): p is Exclude<typeof p, { type: "text" }> => p.type !== "text")
        parts = [{ type: "text" as const, text: visibleText }, ...attachments, ...(input.parts ?? [])]
      } else {
        parts = [...templateParts, ...(input.parts ?? [])]
      }

      const userAgent = isSubtask ? (input.agent ?? (yield* agents.defaultAgent())) : agentName
      const userModel = isSubtask
        ? input.model
          ? Provider.parseModel(input.model)
          : yield* lastModel(input.sessionID)
        : taskModel

      yield* plugin.trigger(
        "command.execute.before",
        { command: input.command, sessionID: input.sessionID, arguments: input.arguments },
        { parts },
      )

      const result = yield* prompt({
        sessionID: input.sessionID,
        messageID: input.messageID,
        command: input.command,
        arguments: input.arguments,
        model: userModel,
        agent: userAgent,
        parts,
        variant: input.variant,
      })
      yield* bus.publish(Command.Event.Executed, {
        name: input.command,
        sessionID: input.sessionID,
        arguments: input.arguments,
        messageID: result.info.id,
      })
      return result
    })

    const impl = Service.of({
      cancel,
      prompt,
      recovery,
      resume,
      resumeBackground,
      loop,
      shell,
      command,
      resolvePromptParts,
      sweepOrphanAssistants,
      predict,
    })
    sessionPromptRef.current = { loop: impl.loop }
    // Expose the project default-model resolver to Inbox.drain's option-2
    // fallback (seed a synthetic message for a turnCount-0 standing peer whose
    // slice has no model-bearing message yet). Reads Provider, which is already
    // in scope here — Inbox.layer stays free of a Provider dependency.
    const defaultModelResolver = { defaultModel: () => provider.defaultModel() }
    defaultModelRef.current = defaultModelResolver
    yield* Effect.addFinalizer(() =>
      Effect.sync(() => {
        if (sessionPromptRef.current?.loop === impl.loop) sessionPromptRef.current = undefined
        if (defaultModelRef.current === defaultModelResolver) defaultModelRef.current = undefined
      }),
    )
    return impl
  }),
)

/**
 * Application composition variant. Leaves `MCP.Service` unmet so the root
 * AppLayer chain (Actor.appLayer ← SessionPrompt.appLayer ← Command.appLayer ←
 * MCP.defaultLayer) supplies one client set. Provides Command.appLayer rather
 * than Command.defaultLayer so this layer cannot smuggle its own MCP in.
 */
export const appLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(SessionRunState.defaultLayer),
    Layer.provide(SessionStatus.defaultLayer),
    Layer.provide(SessionPrune.defaultLayer),
    Layer.provide(SessionCheckpoint.defaultLayer),
    Layer.provide(SessionCompaction.defaultLayer),
    Layer.provide(SessionProcessor.defaultLayer),
    Layer.provide(Command.appLayer),
    Layer.provide(Permission.defaultLayer),
    Layer.provide(LSP.defaultLayer),
    Layer.provide(ToolRegistry.defaultLayer),
    Layer.provide(Truncate.defaultLayer),
    Layer.provide(Provider.defaultLayer),
    Layer.provide(Instruction.defaultLayer),
    Layer.provide(AppFileSystem.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(Session.defaultLayer),
    Layer.provide(SessionRevert.defaultLayer),
    Layer.provide(
      Layer.mergeAll(
        Config.defaultLayer,
        SessionSummary.defaultLayer,
        Team.defaultLayer,
        ActorRegistry.defaultLayer,
        Agent.defaultLayer,
        SystemPrompt.defaultLayer,
        LLM.defaultLayer,
        Bus.layer,
        CrossSpawnSpawner.defaultLayer,
        Inbox.defaultLayer,
        Goal.defaultLayer,
        TaskRegistry.defaultLayer,
      ),
    ),
  ),
)

export const defaultLayer = Layer.suspend(() => appLayer.pipe(Layer.provide(MCP.defaultLayer)))
/**
 * Returns true when at least one resolved user-message part carries substantive
 * content that will survive the send-side filter (message-v2.ts).  Used by
 * createUserMessage to reject empty-content messages before they are persisted.
 */
export function hasSubstantiveContent(parts: readonly MessageV2.Part[]): boolean {
  return parts.some((p) => {
    if (p.type === "text" && !p.ignored && p.text.trim().length > 0) return true
    if (p.type === "file" && p.mime !== "text/plain" && p.mime !== "application/x-directory") return true
    // checkpoint / compaction / subtask parts always produce text at send time
    if (p.type === "checkpoint" || p.type === "compaction" || p.type === "subtask") return true
    return false
  })
}

export const PromptInput = z.object({
  sessionID: SessionID.zod,
  messageID: MessageID.zod.optional(),
  model: z
    .object({
      providerID: ProviderID.zod,
      modelID: ModelID.zod,
    })
    .optional(),
  modelRef: z
    .string()
    .optional()
    .describe(
      "Model group/tier name (e.g. ultra/standard/lite) or a literal provider/model. Resolved provider-aware. Takes precedence over `model` when both are set.",
    ),
  agent: z.string().optional(),
  agentID: z.string().optional(),
  task_id: z
    .string()
    .optional()
    .describe(
      "If the spawning caller bound this prompt to a specific user-task (T4 etc), pass its TID. Propagates to Tool.Context.taskId so memory-path-guard allows writes to tasks/<task_id>/*.md.",
    ),
  source: z.enum(["user", "spawn", "hook"]).optional(),
  command: z.string().optional(),
  arguments: z.string().optional(),
  titleLocale: z.string().optional(),
  provenance: MessageV2.Provenance.optional(),
  noReply: z.boolean().optional(),
  verifyGoalTarget: Goal.Target.optional(),
  tools: z
    .record(z.string(), z.boolean())
    .optional()
    .describe("@deprecated tools and permissions have been merged, you can set permissions on the session itself now"),
  format: MessageV2.Format.optional(),
  system: z.string().optional(),
  variant: z.string().nullable().optional(),
  parts: z
    .array(
      z.discriminatedUnion("type", [
        MessageV2.TextPart.omit({
          messageID: true,
          sessionID: true,
        })
          .partial({
            id: true,
          })
          .meta({
            ref: "TextPartInput",
          }),
        MessageV2.FilePart.omit({
          messageID: true,
          sessionID: true,
        })
          .partial({
            id: true,
          })
          .meta({
            ref: "FilePartInput",
          }),
        MessageV2.AgentPart.omit({
          messageID: true,
          sessionID: true,
        })
          .partial({
            id: true,
          })
          .meta({
            ref: "AgentPartInput",
          }),
        MessageV2.SubtaskPart.omit({
          messageID: true,
          sessionID: true,
        })
          .partial({
            id: true,
          })
          .meta({
            ref: "SubtaskPartInput",
          }),
      ]),
    )
    .min(1, "parts must contain at least one element"),
}).strict()
export type PromptInput = z.infer<typeof PromptInput>

export const LoopInput = z.object({
  sessionID: SessionID.zod,
  agentID: z.string().optional(),
  model: z.object({ providerID: ProviderID.zod, modelID: ModelID.zod }).optional(),
  afterTurnReady: z.boolean().optional(),
  actorAgent: z.string().optional(),
  manageExecution: z.boolean().optional(),
  executionRevision: z.number().int().nonnegative().optional(),
  task_id: z.string().optional(),
  // Set by the inbox wake path so a persistent background peer that finishes a
  // woken turn notifies its parent (mirroring forkWork.notify, which only wraps
  // the FIRST/spawn turn). Left false on spawn/user-driven loops to avoid
  // double-notifying the spawn turn that forkWork already covers.
  notifyParentOnComplete: z.boolean().optional(),
  verifyGoalTarget: Goal.Target.optional(),
})

export const ShellInput = z.object({
  sessionID: SessionID.zod,
  messageID: MessageID.zod.optional(),
  agent: z.string(),
  model: z
    .object({
      providerID: ProviderID.zod,
      modelID: ModelID.zod,
    })
    .optional(),
  modelRef: z
    .string()
    .optional()
    .describe(
      "Model group/tier name (e.g. ultra/standard/lite) or a literal provider/model. Resolved provider-aware. Takes precedence over `model` when both are set.",
    ),
  command: z.string(),
})
export type ShellInput = z.infer<typeof ShellInput>

export const CommandInput = z.object({
  messageID: MessageID.zod.optional(),
  sessionID: SessionID.zod,
  agent: z.string().optional(),
  model: z.string().optional(),
  arguments: z.string(),
  command: z.string(),
  variant: z.string().optional(),
  parts: z
    .array(
      z.discriminatedUnion("type", [
        MessageV2.TextPart.omit({
          messageID: true,
          sessionID: true,
        }).partial({
          id: true,
        }),
        MessageV2.FilePart.omit({
          messageID: true,
          sessionID: true,
        }).partial({
          id: true,
        }),
      ]),
    )
    .optional(),
})
export type CommandInput = z.infer<typeof CommandInput>

/** @internal Exported for testing */
export function createStructuredOutputTool(input: {
  schema: Record<string, any>
  onSuccess: (output: unknown) => void
}): AITool {
  // Remove $schema property if present (not needed for tool input)
  const { $schema: _, ...toolSchema } = input.schema

  return tool({
    description: STRUCTURED_OUTPUT_DESCRIPTION,
    inputSchema: jsonSchema(toolSchema as JSONSchema7),
    async execute(args) {
      // AI SDK validates args against inputSchema before calling execute()
      input.onSuccess(args)
      return {
        output: "Structured output captured successfully.",
        title: "Structured Output",
        metadata: { valid: true },
      }
    },
    toModelOutput({ output }) {
      return {
        type: "text",
        value: output.output,
      }
    },
  })
}
const bashRegex = /!`([^`]+)`/g
// Match [Image N] as single token, quoted strings, or non-space sequences
const argsRegex = /(?:\[Image\s+\d+\]|"[^"]*"|'[^']*'|[^\s"']+)/gi
const placeholderRegex = /\$(\d+)/g
const quoteTrimRegex = /^["']|["']$/g

/**
 * Fire seam for scheduled prompts (T18, spec [S5]).
 *
 * Funnels a cron/loop fire through the SAME entry point typed user prompts use:
 * `SessionPrompt.Service.prompt`. The synthetic part carries `synthetic: true`
 * (mimocode convention for `isMeta`) so transcript-preview surfaces can hide it,
 * and `metadata.origin = { kind: "cron", taskId, kindOfTask }` so the TUI can
 * render a clock icon. Sentinel expansion is intentionally NOT done here — T19
 * will wrap `value` before this call.
 */
export type ScheduledPromptOrigin = {
  kind: "cron"
  taskId: string
  kindOfTask: "cron" | "loop"
  /**
   * ISO-8601 timestamp of when the scheduler tick fired this task. Set by the
   * cron bridge in `onFire`; persisted on the synthetic part's metadata so the
   * TUI and downstream consumers can recover fire time without parsing the
   * prepended text prefix.
   */
  firedAt?: string
}

export type InjectScheduledPromptInput = {
  sessionID: SessionID
  value: string
  origin: ScheduledPromptOrigin
  priority?: "later" | "next" | "now"
  isMeta?: boolean
}

export const injectScheduledPrompt = (input: InjectScheduledPromptInput) =>
  Effect.gen(function* () {
    const sp = yield* Service
    yield* Effect.asVoid(
      sp.prompt({
        sessionID: input.sessionID,
        source: "hook",
        parts: [
          {
            type: "text",
            text: input.value,
            synthetic: input.isMeta ?? true,
            metadata: {
              origin: input.origin,
              priority: input.priority ?? "later",
            },
          },
        ],
      }),
    )
  })

export * as SessionPrompt from "./prompt"
