import path from "path"
import { Effect, Layer, Option, Record, Result, Schema, Context, Semaphore } from "effect"
import { zod } from "@/util/effect-zod"
import { Global } from "../global"
import { AppFileSystem } from "@mimo-ai/shared/filesystem"

export const OAUTH_DUMMY_KEY = "mimocode-oauth-dummy-key"
export const STORE_VERSION = 2 as const

const file = path.join(Global.Path.data, "auth.json")

const fail = (message: string) => (cause: unknown) => new AuthError({ message, cause })

export class Oauth extends Schema.Class<Oauth>("OAuth")({
  type: Schema.Literal("oauth"),
  refresh: Schema.String,
  access: Schema.String,
  expires: Schema.Number,
  accountId: Schema.optional(Schema.String),
  enterpriseUrl: Schema.optional(Schema.String),
}) {}

export class Api extends Schema.Class<Api>("ApiAuth")({
  type: Schema.Literal("api"),
  key: Schema.String,
  metadata: Schema.optional(Schema.Record(Schema.String, Schema.String)),
}) {}

export class WellKnown extends Schema.Class<WellKnown>("WellKnownAuth")({
  type: Schema.Literal("wellknown"),
  key: Schema.String,
  token: Schema.String,
}) {}

const _Info = Schema.Union([Oauth, Api, WellKnown]).annotate({ discriminator: "type", identifier: "Auth" })
export const Info = Object.assign(_Info, { zod: zod(_Info) })
export type Info = Schema.Schema.Type<typeof _Info>

export const BillingLane = Schema.Literals(["subscription", "metered_api", "local", "custom", "legacy"])
export type BillingLane = Schema.Schema.Type<typeof BillingLane>

/**
 * A stable credential identity inside one connection. `revision` is the CAS
 * token for refresh, reconnect, deletion, and provider-client cache fencing.
 * The Open Clank managed host encrypts `credential` at rest; the local file
 * backend exists only as an explicit compatibility/migration backend.
 */
export class Account extends Schema.Class<Account>("ProviderAuthAccount")({
  id: Schema.String,
  label: Schema.String,
  enabled: Schema.Boolean,
  order: Schema.Number,
  revision: Schema.Number,
  credentialRevision: Schema.Number,
  credential: Info,
  identity: Schema.optional(Schema.Record(Schema.String, Schema.String)),
}) {}

/** A rotation boundary. Different connection IDs and billing lanes never mix. */
export class Pool extends Schema.Class<Pool>("ProviderAuthPool")({
  connectionID: Schema.String,
  providerID: Schema.String,
  billingLane: BillingLane,
  revision: Schema.Number,
  cursor: Schema.Number,
  accounts: Schema.Record(Schema.String, Account),
}) {}

export class Store extends Schema.Class<Store>("ProviderAuthStoreV2")({
  version: Schema.Literal(STORE_VERSION),
  revision: Schema.Number,
  pools: Schema.Record(Schema.String, Pool),
}) {}

export const LegacyStore = Schema.Record(Schema.String, Info)
export type LegacyStore = Schema.Schema.Type<typeof LegacyStore>

export const emptyStore = (): Store => ({
  version: STORE_VERSION,
  revision: 0,
  pools: {},
})

export function readStoreV2(input: unknown): Store | undefined {
  const decoded = Schema.decodeUnknownOption(Store)(input)
  return Option.isSome(decoded) ? decoded.value : undefined
}

export function normalizeConnectionID(value: string): string {
  return value.replace(/\/+$/, "")
}

export function legacyAccountID(providerID: string): string {
  return `legacy:${normalizeConnectionID(providerID)}`
}

export function selectCompatibilityAccount(pool: Pool | undefined): Account | undefined {
  if (!pool) return
  const legacy = pool.accounts[legacyAccountID(pool.connectionID)]
  if (legacy?.enabled) return legacy
  return Object.values(pool.accounts)
    .filter((account) => account.enabled)
    .sort((left, right) => left.order - right.order || left.id.localeCompare(right.id))[0]
}

export class AuthError extends Schema.TaggedErrorClass<AuthError>()("AuthError", {
  message: Schema.String,
  cause: Schema.optional(Schema.Defect),
}) {}

export class RevisionConflict extends Schema.TaggedErrorClass<RevisionConflict>()("AuthRevisionConflict", {
  connectionID: Schema.String,
  accountID: Schema.optional(Schema.String),
  expectedRevision: Schema.Number,
  actualRevision: Schema.Number,
}) {}

export class AccountNotFound extends Schema.TaggedErrorClass<AccountNotFound>()("AuthAccountNotFound", {
  connectionID: Schema.String,
  accountID: Schema.String,
}) {}

export type MutationError = AuthError | RevisionConflict | AccountNotFound

export interface PutAccountInput {
  readonly connectionID: string
  readonly providerID: string
  readonly billingLane: BillingLane
  readonly accountID: string
  readonly label: string
  readonly credential: Info
  readonly enabled?: boolean
  readonly order?: number
  readonly identity?: Record<string, string>
  /** Zero means the account must not exist. */
  readonly expectedRevision: number
}

export interface ReplaceCredentialInput {
  readonly connectionID: string
  readonly accountID: string
  readonly expectedRevision: number
  readonly credential: Info
}

export interface RemoveAccountInput {
  readonly connectionID: string
  readonly accountID: string
  readonly expectedRevision: number
}

export interface RefreshLease {
  readonly leaseID: string
  readonly connectionID: string
  readonly accountID: string
  readonly credentialRevision: number
  readonly expiresAt: number
  readonly renewable: boolean
}

/**
 * Account-scoped refresh authority implemented by the managed Open Clank host.
 * A refresh result only becomes durable through `commit`, which must CAS the
 * credential revision. Implementations must fence expired/aborted lease IDs.
 */
export interface RefreshInterface {
  readonly acquire: (input: {
    connectionID: string
    accountID: string
    expectedRevision: number
    ttlMs?: number
  }) => Effect.Effect<RefreshLease, MutationError>
  readonly renew: (input: { leaseID: string; ttlMs?: number }) => Effect.Effect<RefreshLease, MutationError>
  readonly commit: (input: {
    leaseID: string
    expectedRevision: number
    credential: Info
  }) => Effect.Effect<Account, MutationError>
  readonly abort: (input: { leaseID: string }) => Effect.Effect<void, AuthError>
}

export class RefreshService extends Context.Service<RefreshService, RefreshInterface>()("@opencode/AuthRefresh") {}

export interface Interface {
  readonly get: (providerID: string) => Effect.Effect<Info | undefined, AuthError>
  readonly all: () => Effect.Effect<Record<string, Info>, AuthError>
  readonly set: (key: string, info: Info) => Effect.Effect<void, AuthError>
  readonly remove: (key: string) => Effect.Effect<void, AuthError>
  readonly snapshot: () => Effect.Effect<Store, AuthError>
  readonly listAccounts: (connectionID: string) => Effect.Effect<ReadonlyArray<Account>, AuthError>
  readonly getAccount: (connectionID: string, accountID: string) => Effect.Effect<Account | undefined, AuthError>
  readonly putAccount: (input: PutAccountInput) => Effect.Effect<Account, AuthError | RevisionConflict>
  readonly replaceCredential: (input: ReplaceCredentialInput) => Effect.Effect<Account, MutationError>
  readonly removeAccount: (input: RemoveAccountInput) => Effect.Effect<void, MutationError>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Auth") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const fsys = yield* AppFileSystem.Service
    const writeLock = Semaphore.makeUnsafe(1)
    const managed = process.env.OPEN_CLANK_MANAGED === "1"

    const snapshot = Effect.fn("Auth.snapshot")(function* () {
      // The managed host is the only provider credential authority. Never read
      // auth.json or a credential-bearing environment blob in this mode.
      if (managed) return emptyStore()
      let raw: unknown
      if (process.env.MIMOCODE_AUTH_CONTENT) {
        try {
          raw = JSON.parse(process.env.MIMOCODE_AUTH_CONTENT)
        } catch (err) {
          raw = {}
        }
      } else {
        raw = yield* fsys.readJson(file).pipe(Effect.orElseSucceed(() => ({})))
      }

      const current = readStoreV2(raw)
      if (current) return current
      if (typeof raw === "object" && raw !== null && !Array.isArray(raw) && ("version" in raw || "pools" in raw)) {
        return yield* new AuthError({ message: "Invalid versioned provider auth store" })
      }
      if (typeof raw === "object" && raw !== null && !Array.isArray(raw) && Object.keys(raw).length === 0) return emptyStore()
      return yield* new AuthError({ message: "Legacy provider auth store; run .clanker/tools/native/mimo auth with the explicit auth.json path" })
    })

    const persist = (store: Store) =>
      managed
        ? Effect.fail(new AuthError({ message: "Managed provider credentials must be mutated through Open Clank" }))
        : fsys.writeJson(file, store, 0o600).pipe(Effect.mapError(fail("Failed to write auth data")))

    const mutate = <A, E>(fn: (store: Store) => Effect.Effect<readonly [A, Store], E>) =>
      writeLock.withPermits(1)(
        Effect.gen(function* () {
          const store = yield* snapshot()
          const [result, next] = yield* fn(store)
          yield* persist(next)
          return result
        }),
      )

    const poolForCompatibility = (store: Store, providerID: string) => {
      const normalized = normalizeConnectionID(providerID)
      return (
        store.pools[normalized] ??
        Object.values(store.pools)
          .filter((pool) => pool.providerID === normalized)
          .sort((left, right) => left.connectionID.localeCompare(right.connectionID))[0]
      )
    }

    const all = Effect.fn("Auth.all")(function* () {
      const store = yield* snapshot()
      const result: Record<string, Info> = {}
      for (const pool of Object.values(store.pools).sort((left, right) =>
        left.connectionID.localeCompare(right.connectionID),
      )) {
        if (result[pool.providerID]) continue
        const account = selectCompatibilityAccount(pool)
        if (account) result[pool.providerID] = account.credential
      }
      return result
    })

    const get = Effect.fn("Auth.get")(function* (providerID: string) {
      const store = yield* snapshot()
      return selectCompatibilityAccount(poolForCompatibility(store, providerID))?.credential
    })

    const set = Effect.fn("Auth.set")(function* (key: string, info: Info) {
      const connectionID = normalizeConnectionID(key)
      yield* mutate((store) =>
        Effect.sync(() => {
          const currentPool = store.pools[connectionID]
          const accountID = legacyAccountID(connectionID)
          const current = currentPool?.accounts[accountID]
          const account: Account = {
            id: accountID,
            label: current?.label ?? "Imported account",
            enabled: true,
            order: current?.order ?? 0,
            revision: (current?.revision ?? 0) + 1,
            credentialRevision: (current?.credentialRevision ?? 0) + 1,
            credential: info,
            ...(current?.identity ? { identity: current.identity } : {}),
          }
          const pool: Pool = {
            connectionID,
            providerID: currentPool?.providerID ?? connectionID,
            billingLane: currentPool?.billingLane ?? "legacy",
            revision: (currentPool?.revision ?? 0) + 1,
            cursor: currentPool?.cursor ?? 0,
            accounts: { ...(currentPool?.accounts ?? {}), [accountID]: account },
          }
          return [
            undefined,
            { ...store, revision: store.revision + 1, pools: { ...store.pools, [connectionID]: pool } },
          ]
        }),
      )
    })

    const remove = Effect.fn("Auth.remove")(function* (key: string) {
      const connectionID = normalizeConnectionID(key)
      yield* mutate((store) =>
        Effect.sync(() => {
          const pools = { ...store.pools }
          delete pools[connectionID]
          return [undefined, { ...store, revision: store.revision + 1, pools }]
        }),
      )
    })

    const listAccounts = Effect.fn("Auth.listAccounts")(function* (connectionID: string) {
      const store = yield* snapshot()
      return Object.values(store.pools[normalizeConnectionID(connectionID)]?.accounts ?? {}).sort(
        (left, right) => left.order - right.order || left.id.localeCompare(right.id),
      )
    })

    const getAccount = Effect.fn("Auth.getAccount")(function* (connectionID: string, accountID: string) {
      const store = yield* snapshot()
      return store.pools[normalizeConnectionID(connectionID)]?.accounts[accountID]
    })

    const putAccount = Effect.fn("Auth.putAccount")((input: PutAccountInput) =>
      mutate((store) =>
        Effect.gen(function* () {
          const connectionID = normalizeConnectionID(input.connectionID)
          const currentPool = store.pools[connectionID]
          const current = currentPool?.accounts[input.accountID]
          const actualRevision = current?.revision ?? 0
          if (actualRevision !== input.expectedRevision) {
            return yield* new RevisionConflict({
              connectionID,
              accountID: input.accountID,
              expectedRevision: input.expectedRevision,
              actualRevision,
            })
          }
          if (
            currentPool &&
            (currentPool.providerID !== input.providerID || currentPool.billingLane !== input.billingLane)
          ) {
            return yield* new AuthError({
              message: `Connection ${connectionID} cannot change provider or billing lane`,
            })
          }
          const account: Account = {
            id: input.accountID,
            label: input.label,
            enabled: input.enabled ?? current?.enabled ?? true,
            order: input.order ?? current?.order ?? Object.keys(currentPool?.accounts ?? {}).length,
            revision: actualRevision + 1,
            credentialRevision: (current?.credentialRevision ?? 0) + 1,
            credential: input.credential,
            ...(input.identity
              ? { identity: input.identity }
              : current?.identity
                ? { identity: current.identity }
                : {}),
          }
          const pool: Pool = {
            connectionID,
            providerID: input.providerID,
            billingLane: input.billingLane,
            revision: (currentPool?.revision ?? 0) + 1,
            cursor: currentPool?.cursor ?? 0,
            accounts: { ...(currentPool?.accounts ?? {}), [account.id]: account },
          }
          return [
            account,
            { ...store, revision: store.revision + 1, pools: { ...store.pools, [connectionID]: pool } },
          ] as const
        }),
      ),
    )

    const replaceCredential = Effect.fn("Auth.replaceCredential")((input: ReplaceCredentialInput) =>
      mutate((store) =>
        Effect.gen(function* () {
          const connectionID = normalizeConnectionID(input.connectionID)
          const pool = store.pools[connectionID]
          const current = pool?.accounts[input.accountID]
          if (!pool || !current) {
            return yield* new AccountNotFound({ connectionID, accountID: input.accountID })
          }
          if (current.credentialRevision !== input.expectedRevision) {
            return yield* new RevisionConflict({
              connectionID,
              accountID: input.accountID,
              expectedRevision: input.expectedRevision,
              actualRevision: current.credentialRevision,
            })
          }
          const account: Account = {
            ...current,
            revision: current.revision + 1,
            credentialRevision: current.credentialRevision + 1,
            credential: input.credential,
          }
          const nextPool: Pool = {
            ...pool,
            revision: pool.revision + 1,
            accounts: { ...pool.accounts, [account.id]: account },
          }
          return [
            account,
            { ...store, revision: store.revision + 1, pools: { ...store.pools, [connectionID]: nextPool } },
          ] as const
        }),
      ),
    )

    const removeAccount = Effect.fn("Auth.removeAccount")((input: RemoveAccountInput) =>
      mutate((store) =>
        Effect.gen(function* () {
          const connectionID = normalizeConnectionID(input.connectionID)
          const pool = store.pools[connectionID]
          const current = pool?.accounts[input.accountID]
          if (!pool || !current) {
            return yield* new AccountNotFound({ connectionID, accountID: input.accountID })
          }
          if (current.revision !== input.expectedRevision) {
            return yield* new RevisionConflict({
              connectionID,
              accountID: input.accountID,
              expectedRevision: input.expectedRevision,
              actualRevision: current.revision,
            })
          }
          const accounts = { ...pool.accounts }
          delete accounts[input.accountID]
          const pools = { ...store.pools }
          if (Object.keys(accounts).length === 0) delete pools[connectionID]
          else pools[connectionID] = { ...pool, revision: pool.revision + 1, accounts }
          return [undefined, { ...store, revision: store.revision + 1, pools }] as const
        }),
      ),
    )

    return Service.of({
      get,
      all,
      set,
      remove,
      snapshot,
      listAccounts,
      getAccount,
      putAccount,
      replaceCredential,
      removeAccount,
    })
  }),
)

export const defaultLayer = layer.pipe(Layer.provide(AppFileSystem.defaultLayer))

export * as Auth from "."
