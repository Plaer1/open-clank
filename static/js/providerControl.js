import { uiIcon } from './uiIcons.js';
// Unified Open Clank provider control plane.
//
// This browser module is deliberately a projection of /api/v1/providers/**.
// It does not keep a provider registry, retain credentials, or call an
// upstream provider directly. API keys only exist in the password input long
// enough to submit one write-only request.

import { bindMenuDismiss } from './escMenuStack.js';
import { providerLogo, spriteLogo } from './providers.js';
import { topPortalZ } from './toolWindowZOrder.js';
import {
  providerDisplayName,
  sharedProviderLabel,
  sharedSecondaryLabel,
} from './modelLabels.js';

const API_ROOT = '/api/v1/providers';
const FAMILY_CACHE_KEY = 'open-clank:provider-families:v1';
const FAMILY_CACHE_FORMAT = 1;
const FAMILY_CACHE_MAX_AGE_MS = 24 * 60 * 60 * 1000;
const VIEW_FRESH_MAX_AGE_MS = 30 * 1000;
const REQUEST_TIMEOUT_MS = 8 * 1000;
const FAMILY_ENRICH_TIMEOUT_MS = 45 * 1000;
const OAUTH_STATUS_RETRY_BASE_MS = 1200;
const OAUTH_STATUS_RETRY_MAX_MS = 10 * 1000;
const OAUTH_STATUS_RETENTION_MS = 10 * 60 * 1000;
const OAUTH_FLOW_STORAGE_KEY = 'open-clank:provider-oauth-flow:v1';
const FAMILY_PICKER_SEARCH_THRESHOLD = 12;
const PURPOSES = Object.freeze([
  ['chat', 'Chat', ['chat.stream', 'chat.complete']],
  ['utility', 'Utility', ['chat.stream', 'chat.complete']],
  ['memory', 'Memory', ['chat.complete']],
  ['research', 'Research', ['chat.stream', 'chat.complete']],
  ['tasks', 'Tasks', ['chat.stream', 'chat.complete']],
  ['vision', 'Vision', ['vision.describe', 'chat.stream', 'chat.complete']],
  ['images', 'Images', ['image.generate', 'image.edit', 'image.inpaint', 'image.img2img', 'image.upscale', 'image.denoise', 'image.segment', 'image.remove_background', 'image.restore_face']],
  ['tts', 'Text to speech', ['audio.synthesize']],
  ['stt', 'Speech to text', ['audio.transcribe']],
  ['embeddings', 'Embeddings', ['embeddings.create']],
]);

let root = null;
let bound = false;
let loading = null;
let onCatalogChanged = async () => {};
let activeOAuth = null;
let loadGeneration = 0;
const eligibilityLoads = new Map();
const connectionDetailLoads = new Map();
const loadedConnectionAccounts = new Set();
const openConnections = new Set();
const openAccountDetails = new Set();
const openShareChoosers = new Set();
const modelSearchQueries = new Map();
const enrichedFamilyAuthMethods = new Map();
const loadedAt = new Map();
const detailControllers = new Set();
let shareDirectoryLoad = null;
let shareDirectoryLoaded = false;
let bindingsLoad = null;
let bindingsLoaded = false;
let renderedFamilySignature = '';
let familyCatalogKey = '';
let detailEpoch = 0;
let deferredInvalidationGeneration = 0;
let state = emptyState();
let activePicker = null;
let pickerSerial = 0;

const CARET_ICON = uiIcon("chevron-down", 10, {"className":"provider-control-picker-caret"});
const ADD_ICON = uiIcon("add", 15);
const API_ADD_ICON = uiIcon("add", 15);
const LOCAL_ICON = uiIcon("computer", 11);
const API_ICON = uiIcon("network", 11);
const KEY_ICON = uiIcon("key", 13);

function familyLogo(family) {
  if (!family) return '';
  return spriteLogo(family.id)
    || providerLogo(`${family.id || ''} ${family.display_name || ''}`)
    || '';
}

function iconNode(markup = '', className = 'provider-control-provider-logo') {
  const icon = element('span', { class: className, 'aria-hidden': 'true' });
  if (markup) icon.innerHTML = markup;
  else icon.append(element('span', { class: 'provider-control-provider-fallback' }, '•'));
  return icon;
}

function createThemedPicker({
  label,
  options = [],
  value = '',
  placeholder = 'Choose…',
  searchable = false,
  showTriggerHint = true,
  disabled = false,
  className = '',
  menuClassName = '',
  menuAnchor = null,
  onChange = () => {},
} = {}) {
  const pickerId = `provider-control-picker-${++pickerSerial}`;
  const shell = element('div', {
    class: `provider-control-picker${className ? ` ${className}` : ''}`,
    'data-provider-picker': label || pickerId,
  });
  const trigger = element('button', {
    type: 'button',
    class: 'provider-control-picker-trigger',
    role: 'combobox',
    'aria-label': label || 'Choose an option',
    'aria-haspopup': 'listbox',
    'aria-controls': `${pickerId}-menu`,
    'aria-expanded': 'false',
    disabled,
  });
  const current = element('span', { class: 'provider-control-picker-current' });
  trigger.append(current);
  const menu = element('div', {
    id: `${pickerId}-menu`,
    class: `provider-control-picker-menu${menuClassName ? ` ${menuClassName}` : ''} hidden`,
    role: 'listbox',
    'aria-label': label || 'Options',
  });
  const search = searchable
    ? element('input', {
      class: 'provider-control-picker-search',
      type: 'search',
      placeholder: `Search ${String(label || 'options').toLowerCase()}…`,
      autocomplete: 'off',
      'aria-label': `Search ${String(label || 'options').toLowerCase()}`,
    })
    : null;
  const list = element('div', { class: 'provider-control-picker-options' });
  if (search) menu.append(search);
  menu.append(list);
  shell.append(trigger, menu);

  let items = [];
  let selectedValue = '';
  let dismiss = null;

  function selectedOption() {
    return items.find(item => item.value === selectedValue) || null;
  }

  function syncTrigger() {
    const selected = selectedOption();
    const currentChildren = [
      iconNode(selected?.logo || '', 'provider-control-picker-icon'),
      element('span', { class: 'provider-control-picker-label' }, selected?.label || placeholder),
    ];
    if (showTriggerHint && selected?.hint) currentChildren.push(
      element('span', { class: 'provider-control-picker-current-hint' }, selected.hint),
    );
    current.replaceChildren(...currentChildren);
    const caret = element('span', { class: 'provider-control-picker-caret-shell', 'aria-hidden': 'true' });
    caret.innerHTML = CARET_ICON;
    trigger.replaceChildren(current, caret);
  }

  function visibleOptions() {
    return [...(list.children || [])].filter(item => !item.hidden);
  }

  function focusOption(position = 'selected') {
    const visible = visibleOptions();
    if (!visible.length) return;
    let index = position === 'last' ? visible.length - 1 : 0;
    if (position === 'selected') {
      const selectedIndex = visible.findIndex(item => item.dataset.value === selectedValue);
      index = selectedIndex >= 0 ? selectedIndex : 0;
    }
    visible[index]?.focus?.();
  }

  function close({ restoreFocus = false } = {}) {
    if (dismiss) {
      const closeDismiss = dismiss;
      dismiss = null;
      closeDismiss();
      if (restoreFocus) trigger.focus?.();
      return;
    }
    menu.classList?.add?.('hidden');
    trigger.setAttribute('aria-expanded', 'false');
    shell.classList?.remove?.('is-open');
    if (activePicker?.shell === shell) activePicker = null;
    if (restoreFocus) trigger.focus?.();
  }

  function finishClose(restoreFocus = false) {
    menu.classList?.add?.('hidden');
    if (menu.parentNode !== shell) shell.append(menu);
    for (const property of ['position', 'left', 'top', 'width', 'maxHeight', 'zIndex']) {
      menu.style?.removeProperty?.(property.replace(/[A-Z]/g, match => `-${match.toLowerCase()}`));
    }
    trigger.setAttribute('aria-expanded', 'false');
    shell.classList?.remove?.('is-open');
    if (activePicker?.shell === shell) activePicker = null;
    dismiss = null;
    if (restoreFocus) trigger.focus?.();
  }

  function open({ focus = 'selected' } = {}) {
    if (!menu.classList?.contains?.('hidden')) return;
    if (activePicker && activePicker.shell !== shell) activePicker.close();
    if (search) {
      search.value = '';
      for (const item of list.children || []) item.hidden = false;
    }
    menu.classList?.remove?.('hidden');
    trigger.setAttribute('aria-expanded', 'true');
    shell.classList?.add?.('is-open');
    activePicker = { shell, close };
    const anchoredElement = typeof menuAnchor === 'function' ? menuAnchor() : menuAnchor;
    let rect = anchoredElement?.getBoundingClientRect?.() || trigger.getBoundingClientRect?.();
    if (rect && document.body?.append) {
      document.body.append(menu);
      menu.style.position = 'fixed';
      menu.style.zIndex = String(topPortalZ());
      const placeMenu = () => {
        if (menu.classList?.contains?.('hidden')) return;
        // Portaling and the Settings open transition both change the visual
        // anchor geometry. Re-read it before every placement instead of
        // retaining the pre-portal width.
        rect = anchoredElement?.getBoundingClientRect?.() || trigger.getBoundingClientRect?.() || rect;
        const width = Math.max(rect.width, 230);
        const viewportWidth = window.innerWidth || document.documentElement?.clientWidth || width + 16;
        const menuWidth = Math.min(width, viewportWidth - 16);
        menu.style.width = `${menuWidth}px`;
        menu.style.left = `${Math.max(8, Math.min(rect.left, viewportWidth - menuWidth - 8))}px`;
        menu.style.top = `${rect.bottom + 4}px`;
        menu.style?.removeProperty?.('max-height');
        const menuRect = menu.getBoundingClientRect();
        const viewportHeight = window.innerHeight || document.documentElement?.clientHeight || menuRect.bottom + 8;
        if (menuRect.bottom > viewportHeight - 8) {
          const above = rect.top - menuRect.height - 4;
          if (above >= 8) menu.style.top = `${above}px`;
          else {
            menu.style.top = '8px';
            menu.style.maxHeight = `${Math.max(120, viewportHeight - 16)}px`;
          }
        }
      };
      // A few synchronous passes converge flex geometry changed by the
      // portal. A short animation-frame settle keeps the menu attached while
      // the Settings modal finishes its opening transition.
      for (let pass = 0; pass < 3; pass += 1) placeMenu();
      let settleFrames = 12;
      const settlePlacement = () => {
        placeMenu();
        settleFrames -= 1;
        if (settleFrames > 0 && !menu.classList?.contains?.('hidden')) {
          requestAnimationFrame(settlePlacement);
        }
      };
      requestAnimationFrame(settlePlacement);
    }
    dismiss = bindMenuDismiss(
      menu,
      () => finishClose(false),
      event => !shell.contains(event.target) && !menu.contains(event.target),
    );
    if (search) search.focus?.();
    else focusOption(focus);
  }

  function choose(nextValue, { emit = true } = {}) {
    const next = items.find(item => item.value === String(nextValue));
    if (!next) return false;
    const changed = selectedValue !== next.value;
    selectedValue = next.value;
    for (const item of list.children || []) {
      const selected = item.dataset.value === selectedValue;
      item.classList?.toggle?.('is-selected', selected);
      item.setAttribute('aria-selected', selected ? 'true' : 'false');
    }
    syncTrigger();
    if (changed && emit) {
      onChange(selectedValue, next);
      try { shell.dispatchEvent(new Event('change', { bubbles: true })); } catch (_) {}
    }
    return true;
  }

  function drawOptions() {
    const rows = items.map(option => {
      const row = element('button', {
        type: 'button',
        class: 'provider-control-picker-option',
        role: 'option',
        'aria-selected': option.value === selectedValue ? 'true' : 'false',
        'data-value': option.value,
      });
      row.append(
        iconNode(option.logo || '', 'provider-control-picker-option-icon'),
        element('span', { class: 'provider-control-picker-option-copy' }, ''),
      );
      const copy = row.children[1];
      copy.append(element('span', { class: 'provider-control-picker-option-label' }, option.label));
      if (option.hint) copy.append(
        element('span', { class: 'provider-control-picker-option-hint' }, option.hint),
      );
      row.classList?.toggle?.('is-selected', option.value === selectedValue);
      row.addEventListener('click', event => {
        event.stopPropagation?.();
        choose(option.value);
        close({ restoreFocus: true });
      });
      return row;
    });
    if (!rows.length) rows.push(element('p', { class: 'provider-control-picker-empty' }, 'No matching options.'));
    list.replaceChildren(...rows);
  }

  function setOptions(nextOptions, nextValue = selectedValue) {
    items = (nextOptions || []).map(item => ({
      ...item,
      value: String(item.value ?? ''),
      label: String(item.label ?? item.value ?? ''),
      hint: item.hint ? String(item.hint) : '',
      logo: item.logo || '',
      search: String(item.search || `${item.label || ''} ${item.hint || ''} ${item.value || ''}`).toLowerCase(),
    }));
    selectedValue = items.some(item => item.value === String(nextValue))
      ? String(nextValue)
      : (items[0]?.value || '');
    drawOptions();
    syncTrigger();
  }

  function moveFocus(event) {
    const visible = visibleOptions();
    if (!visible.length) return;
    const active = document.activeElement;
    let index = visible.indexOf(active);
    if (event.key === 'Home') index = 0;
    else if (event.key === 'End') index = visible.length - 1;
    else if (event.key === 'ArrowDown') index = index < visible.length - 1 ? index + 1 : 0;
    else if (event.key === 'ArrowUp') index = index > 0 ? index - 1 : visible.length - 1;
    else return;
    event.preventDefault?.();
    visible[index]?.focus?.();
  }

  trigger.addEventListener('click', event => {
    event.stopPropagation?.();
    if (menu.classList?.contains?.('hidden')) open();
    else close();
  });
  trigger.addEventListener('keydown', event => {
    if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
    event.preventDefault?.();
    open({ focus: event.key === 'ArrowUp' || event.key === 'End' ? 'last' : 'selected' });
  });
  menu.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      event.preventDefault?.();
      event.stopPropagation?.();
      close({ restoreFocus: true });
      return;
    }
    if (event.key === 'Enter' && document.activeElement?.dataset?.value) {
      event.preventDefault?.();
      document.activeElement.click?.();
      return;
    }
    moveFocus(event);
  });
  search?.addEventListener('input', () => {
    const needle = search.value.trim().toLowerCase();
    for (const row of list.children || []) {
      const option = items.find(item => item.value === row.dataset.value);
      row.hidden = Boolean(option && needle && !option.search.includes(needle));
    }
  });
  search?.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown') {
      event.preventDefault?.();
      focusOption('selected');
    }
  });

  setOptions(options, value);
  return {
    root: shell,
    trigger,
    get value() { return selectedValue; },
    set value(next) { choose(next, { emit: false }); },
    setOptions,
    choose,
    close,
    focus: () => trigger.focus?.(),
    set disabled(next) {
      trigger.disabled = Boolean(next);
      shell.classList?.toggle?.('is-disabled', Boolean(next));
    },
    get disabled() { return Boolean(trigger.disabled); },
  };
}

function emptyState() {
  return {
    families: [],
    connections: [],
    accounts: new Map(),
    models: [],
    eligibility: new Map(),
    bindings: [],
    shareRecipients: [],
    ownedShares: [],
    receivedShares: [],
  };
}

function validCachedFamily(family) {
  if (!family || typeof family !== 'object' || Array.isArray(family)) return false;
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(String(family.id || ''))) return false;
  if (typeof family.display_name !== 'string' || family.display_name.length > 256) return false;
  for (const key of ['kinds', 'adapters', 'billing_lanes', 'auth_methods']) {
    if (!Array.isArray(family[key]) || family[key].length > 64) return false;
  }
  return family.kinds.every(value => typeof value === 'string' && value.length <= 128)
    && family.adapters.every(value => typeof value === 'string' && value.length <= 128)
    && family.billing_lanes.every(value => typeof value === 'string' && value.length <= 128)
    && family.auth_methods.every(value => (
      (typeof value === 'string' && value.length <= 128)
      || (value && typeof value === 'object' && !Array.isArray(value))
    ));
}

function readCachedFamilies() {
  try {
    const raw = sessionStorage.getItem(FAMILY_CACHE_KEY);
    if (!raw || raw.length > 1_000_000) return [];
    const cached = JSON.parse(raw);
    const age = Date.now() - Number(cached?.cached_at || 0);
    if (
      cached?.format !== FAMILY_CACHE_FORMAT
      || !Number.isInteger(cached.schema_version)
      || cached.schema_version < 1
      || !Number.isFinite(age)
      || age < 0
      || age > FAMILY_CACHE_MAX_AGE_MS
      || !Array.isArray(cached.families)
      || cached.families.length > 256
      || !cached.families.every(validCachedFamily)
    ) {
      sessionStorage.removeItem(FAMILY_CACHE_KEY);
      return [];
    }
    return cached.families;
  } catch (_) {
    try { sessionStorage.removeItem(FAMILY_CACHE_KEY); } catch (_) {}
    return [];
  }
}

function writeCachedFamilies(data) {
  const families = Array.isArray(data?.families) ? data.families : [];
  const schemaVersion = Number(data?.schema_version);
  if (
    !Number.isInteger(schemaVersion)
    || schemaVersion < 1
    || families.length > 256
    || !families.every(validCachedFamily)
  ) return;
  try {
    sessionStorage.setItem(FAMILY_CACHE_KEY, JSON.stringify({
      format: FAMILY_CACHE_FORMAT,
      schema_version: schemaVersion,
      cached_at: Date.now(),
      families,
    }));
  } catch (_) {}
}

function acceptFamilyCatalog(data) {
  if (
    Number(data?.schema_version) !== 1
    || !Array.isArray(data?.families)
    || data.families.length > 256
    || !data.families.every(validCachedFamily)
  ) {
    throw new Error('Provider catalogue returned an invalid response.');
  }
  const nextCatalogKey = typeof data?.catalog_key === 'string' ? data.catalog_key : '';
  if (familyCatalogKey && nextCatalogKey && familyCatalogKey !== nextCatalogKey) {
    enrichedFamilyAuthMethods.clear();
  }
  if (nextCatalogKey) familyCatalogKey = nextCatalogKey;
  const bundled = Array.isArray(data?.families) ? data.families : [];
  writeCachedFamilies(data);
  return bundled.map(family => {
    const methods = enrichedFamilyAuthMethods.get(String(family?.id || ''));
    return methods
      ? { ...family, auth_methods: methods, auth_methods_complete: true }
      : family;
  });
}

function acceptManagementSnapshot(data) {
  if (
    Number(data?.schema_version) !== 1
    || !Array.isArray(data?.connections)
    || !Array.isArray(data?.models)
    || !data?.shares
    || !Array.isArray(data.shares.received)
  ) {
    throw new Error('Added Models returned an invalid response.');
  }
  return {
    connections: data.connections,
    models: data.models,
    receivedShares: data.shares.received,
  };
}

function familyRenderSignature(families) {
  return JSON.stringify((families || []).map(family => ({
    id: family.id,
    display_name: family.display_name,
    adapters: family.adapters,
    kinds: family.kinds,
    billing_lanes: family.billing_lanes,
    auth_methods: family.auth_methods,
    auth_methods_complete: family.auth_methods_complete,
    model_count: family.model_count,
  })));
}

function captureListRenderState(host) {
  const active = document.activeElement;
  const panel = host.closest?.('.settings-panels') || document.querySelector?.('.settings-panels');
  return {
    focusKey: active && host.contains?.(active)
      ? String(active.dataset?.providerFocusKey || '')
      : '',
    hostScrollLeft: Number(host.scrollLeft || 0),
    hostScrollTop: Number(host.scrollTop || 0),
    panel,
    panelScrollLeft: Number(panel?.scrollLeft || 0),
    panelScrollTop: Number(panel?.scrollTop || 0),
    windowScrollX: Number(window.scrollX || 0),
    windowScrollY: Number(window.scrollY || 0),
  };
}

function restoreListRenderState(host, snapshot) {
  host.scrollLeft = snapshot.hostScrollLeft;
  host.scrollTop = snapshot.hostScrollTop;
  if (snapshot.panel) {
    snapshot.panel.scrollLeft = snapshot.panelScrollLeft;
    snapshot.panel.scrollTop = snapshot.panelScrollTop;
  }
  if (snapshot.focusKey) {
    const next = [...(host.querySelectorAll?.('[data-provider-focus-key]') || [])]
      .find(item => item.dataset?.providerFocusKey === snapshot.focusKey);
    next?.focus?.({ preventScroll: true });
  }
  if (typeof window.scrollTo === 'function') {
    window.scrollTo(snapshot.windowScrollX, snapshot.windowScrollY);
  }
}

function byId(id) {
  return document.getElementById(id);
}

function element(tag, attrs = {}, text = '') {
  const result = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (name === 'class') result.className = value;
    else if (name === 'type') result.type = value;
    else if (name === 'value') result.value = value;
    else if (name === 'checked') result.checked = Boolean(value);
    else if (name === 'disabled') result.disabled = Boolean(value);
    else result.setAttribute(name, String(value));
  }
  if (text !== '') result.textContent = String(text);
  return result;
}

function button(label, className = 'admin-btn-sm') {
  return element('button', { type: 'button', class: className }, label);
}

function field(label, control, hint = '') {
  const wrapper = element('label', { class: 'provider-control-field' });
  wrapper.append(element('span', { class: 'settings-label' }, label), control);
  if (hint) wrapper.append(element('span', { class: 'admin-toggle-sub' }, hint));
  return wrapper;
}

function safeDetail(data, status) {
  const detail = data && data.detail;
  if (typeof detail === 'string' && detail) return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  if (Array.isArray(detail) && detail[0] && typeof detail[0].msg === 'string') {
    return detail[0].msg;
  }
  if (data && typeof data.error === 'string') return data.error;
  return `Provider request failed (${status})`;
}

function idempotencyKey() {
  try {
    if (globalThis.crypto?.randomUUID) return `web-${globalThis.crypto.randomUUID()}`;
  } catch (_) {}
  return `web-${Date.now()}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
}

async function request(path, options = {}) {
  const {
    mutation = false,
    revision,
    timeoutMs = REQUEST_TIMEOUT_MS,
    timeoutLabel = 'Provider request',
    signal: parentSignal,
    ...fetchOptions
  } = options;
  const headers = { Accept: 'application/json', ...(fetchOptions.headers || {}) };
  if (fetchOptions.body !== undefined) headers['Content-Type'] = 'application/json';
  if (mutation) headers['Idempotency-Key'] = idempotencyKey();
  if (revision !== undefined && revision !== null) {
    headers['If-Match'] = `"${Number(revision)}"`;
  }
  const controller = new AbortController();
  let timedOut = false;
  const abortFromParent = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) abortFromParent();
  else parentSignal?.addEventListener?.('abort', abortFromParent, { once: true });
  const timer = timeoutMs > 0
    ? setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs)
    : null;
  try {
    const response = await fetch(`${API_ROOT}${path}`, {
      credentials: 'same-origin',
      ...fetchOptions,
      headers,
      signal: controller.signal,
      body: fetchOptions.body === undefined
        ? undefined
        : (typeof fetchOptions.body === 'string' ? fetchOptions.body : JSON.stringify(fetchOptions.body)),
    });
    let data = {};
    if (response.status !== 204) {
      try {
        data = await response.json();
      } catch (error) {
        if (response.ok) throw new Error(`${timeoutLabel} returned an invalid response`);
      }
    }
    if (!response.ok) throw new Error(safeDetail(data, response.status));
    return data;
  } catch (error) {
    if (timedOut) throw new Error(`${timeoutLabel} timed out`);
    throw error;
  } finally {
    if (timer !== null) clearTimeout(timer);
    parentSignal?.removeEventListener?.('abort', abortFromParent);
  }
}

function setStatus(message, isError = false) {
  for (const id of ['provider-control-status', 'provider-control-added-status']) {
    const status = byId(id);
    if (!status) continue;
    // The Add page stays task-focused. Background loading/routing warnings
    // belong on Added Models/Advanced; only an error that blocks the user's
    // current add action is shown above the quick forms.
    status.textContent = id === 'provider-control-status' && !isError
      ? ''
      : String(message || '');
    status.classList?.toggle?.('provider-control-error', Boolean(isError));
  }
}

function familyFor(connectionOrId) {
  const familyId = typeof connectionOrId === 'string'
    ? connectionOrId
    : connectionOrId?.family_id;
  return state.families.find(item => item.id === familyId) || null;
}

function accountsFor(connectionId) {
  return state.accounts.get(connectionId) || [];
}

function modelsFor(connectionId) {
  return state.models.filter(model => model.connection_id === connectionId);
}

function laneLabel(lane) {
  return {
    metered_api: 'Metered API',
    subscription: 'Subscription',
    custom: 'Custom route',
    local: 'Local',
    legacy: 'Legacy review',
  }[lane] || String(lane || 'Unknown lane').replaceAll('_', ' ');
}

function authLabel(method) {
  return {
    api_key: 'API key',
    oauth: 'Login',
    none: 'Keyless',
  }[method] || String(method || 'Unknown');
}

function authMethodsFor(family) {
  let oauthIndex = 0;
  return (family?.auth_methods || []).map((method) => {
    if (method && typeof method === 'object') {
      const id = String(method.id || '');
      const type = method.type === 'api' || id === 'api_key'
        ? 'api_key'
        : method.type === 'none' || id === 'none'
          ? 'none'
          : 'oauth';
      return { ...method, id: id || (type === 'oauth' ? `oauth:${oauthIndex++}` : type), type };
    }
    const value = String(method || '');
    if (value === 'api' || value === 'api_key') return { id: 'api_key', type: 'api_key', label: 'API key' };
    if (value === 'none') return { id: 'none', type: 'none', label: 'No API key' };
    const result = { id: value.startsWith('oauth:') ? value : `oauth:${oauthIndex}`, type: 'oauth', label: 'Provider login' };
    oauthIndex += 1;
    return result;
  });
}

function oauthMethodIndex(method) {
  const match = String(method?.id || '').match(/^oauth:(\d+)$/);
  return match ? Number(match[1]) : 0;
}

function preferredMethod(family) {
  const methods = authMethodsFor(family);
  const localOnly = (family?.kinds || []).length === 1 && family.kinds[0] === 'local';
  return methods.find(method => localOnly && method.type === 'none')
    || methods.find(method => method.type === 'api_key')
    || methods.find(method => method.type === 'oauth')
    || methods[0]
    || { id: 'none', type: 'none', label: 'No API key' };
}

function preferredKind(family, method) {
  const kinds = family?.kinds || [];
  if (method?.type === 'oauth' && kinds.includes('subscription')) return 'subscription';
  if (method?.type === 'none' && kinds.includes('local')) return 'local';
  if (method?.type === 'api_key' && kinds.includes('official')) return 'official';
  if (method?.type === 'api_key' && kinds.includes('local')) return 'local';
  if (method?.type === 'api_key' && kinds.includes('custom_gateway')) return 'custom_gateway';
  return kinds[0] || 'official';
}

function preferredLane(family, kind) {
  const lanes = family?.billing_lanes || [];
  const preferred = kind === 'subscription'
    ? 'subscription'
    : kind === 'local'
      ? 'local'
      : kind === 'custom_gateway'
        ? 'custom'
        : 'metered_api';
  return lanes.includes(preferred) ? preferred : (lanes[0] || preferred);
}

function compatibleModels(purpose) {
  const entry = PURPOSES.find(([id]) => id === purpose);
  const operations = new Set(entry?.[2] || []);
  return state.models.filter(model => (
    model.enabled !== false
    && (model.operations || []).some(operation => operations.has(operation))
  ));
}

function healthFor(account) {
  const rows = [];
  for (const model of modelsFor(account.connection_id)) {
    const entries = state.eligibility.get(model.id) || [];
    const entry = entries.find(item => item.account_id === account.id);
    if (entry) rows.push(entry);
  }
  if (!account.enabled) return { tone: 'off', label: 'Disabled', detail: '' };
  if (!rows.length) return { tone: 'quiet', label: 'No model health yet', detail: '' };
  if (rows.some(item => item.account_state === 'reauth_required')) {
    return { tone: 'bad', label: 'Sign-in required', detail: '' };
  }
  const cooldown = rows.find(item => item.account_cooldown_until || item.model_cooldown_until);
  if (cooldown) {
    return {
      tone: 'warn',
      label: 'Cooling down',
      detail: cooldown.account_cooldown_until || cooldown.model_cooldown_until || '',
    };
  }
  const eligible = rows.filter(item => item.eligible).length;
  if (!eligible) return { tone: 'bad', label: 'Unavailable for current models', detail: '' };
  return {
    tone: eligible === rows.length ? 'good' : 'warn',
    label: `${eligible}/${rows.length} model route${rows.length === 1 ? '' : 's'} eligible`,
    detail: '',
  };
}

function announceCatalogChange() {
  try {
    document.dispatchEvent?.(new CustomEvent('open-clank:providers-updated', {
      detail: { source: 'provider-control' },
    }));
  } catch (_) {}
  return onCatalogChanged();
}

async function changed(message) {
  invalidateLoadedViews();
  await load({ force: true, view: activeManagementView() });
  setStatus(message);
  // Refresh the visible Added Models surface first. Other model pickers update
  // in the background instead of holding this page behind their own reads.
  Promise.resolve(announceCatalogChange()).catch(error => {
    console.warn('[provider-control] catalogue refresh failed', error);
  });
}

function beginDetailRequest() {
  const epoch = detailEpoch;
  const controller = new AbortController();
  detailControllers.add(controller);
  return {
    signal: controller.signal,
    isCurrent: () => epoch === detailEpoch && !controller.signal.aborted,
    finish: () => detailControllers.delete(controller),
  };
}

function cancelInFlightDetailLoads() {
  detailEpoch += 1;
  for (const controller of detailControllers) controller.abort();
  detailControllers.clear();
  eligibilityLoads.clear();
  connectionDetailLoads.clear();
  shareDirectoryLoad = null;
  bindingsLoad = null;
}

async function fetchState(view, onProgress = () => {}, signal = undefined) {
  const next = {
    families: [...state.families],
    connections: [...state.connections],
    accounts: new Map(state.accounts),
    models: [...state.models],
    eligibility: new Map(state.eligibility),
    bindings: [...state.bindings],
    shareRecipients: [...state.shareRecipients],
    ownedShares: [...state.ownedShares],
    receivedShares: [...state.receivedShares],
  };
  const warnings = [];
  let requiredSucceeded = false;
  const publish = phase => onProgress(next, [...warnings], phase);

  // Add Models owns only the nonsecret family catalogue. Connected models,
  // shares, accounts, routing, and health belong to Added Models and must not
  // compete with this first usable paint.
  if (view === 'services') {
    try {
      const data = await request('/families', {
        signal,
        timeoutLabel: 'Provider catalog',
      });
      next.families = acceptFamilyCatalog(data);
      requiredSucceeded = true;
    } catch (error) {
      warnings.push(`families: ${error?.message || 'unavailable'}`);
    }
    publish('catalog');
    return { next, warnings, requiredSucceeded };
  }

  // Added Models uses owner-local persisted projections. A family read is
  // needed only when neither memory nor the validated session cache supplied
  // labels for the connection rows.
  const catalogRequest = next.families.length
    ? null
    : request('/families', {
      signal,
      timeoutLabel: 'Provider catalog',
    });
  const managementRequest = request('/management-snapshot', {
    signal,
    timeoutLabel: 'Added Models snapshot',
  });
  const catalogTask = catalogRequest ? (async () => {
    try {
      const data = await catalogRequest;
      next.families = acceptFamilyCatalog(data);
    } catch (error) {
      warnings.push(`families: ${error?.message || 'unavailable'}`);
    }
    publish('catalog');
  })() : Promise.resolve();
  const coreTask = (async () => {
    try {
      const data = await managementRequest;
      const snapshot = acceptManagementSnapshot(data);
      next.connections = snapshot.connections;
      next.models = snapshot.models;
      next.receivedShares = snapshot.receivedShares;
      requiredSucceeded = true;
      const liveIds = new Set(next.models.map(model => model.id));
      next.eligibility = new Map([...next.eligibility].filter(([id]) => liveIds.has(id)));
    } catch (error) {
      warnings.push(`management snapshot: ${error?.message || 'unavailable'}`);
    }
    const connectionIds = new Set(next.connections.map(connection => connection.id));
    next.accounts = new Map([...next.accounts].filter(([id]) => connectionIds.has(id)));
    for (const remembered of [openConnections, openAccountDetails, loadedConnectionAccounts]) {
      for (const connectionId of [...remembered]) {
        if (!connectionIds.has(connectionId)) remembered.delete(connectionId);
      }
    }
    for (const connectionId of [...modelSearchQueries.keys()]) {
      if (!connectionIds.has(connectionId)) modelSearchQueries.delete(connectionId);
    }
    const modelIds = new Set(next.models.map(model => model.id));
    for (const modelId of [...openShareChoosers]) {
      if (!modelIds.has(modelId)) openShareChoosers.delete(modelId);
    }
    publish('core');
  })();
  await Promise.all([catalogTask, coreTask]);
  return { next, warnings, requiredSucceeded };
}

async function loadConnectionEligibility(connectionId) {
  if (eligibilityLoads.has(connectionId)) return eligibilityLoads.get(connectionId);
  const detail = beginDetailRequest();
  const task = (async () => {
    const data = await request(`/connections/${encodeURIComponent(connectionId)}/eligibility`, {
      timeoutLabel: 'Connection model health',
      signal: detail.signal,
    });
    if (!detail.isCurrent()) return;
    for (const item of Array.isArray(data.models) ? data.models : []) {
      const modelRouteId = String(item?.model_route_id || '');
      if (!modelRouteId) continue;
      state.eligibility.set(
        modelRouteId,
        Array.isArray(item.accounts) ? item.accounts : [],
      );
    }
  })();
  eligibilityLoads.set(connectionId, task);
  try {
    await task;
  } catch (error) {
    if (!detail.signal.aborted) throw error;
  } finally {
    detail.finish();
    if (eligibilityLoads.get(connectionId) === task) eligibilityLoads.delete(connectionId);
  }
}

async function loadConnectionAccounts(connectionId) {
  if (loadedConnectionAccounts.has(connectionId)) return;
  if (connectionDetailLoads.has(connectionId)) return connectionDetailLoads.get(connectionId);
  const connection = state.connections.find(item => item.id === connectionId);
  const detail = beginDetailRequest();
  const task = (async () => {
    const data = await request(`/connections/${encodeURIComponent(connectionId)}/accounts`, {
      timeoutLabel: `${connection?.label || 'Provider'} accounts`,
      signal: detail.signal,
    });
    if (!detail.isCurrent()) return;
    state.accounts.set(connectionId, Array.isArray(data.accounts) ? data.accounts : []);
    loadedConnectionAccounts.add(connectionId);
  })();
  connectionDetailLoads.set(connectionId, task);
  try {
    await task;
  } catch (error) {
    if (!detail.signal.aborted) throw error;
  } finally {
    detail.finish();
    if (connectionDetailLoads.get(connectionId) === task) connectionDetailLoads.delete(connectionId);
  }
}

async function loadShareDirectory(options = {}) {
  if (shareDirectoryLoaded && !options.force) return;
  if (shareDirectoryLoad && !options.force) return shareDirectoryLoad;
  const detail = beginDetailRequest();
  const task = (async () => {
    const [recipients, shares] = await Promise.all([
      request('/share-recipients', { timeoutLabel: 'Share recipients', signal: detail.signal }),
      request('/shares', { timeoutLabel: 'Provider shares', signal: detail.signal }),
    ]);
    if (!detail.isCurrent()) return;
    state.shareRecipients = Array.isArray(recipients.recipients) ? recipients.recipients : [];
    state.ownedShares = Array.isArray(shares.shares) ? shares.shares : [];
    shareDirectoryLoaded = true;
  })();
  shareDirectoryLoad = task;
  try {
    await task;
  } catch (error) {
    if (!detail.signal.aborted) throw error;
  } finally {
    detail.finish();
    if (shareDirectoryLoad === task) shareDirectoryLoad = null;
  }
}

async function loadBindings(options = {}) {
  if (bindingsLoaded && !options.force) return;
  if (bindingsLoad && !options.force) return bindingsLoad;
  const detail = beginDetailRequest();
  const task = (async () => {
    const data = await request('/bindings', { timeoutLabel: 'Model routing', signal: detail.signal });
    if (!detail.isCurrent()) return;
    state.bindings = Array.isArray(data.bindings) ? data.bindings : [];
    bindingsLoaded = true;
  })();
  bindingsLoad = task;
  try {
    await task;
  } catch (error) {
    if (!detail.signal.aborted) throw error;
  } finally {
    detail.finish();
    if (bindingsLoad === task) bindingsLoad = null;
  }
}

function oauthPromptEditor(host, method) {
  const controls = new Map();
  const prompts = Array.isArray(method?.prompts) ? method.prompts : [];
  const syncVisibility = () => {
    controls.forEach(({ prompt, wrapper }) => {
      const condition = prompt.when;
      if (!condition) {
        wrapper.classList?.remove?.('hidden');
        return;
      }
      const actual = controls.get(condition.key)?.control?.value || '';
      const visible = condition.op === 'neq' ? actual !== condition.value : actual === condition.value;
      wrapper.classList?.toggle?.('hidden', !visible);
    });
  };
  host.replaceChildren();
  prompts.forEach(prompt => {
    let control;
    let controlNode;
    if (prompt.type === 'select') {
      control = createThemedPicker({
        label: prompt.message || prompt.key,
        options: (prompt.options || []).map(option => ({
          value: option.value,
          label: option.label || option.value,
        })),
      });
      controlNode = control.root;
    } else {
      control = element('input', {
        class: 'settings-input',
        type: 'text',
        autocomplete: 'off',
        placeholder: prompt.placeholder || '',
      });
      controlNode = control;
    }
    const wrapper = field(prompt.message || prompt.key, controlNode);
    wrapper.classList?.add?.('provider-control-oauth-prompt');
    controls.set(prompt.key, { control, prompt, wrapper });
    if (controlNode.addEventListener) {
      controlNode.addEventListener('change', syncVisibility);
      controlNode.addEventListener('input', syncVisibility);
    }
    host.append(wrapper);
  });
  syncVisibility();
  return () => Object.fromEntries(
    [...controls]
      .filter(([, entry]) => !entry.wrapper.classList?.contains?.('hidden'))
      .map(([key, entry]) => [key, entry.control.value]),
  );
}

async function openAddedModels(connectionId) {
  const tab = document.querySelector?.('[data-settings-tab="added-models"]');
  tab?.click?.();
  await load({ view: 'added-models' });
  selectConnection(connectionId);
}

async function refreshConnectionsForAdd() {
  const data = await request('/connections', {
    timeoutLabel: 'Existing provider connections',
  });
  state = {
    ...state,
    connections: Array.isArray(data.connections) ? data.connections : [],
  };
}

function renderCreateConnection() {
  const host = byId('provider-control-create');
  if (!host) return;
  const signature = familyRenderSignature(state.families);
  if (signature === renderedFamilySignature && host.children?.length) return;
  activePicker?.close?.();
  host.replaceChildren();
  const addableFamilies = state.families.filter(item => item.id !== 'local-executor');
  if (!addableFamilies.length) {
    host.append(element('p', { class: 'admin-empty' }, 'Loading providers… Your existing models are already available under Added Models. Use Refresh if this does not clear.'));
    renderedFamilySignature = signature;
    return;
  }

  const remoteFamilies = addableFamilies.filter(item => (
    (item.kinds || []).some(kind => ['official', 'subscription', 'custom_gateway'].includes(kind))
  ));
  const subscriptionFamilies = remoteFamilies.filter(item => (item.kinds || []).includes('subscription'));
  const localFamilies = addableFamilies.filter(item => (item.kinds || []).includes('local'));

  function providerOptions(families) {
    return families.map(item => {
      const count = Number(item.model_count || 0);
      return {
        value: item.id,
        label: item.display_name || item.id,
        hint: count ? `${count} model${count === 1 ? '' : 's'}` : '',
        logo: familyLogo(item),
        search: `${item.display_name || ''} ${item.id || ''}`,
      };
    });
  }

  function addCard(mode, families) {
    if (!families.length) return null;
    const isLocal = mode === 'local';
    const isSubscription = mode === 'subscription';
    const preferredFamily = families.find(item => item.id === (isLocal ? 'ollama' : 'openai')) || families[0];
    const form = element('form', {
      class: 'admin-card provider-control-quick-form provider-control-add-card',
      'data-provider-add-mode': mode,
    });
    const heading = element('div', { class: 'provider-control-add-heading' });
    const headingCopy = element('div', { class: 'provider-control-add-title' });
    const title = isLocal ? 'Add Local Models' : isSubscription ? 'Add Subscription Models' : 'Add API Models';
    headingCopy.append(
      iconNode(isLocal ? ADD_ICON : API_ADD_ICON, 'provider-control-add-title-icon'),
      element('h2', {}, title),
      element('span', { class: 'provider-control-add-kind' }, isSubscription ? '(Account)' : '(Endpoint)'),
    );
    heading.append(headingCopy);
    const description = element('p', { class: 'admin-toggle-sub provider-control-add-description' },
      isLocal
        ? 'Connect Ollama or another model server running on your network.'
        : isSubscription
          ? 'Sign in with a supported provider account to add its subscription models.'
          : 'Connect an API key endpoint such as OpenAI, Anthropic, DeepSeek, or OpenRouter.',
    );
    let pickerMenuAnchor = null;
    const family = createThemedPicker({
      label: isLocal ? 'Local model provider' : isSubscription ? 'Subscription provider' : 'API model provider',
      options: providerOptions(families),
      value: preferredFamily.id,
      searchable: families.length > FAMILY_PICKER_SEARCH_THRESHOLD,
      showTriggerHint: false,
      className: 'provider-control-family-picker',
      menuClassName: 'provider-control-family-picker-menu',
      menuAnchor: () => pickerMenuAnchor,
      onChange: () => syncFamily(true),
    });
    const url = element('input', {
      class: 'settings-input provider-control-url-input',
      type: 'url',
      maxlength: 2048,
      autocomplete: 'off',
      placeholder: isLocal ? 'http://localhost:11434' : 'Provider base URL',
      'aria-label': isLocal ? 'Local model server URL' : 'Provider base URL',
    });
    const urlShell = element('div', { class: 'provider-control-url-shell' });
    const routeHint = element('div', {
      class: 'provider-control-route-hint hidden',
      'aria-live': 'polite',
    });
    urlShell.append(url, routeHint);
    const authChoices = element('div', {
      class: 'provider-control-methods',
      role: 'group',
      'aria-label': 'Connect with',
    });
    const promptHost = element('div', { class: 'provider-control-prompt-fields' });
    const secret = element('input', {
      class: 'settings-input provider-control-secret-input',
      type: 'password',
      autocomplete: 'off',
      placeholder: isLocal ? 'API key (optional)' : 'API key',
      'aria-label': 'API key',
    });
    const credentialIcon = iconNode(KEY_ICON, 'provider-control-credential-icon');
    const credentialShell = element('div', { class: 'provider-control-credential-shell' });
    credentialShell.append(credentialIcon, secret);
    const save = button('Add', 'admin-btn-add provider-control-add-submit');
    save.type = 'submit';
    save.dataset.providerFocusKey = `add:${mode}:submit`;
    const actionRow = element('div', { class: 'provider-control-add-action-row' });
    actionRow.append(credentialShell, save);
    const providerCombo = element('div', { class: 'provider-control-provider-combo' });
    providerCombo.append(family.root, urlShell);
    const primaryRow = element('div', { class: 'provider-control-add-primary-row' });
    primaryRow.append(providerCombo);
    pickerMenuAnchor = providerCombo;
    let methodId = '';
    let readPromptValues = () => ({});

    function contract() {
      return familyFor(family.value) || families.find(item => item.id === family.value) || {};
    }

    function availableMethods() {
      const methods = authMethodsFor(contract());
      if (isLocal) return methods.filter(method => ['none', 'api_key'].includes(method.type));
      if (isSubscription) return methods.filter(method => method.type === 'oauth');
      return methods.filter(method => !(
        method.type === 'oauth' && (contract().kinds || []).includes('subscription')
      ));
    }

    function authMethodsIncomplete() {
      return contract().auth_methods_complete === false;
    }

    function selectedMethod() {
      const methods = availableMethods();
      const unavailable = {
        id: 'unavailable',
        type: 'unavailable',
        label: isSubscription ? 'Subscription login unavailable' : 'Sign-in methods unavailable',
      };
      if (isSubscription && !methods.some(method => method.type === 'oauth')) return unavailable;
      if (!isLocal && !methods.length) return unavailable;
      return methods.find(method => method.id === methodId)
        || (isLocal ? methods.find(method => method.type === 'none') : null)
        || methods.find(method => method.type === 'api_key')
        || methods.find(method => method.type === 'oauth')
        || methods[0]
        || (authMethodsIncomplete()
          ? { id: 'unavailable', type: 'unavailable', label: 'Sign-in methods unavailable' }
          : { id: 'none', type: 'none', label: 'No API key' });
    }

    function selectedKind(method = selectedMethod()) {
      const kinds = contract().kinds || [];
      if (isLocal && kinds.includes('local')) return 'local';
      if (isSubscription && kinds.includes('subscription')) return 'subscription';
      if (method.type === 'oauth' && kinds.includes('subscription')) return 'subscription';
      if (kinds.includes('official')) return 'official';
      if (kinds.includes('custom_gateway')) return 'custom_gateway';
      return preferredKind(contract(), method);
    }

    function renderMethodChoices() {
      const methods = availableMethods();
      const chosen = selectedMethod();
      methodId = chosen.id;
      authChoices.replaceChildren();
      const seenAlternatives = new Set();
      const alternatives = methods.filter(item => item.id !== methodId).filter(item => {
        const key = item.type === 'api_key' ? 'api_key' : item.id;
        if (seenAlternatives.has(key)) return false;
        seenAlternatives.add(key);
        return true;
      });
      if (!alternatives.length && !authMethodsIncomplete()) {
        authChoices.classList?.add?.('hidden');
        return;
      }
      authChoices.classList?.remove?.('hidden');
      for (const method of alternatives) {
        const label = method.label || authLabel(method.type);
        const choice = button(`Use ${label} instead`, 'provider-control-method-choice');
        choice.addEventListener('click', () => {
          methodId = method.id;
          syncMethod();
          renderMethodChoices();
        });
        authChoices.append(choice);
      }
      if (authMethodsIncomplete()) {
        const enrich = button(
          methods.length ? 'Load more sign-in methods' : 'Load sign-in methods',
          'provider-control-method-choice',
        );
        enrich.addEventListener('click', async () => {
          enrich.disabled = true;
          enrich.textContent = 'Loading sign-in methods…';
          try {
            const data = await request(`/families/${encodeURIComponent(family.value)}/auth-methods`, {
              method: 'POST',
              timeoutMs: FAMILY_ENRICH_TIMEOUT_MS,
              timeoutLabel: 'Provider sign-in methods',
            });
            const index = state.families.findIndex(item => item.id === family.value);
            if (index < 0) throw new Error('Provider family is no longer available.');
            state.families[index] = {
              ...state.families[index],
              auth_methods: Array.isArray(data.auth_methods) ? data.auth_methods : [],
              auth_methods_complete: true,
            };
            enrichedFamilyAuthMethods.set(
              family.value,
              Array.isArray(data.auth_methods) ? data.auth_methods : [],
            );
            syncFamily(false);
            renderedFamilySignature = familyRenderSignature(state.families);
          } catch (error) {
            enrich.disabled = false;
            enrich.textContent = methods.length ? 'Retry sign-in methods' : 'Retry loading sign-in methods';
            setStatus(error.message, true);
          }
        });
        authChoices.append(enrich);
      }
    }

    function syncMethod() {
      const method = selectedMethod();
      const unavailable = method.type === 'unavailable';
      const kind = selectedKind(method);
      const needsUrl = isLocal || kind === 'custom_gateway';
      url.classList?.toggle?.('hidden', !needsUrl);
      routeHint.classList?.toggle?.('hidden', needsUrl && !unavailable);
      routeHint.textContent = unavailable
        ? authMethodsIncomplete()
          ? 'Load this provider’s sign-in methods before adding it.'
          : isSubscription
            ? 'This provider does not offer subscription login.'
            : 'This provider has no compatible sign-in method.'
        : method.type === 'oauth' ? 'Browser account' : 'Official provider API';
      url.required = needsUrl && !unavailable;
      const usesSecret = method.type === 'api_key';
      credentialShell.classList?.toggle?.('hidden', !usesSecret);
      secret.required = usesSecret;
      secret.placeholder = isLocal ? 'API key for a protected server' : 'API key';
      promptHost.classList?.toggle?.('hidden', method.type !== 'oauth');
      readPromptValues = method.type === 'oauth' ? oauthPromptEditor(promptHost, method) : () => ({});
      if (method.type !== 'oauth') promptHost.replaceChildren();
      save.disabled = unavailable;
      save.textContent = unavailable
        ? isSubscription && !authMethodsIncomplete() ? 'Unavailable' : 'Sign-in required'
        : method.type === 'oauth' ? (method.label || 'Sign in') : 'Add';
      if (method.type === 'none') {
        primaryRow.append(save);
        actionRow.classList?.add?.('hidden');
      } else {
        actionRow.append(save);
        actionRow.classList?.remove?.('hidden');
      }
      save.setAttribute('aria-label', method.type === 'oauth'
        ? `Continue with ${method.label || 'provider login'}`
        : `Add ${contract().display_name || contract().id || 'models'}`);
    }

    function syncFamily(resetUrl = false) {
      const selected = contract();
      const methods = availableMethods();
      const preferred = isLocal
        ? methods.find(method => method.type === 'none') || methods.find(method => method.type === 'api_key')
        : isSubscription
          ? methods.find(method => method.type === 'oauth')
          : methods.find(method => method.type === 'api_key') || methods.find(method => method.type === 'oauth') || methods[0];
      methodId = preferred?.id || (isLocal ? 'none' : 'unavailable');
      if (resetUrl) {
        url.value = selected.id === 'ollama' ? 'http://localhost:11434' : '';
        secret.value = '';
      }
      renderMethodChoices();
      syncMethod();
    }

    form.addEventListener('submit', async event => {
      event.preventDefault();
      save.disabled = true;
      const selected = contract();
      const method = selectedMethod();
      const kind = selectedKind(method);
      const lane = preferredLane(selected, kind);
      const adapter = (selected.adapters || [])[0];
      const enteredUrl = url.value.trim();
      const enteredSecret = secret.value;
      secret.value = '';
      let connection = null;
      let created = false;
      try {
        if (method.type === 'unavailable') throw new Error('Load this provider’s sign-in methods first.');
        if (!adapter) throw new Error(`${selected.display_name || selected.id} is not ready to connect.`);
        if ((isLocal || kind === 'custom_gateway') && !enteredUrl) throw new Error('Enter the model server URL.');
        if (method.type === 'api_key' && !enteredSecret) throw new Error('Enter an API key.');
        // Add Models does not preload owner management data. Refresh just the
        // connection identities at submit time so an existing exact endpoint
        // receives the new account instead of being duplicated.
        await refreshConnectionsForAdd();
        connection = state.connections.find(item => (
          item.family_id === family.value
          && item.adapter_id === adapter
          && item.kind === kind
          && item.billing_lane === lane
          && String(item.url || '').replace(/\/+$/, '') === enteredUrl.replace(/\/+$/, '')
        ));
        if (!connection) {
          connection = await request('/connections', {
            method: 'POST',
            mutation: true,
            body: {
              family_id: family.value,
              adapter_id: adapter,
              kind,
              billing_lane: lane,
              label: selected.display_name || selected.id || family.value,
              url: enteredUrl || null,
              settings: {},
              enabled: true,
            },
          });
          created = true;
        }
        if (method.type === 'api_key') {
          await request(`/connections/${encodeURIComponent(connection.id)}/accounts`, {
            method: 'POST',
            mutation: true,
            body: { label: `Account ${accountsFor(connection.id).length + 1}`, api_key: enteredSecret },
          });
          await changed(`${selected.display_name || connection.label} added.`);
          await openAddedModels(connection.id);
        } else if (method.type === 'oauth') {
          const started = await startOAuth(connection, null, method, readPromptValues());
          if (!started) return;
          await announceCatalogChange();
          await load({ force: true });
          setStatus(`Finish ${method.label || 'provider login'} in the opened window.`);
        } else {
          await changed(`${selected.display_name || connection.label} added.`);
          await openAddedModels(connection.id);
        }
      } catch (error) {
        if (created && connection?.id && method.type === 'api_key' && enteredSecret) {
          try {
            await request(`/connections/${encodeURIComponent(connection.id)}`, {
              method: 'DELETE', mutation: true, revision: connection.revision,
            });
          } catch (_) {}
        }
        setStatus(error.message, true);
      } finally {
        secret.value = '';
        // Programmatic submission can reach this path even while an
        // incomplete family is fail-closed. Do not accidentally turn that
        // state into an enabled, keyless Add button after the rejected submit.
        save.disabled = selectedMethod().type === 'unavailable';
      }
    });

    form.append(heading, description, primaryRow, authChoices, promptHost, actionRow);
    syncFamily(true);
    return form;
  }

  const cards = element('div', { class: 'provider-control-add-cards' });
  const localCard = addCard('local', localFamilies);
  const remoteCard = addCard('remote', remoteFamilies);
  const subscriptionCard = addCard('subscription', subscriptionFamilies);
  if (localCard) cards.append(localCard);
  if (remoteCard) cards.append(remoteCard);
  if (subscriptionCard) cards.append(subscriptionCard);
  host.append(cards);
  renderedFamilySignature = signature;
}

function accountHealthBadge(account) {
  const health = healthFor(account);
  const badge = element('span', {
    class: `provider-control-health provider-control-health-${health.tone}`,
    title: health.detail || health.label,
  }, health.label);
  return badge;
}

async function reorder(connection, orderedIds) {
  try {
    await request(`/connections/${encodeURIComponent(connection.id)}/pool/order`, {
      method: 'PUT',
      mutation: true,
      revision: connection.revision,
      body: { account_ids: orderedIds },
    });
    await changed('Account rotation order updated.');
  } catch (error) {
    setStatus(error.message, true);
  }
}

function renderAccount(connection, account, index, accounts) {
  const row = element('div', { class: 'provider-control-account' });
  const identity = Object.values(account.identity || {}).filter(Boolean).join(' · ');
  const heading = element('div', { class: 'provider-control-account-heading' });
  heading.append(
    element('strong', {}, `${accounts.length > 1 ? `${index + 1}. ` : ''}${account.label}`),
    element('span', { class: 'admin-badge' }, authLabel(account.auth_method)),
    accountHealthBadge(account),
  );
  const metadata = element('div', { class: 'admin-toggle-sub' },
    `${account.auth_class || 'provider'}${identity ? ` · ${identity}` : ''}`,
  );
  const actions = element('div', { class: 'provider-control-actions' });
  if (accounts.length > 1) {
    const up = button('Move up');
    const down = button('Move down');
    up.disabled = index === 0;
    down.disabled = index === accounts.length - 1;
    up.addEventListener('click', () => {
      const ids = accounts.map(item => item.id);
      [ids[index - 1], ids[index]] = [ids[index], ids[index - 1]];
      reorder(connection, ids);
    });
    down.addEventListener('click', () => {
      const ids = accounts.map(item => item.id);
      [ids[index], ids[index + 1]] = [ids[index + 1], ids[index]];
      reorder(connection, ids);
    });
    actions.append(up, down);
  }
  const enabled = button(account.enabled ? 'Disable' : 'Enable');
  enabled.addEventListener('click', async () => {
    enabled.disabled = true;
    try {
      await request(`/accounts/${encodeURIComponent(account.id)}/enable`, {
        method: 'POST',
        mutation: true,
        revision: account.revision,
        body: { enabled: !account.enabled },
      });
      await changed(`Account ${account.enabled ? 'disabled' : 'enabled'}.`);
    } catch (error) {
      setStatus(error.message, true);
      enabled.disabled = false;
    }
  });
  actions.append(enabled);
  const family = familyFor(connection) || {};
  const reconnectMethod = authMethodsFor(family).find(method => method.type === 'oauth');
  if (account.auth_method === 'oauth' && reconnectMethod) {
    const reauth = button('Reconnect');
    reauth.addEventListener('click', () => startOAuth(connection, account, reconnectMethod));
    actions.append(reauth);
  }
  const remove = button('Remove', 'admin-btn-delete');
  remove.addEventListener('click', async () => {
    const confirmRemove = window.styledConfirm || window.confirm;
    const allowed = typeof confirmRemove !== 'function'
      || await confirmRemove(`Remove ${account.label} from this account pool?`, { confirmText: 'Remove', danger: true });
    if (!allowed) return;
    remove.disabled = true;
    try {
      await request(`/accounts/${encodeURIComponent(account.id)}`, {
        method: 'DELETE',
        mutation: true,
        revision: account.revision,
      });
      await changed('Account removed. Historical references keep a credential-free tombstone.');
    } catch (error) {
      setStatus(error.message, true);
      remove.disabled = false;
    }
  });
  actions.append(remove);
  row.append(heading, metadata, actions);
  return row;
}

function renderAddAccount(connection, host) {
  const family = familyFor(connection) || {};
  const methods = authMethodsFor(family);
  const apiMethod = methods.find(method => method.type === 'api_key');
  const oauthMethods = methods.filter(method => method.type === 'oauth');
  const keyless = methods.some(method => method.type === 'none');
  const controls = element('div', { class: 'provider-control-add-account' });
  controls.append(element('strong', {}, 'Add account'));
  if (keyless && connection.billing_lane === 'local') {
    controls.append(element('span', { class: 'admin-toggle-sub' }, 'No API key is required. Add one only if this local server is protected.'));
  }
  if (apiMethod && connection.billing_lane !== 'subscription') {
    const open = button(connection.billing_lane === 'local' ? 'Add optional API key' : 'Add API key', 'admin-btn-add');
    open.addEventListener('click', () => {
      controls.querySelectorAll?.('[data-provider-account-form]')?.forEach(item => item.remove());
      const form = element('form', { class: 'provider-control-form', 'data-provider-account-form': 'api-key' });
      const label = element('input', { class: 'settings-input', type: 'text', maxlength: 160, required: true, autocomplete: 'off', value: `Account ${accountsFor(connection.id).length + 1}` });
      const secret = element('input', { class: 'settings-input', type: 'password', required: true, autocomplete: 'off', placeholder: 'API key' });
      const save = button('Save account', 'admin-btn-add');
      save.type = 'submit';
      const cancel = button('Cancel');
      cancel.addEventListener('click', () => {
        secret.value = '';
        form.remove();
      });
      form.addEventListener('submit', async event => {
        event.preventDefault();
        const value = secret.value;
        secret.value = '';
        save.disabled = true;
        try {
          await request(`/connections/${encodeURIComponent(connection.id)}/accounts`, {
            method: 'POST',
            mutation: true,
            body: { label: label.value.trim(), api_key: value },
          });
          await changed('API-key account added to the rotation pool.');
        } catch (error) {
          setStatus(error.message, true);
          save.disabled = false;
        } finally {
          secret.value = '';
        }
      });
      const actions = element('div', { class: 'provider-control-actions' });
      actions.append(save, cancel);
      form.append(
        field('Account label', label),
        field('API key', secret, 'Write-only: the browser clears this field immediately after submission.'),
        actions,
      );
      controls.append(form);
      label.focus?.();
    });
    controls.append(open);
  }
  for (const method of oauthMethods) {
    const login = button(method.label || 'Add login', 'admin-btn-add');
    login.addEventListener('click', () => {
      controls.querySelectorAll?.('[data-provider-account-form]')?.forEach(item => item.remove());
      if (!(method.prompts || []).length) {
        startOAuth(connection, null, method);
        return;
      }
      const form = element('form', { class: 'provider-control-form', 'data-provider-account-form': `oauth-${method.id}` });
      const promptHost = element('div', { class: 'provider-control-prompt-fields' });
      const values = oauthPromptEditor(promptHost, method);
      const submit = button(`Continue with ${method.label || 'provider login'}`, 'admin-btn-add');
      submit.type = 'submit';
      const cancel = button('Cancel');
      cancel.addEventListener('click', () => form.remove());
      form.addEventListener('submit', async event => {
        event.preventDefault();
        submit.disabled = true;
        try {
          if (await startOAuth(connection, null, method, values())) form.remove();
        } finally {
          submit.disabled = false;
        }
      });
      const actions = element('div', { class: 'provider-control-actions' });
      actions.append(submit, cancel);
      form.append(promptHost, actions);
      controls.append(form);
    });
    controls.append(login);
  }
  if (!apiMethod && !oauthMethods.length && !keyless) {
    controls.append(element('span', { class: 'admin-toggle-sub' }, 'This connection does not accept credential accounts.'));
  }
  host.append(controls);
}

function oauthPanel() {
  return byId('provider-control-oauth');
}

function oauthExpiryMs(value) {
  const parsed = typeof value === 'number' ? value : Date.parse(String(value || ''));
  return Number.isFinite(parsed) && parsed > 0 ? parsed : Date.now() + OAUTH_STATUS_RETENTION_MS;
}

function readOAuthFlowSession() {
  try {
    const raw = globalThis.sessionStorage?.getItem(OAUTH_FLOW_STORAGE_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw);
    const flowId = typeof value?.flow_id === 'string' ? value.flow_id : '';
    const expiresAtMs = Number(value?.expires_at_ms);
    if (!/^[A-Za-z0-9_-]{16,128}$/.test(flowId) || !Number.isFinite(expiresAtMs) || expiresAtMs <= 0) {
      globalThis.sessionStorage?.removeItem(OAUTH_FLOW_STORAGE_KEY);
      return null;
    }
    return { flowId, expiresAtMs };
  } catch (_) {
    return null;
  }
}

function rememberOAuthFlow(flowId, expiresAtMs) {
  try {
    globalThis.sessionStorage?.setItem(OAUTH_FLOW_STORAGE_KEY, JSON.stringify({
      flow_id: flowId,
      expires_at_ms: expiresAtMs,
    }));
  } catch (_) {}
}

function forgetOAuthFlow(flowId) {
  try {
    const saved = readOAuthFlowSession();
    if (!flowId || saved?.flowId === flowId) {
      globalThis.sessionStorage?.removeItem(OAUTH_FLOW_STORAGE_KEY);
    }
  } catch (_) {}
}

function stopOAuthPolling() {
  if (activeOAuth?.timer) clearTimeout(activeOAuth.timer);
  forgetOAuthFlow(activeOAuth?.flowId);
  activeOAuth = null;
}

function addOAuthPublicDetails(result) {
  const flow = activeOAuth;
  if (!flow || flow.flowId !== result.flow_id || flow.detailsLoaded) return;
  if (typeof result.instructions === 'string' && result.instructions) {
    flow.instructions.textContent = result.instructions;
    flow.instructions.classList?.remove?.('hidden');
  }
  if (typeof result.url === 'string' && result.url && !flow.loginLink) {
    flow.loginLink = element(
      'a',
      { class: 'admin-btn-add', href: result.url, target: '_blank', rel: 'noopener noreferrer' },
      'Open provider login',
    );
    flow.loginLinkHost.append(flow.loginLink);
    flow.loginLinkHost.classList?.remove?.('hidden');
    if (flow.openLoginOnStart) {
      try { window.open?.(result.url, '_blank', 'noopener,noreferrer'); } catch (_) {}
      flow.openLoginOnStart = false;
    }
  }
  const userCode = typeof result.user_code === 'string' && /^[A-Za-z0-9-]{4,64}$/.test(result.user_code)
    ? result.user_code
    : '';
  if (userCode && !flow.copyButton) {
    const codeRow = element('div', {
      class: 'provider-control-oauth-code-row',
      role: 'group',
      'aria-label': 'Provider sign-in code',
    });
    const code = element('code', { class: 'provider-control-oauth-user-code' }, userCode);
    const copyButton = button('Copy code', 'admin-btn-sm provider-control-oauth-copy-button');
    const copyFeedback = element('p', {
      class: 'admin-toggle-sub provider-control-oauth-copy-feedback',
      role: 'status',
      'aria-live': 'polite',
    });
    copyButton.addEventListener('click', async () => {
      copyButton.disabled = true;
      try {
        const writeText = globalThis.navigator?.clipboard?.writeText;
        if (typeof writeText !== 'function') throw new Error('Clipboard unavailable');
        await writeText.call(globalThis.navigator.clipboard, userCode);
        copyFeedback.textContent = 'Code copied.';
      } catch (_) {
        copyFeedback.textContent = 'Copy failed. Select the code to copy it.';
      } finally {
        copyButton.disabled = Boolean(flow.codeExpired);
      }
    });
    codeRow.append(code, copyButton);
    flow.codeHost.replaceChildren(
      element('span', { class: 'provider-control-oauth-code-label' }, 'Sign-in code'),
      codeRow,
      copyFeedback,
    );
    flow.codeHost.classList?.remove?.('hidden');
    flow.copyButton = copyButton;
    flow.copyFeedback = copyFeedback;
  }
  flow.detailsLoaded = Boolean(flow.instructions.textContent || flow.loginLink || flow.copyButton);
}

function mountOAuthFlow(result, options = {}) {
  const panel = oauthPanel();
  if (!panel) return null;
  const flowId = String(result.flow_id || '');
  const card = element('div', { class: 'admin-card provider-control-oauth-card' });
  const title = options.title
    || (result.mode === 'reauth' ? 'Reconnect provider account' : 'Provider login');
  card.append(element('h2', {}, title));
  const instructions = element('p', {
    class: 'admin-toggle-sub provider-control-oauth-instructions hidden',
  });
  const loginLinkHost = element('div', { class: 'provider-control-oauth-login hidden' });
  const codeHost = element('div', { class: 'provider-control-oauth-code hidden' });
  const status = element('p', {
    class: 'admin-toggle-sub',
    role: 'status',
    'aria-live': 'polite',
  }, options.statusText || `Status: ${result.status || 'pending'}`);
  const cancel = button('Cancel login');
  card.append(instructions, loginLinkHost, codeHost, status, cancel);
  panel.replaceChildren(card);
  const expiresAtMs = oauthExpiryMs(result.expires_at);
  activeOAuth = {
    flowId,
    status,
    timer: null,
    expiresAtMs,
    retryCount: 0,
    instructions,
    loginLinkHost,
    codeHost,
    loginLink: null,
    copyButton: null,
    copyFeedback: null,
    codeExpired: false,
    detailsLoaded: false,
    openLoginOnStart: Boolean(options.openLoginOnStart),
  };
  rememberOAuthFlow(flowId, expiresAtMs);
  addOAuthPublicDetails({ ...result, flow_id: flowId });
  cancel.addEventListener('click', async () => {
    const current = activeOAuth;
    if (!current || current.flowId !== flowId) return;
    cancel.disabled = true;
    try {
      const done = await request(`/oauth/flows/${encodeURIComponent(flowId)}`, { method: 'DELETE' });
      if (done.status === 'complete') {
        status.textContent = 'Provider login saved to the account pool.';
        stopOAuthPolling();
        panel.replaceChildren();
        await changed('Provider login saved to the account pool.');
        return;
      }
      if (done.status === 'cancelled') {
        stopOAuthPolling();
        panel.replaceChildren();
        return;
      }
      status.textContent = `Login ${done.status || 'status unknown'}`;
      cancel.disabled = false;
    } catch (_) {
      if (activeOAuth?.flowId === flowId) {
        status.textContent = 'Cancellation could not be confirmed. Login status checks will continue.';
        cancel.disabled = false;
      }
    }
  });
  return activeOAuth;
}

async function pollOAuth(flowId) {
  if (!activeOAuth || activeOAuth.flowId !== flowId) return;
  try {
    const result = await request(`/oauth/flows/${encodeURIComponent(flowId)}`);
    if (!activeOAuth || activeOAuth.flowId !== flowId) return;
    addOAuthPublicDetails({ ...result, flow_id: flowId });
    if (Number.isFinite(Date.parse(String(result.expires_at || '')))) {
      activeOAuth.expiresAtMs = oauthExpiryMs(result.expires_at);
      rememberOAuthFlow(flowId, activeOAuth.expiresAtMs);
    }
    activeOAuth.retryCount = 0;
    activeOAuth.status.textContent = `Status: ${result.status}`;
    if (result.status === 'complete') {
      stopOAuthPolling();
      oauthPanel()?.replaceChildren();
      await changed('Provider login saved to the account pool.');
      return;
    }
    if (['failed', 'cancelled', 'expired'].includes(result.status)) {
      activeOAuth.status.textContent = `Login ${result.status}${result.error_code ? ` · ${result.error_code}` : ''}`;
      if (activeOAuth.copyButton) {
        activeOAuth.codeExpired = true;
        activeOAuth.copyButton.disabled = true;
        activeOAuth.copyFeedback.textContent = result.status === 'expired'
          ? 'This sign-in code has expired.'
          : 'This sign-in code is no longer available.';
      }
      stopOAuthPolling();
      return;
    }
    activeOAuth.timer = setTimeout(() => pollOAuth(flowId), 1200);
  } catch (_) {
    const flow = activeOAuth;
    if (!flow || flow.flowId !== flowId) return;
    if (Date.now() >= flow.expiresAtMs + OAUTH_STATUS_RETENTION_MS) {
      flow.status.textContent = 'Login status could not be confirmed. Check Added Models before starting another login.';
      stopOAuthPolling();
      return;
    }
    flow.retryCount += 1;
    const delay = Math.min(
      OAUTH_STATUS_RETRY_BASE_MS * (2 ** Math.min(flow.retryCount - 1, 4)),
      OAUTH_STATUS_RETRY_MAX_MS,
    );
    flow.status.textContent = `Could not check login status. Retrying in ${Math.ceil(delay / 1000)} seconds…`;
    flow.timer = setTimeout(() => pollOAuth(flowId), delay);
  }
}

function restoreOAuthFlow() {
  const saved = readOAuthFlowSession();
  if (!saved || !oauthPanel()) return;
  mountOAuthFlow({
    flow_id: saved.flowId,
    status: 'pending',
    expires_at: new Date(saved.expiresAtMs).toISOString(),
  }, {
    title: 'Restoring provider login',
    statusText: 'Restoring login status…',
  });
  pollOAuth(saved.flowId);
}

async function startOAuth(connection, account = null, method = null, inputs = {}) {
  const panel = oauthPanel();
  if (!panel) return;
  const selectedMethod = method || authMethodsFor(familyFor(connection)).find(item => item.type === 'oauth');
  if (!selectedMethod) {
    setStatus('This provider does not offer a login method.', true);
    return false;
  }
  stopOAuthPolling();
  panel.replaceChildren(element('p', { class: 'admin-toggle-sub' }, 'Starting provider login…'));
  try {
    const path = account
      ? `/accounts/${encodeURIComponent(account.id)}/oauth/reauth`
      : `/connections/${encodeURIComponent(connection.id)}/oauth/start`;
    const result = await request(path, {
      method: 'POST',
      mutation: true,
      revision: account?.revision,
      body: {
        label: account?.label || `Account ${accountsFor(connection.id).length + 1}`,
        method: oauthMethodIndex(selectedMethod),
        inputs,
      },
    });
    const status = mountOAuthFlow(result, {
      title: account ? `Reconnect ${account.label}` : `Add ${connection.label} login`,
      openLoginOnStart: true,
    })?.status;
    if (!status) return false;
    if (result.method === 'code') {
      const stateInput = element('input', { class: 'settings-input', type: 'text', autocomplete: 'off', placeholder: 'State from the callback URL', value: result.state || '' });
      const codeInput = element('input', { class: 'settings-input', type: 'text', autocomplete: 'off', placeholder: 'Authorization code (if provided)' });
      const finish = button('Complete login', 'admin-btn-add');
      finish.addEventListener('click', async () => {
        finish.disabled = true;
        try {
          const done = await request(`/oauth/flows/${encodeURIComponent(result.flow_id)}/callback`, {
            method: 'POST',
            mutation: true,
            body: { state: stateInput.value.trim(), code: codeInput.value.trim() || null },
          });
          codeInput.value = '';
          status.textContent = `Status: ${done.status}`;
          if (done.status === 'complete') {
            stopOAuthPolling();
            panel.replaceChildren();
            await changed('Provider login saved to the account pool.');
          }
        } catch (error) {
          codeInput.value = '';
          status.textContent = error.message;
          finish.disabled = false;
        }
      });
      status.parentElement?.insertBefore(field('OAuth state', stateInput), status);
      status.parentElement?.insertBefore(field('Authorization code', codeInput), status);
      status.parentElement?.insertBefore(finish, status);
    }
    pollOAuth(result.flow_id);
    return true;
  } catch (error) {
    panel.replaceChildren();
    setStatus(error.message, true);
    return false;
  }
}

function shareIncludesModel(share, model) {
  if (!share || share.connection_id !== model.connection_id) return false;
  const selector = share.model_selector || {};
  if (selector.mode === 'all_live_models') return true;
  return selector.mode === 'explicit_models'
    && Array.isArray(selector.model_route_ids)
    && selector.model_route_ids.includes(model.id);
}

function sharedRecipientsFor(model) {
  return [...new Set(state.ownedShares
    .filter(share => shareIncludesModel(share, model))
    .map(share => String(share.recipient || '').trim().toLowerCase())
    .filter(Boolean))];
}

function renderModelShareControl(model) {
  const wrapper = element('div', { class: 'provider-control-model-share' });
  const chooserOpen = openShareChoosers.has(model.id);
  const summary = button(
    'Share',
    'admin-btn-sm provider-control-model-share-button',
  );
  summary.setAttribute('aria-label', `Choose who can use ${model.display_name || model.model_id}`);
  summary.setAttribute('aria-expanded', chooserOpen ? 'true' : 'false');
  summary.dataset.providerFocusKey = `model:${model.id}:share`;
  const chooser = element('div', {
    class: `provider-control-model-share-users${chooserOpen ? '' : ' hidden'}`,
    'data-provider-model-share-users': model.id,
  });
  const drawChooser = () => {
    const selected = new Set(sharedRecipientsFor(model));
    summary.textContent = selected.size ? `Shared with ${selected.size}` : 'Share';
    const recipients = [...new Set(state.shareRecipients
      .map(recipient => String(recipient?.username || '').trim().toLowerCase())
      .filter(Boolean))].sort((left, right) => left.localeCompare(right));
    chooser.replaceChildren();
    if (!recipients.length) {
      chooser.append(element('span', { class: 'admin-toggle-sub' }, 'No other users are available.'));
      return;
    }
    for (const username of recipients) {
      const input = element('input', {
        type: 'checkbox',
        checked: selected.has(username),
        'aria-label': `Share ${model.display_name || model.model_id} with ${username}`,
      });
      const option = element('label', { class: 'provider-control-model-share-user' });
      option.append(input, element('span', {}, username));
      input.addEventListener('change', async event => {
        event.stopPropagation?.();
        const wanted = input.checked;
        input.disabled = true;
        summary.disabled = true;
        try {
          const result = await request(`/models/${encodeURIComponent(model.id)}/shares/${encodeURIComponent(username)}`, {
            method: 'PUT',
            mutation: true,
            body: { enabled: wanted },
          });
          state.ownedShares = state.ownedShares.filter(share => !(
            share.recipient === username && shareIncludesModel(share, model)
          ));
          if (wanted && result?.share) state.ownedShares.push(result.share);
          openShareChoosers.add(model.id);
          drawChooser();
          setStatus(wanted ? `Shared ${model.display_name || model.model_id} with ${username}.` : `Stopped sharing ${model.display_name || model.model_id} with ${username}.`);
          Promise.resolve(announceCatalogChange()).catch(error => {
            console.warn('[provider-control] catalogue refresh failed', error);
          });
        } catch (error) {
          input.checked = !wanted;
          input.disabled = false;
          setStatus(error.message, true);
        } finally {
          summary.disabled = false;
        }
      });
      chooser.append(option);
    }
  };

  if (shareDirectoryLoaded) drawChooser();
  else if (chooserOpen) {
    chooser.replaceChildren(element('span', { class: 'admin-toggle-sub' }, 'Loading people…'));
    chooser.setAttribute('aria-busy', 'true');
    void loadShareDirectory().then(() => {
      chooser.removeAttribute?.('aria-busy');
      if (openShareChoosers.has(model.id)) drawChooser();
    }).catch(error => {
      chooser.removeAttribute?.('aria-busy');
      chooser.replaceChildren(element('span', { class: 'admin-toggle-sub' }, error.message));
      setStatus(error.message, true);
    });
  }
  summary.addEventListener('click', async event => {
    event.preventDefault?.();
    event.stopPropagation?.();
    const open = chooser.classList?.toggle?.('hidden') === false;
    summary.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      openShareChoosers.add(model.id);
      if (!shareDirectoryLoaded) {
        chooser.replaceChildren(element('span', { class: 'admin-toggle-sub' }, 'Loading people…'));
        chooser.setAttribute('aria-busy', 'true');
        summary.disabled = true;
        try {
          await loadShareDirectory();
          drawChooser();
        } catch (error) {
          chooser.replaceChildren(element('span', { class: 'admin-toggle-sub' }, error.message));
          setStatus(error.message, true);
        } finally {
          chooser.removeAttribute?.('aria-busy');
          summary.disabled = false;
        }
      }
    } else {
      openShareChoosers.delete(model.id);
    }
  });
  chooser.addEventListener('click', event => event.stopPropagation?.());
  wrapper.append(summary, chooser);
  return wrapper;
}

function renderModels(connection) {
  const models = modelsFor(connection.id);
  const section = element('div', { class: 'provider-control-models' });
  const toolbar = element('div', { class: 'provider-control-model-toolbar' });
  toolbar.append(element('strong', {}, 'Models'));
  const tools = element('div', { class: 'provider-control-model-tools' });
  const refresh = button('Refresh models');
  refresh.dataset.providerFocusKey = `connection:${connection.id}:refresh-models`;
  refresh.addEventListener('click', async () => {
    refresh.disabled = true;
    try {
      await request(`/connections/${encodeURIComponent(connection.id)}/models/refresh`, {
        method: 'POST', mutation: true, revision: connection.revision, body: {},
      });
      await changed(`${connection.label} model list refreshed.`);
    } catch (error) {
      // Keep the last known route list visible when discovery is temporarily unavailable.
      setStatus(`Could not refresh ${connection.label} models: ${error.message}`, true);
      refresh.disabled = false;
    }
  });
  tools.append(refresh);
  toolbar.append(tools);
  const contents = element('div', { class: 'provider-control-model-list' });
  let search = null;
  if (models.length >= 8) {
    search = element('input', {
      class: 'mcp-tools-search provider-control-model-search',
      type: 'search',
      placeholder: `Search ${models.length} models…`,
      'aria-label': `Search models from ${connection.label}`,
    });
    search.value = modelSearchQueries.get(connection.id) || '';
    search.dataset.providerFocusKey = `connection:${connection.id}:model-search`;
    search.addEventListener('input', () => {
      const needle = search.value.trim().toLowerCase();
      modelSearchQueries.set(connection.id, search.value);
      for (const row of contents.children || []) {
        row.hidden = Boolean(needle && !String(row.dataset.search || '').includes(needle));
      }
    });
  }
  const draw = () => {
    contents.replaceChildren();
    if (!models.length) {
      contents.append(element('p', { class: 'admin-toggle-sub' }, 'No model routes are published for this connection yet.'));
      return;
    }
    for (const model of models) {
      const eligibility = state.eligibility.get(model.id);
      const eligible = eligibility?.filter(row => row.eligible).length;
      const modelName = model.display_name || model.model_id;
      const providerModelId = model.model_id && model.model_id !== modelName ? model.model_id : '';
      const readiness = eligibility === undefined
        ? 'Available'
        : eligibility.length === 0
          ? (connection.billing_lane === 'local'
              ? 'Local server'
              : (loadedConnectionAccounts.has(connection.id)
                  ? accountsFor(connection.id).length
                  : Number(connection.account_count || 0))
                ? 'Account connected'
                : 'No account connected')
          : eligible > 0
            ? `${eligible} account${eligible === 1 ? '' : 's'} ready`
            : 'Account needs attention';
      const item = element('div', { class: 'provider-control-model' });
      item.dataset.search = `${modelName} ${providerModelId}`.toLowerCase();
      item.hidden = Boolean(search?.value.trim() && !item.dataset.search.includes(search.value.trim().toLowerCase()));
      item.append(
        element('span', { class: 'adm-check-dot provider-control-model-dot', 'aria-hidden': 'true' }),
        element('span', { class: 'provider-control-model-copy' }, ''),
      );
      item.children[1].append(
        element('strong', {}, modelName),
        element('span', { class: 'admin-toggle-sub' }, [providerModelId, readiness].filter(Boolean).join(' · ')),
      );
      if (model.enabled !== false && String(model.visibility || 'visible') === 'visible') {
        item.append(renderModelShareControl(model));
      }
      contents.append(item);
    }
  };
  draw();
  section.append(toolbar);
  if (search) section.append(search);
  section.append(contents);
  section.refreshHealth = async (options = {}) => {
    if (section.dataset.healthLoaded === '1' && !options.force) return;
    try {
      await loadConnectionEligibility(connection.id);
      section.dataset.healthLoaded = '1';
      draw();
    } catch (error) {
      setStatus(`Could not load ${connection.label} model health: ${error.message}`, true);
    }
  };
  return section;
}

function renderConnection(connection) {
  const renderSignature = connectionRenderSignature(connection);
  const card = element('section', {
    class: `provider-control-connection${connection.enabled ? '' : ' provider-control-disabled'}`,
    'data-provider-connection-id': connection.id,
    'data-provider-render-signature': renderSignature,
  });
  const family = familyFor(connection);
  const normalizedLabel = value => String(value || '').toLowerCase().replace(/[^a-z0-9]+/g, '');
  const displayLabel = family?.display_name
    && normalizedLabel(connection.label) === normalizedLabel(family.display_name)
    ? family.display_name
    : connection.label;
  const heading = element('div', { class: 'provider-control-connection-heading' });
  const expand = button('', 'provider-control-connection-toggle');
  expand.dataset.providerFocusKey = `connection:${connection.id}:toggle`;
  const initiallyOpen = openConnections.has(connection.id);
  expand.setAttribute('aria-expanded', initiallyOpen ? 'true' : 'false');
  expand.setAttribute('aria-controls', `provider-control-connection-body-${connection.id}`);
  const identity = element('span', { class: 'provider-control-connection-identity' });
  identity.append(
    iconNode(familyLogo(family) || providerLogo(connection.label) || '', 'provider-control-connection-logo'),
    element('span', { class: 'provider-control-connection-copy' }, ''),
  );
  const loadedAccounts = loadedConnectionAccounts.has(connection.id);
  const projectedAccountCount = Number(connection.account_count);
  const accountCountKnown = loadedAccounts
    || (Number.isInteger(projectedAccountCount) && projectedAccountCount >= 0);
  const accountCount = loadedAccounts
    ? accountsFor(connection.id).length
    : (accountCountKnown ? projectedAccountCount : 0);
  const accountSummary = !accountCountKnown
    ? (connection.billing_lane === 'local' ? 'No key required' : 'Account details on open')
    : !accountCount && connection.billing_lane === 'local'
      ? 'No key required'
      : `${accountCount || 'No'} account${accountCount === 1 ? '' : 's'}`;
  const title = identity.children[1];
  title.append(
    element('strong', { class: 'provider-control-connection-name' }, displayLabel),
    element('span', { class: 'admin-toggle-sub' }, 'Click to manage models'),
  );
  const badges = element('div', { class: 'provider-control-badges' });
  const isLocal = connection.kind === 'local' || connection.billing_lane === 'local';
  const modelCount = modelsFor(connection.id).length;
  badges.append(
    element('span', { class: 'admin-badge provider-control-kind-badge' }, isLocal ? 'LOCAL' : 'API'),
    element('span', { class: `admin-badge${connection.enabled ? '' : ' admin-badge-off'}` }, connection.enabled ? `${modelCount} model${modelCount === 1 ? '' : 's'}` : 'Disabled'),
  );
  const chevron = element('span', { class: 'provider-control-connection-chevron', 'aria-hidden': 'true' });
  chevron.innerHTML = CARET_ICON;
  expand.append(identity, badges, chevron);
  const headingActions = element('div', { class: 'provider-control-connection-actions' });
  const toggle = button(connection.enabled ? 'Disable' : 'Enable');
  toggle.dataset.providerFocusKey = `connection:${connection.id}:enabled`;
  toggle.addEventListener('click', async () => {
    toggle.disabled = true;
    try {
      await request(`/connections/${encodeURIComponent(connection.id)}`, {
        method: 'PATCH',
        mutation: true,
        revision: connection.revision,
        body: { enabled: !connection.enabled },
      });
      await changed(`Connection ${connection.enabled ? 'disabled' : 'enabled'}.`);
    } catch (error) {
      setStatus(error.message, true);
      toggle.disabled = false;
    }
  });
  const remove = button('Delete', 'admin-btn-delete');
  remove.dataset.providerFocusKey = `connection:${connection.id}:delete`;
  remove.addEventListener('click', async () => {
    const confirmDelete = window.styledConfirm || window.confirm;
    const allowed = typeof confirmDelete !== 'function'
      || await confirmDelete(`Delete ${connection.label} and its accounts?`, { confirmText: 'Delete', danger: true });
    if (!allowed) return;
    remove.disabled = true;
    try {
      await request(`/connections/${encodeURIComponent(connection.id)}`, {
        method: 'DELETE',
        mutation: true,
        revision: connection.revision,
      });
      await changed('Connection removed.');
    } catch (error) {
      setStatus(error.message, true);
      remove.disabled = false;
    }
  });
  headingActions.append(toggle, remove);
  heading.append(expand, headingActions);
  card.append(heading);
  const metadata = element('div', { class: 'provider-control-url' },
    [connection.url || family?.display_name || connection.family_id, accountSummary].filter(Boolean).join(' · '),
  );
  card.append(metadata);
  const body = element('div', {
    id: `provider-control-connection-body-${connection.id}`,
    class: `provider-control-connection-body${initiallyOpen ? '' : ' hidden'}`,
  });
  card.append(body);

  let modelsView = null;
  let advanced = null;
  let advancedBody = null;
  const drawAccounts = () => {
    if (!advancedBody) return;
    advancedBody.replaceChildren(
      element('div', { class: 'admin-toggle-sub provider-control-technical' }, `${connection.kind.replaceAll('_', ' ')} · ${connection.adapter_id} · ${laneLabel(connection.billing_lane)}`),
    );
    if (!loadedConnectionAccounts.has(connection.id)) {
      advancedBody.append(element('p', { class: 'admin-toggle-sub' }, 'Open to load account details.'));
      return;
    }
    const pool = element('div', { class: 'provider-control-pool' });
    const accounts = accountsFor(connection.id);
    pool.append(element('h3', {}, accounts.length > 1 ? `Account rotation pool (${accounts.length})` : `Accounts (${accounts.length})`));
    if (accounts.length) {
      accounts.forEach((account, index) => pool.append(renderAccount(connection, account, index, accounts)));
    } else {
      pool.append(element('p', { class: 'admin-toggle-sub' },
        connection.billing_lane === 'local'
          ? 'This local lane does not require an account.'
          : 'No accounts yet. Add account remains available so credentials never overwrite one another.',
      ));
    }
    renderAddAccount(connection, pool);
    advancedBody.append(pool);
  };
  const loadAccounts = async () => {
    if (!advanced?.open || loadedConnectionAccounts.has(connection.id)) return;
    advancedBody?.replaceChildren(element('p', { class: 'admin-toggle-sub' }, 'Loading account details…'));
    try {
      await loadConnectionAccounts(connection.id);
      if (advanced?.open) drawAccounts();
    } catch (error) {
      if (advancedBody) advancedBody.replaceChildren(element('p', { class: 'admin-toggle-sub' }, error.message));
      setStatus(`Could not load ${connection.label} accounts: ${error.message}`, true);
    }
  };
  const buildBody = () => {
    if (body.dataset.rendered === '1') return;
    body.dataset.rendered = '1';
    modelsView = renderModels(connection);
    advanced = element('details', { class: 'provider-control-connection-advanced' });
    advanced.open = openAccountDetails.has(connection.id);
    const advancedSummary = element('summary', {
      'data-provider-focus-key': `connection:${connection.id}:advanced`,
    }, accountCount > 1 ? 'Accounts & advanced' : 'Account & advanced');
    advanced.append(advancedSummary);
    advancedBody = element('div', { class: 'provider-control-connection-advanced-body' });
    drawAccounts();
    advanced.append(advancedBody);
    advanced.addEventListener('toggle', async () => {
      if (advanced.open) {
        openAccountDetails.add(connection.id);
        await loadAccounts();
      } else {
        openAccountDetails.delete(connection.id);
      }
    });
    body.append(modelsView, advanced);
    void modelsView.refreshHealth?.();
    if (advanced.open) void loadAccounts();
  };
  if (initiallyOpen) buildBody();
  expand.addEventListener('click', async () => {
    const open = body.classList?.contains?.('hidden');
    body.classList?.toggle?.('hidden', !open);
    card.classList?.toggle?.('is-open', open);
    expand.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      openConnections.add(connection.id);
      buildBody();
      await modelsView?.refreshHealth?.();
    } else {
      openConnections.delete(connection.id);
    }
  });
  return card;
}

function renderReceivedShareConnection(share) {
  const models = Array.isArray(share.models) ? share.models : [];
  const card = element('section', {
    class: 'provider-control-connection provider-control-shared-connection',
    'data-provider-shared-group-id': share.provider_group_id || share.id,
    'data-provider-render-signature': receivedShareRenderSignature(share),
  });
  const heading = element('div', { class: 'provider-control-connection-heading' });
  const identity = element('div', { class: 'provider-control-connection-identity' });
  identity.append(
    iconNode(providerLogo(share.family_id) || API_ICON, 'provider-control-connection-logo'),
    element('span', { class: 'provider-control-connection-copy' }, ''),
  );
  const rawGroupLabel = String(share.provider_group_label || '').trim();
  const providerName = providerDisplayName(
    share.provider_display_name
      || share.provider_family_name
      || share.provider,
  );
  const groupLabel = sharedProviderLabel(providerName);
  const secondaryLabel = sharedSecondaryLabel({
    label: [rawGroupLabel, share.label].filter(Boolean).join(' · '),
    owner: share.shared_by,
  });
  identity.children[1].append(
    element('strong', { class: 'provider-control-connection-name' }, groupLabel),
    element('span', { class: 'admin-toggle-sub' }, secondaryLabel || 'Shared with you'),
  );
  const badges = element('div', { class: 'provider-control-badges' });
  badges.append(
    element('span', { class: 'admin-badge provider-control-kind-badge' }, 'API'),
    element('span', { class: 'admin-badge' }, 'READ ONLY'),
  );
  heading.append(identity, badges);
  card.append(
    heading,
    element('div', { class: 'provider-control-url' }, `${models.length} shared model${models.length === 1 ? '' : 's'}`),
  );
  const contents = element('div', { class: 'provider-control-model-list provider-control-shared-model-list' });
  if (!models.length) {
    contents.append(element('p', { class: 'admin-toggle-sub' }, 'No live models are available in this share.'));
  } else {
    for (const model of models) {
      const modelName = model.display_name || model.model_id || 'Shared model';
      const providerModelId = model.model_id && model.model_id !== modelName ? model.model_id : '';
      const row = element('div', { class: 'provider-control-model provider-control-shared-model' });
      row.append(
        element('span', { class: 'adm-check-dot provider-control-model-dot', 'aria-hidden': 'true' }),
        element('span', { class: 'provider-control-model-copy' }, ''),
      );
      row.children[1].append(
        element('strong', {}, modelName),
        element('span', { class: 'admin-toggle-sub' }, [providerModelId, 'Available'].filter(Boolean).join(' · ')),
      );
      contents.append(row);
    }
  }
  card.append(contents);
  return card;
}

function groupedReceivedShares() {
  const groups = new Map();
  for (const share of state.receivedShares) {
    const key = share.provider_group_id || share.connection_slot_id || share.id;
    if (!groups.has(key)) {
      groups.set(key, {
        ...share,
        id: key,
        provider_group_id: key,
        models: [],
        grant_ids: [],
      });
    }
    const group = groups.get(key);
    group.grant_ids.push(share.id);
    const known = new Set(group.models.map(model => String(model.model_id || model.display_name || '')));
    for (const model of Array.isArray(share.models) ? share.models : []) {
      const identity = String(model.model_id || model.display_name || '');
      if (!identity || known.has(identity)) continue;
      known.add(identity);
      group.models.push(model);
    }
  }
  return [...groups.values()].sort((left, right) =>
    String(left.provider_group_label || left.label || '').localeCompare(
      String(right.provider_group_label || right.label || ''),
    ),
  );
}

function connectionRenderSignature(connection) {
  return JSON.stringify({
    connection,
    family: familyFor(connection),
    models: modelsFor(connection.id),
    deferredInvalidationGeneration,
  });
}

function receivedShareRenderSignature(share) {
  return JSON.stringify({ share, deferredInvalidationGeneration });
}

function reconcileChildren(parent, desired) {
  if (typeof parent.insertBefore !== 'function') {
    parent.replaceChildren(...desired);
    return;
  }
  const wanted = new Set(desired);
  for (const child of [...parent.children]) {
    if (!wanted.has(child)) child.remove();
  }
  desired.forEach((child, index) => {
    const current = parent.children[index] || null;
    if (current !== child) parent.insertBefore(child, current);
  });
}

function renderConnections(options = {}) {
  const host = byId('provider-control-connections');
  if (!host) return;
  const renderState = captureListRenderState(host);
  const receivedGroups = groupedReceivedShares();
  if (!state.connections.length && !receivedGroups.length) {
    const empty = element('div', { class: 'admin-empty provider-control-empty' });
    empty.append(element('p', {}, 'No models added yet. Connect an API provider or local model server to get started.'));
    const add = button('Add models', 'admin-btn-add');
    add.addEventListener('click', () => document.querySelector?.('[data-settings-tab="services"]')?.click?.());
    empty.append(add);
    host.replaceChildren(empty);
    restoreListRenderState(host, renderState);
    return;
  }
  const existingConnections = new Map(
    [...(host.querySelectorAll?.('[data-provider-connection-id]') || [])]
      .map(card => [card.dataset.providerConnectionId, card]),
  );
  const existingShares = new Map(
    [...(host.querySelectorAll?.('[data-provider-shared-group-id]') || [])]
      .map(card => [card.dataset.providerSharedGroupId, card]),
  );
  const existingGroups = new Map(
    [...(host.querySelectorAll?.('[data-provider-connection-group]') || [])]
      .map(section => [section.dataset.providerConnectionGroup, section]),
  );
  const connectionRow = connection => {
    const existing = existingConnections.get(connection.id);
    return existing?.dataset.providerRenderSignature === connectionRenderSignature(connection)
      ? existing
      : renderConnection(connection);
  };
  const receivedRow = share => {
    const id = String(share.provider_group_id || share.id || '');
    const existing = existingShares.get(id);
    return existing?.dataset.providerRenderSignature === receivedShareRenderSignature(share)
      ? existing
      : renderReceivedShareConnection(share);
  };
  const local = state.connections.filter(connection => connection.kind === 'local' || connection.billing_lane === 'local');
  const api = state.connections.filter(connection => !local.includes(connection));
  const groups = [];
  const groupRows = [
    ['local', 'Local', local.map(connectionRow)],
    ['api', 'API', [...api.map(connectionRow), ...receivedGroups.map(receivedRow)]],
  ];
  for (const [id, label, rows] of groupRows) {
    if (!rows.length) continue;
    const section = existingGroups.get(id)
      || element('section', { class: 'provider-control-connection-group', 'data-provider-connection-group': id });
    let heading = section.querySelector?.('.provider-control-group-heading');
    if (!heading) {
      heading = element('div', { class: 'provider-control-group-heading' });
      heading.append(
        iconNode(id === 'local' ? LOCAL_ICON : API_ICON, 'provider-control-group-icon'),
        element('span', {}, label),
        element('span', { class: 'provider-control-group-count' }, String(rows.length)),
      );
    } else {
      const count = heading.querySelector?.('.provider-control-group-count');
      if (count) count.textContent = String(rows.length);
    }
    reconcileChildren(section, [heading, ...rows]);
    groups.push(section);
  }
  reconcileChildren(host, groups);
  if (options.refreshOpenHealth) {
    for (const connectionId of openConnections) {
      const card = [...(host.querySelectorAll?.('[data-provider-connection-id]') || [])]
        .find(item => item.dataset.providerConnectionId === connectionId);
      void card?.querySelector?.('.provider-control-models')?.refreshHealth?.({ force: true });
    }
  }
  restoreListRenderState(host, renderState);
}

function bindingFor(purpose) {
  return state.bindings.find(item => item.purpose === purpose) || { purpose, revision: 0, routes: [] };
}

function renderBindings() {
  const host = byId('provider-control-bindings');
  if (!host) return;
  host.replaceChildren();
  for (const [purpose, label] of PURPOSES) {
    const binding = bindingFor(purpose);
    const available = compatibleModels(purpose);
    const card = element('div', { class: 'provider-control-binding' });
    const heading = element('div', { class: 'provider-control-binding-heading' });
    heading.append(element('strong', {}, label), element('span', { class: 'admin-toggle-sub' }, 'Selected model'));
    const selected = (binding.routes || []).find(route => route.enabled !== false)?.model_route_id || '';
    const select = createThemedPicker({
      label: `${label} selected model`,
      value: selected,
      searchable: available.length > 6,
      options: [
        { value: '', label: 'Not selected' },
        ...available.map(model => ({
          value: model.id,
          label: model.display_name || model.model_id,
          hint: state.connections.find(item => item.id === model.connection_id)?.label || 'connection',
          logo: providerLogo(model.model_id || model.display_name) || '',
        })),
      ],
    });
    const save = button('Save selected model', 'admin-btn-add');
    save.addEventListener('click', async () => {
      const nextModelRouteId = select.value;
      if (!nextModelRouteId && !(binding.routes || []).length) return;
      save.disabled = true;
      try {
        if (!nextModelRouteId) {
          await request(`/bindings/${encodeURIComponent(purpose)}`, {
            method: 'DELETE', mutation: true, revision: binding.revision,
          });
        } else {
          await request(`/bindings/${encodeURIComponent(purpose)}`, {
            method: 'PUT',
            mutation: true,
            revision: binding.revision,
            body: { routes: [{ model_route_id: nextModelRouteId, enabled: true }] },
          });
        }
        await changed(`${label} selected model saved.`);
      } catch (error) {
        setStatus(error.message, true);
        save.disabled = false;
      }
    });
    const controls = element('div', { class: 'provider-control-actions' });
    controls.append(select.root, save);
    card.append(heading, controls);
    host.append(card);
  }
}

function renderSummary() {
  const receivedGroups = groupedReceivedShares();
  const providerCount = state.connections.length + receivedGroups.length;
  const sharedModelCount = receivedGroups.reduce((count, share) => count + share.models.length, 0);
  const modelCount = state.models.length + sharedModelCount;
  const projectedCounts = state.connections.map(connection => {
    if (loadedConnectionAccounts.has(connection.id)) return accountsFor(connection.id).length;
    const count = Number(connection.account_count);
    return Number.isInteger(count) && count >= 0 ? count : null;
  });
  const accountsKnown = projectedCounts.every(count => count !== null);
  const accountCount = accountsKnown
    ? projectedCounts.reduce((count, value) => count + value, 0)
    : null;
  const addSummary = byId('provider-control-summary');
  if (addSummary) {
    const familyCount = state.families.filter(item => item.id !== 'local-executor').length;
    addSummary.textContent = familyCount
      ? `${familyCount} provider${familyCount === 1 ? '' : 's'} available`
      : '';
  }
  const addedSummary = byId('provider-control-added-summary');
  if (addedSummary) {
    addedSummary.textContent = [
      `${providerCount} provider${providerCount === 1 ? '' : 's'}`,
      accountCount === null ? null : `${accountCount} account${accountCount === 1 ? '' : 's'}`,
      `${modelCount} model${modelCount === 1 ? '' : 's'}`,
    ].filter(Boolean).join(' · ');
  }
}

function render() {
  renderCreateConnection();
  renderConnections();
  renderBindings();
  renderSummary();
}

function activeManagementView(requested = '') {
  if (requested === 'services' || requested === 'added-models') return requested;
  const added = document.querySelector?.('[data-settings-panel="added-models"]');
  return added && !added.classList?.contains?.('hidden') ? 'added-models' : 'services';
}

function viewIsFresh(view) {
  const completedAt = Number(loadedAt.get(view) || 0);
  const age = Date.now() - completedAt;
  return Number.isFinite(age) && age >= 0 && age <= VIEW_FRESH_MAX_AGE_MS;
}

function renderView(view) {
  if (view === 'services') {
    renderCreateConnection();
  } else {
    renderConnections();
    if (bindingsLoaded) renderBindings();
  }
  renderSummary();
}

function invalidateLoadedViews() {
  cancelInFlightDetailLoads();
  deferredInvalidationGeneration += 1;
  loadedAt.clear();
  loadedConnectionAccounts.clear();
  shareDirectoryLoaded = false;
  bindingsLoaded = false;
}

export async function load(options = {}) {
  if (!root) root = byId('provider-control-root');
  if (!root) return;
  const view = activeManagementView(options.view);
  if (loading) {
    if (!options.force && loading.view === view) return loading.promise;
    loading.controller.abort();
  }
  if (!options.force && viewIsFresh(view)) {
    // A fresh revisit is a true no-op for both network and DOM ownership.
    // Keeping the existing nodes preserves typed credentials/URLs, expansion,
    // focus, and scroll position while the tab was hidden.
    renderSummary();
    return;
  }
  loadedAt.delete(view);
  if (view === 'added-models') cancelInFlightDetailLoads();
  if (!state.families.length) {
    const cachedFamilies = readCachedFamilies();
    if (cachedFamilies.length) {
      state = { ...state, families: cachedFamilies };
    }
    renderCreateConnection();
    renderSummary();
  }
  const generation = ++loadGeneration;
  const controller = new AbortController();
  setStatus('Loading provider control plane…');
  const task = (async () => {
    let renderedProgress = false;
    try {
      const result = await fetchState(view, (next, warnings, phase) => {
        if (generation !== loadGeneration) return;
        state = next;
        renderedProgress = true;
        if (phase === 'catalog') {
          renderCreateConnection();
          if (view === 'added-models') renderConnections();
        }
        else if (phase === 'core' || phase === 'shared') renderConnections({
          refreshOpenHealth: phase === 'core',
        });
        else {
          renderConnections();
          renderBindings();
        }
        renderSummary();
        const progressMessage = phase === 'catalog'
          ? 'Provider catalog loaded. Loading your models…'
          : 'Added Models snapshot loaded.';
        setStatus(warnings.length ? `Loaded available provider data; ${warnings.length} section${warnings.length === 1 ? '' : 's'} still retrying.` : progressMessage);
      }, controller.signal);
      if (generation !== loadGeneration) return;
      state = result.next;
      if (result.requiredSucceeded) loadedAt.set(view, Date.now());
      if (!renderedProgress) renderView(view);
      else renderSummary();
      if (result.warnings.length) {
        const blocked = !result.requiredSucceeded && (
          view === 'services' ? !state.families.length : !state.connections.length && !state.receivedShares.length
        );
        const subject = view === 'services' ? 'Provider catalogue' : 'Added Models';
        setStatus(`${subject} loaded available data, but ${result.warnings.join('; ')}. Refresh to retry.`, blocked);
      } else {
        setStatus('');
      }
    } catch (error) {
      if (generation === loadGeneration && !controller.signal.aborted) {
        setStatus(error.message, true);
      }
    } finally {
      if (generation === loadGeneration) loading = null;
    }
  })();
  loading = { controller, generation, promise: task, view };
  return task;
}

export function init(options = {}) {
  root = byId('provider-control-root');
  if (!root || bound) return;
  bound = true;
  onCatalogChanged = options.onCatalogChanged || onCatalogChanged;
  byId('provider-control-refresh')?.addEventListener('click', () => load({ force: true, view: 'services' }));
  for (const refresh of document.querySelectorAll?.('[data-provider-control-refresh]') || []) {
    refresh.addEventListener('click', () => load({ force: true, view: 'added-models' }));
  }
  const routingDetails = byId('provider-control-bindings')?.closest?.('details');
  routingDetails?.addEventListener?.('toggle', async () => {
    if (!routingDetails.open || bindingsLoaded) return;
    const host = byId('provider-control-bindings');
    if (host) host.textContent = 'Loading model routing…';
    try {
      await loadBindings();
      if (routingDetails.open) renderBindings();
    } catch (error) {
      if (host) host.textContent = error.message;
      setStatus(error.message, true);
    }
  });
  document.addEventListener?.('open-clank:providers-updated', event => {
    if (event?.detail?.source === 'provider-control') return;
    invalidateLoadedViews();
  });
  document.addEventListener?.('open-clank:open-providers', () => {
    invalidateLoadedViews();
    load({ force: true, view: activeManagementView() });
  });
  restoreOAuthFlow();
}

export function selectConnection(connectionId) {
  const selector = `[data-provider-connection-id="${String(connectionId || '').replace(/["\\]/g, '')}"]`;
  const target = document.querySelector?.(selector) || root?.querySelector?.(selector);
  target?.scrollIntoView?.({ behavior: 'smooth', block: 'start' });
}

export default { init, load, selectConnection };
