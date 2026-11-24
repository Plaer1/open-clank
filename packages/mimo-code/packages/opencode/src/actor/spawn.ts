import { Effect, Deferred, Context, Fiber, Layer, Scope, Cause, Exit, Schedule, Option } from "effect"
import type { SessionID } from "@/session/schema"
import type { ProviderID, ModelID } from "@/provider/schema"
import { Session } from "@/session"
import { SessionPrompt } from "@/session/prompt"
import * as UserInput from "@/session/user-input"
import { SessionRunState } from "@/session/run-state"
import { ActorRegistry } from "@/actor/registry"
import * as ManagedProvider from "@/acp/managed-provider"
import type { ActorRoutePreflightResult, ActorOptionCandidate } from "@/acp/openclank-protocol"
import type { InputHandoffClaim, InputHandoffDisposition } from "@/actor/registry"
import { TaskRegistry } from "@/task/registry"
import { TaskGate, MAX_TASK_GATE_SUBAGENT_REACT } from "@/task/gate"
import { Agent } from "@/agent/agent"
import { Permission } from "@/permission"
import type { Actor, SpawnMode, ContextMode, ToolWhitelist, Lifecycle, ActorHostContext } from "@/actor/schema"
import { deriveLiveness, DEFAULT_LIVENESS_STALL_MS } from "@/actor/schema"
import * as ActorEvents from "@/actor/events"
import { ActorExecutionContext, runTurn } from "@/actor/turn"
import { registerBinding } from "@/actor/spawn-ref"
import * as ExecutionLock from "@/actor/execution-lock"
import { SYSTEM_SPAWNED_AGENT_TYPES } from "@/agent/config"
import { Bus } from "@/bus"
import { TuiEvent } from "@/cli/cmd/tui/event"
import { MessageV2 } from "@/session/message-v2"
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

type DirectoryAdmission = { lease: DirectoryLease; transferred: boolean; outer: boolean; retain: boolean }
type AdmittedSpawn = SpawnInput & { directoryAdmission: DirectoryAdmission }

export type { ForkContext } from "./fork-context"

const log = Log.create({ service: "actor.spawn" })

/**
 * Cap on preStop ReAct re-entries per spawn — prevents infinite loops.
 * TODO: lift to mimocode.json config (e.g. actor.maxPreReact) and add per-hook
 * `maxContinue` clamp at registration. Plan: platform cap = hard ceiling, hook
 * cap may only narrow, never widen. See spec Future work.
 */
export const MAX_PRE_REACT = 3
/** Cap on postStop ReAct re-entries per spawn. See MAX_PRE_REACT TODO. */
export const MAX_POST_REACT = 3
/**
 * T40 stall watchdog scan cadence. Sits between the per-step turn heartbeat and
 * the DEFAULT_LIVENESS_STALL_MS (90s) window, and just under the registry's own
 * 60s stuck-scan, so a genuinely stalled child is caught within ~one window of
 * flipping to `stalled` without hammering the DB.
 */
export const WATCHDOG_SCAN_INTERVAL_MS = 45_000
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
      // Subagent's self-reported header status (parsed from finalText), possibly
      // overridden by the completion gate (DB truth wins — see onSuccess).
      reportedStatus?: ReturnStatus
      reportedSummary?: string
      // Task IDs the subagent left non-terminal after the gate's cap. Present
      // only when reportedStatus was downgraded to "partial"/"blocked".
      incompleteTasks?: string[]
    }
  | { status: "failure"; error: string }
  | { status: "cancelled" }

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
  /** User/config request before resolving groups and inheritance. */
  requestedModel?: string
  /** Trusted exact parent execution value, never model-authored. */
  optionCandidate?: ActorOptionCandidate
  /** Host-owned account/grant/credential/workspace binding for this actor. */
  hostContext?: ActorHostContext
  /** Private host-user binding, supplied only by an exact admitted parent. */
  originalOwnerAccountID?: string
  /** Existing actor ID for an explicit continuation. */
  actorID?: string
  /** Internal root lineage; overwritten from the trusted active execution context. */
  rootOperationID?: string
  background: boolean
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
  readonly cancel: (sessionID: SessionID, actorID: string, mode: "graceful" | "forced") => Effect.Effect<void>
  readonly getForkContext: (sessionID: SessionID, actorID: string) => Effect.Effect<ForkContextProjection | undefined>
  /**
   * Run ONE stall-watchdog scan pass synchronously (the same body the background
   * fiber repeats every WATCHDOG_SCAN_INTERVAL_MS). Exposed for deterministic
   * tests that can't wait a real scan interval — it shares the same `notified`
   * debounce set as the fiber, so driving it repeatedly exercises the real
   * one-shot / re-arm semantics without touching wall-clock scheduling.
   * Optional so lightweight test mocks of this Service need not implement it.
   */
  readonly scanStalledOnce?: () => Effect.Effect<void>
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
    // cleared on terminal status. Fiber tracking moved to SessionRunState.
    const forkContexts = new Map<string, { revision: number; hash: string; context: ForkContext }>()
    const forkContextKey = (sessionID: SessionID, actorID: string) => `${sessionID}:${actorID}`
    const clearForkContext = (sessionID: SessionID, actorID: string, revision: number) => {
      const key = forkContextKey(sessionID, actorID)
      const current = forkContexts.get(key)
      if (current?.revision === revision) forkContexts.delete(key)
    }

    // Outer actor fibers are registered before onActorID is invoked. This
    // closes the pre-registration window where workflow reclaim could see a
    // pending actor but have no fiber to interrupt yet.
    const outerFibers = new Map<
      string,
      { revision: number; fiber: Fiber.Fiber<unknown, unknown>; outcome: Deferred.Deferred<AgentOutcome> }
    >()
    const pendingOutcomes = new Map<string, Deferred.Deferred<AgentOutcome>>()

    // Serialize durable generation handoff with cancellation teardown. The
    // registry CAS alone cannot fence the later unversioned SessionRunState
    // interrupt, so the same actor lane covers begin→park and cancel CAS→
    // inner-runner/descendant teardown.
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
        return yield* Effect.fail(new Error(`Actor assistant failed: ${info.error.name}`))
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
      model?: { providerID: ProviderID; modelID: ModelID }
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
      // True for non-specialized subagents (those that received
      // RETURN_FORMAT_INSTRUCTION). Only these are subject to the completion
      // gate; specialized/system agents and peers create no user tasks.
      gateEligible?: boolean
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
      const executionKey = cancelKey(input.sessionID, input.actorID)
      const retirement: { unknown: boolean; lastInnerTurnID?: string; lastTurnOutcome?: "completed" | "cancelled" | "error" } = { unknown: false }
      const settlement = () => ManagedProvider.enabled() && input.rootOperationID && retirement.lastInnerTurnID
        ? { rootOperationID: input.rootOperationID, rootSessionID: preflightRoute?.bootstrap.rootSessionID, innerTurnID: retirement.lastInnerTurnID, turnOutcome: retirement.lastTurnOutcome } : undefined
      const retireManagedExecution = (revision: number, outcome: "success" | "failure" | "cancelled") => Effect.gen(function* () {
        const identity = settlement()
        if (!identity || retirement.unknown) return
        const result = yield* Effect.tryPromise({
          try: () => ManagedProvider.finishActorExecution({
            sessionID: input.sessionID, actorID: input.actorID, rootOperationID: identity.rootOperationID, executionRevision: revision,
          }, outcome),
          catch: (cause) => new Error(`managed execution retirement failed: ${String(cause)}`),
        }).pipe(Effect.exit)
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
              const releaseExit = yield* (operationRelease ?? Effect.void).pipe(Effect.exit)
              if (Exit.isFailure(releaseExit)) retirement.unknown = true
              if (retirement.unknown) input.directoryAdmission.retain = true
              if (installedFiber) yield* Fiber.interrupt(installedFiber).pipe(Effect.ignore)
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
          yield* Fiber.interrupt(previous.fiber).pipe(Effect.ignore)
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
        const description = input.description ?? input.agentType
        const notify = (
          status: "completed" | "failed" | "cancelled",
          extra: { result?: string; error?: string; reportedStatus?: ReturnStatus; reportedSummary?: string },
          revision: number,
        ) =>
          Effect.gen(function* () {
            if (!input.background || input.agentType === "checkpoint-writer") return
            const outcome = status === "completed" ? "success" : status === "failed" ? "failure" : "cancelled"
              if (!(yield* actorReg.claimTerminalNotification(input.sessionID, input.actorID, revision, outcome)))
                return
            yield* inbox
              .send({
                receiverSessionID: input.parentSessionID,
                receiverActorID: input.parentActorID ?? "main",
                senderSessionID: input.sessionID,
                senderActorID: input.actorID,
                type: "actor_notification",
                notification: {
                  actorID: input.actorID,
                  description,
                  status,
                  ...(input.requestedModel ? { requestedModel: input.requestedModel } : {}),
                    ...(input.model
                      ? { effectiveModel: { providerID: input.model.providerID, modelID: input.model.modelID } }
                      : {}),
                  ...(executionHostContext ? { hostContext: executionHostContext } : {}),
                  ...extra,
                },
              })
              .pipe(Effect.ignore)
            yield* Effect.promise(() =>
              Bus.publish(TuiEvent.ToastShow, {
                message: `Child "${description}" ${status}`,
                variant: status === "completed" ? "success" : status === "cancelled" ? "info" : "error",
              }),
            ).pipe(Effect.ignore)
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
        // ignored: not reachable in normal usage (mimo run sets no such rule, spawn doesn't
        // rewrite session.permission). See spec §Decision. Unknown agent → fail-open (true).
        const forkAgentInfo = yield* agents.get(input.agentType)
        const canWrite = forkAgentInfo ? !Permission.disabled(["write"], forkAgentInfo.permission).has("write") : true

        const work = Effect.gen(function* () {
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
              { executionRevision, rootOperationID: input.rootOperationID, preflightRoute, retirement, ...(iteration === 0 && input.frozenReceipts ? { frozenReceipts: input.frozenReceipts } : {}) },
            )
            finalText = turn.finalText
            structured = turn.structured

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
          Effect.provideService(ActorRegistry.Service, actorReg),
          Effect.matchCauseEffect({
            onSuccess: ({ finalText, structured }) =>
              Effect.gen(function* () {
                // === COMPLETION GATE (B) + structured parse (A) ===
                // Delegates the list/decide step to TaskGate.decide.
                // We retain the runTurn re-entry + delivered-text update here
                // because that is gate-policy, not list-policy.
                let deliveredText = finalText
                if (input.gateEligible) {
                  let gateIter = 0
                  while (true) {
                    const decision = yield* TaskGate.decide({
                      session_id: input.parentSessionID,
                      owner: input.actorID,
                      reactCount: gateIter,
                      maxReact: MAX_TASK_GATE_SUBAGENT_REACT,
                    }).pipe(Effect.provideService(TaskRegistry.Service, taskRegistry))
                    if (!decision.needReentry) break
                    gateIter++
                    const gateTurn = yield* runTurn(
                      input.sessionID,
                      input.actorID,
                      runAgentLoop({
                        ...input,
                        task: decision.reentryText,
                        receiptOnly: false,
                        source: "hook",
                        provenance: { hookPhase: "post", hookIteration: gateIter, pluginNames: [], hookIDs: [] },
                      }),
                      { executionRevision, rootOperationID: input.rootOperationID, preflightRoute, retirement },
                    ).pipe(
                      Effect.catch(() =>
                        Effect.gen(function* () {
                          log.error("actor.gate runTurn failed", { actorID: input.actorID })
                          return { finalText: undefined as string | undefined, structured: undefined as unknown }
                        }),
                      ),
                      Effect.provideService(ActorRegistry.Service, actorReg),
                    )
                    // The gate re-run's re-emitted text updates the delivered body
                    // (and structured, if it produced one) so the reconciliation +
                    // delivery below see the latest turn.
                    if (gateTurn.finalText !== undefined) deliveredText = gateTurn.finalText
                    if (gateTurn.structured !== undefined) structured = gateTurn.structured
                  }
                }

                // Reconcile: DB truth wins over the model's self-reported header.
                const remaining = input.gateEligible
                  ? yield* taskRegistry
                      .list({ session_id: input.parentSessionID, owner: input.actorID, include_terminal: false })
                      .pipe(Effect.orElseSucceed(() => []))
                  : []
                const stillActionable = remaining.filter((t) => t.status === "open" || t.status === "in_progress")
                const downgrade: ReturnStatus | undefined =
                  stillActionable.length > 0 ? "partial" : remaining.length > 0 ? "blocked" : undefined
                const parsed = parseReturnHeader(deliveredText)
                const reportedStatus = downgrade ?? parsed.status
                const incompleteTasks = remaining.map((t) => t.id)
                const reconciledText =
                  downgrade && incompleteTasks.length > 0
                    ? `${deliveredText ?? ""}\n\n**Incomplete tasks**: ${incompleteTasks.join(", ")}`
                    : deliveredText

                // === DELIVERY ===
                // structured (json_schema result) takes precedence over text for the
                // notification body (DW spec P3 §5.2); otherwise deliver the gate's
                // reconciled text. The success outcome carries both the reconciled
                // text + completion-gate fields AND structured when present.
                const deliveryText =
                  structured !== undefined ? JSON.stringify(structured) : (reconciledText ?? "(no output)")
                let settled = yield* actorReg
                  .settleExecution(input.sessionID, input.actorID, executionRevision, "success", undefined, settlement())
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
                  yield* notify(
                    "completed",
                    {
                  result: deliveryText,
                  ...(reportedStatus ? { reportedStatus } : {}),
                  ...(parsed.summary ? { reportedSummary: parsed.summary } : {}),
                    },
                    executionRevision,
                  )
                yield* Deferred.succeed(outcome, {
                  status: "success" as const,
                  ...(reconciledText !== undefined ? { finalText: reconciledText } : {}),
                  ...(structured !== undefined ? { structured } : {}),
                  ...(reportedStatus ? { reportedStatus } : {}),
                  ...(parsed.summary ? { reportedSummary: parsed.summary } : {}),
                  ...(incompleteTasks.length > 0 ? { incompleteTasks } : {}),
                })

                // === postStop ReAct loop ===
                // Caller has already resolved; new finalTexts are not propagated.
                // NOTE: parallel structure to preStop loop above — pre runs turn THEN checks,
                // post checks THEN runs turn. Both give 1 (delivery) + MAX_POST_REACT re-entries.
                let postIter = 0
                let lastFinalText = finalText
                let postReentry:
                  | { reason: string; contributingPluginNames: string[]; contributingHookIDs: string[] }
                  | undefined

                while (input.actorID !== "main") {
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

                  // Run another turn (new finalText is not written back to outcome)
                  const newTurn = yield* runTurn(
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
                    { executionRevision, rootOperationID: input.rootOperationID, preflightRoute, retirement },
                  ).pipe(
                    // postStop LLM failure: log + break loop, do NOT propagate
                    Effect.catch(() =>
                      Effect.gen(function* () {
                        log.error("actor.postStop runTurn failed", {
                          actorID: input.actorID,
                        })
                        return { finalText: undefined as string | undefined, structured: undefined as unknown }
                      }),
                    ),
                    Effect.provideService(ActorRegistry.Service, actorReg),
                  )

                  if (newTurn.finalText === undefined) break
                  lastFinalText = newTurn.finalText
                }

                yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, executionRevision))
                yield* Effect.sync(() => cancelling.delete(cancelKey(input.sessionID, input.actorID)))
              }),
            onFailure: (cause) =>
              Effect.gen(function* () {
                const cancelled = Cause.hasInterruptsOnly(cause)
                const error = Cause.pretty(cause)
                const status: "cancelled" | "failure" = cancelled ? "cancelled" : "failure"
                if (retirement.unknown) yield* actorReg.markExecutionUncertain(input.sessionID, input.actorID, executionRevision, settlement()).pipe(Effect.ignore)
                const settled = retirement.unknown ? false : yield* actorReg
                  .settleExecution(
                    input.sessionID,
                    input.actorID,
                    executionRevision,
                    status,
                    cancelled ? undefined : error,
                    settlement(),
                  )
                  .pipe(Effect.orElseSucceed(() => false))
                if (settled) {
                  yield* retireManagedExecution(executionRevision, cancelled ? "cancelled" : "failure")
                  yield* notify(cancelled ? "cancelled" : "failed", cancelled ? {} : { error }, executionRevision)
                }
                if (!cancelled || !cancelling.has(cancelKey(input.sessionID, input.actorID))) {
                  yield* Deferred.succeed(
                    outcome,
                    cancelled ? { status: "cancelled" as const } : { status: "failure" as const, error },
                  )
                }
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
              yield* taskRegistry
                .start({ session_id: input.parentSessionID, id: input.task_id, owner: input.actorID })
                .pipe(Effect.ignoreCause({ log: "Warn", message: `auto-start of task ${input.task_id} failed` }))
            }
            return yield* boundWork
          })
        const key = executionKey
        installedKey = key
        installedRevision = executionRevision
        const outerWork = gatedWork.pipe(
            Effect.ensuring(Effect.gen(function* () {
              const releaseExit=yield* (operationRelease ?? Effect.void).pipe(Effect.exit)
              if (Exit.isFailure(releaseExit)) retirement.unknown=true
              if (retirement.unknown) input.directoryAdmission.retain=true
              if (!retirement.unknown) input.directoryAdmission.lease.release()
              if (Exit.isFailure(releaseExit)) return yield* Effect.failCause(releaseExit.cause)
            })),
            Effect.ensuring(
              Effect.sync(() => {
                const current = outerFibers.get(key)
                if (current?.revision === executionRevision) outerFibers.delete(key)
              }),
            ),
          )
          // Setup/claim stays masked through registration, but detached actor
          // work must inherit an interruptible status even when forkWork is
          // called from its masked setup/transfer block.
          const fiber = yield* outerWork.pipe(Effect.interruptible, Effect.forkIn(scope))
          input.directoryAdmission.outer = true
          input.directoryAdmission.transferred = true
          installedFiber = fiber
          const currentOuter = outerFibers.get(key)
          if (!currentOuter || currentOuter.revision < executionRevision) {
            outerFibers.set(key, { revision: executionRevision, fiber, outcome })
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
              modelSource: input.requestedModel ? "explicit" : "inherited",
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
        return yield* withExecutionLock(executionKey,
          Effect.uninterruptibleMask((restore) =>
            Effect.gen(function* () {
              const current = yield* actorReg.get(input.sessionID, input.actorID)
              const revision = begunRevision!
              if (!current || current.status !== "running" || current.executionRevision !== revision) {
                const installed = outerFibers.get(executionKey)
                if (installed?.revision === revision) outerFibers.delete(executionKey)
                yield* Fiber.interrupt(parked.fiber).pipe(Effect.ignore)
              yield* Deferred.succeed(parked.outcome, settledOutcome(current)).pipe(Effect.ignore)
              yield* Effect.sync(() => clearForkContext(input.sessionID, input.actorID, revision))
                return { fiber: parked.fiber, outcome: parked.outcome }
              }
              yield* Deferred.succeed(parked.startGate, undefined)
              return { fiber: parked.fiber, outcome: parked.outcome }
            }),
          ),
        )
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
        ...(input.cwd ? { directory: input.cwd } : {}),
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
                model: input.model,
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
          const gateEligible =
            agentInfo?.mode === "subagent" && !agentInfo?.prompt && input.agentType !== "checkpoint-writer"
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
        ...(input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
        ...(runEncodedForkContext ? { forkContext: runEncodedForkContext } : {}),
      }))
      const actorID = actor.actorID

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
        model: persistedEffectiveModel,
        requestedModel: persistedRequestedModel,
        ...(input.optionCandidate ? { optionCandidate: input.optionCandidate } : {}),
        rootOperationID: input.rootOperationID,
        hostContext: inheritedHostContext,
        ...(!existing && input.originalOwnerAccountID ? { originalOwnerAccountID:input.originalOwnerAccountID } : {}),
        lifecycle: input.lifecycle ?? "ephemeral",
        forkContext: runForkContext,
        encodedForkContext: runEncodedForkContext,
        task_id: input.task_id,
            onActorID: input.onActorID,
        gateEligible,
        format: input.format,
        instanceRef,
        directoryAdmission: input.directoryAdmission,
      })
      if (!started) return yield* Effect.die(new Error("Subagent execution did not start"))
      const { fiber, outcome } = started
      if (input.onReady) yield* Effect.ignore(input.onReady({ actorID, sessionID: input.sessionID }))
          if (!input.background) yield* restore(Fiber.join(fiber).pipe(Effect.ignore))
      return {
        actorID,
        sessionID: input.sessionID,
        outcome,
        ...(persistedRequestedModel ? { requestedModel: persistedRequestedModel } : {}),
        ...(persistedEffectiveModel ? { effectiveModel: persistedEffectiveModel } : {}),
        ...(inheritedHostContext ? { hostContext: inheritedHostContext } : {}),
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
      return yield* Effect.uninterruptibleMask((restore)=>Effect.gen(function* () {
      if (!(yield* actorReg.assertDirectoryActive(input.parentSessionID ?? input.sessionID, input.parentActorID))) return yield* Effect.die(new Error('parent directory scope retired'))
      if (input.actorID && !(yield* actorReg.assertDirectoryActive(input.sessionID,input.actorID))) return yield* Effect.die(new Error('actor directory scope retired'))
      const requested = input.cwd ?? (yield* InstanceState.directory)
      const directory = yield* Effect.tryPromise({try:()=>realpath(requested),catch:()=>new Error('actor directory unavailable')}).pipe(Effect.orDie)
      const lease = yield* actorReg.admitExecutionDirectory(directory)
      if (!lease) return yield* Effect.die(new Error('actor directory unavailable'))
      const admission:DirectoryAdmission = {lease,transferred:false,outer:false,retain:false}
      return yield* restore(spawnInner({...input,directoryAdmission:admission})).pipe(Effect.ensuring(Effect.sync(()=>{if (!admission.transferred && !admission.retain) lease.release()})))
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
        yield* SessionPrompt.reconcileManagedActorExecution(actorReg, input.sessionID, input.actorID,
          state.assertNotBusy(input.sessionID, input.actorID),
          (revision) => Effect.sync(() => outerFibers.get(cancelKey(input.sessionID, input.actorID))?.revision === revision))
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
        gateEligible: agentInfo.mode === "subagent" && !agentInfo.prompt && actor.agent !== "checkpoint-writer",
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

    // === Unified terminal notification (T41) ===
    // Single parent-notification contract for a terminal outcome. Completion and
    // failure already notify directly from forkWork.notify on the woken/spawn
    // turn; a CANCELLED child cannot, because its interrupt is masked by the
    // runLoop.onInterrupt handler (returns lastAssistant) so the work fiber sees
    // a spurious "success" and its own notify is suppressed via the `cancelling`
    // set. `cancel` therefore emits the one authoritative cancelled notification
    // here, reusing the exact shape forkWork.notify uses (renderActorNotification
    // + actor_notification inbox message + a TUI toast). Gating (background,
    // non-system peer/subagent) matches forkWork.notify so cancel/success/fail
    // stay a single contract with no double-notify. Best-effort: a missing row
    // or unresolved parent silently no-ops.
    const notifyTerminal = (
      sessionID: SessionID,
      actorID: string,
      actor: Actor | undefined,
      status: "cancelled",
      executionRevision: number,
    ) =>
      Effect.gen(function* () {
        if (!actor) return
        if (!actor.background) return
        if (actor.mode !== "peer" && actor.mode !== "subagent") return
        if (SYSTEM_SPAWNED_AGENT_TYPES.has(actor.agent)) return
        if (!(yield* actorReg.claimTerminalNotification(sessionID, actorID, executionRevision, "cancelled"))) return
        // Resolve the parent session: a peer runs in its own child session (notify
        // its parentID); a subagent shares the parent's session.
        const parentSessionID = actor.mode === "peer" ? (yield* session.get(sessionID)).parentID : sessionID
        if (!parentSessionID) return
        yield* inbox
          .send({
            receiverSessionID: parentSessionID,
            receiverActorID: actor.parentActorID ?? "main",
            senderSessionID: sessionID,
            senderActorID: actorID,
            type: "actor_notification",
            notification: {
              actorID,
              description: actor.description,
              status,
              ...(actor.requestedModel ? { requestedModel: actor.requestedModel } : {}),
              ...(actor.effectiveModel ? { effectiveModel: actor.effectiveModel } : {}),
              ...(actor.hostContext ? { hostContext: actor.hostContext } : {}),
            },
          })
          .pipe(Effect.ignore)
        yield* Effect.promise(() =>
          Bus.publish(TuiEvent.ToastShow, {
            message: `Child "${actor.description}" ${status}`,
            variant: "info",
          }),
        ).pipe(Effect.ignore)
      }).pipe(Effect.catchCause((cause) => Effect.logError(`terminal notify failed: ${cause}`)))

    const cancel: (sessionID: SessionID, actorID: string, mode: "graceful" | "forced") => Effect.Effect<void> =
      Effect.fn("Actor.cancel")(function* (sessionID: SessionID, actorID: string, mode: "graceful" | "forced") {
        const key = cancelKey(sessionID, actorID)
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
            yield* notifyTerminal(sessionID, actorID, actor!, "cancelled", actor!.executionRevision)
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
        // Cancellation is a durable CAS first. A completion/failure winner, or
        // a second cancellation, becomes a strict no-op and cannot interrupt or
        // overwrite the already-settled execution.
        const settled = yield* actorReg
          .settleExecution(sessionID, actorID, actor.executionRevision, "cancelled")
          .pipe(Effect.orElseSucceed(() => false))
        if (!settled) return
        yield* Effect.sync(() => cancelling.add(key))
        // The actor fiber may be awaiting SessionRunState's main runner. Stop
        // that nested runner first so its interruption wakes the outer wait;
        // interrupting only the waiter can otherwise wait forever for the
        // nested runner to finish on its own.
        yield* state.cancelActor(sessionID, actorID)
        const activeOuter = outerFibers.get(key)
        if (activeOuter?.revision === actor.executionRevision) {
          outerFibers.delete(key)
          const interruptFiber = yield* Fiber.interrupt(activeOuter.fiber).pipe(Effect.forkIn(scope))
          yield* Fiber.join(interruptFiber).pipe(Effect.ignore)
        }
        // A target whose cancellation CAS lost is a complete no-op, including
        // its descendants. Only the winner is allowed to cascade interruption.
        const children = yield* actorReg.listByParent(sessionID, actorID)
        yield* Effect.forEach(children, (c) => cancel(sessionID, c.actorID, mode), {
          concurrency: "unbounded",
          discard: true,
        })
        yield* notifyTerminal(sessionID, actorID, actor, "cancelled", actor.executionRevision)
        if (activeOuter?.revision === actor.executionRevision) {
          yield* Deferred.succeed(activeOuter.outcome, { status: "cancelled" as const }).pipe(Effect.ignore)
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

    // === T40 stall watchdog ===
    // Event-driven stall detection: a background fiber periodically scans active
    // background actors (ActorRegistry.listActive → pending/running + background),
    // computes deriveLiveness for each, and when a PEER/subagent flips to
    // `stalled` (running/pending but now-lastTurnTime > DEFAULT_LIVENESS_STALL_MS
    // AND turnCount not advancing — deriveLiveness encodes exactly that) pushes
    // ONE actor_notification{stalled} to its parent. Reuses the notifyTerminal
    // shape (inbox.send actor_notification + renderActorNotification + a TUI
    // toast) so stalled joins completed/failed/cancelled on one contract.
    //
    // Debounce — the crux: `notified` holds the "sessionID:actorID" of actors we
    // have ALREADY warned about for their CURRENT stall episode. We emit only on
    // the not-yet-notified → stalled edge; while it STAYS stalled across ticks it
    // is in `notified` and we skip. We re-arm (delete the key) the moment the
    // actor is no longer stalled — it resumed (turnCount advanced so
    // deriveLiveness reads `progressing`), went terminal, or vanished — so a
    // later re-stall notifies again. One notification per stall episode.
    const notified = new Set<string>()

    // Emit the single stalled notification for one actor. Same gating +
    // parent-resolution as notifyTerminal: background only, peer/subagent only,
    // exclude SYSTEM_SPAWNED_AGENT_TYPES, address the parent's main inbox.
    const notifyStalled = (actor: Actor, stalledForMs: number) =>
      Effect.gen(function* () {
        if (!actor.background) return
        if (actor.mode !== "peer" && actor.mode !== "subagent") return
        if (SYSTEM_SPAWNED_AGENT_TYPES.has(actor.agent)) return
        const parentSessionID = actor.mode === "peer" ? (yield* session.get(actor.sessionID)).parentID : actor.sessionID
        if (!parentSessionID) return
        yield* inbox
          .send({
            receiverSessionID: parentSessionID,
            receiverActorID: actor.parentActorID ?? "main",
            senderSessionID: actor.sessionID,
            senderActorID: actor.actorID,
            type: "actor_notification",
            notification: {
              actorID: actor.actorID,
              description: actor.description,
              status: "stalled",
              ...(actor.requestedModel ? { requestedModel: actor.requestedModel } : {}),
              ...(actor.effectiveModel ? { effectiveModel: actor.effectiveModel } : {}),
              ...(actor.hostContext ? { hostContext: actor.hostContext } : {}),
              stalledForMs,
            },
          })
          .pipe(Effect.ignore)
        yield* bus
          .publish(ActorEvents.ActorStalled, {
            sessionID: actor.sessionID,
            actorID: actor.actorID,
            description: actor.description,
            lastTurnTime: actor.lastTurnTime,
            stalledDuration: stalledForMs,
            ...(actor.requestedModel ? { requestedModel: actor.requestedModel } : {}),
            ...(actor.effectiveModel ? { effectiveModel: actor.effectiveModel } : {}),
            ...(actor.hostContext ? { hostContext: actor.hostContext } : {}),
          })
          .pipe(Effect.ignore)
        yield* Effect.promise(() =>
          Bus.publish(TuiEvent.ToastShow, {
            message: `Child "${actor.description}" appears stalled`,
            variant: "info",
          }),
        ).pipe(Effect.ignore)
      }).pipe(Effect.catchCause((cause) => Effect.logError(`stall notify failed: ${cause}`)))

    const scanStalled = Effect.gen(function* () {
      const now = Date.now()
      const active = yield* actorReg.listActive().pipe(Effect.orElseSucceed(() => [] as Actor[]))
      const seen = new Set<string>()
      for (const actor of active) {
        const key = `${actor.sessionID}:${actor.actorID}`
        seen.add(key)
        const live = deriveLiveness(actor, now)
        if (live === "stalled") {
          if (notified.has(key)) continue // already warned this episode — debounce
          notified.add(key)
          yield* notifyStalled(actor, now - actor.lastTurnTime)
          continue
        }
        // Not stalled (progressing/terminal) → re-arm so a future re-stall notifies.
        notified.delete(key)
      }
      // Drop debounce keys for actors that fell out of listActive entirely
      // (went terminal / row gone) so the set can't grow unbounded and a
      // recycled id re-arms cleanly.
      for (const key of notified) if (!seen.has(key)) notified.delete(key)
    }).pipe(Effect.catchCause((cause) => Effect.logError(`stall watchdog scan failed: ${cause}`)))

    // Fork the watchdog into the instance (layer) scope. CRITICAL (T41 lesson):
    // once the fiber detaches, Instance.current ALS context is lost, so
    // actorReg.listActive / inbox.send → Database.use → Client() →
    // InstanceState.bind would throw NotFound(instance). We capture the instance
    // context that is ALS-bound HERE at layer-build time and re-provide it via
    // InstanceRef, whose fallback path InstanceState.bind reads off the fiber
    // context. `undefined` (no instance at build, e.g. some test harnesses) is a
    // safe no-op — Database.use then takes its own NotFound fallback.
    const watchdogInstance = yield* Effect.sync(() => {
      try {
        return Instance.current
      } catch {
        return undefined
      }
    })
    yield* scanStalled.pipe(
      Effect.repeat(Schedule.spaced(WATCHDOG_SCAN_INTERVAL_MS)),
      Effect.provideService(InstanceRef, watchdogInstance),
      Effect.ignore,
      Effect.forkIn(scope),
    )

    const hasOuterExecution = (sessionID: SessionID, actorID: string, executionRevision: number) =>
      Effect.sync(() => outerFibers.get(cancelKey(sessionID, actorID))?.revision === executionRevision)
    const impl = Service.of({ spawn, resumeInput, cancel, getForkContext, hasOuterExecution, scanStalledOnce: () => scanStalled })
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
)

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
