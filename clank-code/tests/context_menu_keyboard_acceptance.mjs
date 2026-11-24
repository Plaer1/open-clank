// Real shared menu + trusted CDP keyboard input; synthetic owners only.
// No normal-auth/native/package/performance qualification or live app requests.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const output=fs.mkdtempSync(path.join(os.tmpdir(),'openclank-context-keyboard-'));
const result={schema:'openclank.context-keyboard-component/v1',classification:'synthetic-static shared-menu keyboard component; GPU disabled',groups:[],failures:[],output,sourceSha256:createHash('sha256').update(fs.readFileSync('static/js/custom-context-menu.js')).digest('hex'),screenshots:{},browserClosed:false};
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><body class="theme-clanker-dark" style="padding:40px"><h1>Keyboard context fixture</h1><div id="a" tabindex="0" style="padding:30px;border:1px solid">Owner A</div><div id="b" tabindex="0" style="padding:30px;border:1px solid">Owner B</div><textarea id="text">alpha beta</textarea><div id="unknown" tabindex="0">Unsupported surface</div><button id="actions">Actions</button><script type="module">
window.fixture={epoch:1,live:true,executed:[],keys:[],block:false,captures:[],otherActive:false};
document.addEventListener('keydown',e=>{if(fixture.block)e.preventDefault();if(fixture.keys.length<64)fixture.keys.push({key:e.key,trusted:e.isTrusted,get prevented(){return e.defaultPrevented;},composing:e.isComposing});},true);
const menu=await import('/static/js/custom-context-menu.js');
for(const id of ['a','b']){const el=document.getElementById(id);const dispose=menu.registerAdapter(el,{ownsObjectCommands:true,capture(target){const c={owner:id,epoch:fixture.epoch};fixture.captures.push({owner:id});return c;},isCurrent:r=>fixture.live&&r.adapterContext.epoch===fixture.epoch,commands:()=>[{id:'fixture.saved-source',label:'Inspect saved source',disabled:false}],execute(command,r){if(!fixture.live||r.adapterContext.epoch!==fixture.epoch)throw Error('Captured owner changed');fixture.executed.push({command,owner:r.adapterContext.owner});return true;}});fixture['dispose'+id]=dispose;}
const actions=document.getElementById('actions');
actions.addEventListener('click', event => menu.openContextActionMenu({anchor:actions,event,isCurrent:()=>fixture.live,items:[
  {id:'fixture.root',label:'Root action',run:()=>fixture.executed.push({command:'root'})},
  {id:'fixture.child',label:'Child action',menu:'More actions',run:()=>fixture.executed.push({command:'child'})},
]}));
menu.initCustomContextMenu();fixture.menu=menu;window.ready=true;
</script>`;
try{
 await withCopalBrowser({page},async({cdp,evaluate,until,url})=>{
  const shot=async name=>{assert.equal(await evaluate('location.origin'),new URL(url).origin);const p=path.join(output,name+'.png');fs.writeFileSync(p,Buffer.from((await cdp('Page.captureScreenshot',{format:'png'})).data,'base64'));result.screenshots[name]=p;};
  const focus=id=>evaluate(`document.getElementById(${JSON.stringify(id)}).focus()`);
  const key=async(key,code=key,modifiers=0)=>{const values={F10:121,ContextMenu:93,Escape:27,End:35,ArrowLeft:37,ArrowRight:39};for(const type of ['keyDown','keyUp'])await cdp('Input.dispatchKeyEvent',{type,key,code,modifiers,windowsVirtualKeyCode:values[key]||0});};
  const shift=()=>key('F10','F10',8),escape=()=>key('Escape');
  const menu=()=>evaluate('!!document.querySelector("#openclank-context-menu")');
  const settled=()=>evaluate('new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))');
  const clickCommand=async()=>{const p=await evaluate(`(()=>{const e=document.querySelector('[data-command="fixture.saved-source"]'),r=e.getBoundingClientRect(),x=r.x+r.width/2,y=r.y+r.height/2,h=document.elementFromPoint(x,y);if(!r.width||!r.height||!(h===e||e.contains(h)))throw Error('Owned command occluded');return{x,y};})()`);for(const type of ['mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,button:'left',clickCount:1,...p});};
  try{
   await until('window.ready');await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:800,deviceScaleFactor:1,mobile:false});await evaluate('document.fonts.ready.then(()=>true)');await settled();
   result.phase='trusted-Shift-F10';await focus('a');await shift();await until('!!document.querySelector("#openclank-context-menu")');assert(await evaluate('fixture.keys.some(k=>k.key==="F10"&&k.trusted&&k.prevented)'));assert.equal(await evaluate('fixture.captures.at(-1).owner'),'a');await shot('shift-f10-owner-a');await escape();assert.equal(await menu(),false);assert.equal(await evaluate('document.activeElement.id'),'a');result.groups.push('trusted Shift-F10 opens captured owner; Escape restores focus');
   result.phase='trusted-ContextMenu';await focus('b');await key('ContextMenu');await until('!!document.querySelector("#openclank-context-menu")');assert(await evaluate('fixture.keys.some(k=>k.key==="ContextMenu"&&k.trusted&&k.prevented)'));await shot('context-key-owner-b');await escape();assert.equal(await evaluate('document.activeElement.id'),'b');result.groups.push('trusted ContextMenu key uses focused owner B');
   result.phase='captured-owner-versus-active';await focus('a');await shift();await until('!!document.querySelector("#openclank-context-menu")');await evaluate('fixture.otherActive=true;document.getElementById("b").focus()');await clickCommand();await until('fixture.executed.length===1');assert.equal(await evaluate('fixture.executed[0].owner'),'a');result.groups.push('another active owner does not retarget original command');
   result.phase='stale-owner';await focus('a');await shift();await until('!!document.querySelector("#openclank-context-menu")');await evaluate('fixture.epoch++');await clickCommand();await settled();assert.equal(await evaluate('fixture.executed.length'),1);await evaluate('fixture.live=false');await focus('a');await shift();await settled();assert.equal(await menu(),false);await evaluate('fixture.live=true');result.groups.push('stale captured epoch and denied owner cannot execute or open');
   result.phase='off-and-unsupported';await evaluate("localStorage.setItem('openclank-custom-context-menu','off')");await focus('a');await shift();await settled();assert.equal(await menu(),false);assert.equal(await evaluate('fixture.keys.at(-1).prevented'),false);await escape();await evaluate("localStorage.removeItem('openclank-custom-context-menu')");await focus('unknown');await shift();await settled();assert.equal(await menu(),false);assert.equal(await evaluate('fixture.keys.at(-1).prevented'),false);result.groups.push('disabled custom menu and unknown surface retain native/default handling');
   result.phase='modifiers-and-prevented';await focus('a');for(const mods of [0,9,10,12]){await key('F10','F10',mods);await settled();assert.equal(await menu(),false);}await evaluate('fixture.block=true');await shift();await settled();assert.equal(await menu(),false);await evaluate('fixture.block=false');await evaluate("document.getElementById('a').dispatchEvent(new KeyboardEvent('keydown',{key:'F10',shiftKey:true,isComposing:true,bubbles:true,cancelable:true}))");await settled();assert.equal(await menu(),false);result.groups.push('extra modifiers/defaultPrevented/IME cannot invoke menu; synthetic events used only for IME negative');
   result.phase='text-selection';await evaluate("const t=document.getElementById('text');t.focus();t.setSelectionRange(6,10)");await shift();await until('!!document.querySelector("#openclank-context-menu")');await escape();assert.deepEqual(await evaluate("[document.activeElement.id,document.getElementById('text').selectionStart,document.getElementById('text').selectionEnd]"),['text',6,10]);await focus('a');await evaluate('fixture.disposea()');await shift();await settled();assert.equal(await menu(),false);result.groups.push('text selection preserved and unregistered owner no longer invokes commands');
   result.phase='anchored-submenu';
   await evaluate("localStorage.setItem('openclank-custom-context-menu','off'); document.getElementById('actions').click()");
   await until('!!document.querySelector("#openclank-context-menu")');
   const rootWidth = await evaluate('document.getElementById("openclank-context-menu").getBoundingClientRect().width');
   await key('End');await key('ArrowRight');await until('!!document.querySelector("#openclank-context-menu-child")');
   const [command, expanded, widthWithChild] = await evaluate('[document.activeElement.dataset.command,document.querySelector("#openclank-context-menu [aria-haspopup]").getAttribute("aria-expanded"),document.getElementById("openclank-context-menu").getBoundingClientRect().width]');
   assert.deepEqual([command, expanded], ['fixture.child','true']);
   assert(Math.abs(widthWithChild - rootWidth) < 1.5, 'root width stays fixed apart from layout rounding');
   await escape();
   assert(await menu());assert.equal(await evaluate('document.querySelector("#openclank-context-menu-child")===null && document.activeElement.hasAttribute("aria-haspopup")'),true);
   await key('ArrowRight');await until('!!document.querySelector("#openclank-context-menu-child")');
   await key('ArrowLeft');assert.equal(await evaluate('document.querySelector("#openclank-context-menu-child")===null'),true);
   await escape();assert.equal(await menu(),false);assert.equal(await evaluate('document.activeElement.id'),'actions');
   result.groups.push('anchored menu works with secondary menu off; child width leaves root intact; Escape and Left restore each level');
   result.phase='narrow-drill-down';
   await cdp('Emulation.setDeviceMetricsOverride',{width:210,height:420,deviceScaleFactor:1,mobile:false});
   await settled();
   await evaluate("document.getElementById('actions').click()");
   await key('End');await key('ArrowRight');
   await until('!!document.querySelector("#openclank-context-menu-child")');
   assert.equal(await evaluate('!document.querySelector("#openclank-context-menu-child .openclank-context-back").hidden && getComputedStyle(document.getElementById("openclank-context-menu")).visibility==="hidden"'),true);
   await evaluate('document.querySelector("#openclank-context-menu-child .openclank-context-back").click()');
   assert.equal(await evaluate('!document.querySelector("#openclank-context-menu-child") && document.activeElement.hasAttribute("aria-haspopup")'),true);
   await escape();
   result.groups.push('narrow window uses a drill-down panel and Back restores the parent item');
   result.status='passed-component';
  }catch(error){result.status='failed';result.failures.push({phase:result.phase,name:error.name,message:error.message});result.keyObservations=await evaluate('fixture.keys').catch(()=>[]);await shot('failure').catch(()=>{});throw error;}
 });
}catch(error){result.status='failed';if(!result.failures.length)result.failures.push({phase:result.phase||'fixture-start',name:error.name,message:error.message});process.exitCode=1;}
finally{result.browserClosed=true;fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2)+'\n');console.log(JSON.stringify({output,status:result.status,groups:result.groups,failures:result.failures,browserClosed:result.browserClosed}));}
