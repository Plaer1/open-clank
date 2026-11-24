import assert from 'node:assert/strict';
import { sortedCounts, dailySeries, weeklySeries, bar, changeLine, unavailableList } from '../static/js/memoryExplorer.js';

assert.deepEqual(sortedCounts({ b: 1, a: 1, c: 5, bad: -1 }), [['c', 5], ['a', 1], ['b', 1]]);
const daily = dailySeries([{ day: '2026-11-13', created: 3 }], 3, '2026-11-14');
assert.deepEqual(daily, [{ day: '2026-11-12', created: 0 }, { day: '2026-11-13', created: 3 }, { day: '2026-11-14', created: 0 }]);
assert.deepEqual(weeklySeries(dailySeries([{ day: '2026-11-13', created: 3 }, { day: '2026-11-01', created: 2 }], 14, '2026-11-14')).map(w => w.created), [2, 3]);
assert.equal(bar(0, 5), ''); assert.equal(bar(5, 5, 4), '████'); assert.equal(bar(1, 100, 20), '█');
assert.match(changeLine({ event: 'superseded', title: 'likes', kind: 'fact', revision: 2, at: 't' }), /^Superseded · likes · fact · rev 2/);
assert.equal(changeLine({ event: 'created', block_id: 'abcdef123456', kind: 'fact', revision: 1, at: 't' }), 'Created · abcdef12 · fact · rev 1 · t');
assert.equal(changeLine({ event: 'created', title: null, kind: 'fact', revision: 1, at: 't' }), 'Created · memory · fact · rev 1 · t');
assert.deepEqual(unavailableList({ state: 'legacy', unavailable: ['links'] }), ['links']);
assert.deepEqual(unavailableList({ state: 'ready', truncated: false, links: { domains: { state: 'unavailable' } } }), ['domains']);
assert.deepEqual(unavailableList(null), ['everything']);
console.log('memory explorer helpers ok');
