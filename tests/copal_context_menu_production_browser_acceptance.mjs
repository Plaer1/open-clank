#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body>
<nav><a href="/copal/editor" data-copal-view="notes">Editor</a></nav><main id="app"></main>
<script type="module">
  const { initCustomContextMenu } = await import('/static/js/custom-context-menu.js?production-context');
  initCustomContextMenu();
  window.__openFiles = async () => { const module = await import('/static/js/files.js?production-context-files'); await module.default.open(); return undefined; };
  window.__run = async () => { const module = await import('/static/js/copal.js?production-context'); await module.init(location.origin); window.__copal = module.default; await module.default.open('timeline'); return undefined; };
</script></body>`;

const resource = { ref:'rr1.readme', id:'resource-readme', provider:'host', kind:'file', name:'README.md', mime_type:'text/markdown', capabilities:['stat','open','preview'] };
const doc = { id:'note-1', kind:'note', name:'README.md', head:'head-1', text:'# Readme', properties:{}, relations:[], tags:[], resource:{ key:{ accountId:'account-owner', workspaceId:'default', provider:'host', resourceId:'resource-readme' }, revision:{ kind:'hostFingerprint', value:'fp-1' }, representation:'markdown', locator:{ displayName:'README.md', locationLabel:'README.md', opaqueRef:'rr1.readme' }, capabilities:{ read:true, edit:true, rename:false, delete:false } } };
const localDate = (offset) => { const value = new Date(); value.setDate(value.getDate() + offset); return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; };
const planning = { tracks:[{ id:'track-1', name:'Build', color:'#8b5cf6', icon:'build', enabled:true, tasks:[{ id:'event-1', title:'Ship', startDate:localDate(-1), dueDate:localDate(1), primaryTrackId:'track-1', status:'pending', sharedTrackIds:[] }] }], floatingTodos:[] };
let fileOpens = 0;
const treehouse = { accountId:'account-owner', workspace:'default', actor:{ id:'owner', displayName:'Owner' }, permissions:{ learner:true, author:false, admin:false, analytics:false, grade:false }, courseCapabilities:{}, state:{ revision:'r1', profiles:{ owner:{ id:'owner', displayName:'Owner', active:true, roles:['learner'] } }, courses:{ "course-1":{ id:'course-1', title:'Course One', description:'A course', status:'published', moduleIds:['module-1'] } }, modules:{ "module-1":{ id:'module-1', courseId:'course-1', title:'Module One', description:'', activityIds:['activity-1'], assignmentIds:[] } }, activities:{ "activity-1":{ id:'activity-1', title:'Lesson One', activityType:'lesson', points:1, status:'published', skillIds:[], content:'Lesson', surface:{ href:'/copal/editor?doc=note-1', label:'Editor' } } }, assignments:{}, courseGrants:{}, enrollments:{}, submissions:{}, skills:{}, events:[] }, projection:{ eventCount:0, learners:{ owner:{ points:0, badges:[], quests:[], streak:0, courses:{}, skills:{}, pointEvidence:[] } } } };

function json(res, value, status=200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }
function payload(ref='rr1.readme', text='# Readme\\n') { return { target:{ app:'editor' }, exact:true, resource, payload:{ name:'README.md', kind:'text', corpus:'host', text, representation:'markdown', resource:{ ref, key:{ accountId:'account-owner', workspaceId:'default', provider:'host', resourceId:'resource-readme' }, revision:{ kind:'hostFingerprint', value:'fp-1' }, locator:{ displayName:'README.md', locationLabel:'README.md', opaqueRef:ref }, metadata:{ encoding:'utf-8', newline:'\\n', bomBytes:0, mode:'markdown', language:'markdown' }, capabilities:{ read:true, edit:true, rename:false, delete:false } } } }; }

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:'production-context', account_id:'account-owner' });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.end(': fixture\n\n'); return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[doc] });
  if (url.pathname === '/api/copal/documents/note-1') return json(res, doc);
  if (url.pathname === '/api/copal/planning') return json(res, planning);
  if (url.pathname === '/api/copal/treehouse') return json(res, treehouse);
  if (url.pathname === '/api/files-v1/roots') return json(res, { version:1, policy_generation:1, entries:[{ id:'root', ref:'root-ref', provider:'host', kind:'provider_root', name:'Host', capabilities:['children','stat'], sort_keys:['name'] }] });
  if (url.pathname === '/api/files-v1/children') return json(res, { entries:[{ id:'resource-readme', ref:'rr1.readme', resource_ref:'rr1.readme', provider:'host', kind:'file', name:'README.md', mime_type:'text/markdown', capabilities:['stat','open','preview'], sort_keys:['name'] }], next_cursor:null });
  if (url.pathname === '/api/files-v1/places' || url.pathname === '/api/files-v1/workspaces') return json(res, { entries:[] });
  if (url.pathname === '/api/files-v1/open-resource' && req.method === 'POST') { fileOpens += 1; return json(res, payload()); }
  if (url.pathname === '/api/files-v1/action') { fileOpens += 1; return json(res, { target:{ app:'editor' }, exact:true, resource }); }
  if (url.pathname === '/api/test-state') return json(res, { fileOpens });
  return false;
}}, async ({ cdp, evaluate, until }) => {
  await until('document.readyState === "complete"');
  await evaluate('void window.__run()');
  await until('!!document.querySelector(".copal-track[data-track-id=\\"track-1\\"]")');
  const openMenu = async (selector, touch=false) => {
    const point = await evaluate(`new Promise((resolve) => { const node=document.querySelector(${JSON.stringify(selector)}); node.scrollIntoView({ block:'center', inline:'center' }); requestAnimationFrame(() => requestAnimationFrame(() => { const rect=node.getBoundingClientRect(); resolve({ x:rect.left + Math.min(20, rect.width / 2), y:rect.top + Math.min(20, rect.height / 2) }); })); })`);
    if (touch) {
      await cdp('Emulation.setTouchEmulationEnabled', { enabled:true, maxTouchPoints:1 });
      await cdp('Input.dispatchTouchEvent', { type:'touchStart', touchPoints:[{ x:point.x, y:point.y, radiusX:2, radiusY:2, force:1, id:1 }] });
    } else {
      await evaluate(`document.querySelector(${JSON.stringify(selector)}).dispatchEvent(new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:20, clientY:20, detail:1 }))`);
    }
    await until('!!document.querySelector("#openclank-context-menu")');
    if (touch) {
      await cdp('Input.dispatchTouchEvent', { type:'touchEnd', touchPoints:[] });
      await cdp('Emulation.setTouchEmulationEnabled', { enabled:false });
    }
  };
  await openMenu('.copal-track');
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "edit-track")'), true);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=edit-track]").click()');
  await until('!!document.querySelector(".copal-track-editor-window")');
  assert.equal(await evaluate('document.querySelector(".copal-track-editor-window").textContent.includes("Edit track")'), true);
  await evaluate('document.querySelector(".copal-track-editor-window")?.remove()');
  await evaluate('window.__copal.open("timeline")');
  await until('!!document.querySelector(".copal-timeline")');
  await until('!!document.querySelector(".copal-event[data-task-id=\\"event-1\\"]")');
  await openMenu('.copal-event');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=edit-event]").click()');
  await until('!!document.querySelector(".copal-event-editor-window")');
  assert.equal(await evaluate('document.querySelector(".copal-event-editor-window").textContent.includes("Edit event")'), true);
  await evaluate('document.querySelector(".copal-event-editor-window")?.remove()');
  await evaluate('window.__copal.open("treehouse")');
  await until('!!document.querySelector(".copal-treehouse-course[data-treehouse-id=\\"course-1\\"]")');
  await openMenu('.copal-treehouse-course', true);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=open-treehouse-item]").click()');
  await until('!!document.querySelector(".copal-treehouse-detail")');
  assert.equal(await evaluate('document.querySelector(".copal-treehouse-detail").textContent.includes("Module One")'), true);
  await evaluate('window.__copal.open("graph")');
  await until('document.querySelectorAll(".copal-graph-node").length > 0');
  await openMenu('.copal-graph-node');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=open-graph-node]").click()');
  await until('location.pathname === "/editor"');
  await evaluate('void window.__openFiles()');
  await until('!!document.querySelector(".files-entry.file")');
  await openMenu('.files-entry.file');
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "open-in-editor")'), true);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=open-in-editor]").click()');
  await until('fetch("/api/test-state").then(r => r.json()).then(s => s.fileOpens >= 1)');
  await evaluate('(async () => { delete window.__openClankCopalContextCommand; await window.__openFiles(); await new Promise(resolve => setTimeout(resolve, 250)); })()');
  await until('!!document.querySelector(".files-entry.file")');
  await openMenu('.files-entry.file');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=open-in-editor]").click()');
  await until('fetch("/api/test-state").then(r => r.json()).then(s => s.fileOpens >= 2)');
  await evaluate('window.__copal.open("notes")');
  await until('!!document.querySelector(".copal-notes-window:not(.hidden) .cm-content")');
  await evaluate('window.__copal.open("timeline")');
  await until('!!document.querySelector(".copal-track[data-track-id=\\"track-1\\"]")');
  const menuOpen = await evaluate('!!document.querySelector("#openclank-context-menu")');
  assert.equal(menuOpen, false);
  console.log('Production Copal context commands: actual Timeline track/event, TreeHouse course touch long-press, Graph node, and Editor handoffs passed.');
});
