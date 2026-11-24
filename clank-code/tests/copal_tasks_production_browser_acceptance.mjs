#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<nav><a href="/copal/todo" data-copal-view="todo">Tasks</a><a href="/copal/editor" data-copal-view="notes">Editor</a></nav>
<main id="app"></main><script type="module">
  window.__runTasks = async () => { const module = await import('/static/js/copal.js?production-task-browser'); await module.init(location.origin); window.__copal = module.default; await module.default.open('todo'); };
</script></body>`;

const key = { accountId:'account-owner', workspaceId:'default', provider:'copal', resourceId:'resource-note-1' };
const resource = { key, revision:{ kind:'copalHead', value:'head-1' }, representation:'nativeNote', locator:{ displayName:'Release.md', locationLabel:'default' }, capabilities:{ read:true, edit:true, rename:true, trash:true, reveal:true } };
const state = {
  doc:{ id:'note-1', kind:'note', name:'Release.md', head:'head-1', text:'- [ ] Write release notes', properties:{}, relations:[], tags:[], resource },
  task:{ id:'note-1:block-1', type:'markdown', source:'vault', resourceKey:key, sourceRevision:'head-1', anchor:{ blockId:'block-1', sourceRange:{ from:0, to:25 }, expectedTextHash:'hash-1', expectedText:'- [ ] Write release notes' }, text:'Write release notes', checked:false, line:1, label:'Release.md', document:{ id:'note-1', name:'Release.md', head:'head-1', kind:'note' } },
  writes:0, creates:0,
};

async function readBody(req) { const chunks=[]; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; }
function json(res, value, status=200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/test-state') return json(res, { writes:state.writes, creates:state.creates, text:state.doc.text, checked:state.task.checked });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:'task-browser', account_id:'account-owner' });
  if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.end(': fixture\n\n'); return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[state.doc] });
  if (url.pathname === '/api/copal/documents/note-1' && req.method === 'GET') return json(res, state.doc);
  if (url.pathname === '/api/copal/tasks/query') return json(res, { items:[state.task], tasks:[state.task], nextCursor:null, snapshotRevision:state.doc.head, queryRevision:state.doc.head, complete:true, total:1 });
  if (decodeURIComponent(url.pathname) === '/api/copal/tasks/note-1:block-1' && req.method === 'PATCH') {
    const body = await readBody(req); state.writes += 1; state.task.checked = !!body.checked; state.doc.text = state.task.checked ? '- [x] Write release notes' : '- [ ] Write release notes'; state.doc.head = 'head-2'; state.task.sourceRevision = 'head-2'; state.task.checked = !!body.checked;
    return json(res, { outcome:'applied', actionId:body.actionId, revision:'head-2', task:state.task });
  }
  if (url.pathname === '/api/copal/tasks/create' && req.method === 'POST') {
    const body = await readBody(req); state.creates += 1; state.doc.text += '\n- [ ] Created from task view'; state.doc.head = 'head-3'; return json(res, { outcome:'applied', actionId:body.actionId, revision:'head-3', task:{ ...state.task, id:'note-1:block-2', text:'Created from task view', checked:false, sourceRevision:'head-3' } });
  }
  return false;
}}, async ({ evaluate, until }) => {
  await until('document.readyState === "complete"');
  assert.equal(await evaluate('typeof window.__runTasks'), 'function');
  await evaluate('window.__runTasks()');
  await until('document.querySelector(".copal-meatbag-tasks")');
  assert.equal(await evaluate('document.querySelectorAll(".copal-task-row").length'), 1);
  await evaluate('document.querySelector(".copal-task-row input").click()');
  await until('fetch("/api/test-state").then(r => r.json()).then(s => s.writes === 1 && s.checked === true)');
  assert.equal((await evaluate('fetch("/api/test-state").then(r => r.json())')).text, '- [x] Write release notes');
  await evaluate('document.querySelector(".copal-task-title").click()');
  await until('location.pathname === "/copal/editor"');
  assert.equal(await evaluate('new URL(location.href).searchParams.get("doc")'), 'note-1');
  await evaluate('window.__copal.open("todo")');
  await until('document.querySelector(".copal-meatbag-tasks")');
  await evaluate('document.querySelector(".copal-meatbag-tasks .primary").click()');
  await until('document.querySelector("#styled-prompt-overlay")?.style.display !== "none"');
  await evaluate('document.querySelector("#styled-prompt-input").value = "Created from task view"; document.querySelector("#styled-prompt-ok").click()');
  await until('fetch("/api/test-state").then(r => r.json()).then(s => s.creates === 1)');
  await evaluate('const s=document.querySelector("[aria-label=\\"Source filter\\"]"); s.value="timeline"; s.dispatchEvent(new Event("change",{bubbles:true}))');
  assert.equal(await evaluate('document.querySelectorAll(".copal-task-row").length'), 0);
  console.log('Production Copal task route: persisted checkbox mutation, one request, exact Editor selection, create-in-note, source filter, and refresh passed.');
});
