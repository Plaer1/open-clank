import { Context, Effect, Schema } from "effect"
import { Auth } from "@/auth"

/**
 * Credential-bearing execution scope returned by the durable account binder.
 * The credential is intentionally optional: managed workers normally receive
 * it through a short-lived lease only at dispatch time.
 */
export class Scope extends Schema.Class<Scope>("ProviderAccountScope")({
  connectionID: Schema.String,
  billingLane: Auth.BillingLane,
  credentialRequired: Schema.Boolean,
  accountID: Schema.optional(Schema.String),
  credentialRevision: Schema.optional(Schema.Number),
  credential: Schema.optional(Auth.Info),
}) {}

export class SelectionRequest extends Schema.Class<SelectionRequest>("ProviderAccountSelectionRequest")({
  rootOperationID: Schema.String,
  connectionID: Schema.String,
  providerID: Schema.String,
  billingLane: Auth.BillingLane,
  modelID: Schema.String,
  preferredAccountID: Schema.optional(Schema.String),
  inheritedAccountID: Schema.optional(Schema.String),
  grantID: Schema.optional(Schema.String),
}) {}

export const SelectionSource = Schema.Literals(["round_robin", "preferred", "inherited", "failover", "keyless"])
export type SelectionSource = Schema.Schema.Type<typeof SelectionSource>

export class Binding extends Schema.Class<Binding>("ProviderAccountBinding")({
  bindingID: Schema.String,
  bindingRevision: Schema.Number,
  rootOperationID: Schema.String,
  connectionID: Schema.String,
  providerID: Schema.String,
  billingLane: Auth.BillingLane,
  modelID: Schema.String,
  credentialRequired: Schema.Boolean,
  accountID: Schema.optional(Schema.String),
  credentialRevision: Schema.optional(Schema.Number),
  source: SelectionSource,
  attempt: Schema.Number,
  committed: Schema.Boolean,
}) {}

export const AttemptOutcome = Schema.Literals(["success", "auth", "quota", "entitlement", "transient", "unknown"])
export type AttemptOutcome = Schema.Schema.Type<typeof AttemptOutcome>

export class SelectionError extends Schema.TaggedErrorClass<SelectionError>()("ProviderAccountSelectionError", {
  code: Schema.Literals(["no_eligible_account", "binding_conflict", "committed", "grant_revoked"]),
  message: Schema.String,
}) {}

export interface Interface {
  /** Atomically get-or-create the sticky binding for this root operation. */
  readonly bind: (input: SelectionRequest) => Effect.Effect<Binding, SelectionError>
  /** Fence commitment by binding revision before any visible output or side effect. */
  readonly commit: (input: { bindingID: string; expectedRevision: number }) => Effect.Effect<Binding, SelectionError>
  /**
   * Persist an attempt outcome and, only for pre-commit auth/quota/entitlement
   * outcomes, optionally return a new account binding inside the same lane.
   */
  readonly recordAttempt: (input: {
    bindingID: string
    expectedRevision: number
    accountID: string
    outcome: AttemptOutcome
    retryAfterMs?: number
    modelEligible?: boolean
  }) => Effect.Effect<Binding, SelectionError>
}

/** Implemented by the Open Clank host repository in managed mode. */
export class Service extends Context.Service<Service, Interface>()("@opencode/ProviderAccountSelection") {}

export function cacheIdentity(
  scope?: Pick<Scope, "connectionID" | "credentialRequired" | "accountID" | "credentialRevision">,
): string {
  if (!scope) return "legacy"
  if (!scope.credentialRequired) return `${scope.connectionID}/keyless`
  return `${scope.connectionID}/${scope.accountID}@${scope.credentialRevision}`
}

export function languageCacheKey(
  model: { providerID: string; id: string },
  scope?: Pick<Scope, "connectionID" | "credentialRequired" | "accountID" | "credentialRevision">,
): string {
  return `${model.providerID}/${model.id}#${cacheIdentity(scope)}`
}

export * as AccountSelection from "./account-selection"
