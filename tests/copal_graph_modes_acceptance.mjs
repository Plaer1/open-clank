#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';
import { propertyFacetOption } from '../static/js/copal/graphModel.js';

const root=process.cwd();
const source=fs.readFileSync(path.join(root,'static/js/copal.js'),'utf8');
const injection=String.raw`
window.__graphTest={
 setup({account='acct-a',workspace='ws-a',docs=[],planning={tracks:[],floatingTodos:[]},drafts={},sourceDocId=null,body}){
  for (const modal of [...document.querySelectorAll('.copal-tool-modal')]) (modal.__openClankWindow||modal.__copalWindow)?.destroy?.();
  configureCopalStorage('graph-test'); localStorage.removeItem(graphStorageKey(account, workspace)); state.accountId=account; state.workspace=workspace; state.storageNamespace='graph-test'; state.contextEpoch=1; state.docs=docs; state.planning=planning; state.filter=''; state.selected=null; state.view='graph'; state.body=body; state.root=document.body; state.content=body;
  state.windows.clear(); state.windows.set('graph',{view:'graph',body,window:{body,content:body,root:body,visible:true,setStatus(){},show(){},focus(){}} ,selected:null,filter:'',story:[],pinned:new Set(),reading:false});
  planningFeature={openTrackEditor(id){window.__graphTest.handoffs.push(['track',id]);},openEventEditor(id){window.__graphTest.handoffs.push(['event',id]);}};
  window.__graphTest.drafts=drafts; window.__graphTest.transactions=[];
  notesFeature={open(id){window.__graphTest.handoffs.push(['note',id]);},render(){},getDraftSnapshot(id){const draft=window.__graphTest.drafts[id];if(!draft)return null;return {expectedRevision:draft.expectedRevision,localRevision:draft.localRevision||0,envelope:{text:draft.text}};} ,applyDocumentTransaction(doc,transform){const draft=window.__graphTest.drafts[doc.id]||{text:String(doc.text||''),expectedRevision:{kind:'copalHead',value:String(doc.head||'')},localRevision:0};const content=transform(draft.text);draft.text=content;draft.localRevision=(draft.localRevision||0)+1;doc.text=content;window.__graphTest.transactions.push({id:doc.id,content});return {outcome:'queued',content,localRevision:draft.localRevision};}};
  wikiFeature={open(id){state.selected=id;window.__graphTest.handoffs.push(['wiki',id]);},render(){}};
  graphView=null; graphScopeKey=null; window.__graphTest.handoffs=[];
 },
 render(){renderGraph();},
 renderMind(){renderMind();},
 setSource(id){getGraphView().source={kind:'document',docId:String(id)};getGraphView().modes.structure.navigation.docId=String(id);},
 actual(){renderGraph();},
 renderProjection(mode, nodes, edges, facets=null){const body=state.body;body.replaceChildren(graphSvg(nodes,edges,node=>window.__graphTest.handoffs.push(['open',node.kind,node.selection||node.id]),mode,facets));},
 route(view,mode){return resolveView(view,mode);},
 snapshot(){return structuredClone(getGraphView());},
 editor(){const body=state.body;state.windows.set('notes',{window:{body,content:body,root:body},selected:null,filter:'',reading:false});activateView('notes');body.replaceChildren(Object.assign(document.createElement('p'),{textContent:'Editor sentinel'}));},
 hide(){state.windows.get('graph').window.visible=false;state.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();},
 dotFolders(show){localStorage.setItem(copalStorageKey('odysseus-copal-notes-layout',state.workspace),JSON.stringify({left:{showDotFolders:show}}));},
 facets(){return makeGraphFacetCatalog().canonical({force:true});},
 catalogSnapshot(){return window.__graphTest.catalog?.snapshot() || null;},
 context(){return {view:state.view,selected:state.selected};},
 graphState(){return {docs:documentGraph(state.docs,findByName),galaxy:galaxyGraph(planningData().tracks||[],allPlanningTasks(planningData()))};},
 mindTransactions(){return window.__graphTest.transactions.slice();},
 handoffs:[],facetCompleted:0
};
const fixtureMakeGraphFacetCatalog=makeGraphFacetCatalog;
makeGraphFacetCatalog=(...args)=>{const catalog=fixtureMakeGraphFacetCatalog(...args);window.__graphTest.catalog=catalog;
 const canonical=catalog.canonical.bind(catalog);catalog.canonical=async(options)=>{try{return await canonical(options);}finally{window.__graphTest.facetCompleted+=1;}};return catalog;};`;
const copal=source.replace('export default { init,',injection+'\nexport default { init,');
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><style>html,body{margin:0;background:#17202a;color:#eee}#graph{width:1100px;height:760px}</style><main id="graph" data-window-id="graph-test"></main><script type="module">import { registerInputContext, activateInputContext } from "/static/js/copal/inputContext.js"; import "/static/js/copal.js"; window.__ready=()=>Boolean(window.__graphTest); window.__register=()=>{const b=document.querySelector('#graph');const c={windowId:'graph-test',paneId:'graph',capabilities:{keyboard:true,pointer:true,wheel:true}};registerInputContext(b,c);activateInputContext(c);};</script>`;
let facetCorpus=[];
let facetHandler=null;
function buildFacetResponse(url){
  const params=new URL(url,'http://x').searchParams;
  const offset=Math.max(0,Number(params.get('offset')||0));
  const limit=Math.min(1000,Math.max(1,Number(params.get('limit')||200)));
  const query=String(params.get('query')||'').trim().toLowerCase();
  const category=params.get('category')||'all', propertyKey=params.get('property_key');
  const docs=facetCorpus;
  const kinds={}, folders={}, tags={}, properties={};
  const totals=new Map(); const official=new Map(); let officialTotal=0;
  for (const doc of docs) {
    const kind=doc.kind==='wiki'?'wiki':doc.kind==='base'?'base':doc.kind==='copal-event'?'event':'note';
    kinds[kind]=(kinds[kind]||0)+1;
    const name=String(doc.name||''); const cut=name.lastIndexOf('/');
    const folder=cut>0?name.slice(0,cut):'';
    folders[folder]=(folders[folder]||0)+1;
    const top=folder.split('/')[0]||'';
    if (top) totals.set(top,(totals.get(top)||0)+1);
    const isOfficial=doc.builtin===true||String((doc.properties&&doc.properties.product)||doc.product||'').trim().toLowerCase()==='open-clank'||(doc.properties&&doc.properties.builtin===true);
    if (isOfficial&&top) { officialTotal+=1; official.set(top,(official.get(top)||0)+1); }
    for (const tag of new Set(doc.tags||[])) { const t=String(tag).trim(); if(t) tags[t]=(tags[t]||0)+1; }
    for (const [key,value] of Object.entries(doc.properties||{})) {
      const pk=String(key); if(!pk) continue;
      if (!Object.hasOwn(properties,pk)) Object.defineProperty(properties,pk,{value:new Map(),enumerable:true});
      const bucket=properties[pk],seen=new Set();
      for (const item of (Array.isArray(value)?value:[value])) {
        const option=propertyFacetOption(item);if(!option||seen.has(option.id))continue;seen.add(option.id);
        const entry=bucket.get(option.id)||{...option,count:0};entry.count++;bucket.set(option.id,entry);
      }
    }
  }
  let officialRoot=null;
  const candidates=[...official.entries()].filter(([f,c])=>(totals.get(f)||0)===c&&c===officialTotal);
  if (candidates.length) officialRoot=candidates.sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0]))[0][0];
  const sorted=(obj)=>Object.entries(obj).sort((a,b)=>b[1]-a[1]||a[0].localeCompare(b[0])).map(([value,count])=>({value,count}));
  const paginate=(values)=>{
    const matched=query?values.filter((item)=>String(item.label??item.value).toLowerCase().includes(query)):values;
    const window=matched.slice(offset,offset+limit);
    return {values:window,total:matched.length,offset,limit,hasMore:offset+window.length<matched.length};
  };
  const facets={};for(const key of ['kinds','folders','tags'])if(category==='all'||category===key)facets[key]=paginate(sorted({kinds,folders,tags}[key]));
  if(category==='all'||category==='properties')facets.properties=Object.fromEntries((propertyKey!==null?[propertyKey]:Object.keys(properties)).map(key=>[key,paginate([...(properties[key]||new Map()).values()].sort((a,b)=>b.count-a.count||a.id.localeCompare(b.id)))]));
  return {version:2,generation:'fixture-facets',officialRoot,totalDocuments:docs.length,propertyKeys:Object.keys(properties),countUnit:'documents',
    catalog:{kind:category==='all'&&!query&&propertyKey===null?'canonical':'query',category,propertyKey,query},facets};
}
const server=http.createServer((req,res)=>{if(req.url==='/'){res.writeHead(200,{'content-type':'text/html'});res.end(page);return;}if(req.url==='/static/js/copal.js'){res.writeHead(200,{'content-type':'text/javascript'});res.end(copal);return;}if(req.url.startsWith('/static/')){const file=path.join(root,req.url.slice(1));if(!file.startsWith(path.join(root,'static'))){res.writeHead(403);res.end();return;}try{const type=file.endsWith('.js')?'text/javascript':file.endsWith('.css')?'text/css':'application/octet-stream';res.writeHead(200,{'content-type':type});res.end(fs.readFileSync(file));}catch{res.writeHead(404);res.end();}return;}if(req.url.startsWith('/api/copal/graph/facets')){if(facetHandler){facetHandler(req,res);return;}if(!facetCorpus.length){res.writeHead(404);res.end();return;}res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(buildFacetResponse(req.url)));return;}res.writeHead(404);res.end();});
let browser,ws,serverClosed=false,profile; let serverPort;
try {
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve)); serverPort=server.address().port;
 const chrome=['/usr/bin/chromium','/usr/bin/chromium-browser','/usr/bin/google-chrome','/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'].find(fs.existsSync); assert(chrome,'Chrome executable required');
 profile=fs.mkdtempSync(path.join(os.tmpdir(),'copal-graph-')); browser=spawn(chrome,['--headless=new','--no-sandbox','--disable-gpu',`--user-data-dir=${profile}`,'--remote-debugging-port=0','about:blank'],{stdio:['ignore','pipe','pipe']});
 let port, startupLog=''; const listen=chunk=>{startupLog+=String(chunk);const m=String(chunk).match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);if(m)port=Number(m[1]);};browser.stdout.on('data',listen);browser.stderr.on('data',listen);for(let i=0;!port&&i<1500;i++)await delay(20);assert(port,`DevTools did not start within 30 seconds: ${startupLog.slice(-1000)}`);
 const target=await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(`http://127.0.0.1:${serverPort}/`)}`,{method:'PUT'})).json();
 ws=new WebSocket(target.webSocketDebuggerUrl);await new Promise((resolve,reject)=>{ws.addEventListener('open',resolve,{once:true});ws.addEventListener('error',reject,{once:true});});let seq=0;const pending=new Map();ws.addEventListener('message',e=>{const m=JSON.parse(e.data);if(m.id&&pending.has(m.id)){pending.get(m.id)(m);pending.delete(m.id);}});
 const cdp=(method,params={})=>new Promise((resolve,reject)=>{const id=++seq;pending.set(id,m=>m.error?reject(new Error(m.error.message)):resolve(m.result));ws.send(JSON.stringify({id,method,params}));});
 const evalPage=async(expression,awaitPromise=true)=>{const r=await cdp('Runtime.evaluate',{expression,awaitPromise,returnByValue:true});if(r.exceptionDetails)throw new Error(JSON.stringify(r.exceptionDetails));return r.result?.value;};
 const waitFor=async(predicate,label)=>{for(let i=0;i<500;i++){if(await predicate())return;await delay(20);}throw new Error(`Timed out: ${label}`);};
 const openPicker=async(category)=>{
  await evalPage(`document.querySelector('.copal-graph-filter-popup header button')?.click();document.querySelector('[data-facet-trigger="${category}"]').click()`);
  await waitFor(()=>evalPage(`Boolean(document.querySelector('.copal-graph-filter-popup')) && !document.querySelector('.copal-graph-picker-status')?.textContent.includes('Loading')`),`${category} picker`);
 };
 const closePicker=()=>evalPage(`document.querySelector('.copal-graph-filter-popup header button')?.click()`);
 await cdp('Runtime.enable');await cdp('Page.enable');await cdp('Emulation.setDeviceMetricsOverride',{width:1400,height:1000,deviceScaleFactor:1,mobile:false});await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 for(let i=0;i<1500;i++){if(await evalPage('window.__ready?.()'))break;await delay(20);}assert(await evalPage('window.__ready?.()'),'Graph fixture did not load within 30 seconds');
 const docs=[{id:'note-1',name:'Alpha.md',kind:'note',text:'Alpha',links:['Wiki.md'],relations:[{targetDocumentId:'wiki-1',kind:'embed'}]},{id:'wiki-1',name:'Wiki.md',kind:'wiki',text:'Wiki',links:[],relations:[]}];
 const planning={tracks:[{id:'track-1',name:'Track One',tasks:[{id:'event-1',title:'Shared Event',startDate:'2026-01-01',dueDate:'2026-01-02',primaryTrackId:'track-1',sharedTrackIds:['track-2']}]},{id:'track-2',name:'Track Two',tasks:[]}],floatingTodos:[]};
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__register();window.__graphTest.render()`);await delay(40);
 const report=[];
 const nav=await evalPage(`({mode:document.querySelector('[data-graph-mode]').value,graph:location.pathname,options:[...document.querySelectorAll('[data-graph-mode] option')].map(x=>x.value),nodes:document.querySelectorAll('.copal-graph-node').length})`);assert.equal(nav.mode,'documents');assert.deepEqual(nav.options,['documents','structure','galaxy','code']);report.push({case:'single Graph destination',nav});
 await evalPage(`(()=>{const s=document.querySelector('[data-graph-mode]');s.value='galaxy';s.dispatchEvent(new Event('change',{bubbles:true}))})()`);await delay(20);const actualGalaxy=await evalPage(`({mode:document.querySelector('[data-graph-mode]').value,nodes:[...document.querySelectorAll('.copal-graph-node')].map(n=>n.dataset.graphId)})`);assert.equal(actualGalaxy.mode,'galaxy');assert(actualGalaxy.nodes.includes('event:event-1'));report.push({case:'actual mode selector/renderGraph',actualGalaxy});
 await evalPage(`(()=>{const s=document.querySelector('[data-graph-mode]');s.value='documents';s.dispatchEvent(new Event('change',{bubbles:true}))})()`);await delay(20);
 const projections=await evalPage('window.__graphTest.graphState()');assert.equal(projections.docs.edges.length,2);assert.equal(projections.galaxy.nodes.filter(n=>n.id==='event:event-1').length,1);assert.equal(projections.galaxy.edges.length,2);report.push({case:'document and Galaxy projections',documentEdges:projections.docs.edges.map(e=>e.type),galaxyEdges:projections.galaxy.edges.map(e=>e.type)});
 await evalPage(`window.__graphTest.renderProjection('documents',window.__graphTest.graphState().docs.nodes,window.__graphTest.graphState().docs.edges);document.querySelector('.copal-graph-search').value='Wiki';document.querySelector('.copal-graph-search').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.copal-graph-ctrl[title="Zoom in"]').click()`);await delay(20);const docState=await evalPage('window.__graphTest.snapshot()');assert.equal(docState.mode,'documents');assert.equal(docState.modes.documents.filters.search,'Wiki');assert.notDeepEqual(docState.modes.documents.camera,{x:0,y:0,w:1000,h:650});
 await evalPage(`window.__graphTest.renderProjection('galaxy',window.__graphTest.graphState().galaxy.nodes,window.__graphTest.graphState().galaxy.edges);document.querySelector('.copal-graph-search').value='Track';document.querySelector('.copal-graph-search').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('.copal-graph-ctrl[title="Zoom out"]').click()`);const both=await evalPage('window.__graphTest.snapshot()');assert.equal(both.modes.documents.filters.search,'Wiki');assert.equal(both.modes.galaxy.filters.search,'Track');assert.notDeepEqual(both.modes.documents.camera,both.modes.galaxy.camera);report.push({case:'mode-scoped filter/camera restore',documents:both.modes.documents,galaxy:both.modes.galaxy});
 await evalPage(`window.__graphTest.renderProjection('documents',window.__graphTest.graphState().docs.nodes,window.__graphTest.graphState().docs.edges)`);const restored=await evalPage('window.__graphTest.snapshot()');assert.equal(restored.modes.documents.filters.search,'Wiki');
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({account:'acct-b',workspace:'ws-b',docs,planning})},{body:document.querySelector('#graph')}));window.__graphTest.render()`);const isolated=await evalPage('window.__graphTest.snapshot()');assert.equal(isolated.mode,'documents');assert.equal(isolated.modes.documents.filters.search,'');report.push({case:'owner/workspace invalidation',scope:'acct-b/ws-b',search:isolated.modes.documents.filters.search});
 const route=await evalPage(`({view:window.__graphTest.route('galaxy'),mode:window.__graphTest.snapshot().mode})`);assert.deepEqual(route,{view:'graph',mode:'galaxy'});report.push({case:'legacy Galaxy route alias',route});
 await evalPage(`window.__graphTest.route('graph','documents');window.__graphTest.render();document.querySelector('.copal-graph-search').value='Wiki';document.querySelector('.copal-graph-search').dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('[data-graph-id="document:wiki-1"]').focus()`);await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39,nativeVirtualKeyCode:39});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowRight',code:'ArrowRight',windowsVirtualKeyCode:39,nativeVirtualKeyCode:39});const focused=await evalPage('document.activeElement?.getAttribute("data-graph-id")');const hidden=await evalPage('(()=>{const node=document.querySelector("[data-graph-id=\\"document:note-1\\"]");return node ? getComputedStyle(node).display : "unmounted"})()');assert.ok(hidden==='none' || hidden==='unmounted');assert.equal(focused,'document:wiki-1');report.push({case:'trusted filtered keyboard node navigation',focused,hidden});
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__graphTest.actual();window.__graphTest.handoffs=[];document.querySelector('[data-graph-id="document:wiki-1"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);assert.deepEqual(await evalPage('window.__graphTest.handoffs'),[]);await evalPage(`document.querySelector('[data-graph-id="document:wiki-1"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);assert.deepEqual(await evalPage('window.__graphTest.handoffs'),[['wiki','wiki-1']]);await delay(20);assert.deepEqual(await evalPage('window.__graphTest.context()'),{view:'wiki',selected:'wiki-1'});await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__graphTest.actual();window.__graphTest.handoffs=[];document.querySelector('[data-graph-id="document:wiki-1"]').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);assert.deepEqual(await evalPage('window.__graphTest.handoffs'),[]);assert.deepEqual(await evalPage('window.__graphTest.context()'),{view:'graph',selected:null});await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__graphTest.route('galaxy');window.__graphTest.actual();window.__graphTest.handoffs=[];document.querySelector('[data-graph-id="track:track-1"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));document.querySelector('[data-graph-id="event:event-1"]').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}))`);assert.deepEqual(await evalPage('window.__graphTest.handoffs'),[['track','track-1'],['event','event-1']]);report.push({case:'actual document/wiki/track/event handoffs',handoffs:await evalPage('window.__graphTest.handoffs')});
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__register();window.__graphTest.route('graph','documents');window.__graphTest.actual()`);const beforePan=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');const rect=await evalPage('(()=>{const r=document.querySelector(".copal-graph").getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()');const px=rect.x+40,py=rect.y+rect.h-30;await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:px,y:py,button:'left',buttons:1,clickCount:1});await delay(20);await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:px+20,y:py,button:'left',buttons:1});await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:px+100,y:py,button:'left',buttons:1});await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:px+100,y:py,button:'left',buttons:0,clickCount:1});await delay(30);const pan=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');assert.notEqual(pan,beforePan,JSON.stringify({beforePan,pan,rect,element:await evalPage(`document.elementFromPoint(${px},${py})?.tagName`)}));report.push({case:'trusted reduced-motion pan',before:beforePan,after:pan});
 const corpus=Array.from({length:5001},(_,index)=>({id:`doc-${index}`,name:`Corpus ${index}`,label:`Corpus ${index}`,kind:'note'}));await evalPage(`window.__graphTest.renderProjection('documents',${JSON.stringify(corpus)},[])`);await evalPage(`(()=>{const input=document.querySelector('.copal-graph-search');input.value='Corpus 5000';input.dispatchEvent(new Event('input',{bubbles:true}))})()`);const bounded=await evalPage('({mounted:document.querySelectorAll(".copal-graph-node").length,match:document.querySelector("[data-graph-id=\\"doc-5000\\"]")?.getAttribute("data-graph-id"),info:document.querySelector(".copal-graph-info")?.textContent})');assert(bounded.mounted<=300);assert.equal(bounded.match,'doc-5000');assert.match(bounded.info,/1 nodes and 0 edges/);report.push({case:'5,001 bounded search remount',bounded});await evalPage(`(()=>{const input=document.querySelector('.copal-graph-search');input.value='';input.dispatchEvent(new Event('input',{bubbles:true}))})()`);const layout=await evalPage('(()=>{const circles=[...document.querySelectorAll(".copal-graph-node circle")];const pairs=circles.map(c=>`${c.getAttribute("cx")},${c.getAttribute("cy")}`);const xs=circles.map(c=>Number(c.getAttribute("cx")));return {mounted:circles.length,distinct:new Set(pairs).size,span:Math.max(...xs)-Math.min(...xs)}})()');assert.equal(layout.mounted,300);assert(layout.distinct>200);assert(layout.span>700);report.push({case:'bounded spatial layout',layout});
 await evalPage(`window.__graphTest.setup(Object.assign({account:'acct-5001',workspace:'ws-5001',docs:${JSON.stringify(corpus)},planning:{tracks:[],floatingTodos:[]}},{body:document.querySelector('#graph')}));window.__graphTest.renderMind()`);const mindBounded=await evalPage('({rows:document.querySelectorAll(".copal-mind-doc-item").length,status:document.querySelector(".copal-mind-picker-status")?.textContent})');assert(mindBounded.rows<=101);assert.match(mindBounded.status,/5001/);await evalPage(`(()=>{const input=document.querySelector('.copal-mind-doc-search');input.value='Corpus 5000';input.dispatchEvent(new Event('input',{bubbles:true}))})()`);const mindSearch=await evalPage('document.querySelectorAll(".copal-mind-doc-item").length');assert.equal(mindSearch,1);report.push({case:'5,001 bounded Mind source picker',mindBounded,mindSearch});
 const mindText='---\ntitle: Fixture\n---\n# Root\n```md\n# Fake\n```\n## Child\n# Sibling\nBody';
 const mindDoc={id:'mind-doc',name:'Mind.md',kind:'note',text:'Saved only',head:'mind-head-1',resource:{key:{accountId:'acct-mind',workspaceId:'ws-mind',provider:'copal',resourceId:'mind-resource'}},links:[],relations:[]};
 await evalPage(`window.__graphTest.setup({account:'acct-mind',workspace:'ws-mind',docs:${JSON.stringify([mindDoc])},drafts:{'mind-doc':{text:${JSON.stringify(mindText)},expectedRevision:{kind:'copalHead',value:'mind-head-1'},localRevision:4}},body:document.querySelector('#graph')});window.__graphTest.setSource('mind-doc');window.__graphTest.renderMind()`);
 const mindShape=await evalPage('({rows:[...document.querySelectorAll(".copal-mind-tree-label")].map(node=>node.textContent),count:document.querySelectorAll(".copal-mind-tree-node").length})');assert.equal(mindShape.count,3);assert.deepEqual(mindShape.rows.map(value=>value.trim()),['Root','Child','Sibling']);report.push({case:'mounted unsaved Mind draft excludes frontmatter/fenced fake headings',mindShape});
 const beforeRename=(await evalPage('window.__graphTest.mindTransactions()')).length;
 await evalPage(`(()=>{const label=[...document.querySelectorAll('.copal-mind-tree-label')].find(item=>item.textContent.trim()==='Child');label.closest('.copal-mind-tree-node').dispatchEvent(new MouseEvent('dblclick',{bubbles:true}));const input=document.querySelector('.copal-mind-tree-input');input.value='Renamed';input.dispatchEvent(new Event('blur'))})()`);await delay(10);assert.equal((await evalPage('window.__graphTest.mindTransactions()')).length,beforeRename+1);assert.match(await evalPage('window.__graphTest.drafts["mind-doc"].text'),/## Renamed/);
 await evalPage(`(()=>{document.querySelector('[data-line="8"]')?.click();[...document.querySelectorAll('.copal-mind-toolbar button')].find(button=>button.textContent.trim()==='Heading').click()})()`);await delay(10);assert.equal((await evalPage('window.__graphTest.mindTransactions()')).length,beforeRename+2);assert.match(await evalPage('window.__graphTest.drafts["mind-doc"].text'),/## New heading/);
 const reparentCount=(await evalPage('window.__graphTest.mindTransactions()')).length;
 await evalPage(`(()=>{const nodes=[...document.querySelectorAll('.copal-mind-tree-node')];const source=nodes.find(item=>item.textContent.includes('Sibling'));const target=nodes.find(item=>item.textContent.includes('Root'));const transfer=new DataTransfer();source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));target.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:transfer}));target.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}))})()`);await delay(10);assert.equal((await evalPage('window.__graphTest.mindTransactions()')).length,reparentCount+1);
 const cycleCount=(await evalPage('window.__graphTest.mindTransactions()')).length;
 await evalPage(`(()=>{const nodes=[...document.querySelectorAll('.copal-mind-tree-node')];const source=nodes.find(item=>item.textContent.includes('Root'));const target=nodes.find(item=>item.textContent.includes('Renamed'));const transfer=new DataTransfer();source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));target.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:transfer}));target.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}))})()`);assert.equal((await evalPage('window.__graphTest.mindTransactions()')).length,cycleCount);
 await evalPage(`(()=>{const nodes=[...document.querySelectorAll('.copal-mind-tree-node')];const source=nodes.find(item=>item.textContent.includes('Root'));const target=nodes.find(item=>item.textContent.includes('Renamed'));const transfer=new DataTransfer();source.dispatchEvent(new DragEvent('dragstart',{bubbles:true,dataTransfer:transfer}));window.__graphTest.drafts['mind-doc'].localRevision+=1;target.dispatchEvent(new DragEvent('dragover',{bubbles:true,dataTransfer:transfer}));target.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}))})()`);assert.equal((await evalPage('window.__graphTest.mindTransactions()')).length,cycleCount);
 const readOnlyDoc={...mindDoc,id:'readonly-doc',name:'Read only.md',readOnly:true};await evalPage(`window.__graphTest.setup({account:'acct-mind',workspace:'ws-mind',docs:${JSON.stringify([readOnlyDoc])},drafts:{'readonly-doc':{text:${JSON.stringify(mindText)},expectedRevision:{kind:'copalHead',value:'mind-head-1'},localRevision:0}},body:document.querySelector('#graph')});window.__graphTest.setSource('readonly-doc');window.__graphTest.renderMind()`);assert.equal(await evalPage('document.querySelector(".copal-mind-tree-node")?.draggable'),false);assert.equal(await evalPage('(()=>{const n=document.querySelector(".copal-mind-tree-node");const d=new DataTransfer();n.dispatchEvent(new DragEvent("dragstart",{bubbles:true,dataTransfer:d}));const event=new DragEvent("dragover",{bubbles:true,dataTransfer:d});n.dispatchEvent(event);return d.dropEffect})()'),'none');
 const stateCases=[
  {id:'base-source',doc:{id:'base-source',name:'View.base',kind:'base',text:'version: 1',head:'base-1'},title:'Structure source unsupported'},
  {id:'missing-source',doc:null,title:'Structure source unavailable'},
  {id:'parse-source',doc:{id:'parse-source',name:'Broken.md',kind:'note',text:'',head:'parse-1',rawPreserved:true,recoveryState:'malformed-preserved',note_error:'native parse failed'},title:'Structure source parse failed'},
  {id:'empty-source',doc:{id:'empty-source',name:'Empty.md',kind:'note',text:'plain body',head:'empty-1'},title:'No headings or bullets found in this source'},
 ];
 for (const item of stateCases) { const docs=item.doc?[item.doc]:[]; await evalPage(`window.__graphTest.setup({account:'acct-state',workspace:'ws-state',docs:${JSON.stringify(docs)},body:document.querySelector('#graph')});window.__graphTest.setSource(${JSON.stringify(item.id)});window.__graphTest.renderMind()`);await delay(2);assert.match(await evalPage('document.body.textContent'),new RegExp(item.title.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&'))); }
 report.push({case:'mounted Mind mutations reject cycle/stale/read-only and expose unsupported/missing/parse/no-heading states',transactions:await evalPage('window.__graphTest.mindTransactions().length')});
 // S20: legacy Mind entrypoints resolve into Graph structure mode, not a
 // separate Mind surface. The mode vocabulary is Documents/Structure/Galaxy.
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__graphTest.route('mind')`);const mindRoute=await evalPage(`({view:window.__graphTest.route('mind'),mode:window.__graphTest.snapshot().mode,options:[...document.querySelectorAll('[data-graph-mode] option')].map(x=>x.value)})`);assert.equal(mindRoute.view,'graph');assert.equal(mindRoute.mode,'structure');assert.ok(!mindRoute.options.includes('mind'),'Mind is not a separate Graph mode');report.push({case:'legacy Mind resolves to Graph structure mode',mindRoute});
 // S20: wheel zoom is pointer-anchored and moves the camera without buttons.
 await evalPage(`window.__graphTest.setup(Object.assign(${JSON.stringify({docs,planning})},{body:document.querySelector('#graph')}));window.__register();window.__graphTest.route('graph','documents');window.__graphTest.actual()`);const beforeWheel=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');const wrect=await evalPage(`(()=>{const r=document.querySelector(".copal-graph").getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height}})()`);const wx=wrect.x+wrect.w/2,wy=wrect.y+wrect.h/2;await cdp('Input.dispatchMouseEvent',{type:'mouseWheel',x:wx,y:wy,deltaX:0,deltaY:-120,button:'none',buttons:0});await delay(30);const afterWheel=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');assert.notEqual(afterWheel,beforeWheel,JSON.stringify({beforeWheel,afterWheel}));report.push({case:'pointer-anchored wheel zoom',before:beforeWheel,after:afterWheel});
 const fitBefore=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');await evalPage(`document.querySelector('.copal-graph-ctrl[title="Fit graph"]').click()`);await delay(20);const fitAfter=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');assert.notEqual(fitAfter,fitBefore);report.push({case:'Fit control reframes mounted content',fitBefore,fitAfter});
 // S20: real facet filters and the official-docs default come from scoped
 // metadata, not a hardcoded kind allowlist.
 const facetDocs=[{id:'p1',name:'Notes/Personal.md',kind:'note',tags:['keep'],properties:{status:'open'},text:'personal'},{id:'o1',name:'OpenClank/Start Here',kind:'note',tags:['builtin'],properties:{product:'open-clank',builtin:true},builtin:true,text:'official'}]; facetCorpus=facetDocs;
 await evalPage(`window.__graphTest.setup(Object.assign({account:'acct-facet',workspace:'ws-facet',docs:${JSON.stringify(facetDocs)},planning:{tracks:[],floatingTodos:[]}},{body:document.querySelector('#graph')}));window.__graphTest.actual()`);await delay(20);const officialDefault=await evalPage(`({mounted:document.querySelectorAll('.copal-graph-node').length,official:document.querySelector('[data-graph-id="document:o1"]')!==null})`);assert.equal(officialDefault.official,false,'official docs are hidden by default');assert.equal(officialDefault.mounted,1);
 await openPicker('properties');await evalPage(`(()=>{const t=document.querySelector('[aria-label="Include official documentation"]');t.checked=true;t.dispatchEvent(new Event('change',{bubbles:true}))})()`);await closePicker();
 const officialShown=await evalPage(`document.querySelector('[data-graph-id="document:o1"]')!==null`);assert.equal(officialShown,true,'official docs are explicitly includable');report.push({case:'official docs excluded by default and includable',officialDefault,officialShown});
 await openPicker('tags');const tagPick=await evalPage(`(()=>{const row=[...document.querySelectorAll('[data-facet-group="Tags"] label')].find(row=>row.querySelector('span').textContent==='keep');const input=row.querySelector('input');input.click();return {label:row.textContent}})()`);await closePicker();const tagFiltered=await evalPage(`({nodes:document.querySelectorAll('.copal-graph-node').length,info:document.querySelector('.copal-graph-info')?.textContent,visible:[...document.querySelectorAll('.copal-graph-node')].map(n=>n.dataset.graphId)})`);assert.equal(tagFiltered.nodes,1,'a tag facet narrows the graph to matching documents');assert.match(tagFiltered.info||'',/1 nodes/,'info reports the narrowed node count');report.push({case:'real tag facet filter',tagPick,tagFiltered});
 // S20: structure mode shows nested bullets beside headings, and a source
 // without structure gets a useful empty state.
 const structureText='# Root\n- top\n  - nested\n## Child\n\nJust prose after.';
 const structureDoc={id:'structure-doc',name:'Structure.md',kind:'note',text:'saved',head:'sh-1',resource:{key:{accountId:'acct-structure',workspaceId:'ws-structure',provider:'copal',resourceId:'structure-res'}},links:[],relations:[]};
 await evalPage(`window.__graphTest.setup({account:'acct-structure',workspace:'ws-structure',docs:${JSON.stringify([structureDoc])},drafts:{'structure-doc':{text:${JSON.stringify(structureText)},expectedRevision:{kind:'copalHead',value:'sh-1'},localRevision:0}},body:document.querySelector('#graph')});window.__graphTest.setSource('structure-doc');window.__graphTest.renderMind()`);const structureShape=await evalPage('({rows:[...document.querySelectorAll(".copal-mind-tree-label")].map(node=>node.textContent.trim()),kinds:[...document.querySelectorAll(".copal-mind-tree-node")].map(node=>node.dataset.structureKind)})');assert.deepEqual(structureShape.rows,['Root','top','nested','Child']);assert.deepEqual(structureShape.kinds,['heading','bullet','bullet','heading'],'nested bullets appear as bullet structure nodes');report.push({case:'structure mode shows headings and nested bullets',structureShape});

 // S02B: exercise actual renderGraph, API fetches and mounted controls with
 // synthetic responses. Delayed responses are explicitly released by tests.
 const settled=()=>waitFor(async()=>!(await evalPage(`document.querySelector('.copal-graph-info')?.textContent.includes('(loading)')`)),'facet refinement');
 const setup=async(docs,account)=>evalPage(`window.__graphTest.setup({account:${JSON.stringify(account)},docs:${JSON.stringify(docs)},body:document.querySelector('#graph')});window.__graphTest.actual()`);
 const respond=(res,value)=>{res.writeHead(200,{'content-type':'application/json'});res.end(JSON.stringify(value));};
 const filterSearch=async(value)=>evalPage(`(()=>{const input=document.querySelector('.copal-graph-search');input.value=${JSON.stringify(value)};input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
 const ids=()=>evalPage(`[...document.querySelectorAll('.copal-graph-node')].map(node=>node.dataset.graphId)`);
 const searchDocs=[{id:'body',name:'Alpha.md',kind:'note',text:'unique body needle',tags:['shared']},{id:'tag',name:'Beta.md',kind:'note',text:'other',tags:['unique-tag-needle','shared']}];
 facetCorpus=searchDocs;facetHandler=null;
 await setup(searchDocs,'s02b-search');await settled();
 await openPicker('tags');const sharedCount=await evalPage(`[...document.querySelectorAll('[data-facet-group="Tags"] label')].find(row=>row.querySelector('span').textContent==='shared')?.querySelector('small').textContent`);await closePicker();
 assert.equal(sharedCount,'2 documents','local/server overlap must not double counts');
 await filterSearch('body needle');assert.deepEqual(await ids(),['document:body']);
 await evalPage(`document.querySelector('.copal-graph-node').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
 await filterSearch('unique-tag-needle');assert.deepEqual(await ids(),['document:tag']);
 assert.equal(await evalPage('window.__graphTest.snapshot().modes.documents.selection'),null,'filtered selection is cleared');
 assert.equal(await evalPage('document.querySelectorAll(".copal-graph-node.dimmed").length'),0);
 await evalPage(`document.querySelector('.copal-graph-node').dispatchEvent(new MouseEvent('click',{bubbles:true}));window.__graphTest.actual()`);await settled();
 assert.equal(await evalPage('window.__graphTest.snapshot().modes.documents.selection.nodeId'),'document:tag','matching selection restores across refinement/render');
 report.push({case:'S02B same-corpus counts, body/tag search and filtered selection',sharedCount});

 const dotDocs=[{id:'hidden',name:'.private/Secret.md',kind:'note'},{id:'wiki-dot',name:'.memes/Page',kind:'wiki'},{id:'public',name:'Public.md',kind:'note'}];
 facetCorpus=dotDocs;
 await setup(dotDocs,'s02b-dot');await evalPage('window.__graphTest.dotFolders(false);window.__graphTest.actual()');await settled();
 assert.deepEqual((await ids()).sort(),['document:public','document:wiki-dot']);
 await evalPage('window.__graphTest.dotFolders(true);window.__graphTest.actual()');await settled();assert.equal((await ids()).length,3);
 report.push({case:'S02B authored Editor dot-folder preference preserves canonical Wiki'});

 const pageFacet=(generation,tags,more=false)=>({version:2,generation,totalDocuments:1,officialRoot:null,propertyKeys:[],catalog:{kind:'canonical'},countUnit:'documents',facets:{kinds:{values:[{value:'note',count:1}],hasMore:false},folders:{values:[],hasMore:false},tags:{values:tags.map(value=>({value,count:1})),hasMore:more},properties:{}}});
 let driftRequests=0;
 facetHandler=(req,res)=>{driftRequests++;respond(res,driftRequests===1?pageFacet('old',['old-only'],true):pageFacet('new',driftRequests===2?['discard-me']:['new-only']));};
 await setup([searchDocs[0]],'s02b-drift');await settled();
 assert.equal(driftRequests,3,'generation drift restarts at page zero');
 const driftTags=await evalPage(`window.__graphTest.catalogSnapshot().tags.map(item=>item.value)`);
 assert.deepEqual(driftTags,['new-only']);
 const driftCache=await evalPage('window.__graphTest.catalogSnapshot()');
 assert.equal(driftCache.generation,'new');assert(driftCache.tags.every(tag=>tag.value==='new-only'));
 report.push({case:'S02B actual facet generation drift discards accumulator/cache',driftRequests,driftTags});

 let unstableRequests=0;
 facetHandler=(req,res)=>respond(res,pageFacet(`unstable-${++unstableRequests}`,['remote'],true));
 await setup([searchDocs[0]],'s02b-unstable');await settled();assert.equal(unstableRequests,6);
 assert.deepEqual(await ids(),['document:body']);
 assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/Facets partial \(generation changed\)/);
 assert.equal(await evalPage('window.__graphTest.catalogSnapshot()'),null,'unstable generations do not publish a canonical cache');
 report.push({case:'S02B repeated generation drift retains local partial graph',unstableRequests});

 let cappedRequests=0;
 facetHandler=(req,res)=>respond(res,pageFacet('capped',[`page-${++cappedRequests}`],true));
 const capped=await evalPage('window.__graphTest.facets()');assert.equal(cappedRequests,25);assert.equal(capped.incomplete,true);assert.equal(capped.tags.length,25);assert.equal(capped.kinds[0].count,1,'repeated whole-corpus kind count is not summed');assert.deepEqual(capped.continuation,{offset:5000,limit:200,generation:'capped'});
 report.push({case:'S02B capped actual facet walk remains incomplete',cappedRequests});

 facetHandler=(req,res)=>{res.writeHead(503,{'content-type':'application/json'});res.end(JSON.stringify({detail:'fixture unavailable'}));};
 await setup([searchDocs[0]],'s02b-failure');await settled();assert.deepEqual(await ids(),['document:body']);assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/Facets partial \(local-only\)/);
 await openPicker('tags');assert.match(await evalPage(`document.querySelector('.copal-graph-picker-status').textContent`),/loaded documents/);assert.equal(await evalPage(`document.querySelector('[data-facet-group="Tags"] small')?.textContent`),'1 documents');await closePicker();
 report.push({case:'S02B unavailable facets retain local projection with honest status'});
 facetHandler=(req,res)=>respond(res,pageFacet('',[],false));
 await setup([searchDocs[0]],'s02b-no-generation');await settled();assert.deepEqual(await ids(),['document:body']);assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/Facets partial \(local-only\)/);assert.equal(await evalPage('window.__graphTest.catalogSnapshot()'),null);
 report.push({case:'S02B missing generation cannot cache a complete catalog'});

 const held=[];facetHandler=(req,res)=>{res.fixtureAccount=req.headers['x-copal-account'];held.push(res);};
 const release=async()=>{const before=await evalPage('window.__graphTest.facetCompleted');respond(held.shift(),pageFacet('late',['late']));await waitFor(async()=>(await evalPage('window.__graphTest.facetCompleted'))>before,'released facet completion');};
 await setup([searchDocs[0]],'s02b-editor');await waitFor(()=>held.length===1,'pending Editor switch');await evalPage('window.__graphTest.editor()');await release();assert.equal(await evalPage('document.querySelector("#graph").textContent'),'Editor sentinel');
 await setup([searchDocs[0]],'s02b-mode');await waitFor(()=>held.length===1,'pending Graph mode switch');await evalPage(`{const mode=document.querySelector('[data-graph-mode]');mode.value='galaxy';mode.dispatchEvent(new Event('change',{bubbles:true}));}`);await release();assert.equal(await evalPage('window.__graphTest.snapshot().mode'),'galaxy');assert.equal(await evalPage('document.querySelector("[data-graph-mode]").value'),'galaxy','obsolete Documents completion cannot replace Galaxy');
 await setup([searchDocs[0]],'s02b-remount');await waitFor(()=>held.length===1,'first remount fetch');await evalPage('window.__graphTest.actual();document.querySelector(".copal-graph-wrap").dataset.latest="yes"');await release();assert.equal(await evalPage('document.querySelector(".copal-graph-wrap").dataset.latest'),'yes');await waitFor(()=>held.length===1,'second remount fetch after old response');await release();await settled();
 await setup([searchDocs[0]],'s02b-close');await waitFor(()=>held.length===1,'close fetch');await evalPage('window.__graphTest.hide();document.querySelector("#graph").textContent="Closed sentinel"');await release();assert.equal(await evalPage('document.querySelector("#graph").textContent'),'Closed sentinel');
 await setup([searchDocs[0]],'s02b-scope-old');await waitFor(()=>held.length===1,'old scope fetch');await setup([searchDocs[1]],'s02b-scope-new');await evalPage('document.querySelector(".copal-graph-wrap").dataset.latest="new-scope"');await release();await waitFor(()=>held.length===1,'new scope fetch after old response');assert.deepEqual(await ids(),['document:tag']);assert.equal(await evalPage('document.querySelector(".copal-graph-wrap").dataset.latest'),'new-scope','old scope completion cannot remount new graph');await release();await settled();
 for(let i=0;i<3;i++){await setup([searchDocs[0]],`s02b-unmount-${i}`);await waitFor(()=>held.length===1,'unmount fetch');await evalPage('document.querySelector(".copal-graph-wrap")._copalDestroy();document.querySelector("#graph").textContent="Unmount sentinel"');await release();assert.equal(await evalPage('document.querySelector("#graph").textContent'),'Unmount sentinel');}
 report.push({case:'S02B delayed Graph→Editor/Galaxy, same-mode render, close, scope and repeated unmount reject obsolete completions'});

 // S04 integration: a substantial scoped Documents corpus, not only a
 // standalone toolbar fixture. Scalar selections must survive real persistence.
 const matrix=Array.from({length:406},(_,index)=>({id:`matrix-${index}`,head:`matrix-${index}-h1`,name:`Matrix/Note ${index}.md`,kind:index===404?'wiki':'note',
   text:`Matrix body ${index}`,tags:[index%2?'odd':'even'],properties:{flag:index%2?'true':true,status:index%3?'open':'done',number:index},
   links:index===400?['Matrix/Note 401.md']:[],relations:[],...(index===405?{builtin:true}: {})}));
 Object.defineProperty(matrix[0].properties,'__proto__',{value:'safe',enumerable:true});
 Object.defineProperty(matrix[0].properties,'constructor',{value:true,enumerable:true});
 facetCorpus=matrix;facetHandler=null;await setup(matrix,'s04-matrix');await settled();
 assert.equal(await evalPage('document.querySelectorAll(".copal-graph-node").length'),300);
 assert.equal(await evalPage('document.querySelectorAll(".copal-graph-toolbar input[type=checkbox]").length'),0,'406-doc graph has no closed checkbox wall');
 assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/405 nodes/,'official document still excluded in substantial corpus');
 const beforeDiscover=await evalPage('window.__graphTest.snapshot().modes.documents.filters');
 await openPicker('properties');await evalPage(`{const input=document.querySelector('[aria-label="Find graph filter property"]');input.value='flag';input.dispatchEvent(new Event('input'));document.querySelector('[data-property-key="flag"]').click();}`);
 await waitFor(()=>evalPage('document.querySelectorAll("[data-facet-group=flag] input").length===2'),'typed flag options');
 assert.deepEqual(await evalPage('window.__graphTest.snapshot().modes.documents.filters'),beforeDiscover,'opening/searching the property picker never changes predicates');
 await evalPage(`document.querySelector('[data-facet-group=flag] input[data-facet-value="boolean:true"]').click()`);
 assert.deepEqual(await evalPage('window.__graphTest.snapshot().modes.documents.filters.properties.flag'),[{type:'boolean',value:true}]);
 assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/203 nodes/);
 await evalPage(`[...document.querySelectorAll('[data-facet-group=flag] input')].find(input=>input.dataset.facetValue==='string:'+JSON.stringify('true')).click()`);
 assert.deepEqual(await evalPage('window.__graphTest.snapshot().modes.documents.filters.properties.flag.map(item=>item.type).sort()'),['boolean','string']);
 assert.match(await evalPage('document.querySelector(".copal-graph-info").textContent'),/405 nodes/);
 await closePicker();await evalPage(`document.querySelector('[aria-label="Reset graph filters"]').click()`);
 await openPicker('properties');await evalPage(`{const input=document.querySelector('[aria-label="Find graph filter property"]');input.value='constructor';input.dispatchEvent(new Event('input'));document.querySelector('[data-property-key="constructor"]').click();}`);
 await waitFor(()=>evalPage('document.querySelectorAll("[data-facet-group=constructor] input").length===1'),'prototype-named property');
 await evalPage(`document.querySelector('[data-facet-group=constructor] input').click()`);assert.deepEqual(await ids(),['document:matrix-0']);
 await closePicker();await evalPage(`document.querySelector('[aria-label="Reset graph filters"]').click()`);
 await filterSearch('Note 400.md');assert.deepEqual(await ids(),['document:matrix-400']);
 await evalPage(`document.querySelector('.copal-graph-node').dispatchEvent(new MouseEvent('click',{bubbles:true}))`);
 assert.equal(await evalPage('window.__graphTest.snapshot().modes.documents.selection.nodeId'),'document:matrix-400');
 assert.match(await evalPage('document.querySelector(".copal-graph-inspector").textContent'),/Note 400/);
 const matrixCamera=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');
 await evalPage(`document.querySelector('.copal-graph-ctrl[title="Zoom in"]').click()`);await delay(25);
 assert.notEqual(await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")'),matrixCamera);
 report.push({case:'S04 integrated406-doc typed OR filters, constructor metadata, off-mounted search, selection/inspector/camera',documents:matrix.length});

 // Same mounted applet at narrow/desktop sizes, short height, two inherited
 // palettes and CSS 2x zoom. This supplements (not replaces) actual-app S07.
 await evalPage(`document.querySelector('#graph').style.display='flex';document.querySelector('#graph').style.flexDirection='column'`);
 const bounds=[];
 for(const palette of ['dark','light'])for(const [width,zoom] of [[320,1],[390,1],[1100,1],[320,2],[390,2]]){
  await evalPage(`{const graph=document.querySelector('#graph');graph.style.width='${width}px';graph.style.height='480px';graph.style.zoom='${zoom}';
    for(const [key,value] of Object.entries(${JSON.stringify(palette==='dark'?{'--panel':'#242a30','--bg':'#151a20','--fg':'#eeeeee','--border':'#79828b','--accent':'#edb54a'}:{'--panel':'#ffffff','--bg':'#f3f5f7','--fg':'#17202a','--border':'#6b7280','--accent':'#1d4ed8'})}))graph.style.setProperty(key,value);}`);
  await evalPage(`document.querySelector('.copal-graph-filter-popup header button')?.click();{const trigger=getComputedStyle(document.querySelector('.copal-graph-mobile-filters')).display!=='none'?document.querySelector('.copal-graph-mobile-filters'):document.querySelector('[data-facet-trigger=properties]');trigger.click();}`);
  await waitFor(()=>evalPage('Boolean(document.querySelector(".copal-graph-filter-popup"))'),'bounded mounted popup');await delay(30);
  const measured=await evalPage(`(()=>{const root=document.querySelector('.copal-graph-wrap').getBoundingClientRect(),popup=document.querySelector('.copal-graph-filter-popup').getBoundingClientRect(),svg=document.querySelector('.copal-graph').getBoundingClientRect();return {left:popup.left>=root.left-1,right:popup.right<=root.right+1,bottom:popup.bottom<=root.bottom+1,graphHeight:svg.height,popupHeight:popup.height};})()`);
  assert.ok(measured.left&&measured.right&&measured.bottom,JSON.stringify({palette,width,zoom,measured}));assert.ok(measured.graphHeight>60,JSON.stringify({palette,width,zoom,measured}));
  bounds.push({palette,width,zoom,...measured});await closePicker();
 }
 await evalPage(`{const graph=document.querySelector('#graph');graph.style.width='1100px';graph.style.height='760px';graph.style.zoom='1';}`);
 report.push({case:'S04 full Graph short480px applet dark/light320/390/1100 bounds at1x/2x',bounds});

 // A user can open a picker before canonical discovery resolves. Its first
 // generation must not strand the current mount in a perpetual loading state.
 const canonicalHeld=[],refinementDocs=searchDocs.map((doc,index)=>({...doc,links:index===0?['Beta.md']:[]}));facetCorpus=refinementDocs;
 facetHandler=(req,res)=>{const p=new URL(req.url,'http://fixture').searchParams;if((p.get('category')||'all')==='all')canonicalHeld.push(res);else respond(res,buildFacetResponse(req.url));};
 await setup(refinementDocs,'s04-canonical-picker-race');await waitFor(()=>canonicalHeld.length===1,'held first canonical');
 await evalPage(`document.querySelector('[data-graph-id="document:body"]').dispatchEvent(new MouseEvent('click',{bubbles:true}));document.querySelector('.copal-graph-ctrl[title="Zoom in"]').click();window.__graphTest.mountedBefore=document.querySelector('.copal-graph-wrap')`);await delay(25);
 const cameraBeforeRefine=await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")');
 await openPicker('tags');await evalPage(`{const input=document.querySelector('[aria-label="Find Tags values"]');input.value='sha';input.focus();input.setSelectionRange(1,2);window.__graphTest.pickerInputBefore=input;}`);
 respond(canonicalHeld.shift(),buildFacetResponse('/api/copal/graph/facets?facet_version=2'));
 await waitFor(()=>canonicalHeld.length===1,'bounded canonical retry after picker generation');
 respond(canonicalHeld.shift(),buildFacetResponse('/api/copal/graph/facets?facet_version=2'));await settled();
 assert.equal(await evalPage('document.querySelector(".copal-graph-wrap")===window.__graphTest.mountedBefore'),true,'canonical refinement never remounts graph');
 assert.equal(await evalPage('document.querySelector(".copal-graph").getAttribute("viewBox")'),cameraBeforeRefine);
 assert.equal(await evalPage('window.__graphTest.snapshot().modes.documents.selection.nodeId'),'document:body');
 assert.equal(await evalPage('document.querySelector("[data-graph-id=\\"document:body\\"]").classList.contains("selected")'),true,'refinement preserves visible selection highlight');
 assert.equal(await evalPage('document.querySelectorAll(".copal-graph-edge.edge-highlight").length'),1,'refinement preserves selected connection highlight');
 assert.match(await evalPage('document.querySelector(".copal-graph-inspector").textContent'),/Alpha/);
 assert.deepEqual(await evalPage(`({same:document.activeElement===window.__graphTest.pickerInputBefore,value:document.activeElement.value,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd})`),{same:true,value:'sha',start:1,end:2});
 await closePicker();report.push({case:'S04 picker-first generation retries canonical without remount; selection/inspector/camera/caret survive'});
 console.log(JSON.stringify({passed:report},null,2));
} finally {
 if(ws)ws.close();
 if(browser){
  if(browser.exitCode===null){
   const exited=new Promise(resolve=>browser.once('exit',resolve));
   browser.kill('SIGTERM');
   await Promise.race([exited,delay(1500)]);
   if(browser.exitCode===null){browser.kill('SIGKILL');await Promise.race([exited,delay(500)]);}
  }
  browser.stdout?.destroy();browser.stderr?.destroy();
 }
 if(!serverClosed){serverClosed=true;server.closeAllConnections();await new Promise(resolve=>server.close(resolve));}
 if(profile)fs.rmSync(profile,{recursive:true,force:true,maxRetries:8,retryDelay:100});
}
