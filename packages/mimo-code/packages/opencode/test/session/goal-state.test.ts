import { describe, expect, test } from "bun:test"
import * as GoalState from "../../src/session/goal-state"

function record(input?: {
  status?: GoalState.Status
  budget?: Partial<GoalState.Budget>
  requiredEvidence?: GoalState.EvidenceKind[]
}) {
  return GoalState.create({
    objective: "ship safely",
    owner: "owner-a",
    workspace: "workspace-a",
    project: "project-a",
    sessionID: "session-a",
    status: input?.status,
    budget: input?.budget,
    requiredEvidence: input?.requiredEvidence,
    now: 1_000,
  })
}

describe("goal lifecycle", () => {
  test("summary exposes the full material state shared by CLI and TUI", () => {
    const evidence = GoalState.makeEvidence({
      kind: "command",
      subject: "focused tests",
      sourceRef: "tool:test",
      observation: "exit 0",
      producer: "tool:bash",
      verifier: "session-tool:bash",
      capturedAt: 2_000,
    })
    const active = {
      ...record({ budget: { maxTurns: 3 }, requiredEvidence: ["command"] }),
      evidence: [evidence],
    }
    const queued = { ...record({ status: "queued" }), objective: "write release notes" }
    const history = {
      ...record({ status: "completed" }),
      objective: "prepare release",
      lastOutcome: {
        code: "met",
        reason: "verified",
        verifiedAt: 3_000,
        evidenceRefs: [evidence.id],
      },
    }
    const lines = GoalState.summaryLines(
      GoalState.Envelope.parse({ revision: 7, active, queue: [queued], history: [history] }),
      { completed: 1, objective_edited: 2 },
    )

    expect(lines).toContain("State revision: 7")
    expect(lines).toContain("Active: ship safely [active, revision 1]")
    expect(lines).toContain("  Budget: turns 0/3 · tokens 0/no limit · tool calls 0/no limit")
    expect(lines).toContain("  Evidence: required command · attached 1")
    expect(lines).toContain("  Evidence command: focused tests")
    expect(lines).toContain("Queued 1: write release notes [queued, revision 1]")
    expect(lines).toContain("History: 1")
    expect(lines).toContain("  Outcome: met — verified")
    expect(lines).toContain("Outcomes: completed 1 · objective edited 2")
  })

  test("rejects stale revisions and invalid transitions", () => {
    const active = record()
    expect(() => GoalState.transition(active, "paused", 2_000, active.revision - 1)).toThrow(GoalState.ConflictError)
    const done = GoalState.transition(GoalState.transition(active, "awaiting_verification"), "completed")
    expect(() => GoalState.transition(done, "active")).toThrow("Invalid goal transition")
  })

  test("turn, token, tool, wall, and deadline boundaries pause or expire", () => {
    expect(GoalState.consumeBudget(record({ budget: { maxTurns: 0 } }), {}, 1_000).record.status).toBe("paused")
    expect(GoalState.consumeBudget(record({ budget: { maxTurns: 1 } }), { turns: 1 }, 1_100).record.status).toBe(
      "paused",
    )
    expect(GoalState.consumeBudget(record({ budget: { maxTokens: 1 } }), { tokens: 1 }, 1_100).record.status).toBe(
      "paused",
    )
    expect(
      GoalState.consumeBudget(record({ budget: { maxToolCalls: 1 } }), { toolCalls: 1 }, 1_100).record.status,
    ).toBe("paused")
    expect(GoalState.consumeBudget(record({ budget: { maxWallMs: 100 } }), {}, 1_100).record.status).toBe("expired")
    expect(GoalState.consumeBudget(record({ budget: { deadline: 1_100 } }), {}, 1_100).record.status).toBe("expired")
  })

  test("only one live lease owner can acquire and expired leases recover once", () => {
    const first = GoalState.acquireLease(record(), "worker-a", 2_000, 100)
    expect(first.acquired).toBeTrue()
    const raced = GoalState.acquireLease(first.record, "worker-b", 2_050, 100)
    expect(raced.acquired).toBeFalse()
    const recovered = GoalState.acquireLease(first.record, "worker-b", 2_101, 100)
    expect(recovered.acquired).toBeTrue()
    expect(recovered.record.lease?.attempt).toBe(2)
  })

  test("lease heartbeat is owner-bound and cannot revive an expired lease", () => {
    const leased = GoalState.acquireLease(record(), "worker-a", 2_000, 100).record
    expect(GoalState.heartbeatLease(leased, leased.lease!.id, "worker-b", 2_050, 100).renewed).toBeFalse()
    const heartbeat = GoalState.heartbeatLease(leased, leased.lease!.id, "worker-a", 2_050, 100)
    expect(heartbeat.renewed).toBeTrue()
    expect(heartbeat.record.lease?.expiresAt).toBe(2_150)
    expect(GoalState.heartbeatLease(leased, leased.lease!.id, "worker-a", 2_101, 100).renewed).toBeFalse()
  })

  test("required evidence kinds gate completion", () => {
    const current = record({ requiredEvidence: ["command", "file"] })
    const command = GoalState.makeEvidence({
      kind: "command",
      subject: "tests",
      sourceRef: "tool:1",
      observation: "exit 0",
      producer: "tool:bash",
      verifier: "session-tool:bash",
      capturedAt: 2_000,
    })
    expect(GoalState.evidenceSatisfies({ ...current, evidence: [command] }, new Set([command.id]))).toBeFalse()
    const file = GoalState.makeEvidence({
      kind: "file",
      subject: "artifact",
      sourceRef: "file:1",
      observation: "present",
      producer: "runner",
      verifier: `file-sha256:${"a".repeat(64)}`,
      capturedAt: 2_001,
    })
    const trusted = new Set([command.id, file.id])
    expect(GoalState.evidenceSatisfies({ ...current, evidence: [command, file] }, trusted)).toBeTrue()
    expect(
      GoalState.evidenceSatisfies(
        {
          ...current,
          evidence: [{ ...command, contentHash: "tampered" }, file],
        },
        trusted,
      ),
    ).toBeFalse()
    expect(
      GoalState.evidenceSatisfies(
        {
          ...current,
          evidence: [{ ...command, capturedAt: current.createdAt - 1 }, file],
        },
        trusted,
      ),
    ).toBeFalse()
    expect(GoalState.missingEvidenceKinds({ ...current, evidence: [command] }, new Set([command.id]))).toEqual(["file"])
    expect(GoalState.evidenceSatisfies({ ...current, evidence: [command, file] }, new Set())).toBeFalse()
  })

  test("journal snapshots replay state without retaining credential text", () => {
    const current = { ...record(), objective: "ship with sk-abcdefghijklmnopqrstuvwxyz123456" }
    const envelope = GoalState.Envelope.parse({ revision: 1, active: current, queue: [], history: [] })
    const item = GoalState.withSnapshot(
      GoalState.journal(current, "verification_rejected", "verifier", 2_000, {
        reasonCode: "not_met",
        budgetDelta: { turns: 1 },
      }),
      envelope,
    )
    expect(item.reasonCode).toBe("not_met")
    expect(JSON.stringify(item)).not.toContain("sk-abcdefghijklmnopqrstuvwxyz123456")
    expect(GoalState.replayJournal([item])?.active?.objective).toBe("ship with <redacted-openai-key>")
    expect(() => GoalState.replayJournal([{ ...item, stateHash: "0".repeat(64) }])).toThrow(
      "Goal journal snapshot hash mismatch",
    )
  })
})
