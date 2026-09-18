// Frozen nspell worker facade. Personal words are scoped to the Copal account,
// workspace, and dictionary locale so one user's vocabulary never leaks into
// another user's Editor session.
const PERSONAL_KEY = 'openclank-copal-personal-words-v2';
const SUPPORTED_LOCALES = new Set(['en', 'en-us', 'en-gb']);

function localeInfo(locale) {
  const requested = String(locale || 'en-US').trim().toLowerCase() || 'en-us';
  const supported = SUPPORTED_LOCALES.has(requested);
  return { requested, supported, dictionaryLocale:'en-US', locale:supported ? requested : 'en-us' };
}

function scopeKey(scope, locale) {
  const value = scope || {};
  const account = value.accountId || value.userId || value.storageNamespace || 'anonymous';
  const workspace = value.workspace || 'default';
  return `${PERSONAL_KEY}:${encodeURIComponent(String(account))}:${encodeURIComponent(String(workspace))}:${encodeURIComponent(locale)}`;
}

export function createSpellingService(workerUrl = '/static/js/copal/spelling-worker.js', options = {}) {
  if (window.openClankSpelling) return window.openClankSpelling;
  const worker = new Worker(workerUrl, { name:'copal-spelling' });
  const info = localeInfo(options.locale || navigator.language);
  const storageKey = scopeKey(options.scope, info.locale);
  let sequence = 0;
  const pending = new Map();
  let personal = [];
  try { personal = JSON.parse(localStorage.getItem(storageKey) || '[]'); } catch (_) { personal = []; }
  worker.addEventListener('message', (event) => {
    const { id, result, error } = event.data || {};
    const callback = pending.get(id);
    if (!callback) return;
    pending.delete(id);
    if (error) callback(Promise.reject(new Error(error)));
    else callback(result);
  });
  const ready = new Promise((resolve) => {
    pending.set(0, resolve);
    worker.postMessage({ id:0, method:'init', personal });
  });
  const call = (method, args = []) => new Promise((resolve, reject) => {
    const id = ++sequence;
    pending.set(id, (value) => value instanceof Promise ? value.catch(reject) : resolve(value));
    worker.postMessage({ id, method, args });
  });
  const savePersonal = (words) => {
    personal = [...new Set(words.map((word) => String(word).trim().toLocaleLowerCase()).filter(Boolean))].sort();
    try { localStorage.setItem(storageKey, JSON.stringify(personal)); } catch (_) {}
  };
  const service = {
    ready,
    check: (word) => call('check', [word]),
    suggest: (word) => call('suggest', [word]),
    add: async (words) => { const result = await call('add', [words]); savePersonal(result?.personal || personal.concat(words)); return result || { personal }; },
    remove: async (words) => { const result = await call('remove', [words]); savePersonal(result?.personal || personal.filter((word) => !words.includes(word))); return result || { personal }; },
    personal: () => [...personal],
    locale: () => ({ ...info }),
    storageKey: () => storageKey,
    scopeKey: () => storageKey,
    destroy: () => { for (const callback of pending.values()) callback(null); pending.clear(); worker.terminate(); delete window.openClankSpelling; },
  };
  window.openClankSpelling = Object.freeze(service);
  return service;
}
