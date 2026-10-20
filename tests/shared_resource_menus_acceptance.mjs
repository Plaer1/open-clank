// Actual Files/Copal/Editor consumers, synthetic HTTP boundaries only.
// Not normal-auth, native, packaged, GPU or performance qualification.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const output=fs.mkdtempSync(path.join(os.tmpdir(),'openclank-shared-menus-'));
const golden=JSON.parse(fs.readFileSync('mcp_servers/frankenmemory/crates/fm-core/tests/fixtures/code_map_published.json','utf8'));
const result={class:'synthetic-static-actual-consumer',groups:[],failures:[],output};
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><body class="theme-clanker-dark"><main id="app"></main>
<script type="module">
const nativeGolden=${JSON.stringify(golden)};
const fixture=window.fixture={generation:4,requests:[],saves:0,contextWrites:0};
const folder=()=>({id:'project-root',ref:'rr-project',provider:'host',kind:'folder',name:'Authorized project',capabilities:['stat','children'],policy_generation:fixture.generation});
const file=()=>({id:'caller-resource',ref:'rr-caller',provider:'host',kind:'file',name:'caller.py',mime_type:'text/x-python',capabilities:['stat','open','preview'],policy_generation:fixture.generation});
const workspace=()=>({id:'fixture-workspace',name:'Saved project',location_id:'fixture-location',relative_folder:'project',archived:false,generation:fixture.generation,revision:7});
const json=(v,status=200)=>new Response(JSON.stringify(v),{status,headers:{'Content-Type':'application/json'}});
class Events{addEventListener(){}close(){}}window.EventSource=Events;
window.fetch=async(input,init={})=>{
 const u=new URL(String(input),location.origin),body=init.body?JSON.parse(init.body):{},method=init.method||'GET';
 fixture.requests.push({path:u.pathname,method,body});
 if(u.pathname==='/api/auth/status')return json({ok:true,username:'synthetic-menu-owner',is_admin:true});
 if(u.pathname==='/api/copal/status')return json({storage_namespace:'synthetic-shared-menus',account_id:'fixture-account'});
 if(u.pathname==='/api/copal/documents')return json({docs:[]});
 if(u.pathname==='/api/copal/planning')return json({tracks:[],floatingTodos:[]});
 if(u.pathname==='/api/prefs/copal_entry_visibility')return json({value:{}});
 if(u.pathname==='/api/prefs/code_context_enabled'){if(method!=='GET')fixture.contextWrites++;return json({value:false});}
 if(u.pathname==='/api/files-v1/roots')return json({version:1,policy_generation:fixture.generation,entries:[folder()]});
 if(u.pathname==='/api/files-v1/places')return json({version:1,generation:fixture.generation,entries:[]});
 if(u.pathname==='/api/files-v1/workspaces')return json({generation:fixture.generation,entries:[{workspace:workspace(),resource:folder(),availability:'available'}]});
 if(u.pathname==='/api/files-v1/children')return json({generation:fixture.generation,entries:[file()],next_cursor:null});
 if(u.pathname==='/api/files-v1/stat')return json(body.resource_ref==='rr-caller'?file():folder());
 if(u.pathname==='/api/files-v1/reveal')return json({version:1,provider:'host',resource:file(),parent:folder(),ancestors:[folder()]});
 if(u.pathname==='/api/files-v1/code-graph-target'){
   fixture.generation++;return json({version:1,generation:fixture.generation,workspace:workspace(),open_relative:body.resource_ref==='rr-caller'?'caller.py':'',scope_kind:body.workspace_id?'captured_workspace':body.resource_ref==='rr-caller'?'parent_folder':'selected_folder'});
 }
 if(u.pathname==='/api/files-v1/workspace-resource')return json({version:1,generation:fixture.generation,workspace:workspace(),resource:body.relative_path?file():folder(),parent:folder()});
 if(u.pathname==='/api/files-v1/open-resource')return json({resource:file(),payload:{name:'caller.py',kind:'text',text:'def caller():\\n    return 1\\n',resource:{
   key:{accountId:'fixture-account',workspaceId:'host',provider:'host',resourceId:'caller-resource'},revision:{kind:'hostFingerprint',value:'saved-fp'},representation:'text',locator:{displayName:'caller.py',locationLabel:'caller.py',opaqueRef:'rr-caller'},capabilities:{read:true,edit:true},metadata:{encoding:'utf-8',newline:'\\n',language:'python',mode:'source'}}}});
 if(u.pathname==='/api/files-v1/save-resource'){fixture.saves++;return json({},500);}
 if(u.pathname.includes('/index/'))return json({index:{lifecycle:'ready',registration_id:'fixture-registration',admission_epoch:1,policy_generation:fixture.generation,head_generation:'fixture-head'}});
 if(u.pathname==='/api/memory/code/map'){
   const v=structuredClone(nativeGolden),r=body.visual_request.request;v.operation=r.operation;v.authority.receipt.policy_generation=fixture.generation;
   v.nodes=v.nodes.filter(n=>n.kind==='file').map(n=>({...n,parent:null}));v.edges=v.edges.filter(e=>e.relation!=='containment');v.page.returned_nodes=v.nodes.length;v.page.returned_edges=v.edges.length;
   if(r.operation!=='overview'){v.root=r.root.id;if(r.operation==='neighbors'){v.edges=v.edges.filter(e=>e.from===v.root||e.to===v.root);v.page.returned_edges=v.edges.length;}if(v.nodes.find(n=>n.id===v.root)?.kind==='file')v.selection={visual_id:v.root,resource_id:'file:abc',receipt:structuredClone(v.authority.receipt)};}
   for(let i=0;i<8;i++)v.cost.serialized_bytes=new TextEncoder().encode(JSON.stringify(v)).length;return json(v);
 }
 if(u.pathname==='/api/memory/code/resource')return json({schema:'open-clank.code-resource/v1',workspace_id:'fixture-workspace',capabilities:[{kind:'code',available:true}]});
 if(u.pathname==='/api/copal/events')return json({});
 return json({detail:{code:'fixture_unavailable',message:'Synthetic route unavailable.'}},404);
};
const copal=await import('/static/js/copal.js'),files=await import('/static/js/files.js'),context=await import('/static/js/custom-context-menu.js');
window.copalModule=copal.default;window.filesModule=files.default;context.initCustomContextMenu();
await copal.init(location.origin);await files.default.open();
fixture.files=files.getActiveBrowser();fixture.openEditor=()=>copal.openResource('rr-caller');
fixture.contextCommands=target=>{const request=context.captureContextCommandTarget(target);return request.adapter.commands(request).map(c=>({id:c.id,label:c.label,disabled:Boolean(c.disabled||c.disabledReason)}));};
window.ready=true;
</script>`;
try{
 await withCopalBrowser({page},async({cdp,evaluate,until,url})=>{
  const shot=async name=>{assert.equal(await evaluate('location.origin'),new URL(url).origin);fs.writeFileSync(path.join(output,name+'.png'),Buffer.from((await cdp('Page.captureScreenshot',{format:'png'})).data,'base64'));};
  const pointer=async(selector,{right=false}={})=>{
   result.phase=selector;
   await evaluate('new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))');
   const p=await evaluate(`(()=>{const all=[...document.querySelectorAll(${JSON.stringify(selector)})].filter(n=>n.getClientRects().length&&!n.disabled);if(all.length!==1)throw Error('Expected one owned control');const e=all[0];e.scrollIntoView({block:'nearest'});const r=e.getBoundingClientRect(),x=r.left+r.width/2,y=r.top+r.height/2,h=document.elementFromPoint(x,y);window.lastPointer={x,y,width:r.width,height:r.height,hit:h?.tagName||null,hitClass:String(h?.className||'').slice(0,120),menuPresent:!!document.querySelector('.openclank-context-menu')};if(h!==e&&!e.contains(h))throw Error('Owned control occluded');return{x,y};})()`);
   for(const type of ['mousePressed','mouseReleased'])await cdp('Input.dispatchMouseEvent',{type,...p,button:right?'right':'left',clickCount:1});
  };
  try{
   await until('window.ready');
   await cdp('Emulation.setDeviceMetricsOverride',{width:1280,height:800,deviceScaleFactor:1,mobile:false});
   await until("!document.querySelector('.files-window')?.getAnimations({subtree:true}).some(a=>a.playState==='running')");
   await until("!!document.querySelector('[data-explorer-action=\"workspace-actions\"]')");
   await pointer('[data-explorer-action="workspace-actions"]');
   await until("!!document.querySelector('.files-tree-actions-menu button')");
   const ellipsis=await evaluate("[...document.querySelectorAll('.files-tree-actions-menu button')].map(n=>n.textContent)");
   for(const label of ['Use as workspace','Rename workspace','Archive workspace','Open in Code Graph'])assert(ellipsis.includes(label));
   const same=await evaluate("fixture.contextCommands(document.querySelector('[data-explorer-action=\"workspace-actions\"]')).map(c=>c.label)");
   for(const label of ellipsis)assert(same.includes(label));result.groups.push('Workspace ellipsis and context share captured descriptors');
   await shot('workspace-ellipsis');
   await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape'});
   await evaluate("fixture.files.openDirectory({id:'project-root',ref:'rr-project',provider:'host',kind:'folder',name:'Authorized project',capabilities:['children','stat']})");
   await until("!!document.querySelector('.files-entry.file')");
   const itemCommands=await evaluate("fixture.contextCommands(document.querySelector('.files-entry.file'))");
   assert(itemCommands.some(c=>c.id==='files.code-graph'&&!c.disabled));assert(!itemCommands.some(c=>c.label==='Archive workspace'));
   await pointer('.files-entry.file',{right:true});await until("!!document.querySelector('.openclank-context-menu')");
   await pointer('.openclank-context-menu details:has([data-command="files.code-graph"]) > summary');
   await shot('file-context');
   await pointer('.openclank-context-menu [data-command="files.code-graph"]');
   await until("document.querySelector('#styled-confirm-ok')?.getClientRects().length");
   assert.match(await evaluate("document.querySelector('#styled-confirm-msg').textContent"),/Authorized project/);
   await pointer('#styled-confirm-ok');
   await until("document.querySelector('.code-map-workbench [data-code-workspace]')?.textContent==='Saved project'");
   await until("document.querySelector('.code-map-inspector')?.textContent.includes('caller.py')");
   assert.equal(await evaluate('fixture.generation'),5);result.groups.push('Real Files command reconciles own generation increase and selects complete saved path');await shot('files-to-graph');
   await evaluate('fixture.openEditor()');await until("(()=>{const e=document.querySelector('.copal-notes-window:not(.hidden) .cm-content');return !!e&&getComputedStyle(e).visibility==='visible'&&e.getAttribute('contenteditable')==='true'&&getComputedStyle(e.closest('.cm-editor')).visibility==='visible';})()");
   await pointer('.copal-notes-window:not(.hidden) .cm-content');await cdp('Input.insertText',{text:'# UNSAVED MENU FIXTURE\n'});
   await pointer('.copal-notes-window:not(.hidden) .cm-content',{right:true});await until("!!document.querySelector('.openclank-context-menu [data-command=\"editor.code-graph\"]')");
   await pointer('.openclank-context-menu details:has([data-command="editor.code-graph"]) > summary');
   const labels=await evaluate("[...document.querySelectorAll('.openclank-context-menu button')].map(n=>n.textContent)");for(const label of ['Undo','Redo','Copy','Paste','Open in Code Graph'])assert(labels.includes(label));
   await shot('dirty-editor-context');
   await pointer('.openclank-context-menu [data-command="editor.code-graph"]');
   await until("document.querySelector('#styled-confirm-ok')?.getClientRects().length");
   assert.match(await evaluate("document.querySelector('#styled-confirm-msg').textContent"),/unsaved changes will not be saved or indexed/);
   await pointer('#styled-confirm-ok');await until('fixture.generation===6');
   assert.equal(await evaluate('fixture.saves'),0);assert.equal(await evaluate('fixture.contextWrites'),0);
   assert(await evaluate("[...document.querySelectorAll('.cm-content')].some(n=>n.textContent.includes('UNSAVED MENU FIXTURE'))"));
   assert(!(await evaluate('fixture.requests')).some(r=>r.path==='/api/files-v1/workspace'||r.path.endsWith('/workspaces/from-path')));
   await until("[...(window.__openClankCodeMapScenes?.values()||[])].some(s=>{const v=s.snapshot();return v.status==='ready'&&v.drawn.nodes===2&&v.drawn.edges===1&&v.drawn.labels===2;})");
   result.scene=await evaluate("[...window.__openClankCodeMapScenes.values()].map(s=>s.snapshot()).map(({status,drawn,backend,camera})=>({status,drawn,backend,camera}))");
   result.groups.push('Dirty Editor context retains native editing commands and saved source only; no Save/adoption/chat context write');await shot('dirty-editor-graph');
   await evaluate('fixture.openEditor()');await until("(()=>{const e=document.querySelector('.copal-notes-window:not(.hidden) .cm-content');return !!e&&getComputedStyle(e).visibility==='visible';})()");
   await pointer('.copal-notes-window:not(.hidden) .cm-content');
   for(const type of ['keyDown','keyUp'])await cdp('Input.dispatchKeyEvent',{type,key:'z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:4});
   await until("![...document.querySelectorAll('.cm-content')].some(n=>n.textContent.includes('UNSAVED MENU FIXTURE'))");
   for(const type of ['keyDown','keyUp'])await cdp('Input.dispatchKeyEvent',{type,key:'Z',code:'KeyZ',windowsVirtualKeyCode:90,modifiers:12});
   await until("[...document.querySelectorAll('.cm-content')].some(n=>n.textContent.includes('UNSAVED MENU FIXTURE'))");
   assert.equal(await evaluate('fixture.saves'),0);result.groups.push('Return to original dirty Editor retains real keyboard undo/redo without Save');await shot('dirty-editor-return');
   result.status='passed-component';
  }catch(error){result.pointer=await evaluate(`(()=>{const menu=document.querySelector('.openclank-context-menu'),owner=document.querySelector('.copal-notes-window:not(.hidden)');const style=n=>n?{z:getComputedStyle(n).zIndex,display:getComputedStyle(n).display,visibility:getComputedStyle(n).visibility,pointer:getComputedStyle(n).pointerEvents,opacity:getComputedStyle(n).opacity}:null;return{...window.lastPointer,menu:style(menu),owner:style(owner)};})()`).catch(()=>null);await shot('failure-ui').catch(()=>{});throw error;}
 });
}catch(error){result.status='failed';result.failures.push(error.message);process.exitCode=1;}
finally{fs.writeFileSync(path.join(output,'result.json'),JSON.stringify(result,null,2));console.log(JSON.stringify(result));}
