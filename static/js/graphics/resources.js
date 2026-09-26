/**
 * Bounded GPU/CPU resource budgets.
 *
 * Every cache is hard-capped. Eviction is LRU so a long session cannot grow
 * texture, glyph, particle or paint allocations without bound. The scene
 * state that feeds these caches lives outside the GPU — these are render
 * resources only and are safe to drop at any time.
 */

/**
 * LRU cache with a hard entry cap.
 * @param {{ maxEntries: number }} options
 */
export function createBoundedCache({ maxEntries = 256 } = {}) {
  const limit = Math.max(1, Math.floor(maxEntries));
  const map = new Map();
  return {
    get limit() { return limit; },
    get size() { return map.size; },
    has(key) { return map.has(key); },
    get(key) {
      if (!map.has(key)) return undefined;
      const value = map.get(key);
      map.delete(key);
      map.set(key, value);
      return value;
    },
    set(key, value) {
      if (map.has(key)) map.delete(key);
      map.set(key, value);
      while (map.size > limit) {
        const oldest = map.keys().next();
        if (oldest.done) break;
        const evictedKey = oldest.value;
        const evicted = map.get(evictedKey);
        map.delete(evictedKey);
        if (evicted && typeof evicted.dispose === 'function') {
          try { evicted.dispose(); } catch (_) { /* best-effort release */ }
        }
      }
      return value;
    },
    delete(key) {
      const value = map.get(key);
      map.delete(key);
      if (value && typeof value.dispose === 'function') {
        try { value.dispose(); } catch (_) { /* best-effort release */ }
      }
      return value !== undefined;
    },
    keys() { return [...map.keys()]; },
    clear() {
      for (const value of map.values()) {
        if (value && typeof value.dispose === 'function') {
          try { value.dispose(); } catch (_) { /* best-effort release */ }
        }
      }
      map.clear();
    },
  };
}

/**
 * Glyph atlas budget. Entries are quantized raster sizes so live font-size
 * jitter cannot spawn unbounded sprites (T11).
 */
export function createGlyphAtlas({ maxEntries = 512, maxPixels = 1024 * 1024 } = {}) {
  const cache = createBoundedCache({ maxEntries });
  let pixels = 0;
  return {
    get size() { return cache.size; },
    get pixels() { return pixels; },
    get limit() { return cache.limit; },
    /** Quantize a CSS font size to a stable raster bucket. */
    quantizeSize(cssSize) {
      const n = Number(cssSize);
      if (!Number.isFinite(n) || n <= 0) return 12;
      const buckets = [8, 10, 12, 14, 16, 20, 24, 32, 48, 64];
      let best = buckets[0];
      let bestDelta = Math.abs(n - best);
      for (const bucket of buckets) {
        const delta = Math.abs(n - bucket);
        if (delta < bestDelta) { best = bucket; bestDelta = delta; }
      }
      return best;
    },
    key(ch, cssSize, weight, font) {
      return `${this.quantizeSize(cssSize)}:${weight || 400}:${font || 'sans-serif'}:${ch}`;
    },
    get(key) { return cache.get(key); },
    set(key, entry, entryPixels = 0) {
      const previous = cache.get(key);
      if (previous) pixels = Math.max(0, pixels - (previous.__pixels || 0));
      const wrapped = entry && typeof entry === 'object' ? entry : { value: entry };
      wrapped.__pixels = Math.max(0, Math.floor(entryPixels));
      cache.set(key, wrapped);
      pixels += wrapped.__pixels;
      // Pixel budget is enforced by evicting the oldest entries until under cap.
      while (pixels > maxPixels && cache.size > 1) {
        const oldest = cache.keys()[0];
        if (!oldest) break;
        cache.delete(oldest);
        // delete() disposes; recompute pixels conservatively.
        pixels = 0;
        for (const k of cache.keys()) {
          const item = cache.get(k);
          pixels += (item && item.__pixels) || 0;
        }
      }
      return wrapped;
    },
    clear() {
      cache.clear();
      pixels = 0;
    },
  };
}

/**
 * Particle / paint-cell budget. Callers reserve before spawning and release
 * on recycle so a scene cannot exceed the declared ceiling.
 */
export function createResourceBudget({ maxParticles = 4096, maxPaintCells = 20000 } = {}) {
  let particles = 0;
  let paintCells = 0;
  return {
    get particles() { return particles; },
    get paintCells() { return paintCells; },
    get maxParticles() { return maxParticles; },
    get maxPaintCells() { return maxPaintCells; },
    reserveParticles(count = 1) {
      const n = Math.max(0, Math.floor(count));
      if (particles + n > maxParticles) return 0;
      particles += n;
      return n;
    },
    releaseParticles(count = 1) {
      particles = Math.max(0, particles - Math.max(0, Math.floor(count)));
    },
    reservePaintCells(count = 1) {
      const n = Math.max(0, Math.floor(count));
      if (paintCells + n > maxPaintCells) return 0;
      paintCells += n;
      return n;
    },
    releasePaintCells(count = 1) {
      paintCells = Math.max(0, paintCells - Math.max(0, Math.floor(count)));
    },
    reset() {
      particles = 0;
      paintCells = 0;
    },
  };
}

/**
 * Clamp a requested backing-store size to the capability limits.
 * @returns {{ width:number, height:number, dpr:number, clamped:boolean }}
 */
export function clampCanvasAllocation(cssWidth, cssHeight, dpr, limits) {
  const maxDpr = Math.max(1, limits.maxDpr || 2);
  const maxTexture = Math.max(64, limits.maxTextureSize || 2048);
  const maxPixels = Math.max(4096, limits.maxTexturePixels || (2048 * 2048));
  const rawDpr = Number(dpr);
  const usedDpr = Math.max(1, Math.min(maxDpr, Number.isFinite(rawDpr) ? rawDpr : 1));
  let width = Math.max(1, Math.floor((Number(cssWidth) || 1) * usedDpr));
  let height = Math.max(1, Math.floor((Number(cssHeight) || 1) * usedDpr));
  let clamped = false;
  if (width > maxTexture) { width = maxTexture; clamped = true; }
  if (height > maxTexture) { height = maxTexture; clamped = true; }
  if (width * height > maxPixels) {
    const scale = Math.sqrt(maxPixels / (width * height));
    width = Math.max(1, Math.floor(width * scale));
    height = Math.max(1, Math.floor(height * scale));
    clamped = true;
  }
  return { width, height, dpr: usedDpr, clamped };
}
