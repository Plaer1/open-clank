import { describe, expect } from "bun:test"
import { Deferred, Effect, Layer } from "effect"
import { Bus } from "../../src/bus"
import { Config } from "../../src/config"
import { Plugin } from "../../src/plugin"
import { Agent } from "../../src/agent/agent"
import { Session as SessionNs } from "../../src/session"
import { Provider } from "../../src/provider"
import { ProviderTest } from "../fake/provider"
import { Log } from "../../src/util"
import { testEffect } from "../lib/effect"
import { provideTmpdirInstance } from "../fixture/fixture"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { forkQuery } from "../../src/tool/session"
import { prefixCaptureRef } from "../../src/session/prefix-capture-ref"
import { MessageID, PartID } from "../../src/session/schema"

void Log.init({ print: false })

const deps = Layer.mergeAll(
  ProviderTest.fake().layer,
  Agent.defaultLayer,
  Plugin.defaultLayer,
  Bus.layer,
  Config.defaultLayer,
)

const env = Layer.mergeAll(SessionNs.defaultLayer, CrossSpawnSpawner.defaultLayer, deps)

const it = testEffect(env)

// The POST /:sessionID/ask route is a thin wrapper over forkQuery (the same
// frozen-snapshot, read-only fork-query the session tool's `ask` verb uses).
// This asserts forkQuery is exported and its no-activity early-return path
// works without spawning a fork or touching an LLM: a brand-new session with
// no main-slice user message has nothing to snapshot, so forkQuery answers
// directly. The full LLM-backed path is covered in session-tool.test.ts.
describe("forkQuery (backing the /:sessionID/ask route)", () => {
  it.live(
    "inherits the source model by default and honors an explicitly resolved worker model",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const sessions = yield* SessionNs.Service
        const info = yield* sessions.create({})
        const sourceModel = { providerID: "expensive-provider", modelID: "large-model" }
        const workerModel = { providerID: "cheap-provider", modelID: "small-model" }
        const userID = MessageID.ascending()
        yield* sessions.updateMessage({
          id: userID,
          sessionID: info.id,
          role: "user",
          agentID: "main",
          agent: "build",
          model: sourceModel as never,
          time: { created: Date.now() },
        })
        yield* sessions.updatePart({
          id: PartID.ascending(), sessionID: info.id, messageID: userID, type: "text", text: "source prompt",
        })

        const spawned: Array<{ model: unknown; forkModel: unknown }> = []
        const previousCapture = prefixCaptureRef.current
        prefixCaptureRef.current = () => Effect.succeed({
          system: ["captured prefix"],
          tools: {},
          inheritedMessages: [{ role: "user", content: "source prompt" }],
          parentPermission: [],
        })
        try {
          const source = { effectiveModel: sourceModel, agent: "main" }
          const actorRegistry = { get: () => Effect.succeed(source) } as never
          const provider = {
            getModel: (providerID: string, modelID: string) => Effect.succeed({ providerID, id: modelID }),
          } as never
          const actor = {
            spawn: (input: any) => Effect.gen(function* () {
              spawned.push({ model: input.model, forkModel: input.forkContext.model })
              const outcome = yield* Deferred.make<any>()
              yield* Deferred.succeed(outcome, { status: "success", finalText: "answer" })
              return { actorID: "actor-test", sessionID: input.sessionID, outcome }
            }),
          } as never
          const deps = { sessions, actorRegistry, provider, actor }
          yield* forkQuery(deps, info.id, "question")
          yield* forkQuery(deps, info.id, "question", workerModel as never)
          expect(spawned[0]).toEqual({ model: sourceModel, forkModel: sourceModel })
          expect(spawned[1]).toEqual({ model: workerModel, forkModel: workerModel })
        } finally {
          prefixCaptureRef.current = previousCapture
        }
      }),
    ),
  )

  it.live(
    "returns a graceful answer for a session with no activity",
    provideTmpdirInstance(() =>
      Effect.gen(function* () {
        const sessions = yield* SessionNs.Service
        const provider = yield* Provider.Service
        const info = yield* sessions.create({})

        const answer = yield* forkQuery(
          // actor is never reached on the no-activity path; the early return
          // fires before any spawn, so a stub interface is sufficient here.
          { sessions, actorRegistry: {} as never, provider, actor: {} as never },
          info.id,
          "what is this session about?",
        )

        expect(answer).toContain("no activity")
      }),
    ),
  )
})
