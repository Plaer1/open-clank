import assert from 'node:assert/strict';
import test from 'node:test';

import { initInteractionModeControl } from '../../static/js/interaction-mode.js';

function fakeDom() {
  const documentListeners = new Map();
  const makeClassList = () => ({
    values: new Set(),
    toggle(name, on) { if (on) this.values.add(name); else this.values.delete(name); },
  });
  const makeNode = () => {
    const listeners = new Map();
    return {
      hidden: false,
      dataset: {},
      classList: makeClassList(),
      attributes: {},
      listeners,
      setAttribute(name, value) { this.attributes[name] = String(value); this[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; },
      addEventListener(name, fn) { listeners.set(name, fn); },
      removeEventListener(name) { listeners.delete(name); },
      focus() { documentRef.activeElement = this; },
      click() { listeners.get('click')?.({ target: this }); },
      contains(target) { return target === this || target?.parentNode === this; },
      closest(selector) { return selector === '[data-interaction-mode]' && this.dataset.interactionMode ? this : null; },
    };
  };
  const button = makeNode();
  const label = makeNode();
  const menu = makeNode();
  const root = makeNode();
  const options = ['chat', 'plan', 'agent'].map((mode) => {
    const option = makeNode();
    option.dataset.interactionMode = mode;
    option.parentNode = menu;
    return option;
  });
  menu.querySelectorAll = () => options;
  root.contains = (target) => [root, button, menu, ...options].includes(target);
  const documentRef = {
    activeElement: null,
    getElementById(id) {
      return {
        'chat-mode-picker': root,
        'chat-mode-picker-btn': button,
        'chat-mode-picker-label': label,
        'chat-mode-picker-menu': menu,
      }[id] || null;
    },
    addEventListener(name, fn) { documentListeners.set(name, fn); },
    removeEventListener(name) { documentListeners.delete(name); },
  };
  return { documentRef, root, button, label, menu, options, documentListeners };
}

test('interaction picker exposes one public mode and keyboard/click selection', () => {
  const dom = fakeDom();
  globalThis.window = {};
  const selected = [];
  const control = initInteractionModeControl({
    documentRef: dom.documentRef,
    getMode: () => 'agent',
    setMode: (mode) => selected.push(mode),
  });

  assert.equal(dom.label.textContent, 'Agent');
  assert.equal(dom.button.dataset.mode, 'agent');
  dom.button.click();
  assert.equal(dom.menu.hidden, false);
  dom.menu.listeners.get('click')({ target: dom.options[1] });
  assert.deepEqual(selected, ['plan']);
  assert.equal(dom.label.textContent, 'Plan');
  assert.equal(dom.menu.hidden, true);

  control.destroy();
  assert.equal(dom.menu.listeners.has('click'), false);
  assert.equal(window.__odysseusInteractionModeSync, undefined);
});
