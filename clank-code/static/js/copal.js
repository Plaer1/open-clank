import { journalEvidence } from './copal/journalEvidence.js';
import { createSharedTextClient } from './copal/sharedTextClient.js';
import { sharedTextHandle, createSharedTextSaveAdapter } from './copal/sharedTextBuffer.js';
import { openSharedTextStream } from './sharedTextStream.js';
import { parseWorkbook } from './editor/workbookModel.js';
import { parseSlideDeck } from './editor/slideModel.js';
import { hasBrowserPreview } from './editor/previewModel.js';
import { normalizeGraphSettings, resolveGraphSettings, mountGraphSettings } from './copal/graphSettings.js';
import { recordPresentation, acknowledgeVisible, achievementOwner } from './achievementProducer.js';
import { mountWorkbenchMenu } from './workbenchMenu.js';
import { uiIcon } from './uiIcons.js';
import { openCalendar } from './calendar.js';
import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort, canonicalSourceProperty, setFrontmatterProperty, flattenFilterToLines, hasNestedFilterGroups, parseFilterLines, removeBaseView, reorderBaseView, makeViewFromTemplate, VIEW_TYPES, parseDataviewQuery, reorderBaseColumn } from './copal/bases.js';
import { createNotesFeature } from './copal/notesFeature.js';
import { installEditorFilesStyles } from './copal/editorFilesBrowser.js';
import { openScopedImageRef } from './copal/resourceDestinations.js';
import { createBufferRegistry } from './copal/documentBuffers.js';
import { createWikiWorkspace } from './copal/wikiWorkspace.js';
import { databaseRelations, moveHeadingSection, moveHeadingSectionTo, outlineEntries, reparentHeading } from './copal/notesModel.js';
import { createPlanningFeature, galaxyPlanningPopulation } from './copal/planning.js';
import { createCopalWindow } from './copal/windows.js';
import { wireDialog } from './copal/overlays.js';
import { configureCopalStorage, copalStorageKey } from './copal/storage.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { prepareDocumentSave, commitDocumentSave, sameSaveScope } from './copal/documentSave.js';
import { cloneEnvelope, normalizeResourceHandle, sameResourceKey, snapshotEnvelope } from './copal/resourceModel.js';
import { documentLinkBatches, documentGraph, galaxyGraph, graphStorageKey, normalizeGraphState, headingEntries, headingTree, renameHeading, changeHeadingLevel, deleteHeadingSection, reparentHeadingSection, moveHeadingSection as moveGraphHeadingSection, GRAPH_MODES, structureEntries, structureTree, deriveFacets, matchesFilters, filterDocuments, reconcileFilters, officialDocsRoot, isOfficialDocument, mergeFacets, documentGraphLabelMetrics } from './copal/graphModel.js';
import { createFacetCatalog } from './copal/graphFacetCatalog.js';
import { documentNeighborhood, relationPanel } from './copal/documentNeighbors.js';
import { mountGraphFilters } from './copal/graphFilterControls.js';
import { canHandleInput } from './copal/inputContext.js';
import { codeReceiptKey } from './copal/codeGraphModel.js';
import { mountCodeGraphWorkbench } from './copal/codeGraphWorkbench.js';
import { mountStaticFunctionOverlay } from './copal/staticFunctionOverlay.js';
import { mountTraceOverlay, traceOverlayOffered } from './copal/traceOverlay.js';
import { openCodeSummary } from './copal/codeSummaryPanel.js';
import { openWikiGenerate } from './copal/wikiGenerate.js';
import { openResourceHistory } from './historyView.js';
import { openWorkspaceBrowser, bindWorkspacePath, subscribeWorkspaceIndex } from './workspace.js';
import { extractReferenceSection, extractDocumentReferences } from './copal/markdownResources.js';
import { createMarkdownRenderer, registerAppDestination } from './copal/markdownRenderer.js';
import { navigationIntentFromEvent } from './copal/notesWorkspace.js';
import { createSpellingService } from './copal/spelling.js';
import { appletPath, updateAppletRoute, resolveAppletLocation } from './appletRoutes.js';
import { registerAdapter } from './custom-context-menu.js';
import { isReadOnlyColumn } from './copal/sheetModel.js';
import { styledConfirm, styledPrompt } from './ui.js';

const VIEWS = ['notes', 'wiki', 'timeline', 'treehouse', 'clankshop', 'achievements', 'todo'];
// Mind is not a separate destination.  Its label is kept only as the legacy
// deep-link alias text; Graph owns both views and Mind opens structure mode.
const LABELS = { notes: 'Editor', wiki: 'Wiki', timeline: 'Timeline', galaxy: 'Galaxy', graph: 'Graph', mind: 'Graph', bases: 'Bases', treehouse: 'TreeHouse', clankshop: 'Clankshop', achievements: 'Achievements', todo: 'Meatbag Tasks' };
// Code and Notes remain accepted route/DOM aliases, but only the canonical
// Editor destination participates in visible Copal launcher preferences.
const COPAL_LAUNCHER_IDS = ['journal', 'notes', 'files', 'timeline', 'treehouse', 'clankshop', 'achievements', 'todo', 'calendar'];
const LAUNCHER_LABELS = { ...LABELS, journal:'Journal', code: 'Editor', files: 'Files', calendar: 'Calendar' };
const MEMES_MIME = 'application/vnd.openclank.memes+json';
const HIDDEN_KINDS = new Set(['asset', 'planning', 'calendar-projection', 'treehouse-state', 'copal-tracks', 'copal-migration', 'image-session']);
const state = {
  api: '', workspace: 'default', storageNamespace: null, view: 'notes', docs: [], taskProjection: [], taskProjectionLoaded: false, taskCursor: null, taskSnapshotRevision: null, taskProjectionTotal: null, taskLoading: false, taskQueryToken: 0, taskQuery: { query:'', completed:null, source:'all', sourceFilter:'all', statusFilter:'all', hideDone:false }, taskSelectedId: null, planning: { tracks: [], floatingTodos: [] }, selected: null,
  filter: '', reading: false, saveTimers: new Map(),
  calendarMonth: null, windows: new Map(),
  root: null, content: null, body: null,
  treehouseSection: 'courses', treehouseLesson: null,
  title: null, status: null, search: null, events: null, reloadTimer: null, ignoreEventsUntil: 0, documentLoadToken: 0,
  documentWriteVersion:0, documentWriteVersions:new Map(), journalCatalog:null,
  projectedPlanningHead: null, projectingPlanning: null,
  baseId: null, baseView: null, basePage: 1, basePageSize: 100, baseQueryToken: 0, baseDefinition: null,
  baseSourceDocs: new Map(), baseFocusRow: -1, baseFocusCol: -1, baseFocusTable: null,
  noteEditors: new Set(),
  filesMutationEvents: null, filesMutationBridgeInstallations: 0,
  entryVisibility: null,
  entryVisibilityError: null,
  accountId: null, contextEpoch: 0,
  documentsLoaded: false, planningLoaded: false, documentLoadError: null, planningLoadError: null, sourceRefreshPending: false,
  navigationEvents: null,
};
let planningFeature = null;
let notesFeature = null;
let wikiFeature = null;
let wikiWorkspace = null;
let graphRenderTicket = 0;
let codeGraphController = null;
let codeGraphMenuGeneration=0;
let copalInitialization = null;
let pendingDocumentLoad = null;
let entryVisibilityLoad = null;
let expectedCopalAccount = null;
let codeGraphSelection = null; // Canonical code workspace, never Copal's document workspace.
let codeGraphContextEpoch = 0;
let graphFolderTicket = 0;
let editorNavigationTicket = 0;
function invalidateCodeGraphContext(event) {
  // Chat workspace display is independent from the explicitly bound Code folder.
  if(event?.type==='workspace-change')return;
  const account=event?.detail?.accountId;
  if(typeof account==='string'&&account&&account===state.accountId)return;
  codeGraphContextEpoch += 1; codeGraphController?.dispose();
  const visible = getViewContext('graph')?.window.body?.querySelector('[data-code-graph]');
  if (visible) { visible.replaceChildren(h('p',{role:'status',text:'Account authorization changed. Choose an authorized code folder after signing in.'})); getViewContext('graph')?.window.setStatus('Code context unavailable'); }
}
for (const eventName of ['openclank:auth-context-changed','workspace-change']) window.addEventListener(eventName, invalidateCodeGraphContext);
document.addEventListener('openclank:auth-context-changed', invalidateCodeGraphContext);
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

function getViewContext(view) {
  const editor = state.windows.get('notes');
  if (view === 'graph') return editor?.graphMode;
  if (view === 'wiki') return editor?.wikiMode;
  return state.windows.get(view);
}

function editorDocumentContexts() {
  const editor = state.windows.get('notes');
  return [editor, editor?.wikiMode].filter(Boolean);
}

function visibleViewEntries() {
  return [...state.windows].map(([view, context]) => {
    const active = view === 'notes' ? context.editorMode : view;
    return [active, getViewContext(active)];
  }).filter(([, context]) => context?.window.visible);
}

function persistActiveContext() {
  const context = getViewContext(state.view);
  if (!context) return;
  context.selected = state.selected;
  context.filter = state.filter;
  context.reading = state.reading;
}

function activateView(view, { navigation = true } = {}) {
  if (navigation) editorNavigationTicket += 1;
  // Foreground changes do not close another still-visible applet window.
  if (state.view !== view) persistActiveContext();
  const context = getViewContext(view);
  if (!context) return null;
  if (navigation && ['notes','wiki','graph'].includes(view)) showEditorMode(view);
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
  const mutating = options.method && options.method !== 'GET' && !options.readOnly;
  if (mutating) state.ignoreEventsUntil = Date.now() + 5000;
  const contentHeaders = options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' };
  const response = await fetch(`${state.api}/api/copal${path}${separator}workspace=${encodeURIComponent(workspace)}`, {
    ...options,
    headers: { ...contentHeaders, ...(state.accountId ? { 'X-Copal-Account':state.accountId } : {}), ...(options.headers || {}) },
  });
  if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active account changed.'), { code:'stale_scope' });
  if (response.ok) {
    const value = await response.json();
    if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active account changed.'), { code:'stale_scope' });
    if (mutating) {
      state.ignoreEventsUntil = Date.now() + 1000;
    }
    return value;
  }
  const error = await response.json().catch(() => ({}));
  if (epoch !== state.contextEpoch) throw Object.assign(new Error('The active account changed.'), { code:'stale_scope' });
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
  const context = getViewContext(state.view);
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

function visibleDocs(corpus, filter = state.filter) {
  const query = filter.trim().toLowerCase();
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
  const scope = saveScope();
  // Empty planning is a real revision too. Include identities and legacy
  // planning heads so deletion/replacement cannot collide with an old result.
  const sources = [
    state.planning?.trackRegistry,
    ...allPlanningTasks(),
    ...(state.planning?.floatingTodos || []),
    ...state.docs.filter((doc) => doc.kind === 'planning'),
  ].filter(Boolean).map((doc) => [doc.id, doc.head]).sort((a, b) => JSON.stringify(a).localeCompare(JSON.stringify(b)));
  const revision = JSON.stringify([scope, sources]);
  if (!force && revision === state.projectedPlanningHead) return;
  const pending = state.projectingPlanning;
  if (pending && sameSaveScope(pending.scope, scope)) {
    if (pending.revision === revision) return pending.promise;
    // Serialize changed revisions, then read the latest state rather than
    // replaying every intermediate SSE snapshot. Failures remain retryable.
    await pending.promise.catch(() => {});
    if (sameSaveScope(scope, saveScope())) return reconcileCalendarProjection(force);
    return;
  }
  const job = { scope, revision, promise:null };
  state.projectingPlanning = job;
  job.promise = (async () => {
    try {
      const result = await api('/calendar/reconcile', {
        method: 'POST', body: JSON.stringify({}),
      }, scope.workspace);
      if (!sameSaveScope(scope, saveScope())) return;
      const projections = result.projections || [];
      if (result.ok === false || projections.some((projection) => projection.ok === false)) return;
      state.projectedPlanningHead = revision;
      if (projections.some((projection) => projection.enabled)) window.dispatchEvent(new CustomEvent('calendar-refresh'));
    } finally {
      if (state.projectingPlanning === job) state.projectingPlanning = null;
    }
  })();
  return job.promise;
}

function loadDocuments(render = true, watchedDocuments = null) {
  const scope = saveScope();
  if (!scope.storageNamespace) return Promise.resolve(false);
  const loadToken = ++state.documentLoadToken;
  state.sourceRefreshPending = false;
  const current = () => loadToken === state.documentLoadToken && sameSaveScope(scope, saveScope());
  const pending = refreshDocumentSources({ scope, render, watchedDocuments, current }).catch((error) => {
    if (current()) setStatus(error.message, true);
    return false;
  }).finally(() => {
    if (pendingDocumentLoad === pending) pendingDocumentLoad = null;
  });
  pendingDocumentLoad = pending;
  return pending;
}

function updateDocumentViewStatus(view) {
  const mode = view === 'graph' ? getGraphView().mode : null;
  if (mode === 'code') return;
  const planning = view === 'timeline' || mode === 'galaxy';
  const failure = planning ? state.planningLoadError : state.documentLoadError;
  if (failure) {
    setViewStatus(view, `${planning ? 'Planning' : 'Documents'} unavailable · ${failure.message}`, true);
  } else if (planning && state.planning.migrationRequired) {
    setViewStatus(view, 'Saved Timeline notes need explicit workspace migration; source preserved.', true);
  } else if (!['achievements', 'treehouse'].includes(view)) {
    setViewStatus(view, `${state.docs.length} documents`);
  }
}

function isCopalAuthorityFailure(error) {
  return [401, 403].includes(error?.status) || error?.detail?.outcome === 'stale_session';
}

async function refreshDocumentSources({ scope, render, watchedDocuments, current }) {
  // A failed planning projection must leave the document index usable, and
  // a failed document read must leave a successful Galaxy projection usable.
  const writeVersion = state.documentWriteVersion;
  const [documents, planning, journal] = await Promise.allSettled([
    api('/documents?hidden=include', {}, scope.workspace),
    api('/planning', {}, scope.workspace),
    state.journalCatalog || filesFacadeClient.binders(),
  ]);
  if (!current()) return false;
  for (const source of [documents, planning]) {
    const error = source.reason;
    if (isCopalAuthorityFailure(error)) {
      retireCopalScope();
      window.uiModule?.showError?.('Workspace access changed. Open the applet again to reconnect.');
      return false;
    }
  }
  state.documentLoadError = documents.reason || null;
  state.planningLoadError = planning.reason || null;
  if (journal.status === 'fulfilled') acceptJournalCatalog(journal.value);
  if (documents.status === 'fulfilled') {
    const editorImages = notesFeature?.retainedImageDocuments() || [];
    const imageIds = new Set(editorImages.map(doc => doc.id));
    const newer = new Map(state.docs.filter(doc => state.documentWriteVersions.get(doc.id) > writeVersion).map(doc => [doc.id, doc]));
    const indexed = (documents.value.docs || []).filter(doc => !imageIds.has(doc.id)).map(doc => {
      const committed = newer.get(doc.id);
      newer.delete(doc.id);
      return committed || notesFeature?.projectDocument?.(doc, scope) || doc;
    });
    indexed.push(...newer.values());
    state.docs = [...indexed, ...editorImages];
    state.documentsLoaded = true;
    if (state.selected && !state.docs.some((doc) => doc.id === state.selected)) state.selected = null;
  }
  if (planning.status === 'fulfilled') {
    state.planning = planning.value;
    state.planningLoaded = true;
  }
  if (documents.status === 'fulfilled' || planning.status === 'fulfilled') {
    state.taskProjection = [];
    state.taskProjectionLoaded = false;
    state.taskLoading = false;
    state.taskCursor = null;
    state.taskSnapshotRevision = null;
    state.taskProjectionTotal = null;
    state.taskIndexedTotal = null;
    state.taskMatchedTotal = null;
    state.taskTotalExact = false;
    state.taskQueryToken += 1;
  }
  if (render) {
    const active = state.view;
    // Code Graph owns a different source. Keep its live mount and folder picker.
    const retainedCode = getGraphView().mode === 'code' && codeGraphController?.isCurrent?.()
      ? codeGraphController : null;
    for (const [view] of visibleViewEntries()) {
      if (view === 'graph' && retainedCode === codeGraphController && retainedCode?.isMounted()) continue;
      const usesPlanning = view === 'timeline' || (view === 'graph' && getGraphView().mode === 'galaxy');
      const source = usesPlanning ? planning : documents;
      const hadSnapshot = usesPlanning ? state.planningLoaded : state.documentsLoaded;
      if (source.status === 'fulfilled' || (!hadSnapshot && view === 'graph')) renderView(view);
      updateDocumentViewStatus(view);
    }
    activateView(active, { navigation:false });
  }
  if (watchedDocuments && documents.status === 'fulfilled') {
    for (const id of watchedDocuments) {
      if (!current()) break;
      void notesFeature?.invalidateBaseLeaves?.(id);
    }
  }
  if (documents.status === 'fulfilled' && planning.status === 'fulfilled') reconcileCalendarProjection().catch(() => {});
  return documents.status === 'fulfilled';
}

function updateRoute(view, replace = false) {
  const selected = getViewContext(view)?.selected || (state.view === view ? state.selected : null);
  // Canonical direct applet address — never the legacy /copal prefix. The
  // live rewrite keeps unconsumed one-shot query tokens (TreeHouse share) so
  // this normalization cannot eat a deep link before its consumer accepts it.
  const opts = { doc: selected || undefined };
  if (view === 'graph') opts.mode = getGraphView().mode;
  updateAppletRoute(view, opts, replace);
}

function markActive() {
  document.querySelectorAll('[data-copal-view]').forEach((link) => {
    const visible = getViewContext(link.dataset.copalView)?.window.visible;
    link.classList.toggle('active', !!visible);
    if (link.dataset.copalView === state.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

export function openResolvedRoute(resolved) {
  if (resolved?.target !== 'editor') return;
  if (['wiki','graph'].includes(resolved.view) && resolved.canonicalPath !== location.pathname + location.search) {
    history.replaceState(history.state, '', resolved.canonicalPath + location.hash);
  }
  const legacyGraph = resolved.view === 'graph' && ['mind', 'galaxy'].includes(resolved.mode);
  const view = resolved.openBases ? 'bases' : legacyGraph ? resolved.mode : resolved.view || 'notes';
  return open(view, false, { mode:resolved.mode, doc:resolved.doc });
}

async function open(view = 'notes', push = true, { mode = null, doc = null, focusRail = false } = {}) {
  const navigationTicket=++editorNavigationTicket;
  // A launcher can recover an earlier failed /status without using an
  // unscoped preference key. Concurrent opens share the same scope lookup.
  if (!state.storageNamespace) {
    const pending = init(state.api || window.location.origin);
    const epoch = state.contextEpoch;
    await pending;
    if (epoch !== state.contextEpoch || navigationTicket !== editorNavigationTicket) return;
    if (!state.storageNamespace) {
      if (push) window.uiModule?.showError?.('Could not open this applet. Open it again to retry.');
      return;
    }
  }
  if (view === 'calendar') {
    if (push || location.pathname.startsWith('/calendar')) {
      history.replaceState({}, '', '/calendar');
    }
    openCalendar();
    return;
  }
  const requestedView = String(view || 'notes').toLowerCase();
  const openingBases = requestedView === 'bases' || requestedView === 'base';
  view = resolveView(view, mode);
  const scope = saveScope();
  const context = ensureViewWindow(view);
  if (doc) context.selected = doc;
  activateView(view);
  localStorage.setItem(copalStorageKey('openclank-copal-view'), view);
  context.window.show(document.activeElement);
  const modeButton = focusRail ? state.windows.get('notes')?.activityRail?.buttons[view] : null;
  modeButton?.focus({ preventScroll:true });
  markActive();
  if (push) updateRoute(view);
  const graphMode = view === 'graph' ? getGraphView().mode : null;
  const usesPlanning = view === 'timeline' || graphMode === 'galaxy';
  const needsSource = usesPlanning ? !state.planningLoaded || state.planningLoadError
    : !state.documentsLoaded || state.documentLoadError;
  if (graphMode !== 'code' && (needsSource || state.sourceRefreshPending)) {
    context.window.setStatus('Loading…');
    let pending = pendingDocumentLoad || loadDocuments(false);
    await pending;
    while (sameSaveScope(scope, saveScope()) && pendingDocumentLoad && pendingDocumentLoad !== pending) {
      pending = pendingDocumentLoad;
      await pending;
    }
  }
  if (!sameSaveScope(scope, saveScope()) || !context.window.visible
      || (view === 'notes' && context.window.body.hidden)) return;
  const active = state.view;
  const retainedCode = view === 'graph' && getGraphView().mode === 'code'
    && codeGraphController?.isMounted?.() && codeGraphController.isCurrent();
  if (!retainedCode) renderView(view);
  if (state.documentsLoaded && state.planningLoaded && !state.documentLoadError && !state.planningLoadError) {
    reconcileCalendarProjection().catch(() => {});
  }
  updateDocumentViewStatus(view);
  activateView(active, { navigation:false });
  // `/copal/bases` remains a bookmark-compatible alias. Select the requested
  // Base before the shared Editor workspace normalizes its first leaf.
  if (openingBases && view === 'notes' && editorNavigationTicket === navigationTicket + 1) {
    const preferredId = state.baseId || (context.selected && state.docs.find((doc) => doc.id === context.selected && doc.kind === 'base')?.id);
    const base = state.docs.find((doc) => doc.kind === 'base' && doc.id === preferredId) || state.docs.find((doc) => doc.kind === 'base');
    if (base) { state.baseId = base.id; notesFeature?.open(base.id, { reuse:true }); context.selected = base.id; state.selected = base.id; renderView(view); }
  }
  if (!sameSaveScope(scope, saveScope())) return;
  if (editorNavigationTicket === navigationTicket + 1 && !modeButton) context.window.focus();
  const surfaces = { notes:['editor'], wiki:['editor'], graph:['editor','graph'], treehouse:['treehouse'] }[view] || [];
  for (const surface of surfaces) {
    acknowledgeVisible(context.window.body, 'surface.visited', { surface, visited:true }, { accountId:state.accountId, workspaceId:state.workspace });
  }
}

function close(view = state.view, push = true, fromManager = false) {
  const context = getViewContext(resolveView(view));
  if (!context?.window.visible) return;
  clearTimeout(state.saveTimer);
  context.window.requestClose(fromManager);
  markActive();
  if (push) history.pushState({}, '', '/');
}

const WIKI_LESSON_ID = 'openclank-docs-wiki-recipes';

function officialArticleId(doc) {
  return doc?.readOnly === true && isOfficialDocument(doc) && typeof doc.officialRef === 'string'
    ? doc.officialRef : '';
}

/** Help opens one canonical handbook in the chosen applet presentation. */
export async function openClankHandbook({ presentation = 'copal', articleId = 'openclank-docs-home', isCurrent = () => true } = {}) {
  const view = presentation === 'wiki' ? 'wiki' : 'notes';
  const scope = saveScope();
  const findArticle = () => state.docs.find(doc => officialArticleId(doc) === articleId)
    || state.docs.find(doc => doc.readOnly === true && doc.builtin === true
      && doc.properties?.docId === articleId);
  // Installed article bodies can advance while an applet stays open. Help
  // refreshes through the normal index path, which preserves personal drafts.
  await loadDocuments(false);
  if (!sameSaveScope(scope, saveScope()) || !isCurrent()) return null;
  const match = findArticle();
  if (!match || !notesFeature) return null;
  // Both Editor modes address the same installed article and canonical buffer.
  openDocument(match.id, view, true, { mode:'reading', manualEntry:true });
  return match.id;
}
window.openClankHandbook = openClankHandbook;

function openDocument(id, view = state.view, push = true, options = {}) {
  if (view === 'graph') view = 'notes';
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
  return false;
}

function saveScope() {
  return { workspace:state.workspace, storageNamespace:state.storageNamespace, accountId:state.accountId, epoch:state.contextEpoch };
}

async function refreshIndexedDocument(id, scope = saveScope()) {
  const writeVersion = state.documentWriteVersion;
  const fresh = await api(`/documents/${encodeURIComponent(id)}`, {}, scope.workspace);
  if (!sameSaveScope(scope, saveScope())) return fresh;
  if (state.documentWriteVersions.get(id) > writeVersion) return refreshIndexedDocument(id, scope);
  acceptIndexedDocument(fresh, scope);
  return fresh;
}

function acceptIndexedDocument(fresh, scope) {
  const projected = notesFeature?.projectDocument?.(fresh, scope) || fresh;
  const index = state.docs.findIndex((doc) => doc.id === fresh.id);
  if (index >= 0) state.docs[index] = projected;
  else state.docs.push(projected);
}

function setViewStatus(view, message, bad = false) {
  getViewContext(view)?.window.setStatus(message, bad);
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
    const prepared = prepareDocumentSave(doc, content, { snapshot:options.snapshot, scope, relationsFor:(text) => databaseRelations(text, state.docs, doc) });
    content = prepared.payload.content;
    receipt = await commitDocumentSave(prepared, (id, payload) => { writeStarted = true; return api(`/documents/${encodeURIComponent(id)}`, {
      method:'PUT', body:JSON.stringify(payload),
      headers:scope.accountId ? { 'X-Copal-Account':scope.accountId } : {},
    }, scope.workspace); });
    if (!current()) return options.returnReceipt ? { outcome:'failed', message:'The editing session changed' } : false;
    if (receipt.outcome !== 'applied') throw Object.assign(new Error('A newer version exists.'), { status:409, detail:{ doc:receipt.remote } });
    const result = receipt.result;
    doc.head = receipt.revision.value;
    state.documentWriteVersions.set(doc.id, ++state.documentWriteVersion);
    acceptIndexedDocument(receipt.doc, scope);
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
    const taskProjection = receipt.projections?.tasks;
    const latestDraft = options.viaBuffer && notesFeature?.getDraftSnapshot?.(doc.id);
    const newerEdits = latestDraft && latestDraft.localRevision > receipt.acknowledgedLocalRevision;
    setViewStatus(view, newerEdits ? 'Saved captured version · newer edits unsaved'
      : taskProjection && taskProjection.status !== 'ready' ? 'Saved · indexing pending' : 'Saved');
    const active = state.view;
    for (const [openView] of visibleViewEntries()) if (openView !== view) renderView(openView);
    if (rerender) renderView(view);
    activateView(active, {navigation:false});
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
      const relations = databaseRelations(source, state.docs, { name });
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
async function resolveHostCommentAsset(reference, origin, vaultAsset = null) {
  if (vaultAsset) {
    const key = vaultAsset.resource?.key;
    const scope = saveScope(), capturedHead = String(vaultAsset.head || '');
    const current = () => sameSaveScope(scope, saveScope()) && state.docs.some(doc => doc.id === vaultAsset.id
      && doc.resource?.key?.resourceId === key?.resourceId && doc.resource?.key?.accountId === key?.accountId
      && doc.resource?.key?.workspaceId === key?.workspaceId && String(doc.head || '') === capturedHead);
    if (vaultAsset.kind !== 'asset' || key?.provider !== 'copal' || !vaultAsset.id || !key.accountId || !key.workspaceId || !key.resourceId || !current()) return null;
    const resolved = await filesFacadeClient.resolveResource({provider:'copal', resource_id:vaultAsset.id,
      account_id:key.accountId, workspace_id:key.workspaceId});
    if (!current() || resolved.id !== key.resourceId || !resolved.ref) return null;
    const checked = await filesFacadeClient.stat(resolved.ref);
    if (!current() || checked.id !== key.resourceId || !hasBrowserPreview(checked)) return null;
    return {target:{kind:'asset', name:checked.name, mimeType:checked.mime_type,
      resourceRef:checked.ref, resourceID:checked.id, revision:checked.revision, accountScope:scope.accountId,
      workspaceScope:scope.workspace, browserPreview:true, isCurrent:current}};
  }
  if (origin?.resource?.key?.provider !== 'host') return null;
  let path;
  try {path=decodeURIComponent(String(reference.target || '').split('#')[0]);} catch {return null;}
  if (!path || path.startsWith('/') || path.includes('\\') || /^[a-z][a-z\d+.-]*:/i.test(path) || !/\.(?:png|jpe?g|gif|webp|svg|avif|bmp|ico|mp3|wav|ogg|m4a|flac|aac|mp4|webm|mov|m4v|pdf|html?|xhtml|clankbook)$/i.test(path)) return null;
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
    if (/\.clankbook$/i.test(name)) {
      if (!has('stat') || !has('open')) return null;
      const checked = await filesFacadeClient.stat(match.ref);
      const format=checked?.format, revision=checked?.revision, capturedResource=origin.resource, capturedHead=origin.head;
      if (!current() || checked?.provider!=='host' || checked.id!==match.id || !checked.ref || !revision
          || format?.variant!=='openclank-workbook-v1' || format?.resourceRef!==checked.ref
          || format?.providerRevision?.kind!==revision.kind || format?.providerRevision?.value!==revision.value
          || format?.policyGeneration!==checked.policy_generation || format?.operations?.workbookView?.available!==true
          || format?.operations?.workbookView?.requires!=='workbook-v1') return null;
      let fragment=''; try {fragment=decodeURIComponent(String(reference.target||'').split('#').slice(1).join('#'));} catch (_) {return null;}
      const sourceCurrent=()=>current() && origin.resource===capturedResource && origin.head===capturedHead;
      return {target:{kind:'asset',name:checked.name,workbookEmbed:true,resourceRef:checked.ref,resourceID:checked.id,
        revision,policyGeneration:checked.policy_generation,accountScope:scope.accountId,workspaceScope:scope.workspace,fragment,isCurrent:sourceCurrent,
        load:async (isCurrent,signal)=>{const {loadWorkbookSnapshot}=await import('./editor/workbookResourceLoader.js');
          return loadWorkbookSnapshot({client:filesFacadeClient,resourceRef:checked.ref,resourceId:checked.id,revision,
            policyGeneration:checked.policy_generation,accountId:scope.accountId,fragment,isCurrent,signal});},
        openEditor:(identity,isCurrent)=>openResource(identity.resourceRef,{expectedRevision:identity.revision,isCurrent:()=>sourceCurrent() && isCurrent()})}};
    }
    if (/\.(?:html?|xhtml)$/i.test(name)) {
      if (!has('stat')) return null;
      const checked = await filesFacadeClient.stat(match.ref);
      if (!current() || !hasBrowserPreview(checked) || !checked.id || checked.id !== match.id) return null;
      return {target:{kind:'asset',name:checked.name,mimeType:checked.mime_type,
        resourceRef:checked.ref || checked.resource_ref,resourceID:checked.id,revision:checked.revision,
        accountScope:scope.accountId,workspaceScope:scope.workspace,browserPreview:true,isCurrent:current}};
    }
    return {target:{kind:'asset',name:match.name,mimeType:match.mime_type,resourceRef:match.ref},url:filesFacadeClient.contentUrl(match.ref,{purpose:'preview'})};
  }
  return null;
}

function chooseRelocatedPassage(result, isCurrent) {
  if (result?.state === 'relocated' && result.candidates?.length === 1) return Promise.resolve(result.candidates[0]);
  if (result?.state !== 'ambiguous' || !Array.isArray(result.candidates)
      || result.candidates.length < 2 || result.candidates.length > 16 || !isCurrent()) return Promise.resolve(null);
  return new Promise(resolve => {
    const dialog = h('dialog', {class:'copal-dialog','aria-label':'Choose a moved citation'});
    const options = result.candidates.map(candidate => h('li',{},h('button',{type:'button',class:'copal-btn',
      text:`Open line ${candidate.line}`,onclick:() => {
        if (isCurrent()) selected = candidate;
        dialog.close();
      }})));
    let selected = null;
    dialog.append(h('h2',{text:'Choose a moved citation'}),
      h('p',{text:'More than one passage has the original citation text. Choose a line to inspect.'}),
      h('ul',{},...options),
      ...(result.omittedCandidates ? [h('p',{text:`${result.omittedCandidates} more matches omitted.`})] : []),
      h('button',{type:'button',class:'copal-btn',text:'Cancel',onclick:()=>dialog.close()}));
    dialog.addEventListener('close',()=>resolve(isCurrent() ? selected : null),{once:true});
    wireDialog(dialog); document.body.append(dialog); dialog.showModal();
  });
}

async function openCommittedCitation(citation, {isCurrent = () => true, intent = 'current'} = {}) {
  const scope = saveScope();
  const current = () => sameSaveScope(scope,saveScope());
  if (!isCurrent() || !scope.accountId || typeof citation?.documentId !== 'string' || typeof citation.revisionId !== 'string'
      || typeof citation.spelling !== 'string' || !Number.isSafeInteger(citation.line) || citation.line < 1
      || !Number.isSafeInteger(citation.startByte) || !Number.isSafeInteger(citation.endByte)
      || citation.startByte < 0 || citation.endByte <= citation.startByte) return null;
  const documentPath = `/documents/${encodeURIComponent(citation.documentId)}`;
  const doc = await api(documentPath, {}, scope.workspace).catch(() => null);
  const stillCurrent = () => isCurrent() && current();
  if (!stillCurrent() || doc?.id !== citation.documentId) return null;
  if (String(doc.head) !== citation.revisionId) {
    if (!/^[0-9a-f]{64}$/.test(citation.anchorProof || '')) return null;
    const result = await api(documentPath + '/citation-locate', {method:'POST',readOnly:true,
      body:JSON.stringify({spelling:citation.spelling,anchorProof:citation.anchorProof})},scope.workspace).catch(() => null);
    if (!stillCurrent()) return null;
    const selected = await chooseRelocatedPassage(result, stillCurrent);
    if (!selected || selected.documentId !== citation.documentId || selected.spelling !== citation.spelling
        || selected.anchorProof !== citation.anchorProof || selected.revisionId !== String(doc.head) || !stillCurrent()) {
      if (result?.state === 'unresolved' && stillCurrent()) setStatus('The citation could not be located in the current revision.',true);
      return null;
    }
    citation = selected;
  }
  const raw = new TextEncoder().encode(String(doc.text || ''));
  const decoded = new TextDecoder().decode(raw.slice(citation.startByte,citation.endByte));
  if (citation.endByte > raw.length || decoded !== citation.spelling) return null;
  let witnessedLine = 1;
  for (let offset = 0; offset < citation.startByte; offset++) if (raw[offset] === 10) witnessedLine++;
  if (witnessedLine !== citation.line) return null;
  if (!state.docs.some(item => item.id === doc.id && String(item.head) === citation.revisionId)) {
    await loadDocuments(false);
    if (!isCurrent() || !current() || !state.docs.some(item => item.id === doc.id && String(item.head) === citation.revisionId)) return null;
  }
  const view = doc.kind === 'wiki' ? 'wiki' : 'notes';
  const feature = featureFor(view);
  const exactText = () => {
    const snapshot = feature?.getDraftSnapshot?.(doc.id) || feature?.getAuthoritativeSnapshot?.(doc.id);
    return !snapshot || (snapshot.expectedRevision?.value === citation.revisionId && snapshot.envelope?.text === doc.text);
  };
  if (!exactText()) {
    setStatus('This passage is in a different revision than the open draft.',true);
    return null;
  }
  openDocument(doc.id, view, true, {intent});
  feature?.focusSourceLine(doc.id, citation.line, () => current() && exactText()
    && state.docs.some(item => item.id === doc.id && String(item.head) === citation.revisionId));
  const cache = [...(getViewContext(view)?.noteLeafViews?.values() || [])].find(item => item.docId === doc.id && item.root?.isConnected && item.root.getClientRects().length);
  return resolvedElement(cache?.root, doc.id);
}

async function openCommittedCodeMention({document:doc, revisionId, spelling, startLine, endLine, isCurrent}) {
  const scope = saveScope(), selection = codeGraphSelection;
  const current = () => sameSaveScope(scope,saveScope()) && codeGraphSelection === selection;
  try {
    if (!isCurrent() || !scope.accountId || selection?.owner !== scope.accountId || !selection.workspaceId) {
      if (isCurrent()) setStatus('Choose an authorized workspace in Code Graph first.',true);
      return;
    }
    const workspace = selection.workspaceId;
    let receipt = codeGraphController?.publishedContext?.()?.receipt;
    if (!receipt || receipt.workspace_id !== workspace) {
      const response = await fetch(`${state.api}/api/memory/code/graph`,{method:'POST',credentials:'same-origin',cache:'no-store',
        headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspace, query:{operation:'capabilities', fields:[], channels:[]}})});
      if (!response.ok) throw new Error('The Code Graph index is unavailable.');
      receipt = (await response.json())?.receipt;
    }
    if (!isCurrent() || !current() || !receipt || receipt.workspace_id !== workspace) return;
    const result = await api(`/documents/${encodeURIComponent(doc.id)}/code-mention`, {
      method:'POST',readOnly:true,
      body:JSON.stringify({codeWorkspaceId:workspace,codeReceipt:receipt,revisionId,spelling,startLine,endLine}),
    },scope.workspace);
    if (!isCurrent() || !current()) return;
    const destination = result?.destination;
    if (result?.state !== 'current' || result?.reference?.kind !== 'code' || codeReceiptKey(result.reference.receipt) !== codeReceiptKey(receipt)
        || destination?.provider !== 'host' || typeof destination.resource?.ref !== 'string') {
      setStatus(result?.state === 'ambiguous' ? 'This reference names more than one current code target.' : 'No single current code target was found.',true);
      return;
    }
    const expectedRevision = destination.expected_revision;
    const sourceLine = result.source?.span?.start_line;
    if (expectedRevision?.kind !== 'hostFingerprint' || !Number.isSafeInteger(sourceLine) || sourceLine < 1) return;
    await openResource(destination.resource.ref,{expectedRevision,sourceLine,isCurrent:()=>isCurrent()&&current(),handoffCurrent:current});
  } catch (error) {
    if (isCurrent() && current()) setStatus(error.message || 'This code reference is unavailable.',true);
  }
}

const markdownRenderer = createMarkdownRenderer({
  h,
  documents:() => state.docs,
  findByName,
  resolveAsset:resolveHostCommentAsset,
  assetUrl:target => target.resourceRef ? null : `${state.api}/api/copal/assets/${encodeURIComponent(target.id)}?workspace=${encodeURIComponent(state.workspace)}`,
  openTarget:(target, fragment, event, { navigation } = {}) => {
    if (navigation && !navigation.isCurrent()) return null;
    if (target?.kind === 'app-destination-error') {
      setStatus(target.name || 'Unknown app destination.', true);
      return;
    }
    const article = officialArticleId(target);
    const view = navigation?.presentation === 'notes' && article
      ? (article === WIKI_LESSON_ID ? 'wiki' : 'notes')
      : (target.kind === 'wiki' ? 'wiki' : 'notes');
    const intent = navigationIntentFromEvent(event);
    openDocument(target.id, view, true, { intent });
    if (fragment && ['notes', 'wiki'].includes(view)) {
      const section = extractReferenceSection(target.text, fragment);
      if (section.status === 'resolved') featureFor(view)?.focusSourceLine(target.id, section.line);
      else setStatus(`${section.status === 'ambiguous' ? 'Ambiguous' : 'Missing'} section: ${fragment}`, true);
    }
    const cache = [...(getViewContext(view)?.noteLeafViews?.values() || [])].find(item => item.docId === target.id && item.root?.isConnected && item.root.getClientRects().length);
    return { documentId:cache?.docId, element:cache?.root };
  },
  onConvertPluginBlock:convertPluginBlock,
  openCodeMention:openCommittedCodeMention,
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
  const cache = [...(getViewContext(view)?.noteLeafViews?.values() || [])].find(item => item.docId === doc.id && item.root?.isConnected && item.root.getClientRects().length);
  return resolvedElement(cache?.root, cache?.docId);
};
registerAppDestination('editor', resolveLinkedAppDocument);
registerAppDestination('wiki', resolveLinkedAppDocument);
registerAppDestination('citation', async ({panel,event}) => {
  const fields = String(panel || '').split('/');
  if (fields.length !== 6 && fields.length !== 7) return null;
  const [documentId,revisionId,line,startByte,endByte,spelling,anchorProof] = fields.map(decodeURIComponent);
  return openCommittedCitation({documentId,revisionId,line:Number(line),startByte:Number(startByte),
    endByte:Number(endByte),spelling,anchorProof},{intent:navigationIntentFromEvent(event)});
});
for (const view of ['graph','mind','galaxy']) {
  registerAppDestination(view, async () => {
    await open(view);
    const context = getViewContext('graph');
    return context?.window.visible ? resolvedElement(context.window.body) : null;
  });
}
for (const [screen, view] of [['achievements','achievements'], ['treehouse','treehouse'], ['timeline','timeline'], ['tasks','todo']]) {
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
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: kind === 'base' ? 'Deleted Bases' : 'Deleted documents' }));
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
  if (!unique.length || !await styledConfirm(`Move ${unique.length} selected document${unique.length === 1 ? '' : 's'} to trash?`, { title: 'Move documents to Trash', confirmText: 'Move to Trash', danger: true })) return false;
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
  const current = getViewContext('wiki');
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
    dialog.close(); await loadDocuments(false); renderView(state.view); setStatus(restore ? 'Wiki .memes restore complete' : 'Wiki .memes import complete');
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

const graphNeighborhoods = new WeakMap();
const galaxyCaches = new WeakMap();
function scopedGalaxyGraph(view) {
  const population = galaxyPlanningPopulation(planningData(), planningFeature.timelineState);
  if (!galaxyCaches.has(view)) galaxyCaches.set(view, {});
  return galaxyGraph(population.tracks, population.events, {
    registryHead:population.registryHead, cache:galaxyCaches.get(view),
    showUnassigned:view.modes.galaxy.filters.showUnassigned,
  });
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

function readCodeSettings(workspaceId){
  const key=graphStorageKey(state.accountId,'code:'+workspaceId);
  try {
    const saved=JSON.parse(localStorage.getItem(key)||'{}');
    if(workspaceId && !saved.settings)return readCodeSettings('');
    return normalizeGraphState(saved).settings;
  } catch {return normalizeGraphSettings();}
}
function writeCodeSettings(workspaceId,value){
  const key=graphStorageKey(state.accountId,'code:'+workspaceId);if(!key||!state.accountId)return;
  let saved={};try{saved=JSON.parse(localStorage.getItem(key)||'{}');}catch{}
  localStorage.setItem(key,JSON.stringify({...normalizeGraphState(saved),settings:normalizeGraphSettings(value)}));
}
function effectiveCodeSettings(workspaceId) {
  const saved = readCodeSettings(workspaceId);
  return resolveGraphSettings(saved, saved.presentation);
}

function graphSettingsPanel(context) {
  if (!context.settingsPanel) {
    context.settingsPanel = h('section',{class:'graph-sidebar-settings','aria-label':'Graph settings'});
    context.sidebarContent.before(context.settingsPanel);
  }
  return context.settingsPanel;
}

function mountFamilySettings(context, view) {
  const panel = graphSettingsPanel(context);
  const mode = view.mode;
  if (context.settingsView === view && context.settingsMode === mode) return;
  context.disposeFamilySettings?.();
  panel.replaceChildren();
  context.settingsView = view;
  context.settingsMode = mode;
  if (mode === 'code') return;
  const current = () => context.window.visible && view === getGraphView() && mode === view.mode;
  context.disposeFamilySettings = mountGraphSettings(panel,{
    family:mode, mode:'common', read:()=>view.settings, isCurrent:current,
    write:value=>{if(current()){view.settings=value;persistGraphView(view);}},
    onChange:()=>{if(current())renderView('graph');},
  });
}

function showFamilyGraphSettings() {
  graphSettingsPanel(getViewContext('graph')).querySelector('h3')?.focus();
}
window.addEventListener('openclank:graph-settings',async()=>{if(!state.storageNamespace)await init(state.api || window.location.origin);if(!state.storageNamespace)return;const owner=state.accountId,epoch=state.contextEpoch;await open('graph');if(owner!==state.accountId||epoch!==state.contextEpoch)return;if(getGraphView().mode==='code')codeGraphController?.openSettings?.();else showFamilyGraphSettings();});
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
  if (view === 'galaxy') return 'graph';
  return view === 'graph' || VIEWS.includes(view) ? view : 'notes';
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

function journalConnections(selected,direction,kind,current) {
  const scope=saveScope();
  const live=()=>sameSaveScope(scope,saveScope()) && current();
  const read=params=>api('/documents/'+encodeURIComponent(selected.id)+'/neighbors?'+new URLSearchParams({kind,...params}),{},scope.workspace);
  return relationPanel({h,direction,current:live,read,open:async(row,direction,structure,valid)=>{
    const receipt=await read({direction,witness:row.id});
    if (!valid()) return;
    const link=receipt.link, incoming=direction==='incoming';
    const endpoint=incoming?link.source:link.target;
    const doc=await refreshIndexedDocument(endpoint.id,scope);
    const matches=()=>sameSaveScope(scope,saveScope()) && doc.head===endpoint.head &&
      state.docs.some(item=>item.id===doc.id && item.head===endpoint.head) &&
      [notesFeature,wikiFeature].every(feature=>{
        const draft=feature?.getDraftSnapshot?.(doc.id);
        return !draft || draft.envelope?.text===doc.text;
      });
    if (!valid()) return;
    if (!matches()) throw new Error('The page changed. Reload connections.');
    let line=1;
    const range=incoming?link.range:null;
    const blockId=incoming?link.sourceBlockId:link.targetBlockId;
    if (range) {
      const hash=await attachmentTextHash(doc.text.slice(range.from,range.to));
      if (hash!==range.spellingHash) throw new Error('The source range changed. Reload connections.');
      line=range.line;
    } else if (blockId) {
      const blocks=doc.blocks || [], index=blocks.findIndex(item=>item.id===blockId);
      if (index<0) throw new Error('The linked block location is unavailable.');
      line=1+blocks.slice(0,index).reduce((lines,item)=>lines+String(item.source ?? item.text ?? '').split('\n').length,0);
    } else if (!incoming && link.fragment) {
      const section=extractReferenceSection(doc.text,link.fragment);
      if(section.status!=='resolved') throw new Error(section.status==='ambiguous'?'The section is ambiguous.':'The linked section is missing.');
      line=section.line;
    }
    if (!valid() || !matches()) return;
    if (structure) {
      const graph=getGraphView(), nav=mindModeState();
      graph.source={...graphSourceEnvelope(doc),connection:{workspace:scope.workspace,head:doc.head,fragment:link.fragment,range,line}};
      nav.docId=doc.id;
      nav.collapsed.clear();
      nav.selectedLine=structureEntries(doc.text).filter(entry=>entry.line<=line).at(-1)?.line || null;
      nav.editingLine=null;
      persistGraphView();
      await open('graph',true,{mode:'structure',doc:doc.id});
    } else {
      const view=doc.kind==='wiki'?'wiki':'notes';
      openDocument(doc.id,view,true,{mode:'source'});
      if(range) featureFor(view).focusSourceRange(doc.id,range,matches);
      else featureFor(view).focusSourceLine(doc.id,line,matches);
    }
  }});
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

function graphSvg(nodes, edges, onOpen, mode = getGraphView().mode, facets = null, catalog = null, isCurrent = () => true, documentProjection = null, onNeighborhood = null, onFullGraph = null, neighborhoodCoverage = null, journalInspector = {}) {
  const root = h('div', { class: 'copal-graph-wrap' });
  const view = getGraphView();
  const modeState = view.modes[mode];
  // Real facet state derived from server-scoped metadata/folders/values. The
  // saved filter set is reconciled against the loaded snapshot so renamed or
  // deleted values recover instead of silently hiding the graph.
  const reconciled = reconcileFilters(modeState.filters, facets);
  if (mode === 'galaxy') reconciled.showUnassigned = modeState.filters.showUnassigned === true;
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
  const graphSettings=resolveGraphSettings(view.settings,'common');
  const MIN_VIEW=100000/graphSettings.documentZoomMax, MAX_VIEW=100000/graphSettings.documentZoomMin;
  const clampWidth = width => Math.max(MIN_VIEW, Math.min(MAX_VIEW, width));
  const clampHeight = height => Math.max(MIN_VIEW*0.65, Math.min(MAX_VIEW*0.65, height));
  const vb = { ...modeState.camera };
  const width = clampWidth(vb.w), height = clampHeight(vb.h * width / vb.w);
  vb.x += (vb.w-width)/2;
  vb.y += (vb.h-height)/2;
  vb.w=width;
  vb.h=height;
  const initialCamera = { ...vb }, cameraViewId = crypto.randomUUID();
  const cameraScope = saveScope();
  const cameraOwner = achievementOwner();
  const cameraOptions = { accountId:cameraOwner, workspaceId:cameraScope.workspace };
  const cameraScopeCurrent = () => sameSaveScope(cameraScope, saveScope());
  let cameraStart = null;
  const cameraFacts = camera => ({ accepted:true, populatedView:nodes.length > 0, viewId:cameraViewId,
    scale:1000 / camera.w, panX:-camera.x * 1000 / camera.w, panY:-camera.y * 650 / camera.h });
  const MAX_MOUNTED=mode==='galaxy'?graphSettings.galaxyItems:graphSettings.documentItems;
  const MAX_EDGES=mode==='galaxy'?graphSettings.galaxyEdges:graphSettings.documentEdges;
  const nodesById = new Map(nodes.map((node) => [node.id, node]));
  modeState.camera = { ...vb };
  const persist = () => {
    modeState.filters = {
      search:graphState.searchQuery,
      kinds:[...graphState.activeKinds],
      folders:[...graphState.activeFolders],
      tags:[...graphState.activeTags],
      properties:Object.fromEntries(Object.entries(graphState.activeProperties).map(([key, values]) => [key, [...values]])),
      includeOfficial:graphState.includeOfficial,
      ...(mode === 'galaxy' ? {showUnassigned:modeState.filters.showUnassigned === true} : {}),
    };
    const selected = nodesById.get(graphState.selectedNodeId);
    if (selected) modeState.selection = { ...selected.selection, nodeId:selected.id };
    else if (!graphState.selectedNodeId) modeState.selection = null;
    persistGraphView(view);
  };
  const svgEl = svg('svg', { class: 'copal-graph', viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`, role: 'group', 'aria-label': `Graph showing ${nodes.length} nodes and ${edges.length} edges` });
  let cameraFrame = 0, cameraFrameCancel = null, destroyed = false;
  let cameraDirty = false, cameraWasVisible = false, cameraCheckpointTimer = null;
  let renderedWidth = vb.w, renderedHeight = vb.h;
  const checkpointCamera = () => {
    clearTimeout(cameraCheckpointTimer);
    cameraCheckpointTimer = null;
    if (!cameraDirty || !cameraScopeCurrent()) return;
    cameraDirty = false;
    persistGraphView(view);
    const report = cameraWasVisible && cameraOwner === achievementOwner();
    cameraWasVisible = false;
    if (!report) return;
    // Capture the final facts now, so the asynchronous start receipt can't
    // observe a later gesture or substitute the next workspace.
    const final = cameraFacts(vb);
    cameraStart ||= recordPresentation('graph.camera.gesture', { ...cameraFacts(initialCamera), start:true }, cameraOptions);
    void cameraStart.then(() => {
      if (cameraScopeCurrent() && cameraOwner === achievementOwner()) {
        return recordPresentation('graph.camera.gesture', final, cameraOptions);
      }
    }).catch(() => {});
  };
  const scheduleCameraCheckpoint = () => {
    if (!cameraDirty) return;
    clearTimeout(cameraCheckpointTimer);
    cameraCheckpointTimer = setTimeout(() => {
      cameraCheckpointTimer = null;
      if (!dragging && !pinch) checkpointCamera();
    }, 180);
  };
  const applyCamera = () => {
    if (destroyed || !isCurrent() || !cameraScopeCurrent()) return;
    const last = modeState.camera;
    if (last.x === vb.x && last.y === vb.y && last.w === vb.w && last.h === vb.h) return;
    Object.assign(last, vb);
    if (!cameraDirty) {
      cameraWasVisible = Boolean(cameraOwner && cameraOwner === cameraScope.accountId && nodes.length
        && document.visibilityState === 'visible' && root.getClientRects().length);
    }
    cameraDirty = true;
    if (!dragging && !pinch) scheduleCameraCheckpoint();
    if (cameraFrame) return;
    const useRaf = typeof globalThis.requestAnimationFrame === 'function';
    const frame = useRaf ? globalThis.requestAnimationFrame.bind(globalThis) : (callback) => setTimeout(callback, 0);
    cameraFrameCancel = useRaf ? globalThis.cancelAnimationFrame?.bind(globalThis) : clearTimeout;
    cameraFrame = frame(() => {
      cameraFrame = 0;
      cameraFrameCancel = null;
      if (destroyed || !isCurrent()) return;
      svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`);
      const sizeChanged = vb.w !== renderedWidth || vb.h !== renderedHeight;
      if (sizeChanged) {
        renderedWidth = vb.w;
        renderedHeight = vb.h;
        syncGraphLabels();
        updateZoomBounds();
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

  let edgeEls = [];
  let nodeEls = [];
  let arrivalFrame = 0;
  function syncGraphLabels() {
    if (destroyed || !root.isConnected) return;
    const metrics = documentGraphLabelMetrics(svgEl.getScreenCTM());
    if (!metrics) return;
    for (const { el, node } of nodeEls) {
      const label = el.querySelector('.copal-graph-label'), pos = positions.get(node.id);
      if (!label || !pos) continue;
      label.style.fontSize = `${metrics.fontSize}px`;
      label.setAttribute('x', pos.x + (node.hub ? 22 : 15) + metrics.gap);
      label.setAttribute('y', pos.y + metrics.baseline);
    }
  }

  // Inspector panel (B3)
  const inspector = h('div', { class: 'copal-graph-inspector' });

  function updateInspector(node) {
    const focusedConnection = journalInspector.controls?.contains(document.activeElement) ? document.activeElement : null;
    if (!node || node.doc?.id !== journalInspector.docId) journalInspector.controls = null;
    inspector.querySelector('.copal-graph-evidence')?.disposeEvidence();
    inspector.textContent = '';
    if (!node) return;
    const name = h('div', { class: 'copal-graph-inspector-name', text: node.label || '' });
    const kindLabel = h('span', { class: `copal-graph-inspector-kind kind-${node.kind || 'note'}`, text: (node.kind || 'note').charAt(0).toUpperCase() + (node.kind || 'note').slice(1) });
    const meta = h('div', { class: 'copal-graph-inspector-meta' });

    // Find connected nodes
    const connected = adjacentEdges.get(node.id) || [];
    const links = h('div', { class: 'copal-graph-inspector-links' });
    for (const edge of connected.slice(0,graphSettings.connections)) {
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
    if (connected.length > graphSettings.connections) inspector.append(h('p',{text:`Showing ${graphSettings.connections} of ${connected.length} connections.`}));
    if (mode === 'galaxy' && node.task?.documentId) {
      const source = state.docs.find(doc => doc.id === node.task.documentId);
      if (source) {
        const targets = new Set([source.id]);
        for (const relation of source.relations || []) {
          if (relation.targetDocumentId) targets.add(relation.targetDocumentId);
        }
        for (const id of targets) {
          const target = state.docs.find(doc => doc.id === id);
          if (target) inspector.append(h('button', {type:'button', text:id === source.id ? 'Open event note' : `Open linked note · ${target.name}`,
            onclick:() => { if (isCurrent() && state.docs.some(doc => doc.id === id)) openDocument(id, 'notes'); }}));
        }
      }
    }
    if (mode === 'documents' && node.doc && documentProjection) {
      const doc = node.doc, scope = saveScope();
      const current = () => isCurrent() && sameSaveScope(scope,saveScope());
      inspector.append(journalEvidence({h, current, load:async()=>{
        const selection = codeGraphSelection;
        let receipt = codeGraphController?.publishedContext?.()?.receipt;
        if (selection?.owner === scope.accountId && selection.workspaceId && receipt?.workspace_id !== selection.workspaceId) {
          const response = await fetch(`${state.api}/api/memory/code/graph`, {method:'POST',cache:'no-store',
            credentials:'same-origin',headers:{'Content-Type':'application/json'},
            body:JSON.stringify({workspace_id:selection.workspaceId,query:{operation:'capabilities',fields:[],channels:[]}})});
          receipt = response.ok ? (await response.json()).receipt : null;
        }
        if (!current() || codeGraphSelection !== selection) throw new Error('Graph selection changed');
        const code = selection?.owner === scope.accountId && receipt && receipt.workspace_id === selection.workspaceId
          ? {codeWorkspaceId:selection.workspaceId,codeReceipt:receipt} : null;
        const data = await api(`/documents/${encodeURIComponent(doc.id)}/graph-evidence`,{method:'POST',readOnly:true,
          body:JSON.stringify({head:doc.head,...(code ? {code} : {})})},scope.workspace);
        if (!current() || codeGraphSelection !== selection || data.page.id !== doc.id || data.page.revision !== doc.head)
          throw new Error('Graph selection changed');
        return data;
      }, open:async(row,valid)=>{
        if (row.kind === 'mentions') return openCommittedCodeMention({document:doc,revisionId:doc.head,
          spelling:row.label,startLine:row.line,endLine:row.line,isCurrent:valid});
        if (row.to.kind === 'fact') return window.memoryModule?.openReference?.(row.to.id);
        const view = doc.kind === 'wiki' ? 'wiki' : 'notes';
        openDocument(doc.id,view,true);
        featureFor(view)?.focusSourceLine(doc.id,row.line,()=>current() && state.docs.some(d=>d.id===doc.id && d.head===doc.head));
      }}));

      journalInspector.focusNeighborhood = onNeighborhood;
      if (journalInspector.docId !== doc.id || journalInspector.head !== doc.head || !journalInspector.controls) {
        journalInspector.docId = doc.id;
        journalInspector.head = doc.head;
        const direction = h('select', {'aria-label':'Connection direction'},
          ...[['both','Both'],['incoming','Incoming'],['outgoing','Outgoing']].map(([value,text])=>h('option',{value,text})));
        const kinds = h('select', {'aria-label':'Connection type'},
          ...[['both','Links and embeds'],['link','Links'],['embed','Embeds']].map(([value,text])=>h('option',{value,text})));
        const depth = h('select', {'aria-label':'Connection depth'},
          h('option',{value:'1',text:'One level'}), h('option',{value:'2',text:'Two levels'}));
        const focus = h('button',{type:'button',text:'Focus connections',onclick:()=>{
          if (isCurrent()) journalInspector.focusNeighborhood?.(node.id,{direction:direction.value,depth:Number(depth.value),kinds:kinds.value==='both'?['link','embed']:[kinds.value]});
        }});
        const panels=h('div');
        const updatePanels=()=>panels.replaceChildren(...(direction.value==='both'?['incoming','outgoing']:[direction.value])
          .map(value=>journalConnections(doc,value,kinds.value,current)));
        direction.addEventListener('change',updatePanels);
        kinds.addEventListener('change',updatePanels);
        updatePanels();
        journalInspector.controls = h('div',{},direction,kinds,depth,focus,panels);
      }
      inspector.append(h('p',{text:'The canvas shows loaded pages. Browse connections across this journal below.'}),journalInspector.controls);
      if (focusedConnection?.isConnected) focusedConnection.focus({preventScroll:true});
      if (neighborhoodCoverage) inspector.append(h('p',{text:`Neighborhood limit omitted ${neighborhoodCoverage.omittedNodes} nodes and ${neighborhoodCoverage.omittedEdges} edges; ${neighborhoodCoverage.filteredEdges} loaded graph edges excluded by type.`}));
      if (onFullGraph) inspector.append(h('button',{type:'button',text:'Return to full graph',onclick:()=>{if(isCurrent())onFullGraph();}}));
      const parsed = extractDocumentReferences(node.doc.text || '');
      if (parsed.truncated || parsed.unresolvedDefinitions) inspector.append(h('p',{text:`Link extraction is partial${parsed.truncated ? ' (source or link limit)' : ''}${parsed.unresolvedDefinitions ? `; ${parsed.unresolvedDefinitions} reference definitions unavailable` : ''}.`}));
      for (const edge of connected.slice(0,50)) {
        if (edge.from !== node.id || !edge.sourceLine) continue;
        const sourceLine = String(node.doc.text || '').split('\n')[edge.sourceLine - 1];
        if (sourceLine != null) inspector.append(h('p',{text:`Line ${edge.sourceLine}${edge.fragment ? ` #${edge.fragment}` : ''}: ${sourceLine.trim().slice(0,160)}`}));
      }
      const notices = (documentProjection.notices || []).filter(item=>item.sourceNodeId===node.id);
      for (const item of notices.slice(0,50)) {
        const sourceLine = item.sourceLine ? String(node.doc.text || '').split('\n')[item.sourceLine-1] : null;
        inspector.append(h('p',{text:`${item.kind}: ${item.reason}${item.locator ? ' · '+item.locator : ''}${item.fragment ? ' #'+item.fragment : ''}${sourceLine != null ? ` · line ${item.sourceLine}: ${sourceLine.trim().slice(0,160)}` : ''}`}));
      }
      if (notices.length>50) inspector.append(h('p',{text:`${notices.length-50} more notices not shown.`}));
      if (documentProjection.coverage?.omittedNotices || documentProjection.coverage?.omittedEdges) inspector.append(h('p',{text:`Loaded graph limits omitted ${documentProjection.coverage.omittedNotices || 0} notices and ${documentProjection.coverage.omittedEdges || 0} connection occurrences.`}));
    }
    inspector.append(openBtn);
  }

  function clearInspector() { journalInspector.controls = null; inspector.querySelector('.copal-graph-evidence')?.disposeEvidence(); inspector.textContent = ''; }

  // B4: select/deselect node
  let selectedEl = null;
  function selectNode(nodeEl, node) {
    if (selectedEl) { selectedEl.classList.remove('selected'); selectedEl.setAttribute('aria-pressed','false'); }
    selectedEl = nodeEl;
    nodeEl.classList.add('selected');
    nodeEl.setAttribute('aria-pressed','true');
    graphState.selectedNodeId = node.id;
    if (itemBrowser.open) renderItemPage(true);
    if (node.doc) view.source = graphSourceEnvelope(node.doc);
    persist();
    updateInspector(node);
    // highlight adjacent edges
    for (const ee of edgeEls) ee.el.classList.toggle('edge-highlight', ee.edge.from === node.id || ee.edge.to === node.id);
  }
  function deselectNode() {
    if (selectedEl) { selectedEl.classList.remove('selected'); selectedEl.setAttribute('aria-pressed','false'); }
    selectedEl = null;
    graphState.selectedNodeId = null;
    if (itemBrowser.open) renderItemPage(false);
    persist();
    clearInspector();
    for (const ee of edgeEls) ee.el.classList.remove('edge-highlight');
  }

  // Mount only a bounded window, rebuilding it when search/filter/selection
  // changes. This keeps a large corpus discoverable without creating 5,001
  // SVG subtrees, and always makes the selected match reachable.
  function mountGraph(visibleIds, onProgress) {
    if (arrivalFrame) cancelAnimationFrame(arrivalFrame);
    arrivalFrame=0;
    const selected = graphState.selectedNodeId;
    const mountedIds = [];
    for (const id of visibleIds) {
      if (mountedIds.length >= MAX_MOUNTED) break;
      mountedIds.push(id);
    }
    if (selected && visibleIds.has(selected) && !mountedIds.includes(selected)) {
      mountedIds[mountedIds.length-1] = selected;
    }
    const mounted = new Set(mountedIds);
    positions.clear();
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
    const edgeLayer = svg('g',{'aria-hidden':'true'});
    svgEl.append(edgeLayer);
    const drawnConnections=new Map(), edgesByEndpoint=new Map(), admitted=new Set();
    let admittedEdges=0;
    for (const edge of edges) {
      if (admittedEdges>=MAX_EDGES) break;
      if (!mounted.has(edge.from) || !mounted.has(edge.to)) continue;
      if ((drawnConnections.get(edge.from)||0)>=graphSettings.documentConnections ||
          (drawnConnections.get(edge.to)||0)>=graphSettings.documentConnections) continue;
      admittedEdges++;
      for (const id of new Set([edge.from,edge.to])) {
        drawnConnections.set(id,(drawnConnections.get(id)||0)+1);
        if (!edgesByEndpoint.has(id)) edgesByEndpoint.set(id,[]);
        edgesByEndpoint.get(id).push(edge);
      }
    }
    function appendNode(id) {
      const node = nodesById.get(id);
      const pos = positions.get(node.id);
      const group = svg('g', { class: `copal-graph-node${visibleIds.has(node.id) ? '' : ' dimmed'}`, 'data-graph-id':node.id, tabindex:'0', role:'button', 'aria-pressed':String(graphState.selectedNodeId===id), 'aria-label':`${node.label} (${node.kind || 'note'})` });
      const color = kindColor(node.kind || 'note');
      group.append(svg('circle', { cx: pos.x, cy: pos.y, r: node.hub ? 22 : 15, class: `kind-${node.kind || 'note'}${node.hub ? ' hub' : ''}`, fill: color, stroke: color }));
      const label = svg('text', { x: pos.x + (node.hub ? 26 : 20), y: pos.y + 4, class: 'copal-graph-label' });
      label.textContent = String(node.label || '').slice(0, 40);
      group.append(label);
      if (node.id === graphState.selectedNodeId) { group.classList.add('selected'); selectedEl = group; }
      nodeEls.push({ el: group, node });
      if (node.doc || node.task || node.track) {
        const command = { id:'open-graph-node', label:node.track ? 'Edit track' : node.task ? 'Open event' : 'Open in Editor', dialog:!!(node.track || node.task) };
        const nodeCurrent = request => group.isConnected && isCurrent() && request.adapterContext?.node === node;
        registerAdapter(group, {
          editingCommands:false,
          capture:() => ({ node }),
          isCurrent:nodeCurrent,
          commands:() => [command],
          execute:(id, request) => id === command.id && nodeCurrent(request) ? onOpen(node, group) : false,
        });
      }
      group.addEventListener('click', () => selectNode(group, node));
      group.addEventListener('dblclick', (event) => { event.preventDefault(); selectNode(group, node); onOpen(node); });
      group.addEventListener('keydown', (event) => {
        if (!canHandleInput(event)) return;
        if (event.key === ' ') { event.preventDefault(); selectNode(group,node); }
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
      admitted.add(id);
      for (const edge of edgesByEndpoint.get(id)||[]) {
        if (!admitted.has(edge.from) || !admitted.has(edge.to)) continue;
        const from=positions.get(edge.from), to=positions.get(edge.to);
        const line=svg('line',{x1:from.x,y1:from.y,x2:to.x,y2:to.y,class:`copal-graph-edge edge-${edge.type||'link'}`,'data-from':edge.from,'data-to':edge.to});
        line.classList.toggle('edge-highlight',edge.from===graphState.selectedNodeId || edge.to===graphState.selectedNodeId);
        edgeEls.push({el:line,edge});
        edgeLayer.append(line);
      }
    }
    // Construct large pages in bounded frames. The selected node is available
    // immediately, including when its natural slot is on the last batch.
    const inView=[], outside=[];
    for (const id of mountedIds) {
      if (id===selected) continue;
      const point=positions.get(id);
      const inside=point.x>=vb.x && point.x<=vb.x+vb.w && point.y>=vb.y && point.y<=vb.y+vb.h;
      (inside?inView:outside).push(id);
    }
    const order=mounted.has(selected)?[selected,...inView,...outside]:[...inView,...outside];
    let offset=0;
    function appendBatch() {
      arrivalFrame=0;
      const end=Math.min(order.length,offset+64);
      while (offset<end) appendNode(order[offset++]);
      if (offset<order.length) arrivalFrame=requestAnimationFrame(() => {
        if (!destroyed && isCurrent()) { appendBatch(); syncGraphLabels(); }
      });
      onProgress();
    }
    appendBatch();
    syncGraphLabels();
  }
  svgEl.addEventListener('click', (e) => { if (e.target === svgEl || e.target.tagName === 'line') deselectNode(); });

  // Zoom controls. Zoom keeps the pointer anchor fixed so wheel/pinch feels
  // natural, while the buttons remain secondary helpers.
  const zoomAroundCenter = (factor) => {
    const width = clampWidth(vb.w * factor);
    const height = clampHeight(vb.h * width / vb.w);
    vb.x += (vb.w - width) / 2; vb.y += (vb.h - height) / 2; vb.w = width; vb.h = height; applyCamera();
  };
  // Zoom around a screen point so the world coordinate under the pointer stays
  // put. Used by wheel and pinch gestures.
  const zoomAroundPoint = (factor, clientX, clientY) => {
    const transform = svgEl.getScreenCTM();
    if (!transform) { zoomAroundCenter(factor); return; }
    const inverse = transform.inverse();
    const anchor = new DOMPoint(clientX, clientY).matrixTransform(inverse);
    const width = clampWidth(vb.w * factor);
    const height = clampHeight(vb.h * width / vb.w);
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
    const width = clampWidth(maxX-minX);
    const height = clampHeight(maxY-minY);
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
  const controls = h('div', { class: 'copal-graph-controls' }, zoomIn, zoomOut, fit, reset,
    h('details', { class:'copal-graph-legend-disclosure' }, h('summary', { text:'Legend' }), legend));

  // Discovery and predicate edits are independent: opening/searching a picker
  // never filters the graph. Catalog refinement updates this same toolbar.
  const toolbar = h('div', { class:'copal-graph-toolbar' });
  let pendingAchievementFacet = null, previousResultCount = null;
  facets ||= { kinds:[...presentKinds].map(value => ({ value })), folders:[], tags:[], properties:{}, incomplete:true };
  const filterControls = mountGraphFilters({ element:toolbar, graphState, facets, mode, catalog,
    isCurrent:() => !destroyed && isCurrent(),
    onChange:detail => { pendingAchievementFacet = detail || null; rebuildGraph(); } });
  const info = h('div', { class: 'copal-graph-info', role: 'status', 'aria-live': 'polite' }, h('span', { text: `Graph showing ${nodes.length} nodes and ${edges.length} edges` }));

  // The canvas budget limits painting. The item picker can reach every
  // matching record using native keyboard navigation and bounded DOM pages.
  const pageSize = 50;
  let matchingIds = [], itemPage = 0;
  const itemPicker = h('select', {'aria-label':'Graph item', onchange:() => {
    if (!isCurrent() || !itemPicker.value) return;
    graphState.selectedNodeId = itemPicker.value;
    rebuildGraph();
    const selected = nodeEls.find(item => item.node.id === graphState.selectedNodeId);
    if (selected) {
      selectNode(selected.el,selected.node);
      const point=positions.get(selected.node.id);
      if (point.x<vb.x || point.x>vb.x+vb.w || point.y<vb.y || point.y>vb.y+vb.h) {
        vb.x=point.x-vb.w/2; vb.y=point.y-vb.h/2; applyCamera();
      }
    }
  }});
  const previousItems = h('button',{type:'button',class:'copal-btn',text:'Previous items',onclick:()=>{ itemPage--; renderItemPage(false); }});
  const nextItems = h('button',{type:'button',class:'copal-btn',text:'Next items',onclick:()=>{ itemPage++; renderItemPage(false); }});
  const itemRange = h('span',{'aria-live':'polite'});
  const itemBrowser = h('details', {class:'copal-graph-legend-disclosure'},
    h('summary',{text:'Browse items'}),itemPicker,previousItems,nextItems,itemRange);
  controls.append(itemBrowser);
  itemBrowser.addEventListener('toggle',()=>{ if (itemBrowser.open) renderItemPage(true); });
  function renderItemPage(followSelection) {
    const selectedIndex = followSelection ? matchingIds.indexOf(graphState.selectedNodeId) : -1;
    if (selectedIndex >= 0) itemPage = Math.floor(selectedIndex/pageSize);
    itemPage = Math.max(0,Math.min(itemPage,Math.ceil(matchingIds.length/pageSize)-1));
    const start = itemPage*pageSize, page = matchingIds.slice(start,start+pageSize);
    itemPicker.replaceChildren(h('option',{value:'',text:'Choose an item'}),
      ...page.map(id=>h('option',{value:id,text:nodesById.get(id).label || id})));
    if (page.includes(graphState.selectedNodeId)) itemPicker.value=graphState.selectedNodeId;
    previousItems.disabled = !itemPage;
    nextItems.disabled = start+page.length >= matchingIds.length;
    itemPicker.disabled = !page.length;
    itemRange.textContent = page.length ? `${start+1}–${start+page.length} of ${matchingIds.length}` : 'No matching items';
  }

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
      if (!doc && activeFilters.search.trim() && !String(node.label || '').toLowerCase().includes(activeFilters.search.trim().toLowerCase())) continue;
      visibleIds.add(node.id);
    }
    if (nodesById.has(graphState.selectedNodeId) && !visibleIds.has(graphState.selectedNodeId)) graphState.selectedNodeId = null;
    matchingIds = [...visibleIds];
    if (itemBrowser.open) renderItemPage(true);
    const activeGraphId = svgEl.contains(document.activeElement) ? document.activeElement?.getAttribute('data-graph-id') : null;
    const visibleEdges = edges.filter(edge => visibleIds.has(edge.from) && visibleIds.has(edge.to)).length;
    mountGraph(visibleIds,() => {
      info.textContent = visibleIds.size ? `Graph showing ${nodeEls.length} of ${visibleIds.size} matching items and ${edgeEls.length} of ${visibleEdges} matching edges. Browse or search all matches.` : 'No graph results. Reset filters or search again.';
      if (facets?.incomplete) info.textContent += ` · Facets partial${facets.status ? ` (${facets.status})` : ''}`;
    });
    if (activeGraphId !== null) {
      const target = nodeEls.find(item => item.node.id === activeGraphId) || nodeEls[0];
      (target?.el || toolbar.querySelector('input'))?.focus({ preventScroll:true });
    }
    if (pendingAchievementFacet && previousResultCount != null && previousResultCount !== visibleIds.size
        && facets?.generation && facets.generation !== 'local') {
      acknowledgeVisible(root, 'graph.filter.applied', { ...pendingAchievementFacet, facetFromScopedGeneration:true,
        resultCountBefore:previousResultCount, resultCountAfter:visibleIds.size }, { accountId:cameraOwner, workspaceId:state.workspace });
    }
    pendingAchievementFacet = null;
    previousResultCount = visibleIds.size;
    updateInspector(graphState.selectedNodeId ? nodesById.get(graphState.selectedNodeId) : null);
    persist();
  }

  root.append(toolbar, controls, svgEl, inspector, info);
  root._updateGraphFacets = next => {
    if (destroyed || !isCurrent()) return;
    // Keep picker references and text/caret intact while canonical knowledge
    // changes. Only complete canonical catalogs may prune unavailable values.
    const updated = reconcileFilters({ search:graphState.searchQuery, kinds:[...graphState.activeKinds],
      folders:[...graphState.activeFolders], tags:[...graphState.activeTags],
      properties:Object.fromEntries(Object.entries(graphState.activeProperties).map(([key, values]) => [key,[...values]])),
      includeOfficial:graphState.includeOfficial }, next);
    for (const [field, values] of [['activeKinds',updated.kinds],['activeFolders',updated.folders],['activeTags',updated.tags]]) {
      graphState[field].clear(); for (const value of values) graphState[field].add(value);
    }
    for (const [key, values] of Object.entries(graphState.activeProperties)) {
      values.clear(); for (const value of Object.hasOwn(updated.properties, key) ? updated.properties[key] : []) values.add(value);
    }
    facets = next; filterControls.updateFacets(next); rebuildGraph();
  };
  rebuildGraph();
  updateZoomBounds();
  // First mount runs before insertion. The microtask observes the connected
  // viewport without fabricating a camera gesture; resize stays event-driven.
  queueMicrotask(syncGraphLabels);
  const labelResizeObserver = typeof ResizeObserver === 'function' ? new ResizeObserver(syncGraphLabels) : null;
  labelResizeObserver?.observe(svgEl);
  globalThis.addEventListener?.('resize', syncGraphLabels);
  const restoredSelection = nodeEls.find((item) => item.node.id === graphState.selectedNodeId);
  if (restoredSelection) selectNode(restoredSelection.el, restoredSelection.node);

  // Pan via pointer drag
  let dragging = false, lastX = 0, lastY = 0;
  svgEl.addEventListener('pointerdown', (e) => { if (e.button === 0 && canHandleInput(e) && (e.target === svgEl || e.target.tagName === 'line')) { dragging = true; lastX = e.clientX; lastY = e.clientY; svgEl.setPointerCapture(e.pointerId); } });
  svgEl.addEventListener('pointermove', (e) => {
    if (!dragging) return;
    if (!canHandleInput(e)) { dragging = false; scheduleCameraCheckpoint(); return; }
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
    const cameraGestureEnded = dragging || (pinch && activeTouches.has(e.pointerId));
    dragging = false;
    activeTouches.delete(e.pointerId);
    if (activeTouches.size < 2) pinch = null;
    if (cameraGestureEnded) scheduleCameraCheckpoint();
  };
  for (const type of ['pointerup', 'pointercancel', 'lostpointercapture']) svgEl.addEventListener(type, endPointer);
  window.addEventListener('pagehide', checkpointCamera);
  root._copalDestroy = () => {
    if (destroyed) return;
    inspector.querySelector('.copal-graph-evidence')?.disposeEvidence();
    checkpointCamera();
    destroyed = true;
    root._copalDisposed = true;
    window.removeEventListener('pagehide', checkpointCamera);
    if (arrivalFrame) cancelAnimationFrame(arrivalFrame);
    labelResizeObserver?.disconnect();
    globalThis.removeEventListener?.('resize', syncGraphLabels);
    filterControls.dispose(); catalog?.dispose();
    if (cameraFrame) { cameraFrameCancel?.(cameraFrame); clearTimeout(cameraFrame); cameraFrame = 0; cameraFrameCancel = null; }
  };
  return root;
}

// Each mounted graph owns its discovery requests and cache. Account/workspace
// transitions, closing and mode changes invalidate both canonical and picker work.
function makeGraphFacetCatalog() {
  return createFacetCatalog({
    getScope:() => ({ account:state.accountId, workspace:state.workspace }),
    getDocuments:() => visibleDocs(),
    request:(parameters, scope) => api(`/graph/facets?${new URLSearchParams(parameters)}`, {}, scope.workspace),
  });
}

function renderGraph() {
  const context = getViewContext('graph');
  if (!context?.window.visible) return;
  const ticket = ++graphRenderTicket;
  codeGraphController?.dispose(); codeGraphController = null;
  const body = context.window.body;
  const scope = saveScope();
  const view = getGraphView();
  const modeId = view.mode;
  mountFamilySettings(context,view);
  context.modePicker.value = modeId;
  context.sidebarContent.replaceChildren();
  context.search.hidden = modeId === 'code' || (context.search.hidden && !context.filter);
  let mountedGraph = null;
  const isCurrent = () => ticket === graphRenderTicket && context?.window.body === body
    && body?.isConnected && context === getViewContext('graph')
    && !context.host.hidden && (context.window.visible || context.window.root.classList.contains('modal-minimized')) && (modeId==='code'?scope.accountId===state.accountId&&scope.epoch===state.contextEpoch:sameSaveScope(scope, saveScope()))
    && getGraphView().mode === modeId && (!mountedGraph || (mountedGraph.isConnected && !mountedGraph._copalDisposed));
  if (modeId === 'code') { renderCodeGraph({ body, isCurrent }); return; }
  if (state.sourceRefreshPending && !pendingDocumentLoad) void loadDocuments();
  const needsPlanning = modeId === 'galaxy';
  const loaded = needsPlanning ? state.planningLoaded : state.documentsLoaded;
  const loadError = needsPlanning ? state.planningLoadError : state.documentLoadError;
  if (!loaded) {
    body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    body.replaceChildren(h('div', { class:'copal-empty', role:'status' },
      h('p', { text:loadError ? (needsPlanning ? 'Planning is unavailable.' : 'Documents are unavailable.') : 'Loading Graph…' }),
      loadError ? h('button', { class:'copal-btn', text:'Retry', onclick:() => { if (isCurrent()) void loadDocuments(); } }) : null,
    ));
    if (!loadError && !pendingDocumentLoad) void loadDocuments();
    if (loadError) updateDocumentViewStatus('graph');
    else setViewStatus('graph', 'Loading…');
    return;
  }
  updateDocumentViewStatus('graph');
  // B2: respect dot-folder toggle from workspace settings
  let docs = visibleDocs(undefined, context.filter);
  try {
    const saved = JSON.parse(localStorage.getItem(copalStorageKey('openclank-copal-notes-layout', state.workspace)) || '{}');
    const showDot = saved?.left?.showDotFolders === true;
    if (!showDot) docs = docs.filter((doc) => {
      // Canonical .memes Wiki pages are first-party graph sources. The
      // preference hides ordinary dot-folder notes only.
      if (String(doc.kind || '').toLowerCase() === 'wiki' || (doc.name || '').split('/').includes('.memes')) return true;
      return !(doc.name || '').split('/').some((part) => part.startsWith('.'));
    });
  } catch (_) {}

  if (view.mode === 'structure') { renderMind(); return; }
  let projection = view.mode === 'galaxy' ? scopedGalaxyGraph(view) : documentGraph(docs, findByName);
  const facetCatalog = view.mode === 'documents' ? makeGraphFacetCatalog() : null;
  const focusedId = body.contains(document.activeElement) ? document.activeElement?.getAttribute('data-graph-id') : null;
  const openNode = (node, trigger) => {
    const scopeCurrent = () => sameSaveScope(scope, saveScope());
    if (!scopeCurrent()) return;
    if (node.task) return planningFeature.openEventEditor(node.task.id, trigger, scopeCurrent);
    if (node.track) return planningFeature.openTrackEditor(node.track.id, trigger, scopeCurrent);
    else if (node.doc) openDocument(node.doc.id, 'notes');
  };
  const scopeState = view.modes[modeId];
  let neighborhood = graphNeighborhoods.get(scopeState) || null, currentFacets = null;
  const changeNeighborhood = value => {
    neighborhood=value;
    if (value) graphNeighborhoods.set(scopeState,value);
    else graphNeighborhoods.delete(scopeState);
  };
  const journalInspector = {}; // One selection inside this render's account, workspace and projection generation.
  const mount = (facets) => {
    if (!isCurrent()) return;
    currentFacets = facets;
    const focusedConnection = journalInspector.controls?.contains(document.activeElement) ? document.activeElement : null;
    const activeId = body.contains(document.activeElement) ? document.activeElement?.getAttribute('data-graph-id') : null;
    const shown = view.mode === 'documents' && neighborhood
      ? documentNeighborhood(projection, neighborhood.id, neighborhood.options) : projection;
    const focus = (id, options) => { if (!isCurrent()) return; changeNeighborhood({id,options}); mount(facets); };
    const clear = neighborhood ? () => { if (!isCurrent()) return; changeNeighborhood(null); mount(facets); } : null;
    body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    mountedGraph = null;
    const graph = graphSvg(shown.nodes, shown.edges, openNode, view.mode, facets, facetCatalog, isCurrent,
      view.mode === 'documents' ? projection : null, focus, clear, neighborhood ? shown.coverage : null, journalInspector);
    context.sidebarContent.replaceChildren();
    if (neighborhood) context.sidebarContent.append(h('button',{class:'copal-btn',type:'button',text:'Return to full graph',onclick:()=>{if(isCurrent()){changeNeighborhood(null);mount(facets);}}}));
    body.replaceChildren(graph);
    mountedGraph = graph;
    if (view.mode === 'documents') acknowledgeVisible(graph, 'graph.mode.presented', { mode:'linked', nodeCount:projection.nodes.length }, { accountId:state.accountId, workspaceId:state.workspace });
    if (focusedConnection?.isConnected) focusedConnection.focus({preventScroll:true});
    else if (activeId || focusedId) graph.querySelector(`[data-graph-id="${CSS.escape(activeId || focusedId)}"]`)?.focus({ preventScroll:true });
  };
  // Mount immediately with locally derived facets, then refine with the lazy
  // server snapshot so the graph stays responsive while values stream in.
  // Both sources describe the same corpus. Server counts are authoritative;
  // incomplete catalogs retain local vocabulary with unknown counts.
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
    const toggle = h('input', {type:'checkbox'});
    toggle.checked = view.modes.galaxy.filters.showUnassigned;
    toggle.addEventListener('change', () => {
      if (!isCurrent()) return;
      view.modes.galaxy.filters.showUnassigned = toggle.checked;
      persistGraphView(view);
      renderGraph();
    });
    const {eligible, unassigned, unavailable, omitted} = projection.coverage;
    if (!state.planningLoadError && !state.planning.migrationRequired) setViewStatus('graph', `${eligible} events in Timeline scope`);
    const {rangeStart, rangeEnd} = planningFeature.timelineState;
    const range = rangeStart && rangeEnd ? `${rangeStart.toLocaleDateString()} – ${rangeEnd.toLocaleDateString()}` : 'All dates';
    context.sidebarContent.append(h('label', {}, toggle, ' Include events without a loaded track'),
      h('p', {text:`${range} · ${unassigned} unassigned · ${unavailable} with unavailable tracks · ${omitted} omitted`}));

    return;
  }
  const localFacets = { ...deriveFacets(docs, { generation:'local', officialRoot:officialDocsRoot(docs) }), incomplete:true, status:'loading' };
  mount(localFacets);
  if (view.mode === 'documents') {
    void (async () => {
      const verified = new Map();
      let generation;
      for (const sources of documentLinkBatches(docs)) {
        if (!isCurrent()) return;
        const result = await api('/documents/link-resolutions', {
          method:'POST', readOnly:true, body:JSON.stringify({sources}),
        }, scope.workspace);
        if (!isCurrent() || !result?.coverage?.complete || result?.sources?.length !== sources.length) return;
        if (generation !== undefined && generation !== result.coverage.generation) return;
        generation = result.coverage.generation;
        for (const [i, source] of sources.entries()) {
          const received = result.sources[i];
          if (received?.id !== source.id || received.head !== source.head ||
              !Array.isArray(received.targets) || received.targets.length !== source.targets.length) return;
          source.targets.forEach((target, index) => verified.set(JSON.stringify([source.id, source.head, target]), received.targets[index]));
        }
      }
      if (!isCurrent() || !verified.size) return;
      const current = new Map(visibleDocs().map(doc => [String(doc.id), String(doc.head)]));
      if (docs.some(doc => current.get(String(doc.id)) !== String(doc.head))) return;
      projection = documentGraph(docs, findByName, verified);
      mount(currentFacets || localFacets);
    })().catch(() => {}); // Local unresolved notices remain visible during an outage.
  }
  void (async () => {
    // An early picker can establish a newer generation while the canonical
    // walk is in flight. Retry only this still-live mount; never leave its
    // original "loading" label behind after a superseded response.
    for (let attempt = 0; attempt < 3 && isCurrent(); attempt += 1) {
      const snapshot = await facetCatalog.canonical({ isCurrent });
      if (!isCurrent()) return;
      if (snapshot) {
        currentFacets = mergeFacets(localFacets, snapshot, { sameCorpus:true });
        mountedGraph._updateGraphFacets(currentFacets);
        return;
      }
    }
    if (isCurrent()) mountedGraph._updateGraphFacets({ ...localFacets, status:'catalog changed; reopen Graph to refresh' });
  })().catch(() => {
    if (isCurrent()) mountedGraph._updateGraphFacets({ ...localFacets, status:'unavailable; loaded documents only' });
  });
}

export function captureCodeActivityPreference({accountID, workspaceID} = {}) {
  if (!accountID || accountID !== state.accountId || !workspaceID || codeGraphSelection?.owner !== accountID ||
      codeGraphSelection.workspaceId !== workspaceID || !codeGraphController?.isCurrent?.()) return false;
  const saved=readCodeSettings(workspaceID);
  return resolveGraphSettings(saved,saved.presentation).agentActivity === true;
}

export function observeCodeActivity(event, {accountID, chatID} = {}) {
  if (!accountID || accountID !== state.accountId || !chatID || event?.hostChatID !== chatID ||
      event?.accountID !== accountID || !codeGraphController?.isCurrent?.()) return false;
  return codeGraphController.observeActivity?.(event, chatID) === true;
}

export async function openCodeGraphBinding(binding,{isCurrent=()=>false,dirty=false}={}) {
  if (!state.storageNamespace) await init(state.api || window.location.origin);
  if(!state.accountId||!isCurrent()||!binding?.workspace?.id||binding.workspace.archived||typeof binding.open_relative!=='string')return false;
  const owner=state.accountId,epoch=state.contextEpoch;
  codeGraphContextEpoch++;
  codeGraphSelection={owner,workspaceId:binding.workspace.id,label:binding.workspace.name,
    openRelative:binding.open_relative,savedSourceNotice:dirty===true};
  getGraphView().mode='code';persistGraphView();
  await open('graph');
  return owner===state.accountId&&epoch===state.contextEpoch&&isCurrent();
}

export async function openStaticFunctionOverlay({workspaceID,dossierID,expectedDossierRevision,sourceRef,accountID,client,watchRoot},{isCurrent=()=>false}={}) {
  if (!state.storageNamespace) await init(state.api || window.location.origin);
  if(!state.storageNamespace||!isCurrent()||!watchRoot?.isConnected||!accountID||accountID!==state.accountId||typeof workspaceID!=='string'||!workspaceID||typeof dossierID!=='string'||!dossierID||!expectedDossierRevision||typeof sourceRef!=='string'||typeof client?.staticAnalysisOverlay!=='function')return false;
  const owner=state.accountId,epoch=state.contextEpoch;
  codeGraphContextEpoch++;
  codeGraphSelection={owner,workspaceId:workspaceID,label:'Static functions',overlay:{workspaceID,dossierID,expectedDossierRevision,sourceRef,client,watchRoot,isCurrent}};
  getGraphView().mode='code';persistGraphView();await open('graph');
  return owner===state.accountId&&epoch===state.contextEpoch&&isCurrent();
}

function chooseGraphWorkspace(isCurrent) {
  return new Promise(resolve=>{
    let cancelled=false, pending=null;
    const current=()=>!cancelled&&isCurrent();
    void openWorkspaceBrowser({
      selectionKind:'app_folder',
      title:'Open folder',
      useLabel:'Open folder',
      successLabel:'Graph folder opened',
      note:'Choose a folder to index and explore in Code Graph.',
      onCancel:()=>{cancelled=true;resolve(null);},
      onSelect:path=>{
        if(!current())throw new Error('The graph changed. Close this picker and open it from the active graph.');
        if(pending)return pending;
        pending=(async()=>{
          try {
            const binding=await bindWorkspacePath(path,'app_folder');
            if (!current() || binding.archived) throw new Error('The graph or folder changed. Reopen the picker.');
            resolve(binding);
          } finally {
            pending=null;
          }
        })();
        return pending;
      },
    }).catch(()=>resolve(null));
  });
}

const validCodeWorkspaceId = id => typeof id === 'string' && /^[^\x00-\x20\x7f]{1,256}$/.test(id);
function codeFolderPreference(owner) {
  return owner ? 'copal-code-folder:v1:' + encodeURIComponent(owner) : null;
}

function lastCodeFolder(owner) {
  const key = codeFolderPreference(owner);
  try {
    const id = key && localStorage.getItem(key);
    return validCodeWorkspaceId(id) ? id : '';
  } catch { return ''; }
}

function rememberCodeFolder(owner, id) {
  const key = codeFolderPreference(owner);
  if (!key || !validCodeWorkspaceId(id)) return;
  try { localStorage.setItem(key, id); } catch { /* Local preference is optional. */ }
}

function forgetCodeFolder(owner, id) {
  const key = codeFolderPreference(owner);
  if (!key) return;
  try { if (localStorage.getItem(key) === id) localStorage.removeItem(key); } catch { /* Local preference is optional. */ }
}

async function openGraphFolder(context) {
  if(!context || context!==getViewContext('graph'))return;
  const mode=getGraphView().mode;
  const ticket=++graphFolderTicket, owner=state.accountId, epoch=state.contextEpoch, codeEpoch=codeGraphContextEpoch;
  const current=()=>ticket===graphFolderTicket&&owner===state.accountId&&epoch===state.contextEpoch
    && codeEpoch===codeGraphContextEpoch&&context===getViewContext('graph')&&context.window.root.isConnected
    && context.window.visible!==false&&getGraphView().mode===mode;
  if(!current())return;
  const binding=await chooseGraphWorkspace(current);
  if(!binding?.id||!current())return;
  codeGraphContextEpoch++;
  codeGraphSelection={owner,workspaceId:binding.id,label:binding.name||binding.path||binding.id,chosenFolder:binding.id};
  rememberCodeFolder(owner, binding.id);
  getGraphView().mode='code';
  persistGraphView();
  renderView('graph');
  updateRoute('graph', true);
}

function renderCodeGraph({ body, isCurrent }) {
  codeGraphMenuGeneration++;const graphContext=getViewContext('graph');if(graphContext)graphContext.search.hidden=true;
  const owner = state.accountId, epoch = state.contextEpoch, codeEpoch = codeGraphContextEpoch;
  if (codeGraphSelection?.owner !== owner || codeGraphSelection.revoked) codeGraphSelection = { owner, workspaceId:'' };
  const selectedWorkspace = codeGraphSelection;
  const wrap = h('div', { class:'copal-graph-wrap', 'data-code-graph':true });
  const current = () => isCurrent() && epoch === state.contextEpoch && codeEpoch === codeGraphContextEpoch && owner === state.accountId
    && codeGraphSelection === selectedWorkspace && wrap.isConnected;
  const surface = h('div', {class:'copal-editor-graph-code-surface'});
  wrap.append(surface);
  body.querySelector('.copal-graph-wrap')?._copalDestroy?.(); body.replaceChildren(wrap);
  if(selectedWorkspace.overlay){
    const selected=selectedWorkspace.overlay;
    const overlay=mountStaticFunctionOverlay({container:surface,watchRoot:selected.watchRoot,
      isCurrent:()=>current()&&selected.isCurrent()&&selectedWorkspace.overlay===selected,
      load:({signal})=>selected.client.staticAnalysisOverlay({workspaceID:selected.workspaceID,dossierID:selected.dossierID,expectedDossierRevision:selected.expectedDossierRevision,sourceRef:selected.sourceRef},{signal}),
      onDispose:()=>{if(selectedWorkspace.overlay===selected)selectedWorkspace.overlay=null;}});
    const traceBox=h('div');wrap.append(traceBox);
    let trace=null;const traceCurrent=()=>current()&&selected.isCurrent()&&selectedWorkspace.overlay===selected;
    if(typeof selected.client.saveAnalysisIngest==='function'&&typeof selected.client.analysisSnapshotCapabilities==='function')
      selected.client.analysisSnapshotCapabilities().then(capability=>{if(!trace&&traceOverlayOffered(capability)&&traceCurrent())trace=mountTraceOverlay({container:traceBox,client:selected.client,workspaceID:selected.workspaceID,sourceRef:selected.sourceRef,isCurrent:traceCurrent});}).catch(()=>{});
    codeGraphController=overlay;wrap._copalDestroy=()=>{overlay.dispose();trace?.dispose();};return;
  }
  const preference = async (value, key = 'code_context_enabled') => {
    const response = await fetch('/api/prefs/' + key, value === undefined ? {credentials:'same-origin'} :
      {method:'PUT',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({value})});
    if (!response.ok) throw new Error('Preference unavailable');
    const result = await response.json();
    if (key === 'code_context_prompt_budget' ? !Number.isInteger(result.value) : typeof result.value !== 'boolean') throw new Error('Invalid preference');
    return result.value;
  };
  const investigationActions = new Map();
  let folderRestore = null;
  let choosingFolder = false;
  async function restoreCodeFolder() {
    if (!current() || selectedWorkspace.workspaceId || choosingFolder || folderRestore) return;
    const id = lastCodeFolder(owner);
    if (!id) return;
    const controller = folderRestore = new AbortController();
    const owns = () => current() && folderRestore === controller && !controller.signal.aborted &&
      !choosingFolder && !selectedWorkspace.workspaceId && lastCodeFolder(owner) === id;
    graphContext?.window.setStatus('Checking the last Code Graph folder…');
    try {
      const signal = AbortSignal.any([controller.signal,AbortSignal.timeout(12000)]);
      const issued = await filesFacadeClient.workspaceResource(id, '', {signal});
      if (!owns()) return;
      const registered = issued?.workspace;
      if (issued?.version !== 1 || registered?.id !== id || registered.archived !== false ||
          issued.resource?.provider !== 'host' || issued.resource.kind !== 'folder' ||
          !Number.isSafeInteger(issued.generation)) {
        if (registered?.id === id && registered.archived === true) forgetCodeFolder(owner,id);
        graphContext?.window.setStatus('Last Code Graph folder is unavailable. Choose a folder to continue.');
        return;
      }
      workbench.selectWorkspace(id,{label:registered.name || id});
    } catch (error) {
      if (!owns()) return;
      if ([403,404].includes(error?.status)) forgetCodeFolder(owner,id);
      graphContext?.window.setStatus('Last Code Graph folder is unavailable. Choose a folder to continue.');
    } finally {
      if (folderRestore === controller) folderRestore = null;
    }
  }
  const workbench = mountCodeGraphWorkbench({element:surface,isCurrent:current,onStatus:text=>{if(current())graphContext?.window.setStatus(text);},workspaceId:selectedWorkspace.workspaceId,accountID:owner,
    settingsHost:graphSettingsPanel(graphContext),
    workspaceLabel:selectedWorkspace.label || '',
    topologyOwner:selectedWorkspace.topologyOwner ||= {},
    readGraphSettings:()=>readCodeSettings(selectedWorkspace.workspaceId),
    readDocumentEvidence:async({workspaceId,receipt,resourceId,signal,isCurrent})=>{
      const scope=saveScope();
      if(!current() || !isCurrent() || workspaceId!==selectedWorkspace.workspaceId) return null;
      const result=await api('/graph/code-citations',{method:'POST',readOnly:true,signal,
        body:JSON.stringify({codeWorkspaceId:workspaceId,codeReceipt:receipt,resourceId})},scope.workspace);
      return current() && isCurrent() && sameSaveScope(scope,saveScope()) ? result : null;
    },
    openDocumentCitation:(citation,{isCurrent})=>openCommittedCitation(citation,{isCurrent:()=>current()&&isCurrent()}),
    captureTopology:async({workspaceId,paths,receipt,operation,isCurrent,persistOriginal})=>{
      if(!current()||!isCurrent()||workspaceId!==selectedWorkspace.workspaceId)return null;
      const {createResourcePicker}=await import('./copal/resourcePicker.js');if(!current()||!isCurrent())return null;
      const folder=await new Promise(resolve=>{const picker=createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:owner,
        originWindow:graphContext?.window,isOriginCurrent:()=>current()&&isCurrent(),getAccountScope:()=>state.accountId,
        rootLabel:'Choose a folder for the topology capture',
        isCompatible:row=>row.provider==='host' && (row.capabilities.create||row.capabilities.write) && row.revision?.kind==='hostDirectoryRevision' ? '' : 'Choose a writable Host folder with a current revision.',
        onSelect:row=>{resolve(row);return true;},onCancel:()=>resolve(null)});void picker.open();});
      if(!folder||!current()||!isCurrent())return null;
      const name=await styledPrompt('Choose a name for this recorded topology.',{title:'Capture indexed topology',defaultValue:'indexed-topology.json',confirmText:'Capture',maxLength:240});
      if(!name||!current()||!isCurrent())return null;
      const settings=effectiveCodeSettings(workspaceId);
      if(paths.length>settings.maxFiles)throw Error('The selected scope exceeds the configured file budget.');
      if(!current()||!isCurrent()||typeof persistOriginal!=='function'||persistOriginal()!==true)return null;
      const response=await fetch(`${state.api}/api/memory/code/topology/capture`,{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({version:1,operation_id:operation,workspace_id:workspaceId,paths,expected_receipt:receipt,parent_ref:folder.ref,name,parent_revision:folder.revision,
          limits:{nodes:settings.retainedNodes,edges:settings.retainedEdges,work:settings.queryWork}})});
      const result=await response.json();if(!current()||!response.ok||result?.version!==1||!['pending','prepared','saved','unknown','unavailable'].includes(result.state)||result.operation_id!==operation)throw Error('Capture unavailable');return result;
    },
    topologyStatus:async({operation,isCurrent})=>{
      const response=await fetch(`${state.api}/api/memory/code/topology/status`,{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,operation_id:operation})});
      const result=await response.json();if(!current()||!isCurrent()||!response.ok||result?.operation_id!==operation)throw Error('Status unavailable');return result;
    },
    compareTopology:({workspaceId,receipt,isCurrent,onResult,onError})=>{void (async()=>{
      try{const {createResourcePicker}=await import('./copal/resourcePicker.js');if(!current()||!isCurrent())return;
        const picker=createResourcePicker({client:filesFacadeClient,purpose:'snapshot-file',accountScope:owner,originWindow:graphContext?.window,
          isOriginCurrent:()=>current()&&isCurrent(),getAccountScope:()=>state.accountId,rootLabel:'Choose a recorded topology file',
          onSelect:row=>{if(!current()||!isCurrent()||!row.resourceId)return false;
            return openResourceHistory({resourceId:row.resourceId,name:row.name,provider:'host',isContextCurrent:()=>current()&&isCurrent(),
              onSelectVersion:async selection=>{if(!current()||!isCurrent())return;const settings=effectiveCodeSettings(workspaceId);
                const response=await fetch(`${state.api}/api/memory/code/topology/compare`,{method:'POST',cache:'no-store',credentials:'same-origin',headers:{'Content-Type':'application/json'},
                  body:JSON.stringify({version:1,workspace_id:workspaceId,resource_id:selection.resource_id,version_ref:selection.version_ref,expected_receipt:receipt,
                    limits:{nodes:settings.retainedNodes,edges:settings.retainedEdges,work:settings.queryWork,bytes:settings.cacheMiB*1048576}})});
                const result=await response.json();if(!current()||!isCurrent())return;if(!response.ok||result?.state!=='compared')throw Error('Comparison unavailable');onResult(result.comparison);}});}});
        void picker.open();
      }catch{if(current()&&isCurrent())onError();}})();},
    writeGraphSettings:value=>writeCodeSettings(selectedWorkspace.workspaceId,value),
    focusPath:selectedWorkspace.openRelative||'',savedSourceNotice:selectedWorkspace.savedSourceNotice===true,
    createWikiDocument:(content, isOriginCurrent)=>{
      const target=saveScope();
      if (!target.accountId || !current() || !isOriginCurrent() || typeof globalThis.crypto?.randomUUID !== 'function') throw new Error('A current authenticated wiki destination is required.');
      const actionId='graph-wiki-'+globalThis.crypto.randomUUID();
      let submitted=null;
      showForm(`Create new local wiki document in ${target.workspace}`, [['name','New document name','Graph snapshot'],['content','Review content before creating',content,'textarea']], async ({name,content:reviewed})=>{
        if (!current() || !isOriginCurrent() || !sameSaveScope(target,saveScope())) throw new Error('The graph or destination changed. Open a fresh preview.');
        if (!name) throw new Error('Choose a new document name.');
        const body=JSON.stringify({name,kind:'wiki',corpus:'wiki',content:reviewed,actionId});
        if (submitted !== null && submitted !== body) throw new Error('This creation was already attempted. Reopen the graph preview to change its content.');
        submitted=body;
        const result=await api('/documents',{method:'POST',body:submitted},target.workspace);
        if (!sameSaveScope(target,saveScope()) || !current() || !isOriginCurrent()) return;
        await loadDocuments();
        if (sameSaveScope(target,saveScope()) && current() && isOriginCurrent() && result?.doc?.id) openDocument(result.doc.id,'wiki');
      });
    },
    setWorkspace:(id,label)=>{if(current()){selectedWorkspace.workspaceId=id;selectedWorkspace.label=label;selectedWorkspace.revoked=false;selectedWorkspace.openRelative='';selectedWorkspace.savedSourceNotice=false;}},
    subscribeIndex:(id,options)=>{
      const replaceStaleRoot=selectedWorkspace.chosenFolder===id;
      if (replaceStaleRoot) selectedWorkspace.chosenFolder=null;
      return subscribeWorkspaceIndex(id,{...options,purpose:'app_folder',observe:true,replaceStaleRoot,onStatus:value=>{
      if (!current() || !options.isCurrent() || selectedWorkspace.workspaceId !== id) return;
      if (value?.lifecycle === 'denied') {
        selectedWorkspace.revoked = true;
        forgetCodeFolder(owner,id);
      }
      options.onStatus(value);
    }});},
    chooseWorkspace:async ownsChoice=>{
      choosingFolder = true;
      folderRestore?.abort();
      folderRestore = null;
      const owns = () => current() && (ownsChoice?.() ?? true);
      let binding = null;
      try {
        binding = await chooseGraphWorkspace(owns);
        if (binding?.id && owns()) {
          selectedWorkspace.chosenFolder=binding.id;
          rememberCodeFolder(owner,binding.id);
        }
        return binding;
      } finally {
        choosingFolder = false;
        if (!binding && owns()) void restoreCodeFolder();
      }
    },
    request:async(workspaceId,query,signal)=>{
      const response=await fetch(`${state.api}/api/memory/code/graph`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId,query}),signal});
      if(!response.ok)throw new Error('Code query unavailable');return response.json();
    },
    requestMap:async(workspaceId,visual_request,signal,reportTimings)=>{
      const response=await fetch(`${state.api}/api/memory/code/map`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId,visual_request}),signal});
      if(response.status===504)throw Object.assign(new Error('Code map read timed out'),{status:504});
      // Structured app errors carry bounded public codes, never an HTML body.
      if(!(response.headers.get('content-type')||'').includes('application/json'))throw new Error('Code map unavailable');
      const result=await response.json();
      if(!response.ok&&!result?.error)throw new Error('Code map unavailable');
      if(reportTimings&&response.ok&&!result?.error){
        const timing=response.headers.get('Server-Timing')||'';
        const durations=new Map(timing.split(',').map(entry=>entry.trim().match(/^(host-envelope|provider-wait);dur=([0-9]{1,9}(?:\.[0-9]{1,3})?)$/)).filter(Boolean).map(([,stage,duration])=>[stage,Number(duration)]));
        reportTimings({providerWaitMs:durations.get('provider-wait'),hostEnvelopeMs:durations.get('host-envelope')});
      }
      return result;
    },
    onRawOpen:async(destination,{isCurrent})=>{if(!isCurrent()||destination?.provider!=='host'||typeof destination.resource?.ref!=='string')return;await openResource(destination.resource.ref,{isCurrent});},
    onLibraryLookup:async({workspaceId,packageName,isCurrent,signal})=>{
      const response=await fetch(`${state.api}/api/memory/code/on-disk-library`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},signal,
        body:JSON.stringify({workspace_id:workspaceId,package:packageName})});
      if(!isCurrent()||!response.ok)return {ok:false};
      const value=await response.json();
      if(!isCurrent()||value?.schema!=='open-clank.library-observation/v1'||value.state!=='current'||value.partial!==true
          ||value.workspace_id!==workspaceId||value.package!==packageName||!Array.isArray(value.declaration?.items)||value.declaration.items.length>128
          ||typeof value.declaration?.rel_path!=='string'||!value.declaration.rel_path.startsWith('node_modules/'+packageName+'/'))return {ok:false};
      const lines=value.declaration.items.map(item=>`${item.kind}: ${item.signature || item.name}`);
      return {ok:true,text:[`${value.manifest.name}${value.manifest.version?' · '+value.manifest.version:''}`,
        `Manifest-declared TypeScript: ${value.declaration.rel_path}`,
        'Partial on-disk declaration shape; exports and runtime resolution are unproved.',...lines,
        ...(value.declaration.omitted?[`${value.declaration.omitted} declarations omitted.`]:[])].join('\n')};
    },
    onEvidenceAction:async(kind,selection)=>{
      if (kind === 'select_scip' || kind === 'clear_scip') {
        let resourceRef = null;
        if (kind === 'select_scip') {
          const {createResourcePicker} = await import('./copal/resourcePicker.js');
          if (!selection.isCurrent()) return {ok:false};
          const file = await new Promise(resolve => {
            const picker = createResourcePicker({client:filesFacadeClient,purpose:'snapshot-file',accountScope:owner,
              originWindow:graphContext?.window,isOriginCurrent:selection.isCurrent,getAccountScope:()=>state.accountId,
              rootLabel:'Choose a local SCIP index in this workspace',
              isCompatible:row=>row.provider==='host'&&row.kind==='file'&&row.capabilities.read&&row.name.endsWith('.scip')
                ? '' : 'Choose a readable .scip file with embedded source text.',
              onSelect:row=>{resolve(row);return true;},onCancel:()=>resolve(null)});
            void picker.open();
          });
          if (!file || !selection.isCurrent()) return {ok:false,message:'SCIP selection cancelled.'};
          resourceRef = file.ref;
        }
        const response = await fetch(`${state.api}/api/memory/code/scip`,{method:'POST',cache:'no-store',
          headers:{'Content-Type':'application/json'},signal:selection.signal,
          body:JSON.stringify({workspace_id:selection.workspaceId,expected_receipt:selection.receipt,resource_ref:resourceRef})});
        const value = response.ok ? await response.json() : null;
        if (!selection.isCurrent() || value?.schema !== 'open-clank.code-scip-selection/v1'
            || value.state !== (kind === 'select_scip' ? 'selected' : 'cleared')) {
          return {ok:false,message:'Choose a bounded binary SCIP index in this workspace with embedded text and position encoding.'};
        }
        return {ok:true,message:kind==='select_scip'?'SCIP index selected for this code receipt.':'SCIP index cleared.'};
      }
      if (kind === 'open_scip_definition') {
        const candidate = selection.scipCandidate;
        if (!selection.isCurrent() || !/^file:[a-f0-9]{64}$/.test(candidate?.id || '')) return {ok:false};
        const matches = (left,right)=>left?.id===right?.id
          && left?.source?.source_hash===right?.source?.source_hash
          && left?.source?.rel_path===right?.source?.rel_path
          && left?.source?.span?.start_byte===right?.source?.span?.start_byte
          && left?.source?.span?.end_byte===right?.source?.span?.end_byte;
        const headers = {'Content-Type':'application/json'};
        const fresh = await fetch(`${state.api}/api/memory/code/graph`,{method:'POST',cache:'no-store',headers,signal:selection.signal,
          body:JSON.stringify({workspace_id:selection.workspaceId,query:{operation:'inspect',seed_ids:[selection.node.id],
            expected_receipt:selection.receipt,channels:[],fields:['source','properties']}})});
        const evidence = fresh.ok ? await fresh.json() : null;
        const live = evidence?.hits?.[0]?.properties?.scipObservation;
        if (!selection.isCurrent() || codeReceiptKey(evidence?.receipt)!==codeReceiptKey(selection.receipt)
            || evidence?.hits?.[0]?.id!==selection.node.id || live?.profile!=='selected_scip_binary_v1'
            || live.index_sha256!==selection.node.properties?.scipObservation?.index_sha256
            || !live.candidates?.some(row=>matches(row,candidate))) return {ok:false,message:'The SCIP witness is no longer current.'};
        const response = await fetch(`${state.api}/api/memory/code/resource?exact_revision=true`,{method:'POST',cache:'no-store',
          headers,signal:selection.signal,body:JSON.stringify({workspace_id:selection.workspaceId,operation:'resolve',
            reference:{kind:'code',id:candidate.id,receipt:selection.receipt},excerpt_bytes:1})});
        const target = response.ok ? await response.json() : null;
        const revision = target?.destination?.expected_revision;
        if (!selection.isCurrent() || target?.current_source_verified!==true
            || target?.reference?.id!==candidate.id || target?.source?.source_hash!==candidate.source.source_hash
            || codeReceiptKey(target.receipt)!==codeReceiptKey(selection.receipt)
            || target.destination?.provider!=='host' || typeof target.destination.resource?.ref!=='string'
            || revision?.kind!=='hostFingerprint') return {ok:false,message:'The definition revision is unavailable.'};
        const opened = await openResource(target.destination.resource.ref,{isCurrent:selection.isCurrent,expectedRevision:revision,
          sourceRange:{startByte:candidate.source.span.start_byte,endByte:candidate.source.span.end_byte,
            sourceHash:candidate.source.source_hash},
          handoffCurrent:()=>owner===state.accountId&&epoch===state.contextEpoch&&codeEpoch===codeGraphContextEpoch
            && codeGraphSelection===selectedWorkspace&&selectedWorkspace.workspaceId===selection.workspaceId});
        return opened ? {ok:true,message:'Opened the SCIP definition witness.'} : {ok:false};
      }
      const compilerComparison = kind === 'compare_compiler_import';
      if(kind==='compare' || compilerComparison){
        const target=await new Promise((resolve,reject)=>openWorkspaceBrowser({selectionKind:'app_folder',title:'Choose target Code Graph workspace',
          note:compilerComparison
            ? 'Look for the explicitly imported Python function in a separately authorized workspace.'
            : 'Compare one literal dependency against this separately authorized workspace. This does not change the chat workspace.',
          onCancel:()=>resolve(null),onSelect:async path=>{try{resolve(await bindWorkspacePath(path,'app_folder'));}catch(error){reject(error);}}}));
        if(!selection.isCurrent() || !target?.id || target.id===selection.workspaceId)return {ok:false,message:'Comparison unavailable.'};
        const headers={'Content-Type':'application/json'};
        const probe=await fetch(`${state.api}/api/memory/code/graph`,{method:'POST',cache:'no-store',headers,
          body:JSON.stringify({workspace_id:target.id,query:{operation:'capabilities',fields:[],channels:[]}}),signal:selection.signal});
        if(!selection.isCurrent()||!probe.ok)return {ok:false,message:'Target index unavailable.'};
        const projected=await probe.json(); const targetReceipt=projected?.receipt;
        if(!selection.isCurrent()||!targetReceipt||targetReceipt.workspace_id!==target.id)return {ok:false,message:'Target index unavailable.'};
        const targetSelection = {workspace_id:target.id,receipt:targetReceipt};
        const sourceSelection = {workspace_id:selection.workspaceId,receipt:selection.receipt};
        const comparison = compilerComparison
          ? {version:2,kind:'python_compiler_import',source:{...sourceSelection,occurrence_id:selection.node.id},target:targetSelection}
          : {version:1,source:{...sourceSelection,file_id:selection.node.id},target:targetSelection};
        const response=await fetch(`${state.api}/api/memory/code/cross-project`,{method:'POST',cache:'no-store',headers,signal:selection.signal,
          body:JSON.stringify(comparison)});
        if(!selection.isCurrent()||!response.ok)return {ok:false,message:'Comparison unavailable or source changed.'};
        const result=await response.json();
        if (compilerComparison) {
          if (!selection.isCurrent() || result?.schema !== 'open-clank.code-cross-project/v2' || result.partial !== true) {
            return {ok:false,message:'Comparison unavailable or source changed.'};
          }
          if (result.state === 'no_match' && result.source === null && result.target === null && result.relation === null) {
            return {ok:true,message:'No unique imported function was witnessed in the selected workspace.'};
          }
          const source = result.source, endpoint = result.target;
          if (result.state !== 'match' || result.relation?.kind !== 'compiler_qualified_import'
              || source?.workspace_id !== selection.workspaceId || source?.id !== selection.node.id
              || endpoint?.workspace_id !== target.id || !/^decl:[0-9a-f]{64}$/.test(endpoint?.id || '')
              || codeReceiptKey(source.receipt) !== codeReceiptKey(selection.receipt)
              || codeReceiptKey(endpoint.receipt) !== codeReceiptKey(targetReceipt)
              || !/^[0-9a-f]{64}$/.test(source.source?.source_hash || '')
              || !/^[0-9a-f]{64}$/.test(endpoint.source?.source_hash || '')) {
            return {ok:false,message:'Comparison unavailable or source changed.'};
          }
          return {ok:true,message:'The selected workspace contains the compiler-qualified import candidate. Runtime loading remains unverified.',
            navigation:{workspace_id:endpoint.workspace_id,id:endpoint.id,receipt:structuredClone(endpoint.receipt),
              source_hash:endpoint.source.source_hash,original_source_hash:source.source.source_hash}};
        }
        if(!selection.isCurrent()||result?.schema!=='open-clank.code-cross-project/v1'||result.partial!==true||result.source?.workspace_id!==selection.workspaceId||result.target?.workspace_id!==target.id||codeReceiptKey(result.source.receipt)!==codeReceiptKey(selection.receipt)||codeReceiptKey(result.target.receipt)!==codeReceiptKey(targetReceipt))return {ok:false,message:'Comparison unavailable or source changed.'};
        if(result.state==='match'&&result.relation?.kind==='declared_local_file_dependency'&&result.relation?.semantics==='lexical_only'&&/^file:[0-9a-f]{64}$/.test(result.target?.file_id||''))return {ok:true,message:'Selected manifest declares a lexical local dependency on the target. Symlinks, installation and runtime resolution are unproved.',navigation:{...structuredClone(result.target),original_source_hash:result.source.source_hash}};
        if(result.state==='no_match'&&result.relation===null)return {ok:true,message:'No supported literal matches in this selected manifest. Other dependencies and runtime resolution remain unexamined.'};
        return {ok:false,message:'Comparison unavailable.'};
      }
      if(kind==='summary'){const composer=window.chatModule, target=composer?.captureCodeEvidenceDraft?.();openCodeSummary({selection,base:state.api,target,onCaptureDocument:()=>notesFeature?.captureSummaryDocumentTarget?.() || null,onInsertDocument:(documentTarget,section)=>notesFeature?.insertSummaryDocumentSection?.(documentTarget,section) || {outcome:'failed',message:'Document unavailable.'},onAttach:(receipt,expected)=>composer?.attachGeneratedSummary?.(receipt,{expected,isCurrent:selection.isCurrent})===true,onOpenSource:async(id,receipt)=>{
        const response=await fetch(`${state.api}/api/memory/code/resource`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:selection.workspaceId,operation:'resolve',reference:{kind:'code',id,receipt},excerpt_bytes:1024}),signal:selection.signal});
        const result=response.ok?await response.json():null;
        if(selection.isCurrent()&&result?.state==='current'&&result.current_source_verified===true&&codeReceiptKey(result.receipt)===codeReceiptKey(receipt)&&result.destination?.provider==='host'&&typeof result.destination.resource?.ref==='string')await openResource(result.destination.resource.ref,{isCurrent:selection.isCurrent});
      }});return {ok:true,message:'Summary preview opened.'};}
      if(kind==='navigate_comparison'){
        const target=selection.navigation;
        const targetID = target?.id || target?.file_id;
        if(!selection.isCurrent()||!target||!/^(file|decl):[0-9a-f]{64}$/.test(targetID||'')||target.receipt?.workspace_id!==target.workspace_id)return {ok:false};
        const verify=async(workspace,id,receipt,hash)=>{
          const response=await fetch(`${state.api}/api/memory/code/resource`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},signal:selection.signal,
            body:JSON.stringify({workspace_id:workspace,operation:'resolve',reference:{kind:'code',id,receipt},excerpt_bytes:1})});
          if(!selection.isCurrent()||!response.ok)return false;
          const value=await response.json();
          return selection.isCurrent()&&value?.state==='current'&&value.current_source_verified===true&&value.validation_kind==='current_source'
            && value.workspace_id===workspace&&value.reference?.id===id&&codeReceiptKey(value.receipt)===codeReceiptKey(receipt)&&value.source?.source_hash===hash;
        };
        if(!await verify(target.workspace_id,targetID,target.receipt,target.source_hash))return {ok:false,message:'Target receipt or source is unavailable.'};
        if(!await verify(selection.workspaceId,selection.node.id,selection.receipt,target.original_source_hash))return {ok:false,message:'Source receipt or source is unavailable.'};
        return selection.isCurrent()?{ok:true}:{ok:false};
      }
      const composer=window.chatModule;
      const draft=['open','history'].includes(kind)?null:composer?.captureCodeEvidenceDraft?.();
      if(!['open','history'].includes(kind)&&!draft)return {ok:false,message:'Open a chat composer before adding evidence.'};
      const range=kind==='open'?selection.sourceRange:undefined;
      const response=await fetch(`${state.api}/api/memory/code/resource${range?'?exact_revision=true':''}`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({workspace_id:selection.workspaceId,operation:'resolve',reference:{kind:'code',id:selection.node.id,receipt:selection.receipt},excerpt_bytes:1024}),signal:selection.signal});
      const result=response.ok?await response.json():null;
      if(!selection.isCurrent())return {ok:false};
      if(!result || result.schema!=='open-clank.code-resource/v1' || result.workspace_id!==selection.workspaceId
        || result.state!=='current' || result.current_source_verified!==true || result.validation_kind!=='current_source'
        || codeReceiptKey(result.receipt)!==codeReceiptKey(selection.receipt) || result.reference?.id!==selection.node.id)
        return {ok:false,message:`Source ${['stale','missing','erased','ambiguous','unavailable','denied','cancelled','invalid'].includes(result?.state)?result.state:'unavailable'}. No evidence was added or opened.`};
      if(kind==='history'){
        const resource=result.destination?.resource;
        if(result.destination?.provider!=='host'||typeof resource?.id!=='string')return {ok:false,message:'History destination unavailable.'};
        const live=()=>selection.isCurrent() && owner===state.accountId && epoch===state.contextEpoch && codeEpoch===codeGraphContextEpoch
          && codeGraphSelection===selectedWorkspace && selectedWorkspace.workspaceId===selection.workspaceId;
        if(!live())return {ok:false};
        if (!state.documentsLoaded || state.documentLoadError || state.sourceRefreshPending) {
          await (pendingDocumentLoad || loadDocuments(false));
          if (!live()) return {ok:false};
        }
        const copalWorkspace=state.workspace;
        const changeSetContext={account:owner,workspace:copalWorkspace,epoch,isCurrent:()=>owner===state.accountId&&epoch===state.contextEpoch&&state.workspace===copalWorkspace,
          choices:state.docs.filter(doc=>doc?.kind==='note'&&typeof doc.id==='string'&&typeof doc.name==='string').slice(0,100).map(doc=>({id:doc.id,name:doc.name})),api};
        const expectedReceipt=structuredClone(selection.receipt);
        const onRecordedImpact=async({locator,resourceID,actionID,versionID,isCurrent})=>{
          const valid=()=>live() && codeGraphController===workbench && typeof isCurrent==='function' && isCurrent()===true;
          if(!valid())return false;
          const response=await fetch(`${state.api}/api/memory/code/recorded-impact`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:selection.workspaceId,locator,resource_id:resourceID,expected_receipt:expectedReceipt}),signal:selection.signal});
          if(!valid()||!response.ok)return false;
          const value=await response.json();
          const comparison=value?.comparison;
          const keys=['action_id','current_sha256','current_size','file_id','index','recorded_sha256','recorded_size','resource_id','state','version','version_id','workspace_id'];
          if(valid()&&value?.schema==='openclank.recorded-code-impact/v1'&&value.state==='unavailable'&&value.graph===null
              && comparison && typeof comparison==='object' && JSON.stringify(Object.keys(comparison).sort())===JSON.stringify(keys)
              && comparison.version===1 && ['recorded_matches_current','current_differs_from_recorded'].includes(comparison.state)
              && ['current','unknown'].includes(comparison.index) && comparison.workspace_id===selection.workspaceId
              && comparison.resource_id===resourceID && comparison.action_id===actionID && comparison.version_id===versionID
              && /^file:[0-9a-f]{64}$/.test(comparison.file_id||'') && /^[0-9a-f]{64}$/.test(comparison.recorded_sha256||'')
              && /^[0-9a-f]{64}$/.test(comparison.current_sha256||'') && Number.isSafeInteger(comparison.recorded_size)
              && comparison.recorded_size>=1 && comparison.recorded_size<=1048576 && Number.isSafeInteger(comparison.current_size)
              && comparison.current_size>=0 && comparison.current_size<=1048576
              && ((comparison.recorded_sha256===comparison.current_sha256 && comparison.recorded_size===comparison.current_size)===(comparison.state==='recorded_matches_current')))
            { const retro=value.retrospective_declarations;
              const ids=x=>Array.isArray(x)&&x.length<=200&&x.every(id=>/^decl:[0-9a-f]{64}$/.test(id))&&new Set(x).size===x.length;
              const validRetro=retro?.state==='complete' && Object.keys(retro).sort().join(',')==='basis,current_only,matched,recorded_only,state'
                && retro.basis==='retrospective_current_parser' && Number.isSafeInteger(retro.matched) && retro.matched>=0 && retro.matched<=200
                && ids(retro.recorded_only) && ids(retro.current_only);
              return {kind:'recorded-comparison',state:comparison.state,index:comparison.index,
                retrospective:validRetro?{matched:retro.matched,recordedOnly:retro.recorded_only.length,currentOnly:retro.current_only.length}:null}; }
          if(!valid()||value?.schema!=='openclank.recorded-code-impact/v1'||value.state!=='current'||value.coverage!=='indexed_declarations_in_one_recorded_file'
              ||value.workspace_id!==selection.workspaceId||value.resource_id!==resourceID||value.recorded_after?.action_id!==actionID||value.recorded_after?.version_id!==versionID
              ||value.recorded_after?.sha256!==value.source?.sha256||value.recorded_after?.size_bytes!==value.source?.size_bytes
              ||codeReceiptKey(value.query?.expected_receipt)!==codeReceiptKey(expectedReceipt)||codeReceiptKey(value.graph?.receipt)!==codeReceiptKey(expectedReceipt))return false;
          // Applying may retire the originating selection; successful apply is final.
          return await workbench.applyQualifiedImpact(value,{expectedReceipt,isCurrent:valid});
        };
        if(!openResourceHistory({resourceId:resource.id,name:selection.node.name||'Selected source',provider:'host',isContextCurrent:live,onRecordedImpact,exactOnly:true,managedGroups:true,changeSetContext}))return {ok:false,message:'History is busy or unavailable.'};
        return {ok:true,message:'Opened recorded change fragments for this resource.'};
      }
      if(kind==='open'){
        if(result.destination?.provider!=='host'||typeof result.destination.resource?.ref!=='string')return {ok:false,message:'Editor destination unavailable.'};
        const expectedRevision = result.destination.expected_revision;
        if(range&&(expectedRevision?.kind!=='hostFingerprint'||range.sourceHash!==result.source?.source_hash))
          return {ok:false,message:'The exact indexed source range is no longer available.'};
        const opened=await openResource(result.destination.resource.ref,{isCurrent:selection.isCurrent,
          ...(range?{expectedRevision,sourceRange:range}:{}),
          handoffCurrent:()=>owner===state.accountId && epoch===state.contextEpoch && codeEpoch===codeGraphContextEpoch
            && codeGraphSelection===selectedWorkspace && selectedWorkspace.workspaceId===selection.workspaceId});
        return opened?{ok:true,message:range?'Opened witnessed source range in Editor.':'Opened current resource in Editor.'}:{ok:false};
      }
      return composer.appendCodeEvidence(result,{expected:draft,isCurrent:selection.isCurrent,ask:kind==='ask'});
    },
    readResourceCapabilities:async(workspaceId,signal)=>{
      const response=await fetch(`${state.api}/api/memory/code/resource`,{method:'POST',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({workspace_id:workspaceId,operation:'capabilities'}),signal});
      if(!response.ok)return {available:false,reason:'Source bridge unavailable.'};
      const result=await response.json();
      if(result.schema!=='open-clank.code-resource/v1'||result.workspace_id!==workspaceId)return {available:false,reason:'Invalid source capability.'};
      return result.capabilities?.find(cap=>cap.kind==='code')||{available:false,reason:'Code source producer unavailable.'};
    },readPreference:()=>preference(),writePreference:value=>preference(value),
    async saveRecordedInvestigation({workspaceId, receipt, pages, title, isCurrent}) {
      const target = saveScope();
      const valid = () => current() && isCurrent() && sameSaveScope(target, saveScope()) &&
        selectedWorkspace.workspaceId === workspaceId && receipt?.workspace_id === workspaceId;
      const {buildRecordedInvestigation} = await import('./copal/codeInvestigation.js');
      if (!valid()) throw new Error('The workspace changed.');
      const manifest = buildRecordedInvestigation(pages, receipt);
      const key = JSON.stringify([title, manifest]);
      if (!investigationActions.has(key)) investigationActions.set(key, crypto.randomUUID().replaceAll('-', ''));
      const body = {version:1, workspace:target.workspace, name:title, title, ...manifest, action_id:investigationActions.get(key)};
      const result = await api('/logical-change-sets', {method:'POST',body:JSON.stringify(body)}, target.workspace);
      if (!valid() || !(result?.state === 'complete' || result?.state === 'status_only' && result.receipt?.state === 'complete') ||
          !result.receipt?.document_id || !result.receipt.head) throw new Error('Save outcome is pending.');
      const documentId = result.receipt.document_id;
      return {documentId, open:async () => {
        if (!valid()) return;
        await refreshIndexedDocument(documentId, target);
        if (valid()) openDocument(documentId, 'notes');
      }};
    },
    async readRecordedActivity({workspaceId, receipt, signal, relativePath = '', cursor = null}) {
      const valid = () => current() && !signal?.aborted && workspaceId === selectedWorkspace.workspaceId
        && receipt?.workspace_id === workspaceId && typeof receipt.checkout_id === 'string';
      if (!valid()) throw new Error('Graph workspace changed');
      const issued = await filesFacadeClient.workspaceResource(workspaceId, relativePath, {signal});
      if (!valid() || issued.workspace?.id !== workspaceId || issued.resource?.provider !== 'host'
          || !/^resource-[0-9a-f]{64}$/.test(issued.resource.id))
        throw new Error('Recorded workspace activity unavailable');
      const params = new URLSearchParams({graph_activity:'true', graph_workspace_id:workspaceId,
        graph_checkout_id:receipt.checkout_id});
      if (cursor) params.set('cursor', cursor);
      const response = await fetch(`${state.api}/api/history/resources/${encodeURIComponent(issued.resource.id)}/workspace-recorded-activity?${params}`,
        {signal, credentials:'same-origin', cache:'no-store'});
      if (!response.ok) throw new Error('Recorded workspace activity unavailable');
      const data = await response.json(), scope = data.projection?.graph_scope;
      if (!valid() || scope?.workspace_id !== workspaceId || scope.checkout_id !== receipt.checkout_id
          || scope.workspace_revision !== issued.workspace.revision || scope.policy_generation !== issued.generation)
        throw new Error('Recorded activity scope changed');
      return data;
    },
    readPromptBudget:()=>preference(undefined,'code_context_prompt_budget'),
    writePromptBudget:value=>preference(value,'code_context_prompt_budget')});
  // From Editor focuses the supplied file once. Subsequent mounts restore the selected symbol.
  selectedWorkspace.openRelative = '';
  // A minimized native window retains its scope/subscription, not GPU work.
  // Actual close, mode replacement and scope invalidation still dispose it.
  const graphWindow=getViewContext('graph')?.window;
  const syncVisibility=()=>workbench.setVisible?.(graphWindow?.visible !== false);
  const visibilityObserver=new MutationObserver(syncVisibility);
  visibilityObserver.observe(graphWindow.root,{attributes:true,attributeFilter:['class','style']});
  const disposeWorkbench=workbench.dispose;
  workbench.dispose=()=>{folderRestore?.abort();folderRestore=null;visibilityObserver.disconnect();disposeWorkbench.call(workbench);};
  syncVisibility();
  codeGraphController=workbench;
  wrap._copalDestroy=()=>workbench.dispose();
  void restoreCodeFolder();
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
  const visible = [...getViewContext('graph').window.body.querySelectorAll('.copal-mind-tree-node')].map((item) => Number(item.dataset.line));
  const sequence = visible.length ? visible : entries.map((entry) => entry.line);
  const idx = sequence.indexOf(node.line);
  if (idx >= 0 && idx < sequence.length - 1) { mindModeState().selectedLine = sequence[idx + 1]; persistGraphView(); renderMind(); }
}

function mindSelectPrev(node, entries) {
  const visible = [...getViewContext('graph').window.body.querySelectorAll('.copal-mind-tree-node')].map((item) => Number(item.dataset.line));
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
  const context = getViewContext('graph');
  if (!context?.window.visible || getGraphView().mode !== 'structure') return;
  const body = context.window.body;
  body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
  context.sidebarContent.replaceChildren();
  const graphView = getGraphView();
  const allDocs = visibleDocs(undefined, context.filter);
  const exact=graphView.source?.connection && state.docs.find(item=>item.id===graphView.source.docId);
  if (exact && !allDocs.includes(exact)) allDocs.push(exact);
  const docs = allDocs.filter((doc) => ['wiki', 'note', 'markdown', 'text'].includes(String(doc.kind || '').toLowerCase()));
  const navigation = mindModeState();
  const requested = graphView.source?.docId || navigation.docId || context.selected;
  const doc = requested ? docs.find((d) => d.id === requested) : docs[0];
  const requestedAny = requested ? allDocs.find((item) => item.id === requested) : null;
  if (graphView.source?.docId && requestedAny && !doc) {
    body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' }, h('h2', { text:'Structure source unsupported' }), h('p', { text:`${requestedAny.name || requestedAny.id} is a ${requestedAny.kind || 'non-text'} resource and has no heading tree.` }), h('button', { class:'copal-btn', text:'Open source', onclick:() => openDocument(requestedAny.id, 'notes') }), h('button', { class:'copal-btn', text:'Choose another source', onclick:() => { graphView.source = null; navigation.docId = null; persistGraphView(); renderMind(); } })));
    return;
  }
  // Keep an explicit source identity visible through deletion, revocation, or
  // an unavailable projection. Never silently substitute another document.
  if (graphView.source?.docId && !doc) {
    body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' },
      h('h2', { text:'Structure source unavailable' }),
      h('p', { text:`The selected source ${graphView.source.docId} is unavailable in this account or workspace.` }),
      h('p', { text:'Its ResourceKey and revision are retained so it can be retried or reauthorized safely.' }),
      h('button', { class:'copal-btn', text:'Retry source', 'aria-label':'Retry unavailable structure source', onclick:() => loadDocuments() }),
      h('button', { class:'copal-btn', text:'Choose another source', onclick:() => { graphView.source = null; navigation.docId = null; persistGraphView(); renderMind(); } }),
    ));
    return;
  }
  if (!doc) {
    body.replaceChildren(h('div', { class: 'copal-mind-empty' }, h('h2', { text: 'Structure' }), h('p', { text: 'No document source is available for headings.' }), h('button', { class:'copal-btn', text:'Open Editor', onclick:() => open('notes') })));
    return;
  }
  navigation.docId = doc.id;
  graphView.source ||= graphSourceEnvelope(doc);
  persistGraphView();
  context.selected = doc.id;
  if (state.view === 'graph') state.selected = doc.id;
  const source = wikiText(doc);
  if (graphView.source.connection && (graphView.source.connection.head!==doc.head || source!==doc.text)) {
    delete graphView.source.connection;
    navigation.selectedLine=null;
    persistGraphView();
    setViewStatus('graph','The source changed. Reopen its link to locate the current section.',true);
  }
  if (doc.rawPreserved && doc.recoveryState !== 'legacy-import') {
    const parseFailed = doc.recoveryState === 'malformed-preserved';
    const sourceLabel = doc.recoveryState === 'unsupported-future' ? 'This source was written by a newer Wiki format.' : parseFailed ? 'This source could not be parsed as a native Wiki record.' : 'This source is preserved but unavailable to the headings parser.';
    body.replaceChildren(h('div', { class:'copal-mind-empty', role:'status' }, h('h2', { text:parseFailed ? 'Structure source parse failed' : 'Structure source unavailable' }), h('p', { text:sourceLabel }), h('p', { text:String(doc.note_error || 'Choose an explicit recovery action in the Editor.') }), h('button', { class:'copal-btn', text:'Open in Editor', onclick:() => openDocument(doc.id, 'notes') })));
    return;
  }
  const entries = mindOutlineEntries(source);
  const structureSettings=resolveGraphSettings(graphView.settings),allTreeNodes=buildMindTree(entries);let remaining=structureSettings.structureItems;
  const boundedTree=(rows,depth=1)=>{const result=[];if(depth>structureSettings.structureDepth)return result;for(const node of rows){if(remaining<=0)break;remaining--;result.push({...node,children:boundedTree(node.children,depth+1)});}return result;};
  const treeNodes=boundedTree(allTreeNodes);
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
      h('button', { class: 'copal-mind-doc-btn', text: d.name, 'aria-label':`Select ${d.name} as structure source`, onclick: () => { navigation.docId = d.id; graphView.source = graphSourceEnvelope(d); context.selected = d.id; if (state.view === 'graph') state.selected = d.id; navigation.collapsed.clear(); navigation.selectedLine = null; navigation.editingLine = null; persistGraphView(); renderMind(); } }))));
    pickerStatus.textContent = matches.length > rows.length ? `${matches.length} sources · showing ${rows.length}; search to reach more` : `${matches.length} source${matches.length === 1 ? '' : 's'}`;
  };
  pickerSearch.addEventListener('input', renderPicker);
  picker.append(pickerSearch, pickerStatus, docList);
  renderPicker();

  // Tree
  const treePane = h('div', { class: 'copal-mind-tree' });
  if(entries.length>structureSettings.structureItems||structureSettings.structureDepth<64)treePane.append(h('p',{role:'status',text:`Structure display is bounded to ${structureSettings.structureItems} items / ${structureSettings.structureDepth} levels. Source remains complete in Editor.`}));
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

  context.sidebarContent.replaceChildren(picker);
  body.replaceChildren(treePane);
  acknowledgeVisible(treePane, 'graph.mode.presented', { mode:'structure', nodeCount:entries.length }, { accountId:state.accountId, workspaceId:state.workspace });

  // Focus selected
  if (navigation.selectedLine) {
    const selected = treePane.querySelector(`[data-line="${navigation.selectedLine}"]`);
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
  dialog.append(baseField('Name', name), h('p', { text: 'Starts as a live table over this workspace. Configure columns, filters, sorts, groups, formulas, and summaries after creation.' }));
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
  const scope = saveScope();
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
    const readOnlySourceIds = new Set(state.docs.filter(doc => doc.readOnly).map(doc => String(doc.id)));
    const canEditCell = (row, column) => Boolean(row.documentId) && !row.readOnly
      && !readOnlySourceIds.has(String(row.documentId)) && !isReadOnlyColumn(column)
      && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
    const registerBaseCell = (cell, row, column, value, edit) => {
      const documentId = row.documentId;
      if (!documentId) return;
      const commands = () => [
        ...(edit && !state.docs.find(doc => String(doc.id) === String(documentId))?.readOnly ? [{ id:'edit-sheet-cell', label:'Edit cell', run:edit }] : []),
        ...(navigator.clipboard?.writeText ? [{ id:'copy-sheet-cell', label:'Copy cell', run:() => navigator.clipboard.writeText(value) }] : []),
        { id:'open-base-document', label:'Open source note', run:() => openDocument(documentId, 'notes') },
      ];
      const current = () => cell.isConnected && mount.contains(cell) && sameSaveScope(scope, saveScope());
      registerAdapter(cell, {
        editingCommands:false,
        hitTest:target => target.closest('input, textarea, select, [contenteditable="true"]') ? 'native' : true,
        capture:() => current() ? { documentId, column } : null,
        isCurrent:request => current() && request.adapterContext?.documentId === documentId && request.adapterContext?.column === column,
        commands:() => commands().map(({ run, ...item }) => item),
        execute:id => {
          const command = current() && commands().find(item => item.id === id);
          return command ? command.run() : false;
        },
      });
    };
    const renderTableRow = (row) => {
      const tr = h('tr', { 'data-document-id':row.documentId });
      for (const column of view.columns) {
        const value = formatBaseCell(row.values?.[column.property]);
        const editable = canEditCell(row, column);
        const td = h('td');
        const edit = () => makeInlineCellEditor(base, row, column, td, rerender);
        td.append(h('button', {
          class:'copal-base-cell', type:'button', text:value,
          title:editable ? `Edit ${column.property}` : `Open ${row.name}`,
          'aria-label':editable ? `Edit ${column.label} for ${row.name}: ${value}` : `Open ${row.name}: ${value}`,
          onclick:editable ? edit : () => openDocument(row.documentId, 'notes'),
        }));
        registerBaseCell(td, row, column, value, editable ? edit : null);
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
          const editable = canEditCell(row, column);
          const field = h('div', { class: 'copal-base-card-field' }, h('span', { class: 'copal-base-card-label', text: column.label }));
          const valueEl = h('span', { class: 'copal-base-card-value', text: value });
          const edit = () => makeInlineCellEditor(base, row, column, valueEl, rerender);
          if (editable) valueEl.style.cursor = 'pointer';
          valueEl.addEventListener('click', event => {
            if (event.target !== valueEl) return;
            if (editable) edit(); else openDocument(row.documentId, 'notes');
          });
          registerBaseCell(valueEl, row, column, value, editable ? edit : null);
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
        registerBaseCell(title, row, mainCol, title.textContent, null);
        item.append(title);
        for (const column of view.columns.slice(1)) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = canEditCell(row, column);
          const meta = h('span', { class:'copal-base-list-meta', text:`${column.label}: ${value}` });
          const edit = () => makeInlineCellEditor(base, row, column, meta, rerender);
          if (editable) {
            meta.style.cursor = 'pointer';
            meta.addEventListener('click', event => { if (event.target === meta) edit(); });
          }
          registerBaseCell(meta, row, column, value, editable ? edit : null);
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

let treeHouse, clankshop;
async function loadTreeHouse() {
  const {createTreeHouseFeature} = await import('./copal/treehouse.js');
  if (!treeHouse) {
    treeHouse = createTreeHouseFeature({h, api, setStatus, renderMarkdown, openDocument, openResource});
    if (state.storageNamespace) treeHouse.loadState();
    const owner = state.windows.get('treehouse');
    if (owner) treeHouse.setNavigationWindow(owner.window);
  }
  return treeHouse;
}

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
  const isCurrent = request?.payload?.isCurrent;
  if (isCurrent && !isCurrent()) return { outcome:'conflict', message:'The saved view changed. Reopen its menu and retry.' };
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
    if (isCurrent && !isCurrent()) return { outcome:'conflict', message:'The saved view changed. Reopen its menu and retry.' };
    const newest = notesFeature?.getDraftSnapshot?.(base.id) || null;
    const advanced = newest && (
      (localRevision != null && newest.localRevision !== localRevision)
      || (localRevision == null && String(newest.envelope?.text ?? '') !== source)
    );
    if (!advanced) break;
    if (isCurrent) return { outcome:'conflict', message:'The saved view changed. Reopen its menu and retry.' };
    draftSnapshot = newest;
    authoritativeSnapshot = notesFeature?.getAuthoritativeSnapshot?.(base.id) || null;
    snapshot = draftSnapshot || authoritativeSnapshot || null;
    source = String(snapshot.envelope?.text ?? currentBase.text ?? '');
    localRevision = snapshot.localRevision ?? null;
    transformed = null;
  }
  if (!transformed) return { outcome:'conflict', message:'The Base changed while this command was being prepared. Retry it on the newest draft.' };
  if (request?.payload?.expectedViewOrder && JSON.stringify(transformed.definition?.views?.map(view => view.id)) !== JSON.stringify(request.payload.expectedViewOrder)) {
    return { outcome:'conflict', message:'The saved view changed. Reopen its menu and retry.' };
  }
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
    canWriteCell:(row) => {
      const { source } = baseSourceForRow(row);
      return !!source && !source.readOnly && !row.readOnly;
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
    viewCommand:async (doc, action, view, controller) => {
      const scope = saveScope();
      const definition = controller.getState().definition;
      const views = definition?.views || [];
      const viewIndex = views.findIndex((item) => item.id === view?.id);
      const capturedView = views[viewIndex];
      const isCurrent = () => sameSaveScope(scope, saveScope()) && controller.getState().definition === definition
        && !doc.readOnly && !state.docs.find(item => String(item.id) === String(doc.id))?.readOnly;
      const run = async (command, payload) => {
        const result = await controller.command(command, { ...payload, isCurrent });
        if (result?.message && ['conflict', 'failed'].includes(result.outcome) && sameSaveScope(scope, saveScope())) setStatus(result.message, true);
        return result;
      };
      if (action === 'rename-view' && capturedView && isCurrent()) {
        const name = await styledPrompt('Rename view', { title:'Rename saved view', defaultValue:capturedView.name, confirmText:'Save', maxLength:128 });
        if (!name?.trim() || !isCurrent()) return;
        return run(action, { viewId:capturedView.id, name:name.trim() });
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
      if (!capturedView || !isCurrent()) return false;
      const viewId = capturedView.id;
      if (action === 'duplicate-view') return run(action, { viewId, name:`${capturedView.name} copy` });
      if (action === 'move-view-left' || action === 'move-view-right') {
        const toIndex = viewIndex + (action === 'move-view-left' ? -1 : 1);
        if (toIndex < 0 || toIndex >= views.length) return false;
        const expectedViewOrder = views.map(item => item.id);
        expectedViewOrder.splice(toIndex, 0, expectedViewOrder.splice(viewIndex, 1)[0]);
        return run('reorder-view', { viewId, fromIndex:viewIndex, toIndex, expectedViewOrder });
      }
      if (action === 'remove-view' && views.length > 1) {
        if (!await styledConfirm(`Delete view "${capturedView.name}"?`, { title:'Delete Base view', confirmText:'Delete view', danger:true }) || !isCurrent()) return false;
        return run(action, { viewId });
      }
      return false;
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
function documentNavigation(view) {
  const scope = saveScope();
  return { presentation:view, workspaceId:scope.workspace, isCurrent:() => sameSaveScope(scope, saveScope()) };
}

function buildDocumentFeature(view) { return createNotesFeature({
  h,
  api,
  state,
  renderMarkdown:(body, seen, doc) => renderMarkdown(body, seen, doc, { navigation:documentNavigation(view) }),
  renderPreview:(body, doc) => renderPreview(body, doc, { navigation:documentNavigation(view) }),
  renderComment:(body, doc) => markdownRenderer.renderComment(body, doc, { navigation:documentNavigation(view) }),
  formatBaseCell,
  saveDocument,
  showDocumentConflict,
  renameNote,
  deleteDocument,
  deleteDocuments,
  showHistory,
  showTrash,
  showForm,
  importVault,
  loadDocuments,
  openDocument,
  mountEditorActivity:view === 'notes' ? controls => {
    state.windows.get('notes')?.activityRail?.documentControls.replaceChildren(controls);
  } : null,
  persistActiveContext,
  saveResource:async(snapshot,options)=>{const {saveResourceSnapshot}=await import('./codeEditor.js');const receipt=await saveResourceSnapshot(snapshot,options);if(receipt?.snapshot?.envelope)window.dispatchEvent(new CustomEvent('openclank-files-resource-mutated',{detail:{action:'save',resource:options.resource,resourceRef:options.resource?.ref}}));return receipt;},
  uploadAttachment,
  commitAttachment,
  abortAttachment,
  activateNotes:() => activateView(view, {navigation:state.view !== view}),
  getContext:() => getViewContext(view),
  getDocumentContexts:editorDocumentContexts,
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
  connections:target=>journalConnections(target.doc,'incoming','both',()=>wikiFeature.captureEditorTarget()?.docId===target.docId),
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

async function renderTreeHouseSurface(view) {
  const owner = state.windows.get(view), scope = saveScope();
  if (!owner?.window.visible) return;
  const feature = await loadTreeHouse();
  if (!sameSaveScope(scope, saveScope()) || !owner.window.visible || state.windows.get(view) !== owner) return;
  if (view === 'achievements') return feature.renderAchievementsPanel(owner.window.body);
  return feature.render(owner.window.body);
}

async function renderClankshop() {
  const owner = state.windows.get('clankshop'), scope = saveScope();
  if (!owner?.window.visible) return;
  const {createClankshopFeature} = await import('./copal/clankshop.js');
  if (!sameSaveScope(scope, saveScope()) || !owner.window.visible || state.windows.get('clankshop') !== owner) return;
  clankshop ||= createClankshopFeature();
  return clankshop.render(owner.window.body, owner.window);
}

async function toggleTask(item, checked) {
  const scope = saveScope();
  const openDoc = item?.doc && state.docs.find((value) => value.id === item.doc.id);
  const draft = openDoc ? notesFeature?.getDraftSnapshot?.(openDoc.id) : null;
  if (openDoc && draft && !['notes', 'wiki'].includes(state.view)) {
    if (!await notesFeature?.resolveDirtyDocuments?.([openDoc.id], { force:true, title:'Update task in document' }) || !sameSaveScope(scope, saveScope())) return;
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
  const scope = saveScope();
  if (!state.docs.some((doc) => String(doc.id) === String(id))) {
    try {
      const doc = await api(`/documents/${encodeURIComponent(id)}`, {}, scope.workspace);
      if (!sameSaveScope(scope, saveScope())) return;
      if (!state.docs.some(item => String(item.id) === String(id))) state.docs.push(doc);
    } catch (error) {
      if (sameSaveScope(scope, saveScope())) setStatus(error.message, true);
      return;
    }
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
  showForm('New document', [['name', 'Name', state.view === 'treehouse' ? 'TreeHouse/New Lesson.md' : 'Untitled'], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
    const result = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: defaultKind, content }) });
    state.selected = result.doc.id; await loadDocuments();
  });
}

function importVault() {
  const input = h('input', { type: 'file', accept: '.zip,application/zip' });
  input.style.display = 'none'; document.body.append(input);
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; input.remove();
    if (!file || !await styledConfirm(`Import ${file.name} into workspace “${state.workspace}”? Existing same-named documents are versioned, not silently replaced.`, { title: 'Import vault', confirmText: 'Import', danger: false })) return;
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
  const target = getViewContext(view);
  if (!target || (view === 'notes' && target.window.body.hidden)
      || (view === 'graph' && !target.window.visible)) return;
  // A cold load can render/focus several visible windows. That is not a
  // newer navigation intent and must not cancel its own source handoff.
  const context = activateView(view, {navigation:false});
  if (!context?.window.body) return;
  // renderCalendar intentionally remains in this module as dormant,
  // recoverable boutique plumbing. Odysseus's native Calendar owns the active
  // route/menu and receives Copal events through the backend projector.
  const renderers = { notes: renderNotes, wiki: renderWiki, timeline: renderTimeline, graph: renderGraph, bases: renderBases, treehouse: () => renderTreeHouseSurface('treehouse'), clankshop: renderClankshop, achievements: () => renderTreeHouseSurface('achievements'), todo: renderTodo };
  const rendered = (renderers[view] || renderNotes)();
  persistActiveContext();
  return rendered;
}

function ensureViewWindow(view) {
  view = resolveView(view);
  if (view === 'graph' || view === 'wiki') {
    const editor = ensureViewWindow('notes');
    if (view === 'graph') return editor.graphMode ||= createEditorGraphMode(editor.window);
    return editor.wikiMode ||= createEditorWikiMode(editor.window);
  }
  if (state.windows.has(view)) return state.windows.get(view);
  const modalId = `copal-${view}-modal`;
  const windowApi = createCopalWindow({
    id:modalId,
    label:LABELS[view],
    icon: { notes:'code', wiki:'book', timeline:'timeline', treehouse:'treehouse', clankshop:'clankshop', achievements:'check', todo:'tasks' }[view] || 'document',
    subtitle:'Open Clank',
    navigationChrome:view !== 'achievements' && view !== 'clankshop',
    chatVisibleMaximize:view === 'notes',
    focusTarget:() => view === 'notes' ? getViewContext(state.windows.get('notes')?.editorMode)?.window.body : null,
    minWidth:view === 'timeline' ? 720 : 560,
    minHeight:420,
    sizeKey:copalStorageKey(`openclank-copal-${view}-window-size`),
    className:`copal-view-window copal-${view}-window`,
    onActivate:() => {
      if (!state.windows.has(view)) return;
      const active = view === 'notes' ? state.windows.get(view).editorMode || 'notes' : view;
      const changed = state.view !== active;
      activateView(active, {navigation:changed});
      if (view === 'treehouse') treeHouse?.refreshEngagement();
      markActive();
      localStorage.setItem(copalStorageKey('openclank-copal-view'), active);
      if (changed && resolveAppletLocation(location.pathname, location.search)?.target === 'editor') updateRoute(active, true);
    },
  onBeforeClose:async () => {
    if (view !== 'notes') return true;
    if (!await notesFeature.beforeWindowClose()) return false;
    return wikiFeature.beforeWindowClose();
  },
  onClosed:() => {
    if (view === 'treehouse') treeHouse?.stopEngagement();
    if (view === 'clankshop') clankshop?.destroy();
    if (view === 'notes') {
      showEditorMode('notes');
      notesFeature.destroy();
      wikiFeature.destroy();
    }
    markActive();
  },
  });
  const search = h('input', { class:'copal-search', type:'search', placeholder:`Search ${LABELS[view]}…`, 'aria-label':`Search ${LABELS[view]}` });
  const context = { view, window:windowApi, search, selected:null, filter:'', reading:false };
  if (['notes','wiki'].includes(view)) {
    // Real Editor interaction supersedes an awaited Graph source handoff;
    // programmatic show/focus/render callbacks above do not impersonate it.
    for (const eventName of ['pointerdown','keydown']) windowApi.root.addEventListener(eventName, () => {
      editorNavigationTicket += 1;
    }, {capture:true});
  }
  if (['notes', 'wiki'].includes(view)) {
    try {
      const saved = JSON.parse(localStorage.getItem(copalStorageKey(`openclank-copal-${view}-layout`, state.workspace)) || '{}');
      featureFor(view)?.loadSaved(context, saved);
      context.selected = saved.selected || null;
    } catch (_) {}
  }
  state.windows.set(view, context);
  if (view === 'notes') createEditorActivityRail(context);
  if(view==='treehouse'&&treeHouse)treeHouse.setNavigationWindow(windowApi);
  search.addEventListener('input', () => { activateView(view); context.filter = search.value; state.filter = search.value; renderView(view); });
  const activate = (callback) => (...args) => { activateView(view); return callback(...args); };
  if (['notes', 'wiki'].includes(view)) {
    context.documentActions = h('div', { class:'copal-editor-document-actions' },
      h('button', { class:'copal-btn', icon:'search', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => featureFor(context.editorMode)?.showChooser() }),
      h('button', { class:'copal-btn', icon:'menu', title:'Editor commands', 'aria-label':'Editor commands', onclick:() => featureFor(context.editorMode)?.showCommands() }),
    );
    windowApi.actions.append(context.documentActions);
  } else if (['achievements', 'treehouse', 'clankshop'].includes(view)) {
    // Learning actions live inside TreeHouse; achievements update from events.
    // Keep shared navigation adapters available to other window consumers.
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

function createEditorActivityRail(editor) {
  const buttons = Object.fromEntries([['notes', 'Text', 'text'], ['wiki', 'Wiki', 'puzzle'], ['graph', 'Graph', 'graph']].map(([view, label, icon]) => [
    view, h('button', { type:'button', icon, class:view === 'notes' ? 'active' : '',
      title:label, 'aria-label':label, 'aria-current':view === 'notes' ? 'page' : null, 'data-editor-mode-button':view,
      onclick:async () => {
        const switching = (editor.editorMode || 'notes') !== view;
        if (switching) await open(view, true, { focusRail:true });
        if (view === 'notes' && state.view === 'notes') notesFeature?.showExplorer(switching);
      },
    }),
  ]));
  const documentControls = h('div', { class:'copal-editor-document-activity' });
  const rail = h('nav', { class:'copal-notes-ribbon copal-editor-activity-rail', 'aria-label':'Editor activity' },
    h('div', { class:'copal-editor-mode-buttons', role:'group', 'aria-label':'Editor modes' }, ...Object.values(buttons)),
    documentControls);
  editor.activityRail = { rail, buttons, documentControls };
  editor.window.root.dataset.editorMode = editor.editorMode = 'notes';
  editor.window.body.prepend(rail);
}

function showEditorMode(view) {
  const editor = state.windows.get('notes');
  if (!editor || (editor.editorMode || 'notes') === view) return;
  const previous = editor.editorMode;
  const graph = editor.graphMode;
  const wiki = editor.wikiMode;
  const graphVisible = view === 'graph';
  const wikiVisible = view === 'wiki';
  editor.editorMode = view;
  editor.window.root.dataset.editorMode = view;
  editor.window.body.hidden = view !== 'notes';
  editor.window.body.inert = view !== 'notes';
  editor.window.status.hidden = graphVisible;
  editor.documentActions.hidden = graphVisible;
  editor.noteWorkbenchMenu?.close({ restore:false });
  if (editor.noteWorkbenchMenu) editor.noteWorkbenchMenu.element.hidden = view !== 'notes';
  if (graph) graph.host.hidden = !graphVisible;
  if (wiki) {
    wiki.host.hidden = !wikiVisible;
    wiki.window.body.hidden = !wikiVisible;
    wiki.host.inert = !wikiVisible;
    if (!wikiVisible) wiki.noteWorkbenchMenu?.close({restore:false});
    if (wiki.noteWorkbenchMenu) wiki.noteWorkbenchMenu.element.hidden = !wikiVisible;
  }
  const { rail, buttons, documentControls } = editor.activityRail;
  const dock = graphVisible ? graph.layout : wikiVisible ? wiki.host : editor.window.body;
  dock.prepend(rail);
  documentControls.hidden = view !== 'notes';
  for (const [mode, button] of Object.entries(buttons)) {
    button.classList.toggle('active', mode === view);
    if (mode === view) button.setAttribute('aria-current','page');
    else button.removeAttribute('aria-current');
  }
  if (graphVisible) {
    for (const owner of editorDocumentContexts()) for (const image of owner.noteImages?.values() || []) {
      for (const imageView of image.views.values()) imageView.runtime?.setVisible(false);
    }
  }
  if (previous === 'graph' && graph) {
    graph.graphMenu.close({ restore:false });
    ++graphRenderTicket;
    graph.window.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    codeGraphController?.dispose(); codeGraphController = null;
    graph.window.body.replaceChildren();
    graph.sidebarContent.replaceChildren();
  }
}

function createEditorWikiMode(windowApi) {
  const body = h('main', {class:'copal-view copal-editor-wiki-body', tabindex:'-1', hidden:true, 'aria-label':'Wiki articles', 'data-editor-help-mode':'wiki'});
  const host = h('section', {class:'copal-editor-wiki', hidden:true, inert:true, 'aria-label':'Editor Wiki mode'}, body);
  windowApi.body.after(host);
  const search = h('input', {type:'search','aria-label':'Search Wiki'});
  const context = {view:'wiki', host, search, selected:null, filter:'', reading:false, window:{
    id:windowApi.id, root:windowApi.root, workbenchHost:host, content:host, body, heading:windowApi.heading, status:windowApi.status,
    get visible() { return windowApi.visible && !host.hidden; },
    get physicalVisible() { return windowApi.visible; },
    show:trigger=>windowApi.show(trigger), focus:()=>windowApi.focus(), requestClose:fromManager=>windowApi.requestClose(fromManager),
    setStatus:(message,bad)=>windowApi.setStatus(message,bad),
  }};
  try {
    const saved=JSON.parse(localStorage.getItem(copalStorageKey('openclank-copal-wiki-layout',state.workspace)) || '{}');
    wikiFeature.loadSaved(context,saved);
    context.selected=saved.selected || null;
  } catch { /* Preserve the existing layout recovery policy. */ }
  return context;
}

function createEditorGraphMode(windowApi) {
  installEditorFilesStyles();
  const graphSearch = h('input', { class:'copal-search', type:'search', hidden:true, placeholder:'Search documents…', 'aria-label':'Search Graph documents' });
  const status = h('p', { class:'copal-editor-graph-status', role:'status' });
  const body = h('div', { class:'copal-editor-graph-body', tabindex:'-1', role:'region', 'aria-label':'Graph canvas', 'data-editor-help-mode':'graph' });
  const host = h('section', { class:'copal-editor-graph', hidden:true, 'aria-label':'Editor Graph mode' });
  const modePicker = h('select', { 'data-graph-mode':true },
    h('option', { value:'documents', text:'Journal & Wiki' }),
    h('option', { value:'structure', text:'Structure' }),
    h('option', { value:'galaxy', text:'Galaxy' }),
    h('option', { value:'code', text:'Code' }));
  const sidebarContent = h('div');
  const sidebar = h('aside', { class:'copal-editor-graph-sidebar', 'aria-label':'Graph sidebar' },
    h('label', {}, 'Graph view', modePicker), graphSearch, sidebarContent, status);
  const content = h('div', { class:'copal-editor-graph-content' }, sidebar, body);
  host.append(content);
  windowApi.body.after(host);
  const context = { view:'graph', host, layout:content, modePicker, sidebarContent, search:graphSearch, selected:null, filter:'', reading:false,
    window: {
      id:windowApi.id, root:windowApi.root, content:host, heading:windowApi.heading, body, status,
      get visible() { return !host.hidden && windowApi.visible; },
      show:trigger => windowApi.show(trigger), focus:() => windowApi.focus(),
      requestClose:fromManager => windowApi.requestClose(fromManager),
      setStatus(message, bad = false) { status.textContent = message || ''; status.classList.toggle('error', bad); },
    },
  };
  function selectGraphFamily(mode) {
    getGraphView().mode = mode;
    persistGraphView();
    context.window.setStatus('');
    renderGraph();
    updateRoute('graph', true);
    if (mode === 'code') return;
    const needsSource = mode === 'galaxy' ? !state.planningLoaded || state.planningLoadError
      : !state.documentsLoaded || state.documentLoadError;
    if (needsSource || state.sourceRefreshPending) void loadDocuments();
    else updateDocumentViewStatus('graph');
  }
  modePicker.addEventListener('change', () => selectGraphFamily(modePicker.value));
  graphSearch.addEventListener('input', () => {
    activateView('graph');
    state.filter = context.filter = graphSearch.value;
    renderGraph();
    graphSearch.hidden = false;
  });
  const activate = callback => (...args) => { activateView('graph'); return callback(...args); };
    const capture=()=>({account:state.accountId,epoch:state.contextEpoch,workspace:state.workspace,mode:getGraphView().mode,
      generation:codeGraphMenuGeneration,code:getGraphView().mode==='code'?JSON.stringify(codeGraphController?.menuCapture?.()||null):null});
    const isCurrent=c=>host.isConnected&&!host.hidden&&c.account===state.accountId&&c.epoch===state.contextEpoch&&c.workspace===state.workspace&&c.mode===getGraphView().mode
      && (c.mode !== 'code' || (c.generation === codeGraphMenuGeneration
        && (c.code === 'null' || codeGraphController?.menuScopeCurrent?.(JSON.parse(c.code)))));
    context.graphMenu=mountWorkbenchMenu({host,container:host,before:content,id:'editor-graph',kind:'graph',label:'Graph',surface:{capture,isCurrent,commands:c=>{
      const family=['documents','structure','galaxy','code'].map(mode=>({id:'graph.family-'+mode,menu:'View',label:mode==='code'?'Code Graph':mode[0].toUpperCase()+mode.slice(1),checked:c.mode===mode,
        run:()=>selectGraphFamily(mode)}));
      const openFolder={id:'graph.choose',menu:'File',label:'Open folder…',run:()=>openGraphFolder(context)};
      if(c.mode==='code'){const wikiWorkspace=codeGraphSelection&&!codeGraphSelection.overlay?codeGraphSelection.workspaceId:'';
        const commands=codeGraphController?.menuCommands?.(JSON.parse(c.code))||[];
        return [...family,openFolder,...commands.filter(command=>command.id!==openFolder.id),
          {id:'graph.wiki-generate',menu:'Graph',label:'Generate code wiki…',disabledReason:wikiWorkspace?'':'Choose a Code Graph workspace first.',
            run:()=>{if(wikiWorkspace&&isCurrent(c))openWikiGenerate({workspaceId:wikiWorkspace,copalWorkspace:state.workspace,base:state.api,isCurrent:()=>isCurrent(c)&&codeGraphSelection?.workspaceId===wikiWorkspace,onCaptureDocument:()=>notesFeature?.captureSummaryDocumentTarget?.()||null,onInsertWiki:(handle,payload)=>notesFeature?.insertWikiPages?.(handle,payload)||{outcome:'failed',message:'Document unavailable.'}});}}];}
      return [...family,openFolder,
        {id:'graph.document-import',menu:'File',label:'Import Obsidian vault…',run:activate(importVault)},
        {id:'graph.document-export',menu:'File',label:'Export Obsidian vault…',run:()=>{window.location.href=`/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`;}},
        {id:'graph.document-search',menu:'View',label:'Search documents…',run:()=>{context.search.hidden=false;context.search.focus();}},
        {id:'graph.settings',menu:'View',label:'Graph settings…',run:activate(showFamilyGraphSettings)},
        {id:'graph.document-refresh',menu:'Graph',label:'Refresh documents',run:activate(()=>loadDocuments())}];
    }}});
  return context;
}

// --- Per-entry Copal visibility (Appearance) ---
// Visibility is a per-user appearance preference (not authorization): hiding a
// launcher only removes its sidebar link. `open(view)` and open windows keep
// working, so direct links/commands/agent actions still reach hidden features
// and no data is touched. Driven by the same VIEWS/LABELS registry as navigation.
const ENTRY_VIS_KEY = 'copal_entry_visibility';
function defaultEntryVisibility() {
  const map = {};
  for (const view of COPAL_LAUNCHER_IDS) map[view] = true;
  return map;
}
function loadEntryVisibility() {
  if (entryVisibilityLoad) return entryVisibilityLoad;
  const scope = saveScope();
  const pending = readEntryVisibility().then(() => {
    if (sameSaveScope(scope, saveScope())) applyEntryVisibility();
  }).finally(() => {
    if (entryVisibilityLoad === pending) entryVisibilityLoad = null;
  });
  entryVisibilityLoad = pending;
  return pending;
}

async function readEntryVisibility() {
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
  const value = { ...state.entryVisibility };
  const response = await fetch(`/api/prefs/${ENTRY_VIS_KEY}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value }) });
  if (!response.ok) throw new Error(`Appearance preference failed (${response.status})`);
}
function applyEntryVisibility() {
  const map = state.entryVisibility || defaultEntryVisibility();
  const journalRow = document.querySelector('[data-copal-journal-row]');
  if (journalRow) journalRow.hidden = map.journal === false;
  document.querySelectorAll('[data-copal-view], [data-copal-launcher]').forEach((link) => {
    const view = link.dataset.copalView || link.dataset.copalLauncher;
    if (view === 'imps') { link.style.display = 'none'; return; }
    if (!COPAL_LAUNCHER_IDS.includes(view)) return;
    link.style.display = link.dataset.copalCompat === 'true' ? 'none' : map[view] === false ? 'none' : '';
  });
}
export function getAppearanceEntries() { return COPAL_LAUNCHER_IDS.map((id) => ({ id, label: LAUNCHER_LABELS[id] || id })); }
export function getEntryVisibility() { return state.entryVisibility ? { ...state.entryVisibility } : null; }
export async function updateEntryVisibility(id, visible) {
  if (!COPAL_LAUNCHER_IDS.includes(id) || !state.entryVisibility) throw new Error('Copal navigation is unavailable. Reopen Appearance to retry.');
  const previous = { ...state.entryVisibility };
  state.entryVisibility = { ...previous, [id]: visible === true };
  try { await saveEntryVisibility(); applyEntryVisibility(); return { ...state.entryVisibility }; }
  catch (error) { state.entryVisibility = previous; applyEntryVisibility(); throw error; }
}
async function replaceEntryVisibility(map) {
  if (!state.entryVisibility) throw new Error('Copal navigation is unavailable. Reopen Appearance to retry.');
  const previous = { ...state.entryVisibility };
  state.entryVisibility = map;
  try { await saveEntryVisibility(); applyEntryVisibility(); return { ...state.entryVisibility }; }
  catch (error) { state.entryVisibility = previous; applyEntryVisibility(); throw error; }
}
export function setAllEntryVisibility(visible) {
  return replaceEntryVisibility(Object.fromEntries(COPAL_LAUNCHER_IDS.map((id) => [id, visible === true])));
}
export function resetEntryVisibility() { return replaceEntryVisibility(defaultEntryVisibility()); }
export async function whenReady() {
  if (!state.storageNamespace) await init(state.api || window.location.origin);
  if (!state.storageNamespace) throw new Error('Copal navigation is unavailable. Reopen Appearance to retry.');
  const scope = saveScope();
  if (state.entryVisibility === null) await loadEntryVisibility();
  if (!sameSaveScope(scope, saveScope())) throw new Error('The active account changed.');
  if (state.entryVisibilityError) throw state.entryVisibilityError;
  if (!state.entryVisibility) throw new Error('Copal navigation is unavailable. Reopen Appearance to retry.');
}

function acceptJournalCatalog(catalog) {
  if (catalog?.accountId !== state.accountId || !Array.isArray(catalog.configuredIds) || !catalog.descriptors ||
      catalog.revision < state.journalCatalog?.revision) return false;
  state.journalCatalog = catalog;
  return true;
}

async function openConfiguredJournal(binderId) {
  const scope = saveScope();
  const opening = open('notes');
  const ticket = editorNavigationTicket;
  await opening;
  const context = state.windows.get('notes');
  const current = () => context?.window.visible && state.view === 'notes'
    && sameSaveScope(scope, saveScope()) && ticket === editorNavigationTicket;
  if (!current()) return false;
  try {
    const catalog = await filesFacadeClient.binders();
    if (!current() || catalog.accountId !== scope.accountId || !acceptJournalCatalog(catalog)) return false;
    const descriptor = catalog.descriptors[binderId ?? catalog.defaultId];
    if (!descriptor?.enabled) throw new Error(binderId
      ? 'This Journal is no longer available. Refresh Manage journals and try again.'
      : 'Choose a default Journal in Manage journals.');
    if (descriptor.source.workspaceId !== scope.workspace) throw new Error('This Journal belongs to a different workspace.');
    const { homeDocumentId } = await filesFacadeClient.resolveBinder(descriptor.id, catalog.revision, descriptor, {openHome:true});
    if (!current()) return false;
    if (homeDocumentId && !state.docs.some(doc => doc.id === homeDocumentId)) await loadDocuments(false);
    return current() && notesFeature.showJournal({ accountId:catalog.accountId, workspace:scope.workspace,
      catalogRevision:catalog.revision, descriptor }, homeDocumentId);
  } catch (error) {
    if (current()) throw error;
    return false;
  }
}

export async function manageJournals() {
  const origin = saveScope();
  const { openJournalManager } = await import('./copal/journalManager.js');
  if (!sameSaveScope(origin, saveScope())) return null;
  return openJournalManager({
    getScope:() => ({ accountId:state.accountId, workspace:state.workspace, generation:state.contextEpoch }),
    openJournal:openConfiguredJournal,
    onCatalogChanged:catalog => { if (acceptJournalCatalog(catalog)) notesFeature?.syncJournalCatalog(catalog); },
  });
}

function bindSidebar() {
  state.navigationEvents?.abort();
  state.navigationEvents = new AbortController();
  const options = { signal:state.navigationEvents.signal };
  window.addEventListener('popstate', () => {
    void openResolvedRoute(resolveAppletLocation(location.pathname, location.search));
  }, options);
  document.querySelectorAll('[data-copal-view]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); open(link.dataset.copalView); }, options));
  document.querySelectorAll('[data-copal-launcher="code"], [data-copal-launcher="files"]').forEach((link) => link.addEventListener('click', (event) => {
    if (link.tagName === 'A') return;
    event.preventDefault(); window.location.href = link.dataset.copalLauncher === 'code' ? '/code' : '/files';
  }, options));
  document.querySelector('[data-copal-launcher="journal"]')?.addEventListener('click', async () => {
    if (!state.storageNamespace) await init(state.api || window.location.origin);
    if (!state.storageNamespace) { window.uiModule?.showError?.('Could not open the Journal. Select Journal to retry.'); return; }
    void openConfiguredJournal().catch(error => window.uiModule?.showError?.(error.message || 'Could not open the Journal.'));
  }, options);
  document.querySelector('[data-copal-journal-manager]')?.addEventListener('click', async () => {
    if (!state.storageNamespace) await init(state.api || window.location.origin);
    if (!state.storageNamespace) { window.uiModule?.showError?.('Journal settings are unavailable. Select Manage journals to retry.'); return; }
    void manageJournals().catch(error => window.uiModule?.showError?.(error.message || 'Could not open Manage journals.'));
  }, options);
  document.getElementById('tool-instruction-manual-btn')?.addEventListener('click', async () => {
    if (!state.storageNamespace) await init(state.api || window.location.origin);
    const scope = saveScope();
    try {
      const opened = await openClankHandbook({ presentation:'copal' });
      if (!opened && sameSaveScope(scope, saveScope())) throw new Error('The Instruction Manual is unavailable.');
    } catch (error) {
      if (sameSaveScope(scope, saveScope())) window.uiModule?.showError?.(error.message || 'Could not open the Instruction Manual.');
    }
  }, options);
}

function connectEvents(reconnecting = false) {
  const scope = saveScope();
  state.events?.close();
  const source = new EventSource(`${state.api}/api/copal/events?workspace=${encodeURIComponent(scope.workspace)}`);
  state.events = source;
  const current = () => state.events === source && sameSaveScope(scope, saveScope());
  const changed = new Set();
  let refreshAll = false;
  let reconnectTimer;
  // The mutating client refreshes directly. Cache event IDs through its
  // suppression window, then read the index once for the whole batch.
  const scheduleRefresh = () => {
    if (!current()) return;
    clearTimeout(state.reloadTimer);
    const deferred = Math.max(0, state.ignoreEventsUntil - Date.now());
    state.reloadTimer = setTimeout(() => {
      if (!current()) return;
      if (Date.now() < state.ignoreEventsUntil) { scheduleRefresh(); return; }
      const watched = refreshAll ? [null] : [...changed];
      changed.clear(); refreshAll = false;
      const visible = visibleViewEntries().some(([view]) => view !== 'graph' || getGraphView().mode !== 'code');
      if (!visible) { state.sourceRefreshPending = true; return; }
      void loadDocuments(true, watched);
    }, Math.max(120, deferred + 20));
  };
  const refresh = (event) => {
    let docId;
    try {
      const payload = JSON.parse(event.data);
      docId = payload.id || payload.documentId;
    } catch { /* A notification without a usable ID invalidates all Base leaves. */ }
    if (docId) changed.add(docId); else refreshAll = true;
    scheduleRefresh();
  };
  source.addEventListener('document', refresh);
  source.addEventListener('deleted', refresh);
  source.addEventListener('ready', () => {
    if (!current() || (!reconnecting && !state.documentLoadError && !state.planningLoadError)) return;
    reconnecting = false;
    refreshAll = true;
    scheduleRefresh();
  });
  // A reset can refuse EventSource's own reconnect with an HTTP error.
  source.onerror = () => {
    if (!current()) return;
    reconnecting = true;
    if (source.readyState !== EventSource.CLOSED) return;
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(async () => {
      if (!current()) return;
      // EventSource does not expose the HTTP rejection. Recheck the owner
      // before a retry, including a logout that left the stream closed.
      try {
        const status = await api('/status', { signal:AbortSignal.timeout(3000) }, scope.workspace);
        if (!current()) return;
        if (status?.storage_namespace !== scope.storageNamespace || (status?.account_id || null) !== scope.accountId) {
          retireCopalScope();
          return;
        }
      } catch (error) {
        if (!current()) return;
        if (isCopalAuthorityFailure(error)) { retireCopalScope(); return; }
      }
      if (current()) connectEvents(true);
    }, 5000);
  };
}

function suspendCopalScope() {
  if (state.storageNamespace && state.windows.size) {
    persistActiveContext();
  }
  notesFeature?.suspendScope();
  wikiFeature?.suspendScope();
  planningFeature?.suspendScope?.();
  treeHouse?.suspendScope();
  state.events?.close(); state.events = null;
  state.navigationEvents?.abort(); state.navigationEvents = null;
  state.filesMutationEvents?.abort(); state.filesMutationEvents = null;
  clearTimeout(state.reloadTimer); state.reloadTimer = null;
  for (const timer of state.saveTimers.values()) clearTimeout(timer);
  state.saveTimers.clear();
  for (const context of state.windows.values()) {
    context.graphMode?.graphMenu.dispose();
    context.graphMode?.window.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();
    context.window.destroy();
  }
  state.windows.clear();
  pendingDocumentLoad = null;
  entryVisibilityLoad = null;
  codeGraphController?.dispose();
  codeGraphController = null;
  codeGraphSelection = null;
  for (const dialog of document.querySelectorAll('dialog.copal-dialog')) { dialog.close(); dialog.remove(); }
  for (const editor of state.noteEditors) editor.destroy();
  state.noteEditors.clear();
  Object.assign(state, {
    storageNamespace:null, accountId:null, workspace:'default', view:'notes', docs:[],
    documentWriteVersion:0, documentWriteVersions:new Map(), journalCatalog:null,
    documentsLoaded:false, planningLoaded:false, documentLoadError:null, planningLoadError:null, sourceRefreshPending:false,
    planning:{ tracks:[], floatingTodos:[] }, taskProjection:[], taskProjectionLoaded:false, taskCursor:null, taskSnapshotRevision:null, taskProjectionTotal:null, taskIndexedTotal:null, taskMatchedTotal:null, taskTotalExact:false, taskLoading:false, taskQuery:{ query:'', completed:null, source:'all', sourceFilter:'all', statusFilter:'all', hideDone:false }, taskQueryToken:state.taskQueryToken + 1, taskSelectedId:null, selected:null, filter:'', reading:false,
    root:null, content:null, body:null, title:null, status:null, search:null,
    calendarMonth:null, projectedPlanningHead:null, projectingPlanning:null, ignoreEventsUntil:0,
    baseId:null, baseView:null, basePage:1, baseDefinition:null, baseSourceDocs:new Map(),
    baseFocusRow:-1, baseFocusCol:-1, baseFocusTable:null, baseQueryToken:state.baseQueryToken + 1,
    entryVisibility:null, entryVisibilityError:null,
    filesMutationBridgeInstallations:0,
  });
  graphRenderTicket += 1;
  graphView = null; graphScopeKey = null;
}

function retireCopalScope() {
  state.contextEpoch += 1;
  copalInitialization = null;
  suspendCopalScope();
  bindSidebar();
}

function handleCopalAccountChange(event) {
  const account = event.detail?.accountId || null;
  if (account && account === state.accountId) return;
  const previous = expectedCopalAccount;
  expectedCopalAccount = account;
  if (!state.storageNamespace && previous === null) return;
  retireCopalScope();
  if (account) void init(state.api || window.location.origin);
}

document.addEventListener('openclank:auth-context-changed', handleCopalAccountChange);
window.addEventListener('openclank:auth-context-changed', handleCopalAccountChange);
window.addEventListener('online', () => {
  if (!state.storageNamespace && state.api) void init(state.api);
});

export function init(apiBase = window.location.origin) {
  if (copalInitialization) return copalInitialization;
  if (state.storageNamespace && state.api === apiBase) return Promise.resolve();
  const pending = initializeCopal(apiBase).finally(() => {
    if (copalInitialization === pending) copalInitialization = null;
  });
  copalInitialization = pending;
  return pending;
}

async function initializeCopal(apiBase) {
  suspendCopalScope();
  const epoch = ++state.contextEpoch;
  bindSidebar();
  const initialNavigationTicket=editorNavigationTicket;
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
        console.error('[copal] could not load the authenticated storage scope', error);
        if (initialNavigationTicket === editorNavigationTicket && resolveAppletLocation(location.pathname, location.search)?.target === 'editor') {
          window.uiModule?.showError?.('Could not open this applet. Open it again to retry.');
        }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
      if (epoch !== state.contextEpoch) return;
    }
  }
  if (epoch !== state.contextEpoch || (expectedCopalAccount && status?.account_id !== expectedCopalAccount)) return;
  if (typeof status?.storage_namespace !== 'string' || !status.storage_namespace.trim()) return;
  configureCopalStorage(status.storage_namespace);
  state.storageNamespace = status.storage_namespace;
  state.accountId = status.account_id || null;
  state.workspace = localStorage.getItem(copalStorageKey('openclank-copal-workspace')) || 'default';
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
  treeHouse?.loadState();
  connectEvents();
  // A launcher queued during scoped initialization owns the next open.
  if (initialNavigationTicket === editorNavigationTicket) {
    void openResolvedRoute(resolveAppletLocation(location.pathname, location.search));
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

/** Create an unbound image in the current authenticated Editor scope. */
export async function openBlankImage(options = {}) {
  const scope=Object.freeze({...saveScope()});
  if (!scope.accountId || !scope.storageNamespace) throw new Error('Image Editor account authority is unavailable.');
  const opening=open('notes',false), ticket=editorNavigationTicket; await opening;
  if (!sameSaveScope(scope,saveScope()) || ticket!==editorNavigationTicket || state.view!=='notes' || (options.isCurrent && !options.isCurrent())) throw new Error('The originating account or Editor view changed.');
  if (!notesFeature?.openBlankImage) throw new Error('Blank image Editor is unavailable.');
  return notesFeature.openBlankImage({name:options.name,presetSize:options.presetSize});
}

export async function openRetainedHostImage(resourceRef,{expectedRevision,isCurrent=()=>true}={}) {
  const scope=Object.freeze({...saveScope()});
  if (!scope.accountId || !scope.storageNamespace || !isCurrent() ||
      expectedRevision?.kind !== 'hostFingerprint' || typeof resourceRef !== 'string' || !resourceRef) throw new Error('Saved image authority is unavailable.');
  const opening=open('notes',false), ticket=editorNavigationTicket;
  await opening;
  const current=()=>sameSaveScope(scope,saveScope()) && ticket === editorNavigationTicket && state.view === 'notes' && isCurrent();
  if (!current() || !notesFeature?.openRetainedHostImage) throw new Error('The original account or Editor view changed.');
  return notesFeature.openRetainedHostImage(resourceRef,expectedRevision,current);
}

export async function openPendingAttachment(file,isCurrent,provenance) {
  const scope=Object.freeze({...saveScope()});
  if (!scope.accountId || !scope.storageNamespace || typeof isCurrent!=='function' || !isCurrent()) throw new Error('Attachment Editor authority is unavailable.');
  const opening=open('notes',false), ticket=editorNavigationTicket; await opening;
  if (!sameSaveScope(scope,saveScope()) || ticket!==editorNavigationTicket || state.view!=='notes' || !isCurrent()) throw new Error('The originating attachment changed.');
  return notesFeature.openPendingAttachment(file,isCurrent,provenance);
}

/** Create one blank image and deliver a captured Files gesture to its exact view. */
export async function openFilesImageInBlank(handoff) {
  const scope=Object.freeze({...saveScope()});
  if (!scope.accountId || !scope.storageNamespace || typeof handoff?.getContext !== 'function' || handoff?.context?.owner!==scope.accountId || handoff?.context?.workspace!==String(scope.workspace || 'default')) throw new Error('Files image handoff authority is unavailable.');
  const opening=open('notes',false), ticket=editorNavigationTicket; await opening;
  if (!sameSaveScope(scope,saveScope()) || ticket!==editorNavigationTicket || state.view!=='notes') throw new Error('The originating account or Editor view changed.');
  if (!notesFeature?.openFilesCanvasHandoff) throw new Error('Canvas image import is unavailable.');
  return notesFeature.openFilesCanvasHandoff(handoff);
}

/** Admit an exact Files image into the existing owner-scoped Editor workspace. */
export async function openImageResource(resourceRef, options = {}) {
  const externalCurrent=typeof options.isCurrent === 'function' ? options.isCurrent : () => true;
  return openScopedImageRef(resourceRef, {
    captureScope:saveScope, sameScope:sameSaveScope, expectedRevision:options.expectedRevision || null,
    activateEditor:async () => { const opening=open('notes',false), ticket=editorNavigationTicket; await opening; return ticket; },
    navigationCurrent:ticket => externalCurrent() && ticket===editorNavigationTicket && state.view==='notes',
    openImage:(ref,guard) => { if (!notesFeature?.openResourceRef) throw new Error('Image Editor exact-open is unavailable.'); return notesFeature.openResourceRef({resourceRef:ref,...guard}); },
  });
}

/** Open one journal page through an owner-bound opaque Files ref. */
export async function openSharedText({publicationID, name = 'Shared text', isCurrent = () => true} = {}) {
  const scope=saveScope(), ticket=++editorNavigationTicket;
  const current=()=>sameSaveScope(scope,saveScope()) && isCurrent();
  if (!scope.accountId || !current()) throw new Error('Shared text authority unavailable.');
  const makeClient=(format)=>createSharedTextClient({accountID:scope.accountId,publicationID,format,
    getScope:()=>current()?{accountID:scope.accountId,publicationID,epoch:scope.epoch}:null,
    exchange:async(phase,body)=>{
      const response=await fetch(`/api/files-v1/shared-${format==='workbook'?'workbook':format==='slides'?'slides':'text'}/${phase}`,{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
      if (!response.ok) throw new Error('Shared resource unavailable.');
      return response.json();
    }});
  let client=makeClient('text'), data=await client.read(), format='text';
  if (!current() || ticket!==editorNavigationTicket) return null;
  let workbook=false;try{parseWorkbook(data.text);workbook=true}catch{}
  if(workbook){
    const prior=data;client=makeClient('workbook');data=await client.read();
    if (!current() || ticket!==editorNavigationTicket) return null;
    if(data.resource_id!==prior.resource_id || data.role!==prior.role)throw Error('Shared workbook changed.');
    parseWorkbook(data.text);format='workbook';
  } else {
    let slides=false;try{parseSlideDeck(data.text);slides=true}catch{}
    if(slides){const prior=data;client=makeClient('slides');data=await client.read();
      if(!current()||ticket!==editorNavigationTicket)return null;
      if(data.resource_id!==prior.resource_id||data.role!==prior.role)throw Error('Shared slides changed.');
      parseSlideDeck(data.text);format='slides';}
  }
  const handle=sharedTextHandle({accountID:scope.accountId,publicationID,resourceID:data.resource_id,revision:data.revision,name,role:data.role});
  const adapter=data.role==='editor'?createSharedTextSaveAdapter({client,publicationID,initialText:data.text,initialRevision:data.revision,role:data.role,format}):null;
  const opening=open('notes',false), navigationTicket=editorNavigationTicket;
  await opening;
  if (!current() || navigationTicket!==editorNavigationTicket || state.view!=='notes' || !notesFeature?.openSharedText) return null;
  history.pushState({copal:'notes'},'',appletPath('editor'));
  const shared={adapter,isCurrent:current,format,publicationID,role:data.role};
  const opened=notesFeature.openSharedText(handle,{name,text:data.text},shared), id=opened.id;
  if (opened.shared!==shared) return id;
  const openedContext=state.windows.get('notes');
  openSharedTextStream({publicationID,isCurrent:()=>current() && Boolean(openedContext?.window?.body?.isConnected),readCurrent:()=>client.read(),onSnapshot:fresh=>{
    if (fresh.resource_id!==data.resource_id || fresh.role!==data.role || !current()) return;
    notesFeature.acceptSharedSnapshot(id,shared,fresh);
  }});
  return id;
}

/** Read a named shared image through its existing publication authority. */
export async function openSharedImage({publicationID, name='Shared image', isCurrent=()=>true}={}) {
  const scope=saveScope(), ticket=++editorNavigationTicket;
  const current=()=>sameSaveScope(scope,saveScope()) && isCurrent();
  if(!scope.accountId || !current()) throw Error('Shared image unavailable.');
  const response=await fetch('/api/files-v1/shared-image/read',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify({version:1,publication_id:publicationID})});
  if(!response.ok || !current() || ticket!==editorNavigationTicket) throw Error('Shared image unavailable.');
  const data=await response.json();
  if(!current() || ticket!==editorNavigationTicket) return null;
  const opening=open('notes',false), navigationTicket=editorNavigationTicket; await opening;
  if(!current() || navigationTicket!==editorNavigationTicket || state.view!=='notes') return null;
  const owner=state.windows.get('notes');
  const {openSharedImageFeedback}=await import('./editor/sharedImageFeedback.js');
  return openSharedImageFeedback({data,name,publicationID,isCurrent:()=>current() && Boolean(owner?.window?.body?.isConnected)});
}

export async function openResource(resourceRef, options = {}) {
  const guarded = typeof options.isCurrent === 'function' || typeof options.handoffCurrent === 'function';
  const isCurrent = options.isCurrent || (() => true), handoffCurrent = options.handoffCurrent || (() => true);
  if (!isCurrent()) return null;
  const {sourceLine,sourceRange} = options;
  const anchored = sourceLine !== undefined || sourceRange !== undefined;
  if (anchored && (options.expectedRevision?.kind !== 'hostFingerprint' || !guarded ||
      sourceLine !== undefined && (!Number.isSafeInteger(sourceLine) || sourceLine < 1) ||
      sourceRange !== undefined && (!sourceRange || !Number.isSafeInteger(sourceRange.startByte) ||
        !Number.isSafeInteger(sourceRange.endByte) || sourceRange.startByte < 0 || sourceRange.endByte <= sourceRange.startByte))) {
    throw new Error('A captured Host revision and valid source location are required.');
  }
  const requestTicket = ++editorNavigationTicket;
  const scope = saveScope();
  const response = await filesFacadeClient.openResource(resourceRef, { expectedRevision:options.expectedRevision || null, purpose:options.purpose || null });
  if (guarded && (!isCurrent() || requestTicket !== editorNavigationTicket)) return null;
  if (!sameSaveScope(scope, saveScope())) throw new Error('The active account changed. Open the file again.');
  if (!response?.resource?.ref || !response?.payload) {
    throw new Error('This Files resource cannot be opened in Editor');
  }
  // Files returns an authorized, provider-neutral ResourceHandle. Validate it
  // before entering the shared workspace so a rotating ref can never become a
  // buffer identity and a filename suffix cannot choose presentation mode.
  const payload = response.payload;
  if (response.target?.app === 'editor' && response.resource.provider === 'host' && payload.kind === 'host-image' && !anchored) {
    return openRetainedHostImage(response.resource.ref, {
      expectedRevision:response.resource.revision,
      isCurrent:() => sameSaveScope(scope, saveScope()) && isCurrent() && handoffCurrent(),
    });
  }
  const resource = normalizeResourceHandle(payload.resource);
  if (options.expectedRevision?.kind === 'copalHead' && (resource.key.provider !== 'copal' ||
      resource.key.resourceId !== response.resource.id || resource.revision.kind !== 'copalHead' ||
      resource.revision.value !== options.expectedRevision.value)) {
    throw new Error('The captured Journal revision has changed.');
  }
  if (anchored && (resource.key.provider !== 'host' ||
      resource.revision.kind !== options.expectedRevision.kind || resource.revision.value !== options.expectedRevision.value)) {
    throw new Error('The captured match revision has changed.');
  }
  const snapshotText = String(payload.text ?? payload.content ?? '');
  let sourceSelection;
  if (sourceRange !== undefined) {
    // The editor consumes UTF-16 offsets after line ending normalization. Only
    // convert when the actual decoded file payload matches the captured bytes.
    const bytes = new TextEncoder().encode(snapshotText);
    const digest = await crypto.subtle.digest('SHA-256', bytes);
    if (!isCurrent() || !sameSaveScope(scope, saveScope()) || requestTicket !== editorNavigationTicket) return null;
    const hash = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2,'0')).join('');
    if (options.expectedRevision.value !== `sha256:${hash}:${bytes.length}` || sourceRange.endByte > bytes.length)
      throw new Error('The captured source bytes have changed.');
    const lengthAt = offset => new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(bytes.subarray(0,offset)).replace(/\r\n?|\n/g,'\n').length;
    sourceSelection = {from:lengthAt(sourceRange.startByte),to:lengthAt(sourceRange.endByte)};
    if (sourceSelection.from >= sourceSelection.to) throw new Error('The captured source selection cannot be shown.');
  }
  const snapshot = snapshotEnvelope({
    key:resource.key,
    expectedRevision:resource.revision,
    envelope:{ text:snapshotText, ...(resource.metadata ? { metadata:resource.metadata } : {}) },
  });
  // Enter the existing Editor surface without putting either a legacy document
  // id or the opaque ResourceRef into browser history.
  if (!isCurrent()) return null;
  const opening = open('notes', false);
  const navigationTicket = editorNavigationTicket;
  await opening;
  if (!sameSaveScope(scope, saveScope()) || (guarded && (navigationTicket !== editorNavigationTicket || !handoffCurrent() || state.view !== 'notes'))) return null;
  if (resource.key.provider === 'copal' && !state.docs.some(doc =>
      sameResourceKey(doc.resource?.key, resource.key) && doc.resource.revision?.value === resource.revision.value)) {
    await loadDocuments(false);
    if (!sameSaveScope(scope, saveScope()) || (guarded && (navigationTicket !== editorNavigationTicket || !handoffCurrent() || state.view !== 'notes'))) return null;
  }
  history.pushState({ copal:'notes' }, '', appletPath('editor'));
  // Install the Files mutation bridge before opening the shared buffer. The
  // notes feature returns its active resource immediately, so a listener
  // placed after that return would never preserve a dirty tab's renamed path.
  installFilesMutationBridge();
  if (notesFeature?.openResource) {
    return notesFeature.openResource(resource, {
      ...payload, openedFilesResource:response.resource,
      recordingJobHint: guarded && response.resource?.format?.formatId === 'video' && /^[a-f0-9]{32}$/.test(String(options.recordingJobHint || '')) ? options.recordingJobHint : null,
      recordingEvidence:guarded && response.resource?.format?.formatId === 'video' && options.recordingEvidence?.resourceRef === resourceRef &&
        options.recordingEvidence.revision?.kind === resource.revision.kind && options.recordingEvidence.revision?.value === resource.revision.value ? options.recordingEvidence : null,
      name:String(payload.name || response.resource.name || resource.locator.displayName || 'Untitled'),
      text:snapshot.envelope.text,
      ...(anchored ? {
        matchedSourceLine: sourceLine,
        matchedSourceRange: sourceSelection,
        matchedSourceCurrent: () => handoffCurrent() && navigationTicket === editorNavigationTicket && sameSaveScope(scope, saveScope()),
      } : {}),
      snapshot,
      resourceRef,
  });
}
  if (anchored) throw new Error('The captured match requires the Editor source view.');
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
  const current = getViewContext(state.view);
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
  if (!actionId || !['notes', 'wiki'].includes(corpus)) throw new Error('Invalid document creation request.');
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
    const current = getViewContext(state.view);
    if (current?.noteDrafts?.size || [...(current?.noteBuffers?.values() || [])].some(buffer => buffer.state().dirty)) {
      featureFor(state.view)?.persistRecovery?.();
      throw new Error('The Editor has unsaved changes. Save explicitly before sharing its saved document with the agent.');
    }
  }
  return true;
}

window.__odysseusGetActiveCopalContext = getActiveAgentContext;
window.__odysseusFlushActiveCopalResource = flushActiveAgentResource;

export default { init, open, openResolvedRoute, close, manageJournals, openResource, openImageResource, openBlankImage, openRetainedHostImage, openPendingAttachment, openFilesImageInBlank, openCodeGraphBinding, getNotesSettings, updateNotesSettings, customizeExplorer, getNotesPanels, updateNotesPanel, getFilesMutationBridgeInstallations, getActiveAgentContext, flushActiveAgentResource, whenReady, getAppearanceEntries, getEntryVisibility, updateEntryVisibility, setAllEntryVisibility, resetEntryVisibility };
