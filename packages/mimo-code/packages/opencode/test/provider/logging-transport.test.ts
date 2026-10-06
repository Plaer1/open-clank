import { expect, test } from "bun:test"
import type { AccountSelection } from "../../src/provider/account-selection"
import {
  captureFetch,
  flushCaptures,
  installCaptureScope,
  captureScopeEvidence,
} from "../../src/provider/logging-transport"
import type { ExtensionConnection, LoggingEventRequest, LoggingAdmissionResult } from "../../src/acp/openclank-protocol"

const binding = {
  bindingID: "binding",
  bindingRevision: 1,
  rootOperationID: "root",
  connectionID: "connection",
  providerID: "openai",
  billingLane: "metered_api",
  modelID: "selected",
  credentialRequired: false,
  source: "keyless",
  attempt: 1,
  committed: true,
} as AccountSelection.Binding

function admission(responseBody = true): LoggingAdmissionResult {
  return {
    admissionID: "admitted",
    policy: {
      revision: 1,
      advanced_enabled: true,
      request_body_enabled: true,
      response_body_enabled: responseBody,
      binary_body_enabled: false,
    },
    context: {
      instanceID: "worker",
      providerID: "openai",
      operationID: "operation",
      rootOperationID: "root",
      bindingID: "binding",
      bindingRevision: 1,
      identityCoverage: "complete",
      transportMode: "advanced_proxy",
      persistence: { numeric: true, content: true, reason: "normal" },
    },
  }
}

test("managed transport preserves split CRLF bytes and protocol terminal outcome; body-off and unsupported stay bounded", async () => {
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  const posted: LoggingEventRequest[] = []
  const connection: ExtensionConnection = {
    async extMethod(method, params) {
      if (method.endsWith("/admit")) throw new Error("fetch must use pinned admission")
      posted.push(params as unknown as LoggingEventRequest)
      return { accepted: true, replayed: false }
    },
  }
  let scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
  try {
    const pieces = [
      'data: {"model":"actual","choices":[{"delta":{"content":"hi"}}]}\r',
      "\n\r",
      '\ndata: {"usage":{"prompt_tokens":12,"completion_tokens":3,"prompt_tokens_details":{"cached_tokens":2}},"choices":[{"finish_reason":"stop"}]}\r\n\r\ndata: [DONE]\r\n\r\n',
    ]
    let index = 0
    const fake = Object.assign(
      async () =>
        new Response(
          new ReadableStream<Uint8Array>({
            pull(controller) {
              if (index < pieces.length) controller.enqueue(new TextEncoder().encode(pieces[index++]))
            },
          }),
          { status: 200, headers: { "content-type": "text/event-stream" } },
        ),
      { preconnect: fetch.preconnect },
    ) as typeof fetch
    const response = await captureFetch(fake, scope)("https://example.invalid/chat/completions", {
      body: JSON.stringify({ password: "exclude", input_audio: { data: "YWJj", format: "wav" } }),
    })
    const reader = response.body!.getReader()
    let delivered = ""
    for (let i = 0; i < pieces.length; i++) delivered += new TextDecoder().decode((await reader.read()).value)
    await reader.cancel("SDK consumed DONE")
    await flushCaptures()
    expect(delivered).toBe(pieces.join(""))
    const terminal = posted.find((value) => value.terminal)!
    expect(terminal.outcome).toBe("completed")
    expect(terminal.metrics).toMatchObject({ input_tokens: 12, output_tokens: 3, cache_read_tokens: 2 })
    expect(terminal.actualModel).toBe("actual")
    expect(JSON.stringify(terminal.requestBody)).not.toContain("exclude")
    expect(JSON.stringify(terminal.requestBody)).toContain("embedded_media")
    posted.length = 0
    scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission(false))
    const jsonFetch = Object.assign(
      async () =>
        new Response(
          JSON.stringify({
            choices: [{ message: { content: "normal JSON" } }],
            usage: { prompt_tokens: 2, completion_tokens: 1 },
          }),
          { headers: { "content-type": "application/json" } },
        ),
      { preconnect: fetch.preconnect },
    ) as typeof fetch
    const json = await captureFetch(jsonFetch, scope)("https://example.invalid/chat/completions")
    await json.text()
    await flushCaptures()
    const bodyOff = posted.find((value) => value.terminal)!
    expect(bodyOff.responseBody).toBeUndefined()
    expect(bodyOff.events).toBeUndefined()
    expect(bodyOff.metrics?.output_tokens).toBe(1)
    posted.length = 0
    scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
    const unsupported = await captureFetch(jsonFetch, scope)("https://example.invalid/vendor/custom")
    await unsupported.text()
    await flushCaptures()
    const unmatched = posted.find((value) => value.terminal)!
    expect(unmatched.responseBody).toBeDefined()
    expect(unmatched.metrics).toMatchObject({ input_tokens: 2, output_tokens: 1 })
    expect(unmatched.normalizationProfile).toBeNull()
    expect(unmatched.lossReasons).toContain("parse_degraded")
    expect(captureFetch(jsonFetch, undefined)).toBe(jsonFetch)
  } finally {
    if (old === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = old
  }
})

test("timed out real logging RPCs keep capacity while provider responses continue", async () => {
  const old = process.env.OPEN_CLANK_MANAGED
  process.env.OPEN_CLANK_MANAGED = "1"
  let calls = 0
  const release: Array<(value: unknown) => void> = []
  const connection: ExtensionConnection = {
    extMethod() {
      calls++
      return new Promise((resolve) => release.push(resolve))
    },
  }
  const scope = installCaptureScope({} as AccountSelection.Scope, binding, connection, admission())
  const fake = Object.assign(async () => new Response("provider response"), {
    preconnect: fetch.preconnect,
  }) as typeof fetch
  try {
    const responses = await Promise.all(
      Array.from({ length: 48 }, async () =>
        (await captureFetch(fake, scope)("https://example.invalid/responses")).text(),
      ),
    )
    expect(responses.every((value) => value === "provider response")).toBe(true)
    expect(calls).toBeLessThanOrEqual(32)
    expect(captureScopeEvidence(scope).lossReasons).toContain("queue_full")
    await captureFetch(fake, scope)("https://example.invalid/responses")
    expect(calls).toBeLessThanOrEqual(32)
  } finally {
    for (const resolve of release) resolve({ accepted: true, replayed: false, captureState: "stored" })
    await Promise.resolve()
    if (old === undefined) delete process.env.OPEN_CLANK_MANAGED
    else process.env.OPEN_CLANK_MANAGED = old
  }
})
