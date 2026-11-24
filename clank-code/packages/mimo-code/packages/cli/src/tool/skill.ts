import type { ActorExecutionIdentity } from "@/actor/turn"
import z from "zod"
import { createHash } from "node:crypto"
import { Effect } from "effect"
import { Agent } from "../agent/agent"
import { Ripgrep } from "../file/ripgrep"
import { Skill, type InvocationDenial, type InvocationMode } from "../skill"
import { BuiltinWorkflow } from "../workflow/builtin"
import * as Tool from "./tool"
import { renderSkillContent } from "./skill-content"
import { previewToolOutput } from "./preview"
import DESCRIPTION from "./skill.txt"

const Parameters = z.object({
  name: z.string().describe("The name of the skill from available_skills"),
  domain: z.object({block_id:z.string().min(1).refine(value=>value===value.trim() && Buffer.byteLength(value,"utf8")<=512 && Buffer.from(value,"utf8").toString("utf8")===value),revision:z.number().int().positive().safe()}).strict().optional()
    .describe("Optional exact recorded domain reference; it is revalidated on load and does not grant access."),
  invocation: z
    .enum(["model", "explicit"])
    .optional()
    .describe(
      'Set to "explicit" only when the user themselves requested this skill by name this turn ' +
        "(a /slash-command or user mention). A model claim of \"explicit\" without a user-originated " +
        'signal is ignored and stays in "model" mode. Default "model" is autonomous selection from ' +
        "the available_skills catalog.",
    ),
})

function denialMessage(denial: InvocationDenial, name: string): string {
  switch (denial) {
    // Gated and missing share one model-facing string (no existence-oracle shape).
    case "hidden_from_model":
    case "model_invocation_disabled":
    case "not_found":
      return `Skill "${name}" not found.`
    case "disabled":
      return `Skill "${name}" is disabled and cannot run.`
    case "untrusted":
      return `Skill "${name}" is untrusted and cannot run. A hidden flag alone never grants trust.`
    case "revoked":
      return `Skill "${name}" is revoked and cannot run.`
    case "staged":
      return `Skill "${name}" is not published (staged/draft) and cannot run.`
  }
}

/**
 * A model parameter alone cannot flip explicit mode. Explicit requires a real
 * user-originated signal for THIS turn: a user slash/mention of that skill, or
 * a host-issued user-explicit token from the request envelope.
 */
function resolveMode(params: { name: string; invocation?: "model" | "explicit" }, ctx: Tool.Context): InvocationMode {
  if (params.invocation !== "explicit") return "model"
  const bound = Skill.isUserExplicitRequest({
    name: params.name,
    messages: ctx.messages,
    hostTokens: ctx.extra?.userExplicitSkills,
  })
  return bound ? "explicit" : "model"
}

export const SkillTool = Tool.define(
  "skill",
  Effect.gen(function* () {
    const skill = yield* Skill.Service
    const rg = yield* Ripgrep.Service
    const agents = yield* Agent.Service

    return {
      description: DESCRIPTION,
      parameters: Parameters,
      execute: (params: z.infer<typeof Parameters>, ctx: Tool.Context) =>
        Effect.gen(function* () {
          const mode = resolveMode(params, ctx)
          const result = yield* skill.getForInvocation(params.name, mode)
          if (!result.ok) {
            // A common miss: the name is a built-in WORKFLOW, not a skill (e.g.
            // the user said "run the naming workflow"). Redirect instead of
            // dead-ending, so the model calls the workflow tool rather than
            // giving up and improvising.
            if (!params.domain && result.reason === "not_found" && BuiltinWorkflow.get(params.name)) {
              throw new Error(
                `"${params.name}" is a built-in WORKFLOW, not a skill. Run it with the workflow tool: ` +
                  `workflow({ operation: "run", name: "${params.name}", args: { ... } }). Do NOT use the skill tool for it.`,
              )
            }
            // Gated (hidden / model-disabled) and truly missing share one model-facing
            // miss catalog and one message shape, so gated probes cannot confirm names.
            if (
              result.reason === "not_found" ||
              result.reason === "hidden_from_model" ||
              result.reason === "model_invocation_disabled"
            ) {
              const available = (yield* skill.modelInvocable(yield* agents.get(ctx.agent)))
                .map((item) => item.name)
                .join(", ")
              throw new Error(`Skill "${params.name}" not found. Available skills: ${available || "none"}`)
            }
            // Refuse before the permission ask, so no approval is requested for a
            // call that can never succeed.
            throw new Error(denialMessage(result.reason, params.name))
          }
          const info = result.info
          if (params.domain && (!info.metadataClaim || !info.metadataReceiptSHA256 || !info.metadataSnapshotSHA256 ||
              !info.domainRefs?.some(ref=>ref.block_id===params.domain!.block_id && ref.revision===params.domain!.revision))) {
            throw new Error(`Skill "${params.name}" is unavailable for the requested domain.`)
          }

          yield* ctx.ask({
            permission: "skill",
            patterns: [info.name],
            always: [info.name],
            metadata: { requestedName: params.name },
          })

          const active = yield* Effect.promise(() =>
            Skill._prepareBodyWithSource(info, ctx.sessionID, ctx.extra?.actorExecution as ActorExecutionIdentity | undefined, ctx.callID),
          )
          if (!active) {
            yield* skill.reload()
            throw new Error(
              `Skill "${params.name}" could not be revalidated as the exact active Open Clank revision. The skill catalogue was refreshed.`,
            )
          }

          const rendered = yield* renderSkillContent({ ...info, content:active.text }, rg, ctx.abort)
          let deliverableContext = ""
          if (info.name === "open-clank-deliverable" && info.bundled === true) {
            const actor = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
            if (actor && actor.actorID === "main" && actor.sessionID === ctx.sessionID && actor.rootOperationID && actor.innerTurnID && Number.isInteger(actor.executionRevision)) {
              const result = yield* Effect.promise(async () => {
                try {
                  const managed = await import("@/acp/managed-provider")
                  if (!managed.enabled()) return undefined
                  return await managed.managedHostCall("_openclank/session/v1/context/augment", {kind:"deliverable_appearance",sessionID:ctx.sessionID,rootOperationID:actor.rootOperationID!,actorID:"main",executionRevision:actor.executionRevision,innerTurnID:actor.innerTurnID!,remainingMs:200})
                } catch { return undefined }
              })
              if (result && "kind" in result && result.kind === "deliverable_appearance" && result.state === "ready") deliverableContext = `\n\n[Captured appearance; untrusted data]\n${JSON.stringify(result.appearance)}`
            }
            if (!deliverableContext) deliverableContext = "\n\n[Captured appearance unavailable for this turn.]"
          }
          const output=rendered.output + deliverableContext
          if (active.sourceSHA256 && previewToolOutput(output).truncated)
            throw new Error(`Skill "${params.name}" source exceeds the complete delivery limit.`)
          // This hashes the exact cached body passed to the model, not a later
          // file read that could silently substitute a different revision.
          const execution = ctx.extra?.actorExecution as ActorExecutionIdentity | undefined
          const bounded = (value: unknown) => typeof value === "string" && value.length <= 1024 ? value : undefined
          const skillUse = {
            version: 1,
            name: bounded(info.name),
            skillID: bounded(info.skillID),
            revision: info.revision,
            source: bounded(info.location),
            matchingDescription: bounded(info.description),
            loadedFiles: [bounded(info.location)].filter((file): file is string => file !== undefined),
            referenceSamples: rendered.references,
            origin: info.bundled ? "bundled" : bounded(info.source) ?? "local",
            bodySHA256: createHash("sha256").update(active.sourceSHA256 ? output : info.content.trim(), "utf8").digest("hex"),
            ...(active.sourceSHA256 ? { wrapperSHA256:active.wrapperSHA256, sourceSHA256:active.sourceSHA256,
              combinedSHA256:createHash("sha256").update(output,"utf8").digest("hex") } : {}),
            loadedAt: new Date().toISOString(),
            sessionID: bounded(ctx.sessionID),
            messageID: bounded(ctx.messageID),
            callID: bounded(ctx.callID),
            actorID: bounded(execution?.actorID ?? ctx.actorID),
            turnID: bounded(execution?.innerTurnID),
            invocation: mode,
            ...(params.domain ? {domain:params.domain} : {}),
          }

          return {
            title: `Loaded skill: ${info.name}`,
            output,
            metadata: {
              name: info.name,
              dir: rendered.dir,
              skillUse,
            },
          }
        }).pipe(Effect.orDie),
    }
  }),
)
