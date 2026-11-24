// Opt-in actual packaged app, normal synthetic auth. Release owner changes
// fixture storage only between runs; this test never activates or edits schema.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const candidate=process.env.TREEHOUSE_STORAGE_CANDIDATE_URL;
const credentialPath=process.env.TREEHOUSE_STORAGE_CANDIDATE_CREDENTIALS;
const expected=process.env.TREEHOUSE_STORAGE_EXPECTED_STATUS;
const shell='#copal-treehouse-modal';
test('packaged shared availability preserves private navigation and maintained help',{skip:!candidate||!credentialPath||!expected,timeout:120000},async()=>{
 const url=new URL(candidate),record=JSON.parse(fs.readFileSync(credentialPath,'utf8'));
 assert.equal(url.protocol,'http:');assert.ok(['127.0.0.1','localhost','[::1]'].includes(url.hostname));
 assert.ok(!['7777','54190'].includes(url.port),'Only a release-owner isolated fixture, never installed/user preview');
 assert.equal(new URL(record.url).origin,url.origin);assert.ok(record.learner?.username&&record.learner?.password);
 assert.ok(['activation-required','incompatible','ready'].includes(expected));
 await withCopalBrowser({page:'<title>Owned packaged availability bootstrap</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let n=0;n<700;n++){if(await evaluate(expression))return;await delay(50);}throw Error('Availability fixture timeout: '+expression+' · '+JSON.stringify(await evaluate('({path:location.pathname,body:document.body.innerText.slice(-1200)})')));};
  const getSnapshot=()=>evaluate('fetch("/api/copal/treehouse").then(r=>{if(!r.ok)throw Error("snapshot "+r.status);return r.json()})');
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});await cdp('Page.navigate',{url:url.origin+'/'});await wait(`location.origin===${JSON.stringify(url.origin)}`);
  assert.equal(await evaluate(`fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(record.learner)})}).then(r=>r.ok)`),true);
  await cdp('Page.addScriptToEvaluateOnNewDocument',{source:`window.__storageWire=[];const originalFetch=window.fetch;window.fetch=async(...args)=>{try{const path=new URL(typeof args[0]==='string'?args[0]:args[0].url,location.href).pathname;if(path.startsWith('/api/copal/treehouse')){const b=args[1]?.body?JSON.parse(args[1].body):null;window.__storageWire.push({path,type:b?.type,sourceKind:b?.payload?.sourceRef?.kind});}}catch{}return originalFetch(...args);};`});
  await cdp('Page.navigate',{url:url.origin+'/treehouse'});await wait(`document.querySelector(${JSON.stringify(shell+' .copal-treehouse-workspace')})`);
  const before=await getSnapshot();assert.equal(before.globalAvailability.status,expected);
  const courseId=record.privateCourseId||process.env.TREEHOUSE_STORAGE_PRIVATE_COURSE_ID||Object.values(before.state.courses).find(c=>c.status==='published'&&c.courseType==='tutorial')?.id;
  assert.ok(courseId,'Owner must supply one published synthetic private Tutorial');assert.equal(before.state.courses[courseId].status,'published');
  if(expected==='ready'){
   await wait(`document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')}).length===4`);
   assert.deepEqual(await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest]')})].map(n=>n.dataset.mainQuest)`),['treehouse:programming','treehouse:games','treehouse:ai','treehouse:openclank']);
   assert.equal(await evaluate(`!!document.querySelector(${JSON.stringify(shell+' .th-global-availability')})`),false);
   assert.ok(before.globalCatalogue);return;
  }
  assert.equal(before.globalCatalogue,null);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-global-availability')})`);
  assert.match(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-global-availability')}).innerText`),expected==='incompatible'?/incompatible/:/need activation/);
  assert.equal(await evaluate(`document.querySelectorAll(${JSON.stringify(shell+' [data-main-quest],'+shell+' .th-web-progress')}).length`),0);
  const privateSelector=shell+' .th-course-card[data-treehouse-id="'+courseId+'"] button';
  await wait(`document.querySelector(${JSON.stringify(privateSelector)})`);await evaluate(`document.querySelector(${JSON.stringify(privateSelector)}).click()`);await wait(`document.querySelector(${JSON.stringify(shell+' .th-lesson')})`);
  const expectedActivity=Object.values(before.state.activities).find(a=>a.courseId===courseId&&a.status==='published');assert.ok(expectedActivity);
  assert.equal(await evaluate(`document.querySelector(${JSON.stringify(shell+' .th-lesson h2')}).textContent`),expectedActivity.title);
  // Native recheck cannot issue schema changes or resurrect stale global data.
  await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-global-availability button')})].find(b=>b.textContent==='Recheck shared quests').click()`);
  await wait(`document.querySelector(${JSON.stringify(shell+' .th-global-availability button')})&&!document.querySelector(${JSON.stringify(shell+' .th-global-availability button')}).disabled`);
  assert.equal((await getSnapshot()).globalAvailability.status,expected);
  const wire=await evaluate('window.__storageWire');assert.equal(wire.some(r=>r.sourceKind==='system'||/activat|migrat|retire/.test(r.type||'')),false);
  assert.equal(wire.some(r=>r.path.includes('/main-quests/')||r.path.includes('/summary')||r.path.includes('/activities/')),false);
  const after=await getSnapshot();assert.deepEqual(after.state.courses,before.state.courses);assert.deepEqual(after.state.courseGrants,before.state.courseGrants);
  // Actual maintained help, not a stub or Editor prewarm. Preview does not earn work.
  await cdp('Page.bringToFront');await evaluate(`[...document.querySelectorAll(${JSON.stringify(shell+' .th-global-availability button')})].find(b=>b.textContent==='Open upgrade help').focus()`);
  assert.equal(await evaluate('document.activeElement.textContent'),'Open upgrade help');await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await wait('document.querySelector(".copal-wiki-page strong")');await evaluate(`[...document.querySelectorAll('.copal-wiki-page')].find(n=>n.querySelector('strong')?.textContent==='OpenClank/TreeHouse').click()`);await wait('document.querySelector(".copal-wiki-title")?.textContent==="OpenClank/TreeHouse"');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-wiki-page-actions button")].find(n=>n.textContent==="Save").disabled'),true);
  assert.equal(await evaluate('document.querySelector(".copal-wiki-readonly")?.innerText.includes("read-only")'),true);
  await wait(`document.querySelector('#copal-wiki-modal .copal-note-leaf')?.innerText.includes('schema-only activation')`);
 });
});
