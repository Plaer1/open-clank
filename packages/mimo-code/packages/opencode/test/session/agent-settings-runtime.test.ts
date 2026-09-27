import { describe, expect, test } from "bun:test"
import { Config } from "@/config"
import { preserveRecentBudgetFor, tailTurnsFor } from "@/session/compaction"
import { resolveThresholds } from "@/session/prune"
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
})
