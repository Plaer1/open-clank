#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
const chrome = [
  process.env.OPEN_CLANK_CHROME_BIN,
  process.env.CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
].filter(Boolean).find(candidate => fs.existsSync(candidate));
if (!chrome) {
  process.stdout.write(JSON.stringify({ skipped: 'Chrome/Chromium unavailable' }) + '\n');
  process.exit(0);
}

const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-session-workspace-'));
const chromium = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let target;
  for (let attempt = 0; attempt < 400; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      target = targets?.find(item => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert(target?.webSocketDebuggerUrl, 'Chrome page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (!message.id) return;
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id);
    clearTimeout(request.timer);
    message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => reject(new Error(`${method} timed out`)), 25_000);
    pending.set(id, { resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    let response;
    try {
      response = await command('Runtime.evaluate', {
      expression, awaitPromise: true, returnByValue: true,
      });
    } catch (error) {
      throw new Error(`${error.message}: ${expression.slice(0, 120).replace(/\s+/g, ' ')}`);
    }
    if (response.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    }
    return response.result.value;
  };
  const waitFor = async (expression, label) => {
    const deadline = Date.now() + 15_000;
    while (Date.now() < deadline) {
      try { if (await evaluate(expression)) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor("document.readyState === 'complete'", 'Open Clank origin');

  await evaluate(`(async () => {
    const html = await fetch('/static/index.html').then(response => response.text());
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    document.body.replaceChildren(...[...parsed.body.childNodes].map(node => node.cloneNode(true)));
    localStorage.clear();
    sessionStorage.clear();
    sessionStorage.setItem('ody-session-active', '1');
    HTMLElement.prototype.scrollIntoView = function () {};
    window.__workspaceErrors = [];
    window.__workspaceToasts = [];
    window.addEventListener('error', event => window.__workspaceErrors.push(event.error?.message || event.message));
    window.addEventListener('unhandledrejection', event => window.__workspaceErrors.push(event.reason?.message || String(event.reason)));
    window.refreshChatContextHeader = () => {};
    window._updateSendBtnIcon = () => {};
    window.compareModule = { isActive:() => false, hasVisibleResults:() => false, cleanupResults() {} };
    window.documentModule = { clearSelection() {}, isPanelOpen:() => false };
    window.presetsModule = { onSessionSwitch() {} };
    window.modelsModule = { getCachedItems:() => [], refreshModels:async () => {} };
    window.chatModule = {
      detachCurrentStream() {}, abortCurrentRequest() {}, showWelcomeScreen() {},
    };
    const json = (body, status = 200) => new Response(JSON.stringify(body), {
      status, headers:{ 'Content-Type':'application/json' },
    });
    const sessionRows = [
      { id:'chat-a', name:'Alpha chat', model:'browser-model', endpoint_url:'browser://model', archived:false, workspace_id:'workspace-a', message_count:1 },
      { id:'chat-b', name:'Beta chat', model:'browser-model', endpoint_url:'browser://model', archived:false, workspace_id:'workspace-b', message_count:1 },
    ];
    window.__sessionRows = sessionRows;
    window.__workspacePatches = [];
    window.__sessionCreates = [];
    window.__chatSends = [];
    window.__chatStreamFrames = 'data: [DONE]\\n\\n';
    window.__deferWorkspaceRejection = false;
    window.__workspaceRejectionController = null;
    window.__resolveDelays = Object.create(null);
    window.__resolveWaiters = Object.create(null);
    window.__patchFailureSession = '';
    window.__patchFailureWaiter = null;
    window.fetch = async (input, options = {}) => {
      const url = new URL(String(input), location.origin);
      const method = String(options.method || 'GET').toUpperCase();
      if (url.pathname === '/api/sessions') return json(sessionRows);
      if (url.pathname.startsWith('/api/history/')) {
        return json({ history:[], model:'browser-model', offset:0, limit:24, total:0, has_more_before:false });
      }
      const resolveMatch = url.pathname.match(/^\\/api\\/file-policy\\/workspaces\\/([^/]+)\\/resolve$/);
      if (resolveMatch) {
        const workspaceId = decodeURIComponent(resolveMatch[1]);
        const finish = () => {
          if (workspaceId === 'workspace-revoked') {
            return json({ detail:{ code:'workspace_unavailable', message:'Workspace unavailable' } }, 403);
          }
          const suffix = workspaceId.replace(/^workspace-/, '');
          return json({ workspace:{ id:workspaceId, path:'/allowed/' + suffix, name:suffix } });
        };
        if (window.__resolveDelays[workspaceId]) {
          return new Promise(resolve => { window.__resolveWaiters[workspaceId] = () => resolve(finish()); });
        }
        return finish();
      }
      const sessionMatch = url.pathname.match(/^\\/api\\/session\\/([^/]+)$/);
      if (sessionMatch && method === 'PATCH') {
        const sessionId = decodeURIComponent(sessionMatch[1]);
        const body = Object.fromEntries(options.body.entries());
        window.__workspacePatches.push({ sessionId, ...body });
        if (window.__patchFailureSession === sessionId) {
          return new Promise(resolve => {
            window.__patchFailureWaiter = () => resolve(json({ detail:{ message:'delayed binding failure' } }, 409));
          });
        }
        const row = sessionRows.find(item => item.id === sessionId);
        if (row) row.workspace_id = body.workspace_id || null;
        return json({ ...(row || { id:sessionId }), workspace_id:body.workspace_id || null });
      }
      if (url.pathname === '/api/session' && method === 'POST') {
        const body = Object.fromEntries(options.body.entries());
        window.__sessionCreates.push(body);
        const created = {
          id:'chat-pending', name:body.name || 'Pending chat', model:body.model || 'browser-model',
          endpoint_url:body.endpoint_url || 'browser://model', endpoint_id:body.endpoint_id || '',
          archived:false, message_count:0, workspace_id:body.workspace_id || null,
        };
        sessionRows.unshift(created);
        return json(created);
      }
      if (url.pathname === '/api/chat_stream' && method === 'POST') {
        window.__chatSends.push(Object.fromEntries(options.body.entries()));
        if (window.__deferWorkspaceRejection) {
          const stream = new ReadableStream({
            start(controller) { window.__workspaceRejectionController = controller; },
          });
          return new Response(stream, {
            status:200, headers:{ 'Content-Type':'text/event-stream' },
          });
        }
        return new Response(window.__chatStreamFrames, {
          status:200, headers:{ 'Content-Type':'text/event-stream' },
        });
      }
      if (url.pathname === '/api/default-chat') return json({});
      if (url.pathname === '/api/research/status') return json({ running:false });
      return json({});
    };
    const sessions = await import('/static/js/sessions.js');
    const workspace = await import('/static/js/workspace.js');
    window.sessionModule = sessions.default;
    window.__workspaceSessions = sessions;
    window.__workspaceModule = workspace;
    window.__workspacePaints = [];
    window.addEventListener('workspace-change', event => {
      window.__workspacePaints.push({
        sessionId:sessions.getCurrentSessionId(),
        path:event.detail?.path || '',
        workspaceId:event.detail?.workspaceId || '',
      });
    });
    await sessions.loadSessions();
  })()`);

  const select = id => evaluate(`window.__workspaceSessions.selectSession(${JSON.stringify(id)}, { showLoading:false, keepSidebar:true })`);
  const display = () => evaluate(`(() => ({
    sessionId:window.__workspaceSessions.getCurrentSessionId(),
    path:localStorage.getItem('odysseus-workspace'),
    workspaceId:localStorage.getItem('odysseus-workspace-id'),
    pillVisible:document.getElementById('workspace-indicator-btn').style.display !== 'none',
    pillName:document.getElementById('workspace-indicator-name').textContent,
  }))()`);

  await select('chat-a');
  assert.deepEqual(await display(), {
    sessionId:'chat-a', path:'/allowed/a', workspaceId:'workspace-a', pillVisible:true, pillName:'a',
  });
  await select('chat-b');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:'/allowed/b', workspaceId:'workspace-b', pillVisible:true, pillName:'b',
  }, 'persisted chats restore independent workspace pills');

  await evaluate('window.__workspaceModule.clearWorkspace()');
  const clearPatch = await evaluate('window.__workspacePatches.at(-1)');
  assert.deepEqual(clearPatch, { sessionId:'chat-b', workspace_id:'' }, 'clear PATCH targets only the active chat');
  assert.equal(await evaluate("window.__workspaceSessions.getSessions().find(row => row.id === 'chat-a').workspace_id"), 'workspace-a');
  assert.equal((await display()).pillVisible, false);

  await select('chat-a');
  const patchCountBeforeRevoked = await evaluate('window.__workspacePatches.length');
  await evaluate("window.__workspaceSessions.setSessionWorkspaceId('chat-b', 'workspace-revoked')");
  await select('chat-b');
  const revoked = await display();
  assert.equal(revoked.path, null);
  assert.equal(revoked.workspaceId, null);
  assert.equal(revoked.pillVisible, false);
  assert.equal(await evaluate('window.__workspacePatches.length'), patchCountBeforeRevoked,
    'revoked restore clears local display without a PATCH loop');
  const revokedPaints = await evaluate("window.__workspacePaints.filter(row => row.sessionId === 'chat-b').slice(-2)");
  assert(revokedPaints.every(row => !row.path && !row.workspaceId), 'revoked workspace never paints a path');

  await select('chat-a');
  await evaluate(`(() => {
    window.__workspaceSessions.setSessionWorkspaceId('chat-b', 'workspace-b');
    window.__resolveDelays['workspace-b'] = true;
  })()`);
  await evaluate("(() => { window.__pendingSelectB = window.__workspaceSessions.selectSession('chat-b', { showLoading:false, keepSidebar:true }); return true; })()");
  await waitFor("window.__resolveWaiters['workspace-b']", 'delayed beta workspace resolution');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:null, workspaceId:null, pillVisible:false, pillName:'',
  }, 'session switch clears the previous send pointer before async resolution');
  await evaluate("window.__resolveWaiters['workspace-b'](); delete window.__resolveWaiters['workspace-b']");
  await evaluate('window.__pendingSelectB');
  assert.equal((await display()).workspaceId, 'workspace-b');

  await evaluate(`(() => {
    window.__resolveDelays['workspace-a'] = true;
    window.__resolveDelays['workspace-b'] = true;
    window.__raceA = window.__workspaceSessions.selectSession('chat-a', { showLoading:false, keepSidebar:true });
  })()`);
  await waitFor("window.__resolveWaiters['workspace-a']", 'delayed alpha race resolution');
  await evaluate("(() => { window.__raceB = window.__workspaceSessions.selectSession('chat-b', { showLoading:false, keepSidebar:true }); return true; })()");
  await waitFor("window.__resolveWaiters['workspace-b']", 'delayed beta race resolution');
  await evaluate("window.__resolveWaiters['workspace-b'](); delete window.__resolveWaiters['workspace-b']");
  await evaluate('window.__raceB');
  await evaluate("window.__resolveWaiters['workspace-a'](); delete window.__resolveWaiters['workspace-a']");
  await evaluate('window.__raceA');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:'/allowed/b', workspaceId:'workspace-b', pillVisible:true, pillName:'b',
  }, 'late navigation cannot repaint the currently selected chat');

  await evaluate(`(() => {
    window.__resolveDelays['workspace-a'] = false;
    window.__resolveDelays['workspace-b'] = false;
  })()`);
  await select('chat-a');
  await evaluate(`(() => {
    window.__patchFailureSession = 'chat-a';
    window.__failingClear = window.__workspaceModule.clearWorkspace().catch(error => error.message);
  })()`);
  await waitFor('window.__patchFailureWaiter', 'delayed clear failure');
  await select('chat-b');
  await evaluate('window.__patchFailureWaiter(); window.__patchFailureWaiter = null');
  assert.equal(await evaluate('window.__failingClear'), 'delayed binding failure');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:'/allowed/b', workspaceId:'workspace-b', pillVisible:true, pillName:'b',
  }, 'an older failed clear cannot roll its display back over a newer chat');
  assert.equal(await evaluate('window.__workspacePatches.at(-1).sessionId'), 'chat-a');

  const pendingState = await evaluate(`(async () => {
    const sessions = window.__workspaceSessions;
    sessions.createDirectChat('browser://pending', 'browser-pending-model', 'endpoint-pending');
    const pendingBeforeWorkspace = sessions.getPendingChat();
    await window.__workspaceModule.setWorkspace('/allowed/pending', 'workspace-pending', { persist:false });
    const pendingBeforeMaterialize = sessions.getPendingChat();
    const ok = await sessions.materializePendingSession();
    return {
      ok,
      pendingBeforeWorkspace,
      pendingBeforeMaterialize,
      id:sessions.getCurrentSessionId(),
      create:window.__sessionCreates.at(-1),
      cached:sessions.getSessions().find(row => row.id === 'chat-pending'),
    };
  })()`);
  assert.equal(pendingState.ok, true, JSON.stringify(pendingState));
  assert.equal(pendingState.id, 'chat-pending');
  assert.equal(pendingState.create.workspace_id, 'workspace-pending',
    'pending chat materializes with the stable Workspace ID');
  assert.equal(pendingState.create.workspace, undefined, 'pending materialization never submits a raw path');
  assert.equal(pendingState.cached.workspace_id, 'workspace-pending');

  const sendState = await evaluate(`(async () => {
    const chat = await import('/static/js/chat.js');
    chat.init(location.origin);
    localStorage.setItem('odysseus-workspace', '/allowed/private-path');
    localStorage.setItem('odysseus-workspace-id', 'workspace-pending');
    window.__chatStreamFrames = 'data: {"type":"workspace_rejected","data":{}}\\n\\ndata: [DONE]\\n\\n';
    const patchCount = window.__workspacePatches.length;
    document.getElementById('message').value = 'workspace id only';
    await chat.handleChatSubmit({ preventDefault() {} });
    for (let i = 0; i < 20 && localStorage.getItem('odysseus-workspace-id'); i++) {
      await new Promise(resolve => setTimeout(resolve, 0));
    }
    return {
      body:window.__chatSends.at(-1),
      patchDelta:window.__workspacePatches.length - patchCount,
      cached:window.__workspaceSessions.getSessions().find(row => row.id === 'chat-pending')?.workspace_id || null,
      displayedId:localStorage.getItem('odysseus-workspace-id'),
    };
  })()`);
  assert.equal(sendState.body.workspace_id, 'workspace-pending');
  assert.equal(sendState.body.workspace, undefined, 'chat send never submits the raw workspace path');
  assert.equal(sendState.body.session, 'chat-pending');
  assert.equal(sendState.patchDelta, 0, 'server workspace rejection clears locally without a PATCH loop');
  assert.equal(sendState.cached, null);
  assert.equal(sendState.displayedId, null);

  await evaluate(`(() => {
    window.__workspaceSessions.setSessionWorkspaceId('chat-b', 'workspace-b');
    const serverRow = window.__sessionRows.find(row => row.id === 'chat-b');
    if (serverRow) serverRow.workspace_id = 'workspace-b';
  })()`);
  await select('chat-a');
  const backgroundPatchCount = await evaluate('window.__workspacePatches.length');
  await evaluate(`(() => {
    window.__deferWorkspaceRejection = true;
    document.getElementById('message').value = 'background workspace rejection';
    window.__backgroundWorkspaceSend = window.chatModule.handleChatSubmit({ preventDefault() {} });
    return true;
  })()`);
  await waitFor('window.__workspaceRejectionController', 'background chat stream');
  await select('chat-b');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:'/allowed/b', workspaceId:'workspace-b', pillVisible:true, pillName:'b',
  }, 'foreground chat restored before the background rejection arrives');
  await evaluate(`(() => {
    const bytes = new TextEncoder().encode('data: {"type":"workspace_rejected","data":{}}\\n\\ndata: [DONE]\\n\\n');
    window.__workspaceRejectionController.enqueue(bytes);
    window.__workspaceRejectionController.close();
    return true;
  })()`);
  await evaluate('window.__backgroundWorkspaceSend');
  assert.deepEqual(await display(), {
    sessionId:'chat-b', path:'/allowed/b', workspaceId:'workspace-b', pillVisible:true, pillName:'b',
  }, 'a background chat rejection cannot clear the foreground chat Workspace');
  assert.equal(await evaluate("window.__workspaceSessions.getSessions().find(row => row.id === 'chat-a').workspace_id"), null);
  assert.equal(await evaluate('window.__workspacePatches.length'), backgroundPatchCount,
    'background rejection also clears without PATCHing');

  const result = await evaluate(`({
    patches:window.__workspacePatches,
    create:window.__sessionCreates.at(-1),
    send:window.__chatSends.at(-1),
    paints:window.__workspacePaints,
    errors:window.__workspaceErrors,
  })`);
  assert.deepEqual(result.errors, [], 'workspace session flows produce no browser errors');
  process.stdout.write(JSON.stringify({
    restored:['chat-a:workspace-a', 'chat-b:workspace-b'],
    revoked:'local-clear-without-patch',
    navigationRace:'latest-chat-wins',
    pendingWorkspace:pendingState.create.workspace_id,
    chatWorkspaceFields:Object.keys(sendState.body).filter(key => key.startsWith('workspace')),
    patchTargets:result.patches.map(row => row.sessionId),
  }) + '\n');
} finally {
  if (socket) socket.close();
  const exited = new Promise(resolve => chromium.once('exit', resolve));
  chromium.kill('SIGTERM');
  await Promise.race([exited, new Promise(resolve => setTimeout(resolve, 3000))]);
  if (chromium.exitCode === null) chromium.kill('SIGKILL');
  fs.rmSync(profile, { recursive:true, force:true });
}
