import { test, expect } from 'bun:test';
import { createEditorState, createEditorLifetime, createImageSceneAuthority } from '../static/js/editor/state.js';
import { createMoveTool } from '../static/js/editor/tools/move.js';
import { createStrokeTool } from '../static/js/editor/tools/stroke.js';

test('move input and retirement belong to their own session', () => {
  const make = () => {
    const state = createEditorState(), lifetime = createEditorLifetime(), root = {};
    const canvas = {width: 100, height: 100, getBoundingClientRect: () => ({left: 0, top: 0, width: 100, height: 100})};
    const layer = {id: 'layer', canvas, visible: true};
    state.mainCanvas = canvas; state.layers.push(layer); state.layerOffsets.set(layer.id, {x: 0, y: 0});
    let saves = 0;
    const tool = createMoveTool({state, root, lifetime, activeLayer: () => layer, saveState: () => saves++, composite: () => {}});
    return {state, lifetime, tool, saves: () => saves};
  };
  const a = make(), b = make();
  const event = (x, y) => ({clientX: x, clientY: y, preventDefault() {}});
  a.tool.begin(event(0, 0)); a.tool.drag(event(12, 9));
  expect(a.state.layerOffsets.get('layer')).toEqual({x: 12, y: 9});
  expect(b.state.layerOffsets.get('layer')).toEqual({x: 0, y: 0});
  a.lifetime.dispose();
  expect(a.state.moving).toBe(false);
  a.tool.begin(event(0, 0)); a.tool.drag(event(20, 20));
  expect(a.saves()).toBe(1);
  b.tool.begin(event(0, 0)); b.tool.drag(event(4, 3));
  expect(b.state.layerOffsets.get('layer')).toEqual({x: 4, y: 3});
});

import { createTransformSession } from '../static/js/editor/tools/transform-session.js';
import { mergeLayerDownAtIndex, wireMergeButtons } from '../static/js/editor/wire-merge-buttons.js';

test('transform cancellation, retired popup and merge stay in the originating session', () => {
  let frames = new Map(), next = 0;
  class Element extends EventTarget {
    constructor() { super(); this.style = {setProperty(){}}; this.classList = {add(){},remove(){},toggle(){}}; this.dataset = {}; this.value = ''; }
    setAttribute() {} remove() {} appendChild() {} querySelectorAll() { return []; }
    querySelector(id) { return this.nodes?.get(id) || this.nodes?.get(id.match(/#ge-[\w-]+/)?.[0]) || null; }
  }
  const doc = new EventTarget(); doc.body = new Element(); doc.defaultView = {matchMedia: () => ({matches:false}), requestAnimationFrame: fn => {frames.set(++next, fn); return next;}, cancelAnimationFrame: id => frames.delete(id)};
  doc.createElement = kind => {
    if (kind === 'canvas') {
      const canvas = {width:0,height:0,pixel:null};
      canvas.getContext = () => ({drawImage: source => {canvas.pixel = source.pixel;}, clearRect: () => {canvas.pixel = null;}, save(){},restore(){},translate(){},rotate(){},scale(){}});
      return canvas;
    }
    const pop = new Element(); pop.ownerDocument = doc; pop.nodes = new Map();
    for (const id of ['#ge-transform-w','#ge-transform-h','#ge-transform-rot','#ge-transform-aspect','#ge-transform-apply','#ge-transform-cancel','#ge-transform-cancel-btn','#ge-transform-min','#ge-transform-flip-h','#ge-transform-flip-v','#ge-transform-rot-90','[data-transform-drag]']) pop.nodes.set(id, new Element());
    return pop;
  };
  const make = (pixel, gesture, extra = {}) => {
    const state = createEditorState(), lifetime = createEditorLifetime(), root = new Element(); root.ownerDocument = doc; state.container = root;
    const canvas = doc.createElement('canvas'); canvas.width = 10; canvas.height = 8; canvas.pixel = pixel;
    const layer = {id:'x',canvas,ctx:canvas.getContext('2d'),opacity:1,visible:true}; state.layers.push(layer); state.layerOffsets.set('x',{x:2,y:3});
    const transform = createTransformSession({state,root,lifetime,gesture,activeLayer:()=>layer,saveState:()=>state.undoStack.push({}),composite(){},fitZoom(){},drawTransformHandles(){},showCanvasLoading(){},hideCanvasLoading(){},uiModule:{showToast(){}},...extra});
    return {state,lifetime,layer,transform};
  };
  const a = make('A'), b = make('B');
  a.transform.startTransform(); b.transform.startTransform();
  const popup = a.state.transformPopup, width = popup.querySelector('#ge-transform-w');
  a.layer.canvas.pixel = 'edited'; a.state.layerOffsets.set('x',{x:9,y:9});
  popup.querySelector('#ge-transform-rot-90').dispatchEvent(new Event('click'));
  expect(frames.size).toBe(1);
  a.lifetime.dispose();
  expect(a.layer.canvas.pixel).toBe('A'); expect(a.state.layerOffsets.get('x')).toEqual({x:2,y:3});
  expect(frames.size).toBe(0); expect(a.state.undoStack.length).toBe(0);
  width.value = '50'; width.dispatchEvent(new Event('input'));
  expect(a.layer.canvas.pixel).toBe('A'); expect(b.layer.canvas.pixel).toBe('B');
  const upper = doc.createElement('canvas'); upper.pixel = 'upper';
  b.state.layers.push({id:'upper',canvas:upper,ctx:upper.getContext('2d'),opacity:1,visible:true});
  mergeLayerDownAtIndex(1, null, b.state, doc);
  expect(b.state.layers.length).toBe(1); expect(a.state.layers.length).toBe(1);
  b.lifetime.dispose();
  const c = make('original');
  c.transform.startTransform();
  c.layer.canvas.pixel = 'newer'; c.state.sceneRevision++;
  c.transform.reapplyTransform(); c.transform.confirmTransform(); c.transform.cancelTransform();
  expect(c.layer.canvas.pixel).toBe('newer');
  expect(c.state.transformActive).toBe(false);
  c.lifetime.dispose();
  const shared = createEditorState(), scene = createImageSceneAuthority({state:shared});
  const makeGesture = operation => { let token; const view = {}; return {begin() { token=scene.beginGesture(view,operation); return !!token; }, owns:() => scene.ownsGesture(token), end() { const old=token; token=null; return scene.endGesture(old); }}; };
  const transformGesture=makeGesture('transform'), strokeGesture=makeGesture('stroke'), d=make('shared',transformGesture);
  const strokeState=createEditorState(), strokeLifetime=createEditorLifetime(); strokeState.tool='brush';
  strokeState.mainCanvas={width:10,height:10,getBoundingClientRect:()=>({left:0,top:0,width:10,height:10})};
  const stroke=createStrokeTool({state:strokeState,root:{},lifetime:strokeLifetime,gesture:strokeGesture,saveState(){},strokeTo(){d.layer.canvas.pixel='stroke';},composite(){},markSceneChanged:()=>scene.markChanged()});
  const event={clientX:1,clientY:1,preventDefault(){}};
  d.transform.startTransform(); stroke.tryBegin(event);
  expect(d.layer.canvas.pixel).toBe('shared'); expect(strokeState.drawing).toBe(false);
  d.lifetime.dispose(); stroke.tryBegin(event); expect(d.layer.canvas.pixel).toBe('stroke');
  expect(scene.endGesture({})).toBe(false); strokeLifetime.dispose(); stroke.tryContinue(event);
  expect(d.layer.canvas.pixel).toBe('stroke'); expect(scene.canApplyAt(scene.revision)).toBe(true);
  expect(scene.dispose()).toBe(true);
  let sharedScene; const view={}, transformOwner={token:null,begin(){this.token=sharedScene.beginGesture(view,'transform');return !!this.token;},owns(){return sharedScene.ownsGesture(this.token);},end(){const token=this.token;this.token=null;return sharedScene.endGesture(token);}};
  const e=make('transform-original',transformOwner,{sceneRevision:()=>sharedScene.revision,markSceneChanged:()=>sharedScene.markChanged()}); sharedScene=createImageSceneAuthority({state:e.state});
  e.state.mainCanvas={width:10,height:8,getBoundingClientRect:()=>({left:0,top:0,width:10,height:8})};
  const move=createMoveTool({state:e.state,root:{},lifetime:e.lifetime,activeLayer:()=>e.layer,saveState(){throw new Error('borrowed move must not create history');},composite(){},sceneRevision:()=>sharedScene.revision,borrowTransform:layer=>e.transform.borrowMove(layer)});
  e.transform.startTransform(); move.begin({clientX:0,clientY:0},{borrowed:true}); move.drag({clientX:3,clientY:4,preventDefault(){}}); move.end();
  expect(e.state.layerOffsets.get('x')).toEqual({x:5,y:7}); expect(sharedScene.revision).toBe(0); expect(e.state.undoStack.length).toBe(1);
  e.transform.cancelTransform(); expect(e.state.layerOffsets.get('x')).toEqual({x:2,y:3}); expect(e.state.undoStack.length).toBe(0); expect(sharedScene.revision).toBe(1);
  e.lifetime.dispose(); expect(sharedScene.dispose()).toBe(true);
});


test('merge toolbar respects the scene owner and current target', () => {
  const state = createEditorState(), lifetime = createEditorLifetime();
  const scene = createImageSceneAuthority({state}), button = new EventTarget();
  const doc = {createElement() { const canvas = {width: 2, height: 2}; canvas.getContext = () => ({drawImage(){}, clearRect(){}}); return canvas; }};
  const root = {ownerDocument: doc, querySelector: selector => selector.includes('#ge-merge-down') ? button : null};
  const layer = id => { const canvas = doc.createElement('canvas'); return {id, canvas, ctx: canvas.getContext('2d'), opacity: 1, visible: true}; };
  const lower = layer('lower'), upper = layer('upper');
  state.imgWidth = 2; state.imgHeight = 2; state.layers = [lower, upper]; state.activeLayerId = upper.id;
  let saves = 0;
  const withSceneMutation = (name, fn) => { const token = scene.beginGesture(root, name); if (!token) return false; try { return fn(); } finally { scene.endGesture(token); } };
  wireMergeButtons({state, root, lifetime, withSceneMutation, saveState: () => saves++, createLayer: layer, renderLayerPanel(){}, composite(){}, renderLayer: l => l.canvas, uiModule: {showToast(){}}, markSceneChanged: () => scene.markChanged()});
  const competing = scene.beginGesture({}, 'stroke');
  button.dispatchEvent(new Event('click'));
  expect(state.layers).toEqual([lower, upper]); expect(saves).toBe(0);
  scene.endGesture(competing); button.dispatchEvent(new Event('click'));
  expect(state.layers).toEqual([lower]); expect(saves).toBe(1); expect(scene.revision).toBe(1);
  lifetime.dispose(); expect(scene.dispose()).toBe(true);
});

import { createCanvasTransforms } from '../static/js/editor/canvas-transforms.js';
test('deferred document transform rejects changed or contended origin', () => {
  const state=createEditorState(), lifetime=createEditorLifetime(), scene=createImageSceneAuthority({state});
  let callback, saves=0, hidden=0; const canvas=()=>{const c={width:4,height:2}; c.getContext=()=>({translate(){},rotate(){},scale(){},drawImage(){}}); return c;};
  const doc={createElement:canvas,defaultView:{requestAnimationFrame:fn=>(callback=fn,1),cancelAnimationFrame(){}}};
  const root={ownerDocument:doc,querySelector(){return null;}};
  const base=canvas(), mask=canvas(), layer={id:'a',canvas:base,ctx:base.getContext('2d'),masks:[{id:'m',canvas:mask,ctx:mask.getContext('2d')} ]};
  state.layers=[layer]; state.imgWidth=4; state.imgHeight=2; state.maskCanvas=mask; state.maskCtx=mask.getContext('2d');
  const owner={}, mutate=(name,fn)=>{const t=scene.beginGesture(owner,name); if(!t)return false; try{return fn();}finally{scene.endGesture(t);}};
  const transforms=createCanvasTransforms({state,root,lifetime,withSceneMutation:mutate,captureOrigin:()=>scene.revision,originCurrent:r=>r===scene.revision,saveState:()=>saves++,composite(){},fitZoom(){},showCanvasLoading:()=>1,hideCanvasLoading:()=>hidden++,markSceneChanged:()=>scene.markChanged()});
  transforms.rotateAll(90); scene.markChanged(); callback(); expect(saves).toBe(0); expect(layer.canvas).toBe(base);
  transforms.rotateAll(90); const competing=scene.beginGesture({},'stroke'); callback(); expect(saves).toBe(0); expect(layer.canvas).toBe(base); scene.endGesture(competing);
  transforms.rotateAll(90); callback(); expect(saves).toBe(1); expect(state.imgWidth).toBe(2); expect(state.maskCanvas).toBe(layer.masks[0].canvas); expect(state.maskCanvas).not.toBe(mask); expect(hidden).toBe(3);
  lifetime.dispose(); expect(scene.dispose()).toBe(true);
});

test('document upscale keeps local offsets and document masks in one owned commit', () => {
  const state=createEditorState(), lifetime=createEditorLifetime(), scene=createImageSceneAuthority({state}); let saves=0;
  const canvas=(w=1,h=1)=>{const c={width:w,height:h};c.getContext=()=>({drawImage(){}});return c;};
  const doc={createElement:()=>canvas()}, root={ownerDocument:doc,querySelector(){return null;}};
  const mask=canvas(4,2), local=canvas(2,1), source={ai:canvas(4,2),base:canvas(4,2),mask:canvas(4,2),padPx:3}, layer={id:'a',canvas:local,ctx:local.getContext('2d'),masks:[{canvas:mask,ctx:mask.getContext('2d')}],inpaintSource:source};
  state.imgWidth=4;state.imgHeight=2;state.layers=[layer];state.layerOffsets.set('a',{x:1,y:-1});state.maskCanvas=mask;state.maskCtx=mask.getContext('2d');
  const transforms=createCanvasTransforms({state,root,lifetime,captureOrigin:()=>scene.revision,originCurrent:v=>v===scene.revision,withSceneMutation:(name,fn)=>{const token=scene.beginGesture(root,name);if(!token)return false;try{return fn();}finally{scene.endGesture(token);}},saveState:()=>saves++,markSceneChanged:()=>scene.markChanged(),composite(){},fitZoom(){},showCanvasLoading(){},hideCanvasLoading(){}});
  const held=scene.beginGesture({},'stroke');expect(transforms.scaleAll(2)).toBe(false);expect(saves).toBe(0);scene.endGesture(held);
  expect(transforms.scaleAll(2)).toBe(true);expect(saves).toBe(1);expect([state.imgWidth,state.imgHeight]).toEqual([8,4]);expect([layer.canvas.width,layer.canvas.height]).toEqual([4,2]);expect(state.layerOffsets.get('a')).toEqual({x:2,y:-2});expect([state.maskCanvas.width,state.maskCanvas.height]).toEqual([8,4]);expect(layer.inpaintSource.padPx).toBe(6);expect(state.maskCanvas).toBe(layer.masks[0].canvas);
  lifetime.dispose();scene.dispose();
});

import { wireTopbarMenus } from '../static/js/editor/wire-topbar-menus.js';
test('canvas bounds dialog rejects stale origin and preserves mask and sidecar', async () => {
  const state=createEditorState(), lifetime=createEditorLifetime(), scene=createImageSceneAuthority({state}); let saves=0,resolveDialog,disabled=0;
  const canvas=(w=2,h=2)=>{const c={width:w,height:h,pixel:'original'};c.getContext=()=>({drawImage(source){c.pixel=source.pixel;}});return c;};
  const doc=new EventTarget();doc.createElement=()=>canvas();const root={ownerDocument:doc,querySelector:()=>null};
  const mask=canvas(4,3), layerCanvas=canvas(2,2), sidecar={ai:canvas(4,3),base:canvas(4,3),mask:canvas(4,3),padPx:2}, layer={id:'a',canvas:layerCanvas,ctx:layerCanvas.getContext('2d'),masks:[{canvas:mask,ctx:mask.getContext('2d')}],inpaintSource:sidecar};
  state.layers=[layer];state.imgWidth=4;state.imgHeight=3;state.layerOffsets.set('a',{x:1,y:2});state.maskCanvas=mask;state.maskCtx=mask.getContext('2d');
  const transforms=createCanvasTransforms({state,root,lifetime,captureOrigin:()=>scene.revision,originCurrent:v=>v===scene.revision,withSceneMutation:(name,fn)=>{const token=scene.beginGesture(root,name);if(!token)return false;try{return fn();}finally{scene.endGesture(token);}},saveState:()=>saves++,markSceneChanged:()=>scene.markChanged(),onResize:()=>disabled++,composite(){},fitZoom(){},showCanvasLoading(){},hideCanvasLoading(){}});
  const menu=wireTopbarMenus({state,root,lifetime,captureResizeOrigin:()=>transforms.captureBounds(),resizeBounds:(w,h,origin)=>transforms.resizeBounds(w,h,origin),promptCanvasSize:()=>new Promise(resolve=>{resolveDialog=resolve;}),uiModule:{showToast(){}}});
  const pending=menu.resizeCustomPrompt();scene.markChanged();resolveDialog({w:8,h:6});await pending;expect(saves).toBe(0);expect(state.imgWidth).toBe(4);
  expect(menu.applyResize(8,6)).toBe(true);expect(saves).toBe(1);expect(state.maskCanvas).toBe(layer.masks[0].canvas);expect(state.maskCanvas.pixel).toBe('original');expect([state.maskCanvas.width,state.maskCanvas.height]).toEqual([8,6]);expect(layer.canvas).toBe(layerCanvas);expect(layer.inpaintSource).toBe(sidecar);expect(state.layerOffsets.get('a')).toEqual({x:1,y:2});expect(disabled).toBe(1);
  expect(menu.applyResize(2,2)).toBe(true);expect([state.maskCanvas.width,state.maskCanvas.height]).toEqual([8,6]);expect(menu.applyResize(2,2)).toBe(false);expect(saves).toBe(2);
  lifetime.dispose();scene.dispose();
});

test('upscale rejects unsavable sidecar geometry before staging or history', () => {
  const state=createEditorState(), lifetime=createEditorLifetime(), scene=createImageSceneAuthority({state});let saves=0,allocations=0;
  const image=(w,h)=>({width:w,height:h}), source={ai:image(4,4),base:image(4,4),mask:image(4,4),padPx:41}, layer={id:'a',canvas:image(4,4),masks:[],inpaintSource:source};
  state.layers=[layer];state.imgWidth=4;state.imgHeight=4;const root={ownerDocument:{createElement(){allocations++;return {}; }},querySelector(){return null;}};
  const transforms=createCanvasTransforms({state,root,lifetime,captureOrigin:()=>scene.revision,originCurrent:value=>value===scene.revision,withSceneMutation:(name,fn)=>{const token=scene.beginGesture(root,name);try{return fn();}finally{scene.endGesture(token);}},saveState:()=>saves++,composite(){},fitZoom(){},showCanvasLoading(){},hideCanvasLoading(){}});
  expect(()=>transforms.scaleAll(2)).toThrow('padding exceeds');expect(saves).toBe(0);expect(allocations).toBe(0);expect(layer.inpaintSource).toBe(source);
  source.padPx=1;source.ai.width=4097;expect(()=>transforms.scaleAll(2)).toThrow('dimensions exceed');expect(saves).toBe(0);expect(allocations).toBe(0);expect(state.imgWidth).toBe(4);
  lifetime.dispose();scene.dispose();
});
