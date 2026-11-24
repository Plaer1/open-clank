import { describe, expect, test } from "bun:test"
import { existsSync } from "node:fs"
import { homedir } from "node:os"
import { encodeGcfAdmission, gcfGraphReferencesAdmission, gcfSameRequestRowsAdmission } from "../../src/session/gcf-policy"
import { createGcfEncoder } from "../../src/session/gcf-native"
import { deriveSameRequestRows } from "../../src/session/gcf-same-request-rows"
import { applyGraphDelta, canonicalGraphJson, GRAPH_SCHEMA, parseGraphWrapper } from "../../src/session/gcf-graph-delta"

const model = { providerID: "p", modelID: "m" }
const rev = "a".repeat(64)
const base = { version: 1, sessionID: "s", rootOperationID: "r", generation: 3, settingsRevision: rev, requested: true, providerID: "p", modelID: "m" }
const enc = (v: unknown) => encodeGcfAdmission(v, "s", "r", model)

describe("graphReferences admission key", () => {
  test("today's shapes validate and re-encode exactly as before", () => {
    // Frozen from the code before graphReferences existed.
    expect(enc(base)).toBe('{"version":1,"sessionID":"s","rootOperationID":"r","generation":3,"settingsRevision":"' + rev + '","requested":true,"providerID":"p","modelID":"m"}')
    expect(enc({ ...base, memoize: true })).toBe('{"version":1,"sessionID":"s","rootOperationID":"r","generation":3,"settingsRevision":"' + rev + '","requested":true,"memoize":true,"providerID":"p","modelID":"m"}')
    expect(enc({ ...base, memoize: false, sameRequestRows: true })).toBe('{"version":1,"sessionID":"s","rootOperationID":"r","generation":3,"settingsRevision":"' + rev + '","requested":true,"memoize":false,"sameRequestRows":true,"providerID":"p","modelID":"m"}')
    expect(enc({ ...base, sameRequestRows: false })).toBeUndefined()
    expect(enc({ ...base, bogus: true })).toBeUndefined()
    expect(enc({ ...base, sessionID: "x" })).toBeUndefined()
  })
  test("graphReferences must be exactly true; absent is unchanged", () => {
    const g = enc({ ...base, graphReferences: true })!
    expect(g).toContain('"graphReferences":true')
    expect(enc({ ...base, graphReferences: false })).toBeUndefined()
    expect(enc({ ...base, graphReferences: 1 })).toBeUndefined()
    expect(enc({ ...base, graphReferences: "true" })).toBeUndefined()
    expect(enc({ ...base, sameRequestRows: true, graphReferences: true })).toContain('"sameRequestRows":true,"graphReferences":true')
  })
  test("selectors are independent", () => {
    const only = enc({ ...base, graphReferences: true }), rows = enc({ ...base, sameRequestRows: true }), none = enc(base)
    expect(gcfGraphReferencesAdmission(only, "s", "r", model)).toEqual({ version: 1, settingsRevision: rev })
    expect(gcfSameRequestRowsAdmission(only, "s", "r", model)).toBeUndefined()
    expect(gcfGraphReferencesAdmission(rows, "s", "r", model)).toBeUndefined()
    expect(gcfGraphReferencesAdmission(none, "s", "r", model)).toBeUndefined()
    expect(gcfGraphReferencesAdmission(undefined, "s", "r", model)).toBeUndefined()
    expect(gcfGraphReferencesAdmission(only, "s", undefined, model)).toBeUndefined()
    expect(gcfGraphReferencesAdmission(only, "other", "r", model)).toBeUndefined()
  })
})

// Real codec: the previous team's addon, loaded read-only through the engine's own encoder owner.
const ADDON = `${homedir()}/.cache/openclank-devtools/gcf-stream-2026-11-08/openclank-gcf.node`
const hit = (n: number, extra: Record<string, unknown> = {}) => ({ id: `sym:${n}`, kind: "function", label: `fn_${n}`, signature: `fn fn_${n}(a: u32, b: &str) -> Result<Vec<u8>, Error>`, source: { path: `src/mod${n % 7}.rs`, start_line: n * 3 }, documentation: { text: `Documentation for function ${n}, long enough to matter.` }, matches: [{ channel: "lexical", score: 3 }], evidence_state: "verified", current_source_verified: true, ...extra })
const graph = (hits: unknown[]) => ({ schema: GRAPH_SCHEMA, operation: { kind: "search", query: "fn" }, receipt: { snapshot: "r1", generation: 4 }, hits, edges: [], paths: [], facets: {}, omissions: [], partial: false, degraded: false, reasons: [], next_cursor: null, error: null, cost: { inspected_records: 10 } })
const wrap = (g: unknown) => JSON.stringify({ output: JSON.stringify(g), exit_code: 0 })

describe("real native codec", () => {
  test.skipIf(!existsSync(ADDON))("encode -> decode -> applyGraphDelta reproduces the later result exactly", () => {
    const mod = { exports: {} } as NodeModule
    process.dlopen(mod, ADDON)
    const n = mod.exports as any
    const artifact = createGcfEncoder(n.encodeV1, n.codecInfoV1().codecVersion, "test", n.decodeV1, n.encodePreviewV1) as any
    const A = graph(Array.from({ length: 30 }, (_, i) => hit(i))), B = graph(Array.from({ length: 30 }, (_, i) => (i === 4 ? hit(4, { label: "changed" }) : hit(i))))
    const body = JSON.stringify({ model: "m", stream: true, messages: [{ role: "tool", tool_call_id: "c1", content: wrap(A) }, { role: "tool", tool_call_id: "c2", content: wrap(B) }] })
    const frames = [{ callID: "c1", frame: wrap(A), sourceJSON: wrap(A) }, { callID: "c2", frame: wrap(B), sourceJSON: wrap(B) }]
    const d = deriveSameRequestRows(body, frames, artifact, "m", { graphReferences: true })
    expect(d).toBeDefined()
    expect(Buffer.byteLength(d!.body)).toBeLessThan(Buffer.byteLength(body))
    const content: string = JSON.parse(d!.body).messages[1].content
    const frame = content.slice(content.indexOf("\nGCF profile=generic\n") + 1)
    const decoded = artifact.decode(Buffer.from(frame), "gcf-generic-v3-safe-integers-v1")
    expect(decoded.state).toBe("decoded")
    const applied = applyGraphDelta(parseGraphWrapper(wrap(A))!.value, JSON.parse(decoded.output.toString("utf8")))
    expect(applied.state).toBe("ok")
    if (applied.state === "ok") expect(applied.canonical).toBe(canonicalGraphJson(B))
  })
})
