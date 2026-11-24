import test from 'node:test';
import assert from 'node:assert/strict';
import {createTreeHouseTopicWeb,topicSourceKey,topicProgressLabel,topicPracticalAnswer,topicDisplayContent} from '../../static/js/copal/treehouseTopicWeb.js';
const sourceId='openclank:treehouse-core',packageRevision='test.1';
const ref=courseId=>({kind:'system',sourceId,...(courseId?{courseId}:{})});
const progress=(resumeActivityId=null)=>({completed:0,total:1,complete:false,requiredAssignmentIds:['cs:a:assessment:one'],missingAssignmentIds:['cs:a:assessment:one'],resumeActivityId});
function fixture(handler) {
 const calls=[],storage=new Map();globalThis.localStorage={getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)};
 const ui={token:1,actorId:'learner',snapshot:{accountId:'learner',workspace:'one',state:{courses:{'cs:a':{title:'Private collision'}}},globalCatalogue:{sourceId,packageRevision,mainQuests:[{id:'treehouse:programming',title:'Programming'}]}}};
 const response=(fields={})=>({sourceRef:ref(fields.subject?.id||fields.subjectId),packageRevision,workRequirements:Object.fromEntries(['cs:a:assessment:one','cs:b:assessment:one','a','b'].map(id=>[id,{unitId:'cs:a:one',requirementDigest:'digest1',completionEvidence:'response'}])),...fields});
 const context={ui,renderLoaded(){},setStatus(){},styledConfirm:async()=>true,api:async(path,options)=>{calls.push({path,options});return handler?.(path,options,{ui,response})??(path.includes('main-quests')?response({mainQuest:{id:'treehouse:programming',title:'Programming'},subjects:[{id:'cs:a',mainQuestId:'treehouse:programming',progress:progress(),progressRevision:0,resetGeneration:0}]}):path.includes('/summary')?response({subject:{id:'cs:a',mainQuestId:'treehouse:programming'},units:[{id:'cs:a:one',title:'One'}],progress:progress(),progressRevision:0,resetGeneration:0}):response({subjectId:'cs:a',unit:{id:'cs:a:one',content:'One'},assignments:[],submissions:{},progress:progress(),progressRevision:0,resetGeneration:0}));}};
 return {controller:createTreeHouseTopicWeb(()=>context),ui,calls,response,context};
}
test('global root and subject summaries stay lazy and separate from colliding account definitions',async()=>{
 const f=fixture();await f.controller.rootDetail('treehouse:programming');await f.controller.subjectDetail('cs:a');
 assert.equal(f.calls.length,2);assert.ok(f.calls.every(x=>!x.path.includes('/activities/')));
 assert.equal(f.ui.snapshot.state.courses['cs:a'].title,'Private collision');
 assert.notEqual(topicSourceKey(ref('cs:a')),topicSourceKey({kind:'account',ownerAccountId:'learner',workspaceId:'one',courseId:'cs:a'}));
});
test('unit cache is scoped and late account response cannot populate it',async()=>{
 let resolve;const f=fixture((path,options,{response})=>path.includes('/activities/')?new Promise(r=>{resolve=()=>r(response({subjectId:'cs:a',unit:{id:'cs:a:one'},progress:progress(),progressRevision:0,resetGeneration:0}));}):undefined);
 f.controller.sync();const pending=f.controller.unitDetail('cs:a','cs:a:one');
 f.ui.snapshot.accountId='other';f.ui.token++;f.controller.sync();resolve();assert.equal(await pending,null);
});
test('explicit source refresh invalidates same-package bodies and drops an older pending read',async()=>{
 let release,paused=false,text='Original source';
 const f=fixture((path,options,{response})=>{
  if(!path.includes('/activities/'))return;
  const value=response({subjectId:'cs:a',unit:{id:'cs:a:one',content:text},assignments:[],progress:progress(),progressRevision:0,resetGeneration:0});
  return paused?new Promise(resolve=>{release=()=>resolve(value);}):value;
 });
 assert.equal((await f.controller.unitDetail('cs:a','cs:a:one')).unit.content,'Original source');
 paused=true;const old=f.controller.unitDetail('cs:a','cs:a:one',true);
 f.controller.invalidateContent();text='Edited source';paused=false;
 assert.equal((await f.controller.unitDetail('cs:a','cs:a:one')).unit.content,'Edited source');
 release();assert.equal(await old,null);
 assert.equal((await f.controller.unitDetail('cs:a','cs:a:one')).unit.content,'Edited source');
});
test('failed fresh source read does not serve the old cached body as current',async()=>{
 let unavailable=false,reads=0;const f=fixture(path=>{if(path.includes('/activities/')){reads++;if(unavailable)throw Error('Source file unavailable');}});
 await f.controller.unitDetail('cs:a','cs:a:one');unavailable=true;
 await assert.rejects(f.controller.unitDetail('cs:a','cs:a:one',true),/Source file unavailable/);
 await assert.rejects(f.controller.unitDetail('cs:a','cs:a:one'),/Source file unavailable/);
 assert.equal(reads,3);
});
test('wrong source or package response is rejected instead of contaminating the current view',async()=>{
 const f=fixture(()=>({sourceRef:{kind:'account',ownerAccountId:'someone'},packageRevision}));
 await assert.rejects(f.controller.subjectDetail('cs:a'),/Curriculum changed/);
});
test('partial global commands preserve private snapshot and use progress revision CAS',async()=>{
 const f=fixture((path,options,{response})=>options?response({ok:true,result:{status:'pending',completionSatisfied:false},progress:progress('cs:a:one'),progressRevision:2,resetGeneration:0}):undefined);
 await f.controller.subjectDetail('cs:a');const before=f.ui.snapshot.state;
 const answer=topicPracticalAnswer('Build failed','failed',true,'Compiler log');
 const result=await f.controller.command('submission.submit',{courseId:'cs:a',assignmentRequirementDigest:'digest1',assignmentId:'cs:a:assessment:one',answer});
 assert.equal(result.result.completionSatisfied,false);assert.equal(f.ui.snapshot.state,before);
 const sent=JSON.parse(f.calls.at(-1).options.body);assert.equal(sent.expectedRevision,0);assert.equal(sent.payload.sourceRef.courseId,'cs:a');assert.equal(sent.payload.answer.outcome.learnerConfirmed,false);
 assert.equal(f.controller.progress.get('cs:a').progressRevision,2);
});
test('root reset includes electives and exact CAS scope vector without private global revision',async()=>{
 const f=fixture((path,options,{response})=>path.includes('main-quests')?response({mainQuest:{id:'treehouse:programming'},subjects:[{id:'cs:a',required:true,progressRevision:3,resetGeneration:1,progress:progress()},{id:'cs:elective',required:false,progressRevision:7,resetGeneration:2,progress:progress()}]}):options?response({ok:true,result:{},subjects:[]}):undefined);
 await f.controller.reset('root','treehouse:programming');const sent=JSON.parse(f.calls.at(-1).options.body);
 assert.equal(sent.expectedRevision,null);assert.deepEqual(sent.payload.resetScopes,[{courseId:'cs:a',progressRevision:3,resetGeneration:1},{courseId:'cs:elective',progressRevision:7,resetGeneration:2}]);assert.equal(sent.payload.mainQuestId,'treehouse:programming');
});
test('root displays required and elective counts, not invented completion from rounding',()=>{
 assert.match(topicProgressLabel({requiredComplete:1,requiredTotal:2,electiveComplete:1,electiveTotal:4,percent:100,complete:false}),/^1\/2 required Quests/);
 assert.equal(topicProgressLabel({completed:199,total:200,complete:false,percent:100}),'199/200 work items');
});
test('switch while unit fetch is pending never records course.open for the new account',async()=>{
 let release;const f=fixture((path,options,{response})=>path.includes('/activities/')?new Promise(r=>{release=()=>r(response({subjectId:'cs:a',unit:{id:'cs:a:one'},progress:progress(),progressRevision:0,resetGeneration:0}));}):undefined);
 const pending=f.controller.openSubject('cs:a');await new Promise(r=>setImmediate(r));
 f.ui.snapshot.accountId='other';f.ui.token++;f.controller.sync();release();await pending;
 assert.equal(f.calls.filter(call=>call.options).length,0);assert.equal(f.controller.selected.courseId,null);
});
test('reset confirmation from learner A cannot mutate learner B',async()=>{
 const f=fixture();let accept;f.context.styledConfirm=()=>new Promise(resolve=>{accept=resolve;});
 const pending=f.controller.reset('root','treehouse:programming');await new Promise(r=>setImmediate(r));
 f.ui.snapshot.accountId='other';f.ui.token++;f.controller.sync();accept(true);await pending;
 assert.equal(f.calls.filter(call=>call.options).length,0);
});
test('switch during first subject lookup cannot capture new-account revision for old action',async()=>{
 let release;const f=fixture((path,options,{response})=>path.includes('/summary')?new Promise(r=>{release=()=>r(response({subject:{id:'cs:a',mainQuestId:'treehouse:programming'},units:[{id:'cs:a:one'}],progress:progress(),progressRevision:0,resetGeneration:0}));}):undefined);
 const pending=f.controller.command('submission.submit',{courseId:'cs:a',assignmentRequirementDigest:'digest1',assignmentId:'cs:a:assessment:one',answer:'old learner response'});
 await new Promise(r=>setImmediate(r));
 f.ui.snapshot.accountId='other';f.ui.token++;f.controller.sync();release();await pending;
 assert.equal(f.calls.filter(call=>call.options).length,0);
});

test('prerequisite mutation refreshes cached dependent availability in another subject',async()=>{
 let dependentReads=0,blocked=true;const f=fixture((path,options,{response})=>{
  if(path.includes('/activities/')){dependentReads++;return response({subjectId:'cs:a',unit:{id:'cs:a:one'},progress:progress(),progressRevision:0,resetGeneration:0,actionAvailability:{status:blocked?'blocked':'available'}});}
  if(path.includes('/cs%3Ab/summary'))return response({sourceRef:ref('cs:b'),subject:{id:'cs:b',mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:0,resetGeneration:0});
  if(options){blocked=JSON.parse(options.body).type==='progress.reset';return response({sourceRef:ref('cs:b'),ok:true,result:{},progress:progress(),progressRevision:1,resetGeneration:0});}
 });
 await f.controller.unitDetail('cs:a','cs:a:one');await f.controller.command('submission.submit',{courseId:'cs:b',assignmentRequirementDigest:'digest1',assignmentId:'cs:b:assessment:one',answer:'Evidence'});
 assert.equal((await f.controller.unitDetail('cs:a','cs:a:one')).actionAvailability.status,'available');
 await f.controller.command('progress.reset',{courseId:'cs:b'});assert.equal((await f.controller.unitDetail('cs:a','cs:a:one')).actionAvailability.status,'blocked');assert.equal(dependentReads,3);
});
test('duplicate body title is removed only for presentation',()=>{
 const unit={title:'Values',content:'# Values\n\n- One value.'};assert.equal(topicDisplayContent(unit),'- One value.');assert.equal(unit.content,'# Values\n\n- One value.');assert.equal(topicDisplayContent({title:'Values',content:'# A different section\nText'}),'# A different section\nText');
});

test('permanent root failure waits for explicit retry rather than autoloading on render',async()=>{
 const f=fixture(()=>{throw new Error('Unavailable topic');});
 f.context.h=(tag,attrs,...children)=>({tag,attrs,childNodes:children,append(...nodes){this.childNodes.push(...nodes);}});
 await f.controller.openRoot('treehouse:programming');
 for(let i=0;i<3;i++)f.controller.render({append(){}});
 assert.equal(f.calls.length,1);
});

test('different-subject mutations serialize and cannot regress aggregate response order',async()=>{
 let release,dispatches=[];const f=fixture((path,options,{response})=>{
  if(path.includes('/cs%3Ab/summary'))return response({sourceRef:ref('cs:b'),subject:{id:'cs:b',mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:3,resetGeneration:0});
  if(options){const sent=JSON.parse(options.body);dispatches.push(sent);const id=sent.payload.courseId;
   const result=response({sourceRef:ref(id),ok:true,result:{},progress:progress(),progressRevision:4,resetGeneration:0,globalCatalogue:{sourceId,packageRevision,mainQuests:[{id:'treehouse:programming',requiredComplete:id==='cs:b'?2:1,requiredTotal:2}]}});
   return id==='cs:a'?new Promise(resolve=>{release=()=>resolve(result);}):result;
  }
 });
 await f.controller.subjectDetail('cs:a');const a=f.controller.command('submission.submit',{courseId:'cs:a',assignmentRequirementDigest:'digest1',assignmentId:'a',answer:'A'});const b=f.controller.command('submission.submit',{courseId:'cs:b',assignmentRequirementDigest:'digest1',assignmentId:'b',answer:'B'});
 await new Promise(r=>setImmediate(r));assert.equal(dispatches.length,1);release();await a;await b;
 assert.equal(dispatches.length,2);assert.equal(dispatches[1].expectedRevision,3);assert.equal(f.ui.snapshot.globalCatalogue.mainQuests[0].requiredComplete,2);
});

test('queued old-account mutations drop on scope change without blocking the new account',async()=>{
 let release;const dispatched=[];const f=fixture((p,o,{response})=>{if(!o)return;const sent=JSON.parse(o.body);dispatched.push(sent);const result=response({ok:true,result:{},progress:progress(),progressRevision:1,resetGeneration:0});return sent.actorId==='B'?result:new Promise(r=>{release=()=>r(result);});});
 await f.controller.subjectDetail('cs:a');const first=f.controller.command('submission.submit',{courseId:'cs:a',assignmentId:'cs:a:assessment:one',assignmentRequirementDigest:'digest1',answer:'A'});const queued=f.controller.command('submission.submit',{courseId:'cs:a',assignmentId:'cs:a:assessment:one',assignmentRequirementDigest:'digest1',answer:'Another A'});
 await new Promise(r=>setImmediate(r));f.ui.snapshot.accountId='B';f.ui.actorId='B';f.ui.token++;f.controller.sync();await f.controller.command('submission.submit',{courseId:'cs:a',assignmentId:'cs:a:assessment:one',assignmentRequirementDigest:'digest1',answer:'B'});assert.equal(dispatched.length,2,'New account can dispatch before the old in-flight request returns');release();await first;await queued;assert.equal(dispatched.length,2);assert.equal(dispatched[1].actorId,'B');
});


test('submission never rebases a mismatching displayed requirement onto refreshed CAS',async()=>{
 const f=fixture((path,options,{response})=>path.includes('/summary')?response({subject:{id:'cs:a',mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:9,resetGeneration:0,workRequirements:{'cs:a:assessment:one':{unitId:'cs:a:one',requirementDigest:'newDigest',completionEvidence:'response'}}}):undefined);
 await assert.rejects(f.controller.command('submission.submit',{courseId:'cs:a',assignmentId:'cs:a:assessment:one',assignmentRequirementDigest:'oldDigest',answer:'old answer'}),/task or reset scope changed/);
 assert.equal(f.calls.filter(call=>call.options).length,0);
 await assert.rejects(f.controller.command('submission.draft',{courseId:'cs:a',assignmentId:'cs:a:assessment:one',answer:'no identity'}),/identity is missing/);
 assert.equal(f.calls.filter(call=>call.options).length,0);
});


for(const [label,nextRevision,nextGeneration] of [['concurrent save',4,1],['generation change',3,2],['concurrent reset and new work',5,2]]) {
 test(`subject confirmation rejects ${label} instead of freshening reset CAS`,async()=>{
  let revision=3,generation=1,confirm;
  const f=fixture((path,options,{response})=>path.includes('/summary')?response({subject:{id:'cs:a',mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:revision,resetGeneration:generation}):undefined);
  f.context.styledConfirm=()=>new Promise(resolve=>{confirm=resolve;});
  const pending=f.controller.reset('subject','cs:a');const rejected=assert.rejects(pending,/Progress changed since this reset was confirmed/);
  await new Promise(resolve=>setImmediate(resolve));assert.equal(typeof confirm,'function');
  revision=nextRevision;generation=nextGeneration;confirm(true);await rejected;
  assert.equal(f.calls.filter(call=>call.options).length,0);
 });
}
test('unchanged subject confirmation sends its original revision and generation',async()=>{
 const f=fixture((path,options,{response})=>path.includes('/summary')?response({subject:{id:'cs:a',mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:3,resetGeneration:1}):options?response({ok:true,result:{},progress:progress(),progressRevision:4,resetGeneration:2}):undefined);
 await f.controller.reset('subject','cs:a');const sent=JSON.parse(f.calls.find(call=>call.options).options.body);
 assert.equal(sent.expectedRevision,3);assert.equal(sent.payload.resetGeneration,1);assert.equal(sent.payload.courseId,'cs:a');
});
test('subject reset retains confirmation CAS while queued behind another mutation',async()=>{
 let revision=3,release;
 const f=fixture((path,options,{response})=>{
  if(path.includes('/summary')){const id=path.includes('cs%3Ab')?'cs:b':'cs:a';return response({sourceRef:ref(id),subject:{id,mainQuestId:'treehouse:programming'},units:[],progress:progress(),progressRevision:id==='cs:a'?revision:1,resetGeneration:1});}
  if(options)return new Promise(resolve=>{release=()=>resolve(response({sourceRef:ref('cs:b'),ok:true,result:{},progress:progress(),progressRevision:2,resetGeneration:1}));});
 });
 const first=f.controller.command('course.open',{courseId:'cs:b',activityId:'cs:b:one'});await new Promise(resolve=>setImmediate(resolve));
 const pending=f.controller.reset('subject','cs:a');const rejected=assert.rejects(pending,/Progress changed since this reset was confirmed/);
 await new Promise(resolve=>setImmediate(resolve));revision=4;release();await first;await rejected;
 assert.equal(f.calls.filter(call=>call.options).length,1,'only the preceding mutation may POST');
});
for(const switchKind of ['account','source'])test(`subject reset confirmation cannot cross ${switchKind}`,async()=>{
 const f=fixture();let confirm;f.context.styledConfirm=()=>new Promise(resolve=>{confirm=resolve;});
 const pending=f.controller.reset('subject','cs:a');await new Promise(resolve=>setImmediate(resolve));
 if(switchKind==='account'){f.ui.snapshot.accountId='other';f.ui.token++;}else f.ui.snapshot.globalCatalogue.sourceId='foreign';
 confirm(true);await pending;assert.equal(f.calls.filter(call=>call.options).length,0);
});
