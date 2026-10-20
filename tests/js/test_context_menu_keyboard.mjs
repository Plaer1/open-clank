import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
const source=fs.readFileSync('static/js/custom-context-menu.js','utf8');
const start=source.indexOf('function onKeyboardContextMenu(event) {'),end=source.indexOf('\nexport function initCustomContextMenu()',start);
assert(start>=0&&end>start);
function fixture(){
 class Element{closest(){return null;}}
 const target=new Element(),body=new Element(),request={owner:'captured'},calls=[],scope={Element,document:{activeElement:target,body,documentElement:new Element()},MENU_ID:'openclank-context-menu',active:null,enabled:()=>true,capture:e=>{calls.push(['capture',e.target===target,e.detail]);return request;},isCapturedContextCurrent:r=>r===request,renderMenu:r=>{calls.push(['render',r===request]);return true;},closeMenu:o=>calls.push(['close',o.restore])};
 vm.createContext(scope);vm.runInContext(source.slice(start,end)+'\nthis.invoke=onKeyboardContextMenu;',scope);
 const event={key:'F10',shiftKey:true,ctrlKey:false,altKey:false,metaKey:false,defaultPrevented:false,isComposing:false,keyCode:121,repeat:false,preventDefault(){this.defaultPrevented=true;}};
 return {scope,event,calls,target,request};
}
test('keyboard captures focused authority and marks real keyboard modality through existing gateway',()=>{const f=fixture();f.scope.invoke(f.event);assert.equal(f.scope.active,f.request);assert.equal(f.event.defaultPrevented,true);assert.deepEqual(f.calls,[['capture',true,0],['render',true]]);});
test('ContextMenu key works without shift, but additional modifiers/IME/repeat/default prevention do not',()=>{const f=fixture();f.event.key='ContextMenu';f.event.shiftKey=false;f.scope.invoke(f.event);assert.equal(f.event.defaultPrevented,true);for(const change of [{key:'ContextMenu',shiftKey:true},{key:'F10',shiftKey:false},{ctrlKey:true},{altKey:true},{metaKey:true},{isComposing:true},{keyCode:229},{repeat:true},{defaultPrevented:true},{key:'x'}]){const f=fixture();Object.assign(f.event,change);f.scope.invoke(f.event);assert.equal(f.calls.length,0);assert.equal(f.scope.active,null);}});
test('custom off and unknown/noncurrent targets are not prevented or fabricated into a popup',()=>{for(const change of [f=>f.scope.enabled=()=>false,f=>f.scope.document.activeElement=f.scope.document.body,f=>f.scope.document.activeElement=null,f=>f.target.closest=()=>({}),f=>f.scope.isCapturedContextCurrent=()=>false,f=>f.scope.renderMenu=()=>false]){const f=fixture();change(f);f.scope.invoke(f.event);assert.equal(f.scope.active,null);assert.equal(f.event.defaultPrevented,false);}});
test('existing popup closes without retarget restoration before recapturing actual focused owner',()=>{const f=fixture();f.scope.active={owner:'old'};f.scope.invoke(f.event);assert.deepEqual(f.calls[0],['close',false]);assert.equal(f.scope.active,f.request);});
test('listener retains existing Escape and composition fences instead of changing command execution',()=>{assert(source.includes('onKeyboardContextMenu(event);'));assert(source.includes("event.key === 'Escape' && active && !event.defaultPrevented && !event.isComposing"));assert(source.includes('const handled = await request.adapter.execute(command, request);'));});
