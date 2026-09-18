import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

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
    this.checked = false;
    this.disabled = false;
    this._classes = new Set();
  }

  set className(value) {
    this._classes = new Set(String(value || '').split(/\s+/).filter(Boolean));
  }

  get className() {
    return [...this._classes].join(' ');
  }

  get classList() {
    return {
      add: (...names) => names.forEach(name => this._classes.add(name)),
      remove: (...names) => names.forEach(name => this._classes.delete(name)),
      contains: name => this._classes.has(name),
      toggle: name => {
        if (this._classes.has(name)) {
          this._classes.delete(name);
          return false;
        }
        this._classes.add(name);
        return true;
      },
    };
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  append(...children) {
    children.forEach(child => {
      child.parentNode = this;
      this.children.push(child);
    });
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

  querySelectorAll(selector) {
    const selectors = selector.split(',').map(item => item.trim());
    const matches = node => selectors.some(item => {
      if (item === 'input') return node.tagName === 'input';
      if (item === 'button') return node.tagName === 'button';
      if (item === 'input[type="checkbox"]') return node.tagName === 'input' && node.type === 'checkbox';
      if (item === 'input[type="checkbox"]:checked') {
        return node.tagName === 'input' && node.type === 'checkbox' && node.checked;
      }
      if (item === '[data-model-share-control]') {
        return node.attributes.has('data-model-share-control');
      }
      if (item === '[data-share-model-id]') return Boolean(node.dataset.shareModelId);
      return false;
    });
    const found = [];
    const visit = node => {
      node.children.forEach(child => {
        if (matches(child)) found.push(child);
        visit(child);
      });
    };
    visit(this);
    return found;
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

function allText(node) {
  return [node.textContent, ...node.children.map(allText)].filter(Boolean).join(' ');
}

function descendants(node, tagName) {
  return [
    ...(node.tagName === tagName ? [node] : []),
    ...node.children.flatMap(child => descendants(child, tagName)),
  ];
}

const ids = new Map();
['model-share-received', 'model-share-status', 'model-share-list'].forEach(id => {
  ids.set(id, new FakeNode());
});
globalThis.window = globalThis;
globalThis.document = {
  createElement: tagName => new FakeNode(tagName),
  getElementById: id => ids.get(id) || null,
};

let receivedEnabled = false;
let ownerRecipients = ['allie'];
const requests = [];
globalThis.fetch = async (url, options = {}) => {
  requests.push({ url, options });
  if (url.endsWith('/subscription')) {
    receivedEnabled = Boolean(JSON.parse(options.body).enabled);
  } else if (url === '/api/model-shares' && options.method === 'PUT') {
    ownerRecipients = JSON.parse(options.body).recipients;
  }
  return {
    ok: true,
    async json() {
      if (options.method === 'PUT') return { ok: true };
      return {
        share_users: [{ username: 'allie' }, { username: 'mom' }],
        owned: [{
          share_id: 'owned-share',
          endpoint_id: 'endpoint-1',
          model_id: 'deepseek/chat',
          source_kind: 'endpoint',
          recipients: ownerRecipients.map(username => ({ username, enabled: false })),
        }],
        received: [
          {
            share_id: 'received-share',
            endpoint_id: 'opaque-shared-endpoint',
            model_id: 'xiaomi/mimo-v2.5-pro/high',
            model_name: 'high',
            provider: 'MiMo',
            shared_by: 'e',
            enabled: receivedEnabled,
            tools: true,
            source_url: 'https://must-not-render.invalid/v1',
            api_key: 'must-not-render',
            headers: { Authorization: 'must-not-render' },
          },
          {
            share_id: 'luna-low',
            model_id: 'openai/gpt-5.6-luna/low',
            model_name: 'low',
            provider: 'OpenAI',
            shared_by: 'e',
            enabled: false,
          },
          {
            share_id: 'luna-medium',
            model_id: 'openai/gpt-5.6-luna/medium',
            model_name: 'medium',
            provider: 'OpenAI',
            shared_by: 'e',
            enabled: false,
          },
        ],
      };
    },
  };
};

const modelSharing = await import('../static/js/modelSharing.js');
let catalogChanges = 0;
modelSharing.init({
  onCatalogChanged: async () => { catalogChanges += 1; },
});
await modelSharing.load();

const receivedList = ids.get('model-share-list');
assert.equal(receivedList.children.length, 2);
assert.equal(descendants(receivedList, 'details').length, 2);
const receivedText = allText(receivedList);
assert.match(receivedText, /MiMo V2\.5 Pro · High reasoning/);
assert.match(receivedText, /GPT-5\.6 Luna · Low reasoning/);
assert.match(receivedText, /GPT-5\.6 Luna · Medium reasoning/);
assert.match(receivedText, /Shared by e/);
assert.match(receivedText, /Add to my models/);
assert.doesNotMatch(receivedText, /must-not-render/);
assert.doesNotMatch(receivedText, /opaque-shared-endpoint/);

const legacyWithoutProvider = modelSharing.normalizeReceived({
  share_id: 'legacy-bare',
  model_name: 'Same bare model',
  shared_by: 'e',
});
assert.equal(legacyWithoutProvider.provider, 'Unknown provider');
assert.equal(legacyWithoutProvider.providerFamilyId, '');
assert.equal(legacyWithoutProvider.modelId, 'Same bare model');

const recipientToggle = descendants(receivedList, 'input')[0];
recipientToggle.checked = true;
await recipientToggle.listeners.get('change')({ stopPropagation() {} });
assert.equal(receivedEnabled, true);
assert.deepEqual(
  JSON.parse(requests.find(item => item.url.endsWith('/subscription')).options.body),
  { enabled: true },
);
assert.equal(catalogChanges, 1);

const panel = new FakeNode();
const modelRow = new FakeNode();
modelRow.dataset.shareModelId = 'deepseek/chat';
modelRow.dataset.shareDisabled = 'false';
panel.append(modelRow);
modelSharing.mountOwnerControls(panel, 'endpoint-1');

const ownerText = allText(panel);
assert.match(ownerText, /Shared with 1/);
assert.doesNotMatch(ownerText, /Share with other Odysseus users/);
assert.match(ownerText, /allie/);
assert.match(ownerText, /mom/);
assert.equal(descendants(panel, 'input').length, 2);
const shareButton = descendants(panel, 'button')[0];
shareButton.listeners.get('click')({ preventDefault() {}, stopPropagation() {} });
let chooser = modelRow.children.find(child => child.classList.contains('model-share-users'));
assert.equal(chooser.classList.contains('hidden'), false);
const momToggle = descendants(panel, 'input').find(input => input._shareUsername === 'mom');
momToggle.checked = true;
await momToggle.listeners.get('change')({ stopPropagation() {} });
const ownerUpdate = requests.filter(item => (
  item.url === '/api/model-shares' && item.options.method === 'PUT'
)).at(-1);
assert.deepEqual(JSON.parse(ownerUpdate.options.body), {
  endpoint_id: 'endpoint-1',
  model_id: 'deepseek/chat',
  shared: true,
  recipients: ['allie', 'mom'],
});
chooser = modelRow.children.find(child => child.classList.contains('model-share-users'));
assert.equal(chooser.classList.contains('hidden'), false);
assert.equal(descendants(panel, 'button')[0].attributes.get('aria-expanded'), 'true');
const allieToggle = descendants(panel, 'input').find(input => input._shareUsername === 'allie');
allieToggle.checked = false;
await allieToggle.listeners.get('change')({ stopPropagation() {} });
assert.deepEqual(
  JSON.parse(requests.filter(item => (
    item.url === '/api/model-shares' && item.options.method === 'PUT'
  )).at(-1).options.body),
  {
    endpoint_id: 'endpoint-1',
    model_id: 'deepseek/chat',
    shared: true,
    recipients: ['mom'],
  },
);
chooser = modelRow.children.find(child => child.classList.contains('model-share-users'));
assert.equal(chooser.classList.contains('hidden'), false);

const autoPanel = new FakeNode();
const autoRow = new FakeNode();
autoRow.dataset.shareModelId = 'xiaomi/mimo-auto';
autoPanel.append(autoRow);
modelSharing.mountOwnerControls(autoPanel, 'mimo:auto');
assert.equal(autoPanel.querySelectorAll('[data-model-share-control]').length, 0);

const disabledPanel = new FakeNode();
const disabledRow = new FakeNode();
disabledRow.dataset.shareModelId = 'deepseek/offline';
disabledRow.dataset.shareDisabled = 'true';
disabledPanel.append(disabledRow);
modelSharing.mountOwnerControls(disabledPanel, 'endpoint-offline');
assert.equal(disabledPanel.querySelectorAll('[data-model-share-control]').length, 0);

const pickerSource = readFileSync(
  new URL('../static/js/modelPicker.js', import.meta.url),
  'utf8',
);
assert.match(pickerSource, /item\.shared/);
assert.match(pickerSource, /Shared by \$\{sharedBy\}/);

console.log('named-user model sharing UI contract checks passed');
