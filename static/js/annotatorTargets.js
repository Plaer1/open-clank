// Product labels are fixed. Never read a target's text, title, field value,
// accessible name, URL, document ID, or application-supplied row key.
const pageRoots = [
  ['.browser-applet-window', 'browser'], ['.files-window', 'files'],
  ['#memory-modal', 'memery'], ['#theme-modal', 'theme'], ['#cookbook-modal', 'cookbook'],
  ['#calendar-modal', 'calendar'], ['.stats-usage-window', 'usage'],
  ['#copal-treehouse-modal', 'treehouse'], ['#copal-clankshop-modal', 'clankshop'],
  ['#copal-achievements-modal', 'achievements'], ['#copal-timeline-modal', 'timeline'],
  ['#copal-todo-modal', 'meatbag-tasks'], ['#chat-container', 'chat'],
];
const routes = new Map([
  ['/', 'shell'], ['/editor', 'editor.explorer'], ['/wiki', 'editor.wiki'], ['/graph', 'editor.graph'],
  ['/files', 'files'], ['/browser', 'browser'], ['/settings', 'settings'], ['/memory', 'memery'],
  ['/treehouse', 'treehouse'], ['/clankshop', 'clankshop'], ['/timeline', 'timeline'],
  ['/todo', 'meatbag-tasks'], ['/calendar', 'calendar'], ['/compare', 'compare'],
  ['/cookbook', 'cookbook'], ['/usage', 'usage'], ['/email', 'email'], ['/tasks', 'clanker-tasks'],
]);
const settingsPanels = new Set([
  'services', 'added-models', 'ai', 'tokenomics', 'search', 'file-access', 'logging',
  'hexes', 'history', 'integrations', 'email', 'reminders', 'appearance', 'shortcuts',
  'help', 'advanced', 'account', 'tools', 'users', 'system', 'buddy', 'memery',
]);
const editorModes = new Map([['notes', 'explorer'], ['wiki', 'wiki'], ['graph', 'graph']]);
const editorViews = new Set(['note', 'markdown', 'wiki', 'source', 'image', 'canvas', 'base', 'workbook', 'slides', 'video']);
const graphModes = new Set(['documents', 'structure', 'galaxy', 'code']);
const menus = new Set(['File', 'Edit', 'Selection', 'View', 'Go', 'Tools', 'Window', 'Help', 'Graph', 'Image', 'Layer', 'Filter']);
const labels = [
  ['#tool-instruction-manual-btn', 'instruction-manual'],
  ['[data-copal-journal-manager]', 'manage-journals'],
  ['[data-copal-launcher="journal"]', 'open-journal'],
  ['#copal-section-toggle', 'copal-section'],
  ['#tools-section > .section-header-flex', 'tools-section'],
  ['#sidebar-new-chat-btn', 'new-chat'],
  ['#chat-workspace-button', 'chat-workspace'],
  ['.chat-meta-overlay', 'chat-title-bar'],
  ['.chat-input-bar', 'chat-composer'],
  ['#chat-history', 'chat-history'],
  ['#settings-annotator', 'annotator-toggle'],
  ['#settings-reset-achievements', 'reset-achievements'],
  ['[data-achievement-notification-enable]', 'enable-system-notifications'],
  ['[data-achievement-notification-test]', 'test-system-notifications'],
  ['[data-achievement-collection-link]', 'open-achievement-collection'],
  ['#settings-logout-btn', 'log-out'],
  ['#settings-full-access', 'full-access'],
  ['#settings-installation-details', 'installed-build-details'],
  ['#settings-show-buddy', 'show-buddy'],
  ['[data-language-select]', 'language-selector'],
  ['#set-copal-manage-journals', 'manage-journals'],
  ['#set-desktopCaptureToggle', 'desktop-capture-toggle'],
  ['#set-desktopControlToggle', 'desktop-control-toggle'],
  ['#user-bar-settings', 'open-settings'],
  ['[data-graph-mode]', 'graph-view-selector'],
  ['[aria-label="Reset graph filters"]', 'reset-graph-filters'],
  ['.copal-editor-graph-body', 'graph-canvas'],
  ['.copal-editor-files-explorer', 'editor-explorer'],
  ['.copal-note-leaf-content', 'document-pane'],
  ['.oc-applet-frame', 'applet-frame'],
  ['.bound-buddy-play', 'buddy'],
  ['[data-contextual-help-button]', 'help'],
];
const regions = [
  ['[data-annotator-settings]', 'annotator-settings'],
  ['[data-achievement-notification-settings]', 'achievement-notifications'],
  ['[data-copal-navigation-appearance-card]', 'copal-navigation'],
  ['[data-logging-settings]', 'logging-settings'],
  ['.copal-editor-graph-sidebar', 'graph-sidebar'],
  ['.copal-editor-graph-body', 'graph-canvas'],
  ['.copal-editor-files-explorer', 'editor-explorer'],
  ['.copal-note-leaf-content', 'document-pane'],
  ['.copal-note-header-actions', 'document-actions'],
  ['.copal-editor-activity-rail', 'activity-rail'],
  ['.files-operation-shelf', 'files-actions'],
  ['.browser-applet-progress-tools', 'report-progress'],
  ['.browser-applet-viewport', 'browser-preview'],
  ['#chat-history', 'chat-history'],
  ['.chat-input-bar', 'chat-composer'],
  ['#copal-section', 'copal-section'],
  ['#tools-section', 'tools-section'],
  ['#sidebar-user-bar', 'account-bar'],
];
const pageRootSelector = pageRoots.map(([selector])=>selector).join(',');
const regionSelector = regions.map(([selector])=>selector).join(',');
const structuralTags = new Set([
  'ARTICLE', 'ASIDE', 'BUTTON', 'DETAILS', 'DIALOG', 'DIV', 'FIELDSET', 'FORM',
  'H1', 'H2', 'H3', 'HEADER', 'INPUT', 'LABEL', 'LI', 'MAIN', 'NAV', 'OL', 'SECTION',
  'SELECT', 'SUMMARY', 'TABLE', 'TBODY', 'TD', 'TEXTAREA', 'TH', 'TR', 'UL', 'A',
]);
const structuralRoles = new Set([
  'button', 'checkbox', 'columnheader', 'combobox', 'dialog', 'grid', 'link',
  'listbox', 'menu', 'menubar', 'menuitem', 'navigation', 'option', 'radio', 'region',
  'row', 'searchbox', 'slider', 'spinbutton', 'switch', 'tab', 'tabpanel', 'textbox',
  'toolbar', 'tree', 'treeitem',
]);
const nativeControls = 'a, button, input, label, select, summary, textarea, [contenteditable], [role]';

function scopeFor(target) {
  const shell = target.closest('#sidebar, #icon-rail, #hamburger-btn');
  if (shell) return {page:'shell.sidebar',root:shell};
  const panel = target.closest('[data-settings-panel]')?.dataset.settingsPanel;
  const tab = target.closest('[data-settings-tab]')?.dataset.settingsTab;
  const settings = target.closest('#settings-modal');
  if (settings) {
    const active = settings.querySelector('[data-settings-tab].active')?.dataset.settingsTab;
    const pane = [panel, tab, active].find(value=>settingsPanels.has(value));
    return {page:pane ? `settings.${pane}` : 'settings', root:target.closest('[data-settings-panel]') || settings};
  }

  const editor = target.closest('[data-editor-mode]');
  if (editor) {
    const helpMode = target.closest('[data-editor-help-mode]')?.dataset.editorHelpMode;
    const mode = helpMode === 'image' ? 'image' : editorModes.get(editor.dataset.editorMode) || 'explorer';
    const graph = editor.querySelector('[data-graph-mode]')?.value;
    const view = target.closest('.copal-note-leaf')?.dataset.viewType;
    const child = mode === 'graph' && graphModes.has(graph) ? graph
      : mode === 'explorer' && editorViews.has(view) ? view : '';
    return {page:child ? `editor.${mode}.${child}` : `editor.${mode}`, root:editor};
  }

  const applet = target.closest(pageRootSelector);
  if (applet) return {page:pageRoots.find(([selector])=>applet.matches(selector))[1],root:applet};
  return {page:routes.get(window.location.pathname) || 'shell',root:target.closest('dialog, .modal, #sidebar') || document.body};
}

function fixedTarget(element) {
  const view = element.dataset.copalView;
  if (['notes','wiki','graph','timeline','treehouse','clankshop','achievements','todo'].includes(view)) {
    return `open-${view === 'notes' ? 'editor' : view === 'todo' ? 'meatbag-tasks' : view}`;
  }
  const editorMode = editorModes.get(element.dataset.editorModeButton);
  if (editorMode) return `mode-${editorMode}`;
  if (settingsPanels.has(element.dataset.settingsTab)) return `open-${element.dataset.settingsTab}`;
  if (menus.has(element.dataset.workbenchMenu)) return `menu-${element.dataset.workbenchMenu.toLowerCase()}`;
  const command = element.dataset.commandId;
  // Workbench assigns these command keys from registered product descriptors.
  if (element.matches('.workbench-command') && /^(?:app|window|editor|files|graph|wiki)\.[a-z][a-z0-9_.-]{0,79}$/.test(command || '')) return command;
  return labels.find(([selector])=>element.matches(selector))?.[1] || '';
}

function kind(element) {
  const role = element.getAttribute('role');
  if (structuralRoles.has(role)) return role;
  return structuralTags.has(element.tagName) ? element.tagName.toLowerCase() : '';
}

function structuralKey(element, root) {
  const key = [];
  const region = element.closest(regionSelector);
  const boundary = region && root.contains(region) ? region : root;
  for (let current = element; current && current !== boundary && key.length < 5; current = current.parentElement) {
    const type = kind(current);
    if (!type) continue;
    const peers = [...(current.parentElement?.children || [])].filter(peer=>kind(peer) === type);
    key.unshift(peers.length > 1 ? `${type}-${peers.indexOf(current) + 1}` : type);
  }
  if (boundary === region) key.unshift(regions.find(([selector])=>region.matches(selector))[1]);
  return key.join('.') || 'pane';
}

export function matchAnnotatorTarget(target) {
  if (!(target instanceof Element) || target.closest('.openclank-annotator, #openclank-annotator-feedback, iframe')) return null;
  const {page,root} = scopeFor(target);
  if (target === document.body || target === document.documentElement) return null;
  let control = null;
  for (let candidate = target; candidate && candidate !== root; candidate = candidate.parentElement) {
    const id = fixedTarget(candidate) || (candidate instanceof HTMLLabelElement && candidate.control ? fixedTarget(candidate.control) : '');
    if (id && !control) return {element:candidate, page, id, label:`${page} · ${id}`};
    if (!control && candidate.matches(nativeControls) && kind(candidate)) control = candidate;
  }
  const element = control || target.closest('header, nav, aside, main, section, article, div') || target;
  const id = structuralKey(element,root);
  return {element, page, id, label:`${page} · ${id}`};
}
