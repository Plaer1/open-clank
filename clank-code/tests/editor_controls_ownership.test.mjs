import { test, expect } from 'bun:test';
import { createEditorState, createEditorLifetime } from '../static/js/editor/state.js';
import { wireStrokeToolSliders } from '../static/js/editor/stroke-tool-sliders.js';

test('slider events stay in their root and stop at retirement', () => {
  const make = () => {
    const state = createEditorState(), lifetime = createEditorLifetime(), nodes = new Map();
    for (const suffix of ['opacity','opacity-label','flow','flow-label','softness','softness-label','preview-opacity','preview-flow','preview-softness']) {
      const node = new EventTarget(); node.style = {}; node.value = '42'; node.textContent = ''; nodes.set('#ge-brush-' + suffix, node);
    }
    const root = {querySelector: selector => nodes.get(selector) || null};
    wireStrokeToolSliders({state, root, lifetime});
    return {state, lifetime, slider:nodes.get('#ge-brush-opacity')};
  };
  const a = make(), b = make(), before = b.state.brushOpacity;
  a.slider.dispatchEvent(new Event('input'));
  expect(a.state.brushOpacity).toBe(42); expect(b.state.brushOpacity).toBe(before);
  a.lifetime.dispose(); a.slider.value = '77'; a.slider.dispatchEvent(new Event('input'));
  expect(a.state.brushOpacity).toBe(42);
});

test('deferred inpaint edge refuses a changed originating scene', async () => {
  const {wireInpaintControls}=await import('../static/js/editor/wire-inpaint-controls.js');
  const state=createEditorState(), lifetime=createEditorLifetime(), nodes=new Map(); let frame, writes=0, saves=0;
  for (const id of ['ge-feather-slider','ge-feather-label','ge-edgestroke-slider']) { const node=new EventTarget();node.value='2';node.style={};node.classList={toggle(){}};nodes.set(id,node); }
  const win={requestAnimationFrame:fn=>(frame=fn,1),cancelAnimationFrame(){}}, doc={defaultView:win,createElement(){return {width:0,height:0,getContext(){return {clearRect(){},drawImage(){}};}};}};
  const root={ownerDocument:doc,querySelector:selector=>nodes.get(selector.match(/#(ge-[\w-]+)/)?.[1])||null};
  const image={width:2,height:2,getContext(){}}, source={ai:image,base:image,mask:image,padPx:2}, layer={id:'x',canvas:image,ctx:{clearRect(){writes++;},drawImage(){}},inpaintSource:source};
  state.imgWidth=2;state.imgHeight=2;state.layers=[layer];state.lastInpaintLayerId='x';
  wireInpaintControls({state,root,lifetime,captureOrigin:()=>state.sceneRevision,originCurrent:value=>value===state.sceneRevision,withSceneMutation:(_name,fn)=>fn(),saveState:()=>saves++,applyInpaintFeather(){},composite(){},syncToolClearIndicators(){},attachColorPicker(){},uiModule:{showToast(){}}});
  nodes.get('ge-feather-slider').dispatchEvent(new Event('input')); state.sceneRevision++; frame();
  expect(writes).toBe(0);expect(saves).toBe(0);
  nodes.get('ge-feather-slider').dispatchEvent(new Event('input'));frame();expect(writes).toBe(1);expect(saves).toBe(1); lifetime.dispose();
});
