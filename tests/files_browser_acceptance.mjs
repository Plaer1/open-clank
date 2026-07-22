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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-files-'));
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
    if (!message.id) return;
    const request = pending.get(message.id);
    if (!request) return;
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
    window.__fileRequests = [];
    window.confirm = () => true;
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      const method = options.method || 'GET';
      window.__fileRequests.push({ url, method, body: options.body || '' });
      if (url.includes('/api/documents/library')) return new Response(JSON.stringify({ documents: [], total: 0, languages: {}, session_count: 0 }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/api/files/library')) return new Response(JSON.stringify({ files: [{ id: 'f'.repeat(32), kind: 'published', filename: 'clänk pack.zip', mime_type: 'application/zip', size: 4096, source: 'agent', created_at: new Date().toISOString(), active_grant_count: 1, audiences: ['owner'], next_expiry: null }], total: 1 }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/grants')) return new Response(JSON.stringify({ download_url: '/api/files/download/new-token', audience: 'public' }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      if (url.includes('/revoke') || method === 'DELETE') return new Response(JSON.stringify({ revoked: 1, deleted: true }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      return new Response('{}', { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    const library = await import('/static/js/documentLibrary.js?files-acceptance=1');
    const noop = () => {};
    library.initLibrary({ apiBase: '', esc: value => String(value).replace(/[&<>"']/g, c => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c])), getDocs: () => new Map(), isOpen: () => false, createDocument: noop, loadDocument: noop, switchToDoc: noop, openPanel: noop, addDocToTabs: noop, syncDocIndicator: noop });
    library.openLibrary();
  })()`);
  await waitFor("document.querySelector('.doclib-published-file')", 'published file card');

  const initial = await evaluate(`(() => ({
    tab: document.querySelector('[data-doclib-tab="documents"]').textContent.trim(),
    title: document.querySelector('[data-doclib-panel="documents"] h2').textContent.trim(),
    filename: document.querySelector('.doclib-published-file .memory-item-title').textContent.trim(),
    meta: document.querySelector('.doclib-published-file .memory-item-meta').textContent,
    actionsLabel: document.querySelector('.doclib-published-file .memory-item-btn').getAttribute('aria-label'),
  }))()`);
  assert.equal(initial.tab, 'Files');
  assert.match(initial.title, /^Files/);
  assert.equal(initial.filename, 'clänk pack.zip');
  assert.match(initial.meta, /4\.0 KB · owner/);
  assert.match(initial.actionsLabel, /clänk pack\.zip/);

  await evaluate("document.querySelector('.doclib-published-file .memory-item-btn').click()");
  const actions = await evaluate("[...document.querySelectorAll('._lib-dd .dropdown-item-compact span:last-child')].map(node => node.textContent)");
  assert.deepEqual(actions.slice(0, 5), ['Download', 'Copy owner link', 'Copy public link', 'Break links', 'Delete']);
  await evaluate("[...document.querySelectorAll('._lib-dd .dropdown-item-compact')].find(node => node.textContent.includes('Break links')).click()");
  await waitFor("window.__fileRequests.some(request => request.url.endsWith('/revoke') && request.method === 'POST')", 'revoke request');

  await evaluate("document.querySelector('.doclib-published-file .memory-item-btn').click()");
  await evaluate("[...document.querySelectorAll('._lib-dd .dropdown-item-compact')].find(node => node.textContent.includes('Delete')).click()");
  await waitFor("window.__fileRequests.some(request => request.url.endsWith('/api/files/' + 'f'.repeat(32)) && request.method === 'DELETE')", 'delete request');

  process.stdout.write(JSON.stringify({ initial, actions, lifecycle: 'pass' }) + '\n');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive: true, force: true });
}
