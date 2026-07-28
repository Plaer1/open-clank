import assert from 'node:assert/strict';
import { init, load } from '../static/js/mimoProviders.js';

class FakeNode {
  constructor(tagName = 'div') {
    this.tagName = tagName;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.style = {};
    this.textContent = '';
    this.value = '';
    this.dataset = {};
    this.parentNode = null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name.startsWith('data-')) {
      const key = name.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
      this.dataset[key] = String(value);
    }
  }

  get classList() {
    return {
      contains: name => String(this.className || '').split(/\s+/).includes(name),
      toggle: name => {
        const classes = new Set(String(this.className || '').split(/\s+/).filter(Boolean));
        if (classes.has(name)) classes.delete(name);
        else classes.add(name);
        this.className = [...classes].join(' ');
        return classes.has(name);
      },
    };
  }

  addEventListener(name, callback) {
    this.listeners.set(name, callback);
  }

  append(...children) {
    children.forEach(child => { child.parentNode = this; });
    this.children.push(...children);
  }

  prepend(...children) {
    this.children.unshift(...children);
  }

  replaceChildren(...children) {
    this.children = [...children];
  }

  remove() {
    if (!this.parentNode) return;
    this.parentNode.children = this.parentNode.children.filter(child => child !== this);
  }

  querySelectorAll(selector) {
    const matches = node => (
      (selector === '[data-model-share-control]' && node.attributes.has('data-model-share-control'))
      || (selector === '[data-share-model-id]' && Boolean(node.dataset.shareModelId))
    );
    const found = [];
    const visit = node => node.children.forEach(child => {
      if (matches(child)) found.push(child);
      visit(child);
    });
    visit(this);
    return found;
  }
}

const ids = new Map();
[
  'mimo-provider-directory',
  'mimo-provider-search',
  'mimo-provider-refresh',
  'mimo-provider-status',
  'mimo-provider-flow',
  'mimo-provider-list',
  'mimo-connected-provider-status',
  'mimo-connected-provider-flow',
  'mimo-connected-provider-list',
].forEach(id => ids.set(id, new FakeNode()));

globalThis.document = {
  createElement: tagName => new FakeNode(tagName),
  getElementById: id => ids.get(id) || null,
  querySelector: () => null,
};
globalThis.window = globalThis;
globalThis.fetch = async url => ({
  ok: true,
  async json() {
    if (url === '/api/model-shares') {
      return {
        share_users: [{ username: 'allie' }],
        owned: [],
        received: [],
      };
    }
    return {
      providers: [
        {
          id: 'mimo',
          name: 'mimo',
          family: 'Mimo',
          connected: true,
          methods: [{ index: 0, type: 'api', label: 'API key' }],
        },
        {
          id: 'ody-direct-endpoint',
          name: 'Internal projection',
          connected: true,
          methods: [{ index: 0, type: 'api', label: 'API key' }],
        },
        {
          id: 'xiaomi',
          name: 'MiMo',
          family: 'MiMo',
          connected: false,
          included_free_models: 1,
          models: ['xiaomi/mimo-auto'],
          methods: [{ index: 0, type: 'oauth', label: 'Browser login (paste code)' }],
        },
        {
          id: 'deepseek',
          name: 'DeepSeek',
          family: 'DeepSeek',
          connected: true,
          free_tier: true,
          active: true,
          chat_models: 2,
          models: ['deepseek/deepseek-chat'],
          methods: [
            { index: 0, type: 'api', label: 'API key' },
            { index: 1, type: 'api', label: 'API key' },
          ],
        },
      ],
    };
  },
});

function allText(node) {
  return [node.textContent, ...node.children.map(allText)].filter(Boolean).join(' ');
}

function descendants(node, tagName) {
  return [
    ...(node.tagName === tagName ? [node] : []),
    ...node.children.flatMap(child => descendants(child, tagName)),
  ];
}

init();
await load();

const available = ids.get('mimo-provider-list');
const connected = ids.get('mimo-connected-provider-list');
assert.equal(available.children.length, 1);
assert.equal(connected.children.length, 2);

const xiaomiText = allText(available.children[0]);
assert.match(xiaomiText, /\bXiaomi\b/);
assert.match(xiaomiText, /\bMiMo models\b/);
assert.match(xiaomiText, /Sign in with Xiaomi/);
assert.doesNotMatch(xiaomiText, /\bserves\b/i);

const deepSeekCard = connected.children.find(card => /\bDeepSeek\b/.test(allText(card)));
const xiaomiCard = connected.children.find(card => /\bXiaomi\b/.test(allText(card)));
const connectedText = allText(deepSeekCard);
assert.match(connectedText, /\bDeepSeek\b/);
assert.match(connectedText, /\bdeepseek-chat\b/);
assert.match(connectedText, /\bShare\b/);
assert.doesNotMatch(allText(xiaomiCard), /\bShare\b/);
assert.doesNotMatch(connectedText, /Internal projection/);
const connectedButtons = descendants(deepSeekCard, 'button').map(button => button.textContent);
assert.deepEqual(connectedButtons, ['Disconnect', 'Show models (1)', 'Share']);
const modelList = descendants(deepSeekCard, 'div').find(node => (
  String(node.className || '').split(/\s+/).includes('mcp-tools-list')
));
const modelToggle = descendants(deepSeekCard, 'button').find(button => button.textContent.startsWith('Show models'));
assert.equal(modelList.classList.contains('hidden'), true);
modelToggle.listeners.get('click')({ preventDefault() {}, stopPropagation() {} });
assert.equal(modelList.classList.contains('hidden'), false);
assert.equal(modelToggle.textContent, 'Hide models (1)');
assert.equal(modelToggle.attributes.get('aria-expanded'), 'true');

assert.equal(ids.get('mimo-provider-status').textContent, '1 provider available to connect');
assert.equal(ids.get('mimo-connected-provider-status').textContent, '1 API provider connected · 1 model included');

console.log('MiMo provider UI contract checks passed');
