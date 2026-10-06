import { visibleWindowBounds } from './windowResize.js';
import { registerMenuDismiss } from './escMenuStack.js';

// The Copal menu owns the editing interaction in every browser. It keeps the
// target and its selection in a short lived context so opening the menu cannot
// redirect a command to whichever window receives focus later.

const STORAGE_KEY = 'odysseus-custom-context-menu';
const MENU_ID = 'openclank-context-menu';
let active = null;
let unregisterMenuDismiss = () => {};
let longPressTimer = null;
let longPressOrigin = null;
const adapters = new WeakMap();
const OBJECT_COMMANDS = Object.freeze({
  image:[['Open image', 'open-image'], ['Copy image address', 'copy-image-address'], ['Copy image', 'copy-image-bytes']],
  link:[['Open link in new tab', 'open-link'], ['Copy link address', 'copy-link-address']],
  file:[['Open in Editor', 'open-in-editor'], ['Open right', 'open-in-editor-split-right'], ['Open below', 'open-in-editor-split-below'], ['Select all files', 'select-all-files'], ['Copy file path', 'copy-file-path'], ['Reveal in Files', 'reveal-file'], ['Rename', 'rename-file'], ['Move', 'move-files'], ['Copy', 'copy-files'], ['Move to trash', 'trash-file'], ['Restore', 'restore-file']],
  workspace:[['Open Workspace Hexes', 'open-workspace-hexes']],
  task:[['Toggle task', 'toggle-task'], ['Open task note', 'open-task']],
  event:[['Edit event', 'edit-event']],
  track:[['Edit track', 'edit-track']],
  graph:[['Open graph node', 'open-graph-node']],
  treehouse:[['Open TreeHouse item', 'open-treehouse-item']],
  'base-cell':[['Edit cell', 'edit-sheet-cell'], ['Clear cell', 'clear-sheet-cell'], ['Copy cell', 'copy-sheet-cell'], ['Open source note', 'open-base-document']],
  table:[['Insert row above', 'table-insert-row-above'], ['Insert row below', 'table-insert-row-below'], ['Delete row', 'table-delete-row'], ['Insert column left', 'table-insert-column-left'], ['Insert column right', 'table-insert-column-right'], ['Delete column', 'table-delete-column']],
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

function findAdapter(target) {
  let current = target;
  while (current && current instanceof Element) {
    const adapter = adapters.get(current);
    if (adapter) return { element: current, adapter };
    current = current.parentElement;
  }
  return null;
}

function objectTarget(target) {
  const explicit = target?.closest?.('[data-copal-context-object]');
  if (explicit) return { target:explicit, kind:String(explicit.dataset.copalContextObject || '') };
  if (target?.closest?.('img')) return { target:target.closest('img'), kind:'image' };
  if (target?.closest?.('.copal-event,[data-event-id]')) return { target:target.closest('.copal-event,[data-event-id]'), kind:'event' };
  if (target?.closest?.('.copal-track,[data-track-id]')) return { target:target.closest('.copal-track,[data-track-id]'), kind:'track' };
  if (target?.closest?.('.copal-task-row,[data-task-id]')) return { target:target.closest('.copal-task-row,[data-task-id]'), kind:'task' };
  if (target?.closest?.('.copal-graph-node,[data-graph-id]')) return { target:target.closest('.copal-graph-node,[data-graph-id]'), kind:'graph' };
  if (target?.closest?.('[class*="copal-treehouse-"]')) return { target:target.closest('[class*="copal-treehouse-"]'), kind:'treehouse' };
  if (target?.closest?.('.copal-file-row,[data-document-id]')) return { target:target.closest('.copal-file-row,[data-document-id]'), kind:'file' };
  return null;
}

function selectionSnapshot(target) {
  if (target && typeof target.selectionStart === 'number') {
    return { start: target.selectionStart, end: target.selectionEnd ?? target.selectionStart };
  }
  const selection = window.getSelection?.();
  if (!selection || !selection.rangeCount || !target?.contains?.(selection.anchorNode)) return null;
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
  const ranges = request.adapterContext?.ranges;
  // Separators used to format multi-range clipboard text are not selections.
  return Array.isArray(ranges) ? ranges.some(range => range.anchor !== range.head) : !!selectedText(request);
}

function fileCapabilities(target) {
  if (!target?.dataset || !Object.prototype.hasOwnProperty.call(target.dataset, 'fileCapabilities')) return null;
  const raw = target.dataset.fileCapabilities || '';
  return new Set(raw.split(/[\s,]+/).map((value) => value.trim()).filter(Boolean));
}

function objectHandlers(kind) {
  const handlers = [];
  if (kind === 'file' && typeof window.__openClankFilesContextCommand === 'function') handlers.push(window.__openClankFilesContextCommand);
  if (kind === 'table' && typeof window.__openClankTableContextCommand === 'function') handlers.push(window.__openClankTableContextCommand);
  if (typeof window.__openClankCopalContextCommand === 'function') handlers.push(window.__openClankCopalContextCommand);
  if (kind !== 'file' && kind !== 'table' && typeof window.__openClankFilesContextCommand === 'function') handlers.push(window.__openClankFilesContextCommand);
  return handlers;
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
function positionContextMenu(menu, request) {
  const bounds = visibleWindowBounds(request.target);
  const left = bounds.left + 8, top = bounds.top + 8;
  const right = Math.max(left, bounds.right - 8), bottom = Math.max(top, bounds.bottom - 8);
  menu.style.maxWidth = `${Math.max(1, right - left)}px`;
  menu.style.maxHeight = `${Math.max(1, bottom - top)}px`;
  menu.style.left = `${Math.max(left, Math.min(request.x, right - menu.offsetWidth))}px`;
  menu.style.top = `${Math.max(top, Math.min(request.y, bottom - menu.offsetHeight))}px`;
}

function capture(event) {
  // Chromium dispatches the keyboard Context Menu key at body in some
  // layouts. The focused editor is the only stable target in that case.
  const eventTarget = event.target instanceof Element ? event.target : null;
  const rawTarget = event.detail === 0 && (!eventTarget || eventTarget === document.body)
    ? (document.activeElement instanceof Element ? document.activeElement : document.body)
    : (eventTarget || document.body);
  const target = editableTarget(rawTarget) || rawTarget;
  const registered = findAdapter(rawTarget);
  const object = objectTarget(rawTarget);
  const range = selectionSnapshot(target);
  const selectionRange = range?.start != null ? range : null;
  const domRange = range?.range || null;
  const selection = window.getSelection?.()?.toString?.() || '';
  const text = selectedText({ target, selectionRange, selection });
  const rect = target.getBoundingClientRect?.();
  const request = {
    requestId: `ctx-${Date.now()}-${Math.random().toString(36).slice(2)}`,
    x: event.clientX || rect?.left || 8, y: event.clientY || rect?.bottom || 8,
    target, editable: !!editableTarget(rawTarget),
    // A mounted adapter owns its revision. Never materialize its document DOM.
    value: registered ? null : typeof target.value === 'string' ? target.value : (requestEditableText(target)),
    domSelection:domRange ? Object.freeze({ start:domRange.startContainer, startOffset:domRange.startOffset, end:domRange.endContainer, endOffset:domRange.endOffset }) : null,
    selection: text, selectionRange, domRange, spellingService:spellingServiceSnapshot(),
    link: rawTarget.closest?.('a')?.href || '',
    modality: event.detail === 0 ? 'keyboard' : 'pointer',
    adapter: registered?.adapter || null,
    adapterElement: registered?.element || null,
    objectTarget: object?.target || null,
    objectKind: object?.kind || (rawTarget.closest?.('a') ? 'link' : ''),
  };
  if (registered?.adapter?.capture) {
    try { request.adapterContext = registered.adapter.capture(rawTarget, event) || null; } catch (_) { request.adapterContext = null; }
  }
  if (object?.kind === 'file' && typeof window.__openClankFilesContextCapture === 'function') {
    try { request.adapterContext = window.__openClankFilesContextCapture(object.target, event) || request.adapterContext; } catch (_) {}
  }
  return Object.freeze(request);
}

function closeMenu({ restore = true } = {}) {
  removeMenu();
  const prior = active; active = null;
  if (restore) restoreSelection(prior || {}, { focus:true });
}

function removeMenu() {
  unregisterMenuDismiss(); unregisterMenuDismiss = () => {};
  document.getElementById(MENU_ID)?.remove();
}

function menuItem(label, command, { disabled = false } = {}) {
  const button = document.createElement('button');
  button.type = 'button'; button.className = 'openclank-context-item';
  button.textContent = label; button.disabled = disabled; button.dataset.command = command;
  return button;
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
  if (!request?.target?.isConnected || request.target.closest('.hidden,[hidden],.modal-minimized') || !request.target.getClientRects().length) return false;
  if (!!editableTarget(request.target) !== request.editable) return false;
  if (request.adapter) {
    if (adapters.get(request.adapterElement) !== request.adapter) return false;
    return typeof request.adapter.isCurrent === 'function' && request.adapter.isCurrent(request);
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

export function capturedEditingCommands(request) {
  const selection = hasSelectedText(request);
  const editable = request.editable;
  const capturedAdapter = !request.adapter || typeof request.adapter.isCurrent === 'function';
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
  if (!capturedAdapter) return basic.map((item) => ({ ...item, disabledReason:'Text commands are unavailable on this surface.' }));
  for (const item of request.adapter?.commands?.(request) || []) {
    const selectionOnly = ['select-all-matches', 'format-bold', 'format-italic', 'format-code'].includes(item.id);
    const readOnlySafe = ['select-next-match', 'select-all-matches', 'see-source', 'collapse-selections', 'add-cursor-above', 'add-cursor-below', 'spellcheck', 'retry-spelling'].includes(item.id);
    basic.push({ ...item, disabledReason:item.disabledReason || (!editable && !readOnlySafe ? reason : '')
      || (item.disabled ? (selectionOnly && !selection ? 'Select text first.' : 'Unavailable in this editor.') : '') });
  }
  return basic;
}

export async function executeCapturedContextCommand(command, request) {
  if (!request) throw new Error('No captured command target.');
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
    if (handled !== false) return;
  }
  const objectCommands = OBJECT_COMMANDS[request.objectKind] || [];
  if ((command === 'copy-link-address' || command === 'open-link') && request.link) {
    if (command === 'copy-link-address') await clipboardWrite(request.link);
    else window.open(request.link, '_blank', 'noopener,noreferrer');
    return;
  }
  // Keep the object captured at menu-open time even when its owning view
  // refreshes while the menu is open. Its immutable dataset identifies the
  // original resource; command owners still resolve current state and the
  // server rechecks authorization before any mutation.
  if (objectCommands.some(([, id]) => id === command) && request.objectTarget) {
    let handled = false;
    for (const handler of objectHandlers(request.objectKind)) {
      if (await handler(command, request.objectTarget, request)) { handled = true; break; }
    }
    if (!handled) {
      request.objectTarget.dispatchEvent(new CustomEvent('copal-context-command', { bubbles:true, detail:Object.freeze({ command, request }) }));
    }
    return;
  }
  if (!isCapturedContextCurrent(request) || !restoreSelection(request)) throw new Error('The original editing target changed. Reopen the menu.');
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

async function invoke(command) {
  const request = active;
  if (!request) return;
  closeMenu({ restore:false });
  try { await executeCapturedContextCommand(command, request); }
  catch (error) { window.uiModule?.showToast?.(error?.message || 'The context-menu command could not be completed.'); }
}

function renderMenu(request) {
  removeMenu();
  const menu = document.createElement('div');
  menu.id = MENU_ID; menu.className = 'openclank-context-menu';
  menu.setAttribute('role', 'menu'); menu.tabIndex = -1;
  const hasSelection = hasSelectedText(request);
  // Editing actions are meaningful only on a real editable control. Folder,
  // tab, image and background menus are assembled from their typed owners.
  const commands = request.editable ? [
    ['Undo', 'undo', !request.editable], ['Redo', 'redo', !request.editable],
    ['Cut', 'cut', !request.editable || !hasSelection], ['Copy', 'copy', !hasSelection],
    ['Paste', 'paste', !request.editable], ['Paste as plain text', 'paste-plain', !request.editable],
    ...(!request.adapter ? [['Check selected spelling locally', 'spellcheck', !hasSelection || typeof request.spellingService?.service?.checkBatch !== 'function'], ...(canRetrySpelling(request) ? [['Retry spelling', 'retry-spelling', false]] : [])] : []), ['Delete', 'delete', !request.editable || !hasSelection],
    // Files owns Select all for its active column; the generic command would
    // select the document body and is therefore disabled on a Files row.
    ['Select all', 'select-all', request.objectKind === 'file' || !request.target],
  ] : (request.adapter && typeof request.adapter.isCurrent === 'function' ? [['Copy', 'copy', !hasSelection], ['Select all', 'select-all', false]] : []);
  for (const [label, command, disabled] of commands) menu.append(menuItem(label, command, { disabled }));
  for (const item of request.adapter?.commands?.(request) || []) {
    if (!item?.id || !item.label) continue;
    menu.append(menuItem(String(item.label), String(item.id), { disabled:!!item.disabled }));
  }
  if (request.link) menu.append(menuItem('Open link in new tab', 'open-link'));
  // A mounted widget adapter owns its extra commands. Files deliberately
  // mounts the Canvas adapter over its whole body, so retain the capability
  // filtered file object commands alongside that image-specific command.
  if (!request.adapter || request.objectKind === 'file' || ['image', 'link'].includes(request.objectKind)) for (const [label, command] of OBJECT_COMMANDS[request.objectKind] || []) {
    if (command === 'open-link' && request.link) continue;
    if (request.objectKind === 'file') {
      const selectedCount = Array.isArray(request.adapterContext?.selectedKeys)
        ? request.adapterContext.selectedKeys.length : 0;
      const groupCommands = new Set(['move-files', 'copy-files', 'select-all-files']);
      // A right-click inside an existing Files multi-selection represents
      // the whole selected group. Suppress row-only actions so they cannot
      // appear to apply to, or silently ignore, the highlighted group.
      if (selectedCount > 1 && !groupCommands.has(command)) continue;
      const capabilities = fileCapabilities(request.objectTarget);
      const required = { 'open-file':'open', 'open-in-editor':'open', 'open-in-editor-split-right':'open', 'open-in-editor-split-below':'open', 'reveal-file':'stat', 'rename-file':'rename', 'move-file':'move', 'move-files':'transfer-move', 'copy-files':'transfer-copy', 'trash-file':'trash', 'restore-file':'restore' }[command];
      if (required && !['transfer-move', 'transfer-copy'].includes(required) && capabilities && !capabilities.has(required)) continue;
      if (required === 'transfer-move' && typeof window.__openClankFilesContextCapabilities === 'function'
        && !window.__openClankFilesContextCapabilities(request.objectTarget, request).move) continue;
      if (required === 'transfer-copy' && typeof window.__openClankFilesContextCapabilities === 'function'
        && !window.__openClankFilesContextCapabilities(request.objectTarget, request).copy) continue;
      // Editor handoff is offered only for a resource explicitly authorized
      // as textual by the Files provider. Images and opaque managed records
      // may expose `open` while still lacking an Editor target.
      if (['open-in-editor', 'open-in-editor-split-right', 'open-in-editor-split-below'].includes(command)
        && request.objectTarget?.dataset?.fileOpenEditor !== 'true') continue;
      // A sealed resource reference is authority data and must never enter a
      // generic clipboard lane. Only compatibility rows with a real host path
      // expose this command.
      if (command === 'copy-file-path' && !request.objectTarget?.dataset?.path) continue;
    }
    if (request.objectKind === 'workspace'
      && (!request.objectTarget?.dataset?.workspaceId || request.objectTarget?.dataset?.workspaceAvailable !== 'true')) continue;
    const disabled = command === 'copy-image-bytes' && (!navigator.clipboard?.write || typeof window.ClipboardItem !== 'function');
    menu.append(menuItem(label, command, { disabled }));
  }
  if (request.editable && hasSelection && (!request.adapter ? /^[\p{L}\p{M}]+(?:['’-][\p{L}\p{M}]+)*$/u.test(selectedText(request).trim()) : request.adapter.spellingEligible?.(request) === true)) {
    menu.append(menuItem('Add word to dictionary', 'add-word', { disabled:typeof request.spellingService?.service?.add !== 'function' }));
    menu.append(menuItem('Remove word from dictionary', 'remove-word', { disabled:typeof request.spellingService?.service?.remove !== 'function' }));
    const word = selectedText(request).trim();
    const spelling = request.spellingService?.service;
    if (/^[\p{L}][\p{L}'-]*$/u.test(word) && spelling?.suggest) {
      Promise.resolve(spelling.suggest(word)).then((suggestions) => {
        if (active !== request || !menu.isConnected || !isCapturedContextCurrent(request) || !spellingServiceCurrent(request)) return;
        for (const suggestion of [...new Set(Array.isArray(suggestions) ? suggestions.map(String) : [])].slice(0, 5)) {
          if (!suggestion || suggestion === word) continue;
          menu.append(menuItem(`Replace with “${suggestion}”`, `replace-spelling:${suggestion}`));
        }
        positionContextMenu(menu, request);
      }).catch(() => {});
    }
  }
  // Leave unknown surfaces to the browser. This avoids replacing useful
  // native actions with an empty or unrelated application menu.
  if (!menu.querySelector('button[data-command]')) {
    menu.remove();
    return false;
  }
  menu.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-command]');
    if (button && !button.disabled) void invoke(button.dataset.command);
  });
  menu.addEventListener('keydown', (event) => {
    const buttons = [...menu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'Escape' && !event.isComposing) { event.preventDefault(); event.stopImmediatePropagation(); closeMenu(); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    }
    if (event.key === 'Home') { event.preventDefault(); buttons[0]?.focus(); }
    if (event.key === 'End') { event.preventDefault(); buttons.at(-1)?.focus(); }
  });
  document.body.append(menu);
  // ui.js arbitrates Escape in capture phase, ahead of this menu's handlers.
  // Register with its existing transient stack before it can close the applet.
  menu._dismiss = () => closeMenu();
  unregisterMenuDismiss = registerMenuDismiss(menu._dismiss);
  positionContextMenu(menu, request);
  menu.querySelector('button:not(:disabled)')?.focus();
  return true;
}

function onContextMenu(event) {
  if (!enabled() || event.defaultPrevented) return;
  // A second right-click is a new command target. Replace the old menu before
  // capturing so the next command cannot accidentally act on the first target.
  if (active) closeMenu({ restore:false });
  const request = capture(event);
  if (!renderMenu(request)) { active = null; return; }
  active = request;
  event.preventDefault();
}

export function initCustomContextMenu() {
  if (window.__openClankContextMenuInitialized) return;
  window.__openClankContextMenuInitialized = true;
  document.addEventListener('contextmenu', onContextMenu);
  document.addEventListener('pointerdown', (event) => {
    if (active && !event.target.closest(`#${MENU_ID}`)) closeMenu();
    if (event.pointerType !== 'touch' || event.target.closest(`#${MENU_ID}`)) return;
    clearTimeout(longPressTimer);
    longPressOrigin = { x:event.clientX, y:event.clientY };
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    longPressTimer = setTimeout(() => {
      longPressTimer = null;
      longPressOrigin = null;
      const synthetic = new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:event.clientX, clientY:event.clientY, detail:1 });
      target.dispatchEvent(synthetic);
    }, 550);
  }, true);
  const cancelLongPress = () => {
    if (longPressTimer) clearTimeout(longPressTimer);
    longPressTimer = null;
    longPressOrigin = null;
  };
  document.addEventListener('pointermove', (event) => {
    if (!longPressTimer || event.pointerType !== 'touch' || !longPressOrigin) return;
    const dx = event.clientX - longPressOrigin.x;
    const dy = event.clientY - longPressOrigin.y;
    if (Math.hypot(dx, dy) >= 8) cancelLongPress();
  }, { capture:true, passive:true });
  document.addEventListener('scroll', cancelLongPress, { capture:true, passive:true });
  document.addEventListener('pointerup', cancelLongPress, true);
  document.addEventListener('pointercancel', cancelLongPress, true);
  document.addEventListener('keydown', (event) => {
    cancelLongPress();
    if (event.key === 'Escape' && active && !event.defaultPrevented && !event.isComposing) {
      event.preventDefault(); event.stopImmediatePropagation(); closeMenu();
    }
  });
  document.addEventListener('copal-context-command', (event) => {
    const target = event.target instanceof Element ? event.target : null;
    const command = event.detail?.command;
    if (!target) return;
    const kind = target.closest?.('[data-copal-context-object]')?.dataset?.copalContextObject || '';
    const dispatchClick = (element) => {
      if (!element) return;
      if (typeof element.click === 'function') element.click();
      else element.dispatchEvent(new MouseEvent('click', { bubbles:true, cancelable:true, view:window }));
    };
    const fallback = () => {
      // Standalone consumers may only subscribe to the event contract. Keep a
      // small compatibility fallback when no mounted owner claims a command.
      if (command === 'toggle-task') target.querySelector('input[type="checkbox"]')?.click?.();
      else if (command === 'open-task') target.querySelector('.copal-task-title,[data-task-open],button,a')?.click?.();
      else if (command === 'open-file') dispatchClick(target);
      else if (command === 'edit-event' || command === 'open-graph-node') dispatchClick(target);
      else if (command === 'edit-track') dispatchClick(target.querySelector('.copal-track-edit,button[aria-label^="Edit "]'));
      else if (command === 'open-treehouse-item') dispatchClick(target.querySelector('button.copal-btn, a.copal-btn, summary'));
    };
    const handlers = objectHandlers(kind);
    if (handlers.length) void (async () => {
      for (const handler of handlers) if (await handler(command, target, event.detail?.request)) return;
      fallback();
    })();
    else fallback();
  });
  window.addEventListener('odysseus-context-menu-changed', () => { if (!enabled()) closeMenu(); });
  window.openClankContextMenu = Object.freeze({ enabled, close: closeMenu, registerAdapter });
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

// Rendered documentation widgets carry bounded source offsets. Inspect only
// mounted viewport widgets; source-mode owners may provide a cached range hook.
function mountedCommentSourceRange(editor, ranges, rawTarget) {
  const explicit = rawTarget?.closest?.('.cm-rich-comment-widget');
  const candidates = explicit && editor.view.dom.contains(explicit)
    ? [explicit] : editor.view.dom.querySelectorAll('.cm-rich-comment-widget');
  for (const node of candidates) {
    const from = Number(node.dataset.commentSourceFrom);
    const to = Number(node.dataset.commentSourceTo);
    if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) continue;
    if (node === explicit || ranges.some((range) => from <= Math.max(range.anchor, range.head) && to >= Math.min(range.anchor, range.head))) return Object.freeze({ from, to });
  }
  return null;
}

export function createCodeMirrorContextAdapter(editor, identity = {}) {
  const readIdentity = (name, fallback = null) => {
    const value = identity[name];
    return typeof value === 'function' ? value() : value ?? fallback;
  };
  const languageIdentity = () => { const language = editor.getStatus?.()?.language; return language ? `${language.id}|${language.mode || ''}|${language.override || ''}` : ''; };
  const isCurrent = (request) => {
    const captured = request?.adapterContext;
    const view = editor.view;
    const selection = view?.state?.selection;
    return !!captured && !!selection && view.dom.isConnected
      && view.state.doc === captured.documentIdentity
      && (view.contentDOM?.getAttribute('contenteditable') === 'false') === captured.readOnly
      && selection.mainIndex === captured.mainIndex
      && selection.ranges.length === captured.ranges.length
      && selection.ranges.every((range, index) => range.anchor === captured.ranges[index].anchor && range.head === captured.ranges[index].head)
      && captured.bufferIdentity === readIdentity('bufferIdentity', editor)
      && captured.revision === readIdentity('revision')
      && captured.scope === readIdentity('scope') && captured.languageIdentity === languageIdentity();
  };
  return {
    isCurrent,
    restore:(request) => { if (!isCurrent(request)) return false; editor.focus(); return true; },
    commands: (request) => {
      // Selection-only match/transform actions are disabled with an empty
      // selection; line/cursor commands (duplicate, indent) stay usable.
      const hasSelection = hasSelectedText(request);
      const readOnly = editor.view.contentDOM?.getAttribute('contenteditable') === 'false';
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
    capture: (rawTarget) => {
      const selection = editor.getSelection?.() || { ranges:[{ anchor:editor.view.state.selection.main.anchor, head:editor.view.state.selection.main.head }], mainIndex:0 };
      const ranges = selection.ranges.map((range) => ({ anchor:range.anchor, head:range.head }));
      const documentIdentity = editor.view.state.doc;
      const rangeText = range => documentIdentity.sliceString(Math.min(range.anchor, range.head), Math.max(range.anchor, range.head));
      const mainRange = ranges[selection.mainIndex] || ranges[0];
      const query = mainRange && mainRange.anchor !== mainRange.head ? rangeText(mainRange) : undefined;
      const text = ranges.filter(range => range.anchor !== range.head).map(rangeText).join('\n');
      return Object.freeze({
        ranges, mainIndex:selection.mainIndex, text, query,
        documentIdentity, documentLength:documentIdentity.length,
        spelling:editor.captureSpelling?.(), languageIdentity:languageIdentity(),
        readOnly:editor.view.contentDOM?.getAttribute('contenteditable') === 'false',
        bufferIdentity:readIdentity('bufferIdentity', editor), revision:readIdentity('revision'), scope:readIdentity('scope'),
        commentSourceRange:typeof identity.commentSourceRange === 'function' ? identity.commentSourceRange(ranges) : editor.peekCommentSourceRange?.(ranges[selection.mainIndex || 0]?.head ?? 0) || mountedCommentSourceRange(editor, ranges, rawTarget),
      });
    },
    spellingEligible:(request) => request.adapterContext?.ranges?.length === 1
      && /^[\p{L}][\p{L}'-]*$/u.test(selectedText(request).trim())
      && editor.isSpellingSelectionEligible?.(request.adapterContext?.spelling) === true,
    execute: async (command, request) => {
      const captured = request.adapterContext; const view = editor.view;
      if (!isCurrent(request)) throw new Error('The original editing target changed before the command completed.');
      const replace = (text) => { editor.replaceSelections?.(String(text ?? '')) || editor.replaceRange?.(captured.ranges[captured.mainIndex]?.anchor || 0, captured.ranges[captured.mainIndex]?.head || 0, String(text ?? '')); };
      const query = captured.query;
      const readOnly = view.contentDOM?.getAttribute('contenteditable') === 'false';
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
