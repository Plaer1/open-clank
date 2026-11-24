import { describe, expect, test } from "bun:test"
import { deriveSameRequestRows, type SameRequestFrame } from "../../src/session/gcf-same-request-rows"
import { applyGraphDelta, canonicalGraphJson, GRAPH_SCHEMA, parseGraphWrapper } from "../../src/session/gcf-graph-delta"

const hit = (n: number, extra: Record<string, unknown> = {}) => ({
  id: `sym:${n}`, kind: "function", label: `fn_${n}`, signature: `fn fn_${n}(a: u32, b: &str) -> Result<Vec<u8>, Error>`,
  source: { path: `src/mod${n % 7}.rs`, start_line: n * 3 }, documentation: { text: `Documentation for function number ${n}, long enough to matter.` },
  matches: [{ channel: "lexical", score: 10 - (n % 5) }], evidence_state: "verified", current_source_verified: true, ...extra,
})
const graph = (hits: unknown[], op: unknown = { kind: "search", query: "fn" }) => ({
  schema: GRAPH_SCHEMA, operation: op, receipt: { snapshot: "r1", generation: 4 }, hits, edges: [], paths: [], facets: {}, omissions: [], partial: false, degraded: false,
  reasons: [], next_cursor: null, error: null, cost: { inspected_records: 10, serialized_bytes: 100, token_upper_bound: 30 },
})
const range = (n: number) => Array.from({ length: n }, (_, i) => i)
const A = () => graph(range(30).map(i => hit(i)))
const B = () => graph(range(30).map(i => (i === 4 ? hit(4, { label: "changed" }) : hit(i))))
const C = () => graph(range(30).map(i => (i === 9 ? hit(9, { label: "again" }) : i === 4 ? hit(4, { label: "changed" }) : hit(i))))
const wrap = (g: unknown) => JSON.stringify({ output: JSON.stringify(g), exit_code: 0 })

// Fake codec: the frame is the payload between a profile line and a newline.
const available = { availability: () => "available", decodeAvailability: () => "available" }
const codec = (over: Record<string, unknown> = {}) => ({
  ...available,
  identity: () => "fake",
  encode: (input: Uint8Array) => { const out = Buffer.from(`GCF profile=generic\n${Buffer.from(input).toString("utf8")}\n`); return { state: "encoded", output: out, inputBytes: input.byteLength, outputBytes: out.byteLength } },
  decode: (input: Uint8Array) => { const s = Buffer.from(input).toString("utf8"); return s.startsWith("GCF profile=generic\n") ? { state: "decoded", output: Buffer.from(s.slice(20, -1)) } : { state: "refused", reason: "invalid" } },
  ...over,
}) as any

const frame = (callID: string, source: string): SameRequestFrame => ({ callID, frame: source, sourceJSON: source })
function body(contents: [string, string][], extra: unknown[] = []) {
  return JSON.stringify({ model: "m", stream: true, messages: [{ role: "user", content: "q" }, ...contents.flatMap(([id, c]) => [
    { role: "assistant", content: null, tool_calls: [{ id, type: "function", function: { name: "search_code", arguments: "{}" } }], cache_control: { type: "ephemeral" } },
    { role: "tool", tool_call_id: id, content: c }]), ...extra] })
}
const run = (b: string, frames: SameRequestFrame[], opts?: { graphReferences?: boolean; rows?: boolean }, art = codec()) => deriveSameRequestRows(b, frames, art, "m", opts)
const [a, b2, c2] = [wrap(A()), wrap(B()), wrap(C())]
const three = () => ({ b: body([["c1", a], ["c2", b2], ["c3", c2]]), frames: [frame("c1", a), frame("c2", b2), frame("c3", c2)] })
const two = () => ({ b: body([["c1", a], ["c2", b2]]), frames: [frame("c1", a), frame("c2", b2)] })

describe("same-request graph references", () => {
  test("bit absent or false leaves the body untouched", () => {
    const { b, frames } = two()
    expect(run(b, frames)).toBeUndefined()
    expect(run(b, frames, {})).toBeUndefined()
    expect(run(b, frames, { graphReferences: false })).toBeUndefined()
  })
  test("bit true replaces the later frame; helper reproduces it", () => {
    const { b, frames } = two()
    const d = run(b, frames, { graphReferences: true })!
    expect(d.baseCallID).toBe("c1"); expect(d.targetCallID).toBe("c2")
    expect(Buffer.byteLength(d.body)).toBeLessThan(Buffer.byteLength(b))
    const before = JSON.parse(b), after = JSON.parse(d.body)
    expect(after.messages.length).toBe(before.messages.length)
    after.messages.forEach((m: any, i: number) => { if (!(m.role === "tool" && m.tool_call_id === "c2")) expect(m).toEqual(before.messages[i]); else expect(m.tool_call_id).toBe("c2") })
    const content: string = after.messages.find((m: any) => m.tool_call_id === "c2").content
    expect(content.startsWith("[Experimental same-request graph delta.")).toBe(true)
    const payload = content.slice(content.indexOf("\nGCF profile=generic\n") + 20, -1)
    const applied = applyGraphDelta(parseGraphWrapper(a)!.value, JSON.parse(payload))
    expect(applied.state).toBe("ok")
    if (applied.state === "ok") expect(applied.canonical).toBe(canonicalGraphJson(B()))
  })
  test("GCF-framed base is witnessed by decode", () => {
    const inner = JSON.stringify(A()), framed = `[explain]\nGCF profile=generic\n${inner}\n`
    const frames = [{ callID: "c1", frame: framed, sourceJSON: a }, frame("c2", b2)]
    expect(run(body([["c1", framed], ["c2", b2]]), frames, { graphReferences: true })?.targetCallID).toBe("c2")
    const lying = [{ callID: "c1", frame: framed.replace("fn_1", "fn_9"), sourceJSON: a }, frame("c2", b2)]
    expect(run(body([["c1", lying[0]!.frame], ["c2", b2]]), lying, { graphReferences: true })).toBeUndefined()
  })
  test("altered base, compacted base, not smaller, unavailable codec stay raw", () => {
    const { b, frames } = two()
    expect(run(b.replace("fn_1", "fn_X"), frames, { graphReferences: true })).toBeUndefined()
    const compacted = body([["c1", "[Old tool result content cleared]"], ["c2", b2]])
    expect(run(compacted, [frame("c2", b2), frame("c9", JSON.stringify({ output: "x", exit_code: 0 }))], { graphReferences: true })).toBeUndefined()
    const mini = (id: string) => wrap({ schema: GRAPH_SCHEMA, operation: {}, hits: [{ id }], edges: [] })
    expect(run(body([["c1", mini("a")], ["c2", mini("b")]]), [frame("c1", mini("a")), frame("c2", mini("b"))], { graphReferences: true })).toBeUndefined()
    expect(run(b, frames, { graphReferences: true }, codec({ availability: () => "unavailable" }))).toBeUndefined()
    expect(run(b, frames, { graphReferences: true }, codec({ encode: (i: Uint8Array) => ({ state: "bypass", reason: "unavailable", inputBytes: i.byteLength, outputBytes: i.byteLength }) }))).toBeUndefined()
    expect(run(b, frames, { graphReferences: true }, codec({ decode: () => ({ state: "refused", reason: "invalid" }) }))).toBeUndefined()
  })
  test("three results: later ones use a full earlier frame, never a delta frame", () => {
    const { b, frames } = three()
    const d = run(b, frames, { graphReferences: true })!
    const msgs = JSON.parse(d.body).messages.filter((m: any) => m.role === "tool")
    expect(msgs[0].content).toBe(a)
    for (const m of msgs.slice(1)) expect(m.content).toContain('earlier full tool result with call ID "c1"')
    // B unrelated (different query): C may use only a full frame (A) and B stays raw.
    const other = wrap(graph(range(30).map(i => hit(i + 500)), { kind: "other" }))
    const d2 = run(body([["c1", a], ["c2", other], ["c3", c2]]), [frame("c1", a), frame("c2", other), frame("c3", c2)], { graphReferences: true })!
    const m2 = JSON.parse(d2.body).messages.filter((m: any) => m.role === "tool")
    expect(m2[1].content).toBe(other); expect(m2[2].content).toContain('call ID "c1"')
  })
  test("rows:false keeps the row path off while graphs still work", () => {
    const rows = (v: number) => JSON.stringify(Array.from({ length: 40 }, (_, i) => ({ id: `r${i}`, name: `name ${i}`, v: i === 3 ? v : 0 })))
    const rb = body([["r1", rows(1)], ["r2", rows(2)]]), rf = [frame("r1", rows(1)), frame("r2", rows(2))]
    expect(run(rb, rf, { graphReferences: false, rows: true })?.targetCallID).toBe("r2")
    expect(run(rb, rf, { graphReferences: true, rows: false })).toBeUndefined()
    const { b, frames } = two()
    expect(run(b, frames, { graphReferences: true, rows: false })?.targetCallID).toBe("c2")
  })
})
