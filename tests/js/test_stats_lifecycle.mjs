import assert from 'node:assert/strict';

import { boundedCadence, createStatsRefreshLifecycle } from '../../static/js/statsLifecycle.js';

const flush = async () => { await Promise.resolve(); await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); };

class Visibility extends EventTarget {
  visibilityState = 'visible';
}

function fakeTimers() {
  let next = 0;
  const intervals = new Map();
  return {
    intervals,
    setInterval(fn, delay) { const id = ++next; intervals.set(id, { fn, delay }); return id; },
    clearInterval(id) { intervals.delete(id); },
  };
}

assert.equal(boundedCadence(1), 30);
assert.equal(boundedCadence(999999), 86400);

{
  const target = new EventTarget();
  const visibility = new Visibility();
  const timers = fakeTimers();
  const pending = [];
  const calls = [];
  const lifecycle = createStatsRefreshLifecycle({
    target, visibility, timers, cadenceSeconds: 45,
    refresh: value => { calls.push(value); return new Promise(resolve => pending.push(resolve)); },
  });
  assert.equal(lifecycle.start(), true);
  assert.equal(lifecycle.start(), false, 'reopen must not duplicate listeners or timers');
  await flush();
  target.dispatchEvent(new Event('openclank:stats-dirty'));
  target.dispatchEvent(new Event('openclank:session-changed'));
  target.dispatchEvent(new Event('online'));
  assert.equal(calls.length, 1, 'events during refresh coalesce');
  pending.shift()();
  await flush();
  assert.equal(calls.length, 2, 'one trailing refresh consumes every dirty event');
  pending.shift()();
  await flush();
  assert.equal(timers.intervals.size, 1);
  assert.equal([...timers.intervals.values()][0].delay, 45000);
  assert.equal(lifecycle.setCadence(2), 30);
  assert.equal([...timers.intervals.values()][0].delay, 30000);
  assert.equal(lifecycle.stop(), true);
  assert.equal(timers.intervals.size, 0);
  target.dispatchEvent(new Event('openclank:stats-dirty'));
  await flush();
  assert.equal(calls.length, 2, 'closed lifecycle owns no listener');
}

{
  const target = new EventTarget();
  const visibility = new Visibility();
  const timers = fakeTimers();
  let calls = 0;
  const lifecycle = createStatsRefreshLifecycle({ target, visibility, timers, refresh: () => { calls += 1; } });
  visibility.visibilityState = 'hidden';
  lifecycle.start({ immediate: false });
  target.dispatchEvent(new Event('openclank:provider-changed'));
  await flush();
  assert.equal(calls, 0);
  visibility.visibilityState = 'visible';
  visibility.dispatchEvent(new Event('visibilitychange'));
  await flush();
  assert.equal(calls, 1, 'visibility consumes one hidden dirty refresh');
  lifecycle.stop();
}

{
  const target = new EventTarget();
  const visibility = new Visibility();
  const timers = fakeTimers();
  let signal;
  const lifecycle = createStatsRefreshLifecycle({
    target, visibility, timers,
    refresh: value => { signal = value.signal; return new Promise(() => {}); },
  });
  lifecycle.start();
  await flush();
  lifecycle.stop();
  assert.equal(signal.aborted, true, 'close aborts the in-flight refresh');
  assert.deepEqual(lifecycle.diagnostics(), { started: false, dirty: false, inFlight: false, cadenceSeconds: 300, refreshCount: 1 });
}

console.log('stats lifecycle behavioral checks passed');
