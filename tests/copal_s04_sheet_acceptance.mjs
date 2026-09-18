#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const definition = { version:1, extensions:{ title:'Projects/To Watch.base' }, views:[{ id:'table', name:'To Watch', type:'table', columns:[{ property:'file.name' }, { property:'file.tags' }, { property:'status' }, { property:'priority', type:'number' }, { property:'score', type:'number' }, { property:'due', type:'date' }, { property:'done', type:'boolean' }], sorts:[], filters:null, summaries:{}, limit:100 }] };
function resource(id, head, representation='nativeNote') { return { key:{ accountId:'sheet-account', workspaceId:'sheet-workspace', provider:'copal', resourceId:id }, revision:{ kind:'copalHead', value:head }, locator:{ displayName:id, locationLabel:id }, representation, capabilities:{ read:true, edit:true } }; }
const sources = [
  { id:'source-1', kind:'note', name:'Alpha.md', head:'source-head-1', text:'# Alpha\n', properties:{ status:'open', priority:1, score:10, due:'2026-09-13', done:false }, relations:[], tags:['work'], links:[] },
  { id:'source-2', kind:'note', name:'Beta.md', head:'source-head-2', text:'# Beta\n', properties:{ status:'done', priority:2, score:20, due:'2026-09-14', done:false }, relations:[], tags:['home'], links:[] },
  ...Array.from({ length:4999 }, (_, index) => ({ id:`source-${index + 3}`, kind:'note', name:`Generated ${index + 3}.md`, head:`source-head-${index + 3}`, text:`# Generated ${index + 3}\n`, properties:{ status:index % 2 ? 'open' : 'done', priority:index, score:index, due:'2026-09-13', done:false }, relations:[], tags:[], links:[] })),
];
for (const doc of sources) doc.resource = resource(doc.id, doc.head);
const base = { id:'base-1', kind:'base', name:'Projects/To Watch.base', head:'base-head-1', text:JSON.stringify(definition), properties:{}, relations:[], tags:[], links:[], resource:resource('base-1', 'base-head-1', 'base') };
const state = { writes:0, queries:0, commands:0, commandBodies:[], requests:[], events:null, source2Failures:0, deferSource1:false, source1DeferredResolve:null, source1Saved:false, deferSource1Refresh:false, source1RefreshResolve:null, source1RefreshReleased:false, hideDue:false };
const page = `<!doctype html><meta charset="utf-8"><body><nav><a href="/copal/bases" data-copal-view="bases">Bases</a></nav><main id="app"></main><script type="module">window.__run=async()=>{const module=await import('/static/js/copal.js?s04-sheet');await module.init(location.origin);window.__copal=module.default;};</script>`;
function json(res, value, status=200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }
async function body(req) { const chunks=[]; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; }
function source(id) { return sources.find((doc) => doc.id === id); }
function activeDefinition() {
  let current = definition;
  try { const parsed = JSON.parse(base.text); if (parsed && Array.isArray(parsed.views)) current = parsed; } catch (_) { /* retain the last valid fixture definition */ }
  if (base.text.includes('title: Projects/Raw Edited.base')) current = { ...current, extensions:{ ...current.extensions, title:'Projects/Raw Edited.base' } };
  return state.hideDue ? { ...current, views:current.views.map((view) => ({ ...view, columns:view.columns.map((column) => column.property === 'due' ? { ...column, visible:false } : column) })) } : current;
}

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') return json(res, { account_id:'sheet-account', storage_namespace:'sheet-fixture' });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.write(': fixture\n\n'); state.events=res; return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[base, ...sources] });
  const documentMatch = url.pathname.match(/^\/api\/copal\/documents\/([^/]+)$/);
  if (documentMatch && req.method === 'GET') return json(res, documentMatch[1] === base.id ? base : source(documentMatch[1]) || { detail:'missing' }, source(documentMatch[1]) || documentMatch[1] === base.id ? 200 : 404);
  if (documentMatch && req.method === 'PUT') {
    const target = documentMatch[1] === base.id ? base : source(documentMatch[1]); if (!target) return json(res, { detail:'missing' }, 404);
    const update = await body(req); state.requests.push({ id:target.id, actionId:update.actionId, base:update.base });
    if (update.base !== target.head) return json(res, { detail:{ outcome:'stale', doc:{ ...target } } }, 409);
    if (target.id === 'source-1' && state.deferSource1) await new Promise((resolve) => { state.source1DeferredResolve = resolve; });
    if (target.id === 'source-2' && state.source2Failures < 1) {
      state.source2Failures += 1; target.head = `${target.head}-remote`; target.text = '# remote\n';
      return json(res, { detail:{ outcome:'stale', doc:{ ...target } } }, 409);
    }
    state.writes += 1; if (target.id === 'source-1') state.source1Saved = true; target.head = `${target.head}-saved`; if (target.kind === 'base') target.text=update.content; else { target.text=update.content; target.properties={ ...(update.properties || target.properties) }; }
    return json(res, { outcome:'committed', actionId:update.actionId, doc:{ ...target } });
  }
  if (url.pathname === '/api/copal/bases/base-1/transform' && req.method === 'POST') {
    state.commandBodies.push(await body(req)); state.commands += 1;
    return json(res, { source:base.text, definition:activeDefinition(), revision:base.head, changed:false, diagnostics:[] });
  }
  if ((url.pathname === '/api/copal/bases/base-1/query' && req.method === 'GET') || (url.pathname === '/api/copal/bases/base-1/query/preview' && req.method === 'POST')) {
    state.queries += 1;
    const previewBody = req.method === 'POST' ? await body(req) : null;
    let queriedDefinition = activeDefinition();
    if (previewBody?.source) { try { const parsed = JSON.parse(previewBody.source); if (parsed && Array.isArray(parsed.views)) queriedDefinition = parsed; } catch (_) { /* production would report a parse diagnostic */ } }
    if (previewBody?.source?.includes('title: Projects/Raw Edited.base')) queriedDefinition = { ...activeDefinition(), extensions:{ ...activeDefinition().extensions, title:'Projects/Raw Edited.base' } };
    if (state.source1Saved && state.deferSource1Refresh && !state.source1RefreshResolve) await new Promise((resolve) => { state.source1RefreshResolve = resolve; });
    return json(res, { base, definition:queriedDefinition, definitionRevision:previewBody?.definitionRevision ?? undefined, view:queriedDefinition.views[0], rows:sources.slice(0, 100).map((doc) => ({ documentId:doc.id, head:doc.head, name:doc.name, resourceKey:doc.resource.key, values:{ 'file.name':doc.name, 'file.tags':doc.tags, status:doc.properties.status, priority:doc.properties.priority, score:doc.properties.score, due:doc.properties.due, done:doc.properties.done }, errors:[] })), groups:[], summaries:{}, page:1, pageSize:100, pages:51, total:sources.length, matchedCount:sources.length, sourceCount:sources.length, resultLimited:true, sourceTruncated:false, queryComplete:true });
  }
  if (url.pathname === '/api/test-defer-source-1' && req.method === 'POST') { state.deferSource1 = Boolean((await body(req)).enabled); return json(res, { enabled:state.deferSource1 }); }
  if (url.pathname === '/api/test-release-source-1' && req.method === 'POST') { state.deferSource1Refresh = true; state.deferSource1 = false; state.source1DeferredResolve?.(); state.source1DeferredResolve = null; return json(res, { released:true }); }
  if (url.pathname === '/api/test-release-source-1-refresh' && req.method === 'POST') { state.deferSource1Refresh = false; state.source1RefreshReleased = true; state.source1RefreshResolve?.(); state.source1RefreshResolve = null; return json(res, { released:true }); }
  if (url.pathname === '/api/test-hide-due' && req.method === 'POST') { state.hideDue = true; return json(res, { hideDue:true }); }
  if (url.pathname === '/api/test-state') return json(res, { writes:state.writes, queries:state.queries, commands:state.commands, commandBodies:state.commandBodies, requests:state.requests, source2Failures:state.source2Failures, source1RefreshReleased:state.source1RefreshReleased });
  return false;
}}, async ({ evaluate, until, cdp }) => {
  await until('document.readyState === "complete"'); await evaluate('window.__run()');
  await evaluate(`(()=>{const add=EventTarget.prototype.addEventListener;const remove=EventTarget.prototype.removeEventListener;const isSheetTarget=(target)=>target?.classList?.contains('copal-note-leaf-content')||target?.classList?.contains('copal-sheet-surface');window.__rawSheetProbe={active:0};EventTarget.prototype.addEventListener=function(type,listener,options){if(window.__rawSheetProbe&&isSheetTarget(this))window.__rawSheetProbe.active+=1;return add.call(this,type,listener,options)};EventTarget.prototype.removeEventListener=function(type,listener,options){if(window.__rawSheetProbe&&isSheetTarget(this))window.__rawSheetProbe.active=Math.max(0,window.__rawSheetProbe.active-1);return remove.call(this,type,listener,options)};})()`);
  await evaluate('window.__copal.open("bases")');
  await until('document.querySelector(".copal-sheet-surface .copal-sheet-title h2")?.textContent === "To Watch" && document.querySelectorAll(".copal-sheet-grid th").length === 7');
  const initial = await evaluate(`({ sheets:document.querySelectorAll('.copal-sheet-surface').length, nested:document.querySelectorAll('.copal-bases-workspace').length, title:document.querySelector('.copal-sheet-title h2')?.textContent, labels:[...document.querySelectorAll('.copal-sheet-grid th')].map(node=>node.textContent), tabs:document.querySelectorAll('.copal-sheet-tab').length, firstTabIndex:document.querySelector('.copal-sheet-cell')?.tabIndex, selectedTabs:[...document.querySelectorAll('.copal-sheet-cell[tabindex="0"]')].length })`);
  assert.deepEqual(initial, { sheets:1, nested:0, title:'To Watch', labels:['Name','Tags','Status','Priority','Score','Due','Done'], tabs:1, firstTabIndex:0, selectedTabs:1 });
  assert.equal(await evaluate('document.querySelector(".copal-sheet-cell[data-sheet-column-key=\\"file.name\\"]")?.getAttribute("aria-readonly")'), 'true', 'file.name is a derived read-only source column');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-resource")'), null);
  assert.deepEqual(await evaluate(`[...document.querySelectorAll('.copal-sheet-tool')].map(node=>node.textContent)`), ['Filter','Sort','Group','Columns','More']);
  assert.equal(await evaluate('document.querySelector("[data-sheet-action=search]")'), null);
  const clipboardDenied = await evaluate(`(async()=>{const prior=navigator.clipboard;Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:()=>Promise.reject(new DOMException('denied','NotAllowedError'))}});const cell=document.querySelector('.copal-sheet-cell');const result=await document.querySelector('.copal-sheet-surface').__openClankSheetContextCommand('copy-sheet-cell',cell);Object.defineProperty(navigator,'clipboard',{configurable:true,value:prior});return result;})()`);
  assert.equal(clipboardDenied, false, 'clipboard denial is reported without an unhandled command');
  await evaluate(`(()=>{localStorage.setItem('odysseus-custom-context-menu','off');window.dispatchEvent(new Event('odysseus-context-menu-changed'));const cell=document.querySelector('.copal-sheet-cell');cell.dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));})()`);
  assert.equal(await evaluate('document.getElementById("openclank-context-menu")'), null, 'disabled context menu does not intercept the sheet');
  await evaluate(`localStorage.removeItem('odysseus-custom-context-menu');window.dispatchEvent(new Event('odysseus-context-menu-changed'))`);
  const watchBefore = await evaluate(`(async()=>{const selector='.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]';document.querySelector(selector).focus();await new Promise((resolve)=>setTimeout(resolve,0));const wrap=document.querySelector('.copal-sheet-grid-wrap');wrap.scrollTop=17;return { selected:document.querySelector(selector)?.getAttribute('aria-selected'), scroll:wrap.scrollTop };})()`);
  state.events?.write('event: document\ndata: {"id":"source-1"}\n\n');
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.queries >= 2)');
  const watchAfter = await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]');return { selected:cell.getAttribute('aria-selected'), scroll:document.querySelector('.copal-sheet-grid-wrap')?.scrollTop };})()`);
  assert.deepEqual(watchAfter, watchBefore, 'SSE refresh preserves selection and grid scroll');
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]').click()`);
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))`);
  await until('document.querySelector(".copal-sheet-editor")');
  await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');input.value='review';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes >= 1)');
  await until("document.querySelector('.copal-sheet-cell[data-sheet-row-index=\"0\"][data-sheet-column-index=\"2\"]')?.textContent.includes('review')");
  const afterEdit = await evaluate(`(async()=>({value:document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]')?.textContent, writes:(await (await fetch('/api/test-state')).json()).writes}))()`);
  assert.equal(afterEdit.writes >= 1, true); assert.match(afterEdit.value, /review/);
  const scoreCell = '.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="score"]';
  await evaluate(`document.querySelector(${JSON.stringify(scoreCell)}).dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  await until('document.querySelector(".copal-sheet-editor")');
  const writesBeforeInvalidScore = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)');
  const invalidScore = await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');input.value='not-a-number';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return { invalid:input.getAttribute('aria-invalid'), value:input.value };})()`);
  assert.deepEqual(invalidScore, { invalid:'true', value:'not-a-number' }, 'invalid score remains open and marked invalid');
  assert.equal(await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)'), writesBeforeInvalidScore, 'invalid score does not write');
  assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-editor").length'), 1, 'invalid score keeps its editor open');
  await evaluate('document.querySelector(".copal-sheet-editor").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
  const dueCell = '.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="due"]';
  const scoreEscaped = await evaluate(`(()=>{const sheet=document.querySelector('.copal-sheet-surface');const due=document.querySelector(${JSON.stringify(dueCell)});return { editors:document.querySelectorAll('.copal-sheet-editor').length, sheetConnected:Boolean(sheet?.isConnected), dueConnected:Boolean(due?.isConnected), dueVisible:Boolean(due&&!due.hidden&&due.closest('.copal-sheet-surface')) };})()`);
  assert.deepEqual(scoreEscaped, { editors:0, sheetConnected:true, dueConnected:true, dueVisible:true }, 'Escape cancels invalid score without closing the sheet or losing Due');
  await evaluate(`document.querySelector(${JSON.stringify(dueCell)}).dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  await until('document.querySelector(".copal-sheet-editor")');
  const writesBeforeInvalidDue = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)');
  const invalidDue = await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');input.value='not-a-date';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));return { invalid:input.getAttribute('aria-invalid'), value:input.value };})()`);
  assert.deepEqual(invalidDue, { invalid:'true', value:'not-a-date' }, 'invalid due date remains open and marked invalid');
  assert.equal(await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)'), writesBeforeInvalidDue, 'invalid due date does not write');
  assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-editor").length'), 1, 'invalid due date keeps its editor open');
  await evaluate('document.querySelector(".copal-sheet-editor").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
  await evaluate(`document.querySelector(${JSON.stringify(scoreCell)}).dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  await until('document.querySelector(".copal-sheet-editor")');
  const writesBeforeScoreClear = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)');
  await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');input.value='';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
  await until(`fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes > ${writesBeforeScoreClear})`);
  await until('document.querySelector(".copal-sheet-editor") === null');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-editor")'), null, 'intentional score clear closes after a successful write');
  assert.equal((await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)')), writesBeforeScoreClear + 1, 'intentional score clear writes once');
  for (const action of ['filter','sort','group','columns']) {
    await evaluate(`document.querySelector('[data-sheet-action="${action}"]').click()`);
    await until('document.querySelector(".copal-sheet-toolbar-menu[open]")');
    await evaluate('document.querySelector(".copal-sheet-toolbar-menu button.primary")?.click()');
  }
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.commands >= 4)');
  await evaluate(`document.querySelector('.copal-sheet-column-menu').click()`);
  await until('document.querySelector(".copal-sheet-column-menu-dialog[open]")');
  assert.deepEqual(await evaluate(`[...document.querySelector('.copal-sheet-column-menu-dialog').querySelectorAll('button')].map(node=>node.textContent)`), ['Sort','Add sort priority','Move sort priority up','Move sort priority down','Move column left','Move column right','Hide column','Group by this','Count summary','Rename label']);
  await evaluate('document.querySelector(".copal-sheet-column-menu-dialog").close()');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-grid th")?.draggable'), true);
  await evaluate('document.querySelector(".copal-sheet-column-resize")?.dispatchEvent(new KeyboardEvent("keydown",{key:"ArrowRight",bubbles:true}))');
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.commands >= 5)');
  assert.equal(await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.commandBodies.some(item => item.command?.action === "resize-column"))'), true);
  await evaluate(`document.querySelector('.copal-sheet-tab').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}))`);
  await until('document.querySelector(".copal-sheet-view-menu[open]")');
  assert.deepEqual(await evaluate(`[...document.querySelector('.copal-sheet-view-menu').querySelectorAll('button')].map(node=>node.textContent)`), ['Duplicate view','Move left','Move right','Delete view']);
  await evaluate('document.querySelector(".copal-sheet-view-menu").close()');
  await evaluate('[...document.querySelectorAll("dialog")].forEach((dialog) => { if (dialog.open) dialog.close(); })');
  await new Promise((resolve) => setTimeout(resolve, 50));
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="2"]');cell.focus();const event=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(event,'clipboardData',{value:{getData:()=>'review\t3\\nblocked\\t4'}});cell.dispatchEvent(event);})()`);
  await until('document.querySelector(".copal-sheet-paste-preview[open]")');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-paste-summary")?.textContent'), '4 cells ready');
  await evaluate('document.querySelector(".copal-sheet-paste-preview button.primary").click()');
  await until('document.querySelector(".copal-sheet-paste-status")?.textContent.includes("conflict")');
  assert.equal(await evaluate('document.querySelector(".copal-conflict-dialog[open]")'), null, 'sheet conflicts stay inside the paste retry surface');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-paste-preview button").textContent'), 'Cancel');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-paste-preview button.primary")?.textContent'), 'Close');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-paste-preview button:not(.primary):not(:first-child)")?.textContent'), 'Review conflict');
  assert.match(await evaluate('document.querySelector(".copal-sheet-paste-rejections")?.textContent || ""'), /intended.*latest/);
  const conflictBeforeRetry = await evaluate('fetch("/api/test-state").then(r=>r.json())');
  const source2InitialRequest = conflictBeforeRetry.requests.find((item) => item.id === 'source-2');
  assert.equal(source2InitialRequest.base, 'source-head-2');
  await evaluate('document.querySelector(".copal-sheet-paste-preview button:not(.primary):not(:first-child)").click()');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-paste-preview button:not(.primary):not(:first-child)")?.textContent'), 'Retry with latest');
  await evaluate('document.querySelector(".copal-sheet-paste-preview button:not(.primary):not(:first-child)").click()');
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes >= 2)');
  await until('document.querySelector(".copal-sheet-paste-preview") === null');
  await until('document.querySelector(".copal-sheet-cell[data-sheet-row-index=\\"0\\"][data-sheet-column-index=\\"2\\"]")?.textContent.includes("review")');
  const conflictAfterRetry = await evaluate('fetch("/api/test-state").then(r=>r.json())');
  const source2Requests = conflictAfterRetry.requests.filter((item) => item.id === 'source-2');
  assert.equal(source2Requests.length, 2, 'conflict retry submits only the failed source row');
  assert.equal(source2Requests[1].base, 'source-head-2-remote', 'explicit rebase uses the authoritative remote head');
  assert.notEqual(source2Requests[1].actionId, source2Requests[0].actionId, 'explicit rebase creates a fresh action identity');
  const sourceTabBefore = await evaluate('document.querySelectorAll(".copal-note-tab").length');
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="file.name"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  await until('document.querySelector(".copal-note-tab.active .copal-note-tab-label")?.title === "Alpha.md" && document.querySelector(".copal-note-leaf[data-view-type=note] .cm-editor")', 'authoritative file.name opens source after source-2 refresh');
  const sourceOpen = await evaluate(`({
    tabs:document.querySelectorAll('.copal-note-tab').length,
    sourceTab:document.querySelector('.copal-note-tab.active .copal-note-tab-label')?.title || '',
    sourceLeaf:document.querySelector('.copal-note-leaf[data-view-type="note"]')?.dataset?.leafId || '',
    editor:Boolean(document.querySelector('.copal-note-leaf[data-view-type="note"] .cm-editor')),
    sheet:Boolean(document.querySelector('.copal-sheet-surface'))
  })`);
  assert.equal(sourceOpen.tabs, sourceTabBefore + 1, 'file.name must open one source tab after the prior source-2 save/refresh');
  assert.deepEqual({ sourceTab:sourceOpen.sourceTab, editor:sourceOpen.editor, sheet:sourceOpen.sheet }, { sourceTab:'Alpha.md', editor:true, sheet:false }, 'file.name must resolve its authoritative ResourceKey to the source editor');
  await evaluate('window.__copal.open("bases",false)');
  await until('document.querySelector(".copal-sheet-surface[data-sheet-status=ready]")?.getAttribute("aria-busy") !== "true" && document.querySelector(".copal-sheet-cell[data-sheet-column-key=due]")', 'return to sheet after source open');
  await new Promise((resolve) => setTimeout(resolve, 50));
  const search = await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-search');input.focus();input.value='Alpha';input.setSelectionRange(2,5);input.dispatchEvent(new InputEvent('input',{bubbles:true,data:'a',inputType:'insertText'}));return { active:document.activeElement === input, selection:[input.selectionStart,input.selectionEnd] };})()`);
  assert.deepEqual(search, { active:true, selection:[2,5] });
  await new Promise((resolve) => setTimeout(resolve, 220));
  const searchAfterRender = await evaluate(`({ active:document.activeElement?.classList.contains('copal-sheet-search'), value:document.activeElement?.value, selection:[document.activeElement?.selectionStart,document.activeElement?.selectionEnd] })`);
  assert.deepEqual(searchAfterRender, { active:true, value:'Alpha', selection:[2,5] });
  const rawBaseTransition = [];
  const rawBaseSnapshot = () => evaluate(`(()=>{const content=document.querySelector('.copal-note-leaf[data-view-type="base"] .cm-content');return {activeLeaf:document.querySelector('.copal-note-tab.active .copal-note-tab-label')?.title||'',rawEditor:document.querySelectorAll('.copal-note-leaf[data-view-type="base"] .cm-editor').length,sheets:document.querySelectorAll('.copal-sheet-surface').length,sheetListeners:window.__rawSheetProbe.active,sourceText:content?.textContent||''};})()`);
  const openRawBase = async () => {
    const opened = await evaluate(`(()=>{const tools=document.querySelectorAll('.copal-sheet-tool');tools[tools.length-1].click();return true})()`);
    assert.equal(opened, true);
    await until('document.querySelector(".copal-sheet-overflow[open]")');
    await evaluate(`[...document.querySelectorAll('.copal-sheet-overflow button')].find((button)=>button.textContent==='Open raw source').click()`);
    await until('Boolean(document.querySelector(\'.copal-note-leaf[data-view-type="base"] .copal-codemirror-host[data-syntax-ready="ready"][aria-busy="false"] .cm-content\'))');
    return rawBaseSnapshot();
  };
  const openTypedBase = async () => {
    await evaluate('window.__copal.open("bases",false)');
    await until('document.querySelector(".copal-sheet-surface[data-sheet-status=ready] .copal-sheet-cell")');
    return evaluate(`(()=>({activeLeaf:document.querySelector('.copal-note-tab.active .copal-note-tab-label')?.title||'',rawEditor:document.querySelectorAll('.copal-note-leaf[data-view-type="base"] .cm-editor').length,sheets:document.querySelectorAll('.copal-sheet-surface').length,sheetListeners:window.__rawSheetProbe.active,title:document.querySelector('.copal-sheet-title h2')?.textContent||'',columns:[...document.querySelectorAll('.copal-sheet-grid th')].map((node)=>node.textContent)}))()`);
  };
  await openRawBase();
  const rawEditedDefinition = `version: 1\nextensions:\n  title: Projects/Raw Edited.base\nviews:\n  - id: table\n    name: To Watch\n    type: table\n    columns:\n      - property: file.name\n      - property: file.tags\n      - property: status\n      - property: priority\n        type: number\n      - property: score\n        type: number\n      - property: due\n        type: date\n      - property: done\n        type: boolean\n`;
  const rawSelection = await evaluate(`(()=>{const leaf=document.querySelector('.copal-note-leaf[data-view-type="base"]');const content=leaf?.querySelector('.cm-content');if(!leaf||!content)throw new Error('raw Base editor content missing');content.focus({ preventScroll:true });const selection=content.ownerDocument.getSelection();const range=content.ownerDocument.createRange();range.selectNodeContents(content);selection.removeAllRanges();selection.addRange(range);const normalize=(value)=>String(value||'').replace(/\\r\\n/g,'\\n');const selected=normalize(selection.toString());const source=normalize(content.textContent);return { active:leaf.contains(document.activeElement), selectionInside:content.contains(selection.anchorNode)&&content.contains(selection.focusNode), selectedLength:selected.length, sourceLength:source.length, selectedMatchesSource:selected===source };})()`);
  assert.equal(rawSelection.active, true, 'raw edit focuses the addressed CodeMirror leaf');
  assert.equal(rawSelection.selectionInside, true, 'raw edit selection stays inside the addressed CodeMirror content');
  assert.equal(rawSelection.selectedMatchesSource, true, 'raw edit selects the complete source buffer');
  await cdp('Input.insertText', { text:rawEditedDefinition });
  await until('document.querySelector(".copal-note-leaf[data-view-type=base] .cm-content")?.textContent.includes("Raw Edited")');
  rawBaseTransition.push(await rawBaseSnapshot());
  rawBaseTransition.push(await openTypedBase());
  rawBaseTransition.push(await openRawBase());
  rawBaseTransition.push(await openTypedBase());
  assert.equal(rawBaseTransition.length, 4, 'Base raw/typed lifecycle completes two full transitions');
  assert.equal(rawBaseTransition[0].sourceText.replace(/\s+/g, ''), rawEditedDefinition.replace(/\s+/g, ''), 'raw edit remains a valid complete source document');
  for (const [index, snapshot] of rawBaseTransition.entries()) {
    assert.equal(snapshot.activeLeaf, 'Projects/To Watch.base', `transition ${index + 1} stays on the addressed Base leaf`);
    if (index % 2 === 0) {
      assert.equal(snapshot.rawEditor, 1, `transition ${index + 1} mounts one Base raw editor`);
      assert.equal(snapshot.sheets, 0, `transition ${index + 1} retires the Base sheet surface`);
      assert.equal(snapshot.sheetListeners, 0, `transition ${index + 1} retires all Base sheet listeners`);
      assert.match(snapshot.sourceText, /version/);
      assert.match(snapshot.sourceText, /Raw Edited/, `transition ${index + 1} retains the dirty raw Base buffer`);
    } else {
      assert.equal(snapshot.rawEditor, 0, `transition ${index + 1} disposes the Base raw editor`);
      assert.equal(snapshot.sheets, 1, `transition ${index + 1} mounts one typed Base sheet`);
      assert.ok(snapshot.sheetListeners > 0, `transition ${index + 1} owns one typed Base listener set`);
      assert.equal(snapshot.title, 'Raw Edited', `transition ${index + 1} renders the edited Base definition`);
      assert.deepEqual(snapshot.columns, ['Name','Tags','Status','Priority','Score','Due','Done']);
    }
  }
  const editLifecycle = await evaluate(`(()=>{
    const selector='.copal-sheet-cell[data-sheet-row-index="2"][data-sheet-column-index="2"]';
    const results=[];
    for (let cycle=0; cycle<24; cycle += 1) {
      const original=document.querySelector(selector);
      const originalValue=original?.textContent || '';
      const surface=original?.closest('.copal-sheet-surface');
      original?.focus();
      original?.dispatchEvent(new KeyboardEvent('keydown',{key:'F2',bubbles:true}));
      const editor=document.querySelector('.copal-sheet-editor');
      const opened={ editors:document.querySelectorAll('.copal-sheet-editor').length, connected:Boolean(editor?.isConnected) };
      editor?.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      const restored=document.querySelector(selector);
      const escaped={ editors:document.querySelectorAll('.copal-sheet-editor').length, tag:restored?.tagName || '', value:restored?.textContent || '', connected:Boolean(restored?.isConnected), surfaceStillConnected:Boolean(restored?.closest('.copal-sheet-surface')?.isConnected) };
      restored?.focus();
      restored?.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',shiftKey:true,bubbles:true}));
      const reopenedEditor=document.querySelector('.copal-sheet-editor');
      const reopened={ editors:document.querySelectorAll('.copal-sheet-editor').length, connected:Boolean(reopenedEditor?.isConnected), sameCell:reopenedEditor?.parentElement === restored, sameSurface:reopenedEditor?.closest('.copal-sheet-surface') === restored?.closest('.copal-sheet-surface') };
      reopenedEditor?.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
      results.push({ cycle, originalValue, opened, escaped, reopened, finalEditors:document.querySelectorAll('.copal-sheet-editor').length });
    }
    return results;
  })()`);
  assert.equal(editLifecycle.length, 24, 'the edit lifecycle must complete every deterministic cycle');
  for (const result of editLifecycle) {
    assert.deepEqual(result.opened, { editors:1, connected:true }, `F2 cycle ${result.cycle} must open exactly one connected editor`);
    assert.equal(result.escaped.editors, 0, `Escape cycle ${result.cycle} must remove the editor`);
    assert.equal(result.escaped.tag, 'TD', `Escape cycle ${result.cycle} must restore a semantic cell`);
    assert.equal(result.escaped.value, result.originalValue, `Escape cycle ${result.cycle} must restore the original value`);
    assert.equal(result.escaped.connected, true, `Escape cycle ${result.cycle} must retain a connected cell`);
    assert.equal(result.escaped.surfaceStillConnected, true, `Escape cycle ${result.cycle} must retain a connected sheet mount`);
    assert.deepEqual(result.reopened, { editors:1, connected:true, sameCell:true, sameSurface:true }, `Shift+Enter cycle ${result.cycle} must reopen on the restored cell`);
    assert.equal(result.finalEditors, 0, `cycle ${result.cycle} cleanup must leave no editor`);
  }
  await evaluate(`fetch('/api/test-defer-source-1',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({enabled:true})})`);
  const writesBeforeDeferred = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes)');
  const requestsBeforeDeferred = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.requests.length)');
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="score"]');cell.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));const input=document.querySelector('.copal-sheet-editor');input.value='31';input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));})()`);
  await until(`fetch("/api/test-state").then(r=>r.json()).then(s=>s.requests.length > ${requestsBeforeDeferred} && s.requests.slice(${requestsBeforeDeferred}).some((request)=>request.id === "source-1"))`, 'first edit request is held open');
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="due"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);
  await until('document.querySelectorAll(".copal-sheet-editor").length === 1');
  const pendingEditor = await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');return { column:input?.closest('[data-sheet-column-key]')?.dataset.sheetColumnKey, connected:Boolean(input?.isConnected), active:document.activeElement === input };})()`);
  assert.deepEqual(pendingEditor, { column:'due', connected:true, active:true }, 'a second edit owns the connected editor while the first write is pending');
  const deferredRefreshBefore = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.queries)');
  await evaluate(`fetch('/api/test-release-source-1',{method:'POST'})`);
  await until(`fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes >= ${writesBeforeDeferred + 1})`, 'first deferred edit resolves');
  await until(`fetch("/api/test-state").then(r=>r.json()).then(s=>s.queries > ${deferredRefreshBefore})`, 'first edit refresh request starts');
  await evaluate(`fetch('/api/test-release-source-1-refresh',{method:'POST'})`);
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.source1RefreshReleased === true)', 'first edit refresh response is released');
  await evaluate('new Promise((resolve)=>queueMicrotask(()=>queueMicrotask(resolve)))');
  const survivingEditor = await evaluate(`(()=>{const input=document.querySelector('.copal-sheet-editor');return { editors:document.querySelectorAll('.copal-sheet-editor').length, column:input?.closest('[data-sheet-column-key]')?.dataset.sheetColumnKey, connected:Boolean(input?.isConnected), active:document.activeElement === input };})()`);
  assert.deepEqual(survivingEditor, { editors:1, column:'due', connected:true, active:true }, 'the first edit completion cannot tear down or focus over the newer edit');
  await evaluate('document.querySelector(".copal-sheet-editor").dispatchEvent(new KeyboardEvent("keydown",{key:"Escape",bubbles:true}))');
  await until('document.querySelectorAll(".copal-sheet-editor").length === 0');
  await evaluate(`fetch('/api/test-defer-source-1',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({enabled:false})})`);
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]').focus();document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))`);
  const focus = await evaluate(`({selected:[...document.querySelectorAll('.copal-sheet-cell[aria-selected="true"]')].length, tabs:[...document.querySelectorAll('.copal-sheet-cell[tabindex="0"]')].length, focused:document.activeElement?.dataset?.sheetColumnIndex})`);
  assert.equal(focus.tabs, 1); assert.equal(focus.selected, 1); assert.equal(focus.focused, '1');
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]');cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'a',metaKey:true,bubbles:true}));})()`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(await evaluate('document.querySelectorAll(".copal-sheet-cell[aria-selected=\\"true\\"]").length'), 700);
  await evaluate(`document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]')?.focus()`);
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="1"]');cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));})()`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(await evaluate('document.activeElement?.dataset?.sheetColumnIndex'), '2');
  await evaluate(`document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',shiftKey:true,bubbles:true}))`);
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(await evaluate('document.activeElement?.dataset?.sheetColumnIndex'), '1');
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="99"][data-sheet-column-index="6"]');cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));})()`);
  await until('document.activeElement?.classList.contains("copal-sheet-search") || document.activeElement?.classList.contains("copal-sheet-tool")', 'Tab from last editable cell focuses a sheet control');
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="99"][data-sheet-column-index="6"]');cell.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));const input=document.querySelector('.copal-sheet-editor');input.checked=true;input.dispatchEvent(new KeyboardEvent('keydown',{key:'Tab',bubbles:true}));})()`);
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes >= 3)');
  await until('document.activeElement?.classList.contains("copal-sheet-search") || document.activeElement?.classList.contains("copal-sheet-tool")', 'Tab after checkbox commit focuses a sheet control');
  await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="6"]');cell.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));const input=document.querySelector('.copal-sheet-editor');input.checked=true;input.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await until('fetch("/api/test-state").then(r=>r.json()).then(s=>s.writes >= 3)');
  await until('document.querySelector(".copal-sheet-editor") === null');
  assert.equal(await evaluate('document.querySelector(".copal-sheet-editor")'), null);
  await evaluate('document.querySelector(".copal-sheet-cell[data-sheet-row-index=\\"0\\"][data-sheet-column-index=\\"0\\"]")?.scrollIntoView({ block:"start" })');
  const pointerProbe = await evaluate(`(()=>{const a=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]');const b=document.querySelector('.copal-sheet-cell[data-sheet-row-index="1"][data-sheet-column-index="1"]');const ra=a.getBoundingClientRect();const rb=b.getBoundingClientRect();return {a:[ra.x+4,ra.y+4],b:[rb.x+4,rb.y+4]};})()`);
  await cdp('Input.dispatchMouseEvent', { type:'mousePressed', button:'left', clickCount:1, x:pointerProbe.a[0], y:pointerProbe.a[1] });
  await cdp('Input.dispatchMouseEvent', { type:'mouseMoved', button:'left', buttons:1, x:pointerProbe.b[0], y:pointerProbe.b[1] });
  await new Promise((resolve) => setTimeout(resolve, 30));
  await cdp('Input.dispatchMouseEvent', { type:'mouseReleased', button:'left', clickCount:1, x:pointerProbe.b[0], y:pointerProbe.b[1] });
  await new Promise((resolve) => setTimeout(resolve, 50));
  const drag = await evaluate(`({ selected:document.querySelectorAll('.copal-sheet-cell[aria-selected="true"]').length, tabs:document.querySelectorAll('.copal-sheet-cell[tabindex="0"]').length })`);
  assert.deepEqual(drag, { selected:4, tabs:1 });
  const perf = await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-index="0"]');const samples=[];for(let i=0;i<20;i++){const start=performance.now();cell.focus();cell.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));cell.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));samples.push(performance.now()-start);}samples.sort((a,b)=>a-b);return { p95:samples[Math.floor(samples.length*.95)-1] || 0, max:Math.max(...samples), cells:document.querySelectorAll('.copal-sheet-cell').length };})()`);
  assert.equal(perf.cells, 700); assert.equal(perf.p95 < 8, true);
  const desktop = await cdp('Page.captureScreenshot', { format:'png' }); fs.writeFileSync('/tmp/copal-s04-sheet-desktop.png', Buffer.from(desktop.data, 'base64'));
  await cdp('Emulation.setDeviceMetricsOverride', { width:420, height:900, deviceScaleFactor:1, mobile:false });
  assert.deepEqual(await evaluate('({ title:document.querySelector(".copal-sheet-title h2")?.textContent, resource:!!document.querySelector(".copal-sheet-resource"), opaque:!!document.querySelector(".copal-sheet-title")?.textContent.match(/sheet-account|base-1/) })'), { title:'Raw Edited', resource:false, opaque:false });
  const narrow = await cdp('Page.captureScreenshot', { format:'png' }); fs.writeFileSync('/tmp/copal-s04-sheet-narrow.png', Buffer.from(narrow.data, 'base64'));
  const hiddenQueryBefore = await evaluate('fetch("/api/test-state").then(r=>r.json()).then(s=>s.queries)');
  await evaluate(`fetch('/api/test-hide-due',{method:'POST'})`);
  await evaluate(`(()=>{window.__sheetNativeSetTimeout=window.setTimeout;window.__heldSheetTimers=[];window.setTimeout=(fn,delay,...args)=>{if(delay===0){window.__heldSheetTimers.push(()=>fn(...args));return 0;}return window.__sheetNativeSetTimeout(fn,delay,...args);};})()`);
  state.events?.write('event: document\ndata: {"id":"source-1"}\n\n');
  await until(`fetch("/api/test-state").then(r=>r.json()).then(s=>s.queries > ${hiddenQueryBefore})`, 'hidden-column definition reaches the controller');
  const staleHiddenCell = await evaluate(`(()=>{const cell=document.querySelector('.copal-sheet-cell[data-sheet-row-index="0"][data-sheet-column-key="due"]');cell?.focus();cell?.dispatchEvent(new KeyboardEvent('keydown',{key:'F2',bubbles:true}));return { connected:Boolean(cell?.isConnected), editors:document.querySelectorAll('.copal-sheet-editor').length };})()`);
  assert.deepEqual(staleHiddenCell, { connected:true, editors:0 }, 'a connected pre-repaint cell for a hidden property cannot start an edit');
  state.hideDue = false;
  await evaluate(`(()=>{window.setTimeout=window.__sheetNativeSetTimeout;for(const run of window.__heldSheetTimers.splice(0))run();})()`);
  await until('document.querySelector(\'.copal-note-leaf[data-view-type="base"] .copal-leaf-menu summary\')');
  await evaluate(`(()=>{const leaf=document.querySelector('.copal-note-leaf[data-view-type="base"]');leaf.querySelector('.copal-leaf-menu summary')?.click();})()`);
  await evaluate(`(()=>{const leaf=document.querySelector('.copal-note-leaf[data-view-type="base"]');const button=[...leaf.querySelectorAll('.copal-popover-menu button')].find((item)=>item.textContent==='Split right');if(!button)throw new Error('Split right action missing');button.click();})()`);
  await until('document.querySelector(".copal-quick-switcher")');
  await evaluate(`(()=>{const row=[...document.querySelectorAll('.copal-quick-switcher .copal-doc-row')].find((button)=>button.textContent.includes('Projects/To Watch.base'));if(!row)throw new Error('Base duplicate chooser row missing');row.click();})()`);
  await until('document.querySelectorAll(".copal-note-leaf[data-view-type=base]").length === 2 && document.querySelectorAll(".copal-sheet-surface").length === 2');
  const duplicateTargetLeaf = await evaluate('document.querySelectorAll(".copal-note-leaf[data-view-type=base]")[0].dataset.leafId');
  const duplicateSiblingLeaf = await evaluate('document.querySelectorAll(".copal-note-leaf[data-view-type=base]")[1].dataset.leafId');
  await evaluate(`(()=>{const article=document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateTargetLeaf}"]');article.querySelector('.copal-sheet-tool:last-of-type')?.click();})()`);
  await until(`document.querySelector('.copal-sheet-overflow[data-sheet-leaf-id="${duplicateTargetLeaf}"]')`);
  await evaluate(`(()=>{const dialog=document.querySelector('.copal-sheet-overflow[data-sheet-leaf-id="${duplicateTargetLeaf}"]');const button=[...dialog.querySelectorAll('button')].find((item)=>item.textContent==='Open raw source');if(!button)throw new Error('target Base raw action missing');button.click();})()`);
  await until(`document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateTargetLeaf}"] .cm-editor')`);
  const duplicateRawRoute = await evaluate(`(()=>({targetRaw:!!document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateTargetLeaf}"] .cm-editor'),targetSheet:!!document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateTargetLeaf}"] .copal-sheet-surface'),siblingRaw:!!document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateSiblingLeaf}"] .cm-editor'),siblingSheet:!!document.querySelector('.copal-note-leaf[data-leaf-id="${duplicateSiblingLeaf}"] .copal-sheet-surface'),baseLeaves:document.querySelectorAll('.copal-note-leaf[data-view-type="base"]').length}))()`);
  assert.deepEqual(duplicateRawRoute, { targetRaw:true, targetSheet:false, siblingRaw:false, siblingSheet:true, baseLeaves:2 }, 'raw overflow routes only the invoking duplicate Base leaf');
  await evaluate(`(()=>{const tabs=document.querySelector('.copal-note-tab.active')?.closest('.copal-note-tabs');tabs?.querySelector('.copal-group-menu summary')?.click();})()`);
  await evaluate(`(()=>{const tabs=document.querySelector('.copal-note-tab.active')?.closest('.copal-note-tabs');const button=[...tabs.querySelectorAll('.copal-group-menu .copal-popover-menu button')].find((item)=>item.textContent==='Close tab group');if(!button)throw new Error('duplicate tab group close action missing');button.click();})()`);
  await until('document.querySelectorAll(".copal-note-leaf[data-view-type=base]").length === 1 && document.querySelectorAll(".copal-sheet-surface").length === 1');
  const directSessionRace = await evaluate(`(async()=>{
    const { mountSheet } = await import('/static/js/copal/sheetView.js?direct-session-race');
    const definition={version:1,views:[{id:'table',name:'Table',type:'table',columns:[{property:'first'},{property:'second'}]}],extensions:{}};
    const directState={definition,viewId:'table',rows:[{documentId:'direct-1',values:{first:'A',second:'B'}}],selected:null,status:'ready',error:null,generation:0,counts:{shown:1,matched:1,limited:null,total:1,truncated:false},density:'compact',wrap:false,query:{text:'',filters:null}};
    const listeners=new Set(); let resolveA; let editCalls=0; let firstEditPromise=null; let firstRefreshDone=false;
    const controller={
      getState:()=>directState,
      subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener);},
      editCell(){editCalls+=1;if(editCalls!==1)return Promise.resolve({outcome:'applied'});const gate=new Promise((resolve)=>{resolveA=resolve;});firstEditPromise=gate.then(async(result)=>{await Promise.resolve();firstRefreshDone=true;return result;});return firstEditPromise;},
      select(){},move(){},setQuery(){},setView(){},setPresentation(){},clear(){return[];},previewPaste(){return{cells:[],rejected:[]};},refresh(){return Promise.resolve({outcome:'applied'});},close(){},
    };
    const target=document.createElement('div');target.dataset.directSessionRace='true';document.body.append(target);
    const dispose=mountSheet(target,controller,{});
    const cellA=target.querySelector('.copal-sheet-cell[data-sheet-column-key="first"]');cellA.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));
    const inputA=target.querySelector('.copal-sheet-editor');inputA.value='A-committed';inputA.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
    if(!firstEditPromise||typeof resolveA!=='function')throw new Error('first edit did not reach the deferred controller promise');
    const cellB=target.querySelector('.copal-sheet-cell[data-sheet-column-key="second"]');cellB.dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));
    const before={editors:target.querySelectorAll('.copal-sheet-editor').length,column:target.querySelector('.copal-sheet-editor')?.closest('[data-sheet-column-key]')?.dataset.sheetColumnKey};
    resolveA({outcome:'applied'});
    await firstEditPromise;
    const after={editors:target.querySelectorAll('.copal-sheet-editor').length,column:target.querySelector('.copal-sheet-editor')?.closest('[data-sheet-column-key]')?.dataset.sheetColumnKey,connected:Boolean(target.querySelector('.copal-sheet-editor')?.isConnected),active:document.activeElement===target.querySelector('.copal-sheet-editor'),refreshDone:firstRefreshDone};
    dispose();target.remove();
    return {before,after};
  })()`);
  assert.deepEqual(directSessionRace.before, { editors:1, column:'second' }, 'direct mountSheet race opens B while A is pending');
  assert.deepEqual(directSessionRace.after, { editors:1, column:'second', connected:true, active:true, refreshDone:true }, 'awaiting A completion cannot tear down or focus over B');
  const directPasteRefresh = await evaluate(`(async()=>{
    const { mountSheet } = await import('/static/js/copal/sheetView.js?direct-paste-refresh');
    const definition={version:1,views:[{id:'table',name:'Table',type:'table',columns:[{property:'first'},{property:'second'}]}],extensions:{}};
    let directState={definition,viewId:'table',rows:[{documentId:'paste-1',values:{first:'before',second:'keep'}}],selected:null,status:'ready',error:null,generation:0,counts:{shown:1,matched:1,limited:null,total:1,truncated:false},density:'compact',wrap:false,query:{text:'',filters:null}};
    const listeners=new Set(); let refreshes=0; let scenario='partial'; let pasteCalls=0;
    const emit=()=>{for(const listener of listeners)listener(directState);};
    const controller={getState:()=>directState,subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener);},select(){},move(){},setQuery(){},setView(){},setPresentation(){},clear(){return[];},previewPaste(){return{cells:[{rowKey:'paste-1',columnKey:'first',value:'after'}],rejected:[]};},refresh(){refreshes+=1;if(scenario==='partial'&&pasteCalls===1)directState={...directState,rows:[{documentId:'paste-1',values:{first:'after',second:'keep'}}]};emit();return Promise.resolve({outcome:'applied'});},editCell(){return Promise.resolve({outcome:'applied'});},close(){}};
    const target=document.createElement('div');document.body.append(target);const settle=()=>new Promise(requestAnimationFrame);
    const deferred=(value)=>Promise.resolve().then(()=>value);
    const paste=async()=>{pasteCalls+=1;if(scenario==='partial')return deferred({outcome:'partial',failures:[{sourceId:'paste-2',message:'retryable'}],retry:async()=>deferred({outcome:'applied'})});if(scenario==='rebase')return deferred({outcome:'partial',failures:[{sourceId:'paste-2',outcome:'conflict',retryKind:'conflict',remote:{head:'head-2'}}],reviewConflict:async()=>deferred({outcome:'applied'})});return deferred({outcome:'failed',message:'blocked'});};
    const dispose=mountSheet(target,controller,{onPaste:paste});
    const openPaste=()=>{const cell=target.querySelector('.copal-sheet-cell[data-sheet-column-key="first"]');const event=new Event('paste',{bubbles:true,cancelable:true});Object.defineProperty(event,'clipboardData',{value:{getData:()=> 'after'}});cell.dispatchEvent(event);};
    openPaste();await settle();document.querySelector('.copal-sheet-paste-preview button.primary').click();await settle();
    const partialRefresh=refreshes;const partialDialog=document.querySelector('.copal-sheet-paste-preview')?.isConnected;const partialValue=target.querySelector('.copal-sheet-cell[data-sheet-column-key="first"]')?.textContent;
    document.querySelector('.copal-sheet-paste-preview button.primary').click();await settle();
    const replayRefreshes=refreshes;const replayClosed=!document.querySelector('.copal-sheet-paste-preview');
    scenario='rebase';openPaste();await settle();document.querySelector('.copal-sheet-paste-preview button.primary').click();await settle();const rebaseInitialRefreshes=refreshes;const rebaseDialog=Boolean(document.querySelector('.copal-sheet-paste-preview'));
    const conflictButton=document.querySelector('.copal-sheet-paste-preview button:not(.primary):not(:first-child)');conflictButton.click();conflictButton.click();await settle();
    const rebaseRefreshes=refreshes;const rebaseClosed=!document.querySelector('.copal-sheet-paste-preview');
    scenario='failed';openPaste();await settle();const failedBefore=refreshes;document.querySelector('.copal-sheet-paste-preview button.primary').click();await settle();const failedRefreshes=refreshes-failedBefore;const failedDialog=Boolean(document.querySelector('.copal-sheet-paste-preview'));document.querySelector('.copal-sheet-paste-preview')?.close();
    dispose();target.remove();return{partialRefresh,partialDialog,partialValue,replayRefreshes,replayClosed,rebaseInitialRefreshes,rebaseDialog,rebaseRefreshes,rebaseClosed,failedRefreshes,failedDialog,pasteCalls};
  })()`);
  assert.deepEqual(directPasteRefresh, { partialRefresh:1, partialDialog:true, partialValue:'after', replayRefreshes:2, replayClosed:true, rebaseInitialRefreshes:3, rebaseDialog:true, rebaseRefreshes:4, rebaseClosed:true, failedRefreshes:0, failedDialog:true, pasteCalls:3 }, 'paste apply, replay, rebase, and failure paths own exactly the intended refreshes and dialog state');
  const directLeafScroll = await evaluate(`(async()=>{
    const { mountSheet } = await import('/static/js/copal/sheetView.js?direct-leaf-scroll');
    const definition={version:1,views:[{id:'table',name:'Table',type:'table',columns:[{property:'first'},{property:'second'}]},{id:'list',name:'List',type:'list',columns:[{property:'first'}]}],extensions:{}};
    const rows=Array.from({length:5},(_,index)=>({documentId:'scroll-'+index,values:{first:'A'+index,second:'B'+index}}));
    const makeController=()=>{let current={definition,viewId:'table',rows,selected:null,status:'ready',error:null,generation:0,counts:{shown:rows.length,matched:rows.length,limited:null,total:rows.length,truncated:false},density:'compact',wrap:false,query:{text:'',filters:null}};const listeners=new Set();const emit=()=>{for(const listener of listeners)listener(current)};return {getState:()=>current,subscribe(listener){listeners.add(listener);return()=>listeners.delete(listener)},setView(id){current={...current,viewId:id};emit();return Promise.resolve({outcome:'applied'})},setPresentation(patch){current={...current,...patch};emit();return current},select(){},move(){},setQuery(){},editCell(){return Promise.resolve({outcome:'applied'})},command(){return Promise.resolve({outcome:'applied'})},previewPaste(){return{cells:[],rejected:[]}},clear(){return[]},refresh(){return Promise.resolve({outcome:'applied'})},close(){}}};
    const firstTarget=document.createElement('div');const secondTarget=document.createElement('div');for(const target of [firstTarget,secondTarget]){target.className='direct-scroll-leaf';target.style.cssText='height:120px;display:flex;flex-direction:column';document.body.append(target)}
    const geometry=document.createElement('style');geometry.dataset.directScrollGeometry='true';geometry.textContent='.direct-scroll-leaf .copal-sheet-grid-wrap{flex:0 0 50px;height:50px;min-height:50px;overflow:auto}';document.head.append(geometry);
    const firstController=makeController();const secondController=makeController();const firstDispose=mountSheet(firstTarget,firstController,{});const secondDispose=mountSheet(secondTarget,secondController,{});await new Promise(requestAnimationFrame);
    const wraps=[firstTarget.querySelector('.copal-sheet-grid-wrap'),secondTarget.querySelector('.copal-sheet-grid-wrap')];if(wraps.some((wrap)=>wrap.scrollHeight<=wrap.clientHeight))throw new Error(JSON.stringify(wraps.map((wrap)=>({scrollHeight:wrap.scrollHeight,clientHeight:wrap.clientHeight}))));wraps[0].scrollTop=37;wraps[1].scrollTop=83;
    const immediate=[wraps[0].scrollTop,wraps[1].scrollTop];if(JSON.stringify(immediate)!==JSON.stringify([37,83]))throw new Error('two mounted leaves accept independent baseline viewport positions: '+JSON.stringify(immediate));
    const settle=()=>new Promise(resolve=>setTimeout(resolve,10));
    secondTarget.querySelectorAll('.copal-sheet-tab')[1].focus();secondTarget.querySelectorAll('.copal-sheet-tab')[1].click();await settle();const listFocus=Boolean(secondTarget.contains(document.activeElement)&&document.activeElement?.textContent==='List'&&!firstTarget.contains(document.activeElement));
    firstController.setPresentation({wrap:true});await settle();const siblingFocus=Boolean(secondTarget.contains(document.activeElement)&&document.activeElement?.textContent==='List'&&!firstTarget.contains(document.activeElement));
    secondTarget.querySelector('.copal-sheet-tab').focus();secondTarget.querySelector('.copal-sheet-tab').click();await settle();
    const result={immediate,scroll:[firstTarget.querySelector('.copal-sheet-grid-wrap')?.scrollTop,secondTarget.querySelector('.copal-sheet-grid-wrap')?.scrollTop],views:[firstTarget.querySelector('.copal-sheet-tab[aria-selected="true"]')?.textContent,secondTarget.querySelector('.copal-sheet-tab[aria-selected="true"]')?.textContent],targets:[firstTarget.querySelectorAll('.copal-sheet-surface').length,secondTarget.querySelectorAll('.copal-sheet-surface').length],listFocus,siblingFocus,finalFocus:secondTarget.contains(document.activeElement)&&document.activeElement?.textContent==='Table'};firstDispose();secondDispose();geometry.remove();firstTarget.remove();secondTarget.remove();return result;
  })()`);
  assert.deepEqual(directLeafScroll, { immediate:[37,83], scroll:[37,83], views:['Table','Table'], targets:[1,1], listFocus:true, siblingFocus:true, finalFocus:true }, 'two independently mounted controllers retain each leaf viewport and focus through sibling view switches and subscription renders');
  await evaluate(`(()=>{window.__sheetListeners={adds:0,removes:0};const add=EventTarget.prototype.addEventListener;const remove=EventTarget.prototype.removeEventListener;const isSheet=(target)=>target?.classList?.contains('copal-note-leaf-content')||target?.classList?.contains('copal-sheet-surface');EventTarget.prototype.addEventListener=function(type,listener,options){if(isSheet(this))window.__sheetListeners.adds+=1;return add.call(this,type,listener,options)};EventTarget.prototype.removeEventListener=function(type,listener,options){if(isSheet(this))window.__sheetListeners.removes+=1;return remove.call(this,type,listener,options)};})()`);
  const beforeClose = await evaluate('document.querySelectorAll(".copal-sheet-surface").length'); await evaluate('window.__copal.close("notes", false)'); await new Promise((resolve) => setTimeout(resolve, 80));
  const afterClose = await evaluate('document.querySelectorAll(".copal-sheet-surface").length'); assert.equal(beforeClose, 1); assert.equal(afterClose, 0);
  await evaluate('window.__copal.open("bases", true)');
  await until('document.querySelector(".copal-sheet-surface[data-sheet-status=ready]")?.getAttribute("aria-busy") !== "true" && document.querySelectorAll(".copal-sheet-cell").length === 700');
  const reopened = await evaluate('({ surfaces:document.querySelectorAll(".copal-sheet-surface").length, title:document.querySelector(".copal-sheet-title h2")?.textContent, cells:document.querySelectorAll(".copal-sheet-cell").length })');
  assert.deepEqual(reopened, { surfaces:1, title:'Raw Edited', cells:700 }, 'direct close/open remounts a ready Editor sheet without init');
  await evaluate('window.__copal.close("notes", false)'); await new Promise((resolve) => setTimeout(resolve, 80));
  const finalClose = await evaluate('document.querySelectorAll(".copal-sheet-surface").length'); assert.equal(finalClose, 0);
  const listenerDelta = await evaluate('window.__sheetListeners'); assert.equal(listenerDelta.adds, 10); assert.ok(listenerDelta.removes >= 20, `all mounted sheet listeners were removed: ${JSON.stringify(listenerDelta)}`);
  console.log(JSON.stringify({ fixture:'mounted Editor spreadsheet', initial, afterEdit, focus, drag, perf, lifecycle:{ beforeClose, afterClose, reopened, finalClose }, screenshots:['/tmp/copal-s04-sheet-desktop.png','/tmp/copal-s04-sheet-narrow.png'], queries:state.queries, writes:state.writes }));
});
