// Native MiMo provider authentication, surfaced inside Odysseus Settings.

let root;
let providers = [];
let activeAbort;
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

function setStatus(message, error = false) {
  const status = root?.querySelector('#mimo-provider-status');
  if (!status) return;
  status.textContent = message || '';
  status.style.color = error ? 'var(--red, #ff5555)' : '';
}

async function request(path, options = {}) {
  const response = await fetch(`/api/mimo/providers${path}`, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || 'MiMo provider operation failed');
  return data;
}

async function changed(message) {
  setStatus(message);
  await onCatalogChanged();
  await load();
}

function closeFlow() {
  if (activeAbort) activeAbort.abort();
  activeAbort = undefined;
  const flow = root?.querySelector('#mimo-provider-flow');
  if (flow) flow.replaceChildren();
}

function flowShell(provider, title) {
  closeFlow();
  const flow = root.querySelector('#mimo-provider-flow');
  const card = node('div', { class: 'admin-card' });
  const heading = node('h2', {}, `${provider.name} — ${title}`);
  const body = node('div', { class: 'settings-col' });
  const actions = node('div', { class: 'settings-row' });
  const cancel = node('button', { type: 'button', class: 'btn secondary' }, 'Cancel');
  cancel.addEventListener('click', closeFlow);
  actions.append(cancel);
  card.append(heading, body, actions);
  flow.append(card);
  return { body, actions };
}

function promptVisible(prompt, values) {
  if (!prompt.when) return true;
  const match = values[prompt.when.key] === prompt.when.value;
  return prompt.when.op === 'eq' ? match : !match;
}

async function beginOAuth(provider, method) {
  const values = {};
  const { body, actions } = flowShell(provider, method.label);
  const fields = [];
  (method.prompts || []).forEach(prompt => {
    const row = node('label', { class: 'settings-row' });
    row.append(node('span', { class: 'settings-label' }, prompt.message || prompt.key));
    let input;
    if (prompt.type === 'select') {
      input = node('select', { class: 'settings-select' });
      (prompt.options || []).forEach(option => {
        const opt = node('option', { value: option.value }, option.hint ? `${option.label} — ${option.hint}` : option.label);
        input.append(opt);
      });
    } else {
      input = node('input', { class: 'settings-input', type: 'text', placeholder: prompt.placeholder || '' });
    }
    values[prompt.key] = input.value;
    input.addEventListener('input', () => {
      values[prompt.key] = input.value;
      fields.forEach(item => { item.row.hidden = !promptVisible(item.prompt, values); });
    });
    fields.push({ prompt, row, input });
    row.append(input);
    body.append(row);
  });
  fields.forEach(item => { item.row.hidden = !promptVisible(item.prompt, values); });
  const start = node('button', { type: 'button', class: 'btn primary' }, 'Continue');
  actions.prepend(start);
  start.addEventListener('click', async () => {
    start.disabled = true;
    setStatus(`Starting ${method.label}…`);
    try {
      const inputs = Object.fromEntries(fields.filter(item => !item.row.hidden).map(item => [item.prompt.key, item.input.value]));
      const authorization = await request(`/${encodeURIComponent(provider.id)}/oauth/authorize`, {
        method: 'POST',
        body: JSON.stringify({ method: method.index, ...(fields.length ? { inputs } : {}) }),
      });
      if (authorization.url) window.open(authorization.url, '_blank', 'noopener,noreferrer');
      await finishOAuth(provider, method, authorization);
    } catch (error) {
      setStatus(error.message, true);
      start.disabled = false;
    }
  });
}

async function finishOAuth(provider, method, authorization) {
  const { body, actions } = flowShell(provider, 'Finish sign-in');
  if (authorization.instructions) body.append(node('p', { class: 'admin-toggle-sub' }, authorization.instructions));
  if (authorization.method === 'code') {
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
          body: JSON.stringify({ method: method.index, code: code.value.trim() }),
        });
        closeFlow();
        await changed(`${provider.name} connected.`);
      } catch (error) {
        setStatus(error.message, true);
        complete.disabled = false;
      }
    });
    return;
  }

  body.append(node('p', { class: 'admin-toggle-sub' }, 'Waiting for authorization in your browser…'));
  activeAbort = new AbortController();
  try {
    await request(`/${encodeURIComponent(provider.id)}/oauth/callback`, {
      method: 'POST',
      body: JSON.stringify({ method: method.index }),
      signal: activeAbort.signal,
    });
    activeAbort = undefined;
    closeFlow();
    await changed(`${provider.name} connected.`);
  } catch (error) {
    if (error.name !== 'AbortError') setStatus(error.message, true);
  }
}

function beginApiKey(provider) {
  const { body, actions } = flowShell(provider, 'API key');
  const row = node('label', { class: 'settings-row' });
  row.append(node('span', { class: 'settings-label' }, 'API key'));
  const key = node('input', { class: 'settings-input', type: 'password', autocomplete: 'off' });
  row.append(key);
  body.append(row, node('p', { class: 'admin-toggle-sub' }, 'Saved by MiMo in its isolated MIMOCODE_HOME. Odysseus never displays it again.'));
  const save = node('button', { type: 'button', class: 'btn primary' }, 'Connect');
  actions.prepend(save);
  save.addEventListener('click', async () => {
    if (!key.value.trim()) return setStatus('Enter an API key.', true);
    save.disabled = true;
    try {
      await request(`/${encodeURIComponent(provider.id)}/api-key`, {
        method: 'PUT', body: JSON.stringify({ key: key.value.trim() }),
      });
      key.value = '';
      closeFlow();
      await changed(`${provider.name} connected.`);
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
    await changed(`${provider.name} disconnected.`);
  } catch (error) {
    setStatus(error.message, true);
    button.disabled = false;
  }
}

function render() {
  const list = root.querySelector('#mimo-provider-list');
  const query = (root.querySelector('#mimo-provider-search')?.value || '').trim().toLowerCase();
  list.replaceChildren();
  providers.filter(provider => !query || `${provider.name} ${provider.id}`.toLowerCase().includes(query)).forEach(provider => {
    const card = node('div', { class: 'admin-card' });
    const title = node('h2', {}, provider.family ? `${provider.name} — serves “${provider.family}” models` : provider.name);
    let statusText = provider.id;
    if (provider.connected) {
      statusText = 'Connected';
      if (provider.chat_models) statusText += ` · ${provider.chat_models} chat model${provider.chat_models === 1 ? '' : 's'}`;
      if (provider.served_by && provider.served_by.endpoint_name) {
        statusText += ` · standing by — “${provider.served_by.endpoint_name}” serves these models directly`;
      } else if (provider.active) {
        statusText += ' · live in the model picker';
      }
    }
    const status = node('span', { class: 'admin-toggle-sub' }, statusText);
    const actions = node('div', { class: 'settings-row' });
    if (provider.connected) {
      const remove = node('button', { type: 'button', class: 'btn secondary' }, 'Disconnect');
      remove.addEventListener('click', () => disconnect(provider, remove));
      actions.append(remove);
    } else {
      (provider.methods || []).forEach(method => {
        const button = node('button', { type: 'button', class: 'btn secondary' }, method.label);
        button.addEventListener('click', () => method.type === 'oauth' ? beginOAuth(provider, method) : beginApiKey(provider));
        actions.append(button);
      });
    }
    card.append(title, status, actions);
    list.append(card);
  });
  if (!list.children.length) list.append(node('p', { class: 'admin-toggle-sub' }, 'No matching providers.'));
}

export async function load() {
  if (!root) return;
  setStatus('Loading MiMo providers…');
  try {
    const data = await request('');
    providers = Array.isArray(data.providers) ? data.providers : [];
    setStatus(`${providers.length} native providers · credentials live in the MiMo runtime`);
    render();
  } catch (error) {
    providers = [];
    setStatus(error.message, true);
    render();
  }
}

export function init(options = {}) {
  root = document.querySelector('[data-settings-panel="mimo-providers"]');
  if (!root || root.dataset.bound === '1') return;
  root.dataset.bound = '1';
  onCatalogChanged = options.onCatalogChanged || onCatalogChanged;
  root.querySelector('#mimo-provider-refresh')?.addEventListener('click', load);
  root.querySelector('#mimo-provider-search')?.addEventListener('input', render);
}

export default { init, load };
