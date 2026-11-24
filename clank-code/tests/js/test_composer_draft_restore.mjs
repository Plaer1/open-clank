import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../../static/js/composerDraftRestore.js', import.meta.url), 'utf8');
const { restoreValidatedComposerDraft: restore } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
function fixture() {
  let state = { sid: 's1', epoch: 0, version: 1, text: '/undo', attachments: null, pending: [] };
  const expected = { ...state, pending: [...state.pending] };
  let committed;
  return { expected, get state(){return state;}, set state(value){state=value;}, apply(receipt){return restore(receipt,{sid:'s1',expected,read:()=>state,commit:value=>{committed=value;}});}, get committed(){return committed;} };
}
const receipt = { available:true, raw_content:'{"literal":"pasted JSON"}', content_kind:'text', attachment_refs:[{type:'attachment_ref',attachment_id:'upload1'}] };
test('exact text and validated upload IDs commit together without parsing JSON',()=>{const f=fixture();assert.equal(f.apply(receipt).restored,true);assert.deepEqual(f.committed,{text:receipt.raw_content,attachmentIds:['upload1']});});
test('missing or malformed attachment keeps entire previous draft',()=>{for(const value of [{...receipt,available:false},{...receipt,attachment_refs:[{url:'/raw/native/path'}]},{...receipt,raw_content:undefined}]){const f=fixture();assert.equal(f.apply(value).restored,false);assert.equal(f.committed,undefined);}});
test('new typed draft, away-and-back chat, carried or staged attachment change prevents overwrite',()=>{for(const change of [{text:'new draft'},{version:2},{sid:'s2'},{epoch:2},{attachments:[]},{pending:[{}]}]){const f=fixture();f.state={...f.state,...change};assert.equal(f.apply(receipt).restored,false);assert.equal(f.committed,undefined);}});
test('same-ID refresh does not invalidate unchanged draft and duplicate upload IDs are carried once',()=>{const f=fixture();f.state={...f.state};assert.equal(f.apply({...receipt,attachment_refs:[...receipt.attachment_refs,...receipt.attachment_refs]}).restored,true);assert.deepEqual(f.committed.attachmentIds,['upload1']);});
