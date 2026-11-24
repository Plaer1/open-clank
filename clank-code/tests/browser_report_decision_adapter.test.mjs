import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeDecisionItems, normalizeDecisionValue, normalizeLegacySelections } from '../static/js/browser/reportDecisionAdapter.js';

test('legacy selected booleans map to the richer per-item decision shape', () => {
  assert.deepEqual(normalizeDecisionValue(true), { state:'selected', adoption_note:'', rejection_note:'' });
  assert.deepEqual(normalizeDecisionValue({ selected:false, note:'Not relevant' }), {
    state:'unselected', adoption_note:'', rejection_note:'Not relevant',
  });
});

test('legacy ID-only selections preserve manifest item identities and initialize notes', () => {
  const result = normalizeLegacySelections(['a'], [
    { id:'a', digest:'sha256:a' }, { id:'b', digest:'sha256:b' },
  ]);
  assert.deepEqual(result.map(item => item.value), [
    { state:'selected', adoption_note:'', rejection_note:'' },
    { state:'unselected', adoption_note:'', rejection_note:'' },
  ]);
});

test('adoption and dislike/rejection notes remain item-scoped', () => {
  const result = normalizeDecisionItems([
    { id:'a', digest:'sha256:a', value:{ state:'adopted', adoption_note:'Use this', rejection_note:'' } },
    { id:'b', digest:'sha256:b', value:{ state:'rejected', adoption_note:'', rejection_note:'Does not fit' } },
  ]);
  assert.equal(result[0].value.adoption_note, 'Use this');
  assert.equal(result[1].value.rejection_note, 'Does not fit');
});

test('malformed states, mixed legacy fields and oversized notes are rejected', () => {
  assert.throws(() => normalizeDecisionValue({ state:'adopted', selected:true }), /mixes legacy/);
  assert.throws(() => normalizeDecisionValue({ state:'unknown' }), /unsupported/);
  assert.throws(() => normalizeDecisionValue({ state:'rejected', rejection_note:'x'.repeat(2001) }), /too long/);
  assert.throws(() => normalizeLegacySelections(['missing'], [{ id:'known', digest:'sha256:a' }]), /unknown/);
});
