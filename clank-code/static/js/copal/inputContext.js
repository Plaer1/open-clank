/**
 * Small, DOM-aware input ownership registry for Copal windows and panes.
 *
 * A context describes the surface that may consume a command.  The registry
 * deliberately does not install a document-level keyboard handler: callers
 * still attach handlers to their owning surface and use `canHandleInput` (or
 * `dispatchCommand`) before consuming an event.
 */

const contexts = new Map();
const elementEntries = new WeakMap();
const managedModalContexts = new WeakMap();
const detachedChildren = new WeakMap();
// These sets only mirror live registry links so the test reset hook can clear
// WeakMap entries for reused fixture elements. They are emptied on every
// unregister and therefore do not retain a disposed tree.
const registeredElements = new Set();
const detachedOwnerElements = new Set();
let activeWindowId = null;
let activePaneId = null;
let activeEntry = null;
let activeModalId = null;
const modalStack = [];
const mru = [];
let navigationListenersInstalled = false;
let recentAuxiliaryGesture = null;

function isHiddenElement(element) {
  return !element || element.hidden === true || element.classList?.contains('hidden')
    || element.classList?.contains('modal-minimized') || element.style?.display === 'none';
}

function hasSuppressedAncestor(element, self) {
  for (let node = element; node; node = node.parentNode) {
    if (node !== element && isHiddenElement(node)) return true;
    const ancestor = node === element ? null : elementEntries.get(node);
    if (ancestor && ancestor !== self && (ancestor.disposed || ancestor.context.visible === false
      || ancestor.context.minimized || ancestor.context.eligible === false)) return true;
  }
  return false;
}

function isEligible(entry) {
  return isEligibleWithSeen(entry, new Set());
}

function isEligibleWithSeen(entry, seen) {
  if (!entry || entry.disposed || entry.element?.isConnected === false) return false;
  if (seen.has(entry)) return false;
  seen.add(entry);
  const context = entry.context;
  if (context.visible === false || context.minimized || context.eligible === false) return false;
  if (entry.detachedOwner && !isEligibleWithSeen(entry.detachedOwner, seen)) return false;
  if (entry.parentEntry && entry.parentEntry !== entry && !isEligibleWithSeen(entry.parentEntry, seen)) return false;
  return !isHiddenElement(entry.element) && !hasSuppressedAncestor(entry.element, entry);
}

function touch(entry) {
  const index = mru.indexOf(entry);
  if (index >= 0) mru.splice(index, 1);
  mru.unshift(entry);
}

function findParentEntry(element, explicitOwner = null) {
  if (explicitOwner) {
    const owner = entryFor(explicitOwner);
    return wouldCreateOwnershipCycle(element, owner) ? null : owner;
  }
  for (let node = element?.parentNode; node; node = node.parentNode) {
    const parent = elementEntries.get(node);
    if (parent) return parent;
  }
  return null;
}

function linkChild(child, owner) {
  if (!child || !owner || wouldCreateOwnershipCycle(child.element, owner)) return false;
  child.parentEntry?.children?.delete(child);
  removeDetachedLink(child);
  child.parentEntry = owner;
  child.detachedOwner = null;
  owner.children.add(child);
  return true;
}

function removeDetachedLink(entry) {
  const detachedOwner = entry?.detachedOwner;
  if (!detachedOwner) return;
  const detached = detachedChildren.get(detachedOwner.element);
  detached?.delete(entry);
  if (detached?.size === 0) {
    detachedChildren.delete(detachedOwner.element);
    detachedOwnerElements.delete(detachedOwner.element);
  }
  entry.detachedOwner = null;
}

function wouldCreateOwnershipCycle(element, owner) {
  if (!owner || owner.element === element) return !!owner;
  const seen = new Set();
  for (let current = owner; current; ) {
    if (current.element === element || seen.has(current)) return true;
    seen.add(current);
    const explicitOwner = current.context?.ownerElement;
    if (explicitOwner === element) return true;
    current = current.parentEntry || entryFor(explicitOwner);
  }
  return false;
}

function adoptChildren(owner) {
  for (const child of contexts.values()) {
    if (child === owner) continue;
    const parent = findParentEntry(child.element, child.context.ownerElement);
    if (parent !== owner) continue;
    linkChild(child, owner);
  }
  for (const child of [...(detachedChildren.get(owner.element) || [])]) {
    // A pane may have been explicitly unregistered while its owner was
    // closed. Do not resurrect that disposed entry during owner re-open.
    if (contexts.get(child.element) !== child) {
      removeDetachedLink(child);
      continue;
    }
    const parent = findParentEntry(child.element, child.context.ownerElement);
    if (parent === owner) linkChild(child, owner);
  }
}

function propagateNavigation(entry, navigation) {
  propagateNavigationWithSeen(entry, navigation, new Set());
}

function propagateNavigationWithSeen(entry, navigation, seen) {
  if (!entry || seen.has(entry)) return;
  seen.add(entry);
  entry.context = normalizeContext({ ...entry.context, navigation });
  for (const child of entry.children || []) propagateNavigationWithSeen(child, navigation, seen);
}

function entryFor(value) {
  if (!value) return null;
  if (value.element && value.context) return value;
  if (value.entry?.element && value.entry?.context) return value.entry;
  if (value.nodeType || typeof value.contains === 'function') return elementEntries.get(value) || null;
  for (const entry of contexts.values()) {
    if (entry.context === value || (entry.context.windowId === value.windowId && entry.context.paneId === value.paneId)) return entry;
  }
  return null;
}

function normalizeNavigation(navigation) {
  if (navigation == null) return null;
  for (const key of ['back', 'forward', 'canGoBack', 'canGoForward']) {
    if (typeof navigation?.[key] !== 'function') throw new TypeError(`navigation.${key} must be a function`);
  }
  return navigation;
}

function pathFor(event) {
  if (typeof event?.composedPath === 'function') return event.composedPath();
  const path = [];
  for (let node = event?.target; node; node = node.parentNode) path.push(node);
  return path;
}

export function isEditableTarget(target) {
  if (!target || typeof target !== 'object') return false;
  if (target.isContentEditable) return true;
  const name = String(target.nodeName || '').toLowerCase();
  if (name === 'textarea' || name === 'input' || name === 'select' || name === 'option') return true;
  if (target.getAttribute?.('contenteditable') === 'true') return true;
  return !!target.closest?.('[contenteditable="true"], [role="textbox"], .cm-content, .cm-editor');
}

export function isComposingEvent(event) {
  return !!(event?.isComposing || event?.keyCode === 229 || event?.which === 229);
}

function normalizeContext(context) {
  if (!context || context.windowId == null || context.paneId == null) {
    throw new TypeError('Input context requires windowId and paneId');
  }
  return {
    resourceKey: null,
    editable: false,
    composing: false,
    modalId: null,
    blockingModal: false,
    minimized: false,
    visible: true,
    eligible: true,
    accountScope: null,
    workspaceScope: null,
    generation: 0,
    navigation: null,
    navigationRefresh: null,
    ownerElement: null,
    capabilities: {},
    selectionAdapter: null,
    ...context,
    navigation: normalizeNavigation(context.navigation),
  };
}

export function registerInputContext(element, context) {
  if (!element || typeof element.contains !== 'function') throw new TypeError('Input context requires an owning element');
  const previous = elementEntries.get(element);
  if (previous) {
    if (previous.parentEntry) previous.parentEntry.children?.delete(previous);
    previous.context = normalizeContext({ ...previous.context, ...context });
    previous.parentEntry = findParentEntry(element, previous.context.ownerElement);
    previous.parentEntry?.children?.add(previous);
    previous.disposed = false;
  contexts.set(element, previous);
    registeredElements.add(element);
    if (isEligible(previous)) touch(previous);
    if (previous.context.navigation) propagateNavigation(previous, previous.context.navigation);
    return () => unregisterInputContext(element);
  }
  const value = normalizeContext(context);
  const entry = { element, context: value, disposed: false, parentEntry: null, children: new Set(), detachedOwner: null };
  entry.parentEntry = findParentEntry(element, value.ownerElement);
  entry.parentEntry?.children?.add(entry);
  contexts.set(element, entry);
  elementEntries.set(element, entry);
  registeredElements.add(element);
  adoptChildren(entry);
  if (value.navigation) propagateNavigation(entry, value.navigation);
  if (isEligible(entry)) touch(entry);
  installNavigationListeners();
  if (value.modalId && activeModalId === value.modalId) activateInputContext(value);
  return () => unregisterInputContext(element);
}

export function updateInputContext(element, context) {
  const entry = contexts.get(element);
  if (!entry) return registerInputContext(element, context);
  entry.context = normalizeContext({ ...entry.context, ...context });
  if (isEligible(entry)) touch(entry);
  return entry.context;
}

export function setInputContextLifecycle(element, state = {}) {
  const entry = entryFor(element);
  if (!entry) return null;
  entry.context = normalizeContext({ ...entry.context, ...state });
  if (isEligible(entry)) touch(entry);
  return entry.context;
}

export function invalidateInputContext(element, { generation = null } = {}) {
  const entry = entryFor(element);
  if (!entry) return null;
  entry.context = normalizeContext({
    ...entry.context,
    eligible: false,
    generation: generation == null ? Number(entry.context.generation || 0) + 1 : generation,
  });
  return entry.context;
}

export function invalidateInputContexts(scope = {}) {
  let count = 0;
  for (const entry of contexts.values()) {
    const accountMatches = scope.accountScope == null || entry.context.accountScope === scope.accountScope;
    const workspaceMatches = scope.workspaceScope == null || entry.context.workspaceScope === scope.workspaceScope;
    if (accountMatches && workspaceMatches) { invalidateInputContext(entry.element); count += 1; }
  }
  return count;
}

export function setInputContextNavigation(element, navigation) {
  const entry = entryFor(element);
  if (!entry) return null;
  propagateNavigation(entry, navigation);
  return entry.context.navigation;
}

export function getInputContextEntry(element) { return entryFor(element); }

export function unregisterInputContext(element, { preserveChildren = true } = {}) {
  const entry = contexts.get(element) || elementEntries.get(element);
  if (!entry) return false;
  const wasDetached = !!entry.detachedOwner;
  entry.disposed = true;
  entry.parentEntry?.children?.delete(entry);
  removeDetachedLink(entry);
  // Preserve registered panes for a close/reopen cycle, but retain their exact
  // owner element so a later destroy can clean this detached tree without
  // sweeping unrelated contexts from the same window id.
  if (preserveChildren && wasDetached) {
    // An explicitly removed pane cannot be rediscovered by a closed owner;
    // dispose its nested registrations with it instead of stranding them in
    // an unreachable detached set.
    const descendants = [];
    const collect = (candidate) => {
      if (!candidate || descendants.includes(candidate)) return;
      descendants.push(candidate);
      for (const child of candidate.children || []) collect(child);
      for (const child of detachedChildren.get(candidate.element) || []) collect(child);
    };
    for (const child of entry.children || []) collect(child);
    for (const child of descendants.reverse()) unregisterInputContext(child.element, { preserveChildren: false });
    preserveChildren = false;
  }
  if (preserveChildren) {
    const detached = detachedChildren.get(entry.element) || new Set();
    for (const child of entry.children || []) {
      child.parentEntry = null;
      child.detachedOwner = entry;
      detached.add(child);
    }
    detachedChildren.set(entry.element, detached);
    if (detached.size) detachedOwnerElements.add(entry.element);
  } else {
    for (const child of entry.children || []) {
      child.parentEntry = null;
      child.detachedOwner = null;
    }
    detachedChildren.delete(entry.element);
    detachedOwnerElements.delete(entry.element);
  }
  entry.children?.clear();
  contexts.delete(element);
  elementEntries.delete(element);
  registeredElements.delete(element);
  const index = mru.indexOf(entry);
  if (index >= 0) mru.splice(index, 1);
  if (activeEntry === entry) {
    activeEntry = null;
    activeWindowId = null;
    activePaneId = null;
    const replacement = mru.find(isEligible);
    if (replacement) {
      activeEntry = replacement;
      activeWindowId = replacement.context.windowId;
      activePaneId = replacement.context.paneId;
    }
  }
  if (entry.context.blockingModal && entry.context.modalId) clearActiveModal(entry.context.modalId);
  return true;
}

export function disposeInputContextTree(element) {
  const entry = entryFor(element);
  const roots = entry
    ? [...entry.children, ...(detachedChildren.get(entry.element) || [])]
    : [...(detachedChildren.get(element) || [])];
  if (!entry && !roots.length) return false;
  const descendants = [];
  const collect = (candidate) => {
    if (!candidate || descendants.includes(candidate)) return;
    if (contexts.get(candidate.element) !== candidate) {
      removeDetachedLink(candidate);
      return;
    }
    descendants.push(candidate);
    for (const child of candidate.children || []) collect(child);
    for (const child of detachedChildren.get(candidate.element) || []) collect(child);
  };
  roots.forEach(collect);
  for (const child of descendants.reverse()) unregisterInputContext(child.element, { preserveChildren: false });
  if (entry) return unregisterInputContext(entry.element, { preserveChildren: false });
  detachedChildren.delete(element);
  detachedOwnerElements.delete(element);
  return true;
}

export function activateInputContext(contextOrElement) {
  const entry = entryFor(contextOrElement);
  const context = entry?.context || contextOrElement;
  if (!context) return null;
  if (entry && !isEligible(entry)) return null;
  activeEntry = entry || null;
  activeWindowId = context.windowId ?? null;
  activePaneId = context.paneId ?? null;
  if (entry) touch(entry);
  if (context.blockingModal && context.modalId) setActiveModal(context.modalId);
  return context;
}

export function setActiveModal(modalId, context = null) {
  if (!modalId) return clearActiveModal();
  const existing = modalStack.findIndex((entry) => entry.id === modalId);
  if (existing >= 0) modalStack.splice(existing, 1);
  modalStack.push({ id: modalId });
  activeModalId = modalId;
  if (context) {
    activeWindowId = context.windowId ?? null;
    activePaneId = context.paneId ?? null;
  }
  return activeModalId;
}

export function clearActiveModal(modalId = null) {
  if (!modalId) modalStack.length = 0;
  else for (let index = modalStack.length - 1; index >= 0; index -= 1) if (modalStack[index].id === modalId) modalStack.splice(index, 1);
  activeModalId = modalStack.at(-1)?.id || null;
  return activeModalId;
}

export function getActiveInputContext() {
  if (isEligible(activeEntry)) return activeEntry.context;
  const replacement = mru.find(isEligible);
  if (replacement) {
    activeEntry = replacement;
    activeWindowId = replacement.context.windowId;
    activePaneId = replacement.context.paneId;
    return replacement.context;
  }
  return null;
}

export function hasInputContexts() {
  return contexts.size > 0;
}

export function getInputContextStats() {
  return {
    registered: contexts.size,
    eligible: [...contexts.values()].filter(isEligible).length,
    mru: mru.filter(isEligible).length,
    navigationListenersInstalled,
  };
}

export function getLastActiveInputContext() { return getActiveInputContext(); }

export function captureInputContext(value = null) {
  const entry = entryFor(value) || (value ? entryFor(resolveInputContext(value, { allowLastActive: true })) : activeEntry);
  return entry && isEligible(entry) ? { entry, windowId: entry.context.windowId, paneId: entry.context.paneId } : null;
}

function resolveEntryForEvent(event, { allowLastActive = false, owner = null } = {}) {
  const captured = owner?.entry ? owner.entry : entryFor(owner);
  if (captured && isEligible(captured)) return captured;
  const path = pathFor(event);
  const candidates = path.map((node) => elementEntries.get(node)).filter(isEligible);
  if (candidates.length) return candidates[0];
  const body = typeof document !== 'undefined' && (event?.target === document.body || event?.target === document.documentElement);
  if (allowLastActive || body) return isEligible(activeEntry) ? activeEntry : mru.find(isEligible) || null;
  return null;
}

export function resolveInputContext(event, options = {}) {
  const resolvedEntry = resolveEntryForEvent(event, options);
  const candidates = resolvedEntry ? [resolvedEntry] : [];
  if (activeModalId) {
    const modal = candidates.find((entry) => entry.context.blockingModal && entry.context.modalId === activeModalId);
    return modal?.context || null;
  }
  if (!candidates.length) return null;
  const active = candidates.find((entry) => options.allowInactive || entry === activeEntry
    || (entry.context.windowId === activeWindowId && entry.context.paneId === activePaneId));
  if (active) return active.context;
  return null;
}

export function ownsInput(event, context = resolveInputContext(event)) {
  if (!context || event?.defaultPrevented || isComposingEvent(event) || context.composing) return false;
  const active = getActiveInputContext();
  const ownsActiveModal = activeModalId && context.blockingModal && context.modalId === activeModalId;
  if (!ownsActiveModal && active && (active.windowId !== context.windowId || active.paneId !== context.paneId)) return false;
  const resolved = resolveInputContext(event);
  return !!resolved && resolved.windowId === context.windowId && resolved.paneId === context.paneId;
}

export function canHandleInput(event, context = resolveInputContext(event), { allowEditable = false } = {}) {
  if (!ownsInput(event, context)) return false;
  if (!allowEditable && isEditableTarget(event?.target)) return false;
  return true;
}

export function dispatchCommand(event, command, options = {}) {
  if (!canHandleInput(event, options.context, options)) return false;
  const handled = command(resolveInputContext(event), event) === true;
  if (handled) {
    event.preventDefault?.();
    if (options.stopPropagation) event.stopPropagation?.();
  }
  return handled;
}

function navigationOwner(event) {
  const captured = event?.copalOwner || event?.detail?.copalOwner || event?.copalOwnerElement;
  // A fallback is valid only for browser body/document delivery.  If the
  // event path identifies another ordinary registered tool, that tool owns
  // the gesture even when it has no local navigation adapter.
  const entry = resolveEntryForEvent(event, { owner: captured });
  if (!entry || !isEligible(entry) || !entry.context.navigation) return null;
  if (activeModalId && (!entry.context.blockingModal || entry.context.modalId !== activeModalId)) return null;
  return entry;
}

function routeAuxiliaryNavigation(event) {
  const button = Number(event?.button);
  if (button !== 3 && button !== 4) return;
  const now = Number(event.timeStamp) || Date.now();
  const entry = navigationOwner(event);
  const duplicate = recentAuxiliaryGesture && recentAuxiliaryGesture.button === button
    && recentAuxiliaryGesture.entry === entry
    && now - recentAuxiliaryGesture.time < (event.type === 'pointerdown' ? 10 : 500)
    && (event.type !== 'pointerdown' || recentAuxiliaryGesture.type === 'pointerdown');
  if (duplicate) {
    event.preventDefault?.();
    event.stopPropagation?.();
    if (event.type === 'auxclick') recentAuxiliaryGesture = null;
    return;
  }
  if (!entry) return;
  const adapter = entry.context.navigation;
  const direction = button === 3 ? 'back' : 'forward';
  let available = false;
  try { available = adapter[button === 3 ? 'canGoBack' : 'canGoForward']() !== false; } catch (_) {}
  // App-owned end-of-stack gestures are consumed, keeping browser history
  // untouched. Outside an eligible adapter the browser remains in charge.
  event.preventDefault?.();
  event.stopPropagation?.();
  recentAuxiliaryGesture = { button, entry, time: now };
  recentAuxiliaryGesture.type = event.type;
  if (!available) return;
  try {
    const result = adapter[direction]();
    if (result && typeof result.then === 'function') {
      Promise.resolve(result).catch((error) => {
        try { adapter.onError?.(error); } catch (_) {}
      }).finally(() => {
        try { entry.context.navigationRefresh?.(); } catch (_) {}
      });
    } else entry.context.navigationRefresh?.();
  } catch (error) {
    try { adapter.onError?.(error); } catch (_) {}
    try { entry.context.navigationRefresh?.(); } catch (_) {}
  }
}

function installNavigationListeners() {
  if (navigationListenersInstalled || typeof document === 'undefined' || !document.addEventListener) return;
  navigationListenersInstalled = true;
  for (const type of ['pointerdown', 'mousedown', 'auxclick']) {
    document.addEventListener(type, routeAuxiliaryNavigation, true);
  }
}

export function resetInputContextsForTests() {
  contexts.clear();
  for (const element of registeredElements) elementEntries.delete(element);
  registeredElements.clear();
  for (const element of detachedOwnerElements) detachedChildren.delete(element);
  detachedOwnerElements.clear();
  mru.length = 0;
  activeEntry = null;
  activeWindowId = null;
  activePaneId = null;
  activeModalId = null;
  modalStack.length = 0;
  recentAuxiliaryGesture = null;
}

// modalManager announces only true modal surfaces here. Ordinary Copal tool
// windows use aria-modal="false" and remain ordinary active-window contexts.
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
  window.addEventListener('odysseus:modal-opened', (event) => {
    const modal = event.detail?.modal;
    const id = event.detail?.id;
    if (!modal) return;
    const blocking = modal.getAttribute?.('aria-modal') === 'true';
    const existing = entryFor(modal);
    if (existing) {
      managedModalContexts.set(modal, { id, unregister: () => unregisterInputContext(modal) });
      if (blocking) setActiveModal(id);
      else activateInputContext(existing);
      return;
    }
    const context = {
      windowId: blocking ? `modal:${id}` : `tool:${id}`,
      paneId: blocking ? `modal:${id}` : `tool:${id}:body`,
      modalId: blocking ? id : null,
      blockingModal: blocking,
      capabilities: { keyboard: true, pointer: true, wheel: true },
    };
    const unregister = registerInputContext(modal, context);
    managedModalContexts.set(modal, { id, unregister });
    if (blocking) setActiveModal(id);
    else activateInputContext(modal);
  });
  window.addEventListener('odysseus:modal-closed', (event) => {
    const id = event.detail?.id;
    const modal = event.detail?.modal;
    const managed = modal && managedModalContexts.get(modal);
    if (managed) {
      managed.unregister();
      managedModalContexts.delete(modal);
    }
    clearActiveModal(id || null);
  });
}
