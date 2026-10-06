// One descriptor authority for applet menus and the command palette.
// Surface owners keep document/workspace capabilities and captured mutations.
import * as Modals from './modalManager.js';
import { applyEdgeDock, clearRightDock } from './modalSnap.js';
import { IS_MAC } from './platform.js';
import {
  captureContextCommandTarget, capturedEditingCommands,
  executeCapturedContextCommand, isCapturedContextCurrent, restoreCapturedContext,
} from './custom-context-menu.js';

export const WORKBENCH_MENUS = Object.freeze(['File', 'Edit', 'Selection', 'View', 'Go', 'Tools', 'Window', 'Help']);
export const FILES_WORKBENCH_MENUS = Object.freeze(['File', 'Edit', 'View', 'Go', 'Window', 'Help']);
const surfaces = new WeakMap();
let operations = {};
const mod = IS_MAC ? 'Cmd' : 'Ctrl';
const keyHints = {
  undo:`${mod}+Z`, redo:IS_MAC ? 'Cmd+Shift+Z' : 'Ctrl+Y', cut:`${mod}+X`, copy:`${mod}+C`, paste:`${mod}+V`,
  'select-all':`${mod}+A`, 'select-next-match':`${mod}+D`, 'select-all-matches':`${mod}+Shift+L`,
  'add-cursor-above':`${mod}+Alt+↑`, 'add-cursor-below':`${mod}+Alt+↓`, 'collapse-selections':'Escape',
};
const selectionIds = new Set(['select-all', 'select-next-match', 'select-all-matches', 'add-cursor-above', 'add-cursor-below', 'collapse-selections']);

export function configureWorkbenchCommands(value) { operations = value || {}; }

export function registerWorkbenchSurface(host, provider) {
  if (!(host instanceof Element) || typeof provider?.capture !== 'function' || typeof provider?.isCurrent !== 'function' || typeof provider?.commands !== 'function') {
    throw new TypeError('Workbench surfaces require a host and capture/isCurrent/commands methods.');
  }
  surfaces.set(host, provider);
  return () => { if (surfaces.get(host) === provider) surfaces.delete(host); };
}

function findSurface(target) {
  for (let node = target; node instanceof Element; node = node.parentElement) {
    if (surfaces.has(node)) return { host:node, provider:surfaces.get(node) };
  }
  return null;
}

function visible(node) {
  return !!node?.isConnected && !node.closest('.hidden,[hidden],.modal-minimized') && node.getClientRects().length > 0;
}

export function workbenchSurfaceHost(target) { return findSurface(target)?.host || null; }

export function captureWorkbenchTarget(target = document.activeElement, rememberedOwner = null, { host = null, kind = null } = {}) {
  let source = target instanceof Element ? target : document.body;
  // A local menu may resolve a replaced row only within its own window.
  // Never fall back to another visible/focused applet.
  if (host) {
    if (!visible(host)) throw new Error('The original applet window is unavailable.');
    if (!source.isConnected || !host.contains(source)) source = host;
  } else if (!source.isConnected) {
    source = visible(rememberedOwner) ? rememberedOwner : document.body;
  }
  const surface = findSurface(source);
  const modal = source.closest('.modal');
  let surfaceTarget = null;
  if (surface) surfaceTarget = surface.provider.capture();
  // A surface can identify its captured active editor when the opener is a
  // header/toolbar button. It must stay inside that exact registered owner.
  const textField = source.matches('input,textarea,[contenteditable="true"]') && !source.closest('.cm-editor');
  const editingSource = textField ? source : surface?.provider.editingTarget?.(surfaceTarget);
  if (editingSource && (!(editingSource instanceof Element) || surface && !surface.host.contains(editingSource))) throw new Error('The captured text editor is unavailable.');
  const editing = captureContextCommandTarget(editingSource || source);
  if (surfaceTarget && typeof surfaceTarget === 'object') Object.freeze(surfaceTarget);
  const surfaceKind = kind || surface?.provider.kind || (modal?.classList.contains('files-window') ? 'files' : modal?.classList.contains('copal-notes-window') ? 'editor' : 'app');
  return Object.freeze({ source, owner:host || surface?.host || modal, modal, surface:surface && Object.freeze(surface), surfaceTarget, editing, kind:surfaceKind });
}

function isSurfaceCurrent(captured) {
  if (!captured || captured.owner && !visible(captured.owner)) return false;
  const { surface, surfaceTarget } = captured;
  return !surface || (visible(surface.host) && surfaces.get(surface.host) === surface.provider && surface.provider.isCurrent(surfaceTarget));
}

function isWindowCurrent(captured) {
  return !!captured.modal && visible(captured.modal) && document.getElementById(captured.modal.id) === captured.modal && Modals.isRegistered(captured.modal.id);
}

const editorSlots = [
  ['editor.new', 'New file or note', 'File'],
  ['editor.open-folder', 'Open folder…', 'File'], ['editor.close-folder', 'Close folder', 'File'],
  ['editor.save', 'Save file', 'File'], ['editor.close-tab', 'Close editor tab', 'File'],
  ['editor.find', 'Find and replace…', 'Edit'],
  ['editor.sidebar', 'Toggle Editor sidebar', 'View'], ['editor.linked-sidebar', 'Toggle linked sidebar', 'View'],
  ['editor.mode-source', 'Source mode', 'View'], ['editor.mode-live', 'Editing / Live Preview mode', 'View'], ['editor.mode-reading', 'Reading mode', 'View'],
  ['editor.quick-open', 'Quick open…', 'Go'], ['editor.search', 'Search documents…', 'Go'],
  ['editor.history', 'Document history…', 'Tools'], ['editor.settings', 'Editor settings…', 'Tools'],
  ['editor.split-right', 'Split editor right', 'Window'], ['editor.split-below', 'Split editor below', 'Window'],
];

export function workbenchCommandDescriptors(captured) {
  const byId = new Map();
  const add = (descriptor) => {
    if (!descriptor?.id || !descriptor.label || !WORKBENCH_MENUS.includes(descriptor.menu)) return;
    byId.set(descriptor.id, Object.freeze({ ...descriptor, disabledReason:String(descriptor.disabledReason || '') }));
  };
  const app = (id, label, menu, operation, shortcut = '') => add({
    id, label, menu, shortcut, targeted:false,
    disabledReason:typeof operations[operation] === 'function' ? '' : 'This action is unavailable.',
    run:() => operations[operation]?.(captured),
  });
  const files = captured.kind === 'files';
  const editor = captured.kind === 'editor';
  const textEditing = captured.editing.editable || captured.editing.selectionRange
    || (!captured.editing.objectKind && captured.editing.domRange && captured.editing.selection);
  if (!files) {
    app('app.editor', 'Open Editor', 'File', 'editor');
    app('app.files', 'Open Files', 'File', 'files');
  }
  if (editor) for (const [id, label, menu] of editorSlots) add({ id, label, menu, disabledReason:'This command is unavailable on the active surface.' });
  if (captured.surface) {
    for (const descriptor of captured.surface.provider.commands(captured.surfaceTarget) || []) {
      // Applet descriptors retain their own context/selection authority.
      // They cannot replace app-wide launchers or captured window controls.
      if (!String(descriptor.id || '').startsWith(files ? 'files.' : 'editor.')) continue;
      if (files && textEditing && ['files.copy', 'files.cut', 'files.paste', 'files.paste-plain', 'files.delete', 'files.select-all'].includes(descriptor.id)) continue;
      const current = descriptor.current;
      add({ ...descriptor, targeted:true, current:() => isSurfaceCurrent(captured) && (!current || current(captured.surfaceTarget)) });
    }
  }
  for (const item of editor || textEditing ? capturedEditingCommands(captured.editing) : []) {
    let disabledReason = item.disabledReason;
    if (['copy', 'cut'].includes(item.id) && !navigator.clipboard?.writeText) disabledReason = 'Clipboard writing is unavailable in this browser.';
    if (['paste', 'paste-plain'].includes(item.id) && !navigator.clipboard?.readText) disabledReason = 'Clipboard reading is unavailable in this browser.';
    add({ id:`edit.${item.id}`, label:item.label, menu:!files && selectionIds.has(item.id) ? 'Selection' : item.id === 'see-source' ? 'View' : !files && ['insert-template', 'new-from-template', 'spellcheck', 'retry-spelling'].includes(item.id) ? 'Tools' : 'Edit',
      shortcut:keyHints[item.id] || '', disabledReason, current:() => isCapturedContextCurrent(captured.editing),
      run:() => executeCapturedContextCommand(item.id, captured.editing) });
  }
  app('app.palette', 'Command palette…', 'View', 'palette', `${mod}+Shift+P`);
  app('app.preferences', 'Settings…', files ? 'Edit' : 'Tools', 'settings');
  app('app.appearance', 'Theme and appearance…', 'View', 'appearance');
  if (!files) app('app.chat-search', 'Search conversations…', 'Go', 'chatSearch');
  app('app.help', 'Open Clank help…', 'Help', 'help');
  app('app.docs', 'Documentation', 'Help', 'docs');
  const windowReason = isWindowCurrent(captured) ? '' : 'Activate an applet window first.';
  add({ id:'window.minimize', label:'Minimize this window', menu:'Window', disabledReason:windowReason,
    current:() => isWindowCurrent(captured), run:() => Modals.minimize(captured.modal.id) });
  add({ id:'window.close', label:'Close this window', menu:'File', disabledReason:windowReason,
    current:() => isWindowCurrent(captured), run:() => Modals.close(captured.modal.id) });
  for (const side of ['left', 'right']) add({ id:`window.dock-${side}`, label:`Dock this window ${side}`, menu:'Window', disabledReason:windowReason,
    checked:!!captured.modal?.classList.contains(`modal-${side}-docked`),
    current:() => isWindowCurrent(captured), run:() => applyEdgeDock(captured.modal, side) });
  const docked = captured.modal?.classList.contains('modal-left-docked') || captured.modal?.classList.contains('modal-right-docked');
  add({ id:'window.undock', label:'Undock this window', menu:'Window', disabledReason:windowReason || (docked ? '' : 'This window is already floating.'),
    current:() => isWindowCurrent(captured), run:() => clearRightDock(captured.modal) });
  return [...byId.values()].filter((item) => !files || FILES_WORKBENCH_MENUS.includes(item.menu));
}

export async function runWorkbenchCommand(descriptor, captured) {
  if (descriptor.disabledReason) throw new Error(descriptor.disabledReason);
  if (descriptor.targeted !== false && (!isSurfaceCurrent(captured) || (descriptor.current && !descriptor.current()))) {
    throw new Error('The original command target changed. Reopen the menu or command palette.');
  }
  if (typeof descriptor.run !== 'function') throw new Error('This action is unavailable on the active surface.');
  return descriptor.run(captured);
}

export function restoreWorkbenchTarget(captured) {
  if (!captured || !isSurfaceCurrent(captured)) return;
  const field = captured.editing?.target;
  if (field === captured.source && field.matches?.('input,textarea,[contenteditable="true"]') && !field.closest('.cm-editor') && restoreCapturedContext(captured.editing)) return;
  if (captured.surface?.provider.restore) captured.surface.provider.restore(captured.surfaceTarget);
  else if (!restoreCapturedContext(captured.editing) && visible(captured.source)) captured.source.focus?.({ preventScroll:true });
}
