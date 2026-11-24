import { expect, test } from "bun:test"
import { createGrokDeviceAuthorization, pollGrokDeviceCode, resolveGrokOAuthMetadata } from "../../src/plugin/grok"

const metadata = {
  issuer: "https://auth.x.ai",
  token_endpoint: "https://auth.x.ai/oauth2/token",
  device_authorization_endpoint: "https://auth.x.ai/oauth2/device/code",
}

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } })
}

test("Grok discovery accepts only the pinned issuer and device/token endpoints", async () => {
  const urls = await resolveGrokOAuthMetadata((async () => response(metadata)) as unknown as typeof fetch)
  expect(urls).toEqual({ tokenURL: metadata.token_endpoint, deviceURL: metadata.device_authorization_endpoint })
  await expect(resolveGrokOAuthMetadata((async () => response({ ...metadata, issuer: "https://attacker.example" })) as unknown as typeof fetch))
    .rejects.toThrow("pinned issuer contract")
  await expect(resolveGrokOAuthMetadata((async () => response({ ...metadata, token_endpoint: "https://evil.example/token" })) as unknown as typeof fetch))
    .rejects.toThrow("pinned issuer contract")
})

test("Grok device authorization honors pending, slow_down, success and scope", async () => {
  const requests: Array<{ url: string; body: string }> = []
  const waits: number[] = []
  let tokenCalls = 0
  const fetcher = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input)
    const body = String(init?.body ?? "")
    requests.push({ url, body })
    if (url === "https://auth.x.ai/.well-known/openid-configuration") return response(metadata)
    if (url === metadata.device_authorization_endpoint) {
      return response({
        device_code: "synthetic-device-code-123",
        user_code: "ABCD-EFGH",
        verification_uri: "https://grok.com/activate",
        verification_uri_complete: "https://grok.com/activate?user_code=ABCD-EFGH",
        expires_in: 60,
        interval: 2,
      })
    }
    tokenCalls++
    if (tokenCalls === 1) return response({ error: "authorization_pending" }, 400)
    if (tokenCalls === 2) return response({ error: "slow_down" }, 400)
    return response({ access_token: "synthetic-access", refresh_token: "synthetic-refresh", expires_in: 3600, scope: "openid profile email offline_access grok-cli:access api:access" })
  }) as unknown as typeof fetch
  const authorization = await createGrokDeviceAuthorization(fetcher, async (ms, signal) => {
    waits.push(ms)
    return !signal.aborted
  })
  expect(authorization.url).toBe("https://grok.com/activate?user_code=ABCD-EFGH")
  expect(authorization.userCode).toBe("ABCD-EFGH")
  expect(requests[1]?.body).toContain("grok-cli%3Aaccess")
  const result = await authorization.callback(undefined, new AbortController().signal)
  expect(result).toMatchObject({ type: "success", access: "synthetic-access", refresh: "synthetic-refresh" })
  expect(waits).toEqual([2_000, 2_000, 7_000])
})

test("Grok device denial and expiry terminate without converting them to credentials", async () => {
  for (const error of ["access_denied", "expired_token"]) {
    const result = await pollGrokDeviceCode(
      (async () => response({ error }, 400)) as unknown as typeof fetch,
      metadata.token_endpoint,
      { deviceCode: "synthetic-device-code", userCode: "ABCD", verificationURI: "https://grok.com/activate", expiresAt: Date.now() + 60_000, intervalMs: 1_000, verificationURIComplete: undefined },
      new AbortController().signal,
      async () => true,
    )
    expect(result).toBeUndefined()
  }
})

test("Grok device polling aborts without another token request", async () => {
  const controller = new AbortController()
  let requests = 0
  const result = await pollGrokDeviceCode(
    (async () => { requests++; return response({ error: "authorization_pending" }, 400) }) as unknown as typeof fetch,
    metadata.token_endpoint,
    { deviceCode: "synthetic-device-code", userCode: "ABCD", verificationURI: "https://grok.com/activate", expiresAt: Date.now() + 60_000, intervalMs: 1_000, verificationURIComplete: undefined },
    controller.signal,
    async (_ms, signal) => { controller.abort(); return !signal.aborted },
  )
  expect(result).toBeUndefined()
  expect(requests).toBe(0)
})
