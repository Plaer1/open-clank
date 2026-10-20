#!/usr/bin/env node

import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const definition = {
  version:1,
  views:[{ id:'all', name:'All sources', type:'table', columns:[{property:'file.name', label:'Name'}, {property:'score', label:'Score'}], sorts:[{property:'score', direction:'asc'}], filters:[], summaries:{}, limit:10000 }],
};
const sourceDocs = Array.from({ length:5_001 }, (_, index) => ({ id:`source-${index}`, kind:'note', name:`Source ${String(index).padStart(4, '0')}.md`, head:`head-${index}`, text:`# Source ${index}\r\n\r\nBody ${index}\r\n`, properties:{score:index}, relations:[], tags:[], links:[] }));
const base = { id:'base-1', kind:'base', name:'All sources.base', head:'base-head-1', text:JSON.stringify(definition), properties:{}, relations:[], tags:[], links:[] };
const otherDocs = [{ id:'other-source-1', kind:'note', name:'Other owner.md', head:'other-head-1', text:'# Other owner\n', properties:{score:1}, relations:[], tags:[], links:[] }];
const state = { account:'account-owner', queries:0, writes:0, sourceRevision:'base-head-1', eventResponse:null, delayPage:false };
const activeSources = () => state.account === 'account-owner' ? sourceDocs : otherDocs;
const activeDocs = () => [base, ...activeSources()];
const sourceById = (id) => activeSources().find((doc) => doc.id === id);
const page = `<!doctype html><meta charset="utf-8"><body><nav><a href="/copal/bases" data-copal-view="bases">Bases</a></nav><main id="app"></main><script type="module">window.__run=async()=>{const module=await import('/static/js/copal.js?base-perf');await module.init(location.origin);window.__copal=module.default;};</script>`;

function json(res, value, status=200) { res.writeHead(status, {'content-type':'application/json'}); res.end(JSON.stringify(value)); return true; }
async function readBody(req) { const chunks=[]; for await (const chunk of req) chunks.push(chunk); return chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : {}; }

await withCopalBrowser({ page, request:async (req, res) => {
  const url = new URL(req.url, 'http://fixture');
  if (url.pathname === '/api/copal/status') return json(res, { storage_namespace:`base-performance-${state.account}`, account_id:state.account });
  if (url.pathname === '/api/prefs/copal_entry_visibility') return json(res, { value:{} });
  if (url.pathname === '/api/copal/planning') return json(res, { tracks:[], floatingTodos:[] });
  if (url.pathname === '/api/copal/events') { res.writeHead(200, {'content-type':'text/event-stream', 'cache-control':'no-cache'}); res.write(': fixture\n\n'); state.eventResponse = res; return true; }
  if (url.pathname === '/api/copal/documents' && req.method === 'GET') return json(res, { docs:activeDocs() });
  if (url.pathname === '/api/copal/documents/base-1' && req.method === 'GET') return json(res, { ...base, head:state.sourceRevision });
  if (url.pathname === '/api/copal/documents/base-1' && req.method === 'PUT') {
    const body = await readBody(req); state.writes += 1; state.sourceRevision = 'base-head-2'; base.text = body.content; base.head = state.sourceRevision;
    return json(res, { outcome:'committed', doc:{...base, head:state.sourceRevision} });
  }
  if (url.pathname === '/api/copal/bases/base-1/transform' && req.method === 'POST') {
    const body = await readBody(req); const next = JSON.parse(String(body.source || base.text));
    const view = next.views?.find((item) => item.id === body.command?.view_id) || next.views?.[0];
    if (body.command?.action === 'set-sort' && view) {
      const property = String(body.command.property || ''); const direction = String(body.command.direction || 'asc');
      view.sorts = direction === 'none' ? [] : [{ property, direction }];
    }
    return json(res, { changed:true, definition:next, source:`${JSON.stringify(next, null, 2)}\n`, revision:'base-head-2', definitionRevision:1 });
  }
  const sourceMatch = url.pathname.match(/^\/api\/copal\/documents\/(source-\d+)(?:\/rename)?$/);
  if (sourceMatch && req.method === 'PUT') {
    const doc = sourceById(sourceMatch[1]); if (!doc) return json(res, { detail:'missing' }, 404);
    const body = await readBody(req); doc.text = body.content; doc.head = `${doc.head}-edited`; return json(res, { outcome:'committed', doc });
  }
  if (sourceMatch && req.method === 'DELETE') {
    const index = activeSources().findIndex((doc) => doc.id === sourceMatch[1]); if (index < 0) return json(res, { detail:'missing' }, 404);
    activeSources().splice(index, 1); return json(res, { outcome:'committed', id:sourceMatch[1] });
  }
  if (sourceMatch && req.method === 'POST' && url.pathname.endsWith('/rename')) {
    const doc = sourceById(sourceMatch[1]); if (!doc) return json(res, { detail:'missing' }, 404);
    const body = await readBody(req); doc.name = body.name; doc.head = `${doc.head}-renamed`; return json(res, { outcome:'committed', doc });
  }
  if (url.pathname === '/api/copal/bases/base-1/query') {
    state.queries += 1;
    const pageNumber = Number(url.searchParams.get('page') || 1); const pageSize = Number(url.searchParams.get('page_size') || 100); if (state.delayPage && pageNumber === 3) await new Promise((resolve) => setTimeout(resolve, 180));
    const sources = activeSources(); const start=(pageNumber-1)*pageSize; const rows=sources.slice(start,start+pageSize).map(doc=>({ documentId:doc.id, head:doc.head, name:doc.name, kind:doc.kind, values:{'file.name':doc.name, score:doc.properties.score}, errors:[] }));
    return json(res, { base:{id:'base-1', name:base.name, head:state.sourceRevision}, definition, diagnostics:[], view:definition.views[0], rows, groups:[], summaries:{}, page:pageNumber, pageSize, total:sources.length, pages:Math.ceil(sources.length/pageSize), sourceCount:sources.length, sourceTruncated:false, queryComplete:true, matchedCount:sources.length, resultLimit:10000, resultLimited:false, summaryScope:'limitedResult' });
  }
  if (url.pathname === '/api/test-account' && req.method === 'POST') { state.account = (await readBody(req)).account; return json(res, { account:state.account }); }
  if (url.pathname === '/api/test-notify') { for (const revision of ['r3', 'r1', 'r3']) state.eventResponse?.write(`event: document\ndata: ${JSON.stringify({ id:'source-0', revision })}\n\n`); return json(res, { sent:3 }); }
  if (url.pathname === '/api/test-delay-page') { state.delayPage = Boolean((await readBody(req)).enabled); return json(res, { delayPage:state.delayPage }); }
  if (url.pathname === '/api/test-state') return json(res, { account:state.account, queries:state.queries, writes:state.writes, sourceRevision:state.sourceRevision, sourceCount:activeSources().length });
  return false;
}}, async ({ evaluate, until }) => {
  await until('document.readyState === "complete"');
  await evaluate('window.__run()');
  await evaluate('window.__copal.open("bases")');
  await until('(()=>{const surface=document.querySelector(".copal-sheet-surface[data-sheet-status=ready]");return Boolean(surface && surface.getAttribute("aria-busy") !== "true" && surface.querySelector(".copal-sheet-grid") && surface.querySelector(".copal-sheet-footer"));})()');
  const initial = await evaluate('(()=>{const surface=document.querySelector(".copal-sheet-surface");const grid=surface?.querySelector(".copal-sheet-grid");return { footer:surface?.querySelector(".copal-sheet-footer")?.textContent || "", rows:grid?.querySelectorAll("tbody tr").length || 0, rowCount:grid?.getAttribute("aria-rowcount"), columns:[...grid?.querySelectorAll("thead th") || []].map((node)=>node.dataset.sheetColumnKey) };})()');
  assert.match(initial.footer, /5001 matched/); assert.equal(initial.rows, 100); assert.equal(initial.rowCount, '5001'); assert.deepEqual(initial.columns, ['file.name', 'score']);
  const timings = [];
  for (let index=0; index<3; index += 1) { const timing = await evaluate(`(async()=>{const start=performance.now();await window.__copal.open('bases');return performance.now()-start})()`); timings.push(timing); }
  const indexTimings = await evaluate(`(async()=>{const values=[];for(let index=0;index<3;index+=1){const start=performance.now();const response=await fetch('/api/copal/documents?hidden=include');const value=await response.json();if(value.docs.length!==5002)throw new Error('source index is incomplete');values.push(performance.now()-start);}return values})()`);
  const queryTimings = await evaluate(`(async()=>{const values=[];for(let index=0;index<3;index+=1){const start=performance.now();const response=await fetch('/api/copal/bases/base-1/query?page=1&page_size=100');if(!response.ok)throw new Error('Base query failed');await response.json();values.push(performance.now()-start);}return values})()`);
  const saveMs = await evaluate(`(async()=>{const start=performance.now();document.querySelector('.copal-sheet-tool[data-sheet-action="sort"]')?.click();const menu=await new Promise((resolve,reject)=>{const deadline=performance.now()+3000;const tick=()=>{const value=document.querySelector('.copal-sheet-toolbar-menu[open]');if(value)return resolve(value);if(performance.now()>deadline)return reject(new Error('sort menu did not open'));requestAnimationFrame(tick);};tick();});const apply=menu.querySelector('button.primary');if(!apply)throw new Error('sort apply button missing');apply.click();while((await (await fetch('/api/test-state')).json()).writes < 1) await new Promise(requestAnimationFrame);return performance.now()-start})()`);
  await until('fetch("/api/test-state").then(response=>response.json()).then(state=>state.writes === 1)');
  const final = await evaluate('fetch("/api/test-state").then(response=>response.json())');
  assert.equal(final.writes, 1, JSON.stringify(final)); assert.equal(final.sourceRevision, 'base-head-2', JSON.stringify(final)); assert.equal(final.queries >= 4, true, JSON.stringify(final));
  const memory = await evaluate('({ jsHeapBytes:performance.memory?.usedJSHeapSize ?? null, domNodes:document.querySelectorAll("*").length })');
  assert.ok(memory.domNodes > 100, JSON.stringify(memory));
  await evaluate("fetch('/api/test-delay-page',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({enabled:true})})");
  const staleNavigation = await evaluate(`(async()=>{const delayed=fetch('/api/copal/bases/base-1/query?page=3&page_size=100').then(response=>response.json());await new Promise(r=>setTimeout(r,15));const current=await (await fetch('/api/copal/bases/base-1/query?page=1&page_size=100')).json();const stale=await delayed;return { currentPage:current.page, stalePage:stale.page, currentFirst:current.rows[0]?.documentId, staleFirst:stale.rows[0]?.documentId };})()`);
  assert.equal(staleNavigation.currentPage, 1, JSON.stringify(staleNavigation)); assert.equal(staleNavigation.stalePage, 3, JSON.stringify(staleNavigation));
  await evaluate("fetch('/api/test-delay-page',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({enabled:false})})");
  await new Promise((resolve) => setTimeout(resolve, 1300));
  const beforeNotify = (await evaluate('fetch("/api/test-state").then(response=>response.json())')).queries;
  await evaluate('fetch("/api/test-notify")');
  await new Promise((resolve) => setTimeout(resolve, 450));
  const afterNotify = (await evaluate('fetch("/api/test-state").then(response=>response.json())')).queries;
  assert.equal(afterNotify - beforeNotify, 1, `notifications requery count: ${beforeNotify} -> ${afterNotify}`);
  await evaluate(`fetch('/api/copal/documents/source-0',{method:'PUT',headers:{'content-type':'application/json'},body:JSON.stringify({content:'# Edited source\\r\\n\\r\\nChanged\\r\\n'})})`);
  await evaluate(`fetch('/api/copal/documents/source-1/rename',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({name:'Renamed source.md'})})`);
  await evaluate(`fetch('/api/copal/documents/source-2',{method:'DELETE'})`);
  const afterLifecycleRequests = await evaluate('fetch("/api/test-state").then(response=>response.json())');
  assert.equal(afterLifecycleRequests.sourceCount, 5000, JSON.stringify(afterLifecycleRequests));
  await evaluate('window.__copal.open("bases")');
  await new Promise((resolve) => setTimeout(resolve, 500));
  const lifecycle = await evaluate('(async()=>{const value=await (await fetch("/api/copal/documents?hidden=include")).json();return { count:value.docs.filter(doc=>doc.kind!=="base").length, renamed:value.docs.some(doc=>doc.id==="source-1"&&doc.name==="Renamed source.md"), edited:value.docs.some(doc=>doc.id==="source-0"&&doc.text.includes("Edited source")), deleted:!value.docs.some(doc=>doc.id==="source-2")};})()');
  assert.equal(lifecycle.count, 5000); assert.equal(lifecycle.renamed, true); assert.equal(lifecycle.edited, true); assert.equal(lifecycle.deleted, true);
  await evaluate(`fetch('/api/test-account',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({account:'account-other'})})`);
  await evaluate('window.__copal.init(location.origin)');
  await evaluate('window.__copal.open("bases")');
  await until('document.querySelector(".copal-sheet-footer")?.textContent.includes("1 matched")');
  const other = await evaluate('({ count:document.querySelector(".copal-sheet-footer")?.textContent || "", ownerVisible:document.body.textContent.includes("Source 0000") })');
  assert.match(other.count, /1 matched/); assert.equal(other.ownerVisible, false);
  await evaluate(`fetch('/api/test-account',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({account:'account-owner'})})`);
  await evaluate('window.__copal.init(location.origin)');
  await evaluate('window.__copal.open("bases")');
  await until('document.querySelector(".copal-sheet-footer")?.textContent.includes("5000 matched")');
  const restored = await evaluate('fetch("/api/test-state").then(response=>response.json())');
  assert.equal(restored.account, 'account-owner'); assert.equal(restored.sourceCount, 5000);
  console.log(JSON.stringify({ fixture:'production Base renderer', sources:5_001, coldAndWarmOpenMs:timings, indexMs:indexTimings, queryMs:queryTimings, saveToViewMs:saveMs, memory, notificationRequeries:afterNotify-beforeNotify, lifecycle, accountSwitch:{ otherSources:1, restoredSources:restored.sourceCount }, queryCount:restored.queries, complete:true }));
});
