import { describe, expect, test } from "bun:test"
import path from "node:path"
import { parseCcPath } from "../../src/memory/paths"

// Synthetic absolute paths that satisfy parseCcPath's `.claude/projects`
// anchor without referencing any real machine.
describe("parseCcPath", () => {
  test("standard slug + per-memory file", () => {
    expect(
      parseCcPath("/home/u/.claude/projects/-myproj/memory/feedback_x.md"),
    ).toEqual({
      scope: "cc",
      scope_id: "-myproj",
      type: "free", // type is decided by frontmatter, not path; parser leaves "free"
      key: "feedback_x",
    })
  })

  test("MEMORY.md (the index file)", () => {
    expect(parseCcPath("/home/u/.claude/projects/-myproj/memory/MEMORY.md")).toEqual({
      scope: "cc",
      scope_id: "-myproj",
      type: "free",
      key: "MEMORY",
    })
  })

  test("deeply nested slug with dashes", () => {
    expect(
      parseCcPath(
        "/home/u/.claude/projects/-deeply--nested--slug-with-many-dashes/memory/project_x.md",
      ),
    ).toEqual({
      scope: "cc",
      scope_id: "-deeply--nested--slug-with-many-dashes",
      type: "free",
      key: "project_x",
    })
  })

  test("nested key under memory dir is allowed", () => {
    expect(parseCcPath("/home/u/.claude/projects/-foo/memory/sub/file.md")).toEqual({
      scope: "cc",
      scope_id: "-foo",
      type: "free",
      key: "sub/file",
    })
  })

  test("non-CC path returns null", () => {
    expect(parseCcPath("/data/memory/global/x.md")).toBeNull()
  })

  test("missing /memory/ segment returns null", () => {
    expect(parseCcPath("/home/u/.claude/projects/-foo/something-else.md")).toBeNull()
  })

  test("non-md file returns null", () => {
    expect(parseCcPath("/home/u/.claude/projects/-foo/memory/x.txt")).toBeNull()
  })

  // Desktop engine-runtime [TP-R6-06]: CC uses the same native separator boundary.
  test("Windows drive, UNC and mixed separators preserve CC slug and nested key", () => {
    for (const input of [
      path.win32.join("C:/example/.claude/projects/-example/memory", "sub/file.md"),
      String.raw`\\server\share\.claude\projects\-example\memory\sub\file.md`,
      String.raw`C:\example/.claude\projects/-example\memory/sub\file.md`,
    ]) {
      expect(parseCcPath(input)).toEqual({ scope: "cc", scope_id: "-example", key: "sub/file", type: "free" })
    }
  })

  test("Windows non-CC layouts and non-markdown files stay excluded", () => {
    expect(parseCcPath(String.raw`C:\example\projects\-example\memory\file.md`)).toBeNull()
    expect(parseCcPath(String.raw`C:\example\.claude\projects\-example\memory\file.txt`)).toBeNull()
  })

  test.skipIf(process.platform === "win32")("POSIX CC backslash remains in the key", () => {
    expect(parseCcPath(String.raw`/example/.claude/projects/-example/memory/sub\file.md`)).toEqual({
      scope: "cc", scope_id: "-example", key: String.raw`sub\file`, type: "free",
    })
  })
})
