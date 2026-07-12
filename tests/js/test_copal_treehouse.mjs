import assert from 'node:assert/strict';
import test from 'node:test';

import { hasTreeHouseRole, moveTreeHouseItem, profileRoles, treeHouseCommandId } from '../../static/js/copal/treehouse.js';

test('role checks are explicit and do not infer instructor from learner', () => {
  const learner = { roles: ['learner'] };
  assert.equal(hasTreeHouseRole(learner, 'learner'), true);
  assert.equal(hasTreeHouseRole(learner, 'admin', 'instructor'), false);
  assert.deepEqual([...profileRoles({ roles: ['admin', 'learner'] })], ['admin', 'learner']);
});

test('command IDs are namespaced and unique for idempotent backend handling', () => {
  const first = treeHouseCommandId('course.create');
  const second = treeHouseCommandId('course.create');
  assert.match(first, /^course\.create:/);
  assert.notEqual(first, second);
});

test('ordering creates derived arrays and respects boundaries', () => {
  const source = ['a', 'b', 'c'];
  assert.deepEqual(moveTreeHouseItem(source, 'b', -1), ['b', 'a', 'c']);
  assert.deepEqual(moveTreeHouseItem(source, 'a', -1), source);
  assert.deepEqual(source, ['a', 'b', 'c']);
});
