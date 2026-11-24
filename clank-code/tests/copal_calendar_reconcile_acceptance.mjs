import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// Mount the production module without initializing unrelated app features.
const source = fs.readFileSync('static/js/copal.js', 'utf8');
const page = `<!doctype html><script type="module">
import { reconcileFixture } from '/static/js/copal.js';
window.fixture = reconcileFixture;
window.refreshes = 0;
window.addEventListener('calendar-refresh', () => window.refreshes++);
</script>`;
const calls = [], held = [];
let hold = false, response = { ok:true, projections:[] }, status = 200;
await withCopalBrowser({
  page,
  overrides:{ '/static/js/copal.js':source + '\nexport const reconcileFixture = { state, reconcileCalendarProjection };\n' },
  request:async (req, res) => {
    if (!req.url.startsWith('/api/copal/calendar/reconcile')) return false;
    calls.push({ account:req.headers['x-copal-account'], workspace:new URL(req.url, 'http://test').searchParams.get('workspace') });
    const body = JSON.stringify(response), code = status;
    const send = () => { res.writeHead(code, { 'content-type':'application/json' }); res.end(body); };
    if (hold) held.push(send); else send();
    return true;
  },
}, async ({ evaluate, until }) => {
  await until('Boolean(window.fixture)');
  await evaluate(`Object.assign(fixture.state, { accountId:'alice', storageNamespace:'alice', workspace:'one' });`);
  await evaluate('fixture.reconcileCalendarProjection()');
  await evaluate('Promise.all(Array.from({length:12}, () => fixture.reconcileCalendarProjection()))');
  assert.equal(calls.length, 1, 'successful empty planning is cached');
  await evaluate(`fixture.state.docs.push({id:'ordinary',kind:'note',head:'1'}); fixture.reconcileCalendarProjection()`);
  assert.equal(calls.length, 1, 'unrelated documents do not reconcile again');
  await evaluate('fixture.reconcileCalendarProjection(true)');
  assert.equal(calls.length, 2, 'explicit force still repairs');

  hold = true;
  await evaluate(`fixture.state.planning.trackRegistry = {id:'registry',head:'1'}; window.first = fixture.reconcileCalendarProjection(); window.same = fixture.reconcileCalendarProjection(); void 0`, false);
  await until('Boolean(fixture.state.projectingPlanning)');
  while (!held.length) await new Promise(resolve => setTimeout(resolve, 10));
  assert.equal(calls.length, 3, 'same revision shares one in-flight request');
  await evaluate(`fixture.state.planning.trackRegistry.head = '2'; window.changed = fixture.reconcileCalendarProjection(); void 0`, false);
  hold = false;
  held.shift()();
  await evaluate('Promise.all([window.first,window.same,window.changed])');
  assert.equal(calls.length, 4, 'changed revision follows pending request once');
  await evaluate('fixture.reconcileCalendarProjection()');
  assert.equal(calls.length, 4);

  response = {ok:false, projections:[{enabled:true,ok:false}]};
  await evaluate(`fixture.state.planning.trackRegistry.head = '3'; fixture.reconcileCalendarProjection()`);
  response = {ok:true, projections:[{enabled:true,ok:true}]};
  await evaluate('fixture.reconcileCalendarProjection()');
  assert.equal(calls.length, 6, 'projection failure is not cached');
  assert.equal(await evaluate('window.refreshes'), 1);
  status = 503; response = {detail:'retry later'};
  await evaluate(`fixture.state.planning.trackRegistry.head = '4'; fixture.reconcileCalendarProjection().catch(() => {})`);
  status = 200; response = {ok:true,projections:[]};
  await evaluate('fixture.reconcileCalendarProjection()');
  assert.equal(calls.length, 8, 'transport failure is retryable');

  response = {ok:true,projections:[{enabled:true,ok:true}]}; hold = true;
  await evaluate(`fixture.state.planning.trackRegistry.head = '5'; window.old = fixture.reconcileCalendarProjection().catch(() => {}); void 0`, false);
  while (!held.length) await new Promise(resolve => setTimeout(resolve, 10));
  await evaluate(`fixture.state.accountId = 'bob'; fixture.state.storageNamespace = 'bob'; fixture.state.contextEpoch++; fixture.state.workspace = 'two';`);
  hold = false; response = {ok:true,projections:[]};
  await evaluate('fixture.reconcileCalendarProjection()');
  const newHead = await evaluate('fixture.state.projectedPlanningHead');
  held.shift()();
  await evaluate('window.old');
  assert.equal(await evaluate('fixture.state.projectedPlanningHead'), newHead, 'stale owner cannot replace newer cache');
  assert.equal(await evaluate('window.refreshes'), 1, 'stale owner cannot notify');
  assert.deepEqual(calls.at(-1), {account:'bob',workspace:'two'});
  await evaluate(`fixture.state.workspace = 'three'; fixture.reconcileCalendarProjection()`);
  assert.equal(calls.length, 11, 'same heads in a new workspace are not cached');
  await evaluate(`fixture.state.planning.trackRegistry.id = 'replacement'; fixture.reconcileCalendarProjection()`);
  assert.equal(calls.length, 12, 'different identity with same head is a new revision');
  await evaluate(`fixture.state.planning.trackRegistry = null; fixture.state.docs = [{id:'legacy',kind:'planning',head:'1'}]; fixture.reconcileCalendarProjection()`);
  await evaluate(`fixture.state.docs[0].head = '2'; fixture.reconcileCalendarProjection()`);
  assert.equal(calls.length, 14, 'legacy planning heads participate');
  await evaluate(`fixture.state.planning.floatingTodos = [{id:'floating',head:'1'}]; fixture.reconcileCalendarProjection()`);
  await evaluate(`fixture.state.planning.floatingTodos[0].head = '2'; fixture.reconcileCalendarProjection()`);
  await evaluate(`fixture.state.planning.floatingTodos = []; fixture.reconcileCalendarProjection()`);
  assert.equal(calls.length, 17, 'floating event create/edit/delete participates');
  hold = true;
  await evaluate(`fixture.state.planning.floatingTodos = [{id:'floating',head:'3'}]; window.floatingFirst = fixture.reconcileCalendarProjection(); void 0`, false);
  while (!held.length) await new Promise(resolve => setTimeout(resolve, 10));
  await evaluate(`fixture.state.planning.floatingTodos[0].head = '4'; window.floatingNext = fixture.reconcileCalendarProjection(); void 0`, false);
  hold = false;
  held.shift()();
  await evaluate('Promise.all([window.floatingFirst, window.floatingNext])');
  assert.equal(calls.length, 19, 'floating edit during pending reconcile follows once');
  assert.equal(await evaluate('fixture.state.projectingPlanning'), null);
  console.log('Calendar reconciliation: empty, force, single-flight, revision, failure, scope and legacy gates passed.');
});
