import assert from 'node:assert/strict';

class FakeClassList {
  constructor(owner) { this.owner = owner; }
  add(...names) { names.forEach(name => this.owner._classes.add(name)); }
  remove(...names) { names.forEach(name => this.owner._classes.delete(name)); }
  contains(name) { return this.owner._classes.has(name); }
  toggle(name, force) {
    if (force === true) { this.owner._classes.add(name); return true; }
    if (force === false) { this.owner._classes.delete(name); return false; }
    if (this.owner._classes.has(name)) { this.owner._classes.delete(name); return false; }
    this.owner._classes.add(name);
    return true;
  }
}

class FakeNode {
  constructor(tagName = 'div') {
    this.tagName = tagName.toLowerCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.style = {};
    this.textContent = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.required = false;
    this.type = '';
    this._classes = new Set();
    this.classList = new FakeClassList(this);
  }

  set className(value) {
    this._classes = new Set(String(value || '').split(/\s+/).filter(Boolean));
    this.classList = new FakeClassList(this);
  }
  get className() { return [...this._classes].join(' '); }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }

  addEventListener(name, callback) {
    if (!this.listeners.has(name)) this.listeners.set(name, []);
    this.listeners.get(name).push(callback);
  }

  async dispatch(name, event = {}) {
    for (const callback of this.listeners.get(name) || []) {
      await callback({ preventDefault() {}, stopPropagation() {}, target: this, ...event });
    }
  }

  append(...children) {
    for (const child of children) {
      if (child === undefined || child === null) continue;
      child.parentNode = this;
      this.children.push(child);
      if (this.tagName === 'select' && this.children.length === 1 && !this.value) {
        this.value = child.value || '';
      }
    }
  }

  prepend(...children) {
    children.reverse().forEach(child => {
      child.parentNode = this;
      this.children.unshift(child);
    });
  }

  replaceChildren(...children) {
    this.children.forEach(child => { child.parentNode = null; });
    this.children = [];
    this.append(...children);
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
    this.parentNode = null;
  }

  contains(candidate) {
    return this === candidate || this.children.some(child => child.contains(candidate));
  }

  focus() { globalThis.document.activeElement = this; }
  click() { return this.dispatch('click'); }
  scrollIntoView() {}
  reset() {}

  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim());
    const matches = node => selectors.some(item => {
      if (item === '[data-provider-account-form]') return node.attributes.has('data-provider-account-form');
      if (item === 'input[type="checkbox"]') return node.tagName === 'input' && node.type === 'checkbox';
      if (item === 'input[type="checkbox"]:checked') return node.tagName === 'input' && node.type === 'checkbox' && node.checked;
      const connectionMatch = item.match(/^\[data-provider-connection-id="(.+)"\]$/);
      if (connectionMatch) return node.dataset.providerConnectionId === connectionMatch[1];
      return false;
    });
    const result = [];
    const visit = node => node.children.forEach(child => {
      if (matches(child)) result.push(child);
      visit(child);
    });
    visit(this);
    return result;
  }

  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }

  closest(selector) {
    let current = this;
    while (current) {
      if (selector === 'details' && current.tagName === 'details') return current;
      current = current.parentNode;
    }
    return null;
  }
}

function descendants(node, tagName) {
  return [
    ...(node.tagName === tagName ? [node] : []),
    ...node.children.flatMap(child => descendants(child, tagName)),
  ];
}

function allText(node) {
  return [node.textContent, ...node.children.map(allText)].filter(Boolean).join(' ');
}

const ids = new Map();
[
  'provider-control-root',
  'provider-control-refresh',
  'provider-control-new-share',
  'provider-control-summary',
  'provider-control-added-summary',
  'provider-control-status',
  'provider-control-added-status',
  'provider-control-create',
  'provider-control-oauth',
  'provider-control-connections',
  'provider-control-bindings',
].forEach(id => ids.set(id, new FakeNode()));
const routingDetails = new FakeNode('details');
routingDetails.append(ids.get('provider-control-bindings'));

globalThis.window = globalThis;
globalThis.window.confirm = () => true;
const sessionValues = new Map();
globalThis.sessionStorage = {
  getItem(key) { return sessionValues.has(key) ? sessionValues.get(key) : null; },
  setItem(key, value) { sessionValues.set(key, String(value)); },
  removeItem(key) { sessionValues.delete(key); },
};
const documentListeners = new Map();
globalThis.document = {
  createElement: tagName => new FakeNode(tagName),
  getElementById: id => ids.get(id) || null,
  addEventListener(name, callback) {
    if (!documentListeners.has(name)) documentListeners.set(name, []);
    documentListeners.get(name).push(callback);
  },
  removeEventListener(name, callback) {
    documentListeners.set(name, (documentListeners.get(name) || []).filter(item => item !== callback));
  },
  dispatchEvent(event) {
    for (const callback of documentListeners.get(event?.type) || []) callback(event);
  },
  activeElement: null,
};
globalThis.CustomEvent = class CustomEvent { constructor(name, options) { this.type = name; this.detail = options?.detail; } };

const requests = [];
let failReceivedShares = false;
let malformedSnapshot = false;
let failModelRefresh = true;
let heldCorePaths = new Set();
let abortedCoreRequests = 0;
let holdAccountRequest = false;
let abortedAccountRequests = 0;
let ownedShares = [{
  id: 'psg-owned', recipient: 'allie', connection_id: 'pcn-openai-api',
  billing_lane: 'metered_api', label: 'GPT Example',
  account_selector: { mode: 'all_live_accounts' },
  model_selector: { mode: 'explicit_models', model_route_ids: ['pmr-gpt'] },
  disclosure_fields: [], state: 'active', revision: 2, accepted: true,
}];
const family = {
  id: 'openai',
  display_name: 'OpenAI',
  adapters: ['openai-responses'],
  kinds: ['official', 'subscription'],
  billing_lanes: ['metered_api', 'subscription'],
  auth_methods: [
    { id: 'api_key', type: 'api', label: 'API key' },
    { id: 'oauth:0', type: 'oauth', label: 'Browser login' },
  ],
};
const internalFamily = {
  id: 'local-executor',
  display_name: 'Internal executor',
  adapters: ['local-executor'],
  kinds: ['local'],
  billing_lanes: ['local'],
  auth_methods: [{ id: 'none', type: 'none', label: 'No API key' }],
};
const localFamily = {
  id: 'ollama',
  display_name: 'Ollama',
  adapters: ['ollama'],
  kinds: ['local'],
  billing_lanes: ['local'],
  auth_methods: [{ id: 'none', type: 'none', label: 'No API key' }],
};
const connection = {
  id: 'pcn-openai-api',
  family_id: 'openai',
  adapter_id: 'openai-responses',
  kind: 'official',
  billing_lane: 'metered_api',
  label: 'OpenAI API',
  url: null,
  enabled: true,
  revision: 7,
};
const accounts = [
  { id: 'pac-a', connection_id: connection.id, label: 'Primary', auth_method: 'api_key', auth_class: 'metered', order: 0, enabled: true, identity: {}, revision: 2 },
  { id: 'pac-b', connection_id: connection.id, label: 'Backup', auth_method: 'oauth', auth_class: 'metered', order: 1, enabled: true, identity: { organization_tenant: 'Example Org' }, revision: 4 },
];
const model = {
  id: 'pmr-gpt',
  connection_id: connection.id,
  model_id: 'gpt-example',
  display_name: 'GPT Example',
  operations: ['chat.stream', 'chat.complete'],
  capabilities: { tools: true },
  visibility: 'visible',
  enabled: true,
  revision: 1,
};
const receivedShares = [
  {
    id: 'psg-received-a',
    label: 'Research access',
    provider_group_id: 'shared_provider_opaque',
    provider_group_label: "alice's openai",
    family_id: 'openai',
    provider_family_id: 'openai',
    provider_display_name: 'OpenAI',
    shared_by: 'alice',
    billing_lane: 'subscription',
    state: 'active',
    revision: 3,
    disclosure_fields: [],
    accounts: [{ slot_id: 'account_opaque', name: 'Account A', enabled: true }],
    models: [{ slot_id: 'model_opaque_a', model_id: 'safe-model', display_name: 'Safe Model', operations: ['chat.complete'], capabilities: {} }],
    api_key: 'must-never-render',
    source_account_id: 'must-never-render-either',
  },
  {
    id: 'psg-received-b',
    label: 'Second grant from the same provider',
    provider_group_id: 'shared_provider_opaque',
    provider_group_label: "alice's openai",
    family_id: 'openai',
    provider_family_id: 'openai',
    provider_display_name: 'OpenAI',
    shared_by: 'alice',
    billing_lane: 'subscription',
    state: 'active',
    revision: 1,
    disclosure_fields: [],
    accounts: [],
    models: [{ slot_id: 'model_opaque_b', model_id: 'safe-model-2', display_name: 'Safe Model Two', operations: ['chat.complete'], capabilities: {} }],
  },
];

function jsonResponse(data, ok = true, status = 200) {
  return { ok, status, async json() { return data; }, clone() { return this; } };
}

globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  const path = String(url);
  const heldPath = [...heldCorePaths].find(suffix => path.endsWith(suffix));
  if (heldPath) {
    heldCorePaths.delete(heldPath);
    return new Promise((_resolve, reject) => {
      const abort = () => {
        abortedCoreRequests += 1;
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      };
      if (options.signal?.aborted) abort();
      else options.signal?.addEventListener?.('abort', abort, { once: true });
    });
  }
  const shareToggle = path.match(/\/models\/([^/]+)\/shares\/([^/]+)$/);
  if (options.method === 'PUT' && shareToggle) {
    const enabled = Boolean(JSON.parse(options.body).enabled);
    const recipient = decodeURIComponent(shareToggle[2]);
    ownedShares = ownedShares.filter(share => !(share.recipient === recipient && share.model_selector?.model_route_ids?.includes(model.id)));
    if (enabled) ownedShares.push({
      id: `psg-${recipient}`, recipient, connection_id: connection.id,
      billing_lane: 'metered_api', label: model.display_name,
      account_selector: { mode: 'all_live_accounts' },
      model_selector: { mode: 'explicit_models', model_route_ids: [model.id] },
      disclosure_fields: [], state: 'active', revision: 1, accepted: true,
    });
    return jsonResponse({ enabled, share: enabled ? ownedShares.at(-1) : null });
  }
  if (options.method === 'POST' && path.endsWith(`/connections/${connection.id}/models/refresh`)) {
    if (failModelRefresh) return jsonResponse({ detail: 'model discovery unavailable' }, false, 503);
    return jsonResponse({ connection_id: connection.id, revision: 8, models: [model] });
  }
  if (path.endsWith('/families')) return jsonResponse({ schema_version: 1, families: [family, localFamily, internalFamily] });
  if (path.endsWith('/management-snapshot')) {
    if (failReceivedShares) throw new Error('received shares temporarily unavailable');
    if (malformedSnapshot) return jsonResponse({ schema_version: 1, connections: 'not-an-array' });
    return jsonResponse({
      schema_version: 1,
      connections: [{ ...connection, account_count: accounts.length }],
      models: [model],
      shares: { received: receivedShares },
      deferred: ['accounts', 'bindings', 'owned_shares', 'recipients'],
    });
  }
  if (path.endsWith('/connections')) return jsonResponse({ connections: [connection] });
  if (path.endsWith(`/connections/${connection.id}/accounts`)) {
    if (!holdAccountRequest) return jsonResponse({ accounts });
    return new Promise((_resolve, reject) => {
      const abort = () => {
        abortedAccountRequests += 1;
        const error = new Error('aborted');
        error.name = 'AbortError';
        reject(error);
      };
      if (options.signal?.aborted) abort();
      else options.signal?.addEventListener?.('abort', abort, { once: true });
    });
  }
  if (path.endsWith('/models')) return jsonResponse({ models: [model] });
  if (path.endsWith(`/connections/${connection.id}/eligibility`)) {
    return jsonResponse({
      connection_id: connection.id,
      models: [{
        model_route_id: model.id,
        accounts: [
          { account_id: accounts[0].id, eligible: true, account_state: 'healthy', model_state: 'healthy' },
          { account_id: accounts[1].id, eligible: false, account_state: 'cooldown', model_state: 'healthy', account_cooldown_until: '2026-08-09T12:00:00Z' },
        ],
      }],
    });
  }
  if (path.endsWith(`/models/${model.id}/eligibility`)) {
    return jsonResponse({
      model_route_id: model.id,
      accounts: [
        { account_id: accounts[0].id, eligible: true, account_state: 'healthy', model_state: 'healthy' },
        { account_id: accounts[1].id, eligible: false, account_state: 'cooldown', model_state: 'healthy', account_cooldown_until: '2026-08-09T12:00:00Z' },
      ],
    });
  }
  if (path.endsWith('/bindings')) return jsonResponse({ bindings: [{
    purpose: 'chat', revision: 5,
    routes: [
      { model_route_id: model.id, enabled: true, ordinal: 0 },
      { model_route_id: 'pmr-retired-fallback', enabled: true, ordinal: 1 },
    ],
  }] });
  if (path.endsWith('/share-recipients')) return jsonResponse({ recipients: [{ username: 'allie' }, { username: 'mom' }] });
  if (path.endsWith('/shares/received')) {
    if (failReceivedShares) throw new Error('received shares temporarily unavailable');
    return jsonResponse({ shares: receivedShares });
  }
  if (path.endsWith('/shares')) {
    return jsonResponse({ shares: ownedShares });
  }
  throw new Error(`Unexpected request: ${path}`);
};

sessionStorage.setItem('open-clank:provider-families:v1', JSON.stringify({
  format: 1,
  schema_version: 1,
  cached_at: Date.now(),
  families: [family, localFamily, internalFamily],
}));
const providerControl = await import('../static/js/providerControl.js');
let catalogRefreshStarted = 0;
const heldCatalogRefresh = new Promise(() => {});
providerControl.init({
  onCatalogChanged: async () => {
    catalogRefreshStarted += 1;
    await heldCatalogRefresh;
  },
});
const initialLoad = providerControl.load();
assert.match(allText(ids.get('provider-control-create')), /Add API Models/,
  'a validated session family cache paints Add Models before network completion');
assert.match(allText(ids.get('provider-control-create')), /Add Local Models/,
  'the cached first paint includes local providers');
await initialLoad;

assert.equal(requests.length, 1, 'Add Models first paint reads only the family catalogue');
const freshAddRequestCount = requests.length;
await providerControl.load({ view: 'services' });
assert.equal(requests.length, freshAddRequestCount,
  'an immediate Add Models revisit reuses the completed fresh view');
await providerControl.load({ view: 'added-models' });
assert.equal(requests.length, 2,
  'Added Models first paint adds one owner management snapshot');
assert.equal(requests.filter(item => item.url.endsWith('/management-snapshot')).length, 1);
const freshAddedRequestCount = requests.length;
await providerControl.load({ view: 'added-models' });
assert.equal(requests.length, freshAddedRequestCount,
  'an immediate Added Models revisit issues no core reload requests');

heldCorePaths = new Set(['/management-snapshot']);
const supersededLoad = providerControl.load({ force: true, view: 'added-models' });
await Promise.resolve();
const heldRequestCount = requests.length;
const coalescedLoad = providerControl.load({ view: 'added-models' });
await Promise.resolve();
assert.equal(requests.length, heldRequestCount,
  'a non-force visit coalesces with the in-flight management wave');
const replacementLoad = providerControl.load({ force: true, view: 'added-models' });
await Promise.all([supersededLoad, coalescedLoad, replacementLoad]);
assert.equal(abortedCoreRequests, 1,
  'a replacement force load aborts the superseded management snapshot');
assert.ok(requests.every(item => item.url.startsWith('/api/v1/providers')));
assert.equal(requests.some(item => item.url.includes('model-endpoints')), false);
assert.equal(requests.some(item => item.url.includes('/mimo/')), false);
assert.equal(requests.some(item => item.url.includes('/eligibility')), false, 'model health is lazy');
assert.equal(requests.some(item => item.url.endsWith('/bindings')), false, 'routing is lazy');
assert.equal(requests.some(item => item.url.endsWith('/share-recipients')), false, 'share recipients are lazy');
assert.equal(requests.some(item => item.url.endsWith(`/connections/${connection.id}/accounts`)), false,
  'account pools are lazy');

const quickAddText = allText(ids.get('provider-control-create'));
assert.match(quickAddText, /Add API Models/);
assert.match(quickAddText, /Add Local Models/);
assert.match(quickAddText, /Use Browser login instead/);
assert.doesNotMatch(quickAddText, /Internal executor/, 'engine-only providers stay out of quick add');

const quickForms = descendants(ids.get('provider-control-create'), 'form');
assert.deepEqual(quickForms.map(form => form.dataset.providerAddMode).sort(), ['local', 'remote'],
  'Add Models exposes separate compact local and remote forms');
const providerUiNodes = [...ids.values()];
assert.equal(providerUiNodes.flatMap(node => descendants(node, 'select')).length, 0,
  'normalized provider UI contains no native select elements');
const pickerTriggers = providerUiNodes.flatMap(node => descendants(node, 'button'))
  .filter(node => node.attributes.get('role') === 'combobox');
const pickerMenus = providerUiNodes.flatMap(node => descendants(node, 'div'))
  .filter(node => node.attributes.get('role') === 'listbox');
assert.ok(pickerTriggers.length >= 2, 'provider family choices use themed combobox triggers');
assert.ok(pickerMenus.length >= 2, 'provider family choices expose listboxes');
assert.ok(pickerTriggers.every(trigger => (
  trigger.attributes.get('aria-haspopup') === 'listbox'
  && trigger.attributes.get('aria-expanded') === 'false'
  && trigger.attributes.has('aria-controls')
)), 'custom provider pickers expose the expected ARIA contract');

const quickForm = quickForms.find(form => form.dataset.providerAddMode === 'remote');
const quickSecret = descendants(quickForm, 'input').find(item => item.type === 'password');
quickSecret.value = 'write-only-test-key';
await quickForm.dispatch('submit');
assert.equal(catalogRefreshStarted, 1,
  'catalog consumers refresh after the visible Added Models page has repainted');
const quickAccountRequest = requests.find(item => (
  item.options.method === 'POST'
  && item.url.endsWith(`/connections/${connection.id}/accounts`)
  && JSON.parse(item.options.body).api_key === 'write-only-test-key'
));
assert.ok(quickAccountRequest, 'quick add attaches the key to the existing exact connection');
assert.equal(quickSecret.value, '', 'quick-add secret is cleared');

const connectionsText = allText(ids.get('provider-control-connections'));
assert.match(connectionsText, /OpenAI API/);
assert.match(connectionsText, /2 accounts/);
assert.doesNotMatch(connectionsText, /Account rotation pool|Add account|Add API key|Browser login/,
  'collapsed connection rows do not construct hidden account controls');
assert.doesNotMatch(connectionsText, /Use next account/);
assert.doesNotMatch(connectionsText, /gpt-example · Available/,
  'collapsed connection rows do not construct hidden model bodies');
assert.match(connectionsText, /alice's openai/);
assert.match(connectionsText, /Shared OpenAI/);
assert.match(connectionsText, /Safe Model/);
assert.match(connectionsText, /Safe Model Two/);
assert.match(connectionsText, /2 shared models/);
assert.match(connectionsText, /READ ONLY/);
assert.doesNotMatch(connectionsText, /Account A/,
  'received account labels remain absent even when the API response contains one');
assert.doesNotMatch(connectionsText, /Accept share|preferred account/i,
  'received shares are ordinary read-only model rows without account or acceptance controls');
assert.doesNotMatch(connectionsText, /must-never-render/);
assert.doesNotMatch(connectionsText, /chat\.stream|chat\.complete/);

const connectionGroups = descendants(ids.get('provider-control-connections'), 'section')
  .filter(item => item.dataset.providerConnectionGroup);
assert.deepEqual(connectionGroups.map(item => item.dataset.providerConnectionGroup), ['api'],
  'Added Models groups the compact rows by connection type');
const sharedProviderGroups = descendants(ids.get('provider-control-connections'), 'section')
  .filter(item => item.dataset.providerSharedGroupId);
assert.equal(sharedProviderGroups.length, 1,
  'multiple grants from one source provider collapse into one ordinary provider group');
const connectionRow = descendants(ids.get('provider-control-connections'), 'section')
  .find(item => item.dataset.providerConnectionId === connection.id);
const expandConnection = descendants(connectionRow, 'button')
  .find(item => item.classList.contains('provider-control-connection-toggle'));
assert.ok(expandConnection, 'compact Added Models row exposes an expand button');
assert.equal(expandConnection.attributes.get('aria-expanded'), 'false');
assert.ok(expandConnection.attributes.has('aria-controls'));
await expandConnection.dispatch('click');
assert.equal(expandConnection.attributes.get('aria-expanded'), 'true');
assert.ok(requests.some(item => item.url.endsWith(`/connections/${connection.id}/eligibility`)));
assert.equal(requests.some(item => item.url.endsWith(`/models/${model.id}/eligibility`)), false,
  'expansion uses one connection-level eligibility summary');
assert.match(allText(ids.get('provider-control-connections')), /1 account ready/);

const staleAccountDetails = descendants(connectionRow, 'details')
  .find(item => item.classList.contains('provider-control-connection-advanced'));
holdAccountRequest = true;
staleAccountDetails.open = true;
const staleAccountLoad = staleAccountDetails.dispatch('toggle');
await Promise.resolve();
document.dispatchEvent(new CustomEvent('open-clank:providers-updated'));
holdAccountRequest = false;
await staleAccountLoad;
await providerControl.load({ view: 'added-models' });
await new Promise(resolve => setTimeout(resolve, 0));
assert.equal(abortedAccountRequests, 1,
  'provider invalidation aborts an in-flight lazy account read');
assert.ok(requests.some(item => item.url.endsWith(`/connections/${connection.id}/accounts`)));
assert.match(allText(ids.get('provider-control-connections')), /Account rotation pool \(2\)/);
assert.match(allText(ids.get('provider-control-connections')), /Add account/);

const shareButton = descendants(ids.get('provider-control-connections'), 'button')
  .find(item => item.textContent === 'Share');
assert.ok(shareButton, 'each owned model exposes its direct named-user sharing control');
assert.equal(requests.some(item => item.url.endsWith('/share-recipients')), false);
assert.equal(requests.some(item => item.url.endsWith('/shares')), false);
await shareButton.dispatch('click');
assert.equal(shareButton.textContent, 'Shared with 1');
assert.ok(requests.some(item => item.url.endsWith('/share-recipients')));
assert.ok(requests.some(item => item.url.endsWith('/shares')));
const momToggle = descendants(ids.get('provider-control-connections'), 'input')
  .find(item => item.attributes.get('aria-label')?.endsWith('with mom'));
assert.ok(momToggle, 'safe recipient directory populates the per-model chooser');
momToggle.checked = true;
await momToggle.dispatch('change');
const shareRequest = requests.find(item => (
  item.options.method === 'PUT'
  && item.url.endsWith(`/models/${model.id}/shares/mom`)
));
assert.ok(shareRequest);
assert.ok(shareRequest.options.headers['Idempotency-Key'].startsWith('web-'));
assert.deepEqual(JSON.parse(shareRequest.options.body), { enabled: true });
assert.doesNotMatch(shareRequest.options.body, /account|credential|selector/i,
  'the browser cannot choose or expose credential accounts while sharing a model');

const refreshModels = descendants(ids.get('provider-control-connections'), 'button')
  .find(item => item.textContent === 'Refresh models');
await refreshModels.dispatch('click');
const refreshRequest = requests.find(item => (
  item.options.method === 'POST'
  && item.url.endsWith(`/connections/${connection.id}/models/refresh`)
));
assert.ok(refreshRequest, 'Added Models exposes per-connection model discovery');
assert.equal(refreshRequest.options.headers['If-Match'], '"7"');
assert.deepEqual(JSON.parse(refreshRequest.options.body), {});
assert.match(allText(ids.get('provider-control-connections')), /GPT Example/,
  'failed discovery preserves the last known model list');

assert.equal(allText(ids.get('provider-control-bindings')), '', 'collapsed routing does not build hidden controls');
routingDetails.open = true;
await routingDetails.dispatch('toggle');
const routingText = allText(ids.get('provider-control-bindings'));
assert.match(routingText, /Selected model/);
assert.doesNotMatch(routingText, /Primary|Fallback|ordered/i);
const saveSelected = descendants(ids.get('provider-control-bindings'), 'button')
  .find(item => item.textContent === 'Save selected model');
await saveSelected.dispatch('click');
const bindingRequest = requests.find(item => item.options.method === 'PUT' && item.url.endsWith('/bindings/chat'));
assert.deepEqual(JSON.parse(bindingRequest.options.body), {
  routes: [{ model_route_id: model.id, enabled: true }],
}, 'saving a purpose replaces any legacy fallback chain with one selected model');

const allUiText = [connectionsText, routingText].join(' ');
assert.doesNotMatch(allUiText, /MiMo|Xiaomi|OpenCode|Odysseus/i);

const addKey = descendants(ids.get('provider-control-connections'), 'button').find(item => item.textContent === 'Add API key');
await addKey.dispatch('click');
const passwordInput = descendants(ids.get('provider-control-connections'), 'input').find(item => item.type === 'password');
assert.ok(passwordInput, 'API keys use a write-only password input');
assert.equal(passwordInput.attributes.get('autocomplete'), 'off');

assert.equal(requests.some(item => item.url.endsWith('/pool/use-next')), false,
  'the browser never advances a connection-wide cursor without an exact model');

failReceivedShares = true;
await providerControl.load({ force: true, view: 'added-models' });
assert.match(allText(ids.get('provider-control-connections')), /OpenAI API/, 'failed required snapshot keeps the last good Added Models state rendered');
assert.match([
  ids.get('provider-control-status').textContent,
  ids.get('provider-control-added-status').textContent,
].join(' '), /received shares/i);
const snapshotsAfterFailure = requests.filter(item => item.url.endsWith('/management-snapshot')).length;
failReceivedShares = false;
await providerControl.load({ view: 'added-models' });
assert.equal(
  requests.filter(item => item.url.endsWith('/management-snapshot')).length,
  snapshotsAfterFailure + 1,
  'a failed required snapshot is not marked fresh and retries on the next visit',
);
malformedSnapshot = true;
await providerControl.load({ force: true, view: 'added-models' });
assert.match(allText(ids.get('provider-control-connections')), /OpenAI API/,
  'a malformed successful response cannot replace the last valid snapshot');
assert.match(ids.get('provider-control-added-status').textContent, /invalid response/i);

console.log('Unified provider UI contract checks passed');
