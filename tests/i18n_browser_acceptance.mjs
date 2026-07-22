#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7000').replace(/\/$/, '');
const outputDir = process.argv[3] || '/tmp/openclank-i18n-browser';
fs.mkdirSync(outputDir, { recursive: true });

const port = await new Promise((resolve, reject) => {
  const server = net.createServer();
  server.once('error', reject);
  server.listen(0, '127.0.0.1', () => {
    const selected = server.address().port;
    server.close(() => resolve(selected));
  });
});
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-i18n-'));
const chromium = spawn('/usr/bin/chromium', [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  const debuggerBase = `http://127.0.0.1:${port}`;
  let targets;
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try { targets = await fetch(`${debuggerBase}/json`).then(response => response.json()); break; }
    catch { await new Promise(resolve => setTimeout(resolve, 50)); }
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

  await command('Page.navigate', { url: `${base}/static/login.html?__lang=es-ES&__fresh=1` });
  await waitFor("window.openClankI18n && document.querySelector('.i18n-offer')", 'Spanish language offer');
  const offer = await evaluate(`({
    locale:window.openClankI18n.locale,
    lang:document.documentElement.lang,
    message:document.querySelector('.i18n-offer p').textContent,
    direction:document.querySelector('.i18n-offer').dir,
  })`);
  assert.equal(offer.locale, 'en');
  assert.equal(offer.lang, 'en');
  assert.match(offer.message, /idioma.*español/i);
  assert.equal(offer.direction, 'ltr');
  await evaluate("document.querySelector('.i18n-offer [data-accept]').click()");
  await waitFor("window.openClankI18n.locale === 'es' && !document.querySelector('.i18n-offer')", 'accepted Spanish locale');
  assert.equal(await evaluate("localStorage.getItem('openclank.locale')"), 'es');

  const locales = ['en', 'zh-Hans', 'ja', 'ko', 'es', 'hi', 'ar', 'ru', 'pt', 'id', 'pa-Guru', 'bn', 'sw', 'ur', 'fa'];
  await evaluate(`(() => {
    const select=document.createElement('select');
    select.dataset.languageSelect='';
    for (const locale of ${JSON.stringify(locales)}) select.add(new Option(locale, locale));
    document.body.appendChild(select);
  })()`);
  const matrix = await evaluate(`(async () => {
    const locales=${JSON.stringify(locales)};
    const rtl=new Set(['ar','ur','fa']);
    const failures=[];
    let checked=0;
    for (const from of locales) {
      await window.openClankI18n.setLocale(from);
      for (const to of locales) {
        await window.openClankI18n.setLocale(to);
        checked += 1;
        if (document.documentElement.lang !== to) failures.push(from+'>'+to+':lang');
        if (document.documentElement.dir !== (rtl.has(to) ? 'rtl' : 'ltr')) failures.push(from+'>'+to+':dir');
        if (document.querySelector('[data-language-select]').value !== to) failures.push(from+'>'+to+':select');
      }
    }
    return {checked,failures};
  })()`);
  assert.equal(matrix.checked, 225);
  assert.deepEqual(matrix.failures, []);

  const contentSafety = await evaluate(`(async () => {
    const message=document.createElement('div'); message.className='msg';
    message.innerHTML='<div class="body">Save</div><div class="msg-actions"><button>Save</button></div>';
    document.body.appendChild(message);
    await window.openClankI18n.setLocale('ja');
    return {body:message.querySelector('.body').textContent,button:message.querySelector('button').textContent};
  })()`);
  assert.equal(contentSafety.body, 'Save');
  assert.notEqual(contentSafety.button, 'Save');

  const capture = await command('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  fs.writeFileSync(path.join(outputDir, 'japanese-interface.png'), Buffer.from(capture.data, 'base64'));

  await command('Page.navigate', { url: `${base}/static/login.html?__lang=zh-TW&__fresh=1` });
  await waitFor("window.openClankI18n && document.readyState === 'complete'", 'unsupported locale page');
  await new Promise(resolve => setTimeout(resolve, 400));
  assert.equal(await evaluate("window.openClankI18n.locale"), 'en');
  assert.equal(await evaluate("Boolean(document.querySelector('.i18n-offer'))"), false);

  console.log(JSON.stringify({ offer, matrix, contentSafety, unsupportedTraditionalChinese: true }, null, 2));
} finally {
  try { socket?.close(); } catch {}
  chromium.kill('SIGTERM');
  fs.rmSync(profile, { recursive: true, force: true });
}
