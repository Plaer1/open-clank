#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-goals-'));
const chromium = spawn('/usr/bin/chromium', [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let targets;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      break;
    } catch {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
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
    const response = await command('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (response.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    }
    return response.result.value;
  };
  const waitFor = async expression => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    throw new Error(`Timed out waiting for: ${expression}`);
  };
  const pressEnter = async () => {
    const key = {
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    };
    await command('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...key });
    await command('Input.dispatchKeyEvent', { type: 'char', text: '\r', unmodifiedText: '\r', ...key });
    await command('Input.dispatchKeyEvent', { type: 'keyUp', ...key });
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (await evaluate("document.readyState === 'complete'")) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }

  const setup = await evaluate(`(async () => {
    const html = await fetch('/static/index.html').then(response => response.text());
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    document.body.replaceChildren(
      parsed.getElementById('goal-panel-btn'),
      parsed.getElementById('goal-dialog'),
    );
    const requests = [];
    let envelopeRevision = 7;
    let active = {
      id:'goal-1',
      objective:'Ship it',
      revision:3,
      status:'active',
      budget:{ maxTurns:3, usedTurns:1, maxTokens:5000, usedTokens:900, usedToolCalls:2 },
      requiredEvidence:['command'],
      evidence:[{ kind:'command', subject:'tests', sourceRef:'tool:test' }],
    };
    let history = [{
      id:'goal-old',
      objective:'Prepare release',
      revision:5,
      status:'completed',
      budget:{ usedTurns:2, usedTokens:600, usedToolCalls:1 },
      requiredEvidence:[],
      evidence:[],
      lastOutcome:{ code:'verified_completion' },
    }];
    let analytics = { verified_completion:1, cancelled:2 };
    window.sessionModule = {
      getCurrentSessionId: () => 'chat-1',
      getSessions: () => [{ id:'chat-1', endpoint_url:'mimo://acp' }],
    };
    window.confirm = () => true;
    window.__goalRequests = requests;
    window.fetch = async (url, options = {}) => {
      requests.push({ url:String(url), method:options.method || 'GET', body:options.body || null });
      const body = options.body ? JSON.parse(options.body) : null;
      if (body?.action === 'edit') {
        active = { ...active, objective:body.objective, revision:active.revision + 1 };
        envelopeRevision += 1;
      }
      if (body?.action === 'pause') {
        active = { ...active, revision:active.revision + 1, status:'paused' };
        envelopeRevision += 1;
      }
      if (body?.action === 'resume') {
        active = { ...active, revision:active.revision + 1, status:'active' };
        envelopeRevision += 1;
      }
      if (body?.action === 'verify') {
        active = {
          ...active,
          revision:active.revision + 1,
          lastOutcome:{ code:'verified_completion', reason:'focused checks passed' },
        };
        analytics = { ...analytics, verified_completion:analytics.verified_completion + 1 };
        envelopeRevision += 1;
      }
      if (body?.action === 'cancel') {
        history = [
          ...history,
          {
            ...active,
            revision:active.revision + 1,
            status:'cancelled',
            lastOutcome:{ code:'cancelled' },
          },
        ];
        active = null;
        analytics = { ...analytics, cancelled:analytics.cancelled + 1 };
        envelopeRevision += 1;
      }
      if (body?.action === 'create') {
        active = {
          id:'goal-new',
          objective:body.objective,
          revision:1,
          status:'active',
          budget:{ maxTurns:12, usedTurns:0, usedTokens:0, usedToolCalls:0 },
          requiredEvidence:[],
          evidence:[],
        };
        envelopeRevision += 1;
      }
      if (body?.action === 'clear_history') {
        history = [];
        envelopeRevision += 1;
      }
      return new Response(JSON.stringify({
        state: {
          revision:envelopeRevision,
          active,
          queue: [{ id:'goal-2', objective:'Write notes', revision:1, status:'queued' }],
          history,
        },
        analytics,
      }), { status:200, headers:{'Content-Type':'application/json'} });
    };
    await import('/static/js/goals.js?browser-acceptance=1');
    document.dispatchEvent(new CustomEvent('odysseus:session-selected', {
      detail: { sessionId:'chat-1' },
    }));
    const open = document.getElementById('goal-panel-btn');
    open.focus();
    return {
      hidden: open.hidden,
      focused: document.activeElement === open,
    };
  })()`);

  assert.deepEqual(setup, { hidden:false, focused:true });
  await pressEnter();
  await waitFor("document.getElementById('goal-dialog').open && document.getElementById('goal-active').textContent.includes('Ship it')");
  const before = await evaluate(`(() => {
    const dialog = document.getElementById('goal-dialog');
    return {
      active:document.getElementById('goal-active').textContent,
      queue:document.getElementById('goal-queue').textContent,
      history:document.getElementById('goal-history').textContent,
      analytics:document.getElementById('goal-analytics').textContent,
      verifyLabel:dialog.querySelector('[data-goal-action="verify"]')?.getAttribute('aria-label'),
      editLabel:dialog.querySelector('[aria-label="Edit active goal objective"]')?.textContent,
    };
  })()`);
  assert.match(before.active, /Ship it/);
  assert.match(before.active, /turns 1 \/ 3/);
  assert.match(before.active, /Required evidence: command · attached 1/);
  assert.match(before.queue, /Write notes/);
  assert.match(before.history, /Prepare release/);
  assert.match(before.analytics, /verified completion1/);
  assert.equal(before.verifyLabel, 'Verify now active goal');
  assert.equal(before.editLabel, 'Edit objective');

  await evaluate(`document.querySelector('[aria-label="Edit active goal objective"]').focus()`);
  await pressEnter();
  await waitFor("!document.querySelector('.goal-edit-form').hidden");
  await evaluate(`(() => {
    const input = document.querySelector('[aria-label="Goal objective"]');
    input.value = 'Ship verified release';
    input.focus();
  })()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('Ship verified release')");

  await evaluate(`document.querySelector('[data-goal-action="pause"]').focus()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('paused')");

  await evaluate(`document.querySelector('[data-goal-action="resume"]').focus()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('active · revision 6')");

  await evaluate(`document.querySelector('[data-goal-action="verify"]').focus()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('focused checks passed')");

  await evaluate(`document.querySelector('[data-goal-action="cancel"]').focus()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('No active goal')");

  await evaluate(`(() => {
    const input = document.getElementById('goal-objective');
    input.value = 'Document keyboard controls';
    document.querySelector('#goal-create-form button[type="submit"]').focus();
  })()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-active').textContent.includes('Document keyboard controls')");

  await evaluate(`document.getElementById('goal-clear-history').focus()`);
  await pressEnter();
  await waitFor("document.getElementById('goal-history').textContent.includes('No completed goals')");

  const result = await evaluate(`(() => ({
    active:document.getElementById('goal-active').textContent,
    history:document.getElementById('goal-history').textContent,
    analytics:document.getElementById('goal-analytics').textContent,
    requests:window.__goalRequests,
  }))()`);
  assert.match(result.active, /Document keyboard controls/);
  assert.match(result.history, /No completed goals/);
  assert.match(result.analytics, /verified completion2/);
  assert.match(result.analytics, /cancelled3/);
  assert.ok(result.requests.some(request => request.method === 'GET'), 'goal list GET was not exercised');
  const mutations = result.requests
    .filter(request => request.method === 'POST')
    .map(request => JSON.parse(request.body));
  assert.deepEqual(mutations, [{
    action: 'edit',
    target: { goalID: 'goal-1', expectedRevision: 3 },
    objective: 'Ship verified release',
  }, {
    action: 'pause',
    target: { goalID: 'goal-1', expectedRevision: 4 },
  }, {
    action: 'resume',
    target: { goalID: 'goal-1', expectedRevision: 5 },
  }, {
    action: 'verify',
    target: { goalID: 'goal-1', expectedRevision: 6 },
  }, {
    action: 'cancel',
    target: { goalID: 'goal-1', expectedRevision: 7 },
  }, {
    action: 'create',
    objective: 'Document keyboard controls',
  }, {
    action: 'clear_history',
    expectedEnvelopeRevision: 13,
  }]);
  console.log('goal web surface browser acceptance passed');
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  chromium.kill('SIGTERM');
  fs.rmSync(profile, { recursive: true, force: true });
}
