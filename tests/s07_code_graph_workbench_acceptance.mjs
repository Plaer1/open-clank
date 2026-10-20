#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// Baseline runs BEFORE changing the accepted S06 renderer. Fixtures describe
// backend corpus totals, not an all-node DOM or backend throughput benchmark.
const injection=`
window.__s07={setup(){configureCopalStorage('s07-fixture');state.accountId='alice';state.workspace='documents-ws';state.contextEpoch++;state.view='graph';state.docs=[];state.body=document.querySelector('#graph');state.content=state.body;state.root=document.body;state.windows.clear();state.windows.set('graph',{window:{body:state.body,content:state.body,root:state.body,visible:true,requestClose(){this.visible=false;}},selected:null});graphView=normalizeGraphState({mode:'code'});graphScopeKey=graphStorageKey(state.accountId,state.workspace);codeGraphSelection=null;renderGraph();},editor(){state.windows.set('notes',{window:{body:document.createElement('div'),content:document.createElement('div'),root:document.body,visible:true},selected:null});notesFeature={openResource:async(resource,payload)=>{window.__installed={resource,payload};return resource;}};open=async(view)=>{activateView(view);if(window.__holdEditor)await new Promise(resolve=>window.__editorRelease=resolve);};},navigate(){activateView('notes');},select(id){return codeGraphController.selectWorkspace(id);},controller(){return codeGraphController;},snapshot(){return codeGraphController?.snapshot();}};
`;
let baselineSource;
if(process.argv.includes('--baseline')){
 baselineSource=execFileSync('git',['cat-file','blob','refs/clanker/preimages/s07-workbench-20261013/static-js-copal-js-entry'],{encoding:'utf8'});
 assert.equal(createHash('sha256').update(baselineSource).digest('hex'),'701b7b6c0151fe083c6cca65bb11c4518430234a99e968ccf232b1e7076fccb2');
}
const source=(process.argv.includes('--baseline')?baselineSource:fs.readFileSync('static/js/copal.js','utf8')).replace('export default { init,',injection+'\nexport default { init,');
const chatSource=fs.readFileSync('static/js/chat.js','utf8').replace('export default chatModule;',`window.__s07Chat={attachments(ids){_pendingRegenAttachments=ids;_restoredAttachmentOwner=sessionModule.getCurrentSessionId();_restoredAttachmentArray=ids;}};export default chatModule;`);
const page=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><style>body{display:block!important}#graph{width:100%;min-width:0;height:auto}#fixture-composer{position:fixed;left:0;bottom:0;width:300px;visibility:hidden;pointer-events:none}</style><main id="graph"></main><aside id="fixture-composer"><textarea id="message">Existing draft</textarea><button class="send-btn">Send</button></aside><script>
window.__scale=100;window.__calls=[];window.__sourceCalls=[];window.__filesCalls=[];window.__pref=false;window.__sourceState='current';window.__asks=0;window.__sid='chat-a';window.__sessions=[{id:'chat-a',owner:'alice'}];window.__pendingFiles=[{name:'pending.png'}];document.querySelector('.send-btn').onclick=()=>window.__asks++;
const originalFetch=fetch;window.fetch=async(url,options={})=>{
 const json=value=>new Response(JSON.stringify(value),{status:200});
 if(String(url).includes('/api/file-policy/workspaces/')&&String(url).includes('/index/')){window.__indexCalls??=[];window.__indexCalls.push({url:String(url),body:JSON.parse(options.body)});if(window.__holdIndex&&!String(url).endsWith('/release'))await new Promise(resolve=>window.__indexRelease=resolve);if(String(url).endsWith('/status')&&window.__indexTimeouts>0){window.__indexTimeouts--;return new Response(JSON.stringify({detail:'Request exceeded 45s timeout'}),{status:504});}return json({index:window.__indexStatus||{lifecycle:'ready',head_generation:'g',freshness:'current'}});}
 if(String(url).includes('/api/memory/code/graph')){const q=JSON.parse(options.body);window.__calls.push(q);if(window.__holdQuery)await new Promise(resolve=>window.__queryRelease=resolve);return json(window.__pages[Number(q.query.cursor||0)]);}
 if(String(url).includes('/api/prefs/code_context_enabled')){if(window.__holdPrefs)await new Promise(resolve=>window.__prefsRelease=resolve);if(options.method==='PUT')window.__pref=JSON.parse(options.body).value;return json({value:window.__pref});}
 if(String(url).includes('/api/memory/code/resource')){const request=JSON.parse(options.body);window.__sourceCalls.push(request);if(request.operation==='capabilities')return json({schema:'open-clank.code-resource/v1',workspace_id:request.workspace_id,capabilities:[{kind:'code',available:!window.__capUnavailable,reason:window.__capUnavailable?'No current code source producer':null}]});if(window.__holdSource)await new Promise(resolve=>window.__sourceRelease=resolve);const current=window.__sourceState==='current';return json({schema:'open-clank.code-resource/v1',workspace_id:request.workspace_id,state:window.__sourceState,validation_kind:current?'current_source':'none',current_source_verified:current,reference:request.reference,receipt:current?request.reference.receipt:null,source:current?{file_id:'0'.repeat(64),rel_path:'src/target.py',source_hash:'a'.repeat(64),span:{start_byte:0,end_byte:new TextEncoder().encode('def target():\\n return 1').length,start_line:1,end_line:2}}:null,excerpt:current?'def target():\\n return 1':null,destination:current?{provider:'host',resource:{id:'resource-a',ref:'opaque-source-ref',provider:'host',kind:'file'},parent:null,semantics:'current resource, not retained indexed revision'}:null,capabilities:[],error:null,cost:{}});}
 if(String(url).includes('/api/files-v1/open-resource')){window.__filesCalls.push(JSON.parse(options.body));if(window.__holdFiles)await new Promise(resolve=>window.__filesRelease=resolve);return json({resource:{ref:'opaque-source-ref'},payload:{name:'target.py',text:'Fresh Editor bytes',resource:{key:{accountId:'alice',workspaceId:'code-ws',provider:'host',resourceId:'resource-a'},revision:{kind:'hostFingerprint',value:'fresh'},representation:'text',locator:{displayName:'target.py',locationLabel:'Code folder'},capabilities:{read:true}}}});}
 if(String(url).includes('/api/copal'))return json({facets:{},generation:'g',has_more:false});return originalFetch(url,options);
};
</script><script type="module">import '/static/js/copal.js';
if(${!process.argv.includes('--baseline')}){const {default:chat}=await import('/static/js/chat.js');const {default:sessions}=await import('/static/js/sessions.js');const {default:files}=await import('/static/js/fileHandler.js');sessions.getCurrentSessionId=()=>window.__sid;sessions.getSessions=()=>window.__sessions;files.getPendingRaw=()=>window.__pendingFiles;files.clearPending=()=>{throw Error('Must preserve pending attachments');};window.__s07Chat.attachments(['restored-attachment']);}
window.__ready=true;</script>`;
const receipt={workspace_id:'code-ws',registration_id:'r',checkout_id:'c',source_snapshot_id:'s',head_generation:'g',admission_epoch:1,policy_generation:1,config_fingerprint:'f'};
const pageRows=process.argv.includes('--baseline')?50:32;
const envelope=(total,page=0)=>({schema:'open-clank.code-query/v1',domain:'code',operation:'catalog',receipt,
 hits:Array.from({length:Math.min(pageRows,Math.min(total,1000)-page*pageRows)},(_,offset)=>{const i=page*pageRows+offset;return {id:'file:'+(process.argv.includes('--baseline')?i.toString(16):i.toString(16).padStart(64,'0')),kind:'file',label:`src/folder${i%10}/file${i}.py`,evidence_state:'indexed',current_source_verified:false};}),
 edges:[],paths:[],counts:[{name:'eligible',value:total,unit:'file',exactness:'exact'}],facets:{},capabilities:[],
 operation_capabilities:['overview','catalog','facets','outline','inspect','neighbors','path','impact','search','read'].map(operation=>({operation,available:operation!=='read',reason:operation==='read'?'authorized_files_owner_required':null})),channels:[],omissions:[],partial:total>1000,degraded:false,reasons:[],next_cursor:(page+1)*pageRows<total?String(page+1):null,error:null,cost:{}});
const typed=(prefix,n)=>`${prefix}:${n.toString(16).padStart(64,'0')}`;
const edge=(index,from=0,to=1)=>({from:typed('file',from),to:typed('file',to),occurrence_id:typed('occ',index),resolution_state:'exact',witnesses:[{kind:'lexical_binding'}]});
function streamPages({nodes,edges=0,paths=0,topology='star'}){
 const pages=Array.from({length:Math.ceil(nodes/pageRows)},(_,i)=>envelope(nodes,i));
 for(let offset=0;offset<edges;offset+=16)pages.push({...envelope(nodes),hits:[],edges:Array.from({length:Math.min(16,edges-offset)},(_,i)=>{const n=offset+i;return edge(n,topology==='dense'?n%20:0,topology==='dense'?Math.floor(n/20)%20:1+n%(nodes-1));}),paths:[]});
 for(let offset=0;offset<paths;offset+=8)pages.push({...envelope(nodes),hits:[],edges:[],paths:Array.from({length:Math.min(8,paths-offset)},(_,i)=>({node_ids:[typed('file',0),typed('file',1)],edges:[edge(offset+i)]}))});
 pages.forEach((page,index)=>{page.next_cursor=index+1<pages.length?String(index+1):null;assert(page.hits.length+page.edges.length+page.paths.length<=50);assert(Buffer.byteLength(JSON.stringify(page))<=8192);});
 return pages;
}

await withCopalBrowser({page,overrides:{'/static/js/copal.js':source,'/static/js/chat.js':chatSource}},async({evaluate,until,cdp})=>{
 await until('window.__ready && window.__s07');
 if(process.argv.includes('--baseline')){
  const measurements=[];
  for(const total of [100,1000,10000]){
   const rows=[];
   for(let repeat=0;repeat<22;repeat++){
    await evaluate(`window.__s07.setup();window.__pages=${JSON.stringify(Array.from({length:Math.ceil(Math.min(total,1000)/50)},(_,i)=>envelope(total,i)))};`);
    const result=await evaluate(`(async()=>{const set=(label,value)=>{const e=document.querySelector('[aria-label="'+label+'"]');e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));};set('Code workspace ID','code-ws');set('Code operation','catalog');const now=performance.now();[...document.querySelectorAll('button')].find(b=>b.textContent==='Query').click();while(window.__s07.snapshot().status!=='ready')await new Promise(r=>setTimeout(r,0));await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const useful=performance.now()-now;while(window.__s07.snapshot().nodes.length<${Math.min(total,1000)}){[...document.querySelectorAll('button')].find(b=>b.textContent==='Load more').click();while(window.__s07.snapshot().status!=='ready')await new Promise(r=>setTimeout(r,0));}await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));const full=performance.now()-now,mounted=document.querySelectorAll('[data-code-id]').length,loaded=window.__s07.snapshot().nodes.length;const a=performance.now();document.querySelector('[data-code-id]').click();await new Promise(r=>requestAnimationFrame(r));const selection=performance.now()-a;const b=performance.now();set('Code path prefix','src/folder1/');await new Promise(r=>requestAnimationFrame(r));return {useful,full,selection,filter:performance.now()-b,mounted,loaded,memory:performance.memory?.usedJSHeapSize??null};})()`);
    assert.equal(result.loaded,Math.min(total,1000));assert.equal(result.mounted,Math.min(total,200));
    if(repeat>=2)rows.push(result);
   }
   const metric=key=>{const values=rows.map(r=>r[key]).sort((a,b)=>a-b);return {p50:values[9],p95:values[18]};};
   measurements.push({backendTotal:total,firstPageLoaded:50,loaded:Math.min(total,1000),mountedBeforeFilter:Math.min(total,200),warmups:2,repeats:20,firstUsefulMs:metric('useful'),fullPaginationMs:metric('full'),selectionMs:metric('selection'),filterInvalidationMs:metric('filter'),panMs:null,heapBytes:metric('memory')});
  }
  console.log(JSON.stringify({baseline:'accepted S06 unchanged renderer',environment:{cpu:os.cpus()[0]?.model,platform:os.platform(),arch:os.arch(),browser:await evaluate('navigator.userAgent')},limits:'Static immediate fake C04; up to1000 loaded/200mounted; first useful is request to2rAF; filter measures invalidation not backend query; pan unsupported baseline; performance.memory approximate renderer heap not process peak',measurements},null,2));
  return;
 }
 if(process.argv.includes('--s02')){await runColdIndexJourney({evaluate,until});return;}
 if(process.argv.includes('--measure')){await measureWorkbench({evaluate,until,cdp});return;}
 await runWorkbenchJourney({evaluate,until,cdp});
});

async function runColdIndexJourney({evaluate,until}){
 const click=text=>evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify(text)}).click()`);
 // Four real capped-backoff polls intentionally take longer than the generic
 // fixture's five-second DOM wait. This is only a test observation bound.
 const waitIndex=async expression=>{const end=Date.now()+20000;while(Date.now()<end){if(await evaluate(expression))return;await new Promise(resolve=>setTimeout(resolve,25));}throw Error('Synthetic index observation expired');};
 await evaluate(`window.__s07.setup();window.__pages=${JSON.stringify([envelope(32)])};window.__calls=[];window.__indexCalls=[];window.__holdIndex=true;window.__s07.controller().selectWorkspace('code-ws',{label:'Open Clank fixture'});`);
 await until('Boolean(window.__indexRelease)');
 assert.equal(await evaluate('window.__calls.length'),0,'No query before lifecycle ACK');
 assert.equal(await evaluate(`document.querySelector('[data-code-workspace]').textContent`),'Open Clank fixture');
 assert.equal(await evaluate(`document.querySelector('[data-code-operation="overview"]').disabled`),true);
 await evaluate(`window.__holdIndex=false;window.__indexStatus={lifecycle:'building',progress:{completed_files:1,total_files:3}};window.__indexRelease();`);
 await until(`document.querySelector('[data-code-index-status]').textContent.includes('1/3 files')`);
 assert.equal(await evaluate('window.__calls.length'),0,'No query while building');
 await evaluate(`window.__indexTimeouts=4`);
 await until(`document.querySelector('[data-code-index-status]').dataset.recovering==='true'`);
 assert.match(await evaluate(`document.querySelector('[data-code-index-status]').textContent`),/temporarily unavailable; rechecking observation/);
 assert.equal(await evaluate(`document.querySelector('[data-code-operation="overview"]').disabled`),true);
 assert.equal(await evaluate('window.__calls.length'),0,'Temporary status failure cannot expose a head');
 await waitIndex(`window.__indexTimeouts===0&&document.querySelector('[data-code-index-status]').dataset.recovering==='true'`);
 assert.equal(await evaluate(`document.querySelector('[data-code-operation="overview"]').disabled`),true,'More than three timeouts remain nonusable observation, not a failed native job');
 await evaluate(`window.__indexStatus={lifecycle:'ready',head_generation:'g',freshness:'partial',coverage_complete:true};`);
 await waitIndex(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`document.querySelector('[data-code-index-status]').dataset.recovering`),'false');
 const recovered=await evaluate(`window.__indexCalls.filter(c=>!c.url.endsWith('/release'))`);
 assert.equal(recovered.filter(c=>c.url.endsWith('/open')).length,1);
 assert(recovered.filter(c=>c.url.endsWith('/status')).length>=5);
 assert.equal(new Set(recovered.map(c=>c.body.subscriber_id)).size,1,'Recovery observes exactly the original subscriber');
 assert(!recovered.some(c=>c.url.endsWith('/admit')));
 assert.match(await evaluate(`document.querySelector('[data-code-index-status]').textContent`),/partial coverage/);
 assert.equal(await evaluate(`window.__indexCalls.filter(c=>c.url.endsWith('/open')).length`),1);
 assert.equal(await evaluate(`window.__pref`),false,'Indexing cannot enable automatic context');
 assert.equal(await evaluate(`window.__s07.controller().run('overview') instanceof Promise`),true);
 await until(`window.__s07.snapshot()?.status==='ready'`);
 await click('Stop observing index');
 await until(`window.__indexCalls.some(c=>c.url.endsWith('/release'))`);
 assert.equal(await evaluate(`window.__s07.controller().run('overview')`),false,'Detached view cannot query');
 assert.match(await evaluate(`document.querySelector('[data-code-index-status]').textContent`),/other consumers/);
 // Tombstones are terminal until the user deliberately readmits.
 await evaluate(`window.__indexStatus={lifecycle:'erased',head_generation:null};window.__indexCalls=[];window.__calls=[];window.__s07.select('code-ws');`);
 await until(`document.querySelector('[data-code-index-status]').textContent.includes('was erased')`);
 assert.equal(await evaluate('window.__calls.length'),0);
 assert.equal(await evaluate(`window.__indexCalls.some(c=>c.url.endsWith('/admit'))`),false);
 await evaluate(`window.__indexStatus={lifecycle:'ready',head_generation:'g',freshness:'current'};`);
 await click('Readmit erased code index');
 await until(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`window.__indexCalls.filter(c=>c.url.endsWith('/admit')).length`),1);
 // A late open ACK after disposal gets another exact subscriber release.
 await evaluate(`window.__indexCalls=[];window.__holdIndex=true;window.__s07.select('code-ws');`);
 await until('Boolean(window.__indexRelease)');
 await evaluate(`window.__s07.controller().dispose();window.__holdIndex=false;window.__indexRelease();`);
 await until(`window.__indexCalls.filter(c=>c.url.endsWith('/release')).length>=2`);
 const events=await evaluate(`window.__indexCalls.map(c=>({event:c.url.split('/').at(-1),id:c.body.subscriber_id}))`);
 const open=events.find(c=>c.event==='open');
 assert.equal(events.filter(c=>c.event==='release'&&c.id===open.id).length,2,'Late ACK releases the exact opened subscriber twice');
 console.log('S02 synthetic mounted lifecycle: gated cold build, partial ready, detach, explicit readmission and late ACK passed');
}

async function runWorkbenchJourney({evaluate,until,cdp}){
 let groups=0;
 const set=async()=>{await evaluate(`window.__s07.setup();window.__pages=${JSON.stringify([envelope(50)])};window.__s07Controller=undefined;`);};
 // The real production controller is exposed only by the ephemeral source
 // overlay, never by a product global or a second implementation.
 await set();
 await evaluate(`window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`document.querySelectorAll('[data-code-id]').length`),pageRows);groups++;
 const click=text=>evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify(text)}).click()`);
 const select=()=>evaluate(`document.querySelector('[data-code-id]').click()`);
 for(const [label,operation,direction]of [['Files','catalog'],['Overview','overview'],['Folders & filters','facets'],['Search','search'],['Symbols','search'],['Declaration outline','outline'],['Incoming','neighbors','incoming'],['Outgoing','neighbors','outgoing'],['Impact','impact','incoming'],['Focus selected','neighbors','both']]){
  await select();await click(label);await until(`window.__s07.snapshot()?.status==='ready'`);
  const call=await evaluate(`window.__calls.at(-1)`);assert.equal(call.query.operation,operation);if(direction)assert.equal(call.query.direction,direction);
  assert.deepEqual(call.query.channels,operation==='search'?['exact','identifier','fuzzy']:undefined);
  if(label==='Symbols')assert.deepEqual(call.query.filter.kinds,['declaration']);groups++;
 }
 // Search, Enter and Symbols all request fuzzy suggestions explicitly. More
 // retains that selection and receipt without changing native ranking order.
 const searchPages=[0,1].map((page)=>({...envelope(2),operation:'search',hits:[{
  id:typed('decl',page===0?2:1),kind:'declaration',label:page===0?'resolve_resource':'resolve_resources',
  evidence_state:page===0?'metadata':'suggestion',current_source_verified:false,
  matches:[{channel:page===0?'exact':'fuzzy'}]}],next_cursor:page===0?'1':null}));
 await set();await evaluate(`window.__pages=${JSON.stringify(searchPages)};window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
 await evaluate(`document.querySelector('[aria-label="Search scoped code"]').value='resolve_resource'`);
 await click('Symbols');await until(`window.__s07.snapshot()?.status==='ready'`);
 const searchCall=await evaluate(`window.__calls.at(-1)`);
 assert.deepEqual(searchCall.query.channels,['exact','identifier','fuzzy']);
 assert.deepEqual(searchCall.query.filter.kinds,['declaration']);
 assert.deepEqual(searchCall.query.budgets,{rows:50,bytes:65536,tokens:8192,hops:4,work:4000000});groups++;
 await click('Load more');await until(`window.__s07.snapshot()?.nodes.length===2`);
 const continuation=await evaluate(`window.__calls.at(-1)`);
 assert.deepEqual(continuation.query.channels,searchCall.query.channels);
 assert.deepEqual(continuation.query.filter,searchCall.query.filter);
 assert.deepEqual(continuation.query.budgets,searchCall.query.budgets);
 assert.equal(continuation.query.query,'resolve_resource');assert.equal(continuation.query.cursor,'1');
 assert.deepEqual(continuation.query.expected_receipt,receipt);
 assert.deepEqual(await evaluate(`[...document.querySelectorAll('[data-code-id]')].map(node=>node.dataset.codeId)`),[typed('decl',2),typed('decl',1)]);
 assert.match(await evaluate(`document.querySelector('[data-code-list]').textContent`),/resolve_resources.*suggestion/);
 await evaluate(`document.querySelector('[data-code-id="${typed('decl',1)}"]').click()`);
 assert.match(await evaluate(`document.querySelector('[data-code-fact]').textContent`),/suggestion/);groups++;
 await evaluate(`document.querySelector('[aria-label="Search scoped code"]').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}))`);
 await until(`window.__s07.snapshot()?.status==='ready'`);
 const entered=await evaluate(`window.__calls.at(-1)`);
 assert.equal(entered.query.operation,'search');assert.deepEqual(entered.query.channels,['exact','identifier','fuzzy']);
 assert.equal(entered.query.cursor,undefined);assert.deepEqual(entered.query.filter.kinds,[]);groups++;
 // Native Facets pages contain complete bucket units and intentionally no hits.
 const buckets=Array.from({length:34},(_,page)=>({...envelope(1088,page),hits:[],edges:[],paths:[],
  counts:[{name:'eligible',value:8586,unit:'file',exactness:'exact'}],
  facets:{kinds:[],languages:[],states:[],folders:Array.from({length:pageRows},(_,offset)=>({value:`folder-${page*pageRows+offset}`,count:3,unit:'file',exactness:'exact'}))},
  next_cursor:page<33?String(page+1):null}));
 await set();await evaluate(`window.__pages=${JSON.stringify(buckets)};window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
 await click('Folders & filters');await until(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`window.__s07.snapshot().nodes.length`),0);
 assert.match(await evaluate(`document.querySelector('[data-code-status]').textContent`),/32 filter options loaded.*more filter options available/);
 assert(!await evaluate(`document.querySelector('[data-code-status]').textContent.includes('no indexed code resources')`));groups++;
 await click('Load more');await until(`window.__s07.snapshot()?.facets.folders.length===64`);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Code folders"]').options.length`),65);
 assert.equal(await evaluate(`window.__s07.snapshot().counts[0].value`),8586);
 assert.equal(await evaluate(`window.__s07.snapshot().facets.folders[0].count`),3);groups++;
 for(let page=2;page<32;page++){await click('Load more');await until(`window.__s07.snapshot()?.status==='ready'`);}
 assert.equal(await evaluate(`window.__s07.snapshot().facets.folders.length`),1024);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Code folders"]').options.length`),1025);
 assert.equal(await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Load more').disabled`),true);
 assert.match(await evaluate(`document.querySelector('[data-code-status]').textContent`),/option limit reached; more remain/);
 assert.match(await evaluate(`document.querySelector('[data-code-counts]').textContent`),/More options remain beyond this presentation limit/);groups++;
 const outside={...envelope(2),facets:{languages:[{value:'python',count:2,unit:'file',exactness:'exact'}]},next_cursor:null};
 await evaluate(`window.__pages=${JSON.stringify([outside])};{const f=document.querySelector('[aria-label="Code folders"]');f.value='folder-1';f.dispatchEvent(new Event('change'));}`);
 await until(`window.__s07.snapshot()?.status==='ready'&&window.__s07.snapshot()?.operation==='catalog'`);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Code folders"]').value`),'folder-1');
 assert.match(await evaluate(`document.querySelector('[aria-label="Code folders"]').selectedOptions[0].textContent`),/outside result/);
 assert.equal(await evaluate(`window.__calls.at(-1).query.filter.path_prefix`),'folder-1/');groups++;
 await evaluate(`window.__pages[0].receipt.workspace_id='other-code-ws';window.__s07.select('other-code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Code folders"]').value`),'');
 assert.equal(await evaluate(`window.__calls.at(-1).query.filter.path_prefix??null`),null);groups++;
 await evaluate(`window.__pages=${JSON.stringify([{...envelope(2),receipt:null,error:{code:'invalid_query',message:'Synthetic filter refused'}}])};{const f=document.querySelector('[aria-label="Code folders"]');f.append(new Option('problematic filter','problematic'));f.value='problematic';f.dispatchEvent(new Event('change'));}`);
 await until(`window.__s07.snapshot()?.status==='error'`);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Code folders"]').disabled`),false,'An authorized usable index must let the user clear a failing filter');
 await evaluate(`window.__pages=${JSON.stringify([{...envelope(2),receipt:{...envelope(2).receipt,workspace_id:'other-code-ws'}}])};{const f=document.querySelector('[aria-label="Code folders"]');f.value='';f.dispatchEvent(new Event('change'));}`);
 await until(`window.__s07.snapshot()?.status==='ready'`);
 assert.equal(await evaluate(`window.__calls.at(-1).query.filter.path_prefix??null`),null);groups++;
 await set();await evaluate(`window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
 await select();await evaluate(`document.querySelector('[aria-label="Static path target"]').value='file:0000000000000000000000000000000000000000000000000000000000000001'`);await click('Trace to');await until(`window.__s07.snapshot()?.status==='ready'`);assert.equal((await evaluate('window.__calls.at(-1)')).query.target_id,'file:0000000000000000000000000000000000000000000000000000000000000001');groups++;
 await select();assert.match(await evaluate(`document.querySelector('[data-code-fact]').textContent`),/metadata; current bytes not verified/);assert.equal(await evaluate(`document.querySelector('[data-code-id]').getAttribute('aria-selected')`),'true');groups++;
 const selectSizing=await evaluate(`[...document.querySelectorAll('.code-workbench select')].map(element=>{const style=getComputedStyle(element);return {height:element.getBoundingClientRect().height,inner:element.clientHeight-parseFloat(style.paddingTop)-parseFloat(style.paddingBottom),line:parseFloat(style.lineHeight)||parseFloat(style.fontSize)*1.2};})`);
 assert(selectSizing.every(size=>size.height>=44&&size.inner>=size.line),'Select inner height must fit the rendered text line');groups++;
 await evaluate(`document.querySelector('[data-code-id]').focus();document.querySelector('[data-code-list]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));`);
 assert.equal(await evaluate(`document.activeElement.dataset.codeId`),'file:0000000000000000000000000000000000000000000000000000000000000001');assert.match(await evaluate(`document.querySelector('[data-code-detail]').textContent`),/file:0000000000000000000000000000000000000000000000000000000000000001/);groups++;
 const before=await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`);
 await evaluate(`document.querySelector('[data-code-viewport]').dispatchEvent(new WheelEvent('wheel',{deltaY:-100,clientX:400,clientY:300,cancelable:true}));`);
 const zoomed=await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`);assert.notEqual(zoomed,before);
 await select();assert.equal(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),zoomed);
 await click('Files');await until(`window.__s07.snapshot()?.status==='ready'`);assert.equal(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),zoomed);groups++;
 await evaluate(`document.querySelector('[data-code-viewport]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));`);assert.notEqual(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),zoomed);groups++;
 // Destruction aborts listeners even on retained detached DOM references.
 await evaluate(`window.__oldViewport=document.querySelector('[data-code-viewport]');window.__oldController=window.__s07.controller();window.__oldCamera=window.__oldController.camera();window.__oldController.dispose();window.__oldViewport.dispatchEvent(new WheelEvent('wheel',{deltaY:400,clientX:400,clientY:300}));`);
 assert.deepEqual(await evaluate(`window.__oldController.camera()`),await evaluate(`window.__oldCamera`));groups++;
 for(let index=0;index<5;index++){await set();await evaluate(`window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);}groups++;
 // All loaded resources remain reachable through bounded mounted windows.
 await set();await evaluate(`window.__pages=${JSON.stringify(Array.from({length:Math.ceil(300/pageRows)},(_,i)=>envelope(300,i)))};window.__s07.select('code-ws');`);await until(`window.__s07.snapshot()?.status==='ready'`);
 for(let index=0;index<Math.ceil(300/pageRows)-1;index++){await click('Load more');await until(`window.__s07.snapshot()?.status==='ready'`);}
 assert.equal(await evaluate(`window.__s07.snapshot().nodes.length`),300);assert.equal(await evaluate(`document.querySelectorAll('[data-code-id]').length`),200);
 await click('Next resources');assert.equal(await evaluate(`document.querySelectorAll('[data-code-id]').length`),100);
 await evaluate(`document.querySelector('[data-code-id="file:00000000000000000000000000000000000000000000000000000000000000fa"]').click()`);assert.match(await evaluate(`document.querySelector('[data-code-detail]').textContent`),/file:00000000000000000000000000000000000000000000000000000000000000fa/);
 assert.match(await evaluate(`document.querySelector('[data-code-window]').textContent`),/201–300 of 300/);groups++;
 for(const kind of ['edges','paths']){
  const pages=streamPages({nodes:2,edges:kind==='edges'?1008:0,paths:kind==='paths'?104:0});
  await set();await evaluate(`window.__pages=${JSON.stringify(pages)};window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);
  for(let index=1;index<pages.length;index++){await click('Load more');await until(`window.__s07.snapshot()?.status==='ready'`);}
  assert.equal((await evaluate(`window.__s07.snapshot().${kind}`)).length,kind==='edges'?1008:104);
  assert.equal(await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Load more').disabled`),true);
  const calls=(await evaluate('window.__calls')).length;await click('Load more');assert.equal((await evaluate('window.__calls')).length,calls);
  if(kind==='edges'){assert.equal(await evaluate(`document.querySelectorAll('.code-edge').length`),400);await select();assert.equal(await evaluate(`document.querySelectorAll('[data-code-witnesses] details').length`),50);assert.match(await evaluate(`document.querySelector('[data-code-counts]').textContent`),/Edges loaded: 1008, mounted: 400, omitted from viewport: 608/);}
  else{assert(await evaluate(`document.querySelectorAll('.code-path-breadcrumbs button').length<=80`));assert.match(await evaluate(`document.querySelector('[data-code-detail]').textContent`),/"omitted_paths": 94/);}
  groups++;
 }
 const fresh=async()=>{
  await evaluate(`window.__holdSource=false;window.__holdFiles=false;window.__holdEditor=false;window.__sourceState='current';window.__installed=null;window.__sourceCalls=[];window.__filesCalls=[];window.__sid='chat-a';window.__sessions=[{id:'chat-a',owner:'alice'}];window.__pendingFiles=[{name:'pending.png'}];document.querySelector('#message').value='Existing draft';document.querySelector('#message').dispatchEvent(new Event('input',{bubbles:true}));window.__s07Chat.attachments(['restored-attachment']);`);
  await set();await evaluate(`window.__s07.select('code-ws')`);await until(`window.__s07.snapshot()?.status==='ready'`);await select();
 };
 const frames=()=>evaluate(`new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))`);
 await fresh();const initial=await evaluate(`window.chatModule.captureCodeEvidenceDraft()`);
 await click('Add to chat');await until(`document.querySelector('#message').value.includes('[Untrusted code evidence')`);
 assert.equal(await evaluate(`window.__asks`),0);assert.match(await evaluate(`document.querySelector('#message').value`),/^Existing draft\n\n/);
 assert.match(await evaluate(`document.querySelector('#message').value`),/Workspace: code-ws/);
 assert.deepEqual(await evaluate(`window.chatModule.captureCodeEvidenceDraft().attachments`),initial.attachments);
 assert.deepEqual(await evaluate(`window.chatModule.captureCodeEvidenceDraft().pending`),initial.pending);
 assert.equal(await evaluate(`window.__filesCalls.length`),0);groups++;
 await fresh();await click('Ask about this');await until(`window.__asks===1`);groups++;
 for(const state of ['stale','missing','erased','ambiguous','unavailable','denied','cancelled','invalid']){
  await fresh();await evaluate(`window.__sourceState=${JSON.stringify(state)}`);await click('Add to chat');await until(`document.querySelector('[data-code-status]').textContent.includes('Source ${state}')`);
  assert.equal(await evaluate(`document.querySelector('#message').value`),'Existing draft');assert.equal(await evaluate(`window.__filesCalls.length`),0);groups++;
 }
 for(const change of ['draft','session','owner','account','workspace','selection','generation','mode']){
  await fresh();await evaluate(`window.__holdSource=true`);await click('Add to chat');await until(`!!window.__sourceRelease`);
  await evaluate(({draft:`document.querySelector('#message').value='New typing';document.querySelector('#message').dispatchEvent(new Event('input',{bubbles:true}));`,session:`window.__sid='chat-b';window.__sessions.push({id:'chat-b',owner:'alice'});document.dispatchEvent(new Event('odysseus:session-selected'));`,owner:`window.__sessions[0].owner='bob'`,account:`document.dispatchEvent(new Event('openclank:auth-context-changed'))`,workspace:`window.dispatchEvent(new Event('workspace-change'))`,selection:`document.querySelectorAll('[data-code-id]')[1].click()`,generation:`window.__pages[0].receipt.head_generation='next';[...document.querySelectorAll('button')].find(b=>b.textContent==='Files').click()`,mode:`document.querySelector('[data-graph-mode]').value='documents';document.querySelector('[data-graph-mode]').dispatchEvent(new Event('change'))`})[change]);
  await evaluate(`window.__sourceRelease();window.__sourceRelease=null`);await frames();
  assert.equal(await evaluate(`document.querySelector('#message').value`),change==='draft'?'New typing':'Existing draft',change);assert.equal(await evaluate(`window.__asks`),1);groups++;
 }
 await fresh();await evaluate(`window.__s07.editor()`);await click('Open source');await until(`!!window.__installed`);
 assert.deepEqual(await evaluate(`window.__filesCalls`),[{resource_ref:'opaque-source-ref'}]);
 assert.equal(await evaluate(`window.__installed.payload.text`),'Fresh Editor bytes');assert.equal(await evaluate(`window.__installed.payload.resourceRef`),'opaque-source-ref');groups++;
 for(const phase of ['Files','Editor'])for(const change of ['account','workspace','reselection','newer-editor']){
  await fresh();await evaluate(`window.__s07.editor();window.__hold${phase}=true;window.__filesRelease=null;window.__editorRelease=null`);await click('Open source');await until(`!!window.__${phase.toLowerCase()}Release`);
  await evaluate(({account:`document.dispatchEvent(new Event('openclank:auth-context-changed'))`,workspace:`window.dispatchEvent(new Event('workspace-change'))`,reselection:phase==='Files'?`document.querySelectorAll('[data-code-id]')[1].click()`:`window.__s07.setup()`, 'newer-editor':`window.__s07.navigate()`})[change]);
  await evaluate(`window.__${phase.toLowerCase()}Release()`);await frames();assert.equal(await evaluate(`window.__installed`),null,`${phase}/${change}`);groups++;
 }
 await fresh();await until(`!document.querySelector('[aria-label="Automatic code context"]').disabled`);
 assert.equal(await evaluate(`document.querySelector('[aria-label="Automatic code context"]').checked`),false);
 await evaluate(`document.querySelector('[aria-label="Automatic code context"]').click()`);await until(`window.__pref===true && !document.querySelector('[aria-label="Automatic code context"]').disabled`);groups++;
 await evaluate(`window.__pref='true'`);await set();await frames();assert.equal(await evaluate(`document.querySelector('[aria-label="Automatic code context"]').checked`),false);groups++;
 await evaluate(`window.__holdPrefs=true;window.__prefsRelease=null`);await set();await until(`!!window.__prefsRelease`);
 await evaluate(`document.dispatchEvent(new Event('openclank:auth-context-changed'));window.__pref=true;window.__prefsRelease();window.__holdPrefs=false`);await frames();
 assert.equal(await evaluate(`document.querySelector('[aria-label="Automatic code context"]')===null`),true);groups++;
 await evaluate(`window.__pref=false;window.__capUnavailable=true`);await fresh();await until(`document.querySelector('[data-code-status]').textContent.includes('ready')`);
 assert.equal(await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Add to chat').disabled`),true);groups++;
 await evaluate(`window.__capUnavailable=false`);await fresh();await evaluate(`window.__pages[0].hits[0].id='file:0';void window.__s07.controller().run('catalog')`);await until(`window.__s07.snapshot()?.status==='ready'`);await select();
 assert.equal(await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Open source').disabled`),true);groups++;
 await fresh();await evaluate(`document.querySelector('[data-code-viewport]').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));window.__positionCamera=window.__s07.controller().camera();window.__stablePosition=document.querySelector('[data-code-point]').getAttribute('transform');`);
 for(let index=0;index<40;index++){
  const page=envelope(pageRows);page.next_cursor=null;page.hits=page.hits.map((hit,n)=>({...hit,id:typed('file',n?1000+index*pageRows+n:0)}));
  await evaluate(`window.__pages=[${JSON.stringify(page)}];void window.__s07.controller().run('search',{query:'disjoint-${index}'});`);await until(`window.__s07.snapshot()?.status==='ready'`);
  assert.equal(await evaluate(`window.__s07.controller().presentationStats().positionCount`),pageRows);
  assert.equal(await evaluate(`document.querySelector('[data-code-point]').getAttribute('transform')`),await evaluate(`window.__stablePosition`));
  assert.deepEqual(await evaluate(`window.__s07.controller().camera()`),await evaluate(`window.__positionCamera`));
 }
 groups++;
 const retainedId=typed('file',1000+39*pageRows+1),mixed=envelope(2);mixed.hits[0].id=retainedId;mixed.hits[1].id=typed('file',9999);mixed.next_cursor=null;
 const retainedPosition=await evaluate(`document.querySelector('[data-code-point="${retainedId}"]').getAttribute('transform')`);
 await evaluate(`window.__pages=[${JSON.stringify(mixed)}];void window.__s07.controller().run('search',{query:'mixed-retained-new'});`);await until(`window.__s07.snapshot()?.status==='ready'`);
 const mixedPositions=await evaluate(`[...document.querySelectorAll('[data-code-point]')].map(node=>node.getAttribute('transform'))`);
 assert.equal(new Set(mixedPositions).size,2,'Retained slot1 and newly allocated slot must not coincide');assert.equal(mixedPositions[0],retainedPosition);groups++;
 // Actual rendered coordinate matrix: mismatched viewport/viewBox aspect.
 await fresh();await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await frames();
 await evaluate(`document.querySelector('[data-code-viewport]').scrollIntoView({block:'center'});`);await frames();
 await evaluate(`(async()=>{let previous='',stable=0;for(let i=0;i<180;i++){await new Promise(r=>requestAnimationFrame(r));const b=document.querySelector('[data-code-viewport] svg').getBoundingClientRect(),value=JSON.stringify([b.x,b.y,b.width,b.height]);stable=value===previous?stable+1:0;previous=value;if(stable>=10)return;}throw Error('Rendered SVG bounds did not settle');})()`);
 const point=await evaluate(`(()=>{const s=document.querySelector('[data-code-viewport] svg'),b=s.getBoundingClientRect();return {x:Math.floor(b.left+b.width*.72),y:Math.floor(b.top+b.height*.37)};})()`);
 await evaluate(`document.querySelector('[data-code-viewport]').addEventListener('wheel',event=>{const s=document.querySelector('[data-code-viewport] svg'),m=s.getScreenCTM();window.__wheelSeen={x:event.clientX,y:event.clientY,ctm:[m.a,m.d,m.e,m.f]};},{capture:true,once:true});`);
 const worldAt=()=>evaluate(`(()=>{const s=document.querySelector('[data-code-viewport] svg'),m=s.getScreenCTM(),p=new DOMPoint(${point.x},${point.y}).matrixTransform(m.inverse()),c=JSON.parse(document.querySelector('[data-code-viewport]').dataset.camera),b=s.getBoundingClientRect();return {x:(p.x-c.x)/c.scale,y:(p.y-c.y)/c.scale,ctm:[m.a,m.d,m.e,m.f],box:[b.x,b.y,b.width,b.height]};})()`);
 const worldBefore=await worldAt();await cdp('Input.dispatchMouseEvent',{type:'mouseWheel',x:point.x,y:point.y,deltaX:0,deltaY:-180});await frames();const worldAfter=await worldAt();
 assert(Math.abs(worldBefore.x-worldAfter.x)<.05&&Math.abs(worldBefore.y-worldAfter.y)<.05,JSON.stringify({point,worldBefore,worldAfter,event:await evaluate('window.__wheelSeen'),camera:await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`)}));groups++;
 const physicalBefore=await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`);
 await cdp('Input.dispatchMouseEvent',{type:'mousePressed',x:point.x,y:point.y,button:'left',clickCount:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseMoved',x:point.x+45,y:point.y+35,button:'left',buttons:1});
 await cdp('Input.dispatchMouseEvent',{type:'mouseReleased',x:point.x+45,y:point.y+35,button:'left',clickCount:1});await frames();
 assert.notEqual(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),physicalBefore);groups++;
 const resizeBefore=await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`);await cdp('Emulation.setDeviceMetricsOverride',{width:1000,height:900,deviceScaleFactor:1,mobile:false});await frames();
 assert.equal(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),resizeBefore);groups++;
 await cdp('Emulation.setEmulatedMedia',{features:[{name:'prefers-reduced-motion',value:'reduce'}]});
 assert.equal(await evaluate(`matchMedia('(prefers-reduced-motion:reduce)').matches`),true);
 assert.equal(await evaluate(`getComputedStyle(document.querySelector('[data-code-viewport]')).animationName`),'none');groups++;
 await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});await frames();
 assert.equal(await evaluate(`getComputedStyle(document.querySelector('.code-workbench-panels')).flexDirection`),'column');
 assert(await evaluate(`document.querySelector('button').getBoundingClientRect().height>=36`));groups++;
 await evaluate(`document.querySelector('[data-code-viewport]').scrollIntoView({block:'center'});`);await cdp('Emulation.setTouchEmulationEnabled',{enabled:true});
 const touch=await evaluate(`(()=>{const r=document.querySelector('[data-code-viewport]').getBoundingClientRect();return {x:r.left+r.width/2,y:r.top+r.height/2};})()`);
 const touchBefore=await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`);
 await cdp('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x:touch.x-25,y:touch.y,id:1},{x:touch.x+25,y:touch.y,id:2}]});
 await cdp('Input.dispatchTouchEvent',{type:'touchMove',touchPoints:[{x:touch.x-50,y:touch.y+10,id:1},{x:touch.x+50,y:touch.y+10,id:2}]});
 await cdp('Input.dispatchTouchEvent',{type:'touchEnd',touchPoints:[]});await frames();
 assert.notEqual(await evaluate(`document.querySelector('[data-code-viewport]').dataset.camera`),touchBefore);groups++;
 await cdp('Emulation.setTouchEmulationEnabled',{enabled:false});
 // Twenty lifecycle cycles, with retained detached nodes proving abort cleanup.
 await cdp('HeapProfiler.collectGarbage');const startHeap=await evaluate(`performance.memory?.usedJSHeapSize??null`);
 for(let cycle=0;cycle<20;cycle++){
  await fresh();assert.equal(await evaluate(`document.querySelectorAll('.code-workbench').length`),1);
  await evaluate(`window.__oldController=window.__s07.controller();window.__oldViewport=document.querySelector('[data-code-viewport]');window.__oldCamera=window.__oldController.camera();document.querySelector('#graph').replaceChildren(document.createElement('p'));`);await frames();
  await evaluate(`window.__oldViewport.dispatchEvent(new WheelEvent('wheel',{deltaY:100,clientX:50,clientY:100}));`);assert.deepEqual(await evaluate(`window.__oldController.camera()`),await evaluate(`window.__oldCamera`));
 }
 await evaluate(`window.__oldController=null;window.__oldViewport=null;window.__oldCamera=null`);await cdp('HeapProfiler.collectGarbage');const endHeap=await evaluate(`performance.memory?.usedJSHeapSize??null`);
 assert(endHeap-startHeap<=8*1024*1024);groups++;
 await fresh();await evaluate(`window.__pages[0].hits[0].source={file_id:'0'.repeat(64),rel_path:'src/folder0/file0.py',source_hash:'a'.repeat(64),span:{start_byte:0,end_byte:22,start_line:1,end_line:2}};window.__pages[0].hits[1]={...window.__pages[0].hits[1],id:'decl:'+'1'.padStart(64,'0'),kind:'declaration',label:'target',evidence_state:'exact',source:window.__pages[0].hits[0].source,signature:'def target()'};window.__pages[0].hits[2]={...window.__pages[0].hits[2],id:'occ:'+'2'.padStart(64,'0'),kind:'occurrence',label:'target() callsite',evidence_state:'exact',source:window.__pages[0].hits[0].source};window.__pages[0].edges=[{from:window.__pages[0].hits[0].id,to:window.__pages[0].hits[1].id,occurrence_id:'occ:'+'a'.repeat(64),resolution_state:'exact',witnesses:[{kind:'lexical_binding'}]}];void window.__s07.controller().run('neighbors',{seed_ids:[window.__pages[0].hits[0].id],direction:'outgoing'});`);await until(`window.__s07.snapshot()?.status==='ready'`);await select();
 assert.equal(await evaluate(`document.querySelectorAll('.code-edge[marker-end]').length`),1);assert.equal(await evaluate(`document.querySelectorAll('.code-point path').length`),1);assert.equal(await evaluate(`document.querySelectorAll('.code-point circle[style*="fill: none"]').length`),1);groups++;
 if(process.argv.includes('--screenshots')){
  const directory='.clanker/artifacts/code-memory-graph-2026-10-13/s07-workbench';fs.mkdirSync(directory,{recursive:true});
  for(const [name,width,height,mobile]of [['desktop',1440,1000,false],['mobile',390,844,true]]){
   const file=`${directory}/${name}.png`;if(fs.existsSync(file)){const prefix=process.argv.find(arg=>arg.startsWith('--image-preimage-prefix='))?.split('=')[1];assert(prefix,'Hex+Git-pin any existing image before regenerating');const saved=execFileSync('git',['cat-file','blob',prefix+'-'+name]);assert.deepEqual(fs.readFileSync(file),saved,'Image must still match the exact pinned preimage');}
   await cdp('Emulation.setDeviceMetricsOverride',{width,height,deviceScaleFactor:1,mobile});await evaluate(`scrollTo(0,0)`);await frames();
   // Composition capture includes the whole internally scrollable applet.
   // Mobile interaction proof above remains at the real390x844 viewport.
   const contentHeight=await evaluate(`Math.max(document.body.scrollHeight,document.documentElement.scrollHeight,document.querySelector('#graph').scrollHeight,document.querySelector('.code-workbench').scrollHeight)`);
   if(contentHeight>height){await cdp('Emulation.setDeviceMetricsOverride',{width,height:Math.min(5000,contentHeight+50),deviceScaleFactor:1,mobile});await frames();}
   const metrics=await cdp('Page.getLayoutMetrics');const size=metrics.cssContentSize;
   const shot=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:{x:0,y:0,width:size.width,height:size.height,scale:1}});
   fs.writeFileSync(file,Buffer.from(shot.data,'base64'));
  }
 }
 console.log(`S07 actual workbench: ${groups} browser groups passed;20cycles GCsettledheap ${startHeap}→${endHeap}; performance measured separately`);
}

async function measureWorkbench({evaluate,until,cdp}){
 await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 const measurements=[];
 for(const workload of [{total:100},{total:1000},{total:10000},{total:200,edges:400,topology:'dense'},{total:200,edges:199,topology:'star'}]){
  const total=workload.total;
  const pages=workload.edges?streamPages({nodes:total,edges:workload.edges,topology:workload.topology}):Array.from({length:Math.ceil(Math.min(total,1000)/pageRows)},(_,i)=>envelope(total,i));
  for(const page of pages){assert(page.hits.length<=50);assert(Buffer.byteLength(JSON.stringify(page))<=8192);}
  const rows=[];
  for(let repeat=0;repeat<22;repeat++){
   await evaluate(`window.__s07.setup();window.__pages=${JSON.stringify(pages)};`);
   const row=await evaluate(`(async()=>{const frame=()=>new Promise(r=>requestAnimationFrame(r)),ready=async(paint=true)=>{while(window.__s07.snapshot().status!=='ready')await new Promise(r=>setTimeout(r,0));if(paint){await frame();await frame();}},click=text=>[...document.querySelectorAll('button')].find(b=>b.textContent===text).click();const t=performance.now();window.__s07.select('code-ws');await ready();const useful=performance.now()-t;while(![...document.querySelectorAll('button')].find(b=>b.textContent==='Load more').disabled){click('Load more');await ready(false);}await frame();await frame();const full=performance.now()-t,loaded=window.__s07.snapshot().nodes.length,mounted=document.querySelectorAll('[data-code-id]').length,loadedEdges=window.__s07.snapshot().edges.length,mountedEdges=document.querySelectorAll('.code-edge').length;let now=performance.now();document.querySelector('[data-code-id]').click();await frame();const selection=performance.now()-now;const view=document.querySelector('[data-code-viewport]');now=performance.now();view.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));await frame();const pan=performance.now()-now;const box=view.getBoundingClientRect();now=performance.now();view.dispatchEvent(new WheelEvent('wheel',{deltaY:-40,clientX:box.left+box.width*.73,clientY:box.top+box.height*.42,cancelable:true}));await frame();const zoom=performance.now()-now;const folder=document.querySelector('[aria-label="Code folders"]');folder.append(new Option('folder1','src/folder1'));folder.value='src/folder1';now=performance.now();folder.dispatchEvent(new Event('change'));await ready();const filter=performance.now()-now;document.querySelector('[data-code-id]').click();now=performance.now();click('Incoming');await ready();const expansion=performance.now()-now;const intervals=[];let previous=performance.now();for(let i=0;i<20;i++){view.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));await frame();const at=performance.now();intervals.push(at-previous);previous=at;}return {useful,full,selection,pan,zoom,filter,expansion,loaded,mounted,loadedEdges,mountedEdges,intervals,heap:performance.memory?.usedJSHeapSize??null};})()`);
   assert.equal(row.loaded,Math.min(total,1000));assert.equal(row.mounted,Math.min(total,200));assert.equal(row.loadedEdges,workload.edges||0);assert.equal(row.mountedEdges,Math.min(workload.edges||0,400));if(repeat>=2)rows.push(row);
  }
  const percentile=values=>{values.sort((a,b)=>a-b);return {p50:values[Math.ceil(values.length*.5)-1],p95:values[Math.ceil(values.length*.95)-1]};};
  const metric=key=>percentile(rows.map(row=>row[key]));
  const result={topology:workload.topology||'file-catalog',backendTotal:total,loadedEdges:workload.edges||0,mountedEdges:Math.min(workload.edges||0,400),firstPageLoaded:pageRows,loaded:Math.min(total,1000),mounted:Math.min(total,200),warmups:2,repeats:20,firstUsefulMs:metric('useful'),fullPaginationMs:metric('full'),selectionMs:metric('selection'),panMs:metric('pan'),zoomMs:metric('zoom'),filterRefreshMs:metric('filter'),expansionMs:metric('expansion'),rafIntervalsMs:percentile(rows.flatMap(row=>row.intervals)),heapBytes:metric('heap')};
  assert(result.firstUsefulMs.p95<=200);assert(result.fullPaginationMs.p95<=1000);assert(result.filterRefreshMs.p95<=200);assert(result.expansionMs.p95<=200);
  for(const key of ['selectionMs','panMs','zoomMs','rafIntervalsMs'])assert(result[key].p95<=50,`${key} budget`);assert(result.heapBytes.p95<=64*1024*1024);measurements.push(result);
 }
 console.log(JSON.stringify({workbench:'S07 actualproductionmodule',environment:{cpu:os.cpus()[0]?.model,platform:os.platform(),arch:os.arch(),browser:await evaluate('navigator.userAgent')},limits:'Controlled immediate C04;32 native-shaped64hex IDs per page within8192byte envelope; loadedcap1000/mounted200; firstuseful/resultrefresh2rAF,selection+keyboardpan+wheelzoom1rAF; rAF intervals20inputs per sample; approximate GCdependentJSheap notprocesspeak; no backendlatency/Windows/optimizationclaim',measurements},null,2));
}
