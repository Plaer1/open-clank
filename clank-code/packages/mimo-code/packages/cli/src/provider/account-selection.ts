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

export const MethodPolicy = Schema.Struct({
  version: Schema.Literal(1),
  methodID: Schema.Literal("anthropic.claude_subscription"),
  credentialOwner: Schema.String,
  policyRevision: Schema.Number,
})

export const ActorOptions = Schema.Struct({
  version: Schema.Literal(1), routeID: Schema.String, optionRouteID: Schema.String, catalogRevision: Schema.Number,
  model: Schema.Struct({ providerID: Schema.String, modelID: Schema.String }),
  options: Schema.Struct({ reasoning_effort: Schema.optional(Schema.String) }),
  source: Schema.Struct({ kind: Schema.Literals(["parent_execution", "default", "receipt"]), requestID: Schema.optional(Schema.String), sessionID: Schema.optional(Schema.String), actorID: Schema.optional(Schema.String), executionRevision: Schema.optional(Schema.Number), rootOperationID: Schema.optional(Schema.String), messageID: Schema.optional(Schema.String) }),
})

const BootstrapCommon = {
  stableChatID: Schema.String, owner: Schema.String, rootSessionID: Schema.String,
  sessionID: Schema.String, actorID: Schema.String, rootOperationID: Schema.String,
  expectedExecutionRevision: Schema.Number, modelRouteID: Schema.String,
  handoffID: Schema.optional(Schema.String), modelRequestOptions: Schema.optional(ActorOptions),
}
export class SelectionRequest extends Schema.Class<SelectionRequest>("ProviderAccountSelectionRequest")({
  rootOperationID: Schema.String,
  modelRequestOptions: Schema.optional(ActorOptions),
  methodPolicy: Schema.optional(MethodPolicy),
  bootstrap: Schema.optional(Schema.Union([
    Schema.Struct({ ...BootstrapCommon, mapRevision: Schema.Number, mappingRevision: Schema.Number, workspaceRevision: Schema.Number }),
    Schema.Struct({ ...BootstrapCommon, authority: Schema.Struct({ kind: Schema.Literal("private"), generation: Schema.String }), workspaceRevision: Schema.optional(Schema.Number) }),
  ])),
  scopeKind: Schema.Literals(["actor_execution", "auxiliary_operation"]),
  operationID: Schema.optional(Schema.String),
  sessionID: Schema.optional(Schema.String),
  actorID: Schema.optional(Schema.String),
  executionRevision: Schema.optional(Schema.Number),
  modelRouteID: Schema.optional(Schema.String),
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
  pendingCrossModel: Schema.optional(Schema.Struct({version:Schema.Literal(1),coordinatorRevision:Schema.Literal(1)})),
  crossModelAttempt: Schema.optional(Schema.Struct({version:Schema.Literal(1),attemptID:Schema.String,attemptGeneration:Schema.Literal(1),coordinatorRevision:Schema.Number,accounting:Schema.optional(Schema.Literal('unavailable'))})),
  methodPolicy: Schema.optional(MethodPolicy),
  bindingID: Schema.String,
  bindingRevision: Schema.Number,
  rootOperationID: Schema.String,
  scopeKind: Schema.Literals(["actor_execution", "auxiliary_operation"]),
  operationID: Schema.optional(Schema.String),
  sessionID: Schema.optional(Schema.String),
  actorID: Schema.optional(Schema.String),
  executionRevision: Schema.optional(Schema.Number),
  modelRouteID: Schema.optional(Schema.String),
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

export const AttemptOutcome = Schema.Literals(["success", "invalid_output", "auth", "quota", "entitlement", "transient", "unknown"])
export type AttemptOutcome = Schema.Schema.Type<typeof AttemptOutcome>

export type DispatchObservation = { version: 1; profile: "anthropic.messages.v1" | "openai.codex.responses.v1"; captureDispatchID: string; upstreamDispatchID: string; httpStatus: number }

export type AttemptEvidence = {
  version: 1
  kind: "throttle" | "exhausted" | "auth" | "entitlement" | "transient" | "malformed_success" | "unknown"
  httpStatus?: number
  providerReset?: { at: string; source: string; observedAt: string; dispatchID: string }
  codexWindows?: { source: "openai.codex.subscription.headers.v1"; observedAt: string; dispatchID: string; windows: { window: "primary" | "secondary"; usedPercent: number; windowMinutes?: number; resetAt?: string }[] }
  localNotBefore?: string
}

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
    evidence?: AttemptEvidence
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
