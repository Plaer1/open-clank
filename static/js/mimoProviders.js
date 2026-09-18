// Provider-account connections inside the original Local/API model workflow.

import modelSharing from './modelSharing.js';
import { spriteLogo } from './providers.js';

let root;
let _bound = false;
let providers = [];
let selectedProviderId = '';
let explicitSelection = false;
let activeAbort;
let activeFlowId;
let onCatalogChanged = async () => {};

function node(tag, attrs = {}, text = '') {
  const el = document.createElement(tag);
  Object.entries(attrs).forEach(([key, value]) => {
    if (key === 'class') el.className = value;
    else if (key === 'type') el.type = value;
    else el.setAttribute(key, value);
  });
  if (text) el.textContent = text;
  return el;
}

function providerId(provider) {
  return String(provider?.id || '').trim().toLowerCase();
}

function providerName(provider) {
  if (providerId(provider) === 'xiaomi') return 'Xiaomi';
  return String(provider?.name || provider?.id || 'Provider').trim();
}

function isPublicProvider(provider) {
  const id = providerId(provider);
  return Boolean(id) && id !== 'mimo' && !id.startsWith('ody-');
}

function providerFamilyLabel(provider) {
  const name = providerName(provider);
  const family = providerId(provider) === 'xiaomi'
    ? 'MiMo'
    : String(provider?.family || '').trim();
  if (!family || family.toLowerCase() === name.toLowerCase()) return '';
  return `${family} models`;
}

function writeStatus(id, message, error = false) {
  const status = document.getElementById(id);
  if (!status) return;
  status.textContent = message || '';
  status.style.color = error ? 'var(--red, #ff5555)' : '';
}

function setStatus(message, error = false) {
  writeStatus('mimo-provider-status', message, error);
  writeStatus('mimo-connected-provider-status', message, error);
}

async function request(path, options = {}) {
  const response = await fetch(`/api/mimo/providers${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'Provider operation failed');
  return data;
}

async function changed(message) {
  setStatus(message);
  await onCatalogChanged();
  await load();
}

function closeFlow({ cancel = true } = {}) {
  if (activeAbort) activeAbort.abort();
  activeAbort = undefined;
  if (cancel && activeFlowId) {
    const flowId = activeFlowId;
    activeFlowId = undefined;
    request('/oauth/cancel', {
      method: 'POST',
      body: JSON.stringify({ flow_id: flowId }),
    }).catch(() => {});
  } else {
    activeFlowId = undefined;
  }
  ['mimo-provider-flow', 'mimo-connected-provider-flow'].forEach(id => {
    document.getElementById(id)?.replaceChildren();
  });
}

function visibleFlow() {
  const addedPanel = document.querySelector('[data-settings-panel="added-models"]:not(.hidden)');
  return addedPanel?.querySelector('#mimo-connected-provider-flow')
    || document.getElementById('mimo-provider-flow');
}

function flowShell(provider, title) {
  closeFlow();
  const flow = visibleFlow();
  const card = node('div', { class: 'admin-card' });
  const heading = node('h2', {}, `${providerName(provider)} — ${title}`);
  const body = node('div', { class: 'settings-col' });
  const actions = node('div', { class: 'settings-row' });
  const cancel = node('button', { type: 'button', class: 'btn secondary' }, 'Cancel');
  cancel.addEventListener('click', closeFlow);
  actions.append(cancel);
  card.append(heading, body, actions);
  flow.append(card);
  return { body, actions };
}

function openClientLoginWindow() {
  const popup = window.open('about:blank', '_blank');
  if (!popup) return null;
  try {
    popup.opener = null;
    popup.document.title = 'Open Clank provider login';
    popup.document.body.textContent = 'Starting provider login…';
  } catch (_) {}
  return popup;
}

function showLoginLink(body, url) {
  const link = node('a', {
    class: 'btn secondary',
    href: url,
    target: '_blank',
    rel: 'noopener noreferrer',
  }, 'Open provider login');
  body.append(link);
}

function sendLoginWindow(popup, url) {
  if (!popup || popup.closed) return false;
  try {
    popup.location.replace(url);
    return true;
  } catch (_) {
    try {
      popup.location.href = url;
      return true;
    } catch (_) {
      return false;
    }
  }
}

async function pollOAuthFlow(flowId) {
  activeAbort = new AbortController();
  while (!activeAbort.signal.aborted) {
    const result = await request(`/oauth/status?flow_id=${encodeURIComponent(flowId)}`, {
      signal: activeAbort.signal,
    });
    if (result.status === 'connected') return result;
    if (result.status === 'failed' || result.status === 'expired' || result.status === 'cancelled') {
      throw new Error(result.error || `Provider login ${result.status}.`);
    }
    await new Promise(resolve => setTimeout(resolve, 1200));
  }
  throw new DOMException('Provider login cancelled', 'AbortError');
}

function promptVisible(prompt, values) {
  if (!prompt.when) return true;
  const match = values[prompt.when.key] === prompt.when.value;
  return prompt.when.op === 'eq' ? match : !match;
}

function appendPromptFields(body, prompts, values) {
  const fields = [];
  (prompts || []).forEach(prompt => {
    const row = node('label', { class: 'settings-row' });
    row.append(node('span', { class: 'settings-label' }, prompt.message || prompt.key));
    let input;
    if (prompt.type === 'select') {
      input = node('select', { class: 'settings-select' });
      (prompt.options || []).forEach(option => {
        const opt = node(
          'option',
          { value: option.value },
          option.hint ? `${option.label} — ${option.hint}` : option.label,
        );
        input.append(opt);
      });
    } else {
      input = node('input', {
        class: 'settings-input',
        type: 'text',
        placeholder: prompt.placeholder || '',
        autocomplete: 'off',
      });
    }
    values[prompt.key] = input.value;
    input.addEventListener('input', () => {
      values[prompt.key] = input.value;
      fields.forEach(item => {
        item.row.hidden = !promptVisible(item.prompt, values);
      });
    });
    fields.push({ prompt, row, input });
    row.append(input);
    body.append(row);
  });
  fields.forEach(item => {
    item.row.hidden = !promptVisible(item.prompt, values);
  });
  return fields;
}

function authActionLabel(provider, method) {
  const name = providerName(provider);
  if (method.type === 'api') return `Add ${name} API key`;
  if (providerId(provider) === 'xiaomi' && method.type === 'oauth') {
    return 'Sign in with Xiaomi';
  }
  return String(method.label || (method.type === 'oauth' ? `Sign in with ${name}` : 'Connect'));
}

function appendAuthActions(actions, provider) {
  const seen = new Set();
  (provider.methods || []).forEach(method => {
    const label = authActionLabel(provider, method);
    const key = `${method.type}:${label.toLowerCase()}`;
    if (seen.has(key)) return;
    seen.add(key);
    const button = node('button', { type: 'button', class: 'btn secondary' }, label);
    button.addEventListener('click', () => (
      method.type === 'oauth' ? beginOAuth(provider, method) : beginApiKey(provider, method)
    ));
    actions.append(button);
  });
}

async function beginOAuth(provider, method) {
  const values = {};
  const actionLabel = authActionLabel(provider, method);
  const { body, actions } = flowShell(provider, 'Sign in');
  const fields = appendPromptFields(body, method.prompts, values);
  const start = node('button', { type: 'button', class: 'btn primary' }, 'Continue');
  actions.prepend(start);
  start.addEventListener('click', async () => {
    start.disabled = true;
    setStatus(`Starting ${actionLabel}…`);
    const loginWindow = openClientLoginWindow();
    try {
      const inputs = Object.fromEntries(fields.filter(item => !item.row.hidden).map(item => [item.prompt.key, item.input.value]));
      const authorization = await request(`/${encodeURIComponent(provider.id)}/oauth/authorize`, {
        method: 'POST',
        body: JSON.stringify({ method: method.index, ...(fields.length ? { inputs } : {}) }),
      });
      sendLoginWindow(loginWindow, authorization.url);
      await finishOAuth(provider, method, authorization);
    } catch (error) {
      if (loginWindow && !loginWindow.closed) loginWindow.close();
      setStatus(error.message, true);
      start.disabled = false;
    }
  });
}

async function finishOAuth(provider, method, authorization) {
  const { body, actions } = flowShell(provider, 'Finish sign-in');
  activeFlowId = authorization.flow_id;
  if (authorization.instructions) body.append(node('p', { class: 'admin-toggle-sub' }, authorization.instructions));
  showLoginLink(body, authorization.url);
  const capability = authorization.capability || method.capability;
  if (capability === 'paste_code') {
    const row = node('label', { class: 'settings-row' });
    row.append(node('span', { class: 'settings-label' }, 'Authorization code'));
    const code = node('input', { class: 'settings-input', type: 'text', autocomplete: 'off' });
    row.append(code);
    body.append(row);
    const complete = node('button', { type: 'button', class: 'btn primary' }, 'Complete sign-in');
    actions.prepend(complete);
    complete.addEventListener('click', async () => {
      if (!code.value.trim()) return setStatus('Enter the authorization code.', true);
      complete.disabled = true;
      try {
        await request(`/${encodeURIComponent(provider.id)}/oauth/callback`, {
          method: 'POST',
          body: JSON.stringify({ flow_id: authorization.flow_id, code: code.value.trim() }),
        });
        activeFlowId = undefined;
        closeFlow({ cancel: false });
        await changed(`${providerName(provider)} connected.`);
      } catch (error) {
        setStatus(error.message, true);
        complete.disabled = false;
      }
    });
    return;
  }

  body.append(node(
    'p',
    { class: 'admin-toggle-sub' },
    capability === 'device_code'
      ? 'Waiting for you to finish the device login…'
      : 'Waiting for the provider to return to Open Clank…',
  ));
  try {
    await pollOAuthFlow(authorization.flow_id);
    activeAbort = undefined;
    activeFlowId = undefined;
    closeFlow({ cancel: false });
    await changed(`${providerName(provider)} connected.`);
  } catch (error) {
    if (error.name !== 'AbortError') setStatus(error.message, true);
  }
}

function beginApiKey(provider, method) {
  const { body, actions } = flowShell(provider, 'API key');
  const prompts = Array.isArray(method?.prompts) ? method.prompts : [];
  const credentialPrompt = prompts.find(prompt => (
    ['key', 'token', 'apikey', 'api_key'].includes(String(prompt?.key || '').toLowerCase())
  ));
  const row = node('label', { class: 'settings-row' });
  row.append(node(
    'span',
    { class: 'settings-label' },
    credentialPrompt?.message || method?.label || 'API key',
  ));
  const key = node('input', {
    class: 'settings-input',
    type: 'password',
    autocomplete: 'off',
    placeholder: credentialPrompt?.placeholder || '',
  });
  row.append(key);
  body.append(row);
  const values = {};
  const fields = appendPromptFields(
    body,
    prompts.filter(prompt => prompt !== credentialPrompt),
    values,
  );
  body.append(node('p', { class: 'admin-toggle-sub' }, 'Stored securely and never shown again.'));
  const save = node('button', { type: 'button', class: 'btn primary' }, 'Connect');
  actions.prepend(save);
  save.addEventListener('click', async () => {
    if (!key.value.trim()) return setStatus('Enter an API key.', true);
    const inputs = Object.fromEntries(
      fields
        .filter(item => !item.row.hidden && item.input.value.trim())
        .map(item => [item.prompt.key, item.input.value.trim()]),
    );
    save.disabled = true;
    try {
      await request(`/${encodeURIComponent(provider.id)}/api-key`, {
        method: 'PUT',
        body: JSON.stringify({
          key: key.value.trim(),
          method: method?.index,
          ...(Object.keys(inputs).length ? { inputs } : {}),
        }),
      });
      key.value = '';
      closeFlow();
      await changed(`${providerName(provider)} connected.`);
    } catch (error) {
      key.value = '';
      setStatus(error.message, true);
      save.disabled = false;
    }
  });
}

async function disconnect(provider, button) {
  button.disabled = true;
  try {
    await request(`/${encodeURIComponent(provider.id)}`, { method: 'DELETE' });
    await changed(`${providerName(provider)} disconnected.`);
  } catch (error) {
    setStatus(error.message, true);
    button.disabled = false;
  }
}

function providerCard(provider, connectedView = false) {
  const card = node('div', { class: 'admin-user-row' });
  const models = connectedView && Array.isArray(provider.models)
    ? provider.models.filter(modelId => typeof modelId === 'string' && modelId)
    : [];
  // Unified connection-row style: same logo + badge treatment as the
  // Added Models endpoint rows (admin.js rowHtml) so old and new
  // connections read as one list.
  const logo = node('span', {
    class: 'adm-ep-row-logo',
    style: 'display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;flex-shrink:0;opacity:0.9;',
  });
  logo.innerHTML = spriteLogo(providerId(provider)) || spriteLogo(providerName(provider)) || '';
  const title = node('strong', { class: 'admin-user-name' }, providerName(provider));
  const familyLabel = providerFamilyLabel(provider);
  const family = familyLabel
    ? node('span', { class: 'admin-toggle-sub' }, familyLabel)
    : null;
  let badgeText = 'Not connected';
  let badgeOff = true;
  let statusText = 'Not connected';
  if (connectedView && !provider.connected) {
    badgeText = 'Included';
    statusText = 'MiMo Auto included';
  } else if (provider.connected) {
    badgeText = 'Connected';
    badgeOff = false;
    statusText = provider.free_tier ? 'Connected · Free access' : 'Connected';
    if (provider.chat_models) {
      statusText += ` · ${provider.chat_models} chat model${provider.chat_models === 1 ? '' : 's'}`;
    }
    if (provider.served_by?.endpoint_name) {
      statusText += ` · also available through “${provider.served_by.endpoint_name}”`;
    } else if (provider.active) {
      statusText += ' · available in the model picker';
    }
  }
  const hiddenIds = connectedView && Array.isArray(provider.hidden_model_ids)
    ? provider.hidden_model_ids.filter(modelId => typeof modelId === 'string' && modelId)
    : [];
  const toggleableModels = [...models, ...hiddenIds];
  const toggleableTotal = toggleableModels.length;
  const badges = node('span', { style: 'display:inline-flex;gap:4px;align-items:center;flex-wrap:wrap;' });
  badges.append(node(
    'span',
    { class: `admin-badge${badgeOff ? ' admin-badge-off' : ''}`, title: statusText },
    badgeText,
  ));
  let countBadge = null;
  if (connectedView && toggleableTotal) {
    countBadge = node(
      'span',
      { class: 'admin-badge' },
      `${models.length}/${toggleableTotal} models enabled`,
    );
    badges.append(countBadge);
  }
  const status = node('span', { class: 'admin-toggle-sub' }, statusText);
  const authNote = provider.auth_note
    ? node('p', { class: 'admin-toggle-sub' }, provider.auth_note)
    : provider.free_tier
      ? node('p', { class: 'admin-toggle-sub' }, 'Free access is active. Connect your own account or API key whenever you want.')
      : null;
  const actions = node('div', { class: 'settings-row' });
  if (!connectedView && (!provider.connected || provider.free_tier)) {
    appendAuthActions(actions, provider);
  }
  if (provider.connected) {
    const remove = node('button', { type: 'button', class: 'btn secondary' }, 'Disconnect');
    remove.addEventListener('click', () => disconnect(provider, remove));
    actions.append(remove);
  }
  const modelToggle = toggleableTotal
    ? node('button', {
        type: 'button',
        class: 'admin-btn-sm',
        'aria-expanded': 'false',
      }, `Show models (${toggleableTotal})`)
    : null;
  if (modelToggle) actions.append(modelToggle);
  const heading = node('div', { class: 'admin-user-info' });
  heading.append(logo, title);
  if (family) heading.append(family);
  heading.append(badges);
  card.append(heading, status);
  if (authNote) card.append(authNote);
  if (actions.children.length) card.append(actions);

  if (connectedView) {
    if (toggleableTotal) {
      const list = node('div', { class: 'mcp-tools-list hidden' });
      toggleableModels.forEach(modelId => {
        const isHidden = hiddenIds.includes(modelId);
        const modelRow = node('div', {
          class: 'adm-model-cap-row',
          'data-share-model-id': modelId,
          'data-share-disabled': isHidden ? 'true' : 'false',
        });
        const rowLabel = node('label', { class: 'adm-model-row', title: modelId });
        const switchWrap = node('span', { class: 'admin-switch' });
        const toggle = node('input', {
          type: 'checkbox',
          class: 'adm-cb-hidden',
          'data-mimo-model-id': modelId,
        });
        toggle.checked = !isHidden;
        switchWrap.append(toggle, node('span', { class: 'admin-slider', 'aria-hidden': 'true' }));
        rowLabel.append(switchWrap);
        rowLabel.append(node('span', {}, modelId.split('/').slice(1).join('/') || modelId));
        modelRow.append(rowLabel);
        list.append(modelRow);
      });
      const saveVisibility = async () => {
        const hidden = [];
        let enabledCount = 0;
        list.querySelectorAll('input[data-mimo-model-id]').forEach(input => {
          if (input.checked) enabledCount += 1;
          else hidden.push(input.dataset.mimoModelId);
          const rowEl = input.closest('[data-share-model-id]');
          if (rowEl) rowEl.dataset.shareDisabled = input.checked ? 'false' : 'true';
        });
        try {
          const response = await fetch(`/api/model-endpoints/mimo:${providerId(provider)}/models`, {
            method: 'PATCH',
            credentials: 'same-origin',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hidden }),
          });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          if (countBadge) {
            countBadge.textContent = `${enabledCount}/${toggleableTotal} models enabled`;
          }
          // Same refresh fan-out admin.js uses after endpoint model saves so
          // the chat picker drops the hidden models without a page reload.
          try {
            if (window.modelsModule && window.modelsModule.refreshModels) {
              window.modelsModule.refreshModels(true);
            }
          } catch (_) {}
          try {
            if (window.sessionModule && window.sessionModule.updateModelPicker) {
              window.sessionModule.updateModelPicker();
            }
          } catch (_) {}
        } catch (_) { /* silent */ }
      };
      list.querySelectorAll('input[data-mimo-model-id]').forEach(input => {
        input.addEventListener('change', saveVisibility);
      });
      card.append(list);
      modelSharing.mountOwnerControls(
        list,
        String(provider.connection_id || `mimo:${providerId(provider)}`),
      );
      modelToggle.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        const expanded = list.classList.toggle('hidden') === false;
        modelToggle.setAttribute('aria-expanded', expanded ? 'true' : 'false');
        modelToggle.textContent = `${expanded ? 'Hide' : 'Show'} models (${toggleableTotal})`;
      });
    }
  }
  return card;
}

function renderList(list, items, emptyText, connectedView = false) {
  if (!list) return;
  list.replaceChildren(...items.map(provider => providerCard(provider, connectedView)));
  if (!items.length) {
    list.append(node('p', { class: 'admin-toggle-sub' }, emptyText));
  }
}

function publicProviders() {
  return providers.filter(isPublicProvider);
}

function providerSearchText(provider) {
  return `${providerName(provider)} ${provider.id} ${providerFamilyLabel(provider)}`.toLowerCase();
}

function publishCatalog() {
  const catalogue = publicProviders();
  window.__openClankProviderCatalog = catalogue;
  if (typeof document.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
    document.dispatchEvent(new CustomEvent('open-clank:provider-catalog', {
      detail: { providers: catalogue },
    }));
  }
}

function renderProviderMenu(query = '') {
  const menu = document.getElementById('mimo-provider-menu');
  if (!menu) return;
  const needle = String(query || '').trim().toLowerCase();
  const available = publicProviders().filter(provider => (
    !provider.connected
    && (!needle || providerSearchText(provider).includes(needle))
  ));
  menu.replaceChildren(...available.map(provider => {
    const item = node('button', {
      type: 'button',
      class: 'mimo-provider-option',
      role: 'option',
      'data-provider-id': providerId(provider),
      'aria-selected': providerId(provider) === selectedProviderId ? 'true' : 'false',
    });
    const label = node('span', { class: 'mimo-provider-option-name' }, providerName(provider));
    const methodTypes = new Set((provider.methods || []).map(method => method.type));
    const hint = node(
      'span',
      { class: 'mimo-provider-option-hint' },
      methodTypes.has('oauth') && methodTypes.has('api')
        ? 'Browser login or API key'
        : methodTypes.has('oauth')
          ? 'Browser login'
          : 'API key',
    );
    item.append(label, hint);
    item.addEventListener('click', () => selectProvider(providerId(provider)));
    return item;
  }));
  if (!available.length) {
    menu.append(node(
      'p',
      { class: 'admin-toggle-sub mimo-provider-no-results' },
      needle ? 'No matching providers.' : 'No more providers to connect.',
    ));
  }
}

function hideProviderMenu() {
  const menu = document.getElementById('mimo-provider-menu');
  const input = document.getElementById('mimo-provider-search');
  menu?.classList.add('hidden');
  input?.setAttribute('aria-expanded', 'false');
}

function showProviderMenu(query = '') {
  const menu = document.getElementById('mimo-provider-menu');
  const input = document.getElementById('mimo-provider-search');
  renderProviderMenu(query);
  menu?.classList.remove('hidden');
  input?.setAttribute('aria-expanded', 'true');
}

function selectProvider(id, { scroll = false } = {}) {
  const provider = publicProviders().find(item => providerId(item) === String(id || '').toLowerCase());
  if (!provider) return;
  selectedProviderId = providerId(provider);
  explicitSelection = true;
  const input = document.getElementById('mimo-provider-search');
  if (input) input.value = providerName(provider);
  hideProviderMenu();
  renderList(
    document.getElementById('mimo-provider-list'),
    [provider],
    'No provider selected.',
  );
  renderProviderMenu('');
  if (scroll) {
    root?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
    input?.focus?.();
    input?.select?.();
  }
}

function render() {
  const catalogue = publicProviders();
  const available = catalogue.filter(provider => !provider.connected);
  const connected = catalogue.filter(provider => (
    provider.connected || Number(provider.included_free_models || 0) > 0
  ));
  // Directory-by-default: show every unconnected provider until the user
  // explicitly picks one (search menu or deep link). Auto-selecting the
  // first provider collapsed the directory into a single card and made the
  // rest of the catalogue invisible.
  const selected = explicitSelection
    ? available.find(provider => providerId(provider) === selectedProviderId)
    : null;
  if (!selected) {
    explicitSelection = false;
    selectedProviderId = '';
  }
  const search = document.getElementById('mimo-provider-search');
  if (search && document.activeElement !== search) {
    search.value = selected ? providerName(selected) : '';
  }
  renderList(
    document.getElementById('mimo-provider-list'),
    selected ? [selected] : available,
    'No more providers to connect.',
  );
  renderProviderMenu('');
  renderList(
    document.getElementById('mimo-connected-provider-list'),
    connected,
    'No connected API providers.',
    true,
  );
  return {
    available: available.length,
    connected: catalogue.filter(provider => provider.connected).length,
    included: connected.filter(provider => !provider.connected).reduce(
      (total, provider) => total + Number(provider.included_free_models || 0),
      0,
    ),
  };
}

function setProviderCounts(counts) {
  writeStatus(
    'mimo-provider-status',
    `${counts.available} provider${counts.available === 1 ? '' : 's'} available to connect`,
  );
  writeStatus(
    'mimo-connected-provider-status',
    `${counts.connected} API provider${counts.connected === 1 ? '' : 's'} connected`
      + (counts.included ? ` · ${counts.included} model included` : ''),
  );
}

export async function load() {
  if (!root) return;
  setStatus('Loading providers…');
  try {
    const data = await request('');
    providers = Array.isArray(data.providers) ? data.providers : [];
    publishCatalog();
    await modelSharing.load();
    setProviderCounts(render());
  } catch (error) {
    providers = [];
    publishCatalog();
    render();
    setStatus(error.message, true);
  }
}

export function init(options = {}) {
  root = document.getElementById('mimo-provider-directory');
  if (!root || _bound) return;
  _bound = true;
  onCatalogChanged = options.onCatalogChanged || onCatalogChanged;
  document.getElementById('mimo-provider-refresh')?.addEventListener('click', load);
  const search = document.getElementById('mimo-provider-search');
  search?.addEventListener('focus', () => {
    search.select?.();
    showProviderMenu('');
  });
  search?.addEventListener('click', () => showProviderMenu(''));
  search?.addEventListener('input', () => {
    selectedProviderId = '';
    explicitSelection = false;
    showProviderMenu(search.value);
  });
  search?.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.stopPropagation();
      hideProviderMenu();
      const selected = publicProviders().find(provider => providerId(provider) === selectedProviderId);
      search.value = selected ? providerName(selected) : '';
      search.blur?.();
    }
  });
  document.addEventListener?.('click', event => {
    if (!root?.contains?.(event.target)) hideProviderMenu();
  });
  document.addEventListener?.('open-clank:select-provider-account', event => {
    selectProvider(event.detail?.providerId, { scroll: true });
  });
}

export default { init, load, selectProvider };
