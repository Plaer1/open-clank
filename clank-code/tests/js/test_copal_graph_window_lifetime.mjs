// Execute the actual Copal lifetime/currentness functions, not copied predicates.
// Bounded DOM/dependency fixtures; no browser, API, native owner or real stores.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import {sameSaveScope} from '../../static/js/copal/documentSave.js';

const source=fs.readFileSync(new URL('../../static/js/copal.js',import.meta.url),'utf8');
function section(start,end){const a=source.indexOf(start),b=source.indexOf(end,a+start.length);assert(a>=0&&b>a);return source.slice(a,b);}
const functions=[
 section('function persistActiveContext()','async function api('),
 section('function saveScope()','async function refreshIndexedDocument('),
 section('function renderGraph()','export async function openCodeGraphBinding('),
 section('function renderCodeGraph({','function mindOutlineEntries('),
 section('function invalidateCodeGraphContext()','for (const eventName'),
 section('function close(view =','/** Help opens'),
].join('\n');
const retainedDeclaration=section('      const retainedCode =','      for (const [view, context]');

function fixture(){
 const mounts=[],visibilityObservers=[],calls={disposed:0,cleared:0},graphView={mode:'code'};
 const connected=(node,value)=>{if(node&&typeof node==='object'){node.isConnected=value;for(const c of node.children||[])connected(c,value);}};
 function node(attrs={}){return{attrs,children:[],isConnected:true,append(...children){this.children.push(...children);},replaceChildren(...children){for(const c of this.children)connected(c,false);this.children=children;for(const c of children)connected(c,true);},querySelector(selector){for(const c of this.children){if(c&&typeof c==='object'){if(selector==='[data-code-graph]'&&c.attrs['data-code-graph'])return c;const found=c.querySelector?.(selector);if(found)return found;}}return null;}};}
 const classes=new Set(),graphBody=node(),notesBody=node(),graph={view:'graph',selected:null,filter:'',reading:false,window:{root:{classList:{contains:n=>classes.has(n)}},body:graphBody,visible:true}},notes={view:'notes',selected:null,filter:'',reading:false,window:{body:notesBody,visible:true}};
 graph.window.requestClose=()=>{graph.window.visible=false;classes.delete('modal-minimized');graphBody.isConnected=false;};
 const state={view:'graph',body:graphBody,accountId:'synthetic-account',workspace:'synthetic-workspace',storageNamespace:'synthetic-storage',contextEpoch:1,windows:new Map([['graph',graph],['notes',notes]]),selected:null,filter:'',reading:false};
 const deps={state,sameSaveScope,getGraphView:()=>graphView,MutationObserver:class{constructor(fn){this.fn=fn;this.disconnected=false;visibilityObservers.push(this);}observe(root,options){this.root=root;this.options=options;}disconnect(){this.disconnected=true;}fire(){if(!this.disconnected)this.fn();}},
  h:(_tag,attrs,...children)=>{const n=node(attrs);n.append(...children.flat());return n;},markActive(){},
  mountCodeGraphWorkbench(options){const owner={options,disposed:false,visibility:[],setVisible(value){if(!this.disposed)this.visibility.push(value);},dispose(){if(!this.disposed){this.disposed=true;calls.disposed++;}},isCurrent(){return !this.disposed&&options.isCurrent();},isMounted(){return !this.disposed&&options.element.isConnected;}};mounts.push(owner);return owner;},
 };
 const api=new Function('deps',`const {${Object.keys(deps).join(',')}}=deps;let codeGraphController=null,graphRenderTicket=0,editorNavigationTicket=0,codeGraphContextEpoch=0,codeGraphSelection=null;
 ${functions}
 return{activateView,renderGraph,close,invalidateCodeGraphContext,
 retained(active){${retainedDeclaration};return retainedCode;},
 stats:()=>({ticket:graphRenderTicket,navigation:editorNavigationTicket,codeEpoch:codeGraphContextEpoch,controller:codeGraphController}),
 replaceSelection(){codeGraphSelection={...codeGraphSelection};}};`)(deps);
 return{...api,state,graph,notes,graphBody,notesBody,graphView,mounts,calls,visibilityObservers,classes,connect:connected};
}

test('ordinary Editor foreground preserves the still-visible Graph owner and mounted response ticket',()=>{
 const f=fixture();f.renderGraph();const owner=f.mounts[0],before=f.stats();assert(owner.isCurrent());
 f.activateView('notes');assert.equal(f.calls.disposed,0,'Foreground change must not dispose visible Graph');assert.equal(f.stats().ticket,before.ticket);assert.equal(f.stats().navigation,before.navigation+1,'Editor navigation still supersedes an awaited source handoff');assert.equal(f.state.body,f.notesBody);assert(owner.isCurrent(),'Pending Graph response remains bound to originating visible window');
 f.activateView('graph');assert.equal(f.stats().controller,owner);assert(owner.isCurrent());assert.equal(f.mounts.length,1);
});

test('document refresh retains the actual live Code owner even while another Copal window is active',()=>{
 const f=fixture();f.renderGraph();const owner=f.mounts[0];f.activateView('notes');assert.equal(f.retained('notes'),owner);assert.equal(f.mounts.length,1,'Retention does not mount a second index observer');
});

test('mode switch/rerender invalidates old late callbacks while the new owner remains current',()=>{
 const f=fixture();f.renderGraph();const first=f.mounts[0];f.renderGraph();assert(first.disposed);assert(!first.options.isCurrent());assert(f.mounts[1].isCurrent());assert.equal(f.calls.disposed,1);
 f.graphView.mode='documents';assert(!f.mounts[1].options.isCurrent());
});

test('actual close keeps disposal and rejects late responses, including inactive Graph close',()=>{
 const f=fixture();f.renderGraph();const owner=f.mounts[0];f.activateView('notes');f.close('graph',false);assert(owner.disposed);assert.equal(f.graph.window.visible,false);assert(!owner.options.isCurrent());assert.equal(f.retained('notes'),null);
});

test('official minimize retains observer authority but delegates hidden drawing pause and same-owner restore',()=>{
 const f=fixture();f.renderGraph();const owner=f.mounts[0],observer=f.visibilityObservers[0];assert.equal(observer.root,f.graph.window.root);assert.deepEqual(observer.options,{attributes:true,attributeFilter:['class','style']});
 f.graph.window.visible=false;f.classes.add('modal-minimized');observer.fire();assert(owner.options.isCurrent(),'A minimized owned window can consume its same-scope in-flight response');assert.equal(owner.visibility.at(-1),false);assert.equal(f.calls.disposed,0);
 f.graph.window.visible=true;f.classes.delete('modal-minimized');observer.fire();assert.equal(owner.visibility.at(-1),true);assert(owner.isCurrent());assert.equal(f.mounts.length,1,'Restore must not mount/open another index owner');
 f.close('graph',false);assert(observer.disconnected);const before=owner.visibility.length;observer.fire();assert.equal(owner.visibility.length,before);assert(!owner.options.isCurrent());
});

test('visibility observer disconnects on mode replacement and scope/policy disposal',()=>{
 for(const change of [f=>f.renderGraph(),f=>f.invalidateCodeGraphContext()]){const f=fixture();f.renderGraph();const observer=f.visibilityObservers[0];change(f);assert(observer.disconnected);}
});

test('owner/context/body replacement and disconnected/hidden window cannot retain old authority',()=>{
 for(const mutate of [f=>f.state.windows.set('graph',{...f.graph}),f=>f.graph.window.body={},f=>f.graphBody.isConnected=false,f=>f.graph.window.visible=false]){
  const f=fixture();f.renderGraph();const owner=f.mounts[0];mutate(f);assert(!owner.options.isCurrent());assert.equal(f.retained('notes'),null);
 }
});

test('account/workspace/storage/epoch changes retain the existing full save-scope fence',()=>{
 for(const field of ['accountId','workspace','storageNamespace','contextEpoch']){const f=fixture();f.renderGraph();f.state[field]='other';assert(!f.mounts[0].options.isCurrent());}
 const f=fixture();f.renderGraph();f.replaceSelection();assert(!f.mounts[0].options.isCurrent(),'A different Code selection namespace invalidates the old mount');
});

test('policy/auth context invalidation clears the Graph owner, not the currently active Editor body',()=>{
 const f=fixture();f.renderGraph();const owner=f.mounts[0],graphWrap=f.graphBody.querySelector('[data-code-graph]');let graphCleared=0,editorCleared=0;graphWrap.replaceChildren=()=>{graphCleared++;};f.notesBody.querySelector=()=>({replaceChildren(){editorCleared++;}});
 f.activateView('notes');f.invalidateCodeGraphContext();assert(owner.disposed);assert(!owner.options.isCurrent());assert.equal(graphCleared,1);assert.equal(editorCleared,0,'Graph invalidation must not overwrite another foreground applet');
});

test('unchanged source contains no selected Treehouse API/lifetime edits',()=>{
 assert(source.includes("if(view==='treehouse'&&typeof treeHouse.setNavigationWindow==='function')treeHouse.setNavigationWindow(windowApi);"));assert(source.includes("if (view === 'treehouse') treeHouse.stopEngagement();"));
});
