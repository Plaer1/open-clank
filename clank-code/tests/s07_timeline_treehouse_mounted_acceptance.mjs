#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const ACCOUNT = 's07-timeline-treehouse';
const WORKSPACE = 's07-school';
const GENERATION = 41;
const SOURCE = {
  item_id: 's07-source-item', resource_key: `${ACCOUNT}|${WORKSPACE}|host:source-1`,
  resource_ref: 's07-source-ref', revision: { kind: 'hostFingerprint', value: 'source-r1' },
};

// Anchor the mounted event near the browser's current day so the real
// centered timeline viewport always renders it during this journey.
function dateOnlyOffset(offset) {
  const date = new Date();
  date.setUTCHours(12, 0, 0, 0);
  date.setUTCDate(date.getUTCDate() + offset);
  return date.toISOString().slice(0, 10);
}

const EVENT_START = dateOnlyOffset(-1);
const EVENT_DUE = dateOnlyOffset(1);
const GLOBAL_START = dateOnlyOffset(-30);
const MOVE_DEADLINE = dateOnlyOffset(30);

function json(response, body, status = 200) {
  response.writeHead(status, { 'content-type': 'application/json' });
  response.end(JSON.stringify(body));
  return true;
}

function pageSource() {
  return `<!doctype html><html><body>
    <main id="timeline"></main><main id="treehouse"></main>
    <script type="module">
    (async () => { try {
      const [planningModule, treehouseModule, filesSelection] = await Promise.all([
        import('/static/js/copal/planning.js?s07-mounted'),
        import('/static/js/copal/treehouse.js?s07-mounted'),
        import('/static/js/filesSelectionModel.js?s07-mounted'),
      ]);
      const { configureCopalStorage } = await import('/static/js/copal/storage.js');
      configureCopalStorage('s07-mounted-disposable');
      const h = (tag, attrs = {}, ...children) => {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(attrs)) {
          if (key === 'text') node.textContent = value;
          else if (key === 'class') node.className = value;
          else if (key === 'onclick') node.addEventListener('click', value);
          else if (key === 'style') node.setAttribute('style', value);
          else if (key === 'aria-label') node.setAttribute(key, value);
          else if (key === 'disabled') node.disabled = Boolean(value);
          else if (key !== 'selected') node[key] = value;
          if (key.startsWith('data-')) node.setAttribute(key, String(value));
        }
        node.append(...children.filter(child => child != null).map(child => child instanceof Node ? child : document.createTextNode(String(child))));
        return node;
      };
      const timelineData = { globalStart:'${GLOBAL_START}', moveDeadline:'${MOVE_DEADLINE}', tracks:[{ id:'track-1', name:'Mounted lane', enabled:true, parentTrackId:null, tasks:[{ id:'event-1', title:'Attachable event', startDate:'${EVENT_START}', dueDate:'${EVENT_DUE}', head:'event-head-1', trackId:'track-1', attachments:[] }] }, { id:'track-1-child', name:'Nested lane', enabled:true, parentTrackId:'track-1', tasks:[] }], floatingTodos:[] };
      const planning = planningModule.createPlanningFeature({
        h, api: async (path, options = {}) => { const response = await fetch('/api' + path, { ...options, credentials:'same-origin' }); return response.json(); },
        getPlanning:() => timelineData, refresh:async() => {}, setStatus:text => { window.__timelineStatus = String(text); },
        projectionChanged:result => { if (result?.event) Object.assign(timelineData.tracks[0].tasks[0], result.event); }, openDocument:() => {},
        getScope:() => ({ accountId:'${ACCOUNT}', owner:'${ACCOUNT}', workspace:'${WORKSPACE}', pane:'files-window' }),
      });
      planning.loadState('${WORKSPACE}');
      planning.renderTimeline(document.querySelector('#timeline'), { left:0 });
      window.__planning = planning;
      window.__timelineData = timelineData;
      window.__filesMime = filesSelection.FILES_TRANSFER_MIME;
      window.__validateTimeline = planningModule.validateTimelineFilesDrop;
      window.__validateTreeHouse = treehouseModule.validateTreeHouseFilesDrop;

      const treehouse = treehouseModule.createTreeHouseFeature({
        h, api: async (path, options = {}) => { const response = await fetch('/api' + path, { ...options, credentials:'same-origin' }); return response.json(); },
        setStatus:text => { window.__treehouseStatus = String(text); }, renderMarkdown:text => document.createTextNode(text), openDocument:() => {},
        getScope:() => ({ accountId:'${ACCOUNT}', owner:'${ACCOUNT}', workspace:'${WORKSPACE}', pane:'files-window' }),
      });
      treehouse.loadState();
      await treehouse.render(document.querySelector('#treehouse'));
      window.__treehouse = treehouse;
      window.__fixtureReady = true;
    } catch (error) { window.__fixtureError = String(error?.stack || error); } })();
    </script></body></html>`;
}

test('S07 mounted Timeline and TreeHouse Files attachment qualification', async () => {
  const requests = [];
  const timelinePrepare = new Map();
  const treehousePrepare = new Map();
  let timelineCas = 0;
  let timelineAttachments = [];
  const timelinePatchKinds = [];
  let lessonCas = 0;
  let policyGeneration = GENERATION;
  let sourceReadable = true;
  let timelineHead = 'event-head-1';
  let treehouseRevision = 12;
  const lessonAttachments = [];

  const activity = { id:'lesson-1', courseId:'course-1', moduleId:'module-1', title:'Mounted lesson', activityType:'lesson', status:'published', points:10, content:'Attach a source.', skillIds:[] };
  const course = { id:'course-1', title:'Mounted course', description:'S07', status:'published', moduleIds:['module-1'] };
  const module = { id:'module-1', courseId:'course-1', title:'Module', activityIds:['lesson-1'], assignmentIds:[] };
  const profile = { id:ACCOUNT, displayName:'S07 Editor', roles:['admin','instructor','learner'], active:true };
  const snapshot = () => ({ accountId:ACCOUNT, workspace:WORKSPACE, actor:profile,
    permissions:{ admin:true, author:true, learner:true, analytics:true, grade:true },
    courseCapabilities:{ 'course-1':{ learn:true, edit:true, owner:true, author:true } },
    recipientOptions:[], projection:{ eventCount:0, learners:{ [ACCOUNT]:{ points:0,badges:[],quests:[],streak:0,courses:{},skills:{},pointEvidence:[] } },leaderboard:[],courses:{} },
    state:{ revision:treehouseRevision, profiles:{ [ACCOUNT]:profile }, courses:{ 'course-1':course }, modules:{ 'module-1':module }, activities:{ 'lesson-1':{ ...activity, sourceAttachments:[...lessonAttachments] } }, assignments:{}, skills:{}, badges:{}, quests:{}, courseGrants:{}, enrollments:{}, submissions:{}, evidence:{}, events:[] },
  });

  await withCopalBrowser({ page:pageSource(), request:async (request, response) => {
    const url = new URL(request.url, 'http://fixture');
    const pathname = url.pathname;
    if (!pathname.startsWith('/api/')) return false;
    const bodyChunks = [];
    for await (const chunk of request) bodyChunks.push(chunk);
    const rawBody = Buffer.concat(bodyChunks).toString();
    let body = null; try { body = rawBody ? JSON.parse(rawBody) : null; } catch { body = null; }
    requests.push({ method:request.method, pathname, body });
    if (pathname === '/api/auth/status') return json(response, { username:ACCOUNT });
    if (pathname === '/api/files-v1/roots') return json(response, { policy_generation:policyGeneration, entries:[] });
    if (pathname === '/api/files-v1/resolve-resource') return json(response, { resource:{ ref:'s07-event-target-ref', provider:'copal', kind:'file', capabilities:['read','write'], revision:{ kind:'copalHead', value:timelineHead } } });
    if (pathname === '/api/files-v1/stat') return json(response, { resource:{ ref:'s07-source-ref', provider:'host', kind:'file', name:'source.md', mime_type:'text/markdown', capabilities:sourceReadable ? ['read','open','download','copy'] : [], revision:{ kind:'hostFingerprint', value:'source-r1' } } });
    if (pathname === '/api/files-v1/attachments/prepare') {
      const key = body?.operation_id; const isLesson = body?.target?.kind === 'treehouse_lesson';
      const ledger = isLesson ? treehousePrepare : timelinePrepare;
      if (!ledger.has(key)) {
        const prepared = { operation_id:key, generation:GENERATION, preparation_receipt_id:`prep-${key}`, source_revision:{ kind:'hostFingerprint', value:'source-r1' }, target_revision:isLesson ? { kind:'treehouse', value:JSON.stringify({ grantRevision:0,catalogueRevision:treehouseRevision }) } : { kind:'copalHead', value:timelineHead }, target_identity:isLesson ? { kind:'treehouse_lesson', course_id:'course-1', lesson_id:'lesson-1' } : { kind:'copal_document', resource_ref:'s07-event-target-ref' }, insertion:{ format:'markdown', link_target:`s07://prepared/${key}`, label:'source.md', media_kind:'text/markdown' } };
        ledger.set(key, { prepared, calls:0 });
      }
      const entry = ledger.get(key); entry.calls += 1;
      // Model a response lost after the real adapter has durably prepared it.
      if ((key === 'timeline-attachment-event-1-s07-source-item' || key === 'treehouse-lesson-lesson-1-s07-source-item') && entry.calls === 1) return json(response, { code:'provider_unavailable', message:'response lost' }, 503);
      return json(response, entry.prepared);
    }
    if (pathname.startsWith('/api/files-v1/operations/')) {
      const key = decodeURIComponent(pathname.split('/').pop());
      const entry = timelinePrepare.get(key) || treehousePrepare.get(key);
      return json(response, entry?.prepared || { operation_id:key, generation:policyGeneration, state:'pending', items:[] });
    }
    if (pathname.startsWith('/api/planning/events/')) {
      timelineCas += 1;
      timelinePatchKinds.push(Object.keys(body?.patch || {}));
      if (body?.base !== timelineHead) return json(response, { detail:{ code:'stale', message:'event changed' } }, 409);
      timelineHead = 'event-head-2';
      if (Array.isArray(body?.patch?.attachments)) timelineAttachments = body.patch.attachments;
      const attachments = timelineAttachments;
      return json(response, { event:{ id:'event-1', title:'Attachable event', startDate:EVENT_START, dueDate:EVENT_DUE, head:timelineHead, trackId:'track-1', attachments } });
    }
    if (pathname === '/api/treehouse') return json(response, snapshot());
    if (pathname === '/api/treehouse/commands') {
      if (body?.type === 'lesson.attach_source') {
        lessonCas += 1;
        const attachment = { operationId:body.payload.operationId, preparationReceiptId:body.payload.preparationReceiptId, courseId:'course-1', lessonId:'lesson-1', mode:'link' };
        if (!lessonAttachments.some(item => item.operationId === attachment.operationId)) lessonAttachments.push(attachment);
        treehouseRevision += 1;
        return json(response, { ...snapshot(), result:{ outcome:'committed', ...attachment } });
      }
      return json(response, { ...snapshot(), result:{} });
    }
    return json(response, {});
  } }, async ({ cdp, evaluate, until }) => {
    await until('window.__fixtureReady || window.__fixtureError');
    assert.equal(await evaluate('window.__fixtureError || null'), null);
    assert.equal(await evaluate('!!document.querySelector("[data-timeline-event-id]")'), true, await evaluate('document.querySelector("#timeline").innerHTML'));
    assert.equal(await evaluate('document.querySelector(".copal-treehouse-workspace") !== null'), true);

    const payload = JSON.stringify({ version:1, type:'openclank/files-transfer', kind:'copy', owner:ACCOUNT, workspace:WORKSPACE, pane:'files-window', column:'', provider:'host', parent_ref:'s07-root', query:'', generation:GENERATION, policy_generation:GENERATION, selection_epoch:7, sources:[SOURCE] });
    const drop = await evaluate(`(async () => {
      const data = new DataTransfer(); data.setData(window.__filesMime, ${JSON.stringify(payload)});
      const event = new Event('drop', { bubbles:true, cancelable:true }); Object.defineProperty(event, 'dataTransfer', { value:data });
      document.querySelector('[data-timeline-event-id]').dispatchEvent(event); return { defaultPrevented:event.defaultPrevented, url:location.href };
    })()`);
    assert.equal(drop.defaultPrevented, true);
    await new Promise(resolve => setTimeout(resolve, 300));
    assert.equal(timelinePrepare.has('timeline-attachment-event-1-s07-source-item'), true, JSON.stringify(await evaluate('({status:window.__timelineStatus,html:document.querySelector("[data-timeline-event-id]")?.outerHTML})')));
    assert.equal(timelinePrepare.get('timeline-attachment-event-1-s07-source-item').calls, 1);

    const timelineRetry = await evaluate(`window.__planning.attachTimelineResource(window.__timelineData.tracks[0].tasks[0], ${JSON.stringify(SOURCE)}, { operationId:'timeline-attachment-event-1-s07-source-item' })`);
    assert.equal(timelineRetry.outcome, 'attached');
    assert.equal(timelineCas, 1, 'Timeline applies one event CAS after preparation replay');
    assert.equal((await evaluate('window.__timelineData.tracks[0].tasks[0].attachments.length')), 1);
    assert.equal(timelinePrepare.get('timeline-attachment-event-1-s07-source-item').calls, 2, 'same operation ID reuses the durable preparation');

    const gestureCas = timelineCas;
    const gesturePoints = await evaluate(`(() => { const node=document.querySelector('[data-timeline-event-id]'); const right=node.querySelector('.copal-resize-right'); const scroll=document.querySelector('.copal-timeline-scroll'); const box=el=>{const r=el.getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};}; return { event:box(node), right:box(right), scroll:box(scroll) }; })()`);
    const mouseDrag = async (point, to) => { await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',buttons:1,clickCount:1}); await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:to.x,y:to.y,button:'left',buttons:1,clickCount:1}); await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:to.x,y:to.y,button:'left',buttons:0,clickCount:1}); };
    await mouseDrag(gesturePoints.event, { x:gesturePoints.event.x + 20, y:gesturePoints.event.y });
    const rightAfterMove = await evaluate(`(() => { const r=document.querySelector('[data-timeline-event-id] .copal-resize-right').getBoundingClientRect(); return {x:r.left+r.width/2,y:r.top+r.height/2}; })()`);
    await mouseDrag(rightAfterMove, { x:rightAfterMove.x + 18, y:rightAfterMove.y });
    await cdp('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'ArrowRight',code:'ArrowRight',modifiers:1});
    await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowRight',code:'ArrowRight',modifiers:1});
    await mouseDrag(gesturePoints.scroll, { x:gesturePoints.scroll.x - 30, y:gesturePoints.scroll.y });
    await new Promise(resolve => setTimeout(resolve, 120));
    assert(timelineCas > gestureCas, 'mounted move/resize/nudge gestures remain Timeline semantic commands');
    assert.equal(requests.filter(item => item.pathname === '/api/files-v1/transfers').length, 0, 'Timeline gestures never call Files transfers');
    assert(timelinePatchKinds.some(keys => keys.includes('startDate')), 'move/nudge use the Timeline event patch route');
    assert(timelinePatchKinds.some(keys => keys.includes('dueDate')), 'resize uses the Timeline event patch route');
    assert.equal(await evaluate('document.querySelectorAll(".copal-track").length'), 2, 'nested track remains mounted in Timeline');

    const denied = await evaluate(`window.__planning.attachTimelineResource(window.__timelineData.tracks[0].tasks[0], ${JSON.stringify({ ...SOURCE, kind:'folder' })}, { operationId:'timeline-folder' }).then(() => 'unexpected').catch(error => error.message)`);
    assert.match(denied, /Folders cannot be attached/);
    const badContext = await evaluate(`window.__validateTimeline(${JSON.stringify(payload)}, { accountId:'another-account', workspace:${JSON.stringify(WORKSPACE)} }).reason`);
    assert.equal(badContext, 'This resource belongs to another account.');

    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click(); [...document.querySelectorAll('button')].find(button => button.textContent === 'Open').click()");
    await until('document.querySelector("[data-field-guide-lesson=lesson-1]")');
    const lessonDrop = await evaluate(`(() => {
      const data = new DataTransfer(); data.setData(window.__filesMime, ${JSON.stringify(payload)});
      const event = new Event('drop', { bubbles:true, cancelable:true }); Object.defineProperty(event, 'dataTransfer', { value:data });
      const target=document.querySelector('[data-field-guide-lesson="lesson-1"]'); target.dispatchEvent(event); return event.defaultPrevented;
    })()`);
    assert.equal(lessonDrop, true);
    await until('window.__treehouseStatus?.includes("failed") || window.__treehouseStatus?.includes("lost") || window.__treehouseStatus?.includes("response") || window.__treehouseStatus?.includes("unavailable")');
    assert.equal(treehousePrepare.get('treehouse-lesson-lesson-1-s07-source-item').calls, 1);
    const lessonRetry = await evaluate(`window.__treehouse.attachLessonResource(${JSON.stringify(course)}, ${JSON.stringify(activity)}, ${JSON.stringify(SOURCE)}, { operationId:'treehouse-lesson-lesson-1-s07-source-item' })`);
    assert.equal(lessonRetry.outcome, 'prepared');
    await until('window.__treehouseStatus.includes("attached")');
    assert.equal(lessonCas, 1, 'TreeHouse consumes one exact preparation through one lesson CAS');
    assert.equal(lessonAttachments.length, 1);
    assert.equal(treehousePrepare.get('treehouse-lesson-lesson-1-s07-source-item').prepared.target_identity.lesson_id, 'lesson-1');
    const deniedLesson = await evaluate(`window.__validateTreeHouse(${JSON.stringify(payload)}, { accountId:'another-account', workspace:${JSON.stringify(WORKSPACE)} }).reason`);
    assert.equal(deniedLesson, 'This resource belongs to another account.');
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Learner').click()");
    await until("[...document.querySelectorAll('button')].some(button => button.textContent === 'Open')");
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Open').click()");
    await until('document.querySelector("[data-field-guide-lesson=lesson-1]")');
    const learnerDrop = await evaluate(`(() => { const data=new DataTransfer(); data.setData(window.__filesMime, ${JSON.stringify(payload)}); const event=new Event('drop',{bubbles:true,cancelable:true}); Object.defineProperty(event,'dataTransfer',{value:data}); document.querySelector('[data-field-guide-lesson="lesson-1"]').dispatchEvent(event); return event.defaultPrevented; })()`);
    assert.equal(learnerDrop, true);
    assert.equal(await evaluate('window.__treehouseStatus'), 'Learner access cannot attach a source to this lesson.');
    assert.equal(treehousePrepare.get('treehouse-lesson-lesson-1-s07-source-item').calls, 2, 'learner denial does not dispatch another Files preparation');
    await evaluate("[...document.querySelectorAll('button')].find(button => button.textContent === 'Admin').click()");

    sourceReadable = false;
    const unreadable = await evaluate(`window.__treehouse.attachLessonResource(${JSON.stringify(course)}, ${JSON.stringify(activity)}, ${JSON.stringify({ ...SOURCE, item_id:'unreadable' })}, { operationId:'lesson-unreadable' }).then(() => 'unexpected').catch(error => error.message)`);
    assert.match(unreadable, /cannot be read/);
    sourceReadable = true;
    policyGeneration += 1;
    const stalePolicy = await evaluate(`window.__planning.attachTimelineResource(window.__timelineData.tracks[0].tasks[0], ${JSON.stringify(SOURCE)}, { operationId:'timeline-policy-stale', gestureContext:{ raw:${JSON.stringify(payload)}, owner:${JSON.stringify(ACCOUNT)}, accountId:${JSON.stringify(ACCOUNT)}, workspace:${JSON.stringify(WORKSPACE)}, pane:'files-window', generation:${GENERATION}, policyGeneration:${GENERATION}, selectionEpoch:7, provider:'host', resourceKey:${JSON.stringify(SOURCE.resource_key)}, resourceRef:${JSON.stringify(SOURCE.resource_ref)}, sourceRevision:${JSON.stringify(SOURCE.revision)} } }).then(() => 'unexpected').catch(error => error.message)`);
    assert.match(stalePolicy, /policy|generation|access/i);

    const before = await evaluate('({ transferRequests:0, attachmentCount:document.querySelectorAll("[data-timeline-event-id]").length })');
    await evaluate('window.__planning.suspendScope(); window.__treehouse.suspendScope(); document.querySelector("#timeline").replaceChildren(); document.querySelector("#treehouse").replaceChildren()');
    await new Promise(resolve => setTimeout(resolve, 60));
    const after = await evaluate('({ timelineEvents:document.querySelectorAll("[data-timeline-event-id]").length, lessonRows:document.querySelectorAll("[data-field-guide-lesson]").length })');
    assert.equal(requests.filter(item => item.pathname === '/api/files-v1/transfers').length, 0, 'Timeline/TreeHouse attachment and semantic gestures never dispatch Files transfers');
    assert.deepEqual(after, { timelineEvents:0, lessonRows:0 });
    console.log(JSON.stringify({ mounted:true, timeline:{ preparationCalls:timelinePrepare.get('timeline-attachment-event-1-s07-source-item').calls, cas:timelineCas, attachments:before.attachmentCount }, treehouse:{ preparationCalls:treehousePrepare.get('treehouse-lesson-lesson-1-s07-source-item').calls, cas:lessonCas, attachments:lessonAttachments.length }, lifecycle:after }));
  });
});
