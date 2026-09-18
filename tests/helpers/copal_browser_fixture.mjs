import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

// Disposable static fixture only. It never connects to the running application
// or opens an existing Chrome profile. All API behavior is supplied by the test.
export async function withCopalBrowser({ page, overrides = {}, request = null, cdpTimeoutMs = 15000 }, run) {
  const root = process.cwd();
  let browser, ws, profile;
  const server = http.createServer(async (req, res) => {
    try {
      const pathname = new URL(req.url, 'http://fixture').pathname;
      if (request && await request(req, res)) return;
      if (pathname === '/') { res.setHeader('content-type', 'text/html'); res.end(page); return; }
      if (pathname in overrides) { res.setHeader('content-type', 'text/javascript'); res.end(overrides[pathname]); return; }
      const file = path.resolve(root, `.${pathname}`);
      if (pathname.startsWith('/static/') && file.startsWith(`${path.join(root, 'static')}${path.sep}`)) {
        res.setHeader('content-type', file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : 'application/octet-stream');
        res.end(fs.readFileSync(file)); return;
      }
      res.writeHead(404); res.end();
    } catch (error) { res.writeHead(500); res.end(String(error)); }
  });
  try {
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const chrome = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome'].find(fs.existsSync);
    assert(chrome, 'Chrome executable required');
    profile = fs.mkdtempSync(path.join(os.tmpdir(), 'copal-fixture-'));
    browser = spawn(chrome, ['--headless=new', '--disable-gpu', '--no-sandbox', `--user-data-dir=${profile}`, '--remote-debugging-port=0', 'about:blank'], { stdio:['ignore', 'pipe', 'pipe'] });
    let port;
    let diagnostics = '';
    const output = chunk => { diagnostics += String(chunk); const match = diagnostics.match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/); if (match) port = Number(match[1]); };
    browser.stderr.on('data', output); browser.stdout.on('data', output);
    browser.on('error', error => { diagnostics += String(error); });
    const startupDeadline = Date.now() + 120000;
    while (!port && Date.now() < startupDeadline && browser.exitCode == null) await delay(25);
    assert(port, `Chrome DevTools did not start: ${diagnostics.slice(-2000)}`);
    const url = `http://127.0.0.1:${server.address().port}/`;
    const target = await (await fetch(`http://127.0.0.1:${port}/json/new?${encodeURIComponent(url)}`, { method:'PUT' })).json();
    ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { ws.addEventListener('open', resolve, { once:true }); ws.addEventListener('error', reject, { once:true }); });
    let sequence = 0;
    const pending = new Map();
    ws.addEventListener('message', event => {
      const value = JSON.parse(event.data);
      if (value.method === 'Runtime.exceptionThrown') {
        const details = value.params?.exceptionDetails || {};
        const location = details.url ? ` ${details.url}:${details.lineNumber ?? 0}:${details.columnNumber ?? 0}` : '';
        const description = details.exception?.description || details.exception?.value || details.text || 'browser exception';
        process.stderr.write(`CDP page exception${location}: ${description}\n`);
      }
      if (value.id && pending.has(value.id)) { pending.get(value.id)(value); pending.delete(value.id); }
    });
    const cdp = (method, params = {}) => new Promise((resolve, reject) => {
      const id = ++sequence;
      const timer = setTimeout(() => { pending.delete(id); reject(new Error(`CDP timeout: ${method}${method === 'Runtime.evaluate' ? ` (${String(params.expression || '').slice(0, 180)})` : ''}`)); }, cdpTimeoutMs);
      pending.set(id, value => { clearTimeout(timer); value.error ? reject(new Error(value.error.message)) : resolve(value.result); });
      ws.send(JSON.stringify({ id, method, params }));
    });
    const evaluate = async (expression, awaitPromise = true) => {
      const result = await cdp('Runtime.evaluate', { expression, awaitPromise, returnByValue:true });
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result?.value;
    };
    const until = async expression => {
      for (let attempt = 0; attempt < 200; attempt++) { if (await evaluate(expression)) return; await delay(25); }
      throw new Error(`Fixture timeout: ${expression}`);
    };
    await cdp('Runtime.enable'); await cdp('Page.enable');
    // `/json/new` may acknowledge a target before its navigation commits.
    // Explicitly navigate after attaching so an immediate readyState check
    // cannot observe about:blank and race absolute ESM imports.
    await cdp('Page.navigate', { url });
    await until(`location.origin === ${JSON.stringify(new URL(url).origin)}`);
    await run({ cdp, evaluate, until, url });
  } finally {
    ws?.close();
    if (browser && browser.exitCode == null) {
      browser.kill('SIGTERM');
      for (let attempt = 0; browser.exitCode == null && browser.signalCode == null && attempt < 20; attempt++) await delay(25);
      if (browser.exitCode == null && browser.signalCode == null) { browser.kill('SIGKILL'); await delay(50); }
    }
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
    if (profile) fs.rmSync(profile, { recursive:true, force:true, maxRetries:8, retryDelay:100 });
  }
}
