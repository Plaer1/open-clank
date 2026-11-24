// Actual Notes consumer with synthetic handles only. Not normal-auth/full-app
// qualification, image readiness, popup geometry or performance evidence.
import assert from 'node:assert/strict';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

const page=`<!doctype html><meta charset="utf-8"><body><main id="app"></main>
<script type="module">
import {createNotesFeature} from '/static/js/copal/notesFeature.js';
import {createMarkdownEditor,createSourceEditor} from '/static/js/copal/codemirror.js';
import {configureCopalStorage} from '/static/js/copal/storage.js';
configureCopalStorage('synthetic-host-markdown-refresh');
const h=(tag,attrs={},...children)=>{const e=document.createElement(tag);for(const [k,v]of Object.entries(attrs)){if(k==='class')e.className=v;else if(k==='text')e.textContent=v;else if(k.startsWith('on')&&typeof v==='function')e.addEventListener(k.slice(2),v);else if(v!==false&&v!=null)e.setAttribute(k,String(v));}for(const c of children.flat())if(c!=null)e.append(c.nodeType?c:document.createTextNode(String(c)));return e;};
const state={docs:[],windows:new Map(),accountId:'fixture-account',workspace:'fixture-workspace',storageNamespace:'fixture',contextEpoch:1,selected:null,saveTimers:new Map(),noteEditors:new Set()};
const root=document.querySelector('#app'),body=document.createElement('section');root.append(body);state.windows.set('notes',{window:{root,body,setStatus(){}}});
let writes=0;
const feature=createNotesFeature({h,state,createMarkdownEditor,createSourceEditor,
 renderMarkdown:source=>h('article',{},source),renderPreview:source=>h('article',{},source),formatBaseCell:value=>String(value??''),
 api:async()=>({}),saveDocument:async()=>{writes++;throw Error('Unexpected Save');},saveResource:async()=>{writes++;throw Error('Unexpected Host Save');},
 renameNote:async()=>{},deleteDocument:async()=>{},showHistory(){},showTrash(){},showForm(){},importVault:async()=>{},loadDocuments:async()=>{},afterRender(){},
 openDocument(){},persistActiveContext(){},deleteDocuments:async()=>{},activateNotes(){},renderTimeline:()=>h('div'),openEventEditor(){},
});
window.refreshFixture={
 open(id,representation,revision='fp-1',edit=true){const name=id==='python'?'image.py':'image.md';return feature.openResource({
  key:{accountId:'fixture-account',workspaceId:'host',provider:'host',resourceId:id},revision:{kind:'hostFingerprint',value:revision},representation,
  capabilities:{read:true,edit},locator:{displayName:name,locationLabel:name,opaqueRef:'synthetic-'+id},
  metadata:{encoding:'utf-8',newline:'\\n',language:id==='python'?'python':'markdown',mode:representation==='markdown'?'markdown':'source'},
 },{kind:'text',name,text:id==='python'?'print(1)':'# Synthetic Markdown\\n\\n![generated](./still.png)\\n'});},
 snapshot(){const title=[...document.querySelectorAll('.copal-inline-title')].find(e=>e.textContent==='image.md'),leaf=title?.closest('.copal-note-leaf'),doc=state.docs.find(d=>d.resource?.key.resourceId==='markdown');return{
  kind:doc?.kind,representation:doc?.resource.representation,revision:doc?.resourceSnapshot.expectedRevision.value,readOnly:doc?.readOnly,savePolicy:doc?.savePolicy,
  commands:leaf?[...leaf.querySelectorAll('.copal-leaf-menu button')].map(e=>e.textContent.trim()):[],mode:leaf?.querySelector('.copal-leaf-mode')?.textContent,
  editable:Boolean(leaf?.querySelector('.cm-content[contenteditable="true"]')),
  owners:state.docs.filter(d=>d.resource?.key.resourceId==='markdown').length,writes,
 };},
};window.ready=true;
</script>`;

await withCopalBrowser({page},async({evaluate,until})=>{
 await until('window.ready');
 await evaluate("window.refreshFixture.open('markdown','markdown')");
 await until("window.refreshFixture.snapshot().commands.includes('Live Preview')");
 const before=await evaluate('window.refreshFixture.snapshot()');assert.equal(before.kind,'markdown');assert.equal(before.owners,1);
 await evaluate("window.refreshFixture.open('python','text')");
 await evaluate("window.refreshFixture.open('markdown','markdown','fp-2')");
 await until("window.refreshFixture.snapshot().commands.includes('Live Preview')");
 const repeated=await evaluate('window.refreshFixture.snapshot()');assert.equal(repeated.kind,'markdown');assert.equal(repeated.representation,'markdown');assert.equal(repeated.revision,'fp-2');assert.equal(repeated.owners,1);assert.equal(repeated.savePolicy,'explicit');assert.equal(repeated.writes,0);
 await evaluate("window.refreshFixture.open('markdown','text','fp-3')");
 await until("window.refreshFixture.snapshot().kind==='text'&&!window.refreshFixture.snapshot().commands.includes('Live Preview')");
 assert.equal((await evaluate('window.refreshFixture.snapshot()')).mode,'Source');
 await evaluate("window.refreshFixture.open('markdown','markdown','fp-4')");
 await until("window.refreshFixture.snapshot().commands.includes('Live Preview')");
 const returned=await evaluate('window.refreshFixture.snapshot()');assert.equal(returned.kind,'markdown');assert.equal(returned.readOnly,false);assert.equal(returned.revision,'fp-4');assert.equal(returned.owners,1);assert.equal(returned.writes,0);
 await evaluate("window.refreshFixture.open('markdown','markdown','fp-5',false)");
 await until("window.refreshFixture.snapshot().mode==='Read only'");
 const readonly=await evaluate('window.refreshFixture.snapshot()');assert.equal(readonly.kind,'markdown');assert.equal(readonly.representation,'markdown');assert.equal(readonly.readOnly,true);assert.equal(readonly.revision,'fp-5');assert.equal(readonly.editable,false);assert(!readonly.commands.includes('Live Preview'));assert.equal(readonly.writes,0);
});
console.log('Host Markdown clean-refresh actual-consumer regression passed');
