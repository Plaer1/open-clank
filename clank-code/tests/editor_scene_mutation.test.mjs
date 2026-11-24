import {test, expect} from 'bun:test';
import {createEditorState, createEditorLifetime, createSceneMutationSignal} from '../static/js/editor/state.js';
import {createMoveTool} from '../static/js/editor/tools/move.js';

test('committed move advances scene revision and schedules without serializing', () => {
  const state=createEditorState(), lifetime=createEditorLifetime(); let scheduled=0;
  const canvas={width:10,height:10,getBoundingClientRect:()=>({left:0,top:0,width:10,height:10})};
  const layer={id:'a',canvas,visible:true}; state.mainCanvas=canvas; state.layers.push(layer); state.layerOffsets.set('a',{x:0,y:0});
  const mark=createSceneMutationSignal({state,onChange:()=>scheduled++});
  const tool=createMoveTool({state,root:{},lifetime,activeLayer:()=>layer,saveState(){},composite(){},markSceneChanged:mark});
  const e=(x,y)=>({clientX:x,clientY:y,preventDefault(){}});
  const captured=state.sceneRevision; tool.begin(e(0,0)); tool.drag(e(2,3)); expect(state.sceneRevision).toBe(captured);
  tool.end(); expect(state.sceneRevision).toBe(captured+1); expect(state.sceneRevision !== captured).toBe(true); expect(scheduled).toBe(1);
  lifetime.dispose(); tool.begin(e(0,0)); tool.end(); expect(scheduled).toBe(1);
});
