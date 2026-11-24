#!/usr/bin/env node
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// Keep the production DOM but omit app.js bootstrap. The fixture imports and
// initializes the real chat/session modules explicitly with fake HTTP only.
const page = fs.readFileSync('static/index.html', 'utf8').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '');
await withCopalBrowser({ page, disableGpu: true }, async ({ evaluate, until }) => {
  await evaluate(String.raw`(async () => {
    document.body.dataset.accountId='';
    window.__calls = [];
    window.__sessionRows = [];
    window.__receiptRows = [];
    window.__dropReceiptOnce = false;
    window.__options = null;
    window.__hold = false;
    window.__holdOptionsPatch = false;
    window.__optionsPatchRelease = null;
    window.__streamController = null;
    window.__uploadStarted = null;
    window.__uploadDidStart = false;
    window.__uploadRelease = null;
    window.__paths=[];
    const json = (value, status=200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
    window.fetch = async (input, init={}) => {
      const url = new URL(String(input), location.origin);
      const path = url.pathname;
      if (path.includes('model-options')) window.__paths.push(path);
      if (path === '/api/models') return json({items:[]});
      if (path === '/api/sessions') return json(window.__sessionRows);
      if (path === '/api/default-chat') return json({});
      if (path === '/api/session' && init.method === 'POST') {
        const data = Object.fromEntries(init.body.entries());
        const row = {id:'chat-one', name:'Test', owner:'fixture', endpoint_id:data.endpoint_id, endpoint_url:data.endpoint_url, model:data.model, provider_model_route_id:data.provider_model_route_id || 'route-one', message_count:0};
        window.__sessionRows = [row]; return json(row);
      }
      if (path === '/api/session/chat-one/model-options' && (!init.method || init.method === 'GET')) { window.__calls.push({kind:'options-get'}); return json({route_id:'public-route-one',catalog_revision:7,descriptors:[{id:'reasoning_effort',type:'enum',label:'Thinking',values:['low','high','none'],catalog_revision:7}],selection:window.__options}); }
      if (path === '/api/session/chat-one/model-options' && init.method === 'PATCH') {
        if (window.__holdOptionsPatch) await new Promise(resolve=>window.__optionsPatchRelease=resolve);
        window.__options = JSON.parse(init.body).selection; window.__calls.push({kind:'options',selection:window.__options});
        return json({route_id:'public-route-one',catalog_revision:7,descriptors:[{id:'reasoning_effort',type:'enum',label:'Thinking',values:['low','high','none'],catalog_revision:7}],selection:window.__options});
      }
      if (path === '/api/chat_stream') {
        const fields = Object.fromEntries(init.body.entries()); window.__calls.push({kind:'stream',fields});
        const acceptedOptions = fields.model_options ? JSON.stringify(JSON.parse(fields.model_options)) : JSON.stringify({route_id:'public-route-one',catalog_revision:7,options:{}});
        const headers={'Content-Type':'text/event-stream','X-Model-Request-Options':acceptedOptions};
        if (window.__hold) return new Response(new ReadableStream({start(controller){ window.__streamController=controller; controller.enqueue(new TextEncoder().encode('data: {"delta":"started"}\n\n')); }}), {headers});
        return new Response('data: {"delta":"reply"}\n\ndata: [DONE]\n\n', {headers});
      }
      if (path === '/api/upload' && init.method === 'POST') {
        const form=Object.fromEntries(init.body.entries()); const files=init.body.getAll('files').map(file=>file.name); window.__calls.push({kind:'upload',session:form.session_id,files});
        if (window.__uploadWait) { window.__uploadDidStart=true; await new Promise(resolve=>window.__uploadRelease=resolve); }
        return json({files:[{id:'upload-captured',name:'memo.txt',mime:'text/plain',size:5}]});
      }
      if (path === '/api/orchestration/chat-one') return json({mapRevision:3});
      if (path === '/api/orchestration/chat-one/actors/main/input' && init.method === 'GET') return json({receipts:window.__receiptRows});
      if (path === '/api/orchestration/chat-one/actors/main/input' && init.method === 'POST') {
        const receipt=JSON.parse(init.body); window.__calls.push({kind:'receipt',receipt});
        window.__receiptRows=[{requestID:receipt.requestID,text:receipt.text,attachmentUploadIDs:receipt.uploadIDs,state:'persisted',mode:receipt.mode,createdAt:Date.now(),modelRequestOptions:receipt.modelOptions}];
        if(window.__dropReceiptOnce){window.__dropReceiptOnce=false;throw new Error('Synthetic unknown receipt outcome')}
        return json({receipt:{...receipt,state:'persisted'},deliveryState:receipt.mode==='after-turn'?'pending-boundary':'active-steer'});
      }
      if (path.includes('/history')) return json([]);
      if (path.includes('/stream_status')) return json({status:'idle'});
      if (path.endsWith('/mimo-state')) return json({});
      return json({});
    };
    const sessions=(await import('/static/js/sessions.js')).default;
    const chat=(await import('/static/js/chat.js')).default;
    const files=(await import('/static/js/fileHandler.js')).default;
    window.__sessions=sessions; window.__chat=chat; window.chatModule=chat; window.__files=files;
    files.init(location.origin);
    // Auth resolves before delayed chat.init registers its listener.
    document.body.dataset.accountId='fixture';
    chat.init(location.origin);
    document.getElementById('chat-form').addEventListener('submit', event => {
      event.preventDefault(); void chat.handleChatSubmit(event);
    });
    sessions.createDirectChat('https://engine.invalid/chat','model-one','connection-one');
    window.__send=async text=>{const input=document.getElementById('message');input.value=text;input.dispatchEvent(new Event('input',{bubbles:true}));return chat.handleChatSubmit({preventDefault(){}})};
  })()`);

  await evaluate(`(async()=>{ await __send('first'); __chat.init(location.origin); document.dispatchEvent(new CustomEvent('odysseus:session-selected',{detail:{sessionId:'chat-one'}})); })()`);
  await until(`!__chat.hasActiveStream(__sessions.getCurrentSessionId())`, 'first turn settlement');
  await until(`!!document.querySelector('.chat-thinking-control:not([hidden])')`, 'Thinking selector mount');
  assert.deepEqual(await evaluate(`JSON.parse([...document.querySelectorAll('#chat-history .msg-user')].find(node=>node.dataset.raw==='first').dataset.modelRequestOptions).options`), {}, 'accepted server-restored Default reaches the optimistic bubble');
  await evaluate(`(async()=>{
    const selector=document.getElementById('chat-thinking-select'); selector.value='high'; selector.dispatchEvent(new Event('change',{bubbles:true}));
    await new Promise(r=>setTimeout(r,50));
    const assistant=[...document.querySelectorAll('#chat-history .msg-ai')].at(-1);
    await __chat.regenerateFrom(assistant);
  })()`);
  await until(`__calls.filter(x=>x.kind==='stream'&&x.fields.message==='first').length===2`, 'regenerate accepted Default turn');
  assert.deepEqual(await evaluate(`JSON.parse(__calls.filter(x=>x.kind==='stream'&&x.fields.message==='first')[1].fields.model_options).options`), {}, 'regenerate reuses accepted Default after preference changes to high');
  await until(`!__chat.hasActiveStream(__sessions.getCurrentSessionId())`, 'regenerated turn settlement');
  await evaluate(`(async()=>{
    __holdOptionsPatch=true;
    const select=document.getElementById('chat-thinking-select'); select.value='high'; select.dispatchEvent(new Event('change',{bubbles:true}));
    window.__staleFirstSend=__send('must not cross chats');
    while(!__optionsPatchRelease) await new Promise(r=>setTimeout(r,10));
    __sessions.createDirectChat('https://engine.invalid/chat','model-one','connection-one');
    document.getElementById('message').value='new chat draft';
    __optionsPatchRelease(); __holdOptionsPatch=false;
    await __staleFirstSend;
  })()`);
  assert.equal(await evaluate(`__calls.some(x=>x.kind==='stream'&&x.fields.message==='must not cross chats')`), false);
  assert.equal(await evaluate(`document.getElementById('message').value`), 'new chat draft');
  await evaluate(`__sessions.selectSession('chat-one',{showLoading:false})`);
  await until(`__sessions.getCurrentSessionId()==='chat-one' && !!document.querySelector('.chat-thinking-control:not([hidden])')`, 'return to original chat');
  await evaluate(`(async()=>{ const select=document.getElementById('chat-thinking-select'); select.value='high'; select.dispatchEvent(new Event('change',{bubbles:true})); await new Promise(r=>setTimeout(r,50)); await __send('selected effort'); })()`);
  assert.deepEqual(await evaluate(`__calls.find(x=>x.kind==='stream'&&x.fields.message==='selected effort')?.fields.model_options && JSON.parse(__calls.find(x=>x.kind==='stream'&&x.fields.message==='selected effort').fields.model_options).options`), {reasoning_effort:'high'});
  await until(`!__chat.hasActiveStream(__sessions.getCurrentSessionId())`, 'selected effort turn settlement');
  await evaluate(`(async()=>{const select=document.getElementById('chat-thinking-select');select.value='inherit';select.dispatchEvent(new Event('change',{bubbles:true}));})()`);
  await until(`__calls.filter(x=>x.kind==='options').length>=2`, 'explicit Default preference');
  await evaluate(`__send('default effort')`);
  await until(`__calls.some(x=>x.kind==='stream'&&x.fields.message==='default effort')`, 'Default first-send option');
  assert.deepEqual(await evaluate(`JSON.parse(__calls.find(x=>x.kind==='stream'&&x.fields.message==='default effort').fields.model_options).options`), {});
  await until(`!__chat.hasActiveStream(__sessions.getCurrentSessionId())`, 'Default turn settlement');
  await evaluate(`new Promise(resolve=>setTimeout(resolve,250))`);

  await evaluate(`(async()=>{__hold=true; __streamController=null; window.__activeSend=__send('active');})()`);
  await evaluate(`new Promise(resolve=>setTimeout(resolve,250))`);
  await until(`!!window.__streamController && __chat.hasActiveStream(__sessions.getCurrentSessionId())`, 'active stream');
  await evaluate(`(async()=>{
    await __files.addFiles([new File(['owned'],'captured-before-patch.txt',{type:'text/plain'})],{skipCrop:true});
    __holdOptionsPatch=true;
    const select=document.getElementById('chat-thinking-select'); select.value='high'; select.dispatchEvent(new Event('change',{bubbles:true}));
    document.getElementById('message').value='send waits for Thinking save'; __chat.queueStreamingComposerRequest();
  })()`);
  await until(`!!__optionsPatchRelease`, 'pending Thinking save');
  assert.equal(await evaluate(`__calls.some(x=>x.kind==='receipt'&&x.receipt.text==='send waits for Thinking save')`), false);
  await evaluate(`__files.addFiles([new File(['later'],'added-during-patch.txt',{type:'text/plain'})],{skipCrop:true})`);
  await until(`__files.getPendingCount()===2`, 'file added while save is pending');
  await evaluate(`__optionsPatchRelease(); __holdOptionsPatch=false`);
  await until(`__calls.some(x=>x.kind==='receipt'&&x.receipt.text==='send waits for Thinking save')`, 'follow-up after Thinking save');
  assert.deepEqual(await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='send waits for Thinking save').receipt.modelOptions.options`), {reasoning_effort:'high'});
  assert.deepEqual(await evaluate(`__calls.filter(x=>x.kind==='upload').at(-1).files`), ['captured-before-patch.txt']);
  assert.equal(await evaluate(`__files.getPendingCount()`), 1, 'files added while the save was pending remain in the later draft');
  await evaluate(`__files.clearPending()`);
  await evaluate(`(async()=>{document.getElementById('message').value='live steer'; __chat.queueStreamingComposerRequest();})()`);
  await until(`__calls.some(x=>x.kind==='receipt'&&x.receipt.text==='live steer')`, 'durable active steer');
  assert.equal(await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='live steer').receipt.mode`), 'steer');
  await evaluate(`(async()=>{document.getElementById('chat-input-queue-toggle').click(); document.getElementById('message').value='queued follow-up'; __chat.queueStreamingComposerRequest();})()`);
  await until(`__calls.some(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up')`, 'durable after-turn receipt');
  assert.equal(await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up').receipt.mode`), 'after-turn');
  assert.deepEqual(await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up').receipt.modelOptions.options`), {reasoning_effort:'high'});
  const initialReceiptID = await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up').receipt.requestID`);
  await evaluate(`document.querySelector('#chat-input-receipt-'+encodeURIComponent(${JSON.stringify(initialReceiptID)})+' [data-action="send-now"]')?.click()`);
  await until(`__calls.filter(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up').length===2`, 'send now handoff');
  assert.equal(await evaluate(`__calls.filter(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up')[1].receipt.requestID`), initialReceiptID);
  assert.equal(await evaluate(`__calls.filter(x=>x.kind==='receipt'&&x.receipt.text==='queued follow-up')[1].receipt.mode`), 'steer');
  await evaluate(`(async()=>{__dropReceiptOnce=true;document.getElementById('message').value='retry same receipt';__chat.queueStreamingComposerRequest();})()`);
  await until(`__calls.some(x=>x.kind==='receipt'&&x.receipt.text==='retry same receipt')`, 'unknown receipt persisted locally');
  const retryID = await evaluate(`__calls.find(x=>x.kind==='receipt'&&x.receipt.text==='retry same receipt').receipt.requestID`);
  await evaluate(`(async()=>{__chat.init(location.origin);document.dispatchEvent(new CustomEvent('odysseus:session-selected',{detail:{sessionId:'chat-one'}}));await new Promise(r=>setTimeout(r,50));document.querySelector('#chat-input-receipt-'+encodeURIComponent(${JSON.stringify(retryID)})+' [data-action="retry"]')?.click();})()`);
  await until(`__calls.filter(x=>x.kind==='receipt'&&x.receipt.text==='retry same receipt').length===2`, 'same-ID receipt retry');
  assert.equal(await evaluate(`__calls.filter(x=>x.kind==='receipt'&&x.receipt.text==='retry same receipt')[1].receipt.requestID`), retryID);
  await evaluate(`(async()=>{__streamController.enqueue(new TextEncoder().encode('data: [DONE]\\n\\n')); __streamController.close(); __hold=false; await __activeSend;})()`);

  await evaluate(`(async()=>{
    __hold=true; __streamController=null; window.__captureSend=__send('hold for attachment capture');
    while(!window.__streamController || !__chat.hasActiveStream(__sessions.getCurrentSessionId())) await new Promise(r=>setTimeout(r,10));
    await __files.addFiles([new File(['hello'],'memo.txt',{type:'text/plain'})],{skipCrop:true});
    __uploadWait=true;
    document.getElementById('message').value='capture before navigation'; __chat.queueStreamingComposerRequest();
    while(!__uploadDidStart) await new Promise(r=>setTimeout(r,10));
    __sessions.createDirectChat('https://engine.invalid/chat','model-one','connection-one');
    await __files.addFiles([new File(['later'],'memo-later.txt',{type:'text/plain'})],{skipCrop:true});
    __uploadRelease(); await new Promise(r=>setTimeout(r,75));
    __streamController.enqueue(new TextEncoder().encode('data: [DONE]\\n\\n')); __streamController.close(); __hold=false; await __captureSend;
  })()`);
  const scoped = await evaluate(`Object.keys(localStorage).filter(k=>k.includes('chat-input-delivery'))`);
  assert.ok(scoped.length >= 1, 'captured follow-up remains in chat-scoped local retry storage');
  assert.equal(await evaluate(`__files.getPendingCount()`), 1, 'a file added during upload remains pending for the later draft');
  assert.equal(await evaluate(`__files.getPendingInfo()[0].name`), 'memo-later.txt');
  assert.ok(await evaluate(`__calls.some(x=>x.kind==='upload'&&x.session==='chat-one')`), 'upload remains bound to the chat captured before navigation');
});
process.stdout.write('chat input delivery browser acceptance passed\n');
