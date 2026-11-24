import { clearCache, measureNaturalWidth, prepare } from '../vendor/pretext-matrix.js';
import { createBoundedCache } from './resources.js';

const RASTER_SIZE = 48;
const MATRIX_FONT = '400 48px monospace';
const MAX_RASTER_EDGE = 128;
const MAX_GLYPHS = 160;

/**
 * One white alpha mask per glyph. Pretext prepares the advance once. Canvas
 * provides ink bounds and paints the reusable mask at a fixed resolution.
 */
export function createMatrixGlyphCache(doc) {
  const measure = doc.createElement('canvas').getContext('2d');
  const glyphs = createBoundedCache({ maxEntries:MAX_GLYPHS });
  let disposed = false;
  if (measure) {
    measure.font = MATRIX_FONT;
    measure.textAlign = 'left';
    measure.textBaseline = 'alphabetic';
  }

  function rasterize(text) {
    const advance = measureNaturalWidth(prepare(text, MATRIX_FONT));
    if (!Number.isFinite(advance) || advance <= 0) return null;
    const ink = measure.measureText(text);
    const metric = (name, fallback) => Number.isFinite(ink[name]) ? ink[name] : fallback;
    const left = Math.floor(Math.min(0, -metric('actualBoundingBoxLeft', 0))) - 1;
    const right = Math.ceil(Math.max(advance, metric('actualBoundingBoxRight', advance))) + 1;
    const top = Math.floor(Math.min(0, -metric('actualBoundingBoxAscent', RASTER_SIZE * .8))) - 1;
    const bottom = Math.ceil(Math.max(0, metric('actualBoundingBoxDescent', RASTER_SIZE * .2))) + 1;
    const width = right - left;
    const height = bottom - top;
    if (width > MAX_RASTER_EDGE || height > MAX_RASTER_EDGE) return null;

    const source = doc.createElement('canvas');
    source.width = width;
    source.height = height;
    const ctx = source.getContext('2d');
    if (!ctx) return null;
    ctx.font = MATRIX_FONT;
    ctx.textAlign = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.fillStyle = '#fff';
    ctx.fillText(text, -left, -top);
    return {
      source, x:left / RASTER_SIZE, y:top / RASTER_SIZE,
      width:width / RASTER_SIZE, height:height / RASTER_SIZE,
    };
  }

  return {
    get size() { return glyphs.size; },
    get(text) {
      if (disposed || !measure || typeof text !== 'string' || !text) return null;
      const entry = glyphs.get(text);
      if (entry !== undefined) return entry;
      let result = null;
      try { result = rasterize(text); } catch (_) { /* Ordinary text fallback remains available. */ }
      return glyphs.set(text, result);
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      glyphs.clear();
      clearCache();
    },
  };
}
