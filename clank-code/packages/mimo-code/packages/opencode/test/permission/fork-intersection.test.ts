import { describe, expect, test } from "bun:test"
import { Permission } from "../../src/permission"

describe("independent current permission intersection", () => {
  test("preserves each ruleset's precedence before intersecting a deny", () => {
    const source: Permission.Ruleset = [
      { permission: "*", pattern: "*", action: "deny" },
      { permission: "read", pattern: "*", action: "allow" },
    ]
    const destination: Permission.Ruleset = [{ permission: "read", pattern: "*", action: "allow" }]
    const decision = Permission.evaluateIntersection("read", "/project/file", [source, destination])
    expect(decision.rules.map((rule) => rule.action)).toEqual(["allow", "allow"])
    expect(decision.allowed).toBe(true)

    const deniedSource: Permission.Ruleset = [{ permission: "read", pattern: "*", action: "deny" }]
    const denied = Permission.evaluateIntersection("read", "/project/file", [deniedSource, destination])
    expect(denied.denied).toBe(true)
    expect(denied.allowed).toBe(false)
  })

  test("an ask remains an ask and any deny removes visible tools", () => {
    const source: Permission.Ruleset = [{ permission: "read", pattern: "*", action: "ask" }]
    const destination: Permission.Ruleset = [{ permission: "read", pattern: "*", action: "allow" }]
    expect(Permission.evaluateIntersection("read", "/project/file", [source, destination]).needsAsk).toBe(true)

    const denied = Permission.disabledIntersection(["read", "write"], [
      [{ permission: "*", pattern: "*", action: "deny" }, { permission: "read", pattern: "*", action: "allow" }],
      [{ permission: "read", pattern: "*", action: "deny" }],
    ])
    expect([...denied].sort()).toEqual(["read", "write"])
  })
})
