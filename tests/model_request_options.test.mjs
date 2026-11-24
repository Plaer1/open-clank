import assert from 'node:assert/strict';
import test from 'node:test';

import {
  reasoningEffortOption,
  restoreReasoningEffortSelection,
  serializeReasoningEffortSelection,
} from '../static/js/modelRequestOptions.js';
import { catalogEntries } from '../static/js/modelCatalog.js';

const model = {
  mid: 'gpt-6.1-terra',
  optionRouteId: 'pmro_public_route_key',
  catalogRevision: 12,
  capabilities: {
    reasoning: true,
    request_options: [{
      id: 'reasoning_effort',
      type: 'enum',
      label: 'Thinking',
      values: ['low', 'medium', 'none', '0', 'low', 'inherit'],
      default: 'inherit',
      provider_default: 'medium',
      catalog_revision: 12,
    }],
  },
};

test('Thinking descriptor is route-bound and preserves explicit none and zero', () => {
  assert.deepEqual(reasoningEffortOption(model), {
    id: 'reasoning_effort',
    type: 'enum',
    label: 'Thinking',
    values: ['low', 'medium', 'none', '0'],
    default: 'inherit',
    providerDefault: 'medium',
    routeId: 'pmro_public_route_key',
    catalogRevision: 12,
  });
  assert.deepEqual(serializeReasoningEffortSelection(model, 'default'), {
    route_id: 'pmro_public_route_key',
    catalog_revision: 12,
    options: {},
  });
  assert.deepEqual(serializeReasoningEffortSelection(model, 'none'), {
    route_id: 'pmro_public_route_key',
    catalog_revision: 12,
    options: { reasoning_effort: 'none' },
  });
  assert.deepEqual(serializeReasoningEffortSelection(model, '0'), {
    route_id: 'pmro_public_route_key',
    catalog_revision: 12,
    options: { reasoning_effort: '0' },
  });
  assert.throws(() => serializeReasoningEffortSelection(model, 'xhigh'), /not supported/);
});

test('restore rejects changed route or capability revision without changing route identity', () => {
  const selected = serializeReasoningEffortSelection(model, 'low');
  assert.deepEqual(restoreReasoningEffortSelection(model, selected), {
    status: 'selected', value: 'low',
  });
  assert.deepEqual(restoreReasoningEffortSelection({
    ...model,
    optionRouteId: 'pmro_other_public_route_key',
  }, selected), { status: 'stale', value: 'inherit' });
  assert.deepEqual(restoreReasoningEffortSelection(model, null), {
    status: 'default', value: 'inherit',
  });
  assert.deepEqual(restoreReasoningEffortSelection(model, {
    route_id: 'pmro_public_route_key', catalog_revision: 12, options: {},
  }), { status: 'default', value: 'inherit' });
  for (const catalog_revision of [true, 12.5, '12']) {
    assert.deepEqual(restoreReasoningEffortSelection(model, {
      route_id: 'pmro_public_route_key', catalog_revision,
      options: { reasoning_effort: 'low' },
    }), { status: 'stale', value: 'inherit' });
  }
  assert.equal(reasoningEffortOption({ ...model, catalogRevision: 12.5 }), null);
  assert.deepEqual(restoreReasoningEffortSelection({
    ...model,
    catalogRevision: 13,
    capabilities: {
      ...model.capabilities,
      request_options: model.capabilities.request_options.map(item => ({ ...item, catalog_revision: 13 })),
    },
  }, selected), { status: 'stale', value: 'inherit' });
});

test('unsupported models expose no request option descriptor', () => {
  assert.equal(reasoningEffortOption({ mid: 'no-reasoning', capabilities: { reasoning: true } }), null);
});

test('model catalog keeps the capability revision and public route key for the picker', () => {
  const [entry] = catalogEntries({ catalog: [{
    model_id: 'gpt-6.1-terra',
    display_name: 'GPT-6.1 Terra',
    option_route_id: 'pmro_public_route_key',
    catalog_revision: 12,
    capabilities: model.capabilities,
  }] });
  assert.equal(entry.optionRouteId, 'pmro_public_route_key');
  assert.equal(entry.catalogRevision, 12);
  assert.deepEqual(entry.capabilities.request_options[0].values, ['low', 'medium', 'none', '0', 'low', 'inherit']);
});

test('work-kind control preserves sibling drafts and binds saves to the loaded account', async () => {
  const { readFileSync } = await import('node:fs');
  const vm = await import('node:vm');
  const source = readFileSync(new URL('../static/js/settings.js', import.meta.url), 'utf8');
  const start = source.indexOf('async function initMemoryWorkKinds() {');
  const end = source.indexOf('\n/* ── Memory Model ── */', start);
  assert.ok(start >= 0 && end > start);
  class FakeNode {
    constructor(tag = 'div') { this.tag = tag; this.children = []; this.listeners = {}; this.dataset = {}; this.value = ''; this.isConnected = true; this.disabled = false; this.textContent = ''; }
    append(...items) { for (const item of items) { this.children.push(item); if (this.tag === 'select' && this.children.length === 1) this.value = item.value; } }
    replaceChildren(...items) { this.children = []; this.value = ''; this.append(...items); }
    get options() { return this.children; }
    setAttribute() {}
    addEventListener(type, callback) { (this.listeners[type] ||= []).push(callback); }
    async fire(type) { for (const callback of this.listeners[type] || []) await callback({ type }); }
  }
  const rows = new FakeNode(); const status = new FakeNode(); const refresh = new FakeNode('button');
  const document = new FakeNode(); document.body = { dataset: { accountId: 'account-a' } }; document.createElement = (tag) => new FakeNode(tag);
  const window = new FakeNode(); window.sessionModule = { getCurrentSessionId: () => 'chat' };
  let owner = 'alice', revision = 0, assignments = {}, switchBeforePut = false, putBodies = [];
  let holdPut = false, startedPut;
  const putStarted = () => new Promise((resolve) => { startedPut = resolve; });
  const snapshot = () => ({ owner, revision, document: { workKindAssignments: assignments } });
  const fetch = async (url, init = {}) => {
    if (init.method !== 'PUT') return { ok: true, json: async () => snapshot() };
    const body = JSON.parse(init.body); putBodies.push(body);
    if (holdPut) return await new Promise((_resolve, reject) => { init.signal.addEventListener('abort', () => reject(new Error('aborted'))); startedPut(); });
    if (switchBeforePut) owner = 'bob';
    if (body.expected_owner !== owner || body.expected_revision !== revision) return { ok: false, status: 409 };
    const kind = url.split('/').pop(); revision++; assignments = { ...assignments, [kind]: { revision: 1, selection: body.selection && { state: 'available', ...body.selection } } };
    return { ok: true, status: 200, json: async () => snapshot() };
  };
  const modelData = { items: [{ endpoint_id: 'share:grant', endpoint_name: 'Shared', catalog: [{ model_id: 'm', provider_model_id: 'm', display_name: 'Model', option_route_id: 'opaque', catalog_revision: 12, operations: ['chat.complete'], capabilities: model.capabilities }] }] };
  const context = { document, window, fetch, AbortController, reasoningEffortOption, serializeReasoningEffortSelection,
    _fetchOwnerModelCatalog: async () => modelData, _registerAiEndpointRefresh() {},
    el: () => null };
  // Object keys with hyphens must remain literal when exposed to the fixture.
  context.el = (id) => ({ 'set-memoryWorkKindsRows': rows, 'set-memoryWorkKindsStatus': status, 'set-memoryWorkKindsRefresh': refresh })[id];
  const init = vm.runInNewContext(`(${source.slice(start, end)})`, context);
  await init();
  const [interpretation, synthesis] = rows.children;
  const [, firstRoute, firstEffort, firstSave] = interpretation.children;
  const [, otherRoute, otherEffort] = synthesis.children;
  firstRoute.value = firstRoute.options[1].value; await firstRoute.fire('change'); firstEffort.value = '0'; await firstEffort.fire('change');
  otherRoute.value = otherRoute.options[1].value; await otherRoute.fire('change'); otherEffort.value = 'none'; await otherEffort.fire('change');
  await firstSave.fire('click');
  assert.equal(otherRoute.value, otherRoute.options[1].value); assert.equal(otherEffort.value, 'none');
  assert.equal(putBodies[0].expected_owner, 'alice'); assert.equal(putBodies[0].selection.options.reasoning_effort, '0');
  holdPut = true;
  const started = putStarted(); const pending = firstSave.fire('click'); await started;
  assert.equal(firstSave.disabled, true);
  await refresh.fire('click'); await pending;
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(firstSave.disabled, false); assert.equal(otherEffort.value, 'none');
  assert.match(status.textContent, /may have completed/i);
  holdPut = false;
  modelData.items[0].catalog[0].capabilities = {};
  await refresh.fire('click'); await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(otherEffort.value, 'none');
  const [, , , otherSave] = synthesis.children;
  const beforeRefusedSave = putBodies.length;
  await otherSave.fire('click');
  assert.equal(putBodies.length, beforeRefusedSave); assert.match(status.textContent, /thinking choice unavailable/i);
  modelData.items[0].catalog[0].capabilities = model.capabilities;
  await refresh.fire('click'); await new Promise((resolve) => setTimeout(resolve, 0));
  switchBeforePut = true; await firstSave.fire('click');
  assert.equal(putBodies[2].expected_owner, 'alice'); assert.equal(revision, 1);
  assert.match(status.textContent, /changed/i);
});
