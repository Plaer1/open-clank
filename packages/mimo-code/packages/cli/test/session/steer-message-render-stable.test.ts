/**
 * Regression: a user message admitted while a turn is running (a desktop
 * "steer") reaches the model exactly like any other user message — its stored
 * text, byte-identical in every later request.
 *
 * The engine used to wrap such a message in a "The user sent the following
 * message" reminder, recomputed per request against the last finished
 * assistant. The wrap appeared in the request that admitted the message and
 * vanished in the next one, rewriting the prefix of the signed thinking block
 * produced in between; upstreams that verify the binding then reject every
 * later request with 400 "Invalid signature in thinking block ... Content
 * before this block differs from when it was created".
 */
import { describe, expect } from "bun:test"
import { Effect, Fiber, Layer } from "effect"
import { AppLayer } from "../../src/effect/app-runtime"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { Session } from "../../src/session"
import { SessionPrompt } from "../../src/session/prompt"
import { MessageID } from "../../src/session/schema"
import { provideTmpdirServer } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { reply, TestLLMServer } from "../lib/llm-server"

const it = testEffect(Layer.mergeAll(AppLayer, CrossSpawnSpawner.defaultLayer, TestLLMServer.layer))
const config = (url: string) => ({
  enabled_providers: ["alibaba"],
  provider: { alibaba: { options: { apiKey: "test-key", baseURL: url } } },
  agent: { build: { model: "alibaba/qwen-plus" } },
})

type Message = { role: string; content: unknown }

const userContaining = (messages: Message[], text: string) => {
  const found = messages.filter((message) => message.role === "user" && JSON.stringify(message.content).includes(text))
  expect(found).toHaveLength(1)
  return found[0]!
}

describe("mid-turn user message rendering", () => {
  it.live(
    "a message admitted mid tool loop is sent as its stored text, identically in later steps and turns",
    provideTmpdirServer(
      ({ llm, dir }) =>
        Effect.gen(function* () {
          const sessions = yield* Session.Service
          const prompt = yield* SessionPrompt.Service
          const session = yield* sessions.create({ title: "mid-turn message rendering" })
          yield* Effect.addFinalizer(() => prompt.cancel(session.id))
          const gate = Promise.withResolvers<void>()
          yield* Effect.addFinalizer(() => Effect.sync(() => gate.resolve()))

          // Request 0: tool call, held until the mid-turn message is accepted.
          // Request 1: the loop admits the message; the model calls another tool.
          // Request 2: the loop continues after that tool; the model finishes.
          // Request 3: the next user turn.
          yield* llm.push(reply().wait(gate.promise).tool("read", { file_path: dir }))
          yield* llm.tool("read", { file_path: dir })
          yield* llm.text("LOOP-DONE")

          const main = yield* prompt
            .prompt({ sessionID: session.id, agent: "build", parts: [{ type: "text", text: "TASK-U1" }] })
            .pipe(Effect.forkChild)
          yield* llm.wait(1).pipe(Effect.timeout("5 seconds"))
          const steerID = MessageID.ascending()
          yield* prompt.promptAsync({
            sessionID: session.id,
            messageID: steerID,
            agent: "build",
            parts: [{ type: "text", text: "STEER-U2" }],
          })
          gate.resolve()
          yield* Fiber.join(main).pipe(Effect.timeout("10 seconds"))

          yield* llm.text("NEXT-TURN-DONE")
          yield* prompt.prompt({ sessionID: session.id, agent: "build", parts: [{ type: "text", text: "NEXT-U3" }] })

          const inputs = (yield* llm.inputs).map((input) => input.messages as Message[])
          expect(inputs).toHaveLength(4)
          expect(JSON.stringify(inputs[0])).not.toContain("STEER-U2")

          // The request that admits the message sends exactly the stored text...
          const first = userContaining(inputs[1]!, "STEER-U2")
          expect(first.content).toBe("STEER-U2")
          // ...and every later request replays it byte-identically.
          expect(userContaining(inputs[2]!, "STEER-U2")).toEqual(first)
          expect(userContaining(inputs[3]!, "STEER-U2")).toEqual(first)

          // The stored part is the user's own words.
          const stored = (yield* sessions.messages({ sessionID: session.id })).find(
            (message) => message.info.id === steerID,
          )
          expect(stored?.parts.flatMap((part) => (part.type === "text" ? [part.text] : []))).toEqual(["STEER-U2"])
        }),
      { git: true, config },
    ),
  )
})
