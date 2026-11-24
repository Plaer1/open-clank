import { BusEvent } from "@/bus/bus-event"
import { InstanceState } from "@/effect"
import { EffectBridge } from "@/effect"
import { Flag } from "@/flag/flag"
import type { InstanceContext } from "@/project/instance"
import { SessionID, MessageID } from "@/session/schema"
import { Effect, Layer, Context } from "effect"
import z from "zod"
import { Config } from "../config"
import { MCP } from "../mcp"
import { Skill } from "../skill"
import { RecoverableError } from "@/tool/recoverable"
import PROMPT_INITIALIZE from "./template/initialize.txt"
import PROMPT_LOOPS from "./template/loops.txt"
import PROMPT_REVIEW from "./template/review.txt"

export const Event = {
  Executed: BusEvent.define(
    "command.executed",
    z.object({
      name: z.string(),
      sessionID: SessionID.zod,
      arguments: z.string(),
      messageID: MessageID.zod,
    }),
  ),
}

export const Info = z
  .object({
    name: z.string(),
    description: z.string().optional(),
    agent: z.string().optional(),
    model: z.string().optional(),
    source: z.enum(["command", "mcp", "skill"]).optional(),
    bundled: z.boolean().optional(),
    // workaround for zod not supporting async functions natively so we use getters
    // https://zod.dev/v4/changelog?id=zfunction
    template: z.promise(z.string()).or(z.string()),
    subtask: z.boolean().optional(),
    hints: z.array(z.string()),
  })
  .meta({
    ref: "Command",
  })

// for some reason zod is inferring `string` for z.promise(z.string()).or(z.string()) so we have to manually override it
export type Info = Omit<z.infer<typeof Info>, "template"> & { template: Promise<string> | string }

export function hints(template: string) {
  const result: string[] = []
  const numbered = template.match(/\$\d+/g)
  if (numbered) {
    for (const match of [...new Set(numbered)].sort()) result.push(match)
  }
  if (template.includes("$ARGUMENTS")) result.push("$ARGUMENTS")
  return result
}

export const Default = {
  INIT: "init",
  REVIEW: "review",
  DREAM: "dream",
  DISTILL: "distill",
  GOAL: "goal",
  DEEP_RESEARCH: "deep-research",
  LOOPS: "loops",
  REBUILD: "rebuild",
} as const

export function deepResearchTemplate(): string {
  return [
    "The user wants a deep, multi-source, fact-checked research report.",
    "",
    "Research request:",
    "$ARGUMENTS",
    "",
    "If the request is underspecified (missing scope, constraints, region, time range, etc.),",
    "ask 2-3 brief clarifying questions FIRST, then weave the answers into a refined question.",
    "",
    "When the request is specific enough, run the built-in deep-research workflow:",
    '  workflow({ operation: "run", name: "deep-research", args: "<the refined research question>" })',
    "",
    "Pass the full refined question as `args`. The workflow fans out web searches, fetches sources,",
    "adversarially verifies claims, and returns a cited report; relay its result to the user.",
  ].join("\n")
}

export interface Interface {
  readonly get: (name: string) => Effect.Effect<Info | undefined>
  readonly list: () => Effect.Effect<Info[]>
}

export class Service extends Context.Service<Service, Interface>()("@opencode/Command") {}

export const layer = Layer.effect(
  Service,
  Effect.gen(function* () {
    const config = yield* Config.Service
    const mcp = yield* MCP.Service
    const skill = yield* Skill.Service

    const init = Effect.fn("Command.state")(function* (ctx: InstanceContext) {
      const cfg = yield* config.get()
      const bridge = yield* EffectBridge.make()
      const commands: Record<string, Info> = {}

      commands[Default.INIT] = {
        name: Default.INIT,
          bundled: true,
        description: "guided AGENTS.md setup",
        source: "command",
        get template() {
          return PROMPT_INITIALIZE.replace("${path}", ctx.worktree)
        },
        hints: hints(PROMPT_INITIALIZE),
      }
      commands[Default.REVIEW] = {
        name: Default.REVIEW,
          bundled: true,
        description: "review changes [commit|branch|pr], defaults to uncommitted",
        source: "command",
        get template() {
          return PROMPT_REVIEW.replace("${path}", ctx.worktree)
        },
        subtask: true,
        hints: hints(PROMPT_REVIEW),
      }
      commands[Default.DREAM] = {
        name: Default.DREAM, bundled: true, source: "command", subtask: false,
        description: "Consolidation requires an admitted Host maintenance adapter",
        get template() { return "Consolidation is unavailable until the Host maintenance adapter is configured." },
        hints: ["$ARGUMENTS"],
      }
      commands[Default.DISTILL] = {
        name: Default.DISTILL, bundled: true, source: "command", subtask: false,
        description: "Distillation requires an admitted Host teacher adapter",
        get template() { return "Distillation is unavailable until the Host teacher adapter is configured." },
        hints: ["$ARGUMENTS"],
      }
      commands[Default.GOAL] = {
        name: Default.GOAL,
          bundled: true,
        description: "set a stop-condition goal; /goal with no arguments or /goal clear cancels it",
        source: "command",
        subtask: false,
        get template() {
          return "$ARGUMENTS"
        },
        hints: ["$ARGUMENTS"],
      }
      commands[Default.REBUILD] = {
        name: Default.REBUILD,
          bundled: true,
        description: "rebuild the conversation context now from the latest checkpoint (frees context; recent activity is summarized)",
        source: "command",
        subtask: false,
        get template() {
          return "$ARGUMENTS"
        },
        hints: ["$ARGUMENTS"],
      }

      if (Flag.OPENCLANK_EXPERIMENTAL_WORKFLOW_TOOL) {
        commands[Default.DEEP_RESEARCH] = {
          name: Default.DEEP_RESEARCH,
          bundled: true,
          description: "deep multi-source, fact-checked research report (runs the deep-research workflow)",
          source: "command",
          subtask: false,
          get template() {
            return deepResearchTemplate()
          },
          hints: ["$ARGUMENTS"],
        }
      }

      if (Flag.OPENCLANK_EXPERIMENTAL_CRON) {
        commands[Default.LOOPS] = {
          name: Default.LOOPS,
          bundled: true,
          description: "list active scheduled jobs; accepts `cancel <id>` to delete one",
          source: "command",
          subtask: false,
          get template() {
            return PROMPT_LOOPS
          },
          hints: hints(PROMPT_LOOPS),
        }
      }

      for (const [name, command] of Object.entries(cfg.command ?? {})) {
        commands[name] = {
          name,
          agent: command.agent,
          model: command.model,
          description: command.description,
          source: "command",
          get template() {
            return command.template
          },
          subtask: command.subtask,
          hints: hints(command.template),
        }
      }

      for (const [name, prompt] of Object.entries(yield* mcp.prompts())) {
        commands[name] = {
          name,
          source: "mcp",
          description: prompt.description,
          get template() {
            return bridge.promise(
              mcp
                .getPrompt(
                  prompt.client,
                  prompt.name,
                  prompt.arguments
                    ? Object.fromEntries(prompt.arguments.map((argument, i) => [argument.name, `$${i + 1}`]))
                    : {},
                )
                .pipe(
                  Effect.map(
                    (template) =>
                      template?.messages
                        .map((message) => (message.content.type === "text" ? message.content.text : ""))
                        .join("\n") || "",
                  ),
                ),
            )
          },
          hints: prompt.arguments?.map((_, i) => `$${i + 1}`) ?? [],
        }
      }

      // Host slash entries use the same trust filter as the skill tool's
      // user-explicit mode. Skill.all is NOT a trust filter — disabled,
      // untrusted, revoked, and staged skills must never become commands.
      for (const item of yield* skill.explicit()) {
        if (commands[item.name]) continue
        commands[item.name] = {
          name: item.name,
          description: item.description,
          source: "skill",
          bundled: item.bundled,
          get template() {
            return item.content
          },
          hints: [],
        }
      }

      return {
        commands,
      }
    })

    const get = Effect.fn("Command.get")(function* (name: string) {
      const context = yield* InstanceState.context
      const s = yield* init(context)
      if ((name === Default.DREAM || name === Default.DISTILL) && s.commands[name]?.bundled)
        return yield* Effect.die(new RecoverableError(name === Default.DREAM
          ? "Memory consolidation requires the admitted Host maintenance adapter"
          : "Distillation requires the admitted Host teacher adapter"))
      if (s.commands[name]?.source !== "skill") return s.commands[name]
      // Revalidate explicit skill trust at execution, even if discovery was stale.
      yield* skill.reload()
      return (yield* init(context)).commands[name]
    })

    const list = Effect.fn("Command.list")(function* () {
      const s = yield* init(yield* InstanceState.context)
      return Object.values(s.commands)
    })

    return Service.of({ get, list })
  }),
)

/**
 * Application composition variant. The process-wide AppLayer supplies the
 * MCP service so Command and SessionPrompt share one client set instead of
 * each hiding a separately scoped transport layer.
 *
 * Layer.suspend defers the Config/Skill/MCP `.defaultLayer` reads to first
 * use. Without it a circular import that loads this module mid-bootstrap
 * throws "Cannot access 'defaultLayer' before initialization" and poisons
 * combined test runs that share the process module registry.
 */
export const appLayer = Layer.suspend(() =>
  layer.pipe(
    Layer.provide(Config.defaultLayer),
    Layer.provide(Skill.defaultLayer),
  ),
)

export const defaultLayer = Layer.suspend(() => appLayer.pipe(Layer.provide(MCP.defaultLayer)))

export * as Command from "."
