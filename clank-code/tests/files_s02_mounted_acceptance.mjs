#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const entries = Array.from({ length: 5001 }, (_, index) => ({
  id: `file-${index}`, ref: `rr-file-${index}`, provider: 'host',
  kind: index === 0 ? 'folder' : 'file', name: `File ${index}`,
  capabilities: index === 0 ? ['children', 'copy', 'write', 'stat'] : index === 2 ? ['open', 'copy', 'stat', 'read', 'download'] : ['open', 'copy', 'stat'],
  mime_type: index === 2 ? 'image/png' : 'text/plain',
  ...(index === 2 ? { revision: { kind: 'hostFingerprint', value: 'fixture-image-2' } } : {}),
}));
const response = body => new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

test('S02 mounts production Files handlers with bounded 5,001 item windows', async () => {
  const page = `<!doctype html><html><head><style>
    body { overflow: hidden !important; }
    .modal.files-window { position: fixed !important; top: 0 !important; left: 0 !important; margin: 0 !important; }
    .files-browser-body { width: 800px !important; height: 400px !important; overflow: auto !important; scroll-behavior: auto !important; }
    .files-entry { width: 120px !important; }
  </style></head><body><script type="module">
    window.__rafCalls = 0;
    window.__filesBodyScrollListeners = 0;
    const addListener = EventTarget.prototype.addEventListener;
    EventTarget.prototype.addEventListener = function(type, listener, options) {
      if (type === 'scroll' && this?.getAttribute?.('data-files-body') !== null && this?.getAttribute?.('data-files-body') !== undefined) window.__filesBodyScrollListeners += 1;
      return addListener.call(this, type, listener, options);
    };
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = callback => { window.__rafCalls += 1; return raf(callback); };
    import('/static/js/custom-context-menu.js').then(({ initCustomContextMenu }) => {
      initCustomContextMenu();
      return import('/static/js/files.js');
    }).then(module => { window.__filesModule = module; });
  </script></body></html>`;
  let importRequests = 0;
  let transferRequests = 0;
  let multiTransferRequests = 0;
  let transferItemIds = [];
  let workspaceRequests = 0;
  let rootRequests = 0;
  let delayedRootReloads = 0;
  await withCopalBrowser({
    page,
    request: async (request, serverResponse) => {
      const pathname = new URL(request.url, 'http://fixture').pathname;
      if (!pathname.startsWith('/api/')) return false;
      if (pathname === '/api/auth/status') { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ username: 's02-fixture' })); return true; }
      if (pathname === '/api/files-v1/roots') {
        rootRequests += 1;
        if (rootRequests > 1) { delayedRootReloads += 1; await new Promise(resolve => setTimeout(resolve, 300)); }
        serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ entries: [{ id: 'root', ref: 'rr-root', provider: 'host', kind: 'folder', name: 'Fixture', capabilities: ['children', 'copy', 'write'] }], import_capabilities: { host: { max_total_bytes: 10, max_chunk_bytes: 10 } }, policy_generation: 4 })); return true;
      }
      if (pathname === '/api/files-v1/children') { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ entries, next_cursor: null })); return true; }
      if (pathname === '/api/files-v1/places' || pathname === '/api/files-v1/workspaces') { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ entries: [] })); return true; }
      if (pathname === '/api/files-v1/imports') {
        importRequests += 1;
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const text = Buffer.concat(chunks).toString();
        const itemId = text.match(/"item_id":"([^"]+)"/)?.[1] || '';
        serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ operation_id: `import-${importRequests}`, generation: 4, state: 'complete', items: [{ item_id: itemId, outcome: 'committed' }] })); return true;
      }
      if (pathname === '/api/files-v1/transfers') {
        transferRequests += 1;
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = JSON.parse(Buffer.concat(chunks).toString());
        transferItemIds.push(body.sources.map(source => source.item_id));
        if (body.sources.length > 1) multiTransferRequests += 1;
        const retry = transferRequests > 1 && body.sources.length === 1;
        const secondChunkFailure = body.sources.length > 1 && multiTransferRequests === 2;
        const items = body.sources.map((source, index) => ({ item_id: source.item_id, outcome: retry ? 'committed' : body.sources.length > 1 ? (secondChunkFailure ? (index === 0 ? 'conflict' : 'committed') : 'committed') : (index === 0 ? 'conflict' : 'denied') }));
        const state = items.every(item => ['committed', 'unchanged'].includes(item.outcome)) ? 'complete' : 'partial';
        serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ operation_id: body.operation_id, generation: 4, state, items })); return true;
      }
      if (pathname.startsWith('/api/files-v1/operations/')) { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ state: 'pending', items: [] })); return true; }
      if (pathname === '/api/files-v1/action') { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ resource: {} })); return true; }
      if (pathname === '/api/files-v1/workspace') { workspaceRequests += 1; serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ workspace: { id: 'workspace-fixture' } })); return true; }
      if (pathname === '/api/file-policy/workspaces/workspace-fixture/resolve') { serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify({ workspace: { id: 'workspace-fixture', path: '/fixture' } })); return true; }
      serverResponse.writeHead(404); serverResponse.end(); return true;
    },
  }, async ({ cdp, evaluate, until }) => {
    await cdp('Emulation.setDeviceMetricsOverride', { width:1200, height:1000, deviceScaleFactor:1, mobile:false });
    await until('window.__filesModule', 'Files module');
    await evaluate(`window.__filesModule.default.open().then(() => { window.__filesOpened = true; }).catch(error => { window.__filesError = String(error.stack || error); })`);
    await until('window.__filesOpened || window.__filesError', 'Files mount');
    assert.equal(await evaluate('window.__filesError || null'), null);
    await evaluate(`(() => { const body = document.querySelector('[data-files-body]'); Object.assign(body.style, { position:'fixed', top:'0px', left:'0px' }); })()`);
    assert.equal(await evaluate('document.querySelectorAll("[data-files-selection-key]").length'), 240);
    assert.equal(await evaluate('document.querySelector("[data-files-body]").dataset.filesGesturesInstalled'), 'true');
    assert.equal(await evaluate('window.__filesBodyScrollListeners'), 1, 'one owned body scroll listener');
    const rightClickLabels = async (selector, close = true) => {
      const point = await evaluate(`(() => { const rect = document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect(); return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }; })()`);
      await cdp('Input.dispatchMouseEvent', { type:'mouseMoved', x:point.x, y:point.y });
      await cdp('Input.dispatchMouseEvent', { type:'mousePressed', x:point.x, y:point.y, button:'right', clickCount:1 });
      await cdp('Input.dispatchMouseEvent', { type:'mouseReleased', x:point.x, y:point.y, button:'right', clickCount:1 });
      await until('document.querySelector("#openclank-context-menu")', 'Files context menu');
      const labels = await evaluate('[...document.querySelectorAll("#openclank-context-menu [data-command]")].map(node => node.textContent.trim())');
      if (close) await evaluate('window.openClankContextMenu?.close()');
      return labels;
    };
    await evaluate('window.__openClankOpenResourceHandle = async () => { window.__contextEditorOpen = true; };');
    const textMenu = await rightClickLabels('[data-file-name="File 1"]', false);
    assert.equal(textMenu.includes('Open in Editor'), true, 'real Files right-click exposes Editor for authorized text');
    assert.equal(textMenu.includes('Select all files'), true, 'real Files right-click exposes Files selection command');
    await evaluate('document.querySelector("#openclank-context-menu [data-command=\\"open-in-editor\\"]")?.click()');
    await until('window.__contextEditorOpen', 'context Editor command');
    const folderMenu = await rightClickLabels('[data-file-name="File 0"]', false);
    assert.equal(folderMenu.includes('Use as workspace'), true, 'real Files right-click exposes workspace only for capable folders');
    await evaluate('document.querySelector("#openclank-context-menu [data-command=\\"files-use-as-workspace\\"]")?.click()');
    await new Promise(resolve => setTimeout(resolve, 120));
    assert.equal(workspaceRequests, 1, 'folder workspace command executes through opaque authorization');
    assert.ok(delayedRootReloads >= 1, 'workspace handoff exercised a delayed policy refresh');
    const imageMenu = await rightClickLabels('[data-file-name="File 2"]');
    assert.equal(imageMenu.includes('Open in Canvas'), true, 'real Files right-click exposes Canvas for authorized images');
    assert.equal(imageMenu.includes('Open in Editor'), false, 'image right-click does not expose an unauthorized Editor command');
    await evaluate(`(() => { const body = document.querySelector('[data-files-body]'); body.scrollTop = 420; body.dispatchEvent(new Event('scroll')); })()`);
    await evaluate('new Promise(requestAnimationFrame)');
    const emptyPoint = await evaluate(`(() => { const body = document.querySelector('[data-files-body]').getBoundingClientRect(); const target = document.elementFromPoint(body.right - 4, body.top + 20); return { x: body.right - 4, y: body.top + 20, endX: body.left + 100, endY: body.bottom - 4, target: target?.className || target?.tagName, empty: !target?.closest('[data-files-selection-key]') }; })()`);
    assert.equal(emptyPoint.empty, true, 'lasso begins on empty content');
    await cdp('Input.dispatchMouseEvent', { type:'mouseMoved', x:emptyPoint.x, y:emptyPoint.y });
    await cdp('Input.dispatchMouseEvent', { type:'mousePressed', x:emptyPoint.x, y:emptyPoint.y, button:'left', clickCount:1 });
    const gestureStarted = await evaluate('window.__openClankFilesGestureSnapshot()');
    const startedAt = Date.now();
    for (let index = 0; index < 24; index += 1) {
      await cdp('Input.dispatchMouseEvent', { type:'mouseMoved', x:emptyPoint.endX, y:emptyPoint.endY });
      await new Promise(resolve => setTimeout(resolve, 12));
    }
    const scrolledDuringGesture = await evaluate('document.querySelector("[data-files-body]").scrollTop');
    const lassoDuring = await evaluate('window.__odysseusGetActiveFilesContext().selectionCount');
    await cdp('Input.dispatchMouseEvent', { type:'mouseReleased', x:emptyPoint.endX, y:emptyPoint.endY, button:'left', clickCount:1 });
    const gestureDuration = Date.now() - startedAt;
    const lasso = await evaluate('({ scrollTop:document.querySelector("[data-files-body]").scrollTop, selected:window.__odysseusGetActiveFilesContext().selectionCount, dom:document.querySelectorAll("[data-files-selection-key]").length, raf:window.__rafCalls })');
    assert.equal(lasso.dom, 240, 'lasso keeps the virtual DOM bounded');
    assert.ok(scrolledDuringGesture > 0, `trusted pointer lasso autoscrolled the viewport (during=${scrolledDuringGesture}, after=${lasso.scrollTop}, height=${await evaluate('document.querySelector("[data-files-body]").scrollHeight')}, client=${await evaluate('document.querySelector("[data-files-body]").clientHeight')}, raf=${lasso.raf})`);
    assert.ok(gestureStarted.active, `trusted pointer began rectangle (target=${emptyPoint.target}, point=${JSON.stringify(emptyPoint)}, viewport=${await evaluate('JSON.stringify({w:innerWidth,h:innerHeight})')})`);
    assert.ok(lassoDuring > 0 || lasso.selected > 0, `trusted lasso selected logical identities (during=${lassoDuring}, after=${lasso.selected})`);
    assert.ok(gestureDuration < 5000, `trusted input journey completed in ${gestureDuration}ms`);

    const modes = ['list', 'grid', 'details', 'columns', 'gallery'];
    for (const mode of modes) {
      await evaluate(`(async () => { const select = document.querySelector('.files-mode-select'); select.value = ${JSON.stringify(mode)}; select.dispatchEvent(new Event('change', { bubbles:true })); await new Promise(requestAnimationFrame); })()`);
      await evaluate('new Promise(requestAnimationFrame)');
      assert.equal(await evaluate('document.querySelectorAll("[data-files-selection-key]").length'), mode === 'columns' ? 240 : 240, `${mode} DOM window`);
    }

    await evaluate(`(() => {
      const body = document.querySelector('[data-files-body]');
      const row = body.querySelector('[data-files-selection-key]'); row.focus();
      row.dispatchEvent(new KeyboardEvent('keydown', { key:'End', bubbles:true, ctrlKey:false }));
      body.dispatchEvent(new KeyboardEvent('keydown', { key:'a', bubbles:true, ctrlKey:true }));
      const input = document.querySelector('.files-search-input'); input.value = 'native text'; input.focus(); input.setSelectionRange(0, 6);
      input.dispatchEvent(new KeyboardEvent('keydown', { key:'a', bubbles:true, ctrlKey:true }));
      return { selected: document.querySelectorAll('[aria-selected="true"]').length, nativeStart: input.selectionStart, nativeEnd: input.selectionEnd };
    })()`);
    const selection = await evaluate('(() => { const input = document.querySelector(".files-search-input"); return { selected: document.querySelectorAll("[aria-selected=\\"true\\"]").length, nativeStart: input.selectionStart, nativeEnd: input.selectionEnd }; })()');
    assert.ok(selection.selected > 0 && selection.selected <= 240, 'only the bounded visible window is selected in the DOM');
    assert.equal(await evaluate('window.__odysseusGetActiveFilesContext().selectionCount'), 5001, 'Ctrl/Cmd+A selects all loaded identities');
    assert.deepEqual([selection.nativeStart, selection.nativeEnd], [0, 6]);
    await evaluate(`(() => { const body = document.querySelector('[data-files-body]'); body.scrollTop = 0; body.dispatchEvent(new Event('scroll')); })()`);
    await evaluate('new Promise(requestAnimationFrame)');
    await evaluate(`(async () => {
      window.__openClankOpenResourceHandle = async () => { window.__editorOpen = true; };
      const row = document.querySelectorAll('[data-files-selection-key]')[1];
      const captured = window.__openClankFilesContextCapture(row);
      await window.__openClankFilesContextCommand('open-in-editor', row, { adapterContext: captured });
      const dt = new DataTransfer(); dt.items.add(new File(['fixture'], 'fixture.txt', { type:'text/plain' }));
      const event = new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:dt });
      window.__dropInfo = { files: event.dataTransfer.files.length, items: event.dataTransfer.items.length };
      document.querySelector('[data-files-selection-key]').dispatchEvent(event);
    })()`);
    await until('window.__editorOpen === true', 'exact Editor handoff');
    await until('document.body.innerText.includes("imported") || document.body.innerText.includes("rejected")', 'import receipt');
    assert.equal(await evaluate('window.__dropInfo.files + window.__dropInfo.items'), 2);
    assert.equal(importRequests, 1, `OS import uses one child operation; status=${await evaluate('document.body.innerText.slice(-250)')}`);
    await evaluate(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['1234567890'], 'exact.txt')); document.querySelector('[data-files-selection-key]').dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:dt })); })()`);
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(importRequests, 2, 'a file at max_chunk_bytes is accepted');
    await evaluate(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['12345678901'], 'oversize.txt')); document.querySelector('[data-files-selection-key]').dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:dt })); })()`);
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.equal(importRequests, 2, 'a file over max_chunk_bytes is rejected before multipart');
    await evaluate(`(() => {
      const rows = [...document.querySelectorAll('[data-files-selection-key]')];
      const source = rows.find(row => row.dataset.fileName === 'File 1') || rows[1];
      let folder = rows.find(row => row.dataset.fileName === 'File 0') || rows[0];
      source.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1 }));
      const captured = window.__openClankFilesContextCapture(source);
      folder = [...document.querySelectorAll('[data-files-selection-key]')].find(row => row.dataset.fileName === 'File 0') || document.querySelector('[data-files-selection-key]');
      const dt = new DataTransfer();
      dt.setData('application/x-openclank-files+json', JSON.stringify({
        version:1, type:'openclank/files-transfer', kind:'copy', owner:captured.filesOwner,
        workspace:captured.filesWorkspace, pane:captured.filesPane, column:'', provider:'host', parent_ref:'rr-root', query:'',
        generation:captured.filesGeneration, policy_generation:captured.filesGeneration,
        selection_epoch:captured.filesEpoch,
        sources:[{ item_id:'fixture-item-1', resource_key:source.dataset.filesSelectionKey, resource_ref:source.dataset.resourceRef, revision:null }],
      }));
      const drop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(drop, 'dataTransfer', { value:dt });
      folder.dispatchEvent(drop);
    })()`);
    await new Promise(resolve => setTimeout(resolve, 180));
    assert.equal(transferRequests, 1, 'internal typed drop dispatches one batch request');
    assert.equal(await evaluate('document.querySelector(".files-retry-keep-both")?.textContent || ""'), 'Keep Both');
    await evaluate('document.querySelector(".files-retry-keep-both")?.click()');
    await new Promise(resolve => setTimeout(resolve, 180));
    assert.equal(transferRequests, 2, 'Keep Both retries only through the typed executor');
    assert.notDeepEqual(transferItemIds[0], transferItemIds[1], 'Keep Both uses a fresh child item identity');
    await evaluate(`(async () => {
      const mode = document.querySelector('.files-mode-select');
      mode.value = 'columns'; mode.dispatchEvent(new Event('change', { bubbles:true }));
      await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame);
      const firstPanel = document.querySelector('.files-column[data-column-index="0"]');
      let firstRow = firstPanel?.querySelector('[data-files-selection-key]');
      firstRow?.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1 }));
      firstRow = document.querySelector('.files-column[data-column-index="0"] [data-files-selection-key]');
      const firstCapture = window.__openClankFilesContextCapture(firstRow);
      await window.__openClankFilesContextCommand('select-all-files', firstRow, { adapterContext:firstCapture });
      window.__largeBeforeFolder = window.__odysseusGetActiveFilesContext().selectionCount;
      await new Promise(requestAnimationFrame);
      const folderRow = document.querySelector('.files-column[data-column-index="0"] [data-file-name="File 0"]');
      folderRow?.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1, ctrlKey:true }));
    })()`);
    await until('document.querySelectorAll(".files-column").length > 1', 'second Columns destination');
    await evaluate(`(async () => {
      const source = document.querySelector('.files-column[data-column-index="0"] [data-file-name="File 1"]');
      const captured = window.__openClankFilesContextCapture(source);
      window.__largeContextResult = await window.__openClankFilesContextCommand('copy-files', source, { adapterContext:captured });
      window.__largeContextDebug = { captured, result:window.__largeContextResult, status:[...document.querySelectorAll('.window-status')].map(node => node.textContent).at(-1) || '', source:source?.outerHTML || '' };
    })()`);
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(await evaluate('window.__largeBeforeFolder'), 5001, 'context Select All owns every loaded identity before chunking');
    assert.equal(multiTransferRequests, 2, `large context transfer dispatches bounded chunks then stops on second-chunk collision: ${JSON.stringify(await evaluate('window.__largeContextDebug'))}`);
    assert.equal(transferItemIds[2].length, 200, 'first large chunk has exactly 200 source IDs');
    assert.equal(transferItemIds[3].length, 200, 'second large chunk has exactly 200 source IDs');
    assert.equal(await evaluate('window.__largeContextResult'), false, 'partial chunk leaves the remaining selection for attention');
    await evaluate('new Promise(resolve => setTimeout(resolve, 300))');
    const settledRaf = await evaluate('window.__rafCalls');
    await evaluate('new Promise(resolve => setTimeout(resolve, 100))');
    assert.ok(settledRaf < 400, `bounded active frame scheduling (raf=${settledRaf})`);
    assert.deepEqual(await evaluate('window.__openClankFilesGestureSnapshot()'), { active:false, frame:0, autoFrame:0 }, 'gesture frame owner reaches zero');
  });
});

test('S02 fails closed when import byte capabilities are absent', async () => {
  let imports = 0;
  let rootCalls = 0;
  const page = `<!doctype html><html><body><script type="module">import('/static/js/files.js').then(async module => { await module.default.open(); window.__ready = true; });</script></body></html>`;
  await withCopalBrowser({
    page,
    request: async (request, serverResponse) => {
      const pathname = new URL(request.url, 'http://fixture').pathname;
      if (!pathname.startsWith('/api/')) return false;
      let body = {};
      if (pathname === '/api/auth/status') body = { username: 's02-no-cap-fixture' };
      else if (pathname === '/api/files-v1/roots') { rootCalls += 1; body = { entries: [{ id: 'root', ref: 'rr-root', provider: 'host', kind: 'folder', name: 'Fixture', capabilities: ['children', 'write'] }], ...(rootCalls > 1 ? { import_capabilities: { host: { max_total_bytes: 'bad', max_chunk_bytes: -1 } } } : {}), policy_generation: 0 }; }
      else if (pathname === '/api/files-v1/children') body = { entries: [{ id: 'folder', ref: 'rr-folder', provider: 'host', kind: 'folder', name: 'Folder', capabilities: ['children', 'write'] }], next_cursor: null };
      else if (pathname === '/api/files-v1/places' || pathname === '/api/files-v1/workspaces') body = { entries: [] };
      else if (pathname === '/api/files-v1/imports') { imports += 1; body = { state: 'complete', items: [] }; }
      else { serverResponse.writeHead(404); serverResponse.end(); return true; }
      serverResponse.writeHead(200, { 'content-type': 'application/json' }); serverResponse.end(JSON.stringify(body)); return true;
    },
  }, async ({ evaluate, until }) => {
    await until('window.__ready', 'Files mount without import caps');
    await evaluate(`(() => { const dt = new DataTransfer(); dt.items.add(new File(['x'], 'no-cap.txt')); document.querySelector('[data-files-selection-key]').dispatchEvent(new DragEvent('drop', { bubbles:true, cancelable:true, dataTransfer:dt })); })()`);
    await new Promise(resolve => setTimeout(resolve, 150));
    assert.ok(rootCalls >= 2, 'navigation and provider roots both exercised');
    assert.equal(imports, 0, 'missing import capabilities reject before multipart');
  });
});
