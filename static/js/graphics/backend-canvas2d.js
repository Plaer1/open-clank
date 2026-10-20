/**
 * Ordinary Canvas2D graphics backend.
 *
 * This is the fallback that must work without WebGL. It implements the same
 * adapter surface as the WebGL2 backend so scene code never branches on
 * backend type. All draw calls accept CPU-side arrays; the backend owns only
 * the 2D context, never scene state.
 */


// Match the WebGL native-emoji distinction. Only complete, short sequences
// are eligible; ordinary monochrome text continues through fillText unchanged.
const NATIVE_EMOJI_PATTERN = (() => {
  try {
    return new RegExp('^(?:\\p{Extended_Pictographic}(?:\\uFE0E|\\uFE0F|\\p{Emoji_Modifier}|\\u20E3|\\u200D\\p{Extended_Pictographic})*|(?:\\p{Regional_Indicator}){2}|[0-9#*]\\uFE0F?\\u20E3)$', 'u');
  } catch (_) {
    return /^(?:[\u{1F000}-\u{1FAFF}]|[\u{1F1E6}-\u{1F1FF}]{2}|[0-9#*]\uFE0F?\u20E3)$/u;
  }
})();

const EMOJI_RASTER_SIZE = 24;
const MAX_NATIVE_EMOJI_ENTRIES = 512;
const MAX_NATIVE_EMOJI_PIXELS = 1024 * 1024;

function isNativeEmojiGlyph(value) {
  const text = String(value == null ? '' : value);
  return text.length > 0 && text.length <= 32 && NATIVE_EMOJI_PATTERN.test(text);
}

function isWhiteEmojiTint(value) {
  const color = String(value == null ? '#fff' : value).trim().toLowerCase();
  return color === '#fff' || color === '#ffffff';
}

export function createCanvas2DBackend({ canvas, limits }) {
  if (!canvas) throw new Error('canvas2d backend requires a canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('canvas2d context unavailable');

  let width = canvas.width || 1;
  let height = canvas.height || 1;
  let logicalWidth = width;
  let logicalHeight = height;
  let disposed = false;
  let lastComposite = 'source-over';
  // Per-backend LRU. Entries are fixed at 24px and scale on draw, preventing
  // native emoji animation from allocating a fresh raster for every size.
  const nativeEmojiCache = new Map();
  let nativeEmojiPixels = 0;

  function releaseNativeEmojiEntry(entry) {
    if (!entry) return;
    nativeEmojiPixels = Math.max(0, nativeEmojiPixels - (entry.pixels || 0));
    if (entry.canvas) {
      try { entry.canvas.width = 0; entry.canvas.height = 0; } catch (_) { /* detached */ }
    }
  }

  function clearNativeEmojiCache() {
    for (const entry of nativeEmojiCache.values()) releaseNativeEmojiEntry(entry);
    nativeEmojiCache.clear();
    nativeEmojiPixels = 0;
  }

  function touchNativeEmoji(key, entry) {
    nativeEmojiCache.delete(key);
    nativeEmojiCache.set(key, entry);
    return entry;
  }

  function createRasterCanvas(widthValue, heightValue) {
    const doc = canvas.ownerDocument || (typeof document !== 'undefined' ? document : null);
    let surface = null;
    try {
      if (typeof OffscreenCanvas !== 'undefined') surface = new OffscreenCanvas(widthValue, heightValue);
      else if (doc && typeof doc.createElement === 'function') {
        surface = doc.createElement('canvas');
        surface.width = widthValue;
        surface.height = heightValue;
      }
    } catch (_) { return null; }
    return surface;
  }

  function nativeEmojiEntry(glyph) {
    if (!isNativeEmojiGlyph(glyph.text) || !isWhiteEmojiTint(glyph.color)
        || (glyph.glow && glyph.glow.blur > 0)) return null;
    const text = String(glyph.text);
    const weight = glyph.weight || 400;
    const family = glyph.font || 'sans-serif';
    const key = `${text}\u0000${weight}\u0000${family}`;
    const cached = nativeEmojiCache.get(key);
    if (cached) return touchNativeEmoji(key, cached);

    let surface = null;
    try {
      // Measure/rasterize once with source-equivalent left/alphabetic metrics.
      const font = `${weight} ${EMOJI_RASTER_SIZE}px ${family}`;
      let metrics = null;
      let saved = false;
      try {
        ctx.save();
        saved = true;
        ctx.font = font;
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
        metrics = ctx.measureText(text);
      } finally {
        if (saved) ctx.restore();
      }
      const metric = (name, fallback) => {
        const value = Number(metrics?.[name]);
        return Number.isFinite(value) && value >= 0 ? value : fallback;
      };
      const advance = metric('width', EMOJI_RASTER_SIZE * .6 * text.length);
      const inkLeft = metric('actualBoundingBoxLeft', 0);
      const inkRight = metric('actualBoundingBoxRight', Math.max(advance, EMOJI_RASTER_SIZE * .6 * text.length));
      const fontAscent = metric('fontBoundingBoxAscent', metric('actualBoundingBoxAscent', EMOJI_RASTER_SIZE * .8));
      const fontDescent = metric('fontBoundingBoxDescent', metric('actualBoundingBoxDescent', EMOJI_RASTER_SIZE * .2));
      const inkAscent = metric('actualBoundingBoxAscent', fontAscent);
      const inkDescent = metric('actualBoundingBoxDescent', fontDescent);
      const pad = 1;
      const rasterWidth = Math.max(1, Math.ceil(inkLeft + inkRight) + pad * 2);
      const rasterHeight = Math.max(1, Math.ceil(inkAscent + inkDescent) + pad * 2);
      const pixels = rasterWidth * rasterHeight;
      if (pixels > MAX_NATIVE_EMOJI_PIXELS) return null;
      surface = createRasterCanvas(rasterWidth, rasterHeight);
      const rasterCtx = surface && surface.getContext && surface.getContext('2d');
      if (!rasterCtx) return null;
      rasterCtx.font = font;
      rasterCtx.textAlign = 'left';
      rasterCtx.textBaseline = 'alphabetic';
      // Native color emoji ignore this white fill; it keeps fallback glyphs
      // consistent with the #fff/white WebGL atlas tint contract.
      rasterCtx.fillStyle = '#ffffff';
      rasterCtx.fillText(text, pad + inkLeft, pad + inkAscent);
      while (nativeEmojiCache.size >= MAX_NATIVE_EMOJI_ENTRIES
          || nativeEmojiPixels + pixels > MAX_NATIVE_EMOJI_PIXELS) {
        const oldest = nativeEmojiCache.entries().next().value;
        if (!oldest) break;
        nativeEmojiCache.delete(oldest[0]);
        releaseNativeEmojiEntry(oldest[1]);
      }
      if (nativeEmojiPixels + pixels > MAX_NATIVE_EMOJI_PIXELS) return null;
      const entry = {
        canvas: surface, pixels, rasterWidth, rasterHeight, advance, inkLeft,
        inkAscent, fontAscent, fontDescent, pad,
      };
      nativeEmojiCache.set(key, entry);
      nativeEmojiPixels += pixels;
      surface = null; // cache owns the successful raster.
      return entry;
    } catch (_) {
      // A failed offscreen path is optional; use ordinary fillText instead.
      return null;
    } finally {
      // Failed normal returns and exceptions release any raster not transferred
      // to the cache. Successful ownership transfer sets surface to null.
      if (surface) {
        try { surface.width = 0; surface.height = 0; } catch (_) { /* detached */ }
      }
    }
  }

  function drawNativeEmoji(glyph) {
    const entry = nativeEmojiEntry(glyph);
    if (!entry) return false;
    try {
      const requestedSize = Math.max(1, Number(glyph.size) || EMOJI_RASTER_SIZE);
      const scale = requestedSize / EMOJI_RASTER_SIZE;
      const advance = entry.advance * scale;
      let anchorX = glyph.x;
      if (glyph.align === 'center') anchorX -= advance / 2;
      else if (glyph.align === 'right' || glyph.align === 'end') anchorX -= advance;
      let baselineY = glyph.y;
      if (glyph.baseline === 'top') baselineY += entry.fontAscent * scale;
      else if (glyph.baseline === 'middle') baselineY += (entry.fontAscent - entry.fontDescent) * scale / 2;
      else if (glyph.baseline === 'bottom' || glyph.baseline === 'ideographic') baselineY -= entry.fontDescent * scale;
      else if (glyph.baseline === 'hanging') baselineY += entry.fontAscent * scale * .8;
      const drawX = anchorX - (entry.inkLeft + entry.pad) * scale;
      const drawY = baselineY - (entry.inkAscent + entry.pad) * scale;
      ctx.drawImage(entry.canvas, drawX, drawY, entry.rasterWidth * scale, entry.rasterHeight * scale);
      return true;
    } catch (_) {
      return false;
    }
  }

  function alive() {
    return !disposed && !!ctx;
  }

  return {
    kind: 'canvas2d',
    accelerated: false,
    limits: limits || {},
    get width() { return width; },
    get height() { return height; },
    get disposed() { return disposed; },

    resize(nextWidth, nextHeight, dpr = 1, logicalExtent = null) {
      if (!alive()) return;
      width = Math.max(1, Math.floor(nextWidth));
      height = Math.max(1, Math.floor(nextHeight));
      const fallbackWidth = width / Math.max(1, Number(dpr) || 1);
      const fallbackHeight = height / Math.max(1, Number(dpr) || 1);
      logicalWidth = Math.max(1, Number(logicalExtent?.width) || fallbackWidth);
      logicalHeight = Math.max(1, Number(logicalExtent?.height) || fallbackHeight);
      canvas.width = width;
      canvas.height = height;
      // Draw commands remain in CSS/logical coordinates. When allocation is
      // clamped this scales the full logical viewport into the smaller backing
      // store instead of cropping its right or bottom edge.
      if (typeof ctx.setTransform === 'function') {
        ctx.setTransform(width / logicalWidth, 0, 0, height / logicalHeight, 0, 0);
      }
    },

    beginFrame() {
      if (!alive()) return;
      ctx.save();
      lastComposite = 'source-over';
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
    },

    clear(r = 0, g = 0, b = 0, a = 0) {
      if (!alive()) return;
      if (a <= 0) {
        ctx.clearRect(0, 0, width, height);
        return;
      }
      ctx.save();
      ctx.globalCompositeOperation = 'source-over';
      ctx.globalAlpha = 1;
      ctx.fillStyle = `rgba(${Math.round(r * 255)},${Math.round(g * 255)},${Math.round(b * 255)},${a})`;
      ctx.fillRect(0, 0, width, height);
      ctx.restore();
    },

    setAlpha(alpha) {
      if (!alive()) return;
      ctx.globalAlpha = Math.max(0, Math.min(1, Number(alpha) || 0));
    },

    setComposite(operation) {
      if (!alive()) return;
      lastComposite = operation || 'source-over';
      ctx.globalCompositeOperation = lastComposite;
    },

    /**
     * Batched axis-aligned rectangles.
     * rects: Array<{ x, y, w, h, color, alpha }>
     */
    drawRects(rects) {
      if (!alive() || !rects || !rects.length) return;
      for (const rect of rects) {
        if (!rect) continue;
        ctx.globalAlpha = rect.alpha == null ? 1 : Math.max(0, Math.min(1, rect.alpha));
        ctx.fillStyle = rect.color || '#fff';
        ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
      }
      ctx.globalAlpha = 1;
    },

    /**
     * Batched line segments.
     * lines: Array<{ x1, y1, x2, y2, color, width, alpha }>
     */
    drawLines(lines) {
      if (!alive() || !lines || !lines.length) return;
      for (const line of lines) {
        if (!line) continue;
        ctx.globalAlpha = line.alpha == null ? 1 : Math.max(0, Math.min(1, line.alpha));
        ctx.strokeStyle = line.color || '#fff';
        ctx.lineWidth = line.width == null ? 1 : line.width;
        ctx.beginPath();
        ctx.moveTo(line.x1, line.y1);
        ctx.lineTo(line.x2, line.y2);
        ctx.stroke();
      }
      ctx.globalAlpha = 1;
    },

    /**
     * Batched filled circles.
     * circles: Array<{ x, y, r, color, alpha, stroke, strokeWidth }>
     */
    drawCircles(circles) {
      if (!alive() || !circles || !circles.length) return;
      for (const circle of circles) {
        if (!circle) continue;
        ctx.globalAlpha = circle.alpha == null ? 1 : Math.max(0, Math.min(1, circle.alpha));
        ctx.beginPath();
        ctx.arc(circle.x, circle.y, Math.max(0, circle.r || 0), 0, Math.PI * 2);
        if (circle.color) {
          ctx.fillStyle = circle.color;
          ctx.fill();
        }
        if (circle.stroke) {
          ctx.strokeStyle = circle.stroke;
          ctx.lineWidth = circle.strokeWidth == null ? 1 : circle.strokeWidth;
          ctx.stroke();
        }
      }
      ctx.globalAlpha = 1;
    },

    /**
     * Glyph runs. Canvas2D ordinarily uses fillText; complete native white
     * emoji use this backend's bounded fixed-size raster cache.
     * glyphs: Array<{ text, x, y, size, weight, color, alpha }>
     */
    drawGlyphs(glyphs) {
      if (!alive() || !glyphs || !glyphs.length) return;
      for (const glyph of glyphs) {
        if (!glyph || glyph.text == null) continue;
        ctx.globalAlpha = glyph.alpha == null ? 1 : Math.max(0, Math.min(1, glyph.alpha));
        // Native white emoji route through a fixed 24px raster cache. All
        // other glyph semantics retain the ordinary Canvas2D fillText path.
        if (drawNativeEmoji(glyph)) continue;
        ctx.fillStyle = glyph.color || '#fff';
        const family = glyph.font || 'sans-serif';
        ctx.font = `${glyph.weight || 400} ${glyph.size || 12}px ${family}`;
        if (glyph.align) ctx.textAlign = glyph.align;
        if (glyph.baseline) ctx.textBaseline = glyph.baseline;
        // Head-glyph glow: pre-S24 shadowBlur on the leading glyph only.
        const glow = glyph.glow;
        if (glow && glow.blur > 0) {
          ctx.shadowColor = glow.color || glyph.color || '#fff';
          ctx.shadowBlur = glow.blur;
        }
        ctx.fillText(glyph.text, glyph.x, glyph.y);
        if (glow && glow.blur > 0) {
          ctx.shadowBlur = 0;
          ctx.shadowColor = 'transparent';
        }
      }
      ctx.globalAlpha = 1;
      ctx.textAlign = 'start';
      ctx.textBaseline = 'alphabetic';
    },

    /** Paint a ready image source with per-sprite opacity. */
    drawImage(source, dx, dy, dw, dh, alpha = 1) {
      if (!alive() || !source) return;
      const previousAlpha = ctx.globalAlpha;
      const requestedAlpha = Number(alpha);
      const opacity = Math.max(0, Math.min(1, Number.isFinite(requestedAlpha) ? requestedAlpha : 1));
      ctx.globalAlpha = previousAlpha * opacity;
      try {
        if (dw == null) ctx.drawImage(source, dx, dy);
        else ctx.drawImage(source, dx, dy, dw, dh);
      } catch (_) {
        // A source that became unavailable between lookup and paint is skipped.
      } finally {
        ctx.globalAlpha = previousAlpha;
      }
    },

    endFrame() {
      if (!alive()) return;
      ctx.restore();
    },

    dispose() {
      if (disposed) return;
      disposed = true;
      clearNativeEmojiCache();
    },
  };
}
