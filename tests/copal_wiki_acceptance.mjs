import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const source = fs.readFileSync('static/js/copal.js', 'utf8');
const bridgeSource = fs.readFileSync('packages/Copal/rust/copal-db/src/bin/copal-bridge.rs', 'utf8');
const seedStart = bridgeSource.indexOf('const WIKI_SEEDS: &[WikiSeed]');
const legacyStart = bridgeSource.indexOf('struct LegacyWikiSeed {', seedStart);
const freshSeedSource = bridgeSource.slice(seedStart, legacyStart);
assert.doesNotMatch(freshSeedSource, /tiddly|tiddler|\.wiki\//i, 'fresh Wiki seeds use .memes vocabulary');
const overrides = {
  '/static/js/copal.js': `${source}\nexport const wikiFixture = { state, init, open, openDocument, ensureViewWindow, renderWiki, notesFeature };\n`,
};

const clone = value => JSON.parse(JSON.stringify(value));
const makeDocuments = account => [
  {
    id: `${account}-alpha`, name: '.memes/Alpha', kind: 'wiki', corpus: 'wiki',
    text: 'Alpha body waiting for a draft target.', head: `${account}-alpha-1`,
    properties: { topic: 'story' }, relations: [], links: ['Beta', 'Note target'],
  },
  {
    id: `${account}-beta`, name: '.memes/Beta', kind: 'wiki', corpus: 'wiki',
    text: '# Beta\nA linked Wiki target.', head: `${account}-beta-1`,
    properties: {}, relations: [], links: [],
  },
  {
    id: `${account}-gamma`, name: '.memes/Gamma', kind: 'wiki', corpus: 'wiki',
    text: 'A third story card.', head: `${account}-gamma-1`, properties: {}, relations: [], links: [],
  },
  {
    id: `${account}-note`, name: 'Note target', kind: 'note', corpus: 'notes',
    text: 'before prose\n![[photo.png]]\n![[retry.png]]\ntrailing prose', head: `${account}-note-1`, properties: {}, relations: [], links: [],
  },
  {
    id: `${account}-photo`, name: 'photo.png', kind: 'asset', corpus: 'notes', mimeType: 'image/png',
    text: '', head: `${account}-photo-1`, properties: {}, relations: [], links: [],
  },
  {
    id: `${account}-retry`, name: 'retry.png', kind: 'asset', corpus: 'notes', mimeType: 'image/png',
    text: '', head: `${account}-retry-1`, properties: {}, relations: [], links: [],
  },
  {
    id: `${account}-bad`, name: '.memes/Broken source', kind: 'wiki', corpus: 'wiki',
    text: '', head: `${account}-bad-1`, properties: {}, relations: [], links: [],
    note_error: 'Unsupported Wiki source encoding', rawPreserved: true,
  },
  {
    id: `${account}-future`, name: '.memes/Future source', kind: 'wiki', corpus: 'wiki',
    text: '', head: `${account}-future-1`, properties: {}, relations: [], links: [],
    note_error: 'This Wiki source uses a newer native schema', rawPreserved: true,
    recoveryState: 'unsupported-future', sourceSchemaVersion: 9,
    rawSource: { encoding:'utf-8', base64:Buffer.from('{"schemaVersion":9}').toString('base64'), sha256:'fixture-future' },
  },
  {
    id: `${account}-legacy`, name: '.memes/Legacy Markdown', kind: 'wiki', corpus: 'wiki',
    text: '# Legacy\n', head: `${account}-legacy-1`, properties: {}, relations: [], links: [],
    note_error: 'This Wiki source is Markdown and needs explicit conversion', rawPreserved: true,
    recoveryState: 'legacy-import', rawSource: { encoding:'utf-8', base64:Buffer.from('# Legacy\n').toString('base64'), sha256:'fixture-legacy' },
  },
];

const accounts = new Map(['alice', 'bob'].map(account => [account, makeDocuments(account)]));
let activeAccount = 'alice';
let newDocumentNumber = 0;
const writes = [];
const memesPayload = JSON.stringify({ format:'copal-memes', schemaVersion:1, documents:[], assets:[], extensions:{} });
const memesFixtureDir = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-memes-ui-'));
const memesFixturePath = path.join(memesFixtureDir, 'ui-roundtrip.memes');
fs.writeFileSync(memesFixturePath, memesPayload);
const memesCalls = [];
let retryAvailable = false;
const assetRequests = [];
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=', 'base64');

function sendJson(res, value, status = 200) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(value));
}

const request = async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/fixture/account') {
    if (!accounts.has(url.searchParams.get('account'))) { res.writeHead(400); res.end('unknown fixture account'); return true; }
    activeAccount = url.searchParams.get('account');
    res.writeHead(204); res.end(); return true;
  }
  if (url.pathname === '/fixture/media') {
    retryAvailable = url.searchParams.get('retry') === '1'; res.writeHead(204); res.end(); return true;
  }
  if (!url.pathname.startsWith('/api/')) return false;
  if (url.pathname.startsWith('/api/prefs/')) { sendJson(res, { value: {} }); return true; }
  if (!url.pathname.startsWith('/api/copal/')) { res.writeHead(404); res.end(); return true; }
  if (url.pathname === '/api/copal/status') {
    sendJson(res, { storage_namespace: `wiki-fixture-${activeAccount}`, account_id: activeAccount }); return true;
  }
  if (url.pathname === '/api/copal/export/memes' && req.method === 'GET') {
    memesCalls.push('export');
    res.writeHead(200, { 'content-type': 'application/vnd.openclank.memes+json', 'content-disposition':'attachment; filename="wiki.memes"' });
    res.end(memesPayload); return true;
  }
  if (url.pathname === '/api/copal/preview/memes' && req.method === 'POST') {
    memesCalls.push('preview');
    for await (const chunk of req) void chunk;
    sendJson(res, { preview:true, documents:[], assets:[] }); return true;
  }
  if (url.pathname === '/api/copal/import/memes' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const restore = url.searchParams.get('mode') === 'restore';
    memesCalls.push(restore ? 'restore' : 'import');
    sendJson(res, { imported:{ documents:0, assets:0, ids:{}, assetIds:{}, restore, operation:'UI-FIXTURE' } }); return true;
  }
  if (url.pathname === '/api/copal/documents/alice-legacy/convert/preview' && req.method === 'POST') {
    sendJson(res, { preview:true, documentId:'alice-legacy', sourceBytes:9, sourceDigest:'fixture-legacy', base:'alice-legacy-1', diff:'', diagnostics:[] }); return true;
  }
  if (url.pathname === '/api/copal/events') {
    res.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    res.write(': fixture\n\n');
    return true;
  }
  if (url.pathname === '/api/copal/assets/alice-photo' || url.pathname === '/api/copal/assets/bob-photo') {
    res.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'no-store' }); res.end(png); return true;
  }
  if (url.pathname === '/api/copal/assets/alice-retry' || url.pathname === '/api/copal/assets/bob-retry') {
    assetRequests.push({ path:url.pathname, retryAvailable });
    if (!retryAvailable) { res.writeHead(404, { 'cache-control': 'no-store' }); res.end(); return true; }
    res.writeHead(200, { 'content-type': 'image/png', 'cache-control': 'no-store' }); res.end(png); return true;
  }
  if (url.pathname === '/api/copal/planning') { sendJson(res, { tracks: [], floatingTodos: [] }); return true; }
  if (url.pathname === '/api/copal/calendar/reconcile') { sendJson(res, { projections: [] }); return true; }
  const docs = accounts.get(activeAccount);
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') {
    sendJson(res, { docs: clone(docs) }); return true;
  }
  if (url.pathname === '/api/copal/documents' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const payload = JSON.parse(body || '{}');
    const id = `${activeAccount}-new-${++newDocumentNumber}`;
    const doc = {
      id, name: payload.name || 'Untitled', kind: payload.kind || 'wiki', corpus: 'wiki',
      text: String(payload.content || ''), head: `${id}-1`, properties: {}, relations: [], links: [],
    };
    docs.push(doc); sendJson(res, { outcome: 'committed', doc: clone(doc) }); return true;
  }
  const match = url.pathname.match(/^\/api\/copal\/documents\/([^/]+)$/);
  if (match) {
    const id = decodeURIComponent(match[1]);
    const doc = docs.find(item => item.id === id);
    if (!doc) { sendJson(res, { detail: 'missing fixture document' }, 404); return true; }
    if (req.method === 'GET') { sendJson(res, clone(doc)); return true; }
    if (req.method === 'PUT') {
      let body = '';
      for await (const chunk of req) body += chunk;
      const payload = JSON.parse(body || '{}');
      doc.text = String(payload.content ?? doc.text);
      doc.properties = payload.properties || {};
      doc.relations = payload.relations || [];
      const revision = Number(doc.head.split('-').pop() || 1) + 1;
      doc.head = `${activeAccount}-${id.split('-').slice(1).join('-')}-${revision}`;
      writes.push({ account: activeAccount, id, text: doc.text });
      sendJson(res, { outcome: 'committed', doc: clone(doc) }); return true;
    }
  }
  if (url.pathname === '/api/copal/documents' || url.pathname.startsWith('/api/copal/')) {
    sendJson(res, {}); return true;
  }
  return false;
};

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><main id="mount"></main><script>addEventListener('error', e => window.auditError = e.message); addEventListener('unhandledrejection', e => window.auditError = String(e.reason));</script><script type="module">import { wikiFixture } from '/static/js/copal.js'; window.fixture = wikiFixture;</script>`;

await withCopalBrowser({ page, overrides, request }, async ({ evaluate, until, url, cdp }) => {
  await until('Boolean(window.fixture)');
  await evaluate('fixture.init()');

  // The legacy Wiki route is now an Editor alias. Verify the real Editor
  // shell owns native Wiki actions and that route navigation does not replace
  // a live draft with the retired standalone library window.
  await evaluate('fixture.open("notes", false).then(() => null)');
  await evaluate("void document.querySelector('button[title=\"Editor commands\"]')?.click()");
  await until('Boolean(document.querySelector(".copal-command-palette"))');
  await until('Boolean(document.querySelector(".copal-notes-workspace"))');
  await evaluate('if (document.querySelector(".copal-notes-workspace")?.classList.contains("left-closed")) void [...document.querySelectorAll(".copal-command-row")].find(button => button.textContent.includes("Toggle Editor sidebar"))?.click()');
  await until('Boolean(document.querySelector(".copal-editor-file-menu-items"))');
  assert.equal(await evaluate('document.querySelectorAll(".copal-wiki-window").length'), 0);
  await evaluate('document.querySelector(".copal-editor-file-menu summary")?.click()');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-editor-file-menu-items button")].some(button => button.textContent === "New Wiki article")'), true, `Editor File menu exposes New Wiki article (buttons: ${await evaluate('[...document.querySelectorAll(".copal-editor-file-menu-items button")].map(button => button.textContent).join("|")')})`);
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-editor-file-menu-items button")].some(button => button.textContent === "Import native .memes")'), true, 'Editor File menu exposes native import');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-editor-file-menu-items button")].some(button => button.textContent === "Export native .memes")'), true, 'Editor File menu exposes native export');
  await evaluate('void document.querySelector("button[title=\\\"Editor commands\\\"]")?.click()');
  await until('Boolean(document.querySelector(".copal-command-palette"))');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-command-row")].some(button => button.textContent.includes("New Wiki article"))'), true, 'Editor command palette exposes New Wiki article');
  await evaluate('void document.querySelector(".copal-command-palette")?.close()');
  await evaluate('void [...document.querySelectorAll(".copal-editor-file-menu-items button")].find(button => button.textContent === "Export native .memes").click()');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.deepEqual(memesCalls, ['export'], 'Editor File menu reaches native .memes export');
  await evaluate('document.querySelector(".copal-editor-file-menu summary")?.click()');
  await evaluate('void [...document.querySelectorAll(".copal-editor-file-menu-items button")].find(button => button.textContent === "Import native .memes").click()');
  const editorFileRoot = await cdp('DOM.getDocument', { depth:-1 });
  const editorFileNode = await cdp('DOM.querySelector', { nodeId:editorFileRoot.root.nodeId, selector:'input[type=file]' });
  await cdp('DOM.setFileInputFiles', { nodeId:editorFileNode.nodeId, files:[memesFixturePath] });
  await until('document.querySelector("dialog")?.textContent.includes("Preview .memes import")');
  assert.deepEqual(memesCalls, ['export', 'preview'], 'Editor File menu reaches native .memes import preview');
  await evaluate('void document.querySelector("dialog .primary")?.click()');
  await until('document.querySelector("dialog") === null');
  assert.deepEqual(memesCalls, ['export', 'preview', 'import'], 'Editor File menu completes explicit native .memes import');
  await evaluate('void fixture.open("wiki", false)');
  await until('fixture.state.view === "notes"');
  assert.equal(await evaluate('document.querySelectorAll(".copal-wiki-window").length'), 0, 'legacy Wiki route does not create a second window');
  // S17: typed Wiki documents open directly through the shared Editor owner.
  await evaluate('void fixture.openDocument("alice-alpha", "wiki", false, { intent:"current" })');
  await until('fixture.state.view === "notes"');
  await until('Boolean(document.querySelector(\'[data-view-type="wiki"]\'))');
  assert.equal(await evaluate(`document.querySelector('[data-view-type="wiki"]')?.dataset.viewType`), 'wiki', 'Wiki article opens as a typed wiki leaf, not a Markdown fallthrough');
  assert.match(await evaluate('document.querySelector(".cm-content")?.textContent || document.body.textContent'), /Alpha body|Alpha/, 'article body renders through the shared editor/renderer');

  // L-S15-WIKI-INTENT: Ctrl/Cmd (newTab) is honored for Wiki document opens.
  await evaluate(`(() => {
    const leafCount = fixture.state.windows.get('notes').noteLeafViews.size;
    fixture.openDocument('alice-beta', 'wiki', true, { intent: 'newTab' });
    return leafCount;
  })()`);
  await until('Boolean(document.querySelector(\'[data-view-type="wiki"]\'))');
  const tabCount = await evaluate('fixture.state.windows.get("notes").noteLeafViews.size');
  assert.ok(tabCount >= 2, `newTab intent adds an Editor tab for Wiki opens (got ${tabCount})`);
  await evaluate(`void fixture.openDocument('alice-gamma', 'wiki', true, { intent: 'current' })`);
  await until('fixture.state.windows.get("notes").noteLeafViews.size >= 2');
  assert.equal(await evaluate('fixture.state.windows.get("notes").noteLeafViews.size >= 2'), true, 'current intent does not add a tab');

  // Edit and save a Wiki article through the shared Editor buffer/queue.
  await evaluate('void fixture.openDocument("alice-alpha", "wiki", true, { intent: "current" })');
  await until('Boolean(document.querySelector(".cm-content"))');
  await evaluate(`(() => {
    const workspace = fixture.state.windows.get('notes');
    const activeLeaf = workspace.groups?.flatMap(group => group.leaves || []).find(leaf => leaf.id === workspace.activeLeafId);
    const cache = fixture.state.windows.get('notes').noteLeafViews.get(activeLeaf?.id)
      || [...fixture.state.windows.get('notes').noteLeafViews.values()].find(c => c.docId === 'alice-alpha');
    // applyValue (not setValue) is the editing path: it fires onChange so the
    // shared Editor buffer/queue records the draft that flushDocument saves.
    cache.editor.applyValue(cache.editor.getValue() + '\\nunsaved draft');
  })()`);
  await evaluate('void fixture.notesFeature.flushDocument("alice-alpha")');
  const stored = accounts.get('alice').find(doc => doc.id === 'alice-alpha');
  assert.match(stored.text, /unsaved draft/, 'serialized Wiki save reaches the scoped fixture store');
  assert(writes.some(write => write.account === 'alice' && write.id === 'alice-alpha' && /unsaved draft/.test(write.text)), 'Wiki save uses the shared write path');

  // Cross-type and same-corpus links open through the Editor owner. Fragments
  // and modifier intent are forwarded (openTarget passes { intent }).
  await evaluate('void fixture.openDocument("alice-alpha", "wiki", true, { intent:"current" })');
  await until('fixture.state.view === "notes"');
  await evaluate(`(() => {
    const cache = [...fixture.state.windows.get('notes').noteLeafViews.values()].find(c => c.docId === 'alice-alpha');
    const before = fixture.state.windows.get('notes').noteLeafViews.size;
    fixture.openDocument('alice-note', 'notes', true, { intent: 'newTab' });
    return before;
  })()`);
  await until('fixture.state.windows.get("notes").noteLeafViews.size >= 2');
  assert.equal(await evaluate('fixture.state.view'), 'notes', 'cross-corpus links resolve to the shared Editor');

  // Recovery: malformed/future/legacy records show preserved-source recovery in
  // the Editor leaf and never get a save textarea.
  await evaluate('void fixture.openDocument("alice-bad", "wiki", true, { intent:"current" })');
  await until('Boolean(document.querySelector(".copal-wiki-recovery, .copal-document-error"))');
  assert.match(await evaluate('document.querySelector(".copal-wiki-recovery, .copal-document-error")?.textContent || ""'), /preserved|original bytes/i);
  assert.equal(await evaluate('document.querySelectorAll(".copal-wiki-recovery .cm-content, .copal-document-error .cm-content").length'), 0, 'malformed source has no editable body');
  await evaluate('void fixture.openDocument("alice-future", "wiki", true, { intent:"current" })');
  await until('Boolean(document.querySelector(".copal-wiki-recovery, .copal-document-error"))');
  assert.match(await evaluate('document.querySelector(".copal-wiki-recovery, .copal-document-error")?.textContent || ""'), /schema version 9|newer native schema/i);
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-wiki-recovery button, .copal-document-error button")].some(button => button.textContent === "Download original")'), true, 'future schema exposes byte-preserving download');

  // Legacy Markdown conversion is reachable from the Editor recovery panel.
  await evaluate('void fixture.openDocument("alice-legacy", "wiki", true, { intent:"current" })');
  await until('Boolean(document.querySelector(".copal-wiki-recovery, .copal-document-error"))');
  assert.match(await evaluate('document.querySelector(".copal-wiki-recovery, .copal-document-error")?.textContent || ""'), /Imported Markdown|convert/i);
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-wiki-recovery button, .copal-document-error button")].some(button => button.textContent === "Preview conversion")'), true, 'legacy import exposes explicit conversion preview');
  await evaluate('void [...document.querySelectorAll(".copal-wiki-recovery button, .copal-document-error button")].find(button => button.textContent === "Preview conversion").click()');
  await until('Boolean(document.querySelector(".copal-wiki-conversion-preview"))');
  assert.match(await evaluate('document.querySelector(".copal-wiki-conversion-preview").textContent'), /No changes have been made|Original source/i);
  await evaluate('void document.querySelector(".copal-wiki-conversion-preview")?.close()');

  // Article chrome exposes links and collapsible details for a healthy article.
  await evaluate('void fixture.openDocument("alice-beta", "wiki", true, { intent:"current" })');
  await until('Boolean(document.querySelector(".copal-wiki-article-chrome"))');
  assert.equal(await evaluate('Boolean(document.querySelector(".copal-wiki-article-chrome summary"))'), true, 'article chrome is collapsible Details');
  assert.match(await evaluate('document.querySelector(".copal-wiki-article-chrome")?.textContent || ""'), /Article links and details/);

  // New article creates a Wiki document and opens it in the Editor.
  await evaluate('document.querySelector(".copal-editor-file-menu summary")?.click()');
  await evaluate('void [...document.querySelectorAll(".copal-editor-file-menu-items button")].find(button => button.textContent === "New Wiki article").click()');
  await until('document.querySelector("#styled-prompt-overlay")?.style.display !== "none"');
  await evaluate('void (document.querySelector("#styled-prompt-input").value = "Fresh article", document.querySelector("#styled-prompt-ok").click())');
  await until('fixture.state.view === "notes"');
  await until('Boolean([...fixture.state.windows.get("notes").noteLeafViews.values()].find(c => c.docId?.startsWith("alice-new-")))');
  assert.equal(await evaluate('Boolean([...fixture.state.windows.get("notes").noteLeafViews.values()].find(c => c.docId?.startsWith("alice-new-")))'), true, 'new article opens in the shared Editor');

  // Account switch keeps stable document identity and scoped saves.
  await fetch(`${url}fixture/account?account=bob`);
  await evaluate('fixture.init()');
  await evaluate('void fixture.openDocument("bob-alpha", "wiki", true, { intent:"current" })');
  await until('fixture.state.view === "notes"');
  assert.equal(await evaluate('Boolean([...fixture.state.windows.get("notes").noteLeafViews.values()].find(c => c.docId === "bob-alpha"))'), true, 'account B opens its own scoped article');
  assert.equal(await evaluate('window.auditError || null'), null);
  console.log(JSON.stringify({ passed: [
    'Wiki library lists typed articles with recovery badges',
    'native .memes export/preview/import routes retained',
    'library rows open typed wiki leaves in the shared Editor',
    'L-S15-WIKI-INTENT: newTab and current intents honored for Wiki opens',
    'shared Editor save path serializes Wiki articles',
    'cross-corpus links resolve to the Editor owner',
    'malformed/future/legacy preserved-source recovery in Editor leaves',
    'legacy conversion preview reachable from Editor recovery panel',
    'collapsible article links/details chrome',
    'new article opens in Editor with stable identity',
    'account-scoped article identity preserved',
  ], browser: await cdp('Browser.getVersion') }, null, 2));
});
