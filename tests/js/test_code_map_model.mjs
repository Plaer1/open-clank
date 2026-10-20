import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createCodeMapModel,validateMapResult,mapRequest,codeMapKey} from '../../static/js/copal/codeMapModel.js';
const folder=new URL('../../mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/',import.meta.url);
const load=name=>JSON.parse(fs.readFileSync(new URL(name,folder),'utf8'));
const preview=()=>measure(load('code_map_preview.json'));
const published=()=>measure(load('code_map_published.json'));
function measure(v){for(let i=0;i<8;i++){const n=Buffer.byteLength(JSON.stringify(v));if(v.cost.serialized_bytes===n)return v;v.cost.serialized_bytes=n;}throw Error('measure');}
const overview={schema:'open-clank.code-map/v1',operation:'overview'};
function harness(options={}){let scope={account:'synthetic',workspace:'fixture-workspace'},queue=[],calls=[];const model=createCodeMapModel({request:(w,r,s)=>{calls.push({w,r,s});const next=queue.shift();return typeof next==='function'?next():structuredClone(next);},getScope:()=>scope,...options});return{model,calls,queue,setScope:v=>scope=v};}
function neighbor(v){v.operation='neighbors';v.root=v.nodes[1].id;v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;return measure(v);}
function nr(v){return{...overview,operation:'neighbors',root:{kind:'visual',id:v.root},expected_authority:v.authority};}
test('strict canonical DTO goldens, repository overview anchor and no evidence preview',()=>{
 for(const v of [preview(),published()])assert.deepEqual(validateMapResult(v,overview),v);
 const v=preview();v.root=v.nodes[0].id;v.nodes[0].kind='repository';assert.doesNotThrow(()=>validateMapResult(measure(v),overview));
 for(const mutate of [v=>v.extra='private',v=>v.nodes[0].body='source',v=>v.authority.receipt={},v=>v.coverage.indexed_files=0,v=>v.edges[0].state='exact',v=>v.selection={receipt:{}},v=>v.cost.source_bytes_read=1,v=>v.page.returned_nodes++,v=>v.nodes[0].label='x'.repeat(161),v=>v.root=v.nodes[1].id,v=>v.nodes[0].parent=v.nodes[1].id]){const x=preview();mutate(x);assert.throws(()=>validateMapResult(measure(x),overview));}
 assert.throws(()=>mapRequest({...overview,owner:'no'}));assert.throws(()=>mapRequest({...overview,limits:null}));assert.throws(()=>mapRequest({...overview,limits:{nodes:201}}));assert.throws(()=>mapRequest({...overview,operation:'neighbors'}));
});
test('normal request copy, preview nonactionability, strict workspace ownership',async()=>{
 const h=harness(),v=preview();h.queue.push(v);assert(await h.model.run('fixture-workspace',overview));assert.equal(h.model.snapshot().evidenceEnabled,false);assert.equal(await h.model.selectAggregate('anything'),false);
 assert.deepEqual(h.calls[0].r,{mode:'map',request:mapRequest(overview)});assert(h.calls[0].s instanceof AbortSignal);
 h.queue.push(v);assert.equal(await h.model.run('another-workspace',overview),false);assert.equal(h.model.snapshot().nodes.length,0);
});
test('preview to published atomically replaces namespace; snapshots cannot mutate state',async()=>{
 const h=harness();h.queue.push(preview());await h.model.run('fixture-workspace',overview);const p=published();p.nodes.forEach(n=>{n.id='published-'+n.id;if(n.parent)n.parent='published-'+n.parent;});p.edges.forEach(e=>{e.from='published-'+e.from;e.to='published-'+e.to;});h.queue.push(measure(p));assert(await h.model.run('fixture-workspace',overview));const s=h.model.snapshot();assert(s.nodes.every(n=>n.id.startsWith('published-')));assert(s.evidenceEnabled);s.nodes.length=0;assert(h.model.snapshot().nodes.length>0);
});
test('abort, serial, external scope, close and late completion fences',async()=>{
 const h=harness();let release;h.queue.push(()=>new Promise(r=>release=r));const old=h.model.run('fixture-workspace',overview);h.queue.push(published());await h.model.run('fixture-workspace',overview);assert(h.calls[0].s.aborted);release(preview());assert.equal(await old,false);assert.equal(h.model.snapshot().authority.variant,'published');
 h.queue.push(()=>new Promise(r=>release=r));const pending=h.model.run('fixture-workspace',overview);h.setScope({account:'other'});release(preview());assert.equal(await pending,false);assert.equal(h.model.snapshot().nodes.length,0);
 h.queue.push(()=>new Promise(r=>release=r));const last=h.model.run('fixture-workspace',overview);h.model.dispose();release(preview());assert.equal(await last,false);assert.equal(h.model.snapshot().authority,null);assert.equal(await h.model.run('fixture-workspace',overview),false);
});
test('advancing pages repeat exact endpoints without sums; complete only exact adjacency',async()=>{
 const h=harness(),v=neighbor(published()),r=nr(v),first=structuredClone(v);first.page={...first.page,complete:false,next_cursor:'page-two',remaining_nodes:1};h.queue.push(measure(first));assert(await h.model.run('fixture-workspace',r));assert.equal(h.model.hasCompleteAdjacency(r),false);
 const second=structuredClone(v);h.queue.push(measure(second));assert(await h.model.run('fixture-workspace',r,{more:true}));assert.equal(h.model.snapshot().nodes.length,v.nodes.length);assert.equal(h.model.snapshot().edges[0].count,v.edges[0].count);assert(h.model.hasCompleteAdjacency(r));assert.equal(h.model.hasCompleteAdjacency({...r,direction:'outgoing'}),false);assert.equal(h.model.hasCompleteAdjacency({...r,root:{kind:'visual',id:v.nodes[0].id}}),false);assert.equal(h.calls[1].r.request.cursor,'page-two');assert.deepEqual(h.calls[1].r.request.expected_authority,v.authority);
});
test('changed root, row, authority, duplicate cursor and stale query continuation refuse',async()=>{
 for(const mutate of [v=>v.authority.summary_id='other',v=>v.nodes[0].label='changed',v=>v.root='missing',v=>{v.page.complete=false;v.page.next_cursor='same';v.page.remaining_nodes=1;}]){
  const h=harness(),v=neighbor(published()),r=nr(v),first=structuredClone(v);first.page={...first.page,complete:false,next_cursor:'same',remaining_nodes:1};h.queue.push(measure(first));await h.model.run('fixture-workspace',r);const bad=structuredClone(v);mutate(bad);h.queue.push(measure(bad));assert.equal(await h.model.run('fixture-workspace',r,{more:true}),false);assert.equal(h.model.snapshot().nodes.length,0);
 }
 const h=harness();assert.equal(await h.model.run('fixture-workspace',overview,{more:true}),false);
});
test('known denial/error clears authority and geometry rather than retaining actions',async()=>{
 const h=harness();h.queue.push(published());await h.model.run('fixture-workspace',overview);h.queue.push({schema:overview.schema,mode:'map',error:{code:'erased',message:'Erased'},cost:{}});assert.equal(await h.model.run('fixture-workspace',overview),false);assert.equal(h.model.snapshot().authority,null);assert.equal(h.model.snapshot().nodes.length,0);assert.equal(h.model.snapshot().evidenceEnabled,false);
 h.model.reset();assert.equal(h.model.snapshot().status,'idle');
});
test('cache ceilings retain selected root and endpoints, eviction clears proof',async()=>{
 const v=neighbor(published()),h=harness({maxNodes:3,maxEdges:1,maxBytes:16384}),r=nr(v);h.queue.push(v);assert(await h.model.run('fixture-workspace',r));const s=h.model.snapshot();assert(s.nodes.some(n=>n.id===v.root));assert(s.nodes.length<=3&&s.edges.length<=1&&s.retainedBytes<=16384);for(const e of s.edges)assert(s.nodes.some(n=>n.id===e.from)&&s.nodes.some(n=>n.id===e.to));if(s.evicted)assert.equal(h.model.hasCompleteAdjacency(r),false);
 const small=harness({maxBytes:512});small.queue.push(preview());assert.equal(await small.model.run('fixture-workspace',overview),false);assert.equal(small.model.snapshot().nodes.length,0);
});
test('aggregate selection preserves original receipt, rejects preview/forged/source refs',async()=>{
 const h=harness(),v=published();h.queue.push(v);await h.model.run('fixture-workspace',overview);const selection=load('code_map_selection.json');assert.deepEqual(selection.authority,v.authority);h.queue.push(measure(selection));assert(await h.model.selectAggregate(selection.selection_id));assert.deepEqual(h.model.snapshot().aggregate.receipt,v.authority.receipt);assert.equal(h.calls.at(-1).r.mode,'selection');
 for(const mutate of [s=>s.receipt.head_generation='forged',s=>s.references[0].body='source',s=>s.references[0].occurrence_id='visual-id',s=>s.references.push(s.references[0]),s=>s.cost.source_bytes_read=1]){const x=harness();x.queue.push(v);await x.model.run('fixture-workspace',overview);const s=structuredClone(selection);mutate(s);x.queue.push(measure(s));assert.equal(await x.model.selectAggregate(selection.selection_id),false);assert.equal(x.model.snapshot().aggregate,null);assert.equal(x.model.snapshot().authority,null);}
 assert.equal(await h.model.selectAggregate(selection.selection_id,{cursor:'unissued'}),false);
});
test('aggregate continuation is receipt-bound, disjoint and bounded to 50 original refs',async()=>{
 const h=harness(),v=published(),s=load('code_map_selection.json');h.queue.push(v);await h.model.run('fixture-workspace',overview);
 const ref=s.references[0];s.references=Array.from({length:49},(_,i)=>({...ref,occurrence_id:`occ:${(i+1).toString(16)}`}));s.complete=false;s.remaining=2;s.next_cursor='next';h.queue.push(measure(s));assert(await h.model.selectAggregate(s.selection_id));
 const next=structuredClone(s);next.references=[{...ref,occurrence_id:'occ:ff'}];next.remaining=1;next.next_cursor='third';h.queue.push(measure(next));assert(await h.model.selectAggregate(s.selection_id,{cursor:'next'}));assert.equal(h.calls.at(-1).r.request.max_refs,1);assert.equal(h.model.snapshot().aggregate.references.length,50);assert.equal(h.model.snapshot().aggregate.clientLimited,true);const count=h.calls.length;assert.equal(await h.model.selectAggregate(s.selection_id,{cursor:'third'}),false);assert.equal(h.calls.length,count);
});
test('stale scope cannot launch an aggregate request; isCurrent removes actionable snapshot',async()=>{
 const h=harness(),v=published();h.queue.push(v);await h.model.run('fixture-workspace',overview);h.setScope({account:'other'});assert.equal(await h.model.selectAggregate(v.edges.find(e=>e.selection_id).selection_id),false);assert.equal(h.calls.length,1);assert.equal(h.model.snapshot().authority,null);
 let current=true;const x=harness({isCurrent:()=>current});x.queue.push(v);await x.model.run('fixture-workspace',overview);current=false;assert.equal(x.model.snapshot().nodes.length,0);
});
test('later page cannot complete with only repeated rows and a new cursor',async()=>{
 const h=harness(),v=preview();v.page={...v.page,complete:false,next_cursor:'one',remaining_nodes:1};h.queue.push(measure(v));await h.model.run('fixture-workspace',overview);v.page.next_cursor='two';h.queue.push(measure(v));assert.equal(await h.model.run('fixture-workspace',overview,{more:true}),false);assert.equal(h.model.snapshot().authority,null);
});
test('canonical keys ignore object insertion order but bind every authority field',()=>{
 assert.equal(codeMapKey({a:1,b:2}),codeMapKey({b:2,a:1}));assert.notEqual(codeMapKey(published().authority),codeMapKey({...published().authority,summary_id:'different'}));
});
test('actual 2000-node and 5000-edge retained ceilings evict without false completeness',async()=>{
 const h=harness();
 for(let page=0;page<11;page++){
  const v=published(),base=v.nodes.find(n=>n.kind==='file');v.root=null;v.selection=null;
  v.nodes=Array.from({length:200},(_,i)=>({...base,id:`node-${page}-${i}`,parent:null}));v.edges=[];v.page={returned_nodes:200,returned_edges:0,remaining_nodes:0,remaining_edges:0,complete:true,next_cursor:null,truncated:false};
  h.queue.push(measure(v));assert(await h.model.run('fixture-workspace',overview));assert(h.model.snapshot().nodes.length<=2000);
 }
 assert.equal(h.model.snapshot().nodes.length,2000);assert(h.model.snapshot().evicted);
 const x=harness();for(let page=0;page<7;page++){
  const v=published(),edge=v.edges.find(e=>e.relation!=='containment');v.edges=Array.from({length:800},(_,i)=>({...edge,id:`edge-${page}-${i}`}));v.page.returned_edges=800;
  x.queue.push(measure(v));assert(await x.model.run('fixture-workspace',overview));assert(x.model.snapshot().edges.length<=5000);
 }
 assert.equal(x.model.snapshot().edges.length,5000);assert(x.model.snapshot().evicted);assert(x.model.snapshot().retainedBytes<=4*1024*1024);
});
test('node pressure preserves the fresh page relationships and selected endpoints',async()=>{
 const h=harness({maxNodes:4}),first=published();h.queue.push(first);await h.model.run('fixture-workspace',overview);
 const next=published();next.nodes.forEach(n=>{n.id='new-'+n.id;if(n.parent)n.parent='new-'+n.parent;});next.edges.forEach(e=>{e.id='new-'+e.id;e.from='new-'+e.from;e.to='new-'+e.to;});
 h.queue.push(measure(next));assert(await h.model.run('fixture-workspace',overview));const s=h.model.snapshot();assert.equal(s.nodes.length,4);for(const e of next.edges)assert(s.edges.some(retained=>retained.id===e.id));assert(s.evicted);
});
test('finite app error codes survive with fixed public text; arbitrary fields/codes refuse',async()=>{
 const allowed=['invalid','invalid_schema','denied','erased','missing','stale_cursor','reindex_required','budget_exceeded','busy','cancelled','unsupported','unavailable','invalid_evidence'];
 for(const code of allowed){const h=harness();h.queue.push(published());await h.model.run('fixture-workspace',overview);const error=measure({schema:overview.schema,mode:'map',error:{code,message:'arbitrary server text not for display'},cost:published().cost});h.queue.push(error);assert.equal(await h.model.run('fixture-workspace',overview),false);const s=h.model.snapshot();assert.equal(s.error.code,code);assert(!JSON.stringify(s).includes('arbitrary'));assert.equal(s.authority,null);assert.equal(s.nodes.length,0);}
 for(const mutate of [v=>v.error.code='secret-code',v=>v.error.path='/private',v=>v.authority=published().authority]){const h=harness(),v={schema:overview.schema,mode:'map',error:{code:'denied',message:'not displayed'},cost:published().cost};mutate(v);h.queue.push(measure(v));await h.model.run('fixture-workspace',overview);assert.equal(h.model.snapshot().error.code,'invalid_or_unavailable');}
});
test('scope is bounded before dispatch and fully charged to retained bytes',async()=>{
 const large=harness();large.setScope({account:'x'.repeat(16384)});assert.equal(await large.model.run('fixture-workspace',overview),false);assert.equal(large.calls.length,0);
 const circular={};circular.self=circular;large.setScope(circular);assert.equal(await large.model.run('fixture-workspace',overview),false);
 const a=harness(),b=harness();b.setScope({account:'x'.repeat(1000),workspace:'fixture-workspace'});for(const h of [a,b]){h.queue.push(preview());assert(await h.model.run('fixture-workspace',overview));}
 assert(b.model.snapshot().retainedBytes-a.model.snapshot().retainedBytes>=1900);
 const small=harness({maxBytes:4096});small.setScope({account:'x'.repeat(3000)});small.queue.push(preview());assert.equal(await small.model.run('fixture-workspace',overview),false);assert.equal(small.model.snapshot().authority,null);
});
