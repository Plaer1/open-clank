import { NodeFileSystem } from "@effect/platform-node"
import { FetchHttpClient } from "effect/unstable/http"
import { afterEach, describe, expect } from "bun:test"
import { Deferred, Effect, Fiber, Layer } from "effect"
import { and, eq } from "drizzle-orm"
import { Agent as AgentSvc } from "../../src/agent/agent"
import { Bus } from "../../src/bus"
import { Command } from "../../src/command"
import { Config } from "../../src/config"
import { LSP } from "../../src/lsp"
import { MCP } from "../../src/mcp"
import { Permission } from "../../src/permission"
import { Plugin } from "../../src/plugin"
import { Provider as ProviderSvc } from "../../src/provider"
import { Env } from "../../src/env"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { Question } from "../../src/question"
import { Todo } from "../../src/session/todo"
import { Session } from "../../src/session"
import { LLM } from "../../src/session/llm"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"
import { SessionPrune } from "../../src/session/prune"
import { SessionSummary } from "../../src/session/summary"
import { Instruction } from "../../src/session/instruction"
import { SessionProcessor } from "../../src/session/processor"
import { SessionPrompt } from "../../src/session/prompt"
import { defaultLayer as CronBridgeDefaultLayer } from "../../src/session/cron-bridge"
import { SessionRevert } from "../../src/session/revert"
import { SessionRunState } from "../../src/session/run-state"
import { Goal } from "../../src/session/goal"
import { SessionStatus } from "../../src/session/status"
import { Skill } from "../../src/skill"
import { SystemPrompt } from "../../src/session/system"
import { Snapshot } from "../../src/snapshot"
import { ToolRegistry } from "../../src/tool"
import { Truncate } from "../../src/tool"
import { ActorTool } from "../../src/tool/actor"
import { ActorRegistry } from "../../src/actor/registry"
import { ActorForkContextTable } from "../../src/actor/actor.sql"
import { UserInputReceiptTable } from "../../src/inbox/inbox.sql"
import { ActorWaiter } from "../../src/actor/waiter"
import { Actor } from "../../src/actor/spawn"
import { Worktree } from "../../src/worktree"
import { Memory } from "../../src/memory"
import { History } from "../../src/history"
import { Team } from "../../src/team"
import { SessionCheckpoint } from "../../src/session/checkpoint"
import { SessionCompaction } from "../../src/session/compaction"
import { TaskRegistry } from "../../src/task/registry"
import { defaultLayer as SchedulerDefaultLayer } from "../../src/cron/scheduler"
import { Auth } from "../../src/auth"
import { Database } from "../../src/storage"
import { MessageTable, PartTable } from "../../src/session/session.sql"
import { InboxTable } from "../../src/inbox/inbox.sql"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID, type SessionID } from "../../src/session/schema"
import { SessionReminder } from "../../src/session/reminder"
import { Instance } from "../../src/project/instance"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Ripgrep } from "../../src/file/ripgrep"
import { Format } from "../../src/format"
import { provideTmpdirServer } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { TestLLMServer } from "../lib/llm-server"
import { reply } from "../lib/llm-server"
import { Inbox, inboxControllerRef } from "../../src/inbox"
import * as UserInput from "../../src/session/user-input"
import { inboxServiceRef } from "../../src/inbox/inbox-ref"

afterEach(async () => {
  await Instance.disposeAll()
})

const summary = Layer.succeed(
  SessionSummary.Service,
  SessionSummary.Service.of({
    summarize: () => Effect.void,
    diff: () => Effect.succeed([]),
    diffResult: () => Effect.succeed({ diffs: [] }),
    computeDiff: () => Effect.succeed([]),
  }),
)

const mcp = Layer.succeed(
  MCP.Service,
  MCP.Service.of({
    status: () => Effect.succeed({}),
    clients: () => Effect.succeed({}),
    tools: () => Effect.succeed({}),
    prompts: () => Effect.succeed({}),
    resources: () => Effect.succeed({}),
    add: () => Effect.succeed({ status: { status: "disabled" as const } }),
    connect: () => Effect.void,
    disconnect: () => Effect.void,
    getPrompt: () => Effect.succeed(undefined),
    readResource: () => Effect.succeed(undefined),
    startAuth: () => Effect.die("unexpected MCP auth in spawn tests"),
    authenticate: () => Effect.die("unexpected MCP auth in spawn tests"),
    finishAuth: () => Effect.die("unexpected MCP auth in spawn tests"),
    removeAuth: () => Effect.void,
    supportsOAuth: () => Effect.succeed(false),
    hasStoredTokens: () => Effect.succeed(false),
    getAuthStatus: () => Effect.succeed("not_authenticated" as const),
  }),
)

const lsp = Layer.succeed(
  LSP.Service,
  LSP.Service.of({
    init: () => Effect.void,
    status: () => Effect.succeed([]),
    hasClients: () => Effect.succeed(false),
    touchFile: () => Effect.void,
    diagnostics: () => Effect.succeed({}),
    hover: () => Effect.succeed(undefined),
    definition: () => Effect.succeed([]),
    references: () => Effect.succeed([]),
    implementation: () => Effect.succeed([]),
    documentSymbol: () => Effect.succeed([]),
    workspaceSymbol: () => Effect.succeed([]),
    prepareCallHierarchy: () => Effect.succeed([]),
    incomingCalls: () => Effect.succeed([]),
    outgoingCalls: () => Effect.succeed([]),
  }),
)

const status = SessionStatus.layer.pipe(Layer.provideMerge(Bus.layer))
// Share ONE SessionStatus instance with SessionRunState (provideMerge exports it).
// A second bare `status` in deps would be a separate InstanceState and hide busy/idle.
const run = SessionRunState.layer.pipe(Layer.provideMerge(status), Layer.provide(ActorRegistry.defaultLayer))
const infra = Layer.mergeAll(NodeFileSystem.layer, CrossSpawnSpawner.defaultLayer)

function makeLayer(opts?: {
  settledError?: () => NonNullable<MessageV2.Assistant["error"]> | undefined
  runLayer?: Layer.Layer<SessionRunState.Service>
}) {
  const deps = Layer.mergeAll(
    Session.defaultLayer,
    Snapshot.defaultLayer,
    LLM.defaultLayer,
    Env.defaultLayer,
    AgentSvc.defaultLayer,
    Command.defaultLayer,
    Permission.defaultLayer,
    Plugin.defaultLayer,
    Config.defaultLayer,
    ProviderSvc.defaultLayer,
    lsp,
    mcp,
    AppFileSystem.defaultLayer,
    run,
  ).pipe(Layer.provideMerge(infra))
  const question = Question.layer.pipe(Layer.provideMerge(deps))
  const todo = Todo.layer.pipe(Layer.provideMerge(deps))
  const checkpoint = SessionCheckpoint.defaultLayer
  const taskRegistry = ActorRegistry.defaultLayer
  const taskStore = TaskRegistry.defaultLayer
  const taskWaiter = ActorWaiter.defaultLayer
  const team = Team.defaultLayer
  const registry = ToolRegistry.layer.pipe(
    Layer.provide(Skill.defaultLayer),
    Layer.provide(FetchHttpClient.layer),
    Layer.provide(CrossSpawnSpawner.defaultLayer),
    Layer.provide(Ripgrep.defaultLayer),
    Layer.provide(Format.defaultLayer),
    Layer.provide(taskRegistry),
    // ActorTool resolves its waiter when init() runs, so preserve this
    // service in the outer fixture alongside the registry wiring.
    Layer.provideMerge(taskWaiter),
    Layer.provide(team),
    // Keep the checkpoint service in the test environment as well as wiring it
    // into ToolRegistry: ActorTool resolves it when init() runs, after the
    // registry layer has already been built.
    Layer.provideMerge(checkpoint),
    Layer.provide(Memory.defaultLayer),
    Layer.provide(History.defaultLayer),
    Layer.provide(taskStore),
    Layer.provide(SchedulerDefaultLayer),
    Layer.provide(Auth.defaultLayer),
    Layer.provideMerge(todo),
    Layer.provideMerge(question),
    Layer.provideMerge(deps),
  )
  const trunc = Truncate.layer.pipe(Layer.provideMerge(deps))
  const proc = SessionProcessor.layer.pipe(Layer.provide(summary), Layer.provideMerge(deps))
  const prune = SessionPrune.layer.pipe(Layer.provide(checkpoint), Layer.provideMerge(deps))
  const prompt = SessionPrompt.layer.pipe(
    Layer.provide(CronBridgeDefaultLayer),
    Layer.provide(Goal.defaultLayer),
    Layer.provide(SessionRevert.defaultLayer),
    Layer.provide(summary),
    Layer.provide(checkpoint),
    Layer.provide(SessionCompaction.defaultLayer),
    Layer.provide(team),
    Layer.provide(taskRegistry),
    Layer.provideMerge(opts?.runLayer ?? run),
    Layer.provideMerge(prune),
    Layer.provideMerge(proc),
    Layer.provideMerge(registry),
    Layer.provideMerge(trunc),
    Layer.provide(Instruction.defaultLayer),
    Layer.provide(SystemPrompt.defaultLayer),
    Layer.provide(Inbox.defaultLayer),
    Layer.provideMerge(deps),
  )
  return Layer.mergeAll(
    TestLLMServer.layer,
    taskStore,
    Actor.layer.pipe(
      Layer.provideMerge(opts?.settledError ? settledPromptLayer(opts.settledError, prompt) : prompt),
      Layer.provide(Worktree.defaultLayer),
      Layer.provideMerge(taskRegistry),
      Layer.provide(taskStore),
    Layer.provide(SchedulerDefaultLayer),
      Layer.provide(Inbox.defaultLayer),
    ),
  ).pipe(Layer.provide(summary))
}

/**
 * Failure-classification tests only. Replaces SessionPrompt.prompt so the child's
 * turn settles with a CHOSEN assistant error, then hands that to the real spawn
 * machinery. Everything under test downstream of this point is production code:
 * runAgentLoop's classify call, the failure carrier, Cause.squash in onFailure and
 * the AgentOutcome assembly. Only the upstream provider round-trip is stood in for,
 * which is what makes a transient case testable at all — SessionRetry's ladder is
 * uncapped, so a genuinely retryable provider error never settles on its own.
 */
function settledPromptLayer<A, E, R>(
  settledError: () => NonNullable<MessageV2.Assistant["error"]> | undefined,
  real: Layer.Layer<A, E, R>,
) {
  return Layer.effect(
    SessionPrompt.Service,
    Effect.gen(function* () {
      const inner = yield* SessionPrompt.Service
      return SessionPrompt.Service.of({
        ...inner,
        prompt: (input) => {
          const settled = settledError()
          // Truthiness, not `!== undefined` — see AGENTS.md "Reading a nullable column".
          if (!settled) return inner.prompt(input)
          const info: MessageV2.Assistant = {
            id: MessageID.ascending(),
            sessionID: input.sessionID,
            agentID: input.agentID,
            role: "assistant",
            time: { created: Date.now(), completed: Date.now() },
            error: settled,
            parentID: MessageID.ascending(),
            modelID: ModelID.make("test-model"),
            providerID: ProviderID.make("test"),
            mode: "build",
            agent: input.agent ?? "build",
            path: { cwd: process.cwd(), root: process.cwd() },
            cost: 0,
            tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } },
          }
          return Effect.succeed({ info, parts: [] } satisfies MessageV2.WithParts)
        },
      })
    }),
  ).pipe(Layer.provideMerge(real))
}

const postStopGate: { target?: string; held?: Deferred.Deferred<void>; release?: Deferred.Deferred<void> } = {}
const cancelGate: { target?: string; entered?: Deferred.Deferred<void>; release?: Deferred.Deferred<void> } = {}

function makeGenerationGateLayer() {
  const ensureRunningCounts = new Map<string, number>()
  const baseRun = SessionRunState.layer.pipe(Layer.provide(status), Layer.provide(ActorRegistry.defaultLayer))
  const gatedRun = Layer.effect(
    SessionRunState.Service,
    Effect.gen(function* () {
      const base = yield* SessionRunState.Service
      return SessionRunState.Service.of({
        assertNotBusy: base.assertNotBusy,
        cancel: base.cancel,
        cancelActor: (sessionID, actorID) =>
          cancelGate.target === `${sessionID}:${actorID}` && cancelGate.release
            ? Effect.gen(function* () {
                if (cancelGate.entered) yield* Deferred.succeed(cancelGate.entered, undefined)
                yield* Deferred.await(cancelGate.release!)
                yield* base.cancelActor(sessionID, actorID)
              })
            : base.cancelActor(sessionID, actorID),
        startShell: base.startShell,
        start: base.start,
        startAndWait: base.startAndWait,
        ensureRunning: (sessionID, actorID, onInterrupt, work) => {
          const key = `${sessionID}:${actorID}`
          const count = (ensureRunningCounts.get(key) ?? 0) + 1
          ensureRunningCounts.set(key, count)
          const started = base.ensureRunning(sessionID, actorID, onInterrupt, work)
          return postStopGate.target === key && count === 2 && postStopGate.held && postStopGate.release
            ? started.pipe(
                Effect.ensuring(
                  Effect.gen(function* () {
                    yield* Deferred.succeed(postStopGate.held!, undefined).pipe(Effect.ignore)
                    yield* Effect.uninterruptible(Deferred.await(postStopGate.release!))
                  }),
                ),
              )
            : started
        },
      })
    }),
  ).pipe(Layer.provide(baseRun))
  return makeLayer({ runLayer: gatedRun })
}

const it = testEffect(makeGenerationGateLayer())

describe("Actor execution generation retirement", () => {
  it.live("holds same-ID resume until the settled postStop runner is released", () =>
    Effect.gen(function* () {
      const held = yield* Deferred.make<void>()
      const release = yield* Deferred.make<void>()
      postStopGate.held = held
      postStopGate.release = release
      yield* Effect.addFinalizer(() =>
        postStopGate.release
          ? Deferred.succeed(postStopGate.release, undefined).pipe(
              Effect.ignore,
              Effect.ensuring(
                Effect.sync(() => {
                  postStopGate.target = undefined
                  postStopGate.held = undefined
                  postStopGate.release = undefined
                }),
              ),
            )
          : Effect.sync(() => {
              postStopGate.target = undefined
              postStopGate.held = undefined
            }),
      )
      return yield* provideTmpdirServer(
        Effect.fnUntraced(function* ({ llm }) {
          const actor = yield* Actor.Service
          const session = yield* Session.Service
          const registry = yield* ActorRegistry.Service
          const tasks = yield* TaskRegistry.Service
          const parent = yield* session.create({ title: "postStop generation gate" })
          const task = yield* tasks.create({ session_id: parent.id, summary: "already done" })
          yield* tasks.done({ session_id: parent.id, id: task.id })
          yield* llm.text("first")
          yield* llm.text("postStop")

          const first = yield* actor.spawn({
            mode: "subagent",
            sessionID: parent.id,
            agentType: "general",
            task: "first generation",
            context: "none",
            tools: ["read"],
            background: true,
            model: ref,
            task_id: task.id,
          })
          postStopGate.target = `${parent.id}:${first.actorID}`
          yield* llm.wait(2)
          const firstOutcome = yield* Deferred.await(first.outcome)
          expect(firstOutcome.status).toBe("success")
          const firstRow = yield* registry.get(parent.id, first.actorID)
          expect(firstRow?.status).toBe("idle")
          yield* Deferred.await(held)

          yield* llm.text("second")
          const resume = yield* Effect.forkChild(
            actor.spawn({
              mode: "subagent",
              sessionID: parent.id,
              actorID: first.actorID,
              agentType: "explore",
              task: "second generation",
              context: "none",
              tools: ["read"],
              background: false,
              model: ref,
            }),
          )
          yield* Effect.sleep("100 millis")
          const parked = yield* registry.get(parent.id, first.actorID)
          expect(parked?.executionRevision).toBe(firstRow?.executionRevision)
          expect(yield* llm.calls).toBe(2)

          yield* Deferred.succeed(release, undefined)
          const second = yield* Fiber.join(resume)
          expect((yield* Deferred.await(second.outcome)).status).toBe("success")
          const finalRow = yield* registry.get(parent.id, first.actorID)
          expect(finalRow?.executionRevision).toBeGreaterThan(firstRow?.executionRevision ?? 0)
          expect(yield* llm.calls).toBe(3)
        }),
        { git: true, config: providerCfg },
      )
    }),
  )

  it.live("keeps same-ID resume behind timed-out cancellation cleanup", () =>
    Effect.gen(function* () {
      const entered = yield* Deferred.make<void>()
      const release = yield* Deferred.make<void>()
      cancelGate.target = undefined
      cancelGate.entered = entered
      cancelGate.release = release
      yield* Effect.addFinalizer(() =>
        (cancelGate.release
          ? Deferred.succeed(cancelGate.release, undefined).pipe(Effect.ignore)
          : Effect.void).pipe(
          Effect.ensuring(
            Effect.sync(() => {
              cancelGate.target = undefined
              cancelGate.entered = undefined
              cancelGate.release = undefined
            }),
          ),
        ),
      )
      return yield* provideTmpdirServer(
        Effect.fnUntraced(function* ({ llm }) {
          const actor = yield* Actor.Service
          const session = yield* Session.Service
          const registry = yield* ActorRegistry.Service
          const parent = yield* session.create({ title: "cancel cleanup gate" })
          yield* llm.hang
          const first = yield* actor.spawn({
            mode: "subagent",
            sessionID: parent.id,
            agentType: "build",
            task: "cancel me",
            context: "none",
            tools: ["read"],
            background: true,
            model: ref,
          })
          cancelGate.target = `${parent.id}:${first.actorID}`
          yield* llm.wait(1)
          const firstRow = yield* registry.get(parent.id, first.actorID)

          const cancelFiber = yield* Effect.forkChild(
            actor.cancel(parent.id, first.actorID, "forced").pipe(Effect.timeout("50 millis"), Effect.exit),
          )
          yield* Deferred.await(entered)
          const timed = yield* Fiber.join(cancelFiber)
          expect(timed._tag).toBe("Failure")
          const afterCancel = yield* registry.get(parent.id, first.actorID)
          expect(afterCancel?.status).toBe("idle")
          expect(afterCancel?.lastOutcome).toBe("cancelled")

          yield* llm.text("resumed after cleanup")
          const resume = yield* Effect.forkChild(
            actor.spawn({
              mode: "subagent",
              sessionID: parent.id,
              actorID: first.actorID,
              agentType: "explore",
              task: "resume after cancellation",
              context: "none",
              tools: ["read"],
              background: false,
              model: ref,
            }),
          )
          yield* Effect.sleep("100 millis")
          const parked = yield* registry.get(parent.id, first.actorID)
          expect(parked?.executionRevision).toBe(firstRow?.executionRevision)
          expect(yield* llm.calls).toBe(1)

          yield* Deferred.succeed(release, undefined)
          const second = yield* Fiber.join(resume)
          expect((yield* Deferred.await(second.outcome)).status).toBe("success")
          const finalRow = yield* registry.get(parent.id, first.actorID)
          expect(finalRow?.executionRevision).toBeGreaterThan(firstRow?.executionRevision ?? 0)
          expect(yield* llm.calls).toBe(2)
        }),
        { git: true, config: providerCfg },
      )
    }),
  )
})

const ref = {
  providerID: ProviderID.make("test"),
  modelID: ModelID.make("test-model"),
}
const cheapRef = { providerID: ProviderID.make("test"), modelID: ModelID.make("test-model-cheap") }

// Config that registers a custom "test" provider with a "test-model" model
// so provider model lookup succeeds inside the loop.
const cfg = {
  provider: {
    test: {
      name: "Test",
      id: "test",
      env: [],
      npm: "@ai-sdk/openai-compatible",
      models: {
        "test-model": {
          id: "test-model",
          name: "Test Model",
          attachment: false,
          reasoning: false,
          temperature: false,
          tool_call: true,
          release_date: "2025-01-01",
          limit: { context: 100000, output: 10000 },
          cost: { input: 0, output: 0 },
          options: {},
        },
        "test-model-cheap": {
          id: "test-model-cheap",
          name: "Cheap Test Model",
          attachment: false,
          reasoning: false,
          temperature: false,
          tool_call: true,
          release_date: "2025-01-01",
          limit: { context: 100000, output: 10000 },
          cost: { input: 0, output: 0 },
          options: {},
        },
      },
      options: {
        apiKey: "test-key",
        baseURL: "http://localhost:1/v1",
      },
    },
  },
}

function providerCfg(url: string) {
  return {
    ...cfg,
    provider: {
      ...cfg.provider,
      test: {
        ...cfg.provider.test,
        options: {
          ...cfg.provider.test.options,
          baseURL: url,
        },
      },
    },
  }
}

function gptProviderCfg(url: string) {
  const config = providerCfg(url)
  return {
    ...config,
    provider: {
      ...config.provider,
      "gpt-test": {
        ...config.provider.test,
        id: "gpt-test",
        name: "GPT Test",
        models: {
          "gpt-5.4": {
            ...config.provider.test.models["test-model"],
            id: "deployment-primary",
            name: "GPT-5.4",
          },
        },
      },
    },
  }
}

function actorToolContext(sessionID: SessionID, messageID: MessageID, hostContext: Record<string, string>) {
  return {
    sessionID,
    messageID,
    agent: "build",
    abort: new AbortController().signal,
    extra: { bypassAgentCheck: true, hostContext },
    messages: [],
    metadata: () => Effect.void,
    ask: () => Effect.void,
  }
}

describe("ActorTool durable host binding", () => {
  it.live("resumes through the real Actor registry/spawn path without rebinding a child", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const sessions = yield* Session.Service
        const registry = yield* ActorRegistry.Service
        const parent = yield* sessions.create({
          title: "host binding parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        const messageID = MessageID.ascending()
        yield* sessions.updateMessage({
          id: messageID,
          sessionID: parent.id,
          role: "assistant",
          time: { created: Date.now() },
          agent: "build",
          model: ref,
        } as unknown as MessageV2.Info)

        const tool = yield* ActorTool
        const def = yield* tool.init()
        const contextA = { accountID: "account-a", chatID: "chat-a", workspaceID: "workspace-a", cwd: "/workspace/a" }
        const contextB = { accountID: "account-b", chatID: "chat-b", workspaceID: "workspace-b", cwd: "/workspace/b" }

        yield* llm.text("first successful turn")
        const first = yield* def.execute(
          {
            operation: {
              action: "run",
              subagent_type: "general",
              description: "durable child",
              prompt: "first prompt",
              model: "test/test-model",
            },
          },
          actorToolContext(parent.id, messageID, contextA),
        )
        const actorID = String(first.metadata.actorId)
        const storedAfterFirst = yield* registry.get(parent.id, actorID)
        expect(storedAfterFirst?.hostContext).toEqual(contextA)
        expect(storedAfterFirst?.turnCount).toBeGreaterThanOrEqual(1)
        expect(storedAfterFirst?.executionRevision).toBeGreaterThan(0)

        yield* llm.text("second successful turn")
        const second = yield* def.execute(
          {
            operation: {
              action: "run",
              subagent_type: "general",
              description: "resume must retain A",
              prompt: "second prompt",
              actor_id: actorID,
            },
          },
          actorToolContext(parent.id, messageID, contextB),
        )
        const storedAfterSecond = yield* registry.get(parent.id, actorID)
        expect(storedAfterSecond?.hostContext).toEqual(contextA)
        expect(storedAfterSecond?.turnCount).toBeGreaterThanOrEqual(2)
        expect(storedAfterSecond?.executionRevision).toBeGreaterThan(storedAfterFirst?.executionRevision ?? 0)
        expect(second.metadata.hostContext).toEqual(contextA)

        const status = yield* def.execute(
          { operation: { action: "status", actor_id: actorID } },
          actorToolContext(parent.id, messageID, contextB),
        )
        expect(JSON.parse(status.output).host_context).toEqual(contextA)

        const cancel = yield* def.execute(
          { operation: { action: "cancel", actor_id: actorID } },
          actorToolContext(parent.id, messageID, contextB),
        )
        expect(JSON.parse(cancel.output).host_context).toEqual(contextA)

        yield* llm.text("sibling successful turn")
        const sibling = yield* def.execute(
          {
            operation: {
              action: "run",
              subagent_type: "general",
              description: "new sibling inherits B",
              prompt: "sibling prompt",
              model: "test/test-model",
            },
          },
          actorToolContext(parent.id, messageID, contextB),
        )
        expect(sibling.metadata.hostContext).toEqual(contextB)
      }),
      { git: true, config: providerCfg },
    ) as any,
    30000,
  )
})

describe("Actor.spawn peer mode", () => {
  it.live("creates a new sessionID, registers actor with mode=peer", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "test parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        // Queue a stop response so the prompt loop can run to completion.
        yield* llm.text("done")

        yield* Effect.acquireUseRelease(
          actor.spawn({
            mode: "peer",
            sessionID: parent.id,
            agentType: "build",
            task: "test task",
            context: "none",
            tools: ["read"],
            background: true,
            model: ref,
          }),
          (result) => Effect.gen(function* () {
            expect(result.actorID).not.toBe(parent.id)
            expect(result.sessionID as string).toBe(result.actorID)

            const row = yield* reg.get(result.sessionID, result.actorID)
            expect(row?.mode).toBe("peer")
            expect(row?.agent).toBe("build")
          }),
          (result) => Effect.gen(function* () {
            yield* actor.cancel(result.sessionID, result.actorID, "forced")
            yield* Deferred.await(result.outcome)
          }),
        )
      }),
      { git: true, config: providerCfg },
    ),
  )

  // 尚未首次执行的 peer 也应通过接收方存在性检查。
  it.live("send to a just-created, never-run peer (turnCount 0) does NOT ESRCH and enqueues", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service
        const inbox = inboxServiceRef.current!

        const parent = yield* session.create({
          title: "T42 parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // Hang so the child's spawn turn never completes — the receiver row must
        // exist purely from spawn-time registration, not first-turn arming.
        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "build",
          task: "peer task",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
        })

        // Row present at spawn: pending, zero turns (never ran).
        const row = yield* reg.get(result.sessionID, result.actorID)
        expect(row?.mode).toBe("peer")
        expect(row?.turnCount).toBe(0)
        expect(row?.status).toBe("pending")

        // Both addressing forms resolve without ESRCH and enqueue a durable row.
        const sent = yield* inbox
          .accept({
            receiverSessionID: result.sessionID,
            receiverActorID: result.actorID,
            senderSessionID: parent.id,
            senderActorID: "main",
            content: "relayed while never-run",
          })
          .pipe(Effect.exit)
        expect(sent._tag).toBe("Success")
        if (sent._tag === "Success") expect(sent.value.inboxID).toBeTruthy()

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("cancelling a pending peer prevents its first model turn", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service
        const parent = yield* session.create({ title: "pending cancel" })
        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "build",
          task: "must not start",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
        })
        yield* actor.cancel(result.sessionID, result.actorID, "forced")
        const outcome = yield* Deferred.await(result.outcome)
        const row = yield* reg.get(result.sessionID, result.actorID)
        expect(outcome.status).toBe("cancelled")
        expect(row?.status).toBe("idle")
        expect(row?.turnCount).toBe(0)
        expect(row?.lastOutcome).toBe("cancelled")
        expect(yield* llm.calls).toBe(0)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("foreground peer waits for its deferred launcher and outcome", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "foreground peer" })
        yield* llm.text("finished")

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "build",
          task: "finish now",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("success")
        expect(yield* llm.calls).toBeGreaterThan(0)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("background peer remains pending at return, then launches and completes", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service
        const parent = yield* session.create({ title: "background peer handoff" })
        yield* llm.text("background result")

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "build",
          task: "run after spawn returns",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
        })

        // The deferred launcher keeps the receiver addressable without starting
        // model/task work before the caller observes the new actor.
        const parked = yield* reg.get(result.sessionID, result.actorID)
        expect(parked?.status).toBe("pending")
        expect(parked?.turnCount).toBe(0)

        const outcome = yield* Deferred.await(result.outcome).pipe(Effect.timeout("10 seconds"))
        expect(outcome.status).toBe("success")
        expect(yield* llm.calls).toBeGreaterThan(0)
        const finished = yield* reg.get(result.sessionID, result.actorID)
        expect(finished?.status).toBe("idle")
        expect(finished?.turnCount).toBeGreaterThan(0)
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn subagent mode", () => {
  it.live("gives GPT subagents the default tool set outside Codex mode", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "GPT subagent tools" })

        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "verify tools",
          context: "none",
          tools: "INHERIT",
          background: false,
          model: { providerID: ProviderID.make("gpt-test"), modelID: ModelID.make("gpt-5.4") },
        })
        yield* Deferred.await(result.outcome)

        const request = (yield* llm.hits).find((hit) => Array.isArray(hit.body.tools) && hit.body.tools.length > 0)
        const names = (request?.body.tools as Array<{ function?: { name?: string } }> | undefined)?.map(
          (tool) => tool.function?.name,
        )
        expect(names).toContain("read")
        expect(names).not.toContain("exec")
        expect(names).not.toContain("apply_patch")
        expect(names).not.toContain("view_image")
      }),
      { git: true, config: gptProviderCfg },
    ),
    30000,
  )

  it.live("exposes the full Codex tool set to general subagents in Codex mode", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const previous = process.env.OPENCLANK_CODEX_MODE
        yield* Effect.acquireRelease(
          Effect.sync(() => {
            process.env.OPENCLANK_CODEX_MODE = "true"
          }),
          () =>
            Effect.sync(() => {
              if (previous === undefined) delete process.env.OPENCLANK_CODEX_MODE
              else process.env.OPENCLANK_CODEX_MODE = previous
            }),
        )
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "General GPT subagent tools" })

        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "general",
          task: "verify tools",
          context: "none",
          tools: "INHERIT",
          background: false,
          model: { providerID: ProviderID.make("gpt-test"), modelID: ModelID.make("gpt-5.4") },
        })
        yield* Deferred.await(result.outcome)

        const request = (yield* llm.hits).find(
          (hit) =>
            Array.isArray(hit.body.tools) &&
            hit.body.tools.some((tool) => (tool as { function?: { name?: string } }).function?.name === "exec"),
        )
        const names = (request?.body.tools as Array<{ function?: { name?: string } }> | undefined)?.map(
          (tool) => tool.function?.name,
        )
        expect(names).toContain("exec")
        expect(names).not.toContain("apply_patch")
        expect(names).not.toContain("view_image")
        expect(names).not.toContain("read")
        expect(names).not.toContain("edit")
        expect(names).not.toContain("write")
        const exec = (request?.body.tools as Array<{ function?: { name?: string; description?: string } }>).find(
          (tool) => tool.function?.name === "exec",
        )
        expect(exec?.function?.description).toContain("apply_patch(input:")
        expect(exec?.function?.description).toContain("view_image(input:")
      }),
      { git: true, config: gptProviderCfg },
    ),
    30000,
  )

  it.live("keeps the legacy tool set for non-GPT subagents", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "General non-GPT subagent tools" })

        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "general",
          task: "verify tools",
          context: "none",
          tools: "INHERIT",
          background: false,
          model: ref,
        })
        yield* Deferred.await(result.outcome)

        const request = (yield* llm.hits).find((hit) => Array.isArray(hit.body.tools))
        const names = (request?.body.tools as Array<{ function?: { name?: string } }> | undefined)?.map(
          (tool) => tool.function?.name,
        )
        expect(names).toContain("read")
        expect(names).toContain("edit")
        expect(names).toContain("write")
        expect(names).not.toContain("exec")
        expect(names).not.toContain("apply_patch")
        expect(names).not.toContain("view_image")
      }),
      { git: true, config: providerCfg },
    ),
    30000,
  )

  it.live("does NOT create new session, allocates <type>-<n> actorID", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "test parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "checkpoint task",
          context: "full",
          tools: ["read", "edit"],
          background: true,
          model: ref,
        })

        expect(result.sessionID).toBe(parent.id)
        expect(result.actorID).toBe("build-1")

        const row = yield* reg.get(parent.id, result.actorID)
        expect(row?.mode).toBe("subagent")
        expect(row?.agent).toBe("build")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn fiber lifecycle", () => {
  it.live("outcome resolves with success when fiber completes normally", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "minimal task",
          context: "none",
          tools: ["read"],
          background: false, // blocking — wait for completion
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        expect(["success", "failure"]).toContain(outcome.status)
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn onReady callback", () => {
  it.live("onReady fires before Fiber.join blocks (metadata available while running)", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.hang
        let readyInfo: { actorID: string; sessionID: string } | undefined
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "long running task",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
          onReady: ({ actorID, sessionID }) =>
            Effect.sync(() => { readyInfo = { actorID, sessionID: sessionID as string } }),
        })
        expect(readyInfo).toBeDefined()
        expect(readyInfo!.actorID).toBe(result.actorID)
        expect(readyInfo!.sessionID).toBe(parent.id)
        yield* actor.cancel(parent.id, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.cancel", () => {
  it.live("cancel(forced) interrupts fiber and marks actor cancelled", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const reg = yield* ActorRegistry.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // Hang the LLM so the fiber stays alive long enough to interrupt.
        yield* llm.hang
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "long task",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
        })
        yield* llm.wait(1)
        yield* actor.cancel(result.sessionID, result.actorID, "forced")
        const row = yield* reg.get(result.sessionID, result.actorID)
        expect(row?.status).toBe("idle")
        expect(row?.lastOutcome).toBe("cancelled")
        const receipts = Database.use((db) => db.select().from(InboxTable).where(and(
          eq(InboxTable.receiver_session_id, result.sessionID), eq(InboxTable.receiver_actor_id, result.actorID),
        )).all())
        expect(receipts).toHaveLength(1)
        expect(receipts[0].state).toBe("cancelled")
        expect(receipts[0].outcome).toBe("interrupted")
        expect(receipts[0].consumed).toBe(true)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("completion and forced cancel emit one terminal parent notification", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "terminal race",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // The result may win before cancel or cancellation may win while the
        // turn is active; either way the durable claim must leave one row.
        yield* llm.text("race result")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "race task",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
        })
        yield* Effect.all(
          [actor.cancel(result.sessionID, result.actorID, "forced"), Deferred.await(result.outcome)],
          { concurrency: "unbounded" },
        )
        const rows = Database.use((db) =>
          db
            .select()
            .from(InboxTable)
            .where(
              and(
                eq(InboxTable.receiver_session_id, parent.id),
                eq(InboxTable.receiver_actor_id, "main"),
                eq(InboxTable.type, "actor_notification"),
              ),
            )
            .all(),
        )
        expect(rows).toHaveLength(1)
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("SessionPrompt.cancel — process-group kill", () => {
  // Product contract: session abort simulates a kill signal and must propagate
  // to all same-session subagents (not only main). Independent of Orchestrator.
  // Quiet abort: cancel notifications must not re-wake the aborted session.
  it.live(
    "session abort cascades running actors (registry cancelled, session idle, no wake)",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* ({ llm }) {
          const actor = yield* Actor.Service
          const prompt = yield* SessionPrompt.Service
          const reg = yield* ActorRegistry.Service
          const status = yield* SessionStatus.Service
          const session = yield* Session.Service
          const runState = yield* SessionRunState.Service
          const parent = yield* session.create({
            title: "abort cascade",
            permission: [{ permission: "*", pattern: "*", action: "allow" }],
          })
          // hang is a queued response per LLM request — enqueue one hang for
          // main + actor A + actor B so all three model calls stay pending.
          yield* llm.hang
          yield* llm.hang
          yield* llm.hang
          // Real main turn (runLoop sets SessionStatus.busy for main) with hang LLM.
          yield* prompt
            .prompt({
              sessionID: parent.id,
              agent: "build",
              agentID: "main",
              parts: [{ type: "text", text: "main long turn" }],
              model: ref,
            })
            .pipe(Effect.forkChild)
          for (let i = 0; i < 80; i++) {
            const st = yield* status.get(parent.id)
            if (st.type === "busy") break
            yield* Effect.sleep("50 millis")
          }
          expect((yield* status.get(parent.id)).type).toBe("busy")
          const busyBefore = yield* runState.assertNotBusy(parent.id, "main").pipe(Effect.exit)
          expect(busyBefore._tag).toBe("Failure")

          const spawnOne = (task: string) =>
            actor.spawn({
              mode: "subagent",
              sessionID: parent.id,
              agentType: "build",
              task,
              context: "none",
              tools: ["read"],
              background: true,
              model: ref,
            })
          const a = yield* spawnOne("long background task A")
          const b = yield* spawnOne("long background task B")

          // Wait until each actor is actually executing (running), not merely
          // registered (pending). hang LLM keeps runTurn in running once entered.
          const waitRunning = (sessionID: string, actorID: string) =>
            Effect.gen(function* () {
              let row = yield* reg.get(sessionID as never, actorID)
              for (let i = 0; i < 80 && row?.status !== "running"; i++) {
                yield* Effect.sleep("50 millis")
                row = yield* reg.get(sessionID as never, actorID)
              }
              return row
            })
          const rowA = yield* waitRunning(a.sessionID, a.actorID)
          const rowB = yield* waitRunning(b.sessionID, b.actorID)
          expect(rowA?.status).toBe("running")
          expect(rowB?.status).toBe("running")
          // All three model requests must have actually arrived (hang-queued).
          yield* llm.wait(3)
          const controller = inboxControllerRef.current
          if (!controller) throw new Error("Inbox controller missing")
          const epoch = yield* controller.getEpoch(parent.id)
          yield* prompt.cancel(parent.id)
          expect(yield* controller.getEpoch(parent.id)).toBe(epoch + 1)

          const afterA = yield* reg.get(a.sessionID, a.actorID)
          const afterB = yield* reg.get(b.sessionID, b.actorID)
          expect(afterA?.status).toBe("idle")
          expect(afterA?.lastOutcome).toBe("cancelled")
          expect(afterB?.status).toBe("idle")
          expect(afterB?.lastOutcome).toBe("cancelled")
          expect((yield* status.get(parent.id)).type).toBe("idle")
          const busyAfter = yield* runState.assertNotBusy(parent.id, "main").pipe(Effect.exit)
          expect(busyAfter._tag).toBe("Success")

          // Quiet abort: no wake — session stays idle, actors stay cancelled (not running).
          yield* Effect.sleep("400 millis")
          expect((yield* status.get(parent.id)).type).toBe("idle")
          const laterA = yield* reg.get(a.sessionID, a.actorID)
          const laterB = yield* reg.get(b.sessionID, b.actorID)
          expect(laterA?.status).toBe("idle")
          expect(laterA?.lastOutcome).toBe("cancelled")
          expect(laterB?.status).toBe("idle")
          expect(laterB?.lastOutcome).toBe("cancelled")
          const busyLater = yield* runState.assertNotBusy(parent.id, "main").pipe(Effect.exit)
          expect(busyLater._tag).toBe("Success")

          // R14 terminal inline (TP-R14-14): quiet abort still materializes cancelled
          // notifications into parent session history (synthetic message parts).
          const parts = yield* Effect.sync(() =>
            Database.use((db) =>
              db
                .select({ data: PartTable.data })
                .from(PartTable)
                .where(eq(PartTable.session_id, parent.id))
                .all(),
            ),
          )
          const notes = parts
            .map((row) => row.data as { type?: string; synthetic?: boolean; text?: string })
            .filter((d) => d?.type === "text" && d?.synthetic === true && typeof d.text === "string")
            .map((d) => d.text as string)
            .filter((text) => text.includes("<actor-notification>"))
          expect(notes.length).toBeGreaterThanOrEqual(2)
          expect(notes.filter((text) => text.includes("was cancelled")).length).toBeGreaterThanOrEqual(2)
          // inbox (d)：终止通知在停止收尾结算，消息保持可见。
          const inboxLeft = yield* Effect.sync(() =>
            Database.use((db) =>
              db
                .select()
                .from(InboxTable)
                .where(and(
                  eq(InboxTable.receiver_session_id, parent.id),
                  eq(InboxTable.receiver_actor_id, "main"),
                  eq(InboxTable.type, "actor_notification"),
                ))
                .all(),
            ),
          )
          expect(inboxLeft).toHaveLength(2)
          for (const receipt of inboxLeft) {
            expect(receipt.state).toBe("cancelled")
            expect(receipt.outcome).toBe("never_ran")
            expect(receipt.content).toBeNull()
            expect(receipt.input_message_id).toBeTruthy()
          }
          expect(yield* controller.listAccepted({ sessionID: parent.id, agentID: "main" })).toEqual([])
        }),
        { git: true, config: providerCfg },
      ),
  )

  it.live(
    "registry-only cancel materializes one durable cancelled notification",
    () =>
      provideTmpdirServer(
        Effect.fnUntraced(function* () {
          const actor = yield* Actor.Service
          const reg = yield* ActorRegistry.Service
          const sessions = yield* Session.Service
          const parent = yield* sessions.create({
            title: "registry-only abort",
            permission: [{ permission: "*", pattern: "*", action: "allow" }],
          })
          yield* reg.register({
            sessionID: parent.id,
            actorID: "pending-1",
            mode: "subagent",
            agent: "build",
            description: "pending child",
            contextMode: "none",
            parentActorID: "main",
            background: true,
            lifecycle: "ephemeral",
          })
          yield* reg.updateStatus(parent.id, "pending-1", { status: "pending" })

          yield* actor.cancel(parent.id, "pending-1", "forced")

          const after = yield* reg.get(parent.id, "pending-1")
          expect(after?.status).toBe("idle")
          expect(after?.lastOutcome).toBe("cancelled")
          const rows = yield* Effect.sync(() =>
            Database.use((db) =>
              db
                .select()
                .from(InboxTable)
                .where(and(eq(InboxTable.receiver_session_id, parent.id), eq(InboxTable.receiver_actor_id, "main")))
                .all(),
            ),
          )
          expect(rows.length).toBe(1)
          const message = MessageV2.get({ sessionID: parent.id, messageID: rows[0].input_message_id! })
          expect(message.parts.some((part) => part.type === "text" && part.synthetic && part.text.includes("cancelled"))).toBe(true)
          // Second cancel is a no-op — no duplicate notify.
          yield* actor.cancel(parent.id, "pending-1", "forced")
          const again = yield* Effect.sync(() =>
            Database.use((db) =>
              db
                .select()
                .from(InboxTable)
                .where(and(eq(InboxTable.receiver_session_id, parent.id), eq(InboxTable.receiver_actor_id, "main")))
                .all(),
            ),
          )
          expect(again.length).toBe(1)
        }),
        { git: true, config: providerCfg },
      ),
  )
})

describe("Actor.spawn agent_id persistence", () => {
  it.live("subagent's user message is persisted with agent_id = actorID", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "agent_id verification",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "agent_id verification task",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })

        // Wait for the agent loop to complete.
        yield* Deferred.await(result.outcome)

        // Query the message table directly: the user message persisted by
        // SessionPrompt.prompt should carry agent_id = actorID.
        expect(result.actorID).toBe("build-1")

        const rows = yield* Effect.sync(() =>
          Database.use((db) =>
            db
              .select({ id: MessageTable.id, agent_id: MessageTable.agent_id })
              .from(MessageTable)
              .where(and(eq(MessageTable.session_id, parent.id), eq(MessageTable.agent_id, result.actorID)))
              .all(),
          ),
        )

        expect(rows.length).toBeGreaterThan(0)
        for (const row of rows) {
          expect(row.agent_id).toBe(result.actorID)
        }
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn context_watermark", () => {
  it.live("subagent with context=full captures latest main message ID as watermark", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "watermark verification",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        // Seed parent main thread with a few user messages (agent_id IS NULL).
        const seeded: MessageID[] = []
        for (const text of ["main-1", "main-2", "main-3"]) {
          const id = MessageID.ascending()
          yield* session.updateMessage({
            id,
            sessionID: parent.id,
            role: "user",
            time: { created: Date.now() },
            agent: "test",
            model: { providerID: ProviderID.make("test"), modelID: ModelID.make("test-model") },
            tools: {},
            mode: "",
          } as unknown as MessageV2.Info)
          yield* session.updatePart({
            id: PartID.ascending(),
            sessionID: parent.id,
            messageID: id,
            type: "text",
            text,
          })
          seeded.push(id)
        }

        // Sanity-check Session.lastMainMessageID returns the latest seeded ID.
        const last = yield* session.lastMainMessageID(parent.id)
        expect(last).toBe(seeded[seeded.length - 1])

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "watermark check",
          context: "full",
          tools: ["read"],
          background: true,
          model: ref,
        })

        const row = yield* reg.get(parent.id, result.actorID)
        expect(row?.contextWatermark).toBeDefined()
        expect(row?.contextWatermark).toBe(seeded[seeded.length - 1])
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn description field (F2a)", () => {
  it.live("falls back to agentType when description not provided", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "fallback parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        yield* Effect.acquireUseRelease(
          actor.spawn({
            mode: "subagent",
            sessionID: parent.id,
            agentType: "build",
            task: "x".repeat(500),  // long, would be sliced to 200 chars under the bug
            context: "none",
            tools: ["read"],
            background: true,
            model: ref,
          }),
          (result) => Effect.gen(function* () {
            const row = yield* reg.get(result.sessionID, result.actorID)
            expect(row?.description).toBe("build")  // agentType, NOT first 200 chars of task
          }),
          (result) => Effect.gen(function* () {
            yield* actor.cancel(result.sessionID, result.actorID, "forced")
            yield* Deferred.await(result.outcome)
          }),
        )
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("uses provided description when supplied", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "explicit parent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        yield* Effect.acquireUseRelease(
          actor.spawn({
            mode: "subagent",
            sessionID: parent.id,
            agentType: "build",
            task: "long prompt body",
            description: "explore: find lexer files",
            context: "none",
            tools: ["read"],
            background: true,
            model: ref,
          }),
          (result) => Effect.gen(function* () {
            const row = yield* reg.get(result.sessionID, result.actorID)
            expect(row?.description).toBe("explore: find lexer files")
          }),
          (result) => Effect.gen(function* () {
            yield* actor.cancel(result.sessionID, result.actorID, "forced")
            yield* Deferred.await(result.outcome)
          }),
        )
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor forkContext lifecycle", () => {
  it.live("keeps ancestor permission and tool-whitelist restrictions in nested full-context actors", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const registry = yield* ActorRegistry.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "nested fork restrictions" })
        const hostContext = { chatID: "nested-fork-chat", workspaceID: "nested-fork-workspace", cwd: "/tmp" }
        yield* registry.updateModel(parent.id, "main", undefined, ref)
        yield* registry.updateHostContext(parent.id, "main", hostContext)
        const sourceUserID = MessageID.ascending()
        yield* session.updateMessage({
          id: sourceUserID,
          sessionID: parent.id,
          role: "user",
          agentID: "main",
          agent: "explore",
          model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(),
          sessionID: parent.id,
          messageID: sourceUserID,
          type: "text",
          text: "readonly ancestor source",
        })
        const firstContext = {
          system: ["frozen-system"],
          tools: {},
          parentPermission: [],
          inheritedMessages: [{ role: "user" as const, content: "readonly ancestor source" }],
          watermarkMsgID: sourceUserID,
          model: cheapRef,
        }
        yield* llm.text("first nested actor finished")
        const first = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          parentSessionID: parent.id,
          parentActorID: "main",
          forkContextSource: { sessionID: parent.id, actorID: "main" },
          agentType: "build",
          task: "first child",
          context: "full",
          tools: ["read"],
          background: true,
          model: cheapRef,
          hostContext,
          forkContext: firstContext,
        })
        yield* Deferred.await(first.outcome)
        const firstMessages = yield* session.messages({ sessionID: parent.id, agentID: first.actorID })
        const firstUser = firstMessages.findLast((message) => message.info.role === "user")
        if (!firstUser || firstUser.info.role !== "user") throw new Error("first child origin is missing")
        const secondContext = {
          ...firstContext,
          inheritedMessages: [{ role: "user" as const, content: "nested child prefix" }],
          watermarkMsgID: firstUser.info.id,
        }
        yield* llm.text("second nested actor finished")
        const second = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          parentSessionID: parent.id,
          parentActorID: first.actorID,
          forkContextSource: { sessionID: parent.id, actorID: first.actorID },
          agentType: "build",
          task: "second child requests broader tools",
          context: "full",
          tools: ["read", "write"],
          background: true,
          model: cheapRef,
          hostContext,
          forkContext: secondContext,
        })
        yield* Deferred.await(second.outcome)
        const secondRow = yield* registry.get(parent.id, second.actorID)
        const secondProjection = yield* actor.getForkContext(parent.id, second.actorID)
        expect(secondRow?.tools).toEqual(["read"])
        expect(secondProjection?.effectiveToolWhitelist).toEqual(["read"])
        expect(Permission.evaluateIntersection("write", "*", secondProjection?.permissionRulesets ?? []).denied).toBe(true)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("reloads a settled fork snapshot and resumes the same actor after parent history changes", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const registry = yield* ActorRegistry.Service
        const session = yield* Session.Service
        const parent = yield* session.create({ title: "durable fork resume" })
        const hostContext = { chatID: "fork-chat", workspaceID: "fork-workspace", cwd: "/tmp" }
        yield* registry.updateModel(parent.id, "main", undefined, ref)
        yield* registry.updateHostContext(parent.id, "main", hostContext)
        const sourceUserID = MessageID.ascending()
        yield* session.updateMessage({
          id: sourceUserID,
          sessionID: parent.id,
          role: "user",
          agentID: "main",
          agent: "build",
          model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(),
          sessionID: parent.id,
          messageID: sourceUserID,
          type: "text",
          text: "SOURCE_FROZEN_PREFIX",
        })
        yield* llm.text("initial fork turn completed")
        const fakeForkCtx = {
          system: ["frozen-system"],
          tools: {},
          parentPermission: [],
          inheritedMessages: [{ role: "user" as const, content: "SOURCE_FROZEN_PREFIX" }],
          watermarkMsgID: sourceUserID,
          model: cheapRef,
        }
        const spawned = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          parentSessionID: parent.id,
          parentActorID: "main",
          forkContextSource: { sessionID: parent.id, actorID: "main" },
          agentType: "explore",
          task: "first same-actor turn",
          context: "full",
          tools: [],
          background: true,
          model: cheapRef,
          hostContext,
          forkContext: fakeForkCtx,
        })
        yield* Deferred.await(spawned.outcome)
        const afterFirst = yield* registry.get(parent.id, spawned.actorID)
        expect(afterFirst?.status).toBe("idle")
        // Settlement drops the process-local cache. A read must decode the
        // immutable DB snapshot rather than rebuilding from the current parent.
        const frozen = yield* actor.getForkContext(parent.id, spawned.actorID)
        expect(frozen?.context.inheritedMessages[0]).toEqual({ role: "user", content: "SOURCE_FROZEN_PREFIX" })
        const changedParentID = MessageID.ascending()
        yield* session.updateMessage({
          id: changedParentID,
          sessionID: parent.id,
          role: "user",
          agentID: "main",
          agent: "build",
          model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(),
          sessionID: parent.id,
          messageID: changedParentID,
          type: "text",
          text: "PARENT_CHANGED_AFTER_FORK",
        })
        const actorMessages = yield* session.messages({ sessionID: parent.id, agentID: spawned.actorID })
        const origin = actorMessages.findLast((message) => message.info.role === "user")
        if (!origin || origin.info.role !== "user") throw new Error("spawned actor user origin is missing")
        const current = yield* registry.get(parent.id, spawned.actorID)
        if (!current) throw new Error("spawned actor row is missing after settlement")
        UserInput.admit({
          owner: "fork-owner", stableChatID: "fork-chat", mapRevision: 1, mappingRevision: 1, workspaceRevision: 1,
          requestID: "fork-resume-request", sessionID: parent.id, actorID: spawned.actorID,
          executionRevision: current.executionRevision, originTurnID: origin.info.id,
          text: "RESUMED_FORK_INPUT", attachmentRefs: [], questionRefs: [], mode: "after-turn",
        })
        yield* llm.textMatch(
          (hit) => JSON.stringify(hit.body).includes("SOURCE_FROZEN_PREFIX") &&
            !JSON.stringify(hit.body).includes("PARENT_CHANGED_AFTER_FORK") &&
            JSON.stringify(hit.body).includes(ref.modelID),
          "resumed from frozen context",
        )
        if (!actor.resumeInput) throw new Error("same-actor resume is unavailable")
        const result = yield* actor.resumeInput({
          sessionID: parent.id,
          actorID: spawned.actorID,
          claim: {
            owner: "fork-owner", stableChatID: "fork-chat", mapRevision: 1, mappingRevision: 1, workspaceRevision: 1,
            requestID: "fork-resume-request", handoffID: "fork-resume-handoff", rootOperationID: "fork-resume-root",
            expectedExecutionRevision: current.executionRevision,
            agent: current.agent,
            effectiveModel: current.effectiveModel,
            hostContext,
          },
        })
        expect(result.kind).toBe("fresh-claim")
        const deadline = Date.now() + 10_000
        let settled = false
        while (Date.now() < deadline) {
          const row = yield* registry.get(parent.id, spawned.actorID)
          if (row?.status === "idle" && row.executionRevision > current.executionRevision) {
            settled = true
            break
          }
          yield* Effect.sleep("20 millis")
        }
        expect(settled).toBe(true)

        const resumedActor = yield* registry.get(parent.id, spawned.actorID)
        if (!resumedActor) throw new Error("resumed actor row is missing")
        const afterResumeMessages = yield* session.messages({ sessionID: parent.id, agentID: spawned.actorID })
        const retryOrigin = afterResumeMessages.findLast((message) => message.info.role === "user")
        if (!retryOrigin || retryOrigin.info.role !== "user") throw new Error("resumed actor origin is missing")
        UserInput.admit({
          owner: "fork-owner", stableChatID: "fork-chat", mapRevision: 1, mappingRevision: 1, workspaceRevision: 1,
          requestID: "fork-missing-snapshot-request", sessionID: parent.id, actorID: spawned.actorID,
          executionRevision: resumedActor.executionRevision, originTurnID: retryOrigin.info.id,
          text: "must remain pending when snapshot is missing", attachmentRefs: [], questionRefs: [], mode: "after-turn",
        })
        Database.use((db) => db.delete(ActorForkContextTable).where(and(
          eq(ActorForkContextTable.session_id, parent.id), eq(ActorForkContextTable.actor_id, spawned.actorID),
        )).run())
        const missing = yield* actor.resumeInput({
          sessionID: parent.id,
          actorID: spawned.actorID,
          claim: {
            owner: "fork-owner", stableChatID: "fork-chat", mapRevision: 1, mappingRevision: 1, workspaceRevision: 1,
            requestID: "fork-missing-snapshot-request", handoffID: "fork-missing-snapshot-handoff",
            rootOperationID: "fork-missing-snapshot-root", expectedExecutionRevision: resumedActor.executionRevision,
            agent: resumedActor.agent, effectiveModel: resumedActor.effectiveModel, hostContext,
          },
        })
        expect(missing.kind).toBe("unavailable")
        const notClaimed = Database.use((db) => db.select().from(UserInputReceiptTable).where(and(
          eq(UserInputReceiptTable.owner, "fork-owner"),
          eq(UserInputReceiptTable.stable_chat_id, "fork-chat"),
          eq(UserInputReceiptTable.request_id, "fork-missing-snapshot-request"),
        )).get())
        expect(notClaimed?.claimed_execution_revision).toBeNull()
        expect((yield* registry.get(parent.id, spawned.actorID))?.executionRevision).toBe(resumedActor.executionRevision)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("forkContext remains durable after actor is cancelled", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "forkCtx lifecycle",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        const hostContext = { chatID: "cancel-fork-chat", workspaceID: "cancel-fork-workspace", cwd: "/tmp" }
        const registry = yield* ActorRegistry.Service
        yield* registry.updateModel(parent.id, "main", undefined, ref)
        yield* registry.updateHostContext(parent.id, "main", hostContext)
        const sourceUserID = MessageID.ascending()
        yield* session.updateMessage({
          id: sourceUserID,
          sessionID: parent.id,
          role: "user",
          agentID: "main",
          agent: "build",
          model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(),
          sessionID: parent.id,
          messageID: sourceUserID,
          type: "text",
          text: "cancel snapshot source",
        })

        // Hang the LLM so the fiber stays alive while we verify pre-cancel state.
        yield* llm.hang

        const fakeForkCtx = {
          system: ["test-system"],
          tools: {},
          inheritedMessages: [{ role: "user" as const, content: "cancel snapshot source" }],
          parentPermission: [],
          watermarkMsgID: sourceUserID,
          model: ref,
        }

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "noop",
          context: "full",
          tools: [],
          background: true,
          model: ref,
          parentActorID: "main",
          forkContextSource: { sessionID: parent.id, actorID: "main" },
          hostContext,
          forkContext: fakeForkCtx,
        })

        // Before cancel: forkContext must be present.
        const before = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(before).toBeDefined()
        expect(before?.context.system).toEqual(["test-system"])

        // Cancel forces immediate termination.
        yield* actor.cancel(result.sessionID, result.actorID, "forced")

        // Cancellation removes the process cache, but the immutable row stays
        // for explicit recovery/export and same-actor resume.
        const after = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(after?.context.system).toEqual(["test-system"])
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("keeps forkContexts isolated when actor ids repeat across sessions", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const first = yield* session.create({ title: "first fork context" })
        const second = yield* session.create({ title: "second fork context" })
        // Both executors must remain live while cancellation joins the first.
        yield* llm.hang
        yield* llm.hang

        const firstResult = yield* actor.spawn({
          mode: "subagent",
          sessionID: first.id,
          agentType: "explore",
          task: "first",
          context: "full",
          tools: [],
          background: true,
          model: ref,
          forkContext: {
            system: ["first-system"],
            tools: {},
            inheritedMessages: [],
            parentPermission: [],
            watermarkMsgID: MessageID.ascending(),
            model: ref,
          },
        })
        const secondResult = yield* actor.spawn({
          mode: "subagent",
          sessionID: second.id,
          agentType: "explore",
          task: "second",
          context: "full",
          tools: [],
          background: true,
          model: ref,
          forkContext: {
            system: ["second-system"],
            tools: {},
            inheritedMessages: [],
            parentPermission: [],
            watermarkMsgID: MessageID.ascending(),
            model: ref,
          },
        })

        expect(firstResult.actorID).toBe(secondResult.actorID)
        expect((yield* actor.getForkContext(firstResult.sessionID, firstResult.actorID))?.system).toEqual(["first-system"])
        expect((yield* actor.getForkContext(secondResult.sessionID, secondResult.actorID))?.system).toEqual(["second-system"])

        yield* actor.cancel(firstResult.sessionID, firstResult.actorID, "forced")
        expect((yield* actor.getForkContext(secondResult.sessionID, secondResult.actorID))?.system).toEqual(["second-system"])
        yield* actor.cancel(secondResult.sessionID, secondResult.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("mode × contextMode matrix", () => {
  const fakeForkCtx: Actor.ForkContext = {
    system: ["test-system"],
    tools: {},
    inheritedMessages: [{ role: "user", content: "captured source user" }],
    parentPermission: [],
    watermarkMsgID: MessageID.make("msg_test_watermark"),
    model: ref,
  }

  it.live("subagent + full: forkContext stored under actorID", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "matrix subagent+full",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        const registry = yield* ActorRegistry.Service
        yield* registry.updateModel(parent.id, "main", undefined, ref)
        const sourceUserID = MessageID.ascending()
        yield* session.updateMessage({
          id: sourceUserID, sessionID: parent.id, role: "user", agentID: "main", agent: "build", model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(), sessionID: parent.id, messageID: sourceUserID, type: "text", text: "captured source user",
        })

        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "noop",
          context: "full",
          tools: [],
          background: true,
          model: ref,
          parentActorID: "main",
          forkContextSource: { sessionID: parent.id, actorID: "main" },
          forkContext: fakeForkCtx,
        })

        const ctx = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(ctx).toBeDefined()
        expect(ctx?.context.system).toEqual(["test-system"])

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("subagent + none: no forkContext stored", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "matrix subagent+none",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "noop",
          context: "none",
          tools: [],
          background: true,
          model: ref,
          // no forkContext
        })

        const ctx = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(ctx).toBeUndefined()

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("peer + full: forkContext stored under child session id (result.actorID)", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "matrix peer+full",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        const registry = yield* ActorRegistry.Service
        yield* registry.updateModel(parent.id, "main", undefined, ref)
        const sourceUserID = MessageID.ascending()
        yield* session.updateMessage({
          id: sourceUserID, sessionID: parent.id, role: "user", agentID: "main", agent: "build", model: ref,
          time: { created: Date.now() },
        })
        yield* session.updatePart({
          id: PartID.ascending(), sessionID: parent.id, messageID: sourceUserID, type: "text", text: "captured source user",
        })

        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "explore",
          task: "noop",
          context: "full",
          tools: [],
          background: true,
          model: ref,
          parentActorID: "main",
          forkContextSource: { sessionID: parent.id, actorID: "main" },
          forkContext: fakeForkCtx,
        })

        // For peer, result.actorID === child.id (the new session id)
        expect(result.actorID).not.toBe(parent.id)
        const ctx = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(ctx).toBeDefined()
        expect(ctx?.context.system).toEqual(["test-system"])

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("peer + none: no forkContext stored", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "matrix peer+none",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.hang

        const result = yield* actor.spawn({
          mode: "peer",
          sessionID: parent.id,
          agentType: "explore",
          task: "noop",
          context: "none",
          tools: [],
          background: true,
          model: ref,
          // no forkContext
        })

        const ctx = yield* actor.getForkContext(result.sessionID, result.actorID)
        expect(ctx).toBeUndefined()

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn structured output (P3)", () => {
  const schema = {
    type: "object",
    additionalProperties: false,
    properties: { ok: { type: "boolean" }, count: { type: "number" } },
    required: ["ok", "count"],
  }

  it.live("format=json_schema → outcome.structured carries the validated object, finalText dropped", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "structured success",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        // Fake model emits a preamble text part AND calls StructuredOutput in the
        // SAME turn. Per §5.2 precedence, structured must win and finalText (the
        // preamble) must be dropped. (Must be one turn: a text-only turn under
        // json_schema would trip StructuredOutputError before a second turn runs.)
        yield* llm.push(reply().text("Here is my analysis…").tool("StructuredOutput", { ok: true, count: 3 }))

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "return structured",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
          format: { type: "json_schema", schema, retryCount: 2 },
        })

        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("success")
        if (outcome.status === "success") {
          expect(outcome.structured).toEqual({ ok: true, count: 3 })
          // §5.2: structured present → finalText dropped (no preamble duplication)
          expect(outcome.finalText).toBeUndefined()
        }
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("no format → outcome.finalText only, structured undefined", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "plain text",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("plain answer")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "return text",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })

        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("success")
        if (outcome.status === "success") {
          expect(outcome.structured).toBeUndefined()
          expect(outcome.finalText).toContain("plain answer")
        }
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("checkpoint-writer invalid output exhausting the unknown budget produces a failed actor outcome", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "invalid actor output",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        // A system-spawned agent retries empty output under the unknown budget,
        // configured here as 1 retry after the initial attempt; the server budget
        // stays persistent, so a run that used it would never settle.
        yield* llm.push(...Array.from({ length: 2 }, () => reply().stop()))

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "checkpoint-writer",
          task: "return a result",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })

        const outcome = yield* Deferred.await(result.outcome).pipe(Effect.timeout("10 seconds"))
        expect(outcome.status).toBe("failure")
      }),
      {
        git: true,
        config: (url: string) => ({
          ...providerCfg(url),
          retry: {
            unknown: { maxRetries: 1, initialDelayMs: 1, maxDelayMs: 1, deadlineMs: 1000 },
          },
        }),
      },
    ),
  )
})

describe("Actor.spawn onActorID pre-registration (MR104 #2)", () => {
  // The workflow runtime needs the child's actorID in its reclaim set BEFORE the
  // background work fiber detaches, otherwise a cancel that races an in-flight
  // spawn leaves an orphan. spawn exposes onActorID after forkWork parks and
  // registers its outer fiber, before the start gate opens. Proof:
  // by the time spawn RESOLVES to the caller, the callback has already run AND
  // carries the SAME actorID the registry was populated with — so any consumer
  // (the workflow) is guaranteed to know the id the instant the actor exists.
  it.live("onActorID fires with the registered actorID before spawn resolves", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const reg = yield* ActorRegistry.Service

        const parent = yield* session.create({
          title: "onActorID timing",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // Hang so the background fiber is alive — the callback must fire on the
        // SPAWN path (synchronously, pre-detach), not when the work completes.
        yield* llm.hang

        const seen: string[] = []
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "long task",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
          onActorID: (id) => seen.push(id),
        })

        // Resolved → callback ALREADY ran exactly once, with the resolved actorID.
        expect(seen).toEqual([result.actorID])
        // And the registry row for that id exists (register ran before the cb).
        const row = yield* reg.get(parent.id, result.actorID)
        expect(row?.actorID).toBe(result.actorID)

        yield* actor.cancel(result.sessionID, result.actorID, "forced")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("reclaim during the parked callback window interrupts the outer fiber", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "parked reclaim",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.hang
        const callbackID = yield* Deferred.make<string>()
        const reclaim = yield* Effect.forkChild(
          Deferred.await(callbackID).pipe(Effect.flatMap((actorID) => actor.cancel(parent.id, actorID, "forced"))),
        )
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "must not start",
          context: "none",
          tools: ["read"],
          background: true,
          model: ref,
          onActorID: (actorID) => Effect.runSync(Deferred.succeed(callbackID, actorID)),
        })
        yield* Fiber.join(reclaim)
        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("cancelled")
        expect(yield* llm.calls).toBe(0)
        const row = yield* ActorRegistry.Service.use((reg) => reg.get(parent.id, result.actorID))
        expect(row?.status).toBe("idle")
        expect(row?.lastOutcome).toBe("cancelled")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn concurrent same-session result isolation", () => {
  it.live("two concurrent subagents in one session return their own results", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "concurrent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        // Bind distinct LLM responses to each subagent by matching on the task
        // text in the request body. Each subagent's request carries ONLY its own
        // task (agent-scoped message slice), so the substring match is unambiguous.
        yield* llm.textMatch((h) => JSON.stringify(h.body).includes("task ALPHA"), "RESULT_ALPHA")
        yield* llm.textMatch((h) => JSON.stringify(h.body).includes("task BETA"), "RESULT_BETA")

        const [a, b] = yield* Effect.all(
          [
            actor.spawn({
              mode: "subagent",
              sessionID: parent.id,
              agentType: "build",
              task: "task ALPHA",
              context: "none",
              tools: ["read"],
              background: true,
              model: ref,
            }),
            actor.spawn({
              mode: "subagent",
              sessionID: parent.id,
              agentType: "build",
              task: "task BETA",
              context: "none",
              tools: ["read"],
              background: true,
              model: ref,
            }),
          ],
          { concurrency: "unbounded" },
        )

        const oa = yield* Deferred.await(a.outcome)
        const ob = yield* Deferred.await(b.outcome)
        const ra = oa.status === "success" ? oa.finalText : undefined
        const rb = ob.status === "success" ? ob.finalText : undefined

        // a is ALPHA, b is BETA — each must carry its OWN result. Pre-fix, the
        // session-wide lastAssistant lookup collapses both onto whichever actor
        // persisted last, so ra === rb (both BETA, the newest ascending ID).
        expect(ra).toBe("RESULT_ALPHA")
        expect(rb).toBe("RESULT_BETA")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

describe("Actor.spawn return-format injection (F21)", () => {
  it.live("non-specialized subagent (general) gets return-format instruction injected", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "F21 inject test",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "general",
          task: "do this thing",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })

        yield* Deferred.await(result.outcome)

        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const subAgentUser = msgs.find((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        expect(subAgentUser).toBeDefined()
        const text = subAgentUser?.parts.find((p) => p.type === "text")?.text ?? ""
        expect(text).toContain("Return format (required)")
        expect(text).toContain("**Status**:")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("explore subagent does NOT get return-format injection", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "F21 explore exclusion",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "search for foo",
          context: "none",
          tools: ["read", "grep", "glob"],
          background: false,
          model: ref,
        })

        yield* Deferred.await(result.outcome)

        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const subAgentUser = msgs.find((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        const text = subAgentUser?.parts.find((p) => p.type === "text")?.text ?? ""
        expect(text).not.toContain("Return format (required)")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("checkpoint-writer subagent does NOT get return-format injection", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service

        const parent = yield* session.create({
          title: "F21 writer exclusion",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })

        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "checkpoint-writer",
          task: "write the next checkpoint for this session",
          context: "full",
          tools: ["read", "write", "edit", "glob", "grep"],
          background: false,
          model: ref,
        })

        yield* Deferred.await(result.outcome)

        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const subAgentUser = msgs.find((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        const text = subAgentUser?.parts.find((p) => p.type === "text")?.text ?? ""
        expect(text).not.toContain("Return format (required)")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("a subagent's own window carries the session environment reminder but not memory paths", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "session env subagent",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "explore",
          task: "search for foo",
          context: "none",
          tools: ["read", "grep", "glob"],
          background: false,
          model: ref,
        })
        yield* Deferred.await(result.outcome)

        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const subAgentUser = msgs.find((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        const tags = (subAgentUser?.parts ?? []).flatMap((part) => SessionReminder.tag(part) ?? [])
        expect(tags).toContainEqual({ key: "session-env", rev: Instance.directory })
        expect(tags.some((tag) => tag.key === "memory-paths")).toBe(false)
        expect(JSON.stringify(yield* llm.inputs)).toContain(`- Session directory: ${Instance.directory}`)
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("a bounded-computation agent gets no session environment reminder", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "session env writer",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.text("done")

        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "checkpoint-writer",
          task: "write the next checkpoint for this session",
          context: "full",
          tools: ["read", "write", "edit", "glob", "grep"],
          background: false,
          model: ref,
        })
        yield* Deferred.await(result.outcome)

        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const writerUsers = msgs.filter((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        expect(writerUsers.length).toBeGreaterThan(0)
        for (const user of writerUsers)
          expect(user.parts.some((part) => SessionReminder.tag(part)?.key === "session-env")).toBe(false)
      }),
      { git: true, config: providerCfg },
    ),
  )
})

// ---------------------------------------------------------------------------
// AgentOutcome failure classification (see actor/spawn.ts FailureInfo).
//
// The point of these tests is that a consumer can tell a transient failure from
// a deterministic one WITHOUT string-matching `error`. Both drive the real
// construction path: runAgentLoop classifies the settled assistant error, raises
// the carrier, and forkWork's onFailure squashes it back out and assembles the
// AgentOutcome. Nothing here hand-builds an outcome object.
// ---------------------------------------------------------------------------
let settled: NonNullable<MessageV2.Assistant["error"]> | undefined
const itSettled = testEffect(makeLayer({ settledError: () => settled }))

describe("AgentOutcome failure classification", () => {
  itSettled.live("a transient provider failure is classified retryable/transient", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* () {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // A 429 the provider SDK itself marked retryable — SessionRetry.retryable
        // returns a status for it, which is the oracle spawn.ts reuses.
        settled = new MessageV2.APIError(
          { message: "Too Many Requests", statusCode: 429, isRetryable: true },
        ).toObject()
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "t",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("failure")
        if (outcome.status !== "failure") throw new Error("expected failure")
        // The human string is still there and unchanged.
        expect(outcome.error).toContain("Actor assistant failed: APIError")
        // ...and the classification is there alongside it.
        expect(outcome.failure).toBeTruthy()
        expect(outcome.failure?.retryable).toBe(true)
        expect(outcome.failure?.kind).toBe("transient")
        expect(outcome.failure?.name).toBe("APIError")
      }),
      { git: true, config: providerCfg },
    ),
  )

  itSettled.live("a deterministic overflow failure is classified non-retryable/overflow", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* () {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        // Context overflow is the canonical "will recur identically" failure:
        // ProviderError.isOverflow matched it upstream and SessionRetry.retryable
        // explicitly refuses to retry it.
        settled = new MessageV2.ContextOverflowError({
          message: "Input exceeds context window of this model",
        }).toObject()
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "t",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        expect(outcome.status).toBe("failure")
        if (outcome.status !== "failure") throw new Error("expected failure")
        expect(outcome.error).toContain("Actor assistant failed: ContextOverflowError")
        expect(outcome.failure).toBeTruthy()
        expect(outcome.failure?.retryable).toBe(false)
        expect(outcome.failure?.kind).toBe("overflow")
        expect(outcome.failure?.name).toBe("ContextOverflowError")
      }),
      { git: true, config: providerCfg },
    ),
  )

  itSettled.live("a rejected credential is classified non-retryable/auth", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* () {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        settled = new MessageV2.APIError(
          { message: "Unauthorized", statusCode: 401, isRetryable: false },
        ).toObject()
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "t",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        if (outcome.status !== "failure") throw new Error("expected failure")
        expect(outcome.failure?.retryable).toBe(false)
        expect(outcome.failure?.kind).toBe("auth")
      }),
      { git: true, config: providerCfg },
    ),
  )

  it.live("a failure that never reached a provider carries no classification", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "x",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "build",
          task: "t",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
        })
        const outcome = yield* Deferred.await(result.outcome)
        // A clean turn must not invent a classification; `failure` is only ever
        // present on a settled-assistant-error failure.
        if (outcome.status === "failure") expect(outcome.failure == null).toBe(true)
        else expect(outcome.status).toBe("success")
      }),
      { git: true, config: providerCfg },
    ),
  )
})

// docs/compose/spec/reasoning-variants.md S2.3 — the spawn variant reaches the child's user message.
describe("Actor.spawn reasoning variant", () => {
  it.live("records the given variant on the child user message", () =>
    provideTmpdirServer(
      Effect.fnUntraced(function* ({ llm }) {
        const actor = yield* Actor.Service
        const session = yield* Session.Service
        const parent = yield* session.create({
          title: "variant passthrough",
          permission: [{ permission: "*", pattern: "*", action: "allow" }],
        })
        yield* llm.text("done")
        const result = yield* actor.spawn({
          mode: "subagent",
          sessionID: parent.id,
          agentType: "general",
          task: "think hard",
          context: "none",
          tools: ["read"],
          background: false,
          model: ref,
          variant: "high",
        })
        yield* Deferred.await(result.outcome)
        const msgs = yield* session.messages({ sessionID: result.sessionID, agentID: "*" })
        const childUser = msgs.find((m) => m.info.role === "user" && m.info.agentID === result.actorID)
        expect(childUser?.info.role === "user" ? childUser.info.model.variant : "not-user").toBe("high")
      }),
      { git: true, config: providerCfg },
    ),
  )
})
