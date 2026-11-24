import { Instruction } from "./instruction"
import { settleSourceStreamBeforeTeardown } from "./closed-source-provenance"
import { captureFinalTools } from "./tool-definition-provenance"
import { selectAnnotationView } from "./tool-schema-annotations"
import { isPinnedAnthropicModel } from "@/provider/anthropic-origin"
import { JSONParseError, TypeValidationError } from "@ai-sdk/provider"
import { isPinnedResponsesModel, isPinnedChatModel, pinnedCompatibleChatEndpoint } from "@/provider/malformed-origin"
import { observeAnthropicDoStream, drainAnthropicCallbacks, withAnthropicFailureProbe, issueAnthropicCallback } from "@/provider/fallback-terminal-witness"
import { sourceAttemptLayout } from "./closed-source-provenance"
import { sourceLayoutMessages } from "./source-layout"
import { nativeSingleUserWire, nativeMembershipFrame, sameNativeMembershipIdentity, maintenanceCompactionWire, recallToolFrame, contextPrefaceFrame, standingGuidanceFrame, type NativeMembershipCapture, type NativeMembershipDelivery, type NativeMembershipFrame, type StandingGuidanceEvidence, type MaintenanceWireCandidate, type ContextPrefaceEvidence, type NativeTextDelivery, type NativeTextEvidence, type RecallToolEvidence } from "./context-inspection"
import { contextFrame, type ContextFrame, type ContextSource } from "./context-inspection"
import type { ActorExecutionIdentity } from "@/actor/turn"
import path from "path"
import { createHash } from "node:crypto"
import { Provider } from "@/provider"
import { Log } from "@/util"
import { Context, Duration, Effect, Layer, Record, Schedule, Ref, Cause, Exit } from "effect"
import * as Stream from "effect/Stream"
import { streamText, wrapLanguageModel, type ModelMessage, type Tool, tool, jsonSchema } from "ai"
import { mergeDeep, pipe } from "remeda"
import { GitLabWorkflowLanguageModel } from "gitlab-ai-provider"
import { ProviderTransform } from "@/provider"
import { Config } from "@/config"
import { Instance } from "@/project/instance"
import { Agent } from "@/agent/agent"
import type { MessageV2 } from "./message-v2"
import { Plugin } from "@/plugin"
import { SystemPrompt } from "./system"
import { Permission } from "@/permission"
import { PermissionID } from "@/permission/schema"
import { Bus } from "@/bus"
import { Wildcard, ToolCompat } from "@/util"
import { asSchema } from "@ai-sdk/provider-utils"
import { SessionID } from "@/session/schema"
import * as Session from "@/session/session"
import { assertCurrentProjectMemory } from "./checkpoint-paths"
import { ProjectID } from "@/project/schema"
import { Auth } from "@/auth"
import { Installation } from "@/installation"
import { InstallationVersion } from "@/installation/version"
import { EffectBridge } from "@/effect"
import { Global } from "@/global"
import * as Option from "effect/Option"
import * as OtelTracer from "@effect/opentelemetry/Tracer"
import { ActorRegistry } from "@/actor/registry"
import { Memory } from "@/memory"
import { isRetryableTransientError } from "./retry"
import { MCP_TOOL_SEARCH_ID } from "@/tool/mcp-tool-search"
import { ManagedProvider } from "@/acp/managed-provider"
import { withSdkDispatches, recordSdkFailure, markSdkSuccessStep, markMalformedResponse, retireCaptureAttempt } from "@/provider/logging-transport"
import { contextWindow, contextPressureLevel } from "./overflow"
import { readWindowOrigin } from "../provider/model-window-origin"
import { adaptiveSnapshot, adaptiveAdmissionSHA256, adaptiveModelWindowSHA256, ADAPTIVE_CONCISE_HINT } from "./adaptive-concision"

/** Final actual request-frame hook, also exercised without a model/provider.
 * UTF8 bytes conservatively upper-bound text tokens, never image/audio cost. */
export async function appendFinalCodeContext(input: {
  sessionID: string; messages: ModelMessage[]; system: string[]; options: Record<string, unknown>
  tools: Record<string, Tool>; activeTools: string[]; usable: number; abort: AbortSignal
  ephemeral?: boolean; agentID?: string; actorExecution?: ActorExecutionIdentity
  resolvedTools?: unknown; providerFrame?: boolean
}, read = ManagedProvider.readCodeContext): Promise<ModelMessage[]> {
  if (input.ephemeral || (input.agentID && input.agentID !== "main") || input.abort.aborted || !Number.isSafeInteger(input.usable) || input.usable <= 0 || input.messages.length > 512 || input.activeTools.length > 128) return input.messages
  // Unknown media costs must not be approximated by URL/string byte length.
  function textCostKnown(value: unknown, depth = 0): boolean {
    if (depth > 32 || value instanceof Uint8Array || value instanceof URL) return false
    if (!value || typeof value !== "object") return true
    if (Array.isArray(value)) return value.every((v) => textCostKnown(v, depth + 1))
    const row = value as Record<string, unknown>
    if (typeof row.type === "string" && /image|audio|video|file/.test(row.type)) return false
    return Object.values(row).every((v) => textCostKnown(v, depth + 1))
  }
  if (!textCostKnown(input.messages)) return input.messages
  try {
    const schemas: Record<string, unknown> = {}
    if (input.resolvedTools === undefined) for (const name of input.activeTools) {
      if (!input.tools[name]) return input.messages
      schemas[name] = { ...input.tools[name], inputSchema: await Promise.resolve(asSchema(input.tools[name].inputSchema).jsonSchema) }
    }
    const frame = () => JSON.stringify({ messages: input.messages, system: input.system, options: input.options, tools: input.resolvedTools ?? schemas })
    const initial = frame()
    const bytes = Buffer.byteLength(initial, "utf8")
    const available = Math.min(2048, input.usable - bytes - 1024)
    if (bytes > 1024 * 1024 || available < 768 || input.abort.aborted) return input.messages
    const latest = [...input.messages].reverse().find((m) => m.role === "user")
    const query = typeof latest?.content === "string" ? latest.content : Array.isArray(latest?.content) ? latest.content.filter((p) => p.type === "text").map((p) => "text" in p ? p.text : "").join("\n") : ""
    if (!query || Buffer.byteLength(query, "utf8") > 4096) return input.messages
    const fingerprint = createHash("sha256").update(initial).digest("hex")
    const card = await read({ sessionID: input.sessionID, query, availableByteTokens: available, requestFingerprint: fingerprint, retainedMessages: input.messages as unknown as Record<string, unknown>[] }, input.abort, input.actorExecution)
    if (!card || input.abort.aborted || frame() !== initial || !card.includes("[code-evidence-v1 ")) return input.messages
    const message: ModelMessage = { role: "user", content: input.providerFrame ? [{ type: "text", text: card }] : card }
    if (Buffer.byteLength(JSON.stringify(message), "utf8") + 1 > available) return input.messages
    const out = [...input.messages]
    const index = out.findLastIndex((m) => m.role === "user")
    out.splice(index < 0 ? out.length : index, 0, message)
    return out
  } catch {
    return input.messages
  }
}

const log = Log.create({ service: "llm" })
export const OUTPUT_TOKEN_MAX = ProviderTransform.OUTPUT_TOKEN_MAX
type Result = Awaited<ReturnType<typeof streamText>>

/** Host projection metadata is an internal routing contract, not an upstream
 * provider option.  Keep it available on the catalog model for adapter
 * selection, then remove it from the request option bag before the AI SDK
 * serializes providerOptions. */
export function stripManagedProjectionOptions(options: Record<string, any>): Record<string, any> {
  return Object.fromEntries(Object.entries(options).filter(([key]) => !key.startsWith("_openclank")))
}

/** Keep one-shot structured title generation compatible with DeepSeek's
 * non-thinking forced-tool API; primary chat retains its reasoning settings. */
export function applyEphemeralStructuredOptions(options: Record<string, any>, input: { ephemeral?: boolean; toolChoice?: string; model: Pick<Provider.Model, "providerID" | "api"> }, familyID?: string): void {
  if (input.ephemeral && input.toolChoice === "required" && input.model.api.npm === "@ai-sdk/openai-compatible" && (familyID === "deepseek" || input.model.providerID === "deepseek")) {
    options.thinking = { type: "disabled" }
    delete options.reasoningEffort
  }
}

/**
 * Match transient errors that the PERSISTENT_RETRY layer should retry.
 *
 * - HTTP 429 / 5xx / 529 — capacity / overload responses
 * - ECONNRESET / EPIPE / ETIMEDOUT — network errors typically caused by
 *   stale keep-alive sockets or upstream proxy timeouts
 * - "SSE read timed out" — `provider.ts:wrapSSE` chunk-timeout fired
 *   (configured per-provider via `chunkTimeout` in mimocode.json). This
 *   is HTTP-byte-level: keep-alive comments still count as activity, so
 *   the error only fires when the underlying TCP stream is genuinely dead.
 *
 * Auth errors (401/403), client errors (400, 404, 422), and user-
 * initiated aborts are NOT retryable.
 *
 * @deprecated Use `isRetryableTransientError` from `./retry` directly.
 * Kept as a 1-line wrapper to preserve the existing export name.
 */
export function isTransientCapacityError(error: unknown): boolean {
  return isRetryableTransientError(error)
}

/**
 * Persistent-retry schedule with exponential backoff.
 *
 * Exponential backoff 500ms × 2 (i.e. 0.5, 1, 2, 4, 8, 16, 32, 64, 128, 256s),
 * each individual delay capped at 5 minutes, total attempts capped at 10.
 *
 * Worst-case total = 11 attempts × chunkTimeout + cumulative backoff
 *                  ≈ 11 × 8min + 9min ≈ 97 min (with DEFAULT_CHUNK_TIMEOUT = 8min).
 *
 * Intentionally NOT capped via Schedule.upTo() — retry persistence under
 * brief upstream outages is the design goal. Bounding per-attempt latency
 * via chunkTimeout is the primary lever for hang-time control.
 */
export const persistentRetrySchedule = Schedule.exponential("500 millis", 2).pipe(
  Schedule.modifyDelay((_, delay) =>
    Effect.succeed(Duration.isLessThanOrEqualTo(delay, Duration.minutes(5)) ? delay : Duration.minutes(5)),
  ),
  Schedule.both(Schedule.recurs(10)),
)

/**
 * Memory-system instructions appended to the main agent's system prompt.
 *
 * Teaches the agent its v8.1 ownership of the memory system:
 * - MEMORY.md (project-scoped): writer is sole curator + agent edits for
 *   project-level user-stated rules
 * - checkpoint.md (session-scoped): writer EXCLUSIVE; agent never edits
 * - tasks/<id>/progress.md: writer-derived splitover from session-level
 *   progress.md; not LLM-written. Subagents handed a task may read but
 *   should not write.
 *
 * Also documents the Active recall protocol that prevents re-Reading
 * files already present in the rebuild dump, and the Subagent return
 * format contract.
 *
 * `memoryRoot` is the same absolute root returned by Memory.root(), so these
 * paths match the files used by checkpoint restore and memory/task detection.
 */
function buildManagedMemoryInstructions(): string {
  return "# Memory and operational continuity\nDurable knowledge is governed by current Memery policy and any advertised tools and receipts. Follow the user's and repository's agreements for plans and notes. Session checkpoints, task records, and notes support operational continuity."
}

function buildMemoryInstructions(sessionID: SessionID, projectID: ProjectID, memoryRoot: string): string {
  const memoryFile = path.join(memoryRoot, "projects", projectID, "MEMORY.md")
  const checkpointFile = path.join(memoryRoot, "sessions", sessionID, "checkpoint.md")
  const sessionMemoryDir = path.join(memoryRoot, "sessions", sessionID)
  const globalMemoryFile = path.join(memoryRoot, "global", "MEMORY.md")
  return `# Memory system

You have a persistent file-based memory system. Four file types:

- Project memory at \`${memoryFile}\` — persistent across all sessions in this project. Contains: project context, rules, architecture decisions, durable cross-task knowledge.
- Session checkpoint at \`${checkpointFile}\` — current session's structured state, written ONLY by the checkpoint-writer subagent. 11 sections covering active intent, next action, directives, task tree, current work, files, learnings, errors, live resources, design decisions, and open notes. Task content lives inside §4 Task tree and §5 Current work.
- Per-task progress at \`${path.join(sessionMemoryDir, "tasks", "<id>", "progress.md")}\` — writer-derived splitover from session-level progress.md (not LLM-written). When you spawn a subagent on a task, the subagent may be handed this path for reading; you do not maintain it.
- Global memory at \`${globalMemoryFile}\` — user-level preferences and cross-project feedback that persist across all projects. Auto-injected into rebuild context under the "## Global memory" header when present.

The checkpoint writer is the sole curator of the structured files. You don't maintain them mid-task — the writer extracts everything from the conversation at checkpoint events.

## When to Edit MEMORY.md directly

You may Edit MEMORY.md when:
- User states a project-level rule that should hold across sessions → ## Rules
- User states a project-level architectural decision → ## Architecture decisions
- A clearly durable cross-session fact emerges that you want available immediately, before the next checkpoint → ## Discovered durable knowledge

These are exceptions, not the norm. The writer covers most extraction at checkpoint time.

## Notes scratchpad

You have a single legal scratchpad at \`${path.join(sessionMemoryDir, "notes.md")}\`. Append entries to it when you want to record:

- A quote (from the user, an article, a known engineer) that has lasting value but isn't a task-specific decision
- An unresolved question — something you noticed but won't answer this turn
- A cross-project observation — "we did this in project X, similar pattern here"
- A note for future-self — context that would matter weeks later but doesn't fit any current task

Format each entry as:
  ## [turn N · YYYY-MM-DDTHH:MM:SSZ]
  Free-form body. The writer reorganizes structured content at checkpoint time.

This is your ONLY legal scratchpad — don't create \`learning.md\`, \`scratch.md\`, or any other ad-hoc memory file.

## Subagent return format

When you (as a subagent) finish your task, your final assistant message will be delivered to the spawning agent. If the spawn machinery added a "Return format (required)" section to your prompt, follow it exactly:

  **Status**: success | partial | failed | blocked
  **Summary**: <one-line description>

  <deliverable body>

  **Files touched**: <comma-separated paths or "(none)">
  **Findings worth promoting**: <bullet list, or "(none)">

If your spawn prompt didn't include this format (e.g., explore/title/summary agents have their own contracts), follow whatever your prompt specifies.

## What NOT to do

- Don't Edit checkpoint.md — that's the writer's domain.
- Don't create memory files other than notes.md (no learning.md, no scratch.md). Use notes.md for any free-form entry.
- Don't ask the user about something memory may already record — search first via Grep / Read.

## Active recall protocol

After a checkpoint rebuild, the following dumps may be already in your context (look for the "Summary of previous conversation from checkpoint files:" header followed by these dumps):

- checkpoint.md (full or budget-truncated)
- MEMORY.md (full or budget-truncated)
- notes.md (full or budget-truncated)
- global/MEMORY.md (full or budget-truncated)

If these dumps are visible in your context:

- Do NOT Read them again as whole files. The bytes are already in front of you.
- For specific past details (a particular turn's content, a specific tool output, an old command), use Grep with a keyword pattern to target the exact item — do not pull a whole file.
- For files NOT in the rebuild dump (per-task splitover progress.md files for tasks you don't actively need, spillover files, older session checkpoints in other sessions), Read on demand.

If a dump shows "⚠️ Truncated at ~N tokens. Read(<path>, offset=L) for the rest." — that file was budget-cut. Use Read with the offset only when you need the missing tail.

Memory entries name functions, files, flags, paths — those are CLAIMS about a point in time when they were written. Verify before acting on a specific name.

Don't ask the user about something memory may already record.
`
}

export type StreamInput = {
  user: MessageV2.User
  sessionID: string
  parentSessionID?: string
  model: Provider.Model
  agent: Agent.Info
  permission?: Permission.Ruleset
  system: string[]
  prebuiltSystem?: string[]      // when set, skip buildSystemArray and use this verbatim
  adaptiveStyle?: {admission:string; config:Config.Info; current:()=>boolean}
  /** Dispatch-local opaque source evidence; never sent as model options. */
  sameRequestRows?: Omit<import("@/provider/logging-transport").SameRequestRowsCarrier,"model">
  messages: ModelMessage[]
  small?: boolean
  tools: Record<string, Tool>
  activeTools?: string[]
  retries?: number
  toolChoice?: "auto" | "required" | "none"
  agentID?: string
  requestContext?: { actorExecution: ActorExecutionIdentity }
  /** Private same-turn schema view; not sent to the model or a tool. */
  schemaAnnotationView?: {names:string[]; canonicalTools:Record<string,Tool>; projectedSchemas:Record<string,Tool["inputSchema"]>; current:()=>Promise<boolean>; reanchor:()=>void}
  maintenanceWire?: MaintenanceWireCandidate
  onMaintenanceWire?: (receipt:{dispatchID:string;bodySHA256:string;wireProfile:"openai-chat"|"codex-responses";instructionsSHA256?:string;instructionsClass?:"builtin-compaction-v1"})=>Promise<void>
  nativeTextSources?: import("./context-inspection").NativeTextSnapshot
  nativeMembership?: NativeMembershipCapture
  /** Original-loop private observation; never model input or a capture grant. */
  sourceAttempt?: import("./closed-source-provenance").SourceAttemptHandle
  sourceToolsCurrent?:()=>Promise<boolean>
  onNativeMembership?: (receipt:NativeMembershipDelivery)=>Promise<void>
  recallToolSource?: RecallToolEvidence
  onRecallAdmission?: (evidence: { toolCallID: string; attestationID: string; resultSHA256: string; dispatchID: string; bodySHA256: string }) => Promise<void>
  contextPreface?: ContextPrefaceEvidence
  standingGuidance?: StandingGuidanceEvidence
  onStandingGuidance?: (receipt:{attestationID:string;textSHA256:string;dispatchID:string;bodySHA256:string;systemSHA256:string;wireProfile:'openai-chat'|'codex-responses'})=>Promise<void>
  onContextDelivery?: (evidence:{contextPartID:string;attestationID:string;textSHA256:string;dispatchID:string;bodySHA256:string})=>Promise<void>
  onNativeTextDelivery?: (receipt: NativeTextDelivery) => Promise<void>
  contextSources?: { sources: ContextSource[]; omittedSourceCount: number }
  contextSequence?: { attempt: number; transform: number }
  onContextFrame?: (frame: ContextFrame) => Promise<void>
  /** Isolated one-shot generation (titles and similar helpers). */
  ephemeral?: boolean
  /** Correlates an isolated request without changing the owning session. */
  requestID?: string
}

export type StreamRequest = StreamInput & {
  abort: AbortSignal
  // Set on the reactive one-shot retry after a Bedrock/gateway prefill-rejection
  // 400: hard-prune the trailing assistant (prefill) message(s) before building
  // the request so the resend ends with a user/tool message. See stream(). The
  // proactive guard (ProviderTransform.ensureTrailingUserMessage in message())
  // normally makes this unnecessary; this is a last-resort backstop.
  dropAssistantPrefill?: boolean
  /** Private to one actual scoped invocation; never supplied as model input. */
  ownedAttemptCell?: { handle?: ReturnType<typeof ManagedProvider.beginOwnedAnthropicAttempt>; scope?: object; error?:object; sdkError?:object; returned?:boolean; invalid?:boolean }
}

export type Event = Result["fullStream"] extends AsyncIterable<infer T> ? T : never

type PrefixInputWitness = {environment:import("./closed-source-provenance").EnvironmentProvenanceHandle; environmentStrings:string[]; skills?:SystemPrompt.SkillInvocation; instructions?:Instruction.InstructionResult; persona?:{result:import("../acp/openclank-protocol").OpenClankManagedProtocol.ManagedTurnStartResult;identity:ActorExecutionIdentity}}
const prefixWitnesses = new WeakMap<object, {system:string[]; bytes:readonly string[]; additions:string[]; additionsBytes:readonly string[]; environmentStrings:string[]; environmentBytes:readonly string[]; environment:import("./closed-source-provenance").EnvironmentProvenanceHandle; agent:import("./closed-source-provenance").AgentProvenanceHandle; skills?:SystemPrompt.SkillInvocation; catalogSkill?:boolean; instructions?:Instruction.InstructionResult; managedMemory?:string; transformOutput?:{system:string[]}; transformCurrent?:()=>boolean; personaCurrent?:()=>import("./closed-source-provenance").LegacyPersonaEvidence | import("./closed-source-provenance").ComposedPersonaEvidence | import("./closed-source-provenance").StyledPersonaEvidence | undefined; adaptiveCurrent?:()=>import("./closed-source-provenance").AdaptiveHintEvidence | undefined; unknown:readonly string[]}>()
/** Read-only, process-private partial evidence. It never establishes dispatch closure. */
export function readPrefixAssembly(handle:import("./closed-source-provenance").PrefixProvenanceHandle):import("./closed-source-provenance").PrefixAssemblyEvidence | undefined {
  const witness=prefixWitnesses.get(handle)
  if (!witness || witness.system.length!==witness.bytes.length || witness.system.some((x,i)=>x!==witness.bytes[i]) ||
      witness.additions.length!==witness.additionsBytes.length || witness.additions.some((x,i)=>x!==witness.additionsBytes[i]) ||
      witness.environmentStrings.length!==witness.environmentBytes.length || witness.environmentStrings.some((x,i)=>x!==witness.environmentBytes[i])) return
  if (witness.skills && !(witness.catalogSkill ? SystemPrompt.readCurrentSkillProvenance(witness.skills) : SystemPrompt.readEmptySkillProvenance(witness.skills))) return
  const instructions = witness.instructions ? Instruction.readInstructionProvenance(witness.instructions) : undefined
  if (witness.instructions && !instructions) return
  if (witness.transformCurrent && !witness.transformCurrent()) return
  const environment=SystemPrompt.readEnvironmentProvenance(witness.environment,witness.environmentStrings)
  const agentSHA256=SystemPrompt.readAgentProvenance(witness.agent)
  if (!environment || !agentSHA256) return
  const persona = witness.personaCurrent?.()
  if (witness.personaCurrent && !persona) return
  const adaptive = witness.adaptiveCurrent?.()
  if (witness.adaptiveCurrent && !adaptive) return
  const skill = witness.skills ? (witness.skills.value === undefined ? {kind:"absent" as const} :
    {kind:witness.catalogSkill ? "catalog" as const : "bundled_static" as const,outputSHA256:createHash("sha256").update(witness.skills.value,"utf8").digest("hex")}) : undefined
  const base = {coverage:"partial" as const,environment,agentSHA256:Object.freeze([...agentSHA256]),
    ...(skill ? {skill:Object.freeze(skill)} : {}), ...(persona ? {persona:Object.freeze(persona)} : {}),unknown:witness.unknown}
  if (instructions) return Object.freeze({...base,version:2 as const,instructions,
    ...(witness.managedMemory !== undefined ? {managedMemory:Object.freeze({constructor:"managed.memory-guidance.v1" as const,outputSHA256:createHash("sha256").update(witness.managedMemory,"utf8").digest("hex")})} : {}), ...(adaptive ? {adaptiveHint:adaptive} : {})})
  if (adaptive || skill?.kind === "catalog" || environment.version !== 1 || (persona && persona.constructor !== "host.factory-persona.v1")) return
  const legacySkill = skill?.kind === "absent" ? Object.freeze({kind:"absent" as const}) :
    skill?.kind === "bundled_static" ? Object.freeze({kind:"bundled_static" as const,outputSHA256:skill.outputSHA256}) : undefined
  return Object.freeze({version:1 as const,coverage:"partial" as const,environment,agentSHA256:Object.freeze([...agentSHA256]),
    ...(legacySkill ? {skill:legacySkill} : {}),...(persona ? {persona:Object.freeze(persona)} : {}),unknown:witness.unknown})
}
const prefixHandles = new WeakMap<string[], import("./closed-source-provenance").PrefixProvenanceHandle>()
export function prefixAssemblyHandle(system:string[]):import("./closed-source-provenance").PrefixProvenanceHandle | undefined { return prefixHandles.get(system) }
/** Derive a turn-local prefix without mutating or persisting its base. */
function deriveAdaptivePrefix(base:string[], current:()=>import("./closed-source-provenance").AdaptiveHintEvidence | undefined):string[] {
  const derived=[...base]; derived[0]=derived[0]+'\n\n'+ADAPTIVE_CONCISE_HINT
  const old=prefixHandles.get(base), witness=old && prefixWitnesses.get(old)
  if (old && witness && readPrefixAssembly(old)?.version===2) {
    const handle=Object.freeze({}) as import("./closed-source-provenance").PrefixProvenanceHandle
    prefixWitnesses.set(handle,{...witness,system:derived,bytes:[...derived],adaptiveCurrent:current})
    prefixHandles.set(derived,handle)
  }
  return derived
}

export interface Interface {
  readonly stream: (input: StreamInput) => Stream.Stream<Event, unknown>
  readonly buildSystemArray: (input: {
    agent: Agent.Info
    model: Provider.Model
    system: string[]
    user: MessageV2.User
    sessionID: string
    agentID?: string
    sourceWitness?: PrefixInputWitness
  }) => Effect.Effect<string[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/LLM") {}

const live: Layer.Layer<
  Service,
  never,
  Auth.Service | Config.Service | Provider.Service | Plugin.Service | Permission.Service | ActorRegistry.Service | Memory.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const auth = yield* Auth.Service
    const config = yield* Config.Service
    const provider = yield* Provider.Service
    const plugin = yield* Plugin.Service
    const perm = yield* Permission.Service
    const actorReg = yield* ActorRegistry.Service
    const memory = yield* Memory.Service

    const buildSystemArray = Effect.fn("LLM.buildSystemArray")(function* (input: {
      agent: Agent.Info
      model: Provider.Model
      system: string[]
      user: MessageV2.User
      sessionID: string
      agentID?: string
      ephemeral?: boolean
      sourceWitness?: PrefixInputWitness
    }) {
      const system: string[] = []
      const ownedAgent = input.sourceWitness ? SystemPrompt.agentWithProvenance(input.agent,input.model) : undefined
      system.push(
        [
          ...(ownedAgent?.strings ?? SystemPrompt.agent(input.agent, input.model)),
          // any custom prompt passed into this call
          ...input.system,
          // any custom prompt from last user message
          ...(input.user.system ? [input.user.system] : []),
        ]
          .filter((x) => x)
          .join("\n"),
      )

      // v5: memory-instructions section. Teaches the agent how/where/when to
      // maintain `MEMORY.md` and `checkpoint.md` directly via Edit. Project ID is
      // resolved from the ALS-bound Instance with a safe fallback to
      // `ProjectID.global` (mirrors the pattern in session/checkpoint.ts so the
      // path the prompt advertises matches the path the writer actually writes).
      // Injected only for actors whose context the checkpoint flow serves —
      // main + peer. Subagents (explore/general/compose) use per-actor compaction
      // and have no checkpoint duty; system-spawned actors (checkpoint-writer et al.)
      // are the writers themselves. Shares the exact `servesCheckpoint` judgement
      // with SessionPrune.fireCheckpoints so the "who owns a checkpoint" and "who is
      // taught about it" sets can never drift apart.
      // The chat profile is a no-tools conversation lane (identity ruling
      // R2): it can't Edit MEMORY.md, so teaching it the file-memory manual
      // would assert capabilities the turn doesn't have.
      let managedMemoryText:string | undefined
      const servesCheckpoint =
        !input.ephemeral &&
        input.agent.name !== "chat" &&
        (yield* actorReg.servesCheckpoint(SessionID.make(input.sessionID), input.agentID))
      if (servesCheckpoint) {
        const projectID =
          (yield* Effect.try({
            try: () => Instance.current?.project?.id as ProjectID | undefined,
            catch: () => undefined,
          }).pipe(Effect.orElseSucceed(() => undefined))) ?? ProjectID.global
        // Bootstrap the memory.md → MEMORY.md migration at session start so a
        // legacy lowercase file is renamed before the agent's first direct
        // Edit/Write (which would otherwise miss it on a case-sensitive FS, or
        // create an uppercase sibling and orphan the legacy content). The two
        // checkpoint-flow call sites cover the writer/rebuild paths; this covers
        // the "agent edits MEMORY.md before any checkpoint" path. Idempotent.
        if (ManagedProvider.enabled()) {
          managedMemoryText = buildManagedMemoryInstructions()
          system.push(managedMemoryText)
        } else {
          yield* Effect.promise(() => assertCurrentProjectMemory(projectID))
          system.push(buildMemoryInstructions(SessionID.make(input.sessionID), projectID, yield* memory.root()))
        }
      }

      // Plugins still see the multi-part array (base prompt as [0], memory as a
      // trailing element) so hooks that index or append parts keep working.
      const transformOutput = { system }
      if (input.sourceWitness) plugin.requestEmptySystemTransform?.(transformOutput)
      yield* plugin.trigger(
        "experimental.chat.system.transform",
        { sessionID: input.sessionID, model: input.model },
        transformOutput,
      )

      // Collapse to a single system message. The historical 2-part split existed
      // only to keep a byte-stable cache prefix separate from the memory block's
      // per-session paths — but within a session those paths are fixed, so the
      // whole thing is stable and one block caches just as well. One message also
      // keeps the fork-prefix parity invariant trivial (nothing to misalign) and
      // spares subagents/providers a stray extra system turn. Join with a blank
      // line (\n\n) so adjacent markdown sections (base prompt, "# Memory system")
      // don't run together into one heading.
      const output = system.length <= 1 ? system : [system.filter((x) => x).join("\n\n")]
      const source = input.sourceWitness
      const environmentProof = source && ownedAgent?.handle ? SystemPrompt.readEnvironmentProvenance(source.environment,source.environmentStrings) : undefined
      if (source && ownedAgent?.handle && environmentProof &&
          source.environmentStrings.length <= input.system.length &&
          source.environmentStrings.every((value,index) => value === input.system[index])) {
        // This capsule explicitly remains partial: hooks, additional inputs and
        // post-assembly provider transformations have not been qualified here.
        const noTransform = plugin.readEmptySystemTransform?.(transformOutput,input.sessionID) === true
        const skill = source.skills
        const emptySkill = !!skill && SystemPrompt.readEmptySkillProvenance(skill)
        const catalogSkill = !!skill && !emptySkill && SystemPrompt.readCurrentSkillProvenance(skill)
        const legacySkill = !!skill && emptySkill && (skill.value === undefined ? input.system.length === source.environmentStrings.length :
          input.system.length === source.environmentStrings.length + 1 && input.system[source.environmentStrings.length] === skill.value)
        const qualifiedSkill = source.instructions ? !!skill && (emptySkill || catalogSkill) : legacySkill
        const instructionSnapshot = source.instructions && Instruction.readInstructionProvenance(source.instructions)
        const expected = [...source.environmentStrings,...(skill?.value ? [skill.value] : []),...(source.instructions?.content ?? [])]
        const additionsMatch = source.instructions ? input.system.length === expected.length && expected.every((value,index)=>input.system[index]===value) :
          (qualifiedSkill || input.system.length === source.environmentStrings.length)
        const userSystem=input.user.system
        const persona=source.persona
        const personaCurrent = persona && userSystem ? () => input.user.system === userSystem
          ? ManagedProvider.readFactoryPersonaStart(persona.result,persona.identity,userSystem) : undefined : undefined
        const qualifiedPersona=personaCurrent?.()
        const unknown = [...(!noTransform ? ["system_transform"] : []), ...(!qualifiedSkill ? ["skills"] : []),
          ...(!additionsMatch || (source.instructions && !instructionSnapshot) ? ["additional_system"] : []),
          ...(input.user.system && !qualifiedPersona ? ["user_system"] : []), ...(servesCheckpoint && (managedMemoryText === undefined || !instructionSnapshot) ? ["memory_instructions"] : [])]
        try { log.debug("source_prefix_eligibility",{environment:true,agent:true,skills:qualifiedSkill,instruction:!!instructionSnapshot,
          additions:additionsMatch,persona:!userSystem || !!qualifiedPersona,transform:noTransform,memory:!servesCheckpoint || managedMemoryText !== undefined}) } catch { /* diagnostic only */ }
        const handle=Object.freeze({}) as import("./closed-source-provenance").PrefixProvenanceHandle
        prefixWitnesses.set(handle,{system:output,bytes:[...output],additions:input.system,additionsBytes:[...input.system],
          environmentStrings:source.environmentStrings,environmentBytes:[...source.environmentStrings],environment:source.environment,
          agent:ownedAgent.handle,...(qualifiedSkill ? {skills:skill,catalogSkill} : {}),
          ...(instructionSnapshot && source.instructions ? {instructions:source.instructions} : {}),
          ...(managedMemoryText !== undefined ? {managedMemory:managedMemoryText} : {}),
          ...(noTransform ? {transformOutput,transformCurrent:()=>plugin.readEmptySystemTransform?.(transformOutput,input.sessionID)===true} : {}),
          ...(qualifiedPersona && personaCurrent ? {personaCurrent} : {}),unknown:Object.freeze(unknown)})
        prefixHandles.set(output,handle)
      } else if (source) {
        try { log.debug("source_prefix_eligibility",{environment:!!environmentProof,agent:!!ownedAgent?.handle,skills:false,instruction:false,
          additions:false,persona:false,transform:false,memory:false}) } catch { /* diagnostic only */ }
      }
      return output
    })

    const run = Effect.fn("LLM.run")(function* (input: StreamRequest) {
      const correlationID = input.requestID ?? input.sessionID
      if (ManagedProvider.enabled() && !input.requestContext?.actorExecution) throw new Error("managed actor execution identity is unavailable")
      const managedScope = ManagedProvider.enabled()
        ? ManagedProvider.requireScope(input.sessionID, input.requestContext?.actorExecution.actorID, input.requestContext?.actorExecution.executionRevision)
        : undefined
      const l = log
        .clone()
        .tag("providerID", input.model.providerID)
        .tag("modelID", input.model.id)
        .tag(input.requestID ? "request.id" : "session.id", correlationID)
        .tag("small", (input.small ?? false).toString())
        .tag("agent", input.agent.name)
        .tag("mode", input.agent.mode)
      l.info("stream", {
        modelID: input.model.id,
        providerID: input.model.providerID,
      })

      const [language, cfg, item, info] = yield* Effect.all(
        [
          provider.getLanguage(input.model, managedScope, {
            sessionID: input.sessionID,
            ...(input.requestContext?.actorExecution ? { actorID: input.requestContext.actorExecution.actorID, executionRevision: input.requestContext.actorExecution.executionRevision } : {}),
            hostChatID: input.requestContext?.actorExecution
              ? ManagedProvider.currentActorHostContext(input.sessionID, input.requestContext.actorExecution.actorID, input.requestContext.actorExecution.executionRevision)?.chatID
              : ManagedProvider.currentHostContext(input.sessionID)?.chatID,
          }),
          config.get(),
          provider.getProvider(input.model.providerID),
          managedScope
            ? Effect.succeed(managedScope.credential)
            : auth.get(input.model.providerID),
        ],
        { concurrency: "unbounded" },
      )

      // Attach only the model actually returned for this managed scope and C2.
      const identity=input.requestContext?.actorExecution
      if (managedScope && identity?.actorID==='main' && identity.rootOperationID && identity.innerTurnID &&
          ManagedProvider.isOwnedAlternativeActive({sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,
            executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID},managedScope) && !isPinnedAnthropicModel(language as object))
        throw new Error('Alternative provider implementation unavailable')
      if (input.ownedAttemptCell && managedScope && identity?.actorID === "main" && identity.rootOperationID && identity.innerTurnID) {
        const handle=ManagedProvider.beginOwnedAnthropicAttempt({sessionID:identity.sessionID,rootOperationID:identity.rootOperationID,actorID:identity.actorID,executionRevision:identity.executionRevision,innerTurnID:identity.innerTurnID},language as object,managedScope)
        if (handle) { input.ownedAttemptCell.handle=handle; input.ownedAttemptCell.scope=managedScope }
      }

      const managedMetadata = managedScope ? Provider.managedAdapterMetadata(item, input.model) : undefined
      const compatibleContextEndpoint = input.contextPreface?.wireProfile === "compatible-chat" ? pinnedCompatibleChatEndpoint(language as object) : undefined
      // Never send an inserted memory part through an unqualified compatible SDK.
      if (input.contextPreface?.wireProfile === "compatible-chat" && (!compatibleContextEndpoint || input.ephemeral
          || input.requestContext?.actorExecution?.actorID !== "main" || !input.requestContext.actorExecution.rootOperationID
          || !input.requestContext.actorExecution.innerTurnID || input.model.api.npm !== "@ai-sdk/openai-compatible"
          || managedMetadata?.familyID !== "openai-compatible" || managedMetadata.adapterID !== "openai-chat"
          || managedMetadata.billingLane !== "local" || managedScope?.credentialRequired !== false
          || managedScope.connectionID !== managedMetadata.connectionID))
      {
        // The context is optional. Remove only the exact inserted message when
        // the selected model cannot prove the compatible transport; never send
        // it or report a delivery on an unqualified route.
        const inserted = contextPrefaceFrame(input.contextPreface, input.messages)
        if (!inserted) throw new Error("Compatible context reanchor unavailable")
        input = { ...input, messages: input.messages.filter((_, index) => index !== inserted.index), contextPreface: undefined }
      }

      // ChatGPT subscription uses the Codex Responses transport, whose system
      // prompt belongs in `instructions`. Runtime provider IDs are normalized
      // connection IDs, so managed mode must classify it from host metadata.
      const isOpenaiOauth = managedMetadata
        ? managedMetadata.familyID === "openai" && managedMetadata.billingLane === "subscription" && info?.type === "oauth"
        : item.id === "openai" && info?.type === "oauth"

      const system =
        input.prebuiltSystem ??
        (yield* buildSystemArray({
          agent: input.agent,
          model: input.model,
          system: input.system,
          user: input.user,
          sessionID: input.sessionID,
          agentID: input.agentID,
          ephemeral: input.ephemeral,
        }))

      const variant =
        !input.small && input.model.variants && input.user.model.variant
          ? input.model.variants[input.user.model.variant]
          : {}
      const base = input.small
        ? ProviderTransform.smallOptions(input.model)
        : ProviderTransform.options({
            model: input.model,
            sessionID: input.sessionID,
            providerOptions: item.options,
          })
      const mergedOptions: Record<string, any> = pipe(
        base,
        mergeDeep(input.model.options),
        mergeDeep(input.agent.options),
        mergeDeep(variant),
      )
      const options = managedMetadata ? stripManagedProjectionOptions(mergedOptions) : mergedOptions
      applyEphemeralStructuredOptions(options, input, managedMetadata?.familyID)
      if (isOpenaiOauth) {
        options.instructions = system.join("\n")
      }

      const isWorkflow = language instanceof GitLabWorkflowLanguageModel
      if (input.ephemeral && isWorkflow) return yield* Effect.fail(new Error("Ephemeral workflow generation is unsupported"))
      // Reactive prefill-rejection backstop. The PRIMARY mechanism is the
      // proactive guard in ProviderTransform.message()
      // (ensureTrailingUserMessage): we never send a request ending in an
      // assistant (prefill) turn, and we never delete a completed reply to do so.
      // This reactive path is defense-in-depth: if any code path still slips a
      // trailing assistant through to the wire (e.g. a provider-side transform
      // re-adds one) and the backend 400s on it, run() re-runs with this flag set
      // to hard-prune the trailing assistant turn(s) so the resend ends with a
      // user/tool message. It should effectively never fire, but keeping it is
      // cheap and safe.
      const requestMessages = input.dropAssistantPrefill
        ? ProviderTransform.dropTrailingAssistantPrefill(input.messages)
        : input.messages
      let messages = isOpenaiOauth
        ? requestMessages
        : isWorkflow
          ? requestMessages
          : [
              ...system.map(
                (x): ModelMessage => ({
                  role: "system",
                  content: x,
                }),
              ),
              ...requestMessages,
            ]
      const prefaceInput = input.contextPreface && requestMessages.length === input.contextPreface.count
        ? contextPrefaceFrame({...input.contextPreface,index:input.contextPreface.index + messages.length - requestMessages.length,count:messages.length},messages)
        : undefined

      const defaults = {
        temperature: input.model.capabilities.temperature
          ? (input.agent.temperature ?? ProviderTransform.temperature(input.model))
          : undefined,
        topP: input.agent.topP ?? ProviderTransform.topP(input.model),
        topK: ProviderTransform.topK(input.model),
        maxOutputTokens: ProviderTransform.maxOutputTokens(input.model),
        options,
      }
      const params = input.ephemeral ? defaults : yield* plugin.trigger(
        "chat.params",
        {
          sessionID: input.sessionID,
          agent: input.agent.name,
          model: input.model,
          provider: item,
          message: input.user,
        },
        {
          temperature: input.model.capabilities.temperature
            ? (input.agent.temperature ?? ProviderTransform.temperature(input.model))
            : undefined,
          topP: input.agent.topP ?? ProviderTransform.topP(input.model),
          topK: ProviderTransform.topK(input.model),
          maxOutputTokens: ProviderTransform.maxOutputTokens(input.model),
          options,
        },
      )

      const { headers } = input.ephemeral ? { headers: {} } : yield* plugin.trigger(
        "chat.headers",
        {
          sessionID: input.sessionID,
          agent: input.agent.name,
          model: input.model,
          provider: item,
          message: input.user,
        },
        {
          headers: {},
        },
      )

      let {tools,activeTools} = selectFinalTools(input)

      // LiteLLM and some Anthropic proxies require the tools parameter to be present
      // when message history contains tool calls, even if no tools are being used.
      // Add a dummy tool that is never called to satisfy this validation.
      // This is enabled for:
      // 1. Providers with "litellm" in their ID or API ID (auto-detected)
      // 2. Providers with explicit "litellmProxy: true" option (opt-in for custom gateways)
      const isLiteLLMProxy =
        item.options?.["litellmProxy"] === true ||
        input.model.providerID.toLowerCase().includes("litellm") ||
        input.model.api.id.toLowerCase().includes("litellm")

      // LiteLLM/Bedrock rejects requests where the message history contains tool
      // calls but no tools param is present. When there are no active tools (e.g.
      // during compaction), inject a stub tool to satisfy the validation requirement.
      // The stub description explicitly tells the model not to call it.
      const ensureNoop = () => {
        if ((isLiteLLMProxy ||
            (managedMetadata ? managedMetadata.adapterID === "copilot-chat" : input.model.providerID.includes("github-copilot"))) &&
            activeTools.length === 0 && hasToolCalls(input.messages)) {
          tools["_noop"] = tool({
            description: "Do not call this tool. It exists only for API compatibility and must never be invoked.",
            inputSchema: jsonSchema({type:"object",properties:{reason:{type:"string",description:"Unused"}}}),
            execute: async () => ({ output: "", title: "", metadata: {} }),
          })
          activeTools.push("_noop")
        }
      }
      ensureNoop()

      // Wire up toolExecutor for DWS workflow models so that tool calls
      // from the workflow service are executed via opencode's tool system
      // and results sent back over the WebSocket.
      if (language instanceof GitLabWorkflowLanguageModel) {
        const workflowModel = language as GitLabWorkflowLanguageModel & {
          sessionID?: string
          sessionPreapprovedTools?: string[]
          approvalHandler?: (approvalTools: { name: string; args: string }[]) => Promise<{ approved: boolean }>
        }
        workflowModel.sessionID = input.sessionID
        workflowModel.systemPrompt = system.join("\n")
        workflowModel.toolExecutor = async (toolName, argsJson, _requestID) => {
          const registered = Object.keys(tools)
          const resolvedName = ToolCompat.resolveName(toolName, registered) ?? toolName
          const t = tools[resolvedName]
          if (!t || !t.execute) {
            return { result: "", error: `Unknown tool: ${toolName}` }
          }
          try {
            const schema = await Promise.resolve(asSchema(t.inputSchema).jsonSchema)
            const args = ToolCompat.normalizeInput(ToolCompat.parseToolInput(argsJson), schema)
            const result = await t.execute!(args, {
              toolCallId: _requestID,
              messages: input.messages,
              abortSignal: input.abort,
            })
            const output = typeof result === "string" ? result : (result?.output ?? JSON.stringify(result))
            return {
              result: output,
              metadata: typeof result === "object" ? result?.metadata : undefined,
              title: typeof result === "object" ? result?.title : undefined,
            }
          } catch (e: any) {
            return { result: "", error: e.message ?? String(e) }
          }
        }

        const ruleset = Agent.runtimePermission(input.agent, input.permission)
        workflowModel.sessionPreapprovedTools = Object.keys(tools).filter((name) => {
          const match = ruleset.findLast((rule) => Wildcard.match(name, rule.permission))
          return !match || match.action !== "ask"
        })

        const bridge = yield* EffectBridge.make()
        const approvedToolsForSession = new Set<string>()
        workflowModel.approvalHandler = Instance.bind(async (approvalTools) => {
          const uniqueNames = [...new Set(approvalTools.map((t: { name: string }) => t.name))] as string[]
          // Auto-approve tools that were already approved in this session
          // (prevents infinite approval loops for server-side MCP tools)
          if (uniqueNames.every((name) => approvedToolsForSession.has(name))) {
            return { approved: true }
          }

          const id = PermissionID.ascending()
          let unsub: (() => void) | undefined
          try {
            unsub = Bus.subscribe(Permission.Event.Replied, (evt) => {
              if (evt.properties.requestID === id) void evt.properties.reply
            })
            const toolPatterns = approvalTools.map((t: { name: string; args: string }) => {
              try {
                const parsed = JSON.parse(t.args) as Record<string, unknown>
                const title = (parsed?.title ?? parsed?.name ?? "") as string
                return title ? `${t.name}: ${title}` : t.name
              } catch {
                return t.name
              }
            })
            const uniquePatterns = [...new Set(toolPatterns)] as string[]
            await bridge.promise(
              perm.ask({
                id,
                sessionID: SessionID.make(input.sessionID),
                permission: "workflow_tool_approval",
                patterns: uniquePatterns,
                metadata: { tools: approvalTools },
                always: uniquePatterns,
                ruleset: [],
              }),
            )
            for (const name of uniqueNames) approvedToolsForSession.add(name)
            workflowModel.sessionPreapprovedTools = [...(workflowModel.sessionPreapprovedTools ?? []), ...uniqueNames]
            return { approved: true }
          } catch {
            return { approved: false }
          } finally {
            unsub?.()
          }
        })
      }

      const tracer = cfg.experimental?.openTelemetry
        ? Option.getOrUndefined(yield* Effect.serviceOption(OtelTracer.OtelTracer))
        : undefined
      const telemetryTracer = tracer
        ? new Proxy(tracer, {
            get(target, prop, receiver) {
              if (prop !== "startSpan") return Reflect.get(target, prop, receiver)
              return (...args: Parameters<typeof target.startSpan>) => {
                const span = target.startSpan(...args)
                span.setAttribute(input.requestID ? "request.id" : "session.id", correlationID)
                return span
              }
            },
          })
        : undefined

      const streamStartTs = Date.now()
      l.debug("streamText starting", {
        messageID: input.user.id,
        msgCount: messages.length,
        registeredToolCount: Object.keys(tools).length,
        activeToolCount: activeTools.length,
      })
      if (!input.ephemeral) yield* plugin
        .trigger(
          "session.llm.request",
          {
            sessionID: input.sessionID,
            providerID: input.model.providerID,
            modelID: input.model.id,
            trajectory: [
              ...system.map((content) => ({ role: "system", content })),
              ...requestMessages,
            ],
            systemPrompt: system,
          },
          {},
        )
        .pipe(Effect.ignore)

      let adaptiveDispatch: {basePrompt:any;derivedPrompt:any;baseSystem:string[];derivedSystem:string[];current:()=>boolean} | undefined
      let sdkDispatchIDs: string[] = []
      let maintenanceCandidate: MaintenanceWireCandidate | undefined
      let standingCandidate: StandingGuidanceEvidence | undefined
      let membershipCandidate: NativeMembershipFrame | undefined
      let sourceFinalFrame: import("./closed-source-provenance").SourceFinalFrame | undefined
      let nativeDeliveryCandidate: NativeTextEvidence | undefined
      let recallDeliveryCandidate: RecallToolEvidence | undefined
      let contextDeliveryCandidate: ContextPrefaceEvidence | undefined
      if (input.schemaAnnotationView) {
        const profile=input.schemaAnnotationView
        const selected=selectFinalTools(input)
        const resolved=yield* Effect.promise(()=>selectAnnotationView(selected,profile))
        if (resolved.reanchored) {
          tools=resolved.tools
          activeTools=resolved.activeTools
          ensureNoop()
        }
      }
      return streamText({
        onStepFinish(step) {
          if (input.ownedAttemptCell?.handle) input.ownedAttemptCell.invalid=true
          const metadata=step.providerMetadata && Object.fromEntries(Object.entries(step.providerMetadata).filter(([key])=>key!=='_openclankSuccessStep'))
          Session.getUsage({ model: input.model, usage: step.usage, metadata,
            captureScope: managedScope, captureDispatchIDs: sdkDispatchIDs, mainReply: !input.ephemeral })
        },
        onError(error) {
          const cause = error && typeof error === "object" && "error" in error ? error.error : error
          if (input.ownedAttemptCell?.handle) {
            if (!cause || typeof cause!=="object" || input.ownedAttemptCell.sdkError) input.ownedAttemptCell.invalid=true
            else input.ownedAttemptCell.sdkError=cause
          }
          if (!input.abort.aborted) recordSdkFailure(cause, managedScope, sdkDispatchIDs)
          l.debug("streamText error", {
            messageID: input.user.id,
            error: error instanceof Error ? error.message : String(error),
            elapsedMs: Date.now() - streamStartTs,
          })
          l.error("stream error", {
            error,
          })
        },
        async experimental_repairToolCall(failed) {
          const repaired = await ToolCompat.repairToolCall({
            toolName: failed.toolCall.toolName,
            input: failed.toolCall.input,
            toolNames: activeTools,
            getSchema: (toolName) => failed.inputSchema({ toolName }),
          })
          if (repaired) {
            l.info("repairing tool call", {
              tool: failed.toolCall.toolName,
              repaired: repaired.toolName,
            })
            return {
              ...failed.toolCall,
              toolName: repaired.toolName,
              input: repaired.input,
            }
          }
          return {
            ...failed.toolCall,
            input: JSON.stringify({
              tool: failed.toolCall.toolName,
              error: failed.error.message,
            }),
            toolName: "invalid",
          }
        },
        temperature: params.temperature,
        topP: params.topP,
        topK: params.topK,
        providerOptions: ProviderTransform.providerOptions(input.model, params.options),
        activeTools,
        tools: captureFinalTools(ProviderTransform.tools(tools, input.model), activeTools, input.sourceAttempt, !!sourceAttemptLayout(input.sourceAttempt), input.sourceToolsCurrent),
        toolChoice: input.toolChoice,
        maxOutputTokens: params.maxOutputTokens,
        abortSignal: input.abort,
        headers: {
          ...(!input.ephemeral ? { "x-session-affinity": input.sessionID } : {}),
          ...(!input.ephemeral && input.parentSessionID ? { "x-parent-session-id": input.parentSessionID } : {}),
          ...input.model.headers,
          ...headers,
          "User-Agent": `mimocode/${InstallationVersion}`,
        },
        // AI SDK's internal retry loop is SILENT — it emits no events and does
        // not update session status, so the TUI shows only a dead spinner while
        // it runs. Its backoff is also UNCAPPED (delay *= 2 each attempt, capped
        // only by a retry-after header), so the prior default of 10 meant up to
        // ~34 min (2+4+…+1024s) of invisible retrying before the error surfaced.
        // We keep this layer short (absorb a couple of quick blips) and let the
        // VISIBLE processor-level SessionRetry.policy own long-haul resilience —
        // it publishes `type: "retry"` so the `[retrying attempt #N]` banner
        // shows, and its per-attempt delay is capped at 30s.
        // Managed mode centralizes retry/failover in the account-aware
        // processor. An SDK-internal retry is invisible and would reuse a
        // credential after the host has cooled or revoked that account.
        maxRetries: input.ephemeral ? 0 : (ManagedProvider.enabled() ? 0 : (input.retries ?? 2)),
        messages,
        model: wrapLanguageModel({
          model: language,
          middleware: [
            {
              specificationVersion: "v3" as const,
              async wrapStream({ doStream, params }) {
                if (adaptiveDispatch) { input={...input,prebuiltSystem:adaptiveDispatch.baseSystem}; adaptiveDispatch=undefined }
                  // Attach only after the actual provider-frame context transform. The
                  // unmodified base remains the cache/fork value; this clone is dispatch-local.
                  const adaptive=input.adaptiveStyle, base=input.prebuiltSystem
                  const adaptiveCurrent=adaptive?.current()===true
                  if (adaptive && base?.length===1 && !input.ephemeral && !input.small && !isOpenaiOauth && !isWorkflow && !input.dropAssistantPrefill &&
                      !input.standingGuidance && input.requestContext?.actorExecution?.actorID==='main' && !input.abort.aborted && adaptiveCurrent) {
                    const prompt=params.prompt, first=prompt[0]
                    if (first?.role==='system' && first.content===base[0] && Object.keys(first).every(key=>key==='content' || key==='role' || (key==='providerOptions' && first.providerOptions===undefined)) && !prompt.slice(1).some(item=>item.role==='system')) {
                      const chosen=prompt.slice(1), snapshot=adaptiveSnapshot(base,chosen), descriptor=readWindowOrigin(input.model), window=contextWindow({cfg:adaptive.config,model:input.model})
                      let admission:unknown; try { admission=JSON.parse(adaptive.admission) } catch {}
                      const admissionSHA256=adaptiveAdmissionSHA256(admission), modelWindowSHA256=descriptor && adaptiveModelWindowSHA256(input.model,descriptor,window)
                      const level=snapshot && contextPressureLevel({cfg:adaptive.config,model:input.model,tokens:{input:0,output:0,reasoning:0,cache:{read:0,write:0}},additionalTokens:snapshot.estimatedTokens})
                      if (snapshot && admissionSHA256 && modelWindowSHA256 && (level===2 || level===3)) {
                        const hintSHA256=createHash('sha256').update(ADAPTIVE_CONCISE_HINT,'utf8').digest('hex')
                        const evidence=Object.freeze({constructor:'native.adaptive-concise-hint.v1' as const,hintSHA256,admissionSHA256,snapshotSHA256:snapshot.snapshotSHA256,modelWindowSHA256,pressureLevel:level})
                        const current=()=>{try {const d=readWindowOrigin(input.model), w=contextWindow({cfg:adaptive.config,model:input.model}), snap=adaptiveSnapshot(base,chosen)
                          return adaptive.current() && !!d && adaptiveModelWindowSHA256(input.model,d,w)===modelWindowSHA256 && snap?.snapshotSHA256===snapshot.snapshotSHA256
                        } catch {return false}}
                        const dispatchCurrent=()=>!input.abort.aborted && current()
                        if (dispatchCurrent()) {
                          const derived=deriveAdaptivePrefix(base,()=>current() ? evidence : undefined)
                          const derivedPrompt=[{...first,content:derived[0]!},...chosen] as typeof prompt
                          adaptiveDispatch={basePrompt:prompt,derivedPrompt,baseSystem:base,derivedSystem:derived,current:dispatchCurrent}
                          params.prompt=derivedPrompt
                          input={...input,prebuiltSystem:derived}
                        }
                      }
                    }
                  }
                if (input.adaptiveStyle && input.onContextFrame && !input.ephemeral) {
                  try {
                    const frame=contextFrame({
                      providerID: input.model.providerID, modelID: input.model.id,
                      identity: { sessionID: input.sessionID,
                        ...(input.requestContext?.actorExecution ? {
                          actorID: input.requestContext.actorExecution.actorID,
                          executionRevision: input.requestContext.actorExecution.executionRevision,
                          rootOperationID: input.requestContext.actorExecution.rootOperationID,
                          innerTurnID: input.requestContext.actorExecution.innerTurnID,
                        } : {}) },
                      window: contextWindow({ cfg, model: input.model }), sources: input.contextSources?.sources,
                      omittedSourceCount: input.contextSources?.omittedSourceCount,
                      sequence: { attempt: input.contextSequence?.attempt ?? 1, transform: input.contextSequence ? ++input.contextSequence.transform : 1 },
                      prompt: params.prompt, instructions: isOpenaiOauth ? ((params.providerOptions?.openai as Record<string, unknown> | undefined)?.instructions ?? Symbol("unavailable")) : undefined,
                      tools: params.tools,
                    })
                    const handle=input.ownedAttemptCell?.handle
                    if (handle) await issueAnthropicCallback(handle.probe,()=>input.onContextFrame!(frame))
                    else await input.onContextFrame(frame)
                  } catch (error) {
                    l.warn("context inspection unavailable", { error: String(error) })
                  }
                }
                if (adaptiveDispatch) {
                  sourceFinalFrame=undefined
                  membershipCandidate = !input.ephemeral && !isOpenaiOauth && input.nativeMembership && sameNativeMembershipIdentity(input.nativeMembership.identity,input.requestContext?.actorExecution) && managedMetadata?.familyID==='openai' && managedMetadata.adapterID==='openai-chat' && managedMetadata.billingLane==='metered_api' && info?.type==='api' && item.options?.fetch===undefined && isPinnedChatModel(language as object) ? nativeMembershipFrame(input.nativeMembership,params.prompt,language as object) : undefined
                  if (input.sourceAttempt && input.prebuiltSystem && membershipCandidate) {
                    const capsule=prefixAssemblyHandle(input.prebuiltSystem), current=capsule ? ()=>readPrefixAssembly(capsule) : undefined, proof=current?.()
                    if (proof && proof.unknown.length===0 && membershipCandidate.sources.length===1 && membershipCandidate.sources[0]?.role==='user' && membershipCandidate.roles.filter(role=>role!=='system').length===1 && current)
                      sourceFinalFrame={frame:membershipCandidate,system:input.prebuiltSystem,current}
                  }
                  if (input.sourceAttempt && input.prebuiltSystem && !input.ephemeral && !isOpenaiOauth &&
                      input.model.api.npm==='@ai-sdk/openai-compatible' && managedMetadata?.familyID==='openai-compatible' &&
                      managedMetadata.adapterID==='openai-chat' && managedScope?.connectionID===managedMetadata.connectionID && item.options?.fetch===undefined) {
                    const endpoint=pinnedCompatibleChatEndpoint(language as object), layout=sourceAttemptLayout(input.sourceAttempt)
                    const capsule=prefixAssemblyHandle(input.prebuiltSystem), current=capsule ? ()=>readPrefixAssembly(capsule) : undefined
                    const prefix=current?.(), full=layout ? [...layout.layout,...layout.generated.map(g=>({position:g.position,sha256:g.sha256,synthetic:true}))] : undefined
                    if (endpoint && layout && full && prefix && prefix.unknown.length===0 && current && sourceLayoutMessages(full,params.prompt,false))
                      sourceFinalFrame={version:2,model:language as object,endpoint,system:input.prebuiltSystem,layout,current}
                  }
                }
                if (adaptiveDispatch && !adaptiveDispatch.current() && params.prompt===adaptiveDispatch.derivedPrompt) {
                  params.prompt=adaptiveDispatch.basePrompt
                  input={...input,prebuiltSystem:adaptiveDispatch.baseSystem}
                  adaptiveDispatch=undefined
                  sourceFinalFrame=undefined
                }
                const ids:string[] = []
                sdkDispatchIDs = ids
                const invoke=()=>observeAnthropicDoStream(language as object, () => withSdkDispatches(managedScope, ids, doStream, nativeDeliveryCandidate, input.onNativeTextDelivery, input.requestContext?.actorExecution, recallDeliveryCandidate, input.onRecallAdmission, contextDeliveryCandidate, input.onContextDelivery, maintenanceCandidate, input.onMaintenanceWire, standingCandidate, input.onStandingGuidance, membershipCandidate, input.onNativeMembership, input.sourceAttempt, input.abort, sourceFinalFrame, input.sameRequestRows ? {...input.sameRequestRows,model:language as object,current:()=>!input.abort.aborted && input.sameRequestRows!.current()} : undefined))
                const handle=input.ownedAttemptCell?.handle
                const result=await (handle && handle.current() ? withAnthropicFailureProbe(handle.probe,invoke) : invoke())
                return {...result,stream:result.stream.pipeThrough(new TransformStream({transform(chunk,controller) {
                  if (chunk.type === "error" && !input.abort.aborted && isPinnedResponsesModel(language as object) && (JSONParseError.isInstance(chunk.error) || TypeValidationError.isInstance(chunk.error))) {
                    try { markMalformedResponse(chunk.error,managedScope,ids,input.abort,language as object) } catch {}
                  }
                  if (chunk.type==='finish' && typeof chunk.finishReason?.unified==='string' && chunk.finishReason.unified!=='error' && !input.abort.aborted) {
                    try {
                      const metadata=markSdkSuccessStep(managedScope,ids,chunk.providerMetadata)
                      controller.enqueue({...chunk,...(metadata ? {providerMetadata:metadata} : {})})
                    } catch { controller.enqueue(chunk) }
                  } else controller.enqueue(chunk)
                } }))}
              },
              async transformParams(args) {
                if (args.type === "stream") {
                  // @ts-expect-error
                  args.params.prompt = ProviderTransform.message(args.params.prompt, input.model, options)
                  if (managedScope) {
                    const { prompt, tools: resolvedTools, ...finalOptions } = args.params
                    args.params.prompt = await appendFinalCodeContext({
                      sessionID: input.sessionID, messages: prompt as unknown as ModelMessage[], system,
                      options: finalOptions as Record<string, unknown>, tools, activeTools, resolvedTools,
                      usable: contextWindow({ cfg, model: input.model }).usable, abort: input.abort,
                      ephemeral: input.ephemeral, agentID: input.agentID, actorExecution: input.requestContext?.actorExecution, providerFrame: true,
                    }) as typeof prompt
                  }
                  const knownMaintenance = !input.ephemeral && input.requestContext?.actorExecution?.actorID === "main" &&
                    input.requestContext.actorExecution.rootOperationID && input.requestContext.actorExecution.innerTurnID &&
                    input.model.api.npm === "@ai-sdk/openai" && maintenanceCompactionWire(input.maintenanceWire,args.params.prompt)
                  const codexInstructions = (args.params.providerOptions?.openai as Record<string,unknown> | undefined)?.instructions
                  maintenanceCandidate = knownMaintenance && input.maintenanceWire && managedMetadata?.familyID === "openai" &&
                    managedMetadata.billingLane === "subscription" && managedMetadata.adapterID === "openai-responses" &&
                    isOpenaiOauth && typeof codexInstructions === "string" &&
                    codexInstructions === options.instructions
                    ? {...input.maintenanceWire,wireProfile:"codex-responses",instructionsSHA256:createHash("sha256").update(codexInstructions,"utf8").digest("hex")}
                    : knownMaintenance && input.maintenanceWire && managedMetadata?.familyID === "openai" &&
                      managedMetadata.billingLane === "metered_api" && managedMetadata.adapterID === "openai-chat" ? {...input.maintenanceWire,wireProfile:"openai-chat"} : undefined
                  nativeDeliveryCandidate = !input.ephemeral && input.requestContext?.actorExecution?.actorID === "main" && input.requestContext.actorExecution.rootOperationID && input.requestContext.actorExecution.innerTurnID && input.model.api.npm === "@ai-sdk/openai"
                    ? nativeSingleUserWire(input.nativeTextSources, args.params.prompt) : undefined
                  recallDeliveryCandidate = !input.ephemeral && input.requestContext?.actorExecution?.actorID === "main" && input.requestContext.actorExecution.rootOperationID && input.requestContext.actorExecution.innerTurnID && input.model.api.npm === "@ai-sdk/openai"
                    ? recallToolFrame(input.recallToolSource, args.params.prompt) : undefined
                  const mainContext = !input.ephemeral && input.requestContext?.actorExecution?.actorID === "main" && input.requestContext.actorExecution.rootOperationID && input.requestContext.actorExecution.innerTurnID
                  const knownContext = mainContext && ((input.model.api.npm === "@ai-sdk/openai" && managedMetadata?.familyID === "openai" &&
                    ((prefaceInput?.wireProfile === "openai-chat" && managedMetadata.billingLane === "metered_api" && managedMetadata.adapterID === "openai-chat" && info?.type === "api") ||
                     (prefaceInput?.wireProfile === "codex-responses" && managedMetadata.billingLane === "subscription" && managedMetadata.adapterID === "openai-responses" && info?.type === "oauth"))) ||
                    (prefaceInput?.wireProfile === "compatible-chat" && !!compatibleContextEndpoint && input.model.api.npm === "@ai-sdk/openai-compatible" &&
                     managedMetadata?.familyID === "openai-compatible" && managedMetadata.adapterID === "openai-chat" && managedMetadata.billingLane === "local" &&
                     managedScope?.credentialRequired === false && managedScope.connectionID === managedMetadata.connectionID))
                  const checkedContext = knownContext ? contextPrefaceFrame(prefaceInput, args.params.prompt) : undefined
                  contextDeliveryCandidate = checkedContext?.wireProfile === "compatible-chat" ? {...checkedContext,expectedURL:compatibleContextEndpoint} : checkedContext
                  const knownStanding = !input.ephemeral && input.requestContext?.actorExecution?.actorID === 'main' && input.requestContext.actorExecution.rootOperationID && input.requestContext.actorExecution.innerTurnID && input.model.api.npm === '@ai-sdk/openai' && managedMetadata?.familyID === 'openai' &&
                    ((input.standingGuidance?.wireProfile === 'openai-chat' && managedMetadata.billingLane === 'metered_api' && managedMetadata.adapterID === 'openai-chat' && info?.type === 'api') ||
                     (input.standingGuidance?.wireProfile === 'codex-responses' && managedMetadata.billingLane === 'subscription' && managedMetadata.adapterID === 'openai-responses' && info?.type === 'oauth'))
                  standingCandidate = knownStanding && input.standingGuidance?.wireProfile === 'codex-responses'
                    ? (typeof codexInstructions === 'string' && codexInstructions === options.instructions && codexInstructions === system.join('\n')
                        ? standingGuidanceFrame(input.standingGuidance,[{role:'system',content:codexInstructions}]) : undefined)
                    : knownStanding ? standingGuidanceFrame(input.standingGuidance,args.params.prompt) : undefined
                  membershipCandidate = !input.ephemeral && !isOpenaiOauth && input.nativeMembership && sameNativeMembershipIdentity(input.nativeMembership.identity,input.requestContext?.actorExecution) && managedMetadata?.familyID==='openai' && managedMetadata.adapterID==='openai-chat' && managedMetadata.billingLane==='metered_api' && info?.type==='api' && item.options?.fetch===undefined && isPinnedChatModel(language as object) ? nativeMembershipFrame(input.nativeMembership,args.params.prompt,language as object) : undefined
                  sourceFinalFrame = undefined
                  if (input.sourceAttempt && input.prebuiltSystem && membershipCandidate) {
                    const originalSystem=input.prebuiltSystem
                    const capsule=prefixAssemblyHandle(originalSystem)
                    if (capsule) {
                      const current=()=>readPrefixAssembly(capsule)
                      const proof=current()
                      if (proof && proof.unknown.length===0 && membershipCandidate.sources.length===1 &&
                          membershipCandidate.sources[0]?.role==='user' && membershipCandidate.roles.filter(role=>role!=='system').length===1)
                        sourceFinalFrame={frame:membershipCandidate,system:originalSystem,current}
                    }
                  }
                }
                if (args.type === "stream" && input.sourceAttempt && input.prebuiltSystem && !input.ephemeral && !isOpenaiOauth &&
                    input.model.api.npm === '@ai-sdk/openai-compatible' && managedMetadata?.familyID === 'openai-compatible' &&
                    managedMetadata.adapterID === 'openai-chat' && managedScope?.connectionID === managedMetadata.connectionID && item.options?.fetch===undefined) {
                  const endpoint=pinnedCompatibleChatEndpoint(language as object), layout=sourceAttemptLayout(input.sourceAttempt)
                  const capsule=prefixAssemblyHandle(input.prebuiltSystem)
                  const current=capsule ? ()=>readPrefixAssembly(capsule) : undefined
                  const prefix=current?.(), full=layout ? [...layout.layout,...layout.generated.map(g=>({position:g.position,sha256:g.sha256,synthetic:true}))] : undefined
                  const matchingLayout = !!(endpoint && layout && full && prefix && prefix.unknown.length===0 && sourceLayoutMessages(full,args.params.prompt,false))
                  if(matchingLayout && endpoint && layout && prefix && current)
                    sourceFinalFrame={version:2,model:language as object,endpoint,system:input.prebuiltSystem,layout,current}
                  try { log.debug("source_frame_eligibility",{endpoint:!!endpoint,layout:!!layout,prefix:!!prefix,known:!!prefix && prefix.unknown.length===0,matchingLayout,frame:!!sourceFinalFrame}) } catch { /* diagnostic only */ }
                }
                if (args.type === "stream" && !input.adaptiveStyle && input.onContextFrame && !input.ephemeral) {
                  try {
                    const frame=contextFrame({
                      providerID: input.model.providerID, modelID: input.model.id,
                      identity: { sessionID: input.sessionID,
                        ...(input.requestContext?.actorExecution ? {
                          actorID: input.requestContext.actorExecution.actorID,
                          executionRevision: input.requestContext.actorExecution.executionRevision,
                          rootOperationID: input.requestContext.actorExecution.rootOperationID,
                          innerTurnID: input.requestContext.actorExecution.innerTurnID,
                        } : {}) },
                      window: contextWindow({ cfg, model: input.model }), sources: input.contextSources?.sources,
                      omittedSourceCount: input.contextSources?.omittedSourceCount,
                      sequence: { attempt: input.contextSequence?.attempt ?? 1, transform: input.contextSequence ? ++input.contextSequence.transform : 1 },
                      prompt: args.params.prompt, instructions: isOpenaiOauth ? ((args.params.providerOptions?.openai as Record<string, unknown> | undefined)?.instructions ?? Symbol("unavailable")) : undefined,
                      tools: args.params.tools,
                    })
                    const handle=input.ownedAttemptCell?.handle
                    if (handle) await issueAnthropicCallback(handle.probe,()=>input.onContextFrame!(frame))
                    else await input.onContextFrame(frame)
                  } catch (error) {
                    l.warn("context inspection unavailable", { error: String(error) })
                  }
                }
                return args.params
              },
            },
          ],
        }),
        experimental_telemetry: {
          isEnabled: cfg.experimental?.openTelemetry,
          functionId: input.ephemeral ? "title.llm" : "session.llm",
          tracer: telemetryTracer,
          metadata: {
            userId: cfg.username ?? "unknown",
            ...(input.requestID ? { requestId: correlationID } : { sessionId: input.sessionID }),
          },
        },
      })
    })

    const stream: Interface["stream"] = (input) => {
      // Build the scoped stream for one attempt. `dropAssistantPrefill` forces
      // run() to hard-prune the trailing assistant prefill before send — used only
      // by the reactive one-shot retry below.
      const attempt = (dropAssistantPrefill: boolean) =>
        Stream.scoped(
          Stream.unwrap(
            Effect.gen(function* () {
              const ownedAttemptCell: NonNullable<StreamRequest["ownedAttemptCell"]> = {}
              const ctrl = yield* Effect.acquireRelease(
                Effect.sync(() => new AbortController()),
                (ctrl,exit) => ownedAttemptCell.handle
                  ? Effect.promise(async () => {
                      const handle=ownedAttemptCell.handle!
                      const probe=handle.probe
                      try {
                        const drained=await drainAnthropicCallbacks(probe)
                        const okay=drained && handle.current() && !ctrl.signal.aborted && !probe.signal?.aborted &&
                          !(Exit.isFailure(exit) && Cause.hasInterrupts(exit.cause)) && !ownedAttemptCell.invalid && ownedAttemptCell.returned===true &&
                          !!ownedAttemptCell.error && ownedAttemptCell.sdkError===ownedAttemptCell.error && probe.adapterError===ownedAttemptCell.error && probe.originalEOF===true && probe.sends===1 && probe.state==="observed"
                        if (okay) probe.localOutcome={error:ownedAttemptCell.error!}
                        else handle.deny()
                      }
                      finally {
                        if (probe.signal && probe.onAbort) probe.signal.removeEventListener("abort",probe.onAbort)
                        if (!probe.localOutcome && ownedAttemptCell.scope) retireCaptureAttempt(ownedAttemptCell.scope,probe)
                        settleSourceStreamBeforeTeardown(input.sourceAttempt,ctrl.signal,Exit.isSuccess(exit))
                        ctrl.abort()
                      }
                    })
                  : Effect.sync(() => { settleSourceStreamBeforeTeardown(input.sourceAttempt,ctrl.signal,Exit.isSuccess(exit)); ctrl.abort() }),
              )
              const attemptRef = yield* Ref.make(0)

              const publishRetryEvent = (error: unknown, nextAttempt: number) =>
                Effect.gen(function* () {
                  log.debug("retry attempt", {
                    sessionID: input.sessionID,
                    messageID: input.user.id,
                    attempt: nextAttempt,
                    reason: error instanceof Error ? error.message : String(error),
                  })
                  if (nextAttempt > 10) return
                  const delayMs = Math.min(500 * 2 ** (nextAttempt - 1), 300_000)
                  yield* Effect.promise(() =>
                    Bus.publish(Session.Event.RetryAttempt, {
                      sessionID: SessionID.make(input.sessionID),
                      messageID: input.user.id,
                      attempt: nextAttempt,
                      maxAttempts: 10,
                      reason: error instanceof Error ? error.message : String(error),
                      nextDelayMs: delayMs,
                    })
                  )
                })

              const streamWithTelemetry = run({ ...input, abort: ctrl.signal, dropAssistantPrefill, ownedAttemptCell }).pipe(
                Effect.tapError((error) => {
                  if (!isTransientCapacityError(error)) return Effect.void
                  return Ref.updateAndGet(attemptRef, (n) => n + 1).pipe(
                    Effect.flatMap((nextAttempt) => publishRetryEvent(error, nextAttempt))
                  )
                })
              )

              const result = ManagedProvider.enabled()
                ? yield* streamWithTelemetry
                : yield* streamWithTelemetry.pipe(
                    Effect.retry({
                      while: isTransientCapacityError,
                      schedule: persistentRetrySchedule,
                    }),
                  )

              // Structurally identical to the pre-guard stream: a bare scoped
              // stream over the provider's fullStream. No per-event combinator, no
              // extra catch layer — so the normal (non-error) event flow and the
              // AbortController scope teardown are exactly as before. The reactive
              // prefill retry is layered lazily below and only pays a cost when an
              // actual error surfaces.
              const iterable = ownedAttemptCell.handle ? {
                [Symbol.asyncIterator]() {
                  const reader=result.fullStream[Symbol.asyncIterator]()
                  return {
                    async next() {
                      try {
                        const item=await reader.next()
                        if (item.done) ownedAttemptCell.invalid=true
                        else if (item.value.type === "error" && item.value.error && typeof item.value.error === "object" && !ownedAttemptCell.error) ownedAttemptCell.error=item.value.error
                        else if (item.value.type !== "start") ownedAttemptCell.invalid=true
                        return item
                      } catch (error) { ownedAttemptCell.invalid=true; throw error }
                    },
                    async return() {
                      try { if (!reader.return) { ownedAttemptCell.invalid=true; return {done:true as const,value:undefined} }; const value=await reader.return(); ownedAttemptCell.returned=true; return value }
                      catch (error) { ownedAttemptCell.invalid=true; throw error }
                    },
                  }
                },
              } : result.fullStream
              return Stream.fromAsyncIterable(iterable, (e) =>
                e instanceof Error ? e : new Error(String(e)),
              )
            }),
          ),
        )

      // Promote a prefill-rejection 400 — which arrives as an in-band
      // `{ type: "error", error }` event, not a stream fault — into a stream
      // FAILURE so the reactive retry can catch it. `Stream.flatMap` short-circuits
      // every non-matching event straight through with a pure `Stream.succeed` (no
      // per-event Effect fiber, unlike `Stream.mapEffect`), and the failing branch
      // is only ever constructed for the specific error event. On a clean stream
      // this is a transparent passthrough.
      const promotePrefillRejection = (stream: Stream.Stream<Event, Error, never>) =>
        stream.pipe(
          Stream.flatMap((event) =>
            event.type === "error" && ProviderTransform.isAssistantPrefillRejection(event.error)
              ? Stream.fail(event.error instanceof Error ? event.error : new Error(String(event.error)))
              : Stream.succeed(event),
          ),
        )

      // Reactive prefill-rejection backstop. The proactive
      // ProviderTransform.ensureTrailingUserMessage guard runs on every request,
      // so we should never send a trailing assistant prefill and this path should
      // effectively never fire. It remains as defense-in-depth: if any path still
      // slips a trailing assistant through to the wire and the backend 400s with
      // "does not support assistant message prefill", we key off that deterministic
      // error body — not the model id — and retry exactly ONCE with the prefill
      // hard-pruned. Guarded to a single reprune so a persistent failure surfaces
      // the retry's OWN error, falling back to the original prefill cause only when
      // the resend is again prefill-rejected.
      return promotePrefillRejection(attempt(false)).pipe(
        Stream.catchCause((primaryCause) => {
          if (!ProviderTransform.isAssistantPrefillRejection(Cause.squash(primaryCause)))
            return Stream.failCause(primaryCause)
          // Pruned resend passes events through untouched: any residual error flows
          // to the processor and surfaces normally (no promotion, no loop).
          return attempt(true).pipe(
            Stream.catchCause((retryCause) =>
              ProviderTransform.isAssistantPrefillRejection(Cause.squash(retryCause))
                ? Stream.failCause(primaryCause)
                : Stream.failCause(retryCause),
            ),
          )
        }),
      )
    }

    return Service.of({ stream, buildSystemArray })
  }),
)

export const layer = live.pipe(Layer.provide(Permission.defaultLayer))

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Auth.defaultLayer),
    Layer.provide(Config.defaultLayer),
    Layer.provide(Provider.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(ActorRegistry.defaultLayer),
    Layer.provide(Memory.defaultLayer),
  ),
)

export function selectFinalTools(input: Pick<StreamInput, "tools" | "activeTools" | "agent" | "permission" | "user">) {
  const tools=resolveTools(input)
  const requestedActiveTools=new Set(input.activeTools ?? Object.keys(tools))
  const activeTools=Object.keys(tools).filter(name=>name!=="invalid" && requestedActiveTools.has(name))
  return {tools,activeTools}
}

function resolveTools(input: Pick<StreamInput, "tools" | "activeTools" | "agent" | "permission" | "user">) {
  const disabled = Permission.disabled(
    Object.keys(input.tools),
    Agent.runtimePermission(input.agent, input.permission),
  )
  return Record.filter(
    input.tools,
    (_, key) =>
      input.user.tools?.[key] !== false &&
      (!disabled.has(key) || (key === MCP_TOOL_SEARCH_ID && input.activeTools?.includes(key) === true)),
  )
}

// Check if messages contain any tool-call content
// Used to determine if a dummy tool should be added for LiteLLM proxy compatibility
export function hasToolCalls(messages: ModelMessage[]): boolean {
  for (const msg of messages) {
    if (!Array.isArray(msg.content)) continue
    for (const part of msg.content) {
      if (part.type === "tool-call" || part.type === "tool-result") return true
    }
  }
  return false
}

export * as LLM from "./llm"
