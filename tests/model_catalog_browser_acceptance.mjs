#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7000').replace(/\/$/, '');
const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-models-'));
const chromium = spawn('/usr/bin/chromium', [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let targets;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      break;
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
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor("document.readyState === 'complete'", 'login origin');
  await evaluate(`(async () => {
    document.body.innerHTML = '<main><div id="models"></div></main>';
    globalThis.__openClankAuthenticatedUser = 'browser-test';
    const favoriteKey = 'odysseus-model-favorites:scope:browser-test';
    localStorage.setItem(favoriteKey, JSON.stringify(['shared-model']));
    window.__catalogItems = [
      { endpoint_id:'endpoint-a', endpoint_name:'Same name', url:'https://a.invalid/v1/chat/completions', category:'api', models:['shared-model','only-a'] },
      { endpoint_id:'endpoint-b', endpoint_name:'Same name', url:'https://b.invalid/v1/chat/completions', category:'api', models:['shared-model','only-b'] },
    ];
    window.__pickerPatch = null;
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      if (url.includes('/api/models')) {
        return new Response(JSON.stringify({ items: window.__catalogItems }), {
          status:200, headers:{'Content-Type':'application/json'},
        });
      }
      if (options.method === 'PATCH' && url.includes('/api/session/')) {
        const fields = Object.fromEntries(options.body.entries());
        window.__pickerPatch = fields;
        return new Response(JSON.stringify({
          id: 'session-1',
          model: fields.model,
          endpoint_url: fields.endpoint_url,
          endpoint_id: fields.endpoint_id,
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    const models = await import('/static/js/models.js?catalog-identity=1');
    models.init('');
    await models.refreshModels(true);
    window.__catalogModels = models;
    window.modelsModule = models;
  })()`);
  await waitFor("document.querySelectorAll('.models-row').length === 4", 'four unique endpoint-model choices');

  const state = await evaluate(`(() => ({
    rowKeys: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelId),
    mids: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelMid),
    endpointLabels: [...document.querySelectorAll('.models-endpoint-label span:nth-child(2)')].map(node => node.textContent),
    favoriteRows: document.querySelectorAll('.models-category-header + .models-group-content .models-row').length,
    storedFavorites: JSON.parse(localStorage.getItem('odysseus-model-favorites:scope:browser-test') || '[]'),
  }))()`);
  assert.equal(new Set(state.rowKeys).size, 4, 'one row per endpoint + model identity');
  assert.equal(state.mids.filter(mid => mid === 'shared-model').length, 2, 'distinct routes remain selectable');
  assert.deepEqual(state.endpointLabels, ['Same name', 'Same name'], 'same labels do not merge endpoint groups');
  assert.equal(state.favoriteRows, 1, 'favorite choice is not repeated in its source group');
  assert.match(state.storedFavorites[0], /^endpoint:/, 'legacy favorite migrated to a route-aware key');

  await evaluate(`(async () => {
    document.body.innerHTML = \`
      <div id="model-picker-wrap">
        <button id="model-picker-btn"><span id="model-picker-label">Select model</span></button>
        <div id="model-picker-menu" class="hidden">
          <div class="model-picker-search-row">
            <input id="model-picker-search">
            <button id="model-picker-refresh-btn"></button>
          </div>
          <div id="model-picker-list"></div>
        </div>
      </div>
      <textarea id="message"></textarea>
      <button id="scroll-bottom-btn"></button>
    \`;
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    const personal = {
      endpoint_id:'mimo:xiaomi', endpoint_name:'Xiaomi', url:'mimo://acp',
      category:'api', models:[mid],
    };
    const shared = {
      endpoint_id:'shared:grant', endpoint_name:'MiMo shared', url:'mimo://acp',
      category:'api', models:[mid], shared:true, shared_by:'e',
    };
    window.__catalogItems = [personal, shared];
    await window.__catalogModels.refreshModels(true);
    const picker = await import('/static/js/modelPicker.js?route-sync=1');
    const sessions = [{
      id:'session-1', name:'Shared chat', model:mid,
      endpoint_url:'mimo://acp', endpoint_id:'shared:grant',
    }];
    window.__pickerSessions = sessions;
    window.__pickerCurrentSessionId = 'session-1';
    window.__pickerPending = null;
    window.__modelPicker = picker;
    picker.initModelPicker({
      getCurrentSessionId: () => window.__pickerCurrentSessionId,
      getSessions: () => sessions,
      getPendingChat: () => window.__pickerPending,
      setPendingChat: value => { window.__pickerPending = value; },
      createDirectChat: (url, modelId, endpointId) => {
        window.__pickerPending = { url, modelId, endpointId, source:'manual' };
      },
    });
    picker.updateModelPicker();
  })()`);
  await waitFor(
    "document.getElementById('model-picker-label').textContent.includes('mimo-v2.5-pro')",
    'shared session picker restoration',
  );

  await evaluate(`(async () => {
    window.__catalogItems = window.__catalogItems.filter(item => item.endpoint_id !== 'shared:grant');
    await window.__catalogModels.refreshModels(true);
  })()`);
  await waitFor(
    "document.getElementById('model-picker-label').textContent === 'Select model'",
    'revoked shared route no longer impersonated by personal route',
  );

  const revokedState = await evaluate(`(() => {
    document.getElementById('model-picker-btn').click();
    return {
      rows: document.querySelectorAll('.model-switch-item').length,
      endpoints: [...document.querySelectorAll('.model-switch-ep')].map(node => node.textContent),
    };
  })()`);
  assert.equal(revokedState.rows, 1, 'personal same-model route remains after shared route removal');
  assert.deepEqual(revokedState.endpoints, ['Xiaomi']);

  await evaluate(`(async () => {
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    window.__catalogItems.push({
      endpoint_id:'shared:grant', endpoint_name:'MiMo shared', url:'mimo://acp',
      category:'api', models:[mid], shared:true, shared_by:'e',
    });
    await window.__catalogModels.refreshModels(true);
    const button = document.getElementById('model-picker-btn');
    if (document.getElementById('model-picker-menu').classList.contains('hidden')) button.click();
    const sharedRow = [...document.querySelectorAll('.model-switch-item')]
      .find(row => row.querySelector('.model-switch-ep')?.textContent.includes('Shared by e'));
    if (!sharedRow) throw new Error('shared picker row missing');
    sharedRow.click();
  })()`);
  await waitFor("window.__pickerPatch?.endpoint_id === 'shared:grant'", 'route-aware picker PATCH');
  const pickerState = await evaluate(`(() => ({
    patch: window.__pickerPatch,
    session: window.__pickerSessions[0],
    label: document.getElementById('model-picker-label').textContent,
  }))()`);
  assert.equal(pickerState.patch.model, 'xiaomi/mimo-v2.5-pro/high');
  assert.equal(pickerState.patch.endpoint_url, 'mimo://acp');
  assert.equal(pickerState.session.endpoint_id, 'shared:grant');
  assert.equal(pickerState.session.model, 'xiaomi/mimo-v2.5-pro/high');
  assert.match(pickerState.label, /mimo-v2\.5-pro/);

  const raceState = await evaluate(`(async () => {
    const pending = [];
    window.fetch = async input => {
      if (!String(input).includes('/api/models')) {
        return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
      }
      return new Promise(resolve => pending.push(resolve));
    };
    const older = window.__catalogModels.refreshModels(true);
    const newer = window.__catalogModels.refreshModels(true);
    pending[1](new Response(JSON.stringify({
      items:[{ endpoint_id:'newer', url:'https://newer.invalid/chat', models:['newer-model'] }],
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await newer;
    pending[0](new Response(JSON.stringify({
      items:[{ endpoint_id:'older', url:'https://older.invalid/chat', models:['older-model'] }],
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await older;
    return window.__catalogModels.getCachedItems().map(item => item.endpoint_id);
  })()`);
  assert.deepEqual(raceState, ['newer'], 'an older forced refresh cannot overwrite the winning catalogue');

  const defaultRaceState = await evaluate(`(async () => {
    let resolveDefault;
    window.__pickerCurrentSessionId = null;
    window.__pickerSessions.length = 0;
    window.__pickerPending = null;
    window.fetch = async input => {
      if (String(input).includes('/api/default-chat')) {
        return new Promise(resolve => { resolveDefault = resolve; });
      }
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    window.__modelPicker.updateModelPicker();
    while (!resolveDefault) await new Promise(resolve => setTimeout(resolve, 0));
    window.__pickerPending = {
      url:'https://newer.invalid/chat', modelId:'newer-model',
      endpointId:'newer', source:'manual',
    };
    resolveDefault(new Response(JSON.stringify({
      endpoint_url:'https://default.invalid/chat',
      endpoint_id:'default',
      model:'default-model',
    }), { status:200, headers:{'Content-Type':'application/json'} }));
    await new Promise(resolve => setTimeout(resolve, 0));
    await new Promise(resolve => setTimeout(resolve, 0));
    return window.__pickerPending;
  })()`);
  assert.equal(defaultRaceState.endpointId, 'newer');
  assert.equal(defaultRaceState.modelId, 'newer-model', 'an in-flight default cannot overwrite a manual pick');

  const pendingState = await evaluate(`(async () => {
    const mid = 'xiaomi/mimo-v2.5-pro/high';
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      if (url.includes('/api/models')) {
        return new Response(JSON.stringify({ items:[{
          endpoint_id:'shared:grant', endpoint_name:'MiMo shared',
          url:'mimo://acp', category:'api', models:[mid], shared:true, shared_by:'e',
        }] }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      if (options.method === 'POST' && url.includes('/api/session')) {
        return new Response(JSON.stringify({
          id:'created-session', name:'New Chat', model:mid, rag:false, archived:false,
        }), { status:200, headers:{'Content-Type':'application/json'} });
      }
      if (url.includes('/api/sessions')) return new Promise(() => {});
      return new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    };
    await window.__catalogModels.refreshModels(true);
    const sessionsModule = await import('/static/js/sessions.js?pending-route-sync=1');
    sessionsModule.createDirectChat('mimo://acp', mid, 'shared:grant');
    const before = sessionsModule.getCurrentModel();
    const ok = await sessionsModule.materializePendingSession();
    const created = sessionsModule.getSessions().find(item => item.id === 'created-session');
    return { before, ok, created };
  })()`);
  assert.equal(pendingState.before, 'xiaomi/mimo-v2.5-pro/high');
  assert.equal(pendingState.ok, true);
  assert.equal(pendingState.created.endpoint_id, 'shared:grant');
  assert.equal(pendingState.created.endpoint_url, 'mimo://acp');
  assert.equal(pendingState.created.model, 'xiaomi/mimo-v2.5-pro/high');
  process.stdout.write(JSON.stringify(state) + '\n');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive: true, force: true });
}
