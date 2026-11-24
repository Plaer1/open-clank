import { test } from 'bun:test';
import { strict as assert } from 'node:assert';
import { createEditorState, createEditorHistory, createEditorLifetime, state as legacyState } from '../static/js/editor/state.js';
import { wireCanvasEvents } from '../static/js/editor/canvas-events.js';
import { wireKeyboardShortcuts } from '../static/js/editor/keyboard-shortcuts.js';

class Surface extends EventTarget {
  constructor(doc = null) { super(); this.ownerDocument=doc; this.style={}; this.dataset={}; }
  getClientRects() { return [{}]; }
  contains(node) { return node === this; }
  querySelector() { return null; }
  setPointerCapture() {}
  releasePointerCapture() {}
}
function keyEvent(key) {
  const event=new Event('keydown',{cancelable:true});
  Object.defineProperties(event,{key:{value:key},ctrlKey:{value:false},metaKey:{value:false},altKey:{value:false},shiftKey:{value:false}});
  return event;
}

test('independent core history and focus-owned input retire with their view', () => {
  const a=createEditorState(), b=createEditorState();
  assert.notEqual(a,legacyState); assert.notEqual(a.layers,b.layers); assert.notEqual(a.layerOffsets,b.layerOffsets);
  assert.notEqual(a.undoStack,b.undoStack); assert.notEqual(a.redoStack,b.redoStack);
  assert.notEqual(a.editorDocClickHandlers,b.editorDocClickHandlers); assert.notEqual(a.lassoPoints,b.lassoPoints);
  const lifetimeA=createEditorLifetime(), lifetimeB=createEditorLifetime();
  const history = (state,lifetime) => createEditorHistory({state,lifetime,capture:()=>({value:state.imgWidth}),restore:snapshot=>{state.imgWidth=snapshot.value;},now:()=>1});
  const ha=history(a,lifetimeA), hb=history(b,lifetimeB);
  a.imgWidth=10; b.imgWidth=20; ha.save('A'); hb.save('B'); a.imgWidth=11; b.imgWidth=21;
  assert.equal(ha.undo(),true); assert.equal(a.imgWidth,10); assert.equal(b.imgWidth,21);
  assert.equal(hb.undo(),true); assert.equal(b.imgWidth,20); assert.equal(ha.redo(),true); assert.equal(a.imgWidth,11);

  const doc=new Surface(); const win=new Surface(); doc.defaultView=win; doc.body={};
  const rootA=new Surface(doc), rootB=new Surface(doc); doc.activeElement=rootA;
  a.container=rootA; b.container=rootB; a.editorOpen=b.editorOpen=true;
  let keysA=0, keysB=0;
  const deps={toolbar:{querySelector:()=>null},toolKeyMap:{},toggleShortcuts:()=>{},undo:()=>{},redo:()=>{}};
  wireKeyboardShortcuts({...deps,state:a,root:rootA,lifetime:lifetimeA,toggleShortcuts:()=>keysA++});
  wireKeyboardShortcuts({...deps,state:b,root:rootB,lifetime:lifetimeB,toggleShortcuts:()=>keysB++});
  doc.dispatchEvent(keyEvent('?')); assert.equal(keysA,1); assert.equal(keysB,0);
  doc.activeElement=rootB; doc.dispatchEvent(keyEvent('?')); assert.equal(keysA,1); assert.equal(keysB,1);

  const canvas=new Surface(doc), area=new Surface(doc); a.mainCanvas=canvas;
  let began=0, continued=0, ended=0;
  wireCanvasEvents({state:a,root:rootA,lifetime:lifetimeA,canvasArea:area,beginDraw:()=>began++,continueDraw:()=>continued++,endDraw:()=>ended++,updateBrushCursor:()=>{}});
  canvas.dispatchEvent(new Event('mousedown')); win.dispatchEvent(new Event('mousemove'));
  assert.equal(began,1); assert.equal(continued,1);
  assert.equal(lifetimeA.dispose(),true); assert.equal(lifetimeA.dispose(),false);
  win.dispatchEvent(new Event('mousemove')); win.dispatchEvent(new Event('mouseup')); canvas.dispatchEvent(new Event('mousedown'));
  assert.equal(began,1); assert.equal(continued,1); assert.equal(ended,0);
  doc.activeElement=rootA; doc.dispatchEvent(keyEvent('?')); assert.equal(keysA,1); assert.equal(keysB,1);
  assert.equal(ha.undo(),false); assert.equal(hb.redo(),true); assert.equal(b.imgWidth,21);
  lifetimeB.dispose();
});
