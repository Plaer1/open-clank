import assert from 'node:assert/strict';
import test from 'node:test';
import { FilesServiceClient, FilesServiceError } from '../../static/js/filesServiceClient.js';

function response(body, ok = true, status = 200) {
  return { ok, status, async json() { return body; } };
}

test('preserves the global receiver required by Window.fetch', async () => {
  let receiver = null;
  const client = new FilesServiceClient({
    fetchImpl: async function () {
      receiver = this;
      return response({ data: { entries: [] } });
    },
  });
  await client.listDirectory('/workspace');
  assert.equal(receiver, globalThis);
});

test('deduplicates directory requests and caches bounded metadata', async () => {
  let calls = 0;
  const client = new FilesServiceClient({
    fetchImpl: async () => {
      calls += 1;
      await new Promise((resolve) => setTimeout(resolve, 2));
      return response({ data: { entries: [{ name: 'main.rs' }] } });
    },
  });
  const [first, second] = await Promise.all([
    client.listDirectory('/workspace'),
    client.listDirectory('/workspace'),
  ]);
  assert.deepEqual(first, second);
  assert.equal(calls, 1);
  await client.listDirectory('/workspace');
  assert.equal(calls, 1);
});

test('callers can bypass directory and stat caches for live explorer state', async () => {
  let calls = 0;
  const client = new FilesServiceClient({
    fetchImpl: async () => {
      calls += 1;
      return response({ data: { call: calls, entries: [] } });
    },
  });
  const first = await client.listDirectory('/workspace', { cache: false });
  const second = await client.listDirectory('/workspace', { cache: false });
  await client.stat('/workspace', { cache: false });
  await client.stat('/workspace', { cache: false });
  assert.equal(first.data.call, 1);
  assert.equal(second.data.call, 2);
  assert.equal(calls, 4);
});

test('media previews mint and revoke opaque capabilities without raw-path content URLs', async () => {
  const calls = [];
  const token = 'p'.repeat(43);
  const client = new FilesServiceClient({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      if (options.method === 'POST') {
        return response({ token, url: `/api/odysseus-files/preview/${token}`, kind: 'image', media_type: 'image/png', size: 12 });
      }
      return response({ ok: true });
    },
  });

  const handle = await client.mintPreviewHandle('/private/photo.png', 'image');
  await client.revokePreviewHandle(handle.token);

  assert.equal(handle.url, `/api/odysseus-files/preview/${token}`);
  assert.equal(handle.url.includes('/private/photo.png'), false);
  assert.equal(calls[0].url, '/api/odysseus-files/preview-handles');
  assert.equal(calls[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].options.body), { path: '/private/photo.png', kind: 'image' });
  assert.equal(calls[1].url, `/api/odysseus-files/preview-handles/${token}`);
  assert.equal(calls[1].options.method, 'DELETE');
});

test('concurrent mutations never deduplicate by endpoint or path', async () => {
  const pending = [];
  const calls = [];
  const client = new FilesServiceClient({
    fetchImpl: (url, options) => new Promise((resolve) => {
      const body = JSON.parse(options.body);
      calls.push({ url, body });
      pending.push(() => resolve(response({ data: { path: body.path, text: body.text } })));
    }),
  });

  const first = client.writeText('/a.txt', 'first');
  const second = client.writeText('/b.txt', 'second');
  const third = client.writeText('/a.txt', 'newer');
  await new Promise(resolve => setTimeout(resolve, 0));

  assert.equal(calls.length, 3);
  assert.deepEqual(calls.map(call => call.body), [
    { path: '/a.txt', text: 'first' },
    { path: '/b.txt', text: 'second' },
    { path: '/a.txt', text: 'newer' },
  ]);
  pending.forEach(resolve => resolve());
  assert.deepEqual(await Promise.all([first, second, third]), [
    { data: { path: '/a.txt', text: 'first' } },
    { data: { path: '/b.txt', text: 'second' } },
    { data: { path: '/a.txt', text: 'newer' } },
  ]);
});

test('does not cache large reads and normalizes typed errors', async () => {
  let calls = 0;
  const client = new FilesServiceClient({
    fetchImpl: async () => {
      calls += 1;
      return response({ code: 'denied', message: 'operation denied' }, false, 403);
    },
  });
  await assert.rejects(() => client.readRange('/secret', { length: 300 * 1024 }), (error) => {
    assert.ok(error instanceof FilesServiceError);
    assert.equal(error.code, 'denied');
    return true;
  });
  await assert.rejects(() => client.readRange('/secret', { length: 300 * 1024 }), FilesServiceError);
  assert.equal(calls, 2);
});

test('text preview uses the dedicated bounded server route', async () => {
  const calls = [];
  const client = new FilesServiceClient({
    fetchImpl: async (url, options) => {
      calls.push({ url, options });
      return response({ data: { text: 'head\n\n…\n\ntail', truncated: true } });
    },
  });

  const result = await client.readTextPreview('/work/huge.log');

  assert.equal(result.data.truncated, true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/odysseus-files/preview-text?path=%2Fwork%2Fhuge.log');
});

test('search query changes remain uncached and cancellation reaches fetch', async () => {
  let aborted = false;
  const client = new FilesServiceClient({
    fetchImpl: async (_url, options) => new Promise((resolve, reject) => {
      options.signal.addEventListener('abort', () => { aborted = true; reject(new DOMException('aborted', 'AbortError')); });
      void resolve;
    }),
  });
  const promise = client.search('/workspace', 'needle');
  client.cancel('/api/odysseus-files/search?path=%2Fworkspace&query=needle&content=false&max_results=100&max_entries=10000&max_depth=32&include_hidden=false&case_sensitive=false');
  await assert.rejects(promise, { name: 'AbortError' });
  assert.equal(aborted, true);
});

test('deduped callers can cancel independently', async () => {
  let resolveFetch;
  let aborted = 0;
  const client = new FilesServiceClient({
    fetchImpl: async (_url, options) => new Promise((resolve, reject) => {
      resolveFetch = () => resolve(response({ data: { matches: [] } }));
      options.signal.addEventListener('abort', () => {
        aborted += 1;
        reject(new DOMException('aborted', 'AbortError'));
      }, { once: true });
    }),
  });
  const firstController = new AbortController();
  const secondController = new AbortController();
  const first = client.search('/workspace', 'needle', { signal: firstController.signal });
  await new Promise((resolve) => setTimeout(resolve, 0));
  const second = client.search('/workspace', 'needle', { signal: secondController.signal });
  firstController.abort();
  await assert.rejects(first, { name: 'AbortError' });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(aborted, 0);
  resolveFetch();
  await second;
  assert.equal(aborted, 0);
});
