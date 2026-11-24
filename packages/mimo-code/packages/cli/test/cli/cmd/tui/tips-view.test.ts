import { describe, expect, test } from "bun:test"
import {
  buildTipKeys,
  hasUsableCredentials,
  pickDisplayKey,
  tipWeight,
} from "../../../../src/cli/cmd/tui/feature-plugins/home/tips-view"
import { dict as en } from "../../../../src/cli/cmd/tui/i18n/en"
import { dict as es } from "../../../../src/cli/cmd/tui/i18n/es"
import { dict as fr } from "../../../../src/cli/cmd/tui/i18n/fr"
import { dict as ja } from "../../../../src/cli/cmd/tui/i18n/ja"
import { dict as ru } from "../../../../src/cli/cmd/tui/i18n/ru"
import { dict as zh } from "../../../../src/cli/cmd/tui/i18n/zh"
import { dict as zht } from "../../../../src/cli/cmd/tui/i18n/zht"

// buildTipKeys assembles the weighted tip pool.
describe("buildTipKeys", () => {
  test("promotes localized chat guidance for discovering slash commands", () => {
    const key = "tui.tips.ask_slash_commands"
    expect(buildTipKeys("linux")).toContain(key)
    expect(tipWeight(key)).toBeGreaterThan(tipWeight("tui.tips.multi_skills"))
    Array.of(en, es, fr, ja, ru, zh, zht).forEach((dict) => expect(dict[key]).toBeTruthy())
  })

  test("includes localized guidance for toggling visual modes", () => {
    const key = "tui.tips.vivid"
    expect(buildTipKeys("linux")).toContain(key)
    expect(tipWeight(key)).toBe(tipWeight("tui.tips.theme_mode"))
    Array.of(en, es, fr, ja, ru, zh, zht).forEach((dict) => expect(dict[key]).toContain("{highlight}/vivid{/highlight}"))
  })

  test("includes exactly one tab-agent tip", () => {
    const tabKeys = buildTipKeys("linux").filter((k) => k.startsWith("tui.tips.tab_agent"))
    expect(tabKeys).toEqual(["tui.tips.tab_agent"])
  })

  test("appends the platform-specific suspend tip", () => {
    expect(buildTipKeys("win32")).toContain("tui.tips.suspend.win")
    expect(buildTipKeys("darwin")).toContain("tui.tips.suspend.unix")
    expect(buildTipKeys("linux")).toContain("tui.tips.suspend.unix")
  })

  test("keeps the login tip available for credential-less installs", () => {
    expect(buildTipKeys("linux")).toContain("tui.tips.login")
    Array.of(en, es, fr, ja, ru, zh, zht).forEach((dict) => expect(dict["tui.tips.login"]).toBeTruthy())
  })

  test("includes compose-next as a normal rotating tip with positive capability copy", () => {
    const key = "tui.tips.compose_next"
    expect(buildTipKeys("linux")).toContain(key)
    Array.of(en, es, fr, ja, ru, zh, zht).forEach((dict) => {
      expect(dict[key]).toBeTruthy()
      expect(dict[key]).toContain("/compose-next")
      // Never frame compose-next as a replacement for a Compose agent.
      expect(dict[key]).not.toMatch(/instead of|代替|替代|instead of the Compose/i)
    })
  })
})

describe("hasUsableCredentials", () => {
  test("is false when no computed provider carries credentials", () => {
    // mimo is always in the computed list — presence alone must not count.
    expect(hasUsableCredentials([])).toBe(false)
    expect(hasUsableCredentials([{ source: "custom", options: {} }])).toBe(false)
    expect(hasUsableCredentials([{ source: "config", options: { apiKey: "" } }])).toBe(false)
  })

  test("is true when any computed provider carries known credentials", () => {
    expect(hasUsableCredentials([{ source: "env", key: "sk-env" }])).toBe(true)
    expect(hasUsableCredentials([{ source: "env", options: {} }])).toBe(true)
    expect(hasUsableCredentials([{ source: "config", options: { apiKey: "sk-config" } }])).toBe(true)
    expect(hasUsableCredentials([{ source: "custom", options: {} }, { source: "api", key: "sk-api" }])).toBe(true)
  })
})

describe("pickDisplayKey", () => {
  test("forces the login tip when the install has no credentials", () => {
    expect(pickDisplayKey({ hasCredentials: false, rotationKey: "tui.tips.theme" })).toBe("tui.tips.login")
  })

  test("keeps the rotation tip once credentials exist", () => {
    expect(pickDisplayKey({ hasCredentials: true, rotationKey: "tui.tips.theme" })).toBe("tui.tips.theme")
  })

  test("does not lock compose-next over the rotation", () => {
    expect(pickDisplayKey({ hasCredentials: true, rotationKey: "tui.tips.compose_next" })).toBe("tui.tips.compose_next")
    expect(pickDisplayKey({ hasCredentials: true, rotationKey: "tui.tips.theme" })).toBe("tui.tips.theme")
  })
})
