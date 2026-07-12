import assert from 'node:assert/strict';
import {
  DEFAULT_DAY_WIDTH,
  MAX_DAY_WIDTH,
  MIN_DAY_WIDTH,
  WARM_HISTORY_DAYS,
  addDays,
  daysBetween,
  dragPatch,
  eventLayout,
  formatLocalDate,
  glyphFor,
  manipulationGate,
  parseLocalDate,
} from '../../static/js/copal/planningModel.js';

assert.equal(MIN_DAY_WIDTH, 8);
assert.equal(DEFAULT_DAY_WIDTH, 18);
assert.equal(MAX_DAY_WIDTH, 56);
assert.equal(WARM_HISTORY_DAYS, 60);

const dstStart = parseLocalDate('2026-03-07');
assert.equal(formatLocalDate(addDays(dstStart, 2)), '2026-03-09');
assert.equal(daysBetween('2026-03-07', '2026-03-09'), 2);
assert.equal(formatLocalDate(addDays('2024-02-28', 1)), '2024-02-29');
assert.equal(formatLocalDate(addDays('2026-12-31', 1)), '2027-01-01');

const hard = { startDate:'2026-07-10', dueDate:'2026-07-12' };
assert.deepEqual(dragPatch(hard, 'move', 2), { startDate:'2026-07-12', dueDate:'2026-07-14' });
assert.deepEqual(dragPatch(hard, 'left', 1), { startDate:'2026-07-11' });
assert.deepEqual(dragPatch(hard, 'right', -1), { dueDate:'2026-07-11' });
assert.equal(manipulationGate(hard, {}).movable, true);
assert.equal(manipulationGate({ ...hard, startDate:'FUZZY', fuzzy:{ anchorStart:'2026-07-10' } }, {}).movable, false);
assert.equal(manipulationGate(hard, { special:true }).resizeRight, false);

const fuzzy = eventLayout({ startDate:'FUZZY', fuzzy:{ anchorStart:'2026-07-10', anchorEnd:'2026-07-15' } }, parseLocalDate('2026-01-01'));
assert.equal(formatLocalDate(fuzzy.start), '2026-07-10');
assert.equal(formatLocalDate(fuzzy.end), '2026-07-15');
assert.equal(glyphFor('car'), '🚗');
assert.equal(glyphFor('🦕'), '🦕');

console.log('copal planning helpers: ok');
