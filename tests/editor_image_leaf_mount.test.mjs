import {test,expect} from 'bun:test';
import {dispatchFilesDestination} from '../static/js/copal/resourceDestinations.js';
import {createImageSessionRegistry} from '../static/js/editor/imageSession.js';

test('opt-in image dispatch retains one session and retires its runtime', async () => {
  const registry=createImageSessionRegistry(), scope={accountId:'a',workspaceId:'w'};
  const session=registry.open({scope,sourceKey:{...scope,provider:'files',resourceId:'image'},sourceRevision:{kind:'imageSha256',value:'sha256:'+'a'.repeat(64)},opaqueRef:'ref',sourceDigest:'a'.repeat(64)});
  let disposed=0, visible=true;
  const binding=session.attachRuntime({capabilities:()=>({runtime:true}),isDirty:()=>true,beforeClose:async choice=>({status:choice==='cancel'?'cancelled':'closed'}),dispose:()=>{disposed++;},setVisible:value=>{visible=value;}});
  const opened={target:{app:'imps'},resource:{ref:'ref',provider:'files'}};
  expect(await dispatchFilesDestination(opened,{surface:'editor',openImage:(_response,ref)=>ref})).toBe('ref');
  expect((await session.beforeClose('cancel')).status).toBe('cancelled');
  expect(visible).toBe(true);
  expect(session.dispose()).toBe(true); expect(disposed).toBe(1); expect(binding.detach()).toBe(false);
});

test('coordinated close leaves both images intact on cancellation or text failure', async () => {
  const {closeImageTransaction}=await import('../static/js/editor/imageCloseTransaction.js');
  let commits=0, revision=1, choices=['discard','cancel'];
  const makeImage=()=>({prepareClose:async()=>({status:'prepared',revision}),validateClose:token=>token.revision===revision,commitClose:()=>{commits++;}});
  const images=[makeImage(),makeImage()];
  const close=(resolveOthers)=>closeImageTransaction({images,choose:async()=>choices.shift(),resolveOthers,isCurrent:()=>true});
  expect(await close(async()=>true)).toBe(false); expect(commits).toBe(0);
  choices=['discard','discard']; expect(await close(async()=>false)).toBe(false); expect(commits).toBe(0);
  choices=['discard','discard']; expect(await close(async()=>{revision++;return true;})).toBe(false); expect(commits).toBe(0);
  choices=['discard','discard']; expect(await close(async()=>true)).toBe(true); expect(commits).toBe(2);
});

test('cancelled Save As preserves all open leaves without an error notice', async () => {
  const {closeImageTransaction}=await import('../static/js/editor/imageCloseTransaction.js');
  const image={prepareClose:async()=>({status:'cancelled'})};
  const unexpected=()=>{throw Error('Unexpected change after cancellation');};
  expect(await closeImageTransaction({images:[image],choose:async()=>'save',resolveOthers:unexpected,isCurrent:()=>true,
    stageLayout:unexpected,onBlocked:unexpected})).toBe(false);
});

test('Save All captures each dirty session once despite duplicate leaves', async () => {
  const {saveDistinctImageSessions}=await import('../static/js/editor/imageSession.js');
  let calls=0, finish, current='a'; const gate=new Promise(resolve=>{finish=resolve;});
  const a={isDirty:()=>true,save:async()=>{calls++;await gate;return 'saved-a';}}, b={isDirty:()=>false,save:async()=>{throw Error('clean session saved');}};
  const pending=saveDistinctImageSessions([a,a,b]); current='b'; finish();
  const results=await pending; expect(calls).toBe(1); expect(results.map(result=>result.value)).toEqual(['saved-a']); expect(current).toBe('b');
});

test('view close keeps sibling and rejects replaced runtime before last-view commit', async () => {
  const {captureImageCloseTargets,closeImageTransaction}=await import('../static/js/editor/imageCloseTransaction.js');
  let prepares=0,commits=0; const session={sessionId:'s',isDirty:()=>true,getPersistenceController:()=>null,prepareClose:async (_choice,viewId)=>(prepares++,{status:'prepared',viewId}),validateClose:()=>true,commitClose:()=>{commits++;return true;}};
  const a={runtime:{}},b={runtime:{}},image={session,views:new Map([['a',a],['b',b]])},map=new Map([['doc',image]]), leaves=[{id:'la',docId:'doc',image:{sessionId:'s',viewId:'a'}},{id:'lb',docId:'doc',image:{sessionId:'s',viewId:'b'}}];
  const selected=ids=>leaves.filter(leaf=>ids.includes(leaf.id)).map(leaf=>({id:leaf.id,docId:leaf.docId,viewId:leaf.image.viewId}));
  const one=captureImageCloseTargets({leaves,selected:selected(['la']),imageMap:map});expect(one.images.length).toBe(0);expect(one.isCurrent(leaves,map)).toBe(true);expect(b.runtime).toBeTruthy();
  const all=captureImageCloseTargets({leaves,selected:selected(['la','lb']),imageMap:map});expect(all.images.length).toBe(1);
  const result=await closeImageTransaction({images:all.images,choose:async()=> 'discard',resolveOthers:async()=>{b.runtime={};return true;},isCurrent:()=>all.isCurrent(leaves,map)});
  expect(result).toBe(false);expect(prepares).toBe(1);expect(commits).toBe(0);expect(a.runtime).toBeTruthy();expect(b.runtime).toBeTruthy();
});

test('exact image ref routing rejects changed account before admission', async () => {
  const {openScopedImageRef,openExactDestination}=await import('../static/js/copal/resourceDestinations.js');
  let scope={accountId:'a',storageNamespace:'owner-a',workspace:'w',epoch:1},ticket=1,admissions=0,release;
  const options={captureScope:()=>({...scope}),sameScope:(a,b)=>JSON.stringify(a)===JSON.stringify(b),activateEditor:()=>new Promise(resolve=>{release=resolve;}),navigationCurrent:value=>value===ticket,openImage:async(ref,guard)=>{admissions++;expect(ref).toBe('opaque-ref');expect(guard.expectedApp).toBe('imps');expect(guard.isCurrent()).toBe(true);return 'opened';}};
  const pending=openScopedImageRef('opaque-ref',options);scope={...scope,accountId:'b'};release(1);await expect(pending).rejects.toThrow('account or Editor view changed');expect(admissions).toBe(0);
  scope={...scope,accountId:'a'};const good=openScopedImageRef('opaque-ref',options);release(1);expect(await good).toBe('opened');expect(admissions).toBe(1);
  const old=globalThis.copalModule;globalThis.copalModule={openImageResource:async ref=>ref};try {expect(await openExactDestination('imps','opaque-ref')).toBe('opaque-ref');} finally {globalThis.copalModule=old;}
});
