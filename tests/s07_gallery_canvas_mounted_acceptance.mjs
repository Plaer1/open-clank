#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const ONE_PIXEL = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=', 'base64');

test('S07 mounts Files image source and Canvas drop handlers with strict context', async () => {
  const page = `<!doctype html><html><body><script type="module">
    Promise.all([
      import('/static/js/files.js'),
      import('/static/js/editor/clipboard-and-drop.js'),
      import('/static/js/editor/state.js'),
      import('/static/js/filesFacadeClient.js'),
      import('/static/js/custom-context-menu.js'),
    ]).then(async ([files, drop, stateModule, clientModule, contextMenu]) => {
      window.__filesModule = files;
      contextMenu.initCustomContextMenu();
      await files.default.open();
      const canvas = document.createElement('div');
      canvas.id = 'fixture-canvas';
      document.body.append(canvas);
      stateModule.state.editorOpen = true;
      stateModule.state.container = canvas;
      stateModule.state.layers = [];
      window.__canvasLayers = 0;
      window.__canvasProvenance = null;
      window.__dropAdapter = drop.wireClipboardAndDrop({
        container: canvas,
        filesClient: clientModule.filesFacadeClient,
        handleImportedImage: (_image, options) => {
          window.__canvasLayers += 1;
          window.__canvasProvenance = options?.provenance || null;
        },
        uiModule: { showToast(message) { window.__canvasToast = String(message); } },
        context: window.__dropContext || null,
        getContext: () => window.__dropContext || null,
      });
      window.__createFilesCanvasPayload = drop.createFilesCanvasImagePayload;
      window.__fixtureReady = true;
    }).catch(error => { window.__fixtureError = String(error.stack || error); });
  </script></body></html>`;
  let canvasOpenCalls = 0;
  let transferRequests = 0;
  await withCopalBrowser({
    page,
    request: async (request, response) => {
      const pathname = new URL(request.url, 'http://fixture').pathname;
      if (!pathname.startsWith('/api/')) return false;
      if (pathname === '/api/auth/status') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ username: 's07-files-canvas' })); return true;
      }
      if (pathname === '/api/files-v1/roots') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({
          entries: [{ id: 'root', ref: 'rr-root', provider: 'host', kind: 'folder', name: 'Fixture', revision:{kind:'hostFingerprint',value:'root-1'}, capabilities: ['children', 'write'] }],
          import_capabilities: { host: { max_total_bytes: 64 * 1024 * 1024, max_chunk_bytes: 512 * 1024 } },
          policy_generation: 7,
        })); return true;
      }
      if (pathname === '/api/files-v1/children') {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ entries: [{
          id: 'destination-folder', ref: 'rr-destination-folder', provider: 'host', kind: 'folder', name: 'Destination',
          revision: { kind: 'hostFingerprint', value: 'destination-1' }, capabilities: ['children', 'write'],
        }, {
          id: 'image-1', ref: 'rr-image-1', provider: 'host', kind: 'file', name: 'same-name.png',
          mime_type: 'image/png', revision: { kind: 'hostFingerprint', value: 'fp-1' },
          capabilities: ['read', 'open', 'download'], size: ONE_PIXEL.length,
        }], next_cursor: null })); return true;
      }
      if (pathname === '/api/files-v1/open-resource') {
        canvasOpenCalls += 1;
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ resource: {
          id: 'image-1', ref: 'rr-image-1-current', provider: 'host', kind: 'file', name: 'same-name.png',
          mime_type: 'image/png', revision: { kind: 'hostFingerprint', value: 'fp-1' },
          capabilities: ['read', 'open', 'download'],
        } })); return true;
      }
      if (pathname === '/api/files-v1/content/rr-image-1-current') {
        response.writeHead(200, { 'content-type': 'image/png', 'content-length': ONE_PIXEL.length });
        response.end(ONE_PIXEL); return true;
      }
      if (pathname === '/api/files-v1/transfers') {
        transferRequests += 1;
        response.writeHead(500, { 'content-type':'application/json' }); response.end(JSON.stringify({ error:'unexpected Files transfer' })); return true;
      }
      if (pathname === '/api/files-v1/places' || pathname === '/api/files-v1/workspaces') {
        response.writeHead(200, { 'content-type': 'application/json' }); response.end(JSON.stringify({ entries: [] })); return true;
      }
      response.writeHead(404); response.end(); return true;
    },
  }, async ({ evaluate, until }) => {
    await until('window.__fixtureReady || window.__fixtureError');
    assert.equal(await evaluate('window.__fixtureError || null'), null);
    await evaluate(`(() => {
      const data = new DataTransfer();
      const payload = window.__dropCreatePayload = window.__createFilesCanvasPayload({
        ref:'rr-image-1', resource_key:'s07-files-canvas|default|host:image-1', provider:'host', mime_type:'image/png', size:68,
        revision:{ kind:'hostFingerprint', value:'fp-1' }, capabilities:['read', 'open', 'download'],
      }, { account:'s07-files-canvas', workspace:'default', operationId:'files-canvas-op', itemId:'files-canvas-item', context:{
        commandId:'files-canvas-op', generation:7, policyGeneration:7, selectionEpoch:1,
        owner:'s07-files-canvas', workspace:'default', pane:'files-window', provider:'host', parent:'rr-root',
        scopeKey:'s07-files-canvas|default|host|rr-root||', selectedKeys:['s07-files-canvas|default|host:image-1'],
        sourceCapabilities:{'s07-files-canvas|default|host:image-1':{read:true,open:true,download:true,export:false}}, allowCopy:true, allowMove:false,
      }});
      const canvasMime = 'application/vnd.openclank.files-image+json';
      data.setData(canvasMime, JSON.stringify(payload));
      window.__dropContext = payload.gesture_context;
      window.__canvasMime = data.getData(canvasMime);
      window.__canvasPayload = JSON.parse(window.__canvasMime);
      const drop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(drop, 'dataTransfer', { value:data });
      document.getElementById('fixture-canvas').dispatchEvent(drop);
    })()`);
    await new Promise(resolve => setTimeout(resolve, 800));
    assert.equal(await evaluate('window.__canvasLayers'), 1, await evaluate('JSON.stringify({ payload:window.__canvasPayload, toast:window.__canvasToast, classifier:window.__dropAdapter.classify({ dataTransfer: { types:["application/vnd.openclank.files-image+json"], getData:()=>window.__canvasMime } }) })'));
    assert.ok(await evaluate('window.__canvasPayload'));
    assert.equal(await evaluate('window.__canvasPayload.type'), 'application/vnd.openclank.files-image+json');
    assert.equal(await evaluate('window.__canvasPayload.account'), 's07-files-canvas');
    assert.equal(await evaluate('window.__canvasPayload.workspace'), 'default');
    assert.equal(await evaluate('window.__canvasPayload.gesture_context.selectionEpoch > 0'), true);
    assert.equal(await evaluate('window.__canvasLayers'), 1);
    const invalid = await evaluate(`(() => {
      const text = new DataTransfer();
      text.setData('text/plain', 'folder or code');
      const textDrop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(textDrop, 'dataTransfer', { value:text });
      document.getElementById('fixture-canvas').dispatchEvent(textDrop);
      const legacy = new DataTransfer();
      legacy.setData('application/x-openclank-files-image', window.__canvasMime);
      const legacyDrop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(legacyDrop, 'dataTransfer', { value:legacy });
      document.getElementById('fixture-canvas').dispatchEvent(legacyDrop);
      const stale = new DataTransfer();
      const stalePayload = { ...window.__canvasPayload, operation_id:'stale-op', item_id:'stale-item', gesture_context:{ ...window.__canvasPayload.gesture_context, commandId:'stale-op', generation:99, policyGeneration:99 } };
      stale.setData('application/vnd.openclank.files-image+json', JSON.stringify(stalePayload));
      window.__dropContext = { ...window.__canvasPayload.gesture_context, generation:99, policyGeneration:99 };
      const staleDrop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(staleDrop, 'dataTransfer', { value:stale });
      document.getElementById('fixture-canvas').dispatchEvent(staleDrop);
      return { textPrevented:textDrop.defaultPrevented, legacyPrevented:legacyDrop.defaultPrevented, stalePrevented:staleDrop.defaultPrevented };
    })()`);
    assert.deepEqual(invalid, { textPrevented:true, legacyPrevented:true, stalePrevented:true });
    await new Promise(resolve => setTimeout(resolve, 100));
    assert.equal(await evaluate('window.__canvasLayers'), 1);
    assert.equal(canvasOpenCalls, 1);
    const produced = await evaluate(`(() => {
      let row = document.querySelector('[data-file-name="same-name.png"]');
      row.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1 }));
      row = document.querySelector('[data-file-name="same-name.png"]');
      row.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1, ctrlKey:true }));
      row = document.querySelector('[data-file-name="same-name.png"]');
      row.dispatchEvent(new MouseEvent('click', { bubbles:true, detail:1, ctrlKey:true }));
      row = document.querySelector('[data-file-name="same-name.png"]');
      const beforeSelection = row.getAttribute('aria-selected');
      const transfer = new DataTransfer();
      const event = new MouseEvent('dragstart', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:transfer });
      row.dispatchEvent(event);
      const value = transfer.getData('application/vnd.openclank.files-image+json');
      const plain = { value:value ? JSON.parse(value) : null, effectAllowed:transfer.effectAllowed, generic:transfer.getData('application/x-openclank-files+json') ? JSON.parse(transfer.getData('application/x-openclank-files+json')) : null };
      window.__dropContext = plain.value.gesture_context;
      const canvasDrop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(canvasDrop, 'dataTransfer', { value:transfer });
      document.getElementById('fixture-canvas').dispatchEvent(canvasDrop);
      const modifierTransfer = new DataTransfer();
      const modifierEvent = new MouseEvent('dragstart', { bubbles:true, cancelable:true, ...(/mac/i.test(navigator.platform) ? {altKey:true} : {ctrlKey:true}) });
      Object.defineProperty(modifierEvent, 'dataTransfer', { value:modifierTransfer });
      row.dispatchEvent(modifierEvent);
      plain.modifierKind = modifierTransfer.getData('application/x-openclank-files+json') ? JSON.parse(modifierTransfer.getData('application/x-openclank-files+json')).kind : null;
      row.dispatchEvent(new Event('dragend', { bubbles:true }));
      const filesTarget = document.querySelector('[data-file-name="Destination"]');
      const targetTransfer = new DataTransfer();
      const targetEvent = new MouseEvent('dragstart', { bubbles:true, cancelable:true });
      Object.defineProperty(targetEvent, 'dataTransfer', { value:targetTransfer });
      row.dispatchEvent(targetEvent);
      const drop = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(drop, 'dataTransfer', { value:targetTransfer });
      filesTarget?.dispatchEvent(drop);
      return { payload:plain.value, effectAllowed:plain.effectAllowed, generic:plain.generic, modifierKind:plain.modifierKind, filesTargetRejected:Boolean(filesTarget) && drop.defaultPrevented, draggable:row.draggable, beforeSelection, afterSelection:row.getAttribute('aria-selected'), defaultPrevented:event.defaultPrevented, types:[...transfer.types], html:row.outerHTML };
    })()`);
    assert.equal(produced?.payload?.type, 'application/vnd.openclank.files-image+json', JSON.stringify(produced));
    assert.equal(produced?.generic?.kind, 'move', JSON.stringify(produced));
    assert.equal(produced?.modifierKind, 'copy', JSON.stringify(produced));
    assert.equal(produced?.filesTargetRejected, true, JSON.stringify(produced));
    assert.equal(transferRequests, 0);
      assert.equal(produced?.payload?.gesture_context?.allowMove, false, JSON.stringify(produced));
      assert.equal(produced?.payload?.gesture_context?.selectedKeys?.length, 1, JSON.stringify(produced));
    await new Promise(resolve => setTimeout(resolve, 800));
    assert.equal(await evaluate('window.__canvasLayers'), 2);
    assert.equal(await evaluate('window.__canvasProvenance.domain'), 'files');
    assert.equal(await evaluate('window.__canvasProvenance.resource_ref'), 'rr-image-1-current');
    await evaluate(`(() => {
      const row = document.querySelector('[data-file-name="same-name.png"]');
      row.dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:20, clientY:20 }));
    })()`);
    await until('document.querySelector("#openclank-context-menu")');
    assert.equal(await evaluate('document.querySelector("#openclank-context-menu").innerText.includes("Open in Canvas")'), true);
  });
});

test('S07 mounts Gallery export through the authorized Files receipt path', async () => {
  const page = `<!doctype html><html><body><script type="module">
    Promise.all([
      import('/static/js/gallery.js'),
      import('/static/js/filesFacadeClient.js'),
    ]).then(([gallery, client]) => {
      window.__galleryExport = gallery.exportGalleryImageToFiles;
      window.__requestGalleryExport = gallery.requestGalleryExport;
      window.__filesClient = client.filesFacadeClient;
      window.__odysseusGetActiveFilesContext = () => ({ accountId:'s07-gallery-export', workspace:'default', provider:'host', generation:7, policyGeneration:7 });
      window.__fixtureReady = true;
    }).catch(error => { window.__fixtureError = String(error.stack || error); });
  </script></body></html>`;
  const receipts = {
    'gallery-success': { operation_id:'gallery-success', generation:7, state:'complete', items:[{ item_id:'gallery-image-success', outcome:'committed', resource_ref:'dest-success-image' }] },
    'gallery-collision': { operation_id:'gallery-collision', generation:7, state:'partial', items:[{ item_id:'gallery-image-collision', outcome:'conflict' }] },
    'gallery-lost': { operation_id:'gallery-lost', generation:7, state:'complete', items:[{ item_id:'gallery-image-lost', outcome:'committed', resource_ref:'dest-lost-image' }] },
    'gallery-image-picker': { operation_id:'gallery-image-picker', generation:7, state:'complete', items:[{ item_id:'gallery-image-picker', outcome:'committed', resource_ref:'dest-picker-image' }] },
  };
  const importCalls = [];
  await withCopalBrowser({
    page,
    request: async (request, response) => {
      const pathname = new URL(request.url, 'http://fixture').pathname;
      if (!pathname.startsWith('/api/')) return false;
      if (pathname === '/api/auth/status') {
        response.writeHead(200, { 'content-type':'application/json' });
        response.end(JSON.stringify({ username:'s07-gallery-export' })); return true;
      }
      if (pathname === '/api/files-v1/roots') {
        response.writeHead(200, { 'content-type':'application/json' });
        response.end(JSON.stringify({ entries:[{ id:'dest-folder', ref:'dest-folder', provider:'host', kind:'folder', name:'Destination', capabilities:['write','children'], revision:{ kind:'hostFingerprint', value:'dest-1' } }], policy_generation:7 })); return true;
      }
      if (pathname === '/api/files-v1/children') {
        response.writeHead(200, { 'content-type':'application/json' });
        response.end(JSON.stringify({ entries:[], next_cursor:null, policy_generation:7 })); return true;
      }
      if (pathname === '/api/files-v1/stat') {
        response.writeHead(200, { 'content-type':'application/json' });
        response.end(JSON.stringify({ resource:{ ref:'dest-folder', resource_key:'s07-gallery-export|default|host:dest-folder', provider:'host', kind:'folder', capabilities:['write','children'], revision:{ kind:'hostFingerprint', value:'dest-1' } } })); return true;
      }
      if (pathname === '/api/gallery/image-source') {
        response.writeHead(200, { 'content-type':'image/png', 'content-length':ONE_PIXEL.length });
        response.end(ONE_PIXEL); return true;
      }
      if (pathname === '/api/files-v1/imports') {
        const chunks = [];
        for await (const chunk of request) chunks.push(chunk);
        const body = Buffer.concat(chunks).toString();
        const operation = Object.keys(receipts).find(value => body.includes(`gallery-${value.replace('gallery-', '')}`)) || (body.includes('gallery-success') ? 'gallery-success' : body.includes('gallery-collision') ? 'gallery-collision' : body.includes('gallery-image-picker') ? 'gallery-image-picker' : 'gallery-lost');
        importCalls.push(operation);
        if (operation === 'gallery-lost') { response.writeHead(503, { 'content-type':'application/json' }); response.end(JSON.stringify({ code:'provider_unavailable', message:'response lost' })); return true; }
        response.writeHead(200, { 'content-type':'application/json' }); response.end(JSON.stringify(receipts[operation])); return true;
      }
      if (pathname.startsWith('/api/files-v1/operations/')) {
        const operation = decodeURIComponent(pathname.split('/').pop());
        response.writeHead(200, { 'content-type':'application/json' }); response.end(JSON.stringify(receipts[operation] || { operation_id:operation, state:'pending', items:[] })); return true;
      }
      response.writeHead(404); response.end(); return true;
    },
  }, async ({ evaluate, until }) => {
    await until('window.__fixtureReady || window.__fixtureError');
    assert.equal(await evaluate('window.__fixtureError || null'), null);
    const run = async (operationId, imageId) => evaluate(`window.__galleryExport({ id:${JSON.stringify(imageId)}, mime_type:'image/png', file_size:${ONE_PIXEL.length}, readable:true, exportable:true, export_url:'/api/gallery/image-source', filename:'same-name.png' }, { resource_ref:'dest-folder', resource_key:'s07-gallery-export|default|host:dest-folder', provider:'host', capabilities:['write','children'], revision:{kind:'hostFingerprint',value:'dest-1'} }, { filesClient:window.__filesClient, operationId:${JSON.stringify(operationId)}, generation:7, account:'s07-gallery-export', workspace:'default', getContext:()=>({account:'s07-gallery-export',workspace:'default',provider:'host',generation:7,policyGeneration:7}) })`);
    const success = await run('gallery-success', 'image-success');
    assert.equal(success.status, 'committed');
    assert.equal(success.receipt.operation_id, 'gallery-success');
    assert.equal(success.receipt.items[0].item_id, 'gallery-image-success');
    const collision = await run('gallery-collision', 'image-collision');
    assert.equal(collision.status, 'conflict');
    assert.equal(collision.receipt.items[0].outcome, 'conflict');
    const lost = await run('gallery-lost', 'image-lost');
    assert.equal(lost.status, 'committed');
    assert.equal(lost.receipt.operation_id, 'gallery-lost');
    assert.deepEqual(await evaluate('window.__filesClient ? true : false'), true);
    const pickerRun = await evaluate(`(async () => {
      await window.__requestGalleryExport({ id:'image-picker', mime_type:'image/png', file_size:${ONE_PIXEL.length}, readable:true, exportable:true, export_url:'/api/gallery/image-source', filename:'same-name.png' });
      const dialog = document.querySelector('.copal-resource-picker');
      const root = dialog?.querySelector('[data-resource-picker-list] button');
      root?.click();
      await new Promise(resolve => setTimeout(resolve, 500));
      const choose = document.querySelector('[data-resource-picker-list] button.copal-btn.primary');
      choose?.click();
      await new Promise(resolve => setTimeout(resolve, 300));
      return { pickerClosed:!document.querySelector('.copal-resource-picker'), importCount:window.__filesClient ? true : false };
    })()`);
    assert.equal(pickerRun.pickerClosed, true, JSON.stringify(pickerRun));
  });
  assert.deepEqual(importCalls, ['gallery-success', 'gallery-collision', 'gallery-lost', 'gallery-image-picker']);
});
