#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const artifactId = 'a'.repeat(32);
const sourceDigest = `sha256:${'a'.repeat(64)}`;
const itemDigest = `sha256:${'b'.repeat(64)}`;
let artifactServer;
let artifactPort;
let appOrigin = '';
let indexRequests = 0;
let delivered = false;
let statusRequests = 0;
let deleteRequests = 0;
let createRequests = 0;
let filesImportRequests = 0;
let filesReceiptRequests = 0;
let filesImportMetadata = null;
let failNextCreate = false;
let delayNextDelete = false;
let pendingDeleteResponse = null;

artifactServer = http.createServer((request, response) => {
  if (request.headers.host !== `${artifactId}.artifacts.localhost:${artifactPort}`) {
    response.writeHead(421); response.end('unknown host'); return;
  }
  if (request.url !== '/index.html') { response.writeHead(404); response.end(); return; }
  indexRequests += 1;
  if (indexRequests === 1) return; // Simulate a navigation that never fires iframe load/error.
  delivered = true;
  response.writeHead(200, {
    'content-type':'text/html; charset=utf-8',
    'content-security-policy':`default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors ${appOrigin}; object-src 'none'; base-uri 'none'`,
    'cache-control':'no-store',
  });
  response.end(`<!doctype html><meta charset="utf-8"><title>Fixture document</title><h1>Isolated document loaded</h1>
    <script>
      const channel = 'openclank.report.v1';
      const items = [
        { id:'question-1', digest:'${itemDigest}', value:{ state:'adopted', adoption_note:'Use this', rejection_note:'' } },
        { id:'question-2', digest:'sha256:${'c'.repeat(64)}', value:{ state:'rejected', adoption_note:'', rejection_note:'Does not fit' } },
      ];
      let sessionId = '', generation = 0, sourceDigest = '';
      window.addEventListener('message', event => {
        if (event.origin !== ${JSON.stringify(appOrigin)}) return;
        const data = event.data;
        if (!data || data.channel !== channel) return;
        if (data.type === 'host-ready') {
          sessionId = data.session_id; generation = data.generation; sourceDigest = data.source_digest;
          parent.postMessage({ channel, type:'register', session_id:sessionId, generation,
            report_id:'fixture-report', source_digest:sourceDigest, schema_version:1,
            items:[{ id:'question-1', digest:'${itemDigest}' }, { id:'question-2', digest:'sha256:${'c'.repeat(64)}' }] }, '*');
          parent.postMessage({ channel, type:'progress-save', session_id:sessionId, generation,
            report_id:'fixture-report', source_digest:sourceDigest, schema_version:1, items }, '*');
        } else if (data.type === 'progress-restore') {
          document.body.dataset.restored = String(data.progress.items.length);
        } else if (data.type === 'progress-import') {
          parent.postMessage({ channel, type:'progress-import-result', session_id:sessionId, generation,
            report_id:'fixture-report', source_digest:sourceDigest, schema_version:1,
            accepted:true, items:data.items }, '*');
        }
      });
    </script>`);
});
await new Promise(resolve => artifactServer.listen(0, '127.0.0.1', resolve));
artifactPort = artifactServer.address().port;
const artifactHost = `${artifactId}.artifacts.localhost:${artifactPort}`;
const artifactUrl = `http://${artifactHost}/index.html`;
const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><title>Browser applet fixture</title>
  <style>body{font:16px system-ui;margin:0}.fixture-frame{frame-src:http://*.artifacts.localhost:*}</style>
  <script type="module">
    import browserApplet from '/static/js/browserApplet.js';
    import * as Modals from '/static/js/modalManager.js';
    window.__browserApplet = browserApplet;
    window.__modals = Modals;
    window.__openPreview = () => browserApplet.openPreview({
      resourceRef:'sealed-ref-from-files', revision:{kind:'hostFingerprint',value:'fixture-r1'},
      title:'report.html', accountScope:'owner-fixture', workspaceScope:'workspace-fixture',
    });
  </script>`;

try {
  await withCopalBrowser({ page, request: async (request, response) => {
    const url = new URL(request.url, 'http://fixture');
    if (url.pathname === '/api/auth/status') { response.setHeader('content-type','application/json'); response.end('{"username":"owner-fixture"}'); return true; }
    if (url.pathname === '/api/files-v1/roots') {
      response.setHeader('content-type','application/json');
      response.end(JSON.stringify({version:1,policy_generation:12,entries:[{id:'synthetic-root',ref:'synthetic-root-ref',resource_ref:'synthetic-root-ref',resource_id:'synthetic-root',provider:'host',kind:'provider_root',name:'Synthetic Reports',capabilities:['children','write','stat'],sort_keys:['name']}],import_capabilities:{host:{max_total_bytes:1048576,max_chunk_bytes:1048576}}})); return true;
    }
    if (url.pathname === '/api/files-v1/children' && request.method === 'POST') { response.setHeader('content-type','application/json'); response.end('{"version":1,"entries":[],"next_cursor":null}'); return true; }
    if (url.pathname === '/api/files-v1/places' || url.pathname === '/api/files-v1/workspaces') { response.setHeader('content-type','application/json'); response.end('{"version":1,"entries":[]}'); return true; }
    if (url.pathname === '/api/files-v1/imports' && request.method === 'POST') {
      filesImportRequests += 1;
      const chunks = []; for await (const chunk of request) chunks.push(chunk);
      const body = Buffer.concat(chunks).toString('utf8');
      const match = body.match(/name="metadata"\r\n\r\n([^\r\n]+)/);
      filesImportMetadata = match ? JSON.parse(match[1]) : null;
      response.setHeader('content-type','application/json');
      response.end(JSON.stringify({operation_id:filesImportMetadata.operation_id,generation:filesImportMetadata.generation,state:'pending',items:[{item_id:filesImportMetadata.item_id,outcome:'pending'}]})); return true;
    }
    if (url.pathname.startsWith('/api/files-v1/operations/')) {
      filesReceiptRequests += 1;
      response.setHeader('content-type','application/json');
      response.end(JSON.stringify({operation_id:decodeURIComponent(url.pathname.split('/').at(-1)),generation:filesImportMetadata.generation,state:'complete',items:[{item_id:filesImportMetadata.item_id,outcome:'committed',resource_ref:'imported-backup-ref'}]})); return true;
    }
    if (url.pathname === '/api/browser-previews' && request.method === 'POST') {
      createRequests += 1;
      let raw = ''; for await (const chunk of request) raw += chunk;
      const body = JSON.parse(raw);
      if (body.resource_ref !== 'sealed-ref-from-files' || body.client_generation < 1) throw new Error('unexpected preview request');
      response.setHeader('content-type','application/json');
      if (failNextCreate) {
        failNextCreate = false;
        response.writeHead(503);
        response.end(JSON.stringify({detail:{code:'provider_unavailable',message:'fixture create failed'}}));
        return true;
      }
      response.end(JSON.stringify({version:1,session_id:`session-${createRequests}`,artifact_id:artifactId,origin_port:artifactPort,
        url:artifactUrl,expires_at:(Date.now()/1000)+900,workspace_id:'workspace-fixture',digest:sourceDigest}));
      return true;
    }
    if (/^\/api\/browser-previews\/session-[0-9]+\/status$/.test(url.pathname) && request.method === 'GET') {
      statusRequests += 1; response.setHeader('content-type','application/json');
      response.end(JSON.stringify({version:1,status:delivered?'delivered':'active',delivered})); return true;
    }
    if (/^\/api\/browser-previews\/session-[0-9]+$/.test(url.pathname) && request.method === 'DELETE') {
      deleteRequests += 1; response.setHeader('content-type','application/json');
      if (delayNextDelete) {
        delayNextDelete = false;
        pendingDeleteResponse = () => { pendingDeleteResponse = null; response.writeHead(503); response.end('{"version":1,"revoked":false}'); };
        return true;
      }
      response.end('{"version":1,"revoked":true}'); return true;
    }
    if (url.pathname === '/api/browser-previews/cancel' && request.method === 'POST') {
      response.setHeader('content-type','application/json'); response.end('{"version":1,"cancelled":true}'); return true;
    }
    if (url.pathname === '/' && request.method === 'GET') {
      appOrigin = `http://${request.headers.host}`;
      response.setHeader('content-type','text/html; charset=utf-8');
      response.setHeader('content-security-policy',`default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self' http://*.artifacts.localhost:*; object-src 'none'; base-uri 'self'`);
      response.end(page); return true;
    }
    return false;
  } }, async ({ cdp, evaluate, until }) => {
    await until('Boolean(window.__openPreview)');
    await evaluate('window.__openPreview()');
    await until('document.querySelector(".browser-applet-centered-title")?.textContent === "report.html"');
    assert.equal(await evaluate('document.querySelector(".browser-applet-centered-title")?.parentElement?.classList.contains("modal-header")'), true);
    assert.equal(await evaluate('document.querySelector(".copal-workspace-title small")?.textContent'), 'Interactive Files preview');
    await until('Boolean(document.querySelector(".browser-applet-frame"))');
    await until('Boolean(window.__browserApplet && document.querySelector("#browser-applet-window"))');
    await until('document.querySelector(".browser-applet-retry")?.hidden === false && document.body.innerText.includes("still connecting")');
    const waiting = await evaluate('({calls:document.querySelector(".browser-applet-status")?.textContent, count:document.querySelectorAll(".browser-applet-frame").length})');
    assert.match(waiting.calls, /still connecting/);
    assert.equal(indexRequests, 1, 'the first navigation remains pending without a load/error event');
    assert.ok(statusRequests >= 4, 'bounded status checks begin without waiting for iframe load/error');

    await evaluate('document.querySelector(".browser-applet-retry").click()');
    await until('document.querySelector(".browser-applet-status")?.textContent === "Opened report.html."');
    assert.equal(indexRequests, 2);
    assert.ok(statusRequests >= 5);
    await until('document.querySelector(".browser-applet-progress-tools")?.hidden === false && document.querySelector(".browser-applet-progress-status")?.textContent.includes("Local progress saved")');
    await evaluate('window.__modals.minimize("browser-applet-window")');
    const minimized = await evaluate('({frame:!!document.querySelector(".browser-applet-frame"),minimized:window.__modals.isMinimized("browser-applet-window")})');
    assert.equal(minimized.frame, true, 'minimize retains the artifact iframe and its page state');
    assert.equal(minimized.minimized, true);
    await evaluate('window.__modals.restore("browser-applet-window")');
    assert.equal(await evaluate('window.__modals.isMinimized("browser-applet-window")'), false);
    assert.equal(await evaluate('document.querySelector(".browser-applet-centered-title")?.textContent'), 'report.html');

    delayNextDelete = true;
    await evaluate('window.__browserApplet.close()');
    await until('!document.querySelector(".browser-applet-frame")');
    for (let attempt = 0; !pendingDeleteResponse && attempt < 80; attempt++) await new Promise(resolve => setTimeout(resolve, 25));
    assert.equal(typeof pendingDeleteResponse, 'function', 'retire cleanup is held for the stale-status interleaving');
    await evaluate('window.__openPreview()');
    await until('document.querySelector(".browser-applet-status")?.textContent === "Opened report.html."');
    await until('document.querySelector(".browser-applet-progress-status")?.textContent.includes("Local progress restored")');
    pendingDeleteResponse();
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(await evaluate('document.querySelector(".browser-applet-status")?.textContent'), 'Opened report.html.',
      'late cleanup failure from the retired generation cannot overwrite the reopened view');
    assert.equal(await evaluate(`(() => { try { return document.querySelector('.browser-applet-frame').contentWindow.document.body.dataset.restored || 'readable'; } catch { return 'cross-origin-blocked'; } })()`), 'cross-origin-blocked',
      'cross-origin frame internals remain unavailable to the application page');
    await evaluate('document.querySelector(".browser-applet-progress-tools").open = true');
    const backup = await evaluate(`(async () => {
      const originalClick = HTMLAnchorElement.prototype.click;
      const originalCreate = URL.createObjectURL;
      window.__lastDownload = null;
      window.__lastBlob = null;
      URL.createObjectURL = function (blob) { window.__lastBlob = blob; return originalCreate.call(this, blob); };
      HTMLAnchorElement.prototype.click = function () { window.__lastDownload = { name:this.download, href:this.href }; };
      document.querySelector(".browser-applet-progress-tools button")?.click();
      await new Promise(resolve => setTimeout(resolve, 0));
      HTMLAnchorElement.prototype.click = originalClick;
      URL.createObjectURL = originalCreate;
      const file = window.__lastDownload;
      return file && window.__lastBlob ? { name:file.name, text:await window.__lastBlob.text() } : null;
    })()`);
    assert.equal(backup?.name, 'fixture-report-progress.json');
    const backupEnvelope = JSON.parse(backup.text);
    assert.equal(backupEnvelope.report_id, 'fixture-report');
    assert.equal(backupEnvelope.items[0].value.adoption_note, 'Use this');
    assert.equal(backupEnvelope.items[1].value.rejection_note, 'Does not fit');
    assert.match(await evaluate('document.querySelector(".browser-applet-progress-status")?.textContent'), /not imported to Files/);

    const imported = { ...backupEnvelope, source_digest:`sha256:${'c'.repeat(64)}`, items:[...backupEnvelope.items, {id:'removed-question',digest:`sha256:${'d'.repeat(64)}`,value:'old'}] };
    const importedJson = JSON.stringify(imported);
    await evaluate(`(() => {
      const input = document.querySelector('.browser-applet-progress-tools input[type=file]');
      const transfer = new DataTransfer();
      transfer.items.add(new File([${JSON.stringify(importedJson)}], 'old-progress.json', {type:'application/json'}));
      Object.defineProperty(input, 'files', { configurable:true, value:transfer.files });
      input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('document.querySelector(".browser-applet-progress-status")?.textContent.includes("unknown_item")');
    await until('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Apply"))?.hidden === false');
    // A message from the app origin or a stale session cannot update progress.
    const conflictStatus = await evaluate('document.querySelector(".browser-applet-progress-status")?.textContent');
    await evaluate(`window.postMessage({channel:'openclank.report.v1',type:'progress-save',session_id:'wrong',generation:999,report_id:'fixture-report',source_digest:${JSON.stringify(sourceDigest)},schema_version:1,items:[]}, '*')`);
    await new Promise(resolve => setTimeout(resolve, 50));
    assert.equal(await evaluate('document.querySelector(".browser-applet-progress-status")?.textContent'), conflictStatus);
    const compatibleJson = JSON.stringify(backupEnvelope);
    await evaluate(`(() => {
      const input = document.querySelector('.browser-applet-progress-tools input[type=file]');
      const transfer = new DataTransfer();
      transfer.items.add(new File([${JSON.stringify(compatibleJson)}], 'progress.json', {type:'application/json'}));
      Object.defineProperty(input, 'files', { configurable:true, value:transfer.files });
      input.dispatchEvent(new Event('change', {bubbles:true}));
    })()`);
    await until('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Apply"))?.hidden === false');
    await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Apply"))?.click()');
    await until('document.querySelector(".browser-applet-progress-status")?.textContent.includes("Progress imported into this report")');

    await evaluate('window.dispatchEvent(new CustomEvent("workspace-change",{detail:{workspaceId:"another-workspace"}}))');
    await until('!document.querySelector(".browser-applet-frame")');
    await until('!document.querySelector("#browser-applet-window") || document.querySelector("#browser-applet-window").classList.contains("hidden")');
    assert.ok(deleteRequests >= 1, 'scope retirement requests server-side revocation');

    failNextCreate = true;
    await evaluate('window.__openPreview()');
    await until('document.querySelector(".browser-applet-status")?.textContent.includes("fixture create failed") && document.querySelector(".browser-applet-retry")?.hidden === false');
    await evaluate('document.querySelector(".browser-applet-retry").click()');
    await until('document.querySelector(".browser-applet-status")?.textContent === "Opened report.html."');
    assert.equal(createRequests, 4, 'a visible create failure can be retried against the original Files capture');

    await until('document.querySelector(".browser-applet-progress-tools")?.hidden === false');
    await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent === "Export JSON backup")?.click()');
    await until('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Save backup to Files"))?.hidden === false');
    await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Save backup to Files"))?.click()');
    await until('Boolean(document.querySelector("[data-browser-report-destination-picker]"))');
    await cdp('Emulation.setDeviceMetricsOverride', { width:1280, height:800, deviceScaleFactor:1, mobile:false });
    await new Promise(resolve => setTimeout(resolve, 400));
    const pickerLayout = await evaluate(`(() => {
      const overlay = document.querySelector('[data-browser-report-destination-picker]');
      const dialog = overlay?.querySelector('[role=dialog]');
      const box = dialog?.getBoundingClientRect();
      const style = dialog ? getComputedStyle(dialog) : null;
      return { viewport:[innerWidth,innerHeight], box:box && [box.x,box.y,box.width,box.height], display:style?.display,
        opacity:style?.opacity, background:style?.backgroundColor, zIndex:style?.zIndex,
        overlayZ:overlay && getComputedStyle(overlay).zIndex };
    })()`);
    assert.ok(pickerLayout.box && pickerLayout.box[2] > 300 && pickerLayout.box[3] > 180, 'destination dialog has a visible measured layout');
    assert.ok(pickerLayout.box[0] >= 0 && pickerLayout.box[1] >= 0
      && pickerLayout.box[0] + pickerLayout.box[2] <= pickerLayout.viewport[0]
      && pickerLayout.box[1] + pickerLayout.box[3] <= pickerLayout.viewport[1], 'destination dialog fits inside the review viewport');
    assert.equal(pickerLayout.opacity, '1', 'destination dialog is opaque above the Files and Browser windows');
    const pickerShot = await cdp('Page.captureScreenshot', { format:'png', captureBeyondViewport:false });
    fs.mkdirSync('../.clanker/artifacts/workbench-wb06d', { recursive:true });
    fs.writeFileSync('../.clanker/artifacts/workbench-wb06d/files-destination-picker.png', Buffer.from(pickerShot.data, 'base64'));
    await evaluate('Array.from(document.querySelectorAll("[data-browser-report-destination-picker] [role=option]")).find(button => button.textContent === "Synthetic Reports")?.click()');
    await until('document.querySelector("#styled-confirm-overlay")?.style.display !== "none"');
    await evaluate('document.querySelector("#styled-confirm-ok")?.click()');
    await until('document.querySelector(".browser-applet-progress-status")?.textContent.includes("pending or unknown")');
    assert.equal(filesImportRequests, 1, 'one explicit destination/collision selection reaches the Files staged import');
    assert.equal(filesImportMetadata.destination_ref, 'synthetic-root-ref');
    assert.equal(filesImportMetadata.collision, 'rename');
    await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent === "Check pending Files receipt")?.click()');
    await until('document.querySelector(".browser-applet-progress-status")?.textContent.includes("existing committed receipt confirmed")');
    assert.equal(filesReceiptRequests, 1, 'pending import is reconciled using its existing operation ID before another transfer is offered');
    assert.equal(filesImportRequests, 1, 'receipt reconciliation does not replay the backup under a new operation ID');

    await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Save backup to Files"))?.click()');
    await until('Boolean(document.querySelector("[data-browser-report-destination-picker]"))');
    await evaluate('window.__openPreview()');
    await until('!document.querySelector("[data-browser-report-destination-picker]")');
    await until('document.querySelector(".browser-applet-status")?.textContent === "Opened report.html."');
    await until('document.querySelector(".browser-applet-progress-tools")?.hidden === false');
    assert.equal(await evaluate('Array.from(document.querySelectorAll(".browser-applet-progress-tools button")).find(button => button.textContent.startsWith("Save backup to Files"))?.disabled'), false,
      'retiring the earlier chooser does not leave the reopened Browser action disabled');
    assert.equal(filesImportRequests, 1, 'replacing Browser while the folder chooser is open aborts before staging a duplicate import');

    await evaluate('window.dispatchEvent(new CustomEvent("workspace-change",{detail:{workspaceId:"another-workspace"}}))');
    await until('!document.querySelector(".browser-applet-frame")');
    for (let attempt = 0; deleteRequests < 2 && attempt < 80; attempt += 1) await new Promise(resolve => setTimeout(resolve, 25));
    assert.ok(deleteRequests >= 2, 'retried preview revokes when the workspace scope retires');
  });
} finally {
  artifactServer.closeAllConnections();
  await new Promise(resolve => artifactServer.close(resolve));
}
console.log(JSON.stringify({result:'passed', browser:'Chromium via existing Copal fixture', indexRequests, statusRequests, deleteRequests, createRequests}, null, 2));
