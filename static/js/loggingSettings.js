import { setUiIconText } from './uiIcons.js';
// Account-scoped logging settings; archive/Lore/memory retention keep their owners.
import { openStatsUsage } from './statsUsage.js';
function el(tag, text = '', cls = '') { const node = document.createElement(tag); node.textContent = text; node.className = cls; return node; }
const controlIcons = Object.freeze({
  'Apply prune preview':'check', 'Save reviewed retention rule':'save', 'Preview retention':'preview',
  'Disable advanced logging':'pause', 'Enable advanced logging':'play', 'Save display preferences':'save',
  'Provider accounts':'provider', 'Prices and cost coverage':'usage', 'Memory and Recall controls':'memory',
  'Lore history budgets':'restore', 'Optional content Trends':'usage', 'Open logs and export':'terminal',
  'Reload settings':'refresh',
});
function button(text, action) {
  const node = el('button', text, 'admin-btn-sm'); node.type = 'button';
  if (controlIcons[text]) setUiIconText(node, controlIcons[text], text);
  node.addEventListener('click', action); return node;
}
async function api(path, body = null, method = 'POST', signal = null) {
  const response = await fetch('/api/logging/v1' + path, { credentials: 'same-origin', signal, ...(body ? { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}) });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(value.detail?.message || value.detail?.error || value.detail || `Logging request failed (${response.status})`);
  return value;
}
export function mountLoggingSettings(root) {
  if (!root || root.dataset.loggingMounted) return;
  root._loggingDispose?.(); const lifetime = new AbortController(); const token = Symbol('logging-settings'); root._loggingInstance = token; root.dataset.loggingMounted = 'true'; let policy = null; let generation = 0;
  const owned = () => root.isConnected && root._loggingInstance === token;
  const request = (path, body = null, method = 'POST') => api(path, body, method, lifetime.signal);
  const status = el('p', '', 'logging-status'); status.setAttribute('role', 'status');
  const controls = el('div'); const health = el('section', '', 'logging-health');
  root.replaceChildren(el('h2', 'Logging'), el('p', 'Open Clank activity and usage logging is always on. Advanced provider capture is optional and starts off.', 'settings-hint'), status, controls, health);
  async function run(action) { if (!owned()) return; root.setAttribute('aria-busy', 'true'); status.textContent = 'Loading…'; try { await action(); if (owned()) status.textContent = ''; } catch (error) { if (!owned() || error.name === 'AbortError') return; status.textContent = String(error.message) + ' Reload to review the latest saved policy.'; } finally { if (owned()) root.removeAttribute('aria-busy'); } }
  async function save(patch, extra = {}) { const response = await request('/policy', { policy: patch, expected_revision: policy.revision, ...extra }, 'PUT'); policy = response.policy; await load(); }
  function toggle(label, key, hint, disabled = false) {
    const row = el('label', '', 'logging-toggle'); const input = el('input'); input.type = 'checkbox'; input.checked = policy[key]; input.disabled = disabled;
    input.addEventListener('change', () => run(() => save({ [key]: input.checked })));
    row.append(input, el('span', label)); if (hint) row.append(el('small', hint)); return row;
  }
  function retention(key, label, target) {
    const section = el('section', '', 'logging-retention'); section.append(el('h3', label));
    const modes = el('select'); modes.className = 'settings-select'; modes.setAttribute('aria-label', label);
    for (const [value, text] of [['keep_until_deleted', 'Keep until deleted'], ['age', 'Age limit'], ['size', 'Size limit']]) { const option = el('option', text); option.value = value; modes.append(option); }
    modes.value = policy[key].mode; const bound = el('input'); bound.type = 'number'; bound.min = '1'; bound.className = 'settings-select'; bound.value = policy[key].days || policy[key].max_bytes || 30;
    const units = el('span'); const summary = el('p', '', 'settings-hint'); const apply = button('Apply prune preview', () => run(async () => { await request('/prune/apply', { target, preview_id: preview.preview_id }); await load(); })); apply.hidden = true;
    const saveRule = button('Save reviewed retention rule', () => run(async () => { await save({ [key]: proposed() }, preview ? { retention_previews: { [key]: preview.preview_id } } : {}); })); let preview = null;
    function proposed() { const mode = modes.value; return mode === 'keep_until_deleted' ? { mode } : { mode, [mode === 'age' ? 'days' : 'max_bytes']: Number(bound.value) }; }
    function changed() { preview = null; apply.hidden = true; bound.hidden = modes.value === 'keep_until_deleted'; units.textContent = modes.value === 'age' ? 'days' : modes.value === 'size' ? 'bytes' : ''; bound.setAttribute('aria-label', label + ' ' + units.textContent); summary.textContent = 'Preview the rows affected before saving an age or size rule.'; saveRule.disabled = modes.value !== 'keep_until_deleted'; }
    modes.addEventListener('change', changed); bound.addEventListener('input', changed); changed();
    const previewButton = button('Preview retention', () => run(async () => { preview = await request('/prune/preview', { target, retention: proposed() }); const affected = preview.preview || {}; summary.textContent = `${affected.body_records ?? 'unknown'} capture body records (${affected.body_bytes ?? 'unknown'} bytes) and ${affected.metadata_records ?? 'unknown'} metadata records will be removed. Owner totals: ${affected.owner_body_bytes ?? 'unknown'} body bytes, ${affected.owner_metadata_bytes_estimated ?? 'unknown'} estimated metadata bytes. This pass is bounded to ${affected.bounded_batch_limit ?? 'unknown'} attempts${affected.more_maintenance_required ? '; more maintenance is required' : ''}. Saved chats, Stats facts, Lore, memory and in-flight captures remain protected.`; apply.hidden = false; saveRule.disabled = false; }));
    section.append(modes, bound, units, previewButton, summary, saveRule, apply); return section;
  }
  async function load() {
    const currentGeneration = ++generation;
    const [preferences, current, ordinary] = await Promise.all([request('/policy'), request('/status'), request('/sessions?period=all&limit=1').catch(error => ({ coverage: { state: 'unavailable', reason: error.message } }))]); if (!owned() || currentGeneration !== generation) return; policy = preferences.policy;
    controls.replaceChildren(el('p', 'Ordinary logging · Always on', 'logging-always-on'), el('p', 'Saved conversations, tool evidence and numeric usage continue in both modes. Advanced logging adds optional provider transport detail.', 'settings-hint'));
    const advanced = el('section', '', 'logging-advanced'); advanced.append(el('h3', 'Advanced provider logging'), el('p', 'Captures Open Clank operations only. Sanitized request/response bodies can contain user content. Credentials, authentication fields and cookies are excluded; there is no external history import.', 'settings-hint'));
    const enable = button(policy.advanced_enabled ? 'Disable advanced logging' : 'Enable advanced logging', () => run(() => save({ advanced_enabled: !policy.advanced_enabled }))); enable.setAttribute('aria-pressed', String(policy.advanced_enabled));
    advanced.append(enable, toggle('Sanitized request text and structure', 'request_body_enabled', 'Bodies are captured only while advanced logging is enabled.', !policy.advanced_enabled), toggle('Sanitized response text and events', 'response_body_enabled', 'Timing remains available when bodies are off.', !policy.advanced_enabled));
    const capability = current.effective?.binary_body_capability || preferences.effective?.binary_body_capability;
    advanced.append(toggle('Embedded media bodies', 'binary_body_enabled', capability?.state === 'supported' ? 'Optional bounded JSON base64/data URL capture only. No external URL fetch or multipart/raw capture.' : 'Unsupported by the current capture capability; media bytes remain excluded.', !policy.advanced_enabled || capability?.state !== 'supported'));  controls.append(advanced);
    controls.append(toggle('Live refresh while visible', 'live_refresh', 'Hidden views pause; visible changes are coalesced.'));
    const display = el('div', '', 'logging-display'); const timezone = el('input'); timezone.className = 'settings-select'; timezone.value = policy.timezone; timezone.setAttribute('aria-label', 'Logging timezone');
    const range = el('select'); range.className = 'settings-select'; range.setAttribute('aria-label', 'Initial log range'); for (const value of ['7d', '30d', '90d', 'all']) { const option = el('option', value === 'all' ? 'All retained time' : value.replace('d', ' days')); option.value = value; range.append(option); } range.value = policy.initial_period;
    display.append(el('label', 'Timezone (local or IANA name)'), timezone, el('label', 'Initial range'), range, button('Save display preferences', () => run(() => save({ timezone: timezone.value, initial_period: range.value })))); controls.append(display);
    controls.append(el('h3', 'Captured log retention'), el('p', 'These rules apply to advanced capture data. Lore file history, saved-chat deletion and semantic-memory retention use their own controls.', 'settings-hint'), retention('body_retention', 'Capture bodies', 'advanced_bodies'), retention('metadata_retention', 'Capture metadata', 'advanced_metadata'));
    const related = el('section'); related.append(el('h3', 'Related controls'), button('Provider accounts', () => { void import('./settings.js').then(module => module.open('services')); }), button('Prices and cost coverage', () => openStatsUsage({ view: 'usage', mode: 'cost' })), button('Memory and Recall controls', () => run(async () => { const opener = document.getElementById('tool-memory-btn') || document.getElementById('rail-memory'); if (!opener) throw new Error('Memory entry is unavailable in this host'); opener.click(); })), button('Lore history budgets', () => { void import('./settings.js').then(module => module.open('history')); }), button('Optional content Trends', () => openStatsUsage({ view: 'trends' })), el('p', 'Memory consent and optional content analysis remain separate from logging. Price coverage comes from admitted source schedules; unpriced data stays unpriced.', 'settings-hint')); controls.append(related);
    controls.append(button('Open logs and export', () => openStatsUsage({ view: 'logs', range: policy.initial_period, timezone: policy.timezone })), button('Reload settings', () => run(load)));
    const effective = current.effective || {}; const capture = current.capture || {};
    health.replaceChildren(el('h3', 'Effective logging and storage'), el('p', `Ordinary ${effective.ordinary?.state || 'status unavailable'} · Advanced ${effective.advanced_enabled ? 'on' : 'off'} · selected mode ${effective.transport_mode || 'unavailable'} · policy revision ${effective.policy_revision ?? policy.revision}`));
    health.append(el('p', `Ordinary archive read coverage: ${ordinary.coverage?.state || 'unavailable'}${ordinary.coverage?.reason ? ' · ' + ordinary.coverage.reason : ''}. Usage measurement coverage is reported separately in Usage and each attempt.`, 'settings-hint'));
    health.append(el('p', `${effective.inflight_operations ?? 'unknown'} operations in flight · ${effective.old_policy_inflight ?? 'unknown'} using an older pinned policy${effective.draining ? ' · draining' : ''}. Changes apply at the next operation boundary.`, 'settings-hint'));
    health.append(el('p', `Capture storage: ${capture.storage_state || 'unavailable'} · ${capture.body_bytes ?? 'unknown'} body bytes · ${capture.pending_captures ?? 'unknown'} pending captures`));
    health.append(el('p', `Last successful write: ${capture.last_successful_write || 'none observed'}`, 'settings-hint'));
    health.append(el('p', `Active media capture capability: ${effective.binary_body_capability?.state || 'unavailable'} · Transport formats: ${Object.keys(capture.formats || {}).join(', ') || 'unavailable'}`, 'settings-hint'));
    health.append(el('pre', JSON.stringify({ capture_counts: capture.capture_counts || {}, losses: capture.losses || {}, formats: capture.formats || {}, limits: capture.limits || {} }, null, 2), 'logging-source'));
    health.append(el('p', 'The attempt inspector reports measured milestones and gaps for each captured provider format. Theme and reduced motion follow Open Clank.', 'settings-hint'));
  }
  const refresh = () => { if (owned()) run(load); }; root.addEventListener('logging-settings-refresh', refresh); root._loggingDispose = () => { lifetime.abort(); generation++; root.removeEventListener('logging-settings-refresh', refresh); }; run(load);
}
