import {test, expect} from 'bun:test';
import {createImagePersistence} from '../static/js/editor/imagePersistence.js';

test('inpaint sidecars round trip and history owns their immutable pixels', async () => {
  const {serializeEditableImageScene,decodeInpaintSidecar}=await import('../static/js/editor/imageSessionPersistence.js');
  const {captureImageScene,restoreImageScene}=await import('../static/js/editor/imageScene.js');
  const {createEditorState}=await import('../static/js/editor/state.js');
  const png='data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/aS8AAAAASUVORK5CYII=';
  let encodes=0; const doc={createElement(){const canvas={width:1,height:1,pixel:'pixel',ownerDocument:doc,toDataURL(){encodes++;return png;}}; canvas.getContext=()=>({drawImage(source){canvas.pixel=source.pixel;},getImageData(){return {pixel:canvas.pixel};},putImageData(data){canvas.pixel=data.pixel;}});return canvas;}};
  class ImageStub {constructor(){this.naturalWidth=1;this.naturalHeight=1;this.pixel='pixel';} set src(_value){queueMicrotask(()=>this.onload());}}
  const state=createEditorState(), layerCanvas=doc.createElement('canvas'), source=Object.freeze({ai:doc.createElement('canvas'),base:doc.createElement('canvas'),mask:doc.createElement('canvas'),padPx:20});
  state.imgWidth=1; state.imgHeight=1; state.layers=[{id:'l',name:'L',canvas:layerCanvas,ctx:layerCanvas.getContext('2d'),visible:true,opacity:1,locked:false,masks:[],inpaintSource:source}]; state.activeLayerId='l'; state.lastInpaintLayerId='l'; state.layerOffsets.set('l',{x:0,y:0});
  const encoded=serializeEditableImageScene(state), decoded=await decodeInpaintSidecar(encoded.layers[0].inpaintSource,doc,ImageStub);
  expect(encoded.layers[0].inpaintSource.v).toBe(1); expect(decoded.mask.width).toBe(1); expect(encoded.lastInpaintLayerId).toBe('l');
  state.layers[0].inpaintSource=decoded; encodes=0; const snap=captureImageScene(state); captureImageScene(state); expect(encodes).toBe(0);
  decoded.ai.pixel='changed'; restoreImageScene(state,snap,doc); expect(state.layers[0].inpaintSource.ai.pixel).toBe('pixel'); expect(state.lastInpaintLayerId).toBe('l');
  expect(await decodeInpaintSidecar({},doc,ImageStub)).toBeNull();
  await expect(decodeInpaintSidecar({v:1,padPx:20},doc,ImageStub)).rejects.toThrow('Inpaint sidecar dimensions exceed the save limit');
  const sidecar=encoded.layers[0].inpaintSource;
  await expect(decodeInpaintSidecar({...sidecar,ai:{...sidecar.ai,dataUrl:null}},doc,ImageStub)).rejects.toThrow('Invalid inpaint sidecar');
  state.layers[0].inpaintSource=null; state.layers[0]._editableExtras={inpaintSource:encoded.layers[0].inpaintSource};
  expect(serializeEditableImageScene(state).layers[0].inpaintSource).toBeUndefined();
  state.layers[0]._editableExtras={inpaintSource:{v:9,future:'preserved'}};
  expect(serializeEditableImageScene(state).layers[0].inpaintSource).toEqual({v:9,future:'preserved'});
});

test('adapter rejects mismatched source and retains exact delayed save origin', async () => {
  const png=Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC','base64');
  const imageBytes=new Blob([png],{type:'image/png'});
  const digest='sha256:'+Buffer.from(await crypto.subtle.digest('SHA-256',png)).toString('hex');
  const target={sessionId:'a',scope:{accountId:'alice',workspaceId:'w'},sourceKey:{accountId:'alice',workspaceId:'w',provider:'files',resourceId:'stable-a'},opaqueRef:'ref-a',sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,projectId:'project-a',projectRevision:1};
  let current=target, wrong=true;
  const session={captureTarget:()=>current};
  const files={openResource:async()=>({target:{app:'imps'},resource:{id:wrong?'other':'stable-a',ref:'ref-a',provider:'files',revision:{kind:'imageSha256',value:digest},capabilities:['stat','open','preview','download']},payload:{provider:'files',resource_id:'image-a',read_only:false}}),contentUrl:()=>'/content'};
  const drafts=[];
  const beforeIssue=async operation=>{drafts.push(operation); return current;};
  const response=(revision,id)=>({ok:true,json:async()=>({
    project_revision:revision,
    lore_receipt:{action_id:id,outcome:'committed',history_status:'complete'},
    refresh_receipt:{image_revision:digest,image:{provider:'files',resource_id:'image-a'}}
  })});
  const content=()=>new Response(imageBytes);
  const issued=Promise.withResolvers(), pending=Promise.withResolvers();
  const fetchImpl=async (url,opts)=>{
    if (url==='/content') return content();
    issued.resolve(JSON.parse(opts.body).operation_id);
    return pending.promise;
  };
  const deps={session,authority:{validate:()=>true,validateScope:()=>true},filesClient:files};
  const adapter=createImagePersistence({...deps,fetchImpl});
  const accepted=[];
  adapter.bindReceipt(result=>accepted.push(result));
  await expect(adapter.openSource()).rejects.toThrow('identity mismatch'); wrong=false;
  const args={target,state:{layers:[]},imageBytes,name:'A',width:1,height:1,beforeIssue};
  const save=adapter.save(args);
  const id=await issued.promise;
  expect(drafts).toEqual([{operationId:id,kind:'save',target}]);
  current={...target,sessionId:'b'};
  pending.resolve(response(2,id));
  await expect(save).rejects.toThrow('retired origin');
  expect(accepted).toHaveLength(0);
  expect(adapter.uncertain).toBe(true);

  current=target;
  const bodies=[], firstIssued=Promise.withResolvers(), firstReply=Promise.withResolvers();
  const serial=createImagePersistence({...deps,fetchImpl:async (url,opts)=>{
    if (url==='/content') return content();
    const body=JSON.parse(opts.body); bodies.push(body);
    if (bodies.length===1) {firstIssued.resolve(body.operation_id); return firstReply.promise;}
    return response(3,body.operation_id);
  }});
  serial.bindReceipt(result=>{current={...current,projectRevision:result.projectRevision,opaqueRef:result.opaqueRef,sourceRevision:result.sourceRevision,sourceDigest:result.sourceDigest};});
  const one=serial.save(args), two=serial.save(args);
  const firstId=await firstIssued.promise;
  expect(bodies).toHaveLength(1);
  firstReply.resolve(response(2,firstId));
  await Promise.all([one,two]);
  expect(bodies.map(x=>x.expected_project_revision)).toEqual([1,2]);
  expect(drafts.slice(1).map(x=>x.operationId)).toEqual(bodies.map(x=>x.operation_id));
  expect(current.projectRevision).toBe(3);
});

test('unknown copy import reconciles the same identity and preserves its source', async () => {
  const target={sessionId:'a',scope:{accountId:'alice',workspaceId:'w'},sourceKey:{provider:'files',resourceId:'source'},projectId:'source-project',draftId:null};
  let current=target, pending, imports=0, lookup=0, copyBody;
  const files={roots:async()=>({policy_generation:4,entries:[{provider:'files',kind:'provider_root',ref:'gallery',capabilities:['write']}]}),importFile:async()=>{imports++;throw new Error('lost response');},operationReceipt:async(id,{generation})=>{lookup++; return {operation_id:id,generation,items:[{item_id:pending.itemId,outcome:'committed',resource_ref:'copy-ref'}]};},openResource:async()=>({target:{app:'imps'},resource:{provider:'files',revision:{kind:'contentDigest',value:'digest'}},payload:{provider:'files',resource_id:'copy-id'}}),contentUrl:()=>''};
  const adapter=createImagePersistence({session:{captureTarget:()=>current},authority:{validate:()=>true,validateScope:()=>true},filesClient:files,cryptoImpl:{randomUUID:()=>crypto.randomUUID(),subtle:{digest:async()=>new Uint8Array(32).buffer}},fetchImpl:async(_url,opts)=>{copyBody=JSON.parse(opts.body); return {ok:true,json:async()=>({project_id:'copy-project',project_revision:1,lore_receipt:{action_id:copyBody.operation_id,outcome:'committed',history_status:'complete'}})};}});
  await expect(adapter.startCopy({target,state:{layers:[]},imageBytes:new Blob(['x']),name:'copy',width:1,height:1,persistPending:async value=>{pending=value; current={...current,draftId:'draft'};}})).rejects.toThrow('receipt reconciliation');
  expect(pending.filename).toBe('edited-'+pending.operationId.slice('imps-copy:'.length)+'.png');
  expect(pending.bindingPhase).toBe('unissued');
  const persistIssued=async next=>{expect(next).toEqual({...pending,bindingPhase:'issued'});};
  current={...current,opaqueRef:'fresh-authorized-ref'};
  const result=await adapter.retryCopy(pending,{layers:[]},persistIssued);
  expect(copyBody.operation_id).toBe(pending.bindingOperationId);
  expect([imports,lookup,copyBody.operation_key,result.result.project_id,current.sourceKey.resourceId]).toEqual([1,1,pending.operationId,'copy-project','source']);
  files.operationReceipt=async()=>{throw Object.assign(new Error('missing'),{status:404,code:'resource_unavailable'});};
  files.importFile=async(file,options)=>{expect([await file.text(),options.operationId,options.itemId,options.generation,options.destinationRef,options.name]).toEqual(['x',pending.operationId,pending.itemId,4,'gallery',pending.filename]); return {operation_id:pending.operationId,generation:4,items:[{item_id:pending.itemId,outcome:'committed',resource_ref:'copy-ref'}]};};
  expect((await adapter.retryCopy(pending,{layers:[]},persistIssued)).result.project_id).toBe('copy-project');
  current={...current,sourceRevision:{kind:'imageSha256',value:'changed'}};
  await expect(adapter.retryCopy(pending,{layers:[]},persistIssued)).rejects.toThrow('identity or authority changed');
});

test('session save accepts its exact receipt after view detach and keeps newer scene dirty', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'image'};
  const digest='sha256:'+'a'.repeat(64), revision={kind:'imageSha256',value:digest};
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:revision,sourceDigest:digest,opaqueRef:'old',projectId:'p',projectRevision:1});
  const scene={sceneRevision:1,layers:[{}],persistDirty:false}; let accept, release;
  const gate=new Promise(resolve=>{release=resolve;}), issued=Promise.withResolvers();
  const operationId='imps:'+crypto.randomUUID(), requests=[];
  const receipt={project_revision:2,lore_receipt:{action_id:operationId,outcome:'committed',history_status:'complete'}};
  const adapter={bindReceipt:callback=>{accept=callback;return ()=>{};},save:async({target,beforeIssue})=>{
    const expected=await beforeIssue({target,operationId,kind:'save'});
    issued.resolve(); await gate;
    const result={expected,key,opaqueRef:'new',projectId:'p',projectRevision:2,sourceRevision:revision,sourceDigest:digest,receipt};
    accept(result);return result;
  }};
  const fetchImpl=async(url,opts)=>{
    requests.push({url,method:opts.method,payload:JSON.parse(opts.body).payload});
    return Response.json({id:'draft-a'});
  };
  const controller=createImageSessionPersistence({session,scene,adapter,fetchImpl,authority:{validateScope:()=>true}});controller.setReceiptOwner(session.attachPersistenceController(controller));
  const view=session.attachRuntime({capabilities:()=>({}),dispose:()=>{}}), target=session.captureTarget();
  const saving=controller.save({target,stateDoc:{layers:[]},sceneRevision:1},new Blob(['pixels']));
  await issued.promise; view.detach(); scene.sceneRevision=2; release();
  expect(await saving).toBe(receipt);
  expect(requests.map(r=>[r.method,r.url])).toEqual([['POST','/api/editor-drafts'],['PUT','/api/editor-drafts/draft-a']]);
  expect(requests[0].payload._imageProjectOperation).toEqual({target,operationId,kind:'save',phase:'issued'});
  expect(requests[1].payload._imageProjectOperation).toBeUndefined();
  expect(scene.persistDirty).toBe(true);
  expect(session.captureTarget().projectRevision).toBe(2); expect(session.captureTarget().opaqueRef).toBe('new'); expect(controller.dirty).toBe(true); expect(controller.canDispose()).toBe(false);
});

test('quiescent reauthorization transfers one dirty session without partial registry adoption', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64), revision={kind:'imageSha256',value:digest};
  const oldRegistry=createImageSessionRegistry(), newRegistry=createImageSessionRegistry(), collision=createImageSessionRegistry();
  const session=oldRegistry.open({scope,sourceKey:key,sourceRevision:revision,sourceDigest:digest,opaqueRef:'old',projectId:'p',projectRevision:3});
  collision.open({scope,sourceKey:key,sourceRevision:revision,sourceDigest:digest,opaqueRef:'else'});
  const scene={sceneRevision:2,layers:[{}],managedProvider:'files',managedResourceId:'backend',imageRef:'old'}; let oldUnbound=0;
  const oldAdapter={save:async()=>{},bindReceipt:()=>()=>{oldUnbound++;}};
  let oldScope=true;
  const controller=createImageSessionPersistence({session,scene,adapter:oldAdapter,authority:{validateScope:()=>oldScope}}); controller.setReceiptOwner(session.attachPersistenceController(controller));
  const resultId=controller.reserveResult({kind:'ai',epoch:'original-epoch',generation:2}); controller.submitResult(resultId); controller.receiveResult(resultId,{bytes:'cGl4ZWxz',mime:'image/png',metadata:{label:'Result'}}); oldScope=false;
  const fresh={save:async()=>{},bindReceipt:()=>()=>{},openProject:async(_target,_source,opts)=>{expect(opts.adopt).toBe(false);return {id:'p',project_revision:3};},verifyCandidateSource:async()=>({resourceId:'backend'})};
  const authority={validateScope:()=>true,validate:()=>true}, candidate={...session.captureTarget(),opaqueRef:'fresh'};
  const proof=await controller.proveReauthorization({candidate,newAdapter:fresh,newAuthority:authority,isCurrent:()=>true});
  expect(()=>collision.transferFrom(oldRegistry,session,()=>controller.commitReauthorization(proof))).toThrow('conflicts');
  expect(session.captureTarget().opaqueRef).toBe('old'); expect(oldRegistry.get(session.sessionId)).toBe(session);
  expect(newRegistry.transferFrom(oldRegistry,session,()=>controller.commitReauthorization(proof))).toBe(session);
  expect(session.captureTarget().opaqueRef).toBe('fresh'); expect(scene.imageRef).toBe('fresh'); expect(controller.dirty).toBe(true); expect(oldUnbound).toBe(1);
  const retained=controller.readRetainedResult(resultId); expect([retained.bytes,retained.epoch,retained.generation]).toEqual(['cGl4ZWxz','original-epoch',2]); expect(controller.retainedResultStatus().durable).toBe(false);
  expect(oldRegistry.get(session.sessionId)).toBeNull(); expect(newRegistry.get(session.sessionId)).toBe(session);
  const close=await controller.prepareClose('discard'); expect(controller.commitToken(close)).toBe(true); expect(session.dispose()).toBe(true); expect(newRegistry.get(session.sessionId)).toBeNull();
});

test('retained result draft waits for the gesture and survives round trip', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const {createEditorState,createImageSceneAuthority}=await import('../static/js/editor/state.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64);
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,opaqueRef:'ref'});
  const state=createEditorState(), scene=createImageSceneAuthority({state}), calls=[];
  const adapter={save:async()=>{},bindReceipt:()=>()=>{}};
  const authority={validateScope:()=>true,validate:()=>true};
  const controller=createImageSessionPersistence({session,scene:state,adapter,authority,fetchImpl:async(_url,opts)=>{calls.push(JSON.parse(opts.body));return {ok:true,json:async()=>({id:'draft'})};}});
  controller.setReceiptOwner(session.attachPersistenceController(controller)); controller.setResultSceneAuthority(scene);
  const token=scene.beginGesture({},'stroke'), id=controller.reserveResult({kind:'ai',epoch:'epoch-a',generation:0});
  controller.submitResult(id); controller.receiveResult(id,{bytes:'cGl4ZWxz',mime:'image/png',metadata:{label:'result'}});
  await Promise.resolve(); expect(calls.length).toBe(0); expect(controller.canDispose()).toBe(false);
  scene.markChanged(); expect(scene.endGesture(token)).toBe(true);
  await new Promise(resolve=>setTimeout(resolve,0));
  expect(calls.length).toBe(1); expect(calls[0].payload._imageRetainedResults[0].bytes).toBe('cGl4ZWxz');
  expect(controller.retainedResultStatus().durable).toBe(true);
  await controller.saveDraft(controller.captureSnapshot()); expect(calls[1].payload._imageRetainedResults[0].bytes).toBe('cGl4ZWxz');
  const restored=createImageSessionPersistence({session,scene:state,adapter,authority}); restored.restoreResults(calls[1].payload._imageRetainedResults);
  expect(restored.readRetainedResult(id).bytes).toBe('cGl4ZWxz');
  const unknown=structuredClone(calls[1].payload._imageRetainedResults); unknown[0].status='submitted'; delete unknown[0].bytes; delete unknown[0].mime;
  const interrupted=createImageSessionPersistence({session,scene:state,adapter,authority,fetchImpl:async()=>({ok:true,json:async()=>({id:'draft'})})}); interrupted.setReceiptOwner({refresh(){}}); interrupted.restoreResults(unknown);
  expect(interrupted.retainedResultStatus().records[0].status).toBe('unknown');
  const discarded=await interrupted.prepareClose('discard'); expect(discarded.status).toBe('prepared'); expect(interrupted.validateToken(discarded)).toBe(true);
  const keep=await interrupted.prepareClose('keep_draft'); expect(keep.status).toBe('prepared'); expect(interrupted.commitToken(keep)).toBe(true);
  const invalid=createImageSessionPersistence({session,scene:state,adapter,authority}); expect(()=>invalid.restoreResults([{...unknown[0],extra:'forbidden'}])).toThrow('invalid');
});

test('blank session keeps null source, draft identity and owner-scoped remount', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const {createEditorState}=await import('../static/js/editor/state.js');
  const session=createImageSessionRegistry().open({scope:{accountId:'alice',workspaceId:'w'}}), state=createEditorState();
  const canvas={width:2,height:2,toDataURL:()=> 'data:image/png;base64,cGl4ZWxz'}, ctx={};state.imgWidth=2;state.imgHeight=2;state.layers=[{id:'l',name:'Layer',canvas,ctx,visible:true,opacity:1,masks:[]}];state.activeLayerId='l';state.sceneRevision=1;
  let allowed=true,body=null;const authority={validateScope:target=>allowed && target.scope.accountId==='alice',validate:target=>allowed && target.sessionId===session.sessionId && target.sourceKey===null};
  const adapter={bindReceipt:()=>()=>{},save:async()=>{throw new Error('must not overwrite');}};
  const controller=createImageSessionPersistence({session,scene:state,adapter,authority,fetchImpl:async(_url,options)=>{body=JSON.parse(options.body);return {ok:true,json:async()=>({id:'draft-1'})};}});
  controller.setReceiptOwner(session.attachPersistenceController(controller));
  expect((await session.save()).status).toBe('unavailable');expect((await controller.verifySource()).source).toBeNull();
  const close=await controller.prepareClose('keep_draft');expect(close.status).toBe('prepared');expect(session.captureTarget().draftId).toBe('draft-1');
  expect(body.payload._imageSessionRecovery.sessionId).toBe(session.sessionId);expect(body.payload._imageSessionRecovery.sourceKey).toBeNull();expect(body.payload._imageSessionRecovery.sourceDigest).toBeNull();
  allowed=false;await expect(controller.verifySource()).rejects.toThrow('authority');expect(controller.validateToken(close)).toBe(false);allowed=true;expect(controller.commitToken(close)).toBe(true);expect(session.dispose()).toBe(true);
});
test('attachment receipt remains on its original controller after scope retirement', async () => {
  const {createEditorState}=await import('../static/js/editor/state.js');
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const session=createImageSessionRegistry().open({scope:{accountId:'alice',workspaceId:'w'}}), scene=createEditorState(); let authorized=true;
  const controller=createImageSessionPersistence({session,scene,adapter:{save:async()=>{},bindReceipt:()=>()=>{}},authority:{validateScope:()=>authorized}});
  controller.setReceiptOwner(session.attachPersistenceController(controller));
  const descriptor={v:1,operationId:'operation',owner:'alice',workspace:'w',chatId:'chat',authEpoch:1,composerEpoch:2,composerVersion:3,sourceDigest:'a'.repeat(64),sceneRevision:0,pngDigest:'b'.repeat(64),state:'pending',uploadId:null,uploadDigest:null};
  controller.setAttachmentApply(descriptor); const record=controller.createAttachmentOutcomeRecorder(descriptor); expect(controller.canReauthorize()).toBe(false); expect(()=>controller.clearUnsubmittedAttachmentApply('operation')).toThrow();
  authorized=false; expect(()=>record({state:'committed',uploadId:'image.png',uploadDigest:'b'.repeat(64)})).not.toThrow();
  expect(controller.attachmentApply.uploadId).toBe('image.png'); expect(controller.canReauthorize()).toBe(true);
  expect(()=>record({state:'committed',uploadId:'other.png',uploadDigest:'b'.repeat(64)})).toThrow(); expect(controller.canDispose()).toBe(false);
});

test('Save of a restored unbound draft copies visible pixels and preserves its retained result', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'};
  const session=createImageSessionRegistry().open({scope,draftId:'restored'});
  const target=session.captureTarget();
  const originalDocument=globalThis.document;
  const makeCanvas=(pixel='')=>({
    pixel,width:1,height:1,toDataURL(){return 'data:image/png;base64,YQ==';},
    toBlob(done,mime){done(new Blob([this.pixel],{type:mime}));},
    getContext(){return {drawImage:source=>{this.pixel+=source.pixel;}};}
  });
  globalThis.document={createElement:()=>makeCanvas()};
  try {
    const layers=['visible','hidden'].map((id,index)=>({id,visible:index===0,opacity:1,canvas:makeCanvas(id)}));
    const scene={sceneRevision:4,layers,layerOffsets:new Map(),imgWidth:1,imgHeight:1,draftId:'restored'};
    const copies=[],drafts=[];
    const adapter={
      bindReceipt:()=>()=>{},save:()=>{throw Error('Unexpected original save');},retryCopy:()=>{},
      async startCopy({imageBytes,persistPending}) {
        copies.push([imageBytes.type,await imageBytes.text()]);
        await persistPending({operationId:'copy'});
        return {resourceId:'copy-'+copies.length};
      }
    };
    const controller=createImageSessionPersistence({session,scene,adapter,authority:{validateScope:()=>true,validate:()=>true},
      fetchImpl:async(url,request)=>{expect(url).toBe('/api/editor-drafts/restored');drafts.push(JSON.parse(request.body).payload);return {ok:true,json:async()=>({id:'restored'})};}});
    controller.setReceiptOwner(session.attachPersistenceController(controller));
    expect((await session.save()).status).toBe('unavailable');
    const identity=JSON.stringify([target.sessionId,scope,null,null,null]);
    controller.restoreResults([{id:'retained',identity,kind:'ai',status:'submitted',generation:1,epoch:'prior'}]);
    controller.markRestoredDraft(target);
    const receipt=await session.save();
    expect(receipt).toEqual({resourceId:'copy-1',status:'saved_copy',sceneRevision:4});
    expect(await session.save()).toEqual(receipt);
    expect(copies).toEqual([['image/png','visible']]);
    expect(drafts.at(-1)._imageRetainedResults[0].status).toBe('unknown');
    expect(drafts.at(-1)._imagePendingCopy).toBeUndefined();
    expect(session.captureTarget()).toEqual(target);
    scene.sceneRevision++;
    expect((await session.save()).resourceId).toBe('copy-2');
    adapter.uncertain=true;
    await expect(session.save()).rejects.toThrow('unavailable');
  } finally {
    if (originalDocument===undefined) delete globalThis.document;
    else globalThis.document=originalDocument;
  }
});

test('Host PNG Save As survives reload, saves the same original and rejects a changed fingerprint', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const {resolveHostRaster,hostRasterFingerprint}=await import('../static/js/editor/hostRasterOriginal.js');
  const scope={accountId:'a',workspaceId:'w'}, resourceId='resource-'+'1'.repeat(64);
  const makeCanvas=(pixel='')=>({
    pixel,width:1,height:1,toDataURL(){return 'data:image/png;base64,YQ==';},
    toBlob(done,type){done(new Blob([this.pixel],{type}));},getContext(){return {drawImage:src=>{this.pixel+=src.pixel;}};}
  });
  const initialDocument=globalThis.document; globalThis.document={createElement:()=>makeCanvas()};
  let draft,row,disk,writeCount=0;
  const client={stat:async ref=>{expect(ref).toBe('host-ref');return {resource:row};}};
  const adapter={save:()=>{throw Error('unexpected managed save');},bindReceipt:()=>()=>{},
    statHostRaster:ref=>client.stat(ref),resolveHostRaster:(original,current)=>resolveHostRaster(client,original,current),hostRasterTrail:async()=>[]};
  async function write(blob) {
    disk=await blob.text(); writeCount++;
    row={ref:'host-ref',id:resourceId,kind:'file',provider:'host',name:'new.png',mime_type:'image/png',capabilities:['write'],revision:{kind:'hostFingerprint',value:await hostRasterFingerprint(blob)}};
    return {resourceRef:row.ref,resourceId,revision:{...row.revision}};
  }
  const fetchImpl=async(url,req)=>{
    if (url.startsWith('/api/editor-drafts')) {
      draft=JSON.parse(req.body).payload;
      return {ok:true,json:async()=>({id:'draft'})};
    }
    expect(new URL(url,'http://test').searchParams.get('expected_fingerprint')).toBe(row.revision.value);
    await write(req.body);
    return {ok:true,json:async()=>({outcome:'applied',revision:{...row.revision}})};
  };
  const mount=(session,pixel)=>{
    const layer={id:'l',visible:true,opacity:1,canvas:makeCanvas(pixel)};
    const scene={sceneRevision:3,layers:[layer],imgWidth:1,imgHeight:1,layerOffsets:new Map()};
    const controller=createImageSessionPersistence({session,scene,adapter,authority:{validate:()=>true,validateScope:()=>true},fetchImpl});
    controller.setReceiptOwner(session.attachPersistenceController(controller));
    return {controller,scene,layer};
  };
  try {
    const firstSession=createImageSessionRegistry().open({scope});
    const first=mount(firstSession,'first');
    expect((await first.controller.saveToHostDestination({name:'new.png',write})).status).toBe('saved_copy');
    expect(draft._imageHostOriginal.revision).toEqual(row.revision);
    const secondSession=createImageSessionRegistry().restore(firstSession.serializeRecovery());
    const second=mount(secondSession,'first');
    await second.controller.restoreHostOriginal(draft._imageHostOriginal,secondSession.captureTarget(),draft._imageHostPixelsSaved);
    expect(second.controller.dirty).toBe(false);
    second.layer.canvas.pixel='second'; second.scene.sceneRevision++;
    await secondSession.save();
    expect([disk,writeCount,draft._imageHostOriginal.revision,draft._imageHostPixelsSaved]).toEqual(['second',2,row.revision,true]);
    const captured={...draft._imageHostOriginal,scope:{accountId:'elsewhere',workspaceId:'w'}};
    await expect(second.controller.restoreHostOriginal(captured,secondSession.captureTarget())).rejects.toThrow('does not belong');
    row.revision={kind:'hostFingerprint',value:'sha256:'+'f'.repeat(64)+':7'};
    await expect(secondSession.save()).rejects.toThrow('original changed');
    expect(writeCount).toBe(2);
  } finally {
    if (initialDocument===undefined) delete globalThis.document; else globalThis.document=initialDocument;
  }
});

test('expired Host raster ref follows current folder/resource IDs and checks current digest', async()=>{
  const {resolveHostRaster}=await import('../static/js/editor/hostRasterOriginal.js');
  const id=n=>'resource-'+String(n).repeat(64), revision={kind:'hostFingerprint',value:'sha256:'+'c'.repeat(64)+':24'};
  const folder={id:id(1),name:'Photos',provider:'host',kind:'folder',capabilities:['children'],ref:'folder'};
  const row={id:id(2),provider:'host',kind:'file',name:'sample.png',mime_type:'image/png',ref:'new',capabilities:['write'],revision};
  const original={resourceId:row.id,resourceRef:'expired',name:row.name,revision,mime:row.mime_type,trail:[{id:folder.id,name:folder.name}]};
  const client={stat:async ref=>{if(ref==='expired')throw Error('expired');expect(ref).toBe('new');return {resource:row};},
    roots:async()=>({entries:[{kind:'provider_root',provider:'host',ref:'root'}]}),places:async()=>({entries:[]}),
    children:async ref=>({entries:ref==='root'?[folder]:[{...row,id:id(3),ref:'same-name-other'},row]})};
  expect((await resolveHostRaster(client,original,()=>true)).id).toBe(row.id);
  expect(original.resourceRef).toBe('new');
});

test('close an interrupted copy only with a matching durable draft and unchanged local scene', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const session=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}});
  const scene={sceneRevision:2,layers:[{}]};
  let payload;
  const adapter={save(){},bindReceipt:()=>()=>{},async startCopy({target,persistPending}) {
    await persistPending({operationId:'copy',target});
    throw Error('interrupted');
  }};
  const controller=createImageSessionPersistence({session,scene,adapter,authority:{validateScope:()=>true},
    fetchImpl:async(url,req)=>{
      if (req.body) payload=JSON.parse(req.body).payload;
      return {ok:true,json:async()=>({id:'saved-draft',payload})};
    }});
  controller.setReceiptOwner(session.attachPersistenceController(controller));
  const snapshot={target:session.captureTarget(),sceneRevision:2,stateDoc:{pixels:'original'}};
  expect((await controller.prepareClose('save',{save:async()=>({status:'cancelled'})})).status).toBe('cancelled');
  await expect(controller.startCopy(snapshot,new Blob())).rejects.toThrow('interrupted');
  expect(controller.canCloseWithRecovery()).toBe(true);
  const original=payload;
  await expect(controller.saveDraft(snapshot)).rejects.toThrow('pending image copy');
  payload={...original,_imagePendingCopy:null};
  expect((await controller.prepareClose('close_recovery')).status).toBe('pending');
  payload=original;
  const stale=await controller.prepareClose('close_recovery');
  expect(stale.status).toBe('prepared');
  scene.sceneRevision++;
  expect(controller.commitToken(stale)).toBe(false);
  const token=await controller.prepareClose('close_recovery');
  expect(controller.commitToken(token)).toBe(true);
  expect(session.serializeRecovery().draftId).toBe('saved-draft');
  expect(payload._imagePendingCopy.operationId).toBe('copy');
  expect(payload.pixels).toBe('original');
  expect(session.dispose()).toBe(true);
});

test('a lost first draft receipt reuses its creation key and binds the saved scene once', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const session=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}});
  const scene={sceneRevision:1,layers:[{}]};
  const keys=[];
  const controller=createImageSessionPersistence({session,scene,adapter:{save(){},bindReceipt:()=>()=>{}},authority:{validateScope:()=>true},
    fetchImpl:async(url,req)=>{
      keys.push(JSON.parse(req.body).request_id);
      if (keys.length===1) throw Error('lost receipt');
      return {ok:true,json:async()=>({id:'saved-draft'})};
    }});
  controller.setReceiptOwner(session.attachPersistenceController(controller));
  await expect(controller.saveDraft({target:session.captureTarget(),sceneRevision:1,stateDoc:{layers:[]}})).rejects.toThrow('lost receipt');
  expect(controller.recoveryStatus().state).toBe('draft_uncertain');
  expect(await controller.reconcileDraft()).toBe(true);
  expect(keys[0]).toBeTruthy();
  expect(keys[1]).toBe(keys[0]);
  expect(session.captureTarget().draftId).toBe('saved-draft');
  expect(controller.recoveryStatus().state).toBe('ready');
});

test('a 503 on managed Save allows closing to a verified pending project draft', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const session=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}});
  const scene={sceneRevision:2,layers:[{}]};
  let payload;
  const adapter={bindReceipt:()=>()=>{},async save({target,beforeIssue}) {
    await beforeIssue({operationId:'imps:ff39ae83-fbc1-48d5-baa9-fb394a1b45c4',kind:'save',target});
    this.uncertain=true;
    throw Error('503');
  }};
  const controller=createImageSessionPersistence({session,scene,adapter,authority:{validateScope:()=>true},
    fetchImpl:async(url,req)=>{
      if (req.body) payload=JSON.parse(req.body).payload;
      return {ok:true,json:async()=>({id:'saved-draft',payload})};
    }});
  controller.setReceiptOwner(session.attachPersistenceController(controller));
  await expect(controller.save({target:session.captureTarget(),sceneRevision:2,stateDoc:{pixels:'original'}},new Blob())).rejects.toThrow('503');
  expect(controller.recoveryStatus().state).toBe('save_uncertain');
  const token=await controller.prepareClose('close_recovery');
  expect(controller.commitToken(token)).toBe(true);
  expect(payload._imageProjectOperation.kind).toBe('save');
  expect(session.dispose()).toBe(true);
});

test('managed original Save reads format and permission before allocating a project', async () => {
  const {hostRasterMime}=await import('../static/js/editor/hostRasterOriginal.js');
  const samples={
    png:'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAFklEQVR4nGP8//8/AwMDEwMDAwMDAwAkBgMB/DXemwAAAABJRU5ErkJggg==',
    apng:'iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAACGFjVEwAAAACAAAAAPONk3AAAAAaZmNUTAAAAAAAAAACAAAAAgAAAAAAAAAAAAEACgAA6FTcAAAAABZJREFUeJxj/P//PwMDAxMDAwMDAwMAJAYDAfw13psAAAAaZmNUTAAAAAEAAAACAAAAAgAAAAAAAAAAAAEACgAAcyc21AAAABpmZEFUAAAAAnicY/zPwMDAwMDEwMDAwMAAAA0dAQPL5wGYAAAAAElFTkSuQmCC',
    webp:'UklGRiQAAABXRUJQVlA4IBgAAAAwAQCdASoCAAIAAUAmJaQAA3AA/vz0AAA=',
    animatedWebp:'UklGRq4AAABXRUJQVlA4WAoAAAACAAAAAQAAAQAAQU5JTQYAAAAAAAAAAABBTk1GMAAAAAAAAAAAAAEAAAEAAGQAAAJWUDggGAAAADABAJ0BKgIAAgABQCYlpAADcAD+/PQAAEFOTUZKAAAAAAAAAAAAAQAAAQAAZAAAAFZQOCAyAAAANAEAnQEqAgACAAAAJiWgAANwAP7y63//+bA/9vP/BHoB///S4P/+lwf/9Lg/9KQAAAA=',
    gif:'R0lGODdhAgACAIEAAP///wAAAAAAAAAAACwAAAAAAgACAAAIBgABCAQQEAA7'
  };
  const bytes=key=>Uint8Array.from(atob(samples[key]),c=>c.charCodeAt(0)).buffer;
  expect(hostRasterMime(bytes('png'),'image/png')).toBe('image/png');
  expect(hostRasterMime(bytes('webp'),'image/webp')).toBe('image/webp');
  for (const [file,mime,readOnly] of [['gif','image/gif',false],['apng','image/png',false],['animatedWebp','image/webp',false],['png','image/png',true],['gif','image/jpeg',false]]) {
    const blob=new Blob([bytes(file)],{type:mime});
    const digest='sha256:'+Buffer.from(await crypto.subtle.digest('SHA-256',await blob.arrayBuffer())).toString('hex');
    const target={sessionId:'s',scope:{accountId:'a',workspaceId:'w'},sourceKey:{provider:'files',resourceId:'id'},opaqueRef:'ref',sourceDigest:digest,sourceRevision:{kind:'contentDigest',value:'v'}};
    const filesClient={openResource:async()=>({target:{app:'imps'},resource:{id:'id',ref:'ref',provider:'files',revision:target.sourceRevision,capabilities:['open','preview']},payload:{provider:'files',resource_id:'id',read_only:readOnly}}),contentUrl:()=>'/content'};
    const adapter=createImagePersistence({session:{captureTarget:()=>target},authority:{validate:()=>true,validateScope:()=>true},filesClient,fetchImpl:async url=>{
      expect(url).toBe('/content');return new Response(blob,{headers:{'content-type':mime}});
    }});
    adapter.bindReceipt(()=>{});
    const args={target,state:{layers:[]},imageBytes:blob,beforeIssue:()=>{throw Error('unexpected allocation');}};
    await expect(adapter.save(args)).rejects.toThrow('Original encoding or write access');
    expect(adapter.managedSaveMime).toBeNull();
  }
});
