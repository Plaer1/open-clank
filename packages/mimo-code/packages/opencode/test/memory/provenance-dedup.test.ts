import { describe, expect, test } from "bun:test"
import { mergeSearchRows, type SearchRow } from "../../src/memory/service"

// Real producer shapes, not fabricated symmetry:
// - FM (frankenmemory.ts callSearch over native.rs ingest_authored records):
//   one row PER SECTION — path is the stable record id, source_uri carries
//   `file://path#anchor`, content_hash is the section content hash, and
//   metadata.authored_path carries the file path.
// - Native (service.ts FTS mapping): one row PER FILE — path is the file,
//   source_uri is `file://path`, content_hash is the `${size}-${mtimeMs}`
//   fingerprint. The two hash schemes never collide, which is why the old
//   source_uri + content_hash dedup key was inert across backends.

const fmSection = (overrides: Partial<SearchRow>): SearchRow => ({
  path: "authored_9f8e7d6c5b4a",
  scope: "global",
  scope_id: "",
  type: "reference",
  snippet: "## Preferences\nUses bun, never npm",
  score: 1,
  source: "authored",
  trust: "ai",
  source_uri: "file:///brain/MEMORY.md#preferences",
  source_revision: "fm-section-hash-a",
  content_hash: "fm-section-hash-a",
  authored_path: "/brain/MEMORY.md",
  ...overrides,
})

const nativeFile = (overrides: Partial<SearchRow>): SearchRow => ({
  path: "/brain/MEMORY.md",
  scope: "global",
  scope_id: "",
  type: "memory",
  snippet: "## Preferences\nUses <<bun>>, never npm",
  score: 1,
  source: "markdown",
  trust: "authored",
  source_uri: "file:///brain/MEMORY.md",
  source_revision: "512-1724256000000",
  content_hash: "512-1724256000000",
  ...overrides,
})

describe("memory provenance merge", () => {
  test("a file surfaced by both backends with non-matching hashes is a flagged conflict, both rows stay", () => {
    const rows = mergeSearchRows([fmSection({})], [nativeFile({})], 10)
    expect(rows).toHaveLength(2)
    expect(rows.every((item) => item.provenance_conflict)).toBe(true)
    expect(rows.map((item) => item.backend).sort()).toEqual(["frankenmemory", "mimo"])
  })

  test("a native row collapses onto the FM section when the hash matches a section hash", () => {
    const rows = mergeSearchRows(
      [fmSection({})],
      // Hypothetical hash equality (a fingerprint can never equal a section
      // content hash in production) pins the collapse contract itself.
      [nativeFile({ source_revision: "fm-section-hash-a", content_hash: "fm-section-hash-a" })],
      10,
    )
    expect(rows).toHaveLength(1)
    expect(rows[0].backend).toBe("frankenmemory")
    expect(rows[0].path).toBe("authored_9f8e7d6c5b4a")
    expect(rows[0].provenance_conflict).toBeFalsy()
  })

  test("distinct FM sections of one file keep separate identities and do not conflict", () => {
    const rows = mergeSearchRows(
      [
        fmSection({}),
        fmSection({
          path: "authored_1a2b3c4d5e6f",
          snippet: "## Tooling\nPrefers ripgrep",
          source_uri: "file:///brain/MEMORY.md#tooling",
          source_revision: "fm-section-hash-b",
          content_hash: "fm-section-hash-b",
        }),
      ],
      [],
      10,
    )
    expect(rows).toHaveLength(2)
    expect(rows.every((item) => !item.provenance_conflict)).toBe(true)
  })

  test("the canonical FM row wins a collapsed duplicate regardless of score", () => {
    const rows = mergeSearchRows(
      [
        fmSection({ path: "fm-top", snippet: "another result", score: 100, authored_path: undefined }),
        fmSection({ path: "fm-canonical-id", score: 1 }),
      ],
      [nativeFile({ score: 10, source_revision: "fm-section-hash-a", content_hash: "fm-section-hash-a" })],
      10,
    )

    const duplicate = rows.find((item) => item.authored_path === "/brain/MEMORY.md")
    expect(duplicate?.backend).toBe("frankenmemory")
    expect(duplicate?.path).toBe("fm-canonical-id")
  })
})
