import { afterEach, describe, expect, test } from "bun:test"
import { Effect } from "effect"
import { Instance } from "../../src/project/instance"
import { Server } from "../../src/server/server"
import { Session } from "../../src/session"
import { Goal } from "../../src/session/goal"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID } from "../../src/session/schema"
import { Log } from "../../src/util"
import { tmpdir } from "../fixture/fixture"
import { createHash } from "node:crypto"
import { readFileSync } from "node:fs"
import path from "node:path"

void Log.init({ print: false })

afterEach(async () => {
  await Instance.disposeAll()
})

describe("session goal routes", () => {
  test("create, inspect, journal, and stale-edit conflict share one durable contract", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await Effect.runPromise(
          Session.Service.use((svc) => svc.create({})).pipe(Effect.provide(Session.defaultLayer)),
        )
        const app = Server.Default().app
        const secret = "sk-abcdefghijklmnopqrstuvwxyz123456"

        const created = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "create",
            objective: `ship with ${secret}`,
            budget: { maxTurns: 3 },
          }),
        })
        expect(created.status).toBe(200)
        const initial = (await created.json()) as {
          state: { active?: { id: string; objective: string; revision: number } }
          replay?: { active?: { objective: string } }
        }
        expect(initial.state.active?.objective).toContain(secret)
        expect(initial.replay?.active?.objective).toBe("ship with <redacted-openai-key>")

        const edited = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "edit",
            target: {
              goalID: initial.state.active?.id,
              expectedRevision: initial.state.active?.revision,
            },
            objective: "ship with verified artifacts",
          }),
        })
        expect(edited.status).toBe(200)

        const stale = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "edit",
            target: {
              goalID: initial.state.active?.id,
              expectedRevision: initial.state.active?.revision,
            },
            objective: "stale overwrite",
          }),
        })
        expect(stale.status).toBe(409)

        const inspected = await app.request(`/session/${session.id}/goal`)
        expect(inspected.status).toBe(200)
        const state = (await inspected.json()) as { state: { active?: { objective: string } } }
        expect(state.state.active?.objective).toBe("ship with verified artifacts")

        const journal = await app.request(`/session/${session.id}/goal/journal`)
        expect(journal.status).toBe(200)
        const events = (await journal.json()) as Array<{ type: string }>
        expect(events.map((event) => event.type)).toEqual(["created", "objective_edited"])
        expect(JSON.stringify(events)).not.toContain(secret)
      },
    })
  })

  test("goal list command renders budgets, evidence, history, and lifecycle outcomes", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await Effect.runPromise(
          Session.Service.use((svc) => svc.create({})).pipe(Effect.provide(Session.defaultLayer)),
        )
        const app = Server.Default().app
        const created = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "create",
            objective: "prepare release",
            budget: { maxTurns: 3 },
            requiredEvidence: ["command"],
          }),
        })
        const initial = (await created.json()) as {
          state: { active: { id: string; revision: number } }
        }
        await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "cancel",
            target: {
              goalID: initial.state.active.id,
              expectedRevision: initial.state.active.revision,
            },
          }),
        })
        await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: "create", objective: "ship release" }),
        })

        const listed = await app.request(`/session/${session.id}/command`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ command: "goal", arguments: "list" }),
        })
        expect(listed.status).toBe(200)
        const message = (await listed.json()) as { parts: Array<{ type: string; text?: string }> }
        const text = message.parts.flatMap((part) => (part.type === "text" ? [part.text ?? ""] : [])).join("\n")
        expect(text).toContain("State revision: 3")
        expect(text).toContain("Active: ship release [active, revision 1]")
        expect(text).toContain("Budget: turns 0/12")
        expect(text).toContain("Evidence: required none · attached 0")
        expect(text).toContain("History: 1")
        expect(text).toContain("History 1: prepare release [cancelled, revision 2]")
        expect(text).toContain("Evidence: required command · attached 0")
        expect(text).toContain("Outcomes: cancelled 1 · created 2")
      },
    })
  })

  test("goal command rejects a stale verification target before invoking a model", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await Effect.runPromise(
          Session.Service.use((svc) => svc.create({})).pipe(Effect.provide(Session.defaultLayer)),
        )
        const app = Server.Default().app
        const created = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ action: "create", objective: "bind verification" }),
        })
        const initial = (await created.json()) as {
          state: { active: { id: string; revision: number } }
        }
        const edited = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "edit",
            target: {
              goalID: initial.state.active.id,
              expectedRevision: initial.state.active.revision,
            },
            objective: "changed before verify",
          }),
        })
        expect(edited.status).toBe(200)

        const stale = await app.request(`/session/${session.id}/command`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            command: "goal",
            arguments: `verify ${initial.state.active.id} ${initial.state.active.revision}`,
          }),
        })
        expect(stale.status).toBe(500)
        expect(JSON.stringify(await stale.json())).toContain("Goal target is stale")
      },
    })
  })

  test("derives command and file evidence from server-owned state and rejects caller verifier fields", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await Effect.runPromise(
          Session.Service.use((svc) => svc.create({})).pipe(Effect.provide(Session.defaultLayer)),
        )
        const messageID = MessageID.ascending()
        const stalePartID = PartID.ascending()
        const verifiedPartID = PartID.ascending()
        await Effect.runPromise(
          Session.Service.use((svc) =>
            Effect.gen(function* () {
              yield* svc.updateMessage({
                id: messageID,
                sessionID: session.id,
                agentID: "main",
                role: "assistant",
                time: { created: Date.now(), completed: Date.now() },
                parentID: MessageID.ascending(),
                modelID: "test",
                providerID: "test",
                mode: "",
                agent: "test",
                path: { cwd: tmp.path, root: tmp.path },
                cost: 0,
                tokens: {
                  input: 0,
                  output: 0,
                  reasoning: 0,
                  cache: { read: 0, write: 0 },
                },
              } as MessageV2.Assistant)
              yield* svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "read-call",
                tool: "read",
                state: {
                  status: "completed",
                  input: { filePath: "package.json" },
                  output: "not command evidence",
                  title: "read",
                  metadata: {},
                  time: { start: Date.now(), end: Date.now() },
                },
              })
              yield* svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "failed-call",
                tool: "bash",
                state: {
                  status: "completed",
                  input: { command: "test" },
                  output: "tests failed",
                  title: "test",
                  metadata: { exit: 1 },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
              yield* svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "timeout-call",
                tool: "bash",
                state: {
                  status: "completed",
                  input: { command: "test" },
                  output: "command exceeded its timeout",
                  title: "test",
                  metadata: { exit: null },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
              yield* svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "cancelled-call",
                tool: "bash",
                state: {
                  status: "error",
                  input: { command: "test" },
                  error: "Tool execution aborted",
                  metadata: { interrupted: true },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
              yield* svc.updatePart({
                id: stalePartID,
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "stale-call",
                tool: "bash",
                state: {
                  status: "completed",
                  input: { command: "test" },
                  output: "tests passed",
                  title: "test",
                  metadata: { exit: 0 },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
            }),
          ).pipe(Effect.provide(Session.defaultLayer)),
        )
        const artifact = readFileSync(path.resolve("package.json"))
        const app = Server.Default().app
        const created = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "create",
            objective: "prove command and artifact",
            requiredEvidence: ["command", "file"],
          }),
        })
        const initial = (await created.json()) as {
          state: { active: { id: string; revision: number } }
        }
        const target = {
          goalID: initial.state.active.id,
          expectedRevision: initial.state.active.revision,
        }

        const forged = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "evidence",
            target,
            evidence: {
              kind: "command",
              subject: "tests",
              sourceRef: "tool:verified-call",
              producer: "caller",
              verifier: "session-tool:bash",
            },
          }),
        })
        expect(forged.status).toBe(400)

        const rejectedCommand = async (sourceRef: string) => {
          const response = await app.request(`/session/${session.id}/goal`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              action: "evidence",
              target,
              evidence: { kind: "command", subject: "tests", sourceRef },
            }),
          })
          expect(response.status).toBe(500)
          return ((await response.json()) as { data: { message: string } }).data.message
        }
        expect(await rejectedCommand("tool:read-call")).toContain("approved command/test tool")
        expect(await rejectedCommand("tool:failed-call")).toContain("failed with exit code 1")
        expect(await rejectedCommand("tool:timeout-call")).toContain("timed out")
        expect(await rejectedCommand("tool:cancelled-call")).toContain("cancelled")
        expect(await rejectedCommand("tool:stale-call")).toContain("predates the active goal")

        await Effect.runPromise(
          Session.Service.use((svc) =>
            Effect.gen(function* () {
              yield* svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "interactive-call",
                tool: "bash",
                state: {
                  status: "completed",
                  input: { command: "test", interactive: true },
                  output: "client says tests passed",
                  title: "test",
                  metadata: { exit: 0, containment: "interactive-client" },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
              yield* svc.updatePart({
                id: verifiedPartID,
                sessionID: session.id,
                messageID,
                type: "tool",
                callID: "verified-call",
                tool: "bash",
                state: {
                  status: "completed",
                  input: { command: "test" },
                  output: "tests passed",
                  title: "test",
                  metadata: { exit: 0 },
                  time: { start: Date.now(), end: Date.now() },
                },
              })
            }),
          ).pipe(Effect.provide(Session.defaultLayer)),
        )
        expect(await rejectedCommand("tool:interactive-call")).toContain("interactive client")

        const command = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "evidence",
            target,
            evidence: {
              kind: "command",
              subject: "tests",
              sourceRef: "tool:verified-call",
            },
          }),
        })
        expect(command.status).toBe(200)
        const commandState = (await command.json()) as {
          state: {
            active: {
              id: string
              revision: number
              evidence: Array<{ kind: string; producer: string; verifier?: string; observation: string }>
            }
          }
        }
        expect(commandState.state.active.evidence[0]).toMatchObject({
          kind: "command",
          producer: "tool:bash",
          verifier: "session-tool:bash",
          observation: "bash exited 0: tests passed",
        })

        const file = await app.request(`/session/${session.id}/goal`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            action: "evidence",
            target: {
              goalID: commandState.state.active.id,
              expectedRevision: commandState.state.active.revision,
            },
            evidence: {
              kind: "file",
              subject: "artifact",
              sourceRef: "packages/opencode/package.json",
            },
          }),
        })
        expect(file.status).toBe(200)
        const fileState = (await file.json()) as {
          state: {
            active: {
              id: string
              revision: number
              evidence: Array<{ kind: string; producer: string; verifier?: string }>
            }
          }
        }
        expect(fileState.state.active.evidence[1]).toMatchObject({
          kind: "file",
          producer: "open-clank:file-verifier",
          verifier: `file-sha256:${createHash("sha256").update(artifact).digest("hex")}`,
        })

        const lease = await Instance.provide({
          directory: process.cwd(),
          fn: () =>
            Effect.runPromise(
              Goal.Service.use((goal) =>
                goal.beginVerification(session.id, Goal.target(fileState.state.active), "test-verifier"),
              ).pipe(Effect.provide(Goal.defaultLayer)),
            ),
        })
        if (!lease) throw new Error("expected verification lease")
        await Effect.runPromise(
          Session.Service.use((svc) =>
            svc.updatePart({
              id: verifiedPartID,
              sessionID: session.id,
              messageID,
              type: "tool",
              callID: "verified-call",
              tool: "bash",
              state: {
                status: "completed",
                input: { command: "test" },
                output: "tests failed on revalidation",
                title: "test",
                metadata: { exit: 1 },
                time: { start: Date.now(), end: Date.now() },
              },
            }),
          ).pipe(Effect.provide(Session.defaultLayer)),
        )
        const completion = await Instance.provide({
          directory: process.cwd(),
          fn: () =>
            Effect.runPromise(
              Goal.Service.use((goal) =>
                goal.verificationCompleted(session.id, lease, "done", {
                  kind: "model",
                  subject: "goal",
                  sourceRef: "message:test",
                  observation: "done",
                  producer: "judge",
                  verifier: "goal-judge",
                }),
              ).pipe(Effect.provide(Goal.defaultLayer)),
            ),
        })
        expect(completion).toMatchObject({ kind: "evidence_missing", missing: ["command"] })
      },
    })
  })
})
