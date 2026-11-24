import { afterEach, expect, test } from "bun:test"
import { Effect, Fiber } from "effect"
import path from "node:path"
import { textStopResponse } from "../lib/scripted-llm-server"

let disposeManaged: (() => void) | undefined

afterEach(async () => {
  disposeManaged?.()
  disposeManaged = undefined
  delete process.env.OPEN_CLANK_MANAGED
  const { Instance } = await import("../../src/project/instance")
  await Instance.disposeAll()
})

test("main idle receipt resumes with a fresh managed scope after ACP scope ended", async () => {
  await using tmp = await (await import("../fixture/fixture")).tmpdir({ git: true })
  const phase = { value: "test-bootstrap" }
  const requests: string[] = []
  const captures: Array<{ model: string; messages: unknown[] }> = []
  let unblockInitial!: () => void
  let unblockSteer!: () => void
  let unblockFollowup!: () => void
  let unblockCancel!: () => void
  const initialDelayed = new Promise<void>((resolve) => { unblockInitial = resolve })
  const steerDelayed = new Promise<void>((resolve) => { unblockSteer = resolve })
  const followupDelayed = new Promise<void>((resolve) => { unblockFollowup = resolve })
  const cancelDelayed = new Promise<void>((resolve) => { unblockCancel = resolve })
  let responseIndex = 0
  let lateReceipt: ReturnType<typeof import("../../src/session/user-input").admit> | undefined
  let lateOriginTurnID: string | undefined
  const responses = [textStopResponse("first turn"), textStopResponse("active steer delivered"), textStopResponse("receipt delivered")]
  const stub = Bun.serve({
    port: 0,
    async fetch(request) {
      const url = new URL(request.url)
      requests.push(url.pathname)
      if (!url.pathname.endsWith("/chat/completions")) return new Response("unexpected local test path", { status: 404 })
      const body = await request.json() as { model: string; messages: unknown[] }
      captures.push({ model: body.model, messages: body.messages })
      const requestIndex = responseIndex++
      const templateIndex = Math.min(requestIndex, responses.length - 1)
      if (requestIndex === 0) await initialDelayed
      if (requestIndex === 1) await steerDelayed
      if (requestIndex === 2) await followupDelayed
      if (requestIndex === 3) await cancelDelayed
      const encoder = new TextEncoder()
      const stream = new ReadableStream<Uint8Array>({ start(controller) {
        for (const line of responses[templateIndex]) controller.enqueue(encoder.encode(line))
        controller.close()
      } })
      return new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } })
    },
  })
  const localBaseURL = new URL(stub.url).origin
  const previous = process.env.OPEN_CLANK_MANAGED
  const previousOwner = process.env.OPEN_CLANK_OWNER
  const previousWorkspace = process.env.FM_WORKSPACE_ID
  process.env.OPEN_CLANK_MANAGED = "1"
  process.env.OPEN_CLANK_OWNER = "owner-main"
  process.env.FM_WORKSPACE_ID = "memory-main"
  try {
    const [{ Actor }, { ActorRegistry }, { AppRuntime }, { Instance }, { Session }, { SessionPrompt }, UserInput, { ManagedProvider }, memory, { Log }, { Provider }] = await Promise.all([
      import("../../src/actor/spawn"), import("../../src/actor/registry"), import("../../src/effect/app-runtime"),
      import("../../src/project/instance"), import("../../src/session"), import("../../src/session/prompt"),
      import("../../src/session/user-input"), import("../../src/acp/managed-provider"), import("../../src/memory/session-scope"), import("../../src/util"), import("../../src/provider"),
    ])
    void Log.init({ print: false })
    disposeManaged = () => ManagedProvider.resetForTest()

    await Bun.write(path.join(tmp.path, "mimocode.json"), JSON.stringify({
      $schema: "https://opencode.ai/config.json",
      enabled_providers: ["local-openai"],
      provider: { "local-openai": {
        npm: "@ai-sdk/openai-compatible",
        api: `${localBaseURL}/v1`,
        env: [],
        options: {
          apiKey: "local-test", _openclankFamilyID: "openai-compatible",
          _openclankAdapterID: "openai-chat", _openclankBillingLane: "local", _openclankConnectionID: "local-openai",
        },
        models: { "qwen-local": {
        name: "qwen-local", options: { _openclankAdapterID: "openai-chat" },
        limit: { context: 8192, output: 1024 }, modalities: { input: ["text"], output: ["text"] },
      } } } },
      agent: { build: { model: "local-openai/qwen-local" } },
    }))

    const initialRoute = {
      rootOperationID: "root-initial", connectionID: "local-openai", providerID: "local-openai",
      billingLane: "local" as const, modelID: "qwen-local", modelRouteID: "route-qwen-local",
    }
    const bindingRead = (sessionID: string) => ({
      engineSessionID: sessionID, stableChatID: "chat-main", owner: "owner-main", canonicalCwd: tmp.path,
      workspaceRevision: 0, authorityWorkspaceID: "workspace-main", memoryWorkspaceID: "memory-main",
      copalWorkspace: "workspace-main", memoryEnabled: false, engineAliases: [], mapRevision: 1, mappingRevision: 1,
    })
    ManagedProvider.installHostConnection({
      async extMethod(method: string, params: Record<string, unknown>) {
        if (method.endsWith("/account/bind")) return {
          bindingID: `binding-${params.rootOperationID}`, bindingRevision: 1, rootOperationID: params.rootOperationID,
          connectionID: "local-openai", providerID: "local-openai", billingLane: "local", modelID: "qwen-local",
          credentialRequired: false, source: "keyless", attempt: 1, committed: false,
        }
        if (method.endsWith("/logging/admit")) return { bindingID: params.bindingID, rootOperationID: params.rootOperationID }
        if (method.endsWith("/account/attempt")) {
          phase.value = `provider-attempt:${String(params.outcome)}`
          return {
          bindingID: params.bindingID, bindingRevision: Number(params.expectedRevision) + 1,
          rootOperationID: String(params.bindingID).replace(/^binding-/, ""), connectionID: "local-openai", providerID: "local-openai",
          billingLane: "local", modelID: "qwen-local", credentialRequired: false, source: "keyless",
          attempt: 2, committed: false,
          }
        }
        if (method.endsWith("/session/v1/binding/read")) return bindingRead(String(params.sessionID))
        throw new Error(`unexpected managed callback: ${method}`)
      },
    } as any)

    await Instance.provide({ directory: tmp.path, fn: async () => {
      try {
        await AppRuntime.runPromise(Effect.scoped(Effect.gen(function* () {
          const sessions = yield* Session.Service
          const prompt = yield* SessionPrompt.Service
          const actor = yield* Actor.Service
          const registry = yield* ActorRegistry.Service
          const provider = yield* Provider.Service
          const session = yield* sessions.create({ title: "managed idle handoff" })
          const settleExecution = registry.settleExecution
          ;(registry as any).settleExecution = (sessionID: string, actorID: string, executionRevision: number, outcome: string, lastError?: string) => {
            if (sessionID === session.id && actorID === "main" && outcome === "success" && !lateReceipt) {
              if (!lateOriginTurnID) throw new Error("late receipt origin turn was not captured before main settlement")
              lateReceipt = UserInput.admit({
                owner: "owner-main", stableChatID: "chat-main", mapRevision: 1, mappingRevision: 1, workspaceRevision: 0,
                requestID: "request-main-final-gate", sessionID: session.id, actorID: "main", executionRevision,
                originTurnID: lateOriginTurnID, text: "arrived after the final delivery gate", attachmentRefs: [], questionRefs: [], mode: "after-turn",
              })
              phase.value = "receipt-admitted-after-final-gate"
            }
            return settleExecution(sessionID as never, actorID, executionRevision, outcome as never, lastError)
          }
          phase.value = "created-session"
          memory.registerMemorySessionScope(session.id, [{
            name: "lifetools",
            env: [
              { name: "FM_OWNER", value: "owner-main" }, { name: "FM_WORKSPACE_ID", value: "memory-main" },
              { name: "SESSION_ID", value: "chat-main" }, { name: "OPEN_CLANK_AUTHORITY_WORKSPACE_ID", value: "workspace-main" },
              { name: "WORKSPACE", value: tmp.path }, { name: "FM_MEMORY_ENABLED", value: "0" },
              { name: "OPEN_CLANK_SESSION_BINDING_REVISION", value: "0" }, { name: "OPEN_CLANK_SESSION_MAP_REVISION", value: "1" },
              { name: "OPEN_CLANK_SESSION_MAPPING_REVISION", value: "1" }, { name: "COPAL_WORKSPACE", value: "workspace-main" },
              { name: "OPEN_CLANK_ENGINE_SESSION_ALIASES", value: "[]" },
            ],
          } as any], tmp.path)
          phase.value = "entered-acp-scope"
          yield* Effect.tryPromise({
            try: () => ManagedProvider.withHostContext(session.id, {
              chatID: "chat-main", workspaceID: "workspace-main", cwd: tmp.path,
            }, () => ManagedProvider.withOperation(session.id, initialRoute, { providerID: "local-openai", modelID: "qwen-local" }, () =>
              AppRuntime.runPromise(Effect.scoped(Effect.gen(function* () {
                const promptFiber = yield* prompt.prompt({ sessionID: session.id, agent: "build", parts: [{ type: "text", text: "initial turn" }] }).pipe(Effect.forkScoped)
                phase.value = "initial-provider-call-held"
                const firstCaptureDeadline = Date.now() + 12000
                while (captures.length < 1 && Date.now() < firstCaptureDeadline) yield* Effect.sleep("20 millis")
                if (captures.length < 1) throw new Error(`initial provider call was not captured; paths=${requests.join(",")}`)
                const activeActor = yield* registry.get(session.id, "main")
                const activeMessages = yield* sessions.messages({ sessionID: session.id, agentID: "main" })
                const activeOrigin = activeMessages.findLast((message) => message.info.role === "user")
                if (!activeActor || activeActor.status !== "running" || activeOrigin?.info.role !== "user") {
                  throw new Error(`main actor was not running at active-input boundary: ${activeActor?.status}`)
                }
                UserInput.admit({
                  owner: "owner-main", stableChatID: "chat-main", mapRevision: 1, mappingRevision: 1, workspaceRevision: 0,
                  requestID: "request-main-steer", sessionID: session.id, actorID: "main", executionRevision: activeActor.executionRevision,
                  originTurnID: activeOrigin.info.id, text: "steer while running", attachmentRefs: [], questionRefs: [], mode: "steer",
                })
                phase.value = "active-steer-admitted"
                unblockInitial()
                const nextCaptureDeadline = Date.now() + 12000
                while (captures.length < 2 && Date.now() < nextCaptureDeadline) yield* Effect.sleep("20 millis")
                if (captures.length < 2) throw new Error(`active steer did not reach the next prompt boundary; phase=${phase.value}`)
                const activeRequest = JSON.stringify(captures[1].messages)
                if (!activeRequest.includes("steer while running")) throw new Error("active steer was not included in the next provider request")
                phase.value = "active-steer-delivered"
                const messagesAtSteer = yield* sessions.messages({ sessionID: session.id, agentID: "main" })
                const latestAtSteer = messagesAtSteer.findLast((message) => message.info.role === "user")
                if (latestAtSteer?.info.role !== "user") throw new Error("active steer user message was not persisted")
                lateOriginTurnID = latestAtSteer.info.id
                unblockSteer()
                yield* Fiber.join(promptFiber)
              }))))),
            catch: (error) => error as Error,
          })
          phase.value = "settled-first-turn"
          expect(ManagedProvider.currentScope(session.id)).toBeUndefined()
          phase.value = "initial-operation-scope-ended"
          const actorRow = yield* registry.get(session.id, "main")
          const messages = yield* sessions.messages({ sessionID: session.id, agentID: "main" })
          if (captures.length !== 2) {
            const selected = yield* provider.getModel("local-openai" as never, "qwen-local" as never)
            throw new Error(`initial prompt/active steer did not reach local LLM twice; api=${selected.api.url}; npm=${selected.api.npm}; paths=${requests.join(",")}; actor=${actorRow?.status}; messages=${messages.map((message) => `${message.info.role}:${message.parts.map((part) => part.type === "text" ? part.text : part.type).join("|")}`).join(" || ")}`)
          }
          expect(actorRow?.status).toBe("idle")
          const latestUser = messages.findLast((message) => message.info.role === "user")
          if (!actorRow || !latestUser || latestUser.info.role !== "user" || !lateReceipt) throw new Error("main actor turn or post-gate receipt evidence is unavailable")
          expect(lateReceipt.state).toBe("persisted")
          const afterGate = UserInput.exportReceipts({ owner: "owner-main", stableChatID: "chat-main", sessionID: session.id, actorID: "main" })
          expect(afterGate.find((receipt) => receipt.request_id === lateReceipt?.request_id)?.state).toBe("persisted")
          phase.value = "persisted-after-final-gate"
          if (!actor.resumeInput) throw new Error("main actor input resume is unavailable")
          const result = yield* actor.resumeInput({
            sessionID: session.id, actorID: "main",
            claim: {
              owner: "owner-main", stableChatID: "chat-main", mapRevision: 1, mappingRevision: 1, workspaceRevision: 0,
              requestID: lateReceipt.request_id, handoffID: "handoff-main-1", rootOperationID: "root-main-followup",
              expectedExecutionRevision: actorRow.executionRevision,
            },
            managedRoute: { ...initialRoute, rootOperationID: "root-main-followup" },
          })
          expect(result.kind).toBe("fresh-claim")
          phase.value = "handoff-response"
          const captureDeadline = Date.now() + 12000
          while (captures.length < 3 && Date.now() < captureDeadline) yield* Effect.sleep("20 millis")
          if (captures.length < 3) throw new Error(`local LLM request not captured; paths=${requests.join(",")}; phase=${phase.value}`)
          phase.value = "captured-after-response"
          expect(ManagedProvider.currentScope(session.id)).toBeDefined()
          unblockFollowup()
          const settleDeadline = Date.now() + 12000
          let status = yield* registry.get(session.id, "main")
          while (status?.status === "running" && Date.now() < settleDeadline) {
            yield* Effect.sleep("20 millis")
            status = yield* registry.get(session.id, "main")
          }
          phase.value = "settled-followup"
          expect(status?.status).toBe("idle")
          const after = yield* sessions.messages({ sessionID: session.id, agentID: "main" })
          const users = after.filter((message) => message.info.role === "user")
          expect(users).toHaveLength(3)
          expect(users.filter((message) => message.parts.some((part) => part.type === "text" && part.text === "arrived after the final delivery gate"))).toHaveLength(1)
          expect(users.filter((message) => message.parts.some((part) => part.type === "text" && part.text === "steer while running"))).toHaveLength(1)
          expect(ManagedProvider.currentScope(session.id)).toBeUndefined()

          const idleActor = yield* registry.get(session.id, "main")
          const latestMessage = after.findLast((message) => message.info.role === "user")
          const latestTurnID = latestMessage?.info.role === "user" ? latestMessage.info.id : undefined
          if (!idleActor || !latestTurnID || !actor.resumeInput) throw new Error("main actor evidence for cancellation handoff is unavailable")
          const cancelReceipt = UserInput.admit({
            owner: "owner-main", stableChatID: "chat-main", mapRevision: 1, mappingRevision: 1, workspaceRevision: 0,
            requestID: "request-main-cancel", sessionID: session.id, actorID: "main", executionRevision: idleActor.executionRevision,
            originTurnID: latestTurnID, text: "cancel this continuation", attachmentRefs: [], questionRefs: [], mode: "after-turn",
          })
          const cancelResult = yield* actor.resumeInput({
            sessionID: session.id, actorID: "main",
            claim: {
              owner: "owner-main", stableChatID: "chat-main", mapRevision: 1, mappingRevision: 1, workspaceRevision: 0,
              requestID: cancelReceipt.request_id, handoffID: "handoff-main-cancel", rootOperationID: "root-main-cancel",
              expectedExecutionRevision: idleActor.executionRevision,
            },
            managedRoute: { ...initialRoute, rootOperationID: "root-main-cancel" },
          })
          expect(cancelResult.kind).toBe("fresh-claim")
          const cancelCaptureDeadline = Date.now() + 12000
          while (captures.length < 4 && Date.now() < cancelCaptureDeadline) yield* Effect.sleep("20 millis")
          if (captures.length < 4) throw new Error(`cancel continuation did not reach local provider; phase=${phase.value}`)
          expect(ManagedProvider.currentScope(session.id)).toBeDefined()
          phase.value = "cancel-requested"
          const cancelFiber = yield* actor.cancel(session.id, "main", "forced").pipe(Effect.forkScoped)
          const cancelDeadline = Date.now() + 5000
          let cancelled = yield* registry.get(session.id, "main")
          while (cancelled?.lastOutcome !== "cancelled" && Date.now() < cancelDeadline) {
            yield* Effect.sleep("20 millis")
            cancelled = yield* registry.get(session.id, "main")
          }
          expect(cancelled?.lastOutcome).toBe("cancelled")
          const scopeDeadline = Date.now() + 5000
          while (ManagedProvider.currentScope(session.id) && Date.now() < scopeDeadline) yield* Effect.sleep("20 millis")
          phase.value = ManagedProvider.currentScope(session.id) ? "cancel-operation-scope-still-active" : "cancel-operation-scope-released"
          expect(ManagedProvider.currentScope(session.id)).toBeUndefined()
          expect(ManagedProvider.operationScopeAvailable(session.id)).toBe(true)
          yield* Fiber.join(cancelFiber).pipe(Effect.timeout("5 seconds"))
          yield* Effect.sleep("100 millis")
          expect(captures).toHaveLength(4)
          memory.unregisterMemorySessionScope(session.id)
        })))
      } finally {
        unblockInitial()
        unblockSteer()
        unblockFollowup()
        unblockCancel()
        await AppRuntime.dispose()
      }
    } })
  } catch (error) {
    throw new Error(`${String(error)} (last phase: ${phase.value})`)
  } finally {
    unblockInitial()
    unblockSteer()
    unblockFollowup()
    unblockCancel()
    if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = previous
    if (previousOwner === undefined) delete process.env.OPEN_CLANK_OWNER
    else process.env.OPEN_CLANK_OWNER = previousOwner
    if (previousWorkspace === undefined) delete process.env.FM_WORKSPACE_ID
    else process.env.FM_WORKSPACE_ID = previousWorkspace
    await stub.stop(true)
  }
})
