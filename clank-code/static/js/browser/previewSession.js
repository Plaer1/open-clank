const API_ROOT = '/api/browser-previews';
const REQUEST_TIMEOUT_MS = 8_000;
const CLEANUP_TIMEOUT_MS = 2_000;
const STATUS_TIMEOUT_MS = 4_000;

function responseError(response, payload) {
  const detail = payload?.detail;
  const message = detail?.message || payload?.message || `Browser preview request failed (${response.status}).`;
  const error = new Error(String(message));
  error.status = response.status;
  error.code = String(detail?.code || payload?.code || 'preview_unavailable');
  return error;
}

function randomClientId() {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
}

async function fetchBounded(url, options = {}, timeoutMs = REQUEST_TIMEOUT_MS, consume = response => response) {
  const controller = new AbortController();
  const externalSignal = options.signal || null;
  const abortForCaller = () => controller.abort();
  if (externalSignal?.aborted) controller.abort();
  else externalSignal?.addEventListener('abort', abortForCaller, { once: true });
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    const result = await consume(response);
    if (timedOut) {
      const error = new Error('Browser preview request timed out.');
      error.code = 'request_timeout';
      throw error;
    }
    return result;
  } catch (cause) {
    if (timedOut && !externalSignal?.aborted) {
      const error = new Error('Browser preview request timed out.');
      error.code = 'request_timeout';
      error.cause = cause;
      throw error;
    }
    throw cause;
  } finally {
    clearTimeout(timer);
    externalSignal?.removeEventListener('abort', abortForCaller);
  }
}

async function fetchJsonBounded(url, options, timeoutMs) {
  return fetchBounded(url, options, timeoutMs, async response => ({
    response,
    payload: await response.json().catch(error => {
      if (error?.name === 'AbortError') throw error;
      return {};
    }),
  }));
}

export function createPreviewSession({
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
  cleanupTimeoutMs = CLEANUP_TIMEOUT_MS,
  statusTimeoutMs = STATUS_TIMEOUT_MS,
} = {}) {
  let generation = 0;
  let pending = null;
  let current = null;
  const clientId = randomClientId();

  async function revoke(sessionId) {
    if (!sessionId) return false;
    try {
      return await fetchBounded(`${API_ROOT}/${encodeURIComponent(sessionId)}`, {
        method: 'DELETE', credentials: 'same-origin', cache: 'no-store',
      }, cleanupTimeoutMs, async response => {
        await response.body?.cancel().catch(() => {});
        return response.ok;
      });
    } catch {
      return false;
    }
  }

  async function cancelServerGeneration(value) {
    try {
      const { response, payload } = await fetchJsonBounded(`${API_ROOT}/cancel`, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ client_id: clientId, client_generation: value }),
      }, cleanupTimeoutMs);
      return response.ok && payload.cancelled === true;
    } catch {
      return false;
    }
  }

  async function create({ resourceRef, revision = null, dependencies = [], dependencyWarnings = [], isCurrent = () => true }) {
    const operation = ++generation;
    const prior = current;
    current = null;
    const priorRevoked = prior ? await revoke(prior.session_id) : true;
    pending = { operation };
    let created = null;
    try {
      const { response, payload } = await fetchJsonBounded(API_ROOT, {
        method: 'POST', credentials: 'same-origin', cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          resource_ref: String(resourceRef || ''),
          ...(revision && typeof revision === 'object' ? { expected_revision: revision } : {}),
          ...(Array.isArray(dependencies) && dependencies.length ? { dependencies } : {}),
          dependency_warnings: (Array.isArray(dependencyWarnings) ? dependencyWarnings : []).slice(0,16).map(value => Array.from(String(value).replace(/[\u0000-\u001f\u007f]/g, ' ')).slice(0,256).join('')),
          client_id: clientId,
          client_generation: operation,
        }),
      }, requestTimeoutMs);
      if (!response.ok) throw responseError(response, payload);
      created = payload;
      if (operation !== generation || !isCurrent()) {
        const revoked = await revoke(created.session_id);
        return { stale: true, session: null, cleanupConfirmed: revoked };
      }
      current = created;
      return { stale: false, session: created, priorCleanupConfirmed: priorRevoked };
    } catch (error) {
      let cancellationConfirmed = false;
      if (operation === generation) cancellationConfirmed = await cancelServerGeneration(++generation);
      else cancellationConfirmed = await cancelServerGeneration(operation);
      let revokeConfirmed = false;
      if (created?.session_id && (operation !== generation || !isCurrent())) {
        revokeConfirmed = await revoke(created.session_id);
      }
      error.cleanupConfirmed = cancellationConfirmed || revokeConfirmed;
      error.cleanupUnknown = !error.cleanupConfirmed;
      throw error;
    } finally {
      if (pending?.operation === operation) pending = null;
    }
  }

  async function status(sessionId, { signal } = {}) {
    const { response, payload } = await fetchJsonBounded(`${API_ROOT}/${encodeURIComponent(sessionId)}/status`, {
      credentials: 'same-origin', cache: 'no-store', signal,
    }, statusTimeoutMs);
    if (!response.ok) throw responseError(response, payload);
    return payload;
  }

  async function exportSourceBundle(sessionId, { signal } = {}) {
    return fetchBounded(`${API_ROOT}/${encodeURIComponent(sessionId)}/export-source-bundle`, {
      method:'POST', credentials:'same-origin', cache:'no-store', signal,
    }, requestTimeoutMs, async response => {
      if (!response.ok) throw responseError(response, await response.json().catch(() => ({})));
      if (response.headers.get('X-OpenClank-Bundle') !== 'declared_source_bundle'
          || response.headers.get('X-OpenClank-Coverage') !== 'partial_static') throw new Error('Unexpected source bundle response.');
      const maxBytes = 24 * 1024 * 1024, chunks = []; let size = 0;
      const reader = response.body?.getReader();
      if (!reader) throw new Error('Source bundle stream is unavailable.');
      try {
        for (;;) {
          const {done,value} = await reader.read(); if (done) break;
          size += value.byteLength;
          if (size > maxBytes) { await reader.cancel(); throw new Error('Source bundle exceeds the download limit.'); }
          chunks.push(value);
        }
      } finally { reader.releaseLock(); }
      return new Blob(chunks,{type:'application/zip'});
    });
  }

  async function close() {
    const closeGeneration = ++generation;
    pending = null;
    const prior = current;
    current = null;
    const revoked = prior ? await revoke(prior.session_id) : true;
    const cancelled = await cancelServerGeneration(closeGeneration);
    return { cleanupConfirmed: revoked && cancelled, revoked, cancelled };
  }

  async function cancelPending() {
    generation += 1;
    pending = null;
    const cancelled = await cancelServerGeneration(generation);
    return { cleanupConfirmed: cancelled, cancelled };
  }

  return Object.freeze({ create, status, exportSourceBundle, close, cancelPending, get current() { return current; } });
}
