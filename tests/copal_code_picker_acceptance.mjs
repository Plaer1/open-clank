// Actual Copal picker ownership regression. No real app/index/store.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const injection=`
window.__race={setup(){configureCopalStorage('picker-race');state.accountId='alice';state.workspace='docs';state.contextEpoch++;state.view='graph';state.docs=[];state.planning={tracks:[],floatingTodos:[]};state.body=document.querySelector('#graph');state.content=state.body;state.root=state.body;state.status=null;state.selected=null;state.windows.clear();state.windows.set('graph',{window:{body:state.body,content:state.body,root:state.body,visible:true,setStatus(){}},selected:null});state.windows.set('notes',{window:{body:document.querySelector('#notes'),content:document.querySelector('#notes'),root:document.querySelector('#notes'),visible:false,setStatus(){}},selected:null});notesFeature={render(){},projectDocument:d=>d};graphView=normalizeGraphState({mode:'code'});graphScopeKey=graphStorageKey(state.accountId,state.workspace);codeGraphSelection=null;renderGraph();},refresh(){return loadDocuments(true);},temporary(){activateView('notes',{navigation:false});activateView('graph',{navigation:false});},navigate(){activateView('notes');},controller(){return codeGraphController;}};
window.__race.close=()=>{state.windows.get('graph').window.visible=false;state.body.querySelector('.copal-graph-wrap')?._copalDestroy?.();};
window.__race.mode=()=>{getGraphView().mode='documents';renderGraph();};
window.__race.account=()=>{state.accountId='bob';state.contextEpoch++;window.dispatchEvent(new CustomEvent('openclank:auth-context-changed'));};
window.__race.workspace=()=>{state.workspace='other-docs';state.contextEpoch++;window.dispatchEvent(new CustomEvent('workspace-change'));};
window.__race.stale=()=>{state.contextEpoch++;};
window.__race.agent=()=>openWorkspaceBrowser({selectionKind:'agent_workspace'});
window.__pickedPaths=[];
window.__race.folder=path=>openWorkspaceBrowser({selectionKind:'app_folder',initialPath:path,onSelect:async chosen=>{window.__pickedPaths.push(chosen);}});
`;
const source=fs.readFileSync('static/js/copal.js','utf8').replace('export default { init,',injection+'\nexport default { init,');
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><main id="graph"></main><aside id="notes"></aside><div id="toast"></div><script>
window.__calls=[];window.__holdDocs=false;window.__holdBinding=false;window.__holdBrowse=[];window.__browseReleases={};window.__browseErrors=[];window.__browseDirs={};
window.fetch=async(url,options={})=>{url=String(url);window.__calls.push({url,method:options.method||'GET',body:typeof options.body==='string'?JSON.parse(options.body):null});const json=value=>new Response(JSON.stringify(value),{status:200});
if(url.includes('/api/workspace/browse')){const path=new URL(url,location.origin).searchParams.get('path')||'/fixture/code';if(window.__holdBrowse.includes(path))await new Promise(r=>window.__browseReleases[path]=r);if(window.__browseErrors.includes(path))return new Response('{}',{status:403});return json({path,parent:null,dirs:window.__browseDirs[path]||[],selectable:true});}
if(url.includes('/workspaces/from-path')){if(window.__holdBinding)await new Promise(r=>window.__bindingRelease=r);return json({workspace:window.__bindingResult||{id:'code-ws',path:'/fixture/code',name:'Code fixture'}});}
if(url.includes('/index/'))return json({index:{lifecycle:'failed',head_generation:null,error:{code:'synthetic_terminal',message:'No native index in this diagnostic'}}});
if(url.includes('/api/prefs/'))return json({value:false});
if(url.includes('/api/copal/documents')){if(window.__holdDocs)await new Promise(r=>window.__docsRelease=r);return json({docs:[]});}
if(url.includes('/api/copal/planning'))return json({tracks:[],floatingTodos:[]});
if(url.includes('/api/copal'))return json({});return json({});};
</script><script type="module">import '/static/js/copal.js';window.__ready=true;</script>`;
const results=[],retainedMountGuards=[];let supersededOldOutcome,agentOutcome,initialBrowseOutcomes,navigationOutcomes;
await withCopalBrowser({page,overrides:{'/static/js/copal.js':source}},async({evaluate,until})=>{
 await until('window.__ready && window.__race');
 async function trial(name,action){
  await evaluate(`window.__holdDocs=false;window.__holdBinding=true;window.__bindingRelease=null;window.__docsRelease=null;window.__race.setup();`);
  await evaluate(`window.__calls=[];window.__oldController=window.__race.controller();`);
  if(action==='refresh'){
   await evaluate(`window.__holdDocs=true;void(window.__refresh=window.__race.refresh());true;`);
   await until('Boolean(window.__docsRelease)');
  }
  await evaluate(`[...document.querySelectorAll('#graph button')].find(b=>b.textContent==='Choose code folder').click();`);
  await until(`document.querySelector('#workspace-use')?.disabled===false && document.querySelector('#workspace-modal')?.style.display==='flex'`);
  const enabled=await evaluate(`!document.querySelector('#workspace-use').disabled`);
  await evaluate(`document.querySelector('#workspace-use').click();`);
  await until('Boolean(window.__bindingRelease)');
  if(action==='refresh')await evaluate(`(async()=>{window.__holdDocs=false;window.__docsRelease();await window.__refresh;})()`);
  if(action==='temporary')await evaluate(`window.__race.temporary();`);
  if(action==='navigate')await evaluate(`window.__race.navigate();`);
  if(action==='cancel')await evaluate(`document.querySelector('#workspace-cancel').click();`);
  if(['close','mode','account','workspace'].includes(action))await evaluate(`window.__race.${action}();`);
  if(action==='superseded')await evaluate(`[...document.querySelectorAll('#graph button')].find(b=>b.textContent==='Choose code folder').click();`);
  await evaluate(`(async()=>{window.__holdBinding=false;window.__bindingRelease();await new Promise(r=>setTimeout(r,100));})()`);
  if(action==='superseded'){
   supersededOldOutcome=await evaluate(`({opens:window.__calls.filter(c=>c.url.endsWith('/index/open')).length,display:document.querySelector('#workspace-modal').style.display})`);
   assert.equal(supersededOldOutcome.opens,0,'Old binding cannot admit');
   assert.equal(supersededOldOutcome.display,'flex','Old late binding cannot close/cancel newer picker');
   await evaluate(`window.__holdBinding=true;window.__bindingRelease=null;document.querySelector('#workspace-use').click();`);
   await until('Boolean(window.__bindingRelease)');
   await evaluate(`(async()=>{window.__holdBinding=false;window.__bindingRelease();await new Promise(r=>setTimeout(r,100));})()`);
  }
  const result=await evaluate(`({opens:window.__calls.filter(c=>c.url.endsWith('/index/open')).length,folder:document.querySelector('[data-code-workspace]')?.textContent,index:document.querySelector('[data-code-index-status]')?.textContent,remounted:window.__oldController!==window.__race.controller(),pickerDisplay:document.querySelector('#workspace-modal')?.style.display})`);
  results.push({name,enabledWhenClicked:enabled,...result});
 }
 await trial('settled control',null);
 await trial('document refresh while binding awaits','refresh');
 await trial('temporary non-navigation activation while binding awaits','temporary');
 await trial('genuine navigation while binding awaits','navigate');
 await trial('close while binding awaits','close');
 await trial('mode change while binding awaits','mode');
 await trial('account change while binding awaits','account');
 await trial('workspace change while binding awaits','workspace');
 await trial('actual cancel while binding awaits','cancel');
 await trial('superseded picker late binding','superseded');
 for(const action of ['disposed','stale']){
  await evaluate(`window.__holdDocs=false;window.__race.setup();window.__oldController=window.__race.controller();`);
  await evaluate(action==='disposed'?`window.__oldController.dispose();`:`window.__race.stale();`);
  assert.equal(await evaluate(`window.__oldController.isCurrent()`),false);
  await evaluate(`window.__race.refresh()`);
  const result=await evaluate(`({remounted:window.__oldController!==window.__race.controller(),current:window.__race.controller().isCurrent()})`);
  assert(result.remounted&&result.current,'Cannot retain a '+action+' mount');
  retainedMountGuards.push({action,...result});
 }
 await evaluate(`window.__holdBinding=false;window.__calls=[];window.__race.agent();`);
 await until(`document.querySelector('#workspace-use')?.disabled===false`);
 await evaluate(`document.querySelector('#workspace-use').click();`);
 await until(`document.querySelector('#workspace-modal').style.display==='none'`);
 const agentSelected=await evaluate(`(async()=>{const workspace=await import('/static/js/workspace.js');return {id:workspace.getWorkspaceId(),purpose:window.__calls.find(c=>c.url.endsWith('/workspaces/from-path'))?.body?.purpose};})()`);
 assert.equal(agentSelected.id,'code-ws');assert.equal(agentSelected.purpose,'agent_workspace');
 await evaluate(`window.__holdBinding=true;window.__bindingRelease=null;window.__bindingResult={id:'cancelled-agent',path:'/cancelled',name:'Cancelled'};window.__calls=[];window.__race.agent();`);
 await until(`document.querySelector('#workspace-use')?.disabled===false`);
 await evaluate(`document.querySelector('#workspace-use').click();`);await until('Boolean(window.__bindingRelease)');
 await evaluate(`(async()=>{document.querySelector('#workspace-cancel').click();window.__holdBinding=false;window.__bindingRelease();await new Promise(r=>setTimeout(r,100));})()`);
 const agentCancelled=await evaluate(`(async()=>{const workspace=await import('/static/js/workspace.js');return {id:workspace.getWorkspaceId(),opens:window.__calls.filter(c=>c.url.endsWith('/index/open')).length};})()`);
 assert.equal(agentCancelled.id,'code-ws');assert.equal(agentCancelled.opens,0);
 agentOutcome={agentSelected,agentCancelled};
 await evaluate(`window.__holdBrowse=['/older'];void(window.__olderBrowse=window.__race.folder('/older'));true;`);
 await until(`Boolean(window.__browseReleases['/older'])`);
 await evaluate(`window.__race.folder('/newer')`);
 await evaluate(`(async()=>{window.__browseReleases['/older']();await window.__olderBrowse;})()`);
 const supersededInitial=await evaluate(`({path:document.querySelector('#workspace-cur-path').title,disabled:document.querySelector('#workspace-use').disabled})`);
 assert.equal(supersededInitial.path,'/newer');assert.equal(supersededInitial.disabled,false);
 await evaluate(`window.__holdBrowse=['/cancelled-initial'];void(window.__cancelledBrowse=window.__race.folder('/cancelled-initial'));true;`);
 await until(`Boolean(window.__browseReleases['/cancelled-initial'])`);
 await evaluate(`(async()=>{document.querySelector('#workspace-cancel').click();window.__browseReleases['/cancelled-initial']();await window.__cancelledBrowse;})()`);
 const cancelledInitial=await evaluate(`({path:document.querySelector('#workspace-cur-path').title,disabled:document.querySelector('#workspace-use').disabled,display:document.querySelector('#workspace-modal').style.display})`);
 assert.equal(cancelledInitial.path,'');assert.equal(cancelledInitial.disabled,true);assert.equal(cancelledInitial.display,'none');
 initialBrowseOutcomes={supersededInitial,cancelledInitial};

 // Actual picker DOM: an editable draft owns intent before Enter; neither an
 // initial response nor a reordered row/path request may choose another root.
 const draft=async(path,submit=false)=>evaluate(`(()=>{const input=document.querySelector('#workspace-cur-path');input.value=${JSON.stringify(path)};input.dispatchEvent(new Event('input',{bubbles:true}));${submit?"input.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}));":''}})()`);
 const picker=()=>evaluate(`({value:document.querySelector('#workspace-cur-path').value,path:document.querySelector('#workspace-cur-path').title,disabled:document.querySelector('#workspace-use').disabled,body:document.querySelector('#workspace-body').textContent,picked:[...window.__pickedPaths]})`);
 await evaluate(`window.__holdBrowse=['/typing-initial'];void(window.__typingInitial=window.__race.folder('/typing-initial'));true;`);
 await until(`Boolean(window.__browseReleases['/typing-initial'])`);
 await draft('/typed-draft');
 await evaluate(`(async()=>{window.__browseReleases['/typing-initial']();await window.__typingInitial;})()`);
 const typedBeforeEnter=await picker();
 assert.equal(typedBeforeEnter.value,'/typed-draft');assert.equal(typedBeforeEnter.path,'');assert(typedBeforeEnter.disabled);assert.match(typedBeforeEnter.body,/Press Enter/);
 await evaluate(`document.querySelector('#workspace-use').dispatchEvent(new MouseEvent('click',{bubbles:true}));`);
 assert.deepEqual((await picker()).picked,[],'Even synthetic disabled activation cannot select an unconfirmed draft');
 await draft('/typed-draft',true);await until(`document.querySelector('#workspace-cur-path').title==='/typed-draft'`);
 assert.equal((await picker()).disabled,false);

 await evaluate(`window.__holdBrowse=['/slow-row'];window.__browseDirs['/row-root']=[{name:'slow',path:'/slow-row'}];window.__race.folder('/row-root');`);
 await until(`Boolean(document.querySelector('.workspace-row[data-path]'))`);
 await evaluate(`document.querySelector('.workspace-row[data-path]').click();`);await until(`Boolean(window.__browseReleases['/slow-row'])`);
 assert.equal((await picker()).disabled,true);
 await draft('/fast-path',true);await until(`document.querySelector('#workspace-cur-path').title==='/fast-path'`);
 await evaluate(`(async()=>{window.__browseReleases['/slow-row']();await new Promise(r=>setTimeout(r,50));})()`);
 const reordered=await picker();assert.equal(reordered.path,'/fast-path');assert.equal(reordered.value,'/fast-path');assert.equal(reordered.disabled,false);

 await evaluate(`window.__holdBrowse=['/late-navigation'];`);await draft('/late-navigation',true);await until(`Boolean(window.__browseReleases['/late-navigation'])`);
 await evaluate(`(async()=>{document.querySelector('#workspace-cancel').click();await window.__race.folder('/replacement');window.__browseReleases['/late-navigation']();await new Promise(r=>setTimeout(r,50));})()`);
 const cancelledNavigation=await picker();assert.equal(cancelledNavigation.path,'/replacement');assert.equal(cancelledNavigation.disabled,false);

 await evaluate(`(async()=>{const ui=(await import('/static/js/ui.js')).default;window.__pickerErrors=[];window.__originalPickerError=ui.showError;ui.showError=message=>window.__pickerErrors.push(message);window.__holdBrowse=['/stale-error'];window.__browseErrors=['/stale-error','/latest-error'];})()`);
 await draft('/stale-error',true);await until(`Boolean(window.__browseReleases['/stale-error'])`);
 await draft('/after-error',true);await until(`document.querySelector('#workspace-cur-path').title==='/after-error'`);
 await evaluate(`(async()=>{window.__browseReleases['/stale-error']();await new Promise(r=>setTimeout(r,50));})()`);
 assert.equal((await picker()).path,'/after-error');assert.deepEqual(await evaluate('window.__pickerErrors'),[],'Obsolete browse errors do not toast over the latest folder');
 await draft('/latest-error',true);await until(`document.querySelector('#workspace-body').textContent==='Could not open folder'`);
 const failedNavigation=await picker();assert(failedNavigation.disabled);assert.equal(failedNavigation.path,'');assert.equal(failedNavigation.value,'/latest-error');
 assert.deepEqual(await evaluate('window.__pickerErrors'),['Could not open folder']);
 await evaluate(`document.querySelector('#workspace-use').dispatchEvent(new MouseEvent('click',{bubbles:true}));`);
 assert.deepEqual((await picker()).picked,[],'A failed request cannot select the previous folder');
 await draft('/confirmed',true);await until(`document.querySelector('#workspace-cur-path').title==='/confirmed'`);
 await evaluate(`document.querySelector('#workspace-use').click();`);await until(`document.querySelector('#workspace-modal').style.display==='none'`);
 assert.deepEqual((await picker()).picked,['/confirmed']);
 await evaluate(`(async()=>{(await import('/static/js/ui.js')).default.showError=window.__originalPickerError;})()`);
 navigationOutcomes={typedBeforeEnter,reordered,cancelledNavigation,failedNavigation,confirmed:(await picker()).picked};
});
assert.equal(results[0].opens,1,'Settled actual integration must admit once');
for(const result of results.slice(3,-1))assert.equal(result.opens,0,result.name+' must not admit an obsolete selection');
assert.equal(results.at(-1).opens,1,'New picker must still complete and admit exactly once');
const fixed=true;
assert.equal(results[1].opens,fixed?1:0,'Document-only refresh selection ownership');
assert.equal(results[2].opens,fixed?1:0,'Internal temporary activation selection ownership');
assert(results.every(r=>r.enabledWhenClicked));
console.log(JSON.stringify({mode:'regression acceptance',results,retainedMountGuards,supersededOldOutcome,agentOutcome,initialBrowseOutcomes,navigationOutcomes},null,2));

