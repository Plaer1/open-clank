// Only registered product UI names cross into chat. Never sample visible text,
// inputs, DOM attributes carrying user data, a screen image, or a resource path.
const controls = [
  ['.bound-buddy-play', 'Buddy'],
  ['#sidebar-new-chat-btn', 'New Chat'],
  ['#chat-workspace-button', 'Chat Workspace'],
  ['.chat-meta-overlay', 'Chat title bar'],
  ['.chat-input-bar', 'Chat composer'],
  ['[data-editor-mode-button="notes"]', 'Text editor'],
  ['[data-journal-explorer="true"] [data-note-panel="files"], [data-journal-explorer="true"] .copal-editor-files-explorer, [data-journal-explorer="true"]', 'Journal explorer'],
  ['[data-editor-help-mode="journal"]', 'Journal page'],
  ['[data-copal-journal-manager], .copal-journal-manager, #set-copal-manage-journals', 'Manage journals'],
  ['[data-settings-panel="buddy"], [data-settings-tab="buddy"]', 'Buddy'],
  ['[data-settings-panel="memery"], [data-settings-tab="memery"]', 'Memery'],
  ['[data-editor-create="file"][aria-label="New journal page"]', 'New journal page'],
  ['[data-editor-mode] [title="Save file (Ctrl/Cmd+S)"]', 'Save file'],
  ['[data-editor-mode] [aria-label="Search documents"]', 'Document search'],
  ['[data-note-panel="instruction-manual"]', 'Instruction Manual'],
  ['.copal-notes-explorer', 'Explorer'],
  ['.copal-note-tabs', 'Document tabs'],
  ['.copal-editor-activity-rail', 'Editor activity rail'],
  ['.copal-graph-inspector', 'Graph inspector'],
  ['.copal-editor-graph-sidebar', 'Graph sidebar'],
  ...[['properties','Document details'], ['links','Document links'], ['outline','Document outline'],
      ['recent','Recent documents'], ['search','Document search']].map(([key,label]) =>
    [`[data-note-panel="${key}"], [data-panel-id="${key}"]`, label]),
  ['.copal-editor-files-explorer', 'Explorer'],
  ['[data-graph-mode]', 'Graph view selector'],
  ['[aria-label="Reset graph filters"]', 'Reset graph filters'],
  ['[aria-label="Drift route"]', 'Drift route selector'],
  ['[data-contextual-help-button], #tool-help-btn', 'Help'],
];
const sidebar = [
  ['#chats-section-title', 'Chats'], ['#buddy-section', 'Buddy'],
  ['#copal-section-toggle', 'Copal'], ['#email-section', 'Email'],
  ['#tools-section > .section-header-flex', 'Tools'],
  ['#tool-calendar-btn', 'Calendar'], ['#tool-compare-btn', 'Compare'],
  ['#tool-memory-btn', 'Memery'], ['#tool-theme-btn', 'Theme'],
  ['#tool-tasks-btn', 'Clanker Tasks'], ['#tool-research-btn', 'Deep Research'],
  ['#tool-usage-btn', 'Usage'], ['#tool-cookbook-btn', 'Cookbook'],
  ['#user-bar-settings', 'Settings'],
  ['[data-copal-launcher="journal"]', 'Journal'],
];
const applets = [
  ['notes', 'Editor'], ['wiki', 'Wiki'], ['timeline', 'Timeline'],
  ['graph', 'Graph'], ['treehouse', 'TreeHouse'], ['clankshop', 'Clankshop'],
  ['achievements', 'Achievements'], ['todo', 'Meatbag Tasks'],
].flatMap(([id, label]) => [
  [`[data-copal-view="${id}"]`, label],
  [`#copal-${id}-modal .oc-applet-frame`, label],
]);
const editorModes = [
  ['text', 'Text editor', 'notes'],
  ['image', 'Image editor', 'image'],
  ['graph', 'Graph viewer', 'graph'],
  ['wiki', 'Wiki reader and editor', 'wiki'],
].map(([mode, label, button]) => [
  `[data-editor-help-mode="${mode}"], [data-editor-mode-button="${button}"]`, label,
]);
const catalogue = [
  ...controls, ...editorModes, ...sidebar, ...applets,
  ['#tool-files-btn, .files-window .oc-applet-frame', 'Files'],
  ['#settings-modal .oc-applet-frame, #settings-modal .modal-content', 'Settings'],
  ['#chat-container', 'Chat'],
];

const targetSelectors = catalogue.map(([selector]) => selector).join(',');

function match(target) {
  if (!(target instanceof Element)) return null;
  const element = target.closest(targetSelectors);
  if (!element) return null;
  const [, label] = catalogue.find(([selector]) => element.matches(selector));
  return {element, label};
}

let activePicker = null;

export function startBuddyExplanation(root, opener, signal, openChat) {
  return startProductPicker({root, opener, signal, matchTarget:match, targetTooltip:true, onSelect:item=>openChat(item.label)});
}

export function startProductPicker({root, opener, signal, matchTarget, onSelect, targetTooltip = false, initial = 'Point to a pane or control, then select it. Escape cancels.'}) {
  if (signal.aborted) return () => {};
  activePicker?.();
  const picker = new AbortController();
  const active = {capture:true,signal:picker.signal};
  const hint = document.createElement('div');
  hint.className = 'bound-buddy-picker-hint';
  const status = document.createElement('span');
  status.setAttribute('role','status');
  status.textContent = initial;
  const cancel = document.createElement('button');
  cancel.type = 'button'; cancel.textContent = 'Cancel';
  hint.append(status,cancel);
  root.append(hint);
  root.classList.add('bound-buddy-picking');
  const tooltip = targetTooltip ? document.createElement('span') : null;
  if (tooltip) {
    tooltip.className = 'bound-buddy-target-tooltip';
    tooltip.setAttribute('aria-hidden','true'); // The picker status announces this label.
    tooltip.hidden = true;
    root.append(tooltip);
  }
  let highlight = null;
  function preview(item, pointer) {
    if (item?.element !== highlight) {
      highlight?.classList.remove('bound-buddy-target');
      highlight = item?.element || null;
      highlight?.classList.add('bound-buddy-target');
    }
    const text = item?.label || initial;
    if (status.textContent !== text) status.textContent = text;
    if (tooltip) {
      tooltip.hidden = !item || !pointer;
      if (!tooltip.hidden) {
        if (tooltip.textContent !== text) tooltip.textContent = text;
        tooltip.style.left = Math.max(4, Math.min(pointer.clientX + 14, innerWidth - tooltip.offsetWidth - 4)) + 'px';
        tooltip.style.top = Math.max(4, Math.min(pointer.clientY + 16, innerHeight - tooltip.offsetHeight - 4)) + 'px';
      }
    }
  }
  function stop({restore = true} = {}) {
    if (picker.signal.aborted) return;
    picker.abort();
    if (activePicker === scopeClosed) activePicker = null;
    signal.removeEventListener('abort', scopeClosed);
    preview(null);
    root.classList.remove('bound-buddy-picking');
    hint.remove();
    tooltip?.remove();
    if (restore) opener.focus({preventScroll:true});
  }
  function scopeClosed() { stop({restore:false}); }
  function hasTarget(event) { return opener.contains(event.target) || !root.contains(event.target); }
  function choose(item) {
    if (!item) return;
    stop({restore:false});
    onSelect(item);
  }
  activePicker = scopeClosed;
  signal.addEventListener('abort',scopeClosed,{once:true});
  cancel.addEventListener('click',()=>stop(),active);
  document.addEventListener('pointermove',event => preview(hasTarget(event) ? matchTarget(event.target) : null, event),active);
  document.addEventListener('focusin',event => preview(hasTarget(event) ? matchTarget(event.target) : null),active);
  for (const type of ['pointerdown','pointerup','click']) {
    document.addEventListener(type,event => {
      if (!hasTarget(event)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (type === 'click') choose(matchTarget(event.target));
    },active);
  }
  document.addEventListener('keydown',event => {
    if (event.key === 'Escape') {
      event.preventDefault(); event.stopImmediatePropagation(); stop(); return;
    }
    if (!hasTarget(event) || !['Enter',' '].includes(event.key)) return;
    event.preventDefault(); event.stopImmediatePropagation();
    choose(matchTarget(event.target));
  },active);
  cancel.focus({preventScroll:true});
  return scopeClosed;
}
