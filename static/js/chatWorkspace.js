import { uiIcon } from './uiIcons.js';
// Moves the one live chat root between the workspace and an applet window.
// The conversation tree and its controllers never get cloned or re-mounted.
import modalManager from './modalManager.js';
import { makeWindowDraggable } from './windowDrag.js';

const POPUP_ID = 'chat-window-modal';
const WORKSPACE_ID = 'chat-workspace';
const POPUP_CLASS = 'chat-workspace-window';
const POPOUT_ROOT_CLASS = 'chat-popout';
const RESTORE_ID = 'chat-workspace-restore';
const CHAT_ICON = 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z';

let mode = null;
let returnFocus = null;
let popupReturnFocus = null;
let savedScrollTop = null;
let originalParent = null;
let originalNextSibling = null;
let originalPlaceholder = null;
let popupShell = null;
let popupContent = null;
let closeTrigger = null;

const chatRoot = () => document.getElementById('chat-container');

function isUsableFocusTarget(target, root = chatRoot()) {
  if (!(target instanceof HTMLElement) || !target.isConnected || !root?.contains(target)) return false;
  if (target.closest('[hidden], [aria-hidden="true"], .hidden')) return false;
  const style = getComputedStyle(target);
  return style.display !== 'none' && style.visibility !== 'hidden' && target.getClientRects().length > 0;
}

function preferredChatFocus(target) {
  const root = chatRoot();
  if (isUsableFocusTarget(target, root)) return target;
  if (isUsableFocusTarget(root?._chatLastFocus, root)) return root._chatLastFocus;
  return root?.querySelector('#message');
}

function focusChat(target) {
  const focusTarget = preferredChatFocus(target);
  focusTarget?.focus?.({ preventScroll: true });
}

function observeChatFocus() {
  const root = chatRoot();
  if (!root || root._chatWorkspaceFocusBound) return;
  root._chatWorkspaceFocusBound = true;
  root.addEventListener('focusin', (event) => {
    if (event.target instanceof HTMLElement) root._chatLastFocus = event.target;
  });
}

function ensureRestoreControl() {
  let control = document.getElementById(RESTORE_ID);
  if (control) return control;
  control = document.createElement('button');
  control.id = RESTORE_ID;
  control.type = 'button';
  control.className = 'chat-workspace-restore';
  control.addEventListener('click', () => restoreChat());
  document.body.append(control);
  return control;
}

function updateRestoreControl() {
  const control = ensureRestoreControl();
  control.hidden = mode !== 'closed';
  control.textContent = 'Reopen chat';
  control.setAttribute('aria-label', 'Reopen chat');
}

let menuActions = null;
function ensureMenuActions() {
  const menu = document.getElementById('export-dropdown-menu');
  if (!menu || menu.dataset.chatWorkspaceBound) return;
  menu.dataset.chatWorkspaceBound = '1';

  const add = (id, label, icon, action) => {
    const row = document.createElement('div');
    row.id = id;
    row.className = 'export-dropdown-item chat-workspace-action';
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.innerHTML = `<span class="dropdown-icon">${icon}</span><span>${label}</span>`;
    row.addEventListener('click', (event) => {
      event.stopPropagation();
      const focusBeforeAction = document.activeElement;
      menu.classList.remove('open');
      action(row, focusBeforeAction);
    });
    row.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      row.click();
    });
    menu.append(row);
    return row;
  };

  menuActions = {
    popout: add('chat-popout-btn', 'Pop out chat', '↗', (row, focus) => popOutChat(row, focus)),
    dock: add('chat-dock-btn', 'Dock chat', '↙', (row, focus) => dockChat(row, focus)),
  };
  add('chat-minimize-btn', 'Minimize chat', '−', (row, focus) => minimizeChat(row, focus));
  add('chat-close-btn', 'Close chat', '×', (row, focus) => closeChat(row, focus));
  updateMenuActions();
}

function updateMenuActions() {
  if (!menuActions) return;
  const inPopup = !!(popupShell && popupShell.contains(chatRoot()));
  menuActions.popout.style.display = inPopup ? 'none' : '';
  menuActions.dock.style.display = inPopup ? '' : 'none';
}

function emitWorkspaceChange() {
  const root = chatRoot();
  const poppedOut = !!(popupShell && popupShell.contains(root));
  window.dispatchEvent(new Event('resize'));
  window.dispatchEvent(new CustomEvent('chat-workspace-change', {
    detail: { mode, poppedOut, minimized: mode === 'minimized' },
  }));
  updateMenuActions();
  updateRestoreControl();
}

function setRootUnavailable(root, unavailable) {
  if (!root) return;
  if (unavailable) {
    root.setAttribute('aria-hidden', 'true');
    if ('inert' in root) root.inert = true;
    if (document.activeElement && root.contains(document.activeElement)) document.activeElement.blur();
  } else {
    root.removeAttribute('aria-hidden');
    if ('inert' in root) root.inert = false;
  }
}

function setHidden(nextMode, focusBeforeAction) {
  const root = chatRoot();
  if (!root) return false;
  if (nextMode) {
    if (!mode) {
      returnFocus = preferredChatFocus(focusBeforeAction);
      savedScrollTop = root.querySelector('#chat-history')?.scrollTop ?? null;
    }
    mode = nextMode;
    setRootUnavailable(root, true);
    document.body.classList.add('chat-workspace-hidden');
    updateRestoreControl();
    if (nextMode === 'closed') ensureRestoreControl().focus({ preventScroll: true });
  } else {
    mode = null;
    setRootUnavailable(root, false);
    document.body.classList.remove('chat-workspace-hidden');
    if (savedScrollTop != null) {
      const history = root.querySelector('#chat-history');
      if (history) history.scrollTop = savedScrollTop;
    }
    savedScrollTop = null;
    const focusTarget = returnFocus;
    returnFocus = null;
    focusChat(focusTarget);
  }
  emitWorkspaceChange();
  return true;
}

function rememberWorkspaceAnchor(root) {
  if (originalPlaceholder?.isConnected) return;
  originalParent = root.parentNode;
  originalNextSibling = root.nextSibling;
  originalPlaceholder = document.createComment('chat workspace position');
  originalParent?.insertBefore(originalPlaceholder, root);
}

function returnRootToWorkspace() {
  const root = chatRoot();
  if (!root) return false;
  if (originalPlaceholder?.parentNode) {
    originalPlaceholder.parentNode.insertBefore(root, originalPlaceholder);
  } else if (originalParent?.isConnected) {
    const next = originalNextSibling?.parentNode === originalParent ? originalNextSibling : null;
    originalParent.insertBefore(root, next);
  } else {
    document.body.append(root);
  }
  originalPlaceholder?.remove();
  originalPlaceholder = null;
  originalParent = null;
  originalNextSibling = null;
  root.classList.remove(POPOUT_ROOT_CLASS);
  document.body.classList.remove('chat-workspace-popout');
  return true;
}

function restoreInlineFromDock() {
  setHidden(null);
  modalManager.unregister(WORKSPACE_ID);
}

function registerInlineDockEntry() {
  if (modalManager.isRegistered(WORKSPACE_ID)) return;
  modalManager.register(WORKSPACE_ID, {
    label: 'Chat',
    icon: CHAT_ICON,
    restoreFn: restoreInlineFromDock,
    closeFn: () => setHidden('closed', returnFocus),
  });
}

function ensurePopupShell() {
  if (popupShell) return popupShell;
  popupShell = document.createElement('div');
  popupShell.id = POPUP_ID;
  popupShell.className = `modal ${POPUP_CLASS} hidden`;
  popupShell.setAttribute('aria-label', 'Chat window');

  popupContent = document.createElement('div');
  popupContent.className = 'modal-content chat-workspace-window-content';

  const header = document.createElement('div');
  header.className = 'modal-header chat-workspace-window-header';
  const title = document.createElement('h4');
  title.textContent = 'Open Clank Chat';
  const actions = document.createElement('div');
  actions.className = 'chat-workspace-window-actions';

  const dock = document.createElement('button');
  dock.type = 'button';
  dock.className = 'chat-window-dock-btn';
  dock.textContent = 'Dock chat';
  dock.setAttribute('aria-label', 'Dock chat back into the workspace');
  dock.addEventListener('click', (event) => {
    event.stopPropagation();
    dockChat(dock, popupReturnFocus);
  });

  const minimize = document.createElement('button');
  minimize.type = 'button';
  minimize.className = 'modal-minimize-btn chat-window-minimize-btn';
  minimize.title = 'Minimize chat';
  minimize.setAttribute('aria-label', 'Minimize chat');
  minimize.innerHTML = uiIcon("remove", 14);
  minimize.addEventListener('click', (event) => {
    event.stopPropagation();
    minimizeChat(minimize);
  });

  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'modal-close chat-window-close-btn';
  close.title = 'Close chat';
  close.setAttribute('aria-label', 'Close chat');
  close.innerHTML = uiIcon('close', 16); close.setAttribute('aria-label', close.getAttribute('aria-label') || close.title || 'Close');
  close.addEventListener('click', (event) => {
    event.stopPropagation();
    closeChat(close);
  });

  actions.append(dock, minimize, close);
  header.append(title, actions);
  popupContent.append(header);
  popupShell.append(popupContent);
  document.body.append(popupShell);

  makeWindowDraggable(popupShell, {
    content: popupContent,
    header,
    enableDock: false,
    enableResize: true,
    mobileSkip: 768,
    minWidth: 360,
    minHeight: 280,
    skipSelector: 'button, input, select, textarea, a',
  });
  return popupShell;
}

function restorePoppedFromDock() {
  const root = chatRoot();
  if (!root) return;
  mode = 'popped';
  setRootUnavailable(root, false);
  const focusTarget = popupReturnFocus;
  popupReturnFocus = null;
  emitWorkspaceChange();
  focusChat(focusTarget);
}

function finishPoppedClose() {
  const focusTarget = rootLastFocus() || popupReturnFocus || closeTrigger;
  closeTrigger = null;
  popupReturnFocus = null;
  returnRootToWorkspace();
  mode = null;
  return setHidden('closed', focusTarget);
}

function rootLastFocus() {
  return chatRoot()?._chatLastFocus || chatRoot()?.querySelector('#message') || null;
}

function registerPopup() {
  modalManager.register(POPUP_ID, {
    label: 'Chat',
    icon: CHAT_ICON,
    restoreFn: restorePoppedFromDock,
    closeFn: finishPoppedClose,
  });
}

export function popOutChat(trigger, focusBeforeAction) {
  const root = chatRoot();
  if (!root) return false;
  observeChatFocus();
  if (mode === 'closed' || mode === 'minimized') restoreChat();
  if (popupShell?.contains(root)) return true;

  const shell = ensurePopupShell();
  popupReturnFocus = preferredChatFocus(focusBeforeAction || trigger);
  if (!mode) savedScrollTop = null;
  rememberWorkspaceAnchor(root);
  registerPopup();
  setRootUnavailable(root, false);
  root.classList.add(POPOUT_ROOT_CLASS);
  shell.classList.remove('hidden', 'modal-minimized');
  popupContent.append(root);
  mode = 'popped';
  document.body.classList.remove('chat-workspace-hidden');
  document.body.classList.add('chat-workspace-popout');
  emitWorkspaceChange();
  focusChat(popupReturnFocus);
  return true;
}

export function dockChat(trigger, focusBeforeAction) {
  const root = chatRoot();
  if (!root || !popupShell?.contains(root)) return false;
  if (modalManager.isMinimized(POPUP_ID)) modalManager.restore(POPUP_ID);

  const focusTarget = preferredChatFocus(focusBeforeAction || trigger || rootLastFocus());
  if (modalManager.isRegistered(POPUP_ID)) modalManager.unregister(POPUP_ID);
  returnRootToWorkspace();
  popupShell.classList.add('hidden');
  mode = null;
  popupReturnFocus = null;
  emitWorkspaceChange();
  focusChat(focusTarget);
  return true;
}

export function minimizeChat(trigger, focusBeforeAction) {
  const root = chatRoot();
  if (!root) return false;
  observeChatFocus();
  if (popupShell?.contains(root)) {
    popupReturnFocus = preferredChatFocus(focusBeforeAction || trigger || rootLastFocus());
    setRootUnavailable(root, true);
    mode = 'minimized';
    updateRestoreControl();
    if (!modalManager.isRegistered(POPUP_ID)) registerPopup();
    const minimized = modalManager.minimize(POPUP_ID);
    emitWorkspaceChange();
    document.querySelector(`.minimized-dock-chip[data-modal-id="${POPUP_ID}"]`)?.focus({ preventScroll: true });
    return minimized;
  }

  if (mode) return false;
  registerInlineDockEntry();
  if (!setHidden('minimized', focusBeforeAction || trigger)) return false;
  const minimized = modalManager.minimize(WORKSPACE_ID);
  document.querySelector(`.minimized-dock-chip[data-modal-id="${WORKSPACE_ID}"]`)?.focus({ preventScroll: true });
  return minimized;
}

export function closeChat(trigger, focusBeforeAction) {
  const root = chatRoot();
  if (popupShell?.contains(root)) {
    closeTrigger = preferredChatFocus(focusBeforeAction || rootLastFocus() || trigger);
    return modalManager.close(POPUP_ID);
  }
  if (modalManager.isMinimized(WORKSPACE_ID)) {
    return modalManager.close(WORKSPACE_ID);
  }
  return setHidden('closed', focusBeforeAction || trigger);
}

export function restoreChat() {
  if (modalManager.isMinimized(POPUP_ID)) return modalManager.restore(POPUP_ID);
  if (modalManager.isMinimized(WORKSPACE_ID)) return modalManager.restore(WORKSPACE_ID);
  if (mode === 'closed' || mode === 'minimized') return setHidden(null);
  return false;
}

export function isChatHidden() {
  return mode === 'closed' || mode === 'minimized';
}

export function chatWorkspaceMode() {
  return mode;
}

ensureMenuActions();
observeChatFocus();
if (!document.getElementById('export-dropdown-menu') && document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', ensureMenuActions, { once: true });
}

// Conversation selection remains authoritative; restore only the mounted view.
document.addEventListener('odysseus:session-selected', () => {
  if (mode) restoreChat();
});

export default {
  popOutChat,
  dockChat,
  minimizeChat,
  closeChat,
  restoreChat,
  isChatHidden,
  chatWorkspaceMode,
};
