import assert from 'node:assert/strict';
import {
  DEFAULT_DAY_WIDTH,
  MAX_DAY_WIDTH,
  MIN_DAY_WIDTH,
  MAX_RECURRENCE_EXPANSION,
  WARM_HISTORY_DAYS,
  addDays,
  daysBetween,
  dragPatch,
  eventLayout,
  expandRecurrence,
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

// ── Recurrence expansion (JS) ─────────────────────────────────────────

const weekly = { id:'E1', title:'Standup', startDate:'2026-07-07', dueDate:'2026-07-07', recurrence:{ frequency:'weekly', interval:1, count:4 } };
const wOCC = expandRecurrence(weekly, parseLocalDate('2026-07-01'), parseLocalDate('2026-08-15'));
assert.equal(wOCC.length, 4);
assert.equal(wOCC[0].occurrenceKey, '2026-07-07');
assert.equal(wOCC[3].occurrenceKey, '2026-07-28');

const daily = { id:'E2', title:'Meditate', startDate:'2026-07-10', recurrence:{ frequency:'daily', interval:1, until:'2026-07-13' } };
const dOCC = expandRecurrence(daily, parseLocalDate('2026-07-01'), parseLocalDate('2026-07-31'));
assert.deepEqual(dOCC.map((o) => o.occurrenceKey), ['2026-07-10','2026-07-11','2026-07-12','2026-07-13']);

const monthly = { id:'E3', title:'Rent', startDate:'2026-01-15', recurrence:{ frequency:'monthly', interval:1, count:3 } };
const mOCC = expandRecurrence(monthly, parseLocalDate('2026-01-01'), parseLocalDate('2026-12-31'));
assert.deepEqual(mOCC.map((o) => o.occurrenceKey), ['2026-01-15','2026-02-15','2026-03-15']);

const noRec = { id:'E7', title:'One-shot', startDate:'2026-07-10' };
assert.equal(expandRecurrence(noRec, parseLocalDate('2026-07-01'), parseLocalDate('2026-07-31')).length, 0);

const skip = { id:'E5', title:'Yoga', startDate:'2026-07-07', recurrence:{ frequency:'weekly', interval:1, count:4, exceptionDates:['2026-07-14','2026-07-28'] } };
const sOCC = expandRecurrence(skip, parseLocalDate('2026-07-01'), parseLocalDate('2026-08-15'));
assert.equal(sOCC.length, 2);
assert.ok(!sOCC.some((o) => o.occurrenceKey === '2026-07-14'));
assert.ok(!sOCC.some((o) => o.occurrenceKey === '2026-07-28'));

const cap = { id:'E6', title:'Freq', startDate:'2020-01-01', recurrence:{ frequency:'daily', interval:1, count:1000 } };
assert.equal(expandRecurrence(cap, parseLocalDate('2020-01-01'), parseLocalDate('2025-12-31')).length, MAX_RECURRENCE_EXPANSION);

const dur = { id:'E9', title:'Trip', startDate:'2026-07-10', dueDate:'2026-07-12', recurrence:{ frequency:'monthly', interval:1, count:2 } };
const durOcc = expandRecurrence(dur, parseLocalDate('2026-07-01'), parseLocalDate('2026-12-31'));
assert.equal(durOcc[0].startDate, '2026-07-10');
assert.equal(durOcc[0].dueDate, '2026-07-12');
assert.equal(durOcc[1].startDate, '2026-08-10');
assert.equal(durOcc[1].dueDate, '2026-08-12');

console.log('copal planning helpers: ok');
