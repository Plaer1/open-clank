import { afterEach, expect, test } from "bun:test"
import { Effect, Layer } from "effect"
import { tmpdir } from "../fixture/fixture"
import { Instance } from "../../src/project/instance"
import { Auth } from "../../src/auth"
import { Plugin } from "../../src/plugin"
import { ClaudeAuthPlugin, CLAUDE_PROFILE } from "../../src/plugin/claude"
import { ProviderAuth } from "../../src/provider"
import { ProviderID } from "../../src/provider/schema"

// The Claude subscription login through ProviderAuth (authorize → exchange / callback) against a
// fake token endpoint. Guards the provider/auth.ts merge (card M2-A15): the real plugin method,
// the real flow registry, no network.

const ANTHROPIC = ProviderID.make("anthropic")
const PASTE = 1 // "Claude subscription (paste callback)"
const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

function fakeTokenEndpoint() {
  const posts: Array<{ url: string; body: Record<string, string> }> = []
  globalThis.fetch = (async (input: RequestInfo | URL, init?: RequestInit) => {
    posts.push({ url: String(input), body: JSON.parse(String(init?.body)) })
    return new Response(
      JSON.stringify({ access_token: `access-${posts.length}`, refresh_token: `refresh-${posts.length}`, expires_in: 3600 }),
      { status: 200, headers: { "content-type": "application/json" } },
    )
  }) as typeof fetch
  return posts
}

async function withProviderAuth<A>(directory: string, fn: (svc: ProviderAuth.Interface, auth: Auth.Interface) => Effect.Effect<A, unknown>) {
  const hook = await ClaudeAuthPlugin({} as never)
  const layer = Layer.merge(
    ProviderAuth.layer.pipe(
      Layer.provide(Auth.defaultLayer),
      Layer.provide(Layer.mock(Plugin.Service)({ list: () => Effect.succeed([hook]) })),
    ),
    Auth.defaultLayer,
  )
  return Instance.provide({
    directory,
    fn: () =>
      Effect.runPromise(
        Effect.gen(function* () {
          const auth = yield* Auth.Service
          yield* auth.remove(ANTHROPIC)
          return yield* fn(yield* ProviderAuth.Service, auth)
        }).pipe(Effect.provide(layer)),
      ),
  })
}

test("Claude login: exchange returns the credential without persisting it (managed host path)", async () => {
  await using tmp = await tmpdir({})
  const posts = fakeTokenEndpoint()
  const result = await withProviderAuth(tmp.path, (svc, auth) =>
    Effect.gen(function* () {
      const methods = yield* svc.methods()
      const authorized = yield* svc.authorize({ providerID: ANTHROPIC, method: PASTE, flowID: "flow-1", state: "host-state" })
      const credential = yield* svc.exchange({
        providerID: ANTHROPIC,
        method: PASTE,
        flowID: "flow-1",
        code: `${CLAUDE_PROFILE.defaultRedirectURI}?code=the-code&state=host-state`,
      })
      return { methods, authorized, credential, stored: yield* auth.get(ANTHROPIC) }
    }),
  )
  expect(result.methods[ANTHROPIC]).toHaveLength(2)
  expect(result.authorized?.method).toBe("code")
  const url = new URL(result.authorized!.url)
  expect(`${url.origin}${url.pathname}`).toBe(CLAUDE_PROFILE.authorizeURL)
  expect(url.searchParams.get("state")).toBe("host-state")
  expect(posts).toHaveLength(1)
  expect(posts[0].url).toBe(CLAUDE_PROFILE.tokenURL)
  expect(posts[0].body).toMatchObject({
    grant_type: "authorization_code",
    code: "the-code",
    state: "host-state",
    redirect_uri: CLAUDE_PROFILE.defaultRedirectURI,
  })
  expect(posts[0].body.code_verifier).toBeTruthy()
  expect(result.credential).toMatchObject({ type: "oauth", access: "access-1", refresh: "refresh-1" })
  expect(result.stored).toBeUndefined()
}, 30000)

test("Claude login: callback persists the credential under anthropic and the flow cannot be replayed", async () => {
  await using tmp = await tmpdir({})
  const posts = fakeTokenEndpoint()
  const code = `${CLAUDE_PROFILE.defaultRedirectURI}?code=the-code&state=host-state`
  const result = await withProviderAuth(tmp.path, (svc, auth) =>
    Effect.gen(function* () {
      yield* svc.authorize({ providerID: ANTHROPIC, method: PASTE, flowID: "flow-2", state: "host-state" })
      yield* svc.callback({ providerID: ANTHROPIC, method: PASTE, flowID: "flow-2", code })
      const replay = yield* svc.callback({ providerID: ANTHROPIC, method: PASTE, flowID: "flow-2", code }).pipe(Effect.exit)
      const stored = yield* auth.get(ANTHROPIC)
      yield* auth.remove(ANTHROPIC)
      return { stored, replay: replay._tag }
    }),
  )
  expect(posts).toHaveLength(1)
  expect(result.stored).toMatchObject({ type: "oauth", access: "access-1", refresh: "refresh-1" })
  expect(result.replay).toBe("Failure")
}, 30000)

test("Claude login: a wrong state is refused and nothing is stored", async () => {
  await using tmp = await tmpdir({})
  const posts = fakeTokenEndpoint()
  const result = await withProviderAuth(tmp.path, (svc, auth) =>
    Effect.gen(function* () {
      yield* svc.authorize({ providerID: ANTHROPIC, method: PASTE, flowID: "flow-3", state: "host-state" })
      const exit = yield* svc
        .callback({
          providerID: ANTHROPIC,
          method: PASTE,
          flowID: "flow-3",
          code: `${CLAUDE_PROFILE.defaultRedirectURI}?code=the-code&state=other-state`,
        })
        .pipe(Effect.exit)
      return { exit: exit._tag, stored: yield* auth.get(ANTHROPIC) }
    }),
  )
  expect(result.exit).toBe("Failure")
  expect(posts).toHaveLength(0)
  expect(result.stored).toBeUndefined()
}, 30000)
