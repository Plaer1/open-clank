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
  await evaluate('fixture.open("wiki", false)');
  await until('document.querySelectorAll("[data-wiki-document]").length === 3');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-pane-header button")].some(button => button.textContent === "Export .memes")'), true, 'Wiki exposes native .memes export');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-pane-header button")].some(button => button.textContent === "Import .memes")'), true, 'Wiki exposes explicit .memes preview/import');
  assert.match(await evaluate('document.querySelector("input[type=file]")?.accept || ""'), /\.memes/);
  await evaluate('([...document.querySelectorAll(".copal-pane-header button")].find(button => button.textContent === "Export .memes")).click()');
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.deepEqual(memesCalls, ['export'], 'visible Export .memes uses the production route');
  await evaluate('([...document.querySelectorAll(".copal-pane-header button")].find(button => button.textContent === "Import .memes")).click()');
  const fileRoot = await cdp('DOM.getDocument', { depth:-1 });
  const fileNode = await cdp('DOM.querySelector', { nodeId:fileRoot.root.nodeId, selector:'input[type=file]' });
  await cdp('DOM.setFileInputFiles', { nodeId:fileNode.nodeId, files:[memesFixturePath] });
  await until('document.querySelector("dialog")?.textContent.includes("Preview .memes import")');
  assert.equal(await evaluate('document.querySelector("dialog")?.textContent.includes("No changes have been made")'), true, 'visible import requires an explicit preview decision');
  await evaluate('document.querySelector("dialog .primary")?.click()');
  await until('document.querySelector("dialog") === null');
  assert.deepEqual(memesCalls, ['export', 'preview', 'import'], 'visible preview then explicit Import .memes uses both production routes');
  await evaluate('([...document.querySelectorAll(".copal-pane-header button")].find(button => button.textContent === "Import .memes")).click()');
  const restoreFileRoot = await cdp('DOM.getDocument', { depth:-1 });
  const restoreFileNode = await cdp('DOM.querySelector', { nodeId:restoreFileRoot.root.nodeId, selector:'input[type=file]' });
  await cdp('DOM.setFileInputFiles', { nodeId:restoreFileNode.nodeId, files:[memesFixturePath] });
  await until('document.querySelector("dialog")?.textContent.includes("Restore current Wiki")');
  assert.equal(await evaluate('document.querySelector("dialog")?.textContent.includes("Import for new scoped copies")'), true, 'restore is an explicit second choice after preview');
  await evaluate('([...document.querySelectorAll("dialog button")].find(button => button.textContent === "Restore current Wiki")).click()');
  await until('document.querySelector("dialog") === null');
  assert.deepEqual(memesCalls, ['export', 'preview', 'import', 'preview', 'restore'], 'visible restore choice marks the guarded API mode');

  const initialStory = await evaluate('fixture.state.story.slice()');
  assert.deepEqual(initialStory, ['alice-alpha', 'alice-beta', 'alice-gamma'], 'defaults are seeded once in deterministic corpus order');
  assert.doesNotMatch(await evaluate('document.body.textContent'), /tiddly|tiddler/i, 'visible Wiki UI has no retired terminology');
  await evaluate('fixture.renderWiki()');
  assert.deepEqual(await evaluate('fixture.state.story.slice()'), initialStory, 'rerender does not append defaults again');

  // Type through the real Wiki textarea, then exercise all presentation controls
  // before the delayed Notes save is explicitly flushed.
  const expectedSelection = await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent === 'Edit').click(); const editor = document.querySelector('[data-wiki-document="alice-alpha"] .copal-wiki-editor'); editor.focus(); editor.setSelectionRange(6, 10); editor.setRangeText(' unsaved draft', 6, 10, 'select'); editor.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:' unsaved draft' })); return [editor.selectionStart, editor.selectionEnd]; })()`);
  await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent === 'Pin').click(); })()`);
  await until('document.querySelector("[data-wiki-document=\\"alice-alpha\\"]")?.classList.contains("pinned")');
  await evaluate(`document.querySelector('[data-wiki-document="alice-alpha"] [aria-label="Move right"]').click()`);
  await evaluate(`(() => { const search = document.querySelector('.copal-search'); search.value = 'Beta'; search.dispatchEvent(new Event('input', { bubbles:true })); })()`);
  await until('document.querySelectorAll("[data-wiki-library] .copal-doc-row").length === 1');
  assert.equal(await evaluate('fixture.state.story.length'), 3, 'library filtering leaves story membership/order intact');
  // The explicit button lookup keeps this assertion tied to the native Edit/Read
  // control while avoiding class or DOM-index assumptions for the card controls.
  await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent === 'Read').click(); })()`);
  await until('document.querySelector("[data-wiki-document=\\"alice-alpha\\"] .copal-wiki-editor") === null');
  await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent === 'Edit').click(); })()`);
  await until('Boolean(document.querySelector("[data-wiki-document=\\"alice-alpha\\"] .copal-wiki-editor"))');
  const restored = await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); return { text:card.querySelector('.copal-wiki-editor').value, selection:[card.querySelector('.copal-wiki-editor').selectionStart, card.querySelector('.copal-wiki-editor').selectionEnd] }; })()`);
  assert.match(restored.text, /unsaved draft/, 'draft survives pin, reorder, filter and read/edit');
  assert.deepEqual(restored.selection, expectedSelection, 'the transformed selection range is restored exactly');
  await evaluate('fixture.notesFeature.flushDocument("alice-alpha")');
  // The server-side write is inspected through a fixture-owned endpoint below;
  // this avoids claiming persistence from a DOM overlay.
  const stored = accounts.get('alice').find(doc => doc.id === 'alice-alpha');
  assert.match(stored.text, /unsaved draft/, 'serialized Wiki save reaches the scoped fixture store');
  assert(writes.some(write => write.account === 'alice' && write.id === 'alice-alpha' && /unsaved draft/.test(write.text)));

  // Cross-corpus and same-corpus links use the production footer handlers.
  await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent.includes('→ Note target')).click(); })()`);
  await until('fixture.state.view === "notes"');
  await until('Boolean(document.querySelector(".cm-md-embed-widget"))');
  await until('Boolean(document.querySelector(".cm-md-embed-widget .copal-media-embed[data-reference-status=loaded] img"))');
  const liveText = await evaluate('document.querySelector(".cm-content")?.textContent || ""');
  assert.match(liveText, /before/); assert.match(liveText, /trailing prose/);
  await until('document.querySelector(".cm-md-embed-widget [data-reference-status=error] button")');
  await fetch(`${url}fixture/media?retry=1`);
  await evaluate('document.querySelector(".cm-md-embed-widget [data-reference-status=error] button").click()');
  await until('document.querySelectorAll(".cm-md-embed-widget .copal-media-embed[data-reference-status=loaded] img").length === 2');
  assert.deepEqual(assetRequests.map(request => request.retryAvailable), [false, true], 'the production Retry callback requests the resource again');
  await evaluate('document.querySelector(".cm-md-embed-widget").dispatchEvent(new KeyboardEvent("keydown", { key:"Enter", bubbles:true }))');
  await until('document.activeElement?.classList.contains("cm-content")');
  assert((await evaluate('[...fixture.state.windows.get("notes").noteLeafViews.values()].find(cache => cache.docId === "alice-note").editor.getSelection().anchor')) > 0, 'live media widget Enter reveals its source position');
  await evaluate('fixture.open("wiki", false)');
  await evaluate(`(() => { const card = document.querySelector('[data-wiki-document="alice-alpha"]'); [...card.querySelectorAll('button')].find(button => button.textContent.includes('→ Beta')).click(); })()`);
  await until('fixture.state.view === "wiki" && fixture.state.selected === "alice-beta"');

  // A malformed record is shown as preserved source recovery and never gets an
  // Edit button or a save textarea.
  await evaluate(`(() => { const search = document.querySelector('.copal-search'); search.value = ''; search.dispatchEvent(new Event('input', { bubbles:true })); })()`);
  await until('document.querySelectorAll("[data-wiki-library] .copal-doc-row").length === 6');
  assert.equal(await evaluate('[...document.querySelectorAll("[data-wiki-library] .copal-doc-row")].some(button => button.textContent.includes("Broken source"))'), true, 'malformed source is present in the unfiltered library');
  await evaluate(`(() => { const row = [...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].find(button => button.textContent.includes('Broken source')); row.click(); })()`);
  await until('Boolean(document.querySelector("[data-wiki-document=\\"alice-bad\\"]"))');
  assert.match(await evaluate('document.querySelector("[data-wiki-document=\\"alice-bad\\"]").textContent'), /preserved|original bytes/i);
  assert.equal(await evaluate('document.querySelectorAll("[data-wiki-document=\\"alice-bad\\"] .copal-wiki-editor").length'), 0);
  assert.equal(await evaluate('[...document.querySelectorAll("[data-wiki-document=\\"alice-bad\\"] button")].some(button => ["Edit","Read","Save"].includes(button.textContent))'), false);

  // Future native schemas remain explicitly read-only and expose their
  // version plus byte-preserving original download. Legacy Markdown reaches
  // the dedicated preview/CAS conversion branch.
  await evaluate(`(() => { const row = [...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].find(button => button.textContent.includes('Future source')); row.click(); })()`);
  await until('Boolean(document.querySelector("[data-wiki-document=\\"alice-future\\"]"))');
  assert.match(await evaluate('document.querySelector("[data-wiki-document=\\"alice-future\\"]").textContent'), /schema version 9|newer native schema/i);
  assert.equal(await evaluate('[...document.querySelectorAll("[data-wiki-document=\\"alice-future\\"] button")].some(button => button.textContent === "Download original")'), true);
  assert.equal(await evaluate('[...document.querySelectorAll("[data-wiki-document=\\"alice-future\\"] button")].some(button => ["Edit","Read","Save"].includes(button.textContent))'), false);
  await evaluate(`(() => { const row = [...document.querySelectorAll('[data-wiki-library] .copal-doc-row')].find(button => button.textContent.includes('Legacy Markdown')); row.click(); })()`);
  await until('Boolean(document.querySelector("[data-wiki-document=\\"alice-legacy\\"]"))');
  await evaluate('[...document.querySelectorAll("[data-wiki-document=\\"alice-legacy\\"] button")].find(button => button.textContent === "Preview conversion").click()');
  await until('Boolean(document.querySelector(".copal-wiki-conversion-preview"))');
  assert.match(await evaluate('document.querySelector(".copal-wiki-conversion-preview").textContent'), /No changes have been made|Original source/i);
  await evaluate('document.querySelector(".copal-wiki-conversion-preview")?.close()');

  // Unpin then close every card. Empty is an intentional persisted state and
  // survives a render plus a real Page.reload in the same disposable profile.
  await evaluate(`(() => { for (const card of [...document.querySelectorAll('[data-wiki-document]')]) { const unpin = [...card.querySelectorAll('button')].find(button => button.textContent === 'Unpin'); if (unpin) unpin.click(); } })()`);
  await evaluate(`(() => { let card; while ((card = document.querySelector('[data-wiki-document]'))) { [...card.querySelectorAll('button')].find(button => button.getAttribute('aria-label') === 'Close meme').click(); } })()`);
  await until('fixture.state.story.length === 0 && document.querySelector(".copal-empty")');
  await evaluate('fixture.renderWiki()');
  assert.equal(await evaluate('fixture.state.story.length'), 0, 'close-all remains empty after rerender');
  await evaluate('history.replaceState({}, "", "/")');
  await cdp('Page.reload', { ignoreCache:true });
  await until('typeof window.fixture?.init === "function"');
  assert.equal(await evaluate('window.auditError || null'), null, 'reload has no browser error or rejected promise');
  await evaluate('fixture.init()');
  await evaluate('fixture.open("wiki", false)');
  await until('document.querySelector(".copal-empty")');
  assert.equal(await evaluate('fixture.state.story.length'), 0, 'close-all remains empty after reload');

  // Account B gets independent defaults; returning to A restores its empty
  // presentation from the account-scoped localStorage key.
  await fetch(`${url}fixture/account?account=bob`);
  await evaluate('fixture.init()');
  await evaluate('fixture.open("wiki", false)');
  await until('fixture.state.story.length === 3');
  assert.deepEqual(await evaluate('fixture.state.story.slice()'), ['bob-alpha', 'bob-beta', 'bob-gamma']);
  await fetch(`${url}fixture/account?account=alice`);
  await evaluate('fixture.init()');
  await evaluate('fixture.open("wiki", false)');
  await until('document.querySelector(".copal-empty")');
  assert.equal(await evaluate('fixture.state.story.length'), 0, 'account A does not inherit account B presentation');

  // New Meme follows the production form/create path and focuses its editor.
  await evaluate('[...document.querySelectorAll("button")].find(button => button.textContent === "+ Meme").click()');
  await until('document.querySelector("#styled-prompt-overlay")?.style.display !== "none"');
  await evaluate('document.querySelector("#styled-prompt-input").value = "Fresh meme"; document.querySelector("#styled-prompt-ok").click()');
  await until('document.querySelector("[data-wiki-document]")?.dataset.wikiDocument.startsWith("alice-new-")');
  await until('document.activeElement?.matches(".copal-wiki-editor")');
  assert.equal(await evaluate('document.activeElement.matches(".copal-wiki-editor")'), true, 'new Meme enters editing with focus');
  assert.equal(await evaluate('window.auditError || null'), null);
  console.log(JSON.stringify({ passed: [
    'initial defaults exactly once', 'draft and selection through pin/reorder/filter/read/edit',
    'scoped serialized save', 'close-all empty persistence across rerender/init/reload',
    'account-isolated Wiki layouts', 'same-corpus and cross-corpus links',
    'production Notes live-mode media, Retry and source reveal',
    'malformed/future raw-preserving recovery', 'legacy preview conversion and visible .memes export/preview/import/restore choices',
    'new Meme editing focus',
  ], browser: await cdp('Browser.getVersion') }, null, 2));
});
