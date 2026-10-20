import assert from 'node:assert/strict';
import test from 'node:test';
import { createResizablePane } from '../../static/js/editor/resizablePane.js';

class FakeStorage {
  constructor(entries = []) { this.values = new Map(entries); }
  getItem(key) { return this.values.has(key) ? this.values.get(key) : null; }
  setItem(key, value) { this.values.set(String(key), String(value)); }
}

class FakeElement {
  constructor(width = 0) {
    this.width = width;
    this.hidden = false;
    this.dataset = {};
    this.attributes = new Map();
    this.listeners = new Map();
    this.captured = [];
    this.released = [];
    this.styles = new Map();
    this.style = { setProperty: (name, value) => this.styles.set(name, value) };
  }

  getBoundingClientRect() { return { width: this.hidden ? 0 : this.width }; }
  hasAttribute(name) { return this.attributes.has(name); }
  getAttribute(name) { return this.attributes.get(name) ?? null; }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === 'hidden') this.hidden = true;
  }
  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === 'hidden') this.hidden = false;
  }
  toggleAttribute(name, force) {
    const enabled = force == null ? !this.attributes.has(name) : Boolean(force);
    if (enabled) this.setAttribute(name, '');
    else this.removeAttribute(name);
    return enabled;
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) { this.listeners.get(type)?.delete(listener); }
  listenerCount(type) { return this.listeners.get(type)?.size ?? 0; }
  dispatch(type, fields = {}) {
    const event = {
      type,
      defaultPrevented: false,
      preventDefault() { this.defaultPrevented = true; },
      ...fields,
    };
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener(event);
    return event;
  }
  setPointerCapture(pointerId) { this.captured.push(pointerId); }
  releasePointerCapture(pointerId) { this.released.push(pointerId); }
}

function environment(entries = []) {
  const previousStorage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  const previousObserver = Object.getOwnPropertyDescriptor(globalThis, 'ResizeObserver');
  const storage = new FakeStorage(entries);
  const observers = [];
  class FakeResizeObserver {
    constructor(callback) {
      this.callback = callback;
      this.disconnectCount = 0;
      observers.push(this);
    }
    observe(target) { this.target = target; }
    disconnect() { this.disconnectCount += 1; }
    fire() { this.callback([{ target: this.target }], this); }
  }
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage });
  Object.defineProperty(globalThis, 'ResizeObserver', { configurable: true, value: FakeResizeObserver });
  return {
    storage,
    observers,
    restore() {
      if (previousStorage) Object.defineProperty(globalThis, 'localStorage', previousStorage);
      else delete globalThis.localStorage;
      if (previousObserver) Object.defineProperty(globalThis, 'ResizeObserver', previousObserver);
      else delete globalThis.ResizeObserver;
    },
  };
}

function elements(containerWidth = 1000) {
  return {
    container: new FakeElement(containerWidth),
    sidebar: new FakeElement(240),
    main: new FakeElement(containerWidth - 246),
    separator: new FakeElement(6),
  };
}

test('keeps live ARIA bounds while reclamping and restores the desktop preference after compact mode', () => {
  const env = environment([['files:e', '400']]);
  try {
    const parts = elements();
    const changes = [];
    const pane = createResizablePane({
      ...parts,
      cssVar: '--test-width',
      storageKey: 'files',
      getOwner: () => 'e',
      minWidth: 180,
      maxWidth: 420,
      mainMin: 320,
      compactBreakpoint: 680,
      onChange: value => changes.push(value),
    });

    assert.equal(parts.container.styles.get('--test-width'), '400px');
    assert.equal(parts.separator.getAttribute('role'), 'separator');
    assert.equal(parts.separator.getAttribute('aria-orientation'), 'vertical');
    assert.equal(parts.separator.getAttribute('tabindex'), '0');
    assert.equal(parts.separator.getAttribute('aria-valuemin'), '180');
    assert.equal(parts.separator.getAttribute('aria-valuemax'), '420');
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '400');
    assert.equal(parts.separator.hidden, false);

    parts.container.width = 700;
    env.observers[0].fire();
    assert.equal(parts.separator.getAttribute('aria-valuemax'), '374');
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '374');
    assert.equal(parts.container.styles.get('--test-width'), '374px');
    assert.equal(env.storage.getItem('files:e'), '400');

    parts.container.width = 0;
    env.observers[0].fire();
    assert.equal(parts.container.styles.get('--test-width'), '374px');
    assert.equal(parts.separator.hidden, false);

    parts.container.width = 600;
    env.observers[0].fire();
    assert.equal(parts.separator.hidden, true);
    assert.equal(parts.separator.getAttribute('aria-disabled'), 'true');
    assert.equal(parts.separator.getAttribute('aria-valuemax'), '280');
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '280');
    assert.equal(env.storage.getItem('files:e'), '400');

    parts.container.width = 1000;
    env.observers[0].fire();
    assert.equal(parts.separator.hidden, false);
    assert.equal(parts.separator.getAttribute('aria-disabled'), 'false');
    assert.equal(parts.separator.getAttribute('aria-valuemax'), '420');
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '400');
    assert.deepEqual(changes, [400, 374, 280, 400]);
    pane.destroy();
  } finally {
    env.restore();
  }
});

test('isolates persisted widths by app and owner and refreshes owner state without adding listeners', () => {
  const env = environment([
    ['files:e', '215'],
    ['code:e', '330'],
    ['files:bob', '275'],
  ]);
  try {
    let filesOwner = 'e';
    const files = elements();
    const code = elements();
    const filesPane = createResizablePane({
      ...files,
      cssVar: '--files-width',
      storageKey: 'files',
      defaultWidth: 220,
      getOwner: () => filesOwner,
    });
    const codePane = createResizablePane({
      ...code,
      cssVar: '--code-width',
      storageKey: 'code',
      getOwner: () => 'e',
    });

    assert.equal(files.container.styles.get('--files-width'), '215px');
    assert.equal(code.container.styles.get('--code-width'), '330px');
    filesPane.apply(260);
    assert.equal(env.storage.getItem('files:e'), '260');
    assert.equal(env.storage.getItem('code:e'), '330');

    filesOwner = 'bob';
    filesPane.refresh();
    filesPane.refresh();
    assert.equal(files.container.styles.get('--files-width'), '275px');
    assert.equal(files.separator.listenerCount('pointerdown'), 1);
    assert.equal(files.separator.listenerCount('keydown'), 1);

    filesOwner = '';
    filesPane.refresh();
    filesPane.apply(300);
    assert.equal(files.container.styles.get('--files-width'), '300px');
    assert.equal(env.storage.getItem('files:'), null);
    assert.equal(env.storage.getItem('files:bob'), '275');
    filesPane.destroy();
    codePane.destroy();
  } finally {
    env.restore();
  }
});

test('supports pointer, keyboard, Home/End, reset, and complete cancel/lost-capture cleanup', () => {
  const env = environment();
  try {
    const parts = elements(900);
    const pane = createResizablePane({
      ...parts,
      cssVar: '--test-width',
      storageKey: 'files',
      getOwner: () => 'e',
    });

    assert.equal(parts.separator.dispatch('keydown', { key: 'End' }).defaultPrevented, true);
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '420');
    parts.separator.dispatch('keydown', { key: 'Home' });
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '180');
    parts.separator.dispatch('keydown', { key: 'ArrowRight' });
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '192');
    parts.separator.dispatch('keydown', { key: 'ArrowLeft' });
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '180');
    assert.equal(parts.separator.dispatch('dblclick').defaultPrevented, true);
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '240');

    const down = parts.separator.dispatch('pointerdown', { pointerId: 7, clientX: 100, button: 0 });
    assert.equal(down.defaultPrevented, true);
    assert.deepEqual(parts.separator.captured, [7]);
    parts.separator.dispatch('pointermove', { pointerId: 99, clientX: 160 });
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '240');
    parts.separator.dispatch('pointermove', { pointerId: 7, clientX: 160 });
    assert.equal(parts.separator.getAttribute('aria-valuenow'), '300');
    parts.separator.dispatch('pointerup', { pointerId: 7 });
    assert.deepEqual(parts.separator.released, [7]);
    assert.equal(parts.separator.listenerCount('pointermove'), 0);
    assert.equal('pointerId' in parts.separator.dataset, false);

    parts.separator.dispatch('pointerdown', { pointerId: 8, clientX: 100, button: 0 });
    parts.separator.dispatch('pointercancel', { pointerId: 8 });
    assert.deepEqual(parts.separator.released, [7, 8]);
    assert.equal(parts.separator.listenerCount('pointercancel'), 0);

    parts.separator.dispatch('pointerdown', { pointerId: 9, clientX: 100, button: 0 });
    parts.separator.dispatch('lostpointercapture', { pointerId: 9 });
    assert.deepEqual(parts.separator.released, [7, 8]);
    assert.equal(parts.separator.listenerCount('lostpointercapture'), 0);

    parts.container.width = 600;
    env.observers[0].fire();
    const compactWidth = parts.separator.getAttribute('aria-valuenow');
    assert.equal(parts.separator.dispatch('keydown', { key: 'End' }).defaultPrevented, false);
    assert.equal(parts.separator.dispatch('pointerdown', { pointerId: 10, clientX: 0, button: 0 }).defaultPrevented, false);
    assert.equal(parts.separator.getAttribute('aria-valuenow'), compactWidth);
    pane.destroy();
  } finally {
    env.restore();
  }
});

test('refresh and destroy are idempotent across active drags and repeated mounts', () => {
  const env = environment([['files:e', '250']]);
  try {
    const parts = elements();
    const pane = createResizablePane({ ...parts, storageKey: 'files', getOwner: () => 'e' });
    pane.refresh();
    pane.refresh();
    assert.equal(parts.separator.listenerCount('pointerdown'), 1);
    parts.separator.dispatch('pointerdown', { pointerId: 3, clientX: 20, button: 0 });
    assert.equal(parts.separator.listenerCount('pointermove'), 1);

    pane.destroy();
    pane.destroy();
    assert.equal(env.observers[0].disconnectCount, 1);
    assert.deepEqual(parts.separator.released, [3]);
    for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel', 'lostpointercapture', 'keydown', 'dblclick']) {
      assert.equal(parts.separator.listenerCount(type), 0, `${type} listener survives destroy`);
    }
    const width = parts.container.styles.get('--oc-explorer-sidebar-width');
    pane.apply(400);
    pane.refresh();
    env.observers[0].fire();
    assert.equal(parts.container.styles.get('--oc-explorer-sidebar-width'), width);

    const replacement = createResizablePane({ ...parts, storageKey: 'files', getOwner: () => 'e' });
    assert.equal(parts.separator.listenerCount('pointerdown'), 1);
    replacement.destroy();
    assert.equal(parts.separator.listenerCount('pointerdown'), 0);
    assert.equal(env.observers[1].disconnectCount, 1);
  } finally {
    env.restore();
  }
});

test('returns a complete idempotent no-op controller for an incomplete mount', () => {
  const pane = createResizablePane({ defaultWidth: 240 });
  assert.equal(pane.apply(300), 240);
  pane.refresh();
  pane.destroy();
  pane.destroy();
});
