import assert from 'node:assert/strict';
import test from 'node:test';

import { assignEventLanes } from '../../static/js/copal/timeline.js';

test('assigns inclusive overlaps to stable child lanes', () => {
  const result = assignEventLanes([
    { stableId: 'b', startDay: 1, endDay: 3 },
    { stableId: 'a', startDay: 1, endDay: 1 },
    { stableId: 'c', startDay: 3, endDay: 4 },
    { stableId: 'd', startDay: 4, endDay: 4 },
    { stableId: 'e', startDay: 5, endDay: 5 },
  ]);

  assert.equal(result.laneCount, 2);
  assert.deepEqual(
    Object.fromEntries(result.items.map((item) => [item.stableId, item.lane])),
    { a: 0, b: 1, c: 0, d: 1, e: 0 },
  );
});

test('normalizes reversed and missing ranges without mutating input', () => {
  const source = [
    { stableId: 'reversed', startDay: 8, endDay: 2 },
    { stableId: 'missing', startDay: 9 },
  ];
  const result = assignEventLanes(source);

  assert.equal(result.laneCount, 1);
  assert.equal(result.items[0].endDay, 8);
  assert.equal(source[0].endDay, 2);
  assert.equal(Object.hasOwn(source[0], 'lane'), false);
});

test('ordering is deterministic for same-start events', () => {
  const ids = assignEventLanes([
    { stableId: 'z', startDay: 2, endDay: 2 },
    { stableId: 'a', startDay: 2, endDay: 2 },
  ]).items.map((item) => item.stableId);
  assert.deepEqual(ids, ['a', 'z']);
});
