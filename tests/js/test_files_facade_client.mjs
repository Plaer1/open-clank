import assert from 'node:assert/strict';
import test from 'node:test';

import { FilesFacadeClient, FilesFacadeError } from '../../static/js/filesFacadeClient.js';

function jsonResponse(body, { status = 200 } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return body; },
  };
}

test('children sends only opaque ref, cursor, sort, query, and bound limit', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async function (url, options) {
      assert.equal(this, globalThis);
      calls.push([url, options]);
      return jsonResponse({ entries: [] });
    },
  });
  await client.children('rr1.opaque', {
    cursor: 'fc1.cursor',
    limit: 17,
    sort: { key: 'modified', direction: 'desc', directories_first: true },
    query: 'report',
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/facade/children');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    parent_ref: 'rr1.opaque',
    cursor: 'fc1.cursor',
    limit: 17,
    sort: { key: 'modified', direction: 'desc', directories_first: true },
    query: 'report',
  });
});

test('children preserves every advertised direction and binds the supplied opaque cursor', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (_url, options) => {
      calls.push(JSON.parse(options.body));
      return jsonResponse({ entries: [], sort_keys: ['name', 'kind', 'modified', 'size'] });
    },
  });
  for (const key of ['name', 'kind', 'modified', 'size']) {
    for (const direction of ['asc', 'desc']) {
      await client.children('rr1.opaque', {
        cursor: `fc1.${key}-${direction}`,
        sort: { key, direction, directories_first: false },
      });
    }
  }
  assert.deepEqual(calls.map(body => [body.sort.key, body.sort.direction, body.sort.directories_first, body.cursor]), [
    ['name', 'asc', false, 'fc1.name-asc'], ['name', 'desc', false, 'fc1.name-desc'],
    ['kind', 'asc', false, 'fc1.kind-asc'], ['kind', 'desc', false, 'fc1.kind-desc'],
    ['modified', 'asc', false, 'fc1.modified-asc'], ['modified', 'desc', false, 'fc1.modified-desc'],
    ['size', 'asc', false, 'fc1.size-asc'], ['size', 'desc', false, 'fc1.size-desc'],
  ]);
});

test('provider-wide search sends a bounded query without scope identities', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ entries: [], providers: {} });
    },
  });
  await client.search('quarterly report', {
    limit: 999,
    sort: { key: 'modified', direction: 'desc', directories_first: false },
  });
  assert.equal(calls[0][0], '/facade/search');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    query: 'quarterly report',
    limit: 200,
    sort: { key: 'modified', direction: 'desc', directories_first: false },
  });
  assert.throws(() => client.search('  '), /search query is required/);
});

test('FastAPI detail envelope remains machine readable', async () => {
  const client = new FilesFacadeClient({
    fetchImpl: async () => jsonResponse({
      detail: { code: 'stale_cursor', message: 'Listing changed' },
    }, { status: 409 }),
  });
  await assert.rejects(
    client.children('rr1.opaque'),
    (error) => error instanceof FilesFacadeError
      && error.code === 'stale_cursor'
      && error.status === 409
      && error.message === 'Listing changed',
  );
});

test('abort is preserved without provider wrapping', async () => {
  const controller = new AbortController();
  const client = new FilesFacadeClient({
    fetchImpl: async (_url, options) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
    }),
  });
  const request = client.roots({ signal: controller.signal });
  controller.abort();
  await assert.rejects(request, (error) => error?.name === 'AbortError');
});

test('roots carries a validated Copal workspace hint without raw resource identity', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url) => {
      calls.push(url);
      return jsonResponse({ entries: [] });
    },
  });
  await client.roots({ copalWorkspace: 'course-1' });
  assert.deepEqual(calls, ['/facade/roots?copal_workspace=course-1']);
  assert.throws(() => client.roots({ copalWorkspace: '../secret' }), /workspace is invalid/);
});

test('content URL carries only the encoded opaque ref', () => {
  const client = new FilesFacadeClient({ baseUrl: '/facade', fetchImpl: async () => jsonResponse({}) });
  assert.equal(client.contentUrl('rr1.opaque/value'), '/facade/content/rr1.opaque%2Fvalue?purpose=download');
  assert.equal(client.contentUrl('rr1.opaque/value', { purpose: 'preview' }), '/facade/content/rr1.opaque%2Fvalue?purpose=preview');
  assert.throws(() => client.contentUrl(''), /resource reference is required/);
  assert.throws(() => client.contentUrl('rr1.opaque', { purpose: 'execute' }), /content purpose is invalid/);
  assert.equal(
    client.thumbnailUrl('rr1.opaque/value', { width: 96, height: 64, scale: 2 }),
    '/facade/thumbnail/rr1.opaque%2Fvalue?width=96&height=64&scale=2',
  );
  assert.equal(
    client.thumbnailUrl('rr1.opaque', { width: 9999, height: 0, scale: 9 }),
    '/facade/thumbnail/rr1.opaque?width=1024&height=160&scale=3',
  );
  assert.throws(() => client.thumbnailUrl(''), /resource reference is required/);
});

test('open sends only the opaque ref and fixed empty action envelope', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ action: 'open', target: { app: 'gallery' } });
    },
  });
  const result = await client.open('rr1.opaque');
  assert.equal(result.target.app, 'gallery');
  assert.equal(calls[0][0], '/facade/action');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    resource_ref: 'rr1.opaque',
    action: 'open',
    args: {},
  });
});

test('Workspace handoff sends only an opaque Host ref and a fixed purpose', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ workspace: { id: 'workspace-opaque' }, open_relative: 'note.txt' });
    },
  });
  const result = await client.workspace('rr1.host-opaque', 'app_folder');
  assert.equal(result.workspace.id, 'workspace-opaque');
  assert.equal(calls[0][0], '/facade/workspace');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    resource_ref: 'rr1.host-opaque',
    purpose: 'app_folder',
  });
  assert.throws(() => client.workspace('rr1.host-opaque', 'execute'), /purpose is invalid/);
});

test('Workspace catalog and lifecycle carry stable ids without paths', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ entries: [], workspace: { id: 'workspace-opaque', revision: 2 } });
    },
  });
  await client.workspaces();
  await client.workspaces({ includeArchived: true });
  await client.updateWorkspace('workspace-opaque', {
    name: 'Renamed', expected_revision: 1,
  });
  await client.updateWorkspace('workspace-opaque', {
    archived: true, expected_revision: 2,
  });
  assert.deepEqual(calls.map(([url, options]) => [url, options.method || 'GET']), [
    ['/facade/workspaces?include_archived=false', 'GET'],
    ['/facade/workspaces?include_archived=true', 'GET'],
    ['/facade/workspaces/workspace-opaque', 'PATCH'],
    ['/facade/workspaces/workspace-opaque', 'PATCH'],
  ]);
  assert.deepEqual(JSON.parse(calls[2][1].body), { name: 'Renamed', expected_revision: 1 });
  assert.deepEqual(JSON.parse(calls[3][1].body), { archived: true, expected_revision: 2 });
  assert.throws(() => client.updateWorkspace('', { name: 'x' }), /id is invalid/);
  assert.throws(() => client.updateWorkspace('workspace-opaque', {}), /update is empty/);
  assert.throws(() => client.updateWorkspace('workspace-opaque', { name: '  ' }), /name is invalid/);
});

test('Workspace reveal sends only stable id and a relative path', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ resource: { id: 'resource-file' }, parent: { id: 'resource-folder' } });
    },
  });
  const result = await client.workspaceResource('workspace-opaque', 'src/main.js');
  assert.equal(result.resource.id, 'resource-file');
  assert.equal(calls[0][0], '/facade/workspace-resource');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    workspace_id: 'workspace-opaque',
    relative_path: 'src/main.js',
  });
  assert.throws(() => client.workspaceResource('', 'src/main.js'), /id is invalid/);
  assert.throws(() => client.workspaceResource('workspace-opaque', '../secret'), /path is invalid/);
  assert.throws(() => client.workspaceResource('workspace-opaque', '/absolute'), /path is invalid/);
});

test('idempotent actions send only opaque ref, closed verb, and boolean value', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ action: 'favorite.set', state: { favorite: true } });
    },
  });
  const result = await client.action('rr1.opaque', 'favorite.set', { value: true });
  assert.deepEqual(result.state, { favorite: true });
  assert.equal(calls[0][0], '/facade/action');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    resource_ref: 'rr1.opaque',
    action: 'favorite.set',
    args: { value: true },
  });
  assert.throws(() => client.action('rr1.opaque', 'delete', { value: true }), /unsupported/);
  assert.throws(() => client.action('rr1.opaque', 'archive.set', { value: 'true' }), /boolean value/);
  assert.throws(() => client.action('rr1.opaque', 'archive.set', { value: false, id: 'raw' }), /boolean value/);
});

test('Places API persists only opaque refs and validates opaque place ids', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ entries: [] });
    },
  });
  await client.places();
  await client.savePlace('rr1.opaque');
  await client.removePlace('place-0123456789abcdef0123456789abcdef');
  assert.deepEqual(calls.map(([url, options]) => [url, options.method || 'GET']), [
    ['/facade/places', 'GET'],
    ['/facade/places', 'POST'],
    ['/facade/places/place-0123456789abcdef0123456789abcdef', 'DELETE'],
  ]);
  assert.deepEqual(JSON.parse(calls[1][1].body), { resource_ref: 'rr1.opaque' });
  assert.throws(() => client.removePlace('../raw'), /place id is invalid/);
});

test('openResource loads an exact app payload using only the opaque ref', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ target: { app: 'document_editor' }, payload: { title: 'Exact' } });
    },
  });
  const result = await client.openResource('rr1.exact');
  assert.equal(result.payload.title, 'Exact');
  assert.equal(calls[0][0], '/facade/open-resource');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { resource_ref: 'rr1.exact' });
});

test('saveResource sends only an opaque ref, host fingerprint, and immutable text', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ outcome: 'applied', revision: { kind: 'hostFingerprint', value: 'fp-next' } });
    },
  });
  const result = await client.saveResource('rr1.host', {
    expectedRevision: { kind: 'hostFingerprint', value: 'fp-before' },
    text: 'updated\r\ntext',
  });
  assert.equal(result.revision.value, 'fp-next');
  assert.equal(calls[0][0], '/facade/save-resource');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    resource_ref: 'rr1.host',
    expected_revision: { kind: 'hostFingerprint', value: 'fp-before' },
    text: 'updated\r\ntext',
  });
  assert.equal(calls[0][1].body.includes('path'), false);
  assert.throws(() => client.saveResource('rr1.host', { expectedRevision: { kind: 'copalHead', value: 'head' }, text: 'x' }), /host fingerprint/);
});

test('createResource sends an opaque writable parent and Markdown-only filename', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ action: 'create', resource: { ref: 'rr1.created' } });
    },
  });
  await client.createResource('rr1.parent', { name: 'Meeting.md', text: 'body', actionId: 'template-1' });
  assert.equal(calls[0][0], '/facade/create');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    parent_ref: 'rr1.parent', name: 'Meeting.md', text: 'body', action_id: 'template-1',
  });
  assert.throws(() => client.createResource('rr1.parent', { name: '../secret.md' }), /invalid/);
  assert.throws(() => client.createResource('rr1.parent', { name: 'Meeting.txt' }), /Markdown/);
});

test('createFile uses a zero-byte named import and validates the durable receipt', async () => {
  let request;
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      request = { url, options };
      return jsonResponse({ operation_id: 'files-create-1', generation: 7, state: 'complete', items: [{ item_id: 'files-item-1', outcome: 'committed', resource_ref: 'rr1.file' }] });
    },
  });
  const receipt = await client.createFile('rr1.parent', {
    name: "Résumé's.md", operationId: 'files-create-1', itemId: 'files-item-1', generation: 7,
  });
  assert.equal(receipt.items[0].resource_ref, 'rr1.file');
  assert.equal(request.url, '/facade/imports');
  const metadata = JSON.parse(request.options.body.get('metadata'));
  assert.deepEqual(metadata, {
    operation_id: 'files-create-1', item_id: 'files-item-1', generation: 7,
    destination_ref: 'rr1.parent', name: "Résumé's.md", relative_path: "Résumé's.md", collision: 'fail',
  });
  assert.equal((await request.options.body.get('file').arrayBuffer()).byteLength, 0);
  assert.equal(request.options.body.get('file').name, "Résumé's.md");
  await assert.rejects(client.createFile('rr1.parent', { name: 'bad/name', operationId: 'op', itemId: 'item', generation: 7 }), /invalid/);
});

test('createDirectory sends an opaque parent, bounded operation, and generation', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ action: 'create-directory', resource: { ref: 'rr1.folder' } });
    },
  });
  await client.createDirectory('rr1.parent', { name: 'Nested', operationId: 'editor-mkdir-1', generation: 7 });
  assert.equal(calls[0][0], '/facade/create-directory');
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    parent_ref: 'rr1.parent', name: 'Nested', operation_id: 'editor-mkdir-1', generation: 7, collision: 'fail',
  });
  assert.throws(() => client.createDirectory('rr1.parent', { name: '../secret', operationId: 'op', generation: 7 }), /invalid/);
  assert.throws(() => client.createDirectory('rr1.parent', { name: 'Nested', operationId: 'op', generation: -1 }), /invalid/);
});

test('createDirectory carries an optional parent revision and explicit reuse policy', async () => {
  let body;
  const client = new FilesFacadeClient({
    fetchImpl: async (_url, options) => { body = JSON.parse(options.body); return jsonResponse({}); },
  });
  await client.createDirectory('rr1.parent', {
    name: 'Nested', operationId: 'editor-mkdir-2', generation: 7,
    expectedRevision: { kind: 'hostFingerprint', value: 'parent-fp' }, collision: 'reuse',
  });
  assert.deepEqual(body, {
    parent_ref: 'rr1.parent', name: 'Nested', operation_id: 'editor-mkdir-2', generation: 7,
    expected_revision: { kind: 'hostFingerprint', value: 'parent-fp' }, collision: 'reuse',
  });
  assert.throws(() => client.createDirectory('rr1.parent', {
    name: 'Nested', operationId: 'op', generation: 7, collision: 'rename',
  }), /collision/);
});

test('reveal sends only the opaque resource ref', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ parent: { ref: 'rr1.parent' }, resource: { ref: 'rr1.fresh' } });
    },
  });
  const result = await client.reveal('rr1.exact');
  assert.equal(result.parent.ref, 'rr1.parent');
  assert.equal(calls[0][0], '/facade/reveal');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { resource_ref: 'rr1.exact' });
  assert.throws(() => client.reveal(''), /reference is required/);
});

test('exact reissue sends only the stale opaque resource ref', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return jsonResponse({ resource: { ref: 'rr1.current' } });
    },
  });
  const result = await client.reissue('rr1.expired');
  assert.equal(result.resource.ref, 'rr1.current');
  assert.equal(calls[0][0], '/facade/reissue');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { resource_ref: 'rr1.expired' });
  assert.throws(() => client.reissue(''), /reference is required/);
});

test('watch streams path-free hints and sends only the opaque folder ref', async () => {
  const calls = [];
  const encoder = new TextEncoder();
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, options]);
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(': keepalive\n\nevent: files-change\n'));
          controller.enqueue(encoder.encode('data: {"sequence":0,"kind":"modified","rescan_required":false,"observed_unix_ms":9}\n\n'));
          controller.close();
        },
      }), { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    },
  });
  const stream = client.watch('rr1.opaque-folder');
  assert.deepEqual((await stream.next()).value, {
    sequence: 0,
    kind: 'modified',
    rescan_required: false,
    observed_unix_ms: 9,
  });
  await stream.return();
  assert.equal(calls[0][0], '/facade/watch');
  assert.equal(calls[0][1].method, 'POST');
  assert.deepEqual(JSON.parse(calls[0][1].body), { resource_ref: 'rr1.opaque-folder' });
  assert.equal(calls[0][1].body.includes('path'), false);
});

test('transfer and Base query publish bounded generation and opaque refs', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, JSON.parse(options.body)]);
      return jsonResponse({ operation_id: 'op-1', generation: 9, state: 'complete', items: [] });
    },
  });
  await client.transferResources({
    operationId: 'op-1', generation: 9, kind: 'copy', destinationRef: 'rr1.dest',
    sources: [{ itemId: 'item-1', resourceRef: 'rr1.source', expectedRevision: { kind: 'hostFingerprint', value: 'v1' } }],
  });
  await client.queryBaseResource({ baseRef: 'rr1.base', corpusRef: 'rr1.corpus', generation: 9, pageSize: 50 });
  assert.equal(calls[0][0], '/facade/transfers');
  assert.deepEqual(calls[0][1], {
    operation_id: 'op-1', generation: 9, kind: 'copy',
    sources: [{ item_id: 'item-1', resource_ref: 'rr1.source', expected_revision: { kind: 'hostFingerprint', value: 'v1' } }],
    destination_ref: 'rr1.dest', collision: 'fail',
  });
  assert.equal(calls[1][0], '/facade/bases/query');
  assert.equal(calls[1][1].base_ref, 'rr1.base');
  assert.equal(calls[1][1].corpus_ref, 'rr1.corpus');
  assert.equal(calls[1][1].page_size, 50);
});

test('attachment and import clients reject malformed variants before network calls', async () => {
  const client = new FilesFacadeClient({ baseUrl: '/facade', fetchImpl: async () => jsonResponse({}) });
  assert.throws(() => client.prepareAttachment({ operationId: 'op', generation: 1, source: {}, target: { kind: 'copal_document' }, mode: 'link' }), /source must have one variant/);
  await assert.rejects(() => client.importFile(new Blob(['x']), { operationId: 'op', itemId: 'i', generation: 1, destinationRef: 'rr1.dest', name: '../escape.md' }), /import request is invalid/);
});

test('attachment import receipts send the required item id and canonical target', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url, options) => {
      calls.push([url, JSON.parse(options.body)]);
      return jsonResponse({
        operation_id: 'attach-1', generation: 2, preparation_receipt_id: 'prep-1',
        source_revision: { kind: 'hostFingerprint', value: 's1' },
        target_identity: { kind: 'copal_document', resource_ref: 'rr1.target' },
        target_revision: { kind: 'copalHead', value: 't1' },
        insertion: { format: 'markdown', link_target: 'asset', label: 'a.txt', media_kind: 'text/plain' },
      });
    },
  });
  await client.prepareAttachment({
    operationId: 'attach-1', generation: 2,
    source: { importReceiptId: 'import-1', itemId: 'item-1' },
    target: { kind: 'copal_document', resourceRef: 'rr1.target' }, mode: 'embed',
  });
  assert.deepEqual(calls[0][1].source, { import_receipt_id: 'import-1', item_id: 'item-1' });
  assert.deepEqual(calls[0][1].target, { kind: 'copal_document', resource_ref: 'rr1.target' });
});

test('transfer and lost-response receipts bind operation and generation exactly', async () => {
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url) => {
      if (url.endsWith('/transfers')) return jsonResponse({ operation_id: 'other', generation: 7, state: 'complete', items: [] });
      return jsonResponse({ operation_id: 'op-1', generation: 8, state: 'pending', items: [] });
    },
  });
  await assert.rejects(() => client.transferResources({ operationId: 'op-1', generation: 7, kind: 'copy', destinationRef: 'rr1.dest', sources: [{ itemId: 'i', resourceRef: 'rr1.source' }] }), /receipt is invalid/);
  await assert.rejects(() => client.operationReceipt('op-1', { generation: 7 }), /receipt is invalid/);
});

test('import receipts reject wrong operation or generation before Files can apply selection', async () => {
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async () => jsonResponse({ operation_id: 'other', generation: 2, state: 'complete', items: [{ item_id: 'item-1', outcome: 'committed' }] }),
  });
  await assert.rejects(() => client.importFile(new Blob(['x']), { operationId: 'op-1', itemId: 'item-1', generation: 2, destinationRef: 'rr1.dest', name: 'a.txt' }), /receipt is invalid/);
});

test('attachment preparation recovery binds stable operation, generation, and workspace', async () => {
  const calls = [];
  const client = new FilesFacadeClient({
    baseUrl: '/facade',
    fetchImpl: async (url) => {
      calls.push(url);
      return jsonResponse({ operation_id: 'attach-1', generation: 4, state: 'pending', preparation: null });
    },
  });
  const status = await client.attachmentPreparationReceipt('attach-1', { workspace: 'team-a' });
  assert.equal(status.state, 'pending');
  assert.equal(calls[0], '/facade/attachments/attach-1?copal_workspace=team-a');
});
