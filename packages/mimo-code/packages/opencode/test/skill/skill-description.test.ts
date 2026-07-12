import { describe, test, expect } from "bun:test"
import { skillDescription } from "../../src/cli/cmd/tui/i18n/skill"

describe("skillDescription", () => {
  const t = (key: string) => {
    const translations: Record<string, string> = {
      "tui.skill.evolve.description": "Translated evolve description",
      "tui.skill.compose:plan.description": "Translated compose plan",
    }
    return translations[key] as string
  }

  test("returns fallback for non-bundled skill", () => {
    expect(skillDescription(t, "my-custom-skill", "Custom description", false)).toBe("Custom description")
  })

  test("returns fallback when bundled is undefined", () => {
    expect(skillDescription(t, "evolve", "Fallback")).toBe("Fallback")
  })

  test("returns translation for bundled builtin skill", () => {
    expect(skillDescription(t, "evolve", "Fallback", true)).toBe("Translated evolve description")
  })

  test("returns translation for bundled compose skill", () => {
    expect(skillDescription(t, "compose:plan", "Fallback", true)).toBe("Translated compose plan")
  })

  test("returns fallback when translation key is missing", () => {
    expect(skillDescription(t, "unknown-bundled", "Fallback", true)).toBe("Fallback")
  })

  test("user override: same name as builtin but not bundled shows fallback", () => {
    expect(skillDescription(t, "evolve", "User custom evolve", false)).toBe("User custom evolve")
  })
})
