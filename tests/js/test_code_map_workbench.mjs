// Integration/lifecycle over a deliberately tiny DOM. No raster, layout,
// browser throughput or native transport is simulated as passing evidence.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {webcrypto} from 'node:crypto';
import {setMaxListeners} from 'node:events';
import {mountCodeMapWorkbench,codeMapVisible,codeMapMayRead,codeMapStatus,codeMapIndexDiagnostics} from '../../static/js/copal/codeMapWorkbench.js';
import {mountCodeGraphWorkbench} from '../../static/js/copal/codeGraphWorkbench.js';
globalThis.crypto??=webcrypto;
class MiniNode {
 constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.attributes=new Map();this.dataset={};this.style={};this.listeners=new Map();this.parentNode=null;this.hidden=false;this.disabled=false;this.value='';this._text='';this.classList={add:(...names)=>{this.className=[...(this.className||'').split(' '),...names].filter(Boolean).join(' ');}};}
 get isConnected(){return this._connected===true||this.parentNode?.isConnected===true;}
 set textContent(value){this.replaceChildren();this._text=String(value);}
 get textContent(){return this._text+this.children.map(n=>typeof n==='string'?n:n.textContent).join('');}
 setAttribute(k,v){this.attributes.set(k,String(v));if(k==='hidden')this.hidden=true;if(k==='disabled')this.disabled=true;if(k==='tabindex')this.tabIndex=Number(v);if(k.startsWith('data-'))this.dataset[k.slice(5).replace(/-([a-z])/g,(_,x)=>x.toUpperCase())]=String(v);}
 getAttribute(k){return this.attributes.get(k)??null;}
 append(...children){for(const child of children.flat()){if(typeof child!=='string'){child.remove();child.parentNode=this;}this.children.push(child);}}
 replaceChildren(...children){for(const child of this.children)if(typeof child!=='string'){if(child.contains(globalThis.document?.activeElement))globalThis.document.activeElement=globalThis.document.body;child.parentNode=null;}this.children=[];this._text='';this.append(...children);}
 remove(){if(this.parentNode){const p=this.parentNode;p.children=p.children.filter(n=>n!==this);this.parentNode=null;}}
 contains(node){return this===node||this.children.some(n=>typeof n!=='string'&&n.contains(node));}
 matches(selector){if(selector.includes(' ')){const split=selector.lastIndexOf(' '),parentSelector=selector.slice(0,split),own=selector.slice(split+1);if(!this.matches(own))return false;for(let p=this.parentNode;p;p=p.parentNode)if(p.matches(parentSelector))return true;return false;}if(selector.startsWith('[')){const m=selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);return m&&this.attributes.has(m[1])&&(m[2]===undefined||this.getAttribute(m[1])===m[2]);}if(selector.startsWith('.'))return (this.className||'').split(' ').includes(selector.slice(1));return this.tagName.toLowerCase()===selector;}
 querySelectorAll(selector){const found=[];for(const child of this.children)if(typeof child!=='string'){if(child.matches(selector))found.push(child);found.push(...child.querySelectorAll(selector));}return found;}
 querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
 addEventListener(type,fn,options={}){if(!this.listeners.has(type))this.listeners.set(type,new Set());this.listeners.get(type).add(fn);if(options.signal){setMaxListeners(0,options.signal);options.signal.addEventListener('abort',()=>this.listeners.get(type)?.delete(fn),{once:true});}}
 async fire(type,event={}){const value={target:this,preventDefault(){},stopPropagation(){},...event};if(typeof this['on'+type]==='function')await this['on'+type](value);for(const fn of [...this.listeners.get(type)||[]])await fn(value);}
 focus(){if(this.isConnected)document.activeElement=this;}
 getBoundingClientRect(){return {left:0,top:0,width:900,height:600};}
}
function dom(){const body=new MiniNode('body');body._connected=true;globalThis.document={body,activeElement:body,createElement:tag=>new MiniNode(tag),createElementNS:(_ns,tag)=>new MiniNode(tag)};globalThis.window={devicePixelRatio:1};globalThis.ResizeObserver=class{observe(){}disconnect(){this.closed=true;}};globalThis.MutationObserver=class{observe(){}disconnect(){this.closed=true;}};const root=new MiniNode('main');body.append(root);return root;}
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
const base=new URL('../../mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/',import.meta.url);
const golden=name=>JSON.parse(fs.readFileSync(new URL(`code_map_${name}.json`,base),'utf8'));
function measure(v){for(let i=0;i<8;i++){const n=Buffer.byteLength(JSON.stringify(v));if(v.cost.serialized_bytes===n)return v;v.cost.serialized_bytes=n;}throw Error('fixture size');}
const pending={lifecycle:'building',registration_id:'fixture-registration',admission_epoch:1,policy_generation:1};
const ready={...pending,lifecycle:'ready',head_generation:'fixture-head'};
test('overflow renderer suffix reports visible/total and explicit pan or Explore, never full fit',async()=>{
 const f=fixture();await f.status(pending);const report=f.scenes[0].options.onChange;
 report({status:'ready',backend:'webgl2',density:{mode:'overflow',visibleNodes:7,totalNodes:64}});assert.equal(f.root.querySelector('[data-code-renderer]').textContent,'WebGL renderer · 7/64 visible · pan or Explore');
 report({status:'ready',backend:'canvas2d',density:{mode:'fit',visibleNodes:64,totalNodes:64}});const target=f.root.querySelector('[data-code-renderer]');assert.equal(target.textContent,'Software renderer');f.workbench.dispose();report({status:'ready',backend:'webgl2',density:{mode:'overflow',visibleNodes:1,totalNodes:64}});assert.equal(target.textContent,'Software renderer','Disposed consumer cannot mutate retained DOM');
});
function fixture({transport,dispatch=false,width=900,focusPath=''}={}){
 const root=dom(),requests=[],subscriptions=[],scenes=[],actions=[],legacyRuns=[],legacyOptions=[],evidenceRequests=[];let released=0,refreshed=0,admitted=0;
 root.getBoundingClientRect=()=>({left:0,top:0,width,height:600});
 const sceneFactory=options=>{const scene={options,updates:[],selected:[],reveals:[],clears:0,disposed:false,suspended:false,resumes:0,suspends:0,update(v){this.updates.push(structuredClone(v));},select(id){this.selected.push(id);},revealSelected(value){this.reveals.push(structuredClone(value));},clear(){this.clears++;},fit(){},zoom(){},resize(){},resume(){this.suspended=false;this.resumes++;},suspend(){this.suspended=true;this.suspends++;},snapshot(){return {camera:{x:0,y:0,scale:1},status:'ready',backend:'canvas2d'};},dispose(){this.disposed=true;}};scenes.push(scene);return scene;};
 const requestMap=async(wireWorkspace,visual,signal)=>{requests.push({workspace:wireWorkspace,visual:structuredClone(visual),signal});if(transport)return transport(visual,requests.length);
  const v=golden(visual.request.expected_authority?.variant==='published'?'published':'preview');v.operation=visual.request.operation;
  if(v.operation!=='overview'){v.root=visual.request.root.id;v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;
   if(v.authority.variant==='published'&&v.nodes.find(n=>n.id===v.root)?.kind==='file')v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}
  return measure(v);
 };
 const options={element:root,workspaceId:'fixture-workspace',workspaceLabel:'Synthetic',focusPath,isCurrent:()=>true,chooseWorkspace:async()=>({id:'fixture-workspace',name:'Synthetic'}),sceneFactory,requestMap,
  subscribeIndex:(id,callbacks)=>{subscriptions.push({id,...callbacks});const release=()=>released++;release.refresh=()=>refreshed++;release.readmit=()=>admitted++;return release;},
  request:async(w,q)=>{evidenceRequests.push({w,q});return {schema:'open-clank.code-query/v1',domain:'code',error:{code:'unavailable',message:'Synthetic evidence response'},cost:{}};},readPreference:async()=>false,writePreference:async v=>v,readResourceCapabilities:async()=>({available:true}),onEvidenceAction:async(kind,selection)=>{actions.push({kind,...selection});return {message:'Done'};},
  mountEvidence:opts=>{legacyOptions.push(opts);const off=opts.subscribeIndex('fixture-workspace',{onStatus(){}});return {run:async(op,extra)=>legacyRuns.push({op,extra}),snapshot:()=>({nodes:[]}),dispose:off};},
 };
 const workbench=(dispatch?mountCodeGraphWorkbench:mountCodeMapWorkbench)(options);
 const button=text=>root.querySelectorAll('button').find(b=>b.textContent===text);
 return {root,workbench,requests,subscriptions,scenes,actions,legacyRuns,legacyOptions,evidenceRequests,button,get released(){return released;},get refreshed(){return refreshed;},get admitted(){return admitted;},async status(value){subscriptions.at(-1).onStatus(value);await tick();}};
}
test('visible hierarchy handles actual repository roots, null-root folders, neighbors and ancestors',()=>{
 const data={root:'repo',nodes:[{id:'repo',kind:'repository',parent:null},{id:'folder',kind:'folder',parent:'repo'},{id:'file',kind:'file',parent:'folder'},{id:'peer',kind:'file',parent:'folder'}],edges:[{id:'a',from:'repo',to:'folder'},{id:'b',from:'folder',to:'file'},{id:'c',from:'file',to:'peer'}]};
 assert.deepEqual(codeMapVisible(data,{operation:'overview'}).nodes.map(n=>n.id),['repo','folder']);assert.deepEqual(codeMapVisible({...data,root:'folder'},{operation:'children'}).nodes.map(n=>n.id),['repo','folder','file','peer']);
 assert.deepEqual(codeMapVisible({...data,root:'file'},{operation:'neighbors'}).nodes.map(n=>n.id),['repo','folder','file','peer']);assert.deepEqual(codeMapVisible(golden('preview'),{operation:'overview'}).nodes.map(n=>n.label),['src']);
});
test('preview reads only after index ACK, no evidence/source or second index owner',async()=>{
 const f=fixture();assert.equal(f.subscriptions.length,1);assert.equal(f.requests.length,0);await f.status(pending);assert.equal(f.requests.length,1);assert.equal(f.workbench.snapshot().authority.variant,'structure_preview');assert.equal(f.scenes[0].updates.at(-1).nodes.length,1);assert.equal(f.button('Open source').disabled,true);assert.equal(f.button('Search').disabled,true);assert.equal(f.actions.length,0);assert.match(f.root.querySelector('[data-code-index-status]').textContent,/Folder structure/);f.workbench.dispose();assert.equal(f.released,1);
});
test('host hide pauses existing scene while native responses retain same observer; restore never reopens',async()=>{
 const f=fixture(),scene=f.scenes[0];f.workbench.setVisible(false);assert(scene.suspended);const count=scene.suspends;f.workbench.setVisible(false);assert.equal(scene.suspends,count,'Unchanged root styles cannot create pause/resume churn');await f.status(pending);assert(scene.updates.length>0);assert(scene.suspended,'Hidden incoming geometry cannot resume GPU drawing');assert.equal(f.subscriptions.length,1);assert.equal(f.released,0);
 f.workbench.setVisible(true);assert(!scene.suspended);assert.equal(scene.resumes,1);assert.equal(f.scenes.length,1);assert.equal(f.subscriptions.length,1);const resume=scene.resumes;f.workbench.setVisible(true);assert.equal(scene.resumes,resume);f.workbench.dispose();f.workbench.setVisible(true);assert.equal(scene.resumes,resume);assert.equal(f.released,1);
});
test('hidden host and evidence view independently veto map resume',async()=>{
 const f=fixture(),scene=f.scenes[0];await f.status(ready);f.workbench.setVisible(false);await f.button('Search & evidence').fire('click');assert(scene.suspended);const before=scene.resumes;f.workbench.setVisible(true);assert(scene.suspended);assert.equal(scene.resumes,before,'Showing an evidence window must not run its hidden map');f.workbench.setVisible(false);await f.button('Map').fire('click');assert(scene.suspended);assert.equal(scene.resumes,before);f.workbench.setVisible(true);assert(!scene.suspended);assert.equal(scene.resumes,before+1);assert.equal(f.subscriptions.length,1);f.workbench.dispose();
});
test('neighbor reveal receives only bounded real direct file endpoints, not invented hierarchy IDs',async()=>{
 const f=fixture({transport:visual=>{const v=golden('published');v.operation=visual.request.operation;if(v.operation!=='overview'){v.root=visual.request.root.id;v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}return measure(v);}});await f.status(ready);const data=f.workbench.snapshot(),selected=data.nodes.find(n=>n.kind==='file').id,namespace=(await import('../../static/js/copal/codeMapModel.js')).codeMapKey(data.authority);f.scenes[0].options.onSelect({id:selected,authorityNamespace:namespace});await tick();
 const snapshot=f.workbench.snapshot(),contexts=f.scenes[0].reveals.filter(r=>r.contextIds.length);assert(contexts.length>0);const ids=contexts.at(-1).contextIds;assert(ids.length<=32&&new Set(ids).size===ids.length);for(const id of ids){assert(snapshot.nodes.some(n=>n.id===id&&n.kind==='file'));assert(snapshot.edges.some(e=>e.relation!=='containment'&&(e.from===selected&&e.to===id||e.to===selected&&e.from===id)));}assert.equal(f.actions.length,0);assert.equal(f.subscriptions.length,1);f.workbench.dispose();
});
test('same index progress avoids duplicate map/layout reads; publication atomically changes namespace',async()=>{
 let published=false;const f=fixture({transport:()=>measure(golden(published?'published':'preview'))});await f.status(pending);const updates=f.scenes[0].updates.length;await f.status({...pending,progress:{completed_files:1,total_files:2}});assert.equal(f.requests.length,1);assert.equal(f.scenes[0].updates.length,updates);
 // Same subscriber, new real head: fresh Overview atomically replaces preview.
 const old=f.requests[0];published=true;await f.status(ready);assert(old.signal.aborted);assert.equal(f.workbench.snapshot().authority.variant,'published');assert.equal(f.subscriptions.length,1);assert.equal(f.requests.length,2);assert.equal(f.actions.length,0);assert(f.workbench.snapshot().nodes.every(n=>n.id.includes('published')));f.workbench.dispose();
});
test('late preview after actual publication and revoked scope cannot reinstall geometry',async()=>{
 let first;const f=fixture({transport:(_v,n)=>n===1?new Promise(r=>first=r):measure(golden('published'))});await f.status(pending);await f.status(ready);assert.equal(f.workbench.snapshot().authority.variant,'published');first(measure(golden('preview')));await tick();assert.equal(f.workbench.snapshot().authority.variant,'published');assert(f.requests[0].signal.aborted);await f.status({...ready,lifecycle:'denied'});assert.equal(f.workbench.snapshot().authority,null);assert.equal(f.button('Open source').disabled,true);assert(f.scenes[0].clears>0);f.workbench.dispose();
});
test('shared evidence subscription does not open native index and forwards explicit controls',async()=>{
 const f=fixture();await f.status(ready);await f.button('Search & evidence').fire('click');await tick();assert.equal(f.subscriptions.length,1);assert.equal(f.legacyOptions.length,1);assert.equal(f.legacyOptions[0].requestMap,null);assert.equal(f.legacyOptions[0].initialQuery,false);assert.deepEqual(f.legacyRuns[0],{op:'catalog',extra:{}});await f.button('Recheck status').fire('click');assert.equal(f.refreshed,1);f.workbench.dispose();assert.equal(f.released,1);
});
test('Enter search dismisses the shared menu and restores reachable keyboard focus',async()=>{
 const f=fixture();await f.status(ready);const menu=f.root.querySelector('details'),search=menu.querySelector('input');
 menu.open=true;search.value='handler';search.focus();await search.fire('keydown',{key:'Enter'});await tick();
 assert.equal(menu.open,false);assert.equal(document.activeElement,menu.querySelector('summary'));
 assert.deepEqual(f.legacyRuns.at(-1),{op:'search',extra:{query:'handler'}});assert.equal(f.subscriptions.length,1);f.workbench.dispose();
});
test('controlled canvas selection obtains canonical mapping before explicit current-source action',async()=>{
 const f=fixture({transport:visual=>{
  const v=golden('published');if(visual.request.operation!=='overview'){v.operation=visual.request.operation;v.root=visual.request.root.id;v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}return measure(v);
 }});await f.status(ready);const data=f.workbench.snapshot(),id=data.nodes.find(n=>n.kind==='file').id;f.scenes[0].options.onSelect({id,authorityNamespace:JSON.stringify({wrong:true})});assert.equal(f.requests.length,1);f.scenes[0].options.onSelect({id,authorityNamespace:(await import('../../static/js/copal/codeMapModel.js')).codeMapKey(data.authority)});await tick();assert.equal(f.requests.at(-1).visual.request.operation,'neighbors');assert.equal(f.actions.length,0);assert.equal(f.button('Open source').disabled,false);await f.button('Open source').fire('click');assert.equal(f.actions.length,1);assert.equal(f.actions[0].node.id,'file:abc');assert.deepEqual(f.actions[0].receipt,data.authority.receipt);assert.equal(f.actions[0].isCurrent(),true);await f.status({...ready,lifecycle:'erased'});assert.equal(f.actions[0].isCurrent(),false);assert(f.actions[0].signal.aborted);f.workbench.dispose();
});
test('DOM explorer keyboard is bounded and closed/disposed requests cannot mutate',async()=>{
 const f=fixture();await f.status(pending);const list=f.root.querySelector('[role="listbox"]');let items=list.querySelectorAll('[data-code-map-id]');assert.equal(items.length,1);items[0].focus();await list.fire('keydown',{key:'End'});assert.equal(document.activeElement,items.at(-1));await f.button('Explore').fire('click');assert.equal(f.button('Explore').getAttribute('aria-expanded'),'true');await f.root.fire('keydown',{key:'Escape'});assert.equal(f.button('Explore').getAttribute('aria-expanded'),'false');f.workbench.dispose();assert(f.scenes[0].disposed);const count=f.requests.length;await f.workbench.run('overview');assert.equal(f.requests.length,count);
});
test('dispatch opt-in only, typed presentation and status permissions retain distinct states',()=>{
 assert.equal(codeMapMayRead({lifecycle:'starting'}),false);assert.equal(codeMapMayRead({lifecycle:'erased'}),false);assert.equal(codeMapMayRead({lifecycle:'failed'}),false);assert(codeMapMayRead({...ready,lifecycle:'failed'}));assert.match(codeMapStatus({lifecycle:'denied'},{}),/Access denied/);assert.match(codeMapStatus(ready,{status:'error',error:{code:'stale_cursor'}}),/changed/);
 const f=fixture({dispatch:true});assert(f.root.className.includes('code-map-workbench'));assert(!f.root.className.split(' ').includes('code-workbench'));f.workbench.dispose();
});
test('retained published map explicitly reports pending verification instead of ready',()=>{
 const map={...golden('published'),status:'ready'};
 for(const lifecycle of ['starting','admitted','queued','building','running']){
  const status=codeMapStatus({...ready,lifecycle,progress:{completed_files:23,total_files:100}},map);
  assert.match(status,/Previous index/);assert.match(status,/checking files/);assert.match(status,/23\/100/);assert.doesNotMatch(status,/map ready/);
 }
 assert.match(codeMapStatus({...ready,lifecycle:'cancelled'},map),/Previous index.*cancelled/);
 assert.match(codeMapStatus({...ready,lifecycle:'failed'},map),/Previous index.*failed/);
 assert.match(codeMapStatus({...ready,lifecycle:'denied'},map),/Access denied/);
 assert.match(codeMapStatus({...ready,lifecycle:'erased'},map),/erased/);
});
test('large coverage diagnostics remain compact and preserve actual index authority/progress',async()=>{
 const f=fixture();const marker='PRIVATE_COVERAGE_PATH_DO_NOT_MOUNT';
 const coverage=Array.from({length:8659},(_,i)=>({rel_path:`${marker}/${i}.py`,status:i<3902?'indexed':'unsupported',detail:'raw detail '.repeat(100)}));
 await f.status({...ready,coverage,errors:Array(1000).fill(marker),progress:{completed_files:3908,total_files:3908}});
 const text=f.root.querySelector('[data-code-diagnostics]').textContent,d=JSON.parse(text);
 assert(text.length<16384,`Diagnostics unexpectedly serialized ${text.length} chars`);assert(!text.includes(marker));
 assert.equal(d.index.head_generation,ready.head_generation);assert.equal(d.index.lifecycle,'ready');
 assert.deepEqual(d.index.progress,{completed_files:3908,total_files:3908});
 assert.deepEqual(d.index.coverageSummary,{entries:8659,statuses:{indexed:3902,unsupported:4757}});
 assert.equal(d.index.errorCount,1000);assert(!Object.hasOwn(d.index,'coverage'));assert(!Object.hasOwn(d.index,'errors'));
 assert.deepEqual(d.map.authority,f.workbench.snapshot().authority);f.workbench.dispose();
});
test('diagnostics have finite buckets/scalars and never imply absent coverage is complete',()=>{
 const index={lifecycle:'ready',head_generation:'h',extra:'not a declared diagnostic field',
  coverage:Array.from({length:1000},(_,i)=>({status:`arbitrary-${i}`})),error:{code:'x'.repeat(4096),message:'e'.repeat(4096)}};
 const before=structuredClone(index),d=codeMapIndexDiagnostics(index);
 assert.deepEqual(d.coverageSummary,{entries:1000,statuses:{other:1000}});assert.equal(d.error.code.length,256);assert.equal(d.error.message.length,512);
 assert(!Object.hasOwn(d,'extra'));assert.deepEqual(index,before);assert.equal(codeMapIndexDiagnostics({}).coverageSummary,null);
});
test('index progress does not snapshot the full scene just to repaint closed diagnostics',async()=>{
 const f=fixture();await f.status(ready);
 f.scenes[0].snapshot=()=>{throw Error('Unexpected full renderer snapshot');};
 await f.status({...ready,progress:{completed_files:23,total_files:100}});
 assert.equal(JSON.parse(f.root.querySelector('[data-code-diagnostics]').textContent).index.progress.completed_files,23);f.workbench.dispose();
});
test('real legacy evidence constructor shares one index owner and never auto-queries Overview',async()=>{
 const f=fixture({dispatch:true});await f.status(ready);assert.equal(f.evidenceRequests.length,0);await f.button('Search & evidence').fire('click');await tick();assert.equal(f.subscriptions.length,1);assert.equal(f.evidenceRequests.length,1);assert.equal(f.evidenceRequests[0].q.operation,'catalog');assert.equal(f.root.querySelectorAll('.code-workbench').length,1);assert.equal(f.released,0);f.workbench.dispose();assert.equal(f.released,1);
});
test('selected DOM option preserves focus and panel dismissal returns it to opener',async()=>{
 const f=fixture();await f.status(pending);await f.button('Explore').fire('click');const item=f.root.querySelector('[data-code-map-id]');item.focus();await item.fire('click');assert.equal(document.activeElement.getAttribute('data-code-map-id'),item.getAttribute('data-code-map-id'));
 await f.button('Close inspector').fire('click');const current=f.root.querySelector('[data-code-map-id]');current.focus();await f.root.fire('keydown',{key:'Escape'});assert.equal(document.activeElement,f.button('Explore'));f.workbench.dispose();
});
test('narrow panel selection never leaves keyboard focus in the newly hidden explorer',async()=>{
 const f=fixture({width:390});await f.status(pending);await f.button('Explore').fire('click');const item=f.root.querySelector('[data-code-map-id]');item.focus();await item.fire('click');for(let n=document.activeElement;n;n=n.parentNode)assert.equal(n.hidden,false,'Focused element must not be inside a hidden panel');f.workbench.dispose();
});
test('real model page continuation stays receipt-bound and mounts only 100 explorer options',async()=>{
 const f=fixture({transport:visual=>{const request=visual.request,v=golden('published'),repo={...v.nodes[0],id:'repo',label:'Repository',kind:'repository',parent:null},base=v.nodes[1],offset=request.cursor?199:0;v.root='repo';v.nodes=[repo,...Array.from({length:199},(_,i)=>({...base,id:`file-${offset+i}`,label:`file-${offset+i}.py`,parent:'repo'}))];v.edges=[];v.page={returned_nodes:200,returned_edges:0,remaining_nodes:request.cursor?0:199,remaining_edges:0,complete:!!request.cursor,next_cursor:request.cursor?null:'next-200',truncated:false};return measure(v);}});
 await f.status(ready);assert.equal(f.root.querySelectorAll('[data-code-map-id]').length,100);await f.button('Load more').fire('click');await tick();assert.equal(f.requests.length,2);assert.equal(f.requests[1].visual.request.cursor,'next-200');assert.deepEqual(f.requests[1].visual.request.expected_authority,f.workbench.snapshot().authority);assert.equal(f.workbench.snapshot().nodes.length,399);assert.equal(f.root.querySelectorAll('[data-code-map-id]').length,100);await f.button('Next').fire('click');assert.equal(f.root.querySelectorAll('[data-code-map-id]').length,100);assert.equal(f.button('Load more').disabled,true);f.workbench.dispose();
});
test('explicit reindex-required error offers Retry without starting a second job automatically',async()=>{
 const f=fixture({transport:()=>measure({schema:'open-clank.code-map/v1',mode:'map',error:{code:'reindex_required',message:'Public'},cost:golden('preview').cost})});await f.status(ready);assert.equal(f.workbench.snapshot().error.code,'reindex_required');assert.equal(f.button('Retry indexing').hidden,false);assert.equal(f.subscriptions.length,1);await f.button('Retry indexing').fire('click');assert.equal(f.subscriptions.length,2);assert.equal(f.released,1);f.workbench.dispose();
});
test('workspace switch rejects old subscriber and pending visual request without extra source action',async()=>{
 let release;const f=fixture({transport:()=>new Promise(r=>release=r)});await f.status(pending);const old=f.subscriptions[0];f.workbench.selectWorkspace('different',{label:'Other synthetic scope'});assert.equal(f.released,1);assert.equal(f.subscriptions.length,2);assert(f.requests[0].signal.aborted);old.onStatus(ready);release(measure(golden('published')));await tick();assert.equal(f.workbench.snapshot().authority,null);assert.equal(f.actions.length,0);assert.equal(f.requests.length,1);f.workbench.dispose();
});
test('Copal visual transport is separate and source actions retain current resource verification',()=>{
 const source=fs.readFileSync(new URL('../../static/js/copal.js',import.meta.url),'utf8');
 assert(source.includes('`${state.api}/api/memory/code/map`'));assert(source.includes('JSON.stringify({workspace_id:workspaceId,visual_request})'));assert(source.includes("if(!(response.headers.get('content-type')||'').includes('application/json'))"));
 const action=source.slice(source.indexOf('onEvidenceAction:async(kind,selection)=>'),source.indexOf('onEvidenceAction:async(kind,selection)=>')+5000);assert(action.includes('current_source_verified!==true'));assert(action.includes("validation_kind!=='current_source'"));assert(action.includes('codeReceiptKey(result.receipt)!==codeReceiptKey(selection.receipt)'));assert(action.includes('!selection.isCurrent()'));assert(action.includes('result.reference?.id!==selection.node.id'));assert(action.includes('handoffCurrent:'));
});
test('exact requested path re-resolves preview IDs into published IDs without widening workspace or evidence action',async()=>{
 let published=false;
 const f=fixture({focusPath:'src/caller.py',transport:visual=>{
   const v=golden(published?'published':'preview'),r=visual.request;v.operation=r.operation;
   if(r.operation!=='overview'){v.root=r.root.id;if(r.operation==='neighbors'){v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;}if(published&&v.nodes.find(n=>n.id===v.root)?.kind==='file')v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}
   return measure(v);
 }});
 await f.status(pending);await tick();await tick();
 assert.deepEqual(f.requests.map(q=>q.visual.request.operation),['overview','children'],'Just-completed overview must not be fetched again for requested path');
 assert(f.scenes[0].selected.includes('vm1:n:caller'));assert.equal(f.requests.some(q=>q.visual.request.operation==='neighbors'),false);assert.equal(f.actions.length,0);
 published=true;await f.status(ready);await tick();await tick();
 assert.deepEqual(f.requests.slice(2).map(q=>q.visual.request.operation),['overview','children','neighbors'],'Published namespace gets its own overview, not a stale preview reuse or duplicate');
 assert(f.scenes[0].selected.includes('vm1:n:published-caller'));assert.equal(f.workbench.snapshot().selection?.resource_id,'file:abc');
 assert(f.requests.every(q=>q.workspace==='fixture-workspace'));assert.equal(f.subscriptions.length,1);assert.equal(f.actions.length,0);f.workbench.dispose();
});
test('pending neighbors retains accepted folder geometry then reveals completed same-geometry adjacency',async()=>{
 let finish;
 const f=fixture({focusPath:'src/caller.py',transport:visual=>{
  const v=golden('published'),r=visual.request;v.operation=r.operation;
  const src=v.nodes[0];src.parent='repo';
  const repo={...src,id:'repo',label:'Repository',kind:'repository',parent:null},outside={...src,id:'outside',label:'other',parent:'repo',source_files:0,languages:[]};
  if(r.operation==='overview'){
   v.root='repo';v.nodes=[repo,src,outside];v.edges=[{id:'repo-src',from:'repo',to:src.id,relation:'containment',state:'structure',count:1,selection_id:null},{id:'repo-outside',from:'repo',to:'outside',relation:'containment',state:'structure',count:1,selection_id:null},{id:'folder-import',from:src.id,to:'outside',relation:'import',state:'exact',count:1,selection_id:'folder-selection'}];
  }else{v.root=r.root.id;if(r.operation==='neighbors'){v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}else v.edges=[v.edges[1],v.edges[0],v.edges[2]];}
  v.page.returned_nodes=v.nodes.length;v.page.returned_edges=v.edges.length;
  const result=measure(v);if(r.operation==='neighbors')return new Promise(resolve=>finish=()=>resolve(result));return result;
 }});
 await f.status(ready);await tick();await tick();assert(finish);
 const scene=f.scenes[0],accepted=scene.updates.at(-1);assert.equal(f.workbench.snapshot().status,'loading');
 assert.deepEqual(accepted.nodes.map(n=>n.id).sort(),['repo','vm1:n:published-folder','vm1:n:published-caller','vm1:n:published-target'].sort(),'New neighbors query must not expand old folder root to outside adjacency while pending');
 const updates=scene.updates.length;finish();await tick();await tick();
 assert.equal(f.workbench.snapshot().root,'vm1:n:published-caller');assert.equal(scene.updates.length,updates,'Identical accepted geometry needs no new layout');assert(scene.reveals.at(-1).contextIds.includes('vm1:n:published-target'),'Completed neighbor context still reveals when geometry IDs/edges did not change');f.workbench.dispose();
});
test('published loading scene clears on failure or revocation and cannot be revived by a late response',async()=>{
 for(const revoke of [false,true]){
  let finish;const f=fixture({transport:(_v,n)=>n===1?measure(golden('published')):new Promise(resolve=>finish=resolve)});await f.status(ready);const scene=f.scenes[0],before=scene.updates.length;
  const pendingRead=f.workbench.run('overview');assert.equal(scene.updates.length,before,'Same-authority loading leaves accepted scene intact');
  if(revoke){await f.status({...ready,lifecycle:'erased'});assert.equal(f.workbench.snapshot().authority,null);assert(scene.clears>0);finish(measure(golden('published')));await pendingRead;assert.equal(f.workbench.snapshot().authority,null);}
  else{finish(measure({schema:'open-clank.code-map/v1',mode:'map',error:{code:'denied',message:'Public'},cost:golden('preview').cost}));await pendingRead;assert.equal(f.workbench.snapshot().status,'error');assert.equal(scene.updates.at(-1).authority,null);assert.deepEqual(scene.updates.at(-1).nodes,[]);}
  f.workbench.dispose();
 }
});
test('deferred requested neighbors replaces a folder loading projection on repeated mounts',async()=>{
 for(let iteration=0;iteration<2;iteration++){
  let finish;
  const f=fixture({focusPath:'src/caller.py',transport:visual=>{
   const v=golden('published'),r=visual.request;v.operation=r.operation;
   if(r.operation==='overview')v.nodes=v.nodes.filter(n=>n.kind==='folder'),v.edges=[];
   else {v.root=r.root.id;if(r.operation==='neighbors'){
    v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);
    v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};
   }}
   v.page.returned_nodes=v.nodes.length;v.page.returned_edges=v.edges.length;
   const result=measure(v);return r.operation==='neighbors'?new Promise(resolve=>finish=()=>resolve(result)):result;
  }});
  await f.status(ready);await tick();await tick();assert(finish);
  await f.status({...ready,lifecycle:'building',run_id:'same-head-verification',progress:{completed_files:1,total_files:2}});
  finish();await tick();await tick();
  assert.equal(f.workbench.snapshot().status,'ready');
  assert.equal(f.workbench.snapshot().root,'vm1:n:published-caller');
  assert.equal(f.scenes[0].updates.at(-1).root,'vm1:n:published-caller');
  assert.equal(f.scenes[0].selected.at(-1),'vm1:n:published-caller');
  assert(f.scenes[0].reveals.at(-1).contextIds.includes('vm1:n:published-target'));
  const updates=f.scenes[0].updates.length;
  for(const lifecycle of ['queued','building','ready'])await f.status({...ready,lifecycle,run_id:'same-head-verification',progress:{completed_files:2,total_files:2}});
  assert.equal(f.scenes[0].updates.length,updates,'Ordinary same-head progress cannot reinstall a folder projection');
  assert.equal(f.workbench.snapshot().root,'vm1:n:published-caller');
  assert.equal(f.scenes[0].selected.at(-1),'vm1:n:published-caller');
  f.workbench.dispose();
 }
});
test('late hierarchy focus reply cannot select old namespace after workspace replacement or dispose',async()=>{
 let finish;const f=fixture({focusPath:'src/caller.py',transport:()=>new Promise(resolve=>finish=resolve)});
 await f.status(pending);const old=f.subscriptions[0];
 f.workbench.selectWorkspace('different',{label:'Different authorized scope'});
 old.onStatus(ready);finish(measure(golden('preview')));await tick();await tick();
 assert.equal(f.workbench.snapshot().authority,null);assert(!f.scenes[0].selected.some(Boolean));assert.equal(f.actions.length,0);f.workbench.dispose();
});
