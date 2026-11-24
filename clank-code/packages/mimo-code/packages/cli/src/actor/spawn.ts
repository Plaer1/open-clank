import { Effect, Deferred, Context, Fiber, Layer, Scope, Cause, Exit, Option } from "effect"
import { ActorExecution, type Execution } from "./execution"
import { makeTerminalNotifier } from "./notification"
import type { SessionID } from "@/session/schema"
import type { ProviderID, ModelID } from "@/provider/schema"
import { Session } from "@/session"
import { SessionPrompt } from "@/session/prompt"
import * as UserInput from "@/session/user-input"
import { SessionRunState, type ManagedFinalizingIdentity } from "@/session/run-state"
import { ActorRegistry } from "@/actor/registry"
import * as ManagedProvider from "@/acp/managed-provider"
import type { ActorRoutePreflightResult, ActorOptionCandidate } from "@/acp/openclank-protocol"
import type { InputHandoffClaim, InputHandoffDisposition } from "@/actor/registry"
import { TaskRegistry } from "@/task/registry"
import { bindWorkflowTaskAssignment, releaseWorkflowTaskAssignment } from "@/session/workflow-source"
import { Agent } from "@/agent/agent"
import { Permission } from "@/permission"
import type { Actor, SpawnMode, ContextMode, ToolWhitelist, Lifecycle, ActorHostContext } from "@/actor/schema"
import { ActorExecutionContext, preserveIncompleteManagedTurn, runTurn, type ManagedTurnRetirement } from "@/actor/turn"
import { registerBinding } from "@/actor/spawn-ref"
import * as ExecutionLock from "@/actor/execution-lock"
import { Bus } from "@/bus"
import { MessageV2 } from "@/session/message-v2"
import { SessionRetry } from "@/session/retry"
import { Inbox } from "@/inbox"
import { Plugin, HookEvent } from "@/plugin"
import { parseReturnHeader, type ReturnStatus } from "./return-header"
import { Log } from "@/util"
import { Instance, type InstanceContext } from "@/project/instance"
import { InstanceRef } from "@/effect/instance-ref"
import { InstanceState } from "@/effect"
import { decodeForkContext, encodeForkContext, type EncodedForkContext, type ForkContext, type ForkContextProjection, type ForkContextSource } from "./fork-context"
import { realpath } from "node:fs/promises"
import type { DirectoryLease } from "@/actor/registry"
import { inboxControllerRef } from "@/inbox"
import type { AdmissionGuard } from "@/inbox/controller"

type DirectoryAdmission = { lease: DirectoryLease; transferred: boolean; outer: boolean; retain: boolean }
type AdmittedSpawn = SpawnInput & { directoryAdmission: DirectoryAdmission; expectedEpoch?: number }

export type { ForkContext } from "./fork-context"

const log = Log.create({ service: "actor.spawn" })

/**
 * Cap on preStop ReAct re-entries per spawn — prevents infinite loops.
 * TODO: lift to openclank.json config (e.g. actor.maxPreReact) and add per-hook
 * `maxContinue` clamp at registration. Plan: platform cap = hard ceiling, hook
 * cap may only narrow, never widen. See spec Future work.
 */
export const MAX_PRE_REACT = 3
/** Cap on postStop ReAct re-entries per spawn. See MAX_PRE_REACT TODO. */
export const MAX_POST_REACT = 3
const RETURN_FORMAT_INSTRUCTION = `

---

## Return format (required)

Your FINAL assistant message — what the spawning agent will receive — MUST start with this header block:

  **Status**: success | partial | failed | blocked
  **Summary**: <one sentence describing what happened>

After the header, include the actual deliverable (whatever the task asked for in its prompt).

If applicable, also include below the deliverable:

  **Files touched**: <comma-separated paths or "(none)">
  **Findings worth promoting**: <bullet list of cross-task transferable facts; "(none)" if just routine work>

This format lets the spawning agent and the checkpoint writer extract your progress without parsing free-form prose. Do NOT precede the header with an introduction — your final message must start with "**Status**:".
`

export type AgentOutcome =
  | {
      status: "success"
      finalText?: string
      // Structured-output (json_schema) result — when the spawn requested a
      // format, the validated object is surfaced here and takes precedence over
      // finalText (DW spec P3).
      structured?: unknown
      // Subagent's self-reported header status (parsed from finalText).
      reportedStatus?: ReturnStatus
      reportedSummary?: string
      warnings?: string[]
    }
  | { status: "failure"; error: string; failure?: FailureInfo; finalText?: string; structured?: unknown }
  | { status: "cancelled" }

/**
 * Coarse, provider-agnostic category of a settled failure. Deliberately small:
 * a consumer needs to answer "will this recur identically?", not to know which
 * provider spelled which body which way. The provider-specific taxonomy has
 * already been collapsed upstream by MessageV2.fromError, which runs
 * ProviderError.parseAPICallError / isOverflow / isOpenAiErrorRetryable.
 */
export type FailureKind = "transient" | "overflow" | "auth" | "aborted" | "other"

/**
 * Classification carried on a `failure` outcome so a consumer can branch without
 * string-matching `error`.
 *
 * PRESENT only when the failure came from a settled assistant error — i.e. the
 * child's turn ran and persisted a normalized named error. ABSENT when the work
 * fiber failed some other way (a defect during teardown, or a hand-built outcome
 * such as checkpoint's "timeout"): there is no provider taxonomy to report and
 * asserting one would be a lie. Read it with truthiness / `== null`, never
 * `=== undefined` (AGENTS.md, "Reading a nullable column").
 *
 * `retryable` means "belonged to the retryable class", NOT "please retry". The
 * child's LLM calls already run through SessionRetry's ladder (session/retry.ts,
 * consumed by session/llm.ts and session/processor.ts), so any failure reaching
 * a consumer is ALREADY post-retry.
 */
export interface FailureInfo {
  readonly kind: FailureKind
  readonly retryable: boolean
  /** The persisted NamedError name, e.g. "APIError" / "ContextOverflowError". */
  readonly name: string
}

/**
 * Classify a settled assistant error where the typed error still exists.
 *
 * Reuses SessionRetry.retryable as the retryability oracle rather than adding a
 * taxonomy: its input type IS this data shape (`Err` === `NamedError.toObject()`)
 * and it already folds in isRetryableTransientError plus every 429 / 5xx / quota
 * special case. `kind` is then read off the named-error identity, which is what
 * MessageV2.fromError derived from ProviderError.parseAPICallError.
 */
function classifyAssistantError(err: SessionRetry.Err): FailureInfo {
  // Truthiness, not `!== undefined`: retryable() returns a status *message*, and
  // an empty one is not a usable retry signal.
  const retryable = !!SessionRetry.retryable(err)
  // 401/403 read off the statusCode ProviderError.parseAPICallError already
  // extracted — not a new taxonomy and not a re-parse of prose. MessageV2.AuthError
  // ("ProviderAuthError") only covers a MISSING key (LoadAPIKeyError); a rejected
  // one arrives as an APIError, and both are the same thing to a consumer.
  const status = MessageV2.APIError.isInstance(err) ? err.data.statusCode : undefined
  const kind: FailureKind = MessageV2.ContextOverflowError.isInstance(err)
    ? "overflow"
    : MessageV2.AuthError.isInstance(err) || status === 401 || status === 403
      ? "auth"
      : MessageV2.AbortedError.isInstance(err)
        ? "aborted"
        : retryable
          ? "transient"
          : "other"
  return { kind, retryable, name: err.name }
}

/**
 * Raised by runAgentLoop when the child's turn settled with an assistant error.
 * Exists solely to carry the classification across the Effect failure channel to
 * forkWork's onFailure — that is the only place AgentOutcome is built, and the
 * typed error is not reachable from there. Deliberately a plain Error subclass:
 * Cause.pretty renders it byte-identically to `new Error(message)`, so the human
 * `error` string is unchanged.
 */
class AssistantSettledError extends Error {
  constructor(
    message: string,
    readonly failure: FailureInfo,
  ) {
    super(message)
  }
}

export interface SpawnInput {
  mode: SpawnMode
  sessionID: SessionID
  /**
   * Parent session id when the actor runs in a child session (Axis A: checkpoint
   * writer spawns under a child session keyed on parent_id but writes to the
   * parent's checkpoint.md / memory.md). Hooks (actor.preStop / actor.postStop)
   * receive this so plugins re-deriving paths from sessionID can fall back to
   * `parentSessionID ?? sessionID` and reach the parent's artifacts.
   *
   * Defaults to `sessionID` inside spawnSubagent when omitted, so existing
   * callers (peer / dream / distill / regular subagents where parent ===
   * session) need no change.
   */
  parentSessionID?: SessionID
  agentType: string
  task: string
  description?: string
  context: ContextMode
  tools: ToolWhitelist
  model?: { providerID: ProviderID; modelID: ModelID }
  /**
   * Reasoning variant for the child's turns (recorded on its user messages and merged
   * by the LLM layer). The actor tool fills it from the parent turn; omitted = none.
   * A managed route's own reasoning option takes precedence.
   */
  variant?: string | null
  /** User/config request before resolving groups and inheritance. */
  requestedModel?: string
  /** Trusted exact parent execution value, never model-authored. */
  optionCandidate?: ActorOptionCandidate
  /** Host-owned account/grant/credential/workspace binding for this actor. */
  hostContext?: ActorHostContext
  /** Optional creation-only Files binding, rechecked by the allocation transaction. */
  derivedWorkspace?: {chatID:string;workspaceID:string;current:()=>boolean}
  /** Private host-user binding, supplied only by an exact admitted parent. */
  originalOwnerAccountID?: string
  /** Existing actor ID for an explicit continuation. */
  actorID?: string
  /** Internal root lineage; overwritten from the trusted active execution context. */
  rootOperationID?: string
  background: boolean
  // A foreground waiter decides whether terminal delivery must be handed off.
  notifyOnCompletion?: Deferred.Deferred<boolean>
  parentActorID?: string
  /** Transcript actor/session slice from which a full ForkContext was captured. */
  forkContextSource?: ForkContextSource
  task_id?: string // Spec ②: bound user-task ID for postStop progress.md validation
  // Peer-only: directory the child session runs in. When set, the child's work
  // fiber is bound to that directory's Instance (via InstanceRef) so all its
  // file tools / write boundary resolve against it — i.e. real isolation. A
  // worktree is just such a directory; whether to CREATE one is the caller's
  // policy (the session tool creates a worktree and passes its dir here). When
  // unset, the child shares the spawner's directory.
  cwd?: string
  forkContext?: ForkContext // NEW
  /** Set internally by spawn() after validating and encoding the supplied context. */
  encodedForkContext?: EncodedForkContext
  lifecycle?: Lifecycle
  /**
   * Optional structured-output format. When set to a json_schema format, the
   * child's SessionPrompt.prompt requests structured output: the runLoop injects
   * the StructuredOutput tool, forces toolChoice=required, and the validated
   * object flows back via message.structured (see runAgentLoop). The validated
   * object is surfaced on AgentOutcome.structured.
   */
  format?: MessageV2.OutputFormat
  /**
   * Fired synchronously after the parked outer work fiber is registered, before
   * its start gate opens. This lets a caller record the child id and reclaim it
   * before any model/task work begins. Best-effort: callback errors are swallowed
   * so the gate still opens. Only subagents invoke it; peers are not orchestrated
   * by the workflow runtime.
   */
  onActorID?: (actorID: string) => void
  /**
   * Fired as an Effect BEFORE Fiber.join (for non-background spawns). Lets the
   * caller emit metadata (sessionId/actorId) to the tool part state while the
   * tool is still "running" — critical for the TUI to navigate into a running
   * subagent. The callback receives the allocated actorID and sessionID.
   * Swallowed on failure (best-effort, same as onActorID).
   */
  onReady?: (info: { actorID: string; sessionID: SessionID }) => Effect.Effect<void>
  /** Optional best-effort work is owned by the existing actor layer scope. */
  onReadyBackground?: (info: { actorID:string; sessionID:SessionID }) => Effect.Effect<void>
}

export interface SpawnResult {
  actorID: string
  sessionID: SessionID
  titleRevision?: number
  outcome: Deferred.Deferred<AgentOutcome>
  requestedModel?: string
  effectiveModel?: { providerID: ProviderID; modelID: ModelID }
  hostContext?: ActorHostContext
}

export interface Interface {
  readonly spawn: (input: SpawnInput) => Effect.Effect<SpawnResult>
  readonly resumeInput?: (input: {
    sessionID: SessionID
    actorID: string
    claim: InputHandoffClaim
    managedRoute?: ManagedProvider.RouteContext
  }) => Effect.Effect<InputHandoffDisposition>
  readonly hasOuterExecution?: (sessionID: SessionID, actorID: string, executionRevision: number) => Effect.Effect<boolean>
  readonly hasManagedFinalizing?: (sessionID: SessionID, actorID: string, identity: ManagedFinalizingIdentity) => Effect.Effect<boolean>
  readonly cancel: (
    sessionID: SessionID,
    actorID: string,
    mode: "graceful" | "forced",
    guard?: AdmissionGuard,
  ) => Effect.Effect<void>
  readonly getForkContext: (sessionID: SessionID, actorID: string) => Effect.Effect<ForkContextProjection | undefined>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Actor") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const session = yield* Session.Service
    const actorReg = yield* ActorRegistry.Service
    const agents = yield* Agent.Service
    const sessionPrompt = yield* SessionPrompt.Service
    const inbox = yield* Inbox.Service
    const state = yield* SessionRunState.Service
    const plugin = yield* Plugin.Service
    const bus = yield* Bus.Service
    const taskRegistry = yield* TaskRegistry.Service
    const scope = yield* Scope.Scope

    // ForkContext snapshot per actor, captured at spawn for fork agents
    // (contextMode = "full"). Read by fork's runLoop (see prompt.ts) and
    // cleared on terminal status; ActorExecution tracks the in-process lifecycle.
    const forkContexts = new Map<string, { revision: number; hash: string; context: ForkContext }>()
    const forkContextKey = (sessionID: SessionID, actorID: string) => `${sessionID}:${actorID}`
    const clearForkContext = (sessionID: SessionID, actorID: string, revision: number) => {
      const key = forkContextKey(sessionID, actorID)
      const current = forkContexts.get(key)
      if (current?.revision === revision) forkContexts.delete(key)
    }

    // Upstream's ActorExecution is the in-process owner of a run (fiber, cancel
    // flag, group abort, done). This side table adds what it does not carry:
    // the durable execution revision and the public outcome of that run. An
    // entry exists before onActorID is invoked, which closes the window where
    // workflow reclaim could see a pending actor with nothing to interrupt.
    const executions = yield* ActorExecution.Service
    const notifyTerminal = makeTerminalNotifier({ inbox, registry: actorReg, sessions: session })
    const outerFibers = new Map<
      string,
      { revision: number; execution: Execution; outcome: Deferred.Deferred<AgentOutcome>; finalizing?: ManagedFinalizingIdentity }
    >()
    const pendingOutcomes = new Map<string, Deferred.Deferred<AgentOutcome>>()

    // Serialize durable generation handoff with cancellation teardown. The
    // registry CAS alone cannot fence the later unversioned SessionRunState
    // interrupt, so the same actor lane covers begin→park and cancel CAS→
    // inner-runner/descendant teardown. ActorExecution.reserve refuses a second
    // owner; this lane is kept where the second caller must wait instead.
    const executionLocks = ExecutionLock.make()
    const withExecutionLock = <A, E, R>(key: string, effect: Effect.Effect<A, E, R>) =>
      executionLocks.withLock(key, effect)

    // Process-local cancellation hint retained for diagnostics/interrupt
    // coordination. Durable begin/settle/claim CAS operations are authoritative;
    // this set must never decide an outcome or notification.
    const cancelling = new Set<string>()
    const cancelKey = (sessionID: SessionID, actorID: string) => `${sessionID}:${actorID}`

    // Real agent loop: marks the actor running, then drives a SessionPrompt.prompt
    // turn. The user message persisted by SessionPrompt carries the actor's
    // agentID, which the projector writes to MessageTable.agent_id — that is the
    // load-bearing piece this primitive exists for.
    //
    // Returns the assistant's final text (if any) so forkWork's onSuccess can
    // pass it to inbox.send (notification body) and into the success Deferred.
    const runAgentLoop = Effect.fn("Actor.runAgentLoop")(function* (input: {
      sessionID: SessionID
      actorID: string
      agentType: string
      task: string
      task_id?: string
      model?: { providerID: ProviderID; modelID: ModelID }
      variant?: string | null
      source: "spawn" | "hook"
      provenance?: MessageV2.Provenance
      format?: MessageV2.OutputFormat
      receiptOnly?: boolean
      executionRevision?: number
    }) {
      const result = input.receiptOnly
        ? yield* sessionPrompt.loop({ sessionID: input.sessionID, agentID: input.actorID,
            afterTurnReady: true,
            manageExecution: false,
            ...(input.actorID === "main" && input.executionRevision !== undefined
              ? { executionRevision: input.executionRevision } : {}),
            ...(input.model ? { model: input.model } : {}) })
        : yield* sessionPrompt.prompt({
        sessionID: input.sessionID,
        agent: input.agentType,
        agentID: input.actorID,
        source: input.source,
        provenance: input.provenance,
        model: input.model,
        ...(input.variant !== undefined ? { variant: input.variant } : {}),
        task_id: input.task_id,
        parts: [{ type: "text", text: input.task }],
        ...(input.format ? { format: input.format } : {}),
      })
      // structured output (json_schema) takes precedence over finalText: when the
      // child produced a validated object it IS the authoritative result and the
      // last text part (often a pre-tool-call preamble) is dropped to avoid
      // duplicating the result downstream. See spec §5.2.
      const info = (result as MessageV2.WithParts | undefined)?.info
      if (info?.role === "assistant" && info.error) {
        // Classify HERE: `info.error` is the persisted, already-normalized named
        // error. Downstream (forkWork's onFailure) only has Cause.pretty's string.
        return yield* Effect.fail(
          new AssistantSettledError(`Actor assistant failed: ${info.error.name}`, classifyAssistantError(info.error)),
        )
      }
      const structured = info?.role === "assistant" ? info.structured : undefined
      const finalText =
        structured !== undefined
          ? undefined
          : (result as MessageV2.WithParts | undefined)?.parts.findLast(
              (p): p is Extract<MessageV2.Part, { type: "text" }> => p.type === "text",
            )?.text
      return { finalText, structured }
    })

    const forkWork = (input: {
      sessionID: SessionID
      parentSessionID: SessionID
      parentActorID?: string
      actorID: string
      agentType: string
      task: string
      description?: string
      background: boolean
      notifyOnCompletion?: Deferred.Deferred<boolean>
      expectedEpoch?: number
      model?: { providerID: ProviderID; modelID: ModelID }
      variant?: string | null
      requestedModel?: string
      optionCandidate?: ActorOptionCandidate
      hostContext?: ActorHostContext
      originalOwnerAccountID?: string
      lifecycle: "ephemeral" | "persistent"
      forkContext?: ForkContext
      encodedForkContext?: EncodedForkContext
      admitPending?: boolean
      /** Called only after the parked outer fiber is registered. */
      onActorID?: (actorID: string) => void
      task_id?: string
      format?: MessageV2.OutputFormat
      receiptClaim?: InputHandoffClaim
      onClaimDisposition?: (value: InputHandoffDisposition) => void
      receiptOnly?: boolean
      frozenReceipts?: readonly UserInput.BoundaryReceipt[]
      frozenRoute?: ManagedProvider.RouteContext
      rootOperationID?: string
      preflightRoute?: ActorRoutePreflightResult
      managedOperationRelease?: Effect.Effect<void>
      directoryAdmission: DirectoryAdmission
      // When set, the child's work fiber runs under this InstanceContext (via
      // InstanceRef) instead of inheriting the spawner's. Used by peers placed
      // in their own git worktree so their tools resolve paths/write-boundary
      // against the worktree, not the orchestrator's directory.
      instanceRef?: InstanceContext
    }) => {
      // Keep a local handle to the exact revision/deferred while setup runs.
      // If an unexpected defect occurs after beginExecution but before the
      // parked fiber is registered, the compensation below settles that same
      // revision so no durable running row is stranded.
      let begunRevision: number | undefined
      let begunOutcome: Deferred.Deferred<AgentOutcome> | undefined
      let preflightRoute = input.preflightRoute
      let preflightExpectedExecutionRevision: number | undefined
      let operationRelease = input.managedOperationRelease
      let executionHostContext = input.hostContext
      let installedKey: string | undefined
      let installedRevision: number | undefined
      let installedFiber: Fiber.Fiber<unknown, unknown> | undefined
      let reservedExecution: Execution | undefined
      const executionKey = cancelKey(input.sessionID, input.actorID)
      const retirement: ManagedTurnRetirement = { unknown: false, phase: "unissued" }
      const settlement = () => ManagedProvider.enabled() && input.rootOperationID && retirement.lastInnerTurnID
        ? { rootOperationID: input.rootOperationID, rootSessionID: preflightRoute?.bootstrap.rootSessionID, innerTurnID: retirement.lastInnerTurnID, turnOutcome: retirement.lastTurnOutcome } : undefined
      const retireManagedExecution = (revision: number, outcome: "success" | "failure" | "cancelled") => Effect.gen(function* () {
        const identity = settlement()
        if (!identity?.rootSessionID || !identity.innerTurnID || !identity.turnOutcome || retirement.unknown) return
        const owner = outerFibers.get(executionKey)
        if (!owner || owner.revision !== revision || executions.currentUnsafe(input.sessionID, input.actorID) !== owner.execution || owner.finalizing) return
        const witness: ManagedFinalizingIdentity = Object.freeze({ rootSessionID: identity.rootSessionID, rootOperationID: identity.rootOperationID, innerTurnID: identity.innerTurnID, turnOutcome: identity.turnOutcome, executionRevision: revision, executionOutcome: outcome })
        owner.finalizing = witness
        const result = yield* Effect.uninterruptible(Effect.tryPromise({
          try: () => ManagedProvider.finishActorExecution({
            sessionID: input.sessionID, actorID: input.actorID, rootOperationID: identity.rootOperationID, executionRevision: revision,
          }, outcome),
          catch: (cause) => new Error(`managed execution retirement failed: ${String(cause)}`),
        }).pipe(Effect.ensuring(Effect.sync(() => { if (outerFibers.get(executionKey) === owner && owner.finalizing === witness) delete owner.finalizing })))).pipe(Effect.exit)
        if (Exit.isSuccess(result) && result.value.accepted)
          yield* actorReg.clearManagedSettlement(input.sessionID, input.actorID, revision, identity).pipe(Effect.ignore)
      })
      const settledOutcome = (actor: Actor | undefined): AgentOutcome =>
        actor?.lastOutcome === "failure"
          ? { status: "failure", error: actor.lastError ?? "actor settled during setup" }
          : actor?.lastOutcome === "success"
            ? { status: "success" }
            : { status: "cancelled" }
      const compensate = (cause: Cause.Cause<unknown>) => {
        if (begunRevision === undefined) return Effect.failCause(cause)
        const revision = begunRevision
        const outcome = begunOutcome
        return Effect.uninterruptible(
          withExecutionLock(executionKey,
            Effect.gen(function* () {
              preserveIncompleteManagedTurn(retirement)
              const releaseExit = yield* (operationRelease ?? Effect.void).pipe(Effect.exit)
              if (Exit.isFailure(releaseExit)) retirement.unknown = true
              if (retirement.unknown) input.directoryAdmission.retain = true
              if (installedFiber) yield* Fiber.interrupt(installedFiber).pipe(Effect.ignore)
              // Reserved but never forked: nothing else will release it.
              else if (reservedExecution) yield* executions.release(reservedExecution)
              if (installedKey && installedRevision !== undefined) {
                yield* Effect.sync(() => {
                  const current = outerFibers.get(installedKey!)
                  if (current?.revision === installedRevision) outerFibers.delete(installedKey!)
                })
              }
              if (retirement.unknown) yield* actorReg.markExecutionUncertain(input.sessionID, input.actorID, revision, settlement()).pipe(Effect.ignore)
              const settled = retirement.unknown ? false : yield* actorReg
                .settleExecution(input.sessionID, input.actorID, revision, "failure", String(cause), settlement())
                .pipe(Effect.orElseSucceed(() => false))
              if (settled) yield* retireManagedExecution(revision, "failure")
              const winner = settled
                ? ({ status: "failure", error: String(cause) } as const)
                : settledOutcome(yield* actorReg.get(input.sessionID, input.actorID))
              yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, revision))
              if (outcome) yield* Deferred.succeed(outcome, winner).pipe(Effect.ignore)
              return yield* Effect.failCause(cause)
            }),
          ),
        )
      }
      const setupBody = withExecutionLock(executionKey,
        Effect.uninterruptibleMask(() =>
        Effect.gen(function* () {
        const outcome = yield* Deferred.make<AgentOutcome>()
          begunOutcome = outcome
          const startGate = yield* Deferred.make<void>()
        // A successful execution settles its durable row before fire-and-forget
        // postStop hooks finish. Retire that older outer fiber before reusing
        // the actor, otherwise its unversioned SessionRunState work could race
        // the new execution and its cleanup could remove the new map entry.
        const previous = outerFibers.get(executionKey)
        const previousRow = yield* actorReg.get(input.sessionID, input.actorID)
        if (
          previous &&
          previousRow &&
          previousRow.status !== "running" &&
          previousRow.executionRevision === previous.revision
        ) {
          // A completed execution can still be inside its fire-and-forget
          // postStop turn. Retire and await the exact outer fiber first; the
          // runner's own interrupt cleanup then releases SessionRunState before
          // a same-actor resume can install its next revision.
          yield* executions.interrupt(previous.execution).pipe(Effect.ignore)
          yield* state.cancelActor(input.sessionID, input.actorID).pipe(Effect.ignore)
        }
        // One durable execution spans every preStop, completion-gate, and
        // postStop turn in this fork. Re-entries must not create fresh terminal
        // rows or expose an idle gap that lets cancellation overwrite success.
        let executionRevision: number | undefined
        if (input.receiptClaim) {
          const claimExecution = actorReg.beginExecutionForInput
          if (!claimExecution) return undefined
          const disposition = yield* claimExecution(input.sessionID, input.actorID, input.receiptClaim)
          input.onClaimDisposition?.(disposition)
          if (disposition.kind !== "fresh-claim") return undefined
          executionRevision = disposition.claimedExecutionRevision
        } else {
          executionRevision = yield* actorReg.beginExecution(
            input.sessionID,
            input.actorID,
            input.admitPending ? "pending" : undefined,
            preflightExpectedExecutionRevision,
          )
        }
        if (executionRevision === undefined) {
          const existing = yield* actorReg.get(input.sessionID, input.actorID)
          return yield* Effect.die(
              new Error(
                existing
                  ? `Cannot start already-running actor ${input.actorID}`
                  : `Cannot start unknown actor ${input.actorID}`,
              ),
          )
        }
          begunRevision = executionRevision
        // Upstream's ActorExecution owns this run in-process from here to the
        // release in the outer fiber's finalizer. Reserved after the durable
        // begin, so a refused start stays the typed disposition above instead
        // of a thrown "already active".
        const reserving = executions.reserve(input.sessionID, input.actorID, input.instanceRef?.directory)
        const execution = yield* (input.instanceRef
          ? reserving.pipe(Effect.provideService(InstanceRef, input.instanceRef))
          : reserving)
        reservedExecution = execution
        if (ManagedProvider.enabled()) {
          if (!preflightRoute || !input.model || !input.rootOperationID)
            return yield* Effect.die(new Error("managed actor execution is missing its route preflight"))
          const storedOwner = actorReg.getDisplayOwner ? yield* actorReg.getDisplayOwner(input.sessionID,input.actorID) : undefined
          const rootOwner = ManagedProvider.currentHostContext(preflightRoute.bootstrap.rootSessionID)?.ownerAccountID
          const originalOwnerAccountID = storedOwner && (storedOwner === input.originalOwnerAccountID || storedOwner === rootOwner) ? storedOwner : undefined
          const activated = yield* Effect.tryPromise({
            try: () => ManagedProvider.activateActorOperation(
              {
                sessionID: input.sessionID,
                actorID: input.actorID,
                executionRevision: executionRevision!,
                rootOperationID: input.rootOperationID!,
              },
              preflightRoute!,
              { providerID: input.model!.providerID, modelID: input.model!.modelID },
              {
                ...(originalOwnerAccountID ? { ownerAccountID: originalOwnerAccountID } : {}),
                ...(input.hostContext?.chatID ? { chatID: input.hostContext.chatID } : {}),
                ...(input.hostContext?.workspaceID ? { workspaceID: input.hostContext.workspaceID } : {}),
                ...(input.hostContext?.cwd ? { cwd: input.hostContext.cwd } : {}),
                ...(input.hostContext?.goalID ? { goalID: input.hostContext.goalID } : {}),
              },
            ),
            catch: (error) => new Error(`managed actor activation failed: ${String(error)}`),
          }).pipe(Effect.orDie)
          operationRelease = Effect.gen(function* () {
            const captured = yield* SessionPrompt.persistManagedActorCapture(session, {
              sessionID: input.sessionID, actorID: input.actorID, executionRevision: executionRevision!, rootOperationID: input.rootOperationID,
            }).pipe(Effect.orElseSucceed(() => false))
            if (!captured) yield* Effect.sync(() => log.warn("managed actor capture was not persisted", {
              sessionID: input.sessionID, actorID: input.actorID, executionRevision,
            }))
          }).pipe(Effect.ensuring(Effect.promise(() => activated.release()).pipe(Effect.orDie)))
          executionHostContext = {
            ...(input.hostContext ?? {}), chatID: preflightRoute.bootstrap.stableChatID,
            accountID: activated.scope.accountID, credentialRevision: activated.scope.credentialRevision,
            grantID: preflightRoute.grantID, grantRevision: preflightRoute.grantRevision,
          }
          const updated = yield* actorReg.updateHostContextForExecution(input.sessionID, input.actorID, executionRevision, executionHostContext)
          if (!updated) return yield* Effect.die(new Error("managed actor execution changed before binding attribution"))
          if (!input.receiptOnly) {
            if (!preflightRoute.modelRequestOptions) return yield* Effect.die(new Error("managed actor model options are unavailable"))
            const saved = yield* actorReg.updateModelOptionsForExecution(input.sessionID, input.actorID, executionRevision, preflightRoute.modelRequestOptions)
            if (!saved) return yield* Effect.die(new Error("managed actor execution changed before option attribution"))
          }
        }
        if (input.forkContext) {
          yield* Effect.sync(() => forkContexts.set(forkContextKey(input.sessionID, input.actorID), {
            revision: executionRevision,
            hash: input.encodedForkContext?.snapshotSHA256 ?? "",
            context: input.forkContext!,
          }))
        }
        // A reusable actor may be resumed after an earlier cancellation. The
        // process-local hint is never authority and must not leak across epochs.
        yield* Effect.sync(() => cancelling.delete(cancelKey(input.sessionID, input.actorID)))
        const notify = (
          status: "completed" | "failed" | "cancelled",
          extra: {
            result?: string
            error?: string
            reportedStatus?: ReturnStatus
            reportedSummary?: string
            warnings?: string[]
          },
          revision: number,
        ) =>
          Effect.gen(function* () {
            // A foreground waiter decides whether terminal delivery is handed off.
            if (input.notifyOnCompletion && !(yield* Deferred.await(input.notifyOnCompletion))) return
            // Upstream's notifier; it claims this revision's one notification
            // (claimTerminalNotification) before it queues the structured event.
            yield* notifyTerminal({
              sessionID: input.sessionID,
              actorID: input.actorID,
              parentSessionID: input.parentSessionID,
              ...(input.parentActorID ? { parentActorID: input.parentActorID } : {}),
              source: "spawn",
              status,
              executionRevision: revision,
              ...extra,
              expectedEpoch: execution.groupAbort ? undefined : input.expectedEpoch,
            })
          })

        // Derive actor mode from spawn shape: peer creates a new session, subagent shares parent's
        const actorMode: "peer" | "subagent" = input.parentSessionID === input.sessionID ? "subagent" : "peer"

        // Writability of THIS agent, derived from the same predicate the runtime uses to
        // strip the Write tool (llm.ts resolveTools → Permission.disabled). Read-only agents
        // (e.g. explore: "*":deny) → canWrite=false → postStop progress check is skipped for
        // them (they cannot satisfy a "write the journal" nudge; their findings return via
        // finalText). Agent-static: uses agentInfo.permission ONLY, not the session-merged
        // ruleset resolveTools builds (merge(agent, session)). So canWrite diverges from
        // runtime tool-stripping only under a session-level override — e.g. session "*":allow
        // un-stripping a read-only agent's write (we skip though runtime allows), or session
        // "*":deny on a writable agent (we nudge though runtime strips). Both are deliberately
        // ignored: not reachable in normal usage (openclank-engine run sets no such rule, spawn doesn't
        // rewrite session.permission). See spec §Decision. Unknown agent → fail-open (true).
        const forkAgentInfo = yield* agents.get(input.agentType)
        const canWrite = forkAgentInfo ? !Permission.disabled(["write"], forkAgentInfo.permission).has("write") : true

        const warnings: string[] = []
        let lastResult: { finalText?: string; structured?: unknown } = {}
        const turnOptions = { executionRevision, rootOperationID: input.rootOperationID, preflightRoute, retirement }
        // Upstream's settle step: persist the delivery on the actor's last
        // assistant message. The returned id is written with the outcome in
        // settleExecution's compare-and-set. The Host's main actor delivers
        // through its own transcript, so it stores no actorResult.
        const persistDelivery = (result: {
          finalText?: string
          structured?: unknown
          reportedStatus?: ReturnStatus
          reportedSummary?: string
        }) =>
          Effect.gen(function* () {
            if (input.actorID === "main") return undefined
            const messages = yield* session.messages({ sessionID: input.sessionID, agentID: input.actorID })
            const last = messages.findLast((message) => message.info.role === "assistant")
            if (last?.info.role !== "assistant") return undefined
            yield* session.updateMessage({
              ...last.info,
              actorResult: {
                finalText: result.finalText,
                structured: result.structured,
                ...(result.reportedStatus ? { reportedStatus: result.reportedStatus } : {}),
                ...(result.reportedSummary ? { reportedSummary: result.reportedSummary } : {}),
                ...(warnings.length ? { warnings } : {}),
              },
            })
            return last.info.id
          }).pipe(
            Effect.catchCause((cause) =>
              Effect.logError("actor delivery persistence failed", cause).pipe(Effect.as(undefined)),
            ),
          )

        // One durable execution revision spans every turn below; each inner
        // turn runs under runTurn with that revision so it carries the trusted
        // ActorExecutionContext and never settles on its own.
        const work = Effect.gen(function* () {
          // A cancel that won before the work started (upstream's flag).
          if (execution.cancelled) return yield* Effect.interrupt
          let finalText: string | undefined
          let structured: unknown | undefined
          let iteration = 0
          let lastDecision:
            | { reason: string; contributingPluginNames: string[]; contributingHookIDs: string[] }
            | undefined

          while (true) {
            const reentryDecision = iteration > 0 ? lastDecision : undefined
            const turn = yield* runTurn(
              input.sessionID,
              input.actorID,
              runAgentLoop({
                ...input,
                ...(ManagedProvider.enabled() && !input.receiptOnly ? { variant: preflightRoute?.modelRequestOptions?.options.reasoning_effort ?? null } : {}),
                task: reentryDecision ? reentryDecision.reason : input.task,
                receiptOnly: Boolean(input.receiptOnly && iteration === 0 && !reentryDecision),
                executionRevision: input.receiptOnly && input.actorID === "main" ? executionRevision : undefined,
                source: reentryDecision ? "hook" : "spawn",
                provenance: reentryDecision
                  ? {
                      hookPhase: "pre",
                      hookIteration: iteration,
                      pluginNames: reentryDecision.contributingPluginNames,
                      hookIDs: reentryDecision.contributingHookIDs,
                    }
                  : undefined,
              }),
              { ...turnOptions, ...(iteration === 0 && input.frozenReceipts ? { frozenReceipts: input.frozenReceipts } : {}) },
            )
            finalText = turn.finalText
            structured = turn.structured
            lastResult = turn

            iteration++
            if (input.actorID === "main") break
            if (iteration > MAX_PRE_REACT) {
              yield* bus.publish(HookEvent.ReActMaxReached, {
                phase: "pre",
                actorID: input.actorID,
                agentType: input.agentType,
              })
              log.warn("actor.preStop hit MAX_PRE_REACT cap; skipping further hook checks", {
                actorID: input.actorID,
                totalTurns: iteration,
              })
              break
            }

            const decision = yield* plugin.triggerActorPreStop({
              sessionID: input.sessionID,
              parentSessionID: input.parentSessionID,
              actorID: input.actorID,
              parentActorID: input.parentActorID,
              agentType: input.agentType,
              mode: actorMode,
              lifecycle: input.lifecycle,
              finalText,
              task: input.task,
              description: input.description,
              task_id: input.task_id,
              iteration: iteration - 1,
            })
            if (!decision.continue) break
            if (!decision.reason) break // defense-in-depth — T4 invariant guarantees this won't fire

            yield* bus.publish(HookEvent.ReActReentered, {
              phase: "pre",
              actorID: input.actorID,
              agentType: input.agentType,
              iteration,
              triggeredByPlugins: decision.contributingPluginNames,
              reasonPreview: decision.reason.slice(0, 200),
            })

            lastDecision = {
              reason: decision.reason,
              contributingPluginNames: decision.contributingPluginNames,
              contributingHookIDs: decision.contributingHookIDs,
            }
          }

          return { finalText, structured }
        }).pipe(
          Effect.flatMap(({ finalText, structured }) =>
            Effect.gen(function* () {
              // postStop ReAct loop — keep the execution running until hooks settle.
              // NOTE: parallel structure to preStop loop above — pre runs turn THEN checks,
              // post checks THEN runs turn. Both give 1 (delivery) + MAX_POST_REACT re-entries.
              // Task completion is NOT gated here: no `task done` nudge, no re-emit.
              const deliveredText = finalText
              let postIter = 0
              let lastFinalText = deliveredText
              let postReentry:
                | { reason: string; contributingPluginNames: string[]; contributingHookIDs: string[] }
                | undefined

              while (input.actorID !== "main") {
                // A postStop nudge is useful only if the next real turn can
                // actually expose and permit this tool under every current authority.
                const canWorkflowWrite = yield* Effect.gen(function* () {
                  const currentActor=yield* actorReg.get(input.sessionID,input.actorID)
                  const currentSession=yield* session.get(input.sessionID)
                  const agentInfo=yield* agents.get(input.agentType)
                  if (!currentActor || currentActor.status!=="running" || currentActor.executionRevision!==executionRevision || !agentInfo) return false
                  if (Array.isArray(currentActor.tools) && !currentActor.tools.includes("workflow_artifact")) return false
                  const needsFork=currentActor.contextMode==="full" && (currentActor.mode==="subagent" || currentActor.mode==="peer")
                  const projection=needsFork ? yield* getForkContext(input.sessionID,input.actorID) : undefined
                  if (needsFork && !projection) return false
                  if (projection?.effectiveToolWhitelist && !projection.effectiveToolWhitelist.includes("workflow_artifact")) return false
                  const rules=[Agent.runtimePermission(agentInfo,currentSession.permission),...(projection?.permissionRulesets ?? [])]
                  return Permission.evaluateIntersection("workflow_artifact","*",rules).allowed
                }).pipe(Effect.catchCause(cause=>Cause.hasInterrupts(cause) ? Effect.failCause(cause) : Effect.succeed(false)))
                const decision = yield* plugin.triggerActorPostStop({
                  sessionID: input.sessionID,
                  parentSessionID: input.parentSessionID,
                  actorID: input.actorID,
                  parentActorID: input.parentActorID,
                  agentType: input.agentType,
                  mode: actorMode,
                  lifecycle: input.lifecycle,
                  finalText: lastFinalText,
                  task: input.task,
                  description: input.description,
                  task_id: input.task_id,
                  outcome: "success",
                  iteration: postIter,
                  canWrite,
                  canWorkflowWrite,
                })

                if (!decision.continue) break
                if (!decision.reason) break // defense-in-depth
                if (postIter >= MAX_POST_REACT) {
                  yield* bus.publish(HookEvent.ReActMaxReached, {
                    phase: "post",
                    actorID: input.actorID,
                    agentType: input.agentType,
                  })
                  log.warn("actor.postStop hit MAX_POST_REACT cap; skipping further hook checks", {
                    actorID: input.actorID,
                    totalTurns: postIter + 1,
                  })
                  break
                }
                postIter++

                yield* bus.publish(HookEvent.ReActReentered, {
                  phase: "post",
                  actorID: input.actorID,
                  agentType: input.agentType,
                  iteration: postIter,
                  triggeredByPlugins: decision.contributingPluginNames,
                  reasonPreview: decision.reason.slice(0, 200),
                })

                postReentry = {
                  reason: decision.reason,
                  contributingPluginNames: decision.contributingPluginNames,
                  contributingHookIDs: decision.contributingHookIDs,
                }

                const postExit = yield* runTurn(
                  input.sessionID,
                  input.actorID,
                  runAgentLoop({
                    ...input,
                    task: postReentry.reason,
                    receiptOnly: false,
                    source: "hook",
                    provenance: {
                      hookPhase: "post",
                      hookIteration: postIter,
                      pluginNames: postReentry.contributingPluginNames,
                      hookIDs: postReentry.contributingHookIDs,
                    },
                  }),
                  turnOptions,
                ).pipe(Effect.exit)
                if (Exit.isFailure(postExit)) {
                  if (Cause.hasInterruptsOnly(postExit.cause)) return yield* Effect.failCause(postExit.cause)
                  warnings.push(`postStop: ${Cause.pretty(postExit.cause)}`)
                  break
                }
                const newTurn = postExit.value
                if (newTurn.finalText === undefined) break
                lastFinalText = newTurn.finalText
              }

              // Deliver the preserved main result. postStop may re-enter for
              // housekeeping but must NOT replace the delivery body.
              const parsed = parseReturnHeader(deliveredText)
              return {
                status: "success" as const,
                ...(deliveredText !== undefined ? { finalText: deliveredText } : {}),
                ...(structured !== undefined ? { structured } : {}),
                ...(parsed.status ? { reportedStatus: parsed.status } : {}),
                ...(parsed.summary ? { reportedSummary: parsed.summary } : {}),
                ...(warnings.length ? { warnings } : {}),
              }
            }),
          ),
          Effect.provideService(ActorRegistry.Service, actorReg),
          Effect.matchCauseEffect({
            onSuccess: (result) =>
              Effect.gen(function* () {
                // Never publish success while an issued turn lacks its finish acknowledgement.
                preserveIncompleteManagedTurn(retirement)
                if (retirement.unknown) yield* actorReg.markExecutionUncertain(input.sessionID, input.actorID, executionRevision, settlement()).pipe(Effect.ignore)
                const resultMessageID = retirement.unknown ? undefined : yield* persistDelivery(result)
                let settled = retirement.unknown ? false : yield* actorReg
                  .settleExecution(input.sessionID, input.actorID, executionRevision, "success", undefined, settlement(), resultMessageID)
                  .pipe(Effect.orElseSucceed(() => false))
                if (settled) yield* retireManagedExecution(executionRevision, "success")
                if (!settled && input.actorID === "main") {
                  const current = yield* actorReg.get(input.sessionID, input.actorID)
                  settled = current?.status === "idle" && current.executionRevision === executionRevision &&
                    current.lastOutcome === "success"
                }
                if (!settled) {
                  yield* Deferred.succeed(outcome, { status: "cancelled" as const })
                  yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
                  yield* Effect.sync(() => cancelling.delete(cancelKey(input.sessionID, input.actorID)))
                  return
                }
                // The inline waiter needs the reconciled result to decide delivery;
                // notification and release must still finish inside this execution.
                if (input.notifyOnCompletion) yield* Deferred.succeed(outcome, result)
                yield* notify(
                  "completed",
                  {
                    result:
                      result.structured !== undefined
                        ? JSON.stringify(result.structured)
                        : (result.finalText ?? "(no output)"),
                    reportedStatus: result.reportedStatus,
                    reportedSummary: result.reportedSummary,
                    warnings: result.warnings,
                  },
                  executionRevision,
                )
                yield* Deferred.succeed(outcome, result)
                yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
                yield* Effect.sync(() => cancelling.delete(cancelKey(input.sessionID, input.actorID)))
              }),
            onFailure: (cause) =>
              Effect.gen(function* () {
                const cancelled = Cause.hasInterruptsOnly(cause)
                const error = Cause.pretty(cause)
                const status: "cancelled" | "failure" = cancelled ? "cancelled" : "failure"
                // Recover the classification runAgentLoop attached. A failure
                // raised anywhere else carries none, and the field stays absent.
                const squashed = Cause.squash(cause)
                const failure = squashed instanceof AssistantSettledError ? squashed.failure : undefined
                const result: AgentOutcome = cancelled
                  ? { status: "cancelled" }
                  : { status: "failure", error, ...lastResult, ...(failure ? { failure } : {}) }
                const partial = lastResult.finalText !== undefined || lastResult.structured !== undefined
                preserveIncompleteManagedTurn(retirement)
                if (retirement.unknown) yield* actorReg.markExecutionUncertain(input.sessionID, input.actorID, executionRevision, settlement()).pipe(Effect.ignore)
                const resultMessageID = !cancelled && partial && !retirement.unknown ? yield* persistDelivery(lastResult) : undefined
                const settled = retirement.unknown ? false : yield* actorReg
                  .settleExecution(
                    input.sessionID,
                    input.actorID,
                    executionRevision,
                    status,
                    cancelled ? undefined : error,
                    settlement(),
                    resultMessageID,
                  )
                  .pipe(Effect.orElseSucceed(() => false))
                // A cancel() in flight resolves the public outcome itself.
                const resolves = !cancelled || !cancelling.has(cancelKey(input.sessionID, input.actorID))
                if (resolves && input.notifyOnCompletion) yield* Deferred.succeed(outcome, result)
                if (settled) {
                  yield* retireManagedExecution(executionRevision, cancelled ? "cancelled" : "failure")
                  yield* notify(
                    cancelled ? "cancelled" : "failed",
                    cancelled
                      ? {}
                      : {
                          error,
                          result:
                            lastResult.structured !== undefined
                              ? JSON.stringify(lastResult.structured)
                              : lastResult.finalText,
                        },
                    executionRevision,
                  )
                }
                if (resolves) yield* Deferred.succeed(outcome, result)
                yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
                yield* Effect.sync(() => cancelling.delete(cancelKey(input.sessionID, input.actorID)))
              }),
          }),
        )
          const boundWork = input.instanceRef ? work.pipe(Effect.provideService(InstanceRef, input.instanceRef)) : work
          const gatedWork = Effect.gen(function* () {
            yield* Deferred.await(startGate)
            // Cancellation can win immediately after the durable begin and
            // before this fiber reaches the inner runner. Re-check the exact
            // revision before starting task/model work; a stale outer fiber must
            // resolve its public outcome and stop without touching the task.
            const current = yield* actorReg.get(input.sessionID, input.actorID)
            if (!current || current.status !== "running" || current.executionRevision !== executionRevision) {
              yield* Deferred.succeed(outcome, settledOutcome(current)).pipe(Effect.ignore)
              return yield* Effect.interrupt
            }
            // Auto-start the bound task only after the outer fiber has been
            // registered and the caller has received the actor id. This prevents
            // reclaim from racing a pending, not-yet-interruptible child.
            if (input.task_id) {
              const started = yield* taskRegistry
                .start({ session_id: input.parentSessionID, id: input.task_id, owner: input.actorID })
                .pipe(Effect.catchCause((cause) => Cause.hasInterrupts(cause) ? Effect.failCause(cause) : Effect.succeed(undefined)))
              if (started && input.parentSessionID === input.sessionID) yield* Effect.sync(() => {
                bindWorkflowTaskAssignment({sessionID:input.sessionID,actorID:input.actorID,executionRevision,
                  rootOperationID:input.rootOperationID,taskID:input.task_id!},started)
              })
            }
            return yield* boundWork.pipe(Effect.ensuring(Effect.sync(() => releaseWorkflowTaskAssignment(input.sessionID,input.actorID,executionRevision))))
          })
        const key = executionKey
        installedKey = key
        installedRevision = executionRevision
        const releaseOperation = Effect.gen(function* () {
          preserveIncompleteManagedTurn(retirement)
          const releaseExit=yield* (operationRelease ?? Effect.void).pipe(Effect.exit)
          if (Exit.isFailure(releaseExit)) retirement.unknown=true
          if (retirement.unknown) input.directoryAdmission.retain=true
          if (!retirement.unknown) input.directoryAdmission.lease.release()
          if (Exit.isFailure(releaseExit)) return yield* Effect.failCause(releaseExit.cause)
        })
        const forgetOuter = Effect.sync(() => {
          const current = outerFibers.get(key)
          if (current?.revision === executionRevision) outerFibers.delete(key)
        })
        const outerWork = gatedWork.pipe(
            Effect.ensuring(releaseOperation),
            Effect.ensuring(forgetOuter),
            // Setup/claim stays masked through registration, and upstream's
            // ActorExecution.fork starts the fiber uninterruptible; the detached
            // actor work itself must be interruptible.
            Effect.interruptible,
            Effect.asVoid,
            Effect.catchCause(() => Effect.void),
            Effect.ensuring(executions.release(execution)),
          )
          const fiber = yield* executions.fork(execution, outerWork, scope, (cause) =>
            // Session ownership was refused before the work started: settle
            // this exact revision and resolve the caller.
            Effect.gen(function* () {
              const error = Cause.pretty(cause)
              yield* actorReg
                .settleExecution(input.sessionID, input.actorID, executionRevision, "failure", error, settlement())
                .pipe(Effect.ignore)
              yield* releaseOperation.pipe(Effect.ignore)
              yield* forgetOuter
              yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
              yield* Deferred.succeed(outcome, { status: "failure" as const, error })
            }).pipe(Effect.asVoid),
          )
          input.directoryAdmission.outer = true
          input.directoryAdmission.transferred = true
          installedFiber = fiber
          const currentOuter = outerFibers.get(key)
          if (!currentOuter || currentOuter.revision < executionRevision) {
            outerFibers.set(key, { revision: executionRevision, execution, outcome })
          }
          // An external cancel can settle the row between beginExecution and
          // outerFibers.set. Fence that exact revision before exposing the actor
          // id or opening the gate; otherwise a cancelled execution could start
          // detached work after its durable terminal transition.
          const current = yield* actorReg.get(input.sessionID, input.actorID)
          if (!current || current.status !== "running" || current.executionRevision !== executionRevision) {
            const installed = outerFibers.get(key)
            if (installed?.revision === executionRevision) outerFibers.delete(key)
            yield* Fiber.interrupt(fiber).pipe(Effect.ignore)
            yield* Deferred.succeed(outcome, settledOutcome(current)).pipe(Effect.ignore)
            yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
            return { fiber, outcome, startGate }
          }
          // Return while the gate is still closed. The callback and final
          // revision check happen after this execution lock is released so a
          // callback-triggered cancellation can win the parked window.
          return { fiber, outcome, startGate }
        }),
        ),
      )
      const setup = setupBody
      return Effect.uninterruptibleMask((restore) =>
        Effect.gen(function* () {
        if (ManagedProvider.enabled() && !preflightRoute) {
          if (!input.model || !input.rootOperationID)
            return yield* Effect.die(new Error("managed actor route preflight requires a bound model and root operation"))
          const current = yield* actorReg.get(input.sessionID, input.actorID)
          if (!current) return yield* Effect.die(new Error("managed actor route preflight requires a registered actor"))
          preflightExpectedExecutionRevision = current.executionRevision
          const parent = input.parentActorID ? yield* actorReg.get(input.parentSessionID, input.parentActorID) : undefined
          const sameParentModel = !!parent && parent.status === "running" && parent.effectiveModel?.providerID === input.model.providerID &&
            parent.effectiveModel?.modelID === input.model.modelID
          const parentScope = sameParentModel && parent
            ? ManagedProvider.currentScope(input.parentSessionID, parent.actorID, parent.executionRevision) : undefined
          const inheritedGrant = input.frozenRoute?.grantID ?? (sameParentModel ? parent?.hostContext?.grantID : undefined)
          const inheritedAccount = input.frozenRoute?.inheritedAccountID ?? (sameParentModel ? parentScope?.accountID : undefined)
          preflightRoute = yield* restore(Effect.tryPromise({
            try: () => ManagedProvider.preflightActorRoute({
              sessionID: input.sessionID,
              rootOperationID: input.rootOperationID!,
              actorID: input.actorID,
              ...(input.parentActorID ? { parentActorID: input.parentActorID } : {}),
              expectedExecutionRevision: current.executionRevision,
              model: { providerID: input.model!.providerID, modelID: input.model!.modelID },
              modelSource: input.requestedModel && input.requestedModel !== "inherit-parent" ? "explicit" : "inherited",
              ...(!input.receiptOnly ? (input.optionCandidate ? { optionCandidate: input.optionCandidate } : current.modelRequestOptions ? { savedOptionSnapshot: current.modelRequestOptions } : { optionCandidate: { version: 1 as const, source: { kind: "default" as const }, model: { providerID: input.model!.providerID, modelID: input.model!.modelID }, variant: null } }) : {}),
              ...(inheritedGrant ? { grantID: inheritedGrant } : {}),
              ...(inheritedAccount ? { inheritedAccountID: inheritedAccount } : {}),
              ...(input.frozenRoute?.preferredAccountID ? { preferredAccountID: input.frozenRoute.preferredAccountID } : {}),
            }),
            catch: (error) => new Error(`managed actor route preflight failed: ${String(error)}`),
          }).pipe(Effect.orDie))
          if (input.frozenRoute && (preflightRoute.connectionID !== input.frozenRoute.connectionID ||
              preflightRoute.modelRouteID !== input.frozenRoute.modelRouteID || preflightRoute.modelID !== input.frozenRoute.modelID ||
              (input.frozenRoute.grantID && preflightRoute.grantID !== input.frozenRoute.grantID)))
            return yield* Effect.die(new Error("managed actor route changed from frozen receipt admission"))
        }
        const parked = yield* setup
          if (!parked) return undefined
        if (input.onActorID) {
          yield* Effect.sync(() => {
            try {
              input.onActorID!(input.actorID)
            } catch {
              // Callback is best-effort; the start gate must still open.
            }
          })
        }
        // Give reclaim/cancel fibers released by the callback a scheduling
        // point while the outer actor is still parked.
        yield* Effect.yieldNow
        // No lane here (it was one of our execution-lock sites): a cancel that
        // won the parked window has settled this revision and interrupted the
        // parked fiber, and the forked work re-checks execution.cancelled and
        // the durable revision before it starts, so opening the gate is safe.
        yield* Deferred.succeed(parked.startGate, undefined)
        return { fiber: parked.fiber, outcome: parked.outcome }
        }),
      ).pipe(Effect.catchCause((cause) => compensate(cause).pipe(Effect.orDie)))
    }

    const spawnPeer = Effect.fn("Actor.spawnPeer")(function* (input: AdmittedSpawn) {
      const parentHostContext = input.parentActorID
        ? yield* actorReg.get(input.sessionID, input.parentActorID)
        : undefined
      const hostContext = input.hostContext ?? parentHostContext?.hostContext
      // When the caller gives the child its own directory (e.g. a worktree the
      // session tool created), bind the child's work fiber to that directory's
      // Instance so its file tools / write boundary are isolated there. A
      // worktree is just a directory — spawn neither knows nor cares how it was
      // made. An explicit directory that cannot bind fails before launch.
      const instanceRef = input.cwd
        ? yield* Effect.promise(() => Instance.provide({ directory: input.cwd!, fn: () => Instance.current })).pipe(
            Effect.orDie,
          )
        : undefined

      const child = yield* session.create({
        parentID: input.sessionID,
        contextFrom: input.context === "full" ? input.sessionID : undefined,
        title: `${input.agentType}: ${input.task.slice(0, 40)}`,
        ...(input.cwd ? { directory: instanceRef?.directory ?? input.cwd } : {}),
      })
      // T42: register the peer's receiver/actor-registry row (session_id ===
      // actor_id === child.id, mode "peer") SYNCHRONOUSLY here — before spawn
      // resolves and before the child's first turn. This is the single
      // spawn-time registration that makes a child addressable the instant
      // `session create` returns: Inbox.send's ESRCH pre-check (reg.get) and
      // `session send` both resolve against this row without waiting for the
      // child to arm anything on its first turn. turn_count/status start at 0/
      // "pending"; the per-step turn heartbeat (registry.updateTurn) advances
      // them later. No double-registration: nothing on the first-turn path
      // (prompt.ts) re-registers a peer — it only reads (reg.get) and updates
      // (updateTurn/updateStatus). Prerequisite for T43 (--topic reuse).
      const outcome = yield* Deferred.make<AgentOutcome>()
      // Start the peer after spawn returns. This preserves the pending,
      // turnCount-0 addressability window while retaining the normal fork
      // outcome and foreground join semantics.
      const pendingKey = cancelKey(child.id, child.id)
      const launcherBody = Effect.gen(function* () {
        // Let a caller that immediately reclaims the pending actor win its
        // pending CAS before this deferred launcher attempts beginExecution.
        yield* Effect.yieldNow
        const current = yield* actorReg.get(child.id, child.id)
        if (!current || current.status !== "pending") {
          yield* Deferred.succeed(outcome, { status: "cancelled" as const }).pipe(Effect.ignore)
          return
        }
        const started = yield* forkWork({
                sessionID: child.id,
                parentSessionID: input.sessionID,
                parentActorID: input.parentActorID,
                actorID: child.id,
                agentType: input.agentType,
                task: input.task,
                description: input.description,
                background: input.background,
                notifyOnCompletion: input.notifyOnCompletion,
                expectedEpoch: input.expectedEpoch,
                model: input.model,
                variant: input.variant,
                requestedModel: input.requestedModel,
        ...(input.optionCandidate ? { optionCandidate: input.optionCandidate } : {}),
                rootOperationID: input.rootOperationID,
                hostContext,
                ...(input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
                lifecycle: input.lifecycle ?? "persistent",
                forkContext: input.forkContext,
                encodedForkContext: input.encodedForkContext,
                task_id: input.task_id,
                format: input.format,
              directoryAdmission: input.directoryAdmission,
              admitPending: true,
              ...(instanceRef ? { instanceRef } : {}),
            })
        if (started) {
          if (!input.background) yield* Fiber.join(started.fiber).pipe(Effect.ignore)
          const result = yield* Deferred.await(started.outcome)
          yield* Deferred.succeed(outcome, result).pipe(Effect.ignore)
        }
      })
      const launcher = launcherBody.pipe(
        Effect.catchCause((cause) =>
          Effect.gen(function* () {
            const current = yield* actorReg.get(child.id, child.id)
            const result: AgentOutcome =
              current?.lastOutcome === "failure"
                ? { status: "failure", error: current.lastError ?? String(cause) }
                : current?.lastOutcome === "success"
                  ? { status: "success" }
                  : { status: "cancelled" }
            yield* Deferred.succeed(outcome, result).pipe(Effect.ignore)
            return yield* Effect.failCause(cause)
          }),
        ),
        Effect.ensuring(
          Effect.uninterruptible(
            Effect.gen(function* () {
              const current = yield* actorReg.get(child.id, child.id)
              if (current?.status === "pending") {
                const cancelled = yield* actorReg.cancelPending(child.id, child.id)
                if (cancelled) {
                  yield* Deferred.succeed(outcome, { status: "cancelled" as const }).pipe(Effect.ignore)
                  clearForkContext(child.id, child.id, 0)
                }
              }
              yield* Effect.sync(() => {
                pendingOutcomes.delete(pendingKey)
                if (!input.directoryAdmission.outer && !input.directoryAdmission.retain) input.directoryAdmission.lease.release()
              })
            }),
          ),
        ),
      )
      const launcherFiber = yield* Effect.uninterruptible(
        Effect.gen(function* () {
          yield* actorReg.register({
            sessionID: child.id,
            actorID: child.id,
            mode: "peer",
            parentActorID: input.parentActorID,
            agent: input.agentType,
            description: input.description ?? input.agentType,
            contextMode: input.context,
            contextWatermark: undefined,
            background: input.background,
            lifecycle: input.lifecycle ?? "persistent",
            tools: input.tools,
            requestedModel: input.requestedModel,
            effectiveModel: input.model,
            hostContext,
            ...(input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
            ...(input.encodedForkContext ? { forkContext: input.encodedForkContext } : {}),
          })
          if (input.forkContext) {
            // Peers are standing, addressable sessions. Their initial row and
            // fork snapshot exist before the first inbox-driven turn.
            forkContexts.set(forkContextKey(child.id, child.id), {
              revision: 0,
              hash: input.encodedForkContext?.snapshotSHA256 ?? "",
              context: input.forkContext,
            })
          }
          pendingOutcomes.set(pendingKey, outcome)
          const fiber = yield* launcher.pipe(Effect.interruptible, Effect.forkIn(scope))
          input.directoryAdmission.transferred = true
          if (input.onActorID) {
            yield* Effect.sync(() => {
              try {
                input.onActorID!(child.id)
              } catch {
                // Callback bookkeeping must not prevent the deferred launcher
                // from being installed and observable to reclaim.
              }
            })
          }
          return fiber
        }),
      ).pipe(
        Effect.catchCause((cause) =>
          Effect.uninterruptible(
            Effect.gen(function* () {
              const cancelled = yield* actorReg.cancelPending(child.id, child.id).pipe(Effect.orElseSucceed(() => undefined))
              if (cancelled) yield* Deferred.succeed(outcome, { status: "cancelled" as const }).pipe(Effect.ignore)
              yield* Effect.sync(() => {
                pendingOutcomes.delete(pendingKey)
                if (input.forkContext) clearForkContext(child.id, child.id, 0)
              })
              return yield* Effect.failCause(cause)
            }),
          ),
        ),
      )
      // forkIn defaults to a deferred next-turn start. Background callers
      // return with the pending row still untouched; foreground callers join
      // the launcher and therefore wait for the actual peer outcome.
      if (!input.background) yield* Fiber.join(launcherFiber).pipe(Effect.ignore)
      return {
        actorID: child.id,
        sessionID: child.id,
        titleRevision: child.titleRevision,
        outcome,
        ...(input.requestedModel ? { requestedModel: input.requestedModel } : {}),
        ...(input.model ? { effectiveModel: input.model } : {}),
        ...(hostContext ? { hostContext } : {}),
      }
    })

    const spawnSubagent = Effect.fn("Actor.spawnSubagent")(function* (input: AdmittedSpawn) {
      // Once a new registry row exists, keep setup uninterruptible until
      // forkWork has begun the durable revision and parked/registers the outer
      // fiber. Otherwise reclaim can interrupt this caller between register()
      // and forkWork(), leaving a pending rev-0 orphan with no cancellation
      // handle. The actual actor work remains interruptible inside forkWork.
      return yield* Effect.uninterruptibleMask((restore) =>
        Effect.gen(function* () {
      const existing = input.actorID ? yield* actorReg.get(input.sessionID, input.actorID) : undefined
      if (input.actorID && !existing) {
        return yield* Effect.die(new Error(`Cannot resume unknown actor ${input.actorID}`))
      }
      if (existing && existing.status === "running") {
        return yield* Effect.die(new Error(`Cannot resume running actor ${input.actorID}`))
      }

      // A resumed actor keeps its durable host binding unconditionally. The
      // model-facing actor tool has no authority to rebind an existing child;
      // a future rebind must be a separate authenticated host-only operation.
      const sourceActorID = input.encodedForkContext?.sourceActorID ?? input.parentActorID
      const parentHostContext = sourceActorID || input.encodedForkContext
        ? yield* actorReg.get(input.parentSessionID ?? input.sessionID, sourceActorID ?? "main")
        : undefined
      const inheritedHostContext = existing
        ? existing.hostContext
            : (input.hostContext ?? parentHostContext?.hostContext)
      let runForkContext = input.forkContext
      let runEncodedForkContext = input.encodedForkContext
      if (input.context === "full") {
        if (existing) {
          const stored = yield* actorReg.getForkContext(input.sessionID, input.actorID!)
          const projection = yield* getForkContext(input.sessionID, input.actorID!)
          runForkContext = projection?.context
          runEncodedForkContext = projection?.encoded
          if (!stored || !projection || !runForkContext || !runEncodedForkContext)
            return yield* Effect.die(new Error("full-context actor snapshot is unavailable or invalid"))
          if (input.encodedForkContext && (input.encodedForkContext.snapshotSHA256 !== stored.snapshotSHA256 ||
            input.encodedForkContext.sourceSessionID !== stored.sourceSessionID ||
            input.encodedForkContext.sourceActorID !== stored.sourceActorID))
            return yield* Effect.die(new Error("existing full-context actor snapshot is immutable"))
          if (input.model && (input.model.providerID !== stored.modelProviderID || input.model.modelID !== stored.modelID))
            return yield* Effect.die(new Error("existing full-context actor model cannot be replaced"))
        } else if (!runForkContext || !input.encodedForkContext) {
          return yield* Effect.die(new Error("new full-context actor requires a validated durable snapshot"))
        }
      }
      const persistedRequestedModel = existing && input.context === "full"
        ? existing.requestedModel
        : input.requestedModel ?? existing?.requestedModel
      const persistedEffectiveModel: SpawnResult["effectiveModel"] =
        existing && input.context === "full"
          ? (existing.effectiveModel as SpawnResult["effectiveModel"])
          : input.model ?? (existing?.effectiveModel as SpawnResult["effectiveModel"])

      if (existing && input.context !== "full") {
        yield* actorReg.updateModel(input.sessionID, input.actorID!, input.requestedModel, input.model)
      }

          // Resolve the agent before creating the registry row. This keeps all
          // fallible setup ahead of the pending row; once register() commits,
          // uninterruptible setup proceeds straight into forkWork's begin/park/
          // callback sequence.
          const agentInfo = yield* agents.get(input.agentType)
          // Agents with an explicit completionGate keep the return-format
          // instruction even with a dedicated system prompt (upstream). The
          // flag only controls that header instruction: there is no task
          // completion re-entry or incomplete-task downgrade any more.
          const gateEligible =
            agentInfo?.mode === "subagent" &&
            (agentInfo.completionGate === true || (!agentInfo.prompt && input.agentType !== "checkpoint-writer"))
          const taskWithFormat = gateEligible ? input.task + RETURN_FORMAT_INSTRUCTION : input.task
          const instanceRef = yield* InstanceRef

      const actor = existing ?? (yield* actorReg.registerAllocated({
        sessionID: input.sessionID,
        mode: "subagent",
        parentActorID: input.encodedForkContext?.sourceActorID ?? input.parentActorID,
        agent: input.agentType,
        description: input.description ?? input.agentType,
        contextMode: input.context,
        contextWatermark: input.context === "full" ? yield* session.lastMainMessageID(input.sessionID) : undefined,
        background: input.background,
        lifecycle: input.lifecycle ?? "ephemeral",
        tools: input.tools,
        requestedModel: input.requestedModel,
        effectiveModel: persistedEffectiveModel,
        hostContext: inheritedHostContext,
        ...(!existing && input.derivedWorkspace ? { derivedWorkspace:input.derivedWorkspace } : {}),
        ...(input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
        ...(runEncodedForkContext ? { forkContext: runEncodedForkContext } : {}),
      }))
      const actorID = actor.actorID
      const actualHostContext = !existing && input.derivedWorkspace ? actor.hostContext : inheritedHostContext

      // Auto-inject return-format instruction for non-specialized subagents.
      // Excluded: agents with hardcoded `prompt` (explore/title/summary — own
      // contracts), checkpoint-writer (special — task is itself a complete
      // writer-instruction string), and peer mode (routes via spawnPeer).
      const started = yield* forkWork({
        sessionID: input.sessionID,
        parentSessionID: input.parentSessionID ?? input.sessionID,
        parentActorID: input.parentActorID,
        actorID,
        agentType: input.agentType,
        task: taskWithFormat,
        description: input.description,
        background: input.background,
        notifyOnCompletion: input.notifyOnCompletion,
        expectedEpoch: input.expectedEpoch,
        model: persistedEffectiveModel,
        variant: input.variant,
        requestedModel: persistedRequestedModel,
        ...(input.optionCandidate ? { optionCandidate: input.optionCandidate } : {}),
        rootOperationID: input.rootOperationID,
        hostContext: actualHostContext,
        ...(!existing && input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
        lifecycle: input.lifecycle ?? "ephemeral",
        forkContext: runForkContext,
        encodedForkContext: runEncodedForkContext,
        task_id: input.task_id,
            onActorID: input.onActorID,
        format: input.format,
        instanceRef,
        directoryAdmission: input.directoryAdmission,
      })
      if (!started) return yield* Effect.die(new Error("Subagent execution did not start"))
      const { fiber, outcome } = started
      if (input.onReady) yield* Effect.ignore(input.onReady({ actorID, sessionID: input.sessionID }))
      if (input.onReadyBackground) yield* input.onReadyBackground({actorID,sessionID:input.sessionID}).pipe(Effect.timeout(20000),Effect.ignore,Effect.forkIn(scope))
          if (!input.background) yield* restore(Fiber.join(fiber).pipe(Effect.ignore))
      return {
        actorID,
        sessionID: input.sessionID,
        outcome,
        ...(persistedRequestedModel ? { requestedModel: persistedRequestedModel } : {}),
        ...(persistedEffectiveModel ? { effectiveModel: persistedEffectiveModel } : {}),
        ...(actualHostContext ? { hostContext: actualHostContext } : {}),
      }
        }),
      )
    })

    const spawnInner = Effect.fn("Actor.spawnInner")(function* (input: AdmittedSpawn) {
      const activeExecution = Option.getOrUndefined(yield* Effect.serviceOption(ActorExecutionContext))
      let prepared = { ...input, rootOperationID: activeExecution?.rootOperationID }
      if (input.forkContext) {
        const source = input.forkContextSource ?? {
          sessionID: input.mode === "peer" ? input.sessionID : input.parentSessionID ?? input.sessionID,
          actorID: input.parentActorID ?? "main",
        }
        const sourceActor = yield* actorReg.get(source.sessionID, source.actorID)
        if (!sourceActor) return yield* Effect.die(new Error("full-context source actor membership is unavailable"))
        const sourceProjection = sourceActor.mode !== "main" && sourceActor.contextMode === "full"
          ? yield* getForkContext(source.sessionID, source.actorID)
          : undefined
        if (sourceActor.mode !== "main" && sourceActor.contextMode === "full" && !sourceProjection)
          return yield* Effect.die(new Error("full-context source permission lineage is unavailable"))
        // Snapshot.model is the destination worker model. Source actor model is
        // provenance and may differ when a deliberate lower-cost fork is chosen.
        const proposedHostContext = input.hostContext ?? sourceActor.hostContext
        if ((proposedHostContext?.chatID ?? null) !== (sourceActor.hostContext?.chatID ?? null) ||
          (proposedHostContext?.workspaceID ?? null) !== (sourceActor.hostContext?.workspaceID ?? null))
          return yield* Effect.die(new Error("full-context destination must retain the source chat/workspace lineage"))
        if (input.model && (input.model.providerID !== input.forkContext.model.providerID ||
          input.model.modelID !== input.forkContext.model.modelID))
          return yield* Effect.die(new Error("full-context actor model cannot replace the captured model"))
        const encodedForkContext = yield* Effect.tryPromise({
          try: () => encodeForkContext(input.forkContext!, source),
          catch: (error) => new Error(`full-context snapshot unavailable: ${String(error)}`),
        }).pipe(Effect.orDie)
        const inheritedTools = intersectToolWhitelists(
          sourceProjection?.effectiveToolWhitelist,
          Array.isArray(sourceActor.tools) ? sourceActor.tools : undefined,
          Array.isArray(input.tools) ? input.tools : undefined,
        )
        prepared = {
          ...input,
          rootOperationID: input.rootOperationID ?? activeExecution?.rootOperationID,
          parentSessionID: input.parentSessionID ?? source.sessionID,
          parentActorID: source.actorID,
          forkContextSource: source,
          encodedForkContext,
          model: input.model ?? input.forkContext.model,
          ...(inheritedTools ? { tools: inheritedTools } : {}),
        }
      } else if (input.context === "full" && !input.actorID) {
        return yield* Effect.die(new Error("new full-context actors require a complete captured ForkContext"))
      }
      if (prepared.mode === "peer") return yield* spawnPeer(prepared)
      return yield* spawnSubagent(prepared)
    })

    const spawn = Effect.fn("Actor.spawn")(function* (input: SpawnInput) {
      const tq = inboxControllerRef.current
      // Bind delivery to the parent's epoch before any child execution can exist.
      const expectedEpoch = tq ? yield* tq.getEpoch(input.parentSessionID ?? input.sessionID) : undefined
      return yield* Effect.uninterruptibleMask((restore)=>Effect.gen(function* () {
      if (!(yield* actorReg.assertDirectoryActive(input.parentSessionID ?? input.sessionID, input.parentActorID))) return yield* Effect.die(new Error('parent directory scope retired'))
      if (input.actorID && !(yield* actorReg.assertDirectoryActive(input.sessionID,input.actorID))) return yield* Effect.die(new Error('actor directory scope retired'))
      const requested = input.cwd ?? (yield* InstanceState.directory)
      const directory = yield* Effect.tryPromise({try:()=>realpath(requested),catch:()=>new Error('actor directory unavailable')}).pipe(Effect.orDie)
      const lease = yield* actorReg.admitExecutionDirectory(directory)
      if (!lease) return yield* Effect.die(new Error('actor directory unavailable'))
      const admission:DirectoryAdmission = {lease,transferred:false,outer:false,retain:false}
      return yield* restore(spawnInner({...input,directoryAdmission:admission,expectedEpoch})).pipe(Effect.ensuring(Effect.sync(()=>{if (!admission.transferred && !admission.retain) lease.release()})))
      }))
    })

    const resumeInput = Effect.fn("Actor.resumeInput")(function* (input: {
      sessionID: SessionID
      actorID: string
      claim: InputHandoffClaim
      managedRoute?: ManagedProvider.RouteContext
    }) {
      if (!(yield* actorReg.assertDirectoryActive(input.sessionID,input.actorID))) return {kind:'unavailable',reason:'directory scope retired'} as const
      const inspectHandoff = actorReg.inspectInputHandoff
      const prior = inspectHandoff ? yield* inspectHandoff(input.sessionID, input.actorID, input.claim) : undefined
      if (prior) {
        const uncertain = ManagedProvider.enabled() && input.managedRoute
          ? yield* actorReg.uncertainManagedExecution(input.sessionID, input.actorID) : undefined
        if (!uncertain || prior.kind !== "existing-claim" || prior.claimedExecutionRevision !== uncertain.executionRevision) return prior
      }
      if (ManagedProvider.enabled() && input.managedRoute) {
        const available = yield* SessionPrompt.reconcileManagedActorExecution(actorReg, input.sessionID, input.actorID,
          state.assertNotBusy(input.sessionID, input.actorID),
          (revision) => Effect.sync(() => outerFibers.get(cancelKey(input.sessionID, input.actorID))?.revision === revision)).pipe(
            Effect.as(true), Effect.catch(() => Effect.succeed(false)),
          )
        if (!available) return { kind: "unavailable", reason: "actor execution is busy" } as const
      }
      if (prior && inspectHandoff) return (yield* inspectHandoff(input.sessionID, input.actorID, input.claim)) ?? prior
      const actor = yield* actorReg.get(input.sessionID, input.actorID)
      if (!actor) return { kind: "unavailable", reason: "actor membership is unavailable" } as const
      if (actor.status !== "idle" || actor.executionRevision !== input.claim.expectedExecutionRevision)
        return { kind: "unavailable", reason: "actor is not idle at the expected execution revision" } as const
      if (actor.hostContext?.chatID !== input.claim.stableChatID)
        return { kind: "unavailable", reason: "actor is not bound to this host chat" } as const
      const forkProjection = actor.mode !== "main" && actor.contextMode === "full"
        ? yield* getForkContext(input.sessionID, input.actorID)
        : undefined
      const forkContext = forkProjection?.context
      if (actor.mode !== "main" && actor.contextMode === "full" && !forkProjection)
        return { kind: "unavailable", reason: "full-context actor snapshot is unavailable for resume" } as const
      let agentType = actor.agent
      let effectiveModel = actor.effectiveModel
      if (actor.mode === "main") {
        const messages = yield* session.messages({ sessionID: input.sessionID, agentID: "main" })
        const latestUser = messages.findLast((message) => message.info.role === "user")
        if (latestUser?.info.role === "user") {
          agentType = latestUser.info.agent
          effectiveModel ??= { providerID: latestUser.info.model.providerID, modelID: latestUser.info.model.modelID }
        }
      }
      if (!effectiveModel) return { kind: "unavailable", reason: "actor effective model is unavailable" } as const
      const agentInfo = yield* agents.get(agentType)
      if (!agentInfo) return { kind: "unavailable", reason: "actor agent configuration is unavailable" } as const
      if (ManagedProvider.enabled() && !input.managedRoute) return { kind: "route-required" } as const
      if (input.managedRoute && (input.managedRoute.rootOperationID !== input.claim.rootOperationID ||
          input.managedRoute.modelID !== effectiveModel.modelID ||
          (input.managedRoute.providerID !== effectiveModel.providerID && input.managedRoute.connectionID !== effectiveModel.providerID)))
        return { kind: "unavailable", reason: "managed route does not match the actor's frozen model" } as const
      const frozenReceipts = yield* Effect.sync(() => UserInput.snapshotBoundary({
        sessionID: input.sessionID, actorID: input.actorID,
        binding: { owner: input.claim.owner, stableChatID: input.claim.stableChatID, mapRevision: input.claim.mapRevision,
          mappingRevision: input.claim.mappingRevision, workspaceRevision: input.claim.workspaceRevision },
        afterTurnReady: true,
        prepared: { expectedExecutionRevision: input.claim.expectedExecutionRevision, requestID: input.claim.requestID },
      }))
      if (!frozenReceipts.length) return { kind: "unavailable", reason: "the requested input is not the eligible ordered batch prefix" } as const
      const frozenModel = frozenReceipts[0]!.modelRequestOptions.model
      if (frozenModel && (frozenModel.providerID !== effectiveModel.providerID || frozenModel.modelID !== effectiveModel.modelID))
        return { kind: "unavailable", reason: "receipt model does not match the actor's fixed model" } as const
      if (input.managedRoute && frozenReceipts[0]!.modelRequestOptions.routeID &&
          frozenReceipts[0]!.modelRequestOptions.routeID !== input.managedRoute.modelRouteID)
        return { kind: "unavailable", reason: "receipt route does not match the admitted actor route" } as const
      const currentSession = yield* session.get(input.sessionID)
      const parentSessionID = actor.mode === "peer" ? (currentSession.parentID ?? input.sessionID) : input.sessionID
      const requestedDirectory = actor.hostContext?.cwd ?? currentSession.directory
      const resolvedDirectory = yield* Effect.tryPromise({try:()=>realpath(requestedDirectory),catch:()=>new Error('actor directory unavailable')}).pipe(Effect.option)
      if (Option.isNone(resolvedDirectory)) return {kind:'unavailable',reason:'actor directory unavailable'} as const
      let disposition: InputHandoffDisposition = { kind: "unavailable", reason: "resume claim was not attempted" }
      const start = Effect.uninterruptibleMask((restore) => Effect.gen(function* () {
      const directoryLease = yield* actorReg.admitExecutionDirectory(resolvedDirectory.value)
      if (!directoryLease) return {kind:'unavailable',reason:'actor directory unavailable'} as const
      const directoryAdmission:DirectoryAdmission={lease:directoryLease,transferred:false,outer:false,retain:false}
      const startWork = Effect.gen(function* () {
      const instanceRef = yield* Effect.tryPromise({try:()=>Instance.provide({directory:requestedDirectory,fn:()=>Instance.current}),catch:()=>new Error('actor directory unavailable')}).pipe(Effect.orDie)
      const started = yield* forkWork({
        sessionID: input.sessionID,
        parentSessionID,
        parentActorID: actor.parentActorID,
        actorID: actor.actorID,
        agentType,
        task: "",
        description: actor.description,
        background: actor.background,
        model: effectiveModel as SpawnResult["effectiveModel"],
        requestedModel: actor.requestedModel,
        rootOperationID: input.claim.rootOperationID,
        hostContext: actor.hostContext,
        lifecycle: actor.lifecycle,
        forkContext,
        encodedForkContext: forkProjection?.encoded,
        receiptOnly: true,
        frozenReceipts,
        ...(input.managedRoute ? { frozenRoute: input.managedRoute } : {}),
        receiptClaim: { ...input.claim, agent: agentType, effectiveModel, expectedReceipts: frozenReceipts,
          ...(actor.hostContext ? { hostContext: actor.hostContext } : {}) },
        onClaimDisposition: (value) => { disposition = value },
        directoryAdmission,
        instanceRef,
      })
      const claimDisposition = disposition as InputHandoffDisposition
      if (started && claimDisposition.kind === "fresh-claim") {
        if (!actor.background) yield* Effect.forkIn(Fiber.join(started.fiber).pipe(Effect.ignore), scope)
      }
      return claimDisposition
      })
      return yield* restore(startWork).pipe(Effect.ensuring(Effect.sync(()=>{if (!directoryAdmission.transferred && !directoryAdmission.retain) directoryLease.release()})))
      }))
      return yield* start
    })

    // Terminal notification is upstream's notifier (actor/notification.ts); it
    // claims the one notification of an execution revision before it queues.
    const cancel: (
      sessionID: SessionID,
      actorID: string,
      mode: "graceful" | "forced",
      guard?: AdmissionGuard,
    ) => Effect.Effect<void> = Effect.fn("Actor.cancel")(function* (
      sessionID: SessionID,
      actorID: string,
      mode: "graceful" | "forced",
      guard?: AdmissionGuard,
    ) {
        const key = cancelKey(sessionID, actorID)
        // Our lane stays here: a cancel that meets a setup in progress waits for
        // it to park instead of racing its reserve. Inside the lane upstream's
        // ActorExecution owns the in-process side (cancel flag, interrupt, done).
        const cleanup = withExecutionLock(key,
            Effect.gen(function* () {
        // Snapshot the actor row before cancelActor tears state down — we need
        // its mode/agent/background/parentActorID to decide + address the notify.
        let actor = yield* actorReg.get(sessionID, actorID)
        if (!actor) return
        if (actor.status === "pending") {
          // A peer may be registered but not yet launched. Cancel that durable
          // pending row under the same actor lane so its deferred launcher
          // cannot begin a new revision after cancellation wins.
          const cancelled = yield* actorReg.cancelPending(sessionID, actorID)
          if (cancelled) {
            const pending = pendingOutcomes.get(key)
            if (pending) yield* Deferred.succeed(pending, { status: "cancelled" as const }).pipe(Effect.ignore)
            yield* notifyTerminal({
              sessionID, actorID, source: "pending", status: "cancelled", executionRevision: actor!.executionRevision, ...guard,
            })
            yield* Effect.sync(() => {
              clearForkContext(sessionID, actorID, actor!.executionRevision)
              pendingOutcomes.delete(key)
            })
            return
          }
          // The launcher may have won beginExecution between the snapshot and
          // the pending CAS. Re-read under this lane and continue through the
          // revision-aware running cancellation path instead of escaping it.
          const nowRunning = yield* actorReg.get(sessionID, actorID)
          if (!nowRunning || nowRunning.status === "pending") return
          actor = nowRunning
        }
        // Flag the live execution first: registry status can lag behind an
        // execution that was acquired but has not begun its revision yet.
        const live = yield* executions.current(sessionID, actorID)
        if (live) yield* executions.requestCancel(live)
        const activeOuter = outerFibers.get(key)
        // Cancellation is a durable CAS first. A completion/failure winner, or
        // a second cancellation, becomes a strict no-op and cannot interrupt or
        // overwrite the already-settled execution.
        const settled = yield* actorReg
          .settleExecution(sessionID, actorID, actor.executionRevision, "cancelled")
          .pipe(Effect.orElseSucceed(() => false))
        if (!settled) {
          // An execution acquired outside this module that has not begun a
          // revision has nothing durable to settle; stop it the upstream way.
          if (live && activeOuter?.execution !== live) {
            yield* state.cancelActor(sessionID, actorID)
            yield* executions.interrupt(live)
          }
          return
        }
        yield* Effect.sync(() => cancelling.add(key))
        // The actor fiber may be awaiting SessionRunState's main runner. Stop
        // that nested runner first so its interruption wakes the outer wait;
        // interrupting only the waiter can otherwise wait forever for the
        // nested runner to finish on its own.
        yield* state.cancelActor(sessionID, actorID)
        const owned = activeOuter?.revision === actor.executionRevision
        if (owned) outerFibers.delete(key)
        const target = owned ? activeOuter!.execution : live
        if (target) {
          const interruptFiber = yield* executions.interrupt(target).pipe(Effect.forkIn(scope))
          yield* Fiber.join(interruptFiber).pipe(Effect.ignore)
        }
        // A target whose cancellation CAS lost is a complete no-op, including
        // its descendants. Only the winner is allowed to cascade interruption.
        const children = yield* actorReg.listByParent(sessionID, actorID)
        yield* Effect.forEach(children, (c) => cancel(sessionID, c.actorID, mode, guard), {
          concurrency: "unbounded",
          discard: true,
        })
        yield* notifyTerminal({
          sessionID, actorID, source: "pending", status: "cancelled", executionRevision: actor.executionRevision, ...guard,
        })
        if (owned) {
          yield* Deferred.succeed(activeOuter!.outcome, { status: "cancelled" as const }).pipe(Effect.ignore)
        }
        yield* Effect.sync(() => cancelling.delete(cancelKey(sessionID, actorID)))
        yield* Effect.sync(() => clearForkContext(sessionID, actorID, actor.executionRevision))
            }),
        )
        const cleanupFiber = yield* cleanup.pipe(Effect.forkIn(scope, { startImmediately: true }))
        yield* Fiber.join(cleanupFiber).pipe(Effect.ignore)
      })

    const intersectToolWhitelists = (...values: (readonly string[] | undefined)[]) => {
      const constrained = values.filter((value): value is readonly string[] => value !== undefined)
      if (!constrained.length) return undefined
      return [...constrained[0]!].filter((tool) => constrained.every((allowed) => allowed.includes(tool)))
    }

    const getForkContextInternal: (
      sessionID: SessionID,
      actorID: string,
      seen: Set<string>,
    ) => Effect.Effect<ForkContextProjection | undefined> = Effect.fn("Actor.getForkContextInternal")(function* (
      sessionID: SessionID,
      actorID: string,
      seen: Set<string>,
    ) {
      const key = forkContextKey(sessionID, actorID)
      if (seen.has(key)) return undefined
      const nextSeen = new Set(seen)
      nextSeen.add(key)
      const stored = yield* actorReg.getForkContext(sessionID, actorID).pipe(Effect.orElseSucceed(() => undefined))
      if (!stored) return undefined
      const cached = forkContexts.get(key)
      const context = cached?.hash === stored.snapshotSHA256
        ? cached.context
        : yield* Effect.tryPromise({
            try: () => decodeForkContext(stored),
            catch: () => new Error("stored full-context snapshot is invalid"),
          }).pipe(Effect.orElseSucceed(() => undefined))
      if (!context) return undefined
      if (!cached || cached.hash !== stored.snapshotSHA256)
        forkContexts.set(key, { revision: -1, hash: stored.snapshotSHA256, context })

      const destination = yield* actorReg.get(sessionID, actorID)
      const source = yield* actorReg.get(stored.sourceSessionID, stored.sourceActorID)
      if (!destination || !source) return undefined
      const sourceSession = yield* session.get(stored.sourceSessionID).pipe(Effect.orElseSucceed(() => undefined))
      const destinationSession = yield* session.get(sessionID).pipe(Effect.orElseSucceed(() => undefined))
      if (!sourceSession || !destinationSession) return undefined

      let sourceAgentName = source.agent
      if (stored.sourceActorID === "main") {
        const sourceMessages = yield* session.messages({ sessionID: stored.sourceSessionID, agentID: "main" })
        const latestUser = sourceMessages.findLast((message) => message.info.role === "user")
        if (!latestUser || latestUser.info.role !== "user" || !latestUser.info.agent) return undefined
        sourceAgentName = latestUser.info.agent
      }
      const sourceAgent = yield* agents.get(sourceAgentName).pipe(Effect.orElseSucceed(() => undefined))
      const destinationAgent = yield* agents.get(destination.agent).pipe(Effect.orElseSucceed(() => undefined))
      if (!sourceAgent || !destinationAgent) return undefined
      const sourceProjection = source.mode !== "main" && source.contextMode === "full"
        ? yield* getForkContextInternal(stored.sourceSessionID, stored.sourceActorID, nextSeen)
        : undefined
      if (source.mode !== "main" && source.contextMode === "full" && !sourceProjection) return undefined
      const sourceTools = Array.isArray(source.tools) ? source.tools : undefined
      const destinationTools = Array.isArray(destination.tools) ? destination.tools : undefined
      const effectiveToolWhitelist = intersectToolWhitelists(
        sourceProjection?.effectiveToolWhitelist,
        sourceTools,
        destinationTools,
      )
      return {
        context,
        encoded: {
          formatVersion: stored.formatVersion,
          sourceSessionID: stored.sourceSessionID,
          sourceActorID: stored.sourceActorID,
          sourceWatermarkMessageID: stored.sourceWatermarkMessageID,
          modelProviderID: stored.modelProviderID as ProviderID,
          modelID: stored.modelID as ModelID,
          snapshotJSON: stored.snapshotJSON,
          snapshotSHA256: stored.snapshotSHA256,
          snapshotBytes: stored.snapshotBytes,
        },
        source: { sessionID: stored.sourceSessionID, actorID: stored.sourceActorID },
        permissionRulesets: [
          ...(sourceProjection?.permissionRulesets ?? [Agent.runtimePermission(sourceAgent, sourceSession.permission)]),
          Agent.runtimePermission(destinationAgent, destinationSession.permission),
        ],
        ...(effectiveToolWhitelist ? { effectiveToolWhitelist } :
          (sourceTools || destinationTools ? { effectiveToolWhitelist: [] } : {})),
      } satisfies ForkContextProjection
    })

    const getForkContext = Effect.fn("Actor.getForkContext")(function* (sessionID: SessionID, actorID: string) {
      return yield* getForkContextInternal(sessionID, actorID, new Set())
    })

    const hasOuterExecution = (sessionID: SessionID, actorID: string, executionRevision: number) =>
      Effect.sync(() => outerFibers.get(cancelKey(sessionID, actorID))?.revision === executionRevision)
    const hasManagedFinalizing = (sessionID: SessionID, actorID: string, identity: ManagedFinalizingIdentity) => Effect.sync(() => {
      const owner = outerFibers.get(cancelKey(sessionID, actorID))
      return !!owner && owner.revision === identity.executionRevision && executions.currentUnsafe(sessionID, actorID) === owner.execution && !!owner.finalizing &&
        Object.keys(identity).every((key) => identity[key as keyof ManagedFinalizingIdentity] === owner.finalizing![key as keyof ManagedFinalizingIdentity])
    })
    const impl = Service.of({ spawn, resumeInput, cancel, getForkContext, hasOuterExecution, hasManagedFinalizing })
    // Late-bind the impl so SessionCheckpoint.tryStartCheckpointWriter can resolve it
    // without forming a layer cycle. See spawn-ref.ts for rationale.
    // Save the previous binding so the finalizer can restore it: when the same
    // process initialises Actor.layer more than once (memo'd ManagedRuntimes,
    // overlapping test runtimes, etc.) the inner scope's dispose must hand
    // control back to the outer scope's impl instead of wiping the ref to
    // `undefined` and breaking every subsequent tryStartCheckpointWriter call.
    const unregisterBinding = registerBinding(impl)
    yield* Effect.addFinalizer(() => Effect.sync(unregisterBinding))
    return impl
  }),
).pipe(Layer.provideMerge(ActorExecution.layer))

// Wrapped in Layer.suspend so the cross-module `.defaultLayer` reads defer to
// first use instead of running at module load. Without this, the
// spawn → prompt → app-runtime import cycle hits a load order where
// AppLayer's mergeAll runs while SessionPrompt is mid-init and throws
// "Cannot access 'defaultLayer' before initialization", breaking every
// it.live test harness. Same pattern session/prompt, session/checkpoint,
// tool/registry, provider, etc. already use.
/**
 * Application composition variant. Leaves `SessionPrompt.Service` unmet so the
 * root AppLayer chain (Actor.appLayer ← SessionPrompt.appLayer ← Command.appLayer ←
 * MCP.defaultLayer) provides each stateful service once. Actor reaches MCP only
 * through SessionPrompt — SessionPrompt.defaultLayer would smuggle a second MCP.
 */
export const appLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Session.defaultLayer),
    Layer.provide(ActorRegistry.defaultLayer),
    Layer.provide(Agent.defaultLayer),
    Layer.provide(SessionRunState.defaultLayer),
    Layer.provide(Inbox.defaultLayer),
    Layer.provide(Plugin.defaultLayer),
    Layer.provide(Bus.layer),
    Layer.provide(TaskRegistry.defaultLayer),
  ),
)

export const defaultLayer = Layer.suspend(() => appLayer.pipe(Layer.provide(SessionPrompt.defaultLayer)))

export * as Actor from "./spawn"
