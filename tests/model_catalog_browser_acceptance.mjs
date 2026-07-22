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
    localStorage.setItem('odysseus-model-favorites', JSON.stringify(['shared-model']));
    const items = [
      { endpoint_id:'endpoint-a', endpoint_name:'Same name', url:'https://a.invalid/v1/chat/completions', category:'api', models:['shared-model','only-a'] },
      { endpoint_id:'endpoint-b', endpoint_name:'Same name', url:'https://b.invalid/v1/chat/completions', category:'api', models:['shared-model','only-b'] },
    ];
    window.fetch = async input => String(input).includes('/api/models')
      ? new Response(JSON.stringify({ items }), { status:200, headers:{'Content-Type':'application/json'} })
      : new Response('{}', { status:200, headers:{'Content-Type':'application/json'} });
    const models = await import('/static/js/models.js?catalog-identity=1');
    models.init('');
    await models.refreshModels(true);
  })()`);
  await waitFor("document.querySelectorAll('.models-row').length === 4", 'four unique endpoint-model choices');

  const state = await evaluate(`(() => ({
    rowKeys: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelId),
    mids: [...document.querySelectorAll('.models-row')].map(row => row.dataset.modelMid),
    endpointLabels: [...document.querySelectorAll('.models-endpoint-label span:nth-child(2)')].map(node => node.textContent),
    favoriteRows: document.querySelectorAll('.models-category-header + .models-group-content .models-row').length,
    storedFavorites: JSON.parse(localStorage.getItem('odysseus-model-favorites') || '[]'),
  }))()`);
  assert.equal(new Set(state.rowKeys).size, 4, 'one row per endpoint + model identity');
  assert.equal(state.mids.filter(mid => mid === 'shared-model').length, 2, 'distinct routes remain selectable');
  assert.deepEqual(state.endpointLabels, ['Same name', 'Same name'], 'same labels do not merge endpoint groups');
  assert.equal(state.favoriteRows, 1, 'favorite choice is not repeated in its source group');
  assert.match(state.storedFavorites[0], /^endpoint:/, 'legacy favorite migrated to a route-aware key');
  process.stdout.write(JSON.stringify(state) + '\n');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive: true, force: true });
}
