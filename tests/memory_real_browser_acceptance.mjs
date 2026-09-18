#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const fmDir = path.join(repo, 'mcp_servers', 'frankenmemory');
const bootstrap = path.join(repo, 'scripts', 'openclank_bootstrap.py');

function resolveRuntime() {
  const candidates = [
    process.env.OPEN_CLANK_RUNTIME_RESOLVER_PYTHON,
    process.env.PYTHON,
    process.platform === 'win32' ? 'python' : 'python3',
    process.platform === 'win32' ? 'py' : 'python',
    path.join(repo, 'venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
    path.join(repo, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python'),
  ].filter(Boolean);
  const diagnostics = [];
  for (const candidate of candidates) {
    const result = spawnSync(candidate, [bootstrap, 'runtime', '--repo-root', repo, '--browser'], {
      cwd: repo,
      encoding: 'utf8',
      timeout: 15_000,
    });
    const output = `${result.stdout || ''}`.trim();
    if (result.status === 0) {
      try {
        const report = JSON.parse(output.split('\n').at(-1) || '');
        if (report.ok) return report;
      } catch {
        diagnostics.push(`${candidate}: invalid resolver output`);
        continue;
      }
    }
    diagnostics.push(`${candidate}: ${result.error?.message || output.slice(-240) || `exit ${result.status}`}`);
  }
  throw new Error(`runtime resolver failed before browser startup:\n${diagnostics.join('\n')}`);
}

const runtime = resolveRuntime();
const fmBinary = runtime.fm_mcp.path;
const python = runtime.python.path;
const chromium = runtime.browser.path;

if (process.env.OPEN_CLANK_ACCEPTANCE_BUILD === '1') {
  const build = spawnSync('cargo', ['build', '--release', '-p', 'fm-mcp'], {
    cwd: fmDir,
    encoding: 'utf8',
    timeout: 300_000,
  });
  assert.equal(build.status, 0, `current-source fm-mcp build failed:\n${build.stdout}\n${build.stderr}`);
}
assert.ok(fs.existsSync(fmBinary), `fm-mcp was not built at ${fmBinary}`);

const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});

const [appPort, debugPort] = await Promise.all([freePort(), freePort()]);
const base = `http://127.0.0.1:${appPort}`;
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-real-memory-data-'));
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-real-memory-chromium-'));

const server = spawn(python, [
  'tests/real_memory_browser_server.py',
  '--port', String(appPort),
  '--data-dir', dataDir,
  '--fm-binary', fmBinary,
], {
  cwd: repo,
  detached: true,
  env: {
    ...process.env,
    DATABASE_URL: `sqlite:///${path.join(dataDir, 'app.db')}`,
  },
  stdio: ['ignore', 'pipe', 'pipe'],
});
let serverOutput = '';
server.stdout.on('data', chunk => { serverOutput += chunk; });
server.stderr.on('data', chunk => { serverOutput += chunk; });

const browserProcess = spawn(chromium, [
  '--headless=new',
  '--no-sandbox',
  '--disable-gpu',
  '--disable-backgrounding-occluded-windows',
  '--disable-renderer-backgrounding',
  '--disable-background-timer-throttling',
  '--disable-features=CalculateNativeWinOcclusion',
  '--window-size=1440,900',
  '--remote-allow-origins=*',
  `--remote-debugging-port=${debugPort}`,
  `--user-data-dir=${profile}`,
  `${base}/memory`,
], { detached: true, stdio: 'ignore' });

const terminateGroup = async child => {
  if (!child || !Number.isInteger(child.pid) || child.pid <= 0) {
    if (child && child.exitCode === null && typeof child.kill === 'function') child.kill('SIGTERM');
    return;
  }
  const exited = new Promise(resolve => child.once('exit', resolve));
  try {
    process.kill(-child.pid, 'SIGTERM');
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
  if (child.exitCode === null) {
    await Promise.race([
      exited,
      new Promise(resolve => setTimeout(resolve, 3000)),
    ]);
  }
  try {
    process.kill(-child.pid, 'SIGKILL');
  } catch (error) {
    if (error.code !== 'ESRCH') throw error;
  }
  if (child.exitCode === null) {
    await Promise.race([
      exited,
      new Promise(resolve => setTimeout(resolve, 1000)),
    ]);
  }
};

const processRows = () => {
  const result = spawnSync('ps', ['-axo', 'pid=,ppid=,pgid=,command='], {
    encoding: 'utf8',
  });
  if (result.status !== 0) return [];
  return result.stdout.split('\n').flatMap(line => {
    const match = line.trim().match(/^(\d+)\s+(\d+)\s+(\d+)\s+(.*)$/);
    return match ? [{ pid: Number(match[1]), ppid: Number(match[2]), pgid: Number(match[3]), command: match[4] }] : [];
  });
};

const descendantRows = rootPid => {
  if (!Number.isInteger(rootPid) || rootPid <= 0) return [];
  const rows = processRows();
  const descendants = [];
  const queue = [rootPid];
  while (queue.length) {
    const parent = queue.shift();
    for (const row of rows) {
      if (row.ppid !== parent || descendants.some(item => item.pid === row.pid)) continue;
      descendants.push(row);
      queue.push(row.pid);
    }
  }
  return descendants;
};

const signalRows = (rows, signal) => {
  for (const row of [...rows].reverse()) {
    if (!/real_memory_browser_server|fm-mcp/.test(row.command)) continue;
    try { process.kill(row.pid, signal); } catch (error) {
      if (error.code !== 'ESRCH') throw error;
    }
  }
};

let cleanupPromise;
const cleanup = async () => {
  if (cleanupPromise) return cleanupPromise;
  cleanupPromise = (async () => {
    const serverChildren = descendantRows(server.pid);
    if (socket?.readyState === WebSocket.OPEN) socket.close();
    await Promise.all([terminateGroup(browserProcess), terminateGroup(server)]);
    signalRows(serverChildren, 'SIGTERM');
    await new Promise(resolve => setTimeout(resolve, 250));
    signalRows(serverChildren, 'SIGKILL');
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
    fs.rmSync(dataDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 });
  })();
  return cleanupPromise;
};

const handleSignal = code => {
  cleanup().finally(() => process.exit(code));
};
process.once('SIGINT', () => handleSignal(130));
process.once('SIGTERM', () => handleSignal(143));

let socket;
try {
  let ready = false;
  // Provider-store migrations and the isolated FM child can take longer than
  // the old hard-coded 15s window on a cold macOS checkout.
  for (let attempt = 0; attempt < 900; attempt += 1) {
    if (server.exitCode !== null) break;
    try {
      const response = await fetch(`${base}/api/memory`, {
        headers: { 'x-test-user': 'alice' },
      });
      ready = response.ok;
      if (ready) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert.ok(ready, `isolated memory server did not start:\n${serverOutput}`);

  let targets;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      targets = await fetch(`http://127.0.0.1:${debugPort}/json`).then(response => response.json());
      if (targets.some(item => item.type === 'page' && item.url.startsWith(base))) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  const target = targets?.find(item => item.type === 'page' && item.url.startsWith(base));
  assert(target?.webSocketDebuggerUrl, 'Chromium page target is unavailable');

  // Activate the exact application target. Selecting the first `page` target
  // can attach to a hidden Chrome settings/help page on developer machines.
  await fetch(`http://127.0.0.1:${debugPort}/json/activate/${target.id}`).catch(() => {});

  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });

  let sequence = 0;
  const pending = new Map();
  const requests = new Map();
  const responses = [];
  const exceptions = [];
  const consoleErrors = [];
  const lifecycleEvents = [];
  const executionContexts = [];
  const frameNavigations = [];
  const loadingFailures = [];
  const probeErrors = [];
  const timedOutCommands = [];
  let transportClosed = null;
  let importPostData = '';
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    if (message.method === 'Network.requestWillBeSent') {
      requests.set(message.params.requestId, {
        url: message.params.request.url,
        method: message.params.request.method,
        postData: message.params.request.postData || '',
      });
    }
    if (message.method === 'Network.responseReceived') {
      const request = requests.get(message.params.requestId);
      responses.push({
        requestId: message.params.requestId,
        url: message.params.response.url,
        method: request?.method,
        postData: request?.postData || '',
        status: message.params.response.status,
      });
    }
    if (message.method === 'Runtime.exceptionThrown') {
      exceptions.push(
        message.params.exceptionDetails.exception?.description
        || message.params.exceptionDetails.text,
      );
    }
    if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
      consoleErrors.push(
        message.params.args
          .map(item => item.value || item.description || item.type)
          .join(' '),
      );
    }
    if (message.method === 'Page.lifecycleEvent') lifecycleEvents.push(message);
    if (message.method === 'Runtime.executionContextCreated') executionContexts.push(message);
    if (message.method === 'Page.frameNavigated') frameNavigations.push(message);
    if (message.method === 'Network.loadingFailed') loadingFailures.push({
      requestId: message.params.requestId,
      url: requests.get(message.params.requestId)?.url || '',
      errorText: message.params.errorText,
      blockedReason: message.params.blockedReason || null,
    });
    if (!message.id || !pending.has(message.id)) return;
    const request = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(request.timer);
    message.error ? request.reject(new Error(message.error.message)) : request.resolve(message.result);
  });

  const rejectPending = error => {
    if (!transportClosed) transportClosed = error;
    for (const [id, request] of pending) {
      pending.delete(id);
      clearTimeout(request.timer);
      request.reject(error);
    }
  };
  socket.addEventListener('close', () => {
    rejectPending(new Error('CDP websocket closed'));
  }, { once: true });
  socket.addEventListener('error', () => {
    if (!transportClosed) probeErrors.push({ stage: 'cdp-transport', error: 'websocket error' });
  });

  const command = (method, params = {}, timeoutMs = 30_000) => new Promise((resolve, reject) => {
    if (transportClosed || socket.readyState !== WebSocket.OPEN) {
      reject(transportClosed || new Error('CDP websocket is not open'));
      return;
    }
    const id = ++sequence;
    const timer = setTimeout(() => {
      if (!pending.has(id)) return;
      pending.delete(id);
      const error = new Error(`${method} timed out after ${timeoutMs}ms`);
      timedOutCommands.push({ id, method, timeoutMs });
      reject(error);
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer });
    try {
      socket.send(JSON.stringify({ id, method, params }));
    } catch (error) {
      pending.delete(id);
      clearTimeout(timer);
      reject(error);
    }
  });
  const evaluate = async (expression, timeoutMs = 3_000) => {
    const response = await command('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    }, timeoutMs);
    if (response.exceptionDetails) {
      throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    }
    return response.result.value;
  };
  const waitFor = async (expression, label, timeout = 20_000) => {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      try {
        if (await evaluate(expression)) return;
      } catch (error) {
        probeErrors.push({ stage: label, error: error.message });
        if (transportClosed) throw error;
      }
      await new Promise(resolve => setTimeout(resolve, 75));
    }
    const memoryTraffic = responses.filter(item => item.url.includes('/api/memory'));
    const browserState = await evaluate(`(() => ({
      toast: document.getElementById('toast')?.textContent || '',
      suggestions: document.getElementById('memory-suggestions-body')?.textContent || '',
      session: window.sessionModule?.getCurrentSessionId?.() || null,
    }))()`).catch(error => ({ error: error.message }));
    throw new Error(
      `Timed out waiting for ${label}\n`
      + `memory traffic: ${JSON.stringify(memoryTraffic)}\n`
      + `browser state: ${JSON.stringify(browserState)}\n`
      + `runtime exceptions: ${JSON.stringify(exceptions)}\n`
      + `loading failures: ${JSON.stringify(loadingFailures)}\n`
      + `timed out commands: ${JSON.stringify(timedOutCommands)}\n`
      + `probe errors: ${JSON.stringify(probeErrors.slice(-20))}\n`
      + `server: ${serverOutput}`,
    );
  };

  const waitForNavigation = async (label, navigation, before) => {
    const frameId = navigation?.frameId || null;
    const loaderId = navigation?.loaderId || null;
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      const contextReady = executionContexts.some((event, index) => (
        index >= before.contexts
          && (!frameId || event.params?.context?.auxData?.frameId === frameId)
          && event.params?.context?.auxData?.isDefault
      ));
      const lifecycleReady = lifecycleEvents.some((event, index) => (
        index >= before.lifecycle
          && (!frameId || event.params?.frameId === frameId)
          && ['DOMContentLoaded', 'load'].includes(event.params?.name)
          && (!loaderId || !event.params?.loaderId || event.params.loaderId === loaderId)
      ));
      const frameReady = frameNavigations.some((event, index) => (
        index >= before.frames && (!frameId || event.params?.frame?.id === frameId)
      ));
      if ((contextReady && lifecycleReady) || frameReady) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(
      `Timed out waiting for ${label} navigation: ${JSON.stringify({
        frameId,
        loaderId,
        lifecycleEvents: lifecycleEvents.length,
        executionContexts: executionContexts.length,
        frameNavigations: frameNavigations.length,
      })}`,
    );
  };

  const apiMemoryContains = async (needle, label) => {
    const response = await fetch(`${base}/api/memory`, {
      headers: { 'x-test-user': 'alice' },
    });
    assert.equal(response.status, 200, `${label} API read failed`);
    const payload = await response.json();
    assert.ok(JSON.stringify(payload).includes(needle), `${label} API read omitted expected memory`);
  };
  const pressEnter = async () => {
    const key = {
      key: 'Enter',
      code: 'Enter',
      windowsVirtualKeyCode: 13,
      nativeVirtualKeyCode: 13,
    };
    await command('Input.dispatchKeyEvent', { type: 'rawKeyDown', ...key });
    await command('Input.dispatchKeyEvent', {
      type: 'char',
      text: '\r',
      unmodifiedText: '\r',
      ...key,
    });
    await command('Input.dispatchKeyEvent', { type: 'keyUp', ...key });
  };
  const bootBrain = async () => {
    await waitFor(
      "window.memoryModule && window.sessionModule && document.getElementById('tool-memory-btn')",
      'production Brain modules',
      30_000,
    );
    await evaluate(`(() => {
      window.sessionModule.setCurrentSessionId('memory-browser-session');
      document.getElementById('tool-memory-btn').click();
      document.querySelector('.memory-tab[data-memory-tab="browse"]').click();
    })()`);
    await waitFor(
      "!document.getElementById('memory-modal').classList.contains('hidden')",
      'Brain modal',
    );
    await waitFor(
      "!document.getElementById('memory-count-h2').textContent.includes('loading')",
      'memory list',
    );
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Page.setLifecycleEventsEnabled', { enabled: true });
  await command('Page.bringToFront');
  await command('Network.setBypassServiceWorker', { bypass: true });
  await command('Network.setCacheDisabled', { cacheDisabled: true });
  await command('Network.setBlockedURLs', {
    urls: [
      '*://cdn.jsdelivr.net/*',
      `*://127.0.0.1:${appPort}/static/sw.js`,
    ],
  });
  const initialNavigationBefore = {
    lifecycle: lifecycleEvents.length,
    contexts: executionContexts.length,
    frames: frameNavigations.length,
  };
  const initialNavigation = await command('Page.navigate', { url: `${base}/memory` });
  await waitForNavigation('initial', initialNavigation, initialNavigationBefore);
  await bootBrain();

  await evaluate(`(() => {
    document.querySelector('.memory-tab[data-memory-tab="add"]').click();
    const category = document.getElementById('new-memory-category');
    category.value = 'unknown';
    category.dispatchEvent(new Event('change', { bubbles: true }));
    const input = document.getElementById('new-memory-input');
    input.value = 'Where is the launch checklist stored';
    input.focus();
  })()`);
  await pressEnter();
  await waitFor(
    "document.getElementById('new-memory-input').value === ''",
    'open-question save',
  );
  await waitFor(
    "document.getElementById('memory-list').textContent.includes('Where is the launch checklist stored?')",
    'open-question list refresh',
  );
  await apiMemoryContains('Where is the launch checklist stored?', 'open-question save');

  const firstReloadBefore = {
    lifecycle: lifecycleEvents.length,
    contexts: executionContexts.length,
    frames: frameNavigations.length,
  };
  const firstReload = await command('Page.reload', { ignoreCache: true });
  await waitForNavigation('first hard refresh', firstReload, firstReloadBefore);
  await bootBrain();
  await apiMemoryContains('Where is the launch checklist stored?', 'first hard refresh');
  await waitFor(
    "document.getElementById('memory-list').textContent.includes('Where is the launch checklist stored?')",
    'open question after hard refresh',
  );

  await evaluate(`(() => {
    document.querySelector('.memory-tab[data-memory-tab="add"]').click();
    const file = new File(
      ['Project Phoenix launches from the violet notebook.'],
      'phoenix.md',
      { type: 'text/markdown' },
    );
    const transfer = new DataTransfer();
    transfer.items.add(file);
    const input = document.getElementById('memory-import-file');
    input.files = transfer.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  assert.equal(
    await evaluate("window.sessionModule.getCurrentSessionId()"),
    'memory-browser-session',
    'import lost its production session scope',
  );
  await waitFor(
    "document.querySelector('[aria-label^=\"Imported memory text\"]')?.value.includes('violet notebook')",
    'multipart import suggestions',
    30_000,
  );
  const importResponse = responses.find(item => (
    item.method === 'POST' && item.url.endsWith('/api/memory/import-batches')
  ));
  importPostData = importResponse
    ? (await command('Network.getRequestPostData', {
      requestId: importResponse.requestId,
    })).postData
    : '';
  assert.ok(
    importResponse
      && importResponse.status === 200
      && importPostData.includes('name="session"')
      && importPostData.includes('memory-browser-session'),
    `real multipart import did not carry the active production session: ${JSON.stringify({ importResponse, importPostData })}`,
  );
  const importedSuggestion = await evaluate(
    "document.querySelector('[aria-label^=\"Imported memory text\"]').value",
  );
  assert.match(importedSuggestion, /violet notebook/i);

  await evaluate(`(() => {
    const text = document.querySelector('[aria-label^="Imported memory text"]');
    text.value = 'Alice keeps the launch map in the blue notebook.';
    text.dispatchEvent(new Event('input', { bubbles: true }));
    document.querySelector('.memory-suggestion-item .save').click();
  })()`);
  await waitFor(
    "document.getElementById('toast').textContent.includes('Saved to memory')",
    'imported suggestion save',
  );
  await evaluate(
    "document.querySelector('.memory-tab[data-memory-tab=\"browse\"]').click()",
  );
  await waitFor(
    "document.getElementById('memory-list').textContent.includes('Alice keeps the launch map in the blue notebook.')",
    'saved imported memory',
  );

  await evaluate(`(() => {
    const card = [...document.querySelectorAll('#memory-list .memory-item')]
      .find(item => item.textContent.includes('blue notebook'));
    card.querySelector('.memory-item-text').dispatchEvent(
      new MouseEvent('dblclick', { bubbles: true }),
    );
    const input = card.querySelector('.memory-item-edit-input');
    input.value = 'Alice keeps the launch map in the green notebook.';
    card.querySelector('.memory-item-btn.save').click();
  })()`);
  await waitFor(
    "document.getElementById('memory-list').textContent.includes('green notebook')",
    'edited memory save',
  );

  const secondReloadBefore = {
    lifecycle: lifecycleEvents.length,
    contexts: executionContexts.length,
    frames: frameNavigations.length,
  };
  const secondReload = await command('Page.reload', { ignoreCache: true });
  await waitForNavigation('second hard refresh', secondReload, secondReloadBefore);
  await bootBrain();
  await apiMemoryContains('Alice keeps the launch map in the green notebook.', 'second hard refresh');
  await waitFor(
    "document.getElementById('memory-list').textContent.includes('green notebook')"
      + " && document.getElementById('memory-list').textContent.includes('Where is the launch checklist stored?')",
    'edited memories after refresh',
  );

  await evaluate(
    "document.querySelector('.memory-tab[data-memory-tab=\"graph\"]').click()",
  );
  await waitFor(
    "/showing [1-9][0-9]*\\/[1-9][0-9]* nodes/.test(document.getElementById('memory-graph-counts').textContent)",
    'nonempty graph canvas data',
  );
  const graphCounts = await evaluate(
    "document.getElementById('memory-graph-counts').textContent",
  );
  assert.match(graphCounts, /showing [1-9][0-9]*\/[1-9][0-9]* nodes/);
  const graphTrace = await evaluate(`(async () => {
    const overview = await fetch('/api/memory/graph?op=overview&limit=50').then(response => response.json());
    const entity = overview.nodes.find(node => node.id.startsWith('entity_'));
    const block = overview.nodes.find(node => node.id.startsWith('block_'));
    if (!entity || !block) throw new Error('canonical entity and block are present');
    const trace = await fetch(
      '/api/memory/graph?op=trace&node=' + encodeURIComponent(entity.id)
        + '&to_node=' + encodeURIComponent(block.id) + '&limit=5',
    ).then(response => response.json());
    return {
      paths: trace.paths || [],
      dangling: (overview.edges || []).filter(edge => (
        !overview.nodes.some(node => node.id === edge.src_id)
          || !overview.nodes.some(node => node.id === edge.dst_id)
      )).length,
    };
  })()`);
  assert.equal(graphTrace.dangling, 0, 'overview must not return dangling edges');
  assert.ok(Array.isArray(graphTrace.paths[0]?.node_ids));
  assert.equal(graphTrace.paths[0]?.node_ids?.length, 2, 'canonical trace must return two node ids');
  assert.ok(
    responses.some(item => item.method === 'PUT' && /\/api\/memory\/m_/.test(item.url) && item.status === 200),
    'real memory edit request did not complete',
  );
  assert.deepEqual(
    responses.filter(item => item.url.includes('/api/memory') && item.status >= 400),
    [],
    'a real memory API request failed',
  );
  assert.deepEqual(
    exceptions,
    [],
    'the real SPA raised a runtime exception',
  );
  assert.deepEqual(
    consoleErrors,
    [],
    'the real SPA logged a console error',
  );

  process.stdout.write(JSON.stringify({
    lifecycle: 'pass',
    browser: 'chromium-cdp',
    provider: 'release-path fm-mcp',
    persistence: 'hard-refresh',
    import: 'multipart-production-route',
    graph: graphCounts,
  }) + '\n');
} finally {
  await cleanup();
}
