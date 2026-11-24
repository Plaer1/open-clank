import { describe, expect, test } from "bun:test"
import { tool } from "ai"
import { asSchema } from "@ai-sdk/provider-utils"
import z from "zod"
import {
  decodeForkContext,
  encodeForkContext,
  ForkContextCodecError,
  MAX_FORK_CONTEXT_BYTES,
  type ForkContext,
} from "../../src/actor/fork-context"

describe("immutable fork-context codec", () => {
  test("round-trips SDK binary and URL message data without JSON object coercion", async () => {
    const imageBytes = new Uint8Array([0, 7, 255])
    const fileBytes = new ArrayBuffer(3)
    new Uint8Array(fileBytes).set([4, 5, 6])
    const bufferBytes = Buffer.from([9, 8, 7])
    const context = {
      system: ["captured system"],
      tools: {
        read: tool({
          description: "Read a path",
          inputSchema: z.object({ path: z.string(), headers: z.record(z.string(), z.string()).optional() }),
          execute: async () => "runtime closure is omitted",
        }),
      },
      parentPermission: [{ permission: "read", pattern: "*", action: "allow" }],
      inheritedMessages: [{
        role: "user",
        content: [
          { type: "image", image: imageBytes },
          { type: "image", image: fileBytes },
          { type: "file", data: bufferBytes, mediaType: "application/octet-stream" },
          { type: "image", image: new URL("https://example.invalid/image.png") },
        ],
      }],
      watermarkMsgID: "msg_watermark" as never,
      model: { providerID: "provider_test" as never, modelID: "model_test" as never },
    } satisfies ForkContext
    const stored = await encodeForkContext(context, { sessionID: "ses_source" as never, actorID: "main" })
    const restored = await decodeForkContext({ ...stored, sessionID: "ses_child" as never, actorID: "agent_child", timeCreated: 1 })
    const parts = (restored.inheritedMessages[0] as any).content
    expect(parts[0].image).toBeInstanceOf(Uint8Array)
    expect([...parts[0].image]).toEqual([0, 7, 255])
    expect(parts[1].image).toBeInstanceOf(ArrayBuffer)
    expect([...new Uint8Array(parts[1].image)]).toEqual([4, 5, 6])
    expect(Buffer.isBuffer(parts[2].data)).toBe(true)
    expect([...parts[2].data]).toEqual([9, 8, 7])
    expect(parts[3].image).toBeInstanceOf(URL)
    expect(restored.tools.read.execute).toBeUndefined()
    expect(restored.tools.read.description).toBe("Read a path")
    const restoredSchema = await asSchema(restored.tools.read.inputSchema).jsonSchema
    expect(restoredSchema).toHaveProperty("properties.headers")
  })

  test("rejects unknown prototypes and over-limit snapshots explicitly", async () => {
    const base = {
      system: ["captured"],
      tools: {},
      parentPermission: [],
      inheritedMessages: [{ role: "user", content: "prompt" }],
      watermarkMsgID: "msg_watermark" as never,
      model: { providerID: "provider_test" as never, modelID: "model_test" as never },
    } satisfies ForkContext
    await expect(encodeForkContext({ ...base, inheritedMessages: [{ role: "user", content: new Date() } as never] }, { sessionID: "ses" as never, actorID: "main" }))
      .rejects.toThrow(ForkContextCodecError)
    await expect(encodeForkContext({ ...base, system: ["x".repeat(MAX_FORK_CONTEXT_BYTES)] }, { sessionID: "ses" as never, actorID: "main" }))
      .rejects.toThrow(new RegExp(`maximum is ${MAX_FORK_CONTEXT_BYTES} bytes`))
  })

  test("detects snapshot tampering before restoring values", async () => {
    const context = {
      system: ["captured"],
      tools: {},
      parentPermission: [],
      inheritedMessages: [{ role: "user", content: "prompt" }],
      watermarkMsgID: "msg_watermark" as never,
      model: { providerID: "provider_test" as never, modelID: "model_test" as never },
    } satisfies ForkContext
    const stored = await encodeForkContext(context, { sessionID: "ses" as never, actorID: "main" })
    await expect(decodeForkContext({ ...stored, snapshotJSON: stored.snapshotJSON + " " , sessionID: "ses_child" as never, actorID: "agent", timeCreated: 1 }))
      .rejects.toThrow(/byte count|checksum/)
  })
})
