import { uiIcon, hasUiIcon, setUiIconText } from './uiIcons.js';
import { registerMenuDismiss, dismissOrRemove } from './escMenuStack.js';
import { IS_MAC, isAltGrEvent } from './platform.js';
import { visibleWindowBounds } from './windowResize.js';
import {
  WORKBENCH_MENUS, FILES_WORKBENCH_MENUS, configureWorkbenchCommands, registerWorkbenchSurface,
  captureWorkbenchTarget, workbenchCommandDescriptors, runWorkbenchCommand, restoreWorkbenchTarget,
} from './workbenchCommands.js';

const mounts = new Set();
const mountedHosts = new WeakMap();
let nextId = 0;
let activePopup = null;
let activePalette = null;
let routerInstalled = false;
let appOperations = {};
let lastSource = null;

function uniquePrefix(id = 'applet') {
  return `workbench-${String(id).replace(/[^a-zA-Z0-9_-]/g, '-')}-${++nextId}`;
}
function report(message, status) {
  if (status) status.textContent = message;
  window.uiModule?.showToast?.(message);
}
function descriptorsFor(captured, status) {
  try { return workbenchCommandDescriptors(captured); }
  catch (error) { report(error.message || 'The captured commands are unavailable.', status); return null; }
}
const COMMAND_ICON_IDS = Object.freeze({
    'app.editor':'code', 'app.files':'folder', 'app.palette':'menu',
    'app.preferences':'settings', 'app.appearance':'hue', 'app.chat-search':'search',
    'app.help':'help', 'app.docs':'document',
    'window.minimize':'remove', 'window.close':'close', 'window.dock-left':'chevron-left',
    'window.dock-right':'chevron-right', 'window.undock':'restore',
    'new':'file-plus', 'new-file':'file-plus', 'new-note':'file-plus', 'new-folder':'folder-plus',
    'open':'folder-open', 'open-file':'file', 'open-folder':'folder-open', 'close-folder':'close',
    'close-tab':'close', 'find':'search', 'quick-open':'search', 'sidebar':'menu',
    'linked-sidebar':'menu', 'mode-source':'code', 'mode-live':'edit', 'mode-reading':'eye',
    'template-folder':'folder', 'new-template':'file-plus', 'new-wiki':'file-plus',
    'import-memes':'download', 'export-memes':'upload', 'document-back':'back',
    'document-forward':'forward', 'reload-folder':'refresh', 'information':'info',
    'select-all':'select', 'select-next-match':'select', 'select-all-matches':'select',
    'add-cursor-above':'up', 'add-cursor-below':'download', 'collapse-selections':'collapse',
    'paste-plain':'paste', 'see-source':'code', 'insert-template':'paste',
    'new-from-template':'file-plus', 'spellcheck':'check', 'retry-spelling':'refresh',
    'folders-first':'folder', 'navigation':'menu', 'new-window':'workspace',
    'new-window-file':'workspace', 'focus-other':'swap', 'close-split':'close',
});
function commandIconId(descriptor) {
  const ids = COMMAND_ICON_IDS;
  const name = descriptor.id.split('.').slice(1).join('.');
  if (ids[descriptor.id] || ids[name]) return ids[descriptor.id] || ids[name];
  if (name.startsWith('view-')) return name.slice(5);
  if (name.startsWith('sort-')) return 'sort';
  if (name.startsWith('size-')) return 'expand';
  const action = name.replace(/^action-/, '').split('.')[0];
  if (hasUiIcon(action)) return action;
  return { File:'file', Edit:'edit', Selection:'select', View:'eye', Go:'forward', Tools:'settings', Window:'workspace', Help:'help' }[descriptor.menu] || 'menu';
}
function commandButton(descriptor, index, prefix) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'workbench-command'; button.tabIndex = -1;
  button.id = `${prefix}-command-${index}`;
  button.dataset.commandId = descriptor.id;
  button.setAttribute('aria-disabled', String(!!descriptor.disabledReason));
  const name = document.createElement('span'); name.className = 'workbench-command-label'; name.textContent = descriptor.label;
  name.dataset.uiIcons = ''; name.style.cssText = 'display:flex;align-items:center;gap:6px;';
  const icon = document.createElement('span');
  const checked = typeof descriptor.checked === 'boolean';
  icon.innerHTML = uiIcon(checked ? (descriptor.checked ? 'check' : 'unchecked') : commandIconId(descriptor), 14, { role:checked && descriptor.checked ? 'accent' : 'inherit' });
  name.prepend(icon);
  button.append(name);
  if (descriptor.shortcut) {
    const key = document.createElement('kbd'); key.textContent = descriptor.shortcut; button.append(key);
  }
  if (descriptor.disabledReason) {
    const reason = document.createElement('small'); reason.className = 'workbench-command-reason workbench-sr-only';
    button.title = descriptor.disabledReason;
    reason.id = `${prefix}-reason-${index}`; reason.textContent = descriptor.disabledReason;
    button.setAttribute('aria-describedby', reason.id); button.append(reason);
  }
  return button;
}
async function execute(descriptor, captured, status) {
  try {
    restoreWorkbenchTarget(captured);
    await runWorkbenchCommand(descriptor, captured);
  } catch (error) { report(error.message || 'The command could not be completed.', status); }
}
function dismissOtherMenus() {
  activePopup?.close({ restore:false });
  document.querySelectorAll('.openclank-context-menu').forEach(dismissOrRemove);
}

function createPalette(captured, { prefix, label = 'Open Clank', status, onClose = () => {} } = {}) {
  const descriptors = descriptorsFor(captured, status)?.filter((item) => item.id !== 'app.palette');
  if (!descriptors) return null;
  dismissOtherMenus(); activePalette?.close({ restore:false });
  const dialog = document.createElement('dialog'); dialog.className = 'workbench-palette';
  const heading = document.createElement('h2'); heading.id = `${prefix}-palette-title`; heading.textContent = `${label} commands`;
  dialog.setAttribute('aria-labelledby', heading.id);
  const search = document.createElement('input'); search.type = 'search'; search.placeholder = 'Find a command…'; search.autocomplete = 'off';
  search.setAttribute('aria-label', 'Find a command'); search.setAttribute('role', 'combobox'); search.setAttribute('aria-autocomplete', 'list');
  search.setAttribute('aria-expanded', 'true'); search.setAttribute('aria-controls', `${prefix}-results`);
  const list = document.createElement('div'); list.id = `${prefix}-results`; list.className = 'workbench-palette-results';
  list.setAttribute('role', 'listbox'); list.setAttribute('aria-label', 'Commands');
  const feedback = document.createElement('p'); feedback.className = 'workbench-palette-feedback'; feedback.setAttribute('role', 'status');
  const closeButton = document.createElement('button'); closeButton.type = 'button'; closeButton.className = 'workbench-palette-close'; setUiIconText(closeButton, 'close', 'Close');
  let items = [], active = 0, restore = true, done = false;
  function finish() {
    if (done) return;
    done = true; dialog.remove();
    if (activePalette === api) activePalette = null;
    onClose();
    if (restore) restoreWorkbenchTarget(captured);
  }
  const api = {
    element:dialog,
    close(options = {}) { restore = options.restore !== false; if (dialog.open) dialog.close(); finish(); },
  };
  const select = (next) => {
    active = Math.max(0, Math.min(items.length - 1, next));
    items.forEach((item, i) => { item.classList.toggle('active', i === active); item.setAttribute('aria-selected', String(i === active)); });
    if (items[active]) { search.setAttribute('aria-activedescendant', items[active].id); items[active].scrollIntoView({ block:'nearest' }); }
    else search.removeAttribute('aria-activedescendant');
    feedback.textContent = items[active]?.title || (items.length ? `${items.length} commands` : 'No matching commands.');
  };
  const draw = () => {
    const words = search.value.toLowerCase().trim().split(/\s+/).filter(Boolean);
    const matches = descriptors.filter((item) => words.every((word) => `${item.menu} ${item.label}`.toLowerCase().includes(word)));
    list.replaceChildren();
    items = matches.map((descriptor, index) => {
      const item = commandButton({ ...descriptor, label:`${descriptor.menu}: ${descriptor.label}` }, index, `${prefix}-palette`);
      item.setAttribute('role', 'option');
      item.addEventListener('pointerdown', (event) => event.preventDefault());
      item.addEventListener('click', () => {
        if (descriptor.disabledReason) { feedback.textContent = descriptor.disabledReason; return; }
        api.close({ restore:false }); void execute(descriptor, captured, status);
      });
      list.append(item); return item;
    });
    select(0);
  };
  search.addEventListener('input', draw);
  search.addEventListener('keydown', (event) => {
    if (event.isComposing || isAltGrEvent(event)) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); select(active + (event.key === 'ArrowDown' ? 1 : -1)); }
    else if (event.key === 'Enter') { event.preventDefault(); items[active]?.click(); }
    else if ((event.key === 'Home' || event.key === 'End') && event.ctrlKey) { event.preventDefault(); select(event.key === 'Home' ? 0 : items.length - 1); }
  });
  closeButton.addEventListener('click', () => api.close());
  dialog.addEventListener('click', (event) => {
    if (event.target !== dialog) return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) api.close();
  });
  dialog.addEventListener('close', finish, { once:true });
  dialog.append(heading, search, list, feedback, closeButton); document.body.append(dialog);
  activePalette = api; draw(); dialog.showModal(); search.focus();
  return api;
}

function menuFor(target) {
  return target instanceof Node ? [...mounts].find((mount) => mount.host.contains(target)) : null;
}
function openWorkbenchPalette(target) {
  if (document.querySelector('dialog:modal')) {
    if (activePalette?.element.contains(document.activeElement)) activePalette.element.querySelector('input')?.focus();
    return;
  }
  if (target?.editing) {
    const mount = menuFor(target.source) || menuFor(target.owner);
    if (mount) return mount.openPalette(target);
  } else {
    const source = target instanceof Element ? target : document.activeElement;
    const mount = menuFor(source);
    if (mount) return mount.openPalette(source);
  }
  // Keep palette launchers on as-yet-unmounted applets working during adoption.
  try {
    const captured = target?.editing ? target : captureWorkbenchTarget(target instanceof Element ? target : document.activeElement);
    return createPalette(captured, { prefix:uniquePrefix('shell'), label:captured.modal?.getAttribute('aria-label') || 'Open Clank' });
  } catch (error) { report(error.message || 'The command target is unavailable.'); }
}

// Shell setup configures launchers and installs exactly one shortcut router.
// Applet owners mount their own chrome through mountWorkbenchMenu below.
export function initWorkbenchMenu(operations = {}) {
  appOperations = operations;
  configureWorkbenchCommands({ ...appOperations, palette:openWorkbenchPalette });
  window.__openClankWorkbench = Object.freeze({
    registerSurface:registerWorkbenchSurface, capture:captureWorkbenchTarget,
    mount:mountWorkbenchMenu, openPalette:openWorkbenchPalette,
  });
  if (routerInstalled) return;
  routerInstalled = true;
  const remember = (event) => {
    if (event.target instanceof Element && !event.target.closest('.workbench-menubar,.workbench-palette,[role="menu"],.openclank-context-menu,dialog:modal')) lastSource = event.target;
  };
  document.addEventListener('focusin', remember, true);
  document.addEventListener('pointerdown', remember, true);
  document.addEventListener('keydown', (event) => {
    if (event.isComposing || isAltGrEvent(event) || event.defaultPrevented || document.querySelector('dialog:modal')) return;
    const source = document.activeElement;
    const mount = menuFor(source) || (source === document.body ? menuFor(lastSource) : null);
    if ((IS_MAC ? event.metaKey && !event.ctrlKey : event.ctrlKey && !event.metaKey) && event.shiftKey && !event.altKey && event.key.toLowerCase() === 'p') {
      event.preventDefault(); event.stopImmediatePropagation();
      if (mount) mount.openPalette(); else openWorkbenchPalette(source);
    } else if (mount && event.key === 'F10' && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
      event.preventDefault(); event.stopImmediatePropagation(); mount.focusMenu();
    } else if (mount && event.key === 'Escape' && mount.element.contains(source) && !activePopup) {
      event.preventDefault(); event.stopImmediatePropagation(); mount.close();
    }
  }, true);
}

/**
 * Mount below an applet title, inside its content box. For shared windows use
 * {host:win.root, container:win.content, before:win.body}. A surface provider
 * captures this window's active pane and shares its context/action authority.
 */
export function mountWorkbenchMenu({ host, container, before = null, id = host?.id, kind = 'editor', label = kind === 'files' ? 'Files' : 'Editor', surface } = {}) {
  if (!(host instanceof Element) || !(container instanceof Element) || !host.contains(container)
    || before && before.parentElement !== container) throw new TypeError('Applet menus need an owning host and an internal container/insertion point.');
  if (mountedHosts.has(host)) return mountedHosts.get(host).api;
  if (!routerInstalled) initWorkbenchMenu(appOperations);
  const unregisterSurface = surface ? registerWorkbenchSurface(host, { ...surface, kind }) : () => {};
  const prefix = uniquePrefix(id);
  const menuNames = kind === 'files' ? FILES_WORKBENCH_MENUS : WORKBENCH_MENUS;
  const bar = document.createElement('div'); bar.id = `${prefix}-bar`; bar.className = 'workbench-menubar';
  const nav = document.createElement('nav'); nav.className = 'workbench-menu-navigation';
  nav.setAttribute('role', 'menubar'); nav.setAttribute('aria-label', `${label} menu`); nav.setAttribute('aria-orientation', 'horizontal');
  const triggers = menuNames.map((name, index) => {
    const button = document.createElement('button'); button.type = 'button'; button.id = `${prefix}-${name.toLowerCase()}`;
    button.className = 'workbench-menu-trigger'; button.textContent = name; button.tabIndex = index ? -1 : 0;
    button.dataset.workbenchMenu = name; button.setAttribute('role', 'menuitem'); button.setAttribute('aria-haspopup', 'menu');
    button.setAttribute('aria-expanded', 'false'); button.setAttribute('aria-controls', `${prefix}-${name.toLowerCase()}-popup`);
    nav.append(button); return button;
  });
  const status = document.createElement('span'); status.className = 'workbench-sr-only'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  bar.append(nav, status); container.insertBefore(bar, before);
  let lastLocalSource = host, pendingTarget = null, session = null, popup = null, palette = null, openedIndex = -1, disposed = false;
  let unregisterDismiss = () => {}, typeahead = '', typeaheadTimer = null;
  const listeners = [];
  const listen = (node, name, handler, options) => {
    node.addEventListener(name, handler, options); listeners.push(() => node.removeEventListener(name, handler, options));
  };
  const capture = (source = lastLocalSource) => {
    try { return captureWorkbenchTarget(source, host, { host, kind }); }
    catch (error) { report(error.message || 'The original applet is unavailable.', status); return null; }
  };
  const rove = (index) => triggers.forEach((button, i) => { button.tabIndex = i === index ? 0 : -1; });
  function close({ restore = true, keepSession = false } = {}) {
    unregisterDismiss(); unregisterDismiss = () => {};
    popup?.remove(); popup = null;
    if (activePopup === instance) activePopup = null;
    triggers.forEach((button) => button.setAttribute('aria-expanded', 'false'));
    openedIndex = -1; clearTimeout(typeaheadTimer); typeahead = '';
    const prior = session || pendingTarget;
    if (!keepSession) { session = null; pendingTarget = null; }
    if (restore) restoreWorkbenchTarget(prior);
  }
  function positionPopup() {
    if (!popup || openedIndex < 0) return;
    const bounds = visibleWindowBounds(bar), rect = triggers[openedIndex].getBoundingClientRect(), barRect = bar.getBoundingClientRect();
    const width = Math.min(340, Math.max(1, bounds.width - 8));
    popup.style.width = `${width}px`;
    const left = Math.max(bounds.left + 4, Math.min(rect.left, bounds.right - width - 4));
    const top = Math.max(bounds.top + 4, Math.min(barRect.bottom, bounds.bottom - 40));
    popup.style.maxHeight = `${Math.max(1, bounds.bottom - top - 4)}px`;
    // Popup is an absolute child of the local bar, so dragging/docking moves it
    // with the applet and its z-order never rises above an unrelated window.
    popup.style.left = `${left - barRect.left - bar.clientLeft}px`;
    popup.style.top = `${top - barRect.top - bar.clientTop}px`;
  }
  function openMenu(index, { focusItem = true, last = false } = {}) {
    if (disposed || document.querySelector('dialog:modal')) return;
    const captured = session || pendingTarget || capture();
    if (!captured) return;
    const descriptors = descriptorsFor(captured, status)?.filter((item) => item.menu === menuNames[index]);
    if (!descriptors) return;
    // Switching menus retains the exact immutable window/pane snapshot.
    close({ restore:false, keepSession:true }); dismissOtherMenus(); session = captured; pendingTarget = null;
    openedIndex = index; rove(index); activePopup = instance;
    const trigger = triggers[index]; trigger.setAttribute('aria-expanded', 'true');
    popup = document.createElement('div'); popup.id = trigger.getAttribute('aria-controls'); popup.className = 'workbench-menu-popup';
    popup.setAttribute('role', 'menu'); popup.setAttribute('aria-labelledby', trigger.id);
    popup._dismiss = () => close();
    const feedback = document.createElement('div'); feedback.className = 'workbench-menu-feedback';
    feedback.setAttribute('role', 'status'); feedback.setAttribute('aria-live', 'polite');
    const describe = (descriptor) => { feedback.textContent = descriptor.disabledReason || descriptor.label; };
    descriptors.forEach((descriptor, i) => {
      const item = commandButton(descriptor, i, `${prefix}-${index}`);
      item.setAttribute('role', typeof descriptor.checked === 'boolean' ? 'menuitemcheckbox' : 'menuitem');
      if (typeof descriptor.checked === 'boolean') item.setAttribute('aria-checked', String(descriptor.checked));
      item.addEventListener('focus', () => describe(descriptor)); item.addEventListener('pointerenter', () => describe(descriptor));
      item.addEventListener('click', () => {
        if (descriptor.disabledReason) { describe(descriptor); return; }
        close({ restore:false }); void execute(descriptor, captured, status);
      });
      popup.append(item);
    });
    if (!descriptors.length) feedback.textContent = 'No actions are available here.';
    popup.append(feedback); bar.append(popup); positionPopup();
    unregisterDismiss = registerMenuDismiss(() => close());
    const items = [...popup.querySelectorAll('[role="menuitem"],[role="menuitemcheckbox"]')];
    if (focusItem && items.length) (last ? items.at(-1) : items[0]).focus(); else trigger.focus({ preventScroll:true });
    popup.addEventListener('keydown', (event) => {
      if (event.isComposing || isAltGrEvent(event)) return;
      const at = items.indexOf(document.activeElement);
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); items[(at + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); (event.key === 'Home' ? items[0] : items.at(-1))?.focus();
      } else if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault(); openMenu((index + (event.key === 'ArrowRight' ? 1 : -1) + triggers.length) % triggers.length);
      } else if (event.key === 'Tab') {
        close({ restore:false }); trigger.focus({ preventScroll:true });
      } else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && event.key !== ' ') {
        clearTimeout(typeaheadTimer); typeahead += event.key.toLowerCase();
        const rotated = [...items.slice(at + 1), ...items.slice(0, at + 1)];
        let match = rotated.find((item) => item.querySelector('.workbench-command-label').textContent.toLowerCase().startsWith(typeahead));
        if (!match) { typeahead = event.key.toLowerCase(); match = rotated.find((item) => item.querySelector('.workbench-command-label').textContent.toLowerCase().startsWith(typeahead)); }
        typeaheadTimer = setTimeout(() => { typeahead = ''; }, 600);
        if (match) { event.preventDefault(); match.focus(); }
      }
    });
  }
  const remember = (event) => {
    if (event.target instanceof Element && !event.target.closest('.workbench-menubar,.workbench-palette,[role="menu"],.openclank-context-menu')) lastLocalSource = event.target;
  };
  listen(host, 'focusin', remember, true); listen(host, 'pointerdown', remember, true);
  triggers.forEach((button, index) => {
    listen(button, 'pointerdown', () => { if (!session) pendingTarget = capture(); });
    listen(button, 'focus', () => { rove(index); if (!session) pendingTarget ||= capture(); });
    listen(button, 'click', () => { if (openedIndex === index) close(); else openMenu(index); });
    listen(button, 'pointerenter', () => { if (popup && openedIndex !== index) openMenu(index, { focusItem:false }); });
    listen(button, 'keydown', (event) => {
      if (event.isComposing || isAltGrEvent(event)) return;
      if (event.key === 'ArrowRight' || event.key === 'ArrowLeft') {
        event.preventDefault(); const next = (index + (event.key === 'ArrowRight' ? 1 : -1) + triggers.length) % triggers.length;
        if (popup) openMenu(next, { focusItem:false }); else { rove(next); triggers[next].focus(); }
      } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); openMenu(index, { last:event.key === 'ArrowUp' });
      } else if (event.key === 'Home' || event.key === 'End') {
        event.preventDefault(); const next = event.key === 'Home' ? 0 : triggers.length - 1; rove(next); triggers[next].focus();
      } else if (event.key === 'Tab') close({ restore:false });
      else if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey && event.key !== ' ') {
        const next = menuNames.map((name, i) => ({ name, i })).find((item) => item.i > index && item.name.toLowerCase().startsWith(event.key.toLowerCase()))
          || menuNames.map((name, i) => ({ name, i })).find((item) => item.name.toLowerCase().startsWith(event.key.toLowerCase()));
        if (next) { event.preventDefault(); if (popup) openMenu(next.i, { focusItem:false }); else { rove(next.i); triggers[next.i].focus(); } }
      }
    });
  });
  const leaveMenu = (event) => {
    if (!bar.contains(event.target)) { if (popup) close({ restore:false }); pendingTarget = null; }
  };
  listen(document, 'pointerdown', leaveMenu, true); listen(document, 'focusin', leaveMenu, true);
  listen(window, 'resize', positionPopup); listen(nav, 'scroll', positionPopup, { passive:true });
  listen(host, 'scroll', positionPopup, true);
  const lifecycle = () => {
    if (!host.isConnected) { dispose(); return; }
    if (host.closest('.hidden,[hidden],.modal-minimized') || !host.getClientRects().length) {
      close({ restore:false }); palette?.close({ restore:false });
    } else positionPopup();
  };
  const observer = new MutationObserver(lifecycle);
  for (const node of new Set([host, container, host.closest('.modal'), bar.closest('.modal-content')])) {
    if (node) observer.observe(node, { attributes:true, attributeFilter:['class', 'style', 'hidden'] });
  }
  if (host.parentNode) observer.observe(host.parentNode, { childList:true });
  const resizeObserver = new ResizeObserver(positionPopup); resizeObserver.observe(container);
  function openPalette(target) {
    if (disposed) return;
    if (document.querySelector('dialog:modal') && !palette?.element.contains(document.activeElement)) return;
    if (palette?.element.isConnected) { palette.element.querySelector('input')?.focus(); return; }
    const captured = target?.editing ? target : session || pendingTarget || capture(target instanceof Element ? target : lastLocalSource);
    if (!captured) return;
    if (captured.owner !== host && !host.contains(captured.source)) { report('The original command belongs to another applet.', status); return; }
    close({ restore:false });
    palette = createPalette(captured, { prefix, label, status, onClose:() => { palette = null; } });
  }
  function focusMenu() {
    if (popup || bar.contains(document.activeElement)) { close(); return; }
    pendingTarget = capture(); if (!pendingTarget) return;
    rove(0); triggers[0].focus();
  }
  function dispose() {
    if (disposed) return;
    disposed = true; close({ restore:false }); palette?.close({ restore:false });
    listeners.forEach((remove) => remove()); observer.disconnect(); resizeObserver.disconnect(); unregisterSurface();
    bar.remove(); mounts.delete(instance); mountedHosts.delete(host);
  }
  const api = Object.freeze({ element:bar, close, openPalette, dispose });
  const instance = { host, element:bar, api, close, openPalette, focusMenu };
  mounts.add(instance); mountedHosts.set(host, instance);
  return api;
}
