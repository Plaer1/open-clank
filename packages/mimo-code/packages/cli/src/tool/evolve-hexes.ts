import { createHash } from "node:crypto"
import { Effect } from "effect"
import z from "zod"
import * as Tool from "./tool"
import { ManagedProvider } from "@/acp/managed-provider"
import { currentInputOrigin } from "@/session/input-origin"
import type { ActorExecutionIdentity } from "@/actor/turn"

const identifier = z.string().min(1).max(256)
const candidate = {
  projectID: identifier,
  modeRevision: z.number().int().nonnegative(),
  policyRevision: z.number().int().nonnegative(),
  expectedContractHash: z.string().regex(/^[0-9a-f]{64}$/),
  candidateText: z.string().min(1).max(262144),
  reason: z.string().min(1).max(4000),
}
const parameters = z.discriminatedUnion("operation", [
  z.object({operation:z.literal("preview"),...candidate}).strict(),
  z.object({operation:z.literal("apply"),...candidate}).strict(),
  z.object({operation:z.literal("reconcile"),projectID:identifier,operationID:identifier}).strict(),
  z.object({operation:z.literal("rollback"),projectID:identifier,operationID:identifier}).strict(),
  z.object({operation:z.literal("rollback-reconcile"),projectID:identifier,operationID:identifier}).strict(),
])

export const EvolveHexesTool = Tool.define("evolve_hexes", Effect.succeed({
  description: "Preview or propose a project Hex contract change, or reconcile or roll back an earlier operation. Use the current project, mode and policy revisions; explain the proposal. The host decides whether the operation is available and records its outcome.",
  parameters,
  execute: (args:z.infer<typeof parameters>, ctx:Tool.Context) => Effect.gen(function* () {
    const actor=ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
    const origin=currentInputOrigin(actor)
    if (!ManagedProvider.enabled() || !actor || !origin || !ctx.callID || !actor.rootOperationID || !actor.innerTurnID || ctx.abort.aborted)
      throw new Error("Project Hex evolution is unavailable for this turn")
    const identity={sessionID:actor.sessionID,rootOperationID:actor.rootOperationID,actorID:actor.actorID,
      executionRevision:actor.executionRevision,innerTurnID:actor.innerTurnID}
    if (identity.sessionID!==ctx.sessionID) throw new Error("Project Hex evolution scope changed")
    const common={...identity,toolCallID:ctx.callID,...origin}
    const request = args.operation === "preview" || args.operation === "apply"
      ? {...common,...args,candidateSHA256:createHash("sha256").update(args.candidateText,"utf8").digest("hex"),trigger:{kind:"agent_proposal" as const}}
      : {...common,...args}
    // Once issued, drain the Host operation so the enclosing tool owner can
    // finalize its actual result after hooks rather than report a false cancel.
    const result=yield* Effect.tryPromise({
      try:() => ManagedProvider.managedHostCall("_openclank/session/v1/evolve/operate",request),
      catch:() => new Error("Project Hex evolution outcome is unavailable"),
    }).pipe(Effect.uninterruptible,Effect.orDie)
    return {title:"Project Hex evolution",output:JSON.stringify(result),metadata:{state:result.state}}
  }),
}))
