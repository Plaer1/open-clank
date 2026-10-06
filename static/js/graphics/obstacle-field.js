/**
 * Shared painted obstacle field for foreground rain.
 *
 * Filled surfaces retain their geometric box (including rounded corners). Text
 * never promotes its transparent parent to a box: its cached collision mask is
 * rasterized from live Range positions and computed font/style data instead.
 * Mask construction only occurs while the bounded field is refreshed; particle
 * hit tests never read DOM or rasterize glyphs.
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

const MAX_FIELD_ENTRIES = 96;
const MAX_MASK_CACHE = 72;
const MAX_TEXT_MASKS_PER_ENTRY = 24;
const MAX_GLYPH_TOKENS_PER_ENTRY = 160;
const MAX_MASK_PIXELS = 96 * 1024;
const GLYPH_PADDING = 3;

function clamp(value, low, high) {
  return Math.max(low, Math.min(high, value));
}

function alphaValue(value) {
  const raw = String(value == null ? '' : value).trim();
  if (!raw) return 1;
  if (raw.endsWith('%')) {
    const percent = Number.parseFloat(raw.slice(0, -1));
    return Number.isFinite(percent) ? clamp(percent / 100, 0, 1) : 1;
  }
  const numeric = Number(raw);
  return Number.isFinite(numeric) ? clamp(numeric, 0, 1) : 1;
}

function parseAlphaChannel(color) {
  if (!color || typeof color !== 'string') return 0;
  const value = color.trim().toLowerCase();
  if (!value || value === 'transparent' || value === 'none') return 0;
  if (value === 'currentcolor') return 1;
  const rgba = value.match(/^rgba?\(([^)]+)\)$/);
  if (!rgba) return 1; // named, hex, lab, and resolved colors paint opaquely
  const parts = rgba[1].split(/[\s,/]+/).filter(Boolean);
  return parts.length < 4 ? 1 : alphaValue(parts[3]);
}

function styleValue(style, kebab, camel) {
  if (!style) return '';
  if (typeof style.getPropertyValue === 'function') {
    const value = style.getPropertyValue(kebab);
    if (value != null && value !== '') return value;
  }
  return style[camel] != null && style[camel] !== '' ? style[camel] : (style[kebab] || '');
}

function parseRect(raw) {
  if (!raw) return null;
  const left = Number(raw.left);
  const top = Number(raw.top);
  const width = Number(raw.width != null ? raw.width : Number(raw.right) - left);
  const height = Number(raw.height != null ? raw.height : Number(raw.bottom) - top);
  if (![left, top, width, height].every(Number.isFinite) || width <= 0 || height <= 0) return null;
  return { left, top, right: left + width, bottom: top + height, width, height };
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

function unionRect(a, b) {
  if (!a) return b ? { ...b } : null;
  if (!b) return { ...a };
  const left = Math.min(a.left, b.left);
  const top = Math.min(a.top, b.top);
  const right = Math.max(a.right, b.right);
  const bottom = Math.max(a.bottom, b.bottom);
  return { left, top, right, bottom, width: right - left, height: bottom - top };
}

function radiusValue(value, limit) {
  const token = String(value || '0').trim().split(/[\s/]+/)[0] || '0';
  if (token.endsWith('%')) return clamp((Number.parseFloat(token) || 0) * limit / 100, 0, limit / 2);
  return clamp(Number.parseFloat(token) || 0, 0, limit / 2);
}

function cornerRadii(style, rect) {
  return {
    tl: radiusValue(styleValue(style, 'border-top-left-radius', 'borderTopLeftRadius'), Math.min(rect.width, rect.height)),
    tr: radiusValue(styleValue(style, 'border-top-right-radius', 'borderTopRightRadius'), Math.min(rect.width, rect.height)),
    br: radiusValue(styleValue(style, 'border-bottom-right-radius', 'borderBottomRightRadius'), Math.min(rect.width, rect.height)),
    bl: radiusValue(styleValue(style, 'border-bottom-left-radius', 'borderBottomLeftRadius'), Math.min(rect.width, rect.height)),
  };
}

function borderPaint(style) {
  const widths = {};
  const alphas = {};
  for (const [side, camel] of [['top', 'Top'], ['right', 'Right'], ['bottom', 'Bottom'], ['left', 'Left']]) {
    widths[side] = Math.max(0, Number.parseFloat(styleValue(style, `border-${side}-width`, `border${camel}Width`)) || 0);
    alphas[side] = widths[side] > 0 ? parseAlphaChannel(styleValue(style, `border-${side}-color`, `border${camel}Color`)) : 0;
  }
  return { widths, alphas, alpha: Math.max(...Object.values(alphas)) };
}

function backgroundPaintAlpha(style) {
  const backgroundImage = styleValue(style, 'background-image', 'backgroundImage');
  const clip = `${styleValue(style, 'background-clip', 'backgroundClip')} ${styleValue(style, '-webkit-background-clip', 'webkitBackgroundClip')}`;
  // A text-clipped background has no rectangular surface paint; it is handled
  // by the glyph raster below, where alpha is limited to actual letters.
  const textClipped = /\btext\b/.test(clip);
  const background = textClipped ? 0 : parseAlphaChannel(styleValue(style, 'background-color', 'backgroundColor'));
  const image = textClipped ? 0
    : (typeof backgroundImage === 'string' && backgroundImage !== '' && backgroundImage !== 'none' ? 1 : 0);
  return Math.max(background, image);
}

function textPaintAlpha(style) {
  const fill = styleValue(style, '-webkit-text-fill-color', 'webkitTextFillColor');
  const color = fill || styleValue(style, 'color', 'color');
  const colorAlpha = parseAlphaChannel(color);
  const clip = `${styleValue(style, 'background-clip', 'backgroundClip')} ${styleValue(style, '-webkit-background-clip', 'webkitBackgroundClip')}`;
  const backgroundImage = styleValue(style, 'background-image', 'backgroundImage');
  // A gradient clipped to text paints only the glyphs, even when the browser
  // reports transparent -webkit-text-fill-color (the live welcome wordmark).
  const clippedBackground = /\btext\b/.test(clip)
    && typeof backgroundImage === 'string'
    && backgroundImage !== ''
    && backgroundImage !== 'none';
  return Math.max(colorAlpha, clippedBackground ? 1 : 0);
}

/**
 * Public painted-alpha estimate retained for callers that need a scalar. Text
 * paint is included here, but `elementObstacleEntry` routes text into glyph
 * masks instead of treating its ancestor rectangle as solid.
 */
export function estimatePaintedAlpha(style, ancestorAlpha = 1, flags = {}) {
  if (!style) return 0;
  const display = styleValue(style, 'display', 'display');
  const visibility = styleValue(style, 'visibility', 'visibility');
  if (display === 'none' || visibility === 'hidden' || visibility === 'collapse') return 0;
  const ownOpacity = alphaValue(styleValue(style, 'opacity', 'opacity'));
  const composedOpacity = ownOpacity * clamp(Number.isFinite(Number(ancestorAlpha)) ? Number(ancestorAlpha) : 1, 0, 1);
  if (composedOpacity <= PAINTED_ALPHA_THRESHOLD) return 0;
  const coverage = Math.max(
    backgroundPaintAlpha(style),
    borderPaint(style).alpha,
    flags.hasTextPaint ? textPaintAlpha(style) : 0,
    flags.hasCanvasPaint ? 1 : 0,
  );
  return composedOpacity * coverage;
}

function ancestorOpacityProduct(element, getStyle) {
  let product = 1;
  let node = element && element.parentElement;
  let guard = 0;
  while (node && guard < 32) {
    const style = getStyle(node);
    if (style) {
      if (styleValue(style, 'display', 'display') === 'none' || styleValue(style, 'visibility', 'visibility') === 'hidden') return 0;
      product *= alphaValue(styleValue(style, 'opacity', 'opacity'));
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
    const overflowX = styleValue(style, 'overflow-x', 'overflowX') || styleValue(style, 'overflow', 'overflow');
    const overflowY = styleValue(style, 'overflow-y', 'overflowY') || styleValue(style, 'overflow', 'overflow');
    const clipsX = ['hidden', 'clip', 'scroll', 'auto'].includes(overflowX);
    const clipsY = ['hidden', 'clip', 'scroll', 'auto'].includes(overflowY);
    if (clipsX || clipsY) {
      const box = parseRect(getRect(node));
      if (box) {
        clip = intersectRect(clip, {
          left: clipsX ? box.left : -Infinity,
          top: clipsY ? box.top : -Infinity,
          right: clipsX ? box.right : Infinity,
          bottom: clipsY ? box.bottom : Infinity,
          width: box.width,
          height: box.height,
        });
        if (!clip) return false;
      }
    }
    node = node.parentElement;
    guard += 1;
  }
  return clip;
}

function elementHasTextPaint(element) {
  return !!(element && typeof element.textContent === 'string' && element.textContent.trim());
}

function canvasFont(style) {
  const direct = styleValue(style, 'font', 'font');
  if (direct && direct !== 'normal normal normal normal 16px / normal serif') return direct;
  const size = styleValue(style, 'font-size', 'fontSize') || '16px';
  const family = styleValue(style, 'font-family', 'fontFamily') || 'sans-serif';
  const weight = styleValue(style, 'font-weight', 'fontWeight') || 'normal';
  const fontStyle = styleValue(style, 'font-style', 'fontStyle') || 'normal';
  const variant = styleValue(style, 'font-variant', 'fontVariant') || 'normal';
  return `${fontStyle} ${variant} ${weight} ${size} ${family}`;
}

function fontPixels(style, rect) {
  const size = Number.parseFloat(styleValue(style, 'font-size', 'fontSize')) || Math.max(1, rect.height * 0.78);
  const lineHeightValue = styleValue(style, 'line-height', 'lineHeight');
  const lineHeight = lineHeightValue === 'normal'
    ? size * 1.2
    : (Number.parseFloat(lineHeightValue) || size * 1.2);
  return { size, lineHeight };
}

function textNodes(element) {
  const doc = element && element.ownerDocument;
  if (!doc || typeof doc.createTreeWalker !== 'function') return [];
  const walker = doc.createTreeWalker(element, 4); // NodeFilter.SHOW_TEXT
  const nodes = [];
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    if (String(node.nodeValue || '').trim()) nodes.push(node);
  }
  return nodes;
}

function rangeGlyphTokens(node, doc, limit) {
  const value = String(node.nodeValue || '');
  const tokens = [];
  for (let start = 0; start < value.length && tokens.length < limit;) {
    const codePoint = value.codePointAt(start);
    const length = codePoint > 0xffff ? 2 : 1;
    const glyph = value.slice(start, start + length);
    if (!glyph.trim()) {
      start += length;
      continue;
    }
    try {
      const range = doc.createRange();
      range.setStart(node, start);
      range.setEnd(node, start + length);
      const rect = parseRect(range.getBoundingClientRect());
      if (typeof range.detach === 'function') range.detach();
      if (rect && rect.width > 0 && rect.height > 0) tokens.push({ glyph, rect });
    } catch (_) {
      return tokens;
    }
    start += length;
  }
  return tokens;
}

function maskKey(tokens, style, alpha, clip) {
  const styleKey = [
    canvasFont(style), styleValue(style, 'letter-spacing', 'letterSpacing'),
    styleValue(style, 'word-spacing', 'wordSpacing'), styleValue(style, 'transform', 'transform'),
    styleValue(style, 'color', 'color'), styleValue(style, '-webkit-text-fill-color', 'webkitTextFillColor'),
    styleValue(style, 'background-image', 'backgroundImage'), alpha.toFixed(4),
  ].join('|');
  const geometry = tokens.map(({ glyph, rect, paintRect = rect }) => `${glyph}:${paintRect.left.toFixed(2)},${paintRect.top.toFixed(2)},${paintRect.width.toFixed(2)},${paintRect.height.toFixed(2)}>${rect.left.toFixed(2)},${rect.top.toFixed(2)},${rect.width.toFixed(2)},${rect.height.toFixed(2)}`).join(';');
  const clipped = clip ? `${clip.left.toFixed(2)},${clip.top.toFixed(2)},${clip.right.toFixed(2)},${clip.bottom.toFixed(2)}` : '';
  return `${styleKey}|${clipped}|${geometry}`;
}

function cacheMask(cache, key, value) {
  if (cache.has(key)) return cache.get(key);
  while (cache.size >= MAX_MASK_CACHE) cache.delete(cache.keys().next().value);
  cache.set(key, value);
  return value;
}

function buildGlyphMask(tokens, style, alpha, clip, element, cache) {
  if (!tokens.length || alpha <= PAINTED_ALPHA_THRESHOLD) return null;
  const visibleTokens = tokens.map((token) => {
    const paintRect = { ...token.rect };
    const rect = clip ? intersectRect(paintRect, clip) : { ...paintRect };
    // Keep paint geometry separate from the clipped collision surface. Canvas
    // draws at the live Range position; its clipped bitmap provides the crop.
    return rect ? { glyph: token.glyph, paintRect, rect } : null;
  }).filter(Boolean);
  if (!visibleTokens.length) return null;
  let bounds = null;
  for (const token of visibleTokens) bounds = unionRect(bounds, token.rect);
  if (!bounds) return null;
  bounds = {
    left: bounds.left - GLYPH_PADDING,
    top: bounds.top - GLYPH_PADDING,
    right: bounds.right + GLYPH_PADDING,
    bottom: bounds.bottom + GLYPH_PADDING,
    width: bounds.width + GLYPH_PADDING * 2,
    height: bounds.height + GLYPH_PADDING * 2,
  };
  if (clip) {
    bounds = intersectRect(bounds, clip);
    if (!bounds) return null;
  }
  const key = maskKey(visibleTokens, style, alpha, clip);
  if (cache && cache.has(key)) return cache.get(key);
  const doc = element && element.ownerDocument;
  if (!doc || typeof doc.createElement !== 'function') return null;
  let scale = clamp(Number(doc.defaultView && doc.defaultView.devicePixelRatio) || 1, 1, 2);
  const maxScale = Math.sqrt(MAX_MASK_PIXELS / Math.max(1, bounds.width * bounds.height));
  scale = Math.min(scale, maxScale);
  if (scale < 0.5) return null;
  const width = Math.max(1, Math.ceil(bounds.width * scale));
  const height = Math.max(1, Math.ceil(bounds.height * scale));
  const canvas = doc.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext && canvas.getContext('2d', { willReadFrequently: true });
  if (!ctx) return null;
  ctx.setTransform(scale, 0, 0, scale, 0, 0);
  ctx.fillStyle = '#fff';
  ctx.textBaseline = 'alphabetic';
  ctx.textAlign = 'left';
  for (const token of visibleTokens) {
    const paintRect = token.paintRect;
    ctx.font = canvasFont(style);
    const metrics = ctx.measureText(token.glyph);
    const ascent = Number(metrics.fontBoundingBoxAscent);
    const descent = Number(metrics.fontBoundingBoxDescent);
    const fontHeight = ascent + descent;
    const baseline = Number.isFinite(fontHeight) && fontHeight > 0
      ? paintRect.top - bounds.top + Math.max(0, (paintRect.height - fontHeight) / 2) + ascent
      : paintRect.bottom - bounds.top - Math.max(0, (fontPixels(style, paintRect).lineHeight - fontPixels(style, paintRect).size) / 2);
    ctx.fillText(token.glyph, paintRect.left - bounds.left, baseline);
  }
  let image;
  try {
    image = ctx.getImageData(0, 0, width, height).data;
  } catch (_) {
    return null;
  }
  const pixels = new Uint8Array(width * height);
  let hasPaint = false;
  for (let index = 0, pixel = 0; pixel < pixels.length; index += 4, pixel += 1) {
    pixels[pixel] = image[index + 3];
    hasPaint ||= pixels[pixel] > 24;
  }
  if (!hasPaint) return null;
  const mask = {
    left: bounds.left,
    top: bounds.top,
    right: bounds.right,
    bottom: bounds.bottom,
    width,
    height,
    scale,
    alpha: Math.round(clamp(alpha, 0, 1) * 255),
    // The bounded Range rectangles identify the letter struck by a probe.
    tokens: visibleTokens.map((token) => ({ glyph: token.glyph, rect: { ...token.rect } })),
    pixels,
  };
  return cache ? cacheMask(cache, key, mask) : mask;
}

function textMasksForElement(element, getStyle, clip, maskCache) {
  const doc = element && element.ownerDocument;
  if (!doc || typeof doc.createRange !== 'function') return [];
  const masks = [];
  let remaining = MAX_GLYPH_TOKENS_PER_ENTRY;
  for (const node of textNodes(element)) {
    if (!remaining || masks.length >= MAX_TEXT_MASKS_PER_ENTRY) break;
    const parent = node.parentElement || element;
    const style = getStyle(parent);
    if (!style || styleValue(style, 'display', 'display') === 'none' || styleValue(style, 'visibility', 'visibility') !== 'visible' && styleValue(style, 'visibility', 'visibility') !== '') continue;
    const alpha = textPaintAlpha(style) * alphaValue(styleValue(style, 'opacity', 'opacity')) * ancestorOpacityProduct(parent, getStyle);
    if (alpha <= PAINTED_ALPHA_THRESHOLD) continue;
    const tokens = rangeGlyphTokens(node, doc, remaining);
    remaining -= tokens.length;
    const mask = buildGlyphMask(tokens, style, alpha, clip, element, maskCache);
    if (mask) masks.push(mask);
  }
  return masks;
}

/** Build one panel, border ring, or actual-glyph obstacle entry for an element. */
export function elementObstacleEntry(element, hooks = {}) {
  const getRect = hooks.getRect || ((el) => el.getBoundingClientRect());
  const getStyle = hooks.getStyle || ((el) => getComputedStyle(el));
  const style = getStyle(element);
  if (!style) return null;
  const display = styleValue(style, 'display', 'display');
  const visibility = styleValue(style, 'visibility', 'visibility');
  if (display === 'none' || visibility === 'hidden' || visibility === 'collapse') return null;
  const rect = parseRect(getRect(element));
  if (!rect || rect.width < 2 || rect.height < 2) return null;
  const ancestorAlpha = hooks.ancestorAlpha != null ? hooks.ancestorAlpha : ancestorOpacityProduct(element, getStyle);
  const ownOpacity = alphaValue(styleValue(style, 'opacity', 'opacity'));
  const composedOpacity = ownOpacity * clamp(Number(ancestorAlpha) || 0, 0, 1);
  if (composedOpacity <= PAINTED_ALPHA_THRESHOLD) return null;
  const clip = hooks.clippingRect ? hooks.clippingRect(element) : clippingRect(element, getRect, getStyle);
  if (clip === false) return null;
  const box = clip ? intersectRect(rect, clip) : rect;
  if (!box) return null;
  const surfaceAlpha = backgroundPaintAlpha(style) * composedOpacity;
  const border = borderPaint(style);
  const borderAlpha = border.alpha * composedOpacity;
  const hasCanvasPaint = hooks.hasCanvasPaint ? hooks.hasCanvasPaint(element) : element && element.tagName === 'CANVAS';
  if (surfaceAlpha > PAINTED_ALPHA_THRESHOLD || hasCanvasPaint) {
    return {
      type: 'panel', left: box.left, top: box.top, right: box.right, bottom: box.bottom,
      width: box.width, height: box.height, radii: cornerRadii(style, box),
      maskAlpha: Math.round(clamp(hasCanvasPaint ? composedOpacity : surfaceAlpha, 0, 1) * 255),
    };
  }
  const masks = elementHasTextPaint(element)
    ? textMasksForElement(element, getStyle, clip, hooks.maskCache || null)
    : [];
  if (borderAlpha <= PAINTED_ALPHA_THRESHOLD && !masks.length) return null;
  if (borderAlpha <= PAINTED_ALPHA_THRESHOLD) {
    let bounds = null;
    for (const mask of masks) bounds = unionRect(bounds, mask);
    return {
      type: 'glyph', left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom,
      width: bounds.width, height: bounds.height, masks,
      maskAlpha: Math.max(...masks.map((mask) => mask.alpha)),
    };
  }
  const maskBounds = masks.reduce((bounds, mask) => unionRect(bounds, mask), null);
  const bounds = unionRect(box, maskBounds);
  return {
    type: masks.length ? 'border-glyph' : 'border',
    left: bounds.left, top: bounds.top, right: bounds.right, bottom: bounds.bottom,
    width: bounds.width, height: bounds.height,
    border: {
      left: box.left, top: box.top, right: box.right, bottom: box.bottom,
      radii: cornerRadii(style, box), widths: border.widths,
      alpha: Math.round(clamp(borderAlpha, 0, 1) * 255),
    },
    masks,
    maskAlpha: Math.max(Math.round(clamp(borderAlpha, 0, 1) * 255), ...masks.map((mask) => mask.alpha)),
  };
}

function circleTouchesRect(x, y, radius, entry) {
  return x + radius > entry.left && x - radius < entry.right && y + radius > entry.top && y - radius < entry.bottom;
}

function circleTouchesRoundedPanel(entry, x, y, radius) {
  if (!circleTouchesRect(x, y, radius, entry)) return false;
  const radii = entry.radii || { tl: 0, tr: 0, br: 0, bl: 0 };
  const corner = x < entry.left + radii.tl && y < entry.top + radii.tl ? [entry.left + radii.tl, entry.top + radii.tl, radii.tl]
    : x > entry.right - radii.tr && y < entry.top + radii.tr ? [entry.right - radii.tr, entry.top + radii.tr, radii.tr]
      : x > entry.right - radii.br && y > entry.bottom - radii.br ? [entry.right - radii.br, entry.bottom - radii.br, radii.br]
        : x < entry.left + radii.bl && y > entry.bottom - radii.bl ? [entry.left + radii.bl, entry.bottom - radii.bl, radii.bl]
          : null;
  if (!corner || corner[2] <= 0) return true;
  const dx = x - corner[0];
  const dy = y - corner[1];
  return dx * dx + dy * dy <= (corner[2] + radius) ** 2;
}

function glyphTokenAt(mask, px, py) {
  const x = mask.left + (px + .5) / mask.scale;
  const y = mask.top + (py + .5) / mask.scale;
  const covering = mask.tokens.find((token) => x >= token.rect.left && x <= token.rect.right && y >= token.rect.top && y <= token.rect.bottom);
  if (covering) return covering;
  // Antialiasing can put a painted edge fractionally outside a Range rect.
  // Pick the nearest bounded token rather than falling back to a word box.
  let nearest = null;
  let distance = Infinity;
  for (const token of mask.tokens) {
    const dx = Math.max(token.rect.left - x, 0, x - token.rect.right);
    const dy = Math.max(token.rect.top - y, 0, y - token.rect.bottom);
    const next = dx * dx + dy * dy;
    if (next < distance) {
      nearest = token;
      distance = next;
    }
  }
  return nearest;
}

function glyphMaskCollision(mask, x, y, radius) {
  if (!circleTouchesRect(x, y, radius, mask)) return null;
  const centerX = (x - mask.left) * mask.scale;
  const centerY = (y - mask.top) * mask.scale;
  const probeRadius = Math.max(0, radius * mask.scale);
  // Junction probes a bounded neighborhood. Fixed center/cardinal/diagonal
  // samples keep collision work constant even when a glyph is very large.
  const probes = probeRadius > 0
    ? [[0, 0], [1, 0], [-1, 0], [0, 1], [0, -1], [.707, .707], [.707, -.707], [-.707, .707], [-.707, -.707]]
    : [[0, 0]];
  for (const [dx, dy] of probes) {
    const px = Math.floor(centerX + dx * probeRadius);
    const py = Math.floor(centerY + dy * probeRadius);
    if (px < 0 || py < 0 || px >= mask.width || py >= mask.height) continue;
    const pixelAlpha = mask.pixels[py * mask.width + px];
    if (pixelAlpha * mask.alpha <= 24 * 255) continue;
    const token = glyphTokenAt(mask, px, py);
    if (token) return { token, maskAlpha: Math.round(pixelAlpha * mask.alpha / 255) };
  }
  return null;
}

function glyphCollision(entry, x, y, radius) {
  for (const mask of entry.masks || []) {
    const hit = glyphMaskCollision(mask, x, y, radius);
    if (!hit) continue;
    const { rect } = hit.token;
    return {
      type: 'glyph', left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom,
      width: rect.width, height: rect.height, maskAlpha: hit.maskAlpha,
    };
  }
  return null;
}

function circleTouchesBorder(entry, x, y, radius) {
  const border = entry.border;
  if (!border || border.alpha <= 24 || !circleTouchesRect(x, y, radius, border)) return false;
  const { widths, radii } = border;
  const top = { left: border.left + radii.tl, right: border.right - radii.tr, top: border.top, bottom: border.top + widths.top };
  const right = { left: border.right - widths.right, right: border.right, top: border.top + radii.tr, bottom: border.bottom - radii.br };
  const bottom = { left: border.left + radii.bl, right: border.right - radii.br, top: border.bottom - widths.bottom, bottom: border.bottom };
  const left = { left: border.left, right: border.left + widths.left, top: border.top + radii.tl, bottom: border.bottom - radii.bl };
  if ([top, right, bottom, left].some((segment) => segment.right > segment.left && segment.bottom > segment.top && circleTouchesRect(x, y, radius, segment))) return true;
  const corners = [
    [border.left + radii.tl, border.top + radii.tl, radii.tl, Math.max(widths.top, widths.left), -1, -1],
    [border.right - radii.tr, border.top + radii.tr, radii.tr, Math.max(widths.top, widths.right), 1, -1],
    [border.right - radii.br, border.bottom - radii.br, radii.br, Math.max(widths.bottom, widths.right), 1, 1],
    [border.left + radii.bl, border.bottom - radii.bl, radii.bl, Math.max(widths.bottom, widths.left), -1, 1],
  ];
  return corners.some(([cx, cy, outer, width, horizontal, vertical]) => {
    if (outer <= 0 || width <= 0) return false;
    // Each annulus belongs only to its CSS corner. The radius tolerance admits
    // a drop whose edge reaches that arc without creating an interior ghost.
    if ((horizontal < 0 && x > cx + radius) || (horizontal > 0 && x < cx - radius)
      || (vertical < 0 && y > cy + radius) || (vertical > 0 && y < cy - radius)) return false;
    const distance = Math.hypot(x - cx, y - cy);
    return distance <= outer + radius && distance >= Math.max(0, outer - width - radius);
  });
}

/**
 * Return the exact painted collision surface, or null. Glyph hits resolve to
 * their Range-backed letter rectangle; panels retain their own surface.
 */
export function obstacleCollision(entry, x, y, radius = 0) {
  if (!entry || entry.maskAlpha <= 24 || !circleTouchesRect(x, y, radius, entry)) return null;
  if (entry.type === 'glyph') return glyphCollision(entry, x, y, radius);
  if (entry.type === 'border') return circleTouchesBorder(entry, x, y, radius) ? entry : null;
  if (entry.type === 'border-glyph') return circleTouchesBorder(entry, x, y, radius)
    ? entry : glyphCollision(entry, x, y, radius);
  return circleTouchesRoundedPanel(entry, x, y, radius) ? entry : null;
}

/** Boolean convenience for callers that only need collision eligibility. */
export function hitObstacle(entry, x, y, radius = 0) {
  return obstacleCollision(entry, x, y, radius) !== null;
}

/** Create a bounded cached obstacle field over a selector list. */
export function createObstacleField(options = {}) {
  const {
    root = typeof document !== 'undefined' ? document : null,
    selectors = DEFAULT_SELECTORS,
    minAlpha = PAINTED_ALPHA_THRESHOLD,
    cacheMs = 240,
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
  const observers = [];
  const maskCache = new Map();
  const moving = new Map();
  const observedElements = new Set();
  const resizeObserver = win?.ResizeObserver ? new win.ResizeObserver(invalidate) : null;
  if (resizeObserver) observers.push(resizeObserver);

  function attach(target, type, handler) {
    if (!target || typeof target.addEventListener !== 'function') return;
    target.addEventListener(type, handler, { passive: true, capture: type === 'scroll' });
    listeners.push([target, type, handler]);
  }

  function invalidate() {
    if (!disposed) dirty = true;
  }

  function sampleElements() {
    if (!root || typeof root.querySelectorAll !== 'function') return [];
    const seen = new Set();
    const entries = [];
    for (const selector of selectors) {
      if (entries.length >= MAX_FIELD_ENTRIES) break;
      let matches = [];
      try { matches = root.querySelectorAll(selector); } catch (_) { continue; }
      matches.forEach((element) => {
        if (!element || seen.has(element) || entries.length >= MAX_FIELD_ENTRIES) return;
        seen.add(element);
        if (resizeObserver && !observedElements.has(element)) {
          resizeObserver.observe(element);
          observedElements.add(element);
        }
        const entry = elementObstacleEntry(element, {
          getRect: getRect || undefined,
          getStyle: getStyle || undefined,
          maskCache,
        });
        if (entry && entry.maskAlpha > Math.round(minAlpha * 255)) entries.push(entry);
      });
    }
    for (const element of observedElements) {
      if (!seen.has(element)) {
        resizeObserver?.unobserve(element);
        observedElements.delete(element);
        moving.delete(element);
      }
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
    // An unchanged field keeps both glyph bitmap and Range geometry. Relevant
    // DOM/layout/font/scroll changes below invalidate explicitly.
    if (dirty || moving.size) refresh();
    return obstacles;
  }

  attach(win, 'scroll', invalidate);
  attach(win, 'resize', invalidate);
  attach(root, 'scroll', invalidate);
  if (win && typeof win.MutationObserver === 'function' && root) {
    try {
      const selector = selectors.join(',');
      const observer = new win.MutationObserver(records => {
        const matchesField = node => {
          const element = node?.nodeType === 1 ? node : node?.parentElement;
          if (!element || /^(?:CANVAS|SCRIPT|STYLE)$/.test(element.tagName)) return false;
          return !!element.closest?.(selector) || !!element.querySelector?.(selector);
        };
        if (records.some(record => observedElements.has(record.target) || matchesField(record.target)
            || [...record.addedNodes || [], ...record.removedNodes || []].some(matchesField))) invalidate();
      });
      observer.observe(root.documentElement || root, { childList: true, characterData: true, subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden', 'aria-hidden'] });
      observers.push(observer);
      // CSS transitions move existing geometry without child/text mutations.
      // Follow only a bounded active transition; no idle periodic refresh.
      const transition = event => {
        if (!/^(?:transform|translate|left|right|top|bottom|width|height|margin.*|padding.*|opacity|visibility|color|background-color|border.*color)$/.test(event.propertyName || '')) return;
        if (event.target?.closest?.(selector) || event.target?.querySelector?.(selector)) {
          let properties = moving.get(event.target);
          if (event.type === 'transitionrun') {
            if (!properties) moving.set(event.target, properties = new Set());
            properties.add(event.propertyName);
          } else if (properties) {
            properties.delete(event.propertyName);
            if (!properties.size) moving.delete(event.target);
          }
          invalidate();
        }
      };
      attach(root, 'transitionrun', transition);
      attach(root, 'transitionend', transition);
      attach(root, 'transitioncancel', transition);
      // Welcome/message entrance animations and fading painted surfaces also
      // affect collision geometry/alpha between mutation events.
      const animation = event => {
        if (!event.target?.closest?.(selector) && !event.target?.querySelector?.(selector)) return;
        const key = `animation:${event.animationName}`;
        let properties = moving.get(event.target);
        if (event.type === 'animationstart') {
          if (!properties) moving.set(event.target, properties = new Set());
          properties.add(key);
        } else if (properties) {
          properties.delete(key);
          if (!properties.size) moving.delete(event.target);
        }
        invalidate();
      };
      attach(root, 'animationstart', animation);
      attach(root, 'animationend', animation);
      attach(root, 'animationcancel', animation);
    } catch (_) { /* optional live invalidation */ }
  }
  const fonts = root && root.fonts;
  if (fonts && typeof fonts.addEventListener === 'function') {
    fonts.addEventListener('loadingdone', invalidate);
    listeners.push([fonts, 'loadingdone', invalidate]);
  }

  return {
    get obstacles() { return current(); },
    get size() { return current().length; },
    invalidate,
    refresh,
    hitTest(x, y, radius = 0) {
      return current().find((entry) => hitObstacle(entry, x, y, radius)) || null;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const [target, type, handler] of listeners) {
        try { target.removeEventListener(type, handler, { capture: type === 'scroll' }); } catch (_) { /* gone */ }
      }
      for (const observer of observers) observer.disconnect();
      listeners.length = 0;
      observers.length = 0;
      obstacles = [];
      maskCache.clear();
      observedElements.clear();
      moving.clear();
    },
  };
}

export { DEFAULT_SELECTORS as DEFAULT_OBSTACLE_SELECTORS };
