import assert from 'node:assert/strict';
import { anchorZoom,codePosition,assignCodePositions,capabilityFor,WORKBENCH_LIMITS,codeCanContinue } from '../../static/js/copal/codeGraphWorkbench.js';
for(const initial of [{x:0,y:0,scale:1},{x:-32,y:70,scale:2}]){
 const point={x:357,y:219},next=anchorZoom(initial,point,1.4);
 assert(Math.abs((point.x-initial.x)/initial.scale-(point.x-next.x)/next.scale)<1e-8);
 assert(Math.abs((point.y-initial.y)/initial.scale-(point.y-next.y)/next.scale)<1e-8);
}
assert.equal(anchorZoom({x:0,y:0,scale:1},{x:0,y:0},100).scale,4);
assert.equal(anchorZoom({x:0,y:0,scale:1},{x:0,y:0},.001).scale,.25);
assert.deepEqual(codePosition(0),{x:60,y:35});assert.notDeepEqual(codePosition(6),codePosition(0));
assert.equal(capabilityFor({},'path').available,false);
assert.equal(capabilityFor({operation_capabilities:[{operation:'path',available:true}]},'path').available,true);
assert.match(capabilityFor({operation_capabilities:[{operation:'path',available:false,reason:'unavailable'}]},'path').reason,/unavailable/);
assert.equal(WORKBENCH_LIMITS.mounted,200);assert.equal(WORKBENCH_LIMITS.loaded,1000);
assert.equal(WORKBENCH_LIMITS.mountedEdges,400);assert.equal(WORKBENCH_LIMITS.loadedEdges,1000);assert.equal(WORKBENCH_LIMITS.loadedPaths,100);
const positions=new Map([['retained',codePosition(1)]]);assignCodePositions([{id:'retained'},{id:'new'}],positions);
assert.deepEqual(positions.get('retained'),codePosition(1));assert.notDeepEqual(positions.get('retained'),positions.get('new'));
positions.set('collision',positions.get('retained'));assignCodePositions([{id:'retained'},{id:'collision'},{id:'new'}],positions);
assert.equal(new Set([...positions.values()].map(p=>JSON.stringify(p))).size,3);
console.log('S07 workbench math/capability contracts passed');
assert.equal(WORKBENCH_LIMITS.loadedFacetBuckets,1000);
const facetOnly={status:'ready',operation:'facets',nodes:[],edges:[],paths:[],next_cursor:'next',facets:{folders:[{value:'first'}]},counts:[{name:'eligible',value:8586}]};
assert.equal(codeCanContinue(facetOnly),true,'zero-hit facet pages retain continuation independent of full corpus count');
for(const total of [1000,1049])assert.equal(codeCanContinue({...facetOnly,facets:{folders:Array.from({length:total},(_,i)=>({value:String(i)}))}}),false);
assert.equal(codeCanContinue({...facetOnly,status:'loading'}),false);assert.equal(codeCanContinue({...facetOnly,next_cursor:null}),false);
assert.equal(codeCanContinue({...facetOnly,nodes:Array(1000)}),false);
assert.equal(codeCanContinue({...facetOnly,edges:Array(1000)}),false);
assert.equal(codeCanContinue({...facetOnly,paths:Array(100)}),false);
console.log('Code facet presentation: bounded bucket continuation and independent zero-hit Facets pages passed');
