import { describe, expect, test } from "bun:test"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { streamText } from "ai"

function response() {
  return new Response('data: {"id":"wire","object":"chat.completion.chunk","choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n', {
    headers: { "content-type": "text/event-stream" },
  })
}

async function wire(mimeType: string) {
  let body: any
  const provider = createOpenAICompatible({
    name: "wire",
    apiKey: "fixture",
    baseURL: "https://wire.invalid/v1",
    fetch: (async (_url, init) => {
      body = JSON.parse(init?.body as string)
      return response()
    }) as typeof fetch,
  })
  const result = streamText({
    model: provider.languageModel("fixture"),
    messages: [{ role: "user", content: [{ type: "text", text: "send" }, { type: "file", data: new Uint8Array([1, 2, 3]), mediaType: mimeType }] }],
  })
  await result.text
  return body
}

describe("installed openai-compatible media wire", () => {
  test("patched audio formats become input_audio with original bytes", async () => {
    for (const mimeType of ["audio/ogg", "audio/flac", "audio/x-wav"]) {
      const body = await wire(mimeType)
      const part = body.messages[0].content.find((item: any) => item.type === "input_audio")
      expect(part).toEqual({ type: "input_audio", input_audio: { format: mimeType === "audio/ogg" ? "ogg" : mimeType.includes("flac") ? "flac" : "wav", data: "AQID" } })
    }
  })

  test("video bytes become a data URL", async () => {
    const body = await wire("video/mp4")
    expect(body.messages[0].content.find((item: any) => item.type === "video_url")).toEqual({
      type: "video_url",
      video_url: { url: "data:video/mp4;base64,AQID" },
    })
  })

  test("image bytes remain an exact data URL", async () => {
    const body = await wire("image/png")
    expect(body.messages[0].content.find((item: any) => item.type === "image_url")).toEqual({
      type: "image_url",
      image_url: { url: "data:image/png;base64,AQID" },
    })
  })
})
