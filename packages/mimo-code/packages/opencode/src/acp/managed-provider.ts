import type { AgentSideConnection } from "@agentclientprotocol/sdk"
import { Schema } from "effect"
import { Auth } from "@/auth"
import { AccountSelection } from "@/provider/account-selection"
import { OpenClankManagedProtocol } from "./openclank-protocol"

export const MANAGED_ENV = "OPEN_CLANK_MANAGED" as const

export function enabled(): boolean {
  return process.env[MANAGED_ENV] === "1"
}

export const RouteContext = Schema.Struct({
  rootOperationID: Schema.String,
  connectionID: Schema.String,
  providerID: Schema.String,
  billingLane: Auth.BillingLane,
  modelID: Schema.String,
  modelRouteID: Schema.optional(Schema.String),
  preferredAccountID: Schema.optional(Schema.String),
  inheritedAccountID: Schema.optional(Schema.String),
  grantID: Schema.optional(Schema.String),
})
export type RouteContext = Schema.Schema.Type<typeof RouteContext>

const CredentialLease = Schema.Struct({
  leaseID: Schema.String,
  connectionID: Schema.String,
  accountID: Schema.String,
  credentialRevision: Schema.Number,
  expiresAt: Schema.Number,
  credential: Auth.Info,
})

const RefreshLease = Schema.Struct({
  leaseID: Schema.String,
  connectionID: Schema.String,
  accountID: Schema.String,
  credentialRevision: Schema.Number,
  expiresAt: Schema.Number,
  renewable: Schema.Boolean,
})

let host: AgentSideConnection | undefined

type ActiveOperation = {
  binding: AccountSelection.Binding
  scope: AccountSelection.Scope
  readonly routeValue: unknown
  transientRetries: number
}

const operations = new Map<string, ActiveOperation>()

export class ManagedProviderError extends Error {}

export interface BoundOperation {
  readonly binding: AccountSelection.Binding
  readonly scope: AccountSelection.Scope
}

function decode(schema: any, value: unknown, label: string): any {
  try {
    return Schema.decodeUnknownSync(schema)(value) as any
  } catch {
    throw new ManagedProviderError(`Managed provider host returned an invalid ${label}`)
  }
}

function connection(): AgentSideConnection {
  if (!enabled()) throw new ManagedProviderError("Managed provider callbacks are not enabled")
  if (!host) throw new ManagedProviderError("Managed provider host connection is unavailable")
  return host
}

/** Install the one private ACP connection. It carries no owner selector. */
export function installHostConnection(value: AgentSideConnection): void {
  if (!enabled()) return
  if (host && host !== value) {
    throw new ManagedProviderError("Managed provider host connection is already installed")
  }
  host = value
}

export function currentScope(sessionID: string): AccountSelection.Scope | undefined {
  return operations.get(sessionID)?.scope
}

export function requireScope(sessionID: string): AccountSelection.Scope {
  const scope = currentScope(sessionID)
  if (!scope) {
    throw new ManagedProviderError("No managed provider lease is active for this operation")
  }
  return scope
}

function validateRouteContext(value: unknown, providerID: string, modelID: string): RouteContext {
  const route = decode(RouteContext, value, "route context")
  if (!route.rootOperationID || !route.connectionID || !route.providerID || !route.modelID) {
    throw new ManagedProviderError("Managed provider route context is incomplete")
  }
  // A connection ID is the preferred collision-free runtime provider ID.  An
  // official family ID remains accepted only because the host already proved
  // that the family/model pair resolved to exactly one connection.
  if (providerID !== route.connectionID && providerID !== route.providerID) {
    throw new ManagedProviderError("Managed provider route does not match the selected provider")
  }
  if (modelID !== route.modelID) {
    throw new ManagedProviderError("Managed provider route does not match the selected model")
  }
  return route
}

export async function beginBoundOperation(
  routeValue: unknown,
  model: { providerID: string; modelID: string },
): Promise<BoundOperation> {
  const route = validateRouteContext(routeValue, model.providerID, model.modelID)
  const bindRequest: AccountSelection.SelectionRequest = {
    rootOperationID: route.rootOperationID,
    connectionID: route.connectionID,
    providerID: route.providerID,
    billingLane: route.billingLane,
    modelID: route.modelID,
    ...(route.preferredAccountID ? { preferredAccountID: route.preferredAccountID } : {}),
    ...(route.inheritedAccountID ? { inheritedAccountID: route.inheritedAccountID } : {}),
    ...(route.grantID ? { grantID: route.grantID } : {}),
  }
  const rawBinding = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/bind",
    bindRequest,
  )
  const binding = decode(AccountSelection.Binding, rawBinding, "account binding")
  if (
    binding.rootOperationID !== route.rootOperationID ||
    binding.connectionID !== route.connectionID ||
    binding.billingLane !== route.billingLane ||
    binding.modelID !== route.modelID
  ) {
    throw new ManagedProviderError("Managed provider binding escaped its requested route")
  }

  return leaseBinding(route, binding)
}

export async function leaseBinding(
  routeValue: unknown,
  binding: AccountSelection.Binding,
): Promise<BoundOperation> {
  const route = validateRouteContext(routeValue, binding.providerID, binding.modelID)
  if (
    binding.rootOperationID !== route.rootOperationID ||
    binding.connectionID !== route.connectionID ||
    binding.billingLane !== route.billingLane ||
    binding.modelID !== route.modelID
  ) {
    throw new ManagedProviderError("Managed provider binding escaped its requested route")
  }
  if (!binding.credentialRequired) {
    if (
      binding.source !== "keyless" ||
      binding.accountID !== undefined ||
      binding.credentialRevision !== undefined ||
      binding.billingLane !== "local"
    ) {
      throw new ManagedProviderError("Managed keyless binding is internally inconsistent")
    }
    return {
      binding,
      scope: {
        connectionID: binding.connectionID,
        billingLane: binding.billingLane,
        credentialRequired: false,
      },
    }
  }
  if (!binding.accountID || binding.credentialRevision === undefined) {
    throw new ManagedProviderError("Managed credential binding is incomplete")
  }

  const rawLease = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/credential/lease",
    {
      rootOperationID: route.rootOperationID,
      connectionID: binding.connectionID,
      accountID: binding.accountID,
      modelID: binding.modelID,
      expectedCredentialRevision: binding.credentialRevision,
      ...(route.grantID ? { grantID: route.grantID } : {}),
    },
  )
  const lease = decode(CredentialLease, rawLease, "credential lease")
  if (
    lease.connectionID !== binding.connectionID ||
    lease.accountID !== binding.accountID ||
    lease.credentialRevision !== binding.credentialRevision ||
    lease.expiresAt <= Date.now()
  ) {
    throw new ManagedProviderError("Managed provider credential lease is stale or mismatched")
  }
  return {
    binding,
    scope: {
      connectionID: lease.connectionID,
      accountID: lease.accountID,
      billingLane: binding.billingLane,
      credentialRequired: true,
      credentialRevision: lease.credentialRevision,
      credential: lease.credential,
    },
  }
}

export async function beginOperation(
  routeValue: unknown,
  model: { providerID: string; modelID: string },
): Promise<AccountSelection.Scope> {
  return (await beginBoundOperation(routeValue, model)).scope
}

export async function commitOperation(binding: AccountSelection.Binding): Promise<AccountSelection.Binding> {
  const raw = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/commit",
    { bindingID: binding.bindingID, expectedRevision: binding.bindingRevision },
  )
  const committed = decode(AccountSelection.Binding, raw, "committed account binding")
  if (
    committed.bindingID !== binding.bindingID ||
    committed.connectionID !== binding.connectionID ||
    committed.billingLane !== binding.billingLane ||
    committed.modelID !== binding.modelID ||
    committed.accountID !== binding.accountID ||
    committed.committed !== true
  ) {
    throw new ManagedProviderError("Managed provider commitment escaped its account binding")
  }
  return committed
}

export async function recordAttempt(
  binding: AccountSelection.Binding,
  outcome: AccountSelection.AttemptOutcome,
  options: { retryAfterMs?: number; modelEligible?: boolean } = {},
): Promise<AccountSelection.Binding> {
  const raw = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/account/attempt",
    {
      bindingID: binding.bindingID,
      expectedRevision: binding.bindingRevision,
      ...(binding.accountID ? { accountID: binding.accountID } : {}),
      outcome,
      ...(options.retryAfterMs !== undefined ? { retryAfterMs: options.retryAfterMs } : {}),
      ...(options.modelEligible !== undefined ? { modelEligible: options.modelEligible } : {}),
    },
  )
  const next = decode(AccountSelection.Binding, raw, "account attempt result")
  if (
    next.bindingID !== binding.bindingID ||
    next.connectionID !== binding.connectionID ||
    next.billingLane !== binding.billingLane ||
    next.modelID !== binding.modelID
  ) {
    throw new ManagedProviderError("Managed provider attempt escaped its account binding")
  }
  return next
}

/** Keep leased credential material only for the duration of one server turn. */
export async function withOperation<A>(
  sessionID: string,
  routeValue: unknown,
  model: { providerID: string; modelID: string },
  run: () => Promise<A>,
): Promise<A> {
  if (!enabled()) return run()
  if (operations.has(sessionID)) {
    throw new ManagedProviderError("A managed provider operation is already active for this session")
  }
  const bound = await beginBoundOperation(routeValue, model)
  const operation: ActiveOperation = {
    ...bound,
    routeValue,
    transientRetries: 0,
  }
  operations.set(sessionID, operation)
  try {
    return await run()
  } finally {
    if (operations.get(sessionID) === operation) operations.delete(sessionID)
  }
}

function requireOperation(sessionID: string): ActiveOperation {
  const operation = operations.get(sessionID)
  if (!operation) throw new ManagedProviderError("No managed provider operation is active")
  return operation
}

function errorStatus(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined
  const value = error as any
  const raw = value.status ?? value.statusCode ?? value.response?.status ?? value.data?.statusCode
  const parsed = typeof raw === "string" ? Number.parseInt(raw, 10) : raw
  return typeof parsed === "number" && Number.isFinite(parsed) ? parsed : undefined
}

function errorDetail(error: unknown): string {
  if (!error || typeof error !== "object") return String(error ?? "").toLowerCase()
  const value = error as any
  return [value.message, value.responseBody, value.data?.message, value.data?.responseBody]
    .filter((item) => typeof item === "string")
    .join(" ")
    .toLowerCase()
}

function retryAfterMs(error: unknown): number | undefined {
  if (!error || typeof error !== "object") return undefined
  const value = error as any
  const headers = value.response?.headers ?? value.responseHeaders ?? value.data?.responseHeaders
  if (!headers || typeof headers !== "object") return undefined
  const direct = headers["retry-after-ms"]
  if (direct !== undefined) {
    const parsed = Number.parseFloat(String(direct))
    if (Number.isFinite(parsed) && parsed >= 0) return Math.ceil(parsed)
  }
  const raw = headers["retry-after"]
  if (raw === undefined) return undefined
  const seconds = Number.parseFloat(String(raw))
  if (Number.isFinite(seconds) && seconds >= 0) return Math.ceil(seconds * 1000)
  const date = Date.parse(String(raw))
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined
}

function classifyAttempt(error: unknown): {
  outcome: AccountSelection.AttemptOutcome
  retryAfterMs?: number
  modelEligible?: boolean
} {
  const status = errorStatus(error)
  const detail = errorDetail(error)
  const code =
    error && typeof error === "object" && typeof (error as any).code === "string"
      ? String((error as any).code).toUpperCase()
      : ""
  if (status === 401 || detail.includes("invalid authentication") || detail.includes("invalid_api_key")) {
    return { outcome: "auth" }
  }
  if (
    status === 429 ||
    detail.includes("rate limit") ||
    detail.includes("rate_limit") ||
    detail.includes("insufficient_quota") ||
    detail.includes("quota exceeded") ||
    detail.includes("out of credits")
  ) {
    const retry = retryAfterMs(error)
    return { outcome: "quota", ...(retry === undefined ? {} : { retryAfterMs: retry }) }
  }
  if (status === 403 || status === 404) {
    return { outcome: "entitlement", modelEligible: false }
  }
  if (
    (status !== undefined && status >= 500) ||
    ["ECONNRESET", "EPIPE", "ETIMEDOUT"].includes(code) ||
    detail.includes("sse read timed out") ||
    detail.includes("network error")
  ) {
    return { outcome: "transient" }
  }
  return { outcome: "unknown" }
}

export type AttemptDecision = {
  outcome: AccountSelection.AttemptOutcome
  retry: boolean
  rotated: boolean
  retryAfterMs?: number
}

export async function commitSessionOperation(sessionID: string): Promise<void> {
  if (!enabled()) return
  const operation = requireOperation(sessionID)
  if (operation.binding.committed) return
  const binding = await commitOperation(operation.binding)
  operation.binding = binding
}

async function recordSessionOutcome(
  sessionID: string,
  classification: {
    outcome: AccountSelection.AttemptOutcome
    retryAfterMs?: number
    modelEligible?: boolean
  },
): Promise<AttemptDecision> {
  const operation = requireOperation(sessionID)
  const previous = operation.binding
  const next = await recordAttempt(previous, classification.outcome, classification)
  const rotated =
    previous.accountID !== next.accountID || previous.credentialRevision !== next.credentialRevision
  if (rotated) {
    const leased = await leaseBinding(operation.routeValue, next)
    operation.binding = leased.binding
    operation.scope = leased.scope
    operation.transientRetries = 0
  } else {
    operation.binding = next
  }
  if (classification.outcome === "transient" && !next.committed && !rotated) {
    operation.transientRetries += 1
  }
  const retry =
    !next.committed &&
    ((rotated && ["auth", "quota", "entitlement"].includes(classification.outcome)) ||
      (classification.outcome === "transient" && operation.transientRetries <= 2))
  return {
    outcome: classification.outcome,
    retry,
    rotated,
    ...(classification.retryAfterMs === undefined ? {} : { retryAfterMs: classification.retryAfterMs }),
  }
}

export async function recordSessionAttempt(sessionID: string, error: unknown): Promise<AttemptDecision> {
  if (!enabled()) return { outcome: "unknown", retry: false, rotated: false }
  return recordSessionOutcome(sessionID, classifyAttempt(error))
}

export async function recordSessionSuccess(sessionID: string): Promise<void> {
  if (!enabled()) return
  await recordSessionOutcome(sessionID, { outcome: "success" })
}

export async function replaceCredential(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Info
}): Promise<Auth.Account> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/credential/replace",
    input,
  )
  return decode(Auth.Account, result, "credential replacement")
}

export async function acquireRefresh(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  ttlMs?: number
}): Promise<Auth.RefreshLease> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/refresh/acquire",
    input,
  )
  return decode(RefreshLease, result, "refresh lease")
}

export async function renewRefresh(input: { leaseID: string; ttlMs?: number }): Promise<Auth.RefreshLease> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/refresh/renew",
    input,
  )
  return decode(RefreshLease, result, "renewed refresh lease")
}

export async function commitRefresh(input: {
  leaseID: string
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Info
}): Promise<Auth.Account> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/refresh/commit",
    input,
  )
  const account = decode(Auth.Account, result, "refresh commit") as Auth.Account
  if (
    account.id !== input.accountID ||
    account.credentialRevision !== input.expectedRevision + 1
  ) {
    throw new ManagedProviderError("Managed provider refresh commit escaped its credential CAS")
  }
  // Refresh happens during an active attempt.  Keep every in-process scope
  // for this exact account/revision aligned with the host's atomic binding
  // advance; unrelated connections/accounts and already-newer scopes are
  // untouched.
  for (const operation of operations.values()) {
    if (
      operation.binding.connectionID !== input.connectionID ||
      operation.binding.accountID !== input.accountID ||
      operation.binding.credentialRevision !== input.expectedRevision
    ) {
      continue
    }
    operation.binding = {
      ...operation.binding,
      credentialRevision: account.credentialRevision,
    }
    operation.scope = {
      connectionID: input.connectionID,
      accountID: input.accountID,
      billingLane: operation.binding.billingLane,
      credentialRequired: true,
      credentialRevision: account.credentialRevision,
      credential: account.credential,
    }
  }
  return account
}

export async function abortRefresh(input: { leaseID: string }): Promise<void> {
  const result = await OpenClankManagedProtocol.callHost(
    connection(),
    "_openclank/provider-store/v1/refresh/abort",
    input,
  )
  if (typeof result !== "object" || result === null || Object.keys(result).length !== 0) {
    throw new ManagedProviderError("Managed provider host returned an invalid refresh abort")
  }
}

export class ManagedRefreshPersistenceError extends Error {
  readonly status = 401
  readonly statusCode = 401
  readonly code = "OPENCLANK_REFRESH_PERSISTENCE_FAILED"
}

/**
 * Run one OAuth refresh under the host's cross-process lease and credential
 * revision CAS.  The provider-specific token exchange is injected so this
 * authority remains independent of any adapter.  A token that rotated
 * upstream but could not be durably committed is surfaced as an auth failure;
 * the normal pre-commit attempt path then marks the account reauth-required
 * and may select another account without ever resurrecting the stale token.
 */
export async function refreshOAuthCredential(input: {
  connectionID: string
  accountID: string
  expectedRevision: number
  credential: Auth.Oauth
  exchange: (credential: Auth.Oauth) => Promise<Auth.Oauth>
}): Promise<{ credential: Auth.Oauth; credentialRevision: number }> {
  const lease = await acquireRefresh({
    connectionID: input.connectionID,
    accountID: input.accountID,
    expectedRevision: input.expectedRevision,
  })
  let exchanged = false
  try {
    const credential = await input.exchange(input.credential)
    exchanged = true
    if (
      credential?.type !== "oauth" ||
      !credential.access ||
      !credential.refresh ||
      !Number.isFinite(credential.expires)
    ) {
      throw new ManagedProviderError("Provider returned an invalid OAuth refresh result")
    }
    const account = await commitRefresh({
      leaseID: lease.leaseID,
      connectionID: input.connectionID,
      accountID: input.accountID,
      expectedRevision: input.expectedRevision,
      credential,
    })
    if (account.credential.type !== "oauth") {
      throw new ManagedRefreshPersistenceError(
        "Refreshed provider credential changed authentication class",
      )
    }
    return {
      credential: account.credential,
      credentialRevision: account.credentialRevision,
    }
  } catch (error) {
    try {
      await abortRefresh({ leaseID: lease.leaseID })
    } catch {}
    if (exchanged) {
      if (error instanceof ManagedRefreshPersistenceError) throw error
      throw new ManagedRefreshPersistenceError(
        "Provider token rotated but durable credential commit failed",
      )
    }
    throw error
  }
}

/** Test-only process-state reset; production never swaps a live ACP host. */
export function resetForTest(): void {
  host = undefined
  operations.clear()
}

export * as ManagedProvider from "./managed-provider"
