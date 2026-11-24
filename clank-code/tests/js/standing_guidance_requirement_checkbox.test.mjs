import assert from 'node:assert/strict';
import test from 'node:test';

import { withRequirement, blocksPublicLinks } from '../../static/js/standingGuidanceRequirement.js';

const REQ = { version: 1, effect: 'publish_file', deny: { audience: 'public' } };

test('unchecked: request bodies are byte-identical to the unchecked flow', () => {
  const preview = { decision: 'approve' };
  const apply = { decision: 'approve', selector: { scope: 's', curated_id: '1' }, confirmed: true };
  assert.equal(JSON.stringify(withRequirement(preview, false)), JSON.stringify({ decision: 'approve' }));
  assert.equal(JSON.stringify(withRequirement(apply, false)), '{"decision":"approve","selector":{"scope":"s","curated_id":"1"},"confirmed":true}');
  assert.ok(!('requirement' in withRequirement(preview, false)));
});

test('checked: exact requirement object, rest of the body unchanged, input not mutated', () => {
  const preview = { decision: 'approve' };
  const out = withRequirement(preview, true);
  assert.deepEqual(out, { decision: 'approve', requirement: REQ });
  assert.deepEqual(preview, { decision: 'approve' });
  assert.deepEqual(Object.keys(out.requirement.deny), ['audience']);
  assert.deepEqual(withRequirement({ decision: 'approve', selector: { a: 1 }, confirmed: true }, true).requirement, REQ);
});

test('label shown only for an item carrying exactly the requirement', () => {
  assert.equal(blocksPublicLinks({ requirement: REQ }), true);
  assert.equal(blocksPublicLinks({}), false);
  assert.equal(blocksPublicLinks(null), false);
  assert.equal(blocksPublicLinks({ requirement: { ...REQ, effect: 'x' } }), false);
  assert.equal(blocksPublicLinks({ requirement: { ...REQ, deny: { audience: 'owner' } } }), false);
  assert.equal(blocksPublicLinks({ requirement: { ...REQ, deny: { audience: 'public', more: 1 } } }), false);
  assert.equal(blocksPublicLinks({ requirement: { ...REQ, version: 2 } }), false);
});
