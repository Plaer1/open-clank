#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';

const explicitBase = (process.argv[2] || '').replace(/\/$/, '');
const outputDir = process.argv[3] || '/tmp/openclank-i18n-browser';
fs.mkdirSync(outputDir, { recursive: true });

const freePort = () => new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});

async function stopProcess(child) {
  if (!child || child.exitCode != null) return;
  const exited = new Promise(resolve => child.once('exit', resolve));
  child.kill('SIGTERM');
  await Promise.race([exited, delay(3000)]);
  if (child.exitCode == null) {
    child.kill('SIGKILL');
    await Promise.race([exited, delay(1000)]);
  }
}

function chromeExecutable() {
  const candidates = [
    process.env.OPENCLANK_CHROME_BIN,
    process.env.CHROME_BIN,
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    '/Applications/Chromium.app/Contents/MacOS/Chromium',
    '/opt/homebrew/bin/chromium',
    '/usr/local/bin/chromium',
    '/usr/bin/chromium',
    '/usr/bin/chromium-browser',
    '/usr/bin/google-chrome',
  ].filter(Boolean);
  return candidates.find(candidate => fs.existsSync(candidate));
}
const chrome = chromeExecutable();
assert(chrome, 'Chrome/Chromium executable required; set OPENCLANK_CHROME_BIN or CHROME_BIN');
let staticServer;
let base = explicitBase;
if (!base) {
  const staticPort = await freePort();
  const localPython = path.join(process.cwd(), 'venv', 'bin', 'python');
  const python = process.env.PYTHON || (fs.existsSync(localPython) ? localPython : 'python3');
  staticServer = spawn(python, ['-m', 'http.server', String(staticPort), '--bind', '127.0.0.1'], {
    cwd: process.cwd(),
    stdio: 'ignore',
  });
  base = `http://127.0.0.1:${staticPort}`;
  let ready = false;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (staticServer.exitCode != null) break;
    try {
      if ((await fetch(`${base}/static/login.html`)).ok) {
        ready = true;
        break;
      }
    } catch {}
    await delay(25);
  }
  if (!ready) {
    await stopProcess(staticServer);
    throw new Error('Disposable i18n static server did not become ready');
  }
}

const port = await freePort();
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-i18n-'));
const chromium = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  const debuggerBase = `http://127.0.0.1:${port}`;
  let target;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    try {
      const targets = await fetch(`${debuggerBase}/json`).then(response => response.json());
      target = targets?.find(item => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
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
    message.error ? request.reject(new Error(`${request.method}: ${message.error.message}`)) : request.resolve(message.result);
  });
  const command = (method, params = {}) => new Promise((resolve, reject) => {
    const id = ++sequence;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error(`${method} timed out`)); }, 30_000);
    pending.set(id, { method, resolve, reject, timer });
    socket.send(JSON.stringify({ id, method, params }));
  });
  const evaluate = async expression => {
    const response = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };
  const waitFor = async (expression, label) => {
    const deadline = Date.now() + 30_000;
    while (Date.now() < deadline) {
      try { if (await evaluate(expression)) return; } catch {}
      await new Promise(resolve => setTimeout(resolve, 80));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Network.enable');
  await command('Network.setBypassServiceWorker', { bypass: true });
  await command('Page.addScriptToEvaluateOnNewDocument', { source: `(() => {
    const requested = new URL(location.href).searchParams.get('__lang') || 'en';
    Object.defineProperty(navigator, 'languages', { configurable:true, get:() => [requested] });
    Object.defineProperty(navigator, 'language', { configurable:true, get:() => requested });
    if (new URL(location.href).searchParams.has('__fresh')) {
      localStorage.removeItem('openclank.locale');
      for (const key of Object.keys(localStorage)) if (key.startsWith('openclank.locale.prompted.')) localStorage.removeItem(key);
    }
  })();` });

  const navigate = async query => {
    const url = `${base}/static/login.html?${query}`;
    await command('Page.navigate', { url });
    await waitFor(`location.href === ${JSON.stringify(url)} && Boolean(window.openClankI18n?.ready)`, 'language initialization');
    await evaluate("window.openClankI18n.ready");
  };

  await navigate('__lang=ja-JP&__fresh=1');
  const offer = await evaluate(`({
    locale:window.openClankI18n.locale,
    lang:document.documentElement.lang,
    message:document.querySelector('.i18n-offer p').textContent,
    direction:document.querySelector('.i18n-offer').dir,
  })`);
  assert.equal(offer.locale, 'en');
  assert.equal(offer.lang, 'en');
  assert.match(offer.message, /日本語/);
  assert.equal(offer.direction, 'ltr');
  await evaluate("document.querySelector('.i18n-offer [data-accept]').click()");
  await waitFor("window.openClankI18n.locale === 'ja' && !document.querySelector('.i18n-offer')", 'accepted Japanese locale');
  assert.equal(await evaluate("localStorage.getItem('openclank.locale')"), 'ja');

  // Mount the actual Settings control on this isolated login surface.
  await evaluate(`(async () => {
    const source = await fetch('/static/index.html').then(response => response.text());
    const settings = new DOMParser().parseFromString(source, 'text/html');
    const control = settings.querySelector('[data-language-select]').parentElement;
    document.body.appendChild(control);
    await window.openClankI18n.setLocale('ja');
  })()`);
  const readSettings = () => evaluate(`(() => {
    const select = document.querySelector('[data-language-select]');
    return {
      options: [...select.options].map(option => option.value),
      selected: select.value,
      label: select.labels[0].textContent.trim(),
      lang: document.documentElement.lang,
      dir: document.documentElement.dir,
    };
  })()`);
  const japanese = await readSettings();
  const released = ["en", "ja", "zh-Hans", "zh-Hant", "ru", "hi", "es", "es-419"];
  assert.deepEqual([...japanese.options].sort(), [...released].sort());
  assert.equal(japanese.selected, 'ja');
  assert.match(japanese.label, /言語/);
  assert.equal(japanese.lang, 'ja');
  assert.equal(japanese.dir, 'ltr');
  for (const next of [...released.filter(id => id !== 'ja'), 'ja']) {
    await evaluate(`(() => {
      const select = document.querySelector('[data-language-select]');
      select.value = ${JSON.stringify(next)};
      select.dispatchEvent(new Event('change', { bubbles: true }));
    })()`);
    await waitFor(`document.documentElement.lang === ${JSON.stringify(next)}`, 'Settings language switch');
    const settings = await readSettings();
    assert.equal(settings.selected, next);
    if (next !== 'en') assert.notEqual(settings.label, 'Interface language');
  }

  const contentSafety = await evaluate(`(async () => {
    const english = await fetch('/static/i18n/en.json').then(response => response.json());
    const home = window.openClankI18n.officialArticleTranslation(
      'openclank-docs-home', 'body', english['docs.openclank-docs-home.body']);
    const message = document.createElement('div'); message.className = 'msg';
    message.innerHTML = '<div class="body">Save</div><div class="msg-actions"><button>Save</button></div>';
    const composer = document.createElement('textarea');
    composer.placeholder = 'New Chat';
    composer.value = 'New Chat';
    document.body.append(message, composer);
    await new Promise(resolve => requestAnimationFrame(resolve));
    return {body:message.querySelector('.body').textContent, button:message.querySelector('button').textContent,
      placeholder:composer.placeholder, draft:composer.value, handbookLang:home?.lang, handbookHasJapanese:/[ぁ-んァ-ン]/u.test(home?.text || '')};
  })()`);
  assert.equal(contentSafety.body, 'Save');
  assert.equal(contentSafety.button, '保存');
  assert.match(contentSafety.placeholder, /チャット/);
  assert.equal(contentSafety.draft, 'New Chat');
  assert.equal(contentSafety.handbookLang, 'ja');
  assert.equal(contentSafety.handbookHasJapanese, true);

  const capture = await command('Page.captureScreenshot', { format:'png', captureBeyondViewport:false });
  fs.writeFileSync(path.join(outputDir, 'japanese-interface.png'), Buffer.from(capture.data, 'base64'));

  for (const [requested, expected, endonym] of [
    ['zh-Hant-TW', 'zh-Hant', '繁體中文'], ['es-MX', 'es-419', 'español latinoamericano'],
  ]) {
    await navigate('__lang=' + requested + '&__fresh=1');
    assert.ok(await evaluate(`document.querySelector('.i18n-offer p')?.textContent.includes(${JSON.stringify(endonym)})`));
    await evaluate("document.querySelector('.i18n-offer [data-accept]').click()");
    await waitFor(`document.documentElement.lang === ${JSON.stringify(expected)}`, 'accepted regional language');
    assert.ok(await evaluate(`document.querySelector('#i18n-language-status')?.textContent.includes(${JSON.stringify(endonym)})`));
  }
  await navigate('__lang=ko-KR&__fresh=1');
  assert.equal(await evaluate("window.openClankI18n.locale"), 'en');
  assert.equal(await evaluate("Boolean(document.querySelector('.i18n-offer'))"), false);
  await evaluate("localStorage.setItem('openclank.locale', 'ko')");
  await navigate('__lang=ko-KR');
  assert.equal(await evaluate("window.openClankI18n.locale"), 'en');
  assert.equal(await evaluate("document.documentElement.lang"), 'en');
  assert.equal(await evaluate("Boolean(document.querySelector('.i18n-offer'))"), false);

  console.log(JSON.stringify({ offer, japanese, contentSafety }, null, 2));
} finally {
  try { socket?.close(); } catch {}
  chromium.kill('SIGTERM');
  if (chromium.exitCode == null) {
    await new Promise(resolve => {
      const timer = setTimeout(() => {
        if (chromium.exitCode == null) chromium.kill('SIGKILL');
        resolve();
      }, 3000);
      chromium.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
  for (let attempt = 0; attempt < 10 && fs.existsSync(profile); attempt += 1) {
    fs.rmSync(profile, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
    if (fs.existsSync(profile)) await delay(100);
  }
  await stopProcess(staticServer);
}
