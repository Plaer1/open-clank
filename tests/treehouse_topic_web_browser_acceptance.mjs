import test from 'node:test';
import assert from 'node:assert/strict';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const page=`<!doctype html><html><head><style>body{font-family:sans-serif;margin:20px;--border:#999;--panel:#fff;--text:#111}#treehouse{width:900px;height:850px;overflow:auto}</style></head><body><main id="treehouse" tabindex="-1"></main><script type="module">
try {
const {configureCopalStorage}=await import('/static/js/copal/storage.js');configureCopalStorage('global-topic-fixture');
const {createTreeHouseFeature}=await import('/static/js/copal/treehouse.js');
const {createMarkdownRenderer}=await import('/static/js/copal/markdownRenderer.js');
const h=(tag,attrs={},...children)=>{const n=document.createElement(tag);for(const [key,value]of Object.entries(attrs)){if(key==='text')n.textContent=value;else if(key.startsWith('on'))n.addEventListener(key.slice(2),value);else if(key==='class')n.className=value;else if(key==='value')n.value=value;else if(value!==false&&value!=null)n.setAttribute(key,value===true?'':value);}for(const child of children.flat())if(child!=null)n.append(child instanceof Node?child:document.createTextNode(String(child)));return n;};
const renderer=createMarkdownRenderer({h,documents:()=>[],openTarget(){}});window.__genericHasTopicAnchor=!!renderer.renderMarkdown('[topic](#treehouse-unit=ai%3Amodels%3Aidentity)').querySelector('a');
const sourceId='openclank:treehouse-core',packageRevision='fixture.1',courseId='ai:models',unitId='ai:models:identity',assignmentId='ai:models:assessment:identity';
const ref={kind:'system',sourceId,courseId};let revision=0,submission=null,resume=null;window.__digest='digest1';window.__generation=0;window.__serverState='current';
const progress=()=>({completed:submission?.status==='submitted'?1:0,total:1,complete:submission?.status==='submitted',requiredAssignmentIds:[assignmentId],missingAssignmentIds:submission?.status==='submitted'?[]:[assignmentId],resumeActivityId:resume});
const root={id:'treehouse:ai',title:'AI',requiredTotal:1,requiredComplete:0,electiveTotal:0,electiveComplete:0};
const catalogue={sourceId,packageRevision,mainQuests:[{id:'treehouse:programming',title:'Programming'}, {id:'treehouse:openclank',title:'Open Clank'}, {id:'treehouse:games',title:'Games'},root]};
const snapshot={accountId:'fixture-learner',workspace:'one',actor:{id:'fixture-learner',displayName:'Learner'},permissions:{learner:true},state:{revision:5,profiles:{'fixture-learner':{id:'fixture-learner',active:true,roles:['learner']}},courses:{},modules:{},activities:{},assignments:{},enrollments:{},submissions:{},courseGrants:{},events:[],evidence:{},skills:{},badges:{},quests:{}},projection:{learners:{'fixture-learner':{courses:{},points:0}}},globalCatalogue:catalogue};
window.__calls=[];window.__snap=snapshot;
const response=(fields={})=>({sourceRef:ref,packageRevision:catalogue.packageRevision,progress:progress(),progressRevision:revision,resetGeneration:window.__generation,workRequirements:{[assignmentId]:{unitId,requirementDigest:window.__digest,completionEvidence:'observed-success'},'ai:models:assessment:response':{unitId,requirementDigest:window.__digest,completionEvidence:'response'}},...fields});
const api=async(path,options)=>{window.__calls.push({path,body:options?.body});if(options){const command=JSON.parse(options.body);if(!snapshot.globalCatalogue||command.payload.courseId==='course:private')return {...snapshot,result:{}};if(command.type==='course.open')resume=command.payload.activityId;if(command.type==='progress.reset'){window.__generation++;submission=null;}if(command.type==='submission.draft'){submission={draftAnswer:command.payload.answer,status:'draft'};}if(command.type==='submission.submit'){const answer=command.payload.answer;submission={answer,status:answer.outcome.status==='success'&&answer.outcome.learnerConfirmed&&answer.outcome.evidenceText.trim()?'submitted':'pending'};}revision++;return response({ok:true,result:{completionSatisfied:submission?.status==='submitted'},globalCatalogue:catalogue});}if(path.includes('/activities/'))return response({subjectId:courseId,unit:{id:unitId,title:'Model identity',content:'- A model and its harness are separate. [Same topic](#treehouse-unit=ai%3Amodels%3Aidentity) [Unsafe](#treehouse-unit=javascript%3Aalert%281%29)',optionalSections:[{title:'More detail',content:'Optional depth.'}]},assignments:[{id:assignmentId,requirementDigest:window.__digest,prompt:'Run the owned toy and describe the observed result.',successConditions:['Report actual output.'],completionEvidence:'observed-success',verificationLimit:'Self-report, not verified execution.'}],submissions:submission?{[assignmentId]:{...submission,draftRecoveryStatus:window.__serverState,answerRecoveryStatus:window.__serverState}}:{},actionAvailability:{status:'available',unmetPrerequisites:[]}});if(path.includes('/summary'))return response({subject:{id:courseId,mainQuestId:'treehouse:ai',title:'Models'},units:[{id:unitId,title:'Model identity',assignmentIds:[assignmentId],topicLinks:[]}]});if(path.includes('/main-quests/'))return response({mainQuest:root,subjects:[{id:courseId,title:'Models',mainQuestId:root.id,objective:'Identify model and harness',required:true,sourceRef:ref,progress:progress(),progressRevision:revision,resetGeneration:0}]});return snapshot;};
window.styledConfirm=async()=>false;
window.__setSubmission=value=>submission=value;window.__catalogue=catalogue;window.__feature=createTreeHouseFeature({h,api,setStatus:(message)=>window.__status=message,renderMarkdown:renderer.renderMarkdown,openDocument(){},getScope:()=>({owner:'fixture-learner',workspace:'one'})});
await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;
}catch(e){window.__error=e.stack;}
</script></body></html>`;
test('mounted global hub loads one unit, preserves drafts, renders honest attempts and optional depth',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  assert.deepEqual(await evaluate('[...document.querySelectorAll("[data-main-quest]")].map(n=>n.dataset.mainQuest)'),['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'],await evaluate('document.body.innerText'));
  assert.equal(await evaluate('window.__calls.filter(c=>c.path.includes("/activities/")).length'),0);
  await evaluate('document.querySelector("[data-main-quest=\\"treehouse:ai\\"]").click()');
  await until('document.querySelector("[data-subject-id]")');
  await evaluate('document.querySelector("[data-subject-id]").click()');
  await until('document.querySelector("[data-treehouse-unit]")');
  assert.equal(await evaluate('window.__calls.filter(c=>c.path.includes("/activities/")).length'),1);
  assert.equal(await evaluate('document.querySelector(".th-web-unit").innerText.includes("Run the owned toy")'),true);
  assert.equal(await evaluate('document.querySelector(".th-web-disclosure").open'),false);
  await evaluate('var input=document.querySelector("textarea[aria-label=\\"Your response\\"]");input.value="Compiler rejected this input";input.dispatchEvent(new Event("input"));document.querySelector("select[aria-label=\\"Practical outcome\\"]").value="failed";[...document.querySelectorAll("button")].find(n=>n.textContent==="Save attempt").click()');
  await until('document.querySelector(".th-web-work").innerText.includes("practical work pending")');
  assert.equal(await evaluate('document.querySelector(".th-web-progress").textContent.includes("1/1")'),false);
  assert.equal(await evaluate('window.__feature.snapshot.state.revision'),5,'global command must not replace private snapshot');
  await evaluate('var input=document.querySelector("textarea[aria-label=\\"Your response\\"]");input.value="Unsaved next draft";input.dispatchEvent(new Event("input"));document.querySelector(".th-web-outline button").click()');
  await until('document.querySelector("textarea[aria-label=\\"Your response\\"]")?.value === "Unsaved next draft"');
  await evaluate('document.querySelector("select[aria-label=\\"Practical outcome\\"]").value="success";document.querySelector("input[type=checkbox]").checked=true;document.querySelector("textarea[aria-label=\\"Outcome evidence\\"]").value="Actual output 7";[...document.querySelectorAll("button")].find(n=>n.textContent==="Save attempt").click()');
  await until('document.querySelector(".th-web-work").innerText.includes("Submitted · self-reported work")');
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-progress")].some(n=>n.textContent.includes("1/1 work items"))'),true);
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-unit button")].some(n=>/Record|Next step|Complete lesson/.test(n.textContent))'),false);
 });
});

const privatePage=page.replace("await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;", `snapshot.globalCatalogue=null;
snapshot.state.courses['course:private']={id:'course:private',title:'Private tutorial',courseType:'tutorial',status:'published',moduleIds:['module:private'],freeExploration:true};
snapshot.state.modules['module:private']={id:'module:private',title:'My module',activityIds:['activity:private'],assignmentIds:[]};
snapshot.state.activities['activity:private']={id:'activity:private',moduleId:'module:private',courseId:'course:private',title:'Private lesson',activityType:'markdown',status:'published',content:'Private body remains on the account path.'};
await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;`);
test('account-owned Tutorial keeps its existing learner navigation without a global catalogue',async()=>{
 await withCopalBrowser({page:privatePage},async({evaluate,until})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),0);
  await evaluate('[...document.querySelectorAll("button")].find(n=>n.textContent==="Explore").click()');
  await until('document.body.innerText.includes("Private body remains")');
  assert.equal(await evaluate('[...document.querySelectorAll("button")].some(n=>n.textContent==="Finish Tutorial")'),true);
  assert.equal(await evaluate('window.__calls.filter(c=>c.path.includes("/summary")||c.path.includes("/activities/")).length'),0);
 });
});

const gatedPage=page.replace("completionEvidence:'observed-success',verificationLimit", "completionEvidence:'observed-success',actionAvailability:{status:'blocked',unmetPrerequisites:[{targetId:'cs:values:values-types',targetSubjectId:'cs:values',targetTitle:'Values',reason:'Required for practical success'}]},verificationLimit").replace("assignments:[{id:assignmentId", "assignments:[{id:'ai:models:assessment:response',requirementDigest:window.__digest,prompt:'Explain model identity.',successConditions:['Reason about the model.'],completionEvidence:'response',actionAvailability:{status:'available'}},{id:assignmentId").replace("actionAvailability:{status:'available',unmetPrerequisites:[]}","actionAvailability:{status:'blocked',unmetPrerequisites:[{targetId:'cs:values:values-types',targetSubjectId:'cs:values',targetTitle:'Values',reason:'Required for practical success'}]}");
test('concept responses and failed attempts remain open while practical success prerequisites apply',async()=>{
 await withCopalBrowser({page:gatedPage},async({evaluate,until,cdp})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate('[...document.querySelectorAll("[data-main-quest]")].find(n=>n.dataset.mainQuest==="treehouse:ai").click()');await until('document.querySelector("[data-subject-id]")');await evaluate('document.querySelector("[data-subject-id]").click()');await until('document.querySelectorAll(".th-web-work").length===2');
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work button")].find(n=>n.textContent==="Submit response").disabled'),false);
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work button")].find(n=>n.textContent==="Save attempt").disabled'),false);
  await evaluate('var outcome=document.querySelector(".th-web-work select");outcome.value="success";outcome.dispatchEvent(new Event("change"));');
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work button")].find(n=>n.textContent==="Save attempt").disabled'),true);
  await evaluate('var outcome=document.querySelector(".th-web-work select");outcome.value="failed";outcome.dispatchEvent(new Event("change"));');
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work button")].find(n=>n.textContent==="Save attempt").disabled'),false);
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work button")].filter(n=>n.textContent==="Save draft").every(n=>!n.disabled)'),true);
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await evaluate('document.querySelector("#treehouse").style.width="100%";document.querySelector("textarea").focus();');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth+1'),true);
  assert.equal(await evaluate('document.activeElement.tagName'), 'TEXTAREA');
 });
});

const linkedPrivatePage=privatePage.replace("snapshot.globalCatalogue=null;", "").replace("activityIds:['activity:private']", "activityIds:['activity:private','activity:second']").replace("Private body remains on the account path.", "[Local](#treehouse-unit=activity%3Asecond) [Shared](#treehouse-unit=ai%3Amodels%3Aidentity) [Unknown](#treehouse-unit=activity%3Amissing)").replace("await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;", "snapshot.state.activities['activity:second']={...snapshot.state.activities['activity:private'],id:'activity:second',title:'Second private lesson',content:'Second private topic.'};await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;");
test('copied private topic anchors stay local or explicitly open a known shared topic',async()=>{
 await withCopalBrowser({page:linkedPrivatePage},async({evaluate,until})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate('[...document.querySelectorAll("button")].find(n=>n.textContent==="Private courses").click()');
  await evaluate('[...document.querySelectorAll("button")].find(n=>n.textContent==="Explore").click()');await until('[...document.querySelectorAll("a")].find(n=>n.getAttribute("href")?.includes("activity%3Asecond"))').catch(async e=>assert.fail(e.message+' '+JSON.stringify(await evaluate('({body:document.body.innerText,status:window.__status,html:document.querySelector(".th-lesson")?.innerHTML})'))));
  await evaluate('[...document.querySelectorAll("a")].find(n=>n.getAttribute("href")?.includes("activity%3Amissing")).click()');assert.equal(await evaluate('document.querySelector(".th-lesson h2").textContent'),'Private lesson');
  await evaluate('[...document.querySelectorAll("a")].find(n=>n.getAttribute("href")?.includes("activity%3Asecond")).click()');await until('document.body.innerText.includes("Second private topic.")');
  assert.equal(await evaluate('window.__calls.some(c=>c.path.includes("/summary"))'),false);
  await evaluate('[...document.querySelectorAll(".th-index-step")].find(n=>n.innerText.includes("Private lesson")).click()');await until('[...document.querySelectorAll("a")].find(n=>n.getAttribute("href")?.includes("ai%3Amodels"))');
  await evaluate('[...document.querySelectorAll("a")].find(n=>n.getAttribute("href")?.includes("ai%3Amodels")).click()');await until('document.querySelector(".th-web-unit")');
  assert.equal(await evaluate('document.querySelector("[data-treehouse-unit]").dataset.treehouseUnit'),'ai:models:identity');
  assert.equal(await evaluate('window.__genericHasTopicAnchor'),false);
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-unit a")].some(n=>n.textContent==="Unsafe")'),false);
  const opens=await evaluate('window.__calls.filter(c=>c.body&&JSON.parse(c.body).type==="course.open").length');
  await evaluate('[...document.querySelectorAll(".th-web-unit a")].find(n=>n.textContent==="Same topic").click()');
  await until(`window.__calls.filter(c=>c.body&&JSON.parse(c.body).type==="course.open").length > ${opens}`);

 });
});

const practicalPrivatePage=page.replace("await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;", `const {openTreeHouseSubmission}=await import('/static/js/copal/treehouseAssessment.js');
const privateAssignment={id:'private:practice',courseId:'private:course',title:'Owned practical task',assessmentType:'text',completionEvidence:'observed-success',prompt:'Try an owned task and report actual evidence.',successConditions:['State the observed result.'],graded:false};
const ui={snapshot,actorId:'fixture-learner'};window.__privateSends=[];
const ctx={h,ui,renderMarkdown:renderer.renderMarkdown,learnerProjection:()=>({courses:{}}),command:async(type,payload)=>{window.__privateSends.push({type,payload});snapshot.state.submissions['private:practice:fixture-learner']={answer:payload.answer,status:payload.answer.outcome.status==='success'?'submitted':'pending'};return true;}};
window.__openPrivate=()=>openTreeHouseSubmission(privateAssignment,ctx);window.__openPrivate();window.__ready=true;`);
test('private observed-success task sends structured pending attempts and requires actual success evidence',async()=>{
 await withCopalBrowser({page:practicalPrivatePage},async({evaluate,until})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate('document.querySelectorAll("dialog textarea")[0].value="My owned attempt failed.";var outcome=document.querySelector("dialog select");outcome.value="success";outcome.dispatchEvent(new Event("change"));[...document.querySelectorAll("dialog button")].find(n=>n.textContent==="Submit attempt").click()');
  await until('document.querySelector("dialog [role=alert]").textContent.includes("actual evidence")');assert.equal(await evaluate('window.__privateSends.length'),0);
  await evaluate('var outcome=document.querySelector("dialog select");outcome.value="failed";outcome.dispatchEvent(new Event("change"));document.querySelectorAll("dialog textarea")[1].value="Compiler returned an error; no success claimed.";[...document.querySelectorAll("dialog button")].find(n=>n.textContent==="Submit attempt").click()');
  await until('!document.querySelector("dialog")');
  const sent=await evaluate('window.__privateSends[0]');assert.equal(sent.type,'submission.submit');assert.equal(sent.payload.answer.outcome.status,'failed');assert.equal(sent.payload.answer.outcome.learnerConfirmed,false);assert.match(sent.payload.answer.outcome.evidenceText,/no success/);
  await evaluate('window.__openPrivate()');assert.equal(await evaluate('document.querySelector("dialog select").value'),'failed');assert.equal(await evaluate('document.querySelectorAll("dialog textarea")[0].value'),'My owned attempt failed.');
 });
});


const recoveryPage=`<!doctype html><html><body><main id="root"></main><script type="module">
const {createTreeHouseTopicWeb}=await import('/static/js/copal/treehouseTopicWeb.js');
const h=(tag,attrs={},...children)=>{const n=document.createElement(tag);for(const[k,v]of Object.entries(attrs)){if(k==='text')n.textContent=v;else if(k.startsWith('on'))n.addEventListener(k.slice(2),v);else if(k==='value')n.value=v;else if(v!==false&&v!=null)n.setAttribute(k,v===true?'':v);}for(const c of children.flat())if(c!=null)n.append(c instanceof Node?c:document.createTextNode(String(c)));return n;};
const sourceId='openclank:treehouse-core';const roots=['programming','games','ai','openclank'].map(id=>({id:'treehouse:'+id,title:id}));
const ui={token:1,actorId:'A',snapshot:{accountId:'A',workspace:'one',globalCatalogue:{sourceId,packageRevision:'one',mainQuests:roots}}};
const state={digest:'requirement1',generation:0,revision:0,submissions:{},resources:[],paused:null};window.__state=state;window.__ui=ui;window.__calls=[];
const refs=courseId=>({kind:'system',sourceId,courseId});const unit=id=>id+':one';const aid=id=>id+':assessment:one';
const progress=id=>({completed:0,total:1,requiredAssignmentIds:[aid(id)]});
const reply=(id,fields={})=>({sourceRef:refs(id),packageRevision:ui.snapshot.globalCatalogue.packageRevision,progress:progress(id),progressRevision:state.revision,resetGeneration:state.generations?.[id]??state.generation,...fields});
const summary=id=>reply(id,{subject:{id,mainQuestId:id==='ai:openclank-files'?'treehouse:openclank':'treehouse:programming',title:id},units:[{id:unit(id),title:'One'}],workRequirements:{[aid(id)]:{unitId:unit(id),requirementDigest:state.digest,completionEvidence:'observed-success'}}});
const api=async(path,options)=>{
 if(options){const command=JSON.parse(options.body);window.__calls.push(command);if(command.type==='progress.reset'){const id=command.payload.courseId;state.generations={...state.generations,[id]:(state.generations?.[id]??state.generation)+1};delete state.submissions[id];}state.revision++;return reply(command.payload.courseId,{ok:true,result:{},globalCatalogue:ui.snapshot.globalCatalogue});}
 if(path.includes('/main-quests/'))return reply(null,{mainQuest:roots[0],subjects:['cs:a','cs:b'].map(id=>({id,progress:progress(id),progressRevision:state.revision,resetGeneration:state.generation}))});
 const id=decodeURIComponent(path.split('/courses/')[1].split('/')[0]);
 if(path.includes('/summary')){if(state.paused){const p=state.paused;state.paused=null;await p;}return summary(id);}
 return reply(id,{subjectId:id,unit:{id:unit(id),title:'One',content:'Explain the supplied practice.',resourceIds:state.resources.map(x=>x.id)},assignments:[{id:aid(id),requirementDigest:state.digest,prompt:'Describe an actual owned attempt.',completionEvidence:'observed-success',successConditions:['Distinguish prediction from actual result.']}],submissions:state.submissions[id]?{[aid(id)]:state.submissions[id]}:{},resources:state.resources});
};
const root=document.querySelector('#root');const ctx={h,ui,api,styledConfirm:(...args)=>window.__confirm?window.__confirm(...args):Promise.resolve(true),setStatus:message=>window.__status=message,renderMarkdown:text=>h('p',{text}),renderLoaded(){root.replaceChildren();controller.render(root);}};
const controller=createTreeHouseTopicWeb(()=>ctx);window.__controller=controller;window.__render=ctx.renderLoaded;await controller.openSubject('cs:a');window.__ready=true;
</script></body></html>`;
const responseValue='document.querySelector("textarea[aria-label=\\"Your response\\"]").value';
const fillRecovery=`var answer=document.querySelector('textarea[aria-label="Your response"]');answer.value="A reasoned old answer";answer.dispatchEvent(new Event('input'));`;
test('frozen rendered form rejects package/generation changes; equivalent buffers recover and changed answers require explicit copy',async()=>{
 await withCopalBrowser({page:recoveryPage},async({evaluate,until})=>{
  await until('window.__ready');await evaluate(fillRecovery);
  await evaluate(`window.__oldSave=[...document.querySelectorAll('button')].find(n=>n.textContent==='Save draft');window.__ui.snapshot.globalCatalogue.packageRevision='two';window.__oldSave.click();`);
  await until('window.__status?.includes("task changed")');
  assert.equal(await evaluate('window.__calls.filter(c=>c.type.startsWith("submission.")).length'),0);
  await evaluate('window.__controller.openSubject("cs:a",null,true)');
  assert.equal(await evaluate(responseValue),'A reasoned old answer');
  assert.equal(await evaluate('document.body.innerText.includes("Recovered unsent work")'),true);
  await evaluate(`window.__ui.snapshot.globalCatalogue.packageRevision='three';window.__state.digest='requirement2';window.__controller.openSubject('cs:a',null,true)`);
  assert.equal(await evaluate(responseValue),'');
  assert.equal(await evaluate('document.querySelector("textarea[aria-label=\\"Previous answer\\"]").value'),'A reasoned old answer');
  await evaluate(`[...document.querySelectorAll('button')].find(n=>n.textContent==='Copy previous answer').click()`);
  assert.equal(await evaluate(responseValue),'A reasoned old answer');
  assert.equal(await evaluate('document.querySelector("input[type=checkbox]").checked'),false);
  await evaluate(`[...document.querySelectorAll('button')].find(n=>n.textContent==='Save draft').click()`);
  await until('window.__calls.some(c=>c.type==="submission.draft")');
  assert.equal(await evaluate('window.__calls.find(c=>c.type==="submission.draft").payload.assignmentRequirementDigest'),'requirement2');
  // A fresh CAS lookup must not silently rebase the rendered reset generation.
  await evaluate(`window.__state.generation=1;[...document.querySelectorAll('button')].find(n=>n.textContent==='Save draft').click()`);
  await until('window.__status?.includes("reset scope changed")');
  assert.equal(await evaluate('window.__calls.filter(c=>c.type==="submission.draft").length'),1);
  await evaluate('window.__controller.openSubject("cs:a",null,true)');
  assert.equal(await evaluate(responseValue),'');
  assert.equal(await evaluate('!!document.querySelector(".th-web-previous")'),true);
 });
});

test('server draft identity, scoped reset, reparented subject resume and account boundaries stay separate',async()=>{
 await withCopalBrowser({page:recoveryPage},async({evaluate,until})=>{
  await until('window.__ready');
  await evaluate(`window.__state.submissions['cs:a']={draftAnswer:'legacy saved text',draftRecoveryStatus:'unknown',status:'draft'};window.__controller.openSubject('cs:a',null,true)`);
  assert.equal(await evaluate(responseValue),'');assert.equal(await evaluate('!!document.querySelector(".th-web-previous")'),true);
  await evaluate(`[...document.querySelectorAll('button')].find(n=>n.textContent==='Discard previous answer').click()`);
  assert.equal(await evaluate('!!document.querySelector(".th-web-previous")'),false);
  await evaluate(`window.__state.submissions['cs:a']={draftAnswer:'current saved text',draftRecoveryStatus:'current',status:'draft'};window.__controller.openSubject('cs:a',null,true)`);
  assert.equal(await evaluate(responseValue),'current saved text');
  await evaluate(fillRecovery);
  await evaluate(`window.__controller.openSubject('cs:b')`);await evaluate(fillRecovery.replace('A reasoned old answer','B unsent answer'));
  await evaluate(`window.__controller.reset('subject','cs:a')`);
  // Per-subject fixture epoch advances A only, so B remains current.
  await evaluate(`window.__controller.openSubject('cs:b',null,true)`);
  assert.equal(await evaluate(responseValue),'B unsent answer');
  await evaluate(`window.__state.submissions['cs:a']=null;window.__controller.openSubject('cs:a',null,true)`);
  assert.equal(await evaluate(responseValue),'');assert.equal(await evaluate('!!document.querySelector(".th-web-previous")'),false);
  await evaluate(`window.__controller.openSubject('ai:openclank-files')`);
  assert.equal(await evaluate('window.__controller.selected.rootId'),'treehouse:openclank');
  await evaluate(fillRecovery);
  await evaluate(`window.__ui.snapshot.accountId='B';window.__ui.actorId='B';window.__ui.token++;window.__controller.openSubject('ai:openclank-files',null,true)`);
  assert.equal(await evaluate(responseValue),'');assert.equal(await evaluate('!!document.querySelector(".th-web-previous")'),false);
  await evaluate(`window.__ui.snapshot.accountId='A';window.__ui.actorId='A';window.__ui.token++;window.__controller.openSubject('ai:openclank-files',null,true)`);
  assert.equal(await evaluate(responseValue),'','account changes destroy unsent principal buffers');
 });
});

test('public resource downloads verify bytes/digest and reject active preview, foreign and stale associations',async()=>{
 const {createHash}=await import('node:crypto');const seed='supplied input\n',digest='sha256:'+createHash('sha256').update(seed).digest('hex');
 await withCopalBrowser({page:recoveryPage,request:async(req,res)=>{if(req.url==='/static/treehouse/fixtures/input.txt'){res.setHeader('Content-Type','text/plain');res.end(seed);return true;}return false;}},async({evaluate,until})=>{
  await until('window.__ready');
  const item={id:'seed',url:'/static/treehouse/fixtures/input.txt',path:'static/treehouse/fixtures/input.txt',mime:'text/plain',bytes:Buffer.byteLength(seed),digest,unitIds:['cs:a:one'],kind:'data',provenance:{status:'source-only'}};
  await evaluate(`window.__state.resources=${JSON.stringify([item,{...item,id:'wrong-digest',digest:'sha256:'+'0'.repeat(64)},{...item,id:'foreign',url:'https://example.com/static/treehouse/fixtures/input.txt'},{...item,id:'active',url:'/static/treehouse/fixtures/input.html'},{...item,id:'other-unit',unitIds:['cs:b:one']}])};window.__controller.openSubject('cs:a',null,true)`);
  assert.equal(await evaluate('document.querySelectorAll(".th-web-resources section button").length'),4,'only two admitted-looking records offer copy/download');
  await evaluate(`document.querySelector('.th-web-resources').open=true;[...document.querySelectorAll('.th-web-resources section')][0].querySelectorAll('button')[1].click()`);
  await until('document.querySelector(".th-web-resources a[download]")');
  assert.equal(await evaluate('document.querySelector(".th-web-resources a[download]").getAttribute("download")'),'input.txt');
  await evaluate(`[...document.querySelectorAll('.th-web-resources section')][1].querySelectorAll('button')[1].click()`);
  await until('[...document.querySelectorAll(".th-web-resources section")][1].innerText.includes("unavailable or changed")');
  assert.equal(await evaluate('[...document.querySelectorAll(".th-web-resources section")][1].querySelectorAll("a").length'),0);
  const before=await evaluate('window.__calls.filter(c=>c.type.startsWith("submission.")).length');
  await evaluate(`window.__heldFetch=window.fetch;window.fetch=async(...args)=>{const reply=await window.__heldFetch(...args);await new Promise(resolve=>window.__releaseResource=resolve);return reply;};[...document.querySelectorAll('.th-web-resources section')][0].querySelectorAll('button')[1].click()`);
  await until('!!window.__releaseResource');
  await evaluate(`window.__ui.snapshot.accountId='B';window.__ui.token++;window.__render();window.__releaseResource()`);
  assert.equal(await evaluate('document.querySelectorAll(".th-web-resources a").length'),0);
  assert.equal(await evaluate('window.__calls.filter(c=>c.type.startsWith("submission.")).length'),before);
 });
});


test('source changes cannot submit an old form or recover a foreign source buffer',async()=>{
 await withCopalBrowser({page:recoveryPage},async({evaluate,until})=>{
  await until('window.__ready');await evaluate(fillRecovery);
  await evaluate(`window.__oldSubmit=[...document.querySelectorAll('button')].find(n=>n.textContent==='Save draft');window.__ui.snapshot.globalCatalogue.sourceId='foreign:curriculum';window.__oldSubmit.click();`);
  await until('window.__status?.includes("task changed")');
  assert.equal(await evaluate('window.__calls.filter(c=>c.type.startsWith("submission.")).length'),0);
  assert.equal(await evaluate('document.querySelectorAll("textarea").length'),0);
 });
});


test('mounted subject reset rejects work saved or reset while its confirmation is open',async()=>{
 await withCopalBrowser({page:recoveryPage},async({evaluate,until})=>{
  await until('window.__ready');
  for(const next of [{revision:4,generation:1},{revision:5,generation:2}]){
   await evaluate(`window.__state.revision=3;window.__state.generation=1;window.__confirm=()=>new Promise(resolve=>window.__acceptReset=resolve);window.__status='';[...document.querySelectorAll('button')].find(n=>n.textContent==='Reset this Quest').click()`);
   await until('typeof window.__acceptReset==="function"');
   await evaluate(`window.__state.revision=${next.revision};window.__state.generation=${next.generation};window.__acceptReset(true);window.__acceptReset=null;`);
   await until('window.__status.includes("Progress changed since this reset was confirmed")');
   assert.equal(await evaluate('window.__calls.filter(c=>c.type==="progress.reset").length'),0);
   assert.equal(await evaluate('window.__state.revision'),next.revision);assert.equal(await evaluate('window.__state.generation'),next.generation);
  }
  await evaluate(`window.__confirm=null;[...document.querySelectorAll('button')].find(n=>n.textContent==='Reset this Quest').click()`);
  await until('window.__calls.some(c=>c.type==="progress.reset")');
  const sent=await evaluate('window.__calls.find(c=>c.type==="progress.reset")');assert.equal(sent.expectedRevision,5);assert.equal(sent.payload.resetGeneration,2);
 });
});


const importedPreviewPage=page.replace("openTarget(){}", "openTarget(){window.__unexpectedWikiOpen=(window.__unexpectedWikiOpen||0)+1;}").replace("const api=async(path,options)=>{window.__calls.push({path,body:options?.body});", "const api=async(path,options)=>{window.__calls.push({path,body:options?.body});if(path.includes('/learner-preview'))return {...snapshot,preview:{revision:5}};").replace("await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;", `
snapshot.globalCatalogue=null;snapshot.permissions.author=true;snapshot.state.profiles['fixture-learner'].roles.push('author');
const course='course:imported:import-45cc97602598',module='module:imported:import-45cc97602598';
const first='activity:first:import-45cc97602598',second='activity:second:import-45cc97602598',hidden='activity:hidden:import-45cc97602598';
snapshot.courseCapabilities={[course]:{edit:true,owner:true}};
snapshot.state.courses[course]={id:course,title:'Imported notes',courseType:'quest',status:'draft',moduleIds:[module],freeExploration:true};
snapshot.state.modules[module]={id:module,courseId:course,title:'Notes',activityIds:[first,second,hidden,'activity:code'],assignmentIds:[]};
snapshot.state.activities[first]={id:first,moduleId:module,courseId:course,title:'First note',activityType:'markdown',status:'published',content:'First body. [Next](#treehouse-unit='+encodeURIComponent(second)+') [Foreign](#treehouse-unit=ai%3Amodels%3Aidentity) [Hidden](#treehouse-unit='+encodeURIComponent(hidden)+') [Malformed](#treehouse-unit=%ZZ) [Wiki](Other-note) [App](clank://editor)'};
snapshot.state.activities[second]={id:second,moduleId:module,courseId:course,title:'Second note',activityType:'markdown',status:'published',content:'Second body. [Back](#treehouse-unit='+encodeURIComponent(first)+')'};
snapshot.state.activities[hidden]={id:hidden,moduleId:module,courseId:course,title:'Hidden draft',activityType:'markdown',status:'draft',content:'Must stay absent.'};
snapshot.state.activities['activity:code']={id:'activity:code',moduleId:module,courseId:course,title:'Inert code',activityType:'code',status:'published',adapter:{source:'return 7;'}};
window.__previewIds={first,second,course};
await window.__feature.render(document.querySelector('#treehouse'));window.__ready=true;`);
for(const [courseStatus,scopeChange] of [['draft',null],['draft','account'],['draft','workspace'],['published',null]])test(courseStatus+' native import-shaped preview links navigate locally without progress'+(scopeChange?' and reject changed '+scopeChange:''),async()=>{
 await withCopalBrowser({page:courseStatus==='draft'?importedPreviewPage:importedPreviewPage.replace("courseType:'quest',status:'draft'","courseType:'quest',status:'published'")},async({cdp,evaluate,until})=>{
  await until('window.__ready||window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate(`[...document.querySelectorAll('[aria-label="TreeHouse mode"] button')].find(n=>/Author/.test(n.textContent)).click()`);
  await until('[...document.querySelectorAll("button")].some(n=>n.textContent==="Open curriculum")');
  await evaluate(`[...document.querySelectorAll('button')].find(n=>n.textContent==='Open curriculum').click()`);
  await until('[...document.querySelectorAll("button")].some(n=>n.textContent==="Learner preview")');
  await evaluate(`[...document.querySelectorAll('button')].find(n=>n.textContent==='Learner preview').click()`);
  await until('document.querySelector(".copal-treehouse-detail")');
  if(courseStatus==='draft')await until('window.__calls.some(c=>c.path.includes("learner-preview")&&c.body)');
  assert.equal(await evaluate('document.querySelector(".copal-treehouse-detail").innerText.includes("Missing: #treehouse-unit=activity")'),false);
  assert.equal(await evaluate('document.querySelector(".copal-treehouse-detail").innerText.includes("Must stay absent")'),false);
  const count=await evaluate('window.__calls.length');
  if(scopeChange) {
   await evaluate(`window.__snap[${JSON.stringify(scopeChange==='account'?'accountId':'workspace')}]='other'`);
   await evaluate(`document.querySelector('.copal-treehouse-detail a').click()`);
   assert.notEqual(await evaluate('document.activeElement.dataset.treehouseId'),await evaluate('window.__previewIds.second'));
  } else {
   // Native Enter activation through the actual shared markdown renderer.
   await evaluate(`document.querySelector('.copal-treehouse-detail a').focus()`);
   await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
   assert.equal(await evaluate('document.activeElement.dataset.treehouseId'),await evaluate('window.__previewIds.second'));
   await evaluate(`[...document.querySelectorAll('.copal-treehouse-detail a')].find(a=>a.textContent==='Back').click()`);
   assert.equal(await evaluate('document.activeElement.dataset.treehouseId'),await evaluate('window.__previewIds.first'));
   await evaluate(`[...document.querySelectorAll('.copal-treehouse-detail a')].filter(a=>['Foreign','Hidden'].includes(a.textContent)).forEach(a=>a.click());[...document.querySelectorAll('.copal-treehouse-detail button')].filter(b=>b.textContent!=='Back to authoring').forEach(b=>b.click());`);
   assert.equal(await evaluate('document.activeElement.dataset.treehouseId'),await evaluate('window.__previewIds.first'));
   assert.equal(await evaluate('!!window.__unexpectedWikiOpen'),false);
   assert.equal(await evaluate('document.querySelector(".copal-treehouse-detail iframe")'),null);
   assert.equal(await evaluate(`[...document.querySelectorAll('.copal-treehouse-detail button')].find(b=>b.textContent==='Run JavaScript').closest('[inert]')!==null`),true);
   assert.equal(await evaluate(`[...document.querySelectorAll('.copal-treehouse-detail a')].filter(a=>['Foreign','Hidden'].includes(a.textContent)).every(a=>a.inert)`),true);
  }
  assert.equal(await evaluate('window.__calls.length'),count,'Preview topic navigation never dispatches course.open, progress or shared lookup');
  assert.equal(await evaluate('window.__calls.filter(c=>c.path.includes("learner-preview")&&c.body).length'),courseStatus==='draft'?1:0,'Only the existing draft preview visibility receipt is posted');
 });
});


test('checked resources accept gzip wire length while enforcing decoded bounds and hashes',async()=>{
 const {gzipSync}=await import('node:zlib');const {createHash}=await import('node:crypto');
 const seed='small supplied resource\n'.repeat(40),bytes=Buffer.byteLength(seed),compressed=gzipSync(seed),digest='sha256:'+createHash('sha256').update(seed).digest('hex');
 assert.notEqual(compressed.length,bytes);
 await withCopalBrowser({page:recoveryPage,request:async(req,res)=>{
  if(req.url==='/static/treehouse/fixtures/encoded.txt'){res.setHeader('Content-Type','text/plain');res.setHeader('Content-Encoding','gzip');res.setHeader('Content-Length',compressed.length);res.end(compressed);return true;}
  if(req.url==='/static/treehouse/fixtures/identity.txt'){res.setHeader('Content-Type','text/plain');res.setHeader('Content-Length',bytes);res.end(seed);return true;}
  if(req.url==='/static/treehouse/fixtures/malformed.txt'){res.setHeader('Content-Type','text/plain');res.setHeader('Content-Encoding','gzip');res.end('invalid gzip');return true;}return false;
 }},async({evaluate,until})=>{
  await until('window.__ready');
  const item={id:'gzip',url:'/static/treehouse/fixtures/encoded.txt',mime:'text/plain',bytes,digest,unitIds:['cs:a:one'],kind:'data'};
  const records=[item,{...item,id:'tampered',digest:'sha256:'+'0'.repeat(64)},{...item,id:'decoded-overflow',bytes:bytes-1},{...item,id:'identity-mismatch',url:'/static/treehouse/fixtures/identity.txt',bytes:bytes+1},{...item,id:'malformed',url:'/static/treehouse/fixtures/malformed.txt'}];
  await evaluate(`window.__state.resources=${JSON.stringify(records)};window.__controller.openSubject('cs:a',null,true)`);
  await evaluate(`document.querySelector('.th-web-resources').open=true;document.querySelectorAll('.th-web-resources section')[0].querySelectorAll('button')[1].click()`);
  await until('document.querySelector(".th-web-resources a[download]")');
  assert.equal(await evaluate('document.querySelector(".th-web-resources a[download]").download'),'encoded.txt');
  for(let i=1;i<records.length;i++){
   await evaluate(`document.querySelectorAll('.th-web-resources section')[${i}].querySelectorAll('button')[1].click()`);
   await until(`document.querySelectorAll('.th-web-resources section')[${i}].innerText.includes('unavailable or changed')`);
   assert.equal(await evaluate(`document.querySelectorAll('.th-web-resources section')[${i}].querySelectorAll('a').length`),0,records[i].id);
  }
  assert.equal(await evaluate('window.__calls.filter(c=>c.type.startsWith("submission.")).length'),0);
 });
});


const availabilityPage=privatePage.replace("snapshot.globalCatalogue=null;", "snapshot.globalAvailability={status:'activation-required',code:'global_progress_activation_required'};snapshot.globalCatalogue=null;window.__helpCalls=[];window.openClankHandbook=async options=>window.__helpCalls.push(options);");
for(const status of ['activation-required','incompatible'])test('shared '+status+' preserves private learning and rechecks readiness without migration',async()=>{
 await withCopalBrowser({page:availabilityPage},async({evaluate,until,cdp})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate(`window.__snap.globalAvailability.status=${JSON.stringify(status)};window.__feature.render(document.querySelector('#treehouse'))`);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),0);
  assert.match(await evaluate('document.querySelector(".th-global-availability").innerText'),status==='incompatible'?/incompatible/:/need activation/);
  assert.match(await evaluate('document.querySelector(".th-global-availability").innerText'),/Private courses remain available/);
  await evaluate(`[...document.querySelectorAll('.th-global-availability button')].find(b=>b.textContent==='Open upgrade help').focus()`);
  await cdp('Page.bringToFront');
  assert.equal(await evaluate('document.activeElement.textContent'),'Open upgrade help');
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await until('window.__helpCalls.length===1');
  assert.deepEqual(await evaluate('window.__helpCalls'),[{presentation:'wiki'}]);
  await evaluate(`[...document.querySelectorAll('button')].find(b=>b.textContent==='Explore').click()`);await until('document.body.innerText.includes("Private body remains")');
  assert.equal(await evaluate('window.__calls.some(c=>c.path.includes("/main-quests/")||c.path.includes("/summary")||c.path.includes("/activities/"))'),false);
  await evaluate(`window.__snap.globalAvailability={status:'ready',code:null};window.__snap.globalCatalogue=window.__catalogue;[...document.querySelectorAll('.th-global-availability button')].find(b=>b.textContent==='Recheck shared quests').click()`);
  await until('!document.querySelector(".th-global-availability")');
  assert.equal(await evaluate('window.__calls.some(c=>c.body&&/activate|migrat|retire/.test(JSON.parse(c.body).type))'),false);
 });
});

for(const status of ['activation-required','incompatible'])test('private command explicit '+status+' clears ready catalogue while omitted fields stay intact',async()=>{
 const partial=privatePage.replace('snapshot.globalCatalogue=null;','').replace('if(options){const command=', 'if(options&&window.__privateReply)return window.__privateReply;if(options){const command=');
 await withCopalBrowser({page:partial},async({evaluate,until})=>{
  await until('window.__ready || window.__error');assert.equal(await evaluate('window.__error'),undefined);
  await evaluate(`window.__snap.globalAvailability={status:'ready',code:null};window.__privateReply={...window.__snap};delete window.__privateReply.globalCatalogue;delete window.__privateReply.globalAvailability;window.__feature.command('course.update',{courseId:'course:private'})`);
  assert.equal(await evaluate('!!window.__feature.snapshot.globalCatalogue'),true,'legitimate omitted private fields preserve catalogue');
  await evaluate(`window.__privateReply={...window.__snap,globalAvailability:{status:${JSON.stringify(status)}},globalCatalogue:null};window.__feature.command('course.update',{courseId:'course:private'})`);
  assert.equal(await evaluate('window.__feature.snapshot.globalCatalogue'),null);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),0);
  assert.ok(await evaluate('document.querySelector(".th-global-availability")'));
 });
});

test('ready to unavailable and account change discard global navigation',async()=>{
 await withCopalBrowser({page},async({evaluate,until})=>{
  await until('window.__ready || window.__error');
  await evaluate(`document.querySelector('[data-main-quest="treehouse:ai"]').click()`);await until('document.querySelector("[data-subject-id]")');
  await evaluate(`window.__originalApi=window.fetch;window.__snap.globalAvailability={status:'activation-required'};window.__snap.globalCatalogue=null;window.__feature.render(document.querySelector('#treehouse'))`);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest],[data-subject-id],[data-treehouse-unit],.th-web-progress").length'),0);
  await evaluate(`window.__snap.accountId='other';window.__snap.actor={id:'other'};window.__snap.state.profiles.other={id:'other',roles:['learner'],active:true};window.__snap.globalAvailability={status:'incompatible'};window.__feature.render(document.querySelector('#treehouse'))`);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest],[data-subject-id],[data-treehouse-unit]").length'),0);
  assert.match(await evaluate('document.querySelector(".th-global-availability").innerText'),/incompatible/);
  await evaluate(`window.__snap.globalAvailability={status:'ready'};window.__snap.globalCatalogue=window.__catalogue;window.__feature.render(document.querySelector('#treehouse'))`);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),4);
 });
});


test('availability transition drops a deferred old global detail without recording course open',async()=>{
 const deferred=page.replace("if(path.includes('/activities/'))return response", "if(path.includes('/activities/')&&window.__holdDetail)await new Promise(resolve=>window.__releaseDetail=resolve);if(path.includes('/activities/'))return response");
 await withCopalBrowser({page:deferred},async({evaluate,until})=>{
  await until('window.__ready || window.__error');
  await evaluate(`document.querySelector('[data-main-quest="treehouse:ai"]').click()`);await until('document.querySelector("[data-subject-id]")');
  await evaluate(`window.__holdDetail=true;document.querySelector('[data-subject-id]').click()`);await until('!!window.__releaseDetail');
  await evaluate(`window.__snap.globalAvailability={status:'activation-required'};window.__snap.globalCatalogue=null;window.__feature.render(document.querySelector('#treehouse'))`);
  await evaluate('window.__releaseDetail()');
  assert.equal(await evaluate('document.querySelectorAll("[data-treehouse-unit],.th-web-work,[data-main-quest]").length'),0);
  assert.equal(await evaluate('window.__calls.filter(c=>c.body&&JSON.parse(c.body).type==="course.open").length'),0);
 });
});


test('private partial response for a different account never inherits ready shared fields',async()=>{
 const partial=privatePage.replace('snapshot.globalCatalogue=null;','').replace('if(options){const command=', 'if(options&&window.__privateReply)return window.__privateReply;if(options){const command=');
 await withCopalBrowser({page:partial},async({evaluate,until})=>{
  await until('window.__ready || window.__error');
  await evaluate(`window.__snap.globalAvailability={status:'ready'};window.__privateReply={...window.__snap,accountId:'different',actor:{id:'different'},state:{...window.__snap.state,profiles:{different:{id:'different',roles:['learner'],active:true}}}};delete window.__privateReply.globalCatalogue;delete window.__privateReply.globalAvailability;window.__feature.command('course.update',{courseId:'course:private'})`);
  assert.equal(await evaluate('window.__feature.snapshot.globalCatalogue'),undefined);
  assert.equal(await evaluate('window.__feature.snapshot.globalAvailability'),undefined);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),0);
 });
});
