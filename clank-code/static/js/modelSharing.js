// Named-user model sharing. The UI keeps transport and credential details out
// of the DOM and consumes only the API's explicit display fields.

import { providerDisplayName, sharedProviderLabel } from './modelLabels.js';

let root;
let status;
let receivedList;
let initialized = false;
let loading;
let onCatalogChanged = async () => {};
const openOwnerChoosers = new Set();
let state = {
  users: [],
  owned: [],
  received: [],
};

function toast(message, duration = 3500) {
  window.uiModule?.showToast?.(message, duration);
}

function node(tag, attrs = {}, text = '') {
  const element = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') element.className = value;
    else if (key === 'type') element.type = value;
    else if (key === 'checked') element.checked = Boolean(value);
    else element.setAttribute(key, String(value));
  });
  if (text) element.textContent = text;
  return element;
}

function clean(value, limit = 200) {
  return String(value || '').trim().slice(0, limit);
}

function normalizeUser(value) {
  const username = clean(typeof value === 'string' ? value : value?.username, 128);
  if (!username) return null;
  return {
    username,
    label: clean(
      typeof value === 'string'
        ? value
        : value?.display_name || value?.display || value?.username,
      128,
    ) || username,
  };
}

function normalizeOwned(value) {
  const endpointId = clean(value?.endpoint_id, 256);
  const modelId = clean(value?.model_id, 512);
  if (!endpointId || !modelId) return null;
  const recipientValues = Array.isArray(value?.recipients)
    ? value.recipients
    : Array.isArray(value?.shared_with)
      ? value.shared_with
      : [];
  return {
    endpointId,
    modelId,
    recipients: [...new Set(recipientValues.map(item => clean(
      typeof item === 'string' ? item : item?.username,
      128,
    )).filter(Boolean))],
  };
}

export function normalizeReceived(value) {
  const shareId = clean(value?.share_id, 256);
  const modelId = clean(value?.model_id || value?.model_name || value?.display_name, 512);
  const modelName = clean(value?.model_name || value?.display_name || modelId, 256);
  if (!shareId || !modelId || !modelName) return null;
  // Normalized shares provide this from the source connection's family. The
  // legacy `provider` value is accepted as an explicit server projection;
  // never infer provider identity from a model ID.
  const provider = providerDisplayName(
    value?.provider_display_name
      || value?.provider_family_name
      || value?.provider,
  );
  return {
    shareId,
    modelId,
    modelName,
    provider,
    providerFamilyId: clean(value?.provider_family_id || value?.family_id, 128),
    sharedBy: clean(value?.shared_by, 128) || 'another user',
    enabled: Boolean(value?.enabled),
  };
}

const REASONING_LABELS = {
  none: 'No reasoning',
  minimal: 'Minimal reasoning',
  low: 'Low reasoning',
  medium: 'Medium reasoning',
  high: 'High reasoning',
  xhigh: 'Extra-high reasoning',
  max: 'Maximum reasoning',
};

function titleModelPart(value) {
  const parts = String(value || '')
    .split(/[-_]+/)
    .filter(Boolean);
  const title = part => {
    const lower = part.toLowerCase();
    if (lower === 'mimo') return 'MiMo';
    if (lower === 'deepseek') return 'DeepSeek';
    if (/^v\d/i.test(part)) return `V${part.slice(1)}`;
    return part.charAt(0).toUpperCase() + part.slice(1);
  };
  if (parts[0]?.toLowerCase() === 'gpt' && parts[1]) {
    return [`GPT-${parts[1]}`, ...parts.slice(2).map(title)].join(' ');
  }
  return parts.map(title).join(' ');
}

function receivedModelLabel(share) {
  const path = share.modelId.split('/').filter(Boolean);
  const variant = path.length > 2
    ? REASONING_LABELS[path.at(-1).toLowerCase()]
    : '';
  const modelPart = variant ? path.at(-2) : path.at(-1);
  const family = titleModelPart(modelPart) || share.modelName;
  return variant ? `${family} · ${variant}` : family;
}

function normalizePayload(payload) {
  const users = (Array.isArray(payload?.share_users) ? payload.share_users : [])
    .map(normalizeUser)
    .filter(Boolean);
  const owned = (Array.isArray(payload?.owned) ? payload.owned : [])
    .map(normalizeOwned)
    .filter(Boolean);
  // Deliberately do not consume the old global `available` collection. The
  // named-user contract exposes only grants addressed to this caller.
  const received = (Array.isArray(payload?.received) ? payload.received : [])
    .map(normalizeReceived)
    .filter(Boolean);
  return { users, owned, received };
}

async function request(path = '', options = {}) {
  const response = await fetch(`/api/model-shares${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail?.message || data.detail || data.error || `Sharing request failed (${response.status})`);
  }
  return data;
}

function setStatus(message, error = false) {
  if (!status) return;
  status.textContent = message || '';
  status.style.color = error ? 'var(--red, #ff5555)' : '';
}

function renderReceived() {
  if (!receivedList) return;
  receivedList.replaceChildren();
  if (!state.received.length) {
    receivedList.append(node(
      'p',
      { class: 'admin-toggle-sub' },
      'No models have been shared directly with you.',
    ));
    setStatus('');
    return;
  }
  const providerGroups = new Map();
  state.received.forEach(share => {
    if (!providerGroups.has(share.provider)) providerGroups.set(share.provider, []);
    providerGroups.get(share.provider).push(share);
  });
  [...providerGroups].forEach(([provider, shares]) => {
    const folder = node('details', { class: 'model-share-folder' });
    if (shares.some(share => share.enabled)) folder.setAttribute('open', '');
    const summary = node('summary', { class: 'model-share-folder-summary' });
    summary.append(
        node('strong', {}, sharedProviderLabel(provider)),
      node(
        'span',
        { class: 'model-share-folder-count' },
        `${shares.length} model${shares.length === 1 ? '' : 's'}`,
      ),
    );
    const contents = node('div', { class: 'model-share-folder-contents' });
    shares.forEach(share => {
      const labelText = receivedModelLabel(share);
      const card = node('div', { class: 'model-share-offer' });
      const copy = node('div', { class: 'model-share-offer-copy' });
      copy.append(
        node('strong', {}, labelText),
        node('span', { class: 'admin-toggle-sub' }, `Shared by ${share.sharedBy}`),
      );
      const control = node('label', { class: 'model-share-offer-toggle' });
      const label = node('span', {}, 'Add to my models');
      const toggleShell = node('span', { class: 'admin-switch' });
      const toggle = node('input', {
        type: 'checkbox',
        'aria-label': `Add ${labelText} to my models`,
        checked: share.enabled,
      });
      toggleShell.append(toggle, node('span', { class: 'admin-slider' }));
      control.append(label, toggleShell);
      toggle.addEventListener('change', async event => {
        event.stopPropagation();
        const wanted = toggle.checked;
        toggle.disabled = true;
        try {
          await request(`/${encodeURIComponent(share.shareId)}/subscription`, {
            method: 'PUT',
            body: JSON.stringify({ enabled: wanted }),
          });
          await load();
          await onCatalogChanged();
        } catch (error) {
          toggle.checked = !wanted;
          toggle.disabled = false;
          setStatus(error.message, true);
          toast(`Shared model update failed: ${error.message}`);
        }
      });
      card.append(copy, control);
      contents.append(card);
    });
    folder.append(summary, contents);
    receivedList.append(folder);
  });
  setStatus(`${state.received.length} model${state.received.length === 1 ? '' : 's'} shared with you`);
}

export function ownedRecipients(endpointId, modelId) {
  const match = state.owned.find(item => (
    item.endpointId === String(endpointId)
    && item.modelId === String(modelId)
  ));
  return match ? [...match.recipients] : [];
}

function shareUsersFor(recipients) {
  const users = new Map(state.users.map(user => [user.username, user]));
  recipients.forEach(username => {
    if (!users.has(username)) users.set(username, { username, label: username });
  });
  return [...users.values()].sort((a, b) => a.label.localeCompare(b.label));
}

function isGloballyIncluded(modelId) {
  return ['xiaomi/mimo-auto', 'mimo/mimo-auto', 'mimo-auto'].includes(
    String(modelId || '').toLowerCase(),
  );
}

async function saveOwned(endpointId, modelId, recipients) {
  const uniqueRecipients = [...new Set(recipients.map(value => clean(value, 128)).filter(Boolean))];
  await request('', {
    method: 'PUT',
    body: JSON.stringify({
      endpoint_id: endpointId,
      model_id: modelId,
      shared: uniqueRecipients.length > 0,
      recipients: uniqueRecipients,
    }),
  });
  await load();
  await onCatalogChanged();
}

export function mountOwnerControls(panel, endpointId) {
  if (!panel) return;
  panel.querySelectorAll('[data-model-share-control]').forEach(element => element.remove());
  panel.querySelectorAll('[data-share-model-id]').forEach(row => {
    const modelId = row.dataset.shareModelId;
    if (!modelId || isGloballyIncluded(modelId) || row.dataset.shareDisabled === 'true') return;
    const chooserKey = `${endpointId}\u0000${modelId}`;
    const chooserOpen = openOwnerChoosers.has(chooserKey);
    const recipients = ownedRecipients(endpointId, modelId);
    const users = shareUsersFor(recipients);
    const control = node('div', {
      class: 'adm-model-capability model-share-control',
      'data-model-share-control': '',
    });
    const summary = node(
      'button',
      { type: 'button', class: 'admin-btn-sm model-share-users-button' },
      recipients.length
        ? `Shared with ${recipients.length}`
        : 'Share',
    );
    summary.setAttribute('aria-label', `Choose who can use ${modelId}`);
    summary.setAttribute('aria-expanded', chooserOpen ? 'true' : 'false');
    const chooser = node('div', {
      class: `model-share-users${chooserOpen ? '' : ' hidden'}`,
      'data-model-share-control': '',
    });

    if (!users.length) {
      chooser.append(node(
        'span',
        { class: 'admin-toggle-sub' },
        'No other users are available.',
      ));
    } else {
      users.forEach(user => {
        const option = node('label', { class: 'model-share-user-option' });
        const checkbox = node('input', {
          type: 'checkbox',
          checked: recipients.includes(user.username),
        });
        option.append(checkbox, node('span', {}, user.label));
        checkbox.addEventListener('change', async event => {
          event.stopPropagation();
          const before = ownedRecipients(endpointId, modelId);
          const selected = [...chooser.querySelectorAll('input[type="checkbox"]:checked')]
            .map(input => input._shareUsername);
          chooser.querySelectorAll('input, button').forEach(input => { input.disabled = true; });
          summary.disabled = true;
          try {
            await saveOwned(endpointId, modelId, selected);
            mountOwnerControls(panel, endpointId);
          } catch (error) {
            state.owned = state.owned.filter(item => !(
              item.endpointId === endpointId && item.modelId === modelId
            ));
            if (before.length) state.owned.push({ endpointId, modelId, recipients: before });
            mountOwnerControls(panel, endpointId);
            toast(`Sharing update failed: ${error.message}`);
          }
        });
        checkbox._shareUsername = user.username;
        chooser.append(option);
      });
    }

    summary.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      const open = chooser.classList.toggle('hidden') === false;
      if (open) openOwnerChoosers.add(chooserKey);
      else openOwnerChoosers.delete(chooserKey);
      summary.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    control.addEventListener('click', event => event.stopPropagation());
    chooser.addEventListener('click', event => event.stopPropagation());
    control.append(summary);
    row.append(control, chooser);
  });
}

export async function load() {
  if (loading) return loading;
  loading = (async () => {
    try {
      const payload = await request();
      state = normalizePayload(payload);
      renderReceived();
      return {
        users: state.users.map(item => ({ ...item })),
        owned: state.owned.map(item => ({ ...item, recipients: [...item.recipients] })),
        received: state.received.map(item => ({ ...item })),
      };
    } catch (error) {
      state = { users: [], owned: [], received: [] };
      renderReceived();
      setStatus(error.message, true);
      return state;
    } finally {
      loading = undefined;
    }
  })();
  return loading;
}

export function init(options = {}) {
  if (initialized) return;
  root = document.getElementById('model-share-received');
  status = document.getElementById('model-share-status');
  receivedList = document.getElementById('model-share-list');
  if (!root) return;
  initialized = true;
  onCatalogChanged = options.onCatalogChanged || onCatalogChanged;
}

export default {
  init,
  load,
  mountOwnerControls,
  ownedRecipients,
};
