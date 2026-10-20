#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const now = new Date().toISOString();
const observations = [
  { account_id: 'secret-account-a', provider_id: 'openai', label: 'Requests', window_kind: 'continuous', utilization_numerator: 80, utilization_denominator: 100, state: 'official', observed_at: now, reset_at: new Date(Date.now() + 3600000).toISOString() },
  { account_id: 'secret-account-a', provider_id: 'openai', label: 'Tokens', window_kind: 'continuous', utilization_numerator: 90, utilization_denominator: 100, state: 'official', observed_at: now, reset_at: new Date(Date.now() + 7200000).toISOString() },
  { account_id: 'secret-account-b', provider_id: 'anthropic', label: 'Nonstandard window', window_kind: 'discrete', utilization_numerator: 125, utilization_denominator: 100, state: 'local', observed_at: now, reset_at: new Date(Date.now() + 1800000).toISOString() },
  { account_id: 'secret-account-c', provider_id: 'unsupported', label: 'Subscription', window_kind: 'unavailable', utilization_numerator: null, utilization_denominator: null, state: 'unsupported', observed_at: null, reset_at: null },
  { account_id: 'secret-account-d', provider_id: 'expired', label: 'Expired', window_kind: 'unavailable', utilization_numerator: null, utilization_denominator: null, state: 'expired', observed_at: null, reset_at: null },
  { account_id: 'secret-account-e', provider_id: 'offline', label: 'Offline', window_kind: 'unavailable', utilization_numerator: null, utilization_denominator: null, state: 'error', observed_at: null, reset_at: null },
  { account_id: 'secret-account-f', provider_id: 'stale', label: 'Stale', window_kind: 'continuous', utilization_numerator: 40, utilization_denominator: 100, state: 'stale', observed_at: '2020-01-01T00:00:00Z', reset_at: null },
  { account_id: 'secret-account-g', provider_id: 'permission', label: 'Permission', window_kind: 'unavailable', utilization_numerator: null, utilization_denominator: null, state: 'permission', observed_at: null, reset_at: null },
];
const timeline = observations.filter(item => item.utilization_denominator).map((item, index) => ({ account_index: index, provider_id: item.provider_id, numerator: item.utilization_numerator, denominator: item.utilization_denominator, observed_at: item.observed_at, reset_at: item.reset_at }));
const fixture = { schema: 'open-clank.stats.v1', owner_scope: '0123456789abcdef', observations, timeline, capabilities: { providers: ['openai', 'anthropic'], windows: ['continuous', 'discrete'], models: [] }, current_session: { attributed: true, tokens: '42', cost: { state: 'unpriced' } }, coverage: { state: 'partial' }, truncation: { windows: false } };
let quotaRequests = 0;
const requestUrls = [];

await withCopalBrowser({
  page: '<link rel="stylesheet" href="/static/style.css"><button id="tool-usage-btn">Usage</button><main></main><script>window.sessionModule={getCurrentSessionId:()=>"session-fixture"};</script><script type="module">import("/static/js/usageEntry.js");</script>',
  overrides: { '/static/js/copal/windows.js': `export function createOpenClankWindow({id,onClosed}) { const root=document.createElement('section'); root.id=id; root.className='copal-window'; const body=document.createElement('div'); root.append(body); document.body.append(root); return { root, body, show(){root.classList.remove('hidden')}, hide(){root.classList.add('hidden')}, close(){onClosed?.();root.remove()} }; }` },
  request: async (req, res) => { const url = new URL(req.url, 'http://fixture'); if (url.pathname === '/api/stats/v1/session-handle') { let body = ''; for await (const chunk of req) body += chunk; const payload = JSON.parse(body); assert.equal(payload.session_id, 'session-fixture'); res.setHeader('content-type', 'application/json'); res.end('{"handle":"session_fixture_handle"}'); return true; } if (url.pathname !== '/api/stats/v1/quota') return false; requestUrls.push(req.url); quotaRequests += 1; if (quotaRequests > 3) { res.writeHead(503); res.end('{"error":"offline"}'); return true; } res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(fixture)); return true; },
}, async ({ evaluate, until, cdp }) => {
  await until('Boolean(window.__openStatsUsage)', 'Usage opener');
  await evaluate('window.__openStatsUsage({view:"quota"})');
  await until('document.querySelectorAll(".stats-quota-card").length >= 6', 'quota cards');
  assert(quotaRequests <= 2, 'one opener performs only bounded owner bootstrap requests');
  assert(requestUrls[0].includes('session_id=session_fixture_handle') && !requestUrls[0].includes('session-fixture') && requestUrls[0].includes('period=30d') && requestUrls[0].includes('timezone='), 'quota request carries opaque session handle, default range, and timezone scope');
  const state = await evaluate(`(() => { const text=document.body.innerText; const url=location.href; const prefs=Object.keys(localStorage).join('|'); const over=[...document.querySelectorAll('.stats-quota-card')].find(node=>node.textContent.includes('125.00%')); const before=document.querySelectorAll('#stats-usage-window').length; return { text,url,prefs,chart:Boolean(document.querySelector('.stats-usage-timeline-chart')),table:Boolean(document.querySelector('table')),focusable:Boolean(document.querySelector('.stats-usage-timeline-chart')?.tabIndex >= 0),ring:over?.querySelector('.stats-quota-ring')?.style.getPropertyValue('--stats-quota-progress'),windows:before }; })()`);
  assert(state.chart && state.table && state.focusable, `numeric SVG and accessible table mounted: ${JSON.stringify(state)}`);
  assert.equal(state.ring, '100%', 'over-100 utilization clamps geometry');
  assert(state.text.includes('80.00%') && state.text.includes('90.00%') && state.text.includes('125.00%'), 'threshold values are textual');
  assert(state.text.includes('Tokens 42') && state.text.includes('Cost unpriced'), 'current session attribution is displayed');
  assert(state.text.includes('Unsupported') && state.text.includes('Expired') && state.text.includes('Provider error') && state.text.includes('stale'), 'unsupported/expired/error/stale states remain distinct');
  assert(!state.text.includes('secret-account-') && !state.url.includes('secret-account-') && !state.prefs.includes('secret-account-'), 'opaque identities stay out of UI state');
  assert.equal(state.windows, 1, 'one Usage window is mounted');
  await evaluate('document.querySelector("#stats-usage-window .close-btn")?.click()');
  await evaluate('window.__openStatsUsage({view:"quota"})');
  await until('document.querySelectorAll(".stats-quota-card").length >= 6', 'quota cards after reopen');
  assert.equal(await evaluate('document.querySelectorAll("#stats-usage-window").length'), 1, 'close and reopen retains one live Usage window');
  await cdp('Emulation.setDeviceMetricsOverride', { width: 420, height: 780, deviceScaleFactor: 1, mobile: false });
  await cdp('Emulation.setEmulatedMedia', { features: [{ name: 'prefers-reduced-motion', value: 'reduce' }] });
  const accessibility = await evaluate(`(() => { document.documentElement.dir='rtl'; document.querySelector('.stats-usage-timeline-chart')?.focus(); const shell=getComputedStyle(document.querySelector('.stats-usage-shell')); const toolbar=getComputedStyle(document.querySelector('.stats-usage-toolbar')); const ring=getComputedStyle(document.querySelector('.stats-quota-ring')); return { focused:document.activeElement?.getAttribute('class'), dir:document.documentElement.dir, shell:shell.display, toolbar:toolbar.display, ring:ring.backgroundImage, reduced:matchMedia('(prefers-reduced-motion: reduce)').matches }; })()`);
  assert.equal(accessibility.dir, 'rtl'); assert.equal(accessibility.reduced, true); assert.equal(accessibility.focused, 'stats-usage-timeline-chart'); assert.equal(accessibility.shell, 'grid'); assert.equal(accessibility.toolbar, 'flex'); assert(accessibility.ring.includes('conic-gradient'), 'ring has visible product geometry');
  await evaluate('window.dispatchEvent(new CustomEvent("openclank:auth-context-changed")); window.__openStatsUsage({view:"quota"})');
  await until('document.body.innerText.includes("Unavailable · Retry")', 'owner-isolated retry state after refresh failure');
  assert.equal(await evaluate('document.querySelectorAll(".stats-quota-card").length'), 0, 'failed owner response does not render prior-owner cards');
  console.log('Stats Usage self-contained mounted acceptance passed');
});
