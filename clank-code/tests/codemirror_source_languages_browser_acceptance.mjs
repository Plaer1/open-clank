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
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome',
].filter(Boolean).find(candidate => fs.existsSync(candidate));
if (!chrome) {
  process.stdout.write(JSON.stringify({ skipped:'Chrome/Chromium unavailable' }) + '\n');
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-source-languages-'));
const browser = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio:'ignore' });

let socket;
try {
  let target;
  for (let attempt = 0; attempt < 400; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      target = targets.find(item => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert(target?.webSocketDebuggerUrl, 'Chrome page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once:true });
    socket.addEventListener('error', reject, { once:true });
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
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
    const response = await command('Runtime.evaluate', { expression, awaitPromise:true, returnByValue:true });
    if (response.exceptionDetails) throw new Error(response.exceptionDetails.exception?.description || response.exceptionDetails.text);
    return response.result.value;
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url:`${base}/login` });
  for (let attempt = 0; attempt < 300; attempt += 1) {
    if (await evaluate("document.readyState === 'complete'")) break;
    await new Promise(resolve => setTimeout(resolve, 50));
  }

  const fixtures = {
    JavaScript:'const answer = value => value + 42;',
    'TypeScript JSX':'const view: JSX.Element = <strong>hello</strong>;',
    JSON:'{"enabled": true, "count": 4}',
    HTML:'<main class="app">Hello</main>',
    CSS:'.app { color: rebeccapurple; }',
    Markdown:'# Heading\n\n**strong**',
    Python:'def answer(value: int) -> int:\n    return value + 42',
    PHP:'<?php function answer(int $value): int { return $value + 42; }',
    Rust:'fn answer(value: i32) -> i32 { value + 42 }',
    'C++':'int answer(int value) { return value + 42; }',
    'C#':'int Answer(int value) { return value + 42; }',
    Java:'class Answer { int value() { return 42; } }',
    Kotlin:'fun answer(value: Int): Int = value + 42',
    Go:'package main\nfunc answer(value int) int { return value + 42 }',
    Ruby:'def answer(value) = value + 42',
    Swift:'func answer(_ value: Int) -> Int { value + 42 }',
    Shell:'answer="$((value + 42))" # computed',
    Dockerfile:'FROM alpine:latest\nRUN echo hello',
    YAML:'enabled: true\nitems:\n  - one',
    TOML:'enabled = true\n[owner]\nname = "Open Clank"',
    SQL:'SELECT name FROM files WHERE size > 42;',
    XML:'<files><file name="readme" /></files>',
    Mermaid:'flowchart TD\n  A[Open Clank] --> B{Ready?}\n  B -->|yes| C[Ship]',
  };
  const result = await evaluate(`(async () => {
    const fixtures = ${JSON.stringify(fixtures)};
    const module = await import('/static/js/copal/codemirror.js?language-acceptance=' + Date.now());
    const rows = {};
    for (const [language, doc] of Object.entries(fixtures)) {
      const host = document.body.appendChild(document.createElement('div'));
      host.style.cssText = 'width:800px;height:180px';
      const editor = module.createSourceEditor({ parent:host, doc, language, lineNumbers:true });
      const beforeReady = { syntaxReady:host.dataset.syntaxReady || '', visibility:host.style.visibility || '' };
      const loaded = await editor.languageReady;
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      rows[language] = { loaded, spans:host.querySelectorAll('.cm-content span').length, beforeReady, afterReady:host.dataset.syntaxReady || '' };
      editor.destroy();
      host.remove();
    }
    const plainHost = document.body.appendChild(document.createElement('div'));
    const plain = module.createSourceEditor({ parent:plainHost, doc:'unclassified words', language:'Plain text' });
    rows['Plain text'] = { loaded:await plain.languageReady, spans:plainHost.querySelectorAll('.cm-content span').length };
    plain.destroy(); plainHost.remove();

    const themeHost = document.body.appendChild(document.createElement('div'));
    themeHost.style.cssText = 'width:800px;height:180px;--hl-keyword:rgb(1, 101, 201);--hl-string:rgb(2, 102, 202)';
    const themed = module.createSourceEditor({
      parent:themeHost,
      doc:'const themed = "Open Clank";',
      language:'JavaScript',
    });
    await themed.languageReady;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const tokenSpans = [...themeHost.querySelectorAll('.cm-content span')];
    const keywordToken = tokenSpans.find(span => span.textContent === 'const');
    const stringToken = tokenSpans.find(span => span.textContent === '"Open Clank"');
    const viewBeforeThemeChange = themed.view;
    rows['Open Clank theme'] = {
      keywordFound:!!keywordToken,
      stringFound:!!stringToken,
      keywordBefore:keywordToken ? getComputedStyle(keywordToken).color : '',
      stringColor:stringToken ? getComputedStyle(stringToken).color : '',
    };
    themeHost.style.setProperty('--hl-keyword', 'rgb(201, 101, 1)');
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    rows['Open Clank theme'].keywordAfter = keywordToken ? getComputedStyle(keywordToken).color : '';
    rows['Open Clank theme'].sameView = themed.view === viewBeforeThemeChange;
    themed.destroy(); themeHost.remove();
    return rows;
  })()`);

  for (const language of Object.keys(fixtures)) {
    assert.equal(result[language].loaded, true, `${language} parser did not load`);
    assert.ok(result[language].spans > 0, `${language} did not produce highlighted tokens`);
  }
  assert.equal(result.Mermaid.beforeReady.syntaxReady, 'loading', 'source must not claim Mermaid readiness before parsing');
  assert.equal(result.Mermaid.beforeReady.visibility, 'hidden', 'source must hide the unparsed first frame');
  assert.equal(result.Mermaid.afterReady, 'ready', 'Mermaid source did not reveal as ready');
  assert.equal(result['Plain text'].loaded, true);
  assert.equal(result['Plain text'].spans, 0, 'unknown/plain source must not claim a grammar');
  assert.equal(result['Open Clank theme'].keywordFound, true, 'JavaScript keyword token is missing');
  assert.equal(result['Open Clank theme'].stringFound, true, 'JavaScript string token is missing');
  assert.equal(result['Open Clank theme'].keywordBefore, 'rgb(1, 101, 201)', 'Open Clank keyword variable did not win');
  assert.equal(result['Open Clank theme'].stringColor, 'rgb(2, 102, 202)', 'Open Clank string variable did not win');
  assert.equal(result['Open Clank theme'].keywordAfter, 'rgb(201, 101, 1)', 'live theme variable change did not reach the token');
  assert.equal(result['Open Clank theme'].sameView, true, 'live theme variable change reconstructed the editor view');
  process.stdout.write(JSON.stringify({ languages:Object.keys(fixtures).length, plainText:'honest', result }) + '\n');
} finally {
  socket?.close();
  browser.kill('SIGTERM');
  await new Promise(resolve => browser.once('exit', resolve));
  fs.rmSync(profile, { recursive:true, force:true });
}
