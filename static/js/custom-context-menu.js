// The Copal menu owns the editing interaction in every browser. It keeps the
// target and its selection in a short lived context so opening the menu cannot
// redirect a command to whichever window receives focus later.

const STORAGE_KEY = 'odysseus-custom-context-menu';
const MENU_ID = 'openclank-context-menu';
let active = null;
let longPressTimer = null;
let longPressOrigin = null;
const adapters = new WeakMap();
const OBJECT_COMMANDS = Object.freeze({
  image:[['Open image', 'open-image'], ['Copy image address', 'copy-image-address'], ['Copy image', 'copy-image-bytes']],
  link:[['Open link in new tab', 'open-link'], ['Copy link address', 'copy-link-address']],
  file:[['Open in Editor', 'open-in-editor'], ['Open right', 'open-in-editor-split-right'], ['Open below', 'open-in-editor-split-below'], ['Select all files', 'select-all-files'], ['Copy file path', 'copy-file-path'], ['Reveal in Files', 'reveal-file'], ['Rename', 'rename-file'], ['Move', 'move-files'], ['Copy', 'copy-files'], ['Move to trash', 'trash-file'], ['Restore', 'restore-file']],
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
  if (!candidate || candidate.disabled || candidate.readOnly || candidate.getAttribute('aria-readonly') === 'true') return null;
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
    value: typeof target.value === 'string' ? target.value : target.textContent || '',
    selection: text, selectionRange, domRange,
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
  document.getElementById(MENU_ID)?.remove();
  const prior = active; active = null;
  if (restore) restoreSelection(prior || {}, { focus:true });
}

function removeMenu() {
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

async function invoke(command) {
  const request = active;
  if (!request) return;
    closeMenu({ restore:false });
    try {
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
    if (!restoreSelection(request)) throw new Error('The original editing target is no longer available.');
    if (command === 'undo' || command === 'redo' || command === 'select-all') {
      document.execCommand(command === 'select-all' ? 'selectAll' : command);
    } else if (command === 'copy') {
      await clipboardWrite(selectedText(request));
    } else if (command === 'cut') {
      const text = selectedText(request); await clipboardWrite(text); localDelete(request);
    } else if (command === 'paste' || command === 'paste-plain') {
      const text = await clipboardRead();
      if (request.selectionRange) setInputSelection(request.target, request.selectionRange.start, request.selectionRange.end, text);
      else document.execCommand('insertText', false, text);
    } else if (command === 'delete') {
      localDelete(request);
    } else if (command === 'spellcheck') {
      request.target.spellcheck = true;
      const words = selectedText(request).split(/[^\p{L}'-]+/u).filter(Boolean);
      const service = window.openClankSpelling;
      if (service && words.length) {
        const results = await Promise.all(words.map((word) => service.check(word)));
        const misspelled = words.filter((_, index) => !results[index]);
        window.uiModule?.showToast?.(misspelled.length ? `Possible spelling errors: ${misspelled.join(', ')}` : 'Spelling looks good.');
      }
      request.target.dispatchEvent(new Event('input', { bubbles:true }));
    } else if (command === 'add-word' || command === 'remove-word') {
      const service = window.openClankSpelling;
      if (service) await service[command === 'add-word' ? 'add' : 'remove'](selectedText(request).split(/[^\p{L}'-]+/u).filter(Boolean));
    } else if (command === 'copy-link-address' && request.link) {
      await clipboardWrite(request.link);
    } else if (command.startsWith('replace-spelling:')) {
      const replacement = command.slice('replace-spelling:'.length);
      if (request.selectionRange) setInputSelection(request.target, request.selectionRange.start, request.selectionRange.end, replacement);
      else if (restoreSelection(request)) document.execCommand('insertText', false, replacement);
    } else if (command === 'open-link' && request.link) {
      window.open(request.link, '_blank', 'noopener,noreferrer');
    }
  } catch (error) {
    window.uiModule?.showToast?.(error?.message || 'The context-menu command could not be completed.');
  }
}

function renderMenu(request) {
  removeMenu();
  const menu = document.createElement('div');
  menu.id = MENU_ID; menu.className = 'openclank-context-menu';
  menu.setAttribute('role', 'menu'); menu.tabIndex = -1;
  const hasSelection = !!selectedText(request);
  const commands = [
    ['Undo', 'undo', !request.editable], ['Redo', 'redo', !request.editable],
    ['Cut', 'cut', !request.editable || !hasSelection], ['Copy', 'copy', !hasSelection],
    ['Paste', 'paste', !request.editable], ['Paste as plain text', 'paste-plain', !request.editable],
    ['Check spelling', 'spellcheck', !request.editable], ['Delete', 'delete', !request.editable || !hasSelection],
    // Files owns Select all for its active column; the generic command would
    // select the document body and is therefore disabled on a Files row.
    ['Select all', 'select-all', request.objectKind === 'file' || !request.target],
  ];
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
      const capabilities = fileCapabilities(request.objectTarget);
      const required = { 'open-file':'open', 'open-in-editor':'open', 'open-in-editor-split-right':'open', 'open-in-editor-split-below':'open', 'reveal-file':'reveal', 'rename-file':'rename', 'move-file':'move', 'move-files':'transfer-move', 'copy-files':'transfer-copy', 'trash-file':'trash', 'restore-file':'restore' }[command];
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
    const disabled = command === 'copy-image-bytes' && (!navigator.clipboard?.write || typeof window.ClipboardItem !== 'function');
    menu.append(menuItem(label, command, { disabled }));
  }
  if (request.editable && hasSelection) {
    menu.append(menuItem('Add word to dictionary', 'add-word'));
    menu.append(menuItem('Remove word from dictionary', 'remove-word'));
    const word = selectedText(request).trim();
    const spelling = window.openClankSpelling;
    if (/^[\p{L}][\p{L}'-]*$/u.test(word) && spelling?.suggest) {
      Promise.resolve(spelling.suggest(word)).then((suggestions) => {
        if (active !== request || !menu.isConnected) return;
        for (const suggestion of [...new Set(Array.isArray(suggestions) ? suggestions.map(String) : [])].slice(0, 5)) {
          if (!suggestion || suggestion === word) continue;
          menu.append(menuItem(`Replace with “${suggestion}”`, `replace-spelling:${suggestion}`));
        }
      }).catch(() => {});
    }
  }
  menu.addEventListener('click', (event) => {
    const button = event.target.closest('button[data-command]');
    if (button && !button.disabled) void invoke(button.dataset.command);
  });
  menu.addEventListener('keydown', (event) => {
    const buttons = [...menu.querySelectorAll('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement);
    if (event.key === 'Escape') { event.preventDefault(); closeMenu(); return; }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault(); buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus();
    }
    if (event.key === 'Home') { event.preventDefault(); buttons[0]?.focus(); }
    if (event.key === 'End') { event.preventDefault(); buttons.at(-1)?.focus(); }
  });
  document.body.append(menu);
  menu.style.left = `${Math.max(8, Math.min(request.x, window.innerWidth - menu.offsetWidth - 8))}px`;
  menu.style.top = `${Math.max(8, Math.min(request.y, window.innerHeight - menu.offsetHeight - 8))}px`;
  menu.querySelector('button:not(:disabled)')?.focus();
}

function onContextMenu(event) {
  if (!enabled() || event.defaultPrevented) return;
  // A second right-click is a new command target. Replace the old menu before
  // capturing so the next command cannot accidentally act on the first target.
  if (active) closeMenu({ restore:false });
  active = capture(event);
  event.preventDefault();
  renderMenu(active);
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
  document.addEventListener('keydown', (event) => { cancelLongPress(); if (event.key === 'Escape' && active) closeMenu(); });
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
  const ranges = Array.isArray(captured.ranges) ? captured.ranges : [];
  if (!ranges.length) return false;
  const from = Math.min(...ranges.map((range) => Math.min(range.anchor, range.head)));
  const to = Math.max(...ranges.map((range) => Math.max(range.anchor, range.head)));
  const map = captured.commentSourceMap;
  return Array.isArray(map) && map.some((region) => region.from <= to && region.to >= from);
}

export function createCodeMirrorContextAdapter(editor, identity = {}) {
  const readIdentity = (name, fallback = null) => {
    const value = identity[name];
    return typeof value === 'function' ? value() : value ?? fallback;
  };
  return {
    commands: (request) => {
      // Selection-only match/transform actions are disabled with an empty
      // selection; line/cursor commands (duplicate, indent) stay usable.
      const hasSelection = !!selectedText(request);
      return [
        { id:'select-next-match', label:'Select next match', disabled:!hasSelection },
        { id:'select-all-matches', label:'Select all matches', disabled:!hasSelection },
        { id:'duplicate-line', label:'Duplicate line' },
        { id:'indent', label:'Indent selection' },
        { id:'format-bold', label:'Bold selection', disabled:!hasSelection },
        { id:'format-italic', label:'Italic selection', disabled:!hasSelection },
        { id:'format-code', label:'Inline code selection', disabled:!hasSelection },
        { id:'see-source', label:'See source', disabled:!hasCommentSource(request) },
        { id:'insert-template', label:'Insert template' },
        { id:'new-from-template', label:'New from template' },
      ];
    },
    capture: () => {
      const selection = editor.getSelection?.() || { ranges:[{ anchor:editor.view.state.selection.main.anchor, head:editor.view.state.selection.main.head }], mainIndex:0 };
      const ranges = selection.ranges.map((range) => ({ anchor:range.anchor, head:range.head }));
      return Object.freeze({
        ranges, mainIndex:selection.mainIndex, text:editor.getSelectedText?.() || '',
        documentText:editor.view.state.doc.toString(), documentLength:editor.view.state.doc.length,
        bufferIdentity:readIdentity('bufferIdentity', editor), revision:readIdentity('revision'), scope:readIdentity('scope'),
        commentSourceMap:editor.getCommentSourceMap?.() || [],
      });
    },
    execute: async (command, request) => {
      const captured = request.adapterContext; const view = editor.view;
      const isCurrent = () => {
        const current = {
          bufferIdentity:readIdentity('bufferIdentity', editor), revision:readIdentity('revision'), scope:readIdentity('scope'),
        };
        const currentSelection = editor.getSelection?.();
        const sameSelection = !!currentSelection
          && currentSelection.mainIndex === captured.mainIndex
          && JSON.stringify(currentSelection.ranges) === JSON.stringify(captured.ranges);
        return !!captured && sameSelection && view.dom.isConnected
          && view.state.doc.length === captured.documentLength
          && view.state.doc.toString() === captured.documentText
          && captured.bufferIdentity === current.bufferIdentity
          && captured.revision === current.revision
          && captured.scope === current.scope;
      };
      if (!isCurrent()) {
        throw new Error('The original editing target changed before the command completed.');
      }
      const replace = (text) => { editor.replaceSelections?.(String(text ?? '')) || editor.replaceRange?.(captured.ranges[captured.mainIndex]?.anchor || 0, captured.ranges[captured.mainIndex]?.head || 0, String(text ?? '')); };
      const mainRange = captured.ranges[captured.mainIndex] || captured.ranges[0] || { anchor:0, head:0 };
      const query = view.state.doc.sliceString(Math.min(mainRange.anchor, mainRange.head), Math.max(mainRange.anchor, mainRange.head));
      if (command === 'undo') { editor.undo(); editor.focus(); return; }
      if (command === 'redo') { editor.redo(); editor.focus(); return; }
      if (command === 'select-all') { editor.setSelection?.({ ranges:[{ anchor:0, head:view.state.doc.length }], mainIndex:0 }); return; }
      if (command === 'select-next-match') { editor.selectNextMatch?.(query); return; }
      if (command === 'select-all-matches') { editor.selectAllMatches?.(query); return; }
      if (command === 'duplicate-line') { editor.duplicateLines?.(); return; }
      if (command === 'indent') { editor.runCommand?.('indent'); return; }
      if (command === 'format-bold') { editor.formatSelections?.('**'); return; }
      if (command === 'format-italic') { editor.formatSelections?.('*'); return; }
      if (command === 'format-code') { editor.formatSelections?.('`'); return; }
      // Range-aware See source: reveal the documentation region under the
      // captured selection while preserving the rest of the editing state.
      if (command === 'see-source') {
        const map = captured.commentSourceMap || editor.getCommentSourceMap?.() || [];
        const region = map.find((entry) => entry.from <= Math.max(mainRange.anchor, mainRange.head) && entry.to >= Math.min(mainRange.anchor, mainRange.head));
        if (!region) return false;
        editor.revealCommentSource?.(region.from, region.to);
        return true;
      }
      if (command === 'copy' || command === 'cut') {
        if (!captured.text) return;
        if (!navigator.clipboard?.writeText) throw new Error('Clipboard writing is unavailable in this browser.');
        await navigator.clipboard.writeText(captured.text);
        if (!isCurrent()) throw new Error('The original editing target changed before the command completed.');
        if (command === 'cut') replace(''); return;
      }
      if (command === 'paste' || command === 'paste-plain') {
        if (!navigator.clipboard?.readText) throw new Error('Clipboard reading is unavailable in this browser.');
        const text = await navigator.clipboard.readText();
        if (!isCurrent()) throw new Error('The original editing target changed before the command completed.');
        editor.pasteText?.(text) || replace(text); return;
      }
      if (command === 'delete') { replace(''); return; }
      if (command === 'spellcheck') { view.dom.dataset.spellcheck = 'enabled'; return; }
      if (command.startsWith('replace-spelling:')) { replace(command.slice('replace-spelling:'.length)); return true; }
      if (command === 'add-word' || command === 'remove-word') {
        const words = captured.text.split(/[^\p{L}'-]+/u).filter(Boolean);
        if (words.length) await window.openClankSpelling?.[command === 'add-word' ? 'add' : 'remove']?.(words);
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
