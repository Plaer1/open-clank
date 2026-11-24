import {test, expect} from 'bun:test';
import {createEditorLifetime} from '../static/js/editor/state.js';
import {wireAIModelSelectors} from '../static/js/editor/ai-models.js';

test('retired or account-changed model response cannot populate selector', async () => {
  const doc = new EventTarget(); doc.body = {dataset:{accountId:'a'}};
  const select = {value:'', ownerDocument:doc, children:[], replaceChildren(){this.children=[];}, appendChild(node){this.children.push(node);}};
  doc.createElement = () => ({});
  const root = new EventTarget(); root.ownerDocument=doc; root.isConnected=true; root.querySelector = q => q === '#ge-ai-model' ? select : null; root.querySelectorAll = () => [];
  let resolve; const deferred = new Promise(r => {resolve=r;}); const lifetime=createEditorLifetime();
  wireAIModelSelectors({container:root,apiBase:'',lifetime,fetchImpl:()=>deferred});
  doc.body.dataset.accountId='b'; doc.dispatchEvent(new Event('openclank:auth-context-changed'));
  resolve({ok:true,json:async()=>({models:[{id:'r',operations:['image.generate']}]})});
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();
  expect(select.children.length).toBe(0);
  expect(lifetime.disposed).toBe(false);
});
