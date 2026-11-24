import { test, expect } from "bun:test"
import path from "path"
import { Permission } from "../../src/permission"

// Mirrors the ruleset actually built in agent.ts for the default agent's skill
// permission, so we test the exact rule shape that ships.
function defaultAgentSkillRules() {
  return Permission.fromConfig({
    "*": "allow",
    doom_loop: "ask",
    skill: {
      "*": "allow",
    },
  })
}

// Compose Next discovery evolved in three steps. It was first hidden by an
// exact permission deny, but that also broke the user's own /compose-next
// invocation because permission means authorization for every caller. The
// generic disable-model-invocation frontmatter field then separated model
// reachability from user slash invocation. Compose Next has now graduated from
// that opt-out: it stays permission-allowed and omits the field, so models can
// discover and invoke it. Its description and body still require explicit user
// authorization for the workflow, including clear natural-language requests.
test("default agent allows compose-next for user and model invocation", () => {
  const rule = Permission.evaluate("skill", "compose-next", defaultAgentSkillRules())
  expect(rule.action).toBe("allow")
})

test("compose-next allows model invocation by omitting the opt-out frontmatter", async () => {
  const skill = await Bun.file(
    path.join(import.meta.dir, "../../src/skill/builtin/.bundle/compose-next/SKILL.md"),
  ).text()
  expect(skill).not.toContain("disable-model-invocation")
})

test("default agent allows an ordinary skill", () => {
  const rule = Permission.evaluate("skill", "deep-research", defaultAgentSkillRules())
  expect(rule.action).toBe("allow")
})

test("skill names starting with compose are ordinary skills", () => {
  // Retired `compose:*` deny is gone. A user skill literally named "compose"
  // (or compose:*) is authorized like any other skill.
  expect(Permission.evaluate("skill", "compose", defaultAgentSkillRules()).action).toBe("allow")
  expect(Permission.evaluate("skill", "compose:plan", defaultAgentSkillRules()).action).toBe("allow")
})
