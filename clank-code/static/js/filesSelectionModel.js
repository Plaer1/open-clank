/*
 * Files selection and transfer primitives.
 *
 * This module deliberately has no DOM or browser globals.  The mounted Files
 * window owns rendering and gestures; this model owns the ordered identity
 * set so rerendering, paging, and sealed-ref rotation cannot change what a
 * user selected.
 */

const MAX_ITEMS = 5001;
const MAX_DRAG_ITEMS = 200;
const MAX_DRAG_SERIALIZED_BYTES = 64 * 1024;
const MAX_EPOCH = 0x7fffffff;
const MAX_FIELD_LENGTH = 2048;

function utf8Length(value) {
  try { return new TextEncoder().encode(String(value)).byteLength; } catch (_) { return Infinity; }
}

function text(value) {
  return String(value == null ? '' : value).trim();
}

function stableObject(value) {
  if (!value || typeof value !== 'object') return text(value);
  const provider = text(value.provider || value.provider_id);
  const id = text(value.resource_id || value.resourceId || value.id || value.key);
  const account = canonicalScopePart(value.account_id || value.accountId || value.owner_id || value.owner);
  const workspace = canonicalScopePart(value.workspace_id || value.workspaceId || value.workspace);
  const scope = [account, workspace].filter(Boolean).join('|');
  return provider && id ? `${scope ? `${scope}|` : ''}${provider}:${id}` : text(value.resource_key || value.resourceKey || value.ref);
}

function canonicalScopePart(value) {
  return text(value).replace(/[|\0]/g, '_');
}

/** Resource identity intentionally excludes the short-lived sealed ref. */
export function resourceKey(entry = {}, fallbackProvider = '', fallbackScope = {}) {
  const explicitValue = entry.resource_key || entry.resourceKey;
  const explicit = typeof explicitValue === 'string' ? text(explicitValue) : stableObject(explicitValue);
  if (explicit) {
    // The facade's current public resource_key is often the opaque stable
    // string ``resource-<digest>``.  It is stable within the server's owner
    // scope, but a provider or legacy adapter may also emit a plain key that
    // is reused by another account/workspace.  Scope every explicit string
    // with the same canonical identity used for generated keys while keeping
    // the unscoped legacy spelling byte-for-byte compatible for callers that
    // have no scope information.
    const entryWorkspace = entry.workspace_id || entry.workspaceId || entry.workspace;
    const hasFallbackWorkspace = Object.hasOwn(fallbackScope, 'workspace') || Object.hasOwn(fallbackScope, 'workspaceId') || Object.hasOwn(fallbackScope, 'workspace_id');
    const workspace = canonicalScopePart(entryWorkspace || (hasFallbackWorkspace ? (fallbackScope.workspace || fallbackScope.workspaceId || fallbackScope.workspace_id) : ''));
    const account = canonicalScopePart(entry.account_id || entry.accountId || entry.owner_id || entry.owner || (workspace ? (fallbackScope.account || fallbackScope.owner || fallbackScope.accountId) : ''));
    const scope = [account, workspace].filter(Boolean).join('|');
    return scope ? `${scope}|${explicit}` : explicit;
  }
  const provider = text(entry.provider || fallbackProvider);
  const id = text(entry.resource_id || entry.resourceId || entry.id);
  if (provider && id) {
    const entryWorkspace = entry.workspace_id || entry.workspaceId || entry.workspace;
    const hasFallbackWorkspace = Object.hasOwn(fallbackScope, 'workspace') || Object.hasOwn(fallbackScope, 'workspaceId') || Object.hasOwn(fallbackScope, 'workspace_id');
    const workspace = canonicalScopePart(entryWorkspace || (hasFallbackWorkspace ? (fallbackScope.workspace || fallbackScope.workspaceId || fallbackScope.workspace_id) : ''));
    const account = canonicalScopePart(entry.account_id || entry.accountId || entry.owner_id || entry.owner || (workspace ? (fallbackScope.account || fallbackScope.owner || fallbackScope.accountId) : ''));
    const scope = [account, workspace].filter(Boolean).join('|');
    return scope ? `${scope}|${provider}:${id}` : `${provider}:${id}`;
  }
  // Raw host entries predate ResourceRef. Their canonical path is a bounded
  // compatibility identity, never an authority token or drag export.
  const path = text(entry.path);
  return provider && path ? `${provider}:path:${path}` : text(entry.key || entry.name);
}

export function scopeKey(scope = {}) {
  const owner = text(scope.owner || scope.account || scope.accountId);
  const workspace = text(scope.workspace || scope.workspaceId || scope.workspace_id || 'default');
  const provider = text(scope.provider);
  const parent = text(scope.parent || scope.parentRef || scope.folder || scope.folderRef);
  const query = text(scope.query);
  const column = text(scope.column || scope.columnId);
  return [owner, workspace, provider, parent, query, column].join('|');
}

function sameScope(a, b) { return scopeKey(a) === scopeKey(b); }

function modifierState(modifiers = {}) {
  return {
    additive: !!(modifiers.additive || modifiers.metaKey || modifiers.ctrlKey),
    range: !!(modifiers.range || modifiers.shiftKey),
  };
}

/**
 * One ordered selection set.  `items` may be replaced or appended repeatedly;
 * selected identities survive as long as their scope remains the same.
 */
export function createFilesSelectionModel({ scope = {}, maxItems = MAX_ITEMS, onChange = null } = {}) {
  let currentScope = { ...scope };
  let currentScopeKey = scopeKey(currentScope);
  let ordered = [];
  let entries = new Map();
  let selected = new Set();
  let anchorKey = '';
  let focusKey = '';
  let epoch = 0;
  const limit = Math.max(1, Math.min(MAX_ITEMS, Number(maxItems) || MAX_ITEMS));

  const notify = (reason) => {
    try { onChange?.(snapshot(reason)); } catch (_) { /* UI observers are optional. */ }
  };
  const validKey = (key) => !!text(key) && entries.has(key);
  const snapshot = (reason = '') => ({
    scope: { ...currentScope }, scopeKey: currentScopeKey, epoch,
    orderedKeys: [...ordered], selectedKeys: [...selected],
    anchorKey: anchorKey || null, focusKey: focusKey || null, reason,
  });
  const range = (from, to) => {
    const a = ordered.indexOf(from); const b = ordered.indexOf(to);
    if (a < 0 || b < 0) return [];
    return ordered.slice(Math.min(a, b), Math.max(a, b) + 1);
  };
  const equal = (a, b) => a.length === b.length && a.every((value, index) => value === b[index]);

  function setScope(nextScope = {}, { preserve = false } = {}) {
    const nextKey = scopeKey(nextScope);
    if (nextKey === currentScopeKey) { currentScope = { ...nextScope }; return snapshot('scope-same'); }
    currentScope = { ...nextScope }; currentScopeKey = nextKey; epoch += 1;
    ordered = []; entries = new Map(); anchorKey = ''; focusKey = '';
    // A scope includes the authenticated owner, provider, parent, query and
    // active column.  Stable resource keys are only meaningful inside that
    // complete scope, so never carry selection across a changed scope.  Keep
    // the option for callers that share the model API, but intentionally do
    // not allow it to weaken this boundary.
    void preserve;
    selected.clear();
    notify('scope');
    return snapshot('scope');
  }

  function setItems(items = [], { append = false, scope: nextScope = null } = {}) {
    if (nextScope) setScope(nextScope);
    const previousOrdered = [...ordered];
    const previousSelected = [...selected]; const previousAnchor = anchorKey; const previousFocus = focusKey;
    const incoming = Array.isArray(items) ? items.slice(0, limit) : [];
    if (!append) {
      const prior = selected;
      ordered = []; entries = new Map();
      for (const item of incoming) {
        const key = resourceKey(item, currentScope.provider, currentScope);
        if (!key || entries.has(key)) continue;
        entries.set(key, item); ordered.push(key);
      }
      selected = new Set([...prior].filter(validKey));
    } else {
      for (const item of incoming) {
        const key = resourceKey(item, currentScope.provider, currentScope);
        if (!key) continue;
        if (!entries.has(key)) ordered.push(key);
        entries.set(key, item);
        if (ordered.length >= limit) break;
      }
      selected = new Set([...selected].filter(validKey));
    }
    if (!validKey(anchorKey)) anchorKey = '';
    if (!validKey(focusKey)) focusKey = '';
    if (!equal(previousOrdered, ordered) || !equal(previousSelected, [...selected]) || previousAnchor !== anchorKey || previousFocus !== focusKey) epoch += 1;
    notify('items');
    return snapshot('items');
  }

  function select(keyOrEntry, modifiers = {}) {
    const key = typeof keyOrEntry === 'object' ? resourceKey(keyOrEntry, currentScope.provider, currentScope) : text(keyOrEntry);
    if (!validKey(key)) return false;
    const { additive, range: ranged } = modifierState(modifiers);
    if (ranged && anchorKey && validKey(anchorKey)) {
      if (!additive) selected.clear();
      for (const itemKey of range(anchorKey, key)) selected.add(itemKey);
    } else if (additive) {
      if (selected.has(key)) selected.delete(key); else selected.add(key);
      anchorKey = key;
    } else {
      selected.clear(); selected.add(key); anchorKey = key;
    }
    focusKey = key; epoch += 1; notify('select');
    return true;
  }

  function focus(keyOrEntry, modifiers = {}) {
    const key = typeof keyOrEntry === 'object' ? resourceKey(keyOrEntry, currentScope.provider, currentScope) : text(keyOrEntry);
    if (!validKey(key)) return false;
    const { range: ranged } = modifierState(modifiers);
    const priorSelected = [...selected]; const priorAnchor = anchorKey; const priorFocus = focusKey;
    if (ranged) {
      if (!anchorKey || !validKey(anchorKey)) anchorKey = focusKey || key;
      selected.clear(); for (const itemKey of range(anchorKey, key)) selected.add(itemKey);
    }
    focusKey = key;
    if (!equal(priorSelected, [...selected]) || priorAnchor !== anchorKey || priorFocus !== focusKey) epoch += 1;
    notify('focus');
    return true;
  }

  function moveFocus(delta, modifiers = {}) {
    if (!ordered.length) return false;
    const amount = Number.isFinite(Number(delta)) ? Number(delta) : 0;
    const current = ordered.indexOf(focusKey);
    // ArrowDown from an unfocused list starts at the first item; ArrowUp
    // starts at the last item. This avoids silently skipping item two.
    const index = current >= 0 ? current : (amount < 0 ? ordered.length : -1);
    const target = ordered[Math.max(0, Math.min(ordered.length - 1, index + amount))];
    return focus(target, modifiers);
  }

  function selectAll() {
    const next = new Set(ordered); const changed = !equal([...selected], [...next]) || anchorKey !== (ordered[0] || '') || focusKey !== (ordered.at(-1) || '');
    selected = next; if (ordered.length) { anchorKey = ordered[0]; focusKey = ordered.at(-1); }
    if (changed) epoch += 1; notify('select-all'); return snapshot('select-all');
  }
  function clear() { const changed = selected.size > 0 || !!anchorKey || !!focusKey; selected.clear(); anchorKey = ''; focusKey = ''; if (changed) epoch += 1; notify('clear'); return snapshot('clear'); }
  function replaceSelection(keys = [], { focus = null, anchor = null } = {}) {
    const values = typeof keys === 'string' ? [keys] : keys;
    const incoming = values == null ? [] : [...(typeof values[Symbol.iterator] === 'function' ? values : [])].map(text).filter(validKey);
    const priorSelected = [...selected];
    selected = new Set(incoming);
    const priorAnchor = anchorKey; const priorFocus = focusKey;
    if (anchor != null && validKey(text(anchor))) anchorKey = text(anchor);
    if (focus != null && validKey(text(focus))) focusKey = text(focus);
    if (!equal(priorSelected, [...selected]) || priorAnchor !== anchorKey || priorFocus !== focusKey) epoch += 1;
    notify('replace'); return snapshot('replace');
  }

  return {
    setScope, setItems, select, focus, moveFocus, selectAll, clear, replaceSelection,
    has: (key) => selected.has(text(key)), item: (key) => entries.get(text(key)) || null,
    selectedEntries: () => [...selected].map((key) => entries.get(key)).filter(Boolean),
    selectedKeys: () => [...selected], orderedKeys: () => [...ordered],
    get anchorKey() { return anchorKey; }, get focusKey() { return focusKey; },
    get epoch() { return epoch; }, get scope() { return { ...currentScope }; },
    snapshot, range,
  };
}

/** Selection remains local to a Files column. */
export const createSelectionModel = createFilesSelectionModel;

export function rectangleSelection(boxes = [], rectangle = {}, { additive = false, toggle = false } = {}) {
  const left = Math.min(Number(rectangle.left) || 0, Number(rectangle.right) || 0);
  const right = Math.max(Number(rectangle.left) || 0, Number(rectangle.right) || 0);
  const top = Math.min(Number(rectangle.top) || 0, Number(rectangle.bottom) || 0);
  const bottom = Math.max(Number(rectangle.top) || 0, Number(rectangle.bottom) || 0);
  const hit = (box) => Number(box.right) >= left && Number(box.left) <= right && Number(box.bottom) >= top && Number(box.top) <= bottom;
  const keys = boxes.filter((box) => hit(box)).map((box) => text(box.key)).filter(Boolean);
  return { keys: [...new Set(keys)], additive: !!additive, toggle: !!toggle };
}

export function transferIntent({ platform = '', metaKey = false, ctrlKey = false, shiftKey = false, altKey = false } = {}) {
  const mac = /mac/i.test(platform || (globalThis.navigator?.platform || ''));
  // Cmd is selection on macOS. Option is copy there; Ctrl is copy elsewhere.
  const copy = mac ? !!altKey : !!ctrlKey;
  const move = !!shiftKey;
  if (copy && move) return { intent: 'reject', reason: 'Choose either Copy or Move, not both.' };
  return { intent: copy ? 'copy' : 'move', modifier: copy ? (mac ? 'option' : 'ctrl') : move ? 'shift' : 'default', selectionModifier: mac && !!metaKey };
}

export function buildInternalDragPayload({ scope = {}, generation = 0, policyGeneration = generation, selectionEpoch = 0, owner = '', pane = '', kind = 'move', entries = [], selectedKeys = [] } = {}) {
  const safeInteger = (value, fallback = 0) => {
    if (value == null || value === '') return fallback;
    return Number.isSafeInteger(Number(value)) && Number(value) >= 0 && Number(value) <= MAX_EPOCH ? Number(value) : null;
  };
  // Authority fields are rejected when they exceed their byte budget. A JS
  // code-unit slice can split a surrogate pair and silently change the token.
  const bounded = (value, max = MAX_FIELD_LENGTH) => {
    const result = text(value);
    return utf8Length(result) <= max ? result : null;
  };
  const safeRevision = (revision) => {
    if (!revision || typeof revision !== 'object') return null;
    const revisionKind = bounded(revision.kind, 64);
    const revisionValue = bounded(revision.value, 512);
    return revisionKind && revisionValue ? { kind: revisionKind, value: revisionValue } : null;
  };
  const selectedValues = selectedKeys.map(text).filter(Boolean);
  // HTML5 drag data is deliberately bounded. Callers that own a larger
  // selection must use the batch executor; silently serializing the first 200
  // resources would make the visible command target a different set.
  if (selectedValues.length > MAX_DRAG_ITEMS) return null;
  const selected = new Set(selectedValues);
  const seenKeys = new Set();
  const seenRefs = new Set();
  const seenIds = new Set();
  const sources = entries.filter((entry) => selected.has(resourceKey(entry, scope.provider, scope))).map((entry, index) => {
    const key = bounded(resourceKey(entry, scope.provider, scope));
    const ref = bounded(entry.resource_ref || entry.ref);
    if (!key || !ref || seenKeys.has(key) || seenRefs.has(ref)) return null;
    seenKeys.add(key); seenRefs.add(ref);
    const suppliedItemId = entry.item_id ?? entry.itemId;
    let itemId = bounded(suppliedItemId || `${index}-${key}`, 128);
    // Never replace an explicit authority id with a generated value: callers
    // must see the invalid payload and obtain a fresh bounded id.
    if (suppliedItemId != null && (!itemId || seenIds.has(itemId))) return null;
    if (!itemId || seenIds.has(itemId)) itemId = bounded(`${index}-${key}`, 128);
    if (!itemId || seenIds.has(itemId)) return null;
    seenIds.add(itemId);
    return {
      item_id: itemId,
      resource_key: key,
      resource_ref: ref,
      revision: safeRevision(entry.revision || entry.expected_revision),
    };
  }).filter(Boolean);
  if (sources.length !== selected.size || sources.length > MAX_DRAG_ITEMS) return null;
  const payload = {
    version: 1, type: 'openclank/files-transfer', kind: kind === 'copy' ? 'copy' : 'move',
    owner: bounded(owner || scope.owner || scope.account),
    workspace: bounded(scope.workspace || scope.workspaceId || scope.workspace_id || 'default', 128),
    pane: bounded(pane || scope.pane || 'files-window', 256),
    column: bounded(scope.column || scope.columnId || '', 128),
    provider: bounded(scope.provider, 128), parent_ref: bounded(scope.parentRef || scope.parent),
    query: bounded(scope.query), generation: safeInteger(generation), policy_generation: safeInteger(policyGeneration, safeInteger(generation)), selection_epoch: safeInteger(selectionEpoch),
    sources,
  };
  return parseInternalDragPayload(payload);
}

export function parseInternalDragPayload(value) {
  let payload = value;
  if (typeof value === 'string') {
    const bytes = new TextEncoder().encode(value).byteLength;
    if (bytes > MAX_DRAG_SERIALIZED_BYTES) return null;
    try { payload = JSON.parse(value); } catch (_) { return null; }
  }
  if (!payload || typeof payload !== 'object' || payload.version !== 1 || payload.type !== 'openclank/files-transfer' || !['move', 'copy'].includes(payload.kind)) return null;
  const safeString = (field, max = MAX_FIELD_LENGTH, required = false) => {
    if (typeof field !== 'string' || utf8Length(field) > max || field.includes('\0') || (required && !field)) return required ? null : '';
    return field;
  };
  const owner = safeString(payload.owner, MAX_FIELD_LENGTH, true);
  const workspace = safeString(payload.workspace, 128, true);
  const pane = safeString(payload.pane, 256, true);
  const column = safeString(payload.column, 128, false);
  // These fields bind the authority envelope to one Files owner/pane/provider.
  // An empty provider is ambiguous (and previously allowed a host fallback),
  // so reject it before any destination comparison or command dispatch.
  const provider = safeString(payload.provider, 128, true);
  const parentRef = safeString(payload.parent_ref, MAX_FIELD_LENGTH, true);
  const query = safeString(payload.query, MAX_FIELD_LENGTH, false);
  const generation = payload.generation;
  const policyGeneration = payload.policy_generation;
  const selectionEpoch = payload.selection_epoch;
  if (owner == null || workspace == null || pane == null || column == null || !Object.hasOwn(payload, 'column') || provider == null || parentRef == null || query == null
    || !Number.isSafeInteger(generation) || generation < 0 || generation > MAX_EPOCH
    || !Number.isSafeInteger(policyGeneration) || policyGeneration < 0 || policyGeneration > MAX_EPOCH
    || !Number.isSafeInteger(selectionEpoch) || selectionEpoch < 0 || selectionEpoch > MAX_EPOCH) return null;
  if (!Array.isArray(payload.sources) || payload.sources.length < 1 || payload.sources.length > MAX_DRAG_ITEMS) return null;
  const keys = new Set(); const refs = new Set(); const ids = new Set();
  const sources = [];
  for (const source of payload.sources) {
    if (!source || typeof source !== 'object') return null;
    const itemId = safeString(source.item_id, 128, true);
    const key = safeString(source.resource_key, MAX_FIELD_LENGTH, true);
    const ref = safeString(source.resource_ref, MAX_FIELD_LENGTH, true);
    if (itemId == null || key == null || ref == null || keys.has(key) || refs.has(ref) || ids.has(itemId)) return null;
    let revision = null;
    if (source.revision != null) {
      if (!source.revision || typeof source.revision !== 'object' || Array.isArray(source.revision)) return null;
      const revisionKind = safeString(source.revision.kind, 64, true);
      const revisionValue = safeString(source.revision.value, 512, true);
      if (revisionKind == null || revisionValue == null || Object.keys(source.revision).some((keyName) => !['kind', 'value'].includes(keyName))) return null;
      revision = Object.freeze({ kind: revisionKind, value: revisionValue });
    }
    if (Object.keys(source).some((keyName) => !['item_id', 'resource_key', 'resource_ref', 'revision'].includes(keyName))) return null;
    keys.add(key); refs.add(ref); ids.add(itemId);
    sources.push(Object.freeze({ item_id: itemId, resource_key: key, resource_ref: ref, revision }));
  }
  const canonical = { version: 1, type: 'openclank/files-transfer', kind: payload.kind, owner, workspace, pane, column, provider, parent_ref: parentRef, query, generation, policy_generation: policyGeneration, selection_epoch: selectionEpoch, sources: Object.freeze(sources) };
  if (new TextEncoder().encode(JSON.stringify(canonical)).byteLength > MAX_DRAG_SERIALIZED_BYTES) return null;
  return Object.freeze(canonical);
}

export function validateDropTarget(payload, target = {}, { generation = null, policyGeneration = null, selectionEpoch = null, provider = '', owner = '', workspace = '', pane = '', allowCopy = false, allowMove = false } = {}) {
  const drag = parseInternalDragPayload(payload);
  if (!drag) return { ok: false, reason: 'This drag data is not a valid Files operation.' };
  if (generation != null && Number(drag.generation) !== Number(generation)) return { ok: false, reason: 'The Files view changed; select the items again.' };
  if (policyGeneration != null && Number(drag.policy_generation) !== Number(policyGeneration)) return { ok: false, reason: 'File access changed; select the items again.' };
  if (selectionEpoch != null && Number(drag.selection_epoch) !== Number(selectionEpoch)) return { ok: false, reason: 'The Files selection changed; select the items again.' };
  if (owner && drag.owner !== String(owner)) return { ok: false, reason: 'This drag belongs to another account.' };
  if (workspace && drag.workspace !== String(workspace)) return { ok: false, reason: 'This drag belongs to another workspace.' };
  if (pane && drag.pane !== String(pane)) return { ok: false, reason: 'This drag belongs to another Files pane.' };
  if (!drag.provider || !provider || drag.provider !== provider) return { ok: false, reason: 'Transfers between providers are unavailable.' };
  if (drag.kind === 'move' && drag.parent_ref && text(target.resource_ref) === drag.parent_ref) return { ok: false, reason: 'An item cannot be moved into its current folder.' };
  if (target.resource_key && drag.sources.some((source) => source.resource_key === text(target.resource_key))) return { ok: false, reason: 'A folder cannot receive itself.' };
  if (drag.kind === 'move' && !allowMove) return { ok: false, reason: text(target.reason) || 'This folder cannot receive moved items.' };
  if (drag.kind === 'copy' && !allowCopy) return { ok: false, reason: text(target.reason) || 'This folder cannot receive copied items.' };
  if (target.isDescendant) return { ok: false, reason: 'A folder cannot be moved into itself or a descendant.' };
  if (target.collision && target.collision !== 'fail' && target.collision !== 'rename') return { ok: false, reason: 'The collision choice is unsupported.' };
  return { ok: true, intent: drag.kind, count: drag.sources.length };
}

export const FILES_TRANSFER_MIME = 'application/x-openclank-files+json';
export const FILES_MAX_DRAG_ITEMS = MAX_DRAG_ITEMS;
