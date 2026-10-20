import assert from 'node:assert/strict';
import { createCodeGraphModel, codeEdgeId, codeFacetCount } from '../../static/js/copal/codeGraphModel.js';
const receipt = {workspace_id:'ws',registration_id:'r',checkout_id:'c',source_snapshot_id:'s',head_generation:'g',admission_epoch:1,policy_generation:2,config_fingerprint:'f'};
const envelope = (extra={}) => ({schema:'open-clank.code-query/v1',domain:'code',receipt, hits:[{id:'decl:abc',kind:'function',label:'A',properties:{custom:{source:'literal'}}}], edges:[{from:'decl:abc',to:'file:a',occurrence_id:'occ:1',resolution_state:'unresolved'}],paths:[],counts:[{name:'eligible',value:8,unit:'file',exactness:'exact'}],facets:{kinds:[{value:'file',count:8,unit:'file',exactness:'exact'}]},omissions:[],capabilities:[],channels:[],reasons:[],partial:false,degraded:false,cost:{},operation_capabilities:[{operation:'catalog',available:true}],next_cursor:'page2',error:null,...extra});
const query={operation:'catalog',fields:['label','properties'],filter:{path_prefix:'src/'}};
let calls=[],changes=[],current=true;
const model=createCodeGraphModel({isCurrent:()=>current,changed:value=>changes.push(value),request:(ws,q,signal)=>new Promise((resolve,reject)=>calls.push({ws,q,signal,resolve,reject}))});
const first=model.run('ws',query); calls.at(-1).resolve(envelope()); assert(await first);
assert.equal(model.snapshot().nodes[0].id,'decl:abc'); assert.equal(model.snapshot().nodes[0].domain,'code');
assert.equal(model.snapshot().edges[0].id,codeEdgeId(envelope().edges[0]));
const more=model.run('ws',query,{more:true}); assert.deepEqual(calls.at(-1).q.expected_receipt,receipt); assert.equal(calls.at(-1).q.cursor,'page2');
calls.at(-1).resolve(envelope({hits:[{id:'decl:abc',kind:'function'},{id:'file:a',kind:'file'}],next_cursor:null})); assert(await more);
assert.equal(model.snapshot().nodes.length,2); assert.equal(model.snapshot().counts[0].value,8); assert.equal(model.snapshot().facets.kinds[0].count,8);
assert.deepEqual(model.snapshot().returned,{hits:2,edges:1,paths:0}); assert.equal(await model.run('ws',{...query,filter:{}},{more:true}),false);
for (const field of ['head_generation','admission_epoch','policy_generation','config_fingerprint','registration_id','source_snapshot_id','checkout_id']) {
  const fresh=model.run('ws',query); calls.at(-1).resolve(envelope()); await fresh;
  const stale=model.run('ws',query,{more:true}); calls.at(-1).resolve(envelope({receipt:{...receipt,[field]:typeof receipt[field]==='number'?receipt[field]+1:'changed'}})); assert.equal(await stale,false);
  assert.equal(model.snapshot().status,'stale'); assert.equal(model.snapshot().nodes.length,1); assert.equal(model.snapshot().next_cursor,null);
}
for (const outcome of ['success','error']) {
  const held=model.run('ws',query), count=changes.length; current=false;
  if(outcome==='success')calls.at(-1).resolve(envelope());else calls.at(-1).reject(new Error('/private/native'));
  assert.equal(await held,false); assert.equal(changes.length,count); current=true;
}
const old=model.run('ws',query); const oldCall=calls.at(-1); const newer=model.run('ws',{...query,fields:[]});
calls.at(-1).resolve(envelope({hits:[{id:'file:abc',kind:'file'}]}));await newer;oldCall.resolve(envelope());assert.equal(await old,false);assert.equal(model.snapshot().nodes[0].id,'file:abc');assert(oldCall.signal.aborted);
const denied=model.run('ws',query);calls.at(-1).resolve(envelope({receipt:null,error:{code:'denied',message:'Denied'}}));assert.equal(await denied,false);assert.equal(model.snapshot().nodes.length,0);assert.equal(model.snapshot().status,'error');
const raw=model.run('ws',{operation:'read',fields:['excerpt']});calls.at(-1).resolve(envelope({operation:'read',receipt:null,hits:[{id:'raw:opaque',kind:'raw_window',excerpt:'window',raw_source:{hash_algorithm:'sha256'}}],edges:[],paths:[]}));assert(await raw);assert.equal(model.snapshot().nodes.length,0);assert.equal(model.snapshot().raw_windows.length,1);assert.equal(await model.run('ws',{operation:'neighbors',seed_ids:['raw:opaque']}),false);
const backed=model.run('ws',{operation:'read',fields:['excerpt']});calls.at(-1).resolve(envelope({operation:'read',hits:[{id:'file:abc',kind:'raw_window',excerpt:'window'}],edges:[],paths:[]}));assert(await backed);assert.equal(model.snapshot().nodes.length,0);assert.equal(model.snapshot().raw_windows[0].id,'file:abc');
const missingBacked=model.run('ws',{operation:'read',fields:['excerpt']});calls.at(-1).resolve(envelope({operation:'read',receipt:null,hits:[{id:'file:abc',kind:'raw_window'}],edges:[],paths:[]}));assert.equal(await missingBacked,false);assert.equal(model.snapshot().error.code,'invalid_evidence');
for(const extra of [{receipt:{workspace_id:'ws'}},{hits:[{id:'decl:opaque',kind:'function'}]},{paths:[null]},{edges:[{...envelope().edges[0],witnesses:[null]}]}]){const malformed=model.run('ws',query);calls.at(-1).resolve(envelope(extra));assert.equal(await malformed,false);assert.equal(model.snapshot().error.code,'invalid_evidence');}
const paths=[{node_ids:['decl:abc','file:a'],edges:envelope().edges}];const pathPage=model.run('ws',query);calls.at(-1).resolve(envelope({paths}));await pathPage;const edgePage=model.run('ws',query,{more:true});calls.at(-1).resolve(envelope({hits:[],edges:[{...envelope().edges[0],occurrence_id:'occ:2'}],paths:[]}));assert(await edgePage);assert.deepEqual(model.snapshot().returned,{hits:0,edges:1,paths:0});assert.deepEqual(model.snapshot().paths,paths);
const compact=model.run('ws',query);calls.at(-1).resolve({schema:'open-clank.code-query/v1',domain:'code',operation:'catalog',receipt:null,error:{code:'budget_exceeded',message:'Budget exceeded'},partial:false,degraded:true,cost:{}});assert.equal(await compact,false);assert.equal(model.snapshot().error.code,'budget_exceeded');
const sourceWindow={rel_path:'unsupported.js',start_byte:0,end_byte:100,continuation:'window-cursor',continuity:'progress_only_nonatomic'};const emptyRaw=model.run('ws',{operation:'search',channels:['raw']});calls.at(-1).resolve(envelope({operation:'search',receipt:null,hits:[],edges:[],paths:[],source_window:sourceWindow,next_cursor:null}));assert(await emptyRaw);assert.equal(model.snapshot().nodes.length,0);assert.deepEqual(model.snapshot().source_window,sourceWindow);
const held=model.run('ws',query), count=changes.length;model.dispose();calls.at(-1).resolve(envelope());assert.equal(await held,false);assert.equal(changes.length,count);
console.log('S06 code graph model: receipt, continuation, lifetime and raw-window contracts passed');

const facetCalls=[];
const facetModel=createCodeGraphModel({isCurrent:()=>true,request:(ws,q,signal)=>new Promise(resolve=>facetCalls.push({ws,q,signal,resolve}))});
const bucket=(value,count=1)=>({value,count,unit:'file',exactness:'exact'});
const facetPage=(facets,next_cursor='facet-next')=>envelope({operation:'facets',hits:[],edges:[],facets,next_cursor,
  counts:[{name:'eligible',value:8586,unit:'file',exactness:'exact'}]});
const fq={operation:'facets',filter:{},budgets:{rows:50,tokens:8192}};
let page=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage({kinds:[bucket('file',8586)],folders:[bucket('a',3)]}));assert(await page);
assert.equal(facetModel.snapshot().operation,'facets');assert.equal(facetModel.snapshot().facet_returned,2);
page=facetModel.run('ws',fq,{more:true});assert.equal(facetCalls.at(-1).q.cursor,'facet-next');assert.deepEqual(facetCalls.at(-1).q.expected_receipt,receipt);
facetCalls.at(-1).resolve(facetPage({folders:[bucket('a',3),bucket('b',4)],languages:[bucket('python',200)]}));assert(await page);
assert.deepEqual(facetModel.snapshot().facets.folders.map(row=>[row.value,row.count]),[['a',3],['b',4]]);
assert.equal(facetModel.snapshot().facets.kinds[0].count,8586);assert.equal(facetModel.snapshot().counts[0].value,8586);
assert.equal(codeFacetCount(facetModel.snapshot().facets),4);assert.equal(facetModel.snapshot().nodes.length,0);
page=facetModel.run('ws',fq,{more:true});facetCalls.at(-1).resolve(facetPage({folders:[bucket(''),bucket('__proto__'),bucket('constructor')]},null));assert(await page);
assert.deepEqual(facetModel.snapshot().facets.folders.map(row=>row.value),['a','b','','__proto__','constructor']);
assert.equal(facetModel.snapshot().next_cursor,null);
page=facetModel.run('ws',{...fq,filter:{languages:['rust']}});facetCalls.at(-1).resolve(facetPage({languages:[bucket('rust',2)]}));assert(await page);
assert.equal(codeFacetCount(facetModel.snapshot().facets),1);assert.equal(facetModel.snapshot().facets.folders.length,0);
const pendingPage=facetModel.run('ws',{...fq,filter:{languages:['rust']}},{more:true}),pendingCall=facetCalls.at(-1);
const replacement=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage({states:[bucket('indexed',8586)]}));assert(await replacement);
pendingCall.resolve(facetPage({folders:[bucket('stale-private-folder')]}));assert.equal(await pendingPage,false);assert(pendingCall.signal.aborted);
assert.equal(facetModel.snapshot().facets.folders.length,0);
page=facetModel.run('ws',fq,{more:true});facetCalls.at(-1).resolve({...facetPage({folders:[bucket('other-head')]}),receipt:{...receipt,head_generation:'new'}});assert.equal(await page,false);
assert.equal(facetModel.snapshot().status,'stale');assert.deepEqual(facetModel.snapshot().facets,{});assert.equal(facetModel.snapshot().next_cursor,null);
for(const facets of [{unknown:[bucket('bad')]},{folders:{}},{folders:[bucket(7)]},{folders:[bucket('bad',-1)]},
  {folders:[bucket('bad',Infinity)]},{folders:[{...bucket('bad'),exactness:'maybe'}]},{folders:[{...bucket('bad'),count:null}]},{folders:[{...bucket('bad'),unit:''}]}]){
  page=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage(facets));assert.equal(await page,false);assert.equal(facetModel.snapshot().error.code,'invalid_evidence');
}
page=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage({folders:[{value:'unknown',count:null,unit:'file',exactness:'unknown'}]}));assert(await page);
page=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage({folders:Array.from({length:51},(_,i)=>bucket(String(i)))}));assert.equal(await page,false);assert.equal(facetModel.snapshot().error.code,'invalid_evidence');
page=facetModel.run('ws',fq);facetCalls.at(-1).resolve(facetPage({folders:[bucket('valid')]}));assert(await page);
page=facetModel.run('ws',fq,{more:true});const afterDispose=facetCalls.at(-1);facetModel.dispose();afterDispose.resolve(facetPage({folders:[bucket('disposed')]}));assert.equal(await page,false);
console.log('Code facet pages: deterministic axis/value merge, no summed counts, full8586 totals, scope/race/stale/validation gates passed');
