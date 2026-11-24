import assert from 'node:assert/strict';
import { exportSelectionProblem, bundleStateLine, countBundleStates, wrongDialogMessage } from '../../static/js/knowledgeSnapshot.js';

let n = 0;
const t = (name, fn) => { fn(); n++; };
const enc = (v) => new TextEncoder().encode(JSON.stringify(v));

t('selection ok', () => assert.equal(exportSelectionProblem(['1', '2']), null));
t('selection empty/non-string', () => {
  assert.match(exportSelectionProblem([]), /at least one/);
  assert.match(exportSelectionProblem([1]), /at least one/);
  assert.match(exportSelectionProblem('x'), /at least one/);
});
t('selection over 20 refused with count', () => {
  assert.equal(exportSelectionProblem(Array.from({ length: 20 }, (_, i) => 'a' + i)), null);
  assert.match(exportSelectionProblem(Array.from({ length: 21 }, (_, i) => 'a' + i)), /Too many selected: 21/);
});
t('selection duplicates', () => assert.match(exportSelectionProblem(['a', 'a']), /twice/));
t('state lines', () => {
  assert.equal(bundleStateLine({ index: 0, state: 'staged' }), 'Item 1: Review draft created.');
  assert.match(bundleStateLine({ index: 2, state: 'unknown' }), /^Item 3: .*do not repeat/);
  assert.match(bundleStateLine({ index: 0, state: 'bogus' }), /invalid/);
});
t('counts by state, no success wording', () => {
  const s = countBundleStates([{ state: 'staged' }, { state: 'staged' }, { state: 'unknown' }, { state: 'not_imported' }]);
  assert.equal(s, '4 items: 2 staged, 1 unknown, 1 not imported.');
  assert.doesNotMatch(s, /success/i);
});
t('wrong dialog detection', () => {
  assert.match(wrongDialogMessage(enc({ schema: 'openclank.knowledge-snapshot.v1' })), /one snapshot/);
  assert.equal(wrongDialogMessage(enc({ schema: 'openclank.knowledge-snapshot-bundle.v1' })), null);
  assert.equal(wrongDialogMessage(new Uint8Array([0xff, 0xfe])), null);
  assert.equal(wrongDialogMessage(enc([1])), null);
});

console.log(`${n} test groups ok`);
