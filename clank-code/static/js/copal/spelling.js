// Local nspell facade. Creating the account/workspace service does not start a
// worker: the first operation (or explicitly reading ready) owns initialization.
const PERSONAL_KEY = 'openclank-copal-personal-words-v2';
const LIMITS = Object.freeze({ batch:256, queue:64, cache:2048, suggestions:128, personal:5000, wordBytes:64, suggestBytes:24 });
const encoder = new TextEncoder();

function localeInfo(locale) {
  const requested = String(locale || 'en-US').trim().toLowerCase() || 'en-us';
  const supported = requested === 'en' || requested === 'en-us';
  // Retain the old en-GB storage namespace so correcting the dictionary label
  // does not orphan that workspace's existing personal words.
  return { requested, supported, dictionaryLocale:'en-US', locale:requested === 'en-gb' ? requested : supported ? requested : 'en-us', fallback:!supported };
}
function scopeKey(scope, locale) {
  const value = scope || {};
  const account = value.accountId || value.userId || value.storageNamespace || 'anonymous';
  return `${PERSONAL_KEY}:${encodeURIComponent(String(account))}:${encodeURIComponent(String(value.workspace || 'default'))}:${encodeURIComponent(locale)}`;
}
function wordValue(value) {
  const raw = String(value ?? '');
  if (raw.length > LIMITS.wordBytes) throw new Error('Spelling words are limited to 64 UTF-8 bytes.');
  const word = raw.trim().normalize('NFC').replace(/’/g, "'");
  if (encoder.encode(word).length > LIMITS.wordBytes || !/^[\p{L}\p{M}]+(?:['-][\p{L}\p{M}]+)*$/u.test(word)) throw new Error('Select a word containing letters to check spelling.');
  return word;
}
function personalWords(words) {
  const unique = new Map();
  for (const raw of Array.isArray(words) ? words.slice(0, LIMITS.personal) : []) {
    try { const word = wordValue(raw); unique.set(word.toLowerCase(), word); } catch (_) {}
  }
  return [...unique.values()].sort();
}
function remember(cache, key, value, limit) {
  cache.delete(key); cache.set(key, value);
  if (cache.size > limit) cache.delete(cache.keys().next().value);
}

export function createSpellingService(workerUrl = '/static/js/copal/spelling-worker.js', options = {}) {
  const info = localeInfo(options.locale || navigator.language);
  const storageKey = scopeKey(options.scope, info.locale);
  const existing = window.openClankSpelling;
  if (existing?.scopeKey?.() === storageKey && existing.locale?.().requested === info.requested) return existing;
  existing?.destroy?.();
  let personal = [];
  try { personal = personalWords(JSON.parse(localStorage.getItem(storageKey) || '[]')); } catch (_) {}
  let worker = null, readiness = null, initJob = null, active = null;
  let failure = null, destroyed = false, sequence = 0, revision = 0;
  const queue = [], checks = new Map(), suggestions = new Map(), inflight = new Map();
  function fail(error) {
    failure = error instanceof Error ? error : new Error(String(error));
    worker?.terminate(); worker = null;
    for (const job of [initJob, active, ...queue]) if (job) { clearTimeout(job.timer); job.reject(failure); }
    initJob = active = null; queue.length = 0; inflight.clear();
  }
  function send(job, timeout) {
    job.timer = setTimeout(() => fail(new Error('Offline spelling timed out. Retry spelling to start a fresh worker.')), timeout);
    try { worker.postMessage({ id:job.id, method:job.method, args:job.args, ...(job.method === 'init' ? { personal } : {}) }); }
    catch (error) { fail(error); }
  }
  function start() {
    if (destroyed) return Promise.reject(new Error('Spelling service was closed.'));
    if (failure) return Promise.reject(failure);
    if (readiness) return readiness;
    readiness = new Promise((resolve, reject) => { initJob = { id:++sequence, method:'init', args:[], resolve, reject }; });
    // An owner can inspect status without awaiting ready; do not leak a rejected
    // initialization promise when the worker reports failure between operations.
    readiness.catch(() => {});
    try {
      const owner = worker = new Worker(workerUrl, { name:'copal-spelling' });
      worker.addEventListener('error', event => { if (worker !== owner) return; event.preventDefault?.(); fail(new Error('Offline spelling worker could not start.')); });
      worker.addEventListener('messageerror', () => { if (worker === owner) fail(new Error('Offline spelling returned an unreadable result.')); });
      worker.addEventListener('message', event => {
        if (worker !== owner) return;
        const data = event.data || {}, job = initJob?.id === data.id ? initJob : active?.id === data.id ? active : null;
        if (!job) return;
        clearTimeout(job.timer);
        if (job === initJob) {
          initJob = null;
          if (data.error || data.result?.ready !== true) { job.reject(new Error(data.error || 'Offline spelling did not become ready.')); fail(new Error(data.error || 'Offline spelling did not become ready.')); return; }
        } else active = null;
        if (data.error) job.reject(new Error(data.error)); else job.resolve(data.result);
        pump();
      });
      send(initJob, 5000);
    } catch (error) { fail(error); }
    return readiness;
  }
  function pump() {
    if (!worker || initJob || active || failure || destroyed) return;
    active = queue.shift() || null;
    if (active) send(active, 1500);
  }
  function call(method, args = []) {
    if (destroyed || failure) return Promise.reject(failure || new Error('Spelling service was closed.'));
    if (queue.length >= LIMITS.queue) return Promise.reject(new Error('Spelling is busy. Check a smaller selection.'));
    const promise = new Promise((resolve, reject) => { queue.push({ id:++sequence, method, args, resolve, reject }); });
    const initializing = start();
    void initializing.then(pump).catch(error => { if (readiness === initializing) fail(error); });
    return promise;
  }
  function checkBatch(values) {
    if (destroyed || failure) return Promise.reject(failure || new Error('Spelling service was closed.'));
    if (!Array.isArray(values) || values.length > LIMITS.batch) return Promise.reject(new Error('Check at most 256 words per spelling batch.'));
    let words;
    try { words = values.map(wordValue); } catch (error) { return Promise.reject(error); }
    const currentRevision = revision, missing = [...new Set(words)].filter(word => !checks.has(word) && !inflight.has(`check:${word}:${currentRevision}`));
    if (missing.length) {
      const batch = call('checkBatch', [missing]).then(result => {
        if (!Array.isArray(result) || result.length !== missing.length || result.some(value => typeof value !== 'boolean')) throw new Error('Offline spelling returned an invalid batch.');
        return result;
      });
      missing.forEach((word, index) => {
        const key = `check:${word}:${currentRevision}`;
        const result = batch.then(values => { if (revision === currentRevision) remember(checks, word, values[index], LIMITS.cache); return values[index]; });
        inflight.set(key, result);
        void result.finally(() => { if (inflight.get(key) === result) inflight.delete(key); }).catch(() => {});
      });
    }
    return Promise.all(words.map(word => checks.has(word) ? checks.get(word) : inflight.get(`check:${word}:${currentRevision}`)));
  }
  function suggest(value) {
    if (destroyed || failure) return Promise.reject(failure || new Error('Spelling service was closed.'));
    let word;
    try { word = wordValue(value); } catch (error) { return Promise.reject(error); }
    if (encoder.encode(word).length > LIMITS.suggestBytes) return Promise.resolve([]);
    if (suggestions.has(word)) return Promise.resolve([...suggestions.get(word)]);
    const currentRevision = revision, key = `suggest:${word}:${currentRevision}`;
    if (inflight.has(key)) return inflight.get(key);
    const result = checkBatch([word]).then(([correct]) => correct ? [] : call('suggest', [word])).then(values => {
      const result = [...new Set(Array.isArray(values) ? values.map(String) : [])].slice(0, 5);
      if (revision === currentRevision) remember(suggestions, word, result, LIMITS.suggestions);
      return result;
    });
    inflight.set(key, result);
    void result.finally(() => { if (inflight.get(key) === result) inflight.delete(key); }).catch(() => {});
    return result;
  }
  function mutate(method, values) {
    if (!Array.isArray(values) || values.length > LIMITS.batch) return Promise.reject(new Error('Change at most 256 personal words at a time.'));
    let words;
    try { words = values.map(wordValue); } catch (error) { return Promise.reject(error); }
    revision += 1; checks.clear(); suggestions.clear();
    return call(method, [words]).then(result => {
      personal = personalWords(result?.personal);
      try { localStorage.setItem(storageKey, JSON.stringify(personal)); } catch (_) {}
      // A check invoked during the mutation may have seen the prior overlay.
      revision += 1; checks.clear(); suggestions.clear();
      return { ...result, personal:[...personal] };
    });
  }
  const service = {
    get ready() { return start(); },
    check: word => checkBatch([word]).then(([correct]) => correct), checkBatch, suggest,
    add: words => mutate('add', words), remove: words => mutate('remove', words),
    personal: () => [...personal], locale: () => ({ ...info }), storageKey: () => storageKey, scopeKey: () => storageKey,
    revision: () => revision,
    status: () => ({ state:destroyed ? 'closed' : failure ? 'error' : initJob ? 'loading' : worker ? 'ready' : 'idle', message:failure?.message || '', dictionaryLocale:'en-US' }),
    retry: () => { if (destroyed) return Promise.reject(new Error('Spelling service was closed.')); if (!failure) return start(); failure = null; readiness = null; return start(); },
    destroy: () => { destroyed = true; fail(new Error('Spelling service was closed.')); if (window.openClankSpelling === service) delete window.openClankSpelling; },
  };
  window.openClankSpelling = Object.freeze(service);
  return service;
}
