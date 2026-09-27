#!/usr/bin/env node

// Disposable mounted-app journey for the Lore history settings boundary.
//
// This test intentionally starts the real FastAPI app and the real Rust
// openclank-history worker with a temporary data root.  The fixture test next
// to it remains useful for quick presentation coverage, but it cannot prove
// that Settings, authentication, Redb/Lore persistence, and the worker agree
// on the same state.

import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const repo = process.cwd();
const python = path.join(repo, 'venv', 'bin', 'python');
const worker = process.env.OPENCLANK_HISTORY_TEST_BIN
  || path.join(repo, 'packages', 'openclank-history', 'target', 'debug', 'openclank-history-service');
const copalBridge = process.env.COPAL_BRIDGE_COMMAND
  || path.join(repo, 'packages', 'Copal', 'rust', 'copal-db', 'target', 'release', 'copal-bridge');
const chrome = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
].find(fs.existsSync);

if (!fs.existsSync(python) || !fs.existsSync(worker) || !chrome) {
  console.log(JSON.stringify({ skipped: 'requires venv/bin/python, the debug history worker, and Chrome' }));
  process.exit(0);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function passwordHash(password) {
  return execFileSync(python, ['-c', 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())', password], {
    cwd: repo,
    encoding: 'utf8',
  }).trim();
}

async function waitForHealth(base, child, logs) {
  const deadline = Date.now() + 120000;
  while (Date.now() < deadline) {
    if (child.exitCode != null) {
      throw new Error(`FastAPI exited during startup (${child.exitCode}): ${logs().slice(-5000)}`);
    }
    try {
      const response = await fetch(`${base}/api/health`);
      if (response.ok) return;
    } catch (_) {}
    await delay(100);
  }
  throw new Error(`FastAPI health timeout: ${logs().slice(-5000)}`);
}

function startApp({ data, history, socket, registry, files, port }) {
  const environment = {
    ...process.env,
    DEBUG: 'false',
    OPENCLANK_DEBUG: 'false',
    OPENCLANK_RECOVERY_MODE: 'true',
    AUTH_ENABLED: 'true',
    OPEN_CLANK_AGENT_DRIVE: 'disabled',
    OPEN_CLANK_DATA_DIR: data,
    DATABASE_URL: `sqlite:///${path.join(data, 'app.db')}`,
    OPENCLANK_HISTORY_SERVICE_BIN: worker,
    OPENCLANK_HISTORY_SOCKET: socket,
    OPENCLANK_HISTORY_SETTINGS_FILE: path.join(data, 'history-settings.json'),
    OPENCLANK_HISTORY_ROOT: history,
    OPENCLANK_HISTORY_HOST_ROOT: files,
    OPENCLANK_HISTORY_RECEIPT_ROOT: path.join(history, 'restore-receipts'),
    OPENCLANK_HISTORY_RESOURCE_MAP: path.join(history, 'resource-map.json'),
    ODYSSEUS_FILES_REGISTRY: registry,
    COPAL_STORAGE: 'redb',
    COPAL_DATA_DIR: path.join(data, 'copal'),
    COPAL_BRIDGE_COMMAND: copalBridge,
    PYTHONUNBUFFERED: '1',
  };
  let output = '';
  const child = spawn(python, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(port)], {
    cwd: repo,
    env: environment,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const collect = chunk => { output += String(chunk); if (output.length > 50000) output = output.slice(-50000); };
  child.stdout.on('data', collect);
  child.stderr.on('data', collect);
  return { child, logs: () => output };
}

function findHistoryWorkerPid(socketPath, dataRoot) {
  const rows = execFileSync('ps', ['-axo', 'pid=,command='], { encoding:'utf8' }).split('\n');
  const row = rows.find(line => line.includes('openclank-history-service') && (line.includes(socketPath) || line.includes(dataRoot)));
  const match = row?.trim().match(/^(\d+)/);
  return match ? Number(match[1]) : null;
}

async function stopApp(handle) {
  if (!handle?.child) return;
  if (handle.child.exitCode != null) {
    handle.child.stdout?.destroy();
    handle.child.stderr?.destroy();
    handle.child.removeAllListeners();
    return;
  }
  const exited = new Promise(resolve => {
    if (handle.child.exitCode != null) resolve();
    else handle.child.once('exit', resolve);
  });
  handle.child.kill('SIGTERM');
  for (let i = 0; i < 100 && handle.child.exitCode == null; i += 1) await delay(100);
  if (handle.child.exitCode == null) handle.child.kill('SIGKILL');
  await Promise.race([exited, delay(3000)]);
  handle.child.stdout?.destroy();
  handle.child.stderr?.destroy();
  handle.child.removeAllListeners();
}

async function openBrowser(base) {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-history-browser-'));
  const browser = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox',
    '--no-first-run', '--no-default-browser-check', `--user-data-dir=${profile}`,
    '--remote-debugging-port=0', 'about:blank',
  ], { stdio: ['ignore', 'pipe', 'pipe'] });
  let diagnostic = '';
  let port = null;
  const capture = chunk => {
    diagnostic += String(chunk);
    const match = diagnostic.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
    if (match) port = Number(match[1]);
  };
  browser.stdout.on('data', capture);
  browser.stderr.on('data', capture);
  const deadline = Date.now() + 30000;
  while (!port && Date.now() < deadline && browser.exitCode == null) await delay(25);
  assert(port, `Chrome DevTools did not start: ${diagnostic.slice(-2000)}`);
  const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(`${base}/login`)}`, { method: 'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    ws.addEventListener('open', resolve, { once: true });
    ws.addEventListener('error', reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  ws.addEventListener('close', () => {
    for (const [id, request] of pending) request.reject(new Error(`CDP connection closed with request ${id} pending`));
    pending.clear();
  }, { once: true });
  ws.addEventListener('error', event => {
    for (const [id, request] of pending) request.reject(new Error(`CDP connection error with request ${id} pending: ${event.message || 'unknown error'}`));
    pending.clear();
  }, { once: true });
  ws.addEventListener('message', event => {
    const value = JSON.parse(event.data);
    if (value.id && pending.has(value.id)) {
      pending.get(value.id).settle(value);
      pending.delete(value.id);
    }
  });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`CDP timeout: ${method}`));
    }, 30000);
    pending.set(id, {
      reject,
      settle: value => {
        clearTimeout(timer);
        if (value.error) reject(new Error(`${method} ${value.error.message}`));
        else resolve(value.result);
      },
    });
    ws.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async (expression, awaitPromise = true) => {
    const result = await cdp('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
    if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
    return result.result?.value;
  };
  const click = async selector => {
    await evaluate(`(() => { const node = document.querySelector(${JSON.stringify(selector)}); if (!node) throw new Error('click target not found: ' + ${JSON.stringify(selector)}); node.dispatchEvent(new Event('click', { bubbles:true, cancelable:true })); })()`);
  };
  const navigatePage = async url => {
    try { await cdp('Page.navigate', { url }); }
    catch (error) { if (!String(error.message).includes('Inspected target navigated or closed')) throw error; }
    const expected = new URL(url);
    await until(`location.origin === ${JSON.stringify(expected.origin)} && location.pathname === ${JSON.stringify(expected.pathname)}`);
    await until('document.readyState === "complete"');
  };
  const until = async (expression, label = expression, timeout = 120000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {
        if (await evaluate(expression)) return;
      } catch (error) {
        if (!String(error.message).includes('Inspected target navigated or closed')) throw error;
      }
      await delay(100);
    }
    throw new Error(`Browser timeout (${label}, ${timeout}ms): ${expression}`);
  };
  await cdp('Runtime.enable');
  await cdp('Page.enable');
  await cdp('Network.enable');
  await cdp('Network.setCacheDisabled', { cacheDisabled: true });
  return {
    browser,
    profile,
    ws,
    cdp,
    evaluate,
    click,
    navigatePage,
    until,
    async close() {
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        const closed = new Promise(resolve => ws.addEventListener('close', resolve, { once: true }));
        ws.close();
        await Promise.race([closed, delay(1000)]);
      }
      if (browser.exitCode == null) browser.kill('SIGTERM');
      for (let i = 0; i < 40 && browser.exitCode == null; i += 1) await delay(25);
      if (browser.exitCode == null) browser.kill('SIGKILL');
      await Promise.race([new Promise(resolve => {
        if (browser.exitCode != null) resolve();
        else browser.once('exit', resolve);
      }), delay(2000)]);
      browser.stdout?.destroy();
      browser.stderr?.destroy();
      browser.removeAllListeners();
      fs.rmSync(profile, { recursive: true, force: true, maxRetries: 8, retryDelay: 100 });
    },
  };
}

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-history-production-'));
const data = path.join(temporary, 'data');
const history = path.join(data, 'history');
const files = path.join(temporary, 'files');
const registry = path.join(data, 'odysseus-file-roots.json');
const socket = path.join(data, 'history.sock');
const password = 'history-dogfood-password';
let appHandle;
let browser;
let historyWorkerPid;
let cleanupPromise;

async function cleanup() {
  await browser?.close().catch(() => {});
  await stopApp(appHandle).catch(() => {});
  fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 8, retryDelay: 100 });
}

function handleSignal(signal) {
  if (!cleanupPromise) cleanupPromise = cleanup();
  cleanupPromise.finally(() => process.exit(128 + (signal === 'SIGINT' ? 2 : 15)));
}

process.once('SIGINT', handleSignal);
process.once('SIGTERM', handleSignal);

try {
  const progress = label => console.error(`[history-production] ${label}`);
  progress('prepare disposable data');
  fs.mkdirSync(history, { recursive: true });
  fs.mkdirSync(files, { recursive: true });
  fs.mkdirSync(path.join(history, 'restore-receipts'), { recursive: true });
  // The worker refuses to adopt an unmarked non-empty history directory.
  fs.writeFileSync(path.join(history, '.openclank-history-format'), 'openclank-history\nformat=1\n');
  fs.writeFileSync(path.join(files, 'seed.txt'), 'seed\n');
  const hash = passwordHash(password);
  fs.writeFileSync(path.join(data, 'auth.json'), JSON.stringify({
    signup_enabled: false,
    users: {
      alice: { account_id: 'account-alice', password_hash: hash, created: Date.now() / 1000, is_admin: true },
      bob: { account_id: 'account-bob', password_hash: hash, created: Date.now() / 1000, is_admin: false },
    },
  }, null, 2));
  fs.writeFileSync(registry, JSON.stringify({
    version: 1,
    generation: 1,
    roots: {
      'root-project-files': {
        id: 'root-project-files', owner_id: 'alice', kind: 'recursive_directory',
        canonical_path: files, display_path: 'Project Files', enabled: true,
        capabilities: ['read', 'write'], availability: 'available',
        platform_identity: { volume_id: 'test', file_id: null, device: 1, inode: 1, case_sensitive: true },
        last_validated_unix_ms: Date.now(),
      },
    },
    visibility_assignments: {},
  }, null, 2));
  // Seed the canonical Files policy repository with an active non-default
  // workspace. History must discover this before any Lore scope exists.
  execFileSync(python, ['-c', `
from src.openclank.file_policy import FilePolicyRepository
import sys
repo = FilePolicyRepository()
repo.create_location(
    actor_subject_id='account-alice', path=sys.argv[1], kind='directory',
    capabilities=('read', 'write'), display_path='Project Files',
    location_id='location-project-files',
)
repo.create_workspace(
    actor_subject_id='account-alice', owner_subject_id='account-alice',
    location_id='location-project-files', name='Canonical Project',
    workspace_id='canonical-project',
)
`, files], {
    cwd: repo,
    env: { ...process.env, OPEN_CLANK_DATA_DIR: data, DATABASE_URL: `sqlite:///${path.join(data, 'app.db')}` },
    stdio: 'pipe',
  });

  const port = await freePort();
  const base = `http://127.0.0.1:${port}`;
  appHandle = startApp({ data, history, socket, registry, files, port });
  await waitForHealth(base, appHandle.child, appHandle.logs);
  progress('FastAPI healthy');
  browser = await openBrowser(base);
  progress('browser connected');
  const { evaluate, until, cdp, click, navigatePage } = browser;
  const primaryModifier = await evaluate('/Mac|iPhone|iPad|iPod/i.test(navigator.userAgentData?.platform || navigator.platform) ? 4 : 2');

  async function login(username) {
    // Start each login from a fresh document so background requests from a
    // previous authenticated page cannot race the new session cookie. This
    // matters after an app restart, when an old page can still have a poll in
    // flight while the browser is switching accounts.
    await navigatePage(`${base}/login`);
    await until('document.body != null');
    const result = await evaluate(`fetch('/api/auth/login', { method:'POST', credentials:'same-origin', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username:${JSON.stringify(username)}, password:${JSON.stringify(password)}, remember:true }) }).then(async response => ({ status:response.status, body:await response.json().catch(() => ({})) }))`);
    assert.equal(result.status, 200, `${username} login failed: ${JSON.stringify(result)}`);
    assert.equal(result.body.ok, true, `${username} login response: ${JSON.stringify(result)}`);
    await navigatePage(`${base}/`);
    await until('document.querySelector("#rail-settings") != null');
    await until(`document.body.dataset.accountId === ${JSON.stringify(username === 'alice' ? 'account-alice' : 'account-bob')}`);
  }

  async function historyPanel({ force = true } = {}) {
    // The rail button only reveals the sidebar. Open the mounted Settings
    // module directly so this journey exercises the same production command
    // used by the user bar, slash command, and keyboard shortcut.
    await evaluate('import("/static/js/settings.js").then(module => module.open("history"))');
    await until('document.querySelector("#settings-modal:not(.hidden)") != null');
    if (!force) return;
    await evaluate(`(async () => {
      for (let attempt = 0; attempt < 4; attempt += 1) {
        try {
          return await OpenClankHistorySettings.mount(document.querySelector('[data-history-settings]'), { force:true });
        } catch (error) {
          if (error?.name !== 'AbortError' || attempt === 3) throw error;
          await new Promise(resolve => setTimeout(resolve, 100));
        }
      }
    })()`);
    await click('[data-settings-tab="history"]');
    await until('document.querySelector("[data-history-settings] [data-history-usage]")?.textContent.includes("allocated")');
    await until('document.querySelector("[data-history-status]")?.textContent.length > 0');
  }

  await login('alice');
  progress('alice logged in');
  await historyPanel();
  progress('alice history panel loaded');
  historyWorkerPid = findHistoryWorkerPid(socket, temporary);
  assert(historyWorkerPid, 'fixture history worker did not start');
  assert.match(await evaluate('document.querySelector("[data-history-status]").textContent'), /ready/i);
  const aliceWorkspaceOptions = await evaluate(`(async () => (await fetch('/api/history/settings')).json())().then(value=>value.workspace_options)`);
  assert(aliceWorkspaceOptions.includes('canonical-project'), JSON.stringify(aliceWorkspaceOptions));
  assert.equal(await evaluate('document.querySelectorAll("[data-history-scope-id]").length'), 0);
  assert.equal(await evaluate('document.querySelector("[data-history-settings]")?.closest("[data-settings-panel]")?.dataset.settingsScope'), 'shared-policy');
  const firstUsage = await evaluate('document.querySelector("[data-history-usage]").textContent');
  assert.match(firstUsage, /allocated/i);

  // The mounted document editor owns this dirty buffer while settings are
  // edited.  It is the real app panel, not a standalone textarea fixture.
  // The app may retain the document module's open state while a route or
  // window transition has removed the pane.  `openPanel()` intentionally
  // returns early in that state; use the real mount lifecycle so this
  // authenticated journey cannot wait forever on a missing editor surface.
  await evaluate('window.documentModule.ensurePaneMounted()');
  await until('document.querySelector("#doc-editor-textarea") != null');
  progress('editor panel loaded');
  const draft = 'ordinary Editor draft survives history pause';
  await evaluate(`(() => { const editor=document.querySelector('#doc-editor-textarea'); editor.value=${JSON.stringify(draft)}; editor.dispatchEvent(new Event('input',{bubbles:true})); editor.focus(); })()`);
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea").value'), draft);
  await historyPanel({ force:false });
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea").value'), draft);

  // Add one workspace and one allowlisted Files-root policy through the real
  // Settings panel, then persist it in the Rust worker's Redb/Lore catalog.
  await click('[data-history-action="add-workspace"]');
  await until('document.querySelector("[data-history-new-workspace]") != null');
  await evaluate(`(() => {
    const list=document.querySelector('[data-history-scope-list]');
    const workspace=list.querySelector('[data-history-new-workspace]');
    workspace.querySelector('[data-history-workspace]').value='canonical-project';
    workspace.querySelector('[data-history-workspace-limit]').value='5242880';
    const directoryButton=[...list.querySelectorAll('button')].find(button=>button.textContent==='Add directory limit');
    directoryButton.dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
    const directory=list.querySelector('[data-history-new-directory]');
    if (!directory || !directory.querySelector('[data-history-directory] option[value="root-project-files"]')) throw new Error('allowlisted Files root was not offered');
    directory.querySelector('[data-history-directory-limit]').value='3145728';
    document.querySelector('[data-history-save]').dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
  })()`);
  await until('document.querySelector("[data-history-status]").textContent.includes("saved")');
  progress('alice scopes saved');
  const aliceAfterAdd = await evaluate(`(async () => {
    const response=await fetch('/api/history/settings');
    return response.json();
  })()`);
  assert.equal(aliceAfterAdd.policy.scopes.length, 2, JSON.stringify(aliceAfterAdd));
  assert.equal(aliceAfterAdd.policy.scopes.some(scope=>scope.kind.toLowerCase()==='workspace' && scope.workspace_id==='canonical-project'), true);
  assert.equal(aliceAfterAdd.policy.scopes.some(scope=>scope.kind.toLowerCase()==='directory' && scope.root_id==='root-project-files'), true);
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea").value'), draft);

  // Remove the Files-root scope through the mounted row and prove that the
  // next optimistic save does not resurrect the removed row.
  await evaluate(`(() => {
    const input=[...document.querySelectorAll('[data-history-scope-id]')].find(node=>node.dataset.historyScopeId && node.closest('.settings-row')?.textContent.includes('Directory'));
    input?.closest('.settings-row')?.querySelector('button')?.dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
    document.querySelector('[data-history-save]').dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
  })()`);
  await until('document.querySelector("[data-history-status]").textContent.includes("saved")');
  progress('alice directory removed');
  const aliceAfterRemove = await evaluate(`(async () => (await fetch('/api/history/settings')).json())()`);
  assert.equal(aliceAfterRemove.policy.scopes.length, 1, JSON.stringify(aliceAfterRemove));
  assert.equal(aliceAfterRemove.policy.scopes[0].kind.toLowerCase(), 'workspace');
  assert.equal(aliceAfterRemove.policy.scopes[0].workspace_id, 'canonical-project');

  // Local validation blocks an invalid target without transport, and the real
  // route returns a real optimistic-concurrency 409 for a stale revision.
  const beforeInvalid = await evaluate(`(async () => (await fetch('/api/history/settings')).json())().then(value=>value.policy.revision)`);
  await evaluate(`(() => { const input=document.querySelector('[data-history-total]'); input.value='0'; document.querySelector('[data-history-save]').dispatchEvent(new Event('click', { bubbles:true, cancelable:true })); })()`);
  await until('document.querySelector("[data-history-status]").textContent.includes("greater than zero")');
  progress('invalid target shown');
  const stale = await evaluate(`(async revision => {
    const response=await fetch('/api/history/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({expected_revision:revision-1, policy:{scopes:[]}})});
    return { status:response.status, body:await response.json().catch(()=>({})) };
  })(${beforeInvalid})`);
  assert.equal(stale.status, 409, JSON.stringify(stale));
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea").value'), draft);

  // Force a budget-full status using the real worker.  Status derives from
  // physical Redb/Lore allocation and the installation policy; the editor
  // buffer remains available while capture is paused.
  const revision = await evaluate(`(async () => (await fetch('/api/history/settings')).json())().then(value=>value.policy.revision)`);
  const lowTarget = await evaluate(`(async revision => {
    const response=await fetch('/api/history/settings', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({expected_revision:revision, policy:{global:{total_bytes:1}}})});
    return { status:response.status, body:await response.json().catch(()=>({})) };
  })(${revision})`);
  assert.equal(lowTarget.status, 200, JSON.stringify(lowTarget));
  await evaluate('OpenClankHistorySettings.mount(document.querySelector("[data-history-settings]"), { force:true })');
  await until('document.querySelector("[data-history-status]").textContent.includes("paused")');
  progress('budget pause shown');
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea").value'), draft);

  // Continue the same paused-history session through Copal's actual Redb
  // bridge and unified CodeMirror editor. The Files resource handoff below
  // uses only its opaque ResourceRef, as a user-facing Files action does.
  const copalDocument = await evaluate(`(async () => {
    const response = await fetch('/api/copal/documents?workspace=default', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ name:'Acceptance/History Dogfood.md', kind:'markdown', content:'# History dogfood\\n\\nInitial body.\\n' }) });
    return { status:response.status, body:await response.json().catch(()=>({})) };
  })()`);
  assert.equal(copalDocument.status, 200, JSON.stringify(copalDocument));
  const copalId = copalDocument.body.doc.id;
  await navigatePage(`${base}/editor`);
  await until('document.querySelector("#copal-notes-modal:not(.hidden) .copal-notes-workspace") != null');
  await evaluate('document.querySelector("#copal-notes-modal button[aria-label=\\"Quick switcher\\"]").dispatchEvent(new Event("click", {bubbles:true}))');
  await until('document.querySelector("dialog.copal-quick-switcher[open] input") != null');
  await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='History Dogfood'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').dispatchEvent(new Event('click',{bubbles:true})); })()`);
  await until('document.querySelector("#copal-notes-modal .cm-editor") != null');
  assert.equal(await evaluate('document.querySelectorAll("#copal-notes-modal textarea.copal-editor").length'), 0);
  await evaluate('document.querySelector("#copal-notes-modal .cm-content").focus()');
  await cdp('Input.insertText', { text: '\nPAUSED-COPAL-SAVE' });
  await cdp('Input.dispatchKeyEvent', { type:'keyDown', key:'s', code:'KeyS', windowsVirtualKeyCode:83, modifiers:primaryModifier });
  await cdp('Input.dispatchKeyEvent', { type:'keyUp', key:'s', code:'KeyS', windowsVirtualKeyCode:83, modifiers:primaryModifier });
  await until(`fetch('/api/copal/documents/${encodeURIComponent(copalId)}?workspace=default').then(r=>r.json()).then(doc=>doc.text.includes('PAUSED-COPAL-SAVE'))`);
  const copalRoots = await evaluate(`(async () => (await fetch('/api/files-v1/roots?copal_workspace=default')).json())()`);
  const copalRoot = copalRoots.entries?.find(entry => entry.provider === 'copal');
  assert(copalRoot?.ref, JSON.stringify(copalRoots));
  const copalFolders = await evaluate(`(async () => (await fetch('/api/files-v1/children', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({parent_ref:${JSON.stringify(copalRoot.ref)},limit:100,sort:{},query:''})})).json())()`);
  const copalDocsFolder = copalFolders.entries?.find(entry => entry.name === 'Documents');
  assert(copalDocsFolder?.ref, JSON.stringify(copalFolders));
  const copalFiles = await evaluate(`(async () => (await fetch('/api/files-v1/children', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({parent_ref:${JSON.stringify(copalDocsFolder.ref)},limit:100,sort:{},query:'History Dogfood'})})).json())()`);
  const copalFile = copalFiles.entries?.find(entry => entry.name === 'Acceptance/History Dogfood.md');
  assert(copalFile?.ref, JSON.stringify(copalFiles));
  await evaluate(`window.copalModule.openResource(${JSON.stringify(copalFile.ref)})`);
  await until('document.querySelector("#copal-notes-modal .copal-note-tab.active")?.textContent.includes("History Dogfood") && document.querySelector("#copal-notes-modal .cm-editor")');
  assert.equal(await evaluate('document.querySelector("#copal-notes-modal .cm-editor") != null'), true);
  progress('paused Copal CodeMirror save and Files handoff passed');

  // Stop only this app's exact history worker. Copal content writes must keep
  // succeeding while the capture provider is unavailable.
  assert(historyWorkerPid, 'fixture history worker handle was lost');
  try { process.kill(historyWorkerPid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  await delay(250);
  const unavailable = await evaluate(`(async () => { const response=await fetch('/api/history/settings'); return { status:response.status, body:await response.json().catch(()=>({})) }; })()`);
  assert.equal(unavailable.status, 503, JSON.stringify(unavailable));
  await evaluate('document.querySelector("#copal-notes-modal .cm-content").focus()');
  await cdp('Input.insertText', { text: '\nUNAVAILABLE-COPAL-SAVE' });
  await cdp('Input.dispatchKeyEvent', { type:'keyDown', key:'s', code:'KeyS', windowsVirtualKeyCode:83, modifiers:primaryModifier });
  await cdp('Input.dispatchKeyEvent', { type:'keyUp', key:'s', code:'KeyS', windowsVirtualKeyCode:83, modifiers:primaryModifier });
  await until(`fetch('/api/copal/documents/${encodeURIComponent(copalId)}?workspace=default').then(r=>r.json()).then(doc=>doc.text.includes('UNAVAILABLE-COPAL-SAVE'))`);
  progress('unavailable history Copal save passed');
  // Bring the real history worker back before the remaining account and
  // restart assertions; this restart is also evidence that unavailable
  // capture does not poison the application process.
  await stopApp(appHandle);
  appHandle = startApp({ data, history, socket, registry, files, port });
  await waitForHealth(base, appHandle.child, appHandle.logs);
  await navigatePage(`${base}/`);
  await until('document.querySelector("#rail-settings") != null');
  progress('history worker restarted after unavailable save');

  // A separate authenticated account sees only its own scopes. It can change
  // workspace policy, while the installation target remains inherited.
  await login('bob');
  progress('bob logged in');
  await historyPanel();
  progress('bob history panel loaded');
  historyWorkerPid = findHistoryWorkerPid(socket, temporary);
  assert(historyWorkerPid, 'restarted fixture history worker did not start');
  assert.equal(await evaluate('document.querySelectorAll("[data-history-scope-id]").length'), 0);
  assert.equal(await evaluate('document.querySelector("[data-history-total]").disabled'), true);
  await evaluate(`(() => {
    const list=document.querySelector('[data-history-scope-list]');
    [...list.querySelectorAll('button')].find(button=>button.textContent==='Add workspace limit').dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
    const row=list.querySelector('[data-history-new-workspace]');
    row.querySelector('[data-history-workspace-limit]').value='2097152';
    document.querySelector('[data-history-save]').dispatchEvent(new Event('click', { bubbles:true, cancelable:true }));
  })()`);
  await until('document.querySelector("[data-history-status]").textContent.includes("saved")');
  progress('bob scope saved');
  const bobPolicy = await evaluate(`(async () => (await fetch('/api/history/settings')).json())()`);
  assert.equal(bobPolicy.policy.scopes.length, 1, JSON.stringify(bobPolicy));
  assert.equal(bobPolicy.policy.scopes[0].owner_account_id, 'account-bob');
  assert.equal(bobPolicy.policy.scopes[0].workspace_id, 'default');

  // Switch back without a fresh browser/profile and verify Alice's policy is
  // still isolated from Bob's. The same check also exercises cookie logout.
  await evaluate(`fetch('/api/auth/logout', { method:'POST', credentials:'same-origin' })`);
  await login('alice');
  progress('alice re-logged in');
  await historyPanel();
  progress('alice panel after account switch');
  const alicePolicy = await evaluate(`(async () => (await fetch('/api/history/settings')).json())()`);
  assert.equal(alicePolicy.policy.scopes.some(scope=>scope.owner_account_id === 'account-alice'), true, JSON.stringify(alicePolicy));
  // Alice is the installation administrator, so the authenticated admin
  // inventory includes Bob's scope. Bob's own request above is the isolation
  // assertion: it returned only account-bob scopes.
  assert.equal(await evaluate('document.querySelector("#doc-editor-textarea")?.value || ""'), '', 'logout/account switch clears unscoped Editor state');

  // Stop and restart the actual app over the same Redb/Lore directory. The
  // policy and account partition must be recoverable after process restart.
  try { process.kill(historyWorkerPid, 'SIGTERM'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  await stopApp(appHandle);
  progress('first app stopped');
  appHandle = startApp({ data, history, socket, registry, files, port });
  await waitForHealth(base, appHandle.child, appHandle.logs);
  progress('app restarted');
  await navigatePage(`${base}/`);
  await until('document.querySelector("#rail-settings") != null');
  await historyPanel();
  progress('reopened history panel');
  const reopened = await evaluate(`(async () => (await fetch('/api/history/settings')).json())()`);
  assert.equal(reopened.policy.scopes.some(scope=>scope.owner_account_id === 'account-alice' && scope.kind.toLowerCase()==='workspace'), true, JSON.stringify(reopened));
  assert.equal(reopened.policy.scopes.some(scope=>scope.owner_account_id === 'account-alice'), true, JSON.stringify(reopened));
  assert.match(await evaluate('document.querySelector("[data-history-usage]").textContent'), /allocated/i);

  console.log(JSON.stringify({ passed: [
    'mounted production Settings panel reads real authenticated Lore usage',
    'workspace and allowlisted Files-root limits add/remove through real API',
    'invalid target and stale CAS preserve the mounted Editor buffer',
    'real worker reports budget pause while Editor remains usable',
    'paused history preserves a real Copal CodeMirror save and Files ResourceRef handoff',
    'history service unavailability preserves a real Copal CodeMirror save',
    'cross-account policy isolation survives logout/login',
    'Redb/Lore policy survives app and worker restart',
  ], limitations: [
    'Unavailable capture is exercised with an exact history-worker termination; a separate filesystem permission fault is covered by the history service suite.',
  ] }, null, 2));
} finally {
  process.off('SIGINT', handleSignal);
  process.off('SIGTERM', handleSignal);
  if (!cleanupPromise) cleanupPromise = cleanup();
  await cleanupPromise;
}
