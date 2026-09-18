// Authority-neutral lazy explorer tree controller shared by Files and Code.
//
// The host owns resource identity, authorization, roots, sorting, actions, and
// activation. This module owns only transient tree presentation and request
// lifecycle. In particular, it never derives identity from a path or persists
// product state.

const DEFAULT_TYPEAHEAD_DELAY = 700;

function defaultIdentity(value) {
  return value?.resource_ref?.id ?? value?.resource_id ?? value?.id;
}

function defaultLabel(value) {
  return String(value?.name ?? value?.label ?? value?.title ?? '');
}

function defaultBranch(value) {
  const kind = String(value?.kind ?? value?.type ?? '').toLowerCase();
  return kind === 'directory' || kind === 'folder' || kind.includes('directory');
}

function cleanIdentity(value) {
  if (value == null) throw new TypeError('Explorer entries require a stable identity');
  const id = String(value);
  if (!id) throw new TypeError('Explorer entries require a non-empty stable identity');
  return id;
}

function normalizePage(value) {
  const page = value && typeof value === 'object' ? value : {};
  const items = page.items ?? page.entries ?? page.children ?? [];
  if (!Array.isArray(items)) throw new TypeError('Explorer loadPage must return an items array');
  return {
    items,
    nextCursor: page.nextCursor ?? page.next_cursor ?? null,
  };
}

function printableTypeaheadKey(event) {
  const key = String(event?.key ?? '');
  return key.length === 1 && !event.altKey && !event.ctrlKey && !event.metaKey
    && !/^[\u0000-\u001f\u007f]$/.test(key);
}

export function createExplorerTree({
  container,
  roots = [],
  getId = defaultIdentity,
  getLabel = defaultLabel,
  isBranch = defaultBranch,
  loadPage,
  loadRootPage = null,
  rootNextCursor = null,
  onActivate = null,
  renderIcon = null,
  actions = null,
  onAction = null,
  onContextMenu = null,
  onDoubleActivate = null,
  onError = null,
  classes = null,
  itemAttributes = null,
  rowAttributes = null,
  ariaLabel = '',
  emptyLabel = 'No items',
  loadingLabel = 'Loading…',
  loadMoreLabel = 'Load more',
  retryLabel = 'Retry',
  currentValue = 'page',
  typeaheadDelay = DEFAULT_TYPEAHEAD_DELAY,
} = {}) {
  if (!container?.ownerDocument?.createElement) {
    throw new TypeError('createExplorerTree requires a DOM container');
  }
  if (typeof loadPage !== 'function') {
    throw new TypeError('createExplorerTree requires an injected loadPage function');
  }

  const document = container.ownerDocument;
  const nodes = new Map();
  const controllers = new Map();
  const itemElements = new Map();
  let rootIds = [];
  let focusedId = null;
  let activeId = null;
  let destroyed = false;
  let requestSequence = 0;
  let rootRequest = null;
  let rootLoading = false;
  let rootError = '';
  let rootFailedRequest = null;
  let rootCursor = rootNextCursor;
  let rootPagesLoaded = typeof loadRootPage === 'function' ? 1 : 0;
  let typeahead = '';
  let typeaheadTimer = null;

  if (!container.hasAttribute?.('role')) container.setAttribute('role', 'tree');
  if (ariaLabel && !container.hasAttribute?.('aria-label')) container.setAttribute('aria-label', ariaLabel);
  container.classList?.add('oc-explorer-tree');

  const emitError = (error, context = {}) => {
    if (error?.name === 'AbortError' || destroyed) return;
    try { onError?.(error, context); } catch (_) {}
  };

  const identity = data => cleanIdentity(getId(data));
  const label = data => String(getLabel(data) ?? '');
  const branch = data => !!isBranch(data);

  const classNames = (key, fallback, node = null, level = 0) => {
    let value = classes?.[key];
    if (typeof value === 'function') {
      try { value = value(node?.data, node ? contextFor(node) : null, level); }
      catch (error) {
        emitError(error, { phase: 'class', id: node?.id, key });
        value = '';
      }
    }
    return [fallback, String(value || '').trim()].filter(Boolean).join(' ');
  };

  const applyAttributes = (target, source, node, level, phase) => {
    if (typeof source !== 'function') return;
    try {
      const values = source(node.data, contextFor(node), level) || {};
      for (const [name, value] of Object.entries(values)) {
        if (value == null || value === false || /^on/i.test(name)) continue;
        target.setAttribute(name, value === true ? '' : String(value));
      }
    } catch (error) {
      emitError(error, { phase, id: node.id });
    }
  };

  const element = (tag, className = '', text = null) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  };

  const appendRendered = (target, rendered) => {
    if (rendered == null || rendered === false) return;
    if (Array.isArray(rendered)) {
      for (const value of rendered) appendRendered(target, value);
    } else if (typeof rendered === 'string') {
      // Icons are trusted application renderers. Keeping string support lets the
      // existing SVG glyph registry remain the one source of icon markup.
      target.innerHTML = rendered;
    } else {
      target.append(rendered);
    }
  };

  const visibleIds = () => {
    const result = [];
    const visit = id => {
      const node = nodes.get(id);
      if (!node) return;
      result.push(id);
      if (node.branch && node.expanded) {
        for (const childId of node.children) visit(childId);
      }
    };
    for (const id of rootIds) visit(id);
    return result;
  };

  const captureViewState = () => ({
    expanded: new Set([...nodes.values()].filter(node => node.branch && node.expanded).map(node => node.id)),
    pagesLoaded: new Map([...nodes.values()].filter(node => node.branch).map(node => [node.id, node.pagesLoaded])),
    focusedId,
    activeId,
  });

  const restoreViewState = saved => {
    if (!saved) return;
    for (const id of saved.expanded) {
      const node = nodes.get(id);
      if (node?.branch) node.expanded = true;
    }
    for (const [id, count] of saved.pagesLoaded) {
      const node = nodes.get(id);
      if (node?.branch) node.pagesLoaded = Math.max(node.pagesLoaded, count);
    }
    focusedId = saved.focusedId && nodes.has(saved.focusedId) ? saved.focusedId : focusedId;
    activeId = saved.activeId && nodes.has(saved.activeId) ? saved.activeId : activeId;
    render();
  };

  const publicNode = node => node ? Object.freeze({
    id: node.id,
    data: node.data,
    parentId: node.parentId,
    children: Object.freeze([...node.children]),
    branch: node.branch,
    expanded: node.expanded,
    loaded: node.loaded,
    loading: node.loading,
    error: node.error,
    nextCursor: node.nextCursor,
    pagesLoaded: node.pagesLoaded,
  }) : null;

  const contextFor = (node, extra = {}) => Object.freeze({
    id: node.id,
    parentId: node.parentId,
    expanded: node.expanded,
    loaded: node.loaded,
    loading: node.loading,
    toggle: () => toggle(node.id),
    expand: () => expand(node.id),
    collapse: () => collapse(node.id),
    refresh: options => refresh(node.id, options),
    ...extra,
  });

  const abortNode = id => {
    const pending = controllers.get(id);
    if (!pending) return false;
    controllers.delete(id);
    pending.controller.abort();
    const node = nodes.get(id);
    if (node) node.loading = false;
    return true;
  };

  const removeNode = id => {
    const node = nodes.get(id);
    if (!node) return;
    abortNode(id);
    for (const childId of [...node.children]) removeNode(childId);
    nodes.delete(id);
    itemElements.delete(id);
    if (focusedId === id) focusedId = null;
    if (activeId === id) activeId = null;
  };

  const detachFromParent = (id, exceptParentId = undefined) => {
    const node = nodes.get(id);
    if (!node || node.parentId === exceptParentId) return;
    if (node.parentId == null) rootIds = rootIds.filter(candidate => candidate !== id);
    else {
      const parent = nodes.get(node.parentId);
      if (parent) parent.children = parent.children.filter(candidate => candidate !== id);
    }
  };

  const upsertNode = (data, parentId) => {
    const id = identity(data);
    let node = nodes.get(id);
    if (!node) {
      node = {
        id,
        data,
        parentId,
        children: [],
        branch: branch(data),
        expanded: false,
        loaded: false,
        loading: false,
        error: '',
        nextCursor: null,
        failedRequest: null,
        pagesLoaded: 0,
      };
      nodes.set(id, node);
      return node;
    }
    detachFromParent(id, parentId);
    node.data = data;
    node.parentId = parentId;
    const nextBranch = branch(data);
    if (node.branch && !nextBranch) {
      abortNode(id);
      for (const childId of [...node.children]) removeNode(childId);
      node.children = [];
      node.expanded = false;
      node.loaded = false;
      node.nextCursor = null;
      node.failedRequest = null;
    }
    node.branch = nextBranch;
    return node;
  };

  const reconcileChildren = (parentId, items, {
    append = false,
    nextCursor = null,
    loaded = true,
    shouldRender = true,
  } = {}) => {
    if (!Array.isArray(items)) throw new TypeError('Explorer reconciliation requires an array');
    const parent = parentId == null ? null : nodes.get(String(parentId));
    if (parentId != null && !parent) return false;
    const previous = parent ? parent.children : rootIds;
    const next = append ? [...previous] : [];
    const seen = new Set(next);
    const pageIds = new Set();
    for (const data of items) {
      const node = upsertNode(data, parent ? parent.id : null);
      if (pageIds.has(node.id)) continue;
      pageIds.add(node.id);
      if (!seen.has(node.id)) {
        seen.add(node.id);
        next.push(node.id);
      }
    }
    if (!append) {
      const retained = new Set(next);
      for (const oldId of previous) {
        const oldNode = nodes.get(oldId);
        if (!retained.has(oldId) && oldNode?.parentId === (parent ? parent.id : null)) removeNode(oldId);
      }
    }
    if (parent) {
      parent.children = next;
      parent.nextCursor = nextCursor;
      parent.loaded = !!loaded;
    } else {
      rootIds = next;
    }
    if (shouldRender) render();
    return true;
  };

  const actionRecords = node => {
    try {
      const value = typeof actions === 'function' ? actions(node.data, contextFor(node)) : actions;
      return Array.isArray(value) ? value.filter(Boolean) : [];
    } catch (error) {
      emitError(error, { phase: 'actions', id: node.id });
      return [];
    }
  };

  const activate = (node, trigger = 'programmatic') => {
    try {
      const result = onActivate?.(node.data, contextFor(node, { trigger }));
      Promise.resolve(result).catch(error => emitError(error, { phase: 'activate', id: node.id }));
    } catch (error) {
      emitError(error, { phase: 'activate', id: node.id });
    }
  };

  const invokeAction = (record, node) => {
    try {
      const callback = record.onInvoke ?? record.onAction;
      const result = typeof callback === 'function'
        ? callback(node.data, contextFor(node))
        : onAction?.(record.id ?? record.action, node.data, contextFor(node));
      Promise.resolve(result).catch(error => emitError(error, {
        phase: 'action', id: node.id, action: record.id ?? record.action,
      }));
    } catch (error) {
      emitError(error, { phase: 'action', id: node.id, action: record.id ?? record.action });
    }
  };

  const setRovingFocus = (id, { focus = true } = {}) => {
    if (destroyed || !id || !itemElements.has(id)) return false;
    focusedId = id;
    for (const [candidateId, item] of itemElements) item.tabIndex = candidateId === id ? 0 : -1;
    if (focus) itemElements.get(id)?.focus?.({ preventScroll: true });
    return true;
  };

  const moveFocus = (id, offset) => {
    const visible = visibleIds();
    const index = visible.indexOf(id);
    if (index < 0) return false;
    const target = visible[index + offset];
    return target ? setRovingFocus(target) : false;
  };

  const clearTypeahead = () => {
    typeahead = '';
    if (typeaheadTimer != null) clearTimeout(typeaheadTimer);
    typeaheadTimer = null;
  };

  const runTypeahead = (event, id) => {
    const key = String(event.key).normalize('NFKD').toLocaleLowerCase();
    const repeated = typeahead.length === 1 && typeahead === key;
    typeahead = repeated ? key : `${typeahead}${key}`;
    if (typeaheadTimer != null) clearTimeout(typeaheadTimer);
    typeaheadTimer = setTimeout(clearTypeahead, Math.max(0, Number(typeaheadDelay) || DEFAULT_TYPEAHEAD_DELAY));
    const visible = visibleIds();
    const start = Math.max(0, visible.indexOf(id) + 1);
    const ordered = [...visible.slice(start), ...visible.slice(0, start)];
    let target = ordered.find(candidate => label(nodes.get(candidate)?.data)
      .normalize('NFKD').toLocaleLowerCase().startsWith(typeahead));
    if (!target && typeahead.length > 1) {
      typeahead = key;
      target = ordered.find(candidate => label(nodes.get(candidate)?.data)
        .normalize('NFKD').toLocaleLowerCase().startsWith(typeahead));
    }
    if (target) setRovingFocus(target);
  };

  const handleKeydown = (event, id) => {
    const node = nodes.get(id);
    if (!node || destroyed) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      moveFocus(id, event.key === 'ArrowDown' ? 1 : -1);
      return;
    }
    if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      const visible = visibleIds();
      setRovingFocus(event.key === 'Home' ? visible[0] : visible.at(-1));
      return;
    }
    if (event.key === 'ArrowRight' && node.branch) {
      event.preventDefault();
      if (!node.expanded) void expand(id);
      else if (node.children[0]) setRovingFocus(node.children[0]);
      return;
    }
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      if (node.branch && node.expanded) collapse(id);
      else if (node.parentId != null) setRovingFocus(node.parentId);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      activate(node, 'keyboard');
      return;
    }
    if (event.key === ' ') {
      event.preventDefault();
      if (node.branch) void toggle(id);
      else activate(node, 'keyboard');
      return;
    }
    if (event.key === 'Escape') {
      clearTypeahead();
      return;
    }
    if (printableTypeaheadKey(event)) {
      event.preventDefault();
      runTypeahead(event, id);
    }
  };

  const renderNode = (node, level) => {
    const item = element('div', classNames('item', 'oc-explorer-tree__item', node, level));
    item.setAttribute('role', 'treeitem');
    item.setAttribute('aria-level', String(level));
    item.setAttribute('data-explorer-id', node.id);
    item.tabIndex = node.id === focusedId ? 0 : -1;
    if (node.branch) item.setAttribute('aria-expanded', node.expanded ? 'true' : 'false');
    if (node.loading) item.setAttribute('aria-busy', 'true');
    if (node.id === activeId) item.setAttribute('aria-current', currentValue);
    applyAttributes(item, itemAttributes, node, level, 'item-attributes');
    item.addEventListener('focus', () => {
      focusedId = node.id;
      for (const [id, candidate] of itemElements) candidate.tabIndex = id === node.id ? 0 : -1;
    });
    item.addEventListener('keydown', event => {
      if (event.target === item || event.target === row) handleKeydown(event, node.id);
    });
    itemElements.set(node.id, item);

    const row = element('div', classNames('row', 'oc-explorer-tree__row', node, level));
    row.setAttribute('data-explorer-row', '');
    applyAttributes(row, rowAttributes, node, level, 'row-attributes');
    if (node.branch) {
      const toggleButton = element('button', classNames('toggle', 'oc-explorer-tree__toggle', node, level));
      toggleButton.type = 'button';
      toggleButton.tabIndex = -1;
      toggleButton.setAttribute('aria-label', `${node.expanded ? 'Collapse' : 'Expand'} ${label(node.data)}`.trim());
      toggleButton.setAttribute('data-explorer-toggle', '');
      toggleButton.textContent = node.expanded ? '▾' : '▸';
      toggleButton.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        setRovingFocus(node.id, { focus: false });
        void toggle(node.id);
      });
      row.append(toggleButton);
    } else {
      const spacer = element('span', classNames('toggleSpacer', 'oc-explorer-tree__toggle-spacer', node, level));
      spacer.setAttribute('aria-hidden', 'true');
      row.append(spacer);
    }

    const icon = element('span', classNames('icon', 'oc-explorer-tree__icon', node, level));
    icon.setAttribute('aria-hidden', 'true');
    try {
      appendRendered(icon, renderIcon?.(node.data, Object.freeze({
        expanded: node.expanded,
        loading: node.loading,
        level,
      })));
    } catch (error) {
      emitError(error, { phase: 'icon', id: node.id });
    }
    const text = element('span', classNames('label', 'oc-explorer-tree__label', node, level), label(node.data));
    row.append(icon, text);

    const records = actionRecords(node);
    if (records.length) {
      const actionContainer = element('span', classNames('actions', 'oc-explorer-tree__actions', node, level));
      for (const record of records) {
        const button = element('button', classNames('action', 'oc-explorer-tree__action', node, level));
        button.type = 'button';
        button.tabIndex = -1;
        const actionLabel = String(record.label ?? record.title ?? record.id ?? 'Action');
        button.setAttribute('aria-label', actionLabel);
        button.title = String(record.title ?? actionLabel);
        if (record.id != null) button.setAttribute('data-explorer-action', String(record.id));
        if (record.disabled) button.disabled = true;
        if (record.icon != null) appendRendered(button, record.icon);
        else button.textContent = actionLabel;
        button.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          if (!button.disabled) invokeAction(record, node);
        });
        actionContainer.append(button);
      }
      row.append(actionContainer);
    }
    row.addEventListener('click', event => {
      if (event.defaultPrevented) return;
      setRovingFocus(node.id);
      activate(node, 'pointer');
    });
    if (onDoubleActivate) {
      row.addEventListener('dblclick', event => {
        if (event.defaultPrevented) return;
        try {
          const result = onDoubleActivate(node.data, contextFor(node, { trigger: 'double-pointer' }));
          Promise.resolve(result).catch(error => emitError(error, { phase: 'double-activate', id: node.id }));
        } catch (error) {
          emitError(error, { phase: 'double-activate', id: node.id });
        }
      });
    }
    if (onContextMenu) {
      row.addEventListener('contextmenu', event => {
        try {
          const result = onContextMenu(event, node.data, contextFor(node), records);
          Promise.resolve(result).catch(error => emitError(error, { phase: 'context-menu', id: node.id }));
        } catch (error) {
          emitError(error, { phase: 'context-menu', id: node.id });
        }
      });
    }
    item.append(row);

    if (node.branch && node.expanded) {
      const group = element('div', classNames('group', 'oc-explorer-tree__group', node, level));
      group.setAttribute('role', 'group');
      for (const childId of node.children) {
        const child = nodes.get(childId);
        if (child) group.append(renderNode(child, level + 1));
      }
      if (node.loading) {
        const status = element('div', classNames('status', 'oc-explorer-tree__status', node, level), loadingLabel);
        status.setAttribute('role', 'status');
        group.append(status);
      } else if (node.error) {
        const error = element('div', classNames('error', 'oc-explorer-tree__status oc-explorer-tree__status--error', node, level));
        error.setAttribute('role', 'alert');
        error.append(element('span', '', node.error));
        const retryButton = element('button', classNames('retry', 'oc-explorer-tree__retry', node, level), retryLabel);
        retryButton.type = 'button';
        retryButton.addEventListener('click', () => { void retry(node.id); });
        error.append(retryButton);
        group.append(error);
      } else {
        if (node.loaded && !node.children.length) {
          group.append(element('div', classNames('status', 'oc-explorer-tree__status', node, level), emptyLabel));
        }
        if (node.nextCursor != null) {
          const more = element('button', classNames('more', 'oc-explorer-tree__more', node, level), loadMoreLabel);
          more.type = 'button';
          more.addEventListener('click', () => { void loadMore(node.id); });
          group.append(more);
        }
      }
      item.append(group);
    }
    return item;
  };

  function render() {
    if (destroyed) return;
    const hadFocus = !!container.contains?.(document.activeElement);
    const visible = visibleIds();
    if (!focusedId || !visible.includes(focusedId)) focusedId = visible[0] ?? null;
    itemElements.clear();
    const fragment = document.createDocumentFragment?.() ?? element('div');
    if (!rootIds.length && !rootLoading && !rootError) {
      const empty = element('div', 'oc-explorer-tree__empty', emptyLabel);
      empty.setAttribute('role', 'status');
      fragment.append(empty);
    } else if (rootIds.length) {
      for (const id of rootIds) {
        const node = nodes.get(id);
        if (node) fragment.append(renderNode(node, 1));
      }
    }
    if (rootLoading) {
      const status = element('div', classNames('status', 'oc-explorer-tree__status', null, 0), loadingLabel);
      status.setAttribute('role', 'status');
      fragment.append(status);
    } else if (rootError) {
      const error = element('div', classNames('error', 'oc-explorer-tree__status oc-explorer-tree__status--error', null, 0));
      error.setAttribute('role', 'alert');
      error.append(element('span', '', rootError));
      const retryButton = element('button', classNames('retry', 'oc-explorer-tree__retry', null, 0), retryLabel);
      retryButton.type = 'button';
      retryButton.addEventListener('click', () => { void retry(null); });
      error.append(retryButton);
      fragment.append(error);
    } else if (rootCursor != null && typeof loadRootPage === 'function') {
      const more = element('button', classNames('more', 'oc-explorer-tree__more', null, 0), loadMoreLabel);
      more.type = 'button';
      more.setAttribute('data-explorer-root-more', '');
      more.addEventListener('click', () => { void loadMore(null); });
      fragment.append(more);
    }
    container.replaceChildren(fragment);
    if (hadFocus && focusedId) itemElements.get(focusedId)?.focus?.({ preventScroll: true });
  }

  async function loadNode(id, { cursor = null, append = false } = {}) {
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch) return false;
    abortNode(node.id);
    const controller = new AbortController();
    const requestId = ++requestSequence;
    controllers.set(node.id, { controller, requestId });
    node.loading = true;
    node.error = '';
    node.failedRequest = null;
    render();
    try {
      const result = await loadPage(node.data, Object.freeze({
        id: node.id,
        cursor,
        append,
        signal: controller.signal,
      }));
      const pending = controllers.get(node.id);
      if (destroyed || controller.signal.aborted || pending?.requestId !== requestId) return false;
      const page = normalizePage(result);
      reconcileChildren(node.id, page.items, {
        append,
        nextCursor: page.nextCursor,
        loaded: true,
        shouldRender: false,
      });
      node.error = '';
      node.failedRequest = null;
      node.pagesLoaded = append ? node.pagesLoaded + 1 : 1;
      return true;
    } catch (error) {
      const pending = controllers.get(node.id);
      if (error?.name === 'AbortError' || controller.signal.aborted || destroyed || pending?.requestId !== requestId) {
        return false;
      }
      node.error = String(error?.message || 'Item unavailable');
      node.failedRequest = { cursor, append, stale: error?.code === 'stale_cursor' };
      emitError(error, { phase: 'load', id: node.id, cursor, append });
      return false;
    } finally {
      const pending = controllers.get(node.id);
      if (pending?.requestId === requestId) {
        controllers.delete(node.id);
        node.loading = false;
        render();
      }
    }
  }

  async function loadRoots({ cursor = null, append = false } = {}) {
    if (destroyed || typeof loadRootPage !== 'function') return false;
    rootRequest?.controller.abort();
    const controller = new AbortController();
    const requestId = ++requestSequence;
    rootRequest = { controller, requestId };
    rootLoading = true;
    rootError = '';
    rootFailedRequest = null;
    render();
    try {
      const result = await loadRootPage(Object.freeze({ cursor, append, signal: controller.signal }));
      if (destroyed || controller.signal.aborted || rootRequest?.requestId !== requestId) return false;
      const page = normalizePage(result);
      reconcileChildren(null, page.items, { append, shouldRender: false });
      rootCursor = page.nextCursor;
      rootPagesLoaded = append ? rootPagesLoaded + 1 : 1;
      return true;
    } catch (error) {
      if (error?.name === 'AbortError' || controller.signal.aborted || destroyed || rootRequest?.requestId !== requestId) {
        return false;
      }
      rootError = String(error?.message || 'Items unavailable');
      rootFailedRequest = { cursor, append, stale: error?.code === 'stale_cursor' };
      emitError(error, { phase: 'load-root', cursor, append });
      return false;
    } finally {
      if (rootRequest?.requestId === requestId) {
        rootRequest = null;
        rootLoading = false;
        render();
      }
    }
  }

  async function expand(id) {
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch) return false;
    node.expanded = true;
    render();
    if (!node.loaded && !node.loading) return loadNode(node.id);
    return true;
  }

  function collapse(id) {
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch) return false;
    abortNode(node.id);
    node.expanded = false;
    render();
    return true;
  }

  async function toggle(id) {
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch) return false;
    return node.expanded ? collapse(node.id) : expand(node.id);
  }

  async function loadMore(id = null) {
    if (id == null) {
      if (destroyed || rootLoading || rootCursor == null) return false;
      const loaded = await loadRoots({ cursor: rootCursor, append: true });
      if (!loaded && rootFailedRequest?.stale) return refreshRoots({ recursive: false });
      return loaded;
    }
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch || node.loading || node.nextCursor == null) return false;
    const loaded = await loadNode(node.id, { cursor: node.nextCursor, append: true });
    if (!loaded && node.failedRequest?.stale) return refresh(node.id, { recursive: false });
    return loaded;
  }

  function retry(id = null) {
    if (id == null) {
      if (destroyed || rootLoading || !rootFailedRequest) return Promise.resolve(false);
      return loadRoots(rootFailedRequest);
    }
    const node = nodes.get(String(id));
    if (destroyed || !node?.branch || node.loading || !node.failedRequest) return Promise.resolve(false);
    return loadNode(node.id, node.failedRequest);
  }

  async function refresh(id = null, { recursive = true } = {}) {
    if (destroyed) return false;
    const targets = id == null
      ? rootIds.map(rootId => nodes.get(rootId)).filter(node => node?.branch)
      : [nodes.get(String(id))].filter(node => node?.branch);
    if (!targets.length) return false;
    let okay = true;
    for (const target of targets) {
      const savedView = captureViewState();
      const pageCount = Math.max(1, target.pagesLoaded || 1);
      if (!(await loadNode(target.id))) okay = false;
      for (let page = 1; page < pageCount && target.nextCursor != null; page += 1) {
        if (!(await loadNode(target.id, { cursor: target.nextCursor, append: true }))) okay = false;
      }
      restoreViewState(savedView);
      if (!recursive || destroyed) continue;
      const queue = target.children.map(childId => nodes.get(childId))
        .filter(child => child?.branch && child.expanded);
      while (queue.length && !destroyed) {
        const child = queue.shift();
        const childSavedView = captureViewState();
        const pageCount = Math.max(1, child.pagesLoaded || 1);
        if (!(await loadNode(child.id))) okay = false;
        for (let page = 1; page < pageCount && child.nextCursor != null; page += 1) {
          if (!(await loadNode(child.id, { cursor: child.nextCursor, append: true }))) okay = false;
        }
        restoreViewState(childSavedView);
        for (const grandchildId of child.children) {
          const grandchild = nodes.get(grandchildId);
          if (grandchild?.branch && grandchild.expanded) queue.push(grandchild);
        }
      }
    }
    return okay;
  }

  async function refreshRoots({ recursive = true } = {}) {
    if (destroyed || typeof loadRootPage !== 'function') return false;
    const savedView = captureViewState();
    const pageCount = Math.max(1, rootPagesLoaded || 1);
    let okay = await loadRoots();
    for (let page = 1; page < pageCount && rootCursor != null && !destroyed; page += 1) {
      if (!(await loadRoots({ cursor: rootCursor, append: true }))) okay = false;
    }
    restoreViewState(savedView);
    if (!recursive || destroyed) return okay;
    const expanded = rootIds.map(id => nodes.get(id)).filter(node => node?.branch && node.expanded);
    for (const node of expanded) {
      if (!(await refresh(node.id, { recursive: true }))) okay = false;
    }
    return okay;
  }

  function setRoots(nextRoots, { preserve = true, nextCursor = null, pagesLoaded = null } = {}) {
    if (destroyed) return false;
    rootRequest?.controller.abort();
    rootRequest = null;
    rootLoading = false;
    if (!preserve) {
      for (const id of [...rootIds]) removeNode(id);
      nodes.clear();
      rootIds = [];
      focusedId = null;
      activeId = null;
    }
    rootCursor = nextCursor;
    if (pagesLoaded != null) rootPagesLoaded = Math.max(0, Number(pagesLoaded) || 0);
    rootError = '';
    rootFailedRequest = null;
    return reconcileChildren(null, nextRoots, { append: false });
  }

  function reconcile(parentId, items, options = {}) {
    if (destroyed) return false;
    return reconcileChildren(parentId == null ? null : String(parentId), items, options);
  }

  function setActive(id) {
    if (destroyed) return false;
    const next = id == null ? null : String(id);
    if (activeId === next) return true;
    const previous = itemElements.get(activeId);
    previous?.removeAttribute('aria-current');
    activeId = next;
    if (activeId != null) itemElements.get(activeId)?.setAttribute('aria-current', currentValue);
    return true;
  }

  function focus(id) {
    return setRovingFocus(id == null ? '' : String(id));
  }

  function getNode(id) {
    return publicNode(nodes.get(String(id)));
  }

  function snapshot() {
    return Object.freeze({
      roots: Object.freeze([...rootIds]),
      activeId,
      focusedId,
      destroyed,
      nodes: Object.freeze([...nodes.values()].map(publicNode)),
      pending: Object.freeze([...controllers.keys()]),
      root: Object.freeze({
        loading: rootLoading,
        error: rootError,
        nextCursor: rootCursor,
        pagesLoaded: rootPagesLoaded,
        pending: !!rootRequest,
      }),
    });
  }

  function destroy() {
    if (destroyed) return;
    destroyed = true;
    clearTypeahead();
    rootRequest?.controller.abort();
    rootRequest = null;
    rootLoading = false;
    for (const id of [...controllers.keys()]) abortNode(id);
    controllers.clear();
    itemElements.clear();
    nodes.clear();
    rootIds = [];
    focusedId = null;
    activeId = null;
    container.replaceChildren();
  }

  setRoots(roots, {
    nextCursor: rootNextCursor,
    pagesLoaded: typeof loadRootPage === 'function' ? 1 : 0,
  });

  return Object.freeze({
    setRoots,
    reconcile,
    expand,
    collapse,
    toggle,
    loadMore,
    retry,
    refresh,
    refreshRoots,
    setActive,
    focus,
    getNode,
    snapshot,
    destroy,
  });
}
