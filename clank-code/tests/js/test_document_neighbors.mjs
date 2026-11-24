import assert from 'node:assert/strict';
import { databaseRelations } from '../../static/js/copal/notesModel.js';
import { prepareDocumentSave } from '../../static/js/copal/documentSave.js';
import { documentGraph } from '../../static/js/copal/graphModel.js';
import { documentNeighborhood } from '../../static/js/copal/documentNeighbors.js';

const docs = [
  {id:'source',name:'docs/source.md',kind:'note',head:'r1',text:''},
  {id:'target',name:'docs/target.md',kind:'note',head:'r2',text:''},
  {id:'a',name:'a.md',kind:'note',head:'r3',frontmatter:{aliases:['duplicate']}},
  {id:'b',name:'b.md',kind:'note',head:'r4',frontmatter:{aliases:['duplicate']}},
];
const text = '😀 [[target#Intro]]\n[relative](./target.md#Part)\n[reference][local]\n[local]: ./target.md#Ref\n[short]\n[short]: ./target.md#Other\n[[duplicate]]\n`[[ignored]]`\n```md\n[[hidden]]\n```\n![asset](./image.png)\n[external](https://example.com/x.md)';
const self = databaseRelations('[self](#Here)',docs,docs[0]);
assert.equal(self[0].targetDocumentId,'source');
const parsed = databaseRelations(text,docs,docs[0]);
assert.equal(parsed.length,5);
assert.equal(parsed.filter(r=>r.resolution==='unverified' && r.targetDocumentId===null).length,4);
assert(parsed.some(r=>r.fragment==='Other'));
assert.equal(parsed.find(r=>r.target==='duplicate').resolution,'ambiguous');
assert.equal(parsed.find(r=>r.fragment==='Part').sourceLine,2);
const explicit = {kind:'link',target:'fixed',targetDocumentId:'target',origin:'explicit'};
const saved = prepareDocumentSave({...docs[0],relations:[explicit]},text,{actionId:'save-1',relationsFor:t=>databaseRelations(t,docs,docs[0])});
assert.equal(saved.payload.relations[0].origin,'explicit');
docs[0].text=text; docs[0].relations=saved.payload.relations;
const graph = documentGraph(docs,()=>{throw new Error('must not use first-match fallback');});
assert(graph.notices.some(item=>item.reason==='unverified' && item.fragment==='Ref'));
assert.equal(graph.edges.length,1);
assert(graph.notices.some(item=>item.reason==='ambiguous'));
assert.equal(graph.coverage.complete,false);
const source = graph.nodes.find(node=>node.doc.id==='source').id;
const target = graph.nodes.find(node=>node.doc.id==='target').id;
const neighbors = documentNeighborhood(graph,source,{direction:'outgoing',kinds:['link'],depth:1});
assert(neighbors.nodes.some(node=>node.id===target));
assert(neighbors.notices.some(item=>item.reason==='ambiguous'));
assert.equal(neighbors.coverage.scope,'loaded');
console.log('document relations, ambiguity, neighbors and loaded coverage passed');
const verified = new Map([[JSON.stringify(['source','r1','./target.md#Ref']), {status:'resolved',targetDocumentId:'target',targetRevisionId:'r2'}]]);
const withProof = documentGraph(docs, null, verified);
assert(withProof.edges.some(edge => edge.fragment === 'Ref'));
docs[1].head = 'changed';
assert(documentGraph(docs, null, verified).notices.some(item => item.fragment === 'Ref' && item.reason === 'stale'));
