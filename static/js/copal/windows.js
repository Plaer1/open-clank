import * as Modals from '../modalManager.js';
import { makeWindowDraggable } from '../windowDrag.js';
import {
  activateInputContext,
  disposeInputContextTree,
  getActiveInputContext,
  registerInputContext,
  setInputContextLifecycle,
  setInputContextNavigation,
  unregisterInputContext,
} from './inputContext.js';

export function createOpenClankWindow({
  id,
  label,
  subtitle = 'Copal · Redb',
  minWidth = 560,
  minHeight = 420,
  sizeKey = `odysseus-${id}-size`,
  className = '',
  onActivate = null,
  onBeforeClose = null,
  onClosed = null,
  navigation = null,
  accountScope = null,
  workspaceScope = null,
}) {
  const existing = document.getElementById(id);
  const existingWindow = existing?.__openClankWindow || existing?.__copalWindow;
  if (existingWindow) return existingWindow;

  const root = document.createElement('div');
  root.id = id;
  root.className = `modal copal-tool-modal hidden ${className}`.trim();
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'false');
  root.setAttribute('aria-label', label);

  const content = document.createElement('section');
  content.className = 'modal-content copal-modal-content copal-workspace';
  content.setAttribute('aria-label', label);
  const header = document.createElement('header');
  header.className = 'modal-header copal-workspace-header';
  const heading = document.createElement('div');
  heading.className = 'copal-workspace-title';
  const title = document.createTextNode(label);
  const small = document.createElement('small');
  small.textContent = subtitle;
  heading.append(title, small);
  const actions = document.createElement('div');
  actions.className = 'copal-window-actions';
  const backButton = document.createElement('button');
  backButton.type = 'button';
  backButton.className = 'copal-window-nav copal-window-nav-back';
  backButton.textContent = '‹';
  backButton.title = 'Back';
  backButton.setAttribute('aria-label', `Back in ${label}`);
  const forwardButton = document.createElement('button');
  forwardButton.type = 'button';
  forwardButton.className = 'copal-window-nav copal-window-nav-forward';
  forwardButton.textContent = '›';
  forwardButton.title = 'Forward';
  forwardButton.setAttribute('aria-label', `Forward in ${label}`);
  const status = document.createElement('span');
  status.className = 'copal-workspace-status';
  status.setAttribute('role', 'status');
  const closeButton = document.createElement('button');
  closeButton.className = 'close-btn';
  closeButton.type = 'button';
  closeButton.textContent = '×';
  closeButton.title = `Close ${label}`;
  closeButton.setAttribute('aria-label', `Close ${label}`);
  const body = document.createElement('main');
  body.className = 'copal-view';
  body.tabIndex = -1;
  actions.append(backButton, forwardButton);
  header.append(heading, actions, status, closeButton);
  content.append(header, body);
  root.append(content);
  document.body.append(root);

  const inputContext = {
    windowId: id,
    paneId: `${id}:body`,
    blockingModal: false,
    editable: false,
    composing: false,
    capabilities: { keyboard: true, pointer: true, wheel: true },
    selectionAdapter: null,
    navigation,
    accountScope,
    workspaceScope,
  };
  const unregisterInput = registerInputContext(root, inputContext);

  let returnFocus = null;
  let visible = false;
  let closePromise = null;
  let focusGeneration = 0;

  const finishClose = (fromManager = false) => {
    focusGeneration += 1;
    visible = false;
    setInputContextLifecycle(root, { visible: false, minimized: false, eligible: false });
    unregisterInput();
    root.classList.add('hidden');
    root.classList.remove('modal-minimized', 'copal-window-close-pending');
    root.style.display = 'none';
    if (!fromManager) Modals.unregister(id);
    onClosed?.(windowApi);
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    return true;
  };

  const windowApi = {
    id,
    root,
    content,
    header,
    heading,
    titleNode: title,
    actions,
    body,
    inputContext,
    status,
    get visible() { return visible && !root.classList.contains('hidden'); },
    setTitle(value) { title.data = value; root.setAttribute('aria-label', value); content.setAttribute('aria-label', value); },
    setSubtitle(value) { small.textContent = value || ''; },
    setStatus(message, bad = false) {
      status.textContent = message || '';
      status.classList.toggle('error', !!bad);
    },
    get navigation() { return inputContext.navigation; },
    setNavigationAdapter(adapter) {
      setInputContextNavigation(root, adapter);
      inputContext.navigation = adapter;
      updateNavigationButtons();
      return adapter;
    },
    registerPane(element, pane = {}) {
      return registerInputContext(element, {
        ...inputContext,
        ...pane,
        windowId: id,
        paneId: pane.paneId || `${id}:pane`,
        navigation: inputContext.navigation,
      });
    },
    updateNavigationButtons,
    focus() { focusGeneration += 1; body.focus({ preventScroll: true }); },
    show(trigger = document.activeElement) {
      const showFocusGeneration = ++focusGeneration;
      const focusAtShow = document.activeElement;
      returnFocus = trigger instanceof HTMLElement ? trigger : returnFocus;
      visible = true;
      inputContext.visible = true;
      inputContext.minimized = false;
      inputContext.eligible = true;
      root.classList.remove('hidden', 'modal-minimized');
      root.style.display = 'flex';
      registerInputContext(root, inputContext);
      activateInputContext(inputContext);
      if (Modals.isMinimized(id)) Modals.restore(id);
      Modals.register(id, {
        closeFn: () => windowApi.requestCloseAsync(true),
        restoreFn: () => windowApi.focus(),
        label,
      });
      Modals.injectMinimizeButton(root, id);
      onActivate?.(windowApi);
      requestAnimationFrame(() => {
        if (showFocusGeneration !== focusGeneration || !windowApi.visible) return;
        const activeContext = getActiveInputContext();
        if (activeContext?.windowId && activeContext.windowId !== id) return;
        const active = document.activeElement;
        if (active && active !== document.body && active !== document.documentElement) {
          if (root.contains(active) || active !== focusAtShow) return;
        }
        windowApi.focus();
      });
      return windowApi;
    },
    _beginClose(fromManager = false) {
      if (closePromise) return closePromise;
      const decision = onBeforeClose?.(windowApi);
      if (decision && typeof decision.then === 'function') {
        root.classList.add('copal-window-close-pending');
        closePromise = Promise.resolve(decision).then((allowed) => {
          if (allowed !== false) finishClose(fromManager);
          return allowed !== false;
        }).catch(() => false).finally(() => {
          closePromise = null;
          root.classList.remove('copal-window-close-pending');
        });
        return closePromise;
      }
      if (decision === false) return false;
      return finishClose(fromManager);
    },
    requestClose(fromManager = false) {
      const result = windowApi._beginClose(fromManager);
      // Preserve the established synchronous close-button contract: an async
      // gate reports "not closed yet" while it resolves in the background.
      return result && typeof result.then === 'function' ? false : result;
    },
    requestCloseAsync(fromManager = false) {
      return Promise.resolve(windowApi._beginClose(fromManager));
    },
    destroy() {
      focusGeneration += 1;
      visible = false;
      setInputContextLifecycle(root, { visible: false, eligible: false });
      disposeInputContextTree(root);
      Modals.unregister(id);
      root.remove();
    },
  };
  root.__openClankWindow = windowApi;
  // Compatibility alias for existing Copal callers and any already-mounted
  // windows created before the primitive received its neutral product name.
  root.__copalWindow = windowApi;
  function updateNavigationButtons() {
    const adapter = inputContext.navigation;
    let canBack = false;
    let canForward = false;
    try { canBack = !!adapter?.canGoBack?.(); } catch (_) {}
    try { canForward = !!adapter?.canGoForward?.(); } catch (_) {}
    backButton.disabled = !canBack;
    forwardButton.disabled = !canForward;
  }
  inputContext.navigationRefresh = updateNavigationButtons;
  const invokeNavigation = (direction) => {
    const adapter = inputContext.navigation;
    if (!adapter?.[direction]) return;
    try {
      const result = adapter[direction]();
      if (result && typeof result.then === 'function') {
        Promise.resolve(result).catch((error) => {
          try { adapter.onError?.(error); } catch (_) {}
        }).finally(updateNavigationButtons);
      } else updateNavigationButtons();
    } catch (error) {
      try { adapter.onError?.(error); } catch (_) {}
      updateNavigationButtons();
    }
  };
  backButton.addEventListener('click', () => invokeNavigation('back'));
  forwardButton.addEventListener('click', () => invokeNavigation('forward'));
  updateNavigationButtons();
  closeButton.addEventListener('click', () => windowApi.requestClose());
  root.addEventListener('pointerdown', () => { activateInputContext(inputContext); onActivate?.(windowApi); }, true);
  root.addEventListener('focusin', () => { activateInputContext(inputContext); onActivate?.(windowApi); }, true);
  makeWindowDraggable(root, {
    content,
    header,
    skipSelector: 'button, input, select, textarea, a, [contenteditable="true"], .cm-editor',
    minWidth,
    minHeight,
    resizeStorageKey: sizeKey,
  });
  return windowApi;
}

// Copal remains a consumer of the generic Open Clank window primitive. Keep
// its established import name stable while other first-party tools use the
// neutral name.
export const createCopalWindow = createOpenClankWindow;
