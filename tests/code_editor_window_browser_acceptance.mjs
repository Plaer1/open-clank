#!/usr/bin/env node

// Disposable same-origin browser contract for the Code window. The filesystem
// boundary is mocked; this only proves the in-app explorer/editor composition.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const localPython = path.join(repo, 'venv', 'bin', 'python');
const python = process.env.PYTHON || (fs.existsSync(localPython) ? localPython : 'python3');

let ownedStaticServer = null;
let base = (process.argv[2] || '').replace(/\/$/, '');
if (!base) {
  const staticPort = await new Promise((resolve, reject) => {
    const probe = net.createServer();
    probe.once('error', reject);
    probe.listen(0, '127.0.0.1', () => {
      const value = probe.address().port;
      probe.close(() => resolve(value));
    });
  });
  ownedStaticServer = spawn(python, ['-m', 'http.server', String(staticPort), '--bind', '127.0.0.1'], { cwd: process.cwd(), stdio: 'ignore' });
  for (let attempt = 0; attempt < 100; attempt += 1) {
    try {
      if ((await fetch(`http://127.0.0.1:${staticPort}/static/js/codeEditor.js`)).ok) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  base = `http://127.0.0.1:${staticPort}`;
}
const chrome = [
  process.env.OPEN_CLANK_CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome',
].filter(Boolean).find(candidate => fs.existsSync(candidate));
if (!chrome) {
  process.stdout.write(JSON.stringify({ skipped: 'Chrome/Chromium unavailable' }) + '\n');
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-code-window-'));
const browser = spawn(chrome, ['--headless=new', '--no-sandbox', '--disable-gpu', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
let socket;
try {
  let target;
  for (let attempt = 0; attempt < 400; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      target = targets?.find(item => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) break;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 50));
  }
  assert(target?.webSocketDebuggerUrl, 'Chrome page target is unavailable');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  let sequence = 0;
  const pending = new Map();
  socket.addEventListener('message', event => {
    const message = JSON.parse(event.data);
    const request = pending.get(message.id);
    if (!request) return;
    pending.delete(message.id); clearTimeout(request.timer);
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
      await new Promise(resolve => setTimeout(resolve, 60));
    }
    let diagnostic = '';
    try { diagnostic = ` ${JSON.stringify(await evaluate(`(() => ({rows:document.querySelectorAll('.code-tree-row').length, more:!!document.querySelector('[data-explorer-root-more]'), status:document.querySelector('.code-editor-window .copal-workspace-status')?.textContent, errors:window.__testErrors, reads:window.__testReads, writes:window.__testWrites, requests:window.__testRequests, children:window.__testChildBodies}))()`))}`; } catch {}
    throw new Error(`Timed out waiting for ${label}.${diagnostic}`);
  };

  await command('Page.enable'); await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor(`location.origin === ${JSON.stringify(base)} && document.readyState === 'complete'`, 'Open Clank origin');
  await evaluate(`(async () => {
    localStorage.clear();
    window.__testOwner = 'owner';
    window.__authStatus = 200;
    window.__workspaceBrowseStatus = 200;
    window.__otherFileName = 'other.js';
    window.__testFiles = {
      '/work/main.js': 'const answer = 42;\\n',
      '/work/README.md': '# Read me\\n',
      '/work/src/lib.rs': 'pub fn answer() -> i32 { 42 }\\n',
      '/other/other.js': 'export const other = true;\\n',
      '/other/race-a.js': 'export const raceA = true;\\n',
      '/other/race-b.js': 'export const raceB = true;\\n',
      '/other/trash-race.js': 'export const keepMe = true;\\n',
      '/other/rename-race/first.js': 'export const renameFirst = true;\\n',
      '/other/rename-race/late.js': 'export const renameLate = true;\\n',
      '/other/trash-tree-race/first.js': 'export const trashFirst = true;\\n',
      '/other/trash-tree-race/late.js': 'export const trashLate = true;\\n',
      '/resource/resource.js': 'export const resourceRefOpen = true;\\n',
    };
    window.__testFingerprints = {
      '/work/main.js': 'main-fp-1',
      '/work/README.md': 'readme-fp-1',
      '/work/src/lib.rs': 'lib-fp-1',
      '/other/other.js': 'other-fp-1',
      '/other/race-a.js': 'race-a-fp-1',
      '/other/race-b.js': 'race-b-fp-1',
      '/other/trash-race.js': 'trash-race-fp-1',
      '/other/rename-race/first.js': 'rename-first-fp-1',
      '/other/rename-race/late.js': 'rename-late-fp-1',
      '/other/trash-tree-race/first.js': 'trash-first-fp-1',
      '/other/trash-tree-race/late.js': 'trash-late-fp-1',
      '/resource/resource.js': 'resource-ref-fp-1',
    };
    window.__testReads = {};
    window.__testWrites = [];
    window.__testRequests = [];
    window.__testChildBodies = [];
    window.__testBrowseRequests = [];
    window.__testDeferredWriteStarted = false;
    window.__testDeferredWriteAborted = false;
    window.__deferNextWrite = false;
    window.__resolveTestWrite = null;
    window.__deferReadPaths = new Set();
    window.__resolveTestReads = {};
    window.__testDeferredReadStarted = {};
    window.__testDeferredReadAborted = {};
    window.__deferTrash = false;
    window.__testTrashStarted = false;
    window.__resolveTestTrash = null;
    window.__deferRename = false;
    window.__testRenameStarted = false;
    window.__resolveTestRename = null;
    window.__renameRaceDirectory = 'rename-race';
    window.__testErrors = [];
    window.__resourceWorkspaceRequest = null;
    window.__showInFilesRequest = null;
    window.__showInFilesRequests = [];
    window.addEventListener('error', event => window.__testErrors.push(String(event.error?.stack || event.message || event.error)));
    window.addEventListener('unhandledrejection', event => window.__testErrors.push(String(event.reason?.stack || event.reason)));
    window.__testWriteSequence = 1;
    const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init = {}) => {
      const url = new URL(String(input), location.origin);
      window.__testRequests.push(url.pathname);
      if (url.pathname === '/api/files-v1/children') window.__testChildBodies.push(JSON.parse(init.body || '{}'));
      if (url.pathname === '/api/auth/status') {
        if (window.__authStatus !== 200) return json({ detail: 'auth unavailable' }, window.__authStatus);
        return json({ ok: true, username: window.__testOwner, is_admin: true });
      }
      if (url.pathname === '/api/workspace/browse') {
        if (window.__workspaceBrowseStatus !== 200) return json({ detail: 'workspace unavailable' }, window.__workspaceBrowseStatus);
        const ownerRoot = window.__testOwner === 'other-owner' ? '/other' : '/work';
        return json({ path: url.searchParams.get('path') || ownerRoot, selectable: true, dirs: [] });
      }
      if (url.pathname === '/api/files-v1/workspace') {
        const request = JSON.parse(init.body || '{}');
        window.__resourceWorkspaceRequest = request;
        return json({
          version: 1,
          workspace: { id: 'workspace-resource', name: 'Resource' },
          open_relative: 'resource.js',
        });
      }
      if (url.pathname === '/api/files-v1/roots') {
        return json({ version: 1, policy_generation: 1, entries: [{
          id: 'host-root', ref: 'rr1.host-root', provider: 'host', name: 'Host',
          kind: 'provider_root', capabilities: ['children', 'stat'],
        }] });
      }
      if (url.pathname === '/api/files-v1/places') return json({ version: 1, entries: [] });
      if (url.pathname === '/api/files-v1/workspaces' && (init.method || 'GET') === 'GET') {
        return json({ version: 1, entries: [{
          workspace: { id: 'workspace-resource', name: 'Resource', revision: 1 },
          availability: 'available',
          resource: { id: 'resource-folder', ref: 'rr1.resource-folder', provider: 'host', name: 'Resource', kind: 'folder', capabilities: ['children', 'stat'] },
        }] });
      }
      if (url.pathname === '/api/files-v1/workspace-resource') {
        const request = JSON.parse(init.body || '{}');
        window.__showInFilesRequest = request;
        window.__showInFilesRequests.push(request);
        return json({
          version: 1,
          workspace: { id: 'workspace-resource', name: 'Resource' },
          parent: { id: 'resource-folder', ref: 'rr1.resource-folder-parent', provider: 'host', name: 'Resource', kind: 'folder', capabilities: ['children', 'stat'] },
          resource: { id: 'resource-file', ref: 'rr1.resource-file-target', provider: 'host', name: 'resource.js', kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] },
        });
      }
      if (url.pathname === '/api/files-v1/children') {
        const request = JSON.parse(init.body || '{}');
        const pathRef = path => 'rr1.path:' + encodeURIComponent(path);
        const refPath = ref => String(ref || '').startsWith('rr1.path:') ? decodeURIComponent(String(ref).slice(9)) : '/work';
        window.__testBrowseRequests.push({ path: request.parent_ref === 'rr1.resource-folder' ? '/work' : '__files_root__', cursor: request.cursor });
        if (request.parent_ref === 'rr1.host-root') return json({ entries: [{
          id: 'resource-folder', ref: 'rr1.resource-folder', provider: 'host', name: 'Resource', kind: 'folder', capabilities: ['children', 'stat'],
          provenance: { favorite: true, default: true },
        }], next_cursor: null });
        if (request.parent_ref === 'rr1.resource-folder') {
          if (window.__testOwner === 'other-owner') return json({ entries: [
            { id: 'rename-race', ref: pathRef('/other/rename-race'), path: '/other/rename-race', provider: 'host', name: 'rename-race', kind: 'folder', capabilities: ['children', 'stat'] },
            { id: 'trash-tree-race', ref: pathRef('/other/trash-tree-race'), path: '/other/trash-tree-race', provider: 'host', name: 'trash-tree-race', kind: 'folder', capabilities: ['children', 'stat'] },
            ...[window.__otherFileName, 'race-a.js', 'race-b.js', 'trash-race.js'].map(name => ({ id: name, ref: pathRef('/other/' + name), path: '/other/' + name, provider: 'host', name, kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] })),
          ], next_cursor: null });
          const cursor = request.cursor;
          if (cursor === 'root-page-2') return json({ entries: [{ id: 'readme', ref: pathRef('/work/README.md'), path: '/work/README.md', provider: 'host', name: 'README.md', kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] }], next_cursor: null });
          return json({ entries: [{ id: 'src', ref: pathRef('/work/src'), path: '/work/src', provider: 'host', name: 'src', kind: 'folder', capabilities: ['children', 'stat'] }, { id: 'main', ref: pathRef('/work/main.js'), path: '/work/main.js', provider: 'host', name: 'main.js', kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] }], next_cursor: 'root-page-2' });
        }
        if (String(request.parent_ref).startsWith('rr1.path:')) {
          const parent = refPath(request.parent_ref);
          if (parent === '/work/src') return json({ entries: [{ id: 'lib', ref: pathRef('/work/src/lib.rs'), path: '/work/src/lib.rs', provider: 'host', name: 'lib.rs', kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] }], next_cursor: null });
          const entries = Object.keys(window.__testFiles).filter(path => path.startsWith(parent + '/') && !path.slice(parent.length + 1).includes('/')).map(path => ({ id: path, ref: pathRef(path), path, provider: 'host', name: path.split('/').at(-1), kind: 'file', capabilities: ['stat', 'read', 'open', 'download'] }));
          return json({ entries, next_cursor: null });
        }
        return json({ entries: [], next_cursor: null });
      }
      if (url.pathname === '/api/files-v1/stat' || url.pathname === '/api/files-v1/open-resource') {
        const request = JSON.parse(init.body || '{}');
        if (url.pathname.endsWith('/stat') && window.__workspaceBrowseStatus === 403) return json({ detail: 'workspace revoked' }, 403);
        const pathRef = ref => String(ref || '').startsWith('rr1.path:') ? decodeURIComponent(String(ref).slice(9)) : '/work';
        const requested = request.resource_ref === 'rr1.host-resource' ? '/resource/resource.js' : pathRef(request.resource_ref);
        const fingerprint = window.__testFingerprints[requested] || (requested + '-fp-1');
        if (url.pathname.endsWith('/stat')) return json({ resource: { ref: request.resource_ref, kind: request.resource_ref === 'rr1.resource-folder' ? 'folder' : 'file', revision: { kind: 'hostFingerprint', value: fingerprint } } });
        window.__testReads[requested] = (window.__testReads[requested] || 0) + 1;
        const resource = { ref: request.resource_ref, kind: 'file', revision: { kind: 'hostFingerprint', value: fingerprint } };
        if (window.__deferReadPaths.has(requested)) {
          window.__testDeferredReadStarted[requested] = (window.__testDeferredReadStarted[requested] || 0) + 1;
          return new Promise(resolve => {
            window.__resolveTestReads[requested] = () => resolve(json({ payload: { text: window.__testFiles[requested] || '', encoding: 'utf-8', newline: '\\n', resource }, resource }));
            init.signal?.addEventListener('abort', () => { window.__testDeferredReadAborted[requested] = true; resolve(json({ detail: { code: 'aborted' } }, 499)); }, { once: true });
          });
        }
        return json({ payload: { text: window.__testFiles[requested] || (requested === '/resource/resource.js' ? 'resourceRefOpen\\n' : ''), encoding: 'utf-8', newline: '\\n', resource }, resource });
      }
      if (url.pathname === '/api/files-v1/save-resource') {
        const request = JSON.parse(init.body || '{}');
        const requested = String(request.resource_ref || '').startsWith('rr1.path:') ? decodeURIComponent(String(request.resource_ref).slice(9)) : '/work/main.js';
        const current = window.__testFingerprints[requested];
        window.__testWrites.push({ endpoint: url.pathname, path: requested, expected_fingerprint: request.expected_revision?.value, replace_all: true, old: window.__testFiles[requested] || '', new: request.text });
        if (window.__deferNextWrite) {
          window.__deferNextWrite = false;
          window.__testDeferredWriteStarted = true;
          return new Promise(resolve => {
            window.__resolveTestWrite = () => { const next = requested + '-fp-deferred'; window.__testFiles[requested] = request.text; window.__testFingerprints[requested] = next; resolve(json({ outcome: 'applied', revision: { kind: 'hostFingerprint', value: next } })); };
            init.signal?.addEventListener('abort', () => { window.__testDeferredWriteAborted = true; resolve(json({ detail: { code: 'aborted' } }, 499)); }, { once: true });
          });
        }
        if (request.expected_revision?.value && request.expected_revision.value !== current) return json({ detail: { code: 'conflict', message: 'fingerprint mismatch' } }, 409);
        window.__testFiles[requested] = request.text;
        const nextFingerprint = requested + '-fp-' + (++window.__testWriteSequence);
        window.__testFingerprints[requested] = nextFingerprint;
        return json({ outcome: 'applied', revision: { kind: 'hostFingerprint', value: nextFingerprint } });
      }
      if (url.pathname === '/api/files-v1/action') {
        const request = JSON.parse(init.body || '{}');
        const ref = String(request.resource_ref || '');
        const source = ref.startsWith('rr1.path:') ? decodeURIComponent(ref.slice(9)) : '/other/other.js';
        const action = String(request.action || '');
        if (action === 'rename' || action === 'move') {
          const destination = source.slice(0, source.lastIndexOf('/') + 1) + String(request.args?.name || '').trim();
          if (window.__deferRename) {
            window.__deferRename = false;
            window.__testRenameStarted = true;
            await new Promise(resolve => { window.__resolveTestRename = resolve; });
            window.__resolveTestRename = null;
          }
          for (const oldPath of Object.keys(window.__testFiles).filter(path => path === source || path.startsWith(source + '/'))) {
            const nextPath = oldPath === source ? destination : destination + oldPath.slice(source.length);
            window.__testFiles[nextPath] = window.__testFiles[oldPath];
            window.__testFingerprints[nextPath] = window.__testFingerprints[oldPath];
            delete window.__testFiles[oldPath]; delete window.__testFingerprints[oldPath];
          }
          if (source === '/other/other.js') window.__otherFileName = destination.split('/').at(-1);
          return json({ resource: { ref: 'rr1.path:' + encodeURIComponent(destination), path: destination, kind: 'file', name: destination.split('/').at(-1) } });
        }
        if (action === 'trash') {
          if (window.__deferTrash) { window.__deferTrash = false; window.__testTrashStarted = true; await new Promise(resolve => { window.__resolveTestTrash = resolve; }); window.__resolveTestTrash = null; }
          return json({ resource: { ref, kind: 'file' }, trash: { ref } });
        }
        if (action === 'restore') return json({ resource: { ref, kind: 'file' } });
        return json({ resource: { ref, kind: 'file' } });
      }
      if (url.pathname === '/api/file-policy/workspaces/workspace-resource/resolve') {
        return json({ workspace: { id: 'workspace-resource', path: '/resource', name: 'Resource' } });
      }
      if (url.pathname === '/api/odysseus-files/browse') {
        const requested = url.searchParams.get('path');
        const cursor = url.searchParams.get('cursor');
        window.__testBrowseRequests.push({ path: requested, cursor });
        if (requested === '/resource') return json({ data: { path: '/resource', next_cursor: null, entries: [
          { name: 'resource.js', kind: 'file', size: 38, media_type: 'text/javascript' },
        ] } });
        if (requested === '/other') return json({ data: { path: '/other', next_cursor: null, entries: [
          { name: window.__renameRaceDirectory, kind: 'directory', size: 0 },
          { name: 'trash-tree-race', kind: 'directory', size: 0 },
          { name: window.__otherFileName, kind: 'file', size: 27, media_type: 'text/javascript' },
          { name: 'race-a.js', kind: 'file', size: 27, media_type: 'text/javascript' },
          { name: 'race-b.js', kind: 'file', size: 27, media_type: 'text/javascript' },
          { name: 'trash-race.js', kind: 'file', size: 28, media_type: 'text/javascript' },
        ] } });
        if (requested === '/other/' + window.__renameRaceDirectory) return json({ data: { path: requested, next_cursor: null, entries: [
          { name: 'first.js', kind: 'file', size: 33, media_type: 'text/javascript' },
          { name: 'late.js', kind: 'file', size: 32, media_type: 'text/javascript' },
        ] } });
        if (requested === '/other/trash-tree-race') return json({ data: { path: requested, next_cursor: null, entries: [
          { name: 'first.js', kind: 'file', size: 32, media_type: 'text/javascript' },
          { name: 'late.js', kind: 'file', size: 31, media_type: 'text/javascript' },
        ] } });
        if (requested === '/work/src') return json({ data: { path: '/work/src', next_cursor: null, entries: [
          { name: 'lib.rs', kind: 'file', size: 20, media_type: 'text/rust' },
        ] } });
        if (cursor === 'root-page-2') return json({ data: { path: '/work', next_cursor: null, entries: [
          { name: 'README.md', kind: 'file', size: 20, media_type: 'text/markdown' },
        ] } });
        return json({ data: { path: '/work', next_cursor: 'root-page-2', entries: [
          { name: 'src', kind: 'directory', size: 0 },
          { name: 'main.js', kind: 'file', size: 38, media_type: 'text/javascript' },
        ] } });
      }
      if (url.pathname === '/api/odysseus-files/stat') {
        const requested = url.searchParams.get('path');
        return json({ data: { kind: 'file', fingerprint: { value: window.__testFingerprints[requested] } } });
      }
      if (url.pathname === '/api/odysseus-files/read-text') {
        const requested = url.searchParams.get('path');
        window.__testReads[requested] = (window.__testReads[requested] || 0) + 1;
        if (window.__deferReadPaths.has(requested)) {
          window.__testDeferredReadStarted[requested] = (window.__testDeferredReadStarted[requested] || 0) + 1;
          await new Promise((resolve, reject) => {
            window.__resolveTestReads[requested] = () => {
              window.__deferReadPaths.delete(requested);
              delete window.__resolveTestReads[requested];
              resolve();
            };
            init.signal?.addEventListener('abort', () => {
              window.__testDeferredReadAborted[requested] = true;
              delete window.__resolveTestReads[requested];
              reject(new DOMException('The operation was aborted.', 'AbortError'));
            }, { once: true });
          });
        }
        return json({ data: { text: window.__testFiles[requested], fingerprint: { value: window.__testFingerprints[requested] }, encoding: 'utf-8', newline: '\\n' } });
      }
      if (url.pathname === '/api/odysseus-files/edit' || url.pathname === '/api/odysseus-files/write') {
        const request = JSON.parse(init.body || '{}');
        const currentFingerprint = window.__testFingerprints[request.path];
        window.__testWrites.push({ endpoint: url.pathname, ...request });
        if (request.expected_fingerprint && request.expected_fingerprint !== currentFingerprint) {
          return json({ detail: { code: 'conflict', message: 'fingerprint mismatch' } }, 409);
        }
        if (window.__deferNextWrite) {
          window.__deferNextWrite = false;
          window.__testDeferredWriteStarted = true;
          await new Promise((resolve, reject) => {
            window.__resolveTestWrite = resolve;
            init.signal?.addEventListener('abort', () => {
              window.__testDeferredWriteAborted = true;
              reject(new DOMException('The operation was aborted.', 'AbortError'));
            }, { once: true });
          });
          window.__resolveTestWrite = null;
        }
        window.__testFiles[request.path] = url.pathname.endsWith('/edit') ? request.new : request.text;
        const nextFingerprint = request.path + '-fp-' + (++window.__testWriteSequence);
        window.__testFingerprints[request.path] = nextFingerprint;
        return json({ data: { new_fingerprint: { value: nextFingerprint } } });
      }
      if (url.pathname === '/api/odysseus-files/rename') {
        const request = JSON.parse(init.body || '{}');
        if (window.__deferRename) {
          window.__deferRename = false;
          window.__testRenameStarted = true;
          await new Promise(resolve => { window.__resolveTestRename = resolve; });
          window.__resolveTestRename = null;
        }
        for (const oldPath of Object.keys(window.__testFiles).filter(path => path === request.path || path.startsWith(request.path + '/'))) {
          const nextPath = oldPath === request.path ? request.destination : request.destination + oldPath.slice(request.path.length);
          window.__testFiles[nextPath] = window.__testFiles[oldPath];
          window.__testFingerprints[nextPath] = window.__testFingerprints[oldPath];
          delete window.__testFiles[oldPath];
          delete window.__testFingerprints[oldPath];
        }
        if (request.path === '/other/other.js') window.__otherFileName = request.destination.split('/').at(-1);
        if (request.path === '/other/' + window.__renameRaceDirectory) window.__renameRaceDirectory = request.destination.split('/').at(-1);
        return json({ data: { path: request.destination } });
      }
      if (url.pathname === '/api/odysseus-files/trash') {
        const request = JSON.parse(init.body || '{}');
        if (window.__deferTrash) {
          window.__deferTrash = false;
          window.__testTrashStarted = true;
          await new Promise(resolve => { window.__resolveTestTrash = resolve; });
          window.__resolveTestTrash = null;
        }
        const entry = {
          id: 'trash-test', root_id: 'app-host', original_path: request.path,
          trashed_path: '/other/.odysseus-trash/trash-test',
        };
        for (const oldPath of Object.keys(window.__testFiles).filter(path => path === request.path || path.startsWith(request.path + '/'))) {
          const nextPath = oldPath === request.path ? entry.trashed_path : entry.trashed_path + oldPath.slice(request.path.length);
          window.__testFiles[nextPath] = window.__testFiles[oldPath];
          window.__testFingerprints[nextPath] = window.__testFingerprints[oldPath];
          delete window.__testFiles[oldPath];
          delete window.__testFingerprints[oldPath];
        }
        return json({ data: entry });
      }
      if (url.pathname === '/api/odysseus-files/restore') {
        const request = JSON.parse(init.body || '{}');
        const entry = request.entry;
        for (const oldPath of Object.keys(window.__testFiles).filter(path => path === entry.trashed_path || path.startsWith(entry.trashed_path + '/'))) {
          const nextPath = oldPath === entry.trashed_path ? entry.original_path : entry.original_path + oldPath.slice(entry.trashed_path.length);
          window.__testFiles[nextPath] = window.__testFiles[oldPath];
          window.__testFingerprints[nextPath] = window.__testFingerprints[oldPath];
          delete window.__testFiles[oldPath];
          delete window.__testFingerprints[oldPath];
        }
        return json({ data: { ok: true } });
      }
      return new Response(JSON.stringify({ detail: 'unexpected request: ' + url.pathname }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    };
    const code = await import('/static/js/codeEditor.js?code-acceptance=' + Date.now());
    window.__codeNamed = code;
    window.__codeModule = code.default;
    await code.default.open();
  })()`);
  await waitFor("document.querySelectorAll('.code-tree-row').length === 2 && document.querySelector('[data-explorer-root-more]')", 'lazy first workspace page');
  assert.equal(await evaluate("window.__testBrowseRequests.filter(request => request.path === '/work').length"), 1, `Code must not drain the workspace cursor during mount: ${JSON.stringify(await evaluate('window.__testChildBodies'))}`);
  await evaluate("document.querySelector('[data-explorer-root-more]').click()");
  await waitFor("document.querySelectorAll('.code-tree-row').length === 3", 'continued workspace tree');
  const initial = await evaluate(`(() => ({
    names: [...document.querySelectorAll('.code-tree-row')].map(row => row.querySelector('.code-tree-name')?.textContent),
    neutralController: document.querySelector('[data-code-tree]').classList.contains('oc-explorer-tree'),
    rovingCount: document.querySelectorAll('[data-code-tree] [role=treeitem][tabindex="0"]').length,
    levels: [...document.querySelectorAll('[data-code-tree] [role=treeitem]')].map(node => node.getAttribute('aria-level')),
  }))()`);
  assert.deepEqual(initial.names, ['src', 'main.js', 'README.md']);
  assert.equal(initial.neutralController, true);
  assert.equal(initial.rovingCount, 1);
  assert.deepEqual(initial.levels, ['1', '1', '1']);
  await evaluate(`(() => {
    const src = document.querySelector('.code-tree-row.directory').closest('[role=treeitem]');
    src.focus();
    src.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', bubbles: true }));
  })()`);
  assert.equal(await evaluate("document.activeElement?.querySelector('.code-tree-name')?.textContent"), 'README.md', 'Code explorer typeahead should use the neutral controller');
  await evaluate("document.querySelector('.code-tree-row.directory').click()");
  await waitFor("document.querySelector('.code-tree-children .code-tree-name')?.textContent === 'lib.rs'", 'expanded child');
  await evaluate(`(() => {
    const row = document.querySelector('.code-tree-row.directory');
    row.focus();
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }));
    return !document.querySelector('.code-tree-children .code-tree-name');
  })()`);
  await evaluate(`(() => {
    const row = document.querySelector('.code-tree-row.directory');
    row.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  })()`);
  await waitFor("document.querySelector('.code-tree-children .code-tree-name')?.textContent === 'lib.rs'", 'keyboard-expanded child');
  await evaluate("document.querySelector('button[title=Refresh]').click()");
  await waitFor("document.querySelector('.code-tree-children .code-tree-name')?.textContent === 'lib.rs'", 'expanded child after refresh');
  const browserMenuRestore = await evaluate(`(() => {
    localStorage.setItem('odysseus-custom-context-menu', 'off');
    window.dispatchEvent(new Event('odysseus-context-menu-changed'));
    const row = document.querySelector('.code-tree-row.file[data-code-tree-path$="/main.js"]');
    const event = new MouseEvent('contextmenu', { bubbles:true, cancelable:true, clientX:20, clientY:20 });
    const notCancelled = row.dispatchEvent(event);
    const prompt = document.querySelector('#styled-prompt-overlay');
    const promptVisible = !!prompt && !prompt.classList.contains('hidden') && prompt.style.display !== 'none';
    localStorage.setItem('odysseus-custom-context-menu', 'on');
    window.dispatchEvent(new Event('odysseus-context-menu-changed'));
    return { notCancelled, promptVisible };
  })()`);
  assert.equal(browserMenuRestore.notCancelled, true, 'disabled menu restores the browser context event');
  assert.equal(browserMenuRestore.promptVisible, false, 'disabled menu does not open the code action prompt');
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path$=\"/main.js\"]').click()");
  await waitFor("document.querySelector('.cm-editor') && document.querySelectorAll('.code-editor-tab-close').length === 1", 'source editor');
  await waitFor("document.querySelectorAll('.cm-content span').length > 0", 'source grammar tokens');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'source readiness');
  const editor = await evaluate(`(() => ({
    cm: !!document.querySelector('.cm-editor'),
    tokenSpans: document.querySelectorAll('.cm-content span').length,
    closeButtons: document.querySelectorAll('.code-editor-tab-close').length,
    noWrap: getComputedStyle(document.querySelector('.cm-scroller')).whiteSpace !== 'pre-wrap',
    separator: !!document.querySelector('.code-editor-shell [role=separator]'),
  }))()`);
  assert.equal(editor.cm, true);
  assert.ok(editor.tokenSpans > 0, 'source grammar should produce token spans');
  assert.equal(editor.closeButtons, 1);
  assert.equal(editor.noWrap, true);
  assert.equal(editor.separator, true);

  // Dirty the first buffer, then open two more. CodeMirror must retain the
  // first buffer's unsaved bytes while it is inactive.
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// unsaved-browser-edit\\n' });
  await waitFor("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent === '●'", 'dirty first buffer');
  await evaluate("[...document.querySelectorAll('.code-tree-row.file')].find(row => row.querySelector('.code-tree-name')?.textContent === 'README.md').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title?.endsWith('README.md')", 'README tab');
  await evaluate("[...document.querySelectorAll('.code-tree-row.file')].find(row => row.querySelector('.code-tree-name')?.textContent === 'lib.rs').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title?.endsWith('lib.rs') && document.querySelectorAll('.code-editor-tab-close').length === 3", 'three source tabs');
  assert.equal(await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title.endsWith('main.js'))?.querySelector('.code-editor-tab-dot')?.textContent"), '●');

  // Close Others is one aggregate dirty transaction. Cancel must preserve all
  // tabs and the inactive dirty buffer; Don't Save may then discard only the
  // in-memory edit and must never issue a host write.
  const openTabMenu = path => evaluate(`(() => {
    const tab = [...document.querySelectorAll('.code-editor-tab')].find(item => item.title === ${JSON.stringify(path)} || item.title.endsWith(${JSON.stringify(path.split('/').pop())}));
    tab.closest('.code-editor-tab-wrap').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 80, clientY: 60 }));
  })()`);
  await openTabMenu('/work/README.md');
  await waitFor("document.querySelector('[data-command=code-tab-close-others]')", 'tab context actions');
  await evaluate("document.querySelector('[data-command=code-tab-close-others]').click()");
  await waitFor("getComputedStyle(document.querySelector('#styled-confirm-overlay')).display !== 'none'", 'aggregate dirty prompt');
  await evaluate("document.querySelector('#styled-confirm-cancel').click()");
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 3 && document.querySelector('#styled-confirm-overlay').style.display === 'none'", 'cancelled aggregate close');
  assert.equal(await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title.endsWith('main.js'))?.querySelector('.code-editor-tab-dot')?.textContent"), '●');

  await openTabMenu('/work/README.md');
  await evaluate("document.querySelector('[data-command=code-tab-close-others]').click()");
  await waitFor("getComputedStyle(document.querySelector('#styled-confirm-overlay')).display !== 'none'", 'second aggregate dirty prompt');
  await evaluate("document.querySelector('#styled-confirm-alt').click()");
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 1 && document.querySelector('.code-editor-tab.active')?.title?.endsWith('README.md')", 'discarded other tabs');
  assert.equal(await evaluate("window.__testWrites.length"), 0, 'discarding a browser buffer must not write the host file');
  assert.equal(await evaluate("window.__testFiles['/work/main.js']"), 'const answer = 42;\n');

  // Reopen uses path/view metadata only: each action rereads current disk
  // bytes. The stack order is deterministic and works from button or shortcut.
  assert.equal(await evaluate("document.querySelector('[data-code-reopen-closed]').disabled"), false);
  await evaluate("document.querySelector('[data-code-reopen-closed]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title?.endsWith('lib.rs')", 'reopened last closed tab');
  assert.equal(await evaluate("window.__testReads['/work/src/lib.rs']"), 2);
  await evaluate("document.querySelector('.code-editor-window').dispatchEvent(new KeyboardEvent('keydown', { key: 't', ctrlKey: true, shiftKey: true, bubbles: true }))");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title?.endsWith('main.js') && document.querySelectorAll('.code-editor-tab').length === 3", 'keyboard reopened prior tab');
  assert.equal(await evaluate("window.__testReads['/work/main.js']"), 2);
  assert.equal(await evaluate("document.querySelector('.cm-content').textContent.includes('unsaved-browser-edit')"), false);

  // Closing the active middle tab selects the next tab; closing the final tab
  // on that side selects the previous one. Inactive closes retain the active.
  await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title.endsWith('lib.rs')).click()");
  await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title.endsWith('lib.rs')).closest('.code-editor-tab-wrap').querySelector('.code-editor-tab-close').click()");
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 2 && document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js')", 'next tab successor');
  await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title.endsWith('main.js')).closest('.code-editor-tab-wrap').querySelector('.code-editor-tab-close').click()");
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 1 && document.querySelector('.code-editor-tab.active')?.title.endsWith('README.md')", 'previous tab successor');

  // Change host bytes after close. Reopen must show those bytes instead of a
  // cached tab snapshot, while retaining the latest CAS fingerprint for save.
  await evaluate("window.__testFiles['/work/main.js'] = 'const answer = 84;\\n'; window.__testFingerprints['/work/main.js'] = 'main-fp-external'; document.querySelector('[data-code-reopen-closed]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js') && document.querySelector('.cm-content').textContent.includes('84')", 'reopen current disk bytes');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'reopened source readiness');

  // Close Saved is a context batch over clean tabs. A dirty target survives,
  // and a later Close→Save uses its own expected fingerprint before removal.
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// saved-browser-edit\\n' });
  await waitFor("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent === '●'", 'dirty reopened buffer');
  await openTabMenu('/work/main.js');
  await evaluate("document.querySelector('[data-command=code-tab-close-saved]').click()");
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 1 && document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js')", 'close saved tabs');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');

  await openTabMenu('/work/main.js');
  await evaluate("document.querySelector('[data-command=code-tab-close]').click()");
  await waitFor("getComputedStyle(document.querySelector('#styled-confirm-overlay')).display !== 'none'", 'dirty single close prompt');
  await evaluate("document.querySelector('#styled-confirm-ok').click()");
  try {
    await waitFor("document.querySelectorAll('.code-editor-tab').length === 0 && window.__testWrites.length === 1", 'saved final close');
  } catch (error) {
    const diagnostic = await evaluate(`(() => ({
      tabs: [...document.querySelectorAll('.code-editor-tab')].map(tab => ({ path: tab.title, dot: tab.querySelector('.code-editor-tab-dot')?.textContent })),
      writes: window.__testWrites,
      status: document.querySelector('.code-editor-window .copal-workspace-status')?.textContent,
      prompt: document.querySelector('#styled-confirm-msg')?.textContent,
      promptDisplay: document.querySelector('#styled-confirm-overlay')?.style.display,
      errors: window.__testErrors,
      saveError: document.querySelector('.code-editor-save-error')?.textContent,
    }))()`);
    throw new Error(`${error.message}: ${JSON.stringify(diagnostic)}`);
  }
  const savedWrite = await evaluate("window.__testWrites[0]");
  assert.equal(savedWrite.endpoint, '/api/files-v1/save-resource');
  assert.equal(savedWrite.path, '/work/main.js');
  assert.equal(savedWrite.expected_fingerprint, 'main-fp-external');
  assert.equal(savedWrite.replace_all, true);
  assert.ok(savedWrite.new.includes('saved-browser-edit'));

  await evaluate("document.querySelector('.code-editor-window').dispatchEvent(new KeyboardEvent('keydown', { key: 't', ctrlKey: true, shiftKey: true, bubbles: true }))");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js') && document.querySelector('.cm-content').textContent.includes('saved-browser-edit')", 'reopen saved final tab');
  await evaluate("window.__fingerprintBeforeExternalChange = window.__testFingerprints['/work/main.js']; window.__testFingerprints['/work/main.js'] = 'main-fp-after-external-change'; document.querySelector('button[title=Refresh]').click()");
  await waitFor("document.querySelector('.code-editor-save-error')", 'external file change warning');
  assert.equal(await evaluate("document.querySelector('.code-editor-save-error')?.textContent.includes('changed on disk')"), true);

  // Explorer refresh owns a separate generation from file saves. Hold a valid
  // save response, refresh the workspace while it is in flight, then release it:
  // the exact same owner/root/buffer must accept the completion and advance its
  // fingerprint/original snapshot. A follow-up CAS save proves both fields moved.
  await evaluate(`(() => {
    window.__testFingerprints['/work/main.js'] = window.__fingerprintBeforeExternalChange;
    window.__deferNextWrite = true;
    window.__testDeferredWriteStarted = false;
    document.querySelector('.cm-content').focus();
  })()`);
  await command('Input.insertText', { text: '// save-refresh-race\n' });
  await waitFor("document.querySelector('.cm-content')?.textContent.includes('save-refresh-race')", 'edited save-refresh fixture');
  await evaluate("document.querySelector('.code-editor-save').click()");
  await waitFor("window.__testDeferredWriteStarted && typeof window.__resolveTestWrite === 'function'", 'deferred save request');
  await evaluate("window.__browseCountBeforeSaveRefresh = window.__testBrowseRequests.length; document.querySelector('button[title=Refresh]').click()");
  await waitFor("window.__testBrowseRequests.length > window.__browseCountBeforeSaveRefresh", 'concurrent explorer refresh');
  await evaluate("window.__resolveTestWrite();");
  await waitFor("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent === '·' && document.querySelector('.code-editor-save')?.textContent === 'Saved'", 'save completion after explorer refresh');
  const firstRaceWrite = await evaluate("window.__testWrites.at(-1)");
  const raceFingerprint = await evaluate("window.__testFingerprints['/work/main.js']");
  assert.equal(firstRaceWrite.expected_fingerprint, await evaluate("window.__fingerprintBeforeExternalChange"));
  assert.equal(await evaluate("!!document.querySelector('.code-editor-save-error')"), false);

  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// follow-up-cas\n' });
  const writeCountBeforeFollowup = await evaluate("window.__testWrites.length");
  await evaluate("document.querySelector('.code-editor-save').click()");
  await waitFor(`window.__testWrites.length === ${writeCountBeforeFollowup + 1} && document.querySelector('.code-editor-save')?.textContent === 'Saved'`, 'follow-up CAS save');
  const followupWrite = await evaluate("window.__testWrites.at(-1)");
  assert.equal(followupWrite.expected_fingerprint, raceFingerprint, 'refresh-safe save must advance the next CAS fingerprint');
  assert.ok(followupWrite.old.includes('save-refresh-race'), 'refresh-safe save must advance originalText for the next edit');

  // A status outage is not an account transition. Reopening during an auth
  // 500 must retain the exact dirty buffer and the owner-scoped workspace.
  await evaluate(`(() => {
    localStorage.setItem('odysseus-code-workspace:owner', '/work');
    document.querySelector('.cm-content').focus();
  })()`);
  await command('Input.insertText', { text: '// auth-outage-dirty\n' });
  await waitFor("document.querySelector('.cm-content')?.textContent.includes('auth-outage-dirty')", 'auth-outage edit bytes');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●', 'auth-outage fixture must be dirty');
  await evaluate("(async () => { window.__authStatus = 500; try { await window.__codeModule.open(); } catch (_) {} })()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js') && document.querySelector('.cm-content')?.textContent.includes('auth-outage-dirty')", 'dirty buffer after auth outage');
  assert.equal(await evaluate("localStorage.getItem('odysseus-code-workspace:owner')"), '/work');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');

  // Workspace validation has its own tri-state result. A transient 5xx keeps
  // the preference and buffer; only an authoritative invalid response clears.
  await evaluate("(async () => { window.__authStatus = 200; window.__workspaceBrowseStatus = 500; await window.__codeModule.open(); })()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js') && document.querySelector('.cm-content')?.textContent.includes('auth-outage-dirty')", 'dirty buffer after workspace validation outage');
  assert.equal(await evaluate("localStorage.getItem('odysseus-code-workspace:owner')"), '/work');
  await evaluate("window.__workspaceBrowseStatus = 200");

  // A live policy event invalidates every owner-bound request. A transient
  // revalidation outage must keep the exact dirty buffer, while an
  // authoritative denial must remove its tab, path, and persisted selection.
  await evaluate(`(() => {
    window.__deferNextWrite = true;
    window.__testDeferredWriteStarted = false;
    window.__testDeferredWriteAborted = false;
    document.querySelector('.code-editor-save').click();
  })()`);
  await waitFor("window.__testDeferredWriteStarted", 'policy-event deferred save');
  await evaluate(`(() => {
    window.__workspaceBrowseStatus = 500;
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  })()`);
  await waitFor("window.__testDeferredWriteAborted", 'policy-event save abort');
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js') && document.querySelector('.cm-content')?.textContent.includes('auth-outage-dirty')", 'dirty buffer retained on policy outage');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');

  await evaluate(`(() => {
    window.__workspaceBrowseStatus = 403;
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  })()`);
  await waitFor("document.querySelectorAll('.code-editor-tab').length === 0 && document.querySelector('[data-code-root]')?.textContent === ''", 'revoked policy purges Code content');
  assert.equal(await evaluate("document.querySelector('.code-editor-window')?.textContent.includes('/work/main.js')"), false);
  assert.equal(await evaluate("localStorage.getItem('odysseus-code-workspace:owner')"), null);

  // Restore the fixture through the normal current-policy open path for the
  // independent account-transition race below.
  await evaluate("(async () => { window.__workspaceBrowseStatus = 200; await window.__codeModule.open(); })()");
  await waitFor("[...document.querySelectorAll('.code-tree-row.file')].some(row => row.querySelector('.code-tree-name')?.textContent === 'main.js')", 'workspace restored after policy fixture');
  await evaluate("[...document.querySelectorAll('.code-tree-row.file')].find(row => row.querySelector('.code-tree-name')?.textContent === 'main.js').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('main.js')", 'file restored after policy fixture');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'restored source readiness');

  // init.js emits a non-bubbling owner-ready event on document. Switching the
  // authenticated principal must abort the old save, purge tabs/reopen state,
  // and load only the new owner's validated workspace into the visible window.
  await evaluate(`(() => {
    window.__deferNextWrite = true;
    window.__testDeferredWriteStarted = false;
    window.__testDeferredWriteAborted = false;
    document.querySelector('.cm-content').focus();
  })()`);
  await command('Input.insertText', { text: '// stale-owner-write\n' });
  await waitFor("document.querySelector('.cm-content')?.textContent.includes('stale-owner-write')", 'old-owner deferred edit');
  await evaluate("document.querySelector('.code-editor-save').click()");
  await waitFor("window.__testDeferredWriteStarted && typeof window.__resolveTestWrite === 'function'", 'old-owner deferred save');
  await evaluate(`(() => {
    window.__testOwner = 'other-owner';
    document.dispatchEvent(new CustomEvent('openclank:auth-user-ready', {
      detail: { username: 'other-owner' },
    }));
  })()`);
  await waitFor("window.__testDeferredWriteAborted", 'old-owner save abort');
  await waitFor("[...document.querySelectorAll('.code-tree-name')].some(node => node.textContent === 'other.js')", 'new-owner workspace reload');
  const accountSwitch = await evaluate(`(() => ({
    tabs: document.querySelectorAll('.code-editor-tab').length,
    oldRows: [...document.querySelectorAll('.code-tree-name')].some(node => node.textContent === 'main.js'),
    reopenDisabled: document.querySelector('[data-code-reopen-closed]')?.disabled,
    staleBytesWritten: window.__testFiles['/work/main.js'].includes('stale-owner-write'),
  }))()`);
  assert.equal(accountSwitch.tabs, 0);
  assert.equal(accountSwitch.oldRows, false);
  assert.equal(accountSwitch.reopenDisabled, true);
  assert.equal(accountSwitch.staleBytesWritten, false);

  // File activation is ordered independently from file completion. Open A
  // twice and then B, complete B first, and ensure A still materializes once
  // without stealing B's active tab.
  await evaluate(`(() => {
    window.__deferReadPaths.add('/other/race-a.js');
    window.__deferReadPaths.add('/other/race-b.js');
    const row = path => [...document.querySelectorAll('.code-tree-row.file')].find(item => item.querySelector('.code-tree-name')?.textContent === path.split('/').at(-1));
    row('/other/race-a.js').click();
    row('/other/race-a.js').click();
    row('/other/race-b.js').click();
  })()`);
  await waitFor("window.__testDeferredReadStarted['/other/race-a.js'] === 1 && window.__testDeferredReadStarted['/other/race-b.js'] === 1 && typeof window.__resolveTestReads['/other/race-b.js'] === 'function'", 'two deferred concurrent opens');
  await evaluate("window.__resolveTestReads['/other/race-b.js']() ");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('race-b.js') && document.querySelectorAll('.code-editor-tab').length === 1", 'latest file completes first');
  await evaluate("window.__resolveTestReads['/other/race-a.js']() ");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('race-b.js') && document.querySelectorAll('.code-editor-tab').length === 2", 'earlier file materializes without activation');
  assert.equal(await evaluate("window.__testReads['/other/race-a.js']"), 1, 'same-path pending opens must deduplicate reads');
  assert.equal(await evaluate("window.__testReads['/other/race-b.js']"), 1);

  // Rename rekeys the live buffer. The CodeMirror Mod-S callback must resolve
  // buffer.path dynamically so it writes only the new destination.
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/other.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/other.js'", 'rename fixture tab');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'rename fixture readiness');
  await evaluate(`(() => {
    document.querySelector('.code-tree-row.file[data-code-tree-path="/other/other.js"]')
      .dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 80, clientY: 60 }));
  })()`);
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'rename action prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'rename'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'rename destination prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'renamed.js'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title.endsWith('renamed.js') && [...document.querySelectorAll('.code-tree-row.file')].some(row => row.querySelector('.code-tree-name')?.textContent === 'renamed.js')", 'renamed live buffer');
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// renamed-command-save\n' });
  const renameWriteCount = await evaluate("window.__testWrites.length");
  const primaryModifier = process.platform === 'darwin' ? 4 : 2;
  await command('Input.dispatchKeyEvent', { type: 'keyDown', key: 's', code: 'KeyS', windowsVirtualKeyCode: 83, modifiers: primaryModifier });
  await command('Input.dispatchKeyEvent', { type: 'keyUp', key: 's', code: 'KeyS', windowsVirtualKeyCode: 83, modifiers: primaryModifier });
  await waitFor(`window.__testWrites.length === ${renameWriteCount + 1} && document.querySelector('.code-editor-save')?.textContent === 'Saved'`, 'Mod-S after rename');
  const renamedWrite = await evaluate("window.__testWrites.at(-1)");
  assert.equal(renamedWrite.path, '/other/renamed.js');
  assert.equal(await evaluate("window.__testFiles['/other/other.js']"), undefined);
  assert.equal(await evaluate("window.__testFiles['/other/renamed.js'].includes('renamed-command-save')"), true);

  // A directory rename reserves the whole subtree. Open and edit a second
  // child only after the host mutation has started; completion must rekey both
  // the original tab and the late buffer without losing its unsaved bytes.
  await evaluate("document.querySelector('.code-tree-row.directory[data-code-tree-path=\"/other/rename-race\"]').click()");
  await waitFor("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/rename-race/late.js\"]')", 'rename-race directory children');
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/rename-race/first.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/rename-race/first.js'", 'initial rename subtree buffer');
  await evaluate(`(() => {
    window.__deferRename = true;
    window.__testRenameStarted = false;
    document.querySelector('.code-tree-row.directory[data-code-tree-path="/other/rename-race"]')
      .dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 80, clientY: 60 }));
  })()`);
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'subtree rename action prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'rename'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'subtree rename destination prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'renamed-race'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("window.__testRenameStarted && typeof window.__resolveTestRename === 'function'", 'deferred subtree rename');
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/rename-race/late.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/rename-race/late.js'", 'late rename subtree buffer');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'late rename readiness');
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// typed-during-rename\n' });
  await waitFor("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent === '●'", 'dirty late rename buffer');
  await evaluate("window.__resolveTestRename()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/renamed-race/late.js' && [...document.querySelectorAll('.code-editor-tab')].some(tab => tab.title === '/other/renamed-race/first.js')", 'all subtree buffers rekeyed after rename');
  assert.equal(await evaluate("[...document.querySelectorAll('.code-editor-tab')].some(tab => tab.title.startsWith('/other/rename-race/'))"), false);
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');
  assert.equal(await evaluate("document.querySelector('.cm-content').textContent.includes('typed-during-rename')"), true);
  assert.equal(await evaluate("window.__testFiles['/other/renamed-race/late.js'] !== undefined && window.__testFiles['/other/rename-race/late.js'] === undefined"), true);

  // Recoverable directory Trash uses the same subtree reservation, but its
  // safe result is to restore the host item whenever a late child buffer
  // appears. The newly typed buffer stays at its original path and stays dirty.
  await evaluate("document.querySelector('.code-tree-row.directory[data-code-tree-path=\"/other/trash-tree-race\"]').click()");
  await waitFor("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/trash-tree-race/late.js\"]')", 'trash-race directory children');
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/trash-tree-race/first.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/trash-tree-race/first.js'", 'initial trash subtree buffer');
  await evaluate(`(() => {
    window.__deferTrash = true;
    window.__testTrashStarted = false;
    document.querySelector('.code-tree-row.directory[data-code-tree-path="/other/trash-tree-race"]')
      .dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 80, clientY: 60 }));
  })()`);
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'subtree trash action prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'trash'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("document.querySelector('#styled-confirm-overlay')?.style.display !== 'none'", 'subtree trash confirmation');
  await evaluate("document.querySelector('#styled-confirm-ok').click()");
  await waitFor("window.__testTrashStarted && typeof window.__resolveTestTrash === 'function'", 'deferred subtree trash');
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/trash-tree-race/late.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/trash-tree-race/late.js'", 'late trash subtree buffer');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'late trash readiness');
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// typed-during-tree-trash\n' });
  await waitFor("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent === '●'", 'dirty late trash buffer');
  await evaluate("window.__resolveTestTrash()");
  await waitFor("document.querySelector('.code-editor-window .copal-workspace-status')?.textContent.includes('changed while Trash was running')", 'subtree trash restored');
  assert.equal(await evaluate("window.__testFiles['/other/trash-tree-race/first.js'] !== undefined && window.__testFiles['/other/trash-tree-race/late.js'] !== undefined"), true);
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active')?.title"), '/other/trash-tree-race/late.js');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');
  assert.equal(await evaluate("document.querySelector('.cm-content').textContent.includes('typed-during-tree-trash')"), true);

  // Recoverable Trash must not destroy edits typed while the host mutation is
  // in flight. The action restores the item and retains the dirty buffer.
  await evaluate("document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/trash-race.js\"]').click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/trash-race.js'", 'trash race fixture tab');
  await waitFor("document.querySelector('.code-editor-codemirror')?.dataset.syntaxReady === 'ready'", 'trash race readiness');
  await evaluate("window.__deferTrash = true; window.__testTrashStarted = false; document.querySelector('.code-tree-row.file[data-code-tree-path=\"/other/trash-race.js\"]').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, clientX: 80, clientY: 60 }))");
  await waitFor("document.querySelector('#styled-prompt-overlay')?.style.display !== 'none'", 'trash action prompt');
  await evaluate("document.querySelector('#styled-prompt-input').value = 'trash'; document.querySelector('#styled-prompt-ok').click()");
  await waitFor("document.querySelector('#styled-confirm-overlay')?.style.display !== 'none'", 'trash confirmation');
  await evaluate("document.querySelector('#styled-confirm-ok').click()");
  await waitFor("window.__testTrashStarted && typeof window.__resolveTestTrash === 'function'", 'deferred recoverable trash');
  await evaluate("document.querySelector('.cm-content').focus()");
  await command('Input.insertText', { text: '// typed-during-trash\n' });
  await evaluate("window.__resolveTestTrash()");
  await waitFor("document.querySelector('.code-editor-window .copal-workspace-status')?.textContent.includes('changed while Trash was running')", 'trash race recovery');
  assert.equal(await evaluate("window.__testFiles['/other/trash-race.js'] !== undefined"), true);
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active')?.title"), '/other/trash-race.js');
  assert.equal(await evaluate("document.querySelector('.code-editor-tab.active .code-editor-tab-dot')?.textContent"), '●');
  await evaluate("[...document.querySelectorAll('.code-editor-tab')].find(tab => tab.title === '/other/renamed.js')?.click()");
  await waitFor("document.querySelector('.code-editor-tab.active')?.title === '/other/renamed.js'", 'return to reload fixture');

  // A disk reload has its own abort controller and exact workspace identity.
  // Hold the authorized read, switch accounts, and prove teardown aborts it
  // before stale bytes can repopulate the new principal's editor.
  await evaluate(`(() => {
    window.__testFiles['/other/renamed.js'] = 'export const diskCopy = true;\\n';
    window.__testFingerprints['/other/renamed.js'] = 'renamed-external-fp';
    document.querySelector('button[title=Refresh]').click();
  })()`);
  await waitFor("document.querySelector('.code-editor-save-error') && document.querySelector('.code-editor-reload-disk')", 'reload race warning');
  await evaluate("window.__deferReadPaths.add('/other/renamed.js'); document.querySelector('.code-editor-reload-disk').click()");
  await waitFor("getComputedStyle(document.querySelector('#styled-confirm-overlay')).display !== 'none'", 'reload disk prompt');
  await evaluate("document.querySelector('#styled-confirm-ok').click()");
  await waitFor("window.__testDeferredReadStarted['/other/renamed.js'] === 1 && typeof window.__resolveTestReads['/other/renamed.js'] === 'function'", 'deferred reload read');
  await evaluate(`(() => {
    window.__testOwner = 'owner';
    document.dispatchEvent(new CustomEvent('openclank:auth-user-ready', {
      detail: { username: 'owner' },
    }));
  })()`);
  await waitFor("window.__testDeferredReadAborted['/other/renamed.js'] === true", 'stale reload abort');
  await waitFor("[...document.querySelectorAll('.code-tree-name')].some(node => node.textContent === 'src')", 'workspace after reload teardown');
  assert.equal(await evaluate("document.querySelectorAll('.code-editor-tab').length"), 0);
  assert.equal(await evaluate("[...document.querySelectorAll('.code-editor-tab')].some(tab => tab.title === '/other/renamed.js')"), false);

  // Files hands Code only an opaque Host ResourceRef. Code resolves the stable
  // Workspace ID through the purpose-bound API, switches through the normal
  // dirty coordinator, and opens the server-selected relative file.
  await evaluate("window.__codeNamed.openResource('rr1.host-resource')");
  await waitFor("document.querySelector('[data-code-root]')?.textContent === '/resource' && document.querySelector('.code-editor-tab.active')?.title === '/resource/resource.js'", 'opaque Files to Code handoff');
  assert.deepEqual(await evaluate("window.__resourceWorkspaceRequest"), {
    resource_ref: 'rr1.host-resource',
    purpose: 'app_folder',
  });
  assert.equal(await evaluate("localStorage.getItem('odysseus-code-workspace-id:owner')"), 'workspace-resource');

  // Code sends only the stable Workspace ID and its relative file name. Files
  // resolves fresh opaque Host refs, opens the containing folder, and selects
  // the matching stable resource without putting /resource in the request.
  await evaluate("document.querySelector('.code-editor-show-in-files').click()");
  await waitFor("window.__showInFilesRequest?.workspace_id === 'workspace-resource'", 'Code Show in Files handoff');
  assert.equal(await evaluate("window.__showInFilesRequests.some(request => request.workspace_id === 'workspace-resource' && request.relative_path === 'resource.js')"), true);
  assert.deepEqual(await evaluate("window.__showInFilesRequests.find(request => request.relative_path === 'resource.js')"), {
    workspace_id: 'workspace-resource',
    relative_path: 'resource.js',
  });
  assert.equal(await evaluate("JSON.stringify(window.__showInFilesRequest).includes('/resource')"), false);

  const asyncClose = await evaluate(`(async () => {
    const { createOpenClankWindow } = await import('/static/js/copal/windows.js?close-acceptance=' + Date.now());
    const Modals = await import('/static/js/modalManager.js');
    let closed = 0;
    const testWindow = createOpenClankWindow({
      id: 'qol-async-close-window',
      label: 'Async close test',
      onBeforeClose: async () => { await new Promise(resolve => setTimeout(resolve, 10)); return true; },
      onClosed: () => { closed += 1; },
    });
    testWindow.show();
    const immediate = testWindow.requestClose();
    const stayedVisible = testWindow.visible;
    await new Promise(resolve => setTimeout(resolve, 30));
    const direct = { immediate, stayedVisible, visibleAfter: testWindow.visible, closed };
    testWindow.destroy();

    let managerClosed = 0;
    let allowManagerClose = false;
    const managedWindow = createOpenClankWindow({
      id: 'qol-manager-close-window',
      label: 'Manager close test',
      onBeforeClose: async () => {
        await new Promise(resolve => setTimeout(resolve, 10));
        return allowManagerClose;
      },
      onClosed: () => { managerClosed += 1; },
    });
    managedWindow.show();
    Modals.minimize(managedWindow.id);
    const cancelled = await Modals.close(managedWindow.id);
    const registeredAfterCancel = Modals.isRegistered(managedWindow.id);
    const minimizedAfterCancel = Modals.isMinimized(managedWindow.id);
    Modals.restore(managedWindow.id);
    const visibleAfterRestore = managedWindow.visible;
    allowManagerClose = true;
    const allowed = await Modals.close(managedWindow.id);
    const registeredAfterClose = Modals.isRegistered(managedWindow.id);
    const result = {
      direct,
      cancelled,
      registeredAfterCancel,
      minimizedAfterCancel,
      visibleAfterRestore,
      allowed,
      registeredAfterClose,
      managerClosed,
    };
    managedWindow.destroy();
    return result;
  })()`);
  assert.equal(asyncClose.direct.immediate, false);
  assert.equal(asyncClose.direct.stayedVisible, true);
  assert.equal(asyncClose.direct.visibleAfter, false);
  assert.equal(asyncClose.direct.closed, 1);
  assert.equal(asyncClose.cancelled, false);
  assert.equal(asyncClose.registeredAfterCancel, true);
  assert.equal(asyncClose.minimizedAfterCancel, true);
  assert.equal(asyncClose.visibleAfterRestore, true);
  assert.equal(asyncClose.allowed, true);
  assert.equal(asyncClose.registeredAfterClose, false);
  assert.equal(asyncClose.managerClosed, 1);
  process.stdout.write(JSON.stringify({ foldersFirst: 'pass', syntax: 'pass', tabLifecycle: 'pass', dirtyCoordinator: 'pass', reopenFromDisk: 'pass', concurrentOpenOrder: 'pass', reloadTeardown: 'pass', trashRace: 'pass', subtreeRenameRace: 'pass', subtreeTrashRace: 'pass', casSave: 'pass', saveRefreshRace: 'pass', renameCommandSave: 'pass', authOutage: 'pass', policyRevalidation: 'retain-5xx-purge-403', accountSwitch: 'pass', resourceRefOpen: 'pass', showInFiles: 'opaque-workspace-relative', pane: 'pass', externalChange: 'pass' }) + '\n');
} finally {
  if (socket) socket.close();
  browser.kill('SIGTERM');
  await new Promise(resolve => browser.once('exit', resolve));
  ownedStaticServer?.kill('SIGTERM');
  fs.rmSync(profile, { recursive: true, force: true });
}
