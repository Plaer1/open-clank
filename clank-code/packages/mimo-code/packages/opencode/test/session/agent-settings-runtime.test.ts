import { describe, expect, test } from "bun:test"
import { Config } from "@/config"
import { preserveRecentBudgetFor, tailTurnsFor } from "@/session/compaction"
import { contextWindow } from "@/session/overflow"
import { resolveThresholds } from "@/session/prune"
import { Effect, Layer } from "effect"
import * as CrossSpawnSpawner from "../../src/effect/cross-spawn-spawner"
import { provideTmpdirInstance } from "../fixture/fixture"
import { testEffect } from "../lib/effect"
import type { Provider } from "@/provider"

const model = {
  id: "test-model",
  providerID: "test-provider",
  limit: { context: 100_000, output: 10_000 },
} as Provider.Model

describe("managed agent settings native runtime bridge", () => {
  test("an admitted snapshot changes the real compaction calculations", () => {
    const base = {} as Config.Info
    const oldTurn = Config.withManagedAgentSettings(base, {
      compaction: { tail_turns: 2, preserve_recent_tokens: 8_000 },
      checkpoint: { reserved: 13_000 },
    })
    const newTurn = Config.withManagedAgentSettings(base, {
      compaction: { tail_turns: 0, preserve_recent_tokens: 2_000 },
      checkpoint: { reserved: 30_000 },
    })

    expect(tailTurnsFor(oldTurn)).toBe(2)
    expect(tailTurnsFor(newTurn)).toBe(0)
    expect(preserveRecentBudgetFor(oldTurn, model)).toBe(8_000)
    expect(preserveRecentBudgetFor(newTurn, model)).toBe(2_000)
    expect(resolveThresholds(["90%"], model.limit.context, oldTurn.checkpoint?.reserved)).toEqual([87_000])
    expect(resolveThresholds(["90%"], model.limit.context, newTurn.checkpoint?.reserved)).toEqual([70_000])
  })

  test("the turn snapshot is stable while reset affects only the next admission", () => {
    const admitted = Config.withManagedAgentSettings({} as Config.Info, {
      compaction: { tail_turns: 0, preserve_recent_tokens: 2_000 },
    })
    const reset = Config.withManagedAgentSettings({} as Config.Info, {})
    expect(tailTurnsFor(admitted)).toBe(0)
    expect(tailTurnsFor(reset)).toBe(2)
    expect(preserveRecentBudgetFor(admitted, model)).toBe(2_000)
    expect(preserveRecentBudgetFor(reset, model)).toBe(8_000)
  })
  test("model maps, thresholds and nested caps merge into real native calculations without shared references", () => {
    const base: Config.Info = { compaction: { tail_turns: 4, preserve_recent_tokens: 12_000 }, checkpoint: { reserved: 13_000, push_caps: { notes: 6_000, memory: 10_000 } } }
    const settings = { compaction: { max_context: { "test-provider/*": "50%" }, preserve_recent_tokens: 0 }, checkpoint: { thresholds: ["25%", "90%"], push_caps: { notes: 500 } } }
    const admitted = Config.withManagedAgentSettings(base, settings)
    expect(contextWindow({ cfg: admitted, model }).effective).toBe(50_000)
    expect(resolveThresholds(admitted.checkpoint!.thresholds!, 50_000, admitted.checkpoint?.reserved)).toEqual([12_500, 37_000])
    expect(admitted.checkpoint?.push_caps).toEqual({ notes: 500, memory: 10_000 })
    expect(tailTurnsFor(admitted)).toBe(4)
    expect(preserveRecentBudgetFor(admitted, model)).toBe(0)
    settings.compaction.max_context["test-provider/*"] = "80%"
    settings.checkpoint.thresholds[0] = "60%"
    settings.checkpoint.push_caps.notes = 900
    expect(contextWindow({ cfg: admitted, model }).effective).toBe(50_000)
    expect(admitted.checkpoint?.thresholds).toEqual(["25%", "90%"])
    expect(admitted.checkpoint?.push_caps?.notes).toBe(500)
    expect(base.checkpoint?.push_caps?.notes).toBe(6_000)
    expect(Config.withManagedAgentSettings(base, {}).compaction).toEqual(base.compaction)
  })

  test("lifecycle and deny-loop policies retain inherited fields and captured values", () => {
    const settings = { dream: { auto: false, interval_days: 0 }, distill: { auto: true }, experimental: { continue_loop_on_deny: true, try_best: { edit_matches: 4 } } }
    const cfg = Config.withManagedAgentSettings({ dream: { auto: true }, distill: { interval_days: 30 }, experimental: { try_best: { edit_window: 12 } } }, settings)
    settings.experimental.try_best.edit_matches = 8
    expect(cfg.dream).toEqual({ auto: false, interval_days: 0 })
    expect(cfg.distill).toEqual({ auto: true, interval_days: 30 })
    expect(cfg.experimental?.try_best).toEqual({ edit_window: 12, edit_matches: 4 })
    expect(cfg.experimental?.continue_loop_on_deny).toBe(true)
  })

  test("provider-level managed profile survives redacted public model discovery", () => {
    const base = { compaction: { tail_turns: 4 }, provider: { "test-provider": { options: { _openclankAgentSettings: { compaction: { tail_turns: 0 }, checkpoint: { thresholds: ["20%"] } } } } } } as Config.Info
    const publicModel = { ...model, options: {} }
    const admitted = Config.withManagedModelSettings(base, publicModel)
    expect(tailTurnsFor(admitted)).toBe(0)
    expect(admitted.checkpoint?.thresholds).toEqual(["20%"])
    expect(tailTurnsFor(Config.withManagedModelSettings(base, { ...publicModel, options: { _openclankAgentSettings: { compaction: { tail_turns: 1 } } } }))).toBe(1)
  })

  test("workflow runtime policy reports native defaults and lossless explicit generation policy", () => {
    expect(Config.workflowPolicyFor({})).toMatchObject({ maxDepth: 8, maxLifecycleAgents: 1000, scriptDeadlineMs: 43_200_000 })
    expect(Config.workflowPolicyFor({ workflow: { maxConcurrentAgents: 64, maxDepth: 2, maxLifecycleAgents: 5, scriptDeadlineMs: 1200 } })).toEqual({ maxConcurrentAgents: 64, maxDepth: 2, maxLifecycleAgents: 5, scriptDeadlineMs: 1200 })
  })

})


const runtime = testEffect(Layer.mergeAll(Config.defaultLayer, CrossSpawnSpawner.defaultLayer))
runtime.live("project and owner inline config merge before shared managed admission", () =>
  Effect.acquireUseRelease(
    Effect.sync(() => {
      const previous = process.env.MIMOCODE_CONFIG_CONTENT
      process.env.MIMOCODE_CONFIG_CONTENT = JSON.stringify({ workflow: { maxDepth: 2 }, provider: { "test-provider": { options: { _openclankAgentSettings: { compaction: { max_context: { "test-provider/*": "50%" }, preserve_recent_tokens: 0 }, checkpoint: { thresholds: ["25%"], push_caps: { notes: 500 } } } } } } })
      return previous
    }),
    () => provideTmpdirInstance(() => Effect.gen(function* () {
      const service = yield* Config.Service
      const actual = yield* service.get()
      const admitted = Config.withManagedModelSettings(actual, { ...model, options: {} })
      expect(tailTurnsFor(admitted)).toBe(4)
      expect(preserveRecentBudgetFor(admitted, model)).toBe(0)
      expect(contextWindow({ cfg: admitted, model }).effective).toBe(50_000)
      expect(admitted.checkpoint?.push_caps).toEqual({ memory: 6_000, notes: 500 })
      expect(Config.workflowPolicyFor(admitted).maxDepth).toBe(2)
    }), { git: true, config: { compaction: { tail_turns: 4, preserve_recent_tokens: 12_000 }, checkpoint: { push_caps: { memory: 6_000 } } } }),
    (previous) => Effect.sync(() => { if (previous === undefined) delete process.env.MIMOCODE_CONFIG_CONTENT; else process.env.MIMOCODE_CONFIG_CONTENT = previous }),
  ),
)
