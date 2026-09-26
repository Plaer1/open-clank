/**
 * Shared obstacle-field helper for rain collisions (S24).
 *
 * Builds a cached collision field from participating app elements' painted
 * alpha, bounds, clipping and ancestor opacity. Junction samples painted
 * pixels at alpha > 24/255; DOM surfaces do not expose a portable pixel mask,
 * so this adapter approximates that threshold from compositing inputs and
 * refuses to treat a CSS opacity:1 transparent box as an opaque obstacle.
 *
 * The field is refreshed on geometry/style changes, scrolling and resize —
 * never with expensive DOM scans for every particle or frame.
 */

export const PAINTED_ALPHA_THRESHOLD = 24 / 255;

const DEFAULT_SELECTORS = [
  '.chat-top-bar',
  '.chat-input-bar',
  '.msg-user',
  '.msg-ai',
  '.agent-thread-content',
  '.agent-tool-output',
  '.mimo-plan-dock',
  '.chat-context-popup',
  '.ctx-popup',
  '.toast.show',
  '.tour-hint',
  '[class*="popup"]:not(.hidden)',
  '[class*="popover"]:not(.hidden)',
  '.modal:not(.hidden) .modal-content',
  '.copal-tool-modal:not(.hidden) > *',
  '[role="dialog"]:not([aria-hidden="true"])',
  '.welcome-name',
  '.welcome-sub',
  '.welcome-tip',
];

function parseAlphaChannel(color) {
  if (!color || typeof color !== 'string') return 0;
  const value = color.trim().toLowerCase();
  if (!value || value === 'transparent' || value === 'none') return 0;
  if (value === 'currentcolor') return 1;
  const rgba = value.match(/^rgba?\(([^)]+)\)$/);
  if (!rgba) return 1; // named/hex colors are opaque when painted
  const parts = rgba[1].split(/[\s,/]+/).filter(Boolean);
  if (parts.length < 4) return 1;
  const alpha = Number(parts[3]);
  return Number.isFinite(alpha) ? Math.max(0, Math.min(1, alpha)) : 1;
}

function parseRect(raw) {
  if (!raw) return null;
  const left = Number(raw.left);
  const top = Number(raw.top);
  const width = Number(raw.width != null ? raw.width : (Number(raw.right) - left));
  const height = Number(raw.height != null ? raw.height : (Number(raw.bottom) - top));
  if (![left, top, width, height].every(Number.isFinite)) return null;
  return {
    left,
    top,
    right: left + width,
    bottom: top + height,
    width,
    height,
  };
}

function intersectRect(a, b) {
  if (!a) return b ? { ...b } : null;
  if (!b) return { ...a };
  const left = Math.max(a.left, b.left);
  const top = Math.max(a.top, b.top);
  const right = Math.min(a.right, b.right);
  const bottom = Math.min(a.bottom, b.bottom);
  if (right <= left || bottom <= top) return null;
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

/**
 * Painted-alpha estimate for one element.
 *
 * Accepts a computed-style-like object (or a live CSSStyleDeclaration).
 * `ancestorAlpha` is the product of composited ancestor opacities (1 at the
 * root). Text/canvas masks may refine geometry later; this helper only answers
 * "is there enough paint to collide?"
 */
export function estimatePaintedAlpha(style, ancestorAlpha = 1, flags = {}) {
  if (!style) return 0;
  const read = (kebab, camel) => {
    if (typeof style.getPropertyValue === 'function') {
      const value = style.getPropertyValue(kebab);
      if (value != null && value !== '') return value;
    }
    if (camel && style[camel] != null && style[camel] !== '') return style[camel];
    return style[kebab];
  };
  const display = read('display', 'display') || style.display;
  const visibility = read('visibility', 'visibility') || style.visibility;
  if (display === 'none') return 0;
  if (visibility === 'hidden' || visibility === 'collapse') return 0;
  const opacityRaw = read('opacity', 'opacity');
  const opacity = Number(opacityRaw != null && opacityRaw !== '' ? opacityRaw : style.opacity);
  const ownOpacity = Number.isFinite(opacity) ? Math.max(0, Math.min(1, opacity)) : 1;
  const ancestors = Number(ancestorAlpha);
  const composedOpacity = ownOpacity * (Number.isFinite(ancestors) ? Math.max(0, Math.min(1, ancestors)) : 1);
  if (composedOpacity <= PAINTED_ALPHA_THRESHOLD) return 0;

  const bgAlpha = parseAlphaChannel(read('background-color', 'backgroundColor') || style.backgroundColor);
  const borderAlpha = Math.max(
    parseAlphaChannel(read('border-top-color', 'borderTopColor') || style.borderTopColor) * (parseFloat(read('border-top-width', 'borderTopWidth') || style.borderTopWidth) > 0 ? 1 : 0),
    parseAlphaChannel(read('border-right-color', 'borderRightColor') || style.borderRightColor) * (parseFloat(read('border-right-width', 'borderRightWidth') || style.borderRightWidth) > 0 ? 1 : 0),
    parseAlphaChannel(read('border-bottom-color', 'borderBottomColor') || style.borderBottomColor) * (parseFloat(read('border-bottom-width', 'borderBottomWidth') || style.borderBottomWidth) > 0 ? 1 : 0),
    parseAlphaChannel(read('border-left-color', 'borderLeftColor') || style.borderLeftColor) * (parseFloat(read('border-left-width', 'borderLeftWidth') || style.borderLeftWidth) > 0 ? 1 : 0),
  );
  const backgroundImage = read('background-image', 'backgroundImage') || style.backgroundImage;
  const hasBackgroundImage = typeof backgroundImage === 'string'
    && backgroundImage !== 'none' && backgroundImage !== '';
  const boxShadow = read('box-shadow', 'boxShadow') || style.boxShadow;
  const hasBoxShadow = typeof boxShadow === 'string'
    && boxShadow !== 'none' && boxShadow !== '';
  // A transparent box with opacity:1 is not an opaque obstacle. Painted
  // coverage is the max of the surface's actual paint channels.
  const paintCoverage = Math.max(
    bgAlpha,
    borderAlpha,
    hasBackgroundImage ? 1 : 0,
    hasBoxShadow ? 0.55 : 0,
    // Ruled: visible text still collides even on a clear panel background.
    flags.hasTextPaint ? 1 : 0,
    flags.hasCanvasPaint ? 1 : 0,
  );
  return composedOpacity * paintCoverage;
}

function ancestorOpacityProduct(element, getStyle) {
  let product = 1;
  let node = element && element.parentElement;
  let guard = 0;
  while (node && guard < 32) {
    const style = getStyle(node);
    if (style) {
      if (style.display === 'none' || style.visibility === 'hidden') return 0;
      const opacity = Number(style.opacity);
      if (Number.isFinite(opacity)) product *= Math.max(0, Math.min(1, opacity));
    }
    node = node.parentElement;
    guard += 1;
  }
  return product;
}

function clippingRect(element, getRect, getStyle) {
  let clip = null;
  let node = element && element.parentElement;
  let guard = 0;
  while (node && guard < 32) {
    const style = getStyle(node);
    if (style) {
      const overflowX = style.overflowX || style.overflow;
      const overflowY = style.overflowY || style.overflow;
      const clipsX = overflowX === 'hidden' || overflowX === 'clip' || overflowX === 'scroll' || overflowX === 'auto';
      const clipsY = overflowY === 'hidden' || overflowY === 'clip' || overflowY === 'scroll' || overflowY === 'auto';
      if (clipsX || clipsY) {
        const box = parseRect(getRect(node));
        if (box) {
          const next = clipsX && clipsY
            ? box
            : {
              left: clipsX ? box.left : -Infinity,
              top: clipsY ? box.top : -Infinity,
              right: clipsX ? box.right : Infinity,
              bottom: clipsY ? box.bottom : Infinity,
            };
          if (clipsX && clipsY) clip = intersectRect(clip, box);
          else {
            clip = intersectRect(clip, {
              left: next.left,
              top: next.top,
              right: next.right,
              bottom: next.bottom,
              width: next.right - next.left,
              height: next.bottom - next.top,
            });
          }
          if (!clip) return null;
        }
      }
    }
    node = node.parentElement;
    guard += 1;
  }
  return clip;
}

function elementHasTextPaint(element) {
  if (!element) return false;
  if (typeof element.innerText === 'string' && element.innerText.trim()) return true;
  const walker = typeof element.childNodes !== 'undefined' ? element.childNodes : null;
  if (!walker) return false;
  for (let i = 0; i < walker.length; i += 1) {
    const node = walker[i];
    if (node && node.nodeType === 3 && String(node.textContent || '').trim()) return true;
    if (node && node.nodeType === 1 && elementHasTextPaint(node)) return true;
  }
  return false;
}

/**
 * Build obstacle entries for one element. Returns null when the element is
 * not a painted obstacle (hidden, clipped away, or below the alpha threshold).
 */
export function elementObstacleEntry(element, hooks = {}) {
  const getRect = hooks.getRect || ((el) => el.getBoundingClientRect());
  const getStyle = hooks.getStyle || ((el) => getComputedStyle(el));
  const style = getStyle(element);
  if (!style) return null;
  const rect = parseRect(getRect(element));
  if (!rect || rect.width < 8 || rect.height < 8) return null;

  const ancestorAlpha = hooks.ancestorAlpha != null
    ? hooks.ancestorAlpha
    : ancestorOpacityProduct(element, getStyle);
  const flags = {
    hasTextPaint: hooks.hasTextPaint ? hooks.hasTextPaint(element) : elementHasTextPaint(element),
    hasCanvasPaint: hooks.hasCanvasPaint ? hooks.hasCanvasPaint(element) : (element && element.tagName === 'CANVAS'),
  };
  const alpha = estimatePaintedAlpha(style, ancestorAlpha, flags);
  if (alpha <= PAINTED_ALPHA_THRESHOLD) return null;

  let box = rect;
  const clip = hooks.clippingRect
    ? hooks.clippingRect(element)
    : clippingRect(element, getRect, getStyle);
  if (clip) {
    box = intersectRect(rect, clip);
    if (!box) return null;
  }
  if (box.width < 8 || box.height < 8) return null;

  return {
    left: box.left - 4,
    top: box.top - 4,
    right: box.right + 4,
    bottom: box.bottom + 4,
    width: box.width + 8,
    height: box.height + 8,
    maskAlpha: Math.round(Math.min(1, alpha) * 255),
  };
}

/**
 * Create a cached obstacle field over a selector list.
 *
 * Invalidated by scroll/resize/geometry signals; also expires after `cacheMs`
 * so a missed signal cannot pin a stale field forever. Per-frame callers hit
 * the cache only.
 */
export function createObstacleField(options = {}) {
  const {
    root = typeof document !== 'undefined' ? document : null,
    selectors = DEFAULT_SELECTORS,
    minAlpha = PAINTED_ALPHA_THRESHOLD,
    cacheMs = 110,
    now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
    getRect = null,
    getStyle = null,
    win = typeof window !== 'undefined' ? window : null,
  } = options;

  let obstacles = [];
  let cachedAt = -Infinity;
  let dirty = true;
  let disposed = false;
  const listeners = [];

  function attach(target, type, handler) {
    if (!target || typeof target.addEventListener !== 'function') return;
    target.addEventListener(type, handler, { passive: true });
    listeners.push([target, type, handler]);
  }

  function invalidate() {
    if (disposed) return;
    dirty = true;
  }

  function sampleElements() {
    if (!root || typeof root.querySelectorAll !== 'function') return [];
    const seen = new Set();
    const entries = [];
    for (const selector of selectors) {
      let matches = [];
      try {
        matches = root.querySelectorAll(selector);
      } catch (_) {
        continue;
      }
      matches.forEach((element) => {
        if (!element || seen.has(element)) return;
        seen.add(element);
        const entry = elementObstacleEntry(element, {
          getRect: getRect || undefined,
          getStyle: getStyle || undefined,
        });
        if (!entry) return;
        if (entry.maskAlpha <= Math.round(minAlpha * 255)) return;
        entries.push(entry);
      });
    }
    return entries;
  }

  function refresh() {
    if (disposed) return obstacles;
    obstacles = sampleElements();
    cachedAt = now();
    dirty = false;
    return obstacles;
  }

  function current() {
    if (disposed) return obstacles;
    if (dirty || now() - cachedAt >= cacheMs) refresh();
    return obstacles;
  }

  function hitTest(x, y, radius = 0) {
    const list = current();
    for (let i = 0; i < list.length; i += 1) {
      const rect = list[i];
      if (x + radius > rect.left && x - radius < rect.right
        && y + radius > rect.top && y - radius < rect.bottom) {
        return rect;
      }
    }
    return null;
  }

  // Geometry/style changes, scrolling and resize — not per particle/frame.
  attach(win, 'scroll', invalidate);
  attach(win, 'resize', invalidate);
  attach(root, 'scroll', invalidate);
  if (win && typeof win.matchMedia === 'function') {
    try {
      const media = win.matchMedia('(prefers-reduced-motion: reduce)');
      if (media && typeof media.addEventListener === 'function') {
        media.addEventListener('change', invalidate);
        listeners.push([media, 'change', invalidate]);
      }
    } catch (_) { /* optional */ }
  }

  return {
    get obstacles() { return current(); },
    get size() { return current().length; },
    invalidate,
    refresh,
    hitTest,
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const [target, type, handler] of listeners) {
        try { target.removeEventListener(type, handler); } catch (_) { /* gone */ }
      }
      listeners.length = 0;
      obstacles = [];
    },
  };
}

export { DEFAULT_SELECTORS as DEFAULT_OBSTACLE_SELECTORS };
