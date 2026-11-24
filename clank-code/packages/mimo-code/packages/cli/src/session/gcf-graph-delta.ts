/** Pure Code Graph delta: no I/O, no state. Bases are always caller-proven; any doubt is a typed refusal. */
import { createHash } from 'node:crypto'

export const GRAPH_SCHEMA = 'open-clank.code-query/v1'
export const GRAPH_DELTA_SCHEMA = 'open-clank.gcf-graph-delta/v1'
// Same bounds as gcf-row-delta.ts: 51200 bytes per canonical result, 102400 per pair.
const MAX_RESULT = 51200, MAX_PAIR = 102400, MAX_DEPTH = 64

type Json = null | boolean | number | string | Json[] | { [key: string]: Json }
type Obj = { [key: string]: Json }
export type GraphDelta = {
  schema: string; base_root: string; result_root: string
  added: { hits: Obj[]; edges: Obj[] }
  removed: { hits: string[]; edges: string[] }
  changed: { hits: Obj[]; edges: Obj[]; order: { hits?: string[]; edges?: string[] }; envelope: Obj; envelope_removed: string[] }
}
export type GraphRefusalReason = 'not_eligible' | 'not_smaller' | 'schema_mismatch' | 'query_mismatch' | 'reanchor' | 'invalid'
export type GraphRefusal = { state: 'refused'; reason: GraphRefusalReason }
const refuse = (reason: GraphRefusalReason): GraphRefusal => ({ state: 'refused', reason })
const bytes = (s: string) => Buffer.byteLength(s, 'utf8')
const sha = (s: string) => createHash('sha256').update(s, 'utf8').digest('hex')
const plain = (v: unknown): v is Obj => !!v && typeof v === 'object' && !Array.isArray(v)

/** Deterministic form: object keys sorted by UTF-16 order, array order kept (hits/edges order is the result's ranking). */
export function canonicalGraphJson(v: unknown, depth = 0): string {
  if (depth > MAX_DEPTH) throw new Error('too deep')
  if (v === null || typeof v === 'boolean' || typeof v === 'string') return JSON.stringify(v)
  if (typeof v === 'number') { if (!Number.isFinite(v)) throw new Error('number'); return JSON.stringify(v) }
  if (Array.isArray(v)) return '[' + v.map(x => canonicalGraphJson(x, depth + 1)).join(',') + ']'
  if (plain(v)) return '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canonicalGraphJson(v[k], depth + 1)).join(',') + '}'
  throw new Error('not json')
}

type Model = { obj: Obj; hits: [string, Obj][]; edges: [string, Obj][]; canonical: string; root: string }
const str = (v: Json | undefined): string | undefined => typeof v === 'string' && v && bytes(v) <= 512 ? v : undefined

/** Identity: hit.id; edge = (occurrence_id, from, to, relation_kind). Anything else is not eligible. */
function keyed(items: Json | undefined, keyOf: (o: Obj) => string | undefined): [string, Obj][] | undefined {
  if (!Array.isArray(items)) return
  const seen = new Set<string>(), out: [string, Obj][] = []
  for (const item of items) {
    const key = plain(item) ? keyOf(item) : undefined
    if (key === undefined || seen.has(key)) return
    seen.add(key); out.push([key, item as Obj])
  }
  return out
}
const hitKey = (o: Obj) => str(o.id)
const edgeKey = (o: Obj) => {
  const parts = [o.occurrence_id, o.from, o.to, o.relation_kind].map(str)
  return parts.every(p => p !== undefined) ? JSON.stringify(parts) : undefined
}

function model(value: unknown): Model | GraphRefusal {
  try {
    if (!plain(value)) return refuse('not_eligible')
    if (value.schema !== GRAPH_SCHEMA) return refuse('schema_mismatch')
    const canonical = canonicalGraphJson(value)
    if (bytes(canonical) > MAX_RESULT) return refuse('not_eligible')
    const hits = keyed(value.hits, hitKey), edges = keyed(value.edges, edgeKey)
    if (!hits || !edges) return refuse('not_eligible')
    return { obj: value, hits, edges, canonical, root: sha(canonical) }
  } catch { return refuse('not_eligible') }
}
const isRefusal = (m: Model | GraphRefusal): m is GraphRefusal => 'state' in m

function diffArray(base: [string, Obj][], next: [string, Obj][]) {
  const baseMap = new Map(base), nextKeys = new Set(next.map(([k]) => k))
  const removed = base.filter(([k]) => !nextKeys.has(k)).map(([k]) => k)
  const added = next.filter(([k]) => !baseMap.has(k)).map(([, o]) => o)
  const changed = next.filter(([k, o]) => baseMap.has(k) && canonicalGraphJson(baseMap.get(k)) !== canonicalGraphJson(o)).map(([, o]) => o)
  const natural = [...base.filter(([k]) => nextKeys.has(k)).map(([k]) => k), ...next.filter(([k]) => !baseMap.has(k)).map(([k]) => k)]
  const order = next.map(([k]) => k)
  return { removed, added, changed, order: natural.every((k, i) => k === order[i]) ? undefined : order }
}

export function graphDeltaText(delta: GraphDelta): string { return canonicalGraphJson(delta) }

export function encodeGraphDelta(base: unknown, next: unknown): { state: 'ok'; delta: GraphDelta; text: string } | GraphRefusal {
  const b = model(base), n = model(next)
  if (isRefusal(b)) return b
  if (isRefusal(n)) return n
  try {
    if (bytes(b.canonical) + bytes(n.canonical) > MAX_PAIR) return refuse('not_eligible')
    if (canonicalGraphJson(b.obj.operation) !== canonicalGraphJson(n.obj.operation)) return refuse('query_mismatch')
    const h = diffArray(b.hits, n.hits), e = diffArray(b.edges, n.edges)
    const envelope: Obj = {}, envelopeRemoved: string[] = []
    const rest = (o: Obj) => Object.keys(o).filter(k => k !== 'hits' && k !== 'edges')
    const baseRest = new Set(rest(b.obj))
    for (const k of rest(n.obj)) {
      if (!baseRest.has(k) || canonicalGraphJson(b.obj[k]) !== canonicalGraphJson(n.obj[k])) Object.defineProperty(envelope, k, { value: n.obj[k], enumerable: true, writable: true, configurable: true })
    }
    const nextRest = new Set(rest(n.obj))
    for (const k of baseRest) if (!nextRest.has(k)) envelopeRemoved.push(k)
    const order: GraphDelta['changed']['order'] = {}
    if (h.order) order.hits = h.order
    if (e.order) order.edges = e.order
    const delta: GraphDelta = {
      schema: GRAPH_DELTA_SCHEMA, base_root: b.root, result_root: n.root,
      added: { hits: h.added, edges: e.added }, removed: { hits: h.removed, edges: e.removed },
      changed: { hits: h.changed, edges: e.changed, order, envelope, envelope_removed: envelopeRemoved.sort() },
    }
    const text = graphDeltaText(delta)
    if (bytes(text) >= bytes(n.canonical)) return refuse('not_smaller')
    // Self-check: the delta must reproduce NEXT before it is ever offered.
    const applied = applyGraphDelta(base, JSON.parse(text))
    if (applied.state !== 'ok' || applied.root !== n.root) return refuse('reanchor')
    return { state: 'ok', delta, text }
  } catch { return refuse('invalid') }
}

const exact = (o: Obj, keys: string[]) => { const k = Object.keys(o); return k.length === keys.length && keys.every(x => k.includes(x)) }
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(x => typeof x === 'string')
const objects = (v: unknown): v is Obj[] => Array.isArray(v) && v.every(plain)

function applyArray(base: [string, Obj][], keyOf: (o: Obj) => string | undefined, removed: string[], added: Obj[], changed: Obj[], order: string[] | undefined): Obj[] | undefined {
  const map = new Map(base)
  const used = new Set<string>()
  for (const k of removed) { if (!map.delete(k)) return }
  for (const o of changed) { const k = keyOf(o); if (k === undefined || !map.has(k) || used.has(k)) return; used.add(k); map.set(k, o) }
  for (const o of added) { const k = keyOf(o); if (k === undefined || map.has(k)) return; map.set(k, o) }
  if (!order) return [...map.values()]
  if (new Set(order).size !== order.length || order.length !== map.size || !order.every(k => map.has(k))) return
  return order.map(k => map.get(k)!)
}

/** Verifies base_root before applying and result_root after; reconstruction is exact in canonical form. */
export function applyGraphDelta(base: unknown, delta: unknown): { state: 'ok'; result: Obj; canonical: string; root: string } | GraphRefusal {
  const b = model(base)
  if (isRefusal(b)) return b
  try {
    if (!plain(delta) || !exact(delta, ['schema', 'base_root', 'result_root', 'added', 'removed', 'changed']) || delta.schema !== GRAPH_DELTA_SCHEMA ||
        typeof delta.base_root !== 'string' || typeof delta.result_root !== 'string' || !/^[a-f0-9]{64}$/.test(delta.result_root) ||
        !plain(delta.added) || !exact(delta.added, ['hits', 'edges']) || !objects(delta.added.hits) || !objects(delta.added.edges) ||
        !plain(delta.removed) || !exact(delta.removed, ['hits', 'edges']) || !strings(delta.removed.hits) || !strings(delta.removed.edges) ||
        !plain(delta.changed) || !exact(delta.changed, ['hits', 'edges', 'order', 'envelope', 'envelope_removed']) ||
        !objects(delta.changed.hits) || !objects(delta.changed.edges) || !plain(delta.changed.order) || !plain(delta.changed.envelope) || !strings(delta.changed.envelope_removed)) return refuse('invalid')
    const changed = delta.changed, order = changed.order as Obj, envelope = changed.envelope as Obj, removedKeys = changed.envelope_removed as string[]
    if (Object.keys(order).some(k => k !== 'hits' && k !== 'edges') || (order.hits !== undefined && !strings(order.hits)) || (order.edges !== undefined && !strings(order.edges))) return refuse('invalid')
    if (delta.base_root !== b.root) return refuse('reanchor')
    const hits = applyArray(b.hits, hitKey, delta.removed.hits, delta.added.hits, delta.changed.hits, order.hits as string[] | undefined)
    const edges = applyArray(b.edges, edgeKey, delta.removed.edges, delta.added.edges, delta.changed.edges, order.edges as string[] | undefined)
    if (!hits || !edges) return refuse('reanchor')
    const entries = Object.entries(b.obj).filter(([k]) => !removedKeys.includes(k) && !Object.hasOwn(envelope, k))
    const result = Object.fromEntries([...entries, ...Object.entries(envelope), ['hits', hits], ['edges', edges]]) as Obj
    const checked = model(result)
    if (isRefusal(checked) || checked.root !== delta.result_root) return refuse('reanchor')
    return { state: 'ok', result, canonical: checked.canonical, root: checked.root }
  } catch { return refuse('invalid') }
}

/** Recognise a complete Code Graph tool wrapper `{output, exit_code:0}` the way graph-view does; never throws. */
export function parseGraphWrapper(original: string): { order: string[]; inner: string; value: Obj } | undefined {
  try {
    const wrapper = JSON.parse(original)
    if (!plain(wrapper) || Object.keys(wrapper).length !== 2 || typeof wrapper.output !== 'string' || wrapper.exit_code !== 0) return
    const value = JSON.parse(wrapper.output)
    if (!plain(value) || value.schema !== GRAPH_SCHEMA || value.error !== null || value.partial !== false || value.degraded !== false) return
    return { order: Object.keys(wrapper), inner: wrapper.output, value }
  } catch { return }
}
