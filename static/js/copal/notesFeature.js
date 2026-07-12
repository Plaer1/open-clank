import {
  activateWorkspaceLeaf,
  closeWorkspaceGroup,
  closeWorkspaceLeaf,
  closeWorkspaceOtherLeaves,
  findWorkspaceGroup,
  findWorkspaceLeaf,
  groupForLeaf,
  moveWorkspaceLeaf,
  normalizeNotesWorkspace,
  noteViewType,
  openWorkspaceDocument,
  resizeWorkspaceSplit,
  serializeNotesWorkspace,
  setWorkspaceLeafMode,
  splitWorkspaceGroup,
  workspaceGroups,
  workspaceLeaves,
} from './notesWorkspace.js';
import {
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

const OPERATIONAL_KINDS = new Set(['planning', 'calendar-projection', 'treehouse-state', 'copal-tracks', 'copal-migration']);
const PROPERTY_TYPES = ['text', 'list', 'number', 'checkbox', 'date', 'datetime', 'tags'];

export function createNotesFeature({
  h, api, state, createMarkdownEditor, renderMarkdown, formatBaseCell,
  saveDocument, renameNote, deleteDocument, showHistory, showTrash, showForm,
  importVault, loadDocuments, openDocument:openOtherView, persistActiveContext, deleteDocuments,
  activateNotes,
}) {
  let persistTimer = null;

  function context() {
    return state.windows.get('notes');
  }

  function documents() {
    return state.docs.filter((doc) => !OPERATIONAL_KINDS.has(doc.kind));
  }

  function ensureWorkspace() {
    const current = context();
    if (!current) return null;
    const docs = documents();
    const signature = docs.map((doc) => doc.id).sort().join(':');
    const currentLeaf = current.noteWorkspace ? findWorkspaceLeaf(current.noteWorkspace) : null;
    const requested = current.selected || state.selected;
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
    return leaf ? state.docs.find((doc) => doc.id === leaf.docId) || null : null;
  }

  function inspectorDoc(workspace) {
    const id = workspace.right.pinnedDocId || activeLeaf(workspace)?.docId;
    return state.docs.find((doc) => doc.id === id) || null;
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
    const doc = state.docs.find((item) => item.id === id);
    const workspace = ensureWorkspace();
    if (!doc || !workspace) return null;
    const leaf = openWorkspaceDocument(workspace, doc, options);
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
      if (cache.reading?.isConnected) cache.reading.replaceChildren(renderMarkdown(value, new Set([docId])));
      if (cache.preview && !cache.preview.hidden) cache.preview.replaceChildren(renderMarkdown(value, new Set([docId])));
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
    if (current) current.notePageHideHandler = null;
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

  function closeDialogOnEscape(dialog) {
    dialog.addEventListener('cancel', (event) => { event.preventDefault(); dialog.close(); });
    dialog.addEventListener('close', () => dialog.remove());
  }

  function showChooser({ title = 'Quick switcher', docs = documents(), choose = null, allowCreate = true } = {}) {
    const previous = document.activeElement;
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
        const name = /\.[A-Za-z0-9]+$/.test(query) ? query : `${query}.md`;
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
    document.body.append(dialog); closeDialogOnEscape(dialog);
    dialog.addEventListener('close', () => previous?.focus?.(), { once:true });
    draw(); dialog.showModal(); search.focus();
  }

  function createNew(initial = 'Untitled.md') {
    showForm('New Copal note', [['name', 'Path', initial], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
      const result = await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'markdown', content }) });
      await loadDocuments(false);
      open(result.doc.id, { reuse:false });
    });
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

  function setPreviewLayout(layout) {
    const workspace = ensureWorkspace();
    workspace.settings.previewLayout = layout === 'side-by-side' ? 'side-by-side' : 'inline';
    persist(true); render();
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
    dialog.append(
      h('label', {}, h('span', { text:'Preview layout' }), layout),
      h('label', { class:'copal-check' }, lineNumbers, h('span', { text:'Show line numbers' })),
      h('label', { class:'copal-check' }, readable, h('span', { text:'Use readable line width' })),
      h('label', { class:'copal-check' }, ribbon, h('span', { text:'Show optional Notes ribbon' })),
      h('p', { class:'copal-dialog-hint', text:'Document mode and preview layout are independent. Side-by-side is preserved but never the clean-profile default.' }),
      h('div', { class:'copal-dialog-actions' }, commandButton('Cancel', () => dialog.close()), commandButton('Save', () => {
        workspace.settings.previewLayout = layout.value === 'side-by-side' ? 'side-by-side' : 'inline';
        workspace.settings.lineNumbers = lineNumbers.checked;
        workspace.settings.readableLineWidth = readable.checked;
        workspace.settings.ribbon = ribbon.checked;
        persist(true); dialog.close(); render();
      }, { class:'copal-btn primary' })),
    );
    document.body.append(dialog); closeDialogOnEscape(dialog); dialog.showModal(); layout.focus();
  }

  function showCommands() {
    const workspace = ensureWorkspace();
    const doc = activeDoc(workspace);
    const actions = [
      ['New note', 'Ctrl+N', () => createNew()],
      ['Quick switcher', 'Ctrl+O', () => showChooser()],
      ['Search notes', 'Ctrl+Shift+F', () => showSearch()],
      ['Notes settings', '', showSettings],
      ['Live Preview mode', '', () => setMode('live')],
      ['Source mode', '', () => setMode('source')],
      ['Reading mode', '', () => setMode('reading')],
      ['Inline preview layout', '', () => setPreviewLayout('inline')],
      ['Side-by-side preview layout', '', () => setPreviewLayout('side-by-side')],
      ['Split right', '', () => splitActive('horizontal')],
      ['Split below', '', () => splitActive('vertical')],
      ['Toggle file sidebar', '', () => { workspace.left.open = !workspace.left.open; persist(true); render(); }],
      ['Toggle linked sidebar', '', () => { workspace.right.open = !workspace.right.open; persist(true); render(); }],
      ['Reopen closed note', '', reopenClosed],
      ['History', '', () => doc && showHistory(doc)],
      ['Trash', '', () => showTrash()],
      ['Import Obsidian vault', '', importVault],
      ['Export for Obsidian', '', () => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; }],
    ];
    const previous = document.activeElement;
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
    dialog.append(search, list); document.body.append(dialog); closeDialogOnEscape(dialog);
    dialog.addEventListener('close', () => previous?.focus?.(), { once:true });
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
    const id = workspace.closed.shift();
    const doc = state.docs.find((item) => item.id === id);
    if (doc) open(doc.id, { reuse:false });
    else persist(true);
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
        const openButton = h('button', { class:'copal-file-row', role:'treeitem', 'aria-selected':String(chosen || selected), draggable:'true', title:doc.name, onclick:(event) => {
          if (event.ctrlKey || event.metaKey) {
            chosen ? selectedIds.delete(doc.id) : selectedIds.add(doc.id);
            workspace.left.selected = [...selectedIds]; persist(true); render(); return;
          }
          workspace.left.selected = []; open(doc.id);
        } },
          h('span', { class:'copal-file-kind', text:fileGlyph(doc) }), h('span', { text:displayName(doc) }));
        openButton.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-document', doc.id));
        const menu = h('details', { class:'copal-file-menu' }, h('summary', { title:`Actions for ${doc.name}`, 'aria-label':`Actions for ${doc.name}`, text:'⋯' }),
          h('div', { class:'copal-popover-menu' }, commandButton('Rename or move', () => renameWithForm(doc)), commandButton('Open in new tab', () => open(doc.id, { reuse:false })), commandButton('Reveal path', () => { revealInExplorer(doc, workspace); persist(true); render(); }), commandButton('Trash', () => deleteDocument(doc), { class:'copal-btn danger' })));
        row.append(openButton, menu); parent.append(row);
      }
    };
    const tree = h('div', { class:'copal-file-tree', role:'tree', 'aria-label':'Copal files', tabindex:'-1' });
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
    return ({ markdown:'◇', canvas:'⌘', base:'▦', image:'▧', audio:'♪', video:'▶', pdf:'▤', asset:'·' })[noteViewType(doc)] || '◇';
  }

  function displayName(doc) {
    const name = String(doc?.name || '').split('/').pop();
    return noteViewType(doc) === 'markdown' ? name.replace(/\.md$/i, '') : name;
  }

  function leftSidebar(workspace, docs) {
    const aside = h('aside', { class:'copal-notes-explorer', style:`--copal-left-width:${workspace.left.width}px` });
    const tabs = h('div', { class:'copal-side-tabs', role:'tablist', 'aria-label':'Notes navigation' });
    for (const [key, label] of [['files','Files'],['search','Search'],['bookmarks','Bookmarks'],['recent','Recent']]) {
      tabs.append(h('button', { class:workspace.left.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.left.tab === key), text:label, onclick:() => { workspace.left.tab = key; persist(true); render(); } }));
    }
    const body = h('div', { class:'copal-side-body' });
    if (workspace.left.tab === 'files') {
      const sort = h('select', { class:'copal-file-sort', 'aria-label':'Sort files' }, h('option', { value:'name', text:'Name' }), h('option', { value:'modified', text:'Modified' }));
      sort.value = workspace.left.sort;
      sort.addEventListener('change', () => { workspace.left.sort = sort.value === 'modified' ? 'modified' : 'name'; persist(true); render(); });
      const allFolders = [...new Set(docs.flatMap((doc) => {
        const parts = doc.name.split('/').slice(0, -1); return parts.map((_, index) => parts.slice(0, index + 1).join('/'));
      }))];
      const expand = () => { workspace.left.expanded = workspace.left.expanded.length === allFolders.length ? [] : allFolders; persist(true); render(); };
      body.append(h('header', {}, h('strong', { text:'Files' }), sort,
        commandButton(workspace.left.expanded.length === allFolders.length ? '−' : '+', expand, { title:'Expand or collapse all folders', 'aria-label':'Expand or collapse all folders' }),
        commandButton('＋', () => createNew(), { title:'New note', 'aria-label':'New note' }),
        commandButton('▱+', () => createNew('New folder/Untitled.md'), { title:'New folder with note', 'aria-label':'New folder with note' })));
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
      body.append(fileTree(docs, workspace));
    } else if (workspace.left.tab === 'search') {
      const input = h('input', { class:'copal-note-search-input', type:'search', placeholder:'Search names and contents…', 'aria-label':'Search notes', value:context().noteSearch || '' });
      const results = h('div', { class:'copal-search-results' });
      const draw = () => {
        context().noteSearch = input.value;
        const query = input.value.trim().toLowerCase();
        const matches = query ? docs.map((doc) => {
          const source = String(doc.text || ''); const lower = source.toLowerCase(); const at = lower.indexOf(query);
          const nameScore = fuzzyScore(doc.name, query); const contentScore = at >= 0 ? 500 - Math.min(400, at) : -1;
          const start = Math.max(0, at - 55); const snippet = at >= 0 ? source.slice(start, at + query.length + 85).replace(/\s+/g, ' ').trim() : '';
          return { doc, score:Math.max(nameScore, contentScore), snippet };
        }).filter((item) => item.score >= 0).sort((a, b) => b.score - a.score || a.doc.name.localeCompare(b.doc.name)).slice(0, 100) : [];
        results.replaceChildren(...matches.map(({ doc, snippet }) => h('button', { class:'copal-doc-row', onclick:() => open(doc.id) }, h('strong', { text:doc.name }), snippet ? h('small', { text:snippet }) : null)));
        if (query && !matches.length) results.append(h('p', { class:'copal-empty-inline', text:'No matches' }));
      };
      input.addEventListener('input', draw); body.append(input, results); draw();
    } else {
      const ids = workspace.left.tab === 'bookmarks'
        ? workspaceLeaves(workspace).filter((leaf) => leaf.pinned).map((leaf) => leaf.docId)
        : workspace.recent;
      const unique = [...new Set(ids)].map((id) => state.docs.find((doc) => doc.id === id)).filter(Boolean);
      body.append(h('header', {}, h('strong', { text:workspace.left.tab === 'bookmarks' ? 'Bookmarks' : 'Recent notes' })));
      for (const doc of unique) body.append(h('button', { class:'copal-doc-row', text:doc.name, onclick:() => open(doc.id) }));
      if (!unique.length) body.append(h('p', { class:'copal-empty-inline', text:workspace.left.tab === 'bookmarks' ? 'Pin a tab to bookmark it.' : 'No recent notes.' }));
    }
    const handle = resizeHandle('left', workspace);
    aside.append(tabs, body, handle);
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

  function groupTabs(group, workspace, docs) {
    const bar = h('div', { class:'copal-note-tabs', role:'tablist' });
    for (const leaf of group.tabs) {
      const doc = docs.find((item) => item.id === leaf.docId); if (!doc) continue;
      const tab = h('div', { class:`copal-note-tab${leaf.id === group.activeLeafId ? ' active' : ''}${leaf.pinned ? ' pinned' : ''}`, role:'tab', 'aria-selected':String(leaf.id === group.activeLeafId), draggable:'true', 'data-leaf-id':leaf.id },
        h('button', { class:'copal-note-tab-label', text:displayName(doc), onclick:() => setActive(group.id, leaf.id) }),
        h('button', { class:'copal-note-tab-pin', text:leaf.pinned ? '●' : '○', title:leaf.pinned ? 'Unpin tab' : 'Pin tab', 'aria-pressed':String(leaf.pinned), onclick:() => { leaf.pinned = !leaf.pinned; persist(true); render(); } }),
        h('button', { class:'copal-note-tab-close', text:'×', disabled:leaf.pinned, 'aria-label':`Close ${doc.name}`, onclick:async () => {
          if (!await saveDraft(doc.id)) return;
          const closed = closeWorkspaceLeaf(workspace, leaf.id); if (closed) disposeLeaf(leaf.id); persist(true); render();
        } }));
      tab.addEventListener('auxclick', async (event) => {
        if (event.button !== 1 || leaf.pinned) return;
        event.preventDefault(); if (!await saveDraft(doc.id)) return;
        if (closeWorkspaceLeaf(workspace, leaf.id)) disposeLeaf(leaf.id); persist(true); render();
      });
      tab.addEventListener('dragstart', (event) => event.dataTransfer.setData('text/x-copal-note-leaf', leaf.id));
      tab.addEventListener('dragover', (event) => event.preventDefault());
      tab.addEventListener('drop', (event) => {
        event.preventDefault(); const source = event.dataTransfer.getData('text/x-copal-note-leaf');
        const index = group.tabs.findIndex((item) => item.id === leaf.id);
        if (source && moveWorkspaceLeaf(workspace, source, group.id, index)) { persist(true); render(); }
      });
      bar.append(tab);
    }
    const groupMenu = h('details', { class:'copal-leaf-menu copal-group-menu' }, h('summary', { text:'⋯', title:'Tab group actions', 'aria-label':'Tab group actions' }), h('div', { class:'copal-popover-menu' },
      commandButton('Close other tabs', async () => {
        const activeId = group.activeLeafId; const candidates = group.tabs.filter((leaf) => leaf.id !== activeId && !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceOtherLeaves(workspace, activeId)) disposeLeaf(closed.id);
        persist(true); render();
      }),
      commandButton('Close tab group', async () => {
        const candidates = group.tabs.filter((leaf) => !leaf.pinned);
        const results = await Promise.all(candidates.map((leaf) => saveDraft(leaf.docId)));
        if (results.some((value) => !value)) return;
        for (const closed of closeWorkspaceGroup(workspace, group.id)) disposeLeaf(closed.id);
        persist(true); render();
      })));
    const controls = h('div', { class:'copal-tab-group-controls' },
      commandButton('+', () => showChooser({ title:'Open note in this group', choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:group.id, reuse:false }); persist(true); render(); } }), { title:'Open note', 'aria-label':'Open note' }),
      commandButton('↔', () => showChooser({ title:'Split right', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'horizontal'); persist(true); render(); }, allowCreate:false }), { title:'Split right', 'aria-label':'Split right' }),
      commandButton('↕', () => showChooser({ title:'Split below', choose:(doc) => { splitWorkspaceGroup(workspace, group.id, doc, 'vertical'); persist(true); render(); }, allowCreate:false }), { title:'Split below', 'aria-label':'Split below' }),
      groupMenu);
    bar.append(controls); return bar;
  }

  function renderNode(node, workspace, docs) {
    if (node.type === 'group') {
      const group = h('section', { class:`copal-note-group${node.tabs.some((leaf) => leaf.id === workspace.activeLeafId) ? ' active-group' : ''}`, 'data-group-id':node.id });
      const active = node.tabs.find((leaf) => leaf.id === node.activeLeafId) || node.tabs[0] || null;
      if (active && node.activeLeafId !== active.id) node.activeLeafId = active.id;
      group.append(groupTabs(node, workspace, docs));
      const body = h('div', { class:'copal-note-group-body' });
      if (active) {
        const doc = docs.find((item) => item.id === active.docId);
        if (doc) body.append(renderLeaf(active, doc, workspace, node));
      } else body.append(h('div', { class:'copal-empty' }, h('p', { text:'This tab group is empty.' }), commandButton('Open note', () => showChooser({ choose:(doc) => { openWorkspaceDocument(workspace, doc, { groupId:node.id }); persist(true); render(); } }))));
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
      const wrap = h('div', { class:'copal-note-split-child', style:`flex-basis:${node.sizes[index] || 50}%` }, renderNode(child, workspace, docs));
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
    const menu = h('details', { class:'copal-leaf-menu' }, h('summary', { text:'⋯', title:'Note actions', 'aria-label':'Note actions' }), h('div', { class:'copal-popover-menu' },
      commandButton('Live Preview', () => { setWorkspaceLeafMode(workspace, leaf.id, 'live'); persist(true); render(); }),
      commandButton('Source mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'source'); persist(true); render(); }),
      commandButton('Reading mode', () => { setWorkspaceLeafMode(workspace, leaf.id, 'reading'); persist(true); render(); }),
      commandButton(workspace.settings.previewLayout === 'inline' ? 'Use side-by-side preview' : 'Use inline preview', () => setPreviewLayout(workspace.settings.previewLayout === 'inline' ? 'side-by-side' : 'inline')),
      commandButton('Find and replace', () => cache.editor && showFindReplace(cache.editor)),
      commandButton('History', () => showHistory(doc)),
      commandButton('Split right', () => showChooser({ title:'Split right', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'horizontal'); persist(true); render(); }, allowCreate:false })),
      commandButton('Split below', () => showChooser({ title:'Split below', choose:(item) => { splitWorkspaceGroup(workspace, group.id, item, 'vertical'); persist(true); render(); }, allowCreate:false })),
      commandButton('Move tab left', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index > 0 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index - 1)) { persist(true); render(); } }),
      commandButton('Move tab right', () => { const index = group.tabs.findIndex((item) => item.id === leaf.id); if (index >= 0 && index < group.tabs.length - 1 && moveWorkspaceLeaf(workspace, leaf.id, group.id, index + 1)) { persist(true); render(); } }),
      commandButton('Reveal in Files', () => { workspace.left.open = true; workspace.left.tab = 'files'; revealInExplorer(doc, workspace); persist(true); render(); }),
      ...(leaf.view === 'canvas' || leaf.view === 'base' ? [commandButton(leaf.rawSource ? 'Back to typed view' : 'View raw source', () => { leaf.rawSource = !leaf.rawSource; persist(true); render(); })] : []),
      commandButton('Rename', () => renameWithForm(doc)),
      commandButton('Move to trash', () => deleteDocument(doc), { class:'copal-btn danger' })));
    cache.header.replaceChildren(breadcrumb, title, h('span', { class:'copal-leaf-mode', text:leaf.mode === 'live' ? 'Live Preview' : leaf.mode === 'source' ? 'Source' : 'Reading' }), menu);
  }

  function renderLeaf(leaf, doc, workspace, group) {
    const current = context();
    let cache = current.noteLeafViews.get(leaf.id);
    if (!cache || cache.docId !== doc.id || cache.view !== leaf.view) {
      if (cache) disposeLeaf(leaf.id);
      const root = h('article', { class:'copal-note-leaf', 'data-leaf-id':leaf.id, 'data-view-type':leaf.view });
      const header = h('header', { class:'copal-note-view-header' });
      const body = h('div', { class:'copal-note-leaf-content' });
      const status = h('footer', { class:'copal-note-status', role:'status', 'aria-live':'polite' });
      root.append(header, body, status);
      cache = { root, header, body, status, docId:doc.id, view:leaf.view, saveState:'saved', cursorLine:1, editor:null };
      current.noteLeafViews.set(leaf.id, cache);
    }
    cache.leaf = leaf; cache.doc = doc;
    leafHeader(cache, leaf, doc, workspace, group);
    if (leaf.view === 'markdown') updateMarkdownLeaf(cache, leaf, doc, workspace);
    else if (leaf.rawSource) updateSourceLeaf(cache, leaf, doc, workspace);
    else if (leaf.view === 'canvas') updateCanvasLeaf(cache, doc);
    else if (leaf.view === 'base') updateBaseLeaf(cache, doc);
    else updateAssetLeaf(cache, doc, leaf.view);
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
  }

  function updateMarkdownLeaf(cache, leaf, doc, workspace) {
    const editorWrap = ensureEditor(cache, leaf, doc, workspace);
    cache.root.dataset.mode = leaf.mode;
    cache.root.dataset.previewLayout = workspace.settings.previewLayout;
    if (leaf.mode === 'reading') {
      cache.reading ||= h('article', { class:'copal-note-reading' });
      cache.reading.replaceChildren(renderMarkdown(sourceValue(doc), new Set([doc.id])));
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
    const mode = cache.leaf?.mode === 'live' && ensureWorkspace().settings.previewLayout === 'side-by-side' ? 'Live · side-by-side' : cache.leaf?.mode === 'live' ? 'Live Preview' : cache.leaf?.mode === 'source' ? 'Source' : cache.leaf?.mode === 'reading' ? 'Reading' : cache.view;
    cache.status.replaceChildren(
      h('span', { class:`copal-save-state ${cache.saveState}`, text:cache.saveState === 'saved' ? 'Saved' : cache.saveState === 'saving' ? 'Saving…' : cache.saveState === 'conflict' ? 'Conflict' : 'Unsaved' }),
      h('span', { text:`${wordCount(value)} words` }), h('span', { text:`${value.length} characters` }),
      ...(cache.cursorLine ? [h('span', { text:`Ln ${cache.cursorLine}` })] : []), ...(selected ? [h('span', { text:`${selected} selected` })] : []), h('span', { text:`${mode} · ${cache.doc.kind} · Redb` }));
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
    document.body.append(dialog); closeDialogOnEscape(dialog); dialog.showModal(); find.focus();
  }

  function propertyInput(entry, type) {
    if (type === 'checkbox') { const input = h('input', { type:'checkbox' }); input.checked = entry.value === true; return input; }
    if (type === 'list' || type === 'tags') return h('input', { type:'text', value:Array.isArray(entry.value) ? entry.value.join(', ') : String(entry.value || '') });
    return h('input', { type:type === 'datetime' ? 'datetime-local' : type, value:entry.value == null ? '' : String(entry.value) });
  }

  function propertiesPane(doc) {
    const pane = h('div', { class:'copal-properties-pane' });
    const parsed = parseFrontmatter(sourceValue(doc));
    if (!parsed.valid) return h('div', { class:'copal-inspector-error' }, h('strong', { text:'Properties unavailable' }), h('p', { text:parsed.error }), h('p', { text:'Source is preserved. Repair the opening/closing --- markers in Source mode.' }));
    for (const entry of parsed.entries) {
      const type = propertyType(entry.value, entry.key);
      const keyInput = h('input', { class:'copal-property-key', value:entry.key, 'aria-label':`Property name ${entry.key}` });
      keyInput.addEventListener('change', () => {
        try { applyDocumentSource(doc, renameFrontmatterProperty(sourceValue(doc), entry.key, keyInput.value.trim())); render(); }
        catch (error) { keyInput.value = entry.key; context().window.setStatus(error.message, true); }
      });
      const row = h('div', { class:'copal-property-editor' }, keyInput);
      if (type === 'source') {
        row.append(h('span', { text:'Complex value—edit in Source mode.' }));
      } else {
        const select = h('select', { 'aria-label':`Type for ${entry.key}` }, PROPERTY_TYPES.map((item) => h('option', { value:item, text:item })));
        select.value = type;
        let input = propertyInput(entry, type);
        const commit = async () => {
          const nextType = select.value;
          const value = nextType === 'checkbox' ? input.checked : input.value;
          const content = setFrontmatterProperty(sourceValue(doc), entry.key, value, nextType);
          applyDocumentSource(doc, content); render();
        };
        input.setAttribute('aria-label', `Value for ${entry.key}`); input.addEventListener('change', commit);
        select.addEventListener('change', () => { const replacement = propertyInput({ ...entry, value:input.type === 'checkbox' ? input.checked : input.value }, select.value); replacement.setAttribute('aria-label', `Value for ${entry.key}`); replacement.addEventListener('change', commit); input.replaceWith(replacement); input = replacement; });
        row.append(select, input);
      }
      row.append(commandButton('↑', () => { applyDocumentSource(doc, moveFrontmatterProperty(sourceValue(doc), entry.key, -1)); render(); }, { title:`Move ${entry.key} up`, 'aria-label':`Move ${entry.key} up` }));
      row.append(commandButton('↓', () => { applyDocumentSource(doc, moveFrontmatterProperty(sourceValue(doc), entry.key, 1)); render(); }, { title:`Move ${entry.key} down`, 'aria-label':`Move ${entry.key} down` }));
      row.append(commandButton('×', () => {
        const content = removeFrontmatterProperty(sourceValue(doc), entry.key);
        applyDocumentSource(doc, content); render();
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
        const content = setFrontmatterProperty(sourceValue(doc), key.value.trim(), value.value, type.value);
        applyDocumentSource(doc, content); render();
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
    for (const name of [...(doc.links || [])].sort()) {
      const target = resolveDocumentLink(state.docs, name);
      outgoing.append(h('button', { class:'copal-doc-row copal-link-result', 'data-sort-name':target ? displayName(target) : name, 'data-sort-path':target?.name || name, disabled:!target, onclick:() => target && open(target.id) }, h('strong', { text:target ? name : `${name} · unresolved` }), target ? h('small', { text:target.name }) : null));
    }
    if (!(doc.links || []).length) outgoing.append(h('p', { class:'copal-empty-inline', text:'No outgoing links.' }));
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

  function rightSidebar(workspace) {
    const doc = inspectorDoc(workspace);
    const aside = h('aside', { class:'copal-notes-sidebar' });
    const tabs = h('div', { class:'copal-inspector-tabs', role:'tablist' });
    for (const [key, label] of [['properties','Properties'],['links','Links'],['outline','Outline']]) tabs.append(h('button', { class:workspace.right.tab === key ? 'active' : '', role:'tab', 'aria-selected':String(workspace.right.tab === key), text:label, onclick:() => { workspace.right.tab = key; persist(true); render(); } }));
    const pin = commandButton(workspace.right.pinnedDocId ? 'Unpin' : 'Pin', () => { workspace.right.pinnedDocId = workspace.right.pinnedDocId ? null : activeLeaf(workspace)?.docId || null; persist(true); render(); }, { 'aria-pressed':String(!!workspace.right.pinnedDocId) });
    const body = h('div', { class:'copal-inspector-body' });
    if (!doc) body.append(h('p', { class:'copal-empty-inline', text:'No active document.' }));
    else if (workspace.right.tab === 'properties') body.append(propertiesPane(doc));
    else if (workspace.right.tab === 'links') body.append(linksPane(doc));
    else body.append(outlinePane(doc, workspace));
    aside.append(h('header', {}, tabs, pin), body, resizeHandle('right', workspace));
    return aside;
  }

  function ribbon(workspace) {
    return h('nav', { class:'copal-notes-ribbon', 'aria-label':'Notes actions' },
      h('button', { text:'☰', title:'Toggle files', 'aria-label':'Toggle files', onclick:() => { workspace.left.open = !workspace.left.open; persist(true); render(); } }),
      h('button', { text:'＋', title:'New note', 'aria-label':'New note', onclick:() => createNew() }),
      h('button', { text:'⌕', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => showChooser() }),
      h('button', { text:'⌘', title:'Command palette', 'aria-label':'Command palette', onclick:showCommands }),
      h('button', { text:'◫', title:'Toggle linked views', 'aria-label':'Toggle linked views', onclick:() => { workspace.right.open = !workspace.right.open; persist(true); render(); } }));
  }

  function render() {
    const started = performance.now();
    activateNotes?.();
    const current = context();
    const workspace = ensureWorkspace();
    if (!current || !workspace || !current.window.body) return;
    const docs = documents();
    const leaf = activeLeaf(workspace);
    const doc = leaf ? docs.find((item) => item.id === leaf.docId) || null : null;
    current.selected = doc?.id || null;
    state.selected = current.selected;
    persistActiveContext();
    bindKeys(workspace, doc);
    if (!current.notePageHideHandler) {
      current.notePageHideHandler = () => { persist(true); void flushAll(); };
      window.addEventListener('pagehide', current.notePageHideHandler);
    }

    const shell = h('div', {
      class:`copal-notes-workspace${workspace.left.open ? '' : ' left-closed'}${workspace.right.open ? '' : ' right-closed'}${workspace.settings.ribbon ? ' ribbon-open' : ''}`,
      style:`--copal-left-width:${workspace.left.width}px;--copal-right-width:${workspace.right.width}px`,
      'data-preview-layout':workspace.settings.previewLayout,
      'data-editor-constructions':current.noteMetrics.editorConstructions,
    });
    if (workspace.settings.ribbon) shell.append(ribbon(workspace));
    if (workspace.left.open) shell.append(leftSidebar(workspace, docs));
    const main = h('main', { class:'copal-notes-main' }, renderNode(workspace.root, workspace, docs));
    shell.append(main);
    if (workspace.right.open) shell.append(rightSidebar(workspace));

    const validLeaves = new Set(workspaceLeaves(workspace).map((item) => item.id));
    for (const leafId of [...current.noteLeafViews.keys()]) if (!validLeaves.has(leafId)) disposeLeaf(leafId);
    current.window.body.replaceChildren(shell);
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
    acceptSavedDocument,
    toggleLeft:() => { const workspace = ensureWorkspace(); workspace.left.open = !workspace.left.open; persist(true); render(); },
    toggleRight:() => { const workspace = ensureWorkspace(); workspace.right.open = !workspace.right.open; persist(true); render(); },
  };
}
