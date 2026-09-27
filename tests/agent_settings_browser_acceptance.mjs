import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';
import { setTimeout as delay } from 'node:timers/promises';

const page = `<!doctype html><html><body><script type="module">
  window._isAdmin = false;
  const parsed = new DOMParser().parseFromString(await fetch('/static/index.html').then(r => r.text()), 'text/html');
  parsed.querySelector('#set-agentMaxTools')?.remove();
  parsed.querySelector('#set-agentMaxRounds')?.remove();
  parsed.querySelector('#set-agentMsg')?.remove();
  document.body.replaceChildren(parsed.getElementById('settings-modal'));
  const originalFetch = window.fetch;
  window.fetch = (input, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (window.__agentRace) headers.set('X-Agent-Race', '1');
    return originalFetch(input, {...options, headers});
  };
  const module = await import('/static/js/settings.js?agent-settings-fixture');
  window.settingsModule = module.default;
  await module.open('ai');
  await window.__agentSettingsReady;
</script></body></html>`;

let reads = 0;
let writes = [];
let raceReads = 0;
const response = (res, value, status = 200) => {
  res.writeHead(status, {'content-type': 'application/json'});
  res.end(JSON.stringify(value));
  return true;
};

await withCopalBrowser({ page, request: async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/auth/settings' && req.method === 'GET') {
    reads += 1;
    if (req.headers['x-agent-race']) {
      raceReads += 1;
      if (raceReads === 1) await delay(100);
      return response(res, { agent_settings: {}, agent_settings_override: false, agent_settings_effective: {status:'available', value:{context:{hard:100000 + raceReads, usable:80000 + raceReads}, compaction:{tailTurns: raceReads === 1 ? 111 : 222, reserved:13000}}} });
    }
    const state = reads === 1
      ? { agent_settings: {}, agent_settings_override: false, agent_settings_effective: {status:'pending', reason:'CHAT_ROUTE_UNAVAILABLE'} }
      : { agent_settings: {compaction:{tail_turns: reads}}, agent_settings_override: reads < 4, agent_settings_effective: {status:'available', value:{context:{hard:100000, usable:80000}, compaction:{tailTurns:reads, reserved:13000}}} };
    return response(res, state);
  }
  if (url.pathname === '/api/auth/settings' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    writes.push(JSON.parse(body || '{}'));
    return response(res, {ok:true});
  }
  if (url.pathname === '/api/models/endpoints') return response(res, {items:[]});
  return false;
}}, async ({ evaluate, until }) => {
  await until(`document.querySelector('#set-agentNativeSave')`);
  await until(`/pending|Inherited/.test(document.querySelector('#set-agentNativeStatus').textContent)`);
  assert.match(await evaluate(`document.querySelector('#set-agentNativeStatus').textContent`), /pending|Inherited/);
  await evaluate(`document.querySelector('#set-agentTailTurns').value = '7'`);
  await evaluate(`document.querySelector('#set-agentNativeSave').click()`);
  await until(`document.querySelector('#set-agentNativeEffective').textContent.includes('80000')`);
  assert.equal(writes.at(-1).agent_settings.compaction.tail_turns, 7);
  assert.deepEqual(Object.keys(writes.at(-1).agent_settings.compaction), ['tail_turns']);
  assert.deepEqual(writes.at(-1).agent_settings.checkpoint, {});
  await evaluate(`document.querySelector('#set-agentNativeReset').click()`);
  await until(`document.querySelector('#set-agentNativeStatus').textContent.includes('Inherited')`);
  const before = reads;
  await evaluate(`window.__agentRace = true; window.dispatchEvent(new CustomEvent('openclank:default-chat-changed'))`);
  for (let attempt = 0; attempt < 40 && raceReads < 1; attempt += 1) await delay(5);
  assert.equal(raceReads, 1);
  await evaluate(`window.dispatchEvent(new CustomEvent('openclank:default-chat-changed'))`);
  await until(`document.querySelector('#set-agentNativeEffective').textContent.includes('222 tail turns')`);
  assert.ok(reads > before);
  assert.equal(await evaluate(`document.querySelector('#set-agentMaxTools') === null`), true);
});

console.log('agent settings mounted acceptance passed');
