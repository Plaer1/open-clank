import { setUiIconText } from './uiIcons.js';
// One owner-scoped Hex library, shared by Settings and Files entry points.
import { getWorkspaceId } from './workspace.js';

const labels = value => String(value || '').split('\n').map(x => x.trim()).filter(Boolean);
const readableScope = value => Array.isArray(value) ? value.join('\n') : String(value || '');
function node(tag, text = '', cls = '') {
  const el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls; return el;
}
const controlIcons = Object.freeze({
  'Refresh':'refresh', 'New General Hex':'add', 'Import file':'download', 'General':'hex',
  'Workspace':'workspace', 'Search':'search', 'Save context tags':'save',
  'Previous workspaces':'back', 'More workspaces':'forward', 'Previous':'back', 'Next':'forward',
  'Disable':'pause', 'Enable':'play', 'History':'restore', 'Export':'upload',
  'Apply to workspace':'workspace', 'Accept reviewed revision':'check',
  'Review changed contract':'preview', 'Activate reviewed contract':'check', 'Promote to General':'hex',
  'Save':'save', 'Cancel':'close', 'Delete':'trash', 'Restore as a new revision':'restore',
  'Newer revisions':'back', 'Older revisions':'forward', 'Close':'close',
  'Apply reviewed candidate':'check', 'Preview candidate':'preview', 'Import pending':'download',
  'Accept and import':'check',
});
function button(text, action, icon = '') {
  const el = node('button', text, 'admin-btn-sm');
  const id = icon || controlIcons[text];
  if (id) setUiIconText(el, id, text); el.type = 'button'; el.addEventListener('click', action); return el;
}
function field(label, type = 'input', value = '') {
  const wrap = node('label', '', 'hex-field'); wrap.append(node('span', label));
  const input = node(type); input.className = 'settings-select'; input.value = value;
  if (type === 'textarea') input.rows = 3;
  wrap.append(input); return { wrap, input };
}
async function api(path, options = {}) {
  const response = await fetch('/api/hexes' + path, { credentials: 'same-origin', ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof value.detail === 'string' ? value.detail : `Hex action failed (${response.status})`);
  return value;
}
const payload = value => ({ method: 'POST', body: JSON.stringify(value) });

export function mountGeneralHexLibrary(root) {
  if (!root || root.dataset.hexMounted) return;
  root.dataset.hexMounted = 'true';
  const state = { entries: [], workspaces: [], project: null, preview: null, pinned: null, offset: 0, projectOffset: 0, projectTotal: 0, projectNext: null, selected: null, projectExplicit: false, activeWorkspaceId: getWorkspaceId(), workspaceInspection: null, generation: 0 };
  const heading = node('h2', 'Hexes'); setUiIconText(heading, 'hex', 'Hexes', 16);
  const lead = node('p', 'Your Workspace contract and your General instructions. Saves take effect at the next turn or operation boundary.', 'settings-hint');
  const status = node('p', '', 'hex-status'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
  const toolbar = node('div', '', 'hex-toolbar');
  const project = node('select'); project.className = 'settings-select'; project.setAttribute('aria-label', 'Authorized FilePolicy workspaces');
  const projectSearch = field('Find a workspace'); projectSearch.input.type = 'search';
  const projectPages = node('div', '', 'hex-toolbar');
  const refresh = button('Refresh', () => run(load));
  const create = button('New General Hex', () => edit());
  const importInput = node('input'); importInput.type = 'file'; importInput.accept = '.json,application/json'; importInput.hidden = true;
  const importButton = button('Import file', () => importInput.click());
  importInput.addEventListener('change', () => run(async () => {
    const file = importInput.files?.[0]; if (!file) return;
    if (file.size > 200000) throw new Error('General Hex imports must be under 200 KB.');
    const document = JSON.parse(await file.text());
    const assisted = document.entry?.authorship === 'agent';
    if (assisted) { importReview(document); return; }
    await api('/import', payload({ document })); importInput.value = ''; await load();
  }));
  toolbar.append(projectSearch.wrap, project, projectPages, refresh, create, importButton, importInput);
  const tabs = node('div', '', 'hex-toolbar');
  const library = node('section', '', 'hex-library'); const workspace = node('section', '', 'hex-workspace'); workspace.hidden = true;
  tabs.append(button('General', () => { library.hidden = false; workspace.hidden = true; }), button('Workspace', () => { library.hidden = true; workspace.hidden = false; }));
  const search = field('Search your General library'); search.input.type = 'search';
  const filter = field('Filter by one tag (does not change context tags)');
  const filters = node('div', '', 'hex-toolbar'); filters.append(search.wrap, filter.wrap, button('Search', () => { state.offset = 0; run(load); }));
  const list = node('div', '', 'hex-entry-list'); const paging = node('div', '', 'hex-toolbar');
  library.append(filters, list, paging);
  const editor = node('section', '', 'hex-editor'); editor.hidden = true;
  const preview = node('section', '', 'hex-preview'); preview.append(node('h3', 'Applicability at the next boundary'));
  const tagHint = node('p', 'Experimental · No tags = global. With tags, any matching workspace or task tag applies.', 'settings-hint');
  const contextFields = node('div', '', 'hex-context-fields');
  const workspaceTags = field('Workspace tags — Experimental', 'textarea');
  const taskTags = field('Task tags — Experimental', 'textarea');
  const saveContext = button('Save context tags', () => run(async () => {
    if (!state.preview) return;
    for (const [kind, record, input] of [['workspace', state.preview.context_tags.workspace_tags, workspaceTags.input], ['task', state.preview.context_tags.task_tags, taskTags.input]]) {
      if (record) await api(`/context-tags/${kind}/${encodeURIComponent(record.context_id)}`, { method: 'PUT', body: JSON.stringify({ tags: labels(input.value), expected_revision: record.revision }) });
    }
    await load();
  }));
  contextFields.append(workspaceTags.wrap, taskTags.wrap, saveContext);
  const explain = node('div'); const pinned = node('div', '', 'hex-pinned');
  preview.append(tagHint, contextFields, explain, pinned);
  const columns = node('div', '', 'hex-columns'); const main = node('div'); main.append(tabs, library, workspace, editor); columns.append(main, preview);
  root.replaceChildren(heading, lead, toolbar, status, columns);

  async function run(action) {
    status.textContent = 'Loading…'; root.setAttribute('aria-busy', 'true');
    try { await action(); status.textContent = ''; }
    catch (error) { status.textContent = error.message + ' Refresh to review the latest state before retrying.'; }
    finally { root.removeAttribute('aria-busy'); }
  }
  function currentTask() { return window.sessionModule?.getCurrentSessionId?.() || null; }
  function contextQuery() {
    const query = new URLSearchParams();
    const selectedProject = state.project && (!state.activeWorkspaceId || state.project.workspace_id === state.activeWorkspaceId)
      ? state.project : null;
    if (selectedProject) query.set('project_id', selectedProject.project_id);
    const workspaceId = state.activeWorkspaceId || selectedProject?.workspace_id || '';
    if (workspaceId) query.set('workspace_id', workspaceId);
    if (currentTask()) query.set('task_id', currentTask());
    return query;
  }
  async function load() {
    const generation = ++state.generation;
    const task = currentTask(); const queryContexts = new URLSearchParams({ offset: String(state.projectOffset), limit: '100' });
    if (task) queryContexts.set('task_id', task);
    if (projectSearch.input.value) queryContexts.set('search', projectSearch.input.value);
    const contexts = await api('/contexts?' + queryContexts);
    if (generation !== state.generation) return;
    state.workspaces = contexts.workspaces || []; state.projectTotal = contexts.total || 0; state.projectNext = contexts.next_offset; state.pinned = contexts.latest_snapshot;
    const selectedWorkspace = state.workspaces.find(x => x.workspace_id === state.activeWorkspaceId) || null;
    state.project = selectedWorkspace?.project_id ? selectedWorkspace : null;
    project.replaceChildren(); const none = node('option', `Authorized workspaces (${state.projectTotal})`); none.value = ''; project.append(none);
    for (const item of state.workspaces) {
      const description = [item.name, item.canonical_root || item.path, item.activation_state].filter(Boolean).join(' · ');
      const option = node('option', description || item.workspace_id); option.value = item.workspace_id; project.append(option);
    }
    project.value = state.activeWorkspaceId || '';
    projectPages.replaceChildren();
    if (state.projectOffset) projectPages.append(button('Previous workspaces', () => { state.projectOffset = Math.max(0, state.projectOffset - 100); run(load); }));
    if (state.projectNext != null) projectPages.append(button('More workspaces', () => { state.projectOffset = state.projectNext; run(load); }));
    projectPages.append(node('span', `${Math.min(state.projectTotal, state.projectOffset + state.workspaces.length)} of ${state.projectTotal}`, 'settings-hint'));
    const query = new URLSearchParams({ limit: '25', offset: String(state.offset) });
    if (search.input.value) query.set('search', search.input.value);
    if (filter.input.value) query.set('tag', filter.input.value);
    const [entries, composition] = await Promise.all([api('/general?' + query), api('/preview?' + contextQuery())]);
    if (generation !== state.generation) return;
    state.entries = entries.entries; state.preview = composition;
    renderEntries(); renderPreview();
    paging.replaceChildren();
    if (state.offset) paging.append(button('Previous', () => { state.offset = Math.max(0, state.offset - 25); run(load); }));
    if (entries.next_offset != null) paging.append(button('Next', () => { state.offset = entries.next_offset; run(load); }));
    const workspaceId = state.activeWorkspaceId || state.project?.workspace_id || '';
    if (workspaceId) {
      const listedWorkspace = state.workspaces.find(item => item.workspace_id === workspaceId);
      if (listedWorkspace?.availability === 'unavailable') {
        state.workspaceInspection = { workspace_id: workspaceId, state: 'unavailable', diagnostics: ['This registered workspace is currently unavailable for inspection.'] };
        workspace.replaceChildren(); renderWorkspace(state.workspaceInspection);
        return;
      }
      const data = await api('/workspace/inspect', payload({ workspace_id: workspaceId }));
      if (generation === state.generation) {
        state.workspaceInspection = data;
        const item = state.workspaces.find(x => x.workspace_id === workspaceId);
        state.project = data.project_id ? { ...(item || {}), project_id: data.project_id, workspace_id: workspaceId } : null;
        renderWorkspace(data);
      }
    } else workspace.replaceChildren(node('p', 'Select a workspace in the chat workspace control to inspect or activate its contract. General Hexes also work without a workspace.', 'settings-hint'));
  }
  project.addEventListener('change', () => { editor.hidden = true; state.projectExplicit = true; state.activeWorkspaceId = project.value; state.project = state.workspaces.find(x => x.workspace_id === state.activeWorkspaceId && x.project_id) || null; run(load); });
  projectSearch.input.addEventListener('keydown', event => { if (event.key === 'Enter') { state.projectOffset = 0; run(load); } });
  function renderEntries() {
    list.replaceChildren();
    if (!state.entries.length) { list.append(node('p', state.offset || search.input.value || filter.input.value ? 'No matching entries on this page.' : 'Your General bin is empty. Save an instruction, import a reviewed file, or promote a selected Workspace Hex.', 'hex-empty')); return; }
    for (const entry of state.entries) {
      const row = node('article', '', 'hex-entry');
      const title = node('button', entry.title, 'admin-btn-sm'); title.type = 'button'; title.addEventListener('click', () => edit(entry)); title.classList.add('hex-entry-title');
      const applicability = !entry.tags.length ? 'Global' : 'Experimental · ' + entry.tags.join(' · ');
      row.append(title, node('p', `${applicability} · revision ${entry.revision} · ${entry.enabled ? 'enabled' : 'disabled'}${entry.accepted ? '' : ' · awaiting acceptance'}`, 'settings-hint'));
      const actions = node('div', '', 'hex-toolbar');
      actions.append(button(entry.enabled ? 'Disable' : 'Enable', () => run(async () => { await api('/general/' + entry.hex_id, { method: 'PATCH', body: JSON.stringify({ expected_revision: entry.revision, enabled: !entry.enabled }) }); await load(); })), button('History', () => run(() => history(entry))), button('Export', () => { const link = node('a'); link.href = `/api/hexes/general/${entry.hex_id}/export?revision=${entry.revision}`; link.download = ''; link.click(); }), button('Apply to workspace', () => application(entry)));
      if (!entry.accepted) actions.append(button('Accept reviewed revision', () => run(async () => { await api(`/general/${entry.hex_id}/accept`, payload({ expected_revision: entry.revision })); await load(); })));
      row.append(actions); list.append(row);
    }
  }
  function renderPreview() {
    const contexts = state.preview.context_tags;
    workspaceTags.input.value = (contexts.workspace_tags?.tags || []).join('\n'); workspaceTags.input.disabled = !contexts.workspace_tags;
    taskTags.input.value = (contexts.task_tags?.tags || []).join('\n'); taskTags.input.disabled = !contexts.task_tags;
    taskTags.input.title = contexts.task_tags ? 'Current chat/task' : 'Open a chat to edit task tags';
    explain.replaceChildren();
    for (const item of state.preview.entries) explain.append(node('p', `${item.title} · r${item.revision} · ${item.state}${item.matching_tags.length ? ' · matched ' + item.matching_tags.join(', ') : item.reason === 'untagged-global' ? ' · global' : ''}`, 'hex-disposition'));
    if (!state.preview.entries.length) explain.append(node('p', 'No General instructions apply until you save one.'));
    if (state.preview.budget.omitted_count) explain.append(node('p', `${state.preview.budget.omitted_count} instructions omitted for the preview budget.`, 'hex-status'));
    if (state.preview.workspace) explain.append(node('p', `Workspace: ${state.preview.workspace.state} · contract ${state.preview.workspace.contract_hash || 'none'}`, 'hex-source'));
    pinned.replaceChildren(node('h3', 'Pinned operation'));
    if (!state.pinned) { pinned.append(node('p', 'No persisted snapshot is available for this chat. Incognito operation snapshots stay in memory.', 'settings-hint')); return; }
    pinned.append(node('p', `Last pinned boundary: ${state.pinned.boundary_id}`, 'hex-source'));
    for (const item of state.pinned.entries) pinned.append(node('p', `${item.title} · r${item.revision} · ${item.state}`, 'hex-disposition'));
    pinned.append(node('p', 'Running work keeps its pinned revisions. Changes above apply to the next boundary. Refresh to inspect the latest pinned operation.', 'settings-hint'));
  }
  function renderWorkspace(data) {
    workspace.replaceChildren(node('h3', 'Workspace contract'), node('p', `${data.state || 'unavailable'} · ${data.contract_hash || 'No contract hash'}`, 'hex-source'));
    if (data.state === 'absent') { workspace.append(node('p', 'No Hexes contract was found in this workspace. General Hexes remain available.', 'settings-hint')); return; }
    if (data.state === 'unavailable') { workspace.append(node('p', (data.diagnostics || []).join('\n') || 'This workspace contract is unavailable.', 'hex-status')); return; }
    const rules = data.contract?.hexes || data.contract?.henxels || [];
    if (!rules.length) workspace.append(node('p', 'This contract has no Hex rules. Review its exact hash before activation.', 'settings-hint'));
    if (data.contract) workspace.append(node('pre', JSON.stringify(data.contract, null, 2), 'hex-source'));
    if (data.state !== 'active' && data.contract_hash) {
      workspace.append(button(data.state === 'drifted' || data.state === 'changed' ? 'Review changed contract' : 'Activate reviewed contract', () => run(async () => {
        const current = await api('/workspace/inspect', payload({ workspace_id: data.workspace_id }));
        if (current.contract_hash !== data.contract_hash) { state.workspaceInspection = current; renderWorkspace(current); throw new Error('The contract changed during review. Review the updated contents before activating.'); }
        await api('/workspace/activate', payload({ workspace_id: data.workspace_id, expected_contract_hash: data.contract_hash }));
        state.projectOffset = 0; await load();
      })));
    }
    const inspectedProjectId = data.project_id;
    const canPromote = !!inspectedProjectId && data.state === 'active';
    rules.forEach((rule, rule_index) => {
      const promote = button('Promote to General', () => run(async () => { const draft = await api('/promote/preview', payload({ project_id: inspectedProjectId, rule_index })); edit(null, draft); }));
      promote.disabled = !canPromote;
      if (!canPromote) promote.title = 'Activate this exact Workspace contract before promotion.';
      const row = node('article', '', 'hex-entry'); row.append(node('p', rule.hex || rule.rule || rule.must || rule.description || rule.henxel || ''), node('p', `in: ${JSON.stringify(rule.in || ['./*'])} · except: ${JSON.stringify(rule.except || [])}`, 'hex-source'), promote);
      workspace.append(row);
    });
  }
  function edit(entry = null, draft = null) {
    state.selected = entry; editor.hidden = false; editor.replaceChildren(node('h3', draft ? 'Review promotion to General' : entry ? 'Edit General Hex' : 'New General Hex'));
    const values = draft || entry || {};
    const title = field('Title', 'input', values.title || ''); const body = field('Declarative instruction', 'textarea', values.body || ''); body.input.rows = 7;
    const tags = field('Tags — Experimental (one label per line)', 'textarea', (values.tags || []).join('\n'));
    const summary = node('p', '', 'settings-hint'); const updateSummary = () => { summary.textContent = labels(tags.input.value).length ? 'Experimental · Any matching workspace or task tag applies.' : 'Global · No tags applies across your account’s contexts.'; }; tags.input.addEventListener('input', updateSummary); updateSummary();
    const enabled = node('input'); enabled.type = 'checkbox'; enabled.checked = values.enabled !== false; const enabledLabel = node('label', ' Enabled for the next boundary'); enabledLabel.prepend(enabled);
    const accept = node('input'); accept.type = 'checkbox'; accept.checked = false; const acceptLabel = node('label', ' I accept this agent-assisted instruction and its scope'); acceptLabel.prepend(accept); acceptLabel.hidden = entry?.authorship !== 'agent';
    if (draft) { editor.append(node('p', draft.scope_warning, 'hex-status'), node('p', 'The original workspace contract remains intact.', 'settings-hint'), node('pre', JSON.stringify(draft.source, null, 2), 'hex-source')); }
    editor.append(title.wrap, body.wrap, tags.wrap, summary, enabledLabel, acceptLabel);
    const actions = node('div', '', 'hex-toolbar'); actions.append(button('Save', () => run(async () => {
      const values = { title: title.input.value, body: body.input.value, tags: labels(tags.input.value), enabled: enabled.checked };
      if (draft) await api('/promote/save', payload({ ...values, draft_id: draft.draft_id, expected_draft_revision: draft.draft_revision }));
      else if (entry) await api('/general/' + entry.hex_id, { method: 'PATCH', body: JSON.stringify({ ...values, expected_revision: entry.revision, accepted: entry.authorship === 'agent' ? accept.checked : true }) });
      else await api('/general', payload(values));
      editor.hidden = true; await load();
    })), button('Cancel', () => { editor.hidden = true; }));
    if (entry) actions.append(button('Delete', () => run(async () => { await api(`/general/${entry.hex_id}?expected_revision=${entry.revision}`, { method: 'DELETE' }); editor.hidden = true; await load(); })));
    editor.append(actions); title.input.focus();
  }
  async function history(entry, offset = 0) {
    const data = await api(`/general/${entry.hex_id}/history?limit=25&offset=${offset}`); editor.hidden = false; editor.replaceChildren(node('h3', 'Revision history · ' + entry.title));
    data.revisions.forEach(revision => { const row = node('article', '', 'hex-entry'); row.append(node('p', `Revision ${revision.revision} · ${revision.created_at}`), node('pre', revision.body, 'hex-source'), button('Restore as a new revision', () => run(async () => { await api(`/general/${entry.hex_id}/restore`, payload({ revision: revision.revision, expected_revision: entry.revision })); editor.hidden = true; await load(); }))); editor.append(row); });
    const pages = node('div', '', 'hex-toolbar');
    if (offset) pages.append(button('Newer revisions', () => run(() => history(entry, Math.max(0, offset - 25)))));
    if (data.next_offset != null) pages.append(button('Older revisions', () => run(() => history(entry, data.next_offset))));
    editor.append(pages);
    editor.append(button('Close', () => { editor.hidden = true; }));
  }
  function application(entry) {
    if (!state.project) { status.textContent = 'Choose your target Workspace contract first.'; return; }
    editor.hidden = false; editor.replaceChildren(node('h3', 'Apply a reviewed copy to Workspace'), node('p', 'General tags are not path rules. Review a static workspace scope. The General entry stays intact.', 'settings-hint'));
    const source = entry.provenance.workspace_source || {};
    const original = source.project_id === state.project.project_id && source.original_body === entry.body;
    const scope = field('Target in scope (one path pattern per line)', 'textarea', original ? readableScope(source.original_in) : '');
    const except = field('Target except scope (one pattern per line)', 'textarea', original ? readableScope(source.original_except) : '');
    const candidate = node('pre', '', 'hex-source'); let draft;
    const applyButton = button('Apply reviewed candidate', () => run(async () => { await api('/apply', payload({ draft_id: draft.draft_id, expected_draft_revision: draft.draft_revision })); editor.hidden = true; await load(); })); applyButton.hidden = true;
    editor.append(scope.wrap, except.wrap, button('Preview candidate', () => run(async () => { draft = await api('/apply/preview', payload({ hex_id: entry.hex_id, revision: entry.revision, project_id: state.project.project_id, scope_in: labels(scope.input.value), scope_except: labels(except.input.value) })); candidate.textContent = draft.candidate_text; applyButton.hidden = false; })), candidate, applyButton, button('Cancel', () => { editor.hidden = true; }));
    for (const input of [scope.input, except.input]) input.addEventListener('input', () => { applyButton.hidden = true; draft = null; });
  }
  function importReview(document) {
    editor.hidden = false; editor.replaceChildren(node('h3', 'Review assisted file import'), node('pre', JSON.stringify(document.entry, null, 2), 'hex-source'), node('p', 'Choose whether this imported instruction is accepted for activation.', 'settings-hint'), button('Import pending', () => run(async () => { await api('/import', payload({ document, accept_assisted: false })); editor.hidden = true; await load(); })), button('Accept and import', () => run(async () => { await api('/import', payload({ document, accept_assisted: true })); editor.hidden = true; await load(); })), button('Cancel', () => { editor.hidden = true; }));
  }
  root.addEventListener('hex-refresh', () => run(load));
  window.addEventListener('workspace-change', event => {
    if (state.projectExplicit) return;
    state.project = null;
    state.activeWorkspaceId = event.detail?.workspaceId || getWorkspaceId();
    editor.hidden = true;
    if (!root.closest('.hidden')) run(load);
  });
  root.addEventListener('hex-context', event => {
    state.activeWorkspaceId = String(event.detail?.workspaceId || '');
    state.project = null;
    state.projectExplicit = !!state.activeWorkspaceId;
    editor.hidden = true;
    workspace.replaceChildren(node('p', 'Loading workspace contract…', 'settings-hint'));
    library.hidden = !!state.activeWorkspaceId;
    workspace.hidden = !state.activeWorkspaceId;
    run(load);
  });
  run(load);
}
