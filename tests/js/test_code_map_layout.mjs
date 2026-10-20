import test from 'node:test';
import assert from 'node:assert/strict';
import {layoutCodeMap,createCodeMapLayoutOwner,validateLayoutInput} from '../../static/js/copal/codeMapLayout.js';
import {coldStructure} from '../fixtures/code_map_cold_structure.mjs';
const job=(serial=1,namespace='preview')=>({serial,namespace,nodes:[{id:'repo',kind:'repository',parent:null},{id:'a',kind:'folder',parent:'repo'},{id:'b',kind:'file',parent:'a'},{id:'c',kind:'file',parent:'a'},{id:'island',kind:'file',parent:null}],edges:[{from:'a',to:'b'},{from:'b',to:'c'}],previous:[],anchor:null,viewport:{width:1280,height:700}});
test('deterministic containment layout and real adjacency ordering; no fabricated edges',()=>{
 const a=layoutCodeMap(job()),b=layoutCodeMap({...job(),nodes:job().nodes.slice().reverse()});assert.deepEqual(a,b);assert.equal(a.positions.length,5);const p=new Map(a.positions.map(p=>[p.id,p]));assert(p.get('repo').x<p.get('a').x&&p.get('a').x<p.get('b').x);assert.equal(new Set(a.positions.map(p=>`${p.x},${p.y}`)).size,5);assert(!Object.hasOwn(a,'edges'));
});
test('same namespace expansion retains seed/selected anchor and places new node separately',()=>{
 const input=job(),first=layoutCodeMap(input);input.serial=2;input.previous=first.positions;input.anchor='b';input.nodes.push({id:'new',kind:'file',parent:'a'});input.edges.push({from:'b',to:'new'});const next=layoutCodeMap(input);for(const p of first.positions)assert.deepEqual(next.positions.find(n=>n.id===p.id),p);assert.equal(new Set(next.positions.map(p=>`${p.x},${p.y}`)).size,6);
});
function reportedColdShape(){
 const data=coldStructure();return {serial:1,namespace:'sanitized-native-cold-topology',nodes:data.nodes.map(({id,parent,kind})=>({id,parent,kind})),edges:data.edges.map(({from,to})=>({from,to})),previous:[],anchor:null,viewport:{width:1280,height:700}};
}
test('reported cold topology shape: 64 real-role nodes/63 containment edges occupies useful 2D space',()=>{
 const input=reportedColdShape(),saved=structuredClone(input),result=layoutCodeMap(input),{minX,maxX,minY,maxY}=result.bounds;
 assert.deepEqual(input,saved,'Layout cannot rewrite topology');assert.equal(result.positions.length,64);assert.equal(input.nodes.filter(n=>n.kind!=='file').length,22);assert.equal(input.edges.length,63);
 assert((maxX-minX)/(maxY-minY)>1&& (maxX-minX)/(maxY-minY)<3,'No narrow vertical column or extreme horizontal strip');
 assert(new Set(result.positions.map(p=>p.x)).size>=5);assert(new Set(result.positions.map(p=>p.y)).size>=5);
 const reverse={...input,nodes:input.nodes.slice().reverse(),edges:input.edges.slice().reverse()};assert.deepEqual(layoutCodeMap(reverse),result);
 for(let i=0;i<result.positions.length;i++)for(let j=i+1;j<result.positions.length;j++){const a=result.positions[i],b=result.positions[j];assert(Math.abs(a.x-b.x)>=190||Math.abs(a.y-b.y)>=64,'Reserved node/label footprints cannot overlap');}
});
test('paged branch expansion retains every seed including selected anchor and new relationships',()=>{
 const input=reportedColdShape(),first=layoutCodeMap(input),folder=input.nodes.find(n=>n.kind==='folder').id;input.serial++;input.previous=first.positions;input.anchor=folder;
 for(let i=0;i<40;i++){input.nodes.push({id:`child-${i}`,kind:'file',parent:folder});input.edges.push({from:folder,to:`child-${i}`});}
 const next=layoutCodeMap(input),p=new Map(next.positions.map(n=>[n.id,n]));for(const prior of first.positions)assert.deepEqual(p.get(prior.id),prior);
 assert.equal(next.positions.length,104);assert.equal(input.edges.length,103);assert.equal(new Set(next.positions.map(n=>`${n.x},${n.y}`)).size,104);
 for(const n of next.positions.filter(n=>n.id.startsWith('child-')))for(const old of first.positions)assert(Math.abs(n.x-old.x)>=190||Math.abs(n.y-old.y)>=64);
});
test('wide and disconnected forests stay bounded and deterministic without inventing hierarchy',()=>{
 for(const disconnected of [false,true]){
  const input={serial:1,namespace:'wide',nodes:[{id:'repo',kind:'repository',parent:null},...Array.from({length:1999},(_,i)=>({id:`n${String(i).padStart(4,'0')}`,parent:disconnected?null:'repo',kind:i%3?'file':'folder'}))],edges:[],previous:[],anchor:null,viewport:{width:1280,height:700}};
  const result=layoutCodeMap(input);assert.equal(result.positions.length,2000);assert.equal(new Set(result.positions.map(p=>`${p.x},${p.y}`)).size,2000);assert(result.bounds.maxX<20000&&result.bounds.maxY<20000);assert(!Object.hasOwn(result,'edges'));
 }
});
test('internal logical viewport changes sibling shelves; expansion still preserves every exact seed',()=>{
 const desktop=reportedColdShape(),wide=layoutCodeMap(desktop),mobile={...desktop,viewport:{width:155,height:198}},narrow=layoutCodeMap(mobile);assert(narrow.bounds.maxX<wide.bounds.maxX);assert(narrow.bounds.maxY>wide.bounds.maxY);const seeded=layoutCodeMap({...mobile,serial:2,previous:wide.positions,anchor:'cold-000'});assert.deepEqual(seeded.positions,wide.positions);
 for(const viewport of [null,{width:0,height:10},{width:8193,height:1},{width:NaN,height:1},{width:1,height:1,extra:true}])assert.throws(()=>layoutCodeMap({...desktop,viewport}));const missing={...desktop};delete missing.viewport;assert.throws(()=>layoutCodeMap(missing));
});
test('bounded input rejects oversized, cyclic, foreign endpoint and malformed seed',()=>{
 for(const mutate of [j=>j.nodes.push(j.nodes[0]),j=>j.nodes[0].body='source',j=>j.extra='unknown',j=>j.nodes[0].parent='b',j=>j.edges[0].to='foreign',j=>j.previous=[{id:'foreign',x:0,y:0}],j=>j.previous=[{id:'a',x:NaN,y:0}],j=>j.anchor='foreign',j=>j.namespace='x'.repeat(16385),j=>j.nodes=Array(2001).fill(j.nodes[0]),j=>j.edges=Array(5001).fill(j.edges[0])]){const j=job();mutate(j);assert.throws(()=>layoutCodeMap(j));}
 const large={serial:1,namespace:'bounded',nodes:Array.from({length:2000},(_,i)=>({id:`n${i}`,parent:i?`n${i-1}`:null,kind:'folder'})),edges:[],previous:[],anchor:null,viewport:{width:1280,height:700}};assert.equal(layoutCodeMap(large).positions.length,2000);assert.equal(validateLayoutInput(large),large);
});
test('persistent owner bounds to one in-flight plus one coalesced pending job',()=>{
 const sent=[],results=[],worker={postMessage:j=>sent.push(j),terminate(){this.stopped=true;}},owner=createCodeMapLayoutOwner({workerFactory:()=>worker,changed:(r,mode)=>results.push({r,mode})});
 owner.submit(job());owner.submit(job(2));owner.submit(job(3));assert.equal(sent.length,1);assert.deepEqual(owner.snapshot(),{disposed:false,worker:true,inflight:1,pending:1,latest:3,namespace:'preview'});
 worker.onmessage({data:layoutCodeMap(sent[0])});assert.equal(results.length,0);assert.equal(sent.length,2);assert.equal(sent[1].serial,3);worker.onmessage({data:layoutCodeMap(sent[1])});assert.equal(results.length,1);assert.equal(results[0].r.serial,3);assert.equal(results[0].mode,'worker');owner.dispose();assert(worker.stopped);assert.equal(owner.submit(job(4)),false);
});
test('publication/invalidation/close reject late result, and unavailable worker has honest fallback',()=>{
 const sent=[],results=[],worker={postMessage:j=>sent.push(j),terminate(){}},owner=createCodeMapLayoutOwner({workerFactory:()=>worker,changed:r=>results.push(r)});
 owner.submit(job());owner.invalidate();owner.submit(job(2,'published'));worker.onmessage({data:layoutCodeMap(sent[0])});assert.equal(results.length,0);worker.onmessage({data:layoutCodeMap(sent[1])});assert.equal(results[0].namespace,'published');const callback=worker.onmessage;owner.dispose();callback({data:layoutCodeMap(sent[1])});assert.equal(results.length,1);
 const fallback=[];const o=createCodeMapLayoutOwner({workerFactory:()=>{throw Error('No worker');},changed:(r,mode)=>fallback.push({r,mode})});o.submit(job());assert.equal(fallback[0].mode,'main-thread-fallback');o.dispose();
});
test('worker failure terminates old owner and computes only latest pending bounded job',()=>{
 const results=[],worker={postMessage(){},terminate(){this.stopped=true;}},owner=createCodeMapLayoutOwner({workerFactory:()=>worker,changed:(r,mode)=>results.push({r,mode})});owner.submit(job());owner.submit(job(2));worker.onerror();assert(worker.stopped);assert.equal(results.length,1);assert.equal(results[0].r.serial,2);assert.equal(results[0].mode,'main-thread-fallback');assert.equal(owner.snapshot().inflight,0);owner.dispose();
});
