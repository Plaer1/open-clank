import { Effect } from "effect"
import z from "zod"
import * as Tool from "./tool"
import { ManagedProvider } from "@/acp/managed-provider"
import type { ActorExecutionIdentity } from "@/actor/turn"

const parameters = z.discriminatedUnion("action", [
  z.object({ action: z.literal("list") }).strict(),
  z.object({ action: z.literal("status") }).strict(),
  z.object({ action: z.literal("add_directory"), directory: z.string().min(1).max(4096) }).strict(),
  z.object({ action: z.literal("remove_directory"), directory: z.string().min(1).max(4096) }).strict(),
  z.object({ action: z.literal("rebuild_embeddings") }).strict(),
  z.object({ action: z.literal("rollback_embedding"), generation_id: z.string().min(1).max(256) }).strict(),
])

export const ManageRagTool = Tool.define("manage_rag", Effect.succeed({
  description: "Administer the current workspace's Memery document index. The host checks your identity, file access and approval before applying changes.",
  parameters,
  execute: (args: z.infer<typeof parameters>, ctx: Tool.Context) => Effect.gen(function* () {
    const actor = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
    if (!ManagedProvider.enabled() || !actor || actor.actorID !== "main" || actor.sessionID !== ctx.sessionID ||
        !actor.rootOperationID || !actor.innerTurnID || !ctx.messageID || !ctx.callID || ctx.abort.aborted)
      throw new Error("Document administration requires an active managed turn")
    const identity = {sessionID: actor.sessionID, actorID: actor.actorID, executionRevision: actor.executionRevision,
      rootOperationID: actor.rootOperationID, innerTurnID: actor.innerTurnID}
    const call = {messageID: ctx.messageID, toolCallID: ctx.callID}
    const fresh = yield* Effect.promise(() => ManagedProvider.claimFreshTaskCall({...identity, ...call, toolName:"manage_rag"}))
    if (!fresh || ctx.abort.aborted) throw new Error("Document administration call is stale")
    const result = yield* Effect.promise(() => ManagedProvider.managedHostCall("_openclank/session/v1/memory/manage-rag", {identity, ...call, args}))
    return {title:"Memery document index", output:result.output, metadata:{state:result.state}}
  }),
}))
