import { describe, expect, test } from "bun:test"
import { contextFrame, contextFrameWriter, sourceEvidence } from "../../src/session/context-inspection"

describe("provider frame context evidence", () => {
  test("callback persists bounded normal and changed inherited-prefix frames to an assistant", async () => {
    const window = { hard: 1000, effective: 900, usable: 800, source: "config" as const }
    const assistant: { metadata?: Record<string, unknown> } = { metadata: { unrelated: true } }
    let stored: typeof assistant | undefined, unavailable = 0
    const write = contextFrameWriter(assistant, async (message) => { stored = structuredClone(message) }, () => unavailable++)
    const frozen = [{ role: "user", content: "private original" }], before = JSON.stringify(frozen)
    const base = { identity: { sessionID: "s" }, providerID: "p", modelID: "m", window, instructions: undefined, tools: [{ name: "tool" }] }
    const sources = sourceEvidence([{ info: { id: "m1" }, parts: [{ id: "p1" }] }])
    await write(contextFrame({ ...base, ...sources, sequence: {attempt:1, transform:1}, prompt: frozen }))
    await write(contextFrame({ ...base, ...sourceEvidence([], {kind:"fork_snapshot", id:"m1", digest:"abc"}), sequence:{attempt:2, transform:1}, prompt: [...frozen, { role: "user", content: "retry" }] }))
    const receipt = stored!.metadata!.contextInspection as { frames: ReturnType<typeof contextFrame>[] }
    expect(receipt.frames).toHaveLength(2)
    expect(receipt.frames[1].stage).toBe("provider_frame_pre_dispatch")
    expect(receipt.frames[1].sourceMapping).toBe("unknown")
    expect(receipt.frames[0].sources[0].digest).toBeDefined()
    expect(receipt.frames[0].components.prompt.digest).not.toBe(receipt.frames[1].components.prompt.digest)
    expect(JSON.stringify(stored)).not.toContain("private original")
    expect(JSON.stringify(frozen)).toBe(before)
    assistant.metadata = {contextInspection:{version:2, future:true}, unrelated:true}
    await write(contextFrame({ ...base, sequence:{attempt:3, transform:1}, prompt:frozen }))
    expect(assistant.metadata.contextInspection).toEqual({version:2, future:true})
    expect(unavailable).toBe(1)
  })
})

test("overlapping frame and delivery saves retain both evidence and unrelated metadata", async () => {
  const {nativeTextDeliveryWriter, projectContextInspection} = await import("../../src/session/context-inspection")
  const assistant: {metadata?:Record<string,unknown>} = {metadata:{unrelated:true}}
  let release!: () => void
  const pending = new Promise<void>((resolve) => { release=resolve })
  let saves=0, stored: typeof assistant | undefined
  const save = async (message: typeof assistant) => { if (++saves===1) await pending; stored=structuredClone(message) }
  const frameWriter=contextFrameWriter(assistant,save,() => { throw new Error("frame unavailable") })
  const deliveryWriter=nativeTextDeliveryWriter(assistant,save,() => { throw new Error("delivery unavailable") })
  const identity={sessionID:"s",rootOperationID:"r",actorID:"main" as const,executionRevision:1,innerTurnID:"t"}
  const frame=contextFrame({identity,providerID:"p",modelID:"m",window:{hard:100,effective:90,usable:80,source:"config"},sequence:{attempt:1,transform:1},prompt:[],instructions:undefined,tools:[]})
  const first=frameWriter(frame)
  const second=deliveryWriter({version:1,identity,profile:"openai-chat-single-user-text-v1",dispatchID:"d",bodyDigest:"a".repeat(64),source:{messageID:"m",partID:"p",digest:"b".repeat(64),sourceRevision:"unavailable"},state:"acknowledged",coverage:"partial"})
  release(); await Promise.all([first,second])
  const inspection=stored!.metadata!.contextInspection as {frames:unknown[];nativeDeliveries:unknown[]}
  expect(inspection.frames).toHaveLength(1); expect(inspection.nativeDeliveries).toHaveLength(1); expect(stored!.metadata!.unrelated).toBe(true)
  expect(projectContextInspection(inspection)?.nativeDeliveries).toHaveLength(1)
  expect(projectContextInspection({...inspection,nativeDeliveries:[{...(inspection.nativeDeliveries[0] as object),unexpected:true}]})).toBeUndefined()
})
