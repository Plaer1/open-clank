// static/js/workspace.js
//
// Workspace picker: browse server directories in a draggable modal, choose a
// folder, and show it as a removable pill in the chat input bar. While set, the
// chat request sends `workspace` so the agent's file/shell tools are confined
// to that folder (see routes/chat_routes.py + src/tool_execution.py).

import Storage, { KEYS } from './storage.js';
import uiModule from './ui.js';
import { makeWindowDraggable } from './windowDrag.js';

const API_BASE = window.location.origin;
// Same folder glyph as the overflow menu item + pill (not an emoji).
const _FOLDER_SVG = '<svg class="workspace-row-icon" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
let _modal = null;
let _curPath = '';
let _displayGeneration = 0;
const _persistQueueBySession = new Map();
let _browserContext = {
  selectionKind: 'agent_workspace',
  onSelect: null,
  successLabel: '',
};

export function getWorkspace() {
  return Storage.get(KEYS.WORKSPACE, '') || '';
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
  if (options.persist === false) return;
  try {
    await _persistCurrentChatWorkspace(workspaceId || '');
  } catch (error) {
    // A session switch or newer choice owns the display now. The failed older
    // request may report its error, but it must never repaint that later chat.
    if (displayGeneration === _displayGeneration) {
      _applyWorkspaceDisplay(previousPath, previousId);
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

async function _load(path) {
  const params = new URLSearchParams();
  if (path) params.set('path', path);
  params.set('selection_kind', _browserContext.selectionKind);
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

async function _navigate(path) {
  try {
    _render(await _load(path));
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not open folder');
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
  // Editable path bar: Enter navigates to a typed/pasted folder.
  _modal.querySelector('#workspace-cur-path').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      const v = e.target.value.trim();
      if (v) _navigate(v);
    }
  });
  _modal.querySelector('#workspace-use').addEventListener('click', async () => {
    if (_browserContext.selectionKind === 'app_folder' && typeof _browserContext.onSelect === 'function') {
      try {
        await _browserContext.onSelect(_curPath);
        if (uiModule && uiModule.showToast) uiModule.showToast(
          `${_browserContext.successLabel || 'Code folder opened'}: ${_basename(_curPath)}`,
        );
      } catch (_) {
        if (uiModule && uiModule.showError) uiModule.showError('Could not open folder');
        return;
      }
    } else {
      try {
        const workspace = await bindWorkspacePath(_curPath, 'agent_workspace');
        await setWorkspace(workspace.path, workspace.id);
        if (uiModule && uiModule.showToast) uiModule.showToast(`Workspace set: ${_basename(workspace.path)}`);
      } catch (error) {
        if (uiModule && uiModule.showError) uiModule.showError(error?.message || 'This folder does not have Agent access');
        return;
      }
    }
    closeWorkspaceBrowser();
  });
  const content = _modal.querySelector('.modal-content');
  const header = _modal.querySelector('.modal-header');
  if (content && header) makeWindowDraggable(_modal, { content, header });
  return _modal;
}

export async function openWorkspaceBrowser(options = {}) {
  const appFolder = options?.selectionKind === 'app_folder';
  _browserContext = {
    selectionKind: appFolder ? 'app_folder' : 'agent_workspace',
    onSelect: appFolder && typeof options.onSelect === 'function' ? options.onSelect : null,
    successLabel: String(options.successLabel || ''),
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
  // Do not leave a previously rendered folder selectable while a new browse
  // request is pending or has failed.
  _curPath = '';
  if (use) {
    use.disabled = true;
    use.title = 'Loading folder';
  }
  modal.style.display = 'flex';
  try {
    const initialPath = appFolder ? String(options.initialPath || '') : getWorkspace();
    _render(await _load(initialPath || ''));
  } catch (e) {
    if (uiModule && uiModule.showError) uiModule.showError('Could not browse folders');
  }
}

export function closeWorkspaceBrowser() {
  if (_modal) _modal.style.display = 'none';
}

export async function initWorkspace() {
  // Browser storage is only UI state, never an authority. On a standard-user
  // account validate a stale path against the server projection before showing
  // it; this also prevents an administrator's prior browser session from
  // leaking an unassigned path through the workspace pill.
  const saved = getWorkspace();
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
    // One-time migration of the old raw browser preference. The server only
    // issues an ID when the folder is already inside effective Agent access.
    const checked = await vetAndSetWorkspace(saved);
    if (!checked.ok) await clearWorkspace({ persist: false });
  } else {
    syncWorkspaceIndicator('');
  }
  const overflow = document.getElementById('overflow-workspace-btn');
  if (overflow) overflow.addEventListener('click', openWorkspaceBrowser);
  const pill = document.getElementById('workspace-indicator-btn');
  if (pill) pill.addEventListener('click', clearWorkspace);
}

export default { initWorkspace, openWorkspaceBrowser, getWorkspace, getWorkspaceId, bindWorkspacePath, resolveWorkspaceId, setWorkspace, vetAndSetWorkspace, clearWorkspace, syncWorkspaceIndicator, applyMode };
