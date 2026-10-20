#!/usr/bin/env node

import assert from 'node:assert/strict';
import test from 'node:test';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

test('mounted Files creates nested import directories through opaque refs', async () => {
  const page = '<!doctype html><html><body><script type="module">import(\'/static/js/files.js\').then(async module => { await module.default.open(); window.__ready = true; }).catch(error => { window.__error = String(error.stack || error); });</script></body></html>';
  const creates = [];
  const imports = [];
  const apiCalls = [];
  const operations = new Map();
  let rootCalls = 0;
  await withCopalBrowser({
    page,
    request: async (request, response) => {
      const url = new URL(request.url, 'http://fixture');
      const pathname = url.pathname;
      apiCalls.push(pathname);
      if (!pathname.startsWith('/api/')) return false;
      const send = (body, status = 200) => {
        response.writeHead(status, { 'content-type': 'application/json' });
        response.end(JSON.stringify(body));
      };
      if (pathname === '/api/auth/status') { send({ username: 'nested-fixture' }); return true; }
      if (pathname === '/api/files-v1/roots') {
        rootCalls += 1;
        send({
          entries: [{ id: 'root', ref: 'rr-root', provider: 'host', kind: 'folder', name: 'Host', capabilities: ['children', 'write'], revision: { kind: 'hostFingerprint', value: `root-${rootCalls}` } }],
          import_capabilities: { host: { create_directory: true, max_total_bytes: 1024, max_chunk_bytes: 1024 } },
          policy_generation: 4,
        });
        return true;
      }
      if (pathname === '/api/files-v1/children') {
        const body = JSON.parse(await requestBody(request));
        if (body.parent_ref === 'rr-root') {
          send({ entries: [{ id: 'drop', ref: 'rr-drop', provider: 'host', kind: 'folder', name: 'Drop', capabilities: ['children', 'write'], revision: { kind: 'hostFingerprint', value: 'drop-1' } }], next_cursor: null });
        } else send({ entries: [], next_cursor: null });
        return true;
      }
      if (pathname === '/api/files-v1/places' || pathname === '/api/files-v1/workspaces') { send({ entries: [] }); return true; }
      if (pathname === '/api/files-v1/stat') {
        const body = JSON.parse(await requestBody(request));
        const ref = body.resource_ref;
        const name = ref === 'rr-root' ? 'Host' : ref === 'rr-drop' ? 'Drop' : ref.replace(/^rr-/, '');
        send({ resource: { ref, provider: 'host', kind: 'folder', name, capabilities: ['children', 'write'], revision: { kind: 'hostFingerprint', value: `${ref}-revision` } } });
        return true;
      }
      if (pathname === '/api/files-v1/create-directory') {
        const body = JSON.parse(await requestBody(request));
        creates.push(body);
        if (body.name === 'Denied') { send({ detail: { code: 'forbidden', message: 'directory denied' } }, 403); return true; }
        const ref = `rr-${body.name.toLowerCase()}`;
        const resource = { ref, provider: 'host', kind: 'folder', name: body.name, capabilities: ['children', 'write'], revision: { kind: 'hostFingerprint', value: `${ref}-revision` } };
        operations.set(body.operation_id, { operation_id: body.operation_id, generation: body.generation, state: 'complete', items: [{ item_id: body.operation_id, outcome: 'committed', resource_ref: ref, revision: resource.revision }] });
        send({ action: 'create-directory', resource });
        return true;
      }
      if (pathname.startsWith('/api/files-v1/operations/')) { send(operations.get(pathname.split('/').at(-1)) || { state: 'pending', items: [] }); return true; }
      if (pathname === '/api/files-v1/imports') {
        const text = (await requestBody(request)).toString();
        const metadata = JSON.parse(text.match(/name="metadata"\r?\n\r?\n([\s\S]*?)\r?\n--/)?.[1] || '{}');
        imports.push(metadata);
        send({ operation_id: metadata.operation_id, generation: metadata.generation, state: 'complete', items: [{ item_id: metadata.item_id, outcome: 'committed', resource_ref: 'rr-file' }] });
        return true;
      }
      send({ detail: { code: 'missing', message: pathname } }, 404);
      return true;
    },
  }, async ({ evaluate, until }) => {
    await until('window.__ready || window.__error', 'nested Files mount');
    assert.equal(await evaluate('window.__error || null'), null);
    await until('document.querySelector("[data-file-name=Drop]")', 'drop destination');
    await evaluate(`(() => {
      const file = new File(['nested'], 'note.txt', { type:'text/plain' });
      const fileEntry = { name:'note.txt', isFile:true, isDirectory:false, file:callback => callback(file) };
      const subEntry = { name:'Sub', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[fileEntry], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const topEntry = { name:'Top', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[subEntry], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const item = { kind:'file', webkitGetAsEntry:() => topEntry, getAsFileSystemHandle:() => null, getAsFile:() => file };
      const dataTransfer = { items:[item], files:[], getData:() => '' };
      const event = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:dataTransfer });
      document.querySelector('[data-file-name=Drop]').dispatchEvent(event);
    })()`);
    await until('document.body.innerText.includes("imported") || document.body.innerText.includes("skipped")', 'nested import receipt');
    assert.equal(creates.length, 2, `one create-directory request per nested segment (calls=${apiCalls.join(',')}, target=${await evaluate('document.querySelector("[data-file-name=Drop]")?.outerHTML || "missing"')}, body=${await evaluate('document.body.innerText.slice(-300)')})`);
    assert.deepEqual(creates.map(item => item.name), ['Top', 'Sub']);
    assert.equal(creates[0].parent_ref, 'rr-drop');
    assert.equal(creates[1].parent_ref, 'rr-top');
    assert.equal(creates[0].collision, 'reuse');
    assert.equal(creates[0].generation, 4);
    assert.notEqual(creates[0].operation_id, creates[1].operation_id);
    assert.equal(imports.length, 1);
    assert.equal(imports[0].destination_ref, 'rr-sub');
    assert.equal(imports[0].relative_path, 'note.txt');
    assert.equal(imports[0].collision, 'fail');

    await evaluate(`(() => {
      const file = new File(['direct'], 'direct.txt', { type:'text/plain' });
      const item = { kind:'file', webkitGetAsEntry:() => null, getAsFileSystemHandle:() => null, getAsFile:() => file };
      const dataTransfer = { items:[item], files:[file], getData:() => '' };
      const event = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:dataTransfer });
      document.querySelector('[data-file-name=Drop]').dispatchEvent(event);
    })()`);
    await new Promise(resolve => setTimeout(resolve, 500));
    assert.equal(imports.length, 2, 'direct file request completed');
    assert.equal(apiCalls.some(path => path.startsWith('/api/odysseus-files')), false, 'mounted nested import never calls legacy Files API');
    assert.equal(creates.length, 2, 'direct file does not create a directory');
    assert.equal(imports.at(-1).destination_ref, 'rr-drop');
    assert.equal(imports.at(-1).relative_path, 'direct.txt');

    await evaluate(`(() => {
      const empty = { name:'Empty', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const emptyChild = { name:'EmptyChild', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const emptySibling = { name:'EmptySibling', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const bundle = { name:'Bundle', isFile:false, isDirectory:true, createReader:() => {
        const batches = [[emptyChild, emptySibling], []];
        return { readEntries:callback => callback(batches.shift() || []) };
      } };
      const item = entry => ({ kind:'file', webkitGetAsEntry:() => entry, getAsFileSystemHandle:() => null, getAsFile:() => null });
      const dataTransfer = { items:[item(empty), item(bundle)], files:[], getData:() => '' };
      const event = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:dataTransfer });
      document.querySelector('[data-file-name=Drop]').dispatchEvent(event);
    })()`);
    await until('document.body.innerText.includes("0 imported")', 'empty directory receipt');
    assert.deepEqual(creates.slice(2).map(item => item.name), ['Bundle', 'EmptyChild', 'EmptySibling', 'Empty']);
    assert.equal(imports.length, 2, 'empty folders do not submit file imports');
    assert.equal(creates.slice(2).every(item => item.collision === 'reuse'), true);

    await evaluate(`(() => {
      const unsafe = { name:'../escape', isFile:false, isDirectory:true, createReader:() => ({ readEntries:callback => callback([]) }) };
      const item = { kind:'file', webkitGetAsEntry:() => unsafe, getAsFileSystemHandle:() => null, getAsFile:() => null };
      const dataTransfer = { items:[item], files:[], getData:() => '' };
      const event = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:dataTransfer });
      document.querySelector('[data-file-name=Drop]').dispatchEvent(event);
    })()`);
    await until('document.body.innerText.includes("unsafe_relative_path")', 'unsafe empty directory rejection');
    assert.equal(creates.length, 6, 'unsafe empty directory is rejected before create-directory');

    await evaluate(`(() => {
      const denied = { name:'Denied', isFile:false, isDirectory:true, createReader:() => ({ readEntries:callback => callback([]) }) };
      const item = { kind:'file', webkitGetAsEntry:() => denied, getAsFileSystemHandle:() => null, getAsFile:() => null };
      const dataTransfer = { items:[item], files:[], getData:() => '' };
      const event = new Event('drop', { bubbles:true, cancelable:true });
      Object.defineProperty(event, 'dataTransfer', { value:dataTransfer });
      document.querySelector('[data-file-name=Drop]').dispatchEvent(event);
    })()`);
    await until('document.body.innerText.includes("forbidden")', 'denied empty directory receipt');
    assert.equal(creates.at(-1).name, 'Denied');
    assert.equal(imports.length, 2, 'denied empty folders do not submit file imports');
  });
});

async function requestBody(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks);
}
