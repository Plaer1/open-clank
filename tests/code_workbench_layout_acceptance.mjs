// Fixed applet geometry over the actual stylesheet; static backend, no app/index.
import assert from 'node:assert/strict';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const page=`<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css">
<body style="display:block!important;margin:0"><main class="copal-view" id="fixture" style="position:fixed;overflow:auto"></main>
<script type="module">
import {mountCodeGraphWorkbench} from '/static/js/copal/codeGraphWorkbench.js';
const receipt={workspace_id:'layout',registration_id:'reg',source_snapshot_id:'source',checkout_id:'checkout',head_generation:'head',admission_epoch:0,policy_generation:1,config_fingerprint:'layout'};
const axes=()=>({kinds:[],languages:[],states:[],folders:[]});
window.__layout={calls:[],mount(){this.controller?.dispose();const parent=document.querySelector('#fixture');parent.replaceChildren();
const wrap=document.createElement('div');wrap.className='copal-graph-wrap';wrap.setAttribute('data-code-graph','');
const label=document.createElement('label');label.className='code-graph-mode';label.textContent='Graph view';const mode=document.createElement('select');mode.innerHTML='<option>Code · scoped evidence</option>';label.append(mode);
const surface=document.createElement('div');wrap.append(label,surface);parent.append(wrap);this.calls=[];
this.controller=mountCodeGraphWorkbench({element:surface,isCurrent:()=>true,workspaceId:'layout',readPreference:async()=>false,readResourceCapabilities:async()=>({available:true}),
subscribeIndex:(_id,{onStatus})=>{queueMicrotask(()=>onStatus({lifecycle:'ready',head_generation:'head',freshness:'current'}));return()=>{};},
request:async(_id,query)=>{this.calls.push(query);const page=Number(query.cursor||0),facet=query.operation==='facets';const facets=axes();if(facet)facets.folders=[{value:page?'src':'',count:page?5000:3586,unit:'file',exactness:'exact'}];
return {schema:'open-clank.code-query/v1',domain:'code',operation:query.operation,receipt,hits:facet?[]:Array.from({length:32},(_,n)=>({id:'file:'+(n+page*32).toString(16).padStart(64,'0'),kind:'file',label:'src/long_directory_name_for_layout/'+('long_label_'.repeat(8))+n+'.py',evidence_state:'metadata'})),
edges:[],paths:[],facets,counts:[{name:'matched',value:8586,unit:'resource',exactness:'exact'}],channels:[],capabilities:[],operation_capabilities:['overview','catalog','facets','search'].map(operation=>({operation,available:true})),omissions:[],partial:true,degraded:false,reasons:[],next_cursor:page?null:'1',error:null,cost:{}};},onController:model=>{this.model=model;}});
},snapshot(){return this.model?.snapshot();}};window.__ready=true;
</script>`;
await withCopalBrowser({page},async({evaluate,until,cdp})=>{
 await until('window.__ready');let groups=0;
 const physical=async expression=>{
  const point=await evaluate(`(async()=>{const e=${expression};if(!e||e.disabled)throw Error('Expected enabled layout control');e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const b=e.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2,hit=document.elementFromPoint(x,y),box=n=>{const q=n.getBoundingClientRect();return{top:q.top,bottom:q.bottom,left:q.left,right:q.right,height:q.height};};if(x<0||y<0||x>=innerWidth||y>=innerHeight||!hit||!(hit===e||e.contains(hit)))throw Error('Layout control occluded '+JSON.stringify({control:box(e),hit:hit?.className,workbench:box(document.querySelector('.code-workbench')),panels:box(document.querySelector('.code-workbench-panels')),diagnostics:box(document.querySelector('.code-workbench-diagnostics'))}));return{x,y};})()`);
  await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',...point});await cdp('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});
 };
 const contained=async()=>{const geometry=await evaluate(`(()=>{const rect=s=>{const r=document.querySelector(s).getBoundingClientRect();return{top:r.top,bottom:r.bottom,height:r.height};};return{panels:rect('.code-workbench-panels'),diagnostics:rect('.code-workbench-diagnostics'),explorer:rect('.code-workbench-explorer'),workbench:rect('.code-workbench')};})()`);assert(geometry.explorer.bottom<=geometry.panels.bottom+2&&geometry.diagnostics.top>=geometry.panels.bottom-2&&geometry.workbench.bottom>=geometry.diagnostics.bottom-2,'Panel must contain explorer; diagnostics must follow inside workbench, not overlay it: '+JSON.stringify(geometry));};
 const allReachable=async()=>{const failures=await evaluate(`(async()=>{const root=document.querySelector('.code-workbench'),rows=[...root.querySelectorAll('[data-code-list] button')],controls=[...root.querySelectorAll('button,input,select,summary')].filter(e=>!e.disabled&&e.getClientRects().length&&(!e.closest('[data-code-list]')||e===rows[0]||e===rows.at(-1))),failures=[];for(const e of controls){e.scrollIntoView({block:'center',inline:'center',behavior:'instant'});await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const b=e.getBoundingClientRect(),x=b.left+b.width/2,y=b.top+b.height/2,hit=document.elementFromPoint(x,y);if(x<0||y<0||x>=innerWidth||y>=innerHeight||!hit||!(hit===e||e.contains(hit)))failures.push({tag:e.tagName,label:(e.getAttribute('aria-label')||e.textContent||'').slice(0,80),hit:hit?.className});}return failures;})()`);assert.deepEqual(failures,[],'All enabled non-list controls and first/last catalog resources remain scroll-reachable');};
 for(const [width,height,zoom]of [[1440,1000,1],[390,480,1],[320,480,1],[390,480,2],[320,480,2]]){
  await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile:false});
  await evaluate(`{const e=document.querySelector('#fixture');e.style.left=${width===1440?245:8}+'px';e.style.top=${width===1440?160:16}+'px';e.style.width=${width===1440?1145:width-16}+'px';e.style.height=${width===1440?652:height-32}+'px';window.__layout.mount();document.querySelector('.copal-graph-wrap').style.zoom=${zoom};}`);
  await until(`window.__layout.snapshot()?.status==='ready'`);
  await evaluate(`window.__layout.controller.run('facets')`);await until(`window.__layout.snapshot()?.status==='ready'`);
  assert.equal(await evaluate(`document.querySelectorAll('[data-code-id]').length`),0);
  await physical(`[...document.querySelectorAll('.code-workbench button')].find(e=>e.textContent==='Load more')`);
  await until(`window.__layout.calls.at(-1)?.operation==='facets'&&window.__layout.calls.at(-1)?.cursor==='1'&&window.__layout.snapshot()?.status==='ready'`);
  assert.equal(await evaluate(`window.__layout.snapshot().facets.folders.length`),2);await contained();groups++;
  await physical(`document.querySelector('.code-workbench-diagnostics > summary')`);assert(await evaluate(`document.querySelector('.code-workbench-diagnostics').open`));await contained();groups++;
  await physical(`[...document.querySelectorAll('.code-workbench button')].find(e=>e.textContent==='Files')`);await until(`window.__layout.snapshot()?.status==='ready'`);
  assert.equal(await evaluate(`document.querySelectorAll('[data-code-list] [data-code-id]').length`),32);
  await physical(`[...document.querySelectorAll('.code-workbench button')].find(e=>e.textContent==='Load more')`);await until(`window.__layout.snapshot()?.nodes.length===64`);await contained();await allReachable();groups++;
 }
 console.log('Code fixed-height layout: '+groups+' actual-stylesheet physical paging/diagnostics groups passed');
});
