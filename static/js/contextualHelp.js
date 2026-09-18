// Shared contextual help for Open Clank surfaces, Files, and TreeHouse.
//
// Help receives bounded identifiers from the active owner. It never reads
// editor bodies or filesystem paths, so opening a lesson cannot change the
// selected resource or accidentally attach private content to the assistant.

const HELP_EVENT = 'openclank:contextual-help';
const ASSISTANT_EVENT = 'openclank:assistant-context';

const SURFACES = Object.freeze({
  editor: { label: 'Editor', lesson: 'fg-editor', description: 'Write, link, and save a note in the active workspace.', next: 'Open a note or create a disposable one, then save it.' },
  files: { label: 'Files', lesson: 'fg-files', description: 'Browse authorized locations and open a resource in Editor.', next: 'Choose an authorized location, then select one file.' },
  assistant: { label: 'Assistant', lesson: 'fg-assistant', description: 'Ask for help with the current workflow while keeping resource contents in the workspace.', next: 'Attach only the current identifiers, then review the scope before sending.' },
  timeline: { label: 'Timeline', lesson: 'fg-timeline', description: 'Plan work with dated notes, spans, and relationships.', next: 'Open a day or task and inspect its linked note.' },
  wiki: { label: 'Wiki', lesson: 'fg-wiki', description: 'Arrange authored stories and preserve their source content.', next: 'Open a story card, then follow a link or edit its source.' },
  bases: { label: 'Bases', lesson: 'fg-bases', description: 'Query notes with typed properties and keep the source document available.', next: 'Open a Base view and inspect one typed result.' },
  graph: { label: 'Graph', lesson: 'fg-connections', description: 'Explore links between the resources in this workspace.', next: 'Select a node to follow its source relationship.' },
  tasks: { label: 'Tasks', lesson: 'fg-tasks', description: 'Review actionable checkboxes projected from your notes.', next: 'Filter the list, then open the source note for an edit.' },
  settings: { label: 'Settings', lesson: 'fg-settings', description: 'Change reversible workspace preferences and confirm their account scope.', next: 'Change one preference, reload, and verify its owner.' },
  continuity: { label: 'Memory and history', lesson: 'fg-continuity', description: 'Inspect checkpoints and recovery state without exposing document contents.', next: 'Open the current history status and record the checkpoint boundary.' },
  teaching: { label: 'TreeHouse authoring', lesson: 'fg-teaching', description: 'Author and preview a disposable lesson before publishing it.', next: 'Open the Field Guide authoring lesson and preview its draft.' },
  models: { label: 'Models and Compare', lesson: 'fg-models', description: 'Compare available model entries using explicit provider metadata.', next: 'Compare two recorded choices and save the reason.' },
  automation: { label: 'Automation', lesson: 'fg-automation', description: 'Inspect a disposable scheduled task and cancel it before delivery.', next: 'Open the task payload and confirm its cancelled state.' },
  research: { label: 'Research and Gallery', lesson: 'fg-research-media', description: 'Keep a supplied source connected to a reversible media practice.', next: 'Open the source packet and inspect its media reference.' },
  communications: { label: 'Email and Calendar', lesson: 'fg-communications', description: 'Draft local communication and calendar records without external delivery.', next: 'Inspect the local draft and delivery boundary.' },
  operations: { label: 'Operations', lesson: 'fg-operations', description: 'Run safe diagnostics and export only the disposable practice project.', next: 'Review diagnostics, then inspect the scoped export manifest.' },
  treehouse: { label: 'TreeHouse', lesson: 'fg-orientation', description: 'Learn the workflow in a private Field Guide course.', next: 'Open a lesson and use its disposable practice seed.' },
});

const SURFACE_ALIASES = Object.freeze({ notes: 'editor', chat: 'assistant', todo: 'tasks', 'meatbag-tasks': 'tasks', 'memory-history': 'continuity', 'treehouse-admin': 'teaching', 'research-media': 'research', 'email-calendar': 'communications', treehouse: 'treehouse' });
const INVALID_SOURCE_STATUSES = new Set(['stale', 'private', 'revoked', 'unavailable']);
let dialog = null;
let returnFocus = null;
let observer = null;
let dialogSerial = 0;
let scopeRevision = 0;
let pinnedContext = null;
let pendingAssistantContext = null;
let assistantClaim = null;
let assistantClaimSerial = 0;

function surfaceKey(value) {
  const key = String(value || '').trim().toLowerCase();
  return SURFACES[key] ? key : SURFACE_ALIASES[key] || 'editor';
}

function sourceStatus(value) {
  const status = String(value || '').trim().toLowerCase();
  return INVALID_SOURCE_STATUSES.has(status) ? status : 'ready';
}

function safeId(value, max = 160) {
  const result = String(value ?? '').trim();
  return result ? result.slice(0, max) : null;
}

function safeQuery(value, max = 512) {
  const result = String(value ?? '').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '').trim();
  return result ? result.slice(0, max) : null;
}

function mountedWindowVisible(id) {
  const root = document.getElementById(id);
  if (!root) return false;
  if (root.__openClankWindow?.visible === true || root.__copalWindow?.visible === true) return true;
  return !root.classList?.contains('hidden') && root.getAttribute('aria-hidden') !== 'true' && root.style?.display !== 'none';
}

function currentScope(preferredSurface = null) {
  let copal = {};
  let files = {};
  try { copal = window.__odysseusGetActiveCopalContext?.() || {}; } catch (_) {}
  const filesVisible = mountedWindowVisible('files-window');
  if (preferredSurface === 'files' || filesVisible) {
    try { files = window.__odysseusGetActiveFilesContext?.() || {}; } catch (_) {}
  }
  // A Files getter is available before its window is mounted and returns a
  // harmless default workspace. Only use it for a Files attachment or while
  // the Files window is actually visible; Editor/Timeline/TreeHouse remain
  // scoped to Copal's canonical active-window getter.
  const active = preferredSurface === 'files'
    ? files
    : preferredSurface
      ? copal
      : (!copal.view && filesVisible ? files : copal);
  const accountId = safeId(document.body?.dataset?.accountId || active.accountId || copal.accountId || files.accountId || '', 128);
  const workspace = safeId(active.workspace || copal.workspace || files.workspace || 'default', 64) || 'default';
  return { accountId, workspace, revision: scopeRevision };
}

export function normalizeHelpContext(input = {}) {
  const surfaceValue = input.surface && typeof input.surface === 'object' ? input.surface.key : input.surface;
  const surface = surfaceKey(surfaceValue || input.view || input.resourceKind);
  const spec = SURFACES[surface];
  const scope = currentScope(surface);
  const revision = Number.isInteger(Number(input.scopeRevision))
    ? Number(input.scopeRevision)
    : scope.revision;
  return Object.freeze({
    accountId: safeId(input.accountId ?? scope.accountId, 128),
    workspace: safeId(input.workspace ?? scope.workspace, 64) || 'default',
    scopeRevision: revision >= 0 && revision <= 2147483647 ? revision : scope.revision,
    surface,
    view: safeId(input.view) || surface,
    resourceKind: safeId(input.resourceKind) || null,
    resourceId: safeId(input.resourceId),
    resourceRef: safeId(input.resourceRef, 16384),
    pinnedResourceId: safeId(input.pinnedResourceId),
    pinnedResourceRef: safeId(input.pinnedResourceRef, 16384),
    pinned: input.pinned === true,
    selection: safeId(input.selection),
    baseId: safeId(input.baseId),
    baseQuery: safeQuery(input.baseQuery),
    taskId: safeId(input.taskId),
    taskQuery: safeQuery(input.taskQuery),
    courseId: safeId(input.courseId),
    lessonId: safeId(input.lessonId) || spec.lesson,
    lessonTitle: safeId(input.lessonTitle, 200),
    sourceStatus: sourceStatus(input.sourceStatus),
  });
}

function scopeMatches(context, scope) {
  if (!context) return false;
  scope = scope || currentScope(context.surface);
  if (context.accountId && scope.accountId && context.accountId !== scope.accountId) return false;
  if (context.workspace && scope.workspace && context.workspace !== scope.workspace) return false;
  return context.scopeRevision === scope.revision;
}

function attachable(context) {
  return !!context && !INVALID_SOURCE_STATUSES.has(context.sourceStatus) && scopeMatches(context);
}

function providerContext(surface) {
  try {
    if (surface === 'files') return window.__odysseusGetActiveFilesContext?.() || {};
    const help = window.__odysseusGetActiveCopalHelpContext;
    if (typeof help === 'function') return help(surface) || {};
    const copal = window.__odysseusGetActiveCopalContext?.() || {};
    if (surface || copal.view || !mountedWindowVisible('files-window')) return copal;
    return window.__odysseusGetActiveFilesContext?.() || copal;
  } catch (_) {
    return { sourceStatus: 'unavailable' };
  }
}

function activeContext(surface = null, root = null) {
  const requested = surface ? surfaceKey(surface) : null;
  let context = { ...providerContext(requested || null) };
  if (requested) {
    const providerView = String(context.view || '').trim();
    context = {
      ...context,
      surface: requested,
      // Copal's Notes provider calls the user-facing Editor surface `notes`;
      // retain that canonical view while other requested surfaces map 1:1.
      view: requested === 'editor' && providerView === 'notes' ? providerView : requested,
    };
  }
  if (root) {
    const selected = root.querySelector?.(
      '[data-resource-ref][aria-selected="true"], [data-resource-ref].selected,' +
      '[data-document-id][aria-selected="true"], [data-document-id].selected,' +
      '.files-entry.selected, [data-copal-context-object].selected',
    );
    const dataset = selected?.dataset || {};
    context.resourceId = dataset.resourceId || dataset.documentId || context.resourceId;
    context.resourceRef = dataset.resourceRef || context.resourceRef;
    context.resourceKind = dataset.resourceKind || context.resourceKind;
    context.selection = dataset.selection || context.selection;
    context.baseId = dataset.baseId || context.baseId;
    context.baseQuery = dataset.baseQuery || context.baseQuery;
    context.taskId = dataset.taskId || context.taskId;
    context.taskQuery = dataset.taskQuery || context.taskQuery;
    context.courseId = dataset.courseId || context.courseId;
    context.lessonId = dataset.lessonId || dataset.fieldGuideLesson || context.lessonId;
    context.lessonTitle = dataset.lessonTitle || context.lessonTitle;
  }
  const normalized = normalizeHelpContext(context);
  return scopeMatches(normalized) ? normalized : normalizeHelpContext({
    ...normalized,
    resourceId: null,
    pinnedResourceId: null,
    selection: null,
    baseId: null,
    baseQuery: null,
    taskId: null,
    taskQuery: null,
    sourceStatus: 'stale',
  });
}

function lessonHref(context) {
  const params = new URLSearchParams({ lesson: context.lessonId });
  if (context.workspace) params.set('workspace', context.workspace);
  return `/copal/treehouse?${params.toString()}`;
}

function clearContextState() {
  pinnedContext = null;
  pendingAssistantContext = null;
  assistantClaim = null;
  document.querySelectorAll('[data-copal-help-attachment]').forEach((node) => node.remove());
  if (dialog) closeHelp();
}

function stagedContext(context) {
  const normalized = normalizeHelpContext(context);
  return attachable(normalized) ? normalized : null;
}

function renderAssistantAttachment(context) {
  const bar = document.querySelector('.chat-input-bar');
  if (!bar) return;
  bar.querySelector('[data-copal-help-attachment]')?.remove();
  const chip = document.createElement('div');
  chip.className = 'openclank-assistant-context';
  chip.__openClankHelpContext = context;
  chip.dataset.copalHelpAttachment = '1';
  chip.setAttribute('role', 'status');
  chip.setAttribute('aria-live', 'polite');
  const label = document.createElement('span');
  label.className = 'openclank-assistant-context-label';
  const spec = SURFACES[context.surface];
  label.textContent = `Help context · ${spec.label}${context.resourceId ? ` · ${context.resourceId}` : ''}`;
  const remove = document.createElement('button');
  remove.type = 'button';
  remove.className = 'openclank-assistant-context-remove';
  remove.textContent = 'Remove';
  remove.setAttribute('aria-label', 'Remove attached help context');
  remove.addEventListener('click', () => {
    // A request may have claimed this exact context. Removing the chip is an
    // explicit user revocation, so a later failed request must not release
    // that claim back into the composer.
    if (assistantClaim?.context === pendingAssistantContext || assistantClaim?.context === context) assistantClaim = null;
    pendingAssistantContext = null;
    chip.remove();
    window.dispatchEvent(new CustomEvent('openclank:assistant-context-cleared'));
  });
  chip.append(label, remove);
  const input = document.getElementById('message');
  const row = input?.closest?.('.chat-input-row');
  // The composer input is often nested inside a row; insertBefore only accepts
  // a direct child. Falling back to prepend keeps the attachment visible in
  // compact/custom composer layouts instead of throwing and losing context.
  if (row?.parentNode === bar) bar.insertBefore(chip, row);
  else bar.prepend(chip);
}

function stageAssistantContext(context) {
  const staged = stagedContext(context);
  if (!staged) return null;
  // A new explicit attachment supersedes an in-flight claim. The old sender
  // can no longer restore or commit this newer context because its claim
  // token will not match.
  assistantClaim = null;
  pendingAssistantContext = staged;
  renderAssistantAttachment(staged);
  window.dispatchEvent(new CustomEvent(ASSISTANT_EVENT, { detail: staged }));
  return staged;
}

export function peekAssistantContext() {
  if (!pendingAssistantContext) return null;
  if (!attachable(pendingAssistantContext)) {
    pendingAssistantContext = null;
    assistantClaim = null;
    document.querySelectorAll('[data-copal-help-attachment]').forEach((node) => node.remove());
    return null;
  }
  return pendingAssistantContext;
}

export function claimAssistantContext() {
  const context = peekAssistantContext();
  if (!context || assistantClaim) return null;
  assistantClaim = Object.freeze({ context, token: `help-claim-${++assistantClaimSerial}` });
  return assistantClaim;
}

function resolveAssistantClaim(input) {
  if (input && typeof input === 'object' && input.context && input.token) {
    return input === assistantClaim ? input : null;
  }
  // Keep the old context argument shape working for callers that do not have
  // overlapping sends. Chat uses claim objects so concurrent requests cannot
  // consume the same attachment.
  if (input && input === assistantClaim?.context) return assistantClaim;
  if (input && input === pendingAssistantContext && !assistantClaim) return { context: input, token: null };
  return null;
}

export function commitAssistantContext(input) {
  const claim = resolveAssistantClaim(input);
  if (!claim || pendingAssistantContext !== claim.context) return null;
  const context = claim.context;
  assistantClaim = null;
  pendingAssistantContext = null;
  document.querySelectorAll('[data-copal-help-attachment]').forEach((node) => {
    if (!node.__openClankHelpContext || node.__openClankHelpContext === context) node.remove();
  });
  return context;
}

export function restoreAssistantContext(input) {
  if (!assistantClaim && input && input === pendingAssistantContext && attachable(input)) return input;
  const claim = resolveAssistantClaim(input);
  const context = claim?.context;
  if (!claim || !context || !attachable(context)) return null;
  if (assistantClaim !== claim) return null;
  assistantClaim = null;
  if (pendingAssistantContext === context) return context;
  if (pendingAssistantContext) return null;
  pendingAssistantContext = context;
  renderAssistantAttachment(context);
  return context;
}

export function consumeAssistantContext() {
  const claim = claimAssistantContext();
  return commitAssistantContext(claim);
}

export function getPendingAssistantContext() {
  return pendingAssistantContext && attachable(pendingAssistantContext) ? pendingAssistantContext : null;
}

function closeHelp({ restore = true } = {}) {
  if (!dialog) return;
  if (!restore) returnFocus = null;
  dialog.close();
}

function openHelp(input = {}, { focus = true } = {}) {
  const supplied = normalizeHelpContext({ ...activeContext(), ...input });
  const context = scopeMatches(supplied) && !INVALID_SOURCE_STATUSES.has(supplied.sourceStatus)
    ? supplied
    : normalizeHelpContext({
      ...supplied,
      resourceId: null,
      resourceRef: null,
      selection: null,
      sourceStatus: INVALID_SOURCE_STATUSES.has(supplied.sourceStatus) ? supplied.sourceStatus : 'stale',
    });
  const spec = SURFACES[context.surface];
  if (!document.body) return context;
  // Keep the original opener when a help dialog is replaced from inside the
  // current dialog (pin/reopen). The focused pin button is about to be
  // removed, so treating it as the next return target would strand focus.
  const priorReturnFocus = returnFocus;
  const focusedOutsideCurrent = document.activeElement?.isConnected &&
    (!dialog || !dialog.contains(document.activeElement));
  closeHelp();
  if (focusedOutsideCurrent) returnFocus = document.activeElement;
  else if (priorReturnFocus?.isConnected) returnFocus = priorReturnFocus;
  else returnFocus = null;
  const serial = ++dialogSerial;
  const titleId = `openclank-help-title-${serial}`;
  const descriptionId = `openclank-help-description-${serial}`;
  const helpDialog = document.createElement('dialog');
  dialog = helpDialog;
  helpDialog.className = 'openclank-contextual-help copal-dialog';
  helpDialog.setAttribute('aria-labelledby', titleId);
  helpDialog.setAttribute('aria-describedby', descriptionId);
  helpDialog.innerHTML = `
    <header class="openclank-help-header">
      <div><p class="openclank-help-kicker">Field Guide · ${spec.label}</p><h2 id="${titleId}">Help for ${spec.label}</h2></div>
      <button type="button" class="close-btn" data-help-close aria-label="Close contextual help">×</button>
    </header>
    <p id="${descriptionId}" class="openclank-help-description"></p>
    <p class="openclank-help-state" data-help-state role="status" aria-live="polite"></p>
    <dl class="openclank-help-context">
      <div><dt>Workspace</dt><dd data-help-workspace></dd></div>
      <div><dt>Active resource</dt><dd data-help-resource></dd></div>
      <div><dt>Pinned resource</dt><dd data-help-pinned></dd></div>
      <div><dt>Selection</dt><dd data-help-selection></dd></div>
      <div><dt>Base query</dt><dd data-help-base-query></dd></div>
      <div><dt>Task</dt><dd data-help-task></dd></div>
      <div><dt>Lesson</dt><dd data-help-lesson-context></dd></div>
    </dl>
    <p class="openclank-help-next"><strong>Try this</strong> <span data-help-next></span></p>
    <div class="copal-dialog-actions">
      <a class="copal-btn primary" data-help-lesson target="_self">Open lesson</a>
      <button type="button" class="copal-btn" data-help-pin></button>
      <button type="button" class="copal-btn" data-help-ask>Attach to assistant</button>
      <button type="button" class="copal-btn" data-help-close>Close</button>
    </div>`;
  helpDialog.querySelector('.openclank-help-description').textContent = spec.description;
  helpDialog.querySelector('[data-help-workspace]').textContent = context.workspace;
  helpDialog.querySelector('[data-help-resource]').textContent = context.resourceId || 'None selected';
  helpDialog.querySelector('[data-help-pinned]').textContent = pinnedContext?.resourceId || context.pinnedResourceId || 'None pinned';
  helpDialog.querySelector('[data-help-selection]').textContent = context.selection || 'No explicit selection';
  helpDialog.querySelector('[data-help-base-query]').textContent = context.baseQuery || 'None attached';
  helpDialog.querySelector('[data-help-task]').textContent = context.taskId || context.taskQuery || 'None selected';
  helpDialog.querySelector('[data-help-lesson-context]').textContent = context.lessonTitle || context.lessonId || 'Field Guide overview';
  helpDialog.querySelector('[data-help-next]').textContent = spec.next;
  const state = helpDialog.querySelector('[data-help-state]');
  if (context.sourceStatus === 'stale') state.textContent = 'This selection changed with the workspace. Choose the current resource before attaching it.';
  else if (context.sourceStatus === 'unavailable') state.textContent = 'The active surface is unavailable. The Field Guide lesson is still available.';
  else if (!context.resourceId && !context.selection) state.textContent = 'Nothing is selected yet. Open a resource for more specific help.';
  else state.textContent = 'Only the identifiers and fields shown below are attached; document contents stay in the workspace.';
  const lesson = helpDialog.querySelector('[data-help-lesson]');
  lesson.href = lessonHref(context);
  lesson.setAttribute('aria-label', `Open ${spec.label} Field Guide lesson`);
  const pin = helpDialog.querySelector('[data-help-pin]');
  const pinned = pinnedContext && scopeMatches(pinnedContext) && pinnedContext.resourceId === context.resourceId;
  pin.textContent = pinned ? 'Unpin resource' : 'Pin resource';
  pin.disabled = !context.resourceId || INVALID_SOURCE_STATUSES.has(context.sourceStatus);
  pin.setAttribute('aria-pressed', String(pinned));
  pin.addEventListener('click', () => {
    if (!context.resourceId) return;
    pinnedContext = pinned ? null : normalizeHelpContext({ ...context, pinned: true, pinnedResourceId: context.resourceId });
    openHelp({
      ...context,
      pinnedResourceId: pinnedContext?.resourceId || null,
      pinnedResourceRef: pinnedContext?.resourceRef || null,
      pinned: !!pinnedContext,
    }, { focus: false });
  });
  helpDialog.querySelectorAll('[data-help-close]').forEach((button) => button.addEventListener('click', closeHelp));
  const ask = helpDialog.querySelector('[data-help-ask]');
  ask.disabled = INVALID_SOURCE_STATUSES.has(context.sourceStatus);
  ask.title = ask.disabled ? 'Choose the current resource before attaching help context' : 'Attach identifiers to the assistant';
  ask.addEventListener('click', () => {
    stageAssistantContext({
      ...context,
      pinnedResourceId: pinnedContext?.resourceId || context.pinnedResourceId || null,
      pinnedResourceRef: pinnedContext?.resourceRef || context.pinnedResourceRef || context.resourceRef || null,
      pinned: !!pinnedContext,
    });
    const message = document.getElementById('message');
    closeHelp({ restore: false });
    message?.focus({ preventScroll: true });
  });
  helpDialog.addEventListener('cancel', (event) => { event.preventDefault(); closeHelp(); });
  helpDialog.addEventListener('close', () => {
    // Capture this instance. A pin/reopen can replace the module's global
    // dialog before the browser delivers the old close event.
    const wasCurrent = dialog === helpDialog;
    helpDialog.remove();
    if (!wasCurrent) return;
    dialog = null;
    if (returnFocus?.isConnected && !helpDialog.contains(document.activeElement)) returnFocus.focus({ preventScroll: true });
    returnFocus = null;
  }, { once: true });
  document.body.append(helpDialog);
  helpDialog.showModal();
  if (focus) helpDialog.querySelector('[data-help-close]')?.focus();
  return context;
}

function contextForButton(button) {
  const surface = surfaceKey(button.dataset.helpSurface);
  const root = button.closest('.copal-view-window, .files-window');
  return activeContext(surface, root);
}

function addSurfaceButtons(root = document) {
  root.querySelectorAll?.('.copal-workspace-header .copal-window-actions').forEach((actions) => {
    const windowRoot = actions.closest('.copal-view-window, .files-window');
    if (!windowRoot || actions.querySelector('[data-contextual-help-button]')) return;
    const id = windowRoot.id || '';
    let surface = id === 'files-window' ? 'files' : id.replace(/^copal-/, '').replace(/-modal$/, '');
    surface = surfaceKey(surface);
    const button = document.createElement('button');
    button.type = 'button'; button.className = 'copal-btn contextual-help-button'; button.textContent = '?';
    button.title = `Help for ${SURFACES[surface].label}`; button.setAttribute('aria-label', `Help for ${SURFACES[surface].label}`);
    button.dataset.contextualHelpButton = '1'; button.dataset.helpSurface = surface;
    button.addEventListener('click', () => openHelp(contextForButton(button)));
    actions.prepend(button);
  });
}

function handleHelpEvent(event) {
  if (event.__openClankHelpHandled) return;
  event.__openClankHelpHandled = true;
  openHelp(event.detail || {});
}

export function installContextualHelp() {
  if (window.__openClankContextualHelpInstalled) return;
  window.__openClankContextualHelpInstalled = true;
  window.__openClankTreeHouseHelp = openHelp;
  window.__openClankConsumeAssistantContext = consumeAssistantContext;
  window.addEventListener(HELP_EVENT, handleHelpEvent);
  document.addEventListener(HELP_EVENT, handleHelpEvent);
  const scopeChanged = () => { scopeRevision += 1; clearContextState(); };
  window.addEventListener('openclank:auth-context-changed', scopeChanged);
  document.addEventListener('openclank:auth-context-changed', scopeChanged);
  window.addEventListener('workspace-change', scopeChanged);
  // Providers can revoke or hide a resource without changing the account or
  // workspace. Clear pending chips immediately so a failed Assistant request
  // cannot restore private or revoked context.
  for (const eventName of ['openclank:resource-revoked', 'openclank:resource-private', 'openclank:resource-unavailable']) {
    window.addEventListener(eventName, scopeChanged);
    document.addEventListener(eventName, scopeChanged);
  }
  document.addEventListener('click', (event) => {
    const button = event.target.closest?.('[data-contextual-help-open]');
    if (button) openHelp(contextForButton(button));
  });
  document.addEventListener('keydown', (event) => {
    // `?` is normally produced with Shift on US keyboards. Modifier guards
    // exclude alternate command chords while allowing that primary key.
    if (event.key !== '?' || event.altKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    if (target?.matches?.('input, textarea, select, [contenteditable="true"]')) return;
    event.preventDefault(); openHelp();
  });
  addSurfaceButtons();
  observer = new MutationObserver((mutations) => mutations.forEach((mutation) => mutation.addedNodes.forEach((node) => { if (node.nodeType === 1) addSurfaceButtons(node); })));
  observer.observe(document.body, { childList: true, subtree: true });
}

export { SURFACES, openHelp, stageAssistantContext };

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', installContextualHelp, { once: true });
  else installContextualHelp();
}
