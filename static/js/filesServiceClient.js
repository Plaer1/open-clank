/**
 * Neutral lazy client shared by Notes, Code, and workspace surfaces.
 *
 * It owns request deduplication, small-result caching, and cancellation of
 * client work. It does not hold editor state or decide filesystem authority.
 */

export class FilesServiceError extends Error {
  constructor(message, { code = 'root_unavailable', status = 0 } = {}) {
    super(message);
    this.name = 'FilesServiceError';
    this.code = code;
    this.status = status;
  }
}

function encode(value) {
  return encodeURIComponent(String(value));
}

function cacheWeight(value) {
  try { return JSON.stringify(value).length; } catch { return 0; }
}

export class FilesServiceClient {
  constructor({ baseUrl = '/api/odysseus-files', fetchImpl = globalThis.fetch, maxCacheBytes = 128 * 1024 * 1024, cacheTtlMs = 30_000 } = {}) {
    if (typeof fetchImpl !== 'function') throw new TypeError('FilesServiceClient requires fetch');
    this.baseUrl = baseUrl.replace(/\/$/, '');
    // `window.fetch` performs a Web IDL receiver check in some browsers. If
    // it is stored directly on this client and then called as
    // `this.fetchImpl(...)`, `this` becomes FilesServiceClient instead of the
    // Window that owns fetch and the browser rejects the call. Normalize the
    // receiver once at the dependency boundary; injected test/worker fetch
    // implementations are functions too and remain compatible when bound.
    this.fetchImpl = fetchImpl.bind(globalThis);
    this.maxCacheBytes = maxCacheBytes;
    this.cacheTtlMs = cacheTtlMs;
    this.cache = new Map();
    this.cacheBytes = 0;
    this.inFlight = new Map();
    this.controllers = new Map();
  }

  _cacheGet(key) {
    const entry = this.cache.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= Date.now()) {
      this.cache.delete(key);
      this.cacheBytes -= entry.weight;
      return undefined;
    }
    this.cache.delete(key);
    this.cache.set(key, entry);
    return entry.value;
  }

  _cachePut(key, value) {
    const weight = cacheWeight(value);
    if (weight > this.maxCacheBytes) return;
    const previous = this.cache.get(key);
    if (previous) this.cacheBytes -= previous.weight;
    this.cache.set(key, { value, weight, expiresAt: Date.now() + this.cacheTtlMs });
    this.cacheBytes += weight;
    while (this.cacheBytes > this.maxCacheBytes && this.cache.size) {
      const oldest = this.cache.keys().next().value;
      const evicted = this.cache.get(oldest);
      this.cache.delete(oldest);
      this.cacheBytes -= evicted.weight;
    }
  }

  _abortError() {
    return new DOMException('The operation was aborted.', 'AbortError');
  }

  _subscribe(entry, signal = null) {
    if (signal?.aborted) throw this._abortError();
    entry.subscribers += 1;
    let settled = false;
    let onAbort;
    const finish = (callback) => (value) => {
      if (settled) return;
      settled = true;
      signal?.removeEventListener('abort', onAbort);
      entry.subscribers = Math.max(0, entry.subscribers - 1);
      callback(value);
    };
    return new Promise((resolve, reject) => {
      const resolveOnce = finish(resolve);
      const rejectOnce = finish(reject);
      onAbort = () => {
        if (settled) return;
        if (!entry.finished && entry.subscribers <= 1) entry.controller.abort();
        rejectOnce(this._abortError());
      };
      signal?.addEventListener('abort', onAbort, { once: true });
      entry.promise.then(resolveOnce, rejectOnce);
    });
  }

  async _request(path, { query = {}, cacheKey = null, signal = null, cache = false, method = 'GET', body = null } = {}) {
    const queryString = Object.entries(query)
      .filter(([, value]) => value !== undefined && value !== null && value !== '')
      .map(([key, value]) => `${encode(key)}=${encode(typeof value === 'object' ? JSON.stringify(value) : value)}`)
      .join('&');
    const url = `${this.baseUrl}${path}${queryString ? `?${queryString}` : ''}`;
    const normalizedMethod = String(method || 'GET').toUpperCase();
    const dedupeKey = cacheKey || url;
    // Only idempotent reads may share an in-flight response. A mutation body
    // is part of the operation: collapsing concurrent POSTs by endpoint would
    // make two distinct writes both receive the first write's result.
    const shareable = normalizedMethod === 'GET' || normalizedMethod === 'HEAD';
    const requestKey = shareable ? dedupeKey : Symbol(`${normalizedMethod}:${url}`);
    if (cache && shareable) {
      const cached = this._cacheGet(dedupeKey);
      if (cached !== undefined) return cached;
    }
    const existing = shareable ? this.inFlight.get(dedupeKey) : null;
    if (existing) {
      return this._subscribe(existing, signal);
    }
    const controller = new AbortController();
    if (signal?.aborted) throw this._abortError();
    const entry = { controller, promise: null, subscribers: 0, finished: false };
    const promise = (async () => {
      let response;
      try {
        response = await this.fetchImpl(url, {
          method: normalizedMethod,
          credentials: 'same-origin',
          signal: controller.signal,
          ...(body == null ? {} : { headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }),
        });
      } catch (error) {
        if (error?.name === 'AbortError') throw error;
        throw new FilesServiceError(error?.message || 'filesystem request failed');
      }
      let responseBody = null;
      try { responseBody = await response.json(); } catch { responseBody = null; }
      if (!response.ok) {
        const detail = responseBody?.detail;
        const code = responseBody?.code || (detail && typeof detail === 'object' ? detail.code : null) || (response.status === 403 ? 'denied' : response.status === 409 ? 'conflict' : 'root_unavailable');
        const message = (detail && typeof detail === 'object' ? detail.message : detail) || responseBody?.message || `filesystem request failed (${response.status})`;
        throw new FilesServiceError(message, { code, status: response.status });
      }
      if (cache && shareable) this._cachePut(dedupeKey, responseBody);
      return responseBody;
    })().finally(() => {
      entry.finished = true;
      this.inFlight.delete(requestKey);
      this.controllers.delete(requestKey);
    });
    entry.promise = promise;
    this.inFlight.set(requestKey, entry);
    this.controllers.set(requestKey, controller);
    return this._subscribe(entry, signal);
  }

  listDirectory(path, { cursor = null, sort = null, signal = null, cache = true, cacheKey = null } = {}) {
    return this._request('/browse', {
      query: { path, cursor, sort },
      cacheKey: cacheKey || `browse:${path}:${JSON.stringify(cursor)}:${JSON.stringify(sort)}`,
      signal,
      cache,
    });
  }

  readRange(path, { offset = 0, length = 65536, fingerprint = false, signal = null } = {}) {
    const cache = length <= 256 * 1024;
    return this._request('/read', { query: { path, offset, length, fingerprint }, cacheKey: `read:${path}:${offset}:${length}:${fingerprint}`, signal, cache });
  }

  readTextSnapshot(path, { signal = null } = {}) {
    return this._request('/read-text', {
      query: { path },
      cacheKey: `read-text:${path}`,
      signal,
      cache: false,
    });
  }

  readTextPreview(path, { signal = null } = {}) {
    return this._request('/preview-text', {
      query: { path },
      cacheKey: `preview-text:${path}`,
      signal,
      cache: false,
    });
  }

  mintPreviewHandle(path, kind, { signal = null } = {}) {
    const nonce = globalThis.crypto?.randomUUID?.() || `${Date.now()}:${Math.random()}`;
    return this._request('/preview-handles', {
      method: 'POST',
      body: { path, kind },
      // Handle creation is a capability mint, not an idempotent metadata
      // lookup. Never deduplicate two explicit preview opens.
      cacheKey: `preview-mint:${nonce}`,
      signal,
      cache: false,
    });
  }

  revokePreviewHandle(token, { signal = null } = {}) {
    const value = String(token || '').trim();
    if (!value) return Promise.resolve({ ok: true });
    return this._request(`/preview-handles/${encode(value)}`, {
      method: 'DELETE',
      cacheKey: `preview-revoke:${value}`,
      signal,
      cache: false,
    });
  }

  stat(path, { fingerprint = false, signal = null, cache = true, cacheKey = null } = {}) {
    return this._request('/stat', {
      query: { path, fingerprint },
      cacheKey: cacheKey || `stat:${path}:${fingerprint}`,
      signal,
      cache,
    });
  }

  search(path, query, { content = false, maxResults = 100, maxEntries = 10_000, maxDepth = 32, includeHidden = false, caseSensitive = false, signal = null } = {}) {
    return this._request('/search', {
      query: { path, query, content, max_results: maxResults, max_entries: maxEntries, max_depth: maxDepth, include_hidden: includeHidden, case_sensitive: caseSensitive },
      signal,
      cache: false,
    });
  }

  writeText(path, text, { expectedFingerprint = null, signal = null } = {}) {
    return this._request('/write', {
      method: 'POST',
      body: { path, text, ...(expectedFingerprint ? { expected_fingerprint: expectedFingerprint } : {}) },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  createFile(path, text = '', { signal = null } = {}) {
    return this._request('/create', {
      method: 'POST',
      body: { path, text },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  makeDirectory(path, { signal = null } = {}) {
    return this._request('/mkdir', {
      method: 'POST',
      body: { path },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  trash(path, { signal = null } = {}) {
    return this._request('/trash', {
      method: 'POST',
      body: { path },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  restore(entry, { signal = null } = {}) {
    return this._request('/restore', {
      method: 'POST',
      body: { entry },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  editText(path, oldText, newText, { expectedFingerprint = null, replaceAll = false, signal = null } = {}) {
    return this._request('/edit', {
      method: 'POST',
      body: { path, old: oldText, new: newText, replace_all: replaceAll, ...(expectedFingerprint ? { expected_fingerprint: expectedFingerprint } : {}) },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  copy(path, destination, { signal = null } = {}) {
    return this._request('/copy', {
      method: 'POST',
      body: { path, destination },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  move(path, destination, { signal = null } = {}) {
    return this._request('/move', {
      method: 'POST',
      body: { path, destination },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  rename(path, destination, { signal = null } = {}) {
    return this._request('/rename', {
      method: 'POST',
      body: { path, destination },
      signal,
      cache: false,
    }).then((result) => { this.clearCache(); return result; });
  }

  cancel(key) {
    const controller = this.controllers.get(key);
    if (controller) controller.abort();
  }

  clearCache() {
    this.cache.clear();
    this.cacheBytes = 0;
  }

  dispose() {
    for (const controller of this.controllers.values()) controller.abort();
    this.controllers.clear();
    this.inFlight.clear();
    this.clearCache();
  }
}

export const filesServiceClient = new FilesServiceClient();
