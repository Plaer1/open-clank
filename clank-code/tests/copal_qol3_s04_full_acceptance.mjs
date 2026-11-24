#!/usr/bin/env node

/*
 * S04 qualification gate.  This deliberately uses a disposable, production
 * shaped HTTP fixture and keeps the assertions stricter than the small demo
 * acceptance.  It is a gate for the mounted Editor surface, not a replacement
 * for the real route/browser run in S06.
 */
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const account = 's04-account';
const workspace = 's04-workspace';
const resource = (id, revision, representation) => ({
  key:{ accountId:account, workspaceId:workspace, provider:'copal', resourceId:id },
  revision:{ kind:'copalHead', value:revision },
  locator:{ displayName:id, locationLabel:id }, representation,
  capabilities:{ read:true, edit:representation !== 'base', rename:true, trash:true, reveal:true },
});
const definition = {
  version:1, extensions:{ title:'Projects/To Watch.base', unknown:'preserve-me' },
  views:[
    { id:'table', name:'To Watch', type:'table', columns:[
      { property:'file.name', label:'Name' }, { property:'file.tags', label:'Tags' },
      { property:'status', label:'Status' }, { property:'priority', label:'Priority', type:'number', visible:false },
      { property:'done', label:'Done', type:'boolean' }, { property:'due', label:'Due', type:'date' },
      { property:'score', label:'Score', type:'number' },
    ], sorts:[{ property:'file.tags', direction:'asc' }, { property:'file.name', direction:'asc' }], filters:{ and:[
      { property:'status', operator:'equals', value:'unwatched' },
      { property:'file.path', operator:'not_contains', value:'Projects/Obsidian/Templates' },
    ] }, summaries:{}, limit:100 },
    { id:'list', name:'List', type:'list', columns:[{ property:'file.name', label:'Name' }, { property:'status', label:'Status' }], sorts:[], filters:null, summaries:{}, limit:100 },
    { id:'card', name:'Cards', type:'card', columns:[{ property:'file.name', label:'Name' }, { property:'status', label:'Status' }], sorts:[], filters:null, summaries:{}, limit:100 },
  ],
};
const baseSource = [
  'version: 1',
  'views:',
  '  - id: table',
  '    name: To Watch',
  '    type: table',
  '    columns: [file.name, file.tags, status, priority, done]',
  '    columnSize: {note.status: 82}',
  '    sorts:',
  '      - {property: file.tags, direction: asc}',
  '      - {property: file.name, direction: asc}',
  '    # preserve this comment and unknown keys',
  '    unknownField: keep-me',
].join('\n') + '\n';
const sources = Array.from({ length:5_001 }, (_, index) => {
  const id = `source-${index}`; const head = `source-head-${index}`;
  return {
    id, kind:'note', name:`Projects/Row-${String(index).padStart(4, '0')}.md`, head,
    text:`---\nstatus: unwatched\n---\n# Row ${index}\n`,
    properties:{ status:'unwatched', priority:index, done:false, due:'2026-09-12', score:index }, relations:[], tags:[index % 2 ? 'home' : 'work'], links:[],
    resource:resource(id, head, 'nativeNote'),
  };
});
const base = { id:'base-1', kind:'base', name:'Projects/To Watch.base', head:'base-head-1', text:baseSource, properties:{}, relations:[], tags:[], links:[], resource:resource('base-1', 'base-head-1', 'base') };
const state = { account, writes:[], queries:[], conflicts:0, sourceRevision:'base-head-1', eventResponse:null, delayed:false, rowIdentityMismatch:false, history:[{ id:'h1', message:'Initial', ts:'2026-09-12T00:00:00Z' }] };

function json(res, value, status = 200) { res.writeHead(status, { 'content-type':'application/json' }); res.end(JSON.stringify(value)); return true; }
async function readBody(req) { const chunks = []; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; }
function currentSources() { return state.account === account ? sources : [sources[0]]; }
// Keep the indexed corpus count at 5,001 while limiting the mounted document
// catalogue to the first page.  The production query still supplies the
// authoritative count and 100 rendered rows; this keeps the disposable
// browser fixture bounded and repeatable.
function currentDocs() { return [base, ...currentSources().slice(0, 100)]; }
function selectedView(viewId) { return definition.views.find((view) => view.id === viewId) || definition.views[0]; }
function queryRows(view, query = '') {
  const list = String(query).toLowerCase() === 'fast' ? currentSources().slice(1, 101) : String(query).toLowerCase() === 'slow' ? currentSources().slice(0, 100) : currentSources().slice(0, 100);
  return list.map((doc) => ({ documentId:doc.id, head:doc.head, name:doc.name, kind:doc.kind, resourceKey:state.rowIdentityMismatch && doc.id === 'source-0' ? resource('unknown-source', doc.head, 'nativeNote').key : doc.resource.key, values:{
    'file.name':doc.name, 'file.tags':doc.tags, status:doc.properties.status, priority:doc.properties.priority, done:doc.properties.done,
    due:doc.properties.due, score:doc.properties.score,
  }, errors:[] }));
}

const page = `<!doctype html><meta charset="utf-8"><body><nav><a href="/copal/bases" data-copal-view="bases">Bases</a></nav><main id="app"></main><script type="module">window.__run=async()=>{const module=await import('/static/js/copal.js?s04-full');await module.init(location.origin);window.__copal=module.default;};</script>`;

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') return json(res, { account_id:state.account, storage_namespace:`${state.account}-storage` });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, { 'content-type':'text/event-stream' }); res.write(': fixture\n\n'); state.eventResponse = res; return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:currentDocs() });
  const documentMatch = url.pathname.match(/^\/api\/copal\/documents\/([^/]+)$/);
  if (documentMatch && req.method === 'GET') {
    const doc = currentDocs().find((item) => item.id === documentMatch[1]);
    return json(res, doc || { detail:'missing' }, doc ? 200 : 404);
  }
  if (documentMatch && req.method === 'PUT') {
    const doc = currentDocs().find((item) => item.id === documentMatch[1]);
    if (!doc) return json(res, { detail:'missing' }, 404);
    const payload = await readBody(req); state.writes.push({ id:doc.id, payload });
    if (doc.id === 'source-0' && state.conflicts === 0) { state.conflicts += 1; return json(res, { detail:{ outcome:'stale', doc } }, 409); }
    if (doc.id === 'base-1') { base.text = payload.content; state.sourceRevision = 'base-head-2'; base.head = state.sourceRevision; }
    else { doc.text = payload.content; doc.properties = payload.properties || doc.properties; doc.head = `${doc.head}-saved`; }
    return json(res, { outcome:'committed', actionId:payload.actionId, doc:{ ...doc }, receipt:{ outcome:'applied', actionId:payload.actionId, resourceId:doc.id } });
  }
  const queryMatch = url.pathname.match(/^\/api\/copal\/bases\/base-1\/query$/);
  if (queryMatch && req.method === 'GET') {
    state.queries.push({ query:url.searchParams.get('query') || '', view:url.searchParams.get('view') || 'table' });
    const query = url.searchParams.get('query') || '';
    if (state.delayed && query === 'slow') await new Promise((resolve) => setTimeout(resolve, 180));
    const view = selectedView(url.searchParams.get('view'));
    const total = state.account === account ? 5_001 : 1;
    return json(res, { base, definition, view, rows:queryRows(view, query), groups:[], summaries:{}, page:1, pageSize:100, pages:Math.ceil(total / 100), total, matchedCount:total, sourceCount:total, resultLimited:total > 100, resultLimit:100, sourceTruncated:false, queryComplete:true, definitionRevision:base.head });
  }
  if (url.pathname === '/api/copal/bases/base-1/history' && req.method === 'GET') return json(res, { history:state.history });
  if (url.pathname === '/api/copal/documents/base-1/history' && req.method === 'GET') return json(res, { changes:state.history.map((item) => ({ ...item, commit:item.id })) });
  if (url.pathname === '/api/test-delay' && req.method === 'POST') { state.delayed = Boolean((await readBody(req)).enabled); return json(res, { delayed:state.delayed }); }
  if (url.pathname === '/api/test-row-identity' && req.method === 'POST') { state.rowIdentityMismatch = Boolean((await readBody(req)).enabled); return json(res, { rowIdentityMismatch:state.rowIdentityMismatch }); }
  if (url.pathname === '/api/test-account' && req.method === 'POST') { state.account = String((await readBody(req)).account); return json(res, { account:state.account }); }
  if (url.pathname === '/api/test-state') return json(res, { account:state.account, writes:state.writes, queries:state.queries, conflicts:state.conflicts, sourceRevision:state.sourceRevision, source0:{ text:sources[0].text, properties:{...sources[0].properties} }, source1:{ text:sources[1].text, properties:{...sources[1].properties} } });
  return false;
}}, async ({ evaluate, until }) => {
  const failures = [];
  const metrics = {};
  let fatalScenario = null;
  async function check(name, callback) {
    if (fatalScenario) return;
    try { await callback(); } catch (error) {
      const message = String(error?.message || error);
      const labeled = message.startsWith(`${name}:`) ? message : `${name}: ${message}`;
      fatalScenario = new Error(labeled, { cause:error });
      failures.push(labeled);
    }
  }
  async function waitFor(expression, name = expression) {
    try { await until(expression, name); return true; } catch (error) { throw new Error(`${name}: ${error.message}`, { cause:error }); }
  }
  async function ensureTable(name = 'sheet for check', { openIfMissing = true, queryAfter = null } = {}) {
    const ready = '(()=>{const surface=document.querySelector(".copal-sheet-surface[data-sheet-status=ready]");const table=surface?.querySelector(".copal-sheet-grid");const rows=table?.querySelectorAll("tbody tr").length || 0;const cells=surface?.querySelectorAll(".copal-sheet-cell").length || 0;const row=table?.querySelector("tbody tr:first-child");const keys=[...(row?.querySelectorAll(".copal-sheet-cell") || [])].map((node)=>node.getAttribute("data-sheet-column-key"));const expected=["file.name","file.tags","status","done","due","score"];return Boolean(surface && surface.getAttribute("aria-busy") !== "true" && table && rows === 100 && cells === 600 && JSON.stringify(keys) === JSON.stringify(expected) && keys.includes("due"));})()';
    const target = queryAfter == null ? ready : `fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.length > ${queryAfter} && (${ready}))`;
    if (await evaluate(target)) return;
    if (!await evaluate('Boolean(document.querySelector(".copal-sheet-surface"))')) {
      if (openIfMissing) await evaluate('window.__copal.open("bases",false)');
    } else if (openIfMissing) {
      await evaluate('([...document.querySelectorAll(".copal-sheet-tab")].find((node)=>node.textContent.trim() === "To Watch") || document.querySelector(".copal-sheet-tab"))?.click()');
    }
    await waitFor(target, name);
  }

  await evaluate(`(()=>{
    window.__s04Listeners = { adds:0, removes:0 };
    window.__s04LiveSheetListeners = [];
    window.__s04LongTasks = [];
    if (window.PerformanceObserver) {
      try { const observer = new PerformanceObserver((list) => window.__s04LongTasks.push(...list.getEntries().map((entry) => entry.duration))); observer.observe({ type:'longtask', buffered:true }); window.__s04LongTaskObserver = observer; } catch (_) {}
    }
    const originalAdd = EventTarget.prototype.addEventListener;
    const originalRemove = EventTarget.prototype.removeEventListener;
    const sheetTarget = (target) => target?.classList?.contains('copal-note-leaf-content') || target?.classList?.contains('copal-sheet-surface');
    const owner = (target) => ({ target:target?.classList?.contains('copal-sheet-surface') ? 'surface' : 'leaf-content', leafId:target?.closest?.('.copal-note-leaf')?.dataset?.leafId || null, className:String(target?.className || '') });
    EventTarget.prototype.addEventListener = function(type, listener, options) { if (sheetTarget(this)) { window.__s04Listeners.adds += 1; window.__s04LiveSheetListeners.push({ target:this, type:String(type), listener, capture:options === true || Boolean(options?.capture), owner:owner(this) }); } return originalAdd.call(this, type, listener, options); };
    EventTarget.prototype.removeEventListener = function(type, listener, options) { if (sheetTarget(this)) { window.__s04Listeners.removes += 1; const capture=options === true || Boolean(options?.capture); const index=window.__s04LiveSheetListeners.findIndex((item)=>item.target === this && item.listener === listener && item.type === String(type) && item.capture === capture); if (index >= 0) window.__s04LiveSheetListeners.splice(index, 1); } return originalRemove.call(this, type, listener, options); };
  })()`);
  console.error('S04 gate: initializing Editor');
  await evaluate('window.__run()');
  console.error('S04 gate: opening Bases');
  await evaluate('window.__copal.open("bases")');
  console.error('S04 gate: waiting for sheet');
  await ensureTable('mounted Editor sheet');

  await check('5,001 source rows / <=100 rendered rows / counts', async () => {
    const result = await evaluate(`({ rows:document.querySelectorAll('.copal-sheet-grid tbody tr').length, cells:document.querySelectorAll('.copal-sheet-cell').length, footer:document.querySelector('.copal-sheet-footer')?.textContent || '', rowCount:document.querySelector('.copal-sheet-grid')?.getAttribute('aria-rowcount') })`);
    assert.equal(result.rows, 100); assert.ok(result.cells <= result.rows * 6); assert.match(result.footer, /5001 matched/); assert.equal(result.rowCount, '5001');
    metrics.sourceBounds = result;
    const idle = await evaluate(`(async()=>{const before=await fetch('/api/test-state').then((response)=>response.json());const started=performance.now();await new Promise((resolve)=>setTimeout(resolve,750));const after=await fetch('/api/test-state').then((response)=>response.json());return {durationMs:performance.now()-started,before:before.queries.length,after:after.queries.length,queryDelta:after.queries.length-before.queries.length};})()`);
    assert.equal(idle.queryDelta, 0, JSON.stringify(idle)); metrics.idle = idle;
  });
  await check('To Watch source semantics and labels', async () => {
    const result = await evaluate(`({ title:document.querySelector('.copal-sheet-title h2')?.textContent, labels:[...document.querySelectorAll('.copal-sheet-grid th')].map((node)=>node.textContent), nested:document.querySelectorAll('.copal-bases-workspace').length })`);
    assert.equal(result.title, 'To Watch'); assert.deepEqual(result.labels.slice(2), ['Status','Done','Due','Score']); assert.match(result.labels[0], /^Name/); assert.match(result.labels[1], /^Tags/); assert.equal(result.nested, 0);
  });
  await check('ResourceKey mismatch fails closed without a save', async () => {
    const before = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.writes.length)');
    await evaluate('fetch("/api/test-row-identity",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:true})})');
    try {
      await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-search');input.value='identity';input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
      await waitFor('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.some((item)=>item.query === "identity"))', 'mismatched ResourceKey query');
      await waitFor(`(()=>{const surface=document.querySelector('.copal-sheet-surface');const row=document.querySelector('.copal-sheet-grid tbody tr');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && row?.dataset.sheetRowKey?.includes('unknown-source') && document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]');})()`, 'mismatched ResourceKey row applied');
      const mismatchQueryBeforeDelete = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.length)');
      await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]');cell?.focus();cell?.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));})()`);
      await waitFor(`fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.length > ${mismatchQueryBeforeDelete} && value.writes.length === ${before} && (()=>{const surface=document.querySelector('.copal-sheet-surface');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]');})())`, 'mismatched ResourceKey clear rejection and requery');
      assert.equal(await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.writes.length)'), before, 'unknown ResourceKey must not queue or save by document ID');
    } finally {
      await evaluate('fetch("/api/test-row-identity",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:false})})');
      await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-search');if (input) { input.value=''; input.dispatchEvent(new Event('input',{bubbles:true})); }})()`);
      await waitFor(`(()=>{const surface=document.querySelector('.copal-sheet-surface');const row=document.querySelector('.copal-sheet-grid tbody tr');const state=surface?.querySelector('.copal-sheet-state')?.textContent || '';return row?.dataset.sheetRowKey?.includes('source-0') && surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && state === 'Ready' && !document.querySelector('.copal-sheet-paste-preview[open]');})()`, 'restored ResourceKey row and idle grid');
    }
  });
  await check('keyboard navigation skips hidden columns and exits at boundaries', async () => {
    await ensureTable();
    const first = await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]')?.focus(); ({ col:document.activeElement?.dataset.sheetColumnIndex })`); assert.equal(first.col, '0');
    await evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));`);
    const middle = await evaluate(`({ col:document.activeElement?.dataset.sheetColumnIndex, selected:[...document.querySelectorAll('.copal-sheet-cell[aria-selected="true"]')].map((node)=>node.dataset.sheetColumnIndex) })`); assert.notEqual(middle.col, '3'); assert.deepEqual(middle.selected, ['2']);
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]')?.focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));`);
    assert.equal(await evaluate('document.activeElement?.dataset?.sheetColumnIndex'), '4');
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="6"]')?.focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));`);
    assert.equal(await evaluate('document.activeElement?.dataset?.sheetRowIndex'), '1'); assert.equal(await evaluate('document.activeElement?.dataset?.sheetColumnIndex'), '0');
  });
  await check('full grid keyboard and range rows', async () => {
    await ensureTable();
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="1"][data-sheet-column-index="0"]')?.focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',shiftKey:true,bubbles:true}));document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Home',shiftKey:true,bubbles:true}));document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));`);
    assert.ok(await evaluate("document.querySelectorAll('.copal-sheet-cell[aria-selected=true]').length >= 1"));
    await evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));`);
  });
  await check('F2, Shift+Enter, Cmd-A, copy and Delete clear', async () => {
    await ensureTable();
    const status = 'document.querySelector(\'.copal-sheet-cell[data-sheet-row-index="2"][data-sheet-column-key="status"]\')';
    const assertEditor = async (label) => {
      const diagnostics = await evaluate(`(()=>{const cell=${status};const active=document.activeElement;const focusCell=document.querySelector('.copal-sheet-cell[tabindex="0"]');return {
        editorCount:document.querySelectorAll('.copal-sheet-editor').length,
        cellConnected:Boolean(cell?.isConnected),
        activeTag:active?.tagName || '',
        activeClasses:active?.className || '',
        selected:[...document.querySelectorAll('.copal-sheet-cell[aria-selected="true"]')].slice(0,4).map((node)=>({ row:node.dataset.sheetRowIndex, column:node.dataset.sheetColumnKey })),
        focus:{ row:focusCell?.dataset.sheetRowIndex || '', column:focusCell?.dataset.sheetColumnKey || '' },
        targetOuterHTML:cell?.outerHTML?.slice(0,800) || '',
      };})()`);
      assert.equal(diagnostics.editorCount, 1, `${label}: ${JSON.stringify(diagnostics)}`);
    };
    await evaluate(`(()=>{const cell=${status};cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'F2',bubbles:true}));})()`);
    await assertEditor('F2 editor');
    await evaluate('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await evaluate(`(()=>{const cell=${status};cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true}));})()`);
    await assertEditor('Escape then Shift+Enter editor');
    await evaluate('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    await evaluate(`(()=>{const cell=${status};cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'a',metaKey:true,bubbles:true}));})()`);
    const selected = await evaluate("document.querySelectorAll('.copal-sheet-cell[aria-selected=true]').length");
    assert.equal(selected, 600, `Cmd-A must select every visible cell (100 rows × 6 columns); actual=${selected}`);
    await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="3"][data-sheet-column-key="status"]');cell.focus();})()`);
    const contextBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.writes.length)');
    const contextQueryBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.length)');
    await evaluate(`(async()=>{const sheet=document.querySelector('.copal-sheet-surface');return await sheet.__openClankSheetContextCommand('clear-sheet-cell',document.activeElement);})()`);
    await waitFor(`fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.writes.length > ${contextBefore} && value.writes.some((item)=>item.id === "source-3") && value.queries.length > ${contextQueryBefore} && (()=>{const surface=document.querySelector('.copal-sheet-surface');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && document.querySelector('.copal-sheet-cell[data-sheet-row-index="3"][data-sheet-column-key="status"]');})())`, 'context-menu clear receipt and requery');
    await evaluate(`(()=>{const cell=${status};cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'c',metaKey:true,bubbles:true}));})()`);
    const beforeWrites = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length)');
    const beforeQueries = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.queries.length)');
    await evaluate(`(()=>{const cell=${status};cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'Delete',bubbles:true}));})()`);
    await waitFor(`fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length > ${beforeWrites} && state.writes.some((item)=>item.id === "source-2") && state.queries.length > ${beforeQueries} && (()=>{const surface=document.querySelector('.copal-sheet-surface');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && document.querySelector('.copal-sheet-cell[data-sheet-row-index="2"][data-sheet-column-key="status"]');})())`, 'single-cell clear receipt and requery');
    const writes = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
    assert.equal(writes.writes.filter((item) => item.id === 'source-2').length, 1, 'clear must submit one known writable row');
  });
  await check('read-only source and invalid number/date validation', async () => {
    await ensureTable();
    const readOnlyDiagnostics = await evaluate(`(async()=>{
      await new Promise((resolve)=>requestAnimationFrame(()=>resolve()));
      const surfaces=[...document.querySelectorAll('.copal-sheet-surface')];
      const surface=surfaces.find((node)=>node.matches('[data-sheet-status=ready]') && node.getAttribute('aria-busy') !== 'true') || null;
      const node=surface?.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="file.name"]') || null;
      const state=await fetch('/api/test-state').then((response)=>response.json());
      return {
        surfaceCount:surfaces.length,
        surfaces:surfaces.map((item)=>({ status:item.getAttribute('data-sheet-status'), busy:item.getAttribute('aria-busy') })),
        targetExists:Boolean(node),
        rowColumnKeys:surface ? [...surface.querySelectorAll('.copal-sheet-cell[data-sheet-row-index="0"]')].map((item)=>item.getAttribute('data-sheet-column-key')) : [],
        readonly:node?.getAttribute('aria-readonly') ?? null,
        title:node?.title || '',
        outerHTML:node?.outerHTML?.slice(0,800) || '',
        selectedView:surface?.querySelector('.copal-sheet-tab[aria-selected="true"]')?.textContent?.trim() || '',
        queries:state.queries.length,
        writes:state.writes.length,
      };
    })()`);
    assert.equal(readOnlyDiagnostics.surfaceCount, 1, `read-only gate requires exactly one mounted surface: ${JSON.stringify(readOnlyDiagnostics)}`);
    assert.deepEqual(readOnlyDiagnostics.surfaces, [{ status:'ready', busy:readOnlyDiagnostics.surfaces[0]?.busy }], `read-only gate surface state: ${JSON.stringify(readOnlyDiagnostics)}`);
    assert.notEqual(readOnlyDiagnostics.surfaces[0]?.busy, 'true', `read-only gate found a busy surface: ${JSON.stringify(readOnlyDiagnostics)}`);
    assert.equal(readOnlyDiagnostics.targetExists, true, `semantic file.name cell missing: ${JSON.stringify(readOnlyDiagnostics)}`);
    assert.equal(readOnlyDiagnostics.readonly, 'true', `file.name must be read-only: ${JSON.stringify(readOnlyDiagnostics)}`);
    assert.match(readOnlyDiagnostics.title, /Open source/, JSON.stringify(readOnlyDiagnostics));
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="file.name"]')?.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
    try {
      await waitFor('document.querySelector(".copal-note-editor, .copal-source-editor, .cm-editor")', 'read-only source opens');
    } catch (error) {
      const sourceDiagnostics = await evaluate(`(async()=>{
        const surfaces=[...document.querySelectorAll('.copal-sheet-surface')];
        const surface=surfaces.find((node)=>node.matches('[data-sheet-status=ready]') && node.getAttribute('aria-busy') !== 'true') || surfaces[0] || null;
        const row=surface?.querySelector('.copal-sheet-grid tbody tr[data-sheet-row-index="0"]') || null;
        const safeKey=(value)=>{try{const key=JSON.parse(value || '{}');return {provider:String(key.provider || ''),resourceId:String(key.resourceId || '')};}catch(_){return null;}};
        const state=await fetch('/api/test-state').then((response)=>response.json());
        return {
          rowResourceKey:safeKey(row?.getAttribute('data-sheet-row-key')),
          availableDocumentIds:[...document.querySelectorAll('.copal-file-row[data-document-id]')].map((node)=>node.getAttribute('data-document-id')).filter(Boolean).slice(0,100),
          availableResourceKeys:[...surface?.querySelectorAll('.copal-sheet-grid tbody tr[data-sheet-row-key]') || []].slice(0,10).map((node)=>safeKey(node.getAttribute('data-sheet-row-key'))),
          activeEditorTab:document.querySelector('.copal-note-tab.active,[role="tab"].active')?.textContent?.trim() || '',
          activeSheetTab:surface?.querySelector('.copal-sheet-tab[aria-selected="true"]')?.textContent?.trim() || '',
          activeElement:{tag:document.activeElement?.tagName || '',className:document.activeElement?.className || ''},
          sheet:{surfaceCount:surfaces.length,status:surface?.getAttribute('data-sheet-status') || null,busy:surface?.getAttribute('aria-busy') || null,rows:surface?.querySelectorAll('.copal-sheet-grid tbody tr').length || 0,cells:surface?.querySelectorAll('.copal-sheet-cell').length || 0,keys:[...row?.querySelectorAll('.copal-sheet-cell') || []].map((node)=>node.getAttribute('data-sheet-column-key'))},
          controller:{contextCommand:typeof surface?.__openClankSheetContextCommand,stateText:surface?.querySelector('.copal-sheet-state')?.textContent?.trim() || ''},
          queries:state.queries.length,writes:state.writes.length,
        };
      })()`);
      throw new Error(`${error.message}; source-open diagnostics=${JSON.stringify(sourceDiagnostics)}`, { cause:error });
    }
    await evaluate('window.__copal.open("bases")'); await ensureTable('return to sheet');
    for (const [columnKey, value, label] of [['score','not-a-number','number'], ['due','not-a-date','date']]) {
      await waitFor(`document.querySelector('.copal-sheet-surface[data-sheet-status=ready]:not([aria-busy="true"]) .copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="${columnKey}"]')`, `${label} cell mounted`);
      await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="${columnKey}"]');cell.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));})()`);
      await waitFor('document.querySelector(".copal-sheet-editor")', `${label} editor`);
      const invalidWritesBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length)');
      await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');input.value=${JSON.stringify(value)};input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
      const invalidDiagnostics = await evaluate(`(async()=>{const input=document.querySelector('.copal-sheet-editor');const state=await fetch('/api/test-state').then((response)=>response.json());const valueAsDate=input?.valueAsDate;return {
        label:${JSON.stringify(label)},
        columnKey:${JSON.stringify(columnKey)},
        inputValue:input?.value ?? null,
        inputType:input?.type ?? null,
        ariaInvalid:input?.getAttribute('aria-invalid') ?? null,
        validity:input ? { badInput:Boolean(input.validity?.badInput), valid:Boolean(input.validity?.valid), typeMismatch:Boolean(input.validity?.typeMismatch), rangeUnderflow:Boolean(input.validity?.rangeUnderflow), rangeOverflow:Boolean(input.validity?.rangeOverflow) } : null,
        valueAsNumber:input && Number.isNaN(input.valueAsNumber) ? 'NaN' : input?.valueAsNumber ?? null,
        valueAsDate:valueAsDate ? valueAsDate.toISOString() : null,
        writes:state.writes.length,
        writesSinceEdit:state.writes.length - ${invalidWritesBefore},
      };})()`);
      assert.equal(invalidDiagnostics.ariaInvalid, 'true', `${label} invalid input: ${JSON.stringify(invalidDiagnostics)}`);
      await evaluate('document.activeElement.dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
    }
  });
  await check('mixed-success paste preview and receipt path', async () => {
    await ensureTable();
    const cell = await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]')`); assert.ok(cell);
    await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]');cell.focus();const event=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(event,'clipboardData',{value:{getData:()=> ${JSON.stringify('review\t9\nreview2\t10')}}});cell.dispatchEvent(event);})()`);
    await waitFor('document.querySelector(".copal-sheet-paste-preview[open]")', 'paste preview');
    const summary = await evaluate('document.querySelector(".copal-sheet-paste-summary")?.textContent || ""'); assert.match(summary, /4 cells ready/);
    const pasteWritesBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length)');
    const pasteQueriesBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.queries.length)');
    await evaluate('document.querySelector(".copal-sheet-paste-preview button.primary")?.click()');
    await waitFor(`fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length > ${pasteWritesBefore} && state.writes.some((item)=>item.payload.actionId) && state.queries.length > ${pasteQueriesBefore} && (()=>{const surface=document.querySelector('.copal-sheet-surface');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && document.querySelector('.copal-sheet-cell[data-sheet-row-index="1"][data-sheet-column-key="status"]');})())`, 'paste apply receipts and requery');
    const writes = await evaluate('fetch("/api/test-state").then((response)=>response.json())'); assert.ok(writes.writes.length >= 1); assert.ok(writes.writes.some((item)=>item.payload.actionId));
    assert.equal(writes.writes.filter((item) => item.id === 'source-1').length, 1);
    assert.equal(writes.source0.properties.status, 'unwatched'); assert.equal(writes.source1.properties.status, 'review2');
    const dialogsAfterConflict = await evaluate(`(()=>({
      pasteDialogs:document.querySelectorAll('dialog.copal-sheet-paste-preview[open]').length,
      conflictDialogs:[...document.querySelectorAll('dialog[open]')].filter((dialog)=>dialog.matches('.copal-conflict-dialog') || dialog.querySelector('h2')?.textContent?.startsWith('Resolve conflict')).length,
      reviewLabel:[...document.querySelectorAll('dialog.copal-sheet-paste-preview[open] button')].find((button)=>button.textContent.includes('Review conflict'))?.textContent || '',
      applyLabel:[...document.querySelectorAll('dialog.copal-sheet-paste-preview[open] button')].find((button)=>button.textContent === 'Close')?.textContent || '',
      detail:document.querySelector('dialog.copal-sheet-paste-preview[open] .copal-sheet-paste-rejections')?.textContent || '',
      status:document.querySelector('dialog.copal-sheet-paste-preview[open] .copal-sheet-paste-status')?.textContent || ''
    }))()`);
    assert.equal(dialogsAfterConflict.pasteDialogs, 1, 'the failed paste remains in exactly one paste-preview dialog');
    assert.equal(dialogsAfterConflict.conflictDialogs, 0, 'a sheet paste conflict must not open the global Resolve conflict dialog');
    assert.equal(dialogsAfterConflict.reviewLabel, 'Review conflict');
    assert.equal(dialogsAfterConflict.applyLabel, 'Close');
    assert.match(dialogsAfterConflict.detail, /intended/);
    assert.match(dialogsAfterConflict.detail, /latest/);
    const reviewBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
    await evaluate('[...document.querySelectorAll("dialog.copal-sheet-paste-preview button")].find((node)=>node.textContent.includes("Review conflict"))?.click()');
    const reviewedConflict = await evaluate(`(()=>({
      reviewLabel:[...document.querySelectorAll('dialog.copal-sheet-paste-preview[open] button')].find((button)=>button.textContent.includes('Retry with latest'))?.textContent || '',
      detail:document.querySelector('dialog.copal-sheet-paste-preview[open] .copal-sheet-paste-rejections')?.textContent || '',
      status:document.querySelector('dialog.copal-sheet-paste-preview[open] .copal-sheet-paste-status')?.textContent || ''
    }))()`);
    assert.equal(reviewedConflict.reviewLabel, 'Retry with latest');
    assert.match(reviewedConflict.detail, /intended/);
    assert.match(reviewedConflict.detail, /latest/);
    assert.match(reviewedConflict.status, /Retry with latest/);
    const reviewAfter = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
    assert.equal(reviewAfter.writes.length, reviewBefore.writes.length, 'reviewing a conflict must not resubmit any row');
    assert.equal(reviewAfter.queries.length, reviewBefore.queries.length, 'reviewing a conflict must not requery');
    const retryStateBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
    const retryWritesBefore = retryStateBefore.writes.length;
    const retrySource0WritesBefore = retryStateBefore.writes.filter((item)=>item.id === 'source-0').length;
    const retryQueriesBefore = retryStateBefore.queries.length;
    await evaluate('[...document.querySelectorAll("dialog.copal-sheet-paste-preview button")].find((node)=>node.textContent.includes("Retry with latest"))?.click()');
    await waitFor(`fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.writes.length > ${retryWritesBefore} && state.writes.some((item)=>item.id === "source-0") && state.queries.length > ${retryQueriesBefore} && (()=>{const surface=document.querySelector('.copal-sheet-surface');return surface?.getAttribute('data-sheet-status') === 'ready' && surface?.getAttribute('aria-busy') !== 'true' && document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="status"]') && !document.querySelector('dialog.copal-sheet-paste-preview[open]') && !document.querySelector('dialog.copal-conflict-dialog[open]');})())`, 'paste retry receipt, requery, and dialog cleanup');
    const retried = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
    assert.equal(retried.writes.length - retryWritesBefore, 1, 'retry writes exactly one failed source row');
    assert.equal(retried.writes.filter((item) => item.id === 'source-0').length, retrySource0WritesBefore + 1);
    assert.equal(retried.queries.length - retryQueriesBefore, 1, 'retry triggers exactly one sheet requery');
    assert.equal(retried.writes.filter((item) => item.id === 'source-1').length, 1); assert.equal(retried.source0.properties.status, 'review');
    const dialogsAfterRetry = await evaluate(`(()=>({
      pasteDialogs:document.querySelectorAll('dialog.copal-sheet-paste-preview[open]').length,
      conflictDialogs:[...document.querySelectorAll('dialog[open]')].filter((dialog)=>dialog.matches('.copal-conflict-dialog') || dialog.querySelector('h2')?.textContent?.startsWith('Resolve conflict')).length
    }))()`);
    assert.deepEqual(dialogsAfterRetry, { pasteDialogs:0, conflictDialogs:0 });
  });
  await check('delayed out-of-order query applies newest search', async () => {
    await evaluate('fetch("/api/test-delay",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:true})})');
    await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-search');input.value='slow';input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await waitFor('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.queries.some((item)=>item.query === "slow"))', 'slow query in flight');
    await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-search');input.value='fast';input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
    await new Promise((resolve) => setTimeout(resolve, 500));
    assert.equal(await evaluate('document.querySelector(".copal-sheet-search")?.value'), 'fast');
    const afterFastText = await evaluate('document.querySelector(".copal-sheet-grid")?.textContent || ""');
    const queryState = await evaluate('fetch("/api/test-state").then((response)=>response.json())'); assert.equal(queryState.queries.at(-1).query, 'fast'); assert.match(afterFastText, /Row-0001/); assert.doesNotMatch(afterFastText, /Row-0000/, 'superseded slow rows must not render while fast is current');
    await new Promise((resolve) => setTimeout(resolve, 260));
    const afterSlowSettledText = await evaluate('document.querySelector(".copal-sheet-grid")?.textContent || ""');
    assert.doesNotMatch(afterSlowSettledText, /Row-0000/, 'late slow response must never restore stale rows');
    metrics.supersededQueries = { slowStarted:queryState.queries.some((item) => item.query === 'slow'), newest:queryState.queries.at(-1).query, staleSlowRowsWhileFast:afterFastText.includes('Row-0000'), staleSlowRowsAfterSettled:afterSlowSettledText.includes('Row-0000'), queries:queryState.queries.length };
    await evaluate('fetch("/api/test-delay",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({enabled:false})})');
  });
  await check('card/list views and raw/history entry points', async () => {
    await ensureTable();
    const tabs = await evaluate('[...document.querySelectorAll(".copal-sheet-tab")].map((node)=>node.textContent)'); assert.deepEqual(tabs, ['To Watch', 'List', 'Cards']);
    await evaluate('document.querySelectorAll(".copal-sheet-tab")[1].click()'); await waitFor('document.querySelector(".copal-sheet-lists")', 'list view');
    await evaluate('document.querySelectorAll(".copal-sheet-tab")[2].click()'); await waitFor('document.querySelector(".copal-sheet-cards")', 'card view');
    await evaluate('[...document.querySelectorAll(".copal-sheet-tool")].find((node)=>node.textContent === "More")?.click()');
    await waitFor('document.querySelector(".copal-sheet-overflow[open]")', 'sheet overflow');
    await evaluate('[...document.querySelectorAll(".copal-sheet-overflow button")].find((node)=>node.textContent.includes("History"))?.click()');
    await waitFor('document.querySelector("dialog[open]")', 'history dialog');
    const history = await evaluate('document.querySelector("dialog[open]")?.textContent || ""'); assert.match(history, /Initial/);
    await evaluate('document.querySelector("dialog[open]")?.close()');
    await evaluate('[...document.querySelectorAll(".copal-sheet-tool")].find((node)=>node.textContent === "More")?.click()');
    await waitFor('document.querySelector(".copal-sheet-overflow[open]")', 'raw overflow');
    await evaluate('[...document.querySelectorAll(".copal-sheet-overflow button")].find((node)=>node.textContent.includes("Open raw source"))?.click()');
    await waitFor('document.querySelector(".cm-editor, .copal-source-editor, textarea")', 'raw source entry point');
    await evaluate('window.__copal.open("bases")'); await ensureTable('sheet after raw source');
  });
  await check('two leaves share the Base resource with independent view and scroll', async () => {
    await ensureTable();
    const split = await evaluate(`document.querySelector('button[aria-label="Split right"]')`); assert.ok(split);
    await evaluate(`document.querySelector('button[aria-label="Split right"]')?.click()`);
    await waitFor('document.querySelector(".copal-quick-switcher[open]")', 'split chooser');
    await evaluate('[...document.querySelectorAll(".copal-quick-switcher[open] .copal-doc-row")].find((node)=>node.textContent.includes("To Watch.base"))?.click()');
    await waitFor('(()=>{const surfaces=[...document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]")];return surfaces.length === 2 && surfaces.every((surface)=>surface.querySelectorAll(".copal-sheet-grid tbody tr").length === 100);})()', 'two mounted leaves');
    const groups = await evaluate('document.querySelectorAll(".copal-note-group").length'); assert.equal(groups, 2);
    await evaluate(`(()=>{const groups=document.querySelectorAll('.copal-note-group');groups[0]?.classList.add('s04-scroll-leaf-0');groups[1]?.classList.add('s04-scroll-leaf-1');const geometry=document.createElement('style');geometry.dataset.s04ScrollGeometry='true';geometry.textContent='.s04-scroll-leaf-0 .copal-sheet-grid-wrap,.s04-scroll-leaf-1 .copal-sheet-grid-wrap{flex:0 0 50px!important;height:50px!important;min-height:50px!important;overflow:auto!important}';document.head.append(geometry);const wraps=document.querySelectorAll('.copal-sheet-grid-wrap');if (wraps.length !== 2 || wraps[0].scrollHeight <= wraps[0].clientHeight || wraps[1].scrollHeight <= wraps[1].clientHeight) throw new Error(JSON.stringify([...wraps].map((wrap)=>({scrollHeight:wrap.scrollHeight,clientHeight:wrap.clientHeight}))));wraps[0].scrollTop=37;wraps[1].scrollTop=83;})()`);
    const immediateScroll = await evaluate('([...document.querySelectorAll(".copal-sheet-grid-wrap")].map((node)=>node.scrollTop))'); assert.deepEqual(immediateScroll, [37, 83], 'two mounted leaves accept independent baseline viewport positions before view switching');
    await evaluate('(()=>{const tab=document.querySelectorAll(".copal-note-group")[1]?.querySelectorAll(".copal-sheet-tab")[1];tab?.focus();tab?.click();})()');
    await waitFor('document.querySelectorAll(".copal-note-group")[1]?.querySelector(".copal-sheet-lists")', 'second leaf list view');
    await evaluate('new Promise((resolve) => setTimeout(() => requestAnimationFrame(() => resolve(true)), 0))');
    const listFocus = await evaluate('(()=>{const active=document.activeElement;const group=document.querySelectorAll(".copal-note-group")[1];return Boolean(active && group?.contains(active) && active.classList.contains("copal-sheet-tab") && active.textContent === "List" && !document.querySelectorAll(".copal-note-group")[0]?.contains(active));})()'); assert.equal(listFocus, true, 'view tab focus remains with the invoking leaf after replacement');
    await evaluate('(()=>{const tab=document.querySelectorAll(".copal-note-group")[1]?.querySelector(".copal-sheet-tab");tab?.focus();tab?.click();})()');
    await waitFor('document.querySelectorAll(".copal-note-group")[1]?.querySelector(".copal-sheet-grid")', 'second leaf table view');
    await new Promise((resolve) => setTimeout(resolve, 0)); await evaluate('new Promise((resolve) => requestAnimationFrame(() => resolve(true)))');
    await waitFor('(()=>{const surfaces=[...document.querySelectorAll(".copal-sheet-surface[data-sheet-status=ready]")];const wraps=[...document.querySelectorAll(".copal-sheet-grid-wrap")];return surfaces.length === 2 && surfaces.every((surface)=>surface.getAttribute("aria-busy") !== "true" && surface.querySelectorAll(".copal-sheet-grid tbody tr").length === 100) && wraps.length === 2 && wraps.every((wrap)=>wrap.scrollHeight > wrap.clientHeight);})()', 'two leaf repaint after table view');
    const scroll = await evaluate('([...document.querySelectorAll(".copal-sheet-grid-wrap")].map((node)=>node.scrollTop))'); assert.deepEqual(scroll, [37, 83]);
    const resources = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((state)=>state.queries.filter((item)=>item.view === "table").length)'); assert.ok(resources >= 2);
    const splitClose = await evaluate('(()=>{const groups=[...document.querySelectorAll(".copal-note-group")];const group=groups[1];return {groups:groups.length,leafIds:[...(group?.querySelectorAll(".copal-note-tab[data-leaf-id]") || [])].map((node)=>node.dataset.leafId),surfaceCount:group?.querySelectorAll(".copal-sheet-surface").length || 0};})()');
    assert.deepEqual(splitClose.groups, 2); assert.equal(splitClose.leafIds.length, 1, `split sibling must be a singleton close target: ${JSON.stringify(splitClose)}`); assert.equal(splitClose.surfaceCount, 1, `split sibling must own one sheet surface: ${JSON.stringify(splitClose)}`);
    await evaluate('document.querySelectorAll(".copal-note-group")[1]?.querySelector(".copal-note-tab-close")?.click()');
    await waitFor('document.querySelectorAll(".copal-sheet-surface").length === 1 && document.querySelectorAll(".copal-note-group").length === 1', 'close one leaf and prune empty split group');
    assert.equal(await evaluate('document.querySelectorAll(".copal-note-group").length'), 1);
  });
  await check('selection dispatch and long-task budget', async () => {
    await ensureTable();
    const result = await evaluate(`(()=>{const surface=document.querySelector('.copal-sheet-surface');const cell=surface?.querySelector('.copal-sheet-cell');const samples=[];for(let i=0;i<100;i++){const start=performance.now();cell?.focus();cell?.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));samples.push(performance.now()-start);}samples.sort((a,b)=>a-b);return {p95:samples[Math.floor(samples.length*.95)-1]||0, max:Math.max(...samples), cells:surface?.querySelectorAll('.copal-sheet-cell').length || 0};})()`);
    assert.ok(result.cells <= 700); assert.ok(result.p95 <= 8, JSON.stringify(result));
    const longTasks = await evaluate('window.__s04LongTasks || []'); assert.equal(longTasks.some((duration) => duration > 50), false, JSON.stringify(longTasks));
    metrics.selection = { p95Ms:result.p95, maxMs:result.max, cells:result.cells, longTasksOver50ms:longTasks.filter((duration) => duration > 50).length };
  });
  await check('absolute Tab boundaries exit the grid', async () => {
    await ensureTable();
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]')?.focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true}));`);
    const firstExited = await evaluate('document.activeElement?.dataset?.sheetRowIndex !== "0"');
    await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="99"][data-sheet-column-index="6"]')?.focus();document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));`);
    const lastExited = await evaluate('document.activeElement?.dataset?.sheetRowIndex !== "99"');
    assert.deepEqual({ firstExited, lastExited }, { firstExited:true, lastExited:true });
  });
  await check('20 open/close cycles release sheet surface', async () => {
    const before = await evaluate('(async()=>{const state=await fetch("/api/test-state").then((response)=>response.json());const live=window.__s04LiveSheetListeners.map((item)=>({type:item.type,capture:item.capture,owner:item.owner}));return {...window.__s04Listeners,queries:state.queries.length,live,byType:Object.fromEntries([...new Set(live.map((item)=>item.type))].map((type)=>[type,live.filter((item)=>item.type===type).length]))};})()');
    const initialActiveSheetListeners = before.adds - before.removes;
    const baseLeafId = await evaluate('document.querySelector(".copal-sheet-surface")?.closest(".copal-note-leaf")?.dataset.leafId || null');
    const baseListeners = before.live.filter((item) => item.owner.leafId === baseLeafId);
    const cachedEditorListeners = before.live.filter((item) => item.owner.leafId !== baseLeafId);
    assert.equal(initialActiveSheetListeners, 11, `mounted sheet listener owner count changed: ${JSON.stringify(before)}`);
    assert.deepEqual(baseListeners.map((item) => item.type).sort(), ['click','dblclick','focus','keydown','paste','pointercancel','pointerdown','pointermove','pointerup','scroll'], `Base mount must own exactly its 10 listeners: ${JSON.stringify({ baseLeafId, baseListeners })}`);
    assert.deepEqual(cachedEditorListeners.map((item) => ({ type:item.type, capture:item.capture, target:item.owner.target })), [{ type:'scroll', capture:false, target:'leaf-content' }], `only the cached source editor may add one passive content scroll owner: ${JSON.stringify({ baseLeafId, cachedEditorListeners })}`);
    for (let index = 0; index < 20; index += 1) {
      const cycleQueriesBefore = await evaluate('fetch("/api/test-state").then((response)=>response.json()).then((value)=>value.queries.length)');
      await evaluate('window.__copal.close("notes",false)');
      assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-surface").length'), 0, `cycle ${index + 1} close must remove the mounted sheet`);
      await evaluate('window.__copal.open("bases",false)');
      await ensureTable(`cycle ${index + 1}`, { openIfMissing:false, queryAfter:cycleQueriesBefore });
    }
    await evaluate('window.__copal.close("notes",false)'); assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-surface").length'), 0);
    const after = await evaluate('(async()=>{const state=await fetch("/api/test-state").then((response)=>response.json());const live=window.__s04LiveSheetListeners.map((item)=>({type:item.type,capture:item.capture,owner:item.owner}));return {...window.__s04Listeners,queries:state.queries.length,live,byType:Object.fromEntries([...new Set(live.map((item)=>item.type))].map((type)=>[type,live.filter((item)=>item.type===type).length]))};})()');
    assert.equal(after.adds - after.removes, 0, `sheet listeners remain after final close: ${JSON.stringify(after)}`);
    assert.equal((after.removes - before.removes) - (after.adds - before.adds), initialActiveSheetListeners, `listener removals must retire the initial mounted owner: ${JSON.stringify({ before, after })}`);
    assert.ok(after.queries - before.queries >= 20); assert.ok(after.queries - before.queries <= 40);
    metrics.lifecycle = { cycles:20, before:{adds:before.adds,removes:before.removes,live:before.live.length,queries:before.queries}, after:{adds:after.adds,removes:after.removes,live:after.live.length,queries:after.queries}, listenerAdds:after.adds-before.adds, listenerRemoves:after.removes-before.removes, liveDelta:after.live.length-before.live.length, queryDelta:after.queries-before.queries, finalActive:after.adds-after.removes };
  });
  await check('scope/revocation removes the old sheet', async () => {
    await evaluate('fetch("/api/test-account",{method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({account:"revoked-account"})})');
    await evaluate('window.__copal.init(location.origin)'); await evaluate('window.__copal.open("bases")');
    await waitFor('(()=>{const surface=document.querySelector(".copal-sheet-surface[data-sheet-status=ready]");return Boolean(surface?.querySelector(".copal-sheet-grid") && surface.querySelector(".copal-sheet-footer"));})()', 'scope replacement sheet');
    assert.equal(await evaluate('document.body.textContent.includes("Row-0000")'), true);
    assert.match(await evaluate('document.querySelector(".copal-sheet-footer")?.textContent || ""'), /1 matched/);
    assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-surface").length'), 1);
  });
  const final = await evaluate('fetch("/api/test-state").then((response)=>response.json())');
  assert.deepEqual(failures, [], `S04 full qualification failures:\n${failures.join('\n')}\nstate=${JSON.stringify(final)}`);
  console.log(JSON.stringify({ gate:'S04 full mounted qualification', sources:5001, metrics, final, failures:[] }));
});
