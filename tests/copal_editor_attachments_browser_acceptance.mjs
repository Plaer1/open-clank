#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<nav><a href="/copal/editor" data-copal-view="notes">Editor</a></nav><main id="app"></main>
<script type="module">
  window.__run = async () => { history.replaceState({}, "", "/copal/editor?doc=note-1"); const module = await import('/static/js/copal.js?attachments-production'); await module.init(location.origin); window.__copal = module.default; await module.default.open('notes'); return undefined; };
</script></body>`;

const state = {
  doc:{ id:'note-1', kind:'note', name:'Draft.md', head:'head-1', text:'Hello world', properties:{}, relations:[], tags:[] },
  uploads:0, failed:true, objects:new Set(), objectCreates:0, actionIds:[], references:0, lastPayload:null,
};

function json(res, value, status = 200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }
async function body(req) { const chunks=[]; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; }

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:'attachment-browser', account_id:'account-owner' });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.end(': fixture\n\n'); return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[state.doc] });
  if (url.pathname === '/api/copal/documents/note-1' && req.method === 'GET') return json(res, state.doc);
  if (url.pathname === '/api/test-state') return json(res, { uploads:state.uploads, objects:[...state.objects], objectCreates:state.objectCreates, actionIds:state.actionIds, references:state.references, text:state.doc.text, payload:state.lastPayload });
  if (url.pathname === '/api/copal/attachments' && req.method === 'POST') {
    const payload = await body(req); state.lastPayload = payload; state.uploads += 1; state.actionIds.push(payload.actionId); if (!state.objects.has(payload.name)) state.objectCreates += 1; state.objects.add(payload.name);
    if (state.failed) { state.failed = false; return json(res, { detail:{ outcome:'attachment_uploaded', message:'reference save failed after upload' } }, 503); }
    if (payload.prepareOnly) {
      const asset = { id:'asset-1', kind:'asset', name:payload.name, head:'asset-head-1', text:'', resource:{ key:{ accountId:'account-owner', workspaceId:'default', provider:'copal', resourceId:'asset-1' }, revision:{ kind:'copalHead', value:'asset-head-1' }, representation:'asset', locator:{ displayName:payload.name, locationLabel:'default' }, capabilities:{ read:true, edit:true, rename:true, trash:true, reveal:true } } };
      return json(res, { outcome:'prepared', actionId:payload.actionId, asset, preparation:{ action_id:payload.actionId, phase:'pending', document_id:payload.documentId, base:payload.base, source_text_hash:payload.sourceTextHash, asset_id:'asset-1', asset_name:payload.name } });
    }
    state.doc.text = payload.content; state.doc.head = 'head-2'; state.references = (state.doc.text.match(/!\[\[/g) || []).length;
    const asset = { id:'asset-1', kind:'asset', name:payload.name, head:'asset-head-1', text:'', resource:{ key:{ accountId:'account-owner', workspaceId:'default', provider:'copal', resourceId:'asset-1' }, revision:{ kind:'copalHead', value:'asset-head-1' }, representation:'asset', locator:{ displayName:payload.name, locationLabel:'default' }, capabilities:{ read:true, edit:true, rename:true, trash:true, reveal:true } } };
    return json(res, { outcome:'applied', actionId:payload.actionId, doc:state.doc, asset, receipt:{ outcome:'applied', actionId:payload.actionId, operationId:payload.actionId, assetId:'asset-1', documentId:'note-1' } });
  }
  if (url.pathname === '/api/copal/attachments/commit' && req.method === 'POST') {
    const payload = await body(req); state.doc.text = payload.content; state.doc.head = 'head-2'; state.references = (state.doc.text.match(/!\[\[/g) || []).length;
    return json(res, { outcome:'applied', actionId:payload.actionId, assetId:payload.assetId, doc:state.doc, receipt:{ outcome:'applied', actionId:payload.actionId, operationId:payload.actionId, assetId:payload.assetId, documentId:payload.documentId } });
  }
  if (url.pathname === '/api/copal/attachments/usage') return json(res, { name:url.searchParams.get('name'), count:state.references, usages:[{ id:'note-1', name:state.doc.name, head:state.doc.head }] });
  return false;
}}, async ({ cdp, evaluate, until }) => {
  await until('document.readyState === "complete"');
  await evaluate('void window.__run()');
  await until('!!document.querySelector(".copal-notes-window:not(.hidden) .cm-content")');
  await evaluate('document.querySelector(".copal-notes-window:not(.hidden) .cm-content").focus()');
  for (let i=0; i<5; i += 1) {
    await cdp('Input.dispatchKeyEvent', { type:'keyDown', key:'ArrowRight', code:'ArrowRight', windowsVirtualKeyCode:39, nativeVirtualKeyCode:39 });
    await cdp('Input.dispatchKeyEvent', { type:'keyUp', key:'ArrowRight', code:'ArrowRight', windowsVirtualKeyCode:39, nativeVirtualKeyCode:39 });
  }
  await evaluate('document.querySelector(".copal-notes-window:not(.hidden) button[aria-label=\\"Attach file\\"]").click()');
  await until('!!document.querySelector("input[type=file]")');
  await evaluate(`(() => { const input=document.querySelector('input[type=file]'); const transfer=new DataTransfer(); transfer.items.add(new File(['bytes'], 'photo.png', {type:'image/png'})); input.files=transfer.files; input.dispatchEvent(new Event('change',{bubbles:true})); })()`);
  await until('!!document.querySelector(".copal-attachment-dialog[open]")');
  await evaluate('document.querySelector(".copal-attachment-caption").value="A photo"');
  await evaluate('document.querySelector(".copal-attachment-dialog[open] button.copal-btn.primary").click()');
  await until('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-progress")?.textContent.includes("failed")');
  assert.equal(await evaluate('document.querySelector(".cm-content").textContent.includes("![[")'), false, 'failed upload must not insert a broken reference');
  await evaluate('[...document.querySelectorAll(".copal-attachment-dialog[open] button")].find(button => button.textContent.trim() === "Retry").click()');
  await until('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-progress")?.textContent.includes("Attached")');
  assert.equal(await evaluate('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-progress").textContent.includes("applied")'), true);
  assert.equal(await evaluate('document.querySelector(".cm-content").textContent.includes("![[attachments/photo.png|A photo]]")'), true);
  const result = await evaluate('fetch("/api/test-state").then(r => r.json())');
  assert.equal(result.uploads, 2);
  assert.deepEqual(result.objects, ['attachments/photo.png']);
  assert.deepEqual(result.actionIds, [result.actionIds[0], result.actionIds[0]]);
  assert.equal(result.references, 1);
  const token = '![[attachments/photo.png|A photo]]';
  assert(result.text.indexOf(token) > 0 && result.text.indexOf(token) < result.text.length - token.length, `attachment must be inserted into the existing text: ${result.text}`);
  assert.equal(await evaluate('document.querySelector(".copal-attachment-dialog[open]").textContent.includes("Retry")'), true);
  assert.equal(await evaluate('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-usage").textContent'), '1 document usage');
  await evaluate('document.querySelector(".copal-attachment-dialog[open]").close()');
  await evaluate(`(() => { const host=document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host'); const transfer=new DataTransfer(); transfer.items.add(new File(['paste'], 'pasted.txt', {type:'text/plain'})); const event=new Event('paste',{bubbles:true,cancelable:true}); Object.defineProperty(event,'clipboardData',{value:transfer}); host.dispatchEvent(event); })()`);
  await until('!!document.querySelector(".copal-attachment-dialog[open]")');
  await evaluate('document.querySelector(".copal-attachment-dialog[open] button.copal-btn.primary").click()');
  await until('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-progress")?.textContent.includes("Attached")');
  await evaluate('document.querySelector(".copal-attachment-dialog[open]").close()');
  await evaluate(`(() => { const host=document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host'); const transfer=new DataTransfer(); transfer.items.add(new File(['drop'], 'dropped.txt', {type:'text/plain'})); const event=new Event('drop',{bubbles:true,cancelable:true}); Object.defineProperty(event,'dataTransfer',{value:transfer}); host.dispatchEvent(event); })()`);
  await until('!!document.querySelector(".copal-attachment-dialog[open]")');
  await evaluate('document.querySelector(".copal-attachment-dialog[open] button.copal-btn.primary").click()');
  await until('document.querySelector(".copal-attachment-dialog[open] .copal-attachment-progress")?.textContent.includes("Attached")');
  const completed = await evaluate('fetch("/api/test-state").then(r => r.json())');
  assert.equal(completed.references, 3);
  assert.equal(completed.uploads, 4);
  assert.equal(completed.objectCreates, 3);
  assert.deepEqual(completed.objects.sort(), ['attachments/dropped.txt', 'attachments/pasted.txt', 'attachments/photo.png']);
  assert.equal(completed.actionIds[0], completed.actionIds[1]);
  assert.equal(new Set(completed.actionIds.slice(2)).size, 2);
  console.log('Production Editor attachment path: captured cursor, caption, failure recovery, retry idempotency surface, usage status, and real CodeMirror insertion passed.');
});
