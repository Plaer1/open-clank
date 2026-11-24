import { NodeFileSystem } from "@effect/platform-node"
import { beforeEach, expect } from "bun:test"
import { Cause, Effect, Exit, Fiber, Layer, Option, Stream } from "effect"
import path from "path"
import { tool } from "ai"
import { z } from "zod"
import type { Agent } from "../../src/agent/agent"
import { Agent as AgentSvc } from "../../src/agent/agent"
import { Bus } from "../../src/bus"
import { Config } from "../../src/config"
import { Permission } from "../../src/permission"
import { Plugin } from "../../src/plugin"
import { Provider } from "../../src/provider"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { Session } from "../../src/session"
import { LLM } from "../../src/session/llm"
import { MessageV2 } from "../../src/session/message-v2"
import { SessionProcessor } from "../../src/session/processor"
import { MessageID, PartID, SessionID } from "../../src/session/schema"
import { SessionStatus } from "../../src/session/status"
import { SessionSummary } from "../../src/session/summary"
import { Snapshot } from "../../src/snapshot"
import { ManagedProvider } from "../../src/acp/managed-provider"
import * as EffectBridge from "../../src/effect/bridge"
import { Log } from "../../src/util"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance, provideTmpdirServer } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { raw, reply, TestLLMServer } from "../lib/llm-server"
import { resetAllMonitors } from "../../src/session/try-best-detector"

void Log.init({ print: false })

beforeEach(() => {
  resetAllMonitors()
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

const ref = {
  providerID: ProviderID.make("test"),
  modelID: ModelID.make("test-model"),
}

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

function providerCfgWithOpenAI(url: string) {
  const base = providerCfg(url)
  return {
    ...base,
    provider: {
      ...base.provider,
      openai: {
        ...base.provider.test,
        id: "openai",
        name: "OpenAI fixture",
      },
    },
  }
}

function agent(): Agent.Info {
  return {
    name: "build",
    mode: "primary",
    options: {},
    permission: [{ permission: "*", pattern: "*", action: "allow" }],
  }
}

function defer<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}

const user = Effect.fn("TestSession.user")(function* (sessionID: SessionID, text: string) {
  const session = yield* Session.Service
  const msg = yield* session.updateMessage({
    id: MessageID.ascending(),
    role: "user",
    sessionID,
    agent: "build",
    model: ref,
    time: { created: Date.now() },
  })
  yield* session.updatePart({
    id: PartID.ascending(),
    messageID: msg.id,
    sessionID,
    type: "text",
    text,
  })
  return msg
})

const assistant = Effect.fn("TestSession.assistant")(function* (
  sessionID: SessionID,
  parentID: MessageID,
  root: string,
) {
  const session = yield* Session.Service
  const msg: MessageV2.Assistant = {
    id: MessageID.ascending(),
    role: "assistant",
    sessionID,
    mode: "build",
    agent: "build",
    path: { cwd: root, root },
    cost: 0,
    tokens: {
      total: 0,
      input: 0,
      output: 0,
      reasoning: 0,
      cache: { read: 0, write: 0 },
    },
    modelID: ref.modelID,
    providerID: ref.providerID,
    parentID,
    time: { created: Date.now() },
    finish: "end_turn",
  }
  yield* session.updateMessage(msg)
  return msg
})

const status = SessionStatus.layer.pipe(Layer.provideMerge(Bus.layer))
const infra = Layer.mergeAll(NodeFileSystem.layer, CrossSpawnSpawner.defaultLayer)
const deps = Layer.mergeAll(
  Session.defaultLayer,
  Snapshot.defaultLayer,
  AgentSvc.defaultLayer,
  Permission.defaultLayer,
  Plugin.defaultLayer,
  Config.defaultLayer,
  LLM.defaultLayer,
  Provider.defaultLayer,
  status,
).pipe(Layer.provideMerge(infra))
const env = Layer.mergeAll(
  TestLLMServer.layer,
  SessionProcessor.layer.pipe(Layer.provide(summary), Layer.provideMerge(deps)),
)

let fakeSnapshotTrack: (options?: Snapshot.CaptureOptions) => Effect.Effect<string | undefined> = () => Effect.succeed("a".repeat(40))
let fakeSnapshotPatch: (hash: string) => Effect.Effect<Snapshot.Patch> = (hash) => Effect.succeed({ hash, files: [] })
const fakeSnapshotService = Snapshot.Service.of({
  init: () => Effect.void,
  cleanup: () => Effect.void,
  track: (options) => Effect.suspend(() => fakeSnapshotTrack(options)),
  patch: (hash) => Effect.suspend(() => fakeSnapshotPatch(hash)),
  restore: () => Effect.void,
  revert: () => Effect.void,
  diff: () => Effect.succeed(""),
  diffFull: () => Effect.succeed([]),
})
const fakeSnapshotDeps = Layer.mergeAll(
  Session.defaultLayer,
  AgentSvc.defaultLayer,
  Permission.defaultLayer,
  Plugin.defaultLayer,
  Config.defaultLayer,
  LLM.defaultLayer,
  Provider.defaultLayer,
  status,
).pipe(Layer.provideMerge(infra))
const fakeSnapshotEnv = Layer.mergeAll(
  TestLLMServer.layer,
  SessionProcessor.layer
    .pipe(Layer.provide(summary), Layer.provideMerge(fakeSnapshotDeps), Layer.provideMerge(Layer.succeed(Snapshot.Service, fakeSnapshotService))),
)

let fakeReplayHookActive = false
const replayPluginService = {
  trigger: (name: string, _input: unknown, output: unknown) =>
    Effect.gen(function* () {
      if (name === "experimental.text.complete") {
        fakeReplayHookActive = true
        const barrier = yield* Effect.serviceOption(Snapshot.CaptureBarrier)
        if (Option.isSome(barrier)) yield* barrier.value()
      }
      return output
    }),
} as unknown as Plugin.Interface
const replayPluginDeps = Layer.mergeAll(
  Session.defaultLayer,
  AgentSvc.defaultLayer,
  Permission.defaultLayer,
  Config.defaultLayer,
  LLM.defaultLayer,
  Provider.defaultLayer,
  status,
  Layer.succeed(Plugin.Service, replayPluginService),
).pipe(Layer.provideMerge(infra))
const replayPluginEnv = Layer.mergeAll(
  TestLLMServer.layer,
  SessionProcessor.layer
    .pipe(Layer.provide(summary), Layer.provideMerge(replayPluginDeps), Layer.provideMerge(Layer.succeed(Snapshot.Service, fakeSnapshotService))),
)

const it = testEffect(env)
const itFakeSnapshot = testEffect(fakeSnapshotEnv)
const itReplaySnapshot = testEffect(replayPluginEnv)

let fakeStepMode: "empty" | "valid" | "partial" = "empty"
let fakeStepCalls = 0
const fakeStepLLM = Layer.succeed(LLM.Service, LLM.Service.of({
  stream: () => {
    fakeStepCalls++
    if (fakeStepMode === "empty") return Stream.empty
    if (fakeStepMode === "partial") return Stream.fromIterable([
      { type: "text-start", id: "t" }, { type: "text-delta", id: "t", text: "kept" },
      { type: "error", error: Object.assign(new Error("network error"), { status: 503 }) },
    ] as any[])
    return Stream.fromIterable([{ type: "finish-step", finishReason: "stop", usage: { inputTokens: 0, outputTokens: 0, totalTokens: 0 } }] as any[])
  },
} as unknown as LLM.Interface))
const fakeStepDeps = Layer.mergeAll(Session.defaultLayer, AgentSvc.defaultLayer, Permission.defaultLayer,
  Plugin.defaultLayer, Config.defaultLayer, fakeStepLLM, Provider.defaultLayer, status).pipe(Layer.provideMerge(infra))
const fakeStepEnv = SessionProcessor.layer.pipe(
  Layer.provide(summary), Layer.provideMerge(fakeStepDeps), Layer.provideMerge(Layer.succeed(Snapshot.Service, fakeSnapshotService)))
const itFakeStep = testEffect(fakeStepEnv)

const boot = Effect.fn("test.boot")(function* () {
  const processors = yield* SessionProcessor.Service
  const session = yield* Session.Service
  const provider = yield* Provider.Service
  return { processors, session, provider }
})

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

it.live("session.processor effect tests capture llm input cleanly", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.text("hello")

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "hi")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const input = {
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "hi" }],
          tools: {},
        } satisfies LLM.StreamInput

        const value = yield* handle.process(input)
        const parts = MessageV2.parts(msg.id)
        const calls = yield* llm.calls

        expect(value).toBe("continue")
        expect(calls).toBe(1)
        expect(parts.some((part) => part.type === "text" && part.text === "hello")).toBe(true)
        expect(parts.find((part) => part.type === "step-start")?.snapshot).toBeTruthy()
        expect(parts.find((part) => part.type === "step-finish")?.snapshot).toBeTruthy()
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor preserves eager standalone snapshots around tool calls", () =>
  provideTmpdirServer(
    ({ dir }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "inspect the workspace")
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)

        const quiet = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const quietHandle = yield* processors.create({ assistantMessage: quiet, sessionID: chat.id, model: mdl })
        yield* quietHandle.replay({ finishReason: "stop", toolCalls: [], tools: {}, messages: [] })
        const quietParts = MessageV2.parts(quiet.id)
        expect(quietParts.find((part) => part.type === "step-start")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)
        expect(quietParts.find((part) => part.type === "step-finish")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)

        const changed = yield* assistant(chat.id, quiet.id, path.resolve(dir))
        const changedHandle = yield* processors.create({ assistantMessage: changed, sessionID: chat.id, model: mdl })
        yield* changedHandle.replay({
          finishReason: "tool-calls",
          toolCalls: [{ toolCallId: "snapshot-call", toolName: "mutate", input: {} }],
          tools: {
            mutate: {
              execute: async () => {
                await Bun.write(path.join(dir, "snapshot-barrier-fixture.txt"), "captured before mutation")
                return { title: "Mutated", metadata: {}, output: "done" }
              },
            },
          },
          messages: [],
        })
        const changedParts = MessageV2.parts(changed.id)
        expect(changedParts.find((part) => part.type === "step-start")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)
        expect(changedParts.find((part) => part.type === "step-finish")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)
        expect(changedParts.some((part) => part.type === "patch" && part.files.some((file) => file.includes("snapshot-barrier-fixture.txt")))).toBe(true)
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor skips snapshots for a tool-free managed step", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "hello")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const openaiID = ProviderID.make("openai")
        const mdl = yield* provider.getModel(openaiID, ref.modelID)
        msg.providerID = openaiID
        msg.modelID = mdl.id
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        try {
          return yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-fixture", workspaceID: "workspace-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      yield* llm.text("hello")
                      yield* handle.process({
                        user: {
                          id: parent.id,
                          sessionID: chat.id,
                          role: "user",
                          time: parent.time,
                          agent: parent.agent,
                          model: { providerID: mdl.providerID, modelID: mdl.id },
                        },
                        sessionID: chat.id,
                        model: mdl,
                        agent: agent(),
                        system: [],
                        messages: [{ role: "user", content: "hello" }],
                        tools: {},
                      })
                      const parts = MessageV2.parts(msg.id)
                      expect(parts.find((part) => part.type === "step-start")?.snapshot).toBeUndefined()
                      expect(parts.find((part) => part.type === "step-finish")?.snapshot).toBeUndefined()

                      const changed = yield* assistant(chat.id, msg.id, path.resolve(dir))
                      changed.providerID = openaiID
                      changed.modelID = mdl.id
                      const changedHandle = yield* processors.create({
                        assistantMessage: changed,
                        sessionID: chat.id,
                        model: mdl,
                      })
                      yield* changedHandle.replay({
                        finishReason: "tool-calls",
                        toolCalls: [{ toolCallId: "managed-snapshot-call", toolName: "mutate", input: {} }],
                        tools: {
                          mutate: {
                            execute: async () => {
                              await Bun.write(path.join(dir, "managed-snapshot-fixture.txt"), "captured before mutation")
                              return { title: "Mutated", metadata: {}, output: "done" }
                            },
                          },
                        },
                        messages: [],
                      })
                      const changedParts = MessageV2.parts(changed.id)
                      expect(changedParts.find((part) => part.type === "step-start")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)
                      expect(changedParts.find((part) => part.type === "step-finish")?.snapshot).toMatch(/^[0-9a-f]{40,64}$/i)
                      expect(changedParts.some((part) => part.type === "patch" && part.files.some((file) => file.includes("managed-snapshot-fixture.txt")))).toBe(true)
                      return { quiet: parts, changed: changedParts }
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
        }
      }),
    { git: true, config: (url) => providerCfgWithOpenAI(url) },
  ),
)

itFakeSnapshot.live("session.processor preserves tool results when the post-mutation snapshot fails", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        let trackCalls = 0
        fakeSnapshotTrack = () => {
          trackCalls += 1
          return trackCalls === 1
            ? Effect.succeed("b".repeat(40))
            : Effect.die(new Error("fixture final capture failure"))
        }
        fakeSnapshotPatch = () => Effect.die(new Error("fixture patch failure"))

        yield* llm.push(
          raw({
            head: [
              { id: "chatcmpl-snapshot", object: "chat.completion.chunk", choices: [{ delta: { role: "assistant" } }] },
              {
                id: "chatcmpl-snapshot",
                object: "chat.completion.chunk",
                choices: [{ delta: { tool_calls: [{ index: 0, id: "call_snapshot", type: "function", function: { name: "mutate", arguments: "{}" } }] } }],
              },
              { id: "chatcmpl-snapshot", object: "chat.completion.chunk", choices: [{ delta: {}, finish_reason: "tool_calls" }] },
            ],
          }),
        )

        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "change the workspace")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        try {
          return yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-failure-fixture", workspaceID: "workspace-failure-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      const mutateTool = tool({
                        description: "mutate workspace",
                        inputSchema: z.object({}),
                        execute: async () => {
                          await Bun.write(path.join(dir, "postcapture-fixture.txt"), "already applied")
                          return "applied"
                        },
                      })
                      const result = yield* handle.process({
                        user: {
                          id: parent.id,
                          sessionID: chat.id,
                          role: "user",
                          time: parent.time,
                          agent: parent.agent,
                          model: { providerID: ref.providerID, modelID: ref.modelID },
                        },
                        sessionID: chat.id,
                        model: mdl,
                        agent: agent(),
                        system: [],
                        messages: [{ role: "user", content: "change the workspace" }],
                        tools: { mutate: mutateTool },
                      })
                      const parts = MessageV2.parts(msg.id)
                      expect(result).toBe("continue")
                      expect(trackCalls).toBe(2)
                      expect(parts.some((part) => part.type === "tool" && part.state.status === "completed")).toBe(true)
                      expect(parts.some((part) => part.type === "text" && part.synthetic && part.metadata?.origin?.kind === "snapshot_recovery")).toBe(true)
                      expect(handle.message.error).toBeUndefined()
                      expect(yield* Effect.promise(() => Bun.file(path.join(dir, "postcapture-fixture.txt")).exists())).toBe(true)
                      return parts
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
          fakeSnapshotTrack = () => Effect.succeed("a".repeat(40))
          fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

itFakeSnapshot.live("session.processor blocks tool execution when strict pre-capture fails", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        let strictRequested = false
        let toolStarted = false
        fakeSnapshotTrack = (options) => {
          strictRequested = options?.strict === true
          return Effect.die(new Snapshot.CaptureError("index_stage"))
        }
        yield* llm.push(
          raw({
            head: [
              { id: "chatcmpl-strict-capture", object: "chat.completion.chunk", choices: [{ delta: { role: "assistant" } }] },
              {
                id: "chatcmpl-strict-capture",
                object: "chat.completion.chunk",
                choices: [{ delta: { tool_calls: [{ index: 0, id: "call_strict", type: "function", function: { name: "mutate", arguments: "{}" } }] } }],
              },
              { id: "chatcmpl-strict-capture", object: "chat.completion.chunk", choices: [{ delta: {}, finish_reason: "tool_calls" }] },
            ],
          }),
        )
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "run a tool")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        try {
          yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-strict-fixture", workspaceID: "workspace-strict-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      yield* handle.process({
                        user: { id: parent.id, sessionID: chat.id, role: "user", time: parent.time, agent: parent.agent, model: { providerID: ref.providerID, modelID: ref.modelID } },
                        sessionID: chat.id,
                        model: mdl,
                        agent: agent(),
                        system: [],
                        messages: [{ role: "user", content: "run a tool" }],
                        tools: {
                          mutate: tool({ description: "mutate", inputSchema: z.object({}), execute: async () => { toolStarted = true; return "applied" } }),
                        },
                      }).pipe(Effect.catchCause(() => Effect.void))
                      expect(strictRequested).toBe(true)
                      expect(toolStarted).toBe(false)
                      expect(yield* llm.calls).toBe(1)
                      expect(MessageV2.parts(msg.id).some((part) => part.type === "tool" && part.state.status === "completed")).toBe(false)
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
          fakeSnapshotTrack = () => Effect.succeed("a".repeat(40))
          fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

itReplaySnapshot.live("session.processor replay barriers active text hooks and cancels pending capture", () =>
  provideTmpdirServer(
    ({ dir }) =>
      Effect.gen(function* () {
        let captureStarted = false
        let captureCancelled = false
        fakeReplayHookActive = false
        fakeSnapshotTrack = () =>
          Effect.sync(() => { captureStarted = true }).pipe(
            Effect.andThen(Effect.never),
            Effect.onInterrupt(() => Effect.sync(() => { captureCancelled = true })),
          )
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "replay text")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        try {
          yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-replay-fixture", workspaceID: "workspace-replay-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      const running = yield* handle.replay({
                        text: "replayed",
                        toolCalls: [],
                        tools: {},
                        messages: [],
                        finishReason: "stop",
                      }).pipe(Effect.forkChild)
                      const deadline = Date.now() + 3000
                      while (!captureStarted && Date.now() < deadline) yield* Effect.sleep("10 millis")
                      expect(captureStarted).toBe(true)
                      expect(fakeReplayHookActive).toBe(true)
                      yield* Fiber.interrupt(running)
                      expect(captureCancelled).toBe(true)
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
          fakeSnapshotTrack = () => Effect.succeed("a".repeat(40))
          fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

itFakeSnapshot.live("session.processor shares one pre-capture across tool calls in a step", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        let trackCalls = 0
        fakeSnapshotTrack = () => Effect.sync(() => (++trackCalls === 1 ? "c".repeat(40) : "d".repeat(40)))
        fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        yield* llm.push(
          raw({
            head: [
              { id: "chatcmpl-parallel", object: "chat.completion.chunk", choices: [{ delta: { role: "assistant" } }] },
              {
                id: "chatcmpl-parallel",
                object: "chat.completion.chunk",
                choices: [{ delta: { tool_calls: [
                  { index: 0, id: "call_a", type: "function", function: { name: "first", arguments: "{}" } },
                  { index: 1, id: "call_b", type: "function", function: { name: "second", arguments: "{}" } },
                ] } }],
              },
              { id: "chatcmpl-parallel", object: "chat.completion.chunk", choices: [{ delta: {}, finish_reason: "tool_calls" }] },
            ],
          }),
        )

        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "run both tools")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        const started: string[] = []
        try {
          return yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-parallel-fixture", workspaceID: "workspace-parallel-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      const makeTool = (name: string) => tool({
                        description: `run ${name}`,
                        inputSchema: z.object({}),
                        execute: async () => {
                          started.push(name)
                          return `${name} done`
                        },
                      })
                      yield* handle.process({
                        user: { id: parent.id, sessionID: chat.id, role: "user", time: parent.time, agent: parent.agent, model: { providerID: ref.providerID, modelID: ref.modelID } },
                        sessionID: chat.id,
                        model: mdl,
                        agent: agent(),
                        system: [],
                        messages: [{ role: "user", content: "run both tools" }],
                        tools: { first: makeTool("first"), second: makeTool("second") },
                      })
                      expect(trackCalls).toBe(2)
                      expect(started.sort()).toEqual(["first", "second"])
                      expect(MessageV2.parts(msg.id).filter((part) => part.type === "tool" && part.state.status === "completed")).toHaveLength(2)
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
          fakeSnapshotTrack = () => Effect.succeed("a".repeat(40))
          fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

itFakeSnapshot.live("session.processor cancellation interrupts a pending shared capture before tools run", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        let captureStarted = false
        let captureCancelled = false
        let toolStarted = false
        fakeSnapshotTrack = () =>
          Effect.sync(() => { captureStarted = true }).pipe(
            Effect.andThen(Effect.never),
            Effect.onInterrupt(() => Effect.sync(() => { captureCancelled = true })),
          )
        yield* llm.push(
          raw({
            head: [
              { id: "chatcmpl-cancel", object: "chat.completion.chunk", choices: [{ delta: { role: "assistant" } }] },
              {
                id: "chatcmpl-cancel",
                object: "chat.completion.chunk",
                choices: [{ delta: { tool_calls: [{ index: 0, id: "call_cancel", type: "function", function: { name: "mutate", arguments: "{}" } }] } }],
              },
              { id: "chatcmpl-cancel", object: "chat.completion.chunk", choices: [{ delta: {}, finish_reason: "tool_calls" }] },
            ],
          }),
        )
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "cancel before execution")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const bridge = yield* EffectBridge.make()
        const previous = process.env.OPEN_CLANK_MANAGED
        process.env.OPEN_CLANK_MANAGED = "1"
        try {
          yield* Effect.tryPromise({
            try: () =>
              ManagedProvider.withHostContext(
                chat.id,
                { chatID: "chat-managed-cancel-fixture", workspaceID: "workspace-cancel-fixture", cwd: path.resolve(dir) },
                () =>
                  bridge.promise(
                    Effect.gen(function* () {
                      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model: mdl })
                      if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
                      else process.env.OPEN_CLANK_MANAGED = previous
                      const running = yield* handle.process({
                        user: { id: parent.id, sessionID: chat.id, role: "user", time: parent.time, agent: parent.agent, model: { providerID: ref.providerID, modelID: ref.modelID } },
                        sessionID: chat.id,
                        model: mdl,
                        agent: agent(),
                        system: [],
                        messages: [{ role: "user", content: "cancel before execution" }],
                        tools: {
                          mutate: tool({ description: "mutate", inputSchema: z.object({}), execute: async () => { toolStarted = true; return "applied" } }),
                        },
                      }).pipe(Effect.forkChild)
                      const deadline = Date.now() + 3000
                      while (!captureStarted && Date.now() < deadline) yield* Effect.sleep("10 millis")
                      expect(captureStarted).toBe(true)
                      yield* Fiber.interrupt(running)
                      expect(captureCancelled).toBe(true)
                      expect(toolStarted).toBe(false)
                    }),
                  ),
              ),
            catch: (error) => error,
          })
        } finally {
          if (previous === undefined) delete process.env.OPEN_CLANK_MANAGED
          else process.env.OPEN_CLANK_MANAGED = previous
          fakeSnapshotTrack = () => Effect.succeed("a".repeat(40))
          fakeSnapshotPatch = (hash) => Effect.succeed({ hash, files: [] })
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests preserve text start time", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const gate = defer<void>()
        const { processors, session, provider } = yield* boot()

        yield* llm.push(
          raw({
            head: [
              {
                id: "chatcmpl-test",
                object: "chat.completion.chunk",
                choices: [{ delta: { role: "assistant" } }],
              },
              {
                id: "chatcmpl-test",
                object: "chat.completion.chunk",
                choices: [{ delta: { content: "hello" } }],
              },
            ],
            wait: gate.promise,
            tail: [
              {
                id: "chatcmpl-test",
                object: "chat.completion.chunk",
                choices: [{ delta: {}, finish_reason: "stop" }],
              },
            ],
          }),
        )

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "hi")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const run = yield* handle
          .process({
            user: {
              id: parent.id,
              sessionID: chat.id,
              role: "user",
              time: parent.time,
              agent: parent.agent,
              model: { providerID: ref.providerID, modelID: ref.modelID },
            } satisfies MessageV2.User,
            sessionID: chat.id,
            model: mdl,
            agent: agent(),
            system: [],
            messages: [{ role: "user", content: "hi" }],
            tools: {},
          })
          .pipe(Effect.forkChild)

        yield* Effect.promise(async () => {
          const stop = Date.now() + 500
          while (Date.now() < stop) {
            const text = MessageV2.parts(msg.id).find((part): part is MessageV2.TextPart => part.type === "text")
            if (text?.time?.start) return
            await Bun.sleep(10)
          }
          throw new Error("timed out waiting for text part")
        })
        yield* Effect.sleep("20 millis")
        gate.resolve()

        const exit = yield* Fiber.await(run)
        const text = MessageV2.parts(msg.id).find((part): part is MessageV2.TextPart => part.type === "text")

        expect(Exit.isSuccess(exit)).toBe(true)
        expect(text?.text).toBe("hello")
        expect(text?.time?.start).toBeDefined()
        expect(text?.time?.end).toBeDefined()
        if (!text?.time?.start || !text.time.end) return
        expect(text.time.start).toBeLessThan(text.time.end)
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests stop after token overflow requests compaction", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.text("after", { usage: { input: 100, output: 0 } })

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "compact")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const base = yield* provider.getModel(ref.providerID, ref.modelID)
        const mdl = { ...base, limit: { context: 20, output: 10 } }
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "compact" }],
          tools: {},
        })

        const parts = MessageV2.parts(msg.id)

        expect(value).toBe("overflow")
        expect(parts.some((part) => part.type === "text" && part.text === "after")).toBe(true)
        expect(parts.some((part) => part.type === "step-finish")).toBe(true)
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests capture reasoning from http mock", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.push(reply().reason("think").text("done").stop())

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "reason")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "reason" }],
          tools: {},
        })

        const parts = MessageV2.parts(msg.id)
        const reasoning = parts.find((part): part is MessageV2.ReasoningPart => part.type === "reasoning")
        const text = parts.find((part): part is MessageV2.TextPart => part.type === "text")

        expect(value).toBe("continue")
        expect(yield* llm.calls).toBe(1)
        expect(reasoning?.text).toBe("think")
        expect(text?.text).toBe("done")
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests reset reasoning state across retries", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.push(reply().reason("one").reset(), reply().reason("two").stop())

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "reason")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "reason" }],
          tools: {},
        })

        const parts = MessageV2.parts(msg.id)
        const reasoning = parts.filter((part): part is MessageV2.ReasoningPart => part.type === "reasoning")

        expect(value).toBe("continue")
        expect(yield* llm.calls).toBe(2)
        expect(reasoning.some((part) => part.text === "two")).toBe(true)
        expect(reasoning.some((part) => part.text === "onetwo")).toBe(false)
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests do not retry unknown json errors", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.error(400, { error: { message: "no_kv_space" } })

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "json")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "json" }],
          tools: {},
        })

        expect(value).toBe("stop")
        expect(yield* llm.calls).toBe(1)
        expect(handle.message.error?.name).toBe("APIError")
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests retry recognized structured json errors", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.error(429, { type: "error", error: { type: "too_many_requests" } })
        yield* llm.text("after")

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "retry json")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "retry json" }],
          tools: {},
        })

        const parts = MessageV2.parts(msg.id)

        expect(value).toBe("continue")
        expect(yield* llm.calls).toBe(2)
        expect(parts.some((part) => part.type === "text" && part.text === "after")).toBe(true)
        expect(handle.message.error).toBeUndefined()
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

// TODO: Re-enable after we restructure the retry-status path.
// Task 1 of docs/superpowers/plans/2026-05-17-retry-and-timeout-tuning.md
// bumped streamText maxRetries 0→10. AI SDK now consumes 503 (and other
// transient HTTP errors) inside its own exp-backoff loop, so the outer
// Effect-based SessionRetry.policy at processor.ts:568 never sees this
// error and the `type: "retry"` status banner never publishes for the
// single-503 fixture this test uses. The user-facing contract changed:
// silent retries during the SDK window, banner only when AI SDK gives
// up after 10+ retries. A proper rewrite would either inject 11+
// errors (so AI SDK exhausts then outer retry fires) or use a
// non-AI-SDK-retryable error path.
it.live.skip("session.processor effect tests publish retry status updates", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const bus = yield* Bus.Service

        yield* llm.error(503, { error: "boom" })
        yield* llm.text("")

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "retry")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const states: number[] = []
        const off = yield* bus.subscribeCallback(SessionStatus.Event.Status, (evt) => {
          if (evt.properties.sessionID !== chat.id) return
          if (evt.properties.status.type === "retry") states.push(evt.properties.status.attempt)
        })
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "retry" }],
          tools: {},
        })

        off()

        expect(value).toBe("continue")
        expect(yield* llm.calls).toBe(2)
        expect(states).toStrictEqual([1])
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests compact on structured context overflow", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.error(400, { type: "error", error: { code: "context_length_exceeded" } })

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "compact json")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const value = yield* handle.process({
          user: {
            id: parent.id,
            sessionID: chat.id,
            role: "user",
            time: parent.time,
            agent: parent.agent,
            model: { providerID: ref.providerID, modelID: ref.modelID },
          } satisfies MessageV2.User,
          sessionID: chat.id,
          model: mdl,
          agent: agent(),
          system: [],
          messages: [{ role: "user", content: "compact json" }],
          tools: {},
        })

        expect(value).toBe("overflow")
        expect(yield* llm.calls).toBe(1)
        expect(handle.message.error).toBeUndefined()
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests mark pending tools as aborted on cleanup", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()

        yield* llm.toolHang("bash", { cmd: "pwd" })

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "tool abort")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const run = yield* handle
          .process({
            user: {
              id: parent.id,
              sessionID: chat.id,
              role: "user",
              time: parent.time,
              agent: parent.agent,
              model: { providerID: ref.providerID, modelID: ref.modelID },
            } satisfies MessageV2.User,
            sessionID: chat.id,
            model: mdl,
            agent: agent(),
            system: [],
            messages: [{ role: "user", content: "tool abort" }],
            tools: {},
          })
          .pipe(Effect.forkChild)

        yield* llm.wait(1)
        yield* Effect.promise(async () => {
          const end = Date.now() + 500
          while (Date.now() < end) {
            const parts = await MessageV2.parts(msg.id)
            if (parts.some((part) => part.type === "tool")) return
            await Bun.sleep(10)
          }
        })
        yield* Fiber.interrupt(run)

        const exit = yield* Fiber.await(run)
        const parts = MessageV2.parts(msg.id)
        const call = parts.find((part): part is MessageV2.ToolPart => part.type === "tool")

        expect(Exit.isFailure(exit)).toBe(true)
        if (Exit.isFailure(exit)) {
          expect(Cause.hasInterruptsOnly(exit.cause)).toBe(true)
        }
        expect(yield* llm.calls).toBe(1)
        expect(call?.state.status).toBe("error")
        if (call?.state.status === "error") {
          expect(call.state.error).toBe("Tool execution aborted")
          expect(call.state.metadata?.interrupted).toBe(true)
          expect(call.state.time.end).toBeDefined()
        }
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests record aborted errors and idle state", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const seen = defer<void>()
        const { processors, session, provider } = yield* boot()
        const bus = yield* Bus.Service
        const sts = yield* SessionStatus.Service

        yield* llm.hang

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "abort")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const errs: string[] = []
        const off = yield* bus.subscribeCallback(Session.Event.Error, (evt) => {
          if (evt.properties.sessionID !== chat.id) return
          if (!evt.properties.error) return
          errs.push(evt.properties.error.name)
          seen.resolve()
        })
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const run = yield* handle
          .process({
            user: {
              id: parent.id,
              sessionID: chat.id,
              role: "user",
              time: parent.time,
              agent: parent.agent,
              model: { providerID: ref.providerID, modelID: ref.modelID },
            } satisfies MessageV2.User,
            sessionID: chat.id,
            model: mdl,
            agent: agent(),
            system: [],
            messages: [{ role: "user", content: "abort" }],
            tools: {},
          })
          .pipe(Effect.forkChild)

        yield* llm.wait(1)
        yield* Fiber.interrupt(run)

        const exit = yield* Fiber.await(run)
        yield* Effect.promise(() => seen.promise)
        const stored = MessageV2.get({ sessionID: chat.id, messageID: msg.id })
        const state = yield* sts.get(chat.id)
        off()

        expect(Exit.isFailure(exit)).toBe(true)
        if (Exit.isFailure(exit)) {
          expect(Cause.hasInterruptsOnly(exit.cause)).toBe(true)
        }
        expect(handle.message.error?.name).toBe("MessageAbortedError")
        expect(stored.info.role).toBe("assistant")
        if (stored.info.role === "assistant") {
          expect(stored.info.error?.name).toBe("MessageAbortedError")
        }
        expect(state).toMatchObject({ type: "idle" })
        expect(errs).toContain("MessageAbortedError")
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live("session.processor effect tests mark interruptions aborted without manual abort", () =>
  provideTmpdirServer(
    ({ dir, llm }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const sts = yield* SessionStatus.Service

        yield* llm.hang

        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "interrupt")
        const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const handle = yield* processors.create({
          assistantMessage: msg,
          sessionID: chat.id,
          model: mdl,
        })

        const run = yield* handle
          .process({
            user: {
              id: parent.id,
              sessionID: chat.id,
              role: "user",
              time: parent.time,
              agent: parent.agent,
              model: { providerID: ref.providerID, modelID: ref.modelID },
            } satisfies MessageV2.User,
            sessionID: chat.id,
            model: mdl,
            agent: agent(),
            system: [],
            messages: [{ role: "user", content: "interrupt" }],
            tools: {},
          })
          .pipe(Effect.forkChild)

        yield* llm.wait(1)
        yield* Fiber.interrupt(run)

        const exit = yield* Fiber.await(run)
        const stored = MessageV2.get({ sessionID: chat.id, messageID: msg.id })
        const state = yield* sts.get(chat.id)

        expect(Exit.isFailure(exit)).toBe(true)
        expect(handle.message.error?.name).toBe("MessageAbortedError")
        expect(stored.info.role).toBe("assistant")
        if (stored.info.role === "assistant") {
          expect(stored.info.error?.name).toBe("MessageAbortedError")
        }
        expect(state).toMatchObject({ type: "idle" })
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live.skip("session.processor pauses after three repeated failed bash commands", () =>
  provideTmpdirServer(
    ({ dir }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "fix the failing tests")
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const tools = {
          bash: {
            execute: async () => ({
              title: "Run tests",
              metadata: { exit: 1 },
              output: "1 test failed",
            }),
          },
        }
        const call = (id: string) => ({
          reasoning: "run tests",
          toolCalls: [{ toolCallId: id, toolName: "bash", input: { command: "bun test" } }],
          finishReason: "tool-calls",
          tools,
          messages: [],
        })

        const first = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const firstHandle = yield* processors.create({ assistantMessage: first, sessionID: chat.id, model: mdl })
        expect(yield* firstHandle.replay(call("call_first"))).toBe("continue")

        const second = yield* assistant(chat.id, first.id, path.resolve(dir))
        const secondHandle = yield* processors.create({ assistantMessage: second, sessionID: chat.id, model: mdl })
        expect(yield* secondHandle.replay(call("call_second"))).toBe("continue")

        const third = yield* assistant(chat.id, second.id, path.resolve(dir))
        const thirdHandle = yield* processors.create({ assistantMessage: third, sessionID: chat.id, model: mdl })
        expect(yield* thirdHandle.replay(call("call_third"))).toBe("stop")
        expect(
          MessageV2.parts(third.id).some(
            (part) => part.type === "text" && part.synthetic && part.metadata?.origin?.kind === "try_best",
          ),
        ).toBe(true)
      }),
    { git: true, config: (url) => providerCfg(url) },
  ),
)

it.live.skip("session.processor preserves try-best blocking when denied tools may continue", () =>
  provideTmpdirServer(
    ({ dir }) =>
      Effect.gen(function* () {
        const { processors, session, provider } = yield* boot()
        const chat = yield* session.create({})
        const parent = yield* user(chat.id, "run the tests")
        const mdl = yield* provider.getModel(ref.providerID, ref.modelID)
        const tools = {
          bash: {
            execute: async () => {
              throw new Permission.RejectedError()
            },
          },
        }
        const call = (id: string) => ({
          reasoning: "run tests",
          toolCalls: [{ toolCallId: id, toolName: "bash", input: { command: "bun test" } }],
          finishReason: "tool-calls",
          tools,
          messages: [],
        })

        const first = yield* assistant(chat.id, parent.id, path.resolve(dir))
        const firstHandle = yield* processors.create({ assistantMessage: first, sessionID: chat.id, model: mdl })
        expect(yield* firstHandle.replay(call("call_first"))).toBe("continue")

        const second = yield* assistant(chat.id, first.id, path.resolve(dir))
        const secondHandle = yield* processors.create({ assistantMessage: second, sessionID: chat.id, model: mdl })
        expect(yield* secondHandle.replay(call("call_second"))).toBe("continue")

        const third = yield* assistant(chat.id, second.id, path.resolve(dir))
        const thirdHandle = yield* processors.create({ assistantMessage: third, sessionID: chat.id, model: mdl })
        expect(yield* thirdHandle.replay(call("call_third"))).toBe("stop")
      }),
    {
      git: true,
      config: (url) => ({
        ...providerCfg(url),
        experimental: { continue_loop_on_deny: true },
      }),
    },
  ),
)


itFakeStep.live("processor rejects a missing step but preserves committed partial output without replay", () =>
  provideTmpdirInstance((dir) => Effect.gen(function* () {
    const { processors, session, provider } = yield* boot()
    const model = yield* provider.getModel(ref.providerID, ref.modelID)
    for (const mode of ["empty", "valid", "partial"] as const) {
      fakeStepMode = mode
      fakeStepCalls = 0
      const chat = yield* session.create({})
      const parent = yield* user(chat.id, "test")
      const msg = yield* assistant(chat.id, parent.id, path.resolve(dir))
      const handle = yield* processors.create({ assistantMessage: msg, sessionID: chat.id, model })
      yield* handle.process({ user: { id: parent.id, sessionID: chat.id, role: "user", time: parent.time, agent: parent.agent, model: { providerID: ref.providerID, modelID: ref.modelID } } satisfies MessageV2.User,
        sessionID: chat.id, model, agent: agent(), system: [], messages: [{ role: "user", content: "test" }], tools: {} })
      expect(fakeStepCalls).toBe(1)
      const loaded = MessageV2.get({ sessionID: chat.id, messageID: msg.id })
      expect(loaded.info.role).toBe("assistant")
      if (loaded.info.role !== "assistant") throw new Error("expected assistant message")
      if (mode === "valid") expect(loaded.info.error).toBeUndefined()
      else expect(loaded.info.error).toBeDefined()
      if (mode === "partial") expect(MessageV2.parts(msg.id).some((part) => part.type === "text" && part.text === "kept")).toBe(true)
    }
  }), { git: true, config: cfg }),
)
