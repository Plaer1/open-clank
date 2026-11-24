import { expect } from "bun:test"
import { Effect, Layer } from "effect"
import path from "node:path"
import { Command } from "../../src/command"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import { withEnv } from "../lib/env"

withEnv({ MIMOCODE_DISABLE_BUILTIN_SKILLS: "true", MIMOCODE_DISABLE_COMPOSE_SKILLS: "true" })
const it = testEffect(Layer.mergeAll(Command.defaultLayer, CrossSpawnSpawner.defaultLayer))

it.live("stale advertised skills are revalidated against actual changed lifecycle bytes at execution", () =>
  provideTmpdirInstance((directory) => Effect.gen(function* () {
    const file = path.join(directory, "skills", "gated-native", "SKILL.md")
    const source = (status: string) => `---\nname: gated-native\ndescription: Native trust regression fixture\nstatus: ${status}\n---\nFixture skill body.\n`
    yield* Effect.promise(() => Bun.write(file, source("published")))
    const commands = yield* Command.Service
    expect((yield* commands.list()).some((command) => command.name === "gated-native")).toBe(true)
    yield* Effect.promise(() => Bun.write(file, source("revoked")))
    // The already-discovered Skill state still contains the old advertisement.
    expect((yield* commands.list()).some((command) => command.name === "gated-native")).toBe(true)
    expect(yield* commands.get("gated-native")).toBeUndefined()
    expect((yield* commands.list()).some((command) => command.name === "gated-native")).toBe(false)
  }), { git: true, config: { skills: { paths: ["skills"] } } }),
)
