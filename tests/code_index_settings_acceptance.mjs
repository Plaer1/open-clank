#!/usr/bin/env node
// Real System card/module/CSS; synthetic HTTP responses, not native enforcement.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const out=fs.mkdtempSync(path.join(os.tmpdir(),'openclank-index-settings-browser-'));fs.chmodSync(out,0o700);
const files=['static/index.html','static/js/settings.js','static/js/codeIndexSettings.js','static/style.css'];
const hash=p=>createHash('sha256').update(fs.readFileSync(p)).digest('hex');
const pins=()=>Object.fromEntries(files.map(p=>[p,hash(p)]));
const receipt={status:'running',mode:'static-component-synthetic-settings-api',groups:[],failures:[],sourcePins:pins(),nativeQualified:false,normalAuthQualified:false,browserHarnessFinallyCompleted:false};
const html=fs.readFileSync('static/index.html','utf8');
const card=html.match(/<section class="admin-card" data-code-index-settings[\s\S]*?<\/section>/)?.[0];assert(card);
let state='pending',desired=2,calls=[];
const page=`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/style.css"><style>body{display:block;margin:20px}main{max-width:700px}</style><main>${card}</main><script type="module">import {mountCodeIndexSettings} from '/static/js/codeIndexSettings.js';window.__errors=[];addEventListener('error',e=>__errors.push(e.error?.name||'Error'));addEventListener('unhandledrejection',e=>__errors.push(e.reason?.name||'Error'));mountCodeIndexSettings(document.querySelector('[data-code-index-settings]'));window.__ready=true;</script>`;
try {
 await withCopalBrowser({page,request:async(req,res)=>{
  if(req.url!=='/api/auth/settings')return false;
  let body='';for await(const chunk of req)body+=chunk;
  calls.push({method:req.method,body:body?JSON.parse(body):null});res.setHeader('Content-Type','application/json');
  if(state==='denied'||state==='error'){res.writeHead(state==='denied'?403:500);res.end(JSON.stringify({error:'DO_NOT_RENDER_PRIVATE_DETAIL'}));return true;}
  if(req.method==='POST'){assert.deepEqual(Object.keys(JSON.parse(body)),['code_index_max_threads']);desired=JSON.parse(body).code_index_max_threads;}
  res.end(JSON.stringify({code_index_max_threads:desired,code_index_settings_revision:3,code_index_scheduler:{native_status:state==='pending'?'unavailable':'available',status:state,desired,revision:3,native_revision:state==='pending'?null:3,effective_revision:state==='applied'?3:2,effective:state==='pending'?null:state==='draining'?2:desired,active:state==='pending'?null:state==='draining'?2:0,queued:state==='pending'?null:0,drain_pending:state==='draining'}}));return true;
 }},async({cdp,evaluate,until,url})=>{
  const group=x=>receipt.groups.push(x);
  const text=()=>evaluate("document.querySelector('[data-code-index-settings-status]').textContent");
  const click=async selector=>{const p=await evaluate(`(()=>{const b=document.querySelector(${JSON.stringify(selector)});b.scrollIntoView({block:'nearest'});const r=b.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);if(b.disabled||!(h===b||b.contains(h)))throw Error('Owned card control unavailable');return{x,y};})()`);for(const type of ['mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,button:'left',clickCount:1,...p});};
  const screenshot=async name=>{assert.equal(await evaluate('location.origin'),new URL(url).origin);const image=await cdp('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(out,name+'.png'),Buffer.from(image.data,'base64'),{mode:0o600});};
  await until("window.__ready && document.querySelector('[data-code-index-settings-status]').textContent.includes('Pending application')");
  // Existing helper opens the URL then explicitly navigates after CDP attach.
  // Record those startup document reads; subsequent assertions count actions.
  const startupReads=calls.length;receipt.startupDocumentReads=startupReads;assert(calls.every(c=>c.method==='GET'));
  assert.match(await text(),/Saved\/requested: 2.*Effective: unknown/);group('actual default-2 card reports pending and unknown native counts');await screenshot('pending');
  await click('[data-code-index-threads]');
  for(let i=0;i<2;i++){await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'ArrowUp',code:'ArrowUp',windowsVirtualKeyCode:38});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'ArrowUp',code:'ArrowUp',windowsVirtualKeyCode:38});}
  assert.equal(await evaluate("document.querySelector('[data-code-index-threads]').value"),'4','Physical numeric step keys must set the intended value before save');
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',text:'\r',windowsVirtualKeyCode:13});await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  await until("document.querySelector('[data-code-index-settings-status]').textContent.includes('Saved/requested: 4')");assert.equal(desired,4);group('physical keyboard Enter saves only desired integer');
  state='draining';await click('[data-code-index-refresh]');await until("document.querySelector('[data-code-index-settings-status]').textContent.includes('Draining active work')");assert.match(await text(),/Effective: 2; active: 2; queued: 0/);group('explicit refresh exposes draining without fabricated application');
  state='applied';await click('[data-code-index-refresh]');await until("document.querySelector('[data-code-index-settings-status]').textContent.includes('Applied')");assert.match(await text(),/Effective: 4; active: 0/);group('explicit applied readback');await screenshot('applied');
  state='error';await click('[data-code-index-refresh]');await until("document.querySelector('[data-code-index-settings-status]').textContent.includes('unavailable')");assert.doesNotMatch(await text(),/PRIVATE/);group('server error is fixed redacted text');
  state='denied';await click('[data-code-index-refresh]');await until("document.querySelector('[data-code-index-settings-status]').textContent.includes('administrator')");assert.equal(await evaluate("document.querySelector('[data-code-index-save]').disabled"),true);assert.equal(await evaluate("document.querySelector('[data-code-index-threads]').disabled"),true);group('admin denial disables mutation and preserves explicit refresh');await screenshot('denied');
  receipt.requests=calls;assert.deepEqual(await evaluate('__errors'),[]);assert.equal(calls.filter(c=>c.method==='POST').length,1);assert.equal(calls.length,startupReads+5);group('only five explicit post-start actions and no extra mutations');
 });
 receipt.browserHarnessFinallyCompleted=true;receipt.status='passed-component';receipt.sourceUnchanged=JSON.stringify(receipt.sourcePins)===JSON.stringify(pins());assert(receipt.sourceUnchanged);
} catch(error){receipt.status='failed';receipt.failures.push(String(error.message));process.exitCode=1;}
finally{fs.writeFileSync(path.join(out,'result.json'),JSON.stringify(receipt,null,2),{mode:0o600});console.log(JSON.stringify({out,status:receipt.status,groups:receipt.groups.length,failures:receipt.failures}));}
