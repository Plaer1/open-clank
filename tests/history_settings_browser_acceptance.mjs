#!/usr/bin/env node

// Mounted Settings → History journey. The browser uses the production
// historySettings.js adapter and a disposable HTTP policy service; the service
// state lives outside the page so a Page.reload exercises settings persistence.
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><body data-account-id="alice">
<main>
  <section class="settings-panel" data-history-settings aria-label="History settings">
    <h2>History</h2>
    <div data-history-usage>Loading measured usage…</div>
    <input data-history-total id="history-total-bytes" type="number" min="1" step="1048576">
    <button type="button" data-history-save>Save target</button>
    <div data-history-inherited></div>
    <div data-history-scope-list></div>
    <div data-history-status role="status" aria-live="polite"></div>
  </section>
  <label>Editor buffer <textarea id="editor-buffer"></textarea></label>
</main>
<script>
  window.__switchHistoryAccount = async account => {
    document.body.dataset.accountId = account;
    document.dispatchEvent(new CustomEvent('openclank:auth-context-changed', { detail:{ accountId:account } }));
    await new Promise(resolve => setTimeout(resolve, 50));
  };
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const headers = new Headers(options.headers || {});
    headers.set('X-History-Account', document.body.dataset.accountId || 'alice');
    return nativeFetch(input, { ...options, headers });
  };
</script>
<script src="/static/js/historySettings.js"></script></body>`;

const accountState = {
  alice: {
    policy: {
      revision: 4,
      global: { revision:4, total_bytes:16 * 1024 * 1024, enabled:true },
      scopes: [
        { scope_id:'alice-workspace', kind:'workspace', workspace_id:'default', limit_bytes:4 * 1024 * 1024, enabled:true },
        { scope_id:'alice-files', kind:'directory', root_id:'files-root', display_path:'Project Files', limit_bytes:2 * 1024 * 1024, enabled:true },
      ],
    },
    status: { state:'history_paused_budget', history_paused:true },
  },
  bob: {
    policy: {
      revision:2,
      global: { revision:2, total_bytes:32 * 1024 * 1024, enabled:true },
      scopes: [{ scope_id:'bob-workspace', kind:'workspace', workspace_id:'bob-space', limit_bytes:8 * 1024 * 1024, enabled:true }],
    },
    status: { state:'ready', history_paused:false },
  },
};

let writes = 0;
let failureState = null;
const clone = value => JSON.parse(JSON.stringify(value));
const response = (res, value, status = 200) => {
  res.writeHead(status, { 'content-type':'application/json' });
  res.end(JSON.stringify(value));
  return true;
};
const snapshot = account => {
  const current = accountState[account] || accountState.alice;
  return {
    policy: clone(current.policy),
    usage: { physical_allocated_bytes: 6 * 1024 * 1024, logical_retained_bytes:5 * 1024 * 1024, retained_version_count:7, measurement_quality:'allocated' },
    status: clone(failureState?.account === account ? failureState.status : current.status),
    workspace_options: account === 'bob' ? ['bob-space', 'shared'] : ['default', 'project'],
    directory_options: [{ id: account === 'bob' ? 'bob-files' : 'files-root', label: account === 'bob' ? 'Bob Files' : 'Project Files' }],
  };
};

const request = async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/fixture/bump') {
    const account = url.searchParams.get('account') || 'alice';
    accountState[account].policy.revision += 1;
    accountState[account].policy.global.revision = accountState[account].policy.revision;
    return response(res, { revision:accountState[account].policy.revision });
  }
  if (url.pathname === '/fixture/status') {
    const account = url.searchParams.get('account') || 'alice';
    const state = url.searchParams.get('state') || 'ready';
    failureState = { account, status:{ state, history_paused:state !== 'ready' } };
    return response(res, { ok:true });
  }
  if (url.pathname === '/fixture/state') return response(res, { writes, accounts:clone(accountState), failureState:clone(failureState) });
  if (url.pathname !== '/api/history/settings') return false;
  const account = req.headers['x-history-account'] || 'alice';
  if (!accountState[account]) return response(res, { detail:'unknown account' }, 403);
  const current = accountState[account];
  if (req.method === 'GET') return response(res, snapshot(account));
  if (req.method !== 'PUT') return response(res, { detail:'method not allowed' }, 405);
  let body = '';
  for await (const chunk of req) body += chunk;
  const payload = JSON.parse(body || '{}');
  if (payload.expected_revision !== current.policy.revision) return response(res, { detail:{ code:'stale_revision', message:'stale history policy revision' } }, 409);
  const patch = payload.policy || {};
  const total = patch.global?.total_bytes;
  if (total != null && (!Number.isFinite(Number(total)) || Number(total) <= 0)) return response(res, { detail:{ code:'invalid_global_limit', message:'global limit must be greater than zero' } }, 422);
  const scopes = Array.isArray(patch.scopes) ? patch.scopes : current.policy.scopes;
  if (scopes.some(scope => !Number.isFinite(Number(scope.limit_bytes)) || Number(scope.limit_bytes) <= 0)) return response(res, { detail:{ code:'invalid_scope_limit', message:'scope limit must be greater than zero' } }, 422);
  writes += 1;
  const revision = current.policy.revision + 1;
  current.policy = {
    ...current.policy,
    ...(patch.global ? { global:{ ...current.policy.global, ...patch.global, revision } } : { global:{ ...current.policy.global, revision } }),
    scopes: scopes.map((scope, index) => ({ ...scope, kind:String(scope.kind || '').toLowerCase(), scope_id:scope.scope_id || `${account}-scope-${index + 1}` })),
    revision,
  };
  current.status = { state:'ready', history_paused:false };
  return response(res, snapshot(account));
};

await withCopalBrowser({ page, request }, async ({ evaluate, until, url, cdp }) => {
  await until('document.querySelector("[data-history-settings] [data-history-usage]")?.textContent.includes("allocated")');
  assert.match(await evaluate('document.querySelector("[data-history-status]").textContent'), /paused/i);
  assert.equal(await evaluate('document.querySelector("#history-total-bytes").value'), String(16 * 1024 * 1024));
  assert.equal(await evaluate('document.querySelectorAll("[data-history-scope-id]").length'), 2);

  const draft = 'ordinary Editor draft survives history status refresh';
  await evaluate(`(() => { const editor = document.querySelector('#editor-buffer'); editor.value = ${JSON.stringify(draft)}; editor.dispatchEvent(new Event('input', { bubbles:true })); })()`);
  const writesBeforeInvalid = writes;
  await evaluate('document.querySelector("#history-total-bytes").value = "0"; document.querySelector("[data-history-save]").click()');
  await until('document.querySelector("[data-history-status]").textContent.includes("greater than zero")');
  assert.equal(writes, writesBeforeInvalid, 'invalid limit is rejected before transport');
  const structured422 = await evaluate(`(async () => {
    try {
      await OpenClankHistorySettings.save({ global:{ total_bytes:0 } }, 4);
      return null;
    } catch (error) { return { message:error.message, code:error.code, status:error.status }; }
  })()`);
  assert.deepEqual(structured422, { message:'global limit must be greater than zero', code:'invalid_global_limit', status:422 });

  // Add one workspace and one allowlisted Files-root limit, remove the old
  // Files-root row, and persist all changes through one optimistic revision.
  await evaluate(`(() => {
    const list = document.querySelector('[data-history-scope-list]');
    [...list.querySelectorAll('button')].find(button => button.textContent === 'Add workspace limit').click();
    const workspace = list.querySelector('[data-history-new-workspace]');
    workspace.querySelector('[data-history-workspace-limit]').value = '5242880';
    [...list.querySelectorAll('button')].find(button => button.textContent === 'Add directory limit').click();
    const directory = list.querySelector('[data-history-new-directory]');
    directory.querySelector('[data-history-directory-limit]').value = '3145728';
    const old = [...list.querySelectorAll('[data-history-scope-id]')].find(input => input.dataset.historyScopeId === 'alice-files');
    old?.parentElement?.querySelector('button')?.click();
    document.querySelector('#history-total-bytes').value = '33554432';
  })()`);
  await evaluate('document.querySelector("[data-history-save]").click()');
  await until('document.querySelector("[data-history-status]").textContent.includes("saved")');
  assert.equal(writes, writesBeforeInvalid + 1);
  assert.equal(await evaluate('document.querySelector("#editor-buffer").value'), draft);
  const persisted = await fetch(`${url}fixture/state`).then(result => result.json());
  assert.equal(persisted.accounts.alice.policy.global.total_bytes, 33554432);
  assert.equal(persisted.accounts.alice.policy.scopes.some(scope => scope.kind === 'directory' && scope.limit_bytes === 3145728), true);
  assert.equal(persisted.accounts.alice.policy.scopes.some(scope => scope.scope_id === 'alice-files'), false);

  // A stale optimistic revision reports a conflict and leaves the editor and
  // the settings view mounted for a safe retry.
  await fetch(`${url}fixture/bump?account=alice`);
  await evaluate('document.querySelector("#history-total-bytes").value = "34603008"; document.querySelector("[data-history-save]").click()');
  await until('document.querySelector("[data-history-status]").textContent.includes("stale history policy revision")');
  assert.equal(await evaluate('document.querySelector("#editor-buffer").value'), draft);

  await evaluate('OpenClankHistorySettings.mount(document.querySelector("[data-history-settings]"), { force:true })');
  await until('document.querySelector("#history-total-bytes").value === "33554432"');
  assert.equal(await evaluate('document.querySelector("#editor-buffer").value'), draft);

  // Account changes reload policy/usage into the same panel without adopting
  // Alice's workspace or Files-root scopes.
  await evaluate('window.__switchHistoryAccount("bob")');
  await until('document.querySelector("#history-total-bytes").value === "33554432"');
  await until('document.querySelector("[data-history-scope-id]")?.dataset.historyScopeId === "bob-workspace"');
  assert.equal(await evaluate('document.querySelectorAll("[data-history-scope-id]").length'), 1);
  assert.match(await evaluate('document.querySelector("[data-history-status]").textContent'), /ready/i);
  await evaluate('window.__switchHistoryAccount("alice")');
  await until('document.querySelectorAll("[data-history-scope-id]").length === 3');
  assert.equal(await evaluate('[...document.querySelectorAll("[data-history-scope-id]")].some(node => node.dataset.historyScopeId === "bob-workspace")'), false);
  assert.equal(await evaluate('[...document.querySelectorAll("[data-history-scope-id]")].some(node => node.dataset.historyScopeId === "alice-workspace")'), true);

  // History can be paused or fail independently while an ordinary Editor
  // buffer remains present and editable.
  await fetch(`${url}fixture/status?account=alice&state=history_failed_io`);
  await evaluate('OpenClankHistorySettings.mount(document.querySelector("[data-history-settings]"), { force:true })');
  await until('document.querySelector("[data-history-status]").textContent.includes("failed")');
  assert.equal(await evaluate('document.querySelector("#editor-buffer").value'), draft);
  await cdp('Emulation.setPageScaleFactor', { pageScaleFactor:2 });
  const zoomed = await evaluate(`(() => { const r=document.querySelector('[data-history-settings]').getBoundingClientRect(); return { width:r.width, right:r.right, viewport:innerWidth }; })()`);
  assert(zoomed.width > 0 && zoomed.right > 0, JSON.stringify(zoomed));

  // The HTTP fixture retains state outside the document, so a reload proves
  // the persisted policy is restored by the mounted adapter.
  await cdp('Page.reload', { ignoreCache:true });
  await until('document.querySelector("#history-total-bytes").value === "33554432"');
  assert.equal(await evaluate('document.querySelectorAll("[data-history-scope-id]").length'), 3);
  console.log(JSON.stringify({ passed:[
    'account-scoped measured usage and paused/failed status',
    'invalid limits rejected before save',
    'workspace and Files-root budgets add/remove/persist',
    'stale CAS keeps the panel and Editor draft intact',
    'account switch isolates policies and workspaces',
    '200% zoom and reload persistence',
  ] }, null, 2));
});
