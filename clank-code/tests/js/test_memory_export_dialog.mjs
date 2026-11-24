import assert from 'node:assert/strict';
import test from 'node:test';

import {
  createMemoryExportDialog,
  downloadMemoryAsset,
} from '../../static/js/memoryExportDialog.js';

// Minimal DOM stub — just enough surface for the dialog module (createElement,
// classList, append, listeners, select.selectedOptions). No browser needed.
class FakeClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set(String(this.el.className || '').split(/\s+/).filter(Boolean)); }
  add(...names) { const s = this._set(); names.forEach((n) => s.add(n)); this.el.className = [...s].join(' '); }
  remove(...names) { const s = this._set(); names.forEach((n) => s.delete(n)); this.el.className = [...s].join(' '); }
  contains(name) { return this._set().has(name); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.childNodes = [];
    this.listeners = {};
    this.className = '';
    this.textContent = '';
    this._value = undefined;
    this.checked = false;
    this.disabled = false;
    this.selected = false;
    this.classList = new FakeClassList(this);
  }
  // A real <select> reports its first option's value until set; mirror that
  // so bundle-only controls read their default instead of an empty string.
  get value() {
    if (this._value !== undefined) return this._value;
    return this.tagName === 'SELECT' ? (this.children[0]?.value ?? '') : '';
  }
  set value(v) { this._value = v; }
  append(...nodes) {
    for (const node of nodes) {
      this.childNodes.push(node);
      if (node instanceof FakeElement) this.children.push(node);
    }
  }
  appendChild(node) { this.append(node); return node; }
  setAttribute(name, value) { (this.attributes ||= {})[name] = value; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async dispatch(type) { for (const fn of this.listeners[type] || []) await fn({ target: this }); }
  click() { return this.dispatch('click'); }
  get selectedOptions() { return this.children.filter((c) => c.tagName === 'OPTION' && c.selected); }
}

function fakeDocument() {
  const created = [];
  const doc = {
    created,
    body: new FakeElement('body'),
    createElement(tag) {
      const el = new FakeElement(tag);
      created.push(el);
      return el;
    },
    createTextNode(text) { return { nodeType: 3, textContent: String(text) }; },
  };
  return doc;
}

function okResponse() {
  return { ok: true, status: 200, blob: async () => new Blob(['bytes']) };
}

function harness({ fetchImpl } = {}) {
  const doc = fakeDocument();
  const calls = [];
  const toasts = [];
  const errors = [];
  const dialog = createMemoryExportDialog({
    document: doc,
    host: doc.body,
    fetchImpl: fetchImpl || (async (url, opts) => { calls.push([url, opts]); return okResponse(); }),
    urlApi: { createObjectURL: () => 'blob:mock', revokeObjectURL() {} },
    showToast: (msg) => toasts.push(msg),
    showError: (msg) => errors.push(msg),
  });
  return { doc, calls, toasts, errors, dialog };
}

const flush = async () => { await new Promise((r) => setTimeout(r, 0)); await new Promise((r) => setTimeout(r, 0)); };

test('dialog renders hidden with every control and opens on demand', () => {
  const { doc, dialog } = harness();
  assert.ok(doc.body.children.includes(dialog.element), 'overlay mounts into the host');
  assert.ok(dialog.element.classList.contains('hidden'));
  const { controls } = dialog;
  assert.equal(controls.formatBundle.checked, true);
  assert.equal(controls.sectionBoxes.length, 8);
  assert.ok(controls.sectionBoxes.every((box) => box.checked));
  assert.equal(controls.kindSelect.children.length, 8);
  assert.equal(controls.assetsSelect.value, 'all');
  dialog.open();
  assert.ok(!dialog.element.classList.contains('hidden'));
  dialog.close();
  assert.ok(dialog.element.classList.contains('hidden'));
});

test('untouched submit downloads the exact pre-dialog full bundle', async () => {
  const { doc, calls, toasts, dialog } = harness();
  dialog.open();
  await dialog.controls.submitBtn.click();
  await flush();
  assert.deepEqual(calls, [['/api/memory/export?format=bundle', { credentials: 'same-origin' }]]);
  const anchor = doc.created.find((el) => el.tagName === 'A');
  assert.equal(anchor.href, 'blob:mock');
  assert.equal(anchor.download, 'open-clank-memory-bundle-v3.zip');
  assert.deepEqual(toasts, ['Exported the canonical Memory bundle']);
  assert.ok(dialog.element.classList.contains('hidden'), 'dialog closes after export');
});

test('edited filters assemble the S00 query string and download JSON', async () => {
  const { calls, dialog } = harness();
  const { controls } = dialog;
  dialog.open();
  controls.formatBundle.checked = false;
  controls.formatJson.checked = true;
  await controls.formatJson.dispatch('change');
  assert.equal(controls.assetsSelect.disabled, true, 'asset mode is bundle-only');
  controls.sectionBoxes.find((box) => box.value === 'raw').checked = false;
  controls.kindSelect.children.find((o) => o.value === 'fact').selected = true;
  controls.dateFrom.value = '2026-08-01';
  controls.dateTo.value = '2026-08-21';
  controls.qInput.value = 'dentist';
  controls.archivedToggle.checked = false;
  await controls.submitBtn.click();
  await flush();
  const params = new URLSearchParams(calls[0][0].split('?')[1]);
  assert.equal(params.get('format'), null, 'JSON exports omit the format param');
  assert.equal(params.get('sections'), 'candidates,curated,quarantine,graph_nodes,graph_edges,graph_cues,tombstones');
  assert.equal(params.get('kinds'), 'fact');
  assert.equal(params.get('since'), '2026-08-01');
  assert.equal(params.get('until'), '2026-08-21T23:59:59Z', 'date-only "to" is inclusive of the whole day');
  assert.equal(params.get('q'), 'dentist');
  assert.equal(params.get('include_archived'), 'false');
});

test('client-side validation mirrors the typed 422 and blocks the request', async () => {
  const { calls, errors, dialog } = harness();
  const { controls } = dialog;
  dialog.open();
  controls.dateFrom.value = 'garbage';
  await controls.submitBtn.click();
  await flush();
  assert.deepEqual(calls, [], 'no request leaves the dialog');
  assert.deepEqual(errors, [], 'validation stays inline, not toasted');
  assert.equal(
    controls.errorLine.textContent,
    'since must be an ISO-8601 timestamp, e.g. 2026-08-21T00:00:00Z.',
  );
  assert.ok(!controls.errorLine.classList.contains('hidden'));
  assert.ok(!dialog.element.classList.contains('hidden'), 'dialog stays open for correction');
});

test('images-only shortcut submits a restorable asset-only bundle', async () => {
  const { calls, dialog } = harness();
  dialog.open();
  await dialog.controls.imagesOnlyBtn.click();
  await flush();
  const params = new URLSearchParams(calls[0][0].split('?')[1]);
  assert.equal(params.get('format'), 'bundle');
  assert.equal(params.get('assets'), 'images');
  assert.equal(params.get('assets_only'), 'true');
});

test('server failure toasts the decoded error and keeps the dialog usable', async () => {
  const { errors, dialog } = harness({
    fetchImpl: async () => ({
      ok: false,
      status: 503,
      text: async () => 'provider unavailable',
    }),
  });
  dialog.open();
  await dialog.controls.submitBtn.click();
  await flush();
  assert.deepEqual(errors, ['provider unavailable']);
  assert.equal(dialog.controls.submitBtn.disabled, false);
});

test('per-photo download streams the asset endpoint with the photo filename', async () => {
  const doc = fakeDocument();
  const calls = [];
  const toasts = [];
  await downloadMemoryAsset({
    document: doc,
    fetchImpl: async (url, opts) => { calls.push([url, opts]); return okResponse(); },
    urlApi: { createObjectURL: () => 'blob:photo', revokeObjectURL() {} },
    showToast: (msg) => toasts.push(msg),
    showError: (msg) => { throw new Error(`unexpected error toast: ${msg}`); },
  }, 'asset_ab12', 'kitchen.png');
  assert.deepEqual(calls, [['/api/memory/assets/asset_ab12', { credentials: 'same-origin' }]]);
  const anchor = doc.created.find((el) => el.tagName === 'A');
  assert.equal(anchor.download, 'kitchen.png');
  assert.deepEqual(toasts, ['Downloaded photo']);
});

test('per-photo download surfaces typed 404/409 errors through the error toast', async () => {
  const doc = fakeDocument();
  const errors = [];
  await downloadMemoryAsset({
    document: doc,
    fetchImpl: async () => ({
      ok: false,
      status: 404,
      text: async () => JSON.stringify({ detail: { code: 'asset_not_found', message: 'Unknown or foreign photo asset.', retryable: false } }),
    }),
    urlApi: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
    showToast: () => { throw new Error('failure must not toast success'); },
    showError: (msg) => errors.push(msg),
  }, 'asset_missing', 'gone.png');
  assert.deepEqual(errors, ['Unknown or foreign photo asset.']);
  assert.ok(!doc.created.some((el) => el.tagName === 'A'), 'no download anchor on failure');
});
