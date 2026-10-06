import { createExplorerLayout, showExplorerCustomization } from './editor/explorerLayout.js';
import { isChatResource, dispatchFilesDestination, destinationLabel } from './copal/resourceDestinations.js';
import { acknowledgeVisible } from './achievementProducer.js';
import { filesServiceClient } from './filesServiceClient.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { createOpenClankWindow } from './copal/windows.js';
import { fileIcon, fileIconKey, glyphIcon } from './langIcons.js';
import { createResizablePane } from './editor/resizablePane.js';
import { createExplorerTree } from './editor/explorerTree.js';
import { createThumbnailLoader } from './editor/thumbnailLoader.js';
import {
  SORT_KEYS,
  constrainSortSpec,
  normalizeSortKeys,
  sortEntries as sortEntryList,
  normalizeSortSpec,
} from './editor/entryModel.js';
import { isPreviewable, previewKind, readBoundedTextResponse } from './editor/previewModel.js';
import { isTextPath, EDITOR_BINARY_SUFFIX_PATTERN } from './editor/languageRegistry.js';
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
import { openResourceHistory } from './historyView.js';
import { registerAdapter } from './custom-context-menu.js';
import { safeExternalDropSegment, safeExternalDropPath, applyRectangleSelection } from './filesDropModel.js';

const FILES_FAVORITES_KEY = 'odysseus-files-favorites';
const FILES_VIEW_PREFS_KEY = 'odysseus-files-view-preferences';

const EDITOR_BINARY_SUFFIX = new RegExp(EDITOR_BINARY_SUFFIX_PATTERN, 'i');
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
  defaultPath: '',
  navigationGeneration: null,
  navigationController: null,
  favoritesController: null,
  workspaceController: null,
  navigationRoots: [],
  explorerTree: null,
  favorites: [],
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
  actionMenu: null,
  watchController: null,
  watchResourceRef: '',
  watchEpoch: 0,
  watchRefreshTimer: null,
  selectionModel: null,
  selectionModels: new Map(),
  activeColumnIndex: -1,
  pendingKeepBoth: null,
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
    const label = {copal:'Copal collections',library:'Library · documents, research and downloads',files:'Gallery collections'}[provider];
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

function isDirectory(entry) {
  const kind = String(entry?.kind || '').toLowerCase();
  return Array.isArray(entry?.capabilities) && entry.capabilities.includes('children')
    || ['directory', 'recursive_directory', 'folder', 'virtual_folder', 'provider_root', 'album'].includes(kind);
}

function isTextualEntry(entry) {
  const name = String(entry?.name || '');
  if (EDITOR_BINARY_SUFFIX.test(name)) return false;
  const capabilities = new Set(Array.isArray(entry?.capabilities) ? entry.capabilities : []);
  if (capabilities.has('open_editor') || capabilities.has('edit') || capabilities.has('text')) return true;
  if (entry?.open_target?.app === 'editor') return true;
  const mime = String(entry?.mime_type || entry?.media_type || '').toLowerCase();
  if (mime.startsWith('text/') || /(?:json|javascript|typescript|xml|yaml|toml|sql|markdown)/.test(mime)) return true;
  return isTextPath(name);
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
  if (capabilities.includes('preview') && entry.preview_kind) {
    entry.preview_url = filesFacadeClient.contentUrl(entry.resource_ref, { purpose: 'preview' });
    if (entry.preview_kind === 'image') entry.thumbnail_url = entry.preview_url;
  }
  if (entry.provider === 'host' && capabilities.includes('preview') && entry.native_thumbnail_available !== false) {
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
  const workspace = String(context?.workspace || localStorage.getItem('odysseus-copal-workspace') || 'default').trim();
  return /^[A-Za-z0-9._-]{1,64}$/.test(workspace) ? workspace : 'default';
}

function setStatus(message, bad = false) {
  const paneStatus = state.shell?.querySelector('.files-pane-status');
  if (paneStatus) { paneStatus.textContent = message || ''; paneStatus.classList.toggle('error', !!bad); }
  const split = splits.get(id);
  if (!split || activeBrowser === api) state.nativeWindow?.setStatus(message, bad);
}

function clearKeepBothRetry() {
  state.pendingKeepBoth = null;
  state.shell?.querySelector('.files-retry-keep-both')?.remove();
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
    const response = await fetch('/api/auth/status', { credentials: 'same-origin', signal });
    if (!response.ok) {
      return {
        status: [401, 403].includes(response.status) ? 'invalid' : 'unavailable',
        owner: '',
      };
    }
    const data = await response.json();
    return { status: 'confirmed', owner: String(data?.username || '').trim() };
  } catch (error) {
    if (error?.name === 'AbortError') throw error;
    return { status: 'unavailable', owner: '' };
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
  for (const tree of [state.explorerTree, state.workspaceTree, state.favoriteTree, state.collectionTree]) {
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
    ariaLabel: 'Available host files and folders',
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
      state.treeSelection = node; options.onSelection?.(node);
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
    onContextMenu: (event, node, context) => {
      cancelReveal();
      // Runs on the row before the document context adapter captures it.
      // Use the controller descriptor, never a DOM-derived locator.
      state.treeSelection = node;
      controller.setActive(context.id);
      options.onSelection?.(node);
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
  const presentationKey = state.owner ? `files-tree:${options.surface || "files"}:${state.owner}:${state.currentWorkspaceId}:${treeOptions.persistenceKey || (alternateContainer ? 'collections' : 'locations')}` : '';
  const treeLifetime = new AbortController();
  const savePresentation = () => { if (!presentationKey) return; try { localStorage.setItem(presentationKey,JSON.stringify(tree.snapshot().nodes.filter(node => node.expanded).map(node => node.id))); } catch (_) {} };
  const persistAfterInteraction = () => requestAnimationFrame(() => { if (!treeLifetime.signal.aborted) { savePresentation(); filterTree(state.treeFilter); } });
  container.addEventListener('pointerdown',cancelReveal,{signal:treeLifetime.signal});
  container.addEventListener('click',persistAfterInteraction,{signal:treeLifetime.signal});
  container.addEventListener('keydown',persistAfterInteraction,{signal:treeLifetime.signal});
  tree.destroy = () => { savePresentation(); treeLifetime.abort(); controller.destroy(); };
  if (state.owner) {
    let expanded = [];
    try { expanded = JSON.parse(localStorage.getItem(`files-tree:${options.surface || "files"}:${state.owner}:${state.currentWorkspaceId}:${treeOptions.persistenceKey || (alternateContainer ? 'collections' : 'locations')}`) || '[]'); } catch (_) {}
    void (async () => { for (const identity of expanded) { if (listeners.signal.aborted) break; await tree.expand(identity); } })();
  }
  if (alternateContainer?.matches('[data-files-collections]')) {
    container.addEventListener('contextmenu',event=>{
      if (event.target.closest('.files-tree-toggle') || event.target === container) {event.preventDefault();event.stopPropagation();customizeExplorer();}
    },{signal:treeLifetime.signal});
  }
  updateTreeHighlight();
  return tree;
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
    if (identity.status !== 'confirmed' || !identity.owner) {
      clearFilesAuthorityContent('Files owner could not be verified');
      throw new Error('Files owner could not be verified');
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

async function loadOpaqueNavigationRoots({ force = false } = {}) {
  const startingContent = state.contentGeneration;
  state.navigationController?.abort();
  const controller = new AbortController();
  state.navigationController = controller;
  try {
    const [rootsResponse, identity] = await Promise.all([
      filesFacadeClient.roots({ copalWorkspace: copalWorkspace(), signal: controller.signal }),
      authenticatedOwner(controller.signal),
    ]);
    if (identity.status !== 'confirmed' || !identity.owner) {
      clearFilesAuthorityContent('Files owner could not be verified');
      throw new Error('Files owner could not be verified');
    }
    const owner = identity.owner;
    const managedRoots = (rootsResponse?.entries || []).map(facadeEntry).filter(editorResourceAllowed);
    const hostRoot = managedRoots.find(entry => entry.provider === 'host' && isDirectory(entry));
    const hostNavigation = hostRoot
      ? filesFacadeClient.children(hostRoot.resource_ref, {
        limit: 200,
        sort: managedSortRequest({ key: 'name', direction: 'asc', directoriesFirst: true }),
        signal: controller.signal,
      }).catch((error) => ({ error }))
      : Promise.resolve({ error: new Error('The opaque Host provider is unavailable') });
    const [hostResponse, placesResponse, workspacesResponse] = await Promise.all([
      hostNavigation,
      filesFacadeClient.places({ signal: controller.signal }).catch(error => {
        // Places were added after the first ResourceRef deployment. A server
        // lacking only this preference endpoint may still provide the secure
        // opaque worktree and pinned Home anchor.
        return { entries: [], error };
      }),
      filesFacadeClient.workspaces({ signal: controller.signal }).catch(error => {
        return { entries: [], error };
      }),
    ]);
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    const roots = hostResponse?.error ? [] : (hostResponse?.entries || []).map(explorerEntry).filter(editorResourceAllowed);
    const pinned = roots.filter(item => item.provenance?.favorite);
    if (state.owner && owner !== state.owner) {
      // The current roots have already been authenticated for the new owner;
      // invalidate only stale projected content, not this roots load.
      clearFilesAuthorityContent('Account file access changed');
    }
    state.owner = owner;
    state.navigation?.setScope({ account: owner, workspace: copalWorkspace() }, { clear: false });
    state.viewPreferences = readViewPreferences();
    if (state.contentGeneration === startingContent) applyViewPreferences(state.provider);
    state.pane?.refresh?.();
    state.defaultPath = '';
    state.managedRoots = managedRoots;
    state.importCapabilities = rootsResponse?.import_capabilities && typeof rootsResponse.import_capabilities === 'object'
      ? rootsResponse.import_capabilities : null;
    state.navigationRoots = roots;
    state.workspaces = Array.isArray(workspacesResponse?.entries) ? workspacesResponse.entries : [];
    const workspace = await import('./workspace.js');
    if (controller.signal.aborted) throw new DOMException('Aborted', 'AbortError');
    state.currentWorkspaceId = workspace.getWorkspaceId();
    explorerLayout.apply();
    state.navigationGeneration = rootsResponse?.policy_generation ?? null;
    // Refreshing the projection also refreshes every expiring ResourceRef.
    // Rebuild only on this policy/navigation event; ordinary current-folder
    // navigation never calls this function and cannot reshape the worktree.
    mountExplorerTree();
    state.collectionTree = mountExplorerTree(managedRoots.filter(root => root.provider !== 'host'), state.sidebar?.querySelector('[data-files-collections]'));
    if ((force || roots.length === 1) && roots.length === 1 && isDirectory(roots[0])) {
      await state.explorerTree?.expand(roots[0].tree_id);
    }
    await loadFavorites(pinned, placesResponse?.entries || []);
    renderWorkspaces();
    if (hostResponse?.error || placesResponse?.error || workspacesResponse?.error) {
      setStatus('Some Host navigation data is unavailable; managed Files remain available.', true);
    }
    return {
      version: rootsResponse?.version,
      generation: rootsResponse?.policy_generation,
      opaque: true,
      roots,
    };
  } finally {
    if (state.navigationController === controller) state.navigationController = null;
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

async function renameWorkspace(item) {
  const name = await uiModule.styledPrompt('Choose the name shown across Files, chat, and Editor.', {
    title: 'Rename Workspace',
    defaultValue: item.workspace.name,
    confirmText: 'Rename',
    maxLength: 200,
  });
  if (!name || name === item.workspace.name) return;
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

async function archiveWorkspace(item) {
  const confirmed = await uiModule.styledConfirm(
    `Archive “${item.workspace.name}”? Workspace-scoped agent approvals are revoked; files and the Location are not removed.`,
    { title: 'Archive Workspace?', confirmText: 'Archive', cancelText: 'Keep', danger: true },
  );
  if (!confirmed) return;
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

function showTreeActions(choices) {
  const anchor = document.activeElement;
  const rect = anchor?.getBoundingClientRect?.();
  closeManagedActionMenu();
  const owner = state.owner; const lifecycle = state.lifecycleGeneration;
  const menu = state.actionMenu = el('div',{class:'files-tree-actions-menu',role:'menu'});
  menu.style.left = Math.min(rect?.left || 0,window.innerWidth - 210) + 'px'; menu.style.top = (rect?.bottom || 0) + 'px';
  for (const choice of choices) menu.append(el('button',{type:'button',role:'menuitem',text:choice.label,disabled:choice.disabled ? 'true' : null,onclick:() => { closeManagedActionMenu(); if (owner !== state.owner || lifecycle !== state.lifecycleGeneration) return; void choice.run(); }}));
  menu.addEventListener('keydown',event => { if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); closeManagedActionMenu(); anchor?.focus(); } });
  document.body.append(menu); menu.querySelector('button:not(:disabled)')?.focus();
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
    onAction:(action,node) => {
      const item = state.workspaces.find(item => item.workspace.id === node.workspaceRootId); if (!item) return;
      showTreeActions([
        {label:'Use as workspace',disabled:node.unavailable,run:() => useWorkspace(node.workspaceRootId)},
        {label:'Rename workspace',run:() => renameWorkspace(item)},
        {label:'Archive workspace',run:() => archiveWorkspace(item)},
      ]);
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
  for (const tree of [state.explorerTree, state.workspaceTree, state.favoriteTree, state.collectionTree]) {
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

async function toggleCurrentFavorite() {
  if (state.provider !== 'host') return;
  const column = activeColumn();
  const activeResource = String(column?.resourceId || '');
  const activeRef = String(column?.resourceRef || '');
  if (activeResource && activeRef) {
    const existing = state.favorites.find(item => item.resource_id === activeResource);
    if (existing?.pinned) return;
    try {
      if (existing?.placeId) {
        await filesFacadeClient.removePlace(existing.placeId);
        state.favorites = state.favorites.filter(item => item !== existing);
      } else {
        const response = await filesFacadeClient.savePlace(activeRef);
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
  setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items · ${entries.length} selected${bytes ? ' · ' + formatBytes(bytes) : ''}`);
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

async function createFilesResource(kind, captured) {
  if (options.pickerMode) { setStatus("This dialog selects resources; creating items is unavailable.", true); return false; }
  if (state.contentController) { setStatus('Wait for folder navigation to finish before choosing New.', true); return false; }
  if (!captured?.creationDestinationRef) captured = await pickCreationDestination();
  if (!captured?.creationDestinationRef) {
    setStatus('Choose an authorized writable folder before creating an item.', true);
    return false;
  }
  if (kind === 'folder' && captured.creationSupportsFolders === false) {
    setStatus('This Copal collection supports new notes; folders appear when notes use folder names.', true);
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
        const result = await copal.createFilesNote({ name: proposed, workspace: captured.creationWorkspace,
          corpus: captured.creationCopalCorpus, actionId: operation });
        if (!result?.doc?.id) throw new Error('Copal creation outcome was not confirmed; do not submit it again.');
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

async function pasteFilesClipboard(destination, destinationIndex, current) {
  const clipboard = filesClipboard;
  if (!current()) throw new Error('The destination folder changed; choose Paste again.');
  if (!clipboard?.current()) throw new Error('The copied source changed. Copy the files again.');
  if (!destination?.capabilities?.includes('children') || !destination.capabilities.includes('write')
    || clipboard.payload.provider !== destination.provider) throw new Error('Paste requires a writable folder in the same provider.');
  const response = await executeFilesTransfer(clipboard.payload, destination, 'fail', destinationIndex, { model: clipboard.model });
  clipboard.source.updateTransferSelection(clipboard.model);
  if ((response?.items || []).length && response.items.every(item => ['committed','unchanged'].includes(item.outcome))) await clipboard.source.refresh();
  if (filesClipboard === clipboard && response) filesClipboard = null;
  return true;
}

function canvasMenuAdapter() {
  return {
    capture: target => ({ ...creationCaptureForNode(target), pasteDestination: captureFilesPasteDestination(target) }),
    commands: request => {
      const entry = contextEntryForNode(request?.objectTarget || request?.target);
      const commands = [];
      commands.push({ id: 'files-new-folder', label: 'New Folder…', disabled: request?.adapterContext?.creationSupportsFolders === false });
      commands.push({ id: 'files-new-file', label: 'New File…' });
      if (filesClipboard) {
        const destination = currentFilesPasteDestination(request?.adapterContext?.pasteDestination);
        commands.push({ id: 'files-paste', label: 'Paste', disabled: !destination
          || !filesClipboard.current() || filesClipboard.payload.provider !== destination.provider });
      }
      const selection = entry && currentCanvasMenuSelection(entry, request);
      if (!entry || !selection) return commands;
      const choices = selection.entries.length === 1
        ? entryActionChoices(entry)
        : commonActionChoices(selection.entries);
      for (const choice of choices) {
        // The typed file-object commands below already expose these same
        // operations, with their captured-selection fence and split-open
        // variants. Keep the Files Actions menu and item menu complementary.
        if (['code.open', 'rename', 'trash', 'restore', 'transfer.move', 'transfer.copy'].includes(choice.action)) continue;
        const label = selection.entries.length > 1 ? `${choice.label} (${selection.entries.length})` : choice.label;
        if (commands.some(command => command.id === `files-action:${encodeURIComponent(JSON.stringify([choice.action, choice.value ?? null]))}`)) continue;
        commands.push({
          id: `files-action:${encodeURIComponent(JSON.stringify([choice.action, choice.value ?? null]))}`,
          label,
        });
      }
      if (selection.entries.length === 1 && isCanvasImageEntry(entry)) commands.push({ id: 'files-open-in-canvas', label: 'Open in Canvas' });
      return commands;
    },
    execute: async (command, request) => {
      // Object Copy/Move belongs to the guarded file handler, including
      // multi-selection; do not consume it as an image-specific command.
      if (!['files-paste', 'files-new-folder', 'files-new-file', 'files-open-in-canvas'].includes(command)
        && !command.startsWith('files-action:')) return false;
      if (command === 'files-paste') {
        const captured = request?.adapterContext?.pasteDestination;
        const destination = currentFilesPasteDestination(captured);
        return pasteFilesClipboard(destination, Number(captured?.creationDestinationIndex),
          () => Boolean(currentFilesPasteDestination(captured)));
      }
      if (command === 'files-new-folder' || command === 'files-new-file') {
        return createFilesResource(command === 'files-new-folder' ? 'folder' : 'file', request?.adapterContext);
      }
      const entry = contextEntryForNode(request?.objectTarget || request?.target);
      const selection = currentCanvasMenuSelection(entry, request);
      if (!entry || !selection) { setStatus('The Files selection changed; choose the command again.', true); return true; }
      if (command.startsWith('files-action:')) {
        let identity;
        try { identity = JSON.parse(decodeURIComponent(command.slice('files-action:'.length))); } catch (_) { return false; }
        const choices = selection.entries.length === 1 ? entryActionChoices(entry) : commonActionChoices(selection.entries);
        const choice = choices.find(candidate => candidate.action === identity?.[0] && (candidate.value ?? null) === (identity?.[1] ?? null));
        if (!choice) { setStatus('That action is no longer available for this item.', true); return true; }
        if (selection.entries.length > 1) return performSelectedBulkAction(selection.entries, choice, selection.guard);
        return performManagedAction(entry, choice);
      }
      const model = currentCanvasMenuModel(entry, request);
      if (!model) { setStatus('Canvas requires one selected image. Keep only one item selected and try again.', true); return true; }
      if (command !== 'files-open-in-canvas') return false;
      return openEntryInCanvas(entry, model);
    },
  };
}

async function openEntryInCanvas(entry, model, commandId = operationId('files-canvas')) {
  const handoff = canvasPayloadForEntry(entry, model, commandId);
  if (!handoff) { setStatus('Select one authorized image to open in Canvas.', true); return false; }
  // Imps owns the canvas host after the Gallery retirement — no Gallery
  // modal is opened. The editor self-mounts its container.
  const editor = window.impsModule || await import('./imps.js');
  await editor.openEditor?.(null, null, { w: 1024, h: 1024 }, `Canvas · ${entry.name || 'Files image'}`);
  window.dispatchEvent(new CustomEvent('openclank-files-image-to-canvas', {
    detail: Object.freeze({ payload: handoff.payload, context: handoff.context }),
  }));
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

async function executeFilesTransfer(payload, destination, collision = 'fail', destinationIndex = state.columns.length - 1, { model: suppliedModel = null, selectionKeys = null, batchId = '', operationIdOverride = '', refresh = true } = {}) {
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
  try {
    setStatus(`${sources.length} item${sources.length === 1 ? '' : 's'} · ${payload.kind}…`);
    const response = await filesFacadeClient.transferResources({ operationId: operation, generation: Number(payload.generation), kind: payload.kind, sources, destinationRef, collision });
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
    return response;
  } catch (error) {
    if (error?.name !== 'AbortError') {
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
  return { outcome, terminal, committed: ['committed', 'unchanged', 'created', 'imported'].includes(outcome) };
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

async function importDroppedFiles(files, destination, destinationIndex = state.columns.length - 1, { skipped = [], directories = [] } = {}) {
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
  const importedDirectories = await prepareImportedDirectories(bounded, destination, batchId, generation, lifecycle, owner, directories);
  if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
    setStatus('Imports stopped because file access changed.', true);
    return;
  }
  state.pendingImport = allFiles.slice(FILES_MAX_DRAG_ITEMS);
  if (state.pendingImport.length) setStatus(`${bounded.length} of ${allFiles.length} files queued · ${state.pendingImport.length} remaining`, false);
  const results = new Array(bounded.length);
  let cursor = 0;
  const worker = async () => {
    while (cursor < bounded.length) {
      if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) return;
      const index = cursor++;
      const file = bounded[index];
      const id = operationId('files-import');
      const itemId = `${batchId}-${index}-${operationId('item').slice(-12)}`.slice(0, 128);
      const controller = new AbortController();
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
        let receipt;
        try {
          receipt = await filesFacadeClient.importFile(file.file, {
            operationId: id, itemId, generation, destinationRef: directory.ref,
            name: file.name, relativePath: file.name,
            collision: 'fail', signal: controller.signal,
          });
        } catch (error) {
          if (error?.name === 'AbortError') throw error;
          if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) throw error;
          // A transport failure is unknown until the idempotent receipt says
          // otherwise. Never submit the file again.
          receipt = await filesFacadeClient.operationReceipt(id, { signal: controller.signal, generation });
        }
        if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) return;
        let parsed = importReceiptItem(receipt, itemId, { operationId: id, generation });
        if (!parsed.terminal && receipt?.state !== 'pending') parsed = { outcome: 'malformed', terminal: false, committed: false };
        results[index] = parsed;
      } catch (error) {
        results[index] = { outcome: error?.name === 'AbortError' ? 'cancelled' : 'unknown', terminal: false, committed: false };
      } finally { state.importControllers.delete(controller); }
    }
  };
  await Promise.all([worker(), worker()]);
  if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
    setStatus('Imports stopped because file access changed.', true);
    return;
  }
  const committed = results.filter((result) => result?.committed).length;
  const pending = results.filter((result) => !result?.terminal).length;
  const rejected = results.filter((result) => result?.terminal && !result.committed).length;
  if (committed && state.owner === owner && filesPolicyGeneration() === generation && state.lifecycleGeneration === lifecycle) {
    await reloadManagedColumn(destinationIndex);
    if (state.owner !== owner || filesPolicyGeneration() !== generation || state.lifecycleGeneration !== lifecycle) {
      setStatus('File access or window changed while refreshing imported files.', true);
      return;
    }
  }
  const remaining = state.pendingImport.length;
  const skippedFailures = [...(Array.isArray(skipped) ? skipped : []), ...importedDirectories.failures];
  const skippedCount = skippedFailures.length;
  setStatus(`${committed} imported${rejected ? ` · ${rejected} rejected` : ''}${skippedCount ? ` · ${skippedCount} skipped (${skippedFailures[0]?.code || 'partial_failure'})` : ''}${pending ? ` · ${pending} pending reconciliation` : ''}${remaining ? ` · ${remaining} remaining (drop them separately)` : ''}`, Boolean(rejected || skippedCount || pending));
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
  state.thumbnailLoader?.clear?.();
  state.thumbnailLoader = null;
}

function ensureThumbnailLoader() {
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
  const identity = String(
    entry.resource_id || entry.resource_ref || entry.resourceRef || entry.name || item.url,
  );
  const key = [
    state.owner || 'anonymous',
    entry.provider || 'host',
    identity,
    entry.modified_unix_ms ?? entry.modified ?? '',
    entry.size ?? '',
    item.icon ? 'native-icon:64x64' : 'content-thumbnail:192x192',
    Math.min(3, Math.max(1, Number(window.devicePixelRatio) || 1)),
  ].join(':');
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
      const capabilities = model.selectedEntries().map(transferCapabilities);
      for (const node of entriesHost.querySelectorAll('[data-files-selection-key]')) {
        node.dataset.fileTransferMove = capabilities.length > 0 && capabilities.every(capability => capability.move) ? 'true' : 'false';
        node.dataset.fileTransferCopy = capabilities.length > 0 && capabilities.every(capability => capability.copy) ? 'true' : 'false';
      }
      row.focus({ preventScroll: true });
    });
    row.addEventListener('contextmenu', (event) => {
      // The shared app menu owns this target when enabled.  With that
      // preference disabled, leave the event untouched so the browser menu
      // appears; explicit toolbar/action buttons still open our menu.
    });
    row.addEventListener('keydown', (event) => {
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
    entriesHost.append(row);
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
      item.addEventListener('contextmenu', (event) => {
        // See the list view handler above: browser context menus remain the
        // immediate fallback when the shared menu preference is off.
      });
      item.addEventListener('keydown', (event) => {
        void handleColumnEntryKeydown(event, { columnIndex: index, entryIndex: actualEntryIndex, entry, path, directory });
      });
      list.append(item);
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
  if (!state.columns[index]) {
    return;
  }
  if (!state.columns[index].resourceRef) {
    setStatus('Refresh the authorized Files roots before sorting this folder.', true);
    return false;
  }
  const normalized = constrainSortSpec(spec, state.columns[index].sortKeys || SORT_KEYS);
  state.columns[index].sort = normalized;
  await reloadManagedColumn(index);
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
    setStatus(`${column.entries.length}${column.nextCursor ? '+' : ''} items`);
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

async function previewEntry(entry, path) {
  const preview = state.shell?.querySelector('[data-files-preview]');
  if (!preview) return;
  if (!isFacadeEntry(entry)) {
    setStatus('Refresh the authorized Files roots before previewing this item.', true);
    return;
  }
  closePreview();
  const controller = new AbortController(); state.previewController = controller;
  preview.hidden = false; preview.replaceChildren(el('div', { class: 'files-preview-loading', text: 'Loading preview…' }));
  const kind = previewKind(entry, path);
  const provider = state.provider;
  let handle = null;
  let url = entry.preview_url || entry.download_url;
  try {
    const close = iconButton({ glyph: 'close', title: 'Close preview', onClick: closePreview });
    const head = el('div', { class: 'files-preview-head' }, el('strong', { text: entry.name || displayName(path) }), close);
    const content = el('div', { class: 'files-preview-content' });
    if (kind === 'image' || kind === 'audio') {
      // Let the browser media engine consume the opaque authorized Rust
      // stream. This avoids assembling a second full-size Blob in the Files
      // UI and keeps audio seeking/range requests useful for recordings.
      const media = kind === 'image'
        ? el('img', { class: 'files-preview-image', src: url, alt: entry.name || displayName(path), loading: 'lazy' })
        : el('audio', { class: 'files-preview-audio-engine', preload: 'metadata', src: url, 'aria-label': `Audio preview for ${entry.name || displayName(path)}` });
      media.addEventListener('error', () => setStatus('Preview unavailable', true), { once: true });
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
    preview.replaceChildren(head, content);
  } catch (error) {
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
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`);
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
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`);
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
    && state.nativeWindow?.visible;
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
        throw new Error('Files watch ended');
      }
    } catch (error) {
      if (error?.name !== 'AbortError' && epoch === state.watchEpoch) {
        setStatus('Live folder updates paused · use Refresh to reconcile', true);
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
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`);
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
    const column = managedColumn(resource, response, requestedSort, '');
    const ancestors = !nested && Array.isArray(entry.tree_ancestors) ? entry.tree_ancestors.map(parent => managedColumn(parent,{entries:parent.tree_entries || [],next_cursor:parent.tree_cursor || null},parent.tree_sort || requestedSort)) : [];
    commitManagedColumn(column, {
      provider: resource?.provider || state.provider,
      columns: nested ? [...parents, column] : [...ancestors,column],
    });
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError' && generation === state.contentGeneration) {
      setStatus(error.message || 'Folder unavailable', true);
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

async function reloadManagedColumn(index) {
  const column = state.columns[index];
  if (!column) return false;
  if (!column.resourceRef) return openProvider('all');
  state.contentController?.abort();
  const controller = new AbortController();
  state.contentController = controller;
  const generation = ++state.contentGeneration;
  const requestedSort = constrainSortSpec(column.sort || state.sort, column.sortKeys || SORT_KEYS);
  column.sort = requestedSort;
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
    commitManagedColumn(refreshed, { columns: [...columns, refreshed] });
    return true;
  } catch (error) {
    if (error?.name !== 'AbortError') setStatus(error.message || 'Folder could not be sorted', true);
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
  const cursor = column.nextCursor;
  try {
    const response = await filesFacadeClient.children(column.resourceRef, {
      cursor,
      sort: managedSortRequest(column.sort || state.sort),
      query: column.query || '',
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || controller.signal.aborted || cursor !== column.nextCursor) return false;
    const combined = new Map(column.entries.map((item) => [item.resource_id, item]));
    for (const item of (response?.entries || []).map(facadeEntry).filter(editorResourceAllowed)) combined.set(item.resource_id, item);
    column.entries = [...combined.values()];
    column.nextCursor = response?.next_cursor || null;
    if (index === state.columns.length - 1) {
      state.entries = column.entries;
      state.nextCursor = column.nextCursor;
    }
    renderEntries();
    setStatus(`${column.entries.length}${column.nextCursor ? '+' : ''} items`);
    return true;
  } catch (error) {
    if (error?.code === 'stale_cursor' && generation === state.contentGeneration) return reloadManagedColumn(index);
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
  for (const tree of [state.workspaceTree, state.favoriteTree, state.explorerTree, state.collectionTree]) {
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
    });
    if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
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

async function openProvider(provider) {
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
    const data = await filesFacadeClient.roots({
      copalWorkspace: copalWorkspace(),
      signal: controller.signal,
    });
    if (generation !== state.contentGeneration || !state.nativeWindow?.visible) return false;
    state.managedRoots = (data?.entries || []).map(facadeEntry).filter(editorResourceAllowed);
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
    state.entries = [];
    if (body) body.replaceChildren(el('div', { class: 'files-error', text: error.message || `${provider} unavailable` }));
    setStatus(error.message || `${provider} unavailable`, true);
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
  const selected = [...state.selected];
  const entries = state.entries.filter((entry) => {
    return selected.includes(entrySelectionKey(entry)) && !isDirectory(entry);
  });
  if (!entries.length) { setStatus('Select one or more files first.', true); return; }
  for (const entry of entries) {
    if (entry.download_url) await downloadEntry(entry);
    else if (state.provider === 'host') await download(entryPath(entry));
  }
}

function selectedActionEntry() {
  const selected = state.entries.filter((entry) => state.selected.has(entrySelectionKey(entry)));
  return selected.length === 1 ? selected[0] : null;
}

function selectedActionEntries() {
  const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
  return model?.selectedEntries?.() || state.entries.filter((entry) => state.selected.has(entrySelectionKey(entry)));
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
  return (provider === 'host' || capabilities.has('open'))
    && capabilities.has('stat')
    && capabilities.has('open')
    && !isDirectory(entry)
    && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase());
}

function isLibraryDocumentsView() {
  const column = activeColumn();
  return String(column?.provider || '') === 'library'
    && libraryCollectionView(column) === LIBRARY_COLLECTION_VIEWS.documents;
}

function selectedLibraryDocumentEntries() {
  if (!isLibraryDocumentsView()) return [];
  return state.entries.filter((entry) => (
    state.selected.has(entrySelectionKey(entry)) && !isDirectory(entry) && Boolean(entry.resource_ref)
  ));
}

function entryActionChoices(entry) {
  const capabilities = new Set(entry?.capabilities || []);
  const choices = [];
  if (isHostInteropEntry(entry) && isDirectory(entry) && capabilities.has('children') && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase())) {
    choices.push({ action: 'workspace.use', label: 'Use as workspace', glyph: 'workspace', local: true });
  }
  if (isHostInteropEntry(entry) && !isDirectory(entry) && isTextualEntry(entry) && !['symlink', 'special', 'other'].includes(String(entry?.kind || '').toLowerCase())) {
    choices.push({ action: 'code.open', label: 'Open in Editor', glyph: 'code', local: true });
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
  if (capabilities.has('restore') && entry?.provenance?.domain !== 'copal') choices.push({ action: 'archive.set', value: false, label: 'Restore', glyph: 'restore' });
  if (entry?.provenance?.domain === 'copal' && capabilities.has('rename')) choices.push({ action: 'rename', label: 'Rename', glyph: 'edit' });
  const transfer = transferCapabilities(entry);
  if (entry?.provenance?.domain === 'copal' && transfer.move) choices.push({ action: 'transfer.move', label: 'Move', glyph: 'folder' });
  if (entry?.provenance?.domain === 'copal' && transfer.copy) choices.push({ action: 'transfer.copy', label: 'Copy', glyph: 'copy' });
  if (entry?.provenance?.domain === 'copal' && capabilities.has('trash')) choices.push({ action: 'trash', label: 'Move to trash', glyph: 'trash' });
  if (entry?.provenance?.domain === 'copal' && capabilities.has('restore')) choices.push({ action: 'restore', label: 'Restore', glyph: 'restore' });
  if (isLibraryDocumentsView() && !isDirectory(entry)) {
    choices.push({ action: 'library.clone', label: 'Clone to current session', glyph: 'copy', local: true });
    choices.push({ action: 'library.delete', label: 'Delete', glyph: 'trash', local: true, danger: true });
  }
  return choices;
}

function closeManagedActionMenu() {
  state.actionMenu?.remove?.();
  state.actionMenu = null;
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
  const selectedEntries = selectedActionEntries();
  const entry = selectedEntries.length === 1 ? selectedEntries[0] : null;
  const libraryEntries = selectedLibraryDocumentEntries();
  if (libraryEntries.length > 1 && libraryEntries.length === selectedEntries.length) {
    button.disabled = false;
    setActionButtonLabel(button, libraryEntries.length);
    button.title = `Actions for ${libraryEntries.length} selected Library documents`;
    return;
  }
  if (selectedEntries.length > 1) {
    const common = commonActionChoices(selectedEntries);
    button.disabled = common.length === 0;
    setActionButtonLabel(button, selectedEntries.length);
    button.title = common.length ? `Actions available for all ${selectedEntries.length} selected items` : 'No action is available for every selected item';
    return;
  }
  const count = entry ? entryActionChoices(entry).length : 0;
  button.disabled = count === 0;
  setActionButtonLabel(button, 0);
  button.title = count ? `Actions for ${entry.name}` : 'Select one actionable item';
}

function setActionButtonLabel(button, count) {
  const label = button.querySelector('.files-actions-label');
  if (!label) return;
  label.textContent = count ? `Actions (${count})` : 'Actions';
}

function commonActionChoices(entries) {
  if (!entries.length) return [];
  const batchable = new Set(['archive.set', 'favorite.set', 'transfer.move', 'transfer.copy']);
  const choices = entryActionChoices(entries[0]);
  return choices.filter((choice) => batchable.has(choice.action) && entries.slice(1).every((entry) =>
    entryActionChoices(entry).some((candidate) => candidate.action === choice.action && (candidate.value ?? null) === (choice.value ?? null))));
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

async function performManagedAction(entry, choice) {
  if (options.pickerMode) { setStatus('This dialog selects resources; file changes are unavailable.', true); return false; }
  const menuTrigger = document.activeElement;
  closeManagedActionMenu();
  if (!entry || !choice) return false;
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
    if (choice.action === 'library.delete' && !await uiModule.styledConfirm(
      'Delete this document?', { title: 'Delete document', confirmText: 'Delete', danger: true },
    )) return false;
    try {
      const actions = await import('./filesLibraryActions.js');
      if (choice.action === 'library.clone') {
        const clone = await actions.cloneLibraryDocument(entry.resource_ref, {
          sessionId: window.sessionModule?.getCurrentSessionId?.() || null,
        });
        if (clone?.id && typeof window.documentModule?.loadDocument === 'function') {
          await window.documentModule.loadDocument(clone.id);
        }
      } else {
        await actions.deleteLibraryDocument(entry.resource_ref);
      }
      await reloadManagedColumn(state.columns.length - 1);
      setStatus(choice.label + ': ' + entry.name);
      return true;
    } catch (error) {
      setStatus(error?.message || choice.label + ' failed', true);
      return false;
    }
  }
  if (choice.action === 'transfer.move' || choice.action === 'transfer.copy') {
    const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
    const kind = choice.action === 'transfer.copy' ? 'copy' : 'move';
    return executeSelectedFilesTransfer(kind, { model, destination: activeColumn(), destinationIndex: state.activeColumnIndex });
  }
  const controller = new AbortController();
  state.contentController?.abort();
  state.contentController = controller;
  const generation = state.contentGeneration;
  setStatus(`${choice.label}…`);
  try {
    if (choice.action === 'code.open') {
      const code = window.codeEditorModule || await import('./codeEditor.js');
      const opened = isFacadeEntry(entry)
        ? await code.openResource(entry.resource_ref)
        : await code.openPath(entryPath(entry), { directory: isDirectory(entry) });
      if (!opened) throw new Error('Editor did not open this resource');
      setStatus(`Opened ${entry.name} in Editor`);
      return true;
    }
    if (choice.action === 'workspace.use') {
      const capabilities = new Set(entry.capabilities || []);
      if (!isDirectory(entry) || !capabilities.has('children') || !capabilities.has('stat')) throw new Error('Only an authorized folder can be used as a workspace');
      const workspace = await import('./workspace.js');
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
      if (controller.signal.aborted || generation !== state.contentGeneration) return false;
      await workspace.setWorkspace(bound.path, bound.id);
      setStatus(`Workspace: ${entry.name}`);
      return true;
    }
    if (choice.action === 'host.default' || choice.action === 'host.open') {
      const discovered = await filesFacadeClient.hostApps(entry.resource_ref, { signal: controller.signal });
      const applications = Array.isArray(discovered?.applications) ? discovered.applications : [];
      if (!applications.length) throw new Error('No installed app can open this file.');
      const application = choice.action === 'host.default'
        ? applications.find((candidate) => candidate.default)
        : await chooseHostApplication(applications);
      if (!application && choice.action === 'host.open') return false;
      if (!application) throw new Error('No default application is registered for this file. Choose an installed app instead.');
      await filesFacadeClient.openHost(entry.resource_ref, application.id, { signal: controller.signal });
      setStatus(`Opened ${entry.name} with ${application.name}`);
      return true;
    }
    if (!entry.resource_ref) return false;
    let actionArgs = {};
    if (choice.action === 'rename' || choice.action === 'move') {
      const name = await uiModule.styledPrompt(choice.action === 'rename' ? 'Choose a new file name.' : 'Choose the destination name for this document.', {
        title: choice.action === 'rename' ? 'Rename document' : 'Move document', defaultValue: entry.provenance?.logical_path || entry.name,
      });
      if (name == null) return false;
      if (!String(name).trim()) throw new Error('A name is required.');
      actionArgs = { name: String(name).trim() };
    } else if (choice.action === 'trash' && !await uiModule.styledConfirm(`Move ${entry.name} to trash?`, { title: 'Move to trash', confirmText: 'Move to trash', danger: true })) return false;
    const actionId = `files-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    const response = await filesFacadeClient.action(
      entry.resource_ref,
      choice.action,
      choice.action === 'favorite.set' || choice.action === 'archive.set' ? { value: choice.value } : actionArgs,
      { signal: controller.signal, actionId },
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
    setStatus(recoveryUnavailable ? `Saved; recovery copy unavailable: ${entry.name}` : `${choice.label}: ${entry.name}`);
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
    if (error?.name !== 'AbortError') setStatus(error.message || `${choice.label} failed`, true);
    return false;
  } finally {
    if (state.contentController === controller) state.contentController = null;
  }
}

function sameFileHistoryKeys(left, right) {
  return left.length === right.length && left.every((key, index) => key === right[index]);
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

function showEntryActionMenu(entry, { x = null, y = null, anchor = null } = {}) {
  closeManagedActionMenu();
  const choices = entryActionChoices(entry);
  if (!choices.length) return false;
  const menu = el('div', {
    class: 'files-action-menu',
    role: 'menu',
    'aria-label': `Actions for ${entry.name}`,
  });
  for (const choice of choices) {
    const item = el('button', { type: 'button', class: 'files-action-menu-item', role: 'menuitem' });
    item.append(namedGlyph(choice.glyph, { size: 14 }), el('span', { text: choice.label }));
    item.addEventListener('click', () => { void performManagedAction(entry, choice); });
    menu.append(item);
  }
  document.body.append(menu);
  const box = anchor?.getBoundingClientRect?.();
  const left = Number.isFinite(Number(x)) ? Number(x) : Number(box?.left || 8);
  const top = Number.isFinite(Number(y)) ? Number(y) : Number(box?.bottom || 8) + 4;
  menu.style.left = `${Math.max(6, Math.min(left, window.innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(top, window.innerHeight - menu.offsetHeight - 6))}px`;
  state.actionMenu = menu;
  menu.querySelector('button')?.focus();
  return true;
}

async function performLibraryBulkAction(entries, action) {
  if (action === 'delete' && !await uiModule.styledConfirm(
    'Delete ' + entries.length + ' documents?', { title: 'Delete documents', confirmText: 'Delete', danger: true },
  )) return false;
  const actions = await import('./filesLibraryActions.js');
  let completed = 0;
  let failed = 0;
  for (const entry of entries) {
    try {
      if (action === 'clone') {
        await actions.cloneLibraryDocument(entry.resource_ref, {
          sessionId: window.sessionModule?.getCurrentSessionId?.() || null,
        });
      } else if (action === 'delete') {
        await actions.deleteLibraryDocument(entry.resource_ref);
      } else {
        await filesFacadeClient.action(entry.resource_ref, 'archive.set', { value: action === 'archive' }, {
          actionId: operationId('files-library-' + action),
        });
      }
      completed += 1;
    } catch {
      failed += 1;
    }
  }
  clearFilesSelection();
  await reloadManagedColumn(state.columns.length - 1);
  setStatus((action === 'archive' ? 'Archived' : action === 'restore' ? 'Restored' : action === 'clone' ? 'Cloned' : 'Deleted')
    + ' ' + completed + (failed ? ' · ' + failed + ' failed' : ''));
  return failed === 0;
}

function showLibraryBulkActionMenu(entries, anchor) {
  closeManagedActionMenu();
  const menu = el('div', { class: 'files-action-menu', role: 'menu', 'aria-label': 'Bulk Library document actions' });
  const choices = [
    ...(entries.every((entry) => entry.capabilities?.includes('archive')) ? [['archive', 'Archive', 'archive']] : []),
    ...(entries.every((entry) => entry.capabilities?.includes('restore')) ? [['restore', 'Restore', 'restore']] : []),
    ['clone', 'Clone to current session', 'copy'], ['delete', 'Delete', 'trash'],
  ];
  for (const [action, label, glyph] of choices) {
    const item = el('button', { type: 'button', class: 'files-action-menu-item', role: 'menuitem' });
    item.append(namedGlyph(glyph, { size: 14 }), el('span', { text: label }));
    item.addEventListener('click', () => {
      closeManagedActionMenu();
      void performLibraryBulkAction(entries, action);
    });
    menu.append(item);
  }
  document.body.append(menu);
  const box = anchor?.getBoundingClientRect?.();
  menu.style.left = Math.max(6, Math.min(Number(box?.left || 8), window.innerWidth - menu.offsetWidth - 6)) + 'px';
  menu.style.top = Number(box?.bottom || 8) + 4 + 'px';
  state.actionMenu = menu;
  menu.querySelector('button')?.focus();
}

function showSelectedBulkActionMenu(entries, choices, anchor, guard) {
  closeManagedActionMenu();
  const menu = el('div', { class: 'files-action-menu', role: 'menu', 'aria-label': `Actions for ${entries.length} selected items` });
  for (const choice of choices) {
    const item = el('button', { type: 'button', class: 'files-action-menu-item', role: 'menuitem' });
    item.append(namedGlyph(choice.glyph, { size: 14 }), el('span', { text: `${choice.label} (${entries.length})` }));
    item.addEventListener('click', () => { closeManagedActionMenu(); void performSelectedBulkAction(entries, choice, guard); });
    menu.append(item);
  }
  document.body.append(menu);
  const box = anchor?.getBoundingClientRect?.();
  menu.style.left = `${Math.max(6, Math.min(Number(box?.left || 8), window.innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(Number(box?.bottom || 8) + 4, window.innerHeight - menu.offsetHeight - 6))}px`;
  state.actionMenu = menu;
  menu.querySelector('button')?.focus();
}

async function performSelectedBulkAction(entries, choice, guard) {
  const model = validateFilesSelectionGuard(guard);
  if (!model) {
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
  if (choice.action === 'transfer.move' || choice.action === 'transfer.copy') {
    const destinationIndex = Number(guard.filesDestinationIndex);
    const capturedColumn = Number.isInteger(destinationIndex) && destinationIndex >= 0
      ? state.columns[destinationIndex] || null : null;
    const destinationRef = String(guard.filesDestinationRef || capturedColumn?.resourceRef || '').trim();
    if (!capturedColumn || !destinationRef || destinationResourceRef(capturedColumn) !== destinationRef) {
      setStatus('The destination folder changed; choose the transfer again.', true);
      return false;
    }
    const destination = destinationRef
      ? { ...(capturedColumn || {}), resource_ref:destinationRef, kind:'folder', capabilities:[...new Set([...(capturedColumn?.capabilities || []), 'children'])] }
      : null;
    if (!destination || !isDirectory(destination)) {
      setStatus('Choose an authorized destination folder before transferring the selection.', true);
      return false;
    }
    return executeSelectedFilesTransfer(choice.action === 'transfer.copy' ? 'copy' : 'move', {
      model, destination, destinationIndex,
    });
  }
  if (choice.action !== 'archive.set' && choice.action !== 'favorite.set') return false;
  let completed = 0;
  let failed = 0;
  for (const entry of entries) {
    if (!validateFilesSelectionGuard(guard)) {
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
  if (!validateFilesSelectionGuard(guard)) {
    setStatus(`${completed} of ${entries.length} completed; the Files selection or access changed before refresh.`, true);
    return false;
  }
  clearFilesSelection();
  if (activeColumn()?.globalSearch) await searchAllSources(state.searchQuery);
  else if (usesFacadeListing()) await reloadManagedColumn(state.columns.length - 1);
  setStatus(`${choice.label}: ${completed} of ${entries.length}${failed ? ` · ${failed} failed` : ''}`);
  return failed === 0;
}

function showFilesNewMenu({ anchor = null } = {}) {
  closeManagedActionMenu();
  const captured = creationDestinationForNode(null);
  const menu = el('div', { class: 'files-action-menu', role: 'menu', 'aria-label': 'New Files item' });
  for (const [kind, label, glyph] of [['folder', 'New Folder…', 'folder'], ['file', 'New File…', 'file']]) {
    const item = el('button', { type: 'button', class: 'files-action-menu-item', role: 'menuitem' });
    item.append(namedGlyph(glyph, { size: 14 }), el('span', { text: label }));
    item.disabled = kind === 'folder' && captured?.creationSupportsFolders === false;
    item.addEventListener('click', () => { closeManagedActionMenu(); void createFilesResource(kind, captured); });
    menu.append(item);
  }
  document.body.append(menu);
  const box = anchor?.getBoundingClientRect?.();
  const left = Number(box?.left || 8);
  const top = Number(box?.bottom || 8) + 4;
  menu.style.left = `${Math.max(6, Math.min(left, window.innerWidth - menu.offsetWidth - 6))}px`;
  menu.style.top = `${Math.max(6, Math.min(top, window.innerHeight - menu.offsetHeight - 6))}px`;
  state.actionMenu = menu;
  menu.querySelector('button')?.focus();
  return true;
}

function openSelectedActions() {
  const selectedEntries = selectedActionEntries();
  const entry = selectedEntries.length === 1 ? selectedEntries[0] : null;
  const button = state.shell?.querySelector('[data-files-actions]');
  const libraryEntries = selectedLibraryDocumentEntries();
  if (libraryEntries.length > 1 && libraryEntries.length === selectedEntries.length) {
    showLibraryBulkActionMenu(libraryEntries, button);
    return;
  }
  if (selectedEntries.length > 1) {
    const choices = commonActionChoices(selectedEntries);
    if (choices.length) {
      const model = ensureSelectionModel(activeColumn(), state.activeColumnIndex);
      showSelectedBulkActionMenu(selectedEntries, choices, button, captureFilesSelectionGuard(model));
    }
    return;
  }
  if (entry) showEntryActionMenu(entry, { anchor: button });
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
    const editor = window.impsModule || await import('./imps.js');
    if (exact && resourceRef) {
      if (typeof editor?.openResource === 'function') {
        await editor.openResource(resourceRef);
        return;
      }
    }
    if (typeof editor?.openEditor === 'function') {
      await editor.openEditor(null, null, null, 'Imps');
      return;
    }
    throw new Error('Imps is unavailable');
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

async function openManagedEntry(entry) {
  if (options.pickerMode) return confirmPickerEntry(entry);
  if (isPublishedLibraryEntry(entry)) {
    if (entry.capabilities?.includes('preview')) await previewEntry(entry, entryPath(entry));
    else if (entry.capabilities?.includes('download')) await downloadEntry(entry);
    else setStatus('This published resource can no longer be previewed or downloaded.', true);
    return;
  }
  try {
    const response = await filesFacadeClient.open(entry.resource_ref);
    const app = response?.target?.app;
    const refreshedRef = String(response?.resource?.ref || entry.resource_ref || '');
    await dispatchFilesDestination(response, { resourceRef:refreshedRef, surface:options.surface || 'files' });
    if (response?.resource) {
      const refreshed = facadeEntry(response.resource);
      const index = state.entries.findIndex((candidate) => entrySelectionKey(candidate) === entrySelectionKey(entry));
      if (index >= 0) state.entries[index] = refreshed;
    }
    setStatus(`Opened ${entry.name} in its source app`);
  } catch (error) {
    setStatus(error.message || 'Source app could not be opened', true);
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

async function executeSelectedFilesTransfer(kind, { model = null, destination = null, destinationIndex = null } = {}) {
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
      'fail',
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
  supersedeFilesRefreshForContext();
  const entry = contextEntryForNode(node);
  if (!entry) return null;
  const key = entrySelectionKey(entry);
  const selectedColumn = activateColumn(columnIndexForNode(node));
  const destinationIndex = selectedColumn.index;
  const destination = selectedColumn.column;
  const model = selectedColumn.model;
  // A workspace event can update the scope before its delayed facade reload
  // replaces the rows. Reindex the still-mounted rows under the new scope so
  // the exact clicked opaque identity can be selected and captured.
  if (!model.orderedKeys().includes(key)) model.setItems(selectedColumn.column?.entries || state.entries);
  // Desktop Files keeps a right-clicked selected row in the existing set;
  // right-clicking elsewhere changes the context target to that one row.
  if (!model.has(key)) { model.select(key); state.selected = new Set(model.selectedKeys()); renderEntries(); }
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
  const entries = state.entries.filter((entry) => state.selected.has(entrySelectionKey(entry)));
  if (entries.length !== 1) {
    setStatus('Select one managed file to open.', true);
    return;
  }
  const entry = entries[0];
  if (options.pickerMode) { if (isDirectory(entry)) await openManagedDirectory(entry); else confirmPickerEntry(entry); return; }
  if (isDirectory(entry) || !entry.resource_ref) {
    setStatus('Select a file to open.', true);
    return;
  }
  // Published Library resources remain an exact, read-only Files experience:
  // preview or download them instead of reopening the retired Library modal.
  if (isPublishedLibraryEntry(entry)) {
    if (entry.capabilities?.includes('preview')) await previewEntry(entry, entryPath(entry));
    else if (entry.capabilities?.includes('download')) await downloadEntry(entry);
    else setStatus('This published resource can no longer be previewed or downloaded.', true);
    return;
  }
  if (!entry.capabilities?.includes('open')) {
    setStatus('This item does not expose a source-app action.', true);
    return;
  }
  await openManagedEntry(entry);
}

async function addHostLocation() {
  try {
    const controller = await import('./fileLocationController.js');
    const added = await controller.openFileLocationWizard({
      suggestedPath: state.hostPath || state.defaultPath || '',
    });
    setStatus(added ? 'Host Location added.' : 'Add Location closed.');
  } catch (error) {
    setStatus(error.message || 'Location could not be added', true);
  }
}

function handleWindowClosed() {
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
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  state.contextAdapterDispose?.();
  state.contextAdapterDispose = null;
  closeManagedActionMenu();
  closePreview();
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
  closeManagedActionMenu();
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
  if (!state.contentController && !state.navigationController) return;
  abortFilesAuthorityRequests();
}

function clearFilesAuthorityContent(message = 'File access changed') {
  cancelReveal();
  cancelFilesGesture('policy-change');
  clearKeepBothRetry();
  state.contentGeneration += 1;
  state.contentController?.abort();
  state.contentController = null;
  stopActiveFolderWatch();
  clearNativeThumbnailCache();
  closeManagedActionMenu();
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
    setStatus(`${state.entries.length}${state.nextCursor ? '+' : ''} items`);
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

async function handleFilesPolicyChanged() {
  if (!state.nativeWindow?.visible) return false;
  const snapshot = captureFilesProjection();
  const lifecycle = abortFilesAuthorityRequests();
  const startingContent = state.contentGeneration;
  setStatus('Revalidating file access…');
  try {
    await loadNavigationRoots({ force: true });
  } catch (error) {
    if (error?.name !== 'AbortError' && lifecycle === state.lifecycleGeneration) {
      setStatus('File access could not be revalidated; the current view was retained', true);
    }
    return false;
  }
  if (lifecycle !== state.lifecycleGeneration || state.contentGeneration !== startingContent || !state.nativeWindow?.visible) return false;

  if (snapshot.owner && state.owner !== snapshot.owner) {
    clearFilesAuthorityContent('Account file access changed');
    await openProvider(snapshot.provider || 'host');
    return false;
  }

  if (snapshot.provider === 'host' && snapshot.hostPath) {
    const result = await refreshRawFilesProjection(snapshot, lifecycle);
    if (lifecycle !== state.lifecycleGeneration || result.status === 'stale') return false;
    if (result.status === 'resolved') return true;
    if (result.status === 'unavailable') {
      setStatus('Current file access could not be refreshed; the prior view was retained', true);
      return false;
    }
    clearFilesAuthorityContent('The saved folder has expired; choose an authorized Files folder again');
    await openProvider('host');
    return false;
  }

  const result = await refreshManagedFilesProjection(snapshot, lifecycle);
  if (lifecycle !== state.lifecycleGeneration || result.status === 'stale') return false;
  if (result.status === 'resolved') return true;
  if (result.status === 'unavailable') {
    setStatus('Current file access could not be refreshed; the prior view was retained', true);
    return false;
  }

  // The current-policy root/parent listing succeeded but the selected resource
  // disappeared, or the server authoritatively rejected it. Remove every old
  // path/ref before opening the nearest current-policy provider landing page.
  clearFilesAuthorityContent('The previous folder is no longer available');
  await openProvider(snapshot.provider || 'host');
  return false;
}

async function handleAuthUserReady(event) {
  const announcedOwner = String(event?.detail?.username || '').trim();
  if (!state.owner || announcedOwner === state.owner) return;

  // Treat the event only as a revocation signal. The subsequent authenticated
  // server responses, not browser-supplied event detail, establish the new
  // owner and visible roots.
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
  closeManagedActionMenu();
  closePreview();
  destroyExplorerTree();
  state.owner = '';
  state.navigation?.reset({ account: '', workspace: copalWorkspace() });
  state.path = '';
  state.hostPath = '';
  state.entries = [];
  state.nextCursor = null;
  state.defaultPath = '';
  state.navigationGeneration = null;
  state.navigationRoots = [];
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
    await loadNavigationRoots({ force: true });
    if (lifecycle !== state.lifecycleGeneration || !state.nativeWindow?.visible) return;
    const opaqueOpened = await openProvider('host');
    if (!opaqueOpened) setStatus('No authorized Host folder is available', true);
  } catch (error) {
    if (error?.name !== 'AbortError' && lifecycle === state.lifecycleGeneration) {
      setStatus(error.message || 'Available files could not be reloaded', true);
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
    subtitle: 'Copal · Host and library',
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
  const collectionsSection = el('section', { class: 'files-sidebar-section' }, el('h2', { class: 'files-sidebar-heading', text: 'Open Clank' }), el('div', { 'data-files-collections': '', class: 'files-tree', role: 'tree', 'aria-label': 'Open Clank collections' }));
  sidebar.append(favoritesSection, workspacesSection, worktreeSection, collectionsSection);
  for (const [index,section] of [...sidebar.children].entries()) {
    const ids = ['favorites','workspaces','locations','applications'];
    const heading=section.querySelector('h2'), content=heading.nextElementSibling;
    const label=heading.textContent, categoryId=ids[index]; section.dataset.filesCategory=categoryId;
    const toggle=el('button',{type:'button',class:'files-category-toggle','aria-expanded':'true',text:'▾ '+label}); heading.replaceChildren(toggle);
    explorerLayout.register({id:categoryId,label,group:'places',element:()=>section,content:()=>content,
      sync:record=>{toggle.setAttribute('aria-expanded',String(!record.collapsed));const text=(record.collapsed?'▸ ':'▾ ')+label;if(toggle.textContent!==text)toggle.textContent=text;},
    });
    toggle.addEventListener('click',()=>{const record=explorerLayout.records().find(item=>item.id===categoryId);explorerLayout.update(categoryId,{collapsed:!record.collapsed});});
    heading.addEventListener('contextmenu',event=>{event.preventDefault();event.stopPropagation();customizeExplorer();});
  }
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
    onClick: () => { if (options.pickerMode) return; void (async () => {
      try {
        const actions = await import('./filesLibraryActions.js');
        const created = await actions.createLibraryDocument({ sessionId: window.sessionModule?.getCurrentSessionId?.() || null });
        if (created?.id && typeof window.documentModule?.loadDocument === 'function') await window.documentModule.loadDocument(created.id);
        await reloadManagedColumn(state.columns.length - 1);
        setStatus('Created Library document');
      } catch (error) { setStatus(error?.message || 'Library document could not be created', true); }
    })(); },
  });
  libraryCreate.dataset.filesLibraryCreate = '';
  libraryCreate.hidden = true;
  const libraryImport = iconButton({
    glyph: 'folder-plus', text: 'Import', title: 'Import Library documents',
    onClick: () => { if (!options.pickerMode) state.shell?.querySelector('[data-files-library-input]')?.click(); },
  });
  libraryImport.dataset.filesLibraryImport = '';
  libraryImport.hidden = true;
  const libraryInput = el('input', { type: 'file', multiple: 'true', hidden: 'true', 'data-files-library-input': '' });
  libraryInput.addEventListener('change', () => { void (async () => {
    const files = Array.from(libraryInput.files || []);
    libraryInput.value = '';
    if (options.pickerMode || !files.length) return;
    try {
      const actions = await import('./filesLibraryActions.js');
      const results = await actions.importLibraryDocuments(files);
      await reloadManagedColumn(state.columns.length - 1);
      setStatus('Imported ' + Number(results?.imported || 0)
        + (results?.failed ? ' · ' + results.failed + ' failed' : ''));
    } catch (error) { setStatus(error?.message || 'Library import failed', true); }
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
  const add = iconButton({ glyph: 'folder-plus', text: 'Add location', title: 'Add host location', onClick: addHostLocation });
  const openButton = iconButton({ glyph: 'folder-open', text: 'Open', title: 'Open selected in source app', onClick: openSelected });
  const downloadButton = iconButton({ glyph: 'download', text: 'Download', title: 'Download selected', onClick: downloadSelected });
  const actionsButton = iconButton({ glyph: 'archive', text: 'Actions', title: 'Select one actionable managed item', onClick: openSelectedActions });
  actionsButton.dataset.filesActions = '';
  actionsButton.querySelector('span:not(.files-button-glyph)')?.classList.add('files-actions-label');
  actionsButton.disabled = true;
  const favoriteButton = iconButton({ glyph: 'star', title: 'Add current folder to Favorites', onClick: toggleCurrentFavorite });
  favoriteButton.classList.add('files-favorite-toggle');
  favoriteButton.dataset.filesFavoriteToggle = '';
  favoriteButton.setAttribute('aria-pressed', 'false');
  const provider = el('select', { class: 'files-provider-select', 'aria-label': 'Files source' });
  for (const [value, label] of [[CLANKER_HOME_PROVIDER, 'Clanker home'], ['host', 'Host locations'], ['all', 'All sources'], ['copal', 'Copal'], ['files', 'Images'], ['library', 'Library']]) provider.append(el('option', { value, text: label }));
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
  const operationShelf = el('div', { class: 'files-operation-shelf', hidden: 'true' }, newButton, libraryCreate, libraryImport, librarySelect, libraryInput, add, openButton, downloadButton, previewButton, actionsButton, foldersFirst);
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
  if (!options.pickerMode) state.contextAdapterDispose = registerAdapter(body, canvasMenuAdapter());
  main.append(toolbar, pathLabel, searchNotice, previewPanel, body);
  if (options.pickerMode) favoriteButton.hidden = true;
  const more = state.loadMoreButton = el('button', { type: 'button', class: 'files-content-more', hidden: true, text: 'Load more' });
  more.addEventListener('click', () => void loadManagedMore());
  main.append(more, status);
  if (options.navigationOnly) { state.shell.classList.add('files-navigation-only'); main.hidden = true; separator.hidden = true; }
  state.shell.append(sidebar, separator, main);
  state.nativeWindow.body.append(state.shell);
  state.pane = createResizablePane({ container: state.shell, sidebar, main, separator, cssVar: '--files-sidebar-width', storageKey: 'odysseus-files-pane-width', defaultWidth: 220, minWidth: 170, maxWidth: 420, mainMin: 360, getOwner: () => state.owner });
  listen(window, 'modal-dismissed', (event) => {
    if (event.detail?.id === state.nativeWindow.id) state.nativeWindow.requestClose();
  });
  listen(window, 'odysseus:modal-minimized', (event) => {
    if (event.detail?.id === (options.parentWindow?.id || state.nativeWindow.id)) { cancelFilesGesture('window-hidden'); stopActiveFolderWatch(); resetNativeThumbnails(); closeManagedActionMenu(); }
  });
  listen(window, 'odysseus:modal-opened', (event) => {
    if (event.detail?.id === (options.parentWindow?.id || state.nativeWindow.id)) syncActiveFolderWatch();
    else if (event.detail?.id) cancelFilesGesture('modal-interruption');
  });
  listen(document, 'openclank:auth-user-ready', (event) => {
    void handleAuthUserReady(event);
  });
  listen(document, 'openclank:file-policy-changed', () => {
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
  listen(document, 'pointerdown', (event) => {
    if (state.actionMenu && !state.actionMenu.contains(event.target)) closeManagedActionMenu();
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
      const model = ensureSelectionModel(activeColumn());
      model.selectAll();
      applyModelSelection(model);
      event.preventDefault();
      event.stopPropagation();
      setStatus(`${model.selectedKeys().length}${state.nextCursor ? '+' : ''} items selected`);
      return;
    }
    if (event.key === 'Escape' && state.actionMenu) {
      event.preventDefault();
      event.stopPropagation();
      closeManagedActionMenu();
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
  if (!options.pickerMode) acknowledgeVisible(state.shell, 'surface.visited', { surface:'files', visited:true });
  if (!options.pickerMode && !state.contextAdapterDispose) state.contextAdapterDispose = registerAdapter(state.shell.querySelector('[data-files-body]'), canvasMenuAdapter());
  const lifecycle = ++state.lifecycleGeneration;
  const priorProjection = captureFilesProjection();
  const startingContent = state.contentGeneration;
  setStatus('Loading available files…');
  try {
    await loadNavigationRoots();
    if (lifecycle !== state.lifecycleGeneration || state.contentGeneration !== startingContent || !windowApi.visible) return windowApi;
    // A new Files window starts at Clanker home. Reopening first resolves the
    // retained projection against current owner-bound refs, rather than
    // displaying expired rows from a previous window lifetime.
    if (state.columns.length && priorProjection.owner === state.owner) {
      const refreshed = await refreshManagedFilesProjection(priorProjection, lifecycle);
      if (refreshed.status === 'resolved') return windowApi;
      if (refreshed.status === 'stale') return windowApi;
      clearFilesAuthorityContent('The previous Files folder is no longer available');
    }
    const opaqueOpened = await openProvider(CLANKER_HOME_PROVIDER);
    if (opaqueOpened) return windowApi;
    state.entries = [];
    state.nextCursor = null;
    renderEntries();
    setStatus('No authorized Host folder is available', true);
  } catch (error) {
    if (lifecycle === state.lifecycleGeneration) {
      setStatus(error.message || 'Available files could not be loaded', true);
      const body = state.shell?.querySelector('[data-files-body]');
      if (body) body.replaceChildren(el('div', { class: 'files-error', text: error.message || 'Available files could not be loaded' }));
    }
  }
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
  await loadNavigationRoots();
  const opened = await openProvider(CLANKER_HOME_PROVIDER);
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
  const trees = [state.workspaceTree, state.favoriteTree, state.explorerTree, state.collectionTree];
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
async function revealResource(resourceRef) {
  const windowApi = mount();
  windowApi.show();
  Modals.restore(state.nativeWindow.id);
  cancelReveal();
  const reveal = state.revealGeneration;
  const revealController = state.revealController = new AbortController();
  setStatus('Locating resource…');
  try {
    let response;
    try {
      response = await filesFacadeClient.reveal(resourceRef, {signal:revealController.signal});
    } catch (error) {
      if (error?.code !== 'resource_ref_stale') throw error;
      const renewed = await filesFacadeClient.reissue(resourceRef, {signal:revealController.signal});
      const renewedRef = String(renewed?.resource?.ref || '');
      if (!renewedRef) throw new Error('Resource could not be renewed');
      response = await filesFacadeClient.reveal(renewedRef, {signal:revealController.signal});
    }
    if (reveal !== state.revealGeneration) return false;
    await loadNavigationRoots();
    if (reveal !== state.revealGeneration) return false;
    windowApi.show();
    const ancestors = (
      Array.isArray(response?.ancestors) && response.ancestors.length
        ? response.ancestors
        : [response?.parent]
    ).filter(Boolean).map(facadeEntry).filter(editorResourceAllowed);
    let parent = ancestors.at(-1);
    const target = facadeEntry(response?.resource);
    if (
      !parent?.resource_ref
      || !target.resource_id
      || ancestors.some((ancestor) => !ancestor.resource_ref || !isDirectory(ancestor))
    ) {
      throw new Error('Resource is unavailable');
    }
    const navigation = await expandRevealTrees(ancestors,target,reveal);
    if (reveal !== state.revealGeneration || !navigation.length) return false;
    parent = navigation.at(-1);
    for (let index = 0; index < navigation.length; index += 1) {
      const opened = await openManagedDirectory(navigation[index], index === 0 ? null : index - 1, {reveal});
      if (!opened || reveal !== state.revealGeneration || !windowApi.visible) return false;
    }
    if (target.resource_id !== parent.resource_id) {
      const listed = state.entries.find((entry) => entry.resource_id === target.resource_id);
      if (!listed) throw new Error('Resource is no longer in this folder');
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

function captureCommands() {
  const treeEntry = state.treeSelection || null;
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
  } else model = ensureSelectionModel(activeColumn());
  const selection = captureFilesSelectionGuard(model);
  const destination = treeEntry ? (isDirectory(treeEntry) ? treeEntry : treeParent) : activeColumn();
  const directory = destination ? { ...destination, resourceRef:destinationResourceRef(destination), resourceId:destination.resource_id || destination.resourceId } : null;
  return Object.freeze({ browser: api, lifecycle: state.lifecycleGeneration, content: state.contentGeneration, navigating: Boolean(state.contentController),
    owner: state.owner, policy: filesPolicyGeneration(), selection, treeEntry,
    entries: Object.freeze([...model.selectedEntries()]), directory,
    creation: directory ? creationDestinationForEntry({ ...directory, kind:'folder' }, state.activeColumnIndex) : null });
}
function isCommandCaptureCurrent(snapshot) {
  const treeEntry = state.treeSelection || null;
  const destination = treeEntry ? (isDirectory(treeEntry) ? treeEntry : treeEntry.tree_ancestors?.at(-1)) : activeColumn();
  const destinationId = String(destination?.resource_id || destination?.resourceId || '');
  const capturedId = String(snapshot?.directory?.resource_id || snapshot?.directory?.resourceId || '');
  return !snapshot?.navigating && !state.contentController
    && destinationResourceRef(destination) === String(snapshot?.directory?.resourceRef || '')
    && destinationId === capturedId && snapshot?.browser === api && !listeners.signal.aborted && state.nativeWindow?.visible
    && snapshot.lifecycle === state.lifecycleGeneration && snapshot.content === state.contentGeneration
    && snapshot.owner === state.owner && snapshot.policy === filesPolicyGeneration()
    && snapshot.treeEntry === (state.treeSelection || null)
    && Boolean(validateFilesSelectionGuard(snapshot.selection));
}
function commandDescriptors(snapshot) {
  const descriptors = [];
  const add = (name, label, menu, run, reason = '', checked = undefined) => descriptors.push({ id: 'files.' + name, label, menu, run, disabledReason: snapshot.navigating ? 'Wait for folder navigation to finish; reopen this menu.' : reason, checked });
  const entries = snapshot.entries;
  const single = entries.length === 1 ? entries[0] : null;
  const requiredSingle = single ? '' : 'Select one item.';
  add('new-file', 'New file…', 'File', () => createFilesResource('file', snapshot.creation), snapshot.creation ? '' : 'This folder does not support creating files.');
  add('new-folder', 'New folder…', 'File', () => createFilesResource('folder', snapshot.creation), snapshot.creation && snapshot.creation.creationSupportsFolders !== false ? '' : 'This folder does not support creating folders.');
  add('open', 'Open', 'File', () => isDirectory(single) ? openManagedDirectory(single) : options.pickerMode ? confirmPickerEntry(single) : openManagedEntry(single), requiredSingle || (!single?.capabilities?.includes('open') && !isDirectory(single) ? 'This provider cannot open this item.' : ''));
  add('download', 'Download', 'File', async () => { for (const entry of entries) await downloadEntry(entry); }, entries.length && entries.every(entry => entry.capabilities?.includes('download')) ? '' : 'Select downloadable files.');
  add('information', 'Resource information', 'File', () => uiModule.styledConfirm([single.name, 'Type: ' + (single.kind || single.media_type || 'File'), 'Provider: ' + (single.provider || state.provider), ...(single.size != null ? ['Size: ' + formatBytes(single.size)] : []), ...(single.modified_unix_ms ? ['Modified: ' + formatModified(single.modified_unix_ms)] : [])].join('\n'), { title:'Resource information',confirmText:'Done',cancelText:'Close' }), requiredSingle);
  add('customize-explorer', 'Customize Explorer…', 'View', customizeExplorer);
  add('preview', 'Preview', 'View', () => previewEntry(single, entryPath(single)), requiredSingle || (isPreviewable(single, entryPath(single)) ? '' : 'No preview is available.'));
  const caps = entries.map(transferCapabilities);
  for (const [kind,label] of [['copy','Copy'],['move','Cut']]) add(kind === 'move' ? 'cut' : kind, label, 'Edit', () => {
    const model = validateFilesSelectionGuard(snapshot.selection);
    return setFilesClipboard(model, kind, () => isCommandCaptureCurrent(snapshot));
  }, snapshot.treeEntry && !validateFilesSelectionGuard(snapshot.selection)?.scope.parentRef ? 'This tree resource has no authorized parent; open its containing folder first.' : entries.length && entries.length <= FILES_MAX_DRAG_ITEMS && caps.every(cap => cap[kind]) ? '' : `Select up to ${FILES_MAX_DRAG_ITEMS} transferable items.`);
  add('paste', 'Paste', 'Edit', async () => {
    const destination = { ...snapshot.directory, resource_ref:snapshot.directory.resourceRef, kind:'folder' };
    return pasteFilesClipboard(destination, destinationColumnIndex(destination), () => isCommandCaptureCurrent(snapshot));
  }, filesClipboard && snapshot.directory?.capabilities?.includes('write') && filesClipboard.payload.provider === snapshot.directory?.provider ? '' : 'Copy or cut files from the same provider into a writable folder.');
  add('select-all', 'Select all', 'Edit', () => { const model = ensureSelectionModel(activeColumn()); model.selectAll(); applyModelSelection(model); });
  const choices = single ? entryActionChoices(single) : commonActionChoices(entries);
  for (const choice of choices) {
    if (choice.action.startsWith('transfer.')) continue;
    add('action-' + choice.action, choice.label, ['rename','trash','restore','archive.set'].includes(choice.action) ? 'Edit' : 'File', () => entries.length > 1 ? performSelectedBulkAction(entries, choice, snapshot.selection) : performManagedAction(single, choice));
  }
  for (const mode of ['list','grid','details','columns','gallery']) add('view-' + mode, mode[0].toUpperCase() + mode.slice(1), 'View', () => { const control = state.shell.querySelector('.files-mode-select'); control.value = mode; control.dispatchEvent(new Event('change')); }, '', state.mode === mode);
  for (const [value,label] of FILE_SORT_OPTIONS) add('sort-' + value, 'Sort: ' + label, 'View', () => { const [key,direction] = value.split(':'); return applyPrimarySort({ ...state.sort,key,direction }); });
  add('folders-first', 'Folders first', 'View', () => applyPrimarySort({ ...state.sort, directoriesFirst: !state.sort.directoriesFirst }), '', state.sort.directoriesFirst);
  for (const value of ['grid','gallery'].includes(state.mode) ? [32,64,96,128] : [16,20,24,32]) add('size-' + value, 'Icon size: ' + value + ' px', 'View', () => { const size = state.shell.querySelector('.files-size-select'); size.value = value; size.dispatchEvent(new Event('change')); });
  add('navigation', 'Navigation sidebar', 'View', toggleNavigation, '', state.shell.classList.contains('files-navigation-open'));
  add('refresh', 'Refresh folder', 'View', () => api.refresh());
  add('back', 'Back in folder history', 'Go', () => state.navigation.back(), state.navigation.canGoBack() ? '' : 'No previous folder.');
  add('forward', 'Forward in folder history', 'Go', () => state.navigation.forward(), state.navigation.canGoForward() ? '' : 'No next folder.');
  add('up', 'Parent folder', 'Go', managedUp);
  add('home', 'Home', 'Go', () => openProvider(CLANKER_HOME_PROVIDER));
  add('favorite', 'Favorite current folder', 'Go', toggleCurrentFavorite, state.provider === 'host' && snapshot.directory?.resourceRef ? '' : 'Open a Host folder to save a Favorite.');
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
  explorerLayout, customizeExplorer,
  id, hooks, parentWindow: options.parentWindow, captureCommands, isCommandCaptureCurrent, commandDescriptors, open, openLibraryCollection, revealResource, revealWorkspaceResource,
  get element() { return state.shell; }, get sidebar() { return state.sidebar; }, get sidebarSeparator() { return state.sidebarSeparator; }, get window() { return state.nativeWindow; },
  getCurrentDirectory: () => { const column = activeColumn(); return column?.resourceRef ? { ...column, resource_ref:column.resourceRef, resource_id:column.resourceId, kind:'folder' } : null; },
  getSelection: () => state.treeSelection ? [state.treeSelection] : ensureSelectionModel(activeColumn()).selectedEntries(),
  getContext: () => hooks.__odysseusGetActiveFilesContext(),
  openDirectory: entry => openManagedDirectory(facadeEntry(entry), null),
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
export function focusOtherPane() { const pair = [...splits.values()].find(pair => pair.primary === activeBrowser || pair.browser === activeBrowser); if (!pair) return false; const target = activeBrowser === pair.primary ? pair.browser : pair.primary; target.activate(); target.element.querySelector('[data-files-body]')?.focus(); return true; }
export default { open: () => defaultBrowser().open(), newFile, newWindow, split, closeSplit, focusOtherPane, getActiveBrowser, createFilesBrowser, openLibraryCollection, revealResource, revealWorkspaceResource, close: () => activeBrowser?.window?.requestClose() };
