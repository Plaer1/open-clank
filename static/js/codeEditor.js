import { filesFacadeClient } from './filesFacadeClient.js';
import { createOpenClankWindow } from './copal/windows.js';
import workspaceModule from './workspace.js';
import { styledConfirm, styledPrompt } from './ui.js';
import { createResizablePane } from './editor/resizablePane.js';
import { createExplorerTree } from './editor/explorerTree.js';
import { createResourcePicker } from './copal/resourcePicker.js';
import { languageForPath as sharedLanguageForPath, sortEntries as sortEntryList, isDirectory as entryIsDirectory } from './editor/entryModel.js';
import { fileIcon, glyphIcon } from './langIcons.js';
import { initCustomContextMenu, registerAdapter, createCodeMirrorContextAdapter } from './custom-context-menu.js';

const CODE_WORKSPACE_KEY = 'odysseus-code-workspace';
const CODE_WORKSPACE_ID_KEY = 'odysseus-code-workspace-id';
const CODE_RICH_COMMENTS_KEY = 'odysseus-editor-rich-comments';

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
  pane: null,
  editorFactory: null,
  richCommentLanguageQualified: () => false,
  editorLoadPromise: null,
  editorLoadError: null,
  closedBuffers: [],
  closePromise: null,
  tabMenu: null,
  richCommentsDefault: false,
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
    const page = await loadInitialCodeRoot(resolvedRoot, generation);
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
  updateReopenClosedAction();
}

async function resolveDirtyBuffers(paths, context = 'close') {
  const workspaceEpoch = state.workspaceEpoch;
  const dirtyEntries = [...new Set(paths)]
    .map(path => [path, state.buffers.get(path)])
    .filter(([, buffer]) => buffer?.dirty);
  if (!dirtyEntries.length) return true;
  const dirty = dirtyEntries.map(([path]) => path);
  const stillCurrent = () => workspaceEpoch === state.workspaceEpoch
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

async function listCodeDirectory(path, { cursor = null, signal = null } = {}) {
  const parentRef = String(path || state.rootResourceRef || '').trim();
  if (!parentRef || !state.rootResourceRef && !String(path || '').startsWith('rr')) {
    throw new Error('Choose an authorized Editor folder before browsing.');
  }
  const response = await filesFacadeClient.children(parentRef, {
    cursor,
    sort: { key: 'name', direction: 'asc', directories_first: true, collation: 'open-clank-v1' },
    signal,
  });
  return {
    items: (response?.entries || []).map(entry => codeExplorerEntry(entry, String(state.root || path || 'Workspace'), parentRef)),
    nextCursor: response?.next_cursor || null,
  };
}

async function loadInitialCodeRoot(path, generation) {
  state.rootTreeController?.abort();
  const controller = new AbortController();
  state.rootTreeController = controller;
  try {
    const page = await listCodeDirectory(path, { signal: controller.signal });
    if (generation !== state.requestGeneration || controller.signal.aborted) return null;
    return page;
  } finally {
    if (state.rootTreeController === controller) state.rootTreeController = null;
  }
}

async function handleCodeTreeContextMenu(event, entry) {
  // Turning off the app-owned menu restores the browser's ordinary context
  // menu for the Explorer too. Do this before preventDefault so the setting
  // has one predictable meaning across Editor, Files, and Copal objects.
  if (window.openClankContextMenu?.enabled && !window.openClankContextMenu.enabled()) return;
  event.preventDefault();
  const child = entry.path;
  const resourceRef = String(entry.resource_ref || entry.resourceRef || '').trim();
  if (!resourceRef) {
    setStatus('Refresh the authorized Editor folder before changing this file.', true);
    return;
  }
  const action = await styledPrompt('File action: rename, copy, move, or trash', { title: 'File action', defaultValue: 'rename', confirmText: 'Continue', maxLength: 16 });
  if (!action) return;
  const normalized = action.trim().toLowerCase();
  if (!['rename', 'copy', 'move', 'trash'].includes(normalized)) { setStatus('Unknown file action', true); return; }
  if (normalized === 'trash') {
    const affected = subtreeBufferEntries(child).map(([path]) => path);
    if (!(await resolveDirtyBuffers(affected, `Trash ${displayName(child)}`))) return;
    if (!await styledConfirm(`Move ${entry.name} to recoverable trash?`, { title: 'Move to Trash', confirmText: 'Move to Trash', danger: true })) return;
    // Reserve the entire subtree, not just the tabs that happened to be open
    // before the request. A second file can be opened or edited while the host
    // Trash operation is in flight; recomputing from this reservation keeps it
    // recoverable instead of silently detaching a late buffer.
    const reservation = beginTreeMutation('trash', child);
    try {
      const trashed = await filesFacadeClient.action(resourceRef, 'trash', {}, { actionId: operationId('editor-trash') });
      const changedDuringTrash = treeMutationBuffersChanged(reservation);
      if (changedDuringTrash) {
        const trashEntry = trashed?.resource || null;
        if (trashEntry) {
          try {
            await filesFacadeClient.action(String(trashEntry.ref || resourceRef), 'restore', {}, { actionId: operationId('editor-restore') });
            await refreshExplorer();
            setStatus(`Kept ${entry.name}; it changed while Trash was running`, true);
            return;
          } catch (_) { /* retain the in-memory buffers below */ }
        }
        const retained = retainOrphanedTrashBuffers(child);
        if (retained.length) renderEditor();
        await refreshExplorer();
        setStatus(`Newer edits to ${entry.name} were retained after Trash`, true);
        return;
      }
      const currentAffected = subtreeBufferEntries(child).map(([path]) => path);
      detachBuffers(currentAffected, { remember: false });
      if (currentAffected.length) renderEditor();
      await refreshExplorer();
      setStatus(`Moved ${entry.name} to recoverable trash`);
    } catch (error) { setStatus(error.message || 'Trash failed', true); }
    finally { finishTreeMutation(reservation); }
    return;
  }
  const destinationName = validChildName(await styledPrompt(`${normalized} as (name only)`, { title: `${normalized[0].toUpperCase()}${normalized.slice(1)} file`, defaultValue: entry.name, confirmText: 'Continue', maxLength: 240 }));
  if (!destinationName) { setStatus('A simple file name is required', true); return; }
  const destination = childPath(parentPath(child), destinationName);
  const affected = normalized === 'rename' || normalized === 'move'
    ? subtreeBufferEntries(child).map(([path]) => path)
    : [];
  if (!(await resolveDirtyBuffers(affected, `${normalized[0].toUpperCase()}${normalized.slice(1)} ${displayName(child)}`))) return;
  const reservation = normalized === 'rename' || normalized === 'move'
    ? beginTreeMutation(normalized, child, destination)
    : null;
  try {
    if (resourceRef) {
      if (normalized === 'copy') {
        const destinationRef = String(entry.parent_resource_ref || state.rootResourceRef || '').trim();
        if (!destinationRef) throw new Error('Authorized destination folder is unavailable');
        await filesFacadeClient.transferResources({
          operationId: operationId('editor-copy'), generation: Number(state.fileGeneration), kind: 'copy',
          sources: [{ itemId: operationId('editor-item'), resourceRef, expectedRevision: entry.revision || undefined }],
          destinationRef, collision: 'fail',
        });
      } else {
        const result = await filesFacadeClient.action(resourceRef, normalized, { name: destinationName }, { actionId: operationId(`editor-${normalized}`) });
        const rotatedRef = String(result?.resource?.ref || '').trim();
        if (rotatedRef) {
          for (const [, buffer] of state.buffers) {
            if (buffer.resourceRef === resourceRef) buffer.resourceRef = rotatedRef;
          }
        }
      }
    } else {
      throw new Error('Files did not authorize this Editor mutation. Refresh the folder and try again.');
    }
    if (reservation) {
      const rekeyed = rekeyTreeMutationBuffers(reservation);
      if (rekeyed.length) renderEditor();
    }
    await refreshExplorer();
    setStatus(`${normalized[0].toUpperCase()}${normalized.slice(1)}d ${displayName(destination)}`);
  } catch (error) { setStatus(error.message || `${normalized} failed`, true); }
  finally { if (reservation) finishTreeMutation(reservation); }
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
      const page = await loadInitialCodeRoot(state.root, generation);
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

async function prepareWorkspaceChange(nextRoot) {
  if (!state.root || state.root === nextRoot) return true;
  const dirty = [...state.buffers.keys()].filter(path => state.buffers.get(path)?.dirty);
  if (!(await resolveDirtyBuffers(dirty, `Switching workspace to ${displayName(nextRoot)}`))) return false;
  state.workspaceEpoch += 1;
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

async function displayWorkspaceRoot(root, generation = ++state.requestGeneration) {
  if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
  const tree = state.shell?.querySelector('[data-code-tree]');
  if (!tree) return false;
  setStatus(`Opening ${displayName(root)}…`);
  try {
    // Fetch the candidate page while the current root/buffers/tree remain
    // visible. A denied or stale folder therefore cannot replace a usable
    // Editor view with an empty/error root.
    const page = root ? await loadInitialCodeRoot(root, generation) : { items:[], nextCursor:null };
    if (!page || generation !== state.requestGeneration || !state.nativeWindow.visible) return false;
    if (!(await prepareWorkspaceChange(root))) return false;
    if (generation !== state.requestGeneration || !state.nativeWindow?.visible) return false;
    state.fileGeneration += 1;
    abortPendingFileReads();
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
    if (error?.name === 'AbortError' || generation !== state.requestGeneration) return false;
    setStatus(error.message || 'Folder unavailable', true);
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
    const picker = createResourcePicker({
      client:filesFacadeClient,
      purpose:'folder',
      rootLabel:'Authorized Editor folders',
      getGeneration:() => state.fileGeneration,
      getAccountScope:() => state.workspaceOwner,
      onSelect:(resource) => {
        const owner = state.workspaceOwner; const epoch = state.workspaceEpoch; const generation = state.fileGeneration;
        void (async () => {
          try {
            const response = await filesFacadeClient.workspace(resource.ref, 'app_folder');
            if (owner !== state.workspaceOwner || epoch !== state.workspaceEpoch || generation !== state.fileGeneration) throw new Error('Editor access changed; choose the folder again.');
            const workspaceId = String(response?.workspace?.id || '').trim();
            if (!workspaceId) throw new Error('Files did not issue an Editor workspace.');
            const workspace = await workspaceModule.resolveWorkspaceId(workspaceId, 'app_folder');
            if (owner !== state.workspaceOwner || epoch !== state.workspaceEpoch || generation !== state.fileGeneration) throw new Error('Editor access changed; choose the folder again.');
            state.rootResourceRef = resource.ref;
            const loaded = await displayWorkspaceRoot(workspace.path, ++state.requestGeneration);
            if (!loaded) throw new Error('The authorized folder could not be loaded.');
            if (owner !== state.workspaceOwner || epoch !== state.workspaceEpoch || generation + 1 !== state.fileGeneration) throw new Error('Editor access changed; choose the folder again.');
            saveWorkspaceRoot(workspace.path, state.workspaceOwner, workspace.id);
            setStatus(`Opened ${resource.name}`);
          } catch (error) { setStatus(error.message || 'Folder could not be opened', true); }
        })();
      },
    });
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
      const response = await filesFacadeClient.openResource(entry.resourceRef, { signal: controller.signal });
      if (!ownsCompletion()) return false;
      const payload = response?.payload || {};
      const resource = payload?.resource || response?.resource || {};
      const text = String(payload.text ?? payload.content ?? '');
      const editorFactory = await loadEditorFactory();
      if (!ownsCompletion()) return false;
      Object.assign(buffer, {
        text,
        originalText: text,
        fingerprint: resource.revision?.value || payload.fingerprint?.value || null,
        expectedRevision: resource.revision || null,
        resourceRef: String(resource.ref || entry.resourceRef || ''),
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
    tab.addEventListener('click', () => { state.activePath = path; renderEditor(); });
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
  const editorHost = el('div', { class: 'code-editor-codemirror', 'aria-label': `Edit ${displayName(state.activePath)}` });
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
      richComments: buffer.richComments === true && state.richCommentLanguageQualified(languageForPath(state.activePath)),
      onSelection: selection => { buffer.selection = selection; },
      onScroll: scrollTop => { buffer.scrollTop = scrollTop; },
      onChange: value => { buffer.text = value; buffer.revision = (buffer.revision || 0) + 1; markBufferDirty(buffer); },
      // Rename/move rekeys the same buffer and mutates buffer.path. Resolve the
      // destination at command time rather than capturing the original path.
      onCommand: command => { if (command === 'save') void saveBuffer(buffer.path); },
      });
    }
  } else {
    editorHost.append(buffer.editor.view.dom);
    buffer.editor.focus();
  }
  if (buffer.editor) bindBufferContextMenu(buffer, editorHost);
  const mode = el('span', { class: 'code-editor-language', text: languageForPath(state.activePath) });
  const richCommentQualified = state.richCommentLanguageQualified(languageForPath(state.activePath));
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
      buffer.selection = buffer.editor?.getSelection?.() || buffer.selection;
      buffer.scrollTop = buffer.editor?.getScrollTop?.() ?? buffer.scrollTop;
      buffer.editor?.destroy?.(); buffer.editor = null; renderEditor();
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
  main.append(el('div', { class: 'code-editor-toolbar' }, tabs, title, mode, richComments, showInFiles, save), ...(errorBanner ? [errorBanner] : []), editorHost);
  requestAnimationFrame(() => buffer.editor?.focus());
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

async function saveBuffer(path) {
  if (!path) return false;
  const buffer = bufferFor(path);
  if (!buffer.dirty) return true;
  if (buffer.savePromise) return buffer.savePromise;
  // Explorer refresh has its own request generation and must not invalidate a
  // successful file save. Bind completion to the exact buffer/workspace owner
  // instead: account or workspace teardown removes/replaces that identity,
  // while an in-place tree refresh leaves it valid.
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
    buffer.originalText = text;
    buffer.saveError = null;
    buffer.savedRevision = revision;
    if ((buffer.revision || 0) === revision) buffer.dirty = false;
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
  const workspaceId = savedWorkspaceId();
  if (!path || !workspaceId) {
    setStatus('This file is not bound to a Workspace yet', true);
    return false;
  }
  try {
    const relative = workspaceRelativePath(path);
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
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && event.key.toLowerCase() === 't') { event.preventDefault(); await reopenLastClosedBuffer(); return; }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'w' && state.activePath) { event.preventDefault(); await closeBuffer(state.activePath); return; }
    if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'p') return;
    event.preventDefault();
    const query = await styledPrompt('Enter a path relative to the current folder.', { title: 'Quick Open', confirmText: 'Open', maxLength: 2048 });
    if (!query) return;
    const relative = String(query).replace(/\\/g, '/').trim();
    if (state.rootResourceRef) {
      try {
        const resolved = await resolveEditorRelativeResource(relative);
        await openFile(childPath(state.root, relative), null, resolved.resourceRef);
      } catch (error) {
        setStatus(error.message || 'The Editor resource could not be opened', true);
      }
      return;
    }
    setStatus('Choose an authorized Editor folder before using Quick Open.', true);
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
  const root = await workspaceRoot();
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
  if (location.pathname !== '/copal/editor') history.pushState({}, '', '/copal/editor');
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
  if (!opaqueRef) return { outcome:'failed', message:'Host resource reference is unavailable' };
  try {
    const response = await filesFacadeClient.saveResource(opaqueRef, {
      expectedRevision:snapshot.expectedRevision,
      text:String(snapshot.envelope?.text ?? ''),
    });
    if (response?.outcome === 'conflict') return response;
    if (response?.outcome !== 'applied' || !response.revision) {
      return { outcome:'failed', message:'Host save did not return an accepted fingerprint' };
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
    return { outcome:'failed', code:error?.code, status:error?.status, message:error.message || 'Host save failed' };
  }
}

/** Explicit compatibility shim for callers that have not migrated to a ref. */
export async function openPath(path, { directory = false } = {}) {
  void path; void directory;
  setStatus('Choose this item from the authorized Files view before opening it in Editor.', true);
  return false;
}

function handleWindowClosed() {
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
  close,
  toggle: () => (state.nativeWindow?.visible ? close() : open()),
};
