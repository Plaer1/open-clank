import { afterEach, expect, test } from "bun:test"
import { Effect, Layer, ManagedRuntime } from "effect"
import { Instance } from "../../src/project/instance"
import { Session } from "../../src/session"
import { persistManagedActorCapture, reconcileManagedActorExecution } from "../../src/session/prompt"
import { MessageID } from "../../src/session/schema"
import { ActorRegistry } from "../../src/actor/registry"
import { Bus } from "../../src/bus"
import { ManagedProvider } from "../../src/acp/managed-provider"
import * as UserInput from "../../src/session/user-input"
import { tmpdir } from "../fixture/fixture"

const layer = Layer.mergeAll(Session.defaultLayer, ActorRegistry.defaultLayer, Bus.defaultLayer)
const binding = { owner: "fixture-owner", stableChatID: "fixture-chat", mapRevision: 3, mappingRevision: 5, workspaceRevision: 8 }

afterEach(async () => {
  ManagedProvider.resetForTest()
  delete process.env.OPEN_CLANK_MANAGED
  await Instance.disposeAll()
})

test("actor admission keeps exact provider, turn and input identities through concurrent work and resume", async () => {
  process.env.OPEN_CLANK_MANAGED = "1"
  const calls: string[] = []
  let revokeGrant = false
  let turnStatus: "active" | "unknown" = "unknown"
  ManagedProvider.installHostConnection({
    async extMethod(method: string, params: Record<string, any>) {
      calls.push(method)
      if (method.endsWith("/actor-route/preflight")) {
        const candidate = params.optionCandidate
        const sameModel = candidate && candidate.model.providerID === params.model.providerID && candidate.model.modelID === params.model.modelID
        const snapshot = params.savedOptionSnapshot ?? (candidate ? {
          version: 1, routeID: `route-${params.model.modelID}`, optionRouteID: `route-${params.model.modelID}`, catalogRevision: 1,
          model: params.model, options: sameModel && candidate.variant ? { reasoning_effort: candidate.variant } : {},
          source: sameModel ? candidate.source : { kind: "default" },
        } : params.actorID === "main" ? {
          version: 1, routeID: `route-${params.model.modelID}`, optionRouteID: `route-${params.model.modelID}`, catalogRevision: 1,
          model: params.model, options: {}, source: { kind: "receipt", requestID: params.rootOperationID },
        } : undefined)
        return {
        connectionID: params.model.providerID, providerID: params.model.providerID, billingLane: "local",
        modelID: params.model.modelID, modelRouteID: `route-${params.model.modelID}`,
        ...(snapshot ? { modelRequestOptions: snapshot } : {}),
        bootstrap: { ...binding, rootSessionID: params.sessionID, sessionID: params.sessionID,
          actorID: params.actorID, rootOperationID: params.rootOperationID,
          expectedExecutionRevision: params.expectedExecutionRevision, modelRouteID: `route-${params.model.modelID}`,
          ...(snapshot ? { modelRequestOptions: snapshot } : {}) },
      } }
      if (method.endsWith("/account/bind") && revokeGrant) throw new Error("grant revoked")
      if (method.endsWith("/account/bind")) return {
        ...params, bindingID: `binding-${params.actorID}-${params.executionRevision}`, bindingRevision: 1,
        credentialRequired: false, source: "keyless", attempt: 1, committed: false,
      }
      if (method.endsWith("/logging/v1/admit")) return null
      if (method.endsWith("/turn/start")) return { ...params, accepted: true, replayed: false }
      if (method.endsWith("/turn/status")) return { ...params, state: turnStatus }
      if (method.endsWith("/turn/finish") || method.endsWith("/execution/finish")) return { accepted: true, replayed: false }
      throw new Error(`unexpected callback ${method}`)
    },
  } as never)
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const rt = ManagedRuntime.make(layer)
    try {
      const session = await rt.runPromise(Session.Service.use((service) => service.create()))
      const registry = await rt.runPromise(ActorRegistry.Service.use((service) => Effect.succeed(service)))
      await rt.runPromise(registry.updateModel(session.id, "main", undefined, { providerID: "local", modelID: "main-model" }))
      await rt.runPromise(registry.updateHostContext(session.id, "main", { chatID: binding.stableChatID }))
      await rt.runPromise(registry.register({ sessionID: session.id, actorID: "child", mode: "subagent",
        agent: "build", description: "child", contextMode: "none", background: true, lifecycle: "persistent",
        effectiveModel: { providerID: "local", modelID: "child-model" }, hostContext: { chatID: binding.stableChatID } }))
      const preflight = async (actorID: string, modelID: string, expectedExecutionRevision: number, rootOperationID: string) =>
        ManagedProvider.preflightActorRoute({ sessionID: session.id, actorID, rootOperationID, expectedExecutionRevision,
          model: { providerID: "local", modelID }, modelSource: "explicit" })
      const mainRoute = await preflight("main", "main-model", 0, "root-main")
      expect(mainRoute.modelRequestOptions?.options).toEqual({})
      expect(mainRoute.bootstrap.modelRequestOptions).toEqual(mainRoute.modelRequestOptions)
      const mainRevision = (await rt.runPromise(registry.beginExecution(session.id, "main", undefined, 0)))!
      const mainIdentity = { sessionID: session.id, actorID: "main", executionRevision: mainRevision, rootOperationID: "root-main" }
      const main = await ManagedProvider.activateActorOperation(mainIdentity, mainRoute, { providerID: "local", modelID: "main-model" }, { chatID: binding.stableChatID })
      const candidate = { version: 1 as const, source: { kind: "parent_execution" as const, sessionID: session.id, actorID: "main", executionRevision: mainRevision, rootOperationID: "root-main", messageID: "parent-message" }, model: { providerID: "local", modelID: "main-model" }, variant: "high" }
      const optionPreflight = (modelID: string) => ManagedProvider.preflightActorRoute({ sessionID: session.id, actorID: "child", rootOperationID: "root-main", expectedExecutionRevision: 0, model: { providerID: "local", modelID }, modelSource: "explicit", optionCandidate: candidate })
      expect((await optionPreflight("main-model")).modelRequestOptions?.options.reasoning_effort).toBe("high")
      const childRoute = await optionPreflight("child-model")
      expect(childRoute.modelRequestOptions?.options).toEqual({})
      const childRevision = (await rt.runPromise(registry.beginExecution(session.id, "child", undefined, 0)))!
      expect(await rt.runPromise(registry.updateModelOptionsForExecution(session.id, "child", childRevision, childRoute.modelRequestOptions!))).toBe(true)
      const childIdentity = { sessionID: session.id, actorID: "child", executionRevision: childRevision, rootOperationID: "root-main" }
      const child = await ManagedProvider.activateActorOperation(childIdentity, childRoute, { providerID: "local", modelID: "child-model" }, { chatID: binding.stableChatID })
      expect(ManagedProvider.currentScope(session.id, "main", mainRevision)).toBe(main.scope)
      expect(ManagedProvider.currentScope(session.id, "child", childRevision)).toBe(child.scope)
      expect(ManagedProvider.currentScope(session.id, "child", childRevision + 1)).toBeUndefined()
      const childTurn = { ...childIdentity, innerTurnID: "child-turn-1" }
      await ManagedProvider.managedHostCall("_openclank/session/v1/turn/start", childTurn)
      expect(await rt.runPromise(registry.settleExecution(session.id, "main", mainRevision, "success"))).toBe(true)
      await main.release()
      const saved = (await rt.runPromise(registry.get(session.id, "child")))!.modelRequestOptions!
      const savedRoute = await ManagedProvider.preflightActorRoute({ sessionID: session.id, actorID: "child", rootOperationID: "root-main", expectedExecutionRevision: childRevision, model: { providerID: "local", modelID: "child-model" }, modelSource: "explicit", savedOptionSnapshot: saved })
      expect(savedRoute.modelRequestOptions).toEqual(saved)
      expect(ManagedProvider.currentScope(session.id, "child", childRevision)).toBe(child.scope)
      await ManagedProvider.managedHostCall("_openclank/session/v1/turn/finish", { ...childTurn, outcome: "completed" })
      const sessions = await rt.runPromise(Session.Service.use((service) => Effect.succeed(service)))
      const assistantID = MessageID.ascending()
      await rt.runPromise(sessions.updateMessage({ id: assistantID, sessionID: session.id, agentID: "child", role: "assistant",
        parentID: MessageID.ascending(), modelID: "child-model" as never, providerID: "local" as never, mode: "build", agent: "build",
        path: { cwd: tmp.path, root: tmp.path }, time: { created: Date.now() }, cost: 0,
        tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
        metadata: { managedExecution: childIdentity },
      }))
      expect(await rt.runPromise(persistManagedActorCapture(sessions, childIdentity))).toBe(true)
      expect(await rt.runPromise(registry.settleExecution(session.id, "child", childRevision, "success"))).toBe(true)
      await child.release()
      const captured = (await rt.runPromise(sessions.messages({ sessionID: session.id, agentID: "child" }))).find((item) => item.info.id === assistantID)?.info
      expect(captured?.role === "assistant" ? captured.metadata?.managedCapture?.binding?.modelID : undefined).toBe("child-model")

      const base = { ...binding, sessionID: session.id, actorID: "child", executionRevision: childRevision,
        attachmentRefs: [], questionRefs: [], mode: "steer" as const }
      UserInput.admit({ ...base, requestID: "receipt-a", text: "first", modelRequestOptions: { present: true, routeID: "route-child-model", optionRouteID: "route-child-model", catalogRevision: 1, model: { providerID: "local", modelID: "child-model" }, options: { reasoning_effort: "low" } } })
      UserInput.admit({ ...base, requestID: "receipt-b", text: "second", modelRequestOptions: { present: true, routeID: "route-child-model", optionRouteID: "route-child-model", catalogRevision: 1, model: { providerID: "local", modelID: "child-model" }, options: { reasoning_effort: "low" } } })
      UserInput.admit({ ...base, requestID: "receipt-c", text: "different options", modelRequestOptions: { present: true, routeID: "route-child-model", optionRouteID: "route-child-model", catalogRevision: 1, model: { providerID: "local", modelID: "child-model" }, options: { reasoning_effort: "high" } } })
      const frozen = UserInput.snapshotBoundary({ sessionID: session.id, actorID: "child", binding, afterTurnReady: true,
        prepared: { expectedExecutionRevision: childRevision, requestID: "receipt-a" } })
      expect(frozen.map((item) => item.requestID)).toEqual(["receipt-a", "receipt-b"])
      const resumeRoute = await preflight("child", "child-model", childRevision, "resume-root")
      const claim = await rt.runPromise(registry.beginExecutionForInput!(session.id, "child", {
        ...binding, requestID: "receipt-a", handoffID: "handoff", rootOperationID: "resume-root",
        expectedExecutionRevision: childRevision, expectedReceipts: frozen,
      }))
      expect(claim).toEqual({ kind: "fresh-claim", claimedExecutionRevision: childRevision + 1 })
      const resumedIdentity = { sessionID: session.id, actorID: "child", executionRevision: childRevision + 1, rootOperationID: "resume-root" }
      const resumed = await ManagedProvider.activateActorOperation(resumedIdentity, resumeRoute, { providerID: "local", modelID: "child-model" }, { chatID: binding.stableChatID })
      expect(await rt.runPromise(persistManagedActorCapture(sessions, resumedIdentity))).toBe(false)
      expect(await rt.runPromise(registry.settleExecution(session.id, "child", childRevision, "success"))).toBe(false)
      expect(ManagedProvider.currentScope(session.id, "child", childRevision + 1)).toBe(resumed.scope)
      expect(() => UserInput.consumeBoundary({ sessionID: session.id, actorID: "child", binding: { ...binding, mappingRevision: binding.mappingRevision + 1 }, afterTurnReady: true, expectedReceipts: frozen })).toThrow()
      expect(() => UserInput.consumeBoundary({ sessionID: session.id, actorID: "child", binding, afterTurnReady: true,
        expectedReceipts: [{ ...frozen[0]!, payloadHash: "wrong-hash" }, frozen[1]!] })).toThrow()
      expect(UserInput.consumeBoundary({ sessionID: session.id, actorID: "child", binding, afterTurnReady: true, expectedReceipts: frozen })).toBe(2)
      expect((await rt.runPromise(registry.get(session.id, "child")))?.modelRequestOptions?.source).toEqual({ kind: "receipt", requestID: "receipt-b" })
      expect(UserInput.exportReceipts({ owner: binding.owner, stableChatID: binding.stableChatID, sessionID: session.id, actorID: "child" }).find((item) => item.request_id === "receipt-c")?.state).toBe("persisted")
      const pending = { rootOperationID: "resume-root", innerTurnID: "resume-turn" }
      expect(await rt.runPromise(registry.settleExecution(session.id, "child", childRevision + 1, "success", undefined, pending))).toBe(true)
      expect(await rt.runPromise(registry.beginExecution(session.id, "child", undefined, childRevision + 1))).toBeUndefined()
      expect(await rt.runPromise(registry.clearManagedSettlement(session.id, "child", childRevision + 1, { ...pending, rootOperationID: "wrong" }))).toBe(false)
      expect(await rt.runPromise(registry.clearManagedSettlement(session.id, "child", childRevision + 1, pending))).toBe(true)
      await resumed.release()
      const staleRoute = await preflight("child", "child-model", childRevision + 1, "revoked-root")
      revokeGrant = true
      await expect(ManagedProvider.activateActorOperation({ sessionID: session.id, actorID: "child", executionRevision: childRevision + 2, rootOperationID: "revoked-root" },
        staleRoute, { providerID: "local", modelID: "child-model" }, { chatID: binding.stableChatID })).rejects.toThrow("grant revoked")
      expect(ManagedProvider.currentScope(session.id, "child", childRevision + 2)).toBeUndefined()
      const uncertainRevision = (await rt.runPromise(registry.beginExecution(session.id, "child", undefined, childRevision + 1)))!
      expect(await rt.runPromise(registry.markExecutionUncertain(session.id, "child", uncertainRevision,
        { rootOperationID: "uncertain-root", innerTurnID: "uncertain-turn" }))).toBe(true)
      expect(await rt.runPromise(registry.beginExecution(session.id, "child", undefined, uncertainRevision))).toBeUndefined()
      expect((await rt.runPromise(registry.get(session.id, "child")))?.lastError).toContain("requires reconciliation")
      await expect(rt.runPromise(reconcileManagedActorExecution(registry, session.id, "child", Effect.void, () => Effect.succeed(false)))).rejects.toThrow("outcome remains unknown")
      expect((await rt.runPromise(registry.get(session.id, "child")))?.status).toBe("running")
      turnStatus = "active"
      await rt.runPromise(reconcileManagedActorExecution(registry, session.id, "child", Effect.void, () => Effect.succeed(false)))
      expect((await rt.runPromise(registry.get(session.id, "child")))?.status).toBe("idle")
      expect(calls.indexOf("_openclank/provider-store/v1/actor-route/preflight")).toBeLessThan(calls.indexOf("_openclank/provider-store/v1/account/bind"))
    } finally { await rt.dispose() }
  } })
})
