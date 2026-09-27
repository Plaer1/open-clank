import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = fs.readFileSync('static/index.html', 'utf8');
const summary = { schema: 'open-clank.stats.v1', owner_scope: '0123456789abcdef', scope: { period: '30d', timezone: 'UTC' }, coverage: { state: 'complete' }, events: [], buckets: [] };
let quotaRequests = 0;
const quota = { schema: 'open-clank.stats.v1', owner_scope: '0123456789abcdef', observations: [], timeline: [], token_volume: [], account_status: 'unavailable', current_session: { attributed: false, cost: { state: 'unpriced' } } };

await withCopalBrowser({ page, request: async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify({storage_namespace: 'fixture-owner', account_id: 'fixture'})); return true; }
  if (url.pathname === '/api/notes' || url.pathname === '/api/presets/templates' || url.pathname.startsWith('/api/copal/notes') || url.pathname.startsWith('/api/copal/presets')) { res.setHeader('content-type', 'application/json'); res.end('[]'); return true; }
  if (url.pathname === '/api/stats/v1/summary') { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(summary)); return true; }
  if (url.pathname === '/api/stats/v1/quota') { quotaRequests += 1; res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(quota)); return true; }
  if (url.pathname.startsWith('/api/')) { res.setHeader('content-type', 'application/json'); res.end('{}'); return true; }
  return false;
}}, async ({ evaluate, until }) => {
  await until(`document.querySelector('#tool-usage-btn') && document.querySelector('#rail-usage')`);
  const before = await evaluate(`({usageScript: [...document.scripts].some(s => s.src.includes('statsUsage.js')), sidebar: !!document.querySelector('#tool-usage-btn'), rail: !!document.querySelector('#rail-usage')})`);
  assert.equal(before.usageScript, false, 'Usage remains lazy before first open');
  await until(`typeof window.__openStatsUsage === 'function'`);
  assert.equal(await evaluate(`typeof window.__openStatsUsage`), 'function', 'production shell exposes the Usage opener');
  await evaluate(`document.querySelector('#tool-usage-btn').click()`);
  await until(`document.querySelector('.stats-usage-shell')`);
  const first = await evaluate(`({windows: document.querySelectorAll('.stats-usage-shell').length, path: location.pathname})`);
  assert.equal(first.windows, 1);
  const firstRequests = quotaRequests;
  assert.equal(firstRequests, 2, 'first open performs exactly one owner bootstrap refetch');
  await evaluate(`document.querySelector('.close-btn[aria-label^="Close"]').click()`);
  await new Promise(r => setTimeout(r, 100));
  await evaluate(`document.querySelector('#rail-usage').click()`);
  await until(`document.querySelector('.stats-usage-shell')`);
  assert.equal(await evaluate(`document.querySelectorAll('.stats-usage-shell').length`), 1, 'rail reuses the Usage instance');
  const railRequests = quotaRequests - firstRequests;
  assert.equal(railRequests, 1, 'rail open has exactly its owner bootstrap pair');
  await evaluate(`document.querySelector('.close-btn[aria-label^="Close"]').click()`);
  await new Promise(r => setTimeout(r, 100));
  await evaluate(`history.pushState({}, '', '/usage'); window.dispatchEvent(new PopStateEvent('popstate'))`);
  await until(`document.querySelector('.stats-usage-shell')`);
  const reopened = await evaluate(`document.querySelectorAll('.stats-usage-shell').length`);
  assert.equal(reopened, 1);
  const directRequests = quotaRequests - firstRequests - railRequests;
  assert.equal(directRequests, 2, 'direct route open has exactly its owner bootstrap pair');
  await evaluate(`document.querySelector('.close-btn[aria-label^="Close"]').click()`);
  await new Promise(r => setTimeout(r, 100));
  await evaluate(`window.dispatchEvent(new CustomEvent('openclank:open-usage'))`);
  await until(`document.querySelector('.stats-usage-shell')`);
  await evaluate(`document.querySelector('.close-btn[aria-label^="Close"]').click()`);
  await new Promise(r => setTimeout(r, 100));
  await evaluate(`window.dispatchEvent(new CustomEvent('openclank:ui-control', { detail: { type: 'ui_control', action: 'usage' } }))`);
  await until(`document.querySelector('.stats-usage-shell')`);
  assert.equal(await evaluate(`document.querySelectorAll('.stats-usage-shell').length`), 1);
  assert.equal(quotaRequests - firstRequests - railRequests - directRequests, 2, 'custom dispatch reuses the adopted owner without duplicate refresh');
  console.log(JSON.stringify({ before, first, reopened, quotaRequests }));
});
