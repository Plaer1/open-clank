// No fake-DOM helper exists in tests/js, so only the pure parts (row labelling, state text, size guard) are exercised.
import assert from 'node:assert/strict';
import { traceRows, stateText, checkFile, bytesToBase64, MAX_INGEST_BYTES, ADAPTER_ID, traceOverlayOffered } from '../../static/js/copal/traceOverlay.js';

const id = (n) => 'overlay:' + String(n).repeat(64);
const payload = (sourceState) => ({ schema:'openclank.trace-overlay.v1', state:'verified', sourceState, label:'static',
  functions:[{ id:id(1), origin:'static', name:'entry', address:4096, size:32 }],
  edges:[{ id:id(2), origin:'static_xref', from:4100, to:4128, kind:'call', resolved:true },
         { id:id(3), origin:'runtime_observed', from:4110, to:null, resolved:false }],
  hits:[{ id:id(4), origin:'runtime_observed', address:4096, count:7 }], unresolvedEdges:1 });

const rows = traceRows(payload('current'));
assert.equal(rows.length, 4);
assert.deepEqual(rows.map((r) => r.label), ['static evidence', 'static evidence (xref)', 'runtime-observed', 'runtime-observed']);
assert.ok(rows.every((r) => r.text.startsWith('[' + r.label + ']')), 'every row visibly labelled');
assert.ok(rows[2].text.includes('to=unresolved'));
assert.ok(rows.every((r) => !/^(file|decl|occ):/.test(r.id) && !/(^|\s)(file|decl|occ):/.test(r.text)), 'no canonical graph ids');

assert.match(stateText('historical'), /different source bytes/);
assert.match(stateText('unchecked'), /Not verified/);
assert.match(stateText('current'), /current source/);
assert.equal(traceRows(payload('historical')).length, 4);

for (const bad of [null, { ...payload('current'), schema:'x' }, { ...payload('current'), sourceState:'bogus' },
  { ...payload('current'), functions:[{ id:'file:abc', origin:'static' }] },
  { ...payload('current'), hits:[{ id:id(9), origin:'mystery' }] }]) assert.throws(() => traceRows(bad));

let read = false;
const file = (size, name = 'a.jsonl') => ({ size, name, arrayBuffer() { read = true; return new ArrayBuffer(0); } });
assert.throws(() => checkFile(file(MAX_INGEST_BYTES + 1)), /larger than 1 MiB/);
assert.equal(read, false, 'refused before read');
assert.throws(() => checkFile(file(0)));
assert.throws(() => checkFile(file(10, 'a.db')));
checkFile(file(MAX_INGEST_BYTES));
assert.equal(ADAPTER_ID, 'fixture_jsonl');
assert.equal(bytesToBase64(new Uint8Array([0, 255, 97])), Buffer.from([0, 255, 97]).toString('base64'));
console.log('trace_overlay ok');

// The overlay is offered only when the server lists an adapter with a real producer (none today).
assert.equal(traceOverlayOffered({ ingestAdapters: [] }), false);
assert.equal(traceOverlayOffered({}), false);
assert.equal(traceOverlayOffered(null), false);
assert.equal(traceOverlayOffered({ ingestAdapters: ['other'] }), false);
assert.equal(traceOverlayOffered({ ingestAdapters: [ADAPTER_ID] }), true);
