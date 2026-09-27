import { describe, expect, test } from "bun:test"
import { ProviderTest } from "../fake/provider"
import {
  NATIVE_MAX_PAYLOAD_BYTES,
  NATIVE_MAX_SOURCE_BYTES,
  nativeRejectionFor,
} from "../../src/provider/capability-registry"

describe("native media capability preflight", () => {
  const model = ProviderTest.model({
    api: { id: "fixture", url: "https://wire.invalid", npm: "@ai-sdk/openai-compatible" },
    capabilities: {
      ...ProviderTest.model().capabilities,
      input: { text: true, image: true, audio: true, video: true, pdf: true },
    },
  })

  test("uses installed adapter MIME evidence for audio", () => {
    expect(nativeRejectionFor(model, { modality: "audio", mimeType: "audio/ogg", bytes: 3, payloadBytes: 4 })).toBeUndefined()
    expect(nativeRejectionFor(model, { modality: "audio", mimeType: "audio/aac", bytes: 3, payloadBytes: 4 })).toMatchObject({
      kind: "mime-unsupported",
    })
  })

  test("keeps PDF unknown until an installed wire shape proves it", () => {
    expect(nativeRejectionFor(model, { modality: "pdf", mimeType: "application/pdf", bytes: 3 })).toMatchObject({
      kind: "modality-unknown",
    })
  })

  test("keeps native source and payload caps separate from MCP limits", () => {
    expect(nativeRejectionFor(model, { modality: "video", mimeType: "video/mp4", bytes: NATIVE_MAX_SOURCE_BYTES })).toBeUndefined()
    expect(nativeRejectionFor(model, { modality: "video", mimeType: "video/mp4", bytes: NATIVE_MAX_SOURCE_BYTES + 1 })).toMatchObject({
      kind: "source-too-large",
    })
    expect(nativeRejectionFor(model, { modality: "video", mimeType: "video/mp4", bytes: 3, payloadBytes: NATIVE_MAX_PAYLOAD_BYTES })).toBeUndefined()
    expect(nativeRejectionFor(model, { modality: "video", mimeType: "video/mp4", bytes: 3, payloadBytes: NATIVE_MAX_PAYLOAD_BYTES + 1 })).toMatchObject({
      kind: "payload-too-large",
    })
  })
})
