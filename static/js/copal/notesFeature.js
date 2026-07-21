import {
  NOTES_PANELS,
  activateWorkspaceLeaf,
  closeWorkspaceGroup,
  closeWorkspaceLeaf,
  closeWorkspaceOtherLeaves,
  findWorkspaceGroup,
  findWorkspaceLeaf,
  groupForLeaf,
  moveWorkspaceLeaf,
  normalizeNotesSettings,
  normalizeNotesWorkspace,
  noteViewType,
  openWorkspaceDocument,
  resizeWorkspaceSplit,
  serializeNotesWorkspace,
  setWorkspaceLeafMode,
  setWorkspacePanelPlacement,
  splitWorkspaceGroup,
  workspaceGroups,
  workspaceLeaves,
  workspacePanelsForSide,
} from './notesWorkspace.js';
import {
  coercePropertyValue,
  databaseRelations,
  fuzzyScore,
  linkedMentions,
  moveHeadingSection,
  moveHeadingSectionTo,
  moveFrontmatterProperty,
  outlineEntries,
  parseCanvasDocument,
  parseFrontmatter,
  propertyType,
  renameFrontmatterProperty,
  removeFrontmatterProperty,
  resolveDocumentLink,
  setFrontmatterProperty,
  unlinkedMentions,
  wordCount,
} from './notesModel.js';
import { wireDialog, wirePopover } from './overlays.js';
import { registerMenuDismiss } from '../escMenuStack.js';
import { parseTable, createTableWidget, applyTableEdit } from './tableModel.js';

const OPERATIONAL_KINDS = new Set(['planning', 'calendar-projection', 'treehouse-state', 'copal-tracks', 'copal-migration']);
const PROPERTY_TYPES = ['text', 'list', 'number', 'checkbox', 'date', 'datetime', 'tags', 'object'];
const TIMELINE_DOCUMENT = Object.freeze({
  id:'copal:timeline', name:'Timeline', kind:'timeline', virtual:true, text:'', properties:{}, tags:[], links:[],
});
const NOTES_NARROW_QUERY = '(max-width: 760px)';
const NOTES_COMPACT_QUERY = '(max-width: 1100px)';

export function createNotesFeature({
  h, api, state, createMarkdownEditor, renderMarkdown, formatBaseCell,
  saveDocument, renameNote, deleteDocument, showHistory, showTrash, showForm,
  importVault, loadDocuments, openDocument:openOtherView, persistActiveContext, deleteDocuments,
  activateNotes, renderTimeline, openEventEditor,
}) {
  let persistTimer = null;

  function context() {
    return state.windows.get('notes');
  }

  function shellViewport() {
    return {
      narrow:window.matchMedia(NOTES_NARROW_QUERY).matches,
      compact:window.matchMedia(NOTES_COMPACT_QUERY).matches,
    };
  }

  function toggleSidebar(side) {
    const current = context();
    const workspace = ensureWorkspace();
    if (!current || !workspace) return;
    const viewport = shellViewport();
    const drawer = viewport.narrow || (side === 'right' && viewport.compact);
    if (drawer) current.noteDrawer = current.noteDrawer === side ? null : side;
    else {
      workspace[side].open = !workspace[side].open;
      persist(true);
    }
    render();
  }

  function ensureShellControls() {
    const current = context();
    if (!current) return null;
    if (!current.noteShellControls) {
      const make = (side) => h('button', {
        id:`copal-notes-${side}-sidebar-toggle`,
        type:'button',
        class:`copal-btn copal-shell-toggle ${side}`,
        'data-shell-side':side,
        'aria-controls':`copal-notes-${side}-sidebar`,
        onclick:() => toggleSidebar(side),
      });
      current.noteShellControls = { left:make('left'), right:make('right') };
    }
    return current.noteShellControls;
  }

  function syncShellControl(button, side, expanded, slot) {
    const noun = side === 'left' ? 'files' : 'details';
    const action = expanded ? 'Collapse' : 'Expand';
    button.textContent = slot === 'sidebar' ? `Hide ${noun}` : side === 'left' ? 'Files' : 'Details';
    button.title = `${action} Notes ${noun}`;
    button.setAttribute('aria-label', `${action} Notes ${noun}`);
    button.setAttribute('aria-expanded', String(expanded));
    button.dataset.shellSlot = slot;
  }

  function finishShellControlMove(controls, before, focusedSide) {
    requestAnimationFrame(() => {
      const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      for (const side of ['left', 'right']) {
        const button = controls[side];
        if (focusedSide === side) button.focus({ preventScroll:true });
        const previous = before[side];
        if (!previous || reduced || !button.isConnected) continue;
        const next = button.getBoundingClientRect();
        const dx = previous.left - next.left; const dy = previous.top - next.top;
        if (Math.abs(dx) < 1 && Math.abs(dy) < 1) continue;
        button.style.pointerEvents = 'none';
        const animation = button.animate(
          [{ transform:`translate(${dx}px, ${dy}px)` }, { transform:'translate(0, 0)' }],
          { duration:180, easing:'cubic-bezier(.2,.8,.2,1)' },
        );
        animation.finished.catch(() => {}).finally(() => { button.style.pointerEvents = ''; });
      }
    });
  }

  function documents() {
    return state.docs.filter((doc) => !OPERATIONAL_KINDS.has(doc.kind));
  }

  // Documents visible in the explorer: excludes operational kinds and,
  // when dot-folders are hidden, documents whose path starts with a dot-folder.
  function explorerDocs() {
    const workspace = ensureWorkspace();
    const docs = documents();
    if (workspace?.left?.showDotFolders) return docs;
    return docs.filter((doc) => {
      // Hide by path (.events/, .copal/, etc.)
      const parts = (doc.name || '').split('/');
      if (parts.some((part) => part.startsWith('.'))) return false;
      // Hide by kind (copal-event is always in .events)
      if (doc.kind === 'copal-event') return false;
      return true;
    });
  }

  // Check if a document lives in a dot-folder (hidden by the explorer toggle).
  function isHiddenDoc(doc) {
    const parts = (doc.name || '').split('/');
    return parts.some((part) => part.startsWith('.'));
  }

  function workspaceDocuments() {
    return [...documents(), TIMELINE_DOCUMENT];
  }

  function ensureWorkspace() {
    const current = context();
    if (!current) return null;
    const docs = workspaceDocuments();
    const signature = docs.map((doc) => doc.id).sort().join(':');
    const currentLeaf = current.noteWorkspace ? findWorkspaceLeaf(current.noteWorkspace) : null;
    const requested = current.selected ?? null;
    if (
      current.noteWorkspace
      && !current.noteSaved
      && current.noteDocsSignature === signature
      && (!requested || currentLeaf?.docId === requested)
    ) return current.noteWorkspace;
    const source = current.noteWorkspace || current.noteSaved || {};
    current.noteWorkspace = normalizeNotesWorkspace(source, docs, requested);
    current.noteDocsSignature = signature;
    current.noteSaved = null;
    current.noteLeafViews ||= new Map();
    current.noteDrafts ||= new Map();
    current.noteSaveRuns ||= new Map();
    current.noteSelection ||= new Set();
    current.noteMetrics ||= { renders:0, editorConstructions:0, lastRenderMs:0 };
    const leaf = findWorkspaceLeaf(current.noteWorkspace);
    current.selected = leaf?.docId || null;
    return current.noteWorkspace;
  }

  function persist(immediate = false) {
    const current = context();
    if (!current?.noteWorkspace) return;
    const write = () => {
      persistTimer = null;
      localStorage.setItem(`odysseus-copal-notes-layout:${state.workspace}`, serializeNotesWorkspace(current.noteWorkspace));
    };
    clearTimeout(persistTimer);
    if (immediate) write();
    else persistTimer = setTimeout(write, 80);
  }

  function activeLeaf(workspace = ensureWorkspace()) {
    return workspace ? findWorkspaceLeaf(workspace) : null;
  }

  function activeDoc(workspace = ensureWorkspace()) {
    const leaf = activeLeaf(workspace);
    return leaf ? workspaceDocuments().find((doc) => doc.id === leaf.docId) || null : null;
  }

  function inspectorDoc(workspace) {
    const id = workspace.right.pinnedDocId || activeLeaf(workspace)?.docId;
    return workspaceDocuments().find((doc) => doc.id === id) || null;
  }

  function setActive(groupId, leafId) {
    const workspace = ensureWorkspace();
    if (!workspace || !activateWorkspaceLeaf(workspace, groupId, leafId)) return;
    const leaf = findWorkspaceLeaf(workspace, leafId);
    context().selected = leaf?.docId || null;
    state.selected = context().selected;
    persistActiveContext();
    persist(true);
    render();
  }

  function open(id, options = {}) {
    const doc = workspaceDocuments().find((item) => item.id === id);
    const workspace = ensureWorkspace();
    if (!doc || !workspace) return null;
    // Route copal-event documents to the native event editor.
    if (noteViewType(doc) === 'event' && openEventEditor) {
      openEventEditor(doc.id);
      return null;
    }
    let leaf = doc.virtual ? workspaceLeaves(workspace).find((item) => item.docId === doc.id) : null;
    if (leaf) {
      const group = groupForLeaf(workspace, leaf.id);
      if (group) activateWorkspaceLeaf(workspace, group.id, leaf.id);
    } else leaf = openWorkspaceDocument(workspace, doc, options);
    revealInExplorer(doc, workspace);
    context().selected = id;
    state.selected = id;
    persistActiveContext();
    persist(true);
    render();
    return leaf;
  }

  function revealInExplorer(doc, workspace = ensureWorkspace()) {
    const parts = String(doc?.name || '').split('/').slice(0, -1);
    for (let index = 1; index <= parts.length; index += 1) {
      const path = parts.slice(0, index).join('/');
      if (!workspace.left.expanded.includes(path)) workspace.left.expanded.push(path);
    }
  }

  function disposeLeaf(leafId) {
    const current = context();
    const cache = current?.noteLeafViews?.get(leafId);
    if (!cache) return;
    cache.editor?.destroy();
    state.noteEditors.delete(cache.editor);
    current.noteLeafViews.delete(leafId);
  }

  async function saveDraft(docId) {
    const current = context();
    const running = current?.noteSaveRuns?.get(docId);
    if (running) {
      const succeeded = await running;
      return succeeded && current.noteDrafts.has(docId) ? saveDraft(docId) : succeeded;
    }
    const draft = current?.noteDrafts?.get(docId);
    if (!draft) return true;
    const run = (async () => {
      clearTimeout(state.saveTimers.get(docId));
      state.saveTimers.delete(docId);
      const latest = state.docs.find((doc) => doc.id === docId) || draft.doc;
      const guarded = { ...latest, head:draft.base };
      setLeafSaveState(docId, 'saving');
      const saved = await saveDocument(guarded, draft.value, false, 'notes');
      const queued = current.noteDrafts.get(docId);
      if (!saved) { setLeafSaveState(docId, 'conflict'); return false; }
      const fresh = state.docs.find((doc) => doc.id === docId) || guarded;
      for (const cache of current.noteLeafViews.values()) if (cache.docId === docId) cache.doc = fresh;
      if (queued?.value === draft.value) current.noteDrafts.delete(docId);
      else if (queued) { queued.base = fresh.head; queued.doc = fresh; }
      setLeafSaveState(docId, current.noteDrafts.has(docId) ? 'unsaved' : 'saved');
      return true;
    })();
    current.noteSaveRuns.set(docId, run);
    let succeeded = false;
    try { succeeded = await run; } finally { if (current.noteSaveRuns.get(docId) === run) current.noteSaveRuns.delete(docId); }
    return succeeded && current.noteDrafts.has(docId) ? saveDraft(docId) : succeeded;
  }

  function queueSave(doc, value) {
    const current = context();
    const existing = current.noteDrafts.get(doc.id);
    const indexed = state.docs.find((item) => item.id === doc.id) || doc;
    current.noteDrafts.set(doc.id, { doc:indexed, value, base:existing?.base || indexed.head });
    clearTimeout(state.saveTimers.get(doc.id));
    state.saveTimers.set(doc.id, setTimeout(() => saveDraft(doc.id), 700));
    setLeafSaveState(doc.id, 'unsaved');
  }

  function syncDocumentEditors(docId, value, source = null) {
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId || cache.editor === source) continue;
      cache.editor?.setValue(value);
      if (cache.reading?.isConnected) { cache.reading.replaceChildren(renderMarkdown(value, new Set([docId]))); applyCompletedVisibility(cache.reading); }
      if (cache.preview && !cache.preview.hidden) { cache.preview.replaceChildren(renderMarkdown(value, new Set([docId]))); applyCompletedVisibility(cache.preview); }
    }
  }

  function applyDocumentSource(doc, content, selection = null) {
    const current = context();
    const leaf = workspaceLeaves(ensureWorkspace()).find((item) => item.docId === doc.id && current.noteLeafViews.get(item.id)?.editor);
    const editor = leaf ? current.noteLeafViews.get(leaf.id)?.editor : null;
    doc.text = content;
    if (editor) editor.applyValue(content, selection || editor.getSelection());
    else queueSave(doc, content);
    syncDocumentEditors(doc.id, content, editor);
  }

  function setLeafSaveState(docId, value) {
    for (const cache of context()?.noteLeafViews?.values() || []) {
      if (cache.docId !== docId) continue;
      cache.saveState = value;
      updateLeafStatus(cache);
    }
  }

  async function flushAll() {
    const ids = [...(context()?.noteDrafts?.keys() || [])];
    return Promise.all(ids.map(saveDraft));
  }

  function destroy() {
    void flushAll();
    const current = context();
    for (const leafId of [...(current?.noteLeafViews?.keys() || [])]) disposeLeaf(leafId);
    if (current?.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    if (current?.notePageHideHandler) window.removeEventListener('pagehide', current.notePageHideHandler);
    for (const entry of current?.noteShellMedia || []) entry.query.removeEventListener('change', entry.handler);
    if (current) {
      current.notePageHideHandler = null; current.noteShellMedia = null; current.noteDrawer = null;
      current.noteDrawerRelease?.(); current.noteDrawerRelease = null;
    }
    clearTimeout(persistTimer);
    persist(true);
  }

  function acceptSavedDocument(docId, content = null) {
    const current = context();
    current?.noteDrafts?.delete(docId);
    const doc = state.docs.find((item) => item.id === docId);
    const value = content ?? String(doc?.text || '');
    if (doc) doc.text = value;
    for (const cache of current?.noteLeafViews?.values() || []) if (cache.docId === docId && doc) cache.doc = doc;
    syncDocumentEditors(docId, value);
    setLeafSaveState(docId, 'saved');
  }

  function commandButton(label, run, attrs = {}) {
    return h('button', { type:'button', class:'copal-btn', text:label, ...attrs, onclick:run });
  }

  function showChooser({ title = 'Quick switcher', docs = documents(), choose = null, allowCreate = true } = {}) {
    const dialog = h('dialog', { class:'copal-dialog copal-quick-switcher' }, h('h2', { text:title }));
    const search = h('input', { type:'search', placeholder:'Type a note name…', 'aria-label':title, autocomplete:'off' });
    const list = h('div', { class:'copal-switcher-results', role:'listbox' });
    let buttons = [];
    let active = 0;
    const select = (index) => {
      active = Math.max(0, Math.min(buttons.length - 1, index));
      buttons.forEach((button, position) => button.classList.toggle('active', position === active));
      buttons[active]?.scrollIntoView({ block:'nearest' });
    };
    const draw = () => {
      const query = search.value.trim();
      const recent = ensureWorkspace().recent;
      const ranked = docs
        .map((doc) => ({ doc, score:fuzzyScore(doc.name, query), recent:recent.indexOf(doc.id) }))
        .filter((item) => item.score >= 0)
        .sort((a, b) => b.score - a.score || (a.recent < 0 ? 999 : a.recent) - (b.recent < 0 ? 999 : b.recent) || a.doc.name.localeCompare(b.doc.name))
        .slice(0, 80);
      list.replaceChildren();
      buttons = ranked.map(({ doc }) => {
        const button = h('button', { class:'copal-doc-row', role:'option', type:'button', onclick:(event) => {
          if (choose) choose(doc, event);
          else if (event.altKey) { const group = activeGroup(); if (group) splitWorkspaceGroup(ensureWorkspace(), group.id, doc, 'vertical'); persist(true); render(); }
          else if (event.shiftKey) { const group = activeGroup(); if (group) splitWorkspaceGroup(ensureWorkspace(), group.id, doc, 'horizontal'); persist(true); render(); }
          else open(doc.id, { reuse:!(event.ctrlKey || event.metaKey) });
          dialog.close();
        } },
          h('span', { text:doc.name }), h('small', { text:noteViewType(doc) }));
        list.append(button);
        return button;
      });
      if (!buttons.length && query && allowCreate) {
        const name = query;
        const create = h('button', { class:'copal-doc-row', type:'button', text:`Create “${name}”`, onclick:() => {
          dialog.close(); createNew(name);
        } });
        list.append(create); buttons = [create];
      }
      select(0);
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); select(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); select(active - 1); }
      else if (event.key === 'Enter') {
        event.preventDefault();
        buttons[active]?.dispatchEvent(new MouseEvent('click', { bubbles:true, ctrlKey:event.ctrlKey, metaKey:event.metaKey, shiftKey:event.shiftKey, altKey:event.altKey }));
      }
    });
    dialog.append(search, list, h('footer', { class:'copal-dialog-hint', text:'Enter open · Ctrl new tab · Shift split right · Alt split below · Esc close' }));
    wireDialog(dialog); document.body.append(dialog);
    draw(); dialog.showModal(); search.focus();
  }

  async function createDatabaseNote(name, content = '', properties = {}) {
    const relations = databaseRelations(content, state.docs);
    const result = await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'note', content, properties, relations }) });
    await loadDocuments(false);
    open(result.doc.id, { reuse:false });
    return result.doc;
  }

  function createNew(initial = 'Untitled') {
    showForm('New Copal note', [['name', 'Name', initial], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
      await createDatabaseNote(name, content);
    });
  }

  function localDate() {
    const now = new Date();
    const part = (value) => String(value).padStart(2, '0');
    return `${now.getFullYear()}-${part(now.getMonth() + 1)}-${part(now.getDate())}`;
  }

  async function openDailyNote() {
    const date = localDate();
    const name = `Daily/${date}`;
    const existing = documents().find((doc) => doc.kind === 'note' && doc.name === name);
    if (existing) { open(existing.id); return; }
    await createDatabaseNote(name, '', { type:'daily', date });
  }

  function createFromTemplate() {
    const templates = documents().filter((doc) => doc.kind === 'note' && doc.properties?.type === 'template');
    if (!templates.length) {
      context().window.setStatus('No templates yet. Set a note’s type property to template.');
      return;
    }
    showChooser({ title:'New from template', docs:templates, allowCreate:false, choose:(template) => {
      const initial = `${displayName(template)} copy`;
      showForm('New from template', [['name', 'Name', initial]], async ({ name }) => {
        const properties = { ...(template.properties || {}), template:template.id };
        delete properties.type;
        await createDatabaseNote(name, String(template.text || ''), properties);
      });
    } });
  }

  function toggleBookmark(docId) {
    const workspace = ensureWorkspace();
    workspace.bookmarks ||= [];
    workspace.bookmarks = workspace.bookmarks.includes(docId)
      ? workspace.bookmarks.filter((id) => id !== docId)
      : [docId, ...workspace.bookmarks];
    persist(true); render();
  }

  function renameWithForm(doc) {
    showForm(`Rename ${doc.name}`, [['name', 'Path', doc.name]], async ({ name }) => {
      await renameNote(doc, name);
      render();
    });
  }

  function activeGroup(workspace = ensureWorkspace()) {
    return workspace ? groupForLeaf(workspace, workspace.activeLeafId) || workspaceGroups(workspace)[0] : null;
  }

  function syncSelectionToModel(workspace) {
    const current = context();
    if (!current) return;
    current.selected = findWorkspaceLeaf(workspace)?.docId || null;
    state.selected = current.selected;
    persistActiveContext();
  }

  function splitActive(orientation) {
    const workspace = ensureWorkspace();
    const group = activeGroup(workspace);
    if (!workspace || !group) return;
    showChooser({
      title:orientation === 'vertical' ? 'Split below' : 'Split right',
      choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, orientation); persist(true); render(); },
      allowCreate:false,
    });
  }

  function setMode(mode) {
    const workspace = ensureWorkspace();
    if (setWorkspaceLeafMode(workspace, workspace.activeLeafId, mode)) { persist(true); render(); }
  }

  function getSettings() {
    const workspace = ensureWorkspace();
    return normalizeNotesSettings(workspace?.settings);
  }

  function updateSettings(patch = {}) {
    const workspace = ensureWorkspace();
    if (!workspace) return normalizeNotesSettings(patch);
    workspace.settings = normalizeNotesSettings({ ...workspace.settings, ...patch });
    persist(true);
    if (context()?.window?.visible) render();
    return { ...workspace.settings };
  }

  function setPreviewLayout(layout) {
    updateSettings({ previewLayout:layout });
  }

  function showSettings() {
    const workspace = ensureWorkspace();
    const dialog = h('dialog', { class:'copal-dialog copal-notes-settings' }, h('h2', { text:'Notes settings' }));
    const layout = h('select', { 'aria-label':'Preview layout' },
      h('option', { value:'inline', text:'Inline Live Preview (default)' }),
      h('option', { value:'side-by-side', text:'Side-by-side source and preview' }));
    layout.value = workspace.settings.previewLayout;
    const lineNumbers = h('input', { type:'checkbox', 'aria-label':'Show line numbers' }); lineNumbers.checked = workspace.settings.lineNumbers;
    const readable = h('input', { type:'checkbox', 'aria-label':'Readable line width' }); readable.checked = workspace.settings.readableLineWidth;
    const ribbon = h('input', { type:'checkbox', 'aria-label':'Show Notes ribbon' }); ribbon.checked = workspace.settings.ribbon;
    const hideCompleted = h('input', { type:'checkbox', 'aria-label':'Hide completed tasks' }); hideCompleted.checked = workspace.settings.completedVisibility === 'hide';

    // Sidebar panels section
    const movablePanels = Object.entries(NOTES_PANELS).filter(([, def]) => def.allowedSides.length > 1);
    const panelControls = h('div', { class:'copal-settings-panels' });
    const panelState = {};
    for (const [id, def] of movablePanels) {
      const current = workspace.panels?.[id] || { side:def.defaultSide, hidden:false };
      const sideSelect = h('select', { 'aria-label':`${def.label} side` },
        h('option', { value:'left', text:'Left' }),
        h('option', { value:'right', text:'Right' }));
      sideSelect.value = current.side;
      const hiddenCheck = h('input', { type:'checkbox', 'aria-label':`Hide ${def.label}` }); hiddenCheck.checked = current.hidden === true;
      const orderUp = commandButton('↑', () => {
        const entries = Object.entries(panelState).filter(([, s]) => s.side === sideSelect.value && !s.hidden);
        const idx = entries.findIndex(([eid]) => eid === id);
        if (idx > 0) { const prev = entries[idx - 1][0]; panelState[prev].order = panelState[id].order; panelState[id].order = panelState[id].order - 1; rebuildPanelOrder(); }
      }, { title:'Move up', 'aria-label':`Move ${def.label} up` });
      const orderDown = commandButton('↓', () => {
        const entries = Object.entries(panelState).filter(([, s]) => s.side === sideSelect.value && !s.hidden);
        const idx = entries.findIndex(([eid]) => eid === id);
        if (idx >= 0 && idx < entries.length - 1) { const next = entries[idx + 1][0]; panelState[next].order = panelState[id].order; panelState[id].order = panelState[id].order + 1; rebuildPanelOrder(); }
      }, { title:'Move down', 'aria-label':`Move ${def.label} down` });
      panelState[id] = { side:current.side, order:current.order ?? def.defaultOrder, hidden:current.hidden };
      const row = h('div', { class:'copal-settings-panel-row' },
        h('span', { class:'copal-settings-panel-label', text:def.label }),
        sideSelect, hiddenCheck, h('span', { text:'Hidden' }), orderUp, orderDown);
      panelControls.append(row);
    }
    const rebuildPanelOrder = () => {
      for (const [id, def] of movablePanels) {
        const row = panelControls.querySelector(`[aria-label="${def.label} side"]`)?.closest('.copal-settings-panel-row');
        if (!row) continue;
        const sideSelect = row.querySelector('[aria-label$=" side"]');
        const hiddenCheck = row.querySelector('[aria-label^="Hide"]');
        if (sideSelect) { panelState[id].side = sideSelect.value; sideSelect.value = panelState[id].side; }
        if (hiddenCheck) { panelState[id].hidden = hiddenCheck.checked; hiddenCheck.checked = panelState[id].hidden; }
      }
    };

    dialog.append(
      h('label', {}, h('span', { text:'Preview layout' }), layout),
      h('label', { class:'copal-check' }, lineNumbers, h('span', { text:'Show line numbers' })),
      h('label', { class:'copal-check' }, readable, h('span', { text:'Use readable line width' })),
      h('label', { class:'copal-check' }, ribbon, h('span', { text:'Show optional Notes ribbon' })),
      h('label', { class:'copal-check' }, hideCompleted, h('span', { text:'Hide completed tasks in reading mode' })),
      h('p', { class:'copal-dialog-hint', text:'Document mode and preview layout are independent. Side-by-side is preserved but never the clean-profile default.' }),
      h('h3', { text:'Sidebar panels' }),
      h('p', { class:'copal-dialog-hint', text:'Choose which side each panel lives on and whether it is visible. Files and Search are always on the left.' }),
      panelControls,
      h('div', { class:'copal-dialog-actions' }, commandButton('Cancel', () => dialog.close()), commandButton('Save', () => {
        updateSettings({
          previewLayout:layout.value,
          lineNumbers:lineNumbers.checked,
          readableLineWidth:readable.checked,
          ribbon:ribbon.checked,
          completedVisibility:hideCompleted.checked ? 'hide' : 'show',
        });
        // Read current panel state from DOM before applying
        for (const [id, def] of movablePanels) {
          const row = panelControls.querySelector(`[aria-label="${def.label} side"]`)?.closest('.copal-settings-panel-row');
          if (!row) continue;
          const sideSel = row.querySelector('[aria-label$=" side"]');
          const hiddenChk = row.querySelector('[aria-label^="Hide"]');
          if (sideSel) panelState[id].side = sideSel.value;
          if (hiddenChk) panelState[id].hidden = hiddenChk.checked;
        }
        // Apply panel placements
        for (const [id] of movablePanels) {
          setWorkspacePanelPlacement(workspace, id, panelState[id]);
        }
        // Ensure active tab is still valid on each side
        const leftPanels = workspacePanelsForSide(workspace, 'left');
        if (leftPanels.length && !leftPanels.includes(workspace.left.tab)) workspace.left.tab = leftPanels[0];
        const rightPanels = workspacePanelsForSide(workspace, 'right');
        if (rightPanels.length && !rightPanels.includes(workspace.right.tab)) workspace.right.tab = rightPanels[0];
        persist(true);
        dialog.close();
        render();
      }, { class:'copal-btn primary' })),
    );
    wireDialog(dialog); document.body.append(dialog); dialog.showModal(); layout.focus();
  }

  function showSyntaxGallery() {
    const dialog = h('dialog', { class:'copal-dialog copal-syntax-gallery' }, h('h2', { text:'Syntax Gallery' }));
    const search = h('input', { type:'search', placeholder:'Filter syntax…', 'aria-label':'Filter syntax examples', autocomplete:'off' });
    const list = h('div', { class:'copal-gallery-list' });
    const examples = [
      { category:'Text', title:'Headings', source:'# Heading 1\n## Heading 2\n### Heading 3' },
      { category:'Text', title:'Emphasis', source:'**bold** and *italic* and ~~strikethrough~~ and ==highlight==' },
      { category:'Text', title:'Inline code', source:'Use `code` inline' },
      { category:'Text', title:'Links', source:'[Link text](https://example.com)\n[[Other Note]]' },
      { category:'Text', title:'Blockquote', source:'> A blockquote\n> with multiple lines' },
      { category:'Text', title:'Horizontal rule', source:'---' },
      { category:'Tasks', title:'Task list', source:'- [ ] Incomplete task\n- [x] Completed task\n- [ ] Another task' },
      { category:'Tables', title:'Table', source:'| Name | Status | Value |\n| :--- | :---: | ---: |\n| Alpha | open | 42 |\n| Beta | done | 7 |' },
      { category:'Math', title:'Inline math', source:'The equation $a^2 + b^2 = c^2$ is Pythagoras.' },
      { category:'Math', title:'Block math', source:'$$\nE = mc^2\n$$' },
      { category:'Structure', title:'Callout', source:'> [!info] Info callout\n> This is an informational callout.' },
      { category:'Structure', title:'Footnote', source:'Text with a footnote[^1].\n\n[^1]: Footnote content here.' },
      { category:'Structure', title:'Frontmatter', source:'---\ntitle: My Note\ntags: [reference, draft]\ndate: 2026-07-19\n---' },
      { category:'Structure', title:'Transclusion', source:'![[Other Note]]' },
      { category:'Plugins', title:'Dataview block', source:'```dataview\nLIST FROM #project\nWHERE status != "done"\n```' },
      { category:'Plugins', title:'Tasks query', source:'```tasks\nnot done\ntag includes #tasks\n```' },
      { category:'Plugins', title:'Templater', source:'<% tp.date.now("YYYY-MM-DD") %>' },
    ];
    const doc = activeDoc(ensureWorkspace());
    const insertAtCursor = (source) => {
      if (!doc) return;
      const leaf = activeLeaf();
      if (!leaf) return;
      const cache = context().noteLeafViews.get(leaf.id);
      if (!cache?.editor) return;
      const sel = cache.editor.getSelection();
      const value = sourceValue(doc);
      const pos = sel?.anchor ?? value.length;
      const newValue = value.slice(0, pos) + source + value.slice(pos);
      applyDocumentSource(doc, newValue, { anchor:pos + source.length, head:pos + source.length });
    };
    const copyToClipboard = async (source, button) => {
      try {
        await navigator.clipboard.writeText(source);
        button.textContent = 'Copied!';
        setTimeout(() => { button.textContent = 'Copy'; }, 1500);
      } catch {
        button.textContent = 'Denied';
        setTimeout(() => { button.textContent = 'Copy'; }, 1500);
      }
    };
    const draw = () => {
      const query = search.value.trim().toLowerCase();
      list.replaceChildren();
      for (const example of examples) {
        if (query && !example.title.toLowerCase().includes(query) && !example.source.toLowerCase().includes(query) && !example.category.toLowerCase().includes(query)) continue;
        const pre = h('pre', { class:'copal-gallery-source', tabindex:'0' }, h('code', { text:example.source }));
        const copyBtn = h('button', { type:'button', class:'copal-btn', text:'Copy', onclick:() => copyToClipboard(example.source, copyBtn) });
        const insertBtn = h('button', { type:'button', class:'copal-btn copal-btn primary', text:'Insert at cursor', disabled:!doc, onclick:() => { insertAtCursor(example.source); dialog.close(); } });
        const card = h('div', { class:'copal-gallery-card' },
          h('div', { class:'copal-gallery-card-header' },
            h('span', { class:'copal-gallery-badge', text:example.category }),
            h('strong', { text:example.title })),
          pre,
          h('div', { class:'copal-gallery-actions' }, copyBtn, insertBtn));
        list.append(card);
      }
      if (!list.children.length) list.append(h('p', { class:'copal-empty-inline', text:'No matching examples.' }));
    };
    search.addEventListener('input', draw);
    dialog.append(search, list, h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Close', onclick:() => dialog.close() })));
    wireDialog(dialog); document.body.append(dialog); draw(); dialog.showModal(); search.focus();
  }

  function showCommands() {
    const workspace = ensureWorkspace();
    const doc = activeDoc(workspace);
    const actions = [
      ['New note', 'Ctrl+N', () => createNew()],
      ['Open today’s note', '', openDailyNote],
      ['New from template', '', createFromTemplate],
      ['Quick switcher', 'Ctrl+O', () => showChooser()],
      ['Search notes', 'Ctrl+Shift+F', () => showSearch()],
      ['Open Timeline', '', () => open(TIMELINE_DOCUMENT.id)],
      ['Notes settings', '', showSettings],
      ...(doc?.virtual || doc?.readOnly ? [] : [[doc?.kind === 'note' ? 'Editing mode' : 'Live Preview mode', '', () => setMode('live')]]),
      ...(doc?.virtual || doc?.readOnly || doc?.kind === 'note' ? [] : [['Source mode', '', () => setMode('source')]]),
      ...(doc?.virtual ? [] : [['Reading mode', '', () => setMode('reading')]]),
      ['Inline preview layout', '', () => setPreviewLayout('inline')],
      ['Side-by-side preview layout', '', () => setPreviewLayout('side-by-side')],
      ['Split right', '', () => splitActive('horizontal')],
      ['Split below', '', () => splitActive('vertical')],
      ['Toggle notes sidebar', '', () => { workspace.left.open = !workspace.left.open; persist(true); render(); }],
      ['Toggle linked sidebar', '', () => { workspace.right.open = !workspace.right.open; persist(true); render(); }],
      ...(doc?.virtual ? [] : [['Toggle bookmark', '', () => doc && toggleBookmark(doc.id)]]),
      ['Reopen closed note', '', reopenClosed],
      ...(doc?.virtual ? [] : [['History', '', () => doc && showHistory(doc)]]),
      ['Trash', '', () => showTrash()],
      ['Import Markdown or Obsidian backup', '', importVault],
      ['Syntax gallery', '', () => { const gallery = state.docs.find((d) => d.name.includes('Syntax Gallery') || d.name.includes('syntax-gallery')); if (gallery) open(gallery.id); else showSyntaxGallery(); }],
      ['Export Markdown backup', '', () => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; }],
    ];
    const recentCommands = context().noteRecentCommands ||= [];
    const dialog = h('dialog', { class:'copal-dialog copal-command-palette' }, h('h2', { text:'Notes commands' }));
    const search = h('input', { type:'search', placeholder:'Run a command…', 'aria-label':'Command palette', autocomplete:'off' });
    const list = h('div', { class:'copal-switcher-results', role:'listbox' });
    let buttons = []; let active = 0;
    const select = (index) => { active = Math.max(0, Math.min(buttons.length - 1, index)); buttons.forEach((button, position) => button.classList.toggle('active', position === active)); buttons[active]?.scrollIntoView({ block:'nearest' }); };
    const draw = () => {
      const query = search.value.trim();
      list.replaceChildren();
      buttons = actions.map(([label, shortcut, run]) => ({ label, shortcut, run, score:fuzzyScore(`${label} ${shortcut}`, query), recent:recentCommands.indexOf(label) }))
        .filter((item) => item.score >= 0).sort((a, b) => b.score - a.score || (a.recent < 0 ? 999 : a.recent) - (b.recent < 0 ? 999 : b.recent))
        .map((item) => {
          const button = h('button', { class:'copal-command-row', role:'option', type:'button', onclick:() => {
            context().noteRecentCommands = [item.label, ...recentCommands.filter((label) => label !== item.label)].slice(0, 12);
            dialog.close(); item.run();
          } }, h('span', { text:item.label }), h('kbd', { text:item.shortcut }));
          list.append(button); return button;
        });
      select(0);
    };
    search.addEventListener('input', draw);
    search.addEventListener('keydown', (event) => {
      if (event.key === 'ArrowDown') { event.preventDefault(); select(active + 1); }
      else if (event.key === 'ArrowUp') { event.preventDefault(); select(active - 1); }
      else if (event.key === 'Enter') { event.preventDefault(); buttons[active]?.click(); }
    });
    dialog.append(search, list); wireDialog(dialog); document.body.append(dialog);
    draw(); dialog.showModal(); search.focus();
  }

  function showSearch() {
    const workspace = ensureWorkspace();
    workspace.left.open = true;
    workspace.left.tab = 'search';
    persist(true); render();
    requestAnimationFrame(() => context()?.window.root.querySelector('.copal-note-search-input')?.focus());
  }

  function reopenClosed() {
    const workspace = ensureWorkspace();
    while (workspace.closed.length) {
      const id = workspace.closed.shift();
      const doc = workspaceDocuments().find((item) => item.id === id);
      if (!doc) continue;
      const leaf = workspaceLeaves(workspace).find((item) => item.docId === id);
      if (leaf) {
        const group = groupForLeaf(workspace, leaf.id);
        if (group) { setActive(group.id, leaf.id); return; }
      }
      open(id, { reuse:false });
      return;
    }
    persist(true); render();
  }

  function focusEmptyWorkspaceIfIdle() {
    const workspace = ensureWorkspace();
    if (!workspace || workspaceLeaves(workspace).length) return;
    requestAnimationFrame(() => context()?.window.root.querySelector('.copal-notes-empty-workspace')?.focus({ preventScroll:true }));
  }

  function emptyWorkspace(workspace) {
    return h('section', { class:'copal-empty copal-notes-empty-workspace', tabindex:'-1', role:'region', 'aria-label':'No open notes' },
      h('h2', { text:'No open notes' }),
      h('p', { text:'The workspace is empty. Nothing was reopened for you — pick what to open next.' }),
      h('div', { class:'copal-empty-workspace-actions' },
        commandButton('New note', () => createNew(), { class:'copal-btn primary' }),
        commandButton('Quick switcher', () => showChooser()),
        commandButton('Reopen closed', reopenClosed, workspace.closed.length ? {} : { disabled:true }),
        commandButton('Open Timeline', () => open(TIMELINE_DOCUMENT.id)),
        commandButton('Import backup', importVault)));
  }

  function bindKeys(workspace, doc) {
    const current = context();
    if (current.noteKeyHandler) current.window.root.removeEventListener('keydown', current.noteKeyHandler);
    current.noteKeyHandler = (event) => {
      if (event.isComposing || !(event.ctrlKey || event.metaKey) || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === 'o') { event.preventDefault(); event.stopPropagation(); showChooser(); }
      else if (key === 'p') { event.preventDefault(); event.stopPropagation(); showCommands(); }
      else if (key === 'f' && event.shiftKey) { event.preventDefault(); event.stopPropagation(); showSearch(); }
      else if (key === 'n') { event.preventDefault(); event.stopPropagation(); createNew(); }
      else if (key === 's' && doc) { event.preventDefault(); event.stopPropagation(); void saveDraft(doc.id); }
    };
    current.window.root.addEventListener('keydown', current.noteKeyHandler);
  }

  function fileTree(docs, workspace) {
    const root = { folders:new Map(), docs:[] };
    for (const doc of docs) {
      const parts = doc.name.split('/'); let node = root;
      for (const folder of parts.slice(0, -1)) {
        if (!node.folders.has(folder)) node.folders.set(folder, { folders:new Map(), docs:[] });
        node = node.folders.get(folder);
      }
      node.docs.push(doc);
    }
    const expanded = new Set(workspace.left.expanded);
    const selectedIds = new Set(workspace.left.selected);
    const sorted = (values) => [...values].sort((a, b) => workspace.left.sort === 'modified'
      ? String(b.ts || '').localeCompare(String(a.ts || '')) || a.name.localeCompare(b.name)
      : a.name.localeCompare(b.name));
    const draw = (node, parent, path = []) => {
      for (const [name, folder] of [...node.folders].sort(([a], [b]) => a.localeCompare(b))) {
        const full = [...path, name].join('/'); const isOpen = expanded.has(full);
        const row = h('div', { class:'copal-folder-row', role:'treeitem', 'aria-expanded':String(isOpen), tabindex:'0' }, h('span', { class:'copal-tree-toggle', text:isOpen ? '▾' : '▸' }), h('span', { text:name }));
        const children = h('div', { class:'copal-tree-children', role:'group' }); children.hidden = !isOpen;
        const toggle = () => { isOpen ? expanded.delete(full) : expanded.add(full); workspace.left.expanded = [...expanded]; persist(true); render(); };
        row.addEventListener('click', toggle);
        row.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ' || (event.key === 'ArrowRight' && !isOpen) || (event.key === 'ArrowLeft' && isOpen)) { event.preventDefault(); toggle(); }
        });
        row.addEventListener('dragover', (event) => event.preventDefault());
        row.addEventListener('drop', async (event) => {
          event.preventDefault(); event.stopPropagation();
          const id = event.dataTransfer.getData('text/x-copal-document');
          const doc = state.docs.find((item) => item.id === id);
          if (doc) {
            try { await renameNote(doc, `${full}/${doc.name.split('/').pop()}`); }
            catch (error) { context().window.setStatus(error.message, true); }
          }
        });
        parent.append(row, children); draw(folder, children, [...path, name]);
      }
      for (const doc of sorted(node.docs)) {
        const selected = activeLeaf(workspace)?.docId === doc.id;
        const chosen = selectedIds.has(doc.id);
        const row = h('div', { class:`copal-file-entry${selected ? ' active' : ''}${chosen ? ' selected' : ''}` });
        const openButton = h('button', { class:'copal-file-row', role:'treeitem', 'aria-selected':String(chosen || selected), draggable:doc.readOnly ? false : 'true', title:doc.name, onclick:(event) => {
          if (!doc.readOnly && (event.ctrlKey || event.metaKey)) {
            chosen ? selectedIds.delete(doc.id) : selectedIds.add(doc.id);
            workspace.left.selected = [...selectedIds]; persist(true); render(); return;
          }
          workspace.left.selected = []; open(doc.id);
        } },
          h('span', { class:'copal-file-kind', text:fileGlyph(doc) }), h('span', { text:displayName(doc) }));
        if (!doc.readOnly) openButton.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-document', doc.id));
        const menuItems = [
          ...(doc.readOnly ? [] : [commandButton('Rename or move', () => renameWithForm(doc))]),
          commandButton('Open in new tab', () => open(doc.id, { reuse:false })),
          commandButton('Reveal path', () => { revealInExplorer(doc, workspace); persist(true); render(); }),
          ...(doc.readOnly ? [] : [commandButton('Trash', () => deleteDocument(doc), { class:'copal-btn danger' })]),
        ];
        const menu = wirePopover(h('details', { class:'copal-file-menu' }, h('summary', { title:`Actions for ${doc.name}`, 'aria-label':`Actions for ${doc.name}`, text:'⋯' }),
          h('div', { class:'copal-popover-menu' }, menuItems)));
        row.append(openButton, menu); parent.append(row);
      }
    };
    const tree = h('div', { class:'copal-file-tree', role:'tree', 'aria-label':'Copal notes', tabindex:'-1' });
    tree.addEventListener('keydown', (event) => {
      if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
      const items = [...tree.querySelectorAll('[role="treeitem"]')].filter((item) => item.offsetParent !== null);
      const current = items.indexOf(document.activeElement);
      const index = event.key === 'Home' ? 0 : event.key === 'End' ? items.length - 1 : Math.max(0, Math.min(items.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)));
      if (items[index]) { event.preventDefault(); items[index].focus(); }
    });
    draw(root, tree); return tree;
  }

  function fileGlyph(doc) {
    return ({ note:'◆', markdown:'◇', canvas:'⌘', base:'▦', image:'▧', audio:'♪', video:'▶', pdf:'▤', asset:'·' })[noteViewType(doc)] || '◇';
  }

  function displayName(doc) {
    const name = String(doc?.name || '').split('/').pop();
    return ['note', 'markdown'].includes(noteViewType(doc)) ? name.replace(/\.md$/i, '') : name;
  }

  // Roving tabindex keyboard navigation for a tab strip.
  // Tabs is a container with role="tablist"; panelIds is the ordered list of
  // visible panel ids matching the buttons in DOM order; onSelect fires when
  // the user activates a tab (Enter/Space).
  function addRovingFocus(tabs, panelIds, onSelect) {
    const buttons = () => [...tabs.querySelectorAll('[role="tab"]')];
    tabs.addEventListener('keydown', (event) => {
      const btns = buttons();
      const current = btns.indexOf(document.activeElement);
      if (current < 0) return;
      let next = current;
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % btns.length;
      else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + btns.length) % btns.length;
      else if (event.key === 'Home') next = 0;
      else if (event.key === 'End') next = btns.length - 1;
      else return;
      event.preventDefault();
      btns.forEach((btn, i) => { btn.tabIndex = i === next ? 0 : -1; });
      btns[next].focus();
      const id = panelIds[next];
      if (id && typeof onSelect === 'function') onSelect(id);
    });
  }

  function leftSidebar(workspace, docs, shellState) {
    const aside = h('aside', { id:'copal-notes-left-sidebar', class:'copal-notes-explorer', style:`--copal-left-width:${workspace.left.width}px` });
    const sideHead = h('header', { class:'copal-shell-side-header left' },
      ...(shellState.narrow ? [] : [shellState.controls.left]), h('strong', { text:'Files' }),
      commandButton(workspace.left.showDotFolders ? '◉' : '○', () => { workspace.left.showDotFolders = !workspace.left.showDotFolders; persist(true); render(); }, { title:workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders', 'aria-label':workspace.left.showDotFolders ? 'Hide hidden folders' : 'Show hidden folders' }));
    const tabs = h('div', { class:'copal-side-tabs', role:'tablist', 'aria-label':'Notes navigation' });
    const panelIds = workspacePanelsForSide(workspace, 'left');
    for (const key of panelIds) {
      const def = NOTES_PANELS[key];
      if (!def) continue;
      tabs.append(h('button', { class:workspace.left.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.left.tab === key), text:def.label, 'data-panel-id':key, onclick:() => { workspace.left.tab = key; persist(true); render(); } }));
    }
    addRovingFocus(tabs, panelIds, (id) => { workspace.left.tab = id; persist(true); render(); });
    const visibleDocs = explorerDocs();
    const body = h('div', { class:'copal-side-body' });
    if (workspace.left.tab === 'files') {
      const sort = h('select', { class:'copal-file-sort', 'aria-label':'Sort files' }, h('option', { value:'name', text:'Name' }), h('option', { value:'modified', text:'Modified' }));
      sort.value = workspace.left.sort;
      sort.addEventListener('change', () => { workspace.left.sort = sort.value === 'modified' ? 'modified' : 'name'; persist(true); render(); });
      const allFolders = [...new Set(visibleDocs.flatMap((doc) => {
        const parts = doc.name.split('/').slice(0, -1); return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
      }))];
      const expand = () => { workspace.left.expanded = workspace.left.expanded.length === allFolders.length ? [] : allFolders; persist(true); render(); };
      body.append(h('header', {}, h('strong', { text:'Notes' }), sort,
        commandButton('↔', () => open(TIMELINE_DOCUMENT.id), { title:'Open Timeline', 'aria-label':'Open Timeline' }),
        commandButton(workspace.left.expanded.length === allFolders.length ? '−' : '+', expand, { title:'Expand or collapse all collections', 'aria-label':'Expand or collapse all collections' }),
        commandButton('＋', () => createNew(), { title:'New note', 'aria-label':'New note' }),
        commandButton('▱+', () => createNew('New collection/Untitled'), { title:'New collection with note', 'aria-label':'New collection with note' })));
      if (workspace.left.selected.length) {
        const chosen = workspace.left.selected.map((id) => state.docs.find((doc) => doc.id === id)).filter(Boolean);
        body.append(h('div', { class:'copal-file-selection', role:'status' }, h('span', { text:`${chosen.length} selected` }),
          commandButton('Clear', () => { workspace.left.selected = []; persist(true); render(); }),
          commandButton('Trash', async () => {
            if (deleteDocuments) await deleteDocuments(chosen);
            else for (const doc of chosen) await deleteDocument(doc);
            workspace.left.selected = []; persist(true);
          }, { class:'copal-btn danger' })));
      }
      body.append(fileTree(visibleDocs, workspace));
    } else if (workspace.left.tab === 'search') {
      const input = h('input', { class:'copal-note-search-input', type:'search', placeholder:'Search notes and properties…', 'aria-label':'Search notes', value:context().noteSearch || '' });
      const results = h('div', { class:'copal-search-results' });
      const draw = () => {
        context().noteSearch = input.value;
        const query = input.value.trim().toLowerCase();
        // Search ALL documents, including those in hidden dot-folders.
        const allDocs = documents();
        const matches = query ? allDocs.map((doc) => {
          const source = String(doc.text || ''); const lower = source.toLowerCase(); const at = lower.indexOf(query);
          const metadata = `${JSON.stringify(doc.properties || {})} ${(doc.tags || []).join(' ')}`.toLowerCase();
          const nameScore = fuzzyScore(doc.name, query); const contentScore = at >= 0 ? 500 - Math.min(400, at) : metadata.includes(query) ? 450 : -1;
          const start = Math.max(0, at - 55); const snippet = at >= 0 ? source.slice(start, at + query.length + 85).replace(/\s+/g, ' ').trim() : '';
          return { doc, score:Math.max(nameScore, contentScore), snippet };
        }).filter((item) => item.score >= 0).sort((a, b) => b.score - a.score || a.doc.name.localeCompare(b.doc.name)).slice(0, 100) : [];
        results.replaceChildren(...matches.map(({ doc, snippet }) => {
          const hidden = isHiddenDoc(doc);
          const cls = 'copal-doc-row' + (hidden ? ' copal-doc-row--hidden' : '');
          return h('button', { class:cls, onclick:() => open(doc.id) },
            h('strong', { text:doc.name }),
            hidden ? h('small', { class:'copal-hidden-badge', text:'hidden' }) : null,
            snippet ? h('small', { text:snippet }) : null);
        }));
        if (query && !matches.length) results.append(h('p', { class:'copal-empty-inline', text:'No matches' }));
      };
      input.addEventListener('input', draw); body.append(input, results); draw();
    } else if (workspace.left.tab === 'tags') {
      const tagged = new Map();
      for (const doc of visibleDocs) for (const tag of doc.tags || []) {
        const name = String(tag).replace(/^#/, '');
        if (!tagged.has(name)) tagged.set(name, []);
        tagged.get(name).push(doc);
      }
      body.append(h('header', {}, h('strong', { text:'Tags' })));
      for (const [tag, matches] of [...tagged].sort(([a], [b]) => a.localeCompare(b))) {
        const section = h('details', { class:'copal-tag-group' }, h('summary', { text:`#${tag} · ${matches.length}` }));
        for (const doc of matches) section.append(h('button', { class:'copal-doc-row', text:doc.name, onclick:() => open(doc.id) }));
        body.append(section);
      }
      if (!tagged.size) body.append(h('p', { class:'copal-empty-inline', text:'No tags yet.' }));
    } else {
      const ids = workspace.left.tab === 'bookmarks'
        ? workspace.bookmarks
        : workspace.recent;
      const unique = [...new Set(ids)].map((id) => state.docs.find((doc) => doc.id === id)).filter(Boolean);
      body.append(h('header', {}, h('strong', { text:workspace.left.tab === 'bookmarks' ? 'Bookmarks' : 'Recent notes' })));
      for (const doc of unique) body.append(h('button', { class:'copal-doc-row', text:doc.name, onclick:() => open(doc.id) }));
      if (!unique.length) body.append(h('p', { class:'copal-empty-inline', text:workspace.left.tab === 'bookmarks' ? 'Bookmark a note from its actions menu.' : 'No recent notes.' }));
    }
    const handle = resizeHandle('left', workspace);
    aside.append(sideHead, tabs, body, handle);
    return aside;
  }

  function resizeHandle(side, workspace) {
    const minimum = side === 'left' ? 150 : 190; const maximum = side === 'left' ? 420 : 480;
    const handle = h('div', { class:`copal-sidebar-resize ${side}`, role:'separator', tabindex:'0', 'aria-label':`Resize ${side} Notes sidebar`, 'aria-orientation':'vertical', 'aria-valuemin':minimum, 'aria-valuemax':maximum, 'aria-valuenow':workspace[side].width });
    const apply = (value) => {
      workspace[side].width = Math.max(minimum, Math.min(maximum, value));
      handle.setAttribute('aria-valuenow', String(Math.round(workspace[side].width)));
      const shell = context()?.window.root.querySelector('.copal-notes-workspace');
      shell?.style.setProperty(side === 'left' ? '--copal-left-width' : '--copal-right-width', `${workspace[side].width}px`);
    };
    handle.addEventListener('keydown', (event) => {
      const delta = event.key === 'ArrowLeft' ? -12 : event.key === 'ArrowRight' ? 12 : 0;
      if (!delta) return; event.preventDefault(); apply(workspace[side].width + (side === 'right' ? -delta : delta)); persist(true);
    });
    handle.addEventListener('pointerdown', (event) => {
      event.preventDefault(); handle.setPointerCapture(event.pointerId);
      const start = event.clientX; const width = workspace[side].width;
      const move = (next) => apply(width + (side === 'right' ? start - next.clientX : next.clientX - start));
      const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); persist(true); };
      handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
    });
    return handle;
  }

  function groupTabs(group, workspace, docs, shellState) {
    const bar = h('div', { class:'copal-note-tabs' });
    const ownsShellControls = shellState.controlGroupId === group.id;
    const leftSlot = h('div', { class:'copal-shell-tab-slot left', 'data-shell-slot':'left' });
    if (ownsShellControls && (shellState.narrow || !workspace.left.open)) leftSlot.append(shellState.controls.left);
    const tablist = h('div', { class:'copal-note-tab-scroll', role:'tablist', 'aria-label':'Open Notes tabs' });
    bar.append(leftSlot, tablist);
    for (const leaf of group.tabs) {
      const doc = docs.find((item) => item.id === leaf.docId); if (!doc) continue;
      const tab = h('div', { class:`copal-note-tab${leaf.id === group.activeLeafId ? ' active' : ''}${leaf.pinned ? ' pinned' : ''}`, role:'tab', 'aria-selected':String(leaf.id === group.activeLeafId), draggable:'true', 'data-leaf-id':leaf.id },
        h('button', { class:'copal-note-tab-label', text:displayName(doc), title:doc.name, 'aria-label':`Open ${doc.name}`, onclick:() => setActive(group.id, leaf.id) }),
        h('button', { class:'copal-note-tab-pin', text:leaf.pinned ? '●' : '○', title:leaf.pinned ? 'Unpin tab' : 'Pin tab', 'aria-pressed':String(leaf.pinned), onclick:() => { leaf.pinned = !leaf.pinned; persist(true); render(); } }),
        h('button', { class:'copal-note-tab-close', text:'×', disabled:leaf.pinned, 'aria-label':`Close ${doc.name}`, onclick:async () => {
          if (!await saveDraft(doc.id)) return;
          const closed = closeWorkspaceLeaf(workspace, leaf.id); if (closed) disposeLeaf(leaf.id);
          syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
        } }));
      tab.addEventListener('auxclick', async (event) => {
        if (event.button !== 1 || leaf.pinned) return;
        event.preventDefault(); if (!await saveDraft(doc.id)) return;
        if (closeWorkspaceLeaf(workspace, leaf.id)) disposeLeaf(leaf.id);
        syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
      });
      tab.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-note-leaf', leaf.id));
      tab.addEventListener('dragover', (event) => event.preventDefault());
      tab.addEventListener('drop', (event) => {
        event.preventDefault(); const source = event.dataTransfer.getData('text/x-copal-note-leaf');
        const index = group.tabs.findIndex((item) => item.id === leaf.id);
        if (source && moveWorkspaceLeaf(workspace, source, group.id, index)) { persist(true); render(); }
      });
      tablist.append(tab);
    }
    const groupMenu = wirePopover(h('details', { class:'copal-leaf-menu copal-group-menu' }, h('summary', { text:'⋯', title:'Tab group actions', 'aria-label':'Tab group actions' }), h('div', { class:'copal-popover-menu' },
      commandButton('Close other tabs', async () => {
        const activeId = group.activeLeafId; const candidates = group.tabs.filter((leaf) => leaf.id !== activeId && !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceOtherLeaves(workspace, activeId)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render();
      }),
      commandButton('Close tab group', async () => {
        const candidates = group.tabs.filter((leaf) => !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceGroup(workspace, group.id)) disposeLeaf(closed.id);
        syncSelectionToModel(workspace); persist(true); render(); focusEmptyWorkspaceIfIdle();
      }))));
    const controls = h('div', { class:'copal-tab-group-controls' },
      commandButton('+', () => showChooser({ title:'Open note in this group', choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:group.id, reuse:false }); persist(true); render(); } }), { title:'Open note', 'aria-label':'Open note' }),
      commandButton('↔', () => showChooser({ title:'Split right', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'horizontal'); persist(true); render(); }, allowCreate:false }), { title:'Split right', 'aria-label':'Split right' }),
      commandButton('↕', () => showChooser({ title:'Split below', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'vertical'); persist(true); render(); }, allowCreate:false }), { title:'Split below', 'aria-label':'Split below' }),
      groupMenu);
    const rightSlot = h('div', { class:'copal-shell-tab-slot right', 'data-shell-slot':'right' });
    if (ownsShellControls && (shellState.compact || !workspace.right.open)) rightSlot.append(shellState.controls.right);
    bar.append(controls, rightSlot); return bar;
  }

  function renderNode(node, workspace, docs, shellState) {
    if (node.type === 'group') {
      const group = h('section', { class:`copal-note-group${node.tabs.some((leaf) => leaf.id === workspace.activeLeafId) ? ' active-group' : ''}`, 'data-group-id':node.id });
      const active = node.tabs.find((leaf) => leaf.id === node.activeLeafId) || node.tabs[0] || null;
      if (active && node.activeLeafId !== active.id) node.activeLeafId = active.id;
      group.append(groupTabs(node, workspace, docs, shellState));
      const body = h('div', { class:'copal-note-group-body' });
      if (active) {
        const doc = docs.find((item) => item.id === active.docId);
        if (doc) body.append(renderLeaf(active, doc, workspace, node));
      } else if (!workspaceLeaves(workspace).length) body.append(emptyWorkspace(workspace));
      else body.append(h('div', { class:'copal-empty' }, h('p', { text:'This tab group is empty.' }), commandButton('Open note', () => showChooser({ choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:node.id }); persist(true); render(); } }))));
      group.addEventListener('dragover', (event) => event.preventDefault());
      group.addEventListener('drop', (event) => {
        const leafId = event.dataTransfer.getData('text/x-copal-note-leaf');
        if (leafId && moveWorkspaceLeaf(workspace, leafId, node.id)) { event.preventDefault(); persist(true); render(); }
      });
      group.append(body); return group;
    }
    const renderedOrientation = node.orientation === 'horizontal' && window.matchMedia('(max-width: 760px)').matches ? 'vertical' : node.orientation;
    const split = h('div', { class:`copal-note-split ${renderedOrientation}`, 'data-split-id':node.id, 'data-stored-orientation':node.orientation });
    node.children.forEach((child, index) => {
      const wrap = h('div', { class:'copal-note-split-child', style:`flex-basis:${node.sizes[index] || 50}%` }, renderNode(child, workspace, docs, shellState));
      split.append(wrap);
      if (index >= node.children.length - 1) return;
      const handle = h('div', { class:'copal-note-splitter', role:'separator', tabindex:'0', 'aria-label':`Resize ${renderedOrientation} split`, 'aria-orientation':renderedOrientation === 'horizontal' ? 'vertical' : 'horizontal', 'aria-valuemin':'15', 'aria-valuemax':'85', 'aria-valuenow':String(Math.round(node.sizes[0])) });
      handle.addEventListener('keydown', (event) => {
        const negative = renderedOrientation === 'horizontal' ? event.key === 'ArrowLeft' : event.key === 'ArrowUp';
        const positive = renderedOrientation === 'horizontal' ? event.key === 'ArrowRight' : event.key === 'ArrowDown';
        if (!negative && !positive) return; event.preventDefault();
        resizeWorkspaceSplit(workspace, node.id, node.sizes[0] + (negative ? -5 : 5)); handle.setAttribute('aria-valuenow', String(Math.round(node.sizes[0]))); persist(true); render();
      });
      handle.addEventListener('pointerdown', (event) => {
        event.preventDefault(); handle.setPointerCapture(event.pointerId);
        const rect = split.getBoundingClientRect();
        const move = (next) => {
          const value = renderedOrientation === 'horizontal' ? ((next.clientX - rect.left) / rect.width) * 100 : ((next.clientY - rect.top) / rect.height) * 100;
          if (resizeWorkspaceSplit(workspace, node.id, value)) {
            handle.setAttribute('aria-valuenow', String(Math.round(node.sizes[0])));
            split.children[0].style.flexBasis = `${node.sizes[0]}%`; split.children[2].style.flexBasis = `${node.sizes[1]}%`;
          }
        };
        const up = () => { handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', up); persist(true); };
        handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', up);
      });
      split.append(handle);
    });
    return split;
  }

  function leafHeader(cache, leaf, doc, workspace, group) {
    const parts = doc.name.split('/');
    const breadcrumb = h('div', { class:'copal-note-breadcrumb', 'aria-label':'Document path' });
    parts.slice(0, -1).forEach((part, index) => breadcrumb.append(h('span', { text:part }), h('span', { text:index < parts.length - 2 ? ' / ' : '' })));
    if (doc.readOnly) {
      const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Knowledge note actions', 'aria-label':'Knowledge note actions' }), h('div', { class:'copal-popover-menu' },
        commandButton('History', () => showHistory(doc)),
        commandButton('Split right', () => showChooser({ title:'Split right', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'horizontal'); persist(true); render(); }, allowCreate:false })),
        commandButton('Split below', () => showChooser({ title:'Split below', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'vertical'); persist(true); render(); }, allowCreate:false })),
        commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
        commandButton('Reveal in Notes', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }))));
      cache.header.replaceChildren(breadcrumb, h('strong', { class:'copal-inline-title', text:displayName(doc) }), h('span', { class:'copal-leaf-mode', text:'Built-in knowledge · read only' }), menu);
      return;
    }
    const fileName = parts.at(-1); const extension = /\.[A-Za-z0-9]+$/.exec(fileName)?.[0] || '';
    const title = h('input', { class:'copal-inline-title', value:displayName(doc), 'aria-label':'Note title' });
    title.addEventListener('change', async () => {
      const entered = title.value.trim();
      const nextFile = entered && /\.[A-Za-z0-9]+$/.test(entered) ? entered : `${entered}${extension}`;
      const name = [...parts.slice(0, -1), nextFile].filter(Boolean).join('/');
      if (!entered) { title.value = displayName(doc); return; }
      if (name !== doc.name) {
        try { await renameNote(doc, name); }
        catch (error) { title.value = displayName(doc); context().window.setStatus(error.message, true); }
      }
    });
    const menu = wirePopover(h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Note actions', 'aria-label':'Note actions' }), h('div', { class:'copal-popover-menu' },
      commandButton(doc.kind === 'note' ? 'Editing mode' : 'Live Preview', () => { setWorkspaceLeafMode(workspace, leaf.id, 'live'); persist(true); render(); }),
      ...(doc.kind === 'note' ? [] : [commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); })]),
      commandButton('Reading mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'reading'); persist(true); render(); }),
      commandButton(workspace.settings.previewLayout === 'inline' ? 'Use side-by-side preview' : 'Use inline preview', () => setPreviewLayout(workspace.settings.previewLayout === 'inline' ? 'side-by-side' : 'inline')),
      commandButton('Find and replace', () => cache.editor && showFindReplace(cache.editor)),
      commandButton('History', () => showHistory(doc)),
      commandButton('Split right', () => showChooser({ title:'Split right', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'horizontal'); persist(true); render(); }, allowCreate:false })),
      commandButton('Split below', () => showChooser({ title:'Split below', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'vertical'); persist(true); render(); }, allowCreate:false })),
      commandButton('Move tab left', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index > 0 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index - 1)) { persist(true); render(); } }),
      commandButton('Move tab right', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index >= 0 && index < group.tabs.length - 1 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index + 1)) { persist(true); render(); } }),
      commandButton(workspace.bookmarks?.includes(doc.id) ? 'Remove bookmark' : 'Add bookmark', () => toggleBookmark(doc.id)),
      commandButton('Reveal in Notes', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }),
      ...(leaf.view === 'canvas' || leaf.view === 'base' ? [commandButton(leaf.rawSource ? 'Back to typed view' : 'View raw source', () => { leaf.rawSource = !leaf.rawSource; persist(true); render(); })] : []),
      commandButton('Rename', () => renameWithForm(doc)),
      commandButton('Move to trash', () => deleteDocument(doc), { class:'copal-btn danger' }))));
    const mode = leaf.mode === 'live' ? doc.kind === 'note' ? 'Editing' : 'Live Preview' : leaf.mode === 'source' ? 'Source' : 'Reading';
    cache.header.replaceChildren(breadcrumb, title, h('span', { class:'copal-leaf-mode', text:mode }), menu);
  }

  function renderLeaf(leaf, doc, workspace, group) {
    const current = context();
    let cache = current.noteLeafViews.get(leaf.id);
    if (!cache || cache.docId !== doc.id || cache.view !== leaf.view) {
      if (cache) disposeLeaf(leaf.id);
      const root = h('article', { class:'copal-note-leaf', 'data-leaf-id':leaf.id, 'data-view-type':leaf.view });
      const header = h('header', { class:'copal-note-view-header' });
      const body = h('div', { class:'copal-note-leaf-content' });
      const propsFooter = h('div', { class:'copal-note-props-footer' });
      const status = h('footer', { class:'copal-note-status', role:'status', 'aria-live':'polite' });
      root.append(header, body, propsFooter, status);
      cache = { root, header, body, propsFooter, status, docId:doc.id, view:leaf.view, saveState:'saved', cursorLine:1, editor:null };
      current.noteLeafViews.set(leaf.id, cache);
    }
    cache.leaf = leaf; cache.doc = doc;
    if (leaf.view === 'event') {
      // Auto-open the native event editor for copal-event documents.
      if (openEventEditor) openEventEditor(doc.id);
      cache.header.replaceChildren(
        h('strong', { class:'copal-inline-title', text:doc.name.split('/').pop().replace(/\.md$/i, '') }),
        h('span', { class:'copal-leaf-mode', text:'Event' }),
      );
      cache.body.replaceChildren();
      cache.status.replaceChildren(h('span', { text:'Event · opened in event editor' }));
      return cache.root;
    }
    if (leaf.view === 'timeline') {
      cache.header.replaceChildren(
        h('strong', { class:'copal-inline-title', text:'Timeline' }),
        h('span', { class:'copal-leaf-mode', text:'Canonical Redb view' }),
      );
      renderTimeline?.(cache.body);
      cache.status.replaceChildren(h('span', { text:'Timeline · canonical Copal planning records' }));
      return cache.root;
    }
    leafHeader(cache, leaf, doc, workspace, group);
    if (doc.readOnly) {
      cache.body.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id])));
      cache.status.replaceChildren(h('span', { text:`Built-in knowledge · ${wordCount(sourceValue(doc))} words · read only` }));
      return cache.root;
    }
    if (doc.note_error) cache.body.replaceChildren(h('div', { class:'copal-inspector-error' },
      h('strong', { text:'This database note could not be decoded' }),
      h('p', { text:'Its stored record is preserved and has not been opened for editing.' }),
      h('p', { text:doc.note_error })));
    else if (leaf.view === 'markdown' || leaf.view === 'note') updateMarkdownLeaf(cache, leaf, doc, workspace);
    else if (leaf.rawSource) updateSourceLeaf(cache, leaf, doc, workspace);
    else if (leaf.view === 'canvas') updateCanvasLeaf(cache, doc);
    else if (leaf.view === 'base') updateBaseLeaf(cache, doc);
    else updateAssetLeaf(cache, doc, leaf.view);
    // Properties footer for note/markdown views — inline editable card
    if ((leaf.view === 'markdown' || leaf.view === 'note') && !doc.readOnly && !doc.note_error) {
      const props = buildInlineProps(doc);
      cache.propsFooter.replaceChildren(props);
      cache.propsFooter.style.display = '';
    } else {
      cache.propsFooter.replaceChildren();
      cache.propsFooter.style.display = 'none';
    }
    updateLeafStatus(cache);
    return cache.root;
  }

  function sourceValue(doc) {
    return context().noteDrafts.get(doc.id)?.value ?? String(doc.text || '');
  }

  function ensureEditor(cache, leaf, doc, workspace) {
    if (!cache.editorWrap) {
      cache.editorWrap = h('div', { class:'copal-note-editing-surface' });
      cache.host = h('div', { class:'copal-codemirror-host' });
      cache.preview = h('article', { class:'copal-note-live-preview' });
      cache.editorWrap.append(cache.host, cache.preview);
    }
    if (!cache.editor) {
      const frontmatter = parseFrontmatter(sourceValue(doc));
      const defaultCursor = frontmatter.valid && frontmatter.present ? Math.min(sourceValue(doc).length, frontmatter.end + 1) : 0;
      cache.editor = createMarkdownEditor({
        parent:cache.host, doc:sourceValue(doc), label:`Edit ${doc.name}`, selection:leaf.selection || { anchor:defaultCursor, head:defaultCursor },
        scrollTop:leaf.scrollTop,
        mode:leaf.mode === 'live' && workspace.settings.previewLayout === 'inline' ? 'live' : 'source',
        lineNumbers:workspace.settings.lineNumbers, readableLineWidth:workspace.settings.readableLineWidth,
        onSelection:(selection) => { leaf.selection = selection; cache.cursorLine = selection.line; persist(); updateLeafStatus(cache); },
        onScroll:(scrollTop) => { leaf.scrollTop = scrollTop; persist(); },
        onChange:(value) => { doc.text = value; queueSave(doc, value); syncDocumentEditors(doc.id, value, cache.editor); updatePreview(cache, doc, value); updateLeafStatus(cache); },
        onCommand:(command) => {
          if (command === 'save') void saveDraft(doc.id);
          else if (command === 'quick-open') showChooser();
          else if (command === 'palette') showCommands();
          else if (command === 'search') showSearch();
        },
      });
      state.noteEditors.add(cache.editor);
      context().noteMetrics.editorConstructions += 1;
    } else if (!context().noteDrafts.has(doc.id)) cache.editor.setValue(String(doc.text || ''));
    cache.editor.setMode(leaf.mode === 'live' && workspace.settings.previewLayout === 'inline' ? 'live' : 'source');
    cache.editor.setLineNumbers(workspace.settings.lineNumbers);
    cache.editor.setReadableLineWidth(workspace.settings.readableLineWidth);
    return cache.editorWrap;
  }

  function updatePreview(cache, doc, value = sourceValue(doc)) {
    if (!cache.preview) return;
    cache.preview.replaceChildren(renderMarkdown(value, new Set([doc.id])));
    applyCompletedVisibility(cache.preview);
    wireInteractiveTables(cache.preview, value, doc);
  }

  function wireInteractiveTables(container, source, doc) {
    // Find all table blocks in the source
    const sourceLines = source.split('\n');
    const sourceTables = [];
    let i = 0;
    while (i < sourceLines.length) {
      if (sourceLines[i].includes('|') && sourceLines[i].trim() && i + 1 < sourceLines.length) {
        // Check if next line is a separator
        const sepLine = sourceLines[i + 1] || '';
        if (/^\|?\s*:?-{3,}/.test(sepLine.trim())) {
          const block = [sourceLines[i], sourceLines[i + 1]];
          let j = i + 2;
          while (j < sourceLines.length && sourceLines[j].includes('|') && sourceLines[j].trim()) {
            block.push(sourceLines[j]);
            j++;
          }
          sourceTables.push({ text: block.join('\n'), startLine: i });
          i = j;
          continue;
        }
      }
      i++;
    }
    // Replace static tables with interactive widgets
    const staticTables = container.querySelectorAll('.copal-markdown-table');
    let tableIdx = 0;
    for (const st of staticTables) {
      const src = sourceTables[tableIdx];
      tableIdx++;
      if (!src) continue;
      const model = parseTable(src.text, src.startLine);
      if (!model.valid) {
        // Malformed: wrap with warning, keep raw source
        const wrapper = document.createElement('div');
        wrapper.className = 'copal-table-malformed';
        wrapper.textContent = src.text;
        st.replaceWith(wrapper);
        continue;
      }
      const onEdit = (edit) => {
        const result = applyTableEdit(source, model, edit);
        if (result.changes.length) {
          // Try CodeMirror dispatch for atomic undo, fall back to setValue
          if (cache.editor?.view?.dispatch) {
            cache.editor.view.dispatch({ changes: result.changes });
          } else if (cache.editor?.dispatch) {
            cache.editor.dispatch({ changes: result.changes });
          } else if (cache.editor?.setValue) {
            cache.editor.setValue(result.newText);
          }
        }
      };
      const widget = createTableWidget(model, onEdit);
      st.replaceWith(widget);
    }
  }

  function applyCompletedVisibility(container) {
    if (!container) return;
    const workspace = ensureWorkspace();
    if (workspace?.settings?.completedVisibility !== 'hide') return;
    const tasks = container.querySelectorAll('.copal-markdown-task');
    let hidden = 0;
    for (const task of tasks) {
      const checkbox = task.querySelector('input[type="checkbox"]');
      if (checkbox?.checked) { task.hidden = true; hidden += 1; }
    }
    if (hidden) {
      const status = context()?.window?.root?.querySelector('.copal-notes-workspace');
      if (status) status.setAttribute('aria-label', `${hidden} completed task${hidden === 1 ? '' : 's'} hidden`);
    }
  }

  function updateMarkdownLeaf(cache, leaf, doc, workspace) {
    const editorWrap = ensureEditor(cache, leaf, doc, workspace);
    cache.root.dataset.mode = leaf.mode;
    cache.root.dataset.previewLayout = workspace.settings.previewLayout;
    if (leaf.mode === 'reading') {
      cache.reading ||= h('article', { class:'copal-note-reading' });
      cache.reading.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id])));
      applyCompletedVisibility(cache.reading);
      cache.body.replaceChildren(cache.reading);
      return;
    }
    const sideBySide = leaf.mode === 'live' && workspace.settings.previewLayout === 'side-by-side';
    editorWrap.classList.toggle('side-by-side', sideBySide);
    cache.preview.hidden = !sideBySide;
    if (sideBySide) updatePreview(cache, doc);
    cache.body.replaceChildren(editorWrap);
  }

  function updateSourceLeaf(cache, leaf, doc, workspace) {
    const editorWrap = ensureEditor(cache, { ...leaf, mode:'source' }, doc, workspace);
    editorWrap.classList.remove('side-by-side'); cache.preview.hidden = true;
    cache.body.replaceChildren(editorWrap);
  }

  function updateCanvasLeaf(cache, doc) {
    const parsed = parseCanvasDocument(sourceValue(doc));
    const surface = h('div', { class:'copal-canvas-view' });
    if (!parsed.valid) surface.append(h('div', { class:'copal-empty' }, h('h2', { text:'Canvas needs repair' }), h('p', { text:parsed.error }), h('p', { text:'Use Note actions → View raw source to repair it without losing data.' })));
    else if (!parsed.nodes.length) surface.append(h('div', { class:'copal-empty', text:'This Canvas contains no nodes.' }));
    else {
      const minX = Math.min(...parsed.nodes.map((node) => node.x)); const minY = Math.min(...parsed.nodes.map((node) => node.y));
      const maxX = Math.max(...parsed.nodes.map((node) => node.x + node.width)); const maxY = Math.max(...parsed.nodes.map((node) => node.y + node.height));
      const board = h('div', { class:'copal-canvas-board', style:`width:${Math.max(700, maxX - minX + 160)}px;height:${Math.max(480, maxY - minY + 160)}px` });
      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); svg.setAttribute('class', 'copal-canvas-edges');
      const byId = new Map(parsed.nodes.map((node) => [node.id, node]));
      for (const edge of parsed.edges) {
        const from = byId.get(edge.from); const to = byId.get(edge.to); if (!from || !to) continue;
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', String(from.x - minX + 80 + from.width / 2)); line.setAttribute('y1', String(from.y - minY + 80 + from.height / 2));
        line.setAttribute('x2', String(to.x - minX + 80 + to.width / 2)); line.setAttribute('y2', String(to.y - minY + 80 + to.height / 2));
        svg.append(line);
      }
      board.append(svg);
      for (const node of parsed.nodes) board.append(h('article', { class:'copal-canvas-node', style:`left:${node.x - minX + 80}px;top:${node.y - minY + 80}px;width:${node.width}px;min-height:${node.height}px` }, h('small', { text:node.type }), h('p', { text:node.label })));
      surface.append(board);
    }
    cache.body.replaceChildren(surface);
  }

  function updateBaseLeaf(cache, doc) {
    const token = (cache.queryToken || 0) + 1; cache.queryToken = token;
    const host = h('div', { class:'copal-base-leaf' }, h('div', { class:'copal-empty', text:'Querying live Redb Base…' }));
    cache.body.replaceChildren(host);
    api(`/bases/${encodeURIComponent(doc.id)}/query?page=1&page_size=100`).then((result) => {
      if (cache.queryToken !== token) return;
      const toolbar = h('div', { class:'copal-base-leaf-toolbar' }, h('strong', { text:result.view?.name || doc.name }), h('span', { text:`${result.total} live result${result.total === 1 ? '' : 's'}` }), commandButton('Open full Bases app', () => { state.baseId = doc.id; openOtherView(doc.id, 'bases'); }));
      if (!result.rows?.length) { host.replaceChildren(toolbar, h('div', { class:'copal-empty', text:'This live query returned no rows.' })); return; }
      const table = h('table', { class:'copal-table copal-base-table' });
      table.append(h('thead', {}, h('tr', {}, result.view.columns.map((column) => h('th', { text:column.label }))))) ;
      const body = h('tbody');
      for (const row of result.rows) body.append(h('tr', {}, result.view.columns.map((column) => h('td', {}, h('button', { class:'copal-base-cell', text:formatBaseCell(row.values?.[column.property]), onclick:() => open(row.documentId) })))));
      table.append(body); host.replaceChildren(toolbar, h('div', { class:'copal-base-table-wrap' }, table));
    }).catch((error) => {
      if (cache.queryToken === token) host.replaceChildren(h('div', { class:'copal-empty' }, h('h2', { text:'Base query failed' }), h('p', { text:error.message }), h('p', { text:'Use Note actions → View raw source to repair the definition.' })));
    });
  }

  function updateAssetLeaf(cache, doc, view) {
    const url = `${state.api}/api/copal/assets/${encodeURIComponent(doc.id)}?workspace=${encodeURIComponent(state.workspace)}`;
    const host = h('div', { class:`copal-asset-view ${view}` });
    if (view === 'image') host.append(h('img', { src:url, alt:doc.name }));
    else if (view === 'audio') host.append(h('audio', { src:url, controls:true, 'aria-label':doc.name }));
    else if (view === 'video') host.append(h('video', { src:url, controls:true, 'aria-label':doc.name }));
    else if (view === 'pdf') host.append(h('iframe', { src:url, title:doc.name }));
    else host.append(h('div', { class:'copal-empty' }, h('p', { text:`No inline viewer for ${doc.name}.` }), h('a', { class:'copal-btn', href:url, download:doc.name, text:'Download attachment' })));
    cache.body.replaceChildren(host);
  }

  function updateLeafStatus(cache) {
    if (!cache?.status || !cache.doc) return;
    const value = cache.editor?.getValue?.() ?? sourceValue(cache.doc);
    const selection = cache.editor?.getSelection?.();
    const selected = selection ? Math.abs(selection.head - selection.anchor) : 0;
    const mode = cache.leaf?.mode === 'live' && ensureWorkspace().settings.previewLayout === 'side-by-side' ? 'Editing · side-by-side' : cache.leaf?.mode === 'live' ? cache.doc.kind === 'note' ? 'Editing' : 'Live Preview' : cache.leaf?.mode === 'source' ? 'Source' : cache.leaf?.mode === 'reading' ? 'Reading' : cache.view;
    cache.status.replaceChildren(
      h('span', { class:`copal-save-state ${cache.saveState}`, text:cache.saveState === 'saved' ? 'Saved' : cache.saveState === 'saving' ? 'Saving…' : cache.saveState === 'conflict' ? 'Conflict' : 'Unsaved' }),
      h('span', { text:`${wordCount(value)} words` }), h('span', { text:`${value.length} characters` }),
      ...(cache.cursorLine ? [h('span', { text:`Ln ${cache.cursorLine}` })] : []), ...(selected ? [h('span', { text:`${selected} selected` })] : []), h('span', { text:`${mode} · ${cache.doc.kind === 'note' ? 'database record' : cache.doc.kind} · Redb` }));
  }

  function showFindReplace(editor) {
    const dialog = h('dialog', { class:'copal-dialog copal-find-replace' }, h('h2', { text:'Find and replace' }));
    const find = h('input', { type:'text', placeholder:'Find', 'aria-label':'Find' });
    const replacement = h('input', { type:'text', placeholder:'Replace', 'aria-label':'Replace' });
    const feedback = h('span', { role:'status' });
    dialog.append(find, replacement, h('div', { class:'copal-dialog-actions' },
      commandButton('Find next', () => { feedback.textContent = editor.find(find.value) ? '' : 'No match'; }),
      commandButton('Replace', () => { feedback.textContent = editor.replace(find.value, replacement.value) ? 'Replaced' : 'No match'; }),
      commandButton('Replace all', () => { feedback.textContent = `${editor.replace(find.value, replacement.value, true)} replaced`; }),
      commandButton('Close', () => dialog.close())), feedback);
    wireDialog(dialog); document.body.append(dialog); dialog.showModal(); find.focus();
  }

  function propertyInput(entry, type) {
    if (type === 'checkbox') { const input = h('input', { type:'checkbox' }); input.checked = entry.value === true; return input; }
    if (type === 'list' || type === 'tags') return h('input', { type:'text', value:Array.isArray(entry.value) ? entry.value.join(', ') : String(entry.value || '') });
    if (type === 'object') return h('input', { type:'text', value:JSON.stringify(entry.value || {}) });
    return h('input', { type:type === 'datetime' ? 'datetime-local' : type, value:entry.value == null ? '' : String(entry.value) });
  }

  // Inline properties card for the editor footer — compact, editable, WYSIWYG-integrated
  function buildInlineProps(doc) {
    const pane = h('div', { class:'copal-inline-props' });
    const native = doc.kind === 'note';
    const entries = native
      ? Object.entries(doc.properties || {})
      : (() => { const p = parseFrontmatter(sourceValue(doc)); return p.valid ? p.entries.map((e) => [e.key, e.value]) : []; })();
    if (!entries.length && doc.readOnly) {
      pane.append(h('span', { class:'copal-inline-props-empty', text:'No properties' }));
      return pane;
    }
    const commitNative = (pairs) => {
      doc.properties = Object.fromEntries(pairs);
      doc.frontmatter = doc.properties;
      queueSave(doc, sourceValue(doc));
      render();
    };
    // Render each property as an inline chip
    for (const [key, value] of entries) {
      const display = value == null ? '' : Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
      const chip = h('span', { class:'copal-inline-props-chip', tabindex:'0' },
        h('strong', { text:key }),
        h('span', { text:display || '—' }));
      if (!doc.readOnly) {
        chip.addEventListener('dblclick', () => {
          const newVal = prompt(`Edit ${key}:`, display);
          if (newVal === null) return;
          if (native) {
            try { commitNative(entries.map(([k, v]) => [k, k === key ? newVal : v])); } catch (e) { context().window.setStatus(e.message, true); }
          } else {
            try { applyDocumentSource(doc, setFrontmatterProperty(sourceValue(doc), key, newVal)); render(); } catch (e) { context().window.setStatus(e.message, true); }
          }
        });
        const removeBtn = h('button', { class:'copal-inline-props-remove', text:'×', title:`Remove ${key}`, 'aria-label':`Remove ${key}`, onclick:() => {
          if (native) commitNative(entries.filter(([k]) => k !== key));
          else { applyDocumentSource(doc, removeFrontmatterProperty(sourceValue(doc), key)); render(); }
        } });
        chip.append(removeBtn);
      }
      pane.append(chip);
    }
    // Add property form
    if (!doc.readOnly) {
      const addBtn = h('button', { class:'copal-inline-props-add', text:'＋', title:'Add property', 'aria-label':'Add property' });
      addBtn.addEventListener('click', () => {
        const key = prompt('Property name:');
        if (!key?.trim()) return;
        const val = prompt('Value:', '');
        if (val === null) return;
        if (native) {
          try { commitNative([...entries, [key.trim(), val]]); } catch (e) { context().window.setStatus(e.message, true); }
        } else {
          try { applyDocumentSource(doc, setFrontmatterProperty(sourceValue(doc), key.trim(), val)); render(); } catch (e) { context().window.setStatus(e.message, true); }
        }
      });
      pane.append(addBtn);
    }
    return pane;
  }

  function propertiesPane(doc) {
    const pane = h('div', { class:'copal-properties-pane' });
    const native = doc.kind === 'note';
    const parsed = native
      ? { valid:true, entries:Object.entries(doc.properties || {}).map(([key, value]) => ({ key, value })) }
      : parseFrontmatter(sourceValue(doc));
    if (!parsed.valid) return h('div', { class:'copal-inspector-error' }, h('strong', { text:'Properties unavailable' }), h('p', { text:parsed.error }), h('p', { text:'Source is preserved. Repair the opening/closing --- markers in Source mode.' }));
    if (doc.readOnly) {
      pane.append(h('p', { class:'copal-empty-inline', text:'Built-in shared knowledge · read only' }));
      for (const entry of parsed.entries) pane.append(h('div', { class:'copal-property-editor' },
        h('strong', { text:entry.key }), h('span', { text:typeof entry.value === 'string' ? entry.value : JSON.stringify(entry.value) })));
      return pane;
    }
    const commitNative = (pairs) => {
      const keys = pairs.map(([key]) => key);
      if (keys.some((key) => !/^[A-Za-z0-9_.-]+$/.test(key))) throw new Error('Property names may contain letters, numbers, dots, dashes, and underscores.');
      if (new Set(keys).size !== keys.length) throw new Error('Property names must be unique.');
      doc.properties = Object.fromEntries(pairs);
      doc.frontmatter = doc.properties;
      queueSave(doc, sourceValue(doc));
      render();
    };
    for (const entry of parsed.entries) {
      const type = native && entry.value && typeof entry.value === 'object' && !Array.isArray(entry.value) ? 'object' : propertyType(entry.value, entry.key);
      const keyInput = h('input', { class:'copal-property-key', value:entry.key, 'aria-label':`Property name ${entry.key}` });
      keyInput.addEventListener('change', () => {
        try {
          if (native) commitNative(Object.entries(doc.properties || {}).map(([key, value]) => [key === entry.key ? keyInput.value.trim() : key, value]));
          else { applyDocumentSource(doc, renameFrontmatterProperty(sourceValue(doc), entry.key, keyInput.value.trim())); render(); }
        }
        catch (error) { keyInput.value = entry.key; context().window.setStatus(error.message, true); }
      });
      const row = h('div', { class:'copal-property-editor' }, keyInput);
      if (type === 'source') {
        row.append(h('span', { text:'Complex value—edit in Source mode.' }));
      } else {
        const select = h('select', { 'aria-label':`Type for ${entry.key}` }, PROPERTY_TYPES.map((item) => h('option', { value:item, text:item })));
        select.value = type;
        let input = propertyInput(entry, type);
        const commit = () => {
          try {
            const nextType = select.value;
            const value = nextType === 'checkbox' ? input.checked : input.value;
            if (native) commitNative(Object.entries(doc.properties || {}).map(([key, current]) => [key, key === entry.key ? coercePropertyValue(value, nextType) : current]));
            else { const content = setFrontmatterProperty(sourceValue(doc), entry.key, value, nextType); applyDocumentSource(doc, content); render(); }
          } catch (error) {
            context().window.setStatus(error.message, true);
          }
        };
        input.setAttribute('aria-label', `Value for ${entry.key}`); input.addEventListener('change', commit);
        select.addEventListener('change', commit);
        row.append(select, input);
      }
      const move = (direction) => {
        if (!native) { applyDocumentSource(doc, moveFrontmatterProperty(sourceValue(doc), entry.key, direction)); render(); return; }
        const pairs = Object.entries(doc.properties || {}); const index = pairs.findIndex(([key]) => key === entry.key); const target = index + direction;
        if (index < 0 || target < 0 || target >= pairs.length) return;
        [pairs[index], pairs[target]] = [pairs[target], pairs[index]]; commitNative(pairs);
      };
      row.append(commandButton('↑', () => move(-1), { title:`Move ${entry.key} up`, 'aria-label':`Move ${entry.key} up` }));
      row.append(commandButton('↓', () => move(1), { title:`Move ${entry.key} down`, 'aria-label':`Move ${entry.key} down` }));
      row.append(commandButton('×', () => {
        if (native) commitNative(Object.entries(doc.properties || {}).filter(([key]) => key !== entry.key));
        else { const content = removeFrontmatterProperty(sourceValue(doc), entry.key); applyDocumentSource(doc, content); render(); }
      }, { title:`Remove ${entry.key}`, 'aria-label':`Remove ${entry.key}` }));
      pane.append(row);
    }
    const add = h('form', { class:'copal-property-add' });
    const key = h('input', { placeholder:'property', 'aria-label':'New property name' });
    const value = h('input', { placeholder:'value', 'aria-label':'New property value' });
    const type = h('select', { 'aria-label':'New property type' }, PROPERTY_TYPES.map((item) => h('option', { value:item, text:item })));
    add.append(key, type, value, commandButton('Add', () => {}, { type:'submit' }));
    add.addEventListener('submit', async (event) => {
      event.preventDefault(); if (!key.value.trim()) return;
      try {
        if (native) commitNative([...Object.entries(doc.properties || {}), [key.value.trim(), coercePropertyValue(value.value, type.value)]]);
        else { const content = setFrontmatterProperty(sourceValue(doc), key.value.trim(), value.value, type.value); applyDocumentSource(doc, content); render(); }
      } catch (error) { context().window.setStatus(error.message, true); }
    });
    pane.append(add);
    if (!parsed.entries.length) pane.prepend(h('p', { class:'copal-empty-inline', text:'No properties yet.' }));
    return pane;
  }

  function linksPane(doc) {
    const pane = h('div', { class:'copal-links-pane' });
    const filter = h('input', { class:'copal-links-filter', type:'search', placeholder:'Filter links…', 'aria-label':'Filter linked views' });
    const sort = h('select', { class:'copal-links-sort', 'aria-label':'Sort linked views' }, h('option', { value:'name', text:'Name' }), h('option', { value:'path', text:'Path' }));
    pane.append(h('div', { class:'copal-links-controls' }, filter, sort));
    const section = (title) => { const root = h('section', {}, h('h3', { text:title })); pane.append(root); return root; };
    const outgoing = section('Outgoing links');
    const relations = doc.kind === 'note'
      ? (doc.relations || []).filter((relation) => ['link', 'embed'].includes(relation.kind))
      : (doc.links || []).map((target) => ({ kind:'link', target }));
    for (const relation of [...relations].sort((a, b) => a.target.localeCompare(b.target))) {
      const target = state.docs.find((candidate) => candidate.id === relation.targetDocumentId) || resolveDocumentLink(state.docs, relation.target);
      const label = relation.kind === 'embed' ? `Embed · ${relation.target}` : relation.target;
      outgoing.append(h('button', { class:'copal-doc-row copal-link-result', 'data-sort-name':target ? displayName(target) : relation.target, 'data-sort-path':target?.name || relation.target, disabled:!target, onclick:() => target && open(target.id) }, h('strong', { text:target ? label : `${label} · unresolved` }), target ? h('small', { text:target.name }) : null));
    }
    if (!relations.length) outgoing.append(h('p', { class:'copal-empty-inline', text:'No outgoing links.' }));
    const backlinks = section('Linked mentions');
    const incoming = linkedMentions(state.docs, doc);
    for (const mention of incoming) backlinks.append(h('button', { class:'copal-doc-row copal-link-result', 'data-sort-name':displayName(mention.doc), 'data-sort-path':mention.doc.name, onclick:() => {
      const leaf = open(mention.doc.id); if (mention.line) requestAnimationFrame(() => context().noteLeafViews.get(leaf?.id)?.editor?.focusLine(mention.line));
    } }, h('strong', { text:mention.doc.name }), h('small', { text:mention.snippet })));
    if (!incoming.length) backlinks.append(h('p', { class:'copal-empty-inline', text:'No linked mentions.' }));
    const unlinked = section('Unlinked mentions');
    for (const mention of unlinkedMentions(state.docs, doc)) unlinked.append(h('button', { class:'copal-mention-row copal-link-result', 'data-sort-name':displayName(mention.doc), 'data-sort-path':mention.doc.name, onclick:() => open(mention.doc.id) }, h('strong', { text:mention.doc.name }), h('span', { text:mention.snippet })));
    if (unlinked.children.length === 1) unlinked.append(h('p', { class:'copal-empty-inline', text:'No unlinked mentions.' }));
    const refresh = () => {
      const query = filter.value.trim().toLowerCase();
      for (const result of pane.querySelectorAll('.copal-link-result')) result.hidden = !!query && !result.textContent.toLowerCase().includes(query);
      for (const root of pane.querySelectorAll('section')) {
        const rows = [...root.querySelectorAll('.copal-link-result')].sort((a, b) => String(a.dataset[sort.value === 'path' ? 'sortPath' : 'sortName']).localeCompare(String(b.dataset[sort.value === 'path' ? 'sortPath' : 'sortName'])));
        root.append(...rows);
      }
    };
    filter.addEventListener('input', refresh); sort.addEventListener('change', refresh);
    return pane;
  }

  function focusOutline(leaf, entry) {
    const cache = context().noteLeafViews.get(leaf.id);
    if (leaf.mode === 'reading') {
      cache?.reading?.querySelector(`[data-line="${entry.line}"]`)?.scrollIntoView({ block:'center' });
      return;
    }
    cache?.editor?.focusLine(entry.line);
  }

  function outlinePane(doc, workspace) {
    const pane = h('div', { class:'copal-outline-pane' });
    const entries = outlineEntries(sourceValue(doc));
    const leaf = workspaceLeaves(workspace).find((item) => item.docId === doc.id) || activeLeaf(workspace);
    const currentEntry = (entry, source) => {
      const current = outlineEntries(source);
      return current.find((item) => item.line === entry.line && item.text === entry.text)
        || current.filter((item) => item.level === entry.level && item.text === entry.text).sort((a, b) => Math.abs(a.line - entry.line) - Math.abs(b.line - entry.line))[0]
        || null;
    };
    const move = (entry, direction) => {
      const source = sourceValue(doc); const actual = currentEntry(entry, source); if (!actual) return;
      const content = moveHeadingSection(source, actual.line, direction);
      if (content === source) return;
      applyDocumentSource(doc, content); render();
    };
    const moveTo = (entry, target) => {
      const source = sourceValue(doc); const actual = currentEntry(entry, source); const actualTarget = currentEntry(target, source);
      if (!actual || !actualTarget) return;
      const content = moveHeadingSectionTo(source, actual.line, actualTarget.line);
      if (content === source) return;
      applyDocumentSource(doc, content); render();
    };
    for (const entry of entries) {
      const row = h('div', { class:'copal-outline-entry', draggable:'true', style:`--depth:${entry.level}`, 'data-line':entry.line },
        h('button', { class:'copal-outline-row', text:entry.text, onclick:() => focusOutline(leaf, entry) }),
        commandButton('↑', () => move(entry, -1), { 'aria-label':`Move ${entry.text} up` }),
        commandButton('↓', () => move(entry, 1), { 'aria-label':`Move ${entry.text} down` }));
      row.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-heading-line', String(entry.line)));
      row.addEventListener('dragover', (event) => event.preventDefault());
      row.addEventListener('drop', (event) => { event.preventDefault(); const from = Number(event.dataTransfer.getData('text/x-copal-heading-line')); const source = entries.find((item) => item.line === from); if (source && source.line !== entry.line) moveTo(source, entry); });
      pane.append(row);
    }
    if (!entries.length) pane.append(h('p', { class:'copal-empty-inline', text:'No headings.' }));
    return pane;
  }

  function rightSidebar(workspace, shellState) {
    const doc = inspectorDoc(workspace);
    const aside = h('aside', { id:'copal-notes-right-sidebar', class:'copal-notes-sidebar' });
    const tabs = h('div', { class:'copal-inspector-tabs', role:'tablist' });
    const panelIds = workspacePanelsForSide(workspace, 'right');
    for (const key of panelIds) {
      const def = NOTES_PANELS[key];
      if (!def) continue;
      tabs.append(h('button', { class:workspace.right.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.right.tab === key), text:def.label, 'data-panel-id':key, onclick:() => { workspace.right.tab = key; persist(true); render(); } }));
    }
    addRovingFocus(tabs, panelIds, (id) => { workspace.right.tab = id; persist(true); render(); });
    const pin = commandButton(workspace.right.pinnedDocId ? 'Unpin' : 'Pin', () => { workspace.right.pinnedDocId = workspace.right.pinnedDocId ? null : activeLeaf(workspace)?.docId || null; persist(true); render(); }, { 'aria-pressed':String(!!workspace.right.pinnedDocId) });
    const body = h('div', { class:'copal-inspector-body' });
    if (!doc) body.append(h('p', { class:'copal-empty-inline', text:'No active document.' }));
    else if (doc.virtual) body.append(h('p', { class:'copal-empty-inline', text:'Timeline is a canonical database view. Select a note to inspect properties, links, or outline.' }));
    else if (workspace.right.tab === 'properties') body.append(propertiesPane(doc));
    else if (workspace.right.tab === 'links') body.append(linksPane(doc));
    else body.append(outlinePane(doc, workspace));
    aside.append(h('header', { class:'copal-shell-side-header right' }, tabs, pin,
      ...(shellState.compact ? [] : [shellState.controls.right])), body, resizeHandle('right', workspace));
    return aside;
  }

  function ribbon(workspace) {
    return h('nav', { class:'copal-notes-ribbon', 'aria-label':'Notes actions' },
      h('button', { text:'＋', title:'New note', 'aria-label':'New note', onclick:() => createNew() }),
      h('button', { text:'↔', title:'Open Timeline', 'aria-label':'Open Timeline', onclick:() => open(TIMELINE_DOCUMENT.id) }),
      h('button', { text:'⌕', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => showChooser() }),
      h('button', { text:'⌘', title:'Command palette', 'aria-label':'Command palette', onclick:showCommands }));
  }

  function render() {
    const started = performance.now();
    activateNotes?.();
    const current = context();
    const workspace = ensureWorkspace();
    if (!current || !workspace || !current.window.body) return;
    const docs = documents();
    const workspaceDocs = workspaceDocuments();
    const leaf = activeLeaf(workspace);
    const doc = leaf ? workspaceDocs.find((item) => item.id === leaf.docId) || null : null;
    current.selected = doc?.id || null;
    state.selected = current.selected;
    persistActiveContext();
    bindKeys(workspace, doc);
    if (!current.notePageHideHandler) {
      current.notePageHideHandler = () => { persist(true); void flushAll(); };
      window.addEventListener('pagehide', current.notePageHideHandler);
    }
    if (!current.noteShellMedia) {
      current.noteShellMedia = [NOTES_NARROW_QUERY, NOTES_COMPACT_QUERY].map((value) => {
        const query = window.matchMedia(value);
        const handler = () => { current.noteDrawer = null; render(); };
        query.addEventListener('change', handler);
        return { query, handler };
      });
    }

    const viewport = shellViewport();
    current.noteDrawer ||= null;
    if (!viewport.compact) current.noteDrawer = null;
    if (!viewport.narrow && current.noteDrawer === 'left') current.noteDrawer = null;
    if (current.noteDrawer && !current.noteDrawerRelease) {
      current.noteDrawerRelease = registerMenuDismiss(() => {
        current.noteDrawerRelease = null;
        current.noteDrawer = null;
        render();
      });
    } else if (!current.noteDrawer && current.noteDrawerRelease) {
      current.noteDrawerRelease();
      current.noteDrawerRelease = null;
    }
    const controls = ensureShellControls();
    for (const button of Object.values(controls)) {
      for (const animation of button.getAnimations()) animation.cancel();
      button.style.pointerEvents = '';
    }
    const before = Object.fromEntries(Object.entries(controls).map(([side, button]) => [side, button.isConnected ? button.getBoundingClientRect() : null]));
    const focusedSide = document.activeElement === controls.left ? 'left' : document.activeElement === controls.right ? 'right' : null;
    const leftVisible = viewport.narrow ? current.noteDrawer === 'left' : workspace.left.open;
    const rightVisible = viewport.compact ? current.noteDrawer === 'right' : workspace.right.open;
    const controlGroupId = groupForLeaf(workspace, workspace.activeLeafId)?.id || workspaceGroups(workspace)[0]?.id || null;
    const shellState = { ...viewport, controls, controlGroupId };
    syncShellControl(controls.left, 'left', leftVisible, !viewport.narrow && leftVisible ? 'sidebar' : 'tabs');
    syncShellControl(controls.right, 'right', rightVisible, !viewport.compact && rightVisible ? 'sidebar' : 'tabs');

    const shell = h('div', {
      class:`copal-notes-workspace${leftVisible ? '' : ' left-closed'}${rightVisible ? '' : ' right-closed'}${workspace.settings.ribbon ? ' ribbon-open' : ''}${viewport.narrow ? ' narrow' : viewport.compact ? ' compact' : ''}`,
      style:`--copal-left-width:${workspace.left.width}px;--copal-right-width:${workspace.right.width}px`,
      'data-preview-layout':workspace.settings.previewLayout,
      'data-editor-constructions':current.noteMetrics.editorConstructions,
      'data-drawer':current.noteDrawer || 'none',
    });
    if (workspace.settings.ribbon) shell.append(ribbon(workspace));
    if (leftVisible) shell.append(leftSidebar(workspace, docs, shellState));
    const main = h('main', { class:'copal-notes-main' }, renderNode(workspace.root, workspace, workspaceDocs, shellState));
    shell.append(main);
    if (rightVisible) shell.append(rightSidebar(workspace, shellState));
    if (current.noteDrawer) shell.append(h('button', { class:'copal-shell-scrim', type:'button', 'aria-label':'Close Notes sidebar', onclick:() => { current.noteDrawer = null; render(); } }));

    const validLeaves = new Set(workspaceLeaves(workspace).map((item) => item.id));
    for (const leafId of [...current.noteLeafViews.keys()]) if (!validLeaves.has(leafId)) disposeLeaf(leafId);
    const previousFocus = current.window.body.contains(document.activeElement) ? document.activeElement : null;
    current.window.body.replaceChildren(shell);
    if (previousFocus?.isConnected) {
      const editorHost = previousFocus.closest?.('.cm-editor');
      const editorCache = editorHost
        ? [...current.noteLeafViews.values()].find((cache) => cache.editor && cache.root.contains(editorHost))
        : null;
      // A reattached CodeMirror surface must be refocused through the view so
      // its input binding (EditContext) re-attaches; a raw element focus on an
      // already-active node is a no-op and leaves typing dead until blur.
      if (editorCache) { previousFocus.blur?.(); editorCache.editor.focus(); }
      else previousFocus.focus({ preventScroll:true });
    }
    finishShellControlMove(controls, before, focusedSide);
    current.noteMetrics.renders += 1;
    current.noteMetrics.lastRenderMs = performance.now() - started;
    shell.dataset.renderMs = current.noteMetrics.lastRenderMs.toFixed(2);
    shell.dataset.editorConstructions = String(current.noteMetrics.editorConstructions);
    persist();
  }

  function loadSaved(current, saved) {
    current.noteSaved = saved && typeof saved === 'object' ? saved : {};
  }

  return {
    render, open, destroy, flushAll, loadSaved, showChooser, showCommands, showSearch, showSettings,
    acceptSavedDocument, getSettings, updateSettings,
    toggleLeft:() => toggleSidebar('left'),
    toggleRight:() => toggleSidebar('right'),
  };
}
