import z from "zod"
import { Effect } from "effect"
import { Ripgrep } from "../file/ripgrep"
import { Skill, type InvocationDenial, type InvocationMode } from "../skill"
import { BuiltinWorkflow } from "../workflow/builtin"
import * as Tool from "./tool"
import { renderSkillContent } from "./skill-content"
import DESCRIPTION from "./skill.txt"

const Parameters = z.object({
  name: z.string().describe("The name of the skill from available_skills"),
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
            if (result.reason === "not_found" && BuiltinWorkflow.get(params.name)) {
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
              const all = yield* skill.all()
              // Never leak hidden or model-disabled skills through the miss catalog.
              const available = all
                .filter((item) => Skill.isModelInvocable(item))
                .map((item) => item.name)
                .join(", ")
              throw new Error(`Skill "${params.name}" not found. Available skills: ${available || "none"}`)
            }
            // Refuse before the permission ask, so no approval is requested for a
            // call that can never succeed.
            throw new Error(denialMessage(result.reason, params.name))
          }
          const info = result.info

          yield* ctx.ask({
            permission: "skill",
            patterns: [params.name],
            always: [params.name],
            metadata: {},
          })

          const active = yield* Effect.promise(() =>
            Skill._writeUsage(info, ctx.sessionID),
          )
          if (!active) {
            yield* skill.reload()
            throw new Error(
              `Skill "${params.name}" could not be revalidated as the exact active Open Clank revision. The skill catalogue was refreshed.`,
            )
          }

          const rendered = yield* renderSkillContent(info, rg, ctx.abort)

          return {
            title: `Loaded skill: ${info.name}`,
            output: rendered.output,
            metadata: {
              name: info.name,
              dir: rendered.dir,
            },
          }
        }).pipe(Effect.orDie),
    }
  }),
)
