import assert from 'node:assert/strict';
import test from 'node:test';

import { createExplorerTree } from '../../static/js/editor/explorerTree.js';

class FakeClassList {
  constructor(element) { this.element = element; }
  add(...names) {
    const current = new Set(String(this.element.className || '').split(/\s+/).filter(Boolean));
    for (const name of names) current.add(name);
    this.element.className = [...current].join(' ');
  }
}

class FakeElement {
  constructor(tagName, ownerDocument, nodeType = 1) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.nodeType = nodeType;
    this.parentElement = null;
    this.children = [];
    this.attributes = new Map();
    this.listeners = new Map();
    this.className = '';
    this.classList = new FakeClassList(this);
    this.textContent = '';
    this.innerHTML = '';
    this.tabIndex = -1;
    this.disabled = false;
    this.type = '';
    this.title = '';
  }
  append(...values) {
    for (const value of values) {
      if (value == null) continue;
      if (value.nodeType === 11) {
        this.append(...value.children);
        value.children = [];
        continue;
      }
      if (typeof value !== 'object') {
        const text = new FakeElement('#text', this.ownerDocument, 3);
        text.textContent = String(value);
        this.append(text);
        continue;
      }
      value.parentElement = this;
      this.children.push(value);
    }
  }
  replaceChildren(...values) {
    for (const child of this.children) child.parentElement = null;
    this.children = [];
    this.textContent = '';
    this.innerHTML = '';
    this.append(...values);
  }
  setAttribute(name, value) { this.attributes.set(String(name), String(value)); }
  getAttribute(name) { return this.attributes.has(String(name)) ? this.attributes.get(String(name)) : null; }
  hasAttribute(name) { return this.attributes.has(String(name)); }
  removeAttribute(name) { this.attributes.delete(String(name)); }
  addEventListener(type, callback) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(callback);
  }
  removeEventListener(type, callback) { this.listeners.get(type)?.delete(callback); }
  dispatch(type, init = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      ...init,
    };
    for (const callback of [...(this.listeners.get(type) || [])]) callback(event);
    return event;
  }
  focus() {
    this.ownerDocument.activeElement = this;
    this.dispatch('focus');
  }
  contains(candidate) {
    if (candidate === this) return true;
    return this.children.some(child => child.contains?.(candidate));
  }
}

class FakeDocument {
  constructor() { this.activeElement = null; }
  createElement(tagName) { return new FakeElement(tagName, this); }
  createDocumentFragment() { return new FakeElement('#fragment', this, 11); }
}

function fixture(options = {}) {
  const document = new FakeDocument();
  const container = document.createElement('div');
  const tree = createExplorerTree({
    container,
    getId: item => item.ref,
    getLabel: item => item.name,
    isBranch: item => item.kind === 'directory',
    loadPage: async () => ({ items: [] }),
    ...options,
  });
  return { document, container, tree };
}

function walk(root) {
  const result = [];
  const visit = node => {
    result.push(node);
    for (const child of node.children) visit(child);
  };
  visit(root);
  return result;
}

function byAttribute(root, name, value = undefined) {
  return walk(root).find(node => node.getAttribute?.(name) != null
    && (value === undefined || node.getAttribute(name) === String(value)));
}

function byClass(root, className) {
  return walk(root).find(node => String(node.className).split(/\s+/).includes(className));
}

function item(root, id) {
  return byAttribute(root, 'data-explorer-id', id);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

test('lazy pages preserve stable nodes and expose complete tree ARIA', async () => {
  const calls = [];
  const { container, tree } = fixture({
    roots: [{ ref: 'root', name: 'Workspace', kind: 'directory' }],
    loadPage: async (_entry, request) => {
      calls.push(request);
      if (request.cursor === 'page-2') {
        return { items: [{ ref: 'readme', name: 'README.md', kind: 'file' }] };
      }
      return {
        items: [
          { ref: 'src', name: 'src', kind: 'directory' },
          { ref: 'index', name: 'index.js', kind: 'file' },
        ],
        nextCursor: 'page-2',
      };
    },
  });

  assert.equal(container.getAttribute('role'), 'tree');
  assert.equal(item(container, 'root').getAttribute('aria-level'), '1');
  assert.equal(item(container, 'root').getAttribute('aria-expanded'), 'false');
  assert.equal(item(container, 'root').tabIndex, 0);

  assert.equal(await tree.expand('root'), true);
  assert.equal(item(container, 'root').getAttribute('aria-expanded'), 'true');
  assert.equal(item(container, 'src').getAttribute('aria-level'), '2');
  assert.equal(item(container, 'index').getAttribute('aria-expanded'), null);
  assert.equal(byClass(container, 'oc-explorer-tree__more').textContent, 'Load more');
  assert.equal(calls[0].signal instanceof AbortSignal, true);

  assert.equal(await tree.loadMore('root'), true);
  assert.deepEqual(tree.getNode('root').children, ['src', 'index', 'readme']);
  assert.equal(tree.getNode('root').nextCursor, null);

  await tree.expand('src');
  assert.equal(tree.getNode('src').expanded, true);
  tree.reconcile('root', [
    { ref: 'src', name: 'source', kind: 'directory' },
    { ref: 'index', name: 'index.js', kind: 'file' },
  ]);
  assert.equal(tree.getNode('src').data.name, 'source');
  assert.equal(tree.getNode('src').expanded, true);
  assert.equal(tree.getNode('readme'), null);
  tree.destroy();
});

test('per-node requests abort and stale pages cannot replace refreshed data', async () => {
  const requests = [];
  const { tree } = fixture({
    roots: [{ ref: 'root', name: 'Workspace', kind: 'directory' }],
    loadPage: (_entry, request) => {
      const pending = deferred();
      requests.push({ request, pending });
      return pending.promise;
    },
  });

  const first = tree.expand('root');
  assert.equal(requests.length, 1);
  const refreshed = tree.refresh('root', { recursive: false });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].request.signal.aborted, true);

  requests[0].pending.resolve({ items: [{ ref: 'stale', name: 'stale', kind: 'file' }] });
  requests[1].pending.resolve({ items: [{ ref: 'fresh', name: 'fresh', kind: 'file' }] });
  assert.equal(await first, false);
  assert.equal(await refreshed, true);
  assert.equal(tree.getNode('stale'), null);
  assert.deepEqual(tree.getNode('root').children, ['fresh']);

  const third = tree.refresh('root', { recursive: false });
  assert.equal(requests.length, 3);
  tree.collapse('root');
  assert.equal(requests[2].request.signal.aborted, true);
  requests[2].pending.resolve({ items: [{ ref: 'late', name: 'late', kind: 'file' }] });
  assert.equal(await third, false);
  assert.equal(tree.getNode('late'), null);
  tree.destroy();
});

test('top-level continuation stays lazy and refresh preserves revealed page breadth', async () => {
  const calls = [];
  let version = 1;
  const { container, tree } = fixture({
    roots: [
      { ref: 'folder', name: 'Folder', kind: 'directory' },
      { ref: 'a', name: 'a.txt', kind: 'file' },
    ],
    rootNextCursor: 'second',
    loadRootPage: async request => {
      calls.push({ cursor: request.cursor, append: request.append });
      if (request.cursor === 'second') {
        return { items: [{ ref: 'b', name: `b-${version}.txt`, kind: 'file' }] };
      }
      return {
        items: [
          { ref: 'folder', name: `Folder ${version}`, kind: 'directory' },
          { ref: 'a', name: `a-${version}.txt`, kind: 'file' },
        ],
        nextCursor: 'second',
      };
    },
  });

  assert.equal(calls.length, 0, 'mount must not fetch undisclosed top-level pages');
  assert.equal(byAttribute(container, 'data-explorer-root-more')?.textContent, 'Load more');
  assert.equal(await tree.loadMore(), true);
  assert.deepEqual(tree.snapshot().roots, ['folder', 'a', 'b']);
  assert.deepEqual(calls, [{ cursor: 'second', append: true }]);
  await tree.expand('folder');
  tree.setActive('b');
  tree.focus('b');

  version = 2;
  assert.equal(await tree.refreshRoots({ recursive: false }), true);
  assert.deepEqual(calls.slice(1), [
    { cursor: null, append: false },
    { cursor: 'second', append: true },
  ]);
  assert.deepEqual(tree.snapshot().roots, ['folder', 'a', 'b']);
  assert.equal(tree.getNode('folder').data.name, 'Folder 2');
  assert.equal(tree.getNode('folder').expanded, true, 'refresh retains expansion for stable identities');
  assert.equal(tree.getNode('b').data.name, 'b-2.txt');
  assert.equal(tree.snapshot().activeId, 'b');
  assert.equal(tree.snapshot().focusedId, 'b');
  assert.equal(tree.snapshot().root.pagesLoaded, 2);
  tree.destroy();
});

test('load failures render retry and retry the exact failed page', async () => {
  let attempts = 0;
  const errors = [];
  const { container, tree } = fixture({
    roots: [{ ref: 'root', name: 'Workspace', kind: 'directory' }],
    loadPage: async (_entry, request) => {
      attempts += 1;
      if (attempts === 1) throw new Error(`failed at ${request.cursor ?? 'start'}`);
      return { items: [{ ref: 'ok', name: 'ok.txt', kind: 'file' }] };
    },
    onError: (error, context) => errors.push([error.message, context]),
  });

  assert.equal(await tree.expand('root'), false);
  assert.equal(tree.getNode('root').error, 'failed at start');
  assert.equal(byClass(container, 'oc-explorer-tree__retry').textContent, 'Retry');
  assert.deepEqual(errors[0][1], { phase: 'load', id: 'root', cursor: null, append: false });
  assert.equal(await tree.retry('root'), true);
  assert.deepEqual(tree.getNode('root').children, ['ok']);
  assert.equal(tree.getNode('root').error, '');
  tree.destroy();
});

test('stale continuation cursor restarts the node instead of retrying stale authority forever', async () => {
  let firstPage = 0;
  const calls = [];
  const { tree } = fixture({
    roots: [{ ref: 'root', name: 'Workspace', kind: 'directory' }],
    loadPage: async (_entry, request) => {
      calls.push(request.cursor);
      if (request.cursor === 'stale-page') {
        const error = new Error('directory changed');
        error.code = 'stale_cursor';
        throw error;
      }
      firstPage += 1;
      return {
        items: [{ ref: `fresh-${firstPage}`, name: `fresh-${firstPage}.txt`, kind: 'file' }],
        nextCursor: firstPage === 1 ? 'stale-page' : null,
      };
    },
  });

  await tree.expand('root');
  assert.equal(await tree.loadMore('root'), true);
  assert.deepEqual(calls, [null, 'stale-page', null]);
  assert.deepEqual(tree.getNode('root').children, ['fresh-2']);
  assert.equal(tree.getNode('root').nextCursor, null);
  tree.destroy();
});

test('roving focus supports arrows, Home, End, parents, and typeahead', async () => {
  const activated = [];
  const { container, tree } = fixture({
    roots: [
      { ref: 'root', name: 'Workspace', kind: 'directory' },
      { ref: 'omega', name: 'Omega.txt', kind: 'file' },
    ],
    loadPage: async () => ({ items: [
      { ref: 'alpha', name: 'Alpha', kind: 'directory' },
      { ref: 'beta', name: 'Beta.txt', kind: 'file' },
      { ref: 'charlie', name: 'Charlie.txt', kind: 'file' },
    ] }),
    onActivate: entry => activated.push(entry.ref),
  });
  await tree.expand('root');
  tree.focus('root');

  item(container, 'root').dispatch('keydown', { key: 'ArrowRight' });
  assert.equal(tree.snapshot().focusedId, 'alpha');
  item(container, 'alpha').dispatch('keydown', { key: 'ArrowDown' });
  assert.equal(tree.snapshot().focusedId, 'beta');
  item(container, 'beta').dispatch('keydown', { key: 'End' });
  assert.equal(tree.snapshot().focusedId, 'omega');
  item(container, 'omega').dispatch('keydown', { key: 'Home' });
  assert.equal(tree.snapshot().focusedId, 'root');
  item(container, 'root').dispatch('keydown', { key: 'b' });
  assert.equal(tree.snapshot().focusedId, 'beta');
  item(container, 'beta').dispatch('keydown', { key: 'Enter' });
  assert.deepEqual(activated, ['beta']);
  tree.focus('alpha');
  item(container, 'alpha').dispatch('keydown', { key: 'ArrowLeft' });
  assert.equal(tree.snapshot().focusedId, 'root');

  tree.setActive('beta');
  assert.equal(item(container, 'beta').getAttribute('aria-current'), 'page');
  tree.setActive('omega');
  assert.equal(item(container, 'beta').getAttribute('aria-current'), null);
  assert.equal(item(container, 'omega').getAttribute('aria-current'), 'page');
  tree.destroy();
});

test('icon, activation, and row actions are injected without product state', () => {
  const activations = [];
  const actions = [];
  const document = new FakeDocument();
  const container = document.createElement('div');
  const tree = createExplorerTree({
    container,
    roots: [{ ref: 'file', name: 'file.rs', kind: 'file' }],
    getId: entry => entry.ref,
    getLabel: entry => entry.name,
    isBranch: entry => entry.kind === 'directory',
    loadPage: async () => ({ items: [] }),
    renderIcon: entry => {
      const icon = document.createElement('i');
      icon.textContent = entry.kind;
      return icon;
    },
    onActivate: entry => activations.push(entry.ref),
    actions: entry => [{ id: 'inspect', label: `Inspect ${entry.name}` }],
    onAction: (action, entry) => actions.push([action, entry.ref]),
  });

  assert.equal(byClass(container, 'oc-explorer-tree__icon').children[0].textContent, 'file');
  byAttribute(container, 'data-explorer-row').dispatch('click');
  byAttribute(container, 'data-explorer-action', 'inspect').dispatch('click');
  assert.deepEqual(activations, ['file']);
  assert.deepEqual(actions, [['inspect', 'file']]);
  tree.destroy();
});

test('destroy is idempotent, aborts pending work, and ignores later content', async () => {
  const pending = deferred();
  let signal;
  const { container, tree } = fixture({
    roots: [{ ref: 'root', name: 'Workspace', kind: 'directory' }],
    loadPage: (_entry, request) => {
      signal = request.signal;
      return pending.promise;
    },
  });
  const loading = tree.expand('root');
  tree.destroy();
  tree.destroy();
  assert.equal(signal.aborted, true);
  assert.equal(container.children.length, 0);
  pending.resolve({ items: [{ ref: 'late', name: 'late', kind: 'file' }] });
  assert.equal(await loading, false);
  assert.equal(tree.snapshot().destroyed, true);
  assert.equal(tree.setRoots([]), false);
});

test('entries without server/injected stable identity are rejected', () => {
  const document = new FakeDocument();
  const container = document.createElement('div');
  assert.throws(() => createExplorerTree({
    container,
    roots: [{ name: 'path-looking-but-not-identity', kind: 'file' }],
    loadPage: async () => ({ items: [] }),
  }), /stable identity/);
});
