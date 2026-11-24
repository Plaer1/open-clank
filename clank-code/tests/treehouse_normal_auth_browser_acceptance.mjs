// Opt-in against the platform-owned isolated candidate; never connects by default.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {execFileSync} from 'node:child_process';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const candidate=process.env.TREEHOUSE_CANDIDATE_URL,credentialPath=process.env.TREEHOUSE_CANDIDATE_CREDENTIALS;
test('real-auth native routes persist learner response and resume across workspaces',{skip:!candidate||!credentialPath},async()=>{
 const toyDirectory=fs.mkdtempSync(path.join(os.tmpdir(),'treehouse-owned-toy-'));
 try {
 const toy=path.join(toyDirectory,'toy'),assembly=path.join(toyDirectory,'toy.s');
 const toySource='#include <stdio.h>\nint main(void){puts("7");return 0;}\n';
 execFileSync('clang',['-x','c','-O0','-g','-o',toy,'-'],{input:toySource});
 execFileSync('clang',['-x','c','-O0','-S','-o',assembly,'-'],{input:toySource});
 const actualOutput=execFileSync(toy,{encoding:'utf8'}).trim();assert.equal(actualOutput,'7');assert.match(fs.readFileSync(assembly,'utf8'),/main/);
 const format=execFileSync('file',[toy],{encoding:'utf8'}).replace(toy,'Owned toy').trim();
 const symbols=execFileSync('nm',[toy],{encoding:'utf8'});assert.match(symbols,/main/);
 const actualEvidence=format+'. Source compiled to assembly; assembly contains main and machine instructions. Driver linked an executable with main symbol; loader ran it with stdout='+actualOutput+'. Source->assembly->linked format->loaded process observed.';
 const credentials=JSON.parse(fs.readFileSync(credentialPath,'utf8')).learner;
 await withCopalBrowser({page:'<!doctype html><title>Candidate navigation</title>'},async({cdp,evaluate,until})=>{
  await cdp('Page.navigate',{url:candidate});await until(`location.origin === ${JSON.stringify(new URL(candidate).origin)}`);
  const loggedIn=await evaluate(`(async()=>{const response=await fetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},credentials:'same-origin',body:JSON.stringify(${JSON.stringify(credentials)})});return response.ok;})()`);assert.equal(loggedIn,true);
  await cdp('Page.navigate',{url:new URL('/candidate-ui',candidate).href});await until('window.__candidateReady || window.__candidateError');
  assert.equal(await evaluate('window.__candidateError'),undefined);
  assert.equal(await evaluate('document.querySelectorAll("[data-main-quest]").length'),3);
  assert.equal(await evaluate('window.__candidateRequests.some(c=>c.path.includes("/activities/"))'),false);
  await evaluate('[...document.querySelectorAll("[data-main-quest]")].find(n=>n.dataset.mainQuest==="treehouse:programming").click()');
  await until('document.querySelector("[data-subject-id]")');
  await evaluate('[...document.querySelectorAll("[data-subject-id]")].find(n=>n.dataset.subjectId==="cs:values").click()');
  await until('document.querySelector("textarea")');
  await evaluate('var response=document.querySelector("textarea");response.value="6 integer; 42 text; 6 integer. Parsing pear fails and needs handling.";response.dispatchEvent(new Event("input"));[...document.querySelectorAll("button")].find(n=>n.textContent==="Submit response").click()');
  await until('document.querySelector(".th-web-work")?.innerText.includes("Submitted · self-reported work")');
  const originAccount=await evaluate('window.__feature.snapshot.accountId');assert.ok(originAccount);
  const subjectBefore=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Avalues/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=one");return r.json();})()');
  assert.ok(subjectBefore.progress.completed>=1);assert.equal(subjectBefore.progress.resumeActivityId,'cs:values:values-types');
  await evaluate('window.__setWorkspace("two")');await until('document.querySelector("[data-treehouse-unit]")');
  const subjectAfter=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Avalues/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');
  assert.equal(subjectAfter.progress.completed,subjectBefore.progress.completed);assert.equal(subjectAfter.progress.resumeActivityId,subjectBefore.progress.resumeActivityId);
  assert.equal(await evaluate('window.__feature.snapshot.accountId'),originAccount);
  assert.equal(await evaluate('Object.keys(window.__feature.snapshot.state.courses).some(id=>id.startsWith("cs:"))'),false);
  await evaluate('document.querySelector(".th-web-breadcrumbs button").click()');
  await evaluate('[...document.querySelectorAll("[data-main-quest]")].find(n=>n.dataset.mainQuest==="treehouse:programming").click()');await until('document.querySelector("[data-subject-id]")');
  await evaluate('[...document.querySelectorAll("[data-subject-id]")].find(n=>n.dataset.subjectId==="cs:systems").click()');await until('document.querySelector(".th-web-work select")');
  await evaluate('var work=[...document.querySelectorAll(".th-web-work")].find(n=>n.querySelector("select"));work.querySelector("textarea").value="A failed attempt is retained for debugging.";work.querySelector("select").value="failed";[...work.querySelectorAll("button")].find(n=>n.textContent==="Save attempt").click()');
  await until('[...document.querySelectorAll(".th-web-work")].some(n=>n.querySelector("select")&&n.innerText.includes("practical work pending"))');
  const beforeSuccess=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');
  const pendingDetail=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/activities/cs%3Asystems%3Acompile-link-load?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');
  assert.equal(pendingDetail.submissions['cs:systems:assessment:compile-inspect-run'].status,'pending');
  await evaluate(`var work=[...document.querySelectorAll(".th-web-work")].find(n=>n.querySelector("select"));work.querySelector("textarea").value="I compiled and inspected the owned toy.";work.querySelector("select").value="success";work.querySelector("input[type=checkbox]").checked=true;work.querySelector("textarea[aria-label='Outcome evidence']").value=${JSON.stringify(actualEvidence)};[...work.querySelectorAll("button")].find(n=>n.textContent==="Save attempt").click()`);
  await until('[...document.querySelectorAll(".th-web-work")].some(n=>n.querySelector("select")&&n.innerText.includes("Submitted · self-reported work"))');
  const afterSuccess=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');
  const successDetail=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/activities/cs%3Asystems%3Acompile-link-load?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');
  assert.equal(successDetail.submissions['cs:systems:assessment:compile-inspect-run'].status,'submitted');
  assert.equal(afterSuccess.progress.completed,beforeSuccess.progress.completed,'Elective practical success does not replace required conceptual work');
  await evaluate(`window.__resetSettled=false;var nativeFetch=window.fetch;window.fetch=async(...args)=>{const r=await nativeFetch(...args);if(new URL(String(args[0]),location.href).pathname.endsWith("/commands")&&JSON.parse(args[1]?.body||"{}").type==="progress.reset"){window.__resetReceipt=await r.clone().json();window.__resetSettled=true;}return r;}`);
  await evaluate('window.styledConfirm=async()=>true;[...document.querySelectorAll("button")].find(n=>n.textContent==="Reset this subject").click()');
  await until('window.__resetSettled && [...document.querySelectorAll(".th-web-work")].some(n=>n.querySelector("select")&&n.innerText.includes("Reset · work pending")&&n.querySelector("textarea").value==="")');
  assert.ok(await evaluate(`window.__resetReceipt.resetGeneration > ${afterSuccess.resetGeneration}`));
  const reset=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');assert.ok(reset.resetGeneration>afterSuccess.resetGeneration);
  const resetDetail=await evaluate('(async()=>{const r=await fetch("/api/copal/treehouse/courses/cs%3Asystems/activities/cs%3Asystems%3Acompile-link-load?source=system&sourceId=openclank%3Atreehouse-core&workspace=two");return r.json();})()');assert.ok(!resetDetail.submissions['cs:systems:assessment:compile-inspect-run']||resetDetail.submissions['cs:systems:assessment:compile-inspect-run'].status==='reset');

  for(const journey of [
   {root:'treehouse:games',subject:'games:design',unit:'games:design:rules-and-state',prompt:/counter|rule|state/i,response:'Removing two from seven leaves five counters and passes the turn. Legal moves remove one or two without going below zero; removing three is illegal. Zero counters after a legal move is terminal. Allowing three would add choices and change the reachable states.'},
   {root:'treehouse:ai',subject:'ai:models',unit:'ai:models:identity',prompt:/architecture|checkpoint|model/i,response:'The blueprint is the architecture, the checkpoint contains trained parameters, and the website is the surrounding application. Prompts, tools, and settings can change the website behavior without changing the checkpoint weights.'}
  ]) {
   await evaluate('document.querySelector(".th-web-breadcrumbs button").click()');
   await evaluate(`[...document.querySelectorAll("[data-main-quest]")].find(n=>n.dataset.mainQuest===${JSON.stringify(journey.root)}).click()`);await until('document.querySelector("[data-subject-id]")');
   await evaluate(`[...document.querySelectorAll("[data-subject-id]")].find(n=>n.dataset.subjectId===${JSON.stringify(journey.subject)}).click()`);await until('document.querySelector("textarea")');
   const detail=await evaluate(`(async()=>{const r=await fetch(${JSON.stringify('/api/copal/treehouse/courses/'+encodeURIComponent(journey.subject)+'/activities/'+encodeURIComponent(journey.unit)+'?source=system&sourceId=openclank%3Atreehouse-core&workspace=two')});return r.json();})()`);
   assert.equal(detail.unit.id,journey.unit);assert.match(detail.assignments.find(a=>a.completionEvidence==='response').prompt,journey.prompt);
   await evaluate(`var work=[...document.querySelectorAll(".th-web-work")].find(n=>!n.querySelector("select"));var response=work.querySelector("textarea");response.value=${JSON.stringify(journey.response)};response.dispatchEvent(new Event("input"));[...work.querySelectorAll("button")].find(n=>n.textContent==="Submit response").click()`);
   await until('[...document.querySelectorAll(".th-web-work")].some(n=>!n.querySelector("select")&&n.innerText.includes("Submitted · self-reported work"))');
   await evaluate('window.__setWorkspace("one")');await until('document.querySelector("textarea")');
   const summary=await evaluate(`(async()=>{const r=await fetch(${JSON.stringify('/api/copal/treehouse/courses/'+encodeURIComponent(journey.subject)+'/summary?source=system&sourceId=openclank%3Atreehouse-core&workspace=one')});return r.json();})()`);
   assert.equal(summary.progress.resumeActivityId,journey.unit);assert.ok(summary.progress.completed>=1);
   assert.equal(await evaluate('[...document.querySelectorAll(".th-web-work")].find(n=>!n.querySelector("select")).querySelector("textarea").value'),journey.response);
   await evaluate('window.__setWorkspace("two")');await until('document.querySelector("textarea")');
  }
 });
 } finally { fs.rmSync(toyDirectory,{recursive:true,force:true}); }
});
