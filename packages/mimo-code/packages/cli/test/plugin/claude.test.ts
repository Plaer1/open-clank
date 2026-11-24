import { describe, expect, test } from "bun:test"
import {
  CLAUDE_PROFILE,
  ClaudeOAuthError,
  ClaudeAuthPlugin,
  buildClaudeAuthorizeURL,
  createClaudeOAuthResult,
  createPKCE,
  exchangeClaudeCode,
  parseClaudeCallback,
  refreshClaudeCredential,
} from "../../src/plugin/claude"
import { composeAnthropicAuthHooks } from "../../src/plugin/index"

describe("Claude OAuth helpers", () => {
  test("exposes one native Anthropic subscription method", async () => {
    const hook = await ClaudeAuthPlugin({} as never)
    expect(hook.auth?.provider).toBe("anthropic")
    expect(hook.auth?.methods).toHaveLength(2)
    expect(hook.auth?.methods[0]?.type).toBe("oauth")
    expect(hook.auth?.methods[1]?.label).toContain("paste")
  })

  test("composes Claude methods without replacing the proxy loader", () => {
    const loader = async () => ({ fetch: async () => new Response() })
    const composed = composeAnthropicAuthHooks({ auth: { provider: "anthropic", loader, methods: [] } }, {
      auth: { provider: "anthropic", methods: [{ type: "oauth", label: "native", authorize: async () => { throw new Error("fixture") } }] },
    })
    expect(composed.auth?.loader).toBe(loader)
    expect(composed.auth?.methods).toHaveLength(1)
  })

  test("creates S256 PKCE and a pinned authorize profile", () => {
    const pkce = createPKCE((size = 32) => "a".repeat(size))
    expect(pkce.state).toBe("a".repeat(32))
    expect(pkce.verifier).toBe("a".repeat(32))
    expect(buildClaudeAuthorizeURL({ pkce, redirectURI: CLAUDE_PROFILE.defaultRedirectURI })).toContain(
      "code_challenge_method=S256",
    )
  })

  test("requires exact redirect and matching state for URL and code#state forms", () => {
    const expected = { redirectURI: CLAUDE_PROFILE.defaultRedirectURI, state: "state" }
    expect(parseClaudeCallback("http://localhost:54545/callback?code=code&state=state", expected)).toEqual({ code: "code", state: "state" })
    expect(parseClaudeCallback("code#state", expected)).toEqual({ code: "code", state: "state" })
    expect(() => parseClaudeCallback("code", expected)).toThrow()
    expect(() => parseClaudeCallback("http://localhost:54546/callback?code=code&state=state", expected)).toThrow()
    expect(() => parseClaudeCallback("http://localhost:54545/callback?code=code&state=wrong", expected)).toThrow()
    expect(() => parseClaudeCallback("http://localhost:54545/callback?code=one&code=two&state=state", expected)).toThrow()
    expect(parseClaudeCallback("http://localhost:54545/callback?error=access_denied&state=state", expected)).toMatchObject({ error: "access_denied" })
  })

  test("classifies provider denial and disposes a flow before callback", async () => {
    const result = await createClaudeOAuthResult({ method: "code", state: "host-state" })
    await result.dispose?.()
    await expect(result.callback("code#host-state")).resolves.toBeUndefined()
    await expect((async () => { throw new ClaudeOAuthError("denied") })()).rejects.toMatchObject({ category: "denied" })
  })

  test("manual flow preserves host state and accepts the original callback payload", async () => {
    const result = await createClaudeOAuthResult({
      method: "code",
      state: "host-state",
      fetch: async (_input, init) => {
        const body = JSON.parse(String(init?.body))
        expect(body.state).toBe("host-state")
        expect(body.redirect_uri).toBe("http://localhost:54545/callback")
        return new Response(JSON.stringify({ access_token: "access", refresh_token: "refresh", expires_in: 60 }), { status: 200 })
      },
      now: () => 1_000,
    })
    expect(new URL(result.url).searchParams.get("state")).toBe("host-state")
    await expect(result.callback("http://localhost:54545/callback?code=code&state=host-state")).resolves.toMatchObject({ access: "access" })
  })

  test("external abort closes auto listener and releases a waiting callback", async () => {
    const controller = new AbortController()
    let closed = 0
    const result = await createClaudeOAuthResult({
      method: "auto",
      signal: controller.signal,
      listenerFactory: async () => ({ redirectURI: "http://localhost:54546/callback", close: () => { closed++ } }),
    })
    const pending = result.callback()
    controller.abort()
    await expect(pending).resolves.toBeUndefined()
    expect(closed).toBe(1)
  })

  test("callback signal abort releases an auto waiter", async () => {
    const controller = new AbortController()
    let closed = 0
    const result = await createClaudeOAuthResult({
      method: "auto",
      listenerFactory: async () => ({ redirectURI: "http://localhost:54546/callback", close: () => { closed++ } }),
    })
    const pending = result.callback(undefined, controller.signal)
    controller.abort()
    await expect(pending).resolves.toBeUndefined()
    expect(closed).toBe(1)
  })

  test("posts authorization exchange and retains refresh token on refresh rotation omission", async () => {
    expect(CLAUDE_PROFILE.tokenURL).toBe("https://platform.claude.com/v1/oauth/token")
    const requests: Request[] = []
    const fakeFetch = async (input: RequestInfo | URL, init?: RequestInit) => {
      requests.push(new Request(input, init))
      return new Response(JSON.stringify({ access_token: "access", refresh_token: "next-refresh", expires_in: 60 }), { status: 200 })
    }
    const credential = await exchangeClaudeCode({
      callback: { code: "code", state: "state" },
      pkce: { state: "state", verifier: "verifier", challenge: "challenge" },
      redirectURI: CLAUDE_PROFILE.defaultRedirectURI,
      fetch: fakeFetch,
      now: () => 1_000,
    })
    expect(credential).toEqual({ access: "access", refresh: "next-refresh", expires: 61_000 })
    expect(requests[0].url).toBe(CLAUDE_PROFILE.tokenURL)
    expect(await requests[0].json()).toMatchObject({ grant_type: "authorization_code", code: "code", state: "state" })
    const rotated = await refreshClaudeCredential({
      refresh: "old-refresh",
      fetch: async () => new Response(JSON.stringify({ access_token: "new-access", expires_in: 60 }), { status: 200 }),
      now: () => 2_000,
      accountId: "account-1",
    })
    expect(rotated.refresh).toBe("old-refresh")
    expect(rotated.accountId).toBe("account-1")
  })

  test("auto flow uses the actual listener port and disposes on completion", async () => {
    let callback: ((value: string) => boolean | "denied") | undefined
    let closed = 0
    const result = await createClaudeOAuthResult({
      method: "auto",
      state: "host-state",
      listenerFactory: async ({ onCallback }) => {
        callback = onCallback
        return { redirectURI: "http://localhost:54546/callback", close: () => { closed++ } }
      },
      fetch: async () => new Response(JSON.stringify({ access_token: "access", refresh_token: "refresh", expires_in: 60 }), { status: 200 }),
      now: () => 1_000,
    })
    expect(new URL(new URL(result.url).searchParams.get("redirect_uri")!).port).toBe("54546")
    expect(new URL(result.url).searchParams.get("state")).toBe("host-state")
    expect(callback?.("http://localhost:54546/callback?code=bad&state=wrong")).toBe(false)
    callback?.("http://localhost:54546/callback?code=code&state=host-state")
    await expect(result.callback()).resolves.toMatchObject({ access: "access" })
    await result.dispose?.()
    expect(closed).toBe(1)
  })
})
