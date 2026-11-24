#!/usr/bin/env node
import assert from 'node:assert/strict';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

const report = {
  resource_id:'report-id', resource_ref:'ref-report', ref:'ref-report', provider:'host', kind:'file',
  name:'report.html', capabilities:['preview','stat'], mime_type:'text/html',
  revision:{kind:'hostFingerprint',value:'report-r1'}, size:100,
};
const styleFolder = {
  resource_id:'styles-id', resource_ref:'ref-styles', ref:'ref-styles', provider:'host', kind:'directory',
  name:'styles', capabilities:['children','stat'],
};
const style = {
  resource_id:'style-id', resource_ref:'ref-main-css', ref:'ref-main-css', provider:'host', kind:'file',
  name:'main.css', capabilities:['preview','stat'], mime_type:'text/css',
  revision:{kind:'hostFingerprint',value:'css-r1'}, size:50,
};
const logo = {
  resource_id:'logo-id', resource_ref:'ref-logo', ref:'ref-logo', provider:'host', kind:'file',
  name:'logo.png', capabilities:['preview','stat'], mime_type:'image/png',
  revision:{kind:'hostFingerprint',value:'logo-r1'}, size:24,
};
let createBody = null;
const page = `<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css">
  <script type="module">
    import { createFilesBrowser } from '/static/js/files.js';
    import browserApplet from '/static/js/browserApplet.js';
    window.browserApplet = browserApplet;
    window.fixtureFiles = createFilesBrowser({id:'wb06e-files'});
    window.readyFiles = window.fixtureFiles.open().then(async()=>{
      await window.fixtureFiles.openDirectory({resource_id:'home-id',resource_ref:'ref-home',ref:'ref-home',provider:'host',kind:'folder',name:'Home',capabilities:['children','stat']});
      const row=[...document.querySelectorAll('.files-entry')].find(item=>item.textContent.includes('report.html'));
      row?.dispatchEvent(new MouseEvent('click',{bubbles:true,detail:1}));
      const snapshot=window.fixtureFiles.captureCommands();
      const action=window.fixtureFiles.commandDescriptors(snapshot).find(item=>item.id==='files.open-browser');
      window.commandResult=action?.run();
      await window.commandResult;
      window.commandDone=true;
      return Boolean(action);
    });
  </script>`;

await withCopalBrowser({ page, request: async (request, response) => {
  const url = new URL(request.url, 'http://fixture');
  const json = value => { response.setHeader('content-type','application/json'); response.end(JSON.stringify(value)); return true; };
  if (url.pathname === '/api/auth/status') return json({ok:true,username:'wb06e-owner'});
  if (url.pathname === '/api/files-v1/roots') return json({version:1,policy_generation:3,entries:[{
    resource_id:'root-id',resource_ref:'ref-root',ref:'ref-root',provider:'host',kind:'provider_root',name:'Host locations',
    capabilities:['children','stat'],sort_keys:['name'],
  }],import_capabilities:{}});
  if (url.pathname === '/api/files-v1/places' || url.pathname === '/api/files-v1/workspaces') return json({version:1,entries:[]});
  if (url.pathname === '/api/files-v1/children' && request.method === 'POST') {
    let raw=''; for await (const chunk of request) raw += chunk;
    const body=JSON.parse(raw);
    const entries = body.parent_ref === 'ref-root' ? [{resource_id:'home-id',resource_ref:'ref-home',ref:'ref-home',provider:'host',kind:'folder',name:'Home',capabilities:['children','stat']}]
      : body.parent_ref === 'ref-home' ? [report,styleFolder,logo]
        : body.parent_ref === 'ref-styles' ? [style] : [];
    response.setHeader('content-type','application/json'); response.end(JSON.stringify({version:1,entries,next_cursor:null,total:entries.length})); return true;
  }
  if (url.pathname === '/api/files-v1/content/ref-report') { response.setHeader('content-type','text/html; charset=utf-8'); response.end('<link rel="stylesheet" href="styles/main.css"><img src="logo.png">'); return true; }
  if (url.pathname === '/api/files-v1/content/ref-main-css') { response.setHeader('content-type','text/css; charset=utf-8'); response.end('.hero { color: navy }'); return true; }
  if (url.pathname === '/api/files-v1/stat' && request.method === 'POST') {
    let raw=''; for await (const chunk of request) raw += chunk;
    const ref=JSON.parse(raw).resource_ref;
    const resource=ref==='ref-main-css'?style:ref==='ref-logo'?logo:report;
    return json({version:1,resource});
  }
  if (url.pathname === '/api/browser-previews' && request.method === 'POST') {
    let raw=''; for await (const chunk of request) raw += chunk;
    createBody=JSON.parse(raw);
    response.writeHead(503,{'content-type':'application/json'});
    response.end(JSON.stringify({detail:{code:'provider_unavailable',message:'fixture stops after verifying command payload'}})); return true;
  }
  if (url.pathname.startsWith('/api/')) return json({version:1,entries:[],next_cursor:null});
  return false;
} }, async ({ until }) => {
  await until('Boolean(window.readyFiles)');
  await until('window.readyFiles');
  await until('window.commandDone === true');
  for (let attempt=0; !createBody && attempt<100; attempt++) await new Promise(resolve=>setTimeout(resolve,25));
  assert.ok(createBody, 'the production Files command posted a preview create request');
  assert.equal(createBody.resource_ref,'ref-report');
  assert.deepEqual(createBody.dependencies.map(item=>[item.path,item.resource_ref]),[
    ['styles/main.css','ref-main-css'],['logo.png','ref-logo'],
  ]);
});
