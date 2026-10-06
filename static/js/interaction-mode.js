// Single Open Clank interaction-mode picker. Provider-native mode names stay
// behind the server/ACP adapter and are never rendered here.

import { topPortalZ } from './toolWindowZOrder.js';

const MODES = Object.freeze({
  chat: { label: 'Chat', description: 'Read-only answers and safe context' },
  plan: { label: 'Plan', description: 'Investigate and propose a plan' },
  agent: { label: 'Agent', description: 'Use the enabled agent tools' },
});

export function initInteractionModeControl({
  documentRef = document,
  getMode = () => 'agent',
  setMode = () => {},
} = {}) {
  const root = documentRef.getElementById('chat-mode-picker');
  const button = documentRef.getElementById('chat-mode-picker-btn');
  const label = documentRef.getElementById('chat-mode-picker-label');
  const menu = documentRef.getElementById('chat-mode-picker-menu');
  if (!root || !button || !label || !menu) return { destroy() {} };

  if (root.__openClankInteractionMode?.destroy) {
    root.__openClankInteractionMode.destroy();
  }

  let open = false;
  let destroyed = false;
  const options = Array.from(menu.querySelectorAll('[data-interaction-mode]'));
  const menuOwner = menu.parentNode;
  const menuNextSibling = menu.nextSibling;
  const originalStyle = menu.style.cssText;
  const view = documentRef.defaultView || window;

  // The composer's horizontal tool row clips overflow. An open menu belongs
  // above that row, so use the same body portal as other composer overlays.
  const positionMenu = () => {
    if (!open) return;
    const viewport = view.visualViewport;
    const leftEdge = (viewport?.offsetLeft || 0) + 8;
    const topEdge = (viewport?.offsetTop || 0) + 8;
    const width = viewport?.width || view.innerWidth;
    const height = viewport?.height || view.innerHeight;
    const rightEdge = leftEdge + width - 16;
    const bottomEdge = topEdge + height - 16;
    const anchor = button.getBoundingClientRect();
    menu.style.position = 'fixed';
    menu.style.bottom = 'auto';
    menu.style.right = 'auto';
    menu.style.maxWidth = `${Math.max(0, width - 16)}px`;
    menu.style.maxHeight = '';
    menu.style.overflowY = '';
    menu.style.zIndex = String(topPortalZ({ root:documentRef, getStyle:view.getComputedStyle.bind(view) }));
    const natural = menu.getBoundingClientRect();
    const above = Math.max(0, anchor.top - topEdge - 6);
    const below = Math.max(0, bottomEdge - anchor.bottom - 6);
    const placeAbove = natural.height <= above || above >= below;
    const available = placeAbove ? above : below;
    const menuHeight = Math.min(natural.height, available);
    menu.style.maxHeight = `${available}px`;
    menu.style.overflowY = 'auto';
    menu.style.left = `${Math.max(leftEdge, Math.min(anchor.left, rightEdge - natural.width))}px`;
    menu.style.top = `${Math.max(topEdge, placeAbove ? anchor.top - 6 - menuHeight : anchor.bottom + 6)}px`;
  };
  const onScroll = (event) => {
    if (!menu.contains(event.target)) positionMenu();
  };
  const watchPosition = (watch) => {
    const method = watch ? 'addEventListener' : 'removeEventListener';
    view[method]('resize', positionMenu);
    documentRef[method]('scroll', onScroll, true);
    view.visualViewport?.[method]('resize', positionMenu);
    view.visualViewport?.[method]('scroll', positionMenu);
  };

  const normalize = (mode) => Object.hasOwn(MODES, mode) ? mode : 'agent';
  const render = (mode = getMode()) => {
    const normalized = normalize(mode);
    const info = MODES[normalized];
    label.textContent = info.label;
    button.dataset.mode = normalized;
    button.title = `${info.label} mode — ${info.description}`;
    button.setAttribute('aria-label', `Interaction mode: ${info.label}`);
    options.forEach((option) => {
      const selected = option.dataset.interactionMode === normalized;
      option.setAttribute('aria-selected', String(selected));
      option.classList.toggle('selected', selected);
    });
  };
  const close = (restoreFocus = false) => {
    open = false;
    menu.hidden = true;
    watchPosition(false);
    if (menuOwner && menu.parentNode !== menuOwner) {
      menuOwner.insertBefore(menu, menuNextSibling?.parentNode === menuOwner ? menuNextSibling : null);
    }
    menu.style.cssText = originalStyle;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  };
  const toggle = () => {
    if (open) { close(true); return; }
    open = true;
    documentRef.body.appendChild(menu);
    menu.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    positionMenu();
    watchPosition(true);
    options.find((item) => item.getAttribute('aria-selected') === 'true')?.focus();
  };
  const choose = (mode) => {
    const normalized = normalize(mode);
    setMode(normalized);
    render(normalized);
    close(true);
  };
  const onButtonClick = () => toggle();
  const onDocumentClick = (event) => {
    if (open && !root.contains(event.target) && !menu.contains(event.target)) close();
  };
  const onKeyDown = (event) => {
    if (!open && (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      toggle();
      return;
    }
    if (!open) return;
    const current = Math.max(0, options.indexOf(documentRef.activeElement));
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      options[(current + 1) % options.length]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      options[(current - 1 + options.length) % options.length]?.focus();
    } else if (event.key === 'Home') {
      event.preventDefault();
      options[0]?.focus();
    } else if (event.key === 'End') {
      event.preventDefault();
      options.at(-1)?.focus();
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose(documentRef.activeElement?.dataset.interactionMode);
    }
  };
  const onMenuClick = (event) => {
    const option = event.target.closest?.('[data-interaction-mode]');
    if (option) choose(option.dataset.interactionMode);
  };
  button.addEventListener('click', onButtonClick);
  button.addEventListener('keydown', onKeyDown);
  menu.addEventListener('keydown', onKeyDown);
  menu.addEventListener('click', onMenuClick);
  documentRef.addEventListener('click', onDocumentClick);
  window.__odysseusInteractionModeSync = render;
  render();

  const handle = {
    destroy() {
      if (destroyed) return;
      destroyed = true;
      button.removeEventListener('click', onButtonClick);
      button.removeEventListener('keydown', onKeyDown);
      menu.removeEventListener('keydown', onKeyDown);
      menu.removeEventListener('click', onMenuClick);
      documentRef.removeEventListener('click', onDocumentClick);
      if (window.__odysseusInteractionModeSync === render) {
        delete window.__odysseusInteractionModeSync;
      }
      close();
    },
    render,
  };
  root.__openClankInteractionMode = handle;
  return handle;
}

export { MODES };
export default initInteractionModeControl;
