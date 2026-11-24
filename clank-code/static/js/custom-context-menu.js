import { visibleWindowBounds } from './windowResize.js';
import { dismissTopMenu, registerMenuDismiss } from './escMenuStack.js';
import { IS_MAC } from './platform.js';
import { hasUiIcon, uiIcon } from './uiIcons.js';

// The Copal menu owns the editing interaction in every browser. It keeps the
// target and its selection in a short lived context so opening the menu cannot
// redirect a command to whichever window receives focus later.

const STORAGE_KEY = 'openclank-custom-context-menu';
const MENU_ID = 'openclank-context-menu';
let active = null;
let longPressTimer = null;
let longPressOrigin = null;
let heldTouch = null;
const adapters = new WeakMap();
const LEGACY_FILE_COMMANDS = [
  ['Open in Editor', 'open-in-editor'], ['Open right', 'open-in-editor-split-right'],
  ['Open below', 'open-in-editor-split-below'], ['Select all files', 'select-all-files'],
  ['Copy file path', 'copy-file-path'], ['Reveal in Files', 'reveal-file'], ['Rename', 'rename-file'],
  ['Move', 'move-files'], ['Copy', 'copy-files'], ['Move to trash', 'trash-file'], ['Restore', 'restore-file'],
];
const OBJECT_COMMANDS = Object.freeze({
  image:[['Open image', 'open-image'], ['Copy image address', 'copy-image-address'], ['Copy image', 'copy-image-bytes']],
  link:[['Open link in new tab', 'open-link'], ['Copy link address', 'copy-link-address']],
  file:LEGACY_FILE_COMMANDS,
  workspace:[['Open Workspace Hexes', 'open-workspace-hexes']],
});

const mod = IS_MAC ? 'Cmd' : 'Ctrl';
export const editingShortcutHints = Object.freeze({
  undo:`${mod}+Z`, redo:IS_MAC ? 'Cmd+Shift+Z' : 'Ctrl+Y',
  cut:`${mod}+X`, copy:`${mod}+C`, paste:`${mod}+V`, 'select-all':`${mod}+A`,
  'select-next-match':`${mod}+D`, 'select-all-matches':`${mod}+Shift+L`,
  'add-cursor-above':`${mod}+Alt+↑`, 'add-cursor-below':`${mod}+Alt+↓`, 'collapse-selections':'Escape',
});

function enabled() {
  try { return localStorage.getItem(STORAGE_KEY) !== 'off'; } catch (_) { return true; }
}

function editableTarget(target) {
  const candidate = target?.closest?.('input, textarea, [contenteditable="true"], .cm-content');
  if (!candidate || candidate.disabled || candidate.readOnly || candidate.getAttribute('aria-readonly') === 'true'
    || (candidate.classList.contains('cm-content') && candidate.getAttribute('contenteditable') === 'false')) return null;
  return candidate;
}

const NATIVE_CONTROLS = 'input,textarea,select,button,a[href],summary,[contenteditable],iframe,video,audio';
const PROTECTED_TARGETS = 'input[type="password"],iframe,[data-openclank-native-context-menu]';
const TEXT_INPUT_TYPES = new Set(['search', 'tel', 'text', 'url']);

function nativeTextTarget(target) {
  const input = target?.closest?.('input, textarea, [contenteditable="true"]');
  return input && (!input.matches('input') || TEXT_INPUT_TYPES.has(input.type)) ? input : null;
}

function findAdapter(target, event) {
  const boundary = target?.closest?.(NATIVE_CONTROLS);
  const object = target?.closest?.('[data-copal-context-object]');
  let current = target;
  while (current instanceof Element) {
    const adapter = adapters.get(current);
    if (adapter && !(adapter.ownsObjectCommands && editableTarget(target))) {
      try {
        const hit = adapter.hitTest?.(target, event);
        if (hit === 'native') return { reserved:true };
        if (hit !== false && (!boundary || boundary === current || current === target && boundary.contains(current)
          || hit === true || object === boundary && current.contains(object))) {
          const context = adapter.capture?.(target, event);
          if (!adapter.capture || context != null) return { element:current, adapter, context:context || null };
        }
      } catch (_) { return { reserved:true }; }
    }
    current = current.parentElement;
  }
  return null;
}

function objectTarget(target) {
  const node = target?.closest?.('[data-copal-context-object]');
  const kind = node?.dataset.copalContextObject;
  return kind && Object.hasOwn(OBJECT_COMMANDS, kind) ? { target:node, kind } : null;
}

function selectionSnapshot(target) {
  if (target && typeof target.selectionStart === 'number') {
    return { start: target.selectionStart, end: target.selectionEnd ?? target.selectionStart };
  }
  const selection = window.getSelection?.();
  if (!selection?.rangeCount || selection.isCollapsed || !target?.contains?.(selection.anchorNode) || !target.contains(selection.focusNode)) return null;
  return { range: selection.getRangeAt(0).cloneRange() };
}

function selectedText(request) {
  if (request.adapterContext && typeof request.adapterContext.text === 'string') return request.adapterContext.text;
  const target = request.target;
  if (target && typeof target.value === 'string' && request.selectionRange) {
    return target.value.slice(request.selectionRange.start, request.selectionRange.end);
  }
  return request.selection || '';
}

function hasSelectedText(request) {
  if (request.adapterContext?.widget) return !!request.adapterContext.widgetSelection;
  const ranges = request.adapterContext?.ranges;
  // Separators used to format multi-range clipboard text are not selections.
  return Array.isArray(ranges) ? ranges.some(range => range.anchor !== range.head) : !!selectedText(request);
}

function fileCapabilities(target) {
  if (!target?.dataset || !Object.prototype.hasOwnProperty.call(target.dataset, 'fileCapabilities')) return null;
  const raw = target.dataset.fileCapabilities || '';
  return new Set(raw.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean));
}

function objectHandler(kind) {
  if (kind === 'file' && typeof window.__openClankFilesContextCommand === 'function') return window.__openClankFilesContextCommand;
  return typeof window.__openClankCopalContextCommand === 'function' ? window.__openClankCopalContextCommand : null;
}

function targetVisible(target) {
  return !!target?.isConnected && !target.closest('.hidden,[hidden],.modal-minimized,.modal-closing')
    && !!target.getClientRects().length;
}

function objectIdentity(target) {
  return target ? JSON.stringify([target.currentSrc || target.src || '', target.href || '', ...Object.entries(target.dataset)]) : '';
}

function restoreSelection(request, { focus = true } = {}) {
  const target = request?.target;
  if (!target?.isConnected) return false;
  if (focus) target.focus?.({ preventScroll: true });
  if (request.selectionRange && typeof target.setSelectionRange === 'function') {
    try { target.setSelectionRange(request.selectionRange.start, request.selectionRange.end); } catch (_) { return false; }
    return true;
  }
  const range = request.domRange;
  if (range && target.contains?.(range.commonAncestorContainer)) {
    const selection = window.getSelection?.();
    if (!selection) return false;
    selection.removeAllRanges(); selection.addRange(range);
    return true;
  }
  return !!target;
}

function requestEditableText(target) {
  return target?.isContentEditable ? target.textContent || '' : null;
}

function spellingServiceSnapshot() {
  const service = window.openClankSpelling || null;
  return Object.freeze({ service, scope:service?.scopeKey?.(), revision:service?.revision?.() });
}
function spellingServiceCurrent(request) {
  const captured = request?.spellingService;
  return !!captured && captured.service === (window.openClankSpelling || null)
    && captured.scope === captured.service?.scopeKey?.() && captured.revision === captured.service?.revision?.();
}
function assertSpellingTarget(request) {
  if (!isCapturedContextCurrent(request) || !spellingServiceCurrent(request)) throw new Error('The spelling target or account dictionary changed. Reopen the menu.');
}
function canRetrySpelling(request) {
  const service = request?.spellingService?.service;
  return spellingServiceCurrent(request) && service?.status?.().state === 'error' && typeof service.retry === 'function';
}
function isKeyboardInvocation(event) {
  return event.detail === 0 && !event.pointerType && event.button !== 2;
}

function capture(event) {
  const eventTarget = event.target instanceof Element ? event.target : null;
  const keyboard = isKeyboardInvocation(event);
  const rawTarget = keyboard && (!eventTarget || eventTarget === document.body)
    ? (document.activeElement instanceof Element ? document.activeElement : document.body)
    : (eventTarget || document.body);
  const protectedTarget = rawTarget.closest(PROTECTED_TARGETS);
  const registered = protectedTarget ? { reserved:true } : findAdapter(rawTarget, event);
  const object = !registered && objectTarget(rawTarget);
  const textTarget = !registered?.reserved && nativeTextTarget(rawTarget);
  const target = registered?.adapter
    ? (editableTarget(rawTarget) || rawTarget)
    : textTarget || object?.target || rawTarget;
  const editable = !registered?.reserved && !!editableTarget(target)
    && !registered?.context?.readOnly && (!!registered?.adapter || !!textTarget);
  const range = !registered?.reserved && !registered?.adapter && selectionSnapshot(target);
  const selectionRange = range?.start != null ? range : null;
  const domRange = range?.range || null;
  const rect = target.getBoundingClientRect();
  let adapterContext = registered?.context || null;
  if (object?.kind === 'file' && typeof window.__openClankFilesContextCapture === 'function') {
    try { adapterContext = window.__openClankFilesContextCapture(object.target, event) || null; } catch (_) {}
  }
  return Object.freeze({
    requestId: `ctx-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    x: keyboard ? rect.left : event.clientX ?? rect.left,
    y: keyboard ? rect.bottom : event.clientY ?? rect.bottom,
    target, rawTarget, editable, reserved:!!registered?.reserved,
    value: registered ? null : typeof target.value === 'string' && textTarget ? target.value : requestEditableText(target),
    selection:domRange?.toString() || '', selectionRange, domRange, spellingService:spellingServiceSnapshot(),
    domSelection:domRange ? Object.freeze({ start:domRange.startContainer, startOffset:domRange.startOffset, end:domRange.endContainer, endOffset:domRange.endOffset }) : null,
    link:object?.kind === 'link' ? object.target.href || '' : '',
    modality: event.pointerType === 'touch' ? 'touch' : keyboard ? 'keyboard' : 'pointer',
    adapter: registered?.adapter || null,
    adapterElement: registered?.element || null,
    adapterContext,
    objectTarget: object?.target || null,
    objectKind: object?.kind || '',
    objectIdentity: objectIdentity(object?.target),
    objectHandler: objectHandler(object?.kind),
  });
}

function setInputSelection(target, start, end, text) {
  if (!target || typeof target.setRangeText !== 'function') return false;
  target.focus?.({ preventScroll:true });
  target.setRangeText(text, start, end, 'end');
  target.dispatchEvent(new InputEvent('input', { bubbles:true, inputType:'insertText', data:text }));
  return true;
}

async function clipboardWrite(text) {
  if (!navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
  await navigator.clipboard.writeText(text);
}

async function clipboardRead() {
  if (!navigator.clipboard?.readText) throw new Error('Clipboard reading is unavailable in this browser.');
  return navigator.clipboard.readText();
}

function localDelete(request) {
  const target = request.target;
  if (request.selectionRange && setInputSelection(target, request.selectionRange.start, request.selectionRange.end, '')) return;
  if (restoreSelection(request)) document.execCommand?.('delete');
}

// Shared gateway for main-menu/palette actions; object handlers retain their
// existing captured-resource authority. It does not depend on the open popup.
export function captureContextCommandTarget(target = document.activeElement) {
  return capture({ target, detail:0 });
}

export function isCapturedContextCurrent(request) {
  if (!request || request.reserved || !targetVisible(request.target)) return false;
  if (request.objectTarget && (!targetVisible(request.objectTarget)
    || request.objectIdentity !== objectIdentity(request.objectTarget)
    || request.objectHandler !== objectHandler(request.objectKind))) return false;
  if (request.editable && !editableTarget(request.target)) return false;
  if (!request.editable && !request.adapter && nativeTextTarget(request.target) && editableTarget(request.target)) return false;
  if (request.adapter) {
    if (adapters.get(request.adapterElement) !== request.adapter) return false;
    if (!request.adapterElement.contains(request.rawTarget) || !request.rawTarget.isConnected) return false;
    // Some legacy row adapters validate within execute; their captured node
    // and registration must still belong to the same mounted owner.
    return request.adapter.isCurrent ? !!request.adapter.isCurrent(request) : request.adapterContext != null;
  }
  if (request.value !== null) {
    const current = typeof request.target.value === 'string' ? request.target.value : requestEditableText(request.target);
    if (current !== request.value) return false;
  }
  if (request.selectionRange) {
    return request.target.selectionStart === request.selectionRange.start
      && request.target.selectionEnd === request.selectionRange.end;
  }
  if (request.domSelection) {
    const selection = window.getSelection?.(), captured = request.domSelection;
    if (!selection?.rangeCount || !request.target.contains(captured.start) || !request.target.contains(captured.end)) return false;
    const range = selection.getRangeAt(0);
    return range.startContainer === captured.start && range.startOffset === captured.startOffset
      && range.endContainer === captured.end && range.endOffset === captured.endOffset;
  }
  return true;
}

export function restoreCapturedContext(request) {
  if (!isCapturedContextCurrent(request)) return false;
  if (typeof request.adapter?.restore === 'function') return request.adapter.restore(request);
  return restoreSelection(request);
}

function basicEditingCommands(request) {
  const selection = hasSelectedText(request);
  const editable = request.editable;
  const canCopy = selection && (editable || request.adapter || request.selectionRange || request.domRange);
  const reason = editable ? '' : 'Focus an editable text field or source editor.';
  const basic = [
    ['undo', 'Undo', reason], ['redo', 'Redo', reason],
    ['cut', 'Cut', reason || (!selection ? 'Select text first.' : '')],
    ['copy', 'Copy', canCopy ? '' : 'Select text first.'],
    ['paste', 'Paste', reason], ['paste-plain', 'Paste as plain text', reason],
    ['delete', 'Delete selection', reason || (!selection ? 'Select text first.' : '')],
    ['select-all', 'Select all', (editable || request.adapter || request.selectionRange) ? '' : reason],
  ].map(([id, label, disabledReason]) => ({ id, label, disabledReason }));
  if (!request.adapter && editable) {
    const service = request.spellingService?.service;
    basic.push({ id:'spellcheck', label:'Check selected spelling locally', disabledReason:!selection ? 'Select text to check first.' : typeof service?.checkBatch !== 'function' ? 'The local spelling service is unavailable.' : '' });
    if (canRetrySpelling(request)) basic.push({ id:'retry-spelling', label:'Retry spelling', disabledReason:'' });
  }
  return basic;
}

export function capturedEditingCommands(request) {
  const basic = basicEditingCommands(request);
  const selection = hasSelectedText(request);
  const editable = request.editable;
  const reason = editable ? '' : 'Focus an editable text field or source editor.';
  if (request.reserved || (request.adapter && typeof request.adapter.isCurrent !== 'function')) return basic.map((item) => ({ ...item, disabledReason:'Text commands are unavailable on this surface.' }));
  if (request.adapter?.editingCommands === false) return basic;
  for (const item of request.adapter?.commands?.(request) || []) {
    const selectionOnly = ['select-all-matches', 'format-bold', 'format-italic', 'format-code'].includes(item.id);
    const readOnlySafe = ['select-next-match', 'select-all-matches', 'see-source', 'collapse-selections', 'add-cursor-above', 'add-cursor-below', 'spellcheck', 'retry-spelling'].includes(item.id);
    basic.push({ ...item, disabledReason:item.disabledReason || (!editable && !readOnlySafe ? reason : '')
      || (item.disabled ? (selectionOnly && !selection ? 'Select text first.' : 'Unavailable in this editor.') : '') });
  }
  return basic;
}

export async function executeCapturedContextCommand(command, request) {
  if (!isCapturedContextCurrent(request)) throw new Error('The original menu target changed. Reopen its menu.');
  if (command === 'check-spelling') command = 'spellcheck';
  if (['spellcheck', 'retry-spelling', 'add-word', 'remove-word'].includes(command) || command.startsWith('replace-spelling:')) assertSpellingTarget(request);
  if (command === 'retry-spelling') {
    if (!canRetrySpelling(request)) throw new Error('Spelling does not need a retry.');
    await request.spellingService.service.retry();
    assertSpellingTarget(request);
    // Retry checks a fresh capture only after the original owner/selection and
    // account service are still current; it never resumes a discarded target.
    return executeCapturedContextCommand('spellcheck', captureContextCommandTarget(request.target));
  }
  if (request.adapter?.execute) {
    const handled = await request.adapter.execute(command, request);
    if (handled === false) throw new Error('The context-menu command could not be completed.');
    return;
  }
  if ((command === 'copy-link-address' || command === 'open-link') && request.link) {
    if (command === 'copy-link-address') await clipboardWrite(request.link);
    else window.open(request.link, '_blank', 'noopener,noreferrer');
    return;
  }
  if (request.objectTarget && OBJECT_COMMANDS[request.objectKind]?.some(([, id]) => id === command)) {
    if (request.objectHandler) {
      if (await request.objectHandler(command, request.objectTarget, request) === false) {
        throw new Error('The context-menu command could not be completed.');
      }
    } else {
      request.objectTarget.dispatchEvent(new CustomEvent('copal-context-command', {
        bubbles:true, detail:Object.freeze({ command, request }),
      }));
    }
    return;
  }
  if (!restoreSelection(request)) throw new Error('The original editing target changed. Reopen the menu.');
  if (command === 'undo' || command === 'redo' || command === 'select-all') {
    document.execCommand(command === 'select-all' ? 'selectAll' : command);
  } else if (command === 'copy') {
    await clipboardWrite(selectedText(request));
  } else if (command === 'cut') {
    const text = selectedText(request); await clipboardWrite(text);
    if (!isCapturedContextCurrent(request)) throw new Error('The original editing target changed during clipboard access.');
    localDelete(request);
  } else if (command === 'paste' || command === 'paste-plain') {
    const text = await clipboardRead();
    if (!isCapturedContextCurrent(request) || !restoreSelection(request)) throw new Error('The original editing target changed during clipboard access.');
    if (request.selectionRange) setInputSelection(request.target, request.selectionRange.start, request.selectionRange.end, text);
    else document.execCommand('insertText', false, text);
  } else if (command === 'delete') {
    localDelete(request);
  } else if (command === 'spellcheck') {
    const selected = selectedText(request);
    if (!selected) throw new Error('Select text to check first.');
    const service = request.spellingService.service;
    if (typeof service?.checkBatch !== 'function') throw new Error('Local spelling is unavailable.');
    const bounded = selected.slice(0, 32768), words = [];
    let partial = selected.length > bounded.length;
    for (const match of bounded.matchAll(/[\p{L}\p{M}]+(?:['’-][\p{L}\p{M}]+)*/gu)) {
      if (words.length === 256) { partial = true; break; }
      const word = match[0];
      if (new TextEncoder().encode(word).length > 64) { partial = true; continue; }
      // Do not check a token clipped by the explicit character bound.
      if (partial && match.index + word.length === bounded.length) continue;
      words.push(word);
    }
    if (!words.length) throw new Error('No eligible words in the selected text.');
    const results = await service.checkBatch(words.slice(0, 256));
    assertSpellingTarget(request);
    if (!Array.isArray(results) || results.length !== words.length) throw new Error('Local spelling returned an invalid batch.');
    const misspelled = [...new Set(words.filter((_, index) => results[index] === false))];
    const details = misspelled.length ? `Possible spelling errors: ${misspelled.slice(0, 12).join(', ')}${misspelled.length > 12 ? '…' : ''}` : 'No spelling errors found';
    window.uiModule?.showToast?.(`${details}. Selected text, en-US${partial ? '; partial check (bounded selection/256 words)' : ''}.`);
  } else if (command === 'add-word' || command === 'remove-word') {
    const service = request.spellingService.service, word = selectedText(request).trim();
    const action = command === 'add-word' ? 'add' : 'remove';
    if (!/^[\p{L}\p{M}]+(?:['’-][\p{L}\p{M}]+)*$/u.test(word)) throw new Error('Select exactly one word.');
    if (typeof service?.[action] !== 'function') throw new Error('The local spelling dictionary is unavailable.');
    await service[action]([word]);
  } else if (command === 'copy-link-address' && request.link) {
    await clipboardWrite(request.link);
  } else if (command.startsWith('replace-spelling:')) {
    const replacement = command.slice('replace-spelling:'.length);
    if (request.selectionRange) setInputSelection(request.target, request.selectionRange.start, request.selectionRange.end, replacement);
    else if (restoreSelection(request)) document.execCommand('insertText', false, replacement);
  } else if (command === 'open-link' && request.link) {
    window.open(request.link, '_blank', 'noopener,noreferrer');
  }
}

function normalizeMenuItems(items) {
  const seen = new Set(), groups = new Map(), result = [];
  for (const item of items) {
    if (!item?.separator && (!item?.id || !item.label || typeof item.run !== 'function' || seen.has(item.id))) continue;
    if (!item.separator) seen.add(item.id);
    let destination = result;
    if (item.menu) {
      if (!groups.has(item.menu)) {
        const group = { label:String(item.menu), children:[] };
        groups.set(item.menu, group);
        result.push(group);
      }
      destination = groups.get(item.menu).children;
    }
    destination.push(item);
  }
  const trimSeparators = list => list.filter((item, index) => !item.separator
    || index > 0 && !list[index - 1].separator && list.slice(index + 1).some(next => !next.separator));
  for (const group of groups.values()) group.children = trimSeparators(group.children);
  const root = trimSeparators(result.filter(item => !item.children || item.children.length));
  return root.length === 1 && root[0].children ? root[0].children : root;
}

function closeMenu({ restore = true } = {}) {
  const prior = active;
  if (!prior) return;
  active = null;
  clearTimeout(prior.hoverTimer);
  prior.child?.dismiss();
  prior.root.dismiss();
  if (prior.anchor) {
    prior.anchor.setAttribute('aria-expanded', 'false');
    if (prior.previousControls) prior.anchor.setAttribute('aria-controls', prior.previousControls);
    else prior.anchor.removeAttribute('aria-controls');
  }
  if (restore && targetVisible(prior.origin)) prior.origin.focus?.({ preventScroll:true });
}

function menuBounds(state) {
  const bounds = visibleWindowBounds(state.target);
  const inset = Math.min(6, Math.floor(Math.min(bounds.right - bounds.left, bounds.bottom - bounds.top) / 4));
  return { left:bounds.left + inset, top:bounds.top + inset, right:bounds.right - inset, bottom:bounds.bottom - inset };
}

function placePanel(state, panel, opener = null) {
  const { left, right, top, bottom } = menuBounds(state);
  const { element } = panel;
  const owner = state.target.closest('.modal');
  const zIndex = owner ? Number.parseInt(getComputedStyle(owner).zIndex, 10) : NaN;
  if (Number.isSafeInteger(zIndex) && zIndex >= 0 && zIndex < 2147483645) {
    element.style.zIndex = String(zIndex + (opener ? 2 : 1));
  }
  element.style.maxWidth = `${Math.max(1, Math.min(312, right - left))}px`;
  element.style.maxHeight = `${Math.max(1, bottom - top)}px`;
  let x = state.x, y = state.y;
  if (state.anchor) {
    const rect = state.anchor.getBoundingClientRect();
    x = rect.left;
    y = rect.bottom + 4;
    if (y + element.offsetHeight > bottom && rect.top - element.offsetHeight - 4 >= top) {
      y = rect.top - element.offsetHeight - 4;
    }
  }
  if (opener) {
    const rect = state.root.element.getBoundingClientRect();
    const beside = rect.right - 1;
    const opposite = rect.left - element.offsetWidth + 1;
    panel.drill = beside + element.offsetWidth > right && opposite < left;
    if (!panel.drill) {
      const movesLeft = beside + element.offsetWidth > right;
      opener.classList.toggle('opens-left', movesLeft);
      x = movesLeft ? opposite : beside;
      y = opener.getBoundingClientRect().top - 4;
    } else {
      x = rect.left; y = rect.top;
    }
  }
  element.style.left = `${Math.max(left, Math.min(x, right - element.offsetWidth))}px`;
  element.style.top = `${Math.max(top, Math.min(y, bottom - element.offsetHeight))}px`;
}

function closeChild(state, restore = true) {
  clearTimeout(state.hoverTimer);
  const child = state.child;
  if (!child) return;
  state.child = null;
  child.hadFocus = child.element.contains(document.activeElement);
  child.dismiss();
  child.opener.setAttribute('aria-expanded', 'false');
  child.opener.classList.remove('opens-left');
  state.root.element.style.visibility = '';
  if (restore || child.hadFocus) child.opener.focus({ preventScroll:true });
}

function focusRow(panel, edge = 'first') {
  const buttons = panel.rows.filter(row => !row.hidden);
  (buttons[edge === 'second' ? 1 : 0] || panel.element).focus();
}

function openChild(state, opener, entry, focus) {
  clearTimeout(state.hoverTimer);
  if (state.child?.opener === opener) {
    if (focus) focusRow(state.child, state.child.drill ? 'second' : 'first');
    return;
  }
  if (!state.isCurrent()) { closeMenu(); return; }
  closeChild(state, false);
  const panel = makePanel(state, entry.label, entry.children, opener);
  state.child = panel;
  opener.setAttribute('aria-expanded', 'true');
  layoutChild(state);
  if (focus || panel.drill) focusRow(panel, panel.drill ? 'second' : 'first');
}

function layoutChild(state) {
  const panel = state.child;
  if (!panel) return;
  placePanel(state, panel, panel.opener);
  state.root.element.style.visibility = panel.drill ? 'hidden' : '';
  panel.back.hidden = !panel.drill;
  placePanel(state, panel, panel.opener);
}

function repositionMenu() {
  const state = active;
  if (!state) return;
  if (!state.isCurrent()) { closeMenu({ restore:false }); return; }
  placePanel(state, state.root);
  layoutChild(state);
}

function hoverRow(state, button, entry) {
  clearTimeout(state.hoverTimer);
  if (state.child?.opener === button) return;
  state.hoverTimer = setTimeout(() => {
    if (active !== state) return;
    if (entry.children) openChild(state, button, entry, false);
    else closeChild(state, false);
  }, state.child ? 260 : 140);
}

async function invoke(state, entry) {
  if (active !== state || entry.disabled || entry.disabledReason) return;
  try {
    if (!state.isCurrent() || entry.isCurrent?.() === false) {
      closeMenu({ restore:false });
      throw new Error('The original menu target changed. Reopen its menu.');
    }
    closeMenu({ restore:entry.restoreFocus !== false });
    await entry.run();
  } catch (error) {
    window.uiModule?.showToast?.(error?.message || 'The context-menu command could not be completed.');
  }
}

function appendMenuRows(state, panel, items) {
  for (const item of items) {
    if (item.separator) {
      const separator = document.createElement('div');
      separator.className = 'openclank-context-separator';
      separator.setAttribute('role', 'separator');
      panel.element.append(separator);
      continue;
    }
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'openclank-context-item';
    button.tabIndex = -1;
    button.setAttribute('role', item.radio ? 'menuitemradio' : typeof item.checked === 'boolean' ? 'menuitemcheckbox' : 'menuitem');
    if (item.radio || typeof item.checked === 'boolean') button.setAttribute('aria-checked', String(!!item.checked));
    if (item.children) {
      button.setAttribute('aria-haspopup', 'menu');
      button.setAttribute('aria-expanded', 'false');
    } else {
      button.dataset.command = item.id;
    }
    if (item.disabled || item.disabledReason) button.setAttribute('aria-disabled', 'true');
    if (item.disabledReason) button.title = item.disabledReason;
    if (item.destructive) button.classList.add('is-destructive');
    const glyph = document.createElement('span'), label = document.createElement('span'), trailing = document.createElement('span');
    glyph.className = 'openclank-context-glyph';
    glyph.setAttribute('aria-hidden', 'true');
    if (item.checked) glyph.textContent = item.radio ? '●' : '✓';
    else if (item.icon && hasUiIcon(item.icon)) glyph.innerHTML = uiIcon(item.icon, 13);
    else glyph.textContent = item.glyph || '';
    label.className = 'openclank-context-label';
    label.textContent = item.label;
    if (item.dialog && !/[….]$/.test(item.label)) button.classList.add('opens-dialog');
    trailing.className = 'openclank-context-trailing';
    trailing.textContent = item.children ? '›' : item.shortcut || '';
    trailing.setAttribute('aria-hidden', 'true');
    if (item.ariaShortcut) button.setAttribute('aria-keyshortcuts', item.ariaShortcut);
    button.append(glyph, label, trailing);
    button.addEventListener('click', () => item.children
      ? openChild(state, button, item, true) : void invoke(state, item));
    if (!panel.opener) button.addEventListener('pointerenter', event => {
      if (event.pointerType === 'mouse') hoverRow(state, button, item);
    });
    panel.rows.push(button);
    panel.element.append(button);
  }
}

function makePanel(state, label, items, opener = null) {
  const element = document.createElement('div');
  element.className = 'openclank-context-menu';
  if (!opener) element.id = MENU_ID;
  else element.id = `${MENU_ID}-child`;
  element.setAttribute('role', 'menu');
  element.setAttribute('aria-label', label);
  element.tabIndex = -1;
  const panel = { element, opener, rows:[] };
  if (opener) {
    opener.setAttribute('aria-controls', element.id);
    const back = document.createElement('button');
    back.type = 'button';
    back.className = 'openclank-context-item openclank-context-back';
    back.setAttribute('role', 'menuitem');
    back.tabIndex = -1;
    back.textContent = 'Back';
    back.hidden = true;
    back.addEventListener('click', () => closeChild(state));
    panel.back = back;
    panel.rows.push(back);
    element.append(back);
    element.addEventListener('pointerenter', () => clearTimeout(state.hoverTimer));
  }
  appendMenuRows(state, panel, items);
  element.addEventListener('keydown', event => {
    if (event.isComposing || event.keyCode === 229) return;
    const current = document.activeElement;
    const rows = panel.rows.filter(row => !row.hidden);
    const index = rows.indexOf(current);
    if (event.key === 'Tab') { closeMenu(); return; }
    let next;
    if (event.key === 'Escape') {
      if (state.child) closeChild(state);
      else closeMenu();
    } else if (event.key === 'ArrowLeft' && opener) closeChild(state);
    else if (event.key === 'ArrowRight' && current?.hasAttribute('aria-haspopup')) current.click();
    else if (event.key === 'ArrowDown') next = rows[(index + 1) % rows.length];
    else if (event.key === 'ArrowUp') next = rows[(index <= 0 ? rows.length : index) - 1];
    else if (event.key === 'Home') next = rows[0];
    else if (event.key === 'End') next = rows.at(-1);
    else return;
    event.preventDefault();
    event.stopImmediatePropagation();
    next?.focus();
  });
  (state.target.closest('dialog:modal') || document.body).append(element);
  element._dismiss = () => closeMenu();
  const unregister = registerMenuDismiss(opener ? () => closeChild(state) : closeMenu);
  panel.dismiss = () => {
    unregister();
    opener?.removeAttribute('aria-controls');
    element.remove();
  };
  return panel;
}

function showActionMenu({ items, target, anchor, x, y, isCurrent, origin, label, explicit = false }) {
  const entries = normalizeMenuItems(items);
  if (!entries.length || !targetVisible(target) || !isCurrent()) return false;
  closeMenu({ restore:false });
  const state = { target, anchor, x, y, origin, isCurrent, explicit };
  state.root = makePanel(state, label || 'Context actions', entries);
  active = state;
  if (anchor) {
    state.previousControls = anchor.getAttribute('aria-controls');
    anchor.setAttribute('aria-haspopup', 'menu');
    anchor.setAttribute('aria-controls', MENU_ID);
    anchor.setAttribute('aria-expanded', 'true');
  }
  placePanel(state, state.root);
  focusRow(state.root);
  return true;
}

export function openContextActionMenu({ anchor, event, items = [], isCurrent = () => true, label } = {}) {
  initCustomContextMenu();
  if (!anchor || active?.anchor === anchor) {
    if (anchor) closeMenu();
    return false;
  }
  const shown = showActionMenu({ items, target:anchor, anchor, origin:anchor, label, explicit:true,
    isCurrent:() => targetVisible(anchor) && isCurrent() });
  if (shown) event?.preventDefault?.();
  return shown;
}

function legacyObjectItems(request) {
  const items = [];
  for (const [label, id] of OBJECT_COMMANDS[request.objectKind] || []) {
    if (request.objectKind === 'file') {
      const capabilities = fileCapabilities(request.objectTarget);
      const required = { 'open-in-editor':'open', 'open-in-editor-split-right':'open', 'open-in-editor-split-below':'open',
        'reveal-file':'stat', 'rename-file':'rename', 'trash-file':'trash', 'restore-file':'restore' }[id];
      if (required && capabilities && !capabilities.has(required)) continue;
      if (id.startsWith('open-in-editor') && request.objectTarget.dataset.fileOpenEditor !== 'true') continue;
      if (id === 'copy-file-path' && !request.objectTarget.dataset.path) continue;
      if (id === 'move-files' || id === 'copy-files') {
        const transfer = window.__openClankFilesContextCapabilities?.(request.objectTarget, request);
        if (!transfer?.[id === 'move-files' ? 'move' : 'copy']) continue;
      }
      if (request.adapterContext?.selectedKeys?.length > 1 && !['move-files', 'copy-files', 'select-all-files'].includes(id)) continue;
    }
    if (request.objectKind === 'workspace'
      && (!request.objectTarget.dataset.workspaceId || request.objectTarget.dataset.workspaceAvailable !== 'true')) continue;
    const disabled = id === 'copy-image-bytes' && (!navigator.clipboard?.write || typeof window.ClipboardItem !== 'function');
    items.push({ id, label, disabled });
  }
  return items;
}

function renderMenu(request) {
  if (!isCapturedContextCurrent(request)) return false;
  if (!request.adapter && !request.objectTarget && !nativeTextTarget(request.target)
    && request.rawTarget.closest(NATIVE_CONTROLS + ',img')) return false;
  let supplied = request.adapter?.commands?.(request) || [];
  if (request.adapter?.projectCommands) supplied = request.adapter.projectCommands(request, supplied);
  const suppliedIds = new Set(supplied.map(item => item?.id));
  let basic = request.editable
    ? basicEditingCommands(request).filter(item => !['delete', 'paste-plain'].includes(item.id) && !suppliedIds.has(item.id))
    : (hasSelectedText(request) && !suppliedIds.has('copy') ? [{ id:'copy', label:'Copy' }] : []);
  if (typeof request.adapter?.editingCommands === 'function') basic = request.adapter.editingCommands(request, basic);
  else if (request.adapter?.editingCommands === false) basic = [];
  const toAction = item => {
    const shortcut = item.shortcut ?? editingShortcutHints[item.id];
    return { ...item, shortcut, run:item.run || (() => executeCapturedContextCommand(item.id, request)) };
  };
  const items = [...basic, ...supplied, ...legacyObjectItems(request)].filter(Boolean).map(toAction);
  let spellingWord = '';
  if (request.editable && hasSelectedText(request) && request.adapter?.editingCommands !== false
    && (!request.adapter || request.adapter.spellingEligible?.(request) === true)) {
    const word = selectedText(request).trim();
    if (/^[\p{L}][\p{L}'-]*$/u.test(word)) {
      spellingWord = word;
      const service = request.spellingService.service;
      if (typeof service?.add === 'function') items.push(toAction({ id:'add-word', label:'Add word to dictionary', menu:'Spelling' }));
      if (typeof service?.remove === 'function') items.push(toAction({ id:'remove-word', label:'Remove word from dictionary', menu:'Spelling' }));
    }
  }
  const shown = showActionMenu({ items, target:request.target, x:request.x, y:request.y,
    origin:request.adapter?.focusOrigin?.(request)
      || (request.modality === 'keyboard' ? request.rawTarget : request.editable ? request.target : document.activeElement),
    isCurrent:() => isCapturedContextCurrent(request) });
  const state = active;
  const spelling = request.spellingService?.service;
  if (shown && spellingWord && typeof spelling?.suggest === 'function') {
    Promise.resolve().then(() => spelling.suggest(spellingWord)).then(suggestions => {
      if (active !== state || state.child || !state.isCurrent() || !spellingServiceCurrent(request)) return;
      const replacements = [...new Set(Array.isArray(suggestions) ? suggestions.map(String) : [])]
        .filter(suggestion => suggestion && suggestion !== spellingWord).slice(0, 5);
      if (replacements.length) {
        appendMenuRows(state, state.root, [{ separator:true }, ...replacements.map(suggestion => toAction({
          id:`replace-spelling:${suggestion}`, label:`Replace with “${suggestion}”`,
        }))]);
        placePanel(state, state.root);
      }
    }).catch(() => {});
  }
  return shown;
}

function onContextMenu(event) {
  if (!enabled() || event.defaultPrevented) return;
  if (event.target.closest?.('.openclank-context-menu')) { event.preventDefault(); return; }
  if (heldTouch?.target.contains(event.target) && Date.now() - heldTouch.time < 900) { event.preventDefault(); return; }
  if (event.pointerType === 'touch' && (event.target.closest?.('[draggable="true"]') || editableTarget(event.target))) return;
  closeMenu({ restore:false });
  if (renderMenu(capture(event))) {
    if (event.pointerType === 'touch') heldTouch = { target:event.target, id:event.pointerId, time:Date.now() };
    event.preventDefault();
  }
}

function onKeyboardContextMenu(event) {
  if (!enabled() || event.defaultPrevented || event.isComposing || event.keyCode === 229 || event.repeat
    || event.ctrlKey || event.altKey || event.metaKey) return;
  if (!((event.key === 'F10' && event.shiftKey) || (event.key === 'ContextMenu' && !event.shiftKey))) return;
  const target = document.activeElement;
  if (!(target instanceof Element) || target === document.body || target === document.documentElement
    || target.closest('.openclank-context-menu')) return;
  if (active) closeMenu({ restore:false });
  if (renderMenu(capture({ target, detail:0 }))) event.preventDefault();
}

export function initCustomContextMenu() {
  if (window.__openClankContextMenuInitialized) return;
  window.__openClankContextMenuInitialized = true;
  const cancelLongPress = () => {
    clearTimeout(longPressTimer);
    longPressTimer = null;
    longPressOrigin = null;
  };
  document.addEventListener('contextmenu', event => {
    cancelLongPress();
    onContextMenu(event);
  });
  document.addEventListener('pointerdown', event => {
    cancelLongPress();
    heldTouch = null;
    const target = event.target instanceof Element ? event.target : null;
    if (!target || target.closest('.openclank-context-menu')) return;
    if (active && !active.anchor?.contains(target)) closeMenu({ restore:false });
    if (!enabled() || event.pointerType !== 'touch' || !event.isPrimary
      || target.closest('[draggable="true"],' + PROTECTED_TARGETS) || editableTarget(target)) return;
    longPressOrigin = { id:event.pointerId, x:event.clientX, y:event.clientY };
    longPressTimer = setTimeout(() => {
      cancelLongPress();
      if (!target.isConnected) return;
      if (renderMenu(capture({ target, clientX:event.clientX, clientY:event.clientY, detail:1, pointerType:'touch' }))) {
        heldTouch = { target, id:event.pointerId, time:Date.now() };
      }
    }, 550);
  }, true);
  document.addEventListener('pointermove', event => {
    if (event.pointerId !== longPressOrigin?.id) return;
    if (Math.hypot(event.clientX - longPressOrigin.x, event.clientY - longPressOrigin.y) >= 8) cancelLongPress();
  }, { capture:true, passive:true });
  document.addEventListener('scroll', event => {
    cancelLongPress();
    const state = active;
    if (!state) return;
    if (event.target === state.root.element && state.child) {
      const rootRect = state.root.element.getBoundingClientRect();
      const openerRect = state.child.opener.getBoundingClientRect();
      if (openerRect.bottom <= rootRect.top || openerRect.top >= rootRect.bottom) closeChild(state);
      else if (!state.child.drill) placePanel(state, state.child, state.child.opener);
    }
  }, { capture:true, passive:true });
  document.addEventListener('pointerup', event => {
    cancelLongPress();
    if (event.pointerId === heldTouch?.id && Date.now() - heldTouch.time < 900) event.preventDefault();
  }, true);
  document.addEventListener('pointercancel', cancelLongPress, true);
  document.addEventListener('click', event => {
    if (heldTouch && event.detail !== 0 && Date.now() - heldTouch.time < 900) {
      heldTouch = null;
      event.preventDefault(); event.stopImmediatePropagation();
      return;
    }
    if (active && !event.target.closest?.('.openclank-context-menu') && !active.anchor?.contains(event.target)) {
      closeMenu({ restore:false });
    }
  }, true);
  window.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || event.defaultPrevented || event.isComposing || event.keyCode === 229
      || !active || !event.target?.closest?.('.openclank-context-menu')) return;
    if (dismissTopMenu()) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  document.addEventListener('keydown', event => {
    cancelLongPress();
    onKeyboardContextMenu(event);
    if (event.key === 'Escape' && active && !event.defaultPrevented && !event.isComposing && event.keyCode !== 229) {
      event.preventDefault(); event.stopImmediatePropagation();
      if (active.child) closeChild(active);
      else closeMenu();
    }
  });
  window.addEventListener('resize', repositionMenu);
  window.visualViewport?.addEventListener('resize', repositionMenu);
  window.addEventListener('odysseus-context-menu-changed', () => { if (active && !active.explicit && !enabled()) closeMenu(); });
  window.openClankContextMenu = Object.freeze({ enabled, close:closeMenu, registerAdapter, openContextActionMenu });
}
export function registerAdapter(element, adapter) {
  if (!(element instanceof Element) || !adapter || typeof adapter.execute !== 'function') return () => {};
  adapters.set(element, adapter);
  return () => { if (adapters.get(element) === adapter) adapters.delete(element); };
}

// Shared CodeMirror command semantics. Keeping this beside the menu prevents
// individual Editor leaves from falling back to document.execCommand.
function hasCommentSource(request) {
  const captured = request?.adapterContext;
  if (!captured) return false;
  return !!captured.commentSourceRange;
}

// Rendered comments carry bounded source offsets on the actual DOM hit.
function mountedCommentSourceRange(widget) {
  if (!widget) return null;
  const from = Number(widget.dataset.commentSourceFrom);
  const to = Number(widget.dataset.commentSourceTo);
  return Number.isFinite(from) && Number.isFinite(to) && to >= from ? Object.freeze({ from, to }) : null;
}

function plainSelectionMatches(target, range) {
  if (!range) return true;
  const current = selectionSnapshot(target)?.range;
  return current?.startContainer === range.startContainer && current?.endContainer === range.endContainer
    && current?.startOffset === range.startOffset && current?.endOffset === range.endOffset
    && current?.toString() === range.toString();
}

function capturedRenderObject(rawTarget, widget) {
  const node = rawTarget?.closest?.('img, a[href]');
  if (!widget?.contains(node)) return null;
  const image = node.matches('img');
  const url = image ? node.currentSrc || node.src : node.href;
  if (!/^(https?:|blob:|data:image\/)/i.test(url) || !image && !/^https?:/i.test(url)) return null;
  const current = () => targetVisible(node) && widget.contains(node)
    && url === (image ? node.currentSrc || node.src : node.href);
  const open = () => window.open(url, '_blank', 'noopener,noreferrer');
  return { current, items:[
    { id:image ? 'open-image' : 'open-link', label:image ? 'Open image' : 'Open link in new tab', run:open },
    { id:image ? 'copy-image-address' : 'copy-link-address', label:image ? 'Copy image address' : 'Copy link address',
      disabled:!navigator.clipboard?.writeText, run:() => clipboardWrite(url) },
  ] };
}

export function createCodeMirrorContextAdapter(editor, identity = {}) {
  const readIdentity = (name, fallback = null) => {
    const value = identity[name];
    return typeof value === 'function' ? value() : value ?? fallback;
  };
  const isReadOnly = () => !!readIdentity('readOnly', false)
    || editor.view.contentDOM?.getAttribute('contenteditable') === 'false';
  const languageIdentity = () => { const language = editor.getStatus?.()?.language; return language ? `${language.id}|${language.mode || ''}|${language.override || ''}` : ''; };
  const widgetAt = target => {
    const widget = target?.closest?.('.cm-rich-comment-widget,.cm-md-inline-preview-widget,.cm-md-embed-widget,[contenteditable="false"]');
    return widget && widget !== editor.view.contentDOM && editor.view.contentDOM.contains(widget) ? widget : null;
  };
  const contentHit = (target, event) => {
    const content = editor.view?.contentDOM;
    if (!content?.contains(target) || target.closest('.cm-editor') !== content.closest('.cm-editor')) return false;
    const control = target.closest('input,textarea,select,button,a[href],summary,iframe,video,audio,[role="button"],[contenteditable="true"]');
    const widget = widgetAt(target);
    if (control && control !== content && !capturedRenderObject(target, widget)) return 'native';
    if (event?.pointerType === 'touch' && !widget) return 'native';
    return true;
  };
  const isCurrent = (request) => {
    const captured = request?.adapterContext;
    const view = editor.view;
    const selection = view?.state?.selection;
    return !!captured && !!selection && view.dom.isConnected
      && (!captured.widget || targetVisible(captured.widget) && plainSelectionMatches(captured.widget, captured.widgetSelection))
      && (!captured.object || captured.object.current())
      && view.state.doc === captured.documentIdentity
      && isReadOnly() === captured.readOnly
      && selection.mainIndex === captured.mainIndex
      && selection.ranges.length === captured.ranges.length
      && selection.ranges.every((range, index) => range.anchor === captured.ranges[index].anchor && range.head === captured.ranges[index].head)
      && captured.bufferIdentity === readIdentity('bufferIdentity', editor)
      && captured.revision === readIdentity('revision')
      && captured.scope === readIdentity('scope') && captured.languageIdentity === languageIdentity();
  };
  return {
    hitTest:contentHit,
    isCurrent,
    editingCommands:(request, items) => {
      const captured = request.adapterContext;
      if (captured?.widget) return captured.widgetSelection && !captured.object ? [{ id:'copy', label:'Copy' }] : [];
      const status = editor.getStatus?.();
      const itemCurrent = item => {
        if (!captured?.atSelection && !['undo','redo','select-all'].includes(item.id)) return false;
        if (item.id === 'undo' && status?.canUndo === false || item.id === 'redo' && status?.canRedo === false) return false;
        if (['copy','cut'].includes(item.id) && !navigator.clipboard?.writeText) return false;
        if (item.id === 'paste' && !navigator.clipboard?.readText) return false;
        return !item.disabledReason;
      };
      return items.filter(itemCurrent);
    },
    projectCommands:(request, items) => {
      const captured = request.adapterContext;
      if (!captured) return [];
      if (captured.widget) return captured.object?.items || (captured.commentSourceRange
        ? [{ id:'see-source', label:'See source' }] : []);
      const markdown = /^(markdown|mdx)$/.test(captured.languageId);
      const plain = /^(plain|plaintext|text)$/.test(captured.languageId);
      return items.filter(item => {
        if (!item || item.separator) return true;
        if (item.disabled || item.disabledReason) return false;
        if (!captured.atSelection && item.id !== 'see-source') {
          return item.id === 'duplicate-line' && captured.atMainLine;
        }
        if (item.id.startsWith('format-')) return markdown;
        if (item.id === 'spellcheck' || item.id === 'retry-spelling') return markdown || plain || !!captured.commentSourceRange;
        return true;
      });
    },
    restore:(request) => { if (!isCurrent(request)) return false; editor.focus(); return true; },
    commands: (request) => {
      // Selection-only match/transform actions are disabled with an empty
      // selection; line/cursor commands (duplicate, indent) stay usable.
      const hasSelection = hasSelectedText(request);
      const readOnly = isReadOnly();
      const readOnlySafe = ['select-next-match', 'select-all-matches', 'see-source', 'add-cursor-above', 'add-cursor-below', 'collapse-selections', 'spellcheck', 'retry-spelling'];
      return [
        { id:'select-next-match', label:'Select next match', disabled:typeof editor.selectNextMatch !== 'function' },
        { id:'select-all-matches', label:'Select all matches', disabled:!request.adapterContext?.query, disabledReason:request.adapterContext?.query ? '' : 'Select text in the primary range first.' },
        { id:'duplicate-line', label:'Duplicate line' },
        { id:'indent', label:'Indent selection' },
        { id:'format-bold', label:'Bold selection', disabled:!hasSelection },
        { id:'format-italic', label:'Italic selection', disabled:!hasSelection },
        { id:'format-code', label:'Inline code selection', disabled:!hasSelection },
        { id:'see-source', label:'See source', disabled:!hasCommentSource(request), disabledReason:hasCommentSource(request) ? '' : 'No captured documentation region is available.' },
        { id:'add-cursor-above', label:'Add cursor above', disabled:typeof editor.runCommand !== 'function' },
        { id:'add-cursor-below', label:'Add cursor below', disabled:typeof editor.runCommand !== 'function' },
        { id:'collapse-selections', label:'Keep primary selection', disabled:(request.adapterContext?.ranges?.length || 0) < 2 },
        { id:'spellcheck', label:'Check spelling locally', disabled:typeof editor.checkSpelling !== 'function' || typeof window.openClankSpelling?.checkBatch !== 'function',
          disabledReason:typeof editor.checkSpelling !== 'function' ? 'Local spelling is not available in this editor yet.' : typeof window.openClankSpelling?.checkBatch !== 'function' ? 'The local spelling service is unavailable.' : '' },
        ...(canRetrySpelling(request) ? [{ id:'retry-spelling', label:'Retry spelling' }] : []),
        { id:'insert-template', label:'Insert template', disabled:typeof identity.onCommand !== 'function' },
        { id:'new-from-template', label:'New from template', disabled:typeof identity.onCommand !== 'function' },
      ].map((item) => readOnly && !readOnlySafe.includes(item.id) ? { ...item, disabled:true, disabledReason:'This editor is read-only.' } : item);
    },
    capture: (rawTarget, event = {}) => {
      const widget = widgetAt(rawTarget);
      const rendered = capturedRenderObject(rawTarget, widget);
      const widgetSelection = widget && !rendered ? selectionSnapshot(widget)?.range || null : null;
      const selection = editor.getSelection?.() || { ranges:[{ anchor:editor.view.state.selection.main.anchor, head:editor.view.state.selection.main.head }], mainIndex:0 };
      const ranges = selection.ranges.map((range) => ({ anchor:range.anchor, head:range.head }));
      const documentIdentity = editor.view.state.doc;
      const rangeText = range => documentIdentity.sliceString(Math.min(range.anchor, range.head), Math.max(range.anchor, range.head));
      const mainRange = ranges[selection.mainIndex] || ranges[0];
      const query = mainRange && mainRange.anchor !== mainRange.head ? rangeText(mainRange) : undefined;
      const text = ranges.filter(range => range.anchor !== range.head).map(rangeText).join('\n');
      const commentWidget = rawTarget?.closest?.('.cm-rich-comment-widget');
      const position = isKeyboardInvocation(event) ? mainRange?.head
        : Number.isFinite(event.clientX) && Number.isFinite(event.clientY)
          ? editor.view.posAtCoords?.({ x:event.clientX, y:event.clientY }) : mainRange?.head;
      const atSelection = position != null && ranges.some(range =>
        position >= Math.min(range.anchor, range.head) && position <= Math.max(range.anchor, range.head));
      const atMainLine = position != null && mainRange?.anchor === mainRange?.head && ranges.length === 1
        && typeof documentIdentity.lineAt === 'function'
        && documentIdentity.lineAt(position).number === documentIdentity.lineAt(mainRange.head).number;
      const commentSourceRange = commentWidget ? mountedCommentSourceRange(commentWidget) : widget || position == null ? null
        : (identity.commentSourceRange?.([{ anchor:position, head:position }]) || editor.peekCommentSourceRange?.(position) || null);
      return Object.freeze({
        ranges, mainIndex:selection.mainIndex, text, query, widget, widgetSelection, object:rendered, commentSourceRange,
        atSelection, atMainLine,
        languageId:editor.getStatus?.()?.language?.id || '',
        documentIdentity, documentLength:documentIdentity.length,
        spelling:editor.captureSpelling?.(), languageIdentity:languageIdentity(),
        readOnly:isReadOnly(),
        bufferIdentity:readIdentity('bufferIdentity', editor), revision:readIdentity('revision'), scope:readIdentity('scope'),
      });
    },
    spellingEligible:(request) => !request.adapterContext?.widget && request.adapterContext?.atSelection
      && request.adapterContext?.ranges?.length === 1
      && /^[\p{L}][\p{L}'-]*$/u.test(selectedText(request).trim())
      && editor.isSpellingSelectionEligible?.(request.adapterContext?.spelling) === true,
    execute: async (command, request) => {
      const captured = request.adapterContext; const view = editor.view;
      if (!isCurrent(request)) throw new Error('The original editing target changed before the command completed.');
      if (captured.widget && command !== 'see-source') {
        if (command === 'copy' && captured.widgetSelection) { await clipboardWrite(captured.widgetSelection.toString()); return true; }
        return false;
      }
      const replace = (text) => { editor.replaceSelections?.(String(text ?? '')) || editor.replaceRange?.(captured.ranges[captured.mainIndex]?.anchor || 0, captured.ranges[captured.mainIndex]?.head || 0, String(text ?? '')); };
      const query = captured.query;
      const readOnly = isReadOnly();
      const readOnlySafe = ['copy', 'select-all', 'select-next-match', 'select-all-matches', 'add-cursor-above', 'add-cursor-below', 'collapse-selections', 'see-source', 'open-link', 'add-word', 'remove-word', 'spellcheck'];
      if (readOnly && !readOnlySafe.includes(command)) throw new Error('This editor is read-only.');
      if (command === 'undo') { editor.undo(); editor.focus(); return; }
      if (command === 'redo') { editor.redo(); editor.focus(); return; }
      if (command === 'select-all') { editor.setSelection?.({ ranges:[{ anchor:0, head:view.state.doc.length }], mainIndex:0 }); return; }
      if (command === 'select-next-match') { editor.selectNextMatch?.(query); editor.focus(); return; }
      if (command === 'select-all-matches') { if (query !== undefined) editor.selectAllMatches?.(query); return; }
      if (['add-cursor-above', 'add-cursor-below', 'collapse-selections'].includes(command)) { editor.runCommand?.(command); editor.focus(); return; }
      if (command === 'duplicate-line') { editor.duplicateLines?.(); return; }
      if (command === 'indent') { editor.runCommand?.('indent'); return; }
      if (command === 'format-bold') { editor.formatSelections?.('**'); return; }
      if (command === 'format-italic') { editor.formatSelections?.('*'); return; }
      if (command === 'format-code') { editor.formatSelections?.('`'); return; }
      // Range-aware See source: reveal the documentation region under the
      // captured selection while preserving the rest of the editing state.
      if (command === 'see-source') {
        const region = captured.commentSourceRange;
        if (!region) return false;
        editor.revealCommentSource?.(region.from, region.to);
        return true;
      }
      if (command === 'copy' || command === 'cut') {
        if (!hasSelectedText(request)) return;
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
        await navigator.clipboard.writeText(captured.text);
        if (!isCurrent(request)) throw new Error('The original editing target changed before the command completed.');
        if (command === 'cut') replace(''); return;
      }
      if (command === 'paste' || command === 'paste-plain') {
        if (!navigator.clipboard?.readText) throw new Error('Clipboard reading is unavailable in this browser.');
        const text = await navigator.clipboard.readText();
        if (!isCurrent(request)) throw new Error('The original editing target changed before the command completed.');
        editor.pasteText?.(text) || replace(text); return;
      }
      if (command === 'delete') { if (hasSelectedText(request)) replace(''); return; }
      if (command === 'spellcheck') {
        if (typeof editor.checkSpelling !== 'function' || !window.openClankSpelling) throw new Error('Local spelling is unavailable in this editor.');
        const result = await editor.checkSpelling(request.spellingService.service, captured.spelling);
        if (isCurrent(request) && spellingServiceCurrent(request) && result?.state !== 'stale') window.uiModule?.showToast?.(result?.message || 'Local spelling check completed.');
        return;
      }
      if ((command.startsWith('replace-spelling:') || command === 'add-word' || command === 'remove-word')
        && (captured.ranges.length !== 1 || !/^[\p{L}][\p{L}'-]*$/u.test(captured.text.trim())
          || editor.isSpellingSelectionEligible?.(captured.spelling) !== true)) {
        throw new Error('Select one eligible prose word and reopen its spelling menu.');
      }
      if (command.startsWith('replace-spelling:')) { replace(command.slice('replace-spelling:'.length)); return true; }
      if (command === 'add-word' || command === 'remove-word') {
        const words = captured.text.split(/[^\p{L}'-]+/u).filter(Boolean);
        const action = command === 'add-word' ? 'add' : 'remove';
        if (typeof request.spellingService?.service?.[action] !== 'function') throw new Error('The local spelling dictionary is unavailable.');
        if (words.length) await request.spellingService.service[action](words);
        return;
      }
      if (command === 'open-link' && request.link) { window.open(request.link, '_blank', 'noopener,noreferrer'); return; }
      // Template actions are workspace commands. The Editor owner registers a
      // handler through the adapter identity so the menu never reaches into
      // another window's singleton.
      if (command === 'insert-template' || command === 'new-from-template') {
        const handler = identity.onCommand;
        if (typeof handler === 'function') return Boolean(await handler(command, request));
        return false;
      }
      // Object commands belong to the owner of the captured rendered node
      // (for example an image or link decoration inside this editor).
      return false;
    },
  };
}
export { STORAGE_KEY as CUSTOM_CONTEXT_MENU_STORAGE_KEY };
