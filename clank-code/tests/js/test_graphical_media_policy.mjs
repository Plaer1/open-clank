import test from 'node:test';
import assert from 'node:assert/strict';
import {createPresentationPolicy,GRAPHICS_MODE_KEY,normalizeGraphicsMode} from '../../static/js/graphics/presentationPolicy.js';
import {createSurfaceBudget,GRAPHICAL_SURFACE_LIMITS} from '../../static/js/graphics/surfaceBudget.js';
import {mountRenderingSettings} from '../../static/js/graphics/renderingSettings.js';
test('local preference defaults automatic and cannot claim browser-wide GPU policy',()=>{
  assert.equal(normalizeGraphicsMode('software'),'auto');
  const written=[];const listeners={};const events={addEventListener(k,f){listeners[k]=f;},removeEventListener(k){delete listeners[k];}};
  const policy=createPresentationPolicy({storage:{getItem:()=> 'invalid',setItem:(...a)=>written.push(a)},events});
  assert.equal(policy.mode,'auto');const changes=[];const off=policy.subscribe(v=>changes.push(v));
  policy.set('ordinary');policy.set('ordinary');assert.deepEqual(changes,['ordinary']);assert.deepEqual(written[0],[GRAPHICS_MODE_KEY,'ordinary']);
  off();policy.set('auto');assert.deepEqual(changes,['ordinary']);policy.dispose();assert.deepEqual(listeners,{});
});
test('denied storage is ordinary safe default and listeners are isolated',()=>{
  const policy=createPresentationPolicy({storage:{getItem(){throw Error();},setItem(){throw Error();}}});assert.equal(policy.mode,'auto');
  policy.subscribe(()=>{throw Error();});let seen;policy.subscribe(v=>seen=v);assert.equal(policy.set('ordinary'),'ordinary');assert.equal(seen,'ordinary');policy.dispose();
});
test('cross-document preference uses fixed key and cleans listener',()=>{
  let mode='ordinary',fn;const policy=createPresentationPolicy({storage:{getItem:()=>mode},events:{addEventListener(_,f){fn=f;},removeEventListener(){fn=null;}}});
  mode='auto';fn({key:'unrelated'});assert.equal(policy.mode,'ordinary');fn({key:GRAPHICS_MODE_KEY});assert.equal(policy.mode,'auto');policy.dispose();assert.equal(fn,null);
});
test('context and exact image-byte admission is shared, bounded, idempotently released',()=>{
  const budget=createSurfaceBudget({contexts:2,mediaBytes:20});const first=budget.acquire('files-1'),second=budget.acquire('editor-1');
  assert(first&&second);assert.equal(budget.acquire('graph-1'),null);assert.equal(budget.acquire('files-1'),null);
  assert(first.reserve(12));assert.equal(second.reserve(9),false);assert(second.reserve(8));assert.equal(first.reserve(-1),false);assert.equal(first.reserve(1.5),false);
  first.release();first.release();assert.equal(first.reserve(0),false);assert.deepEqual(budget.snapshot(),{contexts:1,mediaBytes:8,maxContexts:2,maxMediaBytes:20});
  assert(budget.acquire('graph-1'));second.release();assert.equal(budget.snapshot().mediaBytes,0);
});
test('per-region ceiling cannot be bypassed through large shared ceiling',()=>{
  const budget=createSurfaceBudget();const lease=budget.acquire('preview');assert.equal(lease.reserve(GRAPHICAL_SURFACE_LIMITS.regionBytes+1),false);assert(lease.reserve(GRAPHICAL_SURFACE_LIMITS.regionBytes));lease.release();assert.equal(budget.snapshot().contexts,0);
});
test('Appearance control persists only browser rendering mode and renders fixed truthful text',()=>{
  const policy=createPresentationPolicy(),input={value:'',addEventListener(_,fn){this.change=fn;}},status={textContent:''},root={dataset:{},querySelector:selector=>selector.includes('mode')?input:status};
  mountRenderingSettings(root,{policy});assert.equal(input.value,'auto');assert.match(status.textContent,/Text editing remains DOM/);
  input.value='ordinary';input.change();assert.equal(policy.mode,'ordinary');assert.match(status.textContent,/Browser\/OS compositing may still use a GPU/);
  input.value='<private>';input.change();assert.equal(policy.mode,'auto');assert.doesNotMatch(status.textContent,/private/);policy.dispose();
});
