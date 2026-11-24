import assert from 'node:assert/strict';
import test from 'node:test';

import { FilesFacadeClient, transferRootsToAsk } from '../../static/js/filesFacadeClient.js';

const rev = (value) => ({ kind: 'mtime', value });
function clientReplying(reply, bodies = []) {
  return new FilesFacadeClient({ baseUrl: '/f', fetchImpl: async (_url, options) => {
    bodies.push(options.body);
    return { ok: true, status: 200, async json() { return reply; } };
  } });
}
const pre = { generation: 3, sources: [{ itemId: 'i1', resourceRef: 'rr1.a' }], destinationRef: 'rr1.d' };
const base = { operationId: 'op1', generation: 3, kind: 'move', sources: [{ itemId: 'i1', resourceRef: 'rr1.a', expectedRevision: rev('7') }], destinationRef: 'rr1.d', collision: 'fail' };
// Captured from the unmodified transferResources before this change.
const FROZEN_NO_DEST = '{"operation_id":"op1","generation":3,"kind":"move","sources":[{"item_id":"i1","resource_ref":"rr1.a","expected_revision":{"kind":"mtime","value":"7"}}],"destination_ref":"rr1.d","collision":"fail"}';
const FROZEN_MANAGED = '{"operation_id":"op1","generation":3,"kind":"move","sources":[{"item_id":"i1","resource_ref":"rr1.a","expected_revision":{"kind":"mtime","value":"7"}}],"destination_ref":"rr1.d","collision":"fail","managed_root":{"resource_ref":"rr1.root","expected_revision":{"kind":"mtime","value":"9"}}}';

test('root requirement: two-key and old one-key replies are accepted', async () => {
  assert.deepEqual(await clientReplying({ root_required: true, destination_root_required: true }).transferRootRequirement(pre), { root_required: true, destination_root_required: true });
  assert.deepEqual(await clientReplying({ root_required: false, destination_root_required: false }).transferRootRequirement(pre), { root_required: false, destination_root_required: false });
  assert.deepEqual(await clientReplying({ root_required: true }).transferRootRequirement(pre), { root_required: true, destination_root_required: false });
});

test('root requirement: extra key, non-boolean or empty reply throws', async () => {
  for (const bad of [{ root_required: true, destination_root_required: false, x: 1 }, { root_required: true, destination_root_required: 'yes' }, { root_required: 1 }, { destination_root_required: true }, null]) {
    await assert.rejects(clientReplying(bad).transferRootRequirement(pre), /Transfer preflight is unavailable/);
  }
});

test('transferResources body is unchanged without destinationRoot', async () => {
  const bodies = [];
  const c = clientReplying({}, bodies);
  await c.transferResources(base).catch(() => {});
  await c.transferResources({ ...base, managedRoot: { ref: 'rr1.root', revision: rev('9') } }).catch(() => {});
  assert.equal(bodies[0], FROZEN_NO_DEST);
  assert.equal(bodies[1], FROZEN_MANAGED);
});

test('destinationRoot adds only destination_root; bad revision is refused', async () => {
  const bodies = [];
  const c = clientReplying({}, bodies);
  await c.transferResources({ ...base, destinationRoot: { ref: 'rr1.dest', revision: rev('5') } }).catch(() => {});
  const expected = { ...JSON.parse(FROZEN_NO_DEST), destination_root: { resource_ref: 'rr1.dest', expected_revision: rev('5') } };
  assert.deepEqual(JSON.parse(bodies[0]), expected);
  await c.transferResources({ ...base, destinationRoot: { ref: 'rr1.dest' } }).catch(() => {});
  assert.deepEqual(JSON.parse(bodies[1]).destination_root, { resource_ref: 'rr1.dest' });
  assert.throws(() => c.transferResources({ ...base, destinationRoot: { ref: 'rr1.dest', revision: { kind: 'mtime', value: '5', extra: 1 } } }), /revision is invalid/);
  assert.equal(bodies.length, 2);
});

test('transferRootsToAsk: order of roots to pick', () => {
  assert.deepEqual(transferRootsToAsk({ root_required: false, destination_root_required: false }), []);
  assert.deepEqual(transferRootsToAsk({ root_required: false, destination_root_required: true }), ['destination']);
  assert.deepEqual(transferRootsToAsk({ root_required: true, destination_root_required: false }), ['managed']);
  assert.deepEqual(transferRootsToAsk({ root_required: true, destination_root_required: true }), ['managed', 'destination']);
});

test('flow: a cancel at either pick sends no request', async () => {
  // Mirrors the loop in files.js executeFilesTransfer with a scripted picker.
  const run = async (answers) => {
    let sent = 0; const roots = {};
    for (const which of transferRootsToAsk({ root_required: true, destination_root_required: true })) {
      const root = answers.shift();
      if (!root) return { sent, picks: Object.keys(roots) };
      roots[which] = root;
    }
    sent += 1; return { sent, picks: Object.keys(roots) };
  };
  assert.deepEqual(await run([null]), { sent: 0, picks: [] });
  assert.deepEqual(await run(['m', null]), { sent: 0, picks: ['managed'] });
  assert.deepEqual(await run(['m', 'd']), { sent: 1, picks: ['managed', 'destination'] });
});
