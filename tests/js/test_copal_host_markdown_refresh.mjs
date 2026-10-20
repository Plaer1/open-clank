// Execute the exact production ingress/cancel closures with real resource
// normalization and buffers. DOM rendering remains a separate browser gate.
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import {normalizeResourceHandle,sameResourceKey,snapshotEnvelope,cloneEnvelope} from '../../static/js/copal/resourceModel.js';
import {createBufferRegistry} from '../../static/js/copal/documentBuffers.js';

const source=fs.readFileSync(new URL('../../static/js/copal/notesFeature.js',import.meta.url),'utf8');
function closure(name,next) {
 const start=source.indexOf(`  ${name}`),end=source.indexOf(`\n  ${next}`,start);
 assert(start>=0&&end>start,'Exact production closure markers required');return source.slice(start,end);
}
const ingress=closure('function openResource(','function hostHistoryResourceForDocument(');
const resolver=closure('async function resolveDirtyDocuments(','async function reloadDocument(');
const modeExpression=/const mode = (doc\.sourceKind === 'host'[^\n]+);/.exec(source)?.[1];assert(modeExpression);
const label=(doc,mode='live')=>new Function('doc','leaf',`return ${modeExpression}`)(doc,{mode});
const resource=(id='image',representation='markdown',revision='fp-1',edit=true)=>({
 key:{accountId:'synthetic-owner',workspaceId:'host',provider:'host',resourceId:id},
 revision:{kind:'hostFingerprint',value:revision},representation,
 locator:{displayName:id==='python'?'image.py':'image.md',locationLabel:'synthetic',opaqueRef:'opaque-'+id},
 capabilities:{read:true,edit},metadata:{encoding:'utf-8',newline:'\n',mode:representation==='markdown'?'markdown':'source',language:representation==='markdown'?'markdown':'python'},
});
function setup() {
 const state={docs:[],windows:new Map()},current={noteBuffers:new Map(),noteDrafts:new Map(),noteRenderVersion:0},opens=[],effects=[];
 state.windows.set('notes',current);
 const forbidden=()=>{throw Error('Unexpected write/save/discard side effect');};
 const deps={state,context:()=>current,normalizeResourceHandle,sameResourceKey,snapshotEnvelope,cloneEnvelope,
  open:(id,options)=>{opens.push({id,options});},
  currentScope:()=> 'synthetic-scope',transitionRevision:(_c,id)=>current.noteBuffers.get(id)?.localRevision||0,
  workspaceDocuments:()=>state.docs,styledConfirm:async(_message,options)=>{effects.push(options);return false;},
  flushPendingSourceEdits:forbidden,saveDraft:forbidden,ownsSessionDocuments:()=>false,workspaceLeaves:()=>[],
 };
 const factory=new Function('deps',`const {${Object.keys(deps).join(',')}}=deps;${ingress}\n${resolver}\nreturn {openResource,resolveDirtyDocuments};`);
 return {state,current,opens,effects,...factory(deps)};
}

test('clean repeated Host Markdown open ignores generic payload kind text',()=>{
 const f=setup(),first=resource(),id=f.openResource(first,{kind:'text',name:'image.md',text:'# image'}),doc=f.state.docs[0];
 assert.equal(doc.kind,'markdown');assert.equal(label(doc),'Live Preview');const identity=doc.id;
 f.openResource(resource('python','text'),{kind:'text',name:'image.py',text:'print(1)'});
 f.openResource(resource('image','markdown','fp-2',false),{kind:'text',name:'image.md',text:'# refreshed',properties:{test:true}});
 assert.equal(f.state.docs.length,2);assert.equal(f.state.docs[0],doc);assert.equal(doc.id,identity);assert.equal(id,identity);assert.equal(doc.kind,'markdown');assert.equal(label(doc),'Live Preview');
 assert.equal(doc.text,'# refreshed');assert.equal(doc.readOnly,true);assert.equal(doc.savePolicy,'explicit');assert.equal(doc.resourceSnapshot.expectedRevision.value,'fp-2');assert.equal(doc.resource.key.resourceId,'image');assert.equal(doc.sourceMetadata.mode,'markdown');assert.deepEqual(doc.properties,{test:true});assert.equal(f.current.noteRenderVersion,3);
});

test('clean text ↔ Markdown representation transitions control actual mode condition',()=>{
 const f=setup();f.openResource(resource('image','text'),{kind:'markdown',name:'pretend.md',text:'plain'});const doc=f.state.docs[0];
 assert.equal(doc.kind,'text','Name and payload kind cannot opt source into Markdown');assert.equal(label(doc),'Source');
 f.openResource(resource('image','markdown','fp-2'),{kind:'text',name:'image.md',text:'# real markdown'});assert.equal(doc.kind,'markdown');assert.equal(label(doc),'Live Preview');assert.equal(doc.resource.representation,'markdown');
 f.openResource(resource('image','text','fp-3'),{kind:'markdown',name:'still.md',text:'source only'});assert.equal(doc.kind,'text');assert.equal(label(doc),'Source');assert.equal(doc.resource.representation,'text');assert.equal(f.state.docs.length,1);
});

test('clean base/wiki ingress preserves existing representation mapping on refresh',()=>{
 for(const [representation,kind]of [['base','base'],['wikiArticle','wiki'],['meme','wiki'],['nativeNote','text']]){
  const f=setup();f.openResource(resource('image',representation),{kind:'text',text:'one'});const doc=f.state.docs[0];assert.equal(doc.kind,kind);
  f.openResource(resource('image',representation,'fp-2'),{kind:'text',text:'two'});assert.equal(doc.kind,kind);assert.equal(doc.resourceSnapshot.expectedRevision.value,'fp-2');
 }
});

test('dirty buffer reopen focuses only; Cancel retains draft, CAS, representation and undo',async()=>{
 const f=setup(),handle=normalizeResourceHandle(resource()),id=f.openResource(handle,{kind:'text',name:'image.md',text:'# saved'}),doc=f.state.docs[0];
 const registry=createBufferRegistry({storage:null});const buffer=registry.acquire(handle,{text:'# saved'},{save:async()=>{throw Error('Reopen must never Save');}});
 buffer.apply({text:'# unsaved'});f.current.noteBuffers.set(id,buffer);const before=structuredClone(doc),revision=f.current.noteRenderVersion,localRevision=buffer.localRevision;
 f.openResource(resource('image','text','fp-99',false),{kind:'text',name:'changed.py',text:'replacement'});
 assert.deepEqual(doc,before);assert.equal(f.current.noteRenderVersion,revision);assert.deepEqual(f.opens.at(-1),{id,options:{reuse:true}});assert.equal(buffer.state().dirty,true);assert.equal(buffer.localRevision,localRevision);assert.equal(buffer.snapshot().expectedRevision.value,'fp-1');assert.equal(label(doc),'Live Preview');
 assert.equal(await f.resolveDirtyDocuments([id],{force:true,closingWindow:true}),false);assert.equal(f.effects.length,1);assert.equal(f.effects[0].cancelText,'Cancel');assert.equal(f.effects[0].confirmText,'Save');assert.equal(f.effects[0].alternateText,'Discard changes');assert.equal(buffer.envelope.text,'# unsaved');assert.deepEqual(doc,before);
 assert.equal(buffer.undo().envelope.text,'# saved');assert.equal(buffer.redo().envelope.text,'# unsaved');
});

test('recovery draft alone protects dirty focus-only behavior',()=>{
 const f=setup(),id=f.openResource(resource(),{kind:'text',text:'# saved'}),doc=f.state.docs[0];f.current.noteDrafts.set(id,{value:'# recovered'});const before=structuredClone(doc),version=f.current.noteRenderVersion;
 f.openResource(resource('image','text','fp-new'),{kind:'text',text:'remote'});assert.deepEqual(doc,before);assert.equal(f.current.noteRenderVersion,version);assert.equal(f.current.noteDrafts.get(id).value,'# recovered');assert.equal(f.opens.at(-1).id,id);
});

test('unsupported representation is refused before mutating existing document',()=>{
 const f=setup();f.openResource(resource(),{kind:'text',text:'# saved'});const before=structuredClone(f.state.docs);assert.throws(()=>f.openResource(resource('image','arbitrary'),{kind:'markdown',text:'bad'}),/unsupported representation/);assert.deepEqual(f.state.docs,before);
});
