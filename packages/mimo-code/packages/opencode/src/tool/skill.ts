import z from "zod"
import { Effect } from "effect"
import { Ripgrep } from "../file/ripgrep"
import { Skill, type InvocationDenial } from "../skill"
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
      'Set to "explicit" only when the user themselves requested this skill by name ' +
        "(for example a hidden skill the user named or a /slash-command). " +
        'Default "model" is autonomous selection from the available_skills catalog.',
    ),
})

function denialMessage(denial: InvocationDenial, name: string): string {
  switch (denial) {
    case "hidden_from_model":
      return (
        `Skill "${name}" is hidden from autonomous model discovery. ` +
        `If the user explicitly requested it, retry with invocation: "explicit". ` +
        `Otherwise pick a skill from available_skills or continue without one.`
      )
    case "model_invocation_disabled":
      return (
        `Skill "${name}" has disable-model-invocation set. Only the user may start it ` +
        `(via /${name} or an explicit request). If the user explicitly requested it, ` +
        `retry with invocation: "explicit".`
      )
    case "disabled":
      return `Skill "${name}" is disabled and cannot run.`
    case "untrusted":
      return `Skill "${name}" is untrusted and cannot run. A hidden flag alone never grants trust.`
    case "revoked":
      return `Skill "${name}" is revoked and cannot run.`
    case "staged":
      return `Skill "${name}" is not published (staged/draft) and cannot run.`
    default:
      return `Skill "${name}" not found.`
  }
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
          const mode = params.invocation ?? "model"
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
            if (result.reason === "not_found") {
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
