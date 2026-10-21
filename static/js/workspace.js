// static/js/workspace.js
//
// Workspace picker: browse server directories in a draggable modal, choose a
// folder, and show it as a removable pill in the chat input bar. While set, the
// chat request sends `workspace` so the agent's file/shell tools are confined
// to that folder (see routes/chat_routes.py + src/tool_execution.py).

import Storage, { KEYS } from './storage.js';
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';
import { observeCodeIndex } from './copal/codeIndexObserver.js';

const API_BASE = window.location.origin;
// Same folder glyph as the overflow menu item + pill (not an emoji).
const _FOLDER_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
let _modal = null;
let _curPath = '';
let _browseGeneration = 0;
let _displayGeneration = 0;
const _persistQueueBySession = new Map();
let _indexSubscription = null;

async function _indexEvent(subscription, event, keepalive = false) {
  const response = await fetch(
    `${API_BASE}/api/file-policy/workspaces/${encodeURIComponent(subscription.workspaceId)}/index/${event}`,
    { method: 'POST', credentials: 'same-origin', keepalive,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscriber_id: subscription.id, purpose: subscription.purpose || 'agent_workspace',
        ...(subscription.sessionId ? { session_id: subscription.sessionId } : {}) }) },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data?.detail?.message || `Code index unavailable: ${response.status}`);
    error.code = data?.detail?.code || 'index_unavailable';
    error.status = response.status;
    throw error;
  }
  return data.index;
}

function _releaseIndexSubscription() {
  const prior = _indexSubscription;
  _indexSubscription = null;
  if (prior) void _indexEvent(prior, 'release', true).catch(() => {});
}

function _openIndexSubscription(workspaceId, generation) {
  _releaseIndexSubscription();
  if (!workspaceId || window.sessionModule?.isCurrentSessionIncognito?.()) return;
  const subscription = {
    workspaceId, generation, id: crypto.randomUUID(),
    sessionId: window.sessionModule?.getCurrentSessionId?.() || '',
  };
  _indexSubscription = subscription;
  void _indexEvent(subscription, 'open').then(index => {
    if (_indexSubscription !== subscription || generation !== _displayGeneration
        || getWorkspaceId() !== workspaceId) {
      void _indexEvent(subscription, 'release').catch(() => {});
      return;
    }
    window.dispatchEvent(new CustomEvent('workspace-index-status', { detail: { workspaceId, index } }));
  }).catch(error => {
    if (_indexSubscription !== subscription || generation !== _displayGeneration) return;
    window.dispatchEvent(new CustomEvent('workspace-index-status', {
      detail: { workspaceId, index: { lifecycle: error.status === 409 ? 'failed' : 'unavailable',
        error: { code: error.code || 'index_unavailable', message: error.message } } },
    }));
  });
}

window.addEventListener('pagehide', _releaseIndexSubscription);

export function subscribeWorkspaceIndex(workspaceId, {
  purpose = 'app_folder', sessionId = '', isCurrent = () => true, onStatus = () => {}, observe = false, automatic = false,
} = {}) {
  if (!workspaceId || !isCurrent()) return () => {};
  const subscription = { workspaceId, purpose, sessionId, id: crypto.randomUUID() };
  const observer = observeCodeIndex({
    request: event => _indexEvent(subscription, event, event === 'release'),
    isCurrent, onStatus, observe, automatic,
    onDispose: () => window.removeEventListener('pagehide', release),
  });
  const release = () => {
    window.removeEventListener('pagehide', release);
    observer();
  };
  release.refresh = observer.refresh;
  release.verify=observer.verify;release.dirty=observer.dirty;release.setVisible=observer.setVisible;release.configure=observer.configure;
  release.captureSourceChange=observer.captureSourceChange;release.sourceChanged=observer.sourceChanged;
  release.readmit = observer.readmit;
  window.addEventListener('pagehide', release);
  return release;
}
let _browserContext = {
  active: false,
  selectionKind: 'agent_workspace',
  onSelect: null,
  successLabel: '',
};

export function getWorkspace() {
  return getWorkspaceId() ? (Storage.get(KEYS.WORKSPACE, '') || '') : '';
}

export function getWorkspaceId() {
  return Storage.get(KEYS.WORKSPACE_ID, '') || '';
}

function _basename(p) {
  if (!p) return '';
  // Handle both POSIX (/) and Windows (\) separators.
  const parts = p.replace(/[\\/]+$/, '').split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

export function syncWorkspaceIndicator(path) {
  const pill = document.getElementById('workspace-indicator-btn');
  const name = document.getElementById('workspace-indicator-name');
  const overflow = document.getElementById('overflow-workspace-btn');
  if (pill) {
    pill.style.display = path ? '' : 'none';
    pill.classList.toggle('active', !!path);
    if (path) pill.title = `Workspace: ${path}\nFile tools are confined here; shell commands start here but are not sandboxed and can reach outside it.\nClick to clear.`;
  }
  if (name) name.textContent = path ? _basename(path) : '';
  if (overflow) {
    overflow.style.display = '';
    overflow.classList.toggle('active', !!path);
  }
  // Recompute the "+" overflow dot (app.js owns updatePlusDot via this event).
  try { document.dispatchEvent(new CustomEvent('overflow-state-change')); } catch (_) {}
}

// Compatibility hook for callers that refresh the composer state.
export function applyMode(_mode) {
  syncWorkspaceIndicator(getWorkspace());
}

function _applyWorkspaceDisplay(path, workspaceId = '') {
  if (path) Storage.set(KEYS.WORKSPACE, path);
  else Storage.remove(KEYS.WORKSPACE);
  if (path && workspaceId) Storage.set(KEYS.WORKSPACE_ID, workspaceId);
  else Storage.remove(KEYS.WORKSPACE_ID);
  syncWorkspaceIndicator(path || '');
  // Consumers such as Editor and Files can converge on the same
  // workspace selection without reading the raw browser key as authority.
  // The server still re-vets the path on every operation.
  try { window.dispatchEvent(new CustomEvent('workspace-change', { detail: { path: path || '', workspaceId: workspaceId || '' } })); } catch (_) {}
}

async function _persistCurrentChatWorkspace(workspaceId) {
  const sessions = window.sessionModule;
  const sessionId = sessions?.getCurrentSessionId?.();
  if (!sessionId || sessions?.isCurrentSessionIncognito?.()) return;
  // Preserve click order for a single chat. Two quick workspace choices can
  // otherwise finish out of order and leave the server on the older choice.
  const prior = _persistQueueBySession.get(sessionId) || Promise.resolve();
  const request = prior.catch(() => {}).then(async () => {
    const form = new FormData();
    form.append('workspace_id', String(workspaceId || ''));
    const response = await fetch(
      `${API_BASE}/api/session/${encodeURIComponent(sessionId)}`,
      { method: 'PATCH', credentials: 'same-origin', body: form },
    );
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = data?.detail;
      throw new Error(
        (typeof detail === 'string' ? detail : detail?.message)
        || `workspace binding failed: ${response.status}`,
      );
    }
    sessions?.setSessionWorkspaceId?.(sessionId, data.workspace_id || '');
  });
  _persistQueueBySession.set(sessionId, request);
  try {
    await request;
  } finally {
    if (_persistQueueBySession.get(sessionId) === request) {
      _persistQueueBySession.delete(sessionId);
    }
  }
}

export async function setWorkspace(path, workspaceId = '', options = {}) {
  const displayGeneration = ++_displayGeneration;
  const previousPath = getWorkspace();
  const previousId = getWorkspaceId();
  _applyWorkspaceDisplay(path || '', workspaceId || '');
  _releaseIndexSubscription();
  if (options.persist === false) {
    _openIndexSubscription(path ? workspaceId : '', displayGeneration);
    return;
  }
  try {
    await _persistCurrentChatWorkspace(workspaceId || '');
    if (displayGeneration === _displayGeneration) {
      _openIndexSubscription(path ? workspaceId : '', displayGeneration);
    }
  } catch (error) {
    // A session switch or newer choice owns the display now. The failed older
    // request may report its error, but it must never repaint that later chat.
    if (displayGeneration === _displayGeneration) {
      _applyWorkspaceDisplay(previousPath, previousId);
      _openIndexSubscription(previousId, displayGeneration);
    }
    throw error;
  }
}

export async function bindWorkspacePath(path, purpose = 'agent_workspace') {
  const res = await fetch(`${API_BASE}/api/file-policy/workspaces/from-path`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, purpose }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || !data?.workspace?.id || !data?.workspace?.path) {
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    throw new Error(message || `workspace registration failed: ${res.status}`);
  }
  return data.workspace;
}

export async function resolveWorkspaceId(workspaceId, purpose = 'agent_workspace') {
  const response = await fetch(
    `${API_BASE}/api/file-policy/workspaces/${encodeURIComponent(String(workspaceId || ''))}/resolve?purpose=${encodeURIComponent(purpose)}`,
    { credentials: 'same-origin' },
  );
  const data = await response.json().catch(() => ({}));
  if (!response.ok || !data?.workspace?.id || !data?.workspace?.path) {
    const detail = data?.detail;
    const error = new Error(
      (typeof detail === 'string' ? detail : detail?.message)
      || `workspace resolution failed: ${response.status}`,
    );
    error.status = response.status;
    throw error;
  }
  return data.workspace;
}

/**
 * Validate a manually entered path server-side, then persist the canonical
 * form. Returns {ok, path|null}. Without this, a typo / file path / deleted
 * folder / filesystem root would be stored and shown as active while the
 * backend silently refuses to bind it on every send.
 */
export async function vetAndSetWorkspace(path) {
  try {
    const workspace = await bindWorkspacePath(path, 'agent_workspace');
    await setWorkspace(workspace.path, workspace.id);
    return { ok: true, path: workspace.path, workspaceId: workspace.id };
  } catch (e) {
    return { ok: false, path: null, workspaceId: null };
  }
}

export async function clearWorkspace(options = {}) {
  try {
    await setWorkspace('', '', options);
    if (uiModule && uiModule.showToast) uiModule.showToast('Workspace cleared');
  } catch (error) {
    if (uiModule && uiModule.showError) {
      uiModule.showError(error?.message || 'Workspace could not be cleared');
    }
    throw error;
  }
}

async function _load(path, selectionKind) {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  params.set('selection_kind', selectionKind);
  const url = `${API_BASE}/api/workspace/browse?${params.toString()}`;
  const res = await fetch(url, { credentials: 'same-origin' });
  if (!res.ok) throw new Error(`browse failed: ${res.status}`);
  return res.json();
}

function _render(data) {
  _curPath = data.path;
  const body = _modal.querySelector('#workspace-body');
  const pathEl = _modal.querySelector('#workspace-cur-path');
  if (pathEl) {
    // Reflect the resolved (realpath) location back into the editable field.
    pathEl.value = data.path;
    pathEl.title = data.path;
  }
  let rows = '';
  if (data.parent) {
    rows += `<div class="workspace-row workspace-up" data-path="${encodeURIComponent(data.parent)}">↑ ..</div>`;
  }
  for (const d of data.dirs) {
    // Backend supplies the full child path (os.path.join → cross-platform).
    rows += `<div class="workspace-row" data-path="${encodeURIComponent(d.path)}">${_FOLDER_SVG}<span>${uiModule.esc(d.name)}</span></div>`;
  }
  if (data.truncated) {
    rows += '<div class="workspace-empty">Too many folders to list. Type or paste a path above to jump in.</div>';
  }
  if (!data.dirs.length && !data.parent) rows = '<div class="workspace-empty">No subfolders</div>';
  body.innerHTML = rows || '<div class="workspace-empty">No subfolders</div>';
  body.querySelectorAll('.workspace-row').forEach((row) => {
    row.addEventListener('click', () => _navigate(decodeURIComponent(row.dataset.path)));
  });
  // Agent workspaces reject filesystem roots and sensitive directories.
  // App-folder selection instead follows the caller's app-visible scope, so
  // an administrator may deliberately open a filesystem root in Editor.
  const useBtn = _modal.querySelector('#workspace-use');
  if (useBtn) {
    useBtn.disabled = data.selectable === false;
    useBtn.title = data.selectable === false
      ? (_browserContext.selectionKind === 'app_folder'
        ? 'This folder is outside your visible app scope'
        : 'This folder cannot be used as an agent workspace')
      : '';
  }
}

async function _navigate(path, errorMessage = 'Could not open folder') {
  const context = _browserContext;
  if (!context.active) return;
  const generation = ++_browseGeneration;
  const current = () => context === _browserContext && context.active
    && generation === _browseGeneration;
  // A pending/failed request is not a selectable folder. Preserve the user's
  // draft until this exact navigation resolves, not whichever request is last.
  _curPath = '';
  const use = _modal.querySelector('#workspace-use');
  _modal.querySelector('#workspace-cur-path').title = '';
  if (use) { use.disabled = true; use.title = 'Loading folder'; }
  _modal.querySelector('#workspace-body').textContent = 'Loading folder…';
  try {
    const data = await _load(path, context.selectionKind);
    if (current()) _render(data);
  } catch (e) {
    if (!current()) return;
    _modal.querySelector('#workspace-body').textContent = errorMessage;
    if (use) use.title = 'Choose a valid folder before continuing';
    if (uiModule && uiModule.showError) uiModule.showError(errorMessage);
  }
}

function _getModal() {
  if (_modal) return _modal;
  _modal = document.createElement('div');
  _modal.id = 'workspace-modal';
  _modal.className = 'modal';
  _modal.style.display = 'none';
  _modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h4 id="workspace-browser-title"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px;margin-right:6px"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg><span>Select workspace</span></h4>
        <button class="close-btn" id="workspace-close" aria-label="Close">✖</button>
      </div>
      <input type="text" class="styled-prompt-input workspace-cur" id="workspace-cur-path"
             spellcheck="false" autocomplete="off" autocapitalize="off" autocorrect="off"
             placeholder="Type or paste a folder path, then press Enter" />
      <p class="muted workspace-note" id="workspace-browser-note">File tools are <strong>confined</strong> to this folder. Shell commands start here but are <strong>not sandboxed</strong> and can reach outside it. A workspace scopes the tools; it is not a security boundary.</p>
      <div class="modal-body workspace-body" id="workspace-body"></div>
      <div class="modal-footer workspace-footer">
        <button type="button" class="confirm-btn confirm-btn-secondary" id="workspace-cancel">Cancel</button>
        <button type="button" class="confirm-btn confirm-btn-primary" id="workspace-use">Use this folder</button>
      </div>
    </div>`;
  document.body.appendChild(_modal);
  _modal.querySelector('#workspace-close').addEventListener('click', closeWorkspaceBrowser);
  _modal.querySelector('#workspace-cancel').addEventListener('click', closeWorkspaceBrowser);
  _modal.querySelector('#workspace-cur-path').addEventListener('input', () => {
    if (!_browserContext.active) return;
    // Typing is a new intent even before Enter. A late initial browse must not
    // overwrite the draft or allow Use to select the previous resolved path.
    ++_browseGeneration;
    _curPath = '';
    _modal.querySelector('#workspace-cur-path').title = '';
    const use = _modal.querySelector('#workspace-use');
    use.disabled = true;
    use.title = 'Press Enter to check this folder';
    _modal.querySelector('#workspace-body').textContent = 'Press Enter to check this folder.';
  });
  // Editable path bar: Enter navigates to a typed/pasted folder.
  _modal.querySelector('#workspace-cur-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = e.target.value.trim();
      if (v) _navigate(v);
    }
  });
  _modal.querySelector('#workspace-use').addEventListener('click', async () => {
    const context = _browserContext, chosenPath = _curPath;
    const current = () => context === _browserContext && context.active;
    if (!current() || !chosenPath || _modal.querySelector('#workspace-use').disabled) return;
    if (context.selectionKind === 'app_folder' && typeof context.onSelect === 'function') {
      try {
        await context.onSelect(chosenPath);
        if (!current()) return;
        if (uiModule && uiModule.showToast) uiModule.showToast(
          `${context.successLabel || 'Code folder opened'}: ${_basename(chosenPath)}`,
        );
      } catch (_) {
        if (current() && uiModule && uiModule.showError) uiModule.showError('Could not open folder');
        return;
      }
    } else {
      try {
        const workspace = await bindWorkspacePath(chosenPath, 'agent_workspace');
        if (!current()) return;
        await setWorkspace(workspace.path, workspace.id);
        if (!current()) return;
        if (uiModule && uiModule.showToast) uiModule.showToast(`Workspace set: ${_basename(workspace.path)}`);
      } catch (error) {
        if (current() && uiModule && uiModule.showError) uiModule.showError(error?.message || 'This folder does not have Agent access');
        return;
      }
    }
    if (current()) closeWorkspaceBrowser();
  });
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

export async function openWorkspaceBrowser(options = {}) {
  const appFolder = options?.selectionKind === 'app_folder';
  _browserContext.active = false;
  _browserContext.onCancel?.();
  _browserContext = {
    active: true,
    selectionKind: appFolder ? 'app_folder' : 'agent_workspace',
    onSelect: appFolder && typeof options.onSelect === 'function' ? options.onSelect : null,
    successLabel: String(options.successLabel || ''),
    onCancel: appFolder && typeof options.onCancel === 'function' ? options.onCancel : null,
  };
  const modal = _getModal();
  const title = modal.querySelector('#workspace-browser-title span');
  const note = modal.querySelector('#workspace-browser-note');
  const use = modal.querySelector('#workspace-use');
  if (title) title.textContent = String(options.title || (appFolder ? 'Open Editor folder' : 'Select workspace'));
  if (note) note.textContent = String(options.note || (appFolder
    ? 'Choose any folder visible to this account. This changes the Editor only and does not grant agent file access.'
    : 'File tools are confined to this folder. The workspace narrows agent activity; it does not expand agent permissions.'));
  if (use) use.textContent = String(options.useLabel || (appFolder ? 'Open folder' : 'Use this folder'));
  const initialPath = appFolder ? String(options.initialPath || '') : getWorkspace();
  const pathInput = modal.querySelector('#workspace-cur-path');
  if (pathInput) { pathInput.value = initialPath; pathInput.title = ''; }
  modal.style.display = 'flex';
  await _navigate(initialPath || '', 'Could not browse folders');
}

export function closeWorkspaceBrowser() {
  _browserContext.active = false;
  if (_modal) _modal.style.display = 'none';
  const cancel = _browserContext.onCancel;
  _browserContext.onCancel = null;
  cancel?.();
}

export async function initWorkspace() {
  // Browser storage is only UI state, never an authority. On a standard-user
  // account validate a stale path against the server projection before showing
  // it; this also prevents an administrator's prior browser session from
  // leaking an unassigned path through the workspace pill.
  const saved = Storage.get(KEYS.WORKSPACE, '') || '';
  const savedId = getWorkspaceId();
  syncWorkspaceIndicator('');
  if (savedId) {
    try {
      const workspace = await resolveWorkspaceId(savedId, 'agent_workspace');
      await setWorkspace(workspace.path, workspace.id, { persist: false });
    } catch (_) {
      await clearWorkspace({ persist: false });
    }
  } else if (saved) {
    console.info('Saved folder preference is preserved. Choose its folder again to select a current Workspace ID.');
    if (uiModule?.showError) uiModule.showError('Your saved folder needs a Workspace selection. Choose the folder again; the saved preference is preserved.');
  } else {
    syncWorkspaceIndicator('');
  }
  const overflow = document.getElementById('overflow-workspace-btn');
  if (overflow) overflow.addEventListener('click', openWorkspaceBrowser);
  const pill = document.getElementById('workspace-indicator-btn');
  if (pill) pill.addEventListener('click', clearWorkspace);
}

export default { initWorkspace, openWorkspaceBrowser, getWorkspace, getWorkspaceId, bindWorkspacePath, resolveWorkspaceId, setWorkspace, vetAndSetWorkspace, clearWorkspace, syncWorkspaceIndicator, applyMode, subscribeWorkspaceIndex };
