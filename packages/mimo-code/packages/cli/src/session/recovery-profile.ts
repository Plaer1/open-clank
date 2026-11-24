/** Process-private evidence only. It does not select or change Snapshot behavior. */
import type { Effect } from "effect"

export type ToolWitness = Readonly<{
  factory: object
  init: Function
  initialized: Function
  effective: Function
}>
export type HookWitness = Readonly<{
  plugins: string
  fileHooks: string
  handlers: readonly Function[]
  noUnclassifiedHandlers: boolean
  current: () => Effect.Effect<boolean>
}>
export type FormatterWitness = Readonly<{
  explicitlyDisabled: boolean
  current: () => Effect.Effect<boolean>
}>
export type EffectOwnerWitness = Readonly<{
  eligible: boolean
  current: () => Effect.Effect<boolean>
}>
export type GlobalEventWitness = Readonly<{
  eligible: boolean
  current: () => boolean
}>
export type ResolvedToolWitness = Readonly<{
  id: string
  tool: ToolWitness | undefined
  dispatch: Function | undefined
}>
export type RecoveryEvidence = Readonly<{
  state: "lore_unavailable"
  reasons: readonly string[]
  tools: readonly ResolvedToolWitness[]
  hooks: HookWitness | undefined
  formatter: FormatterWitness | undefined
  registryGeneration: string | undefined
  registryCurrent: (() => Effect.Effect<boolean>) | undefined
  binding: Readonly<{sessionID:string; actorID:string; executionRevision:number; rootOperationID:string;
    innerTurnID:string; accountID:string; chatID:string; workspaceID:string; directory:string}> | undefined
  bindingCurrent: (() => boolean) | undefined
  toolsCurrent: () => boolean
}>

/** No upfront durable History capture admission exists yet. Never infer one
 * from environment fields or from a caller-provided flag. */
export function evidence(input: Omit<RecoveryEvidence, "state" | "reasons">): RecoveryEvidence {
  const reasons = ["capture_admission_unavailable"]
  if (!input.tools.length || input.tools.some((item) => !item.tool || !item.dispatch)) reasons.push("unclassified_dispatch")
  if (!input.hooks?.noUnclassifiedHandlers) reasons.push("unclassified_hook")
  if (!input.formatter?.explicitlyDisabled) reasons.push("formatter_unavailable")
  if (!input.registryGeneration || !input.registryCurrent) reasons.push("registry_unavailable")
  if (!input.binding || !input.bindingCurrent) reasons.push("binding_unavailable")
  return Object.freeze({ state: "lore_unavailable", reasons: Object.freeze(reasons),
    tools: Object.freeze([...input.tools]), hooks: input.hooks, formatter: input.formatter,
    registryGeneration: input.registryGeneration, registryCurrent: input.registryCurrent, binding: input.binding,
    bindingCurrent: input.bindingCurrent, toolsCurrent: input.toolsCurrent })
}
