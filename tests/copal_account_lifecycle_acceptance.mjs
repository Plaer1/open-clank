import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const source = fs.readFileSync('static/js/copal.js', 'utf8');
const overrides = {
  '/static/js/copal.js':source + '\nexport const scopeFixture = { state, notesFeature, api, loadDocuments, suspendCopalScope };\n',
};
const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css">
<a href="/copal/graph" data-copal-view="graph">Graph</a><a href="/copal/notes" data-copal-view="notes">Notes</a>
<script type="module">
import Copal, { scopeFixture } from '/static/js/copal.js';
window.Copal = Copal; window.fixture = scopeFixture;
window.errors = []; window.addEventListener('unhandledrejection', e => window.errors.push(String(e.reason)));
</script>`;
let account = 'alice';
let holdDocuments = false;
const delayedDocuments = [];
const writes = [];
const counts = new Map();
const docFor = owner => ({
  id:`${owner}-note`, name:`${owner}.md`, kind:'note', head:`${owner}-head`, text:`${owner} accepted`, properties:{ title:owner }, relations:[], links:[],
  resource:{
    key:{ accountId:`account-${owner}`, workspaceId:'default', provider:'copal', resourceId:`origin-${owner}` },
    revision:{ kind:'copalHead', value:`${owner}-head` }, representation:'nativeNote',
    locator:{ displayName:`${owner}.md`, locationLabel:`${owner}.md` }, capabilities:{ read:true, edit:true },
  },
});
const request = async (req, res) => {
  if (!req.url.startsWith('/api/')) return false;
  const pathname = new URL(req.url, 'http://fixture').pathname;
  counts.set(pathname, (counts.get(pathname) || 0) + 1);
  if (req.method !== 'GET') writes.push({ path:pathname, account:req.headers['x-copal-account'] });
  const json = value => { res.setHeader('content-type', 'application/json'); res.end(JSON.stringify(value)); };
  if (pathname === '/api/copal/status') json({ account_id:`account-${account}`, storage_namespace:`user:${account}` });
  else if (pathname === '/api/prefs/copal_entry_visibility') json({ value:{} });
  else if (pathname === '/api/copal/documents') {
    const value = { docs:[docFor(account)] };
    if (holdDocuments) delayedDocuments.push(() => json(value)); else json(value);
  } else if (pathname === '/api/copal/planning') json({ tracks:[], floatingTodos:[] });
  else if (pathname === '/api/copal/calendar/reconcile') json({});
  else if (pathname === '/api/copal/events') { res.writeHead(200, { 'content-type':'text/event-stream' }); res.write(': fixture\n\n'); }
  else json({});
  return true;
};

await withCopalBrowser({ page, overrides, request }, async ({ evaluate, until, cdp }) => {
  await until('Boolean(window.fixture)');
  await evaluate('Copal.init()');
  await evaluate('fixture.loadDocuments(false); window.initialReady = false');
  await until('fixture.state.docs.length === 1');
  await evaluate(`fixture.notesFeature.getSettings(); fixture.notesFeature.queueSave(fixture.state.docs[0], 'alice unsaved'); window.oldWindow = fixture.state.windows.get('notes').window.root; window.oldBuffer = [...fixture.state.windows.get('notes').noteBuffers.values()][0]`);
  holdDocuments = true;
  await evaluate('window.oldRead = fixture.loadDocuments(false)', false);
  while (!delayedDocuments.length) await new Promise(resolve => setTimeout(resolve, 10));
  const noteWrites = writes.filter(item => item.path.includes('/documents/')).length;
  account = 'bob';
  await evaluate('Copal.init()');
  const switched = await evaluate(`({account:fixture.state.accountId,docs:fixture.state.docs.length,oldConnected:window.oldWindow.isConnected,oldInvalidated:window.oldBuffer.invalidated,windows:document.querySelectorAll('.copal-view-window').length,recovery:Object.keys(localStorage).filter(k=>k.startsWith('copal-buffer-draft:')).map(k=>localStorage.getItem(k))})`);
  assert.equal(switched.account, 'account-bob');
  assert.equal(switched.docs, 0);
  assert.equal(switched.oldConnected, false);
  assert.equal(switched.oldInvalidated, true);
  assert.equal(switched.windows, 7);
  assert.deepEqual(await evaluate('[...document.querySelectorAll(".copal-view-window")].map(node => node.dataset.windowId || node.id).sort()'), ['copal-graph-modal', 'copal-mind-modal', 'copal-notes-modal', 'copal-timeline-modal', 'copal-todo-modal', 'copal-treehouse-modal', 'copal-wiki-modal']);
  assert.equal(await evaluate('document.querySelectorAll(".copal-bases-window, .copal-galaxy-window, .copal-code-window").length'), 0);
  assert(switched.recovery.some(value => value.includes('alice unsaved')));
  assert.equal(writes.filter(item => item.path.includes('/documents/')).length, noteWrites, 'scope suspension must not save old drafts');
  holdDocuments = false;
  delayedDocuments.splice(0).forEach(deliver => deliver());
  await evaluate('window.oldRead');
  assert.equal(await evaluate('fixture.state.docs.length'), 0, 'old document response must not populate Bob');
  await evaluate('fixture.loadDocuments(false)');
  assert.deepEqual(await evaluate('fixture.state.docs.map(d=>d.id)'), ['bob-note']);
  const before = counts.get('/api/copal/documents');
  await evaluate('fixture.state.docs = []; document.querySelector("[data-copal-view=graph]").click()');
  await until('Boolean(document.querySelector("[data-graph-mode]"))');
  assert.equal(counts.get('/api/copal/documents'), before + 1, 'reinitialization must not duplicate launcher callbacks');
  account = 'alice';
  await evaluate('history.replaceState({}, "", "/"); Copal.init()');
  await evaluate('fixture.loadDocuments(false);');
  const recovery = await evaluate(`fixture.notesFeature.getSettings(); ({value:fixture.state.windows.get('notes').noteDrafts.get('alice-note')?.value, newBuffer:[...fixture.state.windows.get('notes').noteBuffers.values()][0] !== window.oldBuffer})`);
  assert.equal(recovery.value, 'alice unsaved');
  assert.equal(recovery.newBuffer, true);
  await evaluate(`Copal.open('notes'); fixture.notesFeature.open('alice-note')`);
  await until('Boolean(document.querySelector(".copal-note-status"))');
  await evaluate(`window.storageSetItem = Storage.prototype.setItem; Storage.prototype.setItem = function(key, value) { if (key.startsWith('copal-buffer-draft:')) throw new DOMException('fixture quota', 'QuotaExceededError'); return window.storageSetItem.call(this, key, value); }; fixture.notesFeature.queueSave(fixture.state.docs[0], 'alice latest memory-only draft'); window.quotaBuffer = [...fixture.state.windows.get('notes').noteBuffers.values()][0]`);
  assert.match(await evaluate('document.querySelector(".copal-note-status").textContent'), /Restart recovery unavailable/);
  const quotaWrites = writes.filter(item => item.path.includes('/documents/')).length;
  account = 'bob';
  await evaluate('history.replaceState({}, "", "/"); Copal.init()');
  assert.equal(await evaluate('window.quotaBuffer.invalidated'), true, 'draft quota cannot prevent old owner detachment');
  assert.equal(await evaluate('fixture.state.accountId'), 'account-bob');
  assert.equal(writes.filter(item => item.path.includes('/documents/')).length, quotaWrites);
  account = 'alice';
  await evaluate('Copal.init();');
  await evaluate('fixture.loadDocuments(false)');
  assert.equal(await evaluate(`fixture.notesFeature.getSettings(); fixture.state.windows.get('notes').noteDrafts.get('alice-note').value`), 'alice latest memory-only draft', 'same-session recovery uses latest failed draft instead of stale storage');
  await evaluate('Storage.prototype.setItem = window.storageSetItem');
  assert.deepEqual(await evaluate('window.errors'), []);
  console.log(JSON.stringify({ passed:['production init disposes old owners without writes', 'late document read ignored', 'one launcher callback after reinit', 'same-owner draft recovery in fresh epoch', 'quota warning and owner detachment with latest same-session draft recovery'], browser:await cdp('Browser.getVersion') }, null, 2));
});
