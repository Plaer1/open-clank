import { openFileLocationWizardDialog } from './fileLocationWizard.js';

async function checkedFetch(input, init = {}) {
  const response = await window.fetch(input, { credentials: 'same-origin', ...init });
  if (response.ok) return response;
  let payload = {};
  try { payload = await response.clone().json(); } catch (_) {}
  const detail = payload?.detail;
  const error = new Error(String(detail?.message || detail || payload?.error || `Request failed (${response.status})`));
  error.status = response.status;
  throw error;
}

async function requireAdministrator() {
  const response = await checkedFetch('/api/auth/status', { cache: 'no-store' });
  const status = await response.json();
  if (!status?.username || !status?.account_id || status?.is_admin !== true) {
    throw new Error('Only an administrator can register a host Location.');
  }
  return status.account_id;
}

async function readPolicy(owner) {
  const response = await checkedFetch('/api/file-policy/state', { cache: 'no-store' });
  const state = await response.json();
  if (state?.subject_id !== owner || state?.is_admin !== true) throw new Error('The managing account changed.');
  return state;
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
      selectionKind: 'register_location',
      initialPath,
      title: 'Choose host folder',
      note: 'Choose a folder on the Open Clank host. Nothing is granted until you review and add the Location.',
      useLabel: 'Choose folder',
      successLabel: 'Host folder selected',
      onSelect: async path => {
        resolve(String(path || ''));
        workspace.closeWorkspaceBrowser();
      },
      onCancel: () => resolve(''),
    });
  });
}

function exactRegistration(state, request) {
  const candidates = state.locations.filter(location => location.enabled && location.kind === request.kind &&
    (location.canonical_path === request.path || location.display_path === request.path));
  if (candidates.length !== 1) return null;
  const [location] = candidates;
  if (location.availability !== 'available' || !request.capabilities.every(cap => location.capabilities.includes(cap))) return null;
  const covers = kind => state.bindings.some(binding => binding.binding_class === kind &&
    binding.subject_id === state.subject_id && binding.location_id === location.id &&
    binding.status === 'active' && binding.lifetime === 'always' &&
    request.capabilities.every(cap => binding.capabilities.includes(cap)));
  const access = state.effective_scope?.locations?.find(row => row.location_id === location.id);
  const appAccess = access?.app_capabilities?.includes('read');
  const agentAccess = access?.agent_capabilities?.includes('read');
  const access_pending = [];
  if (request.app_access && !(appAccess && covers('people'))) access_pending.push('app');
  if (request.agent_access && !covers('agent')) access_pending.push('agent');
  return {location, app_access:Boolean(appAccess), agent_access:Boolean(agentAccess), access_pending};
}

async function submitLocation(owner, { path, kind, capabilities, appAccess, agentAccess }) {
  const inferredWholeRoot = path === '/' || /^[A-Za-z]:[\/]?$/.test(path);
  const request = {
    path, kind: kind === 'whole_root' || inferredWholeRoot ? 'whole_root' : kind,
    capabilities, app_access: appAccess, agent_access: agentAccess,
  };
  await readPolicy(owner);
  let value;
  try {
    const response = await checkedFetch('/api/file-policy/locations', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Openclank-Account': owner },
      body: JSON.stringify(request),
    });
    value = await response.json();
  } catch (error) {
    if (error.status && error.status < 500) throw error;
    try { value = exactRegistration(await readPolicy(owner), request); } catch (_) {}
    if (!value) {
      throw Object.assign(new Error('The result is unknown. Check Locations and People access before opening a new request.'), {unknown:true});
    }
  }
  document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  if (value?.access_pending?.length) {
    const missing = value.access_pending.map(scope => scope === 'app' ? 'Files and Editor' : 'Agents').join(' and ');
    throw Object.assign(new Error(`Location registered. Review and retry the pending ${missing} access.`), {
      remaining: value.access_pending,
      path: value.location.canonical_path,
    });
  }
  if (!value?.location?.id) throw Object.assign(new Error('The result is unknown. Check Locations and People access.'), {unknown:true});
  return {path: value.location.canonical_path, appAccess: value.app_access === true, agentAccess: value.agent_access === true};
}

export async function openFileLocationWizard({ suggestedPath = '', onAdded = null } = {}) {
  const owner = await requireAdministrator();
  await readPolicy(owner);
  return openFileLocationWizardDialog({
    suggestedPath,
    resolveHome,
    resolveWholeRoot,
    browseFolder,
    submit: async input => {
      const result = await submitLocation(owner, input);
      await onAdded?.({...input, ...result});
      return result;
    },
  });
}

export default { openFileLocationWizard };
