import { describe, expect, test } from "bun:test"
import path from "path"
import { Effect } from "effect"
import { Agent } from "../../src/agent/agent"
import { Instance } from "../../src/project/instance"
import { ModelID, ProviderID } from "../../src/provider/schema"
import { SystemPrompt } from "../../src/session/system"
import { provideInstance, tmpdir } from "../fixture/fixture"
import { ProviderTest } from "../fake/provider"

function load<A>(dir: string, fn: (svc: Agent.Interface) => Effect.Effect<A>) {
  return Effect.runPromise(provideInstance(dir)(Agent.Service.use(fn)).pipe(Effect.provide(Agent.defaultLayer)))
}

describe("session.system", () => {
  test("GPT prompt aligns exec and parallel-call guidance", () => {
    const prompt = SystemPrompt.provider(ProviderTest.model())[0]

    expect(prompt).toContain("Parallelize only tool calls that are independent")
    expect(prompt).toContain("keep dependencies sequential")
    expect(prompt).toContain("only one small call is needed")
    expect(prompt).not.toContain("When possible, prefer parallelization over sequential tool calls")
  })

  test("adds GPT tool guidance to prompted subagents", () => {
    const model = ProviderTest.model({
      id: ModelID.make("gpt-5.4"),
      api: { id: "deployment-primary" } as never,
    })
    const prompt = SystemPrompt.agent(
      {
        name: "explore",
        mode: "subagent",
        prompt: "Explore files without modifying them.",
        permission: [],
        options: {},
      },
      model,
    ).join("\n")
    const general = SystemPrompt.agent(
      {
        name: "general",
        mode: "subagent",
        permission: [],
        options: {},
      },
      model,
    ).join("\n")

    expect(prompt).toContain("Explore files without modifying them.")
    expect(prompt).toContain("Use `exec` as the main composition surface")
    expect(prompt).toContain("Use `apply_patch` for project text edits")
    expect(prompt).toContain("Use `view_image`")
    expect(prompt).toContain("`rg --files`")
    expect(general).toContain("On GPT models, use `exec` as the main composition surface")
  })

  test("prefers the catalog model ID when the API deployment ID is opaque", () => {
    const prompt = SystemPrompt.provider(
      ProviderTest.model({
        id: ModelID.make("gpt-5.4"),
        api: { id: "deployment-primary" } as never,
      }),
    )[0]

    expect(prompt).toContain("You are MiMoCode, an agent based on the GPT-5 family")
  })

  test("does not add GPT tool guidance to non-GPT or tool-less subagents", () => {
    const subagent = {
      name: "explore",
      mode: "subagent" as const,
      prompt: "Explore files.",
      permission: [],
      options: {},
    }
    const nonGPT = SystemPrompt.agent(
      subagent,
      ProviderTest.model({ id: ModelID.make("claude-sonnet-4-6"), api: { id: "claude-sonnet-4-6" } as never }),
    ).join("\n")
    const toolLess = SystemPrompt.agent(
      { ...subagent, toolAllowlist: [] },
      ProviderTest.model(),
    ).join("\n")

    expect(nonGPT).toBe("Explore files.")
    expect(toolLess).toBe("Explore files.")
  })

  test("does not inject vision capability guidance for GPT, Claude, or Gemini models", async () => {
    await using tmp = await tmpdir({ git: true })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const prompts = await Effect.runPromise(
          Effect.gen(function* () {
            const system = yield* SystemPrompt.Service
            return yield* Effect.all([
              system.environment(
                ProviderTest.model({ id: ModelID.make("gpt-5.4"), api: { id: "gpt-5.4" } as never }),
                Date.now(),
              ),
              system.environment(
                ProviderTest.model({
                  id: ModelID.make("claude-sonnet-4-6"),
                  providerID: ProviderID.make("anthropic"),
                  api: { id: "claude-sonnet-4-6" } as never,
                }),
                Date.now(),
              ),
              system.environment(
                ProviderTest.model({
                  id: ModelID.make("gemini-2.5-pro"),
                  providerID: ProviderID.make("google"),
                  api: { id: "gemini-2.5-pro" } as never,
                }),
                Date.now(),
              ),
            ])
          }).pipe(Effect.provide(SystemPrompt.defaultLayer)),
        )

        expect(prompts[0].join("\n")).not.toContain("<vision-capability>")
        expect(prompts[1].join("\n")).not.toContain("<vision-capability>")
        expect(prompts[2].join("\n")).not.toContain("<vision-capability>")
      },
    })
  })

  test("prompts the model to search skills from the first user query", async () => {
    await using tmp = await tmpdir({ git: true })
    const home = process.env.HOME
    const userProfile = process.env.USERPROFILE
    process.env.HOME = tmp.path
    process.env.USERPROFILE = tmp.path

    try {
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const build = await load(tmp.path, (svc) => svc.get("build"))
          const prompt = await Effect.runPromise(
            Effect.gen(function* () {
              return yield* (yield* SystemPrompt.Service).skills(build!)
            }).pipe(Effect.provide(SystemPrompt.defaultLayer)),
          )

          expect(prompt).toContain("first user query")
          expect(prompt).toContain("might benefit from a specialized workflow")
          expect(prompt).toContain("skill_search")
          expect(prompt).toContain("action")
          expect(prompt).toContain("input")
          expect(prompt).toContain("output")
          expect(prompt).toContain("audience")
        },
      })
    } finally {
      process.env.HOME = home
      process.env.USERPROFILE = userProfile
    }
  })

  test("skills output is sorted by name and stable across calls", async () => {
    await using tmp = await tmpdir({
      git: true,
      init: async (dir) => {
        for (const [name, description] of [
          ["zeta-skill", "Zeta skill."],
          ["alpha-skill", "Alpha skill."],
          ["middle-skill", "Middle skill."],
        ]) {
          const skillDir = path.join(dir, ".mimocode", "skill", name)
          await Bun.write(
            path.join(skillDir, "SKILL.md"),
            `---
name: ${name}
description: ${description}
---

# ${name}
`,
          )
        }
      },
    })

    const home = process.env.HOME
    const userProfile = process.env.USERPROFILE
    process.env.HOME = tmp.path
    process.env.USERPROFILE = tmp.path

    try {
      await Instance.provide({
        directory: tmp.path,
        fn: async () => {
          const build = await load(tmp.path, (svc) => svc.get("build"))
          const runSkills = Effect.gen(function* () {
            const svc = yield* SystemPrompt.Service
            return yield* svc.skills(build!)
          }).pipe(Effect.provide(SystemPrompt.defaultLayer))

          const first = await Effect.runPromise(runSkills)
          const second = await Effect.runPromise(runSkills)

          expect(first).toBe(second)

          const alpha = first!.indexOf("<name>alpha-skill</name>")
          const middle = first!.indexOf("<name>middle-skill</name>")
          const zeta = first!.indexOf("<name>zeta-skill</name>")

          expect(alpha).toBeGreaterThan(-1)
          expect(middle).toBeGreaterThan(alpha)
          expect(zeta).toBeGreaterThan(middle)
        },
      })
    } finally {
      process.env.HOME = home
      process.env.USERPROFILE = userProfile
    }
  })

  test("does not prompt GPT or Claude models to use skill_search", async () => {
    await using tmp = await tmpdir({ git: true })

    await Instance.provide({
      directory: tmp.path,
      fn: async () => {
        const build = await load(tmp.path, (svc) => svc.get("build"))
        const prompts = await Effect.runPromise(
          Effect.gen(function* () {
            const system = yield* SystemPrompt.Service
            return yield* Effect.all([
              system.skills(build!, { id: "gpt-5.4" }),
              system.skills(build!, { id: "claude-sonnet-4-6" }),
              system.skills(build!, { id: "mimo-v2" }),
            ])
          }).pipe(Effect.provide(SystemPrompt.defaultLayer)),
        )

        expect(prompts[0]).not.toContain("skill_search")
        expect(prompts[1]).not.toContain("skill_search")
        expect(prompts[2]).toContain("skill_search")
      },
    })
  })
})
