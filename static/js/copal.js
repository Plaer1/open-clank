import { recordPresentation, acknowledgeVisible, achievementOwner } from './achievementProducer.js';
import { uiIcon } from './uiIcons.js';
import { openCalendar } from './calendar.js';
import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort, canonicalSourceProperty, setFrontmatterProperty, flattenFilterToLines, hasNestedFilterGroups, parseFilterLines, removeBaseView, reorderBaseView, makeViewFromTemplate, VIEW_TYPES, parseDataviewQuery, reorderBaseColumn } from './copal/bases.js';
import { createMarkdownEditor, createSourceEditor } from './copal/codemirror.js';
import { createNotesFeature } from './copal/notesFeature.js';
import { createBufferRegistry } from './copal/documentBuffers.js';
import { createWikiWorkspace } from './copal/wikiWorkspace.js';
import { databaseRelations, moveHeadingSection, moveHeadingSectionTo, outlineEntries, reparentHeading } from './copal/notesModel.js';
import { createPlanningFeature } from './copal/planning.js';
import { createTreeHouseFeature } from './copal/treehouse.js';
import { createCopalWindow } from './copal/windows.js';
import { wireDialog } from './copal/overlays.js';
import { configureCopalStorage, copalStorageKey } from './copal/storage.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { saveResourceSnapshot } from './codeEditor.js';
import { prepareDocumentSave, commitDocumentSave, sameSaveScope } from './copal/documentSave.js';
import { cloneEnvelope, normalizeResourceHandle, sameResourceKey, snapshotEnvelope } from './copal/resourceModel.js';
import { documentGraph, galaxyGraph, graphStorageKey, normalizeGraphState, headingEntries, headingTree, renameHeading, changeHeadingLevel, deleteHeadingSection, reparentHeadingSection, moveHeadingSection as moveGraphHeadingSection, GRAPH_MODES, structureEntries, structureTree, deriveFacets, matchesFilters, filterDocuments, reconcileFilters, officialDocsRoot, isOfficialDocument, facetCacheKey, mergeFacets } from './copal/graphModel.js';
import { canHandleInput } from './copal/inputContext.js';
import { extractReferenceSection } from './copal/markdownResources.js';
import { createMarkdownRenderer, registerAppDestination } from './copal/markdownRenderer.js';
import { navigationIntentFromEvent } from './copal/notesWorkspace.js';
import { createSpellingService } from './copal/spelling.js';
import { appletPath, updateAppletRoute, resolveAppletLocation } from './appletRoutes.js';
import { registerAdapter, createCodeMirrorContextAdapter } from './custom-context-menu.js';
import { styledConfirm, styledPrompt } from './ui.js';

const VIEWS = ['notes', 'wiki', 'timeline', 'graph', 'treehouse', 'achievements', 'todo'];
// Mind is not a separate destination.  Its label is kept only as the legacy
// deep-link alias text; Graph owns both views and Mind opens structure mode.
const LABELS = { notes: 'Editor', wiki: 'Wiki', timeline: 'Timeline', galaxy: 'Galaxy', graph: 'Graph', mind: 'Graph', bases: 'Bases', treehouse: 'TreeHouse', achievements: 'Achievements', todo: 'Meatbag Tasks' };
// Code and Notes remain accepted route/DOM aliases, but only the canonical
// Editor destination participates in visible Copal launcher preferences.
const COPAL_LAUNCHER_IDS = [...VIEWS.filter((view) => view !== 'notes'), 'notes', 'files', 'imps', 'calendar'];
const LAUNCHER_LABELS = { ...LABELS, code: 'Editor', files: 'Files', imps: 'Image Editor', calendar: 'Calendar' };
const MEMES_MIME = 'application/vnd.openclank.memes+json';
const HIDDEN_KINDS = new Set(['asset', 'planning', 'calendar-projection', 'treehouse-state', 'copal-tracks', 'copal-migration']);
const state = {
  api: '', workspace: 'default', storageNamespace: null, view: 'notes', docs: [], taskProjection: [], taskProjectionLoaded: false, taskCursor: null, taskSnapshotRevision: null, taskProjectionTotal: null, taskLoading: false, taskQueryToken: 0, taskQuery: { query:'', completed:null, source:'all', sourceFilter:'all', statusFilter:'all', hideDone:false }, taskSelectedId: null, planning: { tracks: [], floatingTodos: [] }, selected: null,
  filter: '', reading: false, saveTimers: new Map(),
  calendarMonth: null, windows: new Map(),
  root: null, content: null, body: null,
  treehouseSection: 'courses', treehouseLesson: null,
  title: null, status: null, search: null, events: null, reloadTimer: null, ignoreEventsUntil: 0, documentLoadToken: 0,
  projectedPlanningHead: null,
  baseId: null, baseView: null, basePage: 1, basePageSize: 100, baseQueryToken: 0, baseDefinition: null,
  baseSourceDocs: new Map(), baseFocusRow: -1, baseFocusCol: -1, baseFocusTable: null,
  noteEditors: new Set(),
  filesMutationEvents: null, filesMutationBridgeInstallations: 0,
  entryVisibility: null,
  entryVisibilityError: null,
  accountId: null, contextEpoch: 0,
  navigationEvents: null,
};
let planningFeature = null;
let notesFeature = null;
let wikiFeature = null;
let wikiWorkspace = null;
const sharedDocumentRegistry = createBufferRegistry();
let sharedDocumentState = null;
function documentState() {
  const scope = JSON.stringify(saveScope());
  if (sharedDocumentState?.scope !== scope) sharedDocumentState = { scope };
  return sharedDocumentState;
}
function featureFor(view) { return view === 'wiki' ? wikiFeature : notesFeature; }


function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'icon') { /* mounted after the text attributes below */ }
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) node.setAttribute(key, '');
    else if (value !== false && value != null) node.setAttribute(key, String(value));
  }
  if (attrs.icon) {
    const icon = document.createElement('span');
    icon.innerHTML = uiIcon(attrs.icon, 14, { role:'inherit', style:attrs.text ? 'margin-right:4px;' : '' });
    node.prepend(icon);
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function persistActiveContext() {
  const context = state.windows.get(state.view);
  if (!context) return;
  context.selected = state.selected;
  context.filter = state.filter;
  context.reading = state.reading;
}

function activateView(view) {
  if (state.view !== view) persistActiveContext();
  const context = state.windows.get(view);
  if (!context) return null;
  state.view = view;
  state.root = context.window.root;
  state.content = context.window.content;
  state.body = context.window.body;
  state.title = context.window.heading;
  state.status = context.window.status;
  state.search = context.search;
  state.selected = context.selected || null;
  state.filter = context.filter || '';
  state.reading = !!context.reading;
  return context;
}

async function api(path, options = {}, workspace = state.workspace) {
  const epoch = state.contextEpoch;
  const separator = path.includes('?') ? '&' : '?';
  const mutating = options.method && options.method !== 'GET';
  if (mutating) state.ignoreEventsUntil = Date.now() + 5000;
  const contentHeaders = options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' };
  const response = await fetch(`${state.api}/api/copal${path}${separator}workspace=${encodeURIComponent(workspace)}`, {
    ...options,
    headers: { ...contentHeaders, ...(state.accountId ? { 'X-Copal-Account':state.accountId } : {}), ...(options.headers || {}) },
  });
  if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active Copal account changed.'), { code:'stale_scope' });
  if (response.ok) {
    const value = await response.json();
    if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active Copal account changed.'), { code:'stale_scope' });
    if (mutating) {
      state.ignoreEventsUntil = Date.now() + 1000;
      clearTimeout(state.reloadTimer);
    }
    return value;
  }
  const error = await response.json().catch(() => ({}));
  if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active Copal account changed.'), { code:'stale_scope' });
  const detail = error.detail;
  const message = typeof detail === 'string'
    ? detail
    : detail?.outcome === 'stale'
      ? 'A newer version exists.'
      : detail?.diagnostics?.[0]?.message || detail?.message || 'Copal operation failed.';
  const failure = new Error(message);
  failure.status = response.status;
  failure.detail = detail;
  throw failure;
}

async function attachmentTextHash(value) {
  const bytes = new TextEncoder().encode(String(value || ''));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((item) => item.toString(16).padStart(2, '0')).join('');
}

async function uploadAttachment({ actionId, documentId, name, mime, bytes, content, sourceText, base, caption = '' }) {
  const raw = bytes instanceof Uint8Array ? bytes : new Uint8Array(await bytes.arrayBuffer());
  let binary = '';
  for (let offset = 0; offset < raw.length; offset += 0x8000) binary += String.fromCharCode(...raw.subarray(offset, offset + 0x8000));
  const sourceTextHash = await attachmentTextHash(sourceText);
  try {
    return await api('/attachments', {
      method:'POST',
      body:JSON.stringify({ actionId, documentId, name, mime, contentBase64:btoa(binary), content, base, caption, prepareOnly:true, sourceTextHash }),
    });
  } catch (error) {
    // A provider may have created the asset before the POST response was
    // lost. Reconcile through the typed lifecycle status once; never replay
    // the upload blindly and never fall back to client-side insertion.
    if (error?.name === 'AbortError' || (Number(error?.status) >= 400 && Number(error?.status) < 500 && Number(error?.status) !== 408 && Number(error?.status) !== 429)) throw error;
    try {
      const recovered = await api(`/attachments/${encodeURIComponent(actionId)}/status`);
      const preparation = recovered && ['staged', 'consumed'].includes(String(recovered.phase || '').toLowerCase())
        && recovered.action_id === actionId && recovered.document_id === documentId
        && recovered.base === base && recovered.source_text_hash === sourceTextHash
        && recovered.asset_id && recovered.asset_name
        ? recovered : null;
      if (preparation) return { outcome:'prepared', actionId, asset:{ id:preparation.asset_id, name:preparation.asset_name }, preparation };
    } catch (_) { /* original transport error remains the visible failure */ }
    throw error;
  }
}

async function commitAttachment({ actionId, documentId, content, base, sourceTextHash, assetId, assetName }) {
  return api('/attachments/commit', {
    method:'POST', body:JSON.stringify({ actionId, documentId, content, base, sourceTextHash, assetId, assetName }),
  });
}

async function abortAttachment(actionId) {
  return api(`/attachments/${encodeURIComponent(actionId)}`, { method:'DELETE' });
}

function setStatus(message, bad = false) {
  const context = state.windows.get(state.view);
  if (context) context.window.setStatus(message, bad);
}

function normalizeName(name) {
  return String(name || '').replace(/\.md$/i, '').toLowerCase();
}

function findByName(name) {
  const wanted = normalizeName(name);
  return state.docs.find((doc) => normalizeName(doc.name) === wanted || normalizeName(doc.name.split('/').pop()) === wanted);
}

function planningData() {
  return state.planning || { tracks: [], floatingTodos: [] };
}

function visibleDocs(corpus) {
  const query = state.filter.trim().toLowerCase();
  return state.docs.filter((doc) => {
    if (HIDDEN_KINDS.has(doc.kind)) return false;
    if (corpus === 'wiki' && doc.kind !== 'wiki') return false;
    if (corpus === 'notes' && doc.kind === 'wiki') return false;
    return !query || doc.name.toLowerCase().includes(query) || String(doc.text || '').toLowerCase().includes(query);
  });
}

function projectionChanged(payload) {
  const projection = payload?.calendar_projection;
  if (!projection?.enabled || projection.ok === false) return;
  window.dispatchEvent(new CustomEvent('calendar-refresh'));
}

async function reconcileCalendarProjection(force = false) {
  const revision = [state.planning?.trackRegistry?.head, ...allPlanningTasks().map((event) => event.head)].filter(Boolean).sort().join(':');
  if (!force && revision && revision === state.projectedPlanningHead) return;
  const result = await api('/calendar/reconcile', {
    method: 'POST',
    body: JSON.stringify({}),
  });
  const projection = result.projections?.[0];
  if (projection?.enabled && projection.ok !== false) {
    state.projectedPlanningHead = revision || projection.sourceRevision || 'projected';
    window.dispatchEvent(new CustomEvent('calendar-refresh'));
  }
}

async function loadDocuments(render = true) {
  const scope = saveScope();
  const loadToken = ++state.documentLoadToken;
  // SSE refreshes, explicit restores, and view activation can overlap. Only
  // the newest request may project its snapshot into the workspace; otherwise
  // an older index response can put a restored document back on a stale head.
  const current = () => loadToken === state.documentLoadToken && sameSaveScope(scope, saveScope());
  setStatus('Loading…');
  try {
    let [result, planning] = await Promise.all([api('/documents?hidden=include', {}, scope.workspace), api('/planning', {}, scope.workspace)]);
    if (!current()) return;
    if (planning.migrationRequired) {
      setStatus('Saved Timeline notes need explicit workspace migration. Their source is preserved.', true);
    }
    state.docs = (result.docs || []).map((doc) => notesFeature?.projectDocument?.(doc, scope) || doc);
    state.taskProjection = [];
    state.taskProjectionLoaded = false;
    state.taskCursor = null;
    state.taskSnapshotRevision = null;
    state.taskProjectionTotal = null;
    state.taskIndexedTotal = null;
    state.taskMatchedTotal = null;
    state.taskTotalExact = false;
    state.taskQueryToken += 1;
    state.planning = planning || { tracks: [], floatingTodos: [] };
    if (state.selected && !state.docs.some((doc) => doc.id === state.selected)) state.selected = null;
    if (render) {
      const active = state.view;
      for (const [view, context] of state.windows) if (context.window.visible) renderView(view);
      activateView(active);
    }
    for (const [view, context] of state.windows) context.window.setStatus(planning.migrationRequired ? 'Saved Timeline notes need explicit workspace migration; source preserved.' : view === 'achievements' ? '' : `${state.docs.length} documents`);
    reconcileCalendarProjection().catch(() => {});
  } catch (error) {
    if (!current()) return;
    setStatus(error.message, true);
    if (render && state.body) state.body.replaceChildren(h('div', { class: 'copal-empty', text: error.message }));
  }
}

function updateRoute(view, replace = false) {
  const selected = state.windows.get(view)?.selected || (state.view === view ? state.selected : null);
  // Canonical direct applet address — never the legacy /copal prefix. The
  // live rewrite keeps unconsumed one-shot query tokens (TreeHouse share) so
  // this normalization cannot eat a deep link before its consumer accepts it.
  const opts = { doc: selected || undefined };
  if (view === 'graph') opts.mode = getGraphView().mode;
  updateAppletRoute(view, opts, replace);
}

function markActive() {
  document.querySelectorAll('[data-copal-view]').forEach((link) => {
    const visible = state.windows.get(link.dataset.copalView)?.window.visible;
    link.classList.toggle('active', !!visible);
    if (link.dataset.copalView === state.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

async function open(view = 'notes', push = true) {
  if (view === 'calendar') {
    if (push || location.pathname.startsWith('/calendar')) {
      history.replaceState({}, '', '/calendar');
    }
    openCalendar();
    return;
  }
  const requestedView = String(view || 'notes').toLowerCase();
  const legacyWikiHome = requestedView === 'wiki';
  const openingBases = requestedView === 'bases' || requestedView === 'base';
  view = resolveView(view);
  const scope = saveScope();
  const context = ensureViewWindow(view);
  activateView(view);
  localStorage.setItem(copalStorageKey('odysseus-copal-view'), view);
  context.window.show(document.activeElement);
  markActive();
  if (push) updateRoute(view);
  if (!state.docs.length) await loadDocuments();
  else { renderView(view); reconcileCalendarProjection().catch(() => {}); }
  // `/copal/bases` remains a bookmark-compatible alias. Select the requested
  // Base before the shared Editor workspace normalizes its first leaf.
  if (openingBases && view === 'notes') {
    const preferredId = state.baseId || (context.selected && state.docs.find((doc) => doc.id === context.selected && doc.kind === 'base')?.id);
    const base = state.docs.find((doc) => doc.kind === 'base' && doc.id === preferredId) || state.docs.find((doc) => doc.kind === 'base');
    if (base) { state.baseId = base.id; notesFeature?.open(base.id, { reuse:true }); context.selected = base.id; state.selected = base.id; renderView(view); }
  }
  if (!sameSaveScope(scope, saveScope())) return;
  context.window.focus();
  const surface = { notes:'editor', graph:'graph', treehouse:'treehouse' }[view];
  if (surface) acknowledgeVisible(context.window.body, 'surface.visited', { surface, visited:true }, { accountId:state.accountId, workspaceId:state.workspace });
}

function close(view = state.view, push = true, fromManager = false) {
  clearTimeout(state.saveTimer);
  const context = state.windows.get(view);
  if (context) context.window.requestClose(fromManager);
  markActive();
  if (push) history.pushState({}, '', '/');
}

/** Help opens one canonical handbook in the chosen applet presentation. */
async function openClankHandbook({ presentation = 'copal' } = {}) {
  const view = presentation === 'wiki' ? 'wiki' : 'notes';
  const scope = saveScope();
  const home = () => state.docs.find((doc) => doc.readOnly === true && isOfficialDocument(doc)
    && String(doc.officialRef || doc.properties?.docId || '') === 'openclank-docs-home');
  // Installed article bodies can advance while an applet stays open. Help
  // refreshes through the normal index path, which preserves personal drafts.
  await loadDocuments(false);
  if (!sameSaveScope(scope, saveScope())) return null;
  const match = home();
  if (!match || !notesFeature) return null;
  // Both entry points address the installed article, with independent
  // window presentations over the shared canonical document core.
  openDocument(match.id, view, true, { mode:'reading', revealHandbook:true });
  return match.id;
}
window.openClankHandbook = openClankHandbook;

function openDocument(id, view = state.view, push = true, options = {}) {
  const doc = state.docs.find((item) => item.id === id);
  const context = ensureViewWindow(view);
  if (['notes', 'wiki'].includes(view) && featureFor(view)) {
    activateView(view);
    context.window.show(document.activeElement);
    featureFor(view).open(id, options);
    if (push) updateRoute(view);
    context.window.focus();
    return;
  }
  context.selected = id;
  activateView(view);
  context.window.show(document.activeElement);
  state.selected = id;
  persistActiveContext();
  if (push) updateRoute(view);
  renderView(view);
}

async function handleContextObjectCommand(command, target) {
  const node = target?.closest?.('[data-copal-context-object]') || target;
  if (!node) return false;
  const sheet = node.closest?.('.copal-sheet-surface');
  if (sheet?.__openClankSheetContextCommand && ['edit-sheet-cell', 'clear-sheet-cell', 'copy-sheet-cell', 'edit-sheet-column'].includes(command)) return Boolean(await sheet.__openClankSheetContextCommand(command, node));
  if (String(command).startsWith('table-') && typeof window.__openClankTableContextCommand === 'function') return Boolean(await window.__openClankTableContextCommand(command, node));
  if (command === 'edit-track' || command === 'edit-event' || command === 'toggle-task' || command === 'open-task') {
    return Boolean(await planningFeature?.handleContextCommand?.(command, node));
  }
  if (command === 'open-treehouse-item') {
    return Boolean(await treeHouse?.handleContextCommand?.(command, node));
  }
  if (command === 'open-file') {
    // Notes Explorer rows are Copal documents, even though they share the
    // Files object vocabulary.  Keep the captured document identity when the
    // Files adapter is mounted alongside the Editor.
    const documentId = String(node.dataset?.documentId || '').trim();
    if (documentId) { openDocument(documentId, 'notes'); return true; }
    if (typeof window.__openClankFilesContextCommand === 'function' && await window.__openClankFilesContextCommand(command, node)) return true;
    const resourceRef = String(node.dataset?.resourceRef || '').trim();
    if (resourceRef) { await openResource(resourceRef); return true; }
    const path = String(node.dataset?.path || '').trim();
    if (!path) return false;
    const code = window.codeEditorModule || await import('./codeEditor.js');
    await code.openPath(path);
    return true;
  }
  if (command === 'open-base-document') {
    const id = String(node.dataset?.documentId || '').trim();
    if (!id) return false;
    openDocument(id, 'notes');
    return true;
  }
  if (['edit-sheet-cell', 'clear-sheet-cell', 'copy-sheet-cell', 'edit-sheet-column'].includes(command) && typeof window.__openClankSheetContextCommand === 'function') {
    return Boolean(await window.__openClankSheetContextCommand(command, node));
  }
  if (command === 'copy-file-path') {
    if (typeof window.__openClankFilesContextCommand === 'function' && await window.__openClankFilesContextCommand(command, node)) return true;
    const value = String(node.dataset?.path || node.dataset?.resourceRef || '').trim();
    if (!value || !navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
    await navigator.clipboard.writeText(value);
    return true;
  }
  if (command === 'open-image' && node instanceof HTMLImageElement) {
    window.open(node.currentSrc || node.src, '_blank', 'noopener,noreferrer');
    return true;
  }
  if (command === 'copy-image-address' && node instanceof HTMLImageElement) {
    const value = node.currentSrc || node.src;
    if (!navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
    await navigator.clipboard.writeText(value);
    return true;
  }
  if (command === 'copy-image-bytes' && node instanceof HTMLImageElement) {
    if (!navigator.clipboard?.write || typeof window.ClipboardItem !== 'function') return false;
    const response = await fetch(node.currentSrc || node.src);
    if (!response.ok) throw new Error('The image could not be read for copying.');
    const blob = await response.blob();
    await navigator.clipboard.write([new ClipboardItem({ [blob.type || 'application/octet-stream']: blob })]);
    return true;
  }
  if (command === 'open-graph-node') {
    const id = String(node.dataset?.graphId || '');
    const view = getGraphView();
    const projection = view.mode === 'galaxy' ? galaxyGraph(planningData().tracks || [], allPlanningTasks(planningData())) : documentGraph(visibleDocs(), findByName);
    const graphNode = projection.nodes.find((item) => item.id === id);
    if (!graphNode) return false;
    if (graphNode.task) editPlanningTask(graphNode.task.primaryTrackId, graphNode.task.id);
    else if (graphNode.track) planningFeature.openTrackEditor(graphNode.track.id, node);
    else if (graphNode.doc) openDocument(graphNode.doc.id, graphNode.kind === 'wiki' ? 'wiki' : 'notes');
    return true;
  }
  return false;
}

function saveScope() {
  return { workspace:state.workspace, storageNamespace:state.storageNamespace, accountId:state.accountId, epoch:state.contextEpoch };
}

async function refreshIndexedDocument(id, scope = saveScope()) {
  const fresh = await api(`/documents/${encodeURIComponent(id)}`, {}, scope.workspace);
  if (!sameSaveScope(scope, saveScope())) return fresh;
  const projected = notesFeature?.projectDocument?.(fresh, scope) || fresh;
  const index = state.docs.findIndex((doc) => doc.id === id);
  if (index >= 0) state.docs[index] = projected;
  else state.docs.push(projected);
  return fresh;
}

function setViewStatus(view, message, bad = false) {
  state.windows.get(view)?.window.setStatus(message, bad);
}

function showDocumentConflict(doc, localContent, remote, view, submittedSnapshot = null) {
  const scope = saveScope();
  const snapshot = notesFeature?.getDraftSnapshot?.(doc.id) || submittedSnapshot;
  const envelope = structuredClone(snapshot?.envelope || { text:localContent, properties:doc.properties || {}, relations:doc.relations || [], ...(doc.extensions == null ? {} : { extensions:doc.extensions }) });
  localContent = String(envelope.text || '');
  const current = () => sameSaveScope(scope, saveScope());
  const draftUnchanged = () => !snapshot || notesFeature?.getDraftSnapshot?.(doc.id)?.localRevision === snapshot.localRevision;
  document.querySelector(`#copal-conflict-${CSS.escape(doc.id)}`)?.close();
  const dialog = h('dialog', { id:`copal-conflict-${doc.id}`, class:'copal-dialog copal-conflict-dialog' },
    h('h2', { text:`Resolve conflict · ${doc.name}` }),
    h('p', { text:'The saved version changed since this edit began. Your draft is preserved; compare both versions and choose explicitly.' }),
    h('p', { class:'copal-dialog-hint', text:`Your base: ${doc.head || 'unknown'} · latest: ${remote.head || 'unknown'}` }));
  const comparison = h('div', { class:'copal-conflict-comparison' },
    h('section', {}, h('h3', { text:'Your unsaved version' }), h('pre', { text:localContent }), h('pre', { text:JSON.stringify({ properties:envelope.properties, relations:envelope.relations }, null, 2) })),
    h('section', {}, h('h3', { text:'Latest saved version' }), h('pre', { text:String(remote.text || '') }), h('pre', { text:JSON.stringify({ properties:remote.properties || {}, relations:remote.relations || [] }, null, 2) })));
  const close = () => dialog.close();
  const checkDraft = () => {
    if (!current()) { close(); return false; }
    if (draftUnchanged()) return true;
    close();
    showDocumentConflict(doc, localContent, remote, view);
    setViewStatus(view, 'Your draft changed. Review the updated comparison before resolving.', true);
    return false;
  };
  dialog.append(comparison, h('div', { class:'copal-dialog-actions' },
    h('button', { class:'copal-btn', text:'Keep editing mine', onclick:close }),
    h('button', { class:'copal-btn', text:'Copy mine', onclick:async() => { await navigator.clipboard.writeText(localContent); setViewStatus(view, 'Copied your unsaved version'); } }),
    h('button', { class:'copal-btn', text:'Copy latest', onclick:async() => { await navigator.clipboard.writeText(String(remote.text || '')); setViewStatus(view, 'Copied latest saved version'); } }),
    h('button', { class:'copal-btn', text:'Load latest', onclick:async() => {
      if (!checkDraft()) return;
      close();
      try {
        const fresh = await refreshIndexedDocument(doc.id, scope);
        if (!current()) return;
        if (!draftUnchanged() || fresh.head !== remote.head) {
          showDocumentConflict(doc, localContent, fresh, view);
          setViewStatus(view, 'The comparison changed. Review the latest versions.', true);
          return;
        }
        if (notesFeature?.acceptSavedDocument(doc.id, String(fresh.text || ''), { expectedLocalRevision:snapshot?.localRevision, document:fresh }) === false) return;
        if (state.windows.get('notes')?.window.visible) renderView('notes');
        setViewStatus(view, 'Loaded latest saved version');
      } catch (error) { if (current()) setViewStatus(view, error.message, true); }
    } }),
    h('button', { class:'copal-btn primary', text:'Save mine over latest', onclick:async() => {
      if (!checkDraft()) return;
      close();
      if (snapshot) {
        const saved = await notesFeature?.retrySaveAtRevision?.(doc.id, snapshot.localRevision, { kind:'copalHead', value:remote.head });
        if (saved != null) {
          if (current() && state.windows.get('notes')?.window.visible) renderView('notes');
          return;
        }
      }
      const rebased = snapshot ? { ...snapshot, expectedRevision:{ kind:'copalHead', value:remote.head }, envelope } : null;
      const receipt = await saveDocument({ ...doc, ...envelope, head:remote.head }, localContent, false, view, { snapshot:rebased, scope, returnReceipt:true });
      if (!current() || receipt?.outcome !== 'applied') return;
      const accepted = notesFeature?.acceptSavedDocument(doc.id, localContent, { expectedLocalRevision:snapshot?.localRevision, document:receipt.doc, acknowledge:true });
      if (accepted === false) setViewStatus(view, 'The reviewed version was saved; your newer edits remain unsaved.');
      if (state.windows.get('notes')?.window.visible) renderView('notes');
    } })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
}

async function saveDocument(doc, content, rerender = false, view = state.view, options = {}) {
  const scope = options.scope || saveScope();
  const current = () => sameSaveScope(scope, saveScope());
  if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
  // Base definitions share the same source resource queue as Notes. Existing
  // callers can continue to await a boolean; the queue owns CAS/action
  // identity and serializes commands per resource. `viaBuffer` prevents the
  // queue's own save callback from re-entering this seam.
  if (!options.viaBuffer && (doc?.kind === 'base' || doc?.resource?.representation === 'base')) {
    const queued = await notesFeature?.queueDocumentSave?.(doc, content, {
      flush:!['notes', 'wiki'].includes(view),
      snapshot:options.snapshot || null,
      baseRevision:options.baseRevision ?? null,
    });
    if (options.returnReceipt) return queued && typeof queued === 'object' ? queued : queued ? { outcome:'applied' } : { outcome:'failed' };
    return !!queued;
  }
  setViewStatus(view, 'Saving…');
  let receipt = null, writeStarted = false;
  try {
    const prepared = prepareDocumentSave(doc, content, { snapshot:options.snapshot, scope, relationsFor:(text) => databaseRelations(text, state.docs) });
    content = prepared.payload.content;
    receipt = await commitDocumentSave(prepared, (id, payload) => { writeStarted = true; return api(`/documents/${encodeURIComponent(id)}`, {
      method:'PUT', body:JSON.stringify(payload),
      headers:scope.accountId ? { 'X-Copal-Account':scope.accountId } : {},
    }, scope.workspace); });
    if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
    if (receipt.outcome !== 'applied') throw Object.assign(new Error('A newer version exists.'), { status:409, detail:{ doc:receipt.remote } });
    const result = receipt.result;
    doc.head = receipt.revision.value;
    // A failed projection read must never turn a committed write into a failed
    // save or make the buffer retry against its previous head.
    try { await refreshIndexedDocument(doc.id, scope); } catch (_) { /* next invalidation retries the read */ }
    if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
    if (/^---\n[\s\S]*?^copal_type:\s*["']?event["']?\s*$/m.test(content)) {
      const planning = await api('/planning', {}, scope.workspace);
      if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
      state.planning = planning;
    }
    projectionChanged(result);
    if (doc.kind === 'planning' && result.calendar_projection?.ok !== false) state.projectedPlanningHead = doc.head;
    const taskProjection = receipt.projections?.tasks;
    setViewStatus(view, taskProjection && taskProjection.status !== 'ready' ? 'Saved · indexing pending' : 'Saved');
    const active = state.view;
    for (const [openView, context] of state.windows) if (openView !== view && context.window.visible) renderView(openView);
    if (rerender) renderView(view);
    activateView(active);
    return options.returnReceipt ? receipt : true;
  } catch (error) {
    if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
    if (receipt?.outcome === 'applied') {
      setViewStatus(view, 'Saved · view refresh pending');
      return options.returnReceipt ? receipt : true;
    }
    if (error.status === 409 && error.detail?.doc) {
      setViewStatus(view, 'Conflict: nothing overwritten; choose how to resolve it.', true);
      if (!options.sheet) showDocumentConflict(doc, content, error.detail.doc, view, options.snapshot);
      return options.returnReceipt ? { outcome:'conflict', remote:error.detail.doc } : false;
    }
    setViewStatus(view, error.message, true);
    const status = Number(error.status || 0);
    const retryable = error.retryable !== false && !(status >= 400 && status < 500);
    return options.returnReceipt ? { outcome:'failed', code:error.code, retryable, status, uncertain:writeStarted && (status === 0 || status >= 500 || [408, 429].includes(status) || error.code === 'receipt_identity'), message:error.message } : false;
  }
}

function convertPluginBlock(source, lang) {
  if (lang !== 'dataview') {
    // Non-dataview blocks: create as note (existing behavior)
    showForm('Convert plugin block', [
      ['name', 'Base name', `${lang}-converted.base`],
    ], async ({ name }) => {
      const properties = { source_block: lang, original_source: source };
      const relations = databaseRelations(source, state.docs);
      await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'note', content:source, properties, relations }) });
      await loadDocuments(false);
      setStatus(`Created ${name} from ${lang} block`);
    });
    return;
  }
  // D2: Dataview → Base conversion with preview
  const parsed = parseDataviewQuery(source);
  const dialog = h('dialog', { class: 'copal-dialog' });
  const name = h('input', { value: 'Dataview.base', 'aria-label': 'Base name' });
  let previewBody;
  if (parsed) {
    const cols = parsed.fields.map((f) => h('th', { text: f.label }));
    const headerRow = h('tr', {}, ...cols);
    previewBody = h('div', {},
      h('p', { text: `Type: ${parsed.type}` }),
      h('p', { text: `Columns: ${parsed.fields.map((f) => f.label).join(', ')}` }),
      parsed.folder ? h('p', { text: `Source folder: ${parsed.folder}` }) : null,
      parsed.filter ? h('p', { text: `Filter: ${JSON.stringify(parsed.filter)}` }) : null,
      h('table', { class: 'copal-table', style: 'margin-top:8px' }, h('thead', {}, headerRow), h('tbody', {}, h('tr', {}, ...parsed.fields.map(() => h('td', { text: '—' }))))),
      h('p', { class: 'copal-base-diagnostic', text: 'Rows will be populated from live documents on creation.' }),
    );
  } else {
    previewBody = h('p', { class: 'copal-base-diagnostic', text: 'Could not parse this Dataview query. A default Base will be created.' });
  }
  dialog.append(h('h2', { text: 'Convert Dataview to Base' }), name, previewBody);
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create Base', onclick: async () => {
      const safeName = name.value.trim().endsWith('.base') ? name.value.trim() : `${name.value.trim()}.base`;
      if (!safeName || safeName === '.base') return;
      const def = makeDefaultBase(safeName.replace(/\.base$/i, ''));
      if (parsed && parsed.fields.length) {
        def.views[0].columns = parsed.fields.map((f) => ({ property: f.property, label: f.label }));
        if (parsed.filter) def.views[0].filters = parsed.filter;
      }
      try {
        await api('/documents', { method:'POST', body:JSON.stringify({ name: safeName, kind:'base', content: serializeBase(def) }) });
        await loadDocuments(false);
        dialog.close();
        setStatus(`Created Base ${safeName} from dataview block`);
      } catch (error) { setStatus(error.message, true); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus(); name.select();
}

// Resolve Host media through owner-bound Files refs, never a guessed host URL.
async function resolveHostCommentAsset(reference, origin) {
  if (origin?.resource?.key?.provider !== 'host') return null;
  let path;
  try {path=decodeURIComponent(String(reference.target || '').split('#')[0]);} catch {return null;}
  if (!path || path.startsWith('/') || path.includes('\\') || /^[a-z][a-z\d+.-]*:/i.test(path) || !/\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico|mp3|wav|ogg|m4a|flac|aac|mp4|webm|mov|m4v|pdf)$/i.test(path)) return null;
  const parts=path.split('/').filter(part=>part && part!=='.');
  if (!parts.length || parts.length>32) return null;
  const scope=saveScope(), originalParent=origin.hostParentResourceRef;
  const sourceRef=origin.resourceRef || origin.resource?.locator?.opaqueRef;
  let parentRef=originalParent;
  const current=()=>sameSaveScope(scope,saveScope()) && origin.hostParentResourceRef===originalParent;
  if (!parentRef && sourceRef) parentRef=(await filesFacadeClient.reveal(sourceRef))?.parent?.ref;
  if (!parentRef || !current()) return null;
  for (let index=0;index<parts.length;index++) {
    const name=parts[index];
    if (name==='..') {parentRef=(await filesFacadeClient.reveal(parentRef))?.parent?.ref;if(!parentRef||!current())return null;continue;}
    let cursor=null, match=null;
    // Exact names are selected only from currently authorized child pages.
    for(let pageIndex=0;pageIndex<20;pageIndex++) {
      const page=await filesFacadeClient.children(parentRef,{query:name,limit:200,cursor});
      if(!current())return null;
      const matches=(page.entries || []).filter(item=>item.name===name);
      if(matches.length>1 || (match && matches.length))return null;
      if(matches.length)match=matches[0];
      cursor=page.next_cursor || null;
      if(!cursor)break;
      if(pageIndex===19)throw new Error('Media folder lookup is incomplete; retry after narrowing the path.');
    }
    if(!match?.ref)return null;
    const has=cap=>Array.isArray(match.capabilities)?match.capabilities.includes(cap):match.capabilities?.[cap]===true;
    if(index<parts.length-1){if(!has('children'))return null;parentRef=match.ref;continue;}
    if(!has('preview'))return null;
    return {target:{kind:'asset',name:match.name,mimeType:match.mime_type,resourceRef:match.ref},url:filesFacadeClient.contentUrl(match.ref,{purpose:'preview'})};
  }
  return null;
}

const markdownRenderer = createMarkdownRenderer({
  h,
  documents:() => state.docs,
  findByName,
  resolveAsset:resolveHostCommentAsset,
  assetUrl:target => target.resourceRef ? null : `${state.api}/api/copal/assets/${encodeURIComponent(target.id)}?workspace=${encodeURIComponent(state.workspace)}`,
  openTarget:(target, fragment, event) => {
    if (target?.kind === 'app-destination-error') {
      setStatus(target.name || 'Unknown app destination.', true);
      return;
    }
    const view = target.kind === 'wiki' ? 'wiki' : 'notes';
    const intent = navigationIntentFromEvent(event);
    // S17: Wiki opens are Editor documents and honor the shared navigation
    // intent (L-S15-WIKI-INTENT).
    openDocument(target.id, view, true, { intent });
    if (fragment && ['notes', 'wiki'].includes(view)) {
      const section = extractReferenceSection(target.text, fragment);
      if (section.status === 'resolved') featureFor(view)?.focusSourceLine(target.id, section.line);
      else setStatus(`${section.status === 'ambiguous' ? 'Ambiguous' : 'Missing'} section: ${fragment}`, true);
    }
    const cache = [...(state.windows.get(view)?.noteLeafViews?.values() || [])].find(item => item.docId === target.id && item.root?.isConnected && item.root.getClientRects().length);
    return { documentId:cache?.docId, element:cache?.root };
  },
  onConvertPluginBlock:convertPluginBlock,
});
const { renderMarkdown, renderPreview, renderReference } = markdownRenderer;

// Register the default app destinations. Screens open/focus and preserve
// drafts; they never toggle a window closed or start an unrelated chat.
const resolvedElement = (element, documentId = null) => ({ element, documentId, destinationExists:!!element });
registerAppDestination('chat', () => {
  const input = document.getElementById('message');
  if (!input || !window.sessionModule?.getCurrentSessionId?.()) return null;
  input.focus({ preventScroll:true }); input.scrollIntoView?.({ block:'nearest' });
  return resolvedElement(input);
});
registerAppDestination('settings', async ({ panel }) => {
  if (typeof window.settingsModule?.open !== 'function') return null;
  await window.settingsModule.open(panel || 'appearance');
  return resolvedElement(document.getElementById('settings-modal'));
});
registerAppDestination('files', async () => {
  if (typeof window.filesModule?.open !== 'function') return null;
  await window.filesModule.open();
  return resolvedElement(document.getElementById('files-window'));
});
const resolveLinkedAppDocument = async ({ panel, event, screen }) => {
  const doc = panel ? state.docs.find(item => item.id === panel || item.name === panel)
    : state.docs.find(item => item.id === state.selected && (screen !== 'wiki' || item.kind === 'wiki')) || state.docs.find(item => screen !== 'wiki' || item.kind === 'wiki');
  if (!doc || doc.kind === 'asset') return null;
  const view = screen === 'wiki' ? 'wiki' : 'notes';
  openDocument(doc.id, view, true, { intent:navigationIntentFromEvent(event) });
  const cache = [...(state.windows.get(view)?.noteLeafViews?.values() || [])].find(item => item.docId === doc.id && item.root?.isConnected && item.root.getClientRects().length);
  return resolvedElement(cache?.root, cache?.docId);
};
registerAppDestination('editor', resolveLinkedAppDocument);
registerAppDestination('wiki', resolveLinkedAppDocument);
for (const [screen, view] of [['graph','graph'], ['galaxy','graph'], ['achievements','achievements'], ['treehouse','treehouse'], ['timeline','timeline'], ['tasks','todo']]) {
  registerAppDestination(screen, async () => {
    const context = ensureViewWindow(view); activateView(view); context.window.show(document.activeElement);
    await renderView(view); return resolvedElement(context.window.body);
  });
}
registerAppDestination('memory', () => {
  const opener = document.getElementById('tool-memory-btn') || document.getElementById('rail-memory');
  if (!opener) return null;
  const modal = document.getElementById('memory-modal');
  if (!modal || modal.classList.contains('hidden')) opener.click();
  return resolvedElement(modal);
});


async function showHistory(doc) {
  if (doc?.readOnly && doc?.resourceRef) {
    setStatus('History is not available for a read-only Files resource.', true);
    return;
  }
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: `History · ${doc.name}` }));
  const list = h('div', { text: 'Loading…' });
  dialog.append(list, h('div', { class: 'copal-dialog-actions' }, h('button', { class: 'copal-btn', text: 'Close', onclick: () => dialog.close() })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
  try {
    const result = await api(`/documents/${doc.id}/history`);
    list.replaceChildren();
    for (const change of result.changes || []) {
      const restore = h('button', { class: 'copal-btn', text: 'Restore', onclick: async () => {
        const restored = await api(`/documents/${doc.id}/restore`, { method: 'POST', body: JSON.stringify({ commit: change.commit }) });
        projectionChanged(restored);
        dialog.close(); await loadDocuments();
      } });
      list.append(h('div', { class: 'copal-task-row' }, h('span', { text: `${new Date(change.ts).toLocaleString()}${change.message ? ` · ${change.message}` : ''}` }), restore));
    }
  } catch (error) { list.textContent = error.message; }
}

async function showTrash(kind = null) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: kind === 'base' ? 'Deleted Bases' : 'Deleted Copal documents' }));
  const list = h('div', { text: 'Loading…' });
  dialog.append(list, h('div', { class: 'copal-dialog-actions' }, h('button', { class: 'copal-btn', text: 'Close', onclick: () => dialog.close() })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
  try {
    const result = await api('/trash');
    list.replaceChildren();
    const docs = (result.docs || []).filter((doc) => !kind || doc.kind === kind);
    for (const doc of docs) list.append(h('div', { class: 'copal-task-row' }, h('span', { text: doc.name }), h('small', { text: new Date(doc.ts).toLocaleString() }), h('button', { class: 'copal-btn', text: 'Restore', onclick: async () => {
      const restored = await api(`/trash/${encodeURIComponent(doc.id)}/restore`, { method: 'POST' });
      projectionChanged(restored);
      if (kind === 'base') state.baseId = restored.doc?.id || doc.id;
      dialog.close();
      await loadDocuments();
      setStatus(`Restored ${doc.name} from Trash`);
    } })));
    if (!docs.length) list.textContent = 'Trash is empty.';
  } catch (error) { list.textContent = error.message; }
}

async function deleteDocument(doc) {
  if (!doc || doc.readOnly) {
    setStatus('Built-in knowledge is read only.', true);
    return false;
  }
  if (!await styledConfirm(`Move “${doc.name}” to Trash? You can restore it later.`, { title: 'Move document to Trash', confirmText: 'Move to Trash', danger: true })) return false;
  if (notesFeature?.prepareDelete && !await notesFeature.prepareDelete(doc.id)) return false;
  clearTimeout(state.saveTimers.get(doc.id));
  state.saveTimers.delete(doc.id);
  try {
    const deleted = await api(`/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' });
    projectionChanged(deleted);
    state.selected = null;
    if (state.baseId === doc.id) state.baseId = null;
    await loadDocuments();
    setStatus(`Moved ${doc.name} to Trash`);
    return true;
  } catch (error) {
    setStatus(error.message, true);
    return false;
  }
}

async function deleteDocuments(docs) {
  const unique = [...new Map((docs || []).filter((doc) => doc && !doc.readOnly).map((doc) => [doc.id, doc])).values()];
  if (!unique.length || !await styledConfirm(`Move ${unique.length} selected Copal document${unique.length === 1 ? '' : 's'} to trash?`, { title: 'Move documents to Trash', confirmText: 'Move to Trash', danger: true })) return false;
  for (const doc of unique) {
    if (notesFeature?.prepareDelete && !await notesFeature.prepareDelete(doc.id)) return false;
    clearTimeout(state.saveTimers.get(doc.id));
    state.saveTimers.delete(doc.id);
  }
  try {
    for (const doc of unique) {
      const deleted = await api(`/documents/${encodeURIComponent(doc.id)}`, { method:'DELETE' });
      projectionChanged(deleted);
      if (state.baseId === doc.id) state.baseId = null;
    }
  } catch (error) {
    await loadDocuments();
    setStatus(error.message, true);
    return false;
  }
  if (unique.some((doc) => doc.id === state.selected)) state.selected = null;
  await loadDocuments();
  setStatus(`Moved ${unique.length} document${unique.length === 1 ? '' : 's'} to Trash`);
  return true;
}

function destroyNoteEditors() {
  notesFeature?.destroy();
}

function installFilesMutationBridge() {
  if (state.filesMutationEvents) return;
  const events = new AbortController();
  window.addEventListener('openclank-files-resource-mutated', (event) => {
    const mutated = event.detail?.resource;
    const stableId = String(mutated?.id || '').trim();
    if (!stableId || String(mutated?.provider || '') !== 'copal') return;
    const doc = state.docs.find((candidate) => String(candidate.resource?.key?.resourceId || candidate.resourceKey?.resourceId || '') === stableId);
    if (!doc) return;
    if (mutated.name && doc.name !== mutated.name) doc.name = String(mutated.name);
    const rotatedRef = String(mutated.ref || '').trim();
    if (rotatedRef) notesFeature?.rotateResourceRef?.(doc.id, rotatedRef);
    const draft = notesFeature?.getDraftSnapshot?.(doc.id);
    if (draft?.doc) draft.doc.name = doc.name;
    if (rotatedRef) {
      const eventScope = saveScope();
      const bridge = events;
      void filesFacadeClient.openResource(rotatedRef).then((latest) => {
        if (state.filesMutationEvents === bridge && !bridge.signal.aborted && sameSaveScope(eventScope, saveScope()) && latest?.payload?.resource) {
          notesFeature?.rotateResourceRef?.(doc.id, rotatedRef, latest.payload.resource);
          notesFeature?.render?.();
        }
      }).catch(() => {
        if (state.filesMutationEvents === bridge && !bridge.signal.aborted && sameSaveScope(eventScope, saveScope())) notesFeature?.render?.();
      });
    } else notesFeature?.render?.();
  }, { signal:events.signal });
  state.filesMutationEvents = events;
  state.filesMutationBridgeInstallations += 1;
}

export function getFilesMutationBridgeInstallations() {
  return state.filesMutationBridgeInstallations;
}

async function renameNote(doc, name) {
  // A rename advances the document head. Commit the current Editor snapshot
  // first so the same user's pending body edit is not left behind on the old
  // CAS base and reported later as an unrelated external conflict.
  if (!await notesFeature?.resolveDirtyDocuments?.([doc.id], { force:true, title:'Rename document' })) {
    throw new Error('The document was not renamed; resolve its unsaved changes first.');
  }
  const result = await api(`/documents/${encodeURIComponent(doc.id)}/rename`, { method:'POST', body:JSON.stringify({ name }) });
  await loadDocuments();
  return result;
}

function renderNotes() {
  notesFeature?.render();
}
function renderWiki() {
  const current = state.windows.get('wiki');
  if (current && !current.noteWorkspace && !current.selected) current.selected = state.docs.find(doc => doc.kind === 'wiki' && !doc.builtin)?.id || state.docs.find(doc => doc.kind === 'wiki')?.id || null;
  wikiFeature?.render();
}

function wikiText(doc) {
  return String(notesFeature?.getDraftSnapshot?.(doc.id)?.envelope?.text ?? doc.text ?? '');
}

function openLinkedDocument(target, event = null) {
  if (!target) return;
  const intent = navigationIntentFromEvent(event);
  openDocument(target.id, target.kind === 'wiki' ? 'wiki' : 'notes', true, { intent });
}

async function exportWikiMemes() {
  const response = await fetch(`${state.api}/api/copal/export/memes?workspace=${encodeURIComponent(state.workspace)}`, {
    headers:state.accountId ? { 'X-Copal-Account':state.accountId } : {},
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'Wiki export failed');
  const blob = await response.blob();
  const link = document.createElement('a'); link.href = URL.createObjectURL(blob); link.download = 'wiki.memes'; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 0);
  setStatus('Wiki exported as wiki.memes');
}

function canMakeEditableWikiCopy(doc) {
  // Resolve the current document identity before deciding: copy preparation
  // strips official metadata, but that must not grant a built-in article copy authority.
  const source = state.docs.find((item) => item.id === doc?.id) || doc;
  return source?.kind === 'wiki' && source.readOnly === true && !isOfficialDocument(source);
}

async function makeEditableWikiCopy(doc) {
  if (!canMakeEditableWikiCopy(doc)) throw new Error('This Wiki article is not available to copy.');
  const scope = saveScope(), sourceId = doc.id, sourceRevision = doc.head;
  const proposed = `${doc.name} copy`;
  const name = await styledPrompt('Choose a name for the editable Wiki copy.', { title: 'Make editable copy', defaultValue: proposed, confirmText: 'Create copy', maxLength: 512 });
  if (!name || !sameSaveScope(scope, saveScope())) return;
  if (!canMakeEditableWikiCopy(doc)) throw new Error('This Wiki article is not available to copy.');
  const actionId = `wiki-copy-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
  const result = await api('/documents', {
    method:'POST',
    body:JSON.stringify({ actionId, name, kind:'wiki', corpus:'wiki', copySourceId:sourceId, copySourceRevision:sourceRevision }),
  });
  await loadDocuments(false);
  const copy = result?.doc?.id ? state.docs.find((item) => item.id === result.doc.id) : null;
  if (copy) openDocument(copy.id, 'wiki');
  setStatus(`Created editable Wiki copy ${name}.`);
}

async function createWikiArticle() {
  const name = await styledPrompt('Choose the page name shown in Wiki.', { title:'New Wiki article', defaultValue:'Untitled article', confirmText:'Create', maxLength:160 });
  if (!name) return;
  const result = await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'wiki', content:'', corpus:'wiki' }) });
  await loadDocuments(false);
  if (result?.doc?.id) openDocument(result.doc.id, 'wiki');
}

function importWikiMemesFromEditor() {
  const input = h('input', { type:'file', accept:`.memes,${MEMES_MIME}`, hidden:true, 'aria-label':'Import .memes file' });
  input.addEventListener('change', () => {
    const file = input.files?.[0]; input.remove();
    if (file) previewWikiMemes(file).catch(error => setStatus(error.message, true));
  }, { once:true });
  document.body.append(input); input.click();
}

function exportWikiMemesFromEditor() {
  return exportWikiMemes().catch(error => setStatus(error.message, true));
}

async function previewWikiMemes(file) {
  if (!file || (!file.name.toLowerCase().endsWith('.memes') && file.type !== MEMES_MIME)) {
    setStatus('Choose a .memes file.', true); return;
  }
  const submitMemesImport = async (restore = false) => {
    const form = new FormData(); form.append('file', file, file.name);
    const operation = restore ? 'restore' : 'import';
    const imported = await fetch(`${state.api}/api/copal/import/memes?workspace=${encodeURIComponent(state.workspace)}&mode=${operation}`, {
      method:'POST', headers:state.accountId ? { 'X-Copal-Account':state.accountId } : {}, body:form,
    });
    if (!imported.ok) throw new Error((await imported.json().catch(() => ({}))).detail || 'The .memes import failed');
    dialog.close(); await loadDocuments(false); renderNotes(); setStatus(restore ? 'Wiki .memes restore complete' : 'Wiki .memes import complete');
  };
  const form = new FormData(); form.append('file', file, file.name);
  const response = await fetch(`${state.api}/api/copal/preview/memes?workspace=${encodeURIComponent(state.workspace)}`, {
    method:'POST', headers:state.accountId ? { 'X-Copal-Account':state.accountId } : {}, body:form,
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || 'The .memes file could not be previewed');
  const preview = await response.json();
  const dialog = h('dialog', { class:'copal-dialog' }, h('h2', { text:'Preview .memes import' }),
    h('p', { text:'No changes have been made. Review the native Wiki records, then choose Import for new scoped copies or Restore current Wiki for an exact guarded restore.' }),
    h('p', { text:`${preview.documents.length} memes · ${preview.assets.length} assets` }),
    h('ul', {}, ...preview.documents.map(document => h('li', { text:document.name }))));
  dialog.append(h('div', { class:'copal-dialog-actions' },
    h('button', { class:'copal-btn', text:'Cancel', onclick:() => dialog.close() }),
    h('button', { class:'copal-btn', text:'Restore current Wiki', onclick:() => submitMemesImport(true).catch(error => setStatus(error.message, true)) }),
    h('button', { class:'copal-btn primary', text:'Import .memes', onclick:() => submitMemesImport(false).catch(error => setStatus(error.message, true)) })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal();
}


function allPlanningTasks(data = planningData()) {
  return (data.tracks || []).flatMap((track) => (track.tasks || []).map((task) => ({ ...task, track, primaryTrackId: track.id })));
}

function validDate(value) {
  if (!value || ['AUTO', 'FUZZY'].includes(value)) return null;
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function iso(date) { return date.toISOString().slice(0, 10); }
function addDays(date, days) { const next = new Date(date); next.setDate(next.getDate() + days); return next; }
async function editPlanningTask(trackId, taskId) {
  const task = allPlanningTasks().find((item) => item.id === taskId && (!trackId || item.primaryTrackId === trackId));
  if (task) planningFeature.openEventEditor(task.id);
}

function renderTimeline() {
  planningFeature.renderTimeline(state.body);
}

function renderCalendar() {
  const data = planningData(); const tasks = allPlanningTasks(data).filter((task, index, all) => all.findIndex((item) => item.id === task.id) === index);
  const seed = state.calendarMonth || validDate(data.today) || new Date(); const first = new Date(seed.getFullYear(), seed.getMonth(), 1); const gridStart = addDays(first, -first.getDay());
  const toolbar = h('div', { class: 'copal-timeline-toolbar' },
    h('button', { class: 'copal-btn', icon:'back', title:'Previous month', 'aria-label':'Previous month', onclick: () => { state.calendarMonth = new Date(first.getFullYear(), first.getMonth() - 1, 1); renderCalendar(); } }),
    h('strong', { text: first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) }),
    h('button', { class: 'copal-btn', icon:'forward', title:'Next month', 'aria-label':'Next month', onclick: () => { state.calendarMonth = new Date(first.getFullYear(), first.getMonth() + 1, 1); renderCalendar(); } }));
  const calendar = h('div', { class: 'copal-calendar' });
  for (let day = 0; day < 42; day++) {
    const date = addDays(gridStart, day); const key = iso(date);
    const cell = h('div', { class: 'copal-day' }, h('strong', { text: date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }) }));
    for (const task of tasks.filter((item) => item.startDate === key || item.dueDate === key || item.fuzzy?.anchorStart === key || item.fuzzy?.anchorEnd === key)) cell.append(h('button', { class: 'copal-day-event', text: task.title, onclick: () => editPlanningTask(task.primaryTrackId, task.id) }));
    calendar.append(cell);
  }
  state.body.replaceChildren(toolbar, calendar);
}

let graphView = null;
let graphScopeKey = null;

function getGraphView() {
  const key = graphStorageKey(state.accountId, state.workspace);
  if (!graphView || key !== graphScopeKey) {
    let saved = {};
    try { saved = key ? JSON.parse(localStorage.getItem(key) || '{}') : {}; } catch (_) {}
    graphView = normalizeGraphState(saved);
    graphScopeKey = key;
  }
  return graphView;
}

function persistGraphView(view = getGraphView()) {
  if (view !== getGraphView() || !graphScopeKey) return;
  try {
    const serializable = structuredClone(view);
    for (const mode of Object.values(serializable.modes || {})) {
      if (mode.navigation?.collapsed instanceof Set) mode.navigation.collapsed = [...mode.navigation.collapsed];
    }
    localStorage.setItem(graphScopeKey, JSON.stringify(serializable));
  } catch (_) { /* optional presentation state */ }
}

function mindModeState() {
  // Legacy name kept for call sites; Graph structure mode owns this state.
  const mode = getGraphView().modes.structure;
  mode.navigation ||= { docId:null, collapsed:[], selectedLine:null, editingLine:null, draggingLine:null };
  if (!(mode.navigation.collapsed instanceof Set)) mode.navigation.collapsed = new Set(mode.navigation.collapsed || []);
  return mode.navigation;
}

function resolveView(view, mode = null) {
  if (view === 'editor') view = 'notes';
  // Wiki is a document type owned by the shared Editor. Keep the legacy view
  // name as a route alias, but never create or restore a second window.
  // Wiki retains its own applet; both controllers share canonical buffers.
  // Bases is an Editor leaf now. Keep the old URL as a compatibility alias so
  // bookmarks open the selected Base in the shared Editor workspace.
  if (view === 'bases') view = 'notes';
  // Mind is a legacy deep-link alias for the document-structure mode of the
  // shared Graph family. It opens Graph's structure view, never a separate Mind
  // surface, so saved Mind entrypoints resolve into the Graph page.
  if (view === 'mind') {
    getGraphView().mode = 'structure';
    persistGraphView();
    return 'graph';
  }
  if (view === 'galaxy' || (view === 'graph' && GRAPH_MODES.includes(mode))) {
    getGraphView().mode = view === 'galaxy' ? 'galaxy' : mode;
    persistGraphView();
  }
  return view === 'galaxy' ? 'graph' : VIEWS.includes(view) ? view : 'notes';
}

function docKindToGraphKind(doc) {
  const kind = String(doc.kind || '').toLowerCase();
  if (kind === 'copal-event') return 'event';
  if (kind === 'markdown' || kind === 'wiki') return 'wiki';
  if (kind === 'base') return 'base';
  if (kind === 'note') return 'note';
  return 'note';
}

function kindColor(kind) {
  return { note: 'var(--copal-graph-note, var(--accent, var(--red)))', event: 'var(--copal-graph-event, #22c55e)', wiki: 'var(--copal-graph-wiki, var(--accent, var(--red)))', base: 'var(--copal-graph-base, #f59e0b)', track:'var(--copal-graph-track, #a78bfa)' }[kind] || 'var(--accent, var(--red))';
}

function graphSourceEnvelope(doc) {
  const snapshot = notesFeature?.getDraftSnapshot?.(doc?.id);
  const resource = doc?.resource?.key || doc?.resourceKey || null;
  const revision = snapshot?.expectedRevision || doc?.resource?.revision || { kind:'copalHead', value:String(doc?.head || '') };
  return {
    kind:'document', docId:doc?.id ? String(doc.id) : null,
    resourceKey:resource ? cloneEnvelope(resource) : null,
    revision:cloneEnvelope(revision), state:'selected',
  };
}

function graphSvg(nodes, edges, onOpen, mode = getGraphView().mode, facets = null) {
  const root = h('div', { class: 'copal-graph-wrap' });
  const view = getGraphView();
  const modeState = view.modes[mode];
  // Real facet state derived from server-scoped metadata/folders/values. The
  // saved filter set is reconciled against the loaded snapshot so renamed or
  // deleted values recover instead of silently hiding the graph.
  const reconciled = reconcileFilters(modeState.filters, facets);
  modeState.filters = reconciled;
  const graphState = {
    searchQuery:reconciled.search,
    activeKinds:new Set(reconciled.kinds),
    activeFolders:new Set(reconciled.folders),
    activeTags:new Set(reconciled.tags),
    activeProperties:Object.fromEntries(Object.entries(reconciled.properties || {}).map(([key, values]) => [key, new Set(values)])),
    includeOfficial:reconciled.includeOfficial === true,
    selectedNodeId:modeState.selection?.nodeId || null,
  };
  const vb = { ...modeState.camera };
  const initialCamera = { ...vb }, cameraViewId = crypto.randomUUID();
  const cameraOwner = achievementOwner();
  let cameraStart = null;
  const cameraFacts = camera => ({ accepted:true, populatedView:nodes.length > 0, viewId:cameraViewId,
    scale:1000 / camera.w, panX:-camera.x * 1000 / camera.w, panY:-camera.y * 650 / camera.h });
  const MIN_VIEW = 20, MAX_VIEW = 80000, MAX_MOUNTED = 300;
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  const persist = () => {
    modeState.camera = { ...vb };
    modeState.filters = {
      search:graphState.searchQuery,
      kinds:[...graphState.activeKinds],
      folders:[...graphState.activeFolders],
      tags:[...graphState.activeTags],
      properties:Object.fromEntries(Object.entries(graphState.activeProperties).map(([key, values]) => [key, [...values]])),
      includeOfficial:graphState.includeOfficial,
    };
    const selected = nodesById.get(graphState.selectedNodeId);
    modeState.selection = selected ? { ...selected.selection, nodeId:selected.id } : null;
    persistGraphView(view);
  };
  const svgEl = svg('svg', { class: 'copal-graph', viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`, role: 'img', 'aria-label': `Graph showing ${nodes.length} nodes and ${edges.length} edges` });
  let cameraFrame = 0, cameraFrameCancel = null, destroyed = false;
  const applyCamera = () => {
    if (cameraFrame) return;
    const useRaf = typeof globalThis.requestAnimationFrame === 'function';
    const frame = useRaf ? globalThis.requestAnimationFrame.bind(globalThis) : (callback) => setTimeout(callback, 0);
    cameraFrameCancel = useRaf ? globalThis.cancelAnimationFrame?.bind(globalThis) : clearTimeout;
    cameraFrame = frame(() => { cameraFrame = 0; cameraFrameCancel = null; if (destroyed || !root.isConnected) return; svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); persist(); updateZoomBounds?.();
      if (nodes.length && root.getClientRects().length && document.visibilityState === 'visible') {
        cameraStart ||= recordPresentation('graph.camera.gesture', { ...cameraFacts(initialCamera), start:true }, { accountId:cameraOwner, workspaceId:state.workspace });
        void cameraStart.then(() => recordPresentation('graph.camera.gesture', cameraFacts(vb), { accountId:cameraOwner, workspaceId:state.workspace }));
      }
    });
  };

  // B1: compute edge sets for highlight
  const nodeIds = new Set(nodes.map((n) => n.id));
  const adjacentEdges = new Map();
  for (const edge of edges) {
    if (!adjacentEdges.has(edge.from)) adjacentEdges.set(edge.from, []);
    if (!adjacentEdges.has(edge.to)) adjacentEdges.set(edge.to, []);
    adjacentEdges.get(edge.from).push(edge);
    adjacentEdges.get(edge.to).push(edge);
  }

  // Layout positions
  const positions = new Map();
  nodes.forEach((node, index) => { const angle = index / Math.max(1, nodes.length) * Math.PI * 2; const ring = 190 + (index % 3) * 45; positions.set(node.id, { x: 500 + Math.cos(angle) * ring, y: 325 + Math.sin(angle) * ring }); });

  let edgeEls = [];
  let nodeEls = [];

  // Inspector panel (B3)
  const inspector = h('div', { class: 'copal-graph-inspector' });

  function updateInspector(node) {
    inspector.textContent = '';
    if (!node) return;
    const name = h('div', { class: 'copal-graph-inspector-name', text: node.label || '' });
    const kindLabel = h('span', { class: `copal-graph-inspector-kind kind-${node.kind || 'note'}`, text: (node.kind || 'note').charAt(0).toUpperCase() + (node.kind || 'note').slice(1) });
    const meta = h('div', { class: 'copal-graph-inspector-meta' });

    // Find connected nodes
    const connected = adjacentEdges.get(node.id) || [];
    const links = h('div', { class: 'copal-graph-inspector-links' });
    for (const edge of connected) {
      const otherId = edge.from === node.id ? edge.to : edge.from;
      const otherNode = nodesById.get(otherId);
      if (!otherNode) continue;
      const chip = h('button', { class: 'copal-graph-inspector-chip', text: `${edge.type === 'embed' ? '⬒ ' : edge.type === 'relation' ? '⋯ ' : '→ '}${otherNode.label || otherId}`, onclick: () => onOpen(otherNode) });
      links.append(chip);
    }

    const openBtn = h('button', { class:'copal-graph-inspector-open', text:node.kind === 'track' ? 'Edit track' : node.task ? 'Open event' : 'Open in Editor', onclick:() => onOpen(node) });

    meta.textContent = `${connected.length} connection${connected.length !== 1 ? 's' : ''}`;
    inspector.append(name, kindLabel, meta);
    if (connected.length) inspector.append(links);
    inspector.append(openBtn);
  }

  function clearInspector() { inspector.textContent = ''; }

  // B4: select/deselect node
  let selectedEl = null;
  function selectNode(nodeEl, node) {
    if (selectedEl) selectedEl.classList.remove('selected');
    selectedEl = nodeEl;
    nodeEl.classList.add('selected');
    graphState.selectedNodeId = node.id;
    if (node.doc) view.source = graphSourceEnvelope(node.doc);
    persist();
    updateInspector(node);
    // highlight adjacent edges
    for (const ee of edgeEls) ee.el.classList.toggle('edge-highlight', ee.edge.from === node.id || ee.edge.to === node.id);
  }
  function deselectNode() {
    if (selectedEl) selectedEl.classList.remove('selected');
    selectedEl = null;
    graphState.selectedNodeId = null;
    persist();
    clearInspector();
    for (const ee of edgeEls) ee.el.classList.remove('edge-highlight');
  }

  // Mount only a bounded window, rebuilding it when search/filter/selection
  // changes. This keeps a large corpus discoverable without creating 5,001
  // SVG subtrees, and always makes the selected match reachable.
  function mountGraph(visibleIds) {
    const selected = graphState.selectedNodeId;
    const mountedIds = [];
    if (selected && nodesById.has(selected)) mountedIds.push(selected);
    for (const node of nodes) {
      if (mountedIds.length >= MAX_MOUNTED) break;
      if (visibleIds.has(node.id) && !mountedIds.includes(node.id)) mountedIds.push(node.id);
    }
    const mounted = new Set(mountedIds);
    const columns = Math.max(1, Math.ceil(Math.sqrt(mountedIds.length)));
    const rows = Math.max(1, Math.ceil(mountedIds.length / columns));
    mountedIds.forEach((id, index) => positions.set(id, {
      x: 80 + ((index % columns) + 0.5) * (840 / columns),
      y: 55 + (Math.floor(index / columns) + 0.5) * (540 / rows),
    }));
    if (selectedEl) selectedEl = null;
    svgEl.replaceChildren();
    edgeEls = [];
    nodeEls = [];
    for (const edge of edges) {
      if (!mounted.has(edge.from) || !mounted.has(edge.to)) continue;
      const from = positions.get(edge.from); const to = positions.get(edge.to);
      if (!from || !to) continue;
      const line = svg('line', { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: `copal-graph-edge edge-${edge.type || 'link'}`, 'data-from': edge.from, 'data-to': edge.to });
      edgeEls.push({ el: line, edge });
      svgEl.append(line);
    }
    for (const node of nodes) {
      if (!mounted.has(node.id)) continue;
      const pos = positions.get(node.id); if (!pos) continue;
      const group = svg('g', { class: `copal-graph-node${visibleIds.has(node.id) ? '' : ' dimmed'}`, 'data-copal-context-object':'graph', 'data-graph-id':node.id, tabindex:'0', role:'button', 'aria-label':`${node.label} (${node.kind || 'note'})` });
      const color = kindColor(node.kind || 'note');
      group.append(svg('circle', { cx: pos.x, cy: pos.y, r: node.hub ? 22 : 15, class: `kind-${node.kind || 'note'}${node.hub ? ' hub' : ''}`, fill: color, stroke: color }));
      const label = svg('text', { x: pos.x + (node.hub ? 26 : 20), y: pos.y + 4, class: 'copal-graph-label' });
      label.textContent = String(node.label || '').slice(0, 40);
      group.append(label);
      if (node.id === selected) { group.classList.add('selected'); selectedEl = group; }
      nodeEls.push({ el: group, node });
      group.addEventListener('click', () => selectNode(group, node));
      group.addEventListener('dblclick', (event) => { event.preventDefault(); selectNode(group, node); onOpen(node); });
      group.addEventListener('keydown', (event) => {
        if (!canHandleInput(event)) return;
        if (event.key === 'Enter') { event.preventDefault(); selectNode(group, node); onOpen(node); }
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown' || event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
          event.preventDefault(); event.stopPropagation();
          const available = nodeEls;
          const currentIdx = available.findIndex((item) => item.node.id === node.id);
          const nextIdx = event.key === 'ArrowRight' || event.key === 'ArrowDown' ? (currentIdx + 1) % available.length : (currentIdx - 1 + available.length) % available.length;
          const next = available[nextIdx];
          if (next) { next.el.focus(); selectNode(next.el, next.node); }
        }
      });
      svgEl.append(group);
    }
  }
  svgEl.addEventListener('click', (e) => { if (e.target === svgEl || e.target.tagName === 'line') deselectNode(); });

  // Zoom controls. Zoom keeps the pointer anchor fixed so wheel/pinch feels
  // natural, while the buttons remain secondary helpers.
  const zoomAroundCenter = (factor) => {
    const width = Math.max(MIN_VIEW, Math.min(MAX_VIEW, vb.w * factor));
    const height = Math.max(MIN_VIEW * 650 / 1000, Math.min(MAX_VIEW * 650 / 1000, vb.h * (width / vb.w)));
    vb.x += (vb.w - width) / 2; vb.y += (vb.h - height) / 2; vb.w = width; vb.h = height; applyCamera();
  };
  // Zoom around a screen point so the world coordinate under the pointer stays
  // put. Used by wheel and pinch gestures.
  const zoomAroundPoint = (factor, clientX, clientY) => {
    const transform = svgEl.getScreenCTM();
    if (!transform) { zoomAroundCenter(factor); return; }
    const inverse = transform.inverse();
    const anchor = new DOMPoint(clientX, clientY).matrixTransform(inverse);
    const width = Math.max(MIN_VIEW, Math.min(MAX_VIEW, vb.w * factor));
    const height = Math.max(MIN_VIEW * 650 / 1000, Math.min(MAX_VIEW * 650 / 1000, vb.h * (width / vb.w)));
    // Keep the anchor's relative position inside the viewBox stable.
    const ratioX = (anchor.x - vb.x) / vb.w;
    const ratioY = (anchor.y - vb.y) / vb.h;
    vb.w = width; vb.h = height;
    vb.x = anchor.x - ratioX * width;
    vb.y = anchor.y - ratioY * height;
    applyCamera();
  };
  const zoomIn = h('button', { class:'copal-graph-ctrl', icon:'add', text:'Zoom in', title:'Zoom in', 'aria-label':'Zoom in', onclick:() => { if (vb.w > MIN_VIEW) zoomAroundCenter(0.8); } });
  const zoomOut = h('button', { class:'copal-graph-ctrl', icon:'remove', text:'Zoom out', title:'Zoom out', 'aria-label':'Zoom out', onclick:() => { if (vb.w < MAX_VIEW) zoomAroundCenter(1.25); } });
  const reset = h('button', { class:'copal-graph-ctrl', icon:'restore', text:'Reset view', title:'Reset view', 'aria-label':'Reset view', onclick:() => { Object.assign(vb, { x:0, y:0, w:1000, h:650 }); applyCamera(); } });
  // Fit frames the mounted graph content in the viewport without relying on
  // button zoom as the primary navigation mechanism.
  const fit = h('button', { class:'copal-graph-ctrl', icon:'expand', text:'Fit', title:'Fit graph', 'aria-label':'Fit graph to view', onclick:() => {
    const points = [...positions.values()].filter((point) => Number.isFinite(point.x) && Number.isFinite(point.y));
    if (!points.length) { Object.assign(vb, { x:0, y:0, w:1000, h:650 }); applyCamera(); return; }
    const minX = Math.min(...points.map((point) => point.x)) - 80;
    const maxX = Math.max(...points.map((point) => point.x)) + 80;
    const minY = Math.min(...points.map((point) => point.y)) - 60;
    const maxY = Math.max(...points.map((point) => point.y)) + 60;
    const width = Math.max(MIN_VIEW, Math.min(MAX_VIEW, maxX - minX));
    const height = Math.max(MIN_VIEW * 650 / 1000, Math.min(MAX_VIEW * 650 / 1000, maxY - minY));
    Object.assign(vb, { x:minX, y:minY, w:width, h:height });
    applyCamera();
  } });
  const updateZoomBounds = () => { zoomIn.disabled = vb.w <= MIN_VIEW; zoomOut.disabled = vb.w >= MAX_VIEW; };

  // B1: Legend with actual node/edge types
  const presentKinds = new Set(nodes.map((n) => n.kind || 'note'));
  const presentEdgeTypes = new Set(edges.map((e) => e.type || 'link'));
  const legendItems = [];
  const kindOrder = ['note', 'event', 'wiki', 'base', 'track'];
  const kindLabels = { note:'Documents', event:'Events', wiki:'Wiki', base:'Bases', track:'Tracks' };
  for (const kind of kindOrder) {
    if (!presentKinds.has(kind)) continue;
    legendItems.push(h('span', { class: 'copal-graph-legend-item' }, svg('circle', { cx: 6, cy: 6, r: 6, fill: kindColor(kind), stroke: kindColor(kind) }), h('span', { text: kindLabels[kind] })));
  }
  const edgeOrder = [...presentEdgeTypes];
  const edgeLabels = { link:'Links', embed:'Embeds', relation:'Relations', primary:'Primary track', shared:'Shared track' };
  const edgeDash = { link: '', embed: '6 3', relation: '2 3' };
  for (const type of edgeOrder) {
    if (!presentEdgeTypes.has(type)) continue;
    legendItems.push(h('span', { class:'copal-graph-legend-item' }, svg('line', { x1:0, y1:6, x2:18, y2:6, class:`copal-graph-edge edge-${type}` }), h('span', { text:edgeLabels[type] || type })));
  }
  const legend = h('div', { class: 'copal-graph-legend' }, ...legendItems);
  const controls = h('div', { class: 'copal-graph-controls' }, zoomIn, zoomOut, fit, reset, legend);

  // B2: search + filter toolbar driven by real scoped facets. Values come
  // from the server-derived snapshot rather than a fixed category allowlist.
  const searchInput = h('input', { class: 'copal-graph-search', type: 'search', placeholder: 'Search nodes…', 'aria-label': 'Search graph nodes', value: graphState.searchQuery });
  const filters = h('div', { class: 'copal-graph-filters' });
  const facetRows = [];
  let pendingAchievementFacet = null, previousResultCount = null;
  const FACET_PAGE = 8;
  const addFacetGroup = (title, values, selectedSet, onChange) => {
    if (!Array.isArray(values) || !values.length) return;
    const group = h('div', { class: 'copal-graph-facet-group', 'data-facet-group': title });
    group.append(h('span', { class: 'copal-graph-facet-title', text: title }));
    let shown = 0;
    const row = h('div', { class: 'copal-graph-facet-values' });
    const renderValues = () => {
      row.replaceChildren();
      const window = values.slice(shown, shown + FACET_PAGE);
      for (const item of window) {
        const value = typeof item === 'string' ? item : item.value;
        const label = value === '' ? '(root)' : value;
        const cb = h('label', { class: 'copal-graph-filter-label' });
        const input = h('input', { type: 'checkbox', checked: selectedSet.has(value) || undefined });
        input.addEventListener('change', () => { if (input.checked) selectedSet.add(value); else selectedSet.delete(value); pendingAchievementFacet = { facet:title, value:String(value) }; onChange(); });
        cb.append(input, h('span', { text: label, title: `${label} · ${item.count ?? ''}`.trim() }));
        row.append(cb);
      }
      const more = shown + FACET_PAGE < values.length;
      const loadMore = h('button', { class:'copal-btn copal-graph-facet-more', type:'button', text: `Show ${Math.min(FACET_PAGE, values.length - shown - FACET_PAGE)} more…`, 'aria-label':`Show more ${title} filter values` });
      loadMore.addEventListener('click', () => { shown += FACET_PAGE; renderValues(); });
      if (more) row.append(loadMore);
    };
    renderValues();
    group.append(row);
    filters.append(group);
    facetRows.push({ group, renderValues });
    return group;
  };
  // Facet kinds fall back to kinds present in the mounted projection when a
  // snapshot has not loaded yet, so the graph stays filterable offline.
  const kindValues = facets?.kinds?.length ? facets.kinds : [...presentKinds].map((kind) => ({ value:kind, count:nodes.filter((node) => (node.kind || 'note') === kind).length }));
  addFacetGroup('Kind', kindValues, graphState.activeKinds, rebuildGraph);
  addFacetGroup('Folder', facets?.folders, graphState.activeFolders, rebuildGraph);
  addFacetGroup('Tag', facets?.tags, graphState.activeTags, rebuildGraph);
  if (facets?.properties && typeof facets.properties === 'object') {
    for (const [key, values] of Object.entries(facets.properties)) {
      const selected = graphState.activeProperties[key] || (graphState.activeProperties[key] = new Set());
      addFacetGroup(key, values, selected, rebuildGraph);
    }
  }
  // Official docs are excluded by default through the general folder filter
  // and become selectable like any other value.
  const officialToggle = h('label', { class: 'copal-graph-filter-label copal-graph-official' });
  const officialInput = h('input', { type: 'checkbox', checked: graphState.includeOfficial || undefined, 'aria-label': 'Include official documentation' });
  officialInput.addEventListener('change', () => { graphState.includeOfficial = officialInput.checked; rebuildGraph(); });
  officialToggle.append(officialInput, h('span', { text: 'Official docs' }));
  filters.append(officialToggle);
  const toolbar = h('div', { class: 'copal-graph-toolbar' }, searchInput, filters);

  searchInput.addEventListener('input', () => { graphState.searchQuery = searchInput.value; rebuildGraph(); });

  // B4: screen-reader summary
  const resetFilters = h('button', { class:'copal-btn', icon:'restore', text:'Reset filters', title:'Reset graph search and facet filters', 'aria-label':'Reset graph filters', onclick:() => {
    graphState.searchQuery = '';
    // Mutate in place: facet groups keep a reference to these collections.
    graphState.activeKinds.clear();
    for (const item of kindValues) graphState.activeKinds.add(item.value);
    graphState.activeFolders.clear();
    graphState.activeTags.clear();
    for (const values of Object.values(graphState.activeProperties)) values.clear();
    graphState.includeOfficial = false;
    searchInput.value = '';
    officialInput.checked = false;
    for (const row of facetRows) row.renderValues();
    rebuildGraph();
  } });
  toolbar.append(resetFilters);
  const info = h('div', { class: 'copal-graph-info', role: 'status', 'aria-live': 'polite' }, h('span', { text: `Graph showing ${nodes.length} nodes and ${edges.length} edges` }));

  // Rebuild on filter/search change. Facet filters are AND-ed across
  // dimensions and OR-ed within one dimension, matching the model predicate.
  function rebuildGraph() {
    const activeFilters = {
      search:graphState.searchQuery,
      kinds:[...graphState.activeKinds],
      folders:[...graphState.activeFolders],
      tags:[...graphState.activeTags],
      properties:Object.fromEntries(Object.entries(graphState.activeProperties).map(([key, values]) => [key, [...values]])),
      includeOfficial:graphState.includeOfficial,
    };
    const visibleIds = new Set();
    for (const node of nodes) {
      const doc = node.doc;
      if (doc) {
        // Documents are filtered through the real facet predicate so folder,
        // tag and property values govern visibility, not just node kind.
        if (!matchesFilters(doc, activeFilters, facets)) continue;
      } else if (!graphState.activeKinds.has(node.kind || 'note')) {
        continue;
      }
      if (activeFilters.search && !String(node.label || '').toLowerCase().includes(activeFilters.search.toLowerCase())) continue;
      visibleIds.add(node.id);
    }
    mountGraph(visibleIds);
    if (pendingAchievementFacet && previousResultCount != null && previousResultCount !== visibleIds.size
        && facets?.generation && facets.generation !== 'local') {
      acknowledgeVisible(root, 'graph.filter.applied', { ...pendingAchievementFacet, facetFromScopedGeneration:true,
        resultCountBefore:previousResultCount, resultCountAfter:visibleIds.size }, { accountId:cameraOwner, workspaceId:state.workspace });
    }
    pendingAchievementFacet = null;
    previousResultCount = visibleIds.size;
    const visibleEdges = edges.filter((edge) => visibleIds.has(edge.from) && visibleIds.has(edge.to)).length;
    info.textContent = visibleIds.size ? `Graph showing ${visibleIds.size} nodes and ${visibleEdges} edges${visibleIds.size > MAX_MOUNTED ? ` · showing first ${MAX_MOUNTED}; search to navigate` : ''}` : 'No graph results. Reset filters or search again.';
    updateInspector(graphState.selectedNodeId ? nodesById.get(graphState.selectedNodeId) : null);
    persist();
  }

  root.append(toolbar, svgEl, controls, inspector, info);
  rebuildGraph();
  updateZoomBounds();
  const restoredSelection = nodeEls.find((item) => item.node.id === graphState.selectedNodeId);
  if (restoredSelection) selectNode(restoredSelection.el, restoredSelection.node);

  // Pan via pointer drag
  let dragging = false, lastX = 0, lastY = 0;
  svgEl.addEventListener('pointerdown', (e) => { if (e.button === 0 && canHandleInput(e) && (e.target === svgEl || e.target.tagName === 'line')) { dragging = true; lastX = e.clientX; lastY = e.clientY; svgEl.setPointerCapture(e.pointerId); } });
  svgEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    if (!canHandleInput(e)) { dragging = false; return; }
    // The screen transform includes CSS zoom and SVG aspect-ratio padding.
    const transform = svgEl.getScreenCTM();
    if (!transform) return;
    const inverse = transform.inverse();
    const before = new DOMPoint(lastX, lastY).matrixTransform(inverse);
    const after = new DOMPoint(e.clientX, e.clientY).matrixTransform(inverse);
    vb.x -= after.x - before.x; vb.y -= after.y - before.y;
    lastX = e.clientX; lastY = e.clientY; applyCamera();
  });
  // Wheel and pinch zoom around the pointer so the anchored content stays put.
  const activeTouches = new Map();
  let pinch = null;
  svgEl.addEventListener('wheel', (e) => {
    if (!canHandleInput(e)) return;
    e.preventDefault();
    const factor = e.deltaY > 0 ? 1.12 : 0.89;
    zoomAroundPoint(factor, e.clientX, e.clientY);
  }, { passive:false });
  svgEl.addEventListener('pointerdown', (e) => {
    if (e.pointerType === 'touch' || e.pointerType === 'pen') {
      activeTouches.set(e.pointerId, { x:e.clientX, y:e.clientY });
      if (activeTouches.size === 2) {
        dragging = false;
        const [a, b] = [...activeTouches.values()];
        pinch = { distance:Math.hypot(a.x - b.x, a.y - b.y) };
      }
    }
  });
  svgEl.addEventListener('pointermove', (e) => {
    if (!activeTouches.has(e.pointerId)) return;
    activeTouches.set(e.pointerId, { x:e.clientX, y:e.clientY });
    if (activeTouches.size === 2 && pinch) {
      const [a, b] = [...activeTouches.values()];
      const distance = Math.hypot(a.x - b.x, a.y - b.y);
      if (pinch.distance > 0 && distance > 0) zoomAroundPoint(pinch.distance / distance, (a.x + b.x) / 2, (a.y + b.y) / 2);
      pinch = { distance };
    }
  });
  const endPointer = (e) => {
    dragging = false;
    activeTouches.delete(e.pointerId);
    if (activeTouches.size < 2) pinch = null;
  };
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) svgEl.addEventListener(type, endPointer);
  root._copalDestroy = () => {
    destroyed = true;
    if (cameraFrame) { cameraFrameCancel?.(cameraFrame); clearTimeout(cameraFrame); cameraFrame = 0; cameraFrameCancel = null; }
  };
  return root;
}

function renderGalaxy() {
  resolveView('galaxy');
  renderGraph();
}

// Lazy live facets: cached by account/workspace/index generation. The
// mounted page size is never treated as corpus size — each page is followed
// through `hasMore`/`offset` so a value outside the first page stays
// discoverable, and a new generation refetches so renamed/deleted values
// reconcile. A generation hit reuses the already-paged snapshot.
const graphFacetCache = new Map();
const FACET_FETCH_LIMIT = 200;
const FACET_MAX_PAGES = 25;

function facetPageValues(page) {
  if (!page) return [];
  if (Array.isArray(page)) return page;
  return Array.isArray(page.values) ? page.values : [];
}

function facetPageHasMore(page) {
  return Boolean(page && typeof page === 'object' && !Array.isArray(page) && page.hasMore === true);
}

function anyFacetHasMore(response) {
  const facets = response?.facets || {};
  return facetPageHasMore(facets.kinds) || facetPageHasMore(facets.folders) || facetPageHasMore(facets.tags)
    || Object.values(facets.properties || {}).some((page) => facetPageHasMore(page));
}

function snapshotFromFacetResponse(response) {
  const facets = response?.facets || {};
  return {
    generation: String(response?.generation ?? ''),
    officialRoot: response?.officialRoot ?? null,
    totalDocuments: response?.totalDocuments ?? 0,
    kinds: facetPageValues(facets.kinds),
    folders: facetPageValues(facets.folders),
    tags: facetPageValues(facets.tags),
    properties: Object.fromEntries(Object.entries(facets.properties || {}).map(([key, page]) => [key, facetPageValues(page)])),
    incomplete: false,
  };
}

async function loadGraphFacets({ force = false } = {}) {
  const account = state.accountId || '';
  const workspace = state.workspace || '';
  if (!account || !workspace) return null;
  try {
    const first = await api(`/graph/facets?limit=${FACET_FETCH_LIMIT}&offset=0`, {}, workspace);
    const generation = String(first?.generation ?? '');
    const key = facetCacheKey(account, workspace, generation);
    if (!key) return null;
    if (!force && graphFacetCache.has(key)) return graphFacetCache.get(key);
    const snapshot = snapshotFromFacetResponse(first);
    // Page through remaining values. `hasMore` is per category and the same
    // offset/limit applies to each, so keep requesting while any category
    // still reports more and merge the pages into one complete snapshot.
    let offset = FACET_FETCH_LIMIT;
    let pages = 1;
    let more = anyFacetHasMore(first);
    while (more && pages < FACET_MAX_PAGES) {
      const next = await api(`/graph/facets?limit=${FACET_FETCH_LIMIT}&offset=${offset}`, {}, workspace);
      const merged = mergeFacets(snapshot, snapshotFromFacetResponse(next));
      snapshot.kinds = merged.kinds;
      snapshot.folders = merged.folders;
      snapshot.tags = merged.tags;
      snapshot.properties = merged.properties;
      snapshot.officialRoot = merged.officialRoot;
      snapshot.totalDocuments = merged.totalDocuments;
      snapshot.generation = merged.generation;
      more = anyFacetHasMore(next);
      offset += FACET_FETCH_LIMIT;
      pages += 1;
    }
    // A capped walk is incomplete: off-page values are still discoverable and
    // must never be reconciled as deletions.
    if (more) snapshot.incomplete = true;
    // Keep one snapshot per account/workspace/generation; drop other
    // generations so stale values cannot leak across cache identities.
    for (const cachedKey of [...graphFacetCache.keys()]) {
      if (cachedKey !== key && cachedKey.includes(`:${encodeURIComponent(account)}:${encodeURIComponent(workspace)}:`)) graphFacetCache.delete(cachedKey);
    }
    graphFacetCache.set(key, snapshot);
    return snapshot;
  } catch (_) {
    // Facets are an enhancement over mounted kinds; an unavailable endpoint
    // must not block the ordinary graph fallback.
    return null;
  }
}

function renderGraph() {
  // B2: respect dot-folder toggle from workspace settings
  let docs = visibleDocs();
  try {
    const saved = JSON.parse(localStorage.getItem(copalStorageKey(`odysseus-copal-${view}-layout`, state.workspace)) || '{}');
    const showDot = saved?.left?.showDotFolders === true;
    if (!showDot) docs = docs.filter((doc) => {
      // Canonical .memes Wiki pages are first-party graph sources. The
      // preference hides ordinary dot-folder notes only.
      if (String(doc.kind || '').toLowerCase() === 'wiki' || (doc.name || '').split('/').includes('.memes')) return true;
      return !(doc.name || '').split('/').some((part) => part.startsWith('.'));
    });
  } catch (_) {}

  const scope = saveScope();
  const view = getGraphView();
  if (view.mode === 'structure') { renderMind(); return; }
  const projection = view.mode === 'galaxy' ? galaxyGraph(planningData().tracks || [], allPlanningTasks(planningData())) : documentGraph(docs, findByName);
  // Mind is not offered as a Graph view. Both linked-document and
  // document-structure modes live here; Galaxy keeps its own projection.
  const modeOptions = [h('option', { value:'documents', text:'Documents · links' }), h('option', { value:'structure', text:'Structure · headings and bullets' }), h('option', { value:'galaxy', text:'Galaxy · tracks and events' })];
  const mode = h('select', { 'aria-label':'Graph view', 'data-graph-mode':true }, modeOptions);
  mode.value = view.mode;
  mode.addEventListener('change', () => { view.mode = mode.value; persistGraphView(view); renderGraph(); updateRoute('graph', true); state.body.querySelector('[data-graph-mode]')?.focus(); });
  const focusedId = state.body.contains(document.activeElement) ? document.activeElement?.getAttribute('data-graph-id') : null;
  const openNode = (node) => {
    if (!sameSaveScope(scope, saveScope())) return;
    if (node.task) editPlanningTask(node.task.primaryTrackId, node.task.id);
    else if (node.track) planningFeature.openTrackEditor(node.track.id);
    else if (node.doc) openDocument(node.doc.id, node.kind === 'wiki' ? 'wiki' : 'notes');
  };
  const mount = (facets) => {
    const graph = graphSvg(projection.nodes, projection.edges, openNode, view.mode, facets);
    state.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    state.body.replaceChildren(h('div', { class:'copal-timeline-toolbar' }, h('label', {}, 'Graph view ', mode)), graph);
    if (view.mode === 'documents') acknowledgeVisible(graph, 'graph.mode.presented', { mode:'linked', nodeCount:projection.nodes.length }, { accountId:state.accountId, workspaceId:state.workspace });
    if (focusedId) graph.querySelector(`[data-graph-id="${CSS.escape(focusedId)}"]`)?.focus({ preventScroll:true });
  };
  // Mount immediately with locally derived facets, then refine with the lazy
  // server snapshot so the graph stays responsive while values stream in.
  // Server pages merge over the local derivation — they never replace it — so
  // off-page values stay discoverable and local values are never dropped.
  // Galaxy's facet vocabulary comes from tracks/events, not document metadata,
  // so its kinds are never reconciled against document-derived facets.
  if (view.mode === 'galaxy') {
    const galaxyFacets = {
      generation:'local',
      officialRoot:null,
      totalDocuments:projection.nodes.length,
      kinds:[...new Set(projection.nodes.map((node) => node.kind || 'note'))].map((value) => ({ value, count:projection.nodes.filter((node) => (node.kind || 'note') === value).length })),
      folders:[],
      tags:[],
      properties:{},
    };
    mount(galaxyFacets);
    return;
  }
  const localFacets = deriveFacets(docs, { officialRoot:officialDocsRoot(docs) });
  mount(localFacets);
  loadGraphFacets().then((snapshot) => {
    if (!snapshot || !state.body.isConnected) return;
    if (!sameSaveScope(scope, saveScope())) return;
    if (getGraphView().mode !== view.mode) return;
    mount(mergeFacets(localFacets, snapshot));
  }).catch(() => {});
}

function mindOutlineEntries(text) {
  // Document structure covers headings and nested bullets. Heading mutations
  // still target heading lines; bullets are navigable structure nodes.
  return structureEntries(text);
}

function buildMindTree(entries) {
  return structureTree(entries);
}

function mindDragPayload(doc, node, entries) {
  const snapshot = notesFeature?.getDraftSnapshot?.(doc.id);
  const index = (entries || []).findIndex((entry) => entry.line === node.line);
  const ancestors = [];
  for (const entry of (entries || []).slice(0, index + 1)) {
    while (ancestors.length && ancestors[ancestors.length - 1].level >= entry.level) ancestors.pop();
    ancestors.push({ line:entry.line, text:entry.text, level:entry.level });
  }
  return {
    version:1, type:'copal-heading-reparent', accountId:state.accountId,
    workspace:state.workspace, epoch:state.contextEpoch,
    storageNamespace:state.storageNamespace, policyContext:'copal-graph-mind',
    docId:String(doc.id), path:ancestors,
    resourceKey:cloneEnvelope(doc.resource?.key || doc.resourceKey || null),
    revision:cloneEnvelope(snapshot?.expectedRevision || doc.resource?.revision || { kind:'copalHead', value:String(doc.head || '') }),
    line:node.line, text:node.text, localRevision:Number(snapshot?.localRevision || 0), generation:state.contextEpoch,
  };
}

function parseMindDragPayload(event) {
  try {
    const value = event.dataTransfer?.getData('application/x-copal-heading');
    if (!value || value.length > 8192) return null;
    const payload = JSON.parse(value || 'null');
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
    const allowed = new Set(['version','type','accountId','workspace','epoch','storageNamespace','policyContext','docId','path','resourceKey','revision','line','text','localRevision','generation']);
    if (Object.keys(payload).some((key) => !allowed.has(key)) || Object.keys(payload).length !== allowed.size) return null;
    if (payload.version !== 1 || payload.type !== 'copal-heading-reparent' || !Number.isSafeInteger(payload.epoch) || !Number.isSafeInteger(payload.generation) || !Number.isSafeInteger(payload.line) || payload.line < 1 || !Number.isSafeInteger(payload.localRevision) || payload.localRevision < 0 || typeof payload.accountId !== 'string' || payload.accountId.length < 1 || payload.accountId.length > 256 || typeof payload.workspace !== 'string' || payload.workspace.length < 1 || payload.workspace.length > 256 || typeof payload.storageNamespace !== 'string' || payload.storageNamespace.length < 1 || payload.storageNamespace.length > 256 || payload.policyContext !== 'copal-graph-mind' || typeof payload.docId !== 'string' || payload.docId.length < 1 || payload.docId.length > 256 || typeof payload.text !== 'string' || payload.text.length > 512 || !Array.isArray(payload.path) || payload.path.length < 1 || payload.path.length > 64) return null;
    const key = payload.resourceKey;
    if (!key || typeof key !== 'object' || Array.isArray(key) || !['accountId','workspaceId','provider','resourceId'].every((field) => typeof key[field] === 'string' && key[field].length > 0 && key[field].length <= 512) || !['copal','host','treehouse'].includes(key.provider) || Object.keys(key).length !== 4) return null;
    const revision = payload.revision;
    if (!revision || typeof revision !== 'object' || Array.isArray(revision) || typeof revision.kind !== 'string' || !['copalHead','hostFingerprint','domain'].includes(revision.kind) || revision.kind.length > 64 || typeof revision.value !== 'string' || revision.value.length < 1 || revision.value.length > 512 || Object.keys(revision).length !== 2) return null;
    if (payload.path.some((entry) => !entry || typeof entry !== 'object' || Object.keys(entry).some((key) => !['line','text','level'].includes(key)) || !Number.isSafeInteger(entry.line) || entry.line < 1 || typeof entry.text !== 'string' || entry.text.length > 512 || !Number.isSafeInteger(entry.level) || entry.level < 1 || entry.level > 6)) return null;
    return payload;
  } catch (_) { return null; }
}

function validMindDrop(payload, doc, target, entries) {
  // Read-only sources must never advertise a move target.  Keep this pure so
  // dragover validation does not mutate status or trigger another render.
  if (doc?.readOnly === true || doc?.builtin === true || doc?.note_error || doc?.rawPreserved === true) return false;
  if (!payload || payload.accountId !== state.accountId || payload.workspace !== state.workspace || payload.storageNamespace !== state.storageNamespace || payload.policyContext !== 'copal-graph-mind' || payload.epoch !== state.contextEpoch || payload.generation !== state.contextEpoch || payload.docId !== String(doc.id)) return false;
  if (payload.resourceKey?.accountId !== state.accountId || payload.resourceKey?.workspaceId !== state.workspace) return false;
  const current = notesFeature?.getDraftSnapshot?.(doc.id);
  const currentKey = doc.resource?.key || doc.resourceKey || null;
  if (!currentKey || !sameResourceKey(payload.resourceKey, currentKey)) return false;
  const currentRevision = current?.expectedRevision || { kind:'copalHead', value:String(current?.base ?? doc.resource?.revision?.value ?? doc.head ?? '') };
  if (JSON.stringify(payload.revision || null) !== JSON.stringify(currentRevision)) return false;
  if (Number(payload.localRevision) !== Number(current?.localRevision || 0)) return false;
  const from = entries.find((entry) => entry.line === Number(payload.line));
  if (!from || from.line === target.line) return false;
  const fromIndex = entries.indexOf(from);
  const path = [];
  for (const entry of entries.slice(0, fromIndex + 1)) {
    while (path.length && path[path.length - 1].level >= entry.level) path.pop();
    path.push({ line:entry.line, text:entry.text, level:entry.level });
  }
  if (Array.isArray(payload.path) && JSON.stringify(payload.path) !== JSON.stringify(path)) return false;
  // A branch cannot be inserted beneath itself or one of its descendants.
  const targetIndex = entries.findIndex((entry) => entry.line === target.line);
  if (targetIndex > fromIndex && entries.slice(fromIndex + 1, targetIndex + 1).some((entry) => entry.level <= from.level)) return true;
  return !(targetIndex > fromIndex && entries.slice(fromIndex + 1, targetIndex + 1).every((entry) => entry.level > from.level));
}

function isAncestor(potential, node, entries) {
  const pi = entries.indexOf(potential);
  const ni = entries.indexOf(node);
  if (pi >= ni) return false;
  for (let i = pi + 1; i < ni; i++) {
    if (entries[i].level <= potential.level) return false;
  }
  return true;
}

function renderMindTree(nodes, doc, entries, depth = 0, allNodes = nodes) {
  const list = h('ul', { class: 'copal-mind-tree-list' });
  for (const node of nodes) {
    const navigation = mindModeState();
    const isCollapsed = navigation.collapsed.has(node.line);
    const isSelected = navigation.selectedLine === node.line;
    const isEditing = navigation.editingLine === node.line;
    const hasChildren = node.children.length > 0;

    const label = h('span', { class: 'copal-mind-tree-label', text: node.text });
    if (isEditing) {
      const input = h('input', { class: 'copal-mind-tree-input', type: 'text', value: node.text });
      label.replaceChildren(input);
      requestAnimationFrame(() => { input.focus(); input.select(); });
      input.addEventListener('blur', () => {
        const next = input.value.trim();
        if (next && next !== node.text) {
          if (node.kind === 'bullet') {
            // Bullet text is rewritten in place, keeping its marker and indent.
            notesFeature?.applyDocumentTransaction?.(doc, (source) => {
              const parts = String(source ?? '').split(/\r\n|\n|\r/);
              const index = node.line - 1;
              const line = parts[index] ?? '';
              const rewritten = line.replace(/^(\s*(?:[-*+]|\d{1,9}[.)])\s+)(.*)$/, (_match, prefix) => `${prefix}${next}`);
              if (rewritten !== line) parts[index] = rewritten;
              return parts.join(source.match(/\r\n|\n|\r/)?.[0] || '\n');
            }, { origin:'bullet-rename' });
          } else {
            notesFeature?.applyDocumentTransaction?.(doc, (source) => renameHeading(source, node.line, next), { origin:'heading-rename' });
          }
        }
          navigation.editingLine = null;
        renderMind();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { navigation.editingLine = null; renderMind(); }
        e.stopPropagation();
      });
    }

    const toggle = h('button', { class: 'copal-mind-tree-toggle', text: hasChildren ? (isCollapsed ? '\u25B6' : '\u25BC') : '\u00A0' });
    toggle.addEventListener('click', (e) => { e.stopPropagation(); if (hasChildren) { if (navigation.collapsed.has(node.line)) navigation.collapsed.delete(node.line); else { navigation.collapsed.add(node.line); if (flattenMindTree(node.children).some((child) => child.line === navigation.selectedLine)) navigation.selectedLine = node.line; } persistGraphView(); renderMind(); } });

    // Bullets are navigable structure nodes; heading transactions stay bound
    // to heading lines only so source semantics and revisions are preserved.
    const isHeading = node.kind !== 'bullet';
    const nodeEl = h('li', { class: `copal-mind-tree-node kind-${isHeading ? 'heading' : 'bullet'}${isSelected ? ' selected' : ''}`, tabindex: '0', 'data-line': String(node.line), 'data-structure-kind': isHeading ? 'heading' : 'bullet' }, toggle, label);

    nodeEl.addEventListener('click', (e) => { e.stopPropagation(); navigation.selectedLine = node.line; persistGraphView(); renderMind(); });
    nodeEl.addEventListener('dblclick', (e) => { e.preventDefault(); e.stopPropagation(); navigation.editingLine = node.line; renderMind(); });
    nodeEl.addEventListener('keydown', (e) => {
      if (isEditing) return;
      if (e.key === 'Enter' || e.key === 'F2') { e.preventDefault(); navigation.editingLine = node.line; renderMind(); return; }
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); if (isHeading) mindMindDeleteHeading(doc, node); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); mindSelectNext(node, entries); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); mindSelectPrev(node, entries); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); if (hasChildren && isCollapsed) { navigation.collapsed.delete(node.line); persistGraphView(); renderMind(); } else if (hasChildren) { const next = node.children[0]; navigation.selectedLine = next.line; persistGraphView(); renderMind(); } return; }
      if (e.key === 'ArrowLeft') { e.preventDefault(); if (hasChildren && !isCollapsed) { navigation.collapsed.add(node.line); if (flattenMindTree(node.children).some((child) => child.line === navigation.selectedLine)) navigation.selectedLine = node.line; persistGraphView(); renderMind(); } else { const parent = findMindParent(node, allNodes); if (parent) { navigation.selectedLine = parent.line; persistGraphView(); renderMind(); } } return; }
      if (e.key === 'Tab') { e.preventDefault(); if (!isHeading) return; const newLv = Math.max(1, Math.min(6, node.level + (e.shiftKey ? -1 : 1))); mindMindRenameHeading(doc, node, newLv); return; }
      if (e.key === ' ') { e.preventDefault(); if (hasChildren) { if (navigation.collapsed.has(node.line)) navigation.collapsed.delete(node.line); else navigation.collapsed.add(node.line); persistGraphView(); renderMind(); } return; }
    });

    // Drag reparent
    // Read-only/recovery sources must not advertise a reparent affordance.
    // Keep the handlers installed for defensive synthetic events, while the
    // browser never offers a native drag from an unavailable source.
    nodeEl.draggable = isHeading && !(doc.readOnly === true || doc.builtin === true || doc.note_error || doc.rawPreserved === true);
    nodeEl.addEventListener('dragstart', (e) => {
      const payload = mindDragPayload(doc, node, entries);
      navigation.draggingPayload = payload;
      e.dataTransfer.setData('application/x-copal-heading', JSON.stringify(payload));
      e.dataTransfer.effectAllowed = mindMutationAllowed(doc) && payload.accountId === state.accountId && payload.workspace === state.workspace ? 'move' : 'none';
      nodeEl.classList.add('dragging');
    });
    nodeEl.addEventListener('dragend', () => { navigation.draggingLine = null; navigation.draggingPayload = null; nodeEl.classList.remove('dragging'); });
    nodeEl.addEventListener('dragover', (e) => {
      const payload = parseMindDragPayload(e);
      if (!validMindDrop(payload, doc, node, entries)) { e.preventDefault(); e.dataTransfer.dropEffect = 'none'; return; }
      e.preventDefault(); e.dataTransfer.dropEffect = 'move'; nodeEl.classList.add('drag-over');
    });
    nodeEl.addEventListener('dragleave', () => nodeEl.classList.remove('drag-over'));
    nodeEl.addEventListener('drop', (e) => {
      e.preventDefault(); nodeEl.classList.remove('drag-over');
      const payload = parseMindDragPayload(e);
      const valid = validMindDrop(payload, doc, node, entries);
      navigation.draggingLine = null;
      navigation.draggingPayload = null;
      if (valid) mindMindReparent(doc, payload, node.line);
      else setViewStatus('graph', 'Heading drop rejected because its source, scope, or revision changed.', true);
    });

    if (!isCollapsed && hasChildren) nodeEl.append(renderMindTree(node.children, doc, entries, depth + 1, allNodes));
    list.append(nodeEl);
  }
  return list;
}

function flattenMindTree(nodes, output = []) {
  for (const node of nodes || []) { output.push(node); flattenMindTree(node.children, output); }
  return output;
}

function findMindParent(node, nodes) {
  for (const candidate of nodes || []) {
    if (candidate.children?.some((child) => child.line === node.line)) return candidate;
    const nested = findMindParent(node, candidate.children);
    if (nested) return nested;
  }
  return null;
}


function mindSelectNext(node, entries) {
  const visible = [...state.body.querySelectorAll('.copal-mind-tree-node')].map((item) => Number(item.dataset.line));
  const sequence = visible.length ? visible : entries.map((entry) => entry.line);
  const idx = sequence.indexOf(node.line);
  if (idx >= 0 && idx < sequence.length - 1) { mindModeState().selectedLine = sequence[idx + 1]; persistGraphView(); renderMind(); }
}

function mindSelectPrev(node, entries) {
  const visible = [...state.body.querySelectorAll('.copal-mind-tree-node')].map((item) => Number(item.dataset.line));
  const sequence = visible.length ? visible : entries.map((entry) => entry.line);
  const idx = sequence.indexOf(node.line);
  if (idx > 0) { mindModeState().selectedLine = sequence[idx - 1]; persistGraphView(); renderMind(); }
}

function mindMindDeleteHeading(doc, node) {
  if (!mindMutationAllowed(doc)) return;
  const result = notesFeature?.applyDocumentTransaction?.(doc, (source) => deleteHeadingSection(source, node.line), { origin:'heading-delete' });
  if (result?.outcome === 'queued') { mindModeState().selectedLine = null; persistGraphView(); renderMind(); }
}

function mindSectionFingerprint(source, entry) {
  const lines = String(source ?? '').split(/\r\n|\n|\r/);
  const entries = mindOutlineEntries(source);
  const index = entries.findIndex((item) => item.line === entry.line);
  if (index < 0) return '';
  let end = lines.length;
  for (let i = index + 1; i < entries.length; i += 1) {
    if (entries[i].level <= entry.level) { end = entries[i].line - 1; break; }
  }
  return lines.slice(entry.line - 1, end).map((line) => line.replace(/^[ \t]{0,3}#{1,6}(?=[ \t])/, '#')).join('\n');
}

function mindMutationAllowed(doc) {
  if (doc?.readOnly === true || doc?.builtin === true || doc?.note_error || doc?.rawPreserved === true) {
    setViewStatus('graph', 'This source is read-only until it is recovered or converted.', true);
    return false;
  }
  return true;
}

function remapMindSelection(result, original, preferredLine = null) {
  if (result?.outcome !== 'queued' || !result.content) return;
  const resultEntries = mindOutlineEntries(result.content);
  const fingerprintMatches = original.fingerprint ? resultEntries.filter((entry) => mindSectionFingerprint(result.content, entry) === original.fingerprint) : [];
  const matches = (fingerprintMatches.length ? fingerprintMatches : resultEntries.filter((entry) => entry.text === original.text));
  const next = matches.sort((left, right) => Math.abs(left.line - Number(preferredLine || original.line)) - Math.abs(right.line - Number(preferredLine || original.line)))[0];
  if (next) mindModeState().selectedLine = next.line;
}

function mindMindRenameHeading(doc, node, newLevel) {
  if (!mindMutationAllowed(doc)) return;
  const result = notesFeature?.applyDocumentTransaction?.(doc, (source) => changeHeadingLevel(source, node.line, newLevel), { origin:'heading-level' });
  remapMindSelection(result, node, node.line);
  if (result?.outcome === 'queued') renderMind();
}

function mindMindReparent(doc, payload, toLine) {
  if (!mindMutationAllowed(doc)) return;
  const entries = mindOutlineEntries(wikiText(doc));
  if (!validMindDrop(payload, doc, { line:Number(toLine) }, entries)) return;
  const fromLine = Number(payload.line);
  const original = entries.find((entry) => entry.line === fromLine) || { text:'' };
  original.fingerprint = mindSectionFingerprint(wikiText(doc), original);
  const result = notesFeature?.applyDocumentTransaction?.(doc, (source) => reparentHeadingSection(source, fromLine, toLine), { origin:'heading-reparent' });
  remapMindSelection(result, original, fromLine);
  if (result?.outcome === 'queued') renderMind();
}

function renderMind() {
  state.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
  const graphView = getGraphView();
  const allDocs = visibleDocs();
  const docs = allDocs.filter((doc) => ['wiki', 'note', 'markdown', 'text'].includes(String(doc.kind || '').toLowerCase()));
  const context = state.windows.get('graph');
  const navigation = mindModeState();
  const requested = graphView.source?.docId || navigation.docId || context?.selected || state.selected;
  const doc = requested ? docs.find((d) => d.id === requested) : docs[0];
  const requestedAny = requested ? allDocs.find((item) => item.id === requested) : null;
  if (graphView.source?.docId && requestedAny && !doc) {
    state.body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' }, h('h2', { text:'Structure source unsupported' }), h('p', { text:`${requestedAny.name || requestedAny.id} is a ${requestedAny.kind || 'non-text'} resource and has no heading tree.` }), h('button', { class:'copal-btn', text:'Open source', onclick:() => openDocument(requestedAny.id, requestedAny.kind === 'wiki' ? 'wiki' : 'notes') }), h('button', { class:'copal-btn', text:'Choose another source', onclick:() => { graphView.source = null; navigation.docId = null; persistGraphView(); renderMind(); } })));
    return;
  }
  // Keep an explicit source identity visible through deletion, revocation, or
  // an unavailable projection. Never silently substitute another document.
  if (graphView.source?.docId && !doc) {
    state.body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' },
      h('h2', { text:'Structure source unavailable' }),
      h('p', { text:`The selected source ${graphView.source.docId} is unavailable in this account or workspace.` }),
      h('p', { text:'Its ResourceKey and revision are retained so it can be retried or reauthorized safely.' }),
      h('button', { class:'copal-btn', text:'Retry source', 'aria-label':'Retry unavailable structure source', onclick:() => loadDocuments().then(() => renderMind()).catch((error) => setViewStatus('graph', error.message, true)) }),
      h('button', { class:'copal-btn', text:'Choose another source', onclick:() => { graphView.source = null; navigation.docId = null; persistGraphView(); renderMind(); } }),
    ));
    return;
  }
  if (!doc) {
    state.body.replaceChildren(h('div', { class: 'copal-mind-empty' }, h('h2', { text: 'Structure' }), h('p', { text: 'No document source is available for headings.' }), h('button', { class:'copal-btn', text:'Open Editor', onclick:() => open('notes') })));
    return;
  }
  navigation.docId = doc.id;
  graphView.source ||= graphSourceEnvelope(doc);
  persistGraphView();
  if (context) context.selected = doc.id;
  state.selected = doc.id;
  const source = wikiText(doc);
  if (doc.rawPreserved && doc.recoveryState !== 'legacy-import') {
    const parseFailed = doc.recoveryState === 'malformed-preserved';
    const sourceLabel = doc.recoveryState === 'unsupported-future' ? 'This source was written by a newer Wiki format.' : parseFailed ? 'This source could not be parsed as a native Wiki record.' : 'This source is preserved but unavailable to the headings parser.';
    state.body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' }, h('h2', { text:parseFailed ? 'Structure source parse failed' : 'Structure source unavailable' }), h('p', { text:sourceLabel }), h('p', { text:String(doc.note_error || 'Choose an explicit recovery action in the Editor.') }), h('button', { class:'copal-btn', text:'Open in Editor', onclick:() => openDocument(doc.id, 'wiki') })));
    return;
  }
  const entries = mindOutlineEntries(source);
  const treeNodes = buildMindTree(entries);
  const mindEl = h('div', { class: 'copal-mind' });

  const familyMode = h('select', { class:'copal-mind-mode', 'aria-label':'Graph family mode', 'data-graph-mode':true },
    h('option', { value:'documents', text:'Documents · links' }),
    h('option', { value:'structure', text:'Structure · headings and bullets' }),
    h('option', { value:'galaxy', text:'Galaxy · tracks and events' }));
  familyMode.value = 'structure';
  familyMode.addEventListener('change', () => {
    const next = familyMode.value;
    getGraphView().mode = next;
    persistGraphView();
    if (next === 'structure') renderMind(); else renderGraph();
    updateRoute('graph', true);
  });
  mindEl.append(h('div', { class: 'copal-timeline-toolbar' }, h('label', {}, 'Graph view ', familyMode)));

  // Document picker
  const picker = h('div', { class: 'copal-mind-picker' }, h('h3', { text: 'Documents' }));
  const pickerSearch = h('input', { class:'copal-mind-doc-search', type:'search', placeholder:'Search documents…', 'aria-label':'Search structure sources' });
  const docList = h('ul', { class: 'copal-mind-doc-list' });
  const pickerStatus = h('div', { class:'copal-mind-picker-status', role:'status' });
  const renderPicker = () => {
    const query = pickerSearch.value.trim().toLowerCase();
    const matches = docs.filter((item) => !query || String(item.name || '').toLowerCase().includes(query));
    const rows = matches.slice(0, 100);
    if (navigation.docId && !rows.some((item) => item.id === navigation.docId)) {
      const selected = matches.find((item) => item.id === navigation.docId);
      if (selected) rows.unshift(selected);
    }
    docList.replaceChildren(...rows.map((d) => h('li', { class: `copal-mind-doc-item${d.id === navigation.docId ? ' active' : ''}` },
      h('button', { class: 'copal-mind-doc-btn', text: d.name, 'aria-label':`Select ${d.name} as structure source`, onclick: () => { navigation.docId = d.id; graphView.source = graphSourceEnvelope(d); if (context) context.selected = d.id; state.selected = d.id; navigation.collapsed.clear(); navigation.selectedLine = null; navigation.editingLine = null; persistGraphView(); renderMind(); } }))));
    pickerStatus.textContent = matches.length > rows.length ? `${matches.length} sources · showing ${rows.length}; search to reach more` : `${matches.length} source${matches.length === 1 ? '' : 's'}`;
  };
  pickerSearch.addEventListener('input', renderPicker);
  picker.append(pickerSearch, pickerStatus, docList);
  renderPicker();

  // Tree
  const treePane = h('div', { class: 'copal-mind-tree' });
  if (!treeNodes.length) {
    treePane.append(h('div', { class: 'copal-mind-empty-tree' }, h('p', { text: 'No headings or bullets found in this source.' }), h('button', { class:'copal-btn', text:'Open source', onclick:() => openDocument(doc.id, doc.kind === 'wiki' ? 'wiki' : 'notes') })));
  } else {
    // Toolbar
    const toolbar = h('div', { class: 'copal-mind-toolbar' },
      h('button', { class: 'copal-btn', icon:'add', text:'Heading', onclick: () => mindMindAddHeading(doc) }),
      h('button', { class: 'copal-btn', text: 'Delete', onclick: () => { if (navigation.selectedLine) { const n = flattenMindTree(treeNodes).find((e) => e.line === navigation.selectedLine); if (n) mindMindDeleteHeading(doc, n); } } }),
      h('button', { class: 'copal-btn', text: '\u2191', title: 'Move up', 'aria-label':'Move heading up', onclick: () => { if (navigation.selectedLine && mindMutationAllowed(doc)) { const original = entries.find((entry) => entry.line === navigation.selectedLine) || { text:'' }; const result = notesFeature?.applyDocumentTransaction?.(doc, (current) => moveGraphHeadingSection(current, navigation.selectedLine, -1), { origin:'heading-move' }); remapMindSelection(result, original, navigation.selectedLine); if (result?.outcome === 'queued') renderMind(); } } }),
      h('button', { class: 'copal-btn', text: '\u2193', title: 'Move down', 'aria-label':'Move heading down', onclick: () => { if (navigation.selectedLine && mindMutationAllowed(doc)) { const original = entries.find((entry) => entry.line === navigation.selectedLine) || { text:'' }; const result = notesFeature?.applyDocumentTransaction?.(doc, (current) => moveGraphHeadingSection(current, navigation.selectedLine, 1), { origin:'heading-move' }); remapMindSelection(result, original, navigation.selectedLine); if (result?.outcome === 'queued') renderMind(); } } }),
      h('span', { class: 'copal-mind-doc-name', text: doc.name }),
      h('span', { class: 'copal-mind-headings-count', text: `${entries.filter((entry) => entry.kind !== 'bullet').length} headings · ${entries.filter((entry) => entry.kind === 'bullet').length} bullets` }),
    );
    treePane.append(toolbar);
    const treeList = renderMindTree(treeNodes, doc, entries);
    treePane.append(treeList);
  }

  mindEl.append(picker, treePane);
  state.body.replaceChildren(mindEl);
  acknowledgeVisible(mindEl, 'graph.mode.presented', { mode:'structure', nodeCount:entries.length }, { accountId:state.accountId, workspaceId:state.workspace });

  // Focus selected
  if (navigation.selectedLine) {
    const selected = mindEl.querySelector(`[data-line="${navigation.selectedLine}"]`);
    if (selected) selected.focus();
  }
}

function mindMindAddHeading(doc) {
  if (!mindMutationAllowed(doc)) return;
  const navigation = mindModeState();
  const source = wikiText(doc);
  const entries = mindOutlineEntries(source);
  let insertionIndex = null, level = 1;
  if (navigation.selectedLine) {
    const sel = entries.find((e) => e.line === navigation.selectedLine);
    if (sel) {
      level = sel.level;
      // Find end of selected section
      const idx = entries.indexOf(sel);
      let endLine = null;
      for (let i = idx + 1; i < entries.length; i++) {
        if (entries[i].level <= sel.level) { endLine = entries[i].line; break; }
      }
      insertionIndex = endLine == null ? source.split(/\r\n|\n|\r/).length : endLine - 1;
    }
  }
  const selectionIndex = (() => { let index = insertionIndex == null ? source.split(/\r\n|\n|\r/).length : insertionIndex; while (index > 0 && source.split(/\r\n|\n|\r/)[index - 1] === '') index -= 1; return index; })();
  const result = notesFeature?.applyDocumentTransaction?.(doc, (current) => {
    const parts = current.split(/\r\n|\n|\r/);
    let index = insertionIndex;
    if (index == null) index = parts.length;
    while (index > 0 && parts[index - 1] === '') index -= 1;
    parts.splice(index, 0, `${'#'.repeat(level)} New heading`);
    return parts.join(current.match(/\r\n|\n|\r/)?.[0] || '\n');
  }, { origin:'heading-add' });
  if (result?.outcome === 'queued') { const newLine = selectionIndex + 1; navigation.selectedLine = newLine; navigation.editingLine = newLine; persistGraphView(); renderMind(); }
}

function baseField(label, control) {
  return h('label', { class: 'copal-base-field' }, h('span', { text: label }), control);
}

function parseBaseLiteral(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  try { return JSON.parse(text); } catch (_) { return text; }
}

async function createBaseDocument({ onComplete = null } = {}) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: 'Create a live Base' }));
  const name = h('input', { value: 'Projects.base', 'aria-label': 'Base name' });
  dialog.append(baseField('Name', name), h('p', { text: 'Starts as a live table over this Copal workspace. Configure columns, filters, sorts, groups, formulas, and summaries after creation.' }));
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create', onclick: async () => {
      const safeName = name.value.trim().endsWith('.base') ? name.value.trim() : `${name.value.trim()}.base`;
      if (!safeName || safeName === '.base') return;
      try {
        const created = await api('/documents', { method: 'POST', body: JSON.stringify({ name: safeName, kind: 'base', content: serializeBase(makeDefaultBase(safeName.replace(/\.base$/i, ''))) }) });
        state.baseId = created.doc?.id || null;
        dialog.close();
        await loadDocuments();
        onComplete?.();
      } catch (error) { setStatus(error.message, true); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus(); name.select();
}

async function renameBaseDocument(base, onComplete = null) {
  const proposed = await styledPrompt('Choose a new Base name.', { title: 'Rename Base', defaultValue: base.name, confirmText: 'Rename', maxLength: 160 });
  if (!proposed?.trim()) return;
  const name = proposed.trim().endsWith('.base') ? proposed.trim() : `${proposed.trim()}.base`;
  try {
    await api(`/documents/${encodeURIComponent(base.id)}/rename`, { method: 'POST', body: JSON.stringify({ name }) });
    await loadDocuments(false); onComplete?.();
  } catch (error) { setStatus(error.message, true); }
}

async function duplicateBaseDocument(base, onComplete = null) {
  const stem = base.name.replace(/\.base$/i, '');
  const proposed = await styledPrompt('Choose a name for the duplicate Base.', { title: 'Duplicate Base', defaultValue: `${stem} copy.base`, confirmText: 'Duplicate', maxLength: 160 });
  if (!proposed?.trim()) return;
  const name = proposed.trim().endsWith('.base') ? proposed.trim() : `${proposed.trim()}.base`;
  try {
    const created = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: 'base', content: base.text || serializeBase(state.baseDefinition || makeDefaultBase(stem)) }) });
    state.baseId = created.doc?.id || null; state.baseView = null; state.basePage = 1;
    await loadDocuments(); onComplete?.();
  } catch (error) { setStatus(error.message, true); }
}

function parseDesignerLines(text, separator, mapper) {
  return String(text || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const index = line.indexOf(separator);
    const left = index < 0 ? line : line.slice(0, index).trim();
    const right = index < 0 ? '' : line.slice(index + separator.length).trim();
    return mapper(left, right);
  });
}

function configureBase(base, definition, viewId, onComplete = null) {
  const next = JSON.parse(JSON.stringify(definition));
  const view = next.views.find((item) => item.id === viewId) || next.views[0];
  const dialog = h('dialog', { class: 'copal-dialog copal-base-designer' }, h('h2', { text: `Configure ${view.name}` }));
  const name = h('input', { value: view.name });
  const columns = h('textarea', { rows: '4', placeholder: 'file.name | Name | 220' }); columns.value = view.columns.filter((column) => !column.formula).map((column) => `${column.property} | ${column.label || column.property}${column.width ? ` | ${column.width}` : ''}`).join('\n');
  const formulas = h('textarea', { rows: '3', placeholder: 'total = price * quantity' }); formulas.value = view.columns.filter((column) => column.formula).map((column) => `${column.property} = ${column.formula}`).join('\n');
  const filters = h('textarea', { rows: '3', placeholder: 'status | eq | active' });
  const hasNested = hasNestedFilterGroups(view.filters);
  const filterRawJson = h('textarea', { rows: '5', placeholder: '{"and": [...]}' });
  const filterMode = h('select'); for (const mode of ['and', 'or']) filterMode.append(h('option', { value: mode, text: mode.toUpperCase(), selected: !!view.filters?.[mode] }));
  filters.value = flattenFilterToLines(view.filters).join('\n');
  filterRawJson.value = JSON.stringify(view.filters, null, 2);
  if (hasNested) { filterRawJson.style.display = ''; filters.style.display = 'none'; } else { filterRawJson.style.display = 'none'; }
  const useRawJson = h('label', { class: 'copal-base-field' }, h('input', { type: 'checkbox', checked: hasNested }), ' Edit filter as raw JSON');
  useRawJson.querySelector('input').addEventListener('change', (e) => {
    filterRawJson.style.display = e.target.checked ? '' : 'none';
    filters.style.display = e.target.checked ? 'none' : '';
  });
  const sorts = h('textarea', { rows: '2', placeholder: 'file.name : asc' }); sorts.value = (view.sorts || []).map((sort) => `${sort.property} : ${sort.direction}`).join('\n');
  const groupBy = h('input', { value: view.groupBy || '', placeholder: 'category' });
  const summaries = h('textarea', { rows: '2', placeholder: 'price : avg' }); summaries.value = Object.entries(view.summaries || {}).map(([property, operation]) => `${property} : ${operation}`).join('\n');
  const limit = h('input', { type: 'number', min: '1', max: '5000', value: String(view.limit || 1000) });
  dialog.append(
    baseField('View name', name),
    baseField('Columns (one “property | label | width” per line)', columns),
    baseField('Formulas (one “property = expression” per line)', formulas),
    baseField('Filter mode', filterMode),
    baseField('Filters (one “property | operator | value” per line)', filters),
    useRawJson,
    baseField('Filter JSON (raw)', filterRawJson),
    baseField('Sorts (one “property : asc/desc” per line)', sorts),
    baseField('Group by property', groupBy),
    baseField('Summaries (one “property : count/sum/avg/min/max/distinct” per line)', summaries),
    baseField('Maximum rows', limit),
  );
  const feedback = h('p', { class: 'copal-base-feedback', role: 'status' });
  dialog.append(feedback, h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Validate & save', onclick: async () => {
      try {
        view.name = name.value.trim() || 'Table';
        view.columns = columns.value.split(/\n|,(?![^|]*\|)/).map((line) => line.trim()).filter(Boolean).map((line) => {
          const [property, label, width] = line.split('|').map((part) => part.trim());
          return { property, label: label || property, ...(Number(width) > 0 ? { width: Math.round(Number(width)) } : {}) };
        });
        for (const formula of parseDesignerLines(formulas.value, '=', (property, expression) => ({ property, label: property, formula: expression }))) view.columns.push(formula);
        const useRaw = useRawJson.querySelector('input').checked;
        if (useRaw) {
          const rawText = filterRawJson.value.trim();
          if (!rawText) { view.filters = null; }
          else {
            let parsed;
            try { parsed = JSON.parse(rawText); } catch { throw new Error('Invalid JSON in filter editor'); }
            view.filters = parsed;
          }
        } else {
          view.filters = parseFilterLines(filters.value.split('\n'), filterMode.value);
        }
        view.sorts = parseDesignerLines(sorts.value, ':', (property, direction) => ({ property, direction: (direction || 'asc').toLowerCase() }));
        view.groupBy = groupBy.value.trim() || null;
        view.summaries = Object.fromEntries(parseDesignerLines(summaries.value, ':', (property, operation) => [property, (operation || 'count').toLowerCase()]));
        view.limit = Number(limit.value) || 1000;
        const validation = await api('/bases/validate', { method: 'POST', body: JSON.stringify({ content: serializeBase(next) }) });
        const saved = await saveDocument(base, validation.canonical, false);
        if (!saved) return;
        state.baseDefinition = validation.definition;
        state.baseView = view.id;
        dialog.close(); onComplete?.();
      } catch (error) { feedback.textContent = error.message; feedback.classList.add('error'); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus();
}

async function addBaseView(base, definition, onComplete = null) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: 'Add Base View' }));
  const viewName = h('input', { value: 'Table 2', 'aria-label': 'View name' });
  const viewType = h('select', { 'aria-label': 'View type' });
  for (const type of VIEW_TYPES) viewType.append(h('option', { value: type, text: type.charAt(0).toUpperCase() + type.slice(1), selected: type === 'table' }));
  dialog.append(baseField('View name', viewName), baseField('View type', viewType));
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create', onclick: async () => {
      const name = viewName.value.trim();
      if (!name) return;
      const next = JSON.parse(JSON.stringify(definition));
      const template = makeViewFromTemplate(next.views[0] || makeDefaultBase().views[0], name, viewType.value);
      next.views.push(template);
      const validation = await api('/bases/validate', { method: 'POST', body: JSON.stringify({ content: serializeBase(next) }) });
      if (await saveDocument(base, validation.canonical, false)) { state.baseView = template.id; state.baseDefinition = validation.definition; dialog.close(); onComplete?.(); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); viewName.focus();
}

function makeBaseColumnResizer(base, definition, viewId, property, cell, onComplete = null) {
  const handle = h('span', { class: 'copal-base-resize', role: 'separator', tabindex: '0', 'aria-label': `Resize ${property} column` });
  const persist = async (width) => {
    const next = JSON.parse(JSON.stringify(definition));
    const view = next.views.find((item) => item.id === viewId) || next.views[0];
    const column = view.columns.find((item) => item.property === property);
    if (!column) return;
    column.width = Math.max(80, Math.min(600, Math.round(width)));
    if (await saveDocument(base, serializeBase(next), false)) onComplete?.();
  };
  handle.addEventListener('pointerdown', (event) => {
    event.preventDefault(); event.stopPropagation();
    const startX = event.clientX; const startWidth = cell.getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId);
    const move = (nextEvent) => { cell.style.width = `${Math.max(80, Math.min(600, startWidth + nextEvent.clientX - startX))}px`; };
    const end = async () => {
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end);
      await persist(cell.getBoundingClientRect().width);
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', end); handle.addEventListener('pointercancel', end);
  });
  handle.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault(); persist(cell.getBoundingClientRect().width + (event.key === 'ArrowRight' ? 16 : -16));
  });
  return handle;
}

function makeInlineCellEditor(base, row, column, td, onComplete = null) {
  const current = row.values?.[column.property];
  const prop = column.property;
  const isBoolean = current === true || current === false;
  const isNumber = typeof current === 'number';
  const isArray = Array.isArray(current);
  let input;
  if (isBoolean) {
    input = h('input', { type: 'checkbox', class: 'copal-base-inline-editor', 'aria-label': column.label });
    input.checked = !!current;
  } else if (isNumber) {
    input = h('input', { type: 'number', class: 'copal-base-inline-editor', value: current == null ? '' : String(current), 'aria-label': column.label });
  } else if (isArray) {
    input = h('input', { type: 'text', class: 'copal-base-inline-editor', value: (current || []).join(', '), 'aria-label': column.label, placeholder: 'tag1, tag2' });
  } else {
    input = h('input', { type: 'text', class: 'copal-base-inline-editor', value: current == null ? '' : String(current), 'aria-label': column.label });
  }
  const commit = async () => {
    let newValue;
    if (isBoolean) { newValue = input.checked; }
    else if (isNumber) { newValue = input.value.trim() === '' ? null : Number(input.value); }
    else if (isArray) { newValue = input.value.split(',').map((s) => s.trim()).filter(Boolean); }
    else { newValue = input.value; }
    try {
      await api(`/bases/${encodeURIComponent(base.id)}/rows/${encodeURIComponent(row.documentId)}`, {
        method: 'PATCH', body: JSON.stringify({ property: prop, value: newValue, base: row.head }),
      });
      await loadDocuments(false); onComplete?.();
    } catch (error) { setStatus(error.message, true); }
  };
  const cancel = () => { onComplete?.(); };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !isBoolean) { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    if (e.key === 'Tab') { e.preventDefault(); commit(); }
    e.stopPropagation();
  });
  input.addEventListener('blur', () => { setTimeout(cancel, 150); });
  td.replaceChildren(input);
  requestAnimationFrame(() => { input.focus(); if (input.select) input.select(); });
  return input;
}

async function renderBases(targetBody = state.body, embeddedDoc = null) {
  const mount = targetBody || state.body;
  const rerender = () => renderBases(mount, embeddedDoc);
  const token = ++state.baseQueryToken;
  const bases = state.docs.filter((doc) => doc.kind === 'base');
  if (!bases.length) {
    mount.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'No Bases yet' }), h('p', { text: 'Create a Base to query live Redb documents. No sample rows are fabricated.' }), h('button', { class: 'copal-btn primary', text: 'Create Base', onclick: () => createBaseDocument({ onComplete: rerender }) })));
    return;
  }
  let base = embeddedDoc && bases.some((doc) => doc.id === embeddedDoc.id) ? embeddedDoc : (bases.find((doc) => doc.id === state.baseId) || bases[0]); state.baseId = base.id;
  const shell = h('div', { class: 'copal-bases-workspace copal-base-leaf' });
  const browser = h('aside', { class: 'copal-pane copal-base-browser' }, h('div', { class: 'copal-pane-header' }, h('span', { text: 'Bases' }), h('button', { class: 'copal-btn', text: 'Trash', title: 'Restore deleted Bases', onclick: () => showTrash('base') }), h('button', { class: 'copal-btn', icon:'add', title: 'Create Base', 'aria-label': 'Create Base', onclick: () => createBaseDocument({ onComplete: rerender }) })));
  const list = h('div', { class: 'copal-scroll' });
  for (const item of bases) list.append(h('button', { class: `copal-doc-row${item.id === base.id ? ' active' : ''}`, 'data-base-id': item.id, text: item.name, onclick: () => { state.baseId = item.id; state.baseView = null; state.basePage = 1; rerender(); } }));
  browser.append(list);
  const main = h('section', { class: 'copal-pane copal-base-main' }, h('div', { class: 'copal-empty', text: 'Querying live Redb documents…' }));
  shell.append(browser, main); mount.replaceChildren(shell);
  try {
    const viewParam = state.baseView ? `&view=${encodeURIComponent(state.baseView)}` : '';
    const pageSize = state.basePageSize || 100;
    const result = await api(`/bases/${encodeURIComponent(base.id)}/query?page=${state.basePage}&page_size=${pageSize}${viewParam}`);
    if (token !== state.baseQueryToken) return;
    state.baseDefinition = result.definition;
    // D1: Track source documents for scoped invalidation
    const sourceDocs = new Set();
    for (const row of result.rows) sourceDocs.add(row.documentId);
    state.baseSourceDocs.set(base.id, sourceDocs);
    const view = result.view; state.baseView = view.id;
    const toolbar = h('div', { class: 'copal-base-toolbar' }, h('strong', { text: base.name }));
    const viewSelect = h('select', { 'aria-label': 'Base view' });
    for (const item of result.definition.views) viewSelect.append(h('option', { value: item.id, text: item.name, selected: item.id === view.id }));
    viewSelect.value = view.id;
    viewSelect.addEventListener('change', () => { state.baseView = viewSelect.value; state.basePage = 1; rerender(); });
    toolbar.append(viewSelect,
      h('span', { class:'copal-base-count', text:`${result.sourceTruncated ? 'Partial results: ' : ''}${result.total} result${result.total === 1 ? '' : 's'} from ${result.sourceCount} documents${result.resultLimited ? ` · view limited to ${result.resultLimit}` : ''}` }),
      h('button', { class: 'copal-btn', text: 'Configure', onclick: () => configureBase(base, result.definition, view.id, rerender) }),
      h('button', { class: 'copal-btn', icon:'add', text:'View', onclick: () => addBaseView(base, result.definition, rerender) }),
      h('button', { class: 'copal-btn', text: 'Rename', onclick: () => renameBaseDocument(base, rerender) }),
      h('button', { class: 'copal-btn', text: 'Duplicate', onclick: () => duplicateBaseDocument(base, rerender) }),
      h('button', { class: 'copal-btn', text: 'History', onclick: () => showHistory(base) }),
      h('button', { class: 'copal-btn', text: '↑', title: 'Move view left', disabled: result.definition.views.findIndex((v) => v.id === view.id) <= 0, onclick: async () => {
        const reordered = reorderBaseView(result.definition, view.id, 'up');
        if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; rerender(); }
      } }),
      h('button', { class: 'copal-btn', text: '↓', title: 'Move view right', disabled: result.definition.views.findIndex((v) => v.id === view.id) >= result.definition.views.length - 1, onclick: async () => {
        const reordered = reorderBaseView(result.definition, view.id, 'down');
        if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; rerender(); }
      } }),
      h('button', { class: 'copal-btn danger', icon:'close', text:'View', title: 'Delete this view', disabled: result.definition.views.length <= 1, onclick: async () => {
        if (!await styledConfirm(`Delete view "${view.name}"?`, { title: 'Delete Base view', confirmText: 'Delete view', danger: true })) return;
        const removed = removeBaseView(result.definition, view.id);
        if (removed && await saveDocument(base, serializeBase(removed), false)) { state.baseDefinition = removed; state.baseView = null; rerender(); }
      } }),
      h('button', { class: 'copal-btn danger', text: 'Trash', onclick: () => deleteDocument(base) }));
    const messages = h('div');
    for (const diagnostic of result.diagnostics || []) messages.append(h('p', { class: 'copal-base-diagnostic', text: diagnostic.message }));
    if (result.sourceTruncated) messages.append(h('p', { class:'copal-base-diagnostic error', text:`Only the first ${result.sourceCount} documents were scanned. Sorting and summaries cover this partial result; additional matches may exist.` }));
    if (result.resultLimited) messages.append(h('p', { class:'copal-base-diagnostic', text:`This view shows the first ${result.resultLimit} sorted matches. Summaries include those results across all pages.` }));
    if (!result.rows.length) {
      main.replaceChildren(toolbar, messages, h('div', { class: 'copal-empty', text: 'This live query returned no rows. Adjust its filters or add matching document properties.' }));
      return;
    }
    const renderTableRow = (row) => {
        const tr = h('tr', { 'data-document-id': row.documentId });
        for (const column of view.columns) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const td = h('td', { 'data-copal-context-object':'base-cell', 'data-document-id':row.documentId, 'data-base-id':base.id, 'data-column-property':column.property });
          td.append(h('button', {
            class: 'copal-base-cell', type: 'button', text: value,
            title: editable ? `Edit ${column.property}` : `Open ${row.name}`,
            'aria-label': editable ? `Edit ${column.label} for ${row.name}: ${value}` : `Open ${row.name}: ${value}`,
            onclick: () => { if (editable) makeInlineCellEditor(base, row, column, td, rerender); else openDocument(row.documentId, 'notes'); },
          }));
          tr.append(td);
        }
        return tr;
      };
    let contentWrap;
    if (view.type === 'card') {
      contentWrap = h('div', { class: 'copal-base-card-view' });
      const allRows = result.groups?.length ? result.groups.flatMap((g) => g.rows) : result.rows;
      for (const row of allRows) {
        const card = h('div', { class: 'copal-base-card', 'data-document-id': row.documentId });
        for (const column of view.columns) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const field = h('div', { class: 'copal-base-card-field' }, h('span', { class: 'copal-base-card-label', text: column.label }));
          const valueEl = h('span', { class: 'copal-base-card-value', text: value });
          if (editable) {
            valueEl.style.cursor = 'pointer';
            valueEl.addEventListener('click', () => {
              const td = h('td', {});
              makeInlineCellEditor(base, row, column, td, rerender);
              valueEl.replaceChildren(...td.childNodes);
            });
          } else {
            valueEl.addEventListener('click', () => openDocument(row.documentId, 'notes'));
          }
          field.append(valueEl);
          card.append(field);
        }
        contentWrap.append(card);
      }
    } else if (view.type === 'list') {
      contentWrap = h('div', { class: 'copal-base-list-view' });
      const allRows = result.groups?.length ? result.groups.flatMap((g) => g.rows) : result.rows;
      for (const row of allRows) {
        const item = h('div', { class: 'copal-base-list-item', 'data-document-id': row.documentId });
        const mainCol = view.columns[0];
        const title = h('span', { class: 'copal-base-list-title', text: mainCol ? formatBaseCell(row.values?.[mainCol.property]) : row.name });
        title.addEventListener('click', () => openDocument(row.documentId, 'notes'));
        item.append(title);
        for (const column of view.columns.slice(1)) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const meta = h('span', { class: 'copal-base-list-meta', text: `${column.label}: ${value}` });
          if (editable) {
            meta.style.cursor = 'pointer';
            meta.addEventListener('click', () => {
              const td = h('td', {});
              makeInlineCellEditor(base, row, column, td, rerender);
              meta.replaceChildren(...td.childNodes);
            });
          }
          item.append(meta);
        }
        contentWrap.append(item);
      }
    } else {
      const tableWrap = h('div', { class: 'copal-base-table-wrap' });
      const table = h('table', { class: 'copal-table copal-base-table' });
      const headRow = h('tr');
      for (let colIdx = 0; colIdx < view.columns.length; colIdx++) {
        const column = view.columns[colIdx];
        const sortIndex = (view.sorts || []).findIndex((sort) => sort.property === column.property);
        const sort = sortIndex >= 0 ? view.sorts[sortIndex] : null;
        const cell = h('th', { style: column.width ? `width:${column.width}px` : '' }, h('button', {
          class: 'copal-base-sort', type: 'button',
          'aria-label': `Sort by ${column.label}${sort ? `, ${sort.direction}, priority ${sortIndex + 1}` : ''}`,
          icon: sort ? (sort.direction === 'asc' ? 'chevron-up' : 'chevron-down') : '',
          text: `${column.label}${sort && view.sorts.length > 1 ? ` ${sortIndex + 1}` : ''}`,
          onclick: async (event) => {
            const next = updateViewSort(result.definition, view.id, column.property, event.shiftKey);
            if (await saveDocument(base, serializeBase(next), false)) { state.baseDefinition = next; state.basePage = 1; rerender(); }
          },
        }));
        // D4: Column drag-reorder
        cell.draggable = true;
        cell.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(colIdx)); e.dataTransfer.effectAllowed = 'move'; cell.classList.add('copal-base-col-dragging'); });
        cell.addEventListener('dragend', () => { cell.classList.remove('copal-base-col-dragging'); });
        cell.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; cell.classList.add('copal-base-col-drop'); });
        cell.addEventListener('dragleave', () => { cell.classList.remove('copal-base-col-drop'); });
        cell.addEventListener('drop', async (e) => {
          e.preventDefault(); cell.classList.remove('copal-base-col-drop');
          const fromIdx = Number(e.dataTransfer.getData('text/plain'));
          if (Number.isNaN(fromIdx) || fromIdx === colIdx) return;
          const reordered = reorderBaseColumn(result.definition, view.id, fromIdx, colIdx);
          if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; rerender(); }
        });
        cell.append(makeBaseColumnResizer(base, result.definition, view.id, column.property, cell, rerender));
        headRow.append(cell);
      }
      table.append(h('thead', {}, headRow));
      const body = h('tbody');
      const appendRows = (rows, group = null) => {
        if (group != null) body.append(h('tr', { class: 'copal-base-group' }, h('th', { colspan: String(view.columns.length), text: `${view.groupBy}: ${group}` })));
        for (const row of rows) body.append(renderTableRow(row));
      };
      if (result.groups?.length) for (const group of result.groups) appendRows(group.rows, group.key); else appendRows(result.rows);
      table.append(body);
      if (Object.keys(result.summaries || {}).length) {
        const footer = h('tr');
        for (const column of view.columns) footer.append(h('td', { text: result.summaries[column.property] == null ? '' : `${view.summaries[column.property]}: ${formatBaseCell(result.summaries[column.property])}` }));
        table.append(h('tfoot', {}, footer));
      }
      tableWrap.append(table);
      contentWrap = tableWrap;

      // D3: Keyboard grid navigation
      const totalRows = body.querySelectorAll('tr[data-document-id]').length;
      const totalCols = view.columns.length;
      const setActiveCell = (row, col) => {
        table.querySelectorAll('.copal-base-active-cell').forEach((el) => el.classList.remove('copal-base-active-cell'));
        if (row < 0 || row >= totalRows || col < 0 || col >= totalCols) return;
        const tr = body.querySelectorAll('tr[data-document-id]')[row];
        if (!tr) return;
        const td = tr.querySelectorAll('td')[col];
        if (td) { td.classList.add('copal-base-active-cell'); td.scrollIntoView({ block: 'nearest' }); }
        state.baseFocusRow = row; state.baseFocusCol = col;
      };
      table.addEventListener('click', (e) => {
        const td = e.target.closest('td');
        if (!td) return;
        const tr = td.closest('tr[data-document-id]');
        if (!tr) return;
        const rowIdx = [...body.querySelectorAll('tr[data-document-id]')].indexOf(tr);
        const colIdx = [...tr.querySelectorAll('td')].indexOf(td);
        if (rowIdx >= 0 && colIdx >= 0) setActiveCell(rowIdx, colIdx);
      });
      table.addEventListener('keydown', (e) => {
        const key = e.key;
        if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter', 'Escape', 'Tab'].includes(key)) return;
        const r = state.baseFocusRow, c = state.baseFocusCol;
        if (key === 'ArrowUp') { e.preventDefault(); setActiveCell(Math.max(0, r - 1), c); }
        else if (key === 'ArrowDown') { e.preventDefault(); setActiveCell(Math.min(totalRows - 1, r + 1), c); }
        else if (key === 'ArrowLeft') { e.preventDefault(); setActiveCell(r, Math.max(0, c - 1)); }
        else if (key === 'ArrowRight') { e.preventDefault(); setActiveCell(r, Math.min(totalCols - 1, c + 1)); }
        else if (key === 'Tab') { e.preventDefault(); const next = e.shiftKey ? c - 1 : c + 1; if (next >= 0 && next < totalCols) setActiveCell(r, next); }
        else if (key === 'Enter') {
          e.preventDefault();
          const tr = body.querySelectorAll('tr[data-document-id]')[r];
          if (!tr) return;
          const td = tr.querySelectorAll('td')[c];
          if (!td) return;
          const btn = td.querySelector('.copal-base-cell');
          if (btn) btn.click();
        }
        else if (key === 'Escape') { e.preventDefault(); setActiveCell(-1, -1); }
      });
    }
    const pageSizeSelect = h('select', { 'aria-label': 'Page size', class: 'copal-base-page-size' });
    for (const size of [25, 50, 100, 200, 500]) pageSizeSelect.append(h('option', { value: String(size), text: `${size}/page`, selected: size === pageSize }));
    pageSizeSelect.addEventListener('change', () => { state.basePageSize = Number(pageSizeSelect.value); state.basePage = 1; rerender(); });
    const pagination = h('nav', { class: 'copal-base-pagination', 'aria-label': 'Base result pages' },
      h('button', { class: 'copal-btn', text: 'Previous', disabled: result.page <= 1, onclick: () => { state.basePage = Math.max(1, result.page - 1); rerender(); } }),
      h('span', { text: `Page ${result.page} of ${result.pages}` }),
      h('button', { class: 'copal-btn', text: 'Next', disabled: result.page >= result.pages, onclick: () => { state.basePage = Math.min(result.pages, result.page + 1); rerender(); } }),
      pageSizeSelect);
    main.replaceChildren(toolbar, messages, contentWrap, pagination);
  } catch (error) {
    if (token !== state.baseQueryToken) return;
    main.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'Base query failed' }), h('p', { text: error.message }), h('button', { class: 'copal-btn', text: 'Edit Base definition', onclick: () => openDocument(base.id, 'notes') })));
  }
}

function taskTextHash(value) {
  let hash = 2166136261;
  for (const character of String(value || '')) { hash ^= character.codePointAt(0); hash = Math.imul(hash, 16777619); }
  return (hash >>> 0).toString(16).padStart(8, '0');
}

function taskItems() {
  if (state.taskProjectionLoaded) {
    const docsById = new Map(state.docs.map((doc) => [String(doc.id), doc]));
    return state.taskProjection.map((item) => {
      const metadata = item.document || {};
      const doc = docsById.get(String(metadata.id)) || { ...metadata, resourceKey:item.resourceKey, resource:{ key:item.resourceKey } };
      return { ...item, doc, task:{ id:item.id, blockId:item.anchor?.blockId, line:item.line, text:String(item.text || ''), done:!!item.checked } };
    });
  }
  const items = [];
  for (const doc of visibleDocs()) for (const task of doc.tasks || []) {
    const block = (doc.blocks || []).find((candidate) => candidate?.id === task.blockId);
    const lines = String(doc.text || '').split('\n');
    const lineIndex = block ? (doc.blocks || []).indexOf(block) : Math.max(0, Number(task.line || 1) - 1);
    const from = lines.slice(0, lineIndex).reduce((offset, line) => offset + line.length + 1, 0);
    const raw = lines[lineIndex] || '';
    items.push({
      type:'markdown', source:'vault', id:String(task.id || `${doc.id}:line:${lineIndex}`),
      resourceKey:doc.resource?.key || doc.resourceKey || null, sourceRevision:doc.head || null,
      anchor:{ blockId:task.blockId || null, sourceRange:{ from, to:from + raw.length }, expectedTextHash:taskTextHash(raw), expectedText:raw },
      text:String(task.text || ''), checked:!!task.done,
      doc, task:{ ...task, text:String(task.text || ''), done:!!task.done, line:lineIndex + 1 }, label:doc.name,
    });
  }
  return items;
}

const treeHouse = createTreeHouseFeature({ h, api, setStatus, renderMarkdown, openDocument });

function resourceIdentityToken(resourceKey) {
  if (!resourceKey || typeof resourceKey !== 'object') return null;
  return ['provider', 'accountId', 'workspaceId', 'resourceId']
    .map((field) => String(resourceKey[field] ?? '')).join('\u001f');
}

function cellResourceIdentity(cell = {}) {
  const resourceKey = cell.resourceKey || cell.resource?.key || null;
  const resourceToken = resourceIdentityToken(resourceKey);
  if (resourceToken) return `resource:${resourceToken}`;
  if (cell.documentId != null) return `document:${String(cell.documentId)}`;
  return `row:${String(cell.rowKey ?? '')}`;
}

function commandRowKey(row = {}) {
  const key = row.resourceKey || row.resource?.key || row.documentId || row.id || '';
  return typeof key === 'string' ? key : JSON.stringify(key);
}

function baseSourceForRow(row = {}) {
  const documentId = row.documentId ?? null;
  const resourceKey = row.resourceKey || row.resource?.key || null;
  const hasResourceKey = resourceKey != null;
  let source = hasResourceKey
    ? state.docs.find((doc) => sameResourceKey(doc.resource?.key || doc.resourceKey, resourceKey))
    : null;
  if (hasResourceKey && !source) {
    return { source:null, snapshot:null, reason:'The row ResourceKey no longer identifies an authorized source.' };
  }
  if (source && documentId != null && String(source.id) !== String(documentId)) {
    return { source:null, snapshot:null, reason:'The row identity no longer matches its source.' };
  }
  if (!source && documentId != null) source = state.docs.find((doc) => String(doc.id) === String(documentId));
  const snapshot = source ? notesFeature?.getDraftSnapshot?.(source.id) : null;
  return { source, snapshot };
}

async function transformBaseCommand(base, request, command) {
  // A successful buffered Base write updates the authoritative document in
  // state.docs and discards its clean draft. The leaf cache can still hold
  // the pre-write object, so never reuse its old CAS head for the next
  // command.
  const currentBase = state.docs.find((item) => String(item.id) === String(base.id)) || base;
  // A clean resource buffer still carries the last acknowledged provider head;
  // use it as the next command's CAS source while retaining dirty-draft
  // precedence for source composition.
  let draftSnapshot = notesFeature?.getDraftSnapshot?.(currentBase.id) || null;
  let authoritativeSnapshot = notesFeature?.getAuthoritativeSnapshot?.(currentBase.id) || null;
  let snapshot = draftSnapshot || authoritativeSnapshot || null;
  let source = String(snapshot?.envelope?.text ?? currentBase.text ?? '');
  let localRevision = snapshot?.localRevision ?? null;
  let transformed = null;
  // The server transform is intentionally a preview.  If typing or another
  // command advances this draft while the request is in flight, recompute on
  // that newest source rather than applying an old full-document result.
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const revision = snapshot?.expectedRevision?.value ?? currentBase.head ?? null;
    transformed = await api(`/bases/${encodeURIComponent(currentBase.id)}/transform`, {
      method:'POST', body:JSON.stringify({ command, source, base:revision }),
    }, request?.scope?.workspace || state.workspace);
    const newest = notesFeature?.getDraftSnapshot?.(base.id) || null;
    const advanced = newest && (
      (localRevision != null && newest.localRevision !== localRevision)
      || (localRevision == null && String(newest.envelope?.text ?? '') !== source)
    );
    if (!advanced) break;
    draftSnapshot = newest;
    authoritativeSnapshot = notesFeature?.getAuthoritativeSnapshot?.(base.id) || null;
    snapshot = draftSnapshot || authoritativeSnapshot || null;
    source = String(snapshot.envelope?.text ?? currentBase.text ?? '');
    localRevision = snapshot.localRevision ?? null;
    transformed = null;
  }
  if (!transformed) return { outcome:'conflict', message:'The Base changed while this command was being prepared. Retry it on the newest draft.' };
  const latest = notesFeature?.getDraftSnapshot?.(base.id) || null;
  if (latest && localRevision != null && latest.localRevision !== localRevision) {
    return { outcome:'conflict', message:'The Base changed while this command was being prepared. Retry it on the newest draft.' };
  }
  const revision = snapshot?.expectedRevision?.value ?? currentBase.head ?? null;
  const definitionRevision = transformed.definitionRevision ?? localRevision ?? request?.definitionRevision ?? null;
  if (!transformed.changed) return { outcome:'unchanged', definition:transformed.definition, definitionRevision, revision:transformed.revision, source:transformed.source, baseLocalRevision:localRevision };
  const receipt = await saveDocument({ ...currentBase, head:revision }, transformed.source, false, 'notes', {
    returnReceipt:true,
    // A clean authoritative snapshot supplies the CAS head but must not be
    // treated as an explicit conflict rebase. Dirty snapshots retain their
    // local composition/rebase semantics.
    snapshot:draftSnapshot,
    baseRevision:revision,
    scope:request?.scope || null,
  });
  return { ...receipt, definition:transformed.definition, definitionRevision:receipt?.localRevision ?? receipt?.acknowledgedLocalRevision ?? definitionRevision, baseLocalRevision:localRevision, source:transformed.source };
}

function applyBaseCellsToEnvelope(source, rowCells, envelope = {}) {
  const properties = { ...(envelope.properties || {}) };
  let content = String(envelope.text ?? source.text ?? '');
  const markdownSource = source.kind === 'markdown' && source.format !== 'copal-note-v1';
  let skipped = 0;
  for (const cell of rowCells) {
    if (cell.readOnly === true || cell.editorKind === 'readonly' || String(cell.columnKey || '').startsWith('file.')) { skipped += 1; continue; }
    let value = cell.value;
    if (cell.editorKind === 'checkbox') value = value === true || value === 'true';
    else if (cell.editorKind === 'number') value = String(value).trim() === '' ? null : Number(value);
    else if (cell.editorKind === 'list') value = String(value).split(',').map((item) => item.trim()).filter(Boolean);
    const property = canonicalSourceProperty(cell.columnKey);
    if (String(property).startsWith('formula.')) { skipped += 1; continue; }
    if (markdownSource) content = setFrontmatterProperty(content, property, value, { clear:cell.clear === true });
    else if (cell.clear === true) delete properties[property];
    else properties[property] = value;
  }
  return { envelope:{ text:content, properties, relations:envelope.relations || [], extensions:envelope.extensions }, skipped };
}

function pasteFailureKind(receipt, retryState = null) {
  if (receipt?.outcome === 'conflict' || receipt?.status === 409) return 'conflict';
  return retryState?.replayable === true ? 'replay' : 'none';
}

function pasteReceipt(value) {
  if (value === true) return { outcome:'applied' };
  return value && typeof value === 'object' ? value : { outcome:'failed', message:'The source did not return a save receipt.' };
}

function pastePartialResult(base, receipts, failures, skipped) {
  if (!failures.length) return skipped ? { ...(receipts.at(-1)?.receipt || { outcome:'unchanged' }), receipts, skipped } : { ...(receipts.at(-1)?.receipt || { outcome:'unchanged' }), receipts };
  const replayable = failures.filter((failure) => failure.retryKind === 'replay');
  const conflicts = failures.filter((failure) => failure.retryKind === 'conflict');
  const result = { outcome:'partial', receipts, failures, skipped };
  if (replayable.length) result.retry = () => retryBaseCellFailures(base, replayable, 'replay');
  if (conflicts.length) result.reviewConflict = () => retryBaseCellFailures(base, conflicts, 'rebase');
  return result;
}

async function retryBaseCellFailures(base, failedRows, mode) {
  const receipts = []; const failures = []; let skipped = 0;
  for (const failure of failedRows) {
    const cells = failure.cells || [];
    const { source, snapshot, reason } = baseSourceForRow(failure);
    let receipt;
    if (!source) receipt = { outcome:'failed', message:reason || 'The source row is no longer available.' };
    else if (mode === 'replay') {
      // ResourceBuffer.flush() reuses the immutable pending action and base;
      // do not queue the cell envelope again after an uncertain response.
      receipt = pasteReceipt(await notesFeature?.retryDocumentSave?.(source.id));
    } else {
      const remote = failure.remote;
      const remoteHead = String(remote?.head || '');
      if (!remoteHead) receipt = { outcome:'conflict', message:'The latest source head is unavailable; compare it again.', remote };
      else {
        const remoteEnvelope = { text:remote.text ?? source.text ?? '', properties:remote.properties || {}, relations:remote.relations || [], extensions:remote.extensions };
        const transformed = applyBaseCellsToEnvelope(source, cells, remoteEnvelope);
        skipped += transformed.skipped;
        receipt = pasteReceipt(await notesFeature?.rebaseDocumentAtRevision?.(source.id, { kind:'copalHead', value:remoteHead }, transformed.envelope, { sheet:true }));
      }
    }
    const entry = { rowKey:failure.rowKey, documentId:failure.documentId, resourceKey:failure.resourceKey, sourceId:source?.id || failure.sourceId, cells, receipt };
    receipts.push(entry);
    if (!['applied', 'unchanged', 'queued'].includes(receipt.outcome)) failures.push({ ...entry, ...receipt, retryKind:pasteFailureKind(receipt, notesFeature?.documentRetryState?.(source?.id)), cells });
  }
  return pastePartialResult(base, receipts, failures, skipped);
}

async function applyBaseCells(base, cells = []) {
  const grouped = new Map();
  let skipped = 0;
  for (const cell of cells) {
    const identity = cellResourceIdentity(cell);
    if (!grouped.has(identity)) grouped.set(identity, []);
    grouped.get(identity).push(cell);
  }
  const receipts = []; const failures = [];
  for (const [, rowCells] of grouped) {
    const row = rowCells[0]; const { source, snapshot, reason } = baseSourceForRow(row);
    if (!source) { failures.push({ rowKey:row.rowKey, documentId:row.documentId, resourceKey:row.resourceKey, cells:rowCells, retryKind:'none', outcome:'failed', message:reason || 'The source row is no longer available.' }); continue; }
    const transformed = applyBaseCellsToEnvelope(source, rowCells, snapshot?.envelope || { text:source.text || '', properties:source.properties || {}, relations:source.relations || [] });
    skipped += transformed.skipped;
    let receipt;
    try {
      notesFeature?.queueSave?.({ ...source, properties:transformed.envelope.properties, relations:transformed.envelope.relations, extensions:transformed.envelope.extensions }, transformed.envelope.text, { baseRevision:snapshot?.expectedRevision?.value, origin:'sheet-cell', history:true, sheet:true });
      receipt = { outcome:'queued', localRevision:notesFeature?.getDraftSnapshot?.(source.id)?.localRevision };
    } catch (error) { receipt = { outcome:'failed', message:error.message }; }
    const entry = { rowKey:row.rowKey, documentId:row.documentId, resourceKey:row.resourceKey, sourceId:source.id, cells:rowCells, receipt };
    receipts.push(entry);
    if (!['applied', 'unchanged', 'queued'].includes(receipt.outcome)) failures.push({ ...entry, ...receipt, retryKind:pasteFailureKind(receipt, notesFeature?.documentRetryState?.(source.id)) });
  }
  return pastePartialResult(base, receipts, failures, skipped);
}

function createBaseSheetAdapter() {
  return {
      query:async (doc, request) => {
      const viewParam = request.viewId ? `&view=${encodeURIComponent(request.viewId)}` : '';
      const queryParam = request.query?.text ? `&query=${encodeURIComponent(request.query.text)}` : '';
      const draft = notesFeature?.getDraftSnapshot?.(doc.id);
      if (draft?.envelope?.text != null) {
        return api(`/bases/${encodeURIComponent(doc.id)}/query/preview?view=${encodeURIComponent(request.viewId || '')}&query=${encodeURIComponent(request.query?.text || '')}&page=1&page_size=100`, {
          method:'POST', body:JSON.stringify({ source:String(draft.envelope.text), base:draft.expectedRevision?.value || null, definitionRevision:draft.localRevision == null ? request.definitionRevision == null ? null : Number(request.definitionRevision) : Number(draft.localRevision) }), signal:request.signal,
        }, request.scope?.workspace || state.workspace);
      }
      return api(`/bases/${encodeURIComponent(doc.id)}/query?page=1&page_size=100${viewParam}${queryParam}`, { signal:request.signal }, request.scope?.workspace || state.workspace);
    },
    cellEdit:(doc, request) => applyBaseCells(doc, [{ rowKey:commandRowKey(request.row), documentId:request.row?.documentId ?? request.row?.id, resourceKey:request.row?.resourceKey || request.row?.resource?.key || null, columnKey:request.column.property, value:request.value, editorKind:request.column.type }]),
    clear:(doc, cells) => applyBaseCells(doc, cells),
    paste:(doc, preview) => applyBaseCells(doc, preview.cells),
    command:async (doc, request) => {
      const property = canonicalSourceProperty(request.payload?.property || request.view?.columns?.[0]?.property);
      if (request.command === 'add-view' || request.command === 'duplicate-view' || request.command === 'rename-view' || request.command === 'remove-view' || request.command === 'reorder-view') {
        return transformBaseCommand(doc, request, { action:request.command, view_id:request.payload?.viewId || request.viewId, name:request.payload?.name, view_type:request.payload?.viewType, new_view_id:request.payload?.newViewId, from_index:request.payload?.fromIndex, to_index:request.payload?.toIndex });
      }
      if (!property && !['set-filter'].includes(request.command)) return { outcome:'failed', message:'The selected column is unavailable.' };
      if (request.command === 'filter') return transformBaseCommand(doc, request, { action:'set-filter', view_id:request.viewId, scope:'view', filter:request.payload?.filter || null });
      if (request.command === 'group') return transformBaseCommand(doc, request, { action:'set-grouping', view_id:request.viewId, property:request.payload?.groupBy || property });
      if (request.command === 'columns') return transformBaseCommand(doc, request, { action:'set-column-visibility', view_id:request.viewId, property, visible:request.payload?.visible !== false });
      if (request.command === 'column-label') return transformBaseCommand(doc, request, { action:'label-column', view_id:request.viewId, property, label:request.payload?.label });
      if (request.command === 'column-summary') return transformBaseCommand(doc, request, { action:'set-summary', view_id:request.viewId, property, operation:request.payload?.operation || 'count' });
      if (request.command === 'column-width') return transformBaseCommand(doc, request, { action:'resize-column', view_id:request.viewId, property, width:Math.max(80, Math.min(600, Math.round(Number(request.payload?.width) || 120))) });
      if (request.command === 'reorder-column') {
        const columns = request.view?.columns || [];
        const fromIndex = columns.findIndex((item) => item.property === request.payload?.fromProperty);
        const toIndex = columns.findIndex((item) => item.property === request.payload?.toProperty);
        if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return { outcome:'failed', message:'The column changed before reorder was committed.' };
        return transformBaseCommand(doc, request, { action:'reorder-column', view_id:request.viewId, from_index:fromIndex, to_index:toIndex });
      }
      if (request.command === 'reorder-sort') return transformBaseCommand(doc, request, { action:'reorder-sort', view_id:request.viewId, from_index:request.payload?.fromIndex, to_index:request.payload?.toIndex });
      if (request.command !== 'sort') return { outcome:'unavailable', message:'This typed sheet command is unavailable.' };
      const currentSort = (request.view?.sorts || []).find((item) => item.property === property);
      const direction = currentSort?.direction === 'asc' ? 'desc' : currentSort?.direction === 'desc' ? 'none' : 'asc';
      return transformBaseCommand(doc, request, {
        action:'set-sort', view_id:request.viewId, property, direction,
        replace:request.payload?.additive !== true,
      });
    },
      columnMenu:async (doc, column, controller) => {
      const menu = h('dialog', { class:'copal-dialog copal-sheet-column-menu-dialog' }, h('h2', { text:`${column.label} column` }));
      const run = (command, payload = {}) => { menu.close(); void controller.command(command, { property:column.property, ...payload }); };
      const currentView = controller.getState().definition.views.find((item) => item.id === controller.getState().viewId);
      const sortIndex = (currentView?.sorts || []).findIndex((item) => item.property === column.property);
      const sortCount = currentView?.sorts?.length || 0;
      const columnIndex = (currentView?.columns || []).findIndex((item) => item.property === column.property);
      const columnCount = currentView?.columns?.length || 0;
      menu.append(h('div', { class:'copal-dialog-actions' },
        h('button', { class:'copal-btn', text:'Sort', onclick:() => run('sort') }),
        h('button', { class:'copal-btn', text:'Add sort priority', onclick:() => run('sort', { additive:true }) }),
        h('button', { class:'copal-btn', text:'Move sort priority up', disabled:sortIndex <= 0, onclick:() => run('reorder-sort', { fromIndex:sortIndex, toIndex:Math.max(0, sortIndex - 1) }) }),
        h('button', { class:'copal-btn', text:'Move sort priority down', disabled:sortIndex < 0 || sortIndex >= sortCount - 1, onclick:() => run('reorder-sort', { fromIndex:sortIndex, toIndex:Math.min(sortCount - 1, sortIndex + 1) }) }),
        h('button', { class:'copal-btn', text:'Move column left', 'aria-label':`Move ${column.label} column left`, disabled:columnIndex <= 0, onclick:() => run('reorder-column', { fromProperty:column.property, toProperty:currentView?.columns?.[columnIndex - 1]?.property }) }),
        h('button', { class:'copal-btn', text:'Move column right', 'aria-label':`Move ${column.label} column right`, disabled:columnIndex < 0 || columnIndex >= columnCount - 1, onclick:() => run('reorder-column', { fromProperty:column.property, toProperty:currentView?.columns?.[columnIndex + 1]?.property }) }),
        h('button', { class:'copal-btn', text:column.visible === false ? 'Show column' : 'Hide column', onclick:() => run('columns', { visible:column.visible === false }) }),
        h('button', { class:'copal-btn', text:'Group by this', onclick:() => run('group', { groupBy:column.property }) }),
        h('button', { class:'copal-btn', text:'Count summary', onclick:() => run('column-summary', { operation:'count' }) }),
        h('button', { class:'copal-btn', text:'Rename label', onclick:async () => { const label = await styledPrompt('Column label', { title:'Rename column', defaultValue:column.label, confirmText:'Save', maxLength:128 }); if (label?.trim()) run('column-label', { label:label.trim() }); } }),
      ));
      wireDialog(menu); document.body.append(menu); menu.showModal(); menu.querySelector('button:not([disabled])')?.focus({ preventScroll:true });
      },
    columnResize:(doc, column, width, controller, sheetState) => {
      const view = sheetState.definition?.views?.find((item) => item.id === sheetState.viewId);
      if (!view?.columns?.some((item) => item.property === column.property) || !Number.isFinite(Number(width))) return { outcome:'failed', message:'The column changed before resize was committed.' };
      return controller.command('column-width', { property:column.property, width });
    },
    columnReorder:(doc, fromProperty, toProperty, controller, sheetState, payloadScope) => {
      if (JSON.stringify(payloadScope) !== JSON.stringify(sheetState.scope)) return { outcome:'failed', message:'The column drag came from a different scope.' };
      const view = sheetState.definition?.views?.find((item) => item.id === sheetState.viewId);
      if (!view?.columns?.some((item) => item.property === fromProperty) || !view.columns.some((item) => item.property === toProperty) || fromProperty === toProperty) return { outcome:'failed', message:'The column changed before reorder was committed.' };
      return controller.command('reorder-column', { fromProperty, toProperty });
    },
    contextCommand:(doc, command, node, controller) => {
      if (command === 'edit-sheet-column') return controller.command('sort', { property:node.dataset.sheetColumnKey });
      return false;
    },
    viewCommand:async (doc, action, view, controller) => {
      if (action === 'rename-view') {
        const name = await styledPrompt('Rename view', { title:'Rename saved view', defaultValue:view?.name || 'Table', confirmText:'Save', maxLength:128 });
        if (!name?.trim()) return;
        return controller.command(action, { viewId:view?.id, name:name.trim() });
      }
      if (action === 'add-view') {
        const menu = h('dialog', { class:'copal-dialog copal-sheet-view-create-menu' }, h('h2', { text:'Add saved view' }));
        const name = h('input', { value:'Table 2', 'aria-label':'View name', maxlength:'128' });
        const type = h('select', { 'aria-label':'View type' });
        for (const value of VIEW_TYPES) type.append(h('option', { value, text:value.charAt(0).toUpperCase() + value.slice(1) }));
        const create = () => { const value = name.value.trim(); if (!value) return; menu.close(); void controller.command(action, { name:value, viewType:type.value }); };
        menu.append(h('label', { text:'View name' }), name, h('label', { text:'View type' }), type, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => menu.close() }), h('button', { class:'copal-btn primary', text:'Create', onclick:create })));
        wireDialog(menu); document.body.append(menu); menu.showModal(); name.focus(); return;
      }
      const menu = h('dialog', { class:'copal-dialog copal-sheet-view-menu' }, h('h2', { text:`${view?.name || 'View'} actions` }));
      const run = (command, payload = {}) => { menu.close(); void controller.command(command, { viewId:view?.id, ...payload }); };
      const viewIndex = controller.getState().definition.views.findIndex((item) => item.id === view?.id);
      menu.append(h('div', { class:'copal-dialog-actions' },
        h('button', { class:'copal-btn', text:'Duplicate view', onclick:() => run('duplicate-view', { name:`${view.name} copy` }) }),
        h('button', { class:'copal-btn', text:'Move left', disabled:viewIndex <= 0, onclick:() => run('reorder-view', { fromIndex:viewIndex, toIndex:Math.max(0, viewIndex - 1) }) }),
        h('button', { class:'copal-btn', text:'Move right', disabled:viewIndex < 0 || viewIndex >= controller.getState().definition.views.length - 1, onclick:() => run('reorder-view', { fromIndex:viewIndex, toIndex:Math.min(controller.getState().definition.views.length - 1, viewIndex + 1) }) }),
        h('button', { class:'copal-btn danger', text:'Delete view', disabled:controller.getState().definition.views.length <= 1, onclick:() => run('remove-view') }),
      ));
      wireDialog(menu); document.body.append(menu); menu.showModal(); menu.querySelector('button:not([disabled])')?.focus({ preventScroll:true });
    },
    toolbar:async (doc, action, controller) => {
      const selectedState = controller.getState();
      const activeView = selectedState.definition?.views?.find((item) => item.id === selectedState.viewId) || selectedState.definition?.views?.[0];
      const columns = activeView?.columns || [];
      if (!columns.length) return { outcome:'unavailable', message:'This view has no columns to configure.' };
      const selected = selectedState.selected?.focus?.columnKey;
      const picker = h('select', { 'aria-label':'Column' }, ...columns.map((column) => h('option', { value:column.property, text:column.label, selected:column.property === selected })));
      const menu = h('dialog', { class:'copal-dialog copal-sheet-toolbar-menu' }, h('h2', { text:`Configure ${action}` }));
      const run = (command, payload = {}) => { menu.close(); void controller.command(command, payload); };
      if (action === 'filter') {
        const operator = h('select', { 'aria-label':'Filter operator' }, ...['exists', 'equals', 'contains'].map((value) => h('option', { value, text:value }))); const value = h('input', { 'aria-label':'Filter value', placeholder:'Value (optional)' });
        menu.append(h('label', { text:'Column' }), picker, h('label', { text:'Operator' }), operator, h('label', { text:'Value' }), value, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => menu.close() }), h('button', { class:'copal-btn primary', text:'Apply', onclick:() => run('filter', { filter:{ property:picker.value, operator:operator.value, ...(operator.value === 'exists' ? {} : { value:value.value }) } }) })));
      } else if (action === 'sort') {
        menu.append(h('label', { text:'Column' }), picker, h('p', { class:'copal-dialog-hint', text:'Apply cycles ascending, descending, and unsorted.' }), h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => menu.close() }), h('button', { class:'copal-btn primary', text:'Apply', onclick:() => run('sort', { property:picker.value }) })));
      } else if (action === 'group') {
        menu.append(h('label', { text:'Group rows by' }), picker, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => menu.close() }), h('button', { class:'copal-btn primary', text:'Apply', onclick:() => run('group', { groupBy:picker.value }) })));
      } else if (action === 'columns') {
        menu.append(h('label', { text:'Column to show or hide' }), picker, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel', onclick:() => menu.close() }), h('button', { class:'copal-btn primary', text:'Apply', onclick:() => { const column = columns.find((item) => item.property === picker.value); run('columns', { property:picker.value, visible:column?.visible === false }); } })));
      } else return { outcome:'unavailable', message:'This typed sheet command is unavailable.' };
      wireDialog(menu); document.body.append(menu); menu.showModal(); return { outcome:'menu' };
    },
    overflow:(doc, controller, sheetState, leafId) => {
      const dialog = h('dialog', { class:'copal-dialog copal-sheet-overflow', 'data-sheet-leaf-id':leafId || '' }, h('h2', { text:`${doc.name} sheet` }));
      const actions = [
        ['Rename', () => renameBaseDocument(doc, () => notesFeature?.render?.())],
        ['Duplicate', () => duplicateBaseDocument(doc, () => notesFeature?.render?.())],
        ['History', () => showHistory(doc)],
        ['Open raw source', () => notesFeature?.open?.(doc.id, { mode:'source', leafId })],
        ['Move to trash', () => deleteDocument(doc)],
      ];
      dialog.append(h('div', { class:'copal-dialog-actions' }, ...actions.map(([label, run], index) => h('button', { class:`copal-btn${index === actions.length - 1 ? ' danger' : ''}`, text:label, onclick:() => { dialog.close(); void run(); } }))));
      wireDialog(dialog); document.body.append(dialog); dialog.showModal();
    },
  };
}

planningFeature = createPlanningFeature({
  h,
  api,
  getPlanning: planningData,
  refresh: () => loadDocuments(true),
  setStatus,
  projectionChanged,
  openDocument,
  openMarkdownTask:(item) => openMarkdownTask(item),
  loadMoreMarkdownTasks:() => loadTaskProjection(false),
  canLoadMoreMarkdownTasks:() => Boolean(state.taskCursor) && !state.taskLoading,
  createMarkdownTask:() => createMarkdownTask(),
  queryMarkdownTasks:(options) => loadTaskProjection(true, options),
  patchMarkdownTask:(item, checked) => toggleTask(item, checked),
});
function buildDocumentFeature(view) { return createNotesFeature({
  h,
  api,
  state,
  createMarkdownEditor,
  createSourceEditor,
  renderMarkdown,
  renderPreview,
  renderComment:(body, doc) => markdownRenderer.renderComment(body, doc),
  formatBaseCell,
  saveDocument,
  renameNote,
  deleteDocument,
  deleteDocuments,
  showHistory,
  showTrash,
  showForm,
  importVault,
  loadDocuments,
  openDocument,
  persistActiveContext,
  saveResource:saveResourceSnapshot,
  uploadAttachment,
  commitAttachment,
  abortAttachment,
  activateNotes:() => activateView(view),
  getContext:() => state.windows.get(view),
  presentationId:view, registerGlobalOpener:view === 'notes',
  resourceBufferRegistry:sharedDocumentRegistry, getSharedDocumentState:documentState,
  onSourceChanged:(id, value, changes) => featureFor(view === 'wiki' ? 'notes' : 'wiki')?.receiveDocumentSource(id, value, changes),
  onSaveStateChanged:(id, value) => featureFor(view === 'wiki' ? 'notes' : 'wiki')?.receiveSaveState(id, value),
  afterRender:view === 'wiki' ? shell => wikiWorkspace?.decorate(shell) : null,
  renderTimeline:(body) => planningFeature.renderTimeline(body),
  openEventEditor:(eventId) => planningFeature.openEventEditor(eventId),
  baseAdapter:createBaseSheetAdapter(),
  makeEditableWikiCopy,
  canCopyWikiArticle:canMakeEditableWikiCopy,
  createWikiArticle,
  importWikiMemes:importWikiMemesFromEditor,
  exportWikiMemes:exportWikiMemesFromEditor,
}); }
notesFeature = buildDocumentFeature('notes');
wikiFeature = buildDocumentFeature('wiki');
wikiWorkspace = createWikiWorkspace({ h, core:{
  capture:() => wikiFeature.captureEditorTarget(),
  isCurrent:target => wikiFeature.editorTargetCurrent(target),
  documents:() => state.docs,
  pinned:() => wikiFeature.getContext()?.noteWorkspace?.bookmarks || [],
  recent:() => wikiFeature.getContext()?.noteWorkspace?.recent || [],
  isOfficial:isOfficialDocument,
  open:id => openDocument(id, 'wiki'), mode:mode => wikiFeature.setMode(mode),
  pin:id => wikiFeature.pinDocument(id), save:() => wikiFeature.runCommand('save'),
  history:id => { const doc = state.docs.find(item => item.id === id); if (doc) showHistory(doc); },
  openEditor:id => openDocument(id, 'notes'), create:createWikiArticle, copy:makeEditableWikiCopy,
  canCopy:canMakeEditableWikiCopy,
  importMemes:importWikiMemesFromEditor, exportMemes:exportWikiMemesFromEditor,
  properties:() => wikiFeature.showDocumentPanel('properties'), links:() => wikiFeature.showDocumentPanel('links'),
  notify:message => wikiFeature.getContext()?.window.setStatus(message, true),
  insertMedia:target => wikiFeature.insertAttachment(target),
} });

function renderAchievements() {
  treeHouse.renderAchievementsPanel(state.body);
}

function renderTreeHouse() {
  treeHouse.render(state.body);
}

async function toggleTask(item, checked) {
  const openDoc = item?.doc && state.docs.find((value) => value.id === item.doc.id);
  const draft = openDoc ? notesFeature?.getDraftSnapshot?.(openDoc.id) : null;
  if (openDoc && draft && !['notes', 'wiki'].includes(state.view)) {
    if (!await notesFeature?.resolveDirtyDocuments?.([openDoc.id], { force:true, title:'Update task in document' })) return;
    item = { ...item, sourceRevision:{ kind:'copalHead', value:openDoc.head } };
  }
  if (openDoc && ['notes', 'wiki'].includes(state.view)) {
    const source = String(draft?.envelope?.text ?? openDoc.text ?? '');
    const expected = String(item.anchor?.expectedText || '');
    const first = expected ? source.indexOf(expected) : -1;
    const second = first >= 0 ? source.indexOf(expected, first + expected.length) : -1;
    if (!expected || first < 0 || second >= 0) { setStatus('Task source changed in the open draft; reload before updating it.', true); return; }
    const replacement = expected.replace(/(\[[ xX]\])/, checked ? '[x]' : '[ ]');
    const next = `${source.slice(0, first)}${replacement}${source.slice(first + expected.length)}`;
    notesFeature.queueSave(openDoc, next);
    if (!['notes', 'wiki'].includes(state.view)) setStatus('Task change is in the open Editor draft. Save the document to commit it.');
    return;
  }
  if (item.resourceKey && item.anchor && item.sourceRevision) {
    await api(`/tasks/${encodeURIComponent(item.id)}`, { method:'PATCH', body:JSON.stringify({
      actionId:`editor-task-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
      taskId:item.id, resourceKey:item.resourceKey,
      expectedRevision:{ kind:'copalHead', value:String(item.sourceRevision) }, anchor:item.anchor, checked:!!checked,
    })});
    await loadDocuments(true);
    return;
  }
  const doc = state.docs.find((value) => value.id === item.doc.id); if (!doc || doc.readOnly) return;
  const lines = String(doc.text || '').split('\n');
  let lineIndex = Number(item.task.line || 1) - 1;
  const blockIndex = (doc.blocks || []).findIndex((block) => block?.id === item.task.blockId);
  if (blockIndex >= 0) lineIndex = blockIndex;
  const current = lines[lineIndex] || '';
  if (!/^\s*[-*+]\s+\[[ xX]\]\s+/.test(current)) { setStatus('Task source changed; reload the task before updating it.', true); return; }
  lines[lineIndex] = current.replace(/(\[[ xX]\])/, checked ? '[x]' : '[ ]');
  await saveDocument(doc, lines.join('\n'), true);
}

async function createMarkdownTask() {
  const selectedId = state.windows.get('notes')?.selected || (state.view === 'notes' ? state.selected : null);
  const doc = selectedId ? state.docs.find((item) => String(item.id) === String(selectedId)) : null;
  if (doc && (doc.readOnly || !['note', 'markdown', 'text'].includes(doc.kind))) {
    setStatus('Select an editable note before creating a task.', true);
    return;
  }
  if (!doc) { setStatus('Open an editable note before creating a task.', true); return; }
  const title = await styledPrompt('Task title', { title: 'New task', defaultValue: 'New task', confirmText: 'Create task', maxLength: 240 });
  if (!title?.trim()) return;
  if (['notes', 'wiki'].includes(state.view)) {
    notesFeature?.applyDocumentTransaction?.(doc, source => `${source}${source && !source.endsWith('\n') ? '\n' : ''}- [ ] ${title.trim()}`, { origin:'task-create' });
    openDocument(doc.id, 'notes'); return;
  }
  if (doc.resource?.key) {
    void api('/tasks/create', { method:'POST', body:JSON.stringify({
      actionId:`editor-task-create-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`,
      resourceKey:doc.resource.key, expectedRevision:{ kind:'copalHead', value:String(doc.head || '') }, text:title.trim(),
    })}).then(async() => { await loadDocuments(true); openDocument(doc.id, 'notes'); }).catch((error) => setStatus(error.message, true));
    return;
  }
  const current = String(doc.text || '');
  notesFeature?.queueSave(doc, `${current}${current && !current.endsWith('\n') ? '\n' : ''}- [ ] ${title.trim()}`);
  setStatus('Task added to the Editor draft. Save to commit it.');
  openDocument(doc.id, 'notes');
}

function renderTodo() {
  planningFeature.renderTodo(state.body, taskItems(), {
    total:state.taskProjectionTotal,
    indexedTotal:state.taskIndexedTotal,
    matchedTotal:state.taskMatchedTotal,
    totalExact:state.taskTotalExact,
    query:state.taskQuery,
    selectedTaskId:state.taskSelectedId,
    onSelect:(id) => { state.taskSelectedId = id; },
  });
  if (!state.taskProjectionLoaded && !state.taskLoading) void loadTaskProjection(true);
}

async function loadTaskProjection(reset = false, queryOptions = null) {
  const scope = saveScope();
  if (state.taskLoading && !reset) return;
  if (reset) {
    state.taskQueryToken += 1;
    state.taskProjection = [];
    state.taskProjectionLoaded = false;
    state.taskCursor = null;
    state.taskSnapshotRevision = null;
    if (queryOptions) state.taskQuery = { ...state.taskQuery, ...queryOptions };
    state.taskProjectionTotal = null;
  }
  const token = state.taskQueryToken;
  state.taskLoading = true;
  try {
    const params = new URLSearchParams({ pageSize:'100' });
    if (state.taskQuery.query) params.set('query', state.taskQuery.query);
    if (state.taskQuery.completed != null) params.set('completed', String(state.taskQuery.completed));
    if (state.taskQuery.source && state.taskQuery.source !== 'all') params.set('source', state.taskQuery.source);
    if (state.taskCursor) params.set('cursor', state.taskCursor);
    const result = await api(`/tasks/query?${params}`, {}, scope.workspace);
    if (!sameSaveScope(scope, saveScope()) || token !== state.taskQueryToken) return;
    const seen = new Set(state.taskProjection.map((item) => item.id));
    state.taskProjection.push(...(result.items || []).filter((item) => !seen.has(item.id)));
    state.taskCursor = result.nextCursor || null;
    state.taskSnapshotRevision = result.snapshotRevision || result.queryRevision || null;
    state.taskProjectionTotal = result.matchedTotal ?? result.total ?? null;
    state.taskIndexedTotal = result.indexedTotal ?? null;
    state.taskMatchedTotal = result.matchedTotal ?? result.total ?? null;
    state.taskTotalExact = result.totalExact !== false;
    state.taskProjectionLoaded = true;
    if (state.view === 'todo' && state.body) renderTodo();
  } catch (error) {
    if (sameSaveScope(scope, saveScope()) && token === state.taskQueryToken) {
      state.taskProjectionLoaded = false;
      if (state.view === 'todo' && state.body) renderTodo();
    }
  } finally {
    if (token === state.taskQueryToken) state.taskLoading = false;
  }
}

async function openMarkdownTask(item) {
  const id = item?.doc?.id || item?.document?.id;
  if (!id) return;
  if (!state.docs.some((doc) => String(doc.id) === String(id))) {
    try { state.docs.push(await api(`/documents/${encodeURIComponent(id)}`)); } catch (error) { setStatus(error.message, true); return; }
  }
  openDocument(id, 'notes');
}

function showForm(title, fields, submit) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: title })); const controls = {};
  for (const [name, label, value, type = 'text'] of fields) { const control = h(type === 'textarea' ? 'textarea' : 'input', { name, value: type === 'textarea' ? null : value }); if (type === 'textarea') control.value = value; controls[name] = control; dialog.append(h('label', { text: label }), control); }
  const cancel = h('button', { type: 'button', class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() });
  const save = h('button', { type: 'button', class: 'copal-btn primary', text: 'Save', onclick: async () => { const values = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value.trim()])); save.disabled = true; try { await submit(values); dialog.close(); } catch (error) { setStatus(error.message, true); save.disabled = false; } } });
  dialog.append(h('div', { class: 'copal-dialog-actions' }, cancel, save)); wireDialog(dialog); document.body.append(dialog); dialog.showModal(); controls[fields[0][0]]?.focus();
}

function createDocument() {
  const defaultKind = state.view === 'wiki' ? 'wiki' : state.view === 'treehouse' ? 'lesson' : 'note';
  showForm('New Copal document', [['name', 'Name', state.view === 'treehouse' ? 'TreeHouse/New Lesson.md' : 'Untitled'], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
    const result = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: defaultKind, content }) });
    state.selected = result.doc.id; await loadDocuments();
  });
}

function importVault() {
  const input = h('input', { type: 'file', accept: '.zip,application/zip' });
  input.style.display = 'none'; document.body.append(input);
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; input.remove();
    if (!file || !await styledConfirm(`Import ${file.name} into Copal workspace “${state.workspace}”? Existing same-named documents are versioned, not silently replaced.`, { title: 'Import vault', confirmText: 'Import', danger: false })) return;
    const form = new FormData(); form.append('file', file);
    const controller = new AbortController();
    const progress = h('dialog', { class:'copal-dialog copal-import-progress' }, h('h2', { text:'Importing Obsidian vault' }), h('p', { text:file.name }), h('progress', { 'aria-label':'Import in progress' }), h('p', { class:'copal-dialog-hint', text:'Copal validates the archive before committing imported records.' }));
    progress.append(h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel import', onclick:() => controller.abort() })));
    wireDialog(progress, { dismissable:false }); document.body.append(progress); progress.showModal();
    setStatus('Importing vault…');
    try {
      const result = await api('/import/obsidian', { method: 'POST', body: form, signal:controller.signal });
      for (const projection of result.calendarProjections || []) projectionChanged({ calendar_projection: projection });
      await loadDocuments();
      setStatus(`Imported ${result.imported?.notes || 0} notes · ${result.imported?.assets || 0} assets`);
    } catch (error) { setStatus(error.name === 'AbortError' ? 'Import cancelled before completion' : error.message, error.name !== 'AbortError'); }
    finally { progress.close(); }
  }, { once: true });
  input.addEventListener('cancel', () => input.remove(), { once: true });
  input.click();
}

function renderView(view = state.view) {
  const context = activateView(view);
  if (!context?.window.body) return;
  // renderCalendar intentionally remains in this module as dormant,
  // recoverable boutique plumbing. Odysseus's native Calendar owns the active
  // route/menu and receives Copal events through the backend projector.
  const renderers = { notes: renderNotes, wiki: renderWiki, timeline: renderTimeline, galaxy: renderGalaxy, graph: renderGraph, mind: renderMind, bases: renderBases, treehouse: renderTreeHouse, achievements: renderAchievements, todo: renderTodo };
  const rendered = (renderers[view] || renderNotes)();
  persistActiveContext();
  return rendered;
}

function ensureViewWindow(view) {
  view = resolveView(view);
  if (state.windows.has(view)) return state.windows.get(view);
  const modalId = `copal-${view}-modal`;
  const windowApi = createCopalWindow({
    id:modalId,
    label:LABELS[view],
    icon: { notes:'code', wiki:'book', timeline:'timeline', graph:'graph', treehouse:'treehouse', achievements:'check', todo:'tasks' }[view] || 'document',
    subtitle:'Open Clank',
    minWidth:view === 'timeline' ? 720 : 560,
    minHeight:420,
    sizeKey:copalStorageKey(`odysseus-copal-${view}-window-size`),
    className:`copal-view-window copal-${view}-window`,
    onActivate:() => {
      if (!state.windows.has(view)) return;
      activateView(view); markActive(); localStorage.setItem(copalStorageKey('odysseus-copal-view'), view);
      if (resolveAppletLocation(location.pathname, location.search)?.target === 'editor') updateRoute(view, true);
    },
  onBeforeClose:() => ['notes', 'wiki'].includes(view) ? featureFor(view)?.beforeWindowClose?.() : true,
  onClosed:() => {
    if (['notes', 'wiki'].includes(view)) featureFor(view)?.destroy();
    if (view === 'graph') state.windows.get('graph')?.window.body?.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    markActive();
  },
  });
  const search = h('input', { class:'copal-search', type:'search', placeholder:`Search ${LABELS[view]}…`, 'aria-label':`Search ${LABELS[view]}` });
  const context = { view, window:windowApi, search, selected:null, filter:'', reading:false };
  if (['notes', 'wiki'].includes(view)) {
    try {
      const saved = JSON.parse(localStorage.getItem(copalStorageKey(`odysseus-copal-${view}-layout`, state.workspace)) || '{}');
      featureFor(view)?.loadSaved(context, saved);
      context.selected = saved.selected || null;
    } catch (_) {}
  }
  state.windows.set(view, context);
  if (view === 'wiki' && !context.selected) context.selected = state.docs.find(doc => doc.kind === 'wiki')?.id || null;
  search.addEventListener('input', () => { activateView(view); context.filter = search.value; state.filter = search.value; renderView(view); });
  const activate = (callback) => (...args) => { activateView(view); return callback(...args); };
  if (['notes', 'wiki'].includes(view)) {
    windowApi.actions.append(
      h('button', { class:'copal-btn', icon:'search', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:activate(() => featureFor(view)?.showChooser()) }),
      h('button', { class:'copal-btn', icon:'menu', title:'Editor commands', 'aria-label':'Editor commands', onclick:activate(() => featureFor(view)?.showCommands()) }),
    );
  } else if (view === 'achievements') {
    windowApi.actions.append(h('button', { class:'copal-btn', icon:'refresh', title:'Refresh Achievements', 'aria-label':'Refresh Achievements', onclick:activate(() => renderView('achievements')) }));
  } else {
    windowApi.actions.append(search);
    if (['wiki','treehouse'].includes(view)) windowApi.actions.append(h('button', { class:'copal-btn primary', icon:'add', text:'New', onclick:activate(createDocument) }));
    windowApi.actions.append(
      h('button', { class:'copal-btn copal-header-secondary', icon:'download', text:'Import', title:'Import Obsidian vault', onclick:activate(importVault) }),
      h('button', { class:'copal-btn copal-header-secondary', icon:'upload', text:'Export', title:'Export for Obsidian', onclick:() => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; } }),
      h('button', { class:'copal-btn', icon:'refresh', title:`Refresh ${LABELS[view]}`, 'aria-label':`Refresh ${LABELS[view]}`, onclick:activate(() => loadDocuments()) }),
    );
  }
  return context;
}

function buildWorkspace() {
  for (const view of VIEWS) ensureViewWindow(view);
}

// --- Per-entry Copal visibility (Appearance) ---
// Visibility is a per-user appearance preference (not authorization): hiding a
// launcher only removes its sidebar link. `open(view)` and open windows keep
// working, so direct links/commands/agent actions still reach hidden features
// and no data is touched. Driven by the same VIEWS/LABELS registry as navigation.
const ENTRY_VIS_KEY = 'copal_entry_visibility';
function defaultEntryVisibility() {
  const map = {};
  for (const view of COPAL_LAUNCHER_IDS) map[view] = view !== 'wiki';
  return map;
}
async function loadEntryVisibility() {
  const epoch = state.contextEpoch;
  const stored = await fetch(`/api/prefs/${ENTRY_VIS_KEY}`).then((r) => {
    if (!r.ok) throw new Error(`Appearance preference failed (${r.status})`);
    return r.json();
  }).then((res) => res?.value || {}).catch((error) => { if (epoch === state.contextEpoch) state.entryVisibilityError = error; return null; });
  if (epoch !== state.contextEpoch) return;
  if (stored === null) { state.entryVisibility = null; return; }
  state.entryVisibilityError = null;
  const merged = defaultEntryVisibility();
  for (const view of COPAL_LAUNCHER_IDS) if (typeof stored[view] === 'boolean') merged[view] = stored[view];
  state.entryVisibility = merged;
  window.dispatchEvent(new CustomEvent('copal-appearance-ready'));
}
async function saveEntryVisibility() {
  const response = await fetch(`/api/prefs/${ENTRY_VIS_KEY}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: state.entryVisibility }) });
  if (!response.ok) throw new Error(`Appearance preference failed (${response.status})`);
}
function applyEntryVisibility() {
  const map = state.entryVisibility || defaultEntryVisibility();
  document.querySelectorAll('[data-copal-view], [data-copal-launcher]').forEach((link) => {
    const view = link.dataset.copalView || link.dataset.copalLauncher;
    if (!COPAL_LAUNCHER_IDS.includes(view)) return;
    link.style.display = link.dataset.copalCompat === 'true' ? 'none' : map[view] === false ? 'none' : '';
  });
}
function openCopalAppearance() {
  if (!window.settingsModule?.open) throw new Error('Appearance settings are still loading');
  window.settingsModule.open('appearance');
}

export function getAppearanceEntries() { return COPAL_LAUNCHER_IDS.map((id) => ({ id, label: LAUNCHER_LABELS[id] || id })); }
export function getEntryVisibility() { return state.entryVisibility ? { ...state.entryVisibility } : null; }
export async function updateEntryVisibility(id, visible) {
  if (!COPAL_LAUNCHER_IDS.includes(id) || !state.entryVisibility) throw new Error('Copal Appearance is not ready');
  const previous = { ...state.entryVisibility };
  state.entryVisibility = { ...previous, [id]: visible === true };
  try { await saveEntryVisibility(); applyEntryVisibility(); return { ...state.entryVisibility }; }
  catch (error) { state.entryVisibility = previous; applyEntryVisibility(); throw error; }
}
async function replaceEntryVisibility(map) {
  if (!state.entryVisibility) throw new Error('Copal Appearance is not ready');
  const previous = { ...state.entryVisibility };
  state.entryVisibility = map;
  try { await saveEntryVisibility(); applyEntryVisibility(); return { ...state.entryVisibility }; }
  catch (error) { state.entryVisibility = previous; applyEntryVisibility(); throw error; }
}
export function setAllEntryVisibility(visible) {
  return replaceEntryVisibility(Object.fromEntries(COPAL_LAUNCHER_IDS.map((id) => [id, visible === true])));
}
export function resetEntryVisibility() { return replaceEntryVisibility(defaultEntryVisibility()); }
export function whenReady() {
  if (state.entryVisibilityError) return Promise.reject(state.entryVisibilityError);
  if (state.storageNamespace && state.entryVisibility !== null) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const onReady = () => {
      if (state.entryVisibilityError) reject(state.entryVisibilityError);
      else resolve();
    };
    window.addEventListener('copal-appearance-ready', onReady, { once: true });
  });
}

function bindSidebar() {
  state.navigationEvents?.abort();
  state.navigationEvents = new AbortController();
  const options = { signal:state.navigationEvents.signal };
  document.querySelectorAll('[data-copal-view]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); open(link.dataset.copalView); }, options));
  document.querySelectorAll('[data-copal-launcher="code"], [data-copal-launcher="files"]').forEach((link) => link.addEventListener('click', (event) => {
    if (link.tagName === 'A') return;
    event.preventDefault(); window.location.href = link.dataset.copalLauncher === 'code' ? '/code' : '/files';
  }, options));
  document.getElementById('copal-appearance-btn')?.addEventListener('click', (event) => { event.preventDefault(); openCopalAppearance(); }, options);
  document.getElementById('rail-copal')?.addEventListener('click', () => {
    const view = resolveView(localStorage.getItem(copalStorageKey('odysseus-copal-view')) || 'notes'); const context = ensureViewWindow(view);
    context.window.visible ? close(view) : open(view);
  }, options);
}

function connectEvents() {
  const scope = saveScope();
  state.events?.close();
  state.events = new EventSource(`${state.api}/api/copal/events?workspace=${encodeURIComponent(state.workspace)}`);
  // The mutating client refreshes its own document directly. Ignore the
  // matching SSE echo so it cannot overwrite save/conflict feedback. If a
  // different writer publishes during that short suppression window, queue
  // the refresh instead of dropping the notification; this keeps direct
  // Clanker/API writes visible in an already-open Timeline.
  const refresh = (event) => {
    if (!sameSaveScope(scope, saveScope())) return;
    const deferred = state.ignoreEventsUntil - Date.now();
    if (deferred > 0) {
      clearTimeout(state.reloadTimer);
      state.reloadTimer = setTimeout(() => refresh(event), deferred + 20);
      return;
    }
    clearTimeout(state.reloadTimer);
    state.reloadTimer = setTimeout(() => {
      if (!sameSaveScope(scope, saveScope())) return;
      if (![...state.windows.values()].some((context) => context.window.visible)) return;
      // D1: Scoped invalidation — check if changed doc is a Base source
      let docId = null;
      try { const data = JSON.parse(event?.data || '{}'); docId = data.id || data.documentId; } catch { /* full reload */ }
      // Editor leaves keep their own retained DOM and query controller. Load
      // the newest document envelope, then invalidate only mounted sheets
      // whose Base or visible source row is affected. This preserves each
      // leaf's view, selection, and scroll state while ensuring a watch echo
      // cannot leave a stale query cache behind.
      if (docId && typeof notesFeature?.invalidateBaseLeaves === 'function') {
        loadDocuments(false).then(async () => {
          if (!sameSaveScope(scope, saveScope())) return;
          const refreshed = await notesFeature.invalidateBaseLeaves(docId);
          if (!refreshed) loadDocuments();
        });
        return;
      }
      if (state.view === 'bases' && docId && state.baseSourceDocs.size > 0) {
        const affected = [];
        for (const [baseId, sources] of state.baseSourceDocs) {
          if (sources.has(docId) || baseId === docId) affected.push(baseId);
        }
        if (affected.length === 0) return; // not a source for any visible Base
        // Full document reload (needed for other views + selection context) then scoped requery
        loadDocuments().then(() => {
          for (const baseId of affected) {
            if (state.baseId === baseId) { renderBases(); break; }
          }
        });
        return;
      }
      loadDocuments();
    }, 120);
  };
  state.events.addEventListener('document', refresh); state.events.addEventListener('deleted', refresh);
}

function suspendCopalScope() {
  if (state.storageNamespace && state.windows.size) {
    persistActiveContext();
  }
  notesFeature?.suspendScope();
  wikiFeature?.suspendScope();
  planningFeature?.suspendScope?.();
  treeHouse.suspendScope();
  state.events?.close(); state.events = null;
  state.navigationEvents?.abort(); state.navigationEvents = null;
  state.filesMutationEvents?.abort(); state.filesMutationEvents = null;
  clearTimeout(state.reloadTimer); state.reloadTimer = null;
  for (const timer of state.saveTimers.values()) clearTimeout(timer);
  state.saveTimers.clear();
  for (const context of state.windows.values()) {
    context.window.body?.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    context.window.destroy();
  }
  state.windows.clear();
  for (const dialog of document.querySelectorAll('dialog.copal-dialog')) { dialog.close(); dialog.remove(); }
  for (const editor of state.noteEditors) editor.destroy();
  state.noteEditors.clear();
  Object.assign(state, {
    storageNamespace:null, accountId:null, workspace:'default', view:'notes', docs:[],
    planning:{ tracks:[], floatingTodos:[] }, taskProjection:[], taskProjectionLoaded:false, taskCursor:null, taskSnapshotRevision:null, taskProjectionTotal:null, taskIndexedTotal:null, taskMatchedTotal:null, taskTotalExact:false, taskLoading:false, taskQuery:{ query:'', completed:null, source:'all', sourceFilter:'all', statusFilter:'all', hideDone:false }, taskQueryToken:state.taskQueryToken + 1, taskSelectedId:null, selected:null, filter:'', reading:false,
    root:null, content:null, body:null, title:null, status:null, search:null,
    calendarMonth:null, projectedPlanningHead:null, ignoreEventsUntil:0,
    baseId:null, baseView:null, basePage:1, baseDefinition:null, baseSourceDocs:new Map(),
    baseFocusRow:-1, baseFocusCol:-1, baseFocusTable:null, baseQueryToken:state.baseQueryToken + 1,
    entryVisibility:null, entryVisibilityError:null,
    filesMutationBridgeInstallations:0,
  });
  graphView = null; graphScopeKey = null;
}

export async function init(apiBase = window.location.origin) {
  suspendCopalScope();
  const epoch = ++state.contextEpoch;
  // Context-menu object actions resolve against the same production command
  // owners as visible buttons and keyboard actions. The menu captures a
  // target before focus moves, then calls this owner directly.
  window.__openClankCopalContextCommand = handleContextObjectCommand;
  state.api = apiBase;
  let status = null;
  for (let attempt = 0; attempt < 3 && !status; attempt += 1) {
    try {
      status = await api('/status');
    } catch (error) {
      if (epoch !== state.contextEpoch) return;
      const retryable = !error.status || error.status >= 500;
      if (!retryable || attempt === 2) {
        console.error('[copal] refused to initialize without an authenticated storage scope', error);
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
    }
  }
  if (epoch !== state.contextEpoch) return;
  configureCopalStorage(status.storage_namespace);
  state.storageNamespace = status.storage_namespace;
  state.accountId = status.account_id || null;
  state.workspace = localStorage.getItem(copalStorageKey('odysseus-copal-workspace')) || 'default';
  // Standalone Copal surfaces (Files, deep links, future providers) can
  // follow the active logical workspace without reading Copal's private
  // storage namespace or treating it as host filesystem authority.
  window.__openClankCopalContext = () => ({
    workspace: state.workspace || 'default',
    storageNamespace: state.storageNamespace || null,
  });
  if (typeof Worker !== 'undefined') {
    const spellingScope = { accountId:state.accountId, workspace:state.workspace, storageNamespace:state.storageNamespace };
    const locale = navigator.language;
    const requestedLocale = String(locale || 'en-US').trim().toLowerCase() || 'en-us';
    const spellingLocale = requestedLocale === 'en' || requestedLocale === 'en-gb' ? requestedLocale : 'en-us';
    const expectedScopeKey = `openclank-copal-personal-words-v2:${encodeURIComponent(String(spellingScope.accountId || spellingScope.storageNamespace || 'anonymous'))}:${encodeURIComponent(String(spellingScope.workspace || 'default'))}:${encodeURIComponent(spellingLocale)}`;
    if (window.openClankSpelling && window.openClankSpelling.scopeKey?.() !== expectedScopeKey) window.openClankSpelling.destroy();
    if (!window.openClankSpelling) createSpellingService(undefined, { scope:spellingScope, locale });
  }
  planningFeature.loadState(state.workspace);
  treeHouse.loadState();
  buildWorkspace(); bindSidebar(); connectEvents();
  await loadEntryVisibility();
  if (epoch !== state.contextEpoch) return;
  applyEntryVisibility();
  const _resolveEditorRoute = () => {
    const resolved = resolveAppletLocation(location.pathname, location.search);
    if (!resolved || resolved.target !== 'editor') return null;
    const view = resolved.openBases ? 'bases'
      : resolved.mode === 'mind' ? 'mind'
      : resolved.mode === 'galaxy' ? 'galaxy'
      : (resolved.view || 'notes');
    return resolveView(view, resolved.mode);
  };
  window.addEventListener('popstate', () => {
    const view = _resolveEditorRoute();
    if (!view) return;
    const context = ensureViewWindow(view);
    context.selected = new URLSearchParams(location.search).get('doc'); open(view, false);
  }, { signal:state.navigationEvents.signal });
  {
    const view = _resolveEditorRoute();
    if (view) { ensureViewWindow(view).selected = new URLSearchParams(location.search).get('doc'); open(view, false); }
  }
}

export function getNotesSettings() {
  if (!state.storageNamespace) return {};
  ensureViewWindow('notes');
  return notesFeature?.getSettings() || {};
}

export function updateNotesSettings(patch = {}) {
  if (!state.storageNamespace) return {};
  ensureViewWindow('notes');
  return notesFeature?.updateSettings(patch) || {};
}

/** Open one Copal note through an owner-bound opaque Files ref. */
export async function openResource(resourceRef) {
  const scope = saveScope();
  const response = await filesFacadeClient.openResource(resourceRef);
  if (!sameSaveScope(scope, saveScope())) throw new Error('The active Copal account changed. Open the file again.');
  if (!response?.resource?.ref || !response?.payload) {
    throw new Error('This Files resource cannot be opened in Editor');
  }
  // Files returns an authorized, provider-neutral ResourceHandle. Validate it
  // before entering the shared workspace so a rotating ref can never become a
  // buffer identity and a filename suffix cannot choose presentation mode.
  const payload = response.payload;
  const resource = normalizeResourceHandle(payload.resource);
  const snapshotText = String(payload.text ?? payload.content ?? '');
  const snapshot = snapshotEnvelope({
    key:resource.key,
    expectedRevision:resource.revision,
    envelope:{ text:snapshotText, ...(resource.metadata ? { metadata:resource.metadata } : {}) },
  });
  // Enter the existing Editor surface without putting either a legacy document
  // id or the opaque ResourceRef into browser history.
  await open('notes', false);
  if (!sameSaveScope(scope, saveScope())) return null;
  history.pushState({ copal:'notes' }, '', appletPath('editor'));
  // Install the Files mutation bridge before opening the shared buffer. The
  // notes feature returns its active resource immediately, so a listener
  // placed after that return would never preserve a dirty tab's renamed path.
  installFilesMutationBridge();
  if (notesFeature?.openResource) {
    return notesFeature.openResource(resource, {
      ...payload,
      name:String(payload.name || response.resource.name || resource.locator.displayName || 'Untitled'),
      text:snapshot.envelope.text,
      snapshot,
      resourceRef,
  });
}
  const existing = state.docs.find((doc) => sameResourceKey(doc.resource?.key, resource.key));
  if (existing) { notesFeature.open(existing.id); return existing.id; }
  const id = `resource:${resource.key.provider}:${resource.key.resourceId}`;
  const injected = {
    id, name:String(payload.name || response.resource.name || 'Untitled'), kind:String(payload.kind || 'text'), corpus:String(payload.corpus || 'host'),
    text:snapshot.envelope.text, properties:payload.properties && typeof payload.properties === 'object' ? payload.properties : {}, relations:[],
    tags:Array.isArray(payload.tags) ? payload.tags : [], readOnly:resource.capabilities.edit !== true, resourceRef, resource,
    resourceKey:resource.key, resourceSnapshot:snapshot, savePolicy:'explicit', sourceKind:resource.key.provider,
  };
  const index = state.docs.findIndex((doc) => doc.id === id);
  if (index >= 0) state.docs[index] = injected; else state.docs.push(injected);
  notesFeature.open(id); return id;
}
export function customizeExplorer(view = 'notes') { const context = ensureViewWindow(view); activateView(view); context.window.show(document.activeElement); featureFor(view)?.render(); return featureFor(view)?.customizeExplorer(); }

export function getNotesPanels() { ensureViewWindow('notes'); return notesFeature?.getNotesPanels?.() || []; }
export function updateNotesPanel(id, patch = {}) { ensureViewWindow('notes'); return notesFeature?.updateNotesPanel?.(id, patch) || []; }

// Four-field, identifier-only pointer for the native read_copal/manage_copal
// contract. It never exposes drafts, owner identity, URLs, or user-authored
// content. The most recently activated visible Copal window is state.view.
export function getActiveAgentContext() {
  const current = state.windows.get(state.view);
  if (!current?.window?.visible) {
    return { accountId: state.accountId || null, workspace: state.workspace || 'default', view: null, resourceKind: null, resourceId: null };
  }
  const view = state.view;
  let resourceKind = null;
  let resourceId = null;
  if (view === 'notes' || view === 'wiki' || view === 'mind' || view === 'bases') {
    resourceKind = view === 'wiki' ? 'wiki' : view === 'mind' ? 'mind-source' : view === 'bases' ? 'base' : 'note';
    resourceId = state.selected || current.selected || null;
  } else if (view === 'timeline') {
    resourceKind = 'timeline';
    resourceId = state.selected || current.selected || null;
  } else if (view === 'treehouse') resourceKind = 'treehouse';
  else if (view === 'todo') resourceKind = 'todo-projection';
  else resourceKind = `${view}-projection`;
  return { accountId: state.accountId || null, workspace: state.workspace || 'default', view, resourceKind, resourceId: resourceId || null };
}

// Files' New file creates a canonical empty note, rather than pretending the
// read-only Copal navigation projection implements arbitrary staged imports.
export async function createFilesNote({ name, workspace, corpus = 'notes', actionId }) {
  if (!actionId || !['notes', 'wiki'].includes(corpus)) throw new Error('Invalid Copal creation request.');
  const options = { method: 'POST', body: JSON.stringify({ name, kind: corpus === 'wiki' ? 'wiki' : 'note', corpus, content: '', actionId }) };
  let result;
  try { result = await api('/documents', options, workspace); }
  catch (error) {
    // Canonical actionId replay reconciles a lost transport response without
    // creating a second note. Validation and provider errors are not retried.
    if (!(error instanceof TypeError)) throw error;
    result = await api('/documents', options, workspace);
  }
  return result;
}

export async function flushActiveAgentResource() {
  // Notes owns the only currently shared draft queue. Other views either save
  // through their existing explicit controls or are projections; the method is
  // intentionally awaited so Chat can add future editor flushers centrally.
  if (['notes', 'wiki'].includes(state.view)) {
    const current = state.windows.get(state.view);
    if (current?.noteDrafts?.size || [...(current?.noteBuffers?.values() || [])].some(buffer => buffer.state().dirty)) {
      featureFor(state.view)?.persistRecovery?.();
      throw new Error('The Editor has unsaved changes. Save explicitly before sharing its saved document with the agent.');
    }
  }
  return true;
}

window.__odysseusGetActiveCopalContext = getActiveAgentContext;
window.__odysseusFlushActiveCopalResource = flushActiveAgentResource;

export default { init, open, close, openResource, getNotesSettings, updateNotesSettings, customizeExplorer, getNotesPanels, updateNotesPanel, getFilesMutationBridgeInstallations, getActiveAgentContext, flushActiveAgentResource, whenReady, getAppearanceEntries, getEntryVisibility, updateEntryVisibility, setAllEntryVisibility, resetEntryVisibility };
