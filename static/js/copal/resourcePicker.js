/* Files-v1 resource picker shared by Editor and Notes.
 *
 * The picker is deliberately a display/selection adapter.  It only sends
 * opaque refs to the facade, never a browser File, filesystem path, or a
 * mutation command.  The returned selection is deeply immutable so callers
 * can safely retain it across an async open handoff.
 */

export const RESOURCE_PICKER_PURPOSES = Object.freeze(new Set(['file', 'folder', 'template-folder']));
const MAX_PAGE = 200;
const MAX_RENDER_ROWS = 240;
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
  const value = row?.resource_key ?? row?.resourceKey ?? row?.resource?.key ?? row?.id;
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
    provider,
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

/**
 * Create a bounded, keyboard accessible Files-v1 picker.
 *
 * `picker.loadChildren(ref)` and `picker.search(query)` are also useful to a
 * non-DOM Editor explorer.  They return authorized immutable rows and never
 * mutate provider state.
 */
export function createResourcePicker({
  client,
  purpose = 'file',
  h = defaultElement,
  accountScope = '',
  workspaceScope = '',
  generation = 0,
  signal = null,
  getGeneration = null,
  getAccountScope = null,
  onSelect = null,
  onCancel = null,
  rootLabel = 'Files',
  mount = null,
} = {}) {
  if (!client || typeof client.roots !== 'function' || typeof client.children !== 'function') throw new TypeError('Files facade client is required');
  if (!RESOURCE_PICKER_PURPOSES.has(purpose)) throw new TypeError(`Unsupported picker purpose: ${purpose}`);
  const state = {
    closed:false, destroyed:false, controller:new AbortController(), generation:Number(generation) || 0,
    accountScope:text(accountScope), workspaceScope:text(workspaceScope), roots:[], rows:[], selected:null,
    parentRef:null, cursor:null, query:'', loading:false, error:null, dom:null, focusIndex:0,
    requestEpoch:0, requestController:null, navigation:[], currentFolder:null, searchTimer:null, externalGeneration:getGeneration ? Number(getGeneration()) : null,
  };
  const externalAbort = () => state.controller.abort();
  if (signal) {
    if (signal.aborted) state.controller.abort();
    else signal.addEventListener('abort', externalAbort, { once:true });
  }
  const isCurrent = () => !state.closed && !state.destroyed && !state.controller.signal.aborted;
  const scopeStillCurrent = () => isCurrent()
    && (!getGeneration || Number(getGeneration()) === state.externalGeneration)
    && (!getAccountScope || text(getAccountScope()) === state.accountScope);
  const beginRequest = () => {
    state.requestController?.abort();
    const controller = new AbortController();
    state.requestController = controller; state.requestEpoch += 1;
    return { controller, epoch:state.requestEpoch };
  };
  const requestStillCurrent = (request) => scopeStillCurrent()
    && state.requestEpoch === request.epoch && state.requestController === request.controller;
  const setRows = (rows, parentRef = null) => {
    state.rows = rows.map(row => normalizeAuthorizedResource(row, {
      purpose, generation:state.generation, accountScope:state.accountScope, workspaceScope:state.workspaceScope, parentRef,
    }));
    state.parentRef = parentRef; state.cursor = null; state.focusIndex = 0;
    return Object.freeze([...state.rows]);
  };
  const loadRoots = async () => {
    if (!isCurrent()) return Object.freeze([]);
    const request = beginRequest();
    state.loading = true; state.error = null;
    try {
      const response = await client.roots({ copalWorkspace:state.workspaceScope || 'default', signal:request.controller.signal });
      if (!requestStillCurrent(request)) return Object.freeze([]);
      state.generation = generationFrom(response, state.generation);
      state.externalGeneration = getGeneration ? Number(getGeneration()) : state.externalGeneration;
      state.roots = rowsFrom(response).map(row => normalizeAuthorizedResource(row, {
        purpose:'folder', generation:state.generation, accountScope:state.accountScope, workspaceScope:state.workspaceScope,
      }));
      state.rows = [...state.roots]; state.parentRef = null; state.cursor = null; state.focusIndex = 0;
      return Object.freeze([...state.roots]);
    } catch (error) {
      if (error?.name === 'AbortError' || !requestStillCurrent(request)) return Object.freeze([]);
      state.error = error; throw error;
    } finally { if (state.requestController === request.controller) state.loading = false; render(); }
  };
  const loadChildren = async (parent, { cursor = null, query = state.query, append = false, sort = { key:'name', direction:'asc', directories_first:true } } = {}) => {
    const parentIdentity = typeof parent === 'string' ? { ref:parent } : parent;
    const ref = text(parentIdentity?.ref ?? parentIdentity?.resourceRef);
    if (!ref) throw new TypeError('Folder resource reference is required');
    if (!isCurrent() || (append && state.loading)) return Object.freeze([]);
    const request = beginRequest();
    state.loading = true; state.error = null;
    try {
      const response = await client.children(ref, { cursor, limit:MAX_PAGE, query:String(query || ''), sort, signal:request.controller.signal });
      if (!requestStillCurrent(request)) return Object.freeze([]);
      state.generation = generationFrom(response, state.generation);
      state.externalGeneration = getGeneration ? Number(getGeneration()) : state.externalGeneration;
      const next = rowsFrom(response).map(row => normalizeAuthorizedResource(row, {
        purpose:purpose === 'file' ? 'file' : 'folder', generation:state.generation, accountScope:state.accountScope, workspaceScope:state.workspaceScope, parentRef:ref,
      }));
      if (append) {
        const seen = new Set(state.rows.map(row => `${row.provider}:${row.resourceKey || row.ref}`));
        state.rows = [...state.rows, ...next.filter(row => {
          const key = `${row.provider}:${row.resourceKey || row.ref}`;
          if (seen.has(key)) return false;
          seen.add(key); return true;
        })];
      } else state.rows = next;
      state.rows = state.rows.slice(0, 5001);
      state.parentRef = ref; state.cursor = cursorFrom(response); state.focusIndex = 0;
      render();
      return Object.freeze([...next]);
    } catch (error) {
      if (error?.name === 'AbortError' || !requestStillCurrent(request)) return Object.freeze([]);
      state.error = error; render(); throw error;
    } finally { if (state.requestController === request.controller) state.loading = false; }
  };
  const search = async (query) => {
    const value = text(query);
    state.query = value;
    if (!isCurrent()) return Object.freeze([]);
    // Clearing a query is a new request as well.  This supersedes a slow
    // search before asking the facade for the unfiltered view.
    if (!value) return state.parentRef ? loadChildren(state.parentRef, { query:value }) : loadRoots();
    const request = beginRequest();
    state.loading = true; state.error = null;
    try {
      if (typeof client.search !== 'function') throw new Error('Files search is unavailable');
      const response = await client.search(value, { limit:MAX_PAGE, signal:request.controller.signal });
      if (!requestStillCurrent(request)) return Object.freeze([]);
      state.generation = generationFrom(response, state.generation);
      state.externalGeneration = getGeneration ? Number(getGeneration()) : state.externalGeneration;
      state.rows = rowsFrom(response).slice(0, 5001).map(row => normalizeAuthorizedResource(row, {
        purpose, generation:state.generation, accountScope:state.accountScope, workspaceScope:state.workspaceScope, parentRef:null,
      }));
      state.parentRef = null; state.cursor = null; state.focusIndex = 0; render();
      return Object.freeze([...state.rows]);
    } catch (error) {
      if (error?.name === 'AbortError' || !requestStillCurrent(request)) return Object.freeze([]);
      state.error = error; render(); throw error;
    } finally { if (state.requestController === request.controller) state.loading = false; }
  };
  const select = (value) => {
    if (!value || !isCurrent()) return false;
    const selected = state.rows.find(row => row.ref === (value.ref || value.resourceRef)) || value;
    const caps = selected.capabilities || {};
    if (purpose === 'file' && (selected.kind === 'folder' || caps.open !== true || caps.read !== true)) return false;
    if ((purpose === 'folder' || purpose === 'template-folder') && (selected.kind !== 'folder' || caps.children !== true)) return false;
    if (purpose === 'template-folder' && !(caps.write === true || caps.create === true || caps.edit === true)) return false;
    state.selected = selected;
    try { onSelect?.(selected); } catch (_) { /* terminal selection still tears down the picker */ }
    return true;
  };
  const close = (cancelled = true) => {
    if (state.closed) return;
    state.closed = true; state.loading = false; state.controller.abort(); state.requestController?.abort();
    if (state.searchTimer) { clearTimeout(state.searchTimer); state.searchTimer = null; }
    state.dom?.remove(); state.dom = null;
    detachLifecycle();
    if (cancelled) onCancel?.();
  };
  const enter = async (value) => {
    const selected = state.rows.find(row => row.ref === (value?.ref || value?.resourceRef || value));
    if (!selected || selected.kind !== 'folder' || selected.capabilities?.children !== true) return false;
    const previous = { ref:state.parentRef, rows:[...state.rows], cursor:state.cursor, query:state.query, folder:state.currentFolder };
    const request = beginRequest();
    state.loading = true; state.error = null; render();
    try {
      const response = await client.children(selected.ref, { cursor:null, limit:MAX_PAGE, query:String(state.query || ''), sort:{ key:'name', direction:'asc', directories_first:true }, signal:request.controller.signal });
      if (!requestStillCurrent(request)) return false;
      state.generation = generationFrom(response, state.generation);
      state.externalGeneration = getGeneration ? Number(getGeneration()) : state.externalGeneration;
      const rows = rowsFrom(response).map(row => normalizeAuthorizedResource(row, {
        purpose:purpose === 'file' ? 'file' : 'folder', generation:state.generation,
        accountScope:state.accountScope, workspaceScope:state.workspaceScope, parentRef:selected.ref,
      })).slice(0, 5001);
      // Navigation is a successful-load transaction. Failed, aborted, or
      // stale requests leave the prior folder and its history untouched.
      state.navigation.push(previous);
      state.currentFolder = selected; state.parentRef = selected.ref;
      state.rows = rows; state.cursor = cursorFrom(response); state.focusIndex = 0;
      render(); return true;
    } catch (error) {
      if (error?.name !== 'AbortError' && requestStillCurrent(request)) { state.error = error; render(); }
      return false;
    } finally { if (state.requestController === request.controller) state.loading = false; }
  };
  const back = () => {
    const previous = state.navigation.pop();
    if (!previous) return false;
    state.parentRef = previous.ref; state.rows = previous.rows; state.cursor = previous.cursor; state.query = previous.query; state.focusIndex = 0;
    state.currentFolder = previous.folder || null; render();
    return true;
  };
  const render = () => {
    const dialog = state.dom;
    if (!dialog) return;
    const list = dialog.querySelector('[data-resource-picker-list]');
    if (!list) return;
    list.replaceChildren();
    const backButton = dialog.querySelector('[data-resource-picker-back]');
    if (backButton) { backButton.disabled = state.navigation.length === 0; backButton.onclick = () => back(); }
    const breadcrumb = dialog.querySelector('[data-resource-picker-breadcrumb]');
    if (breadcrumb) breadcrumb.textContent = state.currentFolder ? state.currentFolder.name : rootLabel;
    const total = state.rows.length;
    const start = Math.max(0, Math.min(Math.max(0, total - MAX_RENDER_ROWS), state.focusIndex - Math.floor(MAX_RENDER_ROWS / 2)));
    const visible = state.rows.slice(start, start + MAX_RENDER_ROWS);
    if (start) list.append(h('div', { class:'copal-resource-picker-spacer', style:`height:${start * 32}px`, 'aria-hidden':'true' }));
    visible.forEach((row, visibleIndex) => {
      const index = start + visibleIndex;
      const canEnter = row.kind === 'folder' && row.capabilities?.children === true;
      const selectable = (purpose === 'file' && row.kind !== 'folder' && row.capabilities?.open === true && row.capabilities?.read === true)
        || ((purpose === 'folder' || purpose === 'template-folder') && row.kind === 'folder' && row.capabilities?.children === true && (purpose !== 'template-folder' || row.capabilities?.write === true || row.capabilities?.create === true || row.capabilities?.edit === true));
      const status = canEnter || selectable ? '' : purpose === 'template-folder' ? 'read-only' : row.capabilities?.read === true ? 'unavailable' : 'read-only';
      const details = `${row.provider} · ${row.kind}${row.logicalPath ? ` · ${row.logicalPath}` : ''}${status ? ` · ${status}` : ''}`;
      const button = h('button', { type:'button', class:`copal-doc-row${state.selected?.ref === row.ref ? ' active' : ''}`, role:'option', 'aria-selected':String(state.selected?.ref === row.ref), 'aria-posinset':index + 1, 'aria-setsize':total, 'data-row-index':index, 'data-resource-ref':row.ref, disabled:!canEnter && !selectable },
        h('strong', { text:row.name }), h('small', { text:details }));
      button.tabIndex = index === state.focusIndex ? 0 : -1;
      button.addEventListener('click', () => {
        // File selection uses folder rows as navigation affordances.  A
        // folder purpose selects the folder itself; this keeps the three
        // purposes explicit while sharing one paged list.
        if (row.kind === 'folder' && canEnter) { void enter(row); return; }
        if (select(row)) close(false);
      });
      button.addEventListener('keydown', event => {
        if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'PageDown' || event.key === 'PageUp' || event.key === 'Home' || event.key === 'End') {
          event.preventDefault();
          const next = event.key === 'Home' ? 0 : event.key === 'End' ? total - 1 : Math.max(0, Math.min(total - 1, state.focusIndex + (event.key === 'PageDown' || event.key === 'PageUp' ? (event.key === 'PageDown' ? 20 : -20) : event.key === 'ArrowDown' ? 1 : -1)));
          if (!moveFocus(next)) { state.focusIndex = next; render(); const target = list.querySelector(`[data-row-index="${next}"]`); target?.focus(); target?.scrollIntoView?.({ block:'nearest' }); }
        } else if (event.key === 'Enter') {
          event.preventDefault();
          if (row.kind === 'folder' && canEnter) void enter(row);
          else if (select(row)) close(false);
        }
      });
      list.append(button);
    });
    if (start + visible.length < total) list.append(h('div', { class:'copal-resource-picker-spacer', style:`height:${(total - start - visible.length) * 32}px`, 'aria-hidden':'true' }));
    if (!visible.length) list.append(h('p', { class:'copal-empty-inline', text:state.loading ? 'Loading…' : state.error?.message || 'No authorized resources.' }));
    if (state.currentFolder && (purpose === 'folder' || purpose === 'template-folder')) {
      const required = purpose === 'template-folder' ? 'writable' : 'authorized';
      list.append(h('button', { type:'button', class:'copal-btn primary', text:`Choose this ${required} folder`, onclick:() => { if (select(state.currentFolder)) close(false); } }));
    }
    if (state.cursor) list.append(h('button', { type:'button', class:'copal-btn', text:'Load more', onclick:() => loadChildren(state.parentRef, { cursor:state.cursor, append:true }) }));
  };
  const moveFocus = index => {
    const list = state.dom?.querySelector?.('[data-resource-picker-list]');
    const target = list?.querySelector?.(`[data-row-index="${index}"]`);
    if (!target) return false;
    list.querySelector?.('[data-row-index][tabindex="0"]')?.setAttribute('tabindex', '-1');
    target.tabIndex = 0;
    state.focusIndex = index;
    target.focus();
    target.scrollIntoView?.({ block:'nearest' });
    return true;
  };
  const open = async () => {
    if (state.closed && !state.destroyed && !signal?.aborted) {
      state.closed = false;
      state.controller = new AbortController();
      state.requestController = null;
      attachLifecycle();
    }
    if (!isCurrent()) return false;
    if (state.dom) return true;
    if (typeof document !== 'undefined') {
      const dialog = h('dialog', { class:'copal-dialog copal-resource-picker', 'aria-label':`${rootLabel}: ${purpose}` },
        h('h2', { text:purpose === 'folder' ? 'Open Folder' : purpose === 'template-folder' ? 'Choose template folder' : 'Open File' }),
        h('nav', { class:'copal-resource-picker-breadcrumbs', 'aria-label':'Folder navigation' },
          h('button', { type:'button', class:'copal-btn', 'data-resource-picker-back':true, text:'Back' }),
          h('span', { 'data-resource-picker-breadcrumb':true, text:rootLabel })),
        h('input', { type:'search', placeholder:'Search authorized resources…', 'aria-label':'Search authorized resources', autocomplete:'off' }),
        h('div', { 'data-resource-picker-list':true, role:'listbox', tabindex:'0', 'aria-label':'Authorized resources' }),
        h('footer', { class:'copal-dialog-actions' }, h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => close() })),
      );
      state.dom = dialog; (mount || document.body).append(dialog); dialog.addEventListener('cancel', () => close());
      const input = dialog.querySelector('input');
      const list = dialog.querySelector('[data-resource-picker-list]');
      list.addEventListener('scroll', () => {
        const total = state.rows.length;
        const index = Math.max(0, Math.min(Math.max(0, total - 1), Math.floor(list.scrollTop / 32)));
        if (index !== state.focusIndex) { state.focusIndex = index; render(); }
      }, { passive:true });
      input.addEventListener('input', () => {
        if (state.searchTimer) clearTimeout(state.searchTimer);
        const query = input.value;
        state.searchTimer = setTimeout(() => { state.searchTimer = null; void search(query); }, 120);
      });
      dialog.showModal?.(); input.focus();
    }
    try { await loadRoots(); } catch (_) { /* rendered error state remains truthful */ }
    render(); return true;
  };
  const onLifecycle = () => close();
  const lifecycleTargets = [globalThis, globalThis.document].filter(Boolean);
  let listenersAttached = false;
  const attachLifecycle = () => {
    if (listenersAttached) return;
    listenersAttached = true;
    lifecycleTargets.forEach(target => EVENT_NAMES.forEach(name => target.addEventListener?.(name, onLifecycle)));
    signal?.addEventListener?.('abort', onLifecycle, { once:true });
  };
  const detachLifecycle = () => {
    if (!listenersAttached) return;
    listenersAttached = false;
    lifecycleTargets.forEach(target => EVENT_NAMES.forEach(name => target.removeEventListener?.(name, onLifecycle)));
    signal?.removeEventListener?.('abort', onLifecycle);
    signal?.removeEventListener?.('abort', externalAbort);
  };
  attachLifecycle();
  const destroy = () => { state.destroyed = true; close(false); detachLifecycle(); };
  if (signal?.aborted) close(false);
  const snapshot = () => freeze({
    closed:state.closed, destroyed:state.destroyed, generation:state.generation, accountScope:state.accountScope,
    workspaceScope:state.workspaceScope, parentRef:state.parentRef, cursor:state.cursor,
    navigationDepth:state.navigation.length, currentFolder:state.currentFolder,
    query:state.query, loading:state.loading, error:state.error, selected:state.selected,
    rows:[...state.rows], roots:[...state.roots],
  });
  return Object.freeze({ open, close, destroy, loadRoots, loadChildren, search, select, enter, back, render, state:snapshot });
}

export default { createResourcePicker, normalizeAuthorizedResource, RESOURCE_PICKER_PURPOSES };
