import test from 'node:test';
import assert from 'node:assert/strict';
import {graphDisabledReason,executeCodeGraphNavigation} from '../../static/js/copal/resourceGraphCommands.js';
const file={provider:'host',resource_id:'opaque-file-id',resource_ref:'opaque-file-ref',kind:'file',name:'same.py',capabilities:['open']};
const parent={provider:'host',resource_id:'opaque-parent-id',resource_ref:'opaque-parent-ref',kind:'folder',name:'Authorized project',capabilities:['children']};
function fixture(){
 const calls=[];let observed=4,owner='original',confirm=true;
 const binding={version:1,generation:5,workspace:{id:'canonical-workspace',name:'Project',revision:1,archived:false},open_relative:'same.py',scope_kind:'parent_folder'};
 const options={resource:file,generation:4,dirty:true,isCurrent:g=>g===observed&&owner==='original',acceptGeneration:async({before,after})=>{if(owner!=='original'||![before,after].includes(observed))return false;observed=after;return true;},client:{reveal:async()=>{calls.push('reveal');return {resource:{...file,policy_generation:4},parent:{...parent,policy_generation:4}};},codeGraphTarget:async(ref,o)=>{calls.push(['bind',ref,o]);return binding;},workspaceResource:async(id,path)=>{calls.push(['reissue',id,path]);return {generation:5,workspace:binding.workspace,resource:{...file,resource_ref:'reissued-ref'}};}},confirmParent:async target=>{calls.push(['confirm',target]);return confirm;},handoff:async(b,o)=>{calls.push(['handoff',b,o.dirty,o.isCurrent()]);return true;}};
 return {options,calls,binding,set observed(v){observed=v;},set owner(v){owner=v;},set confirm(v){confirm=v;}};
}
test('one captured Host target only; virtual/special/multi-selection never gain file authority',()=>{
 assert.equal(graphDisabledReason([file]),'');assert.equal(graphDisabledReason([parent]),'');
 for(const entries of [[],[file,file],[{...file,provider:'copal'}],[{...file,kind:'symlink'}],[{...file,capabilities:[]}]])assert(graphDisabledReason(entries));
});
test('authorized parent confirmation, canonical generation reconciliation and exact file identity precede dirty-safe handoff',async()=>{
 const f=fixture(),result=await executeCodeGraphNavigation(f.options);assert.equal(result.ok,true);assert.equal(result.generation,5);assert.match(result.message,/unsaved buffer was not saved or indexed/);
 assert.deepEqual(f.calls[1],['confirm',{name:'Authorized project',dirty:true}]);assert.deepEqual(f.calls.at(-1).slice(2),[true,true]);
 assert.deepEqual(f.calls[2],['bind','opaque-file-ref',{signal:null}]);assert.deepEqual(f.calls[3],['reissue','canonical-workspace','same.py']);
});
test('cancel before bind performs no registration/index or handoff',async()=>{const f=fixture();f.confirm=false;const r=await executeCodeGraphNavigation(f.options);assert.equal(r.code,'cancelled');assert.equal(f.calls.length,2);});
test('known captured workspace uses exact id/revision without parent broadening dialog',async()=>{
 const f=fixture();f.options.knownWorkspace={id:'canonical-workspace',revision:1};f.binding.scope_kind='captured_workspace';const r=await executeCodeGraphNavigation(f.options);assert.equal(r.ok,true);assert.equal(f.calls.some(x=>x==='reveal'),false);assert.deepEqual(f.calls[0][2],{workspaceId:'canonical-workspace',expectedWorkspaceRevision:1,signal:null});
});
test('stale owner/selection/policy after confirmation cannot bind',async()=>{const f=fixture();f.options.confirmParent=async()=>{f.owner='different';return true;};assert.equal((await executeCodeGraphNavigation(f.options)).code,'stale');assert(!f.calls.some(x=>Array.isArray(x)&&x[0]==='bind'));});
test('unrelated newer policy and wrong reissued identity fail closed, not self-healed',async()=>{
 const f=fixture();f.options.client.workspaceResource=async()=>{f.observed=6;return {generation:6,workspace:f.binding.workspace,resource:file};};assert.equal((await executeCodeGraphNavigation(f.options)).code,'stale');assert(!f.calls.some(x=>Array.isArray(x)&&x[0]==='handoff'));
 const g=fixture();g.options.client.workspaceResource=async()=>({generation:5,workspace:g.binding.workspace,resource:{...file,resource_id:'same-name-other-file'}});assert.equal((await executeCodeGraphNavigation(g.options)).code,'stale');
});
test('exact-file-only parent denial never grants or binds; backend private errors are redacted',async()=>{
 const f=fixture();f.options.client.reveal=async()=>({resource:{...file,policy_generation:4},parent:{...parent,policy_generation:4,capabilities:[]}});assert.equal((await executeCodeGraphNavigation(f.options)).code,'parent');assert.equal(f.calls.length,0);
 const g=fixture();g.options.client.reveal=async()=>{throw Error('PRIVATE /Users/secret');};assert.doesNotMatch((await executeCodeGraphNavigation(g.options)).message,/PRIVATE|Users/);
});
test('different containing scope or revision cannot be acknowledged merely because file name or generation matches',async()=>{
 for(const workspace of [{id:'wrong',revision:1},{id:'canonical-workspace',revision:2}]){
   const f=fixture();f.options.client.workspaceResource=async()=>({generation:5,workspace,resource:file});
   assert.equal((await executeCodeGraphNavigation(f.options)).code,'stale');assert(!f.calls.some(x=>Array.isArray(x)&&x[0]==='handoff'));
 }
 const f=fixture();f.options.client.reveal=async()=>({resource:{...file,policy_generation:4},parent:{...parent,policy_generation:5}});
 assert.equal((await executeCodeGraphNavigation(f.options)).code,'parent');assert.equal(f.calls.length,0);
});
