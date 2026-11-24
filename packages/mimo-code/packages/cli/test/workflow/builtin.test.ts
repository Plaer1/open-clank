import { describe, expect, test } from "bun:test"
import { BuiltinWorkflow } from "../../src/workflow/builtin"

describe("BuiltinWorkflow registry", () => {
  test("registers every script in the built-in directory", () => {
    // The directory is the registry, so losing a script is otherwise silent.
    expect(BuiltinWorkflow.list().map((w) => w.name)).toEqual([
      "advisor",
      "compose",
      "council",
      "deep-research",
      "fact-check",
      "research-experiment",
    ])
  })

  test("lists deep-research with parsed meta", () => {
    const list = BuiltinWorkflow.list()
    const dr = list.find((w) => w.name === "deep-research")
    expect(dr).toBeDefined()
    expect(dr!.description).toContain("Deep research")
    expect(dr!.whenToUse).toContain("multi-source")
  })

  test("get returns the script body starting with export const meta", () => {
    const dr = BuiltinWorkflow.get("deep-research")
    expect(dr).toBeDefined()
    expect(dr!.script.startsWith("export const meta")).toBe(true)
  })

  test("get returns undefined for an unknown name", () => {
    expect(BuiltinWorkflow.get("nope")).toBeUndefined()
  })
})
