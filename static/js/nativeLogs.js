import { renderConversation, renderVitals, sessionMeta, identityLabels } from './usageTranscript.js';
// Ordinary archive browser inside the existing Usage window, with bounded pages.
let active = null;
function el(tag, text = '', cls = '') { const node = document.createElement(tag); node.textContent = text; node.className = cls; return node; }
function button(text, action) { const node = el('button', text, 'admin-btn-sm'); node.type = 'button'; node.addEventListener('click', action); return node; }
function input(label, value = '') { const wrap = el('label', '', 'logs-field'); const control = el('input'); control.value = value; control.className = 'settings-select'; wrap.append(el('span', label), control); return { wrap, control }; }
function localZone(value) { return !value || value === 'local' ? Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC' : value; }
function saveFile(name, payload, container) {
  const json = JSON.stringify(payload, null, 2);
  JSON.parse(json); // The download and the visible retry link share these exact bytes.
  container._releaseDownload?.();
  const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
  const link = el('a', `Download ${name}`); link.href = url; link.download = name;
  container.replaceChildren(el('span', 'Export ready. '), link);
  container._releaseDownload = () => { URL.revokeObjectURL(url); container.replaceChildren(); container._releaseDownload = null; };
  // A mounted link remains available for an explicit user click if the browser
  // blocks the automatic download after the asynchronous export request.
  link.click();
}
function openOwnedSession(handle) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      callback(value);
    };
    const fail = error => finish(reject, error instanceof Error ? error : new Error(String(error || 'Session open failed.')));
    const timer = setTimeout(() => fail(new Error('The session bridge did not report whether the conversation opened. Retry open.')), 30000);
    window.dispatchEvent(new CustomEvent('openclank:session-open', { detail: {
      handle,
      resolve: result => result === false ? fail(new Error('The session could not be opened.')) : finish(resolve, result),
      reject: fail,
    } }));
  });
}
function actionUnavailableText(label, availability) {
  const reason = {
    host_session_missing: 'This saved conversation has no host Session record.',
    session_manager_unavailable: 'The host session manager is unavailable.',
  }[availability?.reason] || 'The server did not report this action as available.';
  return `${label} unavailable: ${reason}`;
}

export function disposeNativeLogs() { active?.dispose(); active = null; }
export function mountNativeLogs(root, scope, options = {}) {
  disposeNativeLogs(); scope = { ...scope, filters: { ...scope.filters } }; root.classList.add('native-logs');
  const advanced = options.mode === 'advanced';
  root.classList.toggle('usage-sessions', !advanced);
  if (!document.getElementById('usage-sessions-style')) { const style = document.createElement('link'); style.id = 'usage-sessions-style'; style.rel = 'stylesheet'; style.href = '/static/css/usageSessions.css'; document.head.append(style); }
  const state = { detailGeneration: 0, detailController: null, detailPending: false, generation: 0, controller: null, cursor: null, prior: [], next: null, filters: {}, pinnedFilters: null, selected: null, checks: new Set(), actionAvailability: new Map(), policy: null, sessionPage: null, ready: false, attemptController: null, attemptGeneration: 0, attemptCursor: null, attemptPrior: [], attemptNext: null, disposed: false, actionBusy: false, focusPart: null, focusSession: null, catalogController: null, catalogGeneration: 0 };
  const status = el('p', '', 'logging-status'); status.setAttribute('role', 'status');
  const downloadStatus = el('p', '', 'logging-status'); downloadStatus.setAttribute('role', 'status');
  const catalogStatus = el('p', '', 'logging-status'); catalogStatus.setAttribute('role', 'status');
  const search = input('Search saved message/tool text'); search.control.type = 'search'; search.control.placeholder = 'Search conversations…'; search.control.setAttribute('aria-label', 'Search saved message/tool text');
  const searchMode = el('select', '', 'settings-select'); searchMode.setAttribute('aria-label', 'Search mode');
  for (const [value, label] of [['text', 'Keyword search'], ['semantic', 'Semantic search']]) searchMode.append(Object.assign(el('option', label), { value }));
  const modeLabel = el('label', '', 'logs-field'); modeLabel.append(el('span', 'Search mode'), searchMode);
  const exportSelection = el('input'); exportSelection.type = 'checkbox'; const selectionLabel = el('label', ' Export selected conversation'); selectionLabel.prepend(exportSelection);
  const tool = input('Tool name'); const completion = input('Completion status');
  const toolbar = el('div', '', 'logs-toolbar');
  const follow = el('input'); follow.type = 'checkbox'; follow.checked = true; const followLabel = el('label', ' Follow visible updates'); followLabel.prepend(follow);
  const exportBodies = el('input'); exportBodies.type = 'checkbox'; const exportLabel = el('label', ' Include saved bodies in export'); exportLabel.prepend(exportBodies);
  toolbar.append(search.wrap, modeLabel, tool.wrap, completion.wrap, button('Save filters', () => action(async () => {
    if (!state.policy) throw new Error('Wait for the current logging policy.');
    const saved = { ...state.filters }; delete saved.tool_name; delete saved.status; const current = filters(); for (const field of ['provider_id', 'account_id', 'actual_model', 'workspace_id']) { if (current[field]) saved[field] = current[field]; else delete saved[field]; }
    if (tool.control.value) saved.tool_name = tool.control.value;
    if (completion.control.value) saved.status = completion.control.value;
    const response = await request('/policy', { ...post({ policy: { saved_filters: saved }, expected_revision: state.policy.revision }), method: 'PUT' });
    state.policy = response.policy;
  })), button('Search / filter', () => { invalidateScope(); state.cursor = null; state.prior = []; state.pinnedFilters = null; query(); }), button('Clear source filters', () => { state.filters = {}; scope.filters = {}; const params = new URLSearchParams(location.search); for (const key of ['provider', 'account', 'model', 'log_session']) params.delete(key); history.replaceState(history.state, '', `${location.pathname}?${params}`); for (const select of Object.values(catalogs)) select.value = ''; tool.control.value = ''; completion.control.value = ''; invalidateScope(); state.cursor = null; state.prior = []; state.pinnedFilters = null; query(); }), button('Refresh', () => query()), followLabel, exportLabel, selectionLabel, button('Export this filter', () => action(exportScope)), button('Logging settings', () => { void import('./settings.js').then(module => module.open('logging')); }));
  const catalogs = {}; const catalogLabels = {}; const catalogToolbar = el('div', '', 'logs-toolbar');
  for (const [field, label] of [['provider_id', 'Provider'], ['account_id', 'Account'], ['actual_model', 'Model'], ['workspace_id', 'Workspace']]) { const wrap = el('label', '', 'logs-field'); const labelNode = el('span', label); const control = el('select'); control.className = 'settings-select'; control.setAttribute('aria-label', 'Logs ' + label); control.append(Object.assign(el('option', 'All ' + label.toLowerCase() + 's'), { value: '' })); control.addEventListener('change', () => { state.filters[field] = control.value; const inherited = { provider_id: 'provider', account_id: 'account', actual_model: 'model', workspace_id: 'workspace' }[field]; if (inherited) { scope.filters[inherited] = control.value; const params = new URLSearchParams(location.search); if (control.value) params.set(inherited, control.value); else params.delete(inherited); history.replaceState(history.state, '', `${location.pathname}?${params}`); }  options.onScopeChange?.({ filters: { ...scope.filters, [field]: control.value } }); state.pinnedFilters = null; state.cursor = null; state.prior = []; state.attemptCursor = null; state.attemptPrior = []; invalidateScope(); query().then(loadAttempts); }); wrap.append(labelNode, control); catalogs[field] = control; catalogLabels[field] = { label, node: labelNode }; catalogToolbar.append(wrap); }
  const panels = el('div', '', 'logs-columns'); const sessions = el('section', '', 'logs-sessions'); const detail = el('section', '', 'logs-detail');
  const attempts = el('section', '', 'logs-attempts'); panels.append(sessions, detail);
  const railTools = el('div', '', 'usage-rail-tools');
  const sort = el('select'); sort.setAttribute('aria-label', 'Sort sessions');
  for (const [value,label] of [['latest','Latest'],['messages','Messages'],['tokens','Tokens'],['cost','Cost']]) sort.append(Object.assign(el('option',label),{value}));
  sort.addEventListener('change', () => { state.cursor = null; state.prior = []; query(); });
  const collection = el('select'); collection.setAttribute('aria-label', 'Session collection');
  for (const [value,label] of [['all','All sessions'],['pinned','Pinned'],['archived','Archived']]) collection.append(Object.assign(el('option',label),{value}));
  collection.value = options.collection || 'all'; collection.addEventListener('change', () => { options.collection = collection.value; state.cursor = null; state.prior = []; query(); });
  const filterDrawer = el('details', '', 'usage-session-options'); filterDrawer.append(el('summary','Filters & actions'));
  railTools.append(collection, sort);
  const mobile = button('Sessions', () => { root.classList.toggle('usage-rail-open'); mobile.setAttribute('aria-expanded', String(root.classList.contains('usage-rail-open'))); }); mobile.classList.add('usage-rail-toggle'); mobile.setAttribute('aria-expanded', 'false');
  const rail = el('aside', '', 'usage-session-rail');
  if (advanced) root.replaceChildren(el('p', 'Saved Open Clank conversations and observed tool evidence.', 'settings-hint'), toolbar, catalogToolbar, catalogStatus, status, downloadStatus, panels, attempts);
  else {
    sessions.classList.add('usage-session-list'); detail.classList.add('usage-session-center');
    filterDrawer.append(toolbar, catalogToolbar, catalogStatus, downloadStatus);
    rail.append(search.wrap, railTools, filterDrawer, sessions);
    panels.replaceChildren(rail, detail); root.replaceChildren(mobile, panels, status);
  }
  const initialLinkedSession = new URLSearchParams(location.search).get('log_session');
  function showDashboard({ preserveLink = false } = {}) {
    invalidateDetail(); state.focusPart = null; state.focusSession = null; root.classList.remove('usage-has-session', 'usage-vitals-open');
    panels.querySelector('.usage-session-vitals')?.remove();
    if (!preserveLink) { const params = new URLSearchParams(location.search); params.delete('log_session'); history.replaceState(history.state, '', `${location.pathname}?${params}`); }
    if (!preserveLink) options.onSessionChange?.(null);
    if (options.renderDashboard) options.renderDashboard(detail); else detail.append(el('p', 'Select a conversation to read its saved messages.', 'usage-empty'));
  }
  if (!advanced) showDashboard({ preserveLink: true });
  async function request(path, options = {}, signal = null) {
    const response = await fetch('/api/logging/v1' + path, { credentials: 'same-origin', signal, ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.detail?.message || result.detail?.error || result.detail || `Log request failed (${response.status})`);
    return result;
  }
  function invalidateDetail() {
    state.detailGeneration++; state.detailController?.abort(); state.detailController = null;
    state.detailPending = false; state.selected = null; detail.replaceChildren();
  }
  function invalidateScope() {
    state.generation++; state.controller?.abort(); state.attemptGeneration++; state.attemptController?.abort();
    invalidateDetail(); sessions.replaceChildren(); attempts.replaceChildren(); if (!advanced) showDashboard();
  }
  async function detailRequest(path) {
    // Transcript/inspector selection owns this lane. List refreshes never
    // cancel it or advance its generation; scope/disposal invalidates both.
    state.detailController?.abort(); const controller = new AbortController(); state.detailController = controller;
    const generation = ++state.detailGeneration; state.detailPending = true;
    try {
      const data = await request(path, {}, controller.signal);
      return generation === state.detailGeneration && root.isConnected && !state.disposed ? { data, generation } : null;
    } finally { if (generation === state.detailGeneration) state.detailPending = false; }
  }
  const post = value => ({ method: 'POST', body: JSON.stringify(value) });
  function getQuery(values) { const copy = { ...values }; for (const key of ['start', 'end']) if (typeof copy[key] === 'number') copy[key] = new Date(copy[key]).toISOString(); return new URLSearchParams(copy); }
  function filters() {
    if (state.pinnedFilters) return state.pinnedFilters;
    const result = { ...state.filters, timezone: localZone(scope.timezone) };
    if (scope.start != null && scope.end != null) { result.start = scope.start; result.end = scope.end; }
    else result.period = ['7d','30d','90d','all'].includes(scope.range) ? scope.range : 'all';
    for (const [key,field] of [['provider','provider_id'],['account','account_id'],['model','actual_model'],['workspace','workspace_id']]) {
      const value = scope.filters?.[field] || scope.filters?.[key]; if (value) result[field] = value;
    }
    delete result.tool_name; delete result.status;
    if (tool.control.value) result.tool_name = tool.control.value;
    if (completion.control.value) result.status = completion.control.value;
    return result;
  }
  async function action(callback) { if (state.actionBusy) return; state.actionBusy = true; status.textContent = 'Working…'; try { await callback(); status.textContent = ''; } catch (error) { if (error.name !== 'AbortError') status.textContent = String(error.message) + ' Refresh to review current evidence.'; } finally { state.actionBusy = false; } }
  async function query() {
    state.controller?.abort(); const controller = new AbortController(); state.controller = controller; const generation = ++state.generation; status.textContent = 'Loading logs…';
    try {
      const currentFilters = filters(); const text = search.control.value;
      const params = getQuery({ ...currentFilters, limit: '50', sort: sort.value, collection: options.collection || 'all' }); if (state.cursor) params.set('cursor', state.cursor);
      const data = text ? await request('/search', post({ mode: searchMode.value, query: text, filters: currentFilters, cursor: state.cursor, limit: 50 }), controller.signal) : await request('/sessions?' + params, {}, controller.signal);
      if (generation !== state.generation || !root.isConnected || state.disposed) return;
      state.next = data.page?.next_cursor; state.pinnedFilters = data.filters || currentFilters; state.sessionPage = data;
      status.textContent = `${data.page?.total ?? data.items?.length ?? 0} ${text ? 'matches' : 'sessions'}${data.coverage?.state && data.coverage.state !== 'reported' ? ' · ' + data.coverage.state : ''}${data.coverage?.reason ? ' · ' + data.coverage.reason : ''}`;
      if (text) { renderParts(data, sessions, null); if (data.coverage?.setup) {
        const setup = data.coverage.setup;
        sessions.append(el('p', setup.message, 'settings-hint'), button(setup.label || 'Search setup', () => { void import('./settings.js').then(module => module.open(setup.settings_tab || 'services')); }));
        if (setup.build_path) {
          const build = button(setup.build_label || 'Build conversation index', async () => {
            build.disabled = true;
            status.textContent = 'Building with your selected embeddings model… Refresh to review the durable generation state.';
            try {
              const result = await request(setup.build_path, post({ publish: true }));
              state.cursor = null; state.prior = [];
              await query();
              if (result.state !== 'ready') status.textContent = `Generation ${result.state}: ${result.reason || 'review selected embedding binding and retry'}`;
            } catch (error) { status.textContent = String(error.message); }
            finally { build.disabled = false; }
          });
          sessions.append(build);
        }
      } } else renderSessions(data);
      if (text && searchMode.value === 'semantic') {
        const readiness = data.coverage?.readiness || {};
        sessions.append(el('p', `Conversation index: ${readiness.health || 'unavailable'}${readiness.model ? ' · model ' + readiness.model : ''}${readiness.latest_attempt ? ' · latest generation ' + readiness.latest_attempt.build_state : ''}${readiness.latest_attempt?.failure?.reason ? ' · ' + readiness.latest_attempt.failure.reason : ''}`, 'settings-hint'));
      }
      renderPaging(sessions, () => query());
      if (state.selected && !text && !state.detailPending) {
        const scroll = detail.querySelector('.logs-transcript')?.scrollTop || 0;
        await openSession(state.selected, null, true);
        const transcript = detail.querySelector('.logs-transcript'); if (transcript) transcript.scrollTop = scroll;
      }
    } catch (error) { if (generation === state.generation && error.name !== 'AbortError') status.textContent = String(error.message) + ' Refresh to retry.'; }
  }
  function renderPaging(container, callback) {
    const nav = el('div', '', 'logs-toolbar');
    if (state.prior.length) nav.append(button('Previous page', () => { state.cursor = state.prior.pop(); callback(); }));
    if (state.next) nav.append(button('Next page', () => { state.prior.push(state.cursor); state.cursor = state.next; callback(); }));
    container.append(nav);
  }
  function renderSessions(data) {
    sessions.replaceChildren(el('h3', 'Saved sessions'));sessions.querySelector('h3').title='Saved conversation archives; the dashboard also counts chats with no archived messages.';
    const batch = el('div', '', 'logs-toolbar usage-session-batch');batch.hidden=!state.checks.size;
    for (const [actionName, label] of [['pin', 'Pin selected'], ['archive', 'Archive selected'], ['restore', 'Restore selected']]) {
      if (!(data.items || []).some(item => item.action_availability?.[actionName]?.available === true)) continue;
      batch.append(button(label, () => action(async () => {
      if (!state.checks.size) throw new Error('Select sessions first.');
      const handles = [...state.checks];
      if (handles.some(handle => state.actionAvailability.get(handle)?.[actionName]?.available !== true)) throw new Error(`Cannot ${actionName} the selection: at least one conversation does not report this action as available.`);
      const response = await request('/sessions/actions', post({ handles, action: actionName }));
      const conflicts = response.items?.filter(item => item.error) || [];
      state.checks.clear(); await query(); if (conflicts.length) throw new Error(`${conflicts.length} selected sessions could not be changed: ${conflicts.map(x => x.error?.message || x.error).join(', ')}`);
      })));
    }
    sessions.append(batch);
    const viewport = el('div', '', 'logs-viewport');
    for (const item of data.items || []) {
      const availability = item.action_availability || {};
      state.actionAvailability.set(item.handle, availability);
      const canBatch = ['pin', 'archive', 'restore'].some(name => availability[name]?.available === true);
      if (!canBatch) state.checks.delete(item.handle);
      const row = el('article', '', 'logs-session-row'); row.dataset.sessionHandle = item.handle; row.classList.toggle('is-selected', state.selected?.handle === item.handle); const selected = el('input'); selected.type = 'checkbox'; selected.checked = state.checks.has(item.handle); selected.disabled = !canBatch; selected.title = canBatch ? '' : 'No batch action is available for this saved conversation.'; selected.setAttribute('aria-label', 'Select ' + item.name); selected.addEventListener('change', () => { selected.checked ? state.checks.add(item.handle) : state.checks.delete(item.handle);batch.hidden=!state.checks.size; });
      const open = button(item.name || 'Saved conversation', () => selectSession(item)); open.classList.add('usage-session-open'); open.title = item.name || 'Saved conversation';
      const title = el('span', item.name || 'Saved conversation', 'usage-session-name');
      const badge = el('span', identityLabels(item.provider_identities).join(' / ') || identityLabels(item.model_identities).join(' / '), 'usage-session-badge');
      open.replaceChildren(title, badge, el('span', sessionMeta(item), 'usage-session-meta'));
      if (item.pinned) open.prepend(el('span','◆','usage-session-pin'));
      row.append(selected, open);
      if (!canBatch) selected.title = actionUnavailableText('Batch changes', availability.pin || availability.archive || availability.restore);
      viewport.append(row);
    }
    if (!data.items?.length) viewport.append(el('p', 'No saved conversations match this range/filter.'));
    sessions.append(viewport);
  }
  function selectSession(item, cursor = null) {
    state.selected = item; root.classList.remove('usage-rail-open'); root.classList.add('usage-has-session'); options.onSessionChange?.(item.handle); for (const row of sessions.querySelectorAll('[data-session-handle]')) row.classList.toggle('is-selected', row.dataset.sessionHandle === item.handle); state.attemptController?.abort(); state.attemptGeneration++;
    const params = new URLSearchParams(location.search); params.set('log_session', item.handle); history.replaceState(history.state, '', `${location.pathname}?${params}`);
    state.attemptCursor = null; state.attemptPrior = [];
    return openSession(item, cursor).then(() => { if (state.selected?.handle === item.handle) return loadAttempts(); }).catch(error => { if (error.name !== 'AbortError' && !state.disposed) status.textContent = error.message; });
  }
  async function openSession(item, cursor = null, background = false, seekPages = 0) {
    if (background && state.detailPending) return;
    const params = getQuery({ ...filters(), limit: '50' }); if (cursor) params.set('cursor', cursor);
    const result = await detailRequest('/sessions/' + encodeURIComponent(item.handle) + '?' + params);
    if (!result || state.selected?.handle !== item.handle) return; const { data } = result;
    if (state.focusPart && state.focusSession === item.handle && !(data.items || []).some(part => part.part_id === state.focusPart) && data.page?.next_cursor && seekPages < 100) return openSession(item, data.page.next_cursor, false, seekPages + 1);
    const session = { ...item, ...(data.session || {}), handle: item.handle };
    state.selected = session; detail.replaceChildren(); const breadcrumb = el('div', '', 'usage-session-heading'); breadcrumb.append(button('‹ Sessions', showDashboard), el('strong', session.name || 'Saved conversation'), button('Session analysis', () => { root.classList.toggle('usage-vitals-open'); })); detail.append(breadcrumb); panels.querySelector('.usage-session-vitals')?.remove(); if (!advanced) { const vitals = renderVitals(session, data); vitals.prepend(button('‹ Conversation', () => root.classList.remove('usage-vitals-open'))); panels.append(vitals); }
    const availability = session.action_availability || {};
    const actions = el('div', '', 'logs-toolbar');
    const mutate = (actionName, extra = {}) => action(async () => {
      await request(`/sessions/${encodeURIComponent(item.handle)}/actions`, post({ action: actionName, ...extra }));
      if (actionName === 'resume') await openOwnedSession(item.handle);
      else await query();
    });
    if (availability.rename?.available === true) { const rename = input('Session name', session.name || ''); actions.append(rename.wrap, button('Rename', () => mutate('rename', { name: rename.control.value }))); }
    else actions.append(el('p', actionUnavailableText('Rename', availability.rename), 'settings-hint'));
    if (availability.pin?.available === true) actions.append(button(session.pinned ? 'Unpin' : 'Pin', () => mutate(session.pinned ? 'unpin' : 'pin')));
    else actions.append(el('p', actionUnavailableText('Pin', availability.pin), 'settings-hint'));
    const lifecycleAction = session.archived ? 'restore' : 'archive';
    if (availability[lifecycleAction]?.available === true) actions.append(button(session.archived ? 'Restore' : 'Archive', () => mutate(lifecycleAction)));
    else actions.append(el('p', actionUnavailableText(session.archived ? 'Restore' : 'Archive', availability[lifecycleAction]), 'settings-hint'));
    if (availability.resume?.available === true) actions.append(button('Resume / open', () => mutate('resume')));
    else actions.append(el('p', actionUnavailableText('Resume / open', availability.resume), 'settings-hint'));
    if (advanced) detail.append(actions); else { const menu = el('details', '', 'usage-session-actions'); menu.append(el('summary','Conversation actions'),actions); detail.append(menu); }
    const actors = el('details'); actors.append(el('summary', 'Observed actor / subagent tree'));
    for (const actor of data.actors?.items || []) actors.append(el('p', `${actor.actor_id} ← ${actor.parent_actor_id || 'root'} · ${actor.status || actor.state || 'observed'}`), button('Filter this actor', () => { state.filters.actor_id = actor.actor_id; state.pinnedFilters = null; state.cursor = null; invalidateScope(); query(); }));
    if (!data.actors?.items?.length) actors.append(el('p', data.actors?.coverage?.reason || 'Actor history is unavailable.'));
    detail.append(actors); actors.classList.add('usage-actor-tree'); renderParts(data, detail, session);
    const pages = el('div', '', 'logs-toolbar'); if (cursor) pages.append(button('Latest/current page', () => selectSession(session))); if (data.page?.next_cursor) pages.append(button('Next transcript page', () => selectSession(session, data.page.next_cursor))); detail.append(pages);
  }
  function renderParts(data, container, session) {
    if (!session && !advanced) { container.replaceChildren(el('h3','Search results')); for (const part of data.items || []) { const handle=part.source_ref?.session_handle; const row=button('',()=>{if(!handle)return;state.focusPart=part.part_id;state.focusSession=handle;selectSession({handle,name:'Matched conversation'});});row.className='usage-search-result';row.disabled=!handle;row.append(el('strong',[part.role||part.part_type,part.created_at_ms?new Date(part.created_at_ms).toLocaleDateString():null].filter(Boolean).join(' · ')),el('span',part.text||part.document||'Saved source record'));container.append(row); } return; }
    if (!session) container.replaceChildren(el('h3', searchMode.value === 'semantic' ? 'Semantic search' : 'Search results'));
    const viewport = renderConversation(data, {
      search: !session,
      onChunk: (part,row) => action(() => partChunk(part,0,row)),
      onWorkspace: handle => { state.filters.workspace_id = handle; state.pinnedFilters = null; state.cursor = null; options.onScopeChange?.({ filters: { ...scope.filters, workspace: handle } }); invalidateScope(); query(); },
      onSourceSession: part => { state.focusPart = part.part_id; state.focusSession = part.source_ref.session_handle; selectSession({ handle: part.source_ref.session_handle, name: 'Source conversation' }); },
    });
    container.append(viewport);
    if (state.focusPart && session && state.focusSession === session.handle) { const match = [...viewport.querySelectorAll('[data-part-id]')].find(node => node.dataset.partId === state.focusPart); if (match) { match.classList.add('usage-search-match'); match.scrollIntoView({ block: 'center' }); state.focusPart = null; } }
    if (data.coverage?.reason) container.append(el('p', data.coverage.reason, 'settings-hint'));
  }
  async function partChunk(part, offset, row) { const result = await request('/parts/get', post({ source_ref: part.source_ref, offset, length: 16384 })); const panel = el('div'); panel.append(el('pre', result.text || '', 'logging-source')); if (result.next_offset != null) panel.append(button('Next body chunk', () => action(() => partChunk(part, result.next_offset, row)))); row.querySelector('[data-body-chunk]')?.remove(); panel.dataset.bodyChunk = ''; row.append(panel); }
  async function exportScope() {
    const pages = []; let cursor = null;
    const selectedFilters = { ...filters() };
    const includeBodies = exportBodies.checked, mode = searchMode.value, queryText = search.control.value;
    if (exportSelection.checked) { if (!state.selected?.handle) throw new Error('Select a conversation before exporting its scope.'); selectedFilters.session_id = state.selected.handle; }
    const semantic = queryText && mode === 'semantic';
    do {
      const result = await request(semantic ? '/search' : '/export', post({ mode, filters: selectedFilters, query: queryText, query_text: queryText || null, include_bodies: includeBodies, cursor, limit: 100 }));
      if (semantic && !includeBodies) for (const item of result.items || []) { delete item.text; delete item.document; }
      pages.push(result); cursor = result.page?.next_cursor;
      if (pages.length >= 100 && cursor) throw new Error('Export exceeds the bounded client window. Narrow the filter and retry.');
    } while (cursor);
    if (!root.isConnected) throw new Error('Logs closed before the export was ready. Reopen Logs and retry.');
    saveFile('open-clank-logs.json', { schema: 'open-clank.logging.export.pages.v1', include_bodies: includeBodies, scope: selectedFilters, search_mode: mode, query: queryText || null, pages }, downloadStatus);
  }
  async function loadAttempts() {
    if (!advanced || state.disposed) return;
    try {
      state.attemptController?.abort(); const controller = new AbortController(); state.attemptController = controller; const generation = ++state.attemptGeneration;
      const bounds = filters(); const params = new URLSearchParams({ limit: '50' }); if (bounds.start != null) params.set('since', new Date(bounds.start).toISOString()); if (bounds.end != null) params.set('until', new Date(bounds.end).toISOString()); if (bounds.session_id || state.selected?.handle) params.set('session', bounds.session_id || state.selected.handle); for (const field of ['provider_id', 'account_id', 'actual_model', 'workspace_id']) if (bounds[field]) params.set(field, bounds[field]); if (state.attemptCursor) params.set('cursor', state.attemptCursor);
      const data = await request('/attempts?' + params, {}, controller.signal); if (generation !== state.attemptGeneration || !root.isConnected) return; attempts.replaceChildren(el('h3', 'Advanced attempt inspector'), el('p', 'Additional capture observations enrich the same operations; they do not add Usage totals. Bodies are excluded until explicitly selected.', 'settings-hint'));
      state.attemptNext = data.next_cursor;
      for (const item of data.items || []) attempts.append(button(`${item.outcome || 'incomplete'} · ${item.capture_state || 'unknown'} · ${item.actual_model || 'captured attempt'}`, () => action(() => inspectAttempt(item.handle))));
      const pages = el('div', '', 'logs-toolbar'); if (state.attemptPrior.length) pages.append(button('Previous captured page', () => { state.attemptCursor = state.attemptPrior.pop(); loadAttempts(); })); if (state.attemptNext) pages.append(button('Next captured page', () => { state.attemptPrior.push(state.attemptCursor); state.attemptCursor = state.attemptNext; loadAttempts(); })); attempts.append(pages, el('p', data.coverage?.state || 'unavailable', 'settings-hint'));
      if (!data.items?.length) attempts.append(el('p', 'No captured attempts in this owner scope. Ordinary logs remain available.'));
    } catch (error) { if (error.name === 'AbortError' || !root.isConnected) return; attempts.replaceChildren(el('h3', 'Advanced attempt inspector'), el('p', 'Advanced capture detail is unavailable: ' + error.message, 'settings-hint')); }
  }
  async function inspectAttempt(handle, includeBodies = false) {
    state.selected = null;
    const result = await detailRequest(`/attempts/${encodeURIComponent(handle)}?include_bodies=${includeBodies}`);
    if (!result) return; const { data, generation } = result; const resource = data.resource; detail.replaceChildren(el('h3', 'Captured provider attempt'), el('p', `${resource.outcome || 'incomplete'} · ${resource.capture_state || 'unknown'}`));
    const tabs = el('div', '', 'logs-toolbar logs-inspector-tabs'); tabs.setAttribute('role', 'tablist');
    const panel = el('section', '', 'logs-inspector-panel'); panel.setAttribute('role', 'tabpanel'); panel.id = `logging-inspector-panel-${generation}`;
    const valueText = value => value == null ? 'Missing · no observation' : typeof value === 'object' ? JSON.stringify(value) : String(value);
    function rows(entries) { const list = el('dl', '', 'logs-facts'); for (const [label, value] of entries) list.append(el('dt', label), el('dd', valueText(value))); return list; }
    function evidence(label, value) { const item = el('details'); item.append(el('summary', label), el('pre', JSON.stringify(value ?? { state: 'missing' }, null, 2), 'logging-source')); return item; }
    const contentCoverage = resource.content_coverage || {};
    const http = resource.http || {}; const measurement = resource.measurement || {};
    const categories = new Map([['request', false], ['response', false], ['events', false]]);
    const exportControls = el('div', '', 'logs-toolbar');
    for (const [key, label] of [['request', 'Request bodies'], ['response', 'Response bodies'], ['events', 'Provider events']]) { const check = el('input'); check.type = 'checkbox'; check.disabled = !includeBodies; check.addEventListener('change', () => categories.set(key, check.checked)); const wrap = el('label', ' ' + label); wrap.prepend(check); exportControls.append(wrap); }
    const views = {
      Overview: () => panel.append(rows([['Provider outcome', resource.outcome], ['Capture state', resource.capture_state], ['HTTP status', http.status], ['Operation', resource.operation_type], ['Requested model', resource.requested_model], ['Actual model', resource.actual_model], ['Dispatch', resource.dispatch_id], ['Dispatch index', resource.dispatch_index], ['Retry of dispatch', resource.retry_of_dispatch_id ?? 'No retry link reported'], ['Identity coverage', resource.identity_coverage], ['Pinned policy revision', resource.policy_revision], ['Capture losses', resource.loss_reasons?.length ? resource.loss_reasons.join(', ') : 'None reported']]), evidence('Timing milestones', resource.timing)),
      HTTP: () => { panel.append(rows([['Method', http.method], ['Endpoint origin', http.endpoint?.origin], ['Endpoint path', http.endpoint?.path], ['Response status', http.status], ['HTTP coverage', http.coverage]]), el('p', 'Headers are sanitized at capture. Redacted values remain marked; missing headers do not imply an empty upstream response.', 'settings-hint')); for (const [key, label] of [['requestHeaders', 'Safe request headers'], ['responseHeaders', 'Safe response headers']]) { panel.append(el('h4', label)); const headers = http[key]; if (Array.isArray(headers) && headers.length) panel.append(rows(headers.map(header => [header.name, header.state === 'redacted' ? 'Redacted' : header.state === 'omitted' ? 'Omitted · capture limit' : Array.isArray(header.values) ? (header.values.length ? header.values.map(value => value === '' ? '(empty value)' : String(value)).join(' · ') : 'Empty · no retained values') : 'Missing · no header value']))); else panel.append(el('p', headers ? 'No retained safe header fields.' : 'Missing · headers were not observed or retained.', 'settings-hint')); } },
      Tokens: () => { panel.append(el('p', `Measurement coverage: ${measurement.coverage || 'missing'} · normalization profile: ${measurement.normalization_profile || 'unavailable'}`, 'settings-hint')); const table = el('table', '', 'logs-metrics'); const heading = el('tr'); for (const label of ['Metric', 'Value', 'Coverage', 'Provenance']) heading.append(el('th', label)); const head = el('thead'); head.append(heading); const body = el('tbody'); const metrics = new Set(['input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_write_tokens', 'reasoning_tokens', ...Object.keys(measurement.metrics || {})]); for (const key of metrics) { const row = el('tr'); const coverage = measurement.metric_coverage?.[key]; const provenance = measurement.provenance?.[key]; const coverageCell = el('td', typeof coverage === 'object' && coverage ? [coverage.state, coverage.coverage].filter(Boolean).join(' · ') || 'Unknown coverage' : valueText(coverage)); const provenanceCell = el('td', typeof provenance === 'object' && provenance ? [provenance.source || 'Unknown source', provenance.normalization_profile].filter(Boolean).join(' · ') : valueText(provenance)); if (coverage != null || provenance != null) provenanceCell.append(evidence('Full metric evidence', { coverage, provenance })); row.append(el('th', key.replaceAll('_', ' ')), el('td', valueText(measurement.metrics?.[key])), coverageCell, provenanceCell); body.append(row); } table.append(head, body); const scroll = el('div', '', 'logs-table-scroll'); scroll.append(table); panel.append(scroll, el('p', 'Reported zero is a measurement. Missing is unknown. Covered totals are reconciled by the shared Usage authority; unqualified overlap stays explicit.', 'settings-hint'), evidence('Measurement loss reasons', measurement.loss_reasons)); },
      Timing: () => panel.append(rows(Object.entries(resource.timing || {})), evidence('Capture losses', resource.loss_reasons), evidence('Measurement losses', measurement.loss_reasons), el('p', 'Only observed milestones are shown. Missing timing is unknown; capture loss does not change the provider outcome.', 'settings-hint')),
      Bodies: () => { panel.append(rows(Object.entries(contentCoverage).map(([category, coverage]) => [category.replaceAll('_', ' '), coverage.state])), el('p', 'Retained, disabled and pruned are historical evidence. Unknown means the capture has no recorded historical preference; missing means enabled content was not retained. Embedded media enabled is a preference, not proof of captured bytes.', 'settings-hint')); if (!includeBodies) { panel.append(el('p', 'Bodies and events have not been requested. Capture preferences and retention determine historical availability.', 'settings-hint'), button('Include sanitized bodies and raw events', () => action(() => inspectAttempt(handle, true)))); return; } for (const body of resource.bodies || []) { const section = el('section', '', 'logs-part'); section.append(el('h4', `${body.category}${body.truncated ? ' · truncated' : ''}`)); let payload; try { payload = JSON.parse(body.text); } catch {} const blocks = payload?.messages || (Array.isArray(payload?.input) ? payload.input : null) || (Array.isArray(payload?.output) ? payload.output : null) || payload?.choices?.map(choice => choice.message).filter(Boolean); if (Array.isArray(blocks)) for (const block of blocks.slice(0, 256)) section.append(el('p', block.role || block.type || 'Observed block', 'settings-hint'), el('pre', typeof block.content === 'string' ? block.content : JSON.stringify(block.content ?? block, null, 2), 'logging-source')); section.append(evidence('Sanitized captured structure / tool arguments', payload ?? body.text)); panel.append(section); } if (!resource.bodies?.length) panel.append(el('p', 'No retained body records. See the historical content coverage above; current settings do not determine historical state.', 'settings-hint')); panel.append(evidence('Bounded raw provider events', resource.events || [])); },
      Source: () => panel.append(evidence('Source JSON', data)),
    };
    function show(label) { panel.replaceChildren(); for (const tab of tabs.children) { const selected = tab.textContent === label; tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1; } panel.setAttribute('aria-labelledby', `logging-inspector-tab-${generation}-${label}`); views[label](); }
    for (const label of Object.keys(views)) { const tab = button(label, () => show(label)); tab.setAttribute('role', 'tab'); tab.id = `logging-inspector-tab-${generation}-${label}`; tab.setAttribute('aria-controls', panel.id); tab.addEventListener('keydown', event => { if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return; event.preventDefault(); const labels = Object.keys(views); let index = labels.indexOf(label); index = event.key === 'Home' ? 0 : event.key === 'End' ? labels.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + labels.length) % labels.length; show(labels[index]); tabs.children[index].focus(); }); tabs.append(tab); }
    exportControls.append(button('Export selected attempt categories', () => { const selected = [...categories].filter(([, chosen]) => chosen).map(([key]) => key); const exported = { ...resource }; delete exported.bodies; delete exported.events; if (selected.some(key => key !== 'events')) exported.bodies = (resource.bodies || []).filter(body => selected.includes(body.category?.startsWith('request') ? 'request' : body.category?.startsWith('response') ? 'response' : 'unselected')); if (selected.includes('events')) exported.events = resource.events || []; saveFile('open-clank-attempt.json', { ...data, resource: exported, export: { scope: { attempt_handle: handle }, categories: ['metadata', ...selected], include_bodies: selected.some(key => key !== 'events') } }, downloadStatus); }));
    detail.append(tabs, panel, exportControls); show('Overview');
  }

  async function loadFilterCatalogs() {
    state.catalogController?.abort(); const controller = new AbortController(); state.catalogController = controller; const generation = ++state.catalogGeneration;
    const current = () => !state.disposed && generation === state.catalogGeneration;
    const failures = [];
    let bounds;
    try {
      bounds = filters();
    } catch (error) {
      bounds = { period: 'all', timezone: localZone(scope.timezone) };
      failures.push('current Usage scope (' + error.message + ')');
    }
    for (const [field, control] of Object.entries(catalogs)) {
      const { label, node } = catalogLabels[field];
      control.disabled = false;
      control.replaceChildren(Object.assign(el('option', 'All ' + label.toLowerCase() + 's'), { value: '' }));
      node.textContent = label;
      control.title = '';
      try {
        const response = await fetch('/api/stats/v1/filters/' + field + '?' + getQuery({ period: bounds.period || 'custom', timezone: bounds.timezone, ...(bounds.start != null ? { start: bounds.start } : {}), ...(bounds.end != null ? { end: bounds.end } : {}) }), { credentials: 'same-origin', signal: controller.signal });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const result = await response.json(); if (!current()) return;
        for (const item of result.choices || []) control.append(Object.assign(el('option', item.label), { value: item.handle }));
        if (bounds[field]) control.value = bounds[field];
      } catch (error) {
        if (!current() || error.name === 'AbortError') return;
        if (bounds[field]) {
          control.append(Object.assign(el('option', 'Saved selection · catalog unavailable'), { value: bounds[field] }));
          control.value = bounds[field];
        }
        node.textContent = `${label} choices unavailable`;
        control.title = `Could not load ${label.toLowerCase()} choices. Retry filter catalogs.`;
        failures.push(label);
      }
    }
    if (!current()) return;
    if (failures.length) {
      catalogStatus.replaceChildren(document.createTextNode(`Could not load ${failures.join(', ')} filter choices. The dimensions remain visible; retry to load choices. `), button('Retry filter catalogs', () => { void loadFilterCatalogs(); }));
    } else catalogStatus.textContent = '';
  }
  async function initialize() {
    await action(async () => { const prefs = await request('/policy'); state.policy = prefs.policy; state.filters = prefs.policy.saved_filters || {}; tool.control.value = state.filters.tool_name || ''; completion.control.value = state.filters.status || ''; follow.checked = prefs.policy.live_refresh; });
    if (!root.isConnected || state.disposed) return;
    await loadFilterCatalogs();
    if (state.disposed) return;
    state.ready = true;
    await query(); const linkedSession = initialLinkedSession; if (linkedSession && linkedSession.length < 2048 && root.isConnected) await selectSession({ handle: linkedSession, name: 'Linked conversation' }); await loadAttempts();
  }
  const refresh = async (newScope, newOptions = {}) => {
    const collectionChanged = newOptions.collection != null && newOptions.collection !== (options.collection || 'all');
    options = { ...options, ...newOptions }; collection.value = options.collection || 'all';
    let changedScope = collectionChanged;
    if (newScope) {
      if (scope.range !== newScope.range || scope.timezone !== newScope.timezone || scope.start !== newScope.start || scope.end !== newScope.end || JSON.stringify(scope.filters) !== JSON.stringify(newScope.filters)) { changedScope = true; invalidateScope(); state.pinnedFilters = null; state.cursor = null; state.prior = []; state.attemptCursor = null; state.attemptPrior = []; }
      scope = { ...newScope, filters: { ...newScope.filters } };
    }
    if (!state.ready || !root.isConnected || document.hidden || state.disposed) return;
    if (changedScope) { state.pinnedFilters = null; state.cursor = null; state.prior = []; await loadFilterCatalogs(); }
    if (changedScope || newOptions.force || follow.checked) { await query(); await loadAttempts(); }
  };
  for (const control of [search.control, tool.control, completion.control]) control.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); invalidateScope(); state.pinnedFilters = null; state.cursor = null; state.prior = []; query(); } });
  const dispose = () => { state.disposed = true; state.catalogGeneration++; state.catalogController?.abort(); downloadStatus._releaseDownload?.(); invalidateDetail(); state.generation++; state.controller?.abort(); state.attemptGeneration++; state.attemptController?.abort(); };
  active = { root, refresh, update: refresh, showDashboard, openSession: handle => selectSession(typeof handle === 'string' ? { handle, name: 'Saved conversation' } : handle), dispose }; initialize(); return active;
}
export function refreshNativeLogs(scope, options = {}) { return active?.refresh(scope, options); }
