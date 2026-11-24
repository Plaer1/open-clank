import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

let calls = 0;
let activeOwner = 'A';
const ownerA = { owner_scope: 'aaaaaaaaaaaaaaaa', observations: [
  { account_id: 'raw-account-A', account_label: 'Personal', provider_id: 'raw-provider-openai', provider_label: 'OpenAI', window_id: 'primary', window_kind: 'continuous', utilization_numerator: 80, utilization_denominator: 100, state: 'official', observed_at: new Date().toISOString(), label: 'Requests' },
  { account_id: 'raw-account-A', account_label: 'Personal', provider_id: 'raw-provider-anthropic', provider_label: 'Anthropic', window_id: 'primary', window_kind: 'continuous', utilization_numerator: 70, utilization_denominator: 100, state: 'official', observed_at: new Date().toISOString(), label: 'Tokens' },
], timeline: [], token_volume: [], current_session: { attributed: false, cost: { state: 'unpriced' } } };
const ownerB = { owner_scope: 'bbbbbbbbbbbbbbbb', observations: [{ account_id: 'raw-account-B', account_label: 'Team', provider_id: 'raw-provider-google', provider_label: 'Google', window_id: 'primary', window_kind: 'continuous', utilization_numerator: 90, utilization_denominator: 100, state: 'official', observed_at: new Date().toISOString(), label: 'Requests' }], timeline: [], token_volume: [], current_session: { attributed: false, cost: { state: 'unpriced' } } };
const page = `<link rel="stylesheet" href="/static/style.css"><button id="tool-usage-btn">Usage</button><main></main><script>window.__logs=[]; for (const k of ['log','warn','error']) { const f=console[k]; console[k]=(...a)=>{window.__logs.push(a.join(' '));f(...a)}; }</script><script type="module">import('/static/js/usageEntry.js');</script>`;

await withCopalBrowser({ page, request: async (req, res) => {
  const path = new URL(req.url, 'http://fixture').pathname;
  if (path === '/api/test/set-owner') { activeOwner = new URL(req.url, 'http://fixture').searchParams.get('owner') === 'B' ? 'B' : 'A'; res.end('ok'); return true; }
  if (path !== '/api/stats/v1/quota') return false;
  calls += 1;
  const payload = activeOwner === 'B' ? ownerB : ownerA;
  res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(payload)); return true;
}}, async ({ evaluate, until }) => {
  await until('typeof window.__openStatsUsage === "function"');
  await evaluate('void window.__openStatsUsage({view:"quota"})');
  await until('document.querySelectorAll(".stats-quota-card").length === 2');
  await evaluate('document.querySelector("[data-stats-filter=provider]").value = "1"; document.querySelector("[data-stats-filter=provider]").dispatchEvent(new Event("change", {bubbles:true}))');
  await new Promise(r => setTimeout(r, 100));
  const aPref = await evaluate('JSON.stringify({text:document.body.innerText,url:location.href,keys:Object.keys(localStorage),values:Object.values(localStorage),logs:window.__logs})');
  assert(aPref.includes('Anthropic') && !aPref.includes('raw-provider-') && !aPref.includes('raw-account-'));
  await evaluate('(async()=>{await fetch("/api/test/set-owner?owner=B"); window.dispatchEvent(new CustomEvent("openclank:auth-context-changed")); window.__openStatsUsage({view:"quota"})})()');
  await until('document.body.innerText.includes("Google") && Object.keys(localStorage).some(key => key.includes("bbbbbbbbbbbbbbbb"))');
  const b = await evaluate('JSON.stringify({text:document.body.innerText,url:location.href,values:Object.values(localStorage),logs:window.__logs})');
  assert(b.includes('Team') && !b.includes('Personal') && !b.includes('Anthropic'));
  await evaluate('document.querySelector("[data-stats-range]").value = "3d"; document.querySelector("[data-stats-range]").dispatchEvent(new Event("change", {bubbles:true}))');
  await new Promise(r => setTimeout(r, 100));
  const bPref = await evaluate('JSON.stringify(Object.values(localStorage))');
  assert(bPref.includes('3d') && !bPref.includes('\"provider\":\"1\"'));
  await evaluate('(async()=>{await fetch("/api/test/set-owner?owner=A"); window.dispatchEvent(new CustomEvent("openclank:auth-context-changed")); window.__openStatsUsage({view:"quota"})})()');
  await until('document.body.innerText.includes("Personal") && Object.keys(localStorage).some(key => key.includes("aaaaaaaaaaaaaaaa"))');
  const restoredPrefs = await evaluate('JSON.stringify({keys:Object.keys(localStorage),values:Object.values(localStorage)})');
  const restoredPrefsData = JSON.parse(restoredPrefs);
  const prefsByKey = Object.fromEntries(restoredPrefsData.keys.map((key, index) => [key, JSON.parse(restoredPrefsData.values[index])]));
  assert.deepEqual(prefsByKey['openclank.usage.scope.v2:aaaaaaaaaaaaaaaa'].filters, { provider: '1', model: '', account: '' });
  assert.equal(prefsByKey['openclank.usage.scope.v2:aaaaaaaaaaaaaaaa'].range, '30d');
  assert.deepEqual(prefsByKey['openclank.usage.scope.v2:bbbbbbbbbbbbbbbb'].filters, { provider: '', model: '', account: '' });
  assert.equal(prefsByKey['openclank.usage.scope.v2:bbbbbbbbbbbbbbbb'].range, '3d');
  assert.equal(prefsByKey['openclank.usage.scope.v2:anonymous'].filters.provider, '');
  const restored = await evaluate('JSON.stringify({text:document.body.innerText,url:location.href,values:Object.values(localStorage),logs:window.__logs})');
  assert(restored.includes('Personal') && !restored.includes('Google') && !restored.includes('raw-provider-') && !restored.includes('raw-account-'));
  assert(!restored.includes('raw-provider-') && !restored.includes('raw-account-'));
  console.log(JSON.stringify({calls, aPref: JSON.parse(aPref), b: JSON.parse(b), restored: JSON.parse(restored)}));
});
