/**
 * Ordinary Canvas2D graphics backend.
 *
 * This is the fallback that must work without WebGL. It implements the same
 * adapter surface as the WebGL2 backend so scene code never branches on
 * backend type. All draw calls accept CPU-side arrays; the backend owns only
 * the 2D context, never scene state.
 */

export function createCanvas2DBackend({ canvas, limits }) {
  if (!canvas) throw new Error('canvas2d backend requires a canvas');
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('canvas2d context unavailable');

  let width = canvas.width || 1;
  let height = canvas.height || 1;
  let disposed = false;
  let lastComposite = 'source-over';

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

    resize(nextWidth, nextHeight, dpr = 1) {
      if (!alive()) return;
      width = Math.max(1, Math.floor(nextWidth));
      height = Math.max(1, Math.floor(nextHeight));
      canvas.width = width;
      canvas.height = height;
      if (typeof ctx.setTransform === 'function') ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
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
     * Glyph runs. The Canvas2D backend rasterizes directly via fillText —
     * the atlas is an optimization on the WebGL2 path and is optional here.
     * glyphs: Array<{ text, x, y, size, weight, color, alpha }>
     */
    drawGlyphs(glyphs) {
      if (!alive() || !glyphs || !glyphs.length) return;
      for (const glyph of glyphs) {
        if (!glyph || glyph.text == null) continue;
        ctx.globalAlpha = glyph.alpha == null ? 1 : Math.max(0, Math.min(1, glyph.alpha));
        ctx.fillStyle = glyph.color || '#fff';
        const family = glyph.font || 'sans-serif';
        ctx.font = `${glyph.weight || 400} ${glyph.size || 12}px ${family}`;
        if (glyph.align) ctx.textAlign = glyph.align;
        if (glyph.baseline) ctx.textBaseline = glyph.baseline;
        ctx.fillText(glyph.text, glyph.x, glyph.y);
      }
      ctx.globalAlpha = 1;
      ctx.textAlign = 'start';
      ctx.textBaseline = 'alphabetic';
    },

    /** Paint an existing canvas/image as a texture-sized blit. */
    drawImage(source, dx, dy, dw, dh) {
      if (!alive() || !source) return;
      if (dw == null) ctx.drawImage(source, dx, dy);
      else ctx.drawImage(source, dx, dy, dw, dh);
    },

    endFrame() {
      if (!alive()) return;
      ctx.restore();
    },

    dispose() {
      if (disposed) return;
      disposed = true;
    },
  };
}
