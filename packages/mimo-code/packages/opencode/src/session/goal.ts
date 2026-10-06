import { Effect, Layer, Context, Option } from "effect"
import { generateObject, streamObject, type ModelMessage } from "ai"
import z from "zod"
import { createHash } from "node:crypto"
import { readFileSync, realpathSync, statSync } from "node:fs"
import path from "node:path"
import * as OtelTracer from "@effect/opentelemetry/Tracer"
import { EffectLogger, InstanceState } from "@/effect"
import { Provider, ProviderTransform } from "@/provider"
import type { ProviderID, ModelID } from "@/provider/schema"
import { Auth } from "@/auth"
import { Config } from "@/config"
import { Bus } from "@/bus"
import { BusEvent } from "@/bus/bus-event"
import { SessionID } from "./schema"
import { MessageV2 } from "./message-v2"
import * as GoalState from "./goal-state"
import * as GoalStore from "./goal-store"
import { BashTool } from "@/tool/bash"
import { securityRedact } from "@/util/security-redact"

/**
 * Per-session stop-condition goal. `/goal`: once a goal
 * is set, the main runLoop refuses to stop until an independent judge model
 * decides the condition is satisfied (or genuinely impossible). The judge is a
 * separate model call that only reads the transcript — it does not do the work,
 * so its verdict stays cold relative to the working agent's optimism.
 *
 * State and its append-only journal live in the tenant-partitioned SQLite
 * database, keyed by explicit owner/workspace/project/session scope.
 */

export type Goal = GoalState.Record & {
  condition: string
}

export type SetResult = { goal: Goal; queued: boolean }
export const Target = z.object({
  goalID: z.string().min(1),
  expectedRevision: z.number().int().positive(),
})
export type GoalTarget = z.infer<typeof Target>
export type VerificationHandle = GoalTarget & {
  leaseID: string
  owner: string
  expiresAt: number
}
export type ContinuationHandle = GoalTarget & {
  continuationID: string
  owner: string
  intentID: string
  expiresAt: number
}
export type EvidenceInput =
  | { kind: "command"; subject: string; sourceRef: string }
  | { kind: "file"; subject: string; sourceRef: string }
  | { kind: "user"; subject: string; observation: string }

export function target(goal: Pick<GoalState.Record, "id" | "revision">): GoalTarget {
  return { goalID: goal.id, expectedRevision: goal.revision }
}

export type RejectionResult = {
  goal: Goal
  continuation?: ContinuationHandle
}

export type CompletionResult =
  | { kind: "completed"; next?: Goal; evidenceRef: string }
  | {
      kind: "evidence_missing"
      goal: Goal
      continuation?: ContinuationHandle
      evidenceRef: string
      missing: GoalState.EvidenceKind[]
    }

function view(record: GoalState.Record): Goal {
  return { ...record, condition: record.objective }
}

function eventGoal(record: GoalState.Record | undefined) {
  if (!record) return undefined
  return {
    id: record.id,
    condition: record.objective,
    revision: record.revision,
    status: record.status,
    react: record.react,
  }
}

const VerdictFields = z
  .object({
    ok: z.boolean(),
    impossible: z.boolean().optional(),
    reason: z.string().trim().min(1),
  })
  .strict()

export const Verdict = VerdictFields.superRefine((value, ctx) => {
  if (value.ok && value.impossible !== undefined) {
    ctx.addIssue({
      code: "custom",
      path: ["impossible"],
      message: "A successful verdict cannot also be impossible",
    })
  }
  if (!value.ok && value.impossible === false) {
    ctx.addIssue({
      code: "custom",
      path: ["impossible"],
      message: "Omit impossible unless the goal is genuinely impossible",
    })
  }
})
export type Verdict = z.infer<typeof Verdict>

export type GoalVerificationFailure = "judge_timeout" | "judge_provider_failure" | "judge_malformed_verdict"

const MAX_VERDICT_DISPLAY = 320

/**
 * Verifier prose is untrusted model output. It may be shown to a human, but it
 * must never be replayed verbatim as an instruction to the working model.
 */
export function safeVerdictDisplay(value: unknown) {
  const text = typeof value === "string" ? value : "Verification did not return a usable explanation."
  return securityRedact(text)
    .replace(/<[^>]*>/g, " ")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_VERDICT_DISPLAY)
}

export function goalJudgeFailureCode(error: unknown): GoalVerificationFailure {
  if (error instanceof z.ZodError) return "judge_malformed_verdict"
  const tag = error && typeof error === "object" && "_tag" in error && typeof error._tag === "string" ? error._tag : ""
  if (tag === "TimeoutException") return "judge_timeout"
  return "judge_provider_failure"
}

/**
 * Broadcast whenever a session's goal changes — set, judged, or cleared. The
 * TUI mirrors this into its sync store to render the active-goal indicator and
 * the latest judge verdict. `goal` undefined means there is no active goal
 * (cleared / satisfied / impossible). Mirrors session/status.ts's Event.Status.
 */
export const Event = {
  Updated: BusEvent.define(
    "session.goal",
    z.object({
      sessionID: SessionID.zod,
      goal: z
        .object({
          id: z.string(),
          condition: z.string(),
          revision: z.number(),
          status: GoalState.Status,
          react: z.number(),
        })
        .optional(),
      queued: z.number().int().nonnegative().optional(),
      lastVerdict: VerdictFields.extend({
        attempt: z.number(),
        /** The assistant message the judge evaluated — anchors the verdict to a turn. */
        messageID: z.string().optional(),
        error: z.boolean().optional(),
        reasonCode: z
          .enum(["not_met", "met", "impossible", "judge_timeout", "judge_provider_failure", "judge_malformed_verdict"])
          .or(z.literal("evidence_policy_not_satisfied"))
          .optional(),
      }).optional(),
    }),
  ),
}

// ---- Judge prompts  ----

const JUDGE_SYSTEM = `You are evaluating a stop-condition hook in Mimo Code. Read the conversation transcript carefully, then judge whether the user-provided condition is satisfied.

Your response must be a JSON object with one of these shapes:
- {"ok": true, "reason": "<quote evidence from the transcript that satisfies the condition>"}
- {"ok": false, "reason": "<quote what is missing or what blocks the condition>"}
- {"ok": false, "impossible": true, "reason": "<explain why the condition can never be satisfied>"}

Always include a "reason" field, quoting specific text from the transcript whenever possible. If the transcript does not contain clear evidence that the condition is satisfied, return {"ok": false, "reason": "insufficient evidence in transcript"}.

Only use {"ok": false, "impossible": true} when the condition is genuinely unachievable in this session — for example: the condition is self-contradictory, it depends on a resource or capability that is unavailable, or the assistant has explicitly tried, exhausted reasonable approaches, and stated it cannot be done. Apply your own judgment when deciding this — the assistant claiming the goal is impossible is evidence, not proof; independently confirm the condition is genuinely unachievable rather than deferring to the assistant's self-assessment. Do not use it just because the goal has not been reached yet or because progress is slow. When in doubt, return {"ok": false} without "impossible".`

// The closing question appended after the full conversation.
const judgeUser = (condition: string) =>
  `Based on the conversation transcript above, has the following stopping condition been satisfied? Answer based on transcript evidence only.

Condition: ${condition}`

export interface Interface {
  readonly set: (
    sessionID: SessionID,
    condition: string,
    options?: {
      budget?: Partial<GoalState.Budget>
      requiredEvidence?: GoalState.EvidenceKind[]
    },
  ) => Effect.Effect<SetResult>
  readonly get: (sessionID: SessionID) => Effect.Effect<Goal | undefined>
  readonly inspect: (sessionID: SessionID) => Effect.Effect<GoalState.Envelope>
  readonly journal: (sessionID: SessionID) => Effect.Effect<GoalState.JournalEvent[]>
  readonly replay: (sessionID: SessionID) => Effect.Effect<GoalState.Envelope | undefined>
  readonly analytics: (sessionID: SessionID) => Effect.Effect<Record<string, number>>
  readonly clear: (sessionID: SessionID, target: GoalTarget) => Effect.Effect<void>
  readonly clearHistory: (sessionID: SessionID, expectedEnvelopeRevision: number) => Effect.Effect<void>
  readonly pause: (sessionID: SessionID, target: GoalTarget) => Effect.Effect<Goal>
  readonly resume: (sessionID: SessionID, target: GoalTarget) => Effect.Effect<Goal>
  readonly edit: (sessionID: SessionID, target: GoalTarget, objective: string) => Effect.Effect<Goal>
  readonly addEvidence: (sessionID: SessionID, target: GoalTarget, evidence: EvidenceInput) => Effect.Effect<Goal>
  readonly beginVerification: (
    sessionID: SessionID,
    target: GoalTarget,
    holder: string,
  ) => Effect.Effect<VerificationHandle | undefined>
  readonly heartbeatVerification: (
    sessionID: SessionID,
    handle: VerificationHandle,
  ) => Effect.Effect<VerificationHandle | undefined>
  readonly verificationDegraded: (
    sessionID: SessionID,
    handle: VerificationHandle,
    reasonCode: GoalVerificationFailure,
  ) => Effect.Effect<Goal>
  readonly verificationRejected: (
    sessionID: SessionID,
    handle: VerificationHandle,
    reason: string,
    reasonCode?: "not_met" | "evidence_policy_not_satisfied",
    continuation?: {
      owner: string
      intentID: string
    },
  ) => Effect.Effect<RejectionResult>
  readonly verificationBlocked: (
    sessionID: SessionID,
    handle: VerificationHandle,
    reason: string,
    evidence: Omit<GoalState.Evidence, "id" | "capturedAt" | "contentHash">,
  ) => Effect.Effect<Goal>
  readonly verificationCompleted: (
    sessionID: SessionID,
    handle: VerificationHandle,
    reason: string,
    evidence: Omit<GoalState.Evidence, "id" | "capturedAt" | "contentHash">,
    continuation?: {
      owner: string
      intentID: string
    },
  ) => Effect.Effect<CompletionResult>
  readonly claimContinuation: (
    sessionID: SessionID,
    target: GoalTarget,
    holder: string,
  ) => Effect.Effect<ContinuationHandle | undefined>
  /** Increment the re-entry counter, returning the new count. */
  readonly bumpReact: (sessionID: SessionID) => Effect.Effect<number>
  readonly recordUsage: (
    sessionID: SessionID,
    target: GoalTarget,
    messageID: string,
    tokens: number,
    toolCalls: number,
  ) => Effect.Effect<Goal | undefined>
  /**
   * Run the judge over the conversation against the active goal's condition.
   * `msgs` is the main thread's message list; it is converted to native model
   * messages (tool calls/results/images preserved) so the judge independently
   * confirms the work rather than trusting the assistant's self-report.
   */
  readonly evaluate: (input: {
    condition: string
    msgs: MessageV2.WithParts[]
    model: { providerID: ProviderID; modelID: ModelID }
  }) => Effect.Effect<Verdict>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/SessionGoal") {}

function assertTarget(record: GoalState.Record | undefined, expected: GoalTarget) {
  if (!record || record.id !== expected.goalID) {
    throw new Error(`Goal target is stale: expected ${expected.goalID}`)
  }
  if (record.revision !== expected.expectedRevision) {
    throw new GoalState.ConflictError(expected.expectedRevision, record.revision)
  }
  return record
}

function assertVerification(record: GoalState.Record | undefined, expected: VerificationHandle) {
  const active = assertTarget(record, expected)
  if (
    active.lease?.id !== expected.leaseID ||
    active.lease.owner !== expected.owner ||
    active.lease.expiresAt <= Date.now()
  ) {
    throw new Error("Goal verification lease is stale")
  }
  return active
}

function verificationHandle(record: GoalState.Record): VerificationHandle {
  if (!record.lease) throw new Error("Goal has no verification lease")
  return {
    goalID: record.id,
    expectedRevision: record.revision,
    leaseID: record.lease.id,
    owner: record.lease.owner,
    expiresAt: record.lease.expiresAt,
  }
}

function continuationHandle(record: GoalState.Record): ContinuationHandle | undefined {
  if (!record.continuation) return undefined
  return {
    goalID: record.id,
    expectedRevision: record.revision,
    continuationID: record.continuation.id,
    owner: record.continuation.owner,
    intentID: record.continuation.intentID,
    expiresAt: record.continuation.expiresAt,
  }
}

function budgetEvent(cause: GoalState.BudgetCause) {
  return cause.endsWith("_expired") ? "goal_expired" : "budget_exhausted"
}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const provider = yield* Provider.Service
    const auth = yield* Auth.Service
    const config = yield* Config.Service
    const bus = yield* Bus.Service
    const elog = EffectLogger.create({ service: "SessionGoal" })

    const findToolResult = (sessionID: SessionID, sourceRef: string) => {
      const callID = sourceRef.replace(/^tool:/, "")
      for (const message of MessageV2.stream(sessionID, { agentID: "*" })) {
        for (const part of message.parts) {
          if (part.type !== "tool" || part.callID !== callID) continue
          if (part.tool !== BashTool.id) {
            throw new Error(`Command evidence ${callID} did not come from an approved command/test tool`)
          }
          if (part.state.status === "error") {
            const detail = part.state.metadata?.interrupted === true ? "cancelled" : "failed"
            throw new Error(`Command evidence ${callID} ${detail}`)
          }
          if (part.state.status !== "completed") {
            throw new Error(`Command evidence ${callID} is not complete (${part.state.status})`)
          }
          if (part.state.metadata.containment === "interactive-client") {
            throw new Error(`Command evidence ${callID} was reported by an interactive client, not executed by the server`)
          }
          const exit = part.state.metadata.exit
          if (exit === null) throw new Error(`Command evidence ${callID} timed out`)
          if (!Number.isInteger(exit)) {
            throw new Error(`Command evidence ${callID} has no server-owned exit code`)
          }
          if (exit !== 0) throw new Error(`Command evidence ${callID} failed with exit code ${exit}`)
          return { part, output: part.state.output }
        }
      }
      throw new Error(`Command evidence source was not found: ${callID}`)
    }

    const verifyEvidenceInput = Effect.fn("SessionGoal.verifyEvidenceInput")(function* (
      sessionID: SessionID,
      input: EvidenceInput,
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      if (input.kind === "user") {
        return GoalState.makeEvidence({
          kind: "user",
          subject: input.subject,
          sourceRef: `user:${scoped.owner}`,
          observation: input.observation,
          producer: scoped.owner,
          verifier: `user:${scoped.owner}`,
        })
      }
      if (input.kind === "command") {
        const result = yield* Effect.sync(() => findToolResult(sessionID, input.sourceRef))
        if (result.part.state.status !== "completed") {
          throw new Error(`Command evidence ${result.part.callID} is not complete`)
        }
        const capturedAt = result.part.state.time.end
        if (!Number.isInteger(capturedAt) || !Number.isFinite(capturedAt) || capturedAt <= 0) {
          throw new Error(`Command evidence ${result.part.callID} has no server-owned completion time`)
        }
        return GoalState.makeEvidence({
          kind: "command",
          subject: input.subject,
          sourceRef: `tool:${result.part.callID}`,
          observation: `${result.part.tool} exited 0: ${result.output}`,
          producer: `tool:${result.part.tool}`,
          verifier: `session-tool:${result.part.tool}`,
          capturedAt,
        })
      }
      if (input.kind !== "file") throw new Error("Unsupported goal evidence kind")

      const context = yield* InstanceState.context
      return yield* Effect.sync(() => {
        const root = realpathSync(context.worktree)
        const candidate = path.resolve(root, input.sourceRef)
        const canonical = realpathSync(candidate)
        const relative = path.relative(root, canonical)
        if (relative.startsWith("..") || path.isAbsolute(relative)) {
          throw new Error("File evidence must stay inside the active workspace")
        }
        const stat = statSync(canonical)
        if (!stat.isFile()) throw new Error("File evidence must reference a regular file")
        const digest = createHash("sha256").update(readFileSync(canonical)).digest("hex")
        return GoalState.makeEvidence({
          kind: "file",
          subject: input.subject,
          sourceRef: canonical,
          observation: `file present; sha256=${digest}; bytes=${stat.size}`,
          producer: "open-clank:file-verifier",
          verifier: `file-sha256:${digest}`,
        })
      })
    })

    const evidenceStillValid = Effect.fn("SessionGoal.evidenceStillValid")(function* (
      sessionID: SessionID,
      item: GoalState.Evidence,
    ) {
      if (!GoalState.evidenceVerifierShapeValid(item)) return false
      if (item.kind === "model") return true
      if (item.kind === "user") {
        const scoped = yield* GoalStore.scope(sessionID)
        return (
          item.sourceRef === `user:${scoped.owner}` &&
          item.producer === scoped.owner &&
          item.verifier === `user:${scoped.owner}`
        )
      }
      if (item.kind === "command") {
        return yield* Effect.sync(() => {
          try {
            const { part } = findToolResult(sessionID, item.sourceRef)
            return item.producer === `tool:${part.tool}` && item.verifier === `session-tool:${part.tool}`
          } catch {
            return false
          }
        })
      }
      return yield* Effect.sync(() => {
        try {
          const digest = createHash("sha256").update(readFileSync(item.sourceRef)).digest("hex")
          return item.verifier === `file-sha256:${digest}`
        } catch {
          return false
        }
      })
    })

    const validEvidenceIDs = Effect.fn("SessionGoal.validEvidenceIDs")(function* (
      sessionID: SessionID,
      record: GoalState.Record,
    ) {
      const valid = new Set<string>()
      for (const item of record.evidence) {
        if (yield* evidenceStillValid(sessionID, item)) valid.add(item.id)
      }
      return valid
    })

    const set = Effect.fn("SessionGoal.set")(function* (
      sessionID: SessionID,
      condition: string,
      options?: {
        budget?: Partial<GoalState.Budget>
        requiredEvidence?: GoalState.EvidenceKind[]
      },
    ) {
      const objective = condition.trim()
      if (!objective) throw new Error("Goal objective cannot be empty")
      const scoped = yield* GoalStore.scope(sessionID)
      const result = yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          const queued = Boolean(current.active)
          const record = GoalState.create({
            objective,
            owner: scoped.owner,
            workspace: scoped.workspace,
            project: scoped.project,
            sessionID,
            status: queued ? "queued" : "active",
            budget: options?.budget,
            requiredEvidence: options?.requiredEvidence,
          })
          return {
            envelope: {
              ...current,
              active: current.active ?? record,
              queue: queued ? [...current.queue, record] : current.queue,
            },
            events: [GoalState.journal(record, queued ? "queued" : "created", "user")],
            value: { goal: view(record), queued },
          }
        }),
      )
      yield* elog.info("goal set", { sessionID })
      const envelope = yield* Effect.sync(() => GoalStore.read(scoped))
      yield* bus.publish(Event.Updated, {
        sessionID,
        goal: eventGoal(envelope.active),
        queued: envelope.queue.length,
      })
      return result
    })

    const get = Effect.fn("SessionGoal.get")(function* (sessionID: SessionID) {
      const scoped = yield* GoalStore.scope(sessionID)
      const now = Date.now()
      const active = yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          const record = current.active
          if (!record) return { envelope: current, changed: false, value: record }
          const events: GoalState.JournalEvent[] = []
          let next = record
          const preflight = GoalState.preflightBudget(next, now)
          if (preflight.changed && preflight.cause) {
            next = {
              ...preflight.record,
              lease: undefined,
              continuation: undefined,
            }
            events.push(
              GoalState.journal(next, budgetEvent(preflight.cause), "system", now, {
                reasonCode: preflight.cause,
              }),
            )
          } else if (
            next.lease &&
            next.lease.expiresAt <= now &&
            ["active", "awaiting_verification", "verification_degraded"].includes(next.status)
          ) {
            next = GoalState.releaseLease(next, next.lease.id, now)
            if (next.status !== "active") next = GoalState.transition(next, "active", now)
            events.push(
              GoalState.journal(next, "lease_recovered", "system", now, {
                reasonCode: "expired_lease",
              }),
            )
          }
          if (events.length === 0) return { envelope: current, changed: false, value: record }
          return {
            envelope: { ...current, active: next },
            events,
            value: next,
          }
        }),
      )
      return active ? view(active) : undefined
    })

    const inspect = Effect.fn("SessionGoal.inspect")(function* (sessionID: SessionID) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() => GoalStore.read(scoped))
    })

    const journal = Effect.fn("SessionGoal.journal")(function* (sessionID: SessionID) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() => GoalStore.journal(scoped))
    })

    const replay = Effect.fn("SessionGoal.replay")(function* (sessionID: SessionID) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() => GoalStore.replay(scoped))
    })

    const analytics = Effect.fn("SessionGoal.analytics")(function* (sessionID: SessionID) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() => GoalStore.analytics(scoped))
    })

    const clear = Effect.fn("SessionGoal.clear")(function* (sessionID: SessionID, expected: GoalTarget) {
      const scoped = yield* GoalStore.scope(sessionID)
      const next = yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          const record = assertTarget(current.active, expected)
          const now = Date.now()
          const cancelled = GoalState.transition(record, "cancelled", now)
          const queued = [...current.queue]
          const promoted = queued.shift()
          const active = promoted ? GoalState.transition(promoted, "active", now) : undefined
          const events = [
            GoalState.journal(cancelled, "cancelled", "user", now, { reasonCode: "user_cancelled" }),
            ...(active ? [GoalState.journal(active, "activated", "system", now)] : []),
          ]
          return {
            envelope: {
              ...current,
              active,
              queue: queued,
              history: [...current.history, cancelled],
            },
            events,
            value: active,
          }
        }),
      )
      yield* elog.info("goal cleared", { sessionID })
      const envelope = yield* Effect.sync(() => GoalStore.read(scoped))
      yield* bus.publish(Event.Updated, {
        sessionID,
        goal: eventGoal(next),
        queued: envelope.queue.length,
      })
    })

    const clearHistory = Effect.fn("SessionGoal.clearHistory")(function* (
      sessionID: SessionID,
      expectedEnvelopeRevision: number,
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          if (current.revision !== expectedEnvelopeRevision) {
            throw new GoalState.ConflictError(expectedEnvelopeRevision, current.revision)
          }
          if (current.history.length === 0) {
            return { envelope: current, changed: false, value: undefined }
          }
          const now = Date.now()
          return {
            envelope: { ...current, history: [] },
            events: current.history.map((record) =>
              GoalState.journal(record, "history_cleared", "user", now, {
                reasonCode: "user_cleared_history",
              }),
            ),
            value: undefined,
          }
        }),
      )
      const envelope = yield* Effect.sync(() => GoalStore.read(scoped))
      yield* bus.publish(Event.Updated, {
        sessionID,
        goal: eventGoal(envelope.active),
        queued: envelope.queue.length,
      })
    })

    const mutateActive = <A>(
      sessionID: SessionID,
      expected: GoalTarget,
      fn: (
        record: GoalState.Record,
        current: GoalState.Envelope,
      ) => {
        record: GoalState.Record
        value: A
        event: GoalState.JournalEvent
      },
    ) =>
      Effect.gen(function* () {
        const scoped = yield* GoalStore.scope(sessionID)
        return yield* Effect.sync(() =>
          GoalStore.mutate(scoped, (current) => {
            const record = assertTarget(current.active, expected)
            const result = fn(record, current)
            return {
              envelope: { ...current, active: result.record },
              events: [result.event],
              value: result.value,
            }
          }),
        )
      })

    const pause = Effect.fn("SessionGoal.pause")(function* (sessionID: SessionID, expected: GoalTarget) {
      const result = yield* mutateActive(sessionID, expected, (record) => {
        if (record.status === "paused") {
          throw new Error("Goal is already paused")
        }
        const next = GoalState.transition(record, "paused")
        return { record: next, value: view(next), event: GoalState.journal(next, "paused", "user") }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const resume = Effect.fn("SessionGoal.resume")(function* (sessionID: SessionID, expected: GoalTarget) {
      const result = yield* mutateActive(sessionID, expected, (record) => {
        if (record.status === "active") throw new Error("Goal is already active")
        const cause = GoalState.budgetCause(record)
        if (cause) throw new Error(`Goal cannot resume: ${cause}`)
        const next = GoalState.transition(record, "active")
        return { record: next, value: view(next), event: GoalState.journal(next, "resumed", "user") }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const edit = Effect.fn("SessionGoal.edit")(function* (
      sessionID: SessionID,
      expected: GoalTarget,
      objective: string,
    ) {
      const nextObjective = objective.trim()
      if (!nextObjective) throw new Error("Goal objective cannot be empty")
      const result = yield* mutateActive(sessionID, expected, (record) => {
        const next = {
          ...record,
          objective: nextObjective,
          evidence: [],
          lease: undefined,
          continuation: undefined,
          lastOutcome: undefined,
          revision: record.revision + 1,
          updatedAt: Date.now(),
        }
        return { record: next, value: view(next), event: GoalState.journal(next, "objective_edited", "user") }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const addEvidence = Effect.fn("SessionGoal.addEvidence")(function* (
      sessionID: SessionID,
      expected: GoalTarget,
      input: EvidenceInput,
    ) {
      const evidence = yield* verifyEvidenceInput(sessionID, input)
      const result = yield* mutateActive(sessionID, expected, (record) => {
        if (evidence.capturedAt < record.createdAt) {
          throw new Error("Goal evidence predates the active goal")
        }
        const next = {
          ...record,
          evidence: [...record.evidence, evidence],
          revision: record.revision + 1,
          updatedAt: Date.now(),
        }
        return {
          record: next,
          value: view(next),
          event: GoalState.journal(next, "evidence_added", "worker", undefined, {
            evidenceRefs: [evidence.id],
          }),
        }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const beginVerification = Effect.fn("SessionGoal.beginVerification")(function* (
      sessionID: SessionID,
      expected: GoalTarget,
      holder: string,
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          let record = assertTarget(current.active, expected)
          const preflight = GoalState.preflightBudget(record)
          if (preflight.changed && preflight.cause) {
            record = { ...preflight.record, lease: undefined, continuation: undefined }
            return {
              envelope: { ...current, active: record },
              events: [
                GoalState.journal(record, budgetEvent(preflight.cause), "system", undefined, {
                  reasonCode: preflight.cause,
                }),
              ],
              value: undefined,
            }
          }
          if (
            record.continuation &&
            record.continuation.expiresAt > Date.now() &&
            record.continuation.owner !== holder
          ) {
            return { envelope: current, changed: false, value: undefined }
          }
          if (record.continuation) {
            record = GoalState.releaseContinuation(record, record.continuation.id)
          }
          const leased = GoalState.acquireLease(record, holder, Date.now(), 150_000)
          if (!leased.acquired || !leased.record.lease) {
            return { envelope: current, changed: false, value: undefined }
          }
          const next =
            leased.record.status === "awaiting_verification"
              ? leased.record
              : GoalState.transition(leased.record, "awaiting_verification")
          return {
            envelope: { ...current, active: next },
            events: [GoalState.journal(next, "verification_started", "verifier")],
            value: verificationHandle(next),
          }
        }),
      )
    })

    const heartbeatVerification = Effect.fn("SessionGoal.heartbeatVerification")(function* (
      sessionID: SessionID,
      expected: VerificationHandle,
    ) {
      return yield* mutateActive(sessionID, expected, (candidate) => {
        const record = assertVerification(candidate, expected)
        const heartbeat = GoalState.heartbeatLease(record, expected.leaseID, expected.owner, Date.now(), 150_000)
        if (!heartbeat.renewed) throw new Error("Goal verification lease is stale")
        return {
          record: heartbeat.record,
          value: verificationHandle(heartbeat.record),
          event: GoalState.journal(heartbeat.record, "lease_heartbeat", "worker"),
        }
      })
    })

    const verificationDegraded = Effect.fn("SessionGoal.verificationDegraded")(function* (
      sessionID: SessionID,
      expected: VerificationHandle,
      reasonCode: GoalVerificationFailure,
    ) {
      const result = yield* mutateActive(sessionID, expected, (candidate) => {
        const record = assertVerification(candidate, expected)
        const released = GoalState.releaseLease(record, expected.leaseID)
        const next = GoalState.transition(released, "verification_degraded")
        next.lastOutcome = { code: reasonCode, verifiedAt: Date.now(), evidenceRefs: [] }
        return {
          record: next,
          value: view(next),
          event: GoalState.journal(next, "verification_degraded", "verifier", undefined, { reasonCode }),
        }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const verificationRejected = Effect.fn("SessionGoal.verificationRejected")(function* (
      sessionID: SessionID,
      expected: VerificationHandle,
      reason: string,
      reasonCode: "not_met" | "evidence_policy_not_satisfied" = "not_met",
      continuation?: { owner: string; intentID: string },
    ) {
      const result = yield* mutateActive(sessionID, expected, (candidate) => {
        const record = assertVerification(candidate, expected)
        const released = GoalState.releaseLease(record, expected.leaseID)
        const active = GoalState.transition(released, "active")
        const consumed = GoalState.consumeBudget({ ...active, react: active.react + 1 }, { turns: 1 })
        let next: GoalState.Record = consumed.record
        next.lastOutcome = {
          code: reasonCode,
          reason: safeVerdictDisplay(reason),
          verifiedAt: Date.now(),
          evidenceRefs: [],
        }
        if (!consumed.exhausted && continuation) {
          const leased = GoalState.acquireContinuation(next, {
            ...continuation,
            reasonCode,
            display: safeVerdictDisplay(reason),
          })
          if (!leased.acquired) throw new Error("Goal continuation lease is contended")
          next = leased.record
        }
        return {
          record: next,
          value: {
            goal: view(next),
            continuation: continuationHandle(next),
          },
          event: GoalState.journal(
            next,
            consumed.cause ? budgetEvent(consumed.cause) : "verification_rejected",
            "verifier",
            undefined,
            {
              reasonCode: consumed.cause ?? reasonCode,
              budgetDelta: { turns: 1 },
            },
          ),
        }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result.goal) })
      return result
    })

    const verificationBlocked = Effect.fn("SessionGoal.verificationBlocked")(function* (
      sessionID: SessionID,
      expected: VerificationHandle,
      reason: string,
      evidenceInput: Omit<GoalState.Evidence, "id" | "capturedAt" | "contentHash">,
    ) {
      const result = yield* mutateActive(sessionID, expected, (candidate) => {
        const record = assertVerification(candidate, expected)
        const evidence = GoalState.makeEvidence(evidenceInput)
        const withEvidence = {
          ...record,
          evidence: [...record.evidence, evidence],
          revision: record.revision + 1,
          updatedAt: Date.now(),
        }
        const released = GoalState.releaseLease(withEvidence, expected.leaseID)
        const next = GoalState.transition(released, "blocked")
        next.lastOutcome = {
          code: "impossible",
          reason: safeVerdictDisplay(reason),
          verifiedAt: Date.now(),
          evidenceRefs: [evidence.id],
        }
        return {
          record: next,
          value: view(next),
          event: GoalState.journal(next, "blocked", "verifier", undefined, {
            reasonCode: "impossible",
            evidenceRefs: [evidence.id],
          }),
        }
      })
      yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(result) })
      return result
    })

    const verificationCompleted = Effect.fn("SessionGoal.verificationCompleted")(function* (
      sessionID: SessionID,
      expected: VerificationHandle,
      reason: string,
      evidenceInput: Omit<GoalState.Evidence, "id" | "capturedAt" | "contentHash">,
      continuation?: { owner: string; intentID: string },
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      const before = yield* Effect.sync(() => assertVerification(GoalStore.read(scoped).active, expected))
      const validIDs = yield* validEvidenceIDs(sessionID, before)
      const result = yield* Effect.sync(() =>
        GoalStore.mutate<CompletionResult>(scoped, (current) => {
          const record = assertVerification(current.active, expected)
          const now = Date.now()
          const evidence = GoalState.makeEvidence(evidenceInput)
          const withEvidence = {
            ...record,
            evidence: [...record.evidence, evidence],
            revision: record.revision + 1,
            updatedAt: now,
          }
          const eligible = withEvidence.evidence.filter((item) => item.id === evidence.id || validIDs.has(item.id))
          const trustedEvidenceIDs = new Set([...validIDs, evidence.id])
          const missing = GoalState.missingEvidenceKinds(
            { ...withEvidence, evidence: eligible },
            trustedEvidenceIDs,
            now,
          )
          if (missing.length > 0) {
            const released = GoalState.releaseLease(withEvidence, expected.leaseID, now)
            const active = GoalState.transition(released, "active", now)
            const consumed = GoalState.consumeBudget({ ...active, react: active.react + 1 }, { turns: 1 }, now)
            let next: GoalState.Record = consumed.record
            const display = `Goal still needs ${missing.join(", ")} evidence before it can complete.`
            next.lastOutcome = {
              code: "evidence_policy_not_satisfied",
              reason: display,
              verifiedAt: now,
              evidenceRefs: [evidence.id],
            }
            if (!consumed.exhausted && continuation) {
              const leased = GoalState.acquireContinuation(
                next,
                {
                  ...continuation,
                  reasonCode: "evidence_policy_not_satisfied",
                  display,
                },
                now,
              )
              if (!leased.acquired) throw new Error("Goal continuation lease is contended")
              next = leased.record
            }
            return {
              envelope: { ...current, active: next },
              events: [
                GoalState.journal(
                  next,
                  consumed.cause ? budgetEvent(consumed.cause) : "verification_rejected",
                  "verifier",
                  now,
                  {
                    reasonCode: consumed.cause ?? "evidence_policy_not_satisfied",
                    budgetDelta: { turns: 1 },
                    evidenceRefs: [evidence.id],
                  },
                ),
              ],
              value: {
                kind: "evidence_missing" as const,
                goal: view(next),
                continuation: continuationHandle(next),
                evidenceRef: evidence.id,
                missing,
              },
            }
          }
          const released = GoalState.releaseLease(withEvidence, expected.leaseID, now)
          const completed = GoalState.transition(released, "completed", now)
          completed.lastOutcome = {
            code: "met",
            reason: safeVerdictDisplay(reason),
            verifiedAt: now,
            evidenceRefs: [evidence.id],
          }
          const queue = [...current.queue]
          const promoted = queue.shift()
          const next = promoted ? GoalState.transition(promoted, "active", now) : undefined
          return {
            envelope: {
              ...current,
              active: next,
              queue,
              history: [...current.history, completed],
            },
            events: [
              GoalState.journal(completed, "completed", "verifier", now, {
                reasonCode: "met",
                // Capture only references that this completion revalidated.
                // The journal remains the durable receipt/replay authority.
                evidenceRefs: [evidence.id, ...eligible.filter((item) =>
                  validIDs.has(item.id) && (item.kind === "file" || item.kind === "command"),
                ).map((item) => item.id)],
              }),
              ...(next ? [GoalState.journal(next, "activated", "system", now)] : []),
            ],
            value: {
              kind: "completed" as const,
              next: next ? view(next) : undefined,
              evidenceRef: evidence.id,
            },
          }
        }),
      )
      const envelope = yield* Effect.sync(() => GoalStore.read(scoped))
      yield* bus.publish(Event.Updated, {
        sessionID,
        goal: eventGoal(result.kind === "completed" ? result.next : result.goal),
        queued: envelope.queue.length,
      })
      return result
    })

    const claimContinuation = Effect.fn("SessionGoal.claimContinuation")(function* (
      sessionID: SessionID,
      expected: GoalTarget,
      holder: string,
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      return yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (current) => {
          const record = assertTarget(current.active, expected)
          const claimed = GoalState.claimContinuation(record, holder)
          if (!claimed.claimed) return { envelope: current, changed: false, value: undefined }
          if (!claimed.changed) {
            return { envelope: current, changed: false, value: continuationHandle(record) }
          }
          return {
            envelope: { ...current, active: claimed.record },
            events: [
              GoalState.journal(claimed.record, "continuation_recovered", "worker", undefined, {
                reasonCode: "expired_continuation",
              }),
            ],
            value: continuationHandle(claimed.record),
          }
        }),
      )
    })

    const bumpReact = Effect.fn("SessionGoal.bumpReact")(function* (sessionID: SessionID) {
      const current = yield* get(sessionID)
      if (!current) return 0
      return yield* mutateActive(sessionID, target(current), (record) => {
        const consumed = GoalState.consumeBudget({ ...record, react: record.react + 1 }, { turns: 1 })
        return {
          record: consumed.record,
          value: consumed.record.react,
          event: GoalState.journal(
            consumed.record,
            consumed.cause ? budgetEvent(consumed.cause) : "turn_consumed",
            "worker",
            undefined,
            {
              reasonCode: consumed.cause,
              budgetDelta: { turns: 1 },
            },
          ),
        }
      })
    })

    const recordUsage = Effect.fn("SessionGoal.recordUsage")(function* (
      sessionID: SessionID,
      expected: GoalTarget,
      messageID: string,
      tokens: number,
      toolCalls: number,
    ) {
      const scoped = yield* GoalStore.scope(sessionID)
      const record = yield* Effect.sync(() =>
        GoalStore.mutate(scoped, (envelope) => {
          const active = envelope.active
          if (!active || active.id !== expected.goalID) {
            throw new Error(`Goal target is stale: expected ${expected.goalID}`)
          }
          if (active.accountedMessageIDs.includes(messageID)) {
            return { envelope, changed: false, value: active }
          }
          assertTarget(active, expected)
          if (!["active", "awaiting_verification", "verification_degraded"].includes(active.status)) {
            return { envelope, changed: false, value: active }
          }
          const used = GoalState.consumeBudget(
            {
              ...active,
              accountedMessageIDs: [...active.accountedMessageIDs, messageID].slice(-1024),
            },
            {
              tokens: Math.max(0, Math.trunc(tokens)),
              toolCalls: Math.max(0, Math.trunc(toolCalls)),
            },
          )
          return {
            envelope: { ...envelope, active: used.record },
            events: [
              GoalState.journal(
                used.record,
                used.cause ? budgetEvent(used.cause) : "usage_consumed",
                "worker",
                undefined,
                {
                  reasonCode: used.cause ?? "usage_accounted",
                  budgetDelta: {
                    tokens: Math.max(0, Math.trunc(tokens)),
                    toolCalls: Math.max(0, Math.trunc(toolCalls)),
                  },
                },
              ),
            ],
            value: used.record,
          }
        }),
      )
      if (record) yield* bus.publish(Event.Updated, { sessionID, goal: eventGoal(record) })
      return record ? view(record) : undefined
    })

    const evaluate = Effect.fn("SessionGoal.evaluate")(function* (input: {
      condition: string
      msgs: MessageV2.WithParts[]
      model: { providerID: ProviderID; modelID: ModelID }
    }) {
      const cfg = yield* config.get()
      const resolved = yield* provider.getModel(input.model.providerID, input.model.modelID)
      const language = yield* provider.getLanguage(resolved)
      const tracer = cfg.experimental?.openTelemetry
        ? Option.getOrUndefined(yield* Effect.serviceOption(OtelTracer.OtelTracer))
        : undefined

      const authInfo = yield* auth.get(input.model.providerID).pipe(Effect.orDie)
      const isOpenaiOauth = input.model.providerID === "openai" && authInfo?.type === "oauth"

      // Convert the conversation to native model messages so the judge sees the
      // real tool calls/results/images — same context the working agent had.
      const conversation = yield* MessageV2.toModelMessagesEffect(input.msgs, resolved)

      yield* elog.debug("goal judge transcript", {
        messageCount: conversation.length,
      })

      const params = {
        experimental_telemetry: {
          isEnabled: cfg.experimental?.openTelemetry,
          tracer,
          metadata: { userId: cfg.username ?? "unknown" },
        },
        temperature: 0,
        messages: [
          ...(isOpenaiOauth ? [] : [{ role: "system", content: JUDGE_SYSTEM } satisfies ModelMessage]),
          ...conversation,
          {
            role: "user",
            content: judgeUser(input.condition),
          } satisfies ModelMessage,
        ],
        model: language,
        schema: Verdict,
      } satisfies Parameters<typeof generateObject>[0]

      if (isOpenaiOauth) {
        return yield* Effect.promise(async () => {
          const result = streamObject({
            ...params,
            providerOptions: ProviderTransform.providerOptions(resolved, {
              instructions: JUDGE_SYSTEM,
              store: false,
            }),
            onError: () => {},
          })
          for await (const part of result.fullStream) {
            if (part.type === "error") throw part.error
          }
          return Verdict.parse(await result.object)
        })
      }

      return yield* Effect.promise(() => generateObject(params).then((r) => Verdict.parse(r.object)))
    })

    return Service.of({
      set,
      get,
      inspect,
      journal,
      replay,
      analytics,
      clear,
      clearHistory,
      pause,
      resume,
      edit,
      addEvidence,
      beginVerification,
      heartbeatVerification,
      verificationDegraded,
      verificationRejected,
      verificationBlocked,
      verificationCompleted,
      claimContinuation,
      bumpReact,
      recordUsage,
      evaluate,
    })
  }),
)

export const defaultLayer = layer.pipe(
  Layer.provide(Provider.defaultLayer),
  Layer.provide(Auth.defaultLayer),
  Layer.provide(Config.defaultLayer),
  Layer.provide(Bus.layer),
)

export * as Goal from "./goal"
