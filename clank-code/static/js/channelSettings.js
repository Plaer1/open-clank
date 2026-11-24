// Chat channels settings form (Integrations tab): switches, secret entry, pairing approvals, allow-lists. Secrets are write-only.
export const CHANNEL_LABELS = { telegram: 'Telegram', matrix: 'Matrix (unencrypted rooms)', discord: 'Discord (not yet tried against a real account)',
  feishu: 'Feishu / Lark (UNVERIFIED against the real service)', wecom: 'WeCom (not live verified)' };
export const CHANNEL_FIELDS = {
  telegram: { secrets: [['token', 'Bot token']], settings: [], bools: [['attachments_in', 'Accept photos and documents']] },
  discord: { secrets: [['token', 'Bot token']], settings: [], bools: [['attachments_in', 'Accept photos and documents']] },
  feishu: { secrets: [['app_secret', 'App secret']], settings: [['app_id', 'App ID'], ['domain', 'Domain: feishu or lark']], bools: [['attachments_in', 'Accept photos and documents']] },
  wecom: { secrets: [['bot_secret', 'Bot secret']], settings: [['bot_id', 'Bot ID']], bools: [['attachments_in', 'Accept photos and documents']] },
  matrix: { secrets: [['access_token', 'Access token']], settings: [['homeserver', 'Homeserver URL']], bools: [['attachments_in', 'Accept photos and documents']] },
};

/** The channel's provider route is both halves or nothing; a half-chosen pair stays unresolved ("Choose a model"). */
export function routeChoice(endpointId, model) {
  return endpointId && model ? { endpoint_id: endpointId, model } : {};
}

const CHANNEL_SHORT = { telegram: 'Telegram', matrix: 'Matrix', discord: 'Discord', feishu: 'Feishu', wecom: 'WeCom' };
/** Accessible names and empty choices of the two route selects, per channel. */
export function routeLabels(name) {
  const who = CHANNEL_SHORT[name] || name;
  return { connection: `${who} connection`, model: `${who} model`, noConnection: 'Choose a connection', noModel: 'Choose a model' };
}

export function statusLine(status) {
  if (!status) return 'Unavailable';
  if (!status.configured) return 'Not configured';
  return status.enabled ? 'On' : 'Off';
}

export function pairingRows(status) {
  return (status?.pending || []).map(p => ({ code: p.code, text: `${p.label || p.sender} · code ${p.code} · expires in ${Math.max(1, Math.round(p.expires_in / 60))} min` }));
}

/** Request body for saving: empty secret boxes are left out so a stored secret is never overwritten by accident. */
export function configBody(name, enabled, values) {
  const spec = CHANNEL_FIELDS[name];
  const body = { enabled: !!enabled, secrets: {}, settings: {} };
  for (const [key] of spec.secrets) if (values[key]) body.secrets[key] = values[key];
  for (const [key] of spec.settings) if (values[key]) body.settings[key] = values[key];
  Object.assign(body.settings, routeChoice(values.endpoint_id, values.model));
  for (const [key] of spec.bools || []) if (key in values) body.settings[key] = values[key] ? 'true' : 'false';
  return body;
}

async function api(path, body) {
  const response = await fetch('/api/channels' + path, { method: body ? 'POST' : 'GET', credentials: 'same-origin', cache: 'no-store',
    headers: body ? { 'Content-Type': 'application/json' } : undefined, body: body ? JSON.stringify(body) : undefined });
  if (!response.ok) throw new Error('Channel settings request failed');
  return response.json();
}

/** Connection + model selects from the same chooser as the AI Defaults panel. No first-available fallback. */
async function modelRow(name, saved, values) {
  const names = routeLabels(name);
  const row = node('div', null, { className: 'channel-model-row' }); row.style.cssText = ROW;
  const epSel = node('select'), modelSel = node('select');
  epSel.setAttribute('aria-label', names.connection); modelSel.setAttribute('aria-label', names.model);
  epSel.append(node('option', names.noConnection, { value: '' })); modelSel.append(node('option', names.noModel, { value: '' }));
  row.append(node('span', 'Model for this channel '), epSel, modelSel);
  let endpoints = [];
  try {
    const { modelChooser: c } = await import('./settings.js');
    endpoints = await c.fetchEndpoints();
    const fillModels = (selected) => c.fillModels(modelSel, c.catalog(endpoints.find(e => e.id === epSel.value)), selected, true);
    c.fillEndpoints(epSel, endpoints, saved.endpoint_id || '', true);
    fillModels(saved.model || '');
    values.endpoint_id = epSel.value; values.model = modelSel.value;
    epSel.addEventListener('change', () => { fillModels(''); values.endpoint_id = epSel.value; values.model = modelSel.value; });
    modelSel.addEventListener('change', () => { values.model = modelSel.value; });
  } catch (_) { row.append(node('span', ' Models unavailable.')); }
  return row;
}

const ROW = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap';
// Checkbox rows never wrap the box away from its text.
const CHECK = 'display:flex;align-items:flex-start;gap:8px';

function node(tag, text, attrs = {}) { const n = document.createElement(tag); if (text != null) n.textContent = text; Object.assign(n, attrs); return n; }

export async function mountChannelSettings(root) {
  if (!root) return;
  const draw = async (state) => {
    root.replaceChildren();
    const global = node('label', ' Allow chat channels (Off by default; nothing connects until a channel is switched on and configured)');
    const box = node('input', null, { type: 'checkbox', checked: !!state.global_enabled });
    box.addEventListener('change', async () => draw(await api('/global', { enabled: box.checked })));
    global.prepend(box); root.append(global);
    for (const [name, label] of Object.entries(CHANNEL_LABELS)) {
      const status = state.channels[name], card = node('section', null, { className: 'channel-card' }); card.style.cssText = 'display:grid;gap:8px;margin:12px 0;padding:12px;border:1px solid var(--border);border-radius:8px'; card.append(node('h4', `${label} · ${statusLine(status)}`));
      const values = {}; const spec = CHANNEL_FIELDS[name];
      for (const [key, caption] of [...spec.settings, ...spec.secrets]) {
        const secret = spec.secrets.some(([k]) => k === key);
        const input = node('input', null, { type: secret ? 'password' : 'text', autocomplete: 'off', placeholder: secret ? (status.configured ? 'Saved (hidden); type to replace' : '') : (status.settings?.[key] || '') });
        input.addEventListener('input', () => { values[key] = input.value.trim(); });
        const row = node('label', caption + ' '); row.style.cssText = ROW; input.style.flex = '1'; row.append(input); card.append(row);
      }
      card.append(await modelRow(name, status.settings || {}, values));
      for (const [key, caption] of spec.bools || []) {
        const box2 = node('input', null, { type: 'checkbox', checked: status.settings?.[key] === 'true' });
        box2.addEventListener('change', () => { values[key] = box2.checked; });
        const row2 = node('label', ' ' + caption + ' (images and plain documents only)'); row2.style.cssText = CHECK; box2.style.flex = 'none'; row2.prepend(box2); card.append(row2);
      }
      const enable = node('input', null, { type: 'checkbox', checked: !!status.enabled });
      const enableRow = node('label', ' Switch this channel on'); enableRow.style.cssText = CHECK; enable.style.flex = 'none'; enableRow.prepend(enable); card.append(enableRow);
      const save = node('button', 'Save', { type: 'button', className: 'admin-btn-sm' });
      save.addEventListener('click', async () => draw(await api(`/${name}/config`, configBody(name, enable.checked, values))));
      card.append(save);
      for (const row of pairingRows(status)) {
        const line = node('div', row.text + ' '); 
        for (const [action, text] of [['approve', 'Approve'], ['dismiss', 'Dismiss']]) {
          const b = node('button', text, { type: 'button', className: 'admin-btn-sm' });
          b.addEventListener('click', async () => draw(await api(`/${name}/pairing`, { action, code: row.code }))); line.append(b);
        }
        card.append(line);
      }
      card.append(node('p', `Allowed people: ${status.senders.join(', ') || 'none'}.`, { className: 'settings-hint' }));
      const groupEvidence = status.group_mention_evidence;
      const groupHint = groupEvidence === 'supported' ? 'Mention detection is implemented; live delivery is not verified.' :
        groupEvidence === 'unavailable' ? 'Group delivery is unavailable: mention detection is not established.' : 'Group availability is unknown.';
      card.append(node('p', groupHint, { className: 'settings-hint' }));
      for (const id of status.groups || []) {
        const line = node('div', id + ' ');
        const remove = node('button', 'Remove group', { type: 'button', className: 'admin-btn-sm' });
        remove.addEventListener('click', async () => draw(await api(`/${name}/allow`, { kind: 'group', id, allowed: false })));
        line.append(remove); card.append(line);
      }
      const group = node('input', null, { type: 'text', placeholder: 'Group or room id to allow', disabled: groupEvidence !== 'supported' });
      const addGroup = node('button', 'Allow group', { type: 'button', className: 'admin-btn-sm', disabled: groupEvidence !== 'supported' });
      addGroup.addEventListener('click', async () => { if (group.value.trim()) draw(await api(`/${name}/allow`, { kind: 'group', id: group.value.trim(), allowed: true })); });
      card.append(group, addGroup);
      root.append(card);
    }
  };
  try { await draw(await api('')); } catch (_) { root.replaceChildren(node('p', 'Channel settings are unavailable.')); }
}
