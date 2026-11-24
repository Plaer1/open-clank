// Opt-in real full source app, normal auth, actual /treehouse UI. No API mocks.
// Harness owns only its disposable Chrome/profile; Platform owns the candidate.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const candidate=process.env.TREEHOUSE_FULL_APP_URL;
const credentialPath=process.env.TREEHOUSE_FULL_APP_CREDENTIALS;
const enabled=!!candidate&&!!credentialPath;
const sourceId='openclank:treehouse-core';
const rootOrder=['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'];
const shell='#copal-treehouse-modal';
const query='source=system&sourceId='+encodeURIComponent(sourceId);
const summaryPath=id=>'/api/copal/treehouse/courses/'+encodeURIComponent(id)+'/summary?'+query;
const detailPath=(subject,unit)=>'/api/copal/treehouse/courses/'+encodeURIComponent(subject)+'/activities/'+encodeURIComponent(unit)+'?'+query;
function credentialsFor(role){
 const url=new URL(candidate),record=JSON.parse(fs.readFileSync(credentialPath,'utf8'));
 assert.equal(url.protocol,'http:');assert.ok(['127.0.0.1','localhost','[::1]'].includes(url.hostname));
 assert.notEqual(url.port,'54190','The human preview is never a test candidate');
 assert.equal(new URL(record.url).origin,url.origin,'Use only credentials issued for this isolated candidate');
 assert.ok(record[role]?.username&&record[role]?.password);
 return record[role];
}
async function login(cdp,evaluate,wait,role){
 const credential=credentialsFor(role);
 await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await cdp('Page.navigate',{url:new URL('/',candidate).href});
 await wait(`location.origin===${JSON.stringify(new URL(candidate).origin)}`);
 const ok=await evaluate(`(async()=>{const r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(credential)})});return r.ok;})()`);
 assert.equal(ok,true,'Normal candidate login must succeed');
}
function waiter(evaluate){return async expression=>{
 for(let n=0;n<600;n++){if(await evaluate(expression))return;await delay(50);}
 throw new Error('Full-app condition timed out: '+expression+' · '+JSON.stringify(await evaluate('({url:location.href,ready:document.readyState,body:document.body.innerText.slice(-1800)})')));
};}
async function get(evaluate,url){
 return evaluate(`(async()=>{const r=await fetch(${JSON.stringify(url)},{credentials:'same-origin'});if(!r.ok)throw new Error('Candidate GET failed '+r.status);return r.json();})()`);
}
async function navigateTreeHouse(cdp,evaluate,wait){
 await evaluate('window.__priorFourQuestDocument=true');
 await cdp('Page.navigate',{url:new URL('/treehouse',candidate).href});
 await wait('!window.__priorFourQuestDocument&&document.readyState==="complete"');
 await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-workspace')})`);
 assert.equal(await evaluate('typeof window.__candidateReady'),'undefined','Must use actual app shell, not the old mounted candidate-ui harness');
}
async function hub(evaluate,wait){
 await wait(`document.querySelector(${JSON.stringify(shell+' [data-main-quest]')})||[...document.querySelectorAll(${JSON.stringify(shell+' button')})].some(n=>n.textContent==='Main Quests'&&n.getClientRects().length)`);
 if(!await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-main-quest]')})`)){
  const clicked=await evaluate(`(()=>{const buttons=[...document.querySelectorAll(${JSON.stringify(shell+' button')})];const button=buttons.find(n=>n.textContent==='Main Quests'&&n.getClientRects().length);if(!button)return false;button.click();return true;})()`);
  assert.equal(clicked,true,'Native Main Quests navigation must be available');
 }
 await wait(`document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')}).length===4`);
 assert.deepEqual(await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')})].map(n=>n.dataset.mainQuest)`),rootOrder);
}
async function openUnit(evaluate,wait,root,subject,unit){
 await hub(evaluate,wait);
 await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-main-quest="'+root+'"]')}).click()`);
 await wait(`document.querySelector(${JSON.stringify(shell+' [data-subject-id="'+subject+'"]')})`);
 await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-subject-id="'+subject+'"]')}).click()`);
 await wait(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')})`);
 if(await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')}).dataset.treehouseUnit`)!==unit){
  const summary=await get(evaluate,summaryPath(subject));const metadata=summary.units.find(u=>u.id===unit);assert.ok(metadata);
  const clicked=await evaluate(`(()=>{const b=[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-outline button')})].find(n=>n.textContent===${JSON.stringify(metadata.title)});b?.click();return !!b;})()`);assert.equal(clicked,true);
 }
 await wait(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')})?.dataset.treehouseUnit===${JSON.stringify(unit)}`);
 return get(evaluate,detailPath(subject,unit));
}
const journeys=[
 {root:'treehouse:programming',subject:'cs:values',unit:'cs:values:values-types',prompt:/pear|results|types/i,response:'4+2 is integer6; "4"+"2" is text "42"; int("4")+2 is integer6. int("pear") cannot parse that numeric text and needs an explicit validation/error path.'},
 {root:'treehouse:games',subject:'games:design',unit:'games:design:rules-and-state',prompt:/legal|state|counters/i,response:'Removing2 from7 leaves5 counters and passes the turn. Removing1 or2 present counters is legal; removing3 is illegal. A player wins by legally taking the last counter. Allowing3 adds choices and changes reachable states.'},
 {root:'treehouse:ai',subject:'ai:models',unit:'ai:models:identity',prompt:/blueprint|checkpoint|website/i,response:'The blueprint is the architecture, the checkpoint stores learned parameters, and the website is the surrounding application. Instructions or tools can change website behavior without changing checkpoint weights.'},
 {root:'treehouse:openclank',subject:'ai:openclank-files',unit:'ai:openclank-files:editor-save',prompt:/unsaved|recovery|draft/i,response:'In the supplied offline card, green is only the draft until Save: disk revision1 remains blue. A successful same-resource save/reopen gives revision2 green. If another writer already saved red, preserve green and red, inspect the conflict and deliberately resolve rather than overwriting. This is reasoning from the supplied card, not a claim of my own app save.'}
];
test('four-root real app reads, collects reasoned self-report, persists resume and serves checked resources',{skip:!enabled,timeout:240000},async()=>{
 await withCopalBrowser({page:'<!doctype html><title>Owned full-app navigation bootstrap</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=waiter(evaluate);await login(cdp,evaluate,wait,'learner');
  await cdp('Page.addScriptToEvaluateOnNewDocument',{source:`window.__thWire=[];const originalFetch=window.fetch;window.fetch=async(...args)=>{try{const request=args[0];const url=new URL(typeof request==='string'?request:request.url,location.href);if(url.pathname.startsWith('/api/copal/treehouse')){const payload=args[1]?.body?JSON.parse(args[1].body):null;window.__thWire.push({path:url.pathname,type:payload?.type,digest:payload?.payload?.assignmentRequirementDigest});}}catch{}return originalFetch(...args);};`});
  await navigateTreeHouse(cdp,evaluate,wait);await hub(evaluate,wait);
  assert.equal(await evaluate('window.__thWire.some(r=>r.path.includes("/activities/"))'),false,'Fresh hub remains body-free');
  const privateBefore=await get(evaluate,'/api/copal/treehouse');
  for(const journey of journeys){
   const detail=await openUnit(evaluate,wait,journey.root,journey.subject,journey.unit);
   assert.equal(detail.sourceRef.sourceId,sourceId);assert.equal(detail.subjectId,journey.subject);
   const assignment=detail.assignments.find(a=>a.completionEvidence==='response');assert.ok(assignment);assert.match(assignment.prompt,journey.prompt);assert.match(assignment.requirementDigest,/^sha256:[a-f0-9]{64}$/);
   await evaluate(`(()=>{const work=[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-work')})].find(n=>!n.querySelector('select'));const input=work.querySelector('textarea[aria-label="Your response"]');input.value=${JSON.stringify(journey.response)};input.dispatchEvent(new Event('input'));[...work.querySelectorAll('button')].find(n=>n.textContent==='Submit response').click();})()`);
   await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-work')})].some(n=>!n.querySelector('select')&&n.innerText.includes('Submitted · self-reported work'))`);
   const after=await get(evaluate,detailPath(journey.subject,journey.unit));assert.equal(after.submissions[assignment.id].status,'submitted');assert.equal(after.submissions[assignment.id].answer,journey.response);assert.equal(after.submissions[assignment.id].answerRequirementDigest,assignment.requirementDigest);
   assert.equal(await evaluate('window.__thWire.filter(r=>r.type==="submission.submit").at(-1).digest'),assignment.requirementDigest);
   const one=await get(evaluate,summaryPath(journey.subject)+'&workspace=one'),two=await get(evaluate,summaryPath(journey.subject)+'&workspace=two');
   assert.equal(one.progress.completed,two.progress.completed);assert.equal(one.progress.resumeActivityId,journey.unit);assert.equal(two.progress.resumeActivityId,journey.unit);
   await navigateTreeHouse(cdp,evaluate,wait);
   await wait(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')})?.dataset.treehouseUnit===${JSON.stringify(journey.unit)}`);
   assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-web-work textarea[aria-label="Your response"]')}).value`),journey.response);
  }
  // Traverse each actual outgoing direction; related links do not duplicate work.
  await openUnit(evaluate,wait,'treehouse:programming','cs:functions','cs:functions:recursion');
  for(const [subject,target] of [['cs:discrete','cs:discrete:induction'],['cs:functions','cs:functions:recursion']]){
   const current=await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')}).dataset.treehouseUnit`),fromSubject=current.split(':').slice(0,2).join(':');
   const summary=await get(evaluate,summaryPath(fromSubject)),link=summary.units.find(u=>u.id===current).topicLinks.find(l=>l.toId===target);assert.ok(link);assert.ok(link.reason);
   await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-links button')})].find(n=>n.textContent.endsWith(${JSON.stringify(link.targetTitle)})).click()`);
   await wait(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')})?.dataset.treehouseUnit===${JSON.stringify(target)}`);
   assert.equal((await get(evaluate,summaryPath(subject))).progress.resumeActivityId,target);
  }
  // Honest pending practice, without inventing a compiler execution or success.
  const systems=await openUnit(evaluate,wait,'treehouse:programming','cs:systems','cs:systems:compile-link-load');
  const practical=systems.assignments.find(a=>a.completionEvidence==='observed-success');assert.ok(practical);
  await evaluate(`(()=>{const work=[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-work')})].find(n=>n.querySelector('select'));work.querySelector('textarea[aria-label="Your response"]').value='No external compile or run was performed in this journey; only the supplied source was read.';work.querySelector('select').value='blocked';work.querySelector('select').dispatchEvent(new Event('change'));[...work.querySelectorAll('button')].find(n=>n.textContent==='Save attempt').click();})()`);
  await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-web-work')})].some(n=>n.querySelector('select')&&n.innerText.includes('practical work pending'))`);
  const pending=await get(evaluate,detailPath('cs:systems','cs:systems:compile-link-load'));assert.equal(pending.submissions[practical.id].status,'pending');
  // Resource response is real same-origin bytes. Browser is denied disk writes;
  // test checks fixed native download name, Blob MIME/digest and no execution.
  const resourceDetail=await openUnit(evaluate,wait,'treehouse:openclank','ai:openclank-files','ai:openclank-files:editor-save');
  const resource=resourceDetail.resources.find(r=>r.id==='openclank-notes');assert.ok(resource);assert.equal(resource.mime,'application/zip');
  const wire=await evaluate(`(async()=>{const r=await fetch(${JSON.stringify(resource.url)});const bytes=new Uint8Array(await r.arrayBuffer());const digest='sha256:'+Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),n=>n.toString(16).padStart(2,'0')).join('');return {ok:r.ok,mime:r.headers.get('content-type'),disposition:r.headers.get('content-disposition'),bytes:bytes.length,digest};})()`);
  assert.equal(wire.ok,true);assert.match(wire.mime,/application\/zip/);assert.equal(wire.bytes,resource.bytes);assert.equal(wire.digest,resource.digest);
  if(wire.disposition)assert.match(wire.disposition,/attachment.*openclank-notes\.zip/);
  const beforeDownload=await get(evaluate,summaryPath('ai:openclank-files'));
  await cdp('Browser.setDownloadBehavior',{behavior:'deny'});
  await evaluate(`(()=>{const disclosure=document.querySelector(${JSON.stringify(shell+' .th-web-resources')});disclosure.open=true;const row=[...disclosure.querySelectorAll('section')].find(n=>n.innerText.includes('openclank-notes'));[...row.querySelectorAll('button')].find(n=>n.textContent==='Download resource').click();})()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-web-resources a[download]')})`);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-web-resources a[download]')}).download`),'openclank-notes.zip');
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-web-resources a[download]')}).href.startsWith('blob:')`),true);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-web-resources')}).querySelectorAll('iframe,object,script').length`),0);
  assert.equal((await get(evaluate,summaryPath('ai:openclank-files'))).progress.completed,beforeDownload.progress.completed);
  const privateAfter=await get(evaluate,'/api/copal/treehouse');
  for(const id of Object.keys(privateBefore.state?.courses||{}))assert.ok(privateAfter.state?.courses[id],'Existing private definitions remain available');
  assert.equal(Object.keys(privateAfter.state?.courses||{}).some(id=>['cs:values','games:design','ai:models','ai:openclank-files'].includes(id)),false,'Shared definitions are not seeded private copies');
 });
});

test('reserved historical fixture stays559/660 current with its original receipt unchanged by GET',{skip:!enabled,timeout:90000},async()=>{
 await withCopalBrowser({page:'<!doctype html><title>Owned historical read-only check</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=waiter(evaluate);await login(cdp,evaluate,wait,'historyLearner');
  const url='/api/copal/treehouse/main-quests/'+encodeURIComponent('treehouse:programming');
  const before=await get(evaluate,url);assert.equal(before.mainQuest.progressNumerator,559);assert.equal(before.mainQuest.progressDenominator,660);assert.equal(before.mainQuest.percent,85);assert.equal(before.mainQuest.complete,false);
  assert.equal(before.mainQuest.historicalCompletions.length,1);
  for(const [id,completed,total]of [['cs:values',4,6],['cs:languages',3,5],['cs:algorithms',3,4],['cs:discrete',3,6],['cs:web',4,5]]){
   const subject=before.subjects.find(s=>s.id===id);assert.ok(subject);assert.equal(subject.progress.completed,completed);assert.equal(subject.progress.total,total);
  }
  const after=await get(evaluate,url);assert.deepEqual(after.mainQuest.historicalCompletions,before.mainQuest.historicalCompletions);
  for(const subject of before.subjects){const current=after.subjects.find(s=>s.id===subject.id);assert.equal(current.progressRevision,subject.progressRevision);assert.equal(current.resetGeneration,subject.resetGeneration);}
 });
});
