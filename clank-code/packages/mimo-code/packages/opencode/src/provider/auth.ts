import type { AuthOAuthResult, Hooks } from "@mimo-ai/plugin"
import { NamedError } from "@mimo-ai/shared/util/error"
import { Auth } from "@/auth"
import { InstanceState } from "@/effect"
import { zod } from "@/util/effect-zod"
import { withStatics } from "@/util/schema"
import { Plugin } from "../plugin"
import { ProviderID } from "./schema"
import { Array as Arr, Effect, Layer, Record, Result, Context, Schema } from "effect"
import z from "zod"

const When = Schema.Struct({
  key: Schema.String,
  op: Schema.Literals(["eq", "neq"]),
  value: Schema.String,
})

const TextPrompt = Schema.Struct({
  type: Schema.Literal("text"),
  key: Schema.String,
  message: Schema.String,
  placeholder: Schema.optional(Schema.String),
  when: Schema.optional(When),
})

const SelectOption = Schema.Struct({
  label: Schema.String,
  value: Schema.String,
  hint: Schema.optional(Schema.String),
})

const SelectPrompt = Schema.Struct({
  type: Schema.Literal("select"),
  key: Schema.String,
  message: Schema.String,
  options: Schema.Array(SelectOption),
  when: Schema.optional(When),
})

const Prompt = Schema.Union([TextPrompt, SelectPrompt])

export class Method extends Schema.Class<Method>("ProviderAuthMethod")({
  type: Schema.Literals(["oauth", "api"]),
  acquisitionMethod: Schema.optional(Schema.Literals(["manual-key", "browser", "device"])),
  credentialKinds: Schema.optional(Schema.Array(Schema.Literals(["api", "oauth"]))),
  billingLanes: Schema.optional(Schema.Array(Schema.Literals(["metered_api", "subscription", "custom", "local"]))),
  label: Schema.String,
  prompts: Schema.optional(Schema.Array(Prompt)),
}) {
  static readonly zod = zod(this)
}

export const Methods = Schema.Record(Schema.String, Schema.Array(Method)).pipe(withStatics((s) => ({ zod: zod(s) })))
export type Methods = typeof Methods.Type

export class Authorization extends Schema.Class<Authorization>("ProviderAuthAuthorization")({
  url: Schema.String,
  method: Schema.Literals(["auto", "code"]),
  instructions: Schema.String,
  userCode: Schema.optional(Schema.String),
}) {
  static readonly zod = zod(this)
}

export const AuthorizeInput = Schema.Struct({
  method: Schema.Number.annotate({ description: "Auth method index" }),
  inputs: Schema.optional(Schema.Record(Schema.String, Schema.String)).annotate({ description: "Prompt inputs" }),
  flowID: Schema.optional(Schema.String).annotate({ description: "Caller-owned authorization flow ID" }),
  redirectURI: Schema.optional(Schema.String).annotate({ description: "Public OAuth callback URI" }),
  state: Schema.optional(Schema.String).annotate({ description: "Caller-owned OAuth state" }),
  expiresAt: Schema.optional(Schema.Number).annotate({ description: "Caller-owned authorization flow expiry" }),
}).pipe(withStatics((s) => ({ zod: zod(s) })))
export type AuthorizeInput = Schema.Schema.Type<typeof AuthorizeInput>

export const CallbackInput = Schema.Struct({
  method: Schema.Number.annotate({ description: "Auth method index" }),
  code: Schema.optional(Schema.String).annotate({ description: "OAuth authorization code" }),
  flowID: Schema.optional(Schema.String).annotate({ description: "Caller-owned authorization flow ID" }),
}).pipe(withStatics((s) => ({ zod: zod(s) })))
export type CallbackInput = Schema.Schema.Type<typeof CallbackInput>

export const OauthMissing = NamedError.create("ProviderAuthOauthMissing", z.object({ providerID: ProviderID.zod }))

export const OauthCodeMissing = NamedError.create(
  "ProviderAuthOauthCodeMissing",
  z.object({ providerID: ProviderID.zod }),
)

export const OauthCallbackFailed = NamedError.create("ProviderAuthOauthCallbackFailed", z.object({}))

export const ValidationFailed = NamedError.create(
  "ProviderAuthValidationFailed",
  z.object({
    field: z.string(),
    message: z.string(),
  }),
)

export type Error =
  | Auth.AuthError
  | InstanceType<typeof OauthMissing>
  | InstanceType<typeof OauthCodeMissing>
  | InstanceType<typeof OauthCallbackFailed>
  | InstanceType<typeof ValidationFailed>

const toAuthError = (error: unknown): Error =>
  error instanceof globalThis.Error
    ? new Auth.AuthError({ message: error.message })
    : (error as Error)

type Hook = NonNullable<Hooks["auth"]>
const OPENCLANK_FLOW_INPUT = "__openclank_flow_id"
const OPENCLANK_REDIRECT_INPUT = "__openclank_redirect_uri"
const OPENCLANK_STATE_INPUT = "__openclank_state"
const OAUTH_FLOW_TTL_MS = 10 * 60 * 1000

type PendingOAuth = {
  providerID: ProviderID
  result: AuthOAuthResult & { userCode?: string; dispose?: () => void | Promise<void> }
  expiresAt: number
  running: boolean
  cancelled: boolean
  abortController: AbortController
  disposed: boolean
}

type AuthWriter = Pick<Auth.Interface, "get" | "set" | "remove">

const disposeResult = (result: PendingOAuth["result"]) =>
  Effect.tryPromise({
    try: async () => {
      await result.dispose?.()
    },
    catch: () => undefined,
  }).pipe(Effect.catch(() => Effect.succeed(undefined)))

const disposePending = (entry: PendingOAuth) =>
  Effect.suspend(() => {
    if (entry.disposed) return Effect.void
    entry.disposed = true
    return disposeResult(entry.result)
  })

function sameAuth(left: Auth.Info | undefined, right: Auth.Info): boolean {
  return left !== undefined && JSON.stringify(left) === JSON.stringify(right)
}

/**
 * Commit one flow-produced credential with a cancellation fence on both sides
 * of the write. If cancellation lands while the filesystem write is pending,
 * restore the exact prior credential (or remove the newly-created entry).
 *
 * The final `invalid()` check and return have no async boundary between them,
 * so a successful return cannot race a later cancel inside this process.
 */
export const commitFlowAuth = Effect.fn("ProviderAuth.commitFlowAuth")(function* (
  auth: AuthWriter,
  providerID: string,
  next: Auth.Info,
  invalid: () => boolean,
) {
  const previous = yield* auth.get(providerID)
  if (invalid()) return false

  yield* auth.set(providerID, next)
  if (!invalid()) return true

  const current = yield* auth.get(providerID)
  // Do not clobber a newer, independent credential update that won the race.
  if (sameAuth(current, next)) {
    if (previous) yield* auth.set(providerID, previous)
    else yield* auth.remove(providerID)
  }
  return false
})

export interface Interface {
  readonly methods: () => Effect.Effect<Methods>
  readonly authorize: (
    input: {
      providerID: ProviderID
    } & AuthorizeInput,
  ) => Effect.Effect<Authorization | undefined, Error>
  /**
   * Complete one provider flow and return the normalized credential without
   * persisting it.  Open Clank managed workers use this path so the host can
   * encrypt and CAS the credential in its owner-scoped database before any
   * browser callback is acknowledged.
   */
  readonly exchange: (
    input: { providerID: ProviderID } & CallbackInput,
  ) => Effect.Effect<Auth.Info | undefined, Error>
  readonly callback: (input: { providerID: ProviderID } & CallbackInput) => Effect.Effect<void, Error>
  readonly cancel: (input: { providerID: ProviderID; flowID: string }) => Effect.Effect<void>
}

interface State {
  hooks: Record<ProviderID, Hook>
  pending: Map<string, PendingOAuth>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/ProviderAuth") {}

export const layer: Layer.Layer<Service, never, Auth.Service | Plugin.Service> = Layer.effect(
  Service,
  Effect.gen(function* () {
    const auth = yield* Auth.Service
    const plugin = yield* Plugin.Service
    const state = yield* InstanceState.make<State>(
      Effect.fn("ProviderAuth.state")(function* () {
        const plugins = yield* plugin.list()
        return {
          hooks: Record.fromEntries(
            Arr.filterMap(plugins, (x) =>
              x.auth?.provider !== undefined
                ? Result.succeed([ProviderID.make(x.auth.provider), x.auth] as const)
                : Result.failVoid,
            ),
          ),
          pending: new Map<string, PendingOAuth>(),
        }
      }),
    )

    const decode = Schema.decodeUnknownSync(Methods)
    const methods = Effect.fn("ProviderAuth.methods")(function* () {
      const hooks = (yield* InstanceState.get(state)).hooks
      return decode(
        Record.map(hooks, (item) =>
          item.methods.map((method) => ({
            type: method.type,
            ...(method.acquisitionMethod === undefined ? {} : { acquisitionMethod: method.acquisitionMethod }),
            ...(method.credentialKinds === undefined ? {} : { credentialKinds: method.credentialKinds }),
            ...(method.billingLanes === undefined ? {} : { billingLanes: method.billingLanes }),
            label: method.label,
            prompts: method.prompts?.map((prompt) => {
              if (prompt.type === "select") {
                return {
                  type: "select" as const,
                  key: prompt.key,
                  message: prompt.message,
                  options: prompt.options,
                  when: prompt.when,
                }
              }
              return {
                type: "text" as const,
                key: prompt.key,
                message: prompt.message,
                placeholder: prompt.placeholder,
                when: prompt.when,
              }
            }),
          })),
        ),
      )
    })

    const authorize = Effect.fn("ProviderAuth.authorize")(function* (
      input: { providerID: ProviderID } & AuthorizeInput,
    ) {
      const { hooks, pending } = yield* InstanceState.get(state)
      const method = hooks[input.providerID].methods[input.method]
      if (method.type !== "oauth") return

      if (method.prompts && input.inputs) {
        for (const prompt of method.prompts) {
          if (prompt.type === "text" && prompt.validate && input.inputs[prompt.key] !== undefined) {
            const error = prompt.validate(input.inputs[prompt.key])
            if (error) return yield* Effect.fail(new ValidationFailed({ field: prompt.key, message: error }))
          }
        }
      }

      const hookInputs = { ...(input.inputs ?? {}) }
      if (input.flowID) hookInputs[OPENCLANK_FLOW_INPUT] = input.flowID
      if (input.redirectURI) hookInputs[OPENCLANK_REDIRECT_INPUT] = input.redirectURI
      if (input.state) hookInputs[OPENCLANK_STATE_INPUT] = input.state
      const result: AuthOAuthResult & { userCode?: string } = yield* Effect.promise(() => method.authorize(hookInputs))
      const now = Date.now()
      for (const [key, entry] of pending) {
        if (entry.expiresAt <= now) {
          entry.cancelled = true
          entry.abortController.abort()
          yield* disposePending(entry)
          pending.delete(key)
        }
      }
      const flowID = input.flowID
      const pendingKey = flowID || input.providerID
      const expiresAt = input.expiresAt ?? now + OAUTH_FLOW_TTL_MS
      if (expiresAt <= now || expiresAt > now + OAUTH_FLOW_TTL_MS) {
        yield* disposeResult(result)
        return yield* Effect.fail(new ValidationFailed({ field: "expiresAt", message: "Login flow has expired" }))
      }
      if (flowID && pending.has(pendingKey)) {
        yield* disposeResult(result)
        return yield* Effect.fail(new ValidationFailed({ field: "flowID", message: "Login flow already exists" }))
      }
      pending.set(pendingKey, {
        providerID: input.providerID,
        result,
        expiresAt,
        running: false,
        cancelled: false,
        disposed: false,
        abortController: new AbortController(),
      })
      return {
        url: result.url,
        method: result.method,
        instructions: result.instructions,
        ...(result.userCode === undefined ? {} : { userCode: result.userCode }),
      }
    })

    const completeBody = Effect.fn("ProviderAuth.completeBody")(function* (
      input: { providerID: ProviderID } & CallbackInput,
      persist: boolean,
    ) {
      const pending = (yield* InstanceState.get(state)).pending
      const pendingKey = input.flowID || input.providerID
      const entry = pending.get(pendingKey)
      if (!entry || entry.providerID !== input.providerID) {
        pending.delete(pendingKey)
        return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
      }
      if (entry.expiresAt <= Date.now() || entry.cancelled) {
        entry.cancelled = true
        entry.abortController.abort()
        yield* disposePending(entry)
        pending.delete(pendingKey)
        return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
      }
      if (entry.running) return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
      const match = entry.result
      if (match.method === "code" && !input.code) {
        return yield* Effect.fail(new OauthCodeMissing({ providerID: input.providerID }))
      }

      entry.running = true
      const invoke = match.callback as (
        code?: string,
        signal?: AbortSignal,
      ) => Promise<Awaited<ReturnType<AuthOAuthResult["callback"]>>>
      const cleanup = disposePending(entry)
      const result: Awaited<ReturnType<AuthOAuthResult["callback"]>> = yield* Effect.tryPromise(
        () => invoke(input.code, entry.abortController.signal),
      ).pipe(
        Effect.tapError(() => Effect.sync(() => pending.delete(pendingKey))),
        Effect.ensuring(cleanup),
      )
      if (entry.cancelled || entry.expiresAt <= Date.now()) {
        yield* disposePending(entry)
        pending.delete(pendingKey)
        return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
      }
      if (!result || result.type !== "success") {
        yield* disposePending(entry)
        pending.delete(pendingKey)
        return yield* Effect.fail(new OauthCallbackFailed({}))
      }

      let nextAuth: Auth.Info | undefined
      if ("key" in result) {
        nextAuth = {
          type: "api",
          key: result.key,
          ...("metadata" in result && result.metadata ? { metadata: result.metadata } : {}),
        }
      } else if ("refresh" in result) {
        const { type: _, provider: __, refresh, access, expires, metadata: _legacyMetadata, credentialMetadata, ...extra } = result
        nextAuth = {
          type: "oauth",
          access,
          refresh,
          expires,
          ...extra,
          ...(credentialMetadata ? { metadata: credentialMetadata } : {}),
        }
      }
      if (nextAuth) {
        if (persist) {
          const committed = yield* commitFlowAuth(
            auth,
            input.providerID,
            nextAuth,
            () => entry.cancelled || entry.expiresAt <= Date.now(),
          )
          if (!committed) {
            yield* disposePending(entry)
            pending.delete(pendingKey)
            return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
          }
        }
        yield* disposePending(entry)
        pending.delete(pendingKey)
        return nextAuth
      }
      yield* disposePending(entry)
      pending.delete(pendingKey)
      return undefined
    })

    const complete = Effect.fn("ProviderAuth.complete")(function* (
      input: { providerID: ProviderID } & CallbackInput,
      persist: boolean,
    ) {
      const pending = (yield* InstanceState.get(state)).pending
      const pendingKey = input.flowID || input.providerID
      const entry = pending.get(pendingKey)
      if (!entry) return yield* completeBody(input, persist)
      if (entry.running) return yield* Effect.fail(new OauthMissing({ providerID: input.providerID }))
      const cleanup = disposePending(entry).pipe(
        Effect.tap(() => Effect.sync(() => pending.delete(pendingKey))),
      )
      return yield* completeBody(input, persist).pipe(Effect.ensuring(cleanup))
    })

    const exchange = Effect.fn("ProviderAuth.exchange")(function* (input: { providerID: ProviderID } & CallbackInput) {
      return yield* complete(input, false).pipe(Effect.mapError(toAuthError))
    })

    const callback = Effect.fn("ProviderAuth.callback")(function* (input: { providerID: ProviderID } & CallbackInput) {
      yield* complete(input, true).pipe(Effect.mapError(toAuthError))
    })

    const cancel = Effect.fn("ProviderAuth.cancel")(function* (input: { providerID: ProviderID; flowID: string }) {
      const pending = (yield* InstanceState.get(state)).pending
      const entry = pending.get(input.flowID)
      if (entry?.providerID === input.providerID) {
        entry.cancelled = true
        entry.abortController.abort()
        yield* disposePending(entry)
        pending.delete(input.flowID)
      }
    })

    return Service.of({ methods, authorize, exchange, callback, cancel })
  }),
)

export const defaultLayer = Layer.suspend(() =>
  layer.pipe(Layer.provide(Auth.defaultLayer), Layer.provide(Plugin.defaultLayer)),
)
