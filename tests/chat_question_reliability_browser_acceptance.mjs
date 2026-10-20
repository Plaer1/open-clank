#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

// Serve source only; no running app, credentials, chat store or provider calls.
const sourceRoot = path.resolve(fileURLToPath(new URL('..', import.meta.url)));
const server = http.createServer((req, res) => {
  const pathname = new URL(req.url, 'http://fixture').pathname;
  if (pathname === '/' || pathname === '/login') { res.setHeader('Content-Type', 'text/html'); res.end('<!doctype html><body></body>'); return; }
  const target = path.resolve(sourceRoot, '.' + pathname);
  if (!target.startsWith(sourceRoot + path.sep)) { res.writeHead(403).end(); return; }
  try { res.setHeader('Content-Type', pathname.endsWith('.js') ? 'text/javascript' : 'text/html'); res.end(fs.readFileSync(target)); }
  catch { res.writeHead(404).end(); }
});
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
const base = `http://127.0.0.1:${server.address().port}`;
const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-models-'));
const browserExecutable = process.env.OPENCLANK_CHROME_BIN
  || process.env.CHROME_BIN
  || process.env.ODYSSEUS_BROWSER_EXECUTABLE
  || ['/usr/bin/chromium', '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome']
    .find(candidate => fs.existsSync(candidate));
assert(browserExecutable, 'Chromium or Google Chrome is required for this browser acceptance test');
const chromium = spawn(browserExecutable, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let targets;
  for (let attempt = 0; attempt < 600; attempt += 1) {
    try {
      targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      if (targets?.some(item => item.type === 'page')) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    } catch { await new Promise(resolve => setTimeout(resolve, 50)); }
  }
  const target = targets?.find(item => item.type === 'page');
  assert(target?.webSocketDebuggerUrl, 'Chromium page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id || !pending.has(message.id)) return;
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 20_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const response = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };
  const waitFor = async (expression, label) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      try { if (await evaluate(expression)) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 75));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };


  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: base });
  await waitFor("document.readyState === 'complete'", 'fixture');
  await evaluate(String.raw`(async () => {
    const originalFetch = window.fetch.bind(window);
    const html = await originalFetch('/static/index.html').then(r => r.text());
    document.body.replaceChildren(...new DOMParser().parseFromString(html, 'text/html').body.childNodes);
    window.__errors = [];
    window.addEventListener('error', e => window.__errors.push(e.error?.message || e.message));
    window.addEventListener('unhandledrejection', e => window.__errors.push(e.reason?.message || String(e.reason)));
    window.__posts = [];
    window.__stored = [];
    window.__patchMode = 'ok';
    window.__chatMode = 'ok';
    const json = (value, status = 200) => new Response(JSON.stringify(value), {status, headers:{'Content-Type':'application/json'}});
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      if (url.endsWith('/api/chat_stream')) {
        const fields = Object.fromEntries(options.body.entries());
        window.__posts.push(fields);
        if (window.__chatWait) await window.__chatWait;
        if (window.__chatMode === 'network') throw new Error('Synthetic disconnected transport');
        if (window.__chatMode === 'reject') return json({detail:{code:'selection_incomplete',message:'Choose a provider connection and model'}},400);
        const stored = window.__stored.find(s => s.id === fields.session);
        if (stored && fields.selected_endpoint_id !== stored.endpoint_id) throw new Error('Route identity mismatch');
        const questionEvent = window.__questionToStream ? 'data: '+JSON.stringify({type:'ask_user',data:window.__questionToStream})+'\n\n' : '';
        return new Response(questionEvent+'data: {"delta":"Synthetic reply"}\n\ndata: [DONE]\n\n', {headers:{'Content-Type':'text/event-stream'}});
      }
      if (url.endsWith('/api/session') && options.method === 'POST') {
        if (window.__createWait) await window.__createWait;
        if (window.__createMode === 'reject') return json({detail:'Connection unavailable'},400);
        const fields = Object.fromEntries(options.body.entries());
        const session = { ...fields, id:'chat-' + (window.__stored.length+1), message_count:0 };
        window.__stored.push(session);
        return json(session);
      }
      if (url.includes('/api/session/') && options.method === 'PATCH') {
        const fields = Object.fromEntries(options.body.entries());
        window.__patchFields = fields;
        (window.__patchRequests ||= []).push(fields);
        if (window.__patchWait) await window.__patchWait;
        if (window.__patchMode === 'network') throw new Error('Synthetic picker disconnected');
        if (window.__patchMode === 'reject') return json({detail:{code:'selection_invalid',message:'Choice unavailable'}},400);
        const sid = url.split('/').pop();
        const session = window.__stored.find(s => s.id === sid);
        Object.assign(session,fields);
        return json(session);
      }
      if (url.includes('/api/sessions')) {
        const rows=structuredClone(window.__stored); window.__sessionsReadStarted=true;
        if(window.__sessionsWait) await window.__sessionsWait; return json(rows);
      }
      if (url.includes('/api/models')) return json({items:window.__catalogItems || []});
      if (url.includes('/mimo-state')) {
        const state = window.__state || {pending_question:null};
        if (window.__stateWait) await window.__stateWait;
        return json(state);
      }
      if (url.endsWith('/question') && options.method === 'POST') {
        window.__questionReply = JSON.parse(options.body);
        if (window.__replyWait) await window.__replyWait;
        if (window.__replyMode === 'network') throw new Error('Question connection lost');
        if (window.__replyMode === 'invalid') return json({detail:{code:'question_invalid_answer',message:'One answer required'}},422);
        if (window.__replyMode === 'stale') return json({detail:{code:'question_stale',message:'Question already answered'}},409);
        return json({ok:true});
      }
      if (url.includes('/history')) return json([]);
      if (url.includes('/stream_status')) return json({status:'idle'});
      if (url.endsWith('/api/default-chat')) return json(window.__defaultChat || {});
      return json({});
    };
    const sessions = (await import('/static/js/sessions.js')).default;
    const chat = (await import('/static/js/chat.js')).default;
    const models = await import('/static/js/models.js');
    models.init(''); window.modelsModule = models;
    window.__renderer = (await import('/static/js/chatRenderer.js')).default;
    window.__chat = chat; window.chatModule = chat; window.__sessions = sessions;
    chat.init('');
    window.__send = async (text) => {
      const input = document.getElementById('message');
      input.value = text; input.dispatchEvent(new Event('input',{bubbles:true}));
      await chat.handleChatSubmit({preventDefault(){}});
    };
    await sessions.createDirectChat('https://a.invalid/v1/chat/completions','same-model','pcn-a');
    await window.__send('first');
  })()`);
  assert.deepEqual(await evaluate('__posts.map(p=>[p.session,p.selected_model,p.selected_endpoint_id])'), [['chat-1','same-model','pcn-a']]);
  await evaluate(`__send('second')`);
  assert.equal(await evaluate('__posts[1].selected_endpoint_id'), 'pcn-a', 'second send retains acknowledged connection');
  await evaluate(`(async()=>{
    window.__odysseusLastPickedRoute={model:'foreign',endpoint_id:'foreign',picked_at:Date.now()};
    window.__sessions.setCurrentSessionId('chat-1');
    const realNow=Date.now; Date.now=()=>realNow()+11*60*1000;
    await __send('reopened after ten minutes'); Date.now=realNow;
    await __sessions.createDirectChat('https://b.invalid/v1/chat/completions','same-model','shared:grant');
    await __send('shared first');
    __sessions.setCurrentSessionId('chat-1');
    await __send('back to owned');
  })()`);
  assert.deepEqual(await evaluate('__posts.slice(2).map(p=>p.selected_endpoint_id)'), ['pcn-a','shared:grant','pcn-a']);
  await evaluate(`(async()=>{
    window.__catalogItems=[{endpoint_id:'pcn-b',endpoint_name:'B',url:'https://b.invalid/v1/chat/completions',category:'api',models:['same-model']}];
    await window.modelsModule.refreshModels(true);
    document.getElementById('model-picker-btn').click();
    window.__patchWait=new Promise(resolve=>window.__releasePatch=resolve);
    document.querySelector('.model-switch-item').click();
    window.__sendWait=__send('wait for acknowledged choice');
  })()`);
  assert.equal(await evaluate('__sessions.getCurrentRoute().endpoint_id'), 'pcn-a', 'unacknowledged PATCH does not overwrite session');
  assert.equal(await evaluate('__posts.length'), 5, 'send waits for picker persistence');
  await evaluate(`(async()=>{ __releasePatch(); await __sendWait; __patchWait=null; })()`);
  assert.equal(await evaluate('__posts.at(-1).selected_endpoint_id'), 'pcn-b', 'send snapshots route after picker fence');
  await evaluate(`(async()=>{
    __patchMode='reject'; document.getElementById('model-picker-menu').classList.add('hidden');
    document.getElementById('model-picker-btn').click(); document.querySelector('.model-switch-item').click();
    await window.__odysseusModelSwitchPromise; await __send('after rejected choice'); __patchMode='ok';
  })()`);
  assert.equal(await evaluate('__sessions.getCurrentRoute().endpoint_id'), 'pcn-b');
  await evaluate(`(async()=>{
    document.getElementById('model-picker-menu').classList.add('hidden'); document.getElementById('model-picker-btn').click();
    __patchWait=new Promise(resolve=>window.__releasePatch=resolve); document.querySelector('.model-switch-item').click();
    window.__sendWait=__send('do not send into another chat'); __sessions.setCurrentSessionId('chat-2');
    __releasePatch(); await __sendWait; __patchWait=null;
  })()`);
  assert.equal(await evaluate('__posts.length'), 7, 'navigation during PATCH cancels original send');
  await evaluate(`(async()=>{
    __chatMode='reject'; await __send('restore this draft');
  })()`);
  assert.equal(await evaluate('document.getElementById("message").value'), 'restore this draft', 'admission error restores draft');
  assert.equal(await evaluate('[...document.querySelectorAll(".msg-user")].filter(n=>n.textContent.includes("restore this draft")).length'),0,'pre-turn rejection removes optimistic user bubble');
  await evaluate(`(async()=>{ __chatMode='ok'; await __send('restore this draft'); })()`);
  assert.equal(await evaluate('[...document.querySelectorAll(".msg-user")].filter(n=>n.textContent.includes("restore this draft")).length'),1,'corrected retry has one user bubble');
  await evaluate(`(async()=>{ __chatMode='network'; await __send('network retry'); __chatMode='ok'; })()`);
  assert.equal(await evaluate('document.getElementById("message").value'),'network retry');

  await evaluate(`(async()=>{
    __sessions.setCurrentSessionId('chat-1');
    window.__catalogItems.push({endpoint_id:'pcn-c',endpoint_name:'C',url:'https://c.invalid/v1/chat/completions',category:'api',models:['same-model']});
    await window.modelsModule.refreshModels(true);
    window.__pickEndpoint = id => {
      const menu=document.getElementById('model-picker-menu'); menu.classList.add('hidden');
      document.getElementById('model-picker-btn').click();
      const row=[...document.querySelectorAll('.model-switch-item')].find(n=>n.querySelector('.model-switch-ep')?.textContent===id);
      if(!row) throw new Error('Missing picker '+id); row.click();
    };
    __patchWait=new Promise(resolve=>window.__releasePatch=resolve); __patchRequests=[];
    __pickEndpoint('B'); __pickEndpoint('C'); window.__sendWait=__send('serialized picks');
  })()`);
  assert.equal(await evaluate('__patchRequests.length'),1,'later picker PATCH cannot overtake earlier persistence');
  await evaluate(`(async()=>{__releasePatch(); await __sendWait; __patchWait=null;})()`);
  assert.deepEqual(await evaluate('__patchRequests.map(p=>p.endpoint_id)'),['pcn-b','pcn-c']);
  assert.equal(await evaluate('__posts.at(-1).selected_endpoint_id'),'pcn-c');
  await evaluate(`(async()=>{
    __patchMode='network'; __pickEndpoint('B'); await window.__odysseusModelSwitchPromise;
    await __send('after failed network pick'); __patchMode='ok';
  })()`);
  assert.equal(await evaluate('__posts.at(-1).selected_endpoint_id'),'pcn-c','failed speculative picker route cannot leak into send');
  await evaluate(`(async()=>{
    const sid=__sessions.getCurrentSessionId();
    __chat.restoreComposerDraft({available:true,content_kind:'text',raw_content:'',attachment_refs:[{type:'attachment_ref',attachment_id:'canonical-photo'}]}, {sid,expected:__chat.captureComposerDraftState()});
    __chatMode='reject'; await __send('');
  })()`);
  assert.deepEqual(await evaluate('__chat.captureComposerDraftState().attachments'),['canonical-photo'],'rejected attachment-only draft keeps canonical ids');
  await evaluate(`(async()=>{__chatMode='ok'; await __send('');})()`);
  assert.equal(await evaluate('__posts.at(-1).attachments'),'["canonical-photo"]');
  await evaluate(`(async()=>{
    __chatMode='reject'; __chatWait=new Promise(resolve=>window.__releaseChat=resolve);
    window.__sendWait=__send('older rejected draft');
  })()`);
  await waitFor('__posts.at(-1).message === "older rejected draft"','pending rejection');
  await evaluate(`(async()=>{
    const input=document.getElementById('message'); input.value='newer draft'; input.dispatchEvent(new Event('input',{bubbles:true}));
    __releaseChat(); await __sendWait; __chatWait=null; __chatMode='ok';
  })()`);
  assert.equal(await evaluate('document.getElementById("message").value'),'newer draft','rejection cannot overwrite a newer draft');
  const beforeMoved = await evaluate('__posts.length');
  await evaluate(`(async()=>{
    await __sessions.createDirectChat('https://a.invalid/v1/chat/completions','same-model','pcn-a');
    __createWait=new Promise(resolve=>window.__releaseCreate=resolve); window.__sendWait=__send('abandoned pending');
    await Promise.resolve(); __sessions.setCurrentSessionId('chat-1'); __sessions.clearPendingChat();
    __releaseCreate(); await __sendWait; __createWait=null;
  })()`);
  assert.equal(await evaluate('__posts.length'),beforeMoved,'navigation during materialization cannot send to another chat');
  await evaluate(`(async()=>{
    __sessions.setCurrentSessionId(null); window.__odysseusLastSelectedSessionId='chat-1';
    location.hash='chat-1'; __stateWait=null;
    const originalFetch=window.fetch; window.__historyWait=new Promise(resolve=>window.__releaseHistory=resolve);
    window.fetch=async(input,options)=>{if(String(input).includes('/history')) await __historyWait; return originalFetch(input,options);};
    window.__sendWait=__send('abandoned adoption');
    await Promise.resolve(); __sessions.setCurrentSessionId('chat-2');
    __releaseHistory(); await __sendWait; window.fetch=originalFetch; location.hash='';
  })()`);
  assert.equal(await evaluate('__posts.length'),beforeMoved,'navigation during opened-session adoption cancels original draft');


  await evaluate(`(async()=>{
    window.__catalogItems=[]; await window.modelsModule.refreshModels(true);
    window.__odysseusLastSelectedSessionId=''; location.hash=''; __sessions.setCurrentSessionId(null); __sessions.clearPendingChat();
    __defaultChat={}; window.__odysseusDefaultChat=null; localStorage.removeItem('odysseus-default-chat-cache');
    await __send('missing default draft');
  })()`);
  assert.equal(await evaluate('document.getElementById("message").value'),'missing default draft','missing default leaves unsent draft');
  await evaluate(`(async()=>{
    window.__catalogItems=[{endpoint_id:'pcn-default',endpoint_name:'Default',url:'https://default.invalid/v1/chat/completions',category:'api',models:['same-model']}]; await window.modelsModule.refreshModels(true);
    __defaultChat={endpoint_id:'pcn-default',endpoint_url:'https://default.invalid/v1/chat/completions',model:'same-model'};
    __createMode='reject'; await __send('failed default draft'); __createMode='ok';
  })()`);
  assert.equal(await evaluate('document.getElementById("message").value'),'failed default draft','failed default materialization leaves unsent draft');
  await evaluate(`(async()=>{await __send('default first');await __send('default second');})()`);
  assert.deepEqual(await evaluate('__posts.slice(-2).map(p=>p.selected_endpoint_id)'),['pcn-default','pcn-default'],await evaluate('JSON.stringify({sid:__sessions.getCurrentSessionId(),pending:__sessions.getPendingChat(),draft:document.getElementById("message").value,posts:__posts.length,stored:__stored.map(s=>[s.id,s.endpoint_id])})'));
  await evaluate(`(async()=>{
    window.__catalogItems=[{endpoint_id:'pcn-b',endpoint_name:'B',url:'https://b.invalid/v1/chat/completions',category:'api',models:['same-model']},{endpoint_id:'pcn-c',endpoint_name:'C',url:'https://c.invalid/v1/chat/completions',category:'api',models:['same-model']}]; await window.modelsModule.refreshModels(true);
    await __sessions.selectSession('chat-1',{showLoading:false}); await __send('actual reopened send');
  })()`);
  assert.equal(await evaluate('__posts.at(-1).selected_endpoint_id'),'pcn-c','actual session history reopen preserves complete route');
  await evaluate(`(()=>{
    __sessionsReadStarted=false; __sessionsWait=new Promise(resolve=>window.__releaseSessions=resolve);
    window.__listWait=__sessions.loadSessions();
  })()`);
  await waitFor('__sessionsReadStarted','delayed sidebar snapshot');
  await evaluate(`(async()=>{
    __pickEndpoint('B'); await window.__odysseusModelSwitchPromise;
    __releaseSessions(); await __listWait; __sessionsWait=null; await __send('after stale sidebar response');
  })()`);
  assert.equal(await evaluate('__posts.at(-1).selected_endpoint_id'),'pcn-b','stale sidebar fetch cannot overwrite newer acknowledged picker route');
  await evaluate(`(async()=>{
    await __sessions.selectSession('chat-2',{showLoading:false});
    window.__question = (id, multiple=false) => ({session_id:'chat-2',request_id:id,questions:[{question:'Pick a path',multiple,custom:true,options:[{label:'One'},{label:'Two'}]}]});
    window.__card = id => document.querySelector('.mimo-question-card[data-request-id="'+id+'"]');
    window.__answer = (card, custom) => { const input=card.querySelector('[data-custom-question-index]'); input.value=custom; input.dispatchEvent(new Event('input',{bubbles:true})); };
    window.__renderQuestion = (id,multiple=false) => __renderer.renderAskUserCard(__question(id,multiple),{focus:false,scroll:false});
    __state={pending_question:__question('q-radio')}; await __chat.restoreSessionState('chat-2');
    const card=__card('q-radio'); const radio=card.querySelector('input[type=radio]'); radio.click(); __answer(card,'Custom');
  })()`);
  assert.equal(await evaluate('__card("q-radio").querySelectorAll("input[type=radio]:checked").length'),0,'custom input clears single-choice radio');
  await evaluate(`(()=>{const card=__card('q-radio'); card.querySelector('input[type=radio]').click();})()`);
  assert.equal(await evaluate('__card("q-radio").querySelector("[data-custom-question-index]").value'),'','radio clears custom answer');
  await evaluate(`(()=>{__replyMode='invalid'; __card('q-radio').requestSubmit();})()`);
  await waitFor('__card("q-radio")?.textContent.includes("One answer required")','typed invalid reply');
  assert.equal(await evaluate('__card("q-radio").querySelectorAll("input[type=radio]:checked").length'),1,'invalid answer retains card and selection');
  await evaluate(`(()=>{__replyMode='network'; __card('q-radio').requestSubmit();})()`);
  await waitFor('__card("q-radio")?.textContent.includes("Question connection lost")','network question failure');
  await evaluate(`(()=>{__replyMode='ok'; __card('q-radio').requestSubmit();})()`);
  await waitFor('!__card("q-radio")','acknowledged question removal');
  assert.deepEqual(await evaluate('__questionReply.answers'),[['One']]);
  await evaluate(`(()=>{const card=__renderQuestion('q-multi',true);card.querySelector('input[type=checkbox]').click();__answer(card,'Also custom');card.requestSubmit();})()`);
  await waitFor('!__card("q-multi")','multiselect acknowledgement');
  assert.deepEqual(await evaluate('__questionReply.answers'),[['One','Also custom']],'multiselect remains additive');
  await evaluate(`(()=>{__renderQuestion('q-empty').requestSubmit();})()`);
  assert.equal(await evaluate('__card("q-empty").textContent.includes("Answer every question")'),true);
  await evaluate(`(()=>{__replyMode='stale';__answer(__card('q-empty'),'answer');__card('q-empty').requestSubmit();})()`);
  await waitFor('!__card("q-empty")','typed terminal stale removal');
  await evaluate(`(async()=>{
    __replyMode='ok'; __state={pending_question:__question('q-state')};
    await __chat.restoreSessionState('chat-2');
    __state={pending_question:null}; await __chat.restoreSessionState('chat-2');
  })()`);
  assert.equal(await evaluate('document.querySelectorAll(".mimo-question-card[data-interactive=true]").length'),0,'authoritative absence clears pending controls');
  await evaluate(`(async()=>{
    __state={pending_question:null}; __stateWait=new Promise(resolve=>window.__releaseState=resolve);
    window.__restoreWait=__chat.restoreSessionState('chat-2');
    __questionToStream=__question('q-stream-new'); await __send('ask a question'); __questionToStream=null;
    __releaseState(); await __restoreWait; __stateWait=null;
  })()`);
  assert.equal(await evaluate('Boolean(__card("q-stream-new"))'),true,'older absent-state response cannot erase newer SSE question');
  await evaluate(`(async()=>{
    __sessions.setCurrentSessionId('chat-2');
    __replyWait=new Promise(resolve=>window.__releaseReply=resolve); __answer(__card('q-stream-new'),'old answer'); __card('q-stream-new').requestSubmit();
    __state={pending_question:__question('q-newer')}; await __chat.restoreSessionState('chat-2');
    __releaseReply(); __replyWait=null;
  })()`);
  assert.equal(await evaluate('JSON.parse(sessionStorage.getItem("odysseus-mimo-question-chat-2"))?.request_id'), 'q-newer', 'new request survives old acknowledgement');
  assert.equal(await evaluate('Boolean(__card("q-newer"))'),true);
  await evaluate(`(async()=>{
    __state={pending_question:null}; __stateWait=new Promise(resolve=>window.__releaseState=resolve);
    window.__restoreWait=__chat.restoreSessionState('chat-2');
    __sessions.setCurrentSessionId('chat-1'); __renderQuestion('q-other-view');
    __releaseState(); await __restoreWait; __stateWait=null;
  })()`);
  assert.equal(await evaluate('Boolean(__card("q-other-view"))'),true,'navigation fences delayed state readback');
  await evaluate(`(()=>{__renderer.renderAskUserCard(__question('q-history'),{interactive:false,focus:false,scroll:false});})()`);
  assert.equal(await evaluate('__card("q-history").querySelectorAll("input:not(:disabled),button:not(:disabled)").length'),0,'historical request does not imply live controls');
  assert.deepEqual(await evaluate('__errors'), [], 'mounted shipped modules produce no browser errors');
  if (process.argv.includes('--with-existing-model-picker')) {
    const check = spawn(process.execPath, [path.join(sourceRoot,'tests/model_catalog_browser_acceptance.mjs'), base], {stdio:'inherit'});
    const code = await new Promise(resolve => check.once('exit',resolve));
    assert.equal(code,0,'existing model catalog/picker acceptance');
  }
  console.log('PASS mounted chat continuity, route isolation, serialized picker/navigation fences, draft/attachment recovery and question lifecycle');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => { if (chromium.exitCode !== null || chromium.signalCode) resolve(); else chromium.once('exit',resolve); });
  server.close();
  try { fs.rmSync(profile, {recursive:true,force:true,maxRetries:5,retryDelay:100}); } catch {}
}
