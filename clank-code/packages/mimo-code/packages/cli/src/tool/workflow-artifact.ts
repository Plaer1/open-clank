import type { ActorExecutionIdentity } from "@/actor/turn"
import { createHash } from "node:crypto"
import { Effect } from "effect"
import z from "zod"
import { MessageV2 } from "@/session/message-v2"
import { isAdmittedToolTimingTurn } from "@/session/tool-timing"
import { claimWorkflowWriter, readWorkflowSource, recordWorkflowCheckpointCommit, recordWorkflowTaskCommit } from "@/session/workflow-source"
import { managedSessionBinding, markerMatches } from "@/memory/session-scope"
import { Flag } from "@/flag/flag"
import { Config } from "@/config"
import { isMemoryWriteEnabled } from "@/memory/write-gate"
import * as CheckpointContext from "@/session/checkpoint-context"
import { validateSnapshot, validateLearning, validateBudget, validateBudgetSections } from "@/session/checkpoint-validator"
import { CHECKPOINT_SECTION_BUDGETS } from "@/session/checkpoint-templates"
import type { SessionID } from "@/session/schema"
import * as Tool from "./tool"

const role = z.enum(["metaplan", "ledger", "concern", "checkpoint", "progress", "slice"])
const revision = z.object({ kind: z.literal("hostFingerprint"), value: z.string().max(100).regex(/^sha256:[a-f0-9]{64}:(0|[1-9][0-9]*)$/) }).strict()
const parameters = z.discriminatedUnion("operation", [
  z.object({ operation: z.literal("read"), role, name: z.string().nullable() }).strict(),
  z.object({ operation: z.literal("write"), role, name: z.string().nullable(), expectedRevision: revision.nullable(), text: z.string() }).strict(),
  z.object({ operation: z.literal("status"), role, name: z.string().nullable(), operationID: z.string().regex(/^workflowop_[a-f0-9]{64}$/) }).strict(),
])
export const WorkflowArtifactTool = Tool.define("workflow_artifact", Effect.gen(function* () {
  const config = yield* Config.Service
  return {
  description: "Read, write or check an admitted operational plan or notes through Files and History. For an initial plan draft, the admitted main actor may write role metaplan, name null, expectedRevision null, and the complete plan text; this creates an unapproved draft. Read an existing role before writing with its returned revision. Roles concern and slice require a lower-kebab name; metaplan, ledger, checkpoint and progress require null. Progress is limited to the actual assigned task. If a write outcome is unknown, use status with its original operationID; never resubmit under a new ID. A draft never approves implementation.",
  parameters,
  execute: (args: z.infer<typeof parameters>, ctx) => Effect.gen(function* () {
    const unavailable = (operationID = "") => ({ title: "Workflow artifact unavailable", output: JSON.stringify({ state: "unavailable", operationID }), metadata: { state: "unavailable", operationID } })
    const actor = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
    if (Flag.OPEN_CLANK_PRIVATE_RUNTIME || !actor || actor.sessionID !== ctx.sessionID || !actor.rootOperationID || !actor.innerTurnID ||
        !Number.isSafeInteger(actor.executionRevision) || actor.executionRevision < 0 || !ctx.callID || !ctx.messageID || !ctx.abort || ctx.abort.aborted) return unavailable()
    if ((args.role === "concern" || args.role === "slice") ? typeof args.name !== "string" || args.name.length > 60 || !/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(args.name) : args.name !== null) return unavailable()
    if (args.operation === "write" && (Buffer.byteLength(args.text, "utf8") > 262144 || args.text.includes("\0") || !args.text.isWellFormed())) return unavailable()
    if (args.operation === "write" && !isMemoryWriteEnabled(yield* config.get())) return unavailable()
    const managed = yield* Effect.promise(() => import("@/acp/managed-provider"))
    if (!managed.enabled()) return unavailable()
    const writer = claimWorkflowWriter(actor)
    if (!writer) return unavailable()
    const sourceSessionID = writer.source.sessionID
    if (args.operation === "write" && args.role === "checkpoint") {
      const context = CheckpointContext.get(sourceSessionID as SessionID, actor.actorID)
      if (!context || context.expectedCheckpointRevision === undefined ||
          (args.expectedRevision === null ? context.expectedCheckpointRevision !== null :
            !context.expectedCheckpointRevision || args.expectedRevision.kind !== context.expectedCheckpointRevision.kind ||
            args.expectedRevision.value !== context.expectedCheckpointRevision.value)) return unavailable()
      const violations = [
        ...validateSnapshot(args.text, "checkpoint"),
        ...validateLearning(args.text, "checkpoint", context.priorTitles),
        ...validateBudget(args.text, 11_000, "checkpoint"),
        ...validateBudgetSections(args.text, CHECKPOINT_SECTION_BUDGETS, "checkpoint"),
      ].filter((item) => item.severity === "error" || item.severity === "extract-required")
      if (violations.length) return { title: "Checkpoint requires revision", output: JSON.stringify({ state: "invalid", violations }), metadata: { state: "invalid" } }
    }
    const toolCallID = ctx.callID
    const binding = managedSessionBinding(sourceSessionID)
    const host = managed.ManagedProvider.currentActorHostContext(ctx.sessionID, actor.actorID, actor.executionRevision)
    if (!binding || !markerMatches(sourceSessionID, binding) || !binding.memoryEnabled || binding.authority || binding.transition || !host?.ownerAccountID) return unavailable()
    const identity = { sessionID:ctx.sessionID, rootOperationID:actor.rootOperationID, actorID:actor.actorID, executionRevision:actor.executionRevision, innerTurnID:actor.innerTurnID }
    const current = () => {
      const now = managedSessionBinding(sourceSessionID)
      const h = managed.ManagedProvider.currentActorHostContext(ctx.sessionID, actor.actorID, actor.executionRevision)
      return !ctx.abort.aborted && !!now && markerMatches(sourceSessionID,now) && now.registrationMarker.token===binding.registrationMarker.token &&
        now.memoryEnabled && !now.authority && !now.transition && now.owner===binding.owner && now.stableChatID===binding.stableChatID &&
        now.bindingRevision===binding.bindingRevision && now.mappingRevision===binding.mappingRevision && now.mapRevision===binding.mapRevision &&
        now.authorityWorkspaceID===binding.authorityWorkspaceID && !!h && h.ownerAccountID===host.ownerAccountID && h.chatID===host.chatID && h.workspaceID===host.workspaceID &&
        ctx.extra?.actorExecution===actor && isAdmittedToolTimingTurn(actor,identity) && !!readWorkflowSource(writer.sourceToken)
    }
    if (!current()) return unavailable()
    if (!(yield* Effect.promise(() => managed.ManagedProvider.claimFreshTaskCall({ ...identity, messageID:ctx.messageID, toolCallID, toolName:"workflow_artifact" }))) || !current()) return unavailable()
    const digest = (value:unknown) => createHash("sha256").update(JSON.stringify(value),"utf8").digest("hex")
    const operationID = args.operation === "write" ? "workflowop_" + digest(["openclank.workflow.operation.v1",host.ownerAccountID,ctx.sessionID,
      actor.rootOperationID,actor.actorID,actor.executionRevision,actor.innerTurnID,ctx.callID]) : args.operation === "status" ? args.operationID : undefined
    const argsSHA256 = digest(["openclank.workflow-artifact.v1",args.operation,args.role,args.name,
      args.operation === "write" ? args.expectedRevision : null,args.operation === "write" ? args.text : operationID ?? null])
    if (args.operation === "write") {
      yield* ctx.metadata({metadata:{workflowArtifactOperationID:operationID}})
      const persisted = yield* Effect.sync(() => {
        try { const parts = MessageV2.parts(ctx.messageID).filter((p) => p.type === "tool" && p.callID === ctx.callID); const p = parts.length === 1 ? parts[0] : undefined
          return !!p && p.type === "tool" && p.sessionID === ctx.sessionID && p.tool === "workflow_artifact" && p.execution === "started" && p.state.status === "running" && p.state.metadata?.workflowArtifactOperationID === operationID && JSON.stringify(p.state.input) === JSON.stringify(args)
        } catch { return false }
      })
      if (!persisted) return unavailable(operationID)
    }
    if (!current()) return unavailable(operationID)
    const base = {...identity,sourceToken:writer.sourceToken,messageID:ctx.messageID,toolCallID:ctx.callID,argsSHA256,role:args.role,name:args.name}
    const result = yield* Effect.uninterruptible(Effect.gen(function* () {
      if (args.operation === "write" && !isMemoryWriteEnabled(yield* config.get())) return {state:"unavailable" as const,operationID}
      return yield* Effect.promise(async () => {
      if (!current()) return {state:"unavailable" as const,operationID}
      try {
        if (args.operation === "read") return await managed.managedHostCall("_openclank/session/v1/workflow/artifact/read",base)
        if (args.operation === "write") return await managed.managedHostCall("_openclank/session/v1/workflow/artifact", {...base,operationID:operationID!,expectedRevision:args.expectedRevision,text:args.text})
        return await managed.managedHostCall("_openclank/session/v1/workflow/artifact/status",{...base,operationID:args.operationID})
      } catch { return {state:args.operation === "write" ? "unknown" as const : "unavailable" as const,operationID} }
      })
    }))
    if (args.operation === "write" && (args.role === "checkpoint" || args.role === "progress") && args.name === null && operationID && current() &&
        result && result.state === "complete" && "operationID" in result && result.operationID === operationID &&
        "resourceRef" in result && typeof result.resourceRef === "string" && result.resourceRef.length > 0 && result.resourceRef.isWellFormed() &&
        Buffer.byteLength(result.resourceRef,"utf8") <= 16384 && !/[\/\\\0]/.test(result.resourceRef) &&
        "revision" in result && result.revision?.kind === "hostFingerprint" && typeof result.revision.value === "string" &&
        result.revision.value.length <= 100 && /^sha256:[a-f0-9]{64}:(0|[1-9][0-9]*)$/.test(result.revision.value) &&
        Object.keys(result).sort().join(",") === "operationID,resourceRef,revision,state" &&
        Object.keys(result.revision).sort().join(",") === "kind,value") {
      if (args.role === "checkpoint") recordWorkflowCheckpointCommit(actor,writer.sourceToken,operationID)
      else if (["## §1 Task identity","## §2 Subagent intent","## §3 Files and code sections","## §4 Verbatim commands","## §5 Outcome and discoveries"].every((heading) => args.text.includes(heading)))
        recordWorkflowTaskCommit(actor,writer.sourceToken,operationID)
    }
    const outcome = current() ? result : {state:args.operation === "write" ? "unknown" as const : "unavailable" as const,operationID}
    return {title:"Workflow artifact",output:JSON.stringify(outcome),metadata:{...outcome,workflowArtifactOperationID:operationID}}
  }),
  }
}))
