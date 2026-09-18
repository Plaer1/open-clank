#!/usr/bin/env node

import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';

const base = (process.argv[2] || 'http://127.0.0.1:7777').replace(/\/$/, '');
const chromeCandidates = [
  process.env.OPEN_CLANK_CHROME_BIN,
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/usr/bin/chromium',
  '/usr/bin/chromium-browser',
  '/usr/bin/google-chrome',
].filter(Boolean);
const chrome = chromeCandidates.find(candidate => fs.existsSync(candidate));
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
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'openclank-files-window-'));
const chromium = spawn(chrome, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  '--window-size=1280,1000',
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank',
], { stdio: 'ignore' });

let socket;
try {
  let target;
  for (let attempt = 0; attempt < 400; attempt += 1) {
    try {
      const targets = await fetch(`http://127.0.0.1:${port}/json`).then(response => response.json());
      target = targets?.find(item => item.type === 'page' && item.webSocketDebuggerUrl);
      if (target) break;
      await new Promise(resolve => setTimeout(resolve, 50));
    } catch { await new Promise(resolve => setTimeout(resolve, 50)); }
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
      await new Promise(resolve => setTimeout(resolve, 60));
    }
    throw new Error(`Timed out waiting for ${label}`);
  };

  await command('Page.enable');
  await command('Runtime.enable');
  await command('Page.navigate', { url: `${base}/login` });
  await waitFor("document.readyState === 'complete'", 'Open Clank origin');
  await evaluate(`(async () => {
    localStorage.clear();
    window.__filesOwner = 'owner';
    window.__filesRootsStatus = 200;
    window.__hostPolicyVisible = true;
    localStorage.setItem('odysseus-files-favorites:owner', JSON.stringify(['/work']));
    window.__filesRequests = [];
    window.__filesRequestMethods = [];
    window.__filesChildrenBodies = [];
    window.__filesOpenedTarget = '';
    window.__filesOpenedResource = '';
    window.__filesOpenedPayload = null;
    window.__galleryOpenedResource = '';
    window.__galleryOpenedPayload = null;
    window.__fileArchived = false;
    window.__chatArchived = false;
    window.__filesPlaces = [];
    window.__filesWatchActive = null;
    window.__hostChanged = false;
    window.__codeOpenedResource = '';
    window.__workspaceResourceRequests = [];
    window.__workspaceUpdates = [];
    window.__revealRequests = [];
    window.__reissueRequests = [];
    window.__exactDownloads = [];
    const nativeAnchorClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      if (String(this.href || '').includes('/api/files-v1/content/')) {
        window.__exactDownloads.push({ href: this.getAttribute('href'), download: this.download });
        return;
      }
      return nativeAnchorClick.call(this);
    };
    window.__workspaceCatalog = [{
      workspace: {
        id: 'workspace-host-src', name: 'Source', location_id: 'location-work', relative_folder: 'src',
        archived: false, generation: 7, revision: 1,
      },
      availability: 'available',
      resource: {
        id: 'resource-host-src', ref: 'rr1.host-src', provider: 'host', kind: 'folder',
        capabilities: ['children', 'stat', 'watch'], name: 'src', preview_kind: null,
      },
    }];
    window.__addedLocations = [];
    window.__policyLocations = [];
    window._isAdmin = true;
    window.codeEditorModule = {
      async openResource(resourceRef) {
        window.__codeOpenedResource = resourceRef;
        return true;
      },
      async openPath(filePath) {
        window.__codeOpenedResource = filePath;
        return true;
      },
    };
    window.copalModule = {
      async openResource(resourceRef) {
        const response = await fetch('/api/files-v1/open-resource', {
          method: 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resource_ref: resourceRef }),
        });
        if (!response.ok) throw new Error('exact open failed');
        const data = await response.json();
        window.__filesOpenedTarget = data.target.app;
        window.__filesOpenedResource = resourceRef;
        window.__filesOpenedPayload = data.payload;
      },
    };
    window.galleryModule = {
      async openResource(resourceRef) {
        const response = await fetch('/api/files-v1/open-resource', {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resource_ref: resourceRef }),
        });
        if (!response.ok) throw new Error('Gallery exact open failed');
        const data = await response.json();
        window.__galleryOpenedResource = data.resource.ref;
        window.__galleryOpenedPayload = data.payload;
      },
    };
    const copalLauncher = document.createElement('button');
    copalLauncher.dataset.copalView = 'notes';
    copalLauncher.hidden = true;
    copalLauncher.addEventListener('click', () => { window.__filesOpenedTarget = 'copal_notes'; });
    document.body.append(copalLauncher);
    const json = body => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
    const problem = (code, message, status = 409) => new Response(JSON.stringify({ detail: { code, message } }), { status, headers: { 'Content-Type': 'application/json' } });
    window.fetch = async (input, init = {}) => {
      const url = new URL(String(input), location.origin);
      window.__filesRequests.push(url.pathname + url.search);
      window.__filesRequestMethods.push([String(init.method || 'GET').toUpperCase(), url.pathname]);
      if (url.pathname === '/api/auth/status') return json({ ok: true, username: window.__filesOwner, is_admin: true });
      if (url.pathname === '/api/auth/users') return json({ users: [] });
      if (url.pathname === '/api/file-policy/state') return json({
        version: 1, generation: 11, subject_id: 'account-owner', is_admin: true,
        reset_scopes: ['chat', 'workspace', 'location', 'all_agent'],
        locations: window.__policyLocations, workspaces: [], bindings: [],
      });
      if (url.pathname === '/api/file-policy/location-presets') return json({
        version: 1, home: '/work', whole_roots: [{ name: 'Whole disk /', path: '/' }], os_managed: true,
      });
      if (url.pathname === '/api/file-policy/locations' && String(init.method || '').toUpperCase() === 'POST') {
        const body = JSON.parse(init.body || '{}');
        window.__addedLocations.push(body);
        window.__policyLocations = [{
          id: 'location-whole-root', canonical_path: body.path, display_path: body.path,
          kind: body.kind, capabilities: body.capabilities, availability: 'available', enabled: true,
        }];
        return json({ location: window.__policyLocations[0], agent_binding: body.agent_access ? { id: 'binding-agent' } : null });
      }
      if (url.pathname === '/api/files-v1/roots') {
        if (window.__filesRootsStatus !== 200) {
          return problem('provider_unavailable', 'facade temporarily unavailable', window.__filesRootsStatus);
        }
        return json({
        entries: [{
          id: window.__filesOwner === 'other' ? 'resource-host-other' : 'resource-host',
          ref: window.__filesOwner === 'other' ? 'rr1.host-root-other' : 'rr1.host-root', provider: 'host', kind: 'provider_root',
          capabilities: ['children', 'stat'], name: 'Host locations', preview_kind: null, sort_keys: ['name', 'kind'],
        }, {
          id: 'resource-copal', ref: 'rr1.copal-root', provider: 'copal', kind: 'provider_root',
          capabilities: ['children', 'stat', 'search'], name: 'Copal', preview_kind: null, sort_keys: ['name'],
        }, {
          id: 'resource-gallery', ref: 'rr1.gallery-root', provider: 'gallery', kind: 'provider_root',
          capabilities: ['children', 'stat', 'search'], name: 'Gallery', preview_kind: null, sort_keys: ['name'],
        }, {
          id: 'resource-library', ref: 'rr1.library-root', provider: 'library', kind: 'provider_root',
          capabilities: ['children', 'stat', 'search'], name: 'Library', preview_kind: null, sort_keys: ['name'],
        }],
        providers: { host: { available: true }, copal: { available: true }, gallery: { available: true }, library: { available: true } },
        });
      }
      if (url.pathname === '/api/files-v1/workspace') {
        const body = JSON.parse(init.body || '{}');
        window.__workspaceResourceRequests.push(body);
        return json({
          version: 1,
          workspace: { id: 'workspace-host-src', name: 'src' },
          open_relative: body.resource_ref === 'rr1.host-src' ? '' : 'README.md',
        });
      }
      if (url.pathname === '/api/files-v1/workspaces' && String(init.method || 'GET').toUpperCase() === 'GET') {
        return json({
          version: 1, generation: 7,
          entries: window.__filesOwner === 'owner' ? window.__workspaceCatalog.filter(row => !row.workspace.archived) : [],
        });
      }
      if (url.pathname === '/api/files-v1/workspaces/workspace-host-src' && String(init.method || '').toUpperCase() === 'PATCH') {
        const body = JSON.parse(init.body || '{}');
        window.__workspaceUpdates.push(body);
        const row = window.__workspaceCatalog[0];
        if (body.name) row.workspace.name = body.name;
        if (body.archived === true) row.workspace.archived = true;
        row.workspace.revision += 1;
        return json({ version: 1, generation: 8, ...row, resource: row.workspace.archived ? null : row.resource });
      }
      if (url.pathname === '/api/file-policy/workspaces/workspace-host-src/resolve') {
        return json({ workspace: { id: 'workspace-host-src', path: '/work/src', name: window.__workspaceCatalog[0].workspace.name } });
      }
      if (url.pathname === '/api/files-v1/children') {
        const body = JSON.parse(init.body || '{}');
        window.__filesChildrenBodies.push(body);
        if (body.parent_ref === 'rr1.host-root-other') return json({ entries: [{
          id: 'resource-host-other-home', ref: 'rr1.host-other-home', provider: 'host', kind: 'folder',
          capabilities: ['children', 'stat', 'watch'], name: 'Home', preview_kind: null,
          provenance: { domain: 'host', favorite: true },
        }], next_cursor: null, total: 1 });
        if (body.parent_ref === 'rr1.host-other-home') return json({ entries: [{
          id: 'resource-host-other-file', ref: 'rr1.host-other-file', provider: 'host', kind: 'file',
          capabilities: ['stat', 'download'], name: 'other.txt', size: 5, mime_type: 'text/plain',
        }], next_cursor: null, total: 1 });
        if (body.parent_ref === 'rr1.host-root') return json({ entries: window.__hostPolicyVisible ? [{
          id: 'resource-host-home', ref: 'rr1.host-home', provider: 'host', kind: 'folder',
          capabilities: ['children', 'stat', 'watch'], name: 'Home', preview_kind: null,
          provenance: { domain: 'host', favorite: true },
        }] : [], next_cursor: null, total: window.__hostPolicyVisible ? 1 : 0 });
        if (body.parent_ref === 'rr1.host-home') return json({ entries: [{
          id: 'resource-host-src', ref: 'rr1.host-src', provider: 'host', kind: 'folder',
          capabilities: ['children', 'stat', 'watch'], name: 'src', modified_unix_ms: 1700000000000,
        }, {
          id: 'resource-host-readme', ref: 'rr1.host-readme', provider: 'host', kind: 'file',
          capabilities: ['stat', 'open', 'preview', 'download'], name: 'README.md', size: 2097152,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000000001,
        }, {
          id: 'resource-host-image', ref: 'rr1.host-image', provider: 'host', kind: 'file',
          capabilities: ['stat', 'preview', 'download'], name: 'Native image.png', size: 68,
          mime_type: 'image/png', preview_kind: 'image', modified_unix_ms: 1700000000002,
        }, {
          id: 'resource-host-track', ref: 'rr1.host-track', provider: 'host', kind: 'file',
          capabilities: ['stat', 'preview', 'download'], name: 'track.mp3', size: 256,
          mime_type: 'audio/mpeg', preview_kind: 'audio', modified_unix_ms: 1700000000003,
        }, ...(window.__hostChanged ? [{
          id: 'resource-host-watched', ref: 'rr1.host-watched', provider: 'host', kind: 'file',
          capabilities: ['stat', 'download'], name: 'watched-change.txt', size: 7,
          mime_type: 'text/plain', modified_unix_ms: 1700000000004,
        }] : [])], next_cursor: null, total: window.__hostChanged ? 5 : 4 });
        if (body.parent_ref === 'rr1.host-src') return json({ entries: [{
          id: 'resource-host-sub', ref: 'rr1.host-sub', provider: 'host', kind: 'folder',
          capabilities: ['children', 'stat', 'watch'], name: 'sub', modified_unix_ms: 10,
        }, {
          id: 'resource-host-lib', ref: 'rr1.host-lib', provider: 'host', kind: 'file',
          capabilities: ['stat', 'download'], name: 'lib.rs', size: 81,
          mime_type: 'text/x-rust', preview_kind: 'text', modified_unix_ms: 20,
        }], next_cursor: null, total: 2 });
        if (body.parent_ref === 'rr1.host-sub') return json({ entries: [
          { id: 'resource-host-alpha', ref: 'rr1.host-alpha', provider: 'host', kind: 'file', capabilities: ['stat', 'preview', 'download'], name: 'alpha.txt', size: 2, mime_type: 'text/plain', preview_kind: 'text', modified_unix_ms: 20 },
          { id: 'resource-host-nested', ref: 'rr1.host-nested', provider: 'host', kind: 'file', capabilities: ['stat', 'preview', 'download'], name: 'nested.txt', size: 4, mime_type: 'text/plain', preview_kind: 'text', modified_unix_ms: 30 },
          { id: 'resource-host-zeta', ref: 'rr1.host-zeta', provider: 'host', kind: 'file', capabilities: ['stat', 'preview', 'download'], name: 'zeta.txt', size: 8, mime_type: 'text/plain', preview_kind: 'text', modified_unix_ms: 10 },
        ], next_cursor: null, total: 3 });
        if (body.parent_ref === 'rr1.gallery-root') return json({ entries: [{
          id: 'resource-gallery-albums', ref: 'rr1.gallery-albums', provider: 'gallery', kind: 'virtual_folder',
          capabilities: ['children', 'stat', 'search'], name: 'Albums', preview_kind: null,
          sort_keys: ['name', 'modified'],
        }, {
          id: 'resource-gallery-photos', ref: 'rr1.gallery-photos', provider: 'gallery', kind: 'virtual_folder',
          capabilities: ['children', 'stat', 'search'], name: 'Photos', preview_kind: null,
          sort_keys: ['name', 'kind', 'modified', 'size'],
        }], next_cursor: null, total: 2, sort: body.sort, sort_keys: ['name'] });
        if (body.parent_ref === 'rr1.gallery-albums') return json({ entries: [{
          id: 'resource-gallery-album-a', ref: 'rr1.gallery-album-a', provider: 'gallery', kind: 'album',
          capabilities: ['children', 'stat', 'open'], name: 'Summer', modified_unix_ms: 1700000002000,
          sort_keys: ['name', 'kind', 'modified', 'size'],
        }], next_cursor: null, total: 1, sort: body.sort, sort_keys: ['name', 'modified'] });
        if (body.parent_ref === 'rr1.gallery-photos') return json({ entries: [{
          id: 'resource-gallery-image', ref: 'rr1.gallery-image', provider: 'gallery', kind: 'image',
          capabilities: ['stat', 'open', 'preview', 'download', 'favorite'], name: 'Opaque lake.png', size: 68,
          mime_type: 'image/png', preview_kind: 'image', modified_unix_ms: 1700000004000,
        }], next_cursor: null, total: 1, sort: body.sort, sort_keys: ['name', 'kind', 'modified', 'size'] });
        if (body.parent_ref === 'rr1.copal-root') return json({ entries: [{
          id: 'resource-documents', ref: 'rr1.documents', provider: 'copal', kind: 'virtual_folder',
          capabilities: ['children', 'stat', 'search'], name: 'Documents', preview_kind: null,
        }], next_cursor: null, total: 1 });
        if (body.parent_ref === 'rr1.documents' && !body.cursor) return json({ entries: [{
          id: 'resource-managed-note', ref: 'rr1.managed-note', provider: 'copal', kind: 'document',
          capabilities: ['stat', 'open', 'preview', 'download'], name: 'Managed note', size: 400000,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000000000,
        }], next_cursor: 'fc1.more', total: 2 });
        if (body.parent_ref === 'rr1.documents' && body.cursor === 'fc1.more') return json({ entries: [{
          id: 'resource-second-note', ref: 'rr1.second-note', provider: 'copal', kind: 'document',
          capabilities: ['stat', 'open', 'preview', 'download'], name: 'Second note', size: 12,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000001000,
        }], next_cursor: null, total: 2 });
        if (body.parent_ref === 'rr1.library-root') return json({ entries: [{
          id: 'resource-library-documents', ref: 'rr1.library-documents', provider: 'library', kind: 'virtual_folder',
          capabilities: ['children', 'stat', 'search'], name: 'Documents', preview_kind: null,
          sort_keys: ['name', 'kind', 'modified', 'size'],
        }, {
          id: 'resource-library-chats', ref: 'rr1.library-chats', provider: 'library', kind: 'virtual_folder',
          capabilities: ['children', 'stat', 'search'], name: 'Chats', preview_kind: null,
          sort_keys: ['name', 'modified'],
        }], next_cursor: null, total: 2, sort: body.sort, sort_keys: ['name'] });
        if (body.parent_ref === 'rr1.library-documents') return json({ entries: window.__fileArchived ? [] : [{
          id: 'resource-library-action', ref: 'rr1.library-action', provider: 'library', kind: 'document',
          capabilities: ['stat', 'open', 'preview', 'download', 'archive'], name: 'Archive from Files', size: 10,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000002000,
        }], next_cursor: null, total: window.__fileArchived ? 0 : 1, sort: body.sort, sort_keys: ['name', 'kind', 'modified', 'size'] });
        if (body.parent_ref === 'rr1.library-documents-reveal') return json({ entries: [{
          id: 'resource-library-document', ref: 'rr1.library-document-listed', provider: 'library', kind: 'document',
          capabilities: ['stat', 'open', 'preview', 'download'], name: 'Exact Library document', size: 20,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000004000,
        }], next_cursor: null, total: 1 });
        if (body.parent_ref === 'rr1.library-chats-reveal') return json({ entries: [{
          id: 'resource-library-chat-exact', ref: 'rr1.library-chat-listed', provider: 'library', kind: 'chat',
          capabilities: ['stat', 'open', 'download'], name: 'Opaque exact chat', size: 77,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000005000,
        }], next_cursor: null, total: 1 });
        if (body.parent_ref === 'rr1.library-chats') return json({ entries: window.__chatArchived ? [] : [{
          id: 'resource-library-chat', ref: 'rr1.library-chat', provider: 'library', kind: 'chat',
          capabilities: ['stat', 'open', 'download', 'archive'], name: 'Archive chat from Files', size: 2,
          mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000003000,
        }], next_cursor: null, total: window.__chatArchived ? 0 : 1, sort: body.sort, sort_keys: ['name', 'modified'] });
      }
      if (url.pathname === '/api/files-v1/places' && String(init.method || 'GET').toUpperCase() === 'GET') {
        return json({ version: 1, entries: window.__filesOwner === 'owner' ? window.__filesPlaces : [] });
      }
      if (url.pathname === '/api/files-v1/watch') {
        const body = JSON.parse(init.body || '{}');
        const stream = new ReadableStream({
          start(controller) {
            window.__filesWatchActive = { resourceRef: body.resource_ref, controller, cancelled: false };
          },
          cancel() {
            if (window.__filesWatchActive?.resourceRef === body.resource_ref) {
              window.__filesWatchActive.cancelled = true;
            }
          },
        });
        return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
      }
      if (url.pathname === '/api/files-v1/places' && String(init.method || '').toUpperCase() === 'POST') {
        const body = JSON.parse(init.body || '{}');
        const source = {
          'rr1.host-src': { id: 'resource-host-src', ref: 'rr1.host-src-place', provider: 'host', kind: 'folder', capabilities: ['children', 'stat'], name: 'src' },
        }[body.resource_ref];
        if (!source) return new Response(JSON.stringify({ detail: 'unavailable' }), { status: 404, headers: { 'Content-Type': 'application/json' } });
        const resource = { ...source, place_id: 'place-' + 'a'.repeat(32) };
        window.__filesPlaces = [resource];
        return json({ version: 1, resource });
      }
      if (url.pathname === '/api/files-v1/places/place-' + 'a'.repeat(32) && String(init.method || '').toUpperCase() === 'DELETE') {
        window.__filesPlaces = [];
        return json({ version: 1, removed: true });
      }
      if (url.pathname === '/api/files-v1/search') {
        const body = JSON.parse(init.body || '{}');
        return json({
          query: body.query,
          entries: [{
            id: 'resource-global-note', ref: 'rr1.global-note', provider: 'copal', kind: 'document',
            capabilities: ['stat', 'open', 'preview', 'download'], name: String(body.query) + ' note', size: 12,
            mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000001000,
          }],
          truncated: false,
          providers: { host: { available: true }, copal: { available: true }, gallery: { available: false }, library: { available: true } },
        });
      }
      if (url.pathname === '/api/files-v1/action') {
        const body = JSON.parse(init.body || '{}');
        if (body.action === 'archive.set' && body.resource_ref === 'rr1.library-action' && body.args?.value === true) {
          window.__fileArchived = true;
          return json({
            version: 1, action: 'archive.set', state: { archived: true },
            resource: {
              id: 'resource-library-action', ref: 'rr1.library-action-archived', provider: 'library', kind: 'document',
              capabilities: ['stat', 'open', 'preview', 'download', 'restore'], name: 'Archive from Files',
            },
          });
        }
        if (body.action === 'archive.set' && body.resource_ref === 'rr1.library-chat' && body.args?.value === true) {
          window.__chatArchived = true;
          return json({
            version: 1, action: 'archive.set', state: { archived: true },
            resource: {
              id: 'resource-library-chat', ref: 'rr1.library-chat-archived', provider: 'library', kind: 'chat',
              capabilities: ['stat', 'open', 'download', 'restore'], name: 'Archive chat from Files',
            },
          });
        }
        if (body.action === 'open' && body.resource_ref === 'rr1.managed-note') return json({
          version: 1,
          action: 'open',
          target: { app: 'copal_notes' },
          exact: true,
          resource: {
            id: 'resource-managed-note', ref: 'rr1.managed-note-refreshed', provider: 'copal', kind: 'document',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'Managed note', size: 400000,
            mime_type: 'text/markdown', preview_kind: 'text', modified_unix_ms: 1700000000000,
          },
        });
        if (body.action === 'open' && body.resource_ref === 'rr1.host-readme') return json({
          version: 1, action: 'open', target: { app: 'editor' }, exact: true,
          resource: {
            id: 'resource-host-readme', ref: 'rr1.host-readme-refreshed', provider: 'host', kind: 'file',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'README.md', mime_type: 'text/markdown',
          },
        });
        if (body.action === 'open' && body.resource_ref === 'rr1.gallery-image') return json({
          version: 1, action: 'open', target: { app: 'gallery' }, exact: true,
          resource: {
            id: 'resource-gallery-image', ref: 'rr1.gallery-image-refreshed', provider: 'gallery', kind: 'image',
            capabilities: ['stat', 'open', 'preview', 'download', 'favorite'], name: 'Opaque lake.png',
            mime_type: 'image/png', preview_kind: 'image',
          },
        });
      }
      if (url.pathname === '/api/files-v1/open-resource') {
        const body = JSON.parse(init.body || '{}');
        if (body.resource_ref === 'rr1.host-readme-refreshed') return json({
          version: 1, target: { app: 'editor' },
          resource: {
            id: 'resource-host-readme', ref: 'rr1.host-readme-exact', provider: 'host', kind: 'file',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'README.md',
          },
          payload: {
            name: 'README.md', kind: 'text', corpus: 'host', text: '# Host README', representation: 'markdown',
            encoding: 'utf-8', newline: '\\n', bom_bytes: 0, mode: 'markdown', language: 'markdown',
            properties: {}, relations: [], tags: [], read_only: false,
            resource: {
              key: { accountId: 'account-owner', workspaceId: 'host', provider: 'host', resourceId: 'host-readme' },
              revision: { kind: 'hostFingerprint', value: 'host-readme-v1' }, representation: 'markdown',
              locator: { displayName: 'README.md', locationLabel: 'README.md', opaqueRef: 'rr1.host-readme-refreshed' },
              metadata: { encoding: 'utf-8', newline: '\\n', language: 'markdown', mode: 'markdown', bomBytes: 0 },
              capabilities: { read: true, edit: true, rename: false, move: false, trash: false, attach: false, reveal: true },
            },
          },
        });
        if (body.resource_ref === 'rr1.managed-note-refreshed') return json({
          version: 1,
          target: { app: 'copal_notes' },
          resource: {
            id: 'resource-managed-note', ref: 'rr1.managed-note-exact', provider: 'copal', kind: 'document',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'Managed note',
          },
          payload: { name: 'Managed note', kind: 'note', text: '# exact', relations: [], read_only: false },
        });
        if (body.resource_ref === 'rr1.library-document') return json({
          version: 1,
          target: { app: 'document_editor' },
          resource: {
            id: 'resource-library-document', ref: 'rr1.library-document-refreshed', provider: 'library', kind: 'document',
            capabilities: ['stat', 'open', 'preview', 'download', 'write'], name: 'Exact Library document',
          },
          payload: {
            title: 'Exact Library document', language: 'markdown', content: '# exact library body',
            version: 3, session_ref: null, archived: false, read_only: false,
          },
        });
        if (body.resource_ref === 'rr1.library-chat-exact') return json({
          version: 1,
          target: { app: 'chat' },
          resource: {
            id: 'resource-library-chat-exact', ref: 'rr1.library-chat-refreshed', provider: 'library', kind: 'chat',
            capabilities: ['stat', 'open', 'download'], name: 'Opaque exact chat',
          },
          payload: {
            title: 'Opaque exact chat', model: 'test/model', archived: false, message_count: 77,
            messages: [{ role: 'user', text: 'Opaque chat body', timestamp: 1700000000000 }],
            truncated: true, read_only: true,
          },
        });
        if (body.resource_ref === 'rr1.library-research-exact') return json({
          version: 1,
          target: { app: 'research' },
          resource: {
            id: 'resource-library-research-exact', ref: 'rr1.library-research-refreshed', provider: 'library', kind: 'research',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'Opaque exact research',
          },
          payload: {
            title: 'Opaque exact research', category: 'systems', archived: false,
            report: 'Opaque research body', sources: [{ title: 'Safe source', url: 'https://example.invalid/source' }],
            source_count: 1, truncated: false, read_only: true,
          },
        });
        if (body.resource_ref === 'rr1.gallery-image-refreshed') return json({
          version: 1, target: { app: 'gallery' },
          resource: {
            id: 'resource-gallery-image', ref: 'rr1.gallery-image-exact', provider: 'gallery', kind: 'image',
            capabilities: ['stat', 'open', 'preview', 'download', 'favorite'], name: 'Opaque lake.png',
          },
          payload: {
            filename: 'Opaque lake.png', prompt: 'A quiet lake', caption: '', model: 'imported', size: '', quality: '',
            tags: 'calm', ai_tags: 'water', favorite: false, taken_at: null, created_at: 1700000000000,
            updated_at: 1700000004000, camera: '', width: 1, height: 1, file_size: 68,
            media_type: 'image/png', read_only: true,
          },
        });
      }
      if (url.pathname === '/api/files-v1/reissue') {
        const body = JSON.parse(init.body || '{}');
        window.__reissueRequests.push(body);
        const renewed = {
          'rr1.library-document-refreshed': {
            id: 'resource-library-document', ref: 'rr1.library-document-rehydrated', provider: 'library', kind: 'document',
            capabilities: ['stat', 'open', 'preview', 'download'], name: 'Exact Library document',
          },
          'rr1.library-chat-refreshed': {
            id: 'resource-library-chat-exact', ref: 'rr1.library-chat-rehydrated', provider: 'library', kind: 'chat',
            capabilities: ['stat', 'open', 'download'], name: 'Opaque exact chat',
          },
        }[body.resource_ref];
        if (renewed) return json({ version: 1, resource: renewed });
        return problem('resource_unavailable', 'Resource renewal is unavailable', 404);
      }
      if (url.pathname === '/api/files-v1/reveal') {
        const body = JSON.parse(init.body || '{}');
        window.__revealRequests.push(body);
        if (body.resource_ref === 'rr1.library-document-refreshed') {
          return problem('resource_ref_stale', 'Resource reference expired');
        }
        if (body.resource_ref === 'rr1.library-document-rehydrated') return json({
          version: 1, provider: 'library',
          ancestors: [{
            id: 'resource-library', ref: 'rr1.library-root', provider: 'library',
            kind: 'provider_root', capabilities: ['children', 'stat', 'search'], name: 'Library',
          }, {
            id: 'resource-library-documents', ref: 'rr1.library-documents-reveal', provider: 'library',
            kind: 'virtual_folder', capabilities: ['children', 'stat', 'search'], name: 'Documents',
          }],
          parent: {
            id: 'resource-library-documents', ref: 'rr1.library-documents-reveal', provider: 'library',
            kind: 'virtual_folder', capabilities: ['children', 'stat', 'search'], name: 'Documents',
          },
          resource: {
            id: 'resource-library-document', ref: 'rr1.library-document-reveal', provider: 'library',
            kind: 'document', capabilities: ['stat', 'open', 'preview', 'download'], name: 'Exact Library document',
          },
        });
        if (body.resource_ref === 'rr1.library-chat-refreshed') {
          return problem('resource_ref_stale', 'Resource reference expired');
        }
        if (body.resource_ref === 'rr1.library-chat-rehydrated') return json({
          version: 1, provider: 'library',
          ancestors: [{
            id: 'resource-library', ref: 'rr1.library-root', provider: 'library',
            kind: 'provider_root', capabilities: ['children', 'stat', 'search'], name: 'Library',
          }, {
            id: 'resource-library-chats', ref: 'rr1.library-chats-reveal', provider: 'library',
            kind: 'virtual_folder', capabilities: ['children', 'stat', 'search'], name: 'Chats',
          }],
          parent: {
            id: 'resource-library-chats', ref: 'rr1.library-chats-reveal', provider: 'library',
            kind: 'virtual_folder', capabilities: ['children', 'stat', 'search'], name: 'Chats',
          },
          resource: {
            id: 'resource-library-chat-exact', ref: 'rr1.library-chat-reveal', provider: 'library',
            kind: 'chat', capabilities: ['stat', 'open', 'download'], name: 'Opaque exact chat',
          },
        });
      }
      if (url.pathname === '/api/files-v1/content/rr1.managed-note') {
        const bytes = new TextEncoder().encode('MANAGED HEAD\\n' + 'm'.repeat(399980) + '\\nMANAGED TAIL');
        const stream = new ReadableStream({
          start(controller) { controller.enqueue(bytes); },
          cancel() { window.__managedPreviewCancelled = true; },
        });
        return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/markdown; charset=utf-8', 'Content-Length': String(bytes.length) } });
      }
      if (url.pathname === '/api/files-v1/content/rr1.host-readme') {
        return new Response('README head\\n\\n… [preview truncated] …\\n\\nREADME tail', { status: 200, headers: { 'Content-Type': 'text/markdown; charset=utf-8' } });
      }
      if (url.pathname === '/api/files-v1/content/rr1.host-photo') {
        const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='), character => character.charCodeAt(0));
        return new Response(png, { status: 200, headers: { 'Content-Type': 'image/png' } });
      }
      if (url.pathname === '/api/files-v1/thumbnail/rr1.host-image') {
        const png = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII='), character => character.charCodeAt(0));
        return new Response(png, { status: 200, headers: { 'Content-Type': 'image/png' } });
      }
      if (url.pathname === '/api/odysseus-files/preview-handles' && String(init.method || 'GET').toUpperCase() === 'POST') {
        const body = JSON.parse(init.body || '{}');
        const token = 'p'.repeat(43);
        const kind = body.kind === 'audio' ? 'audio' : 'image';
        return json({ token, url: '/api/odysseus-files/preview/' + token, kind, media_type: kind === 'audio' ? 'audio/mpeg' : 'image/png', size: kind === 'audio' ? 256 : 128 });
      }
      if (url.pathname.startsWith('/api/odysseus-files/preview-handles/') && String(init.method || '').toUpperCase() === 'DELETE') return json({ ok: true });
      if (url.pathname === '/api/odysseus-files/navigation-roots') return json({
        version: 1,
        generation: 7,
        default_path: window.__filesOwner === 'other' ? '/other' : '/work',
        roots: window.__filesOwner === 'other'
          ? [{ id: 'host:/other', name: 'other', path: '/other', kind: 'recursive_directory', capabilities: ['read'] }]
          : [{ id: 'host:/work', name: 'work', path: '/work', kind: 'recursive_directory', capabilities: ['read', 'write'] }],
        favorites: window.__filesOwner === 'other'
          ? [{ id: 'home', name: 'Home', path: '/other', kind: 'recursive_directory', pinned: true }]
          : [{ id: 'home', name: 'Home', path: '/work', kind: 'recursive_directory', pinned: true }],
      });
      if (url.pathname === '/api/odysseus-files/browse') {
        const requested = url.searchParams.get('path');
        if (requested === '/other') return json({ data: {
          path: '/other', next_cursor: null,
          entries: [{ name: 'other.txt', kind: 'file', size: 5, media_type: 'text/plain' }],
        }});
        if (requested === '/work/src/sub') return json({ data: {
          path: '/work/src/sub', next_cursor: null,
          entries: [
            { name: 'alpha.txt', kind: 'file', size: 2, modified_unix_ms: 20, media_type: 'text/plain' },
            { name: 'nested.txt', kind: 'file', size: 4, modified_unix_ms: 30, media_type: 'text/plain' },
            { name: 'zeta.txt', kind: 'file', size: 8, modified_unix_ms: 10, media_type: 'text/plain' },
          ],
        }});
        if (requested === '/work/src') return json({ data: {
          path: '/work/src', next_cursor: null,
          entries: [
            { name: 'sub', kind: 'directory', size: 0, modified_unix_ms: 10 },
            { name: 'lib.rs', kind: 'file', size: 81, modified_unix_ms: 20, media_type: 'rust' },
          ],
        }});
        return json({ data: {
          path: '/work', next_cursor: null,
          entries: [
            { name: 'src', kind: 'directory', size: 0 },
            { name: 'README.md', kind: 'file', size: 42, media_type: 'text/markdown' },
            {
              name: 'photo.png', kind: 'file', size: 128, media_type: 'image/png',
              resource_ref: 'rr1.host-photo',
              preview_url: '/api/files-v1/content/rr1.host-photo?purpose=preview',
              capabilities: ['stat', 'preview', 'download'], preview_kind: 'image',
            },
            { name: 'track.mp3', kind: 'file', size: 256, media_type: 'audio/mpeg' },
          ],
        }});
      }
      if (url.pathname === '/api/odysseus-files/stat') {
        if (!window.__hostPolicyVisible) return problem('denied', 'host location revoked', 403);
        return json({ data: { path: url.searchParams.get('path'), kind: 'directory' } });
      }
      if (url.pathname === '/api/odysseus-files/preview-text') return json({ data: {
        text: 'README head\\n\\n… [preview truncated; 2097152 bytes total] …\\n\\nREADME tail',
        encoding: 'Utf8', newline: '\\n', size: 2097152, truncated: true,
        head_bytes: 230400, tail_bytes: 89600, work: { bytes_read: 320000, hashes_computed: 0 },
      }});
      return new Response(JSON.stringify({ detail: 'unexpected acceptance request: ' + url.pathname }), { status: 404, headers: { 'Content-Type': 'application/json' } });
    };
    const files = await import('/static/js/files.js?tree-acceptance=' + Date.now());
    window.__filesModule = files.default;
    window.filesModule = files.default;
    await files.default.open();
  })()`);
  await waitFor("document.querySelectorAll('[data-files-tree] [role=treeitem]').length === 5 && document.querySelector('[data-files-body]')?.textContent.includes('Native image.png')", 'initial opaque Files Home');
  const marqueeTrace = await evaluate(`(async () => {
    const body = document.querySelector('[data-files-body]');
    const rows = [...body.querySelectorAll('.files-entry')].slice(0, 3);
    if (rows.length < 3) return { skipped: 'fewer than three rows' };
    const first = rows[0].getBoundingClientRect();
    const third = rows[2].getBoundingClientRect();
    const second = rows[1].getBoundingClientRect();
    const point = (rect) => ({ x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 });
    const start = { x: first.left - 8, y: first.top - 2 };
    body.dispatchEvent(new PointerEvent('pointerdown', { bubbles: true, pointerId: 71, isPrimary: true, button: 0, clientX: start.x, clientY: start.y }));
    const wide = point(third);
    body.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 71, isPrimary: true, clientX: wide.x, clientY: wide.y }));
    await new Promise(requestAnimationFrame);
    const expanded = [...body.querySelectorAll('.files-entry.selected .files-entry-name')].map(node => node.textContent.trim());
    const narrow = point(second);
    body.dispatchEvent(new PointerEvent('pointermove', { bubbles: true, pointerId: 71, isPrimary: true, clientX: narrow.x, clientY: narrow.y }));
    await new Promise(requestAnimationFrame);
    const shrunk = [...body.querySelectorAll('.files-entry.selected .files-entry-name')].map(node => node.textContent.trim());
    body.dispatchEvent(new PointerEvent('pointerup', { bubbles: true, pointerId: 71, isPrimary: true, button: 0, clientX: narrow.x, clientY: narrow.y }));
    return { expanded, shrunk };
  })()`);
  if (!marqueeTrace.skipped) {
    assert.equal(marqueeTrace.expanded.length >= 2, true, 'marquee selects rows in its current rectangle');
    assert.equal(marqueeTrace.shrunk.length < marqueeTrace.expanded.length, true, 'shrinking marquee removes departed rows');
  }

  // Files and Settings share one Open Clank Add Location wizard. The Files
  // shortcut remains standalone, and whole disk delegates access boundaries
  // to the host OS without silently making it the current/default folder.
  await evaluate("window.__pathBeforeLocationWizard = document.querySelector('[data-files-path]')?.textContent || ''; document.querySelector('.files-toolbar-button[title=\"Add host location\"]').click()");
  await waitFor("document.querySelector('.file-location-wizard-backdrop:not([hidden])')", 'shared Add Location wizard');
  assert.equal(await evaluate("!document.getElementById('settings-modal') || document.getElementById('settings-modal').classList.contains('hidden')"), true, 'Files must not open Settings behind the shared wizard');
  await evaluate("document.querySelector('[data-location-kind=\"whole_root\"]').click()");
  await waitFor("document.querySelector('.file-location-wizard-path')?.value === '/'", 'whole-disk host preset');
  await evaluate("document.querySelector('[data-location-submit]').click()");
  await waitFor("window.__addedLocations.length === 1 && document.querySelector('.file-location-wizard-backdrop')?.hidden === true", 'whole-disk Location creation');
  assert.deepEqual(await evaluate("window.__addedLocations[0]"), {
    path: '/', kind: 'whole_root', capabilities: ['read'], agent_access: true,
  });
  assert.equal(await evaluate("document.querySelector('[data-files-path]')?.textContent === window.__pathBeforeLocationWizard"), true, 'adding a Location must not change the active Files folder');

  await waitFor("document.querySelector('[data-files-workspace-id=\"workspace-host-src\"]')?.textContent.includes('Source')", 'opaque Workspace catalog');
  assert.equal(await evaluate("document.querySelector('[data-files-workspace-id=\"workspace-host-src\"]')?.title.includes('/work')"), false, 'Workspace catalog must not expose a host path');
  await evaluate("document.querySelector('[title=\"Rename Source\"]').click()");
  await waitFor("document.getElementById('styled-prompt-overlay') && !document.getElementById('styled-prompt-overlay').classList.contains('hidden')", 'Workspace rename prompt');
  await evaluate(`(() => {
    const input = document.getElementById('styled-prompt-input');
    input.value = 'Project Source';
    document.getElementById('styled-prompt-ok').click();
  })()`);
  await waitFor("document.querySelector('[data-files-workspace-id=\"workspace-host-src\"]')?.textContent.includes('Project Source')", 'renamed Workspace catalog');
  assert.deepEqual(await evaluate("window.__workspaceUpdates[0]"), { name: 'Project Source', expected_revision: 1 });
  await evaluate("document.querySelector('[data-files-workspace-id=\"workspace-host-src\"]').click()");
  await waitFor("document.querySelector('[data-files-path]')?.textContent.includes('src') && document.querySelector('[data-files-body]')?.textContent.includes('lib.rs')", 'Workspace opens through opaque Host ref');

  // The mounted Files authority is opaque. Home is the pinned opaque anchor;
  // opening it exercises the same real navigation path without reviving raw
  // host path browsing.
  const initialRawBrowseCount = await evaluate("window.__filesRequests.filter(request => request.startsWith('/api/odysseus-files/browse')).length");
  assert.equal(initialRawBrowseCount, 0, 'opaque startup must not browse a raw host path');
  assert.equal(await evaluate("!document.querySelector('[data-files-path]')?.textContent.includes('/work')"), true, 'the main Files view should open on the opaque Host/Home namespace');
  await evaluate("document.querySelector('[data-files-favorite-id=\"resource-host-home\"]').click()");
  await waitFor("document.querySelectorAll('.files-entry').length === 4 && document.querySelector('[data-files-path]')?.textContent === 'Home'", 'opaque Host Home favorite');
  assert.equal(await evaluate("window.__filesRequests.filter(request => request.startsWith('/api/odysseus-files/browse')).length"), initialRawBrowseCount);

  const before = await evaluate(`(() => ({
    treeIds: [...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId),
    expanded: [...document.querySelectorAll('[data-files-tree] [aria-expanded]')].map(node => [node.dataset.treeResourceId, node.getAttribute('aria-expanded')]),
    favorite: document.querySelector('[data-files-favorite-id]')?.textContent.trim(),
    current: document.querySelector('[data-files-tree] [aria-current="page"]')?.dataset.treeResourceId,
    treeSvgs: document.querySelectorAll('[data-files-tree] .files-tree-icon svg').length,
    neutralController: document.querySelector('[data-files-tree]').classList.contains('oc-explorer-tree'),
    rovingCount: document.querySelectorAll('[data-files-tree] [role=treeitem][tabindex="0"]').length,
    levels: [...document.querySelectorAll('[data-files-tree] [role=treeitem]')].map(node => node.getAttribute('aria-level')),
    contentVisuals: [...document.querySelectorAll('.files-entry .files-entry-visual')].map(node => ({ images: node.querySelectorAll('img').length, glyphs: node.querySelectorAll('[data-file-glyph]').length })),
    contentSvgs: document.querySelectorAll('.files-entry-visual svg').length,
    currentColor: [...document.querySelectorAll('.files-glyph-svg')].every(svg => svg.getAttribute('stroke') === 'currentColor'),
    placeholder: document.querySelector('[data-files-body]').textContent.includes('▱'),
  }))()`);
  assert.deepEqual(before.treeIds, ['resource-host-home', 'resource-host-src', 'resource-host-readme', 'resource-host-image', 'resource-host-track']);
  assert.deepEqual(before.expanded, [['resource-host-home', 'true'], ['resource-host-src', 'false']]);
  assert.equal(before.favorite, 'Home');
  assert.equal(before.current, 'resource-host-home');
  assert.equal(before.treeSvgs, 5);
  assert.equal(before.neutralController, true);
  assert.equal(before.rovingCount, 1);
  assert.deepEqual(before.levels, ['1', '2', '2', '2', '2']);
  assert.equal(before.contentVisuals.length, 4);
  assert.equal(before.contentVisuals.every(visual => visual.images + visual.glyphs === 1), true, 'every opaque Host entry has one visual');
  assert.ok(before.contentSvgs >= 1, 'the folder or a not-yet-loaded thumbnail retains a glyph');
  assert.equal(before.currentColor, true);
  assert.equal(before.placeholder, false);

  await evaluate(`(() => {
    const root = document.querySelector('[data-files-tree] [data-tree-resource-id="resource-host-home"]');
    root.focus();
    root.dispatchEvent(new KeyboardEvent('keydown', { key: 'r', bubbles: true }));
  })()`);
  assert.equal(await evaluate("document.activeElement?.dataset.treeResourceId"), 'resource-host-readme', 'Files worktree typeahead should use the neutral controller');

  await evaluate(`(() => {
    const photo = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Native image.png'));
    photo.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('.files-toolbar-button[title="Preview selected"]').click();
  })()`);
  await waitFor("document.querySelector('.files-preview-image')", 'image preview');
  const preview = await evaluate(`(() => ({
    image: !!document.querySelector('.files-preview-image'),
    stream: document.querySelector('.files-preview-image')?.src || '',
    controls: [...document.querySelectorAll('.files-preview-image-controls button')].map(button => button.textContent.trim()),
    minted: window.__filesRequestMethods.some(([method, path]) => method === 'POST' && path === '/api/odysseus-files/preview-handles'),
  }))()`);
  assert.equal(preview.image, true);
  assert.ok(preview.stream.includes('/api/files-v1/content/rr1.host-image?purpose=preview'));
  assert.equal(preview.stream.includes('/work/photo.png'), false);
  assert.equal(preview.stream.includes('/download?path='), false);
  assert.deepEqual(preview.controls, ['Fit', 'Actual size', '−', '+']);
  assert.equal(preview.minted, false);
  await evaluate(`(() => {
    const buttons = [...document.querySelectorAll('.files-preview-image-controls button')];
    buttons.find(button => button.textContent.trim() === 'Actual size')?.click();
    buttons.find(button => button.textContent.trim() === '+')?.click();
  })()`);
  assert.equal(await evaluate("document.querySelector('.files-preview-zoom-label')?.textContent"), '125%');
  await evaluate("document.querySelector('.files-preview-head .files-toolbar-button')?.click()");
  assert.equal(await evaluate("window.__filesRequestMethods.some(([method, path]) => method === 'DELETE' && path.startsWith('/api/odysseus-files/preview-handles/'))"), false, 'resource_ref previews do not mint revocable host handles');
  await evaluate("document.querySelector('.files-window')?.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true }))");
  assert.equal(await evaluate("document.querySelector('.files-window')?.classList.contains('hidden')"), false, 'Space preview must not minimize Files');
  await waitFor("document.querySelector('.files-preview-image')", 'keyboard image preview');
  await evaluate("document.querySelector('.files-window')?.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))");
  assert.equal(await evaluate("document.querySelector('.files-window')?.classList.contains('hidden')"), false, 'Escape preview close must not close Files');
  await waitFor("document.querySelector('[data-files-preview]')?.hidden === true", 'keyboard preview close');

  await evaluate(`(() => {
    const track = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('track.mp3'));
    track.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('.files-toolbar-button[title="Preview selected"]').click();
  })()`);
  await waitFor("document.querySelector('.files-preview-audio-engine')", 'audio preview');
  const audioPreview = await evaluate(`(() => {
    const audio = document.querySelector('.files-preview-audio-engine');
    const mute = [...document.querySelectorAll('.files-preview-transport-button')].find(button => button.textContent === 'Mute');
    mute?.click();
    const volume = document.querySelector('.files-preview-volume');
    volume.value = '0.4';
    volume.dispatchEvent(new Event('input', { bubbles: true }));
    return { autoplay: audio.autoplay, paused: audio.paused, muted: audio.muted, volume: audio.volume, time: document.querySelector('.files-preview-time')?.textContent, stream: audio.src };
  })()`);
  assert.equal(audioPreview.autoplay, false);
  assert.equal(audioPreview.paused, true);
  assert.equal(audioPreview.muted, false, 'moving volume above zero unmutes the preview');
  assert.equal(audioPreview.volume, 0.4);
  assert.equal(audioPreview.time, '0:00 / 0:00');
  assert.equal(audioPreview.stream.includes('/download?path='), false);
  await evaluate("document.querySelector('.files-preview-head .files-toolbar-button')?.click()");
  await waitFor("document.querySelector('[data-files-preview]')?.hidden === true", 'audio cleanup');

  await evaluate(`(() => {
    const readme = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('README.md'));
    readme.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('.files-toolbar-button[title="Preview selected"]').click();
  })()`);
  await waitFor("document.querySelector('.files-preview-text')?.textContent.includes('README tail')", 'bounded host text preview');
  const textPreview = await evaluate(`(() => ({
    text: document.querySelector('.files-preview-text')?.textContent || '',
    meta: document.querySelector('.files-preview-meta')?.textContent || '',
    request: window.__filesRequests.some(request => request.startsWith('/api/files-v1/content/rr1.host-readme?purpose=preview')),
  }))()`);
  assert.equal(textPreview.text.includes('README head'), true);
  assert.equal(textPreview.text.includes('README tail'), true);
  assert.equal(textPreview.meta.includes('head + tail preview'), false);
  assert.equal(textPreview.request, true);
  await evaluate("document.querySelector('.files-preview-head .files-toolbar-button')?.click()");
  await waitFor("document.querySelector('[data-files-preview]')?.hidden === true", 'text cleanup');

  await evaluate("document.querySelector('.files-entry.directory').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('[data-files-path]').textContent.includes('src') && document.querySelectorAll('.files-entry').length === 2", 'content navigation');
  const after = await evaluate(`(() => ({
    treeIds: [...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId),
    expanded: [...document.querySelectorAll('[data-files-tree] [aria-expanded]')].map(node => [node.dataset.treeResourceId, node.getAttribute('aria-expanded')]),
    current: document.querySelector('[data-files-tree] [aria-current="page"]')?.dataset.treeResourceId,
    navigationCalls: window.__filesRequests.filter(url => url.startsWith('/api/odysseus-files/navigation-roots')).length,
    childrenCalls: window.__filesRequests.filter(url => url.startsWith('/api/files-v1/children')).length,
    iconKey: [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('lib.rs'))?.querySelector('[data-file-glyph]')?.dataset.fileGlyph,
  }))()`);
  assert.deepEqual(after.treeIds, before.treeIds, 'content navigation must not rebuild the worktree');
  assert.deepEqual(after.expanded, before.expanded, 'content navigation must not change expansion state');
  assert.equal(after.current, 'resource-host-src');
  assert.equal(after.navigationCalls, 0);
  assert.ok(after.childrenCalls > 0);
  assert.equal(after.iconKey, 'rust');

  await evaluate("document.querySelector('[data-files-favorite-toggle]').click()");
  await waitFor("document.querySelector('[data-files-favorite-id=\"resource-host-src\"]')", 'new opaque favorite');
  const favorites = await evaluate("[...document.querySelectorAll('[data-files-favorite-id]')].map(node => node.dataset.filesFavoriteId)");
  assert.deepEqual(favorites, ['resource-host-home', 'resource-host-src']);

  const sortOptions = await evaluate("[...document.querySelector('.files-sort-select').options].map(option => [option.value, option.textContent])");
  assert.deepEqual(sortOptions, [
    ['name:asc', 'Name ↑'], ['name:desc', 'Name ↓'],
    ['kind:asc', 'Type ↑'], ['kind:desc', 'Type ↓'],
    ['modified:asc', 'Modified ↑'], ['modified:desc', 'Modified ↓'],
    ['size:asc', 'Size ↑'], ['size:desc', 'Size ↓'],
  ]);

  await evaluate("document.querySelector('[data-files-folders-first]').click()");
  await waitFor("window.__filesChildrenBodies.some(body => body.parent_ref === 'rr1.host-src' && body.sort?.directories_first === false)", 'folders-first cursor restart');
  assert.equal(await evaluate("document.querySelector('[data-files-folders-first]').getAttribute('aria-pressed')"), 'false');

  await evaluate(`(() => {
    const select = document.querySelector('.files-mode-select');
    select.value = 'details';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("document.querySelector('[role=grid].files-details-grid') && document.querySelectorAll('[role=columnheader]').length === 5", 'semantic Details grid');
  const detailsSort = await evaluate(`(() => ({ labels: [...document.querySelectorAll('.files-sort-header-button')].map(button => button.textContent.trim()), active: document.querySelector('[role=columnheader][aria-sort] .files-sort-header-button')?.dataset.sortKey, ariaSort: document.querySelector('[role=columnheader][aria-sort]')?.getAttribute('aria-sort'), buttonAriaSortCount: document.querySelectorAll('.files-sort-header-button[aria-sort]').length, rowCount: document.querySelectorAll('.files-details-grid > [role=row]').length, gridCells: document.querySelectorAll('.files-details-grid .files-entry [role=gridcell]').length }))()`);
  assert.deepEqual(detailsSort.labels, ['Name↑', 'Type↕', 'Size↕', 'Modified↕']);
  assert.equal(detailsSort.active, 'name');
  assert.equal(detailsSort.ariaSort, 'ascending');
  assert.equal(detailsSort.buttonAriaSortCount, 0, 'aria-sort belongs to columnheader, never a toolbar/button');
  assert.equal(detailsSort.rowCount, 3, 'Details exposes one header row plus two entry rows');
  assert.equal(detailsSort.gridCells, 10);

  await evaluate("document.querySelector('.files-sort-header-button[data-sort-key=modified]').click()");
  await waitFor("document.querySelector('[role=columnheader][aria-sort=ascending] .files-sort-header-button')?.dataset.sortKey === 'modified'", 'Details header sort state');
  const detailsRequest = await evaluate(`window.__filesChildrenBodies.some(body => body.parent_ref === 'rr1.host-src' && body.sort?.key === 'modified' && body.sort?.direction === 'asc' && body.sort?.directories_first === false)`);
  assert.equal(detailsRequest, true, 'Details header should restart the opaque cursor with its complete wire SortSpec');

  // Restore folders-first before entering Columns so the directory remains first.
  await evaluate("document.querySelector('[data-files-folders-first]').click()");
  await waitFor("document.querySelector('[data-files-folders-first]').getAttribute('aria-pressed') === 'true'", 'folders first restore');
  await evaluate(`(() => { const select = document.querySelector('.files-mode-select'); select.value = 'columns'; select.dispatchEvent(new Event('change', { bubbles: true })); })()`);
  await waitFor("document.querySelectorAll('.files-column').length === 2 && document.querySelector('.files-column-sort-select')", 'Columns sort controls');
  assert.equal(await evaluate("document.querySelectorAll('.files-sort-header-button').length"), 0, 'Columns uses per-column controls, not a faux table toolbar');
  assert.equal(await evaluate("document.querySelector('.files-column-sort-select').options.length"), 8);
  await evaluate("document.querySelector('.files-column[data-column-index=\"1\"] .files-column-entry.directory').click()");
  await waitFor("document.querySelectorAll('.files-column').length === 3", 'hierarchical columns');
  assert.equal(await evaluate("document.querySelector('.files-column:last-child .files-column-head').textContent.includes('sub')"), true);

  await evaluate(`(() => { const select = document.querySelector('.files-column[data-column-index="1"] .files-column-sort-select'); select.value = 'kind:desc'; select.dispatchEvent(new Event('change', { bubbles: true })); })()`);
  await waitFor("document.querySelector('.files-column[data-column-index=\"1\"] .files-column-sort-select')?.value === 'kind:desc' && document.querySelectorAll('.files-column').length === 2", 'src column sort');
  // A parent reload invalidates descendant columns; reopen the authorized
  // child before applying its independent sort.
  await evaluate("document.querySelector('.files-column[data-column-index=\"1\"] .files-column-entry.directory').click()");
  await waitFor("document.querySelectorAll('.files-column').length === 3", 'reopened nested column');
  await evaluate(`(() => { const select = document.querySelector('.files-column[data-column-index="2"] .files-column-sort-select'); select.value = 'size:desc'; select.dispatchEvent(new Event('change', { bubbles: true })); })()`);
  await waitFor("document.querySelector('.files-column[data-column-index=\"2\"] .files-column-sort-select')?.value === 'size:desc'", 'nested column sort');
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.files-column-sort-select')].map(select => select.value)"), ['name:asc', 'kind:desc', 'size:desc']);
  const independentRequests = await evaluate(`(() => ({
    parent: window.__filesChildrenBodies.some(item => item.parent_ref === 'rr1.host-src' && item.sort?.key === 'kind' && item.sort?.direction === 'desc'),
    child: window.__filesChildrenBodies.some(item => item.parent_ref === 'rr1.host-sub' && item.sort?.key === 'size' && item.sort?.direction === 'desc'),
  }))()`);
  assert.deepEqual(independentRequests, { parent: true, child: true });

  await evaluate("document.querySelector('.files-column[data-column-index=\"1\"] [data-column-folders-first]').click()");
  await waitFor("document.querySelector('.files-column[data-column-index=\"1\"] [data-column-folders-first]').getAttribute('aria-pressed') === 'false'", 'per-column folders first');
  const columnFoldersRequest = await evaluate("window.__filesChildrenBodies.some(body => body.parent_ref === 'rr1.host-src' && body.sort?.key === 'kind' && body.sort?.directories_first === false)");
  assert.equal(columnFoldersRequest, true);

  // Roving focus stays inside Columns and never mutates the persistent left
  // worktree. Right opens a directory, Left returns to its selected parent.
  await evaluate(`(() => {
    const directory = document.querySelector('.files-column[data-column-index="0"] .files-column-entry.directory');
    directory.focus();
    directory.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowRight', bubbles: true }));
  })()`);
  await waitFor("document.activeElement?.closest('.files-column')?.dataset.columnIndex === '1'", 'Columns ArrowRight');
  assert.equal(await evaluate("document.activeElement.dataset.columnEntryIndex"), '0');
  await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }))");
  await evaluate("new Promise(requestAnimationFrame)");
  assert.equal(await evaluate("document.activeElement.dataset.columnEntryIndex"), '1');
  await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'End', bubbles: true }))");
  await evaluate("new Promise(requestAnimationFrame)");
  assert.equal(await evaluate("document.activeElement.dataset.columnEntryIndex"), '1');
  await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'Home', bubbles: true }))");
  await evaluate("new Promise(requestAnimationFrame)");
  assert.equal(await evaluate("document.activeElement.dataset.columnEntryIndex"), '0');
  await evaluate("document.activeElement.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true }))");
  await evaluate("new Promise(requestAnimationFrame)");
  assert.equal(await evaluate("document.activeElement?.closest('.files-column')?.dataset.columnIndex"), '0');
  assert.equal(await evaluate("document.activeElement.classList.contains('directory')"), true);

  const finalTree = await evaluate(`(() => ({
    ids: [...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId),
    expanded: [...document.querySelectorAll('[data-files-tree] [aria-expanded]')].map(node => [node.dataset.treeResourceId, node.getAttribute('aria-expanded')]),
  }))()`);
  assert.deepEqual(finalTree.ids, before.treeIds);
  assert.deepEqual(finalTree.expanded, before.expanded);

  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'all';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Host locations'))", 'opaque Host provider root');
  await evaluate(`(() => {
    const input = document.querySelector('[data-files-search]');
    input.value = 'Quarterly';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  })()`);
  await waitFor("document.querySelector('[data-files-body]')?.textContent.includes('Quarterly note')", 'provider-wide search result');
  assert.equal(await evaluate(`window.__filesRequestMethods.some(([method, pathname]) => method === 'POST' && pathname === '/api/files-v1/search')`), true);
  await evaluate(`(() => {
    const input = document.querySelector('[data-files-search]');
    input.value = '';
    input.dispatchEvent(new Event('search', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Host locations'))", 'provider-wide search clear');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Host locations')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Home')", 'opaque Host anchor');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Home')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Native image.png'))", 'opaque Host file');
  await waitFor("window.__filesRequests.some(request => request.startsWith('/api/files-v1/thumbnail/rr1.host-image?'))", 'viewport Quick Look request');
  assert.equal(await evaluate(`window.__filesRequests.some(request => request.includes('/work') && request.includes('/api/files-v1/thumbnail/'))`), false);

  // Choosing Host locations uses the same opaque facade as All sources. The
  // default Home anchor opens automatically without exposing its host path or
  // falling back to the raw browse endpoint.
  await evaluate(`(() => {
    window.__hostFacadeRequestStart = window.__filesRequests.length;
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'host';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("document.querySelector('[data-files-body]')?.textContent.includes('Native image.png')", 'opaque Host default anchor');
  assert.equal(await evaluate(`window.__filesRequests.slice(window.__hostFacadeRequestStart).some(request => request.startsWith('/api/odysseus-files/browse'))`), false);
  assert.equal(await evaluate("document.querySelector('[data-files-path]').textContent.includes('Host locations') && !document.querySelector('[data-files-path]').textContent.includes('/work')"), true);
  await waitFor("window.__filesWatchActive?.resourceRef === 'rr1.host-home'", 'active opaque Host watch');
  const watchTreeBefore = await evaluate("[...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId)");
  await evaluate(`(() => {
    window.__hostChanged = true;
    window.__filesWatchActive.controller.enqueue(new TextEncoder().encode('event: files-change\\ndata: {"sequence":0,"kind":"modified","rescan_required":false,"observed_unix_ms":10}\\n\\n'));
  })()`);
  await waitFor("document.querySelector('[data-files-body]')?.textContent.includes('watched-change.txt')", 'watch-driven authoritative refresh');
  assert.deepEqual(
    await evaluate("[...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId)"),
    watchTreeBefore,
  );
  await evaluate(`(() => {
    const input = document.querySelector('[data-files-search]');
    input.value = 'Native';
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
  })()`);
  await waitFor("window.__filesChildrenBodies.some(body => body.parent_ref === 'rr1.host-home' && body.query === 'Native')", 'opaque Host scoped search');
  assert.equal(await evaluate("document.querySelector('[data-files-path]').textContent.includes('Search: Native')"), true);
  await evaluate(`(() => {
    const input = document.querySelector('[data-files-search]');
    input.value = '';
    input.dispatchEvent(new Event('search', { bubbles: true }));
  })()`);
  await waitFor("document.querySelector('[data-files-path]').textContent.includes('Host locations') && !document.querySelector('[data-files-path]').textContent.includes('Search:')", 'opaque Host search clear');

  // A real Files double-click follows the facade target through
  // openManagedEntry and the Editor handoff, rather than the context-menu
  // compatibility call below.
  await evaluate(`(() => {
    const mode = document.querySelector('.files-mode-select');
    mode.value = 'list';
    mode.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('README.md'))", 'opaque Host Markdown row');
  const markdownActions = await evaluate(`(() => {
    const row = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('README.md'));
    row.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('[data-files-actions]').click();
    return [...document.querySelectorAll('.files-action-menu-item')].map(node => node.textContent.trim());
  })()`);
  assert.equal(markdownActions.includes('Open in Editor'), true, 'text files expose the Editor action');
  assert.equal(markdownActions.includes('Use as workspace'), false, 'files never expose the workspace action');
  await evaluate(`(() => {
    const row = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('README.md'));
    row.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, clientX: 30, clientY: 30 }));
  })()`);
  await waitFor("window.__filesOpenedTarget === 'editor' && window.__filesOpenedResource === 'rr1.host-readme-refreshed'", 'Host double-click enters the unified Editor');
  assert.equal(await evaluate("window.__filesOpenedPayload.representation"), 'markdown');
  assert.deepEqual(await evaluate("window.__filesOpenedPayload.resource.metadata"), {
    encoding: 'utf-8', newline: '\n', language: 'markdown', mode: 'markdown', bomBytes: 0,
  });
  assert.equal(await evaluate("window.__filesOpenedPayload.resource.locator.opaqueRef"), 'rr1.host-readme-refreshed');

  // New favorites persist as server-owned opaque Places. No raw host path or
  // expiring ResourceRef is written into browser storage.
  await evaluate(`(() => {
    const mode = document.querySelector('.files-mode-select');
    mode.value = 'list';
    mode.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('src'))", 'opaque Host folder in list view');
  await evaluate(`(() => {
    const row = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('README.md'));
    row.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('[data-files-actions]').click();
  })()`);
  await waitFor("[...document.querySelectorAll('.files-action-menu-item')].some(node => node.textContent.trim() === 'Open in Editor')", 'Host Editor action');
  await evaluate("[...document.querySelectorAll('.files-action-menu-item')].find(node => node.textContent.trim() === 'Open in Editor').click()");
  await waitFor("window.__codeOpenedResource === 'rr1.host-readme-refreshed'", 'opaque Host ref handed to Editor compatibility path');
  await evaluate(`(() => {
    const row = [...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('src'));
    row.dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('[data-files-actions]').click();
  })()`);
  await waitFor("[...document.querySelectorAll('.files-action-menu-item')].some(node => node.textContent.includes('Use as workspace'))", 'Host Workspace action');
  await evaluate("[...document.querySelectorAll('.files-action-menu-item')].find(node => node.textContent.includes('Use as workspace')).click()");
  await waitFor("window.__workspaceResourceRequests.some(body => body.resource_ref === 'rr1.host-src' && body.purpose === 'agent_workspace')", 'opaque Workspace handoff');
  await waitFor("localStorage.getItem('odysseus-workspace-id') === 'workspace-host-src'", 'stable Workspace ID persisted');
  await waitFor("document.querySelector('[data-files-path]').textContent.includes('Home')", 'Host refresh after workspace handoff');
  await waitFor("window.__filesPlaces.some(resource => resource.id === 'resource-host-src') && document.querySelector('[data-files-favorite-id=\"resource-host-src\"]')", 'opaque src Place retained after workspace refresh');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('src')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('[data-files-path]').textContent.includes('src') && document.querySelector('[data-files-favorite-toggle]').disabled === false", 'opaque Host subfolder');
  await evaluate("document.querySelector('[data-files-favorite-toggle]').dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))");
  await waitFor("window.__filesRequestMethods.some(([method, pathname]) => method === 'DELETE' && pathname === '/api/files-v1/places/place-' + 'a'.repeat(32))", 'opaque Place removal');
  await waitFor("!document.querySelector('[data-files-favorite-id=\"resource-host-src\"]')", 'opaque server Place removed');
  assert.equal(await evaluate(`window.__filesRequestMethods.some(([method, pathname]) => method === 'DELETE' && pathname === '/api/files-v1/places/place-' + 'a'.repeat(32))`), true);
  assert.equal(await evaluate("localStorage.getItem('odysseus-files-favorites:owner')?.includes('rr1.')"), false);

  await evaluate("document.querySelector('[title=\"Archive Project Source\"]').click()");
  await waitFor("document.getElementById('styled-confirm-overlay') && !document.getElementById('styled-confirm-overlay').classList.contains('hidden')", 'Workspace archive confirmation');
  await evaluate("document.getElementById('styled-confirm-ok').click()");
  await waitFor("window.__workspaceUpdates.length === 2", 'Workspace archive request');
  await waitFor("!document.querySelector('[data-files-workspace-id=\"workspace-host-src\"]') && window.__workspaceCatalog[0].workspace.archived === true", 'archived Workspace removed from active catalog');
  assert.deepEqual(await evaluate("window.__workspaceUpdates[1]"), { archived: true, expected_revision: 2 });
  assert.equal(await evaluate("localStorage.getItem('odysseus-workspace-id')"), null, 'archiving the current Workspace clears the browser pointer');

  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'gallery';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Photos'))", 'Gallery opaque folder');
  assert.deepEqual(await evaluate("[...document.querySelector('.files-sort-select').options].filter(option => !option.disabled).map(option => option.value)"), ['name:asc', 'name:desc']);
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Albums')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Summer')", 'Gallery album folder');
  assert.deepEqual(await evaluate("[...document.querySelector('.files-sort-select').options].filter(option => !option.disabled).map(option => option.value)"), ['name:asc', 'name:desc', 'modified:asc', 'modified:desc']);
  assert.deepEqual(await evaluate("[...document.querySelector('.files-sort-select').options].filter(option => option.disabled).map(option => option.value)"), ['kind:asc', 'kind:desc', 'size:asc', 'size:desc']);
  assert.equal(await evaluate("window.__filesChildrenBodies.some(body => body.parent_ref === 'rr1.gallery-albums' && ['name', 'modified'].includes(body.sort?.key))"), true);
  await evaluate("document.querySelector('.files-toolbar-button[title=\"Parent folder\"]').click()");
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Photos'))", 'Gallery root after sort negotiation');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Photos')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Opaque lake.png')", 'Gallery opaque image');
  await evaluate("document.querySelector('.files-entry').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("window.__galleryOpenedResource === 'rr1.gallery-image-exact'", 'Gallery opaque exact-open');
  assert.equal(await evaluate("window.__galleryOpenedPayload.read_only"), true);
  assert.equal(await evaluate("window.__galleryOpenedPayload.prompt"), 'A quiet lake');
  assert.equal(await evaluate("window.__filesRequests.some(request => request.includes('resource-gallery-image'))"), false);

  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'copal';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Documents')", 'managed provider folder');
  await evaluate("document.querySelector('.files-entry').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Managed note')", 'managed provider text entry');
  await evaluate(`(() => {
    document.querySelector('.files-entry').dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }));
    document.querySelector('.files-toolbar-button[title="Preview selected"]').click();
  })()`);
  await waitFor("document.querySelector('.files-preview-text')?.textContent.includes('MANAGED HEAD')", 'bounded managed text preview');
  const managedPreview = await evaluate(`(() => ({
    length: document.querySelector('.files-preview-text')?.textContent.length || 0,
    meta: document.querySelector('.files-preview-meta')?.textContent || '',
    fallback: [...document.querySelectorAll('.files-preview-transport-button')].some(button => button.textContent === 'Download full file'),
    cancelled: window.__managedPreviewCancelled === true,
  }))()`);
  assert.equal(managedPreview.length < 321000, true);
  assert.equal(managedPreview.meta.includes('start-only preview'), true);
  assert.equal(managedPreview.meta.includes('tail unavailable'), true);
  assert.equal(managedPreview.fallback, true);
  assert.equal(managedPreview.cancelled, true);
  await evaluate("document.querySelector('.files-toolbar-button[title=\"Open selected in source app\"]').click()");
  await waitFor("window.__filesOpenedTarget === 'copal_notes'", 'opaque managed open action');
  assert.equal(await evaluate("window.__filesOpenedResource"), 'rr1.managed-note-refreshed');
  assert.equal(await evaluate(`window.__filesRequestMethods.some(([method, pathname]) => method === 'POST' && pathname === '/api/files-v1/action')`), true);
  assert.equal(await evaluate(`window.__filesRequestMethods.some(([method, pathname]) => method === 'POST' && pathname === '/api/files-v1/open-resource')`), true);
  assert.equal(await evaluate(`window.__filesRequests.some(request => request.includes('resource-managed-note'))`), false, 'open action must not expose provider IDs in URLs');
  await evaluate(`(() => {
    window.__filesOpenedTarget = '';
    document.querySelector('.files-entry').dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
  })()`);
  await waitFor("window.__filesOpenedTarget === 'copal_notes'", 'managed double-click source action');
  await evaluate("document.querySelector('.files-content-more').click()");
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Second note'))", 'managed provider second page');
  assert.equal(await evaluate(`window.__filesRequestMethods.some(([method, pathname]) => method === 'POST' && pathname === '/api/files-v1/children')`), true);

  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'library';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Documents'))", 'Library folder for managed action');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Documents')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Archive from Files')", 'Library actionable document');
  await evaluate("document.querySelector('.files-entry').dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 }))");
  await waitFor("document.querySelector('[data-files-actions]')?.disabled === false", 'managed Actions button');
  await evaluate("document.querySelector('[data-files-actions]').click()");
  await waitFor("[...document.querySelectorAll('.files-action-menu-item')].some(node => node.textContent.includes('Archive'))", 'Open Clank managed action menu');
  await evaluate("[...document.querySelectorAll('.files-action-menu-item')].find(node => node.textContent.includes('Archive')).click()");
  await waitFor("window.__fileArchived === true && document.querySelector('.files-empty')?.textContent.includes('No items')", 'opaque archive action reconciliation');
  assert.equal(await evaluate(`window.__filesRequests.some(request => request.includes('archive-action-document'))`), false);

  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'library';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("[...document.querySelectorAll('.files-entry')].some(node => node.textContent.includes('Chats'))", 'Library chats folder');
  await evaluate("[...document.querySelectorAll('.files-entry')].find(node => node.textContent.includes('Chats')).dispatchEvent(new MouseEvent('dblclick', { bubbles: true }))");
  await waitFor("document.querySelector('.files-entry')?.textContent.includes('Archive chat from Files')", 'Library actionable chat');
  assert.deepEqual(await evaluate("[...document.querySelector('.files-sort-select').options].filter(option => !option.disabled).map(option => option.value)"), ['name:asc', 'name:desc', 'modified:asc', 'modified:desc']);
  await evaluate("document.querySelector('.files-entry').dispatchEvent(new MouseEvent('click', { bubbles: true, detail: 1 })); document.querySelector('[data-files-actions]').click()");
  await waitFor("[...document.querySelectorAll('.files-action-menu-item')].some(node => node.textContent.includes('Archive'))", 'chat archive action');
  await evaluate("[...document.querySelectorAll('.files-action-menu-item')].find(node => node.textContent.includes('Archive')).click()");
  await waitFor("window.__chatArchived === true && document.querySelector('.files-empty')?.textContent.includes('No items')", 'shared chat lifecycle reconciliation');
  assert.equal(await evaluate(`window.__filesRequests.some(request => request.includes('chat-action-origin'))`), false);

  // The real document editor adapter consumes only the opaque ref, mounts the
  // existing editor, and keeps this first exact-open seam read-only so none of
  // its legacy id routes can accidentally receive a provider origin id.
  await evaluate(`(async () => {
    if (!document.getElementById('chat-container')) {
      const chat = document.createElement('div');
      chat.id = 'chat-container';
      document.body.appendChild(chat);
    }
    const module = await import('/static/js/document.js?files-exact-open=' + Date.now());
    module.default.init(location.origin);
    window.__filesExactDocumentModule = module.default;
    await module.default.openResource('rr1.library-document');
  })()`);
  await waitFor("document.getElementById('doc-editor-textarea')?.value === '# exact library body' && document.getElementById('doc-editor-textarea')?.readOnly === true", 'Library opaque exact-open in document editor');
  const exactDocument = await evaluate(`(() => ({
    id: window.__filesExactDocumentModule.getCurrentDocId(),
    saveDisabled: document.getElementById('doc-footer-copy-btn')?.disabled === true,
    languageDisabled: document.getElementById('doc-language-select')?.disabled === true,
    legacyRequests: window.__filesRequests.filter(path => path.startsWith('/api/document/')),
  }))()`);
  assert.equal(exactDocument.id, 'rr1.library-document-refreshed');
  assert.equal(exactDocument.saveDisabled, true);
  assert.equal(exactDocument.languageDisabled, true);
  assert.deepEqual(exactDocument.legacyRequests, []);
  await evaluate("window.__filesExactDocumentModule.saveDocument({ silent:true })");
  assert.deepEqual(await evaluate("window.__filesRequests.filter(path => path.startsWith('/api/document/'))"), []);
  assert.equal(await evaluate("!!document.getElementById('doc-show-in-files-btn')"), true);
  assert.equal(await evaluate("typeof document.getElementById('doc-show-in-files-btn').onclick"), 'function');
  await waitFor("document.getElementById('doc-show-in-files-btn')?.disabled === false", 'managed app Show in Files enabled');
  assert.equal(await evaluate("getComputedStyle(document.getElementById('doc-show-in-files-btn')).display !== 'none' && getComputedStyle(document.getElementById('doc-actions-footer')).display !== 'none'"), true);
  await evaluate("document.getElementById('doc-show-in-files-btn').scrollIntoView({ block:'center', inline:'nearest' })");
  await evaluate("new Promise(requestAnimationFrame)");
  const showInFilesPoint = await evaluate(`(() => {
    const button = document.getElementById('doc-show-in-files-btn');
    const rect = button.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2, width: rect.width, height: rect.height };
  })()`);
  assert.equal(await evaluate(`document.elementFromPoint(${showInFilesPoint.x}, ${showInFilesPoint.y})?.id`), 'doc-show-in-files-btn');
  await command('Input.dispatchMouseEvent', { type: 'mousePressed', x: showInFilesPoint.x, y: showInFilesPoint.y, button: 'left', clickCount: 1 });
  await command('Input.dispatchMouseEvent', { type: 'mouseReleased', x: showInFilesPoint.x, y: showInFilesPoint.y, button: 'left', clickCount: 1 });
  await waitFor("window.__revealRequests.length === 1 && window.__reissueRequests.length === 1", 'managed ref reissue and opaque reveal');
  await waitFor("document.querySelector('.files-entry.selected .files-entry-name')?.textContent === 'Exact Library document'", 'managed app Show in Files reveal');
  assert.deepEqual(await evaluate("window.__revealRequests"), [
    { resource_ref: 'rr1.library-document-rehydrated' },
  ]);
  assert.deepEqual(await evaluate("window.__reissueRequests"), [{ resource_ref: 'rr1.library-document-refreshed' }]);
  await evaluate(`(() => {
    const mode = document.querySelector('.files-mode-select');
    mode.value = 'columns';
    mode.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("document.querySelectorAll('.files-column').length === 2", 'reveal ancestor columns');
  assert.deepEqual(await evaluate("[...document.querySelectorAll('.files-column-head > span:first-child')].map(node => node.textContent)"), ['Library', 'Documents']);
  const revealUpPoint = await evaluate(`(() => {
    const button = document.querySelector('[title="Parent folder"]');
    const rect = button.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  assert.equal(await evaluate(`document.elementFromPoint(${revealUpPoint.x}, ${revealUpPoint.y})?.closest('button')?.title`), 'Parent folder');
  await command('Input.dispatchMouseEvent', { type: 'mousePressed', x: revealUpPoint.x, y: revealUpPoint.y, button: 'left', clickCount: 1 });
  await command('Input.dispatchMouseEvent', { type: 'mouseReleased', x: revealUpPoint.x, y: revealUpPoint.y, button: 'left', clickCount: 1 });
  await waitFor("document.querySelectorAll('.files-column').length === 1", 'reveal Parent folder column');
  assert.equal(await evaluate("document.querySelector('[data-files-path]')?.textContent"), 'Library');
  assert.equal(await evaluate("[...document.querySelectorAll('.files-column-entry')].some(node => node.textContent.includes('Documents'))"), true);
  await evaluate(`(() => {
    const mode = document.querySelector('.files-mode-select');
    mode.value = 'list';
    mode.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await evaluate("window.__filesExactDocumentModule.clearAll()");

  // Chats and Research use the existing Library surface as a bounded,
  // read-only exact viewer. Only the opaque ResourceRef crosses the browser;
  // neither provider id is put into a route, history, or rendered text.
  await evaluate("window.__filesExactDocumentModule.openLibraryResource('rr1.library-chat-exact')");
  await waitFor("document.querySelector('.doclib-exact-resource')?.textContent.includes('Opaque chat body')", 'Library opaque exact chat');
  const exactChat = await evaluate(`(() => ({
    title: document.querySelector('.doclib-exact-resource h2')?.textContent,
    meta: document.querySelector('.doclib-exact-meta')?.textContent,
    download: [...document.querySelectorAll('.doclib-exact-actions button')].some(node => node.textContent === 'Download'),
    body: document.querySelector('.doclib-exact-resource')?.textContent,
  }))()`);
  assert.equal(exactChat.title, 'Opaque exact chat');
  assert.equal(exactChat.meta.includes('77 messages'), true);
  assert.equal(exactChat.download, true);
  assert.equal(exactChat.body.includes('resource-library-chat-exact'), false);
  assert.equal(await evaluate("!!document.querySelector('.doclib-show-in-files')"), true);
  await evaluate("document.querySelector('.doclib-show-in-files').scrollIntoView({ block:'center', inline:'nearest' })");
  const libraryShowPoint = await evaluate(`(() => {
    const rect = document.querySelector('.doclib-show-in-files').getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  assert.equal(await evaluate(`document.elementFromPoint(${libraryShowPoint.x}, ${libraryShowPoint.y})?.classList.contains('doclib-show-in-files')`), true);
  await command('Input.dispatchMouseEvent', { type: 'mousePressed', x: libraryShowPoint.x, y: libraryShowPoint.y, button: 'left', clickCount: 1 });
  await command('Input.dispatchMouseEvent', { type: 'mouseReleased', x: libraryShowPoint.x, y: libraryShowPoint.y, button: 'left', clickCount: 1 });
  await waitFor("window.__revealRequests.length === 2 && window.__reissueRequests.length === 2", 'Library managed ref reissue and opaque reveal');
  await waitFor("document.querySelector('.files-entry.selected .files-entry-name')?.textContent === 'Opaque exact chat'", 'Library Show in Files reveal');
  assert.deepEqual((await evaluate("window.__revealRequests")).slice(1), [
    { resource_ref: 'rr1.library-chat-rehydrated' },
  ]);
  assert.deepEqual((await evaluate("window.__reissueRequests"))[1], { resource_ref: 'rr1.library-chat-refreshed' });
  await evaluate("window.filesModule.close()");
  await waitFor("getComputedStyle(document.querySelector('.files-window')).display === 'none'", 'Files hidden for exact download');
  const exactDownloadPoint = await evaluate(`(() => {
    const button = [...document.querySelectorAll('.doclib-exact-actions button')].find(node => node.textContent === 'Download');
    const rect = button.getBoundingClientRect();
    return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
  })()`);
  assert.equal(await evaluate(`document.elementFromPoint(${exactDownloadPoint.x}, ${exactDownloadPoint.y})?.textContent`), 'Download');
  await command('Input.dispatchMouseEvent', { type: 'mousePressed', x: exactDownloadPoint.x, y: exactDownloadPoint.y, button: 'left', clickCount: 1 });
  await command('Input.dispatchMouseEvent', { type: 'mouseReleased', x: exactDownloadPoint.x, y: exactDownloadPoint.y, button: 'left', clickCount: 1 });
  await waitFor("window.__exactDownloads.length === 1 && window.__reissueRequests.length === 3", 'exact-view just-in-time download reissue');
  assert.equal(await evaluate("window.__exactDownloads[0].href.includes('rr1.library-chat-rehydrated')"), true);

  await evaluate("window.__filesExactDocumentModule.openLibraryResource('rr1.library-research-exact')");
  await waitFor("document.querySelector('.doclib-exact-report')?.textContent.includes('Opaque research body')", 'Library opaque exact research');
  const exactResearch = await evaluate(`(() => ({
    title: document.querySelector('.doclib-exact-resource h2')?.textContent,
    source: document.querySelector('.doclib-exact-sources a')?.textContent,
    href: document.querySelector('.doclib-exact-sources a')?.href,
    body: document.querySelector('.doclib-exact-resource')?.textContent,
  }))()`);
  assert.equal(exactResearch.title, 'Opaque exact research');
  assert.equal(exactResearch.source, 'Safe source');
  assert.equal(exactResearch.href, 'https://example.invalid/source');
  assert.equal(exactResearch.body.includes('resource-library-research-exact'), false);
  await evaluate("window.__filesExactDocumentModule.closeLibrary()");
  await evaluate("(async () => { await window.filesModule.open(); return true; })()");
  await waitFor("getComputedStyle(document.querySelector('.files-window')).display !== 'none'", 'Files reopened after exact download');

  // Policy refresh is transactional for an open Files window. A facade 5xx
  // must retain the current authorized view; a successful current-policy
  // projection that no longer contains the selected folder must remove all of
  // its old entries and fall back to the now-empty Host landing page.
  await evaluate(`(() => {
    const provider = document.querySelector('.files-provider-select');
    provider.value = 'host';
    provider.dispatchEvent(new Event('change', { bubbles: true }));
  })()`);
  await waitFor("document.querySelector('[data-files-body]')?.textContent.includes('Native image.png')", 'Host projection before policy event');
  await evaluate(`(() => {
    window.__filesRootsStatus = 503;
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  })()`);
  await waitFor("document.querySelector('.files-window .copal-workspace-status')?.textContent.includes('retained')", 'Files policy outage status');
  assert.equal(await evaluate("document.querySelector('[data-files-body]')?.textContent.includes('Native image.png')"), true);

  await evaluate(`(() => {
    window.__filesRootsStatus = 200;
    window.__hostPolicyVisible = false;
    window.__filesPlaces = [];
    document.dispatchEvent(new CustomEvent('openclank:file-policy-changed'));
  })()`);
  await waitFor("document.querySelector('[data-files-path]')?.textContent.includes('Host locations') && !document.querySelector('[data-files-body]')?.textContent.includes('Native image.png')", 'Files revoked projection removed');
  const policyRevocation = await evaluate(`(() => ({
    content: document.querySelector('[data-files-body]')?.textContent || '',
    treeText: document.querySelector('[data-files-tree]')?.textContent || '',
    favoriteText: document.querySelector('[data-files-favorites]')?.textContent || '',
  }))()`);
  assert.equal(policyRevocation.content.includes('README.md'), false);
  assert.equal(policyRevocation.treeText.includes('Home'), false);
  assert.equal(policyRevocation.favoriteText.includes('work'), false);

  // Restore current-policy Host visibility before exercising the independent
  // authenticated-owner transition below.
  await evaluate("window.__hostPolicyVisible = true");

  await evaluate(`(() => {
    window.__filesOwner = 'other';
    document.dispatchEvent(new CustomEvent('openclank:auth-user-ready', { detail: { username: 'other' } }));
  })()`);
  await waitFor("document.querySelector('[data-files-path]')?.textContent.includes('Home') && document.querySelector('.files-entry')?.textContent.includes('other.txt')", 'Files account transition');
  const accountSwitch = await evaluate(`(() => ({
    treeIds: [...document.querySelectorAll('[data-files-tree] [data-tree-resource-id]')].map(node => node.dataset.treeResourceId),
    visibleText: document.querySelector('.files-window')?.textContent || '',
    otherPrefs: !!localStorage.getItem('odysseus-files-view-preferences:other'),
  }))()`);
  assert.deepEqual(accountSwitch.treeIds, ['resource-host-other-home', 'resource-host-other-file']);
  assert.equal(accountSwitch.visibleText.includes('/work'), false);
  assert.equal(accountSwitch.visibleText.includes('README.md'), false);

  process.stdout.write(JSON.stringify({ tree: 'persistent', favorites: 'pass', workspaces: 'opaque-lifecycle', glyphs: 'pass', textPreview: 'bounded', managedPreview: 'stream-capped', managedOpen: 'opaque', galleryOpen: 'opaque', managedActions: 'opaque', managedShowInFiles: 'expired-ref-reissue+ancestor-up', hostInterop: 'opaque-workspace', locationWizard: 'shared-whole-disk', liveWatch: 'path-free', policyRevalidation: 'retain-5xx-purge-revoked', detailsSort: 'pass', columnSort: 'pass', columnKeyboard: 'pass', accountSwitch: 'pass' }) + '\n');
} finally {
  if (socket) socket.close();
  chromium.kill('SIGTERM');
  await new Promise(resolve => chromium.once('exit', resolve));
  fs.rmSync(profile, { recursive: true, force: true });
}
