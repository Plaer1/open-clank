import { expect, test } from "bun:test"
import path from "path"
import { Session } from "../../src/session"
import { Instance } from "../../src/project/instance"
import { AppRuntime } from "../../src/effect/app-runtime"
import { MessageID, PartID } from "../../src/session/schema"
import { projectGcfReceipt, stripGcfReceipt, validGcfReceipt } from "../../src/session/gcf-source-proof"

test("completed source evidence survives only its exact first-party persisted fact", async () => {
  await Instance.provide({ directory: path.join(__dirname, "../.."), fn: async () => {
    const session = await AppRuntime.runPromise(Session.Service.use((s) => s.create({})))
    const messageID = MessageID.ascending()
    await AppRuntime.runPromise(Session.Service.use((s) => s.updateMessage({
      id: messageID, sessionID: session.id, role: "user", time: { created: Date.now() },
      agent: "user", model: { providerID: "test", modelID: "test" }, tools: {}, mode: "",
    } as any)))
    const part: any = { id: PartID.ascending(), sessionID: session.id, messageID, type: "tool", callID: "call",
      tool: "mcp", state: { status: "completed", input: {}, title: "", output: JSON.stringify({ text: "x".repeat(150) }),
        metadata: { truncated: false, mcp: { isError: false }, original: "kept" }, time: { start: 1, end: 2 } } }
    const issued = await AppRuntime.runPromise(Session.Service.use((s) => s.updateGcfCompleted(part)))
    const read = () => AppRuntime.runPromise(Session.Service.use((s) => s.getPart({ sessionID: session.id, messageID, partID: part.id })))
    expect(validGcfReceipt((await read()) as any)).toBe(true)
    expect(projectGcfReceipt(issued)).toEqual(stripGcfReceipt(issued)) // foreign replay cannot mint
    expect(validGcfReceipt({ ...issued, messageID: MessageID.ascending() })).toBe(false)
    expect(validGcfReceipt({ ...issued, tool: "another" })).toBe(false)
    expect(validGcfReceipt({ ...issued, metadata: { providerExecuted: true } })).toBe(false)
    expect(validGcfReceipt({ ...issued, state: { ...issued.state, output: "changed" } })).toBe(false)
    const imported = stripGcfReceipt(issued)
    expect((imported.state as any).metadata.original).toBe("kept")
    expect(validGcfReceipt(imported)).toBe(false)
    await AppRuntime.runPromise(Session.Service.use((s) => s.updatePart({ ...issued, state: { ...issued.state, output: "changed" } } as any)))
    expect(validGcfReceipt((await read()) as any)).toBe(false)
    await AppRuntime.runPromise(Session.Service.use((s) => s.updatePart(issued)))
    expect(validGcfReceipt((await read()) as any)).toBe(false)
    await AppRuntime.runPromise(Session.Service.use((s) => s.remove(session.id)))
  } })
})
