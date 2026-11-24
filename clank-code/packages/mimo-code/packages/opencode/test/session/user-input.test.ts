import { createHash } from "node:crypto"
import { afterEach, describe, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { Database, eq, sql } from "../../src/storage"
import { QuestionRequestTable, UserInputHandoffTable, UserInputReceiptTable } from "../../src/inbox/inbox.sql"
import * as UserInput from "../../src/session/user-input"
import { MessageID, PartID } from "../../src/session/schema"
import { tmpdir } from "../fixture/fixture"

const layer = Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer, Bus.defaultLayer)
const binding: UserInput.Binding = {
  owner: "owner-fixture",
  stableChatID: "chat-fixture",
  mapRevision: 3,
  mappingRevision: 5,
  workspaceRevision: 8,
}

afterEach(async () => Instance.disposeAll())

describe("durable user input receipts", () => {
  test("resolves and consumes only the captured policy batch, preserving later arrivals", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        const base = {
          ...binding,
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: executionRevision!,
          attachmentRefs: [],
          questionRefs: [],
          mode: "steer" as const,
        }
        UserInput.admit({ ...base, requestID: "captured-policy-1", text: "first frozen input" })
        const snapshot = UserInput.snapshotBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding,
          afterTurnReady: false,
        })
        expect(snapshot.map((receipt) => receipt.requestID)).toEqual(["captured-policy-1"])
        UserInput.admit({ ...base, requestID: "late-policy-2", text: "arrived during resolve" })
        const policyBatch: UserInput.PolicyBatchEvidence = {
          version: 1,
          receipts: snapshot.map(({ requestID, payloadHash, executionRevision, mapRevision, mappingRevision, workspaceRevision }) =>
            ({ requestID, payloadHash, executionRevision, mapRevision, mappingRevision, workspaceRevision })),
          choices: [{ requestID: snapshot[0]!.requestID, choiceSnapshot: null, guideOnly: false }],
          batchMode: "agent",
          batchGuideOnly: false,
          toolProjection: { blockAllToolCalls: false, disabledTools: [], hiddenTools: [], allowedTools: null },
        }
        expect(UserInput.consumeBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding,
          afterTurnReady: false,
          expectedReceipts: policyBatch.receipts,
          policyBatch,
        })).toBe(1)
        const rows = UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "fixture-actor" })
        expect(rows.find((row) => row.request_id === "captured-policy-1")?.state).toBe("consumed")
        expect(rows.find((row) => row.request_id === "late-policy-2")?.state).toBe("persisted")
        const messages = await rt.runPromise(Session.Service.use((s) => s.messages({ sessionID: session.id, agentID: "fixture-actor" })))
        const consumed = messages.find((message) => message.info.role === "user" && message.info.metadata?.turn_policy_batch)
        expect(consumed?.info.role === "user" ? consumed.info.metadata?.turn_policy_batch : undefined).toEqual(policyBatch)
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("deduplicates identical retries, rejects changed payloads, and consumes transcript once", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        expect(executionRevision).toBe(1)
        const request = {
          ...binding,
          requestID: "stable-request-1",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: executionRevision!,
          text: "continue with the fix",
          attachmentRefs: [],
          attachmentUploadIDs: ["owner-upload-1"],
          attachmentParts: [{ type: "file" as const, mime: "image/png", filename: "screen.png", url: "data:image/png;base64,aGVsbG8=" }],
          questionRefs: [],
          modelRequestOptions: {
            present: true,
            routeID: "model-route-fixture",
            optionRouteID: "public-route-fixture",
            catalogRevision: 7,
            model: { providerID: "openai", modelID: "gpt-fixture" },
            options: { reasoning_effort: "none" },
          },
          mode: "steer" as const,
        }
        const first = UserInput.admit(request)
        const preO09Hash = createHash("sha256").update(JSON.stringify({
          sessionID: request.sessionID,
          actorID: request.actorID,
          text: request.text,
          attachmentRefs: request.attachmentRefs,
          attachmentUploadIDs: request.attachmentUploadIDs,
          questionRefs: request.questionRefs,
          modelRequestOptions: request.modelRequestOptions,
        })).digest("hex")
        Database.use((db) => db.update(UserInputReceiptTable)
          .set({ payload_hash: preO09Hash })
          .where(eq(UserInputReceiptTable.request_id, request.requestID)).run())
        const retry = UserInput.admit(request)
        expect(retry.created_at).toBe(first.created_at)
        expect(UserInput.admit({ ...request, turnPolicy: null }).created_at).toBe(first.created_at)
        expect(first.attachment_upload_ids).toEqual(["owner-upload-1"])
        expect(first.model_request_options).toEqual(request.modelRequestOptions)
        expect(UserInput.hasPending(session.id, "fixture-actor")).toBe(true)
        expect(() => UserInput.admit({ ...request, text: "different payload" })).toThrow(UserInput.UserInputConflict)

        Database.use((db) => db.run(sql.raw(`CREATE TRIGGER fixture_fail_receipt_consume BEFORE UPDATE OF state ON user_input_receipt WHEN NEW.state='consumed' BEGIN SELECT RAISE(ABORT, 'fixture rollback'); END`)))
        expect(() => UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toThrow()
        let messages = await rt.runPromise(Session.Service.use((s) => s.messages({ sessionID: session.id, agentID: "fixture-actor" })))
        expect(messages.some((item) => item.info.role === "user" && item.parts.some((part) => part.type === "text" && part.text === request.text))).toBe(false)
        Database.use((db) => db.run(sql.raw("DROP TRIGGER fixture_fail_receipt_consume")))

        expect(UserInput.consumeBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding,
          afterTurnReady: false,
        })).toBe(1)
        expect(UserInput.hasPending(session.id, "fixture-actor")).toBe(false)
        expect(UserInput.consumeBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding,
          afterTurnReady: false,
        })).toBe(0)
        const receipt = Database.use((db) => db.select().from(UserInputReceiptTable).where(eq(UserInputReceiptTable.request_id, request.requestID)).get())
        expect(receipt?.state).toBe("consumed")
        expect(receipt?.message_id).toBeTruthy()
        expect(receipt?.attachment_parts).toEqual(request.attachmentParts)
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.settleExecution(session.id, "fixture-actor", executionRevision!, "success")))
        const nextRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        expect(UserInput.admit({ ...request, executionRevision: nextRevision!, originTurnID: "host-recomputed-latest-turn" }).state).toBe("consumed")
        messages = await rt.runPromise(Session.Service.use((s) => s.messages({ sessionID: session.id, agentID: "fixture-actor" })))
        expect(messages.filter((item) => item.info.role === "user" && item.parts.some((part) => part.type === "text" && part.text === request.text))).toHaveLength(1)
        expect(messages.find((item) => item.info.role === "user" && item.info.id === receipt?.message_id)?.parts.some((part) => part.type === "file" && part.filename === "screen.png" && part.url === "data:image/png;base64,aGVsbG8=")).toBe(true)
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("persists canonical turn choices, binds changed choices to request identity, and exports them", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "policy-fixture",
          mode: "main",
          agent: "build",
          description: "policy fixture",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "policy-fixture")))
        const turnPolicy = {
          version: 1 as const, mode: "agent" as const, allowBash: false,
          allowWebSearch: false, useWeb: false, useResearch: false,
          compareMode: false, incognito: false, noMemory: false,
        }
        const ceiling = { version: 1 as const, blockAllToolCalls: false, disabledTools: ["bash"], hiddenTools: [], allowedTools: null }
        const request = {
          ...binding,
          requestID: "policy-request-1",
          sessionID: session.id,
          actorID: "policy-fixture",
          executionRevision: executionRevision!,
          text: "continue under my captured choices",
          attachmentRefs: [],
          attachmentUploadIDs: [],
          questionRefs: [],
          turnPolicy,
          effectivePolicyCeiling: ceiling,
          mode: "steer" as const,
        }
        const first = UserInput.admit(request)
        const expectedHash = createHash("sha256").update(JSON.stringify({
          sessionID: request.sessionID,
          actorID: request.actorID,
          text: request.text,
          attachmentRefs: request.attachmentRefs,
          attachmentUploadIDs: request.attachmentUploadIDs,
          questionRefs: request.questionRefs,
          modelRequestOptions: { present: false },
          turnPolicy,
        })).digest("hex")
        expect(first.turn_policy).toEqual(turnPolicy)
        expect(first.effective_policy_ceiling).toEqual(ceiling)
        expect(first.payload_hash).toBe(expectedHash)
        expect(UserInput.admit(request).created_at).toBe(first.created_at)
        expect(UserInput.admit({ ...request, effectivePolicyCeiling: { ...ceiling, disabledTools: ["bash", "web_search"] } }).effective_policy_ceiling).toEqual(ceiling)
        expect(() => UserInput.admit({ ...request, turnPolicy: { ...turnPolicy, allowBash: true } })).toThrow(UserInput.UserInputConflict)
        expect(() => UserInput.admit({ ...request, turnPolicy: { ...turnPolicy, unexpected: true } as never })).toThrow(UserInput.UserInputConflict)
        const exported = UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "policy-fixture" })
        expect(exported[0]?.turn_policy).toEqual(turnPolicy)
        expect(exported[0]?.effective_policy_ceiling).toEqual(ceiling)
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("holds after-turn receipts until an explicit completed-turn boundary and rejects stale bindings", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        const request = {
          ...binding,
          requestID: "stable-request-2",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: executionRevision!,
          text: "after this turn",
          attachmentRefs: [],
          questionRefs: ["question-fixture"],
          mode: "after-turn" as const,
        }
        UserInput.admit(request, new Set(["question-fixture"]))
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(0)
        expect(UserInput.hasPendingAfterTurn(session.id, "fixture-actor", binding)).toBe(true)
        expect(UserInput.consumeBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding: { ...binding, mapRevision: binding.mapRevision + 1 },
          afterTurnReady: true,
        })).toBe(1)
        const sendNow = { ...request, requestID: "send-now-request", text: "promote this same receipt" }
        const queued = UserInput.admit(sendNow, new Set(["question-fixture"]))
        const promoted = UserInput.admit({ ...sendNow, mode: "steer" })
        expect(promoted.created_at).toBe(queued.created_at)
        expect(promoted.mode).toBe("steer")
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(1)
        UserInput.admit({ ...request, requestID: "stable-request-2-stale", questionRefs: [] })
        expect(UserInput.consumeBoundary({
          sessionID: session.id,
          actorID: "fixture-actor",
          binding: { ...binding, mappingRevision: binding.mappingRevision + 1 },
          afterTurnReady: true,
        })).toBe(0)
        const stale = UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "fixture-actor" })
          .find((item) => item.request_id === "stable-request-2-stale")
        expect(stale?.state).toBe("failed")
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("validates attachment references against the same actor slice and preserves them in the transcript", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        const sourceMessageID = MessageID.ascending()
        const sourcePartID = PartID.ascending()
        await rt.runPromise(Session.Service.use((s) => Effect.gen(function* () {
          yield* s.updateMessage({
            id: sourceMessageID,
            sessionID: session.id,
            agentID: "fixture-actor",
            role: "user",
            time: { created: Date.now() },
            agent: "build",
            model: { providerID: "openai" as never, modelID: "gpt-fixture" as never },
          })
          yield* s.updatePart({
            id: sourcePartID,
            messageID: sourceMessageID,
            sessionID: session.id,
            type: "file",
            mime: "text/plain",
            filename: "fixture.txt",
            url: "data:text/plain,fixture",
          })
        })))
        const request = {
          ...binding,
          requestID: "stable-request-3",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: executionRevision!,
          text: "review this file",
          attachmentRefs: [`${sourceMessageID}/${sourcePartID}`],
          questionRefs: [],
          mode: "steer" as const,
        }
        UserInput.admit(request)
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(1)
        const messages = await rt.runPromise(Session.Service.use((s) => s.messages({ sessionID: session.id, agentID: "fixture-actor" })))
        const delivered = messages.find((item) => item.info.role === "user" && item.parts.some((part) => part.type === "text" && part.text === request.text))
        expect(delivered?.parts.some((part) => part.type === "file" && part.filename === "fixture.txt")).toBe(true)
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("keeps idle admission pending until a request-bound execution handoff exists", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        UserInput.admit({
          ...binding,
          requestID: "idle-request",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: 0,
          text: "wait for explicit run",
          attachmentRefs: [],
          questionRefs: [],
          mode: "after-turn",
        })
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(0)
        expect(UserInput.hasPendingAfterTurn(session.id, "fixture-actor", binding)).toBe(false)
        const claimed = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        expect(claimed).toBe(1)
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: true })).toBe(0)
        expect(UserInput.hasPendingAfterTurn(session.id, "fixture-actor", binding)).toBe(false)
        expect(UserInput.hasPending(session.id, "fixture-actor")).toBe(true)
        const exported = UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "fixture-actor" })
        expect(exported.some((receipt) => receipt.request_id === "idle-request" && receipt.state === "pending-boundary")).toBe(true)
        expect(UserInput.cancel({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "fixture-actor", requestID: "idle-request" })).toBe(true)
        expect(UserInput.hasPending(session.id, "fixture-actor")).toBe(false)
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("binds idle receipt delivery to one CAS handoff and permits only a new explicit retry after terminal pre-consumption settlement", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        const registry = await rt.runPromise(ActorRegistry.Service.use((service) => Effect.succeed(service)))
        await rt.runPromise(registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        }))
        const initial = await rt.runPromise(registry.beginExecution(session.id, "fixture-actor"))
        await rt.runPromise(registry.settleExecution(session.id, "fixture-actor", initial!, "success"))
        UserInput.admit({
          ...binding,
          requestID: "claimable-request",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: initial!,
          text: "deliver exactly once",
          attachmentRefs: [],
          questionRefs: [],
          mode: "steer",
        })
        const claim = {
          ...binding,
          requestID: "claimable-request",
          handoffID: "handoff-a",
          rootOperationID: "input-root-a",
          expectedExecutionRevision: initial!,
        }
        expect(await rt.runPromise(registry.beginExecutionForInput!(session.id, "fixture-actor", claim))).toEqual({
          kind: "fresh-claim", claimedExecutionRevision: 2,
        })
        expect(await rt.runPromise(registry.beginExecutionForInput!(session.id, "fixture-actor", claim))).toEqual({
          kind: "existing-claim", claimedExecutionRevision: 2, outcome: "claimed",
        })
        expect((await rt.runPromise(registry.get(session.id, "fixture-actor")))?.executionRevision).toBe(2)
        await rt.runPromise(registry.settleExecution(session.id, "fixture-actor", 2, "failure", "fixture setup failure"))
        const retryClaim = { ...claim, handoffID: "handoff-b", rootOperationID: "input-root-b", expectedExecutionRevision: 2 }
        expect(await rt.runPromise(registry.beginExecutionForInput!(session.id, "fixture-actor", retryClaim))).toEqual({
          kind: "fresh-claim", claimedExecutionRevision: 3,
        })
        expect(await rt.runPromise(registry.beginExecutionForInput!(session.id, "fixture-actor", retryClaim))).toEqual({
          kind: "existing-claim", claimedExecutionRevision: 3, outcome: "claimed",
        })
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(1)
        const attempts = Database.use((db) => db.select().from(UserInputHandoffTable).all().sort((a, b) => a.handoff_id.localeCompare(b.handoff_id)))
        expect(attempts.map((attempt) => [attempt.handoff_id, attempt.outcome])).toEqual([
          ["handoff-a", "settled-before-consumption"],
          ["handoff-b", "consumed"],
        ])
      } finally {
        await rt.dispose()
      }
    } })
  })

  test("queues validated structured question answers and finalizes them with the transcript message exactly once", async () => {
    await using tmp = await tmpdir({ git: true })
    await Instance.provide({ directory: tmp.path, fn: async () => {
      const rt = ManagedRuntime.make(layer)
      try {
        const session = await rt.runPromise(Session.Service.use((s) => s.create()))
        await rt.runPromise(ActorRegistry.Service.use((registry) => registry.register({
          sessionID: session.id,
          actorID: "fixture-actor",
          mode: "main",
          agent: "build",
          description: "fixture actor",
          contextMode: "none",
          background: false,
          lifecycle: "persistent",
          effectiveModel: { providerID: "openai", modelID: "gpt-fixture" },
          hostContext: { chatID: binding.stableChatID },
        })))
        const executionRevision = await rt.runPromise(ActorRegistry.Service.use((registry) => registry.beginExecution(session.id, "fixture-actor")))
        Database.use((db) => db.insert(QuestionRequestTable).values({
          owner: binding.owner,
          stable_chat_id: binding.stableChatID,
          request_id: "question-fixture",
          session_id: session.id,
          actor_id: "fixture-actor",
          origin_turn_id: null,
          tool_message_id: null,
          tool_call_id: null,
          question_revision: 1,
          plan_revision: null,
          blocking: 0,
          questions: [{ question: "Which path?", options: [{ label: "A" }, { label: "B" }], multiple: false, custom: false }],
          payload_hash: "fixture-hash",
          state: "pending",
          answer_payload: null,
          answer_disposition: null,
          answer_request_id: null,
          answer_message_id: null,
          created_at: Date.now(),
          updated_at: Date.now(),
        }).run())
        const request: UserInput.Admission = {
          ...binding,
          requestID: "question-answer-request",
          sessionID: session.id,
          actorID: "fixture-actor",
          executionRevision: executionRevision!,
          text: "",
          attachmentRefs: [],
          questionRefs: ["question-fixture@1"],
          questionAnswers: [{ requestID: "question-fixture", questionRevision: 1, disposition: "answered", answers: [["B"]] }],
          mode: "steer",
        }
        const admitted = UserInput.admit(request)
        expect(admitted.text).toBe('"Which path?"="B"')
        expect(UserInput.admit(request).created_at).toBe(admitted.created_at)
        expect(() => UserInput.admit({ ...request, questionAnswers: [{ ...request.questionAnswers![0]!, answers: [["not-an-option"]] }] })).toThrow(UserInput.UserInputConflict)
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(1)
        expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "fixture-actor", binding, afterTurnReady: false })).toBe(0)
        const question = Database.use((db) => db.select().from(QuestionRequestTable).where(eq(QuestionRequestTable.request_id, "question-fixture")).get())
        const receipt = Database.use((db) => db.select().from(UserInputReceiptTable).where(eq(UserInputReceiptTable.request_id, request.requestID)).get())
        expect(question?.state).toBe("answered")
        expect(question?.answer_payload).toEqual({ answers: [["B"]] })
        expect(question?.answer_request_id).toBe("question-answer-request")
        expect(question?.answer_message_id).toBe(receipt?.message_id)
      } finally {
        await rt.dispose()
      }
    } })
  })
})
