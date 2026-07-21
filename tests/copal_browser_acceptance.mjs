#!/usr/bin/env node

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7000').replace(/\/$/, '');
const debuggerBase = (process.argv[3] || 'http://127.0.0.1:9222').replace(/\/$/, '');
const outputDir = process.argv[4] || '/tmp/openclank-copal-browser';
fs.mkdirSync(outputDir, { recursive:true });

const targets = await fetch(`${debuggerBase}/json`).then((response) => response.json());
const target = targets.find((item) => item.type === 'page');
assert(target?.webSocketDebuggerUrl, 'Chromium page target is unavailable');
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  socket.addEventListener('open', resolve, { once:true });
  socket.addEventListener('error', reject, { once:true });
});

let sequence = 0;
const pending = new Map();
const exceptions = [];
const consoleMessages = [];
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id) {
    const request = pending.get(message.id); if (!request) return;
    pending.delete(message.id); clearTimeout(request.timer);
    message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
    return;
  }
  if (message.method === 'Runtime.exceptionThrown') exceptions.push(message.params.exceptionDetails.exception?.description || message.params.exceptionDetails.text);
  if (message.method === 'Runtime.consoleAPICalled' && ['error','warning'].includes(message.params.type)) consoleMessages.push(message.params.args.map((item) => item.value || item.description || item.type).join(' '));
});

function command(method, params = {}) {
  const id = ++sequence;
  return new Promise((resolve, reject) => {
    const detail = method === 'Runtime.evaluate' ? `: ${String(params.expression || '').replace(/\s+/g, ' ').slice(0, 180)}` : '';
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out${detail}`)); }, 25_000);
    pending.set(id, { resolve, reject, timer }); socket.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const response = await command('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true });
  if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
  return response.result.value;
}

async function waitFor(expression, label, timeout = 25_000) {
  const deadline = Date.now() + timeout; let lastError;
  while (Date.now() < deadline) {
    try { if (await evaluate(expression)) return; } catch (error) { lastError = error; }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out waiting for ${label}${lastError ? `: ${lastError.message}` : ''}`);
}

async function navigate(route) {
  const url = `${base}${route}`; await command('Page.navigate', { url });
  await waitFor(`location.href === ${JSON.stringify(url)}`, `${route} navigation`);
  await waitFor("document.readyState === 'complete' && !document.getElementById('app-loader')", `${route} ready`);
}

async function screenshot(name) {
  const selector = name === 'notes-quick-switcher' ? 'dialog.copal-quick-switcher[open]'
    : name === 'notes-conflict-recovery' ? 'dialog.copal-conflict-dialog[open]'
      : name === 'rich-event-native-window' ? '#copal-event-editor-modal .copal-modal-content'
        : name.startsWith('timeline-') ? '#copal-timeline-modal .copal-modal-content'
          : '#copal-notes-modal .copal-modal-content';
  await evaluate(`(() => {
    const saved=[]; const keep=/Acceptance|QOL Browser|Long Note|Notes Mention|Notes Canvas|Trash Probe/i;
    const textSelectors=['#copal-notes-modal .copal-file-row span:last-child','#copal-notes-modal .copal-folder-row span:last-child','#copal-notes-modal .copal-note-tab-label','#copal-notes-modal .copal-base-cell','#copal-notes-modal .copal-base-leaf-toolbar strong','#copal-notes-modal .copal-base-table th','#copal-notes-modal .copal-link-result strong','#copal-timeline-modal .copal-event','#copal-timeline-modal .copal-track-label > span'];
    for (const node of document.querySelectorAll(textSelectors.join(','))) {
      if (keep.test(node.textContent || '')) continue;
      saved.push([node,'text',node.textContent]); node.textContent=node.matches('.copal-track-label > span') ? 'Synthetic track' : node.matches('.copal-event') ? 'Synthetic event' : 'Reference';
    }
    if (${JSON.stringify(name)} === 'rich-event-native-window') for (const node of document.querySelectorAll('#copal-event-editor-modal input[type="text"],#copal-event-editor-modal textarea')) { saved.push([node,'value',node.value]); node.value='Synthetic fixture'; }
    window.__copalScreenshotRestore=saved;
  })()`);
  const clip = await evaluate(`(() => { const rect=document.querySelector(${JSON.stringify(selector)})?.getBoundingClientRect(); return rect ? { x:Math.max(0,rect.left), y:Math.max(0,rect.top), width:Math.max(1,Math.min(innerWidth,rect.right)-Math.max(0,rect.left)), height:Math.max(1,Math.min(innerHeight,rect.bottom)-Math.max(0,rect.top)), scale:1 } : null; })()`);
  const image = await command('Page.captureScreenshot', { format:'png', captureBeyondViewport:false, ...(clip ? { clip } : {}) });
  await evaluate(`(() => { for (const [node,kind,value] of window.__copalScreenshotRestore || []) { if (kind==='value') node.value=value; else node.textContent=value; } delete window.__copalScreenshotRestore; })()`);
  fs.writeFileSync(path.join(outputDir, `${name}.png`), Buffer.from(image.data, 'base64'));
}

async function jsonRequest(route, options = {}) {
  const response = await fetch(`${base}${route}`, { headers:{ 'Content-Type':'application/json', ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error(`${route}: HTTP ${response.status} ${await response.text()}`);
  return response.json();
}

// Poll planning state from the test process rather than inside the page: the
// post-commit SSE/reload burst can starve the page's per-host connection pool
// long enough that an in-page fetch (and the awaited evaluate) stalls.
async function waitForPlanning(predicate, label, timeout = 25_000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const data = await jsonRequest('/api/copal/planning?workspace=default');
    if (predicate(data)) return data;
    await new Promise((resolve) => setTimeout(resolve, 150));
  }
  throw new Error(`Timed out waiting for ${label}`);
}

function shiftDay(value, amount) {
  const date = new Date(`${value}T12:00:00`); date.setDate(date.getDate() + amount);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

await command('Page.enable');
await command('Runtime.enable');
await command('Network.enable');
await command('Network.setCacheDisabled', { cacheDisabled:true });
await command('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });

const viewSelectors = {
  notes:'.copal-notes-workspace', wiki:'.copal-story .copal-tiddler', timeline:'.copal-timeline-v2', galaxy:'.copal-graph',
  graph:'.copal-graph', mind:'.copal-mind-tree', bases:'.copal-bases-workspace', treehouse:"nav[aria-label='TreeHouse sections']", todo:'.copal-meatbag-tasks',
};
const labels = { notes:'Notes', wiki:'Wiki', timeline:'Timeline', galaxy:'Galaxy', graph:'Graph', mind:'Mind', bases:'Bases', treehouse:'TreeHouse', todo:'Meatbag Tasks' };
const results = { migration:{}, windows:{}, notes:{}, timeline:{}, editor:{}, tracks:{}, labels:{}, calendar:{}, mobile:{}, exceptions, consoleMessages };
const checkpoint = (label) => console.error(`[copal-browser] ${label}`);

const planningBaseline = await jsonRequest('/api/copal/planning?workspace=default');
if (!planningBaseline.trackRegistry && planningBaseline.tracks.length === 0 && !planningBaseline.migration) {
  const today = new Date().toISOString().slice(0, 10);
  const tracks = [
    { id:'acceptance-main', name:'Acceptance main', color:'#14b8a6', icon:'◆', enabled:true, tasks:[
      { id:'acceptance-hard', title:'Hard-date acceptance event', description:'Browser fixture with movable hard dates.', startDate:shiftDay(today,-1), dueDate:shiftDay(today,1), status:'pending', priority:'high', sharedTrackIds:['acceptance-shared'], tags:['acceptance'], stages:[{ id:'proof', title:'Prove movement', done:false, date:today }] },
      { id:'acceptance-overlap', title:'Overlapping acceptance event', description:'For deterministic lane packing.', startDate:shiftDay(today,-1), dueDate:shiftDay(today,1), status:'in-progress', priority:'medium', tags:['acceptance'] },
      { id:'acceptance-fuzzy', title:'Fuzzy acceptance event', description:'Fuzzy dates must not expose drag handles.', startDate:'FUZZY', dueDate:null, fuzzy:{ anchorStart:shiftDay(today,-2), anchorEnd:shiftDay(today,2), fadeIn:true }, status:'pending', priority:'low', tags:['acceptance'] },
    ] },
    { id:'acceptance-shared', name:'Acceptance shared', color:'#0ea5e9', icon:'◇', enabled:true, tasks:[] },
  ];
  await jsonRequest('/api/copal/documents?workspace=default', {
    method:'POST',
    body:JSON.stringify({
      name:'.copal/browser-acceptance-planning.json', kind:'planning',
      content:JSON.stringify({ title:'Browser acceptance', today, globalStart:shiftDay(today,-14), tracks, floatingTodos:[{ id:'acceptance-floating', text:'Floating acceptance task', status:'pending', priority:'medium' }] }),
    }),
  });
  results.migration.seededLegacyFixture = true;
}

let docs = (await jsonRequest('/api/copal/documents?workspace=default')).docs;
if (!docs.some((doc) => doc.kind === 'base')) {
  await jsonRequest('/api/copal/documents?workspace=default', {
    method:'POST',
    body:JSON.stringify({
      name:'Acceptance/Browser Matrix.base', kind:'base',
      content:`${JSON.stringify({
        version:1,
        views:[{ id:'table', name:'Table', type:'table', columns:[{ property:'file.name', label:'Name' },{ property:'status', label:'Status' },{ property:'tags', label:'Tags' }], filters:null, sorts:[], groupBy:null, summaries:{}, limit:1000, extensions:{} }],
        extensions:{ title:'Browser acceptance' },
      }, null, 2)}\n`,
    }),
  });
  docs = (await jsonRequest('/api/copal/documents?workspace=default')).docs;
}
const openClankKnowledge = docs.filter((doc) => doc.owner === 'shared' && doc.name.startsWith('OpenClank/'));
assert.equal(openClankKnowledge.length, 4);
assert(openClankKnowledge.every((doc) => doc.kind === 'note' && doc.storage === 'database' && doc.readOnly === true && doc.properties?.builtin === true));
let acceptance = docs.find((doc) => doc.name === 'Acceptance/QOL Browser.md');
const acceptanceContent = '---\nstatus: active\nscore: 7\ndone: false\ndue: 2026-07-18\nstarted: 2026-07-11T09:30\ntags: ["acceptance", "notes"]\nowners: ["Eliott", "Odysseus"]\n---\n# Browser QOL\n\nThis is **inline** Live Preview with `code`, ==highlight==, ~~strike~~, $x + y$, [a link](https://example.com), and #acceptance. %%hidden note%%\n\n- [ ] Verify editing\n1. Verify ordered lists\n\n> [!note] Shared semantics\n> Reading and inline modes agree.\n\n| Mode | Default |\n| --- | --- |\n| Live Preview | yes |\n\n$$\nx + y = z\n$$\n\n---\n\n[^proof]: Footnote fixture\n\n## Linked section\n\n[[Acceptance/Notes Mention]]\n';
if (!acceptance) acceptance = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:'Acceptance/QOL Browser.md', kind:'markdown', content:acceptanceContent }),
})).doc;
else if (acceptance.text !== acceptanceContent) {
  await jsonRequest(`/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default`, { method:'PUT', body:JSON.stringify({ content:acceptanceContent, base:acceptance.head }) });
  acceptance = await jsonRequest(`/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default`);
}
let canvasFixture = docs.find((doc) => doc.name === 'Acceptance/Notes Canvas.canvas');
if (!canvasFixture) canvasFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:'Acceptance/Notes Canvas.canvas', kind:'canvas', content:JSON.stringify({ nodes:[{ id:'welcome', type:'text', text:'Canvas typed view', x:0, y:0, width:260, height:120 }, { id:'note', type:'file', file:'Acceptance/QOL Browser.md', x:360, y:120, width:280, height:120 }], edges:[{ id:'edge', fromNode:'welcome', toNode:'note', label:'opens' }] }) }),
})).doc;
let mentionFixture = docs.find((doc) => doc.name === 'Acceptance/Notes Mention.md');
if (!mentionFixture) mentionFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:'Acceptance/Notes Mention.md', kind:'markdown', content:'# Mention\n\nQOL Browser appears here as an unlinked mention.\n' }),
})).doc;
const nativeContent = '# Native database note\n\nThis body is projected from a structured Copal record. See [[Acceptance/QOL Browser.md]].\n\n#acceptance #database';
let nativeFixture = docs.find((doc) => doc.name === 'Acceptance/Native Database Note');
if (!nativeFixture) nativeFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({
    name:'Acceptance/Native Database Note', content:nativeContent,
    properties:{ status:'active', score:9, tags:['acceptance','database'] },
    relations:[{ kind:'link', target:'Acceptance/QOL Browser.md', targetDocumentId:acceptance.id }],
  }),
})).doc;
else if (nativeFixture.text !== nativeContent || nativeFixture.properties?.status !== 'active') {
  await jsonRequest(`/api/copal/documents/${encodeURIComponent(nativeFixture.id)}?workspace=default`, {
    method:'PUT', body:JSON.stringify({
      content:nativeContent, base:nativeFixture.head,
      properties:{ status:'active', score:9, tags:['acceptance','database'] },
      relations:[{ kind:'link', target:'Acceptance/QOL Browser.md', targetDocumentId:acceptance.id }],
    }),
  });
  nativeFixture = await jsonRequest(`/api/copal/documents/${encodeURIComponent(nativeFixture.id)}?workspace=default`);
}
const longContent = Array.from({ length:900 }, (_, index) => `## Long heading ${index + 1}\n\nParagraph ${index + 1} with **formatting** and #long-note.`).join('\n\n');
let longFixture = docs.find((doc) => doc.name === 'Acceptance/Long Note.md');
if (!longFixture) longFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:'Acceptance/Long Note.md', kind:'markdown', content:longContent }),
})).doc;
else if (longFixture.text !== longContent) {
  await jsonRequest(`/api/copal/documents/${encodeURIComponent(longFixture.id)}?workspace=default`, { method:'PUT', body:JSON.stringify({ content:longContent, base:longFixture.head }) });
  longFixture = await jsonRequest(`/api/copal/documents/${encodeURIComponent(longFixture.id)}?workspace=default`);
}
const longLabelName = 'Acceptance/RTL عنوان تجريبي — Extremely Long Synthetic Workspace Label For Geometry And Accessible Name Verification 0123456789.md';
let longLabelFixture = docs.find((doc) => doc.name === longLabelName);
if (!longLabelFixture) longLabelFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:longLabelName, kind:'markdown', content:'# RTL and long-label geometry\n\nSynthetic browser-only fixture.\n' }),
})).doc;
let trashFixture = docs.find((doc) => doc.name === 'Acceptance/Trash Probe.md');
if (!trashFixture) trashFixture = (await jsonRequest('/api/copal/documents?workspace=default', {
  method:'POST', body:JSON.stringify({ name:'Acceptance/Trash Probe.md', kind:'markdown', content:'# Trash probe\n' }),
})).doc;
docs = (await jsonRequest('/api/copal/documents?workspace=default')).docs;
const baseFixture = docs.find((doc) => doc.kind === 'base');

await navigate(`/?acceptance=${Date.now()}`);
await evaluate("localStorage.removeItem('odysseus-copal-notes-layout:default')");
await evaluate("localStorage.removeItem('odysseus-copal-timeline-v2:default')");
await evaluate("localStorage.setItem('odysseus-copal-notes-window-size', JSON.stringify({w:99999,h:99999}))");
await navigate(`/copal/timeline?acceptance=${Date.now()}`);
await waitFor("document.querySelector('#copal-timeline-modal:not(.hidden) .copal-timeline-v2')", 'canonical Timeline window', 35_000);
await waitFor(`fetch('/api/copal/planning?workspace=default').then((response) => response.json()).then((data) => data.canonical === true && data.migrationRequired === false)`, 'canonical migration', 35_000);
checkpoint('canonical migration ready');
const migrated = await jsonRequest('/api/copal/planning?workspace=default');
const migratedEvents = migrated.tracks.flatMap((track) => (track.tasks || []).map((event) => ({ ...event, primaryTrackId:track.id }))).concat(migrated.floatingTodos || []);
assert(migrated.trackRegistry?.id);
assert(migratedEvents.length > 0);
assert.equal(migrated.migration?.state, 'complete');
results.migration = { tracks:migrated.tracks.length, events:migratedEvents.length, marker:migrated.migration.state, diagnostics:migrated.diagnostics };

async function openView(view) {
  await evaluate(`document.querySelector('[data-copal-view=${view}]').click()`);
  await waitFor(`location.pathname === '/copal/${view}'`, `${view} route`);
  const selector = `#copal-${view}-modal:not(.hidden) ${viewSelectors[view]}`;
  await waitFor(`document.querySelector(${JSON.stringify(selector)})`, `${view} content`);
}

for (const view of Object.keys(viewSelectors)) {
  await openView(view);
  const details = await evaluate(`(() => {
    const root=document.getElementById('copal-${view}-modal'); const box=root.querySelector('.copal-modal-content').getBoundingClientRect();
    return { title:root.querySelector('.copal-workspace-title').firstChild.textContent.trim(), width:Math.round(box.width), height:Math.round(box.height), instances:document.querySelectorAll('#copal-${view}-modal').length, minimized:root.querySelectorAll('.modal-minimize-btn,.minimize-btn').length };
  })()`);
  assert.equal(details.title, labels[view]);
  assert.equal(details.instances, 1);
  assert.equal(details.minimized, 1);
  assert(details.width >= 500 || view === 'notes');
  results.windows[view] = details;
}

results.windows.simultaneous = await evaluate(`(() => ({ visible:[...document.querySelectorAll('.copal-view-window:not(.hidden)')].map((node) => node.id), sse:performance.getEntriesByType('resource').filter((entry) => entry.name.includes('/api/copal/events')).length }))()`);
assert.equal(new Set(results.windows.simultaneous.visible).size, 9);
checkpoint('nine windows ready');

await evaluate("document.querySelector('#copal-notes-modal .modal-minimize-btn,#copal-notes-modal .minimize-btn').click()");
await waitFor("document.getElementById('copal-notes-modal').classList.contains('modal-minimized')", 'Notes minimize');
await waitFor("document.querySelector('.minimized-dock-chip[data-modal-id=\"copal-notes-modal\"]')", 'Notes dock chip');
await evaluate("document.querySelector('.minimized-dock-chip[data-modal-id=\"copal-notes-modal\"]').click()");
await waitFor("!document.getElementById('copal-notes-modal').classList.contains('modal-minimized')", 'Notes restore');

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'Notes quick switcher');
await screenshot('notes-quick-switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/QOL Browser'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .cm-editor')", 'CodeMirror note editor');
results.notes = await evaluate(`(() => ({
  codeMirror:!!document.querySelector('#copal-notes-modal .cm-editor'), textarea:document.querySelectorAll('#copal-notes-modal textarea.copal-editor').length,
  previewLayout:document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.previewLayout,
  editorMode:document.querySelector('#copal-notes-modal .copal-codemirror-host').dataset.mode,
  previewVisible:[...document.querySelectorAll('#copal-notes-modal .copal-note-live-preview')].filter((node)=>!node.hidden).length,
  ribbon:document.querySelectorAll('#copal-notes-modal .copal-notes-ribbon').length, tabs:document.querySelectorAll('#copal-notes-modal .copal-note-tab').length,
  explorer:!!document.querySelector('#copal-notes-modal .copal-file-tree'), rightSidebar:!!document.querySelector('#copal-notes-modal .copal-notes-sidebar'),
  headings:document.querySelectorAll('#copal-notes-modal .cm-md-heading').length, hiddenSyntax:document.querySelectorAll('#copal-notes-modal .cm-md-syntax-hidden').length,
  groups:document.querySelectorAll('#copal-notes-modal .copal-note-group').length,
  headerSearch:document.querySelectorAll('#copal-notes-modal .copal-window-actions input[type=search]').length,
  inlineTitle:document.querySelector('#copal-notes-modal .copal-inline-title')?.value,
  frontmatterCard:!!document.querySelector('#copal-notes-modal .cm-md-frontmatter-card'),
  semanticWidgets:[...document.querySelectorAll('#copal-notes-modal [class*="cm-md-"][class*="-widget"]')].map((node)=>node.className)
}))()`);
assert(results.notes.codeMirror);
assert.equal(results.notes.textarea, 0);
assert.equal(results.notes.previewLayout, 'inline');
assert.equal(results.notes.editorMode, 'live');
assert.equal(results.notes.previewVisible, 0);
assert.equal(results.notes.ribbon, 0);
assert.equal(results.notes.rightSidebar, false);
assert(results.notes.headings > 0 && results.notes.hiddenSyntax > 0);
assert.equal(results.notes.groups, 1);
assert.equal(results.notes.headerSearch, 0);
assert.equal(results.notes.inlineTitle, 'QOL Browser');
assert(results.notes.frontmatterCard);
assert(['table','callout','math','footnote','hr'].every((kind) => results.notes.semanticWidgets.some((className) => String(className).includes(`cm-md-${kind}-widget`))));
await screenshot('notes-default-inline');
results.notes.windowSizeMigration = await evaluate(`(() => {
  const modal=document.querySelector('#copal-notes-modal .copal-modal-content').getBoundingClientRect();
  const saved=JSON.parse(localStorage.getItem('odysseus-copal-notes-window-size'));
  return {
    saved,
    rect:{ left:modal.left, top:modal.top, right:modal.right, bottom:modal.bottom, width:modal.width, height:modal.height },
    viewport:{ width:innerWidth, height:innerHeight },
  };
})()`);
assert.equal(results.notes.windowSizeMigration.saved.version, 2);
assert(results.notes.windowSizeMigration.saved.width <= results.notes.windowSizeMigration.viewport.width);
assert(results.notes.windowSizeMigration.saved.height <= results.notes.windowSizeMigration.viewport.height);
assert(results.notes.windowSizeMigration.rect.left >= -1 && results.notes.windowSizeMigration.rect.top >= -1);
assert(results.notes.windowSizeMigration.rect.right <= results.notes.windowSizeMigration.viewport.width + 1);
assert(results.notes.windowSizeMigration.rect.bottom <= results.notes.windowSizeMigration.viewport.height + 1);
await evaluate(`(() => {
  window.__copalAcceptanceEditor=document.querySelector('#copal-notes-modal .cm-editor');
  window.__copalEditorConstructions=Number(document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.editorConstructions);
  window.__copalLeftControl=document.getElementById('copal-notes-left-sidebar-toggle');
  window.__copalRightControl=document.getElementById('copal-notes-right-sidebar-toggle');
})()`);
await evaluate("document.querySelector('#copal-notes-modal .copal-note-tab.active .copal-note-tab-pin').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-note-tab.active.pinned .copal-note-tab-close:disabled')", 'pinned Notes tab');
results.notes.pinning = await evaluate("!!document.querySelector('#copal-notes-modal .copal-note-tab.active.pinned')");
await evaluate("document.querySelector('#copal-notes-modal .copal-note-tab.active .copal-note-tab-pin').click()");
await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').focus(); document.getElementById('copal-notes-left-sidebar-toggle').click()");
await waitFor("!document.querySelector('#copal-notes-modal .copal-notes-explorer') && document.querySelector('#copal-notes-modal .copal-shell-tab-slot.left > #copal-notes-left-sidebar-toggle')", 'collapsed Notes explorer');
await waitFor("document.activeElement === document.getElementById('copal-notes-left-sidebar-toggle')", 'left shell control focus preservation');
assert.equal(await evaluate("window.__copalLeftControl===document.getElementById('copal-notes-left-sidebar-toggle') && document.querySelectorAll('#copal-notes-left-sidebar-toggle').length===1"), true);
await screenshot('notes-left-collapsed');
await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-explorer') && document.querySelector('#copal-notes-modal .copal-shell-side-header.left > #copal-notes-left-sidebar-toggle')", 'restored Notes explorer');
await command('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').focus()");
await command('Input.dispatchKeyEvent', { type:'keyDown', key:' ', code:'Space', windowsVirtualKeyCode:32 });
await command('Input.dispatchKeyEvent', { type:'keyUp', key:' ', code:'Space', windowsVirtualKeyCode:32 });
await waitFor("document.querySelector('#copal-notes-modal .copal-shell-tab-slot.left > #copal-notes-left-sidebar-toggle')", 'reduced-motion left collapse');
assert.equal(await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').getAnimations().filter((animation)=>(animation.effect?.getTiming?.().duration||0) > 50).length"), 0);
await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-shell-side-header.left > #copal-notes-left-sidebar-toggle')", 'reduced-motion left restore');
assert.equal(await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').getAnimations().filter((animation)=>(animation.effect?.getTiming?.().duration||0) > 50).length"), 0);
await command('Emulation.setEmulatedMedia', { features:[] });
await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').focus(); document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-sidebar') && document.querySelector('#copal-notes-modal .copal-shell-side-header.right > #copal-notes-right-sidebar-toggle')", 'expanded Notes details');
await waitFor("document.activeElement === document.getElementById('copal-notes-right-sidebar-toggle')", 'right shell control focus preservation');
assert.equal(await evaluate("window.__copalRightControl===document.getElementById('copal-notes-right-sidebar-toggle') && document.querySelectorAll('#copal-notes-right-sidebar-toggle').length===1"), true);
await screenshot('notes-right-expanded');
await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("!document.querySelector('#copal-notes-modal .copal-notes-sidebar') && document.querySelector('#copal-notes-modal .copal-shell-tab-slot.right > #copal-notes-right-sidebar-toggle')", 'collapsed Notes details');
results.notes.collapsibleExplorer = true;
results.notes.stableShellControls = true;
assert.equal(await evaluate("window.__copalAcceptanceEditor===document.querySelector('#copal-notes-modal .cm-editor')"), true);
assert.equal(await evaluate("window.__copalEditorConstructions===Number(document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.editorConstructions)"), true);
results.notes.editorLifecycleStable = true;
await evaluate("document.getElementById('copal-notes-modal').dispatchEvent(new KeyboardEvent('keydown',{key:'o',ctrlKey:true,isComposing:true,bubbles:true}))");
await new Promise((resolve) => setTimeout(resolve, 120));
assert.equal(await evaluate("!!document.querySelector('dialog.copal-quick-switcher[open]')"), false);
await evaluate("document.getElementById('copal-notes-modal').dispatchEvent(new KeyboardEvent('keydown',{key:'o',ctrlKey:true,bubbles:true}))");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open]')", 'keyboard quick switcher');
await evaluate("document.querySelector('dialog.copal-quick-switcher[open]').close()");
await evaluate("document.getElementById('copal-notes-modal').dispatchEvent(new KeyboardEvent('keydown',{key:'p',ctrlKey:true,bubbles:true}))");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'keyboard command palette');
await evaluate("document.querySelector('dialog.copal-command-palette[open]').close()");
results.notes.keyboardCommandsAndComposition = true;
await evaluate(`(() => { const sort=document.querySelector('#copal-notes-modal select[aria-label="Sort files"]'); sort.value='modified'; sort.dispatchEvent(new Event('change',{bubbles:true})); })()`);
await waitFor("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).left.sort === 'modified'", 'file sort persistence');
await evaluate(`(() => { const rows=[...document.querySelectorAll('#copal-notes-modal .copal-file-row')].slice(0,2); rows.forEach((row)=>row.dispatchEvent(new MouseEvent('click',{bubbles:true,ctrlKey:true}))); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-file-selection')?.textContent.includes('2 selected')", 'file multi-selection');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-file-selection button')].find((node)=>node.textContent.trim()==='Clear').click()");
await waitFor("!document.querySelector('#copal-notes-modal .copal-file-selection')", 'file selection clear');
const leftWidthBefore = await evaluate("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).left.width");
await evaluate("document.querySelector('#copal-notes-modal .copal-sidebar-resize.left').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))");
await waitFor(`JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).left.width === ${leftWidthBefore + 12}`, 'keyboard sidebar resize');
const resizedLeftWidth = leftWidthBefore + 12;
results.notes.fileNavigation = true;

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'Notes command palette');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Notes settings')).click()");
await waitFor("document.querySelector('dialog.copal-notes-settings[open] select[aria-label=\"Preview layout\"]')", 'Notes settings');
const defaultLayout = await evaluate("document.querySelector('dialog.copal-notes-settings[open] select[aria-label=\"Preview layout\"]').value");
assert.equal(defaultLayout, 'inline');
await evaluate(`(() => { const dialog=document.querySelector('dialog.copal-notes-settings[open]'); const select=dialog.querySelector('select[aria-label="Preview layout"]'); select.value='side-by-side'; [...dialog.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-note-editing-surface.side-by-side') && !document.querySelector('#copal-notes-modal .copal-note-live-preview').hidden", 'opt-in side-by-side preview');
await screenshot('notes-side-by-side');
results.notes.sideBySidePersisted = await evaluate("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).settings.previewLayout");
assert.equal(results.notes.sideBySidePersisted, 'side-by-side');
await navigate(`/copal/notes?doc=${encodeURIComponent(acceptance.id)}&persist=${Date.now()}`);
await waitFor("document.querySelector('#copal-notes-modal .copal-note-editing-surface.side-by-side')", 'side-by-side reload persistence');
assert.equal(await evaluate("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).left.width"), resizedLeftWidth);
results.notes.sidebarResizeRestore = true;
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'reloaded Notes commands');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Inline preview layout')).click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.previewLayout === 'inline' && !document.querySelector('#copal-notes-modal .copal-note-editing-surface.side-by-side')", 'return to inline preview');

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'independent editor settings commands');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Notes settings')).click()");
await waitFor("document.querySelector('dialog.copal-notes-settings[open]')", 'independent editor settings');
await evaluate(`(() => { const dialog=document.querySelector('dialog.copal-notes-settings[open]'); dialog.querySelector('input[aria-label="Show line numbers"]').checked=true; dialog.querySelector('input[aria-label="Readable line width"]').checked=false; [...dialog.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .cm-lineNumbers') && getComputedStyle(document.querySelector('#copal-notes-modal .cm-content')).maxWidth === 'none'", 'independent line-number and width settings');
assert.deepEqual(await evaluate(`(() => { const settings=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).settings; return { lineNumbers:settings.lineNumbers, readableLineWidth:settings.readableLineWidth }; })()`), { lineNumbers:true, readableLineWidth:false });
results.notes.independentEditorSettings = true;
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'restore editor settings commands');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Notes settings')).click()");
await waitFor("document.querySelector('dialog.copal-notes-settings[open]')", 'restore editor settings');
await evaluate(`(() => { const dialog=document.querySelector('dialog.copal-notes-settings[open]'); dialog.querySelector('input[aria-label="Show line numbers"]').checked=false; dialog.querySelector('input[aria-label="Readable line width"]').checked=true; [...dialog.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitFor("!document.querySelector('#copal-notes-modal .cm-lineNumbers') && getComputedStyle(document.querySelector('#copal-notes-modal .cm-content')).maxWidth !== 'none'", 'restored clean editor settings');

await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-sidebar')", 'linked views sidebar');
const inspectorTabs = await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-inspector-tabs button')].map((node)=>node.textContent.trim())");
assert.deepEqual(inspectorTabs, ['Properties','Links','Outline']);
assert(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-property-editor').length >= 4"));
await screenshot('notes-properties');
await evaluate(`(() => { const input=document.querySelector('#copal-notes-modal input[aria-label="Value for score"]'); input.value='8'; input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
await waitFor(`fetch('/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default').then((response)=>response.json()).then((doc)=>/^score: 8$/m.test(doc.text))`, 'typed property Redb round-trip');
results.notes.typedPropertyRoundTrip = true;
assert.deepEqual(await evaluate(`(() => ({
  done:document.querySelector('#copal-notes-modal input[aria-label="Value for done"]')?.type,
  due:document.querySelector('#copal-notes-modal input[aria-label="Value for due"]')?.type,
  started:document.querySelector('#copal-notes-modal input[aria-label="Value for started"]')?.type,
  owners:document.querySelector('#copal-notes-modal input[aria-label="Value for owners"]')?.type
}))()`), { done:'checkbox', due:'date', started:'datetime-local', owners:'text' });
await evaluate(`(() => { const key=document.querySelector('#copal-notes-modal input[aria-label="Property name done"]'); key.value='complete'; key.dispatchEvent(new Event('change',{bubbles:true})); })()`);
await waitFor("[...document.querySelectorAll('#copal-notes-modal .copal-property-key')].some((node)=>node.value==='complete')", 'property key transaction');
await evaluate("document.querySelector('#copal-notes-modal .cm-content').focus()");
await command('Input.dispatchKeyEvent', { type:'keyDown', key:'z', code:'KeyZ', windowsVirtualKeyCode:90, modifiers:2 });
await command('Input.dispatchKeyEvent', { type:'keyUp', key:'z', code:'KeyZ', windowsVirtualKeyCode:90, modifiers:2 });
await waitFor(`fetch('/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default').then((response)=>response.json()).then((doc)=>/^done: false$/m.test(doc.text))`, 'property undo Redb save');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-inspector-tabs button')].find((node)=>node.textContent.trim()==='Links').click(); [...document.querySelectorAll('#copal-notes-modal .copal-inspector-tabs button')].find((node)=>node.textContent.trim()==='Properties').click()");
await waitFor("[...document.querySelectorAll('#copal-notes-modal .copal-property-key')].some((node)=>node.value==='done')", 'property undo inspector refresh');
results.notes.propertyUndo = true;

await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-inspector-tabs button')].find((node)=>node.textContent.trim()==='Outline').click()");
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-outline-entry').length >= 2", 'outline entries');
await evaluate("document.querySelector('#copal-notes-modal .copal-outline-row').click()");
await waitFor("document.querySelector('#copal-notes-modal .cm-activeLine')?.textContent.includes('Browser QOL')", 'outline heading navigation');
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Move Linked section up\"]').click()");
await waitFor("document.querySelector('#copal-notes-modal .cm-content').textContent.indexOf('Linked section') < document.querySelector('#copal-notes-modal .cm-content').textContent.indexOf('Browser QOL')", 'outline section move');
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Move Linked section down\"]').click()");
await waitFor("document.querySelector('#copal-notes-modal .cm-content').textContent.indexOf('Browser QOL') < document.querySelector('#copal-notes-modal .cm-content').textContent.indexOf('Linked section')", 'outline section restore');
results.notes.outlineTransactions = true;
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-inspector-tabs button')].find((node)=>node.textContent.trim()==='Links').click()");
await waitFor("[...document.querySelectorAll('#copal-notes-modal .copal-links-pane h3')].some((node)=>node.textContent==='Unlinked mentions')", 'unlinked mentions view');
assert(await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-mention-row')].some((node)=>node.textContent.includes('Notes Mention.md'))"));
await screenshot('notes-links');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-notes-sidebar > header button')].find((node)=>node.textContent.trim()==='Pin').click()");
assert.equal(await evaluate(`JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).right.pinnedDocId`), acceptance.id);
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-mention-row')].find((node)=>node.textContent.includes('Notes Mention.md')).click()");
await waitFor(`location.search.includes(${JSON.stringify(mentionFixture.id)}) || document.querySelector('#copal-notes-modal .copal-inline-title')?.value === 'Notes Mention'`, 'linked-view target open');
assert.equal(await evaluate(`JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).right.pinnedDocId`), acceptance.id);
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-notes-sidebar > header button')].find((node)=>node.textContent.trim()==='Unpin').click()");
await waitFor("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).right.pinnedDocId === null", 'linked-view unpin');
results.notes.sidebarPinFollow = true;

for (const [commandLabel, selector, label] of [
  ['Reading mode', '.copal-note-reading', 'Reading mode'],
  ['Source mode', '.copal-codemirror-host[data-mode="source"]', 'Source mode'],
  ['Live Preview mode', '.copal-codemirror-host[data-mode="live"]', 'Live Preview mode'],
]) {
  await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
  await waitFor("document.querySelector('dialog.copal-command-palette[open]')", `${label} commands`);
  await evaluate(`[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes(${JSON.stringify(commandLabel)})).click()`);
  await waitFor(`document.querySelector('#copal-notes-modal ${selector}')`, label);
  await screenshot(`notes-${label.toLowerCase().replaceAll(' ', '-')}`);
}
results.notes.modeTransitions = true;

await evaluate("document.getElementById('copal-notes-modal').dispatchEvent(new KeyboardEvent('keydown',{key:'F',ctrlKey:true,shiftKey:true,bubbles:true}))");
await waitFor("document.querySelector('#copal-notes-modal .copal-side-tabs button.active')?.textContent === 'Search' && document.querySelector('#copal-notes-modal .copal-note-search-input')", 'Notes search hotkey');
results.notes.searchHotkey = true;

const longOpenStarted = Date.now();
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'long-note quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/Long Note'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-inline-title')?.value === 'Long Note' && document.querySelector('#copal-notes-modal .cm-editor')", 'long-note editor');
results.notes.longNote = await evaluate(`(() => ({
  openMs:${Date.now()}-${longOpenStarted},
  renderedHeadings:document.querySelectorAll('#copal-notes-modal .cm-md-heading').length,
  constructions:Number(document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.editorConstructions),
  renderMs:Number(document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.renderMs)
}))()`);
assert(results.notes.longNote.openMs < 2500);
assert(results.notes.longNote.renderedHeadings > 0 && results.notes.longNote.renderedHeadings < 120);
assert(results.notes.longNote.renderMs < 500);

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'long-label quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='RTL عنوان'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor(`document.querySelector('#copal-notes-modal .copal-note-tab.active .copal-note-tab-label')?.title === ${JSON.stringify(longLabelName)}`, 'long-label active tab');
await evaluate("document.getElementById('copal-notes-modal').dir='rtl'");
await new Promise((resolve) => setTimeout(resolve, 120));
results.notes.longRtlLabel = await evaluate(`(() => {
  const root=document.getElementById('copal-notes-modal');
  const modal=root.querySelector('.copal-modal-content').getBoundingClientRect();
  const label=root.querySelector('.copal-note-tab.active .copal-note-tab-label');
  const left=document.getElementById('copal-notes-left-sidebar-toggle').getBoundingClientRect();
  const right=document.getElementById('copal-notes-right-sidebar-toggle').getBoundingClientRect();
  return {
    fullText:label.textContent,
    title:label.title,
    accessibleName:label.getAttribute('aria-label'),
    truncated:label.scrollWidth > label.clientWidth,
    leftPhysical:left.left + left.width / 2 < modal.left + modal.width / 2,
    rightPhysical:right.left + right.width / 2 > modal.left + modal.width / 2,
    controlCounts:[document.querySelectorAll('#copal-notes-left-sidebar-toggle').length,document.querySelectorAll('#copal-notes-right-sidebar-toggle').length],
    noPageOverflow:document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
  };
})()`);
assert.equal(results.notes.longRtlLabel.title, longLabelName);
assert.equal(results.notes.longRtlLabel.accessibleName, `Open ${longLabelName}`);
assert(results.notes.longRtlLabel.truncated && results.notes.longRtlLabel.leftPhysical && results.notes.longRtlLabel.rightPhysical);
assert.deepEqual(results.notes.longRtlLabel.controlCounts, [1,1]);
assert(results.notes.longRtlLabel.noPageOverflow);
await screenshot('notes-rtl-long-label');
await evaluate("document.getElementById('copal-notes-modal').removeAttribute('dir')");

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'Canvas quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Notes Canvas'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-note-leaf[data-view-type=canvas] .copal-canvas-view')", 'typed Canvas leaf');
assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-leaf[data-view-type=canvas] .cm-editor').length"), 0);
assert.equal(await evaluate("document.querySelector('#copal-notes-modal .copal-canvas-node')?.textContent.includes('Canvas typed view')"), true);
await screenshot('notes-canvas');

if (baseFixture) {
  await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
  await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'Base quick switcher');
  await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value=${JSON.stringify(baseFixture.name)}; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
  await waitFor("document.querySelector('#copal-notes-modal .copal-note-leaf[data-view-type=base] .copal-base-leaf')", 'typed Base leaf');
  assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-leaf[data-view-type=base] .cm-editor').length"), 0);
  await screenshot('notes-base');
}

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'return-note quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/QOL Browser'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-note-leaf[data-view-type=markdown] .cm-editor')", 'return to Markdown leaf');

await evaluate("document.querySelector('#copal-notes-modal .copal-tab-group-controls button[aria-label=\"Split right\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'split chooser');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Notes Mention'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-group').length === 2 && document.querySelector('#copal-notes-modal .copal-note-splitter')", 'recursive Notes split');
results.notes.groupsAfterSplit = await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-group').length");
await evaluate("document.querySelector('#copal-notes-modal .copal-note-group.active-group button[aria-label=\"Split below\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'nested split chooser');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Notes Canvas'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-group').length === 3 && document.querySelectorAll('#copal-notes-modal .copal-note-splitter').length === 2", 'nested recursive split');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-note-splitter')].at(-1)?.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}))");
await waitFor("JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).root.children", 'nested split persistence');
await navigate(`/copal/notes?doc=${encodeURIComponent(acceptance.id)}&restore=${Date.now()}`);
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-group').length === 3 && document.querySelectorAll('#copal-notes-modal .copal-note-splitter').length === 2", 'nested split reload restoration');
results.notes.nestedSplitRestore = true;
results.notes.accessibility = await evaluate(`(() => ({
  tabs:[...document.querySelectorAll('#copal-notes-modal [role="tab"]')].every((node)=>node.hasAttribute('aria-selected')),
  separators:[...document.querySelectorAll('#copal-notes-modal [role="separator"]')].every((node)=>node.hasAttribute('aria-label') && node.hasAttribute('tabindex')),
  valued:[...document.querySelectorAll('#copal-notes-modal .copal-note-splitter')].every((node)=>node.hasAttribute('aria-valuenow')),
  status:!!document.querySelector('#copal-notes-modal [role="status"][aria-live="polite"]')
}))()`);
assert(Object.values(results.notes.accessibility).every(Boolean));
await screenshot('notes-clean-room-codemirror');
for (const expected of [3, 2]) {
  assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-group').length"), expected);
  await evaluate(`(() => { const group=[...document.querySelectorAll('#copal-notes-modal .copal-note-group')].at(-1); const menu=group.querySelector('.copal-group-menu'); menu.open=true; [...menu.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Close tab group').click(); })()`);
  await waitFor(`document.querySelectorAll('#copal-notes-modal .copal-note-group').length === ${expected - 1}`, 'tab group close');
}
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'reopen command palette');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Reopen closed note')).click()");
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length >= 2", 'reopen closed note');
results.notes.closeReopen = true;

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'conflict-note quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/QOL Browser'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-inline-title')?.value === 'QOL Browser'", 'conflict note active');
const conflictRemote = await jsonRequest(`/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default`);
// Insert through the real trusted input path. A refocus needs a beat before
// CodeMirror's input binding is live again, so retry the whole focus+type
// attempt; a failed attempt inserts nothing, making the retry idempotent.
let conflictDrafted = false;
for (let attempt = 0; attempt < 10 && !conflictDrafted; attempt++) {
  await evaluate("(() => { const content=document.querySelector('#copal-notes-modal .cm-content'); content.blur(); content.focus(); })()");
  await new Promise((resolve) => setTimeout(resolve, 200));
  await command('Input.dispatchKeyEvent', { type:'keyDown', key:'End', code:'End', windowsVirtualKeyCode:35, modifiers:2 });
  await command('Input.dispatchKeyEvent', { type:'keyUp', key:'End', code:'End', windowsVirtualKeyCode:35, modifiers:2 });
  await command('Input.insertText', { text:'\nLOCAL-CONFLICT-MARKER' });
  await new Promise((resolve) => setTimeout(resolve, 250));
  conflictDrafted = await evaluate("document.querySelector('#copal-notes-modal .cm-content').textContent.includes('LOCAL-CONFLICT-MARKER')");
}
assert.equal(conflictDrafted, true, 'conflict draft typed into editor');
await waitFor("document.querySelector('#copal-notes-modal .copal-save-state.unsaved')", 'local conflict draft');
await jsonRequest(`/api/copal/documents/${encodeURIComponent(acceptance.id)}?workspace=default`, { method:'PUT', body:JSON.stringify({ content:`${conflictRemote.text}\nREMOTE-CONFLICT-MARKER`, base:conflictRemote.head }) });
await waitFor(`document.querySelector('#copal-conflict-${acceptance.id}[open]')`, 'recoverable Notes conflict', 15_000);
assert.equal(await evaluate(`(() => { const text=[...document.querySelectorAll('#copal-conflict-${acceptance.id} pre')].map((node)=>node.textContent); return text[0].includes('LOCAL-CONFLICT-MARKER') && text[1].includes('REMOTE-CONFLICT-MARKER'); })()`), true);
await screenshot('notes-conflict-recovery');
await evaluate(`[...document.querySelectorAll('#copal-conflict-${acceptance.id} button')].find((node)=>node.textContent.trim()==='Load latest').click()`);
await waitFor("!document.querySelector('#copal-conflict-" + acceptance.id + "[open]') && document.querySelector('#copal-notes-modal .cm-content')?.textContent.includes('REMOTE-CONFLICT-MARKER')", 'conflict load-latest recovery');
results.notes.conflictRecovery = true;

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'history command palette');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.trim().startsWith('History')).click()");
await waitFor("[...document.querySelectorAll('dialog[open] button')].some((node)=>node.textContent.trim()==='Restore')", 'Redb history revisions');
results.notes.history = await evaluate("[...document.querySelectorAll('dialog[open] button')].filter((node)=>node.textContent.trim()==='Restore').length");
await evaluate("[...document.querySelectorAll('dialog[open] button')].find((node)=>node.textContent.trim()==='Close').click()");

await jsonRequest(`/api/copal/documents/${encodeURIComponent(trashFixture.id)}?workspace=default`, { method:'DELETE' });
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'trash command palette');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Trash')).click()");
await waitFor("[...document.querySelectorAll('dialog[open] .copal-task-row')].some((node)=>node.textContent.includes('Trash Probe.md'))", 'trash fixture listing');
await evaluate("[...document.querySelectorAll('dialog[open] .copal-task-row')].find((node)=>node.textContent.includes('Trash Probe.md')).querySelector('button').click()");
await waitFor("fetch('/api/copal/documents?workspace=default').then((response)=>response.json()).then((data)=>data.docs.some((doc)=>doc.name==='Acceptance/Trash Probe.md'))", 'trash fixture restore');
results.notes.trashRestore = true;
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-side-tabs button')].find((node)=>node.textContent.trim()==='Notes').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-file-row[title=\"Acceptance/Trash Probe.md\"]')", 'restored file in explorer');
await evaluate(`(() => { const row=document.querySelector('#copal-notes-modal .copal-file-row[title="Acceptance/Trash Probe.md"]').closest('.copal-file-entry'); const menu=row.querySelector('details'); menu.open=true; [...menu.querySelectorAll('button')].find((node)=>node.textContent.includes('Rename or move')).click(); })()`);
await waitFor("document.querySelector('dialog[open] input[name=name]')", 'rename and move form');
await evaluate(`(() => { const dialog=document.querySelector('dialog[open]'); const input=dialog.querySelector('input[name=name]'); input.value='Acceptance/Trash Probe Renamed.md'; [...dialog.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitFor(`fetch('/api/copal/documents/${encodeURIComponent(trashFixture.id)}?workspace=default').then((response)=>response.json()).then((doc)=>doc.name==='Acceptance/Trash Probe Renamed.md')`, 'stable-ID rename and move');
await waitFor("document.querySelector('#copal-notes-modal .copal-file-row[title=\"Acceptance/Trash Probe Renamed.md\"]')", 'renamed file in explorer');
await evaluate(`(() => { const row=document.querySelector('#copal-notes-modal .copal-file-row[title="Acceptance/Trash Probe Renamed.md"]').closest('.copal-file-entry'); const menu=row.querySelector('details'); menu.open=true; [...menu.querySelectorAll('button')].find((node)=>node.textContent.includes('Rename or move')).click(); })()`);
await waitFor("document.querySelector('dialog[open] input[name=name]')", 'restore rename form');
await evaluate(`(() => { const dialog=document.querySelector('dialog[open]'); const input=dialog.querySelector('input[name=name]'); input.value='Acceptance/Trash Probe.md'; [...dialog.querySelectorAll('button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitFor(`fetch('/api/copal/documents/${encodeURIComponent(trashFixture.id)}?workspace=default').then((response)=>response.json()).then((doc)=>doc.name==='Acceptance/Trash Probe.md')`, 'stable-ID rename restore');
results.notes.renameMove = true;
const exported = await fetch(`${base}/api/copal/export/obsidian?workspace=default`);
assert(exported.ok && (exported.headers.get('content-type') || '').includes('zip') && (await exported.arrayBuffer()).byteLength > 100);
results.notes.export = true;

await evaluate("document.getElementById('user-bar-settings').click()");
await waitFor("!document.getElementById('settings-modal').classList.contains('hidden')", 'Settings window');
await evaluate("document.querySelector('#settings-modal [data-settings-tab=\"appearance\"]').click()");
await waitFor("document.querySelector('#settings-modal [data-settings-panel=\"appearance\"] [data-copal-notes-appearance-card]')", 'Copal Notes Appearance card');
assert.equal(await evaluate("document.querySelectorAll('#settings-modal [data-copal-notes-setting]').length"), 4);
await evaluate(`(() => {
  const root=document.querySelector('#settings-modal [data-copal-notes-appearance-card]');
  const layout=root.querySelector('[data-copal-notes-setting="previewLayout"]'); layout.value='side-by-side'; layout.dispatchEvent(new Event('change',{bubbles:true}));
  const lines=root.querySelector('[data-copal-notes-setting="lineNumbers"]'); lines.checked=true; lines.dispatchEvent(new Event('change',{bubbles:true}));
  const readable=root.querySelector('[data-copal-notes-setting="readableLineWidth"]'); readable.checked=false; readable.dispatchEvent(new Event('change',{bubbles:true}));
  const ribbon=root.querySelector('[data-copal-notes-setting="ribbon"]'); ribbon.checked=true; ribbon.dispatchEvent(new Event('change',{bubbles:true}));
})()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-ribbon') && document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.previewLayout === 'side-by-side'", 'live Notes Appearance update');
assert.deepEqual(await evaluate(`(() => { const value=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')).settings; return { previewLayout:value.previewLayout, lineNumbers:value.lineNumbers, readableLineWidth:value.readableLineWidth, ribbon:value.ribbon }; })()`), {
  previewLayout:'side-by-side', lineNumbers:true, readableLineWidth:false, ribbon:true,
});
await evaluate("document.querySelector('#settings-modal button[aria-label=\"Close settings\"]').click()");
await waitFor("document.getElementById('settings-modal').classList.contains('hidden')", 'Settings close');
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'Appearance private-dialog parity commands');
await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].find((node)=>node.textContent.includes('Notes settings')).click()");
await waitFor("document.querySelector('dialog.copal-notes-settings[open]')", 'Appearance private-dialog parity');
assert.deepEqual(await evaluate(`(() => { const dialog=document.querySelector('dialog.copal-notes-settings[open]'); return { previewLayout:dialog.querySelector('[aria-label="Preview layout"]').value, lineNumbers:dialog.querySelector('[aria-label="Show line numbers"]').checked, readableLineWidth:dialog.querySelector('[aria-label="Readable line width"]').checked, ribbon:dialog.querySelector('[aria-label="Show Notes ribbon"]').checked }; })()`), {
  previewLayout:'side-by-side', lineNumbers:true, readableLineWidth:false, ribbon:true,
});
await evaluate("document.querySelector('dialog.copal-notes-settings[open]').close()");
await evaluate("document.getElementById('user-bar-settings').click()");
await waitFor("!document.getElementById('settings-modal').classList.contains('hidden')", 'Settings reset window');
await evaluate("document.querySelector('#settings-modal [data-settings-tab=\"appearance\"]').click()");
await evaluate(`(() => {
  const root=document.querySelector('#settings-modal [data-copal-notes-appearance-card]');
  const layout=root.querySelector('[data-copal-notes-setting="previewLayout"]'); layout.value='inline'; layout.dispatchEvent(new Event('change',{bubbles:true}));
  const lines=root.querySelector('[data-copal-notes-setting="lineNumbers"]'); lines.checked=false; lines.dispatchEvent(new Event('change',{bubbles:true}));
  const readable=root.querySelector('[data-copal-notes-setting="readableLineWidth"]'); readable.checked=true; readable.dispatchEvent(new Event('change',{bubbles:true}));
  const ribbon=root.querySelector('[data-copal-notes-setting="ribbon"]'); ribbon.checked=false; ribbon.dispatchEvent(new Event('change',{bubbles:true}));
})()`);
await waitFor("!document.querySelector('#copal-notes-modal .copal-notes-ribbon') && document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.previewLayout === 'inline'", 'clean Notes Appearance restore');
await evaluate("document.querySelector('#settings-modal button[aria-label=\"Close settings\"]').click()");
results.notes.hostAppearance = true;

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'native-note quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/Native Database Note'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal article[data-view-type=\"note\"] .cm-editor') && document.querySelector('#copal-notes-modal .copal-inline-title')?.value === 'Native Database Note'", 'database-native note leaf');
const nativeIndexed = await jsonRequest(`/api/copal/documents/${encodeURIComponent(nativeFixture.id)}?workspace=default`);
assert.equal(nativeIndexed.storage, 'database');
assert.equal(nativeIndexed.format, 'copal-note-v1');
assert.equal(nativeIndexed.properties.score, 9);
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'native-note commands');
assert.equal(await evaluate("[...document.querySelectorAll('dialog.copal-command-palette[open] .copal-command-row')].some((node)=>node.textContent.trim().startsWith('Source mode'))"), false);
await evaluate("document.querySelector('dialog.copal-command-palette[open]').close()");
results.notes.databaseNative = true;

const startHere = openClankKnowledge.find((doc) => doc.name === 'OpenClank/Start Here');
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'knowledge quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='OpenClank/Start Here'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelector('#copal-notes-modal .copal-leaf-mode')?.textContent.includes('read only')", 'read-only built-in knowledge');
assert.equal(await evaluate("!!document.querySelector('#copal-notes-modal .copal-note-group-body .cm-editor')"), false);
const rejectedSeedWrite = await fetch(`${base}/api/copal/documents/${encodeURIComponent(startHere.id)}?workspace=default`, {
  method:'PUT', headers:{ 'Content-Type':'application/json' }, body:JSON.stringify({ content:'must not mutate shared knowledge', base:startHere.head }),
});
assert.equal(rejectedSeedWrite.status, 403);
results.notes.openClankKnowledge = { count:openClankKnowledge.length, readOnly:true };

await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Open Timeline\"]').click()");
await waitFor("document.querySelector('#copal-notes-modal article[data-view-type=\"timeline\"] .copal-timeline-v2')", 'Timeline inside Notes');
const notesTimelineEventIds = await evaluate("[...document.querySelectorAll('#copal-notes-modal article[data-view-type=\"timeline\"] [data-task-id]')].map((node)=>node.dataset.taskId)");
assert(notesTimelineEventIds.length > 0);
assert(notesTimelineEventIds.every((id) => migratedEvents.some((event) => event.id === id)));
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Open Timeline\"]').click()");
assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal article[data-view-type=\"timeline\"]').length"), 1);
assert.equal(await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-note-tab-label')].filter((node)=>node.textContent.trim()==='Timeline').length"), 1);
results.notes.timelineIntegrated = { events:notesTimelineEventIds.length, singleton:true };
checkpoint('notes acceptance ready');

// ── Slice 05: overlay dismissal, tab close, empty workspace ──
const pressEscape = async () => {
  await command('Input.dispatchKeyEvent', { type:'rawKeyDown', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
  await command('Input.dispatchKeyEvent', { type:'keyUp', key:'Escape', code:'Escape', windowsVirtualKeyCode:27 });
};
const notesVisible = () => evaluate("!document.getElementById('copal-notes-modal').classList.contains('hidden')");

// Popover menus close on Escape and outside click, ahead of window close.
await evaluate("document.querySelector('#copal-notes-modal .copal-group-menu > summary').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-group-menu[open]')", 'group menu popover open');
await pressEscape();
await waitFor("!document.querySelector('#copal-notes-modal .copal-group-menu[open]')", 'popover Escape dismissal');
assert.equal(await notesVisible(), true, 'Notes window must survive popover Escape');
await evaluate("document.querySelector('#copal-notes-modal .copal-group-menu > summary').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-group-menu[open]')", 'group menu popover reopen');
await new Promise((resolve) => setTimeout(resolve, 150));
await evaluate("document.querySelector('#copal-notes-modal .copal-note-group-body').dispatchEvent(new MouseEvent('click',{bubbles:true}))");
await waitFor("!document.querySelector('#copal-notes-modal .copal-group-menu[open]')", 'popover outside-click dismissal');

// Quick Switcher: Escape closes only the dialog; focus returns to trigger.
await evaluate("const trigger=document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]'); trigger.focus(); trigger.click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'slice05 quick switcher');
await pressEscape();
await waitFor("!document.querySelector('dialog.copal-quick-switcher[open]')", 'quick switcher Escape dismissal');
assert.equal(await notesVisible(), true, 'Notes window must survive dialog Escape');
await waitFor("document.activeElement === document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]')", 'dialog focus restoration');

// Outside pointerdown on the backdrop dismisses without activating what is
// underneath — press exactly over a tab close button and prove the tab stays.
const tabsBeforeBackdrop = await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length");
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'backdrop-test quick switcher');
const closeButtonPoint = await evaluate("(() => { const rect=document.querySelector('#copal-notes-modal .copal-note-tab-close:not(:disabled)').getBoundingClientRect(); return { x:Math.round(rect.left+rect.width/2), y:Math.round(rect.top+rect.height/2) }; })()");
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:closeButtonPoint.x, y:closeButtonPoint.y, button:'left', clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:closeButtonPoint.x, y:closeButtonPoint.y, button:'left', clickCount:1 });
await waitFor("!document.querySelector('dialog.copal-quick-switcher[open]')", 'backdrop pointerdown dismissal');
assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length"), tabsBeforeBackdrop, 'backdrop dismissal must not activate the underlying close button');
assert.equal(await notesVisible(), true);

// A press inside the dialog must not dismiss it.
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'inside-click quick switcher');
const insidePoint = await evaluate("(() => { const rect=document.querySelector('dialog.copal-quick-switcher[open] input').getBoundingClientRect(); return { x:Math.round(rect.left+rect.width/2), y:Math.round(rect.top+rect.height/2) }; })()");
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:insidePoint.x, y:insidePoint.y, button:'left', clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:insidePoint.x, y:insidePoint.y, button:'left', clickCount:1 });
await new Promise((resolve) => setTimeout(resolve, 120));
assert.equal(await evaluate("!!document.querySelector('dialog.copal-quick-switcher[open]')"), true, 'inside press must not dismiss the dialog');
await pressEscape();
await waitFor("!document.querySelector('dialog.copal-quick-switcher[open]')", 'inside-click cleanup');

// Notes Commands palette honors the same contract.
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Notes commands\"]').click()");
await waitFor("document.querySelector('dialog.copal-command-palette[open]')", 'slice05 command palette');
await pressEscape();
await waitFor("!document.querySelector('dialog.copal-command-palette[open]')", 'command palette Escape dismissal');
assert.equal(await notesVisible(), true);

// Non-dismissable status dialogs ignore backdrop presses always, and swallow
// Escape whenever the platform allows cancel to be prevented (i.e. after user
// activation; the close watcher deliberately lets a user escape otherwise).
await evaluate("import('/static/js/copal/overlays.js').then(({ wireDialog }) => { const dialog=document.createElement('dialog'); dialog.id='slice05-progress-probe'; dialog.textContent='probe'; wireDialog(dialog,{ dismissable:false }); document.body.append(dialog); dialog.showModal(); })");
await waitFor("document.querySelector('#slice05-progress-probe[open]')", 'non-dismissable probe open');
const probePoint = await evaluate("(() => { const rect=document.getElementById('slice05-progress-probe').getBoundingClientRect(); return { x:Math.round(rect.left+rect.width/2), y:Math.round(rect.top+rect.height/2) }; })()");
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:probePoint.x, y:probePoint.y, button:'left', clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:probePoint.x, y:probePoint.y, button:'left', clickCount:1 });
await pressEscape();
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:20, y:20, button:'left', clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:20, y:20, button:'left', clickCount:1 });
await new Promise((resolve) => setTimeout(resolve, 120));
assert.equal(await evaluate("!!document.querySelector('#slice05-progress-probe[open]')"), true, 'non-dismissable dialog must survive activated Escape and backdrop press');
await evaluate("document.getElementById('slice05-progress-probe').close()");
await waitFor("!document.getElementById('slice05-progress-probe')", 'non-dismissable probe removed');

// Close the whole tab group into a focused, persisted empty workspace.
await evaluate("document.querySelector('#copal-notes-modal .copal-group-menu > summary').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-group-menu[open]')", 'group menu for close-all');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-group-menu .copal-btn')].find((node)=>node.textContent.trim()==='Close tab group').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-empty-workspace')", 'empty workspace after group close');
assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length"), 0);
await waitFor("document.activeElement?.classList.contains('copal-notes-empty-workspace')", 'empty workspace focus');
const storedEmpty = await evaluate("(() => { const layout=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')); const leaves=[]; const visit=(node)=>{ if(!node) return; if(node.type==='group') leaves.push(...node.tabs); else (node.children||[]).forEach(visit); }; visit(layout.root); return { leaves:leaves.length, closed:layout.closed.length, active:layout.activeLeafId }; })()");
assert.equal(storedEmpty.leaves, 0);
assert.equal(storedEmpty.active, null);
assert(storedEmpty.closed >= 1);
await screenshot('notes-empty-workspace');

// The empty workspace survives reload with nothing resurrected.
await navigate(`/copal/notes?slice05empty=${Date.now()}`);
await waitFor("document.querySelector('#copal-notes-modal:not(.hidden) .copal-notes-empty-workspace')", 'empty workspace reload persistence');
assert.equal(await evaluate("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length"), 0, 'reload must not resurrect a tab');

// Empty-state actions: reopen closed, middle-click close, open Timeline.
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-notes-empty-workspace .copal-btn')].find((node)=>node.textContent.trim()==='Reopen closed').click()");
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length === 1", 'reopen closed from empty state');
await evaluate("document.querySelector('#copal-notes-modal .copal-note-tab').dispatchEvent(new MouseEvent('auxclick',{button:1,bubbles:true}))");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-empty-workspace')", 'middle-click close back to empty');
await evaluate("[...document.querySelectorAll('#copal-notes-modal .copal-notes-empty-workspace .copal-btn')].find((node)=>node.textContent.trim()==='Open Timeline').click()");
await waitFor("document.querySelector('#copal-notes-modal article[data-view-type=\"timeline\"]')", 'Timeline from empty state');
await evaluate("document.querySelector('#copal-notes-modal button[aria-label=\"Quick switcher\"]').click()");
await waitFor("document.querySelector('dialog.copal-quick-switcher[open] input')", 'restore-tabs quick switcher');
await evaluate(`(() => { const input=document.querySelector('dialog.copal-quick-switcher[open] input'); input.value='Acceptance/QOL Browser'; input.dispatchEvent(new Event('input',{bubbles:true})); document.querySelector('dialog.copal-quick-switcher[open] .copal-doc-row').click(); })()`);
await waitFor("document.querySelectorAll('#copal-notes-modal .copal-note-tab').length >= 2", 'workspace restored after slice05 checks');
results.notes.slice05 = { emptyWorkspace:true, overlayDismissal:true, backdropSafety:true, reopenClosed:true };
checkpoint('slice05 tabs and overlays ready');

await openView('timeline');
const timelineBefore = await evaluate(`(() => {
  const canvas=document.querySelector('#copal-timeline-modal .copal-timeline-v2'); const scroll=document.querySelector('#copal-timeline-modal .copal-timeline-scroll');
  const now=new Date(); const today=\`${'${'}now.getFullYear()}-${'${'}String(now.getMonth()+1).padStart(2,'0')}-${'${'}String(now.getDate()).padStart(2,'0')}\`;
  return { dayWidth:Number(getComputedStyle(canvas).getPropertyValue('--copal-day-width').replace('px','')), firstDate:document.querySelector('#copal-timeline-modal .copal-day-row span')?.title, today, scrollLeft:scroll.scrollLeft,
    months:document.querySelectorAll('#copal-timeline-modal .copal-month-row span').length, days:document.querySelectorAll('#copal-timeline-modal .copal-day-row span').length,
    controls:[...document.querySelectorAll('#copal-timeline-modal .copal-timeline-controls button')].map((node) => node.getAttribute('aria-label') || node.textContent.trim()) };
})()`);
assert(timelineBefore.months > 1 && timelineBefore.days > 20);
assert.equal(timelineBefore.firstDate, shiftDay(timelineBefore.today, -3));
assert(timelineBefore.controls.includes('Zoom in'));
await evaluate("document.querySelector('#copal-timeline-modal .copal-timeline-scroll').dispatchEvent(new WheelEvent('wheel', { deltaX:-120, bubbles:true }))");
await waitFor(`document.querySelector('#copal-timeline-modal .copal-day-row span')?.title !== ${JSON.stringify(timelineBefore.firstDate)} && document.querySelector('#copal-timeline-modal .copal-timeline-scroll').scrollLeft > 500`, 'wheel backward range extension');
await evaluate("[...document.querySelectorAll('#copal-timeline-modal .copal-timeline-controls button')].find((node) => node.getAttribute('aria-label') === 'Zoom in').click()");
await waitFor(`Number(getComputedStyle(document.querySelector('#copal-timeline-modal .copal-timeline-v2')).getPropertyValue('--copal-day-width').replace('px','')) > ${timelineBefore.dayWidth}`, 'timeline zoom');
const zoomed = await evaluate(`(() => { const canvas=document.querySelector('#copal-timeline-modal .copal-timeline-v2'); const scroll=document.querySelector('#copal-timeline-modal .copal-timeline-scroll'); return { dayWidth:Number(getComputedStyle(canvas).getPropertyValue('--copal-day-width').replace('px','')), scrollLeft:scroll.scrollLeft }; })()`);
assert.equal(zoomed.dayWidth, timelineBefore.dayWidth + 2);

let firstDate = timelineBefore.firstDate;
for (let index = 0; index < 3; index++) {
  await evaluate(`(() => { const scroll=document.querySelector('#copal-timeline-modal .copal-timeline-scroll'); scroll.scrollLeft=1; scroll.dispatchEvent(new Event('scroll')); })()`);
  await waitFor(`document.querySelector('#copal-timeline-modal .copal-day-row span')?.title !== ${JSON.stringify(firstDate)} && document.querySelector('#copal-timeline-modal .copal-timeline-scroll').scrollLeft > 500`, `backward range extension ${index + 1}`);
  const extended = await evaluate(`(() => ({ firstDate:document.querySelector('#copal-timeline-modal .copal-day-row span').title, scrollLeft:document.querySelector('#copal-timeline-modal .copal-timeline-scroll').scrollLeft }))()`);
  assert(extended.scrollLeft > 500, 'prepend did not compensate scroll anchor'); firstDate = extended.firstDate;
  await new Promise((resolve) => setTimeout(resolve, 100));
}

const crowded = await evaluate(`(() => { const rows=[...document.querySelectorAll('#copal-timeline-modal .copal-track[data-lanes]')].sort((a,b) => Number(b.dataset.lanes)-Number(a.dataset.lanes)); const row=rows[0]; return row ? { id:row.dataset.trackId, lanes:Number(row.dataset.lanes), expanded:row.classList.contains('expanded'), overlap:!!row.querySelector('.copal-track-overlap') } : null; })()`);
assert(crowded?.lanes >= 1);
if (crowded.lanes > 1) {
  await evaluate(`document.querySelector('#copal-timeline-modal .copal-track[data-track-id=${JSON.stringify(crowded.id)}] .copal-track-overlap').click()`);
  await waitFor(`document.querySelector('#copal-timeline-modal .copal-track[data-track-id=${JSON.stringify(crowded.id)}]').classList.contains('expanded') !== ${crowded.expanded}`, 'nested lane toggle');
}

const blockedGesture = await evaluate(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event.fuzzy'); return node ? { id:node.dataset.taskId, handles:node.querySelectorAll('.copal-resize-handle').length } : null; })()`);
assert(blockedGesture, 'No fuzzy event available for manipulation gating');
assert.equal(blockedGesture.handles, 0);
checkpoint('timeline parity and blocked gesture ready');
await evaluate(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(blockedGesture.id)}]').click()`);
await waitFor("document.querySelector('#copal-event-editor-modal:not(.hidden) .copal-event-form')", 'fuzzy event editor path');
await evaluate("document.querySelector('#copal-event-editor-modal .close-btn').click()");
await waitFor("document.getElementById('copal-event-editor-modal').classList.contains('hidden')", 'fuzzy editor close');

await evaluate("[...document.querySelectorAll('#copal-timeline-modal .copal-timeline-controls button')].find((node) => node.getAttribute('aria-label') === 'Center today').click()");
const todayValue = migrated.today || new Date().toISOString().slice(0, 10);
const hardEvent = migratedEvents
  .filter((event) => /^\d{4}-\d{2}-\d{2}$/.test(event.startDate) && /^\d{4}-\d{2}-\d{2}$/.test(event.dueDate) && !migrated.tracks.find((track) => track.id === event.trackId)?.special)
  .sort((a, b) => Math.abs(new Date(`${a.startDate}T12:00:00`) - new Date(`${todayValue}T12:00:00`)) - Math.abs(new Date(`${b.startDate}T12:00:00`) - new Date(`${todayValue}T12:00:00`)))[0];
assert(hardEvent, 'No hard-date event available for manipulation');
await waitFor(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]')`, 'hard-date event chip');
const manipulationRow = await evaluate(`(() => { const row=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]').closest('.copal-track'); return { id:row.dataset.trackId, expanded:row.classList.contains('expanded'), overlap:!!row.querySelector('.copal-track-overlap') }; })()`);
if (manipulationRow.overlap && !manipulationRow.expanded) {
  await evaluate(`document.querySelector('#copal-timeline-modal .copal-track[data-track-id=${JSON.stringify(manipulationRow.id)}] .copal-track-overlap').click()`);
  await waitFor(`document.querySelector('#copal-timeline-modal .copal-track[data-track-id=${JSON.stringify(manipulationRow.id)}]').classList.contains('expanded')`, 'manipulation track expansion');
}
await evaluate(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]').scrollIntoView({ inline:'center', block:'center' })`);
await new Promise((resolve) => setTimeout(resolve, 150));
let chip = await evaluate(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); const r=node.getBoundingClientRect(); return { x:r.left+r.width/2, y:r.top+r.height/2, right:r.right-3, width:r.width }; })()`);
const dayWidth = zoomed.dayWidth;
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:chip.x, y:chip.y, button:'left', buttons:1, clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseMoved', x:chip.x + dayWidth, y:chip.y, button:'left', buttons:1 });
await waitFor("!document.querySelector('#copal-timeline-modal .copal-resize-guide').hidden", 'cancelled move preview');
await command('Input.dispatchKeyEvent', { type:'keyDown', key:'Escape', code:'Escape' });
await command('Input.dispatchKeyEvent', { type:'keyUp', key:'Escape', code:'Escape' });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:chip.x + dayWidth, y:chip.y, button:'left', buttons:0, clickCount:1 });
await waitFor("document.querySelector('#copal-timeline-modal .copal-resize-guide').hidden", 'cancelled move rollback');
const cancelledEvent = (await jsonRequest('/api/copal/planning?workspace=default')).tracks.flatMap((track) => track.tasks || []).find((event) => event.id === hardEvent.id);
assert.equal(cancelledEvent.startDate, hardEvent.startDate);
assert.equal(cancelledEvent.dueDate, hardEvent.dueDate);
checkpoint('cancelled gesture preserved data');
await evaluate(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]').scrollIntoView({ inline:'center', block:'center' })`);
await new Promise((resolve) => setTimeout(resolve, 100));
chip = await evaluate(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); const r=node.getBoundingClientRect(); return { x:r.left+r.width/2, y:r.top+r.height/2, right:r.right-3, width:r.width }; })()`);
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:chip.x, y:chip.y, button:'left', buttons:1, clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseMoved', x:chip.x + dayWidth * 2, y:chip.y, button:'left', buttons:1 });
const moveGuide = await evaluate(`(() => { const guide=document.querySelector('#copal-timeline-modal .copal-resize-guide'); return { visible:!guide.hidden, label:guide.querySelector('span').textContent, color:getComputedStyle(guide).backgroundColor }; })()`);
assert(moveGuide.visible && /^\d{4}-\d{2}-\d{2}$/.test(moveGuide.label));
assert.notEqual(moveGuide.color, 'rgb(250, 204, 21)');
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:chip.x + dayWidth * 2, y:chip.y, button:'left', buttons:0, clickCount:1 });
await waitForPlanning((data) => data.tracks.flatMap((track) => track.tasks || []).find((event) => event.id === hardEvent.id)?.startDate !== hardEvent.startDate, 'event move save');
const movedPlanning = await jsonRequest('/api/copal/planning?workspace=default');
const movedEvent = movedPlanning.tracks.flatMap((track) => track.tasks || []).find((event) => event.id === hardEvent.id);
assert.equal(movedEvent.startDate, shiftDay(hardEvent.startDate, 2));
assert.equal(movedEvent.dueDate, shiftDay(hardEvent.dueDate, 2));

await waitFor(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); return node?.getAttribute('aria-label').includes(${JSON.stringify(movedEvent.startDate)}) && !node.classList.contains('saving'); })()`, 'moved event rerender');
await evaluate(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}] .copal-resize-right').scrollIntoView({ inline:'center', block:'nearest' })`);
await new Promise((resolve) => setTimeout(resolve, 100));
chip = await evaluate(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); const r=node.getBoundingClientRect(); return { x:r.right-3, y:r.top+r.height/2 }; })()`);
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:chip.x, y:chip.y, button:'left', buttons:1, clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseMoved', x:chip.x + dayWidth, y:chip.y, button:'left', buttons:1 });
await waitFor("!document.querySelector('#copal-timeline-modal .copal-resize-guide').hidden && document.querySelector('#copal-timeline-modal .copal-resize-guide span').textContent.length === 10", 'resize date guide');
const resizeGuide = await evaluate(`document.querySelector('#copal-timeline-modal .copal-resize-guide span').textContent`);
assert(/^\d{4}-\d{2}-\d{2}$/.test(resizeGuide));
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:chip.x + dayWidth, y:chip.y, button:'left', buttons:0, clickCount:1 });
await waitForPlanning((data) => data.tracks.flatMap((track) => track.tasks || []).find((event) => event.id === hardEvent.id)?.dueDate !== movedEvent.dueDate, 'event resize save');
const resizedPlanning = await jsonRequest('/api/copal/planning?workspace=default');
const resizedEvent = resizedPlanning.tracks.flatMap((track) => track.tasks || []).find((event) => event.id === hardEvent.id);
assert.equal(resizedEvent.dueDate, shiftDay(movedEvent.dueDate, 1));
checkpoint('move and resize committed');
await waitFor(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); return node?.getAttribute('aria-label').includes(${JSON.stringify(resizedEvent.dueDate)}) && !node.classList.contains('saving'); })()`, 'resized event rerender');
await evaluate(`document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]').scrollIntoView({ inline:'center', block:'center' })`);
await new Promise((resolve) => setTimeout(resolve, 100));

chip = await evaluate(`(() => { const node=document.querySelector('#copal-timeline-modal .copal-event[data-task-id=${JSON.stringify(hardEvent.id)}]'); const r=node.getBoundingClientRect(); return { x:r.left+r.width/2, y:r.top+r.height/2 }; })()`);
await command('Input.dispatchMouseEvent', { type:'mousePressed', x:chip.x, y:chip.y, button:'left', buttons:1, clickCount:1 });
await command('Input.dispatchMouseEvent', { type:'mouseReleased', x:chip.x, y:chip.y, button:'left', buttons:0, clickCount:1 });
await waitFor("document.querySelector('#copal-event-editor-modal:not(.hidden) .copal-event-form')", 'native rich event editor');
results.editor = await evaluate(`(() => ({ dialog:document.querySelector('#copal-event-editor-modal') instanceof HTMLDialogElement, fields:[...document.querySelectorAll('#copal-event-editor-modal .copal-form-field > span')].map((node)=>node.textContent.trim()), stages:!!document.querySelector('#copal-event-editor-modal .copal-stage-editor'), movable:getComputedStyle(document.querySelector('#copal-event-editor-modal .copal-modal-content')).position }))()`);
assert.equal(results.editor.dialog, false);
for (const field of ['Title','Description','Main track','Also on (shared)','Priority','Status','Tags (comma separated)']) assert(results.editor.fields.includes(field), `missing editor field ${field}`);
assert(results.editor.stages);
await screenshot('rich-event-native-window');

const eventTitle = `QOL ${Date.now().toString(36)}`;
await evaluate(`(() => { const input=document.querySelector('#copal-event-editor-modal input[aria-label="Event title"]'); input.value=${JSON.stringify(eventTitle)}; input.dispatchEvent(new Event('input',{bubbles:true})); [...document.querySelectorAll('#copal-event-editor-modal button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitForPlanning((data) => data.tracks.flatMap((track) => track.tasks || []).some((event) => event.id === hardEvent.id && event.title === eventTitle), 'rich event save');
await waitFor("document.querySelector('#copal-event-editor-modal .copal-workspace-status')?.textContent.includes('Revision')", 'rich event save settled');

await evaluate("document.querySelector('#copal-event-editor-modal .close-btn').click()");
await waitFor("document.getElementById('copal-event-editor-modal').classList.contains('hidden')", 'event editor close');
await evaluate("document.querySelector('#copal-timeline-modal .copal-track-edit').click()");
await waitFor("document.querySelector('#copal-track-editor-modal:not(.hidden) .copal-track-form')", 'native track editor');
const editedTrackId = await evaluate("document.querySelector('#copal-timeline-modal .copal-track[data-track-id]').dataset.trackId");
const currentTrackIcon = (await jsonRequest('/api/copal/planning?workspace=default')).tracks.find((track) => track.id === editedTrackId)?.icon;
const trackEmoji = currentTrackIcon === '🦕' ? '🐉' : '🦕';
await evaluate(`(() => { const input=document.querySelector('#copal-track-editor-modal input[aria-label="Track emoji or Unicode"]'); input.value=${JSON.stringify(trackEmoji)}; input.dispatchEvent(new Event('input',{bubbles:true})); [...document.querySelectorAll('#copal-track-editor-modal button')].find((node)=>node.textContent.trim()==='Save').click(); })()`);
await waitForPlanning((data) => data.tracks.find((track) => track.id === editedTrackId)?.icon === trackEmoji, 'track emoji save');
results.tracks = { editedTrackId, emoji:trackEmoji, palette:await evaluate("document.querySelectorAll('#copal-track-editor-modal .copal-color').length") };
checkpoint('rich editor and track editor ready');

results.timeline = { before:timelineBefore, zoomed, firstDateAfterExtensions:firstDate, crowded, blockedGesture, cancelledGesture:true, moveGuide, resizeGuide, movedEvent:{ id:movedEvent.id, startDate:movedEvent.startDate, dueDate:movedEvent.dueDate }, resizedDueDate:resizedEvent.dueDate };
await screenshot('timeline-qol-parity');

results.labels = await evaluate(`(() => ({ rail:document.getElementById('rail-tasks').getAttribute('aria-label'), tool:document.querySelector('#tool-tasks-btn .grow').textContent.trim(), copal:document.querySelector('[data-copal-view=todo] .grow').textContent.trim() }))()`);
assert.deepEqual(results.labels, { rail:'Clanker Tasks', tool:'Clanker Tasks', copal:'Meatbag Tasks' });

await command('Page.navigate', { url:`${base}/copal/calendar?acceptance=${Date.now()}` });
await waitFor("location.pathname === '/calendar'", 'retired Copal Calendar redirect');
await waitFor("document.getElementById('calendar-modal') && !document.getElementById('calendar-modal').classList.contains('hidden')", 'native Calendar', 45_000);
const nativeEvents = await jsonRequest('/api/calendar/events?start=2025-01-01&end=2028-12-31');
const projected = (nativeEvents.events || []).filter((event) => event.calendar === 'Copal · default' && event.location === 'Copal');
assert(projected.length > 0);
assert.equal(new Set(projected.map((event) => event.uid)).size, projected.length);
results.calendar = { projected:projected.length, nativeModal:true };
checkpoint('calendar projection ready');

const persistedDesktopShell = await evaluate(`(() => {
  const layout=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default'));
  return { left:layout.left.open, right:layout.right.open };
})()`);
await command('Emulation.setDeviceMetricsOverride', { width:1024, height:768, deviceScaleFactor:1, mobile:false });
await navigate(`/copal/notes?doc=${encodeURIComponent(acceptance.id)}&compact=${Date.now()}`);
await waitFor("document.querySelector('#copal-notes-modal:not(.hidden) .cm-editor')", 'compact Notes shell');
assert.equal(await evaluate("!!document.querySelector('#copal-notes-modal .copal-notes-sidebar')"), false);
await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace[data-drawer=\"right\"] .copal-notes-sidebar') && document.querySelector('#copal-notes-modal .copal-shell-scrim')", 'compact right drawer');
results.notes.compactShell = await evaluate(`(() => {
  const shell=document.querySelector('#copal-notes-modal .copal-notes-workspace');
  const sidebar=shell.querySelector('.copal-notes-sidebar').getBoundingClientRect();
  return {
    drawer:shell.dataset.drawer,
    sidebar:[Math.round(sidebar.width),Math.round(sidebar.height)],
    oneRightControl:document.querySelectorAll('#copal-notes-right-sidebar-toggle').length===1,
    noPageOverflow:document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
  };
})()`);
assert.equal(results.notes.compactShell.drawer, 'right');
assert(results.notes.compactShell.sidebar[0] > 0 && results.notes.compactShell.sidebar[1] > 0);
assert(results.notes.compactShell.oneRightControl && results.notes.compactShell.noPageOverflow);
await screenshot('notes-compact-right-drawer');
await pressEscape();
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.drawer === 'none' && !document.querySelector('#copal-notes-modal .copal-notes-sidebar')", 'compact drawer Escape close');
assert.equal(await notesVisible(), true, 'Notes window must survive drawer Escape');
await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace[data-drawer=\"right\"] .copal-notes-sidebar')", 'compact drawer reopen for scrim test');
await evaluate("document.querySelector('#copal-notes-modal .copal-shell-scrim').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.drawer === 'none' && !document.querySelector('#copal-notes-modal .copal-notes-sidebar')", 'compact drawer close');
assert.deepEqual(await evaluate(`(() => { const layout=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default')); return { left:layout.left.open, right:layout.right.open }; })()`), persistedDesktopShell);

await command('Emulation.setDeviceMetricsOverride', { width:900, height:620, deviceScaleFactor:1, mobile:false });
await command('Emulation.setPageScaleFactor', { pageScaleFactor:2 });
await navigate(`/copal/notes?doc=${encodeURIComponent(acceptance.id)}&zoom=${Date.now()}`);
await waitFor("document.querySelector('#copal-notes-modal:not(.hidden) .cm-editor')", 'Notes at 200 percent zoom');
await command('Emulation.setEmulatedMedia', { features:[{ name:'prefers-reduced-motion', value:'reduce' }] });
results.notes.zoom200 = await evaluate(`(() => { const modal=document.querySelector('#copal-notes-modal .copal-modal-content').getBoundingClientRect(); const header=document.querySelector('#copal-notes-modal .copal-workspace-header').getBoundingClientRect(); const active=document.activeElement; return { modal:[Math.round(modal.width),Math.round(modal.height)], headerVisible:header.top>=0 && header.bottom<=innerHeight, editorVisible:!!document.querySelector('#copal-notes-modal .cm-editor'), focused:!!active }; })()`);
assert(results.notes.zoom200.headerVisible && results.notes.zoom200.editorVisible && results.notes.zoom200.focused);
assert.equal(await evaluate("matchMedia('(prefers-reduced-motion: reduce)').matches"), true);
results.notes.reducedMotion = true;
await screenshot('notes-zoom-200');
await command('Emulation.setEmulatedMedia', { features:[] });
await command('Emulation.setPageScaleFactor', { pageScaleFactor:1 });

await command('Emulation.setDeviceMetricsOverride', { width:390, height:844, deviceScaleFactor:3, mobile:true });
await navigate(`/copal/notes?doc=${encodeURIComponent(acceptance.id)}&mobile=${Date.now()}`);
await waitFor("document.querySelector('#copal-notes-modal:not(.hidden) .copal-notes-workspace')", 'mobile Notes');
await waitFor("!document.querySelector('#copal-notes-modal .copal-notes-explorer') && !document.querySelector('#copal-notes-modal .copal-notes-sidebar')", 'mobile drawers initially closed');
await evaluate("document.getElementById('copal-notes-left-sidebar-toggle').focus(); document.getElementById('copal-notes-left-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace[data-drawer=\"left\"] .copal-notes-explorer') && document.querySelector('#copal-notes-modal .copal-shell-scrim')", 'mobile file drawer open');
await screenshot('notes-mobile-left-drawer');
await evaluate("document.getElementById('copal-notes-right-sidebar-toggle').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace[data-drawer=\"right\"] .copal-notes-sidebar') && !document.querySelector('#copal-notes-modal .copal-notes-explorer')", 'mobile single-drawer switch');
await screenshot('notes-mobile-right-drawer');
await evaluate("document.querySelector('#copal-notes-modal .copal-shell-scrim').click()");
await waitFor("document.querySelector('#copal-notes-modal .copal-notes-workspace').dataset.drawer === 'none' && !document.querySelector('#copal-notes-modal .copal-notes-sidebar')", 'mobile drawer closed');
results.mobile = await evaluate(`(() => {
  const root=document.getElementById('copal-notes-modal');
  const box=root.querySelector('.copal-modal-content').getBoundingClientRect();
  const left=document.getElementById('copal-notes-left-sidebar-toggle').getBoundingClientRect();
  const right=document.getElementById('copal-notes-right-sidebar-toggle').getBoundingClientRect();
  const tabScroll=root.querySelector('.copal-note-tab-scroll');
  const layout=JSON.parse(localStorage.getItem('odysseus-copal-notes-layout:default'));
  return {
    width:Math.round(box.width), height:Math.round(box.height), viewport:[innerWidth,innerHeight], dpr:devicePixelRatio,
    explorer:!!root.querySelector('.copal-notes-explorer'), rightSidebar:!!root.querySelector('.copal-notes-sidebar'),
    editor:!!root.querySelector('.cm-editor'), layout:root.querySelector('.copal-notes-workspace').dataset.previewLayout,
    controls:[Math.round(left.width),Math.round(left.height),Math.round(right.width),Math.round(right.height)],
    controlCounts:[document.querySelectorAll('#copal-notes-left-sidebar-toggle').length,document.querySelectorAll('#copal-notes-right-sidebar-toggle').length],
    tabs:root.querySelectorAll('.copal-note-tab').length,
    tabControlsOutsideScroll:![...root.querySelectorAll('.copal-tab-group-controls')].some((node)=>tabScroll.contains(node)),
    noPageOverflow:document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1,
    persisted:{ left:layout.left.open, right:layout.right.open },
  };
})()`);
assert.equal(results.mobile.width, 390);
assert.equal(results.mobile.height, 844);
assert.equal(results.mobile.explorer, false);
assert.equal(results.mobile.rightSidebar, false);
assert(results.mobile.editor);
assert.equal(results.mobile.dpr, 3);
assert(results.mobile.controls.every((value) => value >= 44));
assert.deepEqual(results.mobile.controlCounts, [1,1]);
assert(results.mobile.tabs >= 2 && results.mobile.tabControlsOutsideScroll && results.mobile.noPageOverflow);
assert.deepEqual(results.mobile.persisted, persistedDesktopShell);
await screenshot('notes-mobile');
checkpoint('mobile acceptance ready');

assert.equal(exceptions.length, 0, `Browser exceptions: ${exceptions.join('\n')}`);
// "Memory provider unavailable" is expected in the isolated acceptance stack,
// which runs without the Frankenmemory provider; release runs (Slice 16) must
// exercise memory with the provider up instead of relying on this filter.
const relevantConsole = consoleMessages.filter((message) => !/favicon|ResizeObserver loop|^TTS: not available$|Memory provider unavailable \(HTTP 503\)/i.test(message));
assert.equal(relevantConsole.length, 0, `Browser warnings/errors: ${relevantConsole.join('\n')}`);
results.consoleMessages = relevantConsole;
fs.writeFileSync(path.join(outputDir, 'results.json'), JSON.stringify(results, null, 2));
console.log(JSON.stringify(results, null, 2));
socket.close();
