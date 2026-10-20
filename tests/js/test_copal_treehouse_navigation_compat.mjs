// Execute actual shared Copal init/window/appearance functions with bounded
// dependency fixtures. No browser, runtime, Treehouse donor or native process.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const source=fs.readFileSync(new URL('../../static/js/copal.js',import.meta.url),'utf8');
function section(start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert(a>=0&&b>a);return source.slice(a,b).replace(/^export /gm,'');}
const actual=[section('function ensureViewWindow(view)','function buildWorkspace()'),section('function buildWorkspace()','// --- Per-entry Copal visibility'),section('function defaultEntryVisibility()','async function saveEntryVisibility()'),section('export function whenReady()','function bindSidebar()'),section('export async function init(','export function getNotesSettings()')].join('\n');

function fixture(hook){
 const events=new Map(),calls=[],windows=[],state={contextEpoch:0,windows:new Map(),docs:[],entryVisibility:null,entryVisibilityError:null};
 const window={location:{href:''},addEventListener(type,fn){if(!events.has(type))events.set(type,[]);events.get(type).push(fn);},dispatchEvent(event){for(const fn of events.get(event.type)||[])fn(event);}};
 const treeHouse={loadState(){calls.push('treehouse-state');},refreshEngagement(){},stopEngagement(){}};
 if(hook!==undefined)treeHouse.setNavigationWindow=hook;
 const deps={state,window,treeHouse,Worker:undefined,planningFeature:{loadState(){calls.push('planning-state');}},VIEWS:['treehouse','graph'],COPAL_LAUNCHER_IDS:['treehouse','graph'],LABELS:{treehouse:'TreeHouse',graph:'Graph'},ENTRY_VIS_KEY:'copal_entry_visibility',
  suspendCopalScope(){calls.push('suspend');},handleContextObjectCommand(){},configureCopalStorage(){calls.push('storage');},copalStorageKey:v=>v,localStorage:{getItem:()=>null,setItem(){}},
  api:async route=>{assert.equal(route,'/status');calls.push('status');return{storage_namespace:'synthetic-init',account_id:'synthetic-account'};},
  createCopalWindow(options){const w={id:options.id,root:{addEventListener(){}},body:{},actions:{append(){}}};windows.push(w);return w;},
  h:()=>({addEventListener(){}}),resolveView:v=>v,featureFor:()=>null,activateView(){},markActive(){},renderView(){},createDocument(){},importVault(){},loadDocuments(){},open(){},
  bindSidebar(){calls.push('sidebar');state.navigationEvents=new AbortController();},connectEvents(){calls.push('events');},applyEntryVisibility(){calls.push('appearance-applied');},
  location:{pathname:'/',search:''},resolveAppletLocation:()=>null,CustomEvent:class{constructor(type){this.type=type;}},
  fetch:async route=>{assert.equal(route,'/api/prefs/copal_entry_visibility');calls.push('appearance-read');return{ok:true,json:async()=>({value:{}})};},
 };
 const functions=new Function('deps',`const {${Object.keys(deps).join(',')}}=deps;${actual};return{init,whenReady,ensureViewWindow};`)(deps);
 return{...functions,state,calls,windows,treeHouse};
}

test('legacy selected Treehouse API allows real init to finish appearance readiness and later Graph construction',async()=>{
 const f=fixture();let ready=false;const completion=f.whenReady().then(()=>{ready=true;});await f.init('http://synthetic.invalid');await completion;
 assert.equal(ready,true);assert.equal(f.state.storageNamespace,'synthetic-init');assert.deepEqual(f.state.entryVisibility,{treehouse:true,graph:true});
 assert.deepEqual(f.windows.map(w=>w.id),['copal-treehouse-modal','copal-graph-modal']);assert.deepEqual(f.calls,['suspend','status','storage','planning-state','treehouse-state','sidebar','events','appearance-read','appearance-applied']);
});

test('newer navigation capability receives exact owning window once with correct receiver',async()=>{
 const received=[];let feature;const f=fixture(function(window){received.push({receiver:this,window});});feature=f.treeHouse;await f.init('http://synthetic.invalid');await f.whenReady();
 assert.equal(received.length,1);assert.equal(received[0].receiver,feature);assert.equal(received[0].window,f.windows[0]);assert.equal(received[0].window.id,'copal-treehouse-modal');
 f.ensureViewWindow('treehouse');assert.equal(received.length,1,'Existing window is not rebound or duplicated');assert(f.calls.includes('appearance-applied'));
});

test('non-callable optional property cannot block legacy initialization',async()=>{for(const value of [null,false,{},'not-a-capability']){const f=fixture(value);await f.init('http://synthetic.invalid');await f.whenReady();assert(f.calls.includes('appearance-applied'));}});

test('real newer capability failure is not swallowed or converted to successful readiness',async()=>{
 const failure=Error('synthetic navigation failure'),f=fixture(()=>{throw failure;});await assert.rejects(f.init('http://synthetic.invalid'),e=>e===failure);assert.equal(f.state.entryVisibility,null);assert(!f.calls.includes('appearance-read'));
});
