// Bounded, keyed native-thumbnail loader for Files.
//
// The loader is intentionally presentation-only: authorization, identity,
// policy generation, and Quick Look representation selection remain server/
// Rust responsibilities.  This module owns only browser queue/cache/lifecycle
// behavior and can therefore be tested without a live filesystem.

const RETRY_DELAYS = Object.freeze([150, 500]);
const TRANSIENT_STATUSES = new Set([429, 503, 504]);
const AUTHORITY_STATUSES = new Set([401, 403, 409, 410]);

function defaultUrlApi() {
  return typeof URL !== 'undefined' ? URL : null;
}

export function createThumbnailLoader({
  fetchImpl = (...args) => fetch(...args),
  maxConcurrent = 4,
  maxQueue = 64,
  maxEntries = 64,
  maxBytes = 32 * 1024 * 1024,
  ttlMs = 5 * 60 * 1000,
  now = () => Date.now(),
  setTimer = (fn, delay) => setTimeout(fn, delay),
  clearTimer = (timer) => clearTimeout(timer),
  urlApi = defaultUrlApi(),
  onAuthorityError = () => {},
} = {}) {
  const cache = new Map();
  const jobs = new Map();
  const queue = [];
  const subscribers = new Map();
  let active = 0;
  let destroyed = false;
  let observer = null;
  let observerRoot = undefined;

  const touch = (key, record) => {
    record.lastUsed = now();
    cache.delete(key);
    cache.set(key, record);
  };

  const revoke = (record) => {
    if (record?.objectUrl && urlApi?.revokeObjectURL) {
      try { urlApi.revokeObjectURL(record.objectUrl); } catch (_) {}
      record.objectUrl = '';
    }
  };

  const evict = () => {
    const cutoff = now() - ttlMs;
    for (const [key, record] of cache) {
      if (record.expires <= now() || record.lastUsed < cutoff) {
        revoke(record);
        cache.delete(key);
      }
    }
    let bytes = [...cache.values()].reduce((sum, item) => sum + (item.bytes || 0), 0);
    while (cache.size > maxEntries || bytes > maxBytes) {
      const oldest = cache.entries().next().value;
      if (!oldest) break;
      const [key, record] = oldest;
      bytes -= record.bytes || 0;
      revoke(record);
      cache.delete(key);
    }
  };

  const fallback = (job) => {
    for (const subscriber of job.subscribers) {
      if (!subscriber.img || subscriber.img.isConnected === false) continue;
      try { subscriber.renderFallback?.(); } catch (_) {}
      subscriber.img.dataset.nativeThumbnailState = 'fallback';
    }
  };

  const paint = (job, record) => {
    for (const subscriber of job.subscribers) {
      if (!subscriber.img || subscriber.img.isConnected === false) continue;
      try {
        subscriber.img.src = record.objectUrl;
        subscriber.img.dataset.nativeThumbnailState = 'loaded';
      } catch (_) {}
    }
  };

  const purgeAuthority = () => {
    for (const record of cache.values()) revoke(record);
    cache.clear();
    onAuthorityError();
  };

  const finishJob = (job) => {
    jobs.delete(job.key);
    active = Math.max(0, active - 1);
    pump();
  };

  const schedule = (job, delay) => {
    job.timer = setTimer(() => {
      job.timer = null;
      if (!destroyed && !job.controller?.signal.aborted) {
        queue.push(job);
        pump();
      }
    }, delay);
  };

  const run = async (job) => {
    active += 1;
    job.attempts += 1;
    job.controller = new AbortController();
    for (const subscriber of job.subscribers) {
      if (subscriber.img) subscriber.img.dataset.nativeThumbnailState = 'loading';
    }
    try {
      const response = await fetchImpl(job.url, {
        credentials: 'same-origin',
        signal: job.controller.signal,
        headers: { Accept: 'image/png' },
      });
      if (!response.ok) {
        if (AUTHORITY_STATUSES.has(response.status)) {
          purgeAuthority();
          fallback(job);
          finishJob(job);
          return;
        }
        if (TRANSIENT_STATUSES.has(response.status) && job.attempts <= RETRY_DELAYS.length + 1) {
          active = Math.max(0, active - 1);
          schedule(job, RETRY_DELAYS[job.attempts - 1]);
          return;
        }
        // 400/404 means this identity is not natively previewable.  Cache the
        // negative result briefly; infrastructure failures never become it.
        if (response.status === 400 || response.status === 404) {
          cache.set(job.key, { negative: true, expires: now() + ttlMs, lastUsed: now(), bytes: 0 });
        }
        fallback(job);
        finishJob(job);
        return;
      }
      const blob = await response.blob();
      if (blob.size > 4 * 1024 * 1024) throw Object.assign(new Error('thumbnail too large'), { permanent: true });
      if (job.controller.signal.aborted) return;
      const objectUrl = urlApi?.createObjectURL ? urlApi.createObjectURL(blob) : job.url;
      const record = { objectUrl, bytes: blob.size, expires: now() + ttlMs, lastUsed: now() };
      evict();
      const prior = cache.get(job.key);
      if (prior) revoke(prior);
      cache.set(job.key, record);
      evict();
      paint(job, record);
      finishJob(job);
    } catch (error) {
      if (error?.name === 'AbortError' || job.controller?.signal.aborted) {
        finishJob(job);
        return;
      }
      if (!error?.permanent && job.attempts <= RETRY_DELAYS.length) {
        active = Math.max(0, active - 1);
        schedule(job, RETRY_DELAYS[job.attempts - 1]);
        return;
      }
      fallback(job);
      finishJob(job);
    }
  };

  function pump() {
    if (destroyed) return;
    evict();
    while (active < maxConcurrent && queue.length) {
      const job = queue.shift();
      if (!job || job.controller?.signal.aborted || !jobs.has(job.key)) continue;
      void run(job);
    }
  }

  const enqueue = (job) => {
    if (queue.length >= maxQueue) {
      fallback(job);
      jobs.delete(job.key);
      return;
    }
    queue.push(job);
    pump();
  };

  const attach = (img, { key, url, renderFallback, root } = {}) => {
    if (destroyed || !img || !key || !url) return () => {};
    const old = img.__openClankThumbnailDetach;
    old?.();
    const subscriber = { img, renderFallback };
    const record = cache.get(key);
    if (record && record.expires > now()) {
      touch(key, record);
      if (record.negative) {
        renderFallback?.();
        img.dataset.nativeThumbnailState = 'fallback';
      } else {
        paint({ subscribers: new Set([subscriber]) }, record);
      }
      return () => {};
    }
    if (record) {
      revoke(record);
      cache.delete(key);
    }
    let job = jobs.get(key);
    if (!job) {
      job = { key, url, attempts: 0, subscribers: new Set(), controller: null, timer: null };
      jobs.set(key, job);
      if (!observer) enqueue(job);
    }
    job.subscribers.add(subscriber);
    img.dataset.nativeThumbnailState = 'idle';
    const detach = () => {
      job.subscribers.delete(subscriber);
      if (observer) observer.unobserve(img);
      if (!job.subscribers.size && job.controller && !job.controller.signal.aborted) {
        // Viewport aborts are neutral: the keyed job/cache may be reused by a
        // later render, and do not consume a retry attempt.
        job.controller.abort();
      }
    };
    img.__openClankThumbnailKey = key;
    img.__openClankThumbnailDetach = detach;
    if (observer) observer.observe(img);
    if (root && observer?.root !== root) {
      // A root change is handled by the next hard observer setup.  The job is
      // keyed and survives this presentation change.
    }
    return detach;
  };

  const setRoot = (root) => {
    if (observer && observerRoot === (root || null)) return;
    if (observer) observer.disconnect();
    observerRoot = root || null;
    if (typeof IntersectionObserver !== 'function') {
      observer = null;
      return;
    }
    observer = new IntersectionObserver((records) => {
      for (const record of records) {
        const img = record.target;
        if (!record.isIntersecting) continue;
        const job = jobs.get(img.__openClankThumbnailKey);
        if (job && !job.controller && !queue.includes(job)) queue.push(job);
      }
      pump();
    }, { root: root || null, rootMargin: '160px' });
  };

  const detachAll = () => {
    observer?.disconnect?.();
    for (const job of jobs.values()) {
      for (const subscriber of job.subscribers) delete subscriber.img.__openClankThumbnailDetach;
      job.subscribers.clear();
    }
  };

  const clear = () => {
    if (destroyed) return;
    destroyed = true;
    observer?.disconnect?.();
    for (const job of jobs.values()) {
      if (job.timer) clearTimer(job.timer);
      job.controller?.abort();
    }
    jobs.clear();
    queue.length = 0;
    for (const record of cache.values()) revoke(record);
    cache.clear();
  };

  return {
    attach,
    setRoot,
    detachAll,
    clear,
    stats: () => ({ active, queued: queue.length, cacheEntries: cache.size, jobs: jobs.size }),
  };
}

export default createThumbnailLoader;
