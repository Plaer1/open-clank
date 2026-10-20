#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><style>
html,body{margin:0;width:100%;height:100%;overflow:auto;background:#151b24;color:#e4eef5}
#fixture-timeline{position:absolute;left:20px;top:20px;width:1300px;height:430px}#timeline-body{height:100%;width:100%;box-sizing:border-box}
#fixture-notes{position:absolute;left:20px;top:475px;width:1300px;height:460px}#notes-body{height:100%;width:100%;box-sizing:border-box}
</style><div id="fixture-timeline"><main id="timeline-body"></main></div><section id="fixture-notes"><main id="notes-body"></main></section>`;

await withCopalBrowser({ page }, async ({ evaluate, until, cdp }) => {
  await cdp('Emulation.setDeviceMetricsOverride', { width:1440, height:1000, deviceScaleFactor:1, mobile:false });
  await until('document.readyState === "complete"');
  await evaluate(`(async () => {
    const { createPlanningFeature } = await import('/static/js/copal/planning.js?perf=1');
    const { createNotesFeature } = await import('/static/js/copal/notesFeature.js?perf=1');
    // Planning imports the canonical storage URL. Keep this fixture on the
    // same ESM module instance so event-editor interactions have the scope
    // configured below instead of a query-string-isolated empty module.
    const { configureCopalStorage } = await import('/static/js/copal/storage.js');
    const h = (tag, attrs = {}, ...children) => { const node = document.createElement(tag); for (const [key, value] of Object.entries(attrs || {})) { if (key === 'text') node.textContent = String(value); else if (key === 'class') node.className = String(value); else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2), value); else if (value != null && value !== false) node.setAttribute(key, String(value)); } for (const child of children.flat()) if (child) node.append(child.nodeType ? child : document.createTextNode(String(child))); return node; };
    const no = () => {}; configureCopalStorage('renderer-performance');
    const date = offset => { const d = new Date(2026, 8, 1 + offset); return d.toISOString().slice(0, 10); };
    const tracks = Array.from({ length: 80 }, (_, index) => ({ id:'track-' + index, name:'Track ' + index, color:'#5ebfe3', icon:'timeline', enabled:true, parentTrackId:null, tasks:[] }));
    for (let index = 0; index < 4000; index++) { const track = tracks[index % tracks.length]; const day = 20 + (Math.floor(index / tracks.length) % 200); track.tasks.push({ id:'event-' + index, trackId:track.id, title:'Event ' + index, startDate:date(day), dueDate:date(day + (index % 7 === 0 ? 2 : 0)), status:'pending', priority:'medium' }); }
    let planningData = { title:'Synthetic Timeline', tracks, floatingTodos:[] };
    const planning = createPlanningFeature({ h, api:async() => ({}), getPlanning:() => planningData, refresh:async() => {}, setStatus:no, projectionChanged:no, openDocument:no });
    planning.loadState('performance');
    const timelineBody = document.querySelector('#timeline-body');
    const renderTimeline = () => { Object.assign(planning.timelineState, { dayWidth:18, rangeStart:new Date(2026,8,1), rangeEnd:new Date(2026,11,31), extending:false, expandedTracks:new Set(), collapsedTrackGroups:new Set() }); planning.renderTimeline(timelineBody, { left:500 }); void timelineBody.offsetHeight; };
    renderTimeline(); const timelineSamples = [];
    const interactionSamples = []; const invalidationSamples = []; const interactionTarget = timelineBody.querySelector('[data-task-id=\"event-0\"]');
    for (let index = 0; index < 30; index++) { await new Promise(resolve => setTimeout(resolve, 0)); const start = performance.now(); interactionTarget.click(); await new Promise(resolve => requestAnimationFrame(() => resolve())); interactionSamples.push(performance.now() - start); }
    interactionSamples.sort((a,b) => a-b);
    for (let index = 0; index < 30; index++) { const start = performance.now(); renderTimeline(); timelineSamples.push(performance.now() - start); }
    const notesState = { workspace:'performance', api:'', docs:[], windows:new Map(), noteEditors:new Set(), selected:null };
    const notesRoot = document.querySelector('#fixture-notes'); const notesBody = document.querySelector('#notes-body');
    const current = { selected:null, window:{ root:notesRoot, body:notesBody, actions:h('div'), setStatus:no } }; notesState.windows.set('notes', current);
    const notes = createNotesFeature({ h, api:async() => ({}), state:notesState, createMarkdownEditor:() => { throw Error('read-only performance fixture must not instantiate an editor'); }, renderMarkdown:text => h('p', { text }), formatBaseCell:String, saveDocument:async() => ({}), renameNote:async()=>{}, deleteDocument:no, showHistory:no, showTrash:no, showForm:no, importVault:no, loadDocuments:async()=>{}, openDocument:no, persistActiveContext:no, activateNotes:no, renderTimeline:no, openEventEditor:no });
    const noteCorpus = count => Array.from({ length:count }, (_, index) => ({ id:'note-' + index, name:'Folder ' + String(Math.floor(index / 10)).padStart(4, '0') + '/Note ' + String(index).padStart(5, '0') + '.md', kind:'note', text:'# Note ' + index + '\\n' + 'Synthetic sentence. '.repeat(40) + '\\n- [ ] Sample task', properties:{ topic:'fixture' }, tags:['fixture'], links:[], readOnly:true, head:'head-' + index }));
    const renderNotes = () => { if (!notesState.docs.length) { notesState.docs = noteCorpus(5000); current.noteSaved = { explorerOpen:true, sidebarOpen:false, expanded:[...new Set(notesState.docs.map(doc => doc.name.split('/')[0]))] }; } notes.render(); void notesBody.offsetHeight; };
    const noteColdStart = performance.now(); renderNotes(); const noteColdMountMs = performance.now() - noteColdStart;
    const notesSamples = [];
    window.__collectRendererSamples = async (count = 10) => { for (let index = 0; index < count; index++) { const start = performance.now(); renderNotes(); notesSamples.push(performance.now() - start); } return { timeline:timelineSamples.length, notes:notesSamples.length, interaction:interactionSamples.length }; };
    window.__getRendererPerf = () => { timelineSamples.sort((a,b) => a-b); notesSamples.sort((a,b) => a-b); interactionSamples.sort((a,b) => a-b); invalidationSamples.sort((a,b) => a-b); return { timeline:{ samples:timelineSamples, medianMs:timelineSamples[14], p95Ms:timelineSamples[Math.ceil(timelineSamples.length * .95) - 1], events:4000, tracks:80, descendants:timelineBody.querySelectorAll('*').length }, notes:{ coldMountMs:noteColdMountMs, samples:notesSamples, medianMs:notesSamples[14], p95Ms:notesSamples[Math.ceil(notesSamples.length * .95) - 1], notes:5000, entries:notesBody.querySelectorAll('.copal-file-entry').length, folders:notesBody.querySelectorAll('.copal-folder-row').length, docs:notesState.docs.length, descendants:notesBody.querySelectorAll('*').length }, interaction:{ samples:interactionSamples, p95Ms:interactionSamples[Math.ceil(interactionSamples.length * .95) - 1] }, invalidation:{ samples:invalidationSamples, medianMs:invalidationSamples[Math.floor(invalidationSamples.length / 2)], p95Ms:invalidationSamples[Math.ceil(invalidationSamples.length * .95) - 1], updates:invalidationSamples.length } }; };
    window.__collectRendererInvalidations = async (count = 30) => { for (let index = 0; index < count; index++) { const event = tracks[0].tasks[0]; event.title = 'Cache invalidation event ' + index; event.status = index % 2 ? 'done' : 'pending'; const start = performance.now(); renderTimeline(); await new Promise(resolve => requestAnimationFrame(() => resolve())); invalidationSamples.push(performance.now() - start); } return invalidationSamples.length; };
    window.__checkTimelineInvalidation = () => { const fail = message => { throw new Error(message); }; const directRender = () => { planning.renderTimeline(timelineBody); void timelineBody.offsetHeight; }; const nodes = () => [...timelineBody.querySelectorAll('[data-task-id="event-0"]')]; const event = tracks[0].tasks[0]; const scroll = timelineBody.querySelector('.copal-timeline-scroll'); scroll.scrollLeft = 137; scroll.focus(); const originalNode = timelineBody.querySelector('[data-task-id="event-0"]'); renderTimeline(); const unchanged = { sameNode:timelineBody.querySelector('[data-task-id="event-0"]') === originalNode, scrollLeft:scroll.scrollLeft, active:document.activeElement === scroll }; if (!unchanged.sameNode || unchanged.scrollLeft !== 137 || !unchanged.active) fail('no-op render must retain event DOM, scroll, and focus');
      event.title = 'in-place title'; renderTimeline(); const titleNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (titleNode !== originalNode || !titleNode.textContent.includes('in-place title')) fail('in-place title mutation must patch the retained event node');
      const priorStart = event.startDate; const priorDue = event.dueDate; event.dueDate = '2026-10-04'; renderTimeline(); const dueNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (dueNode === titleNode) fail('in-place due-date mutation must rebuild event node'); event.dueDate = priorDue; const priorTrack = event.trackId; event.trackId = 'track-1'; renderTimeline(); const reassignedNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (reassignedNode === dueNode || !reassignedNode?.closest('[data-track-id="track-1"]')) fail('in-place track mutation must rebuild event under the new track'); event.trackId = priorTrack; renderTimeline();
      const priorShared = event.sharedTrackIds || []; const singleTrackCount = nodes().length; event.sharedTrackIds = ['track-1']; renderTimeline(); const sharedTrackCount = nodes().length; if (sharedTrackCount <= singleTrackCount) fail('shared-track mutation must add an event projection (' + singleTrackCount + ' -> ' + sharedTrackCount + ')'); event.sharedTrackIds = priorShared; renderTimeline(); if (nodes().length !== singleTrackCount) fail('shared-track removal must remove the extra event projection');
      event.fuzzy = { anchorStart:priorStart, anchorEnd:event.dueDate, whiskerStart:'2026-09-30' }; event.startDate = 'FUZZY'; renderTimeline(); const fuzzyNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (fuzzyNode === reassignedNode || !fuzzyNode.classList.contains('fuzzy')) fail('fuzzy whisker mutation must rebuild event node');
      event.startDate = priorStart; event.fuzzy = null; tracks[0].special = true; renderTimeline(); const hammockNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (hammockNode === fuzzyNode || hammockNode.querySelector('.copal-resize-handle')) fail('special track mutation must rebuild manipulation handles'); tracks[0].special = false; renderTimeline();
      const oldWidth = planning.timelineState.dayWidth; planning.timelineState.dayWidth = oldWidth + 2; directRender(); const zoomNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (zoomNode === hammockNode) fail('zoom mutation must rebuild event geometry'); planning.timelineState.dayWidth = oldWidth; directRender();
      const oldRangeStart = planning.timelineState.rangeStart; planning.timelineState.rangeStart = new Date(2026, 8, 2); directRender(); const rangeNode = timelineBody.querySelector('[data-task-id="event-0"]'); if (rangeNode === zoomNode) fail('range mutation must rebuild event geometry'); planning.timelineState.rangeStart = oldRangeStart; directRender();
      planning.timelineState.hiddenTracks.add('track-0'); directRender(); if (nodes().length) fail('hidden-track mutation must remove its event projection'); planning.timelineState.hiddenTracks.delete('track-0'); directRender();
      const priorParent = tracks[1].parentTrackId; const beforeDepth = timelineBody.querySelector('[data-track-id="track-1"]')?.getAttribute('data-track-depth'); tracks[1].parentTrackId = 'track-0'; directRender(); const afterDepth = timelineBody.querySelector('[data-track-id="track-1"]')?.getAttribute('data-track-depth'); if (beforeDepth === afterDepth) fail('track reparent mutation must update hierarchy depth'); tracks[1].parentTrackId = priorParent; directRender();
      event.title = 'Cache invalidation event 29'; renderTimeline(); return { ...unchanged, titlePatch:true, dueDateRebuild:true, trackRebuild:true, sharedTrackRebuild:true, geometryRebuild:true, fuzzyRebuild:true, specialRebuild:true, zoomRebuild:true, rangeRebuild:true, hiddenTrackRebuild:true, reparentRebuild:true }; };
    window.__rendererStages = { timeline:'ready', notes:'cold-ready', interaction:'ready' };
    window.__rendererReady = true;
  })()`) ;
  await until('window.__rendererReady');
  await evaluate('window.__collectRendererSamples(10)');
  await evaluate('window.__collectRendererSamples(10)');
  await evaluate('window.__collectRendererSamples(10)');
  assert.equal(await evaluate('window.__collectRendererInvalidations(30)'), 30);
  const invalidationCheck = await evaluate('window.__checkTimelineInvalidation()');
  const result = await evaluate('window.__getRendererPerf()');
  assert.equal(result.timeline.samples.length, 30); assert.equal(result.notes.samples.length, 30); assert.equal(result.interaction.samples.length, 30);
  assert.equal(result.timeline.events, 4000); assert.equal(result.timeline.tracks, 80); assert.equal(result.notes.notes, 5000); assert.equal(result.invalidation.updates, 30);
  assert.ok(result.notes.entries >= 5000); assert.ok(result.notes.folders >= 500);
  assert.deepEqual(await evaluate('window.__rendererStages'), { timeline:'ready', notes:'cold-ready', interaction:'ready' });
  assert.ok(result.timeline.medianMs <= 200, `Timeline median ${result.timeline.medianMs}ms exceeds 200ms`);
  assert.ok(result.notes.medianMs <= 200, `Notes median ${result.notes.medianMs}ms exceeds 200ms`);
  assert.ok(result.interaction.p95Ms <= 100, `interaction p95 ${result.interaction.p95Ms}ms exceeds 100ms`);
  assert.ok(result.invalidation.p95Ms <= 200, `one-event invalidation p95 ${result.invalidation.p95Ms}ms exceeds 200ms`);
  assert.equal(await evaluate('document.querySelector("[data-task-id=\\"event-0\\"] .copal-event-label")?.textContent'), 'Cache invalidation event 29');
  assert.deepEqual(invalidationCheck, { sameNode:true, scrollLeft:137, active:true, titlePatch:true, dueDateRebuild:true, trackRebuild:true, sharedTrackRebuild:true, geometryRebuild:true, fuzzyRebuild:true, specialRebuild:true, zoomRebuild:true, rangeRebuild:true, hiddenTrackRebuild:true, reparentRebuild:true });
  console.log(JSON.stringify({ measured:result, invalidationCheck }));
  console.log(JSON.stringify({ fixture:'actual production renderers', ...result }));
});
