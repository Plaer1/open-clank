import { imageViewFrameCurrent, imageViewFramePositions, type ImageViewReceipt, type ImageViewPosition } from './model-image-view'
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
import { HostModelTransport } from "../provider/host-transport"
import { bindHostError, inheritHostError } from "@/error/host-registry"
import { createHash } from "node:crypto"
import { Provider, ProviderError } from "@/provider"
import { openCodeGoSessionHeaders } from "@/provider/opencode-go"
import { Log } from "@/util"
import { errorMessage } from "@/util/error"
import { Context, Duration, Effect, Layer, Record, Cause, Exit } from "effect"
import * as Stream from "effect/Stream"
import { streamText, wrapLanguageModel, type ModelMessage, type Tool, tool, jsonSchema, NoSuchToolError } from "ai"
import { mergeDeep, pipe } from "remeda"
import { GitLabWorkflowLanguageModel } from "gitlab-ai-provider"
import { ProviderTransform } from "@/provider"
import { Config } from "@/config"
import { Instance } from "@/project/instance"
import { Agent } from "@/agent/agent"
import { MessageV2 } from "./message-v2"
import { Plugin } from "@/plugin"
import { SystemPrompt } from "./system"
import { Permission } from "@/permission"
import { PermissionID } from "@/permission/schema"
import { Bus } from "@/bus"
import { Wildcard, ToolCompat } from "@/util"
import { asSchema } from "@ai-sdk/provider-utils"
import { SessionID } from "@/session/schema"
import * as Session from "@/session/session"
import { Auth } from "@/auth"
import { Installation } from "@/installation"
import { InstallationVersion } from "@/installation/version"
import { EffectBridge } from "@/effect"
import { Global } from "@/global"
import * as Option from "effect/Option"
import * as OtelTracer from "@effect/opentelemetry/Tracer"
import { ActorRegistry } from "@/actor/registry"
import { isRetryableTransientError } from "./retry"
import * as SessionRetry from "./retry"
import { MCP_TOOL_SEARCH_ID, MCP_TOOL_USE_ID } from "@/tool/mcp-tool-search"
import { TOOL_SCRIPT_EXCLUDED } from "@/tool/tool-script-ref"
import { Flag } from "@/flag/flag"
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
 * Match transient errors that max-mode local retries should retry.
 *
 * - HTTP 429 / 5xx / 529 — capacity / overload responses
 * - ECONNRESET / EPIPE / ETIMEDOUT — network errors typically caused by
 *   stale keep-alive sockets or upstream proxy timeouts
 * - "SSE read timed out" — `provider.ts:wrapSSE` chunk-timeout fired
 *   (configured per-provider via `chunkTimeout` in openclank.json). This
 *   is HTTP-byte-level: keep-alive comments still count as activity, so
 *   the error only fires when the underlying TCP stream is genuinely dead.
 *
 * Authentication failures, client errors (400, 404, 422), and user-
 * initiated aborts are NOT retryable.
 *
 * @deprecated Use `isRetryableTransientError` from `./retry` directly.
 * Kept as a 1-line wrapper to preserve the existing export name.
 */
export function isTransientCapacityError(error: unknown): boolean {
  return isRetryableTransientError(error)
}

/** Guidance names the actual scoped authority without inventing a file-memory root. */
function buildManagedMemoryInstructions(): string {
  return "# Memory and operational continuity\nDurable knowledge is governed by current Memery policy and any advertised tools and receipts. Follow the user's and repository's agreements for plans and notes. Session checkpoints, task records, and notes support operational continuity."
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
  browserSnapshotRepeat?: Omit<import("@/provider/logging-transport").BrowserSnapshotCarrier,"model">
  messages: ModelMessage[]
  small?: boolean
  tools: Record<string, Tool>
  activeTools?: string[]
  retries?: number
  toolChoice?: "auto" | "required" | "none"
  agentID?: string
  requestContext?: { actorExecution: ActorExecutionIdentity }
  /** Shared by every retry ladder in the admitted logical operation. */
  interactiveRetryBudget?: import("./retry").InteractiveBudget
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
  /** Private request-local conversion proof; no setting or authority by itself. */
  imageViewFrame?: { receipts:readonly ImageViewReceipt[]; current:()=>boolean; claim:()=>boolean }
  contextSources?: { sources: ContextSource[]; omittedSourceCount: number }
  contextSequence?: { attempt: number; transform: number }
  onContextFrame?: (frame: ContextFrame) => Promise<void>
  /** Isolated one-shot generation (titles and similar helpers). */
  ephemeral?: boolean
  /** Correlates an isolated request without changing the owning session. */
  requestID?: string
  mergeTurnContextIntoLastUser?: boolean
  /** Keep an appended control prompt last while applying provider-specific turn context to the conversation before it. */
  mergeTurnContextBeforeLastMessage?: boolean
  /**
   * Propose-only / ensemble draws: skip Session.Event.RetryAttempt on request-phase
   * ladders. Narrower than `ephemeral` (which also skips plugins, affinity headers,
   * OTel functionId, and system assembly). session.status is already processor-owned.
   */
  quietRetryDiagnostics?: boolean
  retryScope?: "max-candidate" | "max-judge"
  assistantMessageID?: string
  /** Request-ladder visible wait. Processor owns SessionStatus (shared with GET /session/status). */
  onRetryStatus?: (info: {
    attempt: number
    message: string
    next: number
    phase?: "request" | "stream"
    scope?: "request" | "live-step" | "max-candidate" | "max-judge"
    kind: SessionRetry.RetryKind
    hostCode?: string
  }) => Effect.Effect<void>
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

/** Open Clank managed lane: the Host persona (`user.system`, append mode) stays in the system prefix, where
 * its provenance is witnessed. Everywhere else upstream's turn-context tail below carries it. */
function systemInPrefix(user: MessageV2.User): boolean {
  return ManagedProvider.enabled() && user.systemMode !== "replace-agent" && !!user.system
}

/** Convert per-turn context into the final model-visible user segment. */
export function turnContextMessages(user: MessageV2.User): ModelMessage[] {
  if (user.systemMode === "replace-agent") return []
  const context = user.system?.trim()
  if (!context) return []
  return [{
      role: "user",
      content: `<system-reminder>\n${context}\n</system-reminder>`,
  }]
}

export function appendTurnContext(messages: ModelMessage[], user: MessageV2.User, mergeWithLastUser = false) {
  const context = turnContextMessages(user)
  if (!context.length) return messages
  const last = messages.at(-1)
  if (!mergeWithLastUser || !last || last.role !== "user" || typeof last.content !== "string") {
    return [...messages, ...context]
  }
  return [...messages.slice(0, -1), { ...last, content: last.content + "\n\n" + context[0].content }]
}

function appendTurnContextBeforeLastMessage(messages: ModelMessage[], user: MessageV2.User) {
  const tail = messages.at(-1)
  if (!tail) return appendTurnContext(messages, user, true)
  const head = messages.slice(0, -1)
  const userIndex = head.findLastIndex((message) => message.role === "user")
  if (userIndex < 0) return appendTurnContext(messages, user, true)
  return [
    ...appendTurnContext(head.slice(0, userIndex + 1), user, true),
    ...head.slice(userIndex + 1),
    tail,
  ]
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
    ephemeral?: boolean
  }) => Effect.Effect<string[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/LLM") {}

const live: Layer.Layer<
  Service,
  never,
  | Auth.Service
  | Bus.Service
  | Config.Service
  | Provider.Service
  | Plugin.Service
  | Permission.Service
  | ActorRegistry.Service
> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const auth = yield* Auth.Service
    const bus = yield* Bus.Service
    const config = yield* Config.Service
    const provider = yield* Provider.Service
    const plugin = yield* Plugin.Service
    const perm = yield* Permission.Service
    const actorReg = yield* ActorRegistry.Service

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
      // "Is this a main/peer actor" — the single judgement memory-instructions
      // and replace-agent base override share. Memory system is ALWAYS taught to
      // these actors; only checkpoint-write copy inside the block is
      // flag-filtered. Injected only for actors whose context the checkpoint
      // flow serves — main + peer. Subagents (explore/general/…) run in the
      // SHARED sessionID (F37 slices) but are NOT main/peer; system-spawned
      // actors (checkpoint-writer et al.) and ephemeral one-shots (title gen)
      // likewise are not. Shares the exact `servesCheckpoint` judgement with
      // SessionPrune.fireCheckpoints so "who owns a checkpoint" and "who is
      // taught about memory" cannot drift apart on the actor axis.
      const servesCheckpoint =
        !input.ephemeral && (yield* actorReg.servesCheckpoint(SessionID.make(input.sessionID), input.agentID))

      // replace-agent replaces the PRIMARY line's base prompt with a session-level
      // system (host execution-profile base). It is a main/peer concern: a
      // subagent shares the sessionID and therefore inherits `systemMode` on the
      // resolved session prompt, but must keep its OWN `agent.prompt` — else
      // explore/general/title/… get their identity clobbered by the parent base.
      // So the base override only fires when this actor `servesCheckpoint`; every
      // other actor falls back to SystemPrompt.agent(self).
      const replaceAgent = input.user.systemMode === "replace-agent" && servesCheckpoint

      const system: string[] = []
      const ownedAgent = input.sourceWitness && !replaceAgent ? SystemPrompt.agentWithProvenance(input.agent,input.model) : undefined
      system.push(
        [
          // replace-agent is the session's base system prompt, so it must occupy
          // the same leading position as the agent prompt it replaces.
          ...(replaceAgent && input.user.system
            ? [input.user.system]
            : (ownedAgent?.strings ?? SystemPrompt.agent(input.agent, input.model, input.user.harness))),
          // Open Clank managed lane: the Host persona stays in the system prefix (see systemInPrefix).
          ...(systemInPrefix(input.user) ? [input.user.system] : []),
        ]
          .filter((x) => x)
          .join("\n"),
      )

      // Only the scoped authority and actually advertised tools define memory behavior.
      let managedMemoryText: string | undefined
      const teachesMemory = servesCheckpoint && input.agent.name !== "chat"
      if (teachesMemory) {
        managedMemoryText = buildManagedMemoryInstructions()
        system.push(managedMemoryText)
      }

      // Plugins transform the stable base before the caller-controlled tail.
      const transformOutput = { system }
      if (input.sourceWitness) plugin.requestEmptySystemTransform?.(transformOutput)
      yield* plugin.trigger(
        "experimental.chat.system.transform",
        { sessionID: input.sessionID, model: input.model },
        transformOutput,
      )

      // Preserve the plugin-observed array for its unchanged-transform witness;
      // the caller-controlled tail belongs only to the assembled output.
      const finalSystem = [...system, ...input.system]

      // Collapse to a single system message. The historical 2-part split existed
      // only to keep a byte-stable cache prefix separate from the memory block's
      // per-session paths — but within a session those paths are fixed, so the
      // whole thing is stable and one block caches just as well. One message also
      // keeps the fork-prefix parity invariant trivial (nothing to misalign) and
      // spares subagents/providers a stray extra system turn. Join with a blank
      // line (\n\n) so adjacent markdown sections (base prompt, "# Memory system")
      // don't run together into one heading.
      const output = finalSystem.length <= 1 ? finalSystem : [finalSystem.filter((x) => x).join("\n\n")]
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
          ...(input.user.system && !qualifiedPersona ? ["user_system"] : []), ...(teachesMemory && (managedMemoryText === undefined || !instructionSnapshot) ? ["memory_instructions"] : [])]
        try { log.debug("source_prefix_eligibility",{environment:true,agent:true,skills:qualifiedSkill,instruction:!!instructionSnapshot,
          additions:additionsMatch,persona:!userSystem || !!qualifiedPersona,transform:noTransform,memory:!teachesMemory || managedMemoryText !== undefined}) } catch { /* diagnostic only */ }
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
      const isWorkflow = language instanceof GitLabWorkflowLanguageModel
      // Workflow connectors mutate shared session/tool-executor state and may
      // ask permissions. They have no safe isolated title adapter.
      if (input.ephemeral && isWorkflow) return yield* Effect.fail(new Error("Ephemeral workflow generation is unsupported"))
      const providerSystem =
        input.user.systemMode !== "replace-agent" && !systemInPrefix(input.user) && (isOpenaiOauth || isWorkflow) && input.user.system?.trim()
          ? [...system, input.user.system]
          : system
      if (isOpenaiOauth) options.instructions = providerSystem.join("\n")
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
      const requestMessagesWithContext = systemInPrefix(input.user)
        ? requestMessages
        : input.mergeTurnContextBeforeLastMessage
          ? appendTurnContextBeforeLastMessage(requestMessages, input.user)
          : appendTurnContext(requestMessages, input.user, input.mergeTurnContextIntoLastUser)
      const messages = isOpenaiOauth
        ? requestMessages
        : isWorkflow
          ? requestMessages
          : [
              ...providerSystem.map(
                (x): ModelMessage => ({
                  role: "system",
                  content: x,
                }),
              ),
              ...requestMessagesWithContext,
            ]
      const prefaceInput = input.contextPreface && requestMessages.length === input.contextPreface.count
        ? contextPrefaceFrame({...input.contextPreface,index:input.contextPreface.index + messages.length - requestMessages.length,count:messages.length},messages)
        : undefined

      const defaults = {
        temperature: input.model.capabilities.temperature ? input.agent.temperature : undefined,
        topP: input.agent.topP,
        // Only plugins (chat.params) set topK; no harness default.
        topK: undefined as number | undefined,
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
          temperature: input.model.capabilities.temperature ? input.agent.temperature : undefined,
          topP: input.agent.topP,
          topK: undefined,
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
          const t = tools[toolName]
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
              ...providerSystem.map((content) => ({ role: "system", content })),
              ...(isOpenaiOauth || isWorkflow ? requestMessages : requestMessagesWithContext),
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
      let imageViewPositions: readonly ImageViewPosition[] | undefined
      let imageContextFrame: ContextFrame | undefined
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
      const execution = yield* HostModelTransport.ExecutionScope
      const executionScope = !input.ephemeral && (!input.agentID || input.agentID === "main")
        && execution?.sessionID === input.sessionID && execution.userMessageID === input.user.id
        ? { executionAttempt: execution.attempt, directory: execution.directory } : {}
      const result = streamText({
        onStepFinish(step) {
          if (input.ownedAttemptCell?.handle) input.ownedAttemptCell.invalid=true
          const metadata=step.providerMetadata && Object.fromEntries(Object.entries(step.providerMetadata).filter(([key])=>key!=='_openclankSuccessStep'))
          Session.getUsage({ model: input.model, usage: step.usage, metadata,
            captureScope: managedScope, captureDispatchIDs: sdkDispatchIDs, mainReply: !input.ephemeral })
        },
        onError({ error }) {
          const cause = error
          if (input.ownedAttemptCell?.handle) {
            if (!cause || typeof cause!=="object" || input.ownedAttemptCell.sdkError) input.ownedAttemptCell.invalid=true
            else input.ownedAttemptCell.sdkError=cause
          }
          if (!input.abort.aborted) recordSdkFailure(cause, managedScope, sdkDispatchIDs)
          l.debug("streamText error", {
            messageID: input.user.id,
            error: errorMessage(error),
            elapsedMs: Date.now() - streamStartTs,
          })
          // The provider error carries requestBodyValues (the whole prompt), so
          // log only its cause summary.
          l.error("stream error", {
            causes: ProviderError.summarizeCause(error),
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
              error: NoSuchToolError.isInstance(failed.error)
                ? new NoSuchToolError({
                    toolName: failed.toolCall.toolName,
                    availableTools: activeTools,
                  }).message
                : failed.error.message,
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
          // OpenCode Go only (provider/opencode-go). Reuse sessionID — including
          // ephemeral title/summary/compaction — under the name that gateway expects.
          // Same value as x-session-affinity; no new identifier. Other providers
          // must not gain this header. Detect via model + provider.options.baseURL.
          ...openCodeGoSessionHeaders(input.model, input.sessionID, item.options),
          ...(!input.ephemeral ? { "x-session-affinity": input.sessionID } : {}),
          ...(!input.ephemeral && input.parentSessionID ? { "x-parent-session-id": input.parentSessionID } : {}),
          ...input.model.headers,
          ...headers,
          "User-Agent": `mimocode/${InstallationVersion}`,
        },
        // Keep one SDK-level retry for a failure before response headers. The
        // processor owns the persistent stream retry budget below this layer.
        // Managed mode centralizes retry/failover in the account-aware
        // processor. An SDK-internal retry is invisible and would reuse a
        // credential after the host has cooled or revoked that account.
        maxRetries: ManagedProvider.enabled() ? 0 : (input.retries ?? 0),
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
                      profileInputs: {persona: systemInPrefix(input.user) ? input.user.system : undefined,
                        guidance: input.standingGuidance, recall: prefaceInput},
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
                    if (input.imageViewFrame && frame.sources.some(source=>source.kind==='media_input' && source.derivedView)) imageContextFrame=structuredClone(frame)
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
                const invoke=()=>observeAnthropicDoStream(language as object, () => withSdkDispatches(managedScope, ids, doStream, nativeDeliveryCandidate, input.onNativeTextDelivery, input.requestContext?.actorExecution, recallDeliveryCandidate, input.onRecallAdmission, contextDeliveryCandidate, input.onContextDelivery, maintenanceCandidate, input.onMaintenanceWire, standingCandidate, input.onStandingGuidance, membershipCandidate, input.onNativeMembership, input.sourceAttempt, input.abort, sourceFinalFrame, input.sameRequestRows ? {...input.sameRequestRows,model:language as object,current:()=>!input.abort.aborted && input.sameRequestRows!.current()} : undefined, input.browserSnapshotRepeat ? {...input.browserSnapshotRepeat,model:language as object,current:()=>!input.abort.aborted && input.browserSnapshotRepeat!.current()} : undefined,
                  input.imageViewFrame && managedScope && input.requestContext?.actorExecution?.rootOperationID && input.requestContext.actorExecution.innerTurnID
                    ? {receipts:input.imageViewFrame.receipts,positions:imageViewPositions ?? [],current:()=>!input.abort.aborted && input.imageViewFrame!.current(),claim:input.imageViewFrame.claim,model:language as object,
                       observe:()=>{const frame=imageContextFrame;if(frame && input.onContextFrame && input.imageViewFrame?.current() && !input.abort.aborted)
                         void input.onContextFrame({...frame,stage:'model_view_dispatch_observed',observedAt:Date.now()}).catch(()=>{})},
                       authorize:()=>{const id=input.requestContext!.actorExecution!;return ManagedProvider.admitOriginalIngress({sessionID:id.sessionID,actorID:id.actorID,executionRevision:id.executionRevision,rootOperationID:id.rootOperationID!,innerTurnID:id.innerTurnID!},managedScope,true)}} : undefined))
                const handle=input.ownedAttemptCell?.handle
                const ownedDoStream=()=>handle && handle.current() ? withAnthropicFailureProbe(handle.probe,invoke) : invoke()
                // Upstream's host transport scope and host-error binding wrap our dispatch capture.
                return HostModelTransport.modelCall({
                ...executionScope,
                sessionID: input.sessionID, userMessageID: input.user.id,
                assistantMessageID: input.assistantMessageID,
                providerID: input.model.providerID, modelID: input.model.id, sdk: input.model.api.npm,
                agent: input.agent.name, ephemeral: !!input.ephemeral, format: input.user.format?.type,
                }, async () => {
                const forwardRaw = params.includeRawChunks === true
                params.includeRawChunks = true
                if (ManagedProvider.enabled()) {
                  const identity = input.requestContext?.actorExecution
                  if (!managedScope || !identity?.rootOperationID || !identity.innerTurnID || input.abort.aborted ||
                      !await ManagedProvider.admitOriginalIngress({sessionID:identity.sessionID, rootOperationID:identity.rootOperationID,
                        actorID:identity.actorID, executionRevision:identity.executionRevision, innerTurnID:identity.innerTurnID}, managedScope) || input.abort.aborted)
                    throw new MessageV2.AuthError({providerID:input.model.providerID, message:"Original ingress authority is unavailable"})
                }
                const result = await Promise.resolve().then(ownedDoStream).catch((error: unknown) => {
                  bindHostError(error, { providerID: input.model.providerID })
                  throw error
                })
                const reader = result.stream.getReader()
                let cancelled = false
                let pendingRaw: { frame: object; message: string } | undefined
                const stream: typeof result.stream = new ReadableStream({
                  async pull(controller) {
                    try {
                      while (true) {
                        const next = await reader.read()
                        if (cancelled) return
                        if (next.done) {
                          pendingRaw = undefined
                          reader.releaseLock()
                          controller.close()
                          return
                        }
                        if (next.value.type === "raw") {
                          const value = next.value.rawValue
                          pendingRaw = value !== null && typeof value === "object" && !Array.isArray(value) &&
                            "error" in value && value.error !== null && typeof value.error === "object" &&
                            "message" in value.error && typeof value.error.message === "string"
                            ? { frame: value, message: value.error.message } : undefined
                          if (!forwardRaw) continue
                          controller.enqueue(next.value)
                          return
                        }
                        const raw = pendingRaw
                        pendingRaw = undefined
                        if (next.value.type === "error") {
                          // Some adapters emit only the message after an adjacent raw error frame.
                          const error = typeof next.value.error === "string" && raw?.message === next.value.error
                            ? bindHostError({ ...raw.frame, type: "error", message: next.value.error }, { providerID: input.model.providerID }, { responseBody: JSON.stringify(raw.frame) })
                            : bindHostError(next.value.error, { providerID: input.model.providerID })
                          controller.enqueue({ ...next.value, error })
                          return
                        }
                        controller.enqueue(next.value)
                        return
                      }
                    } catch (error) {
                      pendingRaw = undefined
                      if (cancelled) return
                      bindHostError(error, { providerID: input.model.providerID })
                      reader.releaseLock()
                      controller.error(error)
                    }
                  },
                  async cancel(reason) {
                    cancelled = true
                    pendingRaw = undefined
                    try {
                      await reader.cancel(reason)
                    } finally {
                      reader.releaseLock()
                    }
                  },
                })
                return {...result,stream:stream.pipeThrough(new TransformStream({transform(chunk,controller) {
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
                })
              },
              async transformParams(args) {
                // `generate || stream`, matching session/prompt.ts:597. This file's
                // only SDK entrypoint is `streamText` (:599), so narrowing to
                // "stream" is not an active hole today — but it would silently drop
                // the whole transform, including the empty-content invariant, the
                // moment a non-streaming call is added here.
                if (args.type === "generate" || args.type === "stream") {
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
                      profileInputs: {persona: systemInPrefix(input.user) ? input.user.system : undefined,
                        guidance: input.standingGuidance, recall: prefaceInput},
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
                    if (input.imageViewFrame && frame.sources.some(source=>source.kind==='media_input' && source.derivedView)) imageContextFrame=structuredClone(frame)
                  } catch (error) {
                    l.warn("context inspection unavailable", { error: String(error) })
                  }
                }
                // No SDK call may carry a derived image after its source, route or
                // setting authority changes. Other SDK frame profiles are refused.
                if (input.imageViewFrame && (!managedScope || !input.requestContext?.actorExecution?.rootOperationID || !input.requestContext.actorExecution.innerTurnID || args.type !== 'stream' || input.ephemeral || input.abort.aborted ||
                    input.model.api.npm !== '@ai-sdk/openai' || managedMetadata?.familyID !== 'openai' ||
                    managedMetadata.adapterID !== 'openai-chat' || managedMetadata.billingLane !== 'metered_api' ||
                    info?.type !== 'api' || item.options?.fetch !== undefined || !isPinnedChatModel(language as object) ||
                    !imageViewFrameCurrent(args.params.prompt, input.imageViewFrame.receipts, input.imageViewFrame.current)))
                  throw new Error('Image model view is no longer current')
                imageViewPositions=input.imageViewFrame ? imageViewFramePositions(args.params.prompt,input.imageViewFrame.receipts,input.imageViewFrame.current) : undefined
                if (input.imageViewFrame && !imageViewPositions) throw new Error('Image model view position is unavailable')
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
      return { result }
    })

    const stream: Interface["stream"] = (input) => {
      const interactiveRetryBudget = input.interactiveRetryBudget ?? SessionRetry.createInteractiveBudget()
      // Build the scoped stream for one attempt. `dropAssistantPrefill` forces
      // run() to hard-prune the trailing assistant prefill before send — used only
      // by the reactive one-shot retry below.
      const attempt = (dropAssistantPrefill: boolean, allowRequestRetry: boolean) =>
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
              const result = yield* run({ ...input, abort: ctrl.signal, dropAssistantPrefill, ownedAttemptCell })

              // Restore canonical tool IDs before session processing and persistence.
              // The reactive prefill retry remains scoped to request failures.
              const iterable = ownedAttemptCell.handle ? {
                [Symbol.asyncIterator]() {
                  const reader=result.result.fullStream[Symbol.asyncIterator]()
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
              } : result.result.fullStream
              const rawStream = Stream.fromAsyncIterable(iterable, (e) =>
                inheritHostError(e, { providerID: input.model.providerID }),
              )
              let hasProviderOutput = false
              return rawStream.pipe(
                Stream.mapEffect((event) =>
                  Effect.gen(function* () {
                    if (event.type === "error") inheritHostError(event.error, { providerID: input.model.providerID })
                    if (
                      event.type === "error" &&
                      !hasProviderOutput &&
                      allowRequestRetry &&
                      // Managed mode: the account-aware processor owns retry, so the error stays in-band.
                      !ManagedProvider.enabled() &&
                      !ProviderTransform.isAssistantPrefillRejection(event.error)
                    ) {
                      const normalized = MessageV2.fromLiveError(event.error, {
                        providerID: input.model.providerID,
                        aborted: ctrl.signal.aborted,
                        allow404Retry: ProviderError.allowsModelNotFoundRetry(input.model),
                      })
                      if (SessionRetry.decide(normalized, "request", input.retryScope).retryable) return yield* Effect.fail(event.error)
                    }
                    if (event.type !== "start" && event.type !== "error") hasProviderOutput = true
                    return event
                  }),
                ),
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
      const promotePrefillRejection = (stream: Stream.Stream<Event, unknown, never>) =>
        stream.pipe(
          Stream.flatMap((event) =>
            event.type === "error" && ProviderTransform.isAssistantPrefillRejection(event.error)
              ? Stream.fail(event.error)
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
      return Stream.unwrap(
        Effect.gen(function* () {
          const retryConfig = SessionRetry.resolve(yield* config.get(), input.model.providerID)
          const retryRequest = (
            source: Stream.Stream<Event, unknown, never>,
            retryCount: number,
            startedAt?: number,
            prefillRepaired = false,
          ): Stream.Stream<Event, unknown, never> =>
            source.pipe(
              Stream.catchCause((primaryCause) => {
                const primaryError = inheritHostError(Cause.squash(primaryCause), { providerID: input.model.providerID })
                const normalized = MessageV2.fromLiveError(primaryError, { providerID: input.model.providerID, allow404Retry: ProviderError.allowsModelNotFoundRetry(input.model) })
                if (!normalized.data.hostCode && ProviderTransform.isAssistantPrefillRejection(primaryError)) {
                  if (prefillRepaired) return Stream.failCause(primaryCause)
                  return retryRequest(attempt(true, true), retryCount, startedAt, true)
                }
                // Managed mode: the account-aware processor owns retry and failover (no request ladder here).
                if (ManagedProvider.enabled()) return Stream.failCause(primaryCause)
                const decision = SessionRetry.decide(normalized, "request", input.retryScope)
                if (!decision.retryable) return Stream.failCause(primaryCause)
                const budget = SessionRetry.budgetFor(retryConfig, decision)
                const nextAttempt = retryCount + 1
                if (budget.mode === "bounded" && (budget.maxRetries ?? 0) < nextAttempt)
                  return Stream.failCause(primaryCause)
                const deadlineStart = startedAt ?? Date.now()
                const elapsed = Date.now() - deadlineStart
                const wait = SessionRetry.retryDelay(
                  nextAttempt,
                  decision,
                  budget.jitterRatio,
                  budget.initialDelayMs,
                  budget.maxDelayMs,
                )
                if (
                  budget.maxElapsedMs > 0 &&
                  (elapsed >= budget.maxElapsedMs || wait >= budget.maxElapsedMs - elapsed)
                )
                  return Stream.failCause(primaryCause)
                if (!interactiveRetryBudget.tryReserve(wait)) return Stream.failCause(primaryCause)
                return Stream.unwrap(
                  Effect.gen(function* () {
                    // Recoverable kinds (network / rate_limit / server) use persistent
                    // budgets and the request-phase ladder is often the only waiter the
                    // user can observe — processor stream retries never see the failure.
                    // The processor records it on the assistant message for every actor
                    // and publishes session.status{retry} for main, so Desktop can show
                    // the reconnect row instead of a dead spinner. Attempts stay on
                    // Session.Event.RetryAttempt as well for diagnostics.
                    // The Promise bridge would reattach ambient ALS instead of this fiber's InstanceRef.
                    if (!input.ephemeral && input.onRetryStatus) {
                      yield* input.onRetryStatus({
                        attempt: nextAttempt,
                        message: decision.message,
                        next: Date.now() + wait,
                        phase: "request",
                        scope: decision.scope,
                        kind: decision.kind,
                        hostCode: decision.hostCode,
                      }).pipe(Effect.ignore)
                    }
                    if (!input.ephemeral && !input.quietRetryDiagnostics) yield* bus.publish(Session.Event.RetryAttempt, {
                      sessionID: SessionID.make(input.sessionID),
                      messageID: input.user.id,
                      attempt: nextAttempt,
                      phaseAttempt: nextAttempt,
                      maxAttempts: budget.maxRetries ?? 0,
                      phase: "request",
                      kind: decision.kind,
                      scope: decision.scope,
                      reason: decision.message,
                      nextDelayMs: wait,
                      hostCode: decision.hostCode,
                    })
                    yield* Effect.sleep(Duration.millis(wait))
                    return retryRequest(attempt(false, true), nextAttempt, deadlineStart)
                  }),
                )
              }),
            )
          return retryRequest(promotePrefillRejection(attempt(false, true)), 0)
        }),
      )
    }

    return Service.of({ stream, buildSystemArray })
  }),
)

export const layer = live.pipe(Layer.provide(Permission.defaultLayer), Layer.provide(Bus.defaultLayer))

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Auth.defaultLayer),
    Layer.provide(Config.defaultLayer),
    Layer.provide(Provider.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(ActorRegistry.defaultLayer),
  ),
)

export function selectFinalTools(input: Pick<StreamInput, "tools" | "activeTools" | "agent" | "permission" | "user">) {
  const tools=resolveTools(input)
  const requestedActiveTools=new Set(input.activeTools ?? Object.keys(input.tools))
  const activeTools=Object.keys(tools).filter(name=>name!=="invalid" && requestedActiveTools.has(name))
  return {tools,activeTools}
}

function resolveTools(input: Pick<StreamInput, "tools" | "activeTools" | "agent" | "permission" | "user">) {
  const disabled = Permission.disabled(Object.keys(input.tools), Agent.runtimePermission(input.agent, input.permission))
  const allowExecGateway =
    input.activeTools?.includes("exec") === true &&
    Object.keys(input.tools).some(
      (key) => !TOOL_SCRIPT_EXCLUDED.has(key) && input.user.tools?.[key] !== false && !disabled.has(key),
    )
  return Record.filter(
    input.tools,
    (_, key) =>
      input.user.tools?.[key] !== false &&
      (!disabled.has(key) ||
        ((key === MCP_TOOL_SEARCH_ID || key === MCP_TOOL_USE_ID) && input.activeTools?.includes(key) === true) ||
        (key === "exec" && allowExecGateway)),
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
