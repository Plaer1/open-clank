#!/usr/bin/env node

// Mounted production Settings → Reminders journey.
//
// The page imports the shipped Settings module and lets that module create and
// mutate every endpoint row.  The HTTP seam only supplies the authenticated
// per-account API boundary and a disposable delivery ledger; it does not
// replace the Settings UI with test-owned state handlers.
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><html><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
</head><body>
  <script type="module">
    window._isAdmin = false;
    window.__fixtureAccount = new URLSearchParams(location.search).get('account') || 'alice';
    const indexHtml = await fetch('/static/index.html').then(response => response.text());
    const parsed = new DOMParser().parseFromString(indexHtml, 'text/html');
    const settingsModal = parsed.getElementById('settings-modal');
    if (!settingsModal) throw new Error('static/index.html has no #settings-modal');
    document.body.replaceChildren(settingsModal);
    document.body.dataset.accountId = window.__fixtureAccount;
    const stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = '/static/style.css';
    document.head.appendChild(stylesheet);
    await new Promise(resolve => {
      stylesheet.addEventListener('load', resolve, { once:true });
      stylesheet.addEventListener('error', resolve, { once:true });
    });
    const fixtureFetch = window.fetch.bind(window);
    window.fetch = (input, options = {}) => {
      const headers = new Headers(options.headers || {});
      headers.set('X-Fixture-Account', document.body.dataset.accountId || 'alice');
      return fixtureFetch(input, { ...options, headers });
    };
    try {
      const module = await import('/static/js/settings.js?reminder-production-fixture');
      window.settingsModule = module.default;
      module.open('reminders');
    } catch (error) {
      window.__settingsError = error.stack || String(error);
    }
  </script>
</body></html>`;

const clone = value => JSON.parse(JSON.stringify(value));
const accountState = {
  alice: { settings: { reminder_channel: 'browser', reminder_endpoints: { version: 1, endpoints: [] } } },
  bob: { settings: { reminder_channel: 'browser', reminder_endpoints: { version: 1, endpoints: [] } } },
};
const deliveries = [];
const tests = [];
const receipts = new Map();
let saves = 0;
let partialArmed = false;

function response(res, value, status = 200) {
  res.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' });
  res.end(JSON.stringify(value));
  return true;
}

async function body(req) {
  let raw = '';
  for await (const chunk of req) raw += chunk;
  return JSON.parse(raw || '{}');
}

function endpointIdentity(row) {
  return JSON.stringify({
    channel: String(row.channel || 'browser').toLowerCase(),
    email_to: String(row.email_to || '').trim().toLowerCase(),
    email_account_id: String(row.email_account_id || ''),
    ntfy_topic: String(row.ntfy_topic || '').trim().toLowerCase(),
    ntfy_integration_id: String(row.ntfy_integration_id || ''),
    webhook_integration_id: String(row.webhook_integration_id || ''),
    webhook_payload_template: String(row.webhook_payload_template || '').trim(),
  });
}

function stableEndpointId(account, row, index) {
  if (row.id) return String(row.id);
  let hash = 2166136261;
  for (const char of `${account}\0${endpointIdentity(row)}`) {
    hash ^= char.charCodeAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return `${account}-endpoint-${(hash >>> 0).toString(16)}-${index + 1}`;
}

function persistEndpoints(account, value) {
  const envelope = value && typeof value === 'object' && !Array.isArray(value)
    ? value : { version: 1, endpoints: value };
  const rows = Array.isArray(envelope.endpoints) ? envelope.endpoints : [];
  return {
    version: 1,
    endpoints: rows.map((row, index) => ({ ...row, id: stableEndpointId(account, row, index) })),
  };
}

function integrationList(host) {
  return [{
    id: 'hook-main', name: 'Disposable webhook', preset: 'generic_webhook',
    base_url: `http://${host}/delivery/hook-main`, enabled: true,
  }];
}

function settingsFor(account) {
  return clone(accountState[account]?.settings || accountState.alice.settings);
}

async function request(req, res) {
  const url = new URL(req.url, 'http://fixture');
  if (!url.pathname.startsWith('/api/') && !url.pathname.startsWith('/fixture/')) return false;
  const account = req.headers['x-fixture-account'] || 'alice';
  if (!accountState[account]) return response(res, { detail: 'unknown account' }, 403);

  if (url.pathname === '/fixture/arm-partial') {
    partialArmed = true;
    return response(res, { ok: true });
  }
  if (url.pathname === '/fixture/state') {
    return response(res, { saves, accounts: clone(accountState), deliveries: clone(deliveries), tests: clone(tests), receipts: clone(Object.fromEntries(receipts)) });
  }
  if (url.pathname === '/api/auth/status') return response(res, { username: account, is_admin: false });
  if (url.pathname === '/api/auth/policy') return response(res, { password_min_length: 8 });
  if (url.pathname === '/api/auth/settings') {
    if (req.method === 'GET') return response(res, settingsFor(account));
    if (req.method === 'POST') {
      const payload = await body(req);
      if (payload.reminder_endpoints !== undefined) {
        accountState[account].settings.reminder_endpoints = persistEndpoints(account, payload.reminder_endpoints);
      }
      for (const key of ['reminder_channel', 'reminder_llm_synthesis', 'reminder_llm_persona', 'app_public_url']) {
        if (key in payload) accountState[account].settings[key] = payload[key];
      }
      saves += 1;
      return response(res, settingsFor(account));
    }
  }
  if (url.pathname === '/api/email/accounts') return response(res, { accounts: [] });
  if (url.pathname === '/api/auth/integrations') return response(res, { integrations: integrationList(req.headers.host) });
  if (url.pathname === '/api/presets/default-persona') return response(res, { persona: '' });
  if (url.pathname === '/api/presets/templates') return response(res, []);
  if (url.pathname === '/api/notes/fire-reminder' && req.method === 'POST') {
    const payload = await body(req);
    if (String(payload.note_id || '').startsWith('test-')) {
      tests.push({ account, ...payload });
      return response(res, { channel: payload.channel || 'browser', webhook_sent: true, email_sent: false, ntfy_sent: false });
    }
    const savedRows = accountState[account].settings.reminder_endpoints.endpoints.filter(row => row.enabled !== false);
    const occurrence = String(payload.occurrence_key || payload.note_id || 'occurrence');
    const endpointResults = savedRows.map((row, index) => {
      const key = `${account}\0${occurrence}\0${row.id}`;
      if (receipts.get(key) === 'sent') return { id: row.id, channel: row.channel, status: 'skipped' };
      const shouldFail = partialArmed && index === savedRows.length - 1;
      if (shouldFail) {
        partialArmed = false;
        receipts.set(key, 'error');
        deliveries.push({ account, endpoint_id: row.id, attempt: 'failed' });
        return { id: row.id, channel: row.channel, status: 'error', error: 'disposable endpoint failure' };
      }
      receipts.set(key, 'sent');
      deliveries.push({ account, endpoint_id: row.id, attempt: 'sent' });
      return { id: row.id, channel: row.channel, status: 'sent' };
    });
    const sent = endpointResults.filter(row => row.status === 'sent').length;
    const errors = endpointResults.filter(row => row.status === 'error').length;
    return response(res, { channel: 'multiple', endpoints: endpointResults, aggregate: errors && sent ? 'partial' : errors ? 'error' : sent ? 'sent' : 'skipped' });
  }
  return response(res, { detail: `unhandled fixture route ${req.method} ${url.pathname}` }, 404);
}

await withCopalBrowser({ page, request }, async ({ evaluate, until, url, cdp }) => {
  const navigate = async target => {
    try { await cdp('Page.navigate', { url: target }); }
    catch (error) {
      if (!String(error.message).includes('Inspected target navigated or closed')) throw error;
    }
  };
  await until('window.__settingsError || document.querySelector("#set-reminder-endpoints-list") != null');
  assert.equal(await evaluate('window.__settingsError'), undefined);
  await until('document.querySelector("#set-reminder-endpoints-msg") != null');
  await until('document.querySelector("#set-reminder-channel-hint").textContent.includes("Reminders appear")', 'initial reminder settings load');
  assert.equal(await evaluate('document.body.dataset.accountId'), 'alice');
  assert.equal(await evaluate('document.querySelectorAll(".reminder-endpoint-row").length'), 0);

  // Add two destinations through the real Settings row controls.  The second
  // row is changed to a webhook and tested while its draft is still visible,
  // proving Test does not wait for or fan out a debounced save.
  await evaluate('document.querySelector("#set-reminder-add-endpoint").click()');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 1', 'first reminder endpoint row');
  await evaluate('document.querySelector("#set-reminder-add-endpoint").click()');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 2');
  await evaluate(`(() => {
    const row = document.querySelectorAll('.reminder-endpoint-row')[1];
    const channel = row.querySelector('.reminder-endpoint-channel');
    channel.value = 'webhook';
    channel.dispatchEvent(new Event('change', { bubbles:true }));
    const integration = row.querySelector('.reminder-endpoint-webhook-integration');
    integration.value = 'hook-main';
    integration.dispatchEvent(new Event('change', { bubbles:true }));
    row.querySelector('.reminder-endpoint-test').click();
  })()`);
  await until('document.querySelectorAll(".reminder-endpoint-row")[1].querySelector(".reminder-endpoint-status").textContent.includes("Test sent")');
  const testBodies = await fetch(`${url}fixture/state`).then(result => result.json());
  assert.equal(testBodies.tests.length, 1, JSON.stringify(testBodies));
  assert.match(testBodies.tests[0].note_id, /^test-/);
  assert.equal(testBodies.tests[0].channel, 'webhook');
  assert.equal(testBodies.tests[0].webhook_integration_id, 'hook-main');

  await until('document.querySelector("#set-reminder-endpoints-msg").textContent === "Saved"', 'two endpoint rows saved');
  const savedAlice = await fetch(`${url}api/auth/settings`, { headers: { 'X-Fixture-Account': 'alice' } }).then(result => result.json());
  assert.equal(savedAlice.reminder_endpoints.endpoints.length, 2, JSON.stringify(savedAlice));
  const aliceIds = savedAlice.reminder_endpoints.endpoints.map(row => row.id);
  assert.equal(new Set(aliceIds).size, 2, JSON.stringify(savedAlice));

  // Close/reopen the production Settings window, then perform a true page
  // reload. Both lifecycle boundaries must retain the same endpoint IDs and
  // order from the owner-scoped server projection.
  await evaluate('settingsModule.close()');
  await new Promise(resolve => setTimeout(resolve, 300));
  await evaluate('settingsModule.open("reminders")');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 2', 'reminder rows after Settings remount');
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".reminder-endpoint-row")].map(row => row.querySelector(".reminder-endpoint-channel").value)'), ['browser', 'webhook']);

  await navigate(`${url}?account=alice&reload=1`);
  await until('location.search.includes("reload=1") && document.querySelector("#set-reminder-endpoints-list") != null', 'reminder rows after page reload');
  await until('document.querySelector("#set-reminder-channel-hint").textContent.includes("Reminders appear")', 'reloaded reminder settings');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 2');
  const reloadedAlice = await evaluate('[...document.querySelectorAll(".reminder-endpoint-row")].map(row => row.querySelector(".reminder-endpoint-channel").value)');
  assert.deepEqual(reloadedAlice, ['browser', 'webhook']);
  const reloadedAliceIds = await evaluate('[...document.querySelectorAll(".reminder-endpoint-row")].map(row => JSON.parse(document.querySelector("#set-reminder-endpoints").value).find(item => item.channel === row.querySelector(".reminder-endpoint-channel").value)?.id)');
  assert.deepEqual(reloadedAliceIds, aliceIds);

  // Bob starts with an independent list, saves one row, and cannot see
  // Alice's two rows. Switching back to Alice still restores her IDs.
  await navigate(`${url}?account=bob`);
  await until('location.search.includes("account=bob") && document.querySelector("#set-reminder-endpoints-list") != null');
  await until('document.querySelector("#set-reminder-channel-hint").textContent.includes("Reminders appear")', 'Bob reminder settings load');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 0', 'Bob owner-scoped empty settings');
  await evaluate('document.querySelector("#set-reminder-add-endpoint").click()');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 1');
  await until('document.querySelector("#set-reminder-endpoints-msg").textContent === "Saved"', 'Bob endpoint saved');
  const savedBob = await fetch(`${url}api/auth/settings`, { headers: { 'X-Fixture-Account': 'bob' } }).then(result => result.json());
  assert.equal(savedBob.reminder_endpoints.endpoints.length, 1, JSON.stringify(savedBob));
  await navigate(`${url}?account=alice&account-check=1`);
  await until('location.search.includes("account-check=1") && document.querySelector("#set-reminder-endpoints-list") != null');
  await until('document.querySelector("#set-reminder-channel-hint").textContent.includes("Reminders appear")', 'Alice reminder settings after account switch');
  await until('document.querySelectorAll(".reminder-endpoint-row").length === 2', 'Alice rows restored after account switch');

  // A shared occurrence first succeeds for Alice's browser row and fails at
  // the webhook row. The retry reuses production-compatible receipt keys and
  // therefore calls only the previously failed endpoint.
  await fetch(`${url}fixture/arm-partial?account=alice`);
  const first = await evaluate(`fetch('/api/notes/fire-reminder', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ note_id:'note-1', occurrence_key:'note-1:fixture', title:'Fixture reminder', body:'Body' }) }).then(response => response.json())`);
  assert.equal(first.aggregate, 'partial', JSON.stringify(first));
  assert.deepEqual(first.endpoints.map(row => row.status), ['sent', 'error']);
  const second = await evaluate(`fetch('/api/notes/fire-reminder', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ note_id:'note-1', occurrence_key:'note-1:fixture', title:'Fixture reminder', body:'Body' }) }).then(response => response.json())`);
  assert.equal(second.aggregate, 'sent', JSON.stringify(second));
  assert.deepEqual(second.endpoints.map(row => row.status), ['skipped', 'sent']);
  const ledger = await fetch(`${url}fixture/state`).then(result => result.json());
  const aliceDeliveryIds = ledger.deliveries.filter(row => row.account === 'alice').map(row => `${row.endpoint_id}:${row.attempt}`);
  assert.deepEqual(aliceDeliveryIds, [`${aliceIds[0]}:sent`, `${aliceIds[1]}:failed`, `${aliceIds[1]}:sent`]);

  console.log(JSON.stringify({
    passed: [
      'real Settings module creates and saves two reminder rows with stable IDs',
      'unsaved webhook draft Test uses an isolated test-* occurrence',
      'Settings close/reopen and page reload retain endpoint order and IDs',
      'account-scoped reminder settings isolate Alice and Bob',
      'partial occurrence retry skips the successful endpoint and retries only the failed endpoint',
    ],
    fixture: 'disposable owner-scoped HTTP seam; no real notification or external transport',
    command: 'node tests/reminder_settings_production_browser_acceptance.mjs',
  }, null, 2));
});
