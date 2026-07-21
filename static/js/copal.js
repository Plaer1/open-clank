import { openCalendar } from './calendar.js';
import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort, flattenFilterToLines, hasNestedFilterGroups, parseFilterLines, removeBaseView, reorderBaseView, makeViewFromTemplate, VIEW_TYPES, parseDataviewQuery, reorderBaseColumn } from './copal/bases.js';
import { createMarkdownEditor } from './copal/codemirror.js';
import { createNotesFeature } from './copal/notesFeature.js';
import { databaseRelations, moveHeadingSection, moveHeadingSectionTo, outlineEntries, reparentHeading } from './copal/notesModel.js';
import { createPlanningFeature } from './copal/planning.js';
import { createTreeHouseFeature } from './copal/treehouse.js';
import { createCopalWindow } from './copal/windows.js';
import { wireDialog } from './copal/overlays.js';

const VIEWS = ['notes', 'wiki', 'timeline', 'galaxy', 'graph', 'mind', 'bases', 'treehouse', 'todo'];
const LABELS = { notes: 'Notes', wiki: 'Wiki', timeline: 'Timeline', galaxy: 'Galaxy', graph: 'Graph', mind: 'Mind', bases: 'Bases', treehouse: 'TreeHouse', todo: 'Meatbag Tasks' };
const HIDDEN_KINDS = new Set(['asset', 'planning', 'calendar-projection', 'treehouse-state', 'copal-tracks', 'copal-migration']);

const state = {
  api: '', workspace: 'default', view: 'notes', docs: [], planning: { tracks: [], floatingTodos: [] }, selected: null,
  filter: '', story: [], pinned: new Set(), reading: false, saveTimers: new Map(),
  calendarMonth: null, windows: new Map(),
  root: null, content: null, body: null,
  wikiEditing: new Set(), treehouseSection: 'courses', treehouseLesson: null,
  title: null, status: null, search: null, events: null, reloadTimer: null, ignoreEventsUntil: 0,
  projectedPlanningHead: null,
  baseId: null, baseView: null, basePage: 1, basePageSize: 100, baseQueryToken: 0, baseDefinition: null,
  baseSourceDocs: new Map(), baseFocusRow: -1, baseFocusCol: -1, baseFocusTable: null,
  noteEditors: new Set(),
};
let planningFeature = null;
let notesFeature = null;

function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value === true) node.setAttribute(key, '');
    else if (value !== false && value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) {
    if (child == null) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function persistActiveContext() {
  const context = state.windows.get(state.view);
  if (!context) return;
  context.selected = state.selected;
  context.filter = state.filter;
  context.story = [...state.story];
  context.pinned = new Set(state.pinned);
  context.reading = state.reading;
}

function activateView(view) {
  if (state.view !== view) persistActiveContext();
  const context = state.windows.get(view);
  if (!context) return null;
  state.view = view;
  state.root = context.window.root;
  state.content = context.window.content;
  state.body = context.window.body;
  state.title = context.window.heading;
  state.status = context.window.status;
  state.search = context.search;
  state.selected = context.selected || null;
  state.filter = context.filter || '';
  state.story = [...(context.story || [])];
  state.pinned = new Set(context.pinned || []);
  state.reading = !!context.reading;
  return context;
}

async function api(path, options = {}) {
  const separator = path.includes('?') ? '&' : '?';
  const mutating = options.method && options.method !== 'GET';
  if (mutating) state.ignoreEventsUntil = Date.now() + 5000;
  const contentHeaders = options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' };
  const response = await fetch(`${state.api}/api/copal${path}${separator}workspace=${encodeURIComponent(state.workspace)}`, {
    ...options,
    headers: { ...contentHeaders, ...(options.headers || {}) },
  });
  if (response.ok) {
    if (mutating) {
      state.ignoreEventsUntil = Date.now() + 1000;
      clearTimeout(state.reloadTimer);
    }
    return response.json();
  }
  const error = await response.json().catch(() => ({}));
  const detail = error.detail;
  const message = typeof detail === 'string'
    ? detail
    : detail?.outcome === 'stale'
      ? 'A newer version exists.'
      : detail?.diagnostics?.[0]?.message || detail?.message || 'Copal operation failed.';
  const failure = new Error(message);
  failure.status = response.status;
  failure.detail = detail;
  throw failure;
}

function setStatus(message, bad = false) {
  const context = state.windows.get(state.view);
  if (context) context.window.setStatus(message, bad);
}

function normalizeName(name) {
  return String(name || '').replace(/\.md$/i, '').toLowerCase();
}

function findByName(name) {
  const wanted = normalizeName(name);
  return state.docs.find((doc) => normalizeName(doc.name) === wanted || normalizeName(doc.name.split('/').pop()) === wanted);
}

function planningData() {
  return state.planning || { tracks: [], floatingTodos: [] };
}

function visibleDocs(corpus) {
  const query = state.filter.trim().toLowerCase();
  return state.docs.filter((doc) => {
    if (HIDDEN_KINDS.has(doc.kind)) return false;
    if (corpus === 'wiki' && doc.kind !== 'wiki') return false;
    if (corpus === 'notes' && doc.kind === 'wiki') return false;
    return !query || doc.name.toLowerCase().includes(query) || String(doc.text || '').toLowerCase().includes(query);
  });
}

function projectionChanged(payload) {
  const projection = payload?.calendar_projection;
  if (!projection?.enabled || projection.ok === false) return;
  window.dispatchEvent(new CustomEvent('calendar-refresh'));
}

async function reconcileCalendarProjection(force = false) {
  const revision = [state.planning?.trackRegistry?.head, ...allPlanningTasks().map((event) => event.head)].filter(Boolean).sort().join(':');
  if (!force && revision && revision === state.projectedPlanningHead) return;
  const result = await api('/calendar/reconcile', {
    method: 'POST',
    body: JSON.stringify({}),
  });
  const projection = result.projections?.[0];
  if (projection?.enabled && projection.ok !== false) {
    state.projectedPlanningHead = revision || projection.sourceRevision || 'projected';
    window.dispatchEvent(new CustomEvent('calendar-refresh'));
  }
}

async function loadDocuments(render = true) {
  setStatus('Loading…');
  try {
    let [result, planning] = await Promise.all([api('/documents?hidden=include'), api('/planning')]);
    if (planning.migrationRequired) {
      setStatus('Migrating Timeline events into canonical Redb notes…');
      const migration = await api('/planning/migrate?dry_run=false', { method:'POST', body:JSON.stringify({ action:'apply' }) });
      projectionChanged(migration);
      [result, planning] = await Promise.all([api('/documents?hidden=include'), api('/planning')]);
    }
    state.docs = result.docs || [];
    state.planning = planning || { tracks: [], floatingTodos: [] };
    if (state.selected && !state.docs.some((doc) => doc.id === state.selected)) state.selected = null;
    if (render) {
      const active = state.view;
      for (const [view, context] of state.windows) if (context.window.visible) renderView(view);
      activateView(active);
    }
    for (const context of state.windows.values()) context.window.setStatus(`${state.docs.length} documents · canonical Redb`);
    reconcileCalendarProjection().catch(() => {});
  } catch (error) {
    setStatus(error.message, true);
    if (render && state.body) state.body.replaceChildren(h('div', { class: 'copal-empty', text: error.message }));
  }
}

function updateRoute(view, replace = false) {
  const selected = state.windows.get(view)?.selected || (state.view === view ? state.selected : null);
  const url = `/copal/${view}${selected ? `?doc=${encodeURIComponent(selected)}` : ''}`;
  history[replace ? 'replaceState' : 'pushState']({ copal: view }, '', url);
}

function markActive() {
  document.querySelectorAll('[data-copal-view]').forEach((link) => {
    const visible = state.windows.get(link.dataset.copalView)?.window.visible;
    link.classList.toggle('active', !!visible);
    if (link.dataset.copalView === state.view) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
}

async function open(view = 'notes', push = true) {
  if (view === 'calendar') {
    if (push || location.pathname.startsWith('/copal/calendar')) {
      history.replaceState({}, '', '/calendar');
    }
    openCalendar();
    return;
  }
  view = VIEWS.includes(view) ? view : 'notes';
  const context = ensureViewWindow(view);
  activateView(view);
  localStorage.setItem('odysseus-copal-view', view);
  context.window.show(document.activeElement);
  markActive();
  if (push) updateRoute(view);
  if (!state.docs.length) await loadDocuments();
  else { renderView(view); reconcileCalendarProjection().catch(() => {}); }
  context.window.focus();
}

function close(view = state.view, push = true, fromManager = false) {
  clearTimeout(state.saveTimer);
  const context = state.windows.get(view);
  if (context) context.window.requestClose(fromManager);
  markActive();
  if (push) history.pushState({}, '', '/');
}

function openDocument(id, view = state.view, push = true) {
  const context = ensureViewWindow(view);
  if (view === 'notes' && notesFeature) {
    activateView(view);
    context.window.show(document.activeElement);
    notesFeature.open(id);
    if (push) updateRoute(view);
    context.window.focus();
    return;
  }
  context.selected = id;
  if (view === 'wiki' && !context.story.includes(id)) context.story.unshift(id);
  activateView(view);
  context.window.show(document.activeElement);
  state.selected = id;
  if (view === 'wiki' && !state.story.includes(id)) state.story.unshift(id);
  persistActiveContext();
  if (push) updateRoute(view);
  renderView(view);
}

async function refreshIndexedDocument(id) {
  const fresh = await api(`/documents/${encodeURIComponent(id)}`);
  const index = state.docs.findIndex((doc) => doc.id === id);
  if (index >= 0) state.docs[index] = fresh;
  else state.docs.push(fresh);
  return fresh;
}

function setViewStatus(view, message, bad = false) {
  state.windows.get(view)?.window.setStatus(message, bad);
}

function showDocumentConflict(doc, localContent, remote, view) {
  document.querySelector(`#copal-conflict-${CSS.escape(doc.id)}`)?.close();
  const dialog = h('dialog', { id:`copal-conflict-${doc.id}`, class:'copal-dialog copal-conflict-dialog' },
    h('h2', { text:`Resolve conflict · ${doc.name}` }),
    h('p', { text:'Another window saved this note first. Nothing was overwritten; compare both versions and choose explicitly.' }),
    h('p', { class:'copal-dialog-hint', text:`Your base: ${doc.head || 'unknown'} · latest: ${remote.head || 'unknown'}` }));
  const comparison = h('div', { class:'copal-conflict-comparison' },
    h('section', {}, h('h3', { text:'Your unsaved version' }), h('pre', { text:localContent })),
    h('section', {}, h('h3', { text:'Latest saved version' }), h('pre', { text:String(remote.text || '') })));
  const close = () => dialog.close();
  dialog.append(comparison, h('div', { class:'copal-dialog-actions' },
    h('button', { class:'copal-btn', text:'Keep editing mine', onclick:close }),
    h('button', { class:'copal-btn', text:'Copy mine', onclick:async() => { await navigator.clipboard.writeText(localContent); setViewStatus(view, 'Copied your unsaved version'); } }),
    h('button', { class:'copal-btn', text:'Copy latest', onclick:async() => { await navigator.clipboard.writeText(String(remote.text || '')); setViewStatus(view, 'Copied latest saved version'); } }),
    h('button', { class:'copal-btn', text:'Load latest', onclick:async() => {
      close();
      await refreshIndexedDocument(doc.id);
      notesFeature?.acceptSavedDocument(doc.id, String(remote.text || ''));
      if (state.windows.get('notes')?.window.visible) renderView('notes');
      setViewStatus(view, 'Loaded latest saved version');
    } }),
    h('button', { class:'copal-btn primary', text:'Save mine over latest', onclick:async() => {
      close();
      doc.head = remote.head;
      if (await saveDocument(doc, localContent, true, view)) notesFeature?.acceptSavedDocument(doc.id, localContent);
    } })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
}

async function saveDocument(doc, content, rerender = false, view = state.view) {
  setViewStatus(view, 'Saving…');
  try {
    const payload = { content, base:doc.head };
    if (doc.kind === 'note') {
      payload.properties = doc.properties || {};
      payload.relations = [
        ...(doc.relations || []).filter((relation) => relation.origin === 'explicit'),
        ...databaseRelations(content, state.docs),
      ];
    }
    const result = await api(`/documents/${encodeURIComponent(doc.id)}`, {
      method: 'PUT', body: JSON.stringify(payload),
    });
    doc.text = content;
    doc.head = result.doc?.head || doc.head;
    await refreshIndexedDocument(doc.id);
    if (/^---\n[\s\S]*?^copal_type:\s*["']?event["']?\s*$/m.test(content)) state.planning = await api('/planning');
    projectionChanged(result);
    if (doc.kind === 'planning' && result.calendar_projection?.ok !== false) state.projectedPlanningHead = doc.head;
    setViewStatus(view, 'Saved · Redb history updated');
    const active = state.view;
    for (const [openView, context] of state.windows) if (openView !== view && context.window.visible) renderView(openView);
    if (rerender) renderView(view);
    activateView(active);
    return true;
  } catch (error) {
    if (error.status === 409 && error.detail?.doc) {
      setViewStatus(view, 'Conflict: nothing overwritten; choose how to resolve it.', true);
      showDocumentConflict(doc, content, error.detail.doc, view);
      return false;
    }
    setViewStatus(view, error.message, true);
    return false;
  }
}

function scheduleSave(doc, textarea) {
  const view = state.view;
  clearTimeout(state.saveTimers.get(doc.id));
  setViewStatus(view, 'Unsaved');
  state.saveTimers.set(doc.id, setTimeout(() => { state.saveTimers.delete(doc.id); saveDocument(doc, textarea.value, false, view); }, 700));
}

function convertPluginBlock(source, lang) {
  if (lang !== 'dataview') {
    // Non-dataview blocks: create as note (existing behavior)
    showForm('Convert plugin block', [
      ['name', 'Base name', `${lang}-converted.base`],
    ], async ({ name }) => {
      const properties = { source_block: lang, original_source: source };
      const relations = databaseRelations(source, state.docs);
      await api('/documents', { method:'POST', body:JSON.stringify({ name, kind:'note', content:source, properties, relations }) });
      await loadDocuments(false);
      setStatus(`Created ${name} from ${lang} block`);
    });
    return;
  }
  // D2: Dataview → Base conversion with preview
  const parsed = parseDataviewQuery(source);
  const dialog = h('dialog', { class: 'copal-dialog' });
  const name = h('input', { value: 'Dataview.base', 'aria-label': 'Base name' });
  let previewBody;
  if (parsed) {
    const cols = parsed.fields.map((f) => h('th', { text: f.label }));
    const headerRow = h('tr', {}, ...cols);
    previewBody = h('div', {},
      h('p', { text: `Type: ${parsed.type}` }),
      h('p', { text: `Columns: ${parsed.fields.map((f) => f.label).join(', ')}` }),
      parsed.folder ? h('p', { text: `Source folder: ${parsed.folder}` }) : null,
      parsed.filter ? h('p', { text: `Filter: ${JSON.stringify(parsed.filter)}` }) : null,
      h('table', { class: 'copal-table', style: 'margin-top:8px' }, h('thead', {}, headerRow), h('tbody', {}, h('tr', {}, ...parsed.fields.map(() => h('td', { text: '—' }))))),
      h('p', { class: 'copal-base-diagnostic', text: 'Rows will be populated from live documents on creation.' }),
    );
  } else {
    previewBody = h('p', { class: 'copal-base-diagnostic', text: 'Could not parse this Dataview query. A default Base will be created.' });
  }
  dialog.append(h('h2', { text: 'Convert Dataview to Base' }), name, previewBody);
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create Base', onclick: async () => {
      const safeName = name.value.trim().endsWith('.base') ? name.value.trim() : `${name.value.trim()}.base`;
      if (!safeName || safeName === '.base') return;
      const def = makeDefaultBase(safeName.replace(/\.base$/i, ''));
      if (parsed && parsed.fields.length) {
        def.views[0].columns = parsed.fields.map((f) => ({ property: f.property, label: f.label }));
        if (parsed.filter) def.views[0].filters = parsed.filter;
      }
      try {
        await api('/documents', { method:'POST', body:JSON.stringify({ name: safeName, kind:'base', content: serializeBase(def) }) });
        await loadDocuments(false);
        dialog.close();
        setStatus(`Created Base ${safeName} from dataview block`);
      } catch (error) { setStatus(error.message, true); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus(); name.select();
}

function appendMarkdownInline(parent, value) {
  let rest = String(value || '');
  const token = /(!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]|`([^`]+)`|\*\*([^*]+)\*\*|~~([^~]+)~~|==([^=]+)==|(?<!\*)\*([^*]+)\*(?!\*)|\[([^\]]+)\]\(([^)\s]+)\)|\$([^$\n]+)\$|<%[^%]*%>|(?<![\p{L}\p{N}_])#([A-Za-z0-9_/-]+))/u;
  const appendText = (text) => parent.append(document.createTextNode(String(text || '').replace(/\\(?=[\\`*_[\]{}()#+.!|~-])/g, '')));
  while (rest) {
    const match = rest.match(token);
    if (!match) { appendText(rest); return; }
    appendText(rest.slice(0, match.index));
    if (match[2]) {
      const target = findByName(match[2]);
      parent.append(h('button', { class:'copal-chip', type:'button', text:match[3] || match[2], onclick:() => target && openDocument(target.id, state.view === 'notes' ? 'notes' : 'wiki') }));
    } else if (match[4]) parent.append(h('code', { text:match[4] }));
    else if (match[5]) parent.append(h('strong', { text:match[5] }));
    else if (match[6]) parent.append(h('del', { text:match[6] }));
    else if (match[7]) parent.append(h('mark', { text:match[7] }));
    else if (match[8]) parent.append(h('em', { text:match[8] }));
    else if (match[9]) {
      const target = /^https?:\/\//i.test(match[10]) ? null : findByName(match[10]);
      parent.append(/^https?:\/\//i.test(match[10])
        ? h('a', { href:match[10], target:'_blank', rel:'noopener noreferrer', text:match[9] })
        : h('button', { class:`copal-chip${target ? '' : ' unresolved'}`, type:'button', disabled:!target, text:match[9], onclick:() => target && openDocument(target.id, state.view === 'notes' ? 'notes' : 'wiki') }));
    } else if (match[11]) parent.append(h('span', { class:'copal-inline-math', text:match[11] }));
    else if (match[12]) parent.append(h('code', { class:'copal-templater-block', text:match[12] }));
    else if (match[13]) parent.append(h('span', { class:'copal-markdown-tag', text:`#${match[13]}` }));
    rest = rest.slice(match.index + match[0].length);
  }
}

function renderMarkdown(text, seen = new Set()) {
  const root = h('div');
  const lines = String(text || '').split('\n');
  let lineOffset = 0;
  if (lines[0]?.trim() === '---') {
    const end = lines.findIndex((line, index) => index > 0 && line.trim() === '---');
    if (end > 0) { lines.splice(0, end + 1); lineOffset = end + 1; }
  }
  let codeBlock = null;
  for (let index = 0; index < lines.length; index += 1) {
    const raw = lines[index].replace(/%%[^%\n]*(?:%(?!%)[^%\n]*)*%%/g, '');
    const fence = /^\s*```\s*([^\s`]*)/.exec(raw);
    if (fence) {
      if (codeBlock) { root.append(codeBlock.wrapper); codeBlock = null; }
      else {
        const lang = fence[1] || '';
        const isPluginBlock = /^(dataview|tasks|dataviewjs|tasksjs)$/i.test(lang);
        const code = h('code', { 'data-language':lang });
        const pre = h('pre', { class:'copal-markdown-code' }, code);
        const copy = h('button', { type:'button', class:'copal-btn copal-code-copy', text:'Copy', onclick:async() => navigator.clipboard.writeText(code.textContent || '') });
        const header = fence[1] ? h('figcaption', { text:fence[1] }) : null;
        if (isPluginBlock) {
          const banner = h('div', { class:'copal-plugin-block-banner' },
            h('span', { class:'copal-plugin-block-badge', text:`${lang} block` }),
            h('span', { text:'Inert — plugin queries are not executed in the editor.' }));
          const convertBtn = h('button', { type:'button', class:'copal-btn copal-plugin-convert', text:'Convert to Base', onclick:() => {
            convertPluginBlock(code.textContent || '', lang);
          } });
          codeBlock = { code, wrapper:h('figure', { class:'copal-code-block copal-plugin-block' }, header, banner, copy, convertBtn, pre) };
        } else {
          codeBlock = { code, wrapper:h('figure', { class:'copal-code-block' }, header, copy, pre) };
        }
      }
      continue;
    }
    if (codeBlock) { codeBlock.code.append(document.createTextNode(`${raw}\n`)); continue; }
    if (!raw.trim()) continue;
    if (/^\s*%%/.test(raw) || /^\s*<!--/.test(raw)) continue;
    if (raw.trim().startsWith('$$')) {
      const math = [raw];
      while (!(math.length > 1 || raw.trim() !== '$$') || !math.at(-1).trim().endsWith('$$')) {
        if (index + 1 >= lines.length) break;
        math.push(lines[++index]);
      }
      root.append(h('pre', { class:'copal-math-block', text:math.join('\n').replace(/^\s*\$\$|\$\$\s*$/g, '').trim() }));
      continue;
    }
    const transclusion = raw.trim().match(/^!\[\[([^\]|#]+)/);
    if (transclusion) {
      const target = findByName(transclusion[1]);
      const box = h('div', { class: 'copal-transclusion' });
      if (!target) box.textContent = `Missing: ${transclusion[1]}`;
      else if (seen.has(target.id)) box.textContent = `Transclusion cycle: ${target.name}`;
      else if (target.kind === 'asset') box.append(h('img', { class: 'copal-attachment', src: `${state.api}/api/copal/assets/${encodeURIComponent(target.id)}?workspace=${encodeURIComponent(state.workspace)}`, alt: target.name, loading: 'lazy' }));
      else {
        box.append(h('strong', { text: target.name }));
        box.append(renderMarkdown(target.text, new Set([...seen, target.id])));
      }
      root.append(box);
      continue;
    }
    const callout = raw.match(/^>\s*\[!([A-Za-z0-9_-]+)\][+-]?\s*(.*)$/);
    if (callout) {
      const body = [];
      while (lines[index + 1]?.match(/^>\s?/)) body.push(lines[++index].replace(/^>\s?/, ''));
      const box = h('aside', { class:`copal-callout copal-callout-${callout[1].toLowerCase()}` }, h('strong', { text:callout[2] || callout[1] }));
      for (const line of body) { const paragraph = h('p'); appendMarkdownInline(paragraph, line); box.append(paragraph); }
      root.append(box);
      continue;
    }
    if (raw.includes('|') && /^\s*\|?\s*:?-{3,}/.test(lines[index + 1] || '')) {
      const cells = (line) => line.trim().replace(/^\||\|$/g, '').split('|').map((cell) => cell.trim());
      const table = h('table', { class:'copal-markdown-table' });
      const header = h('tr'); for (const value of cells(raw)) { const cell = h('th'); appendMarkdownInline(cell, value); header.append(cell); }
      table.append(h('thead', {}, header)); index += 1;
      const body = h('tbody');
      while (lines[index + 1]?.includes('|')) {
        const row = h('tr'); for (const value of cells(lines[++index])) { const cell = h('td'); appendMarkdownInline(cell, value); row.append(cell); }
        body.append(row);
      }
      table.append(body); root.append(table); continue;
    }
    const image = raw.trim().match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (image) {
      const target = findByName(image[2]);
      if (target?.kind === 'asset') root.append(h('img', { class: 'copal-attachment', src: `${state.api}/api/copal/assets/${encodeURIComponent(target.id)}?workspace=${encodeURIComponent(state.workspace)}`, alt: image[1] || target.name, loading: 'lazy' }));
      else root.append(h('p', { text: raw }));
      continue;
    }
    const heading = raw.match(/^(#{1,6})\s+(.*)$/);
    if (heading) { const node = h(`h${heading[1].length}`, { 'data-line':String(index + 1 + lineOffset) }); appendMarkdownInline(node, heading[2]); root.append(node); continue; }
    const task = raw.match(/^(\s*)[-*+] \[([ xX])\]\s+(.*)$/);
    if (task) { const checkbox = h('input', { type:'checkbox', disabled:true, 'aria-label':task[3] }); checkbox.checked = !!task[2].trim(); const node = h('p', { class:'copal-markdown-task', style:`--indent:${task[1].length}` }, checkbox); appendMarkdownInline(node, task[3]); root.append(node); continue; }
    const bullet = raw.match(/^(\s*)[-*+]\s+(.*)$/);
    if (bullet) { const node = h('p', { class:'copal-markdown-bullet', style:`--indent:${bullet[1].length}` }, '• '); appendMarkdownInline(node, bullet[2]); root.append(node); continue; }
    const ordered = raw.match(/^(\s*)(\d+[.)])\s+(.*)$/);
    if (ordered) { const node = h('p', { class:'copal-markdown-bullet ordered', style:`--indent:${ordered[1].length}` }, `${ordered[2]} `); appendMarkdownInline(node, ordered[3]); root.append(node); continue; }
    const quote = raw.match(/^>\s?(.*)$/);
    if (quote) { const node = h('blockquote'); appendMarkdownInline(node, quote[1]); root.append(node); continue; }
    if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(raw)) { root.append(h('hr')); continue; }
    const footnote = raw.match(/^\s*\[\^([^\]]+)\]:\s*(.*)$/);
    if (footnote) { const node = h('aside', { class:'copal-footnote' }, h('sup', { text:footnote[1] })); appendMarkdownInline(node, footnote[2]); root.append(node); continue; }
    const para = h('p');
    appendMarkdownInline(para, raw);
    root.append(para);
  }
  if (codeBlock) root.append(codeBlock.wrapper);
  return root;
}

async function showHistory(doc) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: `History · ${doc.name}` }));
  const list = h('div', { text: 'Loading…' });
  dialog.append(list, h('div', { class: 'copal-dialog-actions' }, h('button', { class: 'copal-btn', text: 'Close', onclick: () => dialog.close() })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
  try {
    const result = await api(`/documents/${doc.id}/history`);
    list.replaceChildren();
    for (const change of result.changes || []) {
      const restore = h('button', { class: 'copal-btn', text: 'Restore', onclick: async () => {
        const restored = await api(`/documents/${doc.id}/restore`, { method: 'POST', body: JSON.stringify({ commit: change.commit }) });
        projectionChanged(restored);
        dialog.close(); await loadDocuments();
      } });
      list.append(h('div', { class: 'copal-task-row' }, h('span', { text: `${new Date(change.ts).toLocaleString()}${change.message ? ` · ${change.message}` : ''}` }), restore));
    }
  } catch (error) { list.textContent = error.message; }
}

async function showTrash(kind = null) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: kind === 'base' ? 'Deleted Bases' : 'Deleted Copal documents' }));
  const list = h('div', { text: 'Loading…' });
  dialog.append(list, h('div', { class: 'copal-dialog-actions' }, h('button', { class: 'copal-btn', text: 'Close', onclick: () => dialog.close() })));
  wireDialog(dialog); document.body.append(dialog);
  dialog.showModal();
  try {
    const result = await api('/trash');
    list.replaceChildren();
    const docs = (result.docs || []).filter((doc) => !kind || doc.kind === kind);
    for (const doc of docs) list.append(h('div', { class: 'copal-task-row' }, h('span', { text: doc.name }), h('small', { text: new Date(doc.ts).toLocaleString() }), h('button', { class: 'copal-btn', text: 'Restore', onclick: async () => { const restored = await api(`/trash/${encodeURIComponent(doc.id)}/restore`, { method: 'POST' }); projectionChanged(restored); if (kind === 'base') state.baseId = restored.doc?.id || doc.id; dialog.close(); await loadDocuments(); } })));
    if (!docs.length) list.textContent = 'Trash is empty.';
  } catch (error) { list.textContent = error.message; }
}

async function deleteDocument(doc) {
  if (!window.confirm(`Move ${doc.name} to Copal trash?`)) return;
  const deleted = await api(`/documents/${encodeURIComponent(doc.id)}`, { method: 'DELETE' });
  projectionChanged(deleted);
  state.selected = null;
  if (state.baseId === doc.id) state.baseId = null;
  await loadDocuments();
}

async function deleteDocuments(docs) {
  const unique = [...new Map((docs || []).map((doc) => [doc.id, doc])).values()];
  if (!unique.length || !window.confirm(`Move ${unique.length} selected Copal document${unique.length === 1 ? '' : 's'} to trash?`)) return false;
  for (const doc of unique) {
    const deleted = await api(`/documents/${encodeURIComponent(doc.id)}`, { method:'DELETE' });
    projectionChanged(deleted);
    if (state.baseId === doc.id) state.baseId = null;
  }
  if (unique.some((doc) => doc.id === state.selected)) state.selected = null;
  await loadDocuments();
  return true;
}

function destroyNoteEditors() {
  notesFeature?.destroy();
}

async function renameNote(doc, name) {
  const result = await api(`/documents/${encodeURIComponent(doc.id)}/rename`, { method:'POST', body:JSON.stringify({ name }) });
  await loadDocuments();
  return result;
}

function renderNotes() {
  notesFeature?.render();
}

function renderWiki() {
  const docs = visibleDocs('wiki').filter((doc) => doc.kind !== 'base' && doc.kind !== 'canvas');
  state.story = state.story.filter((id) => docs.some((doc) => doc.id === id));
  if (state.selected && docs.some((doc) => doc.id === state.selected) && !state.story.includes(state.selected)) state.story.unshift(state.selected);
  if (!state.story.length) state.story = docs.slice(0, 3).map((doc) => doc.id);
  const newMeme = async () => {
    const name = prompt('Meme name:');
    if (!name) return;
    const result = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: 'wiki', content: '', corpus: 'wiki' }) });
    await loadDocuments(false);
    openDocument(result.doc.id, 'wiki');
  };
  const library = h('aside', { class: 'copal-pane' },
    h('div', { class: 'copal-pane-header' },
      h('span', { text: 'Memes' }),
      h('button', { class: 'copal-btn', text: '+ Meme', onclick: newMeme })));
  const rows = h('div', { class: 'copal-scroll' });
  for (const doc of docs) rows.append(h('button', { class: 'copal-doc-row', onclick: () => { if (!state.story.includes(doc.id)) state.story.push(doc.id); renderWiki(); } }, doc.name));
  library.append(rows);
  const story = h('div', { class: 'copal-story' });
  for (const id of state.story) {
    const doc = state.docs.find((item) => item.id === id);
    if (!doc) continue;
    const card = h('article', { class: `copal-meme${state.pinned.has(id) ? ' pinned' : ''}` });
    const move = (delta) => { const index = state.story.indexOf(id); const next = index + delta; if (next < 0 || next >= state.story.length) return; [state.story[index], state.story[next]] = [state.story[next], state.story[index]]; renderWiki(); };
    const editing = state.wikiEditing.has(id);
    card.append(h('header', { class: 'copal-meme-head' }, h('strong', { text: doc.name }),
      h('button', { class: 'copal-btn', text: '←', 'aria-label': 'Move left', onclick: () => move(-1) }),
      h('button', { class: 'copal-btn', text: '→', 'aria-label': 'Move right', onclick: () => move(1) }),
      h('button', { class: 'copal-btn', text: state.pinned.has(id) ? 'Unpin' : 'Pin', onclick: () => { state.pinned.has(id) ? state.pinned.delete(id) : state.pinned.add(id); renderWiki(); } }),
      h('button', { class: 'copal-btn', text: 'History', onclick: () => showHistory(doc) }),
      h('button', { class: 'copal-btn', text: editing ? 'Read' : 'Edit', onclick: () => { editing ? state.wikiEditing.delete(id) : state.wikiEditing.add(id); renderWiki(); } }),
      h('button', { class: 'copal-btn', text: '×', 'aria-label': 'Close meme', onclick: () => { state.story = state.story.filter((value) => value !== id || state.pinned.has(id)); renderWiki(); } })));
    if (editing) {
      const editor = h('textarea', { class: 'copal-editor copal-wiki-editor', 'aria-label': `Edit ${doc.name}` });
      editor.value = doc.text || '';
      editor.addEventListener('input', () => scheduleSave(doc, editor));
      editor.addEventListener('blur', () => saveDocument(doc, editor.value));
      card.append(editor);
    } else {
      card.append(h('div', { class: 'copal-meme-body' }, renderMarkdown(doc.text, new Set([doc.id]))));
    }
    const footer = h('footer', { class: 'copal-inspector-section copal-meme-links' });
    // Meme fields: compact property strip.
    const props = doc.properties;
    if (props && typeof props === 'object' && Object.keys(props).length) {
      const fields = h('div', { class: 'copal-meme-fields' });
      for (const [key, value] of Object.entries(props)) {
        const display = Array.isArray(value) ? value.join(', ') : String(value ?? '');
        fields.append(h('span', { class: 'copal-chip', text: `${key}: ${display}` }));
      }
      footer.append(fields);
    }
    // Links section.
    const incoming = state.docs.filter((candidate) => (candidate.links || []).some((name) => normalizeName(name) === normalizeName(doc.name) || normalizeName(name) === normalizeName(doc.name.split('/').pop())));
    const links = h('div');
    links.append(h('strong', { text: 'Links ' }));
    for (const name of doc.links || []) { const target = findByName(name); links.append(h('button', { class: 'copal-chip', text: `→ ${name}`, onclick: () => target && openDocument(target.id, 'wiki') })); }
    for (const source of incoming) links.append(h('button', { class: 'copal-chip', text: `← ${source.name}`, onclick: () => openDocument(source.id, 'wiki') }));
    if (!(doc.links || []).length && !incoming.length) links.append(h('span', { text: 'None' }));
    footer.append(links);
    card.append(footer);
    story.append(card);
  }
  const shell = h('div', { class: 'copal-layout' }, library, h('section', { style: 'grid-column:2 / -1;min-width:0;overflow:hidden' }, story));
  state.body.replaceChildren(shell);
}

function allPlanningTasks(data = planningData()) {
  return (data.tracks || []).flatMap((track) => (track.tasks || []).map((task) => ({ ...task, track, primaryTrackId: track.id })));
}

function validDate(value) {
  if (!value || ['AUTO', 'FUZZY'].includes(value)) return null;
  const date = new Date(`${value}T00:00:00`);
  return Number.isNaN(date.valueOf()) ? null : date;
}

function iso(date) { return date.toISOString().slice(0, 10); }
function addDays(date, days) { const next = new Date(date); next.setDate(next.getDate() + days); return next; }
async function editPlanningTask(trackId, taskId) {
  const task = allPlanningTasks().find((item) => item.id === taskId && (!trackId || item.primaryTrackId === trackId));
  if (task) planningFeature.openEventEditor(task.id);
}

function renderTimeline() {
  planningFeature.renderTimeline(state.body);
}

function renderCalendar() {
  const data = planningData(); const tasks = allPlanningTasks(data).filter((task, index, all) => all.findIndex((item) => item.id === task.id) === index);
  const seed = state.calendarMonth || validDate(data.today) || new Date(); const first = new Date(seed.getFullYear(), seed.getMonth(), 1); const gridStart = addDays(first, -first.getDay());
  const toolbar = h('div', { class: 'copal-timeline-toolbar' },
    h('button', { class: 'copal-btn', text: '←', onclick: () => { state.calendarMonth = new Date(first.getFullYear(), first.getMonth() - 1, 1); renderCalendar(); } }),
    h('strong', { text: first.toLocaleDateString(undefined, { month: 'long', year: 'numeric' }) }),
    h('button', { class: 'copal-btn', text: '→', onclick: () => { state.calendarMonth = new Date(first.getFullYear(), first.getMonth() + 1, 1); renderCalendar(); } }));
  const calendar = h('div', { class: 'copal-calendar' });
  for (let day = 0; day < 42; day++) {
    const date = addDays(gridStart, day); const key = iso(date);
    const cell = h('div', { class: 'copal-day' }, h('strong', { text: date.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }) }));
    for (const task of tasks.filter((item) => item.startDate === key || item.dueDate === key || item.fuzzy?.anchorStart === key || item.fuzzy?.anchorEnd === key)) cell.append(h('button', { class: 'copal-day-event', text: task.title, onclick: () => editPlanningTask(task.primaryTrackId, task.id) }));
    calendar.append(cell);
  }
  state.body.replaceChildren(toolbar, calendar);
}

const graphState = { searchQuery: '', activeKinds: new Set(['note', 'event', 'wiki', 'base']), selectedNodeId: null };

function docKindToGraphKind(doc) {
  const kind = String(doc.kind || '').toLowerCase();
  if (kind === 'copal-event') return 'event';
  if (kind === 'markdown') return 'wiki';
  if (kind === 'base') return 'base';
  if (kind === 'note') return 'note';
  return 'note';
}

function kindColor(kind) {
  return { note: 'var(--accent, var(--red))', event: '#22c55e', wiki: '#3b82f6', base: '#f59e0b' }[kind] || 'var(--accent, var(--red))';
}

function graphSvg(nodes, edges, onOpen) {
  const root = h('div', { class: 'copal-graph-wrap' });
  const vb = { x: 0, y: 0, w: 1000, h: 650 };
  const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
  const svgEl = svg('svg', { class: 'copal-graph', viewBox: `${vb.x} ${vb.y} ${vb.w} ${vb.h}`, role: 'img', 'aria-label': `Graph showing ${nodes.length} nodes and ${edges.length} edges` });

  // B1: compute edge sets for highlight
  const nodeIds = new Set(nodes.map((n) => n.id));
  const adjacentEdges = new Map();
  for (const edge of edges) {
    if (!adjacentEdges.has(edge.from)) adjacentEdges.set(edge.from, []);
    if (!adjacentEdges.has(edge.to)) adjacentEdges.set(edge.to, []);
    adjacentEdges.get(edge.from).push(edge);
    adjacentEdges.get(edge.to).push(edge);
  }

  // Layout positions
  const positions = new Map();
  nodes.forEach((node, index) => { const angle = index / Math.max(1, nodes.length) * Math.PI * 2; const ring = 190 + (index % 3) * 45; positions.set(node.id, { x: 500 + Math.cos(angle) * ring, y: 325 + Math.sin(angle) * ring }); });

  // B2: filter visibility
  const query = graphState.searchQuery.toLowerCase();
  const visibleNodeIds = new Set();
  for (const node of nodes) {
    if (!graphState.activeKinds.has(node.kind || 'note')) continue;
    if (query && !String(node.label || '').toLowerCase().includes(query)) continue;
    visibleNodeIds.add(node.id);
  }

  // B1: render edges with type-based styling
  const edgeEls = [];
  for (const edge of edges) {
    const from = positions.get(edge.from); const to = positions.get(edge.to);
    if (!from || !to) continue;
    const edgeType = edge.type || 'link';
    const line = svg('line', { x1: from.x, y1: from.y, x2: to.x, y2: to.y, class: `copal-graph-edge edge-${edgeType}`, 'data-from': edge.from, 'data-to': edge.to });
    edgeEls.push({ el: line, edge });
    svgEl.append(line);
  }

  // B4: node elements with kind-based coloring
  const nodeEls = [];
  for (const node of nodes) {
    const pos = positions.get(node.id); if (!pos) continue;
    const visible = visibleNodeIds.has(node.id);
    const group = svg('g', { class: `copal-graph-node${visible ? '' : ' dimmed'}`, tabindex: '0', role: 'button', 'aria-label': `${node.label} (${node.kind || 'note'})` });
    const color = kindColor(node.kind || 'note');
    group.append(svg('circle', { cx: pos.x, cy: pos.y, r: node.hub ? 22 : 15, class: `kind-${node.kind || 'note'}${node.hub ? ' hub' : ''}`, fill: color, stroke: color }));
    const label = svg('text', { x: pos.x + (node.hub ? 26 : 20), y: pos.y + 4, class: 'copal-graph-label' });
    label.textContent = String(node.label || '').slice(0, 40);
    group.append(label);
    nodeEls.push({ el: group, node });
    svgEl.append(group);
  }

  // Inspector panel (B3)
  const inspector = h('div', { class: 'copal-graph-inspector' });

  function updateInspector(node) {
    inspector.textContent = '';
    if (!node) return;
    const name = h('div', { class: 'copal-graph-inspector-name', text: node.label || '' });
    const kindLabel = h('span', { class: `copal-graph-inspector-kind kind-${node.kind || 'note'}`, text: (node.kind || 'note').charAt(0).toUpperCase() + (node.kind || 'note').slice(1) });
    const meta = h('div', { class: 'copal-graph-inspector-meta' });

    // Find connected nodes
    const connected = adjacentEdges.get(node.id) || [];
    const links = h('div', { class: 'copal-graph-inspector-links' });
    for (const edge of connected) {
      const otherId = edge.from === node.id ? edge.to : edge.from;
      const otherNode = nodes.find((n) => n.id === otherId);
      if (!otherNode) continue;
      const chip = h('button', { class: 'copal-graph-inspector-chip', text: `${edge.type === 'embed' ? '⬒ ' : edge.type === 'relation' ? '⋯ ' : '→ '}${otherNode.label || otherId}`, onclick: () => onOpen(otherNode) });
      links.append(chip);
    }

    const openBtn = h('button', { class: 'copal-graph-inspector-open', text: 'Open in Notes', onclick: () => onOpen(node) });

    meta.textContent = `${connected.length} connection${connected.length !== 1 ? 's' : ''}`;
    inspector.append(name, kindLabel, meta);
    if (connected.length) inspector.append(links);
    inspector.append(openBtn);
  }

  function clearInspector() { inspector.textContent = ''; }

  // B4: select/deselect node
  let selectedEl = null;
  function selectNode(nodeEl, node) {
    if (selectedEl) selectedEl.classList.remove('selected');
    selectedEl = nodeEl;
    nodeEl.classList.add('selected');
    graphState.selectedNodeId = node.id;
    updateInspector(node);
    // highlight adjacent edges
    for (const ee of edgeEls) ee.el.classList.toggle('edge-highlight', ee.edge.from === node.id || ee.edge.to === node.id);
  }
  function deselectNode() {
    if (selectedEl) selectedEl.classList.remove('selected');
    selectedEl = null;
    graphState.selectedNodeId = null;
    clearInspector();
    for (const ee of edgeEls) ee.el.classList.remove('edge-highlight');
  }

  // Wire node click/select
  for (const { el, node } of nodeEls) {
    el.addEventListener('click', () => { selectNode(el, node); onOpen(node); });
    el.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectNode(el, node); onOpen(node); }
      // B4: arrow key navigation
      if (event.key === 'ArrowRight' || event.key === 'ArrowDown' || event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
        event.preventDefault();
        const currentIdx = nodeEls.findIndex((n) => n.node.id === node.id);
        let nextIdx = currentIdx;
        if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIdx = (currentIdx + 1) % nodeEls.length;
        else nextIdx = (currentIdx - 1 + nodeEls.length) % nodeEls.length;
        const next = nodeEls[nextIdx];
        next.el.focus();
        selectNode(next.el, next.node);
      }
    });
    // B4: click on SVG background to deselect
    svgEl.addEventListener('click', (e) => { if (e.target === svgEl || e.target.tagName === 'line') deselectNode(); });
  }

  // Zoom controls
  const zoomIn = h('button', { class: 'copal-graph-ctrl', title: 'Zoom in', 'aria-label': 'Zoom in', onclick: () => { vb.w *= 0.8; vb.h *= 0.8; vb.x += vb.w * 0.1; vb.y += vb.h * 0.1; svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); } });
  const zoomOut = h('button', { class: 'copal-graph-ctrl', title: 'Zoom out', 'aria-label': 'Zoom out', onclick: () => { vb.x -= vb.w * 0.125; vb.y -= vb.h * 0.125; vb.w *= 1.25; vb.h *= 1.25; svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); } });
  const reset = h('button', { class: 'copal-graph-ctrl', title: 'Reset view', 'aria-label': 'Reset view', onclick: () => { vb.x = 0; vb.y = 0; vb.w = 1000; vb.h = 650; svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); } });

  // B1: Legend with actual node/edge types
  const presentKinds = new Set(nodes.map((n) => n.kind || 'note'));
  const presentEdgeTypes = new Set(edges.map((e) => e.type || 'link'));
  const legendItems = [];
  const kindOrder = ['note', 'event', 'wiki', 'base'];
  const kindLabels = { note: 'Notes', event: 'Events', wiki: 'Wiki', base: 'Bases' };
  for (const kind of kindOrder) {
    if (!presentKinds.has(kind)) continue;
    legendItems.push(h('span', { class: 'copal-graph-legend-item' }, svg('circle', { cx: 6, cy: 6, r: 6, fill: kindColor(kind), stroke: kindColor(kind) }), h('span', { text: kindLabels[kind] })));
  }
  const edgeOrder = ['link', 'embed', 'relation'];
  const edgeLabels = { link: 'Links', embed: 'Embeds', relation: 'Relations' };
  const edgeDash = { link: '', embed: '6 3', relation: '2 3' };
  for (const type of edgeOrder) {
    if (!presentEdgeTypes.has(type)) continue;
    legendItems.push(h('span', { class: 'copal-graph-legend-item' }, svg('line', { x1: 0, y1: 6, x2: 18, y2: 6, class: `copal-graph-edge edge-${type}` }), h('span', { text: edgeLabels[type] })));
  }
  const legend = h('div', { class: 'copal-graph-legend' }, ...legendItems);
  const controls = h('div', { class: 'copal-graph-controls' }, zoomIn, zoomOut, reset, legend);

  // B2: search + filter toolbar
  const searchInput = h('input', { class: 'copal-graph-search', type: 'search', placeholder: 'Search nodes…', 'aria-label': 'Search graph nodes', value: graphState.searchQuery });
  const filters = h('div', { class: 'copal-graph-filters' });
  const kindCheckboxes = [];
  for (const kind of kindOrder) {
    if (!presentKinds.has(kind)) continue;
    const cb = h('label', { class: 'copal-graph-filter-label' });
    const input = h('input', { type: 'checkbox', checked: graphState.activeKinds.has(kind) || undefined });
    input.addEventListener('change', () => { if (input.checked) graphState.activeKinds.add(kind); else graphState.activeKinds.delete(kind); });
    cb.append(input, h('span', { text: kindLabels[kind] }));
    filters.append(cb);
    kindCheckboxes.push({ kind, input });
  }
  const toolbar = h('div', { class: 'copal-graph-toolbar' }, searchInput, filters);

  searchInput.addEventListener('input', () => { graphState.searchQuery = searchInput.value; rebuildGraph(); });

  // B4: screen-reader summary
  const info = h('div', { class: 'copal-graph-info', role: 'status', 'aria-live': 'polite' }, h('span', { text: `Graph showing ${nodes.length} nodes and ${edges.length} edges` }));

  // Rebuild on filter/search change
  function rebuildGraph() {
    const q = graphState.searchQuery.toLowerCase();
    for (const { el, node } of nodeEls) {
      const visible = graphState.activeKinds.has(node.kind || 'note') && (!q || String(node.label || '').toLowerCase().includes(q));
      el.classList.toggle('dimmed', !visible);
    }
    updateInspector(graphState.selectedNodeId ? nodes.find((n) => n.id === graphState.selectedNodeId) : null);
  }

  root.append(toolbar, svgEl, controls, inspector, info);

  // Pan via pointer drag
  let dragging = false, lastX = 0, lastY = 0;
  svgEl.addEventListener('pointerdown', (e) => { if (e.target === svgEl || e.target.tagName === 'line') { dragging = true; lastX = e.clientX; lastY = e.clientY; svgEl.setPointerCapture(e.pointerId); } });
  if (!reducedMotion) {
    svgEl.addEventListener('pointermove', (e) => { if (!dragging) return; const scale = vb.w / svgEl.clientWidth; vb.x -= (e.clientX - lastX) * scale; vb.y -= (e.clientY - lastY) * scale; lastX = e.clientX; lastY = e.clientY; svgEl.setAttribute('viewBox', `${vb.x} ${vb.y} ${vb.w} ${vb.h}`); });
  }
  svgEl.addEventListener('pointerup', () => { dragging = false; });
  return root;
}

function renderGalaxy() {
  const data = planningData(); const tracks = data.tracks || []; const shared = allPlanningTasks(data).filter((task) => (task.sharedTrackIds || []).length);
  const nodes = [...tracks.map((track) => ({ id: `track:${track.id}`, label: track.name })), ...shared.map((task) => ({ id: `task:${task.id}`, label: task.title, hub: true, task }))];
  const edges = [];
  for (const item of shared) {
    edges.push({ from: `track:${item.primaryTrackId}`, to: `task:${item.id}` });
    for (const target of item.sharedTrackIds || []) edges.push({ from: `task:${item.id}`, to: `track:${target}` });
  }
  state.body.replaceChildren(graphSvg(nodes, edges, (node) => node.task && editPlanningTask(node.task.primaryTrackId, node.task.id)));
}

function renderGraph() {
  // B2: respect dot-folder toggle from workspace settings
  let docs = visibleDocs();
  try {
    const saved = JSON.parse(localStorage.getItem(`odysseus-copal-notes-layout:${state.workspace}`) || '{}');
    const showDot = saved?.left?.showDotFolders === true;
    if (!showDot) docs = docs.filter((doc) => !(doc.name || '').split('/').some((part) => part.startsWith('.')));
  } catch (_) {}

  const nodes = docs.map((doc) => ({ id: doc.id, label: doc.name, doc, kind: docKindToGraphKind(doc) }));

  // B1: build edges from both doc.links (wiki-style) and doc.relations (structured)
  const edgeMap = new Map();
  function addEdge(from, to, type) {
    const key = `${from}\0${to}`;
    if (edgeMap.has(key)) return;
    edgeMap.set(key, { from, to, type });
  }
  const docIds = new Set(docs.map((d) => d.id));
  for (const doc of docs) {
    for (const link of doc.links || []) {
      const target = findByName(link);
      if (target && docIds.has(target.id)) addEdge(doc.id, target.id, 'link');
    }
    for (const rel of doc.relations || []) {
      if (rel.targetDocumentId && docIds.has(rel.targetDocumentId) && rel.targetDocumentId !== doc.id) {
        addEdge(doc.id, rel.targetDocumentId, rel.kind || 'relation');
      }
    }
  }
  const edges = [...edgeMap.values()];
  state.body.replaceChildren(graphSvg(nodes, edges, (node) => node.doc && openDocument(node.doc.id, 'notes')));
}

const mindState = { docId: null, collapsed: new Set(), selectedLine: null, editingLine: null, draggingLine: null };

function buildMindTree(entries) {
  const nodes = entries.map((entry) => ({ ...entry, children: [] }));
  const stack = [];
  for (const node of nodes) {
    while (stack.length && stack[stack.length - 1].level >= node.level) stack.pop();
    if (stack.length) stack[stack.length - 1].children.push(node);
    stack.push(node);
  }
  return nodes.filter((node) => !stack.some((parent) => parent !== node && isAncestor(parent, node, entries)));
}

function isAncestor(potential, node, entries) {
  const pi = entries.indexOf(potential);
  const ni = entries.indexOf(node);
  if (pi >= ni) return false;
  for (let i = pi + 1; i < ni; i++) {
    if (entries[i].level <= potential.level) return false;
  }
  return true;
}

function renderMindTree(nodes, doc, entries, depth = 0) {
  const list = h('ul', { class: 'copal-mind-tree-list' });
  for (const node of nodes) {
    const isCollapsed = mindState.collapsed.has(node.line);
    const isSelected = mindState.selectedLine === node.line;
    const isEditing = mindState.editingLine === node.line;
    const hasChildren = node.children.length > 0;

    const label = h('span', { class: 'copal-mind-tree-label', text: node.text });
    if (isEditing) {
      const input = h('input', { class: 'copal-mind-tree-input', type: 'text', value: node.text });
      label.replaceChildren(input);
      requestAnimationFrame(() => { input.focus(); input.select(); });
      input.addEventListener('blur', () => {
        const next = input.value.trim();
        if (next && next !== node.text) {
          const source = String(doc.text || '');
          const lines = source.split('\n');
          const lineIdx = node.line - 1;
          const hashes = '#'.repeat(node.level);
          lines[lineIdx] = `${hashes} ${next}`;
          saveDocument(doc, lines.join('\n'), true);
        }
        mindState.editingLine = null;
        renderMind();
      });
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
        if (e.key === 'Escape') { mindState.editingLine = null; renderMind(); }
        e.stopPropagation();
      });
    }

    const toggle = h('button', { class: 'copal-mind-tree-toggle', text: hasChildren ? (isCollapsed ? '\u25B6' : '\u25BC') : '\u00A0' });
    toggle.addEventListener('click', (e) => { e.stopPropagation(); if (hasChildren) { if (mindState.collapsed.has(node.line)) mindState.collapsed.delete(node.line); else mindState.collapsed.add(node.line); renderMind(); } });

    const nodeEl = h('li', { class: `copal-mind-tree-node${isSelected ? ' selected' : ''}`, tabindex: '0', 'data-line': String(node.line) }, toggle, label);

    nodeEl.addEventListener('click', () => { mindState.selectedLine = node.line; renderMind(); });
    nodeEl.addEventListener('dblclick', (e) => { e.preventDefault(); mindState.editingLine = node.line; renderMind(); });
    nodeEl.addEventListener('keydown', (e) => {
      if (isEditing) return;
      if (e.key === 'Enter' || e.key === 'F2') { e.preventDefault(); mindState.editingLine = node.line; renderMind(); return; }
      if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); mindMindDeleteHeading(doc, node); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); mindSelectNext(node, entries); return; }
      if (e.key === 'ArrowUp') { e.preventDefault(); mindSelectPrev(node, entries); return; }
      if (e.key === 'ArrowRight') { e.preventDefault(); if (hasChildren && isCollapsed) { mindState.collapsed.delete(node.line); renderMind(); } else if (hasChildren) { const next = node.children[0]; mindState.selectedLine = next.line; renderMind(); } return; }
      if (e.key === 'ArrowLeft') { e.preventDefault(); if (hasChildren && !isCollapsed) { mindState.collapsed.add(node.line); renderMind(); } return; }
      if (e.key === 'Tab') { e.preventDefault(); const src = String(doc.text || ''); const newLv = Math.max(1, Math.min(6, node.level + (e.shiftKey ? -1 : 1))); mindMindRenameHeading(doc, node, newLv); return; }
      if (e.key === ' ') { e.preventDefault(); if (hasChildren) { if (mindState.collapsed.has(node.line)) mindState.collapsed.delete(node.line); else mindState.collapsed.add(node.line); renderMind(); } return; }
    });

    // Drag reparent
    nodeEl.draggable = true;
    nodeEl.addEventListener('dragstart', (e) => { mindState.draggingLine = node.line; e.dataTransfer.effectAllowed = 'move'; nodeEl.classList.add('dragging'); });
    nodeEl.addEventListener('dragend', () => { mindState.draggingLine = null; nodeEl.classList.remove('dragging'); });
    nodeEl.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; nodeEl.classList.add('drag-over'); });
    nodeEl.addEventListener('dragleave', () => nodeEl.classList.remove('drag-over'));
    nodeEl.addEventListener('drop', (e) => {
      e.preventDefault(); nodeEl.classList.remove('drag-over');
      const fromLine = mindState.draggingLine;
      mindState.draggingLine = null;
      if (fromLine && fromLine !== node.line) mindMindReparent(doc, fromLine, node.line);
    });

    if (!isCollapsed && hasChildren) nodeEl.append(renderMindTree(node.children, doc, entries, depth + 1));
    list.append(nodeEl);
  }
  return list;
}


function mindSelectNext(node, entries) {
  const idx = entries.findIndex((e) => e.line === node.line);
  if (idx < entries.length - 1) { mindState.selectedLine = entries[idx + 1].line; renderMind(); }
}

function mindSelectPrev(node, entries) {
  const idx = entries.findIndex((e) => e.line === node.line);
  if (idx > 0) { mindState.selectedLine = entries[idx - 1].line; renderMind(); }
}

function mindMindDeleteHeading(doc, node) {
  const source = String(doc.text || '');
  const lines = source.split('\n');
  const entries = outlineEntries(source);
  const idx = entries.findIndex((e) => e.line === node.line);
  if (idx < 0) return;
  let endLine = lines.length;
  for (let i = idx + 1; i < entries.length; i++) {
    if (entries[i].level <= node.level) { endLine = entries[i].line; break; }
  }
  const before = lines.slice(0, node.line - 1).join('\n');
  const after = lines.slice(endLine - 1).join('\n');
  const newSource = [before, after].filter(Boolean).join('\n');
  saveDocument(doc, newSource, true);
  mindState.selectedLine = null;
}

function mindMindRenameHeading(doc, node, newLevel) {
  const source = String(doc.text || '');
  const lines = source.split('\n');
  lines[node.line - 1] = `${'#'.repeat(newLevel)} ${node.text}`;
  saveDocument(doc, lines.join('\n'), true);
}

function mindMindReparent(doc, fromLine, toLine) {
  const source = String(doc.text || '');
  const entries = outlineEntries(source);
  const from = entries.find((e) => e.line === fromLine);
  const to = entries.find((e) => e.line === toLine);
  if (!from || !to || fromLine === toLine) return;
  // Change level to be one deeper than the target
  const newLevel = Math.max(1, Math.min(6, to.level + 1));
  const afterLevelChange = reparentHeading(source, fromLine, newLevel);
  // Move section after the target's section
  const moved = moveHeadingSectionTo(afterLevelChange, fromLine, toLine);
  saveDocument(doc, moved, true);
  mindState.selectedLine = fromLine;
}

function renderMind() {
  const docs = visibleDocs().filter((doc) => doc.kind === 'markdown' || doc.kind === 'note');
  if (!docs.length) {
    state.body.replaceChildren(h('div', { class: 'copal-mind-empty' }, h('h2', { text: 'Mind' }), h('p', { text: 'No markdown documents available. Import or create notes to use the hierarchy editor.' })));
    return;
  }
  if (!mindState.docId || !docs.some((doc) => doc.id === mindState.docId)) mindState.docId = docs[0].id;
  const doc = docs.find((d) => d.id === mindState.docId);
  if (!doc) { state.body.replaceChildren(h('div', { class: 'copal-mind-empty', text: 'Document not found.' })); return; }
  const source = String(doc.text || '');
  const entries = outlineEntries(source);
  const treeNodes = buildMindTree(entries);
  const mindEl = h('div', { class: 'copal-mind' });

  // Document picker
  const picker = h('div', { class: 'copal-mind-picker' }, h('h3', { text: 'Documents' }));
  const docList = h('ul', { class: 'copal-mind-doc-list' });
  for (const d of docs) {
    const item = h('li', { class: `copal-mind-doc-item${d.id === mindState.docId ? ' active' : ''}` },
      h('button', { class: 'copal-mind-doc-btn', text: d.name, onclick: () => { mindState.docId = d.id; mindState.collapsed.clear(); mindState.selectedLine = null; mindState.editingLine = null; renderMind(); } }));
    docList.append(item);
  }
  picker.append(docList);

  // Tree
  const treePane = h('div', { class: 'copal-mind-tree' });
  if (!treeNodes.length) {
    treePane.append(h('div', { class: 'copal-mind-empty-tree' }, h('p', { text: 'No headings found. Add headings in the source editor to see them here.' })));
  } else {
    // Toolbar
    const toolbar = h('div', { class: 'copal-mind-toolbar' },
      h('button', { class: 'copal-btn', text: '+ Heading', onclick: () => mindMindAddHeading(doc) }),
      h('button', { class: 'copal-btn', text: 'Delete', onclick: () => { if (mindState.selectedLine) { const n = treeNodes.find((e) => e.line === mindState.selectedLine); if (n) mindMindDeleteHeading(doc, n); } } }),
      h('button', { class: 'copal-btn', text: '\u2191', title: 'Move up', onclick: () => { if (mindState.selectedLine) { const newSrc = moveHeadingSection(source, mindState.selectedLine, -1); const moved = outlineEntries(newSrc).find((e) => e.text === entries.find((x) => x.line === mindState.selectedLine)?.text); mindState.selectedLine = moved?.line || mindState.selectedLine; saveDocument(doc, newSrc, true); } } }),
      h('button', { class: 'copal-btn', text: '\u2193', title: 'Move down', onclick: () => { if (mindState.selectedLine) { const newSrc = moveHeadingSection(source, mindState.selectedLine, 1); const moved = outlineEntries(newSrc).find((e) => e.text === entries.find((x) => x.line === mindState.selectedLine)?.text); mindState.selectedLine = moved?.line || mindState.selectedLine; saveDocument(doc, newSrc, true); } } }),
      h('span', { class: 'copal-mind-doc-name', text: doc.name }),
      h('span', { class: 'copal-mind-headings-count', text: `${entries.length} headings` }),
    );
    treePane.append(toolbar);
    const treeList = renderMindTree(treeNodes, doc, entries);
    treePane.append(treeList);
  }

  mindEl.append(picker, treePane);
  state.body.replaceChildren(mindEl);

  // Focus selected
  if (mindState.selectedLine) {
    const selected = mindEl.querySelector(`[data-line="${mindState.selectedLine}"]`);
    if (selected) selected.focus();
  }
}

function mindMindAddHeading(doc) {
  const source = String(doc.text || '');
  const entries = outlineEntries(source);
  let newLine, level = 1;
  if (mindState.selectedLine) {
    const sel = entries.find((e) => e.line === mindState.selectedLine);
    if (sel) {
      level = sel.level;
      // Find end of selected section
      const idx = entries.indexOf(sel);
      let endLine = source.split('\n').length;
      for (let i = idx + 1; i < entries.length; i++) {
        if (entries[i].level <= sel.level) { endLine = entries[i].line; break; }
      }
      newLine = endLine;
    }
  }
  if (!newLine) newLine = (entries.length ? entries[entries.length - 1].line : 0) + 1;
  const lines = source.split('\n');
  lines.splice(newLine - 1, 0, `${'#'.repeat(level)} New heading`);
  const newSource = lines.join('\n');
  const newEntries = outlineEntries(newSource);
  const added = newEntries.find((e) => e.line === newLine);
  if (added) { mindState.selectedLine = added.line; mindState.editingLine = added.line; }
  saveDocument(doc, newSource, true);
}

function baseField(label, control) {
  return h('label', { class: 'copal-base-field' }, h('span', { text: label }), control);
}

function parseBaseLiteral(value) {
  const text = String(value || '').trim();
  if (!text) return '';
  try { return JSON.parse(text); } catch (_) { return text; }
}

async function createBaseDocument() {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: 'Create a live Base' }));
  const name = h('input', { value: 'Projects.base', 'aria-label': 'Base name' });
  dialog.append(baseField('Name', name), h('p', { text: 'Starts as a live table over this Copal workspace. Configure columns, filters, sorts, groups, formulas, and summaries after creation.' }));
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create', onclick: async () => {
      const safeName = name.value.trim().endsWith('.base') ? name.value.trim() : `${name.value.trim()}.base`;
      if (!safeName || safeName === '.base') return;
      try {
        const created = await api('/documents', { method: 'POST', body: JSON.stringify({ name: safeName, kind: 'base', content: serializeBase(makeDefaultBase(safeName.replace(/\.base$/i, ''))) }) });
        state.baseId = created.doc?.id || null;
        dialog.close();
        await loadDocuments();
      } catch (error) { setStatus(error.message, true); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus(); name.select();
}

async function renameBaseDocument(base) {
  const proposed = window.prompt('Rename this Base:', base.name);
  if (!proposed?.trim()) return;
  const name = proposed.trim().endsWith('.base') ? proposed.trim() : `${proposed.trim()}.base`;
  try {
    await api(`/documents/${encodeURIComponent(base.id)}/rename`, { method: 'POST', body: JSON.stringify({ name }) });
    await loadDocuments(false); renderBases();
  } catch (error) { setStatus(error.message, true); }
}

async function duplicateBaseDocument(base) {
  const stem = base.name.replace(/\.base$/i, '');
  const proposed = window.prompt('Name the duplicated Base:', `${stem} copy.base`);
  if (!proposed?.trim()) return;
  const name = proposed.trim().endsWith('.base') ? proposed.trim() : `${proposed.trim()}.base`;
  try {
    const created = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: 'base', content: base.text || serializeBase(state.baseDefinition || makeDefaultBase(stem)) }) });
    state.baseId = created.doc?.id || null; state.baseView = null; state.basePage = 1;
    await loadDocuments();
  } catch (error) { setStatus(error.message, true); }
}

function parseDesignerLines(text, separator, mapper) {
  return String(text || '').split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const index = line.indexOf(separator);
    const left = index < 0 ? line : line.slice(0, index).trim();
    const right = index < 0 ? '' : line.slice(index + separator.length).trim();
    return mapper(left, right);
  });
}

function configureBase(base, definition, viewId) {
  const next = JSON.parse(JSON.stringify(definition));
  const view = next.views.find((item) => item.id === viewId) || next.views[0];
  const dialog = h('dialog', { class: 'copal-dialog copal-base-designer' }, h('h2', { text: `Configure ${view.name}` }));
  const name = h('input', { value: view.name });
  const columns = h('textarea', { rows: '4', placeholder: 'file.name | Name | 220' }); columns.value = view.columns.filter((column) => !column.formula).map((column) => `${column.property} | ${column.label || column.property}${column.width ? ` | ${column.width}` : ''}`).join('\n');
  const formulas = h('textarea', { rows: '3', placeholder: 'total = price * quantity' }); formulas.value = view.columns.filter((column) => column.formula).map((column) => `${column.property} = ${column.formula}`).join('\n');
  const filters = h('textarea', { rows: '3', placeholder: 'status | eq | active' });
  const hasNested = hasNestedFilterGroups(view.filters);
  const filterRawJson = h('textarea', { rows: '5', placeholder: '{"and": [...]}' });
  const filterMode = h('select'); for (const mode of ['and', 'or']) filterMode.append(h('option', { value: mode, text: mode.toUpperCase(), selected: !!view.filters?.[mode] }));
  filters.value = flattenFilterToLines(view.filters).join('\n');
  filterRawJson.value = JSON.stringify(view.filters, null, 2);
  if (hasNested) { filterRawJson.style.display = ''; filters.style.display = 'none'; } else { filterRawJson.style.display = 'none'; }
  const useRawJson = h('label', { class: 'copal-base-field' }, h('input', { type: 'checkbox', checked: hasNested }), ' Edit filter as raw JSON');
  useRawJson.querySelector('input').addEventListener('change', (e) => {
    filterRawJson.style.display = e.target.checked ? '' : 'none';
    filters.style.display = e.target.checked ? 'none' : '';
  });
  const sorts = h('textarea', { rows: '2', placeholder: 'file.name : asc' }); sorts.value = (view.sorts || []).map((sort) => `${sort.property} : ${sort.direction}`).join('\n');
  const groupBy = h('input', { value: view.groupBy || '', placeholder: 'category' });
  const summaries = h('textarea', { rows: '2', placeholder: 'price : avg' }); summaries.value = Object.entries(view.summaries || {}).map(([property, operation]) => `${property} : ${operation}`).join('\n');
  const limit = h('input', { type: 'number', min: '1', max: '5000', value: String(view.limit || 1000) });
  dialog.append(
    baseField('View name', name),
    baseField('Columns (one “property | label | width” per line)', columns),
    baseField('Formulas (one “property = expression” per line)', formulas),
    baseField('Filter mode', filterMode),
    baseField('Filters (one “property | operator | value” per line)', filters),
    useRawJson,
    baseField('Filter JSON (raw)', filterRawJson),
    baseField('Sorts (one “property : asc/desc” per line)', sorts),
    baseField('Group by property', groupBy),
    baseField('Summaries (one “property : count/sum/avg/min/max/distinct” per line)', summaries),
    baseField('Maximum rows', limit),
  );
  const feedback = h('p', { class: 'copal-base-feedback', role: 'status' });
  dialog.append(feedback, h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Validate & save', onclick: async () => {
      try {
        view.name = name.value.trim() || 'Table';
        view.columns = columns.value.split(/\n|,(?![^|]*\|)/).map((line) => line.trim()).filter(Boolean).map((line) => {
          const [property, label, width] = line.split('|').map((part) => part.trim());
          return { property, label: label || property, ...(Number(width) > 0 ? { width: Math.round(Number(width)) } : {}) };
        });
        for (const formula of parseDesignerLines(formulas.value, '=', (property, expression) => ({ property, label: property, formula: expression }))) view.columns.push(formula);
        const useRaw = useRawJson.querySelector('input').checked;
        if (useRaw) {
          const rawText = filterRawJson.value.trim();
          if (!rawText) { view.filters = null; }
          else {
            let parsed;
            try { parsed = JSON.parse(rawText); } catch { throw new Error('Invalid JSON in filter editor'); }
            view.filters = parsed;
          }
        } else {
          view.filters = parseFilterLines(filters.value.split('\n'), filterMode.value);
        }
        view.sorts = parseDesignerLines(sorts.value, ':', (property, direction) => ({ property, direction: (direction || 'asc').toLowerCase() }));
        view.groupBy = groupBy.value.trim() || null;
        view.summaries = Object.fromEntries(parseDesignerLines(summaries.value, ':', (property, operation) => [property, (operation || 'count').toLowerCase()]));
        view.limit = Number(limit.value) || 1000;
        const validation = await api('/bases/validate', { method: 'POST', body: JSON.stringify({ content: serializeBase(next) }) });
        const saved = await saveDocument(base, validation.canonical, false);
        if (!saved) return;
        state.baseDefinition = validation.definition;
        state.baseView = view.id;
        dialog.close(); renderBases();
      } catch (error) { feedback.textContent = error.message; feedback.classList.add('error'); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); name.focus();
}

async function addBaseView(base, definition) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: 'Add Base View' }));
  const viewName = h('input', { value: 'Table 2', 'aria-label': 'View name' });
  const viewType = h('select', { 'aria-label': 'View type' });
  for (const type of VIEW_TYPES) viewType.append(h('option', { value: type, text: type.charAt(0).toUpperCase() + type.slice(1), selected: type === 'table' }));
  dialog.append(baseField('View name', viewName), baseField('View type', viewType));
  dialog.append(h('div', { class: 'copal-dialog-actions' },
    h('button', { class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() }),
    h('button', { class: 'copal-btn primary', text: 'Create', onclick: async () => {
      const name = viewName.value.trim();
      if (!name) return;
      const next = JSON.parse(JSON.stringify(definition));
      const template = makeViewFromTemplate(next.views[0] || makeDefaultBase().views[0], name, viewType.value);
      next.views.push(template);
      const validation = await api('/bases/validate', { method: 'POST', body: JSON.stringify({ content: serializeBase(next) }) });
      if (await saveDocument(base, validation.canonical, false)) { state.baseView = template.id; state.baseDefinition = validation.definition; dialog.close(); renderBases(); }
    } })));
  wireDialog(dialog); document.body.append(dialog); dialog.showModal(); viewName.focus();
}

function makeBaseColumnResizer(base, definition, viewId, property, cell) {
  const handle = h('span', { class: 'copal-base-resize', role: 'separator', tabindex: '0', 'aria-label': `Resize ${property} column` });
  const persist = async (width) => {
    const next = JSON.parse(JSON.stringify(definition));
    const view = next.views.find((item) => item.id === viewId) || next.views[0];
    const column = view.columns.find((item) => item.property === property);
    if (!column) return;
    column.width = Math.max(80, Math.min(600, Math.round(width)));
    if (await saveDocument(base, serializeBase(next), false)) renderBases();
  };
  handle.addEventListener('pointerdown', (event) => {
    event.preventDefault(); event.stopPropagation();
    const startX = event.clientX; const startWidth = cell.getBoundingClientRect().width;
    handle.setPointerCapture(event.pointerId);
    const move = (nextEvent) => { cell.style.width = `${Math.max(80, Math.min(600, startWidth + nextEvent.clientX - startX))}px`; };
    const end = async () => {
      handle.removeEventListener('pointermove', move); handle.removeEventListener('pointerup', end); handle.removeEventListener('pointercancel', end);
      await persist(cell.getBoundingClientRect().width);
    };
    handle.addEventListener('pointermove', move); handle.addEventListener('pointerup', end); handle.addEventListener('pointercancel', end);
  });
  handle.addEventListener('keydown', (event) => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return;
    event.preventDefault(); persist(cell.getBoundingClientRect().width + (event.key === 'ArrowRight' ? 16 : -16));
  });
  return handle;
}

function makeInlineCellEditor(base, row, column, td) {
  const current = row.values?.[column.property];
  const prop = column.property;
  const isBoolean = current === true || current === false;
  const isNumber = typeof current === 'number';
  const isArray = Array.isArray(current);
  let input;
  if (isBoolean) {
    input = h('input', { type: 'checkbox', class: 'copal-base-inline-editor', 'aria-label': column.label });
    input.checked = !!current;
  } else if (isNumber) {
    input = h('input', { type: 'number', class: 'copal-base-inline-editor', value: current == null ? '' : String(current), 'aria-label': column.label });
  } else if (isArray) {
    input = h('input', { type: 'text', class: 'copal-base-inline-editor', value: (current || []).join(', '), 'aria-label': column.label, placeholder: 'tag1, tag2' });
  } else {
    input = h('input', { type: 'text', class: 'copal-base-inline-editor', value: current == null ? '' : String(current), 'aria-label': column.label });
  }
  const commit = async () => {
    let newValue;
    if (isBoolean) { newValue = input.checked; }
    else if (isNumber) { newValue = input.value.trim() === '' ? null : Number(input.value); }
    else if (isArray) { newValue = input.value.split(',').map((s) => s.trim()).filter(Boolean); }
    else { newValue = input.value; }
    try {
      await api(`/bases/${encodeURIComponent(base.id)}/rows/${encodeURIComponent(row.documentId)}`, {
        method: 'PATCH', body: JSON.stringify({ property: prop, value: newValue, base: row.head }),
      });
      await loadDocuments(false); renderBases();
    } catch (error) { setStatus(error.message, true); }
  };
  const cancel = () => { renderBases(); };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !isBoolean) { e.preventDefault(); commit(); }
    if (e.key === 'Escape') { e.preventDefault(); cancel(); }
    if (e.key === 'Tab') { e.preventDefault(); commit(); }
    e.stopPropagation();
  });
  input.addEventListener('blur', () => { setTimeout(cancel, 150); });
  td.replaceChildren(input);
  requestAnimationFrame(() => { input.focus(); if (input.select) input.select(); });
  return input;
}

async function renderBases() {
  const token = ++state.baseQueryToken;
  const bases = state.docs.filter((doc) => doc.kind === 'base');
  if (!bases.length) {
    state.body.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'No Bases yet' }), h('p', { text: 'Create a Base to query live Redb documents. No sample rows are fabricated.' }), h('button', { class: 'copal-btn primary', text: 'Create Base', onclick: createBaseDocument })));
    return;
  }
  let base = bases.find((doc) => doc.id === state.baseId) || bases[0]; state.baseId = base.id;
  const shell = h('div', { class: 'copal-bases-workspace' });
  const browser = h('aside', { class: 'copal-pane copal-base-browser' }, h('div', { class: 'copal-pane-header' }, h('span', { text: 'Bases' }), h('button', { class: 'copal-btn', text: 'Trash', title: 'Restore deleted Bases', onclick: () => showTrash('base') }), h('button', { class: 'copal-btn', text: '+', title: 'Create Base', 'aria-label': 'Create Base', onclick: createBaseDocument })));
  const list = h('div', { class: 'copal-scroll' });
  for (const item of bases) list.append(h('button', { class: `copal-doc-row${item.id === base.id ? ' active' : ''}`, 'data-base-id': item.id, text: item.name, onclick: () => { state.baseId = item.id; state.baseView = null; state.basePage = 1; renderBases(); } }));
  browser.append(list);
  const main = h('section', { class: 'copal-pane copal-base-main' }, h('div', { class: 'copal-empty', text: 'Querying live Redb documents…' }));
  shell.append(browser, main); state.body.replaceChildren(shell);
  try {
    const viewParam = state.baseView ? `&view=${encodeURIComponent(state.baseView)}` : '';
    const pageSize = state.basePageSize || 100;
    const result = await api(`/bases/${encodeURIComponent(base.id)}/query?page=${state.basePage}&page_size=${pageSize}${viewParam}`);
    if (token !== state.baseQueryToken) return;
    state.baseDefinition = result.definition;
    // D1: Track source documents for scoped invalidation
    const sourceDocs = new Set();
    for (const row of result.rows) sourceDocs.add(row.documentId);
    state.baseSourceDocs.set(base.id, sourceDocs);
    const view = result.view; state.baseView = view.id;
    const toolbar = h('div', { class: 'copal-base-toolbar' }, h('strong', { text: base.name }));
    const viewSelect = h('select', { 'aria-label': 'Base view' });
    for (const item of result.definition.views) viewSelect.append(h('option', { value: item.id, text: item.name, selected: item.id === view.id }));
    viewSelect.value = view.id;
    viewSelect.addEventListener('change', () => { state.baseView = viewSelect.value; state.basePage = 1; renderBases(); });
    toolbar.append(viewSelect,
      h('span', { class: 'copal-base-count', text: `${result.total} result${result.total === 1 ? '' : 's'} from ${result.sourceCount} documents` }),
      h('button', { class: 'copal-btn', text: 'Configure', onclick: () => configureBase(base, result.definition, view.id) }),
      h('button', { class: 'copal-btn', text: '+ View', onclick: () => addBaseView(base, result.definition) }),
      h('button', { class: 'copal-btn', text: 'Rename', onclick: () => renameBaseDocument(base) }),
      h('button', { class: 'copal-btn', text: 'Duplicate', onclick: () => duplicateBaseDocument(base) }),
      h('button', { class: 'copal-btn', text: 'History', onclick: () => showHistory(base) }),
      h('button', { class: 'copal-btn', text: '↑', title: 'Move view left', disabled: result.definition.views.findIndex((v) => v.id === view.id) <= 0, onclick: async () => {
        const reordered = reorderBaseView(result.definition, view.id, 'up');
        if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; renderBases(); }
      } }),
      h('button', { class: 'copal-btn', text: '↓', title: 'Move view right', disabled: result.definition.views.findIndex((v) => v.id === view.id) >= result.definition.views.length - 1, onclick: async () => {
        const reordered = reorderBaseView(result.definition, view.id, 'down');
        if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; renderBases(); }
      } }),
      h('button', { class: 'copal-btn danger', text: '× View', title: 'Delete this view', disabled: result.definition.views.length <= 1, onclick: async () => {
        if (!window.confirm(`Delete view "${'$'}{view.name}"?`)) return;
        const removed = removeBaseView(result.definition, view.id);
        if (removed && await saveDocument(base, serializeBase(removed), false)) { state.baseDefinition = removed; state.baseView = null; renderBases(); }
      } }),
      h('button', { class: 'copal-btn danger', text: 'Trash', onclick: () => deleteDocument(base) }));
    const messages = h('div');
    for (const diagnostic of result.diagnostics || []) messages.append(h('p', { class: 'copal-base-diagnostic', text: diagnostic.message }));
    if (result.sourceTruncated) messages.append(h('p', { class: 'copal-base-diagnostic error', text: 'Source scan reached the 5,000-document safety limit.' }));
    if (!result.rows.length) {
      main.replaceChildren(toolbar, messages, h('div', { class: 'copal-empty', text: 'This live query returned no rows. Adjust its filters or add matching document properties.' }));
      return;
    }
    const renderTableRow = (row) => {
        const tr = h('tr', { 'data-document-id': row.documentId });
        for (const column of view.columns) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const td = h('td', {});
          td.append(h('button', {
            class: 'copal-base-cell', type: 'button', text: value,
            title: editable ? `Edit ${column.property}` : `Open ${row.name}`,
            'aria-label': editable ? `Edit ${column.label} for ${row.name}: ${value}` : `Open ${row.name}: ${value}`,
            onclick: () => { if (editable) makeInlineCellEditor(base, row, column, td); else openDocument(row.documentId, 'notes'); },
          }));
          tr.append(td);
        }
        return tr;
      };
    let contentWrap;
    if (view.type === 'card') {
      contentWrap = h('div', { class: 'copal-base-card-view' });
      const allRows = result.groups?.length ? result.groups.flatMap((g) => g.rows) : result.rows;
      for (const row of allRows) {
        const card = h('div', { class: 'copal-base-card', 'data-document-id': row.documentId });
        for (const column of view.columns) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const field = h('div', { class: 'copal-base-card-field' }, h('span', { class: 'copal-base-card-label', text: column.label }));
          const valueEl = h('span', { class: 'copal-base-card-value', text: value });
          if (editable) {
            valueEl.style.cursor = 'pointer';
            valueEl.addEventListener('click', () => {
              const td = h('td', {});
              makeInlineCellEditor(base, row, column, td);
              valueEl.replaceChildren(...td.childNodes);
            });
          } else {
            valueEl.addEventListener('click', () => openDocument(row.documentId, 'notes'));
          }
          field.append(valueEl);
          card.append(field);
        }
        contentWrap.append(card);
      }
    } else if (view.type === 'list') {
      contentWrap = h('div', { class: 'copal-base-list-view' });
      const allRows = result.groups?.length ? result.groups.flatMap((g) => g.rows) : result.rows;
      for (const row of allRows) {
        const item = h('div', { class: 'copal-base-list-item', 'data-document-id': row.documentId });
        const mainCol = view.columns[0];
        const title = h('span', { class: 'copal-base-list-title', text: mainCol ? formatBaseCell(row.values?.[mainCol.property]) : row.name });
        title.addEventListener('click', () => openDocument(row.documentId, 'notes'));
        item.append(title);
        for (const column of view.columns.slice(1)) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          const meta = h('span', { class: 'copal-base-list-meta', text: `${column.label}: ${value}` });
          if (editable) {
            meta.style.cursor = 'pointer';
            meta.addEventListener('click', () => {
              const td = h('td', {});
              makeInlineCellEditor(base, row, column, td);
              meta.replaceChildren(...td.childNodes);
            });
          }
          item.append(meta);
        }
        contentWrap.append(item);
      }
    } else {
      const tableWrap = h('div', { class: 'copal-base-table-wrap' });
      const table = h('table', { class: 'copal-table copal-base-table' });
      const headRow = h('tr');
      for (let colIdx = 0; colIdx < view.columns.length; colIdx++) {
        const column = view.columns[colIdx];
        const sortIndex = (view.sorts || []).findIndex((sort) => sort.property === column.property);
        const sort = sortIndex >= 0 ? view.sorts[sortIndex] : null;
        const cell = h('th', { style: column.width ? `width:${column.width}px` : '' }, h('button', {
          class: 'copal-base-sort', type: 'button',
          'aria-label': `Sort by ${column.label}${sort ? `, ${sort.direction}, priority ${sortIndex + 1}` : ''}`,
          text: `${column.label}${sort ? ` ${sort.direction === 'asc' ? '\u2191' : '\u2193'}${view.sorts.length > 1 ? sortIndex + 1 : ''}` : ''}`,
          onclick: async (event) => {
            const next = updateViewSort(result.definition, view.id, column.property, event.shiftKey);
            if (await saveDocument(base, serializeBase(next), false)) { state.baseDefinition = next; state.basePage = 1; renderBases(); }
          },
        }));
        // D4: Column drag-reorder
        cell.draggable = true;
        cell.addEventListener('dragstart', (e) => { e.dataTransfer.setData('text/plain', String(colIdx)); e.dataTransfer.effectAllowed = 'move'; cell.classList.add('copal-base-col-dragging'); });
        cell.addEventListener('dragend', () => { cell.classList.remove('copal-base-col-dragging'); });
        cell.addEventListener('dragover', (e) => { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; cell.classList.add('copal-base-col-drop'); });
        cell.addEventListener('dragleave', () => { cell.classList.remove('copal-base-col-drop'); });
        cell.addEventListener('drop', async (e) => {
          e.preventDefault(); cell.classList.remove('copal-base-col-drop');
          const fromIdx = Number(e.dataTransfer.getData('text/plain'));
          if (Number.isNaN(fromIdx) || fromIdx === colIdx) return;
          const reordered = reorderBaseColumn(result.definition, view.id, fromIdx, colIdx);
          if (reordered && await saveDocument(base, serializeBase(reordered), false)) { state.baseDefinition = reordered; renderBases(); }
        });
        cell.append(makeBaseColumnResizer(base, result.definition, view.id, column.property, cell));
        headRow.append(cell);
      }
      table.append(h('thead', {}, headRow));
      const body = h('tbody');
      const appendRows = (rows, group = null) => {
        if (group != null) body.append(h('tr', { class: 'copal-base-group' }, h('th', { colspan: String(view.columns.length), text: `${view.groupBy}: ${group}` })));
        for (const row of rows) body.append(renderTableRow(row));
      };
      if (result.groups?.length) for (const group of result.groups) appendRows(group.rows, group.key); else appendRows(result.rows);
      table.append(body);
      if (Object.keys(result.summaries || {}).length) {
        const footer = h('tr');
        for (const column of view.columns) footer.append(h('td', { text: result.summaries[column.property] == null ? '' : `${view.summaries[column.property]}: ${formatBaseCell(result.summaries[column.property])}` }));
        table.append(h('tfoot', {}, footer));
      }
      tableWrap.append(table);
      contentWrap = tableWrap;

      // D3: Keyboard grid navigation
      const totalRows = body.querySelectorAll('tr[data-document-id]').length;
      const totalCols = view.columns.length;
      const setActiveCell = (row, col) => {
        table.querySelectorAll('.copal-base-active-cell').forEach((el) => el.classList.remove('copal-base-active-cell'));
        if (row < 0 || row >= totalRows || col < 0 || col >= totalCols) return;
        const tr = body.querySelectorAll('tr[data-document-id]')[row];
        if (!tr) return;
        const td = tr.querySelectorAll('td')[col];
        if (td) { td.classList.add('copal-base-active-cell'); td.scrollIntoView({ block: 'nearest' }); }
        state.baseFocusRow = row; state.baseFocusCol = col;
      };
      table.addEventListener('click', (e) => {
        const td = e.target.closest('td');
        if (!td) return;
        const tr = td.closest('tr[data-document-id]');
        if (!tr) return;
        const rowIdx = [...body.querySelectorAll('tr[data-document-id]')].indexOf(tr);
        const colIdx = [...tr.querySelectorAll('td')].indexOf(td);
        if (rowIdx >= 0 && colIdx >= 0) setActiveCell(rowIdx, colIdx);
      });
      table.addEventListener('keydown', (e) => {
        const key = e.key;
        if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Enter', 'Escape', 'Tab'].includes(key)) return;
        const r = state.baseFocusRow, c = state.baseFocusCol;
        if (key === 'ArrowUp') { e.preventDefault(); setActiveCell(Math.max(0, r - 1), c); }
        else if (key === 'ArrowDown') { e.preventDefault(); setActiveCell(Math.min(totalRows - 1, r + 1), c); }
        else if (key === 'ArrowLeft') { e.preventDefault(); setActiveCell(r, Math.max(0, c - 1)); }
        else if (key === 'ArrowRight') { e.preventDefault(); setActiveCell(r, Math.min(totalCols - 1, c + 1)); }
        else if (key === 'Tab') { e.preventDefault(); const next = e.shiftKey ? c - 1 : c + 1; if (next >= 0 && next < totalCols) setActiveCell(r, next); }
        else if (key === 'Enter') {
          e.preventDefault();
          const tr = body.querySelectorAll('tr[data-document-id]')[r];
          if (!tr) return;
          const td = tr.querySelectorAll('td')[c];
          if (!td) return;
          const btn = td.querySelector('.copal-base-cell');
          if (btn) btn.click();
        }
        else if (key === 'Escape') { e.preventDefault(); setActiveCell(-1, -1); }
      });
    }
    const pageSizeSelect = h('select', { 'aria-label': 'Page size', class: 'copal-base-page-size' });
    for (const size of [25, 50, 100, 200, 500]) pageSizeSelect.append(h('option', { value: String(size), text: `${'$'}{size}/page`, selected: size === pageSize }));
    pageSizeSelect.addEventListener('change', () => { state.basePageSize = Number(pageSizeSelect.value); state.basePage = 1; renderBases(); });
    const pagination = h('nav', { class: 'copal-base-pagination', 'aria-label': 'Base result pages' },
      h('button', { class: 'copal-btn', text: 'Previous', disabled: result.page <= 1, onclick: () => { state.basePage = Math.max(1, result.page - 1); renderBases(); } }),
      h('span', { text: `Page ${result.page} of ${result.pages}` }),
      h('button', { class: 'copal-btn', text: 'Next', disabled: result.page >= result.pages, onclick: () => { state.basePage = Math.min(result.pages, result.page + 1); renderBases(); } }),
      pageSizeSelect);
    main.replaceChildren(toolbar, messages, contentWrap, pagination);
  } catch (error) {
    if (token !== state.baseQueryToken) return;
    main.replaceChildren(h('div', { class: 'copal-empty' }, h('h2', { text: 'Base query failed' }), h('p', { text: error.message }), h('button', { class: 'copal-btn', text: 'Edit Base definition', onclick: () => openDocument(base.id, 'notes') })));
  }
}

function taskItems() {
  const items = [];
  for (const doc of visibleDocs()) for (const task of doc.tasks || []) items.push({ source: 'markdown', doc, task, label: doc.name });
  return items;
}

const treeHouse = createTreeHouseFeature({ h, api, setStatus, renderMarkdown, openDocument });
planningFeature = createPlanningFeature({
  h,
  api,
  getPlanning: planningData,
  refresh: () => loadDocuments(true),
  setStatus,
  projectionChanged,
  openDocument,
});
notesFeature = createNotesFeature({
  h,
  api,
  state,
  createMarkdownEditor,
  renderMarkdown,
  formatBaseCell,
  saveDocument,
  renameNote,
  deleteDocument,
  deleteDocuments,
  showHistory,
  showTrash,
  showForm,
  importVault,
  loadDocuments,
  openDocument,
  persistActiveContext,
  activateNotes:() => activateView('notes'),
  renderTimeline:(body) => planningFeature.renderTimeline(body),
  openEventEditor:(eventId) => planningFeature.openEventEditor(eventId),
});

function renderTreeHouse() {
  treeHouse.render(state.body);
}

async function toggleTask(item, checked) {
  const doc = state.docs.find((value) => value.id === item.doc.id); const lines = String(doc.text || '').split('\n');
  lines[item.task.line - 1] = lines[item.task.line - 1].replace(/- \[[ xX]\]/, checked ? '- [x]' : '- [ ]');
  await saveDocument(doc, lines.join('\n'), true);
}

function renderTodo() {
  const rows = taskItems().map((item) => {
    const checkbox = h('input', { type:'checkbox', 'aria-label':`Complete ${item.task.text}` }); checkbox.checked = !!item.task.done; checkbox.addEventListener('change', () => toggleTask(item, checkbox.checked));
    return h('label', { class:`copal-task-row${item.task.done ? ' done' : ''}` }, checkbox, h('span', { text:item.task.text }), h('small', { text:item.label }));
  });
  planningFeature.renderTodo(state.body, rows);
}

function showForm(title, fields, submit) {
  const dialog = h('dialog', { class: 'copal-dialog' }, h('h2', { text: title })); const controls = {};
  for (const [name, label, value, type = 'text'] of fields) { const control = h(type === 'textarea' ? 'textarea' : 'input', { name, value: type === 'textarea' ? null : value }); if (type === 'textarea') control.value = value; controls[name] = control; dialog.append(h('label', { text: label }), control); }
  const cancel = h('button', { type: 'button', class: 'copal-btn', text: 'Cancel', onclick: () => dialog.close() });
  const save = h('button', { type: 'button', class: 'copal-btn primary', text: 'Save', onclick: async () => { const values = Object.fromEntries(Object.entries(controls).map(([key, control]) => [key, control.value.trim()])); save.disabled = true; try { await submit(values); dialog.close(); } catch (error) { setStatus(error.message, true); save.disabled = false; } } });
  dialog.append(h('div', { class: 'copal-dialog-actions' }, cancel, save)); wireDialog(dialog); document.body.append(dialog); dialog.showModal(); controls[fields[0][0]]?.focus();
}

function createDocument() {
  const defaultKind = state.view === 'wiki' ? 'wiki' : state.view === 'treehouse' ? 'lesson' : 'note';
  showForm('New Copal document', [['name', 'Name', state.view === 'treehouse' ? 'TreeHouse/New Lesson.md' : 'Untitled'], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
    const result = await api('/documents', { method: 'POST', body: JSON.stringify({ name, kind: defaultKind, content }) });
    state.selected = result.doc.id; await loadDocuments();
  });
}

function importVault() {
  const input = h('input', { type: 'file', accept: '.zip,application/zip' });
  input.style.display = 'none'; document.body.append(input);
  input.addEventListener('change', async () => {
    const file = input.files?.[0]; input.remove();
    if (!file || !window.confirm(`Import ${file.name} into Copal workspace “${state.workspace}”? Existing same-named documents are versioned, not silently replaced.`)) return;
    const form = new FormData(); form.append('file', file);
    const controller = new AbortController();
    const progress = h('dialog', { class:'copal-dialog copal-import-progress' }, h('h2', { text:'Importing Obsidian vault' }), h('p', { text:file.name }), h('progress', { 'aria-label':'Import in progress' }), h('p', { class:'copal-dialog-hint', text:'Copal validates the archive before committing imported records.' }));
    progress.append(h('div', { class:'copal-dialog-actions' }, h('button', { class:'copal-btn', text:'Cancel import', onclick:() => controller.abort() })));
    wireDialog(progress, { dismissable:false }); document.body.append(progress); progress.showModal();
    setStatus('Importing vault…');
    try {
      const result = await api('/import/obsidian', { method: 'POST', body: form, signal:controller.signal });
      for (const projection of result.calendarProjections || []) projectionChanged({ calendar_projection: projection });
      await loadDocuments();
      setStatus(`Imported ${result.imported?.notes || 0} notes · ${result.imported?.assets || 0} assets`);
    } catch (error) { setStatus(error.name === 'AbortError' ? 'Import cancelled before completion' : error.message, error.name !== 'AbortError'); }
    finally { progress.close(); }
  }, { once: true });
  input.addEventListener('cancel', () => input.remove(), { once: true });
  input.click();
}

function renderView(view = state.view) {
  const context = activateView(view);
  if (!context?.window.body) return;
  // renderCalendar intentionally remains in this module as dormant,
  // recoverable boutique plumbing. Odysseus's native Calendar owns the active
  // route/menu and receives Copal events through the backend projector.
  const renderers = { notes: renderNotes, wiki: renderWiki, timeline: renderTimeline, galaxy: renderGalaxy, graph: renderGraph, mind: renderMind, bases: renderBases, treehouse: renderTreeHouse, todo: renderTodo };
  (renderers[view] || renderNotes)();
  persistActiveContext();
}

function ensureViewWindow(view) {
  if (state.windows.has(view)) return state.windows.get(view);
  const modalId = `copal-${view}-modal`;
  const windowApi = createCopalWindow({
    id:modalId,
    label:LABELS[view],
    subtitle:'Copal · canonical Redb',
    minWidth:view === 'timeline' ? 720 : 560,
    minHeight:420,
    sizeKey:`odysseus-copal-${view}-window-size`,
    className:`copal-view-window copal-${view}-window`,
    onActivate:() => {
      if (!state.windows.has(view)) return;
      activateView(view); markActive(); localStorage.setItem('odysseus-copal-view', view);
      if (location.pathname.startsWith('/copal/')) updateRoute(view, true);
    },
    onClosed:() => { if (view === 'notes') notesFeature?.destroy(); markActive(); },
  });
  const search = h('input', { class:'copal-search', type:'search', placeholder:`Search ${LABELS[view]}…`, 'aria-label':`Search ${LABELS[view]}` });
  const context = { view, window:windowApi, search, selected:null, filter:'', story:[], pinned:new Set(), reading:false };
  if (view === 'notes') {
    try {
      const saved = JSON.parse(localStorage.getItem(`odysseus-copal-notes-layout:${state.workspace}`) || '{}');
      notesFeature?.loadSaved(context, saved);
      context.selected = saved.selected || null;
    } catch (_) {}
  }
  state.windows.set(view, context);
  search.addEventListener('input', () => { activateView(view); context.filter = search.value; state.filter = search.value; renderView(view); });
  const activate = (callback) => (...args) => { activateView(view); return callback(...args); };
  if (view === 'notes') {
    windowApi.actions.append(
      h('button', { class:'copal-btn', text:'⌕', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => notesFeature?.showChooser() }),
      h('button', { class:'copal-btn', text:'⌘', title:'Notes commands', 'aria-label':'Notes commands', onclick:() => notesFeature?.showCommands() }),
    );
  } else {
    windowApi.actions.append(search);
    if (['wiki','treehouse'].includes(view)) windowApi.actions.append(h('button', { class:'copal-btn primary', text:'+ New', onclick:activate(createDocument) }));
    windowApi.actions.append(
      h('button', { class:'copal-btn copal-header-secondary', text:'Import', title:'Import Obsidian vault', onclick:activate(importVault) }),
      h('button', { class:'copal-btn copal-header-secondary', text:'Export', title:'Export for Obsidian', onclick:() => { window.location.href = `/api/copal/export/obsidian?workspace=${encodeURIComponent(state.workspace)}`; } }),
      h('button', { class:'copal-btn', text:'↻', title:`Refresh ${LABELS[view]}`, 'aria-label':`Refresh ${LABELS[view]}`, onclick:activate(() => loadDocuments()) }),
    );
  }
  return context;
}

function buildWorkspace() {
  for (const view of VIEWS) ensureViewWindow(view);
}

function bindSidebar() {
  document.querySelectorAll('[data-copal-view]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); open(link.dataset.copalView); }));
  document.getElementById('rail-copal')?.addEventListener('click', () => {
    const view = localStorage.getItem('odysseus-copal-view') || 'notes'; const context = ensureViewWindow(view);
    context.window.visible ? close(view) : open(view);
  });
}

function connectEvents() {
  state.events?.close();
  state.events = new EventSource(`${state.api}/api/copal/events?workspace=${encodeURIComponent(state.workspace)}`);
  // The mutating client refreshes its own document directly. Ignore the
  // matching SSE echo so it cannot overwrite save/conflict feedback.
  const refresh = (event) => {
    if (Date.now() < state.ignoreEventsUntil) return;
    clearTimeout(state.reloadTimer);
    state.reloadTimer = setTimeout(() => {
      if (![...state.windows.values()].some((context) => context.window.visible)) return;
      // D1: Scoped invalidation — check if changed doc is a Base source
      let docId = null;
      try { const data = JSON.parse(event?.data || '{}'); docId = data.id || data.documentId; } catch { /* full reload */ }
      if (state.view === 'bases' && docId && state.baseSourceDocs.size > 0) {
        const affected = [];
        for (const [baseId, sources] of state.baseSourceDocs) {
          if (sources.has(docId) || baseId === docId) affected.push(baseId);
        }
        if (affected.length === 0) return; // not a source for any visible Base
        // Full document reload (needed for other views + selection context) then scoped requery
        loadDocuments().then(() => {
          for (const baseId of affected) {
            if (state.baseId === baseId) { renderBases(); break; }
          }
        });
        return;
      }
      loadDocuments();
    }, 120);
  };
  state.events.addEventListener('document', refresh); state.events.addEventListener('deleted', refresh);
}

export function init(apiBase = window.location.origin) {
  state.api = apiBase;
  state.workspace = localStorage.getItem('odysseus-copal-workspace') || 'default';
  planningFeature.loadState(state.workspace);
  buildWorkspace(); bindSidebar(); connectEvents();
  window.addEventListener('popstate', () => {
    const match = location.pathname.match(/^\/copal(?:\/([^/]+))?\/?$/);
    if (!match) return;
    const view = VIEWS.includes(match[1]) ? match[1] : 'notes'; const context = ensureViewWindow(view);
    context.selected = new URLSearchParams(location.search).get('doc'); open(view, false);
  });
  const match = location.pathname.match(/^\/copal(?:\/([^/]+))?\/?$/);
  if (match) { const view = VIEWS.includes(match[1]) ? match[1] : 'notes'; ensureViewWindow(view).selected = new URLSearchParams(location.search).get('doc'); open(view, false); }
}

export function getNotesSettings() {
  ensureViewWindow('notes');
  return notesFeature?.getSettings() || {};
}

export function updateNotesSettings(patch = {}) {
  ensureViewWindow('notes');
  return notesFeature?.updateSettings(patch) || {};
}

export default { init, open, close, getNotesSettings, updateNotesSettings };
