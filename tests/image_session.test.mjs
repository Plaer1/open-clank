import { test } from 'bun:test';
import { strict as assert } from 'node:assert';
import { normalizeResourceHandle } from '../static/js/copal/resourceModel.js';
import { createBufferRegistry } from '../static/js/copal/documentBuffers.js';
import { createImageSessionRegistry } from '../static/js/editor/imageSession.js';
import { normalizeNotesWorkspace, openWorkspaceImage, refreshWorkspaceImage, serializeNotesWorkspace, restoreWorkspaceImageSessions, workspaceGroups, workspaceLeaves, closeWorkspaceLeaf } from '../static/js/copal/notesWorkspace.js';

test('image identity, split views and v3 recovery remain separate from text buffers', async () => {
  const scope={accountId:'account-a',workspaceId:'workspace-a'};
  const key={...scope,provider:'files',resourceId:'image-a'};
  const revision={kind:'imageSha256',value:'sha256:'+'a'.repeat(64)};
  const handle=normalizeResourceHandle({key,revision,representation:'image',locator:{displayName:'Image',locationLabel:'Files',opaqueRef:'rr.opaque'},capabilities:{read:true}});
  assert.equal(handle.key.provider,'files');
  assert.throws(() => normalizeResourceHandle({...handle,representation:'text'}), /image representation/);
  assert.throws(() => createBufferRegistry({storage:null}).acquire(handle,{body:'not pixels'}), /image sessions/);

  const registry=createImageSessionRegistry();
  const session=registry.open({scope,sourceKey:key,sourceRevision:revision,opaqueRef:'rr.opaque',policyGeneration:7,
    sourceDigest:'a'.repeat(64),projectId:'project-a',projectRevision:4,draftId:'draft-a'});
  assert.equal(registry.open({scope,sourceKey:key,sourceRevision:revision}),session);
  assert.notEqual(registry.open({scope:{...scope,accountId:'account-b'},sourceKey:{...key,accountId:'account-b'},sourceRevision:revision}),session);
  assert.throws(() => registry.open({scope,sourceKey:key,sourceRevision:{...revision,value:'different'}}), /revision changed/);
  assert.throws(() => registry.open({scope,sourceKey:key,sourceRevision:revision,projectId:'other-project'}), /metadata conflicts/);
  assert.equal(session.isDirty(),null);
  assert.equal((await session.save()).reason,'image_runtime_pending');
  assert.equal((await session.beforeClose()).status,'unavailable');
  const saved=session.serializeRecovery();
  assert.equal(saved.projectRevision,4); assert.equal(saved.sourceRevision.kind,'imageSha256');
  assert.throws(() => registry.open({scope,opaqueRef:'data:image/png;base64,AA=='}), /opaqueRef/);

  const textDoc={id:'text-a',kind:'note',name:'Note',text:'hello'};
  const workspace=normalizeNotesWorkspace(null,[textDoc],'text-a');
  const first=openWorkspaceImage(workspace,session,{intent:'newTab'});
  const second=openWorkspaceImage(workspace,session,{intent:'splitRight'});
  assert.equal(first.image.sessionId,second.image.sessionId);
  assert.notEqual(first.image.viewId,second.image.viewId);
  assert.equal(session.mountView({viewId:first.image.viewId,zoom:1}).status,'unavailable');
  session.mountView({viewId:second.image.viewId,zoom:2});
  assert.equal(session.setVisible(first.image.viewId,true),true);
  assert.equal(session.setViewZoom(second.image.viewId,3),true);
  assert.equal(session.viewState(first.image.viewId).zoom,1);
  assert.equal(session.viewState(second.image.viewId).zoom,3);
  const closed=closeWorkspaceLeaf(workspace,first.id);
  assert.equal(session.releaseView(closed.image.viewId),true);
  assert.equal(session.releaseView(closed.image.viewId),false);
  assert.equal(session.viewState(second.image.viewId).zoom,3);
  assert.equal((await registry.saveAll()).filter(item=>item.sessionId===session.sessionId).length,1);
  const serialized=serializeNotesWorkspace(workspace);
  assert(!serialized.includes('data:image'));
  const restored=normalizeNotesWorkspace(JSON.parse(serialized),[textDoc]);
  assert.equal(workspaceGroups(restored).length,2);
  assert(workspaceLeaves(restored).some(leaf=>leaf.docId==='text-a'));
  assert.equal(workspaceLeaves(restored).find(leaf=>leaf.image)?.image.recovery.projectId,'project-a');
  const restoredSessions=createImageSessionRegistry();
  restoreWorkspaceImageSessions(restored,restoredSessions);
  assert.equal(restoredSessions.all().length,1);
  assert.equal(restoredSessions.get(session.sessionId).captureTarget().projectRevision,4);
  const legacy={version:3,root:{type:'group',id:'g',activeLeafId:'l',tabs:[{type:'leaf',id:'l',docId:'text-a',mode:'source',pinned:true,scrollTop:42}]},activeLeafId:'l'};
  const legacyLeaf=workspaceLeaves(normalizeNotesWorkspace(legacy,[textDoc]))[0];
  assert.equal(legacyLeaf.id,'l'); assert.equal(legacyLeaf.pinned,true); assert.equal(legacyLeaf.scrollTop,42);

  const pending=registry.open({scope});
  const pending2=registry.open({scope});
  assert.notEqual(pending.sessionId,pending2.sessionId);
  const pendingLeaf=openWorkspaceImage(workspace,pending,{intent:'newTab'});
  assert.throws(() => pending.bindManagedReceipt({key:{...key,resourceId:'pending-image'}}), /receiptId/);
  pending.bindManagedReceipt({receiptId:'receipt-a',key:{...key,resourceId:'pending-image'},revision:{kind:'filesImageRevision',value:'5'}});
  refreshWorkspaceImage(workspace,pending);
  assert.equal(pendingLeaf.image.recovery.sourceKey.resourceId,'pending-image');
  assert.equal(registry.open({scope,sourceKey:{...key,resourceId:'pending-image'},sourceRevision:{kind:'filesImageRevision',value:'5'}}),pending);
  const future={version:99,root:{type:'group',tabs:[]},futureField:{value:'preserve'}};
  assert.deepEqual(JSON.parse(serializeNotesWorkspace(normalizeNotesWorkspace(future,[textDoc]))),future);
  const futureLeaf={version:3,root:{type:'group',tabs:[{type:'leaf',docId:'future',image:{version:2,recovery:{version:2}}}]}};
  assert.deepEqual(JSON.parse(serializeNotesWorkspace(normalizeNotesWorkspace(futureLeaf,[textDoc]))),futureLeaf);
  assert.equal(session.dispose(),true); assert.equal(session.dispose(),false);
});

test('two registered views share history and retire independently', async () => {
  const {createEditorState,createImageSceneAuthority}=await import('../static/js/editor/state.js');
  const registry=createImageSessionRegistry(), session=registry.open({scope:{accountId:'a',workspaceId:'w'}}), state=createEditorState(), scene=createImageSceneAuthority({state});
  let value=1, aUpdates=0,bUpdates=0, aDisposed=0,bDisposed=0;
  scene.installHistory({state,capture:()=>({value}),restore:snapshot=>{value=snapshot.value;}});session.attachSceneAuthority(scene);
  const offA=scene.subscribeView(()=>aUpdates++),offB=scene.subscribeView(()=>bUpdates++);
  const a={capabilities:()=>({runtime:true}),undo:()=>scene.undo(),redo:()=>scene.redo(),dispose(){aDisposed++;offA();},isEditorOpen:()=>true,isSceneReady:()=>true};
  const b={capabilities:()=>({runtime:true}),undo:()=>scene.undo(),redo:()=>scene.redo(),dispose(){bDisposed++;offB();},isEditorOpen:()=>true,isSceneReady:()=>true};
  const ra=session.attachRuntime(a,{viewId:'a'}), rb=session.attachRuntime(b,{viewId:'b'});
  assert.equal(session.capabilities().reason,'image_view_required');assert.equal(session.undo().status,'unavailable');
  assert.equal(session.capabilities('a').runtime,true);scene.history.save('edit');value=2;scene.markChanged();assert.equal(aUpdates,1);assert.equal(bUpdates,1);
  assert.equal(session.undo('b'),true);assert.equal(value,1);assert.equal(aUpdates,2);assert.equal(bUpdates,2);
  a.dispose();assert.equal(ra.detach(),true);assert.equal(session.undo('a').status,'unavailable');assert.equal(session.redo('b'),true);assert.equal(value,2);assert.equal(aUpdates,2);assert.equal(bUpdates,3);
  assert.equal(rb.detach(),true);b.dispose();assert.equal(aDisposed,1);assert.equal(bDisposed,1);assert.equal(session.dispose(),true);
});

test('kept blank draft survives closed layout and reopens with the same identity', async () => {
  const {retainClosedImageRecovery,openWorkspaceDocument}=await import('../static/js/copal/notesWorkspace.js');
  const {closeImageTransaction}=await import('../static/js/editor/imageCloseTransaction.js');
  const registry=createImageSessionRegistry(), session=registry.open({scope:{accountId:'alice',workspaceId:'w'},draftId:'draft-1'}), recovery=session.serializeRecovery();
  const workspace=normalizeNotesWorkspace(null,[]), leaf=openWorkspaceImage(workspace,session,{intent:'newTab'});
  let committed=0;
  const image={prepareClose:async()=>({status:'prepared'}),validateClose:()=>true,commitClose:()=>{committed++;return true;}};
  const ok=await closeImageTransaction({images:[image],choose:async()=> 'keep_draft',resolveOthers:async()=>true,isCurrent:()=>true,onCommitted:(_image,choice)=>{if(choice==='keep_draft') assert.equal(retainClosedImageRecovery(workspace,recovery),true);}});
  assert.equal(ok,true);assert.equal(committed,1);closeWorkspaceLeaf(workspace,leaf.id);
  const stored=JSON.parse(serializeNotesWorkspace(workspace));assert.equal(stored.closedImageRecovery[leaf.docId].draftId,'draft-1');assert.equal(stored.closedImageRecovery[leaf.docId].sourceKey,null);
  const restored=normalizeNotesWorkspace(stored,[]);assert.equal(restored.closed[0],leaf.docId);
  const restoredRecovery=restored.closedImageRecovery[leaf.docId], reopened=openWorkspaceDocument(restored,{id:leaf.docId,kind:'image-session',imageRecovery:restoredRecovery},{intent:'newTab'});
  assert.equal(reopened.image.sessionId,session.sessionId);assert.equal(reopened.image.recovery.draftId,'draft-1');assert.equal(reopened.image.recovery.sourceDigest,null);
});

test('concurrent blank admission reuses one entry and closed history evicts metadata', async () => {
  const {admitRecoveredBlank}=await import('../static/js/editor/imageSession.js');
  const {retainClosedImageRecovery}=await import('../static/js/copal/notesWorkspace.js');
  const registry=createImageSessionRegistry(), original=registry.open({scope:{accountId:'a',workspaceId:'w'},draftId:'draft'}), saved=original.serializeRecovery(), recoveredRegistry=createImageSessionRegistry(), entries=new Map();
  let release;const gate=new Promise(resolve=>{release=resolve;});
  const admit=async()=>{await gate;return admitRecoveredBlank(entries,recoveredRegistry,saved,'a:w:epoch');};
  const one=admit(),two=admit();release();const [a,b]=await Promise.all([one,two]);assert.equal(a,b);assert.equal(a.session,b.session);assert.equal(entries.size,1);assert.equal(recoveredRegistry.all().length,1);assert.throws(()=>admitRecoveredBlank(entries,recoveredRegistry,saved,'other'),/conflicts/);
  const workspace=normalizeNotesWorkspace(null,[]);
  for (let i=0;i<20;i++) {const session=registry.open({scope:{accountId:'a',workspaceId:'w'},draftId:`draft-${i}`});assert.equal(retainClosedImageRecovery(workspace,session.serializeRecovery()),true);}
  assert.equal(Object.keys(workspace.closedImageRecovery).length,20);
  const oldest=workspace.closed[workspace.closed.length-1], text={id:'ordinary',kind:'note',name:'Note',text:''}, leaf=openWorkspaceDocumentForTest(workspace,text);
  closeWorkspaceLeaf(workspace,leaf.id);assert.equal(workspace.closed.length,20);assert.equal(workspace.closedImageRecovery[oldest],undefined);assert.equal(Object.keys(workspace.closedImageRecovery).length,19);
});

function openWorkspaceDocumentForTest(workspace,doc) { return workspaceOpenDocument(workspace,doc,{intent:'newTab'}); }
import { openWorkspaceDocument as workspaceOpenDocument } from '../static/js/copal/notesWorkspace.js';
