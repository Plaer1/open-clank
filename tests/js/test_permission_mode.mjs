import assert from 'node:assert/strict';
import test from 'node:test';

import { initPermissionModeControl } from '../../static/js/permission-mode.js';

function fakeDom() {
  const listeners = new Map();
  const button = {
    disabled: false,
    dataset: {},
    classList: {
      values: new Set(),
      toggle(name, on) {
        if (on) this.values.add(name);
        else this.values.delete(name);
      },
    },
    setAttribute(name, value) { this[name] = String(value); },
    addEventListener(name, fn) { listeners.set(name, fn); },
    removeEventListener(name) { listeners.delete(name); },
    click() { listeners.get('click')?.(); },
  };
  const label = { textContent: '' };
  return {
    button,
    label,
    documentRef: {
      getElementById(id) {
        return id === 'permission-mode-btn' ? button : id === 'permission-mode-label' ? label : null;
      },
    },
  };
}

const response = (value, ok = true, status = 200) => ({
  ok,
  status,
  async json() { return { value }; },
});

test('permission control hydrates from server and commits acknowledged mode', async () => {
  const dom = fakeDom();
  const calls = [];
  const control = initPermissionModeControl({
    documentRef: dom.documentRef,
    fetchImpl: async (url, options = {}) => {
      calls.push({ url, options });
      return calls.length === 1 ? response('yolo') : response('auto');
    },
    showToast() {},
  });

  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(dom.label.textContent, 'Yolo');
  dom.button.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(dom.label.textContent, 'Auto');
  assert.equal(calls[1].options.method, 'PUT');
  assert.deepEqual(JSON.parse(calls[1].options.body), { value: 'auto' });
  control.destroy();
});

test('permission control rolls back a failed mutation and remains reusable', async () => {
  const dom = fakeDom();
  let count = 0;
  const control = initPermissionModeControl({
    documentRef: dom.documentRef,
    fetchImpl: async (_url, options = {}) => {
      count += 1;
      if (!options.method) return response('manual');
      return response('nope', false, 500);
    },
    showToast() {},
  });

  await new Promise((resolve) => setTimeout(resolve, 0));
  dom.button.click();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(dom.label.textContent, 'Manual');
  assert.equal(dom.button.dataset.permissionMode, 'manual');
  assert.equal(count, 2);
  control.destroy();
});
