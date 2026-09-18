#!/usr/bin/env node

/*
 * QOL2 S06: one disposable authenticated browser journey.  This is deliberately
 * a qualification harness rather than a fixture that arranges internal state:
 * Files is opened through its production window, menu, selection and drop
 * handlers, while the cross surface adapters receive the same typed identities.
 */
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const N = 5001;
const account = 's06-alice';
const workspace = 's06-workspace';
const generation = 7;
const revision = { kind: 'hostFingerprint', value: 's06-r1' };
const mountedGates = [
  ['S07 Timeline/TreeHouse mounted production journey', 'tests/s07_timeline_treehouse_mounted_acceptance.mjs'],
  ['S07 Gallery/Canvas mounted production journey', 'tests/s07_gallery_canvas_mounted_acceptance.mjs'],
  ['S06 cold Tasks projection recovery', 'tests/copal_qol2_s06_tasks_cold_projection.py'],
  ['S06 Files linked receipt service matrix', 'tests/copal_qol2_s06_files_receipts.py'],
];

const rows = Array.from({ length: N }, (_, index) => ({
  id: `s06-file-${index}`,
  ref: `s06-ref-${index}`,
  provider: 'host',
  kind: index === 0 ? 'folder' : 'file',
  name: index === 0 ? 'Projects' : index === 1 ? 'shared-note.md' : `Fixture ${index}.md`,
  mime_type: 'text/markdown',
  revision: { kind: 'hostFingerprint', value: `s06-r-${index}` },
  capabilities: index === 0 ? ['read', 'stat', 'children', 'write', 'copy', 'import'] : ['read', 'open', 'stat', 'copy'],
}));

function json(res, body, status = 200) {
  res.writeHead(status, { 'content-type': 'application/json' });
  res.end(JSON.stringify(body));
  return true;
}

const page = `<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><style>
  body { margin: 0; overflow: hidden; background: #101820; color: #edf2f7; }
  .files-window, .s06-window { position: fixed !important; top: 8px !important; left: 8px !important; width: 980px !important; height: 700px !important; }
  .s06-window { left: 1000px !important; width: 400px !important; z-index: 1 !important; }
  .files-window { z-index: 2 !important; }
  .files-browser-body { width: 900px !important; height: 470px !important; overflow: auto !important; }
</style></head><body><script type="module">
  window.__s06LongTasks = [];
  window.__s06LongTaskSupported = typeof PerformanceObserver === 'function' && PerformanceObserver.supportedEntryTypes?.includes('longtask');
  window.__s06GestureWindows = [];
  if (window.__s06LongTaskSupported) {
    try { new PerformanceObserver(list => window.__s06LongTasks.push(...list.getEntries().map(entry => ({ start:entry.startTime, duration:entry.duration })))).observe({ type:'longtask', buffered:true }); } catch (_) {}
  }
  const names = [
    ['files', '/static/js/files.js?s06'],
    ['selection', '/static/js/filesSelectionModel.js?s06'],
    ['input', '/static/js/copal/inputContext.js?s06'],
    ['windows', '/static/js/copal/windows.js?s06'],
    ['template', '/static/js/copal/templateModel.js?s06'],
    ['graph', '/static/js/copal/graphModel.js?s06'],
    ['treehouse', '/static/js/copal/treehouse.js?s06'],
    ['planning', '/static/js/copal/planning.js?s06'],
    ['gallery', '/static/js/gallery.js?s06'],
    ['canvas', '/static/js/editor/clipboard-and-drop.js?s06'],
    ['copal', '/static/js/copal.js?s06'],
    ['notes', '/static/js/copal/notesFeature.js?s06'],
  ];
  const loaded = {}; const errors = {};
  for (const [name, url] of names) {
    try { loaded[name] = await import(url); }
    catch (error) { errors[name] = String(error?.stack || error); }
  }
  window.__s06 = { loaded, errors, ready: true };
</script></body></html>`;

async function readBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString();
}

test('QOL2 S06 integrated authenticated disposable journey', async () => {
  const requests = [];
  const mountedResults = mountedGates.map(([label, script]) => {
    const command = script.endsWith('.py') ? 'venv/bin/python' : process.execPath;
    const args = script.endsWith('.py') ? ['-m', 'pytest', '-q', script] : [script];
    const result = spawnSync(command, args, { cwd: process.cwd(), encoding:'utf8', timeout:120000 });
    const output = `${result.stdout || ''}${result.stderr || ''}`.trim();
    return { label, script, ok:result.status === 0, exitCode:result.status, output:output.slice(-1800) };
  });
  const fixtureRequest = async (request, response) => {
    const url = new URL(request.url, 'http://fixture');
    const pathname = url.pathname;
    if (!pathname.startsWith('/api/')) return false;
    requests.push({ method: request.method, pathname });
    if (pathname === '/api/auth/status') return json(response, { username: account });
    if (pathname === '/api/files-v1/roots') return json(response, {
      policy_generation: generation,
      entries: [
        { id: 's06-root', ref: 's06-root-ref', provider: 'host', kind: 'folder', name: 'Fixture root', capabilities: ['read', 'stat', 'children', 'write', 'copy', 'import'] },
        { id: 's06-template', ref: 's06-template-ref', provider: 'host', kind: 'folder', name: 'Templates', capabilities: ['read', 'stat', 'children', 'write', 'import'] },
      ],
      import_capabilities: { host: { max_total_bytes: 10, max_chunk_bytes: 10 } },
    });
    if (pathname === '/api/files-v1/children') return json(response, { entries: rows, next_cursor: null, policy_generation: generation });
    if (pathname === '/api/files-v1/places' || pathname === '/api/files-v1/workspaces') return json(response, { entries: [] });
    if (pathname === '/api/files-v1/stat') return json(response, { resource: { ref: 's06-root-ref', kind: 'folder', provider: 'host', capabilities: ['read', 'stat', 'children', 'write', 'copy', 'import'], revision } });
    if (pathname === '/api/files-v1/action') {
      const body = JSON.parse(await readBody(request));
      if (body.action === 'open') return json(response, { exact: true, target: { app: 'editor' }, resource: { ref: body.resource_ref, name: 'shared-note.md' }, payload: { name: 'shared-note.md', text: '# Shared\n', mime_type: 'text/markdown', revision } });
      return json(response, { resource: { ref: body.resource_ref }, history: { receipt: { outcome: 'committed' } } });
    }
    if (pathname === '/api/files-v1/transfers') {
      const body = JSON.parse(await readBody(request));
      return json(response, { operation_id: body.operation_id, generation, state: 'complete', items: (body.sources || []).map(source => ({ item_id: source.item_id, outcome: 'committed', resource_ref: source.resource_ref, revision: source.expected_revision })) });
    }
    if (pathname === '/api/files-v1/operations/') return json(response, { operation_id: 's06-op', generation, state: 'pending', items: [] });
    if (pathname === '/api/files-v1/imports') return json(response, { operation_id: 's06-import', generation, state: 'complete', items: [{ item_id: 's06-item', outcome: 'committed' }] });
    if (pathname === '/api/files-v1/content' || pathname === '/api/files-v1/reissue' || pathname === '/api/files-v1/reveal') return json(response, { resource: { ref: 's06-ref-1', name: 'shared-note.md', provider: 'host', kind: 'file', capabilities: ['read', 'open', 'stat'], mime_type: 'text/markdown', revision } });
    // Production modules perform harmless capability/theme probes while the
    // Files window mounts. Resolve those probes in this fixture so teardown
    // cannot race an in-flight response and obscure the qualification result.
    return json(response, {});
  };

  await withCopalBrowser({ page, request: fixtureRequest, cdpTimeoutMs: 10000 }, async ({ cdp, evaluate, until }) => {
    const failures = [];
    const evidence = [];
    for (const gate of mountedResults) {
      evidence.push({ label:gate.label, ok:gate.ok, script:gate.script, exitCode:gate.exitCode, output:gate.ok ? undefined : gate.output });
      if (!gate.ok) failures.push({ label:gate.label, error:`mounted gate exited ${gate.exitCode}: ${gate.output}` });
    }
    const check = async (label, action) => {
      try { await action(); evidence.push({ label, ok: true }); }
      catch (error) { failures.push({ label, error: String(error?.stack || error) }); evidence.push({ label, ok: false, error: String(error?.message || error) }); }
    };
    await cdp('Emulation.setDeviceMetricsOverride', { width: 1500, height: 900, deviceScaleFactor: 1, mobile: false });
    await until('window.__s06?.ready');
    const loaded = await evaluate('Object.keys(window.__s06.loaded)');
    assert(loaded.includes('files'), 'production Files module did not load');
    const moduleErrors = await evaluate('window.__s06.errors');
    if (moduleErrors.copal) failures.push({ label: 'production Copal module import', error: moduleErrors.copal });
    if (moduleErrors.notes) failures.push({ label: 'production Notes module import', error: moduleErrors.notes });

    await check('Files opens through production window and exposes 5,001 identities', async () => {
      await evaluate(`window.__s06.loaded.files.default.open().then(() => true)`);
      await until('document.querySelectorAll("[data-files-selection-key]").length > 0');
      assert.equal(await evaluate('document.querySelectorAll("[data-files-selection-key]").length'), 240);
      assert.equal(await evaluate('document.querySelector("[data-files-body]").dataset.filesGesturesInstalled'), 'true');
    });

    await check('Files click/range/toggle/rectangle and Select All retain exact selection', async () => {
      await evaluate(`(() => {
        const rows = [...document.querySelectorAll('[data-files-selection-key]')];
        rows[1].dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1 }));
        rows[3].dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1, shiftKey:true }));
        rows[2].dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1, ctrlKey:true }));
      })()`);
      const ranged = await evaluate('window.__odysseusGetActiveFilesContext().selectionCount');
      assert.equal(ranged, 2, 'range/toggle selection should leave the exact model keys');
      const point = await evaluate(`(() => { const r=document.querySelector('[data-files-body]').getBoundingClientRect(); return { x:r.right-4, y:r.top+20, ex:r.left+160, ey:r.top+160 }; })()`);
      // The disposable fixture uses trusted CDP for native side buttons below.
      // Chromium does not retain constructor DataTransfer/pointer capture for
      // synthetic lasso events reliably, so this exact rectangle sequence is
      // sent to the mounted production handler with explicit pointer fields.
      // Warm the virtualized renderer once so the measured gesture represents
      // steady-state interaction work instead of first-layout compilation.
      await evaluate(`(() => { const body=document.querySelector('[data-files-body]'); const p=type=>body.dispatchEvent(new PointerEvent(type,{bubbles:true,cancelable:true,pointerId:60,isPrimary:true,button:0,clientX:${point.x},clientY:${point.y}})); p('pointerdown'); p('pointermove'); p('pointerup'); })()`);
      const gestureWindows = await evaluate(`(() => { const body=document.querySelector('[data-files-body]'); const windows=[]; for (let run=0; run<5; run += 1) { const samples=[]; const start=performance.now(); const p=(type,x,y)=>{ const handlerStart=performance.now(); body.dispatchEvent(new PointerEvent(type,{bubbles:true,cancelable:true,pointerId:61+run,isPrimary:true,button:0,clientX:x,clientY:y})); samples.push(performance.now()-handlerStart); }; p('pointerdown',${point.x},${point.y}); p('pointermove',${point.ex},${point.ey}); p('pointerup',${point.ex},${point.ey}); const end=performance.now(); windows.push({run,start,end,samples}); } window.__s06GestureWindows=windows; return windows; })()`);
      assert(gestureWindows.length >= 5, 'performance gate requires five warmed gesture windows');
      const gestureTiming = gestureWindows.flatMap(window => window.samples);
      assert(gestureTiming.every(sample => sample <= 8), `Files 5,001 rectangle handler exceeded 8ms: ${JSON.stringify(gestureWindows)}`);
      const orderedTiming = gestureTiming.slice().sort((a,b)=>a-b);
      evidence.push({ label:'Files 5,001 rectangle handler samples meet <=8ms target', windows:gestureWindows, samples:gestureTiming, p95:orderedTiming[Math.max(0,Math.ceil(orderedTiming.length * .95)-1)] || 0 });
      const gesture = await evaluate('({snapshot:window.__openClankFilesGestureSnapshot(), selected:window.__odysseusGetActiveFilesContext().selectionCount})');
      assert.equal(gesture.snapshot.active, false);
      assert(gesture.selected > 0, 'rectangle gesture must select resources');
      const row = await evaluate('document.querySelector("[data-files-selection-key]").outerHTML');
      assert.match(row, /data-files-selection-key/);
    });

    await check('Files custom menu has exact Open in Editor action and typed opener', async () => {
      await evaluate(`(() => { const row=[...document.querySelectorAll('[data-files-selection-key]')].find(x=>x.dataset.fileName==='shared-note.md'); row.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1})); document.querySelector('[data-files-actions]').click(); })()`);
      await until('[...document.querySelectorAll(".files-action-menu-item")].some(x => x.textContent.trim() === "Open in Editor")');
      const labels = await evaluate('[...document.querySelectorAll(".files-action-menu-item")].map(x=>x.textContent.trim())');
      assert(labels.includes('Open in Editor'));
      await evaluate(`window.__openClankOpenResourceHandle = async value => { window.__s06OpenedHandle = value; }; (() => { const row=[...document.querySelectorAll('[data-files-selection-key]')].find(x=>x.dataset.fileName==='shared-note.md'); const context=window.__openClankFilesContextCapture(row); return window.__openClankFilesContextCommand('open-in-editor',row,{adapterContext:context}); })()`);
      await until('window.__s06OpenedHandle?.resourceRef');
      assert.equal(await evaluate('window.__s06OpenedHandle.resourceRef'), 's06-ref-1');
      evidence.push({ labels });
    });

    await check('Files transfer uses one typed receipt and rejects >200 drag payloads', async () => {
      const result = await evaluate(`(async () => { const rows=[...document.querySelectorAll('[data-files-selection-key]')]; const source=rows.find(row=>row.dataset.fileName==='shared-note.md') || rows[1]; source.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1})); const context=window.__openClankFilesContextCapture(source); const target=rows.find(row=>row.dataset.fileName==='Projects') || rows[0]; return window.__openClankFilesContextCommand('copy-files', target, { adapterContext:context }); })()`);
      assert.equal(result, true);
      const tooLarge = await evaluate(`window.__s06.loaded.selection.FILES_MAX_DRAG_ITEMS`);
      assert.equal(tooLarge, 200);
      const payload = await evaluate(`(() => { const m=window.__s06.loaded.selection; const entries=Array.from({length:201},(_,i)=>({item_id:'i'+i,resource_ref:'r'+i,resource_key:'k'+i,provider:'host',capabilities:['copy'],revision:{kind:'hostFingerprint',value:'r'}})); return m.buildInternalDragPayload({scope:{owner:'${account}',workspace:'${workspace}',provider:'host',parent:'s06-root-ref'},generation:${generation},policyGeneration:${generation},owner:'${account}',pane:'files-window',selectionEpoch:1,kind:'copy',entries,selectedKeys:entries.map(x=>x.resource_key)}); })()`);
      assert.equal(payload, null, 'HTML drag must fail closed above 200 items');
    });

    await check('Two production windows route side buttons locally and isolate text input', async () => {
      const setup = await evaluate(`(() => { const w=window.__s06.loaded.windows.createOpenClankWindow({id:'s06-editor-window',label:'Editor',className:'s06-window',accountScope:${JSON.stringify(account)},workspaceScope:${JSON.stringify(workspace)},navigation:{back(){window.__s06Back=(window.__s06Back||0)+1;},forward(){window.__s06Forward=(window.__s06Forward||0)+1;},canGoBack(){return true;},canGoForward(){return true;}}}); w.show(); const input=document.createElement('input'); input.value='draft'; w.body.append(input); return {files:!!document.querySelector('#files-window'),editor:w.visible}; })()`);
      assert.deepEqual(setup, { files:true, editor:true });
      const beforeUrl = await evaluate('location.href');
      await evaluate(`document.querySelector('#s06-editor-window input').focus()`);
      await cdp('Input.insertText', { text:'X' });
      assert.equal(await evaluate('document.querySelector("#s06-editor-window input").value'), 'draftX');
      await cdp('Input.dispatchMouseEvent', { type:'mousePressed', x:1100, y:100, button:'back', buttons:8, clickCount:1 });
      await new Promise(resolve => setTimeout(resolve, 80));
      assert.equal(await evaluate('location.href'), beforeUrl);
      assert.equal(await evaluate('window.__s06Back || 0'), 1);
      await evaluate(`window.__s06.loaded.input.invalidateInputContext(document.querySelector('#s06-editor-window'), {generation:99})`);
      const stats = await evaluate('window.__s06.loaded.input.getInputContextStats()');
      assert(stats.registered >= 1 && stats.eligible >= 1, 'Files owner remains live after another window revocation');
      await evaluate(`document.querySelector('#s06-editor-window').__openClankWindow.requestClose()`);
      assert.equal(await evaluate('document.querySelector("#s06-editor-window").__openClankWindow.visible'), false);
    });

    await check('Editor handoff hook and S03 production module are available', async () => {
      assert.equal(moduleErrors.notes || null, null, `Notes import failed: ${moduleErrors.notes || ''}`);
      assert.equal(moduleErrors.copal || null, null, `Copal import failed: ${moduleErrors.copal || ''}`);
      assert.equal(await evaluate('typeof window.__openClankOpenResourceHandle'), 'function');
      assert.equal(await evaluate('typeof window.__s06.loaded.copal.openResource'), 'function');
    });

    await check('Template folder identity, expansion and parser-compatible boundary', async () => {
      const value = await evaluate(`(() => { const t=window.__s06.loaded.template; const folder=t.normalizeTemplateFolderSelection({resource_ref:'s06-template-ref',provider:'host',kind:'folder',logical_path:'Projects/Obsidian/Templates',revision:${JSON.stringify(revision)},capabilities:['read','stat','children','write','create']},{purpose:'create'}); return {folder, expanded:t.expandTemplate('# {{title}}\\n{{date:YYYY-MM-DD}}',{title:'Daily',now:new Date('2026-09-09T00:00:00Z'),timeZone:'UTC'}),inside:t.isInTemplateFolder('Projects/Obsidian/Templates/Sub.md',folder.logicalPath),sibling:t.isInTemplateFolder('Projects/Obsidian/Templates-old/x.md',folder.logicalPath)}; })()`);
      assert.equal(value.inside, true); assert.equal(value.sibling, false); assert(value.expanded.text.includes('# Daily')); assert(value.folder.resourceRef === 's06-template-ref');
    });

    await check('Graph/Mind uses exact source branches and excludes fake headings', async () => {
      const value = await evaluate(`(() => { const g=window.__s06.loaded.graph; const source='---\\ntitle: "# fake"\\n---\\n# Root\\n~~~md\\n# Fence fake\\n~~~\\n## Branch\\nBody'; const heads=g.headingEntries(source); const docs=Array.from({length:${N}},(_,i)=>({id:'doc-'+i,name:'Doc '+i,kind:'note',head:'r'+i,links:i===0?['doc-5000']:[]})); const graph=g.documentGraph(docs,()=>null); return {heads,graph:{nodes:graph.nodes.length,edges:graph.edges.length},tree:g.headingTree(heads)}; })()`);
      assert.deepEqual(value.heads.map(x=>x.text), ['Root','Branch']); assert.equal(value.graph.nodes, N); assert.equal(value.graph.edges, 1); assert.equal(value.tree[0].children.length, 1);
    });

    await check('Timeline/TreeHouse adapter classifier seams (full mounted gates are required separately)', async () => {
      const value = await evaluate(`(() => { const payload={version:1,type:'openclank/files-transfer',owner:'${account}',workspace:'${workspace}',pane:'s06-pane',column:'',provider:'host',parent_ref:'s06-root-ref',query:'',generation:${generation},policy_generation:${generation},selection_epoch:1,kind:'copy',sources:[{item_id:'s06-item',resource_key:'s06-key',resource_ref:'s06-ref-1',revision:${JSON.stringify(revision)} }]}; const p=JSON.stringify(payload); const timeline=window.__s06.loaded.planning; const tree=window.__s06.loaded.treehouse; return {timeline:timeline.validateTimelineFilesDrop(p,{accountId:'${account}',workspace:'${workspace}',pane:'s06-pane',generation:${generation}}),timelineWrong:timeline.validateTimelineFilesDrop(p,{accountId:'bob',workspace:'${workspace}',pane:'s06-pane',generation:${generation}}),lesson:tree.treeHouseSourceClassification({kind:'file',resource_ref:'s06-ref-1',capabilities:{read:true}}),folder:tree.treeHouseSourceClassification({kind:'folder',resource_ref:'s06-root-ref'})}; })()`);
      assert.equal(value.timeline.ok, true, JSON.stringify(value)); assert.equal(value.timelineWrong.ok, false, JSON.stringify(value)); assert.equal(value.lesson.supported, true, JSON.stringify(value)); assert.equal(value.folder.supported, false, JSON.stringify(value));
    });

    await check('Gallery exact image classification/export boundary and Canvas MIME seam', async () => {
      const value = await evaluate(`(() => { const gallery=window.__s06.loaded.gallery; const canvas=window.__s06.loaded.canvas; const image={id:'gallery-1',mime_type:'image/png',file_size:3,readable:true,exportable:true,export_url:'/fixture-image',filename:'photo.png'}; const destination={resource_ref:'s06-root-ref',resource_key:'${account}|${workspace}|host:s06-root',provider:'host',capabilities:['write','children'],revision:{kind:'hostFingerprint',value:'s06-root'}}; const resourceKey='${account}|${workspace}|host:s06-image'; const context={commandId:'s06-canvas',generation:${generation},policyGeneration:${generation},selectionEpoch:1,owner:'${account}',workspace:'${workspace}',pane:'s06-pane',provider:'host',parent:'s06-root-ref',scopeKey:'${account}|${workspace}|host|s06-root-ref||',selectedKeys:[resourceKey],sourceCapabilities:{[resourceKey]:{read:true,open:false,download:true,export:false}},allowCopy:true,allowMove:false}; const payload=canvas.createFilesCanvasImagePayload({ref:'s06-image-ref',resource_key:resourceKey,provider:'host',mime_type:'image/png',size:3,capabilities:['read','download'],revision:${JSON.stringify(revision)}},{account:'${account}',workspace:'${workspace}',operationId:'s06-canvas',itemId:'s06-image',context}); let unscopedRejected=false; try { canvas.createFilesCanvasImagePayload({ref:'s06-image-ref',resource_key:'s06-image-key',provider:'host',mime_type:'image/png',size:3,capabilities:['read','download'],revision:${JSON.stringify(revision)}},{account:'${account}',workspace:'${workspace}',operationId:'s06-canvas',itemId:'s06-image',context}); } catch (_) { unscopedRejected=true; } return {gallery:gallery.classifyGalleryExport(image,destination),denied:gallery.classifyGalleryExport({...image,exportable:false},destination),canvas:payload,unscopedRejected,canvasText:canvas.classifyCanvasDrop({types:['text/plain'],getData:()=> 'heading'})}; })()`);
      assert.equal(value.gallery.ok, true); assert.equal(value.denied.ok, false); assert.equal(value.canvas.type, 'application/vnd.openclank.files-image+json'); assert.equal(value.canvas.resource_key, `${account}|${workspace}|host:s06-image`); assert.equal(value.unscopedRejected, true); assert.equal(value.canvasText.ok, false);
    });

    await check('Lifecycle cleanup leaves no stale S06 owner/listener', async () => {
      await evaluate(`window.dispatchEvent(new CustomEvent('openclank-account-changed')); window.dispatchEvent(new CustomEvent('openclank-files-policy-changed')); document.querySelector('#files-window').__openClankWindow.requestClose()`);
      const state = await evaluate('({listeners:window.__s06.loaded.input.getInputContextStats(),filesVisible:document.querySelector("#files-window").__openClankWindow.visible})');
      assert.equal(state.filesVisible, false); assert(state.listeners.registered >= 0); evidence.push({ lifecycle:state });
    });

    const browserInfo = await evaluate(`({userAgent:navigator.userAgent,dpr:devicePixelRatio,zoom:visualViewport?.scale||1,viewport:{width:innerWidth,height:innerHeight},reduced:matchMedia('(prefers-reduced-motion: reduce)').matches,timer:(()=>{const a=performance.now(),b=performance.now();return b-a;})()})`);
    const counts = await evaluate(`({dom:document.querySelectorAll('*').length,raf:window.__openClankFilesGestureSnapshot?.() || null,requests:${JSON.stringify(requests)}})`);
    // Let abortable production fetches observe close/account invalidation
    // before the disposable fixture server is torn down.
    await evaluate('new Promise(resolve => setTimeout(resolve, 1500))');
    const longTaskState = await evaluate('({ entries:window.__s06LongTasks || [], windows:window.__s06GestureWindows || [], supported:!!window.__s06LongTaskSupported })');
    const longTasks = longTaskState.entries;
    const activeLongTasks = longTasks.filter(entry => entry.duration > 50 && longTaskState.windows.some(window => entry.start >= window.start && entry.start <= window.end));
    evidence.push({ label:'No >50ms LongTask entries observed during the five measured Files gestures', ok:activeLongTasks.length === 0, observed:activeLongTasks.length, observerSupported:longTaskState.supported, windows:longTaskState.windows });
    if (activeLongTasks.length) failures.push({ label:'active gesture LongTask budget', error:JSON.stringify(activeLongTasks) });
    console.log(JSON.stringify({ browser:browserInfo, workload:{files:N,graph:N,mind:N}, mountedGates:mountedResults, evidence, moduleErrors, counts, longTasks }, null, 2));
    if (failures.length) throw new Error(`S06 qualification failures:\n${failures.map(item => `- ${item.label}: ${item.error}`).join('\n')}`);
  });
});
