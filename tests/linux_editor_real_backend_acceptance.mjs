import assert from 'node:assert/strict';
import fs from 'node:fs';
import {withCopalBrowser} from './helpers/copal_browser_fixture.mjs';
const backend=new URL(process.env.LINUX_BACKEND_URL);
assert.equal(backend.hostname,'127.0.0.1');
const headers={'x-linux-fixture':process.env.LINUX_FIXTURE_TOKEN,'content-type':'application/json'};
const info=await(await fetch(new URL('/__fixture/info',backend),{headers})).json();
const production=fs.readFileSync('static/js/copal.js','utf8');
const source=production.replace('export default { init,',`window.__linuxEditor={buffer:()=>notesFeature?.getAuthoritativeSnapshot(window.__linuxDoc||state.selected)||state.docs.find(d=>d.id===(window.__linuxDoc||state.selected))?.resourceSnapshot,draft:()=>notesFeature?.getDraftSnapshot(window.__linuxDoc||state.selected),save:()=>notesFeature.flushDocument(window.__linuxDoc||state.selected,{returnReceipt:true})};\nexport default { init,`);
assert.notEqual(source,production);
const page=`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><main id="app"></main><script type="module">
class QuietEvents{addEventListener(){}close(){}}window.EventSource=QuietEvents;
const original=fetch;window.fetch=async(input,init={})=>{const u=new URL(String(input),location.origin),json=v=>new Response(JSON.stringify(v),{headers:{'content-type':'application/json'}});
if(u.pathname==='/api/copal/status')return json({storage_namespace:'linux-isolated',account_id:'account-alice'});
if(u.pathname==='/api/copal/documents')return json({docs:[]});
if(u.pathname==='/api/copal/planning')return json({tracks:[],floatingTodos:[]});
if(u.pathname.startsWith('/api/prefs/'))return json({value:{}});
if(u.pathname==='/api/auth/status')return json({ok:true,username:'alice',is_admin:true});
return original(input,init);};
window.__run=async()=>{const m=await import('/static/js/copal.js');window.copalModule=m.default;await m.init(location.origin);window.__linuxDoc=await m.default.openResource(${JSON.stringify(info.resource_ref)});};</script>`;
const receipts=[];
await withCopalBrowser({page,overrides:{'/static/js/copal.js':source},request:async(req,res)=>{
 if(!req.url.startsWith('/api/')&&!req.url.startsWith('/__fixture/'))return false;
 const body=Buffer.concat(await Array.fromAsync(req));
 const controller=new AbortController();res.on('close',()=>controller.abort());
 const response=await fetch(new URL(req.url,backend),{method:req.method,headers,body:body.length?body:undefined,signal:controller.signal});
 if(response.headers.get('content-type')?.startsWith('text/event-stream')){
  res.writeHead(response.status,{'content-type':'text/event-stream'});
  try{for await(const chunk of response.body)res.write(Buffer.from(chunk));}catch(error){if(error.name!=='AbortError')throw error;}
  res.end();return true;
 }
 const bytes=Buffer.from(await response.arrayBuffer());
 if(req.url==='/api/files-v1/save-resource')receipts.push(JSON.parse(bytes));
 res.writeHead(response.status,{'content-type':response.headers.get('content-type')||'application/json'});res.end(bytes);return true;
}},async({evaluate,cdp})=>{
 const until=async expression=>{const deadline=Date.now()+20000;do{if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,25));}while(Date.now()<deadline);throw new Error('Editor timeout: '+expression);};
 await until('window.__run');await evaluate('window.__run()');
 await until(`document.querySelector('.copal-notes-window:not(.hidden) .cm-content')?.textContent.length>0`);
 const edit=async text=>{
  await evaluate(`document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()`);
  await cdp('Input.dispatchKeyEvent',{type:'keyDown',key:'a',code:'KeyA',modifiers:2,windowsVirtualKeyCode:65});
  await cdp('Input.dispatchKeyEvent',{type:'keyUp',key:'a',code:'KeyA',modifiers:2,windowsVirtualKeyCode:65});
  await cdp('Input.insertText',{text});
  await until(`window.__linuxEditor.draft()?.envelope?.text===${JSON.stringify(text)}`);
 };
 assert.equal((await evaluate('window.__linuxEditor.buffer()')).envelope.text,info.original);
 const changed='browser changed ü\nsecond line\n';
 await edit(changed);
 const saved=await evaluate('window.__linuxEditor.save()');assert.equal(saved.outcome,'applied',JSON.stringify(saved));
 const disk=await(await fetch(new URL('/__fixture/disk',backend),{headers})).json();assert.equal(disk.text,changed);assert.equal(disk.mode,0o640);
 await evaluate(`window.copalModule.openResource(${JSON.stringify(info.resource_ref)})`);
 assert.equal((await evaluate('window.__linuxEditor.buffer()')).envelope.text,changed);
 await fetch(new URL('/__fixture/external',backend),{method:'POST',headers});
 const unsaved='user unsaved local text\n';await edit(unsaved);
 const conflict=await evaluate('window.__linuxEditor.save()');assert.equal(conflict.outcome,'conflict',JSON.stringify(conflict));
 const external=await(await fetch(new URL('/__fixture/disk',backend),{headers})).json();assert.equal(external.text,'external edit\n');
 assert.equal((await evaluate('window.__linuxEditor.draft()')).envelope.text,unsaved);
 assert(receipts.some(r=>r.outcome==='applied'));assert(receipts.some(r=>r.outcome==='conflict'));
 if(process.env.LINUX_SCREENSHOT_DIR){
  fs.mkdirSync(process.env.LINUX_SCREENSHOT_DIR,{recursive:true});
  const image=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  fs.writeFileSync(process.env.LINUX_SCREENSHOT_DIR+'/editor-conflict.png',Buffer.from(image.data,'base64'));
 }
 await evaluate(`(async()=>{const files=await import('/static/js/files.js');await files.default.open();await files.getActiveBrowser().openDirectory(${JSON.stringify(info.folder)});})()`);
 await until(`([...document.querySelectorAll('img.files-thumbnail')].some(img=>img.complete&&img.naturalWidth>0))`);
 if(process.env.LINUX_SCREENSHOT_DIR){
  const image=await cdp('Page.captureScreenshot',{format:'png',captureBeyondViewport:true});
  fs.writeFileSync(process.env.LINUX_SCREENSHOT_DIR+'/files-kde-thumbnail.png',Buffer.from(image.data,'base64'));
 }
 await evaluate(`import('/static/js/files.js').then(files=>files.getActiveBrowser().dispose())`);
 console.log('Linux real Editor: actual CodeMirror input, Files HTTP/native save, reopen, disk bytes/mode and external-edit CAS conflict passed');
});
