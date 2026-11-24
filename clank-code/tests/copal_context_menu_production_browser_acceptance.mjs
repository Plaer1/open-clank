#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><body>
<nav><a href="/copal/editor" data-copal-view="notes">Editor</a></nav><main id="app"></main>
<script type="module">
  const { initCustomContextMenu } = await import('/static/js/custom-context-menu.js');
  initCustomContextMenu();
  window.__run = async () => { const module = await import('/static/js/copal.js'); await module.init(location.origin); window.__copal = module.default; await module.default.open('timeline'); return undefined; };
</script></body>`;

const doc = { id:'note-1', kind:'note', name:'README.md', head:'head-1', text:'# Readme', properties:{}, relations:[], tags:[], resource:{ key:{ accountId:'account-owner', workspaceId:'default', provider:'host', resourceId:'resource-readme' }, revision:{ kind:'hostFingerprint', value:'fp-1' }, representation:'markdown', locator:{ displayName:'README.md', locationLabel:'README.md', opaqueRef:'rr1.readme' }, capabilities:{ read:true, edit:true, rename:false, delete:false } } };
const localDate = (offset) => { const value = new Date(); value.setDate(value.getDate() + offset); return `${value.getFullYear()}-${String(value.getMonth() + 1).padStart(2, '0')}-${String(value.getDate()).padStart(2, '0')}`; };
const planning = { tracks:[{ id:'track-1', name:'Build', color:'#8b5cf6', icon:'build', enabled:true, tasks:[{ id:'event-1', title:'Ship', startDate:localDate(-1), dueDate:localDate(1), primaryTrackId:'track-1', status:'pending', sharedTrackIds:[] }] }], floatingTodos:[] };
const treehouseCommands = [];
const treehouse = { accountId:'account-owner', workspace:'default', actor:{ id:'owner', displayName:'Owner' }, permissions:{ learner:true, author:false, admin:false, analytics:false, grade:false }, courseCapabilities:{}, state:{ revision:'r1', profiles:{ owner:{ id:'owner', displayName:'Owner', active:true, roles:['learner'] } }, courses:{ "course-1":{ id:'course-1', title:'Course One', description:'A course', status:'published', moduleIds:['module-1'] } }, modules:{ "module-1":{ id:'module-1', courseId:'course-1', title:'Module One', description:'', activityIds:['activity-1','activity-2'], assignmentIds:[] } }, activities:{ "activity-1":{ id:'activity-1', title:'Lesson One', activityType:'lesson', points:1, status:'published', skillIds:[], content:'[Next lesson](#treehouse-unit=activity-2)', surface:{ href:'/copal/editor?doc=note-1', label:'Editor' } }, 'activity-2':{ id:'activity-2', title:'Lesson Two', activityType:'lesson', points:1, status:'published', skillIds:[], content:'Second lesson' } }, assignments:{}, courseGrants:{}, enrollments:{}, submissions:{}, skills:{}, events:[] }, projection:{ eventCount:0, learners:{ owner:{ points:0, badges:[], quests:[], streak:0, courses:{}, skills:{}, pointEvidence:[] } } } };

function json(res, value, status=200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/auth/status') return json(res, { authenticated:true, username:'owner', account_id:'account-owner' });
  if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:'production-context', account_id:'account-owner' });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream'}); res.end(': fixture\n\n'); return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:[doc] });
  if (url.pathname === '/api/copal/documents/note-1') return json(res, doc);
  if (url.pathname === '/api/copal/planning') return json(res, planning);
  if (url.pathname === '/api/copal/treehouse') return json(res, treehouse);
  if (url.pathname === '/api/copal/treehouse/commands' && req.method === 'POST') {
    let body = '';
    for await (const chunk of req) body += chunk;
    const { type, payload } = JSON.parse(body);
    treehouseCommands.push({ type, payload });
    return json(res, treehouse);
  }
  if (url.pathname === '/api/test-state') return json(res, { treehouseCommands });
  return false;
}}, async ({ cdp, evaluate, until }) => {
  await until('typeof window.__run === "function"');
  await evaluate('void window.__run()');
  await until('!!document.querySelector(".copal-track[data-track-id=\\"track-1\\"]")');
  const openMenu = async (selector, touch=false) => {
    const point = await evaluate(`new Promise((resolve) => { const node=document.querySelector(${JSON.stringify(selector)}); node.scrollIntoView({ block:'center', inline:'center' }); requestAnimationFrame(() => requestAnimationFrame(() => { const rect=node.getBoundingClientRect(); resolve({ x:rect.left + Math.min(20, rect.width / 2), y:rect.top + Math.min(20, rect.height / 2) }); })); })`);
    if (touch) {
      await cdp('Emulation.setTouchEmulationEnabled', { enabled:true, maxTouchPoints:1 });
      await cdp('Input.dispatchTouchEvent', { type:'touchStart', touchPoints:[{ x:point.x, y:point.y, radiusX:2, radiusY:2, force:1, id:1 }] });
    } else {
      await cdp('Input.dispatchMouseEvent', { type:'mousePressed', ...point, button:'right', buttons:2, clickCount:1 });
      await cdp('Input.dispatchMouseEvent', { type:'mouseReleased', ...point, button:'right', buttons:0, clickCount:1 });
    }
    await until('!!document.querySelector("#openclank-context-menu")');
    if (touch) {
      await cdp('Input.dispatchTouchEvent', { type:'touchEnd', touchPoints:[] });
      await cdp('Emulation.setTouchEmulationEnabled', { enabled:false });
    }
  };
  const nativeTarget = async (selector) => evaluate(`(() => {
    const target = document.querySelector(${JSON.stringify(selector)});
    const event = new MouseEvent('contextmenu', { bubbles:true, cancelable:true, button:2 });
    target.dispatchEvent(event);
    return { prevented:event.defaultPrevented, menu:!!document.getElementById('openclank-context-menu') };
  })()`);
  assert.deepEqual(await nativeTarget('.copal-track[data-track-id="track-1"]'), { prevented:false, menu:false });
  await openMenu('.copal-track[data-track-id="track-1"] .copal-track-name');
  assert.equal(await evaluate('[...document.querySelectorAll("#openclank-context-menu button")].some(b => b.dataset.command === "edit-track")'), true);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=edit-track]").click()');
  await until('!!document.querySelector(".copal-track-editor-window")');
  assert.equal(await evaluate('document.querySelector(".copal-track-editor-window").textContent.includes("Edit track")'), true);
  await evaluate(`document.querySelector('.copal-track-editor-window button[aria-label="Close Edit track"]').click()`);
  await evaluate('window.__copal.open("timeline")');
  await until('!!document.querySelector(".copal-timeline")');
  await until('!!document.querySelector(".copal-event[data-task-id=\\"event-1\\"]")');
  await openMenu('.copal-event[data-timeline-event-id="event-1"] .copal-event-label');
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu [data-command=edit-track]")'), false);
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=edit-event]").click()');
  await until('!!document.querySelector(".copal-event-editor-window")');
  assert.equal(await evaluate('document.querySelector(".copal-event-editor-window").textContent.includes("Edit event")'), true);
  await evaluate(`document.querySelector('.copal-event-editor-window button[aria-label="Close Edit event"]').click()`);
  await evaluate('window.__copal.open("treehouse")');
  await until('!!document.querySelector(".th-course-card[data-treehouse-id=course-1] h2")');
  await openMenu('.th-course-card[data-treehouse-id="course-1"] h2', true);
  await evaluate('document.querySelector("#openclank-context-menu [data-command=open-treehouse-course]").click()');
  await until('!!document.querySelector(".th-lesson[data-treehouse-id=activity-1]")');
  const nextLesson = '.th-lesson[data-treehouse-id="activity-1"] a[href="#treehouse-unit=activity-2"]';
  await openMenu(nextLesson);
  assert.equal(await evaluate('!!document.querySelector("#openclank-context-menu [data-command=open-treehouse-course]")'), false);
  await evaluate('document.querySelector("#openclank-context-menu [data-command=open-treehouse-lesson]").click()');
  await until('!!document.querySelector(".th-lesson[data-treehouse-id=activity-2]")');
  assert.deepEqual((await evaluate('fetch("/api/test-state").then(r => r.json())')).treehouseCommands, [
    { type:'course.open', payload:{ courseId:'course-1', activityId:'activity-1' } },
    { type:'course.open', payload:{ courseId:'course-1', activityId:'activity-2' } },
  ]);
  await evaluate('window.__copal.open("graph")');
  await until('document.querySelectorAll(".copal-graph-node").length > 0');
  assert.deepEqual(await nativeTarget('.copal-graph'), { prevented:false, menu:false });
  await openMenu('.copal-graph-node circle');
  await evaluate('document.querySelector("#openclank-context-menu button[data-command=open-graph-node]").click()');
  await until('location.pathname === "/editor"');
  // Files-to-Editor handoff has its own files_window_browser_acceptance.mjs fixture.
  await evaluate('window.__copal.open("notes")');
  await until('!!document.querySelector(".copal-notes-window:not(.hidden) .cm-content")');
  await evaluate('window.__copal.open("timeline")');
  await until('!!document.querySelector(".copal-track[data-track-id=\\"track-1\\"]")');
  const menuOpen = await evaluate('!!document.querySelector("#openclank-context-menu")');
  assert.equal(menuOpen, false);
  console.log('Production Copal context commands: Timeline track and event, TreeHouse course hold and linked lesson, and the Graph-to-Editor handoff passed.');
});
