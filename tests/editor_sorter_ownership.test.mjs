import {test, expect} from 'bun:test';
import {createEditorLifetime} from '../static/js/editor/state.js';

test('independent list elements retain separate sorter lifetimes', async () => {
  globalThis.window ||= {}; globalThis.document ||= new EventTarget();
  const {enable}=await import('../static/js/dragSort.js');
  class List extends EventTarget { querySelectorAll(){return [];} }
  const a=new List(), b=new List(), lifeA=createEditorLifetime(), lifeB=createEditorLifetime();
  const sa=enable(a,'.row',{instanceKey:a,lifetime:lifeA}), sb=enable(b,'.row',{instanceKey:b,lifetime:lifeB});
  expect(sa).toBeDefined(); expect(sb).toBeDefined();
  lifeA.dispose(); expect(sa.cleanup()).toBeUndefined(); expect(lifeB.disposed).toBe(false);
  expect(sb.refresh()).toBeUndefined(); lifeB.dispose();
});

test('editor ID namespace keeps label and ARIA references in its owner', async () => {
  const {namespaceEditorIds,editorIdSelector}=await import('../static/js/editor/dom-identity.js');
  class Node { constructor(attrs={}){this.attrs={...attrs};this.children=[];} getAttribute(k){return this.attrs[k]??null;} hasAttribute(k){return k in this.attrs;} setAttribute(k,v){this.attrs[k]=v;} get id(){return this.attrs.id||''} set id(v){this.attrs.id=v} querySelectorAll(){return this.children;} }
  const make=()=>{const root=new Node(), input=new Node({id:'ge-color'}), label=new Node({for:'ge-color','aria-controls':'ge-color'});root.children=[input,label];return {root,input,label};};
  const a=make(),b=make(); namespaceEditorIds(a.root,'owner-a'); namespaceEditorIds(b.root,'owner-b');
  expect(a.input.id).toBe('owner-a--ge-color'); expect(b.input.id).toBe('owner-b--ge-color');
  expect(a.label.getAttribute('for')).toBe(a.input.id); expect(b.label.getAttribute('aria-controls')).toBe(b.input.id);
  namespaceEditorIds(a.root,'owner-a'); expect(a.label.getAttribute('for')).toBe(a.input.id);
  expect(editorIdSelector('ge-color')).toContain('[data-ge-id="ge-color"]');
});
