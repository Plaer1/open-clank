import { describe, expect, test } from "bun:test"
import { ProviderTest } from "../fake/provider"
import { ProviderTransform } from "../../src/provider"
import { parseNativePayload } from "../../src/provider/transform"

const model = ProviderTest.model({
  api: { id: "fixture", url: "https://wire.invalid", npm: "@ai-sdk/openai-compatible" },
  capabilities: {
    ...ProviderTest.model().capabilities,
    input: { text: true, image: true, audio: true, video: true, pdf: true },
  },
})

function firstContent(value: unknown, mediaType: string) {
  return (ProviderTransform.message([{ role: "user", content: [{ type: "file", data: value, mediaType }] }] as any, model, {})[0].content as any[])[0]
}

describe("native media payload parsing and transform preflight", () => {
  test("parses bytes, data URLs, raw base64, and remote URLs with explicit status", () => {
    expect(parseNativePayload(new Uint8Array([1, 2, 3]), "audio/ogg")).toMatchObject({ kind: "inline", mimeType: "audio/ogg", bytes: 3, payloadBytes: 4 })
    expect(parseNativePayload(new Uint8Array([1, 2, 3]).buffer, "video/mp4")).toMatchObject({ kind: "inline", mimeType: "video/mp4", bytes: 3, payloadBytes: 4 })
    expect(parseNativePayload("data:image/png;base64,AQID", "image/png")).toMatchObject({ kind: "inline", mimeType: "image/png", bytes: 3, payloadBytes: 4 })
    expect(parseNativePayload("data:image/png,AQID", "image/png")).toMatchObject({ kind: "malformed" })
    expect(parseNativePayload("AQID", "application/pdf")).toMatchObject({ kind: "inline", mimeType: "application/pdf", bytes: 3, payloadBytes: 4 })
    expect(parseNativePayload(new URL("https://example.invalid/media.mp4"), "video/mp4")).toMatchObject({ kind: "remote", mimeType: "video/mp4" })
  })

  test("rejects malformed and MIME-mismatched data before dispatch", () => {
    expect(parseNativePayload("data:audio/ogg;base64,not base64", "audio/ogg")).toMatchObject({ kind: "malformed" })
    expect(parseNativePayload("data:audio/ogg;base64,AQID", "audio/flac")).toMatchObject({ kind: "malformed" })
    expect(firstContent("data:audio/ogg;base64,AQID", "audio/flac")).toMatchObject({ type: "text", text: expect.stringContaining("does not match") })
    expect(firstContent(new URL("https://example.invalid/audio.ogg"), "audio/ogg")).toMatchObject({ type: "text", text: expect.stringContaining("remote media size") })
    const imageResult = ProviderTransform.message([{ role: "user", content: [{ type: "image", image: "data:image/png;base64,AQID", mediaType: "image/jpeg" }] }] as any, model, {})
    expect((imageResult[0].content as any[])[0]).toMatchObject({ type: "text", text: expect.stringContaining("does not match") })
  })

  test("rejects unknown PDF and unsupported adapter media without sending the part", () => {
    expect(firstContent(new Uint8Array([1, 2, 3]), "application/pdf")).toMatchObject({ type: "text", text: expect.stringContaining("no declared pdf support") })
    expect(firstContent(new Uint8Array([1, 2, 3]), "audio/aac")).toMatchObject({ type: "text", text: expect.stringContaining("does not accept audio/aac") })
  })

  test("preserves accepted typed bytes through transform", () => {
    const bytes = new Uint8Array([1, 2, 3])
    expect(firstContent(bytes, "audio/ogg")).toEqual({ type: "file", data: bytes, mediaType: "audio/ogg" })
    expect(firstContent(bytes, "video/mp4")).toEqual({ type: "file", data: bytes, mediaType: "video/mp4" })
  })

  test("uses the same preflight path for restored typed history", () => {
    const fresh = new Uint8Array([1, 2, 3])
    const restored = new Uint8Array([1, 2, 3])
    expect(firstContent(fresh, "audio/ogg")).toEqual(firstContent(restored, "audio/ogg"))
  })
})
