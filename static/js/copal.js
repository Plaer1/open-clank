import { openCalendar } from './calendar.js';
import { formatBaseCell, makeDefaultBase, serializeBase, updateViewSort } from './copal/bases.js';
import { createMarkdownEditor } from './copal/codemirror.js';
import { createNotesFeature } from './copal/notesFeature.js';
import { createPlanningFeature } from './copal/planning.js';
import { createTreeHouseFeature } from './copal/treehouse.js';
import { createCopalWindow } from './copal/windows.js';

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
  baseId: null, baseView: null, basePage: 1, baseQueryToken: 0, baseDefinition: null,
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

function visibleDocs() {
  const query = state.filter.trim().toLowerCase();
  return state.docs.filter((doc) => {
    if (HIDDEN_KINDS.has(doc.kind)) return false;
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
    let [result, planning] = await Promise.all([api('/documents'), api('/planning')]);
    if (planning.migrationRequired) {
      setStatus('Migrating Timeline events into canonical Redb notes…');
      const migration = await api('/planning/migrate?dry_run=false', { method:'POST', body:JSON.stringify({ action:'apply' }) });
      projectionChanged(migration);
      [result, planning] = await Promise.all([api('/documents'), api('/planning')]);
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
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
  dialog.showModal();
}

async function saveDocument(doc, content, rerender = false, view = state.view) {
  setViewStatus(view, 'Saving…');
  try {
    const result = await api(`/documents/${encodeURIComponent(doc.id)}`, {
      method: 'PUT', body: JSON.stringify({ content, base: doc.head }),
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

function appendMarkdownInline(parent, value) {
  let rest = String(value || '');
  const token = /(!?\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]|`([^`]+)`|\*\*([^*]+)\*\*|~~([^~]+)~~|==([^=]+)==|(?<!\*)\*([^*]+)\*(?!\*)|\[([^\]]+)\]\(([^)\s]+)\)|\$([^$\n]+)\$|(?<![\p{L}\p{N}_])#([A-Za-z0-9_/-]+))/u;
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
    else if (match[12]) parent.append(h('span', { class:'copal-markdown-tag', text:`#${match[12]}` }));
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
        const code = h('code', { 'data-language':fence[1] || '' });
        const pre = h('pre', { class:'copal-markdown-code' }, code);
        const copy = h('button', { type:'button', class:'copal-btn copal-code-copy', text:'Copy', onclick:async() => navigator.clipboard.writeText(code.textContent || '') });
        codeBlock = { code, wrapper:h('figure', { class:'copal-code-block' }, fence[1] ? h('figcaption', { text:fence[1] }) : null, copy, pre) };
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
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
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
  document.body.append(dialog);
  dialog.addEventListener('close', () => dialog.remove());
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
  const docs = visibleDocs().filter((doc) => doc.kind !== 'base' && doc.kind !== 'canvas');
  state.story = state.story.filter((id) => docs.some((doc) => doc.id === id));
  if (state.selected && docs.some((doc) => doc.id === state.selected) && !state.story.includes(state.selected)) state.story.unshift(state.selected);
  if (!state.story.length) state.story = docs.slice(0, 3).map((doc) => doc.id);
  const library = h('aside', { class: 'copal-pane' }, h('div', { class: 'copal-pane-header', text: 'Tiddlers' }));
  const rows = h('div', { class: 'copal-scroll' });
  for (const doc of docs) rows.append(h('button', { class: 'copal-doc-row', onclick: () => { if (!state.story.includes(doc.id)) state.story.push(doc.id); renderWiki(); } }, doc.name));
  library.append(rows);
  const story = h('div', { class: 'copal-story' });
  for (const id of state.story) {
    const doc = state.docs.find((item) => item.id === id);
    if (!doc) continue;
    const card = h('article', { class: `copal-tiddler${state.pinned.has(id) ? ' pinned' : ''}` });
    const move = (delta) => { const index = state.story.indexOf(id); const next = index + delta; if (next < 0 || next >= state.story.length) return; [state.story[index], state.story[next]] = [state.story[next], state.story[index]]; renderWiki(); };
    const editing = state.wikiEditing.has(id);
    card.append(h('header', { class: 'copal-tiddler-head' }, h('strong', { text: doc.name }),
      h('button', { class: 'copal-btn', text: '←', 'aria-label': 'Move left', onclick: () => move(-1) }),
      h('button', { class: 'copal-btn', text: '→', 'aria-label': 'Move right', onclick: () => move(1) }),
      h('button', { class: 'copal-btn', text: state.pinned.has(id) ? 'Unpin' : 'Pin', onclick: () => { state.pinned.has(id) ? state.pinned.delete(id) : state.pinned.add(id); renderWiki(); } }),
      h('button', { class: 'copal-btn', text: 'History', onclick: () => showHistory(doc) }),
      h('button', { class: 'copal-btn', text: editing ? 'Read' : 'Edit', onclick: () => { editing ? state.wikiEditing.delete(id) : state.wikiEditing.add(id); renderWiki(); } }),
      h('button', { class: 'copal-btn', text: '×', 'aria-label': 'Close tiddler', onclick: () => { state.story = state.story.filter((value) => value !== id || state.pinned.has(id)); renderWiki(); } })));
    if (editing) {
      const editor = h('textarea', { class: 'copal-editor copal-wiki-editor', 'aria-label': `Edit ${doc.name}` });
      editor.value = doc.text || '';
      editor.addEventListener('input', () => scheduleSave(doc, editor));
      editor.addEventListener('blur', () => saveDocument(doc, editor.value));
      card.append(editor);
    } else {
      card.append(h('div', { class: 'copal-tiddler-body' }, renderMarkdown(doc.text, new Set([doc.id]))));
    }
    const links = h('footer', { class: 'copal-inspector-section copal-tiddler-links' });
    const incoming = state.docs.filter((candidate) => (candidate.links || []).some((name) => normalizeName(name) === normalizeName(doc.name) || normalizeName(name) === normalizeName(doc.name.split('/').pop())));
    links.append(h('strong', { text: 'Links ' }));
    for (const name of doc.links || []) { const target = findByName(name); links.append(h('button', { class: 'copal-chip', text: `→ ${name}`, onclick: () => target && openDocument(target.id, 'wiki') })); }
    for (const source of incoming) links.append(h('button', { class: 'copal-chip', text: `← ${source.name}`, onclick: () => openDocument(source.id, 'wiki') }));
    if (!(doc.links || []).length && !incoming.length) links.append(h('span', { text: 'None' }));
    card.append(links);
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

function graphSvg(nodes, edges, onOpen) {
  const root = svg('svg', { class: 'copal-graph', viewBox: '0 0 1000 650', role: 'img', 'aria-label': 'Copal relationship graph' });
  const positions = new Map();
  nodes.forEach((node, index) => { const angle = index / Math.max(1, nodes.length) * Math.PI * 2; const ring = 190 + (index % 3) * 45; positions.set(node.id, { x: 500 + Math.cos(angle) * ring, y: 325 + Math.sin(angle) * ring }); });
  for (const edge of edges) { const from = positions.get(edge.from); const to = positions.get(edge.to); if (from && to) root.append(svg('line', { x1: from.x, y1: from.y, x2: to.x, y2: to.y })); }
  for (const node of nodes) {
    const pos = positions.get(node.id); const group = svg('g'); const circle = svg('circle', { cx: pos.x, cy: pos.y, r: node.hub ? 22 : 15, tabindex: '0', role: 'button' });
    circle.addEventListener('click', () => onOpen(node)); circle.addEventListener('keydown', (event) => { if (event.key === 'Enter') onOpen(node); });
    const label = svg('text', { x: pos.x + 19, y: pos.y + 3 }); label.textContent = String(node.label || '').slice(0, 34);
    group.append(circle, label); root.append(group);
  }
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
  const docs = visibleDocs(); const nodes = docs.map((doc) => ({ id: doc.id, label: doc.name, doc })); const edges = [];
  for (const doc of docs) for (const link of doc.links || []) { const target = findByName(link); if (target) edges.push({ from: doc.id, to: target.id }); }
  state.body.replaceChildren(graphSvg(nodes, edges, (node) => node.doc && openDocument(node.doc.id, 'notes')));
}

function renderMind() {
  const groups = new Map();
  for (const doc of visibleDocs()) {
    const keys = [doc.frontmatter?.course, doc.frontmatter?.skill, ...(doc.tags || [])].filter(Boolean);
    for (const key of keys) { if (!groups.has(key)) groups.set(key, []); groups.get(key).push(doc); }
  }
  const tree = h('div', { class: 'copal-mind-tree copal-card' }, h('h2', { text: 'Knowledge outline' })); const list = h('ul');
  for (const [group, docs] of [...groups].sort()) list.append(h('li', {}, h('strong', { text: group }), h('ul', {}, docs.map((doc) => h('li', {}, h('button', { class: 'copal-chip', text: doc.name, onclick: () => openDocument(doc.id, 'notes') }))))));
  tree.append(list); state.body.replaceChildren(tree);
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
  document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal(); name.focus(); name.select();
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
  const flattenFilter = (rule) => {
    if (!rule) return [];
    if (rule.and) return rule.and.map((item) => `${item.property} | ${item.operator} | ${JSON.stringify(item.value ?? '')}`);
    if (rule.or) return rule.or.map((item) => `${item.property} | ${item.operator} | ${JSON.stringify(item.value ?? '')}`);
    return rule.property ? [`${rule.property} | ${rule.operator} | ${JSON.stringify(rule.value ?? '')}`] : [];
  };
  filters.value = flattenFilter(view.filters).join('\n');
  const filterMode = h('select'); for (const mode of ['and', 'or']) filterMode.append(h('option', { value: mode, text: mode.toUpperCase(), selected: !!view.filters?.[mode] }));
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
        const rules = parseDesignerLines(filters.value, '|', (property, remainder) => {
          const split = remainder.indexOf('|');
          const operator = (split < 0 ? remainder : remainder.slice(0, split)).trim() || 'eq';
          const rawValue = split < 0 ? '' : remainder.slice(split + 1).trim();
          const rule = { property, operator };
          if (!['exists', 'missing'].includes(operator)) rule.value = parseBaseLiteral(rawValue);
          return rule;
        });
        view.filters = rules.length ? { [filterMode.value]: rules } : null;
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
  document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal(); name.focus();
}

async function addBaseView(base, definition) {
  const name = window.prompt('Name the new Base view:', 'Table 2');
  if (!name?.trim()) return;
  const next = JSON.parse(JSON.stringify(definition));
  const template = JSON.parse(JSON.stringify(next.views[0] || makeDefaultBase().views[0]));
  template.id = `${name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'view'}-${Date.now().toString(36)}`;
  template.name = name.trim();
  next.views.push(template);
  if (await saveDocument(base, serializeBase(next), false)) { state.baseView = template.id; state.baseDefinition = next; renderBases(); }
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

async function editBaseCell(base, row, column) {
  const current = row.values?.[column.property];
  const value = window.prompt(`Set ${column.property} on ${row.name}\nUse JSON for numbers, booleans, or lists.`, current == null ? '' : (typeof current === 'string' ? current : JSON.stringify(current)));
  if (value == null) return;
  try {
    await api(`/bases/${encodeURIComponent(base.id)}/rows/${encodeURIComponent(row.documentId)}`, {
      method: 'PATCH', body: JSON.stringify({ property: column.property, value: parseBaseLiteral(value), base: row.head }),
    });
    await loadDocuments(false); renderBases();
  } catch (error) { setStatus(error.message, true); }
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
    const result = await api(`/bases/${encodeURIComponent(base.id)}/query?page=${state.basePage}&page_size=100${viewParam}`);
    if (token !== state.baseQueryToken) return;
    state.baseDefinition = result.definition;
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
      h('button', { class: 'copal-btn danger', text: 'Trash', onclick: () => deleteDocument(base) }));
    const messages = h('div');
    for (const diagnostic of result.diagnostics || []) messages.append(h('p', { class: 'copal-base-diagnostic', text: diagnostic.message }));
    if (result.sourceTruncated) messages.append(h('p', { class: 'copal-base-diagnostic error', text: 'Source scan reached the 5,000-document safety limit.' }));
    if (!result.rows.length) {
      main.replaceChildren(toolbar, messages, h('div', { class: 'copal-empty', text: 'This live query returned no rows. Adjust its filters or add matching document properties.' }));
      return;
    }
    const tableWrap = h('div', { class: 'copal-base-table-wrap' });
    const table = h('table', { class: 'copal-table copal-base-table' });
    const headRow = h('tr');
    for (const column of view.columns) {
      const sortIndex = (view.sorts || []).findIndex((sort) => sort.property === column.property);
      const sort = sortIndex >= 0 ? view.sorts[sortIndex] : null;
      const cell = h('th', { style: column.width ? `width:${column.width}px` : '' }, h('button', {
        class: 'copal-base-sort', type: 'button',
        'aria-label': `Sort by ${column.label}${sort ? `, ${sort.direction}, priority ${sortIndex + 1}` : ''}`,
        text: `${column.label}${sort ? ` ${sort.direction === 'asc' ? '↑' : '↓'}${view.sorts.length > 1 ? sortIndex + 1 : ''}` : ''}`,
        onclick: async (event) => {
          const next = updateViewSort(result.definition, view.id, column.property, event.shiftKey);
          if (await saveDocument(base, serializeBase(next), false)) { state.baseDefinition = next; state.basePage = 1; renderBases(); }
        },
      }));
      cell.append(makeBaseColumnResizer(base, result.definition, view.id, column.property, cell));
      headRow.append(cell);
    }
    table.append(h('thead', {}, headRow));
    const body = h('tbody');
    const appendRows = (rows, group = null) => {
      if (group != null) body.append(h('tr', { class: 'copal-base-group' }, h('th', { colspan: String(view.columns.length), text: `${view.groupBy}: ${group}` })));
      for (const row of rows) {
        const tr = h('tr', { 'data-document-id': row.documentId });
        for (const column of view.columns) {
          const value = formatBaseCell(row.values?.[column.property]);
          const editable = !column.formula && !column.property.startsWith('file.') && !['tags', 'links', 'kind', 'name'].includes(column.property);
          tr.append(h('td', {}, h('button', {
            class: 'copal-base-cell', type: 'button', text: value,
            title: editable ? `Edit ${column.property}` : `Open ${row.name}`,
            'aria-label': editable ? `Edit ${column.label} for ${row.name}: ${value}` : `Open ${row.name}: ${value}`,
            onclick: () => editable ? editBaseCell(base, row, column) : openDocument(row.documentId, 'notes'),
          })));
        }
        body.append(tr);
      }
    };
    if (result.groups?.length) for (const group of result.groups) appendRows(group.rows, group.key); else appendRows(result.rows);
    table.append(body);
    if (Object.keys(result.summaries || {}).length) {
      const footer = h('tr');
      for (const column of view.columns) footer.append(h('td', { text: result.summaries[column.property] == null ? '' : `${view.summaries[column.property]}: ${formatBaseCell(result.summaries[column.property])}` }));
      table.append(h('tfoot', {}, footer));
    }
    tableWrap.append(table);
    const pagination = h('nav', { class: 'copal-base-pagination', 'aria-label': 'Base result pages' },
      h('button', { class: 'copal-btn', text: 'Previous', disabled: result.page <= 1, onclick: () => { state.basePage = Math.max(1, result.page - 1); renderBases(); } }),
      h('span', { text: `Page ${result.page} of ${result.pages}` }),
      h('button', { class: 'copal-btn', text: 'Next', disabled: result.page >= result.pages, onclick: () => { state.basePage = Math.min(result.pages, result.page + 1); renderBases(); } }));
    main.replaceChildren(toolbar, messages, tableWrap, pagination);
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
});
planningFeature = createPlanningFeature({
  h,
  api,
  getPlanning: planningData,
  refresh: () => loadDocuments(true),
  setStatus,
  projectionChanged,
  openDocument,
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
  dialog.append(h('div', { class: 'copal-dialog-actions' }, cancel, save)); document.body.append(dialog); dialog.addEventListener('close', () => dialog.remove()); dialog.showModal(); controls[fields[0][0]]?.focus();
}

function createDocument() {
  const defaultKind = state.view === 'wiki' ? 'wiki' : state.view === 'treehouse' ? 'lesson' : 'markdown';
  showForm('New Copal document', [['name', 'Name', state.view === 'treehouse' ? 'TreeHouse/New Lesson.md' : 'Untitled.md'], ['content', 'Starting text', '', 'textarea']], async ({ name, content }) => {
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
    document.body.append(progress); progress.addEventListener('close', () => progress.remove()); progress.showModal();
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
      h('button', { class:'copal-btn', text:'☰', title:'Toggle Notes files', 'aria-label':'Toggle Notes files', onclick:() => notesFeature?.toggleLeft() }),
      h('button', { class:'copal-btn', text:'⌕', title:'Quick switcher', 'aria-label':'Quick switcher', onclick:() => notesFeature?.showChooser() }),
      h('button', { class:'copal-btn', text:'⌘', title:'Notes commands', 'aria-label':'Notes commands', onclick:() => notesFeature?.showCommands() }),
      h('button', { class:'copal-btn', text:'◫', title:'Toggle linked views', 'aria-label':'Toggle linked views', onclick:() => notesFeature?.toggleRight() }),
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
  const refresh = () => { if (Date.now() < state.ignoreEventsUntil) return; clearTimeout(state.reloadTimer); state.reloadTimer = setTimeout(() => { if ([...state.windows.values()].some((context) => context.window.visible)) loadDocuments(); }, 120); };
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

export default { init, open, close };
