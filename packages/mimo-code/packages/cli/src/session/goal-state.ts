import { createHash, randomUUID } from "node:crypto"
import z from "zod"
import { securityRedact } from "@/util/security-redact"

export const Status = z.enum([
  "queued",
  "active",
  "awaiting_verification",
  "paused",
  "blocked",
  "verification_degraded",
  "completed",
  "cancelled",
  "expired",
])
export type Status = z.infer<typeof Status>

export const Budget = z.object({
  maxTurns: z.number().int().nonnegative().optional(),
  maxTokens: z.number().int().nonnegative().optional(),
  maxWallMs: z.number().int().nonnegative().optional(),
  maxToolCalls: z.number().int().nonnegative().optional(),
  deadline: z.number().int().positive().optional(),
  usedTurns: z.number().int().nonnegative().default(0),
  usedTokens: z.number().int().nonnegative().default(0),
  usedToolCalls: z.number().int().nonnegative().default(0),
})
export type Budget = z.infer<typeof Budget>

export const EvidenceKind = z.enum(["model", "command", "file", "user"])
export type EvidenceKind = z.infer<typeof EvidenceKind>

export const Evidence = z.object({
  id: z.string(),
  kind: EvidenceKind,
  subject: z.string(),
  sourceRef: z.string(),
  observation: z.string(),
  capturedAt: z.number().int().positive(),
  producer: z.string(),
  contentHash: z.string(),
  verifier: z.string().optional(),
})
export type Evidence = z.infer<typeof Evidence>

export const Lease = z.object({
  id: z.string(),
  owner: z.string(),
  acquiredAt: z.number().int().positive(),
  expiresAt: z.number().int().positive(),
  attempt: z.number().int().positive(),
})
export type Lease = z.infer<typeof Lease>

export const Continuation = z.object({
  id: z.string(),
  owner: z.string(),
  intentID: z.string(),
  reasonCode: z.string(),
  display: z.string(),
  acquiredAt: z.number().int().positive(),
  expiresAt: z.number().int().positive(),
  attempt: z.number().int().positive(),
})
export type Continuation = z.infer<typeof Continuation>

export const Outcome = z.object({
  code: z.string(),
  reason: z.string().optional(),
  verifiedAt: z.number().int().positive(),
  evidenceRefs: z.array(z.string()).default([]),
})
export type Outcome = z.infer<typeof Outcome>

export const Record = z.object({
  id: z.string(),
  revision: z.number().int().positive(),
  objective: z.string().min(1),
  status: Status,
  owner: z.string().min(1),
  workspace: z.string().min(1),
  project: z.string().min(1),
  sessionID: z.string().min(1),
  createdAt: z.number().int().positive(),
  updatedAt: z.number().int().positive(),
  completedAt: z.number().int().positive().optional(),
  react: z.number().int().nonnegative(),
  budget: Budget,
  lease: Lease.optional(),
  continuation: Continuation.optional(),
  requiredEvidence: z.array(EvidenceKind).default([]),
  evidence: z.array(Evidence).default([]),
  accountedMessageIDs: z.array(z.string()).default([]),
  lastOutcome: Outcome.optional(),
})
export type Record = z.infer<typeof Record>

export const Envelope = z.object({
  revision: z.number().int().nonnegative(),
  active: Record.optional(),
  queue: z.array(Record).default([]),
  history: z.array(Record).default([]),
})
export type Envelope = z.infer<typeof Envelope>

function formatLimit(used: number | undefined, maximum: number | undefined) {
  return `${used ?? 0}/${maximum ?? "no limit"}`
}

export function recordSummaryLines(record: Record, label: string) {
  const budget = record.budget
  const budgetParts = [
    `turns ${formatLimit(budget.usedTurns, budget.maxTurns)}`,
    `tokens ${formatLimit(budget.usedTokens, budget.maxTokens)}`,
    `tool calls ${formatLimit(budget.usedToolCalls, budget.maxToolCalls)}`,
  ]
  if (budget.maxWallMs !== undefined) budgetParts.push(`wall ${budget.maxWallMs}ms`)
  if (budget.deadline !== undefined) budgetParts.push(`deadline ${budget.deadline}`)

  const required = record.requiredEvidence.length ? record.requiredEvidence.join(", ") : "none"
  const lines = [
    `${label}: ${record.objective} [${record.status}, revision ${record.revision}]`,
    `  Budget: ${budgetParts.join(" · ")}`,
    `  Evidence: required ${required} · attached ${record.evidence.length}`,
    ...record.evidence.map((item) => `  Evidence ${item.kind}: ${item.subject || item.sourceRef}`),
  ]
  if (record.lastOutcome) {
    lines.push(
      `  Outcome: ${record.lastOutcome.code}${record.lastOutcome.reason ? ` — ${record.lastOutcome.reason}` : ""}`,
    )
  }
  return lines
}

export function summaryLines(envelope: Envelope, analytics: globalThis.Record<string, number>) {
  const outcomes = Object.entries(analytics).sort(([left], [right]) => left.localeCompare(right))
  return [
    `State revision: ${envelope.revision}`,
    ...(envelope.active ? recordSummaryLines(envelope.active, "Active") : ["Active: none"]),
    ...(envelope.queue.length
      ? envelope.queue.flatMap((record, index) => recordSummaryLines(record, `Queued ${index + 1}`))
      : ["Queue: none"]),
    `History: ${envelope.history.length}`,
    ...envelope.history.flatMap((record, index) => recordSummaryLines(record, `History ${index + 1}`)),
    `Outcomes: ${
      outcomes.length
        ? outcomes.map(([name, count]) => `${name.replaceAll("_", " ")} ${count}`).join(" · ")
        : "none"
    }`,
  ]
}

export const JournalEvent = z.object({
  id: z.string(),
  goalID: z.string(),
  revision: z.number().int().positive(),
  envelopeRevision: z.number().int().positive().optional(),
  actor: z.enum(["user", "worker", "verifier", "system"]),
  type: z.string(),
  reasonCode: z.string().optional(),
  budgetDelta: z
    .object({
      turns: z.number().int().nonnegative().optional(),
      tokens: z.number().int().nonnegative().optional(),
      toolCalls: z.number().int().nonnegative().optional(),
    })
    .optional(),
  evidenceRefs: z.array(z.string()).optional(),
  stateHash: z.string().length(64).optional(),
  snapshot: Envelope.optional(),
  createdAt: z.number().int().positive(),
})
export type JournalEvent = z.infer<typeof JournalEvent>

export class ConflictError extends Error {
  constructor(
    readonly expected: number,
    readonly actual: number,
  ) {
    super(`Goal revision conflict: expected ${expected}, found ${actual}`)
  }
}

const TRANSITIONS: globalThis.Record<Status, ReadonlySet<Status>> = {
  queued: new Set(["active", "cancelled"]),
  active: new Set(["awaiting_verification", "paused", "blocked", "verification_degraded", "cancelled", "expired"]),
  awaiting_verification: new Set([
    "active",
    "paused",
    "blocked",
    "verification_degraded",
    "completed",
    "cancelled",
    "expired",
  ]),
  paused: new Set(["active", "cancelled", "expired"]),
  blocked: new Set(["active", "paused", "cancelled", "expired"]),
  verification_degraded: new Set(["active", "awaiting_verification", "paused", "cancelled", "expired"]),
  completed: new Set(),
  cancelled: new Set(),
  expired: new Set(),
}

export function transition(record: Record, next: Status, now = Date.now(), expectedRevision = record.revision) {
  if (record.revision !== expectedRevision) throw new ConflictError(expectedRevision, record.revision)
  if (record.status !== next && !TRANSITIONS[record.status].has(next)) {
    throw new Error(`Invalid goal transition: ${record.status} -> ${next}`)
  }
  return {
    ...record,
    status: next,
    revision: record.revision + 1,
    updatedAt: now,
    completedAt: next === "completed" ? now : record.completedAt,
  }
}

export function create(input: {
  objective: string
  owner: string
  workspace: string
  project: string
  sessionID: string
  status?: Status
  budget?: Partial<Budget>
  requiredEvidence?: EvidenceKind[]
  now?: number
}): Record {
  const now = input.now ?? Date.now()
  return Record.parse({
    id: `goal_${randomUUID()}`,
    revision: 1,
    objective: input.objective.trim(),
    status: input.status ?? "active",
    owner: input.owner,
    workspace: input.workspace,
    project: input.project,
    sessionID: input.sessionID,
    createdAt: now,
    updatedAt: now,
    react: 0,
    budget: {
      maxTurns: 12,
      usedTurns: 0,
      usedTokens: 0,
      usedToolCalls: 0,
      ...input.budget,
    },
    requiredEvidence: input.requiredEvidence ?? [],
    evidence: [],
    accountedMessageIDs: [],
  })
}

export function consumeBudget(
  record: Record,
  delta: { turns?: number; tokens?: number; toolCalls?: number },
  now = Date.now(),
) {
  const budget = {
    ...record.budget,
    usedTurns: record.budget.usedTurns + (delta.turns ?? 0),
    usedTokens: record.budget.usedTokens + (delta.tokens ?? 0),
    usedToolCalls: record.budget.usedToolCalls + (delta.toolCalls ?? 0),
  }
  const cause = budgetCause({ ...record, budget }, now)
  const next: Status = cause?.endsWith("_expired") ? "expired" : cause ? "paused" : record.status
  const updated = transition({ ...record, budget }, next, now)
  return { record: updated, exhausted: Boolean(cause), cause }
}

export const BudgetCause = z.enum([
  "deadline_expired",
  "wall_time_expired",
  "turn_budget_exhausted",
  "token_budget_exhausted",
  "tool_call_budget_exhausted",
])
export type BudgetCause = z.infer<typeof BudgetCause>

export function budgetCause(record: Record, now = Date.now()): BudgetCause | undefined {
  if (record.budget.deadline !== undefined && now >= record.budget.deadline) return "deadline_expired"
  if (record.budget.maxWallMs !== undefined && now - record.createdAt >= record.budget.maxWallMs) {
    return "wall_time_expired"
  }
  if (record.budget.maxTurns !== undefined && record.budget.usedTurns >= record.budget.maxTurns) {
    return "turn_budget_exhausted"
  }
  if (record.budget.maxTokens !== undefined && record.budget.usedTokens >= record.budget.maxTokens) {
    return "token_budget_exhausted"
  }
  if (record.budget.maxToolCalls !== undefined && record.budget.usedToolCalls >= record.budget.maxToolCalls) {
    return "tool_call_budget_exhausted"
  }
  return undefined
}

export function preflightBudget(record: Record, now = Date.now()) {
  const cause = budgetCause(record, now)
  if (!cause) return { record, cause: undefined, changed: false }
  const next: Status = cause.endsWith("_expired") ? "expired" : "paused"
  if (record.status === next) return { record, cause, changed: false }
  return { record: transition(record, next, now), cause, changed: true }
}

export function acquireLease(
  record: Record,
  owner: string,
  now = Date.now(),
  ttlMs = 60_000,
): { record: Record; acquired: boolean } {
  if (!["active", "verification_degraded", "awaiting_verification"].includes(record.status)) {
    return { record, acquired: false }
  }
  if (record.lease && record.lease.expiresAt > now && record.lease.owner !== owner) {
    return { record, acquired: false }
  }
  const attempt = (record.lease?.attempt ?? 0) + 1
  return {
    record: {
      ...record,
      revision: record.revision + 1,
      updatedAt: now,
      lease: {
        id: `lease_${randomUUID()}`,
        owner,
        acquiredAt: now,
        expiresAt: now + ttlMs,
        attempt,
      },
    },
    acquired: true,
  }
}

export function releaseLease(record: Record, leaseID: string, now = Date.now()) {
  if (!record.lease || record.lease.id !== leaseID) return record
  return {
    ...record,
    revision: record.revision + 1,
    updatedAt: now,
    lease: undefined,
  }
}

export function heartbeatLease(record: Record, leaseID: string, owner: string, now = Date.now(), ttlMs = 60_000) {
  if (!record.lease || record.lease.id !== leaseID || record.lease.owner !== owner) {
    return { record, renewed: false }
  }
  if (record.lease.expiresAt <= now) return { record, renewed: false }
  return {
    record: {
      ...record,
      revision: record.revision + 1,
      updatedAt: now,
      lease: { ...record.lease, expiresAt: now + ttlMs },
    },
    renewed: true,
  }
}

export function acquireContinuation(
  record: Record,
  input: {
    owner: string
    intentID: string
    reasonCode: string
    display: string
  },
  now = Date.now(),
  ttlMs = 150_000,
): { record: Record; acquired: boolean } {
  if (record.status !== "active") return { record, acquired: false }
  const current = record.continuation
  if (current?.intentID === input.intentID && current.expiresAt > now) {
    return { record, acquired: current.owner === input.owner }
  }
  if (current && current.expiresAt > now) return { record, acquired: false }
  const attempt = (current?.attempt ?? 0) + 1
  return {
    acquired: true,
    record: {
      ...record,
      revision: record.revision + 1,
      updatedAt: now,
      continuation: {
        id: `continuation_${randomUUID()}`,
        owner: input.owner,
        intentID: input.intentID,
        reasonCode: input.reasonCode,
        display: securityRedact(input.display).slice(0, 320),
        acquiredAt: now,
        expiresAt: now + ttlMs,
        attempt,
      },
    },
  }
}

export function claimContinuation(
  record: Record,
  owner: string,
  now = Date.now(),
  ttlMs = 150_000,
): { record: Record; claimed: boolean; changed: boolean } {
  const current = record.continuation
  if (!current || record.status !== "active") return { record, claimed: false, changed: false }
  if (current.expiresAt > now) {
    return { record, claimed: current.owner === owner, changed: false }
  }
  return {
    claimed: true,
    changed: true,
    record: {
      ...record,
      revision: record.revision + 1,
      updatedAt: now,
      continuation: {
        ...current,
        id: `continuation_${randomUUID()}`,
        owner,
        acquiredAt: now,
        expiresAt: now + ttlMs,
        attempt: current.attempt + 1,
      },
    },
  }
}

export function releaseContinuation(record: Record, continuationID: string, now = Date.now()) {
  if (!record.continuation || record.continuation.id !== continuationID) return record
  return {
    ...record,
    revision: record.revision + 1,
    updatedAt: now,
    continuation: undefined,
  }
}

export function makeEvidence(
  input: Omit<Evidence, "id" | "capturedAt" | "contentHash"> & { capturedAt?: number },
): Evidence {
  const capturedAt = input.capturedAt ?? Date.now()
  const safe = {
    ...input,
    subject: securityRedact(input.subject).trim().slice(0, 512),
    sourceRef: securityRedact(input.sourceRef).trim().slice(0, 1024),
    observation: securityRedact(input.observation).trim().slice(0, 4096),
    producer: securityRedact(input.producer).trim().slice(0, 512),
    verifier: input.verifier ? securityRedact(input.verifier).trim().slice(0, 512) : undefined,
  }
  const contentHash = evidenceHash(safe)
  return Evidence.parse({
    ...safe,
    id: `evidence_${randomUUID()}`,
    capturedAt,
    contentHash,
  })
}

function evidenceHash(item: Pick<Evidence, "kind" | "subject" | "sourceRef" | "observation" | "producer">) {
  return createHash("sha256")
    .update(JSON.stringify([item.kind, item.subject, item.sourceRef, item.observation, item.producer]))
    .digest("hex")
}

export function evidenceSatisfies(record: Record, trustedEvidenceIDs: ReadonlySet<string>, now = Date.now()) {
  return missingEvidenceKinds(record, trustedEvidenceIDs, now).length === 0
}

export function missingEvidenceKinds(record: Record, trustedEvidenceIDs: ReadonlySet<string>, now = Date.now()) {
  const valid = record.evidence.filter(
    (item) =>
      trustedEvidenceIDs.has(item.id) &&
      item.capturedAt >= record.createdAt &&
      item.capturedAt <= now + 300_000 &&
      item.sourceRef.trim().length > 0 &&
      item.contentHash === evidenceHash(item) &&
      evidenceVerifierShapeValid(item),
  )
  const kinds = new Set(valid.map((item) => item.kind))
  return record.requiredEvidence.filter((kind) => !kinds.has(kind))
}

export function evidenceVerifierShapeValid(item: Evidence) {
  if (item.kind === "model") return item.verifier === "goal-judge"
  if (item.kind === "command") {
    return item.sourceRef.startsWith("tool:") && item.verifier === `session-tool:${item.producer.replace(/^tool:/, "")}`
  }
  if (item.kind === "file") return /^file-sha256:[a-f0-9]{64}$/.test(item.verifier ?? "")
  return item.sourceRef === `user:${item.producer}` && item.verifier === `user:${item.producer}`
}

export function journal(
  record: Record,
  type: string,
  actor: JournalEvent["actor"],
  now = Date.now(),
  extra?: Pick<JournalEvent, "reasonCode" | "budgetDelta" | "evidenceRefs">,
): JournalEvent {
  return JournalEvent.parse({
    id: `goal_event_${randomUUID()}`,
    goalID: record.id,
    revision: record.revision,
    actor,
    type,
    createdAt: now,
    ...extra,
  })
}

function redactRecord(record: Record): Record {
  const evidence = record.evidence.map((item) => {
    const redacted = {
      ...item,
      subject: securityRedact(item.subject),
      sourceRef: securityRedact(item.sourceRef),
      observation: securityRedact(item.observation),
      producer: securityRedact(item.producer),
      verifier: item.verifier ? securityRedact(item.verifier) : undefined,
    }
    return { ...redacted, contentHash: evidenceHash(redacted) }
  })
  return Record.parse({
    ...record,
    objective: securityRedact(record.objective),
    evidence,
    lastOutcome: record.lastOutcome
      ? {
          ...record.lastOutcome,
          reason: record.lastOutcome.reason ? securityRedact(record.lastOutcome.reason) : undefined,
        }
      : undefined,
  })
}

export function privacySafeEnvelope(envelope: Envelope): Envelope {
  return Envelope.parse({
    ...envelope,
    active: envelope.active ? redactRecord(envelope.active) : undefined,
    queue: envelope.queue.map(redactRecord),
    history: envelope.history.map(redactRecord),
  })
}

export function envelopeHash(envelope: Envelope) {
  return createHash("sha256").update(JSON.stringify(envelope)).digest("hex")
}

export function withSnapshot(event: JournalEvent, envelope: Envelope): JournalEvent {
  const snapshot = privacySafeEnvelope(envelope)
  return JournalEvent.parse({
    ...event,
    envelopeRevision: snapshot.revision,
    stateHash: envelopeHash(snapshot),
    snapshot,
  })
}

/**
 * Rebuild the latest privacy-safe lifecycle state from the journal alone.
 * Older pre-snapshot events remain readable but cannot be used as checkpoints.
 */
export function replayJournal(events: JournalEvent[]): Envelope | undefined {
  let revision = 0
  let result: Envelope | undefined
  for (const event of events) {
    if (!event.snapshot || !event.stateHash || event.envelopeRevision === undefined) continue
    if (event.envelopeRevision < revision) throw new Error("Goal journal revisions are out of order")
    if (event.snapshot.revision !== event.envelopeRevision) {
      throw new Error("Goal journal snapshot revision does not match its event")
    }
    if (envelopeHash(event.snapshot) !== event.stateHash) throw new Error("Goal journal snapshot hash mismatch")
    revision = event.envelopeRevision
    result = event.snapshot
  }
  return result
}
