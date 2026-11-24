// Actual consumer + actual stylesheet, synthetic responses only. This stage
// proves UI failure/success, never backend speed, indexing speed or GPU use.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
import {browserGraphObservation,browserMediaMilestones,usefulView,measurementClassification} from './helpers/s00_ui_baseline_contract.mjs';
import {GOLDEN,queryFixture,syntheticPng} from './fixtures/s00_ui_fixture.mjs';

assert(process.argv.slice(2).every(a=>a==='--record-baseline'),'Unknown option');
const record=process.argv.includes('--record-baseline');
const measurement=measurementClassification(process.env.OPENCLANK_S00_MEASUREMENT_CLASS);
const output=fs.mkdtempSync(path.join(os.tmpdir(),'openclank-s00-usefulness-'));fs.chmodSync(output,0o700);
const scenarios=[];
const page=`<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css">
<body style="display:block!important;margin:0"><main class="copal-view" style="position:fixed;left:0;top:0;width:100vw;height:100vh;overflow:auto"><div class="copal-graph-wrap" data-code-graph><div id="consumer"></div></div></main>
<script type="module">
import {mountCodeGraphWorkbench} from '/static/js/copal/codeGraphWorkbench.js';
window.mountFixture=(response)=>{window.controller?.dispose();document.querySelector('#consumer').replaceChildren();window.calls=[];
window.controller=mountCodeGraphWorkbench({element:document.querySelector('#consumer'),workspaceId:'s00',isCurrent:()=>true,
readPreference:async()=>false,readResourceCapabilities:async()=>({available:true}),subscribeIndex:(_,{onStatus})=>{queueMicrotask(()=>onStatus({lifecycle:'ready',head_generation:'s00-head',freshness:'current'}));return()=>{};},
request:async(_,query)=>{window.calls.push(query);return {...response,operation:query.operation};},onController:model=>window.model=model});};window.ready=true;
</script>`;
let failure;
try {
  await withCopalBrowser({page},async({evaluate,until,cdp})=>{
    try {
    await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:800,deviceScaleFactor:1,mobile:false});
    await until('window.ready');
    // Exposure-oracle calibration only: generated image and editable DOM under
    // the real stylesheet. No media speed, product Editor or GPU claim.
    await evaluate(`(()=>{const p=document.querySelector('#consumer');p.innerHTML='<strong class="copal-inline-title">image.md</strong><div class="cm-editor"><div class="cm-content" contenteditable="true" style="min-height:80px">S00 overlay calibration</div></div><div id="media-wrap" style="width:64px;height:64px"><img id="media-calibration" style="width:64px;height:64px;pointer-events:none"/></div>';document.querySelector('#media-calibration').src=${JSON.stringify('data:image/png;base64,'+syntheticPng(8,8,2).toString('base64'))};window.__s00MediaTarget={kind:'editor',name:'image.md',cue:'S00 overlay calibration',image:false};})()`);
    assert.equal((await evaluate(`(${browserMediaMilestones})()`)).documentReady,true);
    await evaluate(`document.body.insertAdjacentHTML('beforeend','<div id="media-cover" style="position:fixed;inset:0;z-index:999999;background:#000"></div>')`);
    assert.equal((await evaluate(`(${browserMediaMilestones})()`)).documentReady,false,'Covered editable DOM must not be ready');await evaluate(`document.querySelector('#media-cover').remove()`);
    await evaluate(`window.__s00MediaTarget={kind:'files',resourceId:'calibration',count:1,browser:{getCurrentDirectory:()=>({resourceId:'calibration',entries:[1],nextCursor:null}),captureCommands:()=>({navigating:false}),element:{querySelectorAll:()=>[document.querySelector('#media-calibration')]}}};document.querySelector('#media-calibration').decode()`);
    await evaluate(`(${browserMediaMilestones})()`);await evaluate('Promise.resolve()');assert.equal((await evaluate(`(${browserMediaMilestones})()`)).imageDecodedVisible,true,'Decoded pointer-transparent image may hit its own wrapper');
    await evaluate(`document.body.insertAdjacentHTML('beforeend','<div id="media-cover" style="position:fixed;inset:0;z-index:999999;background:#000"></div>')`);assert.equal((await evaluate(`(${browserMediaMilestones})()`)).imageDecodedVisible,false,'Covered decoded image must not be exposed');await evaluate(`document.querySelector('#media-cover').remove();document.querySelector('#consumer').replaceChildren();delete window.__s00MediaTarget`);
    // Metric-oracle browser check, NOT a renderer acceptance substitute. Same
    // real stylesheet and screen geometry, intentionally noninteractive SVG.
    await evaluate(`(()=>{const p=document.querySelector('#consumer');p.innerHTML='<div class="code-workbench-viewport" data-code-viewport style="height:640px"><svg viewBox="0 0 1200 700"><g class="code-point" data-code-group="src"><text x="40" y="60" style="pointer-events:none">src</text></g><g class="code-point" data-code-group="src/api"><text x="40" y="120" style="pointer-events:none">api</text></g><g class="code-point" data-code-group="src/storage"><text x="40" y="180" style="pointer-events:none">storage</text></g><line class="code-edge" data-code-edge="e" data-from="a" data-to="b" x1="300" y1="100" x2="500" y2="200" style="pointer-events:none"/></svg></div>';})()`);
    const painted=await evaluate(`(${browserGraphObservation})()`);assert.equal(painted.groups.length,3,'Visible noninteractive labels remain semantic evidence');assert.equal(painted.edges.length,1,'Visible noninteractive stroke remains semantic evidence');
    await evaluate(`document.querySelector('[data-code-edge]').style.cssText='stroke:none;pointer-events:all'`);assert.equal((await evaluate(`(${browserGraphObservation})()`)).edges.length,0,'Hit-testable nonpainted primitive is not evidence');await evaluate(`document.querySelector('[data-code-edge]').style.cssText='pointer-events:none'`);
    await evaluate(`document.querySelector('[data-code-edge]').style.cssText='stroke:rgba(0,0,0,0);pointer-events:all'`);assert.equal((await evaluate(`(${browserGraphObservation})()`)).edges.length,0,'Transparent color is not painted evidence');await evaluate(`document.querySelector('[data-code-edge]').style.cssText='pointer-events:none'`);
    await evaluate(`document.querySelector('[data-code-viewport] svg').insertAdjacentHTML('beforeend','<rect id="same-svg-cover" x="0" y="0" width="1200" height="700" fill="black"/>')`);const internalCover=await evaluate(`(${browserGraphObservation})()`);assert.equal(internalCover.groups.length,0);assert.equal(internalCover.edges.length,0);await evaluate(`document.querySelector('#same-svg-cover').remove()`);
    await evaluate(`document.querySelector('[data-code-viewport]').insertAdjacentHTML('beforeend','<div id="metric-overlay" style="position:fixed;inset:0;z-index:999999;background:#000"></div>')`);
    const covered=await evaluate(`(${browserGraphObservation})()`);assert.equal(covered.groups.length,0);assert.equal(covered.edges.length,0);assert.equal(covered.allExpectedVisible,false);
    await evaluate(`document.querySelector('#consumer').replaceChildren()`);
    for(const semantic of [false,true]){
      await evaluate(`window.mountFixture(${JSON.stringify(queryFixture('overview',semantic))})`);
      await until(`window.model?.snapshot().status==='ready'&&document.querySelector('[data-code-viewport]')`);
      await evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
      const observation=await evaluate(`(${browserGraphObservation})()`);
      if(semantic)assert.equal(observation.semanticEdges.length,2,'Actual complete UI labels map the visible edge endpoints');
      const verdict=usefulView(observation,GOLDEN);
      scenarios.push({scenario:semantic?'known-topology-rich-response':'five-file-inventory',mode:'static-mocked-data-actual-consumer',observation,verdict,
        ...measurement,queryCount:await evaluate('window.calls.length'),manualRescue:false});
      const shot=await cdp('Page.captureScreenshot',{format:'png'});
      fs.writeFileSync(path.join(output,`${semantic?'topology':'inventory'}.png`),Buffer.from(shot.data,'base64'),{flag:'wx',mode:0o600});
    }
    } catch(error) {
      try {const shot=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(output,'failure.png'),Buffer.from(shot.data,'base64'),{flag:'wx',mode:0o600});} catch {}
      throw error;
    }
  });
  assert(scenarios.every(s=>s.verdict.passed),'First-use graph usefulness failed; see retained result and screenshots');
}catch(error){failure=String(error.message||error);}
const result={schema:'openclank.s00-usefulness/v1',status:failure?'failed':'passed',mode:'static-mocked-data-actual-consumer',
  ...measurement,
  backendPerformanceQualified:false,gpuQualified:false,browser:'owned headless Chrome --disable-gpu',scenarios,failure:failure||null,
  recordingOnly:record,defaultAcceptanceUnchanged:true};
fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n',{flag:'wx',mode:0o600});
console.log(JSON.stringify({output,status:result.status,recordingOnly:record}));
if(failure&&!record)process.exitCode=1;
