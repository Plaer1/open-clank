import { createExplorerLayout, showExplorerCustomization } from './editor/explorerLayout.js';
import { isChatResource, dispatchFilesDestination, destinationLabel } from './copal/resourceDestinations.js';
import { acknowledgeVisible } from './achievementProducer.js';
import { filesServiceClient } from './filesServiceClient.js';
import { filesFacadeClient, transferRootsToAsk } from './filesFacadeClient.js';
import { createOpenClankWindow } from './copal/windows.js';
import { fileIcon, fileIconKey, glyphIcon } from './langIcons.js';
import { createResizablePane } from './editor/resizablePane.js';
import { createExplorerTree } from './editor/explorerTree.js';
import { createThumbnailLoader, thumbnailCacheKey } from './editor/thumbnailLoader.js';
import { mountImageRegion } from './graphics/imageRegion.js';
import { markTrustedJpegRepresentation } from './graphics/mediaImageAuthority.js';
import {
  SORT_KEYS,
  constrainSortSpec,
  normalizeSortKeys,
  sortEntries as sortEntryList,
  normalizeSortSpec,
} from './editor/entryModel.js';
import { isPreviewable, previewKind, previewMime, readBoundedTextResponse, readIwiPreview, isMetadataInspection, metadataFormatId, metadataDetails, metadataLabel } from './editor/previewModel.js';
import { acquirePreviewDependencies, createDependencyAcquisitionGuard, isDirectPreviewChild } from './browser/dependencyManifest.js';
import uiModule from './ui.js';
import * as Modals from './modalManager.js';
import {
  createFilesSelectionModel,
  resourceKey,
  rectangleSelection,
  transferIntent,
  buildInternalDragPayload,
  parseInternalDragPayload,
  validateDropTarget,
  FILES_TRANSFER_MIME,
  FILES_MAX_DRAG_ITEMS,
  scopeKey,
} from './filesSelectionModel.js';
import { createWindowNavigation } from './copal/navigation.js';
import { createFilesCanvasImagePayload, FILES_CANVAS_IMAGE_MIME } from './editor/clipboard-and-drop.js';
import { openFolderHistory, openResourceHistory } from './historyView.js';
import { openGitWorkbenchWindow } from './gitWorkbench.js';
import { openContextActionMenu, registerAdapter } from './custom-context-menu.js';
import { CODE_GRAPH_LABEL, graphDisabledReason, executeCodeGraphNavigation } from './copal/resourceGraphCommands.js';
import { safeExternalDropSegment, safeExternalDropPath, applyRectangleSelection } from './filesDropModel.js';
import {
  fileResourceIsDirectory as isDirectory, fileResourceIsTextual as isTextualEntry, fileResourceCanOpen,
  fileResourceCanOpenInEditor, fileResourceCanOpenCodeGraph, fileResourceCanShare, fileResourceCanBrowseHostFolder,
  fileResourceCanReveal, fileResourceCanCopyPath, fileResourceCopyPath,
} from './filesResourceActions.js';

const FILES_FAVORITES_KEY = 'openclank-files-favorites';
const FILES_VIEW_PREFS_KEY = 'openclank-files-view-preferences';

const FILE_SORT_OPTIONS = Object.freeze([
  ['name:asc', 'Name ↑'],
  ['name:desc', 'Name ↓'],
  ['kind:asc', 'Type ↑'],
  ['kind:desc', 'Type ↓'],
  ['modified:asc', 'Modified ↑'],
  ['modified:desc', 'Modified ↓'],
  ['size:asc', 'Size ↑'],
  ['size:desc', 'Size ↓'],
]);
const FILE_DETAIL_COLUMNS = Object.freeze([
  ['name', 'Name'],
  ['kind', 'Type'],
  ['size', 'Size'],
  ['modified', 'Modified'],
]);
const DEFAULT_FILES_MODE = 'details';
const DEFAULT_FILES_SORT = Object.freeze({
  key: 'name',
  direction: 'asc',
  directoriesFirst: true,
  collation: 'open-clank-v1',
});
const MAX_EXTERNAL_DROP_ENTRIES = FILES_MAX_DRAG_ITEMS * 5;
const MAX_EXTERNAL_DROP_DEPTH = 16;
const FILES_READ_RETRY_DELAYS = [1500, 4000, 10000, 20000];
const CLANKER_HOME_PROVIDER = 'clanker';
const LIBRARY_COLLECTION_VIEWS = Object.freeze({
  documents: 'documents:active',
  published: 'published',
  chats: 'chats:active',
  research: 'research:active',
  archive: 'archive',
});
const CLANKER_COLLECTION_LABELS = Object.freeze({
  'documents:active': 'Documents',
  published: 'Downloads',
  'chats:active': 'Chats',
  'research:active': 'Research',
  archive: 'Archive',
});

const browsers = new Map();
let browserSequence = 0;
let activeBrowser = null;
let filesClipboard = null;

export function createFilesBrowser(options = {}) {
  if (!document.querySelector('[data-files-workbench-style]')) { const link = document.createElement('link'); link.rel = 'stylesheet'; link.href = new URL('../css/filesWorkbench.css', import.meta.url).href; link.dataset.filesWorkbenchStyle = ''; document.head.append(link); }
  let localMenu = null;
  let browserResize = null;
  const readRecoveries = new Map();
  let activeReadRetry = null;
  let readRetryTimer = null;
  let readRetryAttempt = 0;
  const id = options.id || `files-window-${++browserSequence}`;
  const hooks = {};
  const listeners = new AbortController();
  const listen = (target, type, callback, config = {}) => target.addEventListener(type, callback, { ...(typeof config === 'boolean' ? { capture: config } : config), signal: listeners.signal });
const state = {
  shell: null,
  nativeWindow: null,
  path: '',
  hostPath: '',
  entries: [],
  nextCursor: null,
  mode: DEFAULT_FILES_MODE,
  smallIconSize: 20,
  largeIconSize: 64,
  provider: CLANKER_HOME_PROVIDER,
  selected: new Set(),
  contentGeneration: 0,
  contentController: null,
  lifecycleGeneration: 0,
  revealGeneration: 0,
  owner: '',
  accountId: '',
  defaultPath: '',
  navigationGeneration: null,
  navigationController: null,
  favoritesController: null,
  workspaceController: null,
  navigationRoots: [],
  explorerTree: null,
  favorites: [],
  managedRootPicker: null,
  libraryImportCapture: null,
  workspaces: [],
  currentWorkspaceId: '',
  favoritePaths: new Set(),
  sort: normalizeSortSpec(DEFAULT_FILES_SORT),
  columns: [],
  managedRoots: [],
  pane: null,
  previewController: null,
  previewHandle: null,
  viewPreferences: {},
  thumbnailObserver: null,
  thumbnailLoader: null,
  searchQuery: '',
  watchController: null,
  watchResourceRef: '',
  watchEpoch: 0,
  watchRetryRef: '',
  watchFailures: 0,
  watchRefreshTimer: null,
  selectionModel: null,
  selectionModels: new Map(),
  activeColumnIndex: -1,
  pendingKeepBoth: null,
  cleanupEpoch: 0,
  pendingImport: null,
  importControllers: new Set(),
  importCapabilities: null,
  gesture: null,
  navigation: null,
  historyRestore: null,
  renderEpoch: 0,
  contextAdapterDispose: null,
};

const explorerLayout = createExplorerLayout({
  getScope:() => options.getLayoutScope?.() || {owner:state.owner,workspace:state.currentWorkspaceId || copalWorkspace(),surface:options.surface || 'files'},
  onApplied:() => filterTree(state.treeFilter),
});
let categoryObserver = null;
const collectionCollapseApplied=new Map();
function syncCollectionLayout(tree = state.collectionTree) {
  if (!tree?.container) return;
  for (const node of tree.snapshot().nodes.filter(item => item.parentId == null)) {
    const provider = node.data?.provider;
    if (!['copal','library','files'].includes(provider)) continue;
    const id = `collection:${provider}`;
    const find = () => [...tree.container.querySelectorAll('[data-explorer-id]')].find(item => item.dataset.explorerId === node.id);
    const label = {copal:'Workspace collections',library:'Library · documents, research and downloads',files:'Gallery collections'}[provider];
    explorerLayout.register({id,label,group:'application-collections',element:find,
      content:() => find()?.querySelector(':scope > [role="group"]'),
      getCollapsed:() => tree.getNode(node.id)?.expanded !== true,
      setCollapsed:(collapsed, configured) => {
        const current = tree.getNode(node.id);
        if (!configured || !current || current.loading) return;
        const prior=collectionCollapseApplied.get(id);
        if(prior?.tree===tree && prior.collapsed===collapsed)return;
        collectionCollapseApplied.set(id,{tree,collapsed});
        if(current.expanded===!collapsed)return;
        if (collapsed) tree.collapse(node.id); else void tree.expand(node.id);
      },
      reset:() => { if(tree.getNode(node.id)?.expanded)tree.collapse(node.id); },
    });
  }
}
function customizeExplorer() { return showExplorerCustomization([explorerLayout], {title:'Customize Files Explorer'}); }

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
  return node;
}

function displayName(path) {
  const parts = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.at(-1) || path || '/';
}

function parentPath(path) {
  const original = String(path || '').replace(/\\/g, '/');
  if (original === '/') return '/';
  if (/^[A-Za-z]:\/?$/.test(original)) return original.slice(0, 2) + '/';
  const normalized = original.replace(/\/$/, '');
  const index = normalized.lastIndexOf('/');
  if (index > 0) return normalized.slice(0, index);
  return normalized.startsWith('/') ? '/' : normalized;
}

function childPath(directory, name) {
  const base = String(directory || '').replace(/\\/g, '/').replace(/\/$/, '');
  const child = String(name || '').replace(/^[/\\]+/, '');
  if (!base || base === '/') return '/' + child;
  return base + '/' + child;
}

function editorResourceAllowed(resource) { return options.surface !== 'editor' || !isChatResource(resource); }
function confirmPickerEntry(entry) {
  if (!editorResourceAllowed(entry)) { setStatus('Chats belong in Chats or Library, outside Editor.', true); return false; }
  const intent = state.openIntent || 'current'; state.openIntent = 'current';
  return options.onConfirm?.(entry, { intent });
}

function facadeEntry(resource) {
  const capabilities = Array.isArray(resource?.capabilities) ? [...resource.capabilities] : [];
  const entry = {
    ...resource,
    resource_ref: String(resource?.ref || resource?.resource_ref || ''),
    resource_id: String(resource?.id || resource?.resource_id || ''),
    provider: String(resource?.provider || ''),
    capabilities,
    name: String(resource?.name || 'Untitled'),
    media_type: String(resource?.mime_type || ''),
    sort_kind: String(resource?.sort_kind || resource?.preview_kind || resource?.mime_type || resource?.kind || 'file'),
    sort_keys: normalizeSortKeys(resource?.sort_keys),
    navigationRole: resource?.navigationRole || resource?.navigation_role || ({ 'documents:active':'documents',published:'download','chats:active':'chat','research:active':'research',archive:'archive' }[resource?.provenance?.view]) || (String(resource?.kind || '').includes('root') ? ({host:'locations',copal:'workspace',files:'gallery',library:'library'}[resource?.provider]) : undefined),
  };
  if (capabilities.includes('download')) {
    entry.download_url = filesFacadeClient.contentUrl(entry.resource_ref, { purpose: 'download' });
  }
  const iwiPreview = entry.format?.formatId === 'iwi' && entry.format?.operations?.preview?.available === true;
  if (iwiPreview) entry.preview_kind = 'image';
  if (capabilities.includes('preview') && entry.preview_kind) {
    const expectedRevision = iwiPreview ? entry.format?.providerRevision : null;
    if (!iwiPreview || expectedRevision) {
      entry.preview_url = filesFacadeClient.contentUrl(entry.resource_ref, { purpose:'preview', expectedRevision });
      if (entry.preview_kind === 'image') entry.thumbnail_url = iwiPreview
        ? filesFacadeClient.thumbnailUrl(entry.resource_ref, { width:192, height:192, expectedRevision }) : entry.preview_url;
    }
  }
  if (!iwiPreview && entry.provider === 'host' && capabilities.includes('preview') && entry.native_thumbnail_available !== false) {
    // macOS Quick Look supplies content pixels only. The request is lazy and
    // failures retain the universal Open Clank glyph already in the tile.
    entry.native_thumbnail_url = filesFacadeClient.thumbnailUrl(entry.resource_ref, {
      width: 192,
      height: 192,
      scale: Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1)),
    });
  }
  if (entry.provider === 'host' && capabilities.includes('preview') && entry.native_icon_available === true) {
    entry.native_icon_url = filesFacadeClient.thumbnailUrl(entry.resource_ref, {
      width: 64, height: 64, icon: true,
      scale: Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1)),
    });
  }
  return entry;
}

function managedLabel() {
  if (!state.columns.length) {
    if (state.provider === CLANKER_HOME_PROVIDER) return 'Clanker home';
    return state.provider === 'all' ? 'All sources' : state.provider;
  }
  const path = state.columns.map((column) => column.name || displayName(column.path)).join(' › ');
  return state.searchQuery ? `${path} · Search: ${state.searchQuery}` : path;
}

function activeColumn() {
  if (state.mode === 'columns' && state.activeColumnIndex >= 0) return state.columns[state.activeColumnIndex] || state.columns.at(-1) || null;
  return state.columns.at(-1) || null;
}

function usesFacadeListing() {
  return Boolean(activeColumn()?.resourceRef);
}

function isFacadeEntry(entry) {
  return Boolean(entry?.resource_ref || entry?.ref);
}

function sortEntries(entries, spec = state.sort) {
  // Rust emits `modified_unix_ms`; managed providers use one of the older
  // spellings. Keep the pure presentation sorter on one normalized field so
  // a server-sorted page is not accidentally reordered by name in the UI.
  const normalized = (entries || []).map((entry) => entry?.modified == null && entry?.modified_ms == null && entry?.mtime == null && entry?.modified_unix_ms != null
    ? { ...entry, modified: entry.modified_unix_ms }
    : entry);
  return sortEntryList(normalized, spec);
}

function requestSortSpec(spec = state.sort) {
  const normalized = normalizeSortSpec(spec);
  return {
    key: normalized.key,
    direction: normalized.direction,
    directories_first: normalized.directoriesFirst,
    collation: normalized.collation,
  };
}

function sortKeysFor(value) {
  return normalizeSortKeys(Array.isArray(value) && value.length ? value : SORT_KEYS);
}

function appendSortOptions(select, supportedKeys = SORT_KEYS) {
  const supported = new Set(sortKeysFor(supportedKeys));
  for (const [value, label] of FILE_SORT_OPTIONS) {
    const [key] = value.split(':');
    select.append(el('option', {
      value,
      text: label,
      disabled: supported.has(key) ? null : 'disabled',
      title: supported.has(key) ? null : 'This folder does not expose that metadata for sorting',
    }));
  }
  return select;
}

function updateSortSelect(select, spec, supportedKeys = SORT_KEYS) {
  if (!select) return;
  const supported = new Set(sortKeysFor(supportedKeys));
  for (const option of select.options) {
    const [key] = option.value.split(':');
    option.disabled = !supported.has(key);
    option.title = option.disabled ? 'This folder does not expose that metadata for sorting' : '';
  }
  const applied = constrainSortSpec(spec, [...supported]);
  select.value = `${applied.key}:${applied.direction}`;
}

function activeSortKeys() {
  return sortKeysFor(activeColumn()?.sortKeys || SORT_KEYS);
}

function syncPrimarySortControls() {
  updateSortSelect(state.shell?.querySelector('.files-sort-select'), state.sort, activeSortKeys());
  updateFoldersFirstControl(state.shell?.querySelector('[data-files-folders-first]'), state.sort);
}

function updateFoldersFirstControl(control, spec = state.sort) {
  if (!control) return;
  const active = normalizeSortSpec(spec).directoriesFirst;
  control.classList.toggle('active', active);
  control.setAttribute('aria-pressed', active ? 'true' : 'false');
  control.title = active ? 'Folders first is on' : 'Folders first is off';
  control.setAttribute('aria-label', control.title);
}

function iconNode(descriptor, { size = 16, className = 'files-glyph', open = false } = {}) {
  const normalized = { ...(descriptor || {}), open, mimeType: descriptor?.mimeType || descriptor?.mime_type || descriptor?.media_type || '' };
  const key = fileIconKey(normalized);
  const node = el('span', { class: className, 'data-file-glyph': key, 'aria-hidden': 'true' });
  node.innerHTML = fileIcon(normalized, size, { className: 'files-glyph-svg' });
  return node;
}

function namedGlyph(name, { size = 16, className = 'files-glyph' } = {}) {
  const node = el('span', { class: className, 'data-file-glyph': name, 'aria-hidden': 'true' });
  node.innerHTML = glyphIcon(name, size, { className: 'files-glyph-svg' });
  return node;
}

function iconButton({ glyph, text = '', title, className = 'files-toolbar-button', onClick }) {
  const button = el('button', { type: 'button', class: className, title, 'aria-label': title });
  button.append(namedGlyph(glyph, { size: 15, className: 'files-button-glyph' }));
  if (text) button.append(el('span', { text }));
  if (onClick) button.addEventListener('click', onClick);
  return button;
}

function copalWorkspace() {
  // Copal owns the workspace selector.  The public context hook is preferred
  // when its window is mounted; the local key is only a display preference
  // fallback and never grants host access.
  const context = typeof window.__odysseusGetActiveCopalContext === 'function'
    ? window.__odysseusGetActiveCopalContext()
    : null;
  const workspace = String(context?.workspace || localStorage.getItem('openclank-copal-workspace') || 'default').trim();
  return /^[A-Za-z0-9._-]{1,64}$/.test(workspace) ? workspace : 'default';
}

function setStatus(message, bad = false, { keepUndo = false } = {}) {
  const paneStatus = state.shell?.querySelector('.files-pane-status');
  // Only the passive item-count line may coexist with a pending trash Undo; any other message ends it.
  if (!keepUndo) state.pendingUndo = null;
  else if (state.pendingUndo && state.pendingUndo.path !== state.path) state.pendingUndo = null;
  if (paneStatus) {
    paneStatus.textContent = message || ''; paneStatus.classList.toggle('error', !!bad);
    if (keepUndo && state.pendingUndo) paneStatus.append(' ', state.pendingUndo.button);
  }
  const split = splits.get(id);
  if (!split || activeBrowser === api) state.nativeWindow?.setStatus(message, bad);
}

function filesReadErrorMessage(error, label) {
  if (error?.status === 401 || error?.code === 'sign_in_required') return 'Your sign-in expired. Sign in again to open Files.';
  if (error?.status === 403 || error?.code === 'permission_denied') return `Access to ${label} was refused.`;
  if (error?.code === 'read_timeout') return `The host did not answer in time while reading ${label}.`;
  if (error?.code === 'transport_unavailable') return `Could not connect to the host to read ${label}.`;
  if (error?.status >= 500 || error?.code === 'provider_unavailable') return `The host or its file helper could not read ${label}.`;
  if (error?.status === 404 || error?.code === 'resource_unavailable') return `The folder for ${label} could not be opened. It may have moved or be unavailable to the host OS.`;
  return error?.message || `${label} could not be loaded.`;
}

function clearFilesReadRecovery(kind) {
  if (!kind) {
    for (const key of readRecoveries.keys()) clearFilesReadRecovery(key);
    clearTimeout(readRetryTimer);
    readRetryTimer = null;
    return;
  }
  const recovery = readRecoveries.get(kind);
  recovery?.notice.remove();
  readRecoveries.delete(kind);
}

function nextFilesReadRecovery({automatic = false} = {}) {
  return ['roots','folder','navigation','library','watch'].find(kind => {
    const recovery = readRecoveries.get(kind);
    return recovery?.current() && (!automatic || recovery.automatic);
  });
}

function retryableFilesRead(error) {
  if (confirmedFilesPolicyFailure(error) || error?.code === 'sign_in_required') return false;
  return ['read_timeout','transport_unavailable','provider_unavailable','root_unavailable','policy_generation_changed','resource_ref_stale'].includes(error?.code)
    || [500, 502, 503, 504].includes(error?.status);
}

function resetFilesReadRetry() {
  clearTimeout(readRetryTimer);
  readRetryTimer = null;
  readRetryAttempt = 0;
}

function scheduleFilesReadRetry() {
  if (readRetryTimer != null || activeReadRetry || document.hidden || !state.nativeWindow?.visible) return;
  const kind = nextFilesReadRecovery({automatic:true});
  if (!kind) {
    if (!nextFilesReadRecovery()) resetFilesReadRetry();
    return;
  }
  const attempt = kind === 'watch' ? Math.max(0, state.watchFailures - 1) : readRetryAttempt;
  if (attempt >= FILES_READ_RETRY_DELAYS.length) return;
  readRetryTimer = setTimeout(() => {
    readRetryTimer = null;
    if (document.hidden || !state.nativeWindow?.visible) return;
    const next = nextFilesReadRecovery({automatic:true});
    if (!next) return;
    if (next !== 'watch') readRetryAttempt += 1;
    void retryFilesRead(next);
  }, FILES_READ_RETRY_DELAYS[attempt]);
}

function retryFilesRead(kind) {
  const recovery = readRecoveries.get(kind);
  if (!recovery || !recovery.current()) return;
  if (activeReadRetry) return activeReadRetry;
  clearTimeout(readRetryTimer);
  readRetryTimer = null;
  recovery.button.disabled = true;
  activeReadRetry = Promise.resolve().then(() => recovery.current() && recovery.retry()).catch(error => {
    if (readRecoveries.get(kind) !== recovery || !recovery.current()) return;
    recovery.message.textContent = filesReadErrorMessage(error, recovery.label);
    recovery.error = error;
    recovery.automatic = retryableFilesRead(error);
  }).finally(() => {
    activeReadRetry = null;
    if (readRecoveries.get(kind) === recovery) recovery.button.disabled = false;
    scheduleFilesReadRetry();
  });
  return activeReadRetry;
}

function showFilesReadRetry(kind, label, error, retry) {
  clearFilesReadRecovery(kind);
  const account = String(document.body?.dataset?.accountId || '');
  const owner = state.owner;
  const workspace = state.currentWorkspaceId, copal = copalWorkspace(), lifecycle = state.lifecycleGeneration;
  const intentGeneration = state.contentGeneration;
  const current = () => state.nativeWindow?.visible === true && !listeners.signal.aborted
    && account === String(document.body?.dataset?.accountId || '') && owner === state.owner && workspace === state.currentWorkspaceId
    && copal === copalWorkspace() && lifecycle === state.lifecycleGeneration
    && (['navigation','roots'].includes(kind) || intentGeneration === state.contentGeneration);
  const text = filesReadErrorMessage(error, label);
  const message = el('span',{text});
  const button = el('button',{type:'button',class:'files-toolbar-button',text:'Retry'});
  button.addEventListener('click', () => retryFilesRead(kind));
  const notice = el('div',{class:'files-error',role:'status'},message,' ',button);
  const inSidebar = kind === 'navigation' || options.navigationOnly;
  const parent = inSidebar ? state.sidebar : state.shell?.querySelector('[data-files-body]');
  if (!parent) return;
  if (inSidebar) parent.append(notice);
  else parent.before(notice);
  readRecoveries.set(kind,{current,retry,notice,message,button,label,error,automatic:retryableFilesRead(error)});
  if (['folder','roots','watch'].includes(kind)) setStatus(text,true);
  scheduleFilesReadRetry();
}

function suspendFilesNavigation() {
  destroyExplorerTree();
  state.navigationRoots = [];
  state.managedRoots = [];
  state.favorites = [];
  state.workspaces = [];
  state.favoritePaths.clear();
  state.navigationGeneration = null;
  state.importCapabilities = null;
  renderFavorites();
  renderWorkspaces();
  const add = state.shell?.querySelector('[data-files-add-host]');
  if (add) { add.hidden = true; add.disabled = true; }
  const sidebar = state.sidebar?.querySelector('[data-files-navigation-status]');
  if (sidebar) sidebar.textContent = '';
}

function clearKeepBothRetry() {
  state.pendingKeepBoth = null;
  state.shell?.querySelector('.files-retry-keep-both')?.remove();
  state.cleanupEpoch += 1;
  state.shell?.querySelector('.files-finish-cleanup')?.remove();
  state.shell?.querySelector('.files-undo-move')?.remove();
}

/** "Undo move" for a completed cross-root file move, next to "Finish cleanup". Refusals are shown in the server's plain words. */
function addUndoMoveButton(operation, itemId, current) {
  const toolbar = state.shell?.querySelector('.files-toolbar');
  if (!toolbar || toolbar.querySelector('.files-undo-move')) return;
  const button = el('button', { type:'button', class:'files-toolbar-button files-undo-move', title:'Move the file and its media back to where they were', text:'Undo move' });
  button.addEventListener('click', async () => {
    if (!current()) return;
    button.disabled = true;
    try {
      const result = await filesFacadeClient.undoMove(operation, itemId);
      if (!current()) return;
      if (result.undo === 'committed') {
        button.remove();
        setStatus('Move undone.');
        if (usesFacadeListing()) await refreshBrowser({ hierarchyProvider: null });
      } else {
        button.disabled = false;
        setStatus('The undo is still finishing. Try again in a moment.', true);
      }
    } catch (error) {
      if (current()) { button.disabled = false; setStatus(error?.message || 'The move could not be undone.', true); }
    }
  });
  toolbar.append(button);
}

async function offerFinishCleanup(operation, generation, owner, lifecycle) {
  const epoch = ++state.cleanupEpoch;
  state.shell?.querySelector('.files-finish-cleanup')?.remove();
  state.shell?.querySelector('.files-undo-move')?.remove();
  const current = () => epoch === state.cleanupEpoch && owner === state.owner
    && generation === filesPolicyGeneration() && lifecycle === state.lifecycleGeneration;
  try {
    const receipt = await filesFacadeClient.operationReceipt(operation, { generation });
    if (!current()) return;
    const undoable = receipt.items.find(item => item.outcome === 'committed' && item.undo === 'available');
    if (undoable) addUndoMoveButton(operation, undoable.item_id, current);
    const pending = receipt.items.find(item => item.outcome === 'committed' && item.cleanup === 'pending');
    if (!pending) return;
    const itemId = pending.item_id;
    const toolbar = state.shell?.querySelector('.files-toolbar');
    if (!toolbar) return;
    const button = el('button', { type:'button', class:'files-toolbar-button files-finish-cleanup', title:'Finish cleanup for the completed move', text:'Finish cleanup' });
    button.addEventListener('click', async () => {
      if (!current()) return;
      button.disabled = true;
      try {
        const before = await filesFacadeClient.operationReceipt(operation, { generation });
        if (!current()) return;
        const exact = before.items.find(item => item.item_id === itemId && item.outcome === 'committed');
        if (!exact || exact.cleanup !== 'pending') {
          button.remove();
          if (exact?.cleanup === 'complete') setStatus('Move completed; cleanup is complete.');
          return;
        }
        try {
          await filesFacadeClient.finishFolderCleanup(operation, itemId);
        } catch (error) {
          if (error?.code !== 'operation_pending' || !current()) throw error;
          const {createResourcePicker} = await import('./copal/resourcePicker.js');
          if (!current()) return;
          const selectedRoot = await new Promise(resolve => {
            let selected = null;
            state.managedRootPicker?.destroy();
            const picker = createResourcePicker({client:filesFacadeClient, purpose:'folder', accountScope:owner,
              workspaceScope:copalWorkspace(), generation, originWindow:state.nativeWindow,
              getGeneration:()=>filesPolicyGeneration(), getAccountScope:()=>state.owner, getWorkspaceScope:()=>copalWorkspace(),
              isOriginCurrent:current, rootLabel:'Choose the selected folder for cleanup',
              isCompatible:resource=>resource.provider === 'host' && resource.capabilities.write ? '' : 'Choose a writable Host folder.',
              onSelect:resource=>{ selected=resource; return true; },
              onClose:cancelled=>{ if (state.managedRootPicker === picker) state.managedRootPicker=null; resolve(cancelled ? null : selected); }});
            state.managedRootPicker=picker; void picker.open();
          });
          if (!selectedRoot || !current()) { if (current()) button.disabled=false; return; }
          if (!selectedRoot.revision) throw new Error('Folder revision is unavailable.');
          await filesFacadeClient.finishFolderCleanup(operation, itemId, {managedRoot:selectedRoot});
        }
        const after = await filesFacadeClient.operationReceipt(operation, { generation });
        if (!current()) return;
        const result = after.items.find(item => item.item_id === itemId && item.outcome === 'committed');
        if (result?.cleanup === 'complete') {
          button.remove();
          setStatus('Move completed; cleanup is complete.');
          void offerFinishCleanup(operation, generation, owner, lifecycle);
        } else {
          button.disabled = false;
          setStatus('Move completed; cleanup is still pending.', true);
        }
      } catch (_) {
        if (current()) { button.disabled = false; setStatus('Move completed; cleanup could not be confirmed.', true); }
      }
    });
    toolbar.append(button);
    setStatus('Move completed; cleanup is pending.');
  } catch (_) {
    // An unavailable or stale receipt is not authority to offer cleanup.
  }
}

function offerKeepBothRetry(payload, destination, response) {
  const items = Array.isArray(response?.items) ? response.items : [];
  const collision = items.some((item) => {
    const outcome = String(item?.outcome || item?.status || '').toLowerCase();
    const code = String(item?.code || item?.error_code || item?.reason || '').toLowerCase();
    return outcome.includes('conflict') || outcome.includes('collision') || code.includes('conflict') || code.includes('collision');
  });
  if (!collision) return;
  clearKeepBothRetry();
  const sourceModel = browsers.get(payload.pane)?.getTransferModel(payload);
  if (sourceModel) payload = { ...payload, selection_epoch:sourceModel.epoch };
  state.pendingKeepBoth = { payload, destination, response };
  const toolbar = state.shell?.querySelector('.files-toolbar');
  if (!toolbar) return;
  const button = el('button', { type: 'button', class: 'files-toolbar-button files-retry-keep-both', title: 'Retry failed items with renamed destination names', text: 'Keep Both' });
  button.addEventListener('click', async () => {
    const pending = state.pendingKeepBoth;
    if (!pending) return;
    button.disabled = true;
    const result = await hooks.__openClankFilesRetryKeepBoth(pending.payload, pending.destination, pending.response);
    if ((result?.items || []).every((item) => ['committed', 'unchanged'].includes(item?.outcome))) clearKeepBothRetry();
    else button.disabled = false;
  });
  toolbar.append(button);
}

function filesHistoryDescriptor() {
  const active = activeColumn();
  const managed = Boolean(active?.resourceRef);
  const body = state.shell?.querySelector('[data-files-body]');
  return {
    scope: { account: state.owner, workspace: copalWorkspace() },
    provider: state.provider,
    // Managed history is keyed by the sealed folder ref. Any label is for
    // display only; never persist a path-shaped authority beside the ref.
    label: managed ? managedLabel() : String(state.path || ''),
    path: managed ? '' : state.path,
    hostPath: managed ? '' : state.hostPath,
    resourceId: String(active?.resourceId || ''), resourceRef: String(active?.resourceRef || ''),
    query: state.searchQuery, mode: state.mode, sort: { ...state.sort },
    selection: [...state.selected].slice(0, 5001), scrollTop: Number(body?.scrollTop || 0),
  };
}

function commitFilesHistory() {
  if (!state.navigation || !state.owner || state.historyRestore) return;
  state.navigation.commit(filesHistoryDescriptor());
}

async function restoreFilesHistory(descriptor) {
  if (!descriptor || descriptor.scope?.account !== state.owner) return false;
  state.historyRestore = descriptor;
  let restored = false;
  try {
    if (descriptor.provider === 'host' && descriptor.hostPath && !descriptor.resourceRef) {
      setStatus('This saved Files location has expired; choose it again from the authorized roots.', true);
      return false;
    } else if (descriptor.resourceRef) {
      const entry = [...state.managedRoots, ...state.entries].find((item) => String(item?.resource_ref || '') === descriptor.resourceRef);
      if (entry && isDirectory(entry)) restored = await openManagedDirectory(entry, null, { history: false });
      else restored = await openProvider(descriptor.provider, { history: false });
    } else restored = await openProvider(descriptor.provider, { history: false });
    if (restored) {
      const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
      const restoredKeys = (descriptor.selection || []).filter((key) => state.entries.some((entry) => entrySelectionKey(entry) === key));
      model.replaceSelection(restoredKeys);
      state.selected = new Set(model.selectedKeys());
      renderEntries();
      const body = state.shell?.querySelector('[data-files-body]');
      if (body) body.scrollTop = Math.max(0, Number(descriptor.scrollTop) || 0);
    }
    return restored;
  } finally { state.historyRestore = null; }
}

function formatBytes(value) {
  const size = Number(value || 0);
  if (!Number.isFinite(size) || size < 1024) return `${size || 0} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let amount = size;
  let index = 0;
  let unit = units[index];
  while (amount >= 1024 && index < units.length) {
    amount /= 1024;
    unit = units[index] || units.at(-1);
    index += 1;
  }
  return `${amount.toFixed(amount >= 10 ? 0 : 1)} ${unit}`;
}

function favoritesStorageKey() {
  return state.owner ? FILES_FAVORITES_KEY + ':' + encodeURIComponent(state.owner) : '';
}

function readFavoritePaths() {
  try {
    const key = favoritesStorageKey();
    const parsed = key ? JSON.parse(localStorage.getItem(key) || '[]') : [];
    return Array.isArray(parsed)
      ? parsed.filter((path) => typeof path === 'string' && path.trim())
      : [];
  } catch { return []; }
}

function saveFavoritePaths() {
  try {
    const key = favoritesStorageKey();
    if (key) localStorage.setItem(key, JSON.stringify([...state.favoritePaths]));
  } catch (_) { /* Favorites are optional owner-scoped UI state, never authority. */ }
}

function viewPreferencesStorageKey() {
  return state.owner ? `${FILES_VIEW_PREFS_KEY}:${encodeURIComponent(state.owner)}` : '';
}

function readViewPreferences() {
  try {
    const key = viewPreferencesStorageKey();
    const value = key ? JSON.parse(localStorage.getItem(key) || '{}') : {};
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {};
  } catch { return {}; }
}

function saveViewPreferences() {
  try {
    const key = viewPreferencesStorageKey();
    if (key) {
      const latest = readViewPreferences();
      latest[state.provider] = state.viewPreferences[state.provider];
      state.viewPreferences = latest;
      localStorage.setItem(key, JSON.stringify(latest));
    }
  } catch (_) { /* presentation preferences are optional */ }
}

function applyViewPreferences(provider = state.provider) {
  const allowedModes = new Set(['list', 'grid', 'details', 'columns', 'gallery']);
  state.viewPreferences = readViewPreferences();
  const preference = state.viewPreferences?.[provider] || {};
  // Preferences are owner-scoped. Start from product defaults so a user with
  // no saved value cannot inherit the prior account's live mode or sort.
  state.mode = DEFAULT_FILES_MODE;
  state.sort = normalizeSortSpec(DEFAULT_FILES_SORT);
  state.smallIconSize = [16,20,24,32].includes(preference.smallIconSize) ? preference.smallIconSize : 20;
  state.largeIconSize = [32,64,96,128].includes(preference.largeIconSize) ? preference.largeIconSize : 64;
  if (allowedModes.has(preference.mode)) state.mode = preference.mode;
  if (preference.sort && typeof preference.sort === 'object') state.sort = normalizeSortSpec(preference.sort);
  const mode = state.shell?.querySelector('.files-mode-select');
  const sort = state.shell?.querySelector('.files-sort-select');
  const foldersFirst = state.shell?.querySelector('[data-files-folders-first]');
  if (mode) { mode.value = state.mode; mode.dispatchEvent(new Event('files-mode-sync')); }
  if (sort) sort.value = `${state.sort.key}:${state.sort.direction}`;
  updateFoldersFirstControl(foldersFirst, state.sort);
}

function rememberViewPreferences() {
  if (!state.owner) return;
  state.viewPreferences[state.provider] = { mode: state.mode, sort: { ...state.sort }, smallIconSize: state.smallIconSize, largeIconSize: state.largeIconSize };
  saveViewPreferences();
}

async function authenticatedOwner(signal = null) {
  try {
    const response = await fetch('/api/auth/status', { credentials:'same-origin', cache:'no-store',
      signal:AbortSignal.any([...(signal ? [signal] : []),AbortSignal.timeout(12_000)]) });
    if (!response.ok) {
      return {
        status: [401, 403].includes(response.status) ? 'invalid' : 'unavailable',
        owner: '',
      };
    }
    const data = await response.json();
    if (data?.authenticated !== true) return {status:'invalid',owner:''};
    return {status:'confirmed',owner:String(data.username || '').trim(),account:String(data.account_id || ''),manager:data.is_admin === true};
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return {status:'unavailable',owner:'',code:error?.name === 'TimeoutError' ? 'read_timeout' : 'transport_unavailable'};
  }
}

function destroyExplorerTree() {
  state.explorerTree?.destroy?.();
  state.explorerTree = null;
  state.collectionTree?.destroy?.(); state.collectionTree = null;
  state.favoriteTree?.destroy?.(); state.favoriteTree = null;
  state.workspaceTree?.destroy?.(); state.workspaceTree = null;
}

function explorerEntry(entry, parentPath = '') {
  if (entry?.ref || entry?.resource_ref) {
    const managed = facadeEntry(entry);
    return {
      ...managed,
      tree_id: entry.tree_id || managed.resource_id || managed.resource_ref,
      name: String(managed.name || 'Untitled'),
      kind: String(managed.kind || 'file').toLowerCase(),
    };
  }
  const path = entry?.path ? String(entry.path) : childPath(parentPath, entry?.name);
  return {
    ...entry,
    // `resource_ref` is transient wire identity for this tree projection. The
    // Rust directory page does not yet expose handles, so the Files adapter
    // supplies its already-authorized canonical path; the neutral controller
    // itself never derives, authorizes, or persists it.
    tree_id: String(entry?.tree_id || entry?.id || path || entry?.name),
    path,
    name: String(entry?.name || displayName(path)),
    kind: String(entry?.kind || 'file').toLowerCase(),
  };
}

function filterTree(query = '') {
  state.treeFilter = String(query || '').trim().toLowerCase();
  const input = state.sidebar?.querySelector('.files-tree-filter');
  if (input && input.value !== String(query || '')) input.value = String(query || '');
  for (const tree of [state.explorerTree, state.workspaceTree, state.favoriteTree, state.collectionTree, state.editorRootTree]) {
    if (!tree?.container) continue;
    const nodes = tree.snapshot().nodes;
    const allowed = new Set();
    const byId = new Map(nodes.map(node => [node.id, node]));
    for (const node of nodes) if (!state.treeFilter || String(node.data?.provenance?.logical_path || node.data?.name || "").toLowerCase().includes(state.treeFilter)) {
      let current = node;
      while (current) { allowed.add(current.id); current = byId.get(current.parentId); }
    }
    for (const item of tree.container.querySelectorAll('[data-files-tree-presentation]')) item.hidden = !allowed.has(item.dataset.filesTreePresentation) || explorerLayout.records().some(record => record.id === item.dataset.explorerCategory && record.hidden);
  }
  return state.treeFilter;
}

function mountExplorerTree(roots = state.navigationRoots, alternateContainer = null, treeOptions = {}) {
  const container = alternateContainer || state.sidebar?.querySelector('[data-files-tree]');
  if (!container) return null;
  if (!alternateContainer) destroyExplorerTree();
  const controller = createExplorerTree({
    container,
    roots: roots.filter(editorResourceAllowed).map(root => explorerEntry(root)),
    getId: entry => entry.tree_namespace ? entry.tree_namespace + ':' + entry.tree_id : entry.tree_id,
    getLabel: entry => entry.name + (entry.unavailable ? ' · unavailable' : ''),
    isBranch: node => !node.unavailable && isDirectory(node),
    ariaLabel: treeOptions.ariaLabel || 'Available host files and folders',
    emptyLabel: treeOptions.emptyLabel || 'No locations available',
    loadPage: async (node, { cursor, signal }) => {
      const resourceRef = String(node.resource_ref || '').trim();
      if (!resourceRef) throw new Error('Refresh the authorized Files roots before browsing.');
      const response = await filesFacadeClient.children(resourceRef, {
        cursor,
        sort: managedSortRequest({ key: 'name', direction: 'asc', directoriesFirst: true }),
        signal,
      });
      requestAnimationFrame(() => { if (!listeners.signal.aborted) filterTree(state.treeFilter); });
      const resources = (response?.entries || []).map(explorerEntry).filter(editorResourceAllowed);
      const previous = cursor ? (controller.getNode(node.tree_namespace ? node.tree_namespace + ':' + node.tree_id : node.tree_id)?.children || []).map(id => controller.getNode(id)?.data).filter(Boolean).map(({ tree_ancestors, ...entry }) => entry) : [];
      const pages = new Map([...previous, ...resources].map(entry => [entry.resource_id, entry]));
      const parent = { ...node, tree_ancestors:undefined, tree_entries:[...pages.values()], tree_cursor:response?.next_cursor || null };
      parent.tree_sort = { key:'name', direction:'asc', directoriesFirst:true };
      const ancestors = [...(node.tree_ancestors || []), parent];
      return { items:resources.map(entry => ({ ...entry, tree_namespace:node.tree_namespace, tree_ancestors:ancestors })), nextCursor:response?.next_cursor || null };

    },
    onActivate: async (node, context) => {
      cancelReveal();
      state.treeSelection = node;
      options.onSelection?.(node);
      if (node.unavailable) { setStatus('This saved location is unavailable. Retry or remove its shortcut.',true); return; }
      if (isDirectory(node)) {
        if (node.resource_ref) await openManagedDirectory(node, null);
        else setStatus('Refresh the authorized Files roots before opening this folder.', true);
      }
      // Preserve Files' desktop convention: a pointer click selects a worktree
      // file, double-click downloads it, while keyboard Enter activates it.
      else if (context.trigger === 'keyboard') {
        if (options.pickerMode) confirmPickerEntry(node);
        else if (node.resource_ref && node.capabilities?.includes('open')) await openManagedEntry(node);
        else if (isPreviewable(node, entryPath(node))) await previewEntry(node, entryPath(node));
        else setStatus('Refresh the authorized Files roots before downloading this file.', true);
      }
    },
    onDoubleActivate: node => {
      if (!isDirectory(node)) {
        if (options.pickerMode) return confirmPickerEntry(node);
        if (node.resource_ref && node.capabilities?.includes('open')) return openManagedEntry(node);
        if (isPreviewable(node, entryPath(node))) return previewEntry(node, entryPath(node));
        setStatus('Refresh the authorized Files roots before downloading this file.', true);
      }
      return null;
    },
    renderIcon: (node, { expanded }) => fileIcon(
      { ...node, open: isDirectory(node) && expanded },
      15,
      { className: 'files-tree-glyph' },
    ),
    onError: error => setStatus(error.message || 'Folder unavailable', true),
    actions:treeOptions.actions, onAction:treeOptions.onAction,
    classes: {
      item: 'files-tree-item',
      row: 'files-tree-row',
      toggle: 'files-tree-toggle',
      toggleSpacer: 'files-tree-toggle-spacer',
      icon: 'files-tree-icon',
      label: 'files-tree-label',
      group: 'files-tree-group',
      status: 'files-tree-message',
      error: 'files-tree-message error',
      retry: 'files-tree-retry',
      more: 'files-tree-more',
    },
    itemAttributes: node => ({
      'data-tree-resource-id': node.resource_id || null,
      'data-files-tree-presentation': node.tree_namespace ? node.tree_namespace + ':' + node.tree_id : node.tree_id,
      'data-tree-path': node.path || null,
    }),
    rowAttributes: node => ({ title: node.resource_ref ? `${node.provenance?.logical_path || node.name}${node.open_target?.app ? ' · Open in ' + destinationLabel(node.open_target.app) : ''}` : node.path }),
  });
  const tree = { ...controller, container };
  if (!alternateContainer) state.explorerTree = tree;
  const presentationKey = state.owner && treeOptions.persist !== false ? `files-tree:${options.surface || "files"}:${state.owner}:${state.currentWorkspaceId}:${treeOptions.persistenceKey || (alternateContainer ? 'collections' : 'locations')}` : '';
  const treeLifetime = new AbortController();
  const savePresentation = () => { if (!presentationKey) return; try { localStorage.setItem(presentationKey,JSON.stringify(tree.snapshot().nodes.filter(node => node.expanded).map(node => node.id))); } catch (_) {} };
  const persistAfterInteraction = () => requestAnimationFrame(() => { if (!treeLifetime.signal.aborted) { savePresentation(); filterTree(state.treeFilter); } });
  container.addEventListener('pointerdown',cancelReveal,{signal:treeLifetime.signal});
  container.addEventListener('click',persistAfterInteraction,{signal:treeLifetime.signal});
  container.addEventListener('keydown',persistAfterInteraction,{signal:treeLifetime.signal});
  tree.destroy = () => { savePresentation(); treeLifetime.abort(); controller.destroy(); };
  if (presentationKey) {
    let expanded = [];
    try { expanded = JSON.parse(localStorage.getItem(`files-tree:${options.surface || "files"}:${state.owner}:${state.currentWorkspaceId}:${treeOptions.persistenceKey || (alternateContainer ? 'collections' : 'locations')}`) || '[]'); } catch (_) {}
    void (async () => { for (const identity of expanded) { if (listeners.signal.aborted) break; await tree.expand(identity); } })();
  }
  updateTreeHighlight();
  return tree;
}

function setEditorRoot(entry, bound) {
  if (!options.editorRootProjection || !state.sidebar) return;
  const host=state.sidebar.querySelector('[data-files-editor-root]');
  const root=entry ? explorerEntry(entry) : null;
  const identity=root ? JSON.stringify([root.resource_id,root.resource_ref]) : null;
  if (bound === state.editorRootBound && identity === state.editorRootIdentity) return;
  state.editorRootBound=bound;
  state.editorRootIdentity=identity;
  state.sidebar.toggleAttribute('data-editor-root-bound',bound);
  host.hidden=!bound;
  state.editorRootTree?.destroy();
  state.editorRootTree=null;
  if (!root) { host.replaceChildren(); return; }
  const tree=state.editorRootTree=mountExplorerTree([root],host,
    {persist:false,ariaLabel:root.name,emptyLabel:'Folder unavailable'});
  const id=tree.snapshot().roots[0];
  if (id) void tree.expand(id);
}

async function loadFavorites(serverFavorites = [], savedPlaces = []) {
  state.favoritesController?.abort();
  const controller = new AbortController();
  state.favoritesController = controller;
  const signal = controller.signal;
  const lifecycle = state.lifecycleGeneration;
  const pinned = (Array.isArray(serverFavorites) ? serverFavorites : [])
    // Server favorites without an opaque resource are stale compatibility
    // records. They cannot enter the mounted projection or acquire authority
    // from their retained browser path.
    .filter((item) => item?.resource_ref || item?.ref)
    .map((item) => ({ ...explorerEntry(item), pinned: true }));
  const places = (Array.isArray(savedPlaces) ? savedPlaces : [])
    .filter((item) => item?.place_id)
    .map((item) => ({ ...explorerEntry(item), placeId: String(item.place_id), pinned: false, unavailable: !(item?.resource_ref || item?.ref) }));
  const opaqueIds = new Set(pinned.map(item => item.resource_id).filter(Boolean));
  const opaque = [...pinned];
  for (const place of places) {
    if (place.resource_id && opaqueIds.has(place.resource_id)) continue;
    if (place.resource_id) opaqueIds.add(place.resource_id);
    opaque.push(place);
  }
  state.favoritePaths = new Set(readFavoritePaths());
  state.favorites = opaque;
  renderFavorites();
  // Path-only favorites predate the Files facade. Drop them from the mounted
  // projection instead of statting browser paths through the legacy service.
  for (const path of state.favoritePaths) state.favorites.push({ name: displayName(path), path, kind: 'directory', unavailable: true, pinned: false });
  renderFavorites();
  updateFavoriteButton();
  return;

  // Raw path favorites are a measured one-time compatibility lane for users
  // who saved them before server-owned Places existed. They remain owner
  // scoped and are reauthorized on every load; all new favorites use Places.
  const pinnedPaths = new Set(pinned.map((item) => item.path).filter(Boolean));
  const paths = [...state.favoritePaths].filter((path) => !pinnedPaths.has(path));
  const validated = [];
  for (let offset = 0; offset < paths.length; offset += 4) {
    const page = await Promise.all(paths.slice(offset, offset + 4).map(async (path) => {
      try {
        const response = await filesServiceClient.stat(path, {
          cache: false,
          cacheKey: 'files-favorite-stat:' + path,
          signal,
        });
        if (lifecycle !== state.lifecycleGeneration) return null;
        const item = response?.data || {};
        const kind = String(item.kind || '').toLowerCase();
        if (!['directory', 'file', 'symlink'].includes(kind)) return null;
        const canonical = String(item.path || path);
        return {
          id: 'favorite:' + canonical,
          name: displayName(canonical),
          path: canonical,
          kind,
          pinned: false,
        };
      } catch (error) {
        if (error?.name === 'AbortError') return null;
        return null; // Revoked, missing, or unreadable favorites disappear quietly.
      }
    }));
    if (signal.aborted || lifecycle !== state.lifecycleGeneration) {
      if (state.favoritesController === controller) state.favoritesController = null;
      return;
    }
    for (const favorite of page) if (favorite) validated.push(favorite);
  }
  try {
    if (signal.aborted || lifecycle !== state.lifecycleGeneration) return;
    state.favoritePaths = new Set(validated.map((item) => item.path));
    saveFavoritePaths();
    state.favorites = [...opaque, ...validated];
    renderFavorites();
    updateFavoriteButton();
  } finally {
    if (state.favoritesController === controller) {
      state.favoritesController = null;
    }
  }
}

async function loadLegacyNavigationRoots({ force = false } = {}) {
  state.navigationController?.abort();
  const controller = new AbortController();
  state.navigationController = controller;
  try {
    const [response, identity] = await Promise.all([
      fetch('/api/odysseus-files/navigation-roots', { credentials: 'same-origin', signal: controller.signal }),
      authenticatedOwner(controller.signal),
    ]);
    const browserAccount = String(document.body?.dataset?.accountId || '');
    if (identity.status !== 'confirmed' || !identity.owner || !identity.account || (browserAccount && identity.account !== browserAccount)) {
      throw Object.assign(new Error('Files owner could not be verified'), {
        code:identity.status === 'invalid' ? 'sign_in_required' : identity.code || 'identity_unverified',
      });
    }
    const owner = identity.owner;
    if (!response.ok) {
      const error = new Error('Available files could not be loaded (' + response.status + ')');
      error.status = response.status;
      throw error;
    }
    const data = await response.json();
    const roots = Array.isArray(data?.roots) ? data.roots.filter((root) => root?.path) : [];
    const signature = JSON.stringify({
      owner,
      roots: roots.map((root) => [root.id, root.path, root.kind, root.capabilities]),
    });
    const previous = JSON.stringify({
      owner: state.owner,
      roots: state.navigationRoots.map(root => [root?.id, root?.path, root?.kind, root?.capabilities]),
    });
    if (state.owner && owner !== state.owner) {
      // Keep the newly fetched roots alive, but remove every old-owner
      // selection, column, and preview before the reopen path can preserve it.
      clearFilesAuthorityContent('Account file access changed');
    }
    state.owner = owner;
    state.navigation?.setScope({ account: owner, workspace: copalWorkspace() }, { clear: false });
    state.viewPreferences = readViewPreferences();
    applyViewPreferences('host');
    state.pane?.refresh?.();
    state.defaultPath = String(data?.default_path || '');
    state.workspaces = [];
    state.currentWorkspaceId = '';
    renderWorkspaces();
    if (force || signature !== previous || !state.explorerTree) {
      state.navigationRoots = roots.map(root => explorerEntry(root));
      state.navigationGeneration = data?.generation ?? null;
      mountExplorerTree();
    state.collectionTree = mountExplorerTree(managedRoots.filter(root => root.provider !== 'host'), state.sidebar?.querySelector('[data-files-collections]'));
      if (state.navigationRoots.length === 1 && isDirectory(state.navigationRoots[0])) {
        await state.explorerTree?.expand(state.navigationRoots[0].tree_id);
      }
    } else {
      state.navigationGeneration = data?.generation ?? null;
      updateTreeHighlight();
    }
    void loadFavorites(data?.favorites || []);
    return data;
  } finally {
    if (state.navigationController === controller) state.navigationController = null;
  }
}

async function loadOpaqueNavigationRoots({ deferSidebar = false, expectedGeneration = null } = {}) {
  const startingContent = state.contentGeneration;
  const startingLifecycle = state.lifecycleGeneration;
  const copalWorkspaceScope = copalWorkspace();
  const startingFilesWorkspace = state.currentWorkspaceId;
  state.navigationController?.abort();
  const controller = new AbortController();
  state.navigationController = controller;
  let requestContent = startingContent;
  let requestOwner = null;
  let requestAccount = null;
  let filesWorkspaceId = startingFilesWorkspace;
  const requestScopeCurrent = () => !controller.signal.aborted
    && state.navigationController === controller
    && state.lifecycleGeneration === startingLifecycle
    && copalWorkspace() === copalWorkspaceScope
    && state.currentWorkspaceId === filesWorkspaceId
    && state.nativeWindow?.visible === true;
  const requestCurrent = () => requestScopeCurrent() && state.contentGeneration === requestContent;
  const ownerScopeCurrent = () => {
    const account = String(document.body?.dataset?.accountId || '');
    return requestScopeCurrent() && requestOwner !== null && state.owner === requestOwner
      && state.accountId === requestAccount && (!account || account === requestAccount);
  };
  const releaseNavigation = () => {
    if (state.navigationController === controller) state.navigationController = null;
  };
  const sidebarStatus = state.sidebar?.querySelector('[data-files-navigation-status]')
    || el('div', { role:'status', 'data-files-navigation-status':'' });
  if (state.sidebar && !sidebarStatus.parentElement) state.sidebar.append(sidebarStatus);
  sidebarStatus.textContent = '';
  let sidebarDeferred = false;
  try {
    const [rootsResponse, identity, workspaceModule] = await Promise.all([
      filesFacadeClient.roots({ copalWorkspace: copalWorkspaceScope, signal: controller.signal }),
      authenticatedOwner(controller.signal),
      import('./workspace.js'),
    ]);
    if (!requestCurrent()) return null;
    const browserAccount = String(document.body?.dataset?.accountId || '');
    if (identity.status !== 'confirmed' || !identity.owner || !identity.account || (browserAccount && identity.account !== browserAccount)) {
      throw Object.assign(new Error('Files owner could not be verified'), {
        code:identity.status === 'invalid' ? 'sign_in_required' : identity.code || 'identity_unverified',
      });
    }
    if (expectedGeneration != null && (rootsResponse.policy_generation !== expectedGeneration || identity.account !== state.accountId)) {
      throw Object.assign(new Error('File access changed'), {code:'policy_generation_changed'});
    }
    const owner = identity.owner;
    requestOwner = owner;
    requestAccount = identity.account;
    const ownerChanged = Boolean(state.owner && (owner !== state.owner || identity.account !== state.accountId));
    if (ownerChanged) {
      // A confirmed owner transition retires all old-owner projections before
      // the new owner's optional catalogs begin resolving.
      clearFilesAuthorityContent('Account file access changed');
      destroyExplorerTree();
      state.navigationRoots = [];
      state.managedRoots = [];
      state.favorites = [];
      state.workspaces = [];
      state.favoritePaths.clear();
      state.currentWorkspaceId = startingFilesWorkspace;
      mountExplorerTree([]);
      renderFavorites();
      renderWorkspaces();
      requestContent = state.contentGeneration;
    }
    if (!requestCurrent()) return null;
    state.owner = owner;
    state.accountId = identity.account;
    state.navigation?.setScope({ account: owner, workspace: copalWorkspaceScope }, { clear: ownerChanged });
    state.viewPreferences = readViewPreferences();
    if (state.contentGeneration === requestContent) applyViewPreferences(state.provider);
    state.pane?.refresh?.();

    const managedRoots = (rootsResponse?.entries || []).map(facadeEntry).filter(editorResourceAllowed);
    const hostRoot = managedRoots.find(entry => entry.provider === 'host' && isDirectory(entry));
    filesWorkspaceId = workspaceModule.getWorkspaceId();
    state.currentWorkspaceId = filesWorkspaceId;
    explorerLayout.apply();
    state.defaultPath = '';
    state.managedRoots = managedRoots;
    state.importCapabilities = rootsResponse?.import_capabilities && typeof rootsResponse.import_capabilities === 'object'
      ? rootsResponse.import_capabilities : null;
    state.navigationGeneration = rootsResponse?.policy_generation ?? null;
    const navigationGeneration = state.navigationGeneration;
    const sidebarCurrent = () => ownerScopeCurrent() && state.navigationGeneration === navigationGeneration;
    const setup = state.shell?.querySelector('[data-files-add-host]');
    if (setup) { setup.hidden = !identity.manager; setup.disabled = !identity.manager; }

    state.collectionTree?.destroy();
    state.collectionTree = mountExplorerTree(managedRoots.filter(root => root.provider !== 'host'), state.sidebar?.querySelector('[data-files-collections]'));
    const warnings = {};
    const warn = (source, message) => {
      warnings[source] = message;
      sidebarStatus.textContent = Object.values(warnings).filter(Boolean).join(' ');
    };
    let pinned = [];
    let places = [];
    const hostReady = (async () => {
      let roots = [];
      try {
        if (!hostRoot) throw Object.assign(new Error('Host file access is unavailable.'), {code:'provider_unavailable'});
        const response = await filesFacadeClient.children(hostRoot.resource_ref, {
          limit: 200,
          sort: managedSortRequest({ key:'name', direction:'asc', directoriesFirst:true }),
          signal: controller.signal,
        });
        if (!sidebarCurrent()) return [];
        roots = response.entries.map(explorerEntry).filter(editorResourceAllowed);
        clearFilesReadRecovery('navigation');
        if (!roots.length) warn('host', identity.manager ? 'No Host folders are shared with you. Use Add folder to choose one.'
          : 'No Host folders are shared with your account. Ask a manager to share a folder.');
      } catch (error) {
        if (!sidebarCurrent() || error?.name === 'AbortError') return [];
        showFilesReadRetry('navigation','Host locations',error,async () => {
          try {
            const result = await loadNavigationRoots({deferSidebar:true, expectedGeneration:navigationGeneration});
            await result?.hostReady;
          } catch (retryError) {
            if (!['policy_generation_changed','sign_in_required','identity_unverified'].includes(retryError?.code)
              && ![401,403].includes(retryError?.status)) throw retryError;
            await handleFilesPolicyChanged();
          }
        });
      }
      state.navigationRoots = roots;
      pinned = roots.filter(item => item.provenance?.favorite);
      // The Host tree and managed collections have independent load states.
      const container = state.sidebar?.querySelector('[data-files-tree]');
      state.explorerTree?.destroy();
      state.explorerTree = container ? mountExplorerTree(roots, container) : null;
      await loadFavorites(pinned, places);
      if (sidebarCurrent() && roots.length === 1 && isDirectory(roots[0])) {
        void state.explorerTree?.expand(roots[0].tree_id).catch(() => {
          if (sidebarCurrent()) warn('expansion','The initial location could not be expanded.');
        });
      }
      return roots;
    })();
    const placesReady = filesFacadeClient.places({ signal:controller.signal }).then(response => {
      if (!sidebarCurrent()) return;
      places = response.entries;
      return loadFavorites(pinned, places);
    }).catch(error => {
      if (sidebarCurrent() && error?.name !== 'AbortError') {
        warn('places','Saved places are unavailable.');
        return error;
      }
    });
    const workspacesReady = filesFacadeClient.workspaces({ signal:controller.signal }).then(response => {
      if (!sidebarCurrent()) return;
      state.workspaces = Array.isArray(response?.entries) ? response.entries : [];
      renderWorkspaces();
    }).catch(error => {
      if (sidebarCurrent() && error?.name !== 'AbortError') {
        warn('workspaces','Saved workspaces are unavailable.');
        return error;
      }
    });
    const sidebarReady = Promise.all([hostReady, placesReady, workspacesReady]);
    if (deferSidebar) {
      sidebarDeferred = true;
      void sidebarReady.catch(() => {
        if (sidebarCurrent()) warn('sidebar','Some navigation data is unavailable.');
      }).finally(releaseNavigation);
    } else {
      await sidebarReady;
      if (!sidebarCurrent()) return null;
    }
    return {
      version: rootsResponse?.version,
      generation: navigationGeneration,
      opaque: true,
      roots: state.navigationRoots,
      hostReady,
      placesReady,
      workspacesReady,
    };
  } finally {
    if (!sidebarDeferred) releaseNavigation();
  }
}

async function loadNavigationRoots(options = {}) {
  // Mounted Copal Files has one authority. A missing facade is an outage and
  // must not repopulate the tree with path-only compatibility entries.
  return loadOpaqueNavigationRoots(options);
}

function renderFavorites() {
  const container = state.sidebar?.querySelector('[data-files-favorites]');
  if (!container) return;
  const visible = state.favoriteVisibleCount || 50;
  const roots = state.favorites.slice(0,visible).map(item => ({ ...item, tree_id:item.resource_id || item.placeId || item.path, tree_namespace:'favorite', favoriteRoot:true }));
  if (state.favoriteTree) state.favoriteTree.setRoots(roots.map(explorerEntry));
  else {
    container.replaceChildren();
    state.favoriteTree = mountExplorerTree(roots,container,{ persistenceKey:'favorites', emptyLabel:'No favorites yet',
      actions:node => node.favoriteRoot ? [...(node.unavailable ? [{id:'retry',label:'Retry',icon:glyphIcon('refresh',12)}] : []), ...(!node.pinned ? [{id:'remove',label:'Remove Favorite shortcut',icon:glyphIcon('close',12)}] : [])] : [],
      onAction:async (action,node) => {
        if (action === 'retry') return loadNavigationRoots({force:true});
        const item = state.favorites.find(item => (item.resource_id || item.placeId || item.path) === node.tree_id);
        if (!item || item.pinned) return;
        try { if (item.placeId) await filesFacadeClient.removePlace(item.placeId); else { state.favoritePaths.delete(item.path); saveFavoritePaths(); }
          state.favorites = state.favorites.filter(candidate => candidate !== item); renderFavorites(); updateFavoriteButton();
          window.dispatchEvent(new CustomEvent('openclank:files-places-changed',{detail:{owner:state.owner,source:id}}));
        } catch(error) { setStatus(error.message || 'Favorite could not be removed',true); }
      },
    });
  }
  container.querySelector('[data-files-favorites-more]')?.remove();
  if (state.favorites.length > visible) container.append(el('button',{type:'button','data-files-favorites-more':'',text:'Load more Favorites',onclick:() => { state.favoriteVisibleCount = visible + 50; renderFavorites(); }}));
  updateTreeHighlight();
}

async function useWorkspace(workspaceId) {
  try {
    const workspace = await import('./workspace.js');
    const resolved = await workspace.resolveWorkspaceId(workspaceId, 'agent_workspace');
    await workspace.setWorkspace(resolved.path, resolved.id);
    state.currentWorkspaceId = resolved.id;
    renderWorkspaces();
    setStatus(`Workspace: ${resolved.name || displayName(resolved.path)}`);
  } catch (error) {
    setStatus(error.message || 'This Workspace does not have Agent access', true);
  }
}

async function renameWorkspace(item, current = () => true) {
  const name = await uiModule.styledPrompt('Choose the name shown across Files, chat, and Editor.', {
    title: 'Rename Workspace',
    defaultValue: item.workspace.name,
    confirmText: 'Rename',
    maxLength: 200,
  });
  if (!name || name === item.workspace.name || !current()) return;
  try {
    await filesFacadeClient.updateWorkspace(item.workspace.id, {
      name,
      expected_revision: item.workspace.revision,
    });
    await loadWorkspaceCatalog();
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
    setStatus(`Renamed Workspace to ${name}`);
  } catch (error) {
    setStatus(error.message || 'Workspace could not be renamed', true);
  }
}

async function archiveWorkspace(item, current = () => true) {
  const confirmed = await uiModule.styledConfirm(
    `Archive “${item.workspace.name}”? Workspace-scoped agent approvals are revoked; files and the Location are not removed.`,
    { title: 'Archive Workspace?', confirmText: 'Archive', cancelText: 'Keep', danger: true },
  );
  if (!confirmed || !current()) return;
  try {
    await filesFacadeClient.updateWorkspace(item.workspace.id, {
      archived: true,
      expected_revision: item.workspace.revision,
    });
    if (state.currentWorkspaceId === item.workspace.id) {
      const workspace = await import('./workspace.js');
      await workspace.setWorkspace('');
      state.currentWorkspaceId = '';
    }
    await loadWorkspaceCatalog();
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
    setStatus(`Archived Workspace: ${item.workspace.name}`);
  } catch (error) {
    setStatus(error.message || 'Workspace could not be archived', true);
  }
}

function renderWorkspaces() {
  const container = state.sidebar?.querySelector('[data-files-workspaces]');
  if (!container) return;
  const roots = state.workspaces.map(item => {
    const resource = item.resource ? facadeEntry(item.resource) : {};
    return { ...resource, name:item.workspace.name, tree_id:item.workspace.id, tree_namespace:'workspace:' + item.workspace.id,
      workspaceRootId:item.workspace.id, navigationRole:'workspace', kind:'folder', unavailable:item.availability !== 'available' || !resource.resource_ref };
  });
  if (state.workspaceTree) { state.workspaceTree.setRoots(roots.map(explorerEntry)); return; }
  container.replaceChildren();
  state.workspaceTree = mountExplorerTree(roots,container,{ persistenceKey:'workspaces', emptyLabel:'No Workspaces yet',
    actions:node => node.workspaceRootId ? [{id:'workspace-actions',label:'Workspace actions',icon:glyphIcon('more',12)}] : [],
    onAction:(_action,node,context) => {
      const row = [...container.querySelectorAll('[data-explorer-id]')].find(item => item.dataset.explorerId === context.id);
      if (!row) return;
      const anchor = row.querySelector('[data-explorer-row] [data-explorer-action="workspace-actions"]') || row;
      const captured = captureTreeCommands(state.workspaceTree, context.id, node);
      showCapturedFilesMenu(anchor, captured);
    },
  });
}

async function loadWorkspaceCatalog({ signal = null } = {}) {
  let controller = null;
  if (!signal) {
    state.workspaceController?.abort();
    controller = new AbortController();
    state.workspaceController = controller;
    signal = controller.signal;
  }
  const lifecycle = state.lifecycleGeneration;
  try {
    const response = await filesFacadeClient.workspaces({ signal });
    const workspace = await import('./workspace.js');
    if (signal?.aborted || lifecycle !== state.lifecycleGeneration) return;
    state.currentWorkspaceId = workspace.getWorkspaceId();
    explorerLayout.apply();
    state.workspaces = Array.isArray(response?.entries) ? response.entries : [];
    renderWorkspaces();
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (error?.status === 404) {
      state.workspaces = [];
      renderWorkspaces();
      return;
    }
    throw error;
  } finally {
    if (controller && state.workspaceController === controller) state.workspaceController = null;
  }
}

async function openTreeFile(path) {
  await download(path);
}

function updateTreeHighlight() {
  const column = activeColumn();
  const activeResource = String(column?.resourceId || '');
  const activePath = state.provider === 'host' && !activeResource ? state.hostPath : '';
  for (const tree of [state.explorerTree, state.workspaceTree, state.favoriteTree, state.collectionTree, state.editorRootTree]) {
    const node = tree?.snapshot().nodes.find(node => activeResource
      ? node.data.resource_id === activeResource : activePath && node.data.path === activePath);
    tree?.setActive(node?.id || null);
  }
}

function updateFavoriteButton() {
  const button = state.shell?.querySelector('[data-files-favorite-toggle]');
  if (!button) return;
  const column = activeColumn();
  const activeResource = state.provider === 'host' ? String(column?.resourceId || '') : '';
  const activeRef = state.provider === 'host' ? String(column?.resourceRef || '') : '';
  const favorite = activeResource
    ? state.favorites.find(item => item.resource_id === activeResource)
    : state.favorites.find(item => item.path === state.hostPath);
  const pinned = Boolean(favorite?.pinned);
  const selected = Boolean(favorite) || (!activeResource && state.favoritePaths.has(state.hostPath));
  button.classList.toggle('active', selected);
  button.setAttribute('aria-pressed', selected ? 'true' : 'false');
  button.title = pinned ? 'Home is pinned in Favorites' : selected ? 'Remove current folder from Favorites' : 'Add current folder to Favorites';
  button.setAttribute('aria-label', button.title);
  button.replaceChildren(namedGlyph(selected ? 'star-filled' : 'star', { size: 15, className: 'files-button-glyph' }));
  button.disabled = state.provider !== 'host' || pinned || (!activeRef && !state.hostPath);
}

async function toggleCurrentFavorite(column = activeColumn(), current = () => true) {
  if ((column?.provider || state.provider) !== 'host' || !current()) return;
  const activeResource = String(column?.resourceId || column?.resource_id || '');
  const activeRef = destinationResourceRef(column);
  if (activeResource && activeRef) {
    const existing = state.favorites.find(item => item.resource_id === activeResource);
    if (existing?.pinned) return;
    try {
      if (existing?.placeId) {
        await filesFacadeClient.removePlace(existing.placeId);
        if (!current()) return;
        state.favorites = state.favorites.filter(item => item !== existing);
      } else {
        const response = await filesFacadeClient.savePlace(activeRef);
        if (!current()) return;
        const place = { ...explorerEntry(response?.resource || {}), placeId: String(response?.resource?.place_id || ''), pinned: false };
        state.favorites = [...state.favorites.filter(item => item.resource_id !== place.resource_id), place];
      }
      renderFavorites();
      updateFavoriteButton();
      window.dispatchEvent(new CustomEvent('openclank:files-places-changed', {detail:{ owner:state.owner, source:id }}));
    } catch (error) {
      setStatus(error.message || 'Favorite could not be changed', true);
    }
    return;
  }
  if (!state.hostPath) return;
  if (state.favoritePaths.has(state.hostPath)) {
    state.favoritePaths.delete(state.hostPath);
    state.favorites = state.favorites.filter((item) => item.path !== state.hostPath || item.pinned);
  } else {
    state.favoritePaths.add(state.hostPath);
    state.favorites.push({
      id: 'favorite:' + state.hostPath,
      name: displayName(state.hostPath),
      path: state.hostPath,
      kind: 'directory',
      pinned: false,
    });
  }
  saveFavoritePaths();
  renderFavorites();
  updateFavoriteButton();
}

function entryPath(entry) {
  if (isFacadeEntry(entry)) return String(entry.resource_ref || entry.ref);
  if (entry?.path) return String(entry.path);
  if (state.provider === 'host') return childPath(state.hostPath, entry?.name);
  if (entry?.provider === 'host' && entry?.path) return String(entry.path);
  return String(entry?.resource_ref || entry?.ref || entry?.resource_id || entry?.id || entry?.name || '');
}

function entrySelectionKey(entry) {
  return resourceKey(entry, entry?.provider || state.provider || 'host', {
    owner: state.owner,
    workspace: entry?.workspace_id || entry?.workspaceId || entry?.workspace || state.currentWorkspaceId || copalWorkspace(),
  });
}

function selectionScope(column = activeColumn(), columnIndex = state.columns.length - 1) {
  return {
    owner: state.owner,
    workspace: state.currentWorkspaceId || copalWorkspace(),
    provider: String(column?.provider || state.provider || 'host'),
    parentRef: String(column?.resourceRef || column?.resourceId || column?.path || ''),
    query: String(column?.query || state.searchQuery || ''),
    column: state.mode === 'columns' ? String(columnIndex) : '',
  };
}

function columnIndexForNode(node) {
  const panel = node instanceof Element ? node.closest('.files-column[data-column-index]') : null;
  const index = Number(panel?.dataset?.columnIndex);
  return Number.isInteger(index) && index >= 0 ? index : state.activeColumnIndex >= 0 ? state.activeColumnIndex : state.columns.length - 1;
}

function activateColumn(index) {
  const safeIndex = Number.isInteger(Number(index)) ? Number(index) : state.columns.length - 1;
  if (state.mode === 'columns' && state.columns[safeIndex]) {
    state.activeColumnIndex = safeIndex;
    const column = state.columns[safeIndex];
    state.entries = column.entries || [];
    state.nextCursor = column.nextCursor || null;
    const model = ensureSelectionModel(column, safeIndex);
    state.selected = new Set(model.selectedKeys());
    return { column, model, index: safeIndex };
  }
  return { column: activeColumn(), model: ensureSelectionModel(activeColumn()), index: state.columns.length - 1 };
}

function ensureSelectionModel(column = activeColumn(), columnIndex = state.columns.length - 1) {
  const inferredIndex = state.mode === 'columns' && Number(columnIndex) < 0
    ? state.columns.indexOf(column)
    : columnIndex;
  const scope = selectionScope(column, inferredIndex >= 0 ? inferredIndex : columnIndex);
  const key = scopeKey(scope);
  let model = state.selectionModels.get(key);
  if (!model) {
    model = createFilesSelectionModel({ scope });
    state.selectionModels.set(key, model);
  }
  state.selectionModel = model;
  return model;
}

function syncSelectionModel(items = state.entries, { append = false, columnIndex = state.columns.length - 1 } = {}) {
  const model = ensureSelectionModel(activeColumn(), columnIndex);
  model.setItems(items, { append });
  model.replaceSelection([...state.selected]);
  return model;
}

function clearFilesSelection(column = activeColumn(), columnIndex = state.activeColumnIndex) {
  const model = ensureSelectionModel(column, columnIndex);
  model.clear();
  if (column) column.selection = new Set();
  state.selected = new Set();
  return model;
}

function applyModelSelection(model, { render = true } = {}) {
  state.treeSelection = null;
  state.selected = new Set(model.selectedKeys());
  options.onSelection?.(model.selectedEntries()[0] || null);
  if (state.mode === 'columns') {
    const index = state.columns.findIndex((column) => selectionScope(column, state.columns.indexOf(column)).owner === model.scope.owner
      && selectionScope(column, state.columns.indexOf(column)).provider === model.scope.provider
      && selectionScope(column, state.columns.indexOf(column)).parentRef === model.scope.parentRef
      && String(state.columns.indexOf(column)) === String(model.scope.column));
    if (index >= 0) state.columns[index].selection = new Set(model.selectedKeys());
  }
  if (render) renderEntries();
  const entries = model.selectedEntries();
  const bytes = entries.reduce((total,entry) => total + (Number(entry.size) || 0),0);
  setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items · ${entries.length} selected${bytes ? ' · ' + formatBytes(bytes) : ''}`, false, { keepUndo: true });
}

function syncVisibleSelection(model, body = state.shell?.querySelector('[data-files-body]')) {
  if (!model || !body) return;
  for (const node of body.querySelectorAll('[data-files-selection-key]')) {
    const selected = model.has(node.dataset.filesSelectionKey);
    node.classList.toggle('selected', selected);
    node.setAttribute('aria-selected', selected ? 'true' : 'false');
  }
}

function filesGestureTarget(target) {
  return target instanceof Element ? target.closest('[data-files-body]') : null;
}

function blockedGestureTarget(target) {
  return target instanceof Element && !!target.closest('button, a, summary, input, textarea, select, [contenteditable="true"], .files-entry, .files-column-entry, [draggable="true"]');
}

function transferCapabilities(entry) {
  const capabilities = new Set(Array.isArray(entry?.capabilities) ? entry.capabilities : []);
  const directory = isDirectory(entry);
  return {
    // S01 grants directory moves through parent write, while file moves need
    // the explicit move capability. Copy is read-like and never implied by a
    // source write grant.
    move: directory ? capabilities.has('write') : capabilities.has('move'),
    copy: ['read', 'download', 'open', 'copy'].some((capability) => capabilities.has(capability)),
  };
}

function filesPolicyGeneration() {
  const value = Number(state.navigationGeneration);
  return Number.isSafeInteger(value) && value >= 0 ? value : 0;
}

function abortFilesImports() {
  for (const controller of state.importControllers) controller.abort();
  state.importControllers.clear();
}

function canDragEntry(entry) {
  const capability = transferCapabilities(entry);
  return Boolean(entry?.resource_ref && (capability.move || capability.copy));
}

function isCanvasImageEntry(entry) {
  const mime = String(entry?.mime_type || entry?.media_type || entry?.mime || '').toLowerCase();
  const capabilities = new Set(Array.isArray(entry?.capabilities) ? entry.capabilities.map(String) : []);
  return !isDirectory(entry) && mime.startsWith('image/') && Boolean(entry?.resource_ref)
    && entry?.revision && typeof entry.revision === 'object' && !Array.isArray(entry.revision)
    && capabilities.has('read') && (capabilities.has('export') || capabilities.has('download'));
}

function filesCanvasGestureContext(entry, model, commandId = operationId('files-canvas')) {
  const capabilities = new Set(Array.isArray(entry?.capabilities) ? entry.capabilities.map(String) : []);
  const scope = model?.scope || selectionScope(activeColumn(), state.activeColumnIndex);
  return Object.freeze({
    commandId: String(commandId), generation: filesPolicyGeneration(),
    policyGeneration: filesPolicyGeneration(), selectionEpoch: Number(model?.epoch || 0),
    owner: String(state.owner), workspace: String(scope.workspace || state.currentWorkspaceId || copalWorkspace()),
    pane: String(state.nativeWindow?.id || 'files-window'), provider: String(scope.provider || entry?.provider || ''),
    parent: String(scope.parentRef || ''), scopeKey: String(model?.snapshot?.().scopeKey || scopeKey(scope)),
    selectedKeys: Object.freeze(model?.selectedKeys?.().map(String) || []),
    sourceCapabilities: Object.freeze({
      [entrySelectionKey(entry)]: Object.freeze({
        read: capabilities.has('read'), open: capabilities.has('open'),
        download: capabilities.has('download'), export: capabilities.has('export'),
      }),
    }),
    allowCopy: Boolean(model?.selectedEntries?.().every(item => transferCapabilities(item).copy)),
    allowMove: Boolean(model?.selectedEntries?.().every(item => transferCapabilities(item).move)),
  });
}

function canvasPayloadForEntry(entry, model, commandId = operationId('files-canvas')) {
  if (!model || model.selectedEntries().length !== 1 || model.selectedEntries()[0] !== entry || !isCanvasImageEntry(entry)) return null;
  const generic = buildInternalDragPayload({
    scope: model.scope, generation: filesPolicyGeneration(), policyGeneration: filesPolicyGeneration(),
    owner: state.owner, pane: state.nativeWindow?.id || 'files-window', selectionEpoch: model.epoch,
    kind: 'copy', entries: transferEntries(model), selectedKeys: model.selectedKeys(),
  });
  const source = generic?.sources?.[0];
  if (!source) return null;
  const context = filesCanvasGestureContext(entry, model, commandId);
  return Object.freeze({
    payload: createFilesCanvasImagePayload({ ...entry, ref: source.resource_ref, resource_ref: source.resource_ref, resource_key: source.resource_key, revision: entry.revision || source.revision }, {
      account: state.owner, workspace: context.workspace, operationId: commandId, itemId: source.item_id, context,
    }), context,
  });
}

function currentCanvasMenuSelection(entry, request) {
  const captured = request?.adapterContext;
  if (!captured?.filesScope) return null;
  const model = validateFilesSelectionGuard(captured, { targetKey:entrySelectionKey(entry) });
  if (!model) return null;
  const selected = model.selectedEntries();
  if (!selected.length) return null;
  return { model, entries:selected, guard:captured };
}

function currentCanvasMenuModel(entry, request) {
  const selection = currentCanvasMenuSelection(entry, request);
  if (!selection || selection.entries.length !== 1 || entrySelectionKey(selection.entries[0]) !== entrySelectionKey(entry)) return null;
  return selection.model;
}

function creationDestinationForNode(node, entry = contextEntryForNode(node)) {
  const index = columnIndexForNode(node);
  const column = state.columns[index] || (state.mode === 'columns' ? null : activeColumn());
  const target = entry && isDirectory(entry) ? entry : column;
  return creationDestinationForEntry(target, index);
}

function creationDestinationForEntry(target, index = state.activeColumnIndex) {
  const ref = destinationResourceRef(target);
  if (!ref || !target || !isDirectory(target)) return null;
  const capabilities = new Set(Array.isArray(target.capabilities) ? target.capabilities.map(String) : []);
  const copalCorpus = target.provider === 'copal' && (
    (target.kind === 'provider_root' || (target.provenance?.domain === 'copal' && !target.provenance?.view)) ? 'notes'
      : target.provenance?.view === 'active' && ['all', 'notes', 'wiki'].includes(target.provenance?.corpus)
        ? (target.provenance.corpus === 'wiki' ? 'wiki' : 'notes') : null
  );
  if (!capabilities.has('children') || (!capabilities.has('write') && !copalCorpus)) return null;
  const revision = target.revision && typeof target.revision === 'object' ? { ...target.revision } : null;
  return Object.freeze({
    creationCopalCorpus: copalCorpus || null,
    creationSupportsFolders: !copalCorpus,
    creationDestinationRef: ref,
    creationDestinationId: String(target.resource_id || target.resourceId || ''),
    creationDestinationRevision: revision ? Object.freeze(revision) : null,
    creationDestinationIndex: index,
    creationCommandId: operationId('files-create-command'),
    creationGeneration: filesPolicyGeneration(),
    creationContent: state.contentGeneration,
    creationNavigating: Boolean(state.contentController),
    creationLifecycle: state.lifecycleGeneration,
    creationOwner: String(state.owner),
    creationWorkspace: String(state.currentWorkspaceId || copalWorkspace()),
    creationProvider: String(target.provider || state.provider || ''),
    creationDestinationName: String(target.name || 'folder'),
  });
}

function creationName(value, kind) {
  const name = String(value ?? '').trim();
  if (!name || name.length > 240 || new TextEncoder().encode(name).length > 240
    || name.includes('/') || name.includes('\\') || name.includes('\0') || name === '.' || name === '..') {
    throw new Error(`${kind === 'folder' ? 'Folder' : 'File'} name is invalid.`);
  }
  return name;
}

function creationCaptureForNode(node) {
  return creationDestinationForNode(node);
}

async function createFilesResource(kind, captured, isCurrent = () => true) {
  if (options.pickerMode) { setStatus("This dialog selects resources; creating items is unavailable.", true); return false; }
  if (!isCurrent()) return false;
  if (state.contentController) { setStatus('Wait for folder navigation to finish before choosing New.', true); return false; }
  if (!captured?.creationDestinationRef) captured = await pickCreationDestination();
  if (!captured?.creationDestinationRef) {
    setStatus('Choose an authorized writable folder before creating an item.', true);
    return false;
  }
  if (kind === 'folder' && captured.creationSupportsFolders === false) {
    setStatus('Create a page in this collection. Include a folder in its name to group pages.', true);
    return false;
  }
  const generation = Number(captured.creationGeneration);
  const lifecycle = Number(captured.creationLifecycle);
  const content = Number(captured.creationContent);
  const owner = String(captured.creationOwner);
  if (captured.creationNavigating || state.contentController || state.contentGeneration !== content
      || state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle
      || String(state.currentWorkspaceId || copalWorkspace()) !== captured.creationWorkspace) {
    setStatus('The Files destination or access changed; choose New again.', true);
    return true;
  }
  let expiredRetried = false;
  let proposed = kind === 'file' ? 'Untitled.md' : 'Untitled folder';
  let commandId = String(captured.creationCommandId || operationId(`files-create-${kind}`));
  while (true) {
    const value = await uiModule.styledPrompt(
      kind === 'file' ? `Choose a file name in ${captured.creationDestinationName}.` : `Choose a folder name in ${captured.creationDestinationName}.`,
      { title: kind === 'file' ? 'New File' : 'New Folder', defaultValue: proposed, confirmText: 'Create', maxLength: 240 },
    );
    if (value == null) return false;
    if (!isCurrent()) return false;
    try { proposed = creationName(value, kind); } catch (error) { setStatus(error.message, true); continue; }
    if (captured.creationNavigating || state.contentController || state.contentGeneration !== content
      || state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle
      || String(state.currentWorkspaceId || copalWorkspace()) !== captured.creationWorkspace) {
      setStatus('The Files destination or access changed; choose New again.', true);
      return true;
    }
    const operation = commandId;
    try {
      if (kind === 'file' && captured.creationCopalCorpus) {
        const copal = await import('./copal.js');
        if (!isCurrent()) return false;
        const result = await copal.createFilesNote({ name: proposed, workspace: captured.creationWorkspace,
          corpus: captured.creationCopalCorpus, actionId: operation });
        if (!result?.doc?.id) throw new Error('Page creation is unconfirmed. Check the folder before retrying.');
        if (state.owner !== owner || state.lifecycleGeneration !== lifecycle
            || String(state.currentWorkspaceId || copalWorkspace()) !== captured.creationWorkspace) {
          setStatus('Files access changed after creation; reopen the collection to confirm the new note.', true);
          return true;
        }
        await reloadManagedColumn(Number(captured.creationDestinationIndex));
        setStatus(`Created file: ${proposed}. Open it from Documents in Editor.`);
        return true;
      }
      let response;
      const create = () => kind === 'folder'
        ? filesFacadeClient.createDirectory(captured.creationDestinationRef, {
          name: proposed, operationId: operation, generation,
          expectedRevision: captured.creationDestinationRevision, collision: 'fail',
        })
        : filesFacadeClient.createFile(captured.creationDestinationRef, {
          name: proposed, operationId: operation, itemId: operation, generation, collision: 'fail',
        });
      try {
        try { response = await create(); } catch (error) {
          // This exact admission denial precedes provider work. Other errors
          // may describe a committed operation and must never trigger create.
          if (kind !== 'folder' || expiredRetried || !isExpiredFilesReference(error)) throw error;
          expiredRetried = true;
          const renewed = await renewManagedDirectoryAuthority({
            resource_id: captured.creationDestinationId,
            resource_ref: captured.creationDestinationRef,
            provider: captured.creationProvider,
          }, Number(captured.creationDestinationIndex));
          if (!isCurrent()) return false;
          if (!renewed.resource.capabilities.includes('write')) throw new Error('This folder is no longer writable.');
          state.columns = renewed.columns;
          captured = { ...captured, creationDestinationRef: renewed.resource.resource_ref };
          response = await create();
        }
      } catch (error) {
        // A lost response is reconciled by the same operation ID. Retrying the
        // provider request would be unsafe because the first request may have
        // already created the item.
        let receiptError = error;
        for (let attempt = 0; attempt < 5; attempt += 1) {
          try {
            response = { ...(await filesFacadeClient.operationReceipt(operation, { generation })) };
            receiptError = null;
            break;
          } catch (reconcileError) {
            receiptError = reconcileError;
            if (attempt < 4) await new Promise((resolve) => setTimeout(resolve, 50 * (attempt + 1)));
          }
        }
        if (receiptError) throw error;
      }
      if (captured.creationNavigating || state.contentController || state.contentGeneration !== content
      || state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle
      || String(state.currentWorkspaceId || copalWorkspace()) !== captured.creationWorkspace) {
        setStatus('Files access changed before the creation result could be confirmed here; do not submit it again.', true);
        return true;
      }
      const receiptItem = response?.items?.find?.((item) => item?.outcome === 'committed' || item?.outcome === 'unchanged');
      const failedItem = response?.items?.find?.((item) => item && !['committed', 'unchanged'].includes(String(item.outcome || '').toLowerCase()));
      if (failedItem) {
        const collision = ['collision', 'resource_changed', 'conflict'].includes(String(failedItem.code || failedItem.outcome || '').toLowerCase());
        const pending = String(response?.state || '').toLowerCase() === 'pending'
          || String(failedItem.outcome || '').toLowerCase() === 'pending';
        const message = collision ? `${proposed} already exists. Choose another name.`
          : pending ? 'Creation outcome is not yet confirmed; do not submit it again.' : 'File creation was not committed.';
        throw Object.assign(new Error(message), { code: collision ? 'collision' : String(failedItem.code || failedItem.outcome || 'creation_failed') });
      }
      const ref = String(response?.resource?.ref || receiptItem?.resource_ref || '');
      const resourceResponse = ref && kind === 'file' ? await filesFacadeClient.stat(ref) : null;
      const resource = facadeEntry(response?.resource || resourceResponse?.resource || resourceResponse || {});
      await reloadManagedColumn(Number(captured.creationDestinationIndex));
      if (resource.resource_ref) {
        const refreshed = state.entries.find((entry) => entry.resource_ref === resource.resource_ref) || resource;
        const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
        model.clear(); model.select(entrySelectionKey(refreshed));
        state.selected = new Set(model.selectedKeys());
        renderEntries();
        if (kind === 'file' && refreshed.capabilities?.includes('open') && await uiModule.styledConfirm(`Open ${proposed} in Editor?`, { title: 'File created', confirmText: 'Open', cancelText: 'Later' })) {
          await openManagedEntry(refreshed);
        }
      }
      setStatus(`Created ${kind === 'folder' ? 'folder' : 'file'}: ${proposed}`);
      return true;
    } catch (error) {
      if (error?.name === 'AbortError') return false;
      if (['collision', 'resource_exists', 'already_exists', 'resource_changed'].includes(String(error?.code || '').toLowerCase())
        || /already exists|collision|conflict/i.test(String(error?.message || ''))) {
        setStatus(`${proposed} already exists. Choose another name.`, true);
        commandId = operationId(`files-create-${kind}`);
        continue;
      }
      setStatus(error?.message || `New ${kind} failed`, true);
      return false;
    }
  }
}

async function pickCreationDestination() {
  try { await loadNavigationRoots({ force: true }); } catch (error) {
    setStatus(error?.message || 'Authorized folders are unavailable.', true);
    return null;
  }
  const candidates = [...state.columns, ...state.managedRoots, ...state.navigationRoots]
    .map((entry, index) => creationDestinationForEntry(entry, index))
    .filter(Boolean)
    .filter((entry, index, all) => all.findIndex((candidate) => candidate.creationDestinationRef === entry.creationDestinationRef) === index);
  for (const candidate of candidates) {
    const name = candidate.creationDestinationName || 'folder';
    if (await uiModule.styledConfirm(`Create in ${name}?`, { title: 'Choose destination folder', confirmText: 'Use folder', cancelText: 'Next' })) return candidate;
  }
  setStatus('No authorized writable folder is available.', true);
  return null;
}

function chooseBrowserReportDestination(candidates, signal, noun = 'backup') {
  if (!candidates.length || signal?.aborted) return Promise.resolve(null);
  return new Promise(resolve => {
    const previousFocus = document.activeElement;
    const overlay = document.createElement('div');
    overlay.className = 'modal';
    overlay.dataset.browserReportDestinationPicker = '';
    overlay.style.zIndex = '10000';
    overlay.innerHTML = '<div class="modal-content styled-confirm-box" role="dialog" aria-modal="true" aria-labelledby="browser-report-destination-title"><div class="modal-header"><h4 id="browser-report-destination-title">Choose Files destination</h4></div><div class="modal-body"><p>Choose an authorized writable folder for this backup.</p><div data-browser-report-destination-list role="listbox" aria-label="Writable Files folders"></div></div><div class="modal-footer"><button type="button" class="confirm-btn confirm-btn-secondary" data-browser-report-cancel>Cancel</button></div></div>';
    overlay.querySelector('.modal-content').style.zIndex = '10001';
    overlay.querySelector('.modal-body p').textContent = `Choose an authorized writable folder for this ${noun}.`;
    const list = overlay.querySelector('[data-browser-report-destination-list]');
    list.style.display = 'grid';
    list.style.gap = '0.5rem';
    for (const row of candidates) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'files-toolbar-button';
      button.style.width = '100%';
      button.style.textAlign = 'left';
      button.setAttribute('role', 'option');
      button.textContent = row.label;
      button.addEventListener('click', () => finish(row.candidate));
      list.append(button);
    }
    const cancel = overlay.querySelector('[data-browser-report-cancel]');
    cancel.addEventListener('click', () => finish(null));
    function onBackdrop(event) { if (event.target === overlay) finish(null); }
    function onKey(event) { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); finish(null); } }
    function finish(value) {
      if (!overlay.isConnected) return;
      overlay.remove();
      overlay.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      signal?.removeEventListener('abort', onAbort);
      try { previousFocus?.focus?.(); } catch {}
      resolve(value);
    }
    function onAbort() { finish(null); }
    overlay.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    signal?.addEventListener('abort', onAbort, { once:true });
    document.body.append(overlay);
    overlay.classList.remove('hidden');
    overlay.style.display = '';
    overlay.querySelector('[role="option"]')?.focus();
  });
}

async function pickBrowserReportImportTarget({ signal = null, isCurrent = () => true, noun = 'backup' } = {}) {
  const current = () => !signal?.aborted && isCurrent();
  if (!current()) return null;
  const scope = { owner:state.owner, workspace:String(state.currentWorkspaceId || copalWorkspace()),
    generation:filesPolicyGeneration(), lifecycle:state.lifecycleGeneration };
  const abortNavigation = () => state.navigationController?.abort();
  signal?.addEventListener('abort', abortNavigation, { once:true });
  try { await loadNavigationRoots({ force: true }); } catch (error) {
    if (signal?.aborted || !isCurrent()) return null;
    setStatus(error?.message || 'Authorized import folders are unavailable.', true);
    return null;
  } finally {
    signal?.removeEventListener('abort', abortNavigation);
  }
  if (!current()) return null;
  const scopeIsCurrent = () => state.owner === scope.owner
    && String(state.currentWorkspaceId || copalWorkspace()) === scope.workspace
    && filesPolicyGeneration() === scope.generation && state.lifecycleGeneration === scope.lifecycle
    && !listeners.signal.aborted;
  if (!scopeIsCurrent()) { setStatus('Files access changed while loading import folders.', true); return null; }
  const entries = [...state.columns, ...state.managedRoots, ...state.navigationRoots];
  const candidates = entries.map((entry, index) => ({ entry, candidate: creationDestinationForEntry(entry, index) }))
    .filter(row => row.candidate && row.entry.capabilities?.includes('children') && row.entry.capabilities?.includes('write'))
    .filter((row, index, all) => all.findIndex(other => other.candidate.creationDestinationRef === row.candidate.creationDestinationRef) === index);
  const selected = await chooseBrowserReportDestination(candidates.map(({ entry, candidate }) => ({
    candidate, label:String(candidate.creationDestinationName || entry.provenance?.logical_path || entry.name || 'Files folder'),
  })), signal, noun);
  if (!scopeIsCurrent() || !current()) { setStatus('Browser or Files access changed while choosing an import folder.', true); return null; }
  if (selected) return selected;
  setStatus(`Choose an authorized writable Files folder before importing the ${noun === 'PDF' ? 'PDF' : 'report'}.`, true);
  return null;
}

async function importBrowserReportFile(file, { signal = null, isCurrent = () => true, purpose = 'report', beforeDispatch = null, videoFrameSource = null } = {}) {
  const noun = purpose === 'pdf' ? 'PDF' : purpose === 'video-frame' ? 'PNG frame copy' : purpose === 'widget-state' ? 'widget state copy' : 'backup';
  if (!file || typeof file.name !== 'string' || !file.size) throw new TypeError(`A non-empty ${purpose === 'pdf' ? 'PDF' : purpose === 'video-frame' ? 'PNG frame copy' : purpose === 'widget-state' ? 'widget state copy' : 'report backup'} file is required.`);
  const current = () => !signal?.aborted && isCurrent() && !listeners.signal.aborted;
  if (!current()) return { committed:0, cancelled:true };
  if (options.pickerMode || listeners.signal.aborted) throw new Error('Files import is unavailable in this pane.');
  if (!state.nativeWindow?.visible) { await api.open(); api.activate(); }
  if (!current()) return { committed:0, cancelled:true };
  const captured = await pickBrowserReportImportTarget({ signal, isCurrent:current, noun });
  if (!captured) return { committed: 0, cancelled: true };
  const owner = captured.creationOwner;
  const workspace = captured.creationWorkspace;
  const generation = captured.creationGeneration;
  const lifecycle = captured.creationLifecycle;
  const content = captured.creationContent;
  const destinationRef = captured.creationDestinationRef;
  const filesScopeCurrent = () => current() && state.nativeWindow?.visible
    && state.owner === owner && String(state.currentWorkspaceId || copalWorkspace()) === workspace
    && filesPolicyGeneration() === generation && state.lifecycleGeneration === lifecycle
    && state.contentGeneration === content;
  if (!filesScopeCurrent()) {
    setStatus('Files access or destination changed; start the import again.', true);
    return { committed: 0, stale: true };
  }
  const destinationEntry = [...state.columns, ...state.managedRoots, ...state.navigationRoots]
    .find(entry => destinationResourceRef(entry) === destinationRef);
  if (!destinationEntry || !destinationEntry.capabilities?.includes('children') || !destinationEntry.capabilities?.includes('write')) {
    setStatus('The selected Files destination is no longer writable.', true);
    return { committed: 0, stale: true };
  }
  const collisionChoice = await uiModule.styledConfirm(
    `If a file with this name already exists, import the ${noun === 'PDF' ? 'PDF' : purpose === 'video-frame' ? 'PNG frame copy' : purpose === 'widget-state' ? 'widget state copy' : 'report backup'} with a unique name?`,
    { title: 'Choose collision policy', confirmText: 'Rename conflict', cancelText: 'Fail on conflict', alternateText:'Cancel import' },
  );
  if (collisionChoice === 'alternate') return { committed: 0, cancelled: true };
  if (!filesScopeCurrent()) {
    setStatus('Files access or destination changed; start the import again.', true);
    return { committed: 0, stale: true };
  }
  if (beforeDispatch) {
    if (typeof beforeDispatch !== 'function') throw new TypeError('Files import source check is unavailable.');
    let allowed = false;
    try { allowed = await beforeDispatch() === true; } catch (_) {}
    if (!allowed || !filesScopeCurrent()) {
      setStatus('The source or Files destination changed; start the import again.', true);
      return { committed:0, stale:true };
    }
  }
  const destinationIndex = state.columns.findIndex(entry => destinationResourceRef(entry) === destinationRef);
  const result = await importDroppedFiles([file], { ...destinationEntry, resource_ref: destinationRef, kind: 'folder' },
    destinationIndex, { collision: collisionChoice === true ? 'rename' : 'fail', refreshDestination: destinationIndex >= 0,
      signal, isCurrent:filesScopeCurrent, ...(purpose==='video-frame' && videoFrameSource ? {videoFrameSource} : {}) });
  return { ...(result || {}), submitted:result?.submitted === true };
}

async function reconcileBrowserReportImport(receiptHandles, { signal = null, isCurrent = () => true } = {}) {
  if (!Array.isArray(receiptHandles) || !receiptHandles.length) return { unknown:true, receiptHandles:[] };
  const results = [];
  for (const handle of receiptHandles) {
    if (signal?.aborted || !isCurrent()) return { unknown:true, receiptHandles };
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 5_000);
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once:true });
    try {
      const receipt = await filesFacadeClient.operationReceipt(handle.operationId, { signal:controller.signal, generation:handle.generation });
      const parsed = importReceiptItem(receipt, handle.itemId, { operationId:handle.operationId, generation:handle.generation });
      results.push({ ...handle, ...parsed });
    } catch (error) {
      results.push({ ...handle, outcome:'unknown', terminal:false, committed:false, error:error?.name || 'receipt_unavailable' });
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener('abort', abort);
    }
  }
  const pending = results.filter(row => !row.terminal);
  const committed = results.filter(row => row.committed).length;
  const rejected = results.filter(row => row.terminal && !row.committed).length;
  return { committed, rejected, pending:pending.length, receiptHandles:pending, terminal:pending.length === 0, recoveryUnavailable:results.some(row=>row.committed && row.recoveryUnavailable) };
}

function captureFilesPasteDestination(node) {
  const captured = creationCaptureForNode(node);
  if (!captured) return null;
  const column = state.columns[captured.creationDestinationIndex];
  return Object.freeze({ ...captured, content: state.contentGeneration,
    columnRef: destinationResourceRef(column) });
}

function currentFilesPasteDestination(captured) {
  if (!captured || options.pickerMode || listeners.signal.aborted || !state.nativeWindow?.visible
    || captured.creationOwner !== state.owner
    || captured.creationWorkspace !== String(state.currentWorkspaceId || copalWorkspace())
    || captured.creationGeneration !== filesPolicyGeneration()
    || captured.creationLifecycle !== state.lifecycleGeneration || captured.content !== state.contentGeneration) return null;
  const column = state.columns[captured.creationDestinationIndex];
  if (!column || destinationResourceRef(column) !== captured.columnRef) return null;
  const target = captured.creationDestinationRef === captured.columnRef ? column
    : column.entries?.find(entry => entry.resource_id === captured.creationDestinationId
      && entry.resource_ref === captured.creationDestinationRef);
  if (!target || String(target.provider || state.provider) !== captured.creationProvider
    || !isDirectory(target) || !target.capabilities?.includes('write')) return null;
  return { ...target, resource_ref: captured.creationDestinationRef, kind: 'folder' };
}

function filesCopyListingMetadata(entry) {
  return JSON.stringify([entry?.kind ?? null, entry?.size ?? null, entry?.modified_unix_ms ?? null]);
}

function currentContextCopySources(captured) {
  if (!captured?.filesParentId || !captured.filesSources?.length || listeners.signal.aborted
    || !state.nativeWindow?.visible || captured.filesPane !== (state.nativeWindow?.id || 'files-window')
    || captured.filesOwner !== state.owner || captured.filesGeneration !== filesPolicyGeneration()
    || captured.filesWorkspace !== String(state.currentWorkspaceId || copalWorkspace())
    || captured.filesLifecycle !== state.lifecycleGeneration || captured.filesContent !== state.contentGeneration) return null;
  const column = state.columns[captured.filesColumnIndex];
  if (!column || String(column.resource_id || column.resourceId || '') !== captured.filesParentId
    || String(column.provider || state.provider) !== captured.filesSourceProvider
    || String(column.query || '') !== captured.filesQuery) return null;
  const model = ensureSelectionModel(column, captured.filesColumnIndex);
  const entries = captured.filesSources.map(source => {
    const entry = model.item(source.key);
    if (!entry || String(entry.resource_id || entry.resourceId || '') !== source.id
      || String(entry.provider || model.scope.provider) !== captured.filesSourceProvider || !entry.resource_ref
      || filesCopyListingMetadata(entry) !== source.metadata || !transferCapabilities(entry).copy) return null;
    if (source.revision) {
      if (!entry.revision || entry.revision.kind !== source.revision.kind || entry.revision.value !== source.revision.value) return null;
    } else if (entry !== source.entry || entry.resource_ref !== source.ref || entry.revision) return null;
    return entry;
  });
  if (entries.some(entry => !entry) || !captured.filesSources.some(source => source.key === captured.targetKey)) return null;
  return { model, entries };
}

function setFilesClipboard(model, kind, current, capturedEntries = null, verifiedRevisions = null) {
  if (!current()) throw new Error('The source Files selection changed; copy the files again.');
  const entries = capturedEntries || model?.selectedEntries() || [];
  if (!entries.length || entries.length > FILES_MAX_DRAG_ITEMS
    || !entries.every(entry => transferCapabilities(entry)[kind])) throw new Error('The selection cannot be copied or cut.');
  // Choosing a Paste target changes the visible selection. Keep the copied
  // selection private, while retaining the original scope and source authority.
  const sourceScope = model.snapshot().scopeKey;
  const sourceKeys = entries.map(entry => resourceKey(entry, model.scope.provider, model.scope));
  const owner = state.owner, workspace = String(state.currentWorkspaceId || copalWorkspace());
  const policy = filesPolicyGeneration(), lifecycle = state.lifecycleGeneration, content = state.contentGeneration;
  const pane = state.nativeWindow?.id || 'files-window';
  const observed = entries.map(entry => ({ ref: entry.resource_ref, metadata: filesCopyListingMetadata(entry),
    revision: entry.revision ? { ...entry.revision } : null }));
  const copiedEntries = entries.map((entry, index) => ({ ...entry, capabilities: [...(entry.capabilities || [])],
    revision: verifiedRevisions?.[index] ? { ...verifiedRevisions[index] } : observed[index].revision }));
  const copiedModel = createFilesSelectionModel({ scope: model.scope });
  copiedModel.setItems(copiedEntries);
  copiedModel.replaceSelection(sourceKeys);
  const payload = transferPayloadForModel(copiedModel, kind);
  if (!payload) throw new Error('The selection cannot be encoded for transfer.');
  const sourceCurrent = () => !listeners.signal.aborted && state.nativeWindow?.visible
    && (state.nativeWindow?.id || 'files-window') === pane && state.owner === owner
    && String(state.currentWorkspaceId || copalWorkspace()) === workspace
    && filesPolicyGeneration() === policy && state.lifecycleGeneration === lifecycle && state.contentGeneration === content
    && state.selectionModels.get(sourceScope) === model && model.snapshot().scopeKey === sourceScope
    && sourceKeys.every((key, index) => {
      const entry = model.item(key), original = observed[index];
      return entry === entries[index] && entry.resource_ref === original.ref
        && filesCopyListingMetadata(entry) === original.metadata
        && (entry.revision?.kind || '') === (original.revision?.kind || '')
        && (entry.revision?.value || '') === (original.revision?.value || '')
        && transferCapabilities(entry)[kind];
    });
  filesClipboard = { payload, model: copiedModel, source: api, current: sourceCurrent };
  setStatus(`${entries.length} item${entries.length === 1 ? '' : 's'} ready to ${kind}`);
  return true;
}

async function pasteFilesClipboard(destination, destinationIndex, current, clipboard = filesClipboard) {
  if (!current()) throw new Error('The destination folder changed; choose Paste again.');
  if (clipboard !== filesClipboard || !clipboard?.current()) throw new Error('The copied source changed. Copy the files again.');
  if (!destination?.capabilities?.includes('children') || !destination.capabilities.includes('write')
    || clipboard.payload.provider !== destination.provider) throw new Error('Paste requires a writable folder in the same provider.');
  const response = await executeFilesTransfer(clipboard.payload, destination, 'fail', destinationIndex, {
    model:clipboard.model, isCurrent:() => current() && clipboard === filesClipboard && clipboard.current(),
  });
  clipboard.source.updateTransferSelection(clipboard.model);
  if ((response?.items || []).length && response.items.every(item => ['committed','unchanged'].includes(item.outcome))) await clipboard.source.refresh();
  if (filesClipboard === clipboard && response) filesClipboard = null;
  return true;
}

function captureTreeCommands(tree, id, entry = tree.getNode(id)?.data) {
  if (!entry) return null;
  cancelReveal();
  state.treeSelection = entry;
  tree.setActive(id);
  options.onSelection?.(entry);
  updateManagedActionControl();
  return captureCommands({ context:'selection', treeEntry:entry, treeHit:{ tree, id } });
}

function sharedFilesMenuAdapter() {
  return {
    ownsObjectCommands:true,groupCommands:true,editingCommands:false,
    hitTest:target => {
      if (target.closest?.('[data-explorer-toggle]')) return 'native';
      if (target.closest?.('[data-files-selection-key], [data-files-row-actions], .files-tree-item[data-explorer-id], [data-files-explorer-heading]')) return true;
      return !target.closest?.('input, textarea, select, button, a, [contenteditable]') &&
        Boolean(target.closest?.('[data-files-body]') || target.matches?.('[data-files-collections]'));
    },
    capture:target=>{
      const treeItem = target.closest?.('.files-tree-item[data-explorer-id]');
      if (treeItem) {
        const trees = [state.explorerTree, state.workspaceTree, state.favoriteTree, state.collectionTree, state.editorRootTree];
        const tree = trees.find(candidate => candidate?.container.contains(treeItem));
        if (tree) return captureTreeCommands(tree, treeItem.dataset.explorerId);
      }
      if (target.closest?.('[data-files-explorer-heading]') || target.matches?.('[data-files-collections]')) {
        return captureCommands({ context:'navigation' });
      }
      const row = target.closest?.('[data-files-selection-key]') ||
        target.closest?.('.files-entry-frame')?.querySelector('[data-files-selection-key]');
      if (!target.closest?.('[data-files-body]')) return null;
      if (row) {
        const captured = hooks.__openClankFilesContextCapture(row);
        return captured ? captureCommands({ context:'selection', columnIndex:captured.filesColumnIndex, treeEntry:null }) : null;
      }
      state.treeSelection = null;
      return captureCommands({ context:'folder', columnIndex:columnIndexForNode(target), treeEntry:null });
    },
    focusOrigin:request => request.rawTarget?.closest?.('[data-files-row-actions], [data-files-selection-key], .files-tree-item, [tabindex]'),
    isCurrent:request=>isCommandCaptureCurrent(request?.adapterContext),
    commands:request=>request?.adapterContext?contextCommandDescriptors(request.adapterContext):[],
    execute:async(command,request)=>{
      const captured=request?.adapterContext;
      if(!isCommandCaptureCurrent(captured))throw new Error('The captured Files target changed. Reopen its menu.');
      const descriptor=contextCommandDescriptors(captured).find(item=>item.id===command);
      if(!descriptor)return false;
      if(descriptor.disabledReason)throw new Error(descriptor.disabledReason);
      await descriptor.run();return true;
    },
  };
}


async function openEntryInCanvas(entry, model, commandId = operationId('files-canvas')) {
  const handoff = canvasPayloadForEntry(entry, model, commandId);
  if (!handoff) { setStatus('Select one authorized image to open in Canvas.', true); return false; }
  const readContext = () => {
    if (state.selectionModels.get(handoff.context.scopeKey)!==model || model.epoch!==handoff.context.selectionEpoch) return null;
    const selected=model.selectedEntries();
    if (selected.length!==1 || entrySelectionKey(selected[0])!==entrySelectionKey(entry)) return null;
    return filesCanvasGestureContext(selected[0],model,commandId);
  };
  const editor=window.copalModule || await import('./copal.js');
  if (typeof editor?.openFilesImageInBlank!=='function') throw new Error('Canvas image import is unavailable.');
  const result=await editor.openFilesImageInBlank({payload:handoff.payload,context:handoff.context,getContext:readContext,name:`Canvas · ${entry.name || 'Files image'}`});
  if (result?.status==='retained') setStatus('The image is retained in its original canvas for review.');
  else if (result?.status!=='imported' && result?.status!=='duplicate') throw new Error('Canvas image import did not complete.');
  return true;
}

function destinationColumnIndex(destination, fallback = state.activeColumnIndex) {
  const index = state.columns.findIndex((column) => column === destination ||
    (destination?.resource_ref && column?.resourceRef === destination.resource_ref));
  return index >= 0 ? index : (Number.isInteger(fallback) && fallback >= 0 ? fallback : state.columns.length - 1);
}

// Resolve a drop target from the event's actual column panel.  In Columns a
// blank list has no entry node, but it still belongs to a concrete panel.  Do
// not activate that panel merely to infer a destination: activation changes
// selection/focus state and made earlier-column background drops target the
// last active column.
function destinationForTarget(target) {
  const index = columnIndexForNode(target);
  if (state.mode === 'columns') {
    const column = state.columns[index];
    return { column: column || null, index };
  }
  return { column: activeColumn(), index: state.activeColumnIndex >= 0 ? state.activeColumnIndex : state.columns.length - 1 };
}

function destinationResourceRef(destination) {
  return String(destination?.resource_ref || destination?.resourceRef || destination?.ref || '').trim();
}

async function readExternalDirectoryEntries(directory) {
  const reader = directory?.createReader?.();
  if (!reader) throw Object.assign(new Error('This browser cannot enumerate dropped folders.'), { code: 'directory_api_unavailable' });
  const entries = [];
  while (true) {
    const batch = await new Promise((resolve, reject) => reader.readEntries(resolve, reject));
    if (!batch?.length) return entries;
    entries.push(...batch);
    if (entries.length > MAX_EXTERNAL_DROP_ENTRIES) throw Object.assign(new Error('The dropped folder contains too many entries.'), { code: 'directory_too_large' });
  }
}

async function readExternalFile(entry) {
  return new Promise((resolve, reject) => {
    try { entry.file(resolve, reject); } catch (error) { reject(error); }
  });
}

async function collectExternalDirectory(entry, parts, output, failures, depth = 0, budget = { count: 0 }) {
  if (depth > MAX_EXTERNAL_DROP_DEPTH) {
    failures.push({ name: parts.join('/'), code: 'directory_depth_exceeded' });
    return;
  }
  const directoryPath = safeExternalDropPath(parts);
  if (!directoryPath) {
    failures.push({ name: parts.join('/'), code: 'unsafe_relative_path' });
    return;
  }
  output.directories.push({ name: parts.at(-1), relativePath: directoryPath, directory: true });
  let children;
  try { children = await readExternalDirectoryEntries(entry); }
  catch (error) {
    failures.push({ name: directoryPath, code: error?.code || 'directory_read_failed' });
    return;
  }
  for (const child of children) {
    budget.count += 1;
    if (budget.count > MAX_EXTERNAL_DROP_ENTRIES) {
      failures.push({ name: parts.join('/'), code: 'directory_too_large' });
      return;
    }
    const childParts = [...parts, child?.name];
    if (!safeExternalDropPath(childParts)) {
      failures.push({ name: childParts.join('/'), code: 'unsafe_relative_path' });
      continue;
    }
    if (child?.isDirectory) await collectExternalDirectory(child, childParts, output, failures, depth + 1, budget);
    else if (child?.isFile) {
      try {
        const file = await readExternalFile(child);
        output.files.push({ file, name: safeExternalDropSegment(file.name), relativePath: childParts.join('/') });
      } catch (_) { failures.push({ name: childParts.join('/'), code: 'file_read_failed' }); }
    }
  }
}

async function collectExternalDirectoryHandle(handle, parts, output, failures, depth = 0, budget = { count: 0 }) {
  if (depth > MAX_EXTERNAL_DROP_DEPTH) {
    failures.push({ name: parts.join('/'), code: 'directory_depth_exceeded' });
    return;
  }
  const directoryPath = safeExternalDropPath(parts);
  if (!directoryPath) {
    failures.push({ name: parts.join('/'), code: 'unsafe_relative_path' });
    return;
  }
  output.directories.push({ name: parts.at(-1), relativePath: directoryPath, directory: true });
  try {
    for await (const [name, child] of handle.entries()) {
      budget.count += 1;
      if (budget.count > MAX_EXTERNAL_DROP_ENTRIES) {
        failures.push({ name: parts.join('/'), code: 'directory_too_large' });
        return;
      }
      const childParts = [...parts, name];
      if (!safeExternalDropPath(childParts)) {
        failures.push({ name: childParts.join('/'), code: 'unsafe_relative_path' });
        continue;
      }
      if (child.kind === 'directory') await collectExternalDirectoryHandle(child, childParts, output, failures, depth + 1, budget);
      else if (child.kind === 'file') {
        try {
          const file = await child.getFile();
          output.files.push({ file, name: safeExternalDropSegment(file.name), relativePath: childParts.join('/') });
        } catch (_) { failures.push({ name: childParts.join('/'), code: 'file_read_failed' }); }
      }
    }
  } catch (_) { failures.push({ name: directoryPath, code: 'directory_read_failed' }); }
}

async function collectExternalDropFiles(dataTransfer) {
  const files = [];
  const directories = [];
  const failures = [];
  const seen = new Set();
  let directorySeen = false;
  const directoryBudget = { count: 0 };
  const items = [...(dataTransfer?.items || [])].filter((item) => item?.kind === 'file');
  for (const item of items) {
    let entry = null;
    try { entry = item.webkitGetAsEntry?.() || null; } catch (_) {}
    if (entry?.isDirectory) {
      directorySeen = true;
      await collectExternalDirectory(entry, [entry.name], { files, directories }, failures, 0, directoryBudget);
      continue;
    }
    let handle = null;
    try { handle = await item.getAsFileSystemHandle?.() || null; } catch (_) {}
    if (handle?.kind === 'directory') {
      directorySeen = true;
      await collectExternalDirectoryHandle(handle, [handle.name], { files, directories }, failures, 0, directoryBudget);
      continue;
    }
    try {
      const file = item.getAsFile?.() || (handle?.kind === 'file' ? await handle.getFile() : null);
      if (file) {
        const key = `${file.name}\0${file.size}\0${file.lastModified}`;
        if (!seen.has(key)) { seen.add(key); files.push({ file, name: safeExternalDropSegment(file.name), relativePath: file.name }); }
      }
    } catch (_) { failures.push({ name: 'dropped file', code: 'file_read_failed' }); }
  }
  for (const file of [...(dataTransfer?.files || [])]) {
    const relativePath = String(file.webkitRelativePath || '').replace(/\\/g, '/');
    if (relativePath) {
      const parts = relativePath.split('/');
      if (!safeExternalDropPath(parts)) { failures.push({ name: relativePath, code: 'unsafe_relative_path' }); continue; }
    }
    const key = `${file.name}\0${file.size}\0${file.lastModified}`;
    if (seen.has(key)) continue;
    seen.add(key);
    files.push({ file, name: safeExternalDropSegment(file.name), relativePath: relativePath || file.name });
  }
  const unique = [];
  const uniqueKeys = new Set();
  for (const item of files) {
    if (!item.name) { failures.push({ name: item.relativePath, code: 'unsafe_relative_path' }); continue; }
    const key = `${item.name}\0${item.file.size}\0${item.file.lastModified}`;
    if (uniqueKeys.has(key)) { failures.push({ name: item.relativePath, code: 'duplicate_drop_item' }); continue; }
    uniqueKeys.add(key);
    unique.push(item);
  }
  return { files: unique, directories, failures };
}

async function handleExternalDrop(dataTransfer, destination, destinationIndex) {
  if (options.pickerMode) return;
  const collected = await collectExternalDropFiles(dataTransfer);
  if (!collected.files.length && !collected.directories.length) {
    setStatus(`Nothing was imported (${collected.failures[0]?.code || 'directory_read_failed'}).`, true);
    return;
  }
  await importDroppedFiles(collected.files, destination, destinationIndex, { skipped: collected.failures, directories: collected.directories });
}

function removeSelectionRectangle() {
  state.gesture?.overlay?.remove?.();
  if (state.gesture) state.gesture.overlay = null;
}

function cancelFilesGesture(reason = 'cancelled') {
  const gesture = state.gesture;
  if (!gesture) return false;
  if (gesture.frame) cancelAnimationFrame(gesture.frame);
  if (gesture.autoFrame) cancelAnimationFrame(gesture.autoFrame);
  try { gesture.body?.releasePointerCapture?.(gesture.pointerId); } catch (_) {}
  removeSelectionRectangle();
  state.gesture = null;
  if (reason !== 'completed') setStatus('Selection cancelled');
  return true;
}

function updateSelectionRectangle() {
  const gesture = state.gesture;
  if (!gesture || gesture.type === 'drag') return;
  gesture.frame = 0;
  const livePanel = gesture.columnIndex >= 0
    ? gesture.body.querySelector(`.files-column[data-column-index="${gesture.columnIndex}"]`)
    : null;
  gesture.columnPanel = livePanel || gesture.columnPanel;
  gesture.viewport = livePanel?.querySelector('.files-column-list') || (gesture.columnIndex < 0 ? gesture.body : gesture.viewport);
  const left = Math.min(gesture.startX, gesture.currentX);
  const right = Math.max(gesture.startX, gesture.currentX);
  const top = Math.min(gesture.startY, gesture.currentY);
  const bottom = Math.max(gesture.startY, gesture.currentY);
  Object.assign(gesture.overlay.style, { left: `${left}px`, top: `${top}px`, width: `${right - left}px`, height: `${bottom - top}px` });
  const boxRoot = gesture.columnIndex >= 0
    ? gesture.body.querySelector(`.files-column[data-column-index="${gesture.columnIndex}"]`) || gesture.body
    : gesture.body;
  const cachedBoxes = gesture.body.__filesSelectionBoxes;
  const boxes = cachedBoxes?.epoch === state.renderEpoch
    ? cachedBoxes.boxes
    : [...boxRoot.querySelectorAll('[data-files-selection-key]')].map((node) => ({ key: node.dataset.filesSelectionKey, ...node.getBoundingClientRect().toJSON() }));
  gesture.body.__filesSelectionBoxes = { epoch:state.renderEpoch, boxes };
  const hit = rectangleSelection(boxes, { left, right, top, bottom }, { additive: gesture.additive, toggle: gesture.toggle });
  const model = ensureSelectionModel(gesture.column, gesture.columnIndex);
  // The rectangle is a live query. Recompute its hits on every frame so rows
  // that leave a shrinking or moving rectangle are removed from the gesture's
  // result while the immutable starting selection remains intact.
  gesture.hitKeys = new Set(hit.keys);
  const next = new Set(applyRectangleSelection(gesture.baseSelection, gesture.hitKeys, { toggle: gesture.toggle }));
  model.replaceSelection([...next], { anchor: gesture.anchor, focus: gesture.focus });
  state.selected = new Set(model.selectedKeys());
  syncVisibleSelection(model, gesture.body);
}

function queueSelectionRectangle(event) {
  const gesture = state.gesture;
  if (!gesture || gesture.type === 'drag') return;
  gesture.currentX = event.clientX; gesture.currentY = event.clientY;
  if (!gesture.frame) gesture.frame = requestAnimationFrame(updateSelectionRectangle);
  const edge = 28;
  const viewportNode = gesture.viewport || gesture.body;
  const viewport = viewportNode.getBoundingClientRect();
  const speed = 18;
  gesture.edgeDirection = event.clientY < viewport.top + edge ? -1 : event.clientY > viewport.bottom - edge ? 1 : 0;
  if (gesture.edgeDirection && !gesture.autoFrame) gesture.autoFrame = requestAnimationFrame(rectangleAutoScrollFrame);
}

function rectangleAutoScrollFrame() {
  const gesture = state.gesture;
  if (!gesture || gesture.type !== 'rectangle' || !gesture.edgeDirection) {
    if (gesture) gesture.autoFrame = 0;
    return;
  }
  const livePanel = gesture.columnIndex >= 0
    ? gesture.body.querySelector(`.files-column[data-column-index="${gesture.columnIndex}"]`)
    : null;
  gesture.columnPanel = livePanel || gesture.columnPanel;
  gesture.viewport = livePanel?.querySelector('.files-column-list') || (gesture.columnIndex < 0 ? gesture.body : gesture.viewport);
  const viewport = gesture.viewport || gesture.body;
  viewport.scrollTop = Math.max(0, viewport.scrollTop + gesture.edgeDirection * 18);
  gesture.body.__filesSelectionBoxes = null;
  updateSelectionRectangle();
  gesture.autoFrame = gesture.edgeDirection ? requestAnimationFrame(rectangleAutoScrollFrame) : 0;
}

function beginSelectionRectangle(event, body) {
  if (event.button !== 0 || blockedGestureTarget(event.target)) return false;
  const columnIndex = columnIndexForNode(event.target);
  const selectedColumn = activateColumn(columnIndex);
  const model = selectedColumn.model;
  const additive = !!(event.metaKey || event.ctrlKey);
  const toggle = additive;
  if (!additive) model.clear();
  const overlay = el('div', { class: 'files-selection-rectangle', 'aria-hidden': 'true' });
  const columnPanel = event.target?.closest?.('.files-column') || null;
  const viewport = columnPanel?.querySelector('.files-column-list') || body;
  body.append(overlay);
  state.gesture = {
    type: 'rectangle',
    body, column: selectedColumn.column, columnIndex: selectedColumn.index, columnPanel, viewport,
    pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
    currentX: event.clientX, currentY: event.clientY, additive, toggle,
    baseSelection: additive ? [...state.selected] : [], hitKeys: new Set(), edgeDirection: 0, autoFrame: 0, anchor: model.anchorKey, focus: model.focusKey, overlay, frame: 0,
  };
  try { body.setPointerCapture(event.pointerId); } catch (_) {}
  event.preventDefault();
  return true;
}

function installFilesGestures(body) {
  if (!body || body.dataset.filesGesturesInstalled) return;
  body.dataset.filesGesturesInstalled = 'true';
  body.addEventListener('scroll', () => {
    if (state.gesture?.type === 'rectangle') {
      const gesture = state.gesture;
      if (gesture.viewport === body && !body.dataset.filesVirtualFrame) {
        body.dataset.filesVirtualFrame = 'true';
        requestAnimationFrame(() => { body.dataset.filesVirtualFrame = ''; renderEntries(); updateSelectionRectangle(); });
      }
      return;
    }
    if (state.entries.length <= 500 || body.dataset.filesVirtualFrame) return;
    body.dataset.filesVirtualFrame = 'true';
    requestAnimationFrame(() => { body.dataset.filesVirtualFrame = ''; renderEntries(); });
  }, { passive: true });
  body.addEventListener('pointerdown', (event) => {
    if (options.pickerMode && !event.target?.closest?.('[data-files-selection-key]')) { clearFilesSelection(); options.onSelection?.(null); return; }
    if (event.isPrimary === false || event.button !== 0) return;
    if (event.target?.closest?.('[data-files-selection-key]')) return;
    beginSelectionRectangle(event, body);
  });
  body.addEventListener('pointermove', queueSelectionRectangle);
  body.addEventListener('pointerup', (event) => {
    if (!state.gesture || event.pointerId !== state.gesture.pointerId) return;
    if (state.gesture.frame) updateSelectionRectangle();
    cancelFilesGesture('completed');
    // Rectangle updates already applied the model and visible row state. The
    // toolbar can follow on the next frame so pointerup stays responsive.
    requestAnimationFrame(() => updateManagedActionControl());
  });
  // Native HTML drag takes over the pointer stream and may cancel pointer
  // capture. Keep its registry handoff alive until dragend so sibling panes
  // can validate protected drag data during dragover.
  body.addEventListener('pointercancel', () => { if (state.gesture?.type !== 'drag') cancelFilesGesture('pointercancel'); });
  body.addEventListener('lostpointercapture', () => { if (state.gesture?.type !== 'drag') cancelFilesGesture('lost-capture'); });
  body.addEventListener('dragstart', (event) => {
    if (options.pickerMode) { event.preventDefault(); return; }
    const node = event.target?.closest?.('[data-files-selection-key]');
    if (!node || !event.dataTransfer) return;
    const key = node.dataset.filesSelectionKey;
    const selectedColumn = activateColumn(columnIndexForNode(node));
    const model = selectedColumn.model;
    if (!model.has(key)) { model.select(key, event); state.selected = new Set(model.selectedKeys()); }
    if (model.selectedKeys().length > FILES_MAX_DRAG_ITEMS) {
      event.preventDefault();
      setStatus(`Drag supports at most ${FILES_MAX_DRAG_ITEMS} selected items; use the context command for a larger batch.`, true);
      return;
    }
    const intent = transferIntent({ platform: navigator.platform, metaKey: event.metaKey, ctrlKey: event.ctrlKey, shiftKey: event.shiftKey, altKey: event.altKey });
    if (intent.intent === 'reject') { event.preventDefault(); setStatus(intent.reason, true); return; }
    const selectedEntries = model.selectedEntries();
    const singleCanvasImage = selectedEntries.length === 1 && isCanvasImageEntry(selectedEntries[0]);
    const payload = buildInternalDragPayload({
      scope: model.scope, generation: filesPolicyGeneration(),
      policyGeneration: filesPolicyGeneration(), owner: state.owner,
      selectionEpoch: model.epoch,
      pane: state.nativeWindow?.id || 'files-window', kind: intent.intent,
      entries: transferEntries(model), selectedKeys: model.selectedKeys(),
    });
    if (!payload?.sources?.length) {
      event.preventDefault();
      setStatus('These items cannot be dragged by Files.', true);
      return;
    }
    const sourceCapabilities = model.selectedEntries().map(transferCapabilities);
    const allowedMove = sourceCapabilities.length > 0 && sourceCapabilities.every((capability) => capability.move);
    const allowedCopy = sourceCapabilities.length > 0 && sourceCapabilities.every((capability) => capability.copy);
    // A plain drag of one readable image is also a Canvas handoff. Keep the
    // Files payload's default move intent so a Files destination still rejects
    // an unavailable move; Canvas consumes its separate MIME as a copy.
    const canvasDefaultCopy = singleCanvasImage && intent.intent === 'move' && !allowedMove && allowedCopy;
    if (!allowedMove && !allowedCopy) {
      event.preventDefault();
      setStatus('These items cannot be moved or copied by Files.', true);
      return;
    }
    if ((intent.intent === 'move' && !allowedMove && !canvasDefaultCopy) || (intent.intent === 'copy' && !allowedCopy)) {
      event.preventDefault();
      setStatus(`${intent.intent === 'move' ? 'Move' : 'Copy'} is unavailable for these items.`, true);
      return;
    }
    state.gesture = { type: 'drag', payload, epoch: model.epoch, renderEpoch: state.renderEpoch, commandId: operationId('files-drag'), scopeKey: model.snapshot().scopeKey, column: selectedColumn.column, columnIndex: selectedColumn.index, allowedMove, allowedCopy };
    // Permit a later modifier change to select either supported effect. The
    // drop target still computes and validates the actual current intent.
    event.dataTransfer.effectAllowed = canvasDefaultCopy ? 'copy' : allowedMove && allowedCopy ? 'copyMove' : allowedCopy ? 'copy' : 'move';
    event.dataTransfer.setData(FILES_TRANSFER_MIME, JSON.stringify(payload));
    // An image drag has a separate, strict handoff for Canvas. It is emitted
    // only for one currently selected image and carries the same immutable
    // Files gesture context as the generic drag operation.
    if (model.selectedEntries().length === 1) {
      const imageEntry = model.selectedEntries()[0];
      const handoff = canvasPayloadForEntry(imageEntry, model, state.gesture.commandId);
      if (handoff) event.dataTransfer.setData(FILES_CANVAS_IMAGE_MIME, JSON.stringify(handoff.payload));
    }
    // Generic text is a display value only. Sealed refs never enter URI/text clipboard lanes.
    event.dataTransfer.setData('text/plain', `${payload.sources.length} selected Files item${payload.sources.length === 1 ? '' : 's'}`);
    setStatus(`${payload.sources.length} item${payload.sources.length === 1 ? '' : 's'} · ${canvasDefaultCopy ? 'copy to Canvas' : intent.intent}`);
  });
  body.addEventListener('dragend', () => {
    if (state.gesture?.type === 'drag') cancelFilesGesture('completed');
  });
  body.addEventListener('dragover', (event) => {
    if (options.pickerMode) { event.preventDefault(); if (event.dataTransfer) event.dataTransfer.dropEffect = 'none'; return; }
    if (!event.dataTransfer) return;
    const target = event.target?.closest?.('[data-files-selection-key]');
    const entry = target && contextEntryForNode(target);
    const payload = state.gesture?.payload || [...browsers.values()].map(browser => browser.getDragPayload()).find(Boolean) || (() => {
      try { return parseInternalDragPayload(event.dataTransfer.getData(FILES_TRANSFER_MIME)); } catch (_) { return null; }
    })();
    if (!payload) {
      const fileItems = [...(event.dataTransfer.items || [])].filter((item) => item?.kind === 'file');
      const hasFiles = Boolean(event.dataTransfer.files?.length || fileItems.length);
      if (!hasFiles) return;
      const targetEntry = target && contextEntryForNode(target);
      const targetInfo = destinationForTarget(event.target);
      const destination = targetEntry && isDirectory(targetEntry) ? targetEntry : targetInfo.column;
      const acceptsImport = Boolean(destinationResourceRef(destination) && (destination.capabilities || []).includes('children') && (destination.capabilities || []).includes('write'));
      const directory = [...(event.dataTransfer.items || [])].some((item) => {
        try { return item.webkitGetAsEntry?.()?.isDirectory === true; } catch (_) { return false; }
      });
      event.preventDefault();
      event.dataTransfer.dropEffect = !acceptsImport ? 'none' : 'copy';
      const count = event.dataTransfer.files?.length || fileItems.length;
      setStatus(directory ? 'Folder · import readable files' : !acceptsImport ? 'This folder does not accept imported files.' : `${count} file${count === 1 ? '' : 's'} · import` , !acceptsImport);
      return;
    }
    const targetInfo = destinationForTarget(event.target);
    const destination = entry && isDirectory(entry) ? entry : targetInfo.column;
    if (!destination || !isDirectory(destination)) return;
    const capabilities = new Set(destination.capabilities || []);
    const intent = transferIntent({ platform: navigator.platform, metaKey: event.metaKey, ctrlKey: event.ctrlKey, shiftKey: event.shiftKey, altKey: event.altKey });
    if (intent.intent === 'reject') { event.dataTransfer.dropEffect = 'none'; setStatus(intent.reason, true); return; }
    const gesture = state.gesture?.type === 'drag' ? state.gesture : null;
    if (gesture && ((intent.intent === 'move' && !gesture.allowedMove) || (intent.intent === 'copy' && !gesture.allowedCopy))) {
      event.dataTransfer.dropEffect = 'none';
      setStatus(`${intent.intent === 'move' ? 'Move' : 'Copy'} is unavailable for these items.`, true);
      return;
    }
    const sourceModel = browsers.get(payload.pane)?.resolveTransferModel(payload);
    if (!sourceModel) { event.dataTransfer.dropEffect = 'none'; return; }
    if (!sourceModel.selectedEntries().every(item => transferCapabilities(item)[intent.intent])) {
      event.dataTransfer.dropEffect = 'none';
      setStatus(`${intent.intent === 'move' ? 'Move' : 'Copy'} is unavailable for these items.`, true);
      return;
    }
    const effectivePayload = { ...payload, kind: intent.intent };
    const validation = validateDropTarget(effectivePayload, { resource_ref: destinationResourceRef(destination), resource_key: entrySelectionKey(destination) }, {
      generation: filesPolicyGeneration(),
      policyGeneration: filesPolicyGeneration(),
      owner: state.owner, workspace: state.currentWorkspaceId || copalWorkspace(), pane: effectivePayload.pane,
      provider: destination.provider, allowMove: capabilities.has('children') && capabilities.has('write'), allowCopy: capabilities.has('children') && capabilities.has('write'),
    });
    if (!validation.ok) { event.dataTransfer.dropEffect = 'none'; return; }
    event.preventDefault(); event.dataTransfer.dropEffect = intent.intent;
    setStatus(`${validation.count} item${validation.count === 1 ? '' : 's'} · ${intent.intent} to ${destination.name || 'this folder'}`);
  });
  body.addEventListener('drop', (event) => {
    if (options.pickerMode) { event.preventDefault(); return; }
    const target = event.target?.closest?.('[data-files-selection-key]');
    const entry = target && contextEntryForNode(target);
    const raw = event.dataTransfer?.getData(FILES_TRANSFER_MIME);
    const payload = parseInternalDragPayload(raw) || state.gesture?.payload;
    const targetInfo = destinationForTarget(event.target);
    const destination = entry && isDirectory(entry) ? entry : targetInfo.column;
    if (payload && destination && isDirectory(destination)) {
      event.preventDefault();
      const intent = transferIntent({ platform: navigator.platform, metaKey: event.metaKey, ctrlKey: event.ctrlKey, shiftKey: event.shiftKey, altKey: event.altKey });
      if (intent.intent === 'reject') setStatus(intent.reason, true);
      else {
        const gesture = state.gesture?.type === 'drag' ? state.gesture : null;
        if (gesture && ((intent.intent === 'move' && !gesture.allowedMove) || (intent.intent === 'copy' && !gesture.allowedCopy))) {
          setStatus(`${intent.intent === 'move' ? 'Move' : 'Copy'} is unavailable for these items.`, true);
        } else void executeFilesTransfer({ ...payload, kind: intent.intent }, destination, 'fail', destinationColumnIndex(destination, targetInfo.index));
      }
    }
    else if (event.dataTransfer?.files?.length || [...(event.dataTransfer?.items || [])].some((item) => item?.kind === 'file')) {
      event.preventDefault();
      if (destination && isDirectory(destination)) void handleExternalDrop(event.dataTransfer, destination, destinationColumnIndex(destination, targetInfo.index));
      else setStatus('This Files view cannot import here.', true);
    }
    state.gesture = null;
  });
  listen(window, 'blur', () => cancelFilesGesture('window-hidden'));
  listen(document, 'visibilitychange', () => { if (document.hidden) cancelFilesGesture('window-hidden'); });
}

// Disposable fixture/diagnostic hook: reports only whether this window owns a
// live gesture frame, never selection authority or resource references.
hooks.__openClankFilesGestureSnapshot = () => Object.freeze({
  active: Boolean(state.gesture),
  frame: Number(state.gesture?.frame || 0),
  autoFrame: Number(state.gesture?.autoFrame || 0),
});

// S03 consumes this short-lived validation handoff while an Editor drop is in
// flight. It contains only immutable identity keys and boolean capabilities;
// sealed source refs remain inside the typed drag payload and never enter this
// cross-surface getter.
hooks.__openClankFilesTransferContext = () => {
  const gesture = state.gesture;
  if (!gesture || gesture.type !== 'drag') return null;
  const model = state.selectionModels.get(String(gesture.scopeKey));
  if (!model || model.epoch !== gesture.epoch || state.renderEpoch !== gesture.renderEpoch
    || state.owner !== gesture.payload.owner || filesPolicyGeneration() !== Number(gesture.payload.policy_generation)) return null;
  const selectedKeys = model.selectedKeys();
  const payloadKeys = gesture.payload.sources.map((source) => String(source.resource_key));
  if (selectedKeys.length !== payloadKeys.length || selectedKeys.some((key, index) => key !== payloadKeys[index])) return null;
  const sourceCapabilities = {};
  for (const entry of model.selectedEntries()) {
    const key = entrySelectionKey(entry);
    const capabilities = new Set(entry?.capabilities || []);
    sourceCapabilities[key] = Object.freeze({
      read: capabilities.has('read'), open: capabilities.has('open'), download: capabilities.has('download'), export: capabilities.has('export'),
    });
  }
  return Object.freeze({
    commandId: String(gesture.commandId), generation: Number(gesture.payload.generation),
    policyGeneration: Number(gesture.payload.policy_generation), selectionEpoch: model.epoch,
    owner: String(state.owner), workspace: String(gesture.payload.workspace), pane: String(gesture.payload.pane), provider: String(gesture.payload.provider),
    parent: String(model.scope.parentRef), scopeKey: String(model.scopeKey || gesture.scopeKey),
    selectedKeys: Object.freeze(selectedKeys), sourceCapabilities: Object.freeze(sourceCapabilities),
    allowCopy: Boolean(gesture.allowedCopy), allowMove: Boolean(gesture.allowedMove),
  });
};

// Item receipt IDs are bounded independently from scoped resource identities.
// Long owner/workspace keys must never become the receipt ID.
function transferEntries(model) {
  return model.selectedEntries().map(entry => ({ ...entry, item_id: operationId('files-item') }));
}

function operationId(prefix = 'files-transfer') {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function reconcileTransferReceipt(receipt, expectedItemIds, { operationId = '', generation = null } = {}) {
  const expectedOperation = String(operationId || '').trim();
  const expectedGeneration = generation == null ? null : Number(generation);
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
    || (expectedOperation && receipt.operation_id !== expectedOperation)
    || !Number.isSafeInteger(Number(receipt.generation)) || Number(receipt.generation) < 0
    || (expectedGeneration != null && Number(receipt.generation) !== expectedGeneration)
    || !['complete', 'partial', 'pending'].includes(String(receipt.state || '').toLowerCase())) {
    return { ok: false, reason: 'Transfer receipt identity or state is invalid.' };
  }
  const items = Array.isArray(receipt?.items) ? receipt.items : null;
  if (!items) return { ok: false, reason: 'Transfer receipt is missing its item results.' };
  const expected = new Set(expectedItemIds.map((id) => String(id)));
  const seen = new Set();
  const normalized = [];
  for (const item of items) {
    const itemId = String(item?.item_id || item?.itemId || '').trim();
    if (!itemId || !expected.has(itemId) || seen.has(itemId)) return { ok: false, reason: 'Transfer receipt did not account for each requested item.' };
    const outcome = String(item?.outcome || item?.status || '').trim().toLowerCase();
    if (!['committed', 'unchanged', 'conflict', 'collision', 'denied', 'stale', 'failed', 'pending'].includes(outcome)) {
      return { ok: false, reason: 'Transfer receipt contains an unknown item outcome.' };
    }
    seen.add(itemId);
    normalized.push({ ...item, item_id: itemId, outcome });
  }
  if (seen.size !== expected.size) return { ok: false, reason: 'Transfer receipt omitted one or more requested items.' };
  return {
    ok: true,
    items: normalized,
    committed: new Set(normalized.filter((item) => ['committed', 'unchanged'].includes(item.outcome)).map((item) => item.item_id)),
    pending: normalized.some((item) => item.outcome === 'pending'),
    failed: normalized.filter((item) => !['committed', 'unchanged'].includes(item.outcome)),
  };
}

function transferFailureStatus(receipt) {
  const labels = {
    resource_changed: 'Source changed or destination name already exists.',
    resource_unavailable: 'Resource access is unavailable.',
    resource_ref_stale: 'File access changed; select the source again.',
    invalid_destination: 'The destination cannot receive this item.',
    provider_unavailable: 'The provider could not complete the transfer.',
    unsupported_operation: 'This provider does not support the operation.',
    policy_generation_changed: 'File access changed during the transfer.',
    managed_media_move_unsupported: 'This kind of move is not supported for a document with managed media; nothing was changed.',
    managed_cross_filesystem_unsupported: 'This move crosses disks. Moving a document with its media between disks is not supported yet; nothing was changed.',
    ...Object.fromEntries(['unmanaged_local_reference', 'unresolvable_reference', 'reference_outside_move', 'media_location_not_preservable', 'invalid_destination_frame']
      .map(code => [code, `This document's media links could not be moved safely, so nothing was changed. (${code})`])),
  };
  const reasons = [...new Set(receipt.failed.map(item => labels[item.code] || item.message || item.reason || item.code || item.outcome))];
  return `${receipt.committed.size} completed · ${receipt.failed.length} need attention${reasons.length ? ' · ' + reasons.join(' ') : ''}`;
}

function updateSelectionAfterTransfer(model, payload, receipt) {
  if (!model || !receipt?.ok) return;
  const committedKeys = new Set(payload.sources.filter((source) => receipt.committed.has(String(source.item_id))).map((source) => String(source.resource_key)));
  if (!committedKeys.size) return;
  model.replaceSelection(model.selectedKeys().filter((key) => !committedKeys.has(key)));
  if (![...state.selectionModels.values()].includes(model) || model.scope.column === 'tree') return;
  state.selected = new Set(model.selectedKeys());
  const column = state.columns.find((candidate) => candidate && candidate.resourceRef === model.scope.parentRef);
  if (column) column.selection = new Set(model.selectedKeys());
}

function transferPayloadForModel(model, kind) {
  if (!model || !model.scope?.owner || !model.scope?.provider || !model.scope?.parentRef) return null;
  return buildInternalDragPayload({
    scope: model.scope, generation: filesPolicyGeneration(), policyGeneration: filesPolicyGeneration(),
    owner: state.owner, pane: state.nativeWindow?.id || 'files-window', selectionEpoch: model.epoch,
    kind, entries: transferEntries(model), selectedKeys: model.selectedKeys(),
  });
}

function payloadScopeKey(payload) {
  return scopeKey({
    owner: payload?.owner,
    workspace: payload?.workspace,
    provider: payload?.provider,
    parentRef: payload?.parent_ref,
    query: payload?.query,
    column: payload?.column,
  });
}

async function executeFilesTransfer(payload, destination, collision = 'fail', destinationIndex = state.columns.length - 1, { model: suppliedModel = null, selectionKeys = null, batchId = '', operationIdOverride = '', refresh = true, rootRequirement = null, isCurrent = () => true } = {}) {
  if (options.pickerMode) { setStatus("This dialog selects resources; transfers are unavailable.", true); return null; }
  const destinationRef = destinationResourceRef(destination);
  if (!payload?.provider || !destination?.provider || !destinationRef || payload.provider !== destination.provider) {
    setStatus('Transfers between providers are unavailable.', true);
    return null;
  }
  const sameProvider = payload.provider === destination.provider;
  const gesture = state.gesture?.type === 'drag' ? state.gesture : null;
  const sourceBrowser = browsers.get(payload.pane);
  const capturedModel = suppliedModel || sourceBrowser?.resolveTransferModel(payload) || null;
  if (!capturedModel) { setStatus('The source Files selection or access changed.',true); return null; }
  const transferLifecycle = state.lifecycleGeneration;
  const transferContent = state.contentGeneration;
  const sourceLifetime = sourceBrowser?.captureLifetime();
  const sourceIsCurrent = () => !sourceBrowser || sourceBrowser.isLifetimeCurrent(sourceLifetime);
  if (capturedModel) {
    const expectedKeys = payload.sources.map((source) => String(source.resource_key));
    const actualKeys = capturedModel?.selectedKeys?.() || [];
    const expectedSelection = Array.isArray(selectionKeys) ? selectionKeys.map(String) : actualKeys;
    const sameKeys = expectedSelection.length === actualKeys.length && expectedSelection.every((key, index) => key === actualKeys[index])
      && expectedKeys.every((key) => expectedSelection.includes(key));
    const currentEntries = capturedModel?.selectedEntries?.() || [];
    const capabilities = currentEntries.map(transferCapabilities);
    const allowed = payload.kind === 'move' ? capabilities.every((capability) => capability.move) : capabilities.every((capability) => capability.copy);
    if (capturedModel.epoch !== Number(payload.selection_epoch) || !sameKeys || !allowed
      || Number(payload.generation) !== filesPolicyGeneration()
      || Number(payload.policy_generation) !== filesPolicyGeneration()
      || capturedModel.scope.provider !== payload.provider || capturedModel.scope.owner !== payload.owner
      || capturedModel.scope.parentRef !== payload.parent_ref) {
      setStatus('The Files selection changed; choose the command again.', true);
      return null;
    }
  }
  const validation = validateDropTarget(payload, { resource_ref: destinationRef, resource_key: entrySelectionKey(destination) }, {
    generation: filesPolicyGeneration(),
    policyGeneration: filesPolicyGeneration(),
    selectionEpoch: capturedModel ? capturedModel.epoch : null,
    owner: state.owner, workspace: state.currentWorkspaceId || copalWorkspace(), pane: payload.pane, provider: destination.provider,
    allowMove: sameProvider && (destination.capabilities || []).includes('children') && (destination.capabilities || []).includes('write'),
    allowCopy: sameProvider && (destination.capabilities || []).includes('children') && (destination.capabilities || []).includes('write'),
  });
  if (!validation.ok) { setStatus(validation.reason, true); return null; }
  const sources = payload.sources.map((source) => ({ itemId: source.item_id, resourceRef: source.resource_ref, expectedRevision: source.revision || undefined }));
  const operation = operationIdOverride || operationId(batchId ? `files-transfer-${batchId}` : 'files-transfer');
  let didDispatch = false;
  try {
    let managedRoot = null;
    let destinationRoot = null;
    if (payload.kind === 'move' && payload.provider === 'host') {
      const capturedCurrent = () => isCurrent() && sourceIsCurrent() && transferLifecycle === state.lifecycleGeneration && transferContent === state.contentGeneration
        && state.owner === payload.owner && filesPolicyGeneration() === Number(payload.policy_generation)
        && capturedModel?.epoch === Number(payload.selection_epoch) && capturedModel.snapshot().scopeKey === payloadScopeKey(payload);
      for (const source of sources) {
        if (!source.expectedRevision) {
          const current = await filesFacadeClient.stat(source.resourceRef);
          if (!capturedCurrent()) throw new Error('The Files selection or access changed.');
          const revision = current?.resource?.revision;
          if (!revision) throw new Error('Refresh this item before moving it.');
          source.expectedRevision = {kind:revision.kind, value:revision.value};
        }
      }
      const preflight = rootRequirement == null ? await filesFacadeClient.transferRootRequirement({generation:Number(payload.generation), sources, destinationRef}) : rootRequirement;
      if (!capturedCurrent()) { setStatus('The Files selection or access changed.', true); return null; }
      const pickRoot = async (rootLabel) => {
        const { createResourcePicker } = await import('./copal/resourcePicker.js');
        if (!capturedCurrent()) return null;
        return new Promise(resolve => {
          let selected = null;
          state.managedRootPicker?.destroy();
          const picker = createResourcePicker({client:filesFacadeClient, purpose:'folder', accountScope:state.owner,
            workspaceScope:copalWorkspace(), generation:Number(payload.generation), originWindow:state.nativeWindow,
            getGeneration:()=>filesPolicyGeneration(), getAccountScope:()=>state.owner, getWorkspaceScope:()=>copalWorkspace(),
            isOriginCurrent:capturedCurrent, rootLabel,
            isCompatible:resource=>resource.provider === 'host' && resource.capabilities.write ? '' : 'Choose a writable Host root.',
            onSelect:resource=>{ selected=resource; return true; },
            onClose:cancelled=>{ if (state.managedRootPicker === picker) state.managedRootPicker=null; resolve(cancelled ? null : selected); }});
          state.managedRootPicker=picker; void picker.open();
        });
      };
      for (const which of transferRootsToAsk(preflight)) {
        const root = await pickRoot(which === 'managed' ? 'Choose an authorized root for this move' : "Choose the folder that will own the moved document's media");
        if (!root || !capturedCurrent()) return null;
        if (which === 'managed') managedRoot = root; else destinationRoot = root;
      }
    }
    if (!isCurrent()) return null;
    setStatus(`${sources.length} item${sources.length === 1 ? '' : 's'} · ${payload.kind}…`);
    didDispatch = true;
    const response = await filesFacadeClient.transferResources({ operationId: operation, generation: Number(payload.generation), kind: payload.kind, sources, destinationRef, collision, managedRoot, destinationRoot });
    if (!sourceIsCurrent() || transferLifecycle !== state.lifecycleGeneration || transferContent !== state.contentGeneration || (capturedModel && (capturedModel.epoch !== Number(payload.selection_epoch)
      || state.owner !== payload.owner || filesPolicyGeneration() !== Number(payload.policy_generation)
      || capturedModel.snapshot().scopeKey !== payloadScopeKey(payload)))) {
      setStatus('File access, window, or selection changed while the transfer was running; reconcile the receipt before continuing.', true);
      return response;
    }
    const reconciled = reconcileTransferReceipt(response, sources.map((source) => source.itemId), { operationId: operation, generation: Number(payload.generation) });
    if (!reconciled.ok) {
      setStatus(`${reconciled.reason} Selection was preserved for reconciliation.`, true);
      return response;
    }
    updateSelectionAfterTransfer(capturedModel, payload, reconciled);
    if (sourceBrowser && (sourceBrowser !== api || capturedModel.scope.column === "tree")) sourceBrowser.updateTransferSelection(capturedModel);
    if (reconciled.failed.length) {
      setStatus(transferFailureStatus(reconciled), true);
      offerKeepBothRetry(payload, destination, response);
    } else {
      clearKeepBothRetry();
      setStatus(`${sources.length} item${sources.length === 1 ? '' : 's'} ${payload.kind}d`);
    }
    if (refresh && reconciled.committed.size && transferLifecycle === state.lifecycleGeneration) {
      await reloadManagedColumn(destinationIndex);
      if (transferLifecycle !== state.lifecycleGeneration || (capturedModel && (state.owner !== payload.owner || filesPolicyGeneration() !== Number(payload.policy_generation)))) {
        setStatus('File access changed while refreshing the transfer result.', true);
      }
    }
    if (payload.kind === 'move' && reconciled.committed.size) {
      void offerFinishCleanup(operation, Number(payload.generation), payload.owner, transferLifecycle);
    }
    return response;
  } catch (error) {
    if (error?.name !== 'AbortError') {
      if (!didDispatch) { setStatus(error?.message || 'Files transfer could not be checked.', true); return null; }
      // A lost response is reconciled by receipt; the browser never resends a
      // request whose commit status is unknown.
      if (transferLifecycle !== state.lifecycleGeneration) {
        setStatus('File access or window changed; the transfer receipt was retained for reconciliation.', true);
        return null;
      }
      try {
        const receipt = await filesFacadeClient.operationReceipt(operation, { signal: null, generation: Number(payload.generation) });
        if (!sourceIsCurrent() || transferLifecycle !== state.lifecycleGeneration || transferContent !== state.contentGeneration || (capturedModel && (capturedModel.epoch !== Number(payload.selection_epoch)
          || state.owner !== payload.owner || filesPolicyGeneration() !== Number(payload.policy_generation)
          || capturedModel.snapshot().scopeKey !== payloadScopeKey(payload)))) {
          setStatus('File access, window, or selection changed; the transfer receipt was retained for reconciliation.', true);
          return receipt;
        }
        const reconciled = reconcileTransferReceipt(receipt, sources.map((source) => source.itemId), { operationId: operation, generation: Number(payload.generation) });
        if (!reconciled.ok) {
          setStatus(`${reconciled.reason} Selection was preserved for reconciliation.`, true);
          return receipt;
        }
        updateSelectionAfterTransfer(capturedModel, payload, reconciled);
    if (sourceBrowser && (sourceBrowser !== api || capturedModel.scope.column === "tree")) sourceBrowser.updateTransferSelection(capturedModel);
        if (refresh && reconciled.committed.size && transferLifecycle === state.lifecycleGeneration) {
          await reloadManagedColumn(destinationIndex);
          if (transferLifecycle !== state.lifecycleGeneration || state.owner !== payload.owner || filesPolicyGeneration() !== Number(payload.policy_generation)) {
            setStatus('File access changed while refreshing the transfer result.', true);
          }
        }
        setStatus(reconciled.pending ? 'Transfer status is still pending reconciliation.' : reconciled.failed.length ? transferFailureStatus(reconciled) : `${reconciled.committed.size} transfer item${reconciled.committed.size === 1 ? '' : 's'} reconciled.`, reconciled.pending || reconciled.failed.length > 0);
        if (reconciled.failed.length) offerKeepBothRetry(payload, destination, receipt);
        if (payload.kind === 'move' && reconciled.committed.size) {
          void offerFinishCleanup(operation, Number(payload.generation), payload.owner, transferLifecycle);
        }
        return receipt;
      } catch (_) { setStatus(error.message || 'Files transfer failed', true); }
    }
    return null;
  }
}

hooks.__openClankFilesRetryKeepBoth = async (payload, destination, response) => {
  const retryable = new Set((response?.items || []).filter((item) => {
    const outcome = String(item?.outcome || '').toLowerCase();
    const code = String(item?.code || item?.error_code || item?.reason || '').toLowerCase();
    return outcome.includes('conflict') || outcome.includes('collision') || code.includes('conflict') || code.includes('collision');
  }).map((item) => String(item.item_id || '')));
  const remaining = { ...payload, sources: payload.sources.filter((source) => retryable.has(String(source.item_id || ''))).map((source, index) => ({
    ...source,
    // Keep Both is a new operation. Item identities must also be new so a
    // retry cannot replay a committed child request.
    item_id: `retry-${Date.now().toString(36)}-${index}-${Math.random().toString(36).slice(2, 8)}`.slice(0, 128),
  })) };
  if (!remaining.sources.length) return response;
  return executeFilesTransfer(remaining, destination, 'rename', destinationColumnIndex(destination));
};

function importReceiptItem(receipt, expectedItemId, { operationId = '', generation = null } = {}) {
  const expectedOperation = String(operationId || '').trim();
  const expectedGeneration = generation == null ? null : Number(generation);
  const stateValue = String(receipt?.state || '').trim().toLowerCase();
  if (!receipt || typeof receipt !== 'object' || Array.isArray(receipt)
    || (expectedOperation && receipt.operation_id !== expectedOperation)
    || !Number.isSafeInteger(Number(receipt.generation)) || Number(receipt.generation) < 0
    || (expectedGeneration != null && Number(receipt.generation) !== expectedGeneration)
    || !['complete', 'partial', 'pending'].includes(stateValue)) return { outcome: 'malformed', terminal: false, committed: false };
  const items = Array.isArray(receipt?.items) ? receipt.items : null;
  if (!items) return { outcome: 'malformed', terminal: false, committed: false };
  const ids = items.map((item) => String(item?.item_id || item?.itemId || '').trim());
  if (ids.some((id) => !id) || new Set(ids).size !== ids.length) return { outcome: 'malformed', terminal: false, committed: false };
  const matches = items.filter((item) => String(item?.item_id || item?.itemId || '') === expectedItemId);
  if (matches.length !== 1) return { outcome: 'malformed', terminal: false, committed: false };
  const outcome = String(matches[0]?.outcome || '').toLowerCase();
  if (stateValue === 'pending' && outcome !== 'pending') return { outcome: 'malformed', terminal: false, committed: false };
  if (stateValue === 'complete' && !['committed', 'unchanged', 'created', 'imported'].includes(outcome)) return { outcome: 'malformed', terminal: false, committed: false };
  const terminal = ['committed', 'unchanged', 'created', 'imported', 'conflict', 'denied', 'stale', 'failed'].includes(outcome);
  const history=matches[0]?.history?.history;
  const recoveryUnavailable = outcome === 'committed' && (!history || !['complete','incomplete','unavailable'].includes(history.state) || history.state !== 'complete');
  return { outcome, terminal, committed: ['committed', 'unchanged', 'created', 'imported'].includes(outcome), recoveryUnavailable };
}

function importedDirectoryParts(item) {
  const name = safeExternalDropSegment(item?.name || item?.file?.name);
  const relative = String(item?.relativePath || name).replace(/\\/g, '/').trim();
  const parts = relative.split('/');
  if (!name || !relative || parts.at(-1) !== name || !safeExternalDropPath(parts)) return null;
  return parts.slice(0, -1);
}

function importedDirectoryPath(item) {
  if (!item?.directory) return null;
  const relative = String(item.relativePath || item.name || '').replace(/\\/g, '/').trim();
  const parts = relative.split('/');
  return safeExternalDropPath(parts) ? parts : null;
}

function importedDirectoryOperationId(batchId, index) {
  return `${batchId}-directory-${index}`.slice(0, 128);
}

function importedDirectoryResponseResource(response, provider) {
  const direct = response?.resource || response?.data?.resource;
  if (direct?.ref || direct?.resource_ref) return { ...direct, provider: direct.provider || provider };
  const item = (Array.isArray(response?.items) ? response.items : [])
    .find(candidate => ['committed', 'unchanged'].includes(String(candidate?.outcome || '').toLowerCase()));
  if (!item?.resource_ref) return null;
  return { ref: item.resource_ref, revision: item.revision || null, provider, kind: 'folder' };
}

async function createImportedDirectory(parent, name, operation, generation, provider, signal) {
  const checked = await filesFacadeClient.stat(parent.ref, { signal });
  const current = checked?.resource || checked;
  const currentRef = String(current?.ref || current?.resource_ref || parent.ref).trim();
  const currentProvider = String(current?.provider || provider).trim();
  const currentKind = String(current?.kind || '').toLowerCase();
  const currentCapabilities = new Set(current?.capabilities || []);
  if (currentRef !== parent.ref || currentProvider !== provider || !['folder', 'provider_root', 'virtual_folder'].includes(currentKind)
    || !currentCapabilities.has('children') || !currentCapabilities.has('write')) {
    throw Object.assign(new Error('The import folder is no longer authorized.'), { code: 'resource_unavailable' });
  }
  const expectedRevision = current?.revision || parent.revision || null;
  let response;
  try {
    response = await filesFacadeClient.createDirectory(parent.ref, {
      name, operationId: operation, generation, expectedRevision, collision: 'reuse', signal,
    });
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    // A lost response is recovered by the same operation receipt. A known
    // denial/collision remains a typed failure and is not retried as a mkdir.
    if (Number(error?.status || 0) >= 400 && Number(error?.status || 0) < 500) throw error;
    response = await filesFacadeClient.operationReceipt(operation, { signal, generation });
  }
  const resource = importedDirectoryResponseResource(response, provider);
  const resourceRef = String(resource?.ref || resource?.resource_ref || '').trim();
  if (!resourceRef) throw Object.assign(new Error('Directory creation did not return an authorized resource.'), { code: 'operation_pending' });
  const verified = await filesFacadeClient.stat(resourceRef, { signal });
  const item = verified?.resource || verified;
  const verifiedRef = String(item?.ref || item?.resource_ref || resourceRef).trim();
  const verifiedProvider = String(item?.provider || provider).trim();
  const verifiedKind = String(item?.kind || '').toLowerCase();
  const capabilities = new Set(item?.capabilities || resource?.capabilities || []);
  if (verifiedRef !== resourceRef || verifiedProvider !== provider
    || !['folder', 'provider_root', 'virtual_folder'].includes(verifiedKind)
    || !capabilities.has('children') || !capabilities.has('write')) {
    throw Object.assign(new Error('Files returned an invalid import directory.'), { code: 'provider_unavailable' });
  }
  return { ref: verifiedRef, revision: item?.revision || resource?.revision || null };
}

async function prepareImportedDirectories(files, destination, batchId, generation, lifecycle, owner, directoryEntries = []) {
  const provider = String(destination?.provider || state.provider || '').trim();
  const limits = state.importCapabilities?.[provider];
  const directoryPaths = new Set();
  for (const parts of directoryEntries.map(importedDirectoryPath)) {
    if (!parts?.length) continue;
    for (let index = 1; index <= parts.length; index += 1) directoryPaths.add(parts.slice(0, index).join('/'));
  }
  for (const parts of files.map(importedDirectoryParts)) {
    if (!parts?.length) continue;
    for (let index = 1; index <= parts.length; index += 1) directoryPaths.add(parts.slice(0, index).join('/'));
  }
  const paths = [...directoryPaths].sort();
  const refs = new Map();
  const failures = [];
  refs.set('', { ref: destinationResourceRef(destination), revision: destination.revision || null });
  if (!paths.length) return { refs, failures };
  if (limits?.create_directory !== true || typeof filesFacadeClient.createDirectory !== 'function') {
    return { refs, failures: paths.map(path => ({ name: path, code: 'nested_directory_unsupported' })) };
  }
  const pathIndex = new Map(paths.map((path, index) => [path, index]));
  for (const path of paths) {
    if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
      failures.push({ name: path, code: 'resource_ref_stale' });
      continue;
    }
    const parts = path.split('/');
    let parent = refs.get('');
    let blocked = false;
    for (let index = 0; index < parts.length; index += 1) {
      const currentPath = parts.slice(0, index + 1).join('/');
      const existing = refs.get(currentPath);
      if (existing) { parent = existing; continue; }
      if (blocked || failures.some(failure => failure.name === currentPath || currentPath.startsWith(`${failure.name}/`))) {
        blocked = true;
        continue;
      }
      try {
        const child = await createImportedDirectory(
          parent, parts[index], importedDirectoryOperationId(batchId, pathIndex.get(currentPath)),
          generation, provider, null,
        );
        if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
          failures.push({ name: currentPath, code: 'resource_ref_stale' });
          blocked = true;
          continue;
        }
        refs.set(currentPath, child);
        parent = child;
      } catch (error) {
        failures.push({ name: currentPath, code: error?.code || 'directory_create_failed' });
        blocked = true;
      }
    }
  }
  return { refs, failures };
}

async function importDroppedFiles(files, destination, destinationIndex = state.columns.length - 1, { skipped = [], directories = [], collision = 'fail', refreshDestination = true, signal = null, isCurrent = () => true, videoFrameSource = null } = {}) {
  if (options.pickerMode) return;
  const destinationRef = destinationResourceRef(destination);
  if (!destinationRef || !(destination.capabilities || []).includes('children') || !(destination.capabilities || []).includes('write')) { setStatus('This folder does not accept imported files.', true); return; }
  const allFiles = [...files]
    .map((item) => ({
      file: item?.file || item,
      name: String(item?.name || item?.file?.name || '').trim(),
      relativePath: String(item?.relativePath || item?.name || item?.file?.name || '').replace(/\\/g, '/'),
    }))
    .filter((item) => item.file && item.name);
  if (videoFrameSource && (allFiles.length!==1 || directories.length || skipped.length)) throw new TypeError('A frame copy must be one file.');
  const provider = String(destination.provider || state.provider || '');
  const limits = state.importCapabilities?.[provider];
  const maxTotal = Number(limits?.max_total_bytes);
  const maxChunk = Number(limits?.max_chunk_bytes);
  if (!Number.isSafeInteger(maxTotal) || maxTotal <= 0 || !Number.isSafeInteger(maxChunk) || maxChunk <= 0) {
    setStatus('This provider did not advertise safe import limits.', true);
    return;
  }
  const oversized = allFiles.find((item) => Number(item.file?.size) > maxChunk);
  const totalBytes = allFiles.reduce((total, item) => total + Math.max(0, Number(item.file?.size) || 0), 0);
  if (oversized || totalBytes > maxTotal) {
    setStatus(oversized ? `${oversized.name} exceeds the provider import limit.` : 'The selected files exceed the provider import limit.', true);
    return;
  }
  const bounded = allFiles.slice(0, FILES_MAX_DRAG_ITEMS);
  const batchId = operationId('files-import-batch');
  const owner = state.owner;
  const generation = filesPolicyGeneration();
  const lifecycle = state.lifecycleGeneration;
  const requestCurrent = () => !signal?.aborted && isCurrent() && state.owner === owner
    && filesPolicyGeneration() === generation && state.lifecycleGeneration === lifecycle;
  if (!requestCurrent()) return { committed:0, rejected:0, pending:0, cancelled:true, receiptHandles:[] };
  const importedDirectories = await prepareImportedDirectories(bounded, destination, batchId, generation, lifecycle, owner, directories);
  if (!requestCurrent()) {
    setStatus('Imports stopped because file access changed.', true);
    return { committed:0, rejected:0, pending:0, cancelled:true, receiptHandles:[] };
  }
  state.pendingImport = allFiles.slice(FILES_MAX_DRAG_ITEMS);
  if (state.pendingImport.length) setStatus(`${bounded.length} of ${allFiles.length} files queued · ${state.pendingImport.length} remaining`, false);
  const results = new Array(bounded.length);
  let submittedCount = 0;
  let cursor = 0;
  const worker = async () => {
    while (cursor < bounded.length) {
      if (!requestCurrent()) return;
      const index = cursor++;
      const file = bounded[index];
      const id = operationId('files-import');
      const itemId = `${batchId}-${index}-${operationId('item').slice(-12)}`.slice(0, 128);
      const controller = new AbortController();
      const abortForCaller = () => controller.abort();
      signal?.addEventListener('abort', abortForCaller, { once:true });
      state.importControllers.add(controller);
      setStatus(`Importing ${index + 1} of ${bounded.length}…`);
      try {
        const directoryParts = importedDirectoryParts(file);
        const directoryPath = directoryParts?.join('/') || '';
        const directory = directoryParts?.length ? importedDirectories.refs.get(directoryPath) : importedDirectories.refs.get('');
        if ((directoryParts?.length && !directory) || !directory?.ref) {
          results[index] = { outcome: 'failed', terminal: true, committed: false, code: 'directory_create_failed' };
          continue;
        }
        if (!requestCurrent()) {
          results[index] = { outcome:'cancelled', terminal:true, committed:false, operationId:id, itemId, generation };
          continue;
        }
        let receipt;
        try {
          submittedCount += 1;
          receipt = await filesFacadeClient.importFile(file.file, {
            operationId: id, itemId, generation, destinationRef: directory.ref,
            name: file.name, relativePath: file.name,
            collision: collision === 'rename' ? 'rename' : 'fail', signal: controller.signal, ...(videoFrameSource ? {videoFrameSource} : {}),
          });
        } catch (error) {
          if (error?.name !== 'AbortError' && requestCurrent()) {
            // A lost response is unknown until this exact operation receipt
            // says otherwise. Never submit the file again.
          }
          const receiptController = new AbortController();
          const receiptDeadline = setTimeout(() => receiptController.abort(), 5_000);
          try { receipt = await filesFacadeClient.operationReceipt(id, { signal:receiptController.signal, generation }); }
          catch { receipt = null; }
          finally { clearTimeout(receiptDeadline); }
        }
        let parsed = importReceiptItem(receipt, itemId, { operationId: id, generation });
        if (!parsed.terminal && receipt?.state !== 'pending') parsed = { outcome: 'malformed', terminal: false, committed: false };
        results[index] = { ...parsed, operationId:id, itemId, generation };
      } catch (error) {
        results[index] = { outcome: error?.name === 'AbortError' ? 'cancelled' : 'unknown', terminal: false, committed: false, operationId:id, itemId, generation };
      } finally { signal?.removeEventListener('abort', abortForCaller); state.importControllers.delete(controller); }
    }
  };
  await Promise.all([worker(), worker()]);
  const receiptHandles = results.filter(result => result && !result.terminal)
    .map(result => ({ operationId:result.operationId, itemId:result.itemId, generation:result.generation }));
  const stillCurrent = requestCurrent();
  if (!stillCurrent) {
    setStatus('Imports stopped because file access changed.', true);
  }
  const committed = results.filter((result) => result?.committed).length;
  const pending = results.filter((result) => !result?.terminal).length;
  const rejected = results.filter((result) => result?.terminal && !result.committed).length;
  if (stillCurrent && refreshDestination && committed && destinationIndex >= 0) {
    await reloadManagedColumn(destinationIndex);
    if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
      setStatus('File access or window changed while refreshing imported files.', true);
      return;
    }
  }
  const recoveryUnavailable = results.some(row=>row?.committed && row.recoveryUnavailable);
  const remaining = state.pendingImport.length;
  const skippedFailures = [...(Array.isArray(skipped) ? skipped : []), ...importedDirectories.failures];
  const skippedCount = skippedFailures.length;
  if (stillCurrent) setStatus(`${committed} imported${rejected ? ` · ${rejected} rejected` : ''}${skippedCount ? ` · ${skippedCount} skipped (${skippedFailures[0]?.code || 'partial_failure'})` : ''}${pending ? ` · ${pending} pending reconciliation` : ''}${recoveryUnavailable ? ' · recovery copy unavailable' : ''}${remaining ? ` · ${remaining} remaining (drop them separately)` : ''}`, Boolean(rejected || skippedCount || pending || recoveryUnavailable));
  return { committed, rejected, pending, receiptHandles, recoveryUnavailable, stale:!stillCurrent, submitted:submittedCount > 0 };
}

// Identifier-only context consumed by contextualHelp.js. Keep host paths out
// of this handoff: the Files provider owns authorization and resource refs.
hooks.__odysseusGetActiveFilesContext = () => {
  const column = activeColumn();
  const activeModel = ensureSelectionModel(column, state.activeColumnIndex);
  const selected = state.entries.find((entry) => state.selected.has(entrySelectionKey(entry)));
  let copal = {};
  try { copal = window.__odysseusGetActiveCopalContext?.() || {}; } catch (_) {}
  return {
    accountId: copal.accountId || state.owner || null,
    workspace: state.currentWorkspaceId || 'default',
    provider: state.provider || column?.provider || null,
    generation: filesPolicyGeneration(),
    policyGeneration: filesPolicyGeneration(),
    surface: 'files',
    view: 'files',
    resourceKind: selected ? (isDirectory(selected) ? 'folder' : 'file') : 'files-folder',
    resourceId: selected?.resource_id || column?.resourceId || null,
    resourceRef: selected?.resource_ref || column?.resourceRef || null,
    selection: selected?.name || null,
    selectionCount: activeModel.selectedKeys().length,
  };
};

function syncSearchControl() {
  const input = state.shell?.querySelector('[data-files-search]');
  if (input && input.value !== state.searchQuery) input.value = state.searchQuery;
}

function resetNativeThumbnails() {
  // Rerenders detach tile subscribers but preserve keyed jobs and cached
  // object URLs.  Only lifecycle teardown/account or policy changes hard-clear
  // the loader (see destroyFilesWindow / auth handlers).
  state.thumbnailLoader?.detachAll?.();
}

function clearNativeThumbnailCache() {
  state.graphicalMedia?.dispose();state.graphicalMedia=null;
  state.thumbnailLoader?.clear?.();
  state.thumbnailLoader = null;
}

function ensureThumbnailLoader() {
  if(!state.graphicalMedia&&state.shell)state.graphicalMedia=mountImageRegion({root:state.shell,selector:'.files-thumbnail, .files-preview-image',isCurrent:()=>!!state.shell?.isConnected});
  if (!state.thumbnailLoader) {
    state.thumbnailLoader = createThumbnailLoader({
      maxConcurrent: 4,
      maxQueue: 64,
      onAuthorityError: () => {
        state.thumbnailLoader?.clear?.();
        try { document.dispatchEvent(new CustomEvent('openclank:file-policy-changed')); } catch (_) {}
      },
    });
  }
  state.thumbnailLoader.setRoot(state.shell?.querySelector('[data-files-body]') || null);
  return state.thumbnailLoader;
}

function observeNativeThumbnail(img, item) {
  const loader = ensureThumbnailLoader();
  const entry = item.entry || {};
  const key = thumbnailCacheKey({
    owner: state.owner, entry, icon: item.icon,
    scale: Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1)),
  });
  loader.attach(img, {
    key,
    url: item.url,
    root: state.shell?.querySelector('[data-files-body]') || null,
    renderFallback: () => {
      if (!item.icon && entry.native_icon_url) {
        const image = el('img', { class: 'files-thumbnail', alt: '', decoding: 'async' });
        item.visual.replaceChildren(image);
        queueMicrotask(() => observeNativeThumbnail(image, { ...item, img: image, icon: true, url: entry.native_icon_url }));
      } else {
        item.visual.replaceChildren(iconNode(item.entry, { size: 24 }));
      }
    },
  });
}

function entryVisual(entry, directory) {
  const visual = el('span', { class: 'files-entry-visual', 'aria-hidden': 'true' });
  if (entry.native_thumbnail_url && !directory) {
    const thumbnail = el('img', {
      class: 'files-thumbnail',
      alt: '',
      decoding: 'async',
    });
    visual.append(thumbnail);
    queueMicrotask(() => observeNativeThumbnail(thumbnail, {
      img: thumbnail,
      visual,
      entry,
      url: entry.native_thumbnail_url,
    }));
  } else if (entry.thumbnail_url && !directory) {
    const thumbnail = el('img', {
      class: 'files-thumbnail',
      src: entry.thumbnail_url,
      alt: '',
      loading: 'lazy',
      decoding: 'async',
    });
    thumbnail.addEventListener('error', () => {
      visual.replaceChildren(iconNode(entry, { size: 24 }));
    }, { once: true });
    visual.append(thumbnail);
  } else if (entry.native_icon_url && !directory) {
    const image = el('img', { class: 'files-thumbnail', alt: '', decoding: 'async' });
    visual.append(image);
    queueMicrotask(() => observeNativeThumbnail(image, { img: image, visual, entry, icon: true, url: entry.native_icon_url }));
  } else {
    visual.append(iconNode(entry, { size: 24 }));
  }
  return visual;
}

function formatModified(value) {
  const timestamp = Number(value);
  if (!Number.isFinite(timestamp) || timestamp <= 0) return '—';
  try { return new Date(timestamp).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); }
  catch { return '—'; }
}

async function applyPrimarySort(spec) {
  state.sort = usesFacadeListing()
    ? constrainSortSpec(spec, activeSortKeys())
    : normalizeSortSpec(spec);
  if (usesFacadeListing()) activeColumn().sort = state.sort;
  rememberViewPreferences();
  syncPrimarySortControls();
  // Server cursors are bound to the complete sort contract.  Restart the
  // active listing instead of mixing a locally reordered page with a cursor
  // produced under the previous order.
  if (activeColumn()?.globalSearch) {
    await searchAllSources(state.searchQuery);
  } else if (usesFacadeListing()) {
    await reloadManagedColumn(state.columns.length - 1);
  } else if (state.provider === 'host' && state.hostPath) {
    setStatus('Refresh the authorized Files roots before sorting this folder.', true);
  } else if (state.columns.length) {
    await reloadManagedColumn(state.columns.length - 1);
  } else {
    state.entries = sortEntries(state.entries);
    renderEntries();
  }
}

async function applyFileSort(key) {
  const direction = state.sort.key === key && state.sort.direction === 'asc' ? 'desc' : 'asc';
  await applyPrimarySort({ ...state.sort, key, direction });
}

function renderDetailsHeader() {
  const header = el('div', { class: 'files-sort-header', role: 'row' });
  header.append(el('div', { class: 'files-sort-icon-header', role: 'columnheader', 'aria-label': 'File icon' }));
  for (const [key, label] of FILE_DETAIL_COLUMNS) {
    const active = state.sort.key === key;
    const direction = active ? state.sort.direction : null;
    const supported = activeSortKeys().includes(key);
    const column = el('div', {
      class: `files-sort-column${active ? ' active' : ''}`,
      role: 'columnheader',
      'aria-sort': active ? (direction === 'asc' ? 'ascending' : 'descending') : null,
    });
    const button = el('button', {
      type: 'button',
      class: `files-sort-header-button${active ? ' active' : ''}`,
      'data-sort-key': key,
      disabled: supported ? null : 'disabled',
      'aria-label': active
        ? `Sort by ${label}, currently ${direction === 'asc' ? 'ascending' : 'descending'}`
        : supported ? `Sort by ${label}` : `${label} sorting is unavailable for this folder`,
    });
    button.append(el('span', { text: label }), el('span', { class: 'files-sort-indicator', 'aria-hidden': 'true', text: active ? (direction === 'asc' ? '↑' : '↓') : '↕' }));
    button.addEventListener('click', () => { void applyFileSort(key); });
    column.append(button);
    header.append(column);
  }
  return header;
}

function layoutMetrics() {
  const large = ['grid', 'gallery'].includes(state.mode);
  const size = large ? state.largeIconSize : state.smallIconSize;
  const gap = state.mode === 'gallery' ? 8 : 4;
  const previewHeight = state.mode === 'gallery' ? Math.max(86, size) : large ? size : Math.max(28, size);
  return { size, previewHeight, tileWidth: Math.max(110, size + 40), rowStride: large ? previewHeight + 68 + gap : Math.max(32, previewHeight + 12), gap };
}
function syncPagingControl() {
  const button = state.loadMoreButton;
  if (!button) return;
  button.hidden = !state.nextCursor || state.mode === 'columns' || Boolean(options.navigationOnly && !activeColumn()?.query);
  button.disabled = Boolean(state.pagingController);
  button.textContent = state.pagingController ? 'Loading more…' : 'Load more';
}

function withFileRowActions(row, entry, details = false) {
  if (options.pickerMode) return row;
  const button = iconButton({ glyph:'more', className:'files-row-actions', title:`Actions for ${entry.name}`,
    onClick:event => {
      event.stopPropagation();
      const hit = hooks.__openClankFilesContextCapture(row);
      if (!hit) return;
      showCapturedFilesMenu(event.currentTarget, captureCommands({ context:'selection', columnIndex:hit.filesColumnIndex, treeEntry:null }));
    },
  });
  button.dataset.filesRowActions = '';
  button.setAttribute('aria-haspopup', 'menu');
  button.addEventListener('dblclick', event => event.stopPropagation());
  if (!details) return el('div', { class:'files-entry-frame', role:'presentation' }, row, button);
  row.classList.add('files-entry-frame');
  row.append(el('span', { class:'files-row-action-cell', role:'gridcell' }, button));
  return row;
}

function renderEntries() {
  syncPagingControl();
  if (options.navigationOnly) {
    const column = activeColumn();
    const query = String(column?.query || '');
    const input = state.sidebar?.querySelector('.files-folder-search');
    if (input) {
      input.disabled = !column?.resourceRef;
      input.title = column?.resourceRef ? 'Search names in the current authorized folder, including unloaded entries.' : 'Open an authorized folder before searching.';
      if (document.activeElement !== input) input.value = query;
    }
    const main = state.shell?.querySelector('.files-main');
    if (main) main.hidden = !query;
    state.shell?.classList.toggle('files-navigation-search-active', Boolean(query));
    if (!query) { state.shell?.querySelector('[data-files-body]')?.replaceChildren(); return; }
    // The compact navigation result surface shares Files rows and callbacks.
    state.mode = 'list';
    syncPagingControl();
  }
  const metrics = layoutMetrics();
  state.shell?.style.setProperty('--files-icon-size', metrics.size + 'px');
  state.shell?.style.setProperty('--files-preview-size', metrics.previewHeight + 'px');
  state.shell?.style.setProperty('--files-tile-width', metrics.tileWidth + 'px');
  state.shell?.style.setProperty('--files-row-height', (metrics.rowStride - (['grid','gallery'].includes(state.mode) ? metrics.gap : 0)) + 'px');
  const body = state.shell?.querySelector('[data-files-body]');
  if (!body) return;
  state.renderEpoch += 1;
  body.__filesSelectionBoxes = null;
  const savedScrollTop = body.scrollTop;
  const activeOverlay = state.gesture?.type === 'rectangle' ? state.gesture.overlay : null;
  if (state.mode !== 'columns') syncSelectionModel(state.entries, { append: false, columnIndex: state.columns.length - 1 });
  resetNativeThumbnails();
  body.className = `files-browser-body files-mode-${state.mode}`;
  body.replaceChildren();
  if (activeOverlay) body.append(activeOverlay);
  if (state.mode === 'columns') {
    body.append(renderColumns());
    updateManagedActionControl();
    return;
  }
  const details = state.mode === 'details';
  const entriesHost = details
    ? el('div', { class: 'files-details-grid', role: 'grid', 'aria-label': 'Files details' })
    : body;
  if (details) {
    entriesHost.append(renderDetailsHeader());
    body.append(entriesHost);
  }
  if (!state.entries.length) {
    const hasActiveQuery = Boolean(state.searchQuery || activeColumn()?.query);
    const message = hasActiveQuery
      ? 'No matches found in the searched locations.'
      : (state.provider === 'host' ? 'This folder is empty.' : 'No items in this source.');
    const empty = details
      ? el('div', { class: 'files-empty', role: 'row' }, el('span', { role: 'gridcell', text: message }))
      : el('div', { class: 'files-empty', text: message });
    entriesHost.append(empty);
    updateManagedActionControl();
    return;
  }
  const allEntries = state.entries;
  const virtualized = state.mode !== 'columns' && allEntries.length > 500;
  const gridMode = state.mode === 'grid' || state.mode === 'gallery';
  const gridGap = state.mode === 'gallery' ? 8 : 4;
  const gridColumns = gridMode ? Math.max(1, Math.floor(((body.clientWidth || 640) - 16 + gridGap) / (layoutMetrics().tileWidth + gridGap))) : 1;
  const rowHeight = layoutMetrics().rowStride;
  const windowSize = 240;
  const startRow = virtualized ? Math.max(0, Math.floor(savedScrollTop / rowHeight) - 2) : 0;
  const start = gridMode ? startRow * gridColumns : startRow;
  const end = virtualized ? Math.min(allEntries.length, start + (gridMode ? Math.ceil(windowSize / gridColumns) * gridColumns : windowSize)) : allEntries.length;
  const selectedTransferEntries = syncSelectionModel(allEntries, { append: false, columnIndex: state.columns.length - 1 }).selectedEntries();
  const selectedTransferCaps = selectedTransferEntries.map(transferCapabilities);
  const transferMoveAllowed = selectedTransferCaps.length > 0 && selectedTransferCaps.every((capability) => capability.move);
  const transferCopyAllowed = selectedTransferCaps.length > 0 && selectedTransferCaps.every((capability) => capability.copy);
  if (virtualized && start) entriesHost.append(el('div', { class: 'files-virtual-spacer', 'aria-hidden': 'true', style: `height:${startRow * rowHeight}px` }));
  for (const entry of allEntries.slice(start, end)) {
    const directory = isDirectory(entry);
    const path = entryPath(entry);
    const selectionKey = entrySelectionKey(entry);
    const selected = state.selected.has(selectionKey);
    const row = el(details ? 'div' : 'button', {
      type: details ? null : 'button',
      class: `files-entry ${directory ? 'directory' : 'file'}${selected ? ' selected' : ''}`,
      title: String(entry.provenance?.logical_path || entry.name || path),
      'aria-label': String(entry.name || path),
      'data-copal-context-object': options.pickerMode ? null : 'file',
      'data-resource-ref': entry.resource_ref || '',
      'data-path': isFacadeEntry(entry) ? '' : String(entry?.path || path),
      'data-file-name': entry.name,
      'data-file-capabilities': (entry.capabilities || []).join(' '),
      'data-file-open-editor': isTextualEntry(entry) && entry.capabilities?.includes('open') ? 'true' : 'false',
      'data-file-transfer-move': transferMoveAllowed ? 'true' : 'false',
      'data-file-transfer-copy': transferCopyAllowed ? 'true' : 'false',
      role: details ? 'row' : null,
      tabindex: details ? '0' : null,
      'aria-selected': selected ? 'true' : 'false',
      draggable: !options.pickerMode && canDragEntry(entry) ? 'true' : null,
      'data-files-selection-key': selectionKey,
    });
    const cellAttrs = details ? { role: 'gridcell' } : {};
    const visual = entryVisual(entry, directory);
    if (details) visual.setAttribute('role', 'gridcell');
    row.append(
      visual,
      el('span', { ...cellAttrs, class: 'files-entry-name', text: entry.name }),
      el('span', { ...cellAttrs, class: 'files-entry-kind', text: directory ? 'Folder' : (entry.media_type || 'File') }),
      el('span', { ...cellAttrs, class: 'files-entry-size', text: directory ? '' : formatBytes(entry.size) }),
      el('span', { ...cellAttrs, class: 'files-entry-modified', text: formatModified(entry.modified_unix_ms || entry.modified_ms || entry.modified) }),
    );
    row.addEventListener('dblclick', () => {
      if (directory) {
        if (isFacadeEntry(entry)) return openManagedDirectory(entry);
        setStatus('Refresh the authorized Files roots before opening this folder.', true);
        return undefined;
      }
      if (options.pickerMode) return confirmPickerEntry(entry);
      if (entry.resource_ref && entry.capabilities?.includes('open')) return openManagedEntry(entry);
      if (isPreviewable(entry, path)) return previewEntry(entry, path);
      if (entry.download_url) return downloadEntry(entry);
      if (state.provider === 'host') return setStatus('Refresh the authorized Files roots before downloading this file.', true);
      setStatus(`${entry.name} is managed by ${entry.provider || state.provider}; open it in its source app.`);
      return undefined;
    });
    row.addEventListener('click', (event) => {
      if (event.detail === 0) return;
      const model = ensureSelectionModel(activeColumn());
      model.select(selectionKey, options.pickerMode ? {} : event);
      // Keep this row mounted between click and dblclick. Remounting also
      // restarts expired thumbnails, whose policy refresh can cancel the open.
      applyModelSelection(model, { render: false });
      syncVisibleSelection(model, entriesHost);
      updateManagedActionControl();
      const capabilities = model.selectedEntries().map(transferCapabilities);
      for (const node of entriesHost.querySelectorAll('[data-files-selection-key]')) {
        node.dataset.fileTransferMove = capabilities.length > 0 && capabilities.every(capability => capability.move) ? 'true' : 'false';
        node.dataset.fileTransferCopy = capabilities.length > 0 && capabilities.every(capability => capability.copy) ? 'true' : 'false';
      }
      row.focus({ preventScroll: true });
    });
    row.addEventListener('keydown', (event) => {
      if (event.target !== row) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'ArrowLeft' || event.key === 'ArrowRight' || event.key === 'Home' || event.key === 'End' || event.key === 'PageDown' || event.key === 'PageUp') {
        event.preventDefault();
        const model = ensureSelectionModel(activeColumn());
        const index = model.orderedKeys().indexOf(selectionKey);
        const body = state.shell?.querySelector('[data-files-body]');
        const grid = state.mode === 'grid' || state.mode === 'gallery';
        const gap = state.mode === 'gallery' ? 8 : 4;
        const columns = grid ? Math.max(1, Math.floor(((body?.clientWidth || 640) - 16 + gap) / (layoutMetrics().tileWidth + gap))) : 1;
        const page = Math.max(1, Math.floor((body?.clientHeight || 640) / layoutMetrics().rowStride)) * columns;
        const delta = event.key === 'PageDown' ? page : event.key === 'PageUp' ? -page : event.key === 'ArrowDown' ? columns : event.key === 'ArrowUp' ? -columns : event.key === 'ArrowRight' ? 1 : -1;
        const target = event.key === 'Home' ? 0 : event.key === 'End' ? model.orderedKeys().length - 1 : index + delta;
        const key = model.orderedKeys()[Math.max(0, Math.min(model.orderedKeys().length - 1, target))];
        model.select(key, options.pickerMode ? {} : event);
        if (body && state.entries.length > 500) {
          const rowHeight = layoutMetrics().rowStride;
          body.scrollTop = Math.floor(Math.max(0, Math.min(model.orderedKeys().length - 1, target)) / columns) * rowHeight;
        }
        applyModelSelection(model);
        requestAnimationFrame(() => {
          const targetRow = state.shell?.querySelector(`[data-files-selection-key="${CSS.escape(key)}"]`);
          if (!targetRow || !body?.contains(targetRow)) return;
          // Scroll only the Files viewport; native scrollIntoView can move the
          // floating window or the outer page as well as this nested body.
          const viewport = body.getBoundingClientRect();
          const bounds = targetRow.getBoundingClientRect();
          const top = viewport.top + body.clientTop;
          const bottom = top + body.clientHeight;
          if (bounds.top < top) body.scrollTop -= top - bounds.top;
          else if (bounds.bottom > bottom) body.scrollTop += bounds.bottom - bottom;
          targetRow.focus({ preventScroll: true });
        });
        return;
      }
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      if (directory) {
        if (isFacadeEntry(entry)) void openManagedDirectory(entry);
        else setStatus('Refresh the authorized Files roots before opening this folder.', true);
      }
      else if (options.pickerMode) confirmPickerEntry(entry);
      else if (entry.resource_ref && entry.capabilities?.includes('open')) openManagedEntry(entry);
      else if (isPreviewable(entry, path)) previewEntry(entry, path);
      else if (options.pickerMode) confirmPickerEntry(entry);
      else if (entry.download_url) downloadEntry(entry);
      else if (state.provider === 'host') setStatus('Refresh the authorized Files roots before downloading this file.', true);
    });
    entriesHost.append(withFileRowActions(row, entry, details));
  }
  if (virtualized && end < allEntries.length) {
    const remainingRows = gridMode ? Math.ceil((allEntries.length - end) / gridColumns) : allEntries.length - end;
    entriesHost.append(el('div', { class: 'files-virtual-spacer', 'aria-hidden': 'true', style: `height:${remainingRows * rowHeight}px` }));
  }
  updateManagedActionControl();
  body.scrollTop = savedScrollTop;
}

function renderColumns() {
  const browser = el('div', { class: 'files-columns-browser', role: 'group', 'aria-label': 'Columns view' });
  const columns = state.columns.length
    ? state.columns
    : [{ path: state.path || state.hostPath || state.provider, name: displayName(state.path || state.provider), entries: state.entries, nextCursor: state.nextCursor, sort: state.sort }];
  for (const [index, column] of columns.entries()) {
    column.sortKeys = sortKeysFor(column.sortKeys || SORT_KEYS);
    column.sort = constrainSortSpec(column.sort || state.sort, column.sortKeys);
    const columnName = column.name || displayName(column.path);
    const panel = el('section', { class: 'files-column', 'aria-label': columnName, 'data-column-index': index });
    const head = el('div', { class: 'files-column-head' }, el('span', { text: columnName }), el('span', { class: 'files-column-count', text: column.nextCursor ? `${column.entries.length}+` : String(column.entries.length) }));
    const columnSort = appendSortOptions(el('select', {
      class: 'files-column-sort-select',
      'aria-label': `Sort ${columnName} column`,
      title: `Sort ${columnName} column`,
    }), column.sortKeys);
    updateSortSelect(columnSort, column.sort, column.sortKeys);
    columnSort.addEventListener('change', () => {
      const [key, direction] = columnSort.value.split(':');
      void applyColumnSort(index, { ...column.sort, key, direction });
    });
    const foldersFirst = el('button', {
      type: 'button',
      class: 'files-column-folders-first',
      'data-column-folders-first': index,
    }, namedGlyph('folder', { size: 13, className: 'files-button-glyph' }));
    updateFoldersFirstControl(foldersFirst, column.sort);
    foldersFirst.addEventListener('click', () => {
      void applyColumnSort(index, { ...column.sort, directoriesFirst: !column.sort.directoriesFirst });
    });
    const sortControls = el('div', { class: 'files-column-sort-controls' }, columnSort, foldersFirst);
    const list = el('div', { class: 'files-column-list', role: 'listbox', 'aria-label': `${columnName} items` });
    const sortedEntries = column.serverOrdered || column.resourceRef
      ? [...(column.entries || [])]
      : sortEntries(column.entries || [], column.sort);
    const columnModel = ensureSelectionModel(column, index);
    columnModel.setItems(sortedEntries);
    columnModel.replaceSelection(column.selection || []);
    const selectedTransferEntries = columnModel.selectedEntries();
    const selectedTransferCaps = selectedTransferEntries.map(transferCapabilities);
    const transferMoveAllowed = selectedTransferCaps.length > 0 && selectedTransferCaps.every((capability) => capability.move);
    const transferCopyAllowed = selectedTransferCaps.length > 0 && selectedTransferCaps.every((capability) => capability.copy);
    const columnVirtualized = sortedEntries.length > 500;
    const columnRowHeight = 34;
    const columnScrollTop = Number(column.scrollTop || 0);
    const columnStart = columnVirtualized ? Math.max(0, Math.min(sortedEntries.length - 1, Math.floor(columnScrollTop / columnRowHeight) - 10)) : 0;
    const columnEnd = columnVirtualized ? Math.min(sortedEntries.length, columnStart + 240) : sortedEntries.length;
    if (columnVirtualized && columnStart) list.append(el('div', { class: 'files-virtual-spacer', 'aria-hidden': 'true', style: `height:${columnStart * columnRowHeight}px` }));
    for (const [entryIndex, entry] of sortedEntries.slice(columnStart, columnEnd).entries()) {
      const actualEntryIndex = columnStart + entryIndex;
      const directory = isDirectory(entry);
      const path = isFacadeEntry(entry) ? entryPath(entry) : childPath(column.path, entry.name);
      const selectionKey = entrySelectionKey(entry);
      const selected = columnModel.has(selectionKey);
      const item = el('button', {
        type: 'button',
        class: `files-column-entry${directory ? ' directory' : ''}${selected ? ' selected' : ''}`,
        'data-copal-context-object': options.pickerMode ? null : 'file',
        'data-resource-ref': entry.resource_ref || '',
        'data-path': isFacadeEntry(entry) ? '' : String(entry?.path || path),
        'data-file-name': entry.name,
        'data-file-capabilities': (entry.capabilities || []).join(' '),
        'data-file-open-editor': isTextualEntry(entry) && entry.capabilities?.includes('open') ? 'true' : 'false',
        'data-file-transfer-move': transferMoveAllowed ? 'true' : 'false',
        'data-file-transfer-copy': transferCopyAllowed ? 'true' : 'false',
        role: 'option',
        'aria-label': entry.name,
        'aria-selected': selected ? 'true' : 'false',
        'data-column-entry-index': actualEntryIndex,
        title: String(entry.provenance?.logical_path || entry.name || path),
        draggable: !options.pickerMode && canDragEntry(entry) ? 'true' : null,
        'data-files-selection-key': selectionKey,
      });
      item.append(iconNode(entry, { size: 17, className: 'files-column-icon', open: directory }), el('span', { class: 'files-column-name', text: entry.name }));
      item.addEventListener('click', (event) => {
        state.activeColumnIndex = index;
        columnModel.select(selectionKey, options.pickerMode ? {} : event);
        column.selection = new Set(columnModel.selectedKeys());
        state.selected = new Set(column.selection);
        state.treeSelection = null; options.onSelection?.(columnModel.selectedEntries()[0] || null);
        markColumnEntryActive(index, item, entry.name);
        renderEntries();
        if (directory) {
          if (isFacadeEntry(entry)) void openManagedDirectory(entry, index);
          else setStatus('Refresh the authorized Files roots before opening this folder.', true);
        }
        else if (!options.pickerMode && !directory && isPreviewable(entry, path)) void previewEntry(entry, path);
      });
      item.addEventListener('keydown', (event) => {
        void handleColumnEntryKeydown(event, { columnIndex: index, entryIndex: actualEntryIndex, entry, path, directory });
      });
      list.append(withFileRowActions(item, entry));
    }
    if (columnVirtualized && columnEnd < sortedEntries.length) list.append(el('div', { class: 'files-virtual-spacer', 'aria-hidden': 'true', style: `height:${(sortedEntries.length - columnEnd) * columnRowHeight}px` }));
    if (columnVirtualized) {
      list.scrollTop = columnScrollTop;
      list.addEventListener('scroll', () => {
        column.scrollTop = list.scrollTop;
        if (list.dataset.filesVirtualFrame) return;
        list.dataset.filesVirtualFrame = 'true';
        requestAnimationFrame(() => {
          list.dataset.filesVirtualFrame = '';
          renderEntries();
          if (state.gesture?.type === 'rectangle' && state.gesture.viewport === list) updateSelectionRectangle();
        });
      }, { passive: true });
    }
    if (column.nextCursor) {
      const more = el('button', { type: 'button', class: 'files-column-more', text: 'Load more' });
      more.addEventListener('click', () => void (column.resourceRef
        ? loadManagedColumnMore(index)
        : setStatus('Refresh the authorized Files roots before loading more items.', true)));
      list.append(more);
    }
    panel.append(head, sortControls, list);
    browser.append(panel);
  }
  return browser;
}

function markColumnEntryActive(columnIndex, item, name) {
  const column = state.columns[columnIndex] || null;
  if (column) column.activeName = name;
  item.classList.add('active');
}

function focusColumnEntry(columnIndex, target = 'first') {
  const panel = state.shell?.querySelector(`.files-column[data-column-index="${columnIndex}"]`);
  const column = state.columns[columnIndex];
  if (!panel || !column) return false;
  const model = ensureSelectionModel(column, columnIndex);
  const keys = model.orderedKeys();
  if (!keys.length) return false;
  const selected = model.selectedKeys();
  const targetIndex = target === 'selected'
    ? Math.max(0, keys.indexOf(selected[0]))
    : target === 'last' ? keys.length - 1
      : Number.isInteger(target) ? Math.max(0, Math.min(keys.length - 1, target)) : 0;
  column.scrollTop = targetIndex * 34;
  state.activeColumnIndex = columnIndex;
  state.entries = column.entries || [];
  state.selected = new Set(model.selectedKeys());
  state.treeSelection = null; options.onSelection?.(model.selectedEntries()[0] || null);
  renderEntries();
  requestAnimationFrame(() => state.shell?.querySelector(`.files-column[data-column-index="${columnIndex}"] [data-column-entry-index="${targetIndex}"]`)?.focus({ preventScroll: true }));
  return true;
}

async function handleColumnEntryKeydown(event, { columnIndex, entryIndex, entry, path, directory }) {
  const movement = {
    ArrowUp: entryIndex - 1,
    ArrowDown: entryIndex + 1,
    Home: 0,
    End: Number.MAX_SAFE_INTEGER,
    PageUp: entryIndex - 20, PageDown: entryIndex + 20,
  };
  if (Object.prototype.hasOwnProperty.call(movement, event.key)) {
    event.preventDefault();
    const selectedColumn = activateColumn(columnIndex);
    const model = selectedColumn.model;
    const keys = model.orderedKeys();
    const currentIndex = Math.max(0, keys.indexOf(model.focusKey || resourceKey(entry, selectedColumn.column?.provider || state.provider)));
    const targetIndex = event.key === 'Home' ? 0 : event.key === 'End' ? keys.length - 1 : Math.max(0, Math.min(keys.length - 1, currentIndex + (event.key === 'PageDown' ? Math.max(1, Math.floor((event.currentTarget?.closest('.files-column-list')?.clientHeight || 680) / 34)) : event.key === 'PageUp' ? -Math.max(1, Math.floor((event.currentTarget?.closest('.files-column-list')?.clientHeight || 680) / 34)) : event.key === 'ArrowDown' ? 1 : -1)));
    const targetKey = keys[targetIndex];
    model.select(targetKey, options.pickerMode ? {} : event);
    selectedColumn.column.selection = new Set(model.selectedKeys());
    state.selected = new Set(model.selectedKeys());
    state.treeSelection = null; options.onSelection?.(model.selectedEntries()[0] || null);
    focusColumnEntry(columnIndex, targetIndex);
    return;
  }
  if (options.pickerMode && (event.key === 'Enter' || event.key === ' ')) {
    event.preventDefault();
    if (directory) await openManagedDirectory(entry, columnIndex);
    else confirmPickerEntry(entry);
    return;
  }
  if (event.key === 'ArrowLeft' && columnIndex > 0) {
    event.preventDefault();
    activateColumn(columnIndex - 1);
    focusColumnEntry(columnIndex - 1, 'selected');
    return;
  }
  if (event.key === 'ArrowRight' && directory) {
    event.preventDefault();
    const selectedColumn = activateColumn(columnIndex);
    selectedColumn.model.select(resourceKey(entry, selectedColumn.column?.provider || state.provider), options.pickerMode ? {} : event);
    state.treeSelection = null; options.onSelection?.(entry);
    selectedColumn.column.selection = new Set(selectedColumn.model.selectedKeys());
    state.selected = new Set(selectedColumn.model.selectedKeys());
    if (!isFacadeEntry(entry)) {
      setStatus('Refresh the authorized Files roots before opening this folder.', true);
      return;
    }
    const opened = await openManagedDirectory(entry, columnIndex);
    if (opened) requestAnimationFrame(() => focusColumnEntry(columnIndex + 1, 'first'));
  }
}

async function applyColumnSort(index, spec) {
  const column = state.columns[index];
  if (!column) return false;
  if (column.globalSearch) {
    activateColumn(index);
    return applyPrimarySort(spec);
  }
  if (!column.resourceRef && column.nextCursor) {
    setStatus('Refresh the authorized Files roots before sorting this folder.', true);
    return false;
  }
  column.sort = constrainSortSpec(spec, column.sortKeys || SORT_KEYS);
  if (column.resourceRef) return reloadManagedColumn(index, { preserveColumns:true });
  column.entries = sortEntries(column.entries || [], column.sort);
  if (activeColumn() === column) {
    state.sort = column.sort;
    state.entries = column.entries;
    rememberViewPreferences();
    syncPrimarySortControls();
  }
  renderEntries();
  return true;
}

async function reloadColumn(index) {
  const column = state.columns[index];
  if (!column || state.provider !== 'host') return false;
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  try {
    const response = await filesServiceClient.listDirectory(column.path, {
      sort: requestSortSpec(column.sort),
      cache: false,
      cacheKey: `files-column-reload:${column.path}:${JSON.stringify(column.sort)}`,
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || controller.signal.aborted || !state.nativeWindow?.visible) return false;
    const data = response?.data || {};
    column.path = String(data.path || column.path);
    column.entries = sortEntries(data.entries || [], column.sort);
    column.nextCursor = data.next_cursor || null;
    if (index === state.columns.length - 1) {
      state.hostPath = column.path;
      state.path = column.path;
      state.entries = column.entries;
      state.nextCursor = column.nextCursor;
    }
    renderEntries();
    setStatus(`${column.entries.length}${column.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError') setStatus(error.message || 'Column could not be sorted', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function openColumnDirectory(path, parentIndex) {
  if (state.provider !== 'host') return false;
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const inheritedSort = normalizeSortSpec(state.columns[parentIndex]?.sort || state.sort);
  try {
    const response = await filesServiceClient.listDirectory(path, {
      sort: requestSortSpec(inheritedSort),
      cache: false,
      cacheKey: `files-column:${path}:${JSON.stringify(inheritedSort)}`,
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || controller.signal.aborted || !state.nativeWindow?.visible) return false;
    const data = response?.data || {};
    const canonical = String(data.path || path);
    if (state.columns[parentIndex]) state.columns[parentIndex].activeName = displayName(canonical);
    const column = { path: canonical, entries: sortEntries(data.entries || [], inheritedSort), nextCursor: data.next_cursor || null, sort: inheritedSort, activeName: '' };
    state.columns = [...state.columns.slice(0, parentIndex + 1), column];
    state.activeColumnIndex = state.columns.length - 1;
    state.hostPath = canonical;
    state.path = canonical;
    state.entries = column.entries;
    state.nextCursor = column.nextCursor;
    const label = state.shell?.querySelector('[data-files-path]');
    if (label) label.textContent = canonical;
    clearFilesSelection();
    renderEntries();
    updateTreeHighlight();
    updateFavoriteButton();
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError') setStatus(error.message || 'Column unavailable', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function loadColumnMore(index) {
  const column = state.columns[index];
  if (!column?.nextCursor || state.provider !== 'host') return false;
  const controller = new AbortController();
  state.contentController?.abort();
  state.contentController = controller;
  const generation = state.contentGeneration;
  try {
    const response = await filesServiceClient.listDirectory(column.path, {
      cursor: column.nextCursor,
      sort: requestSortSpec(column.sort || state.sort),
      cache: false,
      cacheKey: `files-column:${column.path}:${JSON.stringify(column.nextCursor)}:${JSON.stringify(column.sort || state.sort)}`,
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || controller.signal.aborted) return false;
    const data = response?.data || {};
    column.entries = sortEntries([...column.entries, ...(data.entries || [])], column.sort || state.sort);
    column.nextCursor = data.next_cursor || null;
    if (index === state.columns.length - 1) {
      state.entries = column.entries;
      state.nextCursor = column.nextCursor;
    }
    renderEntries();
    return true;
  } catch (error) {
    if (error?.code === 'stale_cursor' && generation === state.contentGeneration) {
      return reloadColumn(index);
    }
    if (error?.name !== 'AbortError') setStatus(error.message || 'More column items could not be loaded', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function closePreview() {
  state.previewController?.abort?.();
  state.previewController = null;
  state.previewHandle = null;
  const preview = state.shell?.querySelector('[data-files-preview]');
  if (!preview) return;
  const media = preview.querySelector('audio, img');
  if (media) {
    if (media.tagName === 'AUDIO') media.pause();
    media.removeAttribute('src');
    if (media.tagName === 'AUDIO') media.load();
  }
  preview.hidden = true;
  preview.replaceChildren();
}

async function previewEntry(entry, path, isCurrent = () => true) {
  if (!isCurrent()) return;
  if(!state.graphicalMedia&&state.shell)state.graphicalMedia=mountImageRegion({root:state.shell,selector:'.files-thumbnail, .files-preview-image',isCurrent:()=>!!state.shell?.isConnected});
  const preview = state.shell?.querySelector('[data-files-preview]');
  if (!preview) return;
  if (!isFacadeEntry(entry)) {
    setStatus('Refresh the authorized Files roots before previewing this item.', true);
    return;
  }
  closePreview();
  const controller = new AbortController(); state.previewController = controller;
  preview.hidden = false; preview.replaceChildren(el('div', { class: 'files-preview-loading', text: 'Loading preview…' }));
  let kind = previewKind(entry, path);
  const provider = state.provider;
  const owner = state.owner, lifecycle = state.lifecycleGeneration, policy = filesPolicyGeneration(), workspace = copalWorkspace();
  let handle = null;
  let url = entry.preview_url || entry.download_url;
  try {
    if (entry.format?.formatId === 'iwi' && !entry.format?.providerRevision) {
      const result = await filesFacadeClient.stat(entry.resource_ref, { signal:controller.signal });
      if (controller.signal.aborted || state.previewController !== controller || state.owner !== owner
          || state.lifecycleGeneration !== lifecycle || filesPolicyGeneration() !== policy || copalWorkspace() !== workspace
          || state.provider !== provider) return;
      const fresh = result;
      if (!fresh || fresh.id !== entry.resource_id || fresh.provider !== entry.provider || !fresh.revision
          || fresh.format?.formatId !== 'iwi') throw new Error('The preview source changed. Refresh the item.');
      entry = facadeEntry(fresh); kind = previewKind(entry, path); url = entry.preview_url;
      if (!url) throw new Error('This preview is unavailable.');
    }
    if (kind === 'video') {
      const fresh=await filesFacadeClient.stat(entry.resource_ref,{signal:controller.signal});
      if(controller.signal.aborted || state.previewController!==controller || state.owner!==owner || state.lifecycleGeneration!==lifecycle || filesPolicyGeneration()!==policy || copalWorkspace()!==workspace || state.provider!==provider) return;
      if(!fresh || fresh.id!==entry.resource_id || fresh.provider!=='host' || fresh.format?.formatId!=='video' || !fresh.revision) throw Error('The recording source changed. Refresh the item.');
      entry=facadeEntry(fresh);url=filesFacadeClient.contentUrl(entry.resource_ref,{purpose:'preview',expectedRevision:fresh.revision});
    }
    let iwiEvidence = null;
    if (entry.format?.formatId === 'iwi') {
      const response = await fetch(url, { credentials:'same-origin', signal:controller.signal });
      const derived = await readIwiPreview(response, { descriptor:entry.format, revision:entry.format.providerRevision, signal:controller.signal });
      const digest = await crypto.subtle.digest('SHA-256', await derived.blob.arrayBuffer());
      if (controller.signal.aborted || state.previewController !== controller || state.owner !== owner || state.lifecycleGeneration !== lifecycle || filesPolicyGeneration() !== policy || copalWorkspace() !== workspace) return;
      iwiEvidence = { ...derived.evidence, pngSHA256:Array.from(new Uint8Array(digest), b=>b.toString(16).padStart(2,'0')).join('') };
      url = URL.createObjectURL(derived.blob);
      controller.signal.addEventListener('abort', () => URL.revokeObjectURL(url), { once:true });
    }
    const close = iconButton({ glyph: 'close', title: 'Close preview', onClick: closePreview });
    const head = el('div', { class: 'files-preview-head' }, el('strong', { text: entry.name || displayName(path) }), close);
    if (iwiEvidence) {
      const save = el('button', { type:'button', text:'Save PNG copy…' });
      save.addEventListener('click', async () => {
        const current = () => !controller.signal.aborted && state.previewController === controller && state.owner === owner
          && state.lifecycleGeneration === lifecycle && filesPolicyGeneration() === policy && copalWorkspace() === workspace;
        try {
          const { createResourcePicker } = await import('./copal/resourcePicker.js');
          if (!current()) return;
          let selected = null;
          const picker = createResourcePicker({ client:filesFacadeClient, purpose:'folder', accountScope:owner, workspaceScope:workspace,
            generation:policy, originWindow:state.nativeWindow, getGeneration:()=>filesPolicyGeneration(), getAccountScope:()=>state.owner,
            getWorkspaceScope:()=>copalWorkspace(), isOriginCurrent:current,
            isCompatible:resource=>resource.capabilities?.write ? '' : 'Choose a writable folder.',
            onSelect:resource=>{ selected=resource; return true; },
            onClose:async cancelled=>{
              if (cancelled || !selected || !current()) return;
              try {
                const name = await uiModule.styledPrompt('Name the new PNG copy.', { title:'Save PNG copy', defaultValue:`${entry.name}.png` });
                if (!name || !current()) return;
                const operation = `iwi-${crypto.randomUUID()}`;
                const request = { operation_id:operation, item_id:operation, generation:policy, source_ref:entry.resource_ref,
                  revision:iwiEvidence.revision, source_sha256:iwiEvidence.sourceSHA256, decoder:iwiEvidence.decoder,
                  png_sha256:iwiEvidence.pngSHA256, destination_ref:selected.ref, name:String(name).trim() };
                let result;
                try { result = await filesFacadeClient.saveIwiPngCopy(request); }
                catch (error) {
                  if (!current()) return;
                  if (error.status >= 400 && error.status < 500 && error.code !== 'mutation_outcome_unknown') throw error;
                  try { result = await filesFacadeClient.operationReceipt(operation, { generation:policy, signal:controller.signal }); }
                  catch (_) { throw new Error(`Save outcome is unknown. Check operation ${operation} before retrying.`); }
                }
                if (current()) setStatus(result.items?.[0]?.outcome === 'committed' ? 'PNG copy saved; original retained.' : `PNG copy status: ${result.items?.[0]?.code || result.state}. Operation ${operation}`);
              } catch (error) { if (current()) setStatus(error.message || 'PNG copy unavailable.', true); }
            } });
          void picker.open();
        } catch (error) { if (current()) setStatus(error.message || 'PNG copy unavailable.', true); }
      });
      head.append(save);
    }
    const content = el('div', { class: 'files-preview-content' });
    if (kind === 'video') {
      const video=el('video',{controls:true,preload:'metadata',src:url,'aria-label':`Recording preview for ${entry.name}`});video.style.maxWidth='100%';
      video.addEventListener('error',()=>setStatus('Video playback unavailable or codec unsupported; original download remains available.',true),{once:true});
      controller.signal.addEventListener('abort',()=>{video.pause();video.removeAttribute('src');video.load();},{once:true});
      const open=el('button',{type:'button',text:'Review in Editor…'});open.addEventListener('click',async()=>{
        const valid=()=>!controller.signal.aborted && state.previewController===controller && state.owner===owner && state.lifecycleGeneration===lifecycle && filesPolicyGeneration()===policy && copalWorkspace()===workspace;
        try{const copal=window.copalModule || await import('./copal.js');if(valid())await copal.openResource(entry.resource_ref,{expectedRevision:entry.revision,isCurrent:valid});}catch(error){if(valid())setStatus(error.message || 'Editor recording review unavailable.',true);}
      });content.append(video,open);
    } else if (kind === 'image' || kind === 'audio') {
      // Let the browser media engine consume the opaque authorized Rust
      // stream. This avoids assembling a second full-size Blob in the Files
      // UI and keeps audio seeking/range requests useful for recordings.
      const media = kind === 'image'
        ? el('img', { class: 'files-preview-image', src: url, alt: entry.name || displayName(path), loading: 'lazy' })
        : el('audio', { class: 'files-preview-audio-engine', preload: 'metadata', src: url, 'aria-label': `Audio preview for ${entry.name || displayName(path)}` });
      media.addEventListener('error', () => setStatus('Preview unavailable', true), { once: true });
      if(kind==='image')media.addEventListener('load',()=>{markTrustedJpegRepresentation(media,{source:media.src,mediaType:entry.media_type,key:String(entry.resource_ref||entry.resource_id||'')});state.graphicalMedia?.refresh();},{once:true});
      if (kind === 'image') {
        const viewport = el('div', { class: 'files-preview-image-viewport' }, media);
        const meta = el('div', { class: 'files-preview-meta files-preview-media-meta' });
        const fit = el('button', { type: 'button', class: 'files-preview-transport-button active', text: 'Fit', 'aria-pressed': 'true' });
        const actual = el('button', { type: 'button', class: 'files-preview-transport-button', text: 'Actual size', 'aria-pressed': 'false' });
        const zoomOut = el('button', { type: 'button', class: 'files-preview-transport-button', text: '−', 'aria-label': 'Zoom out' });
        const zoomIn = el('button', { type: 'button', class: 'files-preview-transport-button', text: '+', 'aria-label': 'Zoom in' });
        const zoomLabel = el('span', { class: 'files-preview-zoom-label', text: 'Fit' });
        const controls = el('div', { class: 'files-preview-image-controls', role: 'group', 'aria-label': 'Image size' }, fit, actual, zoomOut, zoomLabel, zoomIn);
        let fitted = true;
        let zoom = 1;
        const updateImage = () => {
          fit.classList.toggle('active', fitted);
          fit.setAttribute('aria-pressed', fitted ? 'true' : 'false');
          actual.classList.toggle('active', !fitted && zoom === 1);
          actual.setAttribute('aria-pressed', !fitted && zoom === 1 ? 'true' : 'false');
          zoomLabel.textContent = fitted ? 'Fit' : `${Math.round(zoom * 100)}%`;
          if (fitted) {
            media.style.width = '';
            media.style.maxWidth = '100%';
            media.style.maxHeight = '260px';
          } else {
            media.style.width = media.naturalWidth ? `${Math.max(1, Math.round(media.naturalWidth * zoom))}px` : '';
            media.style.maxWidth = 'none';
            media.style.maxHeight = 'none';
          }
          state.graphicalMedia?.refresh();
        };
        fit.addEventListener('click', () => { fitted = true; updateImage(); });
        actual.addEventListener('click', () => { fitted = false; zoom = 1; updateImage(); });
        zoomOut.addEventListener('click', () => { fitted = false; zoom = Math.max(0.25, Math.round((zoom - 0.25) * 100) / 100); updateImage(); });
        zoomIn.addEventListener('click', () => { fitted = false; zoom = Math.min(4, Math.round((zoom + 0.25) * 100) / 100); updateImage(); });
        media.addEventListener('load', () => {
          const type = handle?.media_type || entry.media_type || 'Image';
          const size = handle?.size ?? entry.size;
          meta.textContent = [
            media.naturalWidth && media.naturalHeight ? `${media.naturalWidth} × ${media.naturalHeight}` : null,
            type,
            Number.isFinite(Number(size)) ? formatBytes(size) : null,
          ].filter(Boolean).join(' · ');
          updateImage();
        }, { once: true });
        content.append(controls, meta, viewport);
      } else {
        const play = el('button', { type: 'button', class: 'files-preview-transport-button', text: 'Play', 'aria-label': 'Play preview' });
        const seek = el('input', { type: 'range', class: 'files-preview-seek', min: '0', max: '0', value: '0', step: '0.1', 'aria-label': 'Seek audio preview' });
        const time = el('span', { class: 'files-preview-time', text: '0:00 / 0:00' });
        const mute = el('button', { type: 'button', class: 'files-preview-transport-button', text: 'Mute', 'aria-pressed': 'false' });
        const volume = el('input', { type: 'range', class: 'files-preview-volume', min: '0', max: '1', value: '1', step: '0.05', 'aria-label': 'Preview volume' });
        const transport = el('div', { class: 'files-preview-transport' }, play, seek, time, mute, volume);
        const meta = el('div', { class: 'files-preview-meta files-preview-media-meta', text: [handle?.media_type || entry.media_type || 'Audio', formatBytes(handle?.size ?? entry.size)].filter(Boolean).join(' · ') });
        const clock = value => { const numeric = Number(value); const seconds = Number.isFinite(numeric) ? Math.max(0, numeric) : 0; return `${Math.floor(seconds / 60)}:${String(Math.floor(seconds % 60)).padStart(2, '0')}`; };
        const update = () => { seek.max = String(Number.isFinite(media.duration) ? media.duration : 0); seek.value = String(media.currentTime || 0); time.textContent = `${clock(media.currentTime)} / ${clock(media.duration)}`; };
        play.addEventListener('click', async () => {
          if (media.paused) { try { await media.play(); } catch (_) { setStatus('Audio playback was blocked', true); } }
          else media.pause();
        });
        mute.addEventListener('click', () => { media.muted = !media.muted; });
        volume.addEventListener('input', () => {
          media.volume = Math.max(0, Math.min(1, Number(volume.value) || 0));
          if (media.volume > 0) media.muted = false;
        });
        media.addEventListener('volumechange', () => {
          volume.value = String(media.volume);
          const muted = media.muted || media.volume === 0;
          mute.textContent = muted ? 'Unmute' : 'Mute';
          mute.setAttribute('aria-pressed', muted ? 'true' : 'false');
        });
        media.addEventListener('play', () => { play.textContent = 'Pause'; });
        media.addEventListener('pause', () => { play.textContent = 'Play'; });
        media.addEventListener('loadedmetadata', update);
        media.addEventListener('timeupdate', update);
        seek.addEventListener('input', () => { media.currentTime = Number(seek.value) || 0; });
        content.append(meta, media, transport);
      }
    } else {
      const response = await fetch(url, { credentials: 'same-origin', signal: controller.signal });
      if (!response.ok) throw new Error(`Preview failed (${response.status})`);
      const bounded = await readBoundedTextResponse(response, { signal: controller.signal });
      if (controller.signal.aborted) return;
      const suffix = bounded.truncated
        ? `\n\n… [preview truncated${Number.isFinite(bounded.size) ? `; ${formatBytes(bounded.size)} total` : ''}; download for the complete file] …`
        : '';
      if (bounded.truncated) {
        const downloadFull = el('button', {
          type: 'button',
          class: 'files-preview-transport-button',
          text: 'Download full file',
          onClick: () => downloadEntry(entry),
        });
        content.append(
          el('div', { class: 'files-preview-meta', text: `${bounded.encoding} · start-only preview · tail unavailable from this source` }),
          downloadFull,
        );
      }
      content.append(el('pre', { class: 'files-preview-text', text: bounded.text + suffix }));
    }
    if (!isCurrent()) {
      if (state.previewController === controller) closePreview();
      return;
    }
    preview.replaceChildren(head, content);
  } catch (error) {
    if (!isCurrent()) {
      if (state.previewController === controller) closePreview();
      return;
    }
    if (error?.name !== 'AbortError') {
      if (entry.download_url) {
        const fallbackClose = iconButton({ glyph: 'close', title: 'Close preview', onClick: closePreview });
        const fallbackHead = el('div', { class: 'files-preview-head' }, el('strong', { text: entry.name || displayName(path) }), fallbackClose);
        const fallbackContent = el(
          'div',
          { class: 'files-preview-content' },
          el('div', { class: 'files-preview-error', text: error.message || 'Preview unavailable' }),
          el('button', {
            type: 'button',
            class: 'files-preview-transport-button',
            text: 'Download file',
            onClick: () => downloadEntry(entry),
          }),
        );
        preview.replaceChildren(fallbackHead, fallbackContent);
      } else {
        preview.replaceChildren(el('div', { class: 'files-preview-error', text: error.message || 'Preview unavailable' }));
      }
    }
  } finally { if (state.previewController === controller) state.previewController = null; }
}

async function openDirectory(requestedPath) {
  const requested = String(requestedPath || '').trim();
  if (!requested) return false;
  stopActiveFolderWatch();
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const body = state.shell?.querySelector('[data-files-body]');
  if (body) body.replaceChildren(el('div', { class: 'files-loading', text: 'Loading…' }));
  const label = state.shell?.querySelector('[data-files-path]');
  if (label) label.textContent = requested;
  try {
    const response = await filesServiceClient.listDirectory(requested, {
      sort: requestSortSpec(state.sort),
      cache: false,
      cacheKey: 'files-content:' + requested + ':first:' + JSON.stringify(state.sort),
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
    const data = response?.data || {};
    const canonical = String(data.path || requested);
    state.provider = 'host';
    state.searchQuery = '';
    state.hostPath = canonical;
    state.path = canonical;
    state.entries = sortEntries(data.entries || []);
    state.nextCursor = data.next_cursor || null;
    state.columns = [{ path: canonical, entries: state.entries, nextCursor: state.nextCursor, sort: normalizeSortSpec(state.sort), activeName: '' }];
    state.activeColumnIndex = 0;
    clearFilesSelection();
    const provider = state.shell?.querySelector('.files-provider-select');
    if (provider) provider.value = 'host';
    if (label) label.textContent = canonical;
    syncSearchControl();
    renderEntries();
    updateTreeHighlight();
    updateFavoriteButton();
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
    commitFilesHistory();
    return true;
  } catch (error) {
    if (error?.name === 'AbortError') return false;
    if (generation !== state.contentGeneration) return false;
    state.entries = [];
    state.nextCursor = null;
    if (body) body.replaceChildren(el('div', { class: 'files-error', text: error.message || 'Folder unavailable' }));
    setStatus(error.message || 'Folder unavailable', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function mapWithConcurrency(items, concurrency, mapper) {
  const values = new Array(items.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(concurrency, items.length) }, async () => {
    while (next < items.length) {
      const index = next;
      next += 1;
      values[index] = await mapper(items[index], index);
    }
  });
  await Promise.all(workers);
  return values;
}

async function searchRawHost(query) {
  const value = String(query || '').trim();
  if (!value) return openDirectory(state.hostPath || state.defaultPath);
  if (!state.hostPath) return false;
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const path = state.hostPath;
  setStatus(`Searching ${displayName(path)}…`);
  try {
    const response = await filesServiceClient.search(path, value, {
      maxResults: 100,
      maxEntries: 10_000,
      maxDepth: 32,
      signal: controller.signal,
    });
    const matches = (response?.data?.matches || []).map(String);
    const entries = await mapWithConcurrency(matches, 8, async (match) => {
      const metadata = await filesServiceClient.stat(match, {
        cache: false,
        cacheKey: `files-search-stat:${match}:${generation}`,
        signal: controller.signal,
      });
      const data = metadata?.data || {};
      const canonical = String(data.path || match);
      return {
        path: canonical,
        name: displayName(canonical),
        kind: data.kind || 'Other',
        size: data.size,
        modified_unix_ms: data.modified_unix_ms,
      };
    });
    if (generation !== state.contentGeneration || controller.signal.aborted || path !== state.hostPath) return false;
    state.searchQuery = value;
    state.entries = sortEntries(entries);
    state.nextCursor = null;
    state.columns = [{
      path,
      name: `Search: ${value}`,
      entries: state.entries,
      nextCursor: null,
      sort: normalizeSortSpec(state.sort),
      activeName: '',
      query: value,
    }];
    state.activeColumnIndex = 0;
    clearFilesSelection();
    syncSearchControl();
    const label = state.shell?.querySelector('[data-files-path]');
    if (label) label.textContent = `${path} · Search: ${value}`;
    renderEntries();
    setStatus(`${state.entries.length} result${state.entries.length === 1 ? '' : 's'}`);
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) {
      setStatus(error.message || 'Search failed', true);
    }
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function loadMoreEntries() {
  if (state.provider !== 'host' || !state.hostPath || !state.nextCursor) return;
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = state.contentGeneration;
  const path = state.hostPath;
  const cursor = state.nextCursor;
  setStatus('Loading more…');
  try {
    const response = await filesServiceClient.listDirectory(path, {
      cursor,
      sort: requestSortSpec(state.sort),
      cache: false,
      cacheKey: 'files-content:' + path + ':' + JSON.stringify(cursor) + ':' + JSON.stringify(state.sort),
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || path !== state.hostPath || !state.nativeWindow?.visible) return;
    const data = response?.data || {};
    const combined = new Map(state.entries.map((entry) => [`${entry.kind || ''}:${entry.name || ''}`, entry]));
    for (const entry of data.entries || []) combined.set(`${entry.kind || ''}:${entry.name || ''}`, entry);
    state.entries = sortEntries([...combined.values()]);
    state.nextCursor = data.next_cursor || null;
    renderEntries();
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
  } catch (error) {
    if (error?.code === 'stale_cursor' && generation === state.contentGeneration && path === state.hostPath) {
      return openDirectory(path);
    }
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) {
      setStatus(error.message || 'More items could not be loaded', true);
    }
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function managedSortRequest(spec = state.sort) {
  const normalized = normalizeSortSpec(spec);
  return {
    key: normalized.key,
    direction: normalized.direction,
    directories_first: normalized.directoriesFirst,
  };
}

function managedColumn(resource, response, spec = state.sort, query = '') {
  const sortKeys = sortKeysFor(response?.sort_keys || resource?.sort_keys || SORT_KEYS);
  const appliedSort = constrainSortSpec(response?.sort || spec, sortKeys);
  return {
    path: String(resource?.resource_ref || resource?.ref || resource?.id || resource?.name || 'managed'),
    resourceRef: String(resource?.resource_ref || resource?.ref || ''),
    resourceId: String(resource?.resource_id || resource?.id || ''),
    provider: String(resource?.provider || state.provider || ''),
    capabilities: Array.isArray(resource?.capabilities) ? [...resource.capabilities] : [],
    provenance: resource?.provenance && typeof resource.provenance === 'object' ? { ...resource.provenance } : {},
    revision: resource?.revision && typeof resource.revision === 'object' ? { ...resource.revision } : null,
    binderContext: resource?.binderContext || null,
    name: String(resource?.name || 'Files'),
    // The sealed cursor is bound to the provider's ordering. Never reorder an
    // individual page in the browser or page N can jump ahead of page N-1.
    entries: (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed),
    nextCursor: response?.next_cursor || null,
    sort: appliedSort,
    sortKeys,
    serverOrdered: true,
    activeName: '',
    query: String(query || ''),
    searchComplete: typeof response?.search_complete === 'boolean' ? response.search_complete : null,
  };
}

function stopActiveFolderWatch() {
  clearFilesReadRecovery('watch');
  state.watchEpoch += 1;
  state.watchController?.abort();
  state.watchController = null;
  state.watchResourceRef = '';
  if (state.watchRefreshTimer != null) clearTimeout(state.watchRefreshTimer);
  state.watchRefreshTimer = null;
}

function syncActiveFolderWatch() {
  const column = activeColumn();
  const resourceRef = String(column?.resourceRef || '');
  const watchable = state.provider === 'host'
    && resourceRef
    && !column?.query
    && Array.isArray(column?.capabilities)
    && column.capabilities.includes('watch')
    && !options.navigationOnly && !document.hidden && state.nativeWindow?.visible;
  if (state.watchRetryRef !== resourceRef) {
    state.watchRetryRef = resourceRef;
    state.watchFailures = 0;
  }
  if (!watchable) {
    stopActiveFolderWatch();
    return;
  }
  if (state.watchController && state.watchResourceRef === resourceRef) return;
  stopActiveFolderWatch();
  const controller = new AbortController();
  const epoch = ++state.watchEpoch;
  state.watchController = controller;
  state.watchResourceRef = resourceRef;
  void (async () => {
    try {
      for await (const event of filesFacadeClient.watch(resourceRef, { signal: controller.signal })) {
        if (controller.signal.aborted || epoch !== state.watchEpoch || activeColumn()?.resourceRef !== resourceRef) return;
        state.watchFailures = 0;
        // A successful watch observation is a dirty hint, never source authority.
        window.dispatchEvent(new CustomEvent('openclank-files-watch-dirty',{detail:{provider:'host',resourceRef,rescanRequired:event?.rescan_required===true}}));
        if (state.watchRefreshTimer != null) clearTimeout(state.watchRefreshTimer);
        state.watchRefreshTimer = setTimeout(() => {
          state.watchRefreshTimer = null;
          if (controller.signal.aborted || epoch !== state.watchEpoch || activeColumn()?.resourceRef !== resourceRef) return;
          const index = state.columns.length - 1;
          void reloadManagedColumn(index).then(() => {
            if (!event?.rescan_required) return;
            const resourceId = String(activeColumn()?.resourceId || '');
            if (resourceId && state.explorerTree?.getNode?.(resourceId)) {
              void state.explorerTree.refresh(resourceId, { recursive: false });
            }
          });
        }, 180);
      }
      if (!controller.signal.aborted && epoch === state.watchEpoch) {
        throw Object.assign(new Error('Files watch ended'), {code:'transport_unavailable'});
      }
    } catch (error) {
      if (error?.name !== 'AbortError' && epoch === state.watchEpoch && activeColumn()?.resourceRef === resourceRef) {
        state.watchFailures += 1;
        const snapshot = captureFilesProjection();
        showFilesReadRetry('watch','live folder updates',error,() => {
          if (error?.code === 'resource_ref_stale' || error?.code === 'policy_generation_changed') {
            return handleFilesPolicyChanged(snapshot);
          }
          // Reconcile the one directory that may have missed events. Committing
          // its listing opens the replacement watch without rebuilding all roots.
          return reloadManagedColumn(state.columns.indexOf(activeColumn()));
        });
      }
    } finally {
      if (state.watchController === controller) {
        state.watchController = null;
        state.watchResourceRef = '';
      }
    }
  })();
}

function commitManagedColumn(column, { provider = state.provider, columns = [column] } = {}) {
  state.provider = provider;
  state.columns = columns;
  state.activeColumnIndex = columns.length - 1;
  state.sort = constrainSortSpec(column.sort || state.sort, column.sortKeys || SORT_KEYS);
  if (provider === 'host' && column.resourceRef) state.hostPath = '';
  state.searchQuery = String(column.query || '');
  state.path = column.path;
  state.entries = column.entries;
  state.nextCursor = column.nextCursor;
  clearFilesSelection();
  const providerSelect = state.shell?.querySelector('.files-provider-select');
  if (providerSelect) providerSelect.value = provider;
  const label = state.shell?.querySelector('[data-files-path]');
  if (label) { label.replaceChildren(); for (const [index, parent] of state.columns.entries()) { if (index) label.append(document.createTextNode(' / ')); const crumb = el('button', { type:'button', class:'files-breadcrumb', text:parent.name || parent.path || 'Files' }); crumb.addEventListener('click', () => openManagedDirectory({ ...parent, resource_ref:parent.resourceRef, resource_id:parent.resourceId, kind:'folder' }, index - 1)); label.append(crumb); } }
  syncSearchControl();
  syncPrimarySortControls();
  renderEntries();
  updateTreeHighlight();
  updateFavoriteButton();
  const partialSearch = Boolean(column.query) && column.searchComplete === false;
  const searchNotice = state.shell?.querySelector('[data-files-search-notice]');
  if (searchNotice) searchNotice.hidden = !partialSearch;
  if (partialSearch) {
    setStatus('Partial search', true);
  } else {
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
  }
  if (!options.navigationOnly) syncActiveFolderWatch();
  commitFilesHistory();
  state.treeSelection = null;
  options.onSelection?.(null);
  options.onDirectory?.(column.resourceRef ? { ...column, resource_ref:column.resourceRef, resource_id:column.resourceId, kind:'folder' } : null);
}

function cancelReveal() {
  state.revealGeneration += 1;
  if (state.revealController) { state.revealController.abort(); state.navigationController?.abort(); }
  state.revealController = null;
}

async function openManagedDirectory(entry, parentIndex = state.columns.length - 1, navigation = {}) {
  clearFilesReadRecovery('folder');
  clearFilesReadRecovery('library');
  if (!editorResourceAllowed(entry)) throw new Error('Chats belong in Chats or Library, outside Editor.');
  if (navigation.reveal != null) { if (navigation.reveal !== state.revealGeneration) return false; }
  else cancelReveal();
  options.onSelection?.(null);
  const resourceRef = String(entry?.resource_ref || entry?.ref || '');
  if (!resourceRef || !isDirectory(entry)) return false;
  if (entry.provider && entry.provider !== state.provider) applyViewPreferences(entry.provider);
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const sortKeys = sortKeysFor(entry?.sort_keys || SORT_KEYS);
  const requestedSort = constrainSortSpec(state.sort, sortKeys);
  const intent = captureFilesProjection();
  const selected = {resourceId:String(entry.resource_id || entry.id || ''),provider:entry.provider || state.provider,
    name:entry.name,sort:requestedSort,sortKeys,query:''};
  intent.provider = selected.provider;
  intent.hostPath = '';
  const nestedTarget = Number.isInteger(parentIndex) && parentIndex >= 0;
  const ancestors = (entry.tree_ancestors || []).map(parent => ({
    resourceId:String(parent.resource_id || parent.id || ''),provider:parent.provider || selected.provider,
    name:parent.name,sort:parent.tree_sort || requestedSort,sortKeys:sortKeysFor(parent.sort_keys || SORT_KEYS),query:'',
  }));
  intent.columns = [...(nestedTarget ? intent.columns.slice(0,parentIndex+1) : ancestors),selected];
  setStatus(`Loading ${entry.name || 'folder'}…`);
  try {
    const nested = Number.isInteger(parentIndex) && parentIndex >= 0;
    let resource = entry;
    let parents = state.columns.slice(0, nested ? parentIndex + 1 : 0);
    const read = () => filesFacadeClient.children(destinationResourceRef(resource), {
      sort: managedSortRequest(requestedSort), query: '', signal: controller.signal,
    });
    let response;
    try { response = await read(); } catch (error) {
      if (!isExpiredFilesReference(error)) throw error;
      const renewed = await renewManagedDirectoryAuthority(entry, nested ? parentIndex : -1, controller.signal);
      resource = renewed.resource;
      parents = renewed.columns.slice(0, nested ? parentIndex + 1 : 0);
      response = await read();
    }
    if (generation !== state.contentGeneration || controller.signal.aborted || !state.nativeWindow?.visible) return false;
    if (nested && parents[parentIndex]) parents[parentIndex].activeName = resource.name;
    const column = managedColumn({ ...resource, binderContext:resource.binderContext || (nested ? parents[parentIndex]?.binderContext : null) }, response, requestedSort, '');
    const ancestors = !nested && Array.isArray(entry.tree_ancestors) ? entry.tree_ancestors.map(parent => managedColumn(parent,{entries:parent.tree_entries || [],next_cursor:parent.tree_cursor || null},parent.tree_sort || requestedSort)) : [];
    commitManagedColumn(column, {
      provider: resource?.provider || state.provider,
      columns: nested ? [...parents, column] : [...ancestors,column],
    });
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) {
      clearFilesAuthorityContent('',{catalogs:false});
      showFilesReadRetry('folder','the selected folder',error,()=>handleFilesPolicyChanged(intent));
    }
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function isExpiredFilesReference(error) {
  return error?.status === 409 && error?.code === 'resource_ref_stale'
    && error?.message === 'resource reference expired';
}

async function renewManagedDirectoryAuthority(target, index, signal = null) {
  const identity = entry => ({ id: String(entry?.resource_id || entry?.resourceId || ''), provider: String(entry?.provider || '') });
  const matches = (entry, wanted) => Boolean(wanted.id) && identity(entry).id === wanted.id && identity(entry).provider === wanted.provider;
  const owner = state.owner;
  const workspace = state.currentWorkspaceId;
  const policy = filesPolicyGeneration();
  const lifecycle = state.lifecycleGeneration;
  const content = state.contentGeneration;
  const columns = state.columns.slice(0, index + 1);
  const chain = columns.map(identity);
  const wanted = identity(target);
  if (!chain.length || !matches(columns.at(-1), wanted)) chain.push(wanted);
  const current = () => {
    if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
    if (state.owner !== owner || state.currentWorkspaceId !== workspace || filesPolicyGeneration() !== policy
      || state.lifecycleGeneration !== lifecycle || state.contentGeneration !== content) {
      throw new Error('The Files destination or access changed; choose it again.');
    }
  };
  await loadNavigationRoots({ force: true });
  current();
  // These are newly authenticated projections, never paths or decoded refs.
  const roots = [...state.managedRoots, ...state.navigationRoots, ...state.favorites];
  let resource;
  let anchor = -1;
  for (let cursor = chain.length - 1; cursor >= 0; cursor -= 1) {
    resource = roots.find(entry => matches(entry, chain[cursor]) && isDirectory(entry));
    if (resource) { anchor = cursor; break; }
  }
  if (anchor < 0) throw new Error('Choose this folder again from the authorized Files roots.');
  const refreshed = columns.slice();
  const remember = position => {
    if (position < refreshed.length) refreshed[position] = { ...refreshed[position],
      resourceRef: resource.resource_ref, path: resource.resource_ref,
      capabilities: [...resource.capabilities], revision: resource.revision || null,
      provenance: resource.provenance || {}, name: resource.name,
    };
  };
  remember(anchor);
  for (let position = anchor + 1; position < chain.length; position += 1) {
    let cursor = null;
    let child;
    const seen = new Set();
    for (let page = 0; page < 100; page += 1) {
      current();
      const response = await filesFacadeClient.children(resource.resource_ref, { limit: 200, cursor, signal });
      current();
      child = (response?.entries || []).map(facadeEntry).find(entry => matches(entry, chain[position]) && isDirectory(entry));
      if (child) break;
      cursor = response?.next_cursor || null;
      if (!cursor || seen.has(cursor)) break;
      seen.add(cursor);
    }
    if (!child) throw new Error('This folder is no longer available under the authorized Files roots.');
    resource = child;
    remember(position);
  }
  const checked = await filesFacadeClient.stat(resource.resource_ref, { signal });
  current();
  resource = facadeEntry(checked?.resource || checked);
  if (!matches(resource, wanted) || !resource.resource_ref || !isDirectory(resource)) throw new Error('The Files folder identity changed.');
  remember(chain.length - 1);
  return { resource, columns: refreshed };
}

async function reloadManagedColumn(index, { preserveColumns = false } = {}) {
  clearFilesReadRecovery('folder');
  const column = state.columns[index];
  if (!column) return false;
  if (!column.resourceRef) return openProvider('all');
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const requestedSort = constrainSortSpec(column.sort || state.sort, column.sortKeys || SORT_KEYS);
  column.sort = requestedSort;
  const intent = captureFilesProjection();
  intent.columns = intent.columns.slice(0,index+1);
  try {
    let resource = column;
    let columns = state.columns.slice(0, index);
    const read = () => filesFacadeClient.children(destinationResourceRef(resource), {
      sort: managedSortRequest(requestedSort), query: column.query || '', signal: controller.signal,
    });
    let response;
    try { response = await read(); } catch (error) {
      if (!isExpiredFilesReference(error)) throw error;
      const renewed = await renewManagedDirectoryAuthority(column, index, controller.signal);
      resource = renewed.resource;
      columns = renewed.columns.slice(0, index);
      response = await read();
    }
    if (generation !== state.contentGeneration || controller.signal.aborted || !state.nativeWindow?.visible) return false;
    const refreshed = managedColumn({
      resource_ref: destinationResourceRef(resource),
      resource_id: resource.resource_id || resource.resourceId,
      provider: resource.provider,
      name: resource.name,
      capabilities: resource.capabilities,
      provenance: resource.provenance,
      revision: resource.revision,
    }, response, requestedSort, column.query || '');
    if (preserveColumns && resource === column && state.mode === 'columns') {
      refreshed.activeName = column.activeName;
      refreshed.selection = column.selection;
      refreshed.scrollTop = column.scrollTop;
      state.columns[index] = refreshed;
      if (state.activeColumnIndex === index) {
        state.entries = refreshed.entries;
        state.nextCursor = refreshed.nextCursor;
        state.sort = refreshed.sort;
        syncPrimarySortControls();
        rememberViewPreferences();
      }
      renderEntries();
      commitFilesHistory();
    } else {
      commitManagedColumn(refreshed, { columns: [...columns, refreshed] });
    }
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration && state.nativeWindow?.visible) {
      clearFilesAuthorityContent('',{catalogs:false});
      showFilesReadRetry('folder','the selected folder',error,()=>handleFilesPolicyChanged(intent));
    }
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function loadManagedColumnMore(index) {
  const column = state.columns[index];
  if (!column?.resourceRef || !column.nextCursor) return false;
  if (state.pagingController) return false;
  const controller = new AbortController();
  state.contentController?.abort();
  state.contentController = controller;
  state.pagingController = controller;
  syncPagingControl();
  setStatus('Loading more items…');
  const generation = state.contentGeneration;
  const lifecycle = state.lifecycleGeneration;
  const cursor = column.nextCursor;
  const current = () => state.contentController === controller && state.pagingController === controller
    && !controller.signal.aborted && generation === state.contentGeneration && lifecycle === state.lifecycleGeneration
    && state.columns[index] === column && cursor === column.nextCursor;
  try {
    const response = await filesFacadeClient.children(column.resourceRef, {
      cursor,
      sort: managedSortRequest(column.sort || state.sort),
      query: column.query || '',
      signal: controller.signal,
    });
    if (!current()) return false;
    const combined = new Map(column.entries.map((item) => [item.resource_id, item]));
    for (const item of (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed)) combined.set(item.resource_id, item);
    column.entries = [...combined.values()];
    column.nextCursor = response?.next_cursor || null;
    if (index === state.columns.length - 1) {
      state.entries = column.entries;
      state.nextCursor = column.nextCursor;
    }
    renderEntries();
    setStatus(`${column.entries.length}${column.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
    return true;
  } catch (error) {
    if (!current()) return false;
    if (error?.code === 'stale_cursor') return reloadManagedColumn(index);
    if (error?.name !== 'AbortError') setStatus(error.message || 'More items could not be loaded', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
    if (state.pagingController === controller) state.pagingController = null;
    syncPagingControl();
  }
}

async function loadManagedMore() {
  return loadManagedColumnMore(state.columns.length - 1);
}

async function searchAllSources(query) {
  const value = String(query || '').trim();
  if (!value) return openProvider('all');
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  setStatus('Searching all sources…');
  try {
    const response = await filesFacadeClient.search(value, {
      limit: 100,
      sort: managedSortRequest(state.sort),
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || controller.signal.aborted || !state.nativeWindow?.visible) return false;
    const column = {
      path: 'all-search',
      resourceRef: '',
      name: 'All sources',
      entries: (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed),
      nextCursor: null,
      sort: constrainSortSpec(response?.sort || state.sort, response?.sort_keys || SORT_KEYS),
      sortKeys: sortKeysFor(response?.sort_keys || SORT_KEYS),
      serverOrdered: true,
      activeName: '',
      query: value,
      globalSearch: true,
    };
    commitManagedColumn(column, { provider: 'all', columns: [column] });
    setStatus(`${column.entries.length} result${column.entries.length === 1 ? '' : 's'}${response?.truncated ? ' · more results exist' : ''}`);
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) {
      setStatus(error.message || 'All-source search failed', true);
    }
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function searchActive(value) {
  cancelReveal();
  const query = String(value || '').trim();
  const column = activeColumn();
  if (state.provider === 'all' && (!column?.resourceRef || column.globalSearch)) {
    return searchAllSources(query);
  }
  if (column?.resourceRef) {
    column.query = query;
    state.searchQuery = query;
    syncSearchControl();
    return reloadManagedColumn(state.columns.length - 1);
  }
  if (state.provider === 'host' && state.hostPath) {
    setStatus('Refresh the authorized Files roots before searching this folder.', true);
    return false;
  }
  if (!query) return openProvider(state.provider);
  setStatus('Open a searchable source or folder first.', true);
  return false;
}

async function refreshBrowser({ hierarchyProvider = null } = {}) {
  const refreshLifecycle = state.lifecycleGeneration;
  const refreshContent = state.contentGeneration;
  // Explicit refresh also replaces expired navigation/Place projections with
  // newly authorized roots. Refreshing a virtual All sources column alone
  // leaves Home/Favorites tree refs from the previous projection untouched.
  try {
    await loadNavigationRoots({ force: true });
  } catch (error) {
    if (error?.name !== 'AbortError' && refreshLifecycle === state.lifecycleGeneration) setStatus(error.message || 'Files roots unavailable', true);
    return false;
  }
  if (refreshLifecycle !== state.lifecycleGeneration || refreshContent !== state.contentGeneration) return false;
  const selectedId = state.treeSelection?.resource_id;
  const directoryId = activeColumn()?.resourceId;
  const branches = [];
  const trees = state.editorRootBound ? [state.editorRootTree] : [state.workspaceTree, state.favoriteTree, state.explorerTree, state.collectionTree];
  for (const tree of trees) {
    if (!tree) continue;
    const nodes = tree.snapshot().nodes;
    if (hierarchyProvider) {
      const byId = new Map(nodes.map(node => [node.id, node]));
      const roots = nodes.filter(node => node.branch && node.data.provider === hierarchyProvider
        && byId.get(node.parentId)?.data.provider !== hierarchyProvider);
      for (const root of roots) branches.push({ tree, id: root.id, recursive: true });
      if (roots.length) continue;
    }
    const selected = nodes.find(node => node.data.resource_id === selectedId);
    const ids = new Set(nodes.filter(node => node.branch && node.data.resource_id === directoryId).map(node => node.id));
    if (selected?.parentId) ids.add(selected.parentId);
    if (selected?.branch) ids.add(selected.id);
    for (const id of ids) branches.push({ tree, id });
  }
  const lifecycle = state.lifecycleGeneration;
  const result = await reloadManagedColumn(state.columns.length - 1);
  if (lifecycle !== state.lifecycleGeneration) return false;
  for (const { tree, id, recursive = false } of branches) {
    if (lifecycle !== state.lifecycleGeneration) return false;
    await tree.refresh(id, { recursive });
  }
  return result;
}

async function managedUp() {
  if (state.searchQuery) return searchActive('');
  if (state.provider === 'host' && !usesFacadeListing()) {
    setStatus('Refresh the authorized Files roots before navigating this folder.', true);
    return false;
  }
  if (state.columns.length > 1) {
    const columns = state.columns.slice(0, -1);
    const column = columns.at(-1);
    commitManagedColumn(column, { columns });
    return true;
  }
  const column = activeColumn();
  if (!column?.resourceRef) return false;
  const generation = state.contentGeneration;
  const lifecycle = state.lifecycleGeneration;
  const controller = new AbortController();
  state.contentController?.abort();
  state.contentController = controller;
  try {
    const response = await filesFacadeClient.reveal(column.resourceRef, { signal: controller.signal });
    if (controller.signal.aborted || generation !== state.contentGeneration || lifecycle !== state.lifecycleGeneration) return false;
    const parent = facadeEntry(response?.parent);
    if (!parent.resource_ref || !isDirectory(parent) || parent.resource_id === column.resourceId) {
      setStatus('This folder has no available parent.');
      return false;
    }
    return await openManagedDirectory(parent, null);
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) setStatus(error.message || 'Parent folder unavailable', true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function libraryCollectionView(entry) {
  return String(entry?.provenance?.view || '').trim().toLowerCase();
}

function isPublishedLibraryEntry(entry) {
  return String(entry?.provenance?.domain || '').trim().toLowerCase() === 'published'
    || libraryCollectionView(entry) === LIBRARY_COLLECTION_VIEWS.published;
}

function clankerHomeEntry(entry) {
  const view = libraryCollectionView(entry);
  if (!view) return entry;
  const label = CLANKER_COLLECTION_LABELS[view] || entry.name;
  return {
    ...entry,
    name: label,
    provenance: { ...(entry.provenance || {}), clanker_home: true },
  };
}

async function openClankerHome(controller, generation) {
  const library = state.managedRoots.find((entry) => entry.provider === 'library' && isDirectory(entry));
  const sources = state.managedRoots.filter((entry) => (
    entry.provider === 'copal' || entry.provider === 'files' || entry.provider === 'host'
  ));
  let collections = [];
  if (library?.resource_ref) {
    const response = await filesFacadeClient.children(library.resource_ref, {
      sort: managedSortRequest(constrainSortSpec(state.sort, library.sort_keys || SORT_KEYS)),
      signal: controller.signal,
    }).catch(error => {
      if (error?.name === 'AbortError') throw error;
      return {error};
    });
    if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
    if (response.error) showFilesReadRetry('library','Library collections',response.error,()=>openProvider(CLANKER_HOME_PROVIDER));
    else clearFilesReadRecovery('library');
    collections = (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed)
      .filter((entry) => Object.values(LIBRARY_COLLECTION_VIEWS).includes(libraryCollectionView(entry)))
      .map(clankerHomeEntry);
  }
  const column = {
    path: CLANKER_HOME_PROVIDER,
    resourceRef: '',
    resourceId: '',
    name: 'Clanker home',
    entries: sortEntries([...sources, ...collections], constrainSortSpec(state.sort, ['name'])),
    nextCursor: null,
    sort: constrainSortSpec(state.sort, ['name']),
    sortKeys: ['name'],
    activeName: '',
    query: '',
    clankerHome: true,
  };
  commitManagedColumn(column, { provider: CLANKER_HOME_PROVIDER, columns: [column] });
  return true;
}

async function openProvider(provider, { rootsConfirmed = false } = {}) {
  clearFilesReadRecovery('folder');
  clearFilesReadRecovery('library');
  cancelReveal();
  state.treeSelection = null; options.onSelection?.(null); options.onDirectory?.(null);
  stopActiveFolderWatch();
  if (provider !== state.provider) closePreview();
  state.provider = provider;
  state.searchQuery = '';
  syncSearchControl();
  applyViewPreferences(provider);
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  state.path = provider;
  state.nextCursor = null;
  clearFilesSelection();
  const providerSelect = state.shell?.querySelector('.files-provider-select');
  if (providerSelect) providerSelect.value = provider;
  const body = state.shell?.querySelector('[data-files-body]');
  if (body) body.replaceChildren(el('div', { class: 'files-loading', text: 'Loading…' }));
  const label = state.shell?.querySelector('[data-files-path]');
  if (label) label.textContent = `${provider === 'all' ? 'All sources' : provider} · unified library`;
  updateTreeHighlight();
  updateFavoriteButton();
  try {
    if (!rootsConfirmed) {
      const roots = await filesFacadeClient.roots({
        copalWorkspace: copalWorkspace(),
        signal: controller.signal,
      });
      if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
      state.managedRoots = roots.entries.map(facadeEntry).filter(editorResourceAllowed);
    }
    if (provider === CLANKER_HOME_PROVIDER) return await openClankerHome(controller, generation);
    if (provider === 'all') {
      // Once Host is present in the opaque facade, it is the single unified
      // namespace entry. Keep raw compatibility anchors only as a measured
      // fallback for an older backend so All sources never shows Host twice.
      const hasOpaqueHost = state.managedRoots.some((item) => item.provider === 'host');
      const hostRoots = hasOpaqueHost
        ? []
        : state.navigationRoots.map((root) => ({ ...root, provider: 'host' }));
      const sortKeys = ['name'];
      const appliedSort = constrainSortSpec(state.sort, sortKeys);
      const entries = sortEntries([...hostRoots, ...state.managedRoots], appliedSort);
      const column = {
        path: 'all',
        resourceRef: '',
        name: 'All sources',
        entries,
        nextCursor: null,
        sort: appliedSort,
        sortKeys,
        activeName: '',
      };
      commitManagedColumn(column, { provider, columns: [column] });
      return true;
    }
    const root = state.managedRoots.find((item) => item.provider === provider);
    if (!root) throw new Error(`${provider} is unavailable`);
    const requestedSort = constrainSortSpec(state.sort, root.sort_keys || SORT_KEYS);
    const response = await filesFacadeClient.children(root.resource_ref, {
      sort: managedSortRequest(requestedSort),
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
    const column = managedColumn(root, response, requestedSort);
    if (provider === 'host') {
      const defaultAnchor = column.entries.find((entry) => entry.provenance?.favorite && isDirectory(entry));
      if (defaultAnchor) {
        column.activeName = defaultAnchor.name;
        const childResponse = await filesFacadeClient.children(defaultAnchor.resource_ref, {
          sort: managedSortRequest(constrainSortSpec(requestedSort, defaultAnchor.sort_keys || SORT_KEYS)),
          signal: controller.signal,
        });
        if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
        const child = managedColumn(defaultAnchor, childResponse, requestedSort);
        commitManagedColumn(child, { provider, columns: [column, child] });
        return true;
      }
    }
    commitManagedColumn(column, { provider, columns: [column] });
    return true;
  } catch (error) {
    if (error?.name === 'AbortError') return false;
    if (generation !== state.contentGeneration) return false;
    clearFilesAuthorityContent('', {catalogs:false});
    showFilesReadRetry('folder','this source',error,()=>openProvider(provider));
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function startBrowserDownload(url, filename) {
  const target = new URL(String(url || ''), window.location.href);
  if (target.origin !== window.location.origin) throw new Error('Download source is not part of this Open Clank session');
  // Let the authenticated browser download stack consume the response. The
  // Rust route supplies Content-Disposition, ranges, and backpressure; making
  // a Blob here would pull the entire file into renderer memory first.
  const link = document.createElement('a');
  link.href = target.href;
  link.download = displayName(filename || 'download');
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
}

async function downloadEntry(entry) {
  try {
    startBrowserDownload(entry.download_url, entry.name || 'download');
    setStatus(`Downloading ${entry.name}`);
  } catch (error) {
    setStatus(error.message || 'Download failed', true);
  }
}

async function download(path) {
  void path;
  setStatus('Refresh the authorized Files roots before downloading this file.', true);
}

async function downloadSelected() {
  const captured = captureCommands({ context:'selection' });
  const command = commandDescriptors(captured).find(item => item.id === 'files.download');
  if (!command || command.disabledReason || !isCommandCaptureCurrent(captured)) {
    setStatus(command?.disabledReason || 'Select one or more files first.', true);
    return false;
  }
  return command.run();
}

function selectedActionEntry() {
  const selected = state.entries.filter((entry) => state.selected.has(entrySelectionKey(entry)));
  return selected.length === 1 ? selected[0] : null;
}

function isHostInteropEntry(entry) {
  const provider = String(entry?.provider || (state.provider === 'host' ? 'host' : '')).toLowerCase();
  if (provider !== 'host') return false;
  if (isFacadeEntry(entry)) return entry.capabilities?.includes('stat') === true;
  return Boolean(entryPath(entry));
}

function isNativeHostOpenEntry(entry) {
  if (!isFacadeEntry(entry)) return false;
  const provider = String(entry?.provider || '').toLowerCase();
  const capabilities = new Set(entry?.capabilities || []);
  return (provider === 'host' || provider === 'files' && entry.kind === 'image' ||
      provider === 'copal' && entry.provenance?.document_kind === 'asset' ||
      provider === 'library' && entry.provenance?.domain === 'published')
    && capabilities.has('stat')
    && capabilities.has('open')
    && !isDirectory(entry)
    && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase());
}

function isLibraryDocumentsView(column = activeColumn()) {
  return String(column?.provider || '') === 'library'
    && libraryCollectionView(column) === LIBRARY_COLLECTION_VIEWS.documents;
}

function selectedLibraryDocumentEntries() {
  if (!isLibraryDocumentsView()) return [];
  return state.entries.filter((entry) => (
    state.selected.has(entrySelectionKey(entry)) && !isDirectory(entry) && Boolean(entry.resource_ref)
  ));
}

async function createSelectedLibraryDocument(snapshot) {
  if (options.pickerMode || !isLibraryDocumentsView(snapshot.column)) return false;
  const current = () => isCommandCaptureCurrent(snapshot) && snapshot.sessionId === (window.sessionModule?.getCurrentSessionId?.() || '');
  try {
    const actions = await import('./filesLibraryActions.js');
    if (!current()) return false;
    const created = await actions.createLibraryDocument({ sessionId:snapshot.sessionId || null });
    if (!current()) return false;
    if (created?.id) await window.documentModule?.loadDocument?.(created.id);
    if (!current()) return false;
    await reloadManagedColumn(snapshot.columnIndex);
    setStatus('Created Library document');
    return true;
  } catch (error) {
    if (current()) setStatus(error?.message || 'Library document could not be created', true);
    return false;
  }
}

function chooseLibraryDocuments(snapshot) {
  if (options.pickerMode || !isLibraryDocumentsView(snapshot.column) || !isCommandCaptureCurrent(snapshot)) return false;
  const input = state.shell?.querySelector('[data-files-library-input]');
  if (!input) return false;
  state.libraryImportCapture = snapshot;
  input.click();
  return true;
}

function entryActionChoices(entry) {
  if (entry?.unavailable || !entry?.resource_ref) return [];
  const capabilities = new Set(entry?.capabilities || []);
  const choices = [];
  if (!options.pickerMode && entry?.provider === 'host' && !isDirectory(entry)
      && !['symlink','special','other'].includes(String(entry?.kind || '').toLowerCase())
      && capabilities.has('download') && entry.resource_ref && entry.resource_id) {
    choices.push({action:'analysis.note', label:'Human analysis note…', glyph:'edit', local:true});
    choices.push({action:'analysis.result', label:'Save verified analysis result…', glyph:'edit', local:true});
    choices.push({action:'history.preview', label:'Preview selected history…', glyph:'file', local:true});
    if (/\.json$/i.test(String(entry?.name||''))) choices.push({action:'backup.preference', label:'Review backup preference…', glyph:'file', local:true});
  }
  if (!options.pickerMode && !isDirectory(entry) && entry?.resource_ref && entry?.resource_id && capabilities.has('download') && /\.png$/i.test(String(entry?.name||''))) choices.push({action:'media.spriteCrops',label:'Select sprite frames…',glyph:'image',local:true});
  if (!options.pickerMode && !isDirectory(entry) && /\.json$/i.test(String(entry?.name || '')) && capabilities.has('download')) {
    choices.push({action:'media.workflow', label:'Review media workflow…', glyph:'image', local:true});
  }
  if (!options.pickerMode && !isDirectory(entry) && /\.glb$/i.test(String(entry?.name || '')) && capabilities.has('download')) {
    choices.push({action:'media.materialBake', label:'Bake material maps…', glyph:'image', local:true});
    choices.push({action:'media.blender', label:'Review Blender render…', glyph:'image', local:true});
    choices.push({action:'media.blenderFour', label:'Review four-heading sheet…', glyph:'image', local:true});
    choices.push({action:'media.blenderRig', label:'Review joint pose clip…', glyph:'image', local:true});
  }
  if (isHostInteropEntry(entry) && isDirectory(entry) && capabilities.has('children') && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase())) {
    choices.push({ action: 'workspace.use', label: 'Use as workspace', glyph: 'workspace', local: true });
  }
  if (isHostInteropEntry(entry) && !isDirectory(entry) && isTextualEntry(entry) && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase())) {
    choices.push({ action: 'code.open', label: 'Open in Editor', glyph: 'code', local: true });
  }
  if (entry?.provider === 'host' && !isDirectory(entry) && entry.resource_ref && entry.resource_id && capabilities.has('download')) {
    choices.push({ action: 'history.checkpoint', label: 'Checkpoint selected files…', glyph: 'restore', local: true });
  }
  if (entry?.provider === 'host' && !isDirectory(entry) && String(entry.resource_id || '').trim()) {
    choices.push({ action: 'history.open', label: 'History', glyph: 'restore', local: true });
  }
  if (isNativeHostOpenEntry(entry)) {
    choices.push({ action: 'host.default', label: 'Open with default app', glyph: 'external', local: true });
    choices.push({ action: 'host.open', label: 'Open with installed app…', glyph: 'external', local: true });
  }
  if (capabilities.has('favorite')) {
    const favored = entry?.provenance?.favorite === true;
    choices.push({ action: 'favorite.set', value: !favored, label: favored ? 'Remove favorite' : 'Add favorite', glyph: favored ? 'star-filled' : 'star' });
  }
  if (capabilities.has('archive')) choices.push({ action: 'archive.set', value: true, label: 'Archive', glyph: 'archive' });
  if (capabilities.has('restore') && !['copal', 'host'].includes(entry.provider)) choices.push({ action: 'archive.set', value: false, label: 'Restore', glyph: 'restore' });
  if (['copal', 'host', 'files'].includes(entry.provider) && capabilities.has('rename')) choices.push({ action: 'rename', label: 'Rename', glyph: 'edit' });
  const transfer = transferCapabilities(entry);
  if (entry?.provider === 'host' && !isDirectory(entry) && transfer.copy) choices.push({ action:'transfer.duplicate', label:'Duplicate file', glyph:'copy' });
  if (entry?.provenance?.domain === 'copal' && transfer.move) choices.push({ action: 'transfer.move', label: 'Move', glyph: 'folder' });
  if (entry?.provenance?.domain === 'copal' && transfer.copy) choices.push({ action: 'transfer.copy', label: 'Copy', glyph: 'copy' });
  if ((entry?.provenance?.domain === 'copal' || entry?.provider === 'host') && capabilities.has('trash')) choices.push({ action: 'trash', label: 'Move to trash', glyph: 'trash' });
  if (entry.provenance?.domain === 'copal' && capabilities.has('restore')) choices.push({ action: 'restore', label: 'Restore', glyph: 'restore' });
  if (entry.provider === 'library' && entry.kind === 'document' && entry.provenance?.domain === 'documents' && capabilities.has('open')) {
    choices.push({ action: 'library.clone', label: 'Clone to current session', glyph: 'copy', local: true });
    choices.push({ action: 'library.delete', label: 'Delete', glyph: 'trash', local: true, danger: true });
  }
  return choices;
}

function chooseHostApplication(applications) {
  return new Promise((resolve) => {
    const overlay = el('div', { class: 'modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'Open with installed app' });
    const panel = el('div', { class: 'modal-content styled-confirm-box' });
    const title = el('h4', { text: 'Open with installed app' });
    const message = el('p', { text: 'Choose an installed application for this file.' });
    const list = el('div', { class: 'files-action-menu', role: 'listbox', 'aria-label': 'Installed applications' });
    const cancel = el('button', { type: 'button', class: 'confirm-btn confirm-btn-secondary', text: 'Cancel' });
    const finish = (value) => { overlay.remove(); document.removeEventListener('keydown', onKey); resolve(value); };
    const onKey = (event) => { if (event.key === 'Escape') { event.preventDefault(); finish(null); } };
    for (const application of applications) {
      const item = el('button', { type: 'button', class: 'files-action-menu-item', role: 'option', text: application.name || application.id });
      item.addEventListener('click', () => finish(application));
      list.append(item);
    }
    cancel.addEventListener('click', () => finish(null));
    panel.append(title, message, list, cancel);
    overlay.append(panel);
    overlay.addEventListener('click', (event) => { if (event.target === overlay) finish(null); });
    listen(document, 'keydown', onKey);
    document.body.append(overlay);
    list.querySelector('button')?.focus();
  });
}

function updateManagedActionControl() {
  updateLibraryCommandControls();
  const button = state.shell?.querySelector('[data-files-actions]');
  if (!button) return;
  const captured = captureCommands({ context:'selection' });
  const count = captured.entries.length;
  const available = !options.pickerMode && count > 0 && contextCommandDescriptors(captured).length > 0;
  button.disabled = !available;
  setActionButtonLabel(button, count > 1 ? count : 0);
  button.title = available
    ? count === 1 ? `Actions for ${captured.entries[0].name}` : `Actions available for all ${count} selected items`
    : 'No action is available for the selection';
  button.setAttribute('aria-label', button.title);
}

function setActionButtonLabel(button, count) {
  const label = button.querySelector('.files-actions-label');
  if (!label) return;
  label.textContent = count ? `Actions (${count})` : 'Actions';
}

function commonActionChoices(entries) {
  if (!entries.length) return [];
  const batchable = new Set(['archive.set', 'favorite.set', 'transfer.move', 'transfer.copy', 'transfer.duplicate',
    'history.checkpoint', 'library.clone', 'library.delete']);
  let common = entryActionChoices(entries[0]).filter(choice => batchable.has(choice.action));
  for (const entry of entries.slice(1)) {
    if (!common.length) break;
    const choices = entryActionChoices(entry);
    common = common.filter(choice => choices.some(candidate => candidate.action === choice.action && candidate.value === choice.value));
  }
  return common;
}

function captureFilesSelectionGuard(model, destinationIndex = state.activeColumnIndex) {
  if (!model) return null;
  const columnIndex = Number.isInteger(destinationIndex) && destinationIndex >= 0
    ? destinationIndex : Math.max(0, state.columns.length - 1);
  const column = state.columns[columnIndex] || activeColumn();
  return Object.freeze({
    filesScope: model.snapshot().scopeKey,
    filesEpoch: model.epoch,
    filesGeneration: filesPolicyGeneration(),
    filesOwner: String(state.owner),
    filesWorkspace: String(state.currentWorkspaceId || copalWorkspace()),
    filesPane: state.nativeWindow?.id || 'files-window',
    selectedKeys: Object.freeze(model.selectedKeys()),
    filesDestinationIndex: columnIndex,
    filesDestinationRef: String(destinationResourceRef(column) || ''),
  });
}

function validateFilesSelectionGuard(guard, { targetKey = '' } = {}) {
  if (!guard?.filesScope) return null;
  const model = state.selectionModels.get(String(guard.filesScope));
  if (!model) return null;
  const currentKeys = model.selectedKeys();
  const capturedKeys = Array.isArray(guard.selectedKeys) ? guard.selectedKeys.map(String) : [];
  const exactSelection = currentKeys.length === capturedKeys.length && currentKeys.every((key, index) => key === capturedKeys[index]);
  if (model.snapshot().scopeKey !== guard.filesScope || model.epoch !== Number(guard.filesEpoch)
    || filesPolicyGeneration() !== Number(guard.filesGeneration) || state.owner !== guard.filesOwner
    || String(state.currentWorkspaceId || copalWorkspace()) !== String(guard.filesWorkspace)
    || (state.nativeWindow?.id || 'files-window') !== guard.filesPane || !exactSelection
    || (targetKey && !currentKeys.includes(targetKey))) return null;
  return model;
}

function updateLibraryCommandControls() {
  const enabled = !options.pickerMode && isLibraryDocumentsView();
  for (const selector of ['[data-files-library-create]', '[data-files-library-import]', '[data-files-library-select]']) {
    const control = state.shell?.querySelector(selector);
    if (control) control.hidden = !enabled;
  }
  const selected = selectedLibraryDocumentEntries();
  const select = state.shell?.querySelector('[data-files-library-select]');
  const newButton = state.shell?.querySelector('[data-files-new]');
  if (newButton) newButton.hidden = options.pickerMode || enabled;
  if (select) {
    select.textContent = selected.length ? 'Clear' : 'Select';
    select.title = selected.length ? 'Clear Library document selection' : 'Select all Library documents';
  }
}

async function performManagedAction(entry, choice, snapshot = null) {
  if (options.pickerMode) { setStatus('This dialog selects resources; file changes are unavailable.', true); return false; }
  const menuTrigger = document.activeElement;
  if (!choice) return false;
  let ownedController=null;
  const current=()=>!snapshot||isCommandCaptureCurrent(snapshot,snapshot.policy,ownedController);
  if(!current())return false;
  if (choice.action === 'backup.preference') {
    const owner=state.owner,generation=filesPolicyGeneration(),chatID=String(window.sessionModule?.getCurrentSessionId?.()||'');
    const stillCurrent=()=>current() && state.owner===owner && filesPolicyGeneration()===generation && String(window.sessionModule?.getCurrentSessionId?.()||'')===chatID;
    try {const {runBackupPreferenceTransfer}=await import('./backupPreferenceTransfer.js');await runBackupPreferenceTransfer({client:filesFacadeClient,sourceRef:entry.resource_ref,sourceResourceID:entry.resource_id,current:stillCurrent,scopeRoot:state.nativeWindow?.root,status:(message,error=false)=>setStatus(message,error)});return true;}
    catch(error){if(stillCurrent())setStatus(String(error?.message||'Backup preference unavailable.'),true);return false;}
  }
  if (choice.action === 'history.preview') {
    const owner=state.owner,generation=filesPolicyGeneration(),chatID=String(window.sessionModule?.getCurrentSessionId?.()||'');
    const stillCurrent=()=>current() && state.owner===owner && filesPolicyGeneration()===generation && String(window.sessionModule?.getCurrentSessionId?.()||'')===chatID;
    try {const {runSessionTransferPreview}=await import('./sessionTransfer.js');await runSessionTransferPreview({client:filesFacadeClient,sourceRef:entry.resource_ref,sourceResourceID:entry.resource_id,current:stillCurrent,scopeRoot:state.nativeWindow?.root,status:(message,error=false)=>setStatus(message,error)});return true;}
    catch(error){if(stillCurrent())setStatus(String(error?.message||'Selected history unavailable.'),true);return false;}
  }
  if (choice.action === 'analysis.result') {
    const owner=state.owner,generation=filesPolicyGeneration(),chatID=String(window.sessionModule?.getCurrentSessionId?.()||'');
    if(!chatID){setStatus('Open the original chat before selecting an analysis result.',true);return false;}
    const stillCurrent=()=>current() && state.owner===owner && filesPolicyGeneration()===generation && String(window.sessionModule?.getCurrentSessionId?.()||'')===chatID;
    try {const {runAnalysisResultFlow}=await import('./analysisNoteFlow.js');await runAnalysisResultFlow({client:filesFacadeClient,sourceRef:entry.resource_ref,sourceResourceID:entry.resource_id,chatID,current:stillCurrent,scopeRoot:state.nativeWindow?.root,status:(message,error=false)=>setStatus(message,error),openResult:async ref=>{const result=await filesFacadeClient.open(ref);if(stillCurrent())await dispatchFilesDestination(result,{resourceRef:ref,surface:options.surface||'files',isCurrent:stillCurrent});}});return true;}
    catch(error){if(stillCurrent())setStatus(String(error?.message||'Analysis result unavailable.'),true);return false;}
  }
  if (choice.action === 'analysis.note') {
    const owner=state.owner, generation=filesPolicyGeneration();
    const stillCurrent=()=>current() && state.owner===owner && filesPolicyGeneration()===generation;
    try {
      const {runAnalysisNoteFlow}=await import('./analysisNoteFlow.js');
      await runAnalysisNoteFlow({client:filesFacadeClient,sourceRef:entry.resource_ref,sourceResourceID:entry.resource_id,
        current:stillCurrent,scopeRoot:state.nativeWindow?.root,status:(message,error=false)=>setStatus(message,error),
        openResult:async ref=>{const result=await filesFacadeClient.open(ref);if(stillCurrent())await dispatchFilesDestination(result,{resourceRef:ref,surface:options.surface||'files',isCurrent:stillCurrent});}});
      return true;
    } catch(error) {if(stillCurrent())setStatus(String(error?.message||'Analysis note unavailable.'),true);return false;}
  }
  if (choice.action === 'media.jobs') {
    const owner = state.owner, generation = filesPolicyGeneration();
    const stillCurrent = () => current() && state.owner === owner && filesPolicyGeneration() === generation;
    try {
      const {showMediaJobs} = await import('./mediaJobFlow.js');
      const workspace = copalWorkspace();
      const chooseDestination = async () => {
        const {createResourcePicker} = await import('./copal/resourcePicker.js');
        if (!stillCurrent()) return null;
        return await new Promise(resolve => {
          let selected = null;
          const picker = createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,
            getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:stillCurrent,rootLabel:'Choose a destination for the retained output',
            isCompatible:resource=>resource.capabilities?.write ? '' : 'Choose a writable folder.',
            onSelect:resource=>{selected=resource.ref; return true;},onClose:cancelled=>resolve(cancelled ? null : selected)});
          void picker.open();
        });
      };
      const openResult = async (ref, {recordingJobHint = null, savedImageDigest = null} = {}) => {
        if (savedImageDigest) {
          const stat=await filesFacadeClient.stat(ref), resource=stat?.resource || stat;
          const revision=resource?.revision;
          const fingerprint=revision?.kind === 'hostFingerprint' && /^sha256:([a-f0-9]{64}):[1-9][0-9]*$/.exec(revision.value || '');
          if (!stillCurrent() || resource?.provider !== 'host' || resource.kind !== 'file' ||
              !resource.ref || !fingerprint || fingerprint[1] !== savedImageDigest) throw new Error('The original saved image revision changed.');
          const editor=window.copalModule || await import('./copal.js');
          if (!stillCurrent() || typeof editor.openRetainedHostImage !== 'function' ||
              !await editor.openRetainedHostImage(resource.ref,{expectedRevision:revision,isCurrent:stillCurrent})) {
            throw new Error('The exact saved image could not be opened.');
          }
          return;
        }
        let originalRevision = null, recordingEvidence = null;
        if (recordingJobHint) {
          const original = await filesFacadeClient.openRecordedMedia(recordingJobHint);
          if (!stillCurrent() || typeof original?.resourceRef !== 'string' || !original.revision) throw new Error('Original recording is unavailable. Choose the file in Files.');
          ref = original.resourceRef; originalRevision = original.revision;
          if (original.recordingCoverage) recordingEvidence={resourceRef:ref,revision:originalRevision,coverage:original.recordingCoverage};
        }
        const response = await filesFacadeClient.open(ref);
        if (originalRevision && (response?.resource?.revision?.kind !== originalRevision.kind || response?.resource?.revision?.value !== originalRevision.value)) throw new Error('Original recording changed. Choose the file in Files.');
        if (stillCurrent()) await dispatchFilesDestination(response, {resourceRef:ref,surface:options.surface || 'files',isCurrent:stillCurrent,recordingJobHint,recordingEvidence});
      };
      await showMediaJobs({client:filesFacadeClient,ui:uiModule,current:stillCurrent,chooseDestination,openResult,status:(message,error=false)=>setStatus(message,error)});
      return true;
    } catch (error) { if (stillCurrent()) setStatus(String(error?.message || 'Media jobs unavailable.'), true); return false; }
  }
  if (choice.action === 'media.record' || choice.action === 'media.workflow' || choice.action === 'media.materialBake' || choice.action === 'media.blender' || choice.action === 'media.blenderFour' || choice.action === 'media.blenderRig' || choice.action === 'media.generate' || choice.action === 'media.materialPack' || choice.action === 'media.spriteSheet' || choice.action === 'media.spriteCrops') {
    const sessionID = String(window.sessionModule?.getCurrentSessionId?.() || '');
    if (!sessionID) { setStatus('Open a chat before reviewing a media workflow.', true); return false; }
    const owner = state.owner, workspace = copalWorkspace(), generation = filesPolicyGeneration();
    const stillCurrent = () => current() && state.owner === owner && copalWorkspace() === workspace
      && filesPolicyGeneration() === generation && String(window.sessionModule?.getCurrentSessionId?.() || '') === sessionID;
    try {
      const {runMediaJobFlow, runNeutralMediaFlow, runMaterialPackFlow, runBlenderJobFlow, runSpriteSheetFlow, runSpriteCropsFlow, runRecordingFlow} = await import('./mediaJobFlow.js');
      const filesWorkspace = state.currentWorkspaceId;
      const captureCurrent = () => stillCurrent() && (choice.action !== 'media.record' || state.currentWorkspaceId === filesWorkspace);
      const chooseDestination = async () => {
        const {createResourcePicker} = await import('./copal/resourcePicker.js');
        if (!captureCurrent()) return null;
        let initialDirectory = null;
        if (choice.action === 'media.record' && filesWorkspace) {
          const suggested = await filesFacadeClient.workspaceCaptureDirectory(filesWorkspace, 'movs');
          if (!captureCurrent()) return null;
          initialDirectory = suggested?.resource || null;
        }
        return await new Promise(resolve => {
          let selected = null;
          const picker = createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,
            getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:captureCurrent,initialDirectory,rootLabel:'Choose a destination for the output',
            isCompatible:resource=>resource.capabilities?.write ? '' : 'Choose a writable folder.',
            onSelect:resource=>{selected=resource.ref; return true;},onClose:cancelled=>resolve(cancelled ? null : selected)});
          void picker.open();
        });
      };
      const args={client:filesFacadeClient,ui:uiModule,sessionID,current:choice.action === 'media.record' ? captureCurrent : stillCurrent,chooseDestination,
        status:(message,error=false)=>setStatus(message,error)};
      if (choice.action === 'media.record') await runRecordingFlow(args);
      else if (choice.action === 'media.spriteSheet') {
        const chooseFrame=async index=>{
          const {createResourcePicker}=await import('./copal/resourcePicker.js');
          return await new Promise(resolve=>{let selected=null; const picker=createResourcePicker({client:filesFacadeClient,purpose:'image',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:stillCurrent,
            rootLabel:`Choose PNG frame ${index}`,isCompatible:resource=>resource.capabilities?.download && /\.png$/i.test(String(resource.name||''))?'':'Choose a readable PNG.',
            onSelect:resource=>{selected={ref:resource.ref,name:resource.name};return true;},onClose:cancelled=>resolve(cancelled?null:selected)}); void picker.open();});
        };
        await runSpriteSheetFlow({...args,chooseFrame});
      }
      else if (choice.action === 'media.spriteCrops') await runSpriteCropsFlow({...args,sourceRef:entry.resource_ref,sourceID:entry.resource_id,sourceName:entry.name});
      else if (choice.action === 'media.materialBake') await runBlenderJobFlow({...args,sourceRef:entry.resource_ref,materialBake:true});
      else if (choice.action === 'media.blender') await runBlenderJobFlow({...args,sourceRef:entry.resource_ref});
      else if (choice.action === 'media.blenderFour') await runBlenderJobFlow({...args,sourceRef:entry.resource_ref,fourHeadings:true});
      else if (choice.action === 'media.blenderRig') await runBlenderJobFlow({...args,sourceRef:entry.resource_ref,rigClip:true});
      else if (choice.action === 'media.materialPack') {
        const chooseMap=async role=>{
          const {createResourcePicker}=await import('./copal/resourcePicker.js');
          if(!stillCurrent())return null;
          return await new Promise(resolve=>{let selected=null;const picker=createResourcePicker({client:filesFacadeClient,purpose:'image',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:stillCurrent,
            rootLabel:`Choose the ${role} PNG`,isCompatible:resource=>resource.capabilities?.download && resource.resourceId && resource.revision?'':'Choose a readable image with a current revision.',
            onSelect:resource=>{selected={ref:resource.ref,resourceID:resource.resourceId,revision:resource.revision};return true;},onClose:cancelled=>resolve(cancelled?null:selected)});void picker.open();});
        };
        await runMaterialPackFlow({...args,chooseMap});
      }
      else if (choice.action === 'media.generate') {
        const chooseRecipe=async()=>{
          const {createResourcePicker}=await import('./copal/resourcePicker.js');
          return await new Promise(resolve=>{let selected=null;const picker=createResourcePicker({client:filesFacadeClient,purpose:'file',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:stillCurrent,
            rootLabel:'Choose the workflow input for this profile',isCompatible:resource=>resource.capabilities?.download && /\.json$/i.test(String(resource.name||''))?'':'Choose a readable JSON workflow.',
            onSelect:resource=>{selected=resource.ref;return true;},onClose:cancelled=>resolve(cancelled?null:selected)});void picker.open();});
        };
        const chooseImage=async()=>{
          const {createResourcePicker}=await import('./copal/resourcePicker.js');
          return await new Promise(resolve=>{let selected=null;const picker=createResourcePicker({client:filesFacadeClient,purpose:'image',accountScope:owner,workspaceScope:workspace,
            generation,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,getWorkspaceScope:()=>copalWorkspace(),isOriginCurrent:stillCurrent,
            rootLabel:'Choose a source image',isCompatible:resource=>resource.capabilities?.download && resource.resourceId && resource.revision?'':'Choose a readable image with a current revision.',
            onSelect:resource=>{selected={ref:resource.ref,resourceID:resource.resourceId,revision:resource.revision};return true;},onClose:cancelled=>resolve(cancelled?null:selected)});void picker.open();});
        };
        await runNeutralMediaFlow({...args,chooseRecipe,chooseImage});
      }
      else await runMediaJobFlow({...args,recipeRef:entry.resource_ref});
      return true;
    } catch (error) { if (stillCurrent()) setStatus(String(error?.message || 'Media workflow unavailable.'), true); return false; }
  }
  if (choice.action === 'history.checkpoint') return await openSelectedCheckpoint([entry], current, snapshot?.selection);
  if (choice.action === 'history.open') {
    const target = createFilesHistoryTarget(entry);
    if (!target) {
      setStatus('History is available only for a registered Host file.', true);
      return false;
    }
    const trigger = menuTrigger?.isConnected
      ? menuTrigger
      : state.shell?.querySelector('[data-files-actions]') || null;
    const opened = openResourceHistory(target, trigger);
    if (!opened) setStatus('History could not open for this resource.', true);
    return opened;
  }
  if (choice.action === 'library.clone' || choice.action === 'library.delete') {
    return performLibrarySelectionAction([entry], choice, snapshot);
  }
  if (choice.action === 'transfer.duplicate') {
    if (!snapshot?.selection) { setStatus('Select the file in its containing folder to duplicate it.', true); return false; }
    return performSelectedBulkAction([entry], choice, snapshot);
  }
  if (choice.action === 'transfer.move' || choice.action === 'transfer.copy') {
    const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
    const kind = choice.action === 'transfer.copy' ? 'copy' : 'move';
    return executeSelectedFilesTransfer(kind, { model, destination: activeColumn(), destinationIndex: state.activeColumnIndex });
  }
  const controller = new AbortController();
  ownedController=controller;
  state.contentController?.abort();
  state.contentController = controller;
  const generation = state.contentGeneration;
  const actionOwner = state.owner, actionPolicy = filesPolicyGeneration(), actionRef = String(entry.resource_ref || ''), actionLifecycle = state.lifecycleGeneration;
  let actionRevision = entry.revision ? {kind:entry.revision.kind, value:entry.revision.value} : null;
  const actionCurrent = () => current() && !controller.signal.aborted && generation === state.contentGeneration
    && actionOwner === state.owner && actionPolicy === filesPolicyGeneration() && actionRef === String(entry.resource_ref || '');
  setStatus(`${choice.label}…`);
  try {
    if (choice.action === 'code.open') {
      const code = window.codeEditorModule || await import('./codeEditor.js');
      if (!actionCurrent()) return false;
      const opened = isFacadeEntry(entry)
        ? await code.openResource(entry.resource_ref, { isCurrent:actionCurrent })
        : await code.openPath(entryPath(entry), { directory: isDirectory(entry) });
      if (!opened) throw new Error('Editor did not open this resource');
      setStatus(`Opened ${entry.name} in Editor`);
      return true;
    }
    if (choice.action === 'workspace.use') {
      const capabilities = new Set(entry.capabilities || []);
      if (!isDirectory(entry) || !capabilities.has('children') || !capabilities.has('stat')) throw new Error('Only an authorized folder can be used as a workspace');
      const workspace = await import('./workspace.js');
      if (!actionCurrent()) return false;
      let bound;
      if (isFacadeEntry(entry)) {
        const response = await filesFacadeClient.workspace(
          entry.resource_ref,
          'agent_workspace',
          { signal: controller.signal },
        );
        bound = await workspace.resolveWorkspaceId(
          response?.workspace?.id,
          'agent_workspace',
        );
      } else {
        bound = await workspace.bindWorkspacePath(entryPath(entry), 'agent_workspace');
      }
      if (controller.signal.aborted || generation !== state.contentGeneration || !current()) return false;
      await workspace.setWorkspace(bound.path, bound.id);
      setStatus(`Workspace: ${entry.name}`);
      return true;
    }
    if (choice.action === 'host.default' || choice.action === 'host.open') {
      const discovered = await filesFacadeClient.hostApps(entry.resource_ref, { signal: controller.signal });
      if (!actionCurrent()) return false;
      const applications = Array.isArray(discovered?.applications) ? discovered.applications : [];
      if (!applications.length) throw new Error('No installed app can open this file.');
      const application = choice.action === 'host.default'
        ? applications.find((candidate) => candidate.default)
        : await chooseHostApplication(applications);
      if (!application && choice.action === 'host.open' || !current()) return false;
      if (!application) throw new Error('No default application is registered for this file. Choose an installed app instead.');
      await filesFacadeClient.openHost(entry.resource_ref, application.id, { signal: controller.signal });
      setStatus(`Opened ${entry.name} with ${application.name}`);
      return true;
    }
    if (!entry.resource_ref) return false;
    let actionArgs = {};
    if (choice.action === 'rename' || choice.action === 'move') {
      const itemKind = isDirectory(entry) ? 'folder' : 'file';
      const name = await uiModule.styledPrompt(choice.action === 'rename' ? `Choose a new ${itemKind} name.` : 'Choose the destination name for this document.', {
        title: choice.action === 'rename' ? `Rename ${itemKind}` : 'Move document', defaultValue: entry.provenance?.logical_path || entry.name,
      });
      if (name == null) return false;
      if (!String(name).trim()) throw new Error('A name is required.');
      actionArgs = { name: String(name).trim() };
    } else if (choice.action === 'trash' && !await uiModule.styledConfirm(`Move ${entry.name} to trash?`, { title: 'Move to trash', confirmText: 'Move to trash', danger: true })) return false;
    if(!actionCurrent())return false;
    let managedRoot = null;
    if (entry.provider === 'host' && choice.action === 'rename') {
      if (!actionRevision) {
        const current = await filesFacadeClient.stat(actionRef, {signal:controller.signal});
        if (!actionCurrent()) return false;
        const revision = current?.resource?.revision;
        if (!revision) throw new Error('Refresh this item before renaming it.');
        actionRevision = {kind:revision.kind, value:revision.value};
      }
      const requirement = await filesFacadeClient.actionRootRequirement({generation:actionPolicy, resourceRef:actionRef,
        name:actionArgs.name, expectedRevision:actionRevision}, {signal:controller.signal});
      if (!actionCurrent()) return false;
      if (requirement.root_required) {
        if (!actionRevision) throw new Error('Refresh this file before renaming it.');
        const {createResourcePicker} = await import('./copal/resourcePicker.js');
        if (!actionCurrent()) return false;
        managedRoot = await new Promise(resolve => {
          let selected = null;
          state.managedRootPicker?.destroy();
          const picker = createResourcePicker({client:filesFacadeClient, purpose:'folder', accountScope:actionOwner,
            workspaceScope:copalWorkspace(), generation:actionPolicy, originWindow:state.nativeWindow,
            getGeneration:()=>filesPolicyGeneration(), getAccountScope:()=>state.owner, getWorkspaceScope:()=>copalWorkspace(),
            isOriginCurrent:actionCurrent, rootLabel:'Choose an authorized root for this rename',
            isCompatible:resource=>resource.provider === 'host' && resource.capabilities.write ? '' : 'Choose a writable Host root.',
            onSelect:resource=>{selected=resource; return true;},
            onClose:cancelled=>{if(state.managedRootPicker === picker) state.managedRootPicker=null; resolve(cancelled ? null : selected);}});
          state.managedRootPicker=picker; void picker.open();
        });
        if (!managedRoot || !actionCurrent()) return false;
      }
    }
    const actionId = `files-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    const response = await filesFacadeClient.action(
      entry.resource_ref,
      choice.action,
      choice.action === 'favorite.set' || choice.action === 'archive.set' ? { value: choice.value } : actionArgs,
      { signal: controller.signal, actionId, ...(entry.provider === 'host' && choice.action === 'rename' ? {expectedRevision:actionRevision, managedRoot} : {}) },
    );
    if (controller.signal.aborted || generation !== state.contentGeneration) return false;
    clearFilesSelection();
    if (activeColumn()?.globalSearch) await searchAllSources(state.searchQuery);
    else if (usesFacadeListing()) {
      // A logical-path rename can remove the last leaf of the current virtual
      // folder. Reauthorize the new ancestry before refreshing retained trees.
      if (entry.provider === 'copal' && ['rename', 'move'].includes(choice.action) && response?.resource?.ref) {
        await revealResource(response.resource.ref);
      }
      await refreshBrowser({ hierarchyProvider: entry.provider === 'copal' ? 'copal' : null });
    }
    window.dispatchEvent(new CustomEvent('openclank-files-resource-mutated', { detail: { resourceRef: entry.resource_ref, action: choice.action, resource: response?.resource || null, history: response?.history || null } }));
    const recoveryUnavailable = response?.history && response.history.status !== "complete" && response.history.status !== "unconfigured";
    const hostTrashEntry = entry.provider === 'host' && choice.action === 'trash' ? response?.history?.receipt?.trash_entry : null;
    setStatus(hostTrashEntry ? 'Moved to trash.' : recoveryUnavailable ? `Saved; recovery copy unavailable: ${entry.name}` : `${choice.label}: ${entry.name}`);
    if (hostTrashEntry) {
      // One-shot Undo for this trash only: the entry lives in this closure, never in storage, and goes with the status line.
      const undoOwner = state.owner;
      const undo = el('button', { type: 'button', text: 'Undo', title: 'Put the file back where it was' });
      undo.addEventListener('click', async () => {
        if (listeners.signal.aborted || state.owner !== undoOwner) return;
        undo.disabled = true;
        state.pendingUndo = null;
        try {
          await filesFacadeClient.action(response.resource?.ref || entry.resource_ref, 'restore', { entry: hostTrashEntry },
            { signal: listeners.signal, actionId: operationId('files-untrash') });
          if (listeners.signal.aborted || state.owner !== undoOwner) return;
          if (usesFacadeListing()) await refreshBrowser({ hierarchyProvider: null });
          setStatus('Restored.');
        } catch (error) {
          if (error?.name !== 'AbortError' && state.owner === undoOwner) setStatus(error?.code === 'mutation_outcome_unknown' ? 'The outcome is not known. Refresh to check; do not repeat.' : (error.message || 'Undo failed'), true);
        }
      }, { signal: listeners.signal });
      state.pendingUndo = { button: undo, path: state.path };
      state.shell?.querySelector('.files-pane-status')?.append(' ', undo);
    }
    if (entry.provider === 'host' && choice.action === 'rename' && actionOwner === state.owner && actionPolicy === filesPolicyGeneration()) {
      void offerFinishCleanup(actionId, actionPolicy, actionOwner, actionLifecycle);
    }
    if (recoveryUnavailable && response.history.action_id) {
      const repairActionId = String(response.history.action_id);
      const repairOwner = state.owner;
      const button = el('button', { type: 'button', text: 'Repair recovery copy', title: 'Capture the original saved revision without repeating this operation' });
      button.addEventListener('click', async () => {
        if (listeners.signal.aborted || state.owner !== repairOwner) return;
        button.disabled = true;
        try {
          const repaired = await fetch('/api/history/capture-repair/' + encodeURIComponent(repairActionId), { method: 'POST', credentials: 'same-origin', signal: listeners.signal });
          const payload = await repaired.json();
          if (!repaired.ok) throw new Error(payload?.detail?.message || 'Recovery copy is unavailable; the gap remains.');
          if (listeners.signal.aborted || state.owner !== repairOwner) return;
          setStatus(payload.message || 'Recovery copy repaired. The saved operation was not repeated.');
        } catch (error) {
          if (error?.name !== 'AbortError' && state.owner === repairOwner) {
            setStatus(`Saved; recovery gap remains. ${error.message || 'Recovery repair unavailable.'} Action: ${repairActionId}`, true);
          }
        }
      }, { signal: listeners.signal });
      state.shell?.querySelector('.files-pane-status')?.append(` · Action: ${repairActionId} `, button);
    }
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError') setStatus(error?.code === 'mutation_outcome_unknown' ? 'The outcome is not known. Refresh to check; do not repeat.' : error.message || `${choice.label} failed`, true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function sameFileHistoryKeys(left, right) {
  return left.length === right.length && left.every((key, index) => key === right[index]);
}

async function openSelectedCheckpoint(entries, selectionCurrent, capturedGuard = null) {
  if (!Array.isArray(entries) || entries.length<1 || entries.length>16 || !entries.every(entry=>entry?.provider==='host' && !isDirectory(entry) && entry.resource_ref && entry.resource_id)) {
    setStatus('Choose one to sixteen Host files.',true);return false;
  }
  const model = capturedGuard ? validateFilesSelectionGuard(capturedGuard) : ensureSelectionModel(activeColumn());
  const guard = model && (capturedGuard || captureFilesSelectionGuard(model));
  if (!guard || model.selectedEntries().length!==entries.length || model.selectedEntries().some((entry,index)=>entrySelectionKey(entry)!==entrySelectionKey(entries[index]))) {
    setStatus('Select the files in the current folder first.',true);return false;
  }
  const owner=state.owner,policy=filesPolicyGeneration(),lifecycle=state.lifecycleGeneration,content=state.contentGeneration;
  const current=()=>Boolean(selectionCurrent() && validateFilesSelectionGuard(guard) && state.nativeWindow?.visible && state.owner===owner && filesPolicyGeneration()===policy
    && state.lifecycleGeneration===lifecycle && state.contentGeneration===content);
  try {
    const {runCheckpointFlow}=await import('./checkpointFlow.js');if(!current())return false;
    const chooseRoot=async()=>{
      const {createResourcePicker}=await import('./copal/resourcePicker.js');if(!current())return null;
      return await new Promise(resolve=>{let selected=null;
        const picker=createResourcePicker({client:filesFacadeClient,purpose:'folder',accountScope:owner,workspaceScope:state.currentWorkspaceId||copalWorkspace(),
          generation:policy,originWindow:state.nativeWindow,getGeneration:()=>filesPolicyGeneration(),getAccountScope:()=>state.owner,
          getWorkspaceScope:()=>state.currentWorkspaceId||copalWorkspace(),isOriginCurrent:current,rootLabel:'Choose the authorized History root',
          isCompatible:resource=>resource.provider==='host' && resource.capabilities?.children===true?'':'Choose an authorized Host folder.',
          onSelect:resource=>{selected=resource;return true;},onClose:cancelled=>{if(state.managedRootPicker===picker)state.managedRootPicker=null;resolve(cancelled?null:selected);}});
        state.managedRootPicker?.destroy();state.managedRootPicker=picker;void picker.open();});
    };
    await runCheckpointFlow({client:filesFacadeClient,entries,chooseRoot,current,scopeRoot:state.nativeWindow?.root,
      status:(message,error=false)=>setStatus(message,error),openHistory:async entry=>{const target=createFilesHistoryTarget(entry);if(target)openResourceHistory(target);},
      openCompare:async (entry,load,requestId)=>{if(!current())return;const target=createFilesHistoryTarget(entry);if(!target)return;const {openCheckpointComparison}=await import('./historyView.js');if(current())openCheckpointComparison({...target,exactOnly:true},{requestId,load});},
      openRestore:async (entry,checkpointID,resourceID)=>{if(!current())return;const target=createFilesHistoryTarget(entry);if(!target||target.resourceId!==resourceID)return;const {openCheckpointRestore}=await import('./historyView.js');if(!current())return;const isStatusCurrent=()=>Boolean(state.owner===owner&&state.lifecycleGeneration===lifecycle&&state.nativeWindow?.visible);openCheckpointRestore({...target,exactOnly:true,isStatusCurrent},{checkpointID,client:filesFacadeClient});}});
    return true;
  } catch(error) {if(current())setStatus(String(error?.message||'Checkpoint unavailable.'),true);return false;}
}

function createFilesHistoryTarget(entry) {
  const resourceId = String(entry?.resource_id || '').trim();
  if (!resourceId || entry?.provider !== 'host' || isDirectory(entry)) return null;
  const columnIndex = state.activeColumnIndex;
  const column = state.columns[columnIndex] || activeColumn();
  const model = ensureSelectionModel(column, columnIndex);
  if (!model) return null;
  const scopeKey = String(model.snapshot().scopeKey || '');
  let capturedModel = model;
  const targetKey = entrySelectionKey(entry);
  const captured = {
    owner: String(state.owner || ''),
    workspace: String(state.currentWorkspaceId || copalWorkspace()),
    provider: String(state.provider || ''),
    policyGeneration: filesPolicyGeneration(),
    contentGeneration: Number(state.contentGeneration || 0),
    lifecycleGeneration: Number(state.lifecycleGeneration || 0),
    pane: String(state.nativeWindow?.id || 'files-window'),
    columnIndex,
    parentRef: String(column?.resourceRef || ''),
    parentId: String(column?.resourceId || ''),
    scopeKey,
    selectionEpoch: Number(model.epoch || 0),
    selectionKeys: model.selectedKeys(),
    targetKey,
  };
  const originWindow = state.nativeWindow;
  const statusContextIsCurrent = () => Boolean(originWindow && originWindow.visible && state.nativeWindow === originWindow
    && String(state.owner || '') === captured.owner && Number(state.lifecycleGeneration || 0) === captured.lifecycleGeneration);
  const parentContextIsCurrent = () => {
    const liveColumn = state.columns[captured.columnIndex] || null;
    return String(state.owner || '') === captured.owner
      && String(state.currentWorkspaceId || copalWorkspace()) === captured.workspace
      && String(state.provider || '') === captured.provider
      && filesPolicyGeneration() === captured.policyGeneration
      && Number(state.lifecycleGeneration || 0) === captured.lifecycleGeneration
      && String(state.nativeWindow?.id || 'files-window') === captured.pane
      && state.activeColumnIndex === captured.columnIndex
      && String(liveColumn?.resourceRef || '') === captured.parentRef
      && String(liveColumn?.resourceId || '') === captured.parentId;
  };
  const contextIsCurrent = () => {
    const liveColumn = state.columns[captured.columnIndex] || null;
    const liveModel = state.selectionModels.get(captured.scopeKey);
    const liveEntries = liveColumn?.entries || state.entries;
    return parentContextIsCurrent()
      && Number(state.contentGeneration || 0) === captured.contentGeneration
      && liveModel === capturedModel
      && Number(capturedModel.epoch || 0) === captured.selectionEpoch
      && sameFileHistoryKeys(capturedModel.selectedKeys(), captured.selectionKeys)
      && captured.selectionKeys.includes(captured.targetKey)
      && liveEntries.some((candidate) => String(candidate?.resource_id || '') === resourceId
        && entrySelectionKey(candidate) === captured.targetKey);
  };
  return {
    resourceId,
    name: String(entry.name || 'Selected file'),
    provider: 'host',
    isContextCurrent: contextIsCurrent,
    isStatusCurrent: statusContextIsCurrent,
    onRestored: async () => {
      if (!contextIsCurrent()) return { status: 'context_changed' };
      const expectedRefreshGeneration = captured.contentGeneration + 1;
      const refreshed = await reloadManagedColumn(captured.columnIndex);
      if (!refreshed) return { status: 'refresh_failed' };
      if (!parentContextIsCurrent() || Number(state.contentGeneration || 0) !== expectedRefreshGeneration) {
        return { status: 'context_changed' };
      }
      const refreshedColumn = state.columns[captured.columnIndex];
      const refreshedModel = ensureSelectionModel(refreshedColumn, captured.columnIndex);
      const available = new Set((refreshedColumn?.entries || []).map(entrySelectionKey));
      const preservedKeys = captured.selectionKeys.filter((key) => available.has(key));
      if (!preservedKeys.includes(captured.targetKey)) {
        return { status: 'refresh_failed', message: 'The source file is no longer in the refreshed folder.' };
      }
      refreshedModel.replaceSelection(preservedKeys);
      applyModelSelection(refreshedModel);
      capturedModel = refreshedModel;
      captured.contentGeneration = Number(state.contentGeneration || 0);
      captured.selectionEpoch = Number(refreshedModel.epoch || 0);
      captured.selectionKeys = refreshedModel.selectedKeys();
      return { status: 'refreshed' };
    },
  };
}

function openCapturedFolderHistory(snapshot, trigger = document.activeElement) {
  const folder = snapshot.directory;
  if (!fileResourceCanBrowseHostFolder(folder) || !isCommandCaptureCurrent(snapshot)) return false;
  const folderRef = folder.resourceRef;
  const workspace = String(state.currentWorkspaceId || copalWorkspace());
  const openFolder = destinationResourceRef(snapshot.column) === folderRef;
  const wasActive = snapshot.column === activeColumn();
  const current = openFolder ? () => !listeners.signal.aborted && state.nativeWindow?.visible
    && state.lifecycleGeneration === snapshot.lifecycle && state.owner === snapshot.owner
    && state.accountId === snapshot.accountId && document.body?.dataset?.accountId === snapshot.accountId
    && String(state.currentWorkspaceId || copalWorkspace()) === workspace && filesPolicyGeneration() === snapshot.policy
    && destinationResourceRef(state.columns[snapshot.columnIndex]) === folderRef
    && (!wasActive || destinationResourceRef(activeColumn()) === folderRef)
    : () => isCommandCaptureCurrent(snapshot);
  return openFolderHistory({
    folderRef, name: folder.name || folder.title, isContextCurrent: current,
    onRestored: openFolder ? async () => {
      if (!current()) return { status: 'context_changed' };
      return await reloadManagedColumn(snapshot.columnIndex, { preserveColumns: true })
        ? { status: 'refreshed' } : { status: 'refresh_failed' };
    } : null,
  }, trigger);
}

function openCapturedGitWorkbench(snapshot) {
  const folder = snapshot.directory;
  if (!fileResourceCanBrowseHostFolder(folder) || !isCommandCaptureCurrent(snapshot)) return false;
  const navigation = window.sessionModule?.getSessionNavigationToken?.();
  const sameChat = () => snapshot.sessionId === (window.sessionModule?.getCurrentSessionId?.() || '')
    && navigation === window.sessionModule?.getSessionNavigationToken?.();
  const isStatusCurrent = () => !listeners.signal.aborted && state.nativeWindow?.visible
    && snapshot.owner === state.owner && snapshot.accountId === state.accountId
    && snapshot.accountId === document.body?.dataset?.accountId && sameChat();
  return openGitWorkbenchWindow({
    rootRef: folder.resourceRef, name: folder.name || folder.title, chatID: snapshot.sessionId,
    account: snapshot.owner, generation: snapshot.policy, getGeneration: filesPolicyGeneration,
    isCurrent: () => isStatusCurrent() && isCommandCaptureCurrent(snapshot), isStatusCurrent,
    setStatus, signal: listeners.signal, trigger: document.activeElement,
  });
}

async function performLibrarySelectionAction(entries, choice, snapshot) {
  const clone = choice.action === 'library.clone';
  const current = () => isCommandCaptureCurrent(snapshot) && (!clone ||
    snapshot.sessionId === (window.sessionModule?.getCurrentSessionId?.() || ''));
  if (!current()) return false;
  if (!clone && !await uiModule.styledConfirm(entries.length === 1 ? 'Delete this document?' : `Delete ${entries.length} documents?`, {
    title:entries.length === 1 ? 'Delete document' : 'Delete documents', confirmText:'Delete', danger:true,
  })) return false;
  if (!current()) return false;
  const actions = await import('./filesLibraryActions.js');
  let completed = 0;
  let failed = 0;
  let createdId = null;
  for (const entry of entries) {
    if (!current()) break;
    try {
      if (clone) {
        const result = await actions.cloneLibraryDocument(entry.resource_ref, { sessionId:snapshot.sessionId, isCurrent:current });
        createdId = result?.id || null;
      } else {
        await actions.deleteLibraryDocument(entry.resource_ref, { isCurrent:current });
      }
      completed += 1;
    } catch (error) {
      if (error?.name === 'AbortError') break;
      failed += 1;
    }
  }
  if (current()) {
    if (clone && entries.length === 1 && createdId) await window.documentModule?.loadDocument?.(createdId);
    if (current() && snapshot.column?.globalSearch) await searchAllSources(state.searchQuery);
    else if (current() && snapshot.column?.resourceRef) await reloadManagedColumn(snapshot.columnIndex);
  }
  if (snapshot.owner === state.owner && snapshot.lifecycle === state.lifecycleGeneration) {
    const summary = clone ? 'Cloned' : 'Deleted';
    setStatus(`${summary} ${completed} of ${entries.length}${failed ? ` · ${failed} failed` : ''}`, completed !== entries.length);
  }
  return completed === entries.length;
}

async function performSelectedBulkAction(entries, choice, snapshot) {
  const guard = snapshot.selection;
  const current = () => isCommandCaptureCurrent(snapshot);
  const model = validateFilesSelectionGuard(guard);
  if (!model || !current()) {
    setStatus('The Files selection or access changed; choose the action again.', true);
    return false;
  }
  const capturedKeys = Array.isArray(guard.selectedKeys) ? guard.selectedKeys.map(String) : [];
  const expectedEntries = model.selectedEntries();
  if (expectedEntries.length !== entries.length || expectedEntries.some((entry, index) =>
    entrySelectionKey(entry) !== String(capturedKeys[index]) || entrySelectionKey(entries[index]) !== String(capturedKeys[index]))) {
    setStatus('The Files selection changed; choose the action again.', true);
    return false;
  }
  if (choice.action === 'history.checkpoint') return await openSelectedCheckpoint(entries, current, guard);
  if (choice.action === 'library.clone' || choice.action === 'library.delete') return performLibrarySelectionAction(entries, choice, snapshot);
  if (choice.action === 'transfer.move' || choice.action === 'transfer.copy' || choice.action === 'transfer.duplicate') {
    const destinationIndex = Number(guard.filesDestinationIndex);
    const capturedColumn = Number.isInteger(destinationIndex) && destinationIndex >= 0
      ? state.columns[destinationIndex] || null : null;
    const destinationRef = String(guard.filesDestinationRef || capturedColumn?.resourceRef || '').trim();
    if (!capturedColumn || !destinationRef || destinationResourceRef(capturedColumn) !== destinationRef) {
      setStatus('The destination folder changed; choose the transfer again.', true);
      return false;
    }
    const destination = { ...capturedColumn, resource_ref:destinationRef, kind:'folder' };
    if (!destination.capabilities?.includes('children') || !destination.capabilities.includes('write')) {
      setStatus('Choose an authorized destination folder before transferring the selection.', true);
      return false;
    }
    if (choice.action === 'transfer.duplicate' && destinationRef !== String(model.scope?.parentRef || '')) {
      setStatus('Open the selected files’ containing folder to duplicate them.', true);
      return false;
    }
    return executeSelectedFilesTransfer(choice.action === 'transfer.move' ? 'move' : 'copy', {
      model, destination, destinationIndex, collision:choice.action === 'transfer.duplicate' ? 'rename' : 'fail',
    });
  }
  if (choice.action !== 'archive.set' && choice.action !== 'favorite.set') return false;
  let completed = 0;
  let failed = 0;
  for (const entry of entries) {
    if (!current()) {
      setStatus(`${completed} of ${entries.length} completed; the Files selection or access changed, so remaining actions were stopped.`, true);
      return false;
    }
    if (!entry.resource_ref) { failed += 1; continue; }
    try {
      await filesFacadeClient.action(entry.resource_ref, choice.action,
        { value: choice.value }, { actionId: operationId('files-bulk-action') });
      completed += 1;
    } catch (_) { failed += 1; }
  }
  if (!current()) {
    setStatus(`${completed} of ${entries.length} completed; the Files selection or access changed before refresh.`, true);
    return false;
  }
  clearFilesSelection(snapshot.column, snapshot.columnIndex);
  if (snapshot.column?.globalSearch) await searchAllSources(state.searchQuery);
  else if (snapshot.column?.resourceRef) await reloadManagedColumn(snapshot.columnIndex);
  setStatus(`${choice.label}: ${completed} of ${entries.length}${failed ? ` · ${failed} failed` : ''}`);
  return failed === 0;
}

function showFilesNewMenu({ anchor } = {}) {
  const snapshot = captureCommands({ context:'folder' });
  return showCapturedFilesMenu(anchor, snapshot, ['files.new-file', 'files.new-folder']);
}

function openSelectedActions() {
  const anchor = state.shell?.querySelector('[data-files-actions]');
  return showCapturedFilesMenu(anchor, captureCommands({ context:'selection' }));
}

async function launchManagedSourceApp(app, { resourceRef = '', exact = false } = {}) {
  const target = String(app || '');
  if (target === 'editor') {
    if (exact && resourceRef) {
      const module = window.copalModule || await import('./copal.js');
      if (typeof module?.openResource !== 'function') throw new Error('Editor exact-open is unavailable');
      await module.openResource(resourceRef);
      return;
    }
    const launcher = document.querySelector('[data-copal-view="notes"]');
    if (!launcher) throw new Error('Editor is unavailable');
    launcher.click();
    return;
  }
  if (target === 'copal_notes') {
    if (exact && resourceRef) {
      const module = window.copalModule || await import('./copal.js');
      if (typeof module?.openResource !== 'function') throw new Error('Editor exact-open is unavailable');
      await module.openResource(resourceRef);
      return;
    }
    const launcher = document.querySelector('[data-copal-view="notes"]');
    if (!launcher) throw new Error('Editor is unavailable');
    launcher.click();
    return;
  }
  if (target === 'imps') {
    // Imps (Image Processing Suite) owns image open. Open the editor
    // directly; legacy /gallery links resolve to Files before reaching here.
    if (exact && resourceRef) {
      const editor = window.copalModule || await import('./copal.js');
      if (typeof editor?.openImageResource !== 'function') throw new Error('Image Editor exact-open is unavailable');
      await editor.openImageResource(resourceRef);
      return;
    }
    const editor = window.copalModule || await import('./copal.js');
    if (typeof editor?.openBlankImage !== 'function') throw new Error('Blank image Editor is unavailable');
    await editor.openBlankImage({name:'Untitled image'});
    return;
  }
  if (target === 'document_editor' || target === 'library') {
    if (exact && target === 'document_editor' && resourceRef) {
      if (typeof window.documentModule?.openResource !== 'function') throw new Error('Document exact-open is unavailable');
      await window.documentModule.openResource(resourceRef);
      return;
    }
    if (window.sessionModule?.openLibrary) {
      window.sessionModule.openLibrary('documents');
      return;
    }
    await openLibraryCollection('documents');
    return;
  }
  if (target === 'chat') {
    if (exact && resourceRef) {
      if (typeof window.documentModule?.openLibraryResource !== 'function') throw new Error('Chat exact-open is unavailable');
      await window.documentModule.openLibraryResource(resourceRef);
      return;
    }
    if (window.sessionModule?.openLibrary) {
      window.sessionModule.openLibrary('chats');
      return;
    }
    throw new Error('Chats are unavailable');
  }
  if (target === 'research') {
    if (exact && resourceRef) {
      if (typeof window.documentModule?.openLibraryResource !== 'function') throw new Error('Research exact-open is unavailable');
      await window.documentModule.openLibraryResource(resourceRef);
      return;
    }
    if (window.sessionModule?.openLibrary) {
      window.sessionModule.openLibrary('research');
      return;
    }
    const launcher = document.getElementById('tool-research-btn');
    if (!launcher) throw new Error('Research is unavailable');
    launcher.click();
    return;
  }
  throw new Error('This source app is unavailable');
}

async function openManagedEntry(entry, capturedCurrent = () => true) {
  if (!capturedCurrent()) return false;
  if (options.pickerMode) return confirmPickerEntry(entry);
  if (isPublishedLibraryEntry(entry)) {
    if (entry.capabilities?.includes('preview')) await previewEntry(entry, entryPath(entry));
    else if (entry.capabilities?.includes('download')) await downloadEntry(entry);
    else setStatus('This published resource can no longer be previewed or downloaded.', true);
    return;
  }
  try {
    const owner = state.owner, lifecycle = state.lifecycleGeneration, policy = filesPolicyGeneration(), workspace = copalWorkspace();
    const isCurrent = () => capturedCurrent() && state.owner === owner && state.lifecycleGeneration === lifecycle
      && filesPolicyGeneration() === policy && copalWorkspace() === workspace;
    const purpose = isMetadataInspection(entry?.format) ? 'inspect' : null;
    const response = await filesFacadeClient.open(entry.resource_ref, {purpose});
    if (!isCurrent()) return false;
    if ((response?.resource?.format?.formatId === 'iwi' || metadataFormatId(response?.resource?.format?.formatId)) && (!isCurrent() || response.resource.id !== entry.resource_id)) {
      throw new Error('The preview source or Files scope changed. Reopen the item.');
    }
    const refreshedRef = String(response?.resource?.ref || entry.resource_ref || '');
    await dispatchFilesDestination(response, { resourceRef:refreshedRef, surface:options.surface || 'files', isCurrent });
    if (!isCurrent()) return false;
    if (response?.resource) {
      const refreshed = facadeEntry(response.resource);
      const index = state.entries.findIndex((candidate) => entrySelectionKey(candidate) === entrySelectionKey(entry));
      if (index >= 0) state.entries[index] = refreshed;
    }
    setStatus(`Opened ${entry.name} in its source app`);
  } catch (error) {
    if (capturedCurrent()) setStatus(error.message || 'Source app could not be opened', true);
  }
}

function contextEntryForNode(node) {
  const resourceRef = String(node?.dataset?.resourceRef || '').trim();
  const path = String(node?.dataset?.path || '').trim();
  const candidates = [
    ...state.entries,
    ...state.columns.flatMap((column) => column.entries || []),
  ];
  return candidates.find((entry) => (
    resourceRef && String(entry.resource_ref || '') === resourceRef
  ) || (
    !resourceRef && path && entryPath(entry) === path
  )) || (resourceRef ? { resource_ref:resourceRef, name:String(node?.dataset?.fileName || resourceRef) } : null);
}

async function executeSelectedFilesTransfer(kind, { model = null, destination = null, destinationIndex = null, collision = 'fail' } = {}) {
  const selectedModel = model || ensureSelectionModel(activeColumn(), state.activeColumnIndex);
  const target = destination || activeColumn();
  const initialKeys = selectedModel?.selectedKeys?.() || [];
  const batchGeneration = filesPolicyGeneration();
  if (!initialKeys.length || !target || !isDirectory(target)) {
    setStatus('Choose an authorized destination folder for this transfer.', true);
    return false;
  }
  const batchId = operationId('files-batch');
  const committedKeys = new Set();
  const batchOwner = String(selectedModel.scope?.owner || state.owner || '');
  const batchScopeKey = String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '');
  const batchLifecycle = state.lifecycleGeneration;
  const chunkCount = Math.ceil(initialKeys.length / FILES_MAX_DRAG_ITEMS);
  if (kind === 'move' && selectedModel.scope?.provider === 'host' && initialKeys.length > 1) {
    try {
      const destinationRef = destinationResourceRef(target);
      let rootPayload = null, rootResult = null;
      for (let index = 0; index < chunkCount; index += 1) {
        const keys = initialKeys.slice(index * FILES_MAX_DRAG_ITEMS, (index + 1) * FILES_MAX_DRAG_ITEMS);
        const keySet = new Set(keys);
        const entries = selectedModel.selectedEntries().filter(entry => keySet.has(entrySelectionKey(entry))).map((entry, n) => ({...entry, item_id:`preflight-${index}-${n}`}));
        const payload = buildInternalDragPayload({scope:selectedModel.scope, generation:batchGeneration, policyGeneration:batchGeneration,
          owner:state.owner, pane:state.nativeWindow?.id || 'files-window', selectionEpoch:selectedModel.epoch, kind, entries, selectedKeys:keys});
        if (!payload || !destinationRef) throw new Error('The Files selection could not be checked.');
        const sources = payload.sources.map(source => ({itemId:source.item_id, resourceRef:source.resource_ref, expectedRevision:source.revision || undefined}));
        const result = await filesFacadeClient.transferRootRequirement({generation:batchGeneration, sources, destinationRef});
        const currentKeys = selectedModel.selectedKeys();
        if (state.owner !== batchOwner || filesPolicyGeneration() !== batchGeneration || state.lifecycleGeneration !== batchLifecycle
          || String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '') !== batchScopeKey
          || currentKeys.length !== initialKeys.length || currentKeys.some((key, n) => key !== initialKeys[n])) throw new Error('The Files selection or access changed.');
        if (result.root_required || result.destination_root_required) {
          if (chunkCount !== 1) throw new Error('This selection is too large for one managed move.');
          rootPayload = payload; rootResult = result;
        }
      }
      if (rootPayload) {
        const response = await executeFilesTransfer(rootPayload, target, 'fail', destinationColumnIndex(target, destinationIndex),
          {model:selectedModel, selectionKeys:initialKeys, batchId, rootRequirement:rootResult});
        return Boolean(response && (response.items || []).every(item => ['committed', 'unchanged'].includes(item.outcome)));
      }
    } catch (error) { setStatus(error?.message || 'The Files selection could not be checked.', true); return false; }
  }
  for (let chunkIndex = 0; chunkIndex < chunkCount; chunkIndex += 1) {
    const expectedCurrent = initialKeys.filter((key) => !committedKeys.has(key));
    const currentKeys = selectedModel.selectedKeys();
    if (currentKeys.length !== expectedCurrent.length || currentKeys.some((key, index) => key !== expectedCurrent[index])
      || state.owner !== batchOwner || filesPolicyGeneration() !== batchGeneration || state.lifecycleGeneration !== batchLifecycle
      || String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '') !== batchScopeKey) {
      setStatus('The Files selection or access changed; remaining transfers were stopped.', true);
      return false;
    }
    const chunkKeys = initialKeys.slice(chunkIndex * FILES_MAX_DRAG_ITEMS, (chunkIndex + 1) * FILES_MAX_DRAG_ITEMS)
      .filter((key) => !committedKeys.has(key));
    if (!chunkKeys.length) continue;
    const chunkSet = new Set(chunkKeys);
    const childOperation = operationId(`${batchId}-child-${chunkIndex}`);
    const entries = selectedModel.selectedEntries().filter((entry) => chunkSet.has(entrySelectionKey(entry))).map((entry, entryIndex) => ({
      ...entry,
      // Every linked child request gets fresh item IDs. This prevents a
      // later chunk or collision retry from replaying an earlier child.
      item_id: `${childOperation}-${entryIndex}`.slice(0, 128),
    }));
    const payload = buildInternalDragPayload({
      scope: selectedModel.scope, generation: filesPolicyGeneration(), policyGeneration: filesPolicyGeneration(),
      owner: state.owner, pane: state.nativeWindow?.id || 'files-window', selectionEpoch: selectedModel.epoch,
      kind, entries, selectedKeys: chunkKeys,
    });
    if (!payload) {
      setStatus('The Files batch could not be encoded safely; remaining transfers were stopped.', true);
      return false;
    }
    const response = await executeFilesTransfer(
      payload,
      target,
      collision,
      destinationColumnIndex(target, destinationIndex == null ? state.activeColumnIndex : destinationIndex),
      { model: selectedModel, selectionKeys: expectedCurrent, batchId, operationIdOverride: childOperation, refresh: false },
    );
    if (state.owner !== batchOwner || filesPolicyGeneration() !== batchGeneration || state.lifecycleGeneration !== batchLifecycle
      || String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '') !== batchScopeKey) {
      setStatus('The Files selection or access changed while the transfer was running; remaining transfers were stopped.', true);
      return false;
    }
    const receipt = reconcileTransferReceipt(response, payload.sources.map((source) => source.item_id), { operationId: childOperation, generation: Number(payload.generation) });
    if (!receipt.ok) {
      setStatus(`${receipt.reason} Remaining transfers were stopped.`, true);
      return false;
    }
    const committedSourceKeys = new Set(payload.sources
      .filter((source) => receipt.committed.has(String(source.item_id)))
      .map((source) => String(source.resource_key)));
    const expectedAfter = expectedCurrent.filter((key) => !committedSourceKeys.has(String(key)));
    const actualAfter = selectedModel.selectedKeys();
    if (actualAfter.length !== expectedAfter.length || actualAfter.some((key, index) => key !== expectedAfter[index])) {
      setStatus('The Files selection changed while the transfer was running; remaining transfers were stopped.', true);
      return false;
    }
    for (const source of payload.sources) {
      if (receipt.committed.has(String(source.item_id))) committedKeys.add(String(source.resource_key));
    }
    if (receipt.failed.length || receipt.pending) {
      setStatus(`${committedKeys.size} of ${initialKeys.length} completed; remaining items need attention.`, true);
      return false;
    }
    setStatus(`${committedKeys.size} of ${initialKeys.length} completed…`);
  }
  if (committedKeys.size === initialKeys.length && state.owner === batchOwner && filesPolicyGeneration() === batchGeneration
    && state.lifecycleGeneration === batchLifecycle
    && String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '') === batchScopeKey) {
    await reloadManagedColumn(destinationColumnIndex(target, destinationIndex == null ? state.activeColumnIndex : destinationIndex));
    if (state.owner !== batchOwner || filesPolicyGeneration() !== batchGeneration || state.lifecycleGeneration !== batchLifecycle
      || String(selectedModel.snapshot?.().scopeKey || selectedModel.scopeKey || '') !== batchScopeKey) {
      setStatus('File access or selection changed while refreshing the transfer result.', true);
      return false;
    }
  }
  setStatus(`${committedKeys.size} item${committedKeys.size === 1 ? '' : 's'} ${kind}d`);
  return committedKeys.size === initialKeys.length;
}

hooks.__openClankFilesContextCapture = (node) => {
  const index = columnIndexForNode(node);
  const column = state.columns[index];
  const key = String(node?.dataset?.filesSelectionKey || '');
  const entry = (column?.entries || state.entries).find(item => entrySelectionKey(item) === key);
  if (!entry) return null;
  supersedeFilesRefreshForContext();
  const selectedColumn = activateColumn(index);
  const destinationIndex = selectedColumn.index;
  const destination = selectedColumn.column;
  const model = selectedColumn.model;
  // A workspace event can update the scope before its delayed facade reload
  // replaces the rows. Reindex the still-mounted rows under the new scope so
  // the exact clicked opaque identity can be selected and captured.
  if (!model.orderedKeys().includes(key)) model.setItems(selectedColumn.column?.entries || state.entries);
  // Desktop Files keeps a right-clicked selected row in the existing set;
  // right-clicking elsewhere changes the context target to that one row.
  if (!model.has(key)) model.select(key);
  applyModelSelection(model, { render:false });
  syncVisibleSelection(model, node.closest('.files-column-list') || state.shell?.querySelector('[data-files-body]'));
  updateManagedActionControl();
  return Object.freeze({
    filesScope: model.snapshot().scopeKey,
    filesColumnIndex: selectedColumn.index,
    filesEpoch: model.epoch,
    filesGeneration: filesPolicyGeneration(),
    filesOwner: state.owner,
    filesWorkspace: state.currentWorkspaceId || copalWorkspace(),
    filesPane: state.nativeWindow?.id || 'files-window',
    filesDestinationIndex: destinationIndex,
    filesDestinationRef: destinationResourceRef(destination),
    selectedKeys: Object.freeze(model.selectedKeys()), targetKey: key,
    filesParentId: String(destination?.resource_id || destination?.resourceId || ''),
    filesSourceProvider: String(model.scope.provider || ''), filesQuery: String(destination?.query || ''),
    filesLifecycle: state.lifecycleGeneration, filesContent: state.contentGeneration,
    filesSources: Object.freeze(model.selectedEntries().map(source => Object.freeze({
      key: resourceKey(source, model.scope.provider, model.scope),
      id: String(source.resource_id || source.resourceId || ''), ref: source.resource_ref,
      entry: source, metadata: filesCopyListingMetadata(source),
      revision: source.revision ? Object.freeze({ ...source.revision }) : null,
    }))),
    ...creationCaptureForNode(node),
    pasteDestination: captureFilesPasteDestination(node),
  });
};

hooks.__openClankFilesContextCapabilities = (_node, request = null) => {
  const captured = request?.adapterContext;
  const model = captured?.filesScope
    ? state.selectionModels.get(String(captured.filesScope))
    : ensureSelectionModel(activeColumn(), state.activeColumnIndex);
  const capabilities = model?.selectedEntries?.().map(transferCapabilities) || [];
  return {
    move: capabilities.length > 0 && capabilities.every((capability) => capability.move),
    copy: capabilities.length > 0 && capabilities.every((capability) => capability.copy),
  };
};

hooks.__openClankFilesContextCommand = async (command, node, request = null) => {
  if (options.pickerMode) { setStatus('This dialog selects resources; file commands are unavailable.', true); return true; }
  if (command === 'open-workspace-hexes') {
    const workspaceId = String(node?.dataset?.workspaceId || '').trim();
    const workspace = state.workspaces.find(item => String(item?.workspace?.id || '') === workspaceId);
    if (!workspaceId || !workspace || workspace.availability !== 'available' || !workspace.resource) {
      setStatus('This Workspace is no longer available. Refresh Files and try again.', true);
      return true;
    }
    window.dispatchEvent(new CustomEvent('hex-open', { detail: Object.freeze({ workspaceId }) }));
    return true;
  }
  // Copy is an in-memory capture of the originally requested identities.
  // A refreshed listing may rotate refs/reset visual selection without
  // changing those resources. Never substitute the current selected set.
  if (['copy-files', 'copy-file'].includes(command) && request?.adapterContext?.filesParentId) {
    const captured = request.adapterContext;
    const sources = currentContextCopySources(captured);
    if (!sources) { setStatus('The copied source or file access changed; choose Copy again.', true); return true; }
    const revisions = [];
    // Host listing rows omit fingerprints. Acquire the baseline through the
    // existing authenticated stat before preparing a version-bound clipboard.
    for (const entry of sources.entries) {
      if (entry.revision) { revisions.push(entry.revision); continue; }
      const response = await filesFacadeClient.stat(entry.resource_ref);
      const verified = facadeEntry(response?.resource || response || {});
      const current = currentContextCopySources(captured);
      if (!current || current.model !== sources.model || current.entries.some((item, index) => item !== sources.entries[index])
        || verified.resource_id !== entry.resource_id || verified.provider !== entry.provider
        || filesCopyListingMetadata(verified) !== filesCopyListingMetadata(entry)
        || !transferCapabilities(verified).copy || !verified.revision?.kind || !verified.revision?.value) {
        setStatus('The copied source or file access changed; choose Copy again.', true); return true;
      }
      revisions.push(verified.revision);
    }
    return setFilesClipboard(sources.model, 'copy', () => Boolean(currentContextCopySources(captured)), sources.entries, revisions);
  }
  const entry = contextEntryForNode(node);
  if (!entry) return false;
  let capturedModel = null;
  if (request?.adapterContext?.filesScope) {
    const captured = request.adapterContext;
    // Resolve the immutable scope captured at right-click time. A later focus
    // change must never retarget a command to the active column's model.
    const model = state.selectionModels.get(String(captured.filesScope));
    capturedModel = model;
    if (!model) { setStatus('The Files selection changed; choose the command again.', true); return true; }
    const currentKeys = model.selectedKeys();
    const capturedKeys = Array.isArray(captured.selectedKeys) ? captured.selectedKeys : [];
    const exactSelection = currentKeys.length === capturedKeys.length && currentKeys.every((key, index) => key === capturedKeys[index]);
    const currentGeneration = filesPolicyGeneration();
    if (model.snapshot().scopeKey !== captured.filesScope || currentGeneration !== Number(captured.filesGeneration)
      || state.owner !== captured.filesOwner || (state.nativeWindow?.id || 'files-window') !== captured.filesPane
      || model.epoch !== Number(captured.filesEpoch) || !exactSelection || entrySelectionKey(entry) !== captured.targetKey) {
      setStatus('The Files selection changed; choose the command again.', true);
      return true;
    }
  }
  const capabilities = new Set(entry.capabilities || []);
  if (['move-files', 'copy-files', 'move-file', 'copy-file'].includes(command)) {
    const kind = command === 'copy-files' || command === 'copy-file' ? 'copy' : 'move';
    const selectedModel = capturedModel || ensureSelectionModel(activeColumn(), state.activeColumnIndex);
    const selectedEntries = selectedModel.selectedEntries();
    const allowed = selectedEntries.length > 0 && selectedEntries.every((candidate) => {
      const capability = transferCapabilities(candidate);
      return kind === 'move' ? capability.move : capability.copy;
    });
    if (!allowed) {
      setStatus(`${kind === 'move' ? 'Move' : 'Copy'} is unavailable for the selected items.`, true);
      return true;
    }
    if (kind === 'copy') {
      const guard = request?.adapterContext || captureFilesSelectionGuard(selectedModel);
      const lifecycle = state.lifecycleGeneration;
      const content = state.contentGeneration;
      return setFilesClipboard(selectedModel, kind, () => !listeners.signal.aborted && state.nativeWindow?.visible
        && lifecycle === state.lifecycleGeneration && content === state.contentGeneration && Boolean(validateFilesSelectionGuard(guard)));
    }
    // The typed executor owns authorization, receipts and collision policy.
    // Context Move uses the current folder as the destination; a folder
    // target is used only when it was already the captured selection target.
    const capturedDestinationIndex = request?.adapterContext?.filesDestinationIndex;
    const capturedColumn = capturedDestinationIndex >= 0
      ? state.columns[Number(capturedDestinationIndex)] || null
      : activeColumn();
    // Column descriptors use camelCase resourceRef and are not provider
    // entries themselves. Give the executor an opaque, directory-shaped
    // destination carrying the captured ref so context commands can transfer
    // through the same authorized route in both list and Columns modes.
    const capturedDestinationRef = String(request?.adapterContext?.filesDestinationRef || capturedColumn?.resourceRef || '').trim();
    const destination = isDirectory(entry) && !selectedModel.has(entrySelectionKey(entry))
      ? entry
      : capturedDestinationRef
        ? { ...(capturedColumn || {}), resource_ref:capturedDestinationRef, kind:'folder', capabilities:[...new Set([...(capturedColumn?.capabilities || []), 'children'])] }
        : activeColumn();
    return executeSelectedFilesTransfer(kind, {
      model: selectedModel,
      destination,
      destinationIndex: isDirectory(entry) ? columnIndexForNode(node) : request?.adapterContext?.filesDestinationIndex ?? state.activeColumnIndex,
    });
  }
  if (command === 'select-all-files') {
    const model = capturedModel || ensureSelectionModel(activeColumn());
    model.selectAll();
    applyModelSelection(model);
    setStatus(`${model.selectedKeys().length}${state.nextCursor ? '+' : ''} items selected`);
    return true;
  }
  if (command === 'open-in-editor' || command === 'open-in-editor-split-right' || command === 'open-in-editor-split-below') {
    if (!capabilities.has('open') || !entry.resource_ref || isDirectory(entry) || !isTextualEntry(entry)) {
      setStatus(`${entry.name || 'This item'} cannot be opened in Editor.`, true);
      return true;
    }
    const intent = command === 'open-in-editor-split-right' ? 'splitRight' : command === 'open-in-editor-split-below' ? 'splitBelow' : undefined;
    // The exact-open callback selects a shared buffer/leaf; the Editor shell
    // owns visibility, foreground focus and routing separately.
    const editor = window.copalModule || await import('./copal.js');
    await (editor.default || editor).open('notes');
    const opener = window.__openClankOpenResourceHandle;
    if (typeof opener === 'function') await opener({ resourceRef: entry.resource_ref, resourceKey: entrySelectionKey(entry), name: entry.name, ...(intent ? { intent } : {}) });
    else await openManagedEntry(entry);
    return true;
  }
  if (command === 'open-file') {
    if (!capabilities.has('open') || !entry.resource_ref) return false;
    await openManagedEntry(entry);
    return true;
  }
  if (command === 'copy-file-path') {
    const value = String(entry.path || '').trim();
    if (!value || !navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
    await navigator.clipboard.writeText(value);
    return true;
  }
  const capabilityByCommand = { 'reveal-file':'stat', 'rename-file':'rename', 'trash-file':'trash', 'restore-file':'restore' };
  const required = capabilityByCommand[command];
  if (!required || !capabilities.has(required)) return false;
  if (command === 'reveal-file') return Boolean(await revealResource(entry.resource_ref));
  if (['rename-file', 'move-file', 'trash-file', 'restore-file'].includes(command)) {
    const action = command.replace('-file', '');
    const choice = entryActionChoices(entry).find((candidate) => candidate.action === action);
    if (!choice) return false;
    return performManagedAction(entry, choice);
  }
  return false;
};

async function openSelected() {
  const captured = captureCommands({ context:'selection' });
  const [entry] = captured.entries;
  const items = commandDescriptors(captured);
  if (captured.entries.length === 1 && isCommandCaptureCurrent(captured)) {
    const supportedIds = isPublishedLibraryEntry(entry) ? ['files.preview', 'files.download'] : ['files.open', 'files.preview'];
    for (const id of supportedIds) {
      const command = items.find(item => item.id === id && !item.disabledReason);
      if (command) return command.run();
    }
  }
  setStatus('Select one item that Files can open.', true);
  return false;
}

async function addHostLocation() {
  try {
    const controller = await import('./fileLocationController.js');
    const added = await controller.openFileLocationWizard({
      suggestedPath: state.hostPath || state.defaultPath || '',
      onAdded: ({appAccess}) => setStatus(appAccess ? 'Folder is available in Files and Editor.' : 'Location registered.'),
    });
    if (!added) setStatus('Add folder closed.');
  } catch (error) {
    setStatus(error.message || 'Location could not be added', true);
  }
}

function handleWindowClosed() {
  clearFilesReadRecovery();
  resetFilesReadRetry();
  categoryObserver?.disconnect();
  cancelReveal();
  const splitPair = splits.get(id);
  if (splitPair) { splits.delete(id); restoreSplit(splitPair); }
  cancelFilesGesture('window-hidden');
  abortFilesImports();
  clearKeepBothRetry();
  state.contentGeneration += 1;
  state.lifecycleGeneration += 1;
  state.revealGeneration += 1;
  state.contentController?.abort();
  state.contentController = null;
  state.navigationController?.abort();
  state.navigationController = null;
  state.favoritesController?.abort();
  state.favoritesController = null;
  state.workspaceController?.abort();
  state.workspaceController = null;
  state.managedRootPicker?.destroy(); state.managedRootPicker = null;
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  state.contextAdapterDispose?.();
  state.contextAdapterDispose = null;
  closePreview();
  setEditorRoot(null,false);
  destroyExplorerTree();
  if (id !== 'files-window' && !options.container) {
    listeners.abort(); localMenu?.dispose(); browserResize?.disconnect(); state.navigation?.dispose(); state.pane?.destroy();
    state.nativeWindow?.destroy(); browsers.delete(id); if (activeBrowser === api) activeBrowser = browsers.get('files-window') || null;
  }
}

function confirmedFilesPolicyFailure(error) {
  return [400, 401, 403, 404, 410, 422].includes(Number(error?.status || 0));
}

function captureFilesProjection() {
  return {
    owner: state.owner,
    provider: state.provider,
    path: state.path,
    hostPath: state.hostPath,
    searchQuery: state.searchQuery,
    sort: { ...state.sort },
    columns: state.columns.map(column => ({
      resourceId: String(column?.resourceId || ''),
      provider: String(column?.provider || state.provider || ''),
      name: String(column?.name || ''),
      sort: { ...(column?.sort || state.sort) },
      sortKeys: [...sortKeysFor(column?.sortKeys || SORT_KEYS)],
      query: String(column?.query || ''),
      activeName: String(column?.activeName || ''),
    })),
  };
}

function abortFilesAuthorityRequests() {
  cancelFilesGesture('policy-change');
  abortFilesImports();
  clearKeepBothRetry();
  const lifecycle = ++state.lifecycleGeneration;
  state.contentGeneration += 1;
  state.revealGeneration += 1;
  state.contentController?.abort();
  state.contentController = null;
  state.navigationController?.abort();
  state.navigationController = null;
  state.favoritesController?.abort();
  state.favoritesController = null;
  state.workspaceController?.abort();
  state.workspaceController = null;
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  // Preview URLs and handles are generation-bound capabilities. Close them at
  // the event boundary even when the current folder later revalidates.
  closePreview();
  return lifecycle;
}

// A trusted context capture is a current user gesture. If a policy or
// workspace refresh is still resolving, let that gesture supersede the
// refresh before it mutates the selection model. The refresh keeps its
// original lifecycle token and cannot later clear the captured row.
function supersedeFilesRefreshForContext() {
  if (!state.contentController) return;
  abortFilesAuthorityRequests();
}

function clearFilesAuthorityContent(message = 'File access changed', {catalogs = true} = {}) {
  if (state.editorRootBound) setEditorRoot(null,true);
  if (catalogs) {
    state.managedRootPicker?.destroy(); state.managedRootPicker=null;
    renderFavorites();
  }
  cancelReveal();
  cancelFilesGesture('policy-change');
  clearKeepBothRetry();
  state.contentGeneration += 1;
  state.contentController?.abort();
  state.contentController = null;
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  closePreview();
  state.path = '';
  state.hostPath = '';
  state.entries = [];
  state.nextCursor = null;
  state.columns = [];
  state.selectionModels.clear();
  state.selected = new Set();
  state.treeSelection = null;
  options.onSelection?.(null);
  options.onDirectory?.(null);
  state.searchQuery = '';
  const label = state.shell?.querySelector('[data-files-path]');
  if (label) label.textContent = '';
  syncSearchControl();
  renderEntries();
  updateTreeHighlight();
  updateFavoriteButton();
  setStatus(message, true);
}

async function refreshRawFilesProjection(snapshot, lifecycle) {
  const controller = new AbortController();
  state.contentController = controller;
  const contentGeneration = state.contentGeneration;
  try {
    const response = await filesServiceClient.listDirectory(snapshot.hostPath, {
      sort: requestSortSpec(snapshot.sort),
      cache: false,
      cacheKey: `files-policy-refresh:${snapshot.hostPath}:${JSON.stringify(snapshot.sort)}`,
      signal: controller.signal,
    });
    if (
      controller.signal.aborted
      || lifecycle !== state.lifecycleGeneration
      || contentGeneration !== state.contentGeneration
      || !state.nativeWindow?.visible
    ) return { status: 'stale' };
    const data = response?.data || {};
    const canonical = String(data.path || snapshot.hostPath);
    state.provider = 'host';
    state.hostPath = canonical;
    state.path = canonical;
    state.searchQuery = '';
    state.sort = normalizeSortSpec(snapshot.sort);
    state.entries = sortEntries(data.entries || [], state.sort);
    state.nextCursor = data.next_cursor || null;
    state.columns = [{
      path: canonical,
      entries: state.entries,
      nextCursor: state.nextCursor,
      sort: normalizeSortSpec(state.sort),
      activeName: '',
    }];
    state.activeColumnIndex = 0;
    clearFilesSelection();
    const provider = state.shell?.querySelector('.files-provider-select');
    if (provider) provider.value = 'host';
    const label = state.shell?.querySelector('[data-files-path]');
    if (label) label.textContent = canonical;
    syncSearchControl();
    syncPrimarySortControls();
    renderEntries();
    updateTreeHighlight();
    updateFavoriteButton();
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`, false, { keepUndo: true });
    return { status: 'resolved' };
  } catch (error) {
    if (error?.name === 'AbortError') return { status: 'stale' };
    return { status: confirmedFilesPolicyFailure(error) ? 'invalid' : 'unavailable', error };
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function freshManagedResource(resourceId, provider) {
  const id = String(resourceId || '');
  if (!id) return null;
  const candidates = [
    ...state.managedRoots,
    ...state.navigationRoots,
    ...state.favorites,
    ...state.workspaces.map(item => item?.resource).filter(Boolean),
  ];
  const match = candidates.find(item => (
    String(item?.resource_id || item?.id || '') === id
    && (!provider || String(item?.provider || provider) === provider)
  ));
  return match ? facadeEntry(match) : null;
}

async function refreshManagedFilesProjection(snapshot, lifecycle) {
  if (snapshot.provider === CLANKER_HOME_PROVIDER && !snapshot.columns.some(column => column.resourceId)) {
    const controller = new AbortController();
    state.contentController = controller;
    try {
      const resolved = await openClankerHome(controller, state.contentGeneration);
      if (lifecycle !== state.lifecycleGeneration || controller.signal.aborted || !state.nativeWindow?.visible) {
        return { status: 'stale' };
      }
      return resolved ? { status: 'resolved' } : { status: 'invalid' };
    } catch (error) {
      if (error?.name === 'AbortError') return { status: 'stale' };
      return { status: confirmedFilesPolicyFailure(error) ? 'invalid' : 'unavailable', error };
    } finally {
      if (state.contentController === controller) state.contentController = null;
    }
  }

  if (snapshot.provider === 'all' && !snapshot.columns.some(column => column.resourceId)) {
    try {
      if (snapshot.searchQuery) {
        const response = await filesFacadeClient.search(snapshot.searchQuery, {
          limit: 100,
          sort: managedSortRequest(snapshot.sort),
        });
        if (lifecycle !== state.lifecycleGeneration || !state.nativeWindow?.visible) return { status: 'stale' };
        const column = {
          path: 'all-search', resourceRef: '', resourceId: '', name: 'All sources',
          entries: (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed), nextCursor: null,
          sort: constrainSortSpec(response?.sort || snapshot.sort, response?.sort_keys || SORT_KEYS),
          sortKeys: sortKeysFor(response?.sort_keys || SORT_KEYS), activeName: '',
          query: snapshot.searchQuery, globalSearch: true,
        };
        commitManagedColumn(column, { provider: 'all', columns: [column] });
        return { status: 'resolved' };
      }
      const column = {
        path: 'all', resourceRef: '', resourceId: '', name: 'All sources',
        entries: sortEntries(state.managedRoots, constrainSortSpec(snapshot.sort, ['name'])),
        nextCursor: null, sort: constrainSortSpec(snapshot.sort, ['name']),
        sortKeys: ['name'], activeName: '', query: '', globalSearch: false,
      };
      commitManagedColumn(column, { provider: 'all', columns: [column] });
      return { status: 'resolved' };
    } catch (error) {
      if (error?.name === 'AbortError') return { status: 'stale' };
      return { status: confirmedFilesPolicyFailure(error) ? 'invalid' : 'unavailable', error };
    }
  }

  const fromClankerHome = snapshot.columns[0]?.name === 'Clanker home' && !snapshot.columns[0]?.resourceId;
  const priorColumns = snapshot.columns.filter(column => column.resourceId);
  if (!priorColumns.length) return { status: 'invalid' };
  const controller = new AbortController();
  state.contentController = controller;
  const contentGeneration = state.contentGeneration;
  const columns = [];
  try {
    let current;
    if (fromClankerHome) {
      // The first saved column is a Library child of the synthetic home, not
      // a provider root. Rebuild home first so its fresh child ref becomes
      // the anchor for the normal managed descent below.
      const homeResolved = await openClankerHome(controller, contentGeneration);
      if (!homeResolved || controller.signal.aborted || lifecycle !== state.lifecycleGeneration || !state.nativeWindow?.visible) {
        return { status: controller.signal.aborted ? 'stale' : 'invalid' };
      }
      const home = state.columns.at(-1);
      current = home?.entries?.find((entry) => entry.resource_id === priorColumns[0].resourceId) || null;
      if (!current) return { status: 'invalid' };
      home.activeName = current.name;
      columns.push(home);
    } else {
      current = freshManagedResource(priorColumns[0].resourceId, priorColumns[0].provider || snapshot.provider);
      if (!current) return { status: 'invalid' };
    }
    for (let index = 0; index < priorColumns.length; index += 1) {
      const prior = priorColumns[index];
      if (index > 0) {
        const parent = columns.at(-1);
        current = parent?.entries.find(entry => entry.resource_id === prior.resourceId) || null;
        let cursor = parent?.nextCursor;
        const seen = new Set();
        while (!current && cursor && !seen.has(cursor) && seen.size < 100) {
          seen.add(cursor);
          const page = await filesFacadeClient.children(parent.resourceRef, {
            cursor, sort:managedSortRequest(parent.sort), query:parent.query, signal:controller.signal,
          });
          if (controller.signal.aborted || lifecycle !== state.lifecycleGeneration || contentGeneration !== state.contentGeneration) return { status:'stale' };
          current = page.entries.map(facadeEntry).filter(editorResourceAllowed).find(entry => entry.resource_id === prior.resourceId);
          cursor = page.next_cursor;
        }
        if (!current) return { status: 'invalid' };
        parent.activeName = current.name;
      }
      const requestedSort = constrainSortSpec(prior.sort || snapshot.sort, prior.sortKeys || SORT_KEYS);
      const response = await filesFacadeClient.children(current.resource_ref, {
        sort: managedSortRequest(requestedSort),
        query: prior.query || '',
        signal: controller.signal,
      });
      if (
        controller.signal.aborted
        || lifecycle !== state.lifecycleGeneration
        || contentGeneration !== state.contentGeneration
        || !state.nativeWindow?.visible
      ) return { status: 'stale' };
      columns.push(managedColumn(current, response, requestedSort, prior.query || ''));
    }
    const active = columns.at(-1);
    if (!active) return { status: 'invalid' };
    commitManagedColumn(active, { provider: snapshot.provider, columns });
    return { status: 'resolved' };
  } catch (error) {
    if (error?.name === 'AbortError') return { status: 'stale' };
    return { status: confirmedFilesPolicyFailure(error) ? 'invalid' : 'unavailable', error };
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

async function handleFilesPolicyChanged(snapshot = captureFilesProjection()) {
  clearFilesReadRecovery();
  if (!state.nativeWindow?.visible) {
    abortFilesAuthorityRequests();
    clearFilesAuthorityContent('');
    suspendFilesNavigation();
    return false;
  }
  const lifecycle = abortFilesAuthorityRequests();
  clearFilesAuthorityContent('');
  suspendFilesNavigation();
  const startingContent = state.contentGeneration;
  const current = () => lifecycle === state.lifecycleGeneration && state.contentGeneration === startingContent && state.nativeWindow?.visible;
  setStatus('Revalidating file access…');
  try {
    const roots = await loadNavigationRoots({force:true,deferSidebar:true});
    if (!roots || !current()) return false;
    const first = snapshot.columns.find(column => column.resourceId);
    if (first && first.provider === 'host' && !freshManagedResource(first.resourceId, 'host')) {
      await roots.hostReady;
      if (!current()) return false;
      // Most Host anchors are in the location projection. A saved place or
      // workspace can also be the first anchor for a folder opened directly.
      if (!freshManagedResource(first.resourceId, 'host')) {
        const optionalErrors = await Promise.all([roots.placesReady, roots.workspacesReady]);
        if (!current()) return false;
        const error = readRecoveries.get('navigation')?.error || optionalErrors.find(Boolean);
        if (error && !freshManagedResource(first.resourceId, 'host')) {
          showFilesReadRetry('folder','the selected folder',error,()=>handleFilesPolicyChanged(snapshot));
          return false;
        }
      }
    }
  } catch (error) {
    if (error?.name !== 'AbortError' && current()) {
      showFilesReadRetry('roots','available Files sources',error,()=>handleFilesPolicyChanged(snapshot));
      setStatus(filesReadErrorMessage(error,'available Files sources'),true);
    }
    return false;
  }
  if (snapshot.owner && state.owner !== snapshot.owner) return openProvider(CLANKER_HOME_PROVIDER, {rootsConfirmed:true});
  if (!snapshot.columns.length && !snapshot.hostPath && snapshot.provider !== CLANKER_HOME_PROVIDER) {
    return openProvider(snapshot.provider || CLANKER_HOME_PROVIDER, {rootsConfirmed:true});
  }

  const result = snapshot.provider === 'host' && snapshot.hostPath
    ? await refreshRawFilesProjection(snapshot,lifecycle) : await refreshManagedFilesProjection(snapshot,lifecycle);
  if (lifecycle !== state.lifecycleGeneration || result.status === 'stale') return false;
  if (result.status === 'resolved') return true;
  if (result.error) {
    clearFilesAuthorityContent('',{catalogs:false});
    showFilesReadRetry('folder','the selected folder',result.error,()=>handleFilesPolicyChanged(snapshot));
    return false;
  }

  // An authoritative fresh read did not find the previous resource.
  clearFilesAuthorityContent('The previous folder is no longer available',{catalogs:false});
  const reopened = await openProvider(snapshot.provider || CLANKER_HOME_PROVIDER, {rootsConfirmed:true});
  if (reopened) setStatus('The previous folder is no longer available. Choose a folder from the current sources.',true);
  return false;
}

async function handleAuthUserReady(event) {
  const announcedOwner = String(event?.detail?.username || '').trim();
  const announcedAccount = String(event?.detail?.accountId || '');
  if (!state.owner || (announcedOwner === state.owner && (!announcedAccount || announcedAccount === state.accountId))) return;

  // Treat the event only as a revocation signal. The subsequent authenticated
  // server responses, not browser-supplied event detail, establish the new
  // owner and visible roots.
  clearFilesReadRecovery();
  resetFilesReadRetry();
  const lifecycle = ++state.lifecycleGeneration;
  cancelFilesGesture('account-change');
  abortFilesImports();
  clearKeepBothRetry();
  state.revealGeneration += 1;
  state.contentGeneration += 1;
  state.contentController?.abort();
  state.contentController = null;
  state.navigationController?.abort();
  state.navigationController = null;
  state.favoritesController?.abort();
  state.favoritesController = null;
  state.workspaceController?.abort();
  state.workspaceController = null;
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  closePreview();
  setEditorRoot(null,state.editorRootBound === true);
  destroyExplorerTree();
  state.owner = '';
  state.accountId = '';
  state.navigation?.reset({ account: '', workspace: copalWorkspace() });
  state.path = '';
  state.hostPath = '';
  state.entries = [];
  state.nextCursor = null;
  state.defaultPath = '';
  state.navigationGeneration = null;
  state.navigationRoots = [];
  state.managedRoots = [];
  state.favorites = [];
  state.workspaces = [];
  state.currentWorkspaceId = '';
  state.favoritePaths.clear();
  state.columns = [];
  state.selectionModels.clear();
  state.selected = new Set();
  state.treeSelection = null;
  options.onSelection?.(null);
  options.onDirectory?.(null);
  state.provider = 'host';
  state.searchQuery = '';
  state.viewPreferences = {};
  mountExplorerTree([]);
  renderFavorites();
  renderWorkspaces();
  renderEntries();
  const pathLabel = state.shell?.querySelector('[data-files-path]');
  if (pathLabel) pathLabel.textContent = '';
  const provider = state.shell?.querySelector('.files-provider-select');
  if (provider) provider.value = 'host';
  syncSearchControl();
  setStatus('Reloading available files…');

  if (!state.nativeWindow?.visible) return;
  try {
    await loadNavigationRoots({force:true,deferSidebar:true});
    if (lifecycle !== state.lifecycleGeneration || !state.nativeWindow?.visible) return;
    await openProvider(CLANKER_HOME_PROVIDER, {rootsConfirmed:true});
  } catch (error) {
    if (error?.name !== 'AbortError' && lifecycle === state.lifecycleGeneration) {
      showFilesReadRetry('roots','available Files sources',error,()=>handleFilesPolicyChanged());
      setStatus(filesReadErrorMessage(error,'available Files sources'),true);
    }
  }
}

function toggleNavigation() {
  state.navigationExpanded = !state.shell.classList.contains('files-navigation-open');
  syncNavigationLayout();
}

function syncNavigationLayout() {
  const shell = state.shell;
  if (!shell || options.navigationOnly) return;
  const pair = [...splits.values()].find(pair => pair.primary === api || pair.browser === api);
  const width = shell.getBoundingClientRect().width;
  shell.classList.toggle('files-compact', width > 0 && width < 600);
  const targetWidth = pair ? pair.primary.window.body.getBoundingClientRect().width : width;
  const open = state.navigationExpanded ?? (targetWidth >= (pair ? 700 : 600));
  shell.classList.toggle('files-navigation-open', Boolean(open));
  shell.classList.toggle('files-navigation-collapsed', !open);
  if (pair && activeBrowser === api) pair.navigation.hidden = !open;
  state.navigationToggle?.setAttribute('aria-expanded', String(Boolean(open)));
}

function mount() {
  if (state.nativeWindow) return state.nativeWindow;
  const navigation = state.navigation = createWindowNavigation({
    scope: { account: state.owner, workspace: copalWorkspace() }, restore: restoreFilesHistory,
    onChange: () => state.nativeWindow?.updateNavigationButtons?.(),
  });
  state.nativeWindow = options.container ? {
    id, root: options.container, body: options.container,
    get visible() { return !listeners.signal.aborted && (options.parentWindow ? options.parentWindow.visible : options.container.isConnected); },
    show() {}, requestClose() { api.dispose(); },
    setStatus(message, bad) { status.textContent = message || ''; status.classList.toggle('error', !!bad); },
    updateNavigationButtons() {}, navigation,
  } : createOpenClankWindow({
    id,
    label: 'Files',
    subtitle: 'Host and library',
    className: 'files-window',
    minWidth: 620,
    minHeight: 440,
    navigation,
    onActivate: () => { if (activeBrowser?.window !== state.nativeWindow && activeBrowser?.parentWindow !== state.nativeWindow) activate(); },
    onClosed: handleWindowClosed,
  });
  state.shell = el('div', { class: 'files-shell', 'data-files-browser-id': id });
  const status = el('div', { class: 'files-pane-status', role: 'status' });
  state.shell.addEventListener('pointerdown', event => { state.openIntent = event.altKey ? 'splitBelow' : event.shiftKey ? 'splitRight' : (event.metaKey || event.ctrlKey) ? 'newTab' : 'current'; activate(); }, true);
  listen(state.shell, 'keydown', event => { if (event.key === 'Enter') state.openIntent = event.altKey ? 'splitBelow' : event.shiftKey ? 'splitRight' : (event.metaKey || event.ctrlKey) ? 'newTab' : 'current'; }, {capture:true});
  state.shell.addEventListener('focusin', () => activate());
  const sidebar = state.sidebar = el('aside', { class: 'files-sidebar', 'aria-label': 'Files navigation', 'data-files-browser-id':id });
  sidebar.addEventListener('pointerdown',activate,true); sidebar.addEventListener('focusin',activate);
  const favoritesSection = el('section', { class: 'files-sidebar-section files-favorites-section' });
  favoritesSection.append(
    el('h2', { class: 'files-sidebar-heading', text: 'Favorites' }),
    el('div', { class: 'files-favorites', 'data-files-favorites': '' }),
  );
  const workspacesSection = el('section', { class: 'files-sidebar-section files-workspaces-section' });
  workspacesSection.append(
    el('h2', { class: 'files-sidebar-heading', text: 'Workspaces' }),
    el('div', { class: 'files-workspaces', 'data-files-workspaces': '' }),
  );
  const worktreeSection = el('section', { class: 'files-sidebar-section files-worktree-section' });
  worktreeSection.append(
    el('h2', { class: 'files-sidebar-heading', text: 'Locations' }),
    el('div', {
      class: 'files-tree',
      role: 'tree',
      'aria-label': 'Available host files and folders',
      'data-files-tree': '',
    }),
  );
  const collectionsSection = el('section', { class:'files-sidebar-section', 'data-files-category':'applications' },
    el('h2', {class:'files-sidebar-heading',text:'Library'}),
    el('div', {},
      el('div', {'data-files-collections':'',class:'files-tree',role:'tree','aria-label':'Library collections'})));
  favoritesSection.dataset.filesCategory='favorites';
  workspacesSection.dataset.filesCategory='workspaces';
  worktreeSection.dataset.filesCategory='locations';
  sidebar.append(favoritesSection, workspacesSection, worktreeSection);
  sidebar.append(collectionsSection);
  for (const section of sidebar.children) {
    const heading=section.querySelector('h2'), content=heading.nextElementSibling;
    const label=heading.textContent, categoryId=section.dataset.filesCategory;
    const toggle=el('button',{type:'button',class:'files-category-toggle','aria-expanded':'true',text:'▾ '+label}); heading.replaceChildren(toggle);
    explorerLayout.register({id:categoryId,label,group:'places',element:()=>section,content:()=>content,
      sync:record=>{toggle.setAttribute('aria-expanded',String(!record.collapsed));const text=(record.collapsed?'▸ ':'▾ ')+label;if(toggle.textContent!==text)toggle.textContent=text;},
    });
    toggle.addEventListener('click',()=>{const record=explorerLayout.records().find(item=>item.id===categoryId);explorerLayout.update(categoryId,{collapsed:!record.collapsed});});
    heading.dataset.filesExplorerHeading = '';
  }
  if (options.editorRootProjection) sidebar.prepend(el('div',{
    class:'files-tree','data-files-editor-root':'',hidden:true,
  }));
  categoryObserver?.disconnect();
  categoryObserver = new MutationObserver(()=>{syncCollectionLayout();explorerLayout.apply();});
  categoryObserver.observe(sidebar,{childList:true,subtree:true});

  if (options.navigationOnly) {
    const filter = el('input',{ type:'search', class:'files-folder-search', placeholder:'Search this folder', 'aria-label':'Search this folder', disabled:true });
    let searchTimer;
    const run = (expectedRef = activeColumn()?.resourceRef) => { clearTimeout(searchTimer); if (!expectedRef || expectedRef !== activeColumn()?.resourceRef || listeners.signal.aborted) return; void searchActive(filter.value); };
    filter.addEventListener('input', () => { clearTimeout(searchTimer); const expectedRef = activeColumn()?.resourceRef; searchTimer = setTimeout(() => run(expectedRef),120); });
    filter.addEventListener('search', () => run());
    filter.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); run(); } });
    listeners.signal.addEventListener('abort', () => clearTimeout(searchTimer), {once:true});
    sidebar.prepend(filter);
  }
  const separator = state.sidebarSeparator = el('div', { class: 'oc-explorer-separator', role: 'separator', tabindex: '0', 'aria-orientation': 'vertical', 'aria-label': 'Resize Files navigation' });

  const main = el('main', { class: 'files-main' });
  const toolbar = el('div', { class: 'files-toolbar' });
  const navigationToggle = state.navigationToggle = el('button', { type:'button', class:'files-navigation-toggle', text:'Navigation', 'aria-expanded':'true' });
  navigationToggle.addEventListener('click', toggleNavigation);
  toolbar.append(navigationToggle, iconButton({ glyph: 'back', title: 'Back in folder history', onClick: () => state.navigation.back() }),
    iconButton({ glyph: 'forward', title: 'Forward in folder history', onClick: () => state.navigation.forward() }),
    iconButton({ glyph: 'home', title: 'Home', onClick: () => openProvider(CLANKER_HOME_PROVIDER) }),
    iconButton({ glyph: 'refresh', title: 'Refresh folder', onClick: () => reloadManagedColumn(state.columns.length - 1) }));
  const up = iconButton({
    glyph: 'up',
    title: 'Parent folder',
    onClick: () => managedUp(),
  });
  const newButton = iconButton({ glyph: 'plus', text: 'New', title: 'Create a file or folder', onClick: () => showFilesNewMenu({ anchor: newButton }) });
  newButton.dataset.filesNew = '';
  const libraryCreate = iconButton({
    glyph: 'plus', text: 'Create', title: 'Create a Library document',
    onClick: () => createSelectedLibraryDocument(captureCommands({ context:'folder' })),
  });
  libraryCreate.dataset.filesLibraryCreate = '';
  libraryCreate.hidden = true;
  const libraryImport = iconButton({
    glyph: 'folder-plus', text: 'Import', title: 'Import Library documents',
    onClick: () => chooseLibraryDocuments(captureCommands({ context:'folder' })),
  });
  libraryImport.dataset.filesLibraryImport = '';
  libraryImport.hidden = true;
  const libraryInput = el('input', { type: 'file', multiple: 'true', hidden: 'true', 'data-files-library-input': '' });
  libraryInput.addEventListener('cancel', () => { state.libraryImportCapture = null; });
  libraryInput.addEventListener('change', () => { void (async () => {
    const snapshot = state.libraryImportCapture;
    state.libraryImportCapture = null;
    const current = () => isCommandCaptureCurrent(snapshot);
    const files = Array.from(libraryInput.files || []);
    libraryInput.value = '';
    if (options.pickerMode || !files.length || !current()) return;
    try {
      const actions = await import('./filesLibraryActions.js');
      if (!current()) return;
      const results = await actions.importLibraryDocuments(files);
      if (!current()) return;
      await reloadManagedColumn(snapshot.columnIndex);
      setStatus('Imported ' + Number(results?.imported || 0)
        + (results?.failed ? ' · ' + results.failed + ' failed' : ''));
    } catch (error) { if (current()) setStatus(error?.message || 'Library import failed', true); }
  })(); });
  const librarySelect = iconButton({
    glyph: 'check', text: 'Select', title: 'Select all Library documents',
    onClick: () => {
      const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
      if (selectedLibraryDocumentEntries().length) model.clear();
      else model.selectAll();
      applyModelSelection(model);
    },
  });
  librarySelect.dataset.filesLibrarySelect = '';
  librarySelect.hidden = true;
  const add = iconButton({glyph:'folder-plus',text:'Add folder',title:'Add host folder',onClick:addHostLocation});
  add.dataset.filesAddHost = '';
  add.hidden = true;
  add.disabled = true;
  const openButton = iconButton({ glyph: 'folder-open', text: 'Open', title: 'Open selected in source app', onClick: openSelected });
  const downloadButton = iconButton({ glyph: 'download', text: 'Download', title: 'Download selected', onClick: downloadSelected });
  const actionsButton = iconButton({ glyph: 'archive', text: 'Actions', title: 'Select one actionable managed item', onClick: openSelectedActions });
  actionsButton.dataset.filesActions = '';
  actionsButton.querySelector('span:not(.files-button-glyph)')?.classList.add('files-actions-label');
  actionsButton.disabled = true;
  const historyButton = iconButton({ glyph: 'restore', text: 'History', title: 'History for this folder', onClick: event => {
    const snapshot = captureCommands({ context: 'folder' });
    if (!fileResourceCanBrowseHostFolder(snapshot.directory)) {
      setStatus('Open a Host folder to see its History.', true);
      return;
    }
    openCapturedFolderHistory(snapshot, event.currentTarget);
  } });
  const favoriteButton = iconButton({ glyph: 'star', title: 'Add current folder to Favorites', onClick: () => toggleCurrentFavorite() });
  favoriteButton.classList.add('files-favorite-toggle');
  favoriteButton.dataset.filesFavoriteToggle = '';
  favoriteButton.setAttribute('aria-pressed', 'false');
  const provider = el('select', { class: 'files-provider-select', 'aria-label': 'Files source' });
  for (const [value, label] of [[CLANKER_HOME_PROVIDER, 'Clanker home'], ['host', 'Host locations'], ['all', 'All sources'], ['copal', 'Workspace collections'], ['files', 'Images'], ['library', 'Library']]) provider.append(el('option', { value, text: label }));
  provider.addEventListener('change', () => openProvider(provider.value));
  const searchInput = el('input', {
    type: 'search',
    class: 'files-search-input',
    placeholder: 'Search this folder',
    'aria-label': 'Search current Files folder',
    'data-files-search': '',
    autocomplete: 'off',
    spellcheck: 'false',
  });
  const runSearch = () => { void searchActive(searchInput.value); };
  searchInput.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter') return;
    event.preventDefault();
    runSearch();
  });
  searchInput.addEventListener('search', runSearch);
  const searchButton = iconButton({ glyph: 'search', title: 'Search current folder', onClick: runSearch });
  const searchGroup = el('div', { class: 'files-search-group', role: 'search' }, searchInput, searchButton);
  const modes = el('select', { class: 'files-mode-select', 'aria-label': 'File view mode' });
  for (const [value, label] of [['list', 'List'], ['grid', 'Grid'], ['details', 'Details'], ['columns', 'Columns'], ['gallery', 'Gallery']]) modes.append(el('option', { value, text: label }));
  modes.addEventListener('change', async () => {
    const previousMode = state.mode;
    state.mode = modes.value;
    if (state.mode !== 'columns') state.activeColumnIndex = -1;
    rememberViewPreferences();
    if (state.mode === 'columns' && state.provider === 'host' && state.hostPath && !state.columns.length) {
      state.columns = [{ path: state.hostPath, entries: state.entries, nextCursor: state.nextCursor, sort: normalizeSortSpec(state.sort), activeName: '' }];
      state.activeColumnIndex = 0;
    }
    if (previousMode === 'columns' && state.mode !== 'columns' && state.provider === 'host' && state.hostPath && !usesFacadeListing()) {
      setStatus('Refresh the authorized Files roots before changing views.', true);
      return;
    }
    renderEntries();
  });
  const sort = appendSortOptions(el('select', { class: 'files-sort-select', 'aria-label': 'File sort order' }));
  sort.value = `${state.sort.key}:${state.sort.direction}`;
  sort.addEventListener('change', async () => {
    const [key, direction] = sort.value.split(':');
    await applyPrimarySort({ ...state.sort, key, direction });
  });
  const foldersFirst = iconButton({
    glyph: 'folder',
    text: 'Folders first',
    title: 'Folders first is on',
    className: 'files-toolbar-button files-folders-first',
    onClick: () => { void applyPrimarySort({ ...state.sort, directoriesFirst: !state.sort.directoriesFirst }); },
  });
  foldersFirst.dataset.filesFoldersFirst = '';
  updateFoldersFirstControl(foldersFirst, state.sort);
  const previewButton = iconButton({ glyph: 'image', text: 'Preview', title: 'Preview selected', onClick: () => { const entry = state.entries.find(item => state.selected.has(entrySelectionKey(item))); if (entry) previewEntry(entry, entryPath(entry)); } });
  const operationShelf = el('div', { class: 'files-operation-shelf', hidden: 'true' }, newButton, libraryCreate, libraryImport, librarySelect, libraryInput, add, historyButton, openButton, downloadButton, previewButton, actionsButton, foldersFirst);
  toolbar.append(up, provider, favoriteButton, searchGroup, el('span', { class: 'files-toolbar-spacer' }), modes);
  const size = el('select', { 'aria-label': 'Icon size', class: 'files-size-select' });
  const updateSizes = () => { const large = ['grid', 'gallery'].includes(state.mode); size.replaceChildren(); for (const value of large ? [32,64,96,128] : [16,20,24,32]) size.append(el('option', { value, text: value + ' px' })); size.value = String(large ? state.largeIconSize : state.smallIconSize); };
  size.addEventListener('change', () => { const body = state.shell.querySelector('[data-files-body]'); const row = body.scrollTop / layoutMetrics().rowStride; if (['grid','gallery'].includes(state.mode)) state.largeIconSize = Number(size.value); else state.smallIconSize = Number(size.value); body.scrollTop = row * layoutMetrics().rowStride; rememberViewPreferences(); renderEntries(); });
  modes.addEventListener('change', updateSizes); modes.addEventListener('files-mode-sync', updateSizes); updateSizes();
  toolbar.append(el('label', { class: 'files-size-label', text: 'Icon size' }, size));
  main.append(operationShelf);
  if (!options.pickerMode) toolbar.append(iconButton({ glyph: 'more', title: 'File and view actions', onClick: () => { operationShelf.hidden = !operationShelf.hidden; } }));
  const pathLabel = el('div', { class: 'files-path', 'data-files-path': '', text: '/' });
  const searchNotice = el('div', {
    class: 'files-empty files-search-partial-notice',
    'data-files-search-notice': '',
    role: 'status',
    hidden: 'true',
    text: 'Some folders could not be searched, or search limits were reached. Results may be incomplete. Refine the query or search a narrower folder.',
  });
  const previewPanel = el('section', { class: 'files-preview', 'data-files-preview': '', hidden: 'true', 'aria-label': 'File preview' });
  const body = el('div', { class: 'files-browser-body files-mode-list', 'data-files-body': '', tabindex:'0', 'aria-label':'Files content' });
  installFilesGestures(body);
  main.append(toolbar, pathLabel, searchNotice, previewPanel, body);
  if (options.pickerMode) favoriteButton.hidden = true;
  const more = state.loadMoreButton = el('button', { type: 'button', class: 'files-content-more', hidden: true, text: 'Load more' });
  more.addEventListener('click', () => void loadManagedMore());
  main.append(more, status);
  if (options.navigationOnly) { state.shell.classList.add('files-navigation-only'); main.hidden = true; separator.hidden = true; }
  state.shell.append(sidebar, separator, main);
  registerFilesContextAdapters();
  state.nativeWindow.body.append(state.shell);
  state.pane = createResizablePane({ container: state.shell, sidebar, main, separator, cssVar: '--files-sidebar-width', storageKey: 'openclank-files-pane-width', defaultWidth: 220, minWidth: 170, maxWidth: 420, mainMin: 360, getOwner: () => state.owner });
  listen(window, 'modal-dismissed', (event) => {
    if (event.detail?.id === state.nativeWindow.id) state.nativeWindow.requestClose();
  });
  const retryVisibleReads = () => {
    if (!document.hidden && state.nativeWindow?.visible) {
      const kind = nextFilesReadRecovery({automatic:true});
      if (kind === 'watch') scheduleFilesReadRetry();
      else if (kind) void retryFilesRead(kind);
    }
  };
  listen(state.shell,'focusin',event=>{ if (!event.target.closest?.('.files-error')) retryVisibleReads(); });
  listen(window,'focus',retryVisibleReads);
  listen(window,'online',retryVisibleReads);
  let pausedWatchRef = '';
  listen(document,'visibilitychange',() => {
    if (document.hidden) {
      pausedWatchRef = state.watchResourceRef;
      if (pausedWatchRef) stopActiveFolderWatch();
      return;
    }
    const resume = pausedWatchRef && activeColumn()?.resourceRef === pausedWatchRef;
    pausedWatchRef = '';
    if (resume && state.nativeWindow?.visible) void reloadManagedColumn(state.columns.indexOf(activeColumn()));
    else if (nextFilesReadRecovery({automatic:true})) retryVisibleReads();
    else if (!nextFilesReadRecovery()) syncActiveFolderWatch();
  });
  listen(window,'odysseus:modal-restored',event=>{
    if (event.detail?.id === (options.parentWindow?.id || state.nativeWindow.id)) {
      if (nextFilesReadRecovery({automatic:true})) retryVisibleReads();
      else if (!nextFilesReadRecovery()) syncActiveFolderWatch();
    }
  });
  listen(window, 'odysseus:modal-minimized', (event) => {
    if (event.detail?.id === (options.parentWindow?.id || state.nativeWindow.id)) { cancelFilesGesture('window-hidden'); stopActiveFolderWatch(); resetNativeThumbnails(); }
  });
  listen(window, 'odysseus:modal-opened', (event) => {
    if (event.detail?.id === (options.parentWindow?.id || state.nativeWindow.id)) syncActiveFolderWatch();
    else if (event.detail?.id) cancelFilesGesture('modal-interruption');
  });
  listen(document, 'openclank:auth-user-ready', (event) => {
    void handleAuthUserReady(event);
  });
  listen(document, 'openclank:file-policy-changed', () => {
    resetFilesReadRetry();
    void handleFilesPolicyChanged();
  });
  listen(window, 'openclank:files-places-changed', event => {
    if (event.detail?.owner !== state.owner || event.detail?.source === id) return;
    const lifecycle = state.lifecycleGeneration;
    void filesFacadeClient.places({signal:listeners.signal}).then(response => { if (lifecycle !== state.lifecycleGeneration) return; return loadFavorites(state.favorites.filter(item => item.pinned), response.entries || []); }).catch(error => { if (error.name !== 'AbortError') setStatus('Favorites could not be refreshed.', true); });
  });
  listen(window, 'workspace-change', (event) => {
    const nextWorkspace = String(event?.detail?.workspaceId || '');
    const workspaceChanged = nextWorkspace !== state.currentWorkspaceId;
    if (workspaceChanged) {
      // Workspace identity is part of every selection scope and drag
      // authority envelope. Invalidate both the gesture and in-flight
      // result application before exposing the new workspace to Files.
      cancelFilesGesture('workspace-change');
      abortFilesAuthorityRequests();
    }
    state.currentWorkspaceId = nextWorkspace;
    renderWorkspaces();
    if (workspaceChanged) void handleFilesPolicyChanged();
  });
  state.shell.addEventListener('keydown', (event) => {
    const preview = state.shell?.querySelector('[data-files-preview]');
    if (event.key === 'Escape' && state.gesture) {
      event.preventDefault();
      event.stopPropagation();
      cancelFilesGesture('escape');
      return;
    }
    const inputTarget = event.target instanceof Element && event.target.closest('input, textarea, select, [contenteditable="true"]');
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'a' && !inputTarget) {
      if (options.pickerMode) return;
      const { model } = activateColumn(columnIndexForNode(event.target));
      model.selectAll();
      applyModelSelection(model);
      event.preventDefault();
      event.stopPropagation();
      setStatus(`${model.selectedKeys().length}${state.nextCursor ? '+' : ''} items selected`);
      return;
    }
    if (event.key === 'Escape' && preview && !preview.hidden) {
      event.preventDefault();
      // Keep preview dismissal inside the Files window.  Without stopping
      // propagation the global modal manager treats the same Escape as a
      // window-close request, hiding the explorer while its next navigation
      // is still in flight.
      event.stopPropagation();
      closePreview();
      return;
    }
    if (event.code !== 'Space' || event.defaultPrevented) return;
    if (event.target instanceof Element && event.target.closest('input, textarea, select, button, [contenteditable="true"]')) return;
    const entry = state.entries.find(item => state.selected.has(entrySelectionKey(item)));
    if (!entry || !isPreviewable(entry, entryPath(entry))) return;
    event.preventDefault();
    void previewEntry(entry, entryPath(entry));
  });
  browserResize = new ResizeObserver(() => { syncNavigationLayout(); if (state.nativeWindow?.visible && state.entries.length > 500) renderEntries(); });
  browserResize.observe(body);
  browserResize.observe(state.shell);
  if (!options.container) void import('./workbenchMenu.js').then(module => {
    if (listeners.signal.aborted || !module.mountWorkbenchMenu || localMenu) return;
    localMenu = module.mountWorkbenchMenu({ host: state.nativeWindow.root, container: state.nativeWindow.content, before: state.nativeWindow.body, id, kind: 'files', label: 'Files', surface: {
      kind: 'files',
      capture: () => { const current = activeBrowser && (activeBrowser.window === state.nativeWindow || activeBrowser.window?.root === state.nativeWindow.root || activeBrowser.parentWindow === state.nativeWindow) ? activeBrowser : api; return current.captureCommands(); },
      isCurrent: snapshot => snapshot.browser.isCommandCaptureCurrent(snapshot),
      commands: snapshot => snapshot.browser.commandDescriptors(snapshot),
    } });
    state.shell.querySelector('[aria-label="File and view actions"]')?.setAttribute('hidden','');
  });
  return state.nativeWindow;
}

async function open() {
  const windowApi = mount();
  windowApi.show();
  activate();
  if (!options.pickerMode) acknowledgeVisible(state.shell, 'surface.visited', {surface:'files',visited:true});
  registerFilesContextAdapters();
  const recovery = activeReadRetry || retryFilesRead(nextFilesReadRecovery());
  if (recovery) await recovery;
  else await handleFilesPolicyChanged();
  return windowApi;
}

/**
 * Open a Library collection through its managed Files reference. This is the
 * canonical handoff for the retired Documents tab; chats, research, and
 * archive retain their legacy exact readers while their migrations finish.
 */
async function openLibraryCollection(view = 'documents') {
  const requested = String(view || 'documents').trim().toLowerCase();
  const expectedView = LIBRARY_COLLECTION_VIEWS[requested] || LIBRARY_COLLECTION_VIEWS.documents;
  const windowApi = mount();
  windowApi.show();
  const roots = await loadNavigationRoots({deferSidebar:true});
  if (!roots || !windowApi.visible) return false;
  const opened = await openProvider(CLANKER_HOME_PROVIDER, {rootsConfirmed:true});
  if (!opened) return false;
  const collection = state.entries.find((entry) => libraryCollectionView(entry) === expectedView);
  if (!collection) {
    setStatus('This Library collection is unavailable.', true);
    return false;
  }
  return openManagedDirectory(collection, 0);
}

/** Reveal one Workspace-relative resource without receiving its host path. */
async function revealWorkspaceResource(workspaceId, relativePath = '') {
  const windowApi = mount();
  windowApi.show();
  cancelReveal();
  const reveal = state.revealGeneration;
  const revealController = state.revealController = new AbortController();
  setStatus('Locating Workspace resource…');
  try {
    const response = await filesFacadeClient.workspaceResource(workspaceId, relativePath, {signal:revealController.signal});
    if (reveal !== state.revealGeneration) return false;
    await loadNavigationRoots();
    if (reveal !== state.revealGeneration) return false;
    windowApi.show();
    const parent = facadeEntry(response?.parent);
    const target = facadeEntry(response?.resource);
    if (!parent.resource_ref || !isDirectory(parent) || !target.resource_id) {
      throw new Error('Workspace resource is unavailable');
    }
    const revealed = await filesFacadeClient.reveal(target.resource_ref || parent.resource_ref, {signal:revealController.signal});
    if (reveal !== state.revealGeneration) return false;
    await expandRevealTrees((revealed.ancestors || [revealed.parent]).filter(Boolean).map(facadeEntry).filter(editorResourceAllowed),target,reveal);
    const opened = await openManagedDirectory(parent, null, {reveal});
    if (!opened || reveal !== state.revealGeneration || !windowApi.visible) return false;
    if (target.resource_id !== parent.resource_id) {
      const listed = state.entries.find((entry) => entry.resource_id === target.resource_id);
      if (!listed) throw new Error('Workspace resource is no longer in this folder');
      const column = activeColumn();
      const model = clearFilesSelection(column, state.activeColumnIndex);
      model.select(entrySelectionKey(listed));
      state.selected = new Set(model.selectedKeys());
      if (column) column.activeName = listed.name;
      state.treeSelection = null; options.onSelection?.(listed);
      renderEntries();
      setStatus(`Shown in Files: ${listed.name}`);
    } else {
      setStatus(`Opened Workspace folder: ${target.name}`);
    }
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && reveal === state.revealGeneration) {
      setStatus(error.message || 'Workspace resource could not be shown', true);
    }
    return false;
  } finally { if (state.revealController === revealController) state.revealController = null; }
}

async function expandRevealTrees(ancestors, target, reveal) {
  const chain = [...ancestors];
  if (chain.at(-1)?.resource_id !== target.resource_id) chain.push(target);
  let chosen = null;
  const trees = state.editorRootBound ? [state.editorRootTree] : [state.workspaceTree, state.favoriteTree, state.explorerTree, state.collectionTree];
  // Prefer the deepest registered presentation root. Workspace/Favorites win
  // equal-depth ties; a physical ancestor is never expanded alongside it.
  for (const tree of trees) {
    if (!tree) continue;
    const snapshot = tree.snapshot();
    for (const id of snapshot.roots) {
      const node = snapshot.nodes.find(node => node.id === id);
      const index = chain.findIndex(resource => resource.resource_id === node?.data.resource_id);
      if (index >= 0 && (!chosen || index > chosen.index)) chosen = { tree, node, index };
    }
  }
  if (!chosen) {
    for (const tree of trees) {
      if (!tree) continue;
      for (const node of tree.snapshot().nodes) {
        const index = chain.findIndex(resource => resource.resource_id === node.data.resource_id);
        if (index >= 0 && (!chosen || index > chosen.index)) chosen = { tree, node, index };
      }
    }
  }
  if (state.editorRootBound && !chosen) return [];
  const start = chosen?.index || 0;
  if (chosen) {
    const { tree } = chosen;
    let node = chosen.node;
    for (let index = start; index < chain.length; index += 1) {
      if (reveal !== state.revealGeneration) return [];
      if (node.branch) await tree.expand(node.id);
      if (index === chain.length - 1) { tree.setActive(node.id); break; }
      const nextId = chain[index + 1].resource_id;
      let current = tree.getNode(node.id);
      let next = current?.children.map(id => tree.getNode(id)).find(child => child?.data.resource_id === nextId);
      // Page only this necessary branch, and only while the desired child is
      // absent. Use the controller's sealed cursor, never a path lookup.
      for (let page = 0; !next && current?.nextCursor && page < 100; page += 1) {
        if (reveal !== state.revealGeneration || !(await tree.loadMore(node.id))) return [];
        current = tree.getNode(node.id);
        next = current?.children.map(id => tree.getNode(id)).find(child => child?.data.resource_id === nextId);
      }
      if (!next) break;
      node = next;
    }
  }
  const navigation = chain.slice(start, chain.length - 1);
  return navigation.length ? navigation : isDirectory(target) ? [target] : ancestors;
}

/** Reveal an already-authorized provider resource in its canonical parent. */
async function revealResource(resourceRef, {expectedRevision = null, workspaceMatch = false, isCurrent = () => true} = {}) {
  if (!isCurrent() || (workspaceMatch && expectedRevision?.kind !== 'hostFingerprint')) return false;
  const matchesRevision = item => !expectedRevision || (item?.revision?.kind === expectedRevision.kind && item?.revision?.value === expectedRevision.value);
  const windowApi = mount();
  windowApi.show();
  Modals.restore(state.nativeWindow.id);
  cancelReveal();
  const reveal = state.revealGeneration;
  const revealController = state.revealController = new AbortController();
  setStatus('Locating resource…');
  try {
    const revealOptions = {
      signal: revealController.signal,
      expectedRevision: workspaceMatch ? expectedRevision : null,
    };
    let response;
    try {
      response = await filesFacadeClient.reveal(resourceRef, revealOptions);
    } catch (error) {
      if (expectedRevision || error?.code !== 'resource_ref_stale') throw error;
      const renewed = await filesFacadeClient.reissue(resourceRef, {signal:revealController.signal});
      const renewedRef = String(renewed?.resource?.ref || '');
      if (!renewedRef) throw new Error('Resource could not be renewed');
      response = await filesFacadeClient.reveal(renewedRef, {signal:revealController.signal});
    }
    if (reveal !== state.revealGeneration || !isCurrent()) return false;
    await loadNavigationRoots();
    if (reveal !== state.revealGeneration || !isCurrent()) return false;
    windowApi.show();
    const ancestors = (
      Array.isArray(response?.ancestors) && response.ancestors.length
        ? response.ancestors
        : [response?.parent]
    ).filter(Boolean).map(facadeEntry).filter(editorResourceAllowed);
    let parent = ancestors.at(-1);
    const target = facadeEntry(response?.resource);
    if (!matchesRevision(target)) throw new Error('The saved result changed.');
    if (
      !parent?.resource_ref
      || !target.resource_id
      || ancestors.some((ancestor) => !ancestor.resource_ref || !isDirectory(ancestor))
    ) {
      throw new Error('Resource is unavailable');
    }
    const navigation = await expandRevealTrees(ancestors,target,reveal);
    if (reveal !== state.revealGeneration || !isCurrent() || !navigation.length) return false;
    parent = navigation.at(-1);
    for (let index = 0; index < navigation.length; index += 1) {
      const opened = await openManagedDirectory(navigation[index], index === 0 ? null : index - 1, {reveal});
      if (!opened || reveal !== state.revealGeneration || !isCurrent() || !windowApi.visible) return false;
    }
    if (target.resource_id !== parent.resource_id) {
      const findTarget = () => state.entries.find((entry) => entry.resource_id === target.resource_id);
      let listed = findTarget();
      if (workspaceMatch) {
        const selectedFolder = activeColumn();
        for (let page = 0; !listed && state.nextCursor && page < 8; page += 1) {
          if (!await loadManagedMore() || reveal !== state.revealGeneration || !isCurrent() || activeColumn() !== selectedFolder) return false;
          listed = findTarget();
        }
        if (listed) {
          // Host listings omit byte fingerprints. Recheck the original sealed
          // match through Workspace policy before selecting the listing row.
          const verified = await filesFacadeClient.reveal(resourceRef, revealOptions);
          if (reveal !== state.revealGeneration || !isCurrent() || !windowApi.visible || activeColumn() !== selectedFolder) return false;
          listed = verified?.resource?.id === target.resource_id && matchesRevision(verified.resource) ? findTarget() : null;
        }
      }
      if (!listed || (!workspaceMatch && !matchesRevision(listed))) throw new Error('The saved result changed or is no longer in this folder.');
      const column = activeColumn();
      const model = clearFilesSelection(column, state.activeColumnIndex);
      model.select(entrySelectionKey(listed));
      state.selected = new Set(model.selectedKeys());
      if (column) column.activeName = listed.name;
      state.treeSelection = null; options.onSelection?.(listed);
      renderEntries();
      setStatus(`Shown in Files: ${listed.name}`);
    } else {
      setStatus(`Opened in Files: ${target.name}`);
    }
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && reveal === state.revealGeneration) {
      setStatus(error.message || 'Resource could not be shown', true);
    }
    return false;
  } finally { if (state.revealController === revealController) state.revealController = null; }
}

function registerFilesContextAdapters() {
  if (state.contextAdapterDispose || options.surface === 'editor') return;
  const adapter = sharedFilesMenuAdapter();
  const disposers = [registerAdapter(state.shell, adapter), registerAdapter(state.sidebar, adapter)];
  state.contextAdapterDispose = () => disposers.forEach(dispose => dispose());
}

function captureCommands({ context = '', columnIndex = state.mode === 'columns' ? state.activeColumnIndex : state.columns.length - 1,
  treeEntry = context === 'folder' || context === 'navigation' ? null : state.treeSelection || null, treeHit = null } = {}) {
  if (!state.columns[columnIndex]) columnIndex = state.columns.length - 1;
  const column = state.columns[columnIndex] || null;
  const treeParent = treeEntry?.tree_ancestors?.at(-1) || null;
  let model;
  if (treeEntry) {
    const scope = { owner:state.owner, workspace:state.currentWorkspaceId || copalWorkspace(),
      provider:treeEntry.provider, parentRef:treeParent?.resource_ref || '', query:'', column:'tree' };
    const identity = scopeKey(scope);
    model = state.selectionModels.get(identity);
    if (!model) { model = createFilesSelectionModel({scope}); state.selectionModels.set(identity, model); }
    model.setItems([treeEntry]);
    model.replaceSelection([resourceKey(treeEntry,scope.provider,scope)]);
  } else model = ensureSelectionModel(column, columnIndex);
  const selection = captureFilesSelectionGuard(model, columnIndex);
  const entries = context === 'folder' || context === 'navigation' ? [] : model.selectedEntries();
  const destination = commandDirectory(treeEntry, context, entries, column);
  const directory = destination ? { ...destination,kind:'folder',resourceRef:destinationResourceRef(destination), resourceId:destination.resource_id || destination.resourceId } : null;
  return Object.freeze({ browser: api, lifecycle: state.lifecycleGeneration, content: state.contentGeneration, navigating: Boolean(state.contentController),
    owner: state.owner, accountId: state.accountId, policy: filesPolicyGeneration(), selection, treeEntry, treeHit, context, mode:state.mode,
    column, columnIndex, entries: Object.freeze([...entries]), directory,
    clipboard: filesClipboard, sessionId: window.sessionModule?.getCurrentSessionId?.() || '',
    creation: destination ? creationDestinationForEntry(destination, columnIndex) : null });
}
function commandDirectory(treeEntry, context, entries, column) {
  if (context === 'selection' && entries.length === 1 && isDirectory(entries[0])) return entries[0];
  if (treeEntry) return isDirectory(treeEntry) ? treeEntry : treeEntry.tree_ancestors?.at(-1);
  return column;
}

function isCommandCaptureCurrent(snapshot, policy = snapshot?.policy, ownedController = null) {
  if(!snapshot)return false;
  const stillSelected = snapshot.context === 'folder' || snapshot.context === 'navigation' || snapshot.treeEntry === (state.treeSelection || null);
  const column = state.columns[snapshot.columnIndex] || null;
  const model = validateFilesSelectionGuard({ ...snapshot.selection, filesGeneration:policy });
  return !snapshot.navigating && (!state.contentController || state.contentController === ownedController)
    && column === snapshot.column && state.mode === snapshot.mode && snapshot.browser === api && !listeners.signal.aborted && state.nativeWindow?.visible
    && snapshot.lifecycle === state.lifecycleGeneration && snapshot.content === state.contentGeneration
    && snapshot.owner === state.owner && snapshot.accountId === state.accountId && policy === filesPolicyGeneration()
    && (!document.body?.dataset?.accountId || document.body.dataset.accountId === snapshot.accountId)
    && stillSelected && (snapshot.context !== 'selection' || snapshot.treeEntry || activeColumn() === snapshot.column)
    && (!snapshot.treeHit || snapshot.treeHit.tree.getNode(snapshot.treeHit.id)?.data === snapshot.treeEntry)
    && Boolean(model) && snapshot.entries.every((entry, index) => model.item(snapshot.selection.selectedKeys[index]) === entry);
}
async function openCapturedCodeGraph(snapshot) {
  let accepted = snapshot.policy;
  const current = generation => generation === accepted && isCommandCaptureCurrent(snapshot, accepted);
  const selected = snapshot.entries.length ? snapshot.entries : [snapshot.directory];
  const resource = selected[0];
  const capturedRoot=[...(resource?.tree_ancestors||[]),resource].find(entry=>entry?.workspaceRootId);
  const known = capturedRoot && state.workspaces.find(item => item.workspace.id === capturedRoot.workspaceRootId
    && String(item.resource?.id||item.resource?.resource_id||'')===String(capturedRoot.resource_id||capturedRoot.id||'')
    && item.availability==='available');
  const result = await executeCodeGraphNavigation({resource,generation:accepted,
    knownWorkspace:known ? known.workspace : null,client:filesFacadeClient,isCurrent:current,
    confirmParent:({name})=>uiModule.styledConfirm(`Open the authorized containing folder “${name}” in Code Graph? This does not change chat workspace or enable automatic context.`,{title:CODE_GRAPH_LABEL,confirmText:'Open folder',cancelText:'Cancel'}),
    acceptGeneration:({before,after})=>{
      if(before!==accepted||!isCommandCaptureCurrent(snapshot,accepted))return false;
      state.navigationGeneration=after;accepted=after;return current(after);
    },
    handoff:async(binding,options)=>{const copal=window.copalModule||await import('./copal.js');if(!options.isCurrent())return false;return copal.openCodeGraphBinding(binding,options);},
  });
  if(current(accepted))setStatus(result.message,!result.ok);
  if(result.ok)document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  return result;
}
function contextCommandDescriptors(snapshot) {
  const descriptors = new Map(commandDescriptors(snapshot).filter(item => !item.disabledReason).map(item => [item.id, item]));
  const items = [];
  const include = (names, menu = '', useChoiceLabel = false) => {
    for (const name of names) {
      const item = descriptors.get('files.' + name);
      if (item) items.push({ ...item, menu, label: useChoiceLabel ? item.choiceLabel || item.label : item.label });
    }
  };

  if (snapshot.context === 'navigation') {
    if (!options.pickerMode) include(['customize-explorer']);
    return items;
  }
  if (snapshot.context === 'folder') {
    include(['new-file', 'new-folder', 'library-create', 'library-import'], 'New');
    include(['paste', 'select-all', 'refresh', 'favorite', 'code-graph', 'folder-history', 'git-workbench']);
    include([...FILE_SORT_OPTIONS.map(([value]) => 'sort-' + value), 'folders-first'], 'Sort by', true);
    include(['view-list', 'view-grid', 'view-details', 'view-columns', 'view-gallery'], 'View mode');
    include(['size-16', 'size-20', 'size-24', 'size-32', 'size-64', 'size-96', 'size-128'], 'Icon size', true);
    return items;
  }

  if (snapshot.entries.length > 1) {
    include(['copy', 'cut', 'download', 'action-transfer.duplicate', 'selected-output-export']);
    include(['action-transfer.copy', 'action-transfer.move'], 'Transfer');
    include(['action-history.checkpoint'], 'History');
    include(['action-archive.set', 'action-favorite.set', 'action-library.clone', 'action-library.delete']);
    return items;
  }

  const [entry] = snapshot.entries;
  if (!entry) return items;
  if (options.pickerMode) {
    include(['open']);
    return items;
  }
  include(['open', 'preview']);
  include(['open-editor', 'open-editor-right', 'open-editor-below', 'open-browser', 'open-in-canvas',
    'action-host.default', 'action-host.open', 'code-graph'], 'Open with');
  if (isDirectory(entry)) {
    include(['new-file', 'new-folder', 'library-create', 'library-import'], 'New');
    include(['paste', 'favorite', 'folder-history', 'git-workbench']);
    include(['workspace-use', 'workspace-rename', 'workspace-archive', 'workspace-hexes',
      ...(!descriptors.has('files.workspace-use') ? ['action-workspace.use'] : [])], 'Workspace');
  }
  include(['copy', 'cut', 'copy-path', 'action-rename', 'action-transfer.duplicate', 'download']);
  include(['action-transfer.copy', 'action-transfer.move'], 'Transfer');
  include(['manage-file-access', 'share-download'], 'Share');
  include(['action-history.open', 'action-history.checkpoint'], 'History');
  include(['action-analysis.note', 'action-analysis.result', 'selected-output-export'], 'Analysis');
  include(['action-media.spriteCrops', 'action-media.materialBake', 'action-media.blender',
    'action-media.blenderFour', 'action-media.blenderRig'], 'Media');
  include(['inspect-binary', 'inspect-model-header'], 'Inspect');
  include(['action-favorite.set', 'action-archive.set', 'action-restore', 'action-trash',
    'action-library.clone', 'action-library.delete', 'information']);
  return items;
}

function showCapturedFilesMenu(anchor, snapshot, names = null) {
  if (!snapshot) return false;
  const items = names
    ? commandDescriptors(snapshot).filter(item => names.includes(item.id) && !item.disabledReason).map(item => ({ ...item, menu:'' }))
    : contextCommandDescriptors(snapshot);
  return openContextActionMenu({ anchor, items, isCurrent:() => isCommandCaptureCurrent(snapshot) });
}

function commandDescriptors(snapshot) {
  const descriptors = [];
  const add = (name, label, menu, run, reason = '', checked = undefined, presentation = {}) => descriptors.push({
    id:'files.' + name, label, menu, run,
    disabledReason:snapshot.navigating ? 'Wait for folder navigation to finish; reopen this menu.' : reason,
    checked, ...presentation,
  });
  const entries = snapshot.entries;
  const single = entries.length === 1 ? entries[0] : null;
  const requiredSingle = single ? '' : 'Select one item.';
  const current = () => isCommandCaptureCurrent(snapshot);
  const sameChat = () => snapshot.sessionId === (window.sessionModule?.getCurrentSessionId?.() || '');
  const source = snapshot.column;
  const directory = snapshot.directory;
  add('shared-with-me', 'Shared with me…', 'File', async () => {
    const owner=state.owner, lifecycle=state.lifecycleGeneration, account=document.body.dataset.accountId;
    const current=()=>!!account && document.body.dataset.accountId===account && state.owner===owner && state.lifecycleGeneration===lifecycle;
    if(!current())return;
    try {
      const [{openSharedResources},copal]=await Promise.all([import('./sharedResources.js'),import('./copal.js')]);
      if(current())openSharedResources({isCurrent:current,openSharedText:copal.openSharedText});
    } catch(error) {if(current())setStatus(error?.message || 'Shared files unavailable.',true);}
  });
  const graphEntries = entries.length ? entries : snapshot.directory ? [snapshot.directory] : [];
  add('code-graph', CODE_GRAPH_LABEL, 'Go', () => openCapturedCodeGraph(snapshot),
    graphEntries.length === 1 && fileResourceCanOpenCodeGraph(graphEntries[0]) ? '' :
      graphDisabledReason(graphEntries) || 'Select an available Host source file or folder.');
  const workspaceItem=single?.workspaceRootId && state.workspaces.find(item=>item.workspace.id===single.workspaceRootId);
  if(workspaceItem){
    const still=()=>isCommandCaptureCurrent(snapshot)&&state.workspaces.includes(workspaceItem);
    add('workspace-use','Use as workspace','File',()=>still()&&useWorkspace(workspaceItem.workspace.id),single.unavailable?'This Workspace is unavailable.':'');
    add('workspace-rename','Rename workspace','File',()=>renameWorkspace(workspaceItem,still));
    add('workspace-archive','Archive workspace','File',()=>archiveWorkspace(workspaceItem,still));
    add('workspace-hexes','Open Workspace Hexes','File',()=>window.dispatchEvent(new CustomEvent('hex-open',{detail:Object.freeze({workspaceId:workspaceItem.workspace.id})})),single.unavailable?'This Workspace is unavailable.':'');
  }
  for(const [name,label,intent]of [['open-editor','Open in Editor',null],['open-editor-right','Open right','splitRight'],['open-editor-below','Open below','splitBelow']]){
    add(name,label,'File',async()=>{
      const editor=window.copalModule||await import('./copal.js');if(!isCommandCaptureCurrent(snapshot))return false;
      await (editor.default||editor).open('notes');if(!isCommandCaptureCurrent(snapshot))return false;
      const opener=window.__openClankOpenResourceHandle;
      if(typeof opener!=='function')return false;
      return opener({resourceRef:single.resource_ref,name:single.name,resourceKey:entrySelectionKey(single),...(intent?{intent}:{}),isCurrent:()=>isCommandCaptureCurrent(snapshot)});
    }, requiredSingle || (!fileResourceCanOpenInEditor(single) ? 'Select an authorized source or text file.' : ''));
  }
  add('reveal','Reveal in Files','Go',() => revealResource(single.resource_ref), requiredSingle || (!fileResourceCanReveal(single) ? 'This resource cannot be revealed.' : ''));
  add('copy-path','Copy file path','Edit',() => fileResourceCopyPath(single), requiredSingle || (!fileResourceCanCopyPath(single) ? 'No authorized path or clipboard is available.' : ''));
  add('new-file', 'New file…', 'File', () => createFilesResource('file', snapshot.creation, current), snapshot.creation ? '' : 'This folder does not support creating files.');
  add('new-folder', 'New folder…', 'File', () => createFilesResource('folder', snapshot.creation, current), snapshot.creation && snapshot.creation.creationSupportsFolders !== false ? '' : 'This folder does not support creating folders.');
  if (!options.pickerMode && isLibraryDocumentsView(directory) && directory.resourceRef === destinationResourceRef(source)) {
    add('library-create', 'Create Library document', 'File', () => createSelectedLibraryDocument(snapshot));
    add('library-import', 'Import Library documents…', 'File', () => chooseLibraryDocuments(snapshot));
  }
  if (!options.pickerMode) add('recover-history-import', 'Recover previous history import…', 'File', async () => {
    const owner=state.owner,lifecycle=state.lifecycleGeneration,policy=filesPolicyGeneration(),chatID=String(window.sessionModule?.getCurrentSessionId?.()||'');
    const current=()=>state.owner===owner&&state.lifecycleGeneration===lifecycle&&filesPolicyGeneration()===policy&&String(window.sessionModule?.getCurrentSessionId?.()||'')===chatID;
    try {const {recoverSessionTransfer}=await import('./sessionTransfer.js');if(current())await recoverSessionTransfer({client:filesFacadeClient,current,scopeRoot:state.nativeWindow?.root,status:(message,error=false)=>setStatus(message,error)});}
    catch(error){if(current())setStatus(String(error?.message||'Recovery unavailable.'),true);}
  });
  add('open', options.pickerMode && single && !isDirectory(single) ? 'Choose' : 'Open', 'File', () => isDirectory(single)
    ? openManagedDirectory(single, snapshot.treeEntry ? null : snapshot.columnIndex)
    : options.pickerMode ? confirmPickerEntry(single) : openManagedEntry(single, current),
  requiredSingle || (!fileResourceCanOpen(single) ? 'This provider cannot open this item.' : ''));
  add('download', entries.length > 1 ? 'Download selected files' : 'Download', 'File', async () => {
    for (const entry of entries) {
      if (!current()) return false;
      await downloadEntry(entry);
    }
    return true;
  }, entries.length && entries.every(entry => !entry.unavailable && entry.resource_ref && entry.capabilities?.includes('download')) ? '' : 'Select downloadable files.');
  if (!options.pickerMode) add('selected-output-export','Review selected outputs…','File',async()=>{
    const current=()=>isCommandCaptureCurrent(snapshot); const {reviewSelectedOutputs}=await import('./selectedOutputExport.js');
    if(current()) await reviewSelectedOutputs({entries,client:filesFacadeClient,current,scopeRoot:state.nativeWindow?.root});
  }, entries.length>=1 && entries.length<=16 && entries.every(entry=>entry.provider==='host' && !isDirectory(entry) && entry.resource_ref && entry.resource_id && entry.revision && entry.capabilities?.includes('stat') && entry.capabilities?.includes('download')) ? '' : 'Select one to sixteen current downloadable Host files.');
  add('manage-file-access', 'Invite / manage access…', 'File', async () => {
    const current=()=>isCommandCaptureCurrent(snapshot);
    const owner=state.owner,lifecycle=state.lifecycleGeneration,account=document.body.dataset.accountId;
    const sameHuman=()=>!!account && document.body.dataset.accountId===account && state.owner===owner && state.lifecycleGeneration===lifecycle;
    const {openFilesAccess}=await import('./filesShare.js');
    if(!current() || !sameHuman())return false;
    return openFilesAccess({entry:single,client:filesFacadeClient,current,sameHuman,status:setStatus,onChanged:()=>document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'))});
  }, requiredSingle || (!fileResourceCanShare(single) ? 'Select one current downloadable Host file.' : ''));
  add('share-download', 'Share download…', 'File', async () => {
    const current = () => isCommandCaptureCurrent(snapshot);
    const { openFilesShare } = await import('./filesShare.js');
    if (!current()) return false;
    return openFilesShare({entry:single,client:filesFacadeClient,current,ui:uiModule,status:setStatus});
  }, requiredSingle || (!fileResourceCanShare(single) ? 'Select one current downloadable Host file.' : ''));
  add('manage-shared-downloads', 'Manage shared downloads…', 'File', async () => {
    const current = () => isCommandCaptureCurrent(snapshot);
    const { openFilesShareManager } = await import('./filesShare.js');
    if (!current()) return false;
    return openFilesShareManager({current,ui:uiModule,status:setStatus});
  });
  add('information', 'Resource information', 'File', () => uiModule.styledConfirm([single.name, 'Type: ' + (single.kind || single.media_type || 'File'), 'Provider: ' + (single.provider || state.provider), ...(single.size != null ? ['Size: ' + formatBytes(single.size)] : []), ...(single.modified_unix_ms ? ['Modified: ' + formatModified(single.modified_unix_ms)] : [])].join('\n'), { title:'Resource information',confirmText:'Done',cancelText:'Close' }), requiredSingle || (single?.unavailable ? 'This item is unavailable.' : ''));
  const binaryFormats = ['elf', 'pe', 'macho', 'sqlite'];
  const recognizedBinary = binaryFormats.includes(single?.format?.formatId) ||
    /\.(?:exe|dll|so(?:\.\d+)*|dylib|elf|sqlite3?|db)$/i.test(single?.name || '');
  if (recognizedBinary && !single?.unavailable && single?.resource_ref && single?.provider === 'host' && single?.kind === 'file' && single?.capabilities?.includes('preview')) add('inspect-binary', 'Inspect binary headers…', 'File', async () => {
    const owner=state.owner, lifecycle=state.lifecycleGeneration, policy=filesPolicyGeneration(), workspace=copalWorkspace();
    const current=()=>isCommandCaptureCurrent(snapshot) && state.owner===owner && state.lifecycleGeneration===lifecycle && filesPolicyGeneration()===policy && copalWorkspace()===workspace;
    try {
      const selected=await uiModule.styledPrompt('Choose ELF, PE, Mach-O or SQLite.', {title:'Inspect metadata',defaultValue:single?.format?.formatId === 'pe' ? 'PE' : single?.format?.formatId === 'macho' ? 'Mach-O' : single?.format?.formatId === 'sqlite' ? 'SQLite' : 'ELF'});
      if (!selected || !current()) return;
      const format=selected.trim().toLowerCase().replace('-', '');
      if (!['elf','pe','macho','sqlite'].includes(format)) throw new Error('Choose ELF, PE, Mach-O or SQLite.');
      const fresh=await filesFacadeClient.stat(single.resource_ref, {purpose:`inspect_${format}`});
      if (!current() || fresh?.id!==single.resource_id || !fresh.revision) throw new Error('The inspection source changed.');
      const result=await filesFacadeClient.inspectModelHeader(fresh.ref, fresh.revision, {format});
      if (current()) await uiModule.styledConfirm(metadataDetails(result, fresh.ref, fresh.revision, policy, format), {title:'Read-only inspection',confirmText:'Done',cancelText:'Close'});
    } catch (error) { if (current()) setStatus(error.message || 'ELF inspection unavailable.', true); }
  }, requiredSingle);
  if (!single?.unavailable && single?.resource_ref && isMetadataInspection(single?.format) && !binaryFormats.includes(single.format.formatId)) add('inspect-model-header', `Inspect ${metadataLabel(single.format.formatId)}`, 'File', async () => {
    const owner=state.owner, lifecycle=state.lifecycleGeneration, policy=filesPolicyGeneration(), workspace=copalWorkspace();
    const current=()=>isCommandCaptureCurrent(snapshot) && state.owner===owner && state.lifecycleGeneration===lifecycle && filesPolicyGeneration()===policy && copalWorkspace()===workspace;
    try {
      const fresh=await filesFacadeClient.stat(single.resource_ref, {purpose:'inspect'});
      if (!current() || fresh?.id!==single.resource_id || fresh?.format?.formatId!==single.format.formatId || !fresh.revision) throw new Error('The inspection source changed.');
      const result=await filesFacadeClient.inspectModelHeader(fresh.ref, fresh.revision, {format:fresh.format.formatId});
      if (!current()) return;
      await uiModule.styledConfirm(metadataDetails(result, fresh.ref, fresh.revision, policy, fresh.format.formatId), {title:'Read-only inspection',confirmText:'Done',cancelText:'Close'});
    } catch (error) { if (current()) setStatus(error.message || 'Inspection unavailable.', true); }
  }, requiredSingle);
  add('customize-explorer', 'Customize Explorer…', 'View', customizeExplorer);
  add('preview', 'Preview', 'View', () => previewEntry(single, entryPath(single), current),
    requiredSingle || (!single?.unavailable && !isDirectory(single) && single?.capabilities?.includes('preview') && isPreviewable(single, entryPath(single))
      ? '' : 'No preview is available.'));
  const browserMime = previewMime(single);
  const browserMimeAllowed = browserMime === 'text/html' || browserMime === 'application/xhtml+xml';
  const browserCapabilitiesAllowed = !single?.unavailable && single?.resource_ref && single.capabilities?.includes('preview') && single.capabilities?.includes('stat') && window.browserApplet?.openPreview;
  add('open-browser', 'Open in Browser', 'File', async () => {
    const isCurrent = () => isCommandCaptureCurrent(snapshot);
    if (!isCurrent()) return false;
    const dependencies = [];
    const dependencyWarnings = [];
    const root = snapshot.directory;
    const rootRef = String(root?.resourceRef || '');
    const treeParentRef = String(snapshot.treeEntry?.tree_ancestors?.at(-1)?.resource_ref || '');
    const directChild = isDirectPreviewChild({ folderRef:rootRef, entryParentRef:treeParentRef,
      treeEntry:Boolean(snapshot.treeEntry), directory:isDirectory(single) || Boolean(root?.globalSearch) });
    if (rootRef && root.capabilities?.includes('children') && directChild) {
      const guard = createDependencyAcquisitionGuard({ isCurrent });
      try {
        const readText = async resourceRef => {
          if (!isCurrent()) throw new DOMException('Files selection changed.', 'AbortError');
          const response = await fetch(filesFacadeClient.contentUrl(resourceRef, { purpose:'preview' }), {
            credentials:'same-origin', cache:'no-store', signal:guard.signal,
          });
          if (!response.ok) throw new Error(`Files content read failed (${response.status}).`);
          const bounded = await readBoundedTextResponse(response, { limit:512 * 1024, signal:guard.signal });
          if (bounded.truncated) throw new Error('A linked stylesheet is larger than the bounded dependency scan.');
          if (!isCurrent()) throw new DOMException('Files selection changed.', 'AbortError');
          return bounded.text;
        };
        const response = await fetch(filesFacadeClient.contentUrl(single.resource_ref, { purpose:'preview' }), {
          credentials:'same-origin', cache:'no-store', signal:guard.signal,
        });
        if (!response.ok) throw new Error(`HTML source read failed (${response.status}).`);
        const bounded = await readBoundedTextResponse(response, { limit:512 * 1024, signal:guard.signal });
        if (!isCurrent()) throw new DOMException('Files selection changed.', 'AbortError');
        if (bounded.truncated) {
          dependencyWarnings.push('Linked resources were not acquired because the HTML exceeds the bounded dependency scan.');
        } else {
          const acquired = await acquirePreviewDependencies({ html:bounded.text, rootRef:root.resourceRef,
            client:filesFacadeClient, readText, signal:guard.signal, isCurrent });
          dependencies.push(...acquired.dependencies);
          dependencyWarnings.push(...acquired.warnings);
        }
      } catch (error) {
        if (!isCurrent()) return false;
        if (guard.timedOut) dependencyWarnings.push('Dependency acquisition timed out; only self-contained content may work.');
        else if (error?.name === 'AbortError') return false;
        else dependencyWarnings.push('Linked resources could not be acquired through Files; only self-contained content may work.');
      } finally {
        guard.dispose();
      }
    } else {
      dependencyWarnings.push(rootRef
        ? 'Linked resources were not acquired because the selected HTML is not a direct child of the captured Files folder.'
        : 'Linked resources were not acquired because the selected Files folder cannot be traversed.');
    }
    if (!isCurrent()) return false;
    return window.browserApplet?.openPreview({
      resourceRef: single.resource_ref, revision: single.revision || single.stat_revision || null,
      dependencies, dependencyWarnings,
      title: single.name || 'HTML preview', accountScope: snapshot.owner,
      workspaceScope: single.workspace_id || single.workspaceId || state.currentWorkspaceId || copalWorkspace(),
      isCurrent,
    }) || false;
  }, requiredSingle || (isDirectory(single) || !browserMimeAllowed || !browserCapabilitiesAllowed
    ? 'Select an HTML file with authorized preview and stat access.' : ''));
  const caps = entries.map(transferCapabilities);
  const selectionParentRef = validateFilesSelectionGuard(snapshot.selection)?.scope.parentRef;
  const transferable = entries.length && entries.length <= FILES_MAX_DRAG_ITEMS
    && entries.every(entry => !entry.unavailable && entry.resource_ref && entry.provider === entries[0].provider);
  for (const [kind,label,bulkLabel] of [['copy','Copy','Copy selected items'],['move','Cut','Cut selected items']]) add(kind === 'move' ? 'cut' : kind,
    entries.length > 1 ? bulkLabel : label, 'Edit', () => {
    const model = validateFilesSelectionGuard(snapshot.selection);
    return setFilesClipboard(model, kind, current);
  }, !selectionParentRef ? 'This resource has no authorized parent; open its containing folder first.'
    : transferable && caps.every(cap => cap[kind]) ? '' : `Select up to ${FILES_MAX_DRAG_ITEMS} transferable items.`);
  const destination = directory ? { ...directory, resource_ref:directory.resourceRef } : null;
  const writable = Boolean(destination?.resource_ref && !destination.unavailable && destination.capabilities?.includes('children') && destination.capabilities.includes('write'));
  const clipboard = snapshot.clipboard;
  const paste = writable && clipboard === filesClipboard && clipboard?.current() && validateDropTarget(clipboard.payload, {
    resource_ref:destination.resource_ref, resource_key:entrySelectionKey(destination),
  }, {
    policyGeneration:snapshot.policy, owner:snapshot.owner, workspace:snapshot.selection?.filesWorkspace,
    provider:destination.provider, allowCopy:writable, allowMove:writable,
  }).ok;
  add('paste', 'Paste into this folder', 'Edit', () => pasteFilesClipboard(destination,
    destinationColumnIndex(destination, snapshot.columnIndex), current, clipboard),
  paste ? '' : 'Copy or cut files from the same provider into a writable folder.');
  add('select-all', 'Select all', 'Edit', () => {
    const { model } = activateColumn(snapshot.columnIndex);
    model.selectAll();
    applyModelSelection(model);
  }, source?.entries?.length || (!source && state.entries.length) ? '' : 'This folder has no loaded items.');
  const choices = single ? entryActionChoices(single) : commonActionChoices(entries);
  const groupLabels = {
    'archive.set':value => value ? 'Archive selected items' : 'Restore selected items',
    'favorite.set':value => value ? 'Add selected items to favorites' : 'Remove selected items from favorites',
    'transfer.duplicate':() => 'Duplicate selected files',
    'library.clone':() => 'Clone selected documents',
    'library.delete':() => 'Delete selected documents…',
  };
  for (const choice of choices) {
    const action = choice.action;
    if (action === 'code.open') continue;
    if (action === 'workspace.use' && workspaceItem) continue;
    const transfer = action.startsWith('transfer.');
    const checkpoint = action === 'history.checkpoint';
    const chatAction = action === 'analysis.result' || action.startsWith('media.') || action === 'library.clone';
    const label = transfer && action !== 'transfer.duplicate' ? `${choice.label} into this folder…`
      : entries.length > 1 && groupLabels[action] ? groupLabels[action](choice.value) : choice.label;
    const needsDestination = transfer && (!source?.resourceRef || !source.capabilities?.includes('write') || !source.capabilities.includes('children')
      || entries.some(entry => entry.provider !== source.provider) || snapshot.treeEntry);
    const sameParent = source?.resourceRef && selectionParentRef === source.resourceRef;
    const reason = chatAction && (!snapshot.sessionId || !sameChat()) ? 'Open a chat before using this action.'
      : needsDestination ? 'Open a writable folder in this provider first.'
      : transfer && action !== 'transfer.duplicate' && sameParent ? 'Choose another writable destination folder.'
      : action === 'transfer.duplicate' && !sameParent ? 'Open the selected files’ containing folder to duplicate them.'
      : checkpoint && entries.length > 16 ? 'Select up to sixteen Host files.'
      : (checkpoint || action === 'history.open') && snapshot.treeEntry ? 'Select the file in its containing folder to use History.'
      : '';
    add('action-' + action, label, ['rename','trash','restore','archive.set','library.delete'].includes(action) || transfer ? 'Edit' : 'File', () => {
      if (chatAction && !sameChat()) return false;
      return entries.length > 1 || transfer ? performSelectedBulkAction(entries, choice, snapshot) : performManagedAction(single, choice, snapshot);
    }, reason, undefined, { icon:choice.glyph, destructive:choice.danger || action === 'trash' });
  }
  if (!options.pickerMode) {
    const mediaCommands = [
      ['media.jobs', 'Media jobs…'], ['media.generate', 'Create media…'],
      ['media.materialPack', 'Package material maps…'], ['media.spriteSheet', 'Create sprite sheet…'],
      ...(state.importCapabilities?.screen_record ? [['media.record', 'Record screen…']] : []),
    ];
    for (const [action, label] of mediaCommands) add('action-' + action, label, 'File',
      () => sameChat() && performManagedAction(null, { action, label }, snapshot),
      action === 'media.jobs' || snapshot.sessionId ? '' : 'Open a chat before using this action.');
  }
  if(single&&!single.unavailable&&isCanvasImageEntry(single))add('open-in-canvas','Open in Canvas','File',()=>{
    const model=validateFilesSelectionGuard(snapshot.selection);return model&&openEntryInCanvas(single,model);
  });
  for (const mode of ['list','grid','details','columns','gallery']) add('view-' + mode, mode[0].toUpperCase() + mode.slice(1), 'View', () => { const control = state.shell.querySelector('.files-mode-select'); control.value = mode; control.dispatchEvent(new Event('change')); }, '', state.mode === mode);
  const sort = normalizeSortSpec(source?.sort || state.sort);
  const sortKeys = sortKeysFor(source?.sortKeys);
  const applySort = spec => state.mode === 'columns' && source
    ? applyColumnSort(snapshot.columnIndex, spec) : applyPrimarySort(spec);
  const sortUnavailable = source?.nextCursor && !source.resourceRef && !source.globalSearch;
  for (const [value,label] of FILE_SORT_OPTIONS) {
    const [key,direction] = value.split(':');
    add('sort-' + value, 'Sort: ' + label, 'View', () => applySort({ ...sort, key, direction }),
      !sortUnavailable && sortKeys.includes(key) ? '' : 'This folder does not expose that metadata for sorting',
      sort.key === key && sort.direction === direction, { choiceLabel:label, radio:true });
  }
  add('folders-first', 'Folders first', 'View', () => applySort({ ...sort, directoriesFirst:!sort.directoriesFirst }),
    sortUnavailable ? 'Refresh the authorized Files roots before sorting this folder.' : '', sort.directoriesFirst);
  const largeIcons = ['grid','gallery'].includes(state.mode);
  if (state.mode !== 'columns') for (const value of largeIcons ? [32,64,96,128] : [16,20,24,32]) {
    add('size-' + value, 'Icon size: ' + value + ' px', 'View', () => {
      const size = state.shell.querySelector('.files-size-select');
      size.value = value;
      size.dispatchEvent(new Event('change'));
    }, '', value === (largeIcons ? state.largeIconSize : state.smallIconSize), { choiceLabel:value + ' px', radio:true });
  }
  add('navigation', 'Navigation sidebar', 'View', toggleNavigation, '', state.shell.classList.contains('files-navigation-open'));
  add('refresh', 'Refresh folder', 'View', () => source !== activeColumn() && source?.resourceRef
    ? reloadManagedColumn(snapshot.columnIndex, { preserveColumns:true }) : api.refresh());
  add('back', 'Back in folder history', 'Go', () => state.navigation.back(), state.navigation.canGoBack() ? '' : 'No previous folder.');
  add('forward', 'Forward in folder history', 'Go', () => state.navigation.forward(), state.navigation.canGoForward() ? '' : 'No next folder.');
  add('up', 'Parent folder', 'Go', managedUp);
  add('home', 'Home', 'Go', () => openProvider(CLANKER_HOME_PROVIDER));
  if (!options.pickerMode) {
    const hostFolderReason = fileResourceCanBrowseHostFolder(directory) ? '' : 'Choose an authorized Host folder.';
    add('folder-history', 'Folder History…', 'Go', () => openCapturedFolderHistory(snapshot), hostFolderReason);
    add('git-workbench', 'Git workbench…', 'Go', () => openCapturedGitWorkbench(snapshot),
      hostFolderReason || (snapshot.sessionId ? '' : 'Open a managed chat for this repository.'));
  }
  const favorite = state.favorites.find(item => item.resource_id && item.resource_id === directory?.resourceId);
  add('favorite', favorite ? 'Remove folder from Favorites' : 'Add folder to Favorites', 'Go',
    () => toggleCurrentFavorite(directory, current),
    !favorite?.pinned && fileResourceCanBrowseHostFolder(directory) ? '' : 'Choose an unpinned Host folder.');
  add('new-window-file', 'New Files Window', 'File', newWindow);
  add('new-window', 'New Files Window', 'Window', newWindow);
  add('split-right', 'Split right', 'Window', () => split('right', options.parentWindow?.id || id));
  add('split-below', 'Split below', 'Window', () => split('below', options.parentWindow?.id || id));
  add('focus-other', 'Focus other pane', 'Window', () => { api.activate(); return focusOtherPane(); });
  add('close-split', 'Close split', 'Window', () => { api.activate(); return closeSplit(); });
  return options.pickerMode ? descriptors.filter(item => item.menu === "Go" && item.id !== "files.favorite" || item.menu === "View" || ["files.open", "files.information"].includes(item.id)) : descriptors;
}
function activate() {
  activeBrowser = api;
  const splitPair = [...splits.values()].find(pair => pair.primary === api || pair.browser === api);
  if (splitPair && state.sidebar.parentElement !== splitPair.navigation) {
    for (const pane of [splitPair.primary, splitPair.browser]) {
      if (pane.sidebar?.parentElement === splitPair.navigation) pane.element.prepend(pane.sidebar);
      pane.sidebar.hidden = pane !== api;
    }
    splitPair.navigation.replaceChildren(state.sidebar);
  }
  for (const browser of browsers.values()) browser.element?.classList.toggle('files-pane-active', browser === api);
  options.parentWindow?.setNavigationAdapter?.(state.navigation);
  syncNavigationLayout();
}
const api = {
  captureLifetime: () => ({ lifecycle:state.lifecycleGeneration, owner:state.owner, policy:filesPolicyGeneration() }),
  isLifetimeCurrent: captured => !listeners.signal.aborted && browsers.get(id) === api && state.nativeWindow?.visible && captured?.lifecycle === state.lifecycleGeneration && captured.owner === state.owner && captured.policy === filesPolicyGeneration(),
  getTransferModel: payload => state.selectionModels.get(payloadScopeKey(payload)) || null,
  updateTransferSelection: model => {
    if (![...state.selectionModels.values()].includes(model)) return;
    if (model.scope.column === 'tree') {
      if (!model.selectedKeys().length) { state.treeSelection = null; options.onSelection?.(null); }
      updateTreeHighlight();
    } else applyModelSelection(model);
  },
  getDragPayload: () => state.gesture?.type === 'drag' ? state.gesture.payload : null,
  resolveTransferModel: payload => {
    if (listeners.signal.aborted || !state.nativeWindow?.visible || payload.owner !== state.owner || Number(payload.policy_generation) !== filesPolicyGeneration()) return null;
    const model = state.selectionModels.get(payloadScopeKey(payload));
    if (!model || model.epoch !== Number(payload.selection_epoch) || !payload.sources.every(source => model.has(String(source.resource_key)))) return null;
    return model;
  },
  explorerLayout, customizeExplorer, setEditorRoot,
  id, hooks, parentWindow: options.parentWindow, captureCommands, isCommandCaptureCurrent, commandDescriptors, open, openLibraryCollection, revealResource, revealWorkspaceResource,
  get element() { return state.shell; }, get sidebar() { return state.sidebar; }, get sidebarSeparator() { return state.sidebarSeparator; }, get window() { return state.nativeWindow; },
  getCurrentDirectory: () => { const column = activeColumn(); return column?.resourceRef ? { ...column, resource_ref:column.resourceRef, resource_id:column.resourceId, kind:'folder' } : null; },
  getSelection: () => state.treeSelection ? [state.treeSelection] : ensureSelectionModel(activeColumn()).selectedEntries(),
  getContext: () => hooks.__odysseusGetActiveFilesContext(),
  openDirectory: entry => openManagedDirectory(facadeEntry(entry), null),
  importBrowserReportFile,
  reconcileBrowserReportImport,
  newFile: () => createFilesResource('file', creationDestinationForNode(null)),
  refresh: refreshBrowser, cancelReveal, filterTree, clearFilter: () => filterTree(''), search: searchActive,
  activate,
  invoke: (command, node = null, request = null) => hooks.__openClankFilesContextCommand(command, node, request),
  dispose() { categoryObserver?.disconnect(); handleWindowClosed(); listeners.abort(); localMenu?.dispose(); browserResize?.disconnect(); state.navigation?.dispose(); state.pane?.destroy?.(); state.shell?.remove(); browsers.delete(id); if (activeBrowser === api) activeBrowser = browsers.values().next().value || null; },
};
browsers.set(id, api);
return api;
}

function browserFor(node, request) {
  const pane = request?.adapterContext?.filesPane;
  return browsers.get(pane || node?.closest?.('[data-files-browser-id]')?.dataset.filesBrowserId) || activeBrowser || defaultBrowser();
}
function defaultBrowser() {
  let browser = browsers.get('files-window');
  if (!browser) browser = createFilesBrowser({ id: 'files-window' });
  activeBrowser ||= browser;
  return browser;
}
for (const name of ['__openClankFilesGestureSnapshot','__openClankFilesTransferContext','__openClankFilesRetryKeepBoth','__odysseusGetActiveFilesContext','__openClankFilesContextCapture','__openClankFilesContextCapabilities','__openClankFilesContextCommand']) {
  window[name] = (...args) => {
    const captured = args.find(arg => arg?.adapterContext?.filesPane);
    const node = args.find(arg => arg?.closest);
    const pane = name === '__openClankFilesRetryKeepBoth' ? args[0]?.pane : null;
    const browser = name === '__openClankFilesTransferContext' ? [...browsers.values()].find(item => item.hooks[name]?.()) : browsers.get(pane) || browserFor(node, captured);
    return browser?.hooks[name]?.(...args) ?? null;
  };
}
/** Open standalone Files and reuse its authorized destination/name/Editor flow. */
export async function newFile() {
  const browser = defaultBrowser();
  await browser.open();
  return browser.newFile();
}
export const openLibraryCollection = (...args) => defaultBrowser().openLibraryCollection(...args);
export const revealResource = (...args) => defaultBrowser().revealResource(...args);
export const revealWorkspaceResource = (...args) => defaultBrowser().revealWorkspaceResource(...args);
const splits = new Map();
export async function newWindow() { const browser = createFilesBrowser(); await browser.open(); browser.activate(); return browser.window; }
export async function split(direction = 'right', windowId = activeBrowser?.window?.id) {
  const primary = browsers.get(windowId) || activeBrowser || defaultBrowser();
  const hostWindow = primary.window;
  if (!hostWindow || splits.has(hostWindow.id)) return false;
  const container = document.createElement('section'); container.className = 'files-split-pane';
  const panesHost = document.createElement('div'); panesHost.className = 'files-split-host'; panesHost.dataset.splitDirection = direction;
  const navigation = document.createElement('aside'); navigation.className = 'files-shared-navigation';
  hostWindow.body.classList.add('files-workbench-split');
  hostWindow.body.append(navigation,panesHost); panesHost.append(primary.element);
  primary.element.classList.add('files-pane-with-shared-navigation'); primary.sidebarSeparator.hidden = true;
  const divider = document.createElement('div'); divider.className = 'files-split-divider'; divider.tabIndex = 0; divider.setAttribute('role','separator'); divider.setAttribute('aria-label','Resize Files split'); divider.setAttribute('aria-orientation',direction === 'below' ? 'horizontal' : 'vertical');
  const applySplit = value => { primary.element.style.flex = `0 0 ${Math.max(20,Math.min(80,value))}%`; };
  divider.addEventListener('pointerdown', event => {
    event.preventDefault(); divider.setPointerCapture(event.pointerId);
    const move = moveEvent => { const box = panesHost.getBoundingClientRect(); applySplit(direction === 'below' ? (moveEvent.clientY-box.top)/box.height*100 : (moveEvent.clientX-box.left)/box.width*100); };
    const end = () => { divider.removeEventListener('pointermove',move); divider.removeEventListener('pointerup',end); divider.removeEventListener('pointercancel',end); };
    divider.addEventListener('pointermove',move); divider.addEventListener('pointerup',end); divider.addEventListener('pointercancel',end);
  });
  divider.addEventListener('keydown', event => { if (!['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) return; event.preventDefault(); const current = parseFloat(primary.element.style.flexBasis) || 50; applySplit(current + (['ArrowLeft','ArrowUp'].includes(event.key) ? -5 : 5)); });
  panesHost.append(divider,container);
  const browser = createFilesBrowser({ container, parentWindow: hostWindow });
  splits.set(hostWindow.id, { primary, browser, container, divider, navigation, panesHost });
  await browser.open(); browser.element.classList.add('files-pane-with-shared-navigation'); browser.sidebarSeparator.hidden = true; browser.activate(); return browser;
}
function restoreSplit(pair) {
  pair.primary.element.prepend(pair.primary.sidebar); pair.primary.sidebar.hidden = false;
  pair.primary.sidebarSeparator.hidden = false; pair.primary.element.classList.remove('files-pane-with-shared-navigation'); pair.primary.element.style.flex = '';
  pair.primary.window.body.append(pair.primary.element);
  pair.browser.dispose(); pair.navigation.remove(); pair.panesHost.remove(); pair.primary.window.body.classList.remove('files-workbench-split');
}
export function closeSplit() {
  const pair = [...splits.values()].find(pair => pair.primary === activeBrowser || pair.browser === activeBrowser);
  if (!pair) return false;
  splits.delete(pair.primary.window.id); restoreSplit(pair); pair.primary.activate(); return true;
}
export function getActiveBrowser() { return activeBrowser || defaultBrowser(); }
export async function importBrowserReportFile(file, options = {}) {
  const browser = activeBrowser || defaultBrowser();
  if (!browser || typeof browser.importBrowserReportFile !== 'function') throw new Error('Files import is unavailable.');
  return browser.importBrowserReportFile(file, options);
}
export async function reconcileBrowserReportImport(receiptHandles, options = {}) {
  const browser = activeBrowser || defaultBrowser();
  if (!browser || typeof browser.reconcileBrowserReportImport !== 'function') throw new Error('Files receipt reconciliation is unavailable.');
  return browser.reconcileBrowserReportImport(receiptHandles, options);
}
export function focusOtherPane() { const pair = [...splits.values()].find(pair => pair.primary === activeBrowser || pair.browser === activeBrowser); if (!pair) return false; const target = activeBrowser === pair.primary ? pair.browser : pair.primary; target.activate(); target.element.querySelector('[data-files-body]')?.focus(); return true; }
export default { open: () => defaultBrowser().open(), newFile, newWindow, split, closeSplit, focusOtherPane, getActiveBrowser, createFilesBrowser, openLibraryCollection, revealResource, revealWorkspaceResource, close: () => activeBrowser?.window?.requestClose() };
