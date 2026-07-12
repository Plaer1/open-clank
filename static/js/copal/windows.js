import * as Modals from '../modalManager.js';
import { makeWindowDraggable } from '../windowDrag.js';

export function createCopalWindow({
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
}) {
  const existing = document.getElementById(id);
  if (existing?.__copalWindow) return existing.__copalWindow;

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
  header.append(heading, actions, status, closeButton);
  content.append(header, body);
  root.append(content);
  document.body.append(root);

  let returnFocus = null;
  let visible = false;

  const windowApi = {
    id,
    root,
    content,
    header,
    heading,
    titleNode: title,
    actions,
    body,
    status,
    get visible() { return visible && !root.classList.contains('hidden'); },
    setTitle(value) { title.data = value; root.setAttribute('aria-label', value); content.setAttribute('aria-label', value); },
    setSubtitle(value) { small.textContent = value || ''; },
    setStatus(message, bad = false) {
      status.textContent = message || '';
      status.classList.toggle('error', !!bad);
    },
    focus() { body.focus({ preventScroll: true }); },
    show(trigger = document.activeElement) {
      returnFocus = trigger instanceof HTMLElement ? trigger : returnFocus;
      visible = true;
      if (Modals.isMinimized(id)) Modals.restore(id);
      root.classList.remove('hidden', 'modal-minimized');
      root.style.display = 'flex';
      Modals.register(id, {
        closeFn: () => windowApi.requestClose(true),
        restoreFn: () => windowApi.focus(),
        label,
      });
      Modals.injectMinimizeButton(root, id);
      onActivate?.(windowApi);
      requestAnimationFrame(() => windowApi.focus());
      return windowApi;
    },
    requestClose(fromManager = false) {
      if (onBeforeClose?.(windowApi) === false) return false;
      visible = false;
      root.classList.add('hidden');
      root.classList.remove('modal-minimized');
      root.style.display = 'none';
      if (!fromManager) Modals.unregister(id);
      onClosed?.(windowApi);
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
      return true;
    },
    destroy() {
      visible = false;
      Modals.unregister(id);
      root.remove();
    },
  };
  root.__copalWindow = windowApi;
  closeButton.addEventListener('click', () => windowApi.requestClose());
  root.addEventListener('pointerdown', () => onActivate?.(windowApi), true);
  root.addEventListener('focusin', () => onActivate?.(windowApi), true);
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
