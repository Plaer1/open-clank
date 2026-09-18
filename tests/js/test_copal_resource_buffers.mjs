import assert from 'node:assert/strict';
import { normalizeResourceHandle, resourceKeyId, snapshotEnvelope, cloneEnvelope } from '../../static/js/copal/resourceModel.js';
import { createBufferRegistry } from '../../static/js/copal/documentBuffers.js';

const handle = normalizeResourceHandle({
  key:{ accountId:'acct-a', workspaceId:'ws-a', provider:'copal', resourceId:'origin-1' },
  revision:{ kind:'copalHead', value:'head-1' },
  locator:{ displayName:'Note', locationLabel:'folder/Note' },
  representation:'nativeNote',
  capabilities:{ read:true, edit:true },
});

assert.equal(resourceKeyId(handle.key), 'acct-a:ws-a:copal:origin-1');
const submitted = [];
const observed = [];
const releases = [];
const registry = createBufferRegistry({ storage:null });
const buffer = registry.acquire(handle, { body:'one', properties:{ priority:'low' }, relations:[] }, {
  save:async (snapshot) => {
    submitted.push(snapshot);
    observed.push(cloneEnvelope(snapshot));
    assert.equal(Object.isFrozen(snapshot.envelope), true);
    await new Promise((resolve) => releases.push(resolve));
    return { outcome:'applied', revision:{ kind:'copalHead', value:`head-${submitted.length + 1}` } };
  },
});

buffer.apply((draft) => ({ ...draft, properties:{ priority:'high' } }));
const firstSave = buffer.flush();
buffer.apply((draft) => ({ ...draft, properties:{ priority:'urgent' }, body:'one' })); // distinct metadata, equal body
assert.match(submitted[0]?.actionId || '', /^save-/);
assert.equal(buffer.snapshot().scope.actorId, 'acct-a');
assert.equal(buffer.snapshot().scope.accountId, 'acct-a');
assert.equal(buffer.snapshot().scope.workspace, 'ws-a');
releases.shift()();
assert.equal(submitted[0].localRevision, 1);
while (submitted.length < 2) await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(submitted.length, 2);
assert.equal(submitted[1].localRevision, 2);
releases.shift()();
await firstSave;
assert.equal(buffer.state().dirty, false);
assert.deepEqual(observed[0].envelope.properties, { priority:'high' }, 'submitted metadata is immutable');
assert.equal(submitted[1].expectedRevision.value, 'head-2');
assert.notEqual(submitted[0].actionId, submitted[1].actionId, 'each edited local revision gets a new action binding');

// Sheet provenance belongs to the submitted local revision. An ordinary edit
// that supersedes a still-running sheet save must not inherit its conflict UI
// suppression on the follow-up flush.
const provenance = [];
let releaseProvenance;
const provenanceBuffer = createBufferRegistry({ storage:null }).acquire({ ...handle, key:{ ...handle.key, resourceId:'sheet-provenance' } }, { body:'accepted' }, {
  save:async (snapshot, options) => {
    provenance.push({ revision:snapshot.localRevision, sheet:options.sheet });
    if (provenance.length === 1) await new Promise((resolve) => { releaseProvenance = resolve; });
    return { outcome:'applied', revision:{ kind:'copalHead', value:`sheet-head-${snapshot.localRevision}` } };
  },
});
provenanceBuffer.apply({ body:'sheet edit' }, { sheet:true });
const provenanceDrain = provenanceBuffer.flush();
while (!releaseProvenance) await new Promise((resolve) => setTimeout(resolve, 0));
provenanceBuffer.apply({ body:'ordinary edit' });
releaseProvenance();
await provenanceDrain;
assert.deepEqual(provenance, [{ revision:1, sheet:true }, { revision:2, sheet:false }]);

buffer.apply((draft) => ({ ...draft, properties:{ priority:'third' } }));
const thirdSave = buffer.flush();
releases.shift()();
await thirdSave;
assert.equal(submitted[2].localRevision, 3);
assert.equal(submitted[2].expectedRevision.value, 'head-3');

const conflict = buffer.resolveExternal({ body:'remote' }, { kind:'copalHead', value:'head-remote' });
assert.equal(conflict, true, 'clean buffers refresh from external state');
buffer.apply((draft) => ({ ...draft, body:'local' }));
assert.equal(buffer.resolveExternal({ body:'other' }, { kind:'copalHead', value:'head-other' }), false);
assert.equal(buffer.state().status, 'conflict');

const draft = snapshotEnvelope({ key:handle.key, expectedRevision:handle.revision, localRevision:7, envelope:{ body:'recover me' } });
const restored = registry.acquire(handle, { body:'accepted' });
restored.recover(draft);
assert.equal(restored.state().dirty, true);
assert.equal(restored.snapshot().envelope.body, 'recover me');

const history = registry.acquire({ ...handle, key:{ ...handle.key, resourceId:'history' } }, { body:'a' }, { historyLimit:2 });
history.apply({ body:'b' });
history.apply({ body:'c' });
assert.equal(history.undo().envelope.body, 'b');
assert.equal(history.redo().envelope.body, 'c');

// Oversized edits commit without retaining multiple full-envelope undo copies;
// bounded transactions still retain undo history under the byte budget.
const largeHistory = registry.acquire({ ...handle, key:{ ...handle.key, resourceId:'large-history' } }, { body:'seed' }, { historyLimitBytes:64 * 1024 });
const largeBody = 'x'.repeat(70 * 1024);
largeHistory.apply({ body:largeBody });
assert.equal(largeHistory.history.length, 0, 'an oversized envelope is excluded before undo copies are made');
assert(largeHistory.historyBytes <= largeHistory.historyLimitBytes);
for (let index = 0; index < 4; index += 1) largeHistory.apply({ body:`small-${index}` });
assert(largeHistory.historyBytes <= largeHistory.historyLimitBytes, 'bounded transaction history stays within its byte budget');
assert.equal(largeHistory.undo().envelope.body, 'small-2');

let refreshedBase = null;
const refreshedEdit = registry.acquire({ ...handle, key:{ ...handle.key, resourceId:'refresh-edit' } }, { body:'old' }, {
  save:async (snapshot) => { refreshedBase = snapshot.expectedRevision.value; return { outcome:'applied', revision:{ kind:'copalHead', value:'head-after-edit' } }; },
});
assert.equal(refreshedEdit.resolveExternal({ body:'authoritative' }, { kind:'copalHead', value:'head-authoritative' }), true);
refreshedEdit.apply({ body:'next local edit' });
await refreshedEdit.flush();
assert.equal(refreshedBase, 'head-authoritative', 'an authoritative clean refresh becomes the next edit CAS base');

const failures = createBufferRegistry({ storage:null });
const failed = failures.acquire(handle, { body:'x' }, { save:async () => false });
failed.apply({ body:'y' });
assert.equal(await failed.flush(), false);
assert.equal(failed.state().dirty, true);
const partial = failures.acquire({ ...handle, key:{ ...handle.key, resourceId:'partial' } }, { body:'x' }, { save:async () => ({ outcome:'partial' }) });
partial.apply({ body:'y' });
assert.equal(await partial.flush(), false);
assert.equal(partial.state().dirty, true);
const malformed = failures.acquire({ ...handle, key:{ ...handle.key, resourceId:'malformed' } }, { body:'x' }, { save:async () => ({ outcome:'applied', revision:{ kind:'wrong', value:'bad' } }) });
malformed.apply({ body:'y' });
assert.equal(await malformed.flush(), false, 'malformed applied receipt cannot acknowledge');
assert.equal(malformed.state().dirty, true);

const scoped = createBufferRegistry({ storage:null });
const one = scoped.acquire(handle, { body:'one' }, { actorId:'actor-1', epoch:'epoch-1', save:async () => ({ outcome:'applied', revision:{ kind:'copalHead', value:'late' } }) });
const two = scoped.acquire(handle, { body:'two' }, { actorId:'actor-2', epoch:'epoch-1' });
assert.notEqual(one, two, 'same resource is isolated across actor scopes');
scoped.setScope('actor-1', 'epoch-2');
one.apply({ body:'late' });
assert.equal(await one.flush(), false, 'old epoch completion is refused');

const draftStorage = new Map();
const storage = { setItem:(key, value) => draftStorage.set(key, value), getItem:(key) => draftStorage.get(key) || null, removeItem:(key) => draftStorage.delete(key) };
const personalOne = createBufferRegistry({ storage });
const personalBuffer = personalOne.acquire(handle, { body:'accepted' }, { actorId:'acct-a:ws-a', epoch:1 });
personalBuffer.apply({ body:'personal draft' });
personalOne.persistDraft(personalBuffer);
const personalTwo = createBufferRegistry({ storage });
const recoveredPersonal = personalTwo.recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'acct-a:ws-a', epoch:1 });
assert.equal(recoveredPersonal.snapshot().envelope.body, 'personal draft', 'draft survives username/storage namespace change');
const newerHandle = { ...handle, revision:{ kind:'copalHead', value:'head-newer' } };
const conflictPersonal = personalTwo.recoverDraft(newerHandle, { envelope:{ body:'remote' }, actorId:'acct-a:ws-a', epoch:2, save:async () => { throw new Error('must not overwrite remote'); } });
assert.equal(conflictPersonal.state().status, 'conflict', 'old-base recovery is explicit conflict');
assert.equal((await conflictPersonal.flush()).outcome, 'conflict');

// A retry after an uncertain response reuses the immutable submitted action;
// only a new local edit or explicit conflict rebase creates another binding.
const replayCalls = [];
let replayAttempt = 0;
let replayMutations = 0;
let replayReceipt = null;
const replayRegistry = createBufferRegistry({ storage:null });
const replayBuffer = replayRegistry.acquire(handle, { body:'accepted' }, {
  save:async (snapshot) => {
    replayCalls.push(snapshot);
    replayAttempt += 1;
    if (replayReceipt) return replayReceipt;
    replayMutations += 1;
    replayReceipt = { outcome:'applied', revision:{ kind:'copalHead', value:'head-replay' }, actionId:snapshot.actionId };
    if (replayAttempt === 1) throw new Error('response lost after commit');
    return replayReceipt;
  },
});
replayBuffer.apply({ body:'replay me' });
assert.equal(await replayBuffer.flush(), false);
assert.equal(replayBuffer.state().dirty, true);
assert.equal((await replayBuffer.flush())?.outcome, 'applied');
assert.equal(replayCalls.length, 2);
assert.equal(replayMutations, 1, 'response-loss replay must not duplicate the committed mutation');
assert.equal(replayCalls[0].actionId, replayCalls[1].actionId, 'response-loss retry preserves action identity');
assert.equal(replayCalls[0].expectedRevision.value, replayCalls[1].expectedRevision.value);

const refreshedRegistry = createBufferRegistry({ storage });
assert.equal(refreshedRegistry.recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'refresh-owner', epoch:1 }), null);
const refreshed = refreshedRegistry.acquire(newerHandle, { body:'remote' }, { actorId:'refresh-owner', epoch:1 });
assert.equal(refreshed.snapshot().expectedRevision.value, 'head-newer', 'a clean recovery probe cannot pin a later edit to an obsolete head');
assert.equal(refreshed.snapshot().envelope.body, 'remote');

const quotaStorage = { ...storage, setItem:() => { throw new Error('quota'); } };
const quotaRegistry = createBufferRegistry({ storage:quotaStorage });
const quotaBuffer = quotaRegistry.acquire(handle, { body:'accepted' }, { actorId:'acct-a:ws-a', epoch:1 });
quotaBuffer.apply({ body:'newer memory draft' });
assert.equal(quotaRegistry.persistDraft(quotaBuffer), false);
assert.match(quotaBuffer.state().recoveryError.message, /quota/);
quotaRegistry.invalidateAll();
assert.equal(quotaRegistry.recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'acct-b:ws-a', epoch:2 }), null, 'memory fallback cannot cross actors');
const quotaRecovered = quotaRegistry.recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'acct-a:ws-a', epoch:3 });
assert.equal(quotaRecovered.envelope.body, 'newer memory draft', 'failed persistence supersedes older durable draft in the same session');
quotaStorage.setItem = storage.setItem;
assert.equal(quotaRegistry.persistDraft(quotaRecovered), true);
assert.equal(quotaRecovered.state().recoveryError, null);
assert.equal(createBufferRegistry({ storage }).recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'acct-a:ws-a', epoch:4 }).envelope.body, 'newer memory draft');
quotaStorage.removeItem = () => { throw new Error('cleanup denied'); };
assert.equal(quotaRegistry.discardDraft(quotaRecovered), false);
quotaRegistry.invalidateAll();
assert.equal(quotaRegistry.recoverDraft(handle, { envelope:{ body:'accepted' }, actorId:'acct-a:ws-a', epoch:5 }), null, 'failed cleanup cannot reintroduce a stale draft in the same session');

console.log('copal resource/buffer tests passed');
