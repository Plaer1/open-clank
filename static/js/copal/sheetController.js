import {
  activeSheetView,
  clearSelection,
  createSheetState,
  pastePreview,
  selectCell,
  moveSelection,
  normalizeSheetDefinition,
  rowKey,
  setSheetRows,
  isReadOnlyColumn,
} from './sheetModel.js';

/** Compare local numeric revisions and provider-owned opaque heads safely. */
export function sameDefinitionRevision(left, right) {
  if (left == null || right == null) return true;
  const leftText = String(left); const rightText = String(right);
  if (leftText === rightText) return true;
  const leftNumber = Number(left); const rightNumber = Number(right);
  return Number.isFinite(leftNumber) && Number.isFinite(rightNumber) && leftNumber === rightNumber;
}

// Definition transforms are previews followed by a CAS save.  Keeping the
// queue at resource scope closes the gap between those two phases when two
// leaves issue commands together; a per-controller queue would still allow
// sibling leaves to preview against the same old head.
const definitionCommandQueues = new Map();

/**
 * Leaf-owned query and interaction controller. It never writes source text;
 * cell and definition mutations are delegated to S05/S01 callbacks.
 */
export function createSheetController({
  resourceKey = null,
  scope = null,
  definition = {},
  definitionRevision = null,
  viewId = null,
  rows = [],
  counts = {},
  query = null,
  onCellEdit = null,
  onDefinitionCommand = null,
  onChange = null,
  schema = {},
  definitionStore = null,
} = {}) {
  const sharedDefinition = definitionStore && typeof definitionStore === 'object' ? definitionStore : null;
  if (sharedDefinition && !sharedDefinition.value) {
    sharedDefinition.value = normalizeSheetDefinition(definition);
    if (sharedDefinition.revision == null && definitionRevision != null) sharedDefinition.revision = definitionRevision;
  }
  const localEnvelope = sharedDefinition?.authoritativeLocal && (!sharedDefinition.authoritativeLocal.scope || !scope || JSON.stringify(sharedDefinition.authoritativeLocal.scope) === JSON.stringify(scope))
    ? sharedDefinition.authoritativeLocal : null;
  const initialDefinition = localEnvelope?.definition || sharedDefinition?.value || definition;
  const initialRevision = localEnvelope?.revision ?? (sharedDefinition?.value && sharedDefinition.revision != null ? sharedDefinition.revision : definitionRevision);
  let state = createSheetState({ resourceKey, scope, definition:initialDefinition, definitionRevision:initialRevision, viewId, rows, counts });
  let closed = false;
  let generation = 0;
  let request = null;
  let corpusGeneration = 0;
  let queryTimer = null;
  const queryCache = new Map();
  const queryCacheLimit = 20;
  const definitionQueueKey = resourceKey
    ? JSON.stringify({ resourceKey, scope })
    : {};
  const listeners = new Set();
  let definitionPublished = null;
  const emit = () => { for (const listener of [...listeners]) listener(state); onChange?.(state); };
  const set = (next) => { if (!closed) { state = next; emit(); } return state; };
  const sameRevision = (left, right) => left == null && right == null
    || left != null && right != null && sameDefinitionRevision(left, right);
  const preserveSelection = (candidate) => {
    const selected = candidate.selected; const view = activeSheetView(candidate);
    if (!selected || !view) return null;
    const visible = new Set(view.columns.filter((column) => column.visible !== false).map((column) => String(column.property)));
    const rows = new Set(candidate.rows.map((row) => rowKey(row)));
    const anchor = selected.anchor; const focus = selected.focus;
    return anchor && focus && visible.has(String(anchor.columnKey)) && visible.has(String(focus.columnKey))
      && rows.has(anchor.rowKey) && rows.has(focus.rowKey) ? selected : null;
  };
  const publishDefinition = (definition, revision = undefined) => {
    const normalized = normalizeSheetDefinition(definition);
    const nextRevision = revision === undefined ? sharedDefinition?.revision ?? null : revision;
    if (sharedDefinition) {
      if (JSON.stringify(sharedDefinition.value) !== JSON.stringify(normalized) || !sameRevision(sharedDefinition.revision, nextRevision)) {
        if (typeof sharedDefinition.set === 'function') {
          definitionPublished = { definition:normalized, revision:nextRevision };
          sharedDefinition.set(normalized, nextRevision);
        } else {
          sharedDefinition.value = normalized;
          if (revision !== undefined) sharedDefinition.revision = nextRevision;
        }
      }
    }
    return { definition:normalized, revision:nextRevision };
  };
  const unsubscribeDefinition = sharedDefinition?.subscribe?.((nextDefinition, nextRevision = null) => {
    if (closed || !nextDefinition) return;
    // A query response is published through the shared store below. The
    // publishing controller already applies that exact object in its own
    // state; skip its subscription echo so it does not reset selection or
    // launch a recursive refresh while sibling leaves still receive it.
    if (definitionPublished && nextDefinition === definitionPublished.definition && sameRevision(nextRevision, definitionPublished.revision)) { definitionPublished = null; return; }
    const normalized = normalizeSheetDefinition(nextDefinition);
    const viewId = normalized.views.some((view) => view.id === state.viewId) ? state.viewId : normalized.views[0]?.id;
    const view = normalized.views.find((candidate) => candidate.id === viewId);
    const selected = preserveSelection({ ...state, definition:normalized, viewId });
    state = { ...state, definition:normalized, definitionRevision:nextRevision ?? state.definitionRevision, viewId, selected };
    emit(); void refresh('shared-definition');
  }) || (() => {});
  const context = () => ({ resourceKey:state.resourceKey, scope:state.scope, definitionRevision:state.definitionRevision, definition:state.definition, view:activeSheetView(state), viewId:state.viewId, query:{ ...state.query }, generation });

  async function refresh(reason = 'initial') {
    if (closed || typeof query !== 'function') return { outcome:'unavailable' };
    if (/cell-edit|shared-buffer|watch|sse|definition|invalidation/.test(String(reason))) { corpusGeneration += 1; queryCache.clear(); }
    const cacheKey = JSON.stringify({ resourceKey:state.resourceKey, scope:state.scope, corpusGeneration, definitionRevision:state.definitionRevision, definition:state.definition, viewId:state.viewId, query:state.query });
    const cached = queryCache.get(cacheKey);
    if (cached) {
      const cachedState = { ...setSheetRows(state, cached.rows || [], cached.counts || {}, { generation:state.generation, status:'ready' }), definitionRevision:cached.definitionRevision ?? state.definitionRevision };
      set({ ...cachedState, selected:preserveSelection(cachedState) });
      return { outcome:'cached', result:cached };
    }
    if (request?.cacheKey === cacheKey && !request.signal.aborted) return request.promise.then((result) => ({ ...result, outcome:'coalesced', joined:true }));
    request?.abort?.();
    const controller = new AbortController(); request = controller; const requestGeneration = ++generation;
    controller.cacheKey = cacheKey; controller.generation = requestGeneration;
    set({ ...state, status:'loading', error:null, generation:requestGeneration });
    const run = async () => {
      try {
      const result = await query({ ...context(), reason, signal:controller.signal });
      if (closed || controller.signal.aborted || requestGeneration !== generation) return { outcome:'ignored', generation:requestGeneration };
      const next = result && typeof result === 'object' ? result : {};
      if (state.definitionRevision != null && next.definitionRevision != null && !sameDefinitionRevision(next.definitionRevision, state.definitionRevision)) {
        // Opaque provider heads have no sortable order. A query issued for a
        // pinned head cannot establish that any different response is newer;
        // only an explicit shared-store or command publication may advance it.
        set({ ...state, status:'ready', error:null, generation:requestGeneration });
        return { outcome:'ignored', generation:requestGeneration, reason:'definition-revision' };
      }
      queryCache.set(cacheKey, { ...next, counts:{ shown:next.rows?.length || 0, matched:next.matchedCount ?? next.matched ?? next.total ?? 0, limited:next.resultLimited ? next.resultLimit ?? next.limited : next.limited ?? null, total:next.total ?? null, truncated:next.sourceTruncated === true || next.truncated === true } });
      while (queryCache.size > queryCacheLimit) queryCache.delete(queryCache.keys().next().value);
      let nextDefinition = null;
      if (next.definition) {
        nextDefinition = publishDefinition(next.definition, next.definitionRevision);
      } else if (next.definitionRevision != null) {
        // Preview responses may omit the definition while still advancing the
        // source revision. Carry that revision through the same shared store
        // envelope so sibling leaves cannot reject their otherwise valid
        // response as stale.
        nextDefinition = publishDefinition(state.definition, next.definitionRevision);
      }
      const withDefinition = nextDefinition ? { ...state, definition:nextDefinition.definition, viewId:next.viewId || state.viewId, definitionRevision:nextDefinition.revision ?? state.definitionRevision } : { ...state, definitionRevision:next.definitionRevision ?? state.definitionRevision };
      const nextStatus = next.status === 'indexing' ? 'indexing' : 'ready';
      const nextState = setSheetRows(withDefinition, next.rows || [], {
        shown:next.rows?.length || 0, matched:next.matchedCount ?? next.matched ?? next.total ?? 0,
        limited:next.resultLimited ? next.resultLimit ?? next.limited : next.limited ?? null,
        total:next.total ?? null, truncated:next.sourceTruncated === true || next.truncated === true,
      }, { generation:requestGeneration, status:nextStatus });
      set({ ...nextState, selected:preserveSelection(nextState) });
      return { outcome:'applied', generation:requestGeneration, result:next };
      } catch (error) {
      if (closed || controller.signal.aborted || requestGeneration !== generation) return { outcome:'ignored', generation:requestGeneration };
      set({ ...state, status:'error', error });
      return { outcome:'failed', generation:requestGeneration, error };
      } finally { if (request === controller) request = null; }
    };
    request.promise = run();
    return request.promise;
  }
  function invalidateQueries(predicate = null) {
    corpusGeneration += 1;
    if (typeof predicate !== 'function') { queryCache.clear(); return; }
    for (const [key, value] of queryCache) if (predicate(value, key)) queryCache.delete(key);
  }

  function select(rowIndex, columnIndex, options = {}) { return set(selectCell(state, rowIndex, columnIndex, options)); }
  function move(rowDelta, columnDelta, options = {}) { return set(moveSelection(state, rowDelta, columnDelta, options)); }
  function setQuery(patch = {}, { debounce = 0 } = {}) {
    state = { ...state, query:{ ...state.query, ...patch } }; emit();
    clearTimeout(queryTimer);
    if (debounce > 0) {
      queryTimer = setTimeout(() => { queryTimer = null; void refresh('query'); }, debounce);
      return state;
    }
    return refresh('query');
  }
  function setView(viewIdToUse) {
    if (!state.definition.views.some((view) => view.id === viewIdToUse)) return state;
    // Keep a leaf's selection when the target view still exposes the same
    // source property and row. This restores the user's point locally while
    // sibling controllers retain their own independent selection.
    const nextState = { ...state, viewId:viewIdToUse, editing:null };
    state = { ...nextState, selected:preserveSelection(nextState) }; emit(); return refresh('view');
  }
  function setPresentation(patch = {}) { return set({ ...state, ...patch, density:patch.density === 'comfortable' ? 'comfortable' : patch.density === 'compact' ? 'compact' : state.density, wrap:patch.wrap == null ? state.wrap : Boolean(patch.wrap) }); }

  async function editCell(row, column, value) {
    if (closed) return { outcome:'closed' };
    if (!row || !column) return { outcome:'failed', message:'Cell is unavailable' };
    if (isReadOnlyColumn(column, schema)) return { outcome:'readonly' };
    if (typeof onCellEdit !== 'function') return { outcome:'unavailable' };
    const result = await onCellEdit({ row, column, value, ...context() });
    if (result?.outcome === 'applied' || result === true) await refresh('cell-edit');
    return result;
  }

  async function executeCommand(command, payload = {}) {
    if (closed) return { outcome:'closed' };
    if (typeof onDefinitionCommand !== 'function') return { outcome:'unavailable' };
    // A sibling leaf may publish a newer local or authoritative definition
    // while this provider command is in flight. Opaque revisions cannot be
    // ordered, so capture the complete envelope and require it to be the
    // same envelope before accepting this reply.
    const authorityAtStart = sharedDefinition ? {
      value:sharedDefinition.value,
      revision:sharedDefinition.revision,
      authoritativeLocal:sharedDefinition.authoritativeLocal,
    } : null;
    const result = await onDefinitionCommand({ command, payload, ...context() });
    if (closed) return { outcome:'ignored', reason:'closed' };
    if (authorityAtStart && (
      sharedDefinition.value !== authorityAtStart.value
      || !sameRevision(sharedDefinition.revision, authorityAtStart.revision)
      || sharedDefinition.authoritativeLocal !== authorityAtStart.authoritativeLocal
    )) {
      const currentRevision = sharedDefinition.revision;
      const resultRevision = result?.definitionRevision ?? result?.acknowledgedLocalRevision;
      const baseLocalRevision = result?.baseLocalRevision;
      const localProvenance = Number.isSafeInteger(baseLocalRevision)
        && Number.isSafeInteger(currentRevision)
        && baseLocalRevision === currentRevision
        && Number.isSafeInteger(resultRevision)
        && resultRevision >= currentRevision;
      if (!localProvenance) return { outcome:'ignored', reason:'definition-authority-advanced' };
    }
    if (result?.outcome === 'applied' || result === true) {
      if (result.definition) {
        const resultRevision = result.definitionRevision ?? result.revision;
        const published = publishDefinition(result.definition, resultRevision);
        const nextState = { ...state, definition:published.definition, definitionRevision:published.revision ?? state.definitionRevision };
        state = { ...nextState, selected:preserveSelection(nextState) };
        emit();
      }
      await refresh(`definition:${command}`);
    }
    return result;
  }

  function command(command, payload = {}) {
    const previous = definitionCommandQueues.get(definitionQueueKey) || Promise.resolve();
    // Start the first command immediately so callers can establish their
    // in-flight boundary synchronously; only commands behind an existing
    // resource operation need a promise turn.
    const run = definitionCommandQueues.has(definitionQueueKey)
      ? previous.catch(() => {}).then(() => executeCommand(command, payload))
      : executeCommand(command, payload);
    // Keep a settled chain in the registry only until this command completes;
    // rejected commands must not poison the next independent command.
    const tracked = run.then(
      (value) => { if (definitionCommandQueues.get(definitionQueueKey) === tracked) definitionCommandQueues.delete(definitionQueueKey); return undefined; },
      (error) => { if (definitionCommandQueues.get(definitionQueueKey) === tracked) definitionCommandQueues.delete(definitionQueueKey); return undefined; },
    );
    definitionCommandQueues.set(definitionQueueKey, tracked);
    return run;
  }

  function previewPaste(text, options = {}) { return pastePreview(state, text, { schema, ...options }); }
  function clear() { return clearSelection(state, { schema }); }
  function subscribe(listener) { if (typeof listener !== 'function') return () => {}; listeners.add(listener); return () => listeners.delete(listener); }
  function close() { closed = true; generation += 1; clearTimeout(queryTimer); queryTimer = null; request?.abort?.(); request = null; unsubscribeDefinition(); listeners.clear(); }

  return Object.freeze({ getState:() => state, context, refresh, invalidateQueries, select, move, setQuery, setView, setPresentation, editCell, command, previewPaste, clear, subscribe, close });
}
