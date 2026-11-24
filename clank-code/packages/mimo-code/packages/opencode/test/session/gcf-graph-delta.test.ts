import { describe, expect, test } from "bun:test"
import { applyGraphDelta, canonicalGraphJson, encodeGraphDelta, GRAPH_SCHEMA } from "../../src/session/gcf-graph-delta"
import { createHash } from "node:crypto"

// Synthetic open-clank.code-query/v1 shaped like fm-core code_query.rs CodeQueryResult.
export const hit = (n: number, extra: Record<string, unknown> = {}) => ({
  id: `sym:${n}`, kind: "function", label: `fn_${n}`, signature: `fn fn_${n}(a: u32, b: &str) -> Result<Vec<u8>, Error>`,
  source: { path: `src/mod${n % 7}.rs`, start_line: n * 3, end_line: n * 3 + 2 },
  documentation: { text: `Documentation for function number ${n}, long enough to matter.` },
  matches: [{ channel: "lexical", score: 10 - (n % 5) }], evidence_state: "verified", current_source_verified: true, ...extra,
})
export const edge = (a: number, b: number, extra: Record<string, unknown> = {}) => ({
  from: `sym:${a}`, to: `sym:${b}`, occurrence_id: `occ:${a}:${b}`, relation_kind: "calls", resolution_state: "resolved",
  source: { path: `src/mod${a % 7}.rs`, start_line: a }, ...extra,
})
export function result(hits: unknown[], edges: unknown[], over: Record<string, unknown> = {}) {
  return {
    schema: GRAPH_SCHEMA, operation: { kind: "search", query: "fn" }, receipt: { snapshot: "r1", generation: 4 },
    capabilities: [{ channel: "lexical", status: "ready" }], channels: [], hits, edges, paths: [], counts: [{ name: "hits", value: hits.length, unit: "hit", exactness: "exact" }],
    facets: {}, omissions: [], partial: false, degraded: false, reasons: [], next_cursor: null, error: null,
    cost: { inspected_records: 10, serialized_bytes: 100, token_upper_bound: 30, metadata_bytes: 5, files_bytes_read: 0, validation_bytes_read: 0 }, ...over,
  }
}
const range = (n: number) => Array.from({ length: n }, (_, i) => i)
const baseHits = range(30).map(i => hit(i)), baseEdges = range(20).map(i => edge(i, i + 1))
const base = () => result(baseHits.map(h => ({ ...h })), baseEdges.map(e => ({ ...e })))
const sha = (s: string) => createHash("sha256").update(s).digest("hex")

function roundTrip(b: unknown, n: unknown) {
  const enc = encodeGraphDelta(b, n)
  expect(enc.state).toBe("ok")
  if (enc.state !== "ok") throw new Error("unreachable")
  const dec = applyGraphDelta(b, JSON.parse(enc.text))
  expect(dec.state).toBe("ok")
  if (dec.state !== "ok") throw new Error("unreachable")
  expect(dec.canonical).toBe(canonicalGraphJson(n))
  expect(dec.root).toBe(sha(canonicalGraphJson(n)))
  expect(enc.delta.result_root).toBe(dec.root)
  return enc
}

describe("graph delta", () => {
  test("identical gives an empty delta that round-trips", () => {
    const enc = roundTrip(base(), base())
    expect(enc.delta.added).toEqual({ hits: [], edges: [] })
    expect(enc.delta.removed).toEqual({ hits: [], edges: [] })
    expect(enc.delta.changed.hits).toEqual([])
  })
  test("add, remove, changed metadata, reorder, nested evidence", () => {
    const n = base(); n.hits.push(hit(99)); n.edges.push(edge(99, 1)); roundTrip(base(), n)
    const r = base(); r.hits.splice(3, 2); r.edges.splice(0, 1); expect(roundTrip(base(), r).delta.removed.hits).toEqual(["sym:3", "sym:4"])
    const c = base(); c.hits[5] = hit(5, { signature: "fn renamed()" }); expect(roundTrip(base(), c).delta.changed.hits.length).toBe(1)
    const o = base(); o.hits.reverse(); roundTrip(base(), o)
    const ev = base(); (ev.hits[2] as any).matches[0].score = 1; (ev.edges[1] as any).witnesses = [{ k: "v" }]; (ev as any).cost.token_upper_bound = 31; (ev as any).next_cursor = "abc"
    const e = roundTrip(base(), ev); expect(e.delta.changed.envelope).toEqual({ cost: (ev as any).cost, next_cursor: "abc" })
  })
  test("envelope key removal and addition survive", () => {
    const n: any = base(); delete n.receipt; n.source_window = { start: 1 }
    const e = roundTrip(base(), n); expect(e.delta.changed.envelope_removed).toEqual(["receipt"])
  })
  test("wrong base and tampered delta are refused", () => {
    const n = base(); n.hits[1] = hit(1, { label: "x" })
    const enc = encodeGraphDelta(base(), n); if (enc.state !== "ok") throw new Error("encode")
    const other = base(); other.hits.pop()
    expect(applyGraphDelta(other, JSON.parse(enc.text))).toEqual({ state: "refused", reason: "reanchor" })
    const bad = enc.text.replace("fn_1", "fn_9")
    expect(bad).not.toBe(enc.text)
    expect(applyGraphDelta(base(), JSON.parse(bad)).state).toBe("refused")
    const d = JSON.parse(enc.text); d.result_root = sha("x"); expect(applyGraphDelta(base(), d)).toEqual({ state: "refused", reason: "reanchor" })
    d.extra = 1; expect(applyGraphDelta(base(), d).state).toBe("refused")
  })
  test("typed refusals", () => {
    const n = base(); delete (n.hits[0] as any).id
    expect(encodeGraphDelta(base(), n)).toEqual({ state: "refused", reason: "not_eligible" })
    const dup = base(); dup.hits[1] = hit(0); expect(encodeGraphDelta(base(), dup)).toEqual({ state: "refused", reason: "not_eligible" })
    expect(encodeGraphDelta(base(), { ...base(), schema: "x" })).toEqual({ state: "refused", reason: "schema_mismatch" })
    expect(encodeGraphDelta(base(), { ...base(), operation: { kind: "other" } })).toEqual({ state: "refused", reason: "query_mismatch" })
    const mini = (id: string) => ({ schema: GRAPH_SCHEMA, operation: {}, hits: [{ id }], edges: [] }), small = mini("a"), small2 = mini("b")
    expect(encodeGraphDelta(small, small2)).toEqual({ state: "refused", reason: "not_smaller" })
    const huge = result(range(400).map(i => hit(i)), []); expect(encodeGraphDelta(huge, huge)).toEqual({ state: "refused", reason: "not_eligible" })
  })
  test("property loop: apply(encode) equals next in bytes and hash", () => {
    let seed = 7; const rnd = (n: number) => (seed = (seed * 1103515245 + 12345) & 0x7fffffff) % n
    let ok = 0
    for (let i = 0; i < 50; i++) {
      const n: any = base()
      for (let k = rnd(4); k > 0; k--) n.hits.splice(rnd(n.hits.length), 1)
      for (let k = rnd(4); k > 0; k--) n.hits.splice(rnd(n.hits.length + 1), 0, hit(100 + rnd(1000) + i * 1000))
      for (let k = rnd(3); k > 0; k--) n.hits[rnd(n.hits.length)] = { ...n.hits[rnd(n.hits.length)], label: `L${rnd(99)}` }
      if (rnd(2)) n.hits.reverse()
      if (rnd(2)) n.edges.splice(rnd(n.edges.length), 1)
      if (rnd(2)) n.cost.inspected_records = rnd(999)
      const seen = new Set<string>(); n.hits = n.hits.filter((h: any) => !seen.has(h.id) && seen.add(h.id))
      const enc = encodeGraphDelta(base(), n)
      if (enc.state !== "ok") { expect(enc.reason).toBe("not_smaller"); continue }
      const dec = applyGraphDelta(base(), JSON.parse(enc.text))
      expect(dec.state === "ok" && dec.canonical === canonicalGraphJson(n) && dec.root === sha(canonicalGraphJson(n))).toBe(true)
      ok++
    }
    expect(ok).toBeGreaterThan(30)
  })
})
