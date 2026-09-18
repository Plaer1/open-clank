#!/usr/bin/env node

// G02 uses the real FastAPI app, Redb bridge, authenticated session, and a
// request boundary interception that delays one actual document PUT. The
// interception is disposable and never touches the live service.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const repo = process.cwd();
const python = process.env.OPENCLANK_PYTHON || path.join(repo, 'venv', 'bin', 'python');
const chrome = [
  process.env.OPENCLANK_CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/usr/bin/google-chrome', '/usr/bin/chromium',
].filter(Boolean).find((candidate) => fs.existsSync(candidate));
assert(fs.existsSync(python), `Python runtime not found: ${python}`);
assert(chrome, 'Chrome/Chromium executable required');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close((error) => error ? reject(error) : resolve(port)); });
  });
}
function outputOf(child) {
  let output = '';
  const collect = (chunk) => { output += String(chunk); if (output.length > 30_000) output = output.slice(-30_000); };
  child.stdout?.on('data', collect); child.stderr?.on('data', collect);
  return () => output;
}
async function stop(child, label) {
  if (!child || child.exitCode != null) return;
  const exited = new Promise((resolve) => child.once('exit', resolve));
  try { process.kill(-child.pid, 'SIGTERM'); } catch (_) { try { child.kill('SIGTERM'); } catch (_) {} }
  await Promise.race([exited, delay(5000)]);
  if (child.exitCode == null) {
    try { process.kill(-child.pid, 'SIGKILL'); } catch (_) { try { child.kill('SIGKILL'); } catch (_) {} }
    await Promise.race([exited, delay(2000)]);
  }
  if (child.exitCode == null) throw new Error(`${label} did not exit`);
}
async function waitHealth(base, child, logs) {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (child.exitCode != null) throw new Error(`app exited (${child.exitCode})\n${logs()}`);
    try { if ((await fetch(`${base}/api/health`)).ok) return; } catch (_) {}
    await delay(100);
  }
  throw new Error(`app health timeout\n${logs()}`);
}

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-qol3-g02-'));
const dataDir = path.join(temporary, 'data');
const copalDir = path.join(temporary, 'copal');
fs.mkdirSync(dataDir, { recursive:true }); fs.mkdirSync(copalDir, { recursive:true });
const appPort = await freePort();
const debuggerPort = await freePort();
const appBase = `http://127.0.0.1:${appPort}`;
const base = appBase;
let app; let browser;
let releaseHeldPut;
let heldPutSeen = false;
let heldPutReleased = false;
const putBodies = [];
const environment = {
  ...process.env, APP_BIND:'127.0.0.1', APP_PORT:String(appPort), AUTH_ENABLED:'true',
  DEBUG:'false', OPENCLANK_DEBUG:'false', OPENCLANK_RECOVERY_MODE:'true',
  OPEN_CLANK_DATA_DIR:dataDir, ODYSSEUS_DATA_DIR:dataDir,
  DATABASE_URL:`sqlite:///${path.join(dataDir, 'app.db')}`, COPAL_DATA_DIR:copalDir,
  COPAL_STORAGE:'redb', PYTHONUNBUFFERED:'1',
};

try {
  app = spawn(python, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(appPort)], { cwd:repo, env:environment, detached:true, stdio:['ignore','pipe','pipe'] });
  const appLogs = outputOf(app);
  await waitHealth(appBase, app, appLogs);
  const setup = await fetch(`${appBase}/api/auth/setup`, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({ username:'g02-owner', password:'g02-disposable-password' }) });
  assert.equal(setup.status, 200, await setup.text());
  const login = await fetch(`${appBase}/api/auth/login`, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({ username:'g02-owner', password:'g02-disposable-password', remember:true }) });
  assert.equal(login.status, 200, await login.text());
  const cookie = String(login.headers.get('set-cookie') || '').split(';', 1)[0];
  assert.match(cookie, /^odysseus_session=/);
  const create = await fetch(`${appBase}/api/copal/documents?workspace=default`, { method:'POST', headers:{'content-type':'application/json', cookie}, body:JSON.stringify({ name:'G02 rapid save.md', kind:'markdown', content:'# G02 initial\n\nStable body.\n' }) });
  const createPayload = await create.json();
  assert.equal(create.status, 200, JSON.stringify(createPayload));
  const created = createPayload.doc;
  assert(created?.id && created.head, 'disposable note was not created');

  browser = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox',
    '--disable-background-networking', '--disable-component-update', '--no-first-run',
    `--remote-debugging-port=${debuggerPort}`, `--user-data-dir=${path.join(temporary, 'chrome')}`, 'about:blank',
  ], { detached:true, stdio:['ignore','pipe','pipe'] });
  const browserLogs = outputOf(browser);
  const debuggerBase = `http://127.0.0.1:${debuggerPort}`;
  let target;
  for (let attempt = 0; attempt < 300 && !target; attempt += 1) {
    try { target = (await (await fetch(`${debuggerBase}/json`)).json()).find((item) => item.type === 'page'); } catch (_) {}
    if (!target) await delay(100);
  }
  assert(target?.webSocketDebuggerUrl, `Chrome debugger unavailable\n${browserLogs()}`);
  const socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once:true }); socket.addEventListener('error', reject, { once:true }); });
  let sequence = 0; const pending = new Map();
  socket.addEventListener('message', async (event) => {
    const message = JSON.parse(event.data);
    if (message.method === 'Fetch.requestPaused') {
      const request = message.params.request || {};
      if (request.method === 'PUT' && request.url.startsWith(`${appBase}/api/copal/documents/${encodeURIComponent(created.id)}`)) {
        putBodies.push(JSON.parse(request.postData || '{}'));
        if (!heldPutSeen) {
          heldPutSeen = true;
          releaseHeldPut = () => { heldPutReleased = true; void command('Fetch.continueRequest', { requestId:message.params.requestId }); };
          return;
        }
      }
      void command('Fetch.continueRequest', { requestId:message.params.requestId });
      return;
    }
    const request = pending.get(message.id); if (!request) return;
    pending.delete(message.id); clearTimeout(request.timer); message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
  });
  function command(method, params = {}) {
    const id = ++sequence;
    return new Promise((resolve, reject) => { const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 30_000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); });
  }
  await command('Fetch.enable', { patterns:[{ urlPattern:`${appBase}/api/copal/documents/${encodeURIComponent(created.id)}*`, requestStage:'Request' }] });
  async function evaluate(expression) {
    const result = await command('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
    return result.result.value;
  }
  async function waitFor(expression, label, timeout = 60_000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) { if (await evaluate(expression)) return; await delay(100); }
    const diagnostics = await evaluate('JSON.stringify({ href:location.href, ready:document.readyState, body:(document.body?.innerText || "").slice(0,500), modals:[...document.querySelectorAll(".copal-view-window")].map((node)=>({id:node.id,hidden:node.classList.contains("hidden")})), editor:document.querySelectorAll(".cm-content").length })').catch(() => 'unavailable');
    throw new Error(`Timed out waiting for ${label}: ${diagnostics}`);
  }
  await command('Page.navigate', { url:`${base}/login` });
  await waitFor(`location.origin === ${JSON.stringify(base)} && (location.pathname === '/login' || location.pathname === '/')`, 'login page');
  const browserLogin = await evaluate(`fetch('/api/auth/login',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({username:'g02-owner',password:'g02-disposable-password',remember:true})}).then(async response=>({status:response.status,body:await response.json()}))`);
  assert.equal(browserLogin.status, 200, JSON.stringify(browserLogin));
  assert.equal(await evaluate('fetch("/api/auth/status").then(response=>response.json()).then(value=>value.authenticated)'), true, 'browser session must be authenticated');
  await command('Page.navigate', { url:`${base}/?g02=${Date.now()}` });
  await waitFor('document.readyState === "complete" && Boolean(window.copalModule)', 'authenticated app shell');
  await evaluate(`(()=>{const native=window.fetch.bind(window); window.__g02PutResponses=[]; window.fetch=async(...args)=>{const request=args[0] instanceof Request ? args[0] : new Request(args[0],args[1]); const response=await native(...args); if(request.method==='PUT' && request.url.includes('/api/copal/documents/')) { let body=null; try { body=await response.clone().json(); } catch (_) {} window.__g02PutResponses.push({status:response.status,body}); } return response; };})()`);
  await evaluate('window.copalModule.init(location.origin)');
  await evaluate(`window.copalModule.open('notes')`);
  await waitFor('document.querySelector("#copal-notes-modal") && document.querySelectorAll("#copal-notes-modal .copal-note-tab").length > 0', 'authenticated Notes shell');
  await evaluate(`(()=>{const tab=[...document.querySelectorAll('#copal-notes-modal .copal-note-tab')].find((node)=>node.textContent.includes('G02 rapid save'));if(!tab)throw new Error('G02 note tab was not loaded');tab.click();})()`);
  await waitFor(`document.querySelector('#copal-notes-modal .cm-content')?.textContent.includes('G02 initial')`, 'G02 source editor');
  await evaluate('window.__g02Errors=[]; window.addEventListener("unhandledrejection",event=>window.__g02Errors.push(String(event.reason))); window.addEventListener("error",event=>window.__g02Errors.push(String(event.error||event.message)));');
  const append = async (marker) => {
    await evaluate('document.querySelector("#copal-notes-modal .cm-content").focus()');
    await command('Input.dispatchKeyEvent', { type:'keyDown', key:'End', code:'End', windowsVirtualKeyCode:35 });
    await command('Input.dispatchKeyEvent', { type:'keyUp', key:'End', code:'End', windowsVirtualKeyCode:35 });
    await command('Input.insertText', { text:`\n${marker}` });
    await waitFor(`document.querySelector('#copal-notes-modal .cm-content')?.textContent.includes(${JSON.stringify(marker)})`, `${marker} typed`);
  };
  const saveWithPrimaryShortcut = async () => {
    await evaluate('document.querySelector("#copal-notes-modal .cm-content").blur()');
    await command('Input.dispatchKeyEvent', { type:'keyDown', key:'s', code:'KeyS', windowsVirtualKeyCode:83, nativeVirtualKeyCode:83, modifiers:4 });
    await command('Input.dispatchKeyEvent', { type:'keyUp', key:'s', code:'KeyS', windowsVirtualKeyCode:83, nativeVirtualKeyCode:83, modifiers:4 });
  };
  await append('G02-REVISION-A');
  await saveWithPrimaryShortcut();
  for (let attempt = 0; attempt < 100 && !heldPutSeen; attempt += 1) await delay(20);
  assert.equal(heldPutSeen, true, 'the first save did not reach the real delayed PUT boundary');
  await append('G02-REVISION-B');
  await saveWithPrimaryShortcut();
  assert.equal(heldPutReleased, false, 'the controlled first PUT was released too early');
  releaseHeldPut();
  await waitFor(`window.__g02Errors.length === 0`, 'no browser error after release', 10_000);
  for (let attempt = 0; attempt < 300 && putBodies.length < 2; attempt += 1) await delay(20);
  if (putBodies.length < 2) {
    const state = await evaluate('JSON.stringify({save:[...document.querySelectorAll(".copal-save-state")].map((node)=>node.textContent),body:[...document.querySelectorAll("#copal-notes-modal .cm-line")].map((node)=>node.textContent).join("\\n"),dialogs:[...document.querySelectorAll("dialog[open]")].map((node)=>node.className)})');
    throw new Error(`expected queued newer revision to reach the real PUT, got ${putBodies.length}: ${JSON.stringify({ putBodies, state })}`);
  }
  await waitFor('window.__g02PutResponses.length === 2', 'both real PUT receipts');
  const putResponses = await evaluate('window.__g02PutResponses');
  assert.deepEqual(putResponses.map((response) => response.status), [200, 200], `both queued writes must receive applied receipts: ${JSON.stringify(putResponses)}`);
  const expectedText = await evaluate('document.querySelector("#copal-notes-modal .cm-content") ? [...document.querySelectorAll("#copal-notes-modal .cm-line")].map((line)=>line.textContent).join("\\n") : ""');
  assert.match(expectedText, /G02-REVISION-A/); assert.match(expectedText, /G02-REVISION-B/);
  const saved = await evaluate(`fetch('/api/copal/documents/${encodeURIComponent(created.id)}?workspace=default').then(response=>response.json())`);
  assert.equal(saved.text, expectedText, `latest draft must be durable after both receipts: ${JSON.stringify({ expectedText, savedText:saved.text, savedHead:saved.head, createdHead:created.head, putBodies })}`);
  assert.equal(await evaluate('document.querySelectorAll(".copal-conflict-dialog[open]").length'), 0, 'ordinary save must not self-conflict');
  assert.equal(new Set(putBodies.map((body) => body.actionId)).size, 2, 'each immutable revision has one action identity');
  assert.match(putBodies[0].content, /G02-REVISION-A/); assert.doesNotMatch(putBodies[0].content, /G02-REVISION-B/);
  assert.match(putBodies[1].content, /G02-REVISION-B/);
  await command('Page.navigate', { url:`${base}/?reload=${Date.now()}` });
  await waitFor('document.readyState === "complete" && Boolean(window.copalModule)', 'durable reload shell');
  await evaluate('window.copalModule.init(location.origin)');
  await evaluate(`window.copalModule.open('notes')`);
  await waitFor('document.querySelector("#copal-notes-modal") && document.querySelectorAll("#copal-notes-modal .copal-note-tab").length > 0', 'durable Notes shell');
  await evaluate(`(()=>{const tab=[...document.querySelectorAll('#copal-notes-modal .copal-note-tab')].find((node)=>node.textContent.includes('G02 rapid save'));if(!tab)throw new Error('G02 note tab missing after reload');tab.click();})()`);
  await waitFor(`document.querySelector('#copal-notes-modal .cm-content')?.textContent.includes('G02-REVISION-B')`, 'durable G02 reload');
  const afterReload = await evaluate(`fetch('/api/copal/documents/${encodeURIComponent(created.id)}?workspace=default').then(response=>response.json()).then(value=>({text:value.text,head:value.head}))`);
  assert.equal(afterReload.text, expectedText); assert(afterReload.head && afterReload.head !== created.head);
  console.log(JSON.stringify({ gate:'G02 rapid edit blur Cmd/Ctrl-S durability', authenticated:true, writes:putBodies.length, actionIds:putBodies.map((body)=>body.actionId), firstWriteHasA:/G02-REVISION-A/.test(putBodies[0].content), firstWriteHasB:/G02-REVISION-B/.test(putBodies[0].content), finalHead:afterReload.head, selfConflictDialogs:0, realDelayedPut:true, appBase }));
  socket.close();
} finally {
  await stop(browser, 'Chrome').catch(() => {});
  await stop(app, 'disposable app').catch(() => {});
  fs.rmSync(temporary, { recursive:true, force:true, maxRetries:8, retryDelay:100 });
}
