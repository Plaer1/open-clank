import { describe, expect, test } from "bun:test"
import { buildFtsQuery, buildFtsMatch } from "../../src/history/fts-query"

describe("history.buildFtsQuery", () => {
  test("empty input", () => {
    expect(buildFtsQuery("")).toEqual({ fts: null })
    expect(buildFtsQuery("!!!")).toEqual({ fts: null })
  })

  test("latin tokens AND-join", () => {
    expect(buildFtsQuery("git log")).toEqual({ fts: '"git" AND "log"' })
    expect(buildFtsQuery("hello")).toEqual({ fts: '"hello"' })
  })

  test("CJK unigram: short words become adjacent phrases", () => {
    expect(buildFtsQuery("打包")).toEqual({ fts: '"打 包"' })
    expect(buildFtsQuery("打包脚本")).toEqual({ fts: '"打 包 脚 本"' })
    expect(buildFtsQuery("打包 脚本")).toEqual({ fts: '"打 包" AND "脚 本"' })
  })

  test("mixed CJK and latin", () => {
    expect(buildFtsQuery("go 打包").fts).toBe('"go" AND "打 包"')
    expect(buildFtsMatch("deploy")).toBe('"deploy"')
  })

  test("punctuation splits phrases; each phrase is adjacent", () => {
    expect(buildFtsQuery("foo bar、baz").fts).toBe('"foo bar" AND "baz"')
    expect(buildFtsQuery("打包、脚本").fts).toBe('"打 包" AND "脚 本"')
  })
})
