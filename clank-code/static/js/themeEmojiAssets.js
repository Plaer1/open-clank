// Descriptors resolve to verified packaged or consent-scoped local artwork.
const MAX_DECODED_PIXELS = 16 * 1024 * 1024;
const SVG_MEDIA_TYPE = 'image/svg+xml';
const SUPPORTED_IMAGE_TYPES = new Set(['image/png', 'image/webp', SVG_MEDIA_TYPE]);
let catalogPromise = null;
let catalogIndexPromise = null;
const loaded = new Map();
const pending = new Map();
let decodedPixels = 0;
let activeLoads = 0;
const loadWaiters = [];
let catalogState = 'loading';
let cacheEpoch = 0;
let catalogueGeneration = null;

export function invalidateThemeEmojiAssets(generation = null) {
  cacheEpoch++;
  catalogueGeneration = generation;
  catalogPromise = null;
  catalogIndexPromise = null;
  loaded.clear();
  pending.clear();
  decodedPixels = 0;
  catalogState = 'loading';
}


async function localStateRequest(path, body = null) {
  const response = await fetch(`/api/theme-emoji/local/${path}`, {
    credentials:'same-origin', cache:'no-store',
    ...(body === null ? {} : { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body) }),
  });
  if (!response.ok) throw new Error('Local emoji theme state is unavailable');
  const value = await response.json();
  if (!value || typeof value !== 'object' || Array.isArray(value)
      || !['unknown','accepted','declined','revoked'].includes(value.consent)
      || !Number.isSafeInteger(value.consent_revision) || value.consent_revision < 0
      || !Number.isSafeInteger(value.generation) || value.generation < 0
      || typeof value.ready !== 'boolean' || (value.ready && value.consent !== 'accepted')
      || typeof value.acquisition_available !== 'boolean'
      || (value.catalogue_generation != null && (!Number.isSafeInteger(value.catalogue_generation) || value.catalogue_generation < 0))
      || (value.selection_id != null && !/^[a-f0-9]{32}$/.test(value.selection_id))
      || (value.prompt_required != null && typeof value.prompt_required !== 'boolean')
      || (path === 'selection' && (!value.selection_id || typeof value.prompt_required !== 'boolean'))) {
    throw new Error('Local emoji theme state is invalid');
  }
  return value;
}

export const readThemeEmojiLocalState = (accountId, { includeInventory = true } = {}) => localStateRequest(`status?expected_account_id=${encodeURIComponent(accountId)}&include_inventory=${includeInventory}`);
export const beginThemeEmojiSelection = (accountId, pattern) => localStateRequest('selection', {pattern, expected_account_id:accountId});
export const decideThemeEmojiSelection = (accountId, selectionId, revision, accepted) => localStateRequest('decision', {
  selection_id:selectionId, expected_revision:revision, accepted, expected_account_id:accountId,
});
export const revokeThemeEmojiConsent = (accountId, revision) => localStateRequest('revoke', {expected_revision:revision, expected_account_id:accountId});
export const cancelThemeEmojiUpdate = (accountId, revision) => localStateRequest('cancel', {expected_revision:revision, expected_account_id:accountId});
export const setThemeEmojiUpdates = (accountId, revision, enabled) => localStateRequest('updates', {expected_revision:revision, expected_account_id:accountId, enabled});
export async function deleteThemeEmojiArtwork(accountId, revision) {
  const response = await fetch('/api/theme-emoji/local/delete', { credentials:'same-origin', cache:'no-store', method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({expected_account_id:accountId, expected_revision:revision}) });
  if (!response.ok) throw new Error('Artwork deletion is unavailable');
  const result = await response.json();
  if (!result || !['removed','absent'].includes(result.outcome) || result.diskReclamation !== 'unconfirmed') throw new Error('Artwork deletion is unconfirmed');
  return result;
}

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
    const epoch = cacheEpoch;
    const suffix = catalogueGeneration == null ? '' : `?generation=${encodeURIComponent(catalogueGeneration)}`;
    const request = Promise.allSettled([
      fetchLocal('/api/theme-emoji/google/catalog', true),
      fetchLocal('/api/theme-emoji/kitchen/catalog'),
    ]).then(([googleResult, kitchenResult]) => {
      if (epoch !== cacheEpoch) throw new DOMException('Emoji catalogue changed', 'AbortError');
      const google = googleResult.value?.entries;
      const kitchen = kitchenResult.value?.split(/\r?\n/).filter(Boolean) || [];
      const entries = (Array.isArray(google) ? google : []).map(entry => ({ id: entry.id, kind: 'google',
        src: `/api/theme-emoji/google/${encodeURIComponent(entry.id)}${suffix}` }));
      for (const id of kitchen) entries.push({ id, kind: 'kitchen', src: `/api/theme-emoji/kitchen/${encodeURIComponent(id)}${suffix}` });
      if (!entries.length) throw googleResult.reason || kitchenResult.reason || new Error('Emoji catalogue is empty');
      catalogState = 'ready';
      return entries;
    }).catch(error => {
      if (epoch === cacheEpoch) {
        catalogPromise = null;
        catalogState = error.authenticationRequired ? 'authentication-required'
          : error.transient ? 'interrupted' : 'unavailable';
      }
      throw error;
    });
    catalogPromise = request;
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
  if (!catalogIndexPromise) {
    const epoch = cacheEpoch;
    catalogIndexPromise = getThemeEmojiCatalog()
      .then(entries => new Map(entries.map(entry => [entry.id, entry])))
      .catch(error => { if (epoch === cacheEpoch) catalogIndexPromise = null; throw error; });
  }
  return catalogIndexPromise;
}

function trimDecodedCache() {
  while (decodedPixels > MAX_DECODED_PIXELS && loaded.size > 1) {
    const key = loaded.keys().next().value;
    const asset = loaded.get(key);
    loaded.delete(key);
    decodedPixels = Math.max(0, decodedPixels - asset.width * asset.height);
  }
}

function rasterCacheKey(id, maxDimension) {
  return `${id}:${maxDimension}`;
}

function loadImage(entry, maxDimension, signal) {
  const cached = getThemeEmojiAsset(entry.id, { maxDimension });
  if (cached) return Promise.resolve(cached);
  const key = rasterCacheKey(entry.id, maxDimension);
  const existing = pending.get(key);
  if (existing && !existing.signal?.aborted) return existing.promise;
  const task = { signal, promise: null };
  const epoch = cacheEpoch;
  task.promise = (async () => {
    const acquired = await acquireLoadSlot(signal);
    if (!acquired) return null;
    const image = new Image();
    let sourceUrl = null;
    let abort;
    try {
      if (signal?.aborted || epoch !== cacheEpoch) return null;
      const response = await fetch(entry.src, { mode: 'same-origin', credentials: 'same-origin', redirect: 'error', signal });
      if (!response.ok) throw new Error(`Emoji artwork request failed (${response.status})`);
      const mime = response.headers.get('Content-Type')?.split(';', 1)[0].trim().toLowerCase();
      if (!SUPPORTED_IMAGE_TYPES.has(mime)) throw new Error('Unsupported emoji artwork type');
      const blob = await response.blob();
      if (signal?.aborted || epoch !== cacheEpoch) return null;
      sourceUrl = URL.createObjectURL(blob);
      const aborted = new Promise((_, reject) => {
        abort = () => reject(new DOMException('Emoji scene disposed', 'AbortError'));
      });
      signal?.addEventListener('abort', abort, { once: true });
      image.decoding = 'async';
      // Keep SVG markup inside the browser's image decoder. Nothing from
      // the response enters the document tree or runs as SVG document code.
      image.src = sourceUrl;
      // A cancelled decoder is not required to settle promptly in every
      // browser. Scene abort must independently release the global slot.
      await Promise.race([image.decode(), aborted]);
      if (signal?.aborted || epoch !== cacheEpoch) return null;
      const width = image.naturalWidth;
      const height = image.naturalHeight;
      if (!width || !height) throw new Error('Emoji artwork has empty bounds');
      const longestSide = Math.max(width, height);
      const scale = mime === SVG_MEDIA_TYPE ? (maxDimension || 128) / longestSide
        : Math.min(1, (maxDimension || longestSide) / longestSide);
      const rasterWidth = Math.max(1, Math.round(width * scale));
      const rasterHeight = Math.max(1, Math.round(height * scale));
      const drawable = document.createElement('canvas');
      drawable.width = rasterWidth;
      drawable.height = rasterHeight;
      const ctx = drawable.getContext('2d');
      if (!ctx) throw new Error('Emoji raster context unavailable');
      ctx.drawImage(image, 0, 0, rasterWidth, rasterHeight);
      const asset = { id: entry.id, kind: entry.kind, image: drawable, width: rasterWidth,
        height: rasterHeight, aspectRatio: rasterWidth / rasterHeight, rasterMaxDimension: maxDimension };
      loaded.set(key, asset);
      decodedPixels += rasterWidth * rasterHeight;
      trimDecodedCache();
      return asset;
    } catch (error) {
      if (!signal?.aborted) console.error('Emoji artwork image could not be decoded', entry.id, error);
      return null;
    } finally {
      if (abort) signal?.removeEventListener('abort', abort);
      if (sourceUrl) {
        image.removeAttribute('src');
        URL.revokeObjectURL(sourceUrl);
      }
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
  const key = rasterCacheKey(id, maxDimension);
  const asset = loaded.get(key);
  if (!asset) return null;
  loaded.delete(key);
  loaded.set(key, asset);
  return asset;
}
