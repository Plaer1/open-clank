import { expect, test } from "bun:test"
import { shouldExtract } from "../../src/memory/graph-extract"

test("default policy extracts every turn", () => {
  expect(shouldExtract({}, { turns: 0, lastExtractMs: 0 }, 1000)).toBe(true)
  expect(shouldExtract({}, { turns: 7, lastExtractMs: 0 }, 1000)).toBe(true)
})

test("every_n_turns gates by turn counter", () => {
  const cfg = { every_n_turns: 3 }
  expect(shouldExtract(cfg, { turns: 0, lastExtractMs: 0 }, 0)).toBe(true)
  expect(shouldExtract(cfg, { turns: 1, lastExtractMs: 0 }, 0)).toBe(false)
  expect(shouldExtract(cfg, { turns: 2, lastExtractMs: 0 }, 0)).toBe(false)
  expect(shouldExtract(cfg, { turns: 3, lastExtractMs: 0 }, 0)).toBe(true)
})

test("min_interval_seconds gates by wall clock", () => {
  const cfg = { min_interval_seconds: 60 }
  expect(shouldExtract(cfg, { turns: 0, lastExtractMs: 0 }, 5_000)).toBe(true)
  expect(shouldExtract(cfg, { turns: 0, lastExtractMs: 10_000 }, 40_000)).toBe(false)
  expect(shouldExtract(cfg, { turns: 0, lastExtractMs: 10_000 }, 71_000)).toBe(true)
})

test("both gates must pass", () => {
  const cfg = { every_n_turns: 2, min_interval_seconds: 60 }
  expect(shouldExtract(cfg, { turns: 1, lastExtractMs: 0 }, 999_000)).toBe(false)
  expect(shouldExtract(cfg, { turns: 2, lastExtractMs: 998_000 }, 999_000)).toBe(false)
  expect(shouldExtract(cfg, { turns: 2, lastExtractMs: 0 }, 999_000)).toBe(true)
})

import { resolveExtractionModel } from "../../src/memory/graph-extract"

test("extraction model prefers cheapest non-thinking over cheaper thinking", () => {
  const cfg = {
    provider: {
      deepseek: {
        models: {
          flash: { cost: { input: 0.14 }, reasoning: true },
          instant: { cost: { input: 1.74 }, reasoning: false },
          instant2: { cost: { input: 2.5 }, reasoning: false },
        },
      },
    },
  }
  expect(resolveExtractionModel(cfg)).toEqual({ providerID: "deepseek", modelID: "instant" } as never)
})

test("explicit memory.graph.model overrides everything", () => {
  const cfg = {
    memory: { graph: { model: "deepseek/flash" } },
    provider: { deepseek: { models: { instant: { cost: { input: 1 }, reasoning: false } } } },
  }
  expect(resolveExtractionModel(cfg)).toEqual({ providerID: "deepseek", modelID: "flash" } as never)
})
