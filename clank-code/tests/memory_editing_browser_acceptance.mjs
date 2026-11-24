#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const bootstrap = path.join(repo, 'scripts', 'openclank_bootstrap.py');
const resolverPython = process.env.OPEN_CLANK_RUNTIME_RESOLVER_PYTHON
  || path.join(repo, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const runtimeProbe = spawnSync(resolverPython, [bootstrap, 'runtime', '--repo-root', repo, '--browser'], {
  cwd: repo,
  encoding: 'utf8',
  timeout: 15_000,
});
assert.equal(runtimeProbe.status, 0, `runtime resolver failed:\n${runtimeProbe.stdout}\n${runtimeProbe.stderr}`);
const runtime = JSON.parse(`${runtimeProbe.stdout || ''}`.trim().split('\n').at(-1) || '{}');
assert.equal(runtime.ok, true, 'runtime resolver did not admit the browser');
const chromiumPath = runtime.browser.path;
const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-memory-editing-'));
const chromium = spawn(chromiumPath, [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--remote-allow-origins=*',
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${profile}`,
  'about:blank',
], { detached: true, stdio: 'ignore' });

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

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor("document.readyState === 'complete'");

  const initial = await evaluate(`(async () => {
    document.body.innerHTML = \`
      <input id="new-memory-input">
      <select id="new-memory-category"><option value="fact">fact</option></select>
      <div id="toast"></div>
      <div id="memory-list"></div><span id="memory-count-h2"></span>
      <div id="memory-categories"></div><span id="memory-provider-status"></span>
      <select id="memory-inspect-tier"><option value="curated">curated</option></select>
      <select id="memory-inspect-status"></select>
      <div id="memory-inspect-list"></div><div id="memory-quality"></div>
      <pre id="memory-digest-trusted"></pre><pre id="memory-digest-untrusted"></pre>
      <pre id="memory-digest-counts"></pre><div id="memory-digest-clusters"></div>
      <div id="memory-digest-edit-list"></div>
      <button id="memory-import-btn">import</button>
      <input id="memory-import-file" type="file">
      <div id="memory-modal" class="hidden"></div>
      <div id="memory-suggestions-body" class="hidden"></div>
      <button class="memory-tab" data-memory-tab="browse">browse</button>
    \`;
    const requests = [];
    window.__memoryRequests = requests;
    window.fetch = async (input, options = {}) => {
      const url = String(input);
      const method = options.method || 'GET';
      const body = typeof options.body === 'string' || options.body instanceof URLSearchParams
        ? String(options.body)
        : null;
      requests.push({ url, method, body });
      const json = value => new Response(JSON.stringify(value), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
      if (url.includes('/api/memory/inspect')) {
        return json({ items: [{
          id: 'curated-1',
          content: 'Inspect me',
          category: 'fact',
          owner: 'e',
          workspace_id: 'global',
        }] });
      }
      if (url.includes('/api/memory/quality')) {
        return json({ raw: 1, candidates: 1, curated: 1, quarantined: 0, graph: { integrity_ok: true } });
      }
      if (url.includes('/api/memory/digest-preview')) {
        return json({
          trusted_block: 'trusted',
          untrusted_card: 'reference',
          digest: {
            pinned: [{ id: 'pinned-1', content: 'Digest me', category: 'goal' }],
            open_questions: [{ id: 'question-1', content: 'Answer me' }],
            counts: {},
            clusters: [],
          },
        });
      }
      if (url.endsWith('/api/memory/import-batches')) {
        return json({
          batch_id: 'batch-editing-test',
          items: [{
            item_id: 'item-editing-test',
            filename: 'notes.md',
            state: 'awaiting_review',
            result: {
              suggestions: [{
                suggestion_id: 'suggestion-editing-test',
                text: 'Imported fact',
                category: 'fact',
              }],
            },
          }],
        });
      }
      if (url.includes('/api/memory/import-batches/') && url.endsWith('/review')) {
        return json({ review: { state: 'accepted' } });
      }
      if (url.includes('/api/memory?')) return json({ memory: [], provider: 'frankenmemory' });
      if (url.includes('/api/prefs')) return json({});
      return json({ ok: true, pending_review: false });
    };
    const memory = await import('/static/js/memory.js?editing-browser-acceptance=${Date.now()}');
    window.__memoryModule = memory;
    document.dispatchEvent(new Event('DOMContentLoaded'));
    await memory.loadMemories();
    await memory.loadMemoryInspect();
    await memory.loadDigestPreview();
    return {
      inspect: document.getElementById('memory-inspect-list').textContent,
      digest: document.getElementById('memory-digest-edit-list').textContent,
    };
  })()`);
  assert.match(initial.inspect, /Inspect me.*Edit/s);
  assert.match(initial.digest, /Digest me.*Edit.*Answer me.*Edit/s);

  const added = await evaluate(`(async () => {
    const input = document.getElementById('new-memory-input');
    const category = document.getElementById('new-memory-category');
    input.value = 'Which memory question is still open?';
    category.value = 'unknown';
    await window.__memoryModule.addNewMemory();
    const request = window.__memoryRequests.find(item => {
      if (item.method !== 'POST' || !item.url.includes('/api/memory/add')) return false;
      const body = JSON.parse(item.body);
      return body.text === 'Which memory question is still open?' && body.category === 'unknown';
    });
    return {
      requestSent: Boolean(request),
      inputCleared: input.value === '',
      errorToast: document.getElementById('toast').classList.contains('error'),
    };
  })()`);
  assert.deepEqual(added, {
    requestSent: true,
    inputCleared: true,
    errorToast: false,
  });

  await evaluate(`(() => {
    const card = document.querySelector('#memory-inspect-list .memory-item');
    card.querySelector('button').click();
    card.querySelector('textarea').value = 'Inspect edited';
    card.querySelector('select').value = 'project';
    card.querySelector('.save').click();
  })()`);
  await waitFor(`window.__memoryRequests.some(
    request => request.method === 'PUT' && request.url.includes('/api/memory/curated-1')
      && request.body.includes('text=Inspect+edited') && request.body.includes('category=project')
  )`);

  await evaluate(`(() => {
    const card = document.querySelector('#memory-digest-edit-list .memory-item');
    card.querySelector('button').click();
    card.querySelector('textarea').value = 'Digest edited';
    card.querySelector('.save').click();
  })()`);
  await waitFor(`window.__memoryRequests.some(
    request => request.method === 'PUT' && request.url.includes('/api/memory/pinned-1')
      && request.body.includes('text=Digest+edited')
  )`);

  await evaluate(`(() => {
    const file = new File(['hello'], 'notes.md', { type: 'text/markdown' });
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const input = document.getElementById('memory-import-file');
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor(`document.querySelector('[aria-label^="Imported memory text"]')`);
  await evaluate(`(() => {
    const text = document.querySelector('[aria-label^="Imported memory text"]');
    const category = document.querySelector('[aria-label^="Imported memory category"]');
    text.value = 'Imported edited';
    text.dispatchEvent(new Event('input', { bubbles: true }));
    category.value = 'preference';
    category.dispatchEvent(new Event('change', { bubbles: true }));
    document.querySelector('.memory-suggestion-item .save').click();
  })()`);
  await waitFor(`window.__memoryRequests.some(request => {
    if (request.method !== 'POST' || !request.url.endsWith('/review')) return false;
    const body = JSON.parse(request.body);
    return body.proposal?.text === 'Imported edited'
      && body.proposal?.category === 'preference';
  })`);

  console.log('memory editing browser acceptance passed');
} finally {
  if (socket?.readyState === WebSocket.OPEN) socket.close();
  if (chromium?.pid && chromium.exitCode === null) {
    try { process.kill(-chromium.pid, 'SIGTERM'); } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  if (chromium?.pid && chromium.exitCode === null) {
    try { process.kill(-chromium.pid, 'SIGKILL'); } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
  fs.rmSync(profile, {
    recursive: true,
    force: true,
    maxRetries: 5,
    retryDelay: 100,
  });
}
