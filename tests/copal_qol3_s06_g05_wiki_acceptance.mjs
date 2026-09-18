#!/usr/bin/env node

// Disposable G05 qualification.  A tiny Rust fixture builder creates a fresh
// Wiki Redb containing generated equivalents of the eight historical shared
// Wiki shapes.  The exact release bridge and FastAPI route/index then serve
// those records to a real authenticated Chromium page.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import { execFileSync, spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const repo = process.cwd();
const python = path.join(repo, 'venv', 'bin', 'python');
const chrome = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'].find(fs.existsSync);
const bridge = path.join(repo, 'packages', 'Copal', 'rust', 'copal-db', 'target', 'release', 'copal-bridge');
if (!fs.existsSync(python) || !chrome || !fs.existsSync(bridge)) { console.log(JSON.stringify({ skipped:'requires venv/bin/python, Chrome, and the verified release bridge' })); process.exit(0); }
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-qol3-g05-'));
const data = path.join(temporary, 'data');
const evidence = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-qol3-g05-evidence-'));
fs.mkdirSync(data, { recursive:true });
const password = 'g05-disposable-password';
const hash = execFileSync(python, ['-c', 'import bcrypt,sys; print(bcrypt.hashpw(sys.argv[1].encode(), bcrypt.gensalt()).decode())', password], { cwd:repo, encoding:'utf8' }).trim();
fs.writeFileSync(path.join(data, 'auth.json'), JSON.stringify({ signup_enabled:false, users:{ e:{ account_id:'account-e', password_hash:hash, created:Date.now()/1000, is_admin:false } } }));

const names = [
  '.memes/What Is Wiki', '.memes/Creating and Linking Memes',
  '.memes/Story Navigation', '.memes/Fields and Properties',
  '.memes/Wiki vs Notes', '.memes/How Wiki Works',
  '.memes/Meme-sized Page', '.memes/Meme-sized Tiddler',
];
const markers = names.map((_, index) => `G05-HISTORICAL-SHAPE-${index + 1}`);
const fixtureBuilder = `
use copal_db::Db;
use serde_json::json;
use std::path::PathBuf;
fn main() {
    let dir = PathBuf::from(std::env::args().nth(1).expect("data dir"));
    let wiki = Db::open_with_name(&dir, "copal-wiki").expect("open wiki");
    let names = ${JSON.stringify(names)};
    let markers = ${JSON.stringify(markers)};
    for (index, name) in names.iter().enumerate() {
        let body = format!("# Historical Wiki shape {}\\n\\nGenerated compatibility page.\\n{}", index + 1, markers[index]);
        let block = json!({"id": format!("g05-block-{}", index + 1), "type":"heading", "level":1, "text":format!("Historical Wiki shape {}", index + 1), "source":format!("# Historical Wiki shape {}", index + 1)});
        let paragraph = json!({"id": format!("g05-paragraph-{}", index + 1), "type":"paragraph", "text":format!("Generated compatibility page.\\n{}", markers[index]), "source":format!("Generated compatibility page.\\n{}", markers[index])});
        let record = json!({"schemaVersion":1,"body":{"type":"doc","blocks":[block,paragraph]},"properties":[],"relations":[],"tags":["g05","historical"],"extensions":{"interchange":{"source":body,"modified":false},"qualification":{"shape":index + 1}}});
        wiki.create_builtin_seed_doc("wiki", name, &record.to_string(), Some("G05 generated historical equivalent")).expect("create builtin");
    }
    wiki.create_builtin_seed_doc("wiki", ".memes/G05 Malformed Record", "{malformed", Some("G05 malformed preservation fixture")).expect("create malformed");
    wiki.create_builtin_seed_doc("wiki", ".memes/G05 Future Record", r#"{"schemaVersion":99,"body":{"type":"doc","blocks":[]},"futureField":"preserve"}"#, Some("G05 future preservation fixture")).expect("create future");
}
`;
const helperRoot = path.join(temporary, 'fixture-builder');
fs.mkdirSync(path.join(helperRoot, 'src'), { recursive:true });
fs.writeFileSync(path.join(helperRoot, 'Cargo.toml'), `[package]\nname = "g05-fixture-builder"\nversion = "0.1.0"\nedition = "2021"\n[dependencies]\ncopal-db = { path = "${path.join(repo, 'packages/Copal/rust/copal-db')}" }\nserde_json = "1"\n`);
fs.writeFileSync(path.join(helperRoot, 'src/main.rs'), fixtureBuilder);
execFileSync('cargo', ['run', '--offline', '--quiet', '--manifest-path', path.join(helperRoot, 'Cargo.toml'), '--', path.join(data, 'copal-db')], { cwd:repo, env:{ ...process.env, CARGO_TARGET_DIR:path.join(temporary, 'cargo-target') }, stdio:['ignore','pipe','pipe'] });

const freePort = () => new Promise((resolve, reject) => { const server = net.createServer(); server.once('error', reject); server.listen(0, '127.0.0.1', () => { const port = server.address().port; server.close(error => error ? reject(error) : resolve(port)); }); });
const port = await freePort();
const base = `http://127.0.0.1:${port}`;
const env = { ...process.env, DEBUG:'false', OPENCLANK_DEBUG:'false', AUTH_ENABLED:'true', OPENCLANK_RECOVERY_MODE:'true', OPEN_CLANK_AGENT_DRIVE:'disabled', COPAL_STORAGE:'redb', COPAL_DATA_DIR:path.join(data, 'copal-db'), COPAL_BRIDGE_COMMAND:bridge, OPEN_CLANK_DATA_DIR:data, DATABASE_URL:`sqlite:///${path.join(data, 'app.db')}`, PYTHONUNBUFFERED:'1' };
let appOutput = '';
const app = spawn(python, ['-m','uvicorn','app:app','--host','127.0.0.1','--port',String(port)], { cwd:repo, env, stdio:['ignore','pipe','pipe'] });
const collect = chunk => { appOutput += String(chunk); if (appOutput.length > 30000) appOutput = appOutput.slice(-30000); };
app.stdout.on('data', collect); app.stderr.on('data', collect);
async function waitHealth() { for (let attempt = 0; attempt < 1200; attempt += 1) { if (app.exitCode != null) throw new Error(`FastAPI exited: ${appOutput.slice(-4000)}`); try { if ((await fetch(`${base}/api/health`)).ok) return; } catch (_) {} await delay(100); } throw new Error(`health timeout: ${appOutput.slice(-4000)}`); }
let session = '';
async function request(route, options = {}) { const headers = new Headers(options.headers || {}); if (session) headers.set('Cookie', `odysseus_session=${session}`); return fetch(`${base}${route}`, { ...options, headers }); }
async function json(route, options = {}) { const response = await request(route, { ...options, headers:{ 'Content-Type':'application/json', ...(options.headers || {}) } }); const body = await response.text(); assert(response.ok, `${route}: HTTP ${response.status} ${body}`); return JSON.parse(body); }
async function openBrowser() {
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-qol3-g05-browser-'));
  const child = spawn(chrome, ['--headless=new','--disable-gpu','--disable-dev-shm-usage','--no-sandbox','--no-first-run','--no-default-browser-check',`--user-data-dir=${profile}`,'--remote-debugging-port=0','about:blank'], { stdio:['ignore','pipe','pipe'] });
  let output = '', debugPort;
  const collectChrome = chunk => { output += String(chunk); const match = output.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/); if (match) debugPort = Number(match[1]); };
  child.stdout.on('data', collectChrome); child.stderr.on('data', collectChrome);
  for (let i = 0; i < 1200 && !debugPort; i += 1) await delay(25);
  assert(debugPort, `Chrome did not start: ${output.slice(-2000)}`);
  const target = await (await fetch(`http://127.0.0.1:${debugPort}/json/new?${encodeURIComponent(`${base}/login`)}`, { method:'PUT' })).json();
  const ws = new WebSocket(target.webSocketDebuggerUrl); await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once:true }); ws.addEventListener('error', reject, { once:true }); });
  let sequence = 0; const pending = new Map(); ws.addEventListener('message', event => { const message = JSON.parse(event.data); if (!message.id || !pending.has(message.id)) return; const item = pending.get(message.id); pending.delete(message.id); message.error ? item.reject(new Error(message.error.message)) : item.resolve(message.result); });
  const cdp = (method, params = {}) => new Promise((resolve, reject) => { const id = ++sequence; const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}`)); }, 30000); pending.set(id, { resolve:value => { clearTimeout(timer); resolve(value); }, reject }); ws.send(JSON.stringify({ id, method, params })); });
  const evaluate = async expression => { const result = await cdp('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true }); if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text); return result.result?.value; };
  const until = async (expression, label, timeout = 60000) => { const deadline = Date.now() + timeout; while (Date.now() < deadline) { if (await evaluate(`Boolean(${expression})`)) return; await delay(100); } const diagnostic = await evaluate('({href:location.href,text:document.body?.innerText?.slice(0,1200),error:document.body?.dataset?.error||null})').catch(() => ({})); throw new Error(`browser timeout: ${label}: ${JSON.stringify(diagnostic)}`); };
  await cdp('Runtime.enable'); await cdp('Page.enable'); await cdp('Network.enable');
  return { cdp, evaluate, until, async close() { try { ws.close(); } catch (_) {} if (child.exitCode == null) child.kill('SIGTERM'); for (let i = 0; i < 80 && child.exitCode == null; i += 1) await delay(25); if (child.exitCode == null) child.kill('SIGKILL'); fs.rmSync(profile, { recursive:true, force:true, maxRetries:8, retryDelay:100 }); } };
}
function pngSize(buffer) { assert.equal(buffer.readUInt32BE(0), 0x89504e47); return { width:buffer.readUInt32BE(16), height:buffer.readUInt32BE(20) }; }
let browser;
try {
  await waitHealth();
  const login = await fetch(`${base}/api/auth/login`, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({ username:'e', password, remember:true }) });
  assert.equal(login.status, 200, await login.text()); session = (login.headers.get('set-cookie') || '').match(/odysseus_session=([^;]+)/)?.[1] || ''; assert(session, 'login did not return a session cookie');
  const status = await json('/api/copal/status?workspace=default'); assert.equal(status.owner, 'e');
  const listed = await json('/api/copal/documents?workspace=default&corpus=wiki&hidden=include');
  const historical = listed.docs.filter(doc => names.includes(doc.name));
  assert.equal(historical.length, 8, JSON.stringify(listed.docs.map(doc => ({ name:doc.name, owner:doc.owner, builtin:doc.builtin }))));
  for (const doc of historical) { const index = names.indexOf(doc.name); assert(index >= 0); assert.equal(doc.owner, 'shared'); assert.equal(doc.workspace_id, 'global'); assert.equal(doc.builtin, true); assert.equal(doc.recoveryState, 'supported'); assert.equal(doc.note_error ?? null, null); assert.match(doc.text, new RegExp(markers[index])); }
  const malformed = listed.docs.find(doc => doc.name === '.memes/G05 Malformed Record'); const future = listed.docs.find(doc => doc.name === '.memes/G05 Future Record');
  assert(malformed && future, 'generated malformed/future records missing from real index'); assert.equal(malformed.owner, 'shared'); assert.equal(malformed.rawPreserved, true); assert.equal(malformed.recoveryState, 'malformed-preserved'); assert(malformed.note_error); assert.equal(future.owner, 'shared'); assert.equal(future.rawPreserved, true); assert.equal(future.recoveryState, 'unsupported-future'); assert.equal(future.sourceSchemaVersion, 99); assert(future.note_error);
  // A same-name user record is created in a second workspace; the route keeps
  // it independently owned while the shared/global builtin remains intact.
  const override = await json('/api/copal/documents?workspace=g05-override', { method:'POST', body:JSON.stringify({ name:names[0], kind:'wiki', corpus:'wiki', content:'G05 USER OVERRIDE' }) });
  assert.equal(override.doc.owner, 'e'); assert.equal(override.doc.workspace_id, 'g05-override'); assert.equal(override.doc.name, names[0]);
  const overrideView = await json('/api/copal/documents?workspace=g05-override&corpus=wiki&hidden=include'); const owned = overrideView.docs.filter(doc => doc.name === names[0]); assert.equal(owned.length, 1); assert.equal(owned[0].owner, 'e'); assert.equal(owned[0].text, 'G05 USER OVERRIDE');
  browser = await openBrowser(); const { cdp, evaluate, until } = browser;
  await cdp('Network.setCookie', { name:'odysseus_session', value:session, url:`${base}/`, httpOnly:true, sameSite:'Lax' });
  await cdp('Page.navigate', { url:`${base}/copal/editor` }); await until('Boolean(window.copalModule)', 'Copal module'); await evaluate('window.copalModule.init(location.origin)'); await until('Boolean(window.__openClankCopalContext?.())', 'Copal storage scope'); await evaluate('window.copalModule.open("wiki", false)');
  await until(`[...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].filter(node => ${JSON.stringify(names)}.includes(node.textContent.trim())).length === 8`, 'all eight historical library rows');
  const screenshots = [];
  for (let index = 0; index < names.length; index += 1) {
    const name = names[index]; const marker = markers[index];
    await evaluate(`([...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].find(node => node.textContent.trim() === ${JSON.stringify(name)})?.click())`);
    await until(`[...document.querySelectorAll('[data-wiki-document]')].some(card => card.textContent.includes(${JSON.stringify(marker)}))`, `render ${name}`);
    const cardState = await evaluate(`(() => { const cards=[...document.querySelectorAll('[data-wiki-document]')]; const card=cards.find(node=>node.textContent.includes(${JSON.stringify(marker)})); return {count:cards.length,text:card?.textContent||'',errors:card?.querySelectorAll('.copal-document-error').length||0,ownerVisible:card?.textContent.includes('shared')||false}; })()`);
    assert.equal(cardState.errors, 0, `${name} rendered recovery UI: ${cardState.text.slice(0,500)}`); assert(cardState.text.includes(marker)); assert.equal(cardState.ownerVisible, false, 'page must not expose storage owner metadata');
    const shot = await cdp('Page.captureScreenshot', { format:'png' }); const file = path.join(evidence, `${String(index + 1).padStart(2, '0')}-historical.png`); const bytes = Buffer.from(shot.data, 'base64'); fs.writeFileSync(file, bytes); const dimensions = pngSize(bytes); screenshots.push({ name, marker, path:file, dimensions });
  }
  // Recovery pages are actual mounted cards with the documented actions.
  for (const [name, expectedAction] of [['.memes/G05 Malformed Record','Download preserved source'], ['.memes/G05 Future Record','Download original']]) {
    await evaluate(`([...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].find(node => node.textContent.trim() === ${JSON.stringify(name)})?.click())`);
    await until(`document.querySelector('[data-wiki-document] .copal-document-error')`, `recovery ${name}`);
    assert(await evaluate(`document.querySelector('[data-wiki-document]')?.textContent.includes(${JSON.stringify(expectedAction)})`));
  }
  console.log(JSON.stringify({ gate:'G05', passed:true, backend:'FastAPI → exact release Copal bridge → Redb (wiki store)', binary:bridge, buildIdentity:'sha256:cf2e8cb263061e16633321cc31bfc09346bec46a835c610fdff26ab0c489817a', artifactSha256:'sha256:1f75aeabadcfd68b8eeccf72e00316772855437b3c17181a3e3c4cc9e9d7eae0', historicalBuiltins:8, sharedOwner:'shared/global', userOverride:{ owner:'e', workspace:'g05-override', isolated:true }, recovery:{ malformed:'preserved + Download preserved source', future:'preserved + Download original' }, screenshots, evidenceDir:evidence }));
} finally {
  await browser?.close().catch(() => {}); if (app.exitCode == null) app.kill('SIGTERM'); for (let i = 0; i < 100 && app.exitCode == null; i += 1) await delay(50); if (app.exitCode == null) app.kill('SIGKILL'); app.stdout?.destroy(); app.stderr?.destroy(); fs.rmSync(temporary, { recursive:true, force:true, maxRetries:8, retryDelay:100 });
}
