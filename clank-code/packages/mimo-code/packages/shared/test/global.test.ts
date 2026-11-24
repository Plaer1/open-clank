import { describe, expect, test } from "bun:test"
import path from "path"
import { resolveOpenclankHome } from "@mimo-ai/shared/global"

describe("resolveOpenclankHome", () => {
  test("with OPENCLANK_HOME set, resolves 4 subdirs under root", () => {
    const result = resolveOpenclankHome({
      OPENCLANK_HOME: "/tmp/profile-a",
    })
    expect(result.mode).toBe("openclank_home")
    expect(result.root).toBe("/tmp/profile-a")
    expect(result.config).toBe(path.join("/tmp/profile-a", "config"))
    expect(result.data).toBe(path.join("/tmp/profile-a", "data"))
    expect(result.state).toBe(path.join("/tmp/profile-a", "state"))
    expect(result.cache).toBe(path.join("/tmp/profile-a", "cache"))
  })

  test("without OPENCLANK_HOME, falls through to xdg mode", () => {
    const result = resolveOpenclankHome({})
    expect(result.mode).toBe("xdg")
    expect(result.root).toBeUndefined()
    // xdg paths end with "/openclank"
    expect(result.config.endsWith(path.join("", "openclank-engine"))).toBe(true)
    expect(result.data.endsWith(path.join("", "openclank-engine"))).toBe(true)
    expect(result.state.endsWith(path.join("", "openclank-engine"))).toBe(true)
    expect(result.cache.endsWith(path.join("", "openclank-engine"))).toBe(true)
  })

  test("empty OPENCLANK_HOME string is treated as unset (xdg mode)", () => {
    const result = resolveOpenclankHome({ OPENCLANK_HOME: "" })
    expect(result.mode).toBe("xdg")
  })

  test("relative OPENCLANK_HOME path throws with clear error", () => {
    expect(() => resolveOpenclankHome({ OPENCLANK_HOME: "./foo" })).toThrow(
      /OPENCLANK_HOME must be an absolute path/,
    )
    expect(() => resolveOpenclankHome({ OPENCLANK_HOME: "foo/bar" })).toThrow(
      /OPENCLANK_HOME must be an absolute path/,
    )
  })

  test("tilde-prefixed OPENCLANK_HOME throws (not treated as absolute)", () => {
    expect(() => resolveOpenclankHome({ OPENCLANK_HOME: "~/profiles/a" })).toThrow(
      /OPENCLANK_HOME must be an absolute path/,
    )
  })

  test("error message includes the offending value", () => {
    expect(() => resolveOpenclankHome({ OPENCLANK_HOME: "./relative" })).toThrow(
      /\.\/relative/,
    )
  })
})
