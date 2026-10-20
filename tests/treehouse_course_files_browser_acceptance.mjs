import test from 'node:test';
import assert from 'node:assert/strict';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

// Mounted production UI with explicit API/Editor handoff doubles. Native file
// authority, permissions and real Editor Save remain full-app acceptance gates.
const page=`<!doctype html><meta charset="utf-8"><style>body{--fg:#ddd;--bg:#222;--panel:#333;--border:#666;color:var(--fg);background:var(--bg);font-family:sans-serif}.copal-view{height:900px;overflow:auto}button{cursor:pointer}</style><main class="copal-view"></main><script type="module">`+String.raw`
try {
 const {configureCopalStorage}=await import('/static/js/copal/storage.js');configureCopalStorage('course-files-fixture');
 const {createTreeHouseFeature}=await import('/static/js/copal/treehouse.js');
 const {createMarkdownRenderer}=await import('/static/js/copal/markdownRenderer.js');
 const h=(tag,attrs={},...children)=>{const n=document.createElement(tag);for(const[k,v]of Object.entries(attrs)){if(k==='text')n.textContent=v;else if(k.startsWith('on'))n.addEventListener(k.slice(2),v);else if(k==='value')n.value=v;else if(v!==false&&v!=null)n.setAttribute(k,v===true?'':v);}for(const c of children.flat())if(c!=null)n.append(c instanceof Node?c:document.createTextNode(String(c)));return n;};
 const renderer=createMarkdownRenderer({h,documents:()=>[],openTarget(){}}),courseId='course:files',moduleId='module:files';
 window.account='a';window.revision=0;window.head='body:one';window.metadataHead='metadata:one';window.canEdit=true;window.calls=[];window.opens=[];window.sources={'activity:one':'# First file\n\n[Second](#treehouse-unit=activity%3Atwo)','activity:two':'# Second file\n\n[First](#treehouse-unit=activity%3Aone)'};
 const activity=id=>({id,courseId,moduleId,title:id==='activity:one'?'First lesson':'Second lesson',status:'published',activityType:'markdown',contentDeferred:true,points:0,skillIds:[]});
 const assignment={id:'assignment:one',courseId,moduleId,title:'File task',status:'published',assessmentType:'text',graded:false,maxPoints:1,contentDeferred:true};
 const snapshot=()=>({accountId:window.account,workspace:'default',actor:{id:window.account},permissions:{learner:true,author:true,admin:true},courseCapabilities:{[courseId]:{edit:true,owner:true}},state:{revision:window.aggregateRevision??window.revision,profiles:{[window.account]:{id:window.account,active:true,roles:['owner','learner']}},courses:{[courseId]:{id:courseId,title:'File course',ownerId:window.account,status:'published',courseType:'tutorial',freeExploration:true,moduleIds:[moduleId],contentAuthority:'course-files',sourceMetadataHead:window.metadataHead,curriculumRevision:window.revision},...(window.aggregateRevision?{'course:other':{id:'course:other',ownerId:'other-owner',title:'Other owner course',status:'published',courseType:'tutorial',freeExploration:true,moduleIds:[],curriculumRevision:window.aggregateRevision}}:{})},modules:{[moduleId]:{id:moduleId,courseId,title:'File lessons',activityIds:['activity:one','activity:two'],assignmentIds:['assignment:one']}},activities:{'activity:one':activity('activity:one'),'activity:two':activity('activity:two')},assignments:{'assignment:one':assignment},skills:{},badges:{},quests:{},submissions:window.reviewing?{['assignment:one:'+window.account]:{id:'submission:one',assignmentId:'assignment:one',profileId:window.account,courseId,attempts:1,attemptId:'attempt:one',status:'submitted',answer:'An earlier response',workHead:'body:one'}}:{},courseGrants:{},evidence:{},enrollments:{enrollment:{id:"enrollment",courseId,profileId:window.account,status:"active"}},events:[]},projection:{learners:{[window.account]:{courses:{[courseId]:{requiredActivityIds:['activity:one','activity:two'],requiredAssignmentIds:[],missingAssignmentIds:[],resetGeneration:0}},completedActivityIds:[],points:0}}},globalCatalogue:null,globalAvailability:{status:'ready'}});
 const source=()=>({kind:'course-files',contentHead:window.head,originDigest:'origin',canEdit:window.nativeEdit??window.canEdit,canOpenInFiles:!window.recipient,canEditInFiles:window.canEdit});
 const detail=id=>({accountId:window.account,workspace:'default',courseId,catalogueRevision:window.revision,sourceMetadataHead:window.metadataHead,permissions:{learn:true,edit:true,editText:window.nativeEdit??window.canEdit,editSource:window.canEdit,openSource:!window.recipient},contentSource:source(),...(id.startsWith('assignment:')?{assignment:{...assignment,prompt:'Explain the supplied file.',assessmentType:window.quiz?'quiz':'text',questions:window.quiz?[{id:'q',prompt:'Which supplied value equals 2?',options:[{id:'two',text:'2'},{id:'three',text:'3'}]}]:[],contentDeferred:false}}:{activity:{...activity(id),content:window.sources[id],contentDeferred:false},assignments:[]})});
 const api=async(path,options)=>{const body=options?.body?JSON.parse(options.body):null;window.calls.push({path,body});
  if(path.startsWith('/treehouse?'))return snapshot();
  if(path.includes('/source-open')){if(window.pauseSource)await new Promise(r=>window.releaseSource=r);if(window.denySource)throw Error('Files access denied');return {resource:{ref:'opaque:authorized'},canEdit:window.canEdit,contentHead:window.head};}
  if(path.includes('/activities/')||path.includes('/assignments/')){const id=decodeURIComponent(new URL(path,'http://fixture').pathname.split('/')[5]),value=detail(id);if(window.pauseDetail===id)await new Promise(r=>window.releaseDetail=r);if(window.failDetail===id)throw Error('Source file unavailable');return value;}
  if(path==='/treehouse/commands'){
   if(body.type==='course.create'){if(body.expectedRevision!==null)throw Error('Creation must not use another owner revision');return {...snapshot(),result:{courseId:'course:created'}};}
   if(['submission.draft','submission.submit','submission.grade'].includes(body.type)){if(body.payload.workBase!==window.head)throw Error('stale_work_source');return {...snapshot(),result:{status:'saved'}};}
   if(body.type==='activity.update'||body.type==='assignment.update'){
    if(body.payload.contentBase!==window.head)throw Error('content_conflict');
    if(body.payload.metadataBase!==window.metadataHead)throw Error('source_metadata_conflict');
    if(body.expectedRevision!==window.revision)throw Error('revision_conflict');
    if(body.payload.content)window.sources[body.payload.activityId]=body.payload.content;
    window.head='body:new';window.revision++;
    return {...snapshot(),result:{contentWrite:{outcome:window.pendingIndex?'committed-index-pending':'committed',contentHead:window.head,partial:!!window.partialSave,receipt:{actionId:'native-action',outcome:window.partialSave?'partial':'committed'}}}};
   }
   return {...snapshot(),result:{ok:true}};
  }
  if(path.includes('/learner-preview'))return {...snapshot(),preview:{revision:window.revision}};
  return {unlocked:[],notifications:[]};
 };
 const feature=createTreeHouseFeature({h,api,setStatus:m=>window.statusMessage=m,renderMarkdown:renderer.renderMarkdown,openDocument(){},openResource:async(ref,guards)=>{if(guards.isCurrent()&&guards.handoffCurrent())window.opens.push(ref);return ref;}});
 window.feature=feature;window.root=document.querySelector('main');window.reload=()=>feature.render(window.root);window.switchAccount=async()=>{window.account='b';feature.suspendScope();await window.reload();};
 await window.reload();window.ready=true;
}catch(error){window.error=error.stack;}
</script>`;

const click=(evaluate,text,selector='button')=>evaluate(`(()=>{const n=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(text)});if(!n)throw Error('Missing '+${JSON.stringify(text)});n.click()})()`);
const ready=async(evaluate,until)=>{await until('window.ready||window.error');assert.equal(await evaluate('window.error'),undefined);};
const open=async(evaluate,until)=>{await click(evaluate,'Explore');await until('document.querySelector(".th-lesson-body")');};

test('private outlines stay body-free and selected reads, source handoff and refresh are scoped',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);assert.equal(await evaluate('window.calls.some(x=>x.path.includes("/activities/"))'),false);
  await open(evaluate,until);assert.equal(await evaluate('window.calls.filter(x=>x.path.includes("/activities/")).length'),1);
  assert.match(await evaluate('document.querySelector(".th-lesson-body").textContent'),/First file/);
  await click(evaluate,'Edit source');await until('window.opens.length===1');assert.equal(await evaluate('window.opens[0]'),'opaque:authorized');
  await evaluate('window.sources["activity:one"]="# External edit";window.head="external:head";window.reload()');await until('document.querySelector(".th-lesson-body")?.textContent.includes("External edit")');
  await evaluate('window.pauseSource=true');await click(evaluate,'Edit source');await until('typeof window.releaseSource==="function"');await evaluate('window.switchAccount()');await evaluate('window.releaseSource()');assert.equal(await evaluate('window.opens.length'),1);
 });
});

test('late selected body cannot replace another lesson and a missing body stays pending until explicit retry',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.pauseDetail="activity:one"');await click(evaluate,'Explore');await until('typeof window.releaseDetail==="function"');
  await evaluate('document.querySelectorAll(".th-index-step")[1].click()');await until('document.querySelector(".th-lesson-body")?.textContent.includes("Second file")');
  await evaluate('window.releaseDetail()');assert.match(await evaluate('document.querySelector(".th-lesson-body").textContent'),/Second file/);
  await evaluate('window.failDetail="activity:two";window.reload()');await until('document.querySelector(".th-lesson [role=alert]")');
  assert.equal(await evaluate('!!document.querySelector(".th-player-footer")'),false);
  const count=await evaluate('window.calls.length');await new Promise(r=>setTimeout(r,80));assert.equal(await evaluate('window.calls.length'),count);
  await evaluate('window.failDetail=null');await click(evaluate,'Retry lesson');await until('document.querySelector(".th-lesson-body")?.textContent.includes("Second file")');
 });
});

test('author text save retains displayed body and metadata heads on conflict or committed-index-pending',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await click(evaluate,'Author');await click(evaluate,'Open curriculum');
  await evaluate('document.querySelector(".th-curriculum-row button").click()');await until('!!document.querySelector(".cm-content")');
  await evaluate(`const editor=document.querySelector('.cm-content');editor.focus();document.execCommand('selectAll');document.execCommand('insertText',false,'My unsent exact draft');window.metadataHead='metadata:external'`);
  await click(evaluate,'Save activity');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("source_metadata_conflict")');
  assert.match(await evaluate('document.querySelector(".cm-content").textContent'),/My unsent exact draft/);
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="activity.update").at(-1).body.payload.metadataBase'),'metadata:one');
  await evaluate('window.metadataHead="metadata:one";window.pendingIndex=true');await click(evaluate,'Save activity');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("Source saved")');
  assert.match(await evaluate('document.querySelector(".cm-content").textContent'),/My unsent exact draft/);
  await click(evaluate,'Save activity');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("content_conflict")');
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="activity.update").at(-1).body.payload.contentBase'),'body:one');
  await evaluate('window.head="body:one";window.revision=0;window.partialSave=true');await click(evaluate,'Save activity');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("Some source files saved")');assert.match(await evaluate('document.querySelector(".cm-content").textContent'),/My unsent exact draft/);await click(evaluate,'Recheck source');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("Source reread")');assert.match(await evaluate('document.querySelector(".cm-content").textContent'),/My unsent exact draft/);
 });
});

test('private task dialog hydrates its prompt without reading sibling bodies',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await open(evaluate,until);await evaluate('document.querySelectorAll(".th-index-step")[2].click()');await until('document.querySelector(".th-lesson-body")?.textContent.includes("Explain the supplied file")');
  await click(evaluate,'Submit work');await until('document.querySelector("dialog .th-assessment-prompt")');assert.match(await evaluate('document.querySelector("dialog").textContent'),/Explain the supplied file/);
  assert.equal(await evaluate('window.calls.some(x=>x.path.includes("activity%3Atwo"))'),false);
 });
});

test('author preview reads one opened file and reciprocal imported-style links remain local and inert',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await click(evaluate,'Author');await click(evaluate,'Open curriculum');await click(evaluate,'Learner preview');
  assert.equal(await evaluate('window.calls.some(x=>x.path.includes("/activities/"))'),false);
  await evaluate('document.querySelector("dialog details[data-lazy-lesson]").open=true');await until('document.querySelector("dialog .copal-meme-body a")');
  assert.equal(await evaluate('window.calls.filter(x=>x.path.includes("/activities/")).length'),1);
  await evaluate('document.querySelector("dialog .copal-meme-body a").click()');await until('document.querySelectorAll("dialog .copal-meme-body").length===2');
  assert.equal(await evaluate('window.calls.some(x=>x.body?.type==="course.open"||x.body?.type==="activity.complete")'),false);
  assert.equal(await evaluate('window.opens.length'),0);
 });
});

test('explicit native source read-only keeps author text inert independently of course metadata edit',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.canEdit=false');await open(evaluate,until);assert.equal(await evaluate('[...document.querySelectorAll("button")].some(x=>x.textContent==="Edit source")'),false);
  await evaluate('window.denySource=true');await click(evaluate,'View source');await until('window.statusMessage==="Files access denied"');assert.equal(await evaluate('window.opens.length'),0);
  await click(evaluate,'Author');await click(evaluate,'Open curriculum');await evaluate('document.querySelector(".th-curriculum-row button").click()');await until('document.querySelector("dialog pre")?.textContent.includes("First file")');
  assert.equal(await evaluate('!!document.querySelector("dialog .cm-content")'),false);
 });
});

test('late old-account detail is discarded and a committed receipt closes only the successful source form',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.pauseDetail="activity:one"');await click(evaluate,'Explore');await until('typeof window.releaseDetail==="function"');
  await evaluate('window.switchAccount()');await evaluate('window.pauseDetail=null;window.releaseDetail()');assert.equal(await evaluate('!!document.querySelector(".th-lesson-body")'),false);
  await click(evaluate,'Author');await click(evaluate,'Open curriculum');await evaluate('document.querySelector(".th-curriculum-row button").click()');await until('!!document.querySelector(".cm-content")');
  await click(evaluate,'Save activity');await until('!document.querySelector("dialog")');
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="activity.update").at(-1).body.payload.contentBase'),'body:one');
  assert.equal(await evaluate('window.head'),'body:new');
 });
});

test('recipient course editor retains native text editing without access to owner Files source',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.recipient=true;window.canEdit=false;window.nativeEdit=true;window.denySource=true');
  await open(evaluate,until);assert.equal(await evaluate('[...document.querySelectorAll("button")].some(x=>x.textContent==="Edit source")'),false);
  assert.equal(await evaluate('[...document.querySelectorAll("button")].find(x=>x.textContent==="View source").disabled'),true);
  await click(evaluate,'Author');await click(evaluate,'Open curriculum');await evaluate('document.querySelector(".th-curriculum-row button").click()');await until('!!document.querySelector(".cm-content")');
  await click(evaluate,'Save activity');await until('!document.querySelector("dialog")');assert.equal(await evaluate('window.opens.length'),0);assert.equal(await evaluate('window.head'),'body:new');
 });
});

test('private quiz draft and final attempt pin the displayed work file head without losing choices',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.quiz=true');await open(evaluate,until);await evaluate('document.querySelectorAll(".th-index-step")[2].click()');await until('document.querySelector(".th-lesson-body")?.textContent.includes("Explain the supplied file")');
  await click(evaluate,'Submit work');await until('!!document.querySelector("dialog fieldset input")');await evaluate('document.querySelector("dialog fieldset input[value=two]").click();window.head="work:external-key-edit"');
  await click(evaluate,'Save draft');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("stale_work_source")');
  assert.equal(await evaluate('document.querySelector("dialog fieldset input[value=two]").checked'),true);
  await click(evaluate,'Submit attempt');await until('window.calls.filter(x=>x.body?.type==="submission.submit").length===1');
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="submission.submit").at(-1).body.payload.workBase'),'body:one');assert.equal(await evaluate('!!document.querySelector("dialog")'),true);
  await evaluate('window.head="body:one"');await click(evaluate,'Submit attempt');await until('!document.querySelector("dialog")');
 });
});

test('private review pins the displayed rubric work head and retains feedback after an external edit',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.reviewing=true;window.reload()');await until('window.feature.snapshot.state.submissions["assignment:one:a"]');
  await click(evaluate,'Author');await click(evaluate,'Assignments','.copal-treehouse-nav button');await click(evaluate,'a · submitted · attempt 1');await until('!!document.querySelector("dialog .th-submission-answer")');
  await evaluate('document.querySelector("dialog textarea").value="Feedback kept after conflict";window.head="work:changed-rubric"');await click(evaluate,'Review and grade');await until('document.querySelector("dialog [role=alert]")?.textContent.includes("stale_work_source")');
  assert.equal(await evaluate('document.querySelector("dialog textarea").value'),'Feedback kept after conflict');
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="submission.grade").at(-1).body.payload.workBase'),'body:one');
 });
});

test('mixed owner revisions use selected course revision for lazy detail and author CAS',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.aggregateRevision=88;window.reload()');await until('window.feature.snapshot.state.revision===88');
  assert.equal(await evaluate('window.feature.snapshot.state.courses["course:files"].curriculumRevision'),0);
  assert.equal(await evaluate('window.feature.snapshot.state.courses["course:other"].curriculumRevision'),88);
  await open(evaluate,until);assert.match(await evaluate('document.querySelector(".th-lesson-body").textContent'),/First file/);
  await click(evaluate,'Author');await click(evaluate,'Open curriculum');await evaluate('document.querySelector(".th-curriculum-row button").click()');await until('!!document.querySelector(".cm-content")');
  await click(evaluate,'Save activity');await until('!document.querySelector("dialog")');
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="activity.update").at(-1).body.expectedRevision'),0);
  assert.equal(await evaluate('window.calls.filter(x=>x.body?.type==="activity.update").at(-1).body.payload.courseId'),undefined);
  assert.equal(await evaluate('window.feature.snapshot.state.courses["course:other"].curriculumRevision'),88);
 });
});

test('mixed owner additive course creation uses null CAS without relaxing existing source writes',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await ready(evaluate,until);await evaluate('window.aggregateRevision=88;window.reload()');await until('window.feature.snapshot.state.revision===88');
  await click(evaluate,'Author');await click(evaluate,'Create Tutorial or Quest');await until('!!document.querySelector("dialog")');
  await evaluate('document.querySelector("dialog input").value="My new course"');await click(evaluate,'Save course');await until('!document.querySelector("dialog")');
  const request=await evaluate('window.calls.filter(x=>x.body?.type==="course.create").at(-1).body');assert.equal(request.expectedRevision,null);assert.equal(request.payload.title,'My new course');
  assert.equal(await evaluate('window.feature.snapshot.state.courses["course:other"].curriculumRevision'),88);
  assert.equal(await evaluate('window.calls.some(x=>x.body?.type==="activity.update")'),false);
 });
});
