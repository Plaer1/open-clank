import assert from 'node:assert/strict';
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

const backend=new URL(process.env.S11_BACKEND_URL);
assert.equal(backend.hostname,'127.0.0.1');
assert(process.env.S11_TOKEN);
assert(['erase','revoke'].includes(process.env.S11_DENIAL));
const headers={'x-s11-token':process.env.S11_TOKEN,'content-type':'application/json'};
const info=await (await fetch(new URL('/__fixture/info',backend),{headers})).json();
const responses=[];
const uiTimings=[];
const controls=async action=>{const r=await fetch(new URL('/__fixture/control',backend),{method:'POST',headers,body:JSON.stringify({action})});assert(r.ok);return r.json();};
const production=fs.readFileSync('static/js/copal.js','utf8');
// Read-only fixture access to the real controller/buffer; no renderer, Files,
// source action, open(), or CodeMirror implementation is replaced.
const source=production.replace('export default { init,',`window.__s11={run:(operation,extra)=>codeGraphController.run(operation,extra),select:id=>codeGraphController.selectWorkspace(id),snapshot:()=>codeGraphController?.snapshot(),buffer:()=>notesFeature?.getAuthoritativeSnapshot(state.selected),view:()=>state.view};\nexport default { init,`)
 .replace('  await opening;', '  await opening; window.__s11Handoff={sameScope:sameSaveScope(scope,saveScope()),sameNavigation:navigationTicket===editorNavigationTicket,current:handoffCurrent(),view:state.view};');
assert.notEqual(source,production);
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><main id="app"></main>
<script type="module">
class QuietEvents{addEventListener(){} close(){}}window.EventSource=QuietEvents;window.__sourceAcks=[];window.__sourceAborts=0;
const original=fetch;window.fetch=async(input,init={})=>{
 const u=new URL(String(input),location.origin),json=v=>new Response(JSON.stringify(v),{headers:{'content-type':'application/json'}});
 // Explicit unrelated shell boundary only. Never mock Graph/source/Files.
 if(u.pathname==='/api/copal/status')return json({storage_namespace:'s11-isolated',account_id:${JSON.stringify(info.account_id)}});
 if(u.pathname==='/api/copal/documents')return json({docs:[]});
 if(u.pathname==='/api/copal/planning')return json({tracks:[],floatingTodos:[]});
 if(u.pathname==='/api/prefs/copal_entry_visibility')return json({value:{}});
 if(u.pathname==='/api/prefs/code_context_enabled')return json({value:false});
 if(u.pathname==='/api/auth/status')return json({ok:true,username:'alice',is_admin:false});
 let response;
 try{response=await original(input,init);}catch(error){
  if(u.pathname==='/api/memory/code/resource'&&init.body&&JSON.parse(init.body).operation==='resolve'&&error.name==='AbortError')window.__sourceAborts++;
  throw error;
 }
 if(u.pathname==='/api/memory/code/resource'&&init.body&&JSON.parse(init.body).operation==='resolve')window.__sourceAcks.push(await response.clone().json());
 return response;
};
window.__run=async()=>{const m=await import('/static/js/copal.js');window.copalModule=m.default;await m.init(location.origin);await m.default.open('graph');document.querySelector('[data-graph-mode]').value='code';document.querySelector('[data-graph-mode]').dispatchEvent(new Event('change'));};
</script>`;
try { await withCopalBrowser({page,overrides:{'/static/js/copal.js':source},cdpTimeoutMs:120000,
 request:async(req,res)=>{
  if(!req.url.startsWith('/api/')&&!req.url.startsWith('/__fixture/'))return false;
  const body=Buffer.concat(await Array.fromAsync(req));
  const started=performance.now();
  const response=await fetch(new URL(req.url,backend),{method:req.method,headers,body:body.length?body:undefined});
  const bytes=Buffer.from(await response.arrayBuffer());
  if(req.url.startsWith('/api/memory/code/')||req.url.startsWith('/api/file-policy/')||req.url==='/api/files-v1/open-resource')responses.push({url:req.url,request:body.length?JSON.parse(body):null,status:response.status,value:JSON.parse(bytes),elapsed_ms:Math.round((performance.now()-started)*1000)/1000,response_bytes:bytes.length});
  res.writeHead(response.status,{'content-type':response.headers.get('content-type')||'application/json'});res.end(bytes);return true;
 }},async({evaluate,cdp})=>{
  // The shared static fixture's five-second wait is not a real native/Files
  // completion SLA. Keep bounded functional waits and the outer180s teardown;
  // frozen performance acceptance runs independently on its declared corpus.
  const until=async expression=>{
   const deadline=Date.now()+30000;
   do{if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25));}while(Date.now()<deadline);
   throw new Error('Real backend fixture timeout: '+expression+'; state='+JSON.stringify(await evaluate('({snapshot:window.__s11.snapshot(),text:document.querySelector(".code-workbench")?.textContent?.slice(0,1500)})')));
  };
  let groups=0;
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  const screenshot=async name=>{
   if(process.env.S11_SCREENSHOTS!=='1'||process.env.S11_DENIAL!=='erase')return;
   const folder=process.env.S11_SCREENSHOT_DIR||'.clanker/artifacts/code-memory-graph-2026-10-13/s11-real-codebase',file=folder+'/'+name+'.png';
   if(fs.existsSync(file)){assert(process.env.S11_IMAGE_PREIMAGE_PREFIX,'Fresh Hex+Git image preimage required');assert.deepEqual(fs.readFileSync(file),execFileSync('git',['cat-file','blob',process.env.S11_IMAGE_PREIMAGE_PREFIX+'-'+name]));}
   fs.mkdirSync(folder,{recursive:true});
   const result=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});fs.writeFileSync(file,Buffer.from(result.data,'base64'));
  };
  const click=label=>evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent===${JSON.stringify(label)}&&!b.disabled&&b.getClientRects().length).click()`);
  const ready=async()=>{await until(`['ready','error','cancelled','stale'].includes(window.__s11.snapshot()?.status)`);const snapshot=await evaluate('window.__s11.snapshot()');assert.equal(snapshot.status,'ready',JSON.stringify(snapshot.error));};
  const graph=async()=>{await evaluate(`window.copalModule.open('graph')`);await evaluate(`{const mode=document.querySelector('[data-graph-mode]');if(mode.value!=='code'){mode.value='code';mode.dispatchEvent(new Event('change'));}if(document.querySelector('[data-code-workspace]').textContent!==${JSON.stringify(info.workspace_id)})window.__s11.select(${JSON.stringify(info.workspace_id)});}`);await ready();};
  // Current Graph defaults to the map. Its public controller enters the real
  // Search & evidence surface; indexed queries and every source/Files call
  // remain actual HTTP/native operations. Bound results to declarations.
  const wanted=n=>n.kind==='declaration'&&n.label==='resolve_resource'&&n.source?.rel_path===info.selected_rel_path;
  const search=async()=>{
   const before=responses.filter(r=>r.url==='/api/memory/code/graph').length;
   await evaluate(`window.__s11.run('search',{query:'resolve_resource',filter:{kinds:['declaration']}})`);
   await until(`window.__s11.snapshot()?.operation==='search'`);await ready();
   assert(responses.filter(r=>r.url==='/api/memory/code/graph').length>before,'Search must execute actual native Graph query');
   // The byte/token envelope can paginate even a small symbol result set.
   // Follow actual native cursors through the visible Load more control.
   for(let page=0;page<20;page++){
    const snapshot=await evaluate('window.__s11.snapshot()');
    if(snapshot.nodes.some(wanted))return;
    assert(snapshot.next_cursor,'Target absent from complete symbol result: '+JSON.stringify(snapshot.nodes.map(n=>({kind:n.kind,label:n.label,source:n.source}))));
    await click('Load more');await ready();
   }
   assert.fail('Target exceeded bounded browser fixture pagination');
  };
  const selected=()=>evaluate(`JSON.parse(document.querySelector('[data-code-detail]').textContent)`);
  await until('window.__run');await evaluate('window.__run()');
  let started=performance.now();
  await evaluate(`window.__s11.select(${JSON.stringify(info.workspace_id)})`);await ready();
  uiTimings.push({operation:'initial_catalog_to_ready',elapsed_ms:Math.round((performance.now()-started)*1000)/1000});
  assert((await evaluate('window.__s11.snapshot().nodes.length'))>0);groups++;
  await search();
  const hit=(await evaluate('window.__s11.snapshot().nodes')).find(wanted);
  assert(hit,'Real copied repository declaration must be searchable');assert.match(hit.id,/^decl:[a-f0-9]{64}$/);
  await evaluate(`document.querySelector('[data-code-id="${hit.id}"]').click()`);
  assert.equal((await selected()).id,hit.id);
  const graphResponse=responses.filter(r=>r.url==='/api/memory/code/graph').at(-1).value;
  assert.equal(graphResponse.receipt.workspace_id,info.workspace_id);
  assert(graphResponse.hits.some(h=>h.id===hit.id));groups++;
  await click('Outgoing');await ready();
  assert.equal(responses.filter(r=>r.url==='/api/memory/code/graph').at(-1).request.query.direction,'outgoing');groups++;
  // Restore query and make the actual source→Files→cold Editor handoff.
  await search();await evaluate(`document.querySelector('[data-code-id="${hit.id}"]').click()`);
  await until(`[...document.querySelectorAll('button')].some(b=>b.textContent==='Open source'&&!b.disabled)`);
  assert.equal((await evaluate('window.__s11.snapshot().operation_capabilities')).find(c=>c.operation==='read').available,false,'Native metadata cannot read source; the verified Files bridge owns this action');
  await screenshot('graph');
  started=performance.now();
  await click('Open source');
  // Real native source validation precedes cold Editor mounting. This is a
  // bounded test wait, not a product job timeout or a mocked handoff.
  for(let attempt=0;attempt<120;attempt++){
   if(await evaluate(`document.querySelector('.copal-notes-window:not(.hidden) .cm-content')?.textContent.length>0`))break;
   await new Promise(r=>setTimeout(r,250));
  }
  assert(await evaluate(`document.querySelector('.copal-notes-window:not(.hidden) .cm-content')?.textContent.length>0`),JSON.stringify(await evaluate('({handoff:window.__s11Handoff,view:window.__s11.view(),status:[...document.querySelectorAll("[role=status]")].map(n=>n.textContent)})')));
  await until(`document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host')?.dataset.syntaxReady==='ready'`);
  uiTimings.push({operation:'source_click_to_actual_editor_ready',elapsed_ms:Math.round((performance.now()-started)*1000)/1000});
  const bridge=responses.filter(r=>r.url==='/api/memory/code/resource'&&r.request.operation==='resolve').at(-1).value;
  assert.equal(bridge.state,'current');assert(bridge.current_source_verified);assert.equal(bridge.reference.id,hit.id);
  assert.deepEqual(bridge.receipt,graphResponse.receipt);
  const opened=responses.find(r=>r.url==='/api/files-v1/open-resource');assert(opened);
  assert.equal(opened.request.resource_ref,bridge.destination.resource.ref);
  assert.equal(createHash('sha256').update(opened.value.payload.text).digest('hex'),info.source_sha256);
  assert.equal(opened.value.payload.resource.capabilities.edit,true);
  await screenshot('editor');
  const observed=await evaluate(`({buffer:window.__s11.buffer(),lines:[...document.querySelectorAll('.copal-notes-window:not(.hidden) .cm-line')].map(n=>n.textContent)})`);
  console.log(JSON.stringify({editor_observation:{buffer_present:!!observed.buffer,buffer_keys:Object.keys(observed.buffer||{}),buffer_sha256:typeof observed.buffer?.envelope?.text==='string'?createHash('sha256').update(observed.buffer.envelope.text).digest('hex'):null,expected_sha256:info.source_sha256,visible_lines:observed.lines.length,matching_lines:observed.lines.filter(line=>opened.value.payload.text.split('\n').includes(line)).length}}));
  // CodeMirror virtualizes/scrolls its document. Verify real visible lines
  // against source and the complete authoritative buffer below, without
  // assuming that the first line or selected declaration is currently mounted.
  // Rich docstrings intentionally render their body without triple quotes.
  // Verify ordinary lines literally and each displayed docstring against its
  // own source span, instead of treating that presentation as corrupted bytes.
  await until(`(()=>{const lines=[...document.querySelectorAll('.copal-notes-window:not(.hidden) .cm-line')].filter(n=>!n.querySelector('.cm-rich-comment-widget')).map(n=>n.textContent.replace(/\\u00a0/g,' ')).filter(s=>s.trim().length>10);const source=window.__s11.buffer()?.envelope?.text;const widgets=[...document.querySelectorAll('.copal-notes-window:not(.hidden) .cm-rich-comment-widget')];return typeof source==='string'&&lines.length>2&&lines.every(line=>source.split('\\n').includes(line))&&widgets.length>0&&widgets.every(w=>{const from=Number(w.dataset.commentSourceFrom),to=Number(w.dataset.commentSourceTo),body=w.querySelector('.cm-rich-comment-prose')?.textContent.trim();return Number.isInteger(from)&&Number.isInteger(to)&&from>=0&&to>from&&to<=source.length&&body&&source.slice(from,to).includes(body);});})()`);
  const span=bridge.source.span;
  const excerptBytes=Buffer.from(bridge.excerpt),windowEnd=span.start_byte+excerptBytes.length;
  assert(excerptBytes.length>0&&excerptBytes.length<=1024&&windowEnd<=span.end_byte);
  assert.equal(span.start_byte,hit.source.span.start_byte);
  assert.equal(Buffer.from(opened.value.payload.text).subarray(span.start_byte,windowEnd).toString(),bridge.excerpt);
  const buffer=await evaluate('window.__s11.buffer()');
  assert.equal(createHash('sha256').update(buffer.envelope.text).digest('hex'),info.source_sha256);
  assert.equal(await evaluate(`document.querySelectorAll('.copal-notes-window:not(.hidden)').length`),1);groups++;
  const reference={workspace_id:info.workspace_id,operation:'resolve',reference:bridge.reference,excerpt_bytes:1024};
  const resolve=()=>evaluate(`fetch('/api/memory/code/resource',{method:'POST',headers:{'content-type':'application/json'},body:${JSON.stringify(JSON.stringify(reference))}}).then(r=>r.json())`);
  await controls('edit');const stale=await resolve();assert.equal(stale.state,'stale');assert.equal(stale.destination,null);assert.equal(stale.excerpt,null);groups++;
  await controls('restore');
  for(const change of ['mode','workspace']){
   await graph();await search();await evaluate(`document.querySelector('[data-code-id="${hit.id}"]').click()`);
   await until(`[...document.querySelectorAll('button')].some(b=>b.textContent==='Open source'&&!b.disabled)`);
   await controls('hold');const before=responses.filter(r=>r.url==='/api/files-v1/open-resource').length,acks=await evaluate('window.__sourceAcks.length+window.__sourceAborts');
   await click('Open source');
   while(!(await (await fetch(new URL('/__fixture/info',backend),{headers})).json()).held)await new Promise(r=>setTimeout(r,10));
   if(change==='mode')await evaluate(`document.querySelector('[data-graph-mode]').value='documents';document.querySelector('[data-graph-mode]').dispatchEvent(new Event('change'))`);
   else await evaluate(`window.__s11.select(${JSON.stringify(info.bob_workspace_id)})`);
   await controls('release');
   if(change==='mode')await until(`!document.querySelector('.code-workbench')`);
   await until(`window.__sourceAcks.length+window.__sourceAborts===${acks+1}`);
   // Current UI cancels source resolution when its Graph scope is replaced.
   // Either cancellation or an acknowledged response must yield no Files open.
   if(await evaluate('window.__sourceAcks.length')>acks)assert.equal(await evaluate('window.__sourceAcks.at(-1).state'),'current');
   await evaluate(`new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))`);
   assert.equal(responses.filter(r=>r.url==='/api/files-v1/open-resource').length,before);groups++;
  }
  // Independent fresh published corpus per parameter: revocation must deny
  // a live ready index, not merely an already erased one.
  const removal=await controls(process.env.S11_DENIAL);const denied=await resolve();
  assert.equal(denied.state,process.env.S11_DENIAL==='erase'?'erased':'denied');assert.equal(denied.destination,null);assert.equal(denied.excerpt,null);groups++;
  if(removal.cleanup_pending)await controls('cleanup');
  const wrong=await evaluate(`fetch('/api/memory/code/graph',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({workspace_id:${JSON.stringify(info.bob_workspace_id)},query:{operation:'catalog'}})}).then(r=>r.json())`);
  assert.equal(wrong.error.code,'denied');assert.equal(wrong.receipt,null);groups++;
  console.log(`S11 real backend browser: ${groups} groups passed (${process.env.S11_DENIAL}); corpus manifest ${info.manifest.sha256}; actual Graph/source/Files HTTP and cold CodeMirror, no model/copied-code execution`);
}); } finally {
 console.log(JSON.stringify({s11_actual_http_observations:responses.map(r=>({route:r.url,operation:r.request?.query?.operation||r.request?.operation||'open-resource',status:r.status,elapsed_ms:r.elapsed_ms,response_bytes:r.response_bytes,state:r.value.state||null,error:r.value.error?.code||null,capabilities:r.request?.operation==='capabilities'?r.value.capabilities:null,cost:r.value.cost||null})),uiTimings,limits:'Single executed journey, not p95/SLA; actual isolated backend/native/Files plus loopback/CDP; no source bodies or private handles in measurement output'}));
}
