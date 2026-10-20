#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
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


test('S07 mounts real Imps openResource bytes before managed project creation', async () => {
  const page = `<!doctype html><html><body><div id="toast" aria-live="polite"></div><script>window.__fixtureReady = true;</script></body></html>`;
  const png = ONE_PIXEL;
  const created = new Map();
  let releaseA = null;
  let saveEntered = false;
  let releaseSave = null;
  let saveCountA = 0;
  const saveBodies = [];
  let filesImportCount = 0;
  const copyBodies = [];
  let copyBindFailures = 1;
  await withCopalBrowser({
    page,
    request: async (request, response) => {
      const pathname = new URL(request.url, 'http://fixture').pathname;
      if (!pathname.startsWith('/api/')) return false;
      if (pathname === '/api/test/save-entered') { response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({entered:saveEntered})); return true; }
      if (pathname === '/api/test/release-save') { releaseSave?.(); releaseSave = null; saveEntered = false; response.writeHead(204); response.end(); return true; }
      if (pathname === '/api/auth/status') { response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({username:'s07-imps'})); return true; }
      if (pathname === '/api/files-v1/roots') {
        response.writeHead(200, {'content-type':'application/json'});
        response.end(JSON.stringify({ policy_generation: 7, entries: [{
          ref: 'files-root-ref', provider: 'files', kind: 'provider_root', name: 'Gallery',
          capabilities: ['children', 'write'], revision: { kind: 'filesFingerprint', value: 'root-1' },
        }] })); return true;
      }
      if (pathname === '/api/files-v1/imports') {
        filesImportCount += 1;
        const chunks=[]; for await (const chunk of request) chunks.push(chunk);
        const metadata = Buffer.concat(chunks).toString();
        const operationId = metadata.match(/imps-copy:[A-Za-z0-9:_-]+/)?.[0] || '';
        const itemId = `${operationId}:asset`;
        response.writeHead(200, {'content-type':'application/json'});
        response.end(JSON.stringify({ operation_id:operationId, generation:7, state:'complete', items:[{
          item_id:itemId, outcome:'committed', resource_ref:'files-copy-ref',
          resource_key:'resource-copy-stable', revision:{ kind:'contentDigest', value:'copy-digest' },
        }] })); return true;
      }
      if (pathname === '/api/files-v1/open-resource') {
        const chunks=[]; for await (const chunk of request) chunks.push(chunk);
        const body = JSON.parse(Buffer.concat(chunks).toString());
        if (body.resource_ref === 'files-copy-ref') {
          response.writeHead(200, {'content-type':'application/json'});
          response.end(JSON.stringify({ target:{app:'imps'}, resource:{ ref:'files-copy-ref', provider:'files', kind:'image', revision:{kind:'contentDigest',value:'copy-digest'}, capabilities:['open','read'] }, payload:{ provider:'files', resource_id:'image:copy-1', filename:'edited.png' } })); return true;
        }
        const ref = body.resource_ref;
        const suffix = String(ref).endsWith('-a') ? 'a' : 'b';
        if (suffix === 'a') await new Promise(resolve => { releaseA = resolve; setTimeout(resolve, 250); });
        response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({target:{app:'imps'},resource:{ref:`imps-image-${suffix}`,name:`fixture-${suffix}.png`,provider:'files',kind:'file'},payload:{provider:'files',resource_id:`image:fixture-${suffix}`,filename:`fixture-${suffix}.png`}})); return true;
      }
      if (pathname === '/api/files-v1/content/imps-image-a' || pathname === '/api/files-v1/content/imps-image-b') { response.writeHead(200, {'content-type':'image/png'}); response.end(png); return true; }
      if (pathname.startsWith('/api/imps/projects/for-image/')) {
        const suffix = pathname.endsWith('fixture-a') ? 'a' : 'b';
        const prior = created.get(suffix);
        response.writeHead(200, {'content-type':'application/json'});
        const savedState = prior && suffix === 'b' ? {
          ...prior.state,
          activeLayerId: 77,
          layers: [{ ...prior.state.layers[0], id:77, name:'Saved red layer', isBase:true,
            dataUrl:'data:image/svg+xml,%3Csvg xmlns="http://www.w3.org/2000/svg" width="1" height="1"%3E%3Crect width="1" height="1" fill="red"/%3E%3C/svg%3E' }],
        } : prior?.state;
        response.end(JSON.stringify({project: prior ? { id:`project-fixture-${suffix}`, project_revision:1, expected_image_revision:prior.expected_image_revision, name:suffix === 'b' ? 'Saved B project' : `fixture-${suffix}`, state:savedState } : null})); return true;
      }
      if (pathname === '/api/imps/projects') { const chunks=[]; for await (const chunk of request) chunks.push(chunk); const body=JSON.parse(Buffer.concat(chunks).toString()); const suffix=body.resource_id.endsWith('-a')?'a':'b'; created.set(suffix, body); response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({id:`project-fixture-${suffix}`,project_revision:1,expected_image_revision:body.expected_image_revision,image:{provider:'files',resource_id:body.resource_id}})); return true; }
      if (pathname === '/api/imps/projects/project-fixture-b/save-copy') { const chunks=[]; for await (const chunk of request) chunks.push(chunk); const body=JSON.parse(Buffer.concat(chunks).toString()); copyBodies.push(body); if (copyBindFailures > 0) { copyBindFailures -= 1; response.writeHead(503, {'content-type':'application/json'}); response.end(JSON.stringify({detail:'bind unavailable'})); return true; } response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({project_id:'project-copy-1',project_revision:1,image:{provider:'files',resource_id:'image:copy-1'},refresh_receipt:{image_revision:'copy-digest'}})); return true; }
      if (pathname.endsWith('/save')) { const chunks=[]; for await (const chunk of request) chunks.push(chunk); const body=JSON.parse(Buffer.concat(chunks).toString()); const isA = pathname.includes('project-fixture-a'); if (isA) { saveBodies.push(body); saveCountA += 1; if (saveCountA === 1) { saveEntered = true; await new Promise(resolve => { releaseSave = resolve; }); } } const nextRevision = Number(body.expected_project_revision || 1) + 1; response.writeHead(200, {'content-type':'application/json'}); response.end(JSON.stringify({project_revision:nextRevision,refresh_receipt:{image_revision:body.new_image_revision,image:{revision:body.new_image_revision}}})); return true; }
      response.writeHead(404); response.end(); return true;
    },
  }, async ({ evaluate, until }) => {
    await until('window.__fixtureReady');
    await evaluate(`Promise.all([import('/static/js/imps.js'), import('/static/js/editor/state.js')]).then(([editor, state]) => { window.__openResource = editor.openResource; window.__impsModuleClose = editor.closeEditor; window.__editorState = state.state; window.__impsImportError = null; window.__saveSettled = 0; window.addEventListener('imps-managed-save-settled', () => { window.__saveSettled += 1; }); }).catch(error => { window.__impsImportError = String(error.stack || error); })`);
    await until('window.__openResource || window.__impsImportError');
    assert.equal(await evaluate('window.__impsImportError || null'), null);
    await evaluate('window.__openResource("files://imps-image-b")');
    await until('window.__openResource && document.querySelector("#imps-editor-container")');
    assert.equal(await evaluate('window.__impsEditLive'), true);
    const expectedHash = `sha256:${createHash('sha256').update(png).digest('hex')}`;
    assert.equal(created.get('b')?.expected_image_revision, expectedHash, JSON.stringify([...created]));
    assert.equal(await evaluate('window.__editorState.layers.length > 0'), true);
    await evaluate(`Promise.resolve().then(async () => {
      const editor = await import('/static/js/imps.js');
      window.__exportToGallery = editor.exportToGallery;
      await window.__exportToGallery();
    })`);
    assert.equal(filesImportCount, 1);
    assert.equal(copyBodies.length, 1);
    await evaluate('window.__editorState.pendingSaveCopy != null');
    await evaluate('window.__exportRetry = import("/static/js/imps.js").then(module => module.retryPendingSaveCopy())');
    await evaluate('window.__exportRetry');
    assert.equal(copyBodies.length, 2);
    assert.equal(copyBodies[0].operation_key, copyBodies[1].operation_key);
    assert.equal(await evaluate('window.__editorState.projectId'), 'project-copy-1');
    assert.equal(await evaluate('window.__editorState.managedResourceId'), 'image:copy-1');
    assert.equal(await evaluate('window.__editorState.imgWidth === 1 && window.__editorState.imgHeight === 1'), true);
    const sameSave = await evaluate(`(async () => {
      const originalToBlob = HTMLCanvasElement.prototype.toBlob;
      window.__holdBlob = true;
      window.__releaseBlob = null;
      HTMLCanvasElement.prototype.toBlob = function(callback, mime, quality) {
        if (!window.__holdBlob) return originalToBlob.call(this, callback, mime, quality);
        window.__releaseBlob = () => { window.__holdBlob = false; originalToBlob.call(this, callback, mime, quality); };
      };
      await window.__openResource('files://imps-image-a');
      document.querySelector('#ge-save')?.click();
      await new Promise(resolve => setTimeout(resolve, 30));
      const next = window.__openResource('files://imps-image-b');
      await next;
      window.__releaseBlob();
      while (window.__saveSettled < 1) await new Promise(resolve => setTimeout(resolve, 5));
      HTMLCanvasElement.prototype.toBlob = originalToBlob;
      return (await (await fetch('/api/test/save-entered')).json()).entered;
    })()`);
    assert.equal(await evaluate('window.__editorState.managedResourceId'), 'image:fixture-b');
    assert.equal(await evaluate('window.__editorState.projectId'), 'project-fixture-b');
    assert.equal(await evaluate('window.__editorState.layers[0].name'), 'Saved red layer');
    assert.equal(await evaluate('(async () => (await (await fetch("/api/test/save-entered")).json()).entered)()'), false);
    await evaluate(`(async () => {
      await window.__openResource('files://imps-image-a');
      document.querySelector('#ge-save')?.click();
      while (!(await (await fetch('/api/test/save-entered')).json()).entered) await new Promise(resolve => setTimeout(resolve, 5));
      document.querySelector('#ge-add-layer')?.click();
      window.__editorState.draftName = 'newer draft during save';
      await fetch('/api/test/release-save', { method:'POST' });
      while (window.__saveSettled < 2) await new Promise(resolve => setTimeout(resolve, 5));
      await new Promise(resolve => setTimeout(resolve, 200));
      while (document.querySelector('#ge-save')?.disabled) await new Promise(resolve => setTimeout(resolve, 5));
      document.querySelector('#ge-save')?.click();
      while (window.__saveSettled < 3) await new Promise(resolve => setTimeout(resolve, 5));
      return { layers: window.__editorState.layers.length, name: window.__editorState.draftName, settled: window.__saveSettled, error: window.__impsLastSaveError };
    })()`);
    assert.equal(await evaluate('window.__editorState.managedResourceId'), 'image:fixture-a');
    assert.equal(await evaluate('window.__editorState.projectRevision'), 3, JSON.stringify({ saveBodies }));
    assert.equal(await evaluate('window.__editorState.draftName'), 'newer draft during save');
    assert.equal(saveBodies[0].expected_project_revision, 1);
    assert.equal(saveBodies[1].expected_project_revision, 2);
    assert.equal(saveBodies[1].name, 'newer draft during save');
    assert.equal(await evaluate('window.__editorState.layers.length >= 2'), true);
    await evaluate(`(async () => {
      const next = window.__openResource('files://imps-image-b');
      await next;
    })()`);
    assert.equal(await evaluate('window.__editorState.managedResourceId'), 'image:fixture-b');
    assert.equal(await evaluate('window.__editorState.projectId'), 'project-fixture-b');
    assert.equal(await evaluate('window.__editorState.projectRevision'), 1);
    assert.equal(await evaluate('window.__editorState.layers[0].name'), 'Saved red layer');
    assert.equal(await evaluate('window.__editorState.layers[0].ctx.getImageData(0, 0, 1, 1).data[0] > 200'), true);
    await evaluate(`(async () => {
      window.__impsAllowCloseEditor = true;
      const first = window.__openResource('files://imps-image-a').catch(() => null);
      const second = window.__openResource('files://imps-image-b');
      await second;
      await first;
    })()`);
    assert.equal(await evaluate('window.__editorState.editorOpen'), true);
    assert.equal(await evaluate('window.__editorState.managedResourceId'), 'image:fixture-b');
    const pendingClose = await evaluate(`(async () => {
      const pending = window.__openResource('files://imps-image-a').catch(error => String(error.message));
      await new Promise(resolve => setTimeout(resolve, 20));
      window.__impsAllowCloseEditor = true;
      const closed = window.__impsModuleClose ? window.__impsModuleClose() : false;
      return { closed, error: await pending, open: window.__editorState.editorOpen };
    })()`);
    assert.equal(pendingClose.closed, true);
    assert.equal(pendingClose.open, false);
  });
});
