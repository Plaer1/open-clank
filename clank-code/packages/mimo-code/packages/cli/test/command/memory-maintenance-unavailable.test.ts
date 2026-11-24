import { expect } from "bun:test"
import { Effect, Exit, Layer } from "effect"
import { Command } from "../../src/command"
import { Agent } from "../../src/agent/agent"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"

const it = testEffect(Layer.mergeAll(Command.defaultLayer, Agent.defaultLayer, CrossSpawnSpawner.defaultLayer))

it.live("legacy dream and distill entries fail without Host adapters", () =>
  provideTmpdirInstance(() => Effect.gen(function* () {
    const commands = yield* Command.Service
    const agents = yield* Agent.Service
    for (const name of ["dream", "distill"]) {
      expect(Exit.isFailure(yield* commands.get(name).pipe(Effect.exit))).toBe(true)
      expect(Exit.isFailure(yield* agents.get(name).pipe(Effect.exit))).toBe(true)
    }
    expect((yield* commands.list()).some((item) => item.name === "dream")).toBe(true)
    expect((yield* agents.get("build")).name).toBe("build")
  })),
)
