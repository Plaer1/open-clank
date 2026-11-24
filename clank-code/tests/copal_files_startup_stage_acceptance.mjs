#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const page = `<!doctype html><meta charset="utf-8"><body><script type="module">
  window.__filesStageEvents = [];
  window.__rootProjectionEvents = [];
  let lastRootList = null;
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (input, init = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const isOptionalCatalog = ['/api/files-v1/places','/api/files-v1/workspaces'].includes(url.pathname);
    const catalogMode = window.__catalogMode || 'initial';
    if (isOptionalCatalog && catalogMode !== 'initial') url.searchParams.set('fixture_catalog', catalogMode);
    let requestBody = null;
    try { requestBody = typeof init.body === 'string' ? JSON.parse(init.body) : null; } catch {}
    const event = { path:url.pathname, query:url.search, parentRef:requestBody?.parent_ref || null, start:performance.now(), end:null };
    window.__filesStageEvents.push(event);
    const requestInit = isOptionalCatalog && catalogMode === 'retirement'
      ? { ...init, signal:undefined }
      : init;
    const response = await nativeFetch(isOptionalCatalog && catalogMode !== 'initial' ? url.toString() : input, requestInit);
    event.end = performance.now();
    return response;
  };
  const takeProjection = () => {
    const roots = document.querySelector('[data-files-tree]')?.querySelectorAll('.files-tree-item').length || 0;
    const favorites = document.querySelector('[data-files-favorites]')?.querySelectorAll('.files-tree-item').length || 0;
    const rootText = document.querySelector('[data-files-tree]')?.textContent || '';
    const rootList = document.querySelector('[data-files-tree]')?.firstElementChild || null;
    const now = performance.now();
    if (roots && rootList && rootList !== lastRootList) {
      lastRootList = rootList;
      window.__rootProjectionEvents.push({ at:now, count:roots, text:rootText });
      if (!window.__filesRootProjection) {
        window.__filesRootProjection = { at:now, count:roots };
        window.__filesRootStageElement = rootList;
      }
    }
    if (favorites && !window.__filesFavoriteProjection) window.__filesFavoriteProjection = { at:now, count:favorites };
  };
  new MutationObserver(takeProjection).observe(document.body, { childList:true, subtree:true });
  import { initCustomContextMenu } from '/static/js/custom-context-menu.js';
  await initCustomContextMenu();
  const files = await import('/static/js/files.js');
  const browser = files.createFilesBrowser({ id:'wb00-startup-stages', pickerMode:true });
  window.__filesStartup = { browser, startedAt:performance.now(), done:false, error:null };
  browser.open().then(() => { window.__filesStartup.done = true; }, error => { window.__filesStartup.error = String(error); window.__filesStartup.done = true; });
  window.__runRetirementProbe = async () => {
    const firstBrowser = browser;
    const selectedRow = firstBrowser.sidebar.querySelector('[data-tree-resource-id="host-note"] [data-explorer-row]');
    selectedRow?.click();
    const selectedAfterOptional = firstBrowser.getSelection().map(item => item.resource_id);
    const sameTreeAfterOptional = firstBrowser.sidebar.querySelector('[data-files-tree]')?.firstElementChild === window.__filesRootStageElement;
    firstBrowser.dispose();
    window.__catalogMode = 'retirement';
    const projectionCount = window.__rootProjectionEvents.length;
    const lateBrowser = files.createFilesBrowser({ id:'wb00-retired-catalogs', pickerMode:true });
    const lateOpen = lateBrowser.open();
    const favorites = lateBrowser.sidebar.querySelector('[data-files-favorites]');
    const workspaces = lateBrowser.sidebar.querySelector('[data-files-workspaces]');
    for (let i=0; i<500 && window.__rootProjectionEvents.length <= projectionCount; i += 1) await new Promise(resolve => setTimeout(resolve, 10));
    const rootReached = window.__rootProjectionEvents.length > projectionCount;
    const favoritesBeforeRetire = favorites.querySelectorAll('.files-tree-item').length;
    const workspacesBeforeRetire = workspaces.querySelectorAll('.files-tree-item').length;
    const retiredAt = performance.now();
    lateBrowser.dispose();
    await lateOpen;
    const retirementRequests = () => window.__filesStageEvents.filter(event => event.query.includes('fixture_catalog=retirement'));
    for (let i=0; i<600 && retirementRequests().filter(event => event.end != null).length < 2; i += 1) await new Promise(resolve => setTimeout(resolve, 10));
    await new Promise(resolve => setTimeout(resolve, 30));
    return {
      selectedAfterOptional, sameTreeAfterOptional, rootReached, favoritesBeforeRetire, workspacesBeforeRetire,
      favoritesAfterLate:favorites.querySelectorAll('.files-tree-item').length,
      workspacesAfterLate:workspaces.querySelectorAll('.files-tree-item').length,
      favoritesDisconnected:!favorites.isConnected,
      lateResponseTimes:retirementRequests().filter(event => event.end != null && event.end > retiredAt).map(event => ({path:event.path,end:event.end})),
    };
  };
</script>`;

const measurements = [];
const t0 = Date.now();
const delays = { '/api/files-v1/roots':45, '/api/files-v1/children:host':110, '/api/files-v1/children:library':70, '/api/files-v1/places':240, '/api/files-v1/workspaces':330 };
const roots = [
  { id:'host-root', resource_id:'host-root', resource_ref:'rr-host', provider:'host', kind:'folder', name:'Host files', capabilities:['children','read'], sort_keys:['name'] },
  { id:'library-root', resource_id:'library-root', resource_ref:'rr-library', provider:'library', kind:'folder', name:'Library', capabilities:['children','read'], sort_keys:['name'] },
];
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
await withCopalBrowser({ page, request:async (request, response) => {
  const url = new URL(request.url, 'http://fixture');
  const path = url.pathname;
  if (!path.startsWith('/api/')) return false;
  const started = Date.now();
  const catalogMode = url.searchParams.get('fixture_catalog') || 'initial';
  let body = null;
  if (request.method === 'POST') { const chunks=[]; for await (const chunk of request) chunks.push(chunk); try { body=JSON.parse(Buffer.concat(chunks).toString()); } catch {} }
  let delayMs = 0, payload = {};
  if (path === '/api/auth/status') payload = { username:'wb00-startup-owner' };
  else if (path === '/api/files-v1/roots') {
    delayMs=delays[path];
    payload={ entries:roots, policy_generation:7, import_capabilities:{ fixture:'preserved' } };
  }
  else if (path === '/api/files-v1/children') {
    const parent = String(body?.parent_ref || '');
    const key = parent === 'rr-host' ? 'host' : parent === 'rr-library' ? 'library' : 'other';
    delayMs=delays[`${path}:${key}`] || 0;
    const entries = key === 'host' ? [
      { id:'host-project', resource_id:'host-project', resource_ref:'rr-host-project', provider:'host', kind:'folder', name:'Project files', capabilities:['children','read'], sort_keys:['name'] },
      { id:'host-note', resource_id:'host-note', resource_ref:'rr-host-note', provider:'host', kind:'file', name:'Retain me.md', mime_type:'text/markdown', capabilities:['open','read','stat'] },
    ] : [];
    payload={ entries, next_cursor:null, sort_keys:['name'] };
  }
  else if (path === '/api/files-v1/places') {
    delayMs=catalogMode === 'retirement' ? 900 : catalogMode === 'selection' ? 450 : delays[path];
    const name=catalogMode === 'retirement' ? 'Retired favorite' : catalogMode === 'selection' ? 'Selection favorite' : 'Pinned project';
    payload={ entries:[{ place_id:'place-0123456789abcdef0123456789abcdef', name, resource_id:`${catalogMode}-favorite`, resource_ref:`rr-${catalogMode}-favorite`, provider:'host', kind:'folder', capabilities:['children','read'] }] };
  }
  else if (path === '/api/files-v1/workspaces') {
    delayMs=catalogMode === 'retirement' ? 1100 : catalogMode === 'selection' ? 550 : delays[path];
    payload={ entries:catalogMode === 'initial' ? [] : [{ workspace:{ id:`${catalogMode}-workspace`, name:`${catalogMode} workspace` }, availability:'available', resource:{ id:`${catalogMode}-workspace-resource`, ref:`rr-${catalogMode}-workspace`, provider:'host', kind:'folder', name:`${catalogMode} workspace`, capabilities:['children','read'] } }] };
  }
  else { response.writeHead(404, { 'content-type':'application/json' }); response.end(JSON.stringify({ message:'Unexpected fixture endpoint' })); return true; }
  await wait(delayMs);
  const finished = Date.now();
  measurements.push({ endpoint:path, mode:catalogMode, parent:body?.parent_ref || null, delayMs, startMs:started-t0, endMs:finished-t0, elapsedMs:finished-started });
  response.writeHead(200, { 'content-type':'application/json' }); response.end(JSON.stringify(payload)); return true;
} }, async ({ cdp, evaluate, until }) => {
  await cdp('Emulation.setDeviceMetricsOverride', { width:1280, height:900, deviceScaleFactor:1, mobile:false });
  await until('Boolean(window.__filesStartup)');
  await until('window.__filesRootProjection || window.__filesStartup.error');
  const beforeOptional = await evaluate(`(() => {
    return { roots:document.querySelector('[data-files-tree]')?.querySelectorAll('.files-tree-item').length || 0, places:document.querySelector('[data-files-favorites]')?.children.length || 0, workspaces:document.querySelector('[data-files-workspaces]')?.children.length || 0, done:window.__filesStartup.done };
  })()`);
  const folderNavigation = await evaluate(`(() => {
    const row = document.querySelector('[data-tree-resource-id="host-project"] [data-explorer-row]');
    if (!row) return { clicked:false };
    row.click();
    return { clicked:true };
  })()`);
  assert.equal(folderNavigation.clicked, true, 'newly projected Host folder is navigable while optional catalogs are pending');
  await until("window.__filesStageEvents.some(event => event.path === '/api/files-v1/children' && event.parentRef === 'rr-host-project' && event.end != null)");
  await until('window.__filesStartup.done || window.__filesStartup.error');
  const dom = await evaluate(`({ roots:document.querySelector('[data-files-tree]')?.querySelectorAll('.files-tree-item').length || 0, rootText:document.querySelector('[data-files-tree]')?.textContent || '', sameTree:document.querySelector('[data-files-tree]')?.firstElementChild === window.__filesRootStageElement, favorites:document.querySelector('[data-files-favorites]')?.querySelectorAll('.files-tree-item').length || 0, workspaces:document.querySelector('[data-files-workspaces]')?.children.length || 0, currentDirectory:window.__filesStartup.browser.getCurrentDirectory()?.resourceId || null, status:document.querySelector('.files-pane-status')?.textContent || '' })`);
  const stages = await evaluate(`({ events:window.__filesStageEvents, rootProjection:window.__filesRootProjection || null, favoriteProjection:window.__filesFavoriteProjection || null })`);
  const totals = await evaluate(`({ openMs:performance.now()-window.__filesStartup.startedAt, error:window.__filesStartup.error, requestCount:window.__filesStageEvents.length })`);
  assert.equal(totals.error, null, `production Files startup completed: ${totals.error}`);
  assert(measurements.some(item => item.endpoint.endsWith('/roots')) && measurements.some(item => item.endpoint.endsWith('/children')));
  assert(measurements.some(item => item.endpoint.endsWith('/places') && item.elapsedMs >= 200));
  assert(measurements.some(item => item.endpoint.endsWith('/workspaces') && item.elapsedMs >= 300));
  assert(stages.rootProjection && stages.favoriteProjection, `the actual module projects authorized Locations and Places-backed Favorites: ${JSON.stringify(stages)}`);
  const firstEnd = path => stages.events.find(item => item.path === path)?.end || 0;
  const requiredReadyMs = Math.max(firstEnd('/api/files-v1/roots'), firstEnd('/api/files-v1/children'));
  const optionalGateMs = Math.max(firstEnd('/api/files-v1/places'), firstEnd('/api/files-v1/workspaces'));
  assert(stages.rootProjection.at >= requiredReadyMs, `root projection follows authenticated Roots and Host children (root=${stages.rootProjection.at.toFixed(1)}ms, required=${requiredReadyMs.toFixed(1)}ms)`);
  assert(stages.rootProjection.at < optionalGateMs, `root projection precedes delayed optional catalogs (root=${stages.rootProjection.at.toFixed(1)}ms, optional=${optionalGateMs.toFixed(1)}ms)`);
  assert(beforeOptional.roots >= 1 && beforeOptional.done === false, `required Roots/Host project before optional completion: ${JSON.stringify(beforeOptional)}`);
  assert.equal(dom.sameTree, true, 'optional catalog updates do not remount the projected Locations tree');
  assert.equal(dom.currentDirectory, 'host-project', 'same-scope user navigation remains selected after optional catalogs settle');
  assert(dom.favorites >= 1, `Places-backed Favorites render after optional Places stage: ${JSON.stringify(dom)}`);
  assert(dom.roots >= 1, `authorized roots render after navigation stage: ${JSON.stringify(dom)}`);
  const lifetime = await evaluate('window.__runRetirementProbe()');
  assert.deepEqual(lifetime.selectedAfterOptional, ['host-note'], 'Locations selection remains usable after optional catalogs settle');
  assert.equal(lifetime.sameTreeAfterOptional, true, 'the selected Locations tree remains the projected tree after optional catalogs settle');
  assert.equal(lifetime.rootReached, true, 'retirement probe reaches the required projection');
  assert(lifetime.lateResponseTimes.some(item => item.path.endsWith('/places')) && lifetime.lateResponseTimes.some(item => item.path.endsWith('/workspaces')), `optional responses resolve after retirement: ${JSON.stringify(lifetime)}`);
  assert.equal(lifetime.favoritesAfterLate, lifetime.favoritesBeforeRetire, 'late Places result cannot mutate retired Favorites');
  assert.equal(lifetime.workspacesAfterLate, lifetime.workspacesBeforeRetire, 'late Workspaces result cannot mutate retired Workspaces');
  assert.equal(lifetime.favoritesDisconnected, true);
  console.log(JSON.stringify({ passed:'bounded Files staged startup, same-scope navigation, and catalog retirement', optionalWaitSnapshot:beforeOptional, folderNavigation, stageProjection:stages, requiredReadyMs, optionalGateMs, dom, totals, lifetime, requests:measurements }, null, 2));
});
