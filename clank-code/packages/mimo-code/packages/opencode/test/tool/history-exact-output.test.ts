import { describe, expect, test } from "bun:test"
import { createHash } from "node:crypto"
import { queryHistoryOutput } from "../../src/tool/history-output-query"

// Entry point (HistoryTool exact_output_query) needs ManagedProvider scope/issuer/host-context
// scaffolding; this tests the search function it delegates to, with a fake host store behind readPage.
const sha = (b: Buffer) => createHash("sha256").update(b).digest("hex")

type Ident = { session: string; actor: string; part: string }
function store(records: Record<string, Buffer>, revision = 1) {
  const key = (i: Ident) => `${i.session}|${i.actor}|${i.part}`
  const hashOf = (i: Ident) => sha(Buffer.from(key(i)))
  // Same shape as the host's exact_output page reply; scope = identity key, foreign key -> not found.
  const reader = (i: Ident, mutate?: (text: string) => string) => async (offset: number, limit: number) => {
    const src = records[key(i)]
    if (!src) return { state: "unavailable" }
    let end = Math.min(offset + limit, src.length)
    while (end < src.length && (src[end]! & 0xc0) === 0x80) end--
    const text = src.subarray(offset, end).toString("utf8")
    return {
      state: "ready",
      page: {
        text: mutate ? mutate(text) : text, revision, contentHash: hashOf(i), outputSHA256: sha(src),
        outputBytes: src.length, offset, nextOffset: end < src.length ? end : null,
      },
    }
  }
  return { reader, hashOf }
}
const A: Ident = { session: "ses_a", actor: "main", part: "p1" }

const DISTINCT = "\t  distinct é😀 line  \t"
const PREFIX = `head\n${DISTINCT}\n`
// marker straddles byte 8192: the page size and tool-view line clip (MAX_LINE_BYTES in session/tool-view-repeated-lines.ts)
const LONG = "L".repeat(8190 - Buffer.byteLength(PREFIX)) + "EDGE-MARK" + "L".repeat(1000)
const ORIGINAL = Buffer.from(`${PREFIX}${LONG}needle-after-clip\ntail\n`, "utf8")
const VIEW = "head\n[view clipped: 9000 chars omitted]\ntail\n" // what the model saw; never stored here

async function q(literal: string, opts: { id?: Ident; recs?: Record<string, Buffer>; start?: number; limit?: number; mutate?: (t: string) => string } = {}) {
  const id = opts.id ?? A
  const s = store(opts.recs ?? { [`${A.session}|${A.actor}|${A.part}`]: ORIGINAL })
  return queryHistoryOutput({
    revision: 1, contentHash: s.hashOf(id), literal, start: opts.start ?? 0, limit: opts.limit ?? 20,
    current: () => true, readPage: s.reader(id, opts.mutate),
  })
}
const slice = (r: { matches: { start: number; end: number }[] }, i = 0) => ORIGINAL.subarray(r.matches[i]!.start, r.matches[i]!.end)

describe("history exact_output_query search", () => {
  test("finds a distinctive line byte-identically (whitespace, tabs, non-ASCII)", async () => {
    const r = (await q(DISTINCT))!
    expect(r.matches).toHaveLength(1)
    expect(slice(r).equals(Buffer.from(DISTINCT, "utf8"))).toBe(true)
    expect(r.outputSHA256).toBe(sha(ORIGINAL))
    expect(r.outputBytes).toBe(ORIGINAL.length)
  })

  test("finds text past the clip, straddling a page boundary; returns original bytes not the view", async () => {
    const r = (await q("LLLLLneedle-after-clip"))!
    expect(r.matches).toHaveLength(1)
    expect(r.matches[0]!.start).toBeGreaterThan(8192)
    expect(slice(r).toString("utf8")).toBe("LLLLLneedle-after-clip")
    expect(VIEW).not.toContain("needle-after-clip")
    // a string present only in the model's view is not in the original
    expect((await q("[view clipped"))!.matches).toEqual([])
    // match crossing the first 8192-byte page edge
    const e = (await q("EDGE-MARK"))!
    expect(e.matches).toEqual([{ start: 8190, end: 8199 }])
    expect(slice(e).toString("utf8")).toBe("EDGE-MARK")
  })

  test("scope: other session/actor/part gets nothing; foreign hash is refused", async () => {
    const other: Ident = { session: "ses_b", actor: "main", part: "p1" }
    const otherActor: Ident = { ...A, actor: "sub1" }
    const recs = { [`${A.session}|${A.actor}|${A.part}`]: ORIGINAL }
    expect(await q(DISTINCT, { id: other, recs })).toBeUndefined()
    expect(await q(DISTINCT, { id: otherActor, recs })).toBeUndefined()
    // same text recorded under other session: only that session's own query sees it, and each sees its own bytes
    const r2 = { ...recs, [`ses_b|main|p1`]: Buffer.from(`zz${DISTINCT}`) }
    expect((await q(DISTINCT, { id: other, recs: r2 }))!.matches[0]!.start).toBe(2)
    expect((await q(DISTINCT, { recs: r2 }))!.matches[0]!.start).toBe(5)
    // identity whose pages carry a different content hash than the one requested
    const s = store(recs)
    const bad = await queryHistoryOutput({ revision: 1, contentHash: "0".repeat(64), literal: "head", start: 0, limit: 20, current: () => true, readPage: s.reader(A) })
    expect(bad).toBeUndefined()
  })

  test("exact, literal, not fuzzy", async () => {
    expect((await q("HEAD"))!.matches).toEqual([])
    expect((await q("hean"))!.matches).toEqual([])
    expect((await q("head"))!.matches).toHaveLength(1)
    const recs = { [`${A.session}|${A.actor}|${A.part}`]: Buffer.from("abc a.c a.c") }
    expect((await q("a.c", { recs }))!.matches).toEqual([{ start: 4, end: 7 }, { start: 8, end: 11 }])
    expect((await q(".*", { recs }))!.matches).toEqual([])
  })

  test("limits: query bytes 1..256, limit 1..50, output <= 51200 bytes, paging cursor", async () => {
    expect(await q("")).toBeUndefined()
    expect(await q("x".repeat(257))).toBeUndefined()
    const k = `${A.session}|${A.actor}|${A.part}`
    const at256 = "y".repeat(256)
    expect((await q(at256, { recs: { [k]: Buffer.from(at256) } }))!.matches).toHaveLength(1)
    expect(await q("\ud800")).toBeUndefined() // lone surrogate
    expect(await q("é".repeat(129))).toBeUndefined() // 258 UTF-8 bytes though 129 chars

    const many = { [k]: Buffer.from("ab".repeat(60)) }
    const l50 = (await q("ab", { recs: many, limit: 50 }))!
    expect(l50.matches).toHaveLength(50)
    expect(l50.nextOffset).toBe(100)
    expect(await q("ab", { recs: many, limit: 51 })).toBeUndefined()
    expect(await q("ab", { recs: many, limit: 0 })).toBeUndefined()
    const one = (await q("ab", { recs: many, limit: 1 }))!
    expect(one.matches).toEqual([{ start: 0, end: 2 }])
    expect(one.nextOffset).toBe(2)
    expect((await q("ab", { recs: many, limit: 1, start: 118 }))!.nextOffset).toBeNull()
    expect(await q("ab", { recs: many, start: 1000 })).toBeUndefined()
    expect(await q("é", { recs: { [k]: Buffer.from("aé") }, start: 2 })).toBeUndefined() // mid-character

    const max = Buffer.concat([Buffer.from("needle"), Buffer.alloc(51200 - 6, 0x61)])
    expect((await q("needle", { recs: { [k]: max } }))!.outputBytes).toBe(51200)
    expect(await q("needle", { recs: { [k]: Buffer.concat([max, Buffer.from("a")]) } })).toBeUndefined()
  })

  test("tampered or stale pages are refused", async () => {
    expect(await q("head", { mutate: (t) => t.replace("head", "HEAD") })).toBeUndefined() // sha mismatch
    const s = store({ [`${A.session}|${A.actor}|${A.part}`]: ORIGINAL })
    const stale = await queryHistoryOutput({ revision: 2, contentHash: s.hashOf(A), literal: "head", start: 0, limit: 20, current: () => true, readPage: s.reader(A) })
    expect(stale).toBeUndefined()
    let n = 0
    const lost = await queryHistoryOutput({ revision: 1, contentHash: s.hashOf(A), literal: "head", start: 0, limit: 20, current: () => n++ < 3, readPage: s.reader(A) })
    expect(lost).toBeUndefined()
  })
})
