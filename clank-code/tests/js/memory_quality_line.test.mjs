// memory.js needs a browser to import; the pure function is sliced out of the source text.
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const src = readFileSync(new URL('../../static/js/memory.js', import.meta.url), 'utf8')
const start = src.indexOf('export function memoryQualityLine(')
assert.ok(start >= 0)
const body = src.slice(start, src.indexOf('\n}\n', start) + 2).replace(/^export /, '')
const line = new Function(`${body}; return memoryQualityLine`)()

const guarded = { basis: 'scoped_visible_live',
  counts: { by_kind: { fact: 2, persona: 1, task: 1, note: 1 }, by_tier: { raw: 0, curated: 5, candidates_pending: 1 }, candidates_pending: 1 },
  graph: { visible_cues: 3 } }
assert.equal(line(guarded), 'Memories 5 · Awaiting review 1 · Raw 0 · Kinds fact 2, note 1, persona 1 · Index cues 3')

const legacy = { basis: 'scoped_visible_live',
  counts: { by_kind: { persona: 1 }, by_tier: { raw: 4, curated: 2, candidates_pending: 0 }, candidates_pending: 0 },
  graph: { visible_cues: 5 } }
assert.equal(line(legacy), 'Memories 2 · Awaiting review 0 · Raw 4 · Kinds persona 1 · Index cues 5')

// old-shape and unknown keys: nothing invented
const old = { raw: 9, candidates: 8, curated: 7, quarantined: 6, graph: { cues: 4 } }
assert.equal(line(old), 'Memory quality unavailable.')
assert.ok(!/rebuild|healthy/.test(line(guarded) + line(legacy) + line(old)))
assert.equal(line({ counts: { by_tier: { curated: 1 } }, graph: { integrity_ok: false } }), 'Memories 1 · Index needs rebuild')
assert.equal(line({ counts: { by_tier: { curated: 1 } }, graph: { integrity_ok: true } }), 'Memories 1 · Index healthy')

// null / malformed
for (const bad of [null, undefined, 5, 'x', [], {}]) assert.equal(line(bad), 'Memory quality unavailable.')

// zero only when the key is present
assert.equal(line({ counts: { by_tier: { curated: 0 } } }), 'Memories 0')
assert.equal(line({ counts: { by_tier: {} } }), 'Memory quality unavailable.')
assert.equal(line({ counts: { by_tier: { curated: 'x', raw: -1 } } }), 'Memory quality unavailable.')
console.log('ok')
