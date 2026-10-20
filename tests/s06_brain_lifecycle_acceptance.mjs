import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// Actual mounted memory.js consumer, with only dependencies/transport supplied
// by a disposable static fixture. The probe exposes state/calls, not a parallel
// ticket implementation. No app import or running service is used.
const source = fs.readFileSync('static/js/memory.js', 'utf8');
const probe = `
window.audit.probe = {
 snapshot(){ return JSON.parse(JSON.stringify({nodes:graphState.nodes,edges:graphState.edges,
 selected:graphState.selected,totals:graphState.totals,receipt:graphState.receipt,memoriesLoading,memories,trustPrefsState,
 positions:[...graphState.positions],state:graphState.canvas?.dataset.graphState,
 pending:graphState.canvas?.dataset.graphPending,errors:audit.errors,toasts:audit.toasts,logs:audit.logs,detail:document.getElementById('memory-graph-detail').textContent,
 counts:document.getElementById('memory-graph-counts').textContent})); },
 start(op){const canvas=document.getElementById('memory-graph-canvas');
  if(op==='overview')audit.job=loadMemoryGraph();
  if(op==='cues'){const search=document.getElementById('memory-graph-search');search.value='needle';search.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter'}));}
  if(op==='expand')audit.job=_expandNode('base',canvas);
  if(op==='trace')audit.job=_traceBetween('base','second');
 },
 seed(id='base'){memories=[{id,text:'ordinary memory'}];},
 select(id){graphState.selected=id;graphState.detailRevision++;_renderGraphDetail(id);},
 click(id,shiftKey=false){const canvas=document.getElementById('memory-graph-canvas'),rect=canvas.getBoundingClientRect(),point=graphState.positions.get(id);canvas.dispatchEvent(new PointerEvent('pointerup',{clientX:rect.left+(point.x*graphState.scale+graphState.offsetX)*rect.width/canvas.width,clientY:rect.top+(point.y*graphState.scale+graphState.offsetY)*rect.height/canvas.height,shiftKey}));},
};`;
const page = `<!doctype html><body data-account-id="alice"><div id="memory-modal"><section data-memory-panel="graph">
<canvas id="memory-graph-canvas" width="640" height="420"></canvas><input id="memory-graph-search">
<div id="memory-graph-tags"></div><div id="memory-graph-counts"></div><div id="memory-graph-detail"></div>
</section></div><script type="module" src="/fixture.js"></script>`;
const fixture = `
window.audit={pending:[],errors:[],toasts:[],logs:[],calls:[],confirms:[],deletes:[],lists:[],prefs:[],deleteRefused:false,holdConfirm:false,holdDelete:false,holdList:false,holdPrefs:false};
console.error=(...args)=>audit.logs.push(args.map(String).join(' '));
window.fetch=async(input,options={})=>{const url=new URL(String(input),location.origin);audit.calls.push({path:url.pathname,method:options.method||'GET'});
 if(url.pathname==='/api/memory/graph')return new Promise((resolve,reject)=>audit.pending.push({op:url.searchParams.get('op'),resolve,reject}));
 if(options.method==='DELETE'){const response=()=>new Response(JSON.stringify(audit.deleteRefused?{detail:{code:'code_node_forget_requires_workspace_erase',message:'/private/root/raw native diagnostic'}}:{ok:true}),{status:audit.deleteRefused?409:200,headers:{'Content-Type':'application/json'}});return audit.holdDelete?new Promise(resolve=>audit.deletes.push(()=>resolve(response()))):response();}
 if(url.pathname==='/api/memory'&&audit.holdList)return new Promise(resolve=>audit.lists.push(()=>resolve(new Response(JSON.stringify({memory:[{id:'STALE-list',text:'must not publish'}]}),{status:200}))));
 if(url.pathname==='/api/prefs'&&audit.holdPrefs)return new Promise(resolve=>audit.prefs.push(()=>resolve(new Response(JSON.stringify({memory_trust_auto:true}),{status:200}))));
 return new Response(JSON.stringify(url.pathname==='/api/memory'?{memory:[]}:{}),{status:200,headers:{'Content-Type':'application/json'}});
};
audit.reply=(index,value,error=false)=>{const request=audit.pending[index];if(error)request.reject(Object.assign(new Error('held failure'),{name:error==='cancel'?'AbortError':'Error'}));else request.resolve(new Response(JSON.stringify(value),{status:200}));};
audit.memory=await import('/static/js/memory.js');window.ready=true;`;
const ui = `export default {esc:s=>String(s),showError:s=>audit.errors.push(s),showToast:s=>audit.toasts.push(s),styledConfirm:()=>audit.holdConfirm?new Promise(resolve=>audit.confirms.push(resolve)):Promise.resolve(true)};`;
const overrides = {
  '/fixture.js': fixture, '/static/js/memory.js': source + '\n' + probe,
  '/static/js/ui.js': ui, '/static/js/sessions.js': 'export default {};',
  '/static/js/spinner.js': 'export default {};',
  '/static/js/achievementProducer.js': 'export const recordPresentation=()=>Promise.resolve();',
  '/static/js/uiIcons.js': 'export const uiIcon=()=>"";',
  '/static/js/windowDrag.js': 'export const makeWindowDraggable=()=>{};',
  '/static/js/tileManager.js': 'export const snapModalToZone=()=>{};',
  '/static/js/toolWindowZOrder.js': 'export const topPortalZ=()=>100;',
  '/static/js/memoryExportDialog.js': 'export const createMemoryExportDialog=()=>{};export const downloadMemoryAsset=()=>{};',
  '/static/js/memoryImportDismiss.js': 'export const dismissAllPendingImportBatches=()=>{};',
};
const overview = (generation = 'g1', admission_epoch = 1) => ({generation,admission_epoch,nodes:[{id:'base',kind:'person',name:'Base'},{id:'second',kind:'person',name:'Second'}],edges:[],node_total:10,edge_total:10});
const result = (op, generation = 'g1') => op === 'cues'
  ? {generation,admission_epoch:1,hits:[{node:{id:'late',kind:'person',name:'LATE'}}]}
  : op === 'expand' ? {generation,admission_epoch:1,hits:[{other:{id:'late',kind:'person',name:'LATE'},edge:{id:'late-edge',src_id:'base',dst_id:'late',tag:'knows'}}]}
  : {generation,admission_epoch:1,paths:[{node_ids:['base','late'],tags:['late-trace']}]};

await withCopalBrowser({page,overrides}, async ({evaluate,until}) => {
  await until('window.ready');
  const reply = (i,value,error=false) => evaluate(`audit.reply(${i},${JSON.stringify(value)},${JSON.stringify(error)})`);
  const begin = async op => {const index=await evaluate('audit.pending.length');await evaluate(`audit.probe.start(${JSON.stringify(op)})`);await until(`audit.pending.length>${index}`);return index;};
  const load = async (generation='g1', epoch=1) => {await evaluate(`if(!document.querySelector('canvas')){const next=document.createElement('canvas');next.id='memory-graph-canvas';next.width=640;next.height=420;document.querySelector('[data-memory-panel]').prepend(next);}`);const index=await evaluate('audit.pending.length');await evaluate('void audit.memory.loadMemoryGraph()');await until(`audit.pending.length>${index}`);await reply(index,overview(generation,epoch));await until(`document.querySelector('canvas').dataset.graphState==='ready'`);};
  const snapshot = () => evaluate('audit.probe.snapshot()');
  let groups=0;
  for (const op of ['overview','cues','expand','trace']) for (const error of [false,true]) for (const invalidate of ['reload','account','workspace','generation','admission','unmount','forget']) {
    await load();const held=await begin(op);
    if (invalidate==='account') await evaluate(`document.body.dataset.accountId='owner-'+Math.random();window.dispatchEvent(new Event('openclank:auth-context-changed'))`);
    if (invalidate==='workspace') await evaluate(`localStorage.setItem('odysseus-workspace-id','ws-'+Math.random());window.dispatchEvent(new Event('workspace-change'))`);
    if (invalidate==='unmount') {
      await evaluate(`document.querySelector('canvas').remove()`);
    } else if (invalidate==='generation' && op==='overview') await load('g2');
    else if (invalidate==='generation') {
      const newer=await begin('expand');await reply(newer,result('expand','g2'));
      await until(`document.querySelector('canvas').dataset.graphState==='stale'`);
    } else if (invalidate==='admission' && op==='overview') await load('g1',2);
    else if (invalidate==='admission') {
      const newer=await begin('expand');await reply(newer,{...result('expand'),admission_epoch:2});
      await until(`document.querySelector('canvas').dataset.graphState==='stale'`);
    } else if(invalidate==='forget') {
      await evaluate('audit.probe.seed();audit.memory.deleteMemory("base")');
    } else if(invalidate==='reload') await load();
    const before=await snapshot();await reply(held,result(op),error);
    await evaluate('new Promise(resolve=>setTimeout(resolve,0))');
    assert.deepEqual(await snapshot(),before,`${op} ${error?'error':'success'} after ${invalidate}`);groups++;
  }
  // Valid overlapping expansions merge independently, canonical IDs/counts dedup.
  await load();const a=await begin('expand'),b=await begin('expand');await reply(b,result('expand'));await reply(a,result('expand'));
  await until(`document.querySelector('canvas').dataset.graphPending==='0'`);
  assert.equal((await snapshot()).nodes.length,3);assert.equal((await snapshot()).edges.length,1);assert.match((await snapshot()).counts,/3\/10 nodes.*1\/10 edges/);groups++;
  // Valid graph accumulation must not clobber newer interaction-owned details
  // or status, including a successful trace, error, or explicit cancellation.
  for(const outcome of [false,true,'cancel']){
    await load();const expansion=await begin('expand'),newTrace=await begin('trace');await reply(newTrace,result('trace'),outcome);await until(`document.querySelector('canvas').dataset.graphPending==='1'`);const newer=await snapshot();await reply(expansion,result('expand'));await until(`document.querySelector('canvas').dataset.graphPending==='0'`);const merged=await snapshot();assert.equal(merged.detail,newer.detail);assert.equal(merged.state,newer.state);assert.equal(merged.nodes.length,3);assert.equal(merged.edges.length,1);groups++;
  }
  // Replacement canvas has live search handler; multiple reloads do not duplicate it.
  await load();await load();const cue=await begin('cues');await reply(cue,result('cues'));await until(`audit.probe.snapshot().selected==='late'`);assert.equal((await snapshot()).nodes.length,3);groups++;
  const trace=await begin('trace');await reply(trace,result('trace'));await until(`audit.probe.snapshot().detail.includes('late-trace')`);groups++;
  await load();await evaluate(`audit.probe.click('base')`);assert.equal((await snapshot()).selected,'base');const pointerExpand=await evaluate('audit.pending.length');await evaluate(`audit.probe.click('base')`);await until(`audit.pending.length>${pointerExpand}`);assert.equal(await evaluate(`audit.pending[${pointerExpand}].op`),'expand');await reply(pointerExpand,result('expand'));await until(`audit.probe.snapshot().nodes.length===3`);const pointerTrace=await evaluate('audit.pending.length');await evaluate(`audit.probe.click('second',true)`);await until(`audit.pending.length>${pointerTrace}`);assert.equal(await evaluate(`audit.pending[${pointerTrace}].op`),'trace');await reply(pointerTrace,result('trace'));await until(`audit.probe.snapshot().detail.includes('late-trace')`);groups++;
  const cancel=await begin('trace');await reply(cancel,{},'cancel');await until(`document.querySelector('canvas').dataset.graphState==='cancelled'`);assert.match((await snapshot()).detail,/cancelled/);groups++;
  // Confirmation and DELETE are both held independently. Scope switches do
  // not need a reload to reject every late data/loading/error/toast completion.
  for(const phase of ['confirm','delete'])for(const change of ['account','workspace','reload','unmount'])for(const refused of [false,true]){
    await load();await evaluate(`audit.probe.seed();audit.deleteRefused=${refused};audit.holdConfirm=${phase==='confirm'};audit.holdDelete=${phase==='delete'};void(audit.deleteJob=audit.memory.deleteMemory('base'));`);
    await until(phase==='confirm'?'audit.confirms.length>0':'audit.deletes.length>0');const beforeDeleteCount=await evaluate(`audit.calls.filter(c=>c.method==='DELETE').length`);
    if(change==='account')await evaluate(`document.body.dataset.accountId='delete-owner-'+Math.random();window.dispatchEvent(new Event('openclank:auth-context-changed'))`);
    if(change==='workspace')await evaluate(`localStorage.setItem('odysseus-workspace-id','delete-ws-'+Math.random());window.dispatchEvent(new Event('workspace-change'))`);
    if(change==='reload')await load();
    if(change==='unmount')await evaluate(`document.querySelector('canvas').remove()`);
    const before=await snapshot();await evaluate(phase==='confirm'?'audit.confirms.shift()(true);audit.deleteJob':'audit.deletes.shift()();audit.deleteJob');assert.deepEqual(await snapshot(),before,`${phase} ${change} ${refused}`);if(phase==='confirm')assert.equal(await evaluate(`audit.calls.filter(c=>c.method==='DELETE').length`),beforeDeleteCount,'invalid confirmation must not send DELETE');await evaluate('audit.holdConfirm=false;audit.holdDelete=false;');groups++;
  }
  // Legacy producer with absent generation remains explicitly absent.
  for(const phase of ['list','prefs'])for(const change of ['account','workspace','reload','unmount']){
    await load();await evaluate(`audit.probe.seed();audit.deleteRefused=false;audit.holdList=${phase==='list'};audit.holdPrefs=${phase==='prefs'};void(audit.deleteJob=audit.memory.deleteMemory('base'));`);await until(phase==='list'?'audit.lists.length>0':'audit.prefs.length>0');assert.equal((await snapshot()).memoriesLoading,false,'background post-delete refresh must not strand Browse spinner');
    if(change==='account')await evaluate(`document.body.dataset.accountId='refresh-owner-'+Math.random();window.dispatchEvent(new Event('openclank:auth-context-changed'))`);
    if(change==='workspace')await evaluate(`localStorage.setItem('odysseus-workspace-id','refresh-ws-'+Math.random());window.dispatchEvent(new Event('workspace-change'))`);
    if(change==='reload')await load();if(change==='unmount')await evaluate(`document.querySelector('canvas').remove()`);
    const before=await snapshot();await evaluate(phase==='list'?'audit.lists.shift()();audit.deleteJob':'audit.prefs.shift()();audit.deleteJob');assert.deepEqual(await snapshot(),before,`late ${phase} refresh after ${change}`);assert.equal((await snapshot()).memoriesLoading,false);await evaluate('audit.holdList=false;audit.holdPrefs=false;');groups++;
  }
  await load(); // prior negative intentionally left the canvas unmounted
  const legacy=await evaluate('audit.pending.length');await evaluate('void audit.memory.loadMemoryGraph()');await until(`audit.pending.length>${legacy}`);const noGen=overview();delete noGen.generation;delete noGen.admission_epoch;await reply(legacy,noGen);await until(`document.querySelector('canvas').dataset.graphState==='ready'`);assert.deepEqual((await snapshot()).receipt,{});groups++;
  // Actual inspector button reaches known typed refusal, no implicit wider erase.
  const codeLoad=await evaluate('audit.pending.length');await evaluate('void audit.memory.loadMemoryGraph()');await until(`audit.pending.length>${codeLoad}`);await reply(codeLoad,{...overview(),nodes:[{id:'code-node',kind:'code_symbol',name:'checkout::a.py::opaque',label:'target'}]});await until(`document.querySelector('canvas').dataset.graphState==='ready'`);
  await evaluate(`audit.deleteRefused=true;audit.probe.select('code-node');document.querySelector('#memory-graph-detail button').click()`);
  await until('audit.errors.length>0');assert.match(await evaluate('audit.errors.at(-1)'),/explicit workspace-index erasure/);assert.doesNotMatch(await evaluate('audit.errors.at(-1)'),/private\/root/);assert.equal((await snapshot()).nodes[0].id,'code-node');assert.equal(await evaluate(`audit.calls.some(c=>c.path.includes('/index/remove'))`),false);groups++;
  for(const provenance of [{domain:'code'},{provenance:{domain:'code'}},{}]){
    const next=await evaluate('audit.pending.length');await evaluate('void audit.memory.loadMemoryGraph()');await until(`audit.pending.length>${next}`);await reply(next,{...overview(),nodes:[{id:'file-node',kind:'file',name:'ordinary-or-indexed-file',...provenance}]});await until(`document.querySelector('canvas').dataset.graphState==='ready'`);await evaluate(`audit.probe.select('file-node')`);const count=await evaluate(`document.querySelectorAll('#memory-graph-detail button').length`);assert.equal(count,Object.keys(provenance).length?1:0,'only typed code file provenance enables code Forget');if(count){const errors=await evaluate('audit.errors.length');await evaluate(`document.querySelector('#memory-graph-detail button').click()`);await until(`audit.errors.length>${errors}`);assert.match(await evaluate('audit.errors.at(-1)'),/explicit workspace-index erasure/);}groups++;
  }
  // Untyped legacy files are not guessed as code. Browse's existing Forget
  // may reach confirmation, but the actual known backend refusal stays visible.
  await evaluate(`audit.probe.seed('file-node');audit.holdConfirm=true;audit.deleteRefused=true;void(audit.legacyDelete=audit.memory.deleteMemory('file-node'))`);await until('audit.confirms.length>0');const legacyErrors=await evaluate('audit.errors.length');await evaluate('audit.confirms.shift()(true);audit.legacyDelete');await until(`audit.errors.length>${legacyErrors}`);assert.match(await evaluate('audit.errors.at(-1)'),/explicit workspace-index erasure.*Nothing was deleted/);assert.doesNotMatch(await evaluate('audit.errors.at(-1)'),/private\/root/);assert.equal((await snapshot()).nodes[0].id,'file-node');assert.equal((await snapshot()).memories[0].id,'file-node');assert.equal(await evaluate(`audit.calls.some(c=>c.path.includes('/index/remove'))`),false);groups++;
  assert.deepEqual(await evaluate(`audit.calls.find(c=>c.method==='DELETE'&&c.path==='/api/memory/base')`),{path:'/api/memory/base',method:'DELETE'},'ordinary Forget exact existing DELETE');
  console.log(`S06 Brain actual-consumer acceptance: ${groups} mounted browser groups passed`);
});
