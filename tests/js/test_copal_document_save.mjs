import assert from 'node:assert/strict';
import { prepareDocumentSave, commitDocumentSave, sameSaveScope } from '../../static/js/copal/documentSave.js';
import { createBufferRegistry } from '../../static/js/copal/documentBuffers.js';

const resource = {
  key:{ accountId:'owner-a', workspaceId:'study', provider:'copal', resourceId:'opaque-id' },
  revision:{ kind:'copalHead', value:'h1' }, representation:'nativeNote',
  locator:{ displayName:'Example', locationLabel:'Example' }, capabilities:{ read:true, edit:true },
};
const doc = { id:'internal-id', kind:'note', head:'h1', resource, text:'initial', properties:{ priority:0 }, relations:[] };
let serverHead = 'h1';
const writes = [];
let releaseFirst;
const buffer = createBufferRegistry({ storage:null }).acquire(resource, doc, {
  save:async (snapshot) => commitDocumentSave(prepareDocumentSave(doc, '', { snapshot }), async (id, payload) => {
    assert.equal(payload.base, serverHead, 'each serialized save uses the authoritative previous receipt');
    writes.push(structuredClone(payload));
    if (writes.length === 1) await new Promise((resolve) => { releaseFirst = resolve; });
    serverHead = `h${writes.length + 1}`;
    return { outcome:'committed', actionId:payload.actionId, doc:{ ...doc, head:serverHead, text:payload.content, properties:payload.properties } };
  }),
});
buffer.apply({ text:'same body', properties:{ priority:1 }, relations:[{ origin:'explicit', target:'one' }] });
const drained = buffer.flush();
buffer.apply({ text:'same body', properties:{ priority:2 }, relations:[{ origin:'explicit', target:'two' }] });
doc.properties.priority = 999;
releaseFirst();
await drained;
assert.deepEqual(writes.map((write) => [write.content, write.properties.priority, write.relations[0].target]), [
  ['same body', 1, 'one'], ['same body', 2, 'two'],
]);
assert.equal(buffer.state().dirty, false);
buffer.apply({ text:'third', properties:{ priority:3 }, relations:[] });
await buffer.flush();
assert.equal(writes[2].base, 'h3');
assert.equal(writes[2].content, 'third');

const prepared = prepareDocumentSave({ ...doc, kind:'wiki' }, 'wiki body');
assert.equal(prepared.payload.properties.priority, 999, 'Wiki sends typed native fields too');
assert.throws(() => prepareDocumentSave(doc, '', { snapshot:{ ...buffer.snapshot(), key:{ ...resource.key, resourceId:'another' } } }), /another resource/);
for (const result of [false, undefined, { outcome:'partial', doc:{ head:'x' } }, { outcome:'committed' }, { doc:{ head:'x' } }]) {
  await assert.rejects(() => commitDocumentSave(prepared, async () => result), /did not confirm/);
}
const conflict = await commitDocumentSave(prepared, async () => ({ outcome:'stale', doc:{ text:'remote', head:'h9' } }));
assert.equal(conflict.outcome, 'conflict');
assert.equal(conflict.remote.head, 'h9');
const projected = await commitDocumentSave(prepared, async () => ({
  outcome:'committed', actionId:prepared.actionId,
  doc:{ ...doc, head:'h10', text:'projected' },
  projections:{ tasks:{ status:'pending', generation:'g10' } },
}));
assert.equal(projected.outcome, 'applied');
assert.equal(projected.actionId, prepared.actionId);
assert.deepEqual(projected.projections.tasks, { status:'pending', generation:'g10' }, 'derived projection state travels separately from the durable receipt');
await assert.rejects(() => commitDocumentSave(prepared, async () => ({ outcome:'committed', actionId:'wrong-action', doc:{ ...doc, head:'h11' } })), /action identity/);
await assert.rejects(() => commitDocumentSave(prepared, async () => ({ outcome:'committed', doc:{ ...doc, head:'h11' } })), /action identity/);
await assert.rejects(() => commitDocumentSave(prepared, async () => ({ outcome:'committed', actionId:prepared.actionId, doc:{ ...doc, id:'other-id', head:'h11' } })), /document identity/);
await assert.rejects(() => commitDocumentSave(prepared, async () => ({ outcome:'committed', actionId:prepared.actionId, doc:{ ...doc, id:undefined, documentId:undefined, head:'h11' } })), /document identity/);
await assert.rejects(() => commitDocumentSave(prepared, async () => { throw new Error('offline'); }), /offline/);

const scope = { workspace:'study', accountId:'account-a', storageNamespace:'user:oldname', epoch:1 };
assert(sameSaveScope(scope, { ...scope }));
assert(!sameSaveScope(null, scope));
assert(!sameSaveScope(scope, null));
for (const patch of [{ workspace:'other' }, { accountId:'account-b' }, { epoch:2 }]) assert(!sameSaveScope(scope, { ...scope, ...patch }));
console.log('Copal document save integration passed');
