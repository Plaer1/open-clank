// Identity metaplan Slice 02 (ruling R14): the runtime never claims a
// product self-identity. Who the agent IS comes from the host's persona
// authority; prompt assets carry only operational role text. This test
// freezes that: no self-naming/branding in any model-facing prompt asset
// or in the SystemPrompt environment header.
import { describe, expect, test } from "bun:test"
import { readFileSync, readdirSync } from "node:fs"
import { join } from "node:path"

const ROOT = join(import.meta.dir, "../..")

const FORBIDDEN = [/MiMoCode/, /MiMo Code/, /Xiaomi MiMo Team/, /built by Xiaomi/i]

function offendersIn(path: string): string[] {
  const text = readFileSync(path, "utf8")
  const hits: string[] = []
  text.split("\n").forEach((line, index) => {
    for (const pattern of FORBIDDEN) {
      if (pattern.test(line)) {
        hits.push(`${path}:${index + 1}: ${line.trim().slice(0, 120)}`)
        break
      }
    }
  })
  return hits
}

describe("prompt assets are identity-neutral (R14)", () => {
  test("no self-identity in session prompt assets", () => {
    const dir = join(ROOT, "src/session/prompt")
    const offenders = readdirSync(dir)
      .filter((name) => name.endsWith(".txt"))
      .flatMap((name) => offendersIn(join(dir, name)))
    expect(offenders).toEqual([])
  })

  test("no self-identity in agent prompt assets", () => {
    const dir = join(ROOT, "src/agent")
    const offenders = readdirSync(dir, { recursive: true })
      .map(String)
      .filter((name) => name.endsWith(".txt"))
      .flatMap((name) => offendersIn(join(dir, name)))
    expect(offenders).toEqual([])
  })

  test("SystemPrompt environment header claims no product identity", () => {
    const offenders = offendersIn(join(ROOT, "src/session/system.ts")).filter(
      // comments explaining the rule are fine; template literals are not
      (hit) => !hit.includes("//"),
    )
    expect(offenders).toEqual([])
  })
})
