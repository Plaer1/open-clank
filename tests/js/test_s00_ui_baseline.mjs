import assert from 'node:assert/strict';
import test from 'node:test';
import {createHash} from 'node:crypto';
import {inflateSync} from 'node:zlib';
import fs from 'node:fs';
import path from 'node:path';
import {Script} from 'node:vm';
import {distribution,measurementClassification,installBrowserRecorder,browserSurfaceExposure,usefulView,proveNativeRelationships,validateUsefulSnapshots,validatePartialColdResume,validateCompletedColdResume,validateCompletedWarmResume,syntheticFixtureAncestor,verifySyntheticFavorite,syntheticPreviewRow,asyncBrowserStatements,validateCaptureConfig,stageDurations,mediaStageDurations,browserMediaMilestones,S00_SCENARIOS,browserGraphObservation} from '../helpers/s00_ui_baseline_contract.mjs';
import {GOLDEN,queryFixture,syntheticPng,deriveColdCorpora,readNativeGolden,NATIVE_GOLDEN_SHA256} from '../fixtures/s00_ui_fixture.mjs';

const good=()=>({groups:GOLDEN.groups,edges:GOLDEN.edges,graphHeightFraction:0.72,labels:7,minimumLabelPx:12,clippedLabels:0,overlapFraction:0,allExpectedVisible:true});
test('contended and unclassified results cannot claim controlled performance qualification',()=>{
  for(const value of [undefined,'diagnostic-contended']){const result=measurementClassification(value);assert.equal(result.performanceQualified,false);assert.equal(result.latencyQualified,false);assert.equal(result.gpuQualified,false);assert.match(result.measurementClass,/diagnostic/);}
  assert.equal(measurementClassification('diagnostic-contended').measurementClass,'diagnostic-contended');
  assert.throws(()=>measurementClassification('controlled-baseline'));assert.throws(()=>measurementClassification('arbitrary caption'));
  const schedule={schema:'openclank.s00-ui-schedule/v1',rootApproved:true,noOwnedCompilerOrNativeBenchmark:true,runName:'headed-ui-08',observationKind:'media-only',launchSha256:'a'.repeat(64),notBeforeMs:Date.now(),expiresAtMs:Date.now()+60000};
  const scheduled=measurementClassification('controlled-baseline-scheduled',schedule);assert.equal(scheduled.measurementClass,'controlled-baseline-scheduled');assert.equal(scheduled.performanceQualified,false);assert.equal(scheduled.latencyQualified,false);assert.equal(scheduled.gpuQualified,false);
  for(const patch of [{rootApproved:false},{noOwnedCompilerOrNativeBenchmark:false},{runName:'headed-ui-07'},{expiresAtMs:0},{notBeforeMs:Date.now()+60000},{notBeforeMs:Date.now()-600001},{observationKind:'cold-index'}])assert.throws(()=>measurementClassification('controlled-baseline-scheduled',{...schedule,...patch}));assert.throws(()=>measurementClassification('controlled-baseline-scheduled'));
});
test('all actual warm/media statement snippets parse as ordinary async-IIFE scripts without execution',()=>{
  const source=fs.readFileSync(new URL('../../.clanker/tools/graph-performance-20261015/s00_ui_driver.mjs',import.meta.url),'utf8');
  const snippets=[...source.matchAll(/statements\(`([^`]+)`\)/g)].map(m=>m[1]);assert.equal(snippets.length,9,'Review newly added statement producer');
  for(const raw of snippets){const body=raw.replaceAll('${JSON.stringify(directory)}',JSON.stringify('files-480')).replaceAll('${JSON.stringify(path.basename(fixture.root))}',JSON.stringify('synthetic-root')).replaceAll('${JSON.stringify(mediaWorkspace)}',JSON.stringify('synthetic-workspace')).replaceAll('${syntheticFixtureAncestor}',syntheticFixtureAncestor.toString()).replaceAll('${verifySyntheticFavorite}',verifySyntheticFavorite.toString()).replaceAll('${count}','480');assert(!body.includes('${'),'Unbound test fixture template');try{new Script(asyncBrowserStatements(body));}catch{throw Error('S00 async statement syntax preflight failed; expression omitted');}}
  assert.throws(()=>new Script("(await import('/static/js/copal.js')).default.close('graph')"),SyntaxError);
  assert(source.includes('new Script(expression)'),'Every constructed browser expression is syntax-checked without evaluating it');
});
test('actual recorder uses callback clock for DOM mutation, not earlier rAF timestamp',()=>{
  const names=['window','document','performance','Element','MutationObserver','requestAnimationFrame','cancelAnimationFrame'];const saved=Object.fromEntries(names.map(n=>[n,globalThis[n]]));let now=100,raf,mutate;const listeners={};
  class Target{closest(){return this;}}
  try{globalThis.window={};globalThis.document={querySelector:()=>null,body:{},addEventListener:(n,f)=>{listeners[n]=f;}};globalThis.performance={now:()=>now};globalThis.Element=Target;globalThis.MutationObserver=class{constructor(f){mutate=f;}observe(){}disconnect(){}};globalThis.requestAnimationFrame=f=>(raf=f,1);globalThis.cancelAnimationFrame=()=>{};
    installBrowserRecorder(()=>null,()=>({}),()=>({passed:false}),{},'files-640');now=110;listeners.wheel({isTrusted:true,target:new Target()});mutate();now=112;raf(105);
    assert.deepEqual(window.__s00Baseline.snapshot().inputToMutationAtFrameMs,[2]);assert.deepEqual(window.__s00Baseline.snapshot().activeFrameIntervalsMs,[5]);window.__s00Baseline.stop();
  }finally{for(const [n,v]of Object.entries(saved)){if(v===undefined)delete globalThis[n];else globalThis[n]=v;}}
});
test('arranged exposure rejects resident but covered, hidden or offscreen applets',()=>{
 const names=['document','getComputedStyle','innerWidth','innerHeight'],saved=Object.fromEntries(names.map(n=>[n,globalThis[n]]));let hidden=false,covered=false,offscreen=false;const point={};const content={getBoundingClientRect:()=>({x:offscreen?200:0,y:0,width:100,height:100})};const root={querySelector:()=>content,closest:()=>hidden?{}:null,contains:e=>e===point};
 try{globalThis.innerWidth=100;globalThis.innerHeight=100;globalThis.document={elementFromPoint:()=>covered?{}:point};globalThis.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1'});assert.equal(browserSurfaceExposure([root],['Graph'])[0].exposed,true);for(const mode of ['covered','hidden','offscreen']){covered=mode==='covered';hidden=mode==='hidden';offscreen=mode==='offscreen';assert.equal(browserSurfaceExposure([root],['Graph'])[0].exposed,false,mode);}}
 finally{for(const [n,v]of Object.entries(saved)){if(v===undefined)delete globalThis[n];else globalThis[n]=v;}}
});
test('controlled producer keeps original resume classification and uses real gestures without geometry injection',()=>{
 const source=fs.readFileSync(new URL('../../.clanker/tools/graph-performance-20261015/s00_ui_driver.mjs',import.meta.url),'utf8');
 assert(source.includes('assert.equal(result.mediaOnlyResume,true'));
 assert(source.includes('assert.equal(schedule.launchSha256,launchSha)'));assert(source.includes('assert.equal(hash(b),s)'));
 const arrange=source.slice(source.indexOf("actionStage='normal-arrange-resize'"),source.indexOf('result.distributions={}'));
 assert(arrange.includes("type:'mousePressed'"));assert(arrange.includes("type:'mouseMoved'"));assert(arrange.includes("type:'mouseReleased'"));assert(arrange.includes('document.elementFromPoint'));
 assert(arrange.includes('browserSurfaceExposure'));assert(arrange.includes('sameHead'));assert(!/\.style\s*[.[]|snapModalToZone|_applySnap|zIndex/.test(arrange));
 const contract=fs.readFileSync(new URL('../helpers/s00_ui_baseline_contract.mjs',import.meta.url),'utf8');assert(contract.includes("assert.equal(raw.measurementClass,'diagnostic-contended')"),'Existing negative-proof admission cannot be reclassified');
});
test('media ancestor uses only authority-issued exact target and unique synthetic suffix',()=>{
  const root={name:'generated-synthetic',id:'root-id',ref:'opaque-root',provider:'host',capabilities:['children']},target={name:'trial-05',id:'target-id',ref:'opaque-target',provider:'host'};
  const revealed={provider:'host',resource:target,ancestors:[{name:'/',id:'other',provider:'host'},root,{name:'cold-corpora',id:'cold',provider:'host'}]};
  assert.equal(syntheticFixtureAncestor(revealed,target,root.name),root);
  // The function is serialized into the browser, with no Node assert/import.
  const program=new Script(`(${syntheticFixtureAncestor})(${JSON.stringify(revealed)},${JSON.stringify(target)},${JSON.stringify(root.name)})`);assert.equal(program.runInNewContext().id,root.id);
  for(const mutate of [r=>r.resource={...target,id:'changed'},r=>r.provider='copal',r=>r.ancestors.at(-1).name='other',r=>r.ancestors.at(-2).name='other',r=>r.ancestors.unshift(root),r=>r.ancestors.at(-2).provider='other',r=>r.ancestors.at(-2).ref='']){const changed=structuredClone(revealed);mutate(changed);assert.throws(()=>syntheticFixtureAncestor(changed,target,root.name));}
});
test('normal synthetic Favorite response must retain exact owner-issued target identity',()=>{
  const anchor={id:'synthetic-root',name:'generated-synthetic'},response={resource:{...anchor,provider:'host',ref:'opaque-owned',capabilities:['children'],place_id:'place-'+'a'.repeat(32)}};assert.equal(verifySyntheticFavorite(response,anchor),true);
  assert.equal(new Script(`(${verifySyntheticFavorite})(${JSON.stringify(response)},${JSON.stringify(anchor)})`).runInNewContext(),true);
  for(const mutation of [r=>r.resource.id='other',r=>r.resource.provider='copal',r=>r.resource.name='other',r=>r.resource.place_id='invalid',r=>r.resource.ref='',r=>r.resource.capabilities=[]]){const changed=structuredClone(response);mutation(changed);assert.throws(()=>verifySyntheticFavorite(changed,anchor));}
});
test('side-effect-only circular media target is void, not a by-value CDP result',async()=>{
  const browser={};browser.circular=browser;const context={window:{__s00Browser:browser}};
  const body="window.__s00MediaTarget={kind:'preview',browser:window.__s00Browser,name:'large-preview.png'};";
  const accidental=new Script(body).runInNewContext(context);assert.throws(()=>JSON.stringify(accidental),TypeError);
  assert.equal(await new Script(asyncBrowserStatements(body)).runInNewContext(context),undefined);
});
test('preview selection binds stable ID then CURRENT UI token, not a separately rotated token',()=>{
  const expected={id:'stable-image',name:'large-preview.png',provider:'host',ref:'older-token'},entry={resource_id:expected.id,name:expected.name,provider:'host',resource_ref:'fresh-token'},row={dataset:{fileName:expected.name,resourceRef:'fresh-token'}};
  let entries=[entry],rows=[row];const browser={getCurrentDirectory:()=>({provider:'host',entries}),element:{querySelectorAll:()=>rows}};
  assert.equal(syntheticPreviewRow(browser,expected),row);assert.equal(new Script(`(${syntheticPreviewRow})(browser,expected)`).runInNewContext({browser,expected}),row);
  entry.resource_id='changed';assert.throws(()=>syntheticPreviewRow(browser,expected));entry.resource_id=expected.id;row.dataset.resourceRef='older-token';assert.throws(()=>syntheticPreviewRow(browser,expected));row.dataset.resourceRef=entry.resource_ref;
  rows=[row,row];assert.throws(()=>syntheticPreviewRow(browser,expected));rows=[row];entries=[entry,entry];assert.throws(()=>syntheticPreviewRow(browser,expected));entries=[entry];entry.provider='copal';assert.throws(()=>syntheticPreviewRow(browser,expected));
});
test('five dots fail semantic usefulness, independent of arbitrary list length',()=>{
  const five={...good(),groups:[],edges:[]};
  assert.equal(usefulView(five,GOLDEN).passed,false);
  assert(usefulView(five,GOLDEN).failures.some(x=>x.includes('hierarchy')));
  assert(usefulView(five,GOLDEN).failures.some(x=>x.includes('topology')));
  assert.equal(usefulView({...five,listLength:99999},GOLDEN).passed,false);
});
test('semantic view passes, but each viewport/readability/topology regression fails',()=>{
  assert.equal(usefulView(good(),GOLDEN).passed,true);
  for(const delta of [{graphHeightFraction:0.1},{minimumLabelPx:5},{labels:0},{clippedLabels:1},{overlapFraction:0.5},{allExpectedVisible:false},{edges:GOLDEN.edges.slice(1)},{groups:['src']}])assert.equal(usefulView({...good(),...delta},GOLDEN).passed,false);
});
test('real UI label mapping does not assume static fixture IDs equal native IDs',()=>{
  const expected={groups:['src'],edges:[{from:'src/main.py',to:'src/api/service.py'}]};
  const observed={...good(),edges:[{from:'native:a',to:'native:b'}],semanticEdges:expected.edges};
  assert.equal(usefulView(observed,expected).passed,true);
  assert.equal(usefulView({...observed,semanticEdges:[]},expected).passed,false);
});
test('static fixture distinguishes bad inventory from known semantic evidence',()=>{
  const inventory=queryFixture(),rich=queryFixture('overview',true);
  assert.equal(inventory.hits.length,5);assert.equal(inventory.edges.length,0);
  assert.equal(rich.edges.length,2);assert.equal(rich.receipt.head_generation,inventory.receipt.head_generation);
  assert(GOLDEN.disconnected.length&&GOLDEN.inferred.length&&GOLDEN.unresolved.length);
});
test('generated PNG is deterministic valid bounded synthetic RGB data',()=>{
  const image=syntheticPng(16,12,4);assert.equal(image.subarray(1,4).toString(),'PNG');
  assert.equal(image.readUInt32BE(16),16);assert.equal(image.readUInt32BE(20),12);
  let at=8,body=[];while(at<image.length){const n=image.readUInt32BE(at),type=image.subarray(at+4,at+8).toString();if(type==='IDAT')body.push(image.subarray(at+8,at+8+n));at+=n+12;}
  assert.equal(inflateSync(Buffer.concat(body)).length,(16*3+1)*12);
  assert.equal(createHash('sha256').update(image).digest('hex'),createHash('sha256').update(syntheticPng(16,12,4)).digest('hex'));
  assert.throws(()=>syntheticPng(2049,1));assert.throws(()=>syntheticPng(0,1));
});
test('distribution retains failures separately, does not invent missing samples',()=>{
  assert.deepEqual(distribution([]),{samples:0,median:null,p95:null,max:null});
  assert.deepEqual(distribution([1,4,2,3,9]),{samples:5,median:3,p95:9,max:9});
});
test('stage boundaries never substitute arm time for missing actual choice',()=>{
  const times={armed:0,pickerOpened:20,choice:100,progress:120,scopeCommit:180,drawable:250,structural:280,published:1000,useful:1100};
  assert.deepEqual(stageDurations(times),{pickerOpenToConfirmationMs:80,choiceToProgressMs:20,choiceToScopeCommitMs:80,choiceToFirstDrawableMs:150,choiceToStructuralMs:180,choiceToPublishedMs:900,choiceToUsefulMs:1000,scopeCommitToUsefulMs:920,armToFirstDrawableMs:250,armToPublishedMs:1000,armToUsefulMs:1100});
  assert.equal(stageDurations({armed:0,useful:100}).choiceToUsefulMs,null);
  assert.equal(stageDurations({choice:100,useful:20}).choiceToUsefulMs,null);
  assert(S00_SCENARIOS.includes('files-640')&&S00_SCENARIOS.includes('editor-code-image'));
});
test('media timings require their own navigation and real milestone, never graph stamps',()=>{
  assert.deepEqual(mediaStageDurations({armed:0,drawable:5,useful:10}),{navigationToRowsLoadedMs:null,navigationToImageDecodedVisibleMs:null,navigationToEditableDocumentMs:null});
  assert.deepEqual(mediaStageDurations({navigation:100,rowsLoaded:130,imageDecodedVisible:170,documentReady:150}),{navigationToRowsLoadedMs:30,navigationToImageDecodedVisibleMs:70,navigationToEditableDocumentMs:50});
  assert.equal(mediaStageDurations({navigation:100,rowsLoaded:50}).navigationToRowsLoadedMs,null);
});
test('actual loaded-row milestone is target/cursor/navigation bound',()=>{
  const directory={resourceId:'expected',entries:[1,2],nextCursor:null};let navigating=false;
  const target={kind:'files',resourceId:'expected',count:2,browser:{getCurrentDirectory:()=>directory,captureCommands:()=>({navigating}),element:{querySelectorAll:()=>[]}}};
  assert.equal(browserMediaMilestones(target).rowsLoaded,true);
  directory.nextCursor='cursor';assert.equal(browserMediaMilestones(target).rowsLoaded,false);directory.nextCursor=null;
  navigating=true;assert.equal(browserMediaMilestones(target).rowsLoaded,false);navigating=false;
  directory.resourceId='old-folder';assert.equal(browserMediaMilestones(target).rowsLoaded,undefined);
});
test('decoded-visible milestone awaits actual decode, dimensions and visibility',async()=>{
  const saved={document:globalThis.document,getComputedStyle:globalThis.getComputedStyle,innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};let hidden=false,cover=false,transparent=false,wrapperHit=false;
  const wrapper={};
  const image={complete:true,naturalWidth:2048,naturalHeight:2048,decode:()=>Promise.resolve(),getBoundingClientRect:()=>({x:0,y:0,width:20,height:20,right:20,bottom:20})};
  const target={kind:'preview',name:'large-preview.png',browser:{element:{querySelector:s=>s.includes('strong')?{textContent:'large-preview.png'}:image}}};
  try{globalThis.document={elementFromPoint:()=>cover?{}:wrapperHit?wrapper:image};globalThis.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:hidden?'0':'1',pointerEvents:transparent?'none':'auto'});globalThis.innerWidth=100;globalThis.innerHeight=100;
    assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);await Promise.resolve();assert.equal(browserMediaMilestones(target).imageDecodedVisible,true);
    cover=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false,'Decoded image under opaque external overlay is not exposed');cover=false;
    wrapperHit=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false,'Unrelated wrapper is not owner');transparent=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);
    image.parentElement=wrapper;assert.equal(browserMediaMilestones(target).imageDecodedVisible,true,'Pointer-transparent image permits only its immediate wrapper');wrapperHit=false;transparent=false;delete image.parentElement;
    hidden=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);hidden=false;image.naturalWidth=0;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);
  }finally{for(const [key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
test('preview samples actual clipped image intersection and still rejects covering or empty clips',async()=>{
 const names=['document','getComputedStyle','innerWidth','innerHeight'],saved=Object.fromEntries(names.map(n=>[n,globalThis[n]]));let cover=false,empty=false;
 const clip={getBoundingClientRect:()=>({x:0,y:0,right:100,bottom:empty?0:20})};const image={parentElement:clip,complete:true,naturalWidth:2048,naturalHeight:2048,decode:()=>Promise.resolve(),getBoundingClientRect:()=>({x:0,y:0,width:100,height:200,right:100,bottom:200})};const target={kind:'preview',name:'large-preview.png',browser:{element:{querySelector:s=>s.includes('strong')?{textContent:'large-preview.png'}:image}}};
 try{globalThis.innerWidth=100;globalThis.innerHeight=300;globalThis.getComputedStyle=e=>({display:'block',visibility:'visible',opacity:'1',overflowX:e===clip?'hidden':'visible',overflowY:e===clip?'auto':'visible'});globalThis.document={elementFromPoint:(_x,y)=>!cover&&y<20?image:{}};
  assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);await Promise.resolve();assert.equal(browserMediaMilestones(target).imageDecodedVisible,true);cover=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);cover=false;empty=true;assert.equal(browserMediaMilestones(target).imageDecodedVisible,false);
 }finally{for(const [n,v]of Object.entries(saved)){if(v===undefined)delete globalThis[n];else globalThis[n]=v;}}
});
test('editable-ready milestone rejects stale document cue, wrong title, busy and covered content',()=>{
  const saved={document:globalThis.document,getComputedStyle:globalThis.getComputedStyle,innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};let busy=false,editable=true,cover=false;
  const rect=()=>({x:0,y:0,width:20,height:20,right:20,bottom:20}),title={textContent:'image.md',getBoundingClientRect:()=>({...rect(),y:30,bottom:50})};
  const content={textContent:'S00 desired document',getBoundingClientRect:rect,getAttribute:()=>editable?'true':'false',closest:s=>s.includes('aria-busy')?(busy?{}:null):null};
  const target={kind:'editor',name:'image.md',cue:'S00 desired document',image:false};
  try{globalThis.document={querySelectorAll:s=>s.includes('inline-title')?[title]:[content],elementFromPoint:(_x,y)=>cover?{}:y>=30?title:content};globalThis.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1'});globalThis.innerWidth=100;globalThis.innerHeight=100;
    assert.equal(browserMediaMilestones(target).documentReady,true);content.textContent='old document';assert.equal(browserMediaMilestones(target).documentReady,false);content.textContent=target.cue;
    cover=true;assert.equal(browserMediaMilestones(target).documentReady,false,'Opaque overlay cannot satisfy editable readiness');cover=false;
    title.textContent='other.md';assert.equal(browserMediaMilestones(target).documentReady,false);title.textContent='image.md';busy=true;assert.equal(browserMediaMilestones(target).documentReady,false);busy=false;editable=false;assert.equal(browserMediaMilestones(target).documentReady,false);
  }finally{for(const [key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
test('five cold roots use pinned native golden plus 129 declarations, never old corpus',()=>{
  const root=fs.mkdtempSync('/private/tmp/openclank-s00-media-');fs.chmodSync(root,0o700);
  try{
    fs.mkdirSync(path.join(root,'corpus'),{mode:0o700});const bytes=Buffer.from('VALUE=1\n');fs.writeFileSync(path.join(root,'corpus','tiny.py'),bytes,{flag:'wx',mode:0o600});
    const fixture={schema:'openclank.s00-synthetic-fixture/v1',synthetic:true,root,corpus:{modules:1},files:[{path:'corpus/tiny.py',bytes:bytes.length,sha256:createHash('sha256').update(bytes).digest('hex')}]};
    fs.writeFileSync(path.join(root,'fixture-manifest.json'),JSON.stringify(fixture),{flag:'wx',mode:0o600});
    const derived=deriveColdCorpora(fixture);assert.deepEqual(derived.trials.map(t=>t.number),[1,2,3,4,5]);
    const {golden,source}=readNativeGolden();assert.equal(source.sha256,NATIVE_GOLDEN_SHA256);assert.equal(derived.goldenSource.sha256,source.sha256);
    assert.deepEqual(derived.realGolden.relationships,golden.relationships);assert.deepEqual(golden.relationships.map(r=>r.state),['exact','inferred']);
    for(const trial of derived.trials){assert.equal(trial.files.length,Object.keys(golden.files).length+129);assert(!fs.existsSync(path.join(trial.directory,'tiny.py')));
      for(const [relative,text]of Object.entries(golden.files))assert.equal(fs.readFileSync(path.join(trial.directory,relative),'utf8'),text);
      for(let n=0;n<129;n++)assert.equal(fs.readFileSync(path.join(trial.directory,`partition/f${String(n).padStart(3,'0')}.py`),'utf8'),`def filler_${n}():\n    return ${n}\n`);
      for(const row of trial.files){const target=path.join(trial.directory,row.path);assert.equal(createHash('sha256').update(fs.readFileSync(target)).digest('hex'),row.sha256);assert.equal(fs.statSync(target).mode&0o777,0o600);}
    }
    assert.deepEqual(fs.readFileSync(path.join(root,'corpus','tiny.py')),bytes);
    assert.throws(()=>deriveColdCorpora(fixture),'Refuse to overwrite earlier derived scopes');
    fs.writeFileSync(path.join(root,'corpus','tiny.py'),'CHANGED');assert.throws(()=>deriveColdCorpora(fixture),'Reject unverified source before copying');
    const bad={...fixture,files:[{...fixture.files[0],path:'corpus/../../outside.py'}]};fs.writeFileSync(path.join(root,'fixture-manifest.json'),JSON.stringify(bad));assert.throws(()=>deriveColdCorpora(bad));
  }finally{fs.rmSync(root,{recursive:true});}
});
test('native golden proof rejects label-only IDs, changed receipt, missing edge and wrong state',()=>{
  const receipt={workspace_id:'synthetic',head_generation:'actual-head'};
  const golden={groups:['src'],relationships:[{caller:'caller',caller_file:'caller.py',target:'target',target_file:'dep.py',state:'exact'}]};
  const page=operation=>({schema:'open-clank.code-query/v1',operation,receipt,error:null,next_cursor:null});
  const proof={caller:{...page('search'),hits:[{id:'decl:a',kind:'declaration',label:'caller',properties:{rel_path:'caller.py'}}]},target:{...page('search'),hits:[{id:'decl:b',kind:'declaration',label:'target',properties:{rel_path:'dep.py'}}]},neighbors:{...page('neighbors'),edges:[{from:'decl:a',to:'decl:b',resolution_state:'exact'}]}};
  const expected=proveNativeRelationships(golden,receipt,[proof]);assert.deepEqual(expected.edges,[{from:'decl:a',to:'decl:b',state:'exact'}]);
  for(const mutate of [p=>p.caller.hits[0].properties.rel_path='other.py',p=>p.caller.hits[0].id='file:a',p=>p.target.receipt={...receipt,head_generation:'other'},p=>p.neighbors.edges=[],p=>p.neighbors.edges[0].resolution_state='inferred',p=>p.neighbors.next_cursor='more']){const altered=structuredClone(proof);mutate(altered);assert.throws(()=>proveNativeRelationships(golden,receipt,[altered]));}
  assert.equal(usefulView({...good(),edges:[],semanticEdges:expected.edges},expected).passed,false,'Actual native proof cannot fall back to label aliases');
  assert.equal(usefulView(good(),{groups:[],edges:[],nativeProofRequired:true}).passed,false);
  const graph={...good(),receipt,groups:['src'],edges:expected.edges};const stage={graphCandidates:[{time:25,graph:{...graph,receipt:{...receipt,head_generation:'old'}}},{time:80,graph}]};
  assert.equal(validateUsefulSnapshots(stage,expected).time,80);assert.equal(validateUsefulSnapshots({...stage,graphCandidatesTruncated:true},expected).time,null);
});
test('partial cold resume admits only exact closed pre-second-scope failure and native proof',()=>{
  const receipt={workspace_id:'synthetic',head_generation:'head'},identity={pid:123,birth:'same-birth',origin:'http://127.0.0.1:1234',launchSha256:'a'.repeat(64)};
  const golden={groups:['src'],relationships:[{caller:'caller',caller_file:'caller.py',target:'target',target_file:'dep.py',state:'exact'}]};
  const page=operation=>({schema:'open-clank.code-query/v1',operation,receipt,error:null,next_cursor:null});
  const proofs=[{caller:{...page('search'),hits:[{id:'decl:a',kind:'declaration',label:'caller',properties:{rel_path:'caller.py'}}]},target:{...page('search'),hits:[{id:'decl:b',kind:'declaration',label:'target',properties:{rel_path:'dep.py'}}]},neighbors:{...page('neighbors'),edges:[{from:'decl:a',to:'decl:b',resolution_state:'exact'}]}}];
  const expected=proveNativeRelationships(golden,receipt,proofs),graph={...good(),receipt,groups:[],edges:[]},verdict=usefulView(graph,expected);
  const raw={...identity,status:'failed',browserClosed:true,measurementClass:'diagnostic-contended',groups:['exact prior normal setup reused without setup request','normal authenticated headed shell','exact prior synthetic files retained; no new policy grant','normal synthetic cold folder choice 1'],failures:[...verdict.failures.map(f=>'Cold 1: '+f),'UI observation expired: authorized synthetic folder'],coldTrials:[{number:1,verdict}],measurements:[{scenario:'graph-choice',repetition:1,graph,nativeGoldenProof:{proofs,expected}}],nativeRequests:[{route:'/api/file-policy/workspaces/synthetic/index/open',status:200},{route:'/api/memory/code/graph',status:200}]};
  assert.deepEqual(validatePartialColdResume(raw,identity,golden),{nextColdTrial:2,completedColdTrials:[1],earlierHead:receipt});
  for(const mutate of [r=>r.pid++,r=>r.browserClosed=false,r=>r.status='passed',r=>r.failures.pop(),r=>r.coldTrials.push({number:2}),r=>r.nativeRequests.push({route:'/api/file-policy/workspaces/other/index/open',status:200}),r=>r.nativeRequests.push(r.nativeRequests[0]),r=>r.measurements[0].nativeGoldenProof.proofs[0].neighbors.edges=[],r=>r.measurements[0].graph.receipt={...receipt,head_generation:'other'}]){const changed=structuredClone(raw);mutate(changed);assert.throws(()=>validatePartialColdResume(changed,identity,golden));}
  const complete={...structuredClone(raw),groups:raw.groups.slice(0,3).concat([2,3,4,5].map(n=>'normal synthetic cold folder choice '+n)),measurements:[],coldTrials:[],nativeRequests:[],failures:[],priorPartialCold:{completedColdTrials:[1],nextColdTrial:2,earlierHead:receipt}};
  for(let n=2;n<=5;n++){const stage=structuredClone(raw.measurements[0]),r={workspace_id:'synthetic-'+n,head_generation:'head-'+n};stage.repetition=n;stage.graph.receipt=r;for(const page of Object.values(stage.nativeGoldenProof.proofs[0]))page.receipt=r;stage.nativeGoldenProof.expected=proveNativeRelationships(golden,r,stage.nativeGoldenProof.proofs);const v=usefulView(stage.graph,stage.nativeGoldenProof.expected);complete.measurements.push(stage);complete.coldTrials.push({number:n,verdict:v});complete.failures.push(...v.failures.map(f=>'Cold '+n+': '+f));complete.nativeRequests.push({route:'/api/file-policy/workspaces/'+r.workspace_id+'/index/open',status:200});}
  complete.failures.push('Browser evaluation failed at warm-reopen-0; private expression omitted');
  complete.nativeRequests.push({route:'/api/file-policy/workspaces/synthetic-2/index/release',status:200});
  assert.deepEqual(validateCompletedColdResume(complete,raw,identity,golden),{nextColdTrial:6,completedColdTrials:[1,2,3,4,5],warmupNeedsAttachment:true});
  for(const mutate of [r=>r.pid++,r=>r.measurements.pop(),r=>r.failures.pop(),r=>r.nativeRequests.push(raw.nativeRequests[0]),r=>r.nativeRequests.push(r.nativeRequests.at(-1)),r=>r.priorPartialCold.completedColdTrials=[2],r=>r.measurements[0].nativeGoldenProof.proofs[0].neighbors.edges=[]]){const changed=structuredClone(complete);mutate(changed);assert.throws(()=>validateCompletedColdResume(changed,raw,identity,golden));}
  const warm={...structuredClone(complete),groups:raw.groups.slice(0,3),coldTrials:[],measurements:[],failures:[],nativeRequests:[],priorPartialCold:{completedColdTrials:[1,2,3,4,5],nextColdTrial:6},excludedWarmSessionAttachment:{normalReindexPossible:true,coldCredit:false}};
  for(let n=0;n<=5;n++){const stage=structuredClone(complete.measurements.at(-1));stage.scenario='graph-warm-reopen';stage.repetition=n;stage.excludedWarmup=n===0;stage.readyObserved=true;stage.sessionAttachment=n===0;warm.measurements.push(stage);warm.failures.push(...usefulView(stage.graph,stage.nativeGoldenProof.expected).failures.map(f=>'Warm '+n+': '+f));warm.nativeRequests.push({route:'/api/file-policy/workspaces/synthetic-5/index/open',status:200});if(n)warm.nativeRequests.push({route:'/api/file-policy/workspaces/synthetic-5/index/release',status:200});}
  warm.failures.push('Only exact generated Host Location');assert.deepEqual(validateCompletedWarmResume(warm,complete,raw,identity,golden),{nextColdTrial:6,completedColdTrials:[1,2,3,4,5],completedWarmReopens:5,skipWarm:true});
  for(const mutate of [r=>r.measurements.pop(),r=>r.failures.pop(),r=>r.measurements[0].readyObserved=false,r=>r.measurements[0].nativeGoldenProof.expected.receipt.workspace_id='changed',r=>r.nativeRequests.push({route:'/api/file-policy/workspaces/other/index/open',status:200}),r=>r.excludedWarmSessionAttachment.coldCredit=true]){const changed=structuredClone(warm);mutate(changed);assert.throws(()=>validateCompletedWarmResume(changed,complete,raw,identity,golden));}
});
test('actual observation includes viewBox/camera scale in displayed label pixels',()=>{
  const saved={document:globalThis.document,getComputedStyle:globalThis.getComputedStyle,innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};
  let scale=1;
  const box={x:0,y:0,width:100,height:100,right:100,bottom:100};
  const label={tagName:'text',contains:e=>e===label,getBoundingClientRect:()=>({...box,width:20,height:12,right:20,bottom:12}),getScreenCTM:()=>({a:scale,b:0,c:0,d:scale})};
  const viewport={getBoundingClientRect:()=>box,querySelectorAll:selector=>selector==='text'?[label]:[]};
  const root={getBoundingClientRect:()=>box,querySelector:()=>viewport,querySelectorAll:()=>[],closest:()=>null};
  try{
    globalThis.document={querySelector:()=>root,elementFromPoint:()=>label};globalThis.getComputedStyle=()=>({fontSize:'12px',visibility:'visible',display:'block'});globalThis.innerWidth=100;globalThis.innerHeight=100;
    assert.equal(browserGraphObservation().minimumLabelPx,12);scale=.25;assert.equal(browserGraphObservation().minimumLabelPx,3);
    scale=2;assert.equal(browserGraphObservation().minimumLabelPx,24);
  }finally{for(const [key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
test('semantic DOM markers cannot pass when hidden, offscreen, transparent, ancestor-hidden or occluded',()=>{
  const saved={document:globalThis.document,getComputedStyle:globalThis.getComputedStyle,innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};
  const box=(x=10)=>({x,y:10,width:10,height:10,right:x+10,bottom:20});
  let state='visible';const parent={};
  const item={tagName:'line',parentElement:parent,getBoundingClientRect:()=>box(state==='offscreen'?200:10),contains:e=>e===item,
    getAttribute:key=>({'data-code-group':'src','data-from':'file:a','data-to':'file:b'}[key]),querySelectorAll:()=>[],matches:()=>false};
  const viewport={getBoundingClientRect:()=>({x:0,y:0,width:100,height:100,right:100,bottom:100}),querySelectorAll:s=>s==='text'?[]:[item]};
  const root={getBoundingClientRect:viewport.getBoundingClientRect,querySelector:()=>viewport,querySelectorAll:()=>[],closest:()=>null};
  try{
    globalThis.document={querySelector:()=>root,elementFromPoint:()=>state==='occluded'?{}:item};globalThis.innerWidth=100;globalThis.innerHeight=100;
    globalThis.getComputedStyle=e=>({fontSize:'12px',visibility:state==='hidden'?'hidden':'visible',display:state==='ancestor-hidden'&&e===parent?'none':'block',opacity:state==='transparent'?'0':'1',stroke:'black',strokeWidth:'1px',fill:'none'});
    assert.equal(browserGraphObservation().groups.length,1);assert.equal(browserGraphObservation().edges.length,1);assert.equal(browserGraphObservation().allExpectedVisible,true);
    for(state of ['hidden','offscreen','transparent','ancestor-hidden','occluded']){const r=browserGraphObservation();assert.equal(r.groups.length,0,state);assert.equal(r.edges.length,0,state);assert.equal(r.allExpectedVisible,false,state);}
  }finally{for(const [key,value]of Object.entries(saved)){if(value===undefined)delete globalThis[key];else globalThis[key]=value;}}
});
const config=()=>({schema:'openclank.s00-ui-capture/v1',disposable:true,root:'/private/tmp/openclank-graph-usability-s00-test',origin:'http://127.0.0.1:49341',mode:'source',cacheClass:'cold-data',osCache:'not-flushed',scenario:'graph-choice',observationMs:60000,sourceManifestSha256:'a'.repeat(64),syntheticUsername:'s00-fixture',webSocketDebuggerUrl:'ws://127.0.0.1:49342/devtools/page/ABC-123',server:{pid:100,birth:'Wed Oct 15 10:00:00 2026'},browser:{pid:101,birth:'Wed Oct 15 10:00:01 2026'}});
const approval=c=>({schema:'openclank.s00-ui-capture-approval/v1',config_sha256:'b'.repeat(64),disposable:true,root:c.root,origin:c.origin,server_pid:c.server.pid,browser_pid:c.browser.pid,expires_at_ms:Date.now()+60000});
test('capture requires exact fresh root authorization and synthetic identity',()=>{
  const c=config();assert.equal(validateCaptureConfig(c,approval(c),'b'.repeat(64)),c);
  for(const mutate of [c=>c.origin='http://127.0.0.1:7777',c=>c.origin='https://example.org',c=>c.origin='http://localhost:49341',c=>c.syntheticUsername='real-person',c=>c.disposable=false,c=>c.webSocketDebuggerUrl='ws://example.org:123/devtools/page/X',c=>c.cacheClass='cold-os-flushed']){const bad=config();mutate(bad);assert.throws(()=>validateCaptureConfig(bad,approval(bad),'b'.repeat(64)));}
  assert.throws(()=>validateCaptureConfig(c,{...approval(c),server_pid:102},'b'.repeat(64)));
  assert.throws(()=>validateCaptureConfig(c,{...approval(c),expires_at_ms:Date.now()-1},'b'.repeat(64)));
  assert.throws(()=>validateCaptureConfig(c,approval(c),'c'.repeat(64)));
});
