// Opt-in actual source/package app shell: normal auth, real API/storage, no API mocks.
// The release owner supplies an isolated synthetic learner and private Tutorial.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';

const candidate=process.env.TREEHOUSE_POLISH_CANDIDATE_URL;
const credentialsPath=process.env.TREEHOUSE_POLISH_CREDENTIALS;
const enabled=!!candidate&&!!credentialsPath;
const shell='#copal-treehouse-modal';
const sourceId='openclank:treehouse-core';
const roots=['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank'];
const unit='games:design:rules-and-state',course='games:design';
const systemQuery='source=system&sourceId='+encodeURIComponent(sourceId);

function fixture(){
 const url=new URL(candidate),record=JSON.parse(fs.readFileSync(credentialsPath,'utf8'));
 assert.equal(url.protocol,'http:');
 assert.ok(['127.0.0.1','localhost','[::1]'].includes(url.hostname),'Only owned loopback candidates');
 assert.ok(!['7777','54190'].includes(url.port),'Installed app and human preview are never test candidates');
 assert.equal(url.origin,new URL(record.url).origin,'Credentials must belong to this isolated candidate');
 assert.ok(record.learner?.username&&record.learner?.password);
 assert.ok(record.privateCourseId,'Owner must identify one synthetic published private Tutorial');
 assert.equal(fs.statSync(credentialsPath).mode&0o077,0,'Credential file must be private');
 return {url,record};
}

test('isolated real shell discovers shared/private learning, retains drafts and navigates actual window arrows',{
 skip:!enabled,timeout:180000,
},async()=>{
 const {url,record}=fixture();
 const output=fs.mkdtempSync(path.join(os.tmpdir(),'treehouse-polish-full-app-'));
 const receipt={candidateOrigin:url.origin,runnerSha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),screenshots:[],checks:[]};
 await withCopalBrowser({page:'<title>Owned learner polish full-app bootstrap</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let i=0;i<600;i++){if(await evaluate(expression))return;await delay(50);}throw Error('Full-app condition timed out: '+expression+' · '+JSON.stringify(await evaluate('({path:location.pathname,text:document.body.innerText.slice(-1400)})')));};
  const get=async endpoint=>evaluate(`(async()=>{const r=await fetch(${JSON.stringify(endpoint)},{credentials:'same-origin'});if(!r.ok)throw Error('GET '+r.status);return r.json();})()`);
  const click=async selector=>{await wait(`document.querySelector(${JSON.stringify(selector)})?.getClientRects().length`);assert.equal(await evaluate(`(()=>{const b=document.querySelector(${JSON.stringify(selector)});if(b.disabled)return false;b.click();return true;})()`),true,'Enabled visible control: '+selector);};
  const section=async label=>{const selector=shell+' .copal-treehouse-nav button';assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll(${JSON.stringify(selector)})].find(x=>x.textContent.trim()===${JSON.stringify(label)});b?.click();return !!b;})()`),true,'Section '+label);};
  const arrow=async direction=>click(shell+' .copal-window-nav-'+direction);
  const shot=async name=>{await delay(150);const result=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});const bytes=Buffer.from(result.data,'base64'),file=path.join(output,name+'.png');fs.writeFileSync(file,bytes,{mode:0o600});receipt.screenshots.push({path:file,bytes:bytes.length,sha256:crypto.createHash('sha256').update(bytes).digest('hex')});};
  const progressView=data=>Object.fromEntries(data.sharedCatalogue.quests.map(q=>[q.id,{completed:q.progress.completed,total:q.progress.total,complete:q.progress.complete}]));

  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:url.origin+'/'});await wait(`location.origin===${JSON.stringify(url.origin)}`);
  assert.equal(await evaluate(`fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(record.learner)})}).then(r=>r.ok)`),true,'Normal authentication');
  // Observe requests without substituting responses or exposing answers/tokens.
  await cdp('Page.addScriptToEvaluateOnNewDocument',{source:`window.__polishWire=[];const realFetch=window.fetch;window.fetch=async(...args)=>{try{const u=new URL(typeof args[0]==='string'?args[0]:args[0].url,location.href);if(u.pathname.startsWith('/api/copal/treehouse')){const b=args[1]?.body?JSON.parse(args[1].body):null;window.__polishWire.push({path:u.pathname,type:b?.type});}}catch{}return realFetch(...args);};`});
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});
  await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-workspace')})`);
  assert.equal(await evaluate('typeof window.__candidateReady'),'undefined','Actual app shell, not mounted fixture');
  await wait(`document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')}).length===4`);
  assert.deepEqual(await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')})].map(x=>x.dataset.mainQuest)`),roots);
  const snapshot=await get('/api/copal/treehouse');
  assert.equal(snapshot.state.courses[record.privateCourseId]?.status,'published');
  assert.equal(snapshot.state.courses[record.privateCourseId]?.courseType,'tutorial');
  const before=await get('/api/copal/treehouse/learning-library');
  assert.equal(before.globalAvailability.status,'ready');
  assert.equal(before.sharedCatalogue.sourceId,sourceId);
  assert.equal(before.sharedCatalogue.mainQuests.length,4);assert.equal(before.sharedCatalogue.quests.length,49);assert.equal(before.sharedCatalogue.lessons.length,291);
  receipt.packageDigest=before.sharedCatalogue.packageDigest;receipt.packageRevision=before.sharedCatalogue.packageRevision;
  const metadata=before.sharedCatalogue.lessons.find(x=>x.id===unit);assert.ok(metadata);
  assert.equal(metadata.sourceRef.kind,'system');assert.equal(metadata.courseId,course);

  await section('Skills');await wait(`document.querySelector(${JSON.stringify(shell+' .th-shared-skills [aria-label="Main Quest"] button')})`);
  assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll(${JSON.stringify(shell+' .th-shared-skills [aria-label="Main Quest"] button')})].find(x=>x.querySelector('strong')?.textContent==='Games');b?.click();return !!b;})()`),true);
  await click(shell+' [data-skills-quest="'+course+'"]');
  await wait(`document.querySelector(${JSON.stringify(shell+' [data-skills-detail="'+course+'"]')})`);
  assert.match(await evaluate(`document.querySelector(${JSON.stringify(shell+' [data-skills-detail]')}).innerText`),/not assessed mastery/);
  const contained=await evaluate(`(()=>{return [...document.querySelectorAll(${JSON.stringify(shell+' .th-shared-skills [aria-label="Main Quest"] button,'+shell+' [data-skills-quest]')})].every(b=>[...b.children].every(n=>n.getBoundingClientRect().bottom<=b.getBoundingClientRect().bottom+1));})()`);
  assert.equal(contained,true,'Multiline root and Quest text stays inside real-shell button borders');
  await shot('skills-normal');
  await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' [aria-label="Learning path display"] button')})].find(b=>b.textContent==='Graph').click()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' [aria-label="Learning path display"] button[aria-pressed="true"]')})?.textContent==='Graph'`);
  await shot('skills-graph-normal');
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  await shot('skills-graph-narrow');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  receipt.checks.push('shared Skills uses real corpus, contained multiline controls and explicit Graph selection');

  const search=async query=>{
   await wait(`document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')})`);
   await evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')});input.value=${JSON.stringify(query)};input.dispatchEvent(new Event('input',{bubbles:true}));input.form.requestSubmit();})()`);
  };
  await section('Library');await search(metadata.title);
  const lessonCard=shell+' [data-shared-lesson="'+unit+'"]';await wait(`document.querySelector(${JSON.stringify(lessonCard)})`);
  await shot('library-search-normal');await click(lessonCard+' button');
  await wait(`document.querySelector(${JSON.stringify(shell+' [data-treehouse-unit]')})?.dataset.treehouseUnit===${JSON.stringify(unit)}`);
  const summary=await get('/api/copal/treehouse/courses/'+encodeURIComponent(course)+'/summary?'+systemQuery);
  assert.equal(summary.progress.resumeActivityId,unit,'Real system resume uses selected lesson');
  const response=shell+' .th-web-work textarea[aria-label="Your response"]';await wait(`document.querySelector(${JSON.stringify(response)})`);
  const draft='Owned synthetic unsent draft: a legal move changes the counter state.';
  await evaluate(`(()=>{const input=document.querySelector(${JSON.stringify(response)});input.value=${JSON.stringify(draft)};input.dispatchEvent(new Event('input',{bubbles:true}));})()`);
  await section('Skills');await wait(`document.querySelector(${JSON.stringify(shell+' [data-skills-detail]')})`);
  await arrow('back');await wait(`document.querySelector(${JSON.stringify(response)})?.value===${JSON.stringify(draft)}`);
  await arrow('forward');await wait(`document.querySelector(${JSON.stringify(shell+' [data-skills-detail]')})`);
  await arrow('back');await wait(`document.querySelector(${JSON.stringify(response)})?.value===${JSON.stringify(draft)}`);
  receipt.checks.push('actual window Back/Forward retains scoped unsent response');

  await section('Library');await search('');
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-private-learning')})`);
  await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-private-learning')}).open=true`);
  const privateTitle=snapshot.state.courses[record.privateCourseId].title;
  const opened=await evaluate(`(()=>{const card=[...document.querySelectorAll(${JSON.stringify(shell+' .th-private-learning article')})].find(x=>x.querySelector('h3')?.textContent===${JSON.stringify(privateTitle)});card?.querySelector('button')?.click();return !!card;})()`);assert.equal(opened,true);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-lesson')})`);
  const privateActivity=Object.values(snapshot.state.activities).find(a=>a.courseId===record.privateCourseId&&a.status==='published');assert.ok(privateActivity);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-lesson')}).dataset.treehouseId`),privateActivity.id);
  await arrow('back');await wait(`document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')})`);
  await arrow('forward');await wait(`document.querySelector(${JSON.stringify(shell+' .th-lesson')})`);
  receipt.checks.push('private Library destination and actual arrows retain source identity');

  await section('Records');await wait(`document.querySelector(${JSON.stringify(shell+' select[aria-label="Private course"]')})`);
  await evaluate(`(()=>{const select=document.querySelector(${JSON.stringify(shell+' select[aria-label="Private course"]')});select.value=${JSON.stringify(record.privateCourseId)};select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].some(x=>x.textContent==='Request completion record')`);
  const info=await get('/api/copal/treehouse/courses/'+encodeURIComponent(record.privateCourseId)+'/learning');
  assert.equal(await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].find(x=>x.textContent==='Request completion record').disabled`),!info.completion.eligible);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell)}).innerText.includes('Discuss & board')`),false);
  await shot('records-normal');receipt.checks.push('Records keeps eligibility/action without discussion surface');

  await section('Library');await search(metadata.title);await wait(`document.querySelector(${JSON.stringify(lessonCard)})`);
  await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});
  await delay(200);await shot('library-search-narrow');
  const overflow=await evaluate(`(()=>{const modal=document.querySelector(${JSON.stringify(shell)});return {page:document.documentElement.scrollWidth>innerWidth+2,modal:modal.getBoundingClientRect().right>innerWidth+2};})()`);
  assert.deepEqual(overflow,{page:false,modal:false},'Narrow app shell remains within viewport');
  const after=await get('/api/copal/treehouse/learning-library');
  assert.deepEqual(progressView(after),progressView(before),'Navigation/search/draft editing must not earn work');
  const wire=await evaluate('window.__polishWire');
  assert.equal(wire.some(x=>/^(submission\.|activity\.complete|credential\.|progress\.reset)/.test(x.type||'')),false,'This journey never submits/grades/completes/resets work');
  receipt.checks.push('real progress unchanged and narrow Library readable');
 });
 fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n',{mode:0o600});
 console.log('Learner polish full-app receipt:',path.join(output,'receipt.json'));
});

// Additional opt-in after the owner has offline-converted ONLY this fixture.
// It deliberately edits the synthetic private lesson, never the shared corpus.
test('source-backed private lesson opens ordinary Editor, saves and reloads with effective Files authority',{
 skip:!enabled||process.env.TREEHOUSE_POLISH_COURSE_FILES!=='1',timeout:180000,
},async()=>{
 const {url,record}=fixture();assert.ok(record.admin?.username&&record.admin?.password,'Synthetic source owner required');
 const output=fs.mkdtempSync(path.join(os.tmpdir(),'treehouse-course-files-full-app-'));
 const receipt={candidateOrigin:url.origin,runnerSha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),checks:[]};
 await withCopalBrowser({page:'<title>Owned source-file round trip</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let i=0;i<600;i++){if(await evaluate('(async()=>!!(await ('+expression+')))()'))return;await delay(50);}throw Error('Source-file condition timed out: '+expression);};
  const request=(endpoint,body)=>evaluate(`(async()=>{const r=await fetch(${JSON.stringify(endpoint)},{credentials:'same-origin',${body===undefined?'':`method:'POST',body:JSON.stringify(${JSON.stringify(body)}),`}headers:{'Content-Type':'application/json'}});return {status:r.status,value:await r.json()};})()`);
  const login=async role=>{
   await request('/api/auth/logout',{});
   await cdp('Page.navigate',{url:url.origin+'/login'});await wait(`location.pathname==='/login'&&document.readyState==='complete'`);
   try{assert.equal((await request('/api/auth/login',record[role])).status,200);}catch{throw Error('Synthetic role login interrupted before stable authentication');}
  };
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:url.origin+'/'});await wait(`location.origin===${JSON.stringify(url.origin)}`);await login('admin');
  const snapshot=(await request('/api/copal/treehouse')).value;
  const activity=Object.values(snapshot.state.activities).find(a=>a.courseId===record.privateCourseId&&a.status==='published');assert.ok(activity);
  assert.equal('content' in activity,false,'Converted private outline is metadata only');
  assert.equal(snapshot.state.courses[record.privateCourseId].contentAuthority,'course-files');
  const detailPath='/api/copal/treehouse/courses/'+encodeURIComponent(record.privateCourseId)+'/activities/'+encodeURIComponent(activity.id);
  const sourcePath=detailPath+'/source-open';
  const before=(await request(detailPath)).value;assert.ok(before.contentSource?.contentHead);assert.equal(before.permissions.editSource,true);
  const source=await request(sourcePath,{});assert.equal(source.status,200);assert.equal(source.value.canEdit,true);assert.ok(source.value.resource?.ref);
  assert.equal(source.value.resource.capabilities.includes('write'),true,'Actual Files resource permits owner write');
  assert.equal((await request(sourcePath,{path:'/etc/passwd'})).status,422,'Source-open never accepts a caller path');
  assert.equal((await request(sourcePath,{resourceRef:source.value.resource.ref})).status,422,'Source-open never accepts arbitrary opaque refs');

  // A learning grant is not the owner's generic Files authority. Exercise the
  // actual sealed ref endpoint under the other normal authenticated account.
  await login('learner');
  const recipient=await request(detailPath);assert.equal(recipient.status,200);assert.equal(recipient.value.contentSource.canEdit,false);
  assert.equal((await request(sourcePath,{})).status,403);
  const foreignRef=await request('/api/files-v1/open-resource',{resource_ref:source.value.resource.ref});assert.equal(foreignRef.status,404,'Owner ref cannot be used in recipient Editor');assert.equal(foreignRef.value.detail?.code,'resource_unavailable','Typed non-enumerating Files denial, not missing endpoint');
  const shared=await request('/api/copal/treehouse/courses/'+course+'/activities/'+unit+'?'+systemQuery);assert.equal(shared.status,200);
  const sharedSource=await request('/api/copal/treehouse/courses/'+course+'/activities/'+unit+'/source-open',{sourceRef:{kind:'system',sourceId,courseId:course}});
  assert.equal(sharedSource.status,403,'Default shared learning reads do not mint a Files root grant');
  receipt.checks.push('caller path/ref rejected; recipient and shared-default Files authority denied');

  await login('admin');await cdp('Page.navigate',{url:url.origin+'/treehouse'});
  await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-nav')})`);
  assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll(${JSON.stringify(shell+' .copal-treehouse-nav button')})].find(x=>x.textContent==='Library');b?.click();return !!b;})()`),true);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-private-learning')})`);
  const title=snapshot.state.courses[record.privateCourseId].title;
  await evaluate(`(()=>{const privateList=document.querySelector(${JSON.stringify(shell+' .th-private-learning')});privateList.open=true;const card=[...privateList.querySelectorAll('article')].find(x=>x.querySelector('h3')?.textContent===${JSON.stringify(title)});if(!card)throw Error('Synthetic private course absent');card.querySelector('button').click();})()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-lesson')})?.dataset.treehouseId===${JSON.stringify(activity.id)}`);
  await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-lesson button')})].some(x=>x.textContent==='Edit source')`);
  await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-lesson button')})].find(x=>x.textContent==='Edit source').click()`);
  const editor='#copal-notes-modal .cm-content';await wait(`document.querySelector(${JSON.stringify(editor)})`);
  const marker='Synthetic source round-trip '+crypto.randomUUID();
  await evaluate(`document.querySelector(${JSON.stringify(editor)}).focus()`);await delay(200);
  const modifiers=process.platform==='darwin'?4:2;
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'End',code:'End',windowsVirtualKeyCode:35,modifiers});
  await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'End',code:'End',windowsVirtualKeyCode:35,modifiers});
  await cdp('Input.insertText',{text:'\n\n'+marker});
  await wait(`document.querySelector(${JSON.stringify(editor)}).textContent.includes(${JSON.stringify(marker)})`);
  const saved=await evaluate(`(()=>{const b=[...document.querySelectorAll('#copal-notes-modal .copal-note-header-actions button')].find(x=>x.textContent==='Save'&&!x.disabled);b?.click();return !!b;})()`);assert.equal(saved,true,'Existing native explicit Save control');
  await wait(`(async()=>{const r=await fetch(${JSON.stringify(detailPath)});return r.ok&&(await r.json()).activity.content.includes(${JSON.stringify(marker)});})()`);
  const after=(await request(detailPath)).value;
  assert.notEqual(after.contentSource.contentHead,before.contentSource.contentHead);assert.equal(after.sourceMetadataHead,before.sourceMetadataHead,'Body save does not rewrite structured requirements');
  assert.ok(after.activity.content.includes(before.activity.content));
  const image=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,'native-editor-save.png'),Buffer.from(image.data,'base64'),{mode:0o600});
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-workspace')})`);
  assert.equal((await request(detailPath)).value.activity.content,after.activity.content,'Fresh document reload retains exact file bytes');
  receipt.checks.push('real private source UI opens ordinary Editor; trusted typing, explicit Save, changed head and fresh reload');
  receipt.beforeHead=before.contentSource.contentHead;receipt.afterHead=after.contentSource.contentHead;



 });
 fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n',{mode:0o600});
 console.log('Course-file actual Editor receipt:',path.join(output,'receipt.json'));
});

// Separate fresh-context phase: no logout UX or repeat of the private Save.
test('shared source exact-root policy grants native Editor Save without sibling authority',{
 skip:!enabled||process.env.TREEHOUSE_POLISH_COURSE_FILES!=='1',timeout:180000,
},async()=>{
 const {url,record}=fixture();assert.ok(record.admin&&record.sharedSourceRoot&&record.siblingPath);
 const output=fs.mkdtempSync(path.join(os.tmpdir(),'treehouse-shared-files-full-app-'));
 const receipt={candidateOrigin:url.origin,runnerSha256:crypto.createHash('sha256').update(fs.readFileSync(new URL(import.meta.url))).digest('hex'),checks:[]};
 // A separate normal-auth HTTP owner session performs the explicit supported
 // operator action; it does not authenticate the learner's browser as owner.
 const auth=await fetch(url.origin+'/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(record.admin)});assert.equal(auth.status,200);
 const cookie=auth.headers.getSetCookie().map(x=>x.split(';')[0]).join('; ');assert.ok(cookie);
 const operator=async(endpoint,body)=>{const r=await fetch(url.origin+endpoint,{method:'POST',headers:{'Content-Type':'application/json',Cookie:cookie},body:JSON.stringify(body)});return {status:r.status,value:await r.json()};};
 const location=await operator('/api/file-policy/locations',{path:record.sharedSourceRoot,kind:'directory',capabilities:['read','write'],agent_access:false});assert.equal(location.status,200);assert.ok(location.value.location?.id);assert.equal(location.value.agent_binding_id,null);
 assert.equal((await operator('/api/file-policy/people',{subject_username:record.learner.username,location_id:location.value.location.id,capabilities:['read','write']})).status,200);
 await withCopalBrowser({page:'<title>Owned shared source test</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let i=0;i<600;i++){if(await evaluate('(async()=>!!(await ('+expression+')))()'))return;await delay(50);}throw Error('Shared source condition timed out: '+expression);};
  const request=(endpoint,body)=>evaluate(`(async()=>{const r=await fetch(${JSON.stringify(endpoint)},{credentials:'same-origin',${body===undefined?'':`method:'POST',body:JSON.stringify(${JSON.stringify(body)}),`}headers:{'Content-Type':'application/json'}});return {status:r.status,value:await r.json()};})()`);
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:url.origin+'/login'});await wait(`location.pathname==='/login'&&document.readyState==='complete'`);
  try{assert.equal((await request('/api/auth/login',record.learner)).status,200);}catch{throw Error('Synthetic learner authentication failed');}
  const sibling=await request('/api/file-policy/workspaces/from-path',{path:record.siblingPath,purpose:'app_folder',name:'Ungrantable sibling test'});assert.equal(sibling.status,403,'Exact shared-root grant cannot authorize sibling');
  const sharedPath='/api/copal/treehouse/courses/'+encodeURIComponent(course)+'/activities/'+encodeURIComponent(unit);
  const granted=await request(sharedPath+'/source-open',{sourceRef:{kind:'system',sourceId,courseId:course}});assert.equal(granted.status,200);assert.equal(granted.value.canEdit,true);assert.ok(granted.value.resource.capabilities.includes('write'));
  const before=(await request(sharedPath+'?'+systemQuery)).value;
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-nav')})`);
  await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .copal-treehouse-nav button')})].find(x=>x.textContent==='Library').click()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')})`);
  await evaluate(`(()=>{const i=document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')});i.value=${JSON.stringify(before.unit.title)};i.dispatchEvent(new Event('input',{bubbles:true}));i.form.requestSubmit();})()`);
  const card=shell+' [data-shared-lesson="'+unit+'"]';await wait(`document.querySelector(${JSON.stringify(card)})`);await evaluate(`document.querySelector(${JSON.stringify(card+' button')}).click()`);
  await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].some(x=>x.textContent==='Edit source')`);await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].find(x=>x.textContent==='Edit source').click()`);
  const editor='#copal-notes-modal .cm-content';await wait(`document.querySelector(${JSON.stringify(editor)})`);
  const marker='Synthetic shared source round-trip '+crypto.randomUUID(),modifiers=process.platform==='darwin'?4:2;
  await evaluate(`document.querySelector(${JSON.stringify(editor)}).focus()`);await delay(200);
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'End',code:'End',windowsVirtualKeyCode:35,modifiers});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'End',code:'End',windowsVirtualKeyCode:35,modifiers});await cdp('Input.insertText',{text:'\n\n'+marker});
  await wait(`document.querySelector(${JSON.stringify(editor)}).textContent.includes(${JSON.stringify(marker)})`);
  assert.equal(await evaluate(`(()=>{const b=[...document.querySelectorAll('#copal-notes-modal .copal-note-header-actions button')].find(x=>x.textContent==='Save'&&!x.disabled);b?.click();return !!b;})()`),true);
  await wait(`(async()=>{const r=await fetch(${JSON.stringify(sharedPath+'?'+systemQuery)});return r.ok&&(await r.json()).unit.content.includes(${JSON.stringify(marker)});})()`);
  const after=(await request(sharedPath+'?'+systemQuery)).value;assert.ok(after.unit.content.includes(before.unit.content));assert.notEqual(after.contentSource.contentHead,before.contentSource.contentHead);
  await wait(`document.querySelector('#copal-notes-modal .copal-save-state')?.textContent==='Saved'`);
  const frame=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,'shared-editor-save.png'),Buffer.from(frame.data,'base64'),{mode:0o600});
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-workspace')})`);assert.equal((await request(sharedPath+'?'+systemQuery)).value.unit.content,after.unit.content);
  receipt.beforeHead=before.contentSource.contentHead;receipt.afterHead=after.contentSource.contentHead;receipt.locationId=location.value.location.id;
  receipt.checks.push('operator exact-root policy; fresh normal learner; sibling denied; actual shared Editor Save and fresh reload');
 });
 fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n',{mode:0o600});console.log('Shared source actual Editor receipt:',path.join(output,'receipt.json'));
});

// Read-only completion phase: reuse saved bytes, never retype/re-save for media.
test('saved shared source reopens in clean native Editor with canonical saved state',{
 skip:!enabled||process.env.TREEHOUSE_POLISH_COURSE_FILES!=='1',timeout:120000,
},async()=>{
 const {url,record}=fixture();const output=fs.mkdtempSync(path.join(os.tmpdir(),'treehouse-source-reopen-'));
 const receipt={candidateOrigin:url.origin,checks:[],readOnlyReopen:true};
 await withCopalBrowser({page:'<title>Saved source reopen</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let i=0;i<600;i++){if(await evaluate('(async()=>!!(await ('+expression+')))()'))return;await delay(50);}throw Error('Source reopen condition timed out: '+expression);};
  const get=async endpoint=>evaluate(`fetch(${JSON.stringify(endpoint)},{credentials:'same-origin'}).then(async r=>({status:r.status,value:await r.json()}))`);
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await cdp('Page.navigate',{url:url.origin+'/login'});await wait(`location.pathname==='/login'&&document.readyState==='complete'`);
  try{assert.equal(await evaluate(`fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(record.learner)})}).then(r=>r.status)`),200);}catch{throw Error('Synthetic learner authentication failed');}
  const endpoint='/api/copal/treehouse/courses/'+encodeURIComponent(course)+'/activities/'+encodeURIComponent(unit)+'?'+systemQuery;
  const detail=await get(endpoint);assert.equal(detail.status,200);assert.ok(detail.value.unit.content.includes('Synthetic shared source round-trip'));assert.equal(detail.value.contentSource.canEditInFiles,true);
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-nav')})`);await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .copal-treehouse-nav button')})].find(x=>x.textContent==='Library').click()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')})`);await evaluate(`(()=>{const i=document.querySelector(${JSON.stringify(shell+' input[aria-label="Search learning"]')});i.value=${JSON.stringify(detail.value.unit.title)};i.dispatchEvent(new Event('input',{bubbles:true}));i.form.requestSubmit();})()`);
  const card=shell+' [data-shared-lesson="'+unit+'"]';await wait(`document.querySelector(${JSON.stringify(card)})`);await evaluate(`document.querySelector(${JSON.stringify(card+' button')}).click()`);await wait(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].some(x=>x.textContent==='Edit source')`);await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' button')})].find(x=>x.textContent==='Edit source').click()`);
  await wait(`document.querySelector('#copal-notes-modal .cm-content')?.textContent?.includes('Synthetic shared source round-trip')`);
  await wait(`document.querySelector('#copal-notes-modal .copal-save-state')?.textContent==='Saved'`);
  assert.equal(await evaluate(`!!document.querySelector('#copal-notes-modal .copal-note-tab-state[aria-label="Unsaved changes"]')`),false);
  const image=async name=>{const frame=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});fs.writeFileSync(path.join(output,name+'.png'),Buffer.from(frame.data,'base64'),{mode:0o600});};
  await image('shared-source-saved-normal');await cdp('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:false});await delay(250);await image('shared-source-saved-narrow');
  const fresh=await get(endpoint);assert.equal(fresh.value.unit.content,detail.value.unit.content);assert.equal(fresh.value.contentSource.contentHead,detail.value.contentSource.contentHead);
  receipt.sharedHead=detail.value.contentSource.contentHead;receipt.sharedBodySha256=crypto.createHash('sha256').update(detail.value.unit.content).digest('hex');receipt.canonicalSaveState=await evaluate(`document.querySelector('#copal-notes-modal .copal-save-state').textContent`);receipt.checks.push('actual fresh normal-auth shared Editor; exact persisted text; canonical Saved footer/no dirty tab; normal+narrow frames; no new save');
 });
 fs.writeFileSync(path.join(output,'receipt.json'),JSON.stringify(receipt,null,2)+'\n',{mode:0o600});console.log('Saved source reopen receipt:',path.join(output,'receipt.json'));
});
