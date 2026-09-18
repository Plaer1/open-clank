import { openFileLocationWizardDialog } from './fileLocationWizard.js';

async function checkedFetch(input, init = {}) {
  const response = await window.fetch(input, { credentials: 'same-origin', ...init });
  if (response.ok) return response;
  let payload = {};
  try { payload = await response.clone().json(); } catch (_) {}
  const detail = payload?.detail;
  throw new Error(String(detail?.message || detail || payload?.error || `Request failed (${response.status})`));
}

async function requireAdministrator() {
  const response = await checkedFetch('/api/auth/status', { cache: 'no-store' });
  const status = await response.json();
  if (!status?.username || status?.is_admin !== true) {
    throw new Error('Only an administrator can register a host Location.');
  }
}

async function policyBackend() {
  const response = await window.fetch('/api/file-policy/state', {
    credentials: 'same-origin', cache: 'no-store',
  });
  if (response.ok) return 'canonical';
  if (response.status === 404) return 'legacy';
  let payload = {};
  try { payload = await response.json(); } catch (_) {}
  throw new Error(String(payload?.detail?.message || payload?.detail || `Policy request failed (${response.status})`));
}

async function locationPresets() {
  const response = await checkedFetch('/api/file-policy/location-presets', { cache: 'no-store' });
  return response.json();
}

async function resolveHome() {
  const path = String((await locationPresets())?.home || '');
  if (!path) throw new Error('Host home is unavailable.');
  return path;
}

async function resolveWholeRoot() {
  const path = String((await locationPresets())?.whole_roots?.[0]?.path || '');
  if (!path) throw new Error('A whole-disk or volume preset is unavailable on this host.');
  return path;
}

async function browseFolder(initialPath = '') {
  const workspace = await import('./workspace.js');
  return new Promise(resolve => {
    workspace.openWorkspaceBrowser({
      selectionKind: 'app_folder',
      initialPath,
      title: 'Choose host folder',
      note: 'Choose a folder on the Open Clank host. Nothing is granted until you review and add the Location.',
      useLabel: 'Choose folder',
      successLabel: 'Host folder selected',
      onSelect: async path => {
        workspace.closeWorkspaceBrowser();
        resolve(String(path || ''));
      },
    });
  });
}

async function submitLocation(backend, { path, kind, capabilities, agentAccess }) {
  if (backend === 'canonical') {
    const inferredWholeRoot = path === '/' || /^[A-Za-z]:[\\/]?$/.test(path);
    await checkedFetch('/api/file-policy/locations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path,
        kind: kind === 'whole_root' || inferredWholeRoot ? 'whole_root' : kind,
        capabilities,
        agent_access: agentAccess,
      }),
    });
  } else {
    await checkedFetch('/api/odysseus-files/roots', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        path,
        kind: kind === 'exact_file' ? 'exact_file' : 'recursive_directory',
        capabilities,
      }),
    });
  }
  document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  return true;
}

export async function openFileLocationWizard({ suggestedPath = '', onAdded = null } = {}) {
  await requireAdministrator();
  const backend = await policyBackend();
  return openFileLocationWizardDialog({
    suggestedPath,
    resolveHome,
    resolveWholeRoot,
    browseFolder,
    submit: async input => {
      const result = await submitLocation(backend, input);
      await onAdded?.(input);
      return result;
    },
  });
}

export default { openFileLocationWizard };
