import test from 'node:test';
import assert from 'node:assert/strict';
import { createPreviewSession } from '../static/js/browser/previewSession.js';

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}

test('create sends only the supplied Files dependency refs and revisions', async () => {
  const originalFetch = globalThis.fetch;
  let captured = null;
  globalThis.fetch = async (url, options = {}) => {
    if (url === '/api/browser-previews/cancel') return new Response('{"cancelled":true}', { status:200, headers:{'content-type':'application/json'} });
    if (url === '/api/browser-previews' && options.method === 'POST') {
      captured = JSON.parse(options.body);
      return new Response(JSON.stringify({ session_id:'dependency-session' }), { status:201, headers:{'content-type':'application/json'} });
    }
    if (options.method === 'DELETE') return new Response(null, { status:204 });
    throw new Error(`unexpected request: ${options.method} ${url}`);
  };
  try {
    const manager = createPreviewSession();
    const dependencies = [{ path:'css/main.css', resource_ref:'sealed-css-ref', expected_revision:{kind:'hostFingerprint',value:'css-r7'} }];
    await manager.create({ resourceRef:'sealed-html-ref', dependencies });
    assert.deepEqual(captured.dependencies, dependencies);
    assert.equal(captured.resource_ref, 'sealed-html-ref');
    await manager.close();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('a create response arriving after close is explicitly revoked', async () => {
  const originalFetch = globalThis.fetch;
  const pending = deferred();
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body || null });
    if (options.method === 'POST' && url === '/api/browser-previews') return pending.promise;
    if (options.method === 'POST') return { ok: true, status: 200, json: async () => ({ cancelled: true }) };
    return { ok: true, status: 204, json: async () => ({}) };
  };
  try {
    const manager = createPreviewSession();
    const opening = manager.create({ resourceRef: 'sealed-resource-ref' });
    await manager.close();
    pending.resolve({ ok: true, status: 201, json: async () => ({ session_id: 'late-session', url: 'http://opaque.artifacts.localhost:4555/index.html' }) });
    assert.deepEqual(await opening, { stale: true, session: null, cleanupConfirmed: true });
    assert.equal(manager.current, null);
    assert.deepEqual(calls.map(call => call.method), ['POST', 'POST', 'DELETE']);
    assert.equal(calls[1].url, '/api/browser-previews/cancel');
    assert.match(calls[2].url, /late-session$/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('superseded create responses cannot become the active session', async () => {
  const originalFetch = globalThis.fetch;
  const first = deferred();
  const calls = [];
  let posts = 0;
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET', body: options.body || null });
    if (options.method === 'POST' && url === '/api/browser-previews') {
      posts += 1;
      if (posts === 1) return first.promise;
      return { ok: true, status: 201, json: async () => ({ session_id: 'new-session' }) };
    }
    if (options.method === 'POST') return { ok: true, status: 200, json: async () => ({ cancelled: true }) };
    return { ok: true, status: 204, json: async () => ({}) };
  };
  try {
    const manager = createPreviewSession();
    const oldOpening = manager.create({ resourceRef: 'old-ref' });
    const currentOpening = manager.create({ resourceRef: 'new-ref' });
    assert.equal((await currentOpening).session.session_id, 'new-session');
    first.resolve({ ok: true, status: 201, json: async () => ({ session_id: 'old-session' }) });
    assert.deepEqual(await oldOpening, { stale: true, session: null, cleanupConfirmed: true });
    assert.equal(manager.current.session_id, 'new-session');
    const createCall = calls.filter(call => call.method === 'POST' && call.url === '/api/browser-previews').at(-1);
    assert.match(JSON.parse(createCall.body).client_id, /^[a-f0-9]{32}$/);
    assert.equal(JSON.parse(createCall.body).client_generation, 2);
    assert.ok(calls.some(call => call.method === 'DELETE' && /old-session$/.test(call.url)));
    await manager.close();
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('create body parsing stays under the timeout and confirms server cancellation', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || 'GET' });
    if (url === '/api/browser-previews' && options.method === 'POST') {
      let bodyController;
      const body = new ReadableStream({ start(controller) { bodyController = controller; } });
      options.signal.addEventListener('abort', () => bodyController.error(new DOMException('aborted', 'AbortError')), { once: true });
      return new Response(body, { status: 201, headers: { 'content-type': 'application/json' } });
    }
    if (url === '/api/browser-previews/cancel') {
      return new Response(JSON.stringify({ cancelled: true }), { status: 200, headers: { 'content-type': 'application/json' } });
    }
    throw new Error(`unexpected request: ${options.method} ${url}`);
  };
  try {
    const manager = createPreviewSession({ requestTimeoutMs: 25, cleanupTimeoutMs: 100 });
    const started = Date.now();
    await assert.rejects(manager.create({ resourceRef: 'sealed-resource-ref' }), error => {
      assert.equal(error.code, 'request_timeout');
      assert.equal(error.cleanupConfirmed, true);
      assert.equal(error.cleanupUnknown, false);
      return true;
    });
    assert.ok(Date.now() - started < 1_000, 'stalled JSON body resolves within the configured request and cancel deadlines');
    assert.deepEqual(calls.map(call => call.url), ['/api/browser-previews', '/api/browser-previews/cancel']);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('close cancels its captured generation when a new create starts during revoke', async () => {
  const originalFetch = globalThis.fetch;
  const revokePending = deferred();
  const calls = [];
  let creates = 0;
  globalThis.fetch = async (url, options = {}) => {
    const method = options.method || 'GET';
    calls.push({ url, method, body: options.body || null });
    if (url === '/api/browser-previews' && method === 'POST') {
      creates += 1;
      return new Response(JSON.stringify({ session_id: `session-${creates}` }), { status: 201, headers: { 'content-type': 'application/json' } });
    }
    if (/\/api\/browser-previews\/session-1$/.test(url) && method === 'DELETE') return revokePending.promise;
    if (url === '/api/browser-previews/cancel') return new Response('{"cancelled":true}', { status: 200, headers: { 'content-type': 'application/json' } });
    return new Response('{}', { status: 204 });
  };
  try {
    const manager = createPreviewSession();
    await manager.create({ resourceRef: 'first-ref' });
    const closing = manager.close();
    await new Promise(resolve => setTimeout(resolve, 0));
    const reopening = manager.create({ resourceRef: 'second-ref' });
    await reopening;
    revokePending.resolve(new Response(null, { status: 204 }));
    await closing;
    const cancelledGenerations = calls
      .filter(call => call.url === '/api/browser-previews/cancel')
      .map(call => JSON.parse(call.body).client_generation);
    assert.deepEqual(cancelledGenerations, [2], 'close keeps its own generation and does not cancel the reopened request');
    assert.equal(manager.current.session_id, 'session-2');
    await manager.close();
  } finally {
    globalThis.fetch = originalFetch;
  }
});
