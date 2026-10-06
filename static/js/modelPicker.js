import { uiIcon } from './uiIcons.js';
// Model Picker — chatbox model selector dropdown
// Extracted from sessions.js

import { providerLogo } from './providers.js';
import uiModule from './ui.js';
import settingsModule from './settings.js';
import { sortModelObjects } from './modelSort.js';
import {
  providerDisplayName,
  sharedProviderLabel,
  sharedSecondaryLabel,
} from './modelLabels.js';
// Shared secondary attribution remains available to callers as `Shared by ${sharedBy}`.
import {
  catalogHasModelChoice,
  catalogEntries,
  modelChoiceKey,
  modelStateKey,
  resolveStoredModelChoices,
} from './modelCatalog.js';

const API_BASE = window.location.origin;

// ── Recent + Favorites persistence ──
// Recent is auto-tracked (last 5 picks, most-recent-first) and lives in its
// own key. Favorites is the SAME key the sidebar Models section uses, so a
// favorite toggled here shows up there and vice-versa.
const RECENT_KEY = 'odysseus-model-recent';
const FAVORITES_KEY = 'odysseus-model-favorites';
const RECENT_MAX = 5;
// Catalogs at or below this size are small enough that hiding everything
// behind search would be a regression — keep listing them in browse mode.
const BROWSE_ALL_LIMIT = 12;
const PRESET_LABELS = Object.freeze({
  standard: 'Standard',
  fast: 'Fast preset',
  pro: 'Pro preset',
});
const PRESET_ORDER = Object.freeze({ standard: 0, fast: 1, pro: 2 });
const THINKING_LABELS = Object.freeze({
  default: 'Default',
  none: 'None',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Maximum',
  ultra: 'Ultra',
});
const THINKING_ORDER = Object.freeze({
  default: 0, none: 1, minimal: 2, low: 3, medium: 4,
  high: 5, xhigh: 6, max: 7, ultra: 8,
});

function _loadList(key) {
  try {
    const a = JSON.parse(localStorage.getItem(modelStateKey(key)) || '[]');
    return Array.isArray(a) ? a : [];
  } catch { return []; }
}
function _saveList(key, list) {
  try { localStorage.setItem(modelStateKey(key), JSON.stringify(list)); } catch { /* quota / private mode */ }
}
function _loadRecent() { return _loadList(RECENT_KEY); }
function _pushRecent(model) {
  if (!model?.mid) return;
  const key = modelChoiceKey(model);
  const next = _loadRecent().filter(x => x !== key && x !== model.mid);
  next.unshift(key);
  _saveList(RECENT_KEY, next.slice(0, RECENT_MAX));
}
function _loadFavorites() { return _loadList(FAVORITES_KEY); }
function _toggleFavorite(model) {
  const key = modelChoiceKey(model);
  const favs = _loadFavorites();
  const i = favs.indexOf(key);
  const legacy = favs.indexOf(model.mid);
  if (i >= 0) favs.splice(i, 1);
  else {
    if (legacy >= 0) favs.splice(legacy, 1);
    favs.push(key);
  }
  _saveList(FAVORITES_KEY, favs);
  // Keep the sidebar Models section (same key) in sync if it's mounted.
  try {
    if (window.modelsModule && typeof window.modelsModule.refreshModels === 'function') {
      window.modelsModule.refreshModels();
    }
  } catch { /* sidebar not present */ }
  return i < 0; // true when now favorited
}

function _pickerModelKey(m) {
  if (!m) return '';
  return `${m.endpointId || m.url || m.epName || 'model'}::${m.mid || ''}`;
}

function _modelFamilyKey(m) {
  const baseModelId = m?.baseModelId || m?.mid || '';
  return `${m?.endpointId || m?.url || 'model'}::${baseModelId}`;
}

function _groupModelChoices(models) {
  const groups = new Map();
  (models || []).forEach(model => {
    const key = _modelFamilyKey(model);
    let group = groups.get(key);
    if (!group) {
      group = { key, baseModelId: model.baseModelId || model.mid, choices: [] };
      groups.set(key, group);
    }
    group.choices.push(model);
  });
  return [...groups.values()].map(group => {
    const base = group.choices.find(choice => (
      choice.mid === group.baseModelId && !choice.preset && !choice.variant
    )) || group.choices.find(choice => !choice.preset && !choice.variant)
      || group.choices[0];
    return { ...base, ...group, display: base.display, choices: group.choices };
  });
}

function _presetKey(choice) {
  return String(choice?.preset || 'standard').toLowerCase();
}

function _variantKey(choice) {
  return String(choice?.variant || 'default').toLowerCase();
}

function _presetLabel(value) {
  const key = String(value || 'standard').toLowerCase();
  return PRESET_LABELS[key] || key.replace(/[-_]+/g, ' ').replace(/^./, letter => letter.toUpperCase());
}

function _thinkingLabel(value) {
  const key = String(value || 'default').toLowerCase();
  return THINKING_LABELS[key] || key.replace(/[-_]+/g, ' ').replace(/^./, letter => letter.toUpperCase());
}

// ── Shared keyboard nav for model pickers ──
function _handlePickerKeydown(e, listEl, itemSelector, closeFn) {
  if (e.key === 'Escape') { closeFn(); return; }
  if (e.key === 'Enter') {
    e.preventDefault();
    const active = listEl.querySelector(itemSelector + '.kb-active') || listEl.querySelector(itemSelector);
    if (active) active.click();
    return;
  }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    e.preventDefault();
    const items = [...listEl.querySelectorAll(itemSelector)].filter(el => (
      el.style.display !== 'none' && !el.hidden && !el.closest('[hidden]')
    ));
    if (!items.length) return;
    const cur = items.findIndex(el => el.classList.contains('kb-active'));
    items.forEach(el => el.classList.remove('kb-active'));
    let next;
    if (e.key === 'ArrowDown') next = cur < items.length - 1 ? cur + 1 : 0;
    else next = cur > 0 ? cur - 1 : items.length - 1;
    items[next].classList.add('kb-active');
    items[next].scrollIntoView({ block: 'nearest' });
  }
}

// Dependencies injected via initModelPicker()
let _deps = null;
let _defaultChatPickInFlight = false;
let _defaultChatRefreshQueued = false;
let _defaultPendingSeq = 0;

function _modelExists(modelId, url, endpointId = '') {
  if (!modelId || !window.modelsModule || !window.modelsModule.getCachedItems) return false;
  const items = window.modelsModule.getCachedItems() || [];
  if (!items.length) return true;
  return catalogHasModelChoice(items, modelId, endpointId, url);
}

function _firstAvailableModel() {
  if (!window.modelsModule || !window.modelsModule.getCachedItems) return null;
  const items = window.modelsModule.getCachedItems() || [];
  for (const item of items) {
    if (item.offline) continue;
    const entries = catalogEntries(item);
    if (!entries.length) continue;
    return {
      url: item.url,
      modelId: entries[0].mid,
      endpointId: item.endpoint_id || '',
    };
  }
  return null;
}

async function _ensureModelCacheForFallback() {
  if (!window.modelsModule || !window.modelsModule.getCachedItems) return;
  const items = window.modelsModule.getCachedItems() || [];
  if (items.length) return;
  if (typeof window.modelsModule.refreshModels === 'function') {
    try { await window.modelsModule.refreshModels(false); } catch (_) {}
  }
}

async function _ensureDefaultPendingChat(force = false) {
  if (!_deps) return;
  if (_defaultChatPickInFlight) {
    if (force) _defaultChatRefreshQueued = true;
    return;
  }
  if (_deps.getCurrentSessionId && _deps.getCurrentSessionId()) return;
  let pending = _deps.getPendingChat && _deps.getPendingChat();
  if (pending && pending.modelId && pending.source === 'manual') return;
  _defaultChatPickInFlight = true;
  const seq = ++_defaultPendingSeq;
  try {
    let dc = null;
    try {
      const response = await fetch(`${API_BASE}/api/default-chat`, { credentials: 'same-origin' });
      if (response.ok) dc = await response.json();
    } catch (_) {}
    if (!dc || !dc.endpoint_url || !dc.model) {
      try {
        dc = window.__odysseusDefaultChat
          || JSON.parse(localStorage.getItem('odysseus-default-chat-cache') || 'null');
      } catch (_) {}
    } else {
      try {
        window.__odysseusDefaultChat = dc;
        localStorage.setItem('odysseus-default-chat-cache', JSON.stringify(dc));
      } catch (_) {}
    }
    // Cache/default fetches yield. Never let their stale snapshot replace a
    // model the user picked while they were in flight.
    if (seq !== _defaultPendingSeq) return;
    if (_deps.getCurrentSessionId && _deps.getCurrentSessionId()) return;
    pending = _deps.getPendingChat && _deps.getPendingChat();
    if (pending && pending.modelId && pending.source === 'manual') return;
    if (dc && dc.endpoint_url && dc.model && _modelExists(dc.model, dc.endpoint_url, dc.endpoint_id)) {
      const latest = _deps.getPendingChat && _deps.getPendingChat();
      const pendingUrl = String((pending && pending.url) || '').replace(/\/+$/, '');
      const defaultUrl = String(dc.endpoint_url || '').replace(/\/+$/, '');
      _deps.setPendingChat({
        url: dc.endpoint_url,
        modelId: dc.model,
        endpointId: dc.endpoint_id || '',
        source: 'default',
      });
      if (!latest || latest.modelId !== dc.model || pendingUrl !== defaultUrl || latest.source !== 'default') {
        updateModelPicker();
      }
      return;
    }
    if (pending && pending.modelId) return;
    await _ensureModelCacheForFallback();
    // No configured default, or the configured default is gone/offline:
    // preserve the convenience fallback and keep the picker usable.
    const fallback = _firstAvailableModel();
    if (fallback) {
      if (seq !== _defaultPendingSeq) return;
      const latest = _deps.getPendingChat && _deps.getPendingChat();
      if (latest && latest.modelId && latest.source !== 'default' && latest.source !== 'fallback') return;
      _deps.setPendingChat({ ...fallback, source: 'fallback' });
      updateModelPicker();
    }
  } finally {
    _defaultChatPickInFlight = false;
    if (_defaultChatRefreshQueued) {
      _defaultChatRefreshQueued = false;
      _ensureDefaultPendingChat();
    }
  }
}

/**
 * Initialize the model picker dropdown.
 * @param {Object} deps
 * @param {function} deps.getCurrentSessionId - returns current session ID
 * @param {function} deps.getSessions - returns sessions array
 * @param {function} deps.getPendingChat - returns _pendingChat object
 * @param {function} deps.setPendingChat - sets _pendingChat object
 * @param {function} deps.createDirectChat - creates a new direct chat session
 */
export function initModelPicker(deps) {
  _deps = deps;
  _initModelPickerDropdown();
}

function _initModelPickerDropdown() {
  const wrap = document.getElementById('model-picker-wrap');
  const btn = document.getElementById('model-picker-btn');
  const menu = document.getElementById('model-picker-menu');
  const search = document.getElementById('model-picker-search');
  const listEl = document.getElementById('model-picker-list');
  const searchRow = menu ? menu.querySelector('.model-picker-search-row') : null;
  const refreshBtn = document.getElementById('model-picker-refresh-btn');
  if (!wrap || !btn || !menu || !search || !listEl) return;
  if (wrap.dataset.modelPickerBound === '1') return;
  wrap.dataset.modelPickerBound = '1';

  function _close() {
    if (menu.classList.contains('hidden')) return;
    // Restore scroll button
    const _scrollBtn = document.getElementById('scroll-bottom-btn');
    if (_scrollBtn) _scrollBtn.style.display = '';
    menu.classList.add('closing');
    menu.addEventListener('animationend', function _onDone() {
      menu.removeEventListener('animationend', _onDone);
      menu.classList.remove('closing');
      menu.classList.add('hidden');
      search.value = '';
    }, { once: true });
    // Fallback if animationend doesn't fire
    setTimeout(() => {
      if (!menu.classList.contains('hidden')) {
        menu.classList.remove('closing');
        menu.classList.add('hidden');
        search.value = '';
      }
    }, 200);
  }

  function _openPickerShortcut(kind) {
    _close();
    try {
      if (kind === 'cookbook') {
        if (window.cookbookModule && typeof window.cookbookModule.open === 'function') {
          window.cookbookModule.open();
        } else {
          const btn = document.getElementById('tool-cookbook-btn') || document.getElementById('rail-cookbook');
          if (btn) btn.click();
          else location.hash = '#cookbook';
        }
      } else if (kind === 'settings') {
        if (settingsModule && typeof settingsModule.open === 'function') settingsModule.open();
      } else if (window.adminModule && typeof window.adminModule.open === 'function') {
        window.adminModule.open('services');
      } else if (settingsModule && typeof settingsModule.open === 'function') {
        settingsModule.open('services');
      }
    } catch (_) {}
  }

  let _pickerLoading = false;
  let _pickerLoadSeq = 0;

  function _getAllModels() {
    const items = (window.modelsModule && window.modelsModule.getCachedItems) ? window.modelsModule.getCachedItems() : [];
    const result = [];
    const seen = new Set();
    items.forEach(item => {
      // Previously: offline endpoints were skipped entirely, so a server
      // that briefly went down disappeared from the picker — confusing
      // when the user can still see it (offline-tagged) in Settings.
      // Now: include offline-endpoint models too but flag them
      // `stale: true` so the row renderer dims them + shows the offline
      // pill. The user can still click and try anyway (matches the
      // existing "local server appears offline" path on line 301).
      const epOffline = !!item.offline;
      const entries = catalogEntries(item);
      entries.forEach(entry => {
        const mid = entry.mid;
        const choiceKey = modelChoiceKey(mid, item.endpoint_id, item.url);
        // Deduplicate by model ID — prefer ONLINE endpoint entries over
        // offline duplicates so the user gets a working endpoint first
        // when the same model is exposed by both.
        if (seen.has(choiceKey)) return;
        seen.add(choiceKey);
        // Catalog-boundary rows (entry.family set) carry their own public
        // identity: the transport/endpoint behind them is invisible to users.
        const shared = item.shared === true;
        const boundary = !!entry.family || !!entry.providerDisplayName || !!item.provider_display_name;
        const providerName = providerDisplayName(
          entry.providerDisplayName || item.provider_display_name || entry.family,
        );
        const sharedBy = shared ? String(item.shared_by || '').trim() : '';
        const sharedLabel = shared
          ? sharedSecondaryLabel({ label: item.share_label, owner: sharedBy })
          : '';
        result.push({
          key: choiceKey,
          mid,
          display: (entry.displayName || mid).split('/').pop(),
          baseModelId: entry.baseModelId || null,
          preset: entry.preset || null,
          variant: entry.variant || null,
          family: boundary ? providerName : null,
          providerFamilyId: entry.providerFamilyId || item.provider_family_id || null,
          providerName,
          shared,
          sharedBy,
          shareLabel: item.share_label || null,
          extra: !!entry.extra,
          url: item.url,
          endpointId: item.endpoint_id,
          category: item.category || '',
          epName: sharedLabel || (boundary ? '' : (item.endpoint_name || '')),
          providerText: boundary ? [shared ? sharedProviderLabel(providerName) : providerName, sharedLabel].filter(Boolean).join(' ') : [
            item.endpoint_name || '',
            sharedLabel,
            item.category || '',
            item.host || '',
            item.url || '',
          ].filter(Boolean).join(' '),
          stale: entry.stale || epOffline,
          staleReason: entry.stale
            ? 'catalog entry is stale'
            : (epOffline ? (item.ping_error || 'provider route offline') : ''),
          offline: epOffline,
        });
      });
    });
    return sortModelObjects(result);
  }

  function _hasModelCache() {
    try {
      return !!(window.modelsModule && window.modelsModule.getCachedItems && (window.modelsModule.getCachedItems() || []).length);
    } catch (_) {
      return false;
    }
  }

  function _renderLoading(text = 'Loading models…') {
    listEl.innerHTML = '';
    listEl.classList.remove('is-empty');
    listEl.classList.add('is-loading');
    menu.classList.remove('no-models');
    if (search) search.placeholder = text;
    let row = null;
    try {
      row = spinnerModule.createLoadingRow(text, 15);
    } catch (_) {
      row = document.createElement('div');
      row.className = 'model-switch-empty';
      row.textContent = text;
    }
    row.classList.add('model-picker-loading-row');
    listEl.appendChild(row);
  }

  async function _refreshPickerModels({ force = false, showLoading = false } = {}) {
    if (!window.modelsModule || typeof window.modelsModule.refreshModels !== 'function') return;
    const seq = ++_pickerLoadSeq;
    _pickerLoading = true;
    if (showLoading) _renderLoading(force ? 'Refreshing models…' : 'Loading models…');
    try {
      await window.modelsModule.refreshModels(force);
    } finally {
      if (seq === _pickerLoadSeq) {
        _pickerLoading = false;
        listEl.classList.remove('is-loading');
      }
    }
  }

  // ── Provider display names and grouping ──
  const _PROVIDER_NAMES = {
    '01-ai': 'Yi', 'abacusai': 'Abacus AI', 'adept': 'Adept',
    'ai21': 'AI21 Labs', 'ai21labs': 'AI21 Labs', 'aion-labs': 'Aion Labs',
    'aisingapore': 'AI Singapore', 'allenai': 'Allen AI', 'amazon': 'Amazon',
    'anthracite-org': 'Anthracite', 'anthropic': 'Anthropic', 'arcee-ai': 'Arcee AI',
    'baai': 'BAAI', 'baidu': 'Baidu', 'bigcode': 'BigCode',
    'black-forest-labs': 'Black Forest Labs', 'bytedance': 'ByteDance',
    'bytedance-seed': 'ByteDance', 'cognitivecomputations': 'Cognitive Computations',
    'cohere': 'Cohere', 'databricks': 'Databricks', 'deepcogito': 'DeepCogito',
    'deepseek': 'DeepSeek', 'deepseek-ai': 'DeepSeek', 'essentialai': 'Essential AI',
    'google': 'Google', 'gryphe': 'Gryphe', 'ibm': 'IBM',
    'ibm-granite': 'IBM Granite', 'inception': 'Inception',
    'inclusionai': 'Inclusion AI', 'inflection': 'Inflection',
    'kwaipilot': 'KwaiPilot', 'liquid': 'Liquid AI', 'mancer': 'Mancer',
    'meta': 'Llama', 'meta-llama': 'Llama', 'microsoft': 'Microsoft',
    'minimax': 'MiniMax', 'minimaxai': 'MiniMax', 'mistralai': 'Mistral',
    'moonshotai': 'Moonshot', 'morph': 'Morph', 'nex-agi': 'Nex AGI',
    'nousresearch': 'Nous Research', 'nv-mistralai': 'NVIDIA x Mistral',
    'nvidia': 'NVIDIA', 'openai': 'OpenAI', 'openrouter': 'OpenRouter',
    'perceptron': 'Perceptron', 'perplexity': 'Perplexity', 'poolside': 'Poolside',
    'prime-intellect': 'Prime Intellect', 'qwen': 'Qwen', 'rekaai': 'Reka',
    'relace': 'Relace', 'sao10k': 'Sao10k', 'sarvamai': 'Sarvam AI',
    'snowflake': 'Snowflake', 'stepfun': 'StepFun', 'stepfun-ai': 'StepFun',
    'stockmark': 'Stockmark', 'switchpoint': 'SwitchPoint', 'tencent': 'Tencent',
    'thedrummer': 'TheDrummer', 'undi95': 'Undi95', 'upstage': 'Upstage',
    'writer': 'Writer', 'x-ai': 'xAI', 'xiaomi': 'Xiaomi',
    'z-ai': 'Zhipu', 'zyphra': 'Zyphra',
    '~anthropic': 'Anthropic', '~google': 'Google',
    '~moonshotai': 'Moonshot', '~openai': 'OpenAI',
  };
  const _PROVIDER_ALIAS = {
    'meta-llama': 'meta', 'deepseek': 'deepseek-ai', 'minimaxai': 'minimax',
    'stepfun-ai': 'stepfun', 'ai21labs': 'ai21', 'ibm-granite': 'ibm',
    'bytedance-seed': 'bytedance', '~anthropic': 'anthropic',
    '~google': 'google', '~moonshotai': 'moonshotai', '~openai': 'openai',
  };
  function _providerDisplayName(slug) {
    return _PROVIDER_NAMES[slug] || slug.charAt(0).toUpperCase() + slug.slice(1).replace(/-/g, ' ');
  }
  function _providerGroupKey(m) {
    if (m && m.category && m.category !== 'local' && m.epName) {
      return `~endpoint:${m.epName}`;
    }
    return _providerSlug((m && m.mid) || '');
  }
  function _providerGroupName(key) {
    if (String(key || '').startsWith('~endpoint:')) return String(key).slice('~endpoint:'.length);
    return _providerDisplayName(key);
  }
  function _providerSlug(mid) {
    const slash = mid.indexOf('/');
    let slug = slash > 0 ? mid.substring(0, slash) : 'other';
    return _PROVIDER_ALIAS[slug] || slug;
  }
  // Group label: catalog-boundary rows carry a display-ready `family` from
  // the backend; prefixed ids ("provider/model") use the provider name; bare
  // ids (direct endpoints, local servers) group under their endpoint's name
  // instead of all piling into "Other".
  function _groupLabel(m) {
    if (m.shared) return sharedProviderLabel(m.providerName);
    if (m.family) return m.family;
    if (m.mid.indexOf('/') > 0) return _providerDisplayName(_providerSlug(m.mid));
    return m.epName || 'Other';
  }
  const _collapsedProviders = new Set(_loadList('odysseus-model-collapsed'));
  let _justExpandedProvider = null;

  document.addEventListener('openclank:auth-user-ready', () => {
    _collapsedProviders.clear();
    _loadList('odysseus-model-collapsed').forEach(provider => _collapsedProviders.add(provider));
    if (!menu.classList.contains('hidden')) _populate(search.value);
  });
  document.addEventListener('openclank:model-catalog-updated', () => {
    updateModelPicker();
    if (!menu.classList.contains('hidden')) _populate(search.value || '');
  });
  window.addEventListener('openclank:default-chat-changed', () => {
    if (!_deps || (_deps.getCurrentSessionId && _deps.getCurrentSessionId())) return;
    const pending = _deps.getPendingChat && _deps.getPendingChat();
    if (pending && pending.modelId && pending.source === 'manual') return;
    _defaultPendingSeq++;
    _deps.setPendingChat(null);
    _ensureDefaultPendingChat(true);
    updateModelPicker();
  });

  function _populate(filter) {
    listEl.innerHTML = '';
    listEl.classList.remove('is-loading');
    const allChoices = _getAllModels();
    const all = _groupModelChoices(allChoices);
    const q = (filter || '').trim().toLowerCase();
    const hasAnyModel = all.length > 0;
    listEl.classList.toggle('is-empty', !hasAnyModel);
    menu.classList.toggle('no-models', !hasAnyModel);
    if (search) {
      search.placeholder = hasAnyModel ? 'Search models…' : 'No models connected';
    }
    if (searchRow) {
      searchRow.classList.toggle('searching', !!q);
    }

    if (!hasAnyModel) return; // collapsed empty list — nothing to render

    // Unique lookup so Recent/Favorites (stored as bare model IDs) can be
    // resolved back to full model objects; drops anything no longer offered.
    const byId = new Map();
    allChoices.forEach(m => {
      byId.set(modelChoiceKey(m), m);
      if (!byId.has(m.mid)) byId.set(m.mid, m);
    });

    const groupByChoice = new Map();
    all.forEach(group => group.choices.forEach(choice => {
      groupByChoice.set(modelChoiceKey(choice), group);
    }));
    const favs = resolveStoredModelChoices(_loadFavorites(), allChoices);
    const recent = resolveStoredModelChoices(_loadRecent(), allChoices);
    _saveList(FAVORITES_KEY, favs);
    _saveList(RECENT_KEY, recent);

    function _addSection(label) {
      const el = document.createElement('div');
      el.className = 'mp-section-label';
      el.textContent = label;
      listEl.appendChild(el);
    }
    function _addEmpty(text) {
      const empty = document.createElement('div');
      empty.className = 'model-switch-empty';
      empty.textContent = text;
      listEl.appendChild(empty);
    }
    function _activeChoice(group) {
      let modelId = '';
      let endpointId = '';
      try {
        const currentSessionId = _deps.getCurrentSessionId();
        const session = _deps.getSessions().find(item => item.id === currentSessionId);
        const pending = _deps.getPendingChat();
        modelId = session?.model || pending?.modelId || '';
        endpointId = session?.endpoint_id || pending?.endpointId || '';
      } catch (_) {}
      return group.choices.find(choice => {
        if (choice.mid !== modelId) return false;
        if (endpointId === 'mimo:auto') return String(choice.endpointId || '').startsWith('mimo:');
        return !endpointId || String(choice.endpointId || '') === String(endpointId);
      }) || null;
    }

    function _addRow(m, preferredChoice = null) {
      const family = document.createElement('div');
      family.className = 'mp-model-family';
      family.dataset.modelFamily = m.baseModelId || m.mid;
      const row = document.createElement('div');
      row.className = 'model-switch-item';
      if (m.stale) {
        row.classList.add('model-switch-stale');
        row.style.opacity = '0.45';
        row.title = `Local server appears offline: ${m.staleReason}. Click to try anyway, or relaunch in Cookbook.`;
      }
      const _mlogo = providerLogo(m.mid);
      if (_mlogo) {
        const logoSpan = document.createElement('span');
        logoSpan.className = 'provider-logo';
        logoSpan.style.opacity = '0.6';
        logoSpan.innerHTML = _mlogo;
        row.appendChild(logoSpan);
      }
      const nameSpan = document.createElement('span');
      nameSpan.className = 'mp-model-name';
      nameSpan.textContent = m.display;
      // Long model names are clipped with ellipsis — expose the full name on
      // hover so the suffix/variant tag is still discoverable (#1982).
      nameSpan.title = m.display;
      row.appendChild(nameSpan);
      // Offline state is already conveyed by the row's reduced opacity —
      // a redundant "offline" pill on top of that just added clutter.
      // (Class kept on `row` so the opacity rule still applies; the text
      // badge is gone.)
      const epSpan = document.createElement('span');
      epSpan.className = 'model-switch-ep';
      // Don't show endpoint name if it matches the model name (local self-hosted)
      const _epDisplay = m.epName && !m.display.toLowerCase().includes(m.epName.toLowerCase().split('/').pop()) ? m.epName : '';
      epSpan.textContent = _epDisplay;
      row.appendChild(epSpan);

      const choices = m.choices?.length ? m.choices : [m];
      const baseChoice = choices.find(choice => (
        choice.mid === (m.baseModelId || m.mid) && !choice.preset && !choice.variant
      )) || choices.find(choice => !choice.preset && !choice.variant) || choices[0];
      const initialChoice = (preferredChoice && choices.includes(preferredChoice) ? preferredChoice : null)
        || _activeChoice(m)
        || baseChoice;

      const presetGroups = new Map();
      choices.forEach(choice => {
        const preset = _presetKey(choice);
        if (!presetGroups.has(preset)) presetGroups.set(preset, []);
        presetGroups.get(preset).push(choice);
      });
      const hasOptions = choices.length > 1;
      if (hasOptions) {
        const optionsButton = document.createElement('button');
        optionsButton.type = 'button';
        optionsButton.className = 'mp-model-options-button';
        optionsButton.textContent = 'Options';
        optionsButton.setAttribute('aria-expanded', 'false');
        optionsButton.setAttribute('aria-label', `Choose mode and thinking for ${m.display}`);
        row.appendChild(optionsButton);

        const panel = document.createElement('div');
        panel.className = 'mp-model-options';
        panel.hidden = true;
        const controls = document.createElement('div');
        controls.className = 'mp-model-option-controls';

        const modeWrap = document.createElement('label');
        modeWrap.className = 'mp-model-option-field';
        modeWrap.appendChild(document.createTextNode('Mode'));
        const modeSelect = document.createElement('select');
        modeSelect.setAttribute('aria-label', `${m.display} mode`);
        [...presetGroups.keys()].sort((a, b) => (
          (PRESET_ORDER[a] ?? 99) - (PRESET_ORDER[b] ?? 99)
          || a.localeCompare(b)
        )).forEach(preset => {
          const option = document.createElement('option');
          option.value = preset;
          option.textContent = _presetLabel(preset);
          modeSelect.appendChild(option);
        });
        modeSelect.value = _presetKey(initialChoice);
        modeWrap.appendChild(modeSelect);
        modeWrap.hidden = presetGroups.size < 2;

        const thinkingWrap = document.createElement('label');
        thinkingWrap.className = 'mp-model-option-field';
        thinkingWrap.appendChild(document.createTextNode('Thinking'));
        const thinkingSelect = document.createElement('select');
        thinkingSelect.setAttribute('aria-label', `${m.display} thinking`);
        thinkingWrap.appendChild(thinkingSelect);

        const useButton = document.createElement('button');
        useButton.type = 'button';
        useButton.className = 'mp-model-use-button';
        useButton.textContent = 'Use';

        const choicesForMode = () => presetGroups.get(modeSelect.value) || [];
        const renderThinking = (wanted = '') => {
          const modeChoices = [...choicesForMode()].sort((a, b) => {
            const left = _variantKey(a);
            const right = _variantKey(b);
            return (THINKING_ORDER[left] ?? 99) - (THINKING_ORDER[right] ?? 99)
              || left.localeCompare(right);
          });
          const prior = wanted || thinkingSelect.value || 'default';
          thinkingSelect.replaceChildren();
          modeChoices.forEach(choice => {
            const option = document.createElement('option');
            option.value = _variantKey(choice);
            option.textContent = _thinkingLabel(option.value);
            thinkingSelect.appendChild(option);
          });
          thinkingSelect.value = modeChoices.some(choice => _variantKey(choice) === prior)
            ? prior
            : (_variantKey(modeChoices[0]) || 'default');
          thinkingWrap.hidden = modeChoices.length < 2;
        };
        renderThinking(_variantKey(initialChoice));
        modeSelect.addEventListener('change', event => {
          event.stopPropagation();
          renderThinking('default');
        });
        thinkingSelect.addEventListener('change', event => event.stopPropagation());
        useButton.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          const selected = choicesForMode().find(choice => (
            _variantKey(choice) === thinkingSelect.value
          )) || choicesForMode()[0];
          if (selected) _pick(selected);
        });
        optionsButton.addEventListener('click', event => {
          event.preventDefault();
          event.stopPropagation();
          panel.hidden = !panel.hidden;
          optionsButton.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
          if (!panel.hidden) (modeWrap.hidden ? thinkingSelect : modeSelect).focus();
        });
        panel.addEventListener('click', event => event.stopPropagation());
        controls.append(modeWrap, thinkingWrap, useButton);
        panel.appendChild(controls);
        family.append(row, panel);
      } else {
        family.appendChild(row);
      }

      // Inline favorite dot — toggles the logical model family, never picks it.
      const favDot = document.createElement('button');
      favDot.type = 'button';
      const familyChoiceKeys = choices.map(modelChoiceKey);
      let isFavorite = familyChoiceKeys.some(key => favs.includes(key));
      favDot.className = 'mp-fav-dot' + (isFavorite ? ' active' : '');
      favDot.textContent = '●';
      const _setFavState = (on) => {
        favDot.classList.toggle('active', on);
        favDot.title = on ? 'Remove from favorites' : 'Add to favorites';
        favDot.setAttribute('aria-label', on ? 'Remove from favorites' : 'Add to favorites');
        favDot.setAttribute('aria-pressed', on ? 'true' : 'false');
      };
      _setFavState(isFavorite);
      favDot.addEventListener('click', (e) => {
        e.stopPropagation();
        const nowFav = !isFavorite;
        familyChoiceKeys.forEach(key => {
          const index = favs.indexOf(key);
          if (index >= 0) favs.splice(index, 1);
        });
        if (nowFav) favs.push(modelChoiceKey(initialChoice));
        _saveList(FAVORITES_KEY, favs);
        isFavorite = nowFav;
        _setFavState(nowFav);
        favDot.classList.remove('pulse');
        void favDot.offsetWidth;
        favDot.classList.add('pulse');
        if (uiModule && uiModule.showToast) uiModule.showToast(nowFav ? 'Favorited' : 'Unfavorited');
        // In browse mode the Favorites section membership changed — rebuild
        // (cheap: Recent + Favorites). In search mode the row stays put, so
        // the in-place favorite update above is enough.
        if (!q) {
          const st = listEl.scrollTop;
          _populate('');
          listEl.scrollTop = st;
        }
      });
      row.appendChild(favDot);

      row.addEventListener('click', () => _pick(baseChoice));
      listEl.appendChild(family);
      return family;
    }

    // ── Search mode: flat, filtered results across the whole catalog ──
    if (q) {
      const matches = all.filter(m => {
        const choiceText = m.choices.flatMap(choice => [
          choice.mid,
          choice.display,
          _presetLabel(_presetKey(choice)),
          _thinkingLabel(_variantKey(choice)),
        ]);
        return [m.baseModelId, m.display, m.epName, m.providerText, _groupLabel(m), ...choiceText]
          .filter(Boolean).join(' ').toLowerCase().includes(q);
      });
      if (matches.length === 0) _addEmpty('No matching models');
      else matches.forEach(_addRow);
      return;
    }

    // ── Browse mode: Favorites (manual) + Recent (auto), with dedupe. ──
    // Rules:
    //   1. Never list the same model twice in the dropdown. Favorites
    //      win over Recent (if you favorited it, that's where it
    //      belongs — Recent shouldn't show it again as duplicate).
    //   2. Small catalogs (≤ BROWSE_ALL_LIMIT total) skip the Recent
    //      section entirely — when there's only ~10 models, the whole
    //      list fits below as "All models" and a separate Recent
    //      section just duplicates rows.
    // Transport variants remain exact selectable routes, but browse as one
    // logical model with compact Mode/Thinking controls.
    const browsable = all;

    const shown = new Set();
    const groupedStoredChoices = stored => {
      const seen = new Set();
      return stored.map(id => {
        const choice = byId.get(id);
        const group = choice ? groupByChoice.get(modelChoiceKey(choice)) : null;
        if (!group || seen.has(group.key)) return null;
        seen.add(group.key);
        return { group, choice };
      }).filter(Boolean);
    };
    const favModels = groupedStoredChoices(favs);
    if (favModels.length) {
      _addSection('Favorites');
      favModels.forEach(({ group, choice }) => { shown.add(group.key); _addRow(group, choice); });
    }
    // Recent: only render when the catalog is big enough that surfacing
    // a recency shortlist is actually useful, AND only models that
    // aren't already in Favorites (dedupe).
    if (browsable.length > BROWSE_ALL_LIMIT) {
      const recentModels = groupedStoredChoices(recent)
        .filter(({ group }) => !shown.has(group.key))
        .slice(0, RECENT_MAX);
      if (recentModels.length) {
        _addSection('Recent');
        recentModels.forEach(({ group, choice }) => { shown.add(group.key); _addRow(group, choice); });
      }
    }

    // Small catalogs: still list everything so users aren't forced to search.
    if (browsable.length <= BROWSE_ALL_LIMIT) {
      const rest = browsable.filter(m => !shown.has(m.key));
      if (rest.length) {
        if (shown.size) _addSection('All models');
        rest.forEach(_addRow);
      }
    } else {
      // Large catalog: collapsible family/provider groups.
      const rest = browsable.filter(m => !shown.has(m.key));
      const groups = new Map();
      rest.forEach(m => {
        const provider = _providerGroupKey(m);
        if (!groups.has(provider)) groups.set(provider, []);
        groups.get(provider).push(m);
      });
      const sorted = [...groups.keys()].sort((a, b) =>
        _providerGroupName(a).localeCompare(_providerGroupName(b)));

      sorted.forEach(provider => {
        const models = groups.get(provider);
        const isCollapsed = _collapsedProviders.has(provider);
        const header = document.createElement('div');
        header.className = 'mp-provider-header';
        header.innerHTML =
          uiIcon('chevron-down', 10, { className: `mp-provider-chevron${isCollapsed ? ' collapsed' : ''}` })
          + `<span class="mp-provider-name">${_providerGroupName(provider)}</span>`
          + `<span class="mp-provider-count">${models.length}</span>`;
        header.addEventListener('click', (e) => {
          e.stopPropagation();
          if (_collapsedProviders.has(provider)) {
            _collapsedProviders.delete(provider);
            _justExpandedProvider = provider;
          } else {
            _collapsedProviders.add(provider);
            _justExpandedProvider = null;
          }
          _saveList('odysseus-model-collapsed', [..._collapsedProviders]);
          const st = listEl.scrollTop;
          _populate('');
          listEl.scrollTop = st;
        });
        listEl.appendChild(header);
        if (!isCollapsed) {
          const group = document.createElement('div');
          group.className = 'mp-provider-group' + (_justExpandedProvider === provider ? ' mp-just-expanded' : '');
          models.forEach(m => {
            _addRow(m);
            // Move the just-appended row into the group container
            group.appendChild(listEl.lastElementChild);
          });
          listEl.appendChild(group);
          if (_justExpandedProvider === provider) _justExpandedProvider = null;
        }
      });
    }
  }

async function _pick(m) {
    _defaultPendingSeq++;
    try {
      window.__odysseusLastPickedRoute = {
        model: m.mid || '',
        endpoint_url: m.url || '',
        endpoint_id: m.endpointId || '',
        display: m.display || m.mid || '',
        picked_at: Date.now(),
      };
    } catch (_) {}
    let switchDone = null;
    const switchPromise = new Promise(resolve => { switchDone = resolve; });
    try { window.__odysseusModelSwitchPromise = switchPromise; } catch (_) {}
    const finishSwitch = () => {
      try {
        if (switchDone) switchDone();
        if (window.__odysseusModelSwitchPromise === switchPromise) delete window.__odysseusModelSwitchPromise;
      } catch (_) {}
    };
    try {
      const currentSessionId = _deps.getCurrentSessionId();
      const _pendingChat = _deps.getPendingChat();
      const _completePick = () => {
        if (m && m.mid) _pushRecent(m);
        try { document.dispatchEvent(new CustomEvent('odysseus:model-picked', { detail: m })); } catch {}
        updateModelPicker();
        uiModule.showToast(`Using ${m.display}`);
      };

    // Blur search input before closing to dismiss keyboard on mobile
    if (document.activeElement) document.activeElement.blur();
    _close();
    // Refocus main textarea — skip on mobile to avoid keyboard bounce
    if (window.innerWidth >= 768) {
      const _ta = document.getElementById('message');
      if (_ta) setTimeout(() => _ta.focus(), 50);
    }
    if (!currentSessionId && _pendingChat) {
      // Already have a deferred session — just update the model
      _deps.setPendingChat({ url: m.url, modelId: m.mid, endpointId: m.endpointId, source: 'manual' });
      // Header stays as session name — model switch only updates picker
      _completePick();
      return;
    } else if (!currentSessionId) {
      // No session yet — create one with this model
      try {
        await _deps.createDirectChat(m.url, m.mid, m.endpointId);
      } catch (e) {
        uiModule.showError('Failed to start chat: ' + e);
        finishSwitch();
        return;
      }
    } else {
      // Existing session with no model — PATCH it
      const sessions = _deps.getSessions();
      const s = sessions.find(x => x.id === currentSessionId);
      if (s) { s.model = m.mid; s.endpoint_url = m.url; s.endpoint_id = m.endpointId || s.endpoint_id || ''; }
      updateModelPicker();
      const fd = new FormData();
      fd.append('model', m.mid);
      fd.append('endpoint_url', m.url);
      if (m.endpointId) fd.append('endpoint_id', m.endpointId);
      try {
        const res = await fetch(`${API_BASE}/api/session/${currentSessionId}`, { method: 'PATCH', body: fd });
        let payload = {};
        try { payload = await res.json(); } catch (_) {}
        if (!res.ok) {
          uiModule.showError(payload.detail || 'Failed to set model');
          return;
        }
        const sessions = _deps.getSessions();
        const s = sessions.find(x => x.id === currentSessionId);
        if (s) {
          s.model = payload.model || m.mid;
          s.endpoint_url = payload.endpoint_url || m.url;
          s.endpoint_id = payload.endpoint_id || m.endpointId || '';
        }
        // Header stays as session name — model info shown in picker only
      } catch (e) {
        uiModule.showError('Failed to set model: ' + e);
        finishSwitch();
        return;
      }
    }
      // Persist and broadcast only after the selection actually succeeded.
      _completePick();
    } finally {
      // Chat waits for this fence before POSTing. Every success and failure
      // path must release it or the composer deadlocks before the request.
      finishSwitch();
    }
  }

  document.addEventListener('odysseus:auto-select-model', async (e) => {
    const detail = (e && e.detail) || {};
    const currentSessionId = _deps.getCurrentSessionId();
    const sessions = _deps.getSessions();
    const current = sessions.find(x => x.id === currentSessionId);
    const pending = _deps.getPendingChat();
    if ((current && current.model) || (pending && pending.modelId && pending.source === 'manual')) return;

    if (window.modelsModule && window.modelsModule.refreshModels) {
      try { await window.modelsModule.refreshModels(false); } catch (_) {}
    }
    const items = window.modelsModule && window.modelsModule.getCachedItems ? window.modelsModule.getCachedItems() : [];
    const targetEndpointId = detail.endpointId ? String(detail.endpointId) : '';
    const targetModel = detail.modelId || '';
    let match = null;
    for (const item of items) {
      if (item.offline) continue;
      if (targetEndpointId && String(item.endpoint_id || '') !== targetEndpointId) continue;
      const entries = catalogEntries(item);
      const idx = targetModel ? entries.findIndex(entry => entry.mid === targetModel) : (entries.length ? 0 : -1);
      if (idx >= 0) {
        match = {
          mid: entries[idx].mid,
          display: (entries[idx].displayName || entries[idx].mid).split('/').pop(),
          url: item.url || detail.url || '',
          endpointId: item.endpoint_id || detail.endpointId || '',
          epName: item.shared
            ? sharedSecondaryLabel({ label: item.share_label, owner: item.shared_by })
            : (item.endpoint_name || detail.endpointName || ''),
          providerName: providerDisplayName(
            entries[idx].providerDisplayName || item.provider_display_name || entries[idx].family,
          ),
          shared: item.shared === true,
          providerText: [
            item.shared
              ? sharedProviderLabel(item.provider_display_name || entries[idx].providerDisplayName || entries[idx].family)
              : (item.endpoint_name || detail.endpointName || ''),
            item.url || detail.url || '',
          ].filter(Boolean).join(' '),
        };
        break;
      }
    }
    if (!match && detail.modelId && detail.url) {
      match = {
        mid: detail.modelId,
        display: String(detail.modelId).split('/').pop(),
        url: detail.url,
        endpointId: detail.endpointId || '',
        epName: detail.endpointName || '',
        providerText: [detail.endpointName || '', detail.url || ''].filter(Boolean).join(' '),
      };
    }
    if (match) await _pick(match);
  });

  btn.addEventListener('pointerdown', (e) => {
    e.stopPropagation();
  });
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (menu.classList.contains('hidden') || menu.classList.contains('closing')) {
      // Force-clear any in-progress close animation
      menu.classList.remove('closing', 'hidden');
      const hasCache = _hasModelCache();
      if (hasCache) {
        _populate('');
      } else {
        _renderLoading('Loading models…');
      }
      if (window.modelsModule && window.modelsModule.refreshModels) {
        // Force the cheap /api/models cache refresh when the picker opens.
        // This does not wait on provider probes; the backend returns cached
        // inventory and starts refresh work separately. Without this, models
        // enabled in Added Models can be absent from the chatbox picker until
        // the tab's frontend cache ages out.
        _refreshPickerModels({ force: hasCache, showLoading: !hasCache }).then(() => {
          if (!menu.classList.contains('hidden')) _populate(search.value || '');
          updateModelPicker();
        }).catch(() => {});
      }
      if (window.innerWidth >= 768) search.focus();
      // Hide scroll button so it doesn't overlap
      const _scrollBtn = document.getElementById('scroll-bottom-btn');
      if (_scrollBtn) _scrollBtn.style.display = 'none';
    } else {
      _close();
    }
  });

  search.addEventListener('input', () => {
    if (_pickerLoading) return;
    _populate(search.value);
  });
  search.addEventListener('click', (e) => e.stopPropagation());
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      refreshBtn.disabled = true;
      refreshBtn.classList.add('spinning');
      try {
        await _refreshPickerModels({ force: true, showLoading: true });
        if (!menu.classList.contains('hidden')) _populate(search.value || '');
        updateModelPicker();
      } catch (_) {
        uiModule.showToast('Model refresh failed');
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove('spinning');
      }
    });
  }
  search.addEventListener('keydown', (e) => {
    _handlePickerKeydown(e, listEl, '.model-switch-item', _close);
  });
  const addModelsBtn = document.getElementById('model-picker-add-models-btn');
  if (addModelsBtn) {
    addModelsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _openPickerShortcut('models');
    });
  }
  document.addEventListener('click', (e) => {
    if (!menu.classList.contains('hidden') && !wrap.contains(e.target)) {
      _close();
    }
  });
}

/**
 * Update the model picker label to show the current model.
 * Always visible — shows current model name or "Select model" if none.
 * Called after selectSession, createDirectChat, and model switch.
 */
export function updateModelPicker() {
  if (!_deps) return;
  const label = document.getElementById('model-picker-label');
  if (!label) return;
  // Hide model picker when group chat is active
  const wrap = document.getElementById('model-picker-wrap');
  if (window.groupModule && window.groupModule.isActive()) {
    if (wrap) { wrap.style.display = 'none'; }
    return;
  }
  // Reset inline visibility (may have been hidden by typing in previous session)
  if (wrap) {
    wrap.style.display = '';
    wrap.style.opacity = '';
    wrap.style.pointerEvents = '';
  }
  const currentSessionId = _deps.getCurrentSessionId();
  const sessions = _deps.getSessions();
  const _pendingChat = _deps.getPendingChat();
  const s = sessions.find(x => x.id === currentSessionId);
  let modelId = null;
  if (s && s.model) {
    modelId = s.model;
    if (!_modelExists(modelId, s.endpoint_url || '', s.endpoint_id || '')) {
      modelId = null;
    }
  } else if (_pendingChat && _pendingChat.modelId) {
    modelId = _pendingChat.modelId;
    if (!_modelExists(modelId, _pendingChat.url || '', _pendingChat.endpointId || '')) {
      _deps.setPendingChat(null);
      modelId = null;
    }
  }
  // Deliberately do not turn the first account-scoped favorite into a default.
  // Favorites are picker organization, not an authoritative route for a new
  // server session. With no session model or pending pick, keep the explicit
  // "Select model" placeholder below.
  //
  const latestPending = _deps.getPendingChat && _deps.getPendingChat();
  if (
    !currentSessionId &&
    window.modelsModule &&
    window.modelsModule.getCachedItems &&
    (!modelId || (latestPending && latestPending.source === 'fallback'))
  ) {
    _ensureDefaultPendingChat();
  }

  // Effort-tier variant ids (provider/model/tier) must not degenerate to the
  // bare tier name — "low ⌄" told the user nothing. Same display rule as the
  // picker rows: "model (tier)".
  const _TIER_NAMES = new Set(['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'standard', 'lite']);
  let displayName = 'Select model';
  if (modelId) {
    const parts = modelId.split('/');
    displayName = parts.length >= 3 && _TIER_NAMES.has(parts[parts.length - 1])
      ? `${parts[parts.length - 2]} (${parts[parts.length - 1]})`
      : parts[parts.length - 1];
  }
  // The header indicator clips long names with ellipsis; show the full model
  // identifier on hover (#1982). No tooltip on the "Select model" placeholder.
  label.title = modelId || '';
  const logo = modelId ? providerLogo(modelId) : null;
  if (logo) {
    label.innerHTML = '<span class="model-picker-logo">' + logo + '</span> ' + displayName;
  } else {
    label.textContent = displayName;
  }
}
