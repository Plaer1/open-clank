import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

// Execute the actual close implementation and actual open cancellation prefix
// without importing Settings' unrelated DOM/network startup side effects.
const source=fs.readFileSync(new URL('../../static/js/settings.js',import.meta.url),'utf8');
const cancel=source.slice(source.indexOf('function cancelPendingSettingsClose()'),source.indexOf('\nfunction el('));
const close=source.slice(source.indexOf('export function close()'),source.indexOf('// Handle redirect back from Google OAuth2')).replace('export function','function');
const openPrefix=source.slice(source.indexOf('export function open(tab,'),source.indexOf('  syncAppearanceCheckboxes();',source.indexOf('export function open(tab,'))).replace('export function','function')+'}\n';
const classList=(initial=[])=>{const set=new Set(initial);return{contains:x=>set.has(x),add:x=>set.add(x),remove:x=>set.delete(x)};};
function fixture({content=true,closing=false,hidden=false}={}){
 const timers=new Map(),listeners=new Set(),events=[];let sequence=0;
 const panel={classList:classList(closing?['modal-closing']:[]),addEventListener:(name,fn)=>listeners.add(fn),removeEventListener:(name,fn)=>listeners.delete(fn)};
 const modal={classList:classList(hidden?['hidden']:[]),querySelector:()=>content?panel:null};
 const context=vm.createContext({modalEl:modal,document:{body:{classList:classList(['settings-appearance-open'])}},syncAppearanceOpacity(){},initialized:true,
  setTimeout(fn,ms){assert.equal(ms,250);const id=++sequence;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),
  window:{dispatchEvent(event){events.push({type:event.type,detail:event.detail,hidden:modal.classList.contains('hidden'),closing:panel.classList.contains('modal-closing')});}},CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}}});
 vm.runInContext('let _closeTimer=null,_closeEpoch=0,_closeAnimationTarget=null,_closeAnimationHandler=null;\n'+cancel+close+openPrefix+'globalThis.api={close,open};',context);
 return{...context.api,modal,panel,timers,listeners,events,reopen(){context.api.open();modal.classList.remove('hidden');},animation(target=panel){for(const fn of [...listeners])fn({target});},timer(){for(const fn of [...timers.values()])fn();}};
}
function notified(f){assert.equal(f.events.length,1);assert.equal(f.events[0].type,'openclank:presentation-visibility-changed');assert.equal(f.events[0].detail.source,'settings');assert.equal(f.events[0].detail.visible,false);assert.equal(f.events[0].hidden,true);assert.equal(f.events[0].closing,false);assert.equal(f.timers.size,0);assert.equal(f.listeners.size,0);}
test('unregistered Settings animation completion notifies only after hiding and cleanup',()=>{const f=fixture();f.close();assert.equal(f.events.length,0);assert.equal(f.modal.classList.contains('hidden'),false);f.animation();notified(f);f.timer();assert.equal(f.events.length,1);});
test('no-animation reduced-motion timer emits the same post-hide signal',()=>{const f=fixture();f.close();const oldAnimation=[...f.listeners][0];f.timer();notified(f);oldAnimation();assert.equal(f.events.length,1);});
test('bubbled child animation cannot close Settings or consume its own completion listener',()=>{const f=fixture();f.close();f.animation({});assert.equal(f.modal.classList.contains('hidden'),false);assert.equal(f.events.length,0);assert.equal(f.listeners.size,1);f.animation();notified(f);const g=fixture();g.close();g.animation({});g.timer();notified(g);});
test('immediate close branches notify without manager close side effects; already hidden is silent',()=>{for(const options of [{content:false},{closing:true}]){const f=fixture(options);f.close();notified(f);}const f=fixture({hidden:true,content:false});f.close();assert.equal(f.events.length,0);});
test('actual open prefix invalidates retained animation/timer callbacks before reopening',()=>{const f=fixture();f.close();const animation=[...f.listeners][0],timer=[...f.timers.values()][0];f.reopen();assert.equal(f.listeners.size,0);assert.equal(f.timers.size,0);animation();timer();assert.equal(f.modal.classList.contains('hidden'),false);assert.equal(f.events.length,0);f.close();f.animation();notified(f);});
test('repeated close finishes once and stale earlier completion cannot hide a later open',()=>{const f=fixture();f.close();const stale=[...f.listeners][0];f.close();notified(f);f.reopen();stale();assert.equal(f.modal.classList.contains('hidden'),false);assert.equal(f.events.length,1);});
