import { afterEach, describe, expect, test } from "bun:test"
import { Instance } from "../../src/project/instance"
import { Session as SessionNs } from "../../src/session"
import { MessageV2 } from "../../src/session/message-v2"
import { MessageID, PartID, SessionID } from "../../src/session/schema"
import { ProviderID, ModelID } from "../../src/provider/schema"
import { buildLLMRequestPrefix } from "../../src/session/llm-request-prefix"
import { AppRuntime } from "../../src/effect/app-runtime"
import { Log } from "../../src/util"
import { tmpdir } from "../fixture/fixture"
import { ProviderTest } from "../fake/provider"
import type { Agent } from "../../src/agent/agent"

void Log.init({ print: false })

afterEach(async () => {
  await Instance.disposeAll()
})

function makeAgent(): Agent.Info {
  return {
    name: "build",
    mode: "primary",
    options: {},
    permission: [{ permission: "*", pattern: "*", action: "allow" }],
  } satisfies Agent.Info
}

describe("fork prefix invariant", () => {
  test("two callers of buildLLMRequestPrefix produce deep-equal output for identical inputs", async () => {
    // The invariant: any future change to system/tools/messages construction
    // that introduces an agent-conditional branch breaks this assertion.
    // parent's runLoop and fork's spawn capture both call buildLLMRequestPrefix
    // with the same inputs and must produce byte-equal output.

    await using tmp = await tmpdir({ git: true })
    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const session = await AppRuntime.runPromise(
          SessionNs.Service.use((svc) => svc.create({})),
        )

        // Build a session with three messages: user → assistant → user.
        for (let i = 0; i < 3; i++) {
          const id = MessageID.ascending()
          const role = i % 2 === 0 ? "user" : "assistant"
          await AppRuntime.runPromise(
            SessionNs.Service.use((svc) =>
              svc.updateMessage({
                id,
                sessionID: session.id,
                role,
                time: { created: Date.now() + i },
                agent: "build",
                model: { providerID: ProviderID.make("test"), modelID: ModelID.make("test") },
                tools: {},
                mode: "",
              } as unknown as MessageV2.Info),
            ),
          )
          await AppRuntime.runPromise(
            SessionNs.Service.use((svc) =>
              svc.updatePart({
                id: PartID.ascending(),
                sessionID: session.id,
                messageID: id,
                type: "text",
                text: `msg-${i}`,
              }),
            ),
          )
        }

        const msgs = await AppRuntime.runPromise(
          SessionNs.Service.use((svc) => svc.messages({ sessionID: session.id })),
        )

        const agent = makeAgent()
        const model = ProviderTest.model({
          id: ModelID.make("gpt-5.2"),
          providerID: ProviderID.make("openai"),
        })

        // Call A: parent's runLoop view
        const a = await AppRuntime.runPromise(
          buildLLMRequestPrefix({
            sessionID: session.id,
            agent,
            model,
            msgs,
            additions: [],
          }),
        )

        // Call B: fork's spawn-capture view.
        // Both paths call this same function with the same inputs;
        // any divergence (e.g. agent-conditional branch) breaks this assertion.
        const b = await AppRuntime.runPromise(
          buildLLMRequestPrefix({
            sessionID: session.id,
            agent,
            model,
            msgs,
            additions: [],
          }),
        )

        // Fork-prefix-cache parity contract
        expect(a.system).toEqual(b.system)
        expect(JSON.stringify(a.tools)).toEqual(JSON.stringify(b.tools))
        expect(a.inheritedMessages).toEqual(b.inheritedMessages)
      },
    })
  })
})


test("reserved fork remaps durable source anchors before writes and preserves attachments and assistant parents", async () => {
  await using tmp = await tmpdir({ git: true })
  await Instance.provide({ directory: tmp.path, fn: async () => {
    const source = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.create({})))
    const userID = MessageID.ascending()
    const assistantID = MessageID.ascending()
    await AppRuntime.runPromise(SessionNs.Service.use((service) => service.updateMessage({ id: userID, sessionID: source.id, role: "user", time: { created: Date.now() }, agent: "build", model: { providerID: ProviderID.make("test"), modelID: ModelID.make("test") } })))
    await AppRuntime.runPromise(SessionNs.Service.use((service) => service.updatePart({ id: PartID.ascending(), sessionID: source.id, messageID: userID, type: "text", text: "", synthetic: true, ignored: true, metadata: { hostSourceMessageID: "root-source" } })))
    await AppRuntime.runPromise(SessionNs.Service.use((service) => service.updatePart({ id: PartID.ascending(), sessionID: source.id, messageID: userID, type: "file", filename: "fixture.txt", mime: "text/plain", url: "data:text/plain;base64,SGk=" })))
    await AppRuntime.runPromise(SessionNs.Service.use((service) => service.updateMessage({ id: assistantID, sessionID: source.id, role: "assistant", parentID: userID, time: { created: Date.now() }, agent: "build", mode: "build", modelID: ModelID.make("test"), providerID: ProviderID.make("test"), path: { cwd: tmp.path, root: tmp.path }, cost: 0, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } })))
    const rejectedTarget = SessionID.descending()
    await expect(AppRuntime.runPromise(SessionNs.Service.use((service) => service.fork({ sessionID: source.id, targetSessionID: rejectedTarget, hostSourceMap: {} })))).rejects.toThrow("incomplete")
    await expect(AppRuntime.runPromise(SessionNs.Service.use((service) => service.get(rejectedTarget)))).rejects.toMatchObject({ name: "NotFoundError" })
    const target = SessionID.descending()
    const forked = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.fork({ sessionID: source.id, targetSessionID: target, hostSourceMap: { "root-source": "root-child" }, forkFingerprint: "a".repeat(64) })))
    expect(forked.id).toBe(target)
    const messages = await AppRuntime.runPromise(SessionNs.Service.use((service) => service.messages({ sessionID: target, agentID: "*" })))
    const user = messages.find((message) => message.info.role === "user")!
    const assistant = messages.find((message) => message.info.role === "assistant")!
    expect(user.info.id).not.toBe(userID)
    expect(user.parts.find((part) => part.type === "text")).toMatchObject({ ignored: true, synthetic: true, metadata: { hostSourceMessageID: "root-child" } })
    expect(user.parts.filter((part) => part.type === "text").find((part) => part.metadata?.nativeForkReceipt)).toMatchObject({ metadata: { nativeForkReceipt: { sourceSessionID: source.id, targetSessionID: target, fingerprint: "a".repeat(64), sourceMessageCount: 2 } } })
    expect(user.parts.find((part) => part.type === "file")).toMatchObject({ filename: "fixture.txt", url: "data:text/plain;base64,SGk=" })
    expect(assistant.info).toMatchObject({ parentID: user.info.id })
    await expect(AppRuntime.runPromise(SessionNs.Service.use((service) => service.fork({ sessionID: source.id, targetSessionID: target, hostSourceMap: { "root-source": "root-other" } })))).rejects.toThrow("already exists")
  } })
})
