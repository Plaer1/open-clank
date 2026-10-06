import { recordPresentation, achievementOwner, activityDigest, visiblePresentation } from './achievementProducer.js';
import { LANGUAGE_REGISTRY } from './editor/languageRegistry.js';
import { filesFacadeClient } from './filesFacadeClient.js';
import { createOpenClankWindow } from './copal/windows.js';
import { appletPath } from './appletRoutes.js';
import workspaceModule from './workspace.js';
import { styledConfirm, styledPrompt } from './ui.js';
import { createResizablePane } from './editor/resizablePane.js';
import { createExplorerTree } from './editor/explorerTree.js';
import { createResourcePicker, normalizeAuthorizedResource } from './copal/resourcePicker.js';
import { createMarkdownRenderer } from './copal/markdownRenderer.js';
import { languageForPath as sharedLanguageForPath, languageDialectForPath as sharedLanguageDialectForPath, sortEntries as sortEntryList, isDirectory as entryIsDirectory } from './editor/entryModel.js';
import { fileIcon, glyphIcon } from './langIcons.js';
import { initCustomContextMenu, registerAdapter, createCodeMirrorContextAdapter } from './custom-context-menu.js';

const CODE_WORKSPACE_KEY = 'odysseus-code-workspace';
const CODE_WORKSPACE_ID_KEY = 'odysseus-code-workspace-id';
const CODE_RICH_COMMENTS_KEY = 'odysseus-editor-rich-comments';
const hydratedCodeTreeContextEvents = new WeakSet();
const codeTreeContextHydration = new WeakMap();
let codeTreeContextRequestSequence = 0;

// Code Editor is also mounted directly by the Copal launcher, so initialize
// the shared menu at the first component boundary instead of relying only on
// the main shell's script order. The helper is idempotent for the full app and
// keeps fixture/embedded Editor surfaces on the same interaction contract.
if (typeof document !== 'undefined' && document.body) initCustomContextMenu();

const state = {
  shell: null,
  nativeWindow: null,
  root: '',
  rootResourceRef: '',
  activeDirectoryRef: '',
  explorerTree: null,
  buffers: new Map(),
  activePath: null,
  activeDirectory: '',
  workspaceOpenRelative: '',
  requestGeneration: 0,
  fileGeneration: 0,
  fileActivationSequence: 0,
  pendingFileReads: new Map(),
  treeMutations: new Set(),
  workspaceEpoch: 0,
  workspaceOwner: '',
  rootTreeController: null,
  resourceOpenController: null,
  bufferReconcileController: null,
  workspacePicker: null,
  pane: null,
  editorFactory: null,
  richCommentLanguageQualified: () => false,
  editorLoadPromise: null,
  editorLoadError: null,
  closedBuffers: [],
  closePromise: null,
  tabMenu: null,
  richCommentsDefault: false,
  markdownRenderer: null,
};

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

/**
 * Full shared Markdown renderer for Code Editor rich comments. Explicit
 * callbacks replace any applet-singleton import so this window never reaches
 * into another window's Copal state. Resource origin is the active host file.
 */
function codeEditorMarkdownRenderer() {
  if (state.markdownRenderer) return state.markdownRenderer;
  const renderer = createMarkdownRenderer({
    h: el,
    documents: () => [],
    findByName: () => null,
    assetUrl: (target) => {
      // Relative media paths resolve beside the active source file.
      const name = String(target?.name || target?.target || target?.id || '');
      if (!name || /^(?:https?:|data:|blob:)/i.test(name)) return name || null;
      const base = String(state.activePath || '').replace(/[^/]+$/, '');
      return `${base}${name.replace(/^\.\//, '')}`;
    },
    openTarget: (target, fragment, event) => {
      if (event?.metaKey || event?.ctrlKey || event?.shiftKey || event?.altKey) return;
      const href = typeof target === 'string' ? target : String(target?.href || target?.url || target?.name || '');
      if (/^https?:/i.test(href)) { window.open(href, '_blank', 'noopener,noreferrer'); return; }
      if (fragment || href) setStatus(`Reference: ${href || 'fragment'}${fragment ? `#${fragment}` : ''}`);
    },
  });
  state.markdownRenderer = renderer;
  return renderer;
}

function toolButton(title, glyph, onClick) {
  const button = el('button', { type: 'button', title, 'aria-label': title });
  button.innerHTML = glyphIcon(glyph, 15, { className: 'code-toolbar-glyph' });
  button.addEventListener('click', onClick);
  return button;
}

function ownerWorkspaceKey(owner = state.workspaceOwner) {
  return owner ? `${CODE_WORKSPACE_KEY}:${encodeURIComponent(owner)}` : '';
}

function ownerWorkspaceIdKey(owner = state.workspaceOwner) {
  return owner ? `${CODE_WORKSPACE_ID_KEY}:${encodeURIComponent(owner)}` : '';
}

function richCommentsKey(owner = state.workspaceOwner) {
  return owner ? `${CODE_RICH_COMMENTS_KEY}:${encodeURIComponent(owner)}` : '';
}

function savedRichCommentsDefault(owner = state.workspaceOwner) {
  try { const key = richCommentsKey(owner); return key ? localStorage.getItem(key) === '1' : false; } catch { return false; }
}

function saveRichCommentsDefault(value, owner = state.workspaceOwner) {
  try { const key = richCommentsKey(owner); if (key) localStorage.setItem(key, value ? '1' : '0'); } catch (_) { /* optional browser preference */ }
}

function savedWorkspaceRoot(owner = state.workspaceOwner) {
  try {
    const scoped = ownerWorkspaceKey(owner);
    if (scoped) return localStorage.getItem(scoped) || '';
    return '';
  } catch { return ''; }
}

function savedWorkspaceId(owner = state.workspaceOwner) {
  try {
    const scoped = ownerWorkspaceIdKey(owner);
    return scoped ? localStorage.getItem(scoped) || '' : '';
  } catch { return ''; }
}

function saveWorkspaceRoot(path, owner = state.workspaceOwner, workspaceId = '') {
  try {
    const key = ownerWorkspaceKey(owner);
    const idKey = ownerWorkspaceIdKey(owner);
    if (!key) return;
    if (path) localStorage.setItem(key, path);
    else localStorage.removeItem(key);
    if (path && workspaceId) localStorage.setItem(idKey, workspaceId);
    else localStorage.removeItem(idKey);
  } catch (_) { /* browser-local persistence is optional UI state */ }
}

async function authenticatedOwner() {
  try {
    const response = await fetch('/api/auth/status', { credentials: 'same-origin' });
    // Authentication transport failures are not evidence that the principal
    // changed. Keep that state distinct so a transient 5xx cannot destroy
    // dirty buffers or another owner's persisted workspace selection.
    if (!response.ok) {
      return {
        status: [401, 403].includes(response.status) ? 'invalid' : 'unavailable',
        owner: null,
      };
    }
    const data = await response.json();
    return { status: 'confirmed', owner: String(data?.username || '').trim() };
  } catch (_) {
    return { status: 'unavailable', owner: null };
  }
}

function abortPendingFileReads() {
  for (const [path, pending] of state.pendingFileReads) {
    pending.controller.abort();
    if (state.buffers.get(path) === pending.buffer && !pending.buffer.loaded) {
      state.buffers.delete(path);
    }
  }
  state.pendingFileReads.clear();
}

function clearWorkspaceOwnerState(nextOwner = '') {
  state.workspaceEpoch += 1;
  state.resourceOpenController?.abort?.();
  state.resourceOpenController = null;
  state.fileGeneration += 1;
  abortPendingFileReads();
  state.rootTreeController?.abort();
  state.rootTreeController = null;
  state.bufferReconcileController?.abort?.();
  state.bufferReconcileController = null;
  state.explorerTree?.destroy?.();
  state.explorerTree = null;
  for (const buffer of state.buffers.values()) {
    buffer.saveController?.abort?.();
    buffer.reloadController?.abort?.();
    buffer.contextMenuDispose?.();
    buffer.contextMenuDispose = null;
    buffer.editor?.destroy?.();
  }
  state.buffers.clear();
  state.activePath = null;
  state.activeDirectory = '';
  state.activeDirectoryRef = '';
  state.closedBuffers.length = 0;
  // A stale dirty-close transaction may still resolve its dialog. Its captured
  // workspace epoch prevents it from touching the new owner's buffers.
  state.closePromise = null;
  state.root = '';
  state.rootResourceRef = '';
  state.activeDirectoryRef = '';
  state.workspaceOwner = String(nextOwner || '');
  state.richCommentsDefault = savedRichCommentsDefault(state.workspaceOwner);
  const rootLabel = state.shell?.querySelector('[data-code-root]');
  if (rootLabel) rootLabel.textContent = '';
  closeTabMenu();
  updateReopenClosedAction();
  state.pane?.refresh?.();
}

function abortWorkspaceAuthorityRequests() {
  const generation = ++state.requestGeneration;
  state.workspaceEpoch += 1;
  state.fileGeneration += 1;
  abortPendingFileReads();
  state.rootTreeController?.abort();
  state.rootTreeController = null;
  state.bufferReconcileController?.abort?.();
  state.bufferReconcileController = null;
  for (const buffer of state.buffers.values()) {
    // A policy generation change invalidates an in-flight write even when the
    // selected folder ultimately remains visible. The buffer stays dirty and
    // can be saved again after current authority is confirmed.
    buffer.saveController?.abort?.();
    buffer.reloadController?.abort?.();
  }
  closeTabMenu();
  return generation;
}

function confirmedWorkspaceFailure(error) {
  return [400, 401, 403, 404, 410, 422].includes(Number(error?.status || 0));
}

function purgeWorkspaceAuthority(owner, message = 'Workspace access changed') {
  saveWorkspaceRoot('', owner);
  clearWorkspaceOwnerState(owner);
  mountCodeExplorer([]);
  renderEditor();
  setStatus(message, true);
}

function rebaseWorkspaceBuffers(nextRoot) {
  const previousRoot = state.root;
  if (!previousRoot || previousRoot === nextRoot) {
    state.root = nextRoot;
    return true;
  }
  const remap = path => {
    if (!path) return path;
    if (path === previousRoot) return nextRoot;
    try {
      const relative = workspaceRelativePath(path, previousRoot);
      return relative ? childPath(nextRoot, relative) : nextRoot;
    } catch (_) {
      return null;
    }
  };
  const mappings = [...state.buffers].map(([path, buffer]) => ({
    oldPath: path,
    nextPath: remap(path),
    buffer,
  }));
  if (mappings.some(entry => !entry.nextPath)) return false;
  state.buffers.clear();
  for (const { nextPath, buffer } of mappings) {
    buffer.path = nextPath;
    state.buffers.set(nextPath, buffer);
  }
  state.activePath = remap(state.activePath);
  state.activeDirectory = remap(state.activeDirectory) || nextRoot;
  for (const metadata of state.closedBuffers) {
    if (metadata.owner !== state.workspaceOwner || metadata.root !== previousRoot) continue;
    metadata.path = remap(metadata.path) || metadata.path;
    metadata.root = nextRoot;
  }
  state.root = nextRoot;
  updateReopenClosedAction();
  return true;
}

async function handleCodeFilePolicyChanged() {
  if (!state.nativeWindow?.visible) return false;
  const priorOwner = state.workspaceOwner;
  const priorRoot = state.root;
  const generation = abortWorkspaceAuthorityRequests();
  setStatus('Revalidating workspace access…');

  const identity = await authenticatedOwner();
  if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
  if (identity.status === 'unavailable') {
    renderEditor();
    setStatus('Workspace access could not be revalidated; unsaved changes were retained', true);
    return false;
  }
  if (identity.status === 'invalid' || !identity.owner) {
    purgeWorkspaceAuthority(priorOwner, 'Workspace access is no longer available');
    return false;
  }
  if (priorOwner && identity.owner !== priorOwner) {
    clearWorkspaceOwnerState(identity.owner);
    renderEditor();
    const nextRoot = await workspaceRoot();
    if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
    return displayWorkspaceRoot(nextRoot, generation);
  }
  state.workspaceOwner = identity.owner;
  state.richCommentsDefault = savedRichCommentsDefault(identity.owner);

  let resolvedRoot = priorRoot;
  const workspaceId = savedWorkspaceId(identity.owner);
  if (workspaceId) {
    try {
      const workspace = await workspaceModule.resolveWorkspaceId(workspaceId, 'app_folder');
      resolvedRoot = String(workspace.path || '');
      saveWorkspaceRoot(resolvedRoot, identity.owner, workspace.id);
    } catch (error) {
      if (generation !== state.requestGeneration) return false;
      if (!confirmedWorkspaceFailure(error)) {
        renderEditor();
        setStatus('Workspace access could not be revalidated; unsaved changes were retained', true);
        return false;
      }
      purgeWorkspaceAuthority(identity.owner, 'Workspace access was revoked');
      const fallback = await workspaceRoot();
      if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
      return displayWorkspaceRoot(fallback, generation);
    }
    if (typeof filesFacadeClient.workspaceResource === 'function') {
      try {
        const target = await filesFacadeClient.workspaceResource(workspaceId, '', {});
        if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
        const rootRef = String(target?.resource?.ref || target?.parent?.ref || '').trim();
        const rootKind = String(target?.resource?.kind || target?.parent?.kind || '').toLowerCase();
        if (!rootRef || !['folder', 'provider_root', 'virtual_folder'].includes(rootKind)) {
          purgeWorkspaceAuthority(identity.owner, 'Workspace resource is unavailable');
          return false;
        }
        state.rootResourceRef = rootRef;
      } catch (error) {
        if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
        if (confirmedWorkspaceFailure(error)) purgeWorkspaceAuthority(identity.owner, 'Workspace access was revoked');
        else { renderEditor(); setStatus('Workspace access could not be revalidated; unsaved changes were retained', true); }
        return false;
      }
    }
  } else if (state.rootResourceRef && typeof filesFacadeClient.stat === 'function') {
    try {
      const checked = await filesFacadeClient.stat(state.rootResourceRef, {});
      if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
      const rootRef = String(checked?.resource?.ref || checked?.ref || state.rootResourceRef).trim();
      const rootKind = String(checked?.resource?.kind || checked?.kind || '').toLowerCase();
      if (!rootRef || !['folder', 'provider_root', 'virtual_folder'].includes(rootKind)) {
        purgeWorkspaceAuthority(identity.owner, 'Workspace resource is unavailable');
        return false;
      }
      state.rootResourceRef = rootRef;
    } catch (error) {
      if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
      if (confirmedWorkspaceFailure(error)) purgeWorkspaceAuthority(identity.owner, 'Workspace access was revoked');
      else { renderEditor(); setStatus('Workspace access could not be revalidated; unsaved changes were retained', true); }
      return false;
    }
  } else if (priorRoot) {
    // A path-only prior root is stale compatibility state. Do not reauthorize
    // it through the workspace browse API during a policy transition.
    state.rootResourceRef = '';
    state.activeDirectoryRef = '';
    purgeWorkspaceAuthority(identity.owner, 'Workspace access must be reauthorized through Files.');
    return false;

    state.rootResourceRef = '';
    state.activeDirectoryRef = '';
    const validated = await resolveWorkspaceRoot(priorRoot);
    if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
    if (validated.status === 'unavailable') {
      renderEditor();
      setStatus('Workspace access could not be revalidated; unsaved changes were retained', true);
      return false;
    }
    if (validated.status !== 'resolved') {
      purgeWorkspaceAuthority(identity.owner, 'Workspace access was revoked');
      const fallback = await workspaceRoot();
      if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
      return displayWorkspaceRoot(fallback, generation);
    }
    resolvedRoot = validated.path;
  } else {
    resolvedRoot = await workspaceRoot();
  }

  if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
  if (!resolvedRoot) {
    purgeWorkspaceAuthority(identity.owner, 'No assigned folder');
    return false;
  }
  if (!rebaseWorkspaceBuffers(resolvedRoot)) {
    purgeWorkspaceAuthority(identity.owner, 'Workspace location changed; prior buffers were removed');
    return false;
  }

  try {
    const page = await loadInitialCodeRoot(state.rootResourceRef || state.activeDirectoryRef || resolvedRoot, generation);
    if (!page || generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
    const rootLabel = state.shell?.querySelector('[data-code-root]');
    if (rootLabel) rootLabel.textContent = resolvedRoot;
    mountCodeExplorer(page.items, page.nextCursor, resolvedRoot);
    renderEditor();
    await reconcileOpenBuffers(generation);
    if (generation === state.requestGeneration) setStatus(resolvedRoot);
    return generation === state.requestGeneration;
  } catch (error) {
    if (error?.name === 'AbortError' || generation !== state.requestGeneration) return false;
    if (confirmedWorkspaceFailure(error)) {
      purgeWorkspaceAuthority(identity.owner, 'Workspace access was revoked');
    } else {
      renderEditor();
      setStatus('Workspace refresh failed; unsaved changes were retained', true);
    }
    return false;
  }
}

async function handleAuthenticatedOwnerReady(nextOwner) {
  const next = String(nextOwner || '').trim();
  if (!next || next === state.workspaceOwner) return false;
  const visible = !!state.nativeWindow?.visible;
  const generation = ++state.requestGeneration;
  clearWorkspaceOwnerState(next);
  renderEditor();
  if (!visible) return true;
  setStatus('Loading workspace…');
  const root = await workspaceRoot();
  if (
    generation !== state.requestGeneration
    || state.workspaceOwner !== next
    || !state.nativeWindow?.visible
  ) return false;
  return displayWorkspaceRoot(root, generation);
}

async function resolveWorkspaceRoot(path = '') {
  try {
    const params = new URLSearchParams({ selection_kind: 'app_folder' });
    if (path) params.set('path', path);
    const response = await fetch(`/api/workspace/browse?${params.toString()}`, { credentials: 'same-origin' });
    if (!response.ok) {
      const authoritativeInvalid = [400, 403, 404, 410, 422].includes(response.status);
      return { status: authoritativeInvalid ? 'invalid' : 'unavailable', path: '' };
    }
    const data = await response.json();
    if (data?.path && data.selectable !== false) {
      return { status: 'resolved', path: String(data.path) };
    }
    if (!path) {
      const fallback = data?.dirs?.find?.(entry => entry?.path)?.path || '';
      return fallback
        ? { status: 'resolved', path: String(fallback) }
        : { status: 'empty', path: '' };
    }
    return { status: 'invalid', path: '' };
  } catch (_) {
    return { status: 'unavailable', path: '' };
  }
}

async function workspaceRoot() {
  const identity = await authenticatedOwner();
  const identityUnavailable = identity.status !== 'confirmed';
  let owner = state.workspaceOwner;
  if (identity.status === 'confirmed') {
    owner = identity.owner;
    if (state.workspaceOwner && owner !== state.workspaceOwner) {
      clearWorkspaceOwnerState(owner);
    } else {
      state.workspaceOwner = owner;
      state.richCommentsDefault = savedRichCommentsDefault(owner);
    }
  } else if (state.root) {
    // An identity outage cannot authorize the prior account's opaque root.
    // Keep dirty buffers in memory, but require a fresh Files identity before
    // the mounted Editor can browse or read anything again.
    throw new Error('Authenticated Files account is unavailable; choose the folder again.');
  }
  updateReopenClosedAction();
  state.pane?.refresh?.();

  // The mounted Editor obtains its initial folder from the same Files facade
  // projection as Files. The returned label is presentation only; all child
  // listing and file reads use the sealed root/resource refs.
  if (typeof filesFacadeClient.roots === 'function') {
    try {
      const roots = await filesFacadeClient.roots({ copalWorkspace: 'default' });
      const host = (roots?.entries || []).find((item) => item?.provider === 'host' && item?.capabilities?.includes('children'));
      if (host?.ref) {
        const page = await filesFacadeClient.children(host.ref, { limit: 200, sort: { key: 'name', direction: 'asc', directories_first: true } });
        const folder = (page?.entries || []).find((item) => item?.capabilities?.includes('children') && item?.provenance?.favorite)
          || (page?.entries || []).find((item) => item?.capabilities?.includes('children'));
        if (folder?.ref) {
          state.rootResourceRef = String(folder.ref);
          state.activeDirectoryRef = state.rootResourceRef;
          return String(folder.name || 'Workspace');
        }
      }
    } catch (error) {
      if (Number(error?.status || 0) !== 404) throw error;
    }
  }
  // The mounted Editor has no path-authority fallback. The compatibility
  // workspace browser below is retained for non-Copal callers only; this
  // production entry always fails closed when Files-v1 cannot issue a ref.
  throw new Error('No authorized Files folder is available. Choose one from Files.');

  const saved = savedWorkspaceRoot(owner);
  const stableId = savedWorkspaceId(owner);
  if (stableId) {
    try {
      const workspace = await workspaceModule.resolveWorkspaceId(stableId, 'app_folder');
      saveWorkspaceRoot(workspace.path, owner, workspace.id);
      return workspace.path;
    } catch (error) {
      if (!error?.status || error.status >= 500) return saved || state.root || '';
      if (!identityUnavailable) saveWorkspaceRoot('', owner);
    }
  }
  if (saved) {
    const validated = await resolveWorkspaceRoot(saved);
    if (validated.status === 'resolved') {
      try {
        const workspace = await workspaceModule.bindWorkspacePath(validated.path, 'app_folder');
        saveWorkspaceRoot(workspace.path, owner, workspace.id);
      } catch (_) {
        // A transient canonical-policy outage must not destroy dirty buffers or
        // the last server-vetted display hint. The folder is not newly persisted.
      }
      return validated.path;
    }
    if (validated.status === 'unavailable') return saved;
    if (validated.status === 'invalid') {
      // Without a confirmed principal, even an otherwise-authoritative folder
      // response cannot safely be attributed to the saved workspace owner.
      if (identityUnavailable) return saved;
      saveWorkspaceRoot('', owner);
    }
  }
  // An administrator's server-resolved default is their home directory. A
  // non-admin receives the first virtual assigned root. Neither path depends
  // on the async client-side admin flag, and `/` is never a default.
  const fallback = await resolveWorkspaceRoot();
  if (fallback.status === 'resolved') {
    try {
      const workspace = await workspaceModule.bindWorkspacePath(fallback.path, 'app_folder');
      saveWorkspaceRoot(workspace.path, owner, workspace.id);
      return workspace.path;
    } catch (_) {
      return fallback.path;
    }
  }
  return state.root || '';
}

function displayName(path) {
  const parts = String(path || '').replace(/\\/g, '/').split('/').filter(Boolean);
  return parts.at(-1) || path || '/';
}

function languageForPath(path) { return sharedLanguageForPath(path); }
function languageDialectForPath(path) { return sharedLanguageDialectForPath(path); }

function parentPath(path) {
  const normalized = String(path || '').replace(/\\/g, '/').replace(/\/$/, '');
  const index = normalized.lastIndexOf('/');
  return index > 0 ? normalized.slice(0, index) : '/';
}

function childPath(directory, name) {
  return `${String(directory || '').replace(/[\\/]$/, '')}/${String(name || '').trim()}`.replace('//', '/');
}

function workspaceRelativePath(path, root = state.root) {
  const candidate = String(path || '').replace(/\\/g, '/');
  let base = String(root || '').replace(/\\/g, '/');
  if (!candidate || !base) throw new Error('Workspace resource is unavailable');
  if (base !== '/') base = base.replace(/\/$/, '');
  if (candidate === base) return '';
  const prefix = base === '/' ? '/' : `${base}/`;
  if (!candidate.startsWith(prefix)) throw new Error('File is outside the current Workspace');
  const relative = candidate.slice(prefix.length);
  if (!relative || relative.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new Error('Workspace resource is invalid');
  }
  return relative;
}

function validChildName(name) {
  const value = String(name || '').trim();
  return value && value !== '.' && value !== '..' && !/[\\/]/.test(value) ? value : '';
}

function operationId(prefix = 'editor-operation') {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
}

function pathIsWithin(path, root) {
  const candidate = String(path || '');
  const prefix = String(root || '').replace(/[\\/]$/, '');
  return !!prefix && (candidate === prefix || candidate.startsWith(`${prefix}/`));
}

function remapSubtreePath(path, source, destination) {
  if (!pathIsWithin(path, source)) return path;
  return path === source ? destination : `${destination}/${path.slice(`${source}/`.length)}`;
}

function subtreeBufferEntries(source) {
  return [...state.buffers].filter(([path]) => pathIsWithin(path, source));
}

function beginTreeMutation(action, source, destination = '') {
  const reservation = {
    action,
    source,
    destination,
    owner: state.workspaceOwner,
    root: state.root,
    workspaceEpoch: state.workspaceEpoch,
    snapshots: new Map(subtreeBufferEntries(source).map(([path, buffer]) => [path, {
      buffer,
      revision: buffer?.revision || 0,
    }])),
  };
  state.treeMutations.add(reservation);
  return reservation;
}

function finishTreeMutation(reservation) {
  state.treeMutations.delete(reservation);
}

function treeMutationWorkspaceIsCurrent(reservation) {
  return state.workspaceEpoch === reservation.workspaceEpoch
    && state.workspaceOwner === reservation.owner
    && state.root === reservation.root;
}

function treeMutationBuffersChanged(reservation) {
  if (!treeMutationWorkspaceIsCurrent(reservation)) return true;
  return subtreeBufferEntries(reservation.source).some(([path, buffer]) => {
    const snapshot = reservation.snapshots.get(path);
    return !snapshot
      || snapshot.buffer !== buffer
      || (buffer?.revision || 0) !== snapshot.revision;
  });
}

function rekeyTreeMutationBuffers(reservation) {
  if (!treeMutationWorkspaceIsCurrent(reservation)) return [];
  const mappings = subtreeBufferEntries(reservation.source).map(([oldPath, buffer]) => ({
    oldPath,
    nextPath: remapSubtreePath(oldPath, reservation.source, reservation.destination),
    buffer,
    pending: state.pendingFileReads.get(oldPath) || null,
  }));
  for (const { oldPath } of mappings) {
    state.buffers.delete(oldPath);
    state.pendingFileReads.delete(oldPath);
  }
  for (const { nextPath, buffer, pending } of mappings) {
    buffer.path = nextPath;
    state.buffers.set(nextPath, buffer);
    if (pending) {
      pending.path = nextPath;
      state.pendingFileReads.set(nextPath, pending);
    }
  }
  state.activePath = remapSubtreePath(state.activePath, reservation.source, reservation.destination);
  state.activeDirectory = remapSubtreePath(state.activeDirectory, reservation.source, reservation.destination);
  for (const metadata of state.closedBuffers) {
    if (metadata.owner === reservation.owner && metadata.root === reservation.root) {
      metadata.path = remapSubtreePath(metadata.path, reservation.source, reservation.destination);
    }
  }
  updateReopenClosedAction();
  return mappings.map(({ nextPath }) => nextPath);
}

function retainOrphanedTrashBuffers(source) {
  const retained = [];
  for (const [path, buffer] of subtreeBufferEntries(source)) {
    if (!buffer?.loaded) {
      state.pendingFileReads.get(path)?.controller.abort();
      state.pendingFileReads.delete(path);
      state.buffers.delete(path);
      continue;
    }
    buffer.dirty = true;
    buffer.saveError = {
      code: 'trashed_while_editing',
      message: 'The host item moved to Trash while newer local edits were being made. The local buffer was retained.',
    };
    retained.push(path);
  }
  if (state.activePath && !state.buffers.has(state.activePath)) state.activePath = retained.at(-1) || null;
  updateReopenClosedAction();
  return retained;
}

function discardBufferChanges(buffer) {
  if (!buffer) return;
  for(const edit of buffer.pendingSourceEdits?.values?.() || [])edit.discard?.();
  buffer.pendingSourceEdits?.clear?.();
  const original = String(buffer.originalText ?? '');
  buffer.editor?.setValue?.(original);
  buffer.text = original;
  buffer.dirty = false;
  buffer.revision = buffer.savedRevision || 0;
  buffer.saveError = null;
}

function bindBufferContextMenu(buffer, host) {
  buffer.contextMenuDispose?.();
  buffer.contextMenuDispose = registerAdapter(host, createCodeMirrorContextAdapter(buffer.editor, {
    // These values are read when a command is invoked. A resource rename,
    // workspace switch, or account transition therefore invalidates a menu
    // capture even when the replacement has the same byte length.
    bufferIdentity: () => buffer,
    revision: () => `${buffer.path}:${buffer.revision || 0}`,
    scope: () => `${state.workspaceOwner}:${state.root}:${state.workspaceEpoch}`,
  }));
}

function loadedBufferPaths() {
  return [...state.buffers].filter(([, buffer]) => buffer.loaded).map(([path]) => path);
}

function adjacentOpenPath(path, closingPaths = []) {
  const paths = loadedBufferPaths();
  const index = paths.indexOf(path);
  if (index < 0) return null;
  const closing = new Set(closingPaths);
  for (let next = index + 1; next < paths.length; next += 1) {
    if (!closing.has(paths[next])) return paths[next];
  }
  for (let previous = index - 1; previous >= 0; previous -= 1) {
    if (!closing.has(paths[previous])) return paths[previous];
  }
  return null;
}

function closedBufferMetadata(path, buffer) {
  return {
    path,
    resourceRef: String(buffer?.resourceRef || ''),
    expectedRevision: buffer?.expectedRevision || null,
    owner: state.workspaceOwner,
    root: state.root,
    selection: buffer.editor?.getSelection?.() || buffer.selection || null,
    scrollTop: buffer.editor?.getScrollTop?.() ?? buffer.scrollTop ?? 0,
  };
}

function canReopenClosedBuffer() {
  return state.closedBuffers.some(entry => (
    entry.owner === state.workspaceOwner
    && entry.root === state.root
    && !state.buffers.has(entry.path)
  ));
}

function updateReopenClosedAction() {
  const button = state.shell?.querySelector('[data-code-reopen-closed]');
  if (button) button.disabled = !canReopenClosedBuffer();
}

function rememberClosedBuffer(path, buffer) {
  if (!buffer?.loaded) return;
  // Keep only resource/view metadata. Reopen always reads the current bytes
  // through the authorized file service; unsaved or historical content never
  // enters browser persistence or the closed-tab stack.
  state.closedBuffers.push(closedBufferMetadata(path, buffer));
  if (state.closedBuffers.length > 20) state.closedBuffers.splice(0, state.closedBuffers.length - 20);
}

function detachBuffers(paths, { remember = true } = {}) {
  const targets = [...new Set(paths)].filter(path => state.buffers.has(path));
  if (!targets.length) return;
  const successor = targets.includes(state.activePath)
    ? adjacentOpenPath(state.activePath, targets)
    : state.activePath;
  for (const path of targets) {
    const buffer = state.buffers.get(path);
    if (!buffer) continue;
    if (remember) rememberClosedBuffer(path, buffer);
    state.pendingFileReads.get(path)?.controller.abort();
    state.pendingFileReads.delete(path);
    buffer.saveController?.abort?.();
    buffer.reloadController?.abort?.();
    buffer.contextMenuDispose?.();
    buffer.contextMenuDispose = null;
    buffer.editor?.destroy?.();
    state.buffers.delete(path);
  }
  state.activePath = successor && state.buffers.has(successor) ? successor : null;
  state.fileActivationSequence += 1;
  updateReopenClosedAction();
}

async function resolveDirtyBuffers(paths, context = 'close', isCurrent = () => true) {
  if (!isCurrent()) return false;
  const workspaceEpoch = state.workspaceEpoch;
  const dirtyEntries = [...new Set(paths)]
    .map(path => [path, state.buffers.get(path)])
    .filter(([, buffer]) => buffer?.dirty);
  if (!dirtyEntries.length) return true;
  const dirty = dirtyEntries.map(([path]) => path);
  const stillCurrent = () => isCurrent() && workspaceEpoch === state.workspaceEpoch
    && dirtyEntries.every(([path, buffer]) => state.buffers.get(path) === buffer);
  const choice = await styledConfirm(
    `${context}: ${dirty.map(displayName).join(', ')}`,
    { title: 'Unsaved changes', confirmText: 'Save', alternateText: "Don't Save", cancelText: 'Cancel' },
  );
  if (!stillCurrent()) return false;
  if (choice === false) return false;
  if (choice === 'alternate') {
    dirtyEntries.forEach(([, buffer]) => discardBufferChanges(buffer));
    return true;
  }
  const results = await Promise.all(dirty.map(path => saveBuffer(path)));
  return stillCurrent() && results.every(Boolean) && dirtyEntries.every(([, buffer]) => !buffer.dirty);
}

function bufferFor(path) {
  return state.buffers.get(path) || { path, text: '', fingerprint: null, dirty: false, loaded: false };
}

function loadEditorFactory() {
  if (state.editorFactory) return Promise.resolve(state.editorFactory);
  if (!state.editorLoadPromise) {
    state.editorLoadPromise = import('./copal/codemirror.js').then(module => {
      state.editorFactory = module.createSourceEditor || module.createMarkdownEditor;
      state.richCommentLanguageQualified = module.isRichCommentLanguageQualified || (() => false);
      state.editorLoadError = null;
      return state.editorFactory;
    }).catch(error => {
      state.editorLoadError = error;
      throw error;
    }).finally(() => { state.editorLoadPromise = null; });
  }
  return state.editorLoadPromise;
}

function setStatus(message, bad = false) {
  state.nativeWindow?.setStatus(message, bad);
}

function markLauncherActive(active) {
  const launcher = document.querySelector('[data-code-editor-launcher]');
  launcher?.classList.toggle('active', active);
  if (active) launcher?.setAttribute('aria-current', 'page');
  else launcher?.removeAttribute('aria-current');
}

function codeExplorerEntry(entry, directory, parentResourceRef = '') {
  const resourceRef = String(entry?.resource_ref || entry?.ref || '').trim();
  const path = entry?.path ? String(entry.path) : childPath(directory, entry?.name);
  return {
    ...entry,
    // `path` is a display label used for tab titles and language selection.
    // Every facade request below uses this sealed ref instead.
    resource_ref: resourceRef,
    resource_id: String(entry?.resource_id || entry?.id || resourceRef || path),
    resourceRef,
    parent_resource_ref: String(entry?.parent_resource_ref || parentResourceRef || ''),
    path,
    parent_path: String(directory || parentPath(path)),
    name: String(entry?.name || displayName(path)),
    kind: String(entry?.kind || 'file').toLowerCase(),
  };
}

async function resolveEditorRelativeResource(relativePath) {
  const normalized = String(relativePath || '').replace(/\\/g, '/').trim();
  const parts = normalized.split('/');
  if (!normalized || normalized.length > 2048 || !parts.length || parts.length > 32
    || parts.some(part => !part || part === '.' || part === '..' || part.includes('\u0000'))) {
    throw new Error('Quick Open accepts a safe relative file name.');
  }
  let parentRef = String(state.rootResourceRef || '').trim();
  if (!parentRef) throw new Error('Authorized Editor folder is unavailable');
  for (let index = 0; index < parts.length; index += 1) {
    let cursor = null;
    let match = null;
    for (let page = 0; page < 8 && !match; page += 1) {
      const response = await filesFacadeClient.children(parentRef, {
        cursor,
        limit: 200,
        query: parts[index],
        sort: { key: 'name', direction: 'asc', directories_first: true, collation: 'open-clank-v1' },
      });
      match = (response?.entries || []).find(entry => String(entry?.name || '') === parts[index]);
      cursor = response?.next_cursor || null;
      if (!cursor) break;
    }
    if (!match) throw new Error(`The Editor could not find ${parts[index]}.`);
    const resourceRef = String(match?.resource_ref || match?.ref || '').trim();
    if (!resourceRef) throw new Error('Files did not return an authorized resource.');
    if (index === parts.length - 1) return { resourceRef, kind: String(match?.kind || '').toLowerCase() };
    if (!['folder', 'provider_root', 'virtual_folder'].includes(String(match?.kind || '').toLowerCase())) {
      throw new Error(`${parts[index]} is not an Editor folder.`);
    }
    parentRef = resourceRef;
  }
  throw new Error('The Editor resource could not be resolved.');
}

async function listCodeDirectory(path, { cursor = null, signal = null, directory = null } = {}) {
  const requested = String(path || '').trim();
  // `state.root` is a display label/path retained for tabs and breadcrumbs.
  // Once Files has issued a sealed root ref, it is the only authority for
  // browsing that root; never send the label through the facade as a parent.
  const parentRef = state.rootResourceRef && (!requested || requested === state.root || requested === state.rootResourceRef)
    ? String(state.rootResourceRef).trim()
    : requested;
  if (!parentRef || !state.rootResourceRef && !parentRef.startsWith('rr')) {
    throw new Error('Choose an authorized Editor folder before browsing.');
  }
  const response = await filesFacadeClient.children(parentRef, {
    cursor,
    sort: { key: 'name', direction: 'asc', directories_first: true, collation: 'open-clank-v1' },
    signal,
  });
  return {
    items: (response?.entries || []).map(entry => codeExplorerEntry(entry, String(directory ?? (state.root || path || 'Workspace')), parentRef)),
    nextCursor: response?.next_cursor || null,
  };
}

async function loadInitialCodeRoot(path, generation, { signal = null, directory = null } = {}) {
  if (signal?.aborted) return null;
  state.rootTreeController?.abort();
  const controller = new AbortController();
  state.rootTreeController = controller;
  const abort = () => controller.abort();
  signal?.addEventListener('abort', abort, { once:true });
  try {
    const page = await listCodeDirectory(path, { signal: controller.signal, directory });
    if (generation !== state.requestGeneration || controller.signal.aborted) return null;
    return page;
  } finally {
    signal?.removeEventListener('abort', abort);
    if (state.rootTreeController === controller) state.rootTreeController = null;
  }
}

function codeTreeHasCapability(resource, name) {
  return resource?.capabilities?.[name] === true;
}

function codeTreeCanCopy(resource) {
  return ['read', 'download', 'open', 'copy'].some((name) => codeTreeHasCapability(resource, name));
}

function codeTreeCanMove(entry, resource) {
  return entryIsDirectory(entry) ? codeTreeHasCapability(resource, 'write') : codeTreeHasCapability(resource, 'move');
}

function codeTreeCanCreate(resource) {
  return codeTreeHasCapability(resource, 'write') || codeTreeHasCapability(resource, 'create');
}

function codeTreeIsFolderResource(resource) {
  return resource?.kind === 'folder' || codeTreeHasCapability(resource, 'children');
}

function codeTreeIdentityStillCurrent(identity) {
  if (!identity?.row?.isConnected || state.workspaceOwner !== identity.owner || state.workspaceEpoch !== identity.workspaceEpoch
    || state.fileGeneration !== identity.fileGeneration || state.requestGeneration !== identity.requestGeneration
    || state.rootResourceRef !== identity.rootResourceRef) return false;
  const node = state.explorerTree?.getNode(identity.treeId);
  return node?.data === identity.entry;
}

function normalizeCodeTreeResource(raw, parentRef = '') {
  return normalizeAuthorizedResource(raw, {
    purpose:'file', generation:Number(state.fileGeneration) || 0,
    accountScope:String(state.workspaceOwner || ''), parentRef:String(parentRef || '') || null,
  });
}

async function refreshCodeTreeResource(identity, { includeDestination = false } = {}) {
  if (!codeTreeIdentityStillCurrent(identity)) throw new Error('This Editor item changed. Reopen its menu and try again.');
  const sourceResponse = await filesFacadeClient.stat(identity.resource.ref);
  if (!codeTreeIdentityStillCurrent(identity)) throw new Error('Editor access changed. Reopen the item menu and try again.');
  const source = normalizeCodeTreeResource(sourceResponse?.resource || sourceResponse, identity.entry.parent_resource_ref);
  if (source.resourceKey !== identity.resource.resourceKey
    || entryIsDirectory(identity.entry) !== (source.kind === 'folder')) {
    throw new Error('Files returned a different Editor resource. Refresh the folder and try again.');
  }
  let destination = null;
  if (includeDestination) {
    const destinationRef = String(identity.entry.parent_resource_ref || identity.rootResourceRef || '').trim();
    if (!destinationRef) throw new Error('Authorized destination folder is unavailable.');
    const destinationResponse = await filesFacadeClient.stat(destinationRef);
    if (!codeTreeIdentityStillCurrent(identity)) throw new Error('Editor access changed. Reopen the item menu and try again.');
    destination = normalizeAuthorizedResource(destinationResponse?.resource || destinationResponse, {
      purpose:'folder', generation:Number(state.fileGeneration) || 0,
      accountScope:String(state.workspaceOwner || ''),
    });
    if (!codeTreeIsFolderResource(destination) || !codeTreeCanCreate(destination)) throw new Error('Files did not authorize the destination folder for copying.');
    if (identity.destinationResourceKey && destination.resourceKey !== identity.destinationResourceKey) {
      throw new Error('The destination folder changed. Reopen the menu and try again.');
    }
  }
  return { source, destination };
}

function codeTreeCommands(identity) {
  const resource = identity.resource;
  const commands = [];
  if (entryIsDirectory(identity.entry) && codeTreeHasCapability(resource, 'children')) commands.push({ id:'code-tree-open-folder', label:'Open folder' });
  else if (!entryIsDirectory(identity.entry) && codeTreeHasCapability(resource, 'open') && codeTreeHasCapability(resource, 'read')) commands.push({ id:'code-tree-open-file', label:'Open file' });
  if (codeTreeHasCapability(resource, 'rename')) commands.push({ id:'code-tree-rename', label:'Rename' });
  if (codeTreeCanMove(identity.entry, resource)) commands.push({ id:'code-tree-move', label:'Move' });
  if (codeTreeCanCopy(resource) && codeTreeIsFolderResource(identity.destination) && codeTreeCanCreate(identity.destination)) commands.push({ id:'code-tree-copy', label:'Copy' });
  if (codeTreeHasCapability(resource, 'trash')) commands.push({ id:'code-tree-trash', label:'Move to Trash' });
  return commands.length ? commands : [{ id:'code-tree-no-actions', label:'No supported actions', disabled:true }];
}

async function executeCodeTreeAction(action, identity, treeContext) {
  if (!codeTreeIdentityStillCurrent(identity)) throw new Error('This Editor item changed. Reopen its menu and try again.');
  const entry = identity.entry;
  const child = identity.path;
  if (action === 'open-folder') {
    if (!entryIsDirectory(entry) || !codeTreeHasCapability(identity.resource, 'children')) throw new Error('Files did not authorize this folder for browsing.');
    state.activeDirectory = child;
    state.activeDirectoryRef = identity.resource.ref;
    await treeContext.expand();
    return true;
  }
  if (action === 'open-file') {
    if (entryIsDirectory(entry) || !codeTreeHasCapability(identity.resource, 'open') || !codeTreeHasCapability(identity.resource, 'read')) throw new Error('Files did not authorize this file for opening.');
    await openFile(child, null, identity.resource.ref);
    return true;
  }
  if (!['rename', 'copy', 'move', 'trash'].includes(action)) return false;
  if (action === 'trash') {
    const affected = subtreeBufferEntries(child).map(([path]) => path);
    if (!(await resolveDirtyBuffers(affected, `Trash ${displayName(child)}`))) return true;
    if (!await styledConfirm(`Move ${entry.name} to recoverable trash?`, { title:'Move to Trash', confirmText:'Move to Trash', danger:true })) return true;
    if (!codeTreeIdentityStillCurrent(identity)) throw new Error('This Editor item changed. Reopen its menu and try again.');
    const { source } = await refreshCodeTreeResource(identity);
    if (!codeTreeHasCapability(source, 'trash')) throw new Error('Files did not authorize this Editor item for Trash.');
    const reservation = beginTreeMutation('trash', child);
    try {
      const trashed = await filesFacadeClient.action(source.ref, 'trash', {}, { actionId:operationId('editor-trash') });
      const changedDuringTrash = treeMutationBuffersChanged(reservation);
      if (changedDuringTrash) {
        const trashEntry = trashed?.resource || null;
        if (trashEntry) {
          try {
            await filesFacadeClient.action(String(trashEntry.ref || source.ref), 'restore', {}, { actionId:operationId('editor-restore') });
            await refreshExplorer();
            setStatus(`Kept ${entry.name}; it changed while Trash was running`, true);
            return true;
          } catch (_) { /* retain the in-memory buffers below */ }
        }
        const retained = retainOrphanedTrashBuffers(child);
        if (retained.length) renderEditor();
        await refreshExplorer();
        setStatus(`Newer edits to ${entry.name} were retained after Trash`, true);
        return true;
      }
      const currentAffected = subtreeBufferEntries(child).map(([path]) => path);
      detachBuffers(currentAffected, { remember:false });
      if (currentAffected.length) renderEditor();
      await refreshExplorer();
      setStatus(`Moved ${entry.name} to recoverable trash`);
    } catch (error) { setStatus(error.message || 'Trash failed', true); }
    finally { finishTreeMutation(reservation); }
    return true;
  }

  const destinationName = validChildName(await styledPrompt(`${action} as (name only)`, {
    title:`${action[0].toUpperCase()}${action.slice(1)} ${entryIsDirectory(entry) ? 'folder' : 'file'}`,
    defaultValue:entry.name, confirmText:'Continue', maxLength:240,
  }));
  if (!destinationName) return true;
  const destinationPath = childPath(parentPath(child), destinationName);
  const affected = action === 'rename' || action === 'move'
    ? subtreeBufferEntries(child).map(([path]) => path)
    : [];
  if (!(await resolveDirtyBuffers(affected, `${action[0].toUpperCase()}${action.slice(1)} ${displayName(child)}`))) return true;
  if (!codeTreeIdentityStillCurrent(identity)) throw new Error('This Editor item changed. Reopen its menu and try again.');
  const reservation = action === 'rename' || action === 'move'
    ? beginTreeMutation(action, child, destinationPath)
    : null;
  try {
    const { source, destination } = await refreshCodeTreeResource(identity, { includeDestination:action === 'copy' });
    if (action === 'rename' && !codeTreeHasCapability(source, 'rename')) throw new Error('Files did not authorize this Editor item for Rename.');
    if (action === 'move' && !codeTreeCanMove(entry, source)) throw new Error('Files did not authorize this Editor item for Move.');
    if (action === 'copy' && (!codeTreeCanCopy(source) || !codeTreeIsFolderResource(destination) || !codeTreeCanCreate(destination))) throw new Error('Files did not authorize this Editor item for Copy.');
    if (action === 'copy') {
      await filesFacadeClient.transferResources({
        operationId:operationId('editor-copy'), generation:Number(identity.fileGeneration), kind:'copy',
        sources:[{ itemId:operationId('editor-item'), resourceRef:source.ref, expectedRevision:source.revision?.value || entry.revision || undefined }],
        destinationRef:destination.ref, collision:'fail',
      });
    } else {
      const result = await filesFacadeClient.action(source.ref, action, { name:destinationName }, { actionId:operationId(`editor-${action}`) });
      const rotatedRef = String(result?.resource?.ref || '').trim();
      if (rotatedRef) {
        for (const [, buffer] of state.buffers) {
          if (buffer.resourceRef === source.ref || buffer.resourceRef === identity.resource.ref) buffer.resourceRef = rotatedRef;
        }
      }
    }
    if (reservation) {
      const rekeyed = rekeyTreeMutationBuffers(reservation);
      if (rekeyed.length) renderEditor();
    }
    await refreshExplorer();
    setStatus(`${action[0].toUpperCase()}${action.slice(1)}d ${displayName(destinationPath)}`);
  } catch (error) { setStatus(error.message || `${action} failed`, true); }
  finally { if (reservation) finishTreeMutation(reservation); }
  return true;
}

async function handleCodeTreeContextMenu(event, entry, treeContext) {
  const contextMenu = window.openClankContextMenu;
  if (!contextMenu?.enabled?.() || typeof contextMenu.registerAdapter !== 'function') return;
  const row = event.currentTarget;
  if (hydratedCodeTreeContextEvents.has(event)) {
    hydratedCodeTreeContextEvents.delete(event);
    const identity = codeTreeContextHydration.get(row);
    if (!identity || !codeTreeIdentityStillCurrent(identity)) return;
    registerAdapter(row, {
      capture:() => identity,
      commands:() => codeTreeCommands(identity),
      execute:async (command, request) => {
        const captured = request?.adapterContext;
        if (captured !== identity || !codeTreeIdentityStillCurrent(identity)) throw new Error('This Editor item changed. Reopen its menu and try again.');
        const action = {
          'code-tree-open-folder':'open-folder', 'code-tree-open-file':'open-file',
          'code-tree-rename':'rename', 'code-tree-move':'move', 'code-tree-copy':'copy', 'code-tree-trash':'trash',
        }[command];
        if (!action) return command === 'code-tree-no-actions';
        return executeCodeTreeAction(action, identity, treeContext);
      },
    });
    return;
  }

  event.preventDefault();
  event.stopPropagation();
  const requestId = ++codeTreeContextRequestSequence;
  const resourceRef = String(entry.resource_ref || entry.resourceRef || '').trim();
  if (!resourceRef) { setStatus('Refresh the authorized Editor folder before changing this item.', true); return; }
  const treeId = String(entry.resource_id || entry.resource_ref || entry.path || '');
  const scope = {
    row, entry, treeId, path:String(entry.path || ''), owner:state.workspaceOwner,
    workspaceEpoch:state.workspaceEpoch, fileGeneration:state.fileGeneration,
    requestGeneration:state.requestGeneration, rootResourceRef:state.rootResourceRef,
  };
  if (!codeTreeIdentityStillCurrent(scope)) { setStatus('Refresh the authorized Editor folder before changing this item.', true); return; }
  try {
    const listed = normalizeCodeTreeResource(entry, entry.parent_resource_ref);
    const sourceResponse = await filesFacadeClient.stat(resourceRef);
    if (requestId !== codeTreeContextRequestSequence || !codeTreeIdentityStillCurrent(scope)) return;
    const resource = normalizeCodeTreeResource(sourceResponse?.resource || sourceResponse, entry.parent_resource_ref);
    if (resource.resourceKey !== listed.resourceKey || entryIsDirectory(entry) !== (resource.kind === 'folder')) {
      throw new Error('Files returned a different Editor resource. Refresh the folder and try again.');
    }
    entry.resource_ref = resource.ref;
    entry.resourceRef = resource.ref;
    entry.revision = resource.revision || entry.revision;
    const destinationRef = String(entry.parent_resource_ref || state.rootResourceRef || '').trim();
    let destination = null;
    if (destinationRef && codeTreeCanCopy(resource)) {
      try {
        const destinationResponse = await filesFacadeClient.stat(destinationRef);
        if (requestId !== codeTreeContextRequestSequence || !codeTreeIdentityStillCurrent(scope)) return;
        destination = normalizeAuthorizedResource(destinationResponse?.resource || destinationResponse, {
          purpose:'folder', generation:Number(state.fileGeneration) || 0,
          accountScope:String(state.workspaceOwner || ''),
        });
      } catch (_) { destination = null; }
    }
    if (requestId !== codeTreeContextRequestSequence || !codeTreeIdentityStillCurrent(scope)) return;
    const identity = Object.freeze({
      ...scope, resource, destination, destinationResourceKey:destination?.resourceKey || '',
      destinationRef, resourceRef:resource.ref,
    });
    codeTreeContextHydration.set(row, identity);
    if (!contextMenu.enabled?.() || !event.target?.isConnected) return;
    const synthetic = new MouseEvent('contextmenu', {
      bubbles:true, cancelable:true, clientX:event.clientX, clientY:event.clientY,
      detail:event.detail, shiftKey:event.shiftKey, ctrlKey:event.ctrlKey,
      metaKey:event.metaKey, altKey:event.altKey,
    });
    hydratedCodeTreeContextEvents.add(synthetic);
    event.target.dispatchEvent(synthetic);
  } catch (error) {
    if (requestId === codeTreeContextRequestSequence) setStatus(error.message || 'Files could not authorize this Editor item.', true);
  }
}

function mountCodeExplorer(entries = [], nextCursor = null, rootPath = state.root) {
  const tree = state.shell?.querySelector('[data-code-tree]');
  if (!tree) return null;
  state.explorerTree?.destroy?.();
  state.explorerTree = createExplorerTree({
    container: tree,
    roots: entries,
    getId: entry => entry.resource_id || entry.resource_ref || entry.path,
    getLabel: entry => entry.name,
    isBranch: entryIsDirectory,
    ariaLabel: 'Workspace files',
    emptyLabel: state.root ? 'This folder is empty' : 'No user-visible folder has been assigned.',
    loadPage: (entry, { cursor, signal }) => listCodeDirectory(entry.resource_ref || entry.path, { cursor, signal }),
    loadRootPage: rootPath ? ({ cursor, signal }) => listCodeDirectory(state.rootResourceRef || rootPath, { cursor, signal }) : null,
    rootNextCursor: nextCursor,
    onActivate: async (entry, context) => {
      if (entryIsDirectory(entry)) {
        state.activeDirectory = entry.path;
        state.activeDirectoryRef = entry.resource_ref || entry.resourceRef || '';
        await context.toggle();
      } else await openFile(entry.path, null, entry.resource_ref || entry.resourceRef);
    },
    onContextMenu: handleCodeTreeContextMenu,
    renderIcon: (entry, { expanded }) => fileIcon(
      { ...entry, kind: entryIsDirectory(entry) ? 'directory' : entry.kind, open: entryIsDirectory(entry) && expanded },
      15,
      { className: 'code-tree-glyph' },
    ),
    onError: error => setStatus(error.message || 'Folder unavailable', true),
    classes: {
      item: 'code-tree-item',
      row: entry => `code-tree-row ${entryIsDirectory(entry) ? 'directory' : 'file'}`,
      toggle: 'code-tree-caret',
      toggleSpacer: 'code-tree-caret',
      icon: entry => `code-tree-icon ${entryIsDirectory(entry) ? 'folder' : 'file'}`,
      label: 'code-tree-name',
      group: 'code-tree-children',
      status: 'code-tree-loading',
      error: 'code-tree-error',
      retry: 'code-tree-retry',
      more: 'code-tree-more',
    },
    itemAttributes: entry => ({ 'data-code-tree-item-path': entry.path }),
    rowAttributes: entry => ({
      title: entry.path,
      'data-code-tree-path': entry.path,
      'data-code-tree-parent': entry.parent_path,
    }),
  });
  return state.explorerTree;
}

async function refreshExplorer() {
  if (!state.shell?.querySelector('[data-code-tree]') || !state.root) return false;
  const generation = ++state.requestGeneration;
  try {
    let loaded;
    if (!state.explorerTree) {
      const page = await loadInitialCodeRoot(state.rootResourceRef || state.activeDirectoryRef || state.root, generation);
      if (!page) return false;
      mountCodeExplorer(page.items, page.nextCursor, state.root);
      loaded = true;
    } else {
      loaded = await state.explorerTree.refreshRoots({ recursive: true });
    }
    if (generation !== state.requestGeneration) return false;
    await reconcileOpenBuffers(generation);
    return loaded;
  } catch (error) {
    if (error?.name === 'AbortError' || generation !== state.requestGeneration) return false;
    setStatus(error.message || 'Folder unavailable', true);
    return false;
  }
}

async function reconcileOpenBuffers(generation = state.requestGeneration) {
  state.bufferReconcileController?.abort?.();
  const controller = new AbortController();
  state.bufferReconcileController = controller;
  const paths = [...state.buffers.keys()];
  try {
    await Promise.all(paths.map(async path => {
      const buffer = state.buffers.get(path);
      if (!buffer || controller.signal.aborted) return;
      if (!buffer.resourceRef) {
        buffer.saveError = { code: 'resource_reference_missing', message: 'Files authorization expired; reopen this file from the authorized folder.' };
        return;
      }
      try {
        const response = await filesFacadeClient.stat(buffer.resourceRef, { signal: controller.signal });
        if (generation !== state.requestGeneration || controller.signal.aborted) return;
        const fingerprint = response?.resource?.revision?.value || response?.revision?.value || null;
        if (!fingerprint || !buffer.fingerprint || fingerprint === buffer.fingerprint) return;
        buffer.saveError = {
          code: 'external_change',
          message: buffer.dirty ? 'The file changed on disk; your local edits remain unsaved.' : 'The file changed on disk. Reload to update this buffer.',
        };
      } catch (error) {
        if (error?.name === 'AbortError' || generation !== state.requestGeneration) return;
        buffer.saveError = { code: 'external_missing', message: 'The file is no longer available at this path.' };
      }
    }));
    if (generation === state.requestGeneration && state.activePath && state.buffers.get(state.activePath)?.saveError) renderEditor();
  } finally {
    if (state.bufferReconcileController === controller) state.bufferReconcileController = null;
  }
}

async function prepareWorkspaceChange(nextRoot, { isCurrent = () => true, onPrepared = null } = {}) {
  if (!isCurrent()) return false;
  if (!state.root || state.root === nextRoot) return true;
  const dirty = [...state.buffers.keys()].filter(path => state.buffers.get(path)?.dirty);
  if (!(await resolveDirtyBuffers(dirty, `Switching workspace to ${displayName(nextRoot)}`, isCurrent)) || !isCurrent()) return false;
  state.workspaceEpoch += 1;
  onPrepared?.();
  for (const buffer of state.buffers.values()) {
    buffer.saveController?.abort?.();
    buffer.reloadController?.abort?.();
    buffer.contextMenuDispose?.();
    buffer.contextMenuDispose = null;
    buffer.editor?.destroy?.();
  }
  state.buffers.clear();
  state.activePath = null;
  state.activeDirectory = '';
  state.activeDirectoryRef = '';
  state.closedBuffers.length = 0;
  updateReopenClosedAction();
  return true;
}

async function displayWorkspaceRoot(root, generation = ++state.requestGeneration, {
  resourceRef = null, signal = null, isCurrent = () => true, onPrepared = null, throwOnError = false,
} = {}) {
  const stillCurrent = () => generation === state.requestGeneration && !!state.nativeWindow?.visible
    && !signal?.aborted && isCurrent();
  if (!stillCurrent()) return false;
  const tree = state.shell?.querySelector('[data-code-tree]');
  if (!tree) return false;
  setStatus(`Opening ${displayName(root)}…`);
  try {
    // Fetch the candidate page while the current root/buffers/tree remain
    // visible. A denied or stale folder therefore cannot replace a usable
    // Editor view with an empty/error root.
    const page = root ? await loadInitialCodeRoot(resourceRef ?? (state.rootResourceRef || state.activeDirectoryRef || root), generation, {
      signal, directory:resourceRef === null ? null : root,
    }) : { items:[], nextCursor:null };
    if (!page || !stillCurrent()) return false;
    if (!(await prepareWorkspaceChange(root, { isCurrent:stillCurrent, onPrepared }))) {
      if (throwOnError && stillCurrent()) throw new Error('The workspace switch was canceled or its unsaved files could not be saved.');
      return false;
    }
    if (!stillCurrent()) return false;
    state.fileGeneration += 1;
    abortPendingFileReads();
    if (resourceRef !== null) state.rootResourceRef = resourceRef;
    state.root = root;
    state.activeDirectory = root;
    state.activeDirectoryRef = state.rootResourceRef || '';
    state.explorerTree?.destroy?.();
    state.explorerTree = null;
    const rootLabel = state.shell?.querySelector('[data-code-root]');
    if (rootLabel) rootLabel.textContent = root;
    mountCodeExplorer(page.items, page.nextCursor, root);
    renderEditor();
    setStatus(root || 'No assigned folder', !root);
    return true;
  } catch (error) {
    if (error?.name === 'AbortError' || signal?.aborted || generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
    setStatus(error.message || 'Folder unavailable', true);
    if (throwOnError) throw error;
    return false;
  }
}

async function selectWorkspaceRoot(path) {
  const loaded = await displayWorkspaceRoot(path);
  if (!loaded) throw new Error('Folder could not be opened');
  // Persist only after both the Rust app lane and canonical Workspace binding
  // accept the folder. Browser storage keeps an opaque ID and display hint;
  // the raw hint is never authority on the next open.
  const workspace = await workspaceModule.bindWorkspacePath(path, 'app_folder');
  saveWorkspaceRoot(workspace.path, state.workspaceOwner, workspace.id);
}

function openWorkspaceFolder() {
  // Editor folder selection uses the Files-v1 authority. The old workspace
  // modal remains an explicit compatibility route only when the facade is
  // genuinely absent (for embedded deployments that do not ship Files-v1).
  if (typeof filesFacadeClient.roots === 'function') {
    state.workspacePicker?.destroy();
    const origin = {
      window:state.nativeWindow, shell:state.shell, pane:state.pane, owner:state.workspaceOwner,
      resourceRef:state.rootResourceRef, fileGeneration:state.fileGeneration,
      path:state.activePath, editor:state.buffers.get(state.activePath)?.editor,
    };
    let epoch = state.workspaceEpoch;
    let requestGeneration = state.requestGeneration;
    let picker;
    const originAvailable = () => state.workspacePicker === picker && state.nativeWindow === origin.window
      && state.shell === origin.shell && state.pane === origin.pane && origin.window?.visible
      && origin.window.root?.isConnected && state.workspaceOwner === origin.owner;
    const originCurrent = () => originAvailable() && state.workspaceEpoch === epoch
      && state.fileGeneration === origin.fileGeneration && state.rootResourceRef === origin.resourceRef
      && state.requestGeneration === requestGeneration;
    picker = createResourcePicker({
      client:filesFacadeClient,
      purpose:'folder',
      rootLabel:'Authorized Editor folders',
      originWindow:origin.window,
      getGeneration:() => state.fileGeneration,
      getAccountScope:() => state.workspaceOwner,
      getWorkspaceScope:() => state.rootResourceRef,
      isOriginCurrent:originCurrent,
      onSelect:async (resource, { signal, isCurrent }) => {
        const assertCurrent = () => {
          if (signal.aborted || !originCurrent() || !isCurrent()) throw new Error('The original Editor folder or access changed; reopen the picker.');
        };
        assertCurrent();
        const response = await filesFacadeClient.workspace(resource.ref, 'app_folder', { signal });
        assertCurrent();
        const workspaceId = String(response?.workspace?.id || '').trim();
        if (!workspaceId) throw new Error('Files did not issue an Editor workspace.');
        const workspace = await workspaceModule.resolveWorkspaceId(workspaceId, 'app_folder');
        assertCurrent();
        // Registration can advance Files authority. Load using a fresh bound
        // ref, keeping the current tree/ref paired until the candidate succeeds.
        const target = await filesFacadeClient.workspaceResource(workspaceId, '', { signal });
        assertCurrent();
        const rootRef = String(target?.resource?.ref || target?.parent?.ref || '').trim();
        const rootKind = String(target?.resource?.kind || target?.parent?.kind || '').toLowerCase();
        if (!rootRef || !['folder', 'provider_root', 'virtual_folder'].includes(rootKind)) throw new Error('Files did not return an authorized Workspace folder.');
        requestGeneration = ++state.requestGeneration;
        const loaded = await displayWorkspaceRoot(workspace.path, requestGeneration, {
          resourceRef:rootRef, signal, isCurrent:() => originCurrent() && isCurrent(),
          // Only the synchronous, guarded switch may advance this captured epoch.
          onPrepared:() => { epoch += 1; }, throwOnError:true,
        });
        if (!loaded) throw new Error('The authorized folder could not be loaded.');
        // A successful publication deliberately advances the folder generation.
        if (signal.aborted || !originAvailable() || state.workspaceEpoch !== epoch
          || state.fileGeneration !== origin.fileGeneration + 1 || state.requestGeneration !== requestGeneration
          || state.root !== workspace.path || state.rootResourceRef !== rootRef) throw new Error('Editor access changed; choose the folder again.');
        saveWorkspaceRoot(workspace.path, origin.owner, workspace.id);
        setStatus(`Opened ${resource.name}`);
        return true;
      },
      onClose:() => {
        if (state.workspacePicker === picker) state.workspacePicker = null;
        if (state.nativeWindow !== origin.window || state.shell !== origin.shell || state.pane !== origin.pane
          || state.workspaceOwner !== origin.owner || !origin.window?.visible || !origin.window.root?.isConnected) return;
        if (state.buffers.get(origin.path)?.editor === origin.editor && origin.editor?.view?.dom?.isConnected) origin.editor.focus();
        else origin.window.focus();
      },
    });
    state.workspacePicker = picker;
    void picker.open();
    return;
  }
  workspaceModule.openWorkspaceBrowser({
    selectionKind: 'app_folder',
    initialPath: state.root,
    onSelect: selectWorkspaceRoot,
  });
}

async function createWorkspaceFile() {
  if (!state.root) { setStatus('Open a workspace folder first', true); return; }
  const name = validChildName(await styledPrompt('New file name', { title: 'New file', defaultValue: 'untitled.txt', confirmText: 'Create', maxLength: 240 }));
  if (!name) { setStatus('A simple file name is required', true); return; }
  const directory = state.activeDirectory || state.root;
  try {
    const parentRef = state.activeDirectoryRef || state.rootResourceRef;
    if (!parentRef) throw new Error('Choose an authorized Editor folder before creating a file.');
    if (!/\.(?:md|markdown)$/i.test(name)) throw new Error('The Files facade currently creates Markdown files only.');
    const created = await filesFacadeClient.createResource(parentRef, { name, text: '', actionId: operationId('editor-create') });
    await refreshExplorer();
    const createdRef = String(created?.resource?.ref || '').trim();
    if (!createdRef) throw new Error('Files did not return the created resource reference.');
    await openFile(childPath(directory, name), null, createdRef);
    setStatus(`Created ${name}`);
  } catch (error) { setStatus(error.message || 'Create file failed', true); }
}

async function createWorkspaceDirectory() {
  if (!state.root) { setStatus('Open a workspace folder first', true); return; }
  const name = validChildName(await styledPrompt('New folder name', { title: 'New folder', defaultValue: 'folder', confirmText: 'Create', maxLength: 240 }));
  if (!name) { setStatus('A simple folder name is required', true); return; }
  const directory = state.activeDirectory || state.root;
  try {
    const parentRef = state.activeDirectoryRef || state.rootResourceRef;
    if (!parentRef) throw new Error('Choose an authorized Editor folder before creating a folder.');
    if (typeof filesFacadeClient.createDirectory !== 'function') {
      throw new Error('Folder creation is awaiting the Files facade create-directory contract.');
    }
    await filesFacadeClient.createDirectory(parentRef, { name, operationId: operationId('editor-create-directory'), generation: Number(state.fileGeneration) });
    await refreshExplorer();
    setStatus(`Created ${name}`);
  } catch (error) { setStatus(error.message || 'Create folder failed', true); }
}

async function openFile(path, viewState = null, resourceRef = '') {
  if (!path) return false;
  const opaqueRef = String(resourceRef || '').trim();
  if (!opaqueRef) {
    setStatus('Reopen this file from the authorized Files folder.', true);
    return false;
  }
  const activationSequence = ++state.fileActivationSequence;
  const existing = state.buffers.get(path);
  if (existing && opaqueRef && existing.resourceRef !== opaqueRef) {
    existing.resourceRef = opaqueRef;
    existing.expectedRevision = null;
    existing.fingerprint = null;
  }
  if (existing?.loaded) {
    state.activePath = path;
    renderEditor();
    setStatus(`${displayName(path)} · ${existing.text.length.toLocaleString()} chars`);
    return true;
  }
  const pending = state.pendingFileReads.get(path);
  if (pending) {
    // Same-path opens share one authorized read, but the newest request still
    // participates in the global activation ordering.
    pending.activationSequence = activationSequence;
    if (viewState) pending.viewState = viewState;
    setStatus(`Opening ${displayName(path)}…`);
    return pending.promise;
  }

  const buffer = existing || {
    path,
    text: '',
    originalText: '',
    fingerprint: null,
    encoding: 'utf-8',
    newline: '\n',
    dirty: false,
    loaded: false,
    revision: 0,
    savedRevision: 0,
    saveError: null,
    editor: null,
    selection: null,
    scrollTop: 0,
    richComments: state.richCommentsDefault,
    resourceRef: opaqueRef,
    expectedRevision: null,
  };
  if (!existing) state.buffers.set(path, buffer); // Preserve open-request tab order.
  const controller = new AbortController();
  const entry = {
    path,
    requestedPath: path,
    resourceRef: opaqueRef,
    buffer,
    controller,
    activationSequence,
    viewState,
    fileGeneration: state.fileGeneration,
    workspaceEpoch: state.workspaceEpoch,
    owner: state.workspaceOwner,
    root: state.root,
    promise: null,
  };
  const ownsCompletion = () => !controller.signal.aborted
    && state.pendingFileReads.get(entry.path) === entry
    && state.buffers.get(entry.path) === buffer
    && state.fileGeneration === entry.fileGeneration
    && state.workspaceEpoch === entry.workspaceEpoch
    && state.workspaceOwner === entry.owner
    && state.root === entry.root
    && state.nativeWindow?.visible;
  setStatus(`Opening ${displayName(path)}…`);
  state.pendingFileReads.set(path, entry);
  entry.promise = (async () => {
    try {
      const [response, editorFactory] = await Promise.all([
        filesFacadeClient.openResource(entry.resourceRef, { signal:controller.signal }), loadEditorFactory(),
      ]);
      if (!ownsCompletion()) return false;
      const payload = response?.payload || {};
      const resource = payload?.resource || response?.resource || {};
      const text = String(payload.text ?? payload.content ?? '');
      if (!ownsCompletion()) return false;
      Object.assign(buffer, {
        text,
        originalText: text,
        fingerprint: resource.revision?.value || payload.fingerprint?.value || null,
        expectedRevision: resource.revision || null,
        resourceRef: String(resource.ref || entry.resourceRef || ''),
        resourceIdentity:resource.resourceKey || resource.key || null,
        encoding: payload.encoding || resource.metadata?.encoding || 'utf-8',
        newline: payload.newline || resource.metadata?.newline || '\n',
        dirty: false,
        loaded: true,
        revision: 0,
        savedRevision: 0,
        saveError: null,
        editor: null,
        selection: entry.viewState?.selection || null,
        scrollTop: Number(entry.viewState?.scrollTop) || 0,
      });
      state.editorFactory = editorFactory;
      const activate = entry.activationSequence === state.fileActivationSequence;
      if (activate) state.activePath = entry.path;
      updateReopenClosedAction();
      // An inactive completion still needs to materialize its tab; it must not
      // steal activation from the most recently requested file.
      if (activate || state.activePath) renderEditor();
      if (activate) setStatus(`${displayName(entry.path)} · ${text.length.toLocaleString()} chars`);
      return true;
    } catch (error) {
      if (error?.name === 'AbortError' || !ownsCompletion()) return false;
      if (state.buffers.get(entry.path) === buffer && !buffer.loaded) state.buffers.delete(entry.path);
      updateReopenClosedAction();
      if (entry.activationSequence === state.fileActivationSequence) {
        setStatus(error.message || 'File could not be opened', true);
      }
      return false;
    } finally {
      if (state.pendingFileReads.get(entry.path) === entry) state.pendingFileReads.delete(entry.path);
    }
  })();
  return entry.promise;
}

function closeTabMenu() {
  state.tabMenu?.remove();
  state.tabMenu = null;
}

function tabMenuAction(label, action, path, handler) {
  const button = el('button', {
    type: 'button',
    role: 'menuitem',
    class: 'dropdown-item-compact code-editor-tab-menu-item',
    'data-code-tab-action': action,
    'aria-label': `${label} · ${path}`,
    text: label,
    style: 'width:100%;border:0;color:inherit;text-align:left;font:inherit;',
  });
  button.addEventListener('click', async () => {
    closeTabMenu();
    await handler();
  });
  return button;
}

function showTabMenu(event, path) {
  closeTabMenu();
  const otherPaths = loadedBufferPaths().filter(candidate => candidate !== path);
  const savedPaths = loadedBufferPaths().filter(candidate => !state.buffers.get(candidate)?.dirty);
  const menu = el('div', {
    class: 'doc-tab-dropdown code-editor-tab-menu',
    role: 'menu',
    'aria-label': `Tab actions for ${path}`,
    style: 'position:fixed;z-index:1000;min-width:150px;padding:4px;background:var(--panel);border:1px solid var(--border);border-radius:8px;box-shadow:0 8px 24px rgba(0,0,0,.3);backdrop-filter:blur(12px);font-size:12px;',
  });
  menu.append(
    tabMenuAction('Close', 'close', path, () => closeBuffer(path)),
    tabMenuAction('Close Others', 'close-others', path, () => closeBuffers(otherPaths, `Close other tabs beside ${displayName(path)}`)),
    tabMenuAction('Close Saved', 'close-saved', path, () => closeBuffers(savedPaths, 'Close saved tabs')),
  );
  document.body.append(menu);
  const bounds = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(8, Math.min(event.clientX, window.innerWidth - bounds.width - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(event.clientY, window.innerHeight - bounds.height - 8))}px`;
  state.tabMenu = menu;
  menu.querySelector('[role=menuitem]')?.focus({ preventScroll: true });
}

function markBufferDirty(buffer) {
  buffer.dirty = true;
  buffer.saveError = null;
  const main = state.shell?.querySelector('[data-code-editor-main]');
  if (!main) return;
  const tab = [...main.querySelectorAll('.code-editor-tab')]
    .find(candidate => candidate.title === buffer.path);
  const tabDot = tab?.querySelector('.code-editor-tab-dot');
  if (tabDot) tabDot.textContent = '●';
  if (state.activePath !== buffer.path) return;
  const titleDot = main.querySelector('.code-editor-tab-title .code-editor-tab-dot');
  if (titleDot) titleDot.textContent = '●';
  const save = main.querySelector('.code-editor-save');
  if (save) save.textContent = 'Save';
  main.querySelector('.code-editor-save-error')?.remove();
}

async function quickOpen() {
    const owner = state.workspaceOwner, root = state.root, epoch = state.workspaceEpoch;
    const current = () => owner === state.workspaceOwner && root === state.root && epoch === state.workspaceEpoch;
    const query = await styledPrompt('Enter a path relative to the current folder.', { title: 'Quick Open', confirmText: 'Open', maxLength: 2048 });
    if (!query || !current()) return;
    const relative = String(query).replace(/\\/g, '/').trim();
    if (state.rootResourceRef) {
      try {
        const resolved = await resolveEditorRelativeResource(relative);
        if (!current()) return;
        await openFile(childPath(root, relative), null, resolved.resourceRef);
      } catch (error) {
        setStatus(error.message || 'The Editor resource could not be opened', true);
      }
      return;
    }
    setStatus('Choose an authorized Editor folder before using Quick Open.', true);
}

export function runCommand(name) {
  const editor = state.buffers.get(state.activePath)?.editor;
  if (name === 'save') { void saveActive(); return Boolean(state.activePath); }
  if (name === 'palette' && typeof window.__openClankWorkbench?.openPalette === 'function') { window.__openClankWorkbench.openPalette(); return true; }
  if (name === 'quick-open' || name === 'palette') { void quickOpen(); return true; }
  if (name === 'search') return false;
  if (name === 'retry-syntax') { void editor?.retrySyntax?.(); return Boolean(editor); }
  return editor?.runCommand?.(name) || false;
}

export function getEditorStatus() { return state.buffers.get(state.activePath)?.editor?.getStatus?.() || null; }

function renderEditor() {
  const main = state.shell?.querySelector('[data-code-editor-main]');
  if (!main) return;
  state.explorerTree?.setActive(state.activePath);
  main.replaceChildren();
  if (!state.activePath) {
    main.append(el('div', { class: 'code-editor-empty' }, el('div', { class: 'code-editor-empty-mark', text: '</>' }), el('h2', { text: 'Open a file' }), el('p', { text: 'Choose a file from the workspace tree. The app can browse the visible filesystem; agent permissions remain separate.' })));
    return;
  }
  const buffer = bufferFor(state.activePath);
  const tabs = el('div', { class: 'code-editor-tabs', role: 'tablist', 'aria-label': 'Open files' });
  for (const [path, item] of state.buffers) {
    if (!item.loaded) continue;
    const tabWrap = el('div', { class: `code-editor-tab-wrap ${path === state.activePath ? 'active' : ''}` });
    const tab = el('button', { class: `code-editor-tab ${path === state.activePath ? 'active' : ''}`, type: 'button', role: 'tab', 'aria-selected': path === state.activePath ? 'true' : 'false', title: path });
    tab.append(el('span', { class: 'code-editor-tab-dot', text: item.dirty ? '●' : '·' }), el('span', { text: displayName(path) }));
    tab.addEventListener('click', () => { state.fileActivationSequence += 1; state.activePath = path; renderEditor(); });
    const close = el('button', { class: 'code-editor-tab-close', type: 'button', title: `Close ${displayName(path)}`, 'aria-label': `Close ${displayName(path)}` });
    close.innerHTML = '<span aria-hidden="true">×</span>';
    close.addEventListener('click', (event) => { event.stopPropagation(); void closeBuffer(path); });
    tabWrap.append(tab, close);
    tabWrap.addEventListener('auxclick', (event) => { if (event.button === 1) { event.preventDefault(); void closeBuffer(path); } });
    registerAdapter(tabWrap, {
      commands: () => [
        { id: 'code-tab-close', label: 'Close tab' },
        { id: 'code-tab-close-others', label: 'Close other tabs', disabled: loadedBufferPaths().filter(candidate => candidate !== path).length === 0 },
        { id: 'code-tab-close-saved', label: 'Close saved tabs', disabled: loadedBufferPaths().filter(candidate => !state.buffers.get(candidate)?.dirty).length === 0 },
      ],
      execute: async (command) => {
        if (command === 'code-tab-close') { await closeBuffer(path); return true; }
        if (command === 'code-tab-close-others') {
          await closeBuffers(loadedBufferPaths().filter(candidate => candidate !== path), `Close other tabs beside ${displayName(path)}`);
          return true;
        }
        if (command === 'code-tab-close-saved') {
          await closeBuffers(loadedBufferPaths().filter(candidate => !state.buffers.get(candidate)?.dirty), 'Close saved tabs');
          return true;
        }
        return false;
      },
    });
    tabs.append(tabWrap);
  }
  const title = el('div', { class: 'code-editor-tab-title' }, el('span', { class: 'code-editor-tab-dot', text: buffer.dirty ? '●' : '·' }), el('span', { class: 'code-editor-tab-path', text: state.activePath }));
  const showInFiles = toolButton('Show in Files', 'folder-open', () => { void showActiveInFiles(); });
  showInFiles.classList.add('code-editor-show-in-files');
  const save = el('button', { class: 'code-editor-save', type: 'button', text: buffer.dirty ? 'Save' : 'Saved', onclick: saveActive });
  const editorHost = buffer.editorHost || (buffer.editorHost = el('div', { class:'code-editor-codemirror', 'aria-label':`Edit ${displayName(buffer.path)}` }));
  if (!buffer.editor) {
    if (!state.editorFactory) {
      editorHost.append(el('div', { class: 'code-editor-loading', text: state.editorLoadError ? 'Source editor unavailable.' : 'Loading source editor…' }));
    } else {
      buffer.editor = state.editorFactory({
      parent: editorHost,
      doc: buffer.text,
      label: `Edit ${displayName(state.activePath)}`,
      mode: 'source',
      language: languageForPath(state.activePath),
      lineNumbers: true,
      readableLineWidth: false,
      lineWrapping: false,
      selection: buffer.selection,
      scrollTop: buffer.scrollTop,
      richComments: buffer.richComments === true && state.richCommentLanguageQualified(languageForPath(state.activePath), { dialect: languageDialectForPath(state.activePath), path: state.activePath }),
      languageDialect: languageDialectForPath(state.activePath),
      languagePath: state.activePath,
      renderPreview: (source) => {
        const renderer = codeEditorMarkdownRenderer();
        try { return renderer.renderPreview(source); } catch (_) { return null; }
      },
      renderComment:source=>codeEditorMarkdownRenderer().renderComment(source),
      getLocalRevision:()=>buffer.revision || 0,
      getPendingSourceEdit:id=>buffer.pendingSourceEdits?.get(id)||null,
      getPendingSourceEdits:()=>[...(buffer.pendingSourceEdits?.values()||[])],
      registerPendingSourceEdit:edit=>{buffer.pendingSourceEdits ||= new Map();buffer.pendingSourceEdits.set(edit.id,edit);markBufferDirty(buffer);return true;},
      removePendingSourceEdit:id=>{const removed=buffer.pendingSourceEdits?.delete(id);buffer.dirty=!!buffer.pendingSourceEdits?.size||buffer.text!==buffer.originalText;return !!removed;},
      onSeeSource: (range) => {
        buffer.editor?.revealCommentSource?.(range.from, range.to);
        setStatus('Showing comment source. Press Escape to keep editing the file.');
      },
      onSyntaxStatus: status => {
        if (state.buffers.get(buffer.path) !== buffer || state.activePath !== buffer.path) return;
        const label = state.shell?.querySelector('[data-code-syntax-status]');
        if (label) label.textContent = status.message;
        queueCodeCycle(buffer);
      },
      onSelection: selection => { buffer.selection = selection; },
      onScroll: scrollTop => { buffer.scrollTop = scrollTop; },
      onChange: value => { buffer.cycleSignature = null; buffer.cycleOccurrenceId = null; buffer.text = value; buffer.revision = (buffer.revision || 0) + 1; markBufferDirty(buffer); },
      // Rename/move rekeys the same buffer and mutates buffer.path. Resolve the
      // destination at command time rather than capturing the original path.
      onCommand: command => {
        if (command === 'save') { void saveBuffer(buffer.path); return true; }
        if (command === 'quick-open' || command === 'palette') return runCommand(command);
        return false;
      },
      });
    }
  } else {
    // Reattach the complete stable host, including readiness/loading state.
    buffer.editor.focus();
  }
  if (buffer.editor) bindBufferContextMenu(buffer, editorHost);
  const mode = el('span', { class:'code-editor-language', text:buffer.editor?.getStatus?.().language?.name || languageForPath(state.activePath) });
  const syntaxStatus = el('span', { 'data-code-syntax-status':'', role:'status', 'aria-live':'polite', text:buffer.editor?.getSyntaxStatus?.().message || 'Loading syntax…' });
  const richCommentQualified = state.richCommentLanguageQualified(languageForPath(state.activePath), { dialect: languageDialectForPath(state.activePath), path: state.activePath });
  const activeRichComments = richCommentQualified && buffer.richComments === true;
  const richComments = el('button', {
    class: 'code-editor-rich-comments', type: 'button', text: activeRichComments ? 'Raw comments' : richCommentQualified ? 'Rich comments' : 'Rich comments unavailable',
    title: activeRichComments ? 'Show source comments' : richCommentQualified ? 'Render parser-recognized comments when safe' : 'Rich comments are unavailable until this parser has a qualified source map',
    disabled: richCommentQualified ? null : 'true',
    'aria-pressed': activeRichComments ? 'true' : 'false',
    onclick: () => {
      if (!richCommentQualified) return;
      buffer.richComments = !activeRichComments;
      saveRichCommentsDefault(buffer.richComments);
      buffer.editor?.setRichComments?.(buffer.richComments);
      renderEditor();
      queueCodeCycle(buffer);
      setStatus(buffer.richComments ? 'Rich comments enabled for this file' : 'Raw comments restored');
    },
  });
  const saveError = buffer.saveError;
  const errorBanner = saveError ? el('div', { class: `code-editor-save-error ${saveError.code === 'conflict' ? 'conflict' : ''}`, role: 'alert' },
    el('span', { text: saveError.code === 'conflict' ? 'The file changed on disk. Your edits remain unsaved.' : saveError.message }),
  ) : null;
  if (saveError) {
    const reload = el('button', { type: 'button', class: 'code-editor-reload-disk', text: 'Reload disk copy' });
    reload.addEventListener('click', () => { void reloadBufferFromDisk(state.activePath); });
    errorBanner.append(reload);
  }
  main.append(el('div', { class: 'code-editor-toolbar' }, tabs, title, mode, syntaxStatus, richComments, showInFiles, save), ...(errorBanner ? [errorBanner] : []), editorHost);
  queueCodeCycle(buffer);
  requestAnimationFrame(() => { if (state.activePath === buffer.path && editorHost.isConnected && (document.activeElement === document.body || main.contains(document.activeElement))) buffer.editor?.focus(); });
}

async function reloadBufferFromDisk(path) {
  const buffer = state.buffers.get(path);
  if (!buffer) return false;
  const reloadOwner = state.workspaceOwner;
  const reloadRoot = state.root;
  const reloadWorkspaceEpoch = state.workspaceEpoch;
  const stillCurrent = () => state.workspaceOwner === reloadOwner
    && state.root === reloadRoot
    && state.workspaceEpoch === reloadWorkspaceEpoch
    && state.buffers.get(path) === buffer;
  const choice = await styledConfirm(
    `Discard local edits in ${displayName(path)} and reload the disk copy?`,
    { title: 'Reload from disk', confirmText: 'Reload', alternateText: 'Keep edits', cancelText: 'Cancel' },
  );
  if (choice !== true || !stillCurrent()) return false;
  buffer.reloadController?.abort?.();
  const controller = new AbortController();
  const reloadRevision = buffer.revision || 0;
  buffer.reloadController = controller;
  const ownsCompletion = () => !controller.signal.aborted
    && stillCurrent()
    && (buffer.revision || 0) === reloadRevision;
  try {
    if (!buffer.resourceRef) throw new Error('Files authorization expired; reopen this file from the authorized folder.');
    const response = await filesFacadeClient.openResource(buffer.resourceRef, { signal: controller.signal });
    if (!ownsCompletion()) return false;
    const payload = response?.payload || {};
    const resource = payload?.resource || response?.resource || {};
    const text = String(payload.text ?? payload.content ?? '');
    buffer.editor?.setValue?.(text);
    buffer.text = text;
    buffer.originalText = text;
    buffer.fingerprint = resource.revision?.value || payload.fingerprint?.value || null;
    buffer.expectedRevision = resource.revision || buffer.expectedRevision || null;
    buffer.resourceRef = String(resource.ref || buffer.resourceRef || '');
    buffer.encoding = payload.encoding || resource.metadata?.encoding || buffer.encoding;
    buffer.newline = payload.newline || resource.metadata?.newline || buffer.newline;
    buffer.dirty = false;
    buffer.saveError = null;
    buffer.revision = (buffer.revision || 0) + 1;
    buffer.savedRevision = buffer.revision;
    if (state.activePath === path) renderEditor();
    setStatus(`Reloaded ${displayName(path)} from disk`);
    return true;
  } catch (error) {
    if (error?.name === 'AbortError' || !ownsCompletion()) return false;
    buffer.saveError = { code: error?.code || 'reload_failed', message: error.message || 'Disk copy could not be reloaded' };
    if (state.activePath === path) renderEditor();
    setStatus(buffer.saveError.message, true);
    return false;
  } finally {
    if (buffer.reloadController === controller) buffer.reloadController = null;
  }
}

async function closeBuffers(paths, context = 'Close files') {
  if (state.closePromise) {
    await state.closePromise;
    return closeBuffers(paths, context);
  }
  const targets = [...new Set(paths)].filter(path => state.buffers.get(path)?.loaded);
  if (!targets.length) return true;
  const workspaceEpoch = state.workspaceEpoch;
  const task = (async () => {
    if (!(await resolveDirtyBuffers(targets, context))) return false;
    if (workspaceEpoch !== state.workspaceEpoch) return false;
    detachBuffers(targets);
    renderEditor();
    return true;
  })();
  state.closePromise = task;
  try { return await task; }
  finally { if (state.closePromise === task) state.closePromise = null; }
}

function closeBuffer(path) {
  return closeBuffers([path], `Close ${displayName(path)}`);
}

async function reopenLastClosedBuffer() {
  while (state.closedBuffers.length) {
    const metadata = state.closedBuffers.pop();
    updateReopenClosedAction();
    if (
      metadata.owner !== state.workspaceOwner
      || metadata.root !== state.root
      || state.buffers.has(metadata.path)
    ) continue;
    const reopened = await openFile(metadata.path, metadata, metadata.resourceRef);
    if (reopened) {
      setStatus(`Reopened ${displayName(metadata.path)}`);
      return true;
    }
    return false;
  }
  setStatus('No closed file to reopen');
  return false;
}

async function recordCodeCycle(buffer) {
  const accountId = achievementOwner(), workspaceId = savedWorkspaceId();
  const text = buffer.editor?.getValue?.() ?? buffer.text;
  const revision = buffer.expectedRevision?.value;
  const localRevision = buffer.revision;
  const rich = buffer.richComments === true;
  const editor = buffer.editor;
  if (!accountId || !buffer.resourceIdentity || !revision || buffer.dirty || text !== buffer.originalText || !editor?.getCommentSourceMapAsync) return;
  try {
    const regions = await editor.getCommentSourceMapAsync();
    const supported = state.richCommentLanguageQualified(languageForPath(buffer.path), { dialect:languageDialectForPath(buffer.path), path:buffer.path });
    if (!supported || !regions.some(region => region.markdown?.trim()) || !await visiblePresentation(buffer.editorHost)) return;
    if (state.buffers.get(buffer.path) !== buffer || state.activePath !== buffer.path || buffer.editor !== editor || buffer.revision !== localRevision || buffer.dirty || buffer.richComments !== rich || editor.getValue() !== text || buffer.expectedRevision?.value !== revision) return;
    const renderedRich = !!buffer.editorHost.querySelector('.cm-rich-comment-widget');
    if (rich !== renderedRich) return;
    const sourceHash = await activityDigest(text);
    const documentId = await activityDigest(buffer.resourceIdentity);
    if (!buffer.cycleOccurrenceId) { if (!rich) return; buffer.cycleOccurrenceId = crypto.randomUUID(); }
    const cycleOccurrenceId = buffer.cycleOccurrenceId;
    const signature = `${revision}:${sourceHash}:${rich}`;
    if (buffer.cycleSignature === signature) return;
    buffer.cycleSignature = signature;
    await recordPresentation('source.view.cycle', { documentId, sourceRevisionId:String(revision), sourceHash,
      supportedProgrammingDocument:true, cycleOccurrenceId, cycleSequence:rich ? 'rich' : 'source' }, { accountId, workspaceId });
  } catch (_) { /* Unavailable/changed syntax cannot prove a visible cycle. */ }
}
function queueCodeCycle(buffer) {
  buffer.cycleDelivery = (buffer.cycleDelivery || Promise.resolve()).then(() => recordCodeCycle(buffer)).catch(() => {});
}
async function recordCodeSave(buffer, text, acceptedRevision, accountId, workspaceId) {
  const editor = buffer.editor;
  if (!acceptedRevision || !editor?.getCommentSourceMapAsync || editor.getValue() !== text) return;
  try {
    const regions = await editor.getCommentSourceMapAsync();
    const grammarId = editor.getStatus?.()?.language?.id;
    if (editor.getValue() !== text || !LANGUAGE_REGISTRY.some(entry => entry.id === grammarId)) return;
    const region = regions.find(item => ['comment','docstring'].includes(item.kind) && item.markdown?.trim());
    if (region) await recordPresentation('document.rich-region.saved', {
      documentId:await activityDigest(buffer.resourceIdentity), revisionId:String(acceptedRevision), grammarId,
      regionKind:region.kind, supportedGrammar:true, markdownNonempty:true,
    }, { accountId, workspaceId, kind:'R', occurrenceId:`${await activityDigest(buffer.resourceIdentity)}:${acceptedRevision}` });
  } catch (_) { /* Exact accepted syntax evidence only. */ }
}

async function saveBuffer(path) {
  if (!path) return false;
  const buffer = bufferFor(path);
  for(const edit of [...(buffer.pendingSourceEdits?.values()||[])]){
    const result=edit.flush?.();
    if(!result||!['queued','unchanged'].includes(result.outcome)){setStatus(result?.message||'Review the pending comment body before saving.',true);return false;}
    buffer.pendingSourceEdits.delete(edit.id);
  }
  if (!buffer.dirty) return true;
  if (buffer.savePromise) return buffer.savePromise;
  // Explorer refresh has its own request generation and must not invalidate a
  // successful file save. Bind completion to the exact buffer/workspace owner
  // instead: account or workspace teardown removes/replaces that identity,
  // while an in-place tree refresh leaves it valid.
  const achievementAccount = achievementOwner(), achievementWorkspace = savedWorkspaceId();
  const saveOwner = state.workspaceOwner;
  const saveRoot = state.root;
  const saveWorkspaceEpoch = state.workspaceEpoch;
  const controller = new AbortController();
  buffer.saveController = controller;
  const ownsCompletion = () => !controller.signal.aborted
    && state.workspaceOwner === saveOwner
    && state.root === saveRoot
    && state.workspaceEpoch === saveWorkspaceEpoch
    && state.buffers.get(path) === buffer;
  const savePromise = (async () => {
  const revision = buffer.revision || 0;
  const text = buffer.editor?.getValue?.() ?? buffer.text;
  buffer.text = text;
  if (text === buffer.originalText) {
    buffer.dirty = false;
    buffer.saveError = null;
    if (state.activePath === path) renderEditor();
    return true;
  }
  if (!buffer.resourceRef) {
    buffer.saveError = { code: 'resource_reference_missing', message: 'Files authorization expired; reopen this file before saving.' };
    if (state.activePath === path) renderEditor();
    return false;
  }
  setStatus(`Saving ${displayName(path)}…`);
  try {
    // Patch the complete original snapshot when possible. Rust then keeps the
    // detected encoding/BOM/newline/mode instead of silently rewriting every
    // source file as UTF-8/LF. Empty files have no searchable old value, so
    // their first save uses the normal replace contract.
    const response = await filesFacadeClient.saveResource(buffer.resourceRef, {
      expectedRevision: buffer.expectedRevision || { kind: 'hostFingerprint', value: buffer.fingerprint },
      text,
      signal: controller.signal,
    });
    if (!ownsCompletion()) return false;
    if (response?.outcome === 'conflict') {
      const conflict = new Error('File changed on disk; your edits remain unsaved.');
      conflict.code = 'conflict';
      throw conflict;
    }
    const result = response?.data || response || {};
    buffer.expectedRevision = result.revision || result.snapshot?.expectedRevision || buffer.expectedRevision;
    buffer.fingerprint = buffer.expectedRevision?.value || result.new_fingerprint?.value || result.Replaced?.fingerprint?.value || result.Created?.fingerprint?.value || result.fingerprint?.value || null;
    if ((response?.outcome || result.outcome) === 'applied' && buffer.expectedRevision?.value) void recordCodeSave(buffer, text, buffer.expectedRevision.value, achievementAccount, achievementWorkspace);
    buffer.cycleSignature = null;
    buffer.cycleOccurrenceId = null;
    buffer.originalText = text;
    buffer.saveError = null;
    buffer.savedRevision = revision;
    if ((buffer.revision || 0) === revision) buffer.dirty = !!buffer.pendingSourceEdits?.size;
    if (state.activePath === path) renderEditor();
    setStatus(`${buffer.dirty ? 'Saved revision; newer edits remain' : 'Saved'} ${displayName(path)}`);
    return !buffer.dirty;
  } catch (error) {
    if (error?.name === 'AbortError' || !ownsCompletion()) return false;
    buffer.saveError = { code: error?.code || 'save_failed', message: error.message || 'Save failed; your edits remain unsaved.' };
    if (state.activePath === path) renderEditor();
    setStatus(buffer.saveError.code === 'conflict' ? 'File changed on disk; your edits remain unsaved.' : buffer.saveError.message, true);
    return false;
  }
  })();
  buffer.savePromise = savePromise;
  try { return await savePromise; }
  finally {
    if (buffer.savePromise === savePromise) buffer.savePromise = null;
    if (buffer.saveController === controller) buffer.saveController = null;
  }
}

function saveActive() { return saveBuffer(state.activePath); }

async function showActiveInFiles() {
  const path = state.activePath;
  const activeTitle = document.querySelector('.code-editor-tab.active')?.getAttribute('title') || state.shell?.querySelector('.code-editor-tab.active')?.getAttribute('title') || '';
  const workspaceId = savedWorkspaceId();
  if (!path || !workspaceId) {
    setStatus('This file is not bound to a Workspace yet', true);
    return false;
  }
  try {
    let relative;
    try { relative = workspaceRelativePath(activeTitle || path); } catch (_) { relative = ''; }
    if (!relative && workspaceId) {
      const activeLabel = document.querySelector('.code-editor-tab.active')?.getAttribute('title') || state.shell?.querySelector('.code-editor-tab.active')?.getAttribute('title') || '';
      relative = state.workspaceOpenRelative || displayName(path) || displayName(activeLabel);
    }
    const files = await import('./files.js');
    const reveal = files.revealWorkspaceResource || files.default?.revealWorkspaceResource;
    if (typeof reveal !== 'function') throw new Error('Files integration is unavailable');
    const shown = await reveal(workspaceId, relative);
    if (shown) setStatus(`Shown in Files: ${displayName(path)}`);
    return Boolean(shown);
  } catch (error) {
    setStatus(error.message || 'File could not be shown in Files', true);
    return false;
  }
}

function createShell() {
  let nativeWindow;
  nativeWindow = createOpenClankWindow({
    id: 'code-editor-window',
    label: 'Editor',
    subtitle: 'Workspace',
    minWidth: 600,
    minHeight: 460,
    sizeKey: 'odysseus-code-editor-window-size',
    className: 'code-editor-window',
    onActivate: () => markLauncherActive(true),
    onBeforeClose: () => resolveDirtyBuffers(
      [...state.buffers.keys()],
      'Close Editor',
    ),
    onClosed: handleWindowClosed,
  });
  const shell = el('section', { class: 'code-editor-shell', role: 'application', 'aria-label': 'Editor' });
  const filesHead = el('div', { class: 'code-editor-files-head' }, el('span', { text: 'WORKSPACE' }));
  const reopenClosed = toolButton('Reopen last closed file', 'refresh', () => { void reopenLastClosedBuffer(); });
  reopenClosed.setAttribute('data-code-reopen-closed', '');
  reopenClosed.disabled = true;
  filesHead.append(
    toolButton('Open workspace folder', 'folder', openWorkspaceFolder),
    toolButton('New file', 'file', createWorkspaceFile),
    toolButton('New folder', 'folder-plus', createWorkspaceDirectory),
    reopenClosed,
    toolButton('Refresh', 'refresh', refreshExplorer),
  );
  const files = el('aside', { class: 'code-editor-files', 'aria-label': 'Workspace files' }, filesHead, el('div', { class: 'code-editor-root', 'data-code-root': true }), el('div', { class: 'code-tree', role: 'tree', 'aria-label': 'Workspace files', 'data-code-tree': true }));
  const separator = el('div', { class: 'oc-explorer-separator', role: 'separator', tabindex: '0', 'aria-orientation': 'vertical', 'aria-label': 'Resize workspace explorer' });
  const main = el('main', { class: 'code-editor-main', 'data-code-editor-main': true });
  shell.append(files, separator, main);
  nativeWindow.body.replaceChildren(shell);
  state.nativeWindow = nativeWindow;
  state.shell = shell;
  state.pane = createResizablePane({ container: shell, sidebar: files, main, separator, cssVar: '--code-editor-sidebar-width', storageKey: 'odysseus-code-pane-width', defaultWidth: 240, minWidth: 180, maxWidth: 420, mainMin: 320, getOwner: () => state.workspaceOwner });
  nativeWindow.root.addEventListener('keydown', async (event) => {
    if (event.defaultPrevented || event.isComposing) return;
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 't') { event.preventDefault(); await reopenLastClosedBuffer(); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'w' && state.activePath) { event.preventDefault(); await closeBuffer(state.activePath); return; }
    if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'p') return;
    event.preventDefault();
    await quickOpen();
  });
  document.addEventListener('pointerdown', (event) => {
    if (state.tabMenu && !state.tabMenu.contains(event.target)) closeTabMenu();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && state.tabMenu) {
      event.preventDefault();
      closeTabMenu();
    }
  }, true);
  // Mobile sheet dismissal happens outside the window API. Route it back
  // through the same close lifecycle so routing, launcher state, and stale
  // async generations are cleaned up exactly once.
  window.addEventListener('modal-dismissed', (event) => {
    if (event.detail?.id === nativeWindow.id) nativeWindow.requestClose();
  });
  window.addEventListener('workspace-change', async (event) => {
    // Every chat-scope transition invalidates an older pending root load. An
    // empty event is deliberate while a newly selected chat's opaque
    // Workspace ID is being resolved, and must not allow the old result to
    // repaint if that resolution later fails.
    const generation = ++state.requestGeneration;
    const next = String(event.detail?.path || '').trim();
    const workspaceId = String(event.detail?.workspaceId || '').trim();
    const previousRootResourceRef = state.rootResourceRef;
    if (!next || !nativeWindow.visible) return;
    if (next === state.root && workspaceId && workspaceId === savedWorkspaceId(state.workspaceOwner)) return;
    try {
      if (workspaceId && typeof filesFacadeClient.workspaceResource === 'function') {
        // A Workspace event carries a display path for legacy consumers. The
        // Editor must replace its authority with a freshly checked Files ref
        // before the new root can reach the explorer or any buffer operation.
        const target = await filesFacadeClient.workspaceResource(workspaceId, '', {});
        if (generation !== state.requestGeneration || !nativeWindow.visible) return;
        const rootRef = String(target?.resource?.ref || target?.parent?.ref || '').trim();
        const rootKind = String(target?.resource?.kind || target?.parent?.kind || '').toLowerCase();
        if (!rootRef || !['folder', 'provider_root', 'virtual_folder'].includes(rootKind)) {
          throw new Error('Files did not return an authorized Workspace folder.');
        }
        state.rootResourceRef = rootRef;
      } else {
        // A path-only event cannot authorize a mounted Editor transition.
        // Require the Files picker to issue a workspace/resource ref.
        state.rootResourceRef = '';
        state.activeDirectoryRef = '';
        throw new Error('Choose the folder through the authorized Files picker.');
      }
      const loaded = await displayWorkspaceRoot(next, generation);
      if (loaded && generation === state.requestGeneration && nativeWindow.visible) {
        if (workspaceId) saveWorkspaceRoot(next, state.workspaceOwner, workspaceId);
        else {
          const workspace = await workspaceModule.bindWorkspacePath(next, 'app_folder');
          saveWorkspaceRoot(workspace.path, state.workspaceOwner, workspace.id);
        }
      } else if (!loaded && generation === state.requestGeneration) {
        // A cancelled dirty-buffer transition must leave the old tree and its
        // authority paired; do not strand a newly resolved ref on that view.
        state.rootResourceRef = previousRootResourceRef;
      }
    } catch (_) {
      if (generation === state.requestGeneration) state.rootResourceRef = previousRootResourceRef;
      /* the existing editor folder remains usable on a failed sync */
    }
  });
  // init.js dispatches this non-bubbling event on document. Listen at the
  // actual boundary so an account change cannot leave the prior owner's
  // workspace, tabs, in-flight saves, or reopen metadata in the Code window.
  document.addEventListener('openclank:auth-user-ready', (event) => {
    void handleAuthenticatedOwnerReady(event.detail?.username);
  });
  document.addEventListener('openclank:file-policy-changed', () => {
    void handleCodeFilePolicyChanged();
  });
  return shell;
}

async function open() {
  if (!state.shell) createShell();
  state.nativeWindow.show(document.activeElement);
  markLauncherActive(true);
  setStatus('Loading workspace…');
  const generation = ++state.requestGeneration;
  let root;
  try {
    root = await workspaceRoot();
  } catch (error) {
    if (generation !== state.requestGeneration || error?.name === 'AbortError') return false;
    renderEditor();
    setStatus('Workspace access could not be revalidated; unsaved changes were retained', true);
    return false;
  }
  if (generation !== state.requestGeneration || !state.nativeWindow.visible) return;
  await displayWorkspaceRoot(root, generation);
}

async function activateBoundWorkspace(workspace, openRelative = '') {
  const workspaceId = String(workspace?.id || '').trim();
  const root = String(workspace?.path || '').trim();
  if (!workspaceId || !root) throw new Error('Workspace could not be resolved');
  const relative = String(openRelative || '').trim();
  if (relative && (
    relative === '.'
    || relative === '..'
    || relative.startsWith('/')
    || /^[A-Za-z]:[\\/]/.test(relative)
    || relative.split(/[\\/]+/).some(part => !part || part === '.' || part === '..')
  )) throw new Error('Workspace resource is invalid');

  if (typeof filesFacadeClient.workspaceResource !== 'function') {
    throw new Error('Choose the folder through the authorized Files picker.');
  }
  const target = await filesFacadeClient.workspaceResource(workspaceId, relative, {});
  const rootRef = String(target?.parent?.ref || target?.resource?.ref || '').trim();
  if (!rootRef) throw new Error('Files did not return an authorized Workspace folder.');
  state.rootResourceRef = rootRef;

  const generation = ++state.requestGeneration;
  const loaded = await displayWorkspaceRoot(root, generation);
  if (!loaded || generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
  saveWorkspaceRoot(root, state.workspaceOwner, workspaceId);
  if (relative) {
    const targetRef = String(target?.resource?.ref || '').trim();
    if (!targetRef) throw new Error('Files did not return the opened resource reference.');
    return openFile(childPath(root, relative), null, targetRef);
  }
  return true;
}

async function ensureEditorOwner() {
  const identity = await authenticatedOwner();
  if (identity.status !== 'confirmed' || !identity.owner) {
    if (state.workspaceOwner) return state.workspaceOwner;
    throw new Error('Authenticated account is unavailable');
  }
  if (state.workspaceOwner && state.workspaceOwner !== identity.owner) {
    clearWorkspaceOwnerState(identity.owner);
  } else {
    state.workspaceOwner = identity.owner;
    state.richCommentsDefault = savedRichCommentsDefault(identity.owner);
  }
  state.pane?.refresh?.();
  return identity.owner;
}

/** Open one opaque Host resource without returning its absolute path from Files. */
export async function openResource(resourceRef) {
  // Files' Code action is a compatibility entry. Copal owns the unified
  // Editor buffer when the production module is present.
  if (window.copalModule?.openResource) return window.copalModule.openResource(resourceRef);
  if (!state.shell) createShell();
  state.nativeWindow.show(document.activeElement);
  markLauncherActive(true);
  // Canonical registry address — never a /copal/ history write.
  const editorPath = appletPath('editor');
  if (location.pathname !== editorPath) history.pushState({}, '', editorPath);
  setStatus('Opening in Editor…');
  state.resourceOpenController?.abort?.();
  const controller = new AbortController();
  state.resourceOpenController = controller;
  const requestGeneration = state.requestGeneration;
  try {
    await ensureEditorOwner();
    const owner = state.workspaceOwner;
    const workspaceEpoch = state.workspaceEpoch;
    const current = () => state.resourceOpenController === controller && !controller.signal.aborted
      && requestGeneration === state.requestGeneration && owner === state.workspaceOwner
      && workspaceEpoch === state.workspaceEpoch && state.nativeWindow?.visible;
    if (!current()) throw new Error('Editor access changed; choose the resource again.');
    const response = await filesFacadeClient.workspace(resourceRef, 'app_folder', { signal:controller.signal });
    if (!current()) throw new Error('Editor access changed; choose the resource again.');
    const workspaceId = String(response?.workspace?.id || '').trim();
    state.workspaceOpenRelative = String(response?.open_relative || '').trim();
    const workspace = await workspaceModule.resolveWorkspaceId(workspaceId, 'app_folder');
    if (!current()) throw new Error('Editor access changed; choose the resource again.');
    // Re-resolve the exact target through Files before loading the workspace.
    // The Workspace path is a display hint; the explorer and buffer must carry
    // the sealed folder/resource refs returned by this authority check.
    const target = await filesFacadeClient.workspaceResource(workspaceId, response?.open_relative || '', { signal: controller.signal });
    if (!current()) throw new Error('Editor access changed; choose the resource again.');
    const rootRef = String(target?.parent?.ref || target?.resource?.ref || '').trim();
    if (!rootRef) throw new Error('Files did not return an authorized Editor folder.');
    state.rootResourceRef = rootRef;
    const loaded = await activateBoundWorkspace(workspace, '');
    if (!loaded) return false;
    const targetRef = String(target?.resource?.ref || '').trim();
    const targetKind = String(target?.resource?.kind || '').toLowerCase();
    if (targetRef && targetKind !== 'folder' && targetKind !== 'provider_root' && targetKind !== 'virtual_folder') {
      return openFile(childPath(workspace.path, response?.open_relative || target?.resource?.name || ''), null, targetRef);
    }
    return true;
  } catch (error) {
    if (error?.name === 'AbortError') return false;
    setStatus(error.message || 'Resource could not be opened in Editor', true);
    return false;
  } finally {
    if (state.resourceOpenController === controller) state.resourceOpenController = null;
  }
}

/** Save an authorized Host snapshot through the Files facade CAS boundary. */
export async function saveResourceSnapshot(snapshot, { resource } = {}) {
  const opaqueRef = resource?.locator?.opaqueRef;
  if (!opaqueRef) return { outcome:'failed', retryable:false, uncertain:false, message:'Host resource reference is unavailable' };
  try {
    const response = await filesFacadeClient.saveResource(opaqueRef, {
      expectedRevision:snapshot.expectedRevision,
      text:String(snapshot.envelope?.text ?? ''),
    });
    if (response?.outcome === 'conflict') return response;
    if (response?.outcome !== 'applied' || !response.revision) {
      return { outcome:'failed', uncertain:true, message:'Host save did not return an accepted fingerprint; its outcome needs reconciliation' };
    }
    const accepted = response.snapshot && typeof response.snapshot === 'object' ? response.snapshot : {};
    return {
      outcome:'applied',
      revision:response.revision,
      snapshot:{
        ...snapshot,
        ...accepted,
        expectedRevision:response.revision,
        envelope:{
          ...snapshot.envelope,
          ...(accepted.envelope || {}),
          text:String(accepted.envelope?.text ?? snapshot.envelope?.text ?? ''),
          ...(accepted.envelope?.metadata ? { metadata:accepted.envelope.metadata } : {}),
        },
      },
    };
  } catch (error) {
    const status = Number(error?.status || 0);
    return { outcome:'failed', code:error?.code, status, uncertain:status === 0 || status >= 500 || [408, 429].includes(status), message:error.message || 'Host save failed' };
  }
}

/** Explicit compatibility shim for callers that have not migrated to a ref. */
export async function openPath(path, { directory = false } = {}) {
  void path; void directory;
  setStatus('Choose this item from the authorized Files view before opening it in Editor.', true);
  return false;
}

function handleWindowClosed() {
  state.workspacePicker?.destroy();
  state.workspacePicker = null;
  state.requestGeneration += 1;
  state.resourceOpenController?.abort?.();
  state.resourceOpenController = null;
  state.fileGeneration += 1;
  abortPendingFileReads();
  state.rootTreeController?.abort();
  state.rootTreeController = null;
  state.explorerTree?.destroy?.();
  state.explorerTree = null;
  state.bufferReconcileController?.abort?.();
  state.bufferReconcileController = null;
  for (const buffer of state.buffers.values()) {
    buffer.saveController?.abort?.();
    buffer.reloadController?.abort?.();
    buffer.contextMenuDispose?.();
    buffer.contextMenuDispose = null;
  }
  closeTabMenu();
  markLauncherActive(false);
  window._restoreSidebarIfRouteCollapsed?.();
  if (location.pathname === '/code') history.pushState({}, '', '/');
}

function close() {
  state.nativeWindow?.requestClose();
}

export default {
  init() {},
  open,
  openPath,
  openResource,
  runCommand, getEditorStatus,
  close,
  toggle: () => (state.nativeWindow?.visible ? close() : open()),
};
