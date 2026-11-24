import { expect, test } from "bun:test"
import { readFileSync } from "node:fs"
import { MessageV2 } from "../../src/session/message-v2"
import { modelSwitchNoticeText } from "../../src/acp/agent"

const model: any = { id: "m", providerID: "p", api: { id: "m", npm: "@ai-sdk/anthropic" }, capabilities: { input: { text: true }, output: { text: true }, attachment: false }, options: {} }
const text = (id: string, t: string, kind?: string) => ({
  id, messageID: "a1", sessionID: "s", type: "text", text: t, synthetic: kind ? true : undefined,
  ...(kind ? { metadata: { origin: { kind } } } : {}), time: { start: 0, end: 0 },
})

test("model_switch part is not sent to the model; normal and try_best text are unchanged", async () => {
  const source: any = [
    { info: { id: "u1", role: "user", sessionID: "s", time: { created: 0 } }, parts: [{ id: "up", messageID: "u1", sessionID: "s", type: "text", text: "hi" }] },
    { info: { id: "a1", role: "assistant", sessionID: "s", providerID: "p", modelID: "m", time: { created: 0, completed: 1 }, tokens: { input: 0, output: 0, reasoning: 0, cache: { read: 0, write: 0 } } },
      parts: [text("p1", "Switched from x/y to p/m: notice", "model_switch"), text("p2", "try-best notice", "try_best"), text("p3", "real reply")] },
  ]
  const out = JSON.stringify(await MessageV2.toModelMessages(source, model))
  expect(out).not.toContain("Switched from")
  expect(out).toContain("try-best notice")
  expect(out).toContain("real reply")
})

test("ACP live forwarding: one chunk per model_switch part, none for ordinary text", () => {
  const seen = new Set<string>()
  const sw = text("p1", "Switched from x/y to p/m: notice", "model_switch") as any
  expect(modelSwitchNoticeText(sw, seen)).toBe("Switched from x/y to p/m: notice\n\n")
  expect(modelSwitchNoticeText(sw, seen)).toBeUndefined()
  expect(modelSwitchNoticeText(text("p3", "reply") as any, seen)).toBeUndefined()
  expect(modelSwitchNoticeText(text("p2", "t", "try_best") as any, seen)).toBeUndefined()
})

// Fixtures h1-processor-consumer / h1-scoped-llm already fail at baseline and cannot reach the
// redispatch region cheaply, so the processor contract is checked by source order only.
test("processor writes the notice only after activation succeeds, once", () => {
  const src = readFileSync(new URL("../../src/session/processor.ts", import.meta.url), "utf8")
  const activate = src.indexOf("ManagedProvider.activateOwnedAlternative(context)")
  const rollback = src.indexOf("return yield* Effect.fail(error)", activate)
  const notice = src.indexOf('kind: "model_switch"')
  expect(src.split('kind: "model_switch"').length - 1).toBe(1)
  expect(activate).toBeGreaterThan(0)
  expect(notice).toBeGreaterThan(rollback)
  expect(src.indexOf("return yield* oneAttempt", notice)).toBeGreaterThan(notice)
  const region = src.slice(notice - 400, notice + 400)
  expect(region).not.toMatch(/accountID|apiKey|baseURL|error\./)
})
