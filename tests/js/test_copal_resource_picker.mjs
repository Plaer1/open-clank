import assert from 'node:assert/strict';
import test from 'node:test';

import { createResourcePicker, normalizeAuthorizedResource } from '../../static/js/copal/resourcePicker.js';

const folder = (ref, name, provider = 'host') => ({ ref, id:`stable-${ref}`, name, provider, kind:'folder', capabilities:['read', 'stat', 'children', 'write'] });
const file = (ref, name, provider = 'host') => ({ ref, id:`stable-${ref}`, name, provider, kind:'file', revision:{ kind:'hostFingerprint', value:`rev-${ref}` }, capabilities:['read', 'open', 'edit'] });

test('picker emits immutable opaque identity and keeps provider/root namespaces distinct', () => {
  const host = normalizeAuthorizedResource(file('same-ref-host', 'index.md', 'host'), { purpose:'file', accountScope:'a', workspaceScope:'w', generation:7 });
  const copal = normalizeAuthorizedResource(file('same-ref-copal', 'index.md', 'copal'), { purpose:'file', accountScope:'a', workspaceScope:'w', generation:7 });
  assert.equal(host.ref, 'same-ref-host');
  assert.equal(copal.provider, 'copal');
  assert.notEqual(host.resourceKey, copal.resourceKey);
  assert(Object.isFrozen(host));
  assert(Object.isFrozen(host.capabilities));
  assert.throws(() => { host.ref = 'path'; }, TypeError);
});

test('picker preserves object ResourceKey authority components', () => {
  const first = normalizeAuthorizedResource({ ref:'sealed-a', name:'same.md', provider:'host', kind:'file', resource_key:{ accountId:'alice', workspaceId:'one', provider:'host', resourceId:'same' }, capabilities:['read','open'] }, { purpose:'file' });
  const second = normalizeAuthorizedResource({ ref:'sealed-b', name:'same.md', provider:'host', kind:'file', resource_key:{ accountId:'bob', workspaceId:'one', provider:'host', resourceId:'same' }, capabilities:['read','open'] }, { purpose:'file' });
  assert.notEqual(first.resourceKey, second.resourceKey);
  assert.match(first.resourceKey, /alice/);
  assert.match(second.resourceKey, /bob/);
});

test('picker pages children and searches through the facade without mutations', async () => {
  const calls = [];
  const client = {
    async roots() { calls.push(['roots']); return { policy_generation:9, entries:[folder('host-root', 'Host'), folder('copal-root', 'Copal', 'copal')] }; },
    async children(ref, options) { calls.push(['children', ref, options]); return { entries:[file(`${ref}-a`, 'a.md'), folder(`${ref}-nested`, 'Nested')], next_cursor:null, policy_generation:9 }; },
    async search(query, options) { calls.push(['search', query, options]); return { entries:[file('search-a', `${query}.base`, 'copal')], policy_generation:10 }; },
  };
  const picker = createResourcePicker({ client, purpose:'file', accountScope:'acct', workspaceScope:'workspace' });
  await picker.open();
  const roots = picker.state().rows;
  assert.equal(roots.length, 2);
  await picker.loadChildren(roots[0].ref);
  assert.equal(picker.state().rows[0].ref, 'host-root-a');
  await picker.search('report');
  assert.equal(picker.state().rows[0].name, 'report.base');
  assert.equal(calls.filter(([kind]) => kind === 'roots').length, 1);
  assert.equal(calls.filter(([kind]) => kind === 'children').length, 1);
  assert.equal(calls.filter(([kind]) => kind === 'search').length, 1);
  assert.equal(calls.some(([kind]) => ['create', 'save', 'move', 'copy'].includes(kind)), false);
  picker.destroy();
});

test('close aborts in-flight work and late results cannot replace the selection', async () => {
  let resolveRoots;
  const client = {
    roots:() => new Promise(resolve => { resolveRoots = resolve; }),
    children:async () => ({ entries:[] }),
  };
  const picker = createResourcePicker({ client, purpose:'folder' });
  const opening = picker.open();
  picker.close();
  resolveRoots({ entries:[folder('late', 'Late')] });
  await opening;
  assert.equal(picker.state().closed, true);
  assert.equal(picker.state().rows.length, 0);
});

test('folder traversal is explicit, capabilities gate selection, and stale searches cannot win', async () => {
  let resolveOld;
  const client = {
    async roots() { return { entries:[folder('root', 'Approved')] }; },
    async children(ref) { return { entries:[folder(`${ref}-nested`, 'Nested'), file(`${ref}-file`, 'readme.md')] }; },
    search(query) {
      if (query === 'old') return new Promise(resolve => { resolveOld = resolve; });
      return Promise.resolve({ entries:[file('new-file', 'new.md')] });
    },
  };
  const picker = createResourcePicker({ client, purpose:'file' });
  await picker.open();
  const root = picker.state().rows[0];
  assert.equal(await picker.enter(root), true);
  assert.equal(picker.state().currentFolder.ref, 'root');
  assert.equal(picker.select(picker.state().rows[1]), true);
  assert.equal(picker.select(picker.state().currentFolder), false, 'file purpose cannot choose a folder');
  const old = picker.search('old');
  const fresh = picker.search('new');
  await fresh;
  resolveOld({ entries:[file('old-file', 'old.md')] });
  await old;
  assert.equal(picker.state().rows[0].name, 'new.md');
  assert.equal(picker.back(), true);
  assert.equal(picker.state().currentFolder, null);
  picker.destroy();
});

test('failed child navigation leaves the prior folder and history unchanged', async () => {
  const client = {
    async roots() { return { entries:[folder('root', 'Approved')] }; },
    async children() { throw new Error('revoked'); },
  };
  const picker = createResourcePicker({ client, purpose:'folder' });
  await picker.open();
  const before = picker.state();
  assert.equal(await picker.enter(before.rows[0]), false);
  const after = picker.state();
  assert.equal(after.currentFolder, null);
  assert.equal(after.parentRef, null);
  assert.equal(after.navigationDepth, 0);
  assert.deepEqual(after.rows.map(row => row.ref), before.rows.map(row => row.ref));
  picker.destroy();
});

test('malformed pages fail closed without replacing the prior authorized view', async () => {
  let malformed = false;
  const client = {
    async roots() { return malformed ? {} : { entries:[folder('root', 'Approved')] }; },
    async children() { return { entries:[] }; },
  };
  const picker = createResourcePicker({ client, purpose:'file' });
  await picker.open();
  const before = picker.state();
  malformed = true;
  await assert.rejects(() => picker.loadRoots(), /malformed|authorized entry page/);
  const after = picker.state();
  assert.deepEqual(after.rows.map(row => row.ref), before.rows.map(row => row.ref));
  assert(after.error);
  picker.destroy();
});

test('load-more supersedes duplicate clicks and deduplicates provider identities', async () => {
  let pending;
  const calls = [];
  const client = {
    async roots() { return { entries:[folder('root', 'Approved')] }; },
    children(ref, options = {}) {
      calls.push({ ref, options });
      if (!options.cursor) return Promise.resolve({ entries:[file('first', 'first.md')], next_cursor:'page-2' });
      return new Promise(resolve => { pending = resolve; });
    },
  };
  const picker = createResourcePicker({ client, purpose:'file' });
  await picker.open();
  await picker.loadChildren('root');
  const first = picker.loadChildren('root', { cursor:'page-2', append:true });
  const duplicate = picker.loadChildren('root', { cursor:'page-2', append:true });
  assert.equal(calls.length, 2, 'a second load-more click is ignored while the page is pending');
  pending({ entries:[file('first', 'first.md'), file('second', 'second.md')], next_cursor:null });
  await Promise.all([first, duplicate]);
  assert.deepEqual(picker.state().rows.map(row => row.ref), ['first', 'second']);
  picker.destroy();
});
