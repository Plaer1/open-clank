import assert from 'node:assert/strict';
import { buildMenu, menuPrompt, pickOperation, pickProfile, needsProfileQuestion, profilePrompt } from '../../static/js/mediaMenuModel.js';

const p = (id, op, extra = {}) => ({ profile_id: id, label: 'L-' + id, operation: op, revision: 'r', ...extra });
const catalog = { operations: [
  { operation: 'image.generate', label: 'Generate image', profiles: [p('a', 'image.generate')] },
  { operation: 'mesh.generate', label: 'Generate 3D model', profiles: [p('b', 'mesh.generate'), p('c', 'mesh.generate')], partial: true },
  { operation: 'animation.generate', label: 'Animate sprite', profiles: [], reason: 'No configured profile can perform this operation.' },
  { operation: 'future.op', profiles: [p('z', 'future.op')] },
  { operation: 'bad.op', label: 'Bad', profiles: [p('q', 'other.op'), null, { label: 'no id' }] },
] };
const menu = buildMenu(catalog);
const by = Object.fromEntries(menu.map(i => [i.operation, i]));
assert.equal(by['image.generate'].state, 'ready'); assert.equal(needsProfileQuestion(by['image.generate']), false);
assert.equal(pickProfile(by['image.generate']).profile_id, 'a', 'one profile: no extra question');
assert.equal(by['mesh.generate'].state, 'choose'); assert.equal(pickProfile(by['mesh.generate'], '2').profile_id, 'c');
assert.match(profilePrompt(by['mesh.generate']), /^1\. L-b\n2\. L-c\nSome profiles are unavailable\.$/);
assert.throws(() => pickProfile(by['mesh.generate'], '9'), /Choose a listed profile/);
assert.equal(by['animation.generate'].state, 'unavailable'); assert.match(by['animation.generate'].reason, /No configured profile/);
assert.throws(() => pickOperation(menu, '3'), /Animate sprite is unavailable: No configured profile/);
assert.equal(by['future.op'].label, 'future.op', 'unknown operation is listed under its id');
assert.equal(by['future.op'].state, 'ready');
assert.equal(by['bad.op'].state, 'unavailable', 'profiles that do not belong to the operation or lack an id are not offered');
assert.match(menuPrompt(menu), /^1\. Generate image\n2\. Generate 3D model\n3\. Animate sprite \(unavailable: /);
assert.deepEqual(buildMenu(catalog, ['image.generate']).map(i => i.operation), ['image.generate']);
assert.deepEqual(buildMenu(null), []);
assert.throws(() => pickOperation(menu, 'x'), /Choose a listed operation/);
// no family string is ever read or produced
assert.equal(JSON.stringify(menu).includes('family'), false);
console.log('media menu model ok');
