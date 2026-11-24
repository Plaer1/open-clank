import { test, expect } from 'bun:test';
import { createEditorLifetime } from '../static/js/editor/state.js';
import { captureImportOperation } from '../static/js/editor/async-import-ownership.js';

test('deferred import remains with its origin and exposes edited results', async () => {
  let epoch = 1;
  const a = {revision:'r', generation:0, images:[], captureTarget() { return {sessionId:'a',scope:{accountId:'a',workspaceId:'w'},sourceRevision:this.revision}; }}, b = {images:[]};
  const life = createEditorLifetime();
  const make = () => captureImportOperation({session:a, lifetime:life, readAuthorityEpoch:()=>epoch, readEditGeneration:()=>a.generation});
  let resolve; const decoded = new Promise(r => {resolve=r;});
  const op = make(); a.generation++;
  resolve('pixels'); const result = op.settle(await decoded, (image, session) => session.images.push(image));
  expect(result.status).toBe('recoverable'); expect(result.session).toBe(a); expect(result.value).toBe('pixels'); expect(b.images).toEqual([]);
  const retired = make(); life.dispose(); expect(retired.settle('late', () => {throw new Error('must not apply');}).status).toBe('retired');
  expect(() => captureImportOperation({session:a,lifetime:createEditorLifetime(),readAuthorityEpoch:()=>null,readEditGeneration:()=>0})).toThrow();
});

test('clipboard import retains exact bytes after the originating scene changes', async () => {
  const {wireImport}=await import('../static/js/editor/wire-import.js');
  const {createEditorState}=await import('../static/js/editor/state.js');
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64);
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,opaqueRef:'ref'});
  const state=createEditorState(), lifetime=createEditorLifetime(), authority={validateScope:()=>true,validate:()=>true}; state.editorOpen=true;
  const queue=createImageSessionPersistence({session,scene:state,adapter:{save:async()=>{},bindReceipt:()=>()=>{}},authority}); queue.setReceiptOwner(session.attachPersistenceController(queue));
  let release; const data=new Promise(resolve=>{release=resolve;}), buttons=new Map();
  const doc=new EventTarget(); doc.defaultView={navigator:{clipboard:{read:()=>data}}}; doc.createElement=()=>Object.assign(new EventTarget(),{style:{},remove(){}});
  const paste=new EventTarget(); buttons.set('ge-import-paste',paste);
  const container={ownerDocument:doc,appendChild(){},querySelector(selector){return buttons.get(selector.match(/#(ge-[\w-]+)/)?.[1]) || null;}};
  const previous=globalThis.Image; globalThis.Image=class {constructor(){this.width=1;this.height=1;} set src(_value){queueMicrotask(()=>this.onload?.());}};
  const captureImport=()=>{const revision=state.sceneRevision; const check=()=>state.sceneRevision===revision?'current':'edited'; check.revision=revision; return check;};
  const startImport=check=>{const id=queue.reserveResult({kind:'import',epoch:'a',generation:check.revision});let received=false;return {async receive(blob,metadata){queue.receiveResult(id,{bytes:btoa(await blob.text()),mime:blob.type,metadata});received=true;},apply(fn){if(check()!=='current') return false;fn();queue.resolveResult(id);return true;},cancel(){if(!received) queue.cancelLocalReservation(id);}};};
  try {
    wireImport({state,container,lifetime,captureImport,startImport,saveState(){throw new Error('must not apply');},createLayer(){},composite(){},renderLayerPanel(){},uiModule:{showToast(){}}});
    paste.dispatchEvent(new Event('click')); state.sceneRevision++; release([{types:['image/png'],getType:async()=>new Blob(['pixels'],{type:'image/png'})}]);
    await new Promise(resolve=>setTimeout(resolve,0)); const record=queue.retainedResultStatus().records[0];
    expect(record.status).toBe('received'); expect(queue.readRetainedResult(record.id).bytes).toBe(btoa('pixels')); expect(state.layers.length).toBe(0);
  } finally {lifetime.dispose(); globalThis.Image=previous;}
});

test('internal paste requires focused live view and scene ownership', async () => {
  const {wireClipboardAndDrop}=await import('../static/js/editor/clipboard-and-drop.js');
  const {createEditorState,createImageSceneAuthority}=await import('../static/js/editor/state.js');
  const oldWindow=globalThis.window, win=new EventTarget(); globalThis.window=win;
  const state=createEditorState(), scene=createImageSceneAuthority({state}), doc=new EventTarget(), container=new EventTarget();
  const canvas=()=>{const c={width:2,height:2};c.getContext=()=>({drawImage(){}});return c;};
  doc.createElement=canvas; container.ownerDocument=doc; container.getClientRects=()=>[{}]; container.querySelector=()=>null;
  state.editorOpen=true; state.internalClipboard=canvas(); let focused=false,saves=0;
  const wiring=wireClipboardAndDrop({state,container,isFocused:()=>focused,capturePaste:()=>scene.revision,pasteCurrent:r=>r===scene.revision,withSceneMutation:(name,fn)=>{const t=scene.beginGesture(container,name);if(!t)return false;try{return fn();}finally{scene.endGesture(t);}},saveState:()=>saves++,createLayer:()=>({id:'new',canvas:canvas(),ctx:{drawImage(){}}}),markSceneChanged:()=>scene.markChanged(),renderLayerPanel(){},composite(){}});
  try {
    win.dispatchEvent(new Event('paste',{cancelable:true})); expect(saves).toBe(0);
    focused=true; const held=scene.beginGesture({},'stroke'); win.dispatchEvent(new Event('paste',{cancelable:true})); expect(saves).toBe(0); expect(state.layers.length).toBe(0);
    scene.endGesture(held); win.dispatchEvent(new Event('paste',{cancelable:true})); expect(saves).toBe(1); expect(state.layers.length).toBe(1);
  } finally {wiring.dispose();scene.dispose();globalThis.window=oldWindow;}
});

test('adjustment preview projection leaves committed render cache unchanged', async () => {
  const {renderLayerWithAdjLayers}=await import('../static/js/editor/fx/pixel-pass.js');
  const oldDocument=globalThis.document;
  globalThis.document={createElement(){const canvas={width:1,height:1,draws:[]};canvas.getContext=()=>({filter:'',globalAlpha:1,drawImage(source){canvas.draws.push({source,filter:this.filter,alpha:this.globalAlpha});}});return canvas;}};
  try {
    const source={width:1,height:1}, adj={id:'a',type:'brightness-contrast',visible:true,opacity:0.4,params:{brightness:1,contrast:1}}, second={id:'b',type:'hue-saturation',visible:true,opacity:1,params:{hue:20,saturation:1}}, layer={canvas:source,adjLayers:[adj,second]};
    const committed=renderLayerWithAdjLayers(layer), key=layer._adjFinalKey;
    const projection={editingId:'a',staged:{type:'brightness-contrast',params:{brightness:2,contrast:1}},revision:1,cache:{}};
    const preview=renderLayerWithAdjLayers(layer,projection), blend=preview.draws[0].source;
    expect(preview).not.toBe(committed); expect(preview.draws[0].filter).toBe('saturate(1) hue-rotate(20deg)');
    expect(blend.draws[1].alpha).toBe(0.4); expect(blend.draws[1].source.draws[0].filter).toBe('brightness(2) contrast(1)');
    expect(layer._adjFinal).toBe(committed); expect(layer._adjFinalKey).toBe(key); expect(renderLayerWithAdjLayers(layer)).toBe(committed);
    expect(renderLayerWithAdjLayers(layer,projection)).toBe(preview);
    projection.staged.params.brightness=3; const changed=renderLayerWithAdjLayers(layer,projection); expect(changed).not.toBe(preview);
    projection.revision++; expect(renderLayerWithAdjLayers(layer,projection)).not.toBe(changed);
    adj.visible=false; const hidden=renderLayerWithAdjLayers(layer,projection); expect(hidden.draws[0].source).toBe(source);
  } finally {globalThis.document=oldDocument;}
});

test('addressed Files import retains received bytes and fences close during encoding', async () => {
  const {wireClipboardAndDrop,createFilesCanvasImagePayload}=await import('../static/js/editor/clipboard-and-drop.js');
  const {createEditorState}=await import('../static/js/editor/state.js');
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const state=createEditorState(),session=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}}), adapter={save:async()=>{},bindReceipt:()=>()=>{}}, authority={validateScope:()=>true,validate:()=>true};
  const controller=createImageSessionPersistence({session,scene:state,adapter,authority});controller.setReceiptOwner(session.attachPersistenceController(controller));
  const prior=await controller.prepareClose('discard');let release,started;const gate=new Promise(resolve=>{release=resolve;}),onStarted=new Promise(resolve=>{started=resolve;});
  const key='a|w|files:r', revision={kind:'filesImageRevision',value:'1'}, context={commandId:'cmd',generation:1,policyGeneration:1,selectionEpoch:1,owner:'a',workspace:'w',pane:'p',provider:'files',parent:'root',scopeKey:'scope',selectedKeys:[key],sourceCapabilities:{[key]:{read:true,open:false,download:true,export:false}},allowCopy:true,allowMove:false};
  const payload=createFilesCanvasImagePayload({ref:'ref',resource_key:key,provider:'files',mime_type:'image/png',size:3,capabilities:['read','download'],revision},{account:'a',workspace:'w',operationId:'cmd',itemId:'item',context});
  const filesClient={openResource:async()=>({resource:{id:'r',ref:'ref',provider:'files',mime_type:'image/png',revision,capabilities:['read','download']}}),contentUrl:()=>'/content'};
  const oldWindow=globalThis.window,oldFetch=globalThis.fetch,win=new EventTarget(),doc=new EventTarget(),container=new EventTarget();globalThis.window=win;globalThis.fetch=async()=>({ok:true,headers:{get:()=>null},blob:async()=>new Blob(['abc'],{type:'image/png'})});
  doc.createElement=()=>({style:{},remove(){}});container.ownerDocument=doc;container.querySelector=()=>null;container.getClientRects=()=>[{}];state.editorOpen=true;let live=context,applied=0;
  const startImport=check=>{const id=controller.reserveResult({kind:'import',epoch:'1',generation:check.revision});let received=false;return {get received(){return received;},async receive(blob,metadata){started();await gate;controller.receiveResult(id,{bytes:btoa(await blob.text()),mime:blob.type,metadata});received=true;},apply(){applied++;return true;},cancel(){controller.cancelLocalReservation(id);}};};
  const wiring=wireClipboardAndDrop({state,container,filesClient,acceptLegacyBroadcast:false,captureImport:()=>Object.assign(()=> 'current',{revision:state.sceneRevision}),startImport,saveState(){},createLayer(){},renderLayerPanel(){},composite(){}});
  try { const pending=wiring.deliverFilesImage(payload,context,()=>live);await onStarted;expect((await controller.prepareClose('discard')).reason).toBe('image_import_must_settle_or_cancel');expect(controller.validateToken(prior)).toBe(false);live={...context,selectionEpoch:2};release();expect((await pending).status).toBe('retained');expect(applied).toBe(0);const result=controller.retainedResultStatus().records[0];expect(result.status).toBe('received');expect(controller.readRetainedResult(result.id).bytes).toBe(btoa('abc'));expect((await controller.prepareClose('discard')).status).toBe('prepared'); }
  finally {wiring.dispose();globalThis.window=oldWindow;globalThis.fetch=oldFetch;}
  const other=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}}), otherController=createImageSessionPersistence({session:other,scene:createEditorState(),adapter,authority});otherController.setReceiptOwner(other.attachPersistenceController(otherController));
  const reservation=otherController.reserveResult({kind:'import',epoch:'1',generation:0});expect((await otherController.prepareClose('discard')).reason).toBe('image_import_must_settle_or_cancel');expect(otherController.cancelLocalReservation(reservation)).toBe(true);expect((await otherController.prepareClose('discard')).status).toBe('prepared');
});
