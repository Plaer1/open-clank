import {test, expect} from 'bun:test';
import {createEditorState, createEditorLifetime} from '../static/js/editor/state.js';
import {createApplyImageTool} from '../static/js/editor/ai-tool-runner.js';

test('runner waits for decode and retires old origin without touching new session', async () => {
  let resolveFetch, image; const response = new Promise(r => {resolveFetch=r;});
  class FakeImage { constructor(){ image=this; } set src(value){ this.url=value; } }
  const life=createEditorLifetime(), a=createEditorState(), b=createEditorState(); a.editorOpen=true; a.imgWidth=1; a.imgHeight=1;
  const btn={innerHTML:'Go',offsetWidth:10,disabled:false,style:{},classList:{add(){},remove(){}},appendChild(){}};
  const root={ownerDocument:{createElement:()=>({})}}; let destroyed=0; const payload={strength:1};
  const run=createApplyImageTool({state:a,root,lifetime:life,captureOrigin:()=>()=>life.disposed?'retired':'current',fetchImpl:()=>response,ImageClass:FakeImage,flatten:()=>({toDataURL:()=> 'data:image/png;base64,AA'}),saveState(){},createLayer:()=>({id:'x',ctx:{drawImage(){}}}),composite(){},renderLayerPanel(){},deriveBusyLabel:()=>'',getSelectedAIEndpoint:()=>({modelRouteId:'route'}),spinnerModule:{create:()=>({createElement:()=>({}),start(){},destroy(){destroyed++;}})},uiModule:{showToast(){}}});
  const pending=run('/api/image/sharpen',payload,'Result',btn); resolveFetch({ok:true,json:async()=>({image:'AA'})});
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  expect(image).toBeDefined(); expect(a.layers.length).toBe(0); expect(payload.model_route_id).toBeUndefined();
  life.dispose(); const result=await pending;
  expect(result.status).toBe('retired'); expect(a.layers.length).toBe(0); expect(b.layers.length).toBe(0); expect(destroyed).toBe(1);
});

test('runner retains returned bytes in the original session after an edit', async () => {
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64);
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,opaqueRef:'ref'});
  const state=createEditorState(), life=createEditorLifetime(), authority={validateScope:()=>true,validate:()=>true}; state.editorOpen=true;
  const queue=createImageSessionPersistence({session,scene:state,adapter:{save:async()=>{},bindReceipt:()=>()=>{}},authority});
  queue.setReceiptOwner(session.attachPersistenceController(queue));
  const hooks={reserve:()=>queue.reserveResult({kind:'ai',epoch:'a',generation:0}),submit:id=>queue.submitResult(id),receive:(id,value)=>queue.receiveResult(id,value),reject:id=>queue.rejectResult(id),unknown:id=>queue.markResultUnknown(id),resolve:id=>queue.resolveResult(id)};
  let release, changed=false; const response=new Promise(resolve=>{release=resolve;});
  const btn={innerHTML:'Go',offsetWidth:10,style:{},classList:{add(){},remove(){}},appendChild(){}};
  const run=createApplyImageTool({state,root:{ownerDocument:{createElement:()=>({})}},lifetime:life,captureOrigin:()=>()=>changed?'changed':'current',resultHooks:hooks,fetchImpl:()=>response,flatten:()=>({toDataURL:()=> 'data:image/png;base64,AA'}),saveState(){throw new Error('must not apply');},createLayer(){},composite(){},renderLayerPanel(){},deriveBusyLabel:()=>'',getSelectedAIEndpoint:()=>({}),spinnerModule:{create:()=>({createElement:()=>({}),start(){},destroy(){}})},uiModule:{showToast(){}}});
  const pending=run('/api/image/sharpen',{},'Result',btn); changed=true; release({ok:true,json:async()=>({image:'cGl4ZWxz'})});
  expect((await pending).status).toBe('retained'); const record=queue.retainedResultStatus().records[0];
  expect(record.status).toBe('received'); expect(queue.readRetainedResult(record.id).bytes).toBe('cGl4ZWxz'); expect(state.layers.length).toBe(0);
});

test('inpaint retains changed-origin output and original base and mask', async () => {
  const {wireInpaintButtons}=await import('../static/js/editor/ai-inpaint.js');
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64);
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,opaqueRef:'ref'});
  const state=createEditorState(), life=createEditorLifetime(), authority={validateScope:()=>true,validate:()=>true}; state.editorOpen=true; state.imgWidth=1; state.imgHeight=1;
  const queue=createImageSessionPersistence({session,scene:state,adapter:{save:async()=>{},bindReceipt:()=>()=>{}},authority}); queue.setReceiptOwner(session.attachPersistenceController(queue));
  const hooks={reserve:()=>queue.reserveResult({kind:'ai',epoch:'a',generation:0}),submit:id=>queue.submitResult(id),receive:(id,value)=>queue.receiveResult(id,value),reject:id=>queue.rejectResult(id),unknown:id=>queue.markResultUnknown(id),resolve:id=>queue.resolveResult(id)};
  const btn=Object.assign(new EventTarget(),{appendChild(){},disabled:false}), prompt={value:'private prompt'}, nodes=new Map([['ge-inpaint-run',btn],['ge-inpaint-prompt',prompt]]);
  const canvas=()=>({width:1,height:1,getContext:()=>({getImageData:()=>({data:new Uint8Array([0,0,0,255])}),drawImage(){}}),toDataURL:()=> 'data:image/png;base64,cGl4ZWxz'});
  state.layers.push({id:'hidden-parent',visible:false,masks:[{id:'visible-mask',visible:true,canvas:canvas()},{id:'hidden-mask',visible:false,canvas:canvas()}]});
  const doc={createElement:()=>canvas(),body:{appendChild(){}}}, root={ownerDocument:doc,querySelector:selector=>nodes.get(selector.match(/#(ge-[\w-]+)/)?.[1]) || null};
  state.container=root; state.mainCanvas={getBoundingClientRect:()=>({width:0,height:0})}; let changed=false, release;
  const previousFetch=globalThis.fetch, previousWindow=globalThis.window; globalThis.window=new EventTarget(); globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
  try {
    wireInpaintButtons({state,root,lifetime:life,captureOrigin:()=>()=>changed?'changed':'current',resultHooks:hooks,buildMergedMaskCanvas:canvas,dilateMask:canvas,applyInpaintFeather(){},getSelectedAIEndpoint:()=>({}),saveState(){throw new Error('must not apply');},createLayer(){},composite(){},renderLayerPanel(){},spinnerModule:{createWhirlpool:()=>({element:{style:{}},destroy(){}})},uiModule:{showToast(){}}});
    btn.dispatchEvent(new Event('click')); changed=true; release({ok:true,json:async()=>({image:'cmVzdWx0'})}); await new Promise(resolve=>setTimeout(resolve,0));
    const item=queue.readRetainedResult(queue.retainedResultStatus().records[0].id);
    expect(item.bytes).toBe('cmVzdWx0'); expect(item.sidecars).toEqual({base:'cGl4ZWxz',mask:'cGl4ZWxz'}); expect(item.metadata.inpaint.width).toBe(1); expect(item.metadata.inpaint.participants).toEqual([{layer:'hidden-parent',mask:'visible-mask'}]); expect(JSON.stringify(item.metadata)).not.toContain('private prompt'); expect(state.layers.length).toBe(1);
  } finally {life.dispose(); globalThis.fetch=previousFetch; globalThis.window=previousWindow;}
});

test('upscale retains changed-origin output as an independent full-size image', async () => {
  const {wireAIToolsMisc}=await import('../static/js/editor/ai-tools-misc.js');
  const {createImageSessionRegistry}=await import('../static/js/editor/imageSession.js');
  const {createImageSessionPersistence}=await import('../static/js/editor/imageSessionPersistence.js');
  const scope={accountId:'a',workspaceId:'w'}, key={...scope,provider:'files',resourceId:'r'}, digest='sha256:'+'a'.repeat(64);
  const session=createImageSessionRegistry().open({scope,sourceKey:key,sourceRevision:{kind:'imageSha256',value:digest},sourceDigest:digest,opaqueRef:'ref'});
  const state=createEditorState(), life=createEditorLifetime(), authority={validateScope:()=>true,validate:()=>true}; state.imgWidth=2; state.imgHeight=3;
  const queue=createImageSessionPersistence({session,scene:state,adapter:{save:async()=>{},bindReceipt:()=>()=>{}},authority}); queue.setReceiptOwner(session.attachPersistenceController(queue));
  const hooks={reserve:()=>queue.reserveResult({kind:'ai',epoch:'a',generation:0}),submit:id=>queue.submitResult(id),receive:(id,value)=>queue.receiveResult(id,value),reject:id=>queue.rejectResult(id),unknown:id=>queue.markResultUnknown(id),resolve:id=>queue.resolveResult(id)};
  const btn=Object.assign(new EventTarget(),{innerHTML:'Go',appendChild(){}}), doc={createElement:()=>({})}, root={ownerDocument:doc,querySelector:selector=>selector.includes('#ge-upscale-ai') ? btn : null};
  let changed=false, release; const previousFetch=globalThis.fetch; globalThis.fetch=()=>new Promise(resolve=>{release=resolve;});
  try {
    wireAIToolsMisc({state,root,lifetime:life,captureOrigin:()=>()=>changed?'changed':'current',resultHooks:hooks,flatten:()=>({toDataURL:()=> 'data:image/png;base64,AA'}),saveState(){throw new Error('must not apply');},createLayer(){},fitZoom(){},composite(){},renderLayerPanel(){},spinnerModule:{createWhirlpool:()=>({element:{style:{}},destroy(){}})},uiModule:{showToast(){}}});
    btn.dispatchEvent(new Event('click')); changed=true; state.sceneRevision++; release({ok:true,json:async()=>({image:'cmVzdWx0'})}); await new Promise(resolve=>setTimeout(resolve,0));
    const record=queue.retainedResultStatus().records[0], item=queue.readRetainedResult(record.id);
    expect(item.bytes).toBe('cmVzdWx0'); expect(item.metadata.fullSize).toBe(true); expect(record.originChanged).toBe(true); expect([state.imgWidth,state.imgHeight,state.layers.length]).toEqual([2,3,0]);
  } finally {life.dispose();globalThis.fetch=previousFetch;}
});
