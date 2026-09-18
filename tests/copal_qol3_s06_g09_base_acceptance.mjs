#!/usr/bin/env node

// G09 qualification: a disposable authenticated FastAPI application, the
// selected Darwin Copal release bridge, Redb, and a mounted Editor sheet.
// The Base source and note corpus are generated here; private audit copies are
// deliberately not used as fixtures.
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { execFileSync, spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const repo = process.cwd();
const python = process.env.OPENCLANK_PYTHON || path.join(repo, 'venv', 'bin', 'python');
const chrome = [process.env.OPENCLANK_CHROME_BIN, '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/Applications/Chromium.app/Contents/MacOS/Chromium', '/usr/bin/google-chrome', '/usr/bin/chromium'].filter(Boolean).find(fs.existsSync);
const bridge = path.join(repo, 'packages', 'Copal', 'rust', 'copal-db', 'target', 'release', 'copal-bridge');
assert(fs.existsSync(python), `Python runtime not found: ${python}`); assert(chrome, 'Chrome/Chromium executable required'); assert(fs.existsSync(bridge), `release bridge missing: ${bridge}`);
const sidecar = JSON.parse(fs.readFileSync(`${bridge}.copal-build.json`, 'utf8'));
const artifactSha256 = `sha256:${crypto.createHash('sha256').update(fs.readFileSync(bridge)).digest('hex')}`;
assert.equal(artifactSha256, sidecar.artifact_sha256, 'release bridge sidecar digest must match');
assert.equal(sidecar.build_identity, 'sha256:cf2e8cb263061e16633321cc31bfc09346bec46a835c610fdff26ab0c489817a');

const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-qol3-g09-'));
const data = path.join(temporary, 'data'); fs.mkdirSync(data, { recursive:true });
const evidence = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-qol3-g09-evidence-'));
const password = 'g09-disposable-password';
const hash = execFileSync(python, ['-c', 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())', password], { cwd:repo, encoding:'utf8' }).trim();
fs.writeFileSync(path.join(data, 'auth.json'), JSON.stringify({ signup_enabled:false, users:{ e:{ account_id:'g09-account', password_hash:hash, created:Date.now()/1000, is_admin:false } } }));

const baseSource = [
  '# Generated To Watch source: preserve this comment',
  'version: 1',
  'extensions: {title: To Watch}',
  'filters:',
  '  and:',
  '    - property: status',
  '      operator: eq',
  '      value: unwatched',
  '    - property: file.path',
  '      operator: not_contains',
  '      value: Projects/Obsidian/Templates',
  'views:',
  '  - id: table',
  '    name: To Watch',
  '    type: table',
  '    columns:',
  '      - property: file.name',
  '        label: Name',
  '      - property: file.tags',
  '        label: Tags',
  '      - property: status',
  '        label: Status',
  '      - property: score',
  '        label: Score',
  '      - property: due',
  '        label: Due',
  '    columnSize: {note.status: 82}',
  '    sorts:',
  '      - {property: file.tags, direction: asc}',
  '      - {property: file.name, direction: asc}',
  '    formulas:',
  '      scoreLabel: "score + 1"',
  '    unknownField: keep-g09',
  '  - id: list',
  '    name: List',
  '    type: list',
  '    columns: [{property: file.name, label: Name}, {property: status, label: Status}]',
  '    sorts: []',
  '    filters: null',
  '    limit: 100',
].join('\n') + '\n';

function freePort() { return new Promise((resolve, reject) => { const s = net.createServer(); s.once('error', reject); s.listen(0, '127.0.0.1', () => { const p = s.address().port; s.close((e) => e ? reject(e) : resolve(p)); }); }); }
function outputOf(child) { let output = ''; const collect = (chunk) => { output += String(chunk); if (output.length > 30000) output = output.slice(-30000); }; child.stdout?.on('data', collect); child.stderr?.on('data', collect); return () => output; }
async function stop(child, label) { if (!child || child.exitCode != null) return; const done = new Promise(resolve => child.once('exit', resolve)); try { process.kill(-child.pid, 'SIGTERM'); } catch (_) { try { child.kill('SIGTERM'); } catch (_) {} } await Promise.race([done, delay(5000)]); if (child.exitCode == null) { try { process.kill(-child.pid, 'SIGKILL'); } catch (_) { try { child.kill('SIGKILL'); } catch (_) {} } await Promise.race([done, delay(2000)]); } if (child.exitCode == null) throw new Error(`${label} did not exit`); }

const port = await freePort(); const appBase = `http://127.0.0.1:${port}`;
const environment = { ...process.env, APP_BIND:'127.0.0.1', APP_PORT:String(port), AUTH_ENABLED:'true', DEBUG:'false', OPENCLANK_DEBUG:'false', OPENCLANK_RECOVERY_MODE:'true', OPEN_CLANK_AGENT_DRIVE:'disabled', OPEN_CLANK_DATA_DIR:data, ODYSSEUS_DATA_DIR:data, DATABASE_URL:`sqlite:///${path.join(data, 'app.db')}`, COPAL_STORAGE:'redb', COPAL_DATA_DIR:path.join(data, 'copal-db'), COPAL_BRIDGE_COMMAND:bridge, PYTHONUNBUFFERED:'1' };
let app; let browser; let session = '';
async function waitHealth(logs) { const deadline = Date.now() + 120000; while (Date.now() < deadline) { if (app.exitCode != null) throw new Error(`FastAPI exited: ${logs()}`); try { if ((await fetch(`${appBase}/api/health`)).ok) return; } catch (_) {} await delay(100); } throw new Error(`FastAPI health timeout: ${logs()}`); }
async function request(route, options = {}) { const headers = new Headers(options.headers || {}); if (session) headers.set('Cookie', `odysseus_session=${session}`); return fetch(`${appBase}${route}`, { ...options, headers }); }
async function json(route, options = {}) { const response = await request(route, { ...options, headers:{ 'Content-Type':'application/json', ...(options.headers || {}) } }); const text = await response.text(); assert(response.ok, `${route} HTTP ${response.status}: ${text}`); return JSON.parse(text); }

async function openBrowser() {
  const profile = path.join(temporary, 'chrome');
  const child = spawn(chrome, ['--headless=new','--disable-gpu','--disable-dev-shm-usage','--no-sandbox','--disable-background-networking','--no-first-run','--no-default-browser-check','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'], { detached:true, stdio:['ignore','pipe','pipe'] });
  let output = '', debugPort = 0; const collect = chunk => { output += String(chunk); const match = output.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/); if (match) debugPort = Number(match[1]); }; child.stdout.on('data', collect); child.stderr.on('data', collect);
  for (let i = 0; i < 1200 && !debugPort; i++) await delay(25); assert(debugPort, `Chrome did not start: ${output.slice(-2000)}`);
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${appBase}/login`)}`, { method:'PUT' })).json();
  const socket = new WebSocket(target.webSocketDebuggerUrl); await new Promise((resolve, reject) => { socket.addEventListener('open', resolve, { once:true }); socket.addEventListener('error', reject, { once:true }); });
  let sequence = 0; const pending = new Map(); socket.addEventListener('message', event => { const message = JSON.parse(event.data); const item = pending.get(message.id); if (!item) return; pending.delete(message.id); clearTimeout(item.timer); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result); });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000); pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params })); });
  const evaluate = async expression => { const result = await cdp('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text); return result.result?.value; };
  const until = async (expression, label, timeout = 60000) => { const deadline = Date.now() + timeout; while (Date.now() < deadline) { if (await evaluate(`Boolean(${expression})`)) return; await delay(100); } const diag = await evaluate('({href:location.href,body:(document.body?.innerText||"").slice(0,1400),surfaces:document.querySelectorAll(".copal-sheet-surface").length,queries:window.__g09Queries?.length||0})').catch(() => ({})); throw new Error(`browser timeout: ${label}: ${JSON.stringify(diag)}`); };
  await cdp('Runtime.enable'); await cdp('Page.enable');
  return { child, socket, cdp, evaluate, until, async close() { try { socket.close(); } catch (_) {} await stop(child, 'Chrome').catch(() => {}); fs.rmSync(profile, { recursive:true, force:true, maxRetries:8, retryDelay:100 }); } };
}
function pngSize(buffer) { assert.equal(buffer.readUInt32BE(0), 0x89504e47); return { width:buffer.readUInt32BE(16), height:buffer.readUInt32BE(20) }; }
async function screenshot(label, browser) { const shot = await browser.cdp('Page.captureScreenshot', { format:'png' }); const bytes = Buffer.from(shot.data, 'base64'); const file = path.join(evidence, `${label}.png`); fs.writeFileSync(file, bytes); return { path:file, dimensions:pngSize(bytes) }; }

let base; let targetNote; const noteDocs = [];
try {
  app = spawn(python, ['-m','uvicorn','app:app','--host','127.0.0.1','--port',String(port)], { cwd:repo, env:environment, detached:true, stdio:['ignore','pipe','pipe'] });
  const logs = outputOf(app); await waitHealth(logs);
  const login = await fetch(`${appBase}/api/auth/login`, { method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({ username:'e', password, remember:true }) });
  assert.equal(login.status, 200, await login.text()); session = (login.headers.get('set-cookie') || '').match(/odysseus_session=([^;]+)/)?.[1] || ''; assert(session, 'session cookie missing');
  const sourceBody = '---\r\nstatus: unwatched\r\nquoted: "literal # marker"\r\n---\r\n# Shared G09 note\r\n\r\nUnrelated body marker.\r\n';
  targetNote = (await json('/api/copal/documents?workspace=default', { method:'POST', body:JSON.stringify({ name:'Projects/00 G09 Shared.md', kind:'note', content:sourceBody, properties:{ status:'unwatched', score:0, due:'2026-09-16', tags:['g09'] } }) })).doc;
  assert(targetNote?.id && targetNote.head);
  for (let i = 1; i <= 104; i++) {
    const doc = (await json('/api/copal/documents?workspace=default', { method:'POST', body:JSON.stringify({ name:`Projects/${String(i).padStart(3, '0')} G09 Row.md`, kind:'note', content:`# Generated row ${i}\n`, properties:{ status:'unwatched', score:i, due:'2026-09-16', tags:[i % 2 ? 'home' : 'work'] } }) })).doc;
    noteDocs.push(doc);
  }
  base = (await json('/api/copal/documents?workspace=default', { method:'POST', body:JSON.stringify({ name:'Projects/To Watch.base', kind:'base', content:baseSource }) })).doc;
  const initialBase = await json(`/api/copal/documents/${encodeURIComponent(base.id)}?workspace=default`); const initialNote = await json(`/api/copal/documents/${encodeURIComponent(targetNote.id)}?workspace=default`);
  assert.equal(initialBase.text, baseSource, 'generated Base source must be served byte exact before commands');
  const initialBaseHash = crypto.createHash('sha256').update(initialBase.text).digest('hex'); const initialNoteText = initialNote.text;

  browser = await openBrowser(); const { cdp, evaluate, until } = browser;
  await cdp('Network.setCookie', { name:'odysseus_session', value:session, url:`${appBase}/`, httpOnly:true, sameSite:'Lax' });
  await cdp('Page.navigate', { url:`${appBase}/?g09=${Date.now()}` }); await until('document.readyState === "complete" && Boolean(window.copalModule)', 'app shell');
  await evaluate(`(()=>{const native=window.fetch.bind(window);window.__g09Puts=[];window.__g09Queries=[];window.fetch=async(...args)=>{const request=args[0] instanceof Request?args[0]:new Request(args[0],args[1]);const body=request.method==='PUT'?await request.clone().text():'';if(request.url.includes('/api/copal/bases/')&&request.url.includes('/query'))window.__g09Queries.push({url:request.url});if(request.method==='PUT'&&request.url.includes('/api/copal/documents/'))window.__g09Puts.push({url:request.url,body});const response=await native(...args);return response;};})()`);
  await evaluate('window.copalModule.init(location.origin)'); await evaluate('window.copalModule.open("bases")');
  const ready = '(()=>{const surfaces=[...document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]")];return surfaces.length===1&&surfaces[0].getAttribute("aria-busy")!=="true"&&surfaces[0].querySelector(".copal-sheet-grid tbody tr")&&surfaces[0].querySelectorAll(".copal-sheet-grid tbody tr").length===100;})()';
  await until(ready, 'initial To Watch table'); assert.equal(await evaluate('document.querySelector(".copal-sheet-title h2")?.textContent'), 'To Watch');
  const beforeShot = await screenshot('before-desktop', browser);
  const initialQueries = await evaluate('window.__g09Queries.length');
  const sourceKeys = await evaluate('[...document.querySelectorAll(".copal-sheet-grid tbody tr:first-child [data-sheet-column-key]")].map(node=>node.dataset.sheetColumnKey)'); assert.deepEqual(sourceKeys, ['file.name','file.tags','status','score','due']);

  // Typed commands use the actual mounted column menu and resize control.
  async function waitMutation(label, before) { await until(`window.__g09Puts.length>${before}`, `${label} receipt`); await until(ready, `${label} query`); }
  async function columnMenu(columnLabel, commandText) {
    const before = await evaluate('window.__g09Puts.length');
    await evaluate(`([...document.querySelectorAll('.copal-sheet-column-menu')].find(node=>node.getAttribute('aria-label')===${JSON.stringify(`${columnLabel} column menu`)})?.click())`);
    await until('document.querySelector(".copal-sheet-column-menu-dialog[open]")', `${columnLabel} menu`);
    await evaluate(`([...document.querySelectorAll('.copal-sheet-column-menu-dialog[open] button')].find(node=>node.textContent.trim()===${JSON.stringify(commandText)})?.click())`);
    await waitMutation(commandText, before);
  }
  await columnMenu('Tags', 'Sort');
  await columnMenu('Status', 'Add sort priority');
  await columnMenu('Status', 'Move sort priority down');
  const resizeBefore = await evaluate('window.__g09Puts.length');
  await evaluate('document.querySelector("[data-sheet-column-resize=status]")?.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true}))'); await waitMutation('resize status', resizeBefore);
  const reorderBefore = await evaluate('window.__g09Puts.length');
  await evaluate('document.querySelector("[data-sheet-column-resize=due]")?.closest("th")?.querySelector(".copal-sheet-column-menu")?.click()'); await until('document.querySelector(".copal-sheet-column-menu-dialog[open]")', 'Due menu'); await evaluate('[...document.querySelectorAll(".copal-sheet-column-menu-dialog[open] button")].find(node=>node.textContent.includes("Move column left"))?.click()'); await waitMutation('reorder due', reorderBefore);
  const typedDefinition = await json(`/api/copal/documents/${encodeURIComponent(base.id)}?workspace=default`); assert(typedDefinition.text.includes('unknownField: keep-g09')); assert(typedDefinition.text.includes('scoreLabel: "score + 1"')); assert(typedDefinition.text.includes('Projects/Obsidian/Templates')); assert(typedDefinition.text.includes('# Generated To Watch source')); assert(typedDefinition.text.includes('columnSize: {note.status: 82}') || typedDefinition.text.includes('columnSize: {note.status: 98}'));

  // Two actual workspace leaves share the Base resource. Each gets its own
  // sheet controller/view/selection/scroll state while the Redb head is shared.
  await evaluate('document.querySelector("button[aria-label=\\"Split right\\"]")?.click()'); await until('document.querySelector(".copal-quick-switcher[open]")', 'split chooser'); await evaluate(`([...document.querySelectorAll('.copal-quick-switcher[open] .copal-doc-row')].find(node=>node.textContent.includes('To Watch.base'))?.click())`); await until('document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]").length===2', 'two Base leaves');
  const leafState = await evaluate(`(()=>{const groups=[...document.querySelectorAll('.copal-note-group')];const surfaces=[...document.querySelectorAll('.copal-sheet-surface[data-sheet-status=ready]')];if(groups.length!==2||surfaces.length!==2) return null;const wraps=surfaces.map(s=>s.querySelector('.copal-sheet-grid-wrap'));for(const wrap of wraps){wrap.style.height='52px';wrap.style.overflow='auto';}wraps[0].scrollTop=31;wraps[1].scrollTop=67;const cells=surfaces.map(s=>s.querySelector('.copal-sheet-cell[data-sheet-row-index="4"][data-sheet-column-key="status"]'));cells[0]?.focus();cells[0]?.click();cells[1]?.focus();cells[1]?.click();return {groups:groups.length,scroll:[...wraps].map(w=>w.scrollTop),selected:surfaces.map(s=>s.querySelector('.copal-sheet-cell[aria-selected="true"]')?.dataset.sheetRowKey||null),leafIds:groups.map(g=>g.querySelector('.copal-note-leaf')?.dataset.leafId||null)};})()`); assert(leafState?.groups===2 && leafState.scroll[0]===31 && leafState.scroll[1]===67 && leafState.selected.every(Boolean), JSON.stringify(leafState));
  const listTab = await evaluate('document.querySelectorAll(".copal-note-group")[1]?.querySelector(".copal-sheet-tab[data-sheet-view-id=list]")'); assert(listTab, 'second leaf List view tab missing'); await evaluate('document.querySelectorAll(".copal-note-group")[1].querySelector(".copal-sheet-tab[data-sheet-view-id=list]").focus();document.querySelectorAll(".copal-note-group")[1].querySelector(".copal-sheet-tab[data-sheet-view-id=list]").click()'); await until('document.querySelectorAll(".copal-sheet-lists").length===1', 'second leaf List view');
  await evaluate('document.querySelectorAll(".copal-note-group")[1].querySelector(".copal-sheet-tab[data-sheet-view-id=table]").focus();document.querySelectorAll(".copal-note-group")[1].querySelector(".copal-sheet-tab[data-sheet-view-id=table]").click()'); await until('document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]").length===2', 'second leaf return Table');
  const afterViews = await evaluate('(()=>{const surfaces=[...document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]")];return {surfaces:surfaces.length,views:surfaces.map(s=>s.querySelector(".copal-sheet-tab[aria-selected=true]")?.dataset.sheetViewId),scroll:[...document.querySelectorAll(".copal-sheet-grid-wrap")].map(w=>w.scrollTop),selected:surfaces.map(s=>s.querySelector(".copal-sheet-cell[aria-selected=true]")?.dataset.sheetRowKey||null),queries:window.__g09Queries.length};})()'); assert.equal(afterViews.surfaces, 2); assert(afterViews.views.every(view=>view === 'table')); assert.deepEqual(afterViews.scroll, [31, 67]); assert(afterViews.selected.every(Boolean)); assert(afterViews.queries > initialQueries);

  // Raw↔typed entry point uses the second mounted leaf, then restores the
  // sheet without creating a nested/duplicate Base surface.
  await evaluate('document.querySelectorAll(".copal-note-group")[1].querySelector(".copal-sheet-tool:last-of-type")?.click()'); await until('document.querySelector(".copal-sheet-overflow[open]")', 'sheet overflow'); await evaluate('[...document.querySelectorAll(".copal-sheet-overflow[open] button")].find(node=>node.textContent.includes("Open raw source"))?.click()'); await until('document.querySelectorAll(".cm-editor,.copal-codemirror-host").length>0', 'raw source mode'); await evaluate('window.copalModule.open("bases",false)'); await until('document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]").length>=1', 'typed sheet after raw mode');
  const afterSource = await json(`/api/copal/documents/${encodeURIComponent(base.id)}?workspace=default`); assert(afterSource.text.includes('unknownField: keep-g09')); assert(afterSource.text.includes('scoreLabel: "score + 1"')); assert(afterSource.text.includes('Projects/Obsidian/Templates')); assert(afterSource.text.includes('# Generated To Watch source'));
  const afterShotDesktop = await screenshot('after-desktop', browser); await cdp('Emulation.setDeviceMetricsOverride', { width:680, height:900, deviceScaleFactor:1, mobile:false }); await evaluate('window.__copal.open("bases",false)'); await until('document.querySelector(".copal-sheet-surface[data-sheet-status=ready]")', 'narrow typed sheet'); const beforeShotNarrow = await screenshot('before-narrow', browser); await evaluate('document.querySelector(".copal-sheet-tab[data-sheet-view-id=list]")?.click()'); await until('document.querySelector(".copal-sheet-lists")', 'narrow List'); await evaluate('document.querySelector(".copal-sheet-tab[data-sheet-view-id=table]")?.click()'); await until('document.querySelector(".copal-sheet-grid")', 'narrow Table'); const afterShotNarrow = await screenshot('after-narrow', browser);
  const finalBase = await json(`/api/copal/documents/${encodeURIComponent(base.id)}?workspace=default`); const history = await json(`/api/copal/documents/${encodeURIComponent(base.id)}/history?workspace=default`).catch(() => ({ changes:[] })); const putLog = await evaluate('window.__g09Puts');
  assert(finalBase.head && finalBase.head !== initialBase.head); assert(finalBase.text.includes('unknownField: keep-g09')); assert(finalBase.text.includes('scoreLabel: "score + 1"')); assert(finalBase.text.includes('Projects/Obsidian/Templates')); assert(finalBase.text.includes('# Generated To Watch source')); assert(finalBase.text.includes('columnSize:')); assert.notEqual(crypto.createHash('sha256').update(finalBase.text).digest('hex'), initialBaseHash); assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-surface").length'), 1, 'returning from raw mode must leave one clean typed sheet surface'); assert(putLog.length >= 5 && putLog.every(item => item.body.includes('actionId')), `every Base mutation needs an acknowledged action: ${JSON.stringify(putLog)}`);
  console.log(JSON.stringify({ gate:'G09', passed:true, backend:'FastAPI → exact release Copal bridge → Redb', binary:bridge, buildIdentity:sidecar.build_identity, artifactSha256, generatedRows:105, finalHead:finalBase.head, baseWrites:putLog.length, acknowledgedActionIds:putLog.map(item=>JSON.parse(item.body).actionId), historyEntries:history.changes?.length || history.history?.length || 0, sourcePreservation:{comment:true,unknown:true,formula:true,unrelatedFilter:true,initialSha256:`sha256:${initialBaseHash}`,finalSha256:`sha256:${crypto.createHash('sha256').update(finalBase.text).digest('hex')}`}, leaves:afterViews, screenshots:{beforeDesktop:beforeShot,afterDesktop:afterShotDesktop,beforeNarrow:beforeShotNarrow,afterNarrow:afterShotNarrow}, evidenceDir:evidence }));
} finally {
  await browser?.close().catch(() => {}); await stop(app, 'disposable app').catch(() => {}); fs.rmSync(temporary, { recursive:true, force:true, maxRetries:8, retryDelay:100 });
}
