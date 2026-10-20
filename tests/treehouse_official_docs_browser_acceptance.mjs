// Opt-in normal-auth full-source-shell journey. The harness only owns Chrome.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {setTimeout as delay} from 'node:timers/promises';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const candidate=process.env.TREEHOUSE_DOCS_CANDIDATE_URL;
const credentialPath=process.env.TREEHOUSE_DOCS_CANDIDATE_CREDENTIALS;
test('maintained TreeHouse article shares identity and read-only images in actual Wiki and Editor',{skip:!candidate||!credentialPath,timeout:180000},async()=>{
 const credentials=JSON.parse(fs.readFileSync(credentialPath,'utf8')).learner;
 await withCopalBrowser({page:'<!doctype html><title>Full-shell navigation</title>',cdpTimeoutMs:30000},async({cdp,evaluate})=>{
  const wait=async expression=>{for(let i=0;i<400;i++){if(await evaluate(expression))return;await delay(50);}throw new Error(`Full-shell timeout: ${expression} · ${JSON.stringify(await evaluate('({url:location.href,ready:document.readyState,body:document.body.innerText.slice(-1800),help:document.querySelector("#settings-help-status")?.textContent})'))}`);};
  await cdp('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
  await cdp('Page.navigate',{url:candidate});await wait(`location.origin===${JSON.stringify(new URL(candidate).origin)}`);
  assert.equal(await evaluate(`(async()=>{const r=await fetch('/api/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify(${JSON.stringify(credentials)})});return r.ok;})()`),true);
  await evaluate('window.__docsPriorDocument=true');
  await cdp('Page.navigate',{url:candidate});await wait('!window.__docsPriorDocument && document.readyState==="complete"');
  const navigation=await evaluate('({width:innerWidth,height:innerHeight,settingsVisible:!!document.querySelector("#user-bar-settings")?.getClientRects().length,sidebarExpanded:document.querySelector("#hamburger-btn")?.getAttribute("aria-expanded"),hamburgerVisible:!!document.querySelector("#hamburger-btn")?.getClientRects().length})');
  assert.equal(navigation.width,1440);assert.equal(navigation.height,1000);
  if(!navigation.settingsVisible){assert.equal(navigation.hamburgerVisible,true);await evaluate('document.querySelector("#hamburger-btn").click()');}
  await wait('document.querySelector("#user-bar-settings")?.getClientRects().length');

  const article=await evaluate('(async()=>{const r=await fetch("/api/copal/official/docs");return (await r.json()).articles.find(a=>a.docId==="openclank-docs-treehouse");})()');
  assert.ok(article);assert.equal(article.read_only,true);
  const record=JSON.parse(article.content),body=record.extensions.interchange.source;
  const images=[...body.matchAll(/!\[[^\]]*\]\((\/static\/docs\/media\/treehouse-[^/\s)]+\/preview\.webp)\)/g)].map(m=>m[1]);
  assert.equal(images.length,4,'All four active TreeHouse media bindings must be installed before this journey');
  assert.equal(new Set(images).size,4);
  assert.deepEqual(images,['/static/docs/media/treehouse-four-main-quests-20261014/preview.webp', '/static/docs/media/treehouse-connected-topics-20261014/preview.webp', '/static/docs/media/treehouse-openclank-work-20261014/preview.webp', '/static/docs/media/treehouse-current-work-20261014/preview.webp']);
  for(const title of ['Programming','Games','AI','Open Clank'])assert.ok(body.includes('**'+title+'**'));
  assert.ok(body.includes('Previous answer'));
  await evaluate('document.querySelector("#user-bar-settings").click()');await wait('!document.querySelector("#settings-modal").classList.contains("hidden")');
  await evaluate('document.querySelector("[data-settings-tab=help]").click();document.querySelector("#settings-help-wiki").click()');
  await wait('document.querySelector(".copal-wiki-page strong")');
  await evaluate('[...document.querySelectorAll(".copal-wiki-page")].find(n=>n.querySelector("strong")?.textContent==="OpenClank/TreeHouse").click()');
  await wait('document.querySelector(".copal-wiki-title")?.textContent==="OpenClank/TreeHouse"');
  assert.equal(await evaluate('[...document.querySelectorAll(".copal-wiki-page-actions button")].find(n=>n.textContent==="Save").disabled'),true);
  assert.equal(await evaluate('document.querySelector(".copal-wiki-readonly")?.innerText.includes("read-only")'),true);
  const checkImages=async surface=>{
   for(const url of images){
    const selector=JSON.stringify(surface+' .copal-note-leaf img');
    await wait(`[...document.querySelectorAll(${selector})].some(img=>img.getAttribute('src')===${JSON.stringify(url)})`);
    await evaluate(`[...document.querySelectorAll(${selector})].find(img=>img.getAttribute('src')===${JSON.stringify(url)}).scrollIntoView({block:'center'})`);
    await wait(`[...document.querySelectorAll(${selector})].some(img=>img.getAttribute('src')===${JSON.stringify(url)}&&img.complete&&img.naturalWidth>0)`);
   }
   await wait(`(()=>{const leaves=[...document.querySelectorAll(${JSON.stringify(surface+' .copal-note-leaf')})].filter(n=>n.getClientRects().length&&n.innerText.includes('Explore a topic'));return leaves.some(n=>${JSON.stringify(images)}.every(url=>[...n.querySelectorAll('img')].some(img=>img.getAttribute('src')===url&&img.complete&&img.naturalWidth>0)));})()`);
   assert.equal(await evaluate(`(()=>{const leaf=[...document.querySelectorAll(${JSON.stringify(surface+' .copal-note-leaf')})].find(n=>n.getClientRects().length&&n.innerText.includes("Explore a topic"));return !!leaf&&!leaf.querySelector("textarea,[contenteditable=true]")&&leaf.querySelector(".copal-leaf-mode")?.textContent==="Read only";})()`),true);
  };
  await checkImages("#copal-wiki-modal");
  await evaluate('[...document.querySelectorAll(".copal-wiki-page-actions button")].find(n=>n.textContent==="Open in Editor").click()');
  await wait('[...document.querySelectorAll("#copal-notes-modal .copal-note-tab-label")].some(n=>n.getClientRects().length&&n.getAttribute("aria-label")==="Open OpenClank/TreeHouse, read only")');
  await checkImages("#copal-notes-modal");
  const unchanged=await evaluate('(async()=>{const r=await fetch("/api/copal/official/docs");return (await r.json()).articles.find(a=>a.docId==="openclank-docs-treehouse");})()');
  assert.equal(unchanged.docId,article.docId);assert.equal(unchanged.content,article.content);assert.equal(unchanged.read_only,true);
 });
});
