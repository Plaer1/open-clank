import { describe, expect, test } from "bun:test"
import { Effect } from "effect"
import type { Config } from "../../src/config"
import { shouldAutoDistill, shouldAutoDream } from "../../src/session/auto-dream"

const projectID = "project-memory-disabled" as any
const disabledConfig = {
  dream: { auto: true },
  distill: { auto: true },
  memory: { disable_write: true },
} as Config.Info

describe("automatic memory jobs", () => {
  test("does not schedule dream or distill while memory writes are disabled", async () => {
    expect(await Effect.runPromise(shouldAutoDream(disabledConfig, projectID))).toBe(false)
    expect(await Effect.runPromise(shouldAutoDistill(disabledConfig, projectID))).toBe(false)
  })
})
