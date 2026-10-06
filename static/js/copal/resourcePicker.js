import { isChatResource, editorDestinationReason } from './resourceDestinations.js';
/* Files-v1 resource picker shared by Editor and Notes.
 *
 * The picker is deliberately a display/selection adapter.  It only sends
 * opaque refs to the facade, never a browser File, filesystem path, or a
 * mutation command.  The returned selection is deeply immutable so callers
 * can safely retain it across an async open handoff.
 */

import { wireDialog } from './overlays.js';
import { filesBrowserResource, installEditorFilesStyles, mountEditorFilesBrowser } from './editorFilesBrowser.js';

export const RESOURCE_PICKER_PURPOSES = Object.freeze(new Set(['file', 'folder', 'template-folder']));
const MAX_PAGE = 200;
const EVENT_NAMES = [
  'openclank-account-changed', 'openclank-files-policy-changed', 'openclank-policy-changed',
  'openclank-window-closed', 'openclank:auth-context-changed', 'openclank:file-policy-changed',
];

function text(value, fallback = '') {
  const result = String(value ?? '').trim();
  return result || fallback;
}

function freeze(value, seen = new WeakSet()) {
  if (!value || typeof value !== 'object' || seen.has(value)) return value;
  seen.add(value);
  Object.values(value).forEach(child => freeze(child, seen));
  return Object.freeze(value);
}

function capabilities(value) {
  if (Array.isArray(value)) {
    if (value.some(name => typeof name !== 'string' || !name.trim())) throw new TypeError('Files row capabilities are malformed');
    return Object.fromEntries(value.map(name => [name, true]));
  }
  if (!value || typeof value !== 'object') return {};
  if (Object.entries(value).some(([name, enabled]) => typeof name !== 'string' || enabled !== true)) throw new TypeError('Files row capabilities are malformed');
  return Object.fromEntries(Object.entries(value).filter(([name]) => typeof name === 'string' && value[name] === true));
}

function rowRef(row) {
  const value = row?.ref ?? row?.resource_ref ?? row?.resourceRef ?? row?.resource?.ref;
  return typeof value === 'string' ? text(value) : '';
}

function rowKind(row) {
  const kind = text(row?.kind ?? row?.resource?.kind).toLowerCase();
  return ['folder', 'directory', 'provider_root', 'provider-root'].includes(kind) ? 'folder' : 'file';
}

// ResourceKey is an object in the Files facade. Keep every authority-bearing
// component when adapting it for DOM identity; String(object) would collapse
// unrelated owner/workspace/provider resources to "[object Object]".
function canonicalResourceKey(row, accountScope = '', workspaceScope = '') {
  const value = row?.resource_key ?? row?.resource?.key ?? row?.resourceId ?? row?.resourceKey ?? row?.id;
  const source = value && typeof value === 'object' ? value : null;
  const account = text(source?.accountId ?? source?.account_id ?? source?.owner ?? source?.account, accountScope);
  const workspace = text(source?.workspaceId ?? source?.workspace_id ?? source?.workspace, workspaceScope);
  const provider = text(source?.provider ?? row?.provider ?? row?.resource?.provider, 'unknown');
  const id = text(source?.resourceId ?? source?.resource_id ?? source?.id ?? source?.key ?? (source ? '' : value));
  if (id) {
    // Match S02's complete ResourceKey spelling without feeding an already
    // scoped object back through its string-key compatibility branch (which
    // would duplicate account/workspace components).
    const scope = [account, workspace].filter(Boolean).join('|');
    return `${scope ? `${scope}|` : ''}${provider}:${id}`;
  }
  return '';
}

/** Convert one facade row into the only identity shape emitted by this UI. */
export function normalizeAuthorizedResource(row, {
  purpose = 'file', generation = 0, accountScope = '', workspaceScope = '', parentRef = null,
} = {}) {
  if (!RESOURCE_PICKER_PURPOSES.has(purpose)) throw new TypeError(`Unsupported picker purpose: ${purpose}`);
  if (!row || typeof row !== 'object' || Array.isArray(row)) throw new TypeError('Files row is malformed');
  const ref = rowRef(row);
  if (!ref) throw new TypeError('Files row has no opaque resource reference');
  const kind = rowKind(row);
  const rawCapabilities = row?.capabilities ?? row?.resource?.capabilities;
  if (rawCapabilities != null && (typeof rawCapabilities !== 'object' || rawCapabilities === null)) throw new TypeError('Files row capabilities are malformed');
  const caps = capabilities(rawCapabilities);
  const revision = row?.revision ?? row?.resource?.revision ?? null;
  if (revision != null && (typeof revision !== 'object' || Array.isArray(revision) || typeof revision.kind !== 'string' || typeof revision.value !== 'string' || !revision.kind.trim() || !revision.value.trim() || Object.keys(revision).some(key => !['kind', 'value'].includes(key)))) throw new TypeError('Files row revision is malformed');
  const resourceKeyValue = row?.resource_key ?? row?.resourceKey ?? row?.resource?.key;
  if (resourceKeyValue != null && typeof resourceKeyValue !== 'string' && (typeof resourceKeyValue !== 'object' || Array.isArray(resourceKeyValue))) throw new TypeError('Files row ResourceKey is malformed');
  if (resourceKeyValue && typeof resourceKeyValue === 'object' && (Object.keys(resourceKeyValue).some(key => !['provider', 'account_id', 'accountId', 'workspace_id', 'workspaceId', 'resource_id', 'resourceId', 'id', 'key', 'owner', 'account', 'workspace'].includes(key)) || Object.values(resourceKeyValue).some(value => typeof value !== 'string'))) throw new TypeError('Files row ResourceKey is malformed');
  const provider = text(row?.provider ?? row?.resource?.provider);
  if (!provider) throw new TypeError('Files row provider is missing');
  const resourceKey = canonicalResourceKey(row, accountScope, workspaceScope);
  if (!resourceKey) throw new TypeError('Files row ResourceKey is missing');
  const identity = {
    ref,
    resourceRef: ref,
    resourceKey,
    resourceId:text(row?.resource_id ?? row?.resourceId ?? (typeof row?.id === 'string' ? row.id : null) ?? resourceKeyValue?.resource_id ?? resourceKeyValue?.resourceId),
    provider,
    open_target:row.open_target || row.openTarget || null,
    provenance:row.provenance || null,
    kind,
    name: text(row?.name ?? row?.display_name ?? row?.resource?.name, ref),
    logicalPath: text(row?.logical_path ?? row?.logicalPath ?? row?.path ?? row?.resource?.logical_path),
    capabilities: caps,
    revision: revision && typeof revision === 'object' ? { kind:text(revision.kind), value:text(revision.value) } : null,
    generation: Number.isSafeInteger(Number(generation)) ? Number(generation) : 0,
    accountScope: text(accountScope),
    workspaceScope: text(workspaceScope),
    ...(parentRef ? { parentRef:text(parentRef) } : {}),
  };
  return freeze(identity);
}

function rowsFrom(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('Files response is malformed.');
  if (payload.error != null || payload.errors != null || payload.detail != null) throw new Error('Files response reports an error.');
  const hasEntries = Object.prototype.hasOwnProperty.call(payload, 'entries');
  const hasItems = Object.prototype.hasOwnProperty.call(payload, 'items');
  if (hasEntries === hasItems || (!hasEntries && !hasItems)) throw new Error('Files response has no authorized entry page.');
  const rows = hasEntries ? payload.entries : payload.items;
  if (!Array.isArray(rows) || rows.length > 5001 || rows.some(row => !row || typeof row !== 'object' || Array.isArray(row))) throw new Error('Files response contains malformed entries.');
  return rows;
}

function cursorFrom(payload) {
  const value = payload?.next_cursor ?? payload?.nextCursor ?? null;
  if (value != null && (typeof value !== 'string' || !value.trim() || value.length > 16384)) throw new Error('Files response cursor is malformed.');
  return value;
}

function generationFrom(payload, fallback) {
  const hasGeneration = Object.prototype.hasOwnProperty.call(payload || {}, 'policy_generation') || Object.prototype.hasOwnProperty.call(payload || {}, 'generation');
  const value = Number(payload?.policy_generation ?? payload?.generation ?? fallback);
  if (hasGeneration && (!Number.isSafeInteger(value) || value < 0)) throw new Error('Files response generation is malformed.');
  return Number.isSafeInteger(value) && value >= 0 ? value : fallback;
}

function defaultElement(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') element.textContent = String(value);
    else if (key === 'class') element.className = String(value);
    else if (key.startsWith('on') && typeof value === 'function') element.addEventListener(key.slice(2), value);
    else if (value !== false && value != null) element.setAttribute(key, String(value));
  }
  element.append(...children.filter(Boolean));
  return element;
}

/** Selection/lifetime adapter for the actual shared Files browser. */
export function createResourcePicker({
  client, purpose = 'file', h = defaultElement, accountScope = '', workspaceScope = '', generation = 0,
  signal = null, getGeneration = null, getAccountScope = null, getWorkspaceScope = null,
  isOriginCurrent = null, originWindow = null, initialDirectory = null, isCompatible = null,
  onSelect = null, onCancel = null, onClose = null, rootLabel = 'Files', mount = null,
} = {}) {
  if (!client || typeof client.roots !== 'function' || typeof client.children !== 'function') throw new TypeError('Files facade client is required');
  if (!RESOURCE_PICKER_PURPOSES.has(purpose)) throw new TypeError(`Unsupported picker purpose: ${purpose}`);
  const state = {
    closed:false, destroyed:false, controller:new AbortController(), generation:Number(generation) || 0,
    accountScope:text(accountScope || getAccountScope?.()), workspaceScope:text(workspaceScope || getWorkspaceScope?.()),
    roots:[], rows:[], selected:null, currentFolder:null, parentRef:null, cursor:null, query:'',
    loading:false, confirming:false, error:null, dom:null, browser:null, requestEpoch:0,
    requestController:null, navigation:[], externalGeneration:getGeneration ? Number(getGeneration()) : null,
  };
  const isCurrent = () => !state.closed && !state.destroyed && !state.controller.signal.aborted
    && (!state.dom || state.dom.open && state.dom.isConnected);
  const scopeStillCurrent = () => isCurrent()
    && (!getGeneration || Number(getGeneration()) === state.externalGeneration)
    && (!getAccountScope || text(getAccountScope()) === state.accountScope)
    && (!getWorkspaceScope || text(getWorkspaceScope()) === state.workspaceScope)
    && (!isOriginCurrent || isOriginCurrent());
  const normalize = (row, parentRef = null) => normalizeAuthorizedResource(filesBrowserResource(row), {
    purpose, generation:state.generation, accountScope:state.accountScope, workspaceScope:state.workspaceScope, parentRef,
  });
  const reason = (row) => {
    if (row && isChatResource(row)) return 'Chats belong in Chats or Library, outside Editor.';
    if (purpose === 'file' && row && editorDestinationReason(row)) return editorDestinationReason(row);
    if (!row) return purpose === 'file' ? 'Select a file to open.' : 'Select or browse to a folder.';
    if (purpose === 'file') {
      if (row.kind === 'folder') return 'Open a folder to browse its files.';
      if (row.capabilities.open !== true) return 'This resource cannot be opened in Editor.';
    } else {
      if (row.kind !== 'folder') return 'Choose a folder.';
      if (row.capabilities.children !== true) return 'This folder cannot be browsed.';
      if (purpose === 'template-folder' && !(row.capabilities.write || row.capabilities.create || row.capabilities.edit)) return 'Template folders require permission to create or edit files.';
    }
    const compatible = isCompatible?.(row);
    return compatible === false ? 'This resource is not supported by this Editor.' : typeof compatible === 'string' ? compatible : '';
  };
  const candidate = () => state.selected || (purpose !== 'file' ? state.currentFolder : null);
  const render = () => {
    if (!state.dom) return;
    const confirm = state.dom.querySelector('[data-resource-picker-confirm]');
    const status = state.dom.querySelector('[data-resource-picker-status]');
    const row = candidate();
    if (confirm) {
      confirm.disabled = !scopeStillCurrent() || state.loading || state.confirming || !!reason(row);
      confirm.textContent = state.confirming ? 'Opening…' : purpose === 'template-folder' ? 'Choose Folder' : 'Open';
      confirm.title = reason(row) || row?.name || '';
    }
    if (status) {
      status.textContent = state.error?.message || (state.confirming ? 'Opening in the original Editor…' : reason(row) || row?.name || '');
      status.classList.toggle('error', !!state.error);
    }
    state.dom.setAttribute('aria-busy', String(state.loading || state.confirming));
  };
  const beginRequest = () => {
    state.requestController?.abort();
    const controller = new AbortController();
    state.requestController = controller;
    return { controller, epoch:++state.requestEpoch };
  };
  const requestStillCurrent = request => scopeStillCurrent()
    && state.requestEpoch === request.epoch && state.requestController === request.controller && !request.controller.signal.aborted;
  // Retain the existing non-DOM authorized-page API. DOM navigation/search
  // belong exclusively to the shared browser below.
  const loadPage = async (kind, ref = null, options = {}) => {
    if (!scopeStillCurrent()) return Object.freeze([]);
    const request = beginRequest();
    state.loading = true; state.error = null; render();
    try {
      const response = kind === 'roots'
        ? await client.roots({ copalWorkspace:state.workspaceScope || 'default', signal:request.controller.signal })
        : kind === 'search'
          ? await client.search(state.query, { limit:MAX_PAGE, signal:request.controller.signal })
          : await client.children(ref, { ...options, limit:MAX_PAGE, signal:request.controller.signal });
      if (!requestStillCurrent(request)) return Object.freeze([]);
      state.generation = generationFrom(response, state.generation);
      const rows = rowsFrom(response).filter(row => !isChatResource(row)).map(row => normalize(row, ref));
      const previous = options.append ? state.rows : [];
      const seen = new Set(previous.map(row => row.resourceKey));
      state.rows = [...previous, ...rows.filter(row => !seen.has(row.resourceKey) && !!seen.add(row.resourceKey))].slice(0, 5001);
      state.parentRef = ref; state.cursor = cursorFrom(response);
      if (kind === 'roots') state.roots = [...state.rows];
      return Object.freeze([...rows]);
    } catch (error) {
      if (error?.name === 'AbortError' || !requestStillCurrent(request)) return Object.freeze([]);
      state.error = error; throw error;
    } finally { if (state.requestController === request.controller) state.loading = false; render(); }
  };
  const loadRoots = () => loadPage('roots');
  const loadChildren = (parent, options = {}) => {
    const ref = text(typeof parent === 'string' ? parent : parent?.ref || parent?.resourceRef || parent?.resource_ref);
    if (!ref) throw new TypeError('Folder resource reference is required');
    return loadPage('children', ref, { query:state.query, sort:{ key:'name', direction:'asc', directories_first:true }, ...options });
  };
  const search = query => {
    state.query = text(query);
    return state.query ? loadPage('search') : state.parentRef ? loadChildren(state.parentRef) : loadRoots();
  };
  const select = value => {
    if (!scopeStillCurrent() || state.confirming) return false;
    try {
      state.selected = value ? normalize(value, state.parentRef) : null;
      state.error = null; render();
      return !reason(candidate());
    } catch (error) { state.selected = null; state.error = error; render(); return false; }
  };
  const close = (cancelled = true) => {
    if (state.closed) return;
    state.closed = true; state.loading = false;
    state.controller.abort(); state.requestController?.abort();
    state.browser?.dispose(); state.browser = null;
    detachLifecycle();
    const dialog = state.dom; state.dom = null;
    if (dialog?.open) dialog.close();
    else dialog?.remove();
    if (cancelled) onCancel?.();
    onClose?.(cancelled);
  };
  const confirm = async value => {
    if (state.confirming || state.loading) return false;
    if (value && !select(value)) { render(); return false; }
    const selected = candidate();
    if (!scopeStillCurrent()) { close(); return false; }
    if (reason(selected)) { render(); return false; }
    state.confirming = true; state.error = null; render();
    try {
      // A displayed row is never authority for a later open. Revalidate its
      // exact opaque reference before handing the immutable identity back.
      let authorized = selected;
      if (typeof client.stat === 'function') {
        const result = await client.stat(selected.ref, { signal:state.controller.signal });
        if (!scopeStillCurrent()) { close(); return false; }
        state.generation = generationFrom(result, state.generation);
        authorized = normalize(result?.resource || result, selected.parentRef || state.parentRef);
        if (authorized.resourceKey !== selected.resourceKey || authorized.provider !== selected.provider || authorized.kind !== selected.kind) throw new Error('The selected resource changed. Select it again.');
      }
      const unavailable = reason(authorized);
      if (unavailable) throw new Error(unavailable);
      const result = await onSelect?.(authorized, { signal:state.controller.signal, isCurrent:scopeStillCurrent });
      if (result === false) throw new Error('The Editor could not open this resource. Try again.');
      if (!isCurrent()) return false;
      close(false); return true;
    } catch (error) {
      if (!isCurrent() || error?.name === 'AbortError') return false;
      if (!scopeStillCurrent()) { close(); return false; }
      state.error = error; return false;
    } finally { state.confirming = false; render(); }
  };
  const directoryChanged = row => {
    if (!scopeStillCurrent()) return;
    try {
      state.currentFolder = row ? normalize(row) : null;
      state.parentRef = state.currentFolder?.ref || null;
      state.selected = null; state.error = null; render();
    } catch (error) { state.error = error; render(); }
  };
  const enter = async value => {
    const folder = normalize(value);
    if (folder.kind !== 'folder' || folder.capabilities.children !== true || !scopeStillCurrent()) return false;
    if (state.browser) return state.browser.openDirectory(filesBrowserResource(value));
    const previous = { folder:state.currentFolder, ref:state.parentRef, rows:state.rows, cursor:state.cursor };
    try { await loadChildren(folder); } catch (_) { return false; }
    if (!scopeStillCurrent()) return false;
    state.navigation.push(previous); state.currentFolder = folder; state.selected = null; render(); return true;
  };
  const back = () => {
    if (state.browser) return state.browser.window?.navigation?.back?.();
    const previous = state.navigation.pop();
    if (!previous) return false;
    state.currentFolder = previous.folder; state.parentRef = previous.ref; state.rows = previous.rows; state.cursor = previous.cursor; state.selected = null; render(); return true;
  };
  const open = async () => {
    if (!scopeStillCurrent() || state.dom) return false;
    if (typeof document === 'undefined') { await loadRoots(); return true; }
    installEditorFilesStyles();
    const browserHost = h('div', { class:'copal-resource-picker-browser' });
    const dialog = h('dialog', { class:'copal-dialog copal-resource-picker copal-files-selection-dialog', 'aria-label':`${rootLabel}: ${purpose}` },
      h('h2', { text:purpose === 'folder' ? 'Open Folder' : purpose === 'template-folder' ? 'Choose template folder' : 'Open File' }),
      browserHost,
      h('p', { class:'copal-resource-picker-status', 'data-resource-picker-status':true, role:'status', 'aria-live':'polite' }),
      h('footer', { class:'copal-dialog-actions' },
        h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => close() }),
        h('button', { type:'button', class:'copal-btn primary', 'data-resource-picker-confirm':true, disabled:true, text:'Open', onclick:() => { void confirm(); } })),
    );
    state.dom = dialog;
    (mount || document.body).append(dialog);
    // Native close notifications are queued. Invalidate the callback scope in
    // the dismissal event itself, before an awaited open can resume.
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(); }, { capture:true });
    dialog.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || event.isComposing || !dialog.open) return;
      event.preventDefault(); event.stopPropagation(); close();
    }, { capture:true });
    dialog.addEventListener('pointerdown', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom) return;
      event.preventDefault(); event.stopPropagation(); close();
    }, { capture:true });
    wireDialog(dialog, { restoreFocus:!onClose });
    dialog.addEventListener('close', () => close(), { once:true });
    dialog.showModal(); render();
    state.loading = true; render();
    try {
      const browser = await mountEditorFilesBrowser({
        container:browserHost,
        onDirectory:directoryChanged,
        onSelection:select,
        onConfirm:row => confirm(row),
      }, scopeStillCurrent);
      if (!browser || !scopeStillCurrent()) { browser?.dispose(); close(); return false; }
      state.browser = browser;
      await browser.open();
      if (!scopeStillCurrent()) { close(); return false; }
      if (initialDirectory) await browser.openDirectory(filesBrowserResource(initialDirectory));
      if (!scopeStillCurrent()) { close(); return false; }
      return true;
    } catch (error) {
      if (isCurrent()) state.error = error;
      return false;
    } finally { state.loading = false; render(); }
  };
  const onLifecycle = event => {
    if (['openclank-window-closed', 'modal-dismissed'].includes(event?.type)) {
      const id = event.detail?.id || event.detail?.windowId;
      if (!originWindow || id !== originWindow.id) return;
    }
    close();
  };
  const eventNames = [...EVENT_NAMES, 'openclank:auth-user-ready', 'workspace-change', 'modal-dismissed'];
  const lifecycleTargets = [globalThis, globalThis.document].filter(Boolean);
  let listenersAttached = false;
  const attachLifecycle = () => {
    if (listenersAttached) return;
    listenersAttached = true;
    lifecycleTargets.forEach(target => eventNames.forEach(name => target.addEventListener?.(name, onLifecycle)));
    signal?.addEventListener?.('abort', onLifecycle, { once:true });
  };
  const detachLifecycle = () => {
    if (!listenersAttached) return;
    listenersAttached = false;
    lifecycleTargets.forEach(target => eventNames.forEach(name => target.removeEventListener?.(name, onLifecycle)));
    signal?.removeEventListener?.('abort', onLifecycle);
  };
  attachLifecycle();
  const destroy = () => { state.destroyed = true; close(false); detachLifecycle(); };
  if (signal?.aborted) close(false);
  const snapshot = () => freeze({
    closed:state.closed, destroyed:state.destroyed, generation:state.generation, accountScope:state.accountScope,
    workspaceScope:state.workspaceScope, parentRef:state.parentRef, cursor:state.cursor,
    navigationDepth:state.navigation.length, currentFolder:state.currentFolder,
    query:state.query, loading:state.loading, confirming:state.confirming, error:state.error, selected:state.selected,
    rows:[...state.rows], roots:[...state.roots],
  });
  return Object.freeze({ open, close, destroy, loadRoots, loadChildren, search, select, confirm, enter, back, render, state:snapshot });
}

export default { createResourcePicker, normalizeAuthorizedResource, RESOURCE_PICKER_PURPOSES };
