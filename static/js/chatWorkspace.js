// Owns reversible visibility for the persistent chat. Hiding changes layout
// and input ownership while leaving session, draft, history and scroll mounted.
const RESTORE_ID = 'chat-workspace-restore';
let mode = null;
let returnFocus = null;
let savedScrollTop = null;
const chatRoot = () => document.getElementById('chat-container');

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
      menu.classList.remove('open');
      action(row);
    });
    row.addEventListener('keydown', (event) => {
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      row.click();
    });
    menu.append(row);
  };
  add('chat-minimize-btn', 'Minimize chat', '_', (row) => minimizeChat(row));
  add('chat-close-btn', 'Close chat', '×', (row) => closeChat(row));
}

function updateRestoreControl() {
  const control = ensureRestoreControl();
  control.hidden = !mode;
  control.textContent = mode === 'closed' ? 'Reopen chat' : 'Restore chat';
  control.setAttribute('aria-label', control.textContent);
}

function setHidden(nextMode, trigger) {
  const root = chatRoot();
  if (!root) return false;
  if (nextMode) {
    if (!mode) returnFocus = trigger instanceof HTMLElement ? trigger : document.activeElement;
    if (!mode) savedScrollTop = chatRoot().querySelector('#chat-history')?.scrollTop ?? null;
    mode = nextMode;
    root.setAttribute('aria-hidden', 'true');
    if ('inert' in root) root.inert = true;
    document.body.classList.add('chat-workspace-hidden');
    if (document.activeElement && root.contains(document.activeElement)) document.activeElement.blur();
    updateRestoreControl();
    ensureRestoreControl().focus({ preventScroll: true });
  } else {
    mode = null;
    root.removeAttribute('aria-hidden');
    if ('inert' in root) root.inert = false;
    document.body.classList.remove('chat-workspace-hidden');
    if (savedScrollTop != null) {
      const history = root.querySelector('#chat-history');
      if (history) history.scrollTop = savedScrollTop;
    }
    savedScrollTop = null;
    updateRestoreControl();
    const focusTarget = returnFocus?.isConnected && !returnFocus.closest?.('#chat-container')
      ? returnFocus : document.getElementById('message');
    returnFocus = null;
    focusTarget?.focus?.({ preventScroll: true });
  }
  window.dispatchEvent(new Event('resize'));
  window.dispatchEvent(new CustomEvent('chat-workspace-change', { detail: { mode } }));
  return true;
}

export function minimizeChat(trigger) { return setHidden('minimized', trigger); }
export function closeChat(trigger) { return setHidden('closed', trigger); }
export function restoreChat() { return setHidden(null); }
export function isChatHidden() { return !!mode; }
export function chatWorkspaceMode() { return mode; }

ensureMenuActions();
// Opening any chat through the canonical session/navigation path must make a
// hidden workspace available again. The session remains authoritative for
// persistence; this listener only restores the mounted view and focus scope.
document.addEventListener('odysseus:session-selected', () => {
  if (mode) restoreChat();
});
export default { minimizeChat, closeChat, restoreChat, isChatHidden, chatWorkspaceMode };
