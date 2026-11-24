import {test,expect} from 'bun:test';
import {createEditorState,createImageSceneAuthority} from '../static/js/editor/state.js';
import {createImageSessionRegistry} from '../static/js/editor/imageSession.js';

test('one session history invalidates two view projections and retires with owner', () => {
  const state=createEditorState(), session=createImageSessionRegistry().open({scope:{accountId:'a',workspaceId:'w'}});
  const scene=createImageSceneAuthority({state}), release=session.attachSceneAuthority(scene);
  const history=scene.installHistory({capture:()=>({width:state.imgWidth}),restore:snapshot=>{state.imgWidth=snapshot.width;}});
  const first=[],second=[]; const off=scene.subscribeView(revision=>first.push(revision)); scene.subscribeView(revision=>second.push(revision));
  expect(()=>scene.installHistory({})).toThrow(); expect(()=>createImageSceneAuthority({state})).toThrow();
  state.imgWidth=10; history.save('resize'); state.imgWidth=20; scene.markChanged();
  expect(history.undo()).toBe(true); scene.markChanged(); expect(state.imgWidth).toBe(10);
  off(); state.imgWidth=30; scene.markChanged(); expect(first).toEqual([1,2]); expect(second).toEqual([1,2,3]);
  expect(session.getSceneAuthority()).toBe(scene); expect(release()).toBe(true); expect(history.undo()).toBe(false); expect(session.getSceneAuthority()).toBeNull();
});
