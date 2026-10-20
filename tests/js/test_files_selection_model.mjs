import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createFilesSelectionModel,
  resourceKey,
  rectangleSelection,
  transferIntent,
  buildInternalDragPayload,
  parseInternalDragPayload,
  validateDropTarget,
} from '../../static/js/filesSelectionModel.js';
import { applyRectangleSelection, safeExternalDropPath, safeExternalDropSegment } from '../../static/js/filesDropModel.js';

const entries = Array.from({ length: 6 }, (_, index) => ({ provider: 'host', resource_id: `r${index}`, resource_ref: `sealed-${index}`, name: `File ${index}` }));

test('selection is ordered, stable across ref rotation, and supports toggle/range/focus', () => {
  const model = createFilesSelectionModel({ scope: { owner: 'alice', provider: 'host', parentRef: 'folder' } });
  model.setItems(entries);
  model.select('host:r1');
  model.select('host:r4', { shiftKey: true });
  assert.deepEqual(model.selectedKeys(), ['host:r1', 'host:r2', 'host:r3', 'host:r4']);
  model.select('host:r2', { ctrlKey: true });
  assert.deepEqual(model.selectedKeys(), ['host:r1', 'host:r3', 'host:r4']);
  model.focus('host:r5', { shiftKey: true });
  assert.deepEqual(model.selectedKeys(), ['host:r2', 'host:r3', 'host:r4', 'host:r5']);
  model.setItems(entries.map((entry) => ({ ...entry, resource_ref: `${entry.resource_ref}-rotated` })));
  assert.equal(resourceKey(entries[1]), 'host:r1');
  assert.deepEqual(model.selectedKeys(), ['host:r2', 'host:r3', 'host:r4', 'host:r5']);
});

test('resource identity keeps account and workspace scope when supplied', () => {
  const alice = resourceKey({ account_id: 'alice', workspace_id: 'ws1', provider: 'host', resource_id: 'same' });
  const bob = resourceKey({ account_id: 'bob', workspace_id: 'ws2', provider: 'host', resource_id: 'same' });
  assert.notEqual(alice, bob);
  assert.equal(resourceKey({ provider: 'host', resource_id: 'same' }), 'host:same');
  assert.notEqual(
    resourceKey({ provider: 'host', resource_key: 'same-key' }, 'host', { owner: 'alice', workspace: 'ws1' }),
    resourceKey({ provider: 'host', resource_key: 'same-key' }, 'host', { owner: 'bob', workspace: 'ws2' }),
  );
});

test('rectangle, select all and transfer intent stay bounded and scoped', () => {
  assert.deepEqual(rectangleSelection([{ key: 'a', left: 0, top: 0, right: 10, bottom: 10 }, { key: 'b', left: 20, top: 20, right: 30, bottom: 30 }], { left: 5, top: 5, right: 12, bottom: 12 }).keys, ['a']);
  assert.equal(transferIntent({ platform: 'MacIntel', altKey: true }).intent, 'copy');
  assert.equal(transferIntent({ platform: 'Linux x86_64', ctrlKey: true }).intent, 'copy');
  assert.equal(transferIntent({ platform: 'MacIntel', metaKey: true }).intent, 'move');
  assert.equal(transferIntent({ platform: 'Linux', ctrlKey: true, shiftKey: true }).intent, 'reject');
  const model = createFilesSelectionModel({ scope: { owner: 'a', provider: 'host', parentRef: 'f' } });
  model.setItems(entries); model.selectAll(); assert.equal(model.selectedKeys().length, 6);
});

test('marquee selection recomputes hits when the rectangle shrinks or moves', () => {
  const base = ['host:r1'];
  assert.deepEqual(applyRectangleSelection(base, ['host:r2', 'host:r3']), ['host:r1', 'host:r2', 'host:r3']);
  assert.deepEqual(applyRectangleSelection(base, ['host:r3']), ['host:r1', 'host:r3']);
  assert.deepEqual(applyRectangleSelection(base, [], { toggle: true }), ['host:r1']);
  assert.deepEqual(applyRectangleSelection(base, ['host:r1', 'host:r4'], { toggle: true }), ['host:r4']);
});

test('external drop paths reject traversal and remain bounded to safe names', () => {
  assert.equal(safeExternalDropSegment('../escape.txt'), null);
  assert.equal(safeExternalDropSegment('nested/file.txt'), null);
  assert.equal(safeExternalDropPath(['Empty']), 'Empty');
  assert.equal(safeExternalDropPath(['Folder', 'Empty']), 'Folder/Empty');
  assert.equal(safeExternalDropPath(['Folder', 'nested', 'file.txt']), 'Folder/nested/file.txt');
  assert.equal(safeExternalDropPath(['Folder', '..', 'escape.txt']), null);
  assert.equal(safeExternalDropPath(['Folder', 'a'.repeat(1025)]), null);
});

test('internal payload carries revisions and rejects stale or incapable destinations', () => {
  const payload = buildInternalDragPayload({ scope: { owner: 'alice', provider: 'host', parentRef: 'f' }, generation: 7, policyGeneration: 7, entries: entries.map((entry) => ({ ...entry, revision: { kind: 'hostFingerprint', value: entry.resource_id } })), selectedKeys: ['host:r1', 'host:r2'] });
  const parsed = parseInternalDragPayload(JSON.stringify(payload));
  assert.equal(parsed.sources.length, 2);
  assert.equal(parsed.sources[0].resource_ref, 'sealed-1');
  assert.equal(validateDropTarget(parsed, { resource_ref: 'dest' }, { generation: 8, provider: 'host', allowMove: true }).ok, false);
  assert.equal(validateDropTarget(parsed, { resource_ref: 'dest' }, { generation: 7, provider: 'host', allowMove: true }).ok, true);
  assert.equal(validateDropTarget(parsed, { resource_ref: 'dest' }, { generation: 7, provider: 'host', allowCopy: false }).ok, false);
});

test('scope changes always clear selection and unfocused keyboard movement starts at an edge', () => {
  const model = createFilesSelectionModel({ scope: { owner: 'alice', provider: 'host', parentRef: 'one' } });
  model.setItems(entries);
  model.select('host:r2');
  model.setScope({ owner: 'alice', provider: 'host', parentRef: 'two' }, { preserve: true });
  model.setItems(entries);
  assert.deepEqual(model.selectedKeys(), []);
  assert.equal(model.moveFocus(1), true);
  assert.equal(model.focusKey, 'host:r0');
  model.setScope({ owner: 'alice', provider: 'host', parentRef: 'three' });
  model.setItems(entries);
  assert.equal(model.moveFocus(-1), true);
  assert.equal(model.focusKey, 'host:r5');
});

test('drag payload parser canonicalizes and rejects untrusted shapes and bounds', () => {
  const payload = buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' },
    generation: 4, policyGeneration: 9, entries,
    selectedKeys: ['host:r1', 'host:r2'],
  });
  assert.equal(payload.pane, 'files-window');
  assert.equal(Object.isFrozen(payload), true);
  assert.equal(Object.isFrozen(payload.sources), true);
  assert.equal(Object.isFrozen(payload.sources[0]), true);
  assert.equal(Object.isFrozen(payload.sources[0].revision), true);
  assert.equal(parseInternalDragPayload({ ...payload, generation: -1 }), null);
  assert.equal(parseInternalDragPayload({ ...payload, policy_generation: Infinity }), null);
  assert.equal(parseInternalDragPayload({ ...payload, owner: '' }), null);
  assert.equal(parseInternalDragPayload({ ...payload, sources: [payload.sources[0], payload.sources[0]] }), null);
  assert.equal(parseInternalDragPayload({ ...payload, sources: [{ ...payload.sources[0], revision: { kind: 'host', value: 'v', secret: 'leak' } }] }), null);
  assert.equal(parseInternalDragPayload(JSON.stringify(payload) + 'x'.repeat(70_000)), null);
  const largeEntries = Array.from({ length: 201 }, (_, index) => ({ provider: 'host', resource_id: `large-${index}`, resource_ref: `large-ref-${index}` }));
  assert.equal(buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' }, generation: 1, policyGeneration: 1,
    entries: largeEntries, selectedKeys: largeEntries.map((entry) => `host:${entry.resource_id}`),
  }), null, 'bounded HTML5 payloads reject oversized selections instead of truncating');
});

test('drag authority fields use UTF-8 byte limits and rerendering preserves epoch', () => {
  const model = createFilesSelectionModel({ scope: { owner: 'alice', provider: 'host', parentRef: 'folder' } });
  model.setItems(entries);
  const epoch = model.epoch;
  model.setItems(entries.map((entry) => ({ ...entry })));
  assert.equal(model.epoch, epoch);
  const payload = buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' },
    generation: 1, policyGeneration: 1, entries: [{ ...entries[0], item_id: '😀'.repeat(40), resource_ref: 'ref' }],
    selectedKeys: ['host:r0'],
  });
  assert.equal(payload, null, 'a 128-byte item id budget must reject multibyte overflow');
  const valid = buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' },
    generation: 1, policyGeneration: 1, entries: [{ ...entries[0], item_id: '😀'.repeat(32), resource_ref: 'ref' }],
    selectedKeys: ['host:r0'],
  });
  assert.ok(valid);
  assert.equal(parseInternalDragPayload({ ...valid, sources: [{ ...valid.sources[0], item_id: '😀'.repeat(33) }] }), null);
});

test('internal transfer authority requires a provider and complete parent scope', () => {
  const valid = buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' }, generation: 2, policyGeneration: 2,
    entries: [entries[0]], selectedKeys: ['host:r0'],
  });
  assert.ok(valid);
  assert.equal(parseInternalDragPayload({ ...valid, provider: '' }), null);
  const { workspace: _workspace, ...withoutWorkspace } = valid;
  const { column: _column, ...withoutColumn } = valid;
  assert.equal(parseInternalDragPayload(withoutWorkspace), null, 'workspace is required for internal authority payloads');
  assert.equal(parseInternalDragPayload(withoutColumn), null, 'column scope is required for in-flight payload validation');
  assert.equal(parseInternalDragPayload({ ...valid, parent_ref: '' }), null);
  assert.equal(validateDropTarget(valid, { resource_ref: 'dest' }, {
    generation: 2, policyGeneration: 2, owner: 'alice', pane: 'files-window', provider: '', allowMove: true,
  }).ok, false);
  assert.equal(validateDropTarget(valid, { resource_ref: 'dest' }, {
    generation: 2, policyGeneration: 2, owner: 'alice', pane: 'files-window', provider: 'gallery', allowMove: true,
  }).ok, false);
});

test('drop target binds the captured owner and pane before dispatch', () => {
  const payload = buildInternalDragPayload({
    scope: { owner: 'alice', provider: 'host', parentRef: 'folder' }, generation: 3, policyGeneration: 3,
    owner: 'alice', pane: 'files-window', entries: [entries[0]], selectedKeys: ['host:r0'],
  });
  assert.ok(payload);
  assert.equal(validateDropTarget(payload, { resource_ref: 'other' }, {
    generation: 3, policyGeneration: 3, owner: 'mallory', pane: 'files-window', provider: 'host', allowMove: true,
  }).ok, false);
  assert.equal(validateDropTarget(payload, { resource_ref: 'other' }, {
    generation: 3, policyGeneration: 3, owner: 'alice', pane: 'other-pane', provider: 'host', allowMove: true,
  }).ok, false);
  assert.equal(validateDropTarget(payload, { resource_ref: 'other' }, {
    generation: 3, policyGeneration: 3, owner: 'alice', pane: 'files-window', provider: 'host', allowMove: true,
  }).ok, true);
});
