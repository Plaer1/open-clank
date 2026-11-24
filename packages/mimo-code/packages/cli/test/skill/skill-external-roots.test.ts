import { describe, expect } from "bun:test"
import { Effect, Layer } from "effect"
import path from "path"
import fs from "fs/promises"
import { Skill } from "../../src/skill"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideInstance, provideTmpdirInstance, tmpdir } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { withEnv } from "../lib/env"

// Isolate from bundles and host ~/.agents. Each case sets the root env it needs.
withEnv({
  OPENCLANK_DISABLE_BUILTIN_SKILLS: "true",
  OPENCLANK_DISABLE_AGENTS_SKILLS: "true",
  OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS: undefined,
  OPENCLANK_ENABLE_CODEX_SKILLS: undefined,
  OPENCLANK_ENABLE_OPENCODE_SKILLS: undefined,
})

const it = testEffect(Layer.mergeAll(Skill.defaultLayer, CrossSpawnSpawner.defaultLayer))

async function writeSkill(root: string, rel: string, name: string, description: string) {
  const dir = path.join(root, ...rel.split("/"))
  await fs.mkdir(dir, { recursive: true })
  await Bun.write(
    path.join(dir, "SKILL.md"),
    `---
name: ${name}
description: ${description}
---

# ${name}
`,
  )
}

const withEnvFor = <A, E, R>(values: Record<string, string | undefined>, self: Effect.Effect<A, E, R>) => {
  const keys = Object.keys(values)
  return Effect.acquireUseRelease(
    Effect.sync(() => {
      const saved = keys.map((key) => [key, process.env[key]] as const)
      for (const [key, value] of Object.entries(values)) {
        if (value === undefined) delete process.env[key]
        else process.env[key] = value
      }
      return saved
    }),
    () => self,
    (saved) =>
      Effect.sync(() => {
        for (const [key, value] of saved) {
          if (value === undefined) delete process.env[key]
          else process.env[key] = value
        }
      }),
  )
}

const withHome = <A, E, R>(home: string, self: Effect.Effect<A, E, R>) =>
  Effect.acquireUseRelease(
    Effect.sync(() => {
      const prev = process.env.HOME
      const prevUserProfile = process.env.USERPROFILE
      process.env.HOME = home
      process.env.USERPROFILE = home
      return { prev, prevUserProfile }
    }),
    () => self,
    ({ prev, prevUserProfile }) =>
      Effect.sync(() => {
        process.env.HOME = prev
        process.env.USERPROFILE = prevUserProfile
      }),
  )

describe("skill external root defaults", () => {
  it.live("external disable blocks implicit roots while preserving configured skills", () =>
    provideTmpdirInstance(
      (dir) => withEnvFor({ OPENCLANK_DISABLE_EXTERNAL_SKILLS: "true", OPENCLANK_ENABLE_AGENTS_SKILLS: "true",
        OPENCLANK_DISABLE_AGENTS_SKILLS: undefined, OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS: "true" },
        Effect.gen(function* () {
          yield* Effect.promise(() => writeSkill(dir, ".agents/skills/shared", "shared", "implicit"))
          yield* Effect.promise(() => writeSkill(dir, ".claude/skills/claude", "claude", "implicit"))
          yield* Effect.promise(() => writeSkill(dir, "owned/host", "host", "configured"))
          const skills = yield* Skill.Service
          const names = (yield* skills.all()).map((item) => item.name)
          expect(names).toContain("host")
          expect(names).not.toContain("shared")
          expect(names).not.toContain("claude")
        })),
      { git: true, config: { skills: { paths: ["owned"] } } },
    ),
  )

  it.live("no brand root is loaded without opt-in", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() => writeSkill(dir, ".claude/skills/claude-skill", "claude-skill", "claude"))
          yield* Effect.promise(() => writeSkill(dir, ".codex/skills/codex-skill", "codex-skill", "codex"))
          yield* Effect.promise(() => writeSkill(dir, ".opencode/skills/opencode-skill", "opencode-skill", "opencode"))
          yield* Effect.promise(() => writeSkill(dir, ".agents/skills/agent-skill", "agent-skill", "agents"))

          const skill = yield* Skill.Service
          expect(yield* skill.all()).toEqual([])
        }),
      { git: true },
    ),
  )

  it.live("agents root loads only with explicit opt-in", () =>
    provideTmpdirInstance(
      (dir) =>
        withEnvFor(
          { OPENCLANK_DISABLE_AGENTS_SKILLS: undefined, OPENCLANK_ENABLE_AGENTS_SKILLS: "true" },
          Effect.gen(function* () {
            yield* Effect.promise(() => writeSkill(dir, ".agents/skills/agent-skill", "agent-skill", "agents"))
            const skill = yield* Skill.Service
            expect((yield* skill.all()).map((s) => s.name)).toContain("agent-skill")
          }),
        ),
      { git: true },
    ),
  )

  it.live("OPENCLANK_DISABLE_AGENTS_SKILLS turns the agents root off", () =>
    provideTmpdirInstance(
      (dir) =>
        Effect.gen(function* () {
          yield* Effect.promise(() => writeSkill(dir, ".agents/skills/agent-skill", "agent-skill", "agents"))
          const skill = yield* Skill.Service
          expect(yield* skill.all()).toEqual([])
        }),
      { git: true },
    ),
  )

  it.live("OPENCLANK_ENABLE_CODEX_SKILLS loads user skills but not skills/.system", () =>
    provideTmpdirInstance(
      (dir) =>
        withEnvFor(
          { OPENCLANK_ENABLE_CODEX_SKILLS: "true" },
          Effect.gen(function* () {
            yield* Effect.promise(() => writeSkill(dir, ".codex/skills/user-skill", "user-skill", "user"))
            yield* Effect.promise(() => writeSkill(dir, ".codex/skills/.system/system-skill", "system-skill", "system"))

            const skill = yield* Skill.Service
            const names = (yield* skill.all()).map((s) => s.name)
            expect(names).toEqual(["user-skill"])
          }),
        ),
      { git: true },
    ),
  )

  it.live("OPENCLANK_ENABLE_OPENCODE_SKILLS loads user skills", () =>
    provideTmpdirInstance(
      (dir) =>
        withEnvFor(
          { OPENCLANK_ENABLE_OPENCODE_SKILLS: "true" },
          Effect.gen(function* () {
            yield* Effect.promise(() => writeSkill(dir, ".opencode/skills/user-skill", "user-skill", "user"))

            const skill = yield* Skill.Service
            expect((yield* skill.all()).map((s) => s.name)).toEqual(["user-skill"])
          }),
        ),
      { git: true },
    ),
  )

  it.live("OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS loads user skills but not skills/.trash", () =>
    provideTmpdirInstance(
      (dir) =>
        withEnvFor(
          { OPENCLANK_ENABLE_CLAUDE_CODE_SKILLS: "true" },
          Effect.gen(function* () {
            yield* Effect.promise(() => writeSkill(dir, ".claude/skills/user-skill", "user-skill", "user"))
            yield* Effect.promise(() => writeSkill(dir, ".claude/skills/.trash/old-skill", "old-skill", "trash"))

            const skill = yield* Skill.Service
            const names = (yield* skill.all()).map((s) => s.name)
            expect(names).toEqual(["user-skill"])
          }),
        ),
      { git: true },
    ),
  )

  it.live("global codex user skills load under ENABLE and .system stays invisible", () =>
    Effect.gen(function* () {
      const tmp = yield* Effect.acquireRelease(
        Effect.promise(() => tmpdir({ git: true })),
        (tmp) => Effect.promise(() => tmp[Symbol.asyncDispose]()),
      )

      yield* withHome(
        tmp.path,
        withEnvFor(
          { OPENCLANK_ENABLE_CODEX_SKILLS: "true" },
          Effect.gen(function* () {
            yield* Effect.promise(() => writeSkill(tmp.path, ".codex/skills/user-skill", "user-skill", "user"))
            yield* Effect.promise(() => writeSkill(tmp.path, ".codex/skills/.system/system-skill", "system-skill", "system"))

            yield* Effect.gen(function* () {
              const skill = yield* Skill.Service
              const list = yield* skill.all()
              expect(list.map((s) => s.name)).toEqual(["user-skill"])
              expect(list[0]!.location).toContain(path.join(".codex", "skills", "user-skill", "SKILL.md"))
            }).pipe(provideInstance(tmp.path))
          }),
        ),
      )
    }),
    30_000,
  )
})
