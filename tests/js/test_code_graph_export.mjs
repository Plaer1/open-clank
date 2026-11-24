import assert from 'node:assert/strict';
import {codeGraphExportSnapshot,codeGraphExportJSON,codeGraphExportHTML,codeGraphExportGraphML,codeGraphExportSVG,codeGraphExportCanvas,codeGraphExportCypher,codeGraphExportMarkdown} from '../../static/js/copal/codeGraphExport.js';
const input={authority:{variant:'published',receipt:{head_generation:'head-17',config_fingerprint:'config-1',workspace_id:'private-workspace'}},nodes:[{id:'/private/path',kind:'file',label:'/home/alice/private',source:'secret',owner:'alice'},{id:'x',kind:'symbol',label:'<script>alert(1)',parent:'/private/path'}],edges:[{from:'/private/path',to:'x',relation:'call',state:'structure',evidence:{text:'secret'}}]};
const data=codeGraphExportSnapshot(input,{presentation:'web',omittedNodes:3,incomplete:true});
const json=codeGraphExportJSON(data), html=codeGraphExportHTML(data);
assert.equal(data.receipt.headGeneration,'head-17');assert.equal(data.receipt.configFingerprint,'config-1');assert.equal(data.edges[0].state,'structure');assert.equal(data.omissions.remainingNodes,null);assert(!json.includes('private-workspace'));assert.equal(data.nodes.length,2);assert.equal(data.edges.length,1);assert.equal(data.omissions.nodes,3);
assert(!json.includes('/home/') && !json.includes('/private/') && !json.includes('secret') && !json.includes('alice'));
assert(!html.includes('<script>') && html.includes('&lt;script&gt;') && !html.includes('http://') && !html.includes('https://'));
assert.equal(json,codeGraphExportJSON(codeGraphExportSnapshot(input,{presentation:'web',omittedNodes:3,incomplete:true})));
console.log('allowlisted graph export, inert offline HTML and deterministic projection passed');

const xml=codeGraphExportGraphML(data);
assert(xml.includes('&lt;script&gt;') && !xml.includes('<script>') && !xml.includes('secret'));

const svg=codeGraphExportSVG(data);
assert(svg.includes('&lt;script&gt;') && !svg.includes('<script>') && !svg.includes('secret'));

const canvas=JSON.parse(codeGraphExportCanvas(data));assert.equal(canvas.nodes.length,2);assert.equal(canvas.edges.length,1);assert(!JSON.stringify(canvas).includes('secret'));
const cypher=codeGraphExportCypher(data);assert(cypher.includes('review before executing') && !cypher.includes('secret') && !cypher.includes('private-workspace'));

assert(canvas.nodes.some(node=>node.text.includes('\\<script\\>')));

const markdown=codeGraphExportMarkdown(data);assert(markdown.includes('not an endorsement') && markdown.includes('\\<script\\>') && !markdown.includes('secret'));
