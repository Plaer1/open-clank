// All descriptors and original bytes resolve to immutable packaged artwork.
const MAX_DECODED_PIXELS = 16 * 1024 * 1024;
let catalogPromise = null;
let catalogIndexPromise = null;
const loaded = new Map();
const pending = new Map();
let decodedPixels = 0;
let activeLoads = 0;
const loadWaiters = [];
let catalogState = 'loading';

async function fetchLocal(url, json = false) {
  let response;
  try {
    response = await fetch(url, { credentials: 'same-origin' });
  } catch (cause) {
    const error = new Error('Local emoji catalogue request was interrupted', { cause });
    error.transient = true;
    error.connectionInterrupted = true;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(`Local emoji catalogue request failed (${response.status})`);
    error.transient = response.status === 502 || response.status === 504;
    error.authenticationRequired = response.status === 401 || response.status === 403;
    throw error;
  }
  return json ? response.json() : response.text();
}

export function getThemeEmojiCatalog() {
  if (!catalogPromise) {
    catalogState = 'loading';
    catalogPromise = Promise.all([
      fetchLocal('/api/theme-emoji/google/catalog', true),
      fetchLocal('/api/theme-emoji/kitchen/catalog'),
    ]).then(([google, text]) => {
      const kitchen = text.split(/\r?\n/).filter(Boolean);
      if (!kitchen.length || !(google.entries?.length > 0)) throw new Error('Packaged emoji catalogue is incomplete');
      const entries = google.entries.map(entry => ({ id: entry.id, kind: 'google',
        src: `/api/theme-emoji/google/${encodeURIComponent(entry.id)}` }));
      for (const id of kitchen) entries.push({ id, kind: 'kitchen', src: `/api/theme-emoji/kitchen/${encodeURIComponent(id)}` });
      catalogState = 'ready';
      return entries;
    }).catch(error => {
      catalogPromise = null;
      catalogState = error.authenticationRequired ? 'authentication-required'
        : error.transient ? 'interrupted' : 'unavailable';
      throw error;
    });
  }
  return catalogPromise;
}

export function getThemeEmojiCatalogStatus() {
  return { kitchen: catalogState, local: true };
}

function acquireLoadSlot(signal) {
  if (signal?.aborted) return Promise.resolve(false);
  if (activeLoads < 6) { activeLoads++; return Promise.resolve(true); }
  return new Promise(resolve => loadWaiters.push({ resolve, signal }));
}

function releaseLoadSlot() {
  while (loadWaiters.length) {
    const next = loadWaiters.shift();
    if (next.signal?.aborted) { next.resolve(false); continue; }
    next.resolve(true);
    return;
  }
  activeLoads = Math.max(0, activeLoads - 1);
}

function getCatalogIndex() {
  if (!catalogIndexPromise) catalogIndexPromise = getThemeEmojiCatalog()
    .then(entries => new Map(entries.map(entry => [entry.id, entry])))
    .catch(error => { catalogIndexPromise = null; throw error; });
  return catalogIndexPromise;
}

function trimDecodedCache() {
  while (decodedPixels > MAX_DECODED_PIXELS && loaded.size > 1) {
    const id = loaded.keys().next().value;
    const asset = loaded.get(id);
    loaded.delete(id);
    decodedPixels = Math.max(0, decodedPixels - asset.width * asset.height);
  }
}

function rasterMatches(asset, maxDimension) {
  return asset && (asset.rasterMaxDimension || 0) === maxDimension;
}

function loadImage(entry, maxDimension, signal) {
  const cached = getThemeEmojiAsset(entry.id, { maxDimension });
  if (cached) return Promise.resolve(cached);
  const key = `${entry.id}:${maxDimension}`;
  const existing = pending.get(key);
  if (existing && !existing.signal?.aborted) return existing.promise;
  const task = { signal, promise: null };
  task.promise = (async () => {
    const acquired = await acquireLoadSlot(signal);
    if (!acquired) return null;
    const image = new Image();
    let rejectAbort;
    const aborted = new Promise((_, reject) => { rejectAbort = reject; });
    const abort = () => {
      image.src = '';
      rejectAbort(new DOMException('Emoji scene disposed', 'AbortError'));
    };
    signal?.addEventListener('abort', abort, { once: true });
    try {
      if (signal?.aborted) return null;
      image.decoding = 'async';
      image.src = entry.src;
      // A cancelled decoder is not required to settle promptly in every
      // browser. Scene abort must independently release the global slot.
      await Promise.race([image.decode(), aborted]);
      if (signal?.aborted) return null;
      const width = image.naturalWidth;
      const height = image.naturalHeight;
      if (!width || !height) throw new Error('Packaged emoji image has empty bounds');
      let drawable = image;
      let rasterWidth = width;
      let rasterHeight = height;
      if (maxDimension) {
        const scale = Math.min(1, maxDimension / Math.max(width, height));
        rasterWidth = Math.max(1, Math.round(width * scale));
        rasterHeight = Math.max(1, Math.round(height * scale));
        drawable = document.createElement('canvas');
        drawable.width = rasterWidth;
        drawable.height = rasterHeight;
        const ctx = drawable.getContext('2d');
        if (!ctx) throw new Error('Emoji raster context unavailable');
        ctx.drawImage(image, 0, 0, rasterWidth, rasterHeight);
        image.src = '';
      }
      const asset = { id: entry.id, kind: entry.kind, image: drawable, width: rasterWidth,
        height: rasterHeight, rasterMaxDimension: maxDimension };
      const previous = loaded.get(entry.id);
      if (previous) decodedPixels -= previous.width * previous.height;
      loaded.set(entry.id, asset);
      decodedPixels += rasterWidth * rasterHeight;
      trimDecodedCache();
      return asset;
    } catch (error) {
      image.src = '';
      if (!signal?.aborted) console.error('Packaged emoji image integrity failure', entry.id, error);
      return null;
    } finally {
      signal?.removeEventListener('abort', abort);
      releaseLoadSlot();
    }
  })().finally(() => { if (pending.get(key) === task) pending.delete(key); });
  pending.set(key, task);
  return task.promise;
}

export async function preloadThemeEmojiAssets(ids, { concurrency = 6, maxDimension = 0, signal, onAsset } = {}) {
  maxDimension = Math.max(0, Math.min(512, Math.floor(Number(maxDimension) || 0)));
  const byId = await getCatalogIndex();
  const results = new Array(ids.length).fill(null);
  const queue = ids.map((id, index) => ({ id, index })).filter(item => byId.has(item.id));
  let cursor = 0;
  const workers = Math.max(1, Math.min(6, Math.floor(Number(concurrency) || 6), queue.length || 1));
  await Promise.all(Array.from({ length: workers }, async () => {
    while (!signal?.aborted && cursor < queue.length) {
      const item = queue[cursor++];
      const asset = await loadImage(byId.get(item.id), maxDimension, signal);
      results[item.index] = asset;
      if (!signal?.aborted && typeof onAsset === 'function') onAsset(asset, item.id, item.index);
    }
  }));
  return results;
}

export function getThemeEmojiAsset(id, { maxDimension = 0 } = {}) {
  const asset = loaded.get(id);
  if (!rasterMatches(asset, maxDimension)) return null;
  loaded.delete(id);
  loaded.set(id, asset);
  return asset;
}
