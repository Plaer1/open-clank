import { expect, test } from "bun:test"
import { generateText } from "ai"
import { createOpenAICompatible } from "@ai-sdk/openai-compatible"
import { applyManagedAdapterOptions } from "../../src/provider/provider"
import { extractMimoSearchAnnotations } from "../../src/acp/managed-operations"

test("managed MiMo search transform reaches the serialized provider body", async () => {
  let captured: Record<string, unknown> | undefined
  const response = {
    id: "mimo-search-fixture",
    choices: [{ message: { role: "assistant", content: "answer", annotations: [{ url: "https://source.example", title: "Source" }] }, finish_reason: "stop" }],
    usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 },
  }
  const metadata = { familyID: "xiaomi", adapterID: "mimo-native", billingLane: "metered_api", connectionID: "connection-mimo" } as any
  const scope = { connectionID: "connection-mimo", billingLane: "metered_api", credentialRequired: true, accountID: "account-1", credentialRevision: 1, credential: { type: "api", key: "fixture-secret" } } as any
  const options = applyManagedAdapterOptions(metadata, scope.credential, { baseURL: "https://mimo.example/v1", headers: {} }, scope, { kind: "web.search", count: 4 })
  const provider = createOpenAICompatible({
    name: "xiaomi",
    baseURL: options.baseURL,
    apiKey: options.apiKey,
    transformRequestBody: options.transformRequestBody,
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init)
      captured = JSON.parse(await request.text())
      return new Response(JSON.stringify(response), { status: 200, headers: { "content-type": "application/json" } })
    }) as any,
  })
  await generateText({ model: provider.languageModel("mimo-v2.5"), prompt: "typed query" })
  expect(captured).toMatchObject({
    model: "mimo-v2.5",
    tools: [{ type: "web_search", max_keyword: 1, force_search: true, limit: 4 }],
  })
  expect(extractMimoSearchAnnotations(response)).toEqual([{ url: "https://source.example", title: "Source" }])
  expect(JSON.stringify(captured)).not.toContain("fixture-secret")
})

test("ordinary compatible chat does not receive the managed search tool", async () => {
  let captured: Record<string, unknown> | undefined
  const provider = createOpenAICompatible({
    name: "ordinary",
    baseURL: "https://ordinary.example/v1",
    apiKey: "fixture-secret",
    fetch: (async (input: RequestInfo | URL, init?: RequestInit) => {
      const request = new Request(input, init)
      captured = JSON.parse(await request.text())
      return new Response(JSON.stringify({ choices: [{ message: { role: "assistant", content: "ok" }, finish_reason: "stop" }] }), { status: 200, headers: { "content-type": "application/json" } })
    }) as any,
  })
  await generateText({ model: provider.languageModel("ordinary"), prompt: "hello" })
  expect(captured?.tools).toBeUndefined()
})
