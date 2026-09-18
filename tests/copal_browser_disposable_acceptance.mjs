#!/usr/bin/env node

// Own the complete runtime boundary for the broad Copal journey. The existing
// browser script remains the assertion source; this wrapper supplies it with
// a fresh app, database/data roots, Chrome profile, debugger port, and output
// directory so it cannot fall through to the developer's live ports or data.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { spawn } from 'node:child_process';
import { setTimeout as delay } from 'node:timers/promises';

const repo = process.cwd();
const python = process.env.OPENCLANK_PYTHON
  || (fs.existsSync(path.join(repo, 'venv', 'bin', 'python')) ? path.join(repo, 'venv', 'bin', 'python') : path.join(repo, '.venv', 'bin', 'python'));
const chrome = [
  process.env.OPENCLANK_CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Brave Browser.app/Contents/MacOS/Brave Browser',
  '/usr/bin/google-chrome',
  '/usr/bin/google-chrome-stable',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
].filter(Boolean).find(candidate => fs.existsSync(candidate));
assert(fs.existsSync(python), `Python runtime not found: ${python}`);
assert(chrome, 'Chrome/Chromium executable required');

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const port = server.address().port;
      server.close(error => error ? reject(error) : resolve(port));
    });
  });
}

function outputOf(child) {
  let output = '';
  const collect = chunk => {
    output += String(chunk);
    if (output.length > 100_000) output = output.slice(-100_000);
  };
  child.stdout?.on('data', collect);
  child.stderr?.on('data', collect);
  return () => output;
}

async function stopProcess(child, label) {
  if (!child || child.exitCode != null) return;
  const exited = new Promise(resolve => child.once('exit', resolve));
  try { process.kill(-child.pid, 'SIGTERM'); }
  catch (_) { try { child.kill('SIGTERM'); } catch (_) {} }
  await Promise.race([exited, delay(5000)]);
  if (child.exitCode == null) {
    try { process.kill(-child.pid, 'SIGKILL'); }
    catch (_) { try { child.kill('SIGKILL'); } catch (_) {} }
    await Promise.race([exited, delay(2000)]);
  }
  if (child.exitCode == null) throw new Error(`${label} did not exit during cleanup`);
}

async function waitForHealth(base, app, logs) {
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    if (app.exitCode != null) throw new Error(`Disposable app exited (${app.exitCode})\n${logs().slice(-5000)}`);
    try {
      const response = await fetch(`${base}/api/health`);
      if (response.ok) return;
    } catch (_) {}
    await delay(100);
  }
  throw new Error(`Disposable app health timeout\n${logs().slice(-5000)}`);
}

async function waitForDebugger(browser, logs) {
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline && browser.exitCode == null) {
    const match = logs().match(/DevTools listening on ws:\/\/127\.0\.0\.1:(\d+)/);
    if (match) return Number(match[1]);
    await delay(50);
  }
  throw new Error(`Chrome DevTools startup timeout\n${logs().slice(-3000)}`);
}

const startedAt = Date.now();
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-copal-browser-disposable-'));
const data = path.join(temporary, 'data');
const copal = path.join(temporary, 'copal');
const outputDir = path.join(temporary, 'output');
fs.mkdirSync(data, { recursive: true });
fs.mkdirSync(copal, { recursive: true });
fs.mkdirSync(outputDir, { recursive: true });

let app;
let browser;
let journey;
let cleanupPromise;
const appPort = await freePort();
const debuggerPort = await freePort();
const base = `http://127.0.0.1:${appPort}`;
const debuggerBase = `http://127.0.0.1:${debuggerPort}`;
const environment = {
  ...process.env,
  APP_BIND: '127.0.0.1',
  APP_PORT: String(appPort),
  AUTH_ENABLED: 'false',
  DEBUG: 'false',
  OPENCLANK_DEBUG: 'false',
  OPENCLANK_RECOVERY_MODE: 'true',
  OPEN_CLANK_DATA_DIR: data,
  ODYSSEUS_DATA_DIR: data,
  DATABASE_URL: `sqlite:///${path.join(data, 'app.db')}`,
  COPAL_DATA_DIR: copal,
  COPAL_STORAGE: 'redb',
  PYTHONUNBUFFERED: '1',
};

async function cleanup() {
  await stopProcess(journey, 'broad Copal journey').catch(() => {});
  await stopProcess(browser, 'Chrome').catch(() => {});
  await stopProcess(app, 'disposable app').catch(() => {});
  fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 8, retryDelay: 100 });
}

function handleSignal(signal) {
  if (!cleanupPromise) cleanupPromise = cleanup();
  cleanupPromise.finally(() => process.exit(128 + (signal === 'SIGINT' ? 2 : 15)));
}

process.once('SIGINT', handleSignal);
process.once('SIGTERM', handleSignal);

try {
  app = spawn(python, ['-m', 'uvicorn', 'app:app', '--host', '127.0.0.1', '--port', String(appPort)], {
    cwd: repo,
    env: environment,
    detached: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const appLogs = outputOf(app);
  await waitForHealth(base, app, appLogs);

  browser = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox',
    '--disable-background-networking', '--disable-component-update',
    '--disable-default-apps', '--disable-sync', '--no-first-run',
    '--no-default-browser-check', `--remote-debugging-port=${debuggerPort}`,
    `--user-data-dir=${path.join(temporary, 'chrome-profile')}`, 'about:blank',
  ], { detached: true, stdio: ['ignore', 'pipe', 'pipe'] });
  const browserLogs = outputOf(browser);
  await waitForDebugger(browser, browserLogs);

  const command = [process.execPath, 'tests/copal_browser_acceptance.mjs', base, debuggerBase, outputDir];
  const journeyStartedAt = Date.now();
  journey = spawn(command[0], command.slice(1), {
    cwd: repo,
    env: environment,
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  const journeyLogs = outputOf(journey);
  const journeyExit = await new Promise((resolve, reject) => {
    journey.once('error', reject);
    journey.once('exit', (code, signal) => resolve({ code, signal }));
  });
  const resultPath = path.join(outputDir, 'results.json');
  const result = fs.existsSync(resultPath) ? JSON.parse(fs.readFileSync(resultPath, 'utf8')) : null;
  const summary = {
    command: command.join(' '),
    app: { base, auth: 'explicitly-disabled', dataDir: data, databaseUrl: environment.DATABASE_URL, copalDataDir: copal },
    debugger: debuggerBase,
    exitCode: journeyExit.code,
    signal: journeyExit.signal,
    durationMs: Date.now() - journeyStartedAt,
    totalDurationMs: Date.now() - startedAt,
    result: result ? { keys: Object.keys(result), exceptions: result.exceptions, consoleMessages: result.consoleMessages } : null,
    outputTail: journeyLogs().slice(-4000),
  };
  console.log(JSON.stringify(summary, null, 2));
  assert.equal(journeyExit.code, 0, `Broad Copal journey failed\n${journeyLogs().slice(-8000)}`);
} finally {
  if (!cleanupPromise) cleanupPromise = cleanup();
  await cleanupPromise;
  process.removeListener('SIGINT', handleSignal);
  process.removeListener('SIGTERM', handleSignal);
}
