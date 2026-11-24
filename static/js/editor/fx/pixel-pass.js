/**
 * Apply a Brightness/Contrast, Hue/Saturation, Levels, or Color Balance
 * adjustment to a source canvas and return a fresh canvas with the
 * result. Pure pixel math — no DOM, no module state.
 *
 * Used by the editor's per-layer FX stack: each `adjLayer` calls
 * `applyAdjustment(prevCanvas, adjLayer)` and the result feeds the
 * next layer in the stack.
 *
 * Adjustment shape:
 *   { type: 'brightness-contrast', params: { brightness, contrast } }
 *   { type: 'hue-saturation',      params: { hue, saturation } }
 *   { type: 'levels',              params: { inBlack, inWhite, gamma, outBlack, outWhite } }
 *   { type: 'color-balance',       params: { shadows, midtones, highlights } }
 */
export function applyAdjustment(srcCanvas, adj) {
  const w = srcCanvas.width, h = srcCanvas.height;
  const out = document.createElement('canvas');
  out.width = w; out.height = h;
  const octx = out.getContext('2d');

  // B/C and H/S can use the fast browser-native CSS filter pipeline.
  if (adj.type === 'brightness-contrast') {
    const p = adj.params;
    octx.filter = `brightness(${p.brightness}) contrast(${p.contrast})`;
    octx.drawImage(srcCanvas, 0, 0);
    octx.filter = 'none';
    return out;
  }
  if (adj.type === 'hue-saturation') {
    const p = adj.params;
    octx.filter = `saturate(${p.saturation}) hue-rotate(${p.hue}deg)`;
    octx.drawImage(srcCanvas, 0, 0);
    octx.filter = 'none';
    return out;
  }

  // Levels + Color Balance need per-pixel math.
  octx.drawImage(srcCanvas, 0, 0);
  const img = octx.getImageData(0, 0, w, h);
  const d = img.data;

  if (adj.type === 'levels') {
    const l = adj.params;
    const inLow  = Math.max(0, Math.min(254, l.inBlack));
    const inHigh = Math.max(inLow + 1, Math.min(255, l.inWhite));
    const gamma  = Math.max(0.1, l.gamma || 1);
    const outLow  = Math.max(0, Math.min(255, l.outBlack));
    const outHigh = Math.max(outLow, Math.min(255, l.outWhite));
    const inv = 1.0 / gamma;
    const span = (outHigh - outLow);
    const lut = new Uint8ClampedArray(256);
    for (let v = 0; v < 256; v++) {
      let t = (v - inLow) / (inHigh - inLow);
      if (t < 0) t = 0; else if (t > 1) t = 1;
      t = Math.pow(t, inv);
      lut[v] = Math.round(t * span + outLow);
    }
    for (let i = 0; i < d.length; i += 4) {
      d[i] = lut[d[i]]; d[i+1] = lut[d[i+1]]; d[i+2] = lut[d[i+2]];
    }
    octx.putImageData(img, 0, 0);
    return out;
  }

  if (adj.type === 'color-balance') {
    const cb = adj.params;
    const scale = 0.6;
    const s = cb.shadows, m = cb.midtones, hi = cb.highlights;
    const sR = s.r*scale, sG = s.g*scale, sB = s.b*scale;
    const mR = m.r*scale, mG = m.g*scale, mB = m.b*scale;
    const hR = hi.r*scale, hG = hi.g*scale, hB = hi.b*scale;
    // Bell-curve tone weights so each pixel's shift is proportional to
    // how "shadow", "midtone", or "highlight" its luminance is.
    const wS = new Float32Array(256), wM = new Float32Array(256), wH = new Float32Array(256);
    const sig = 0.25;
    for (let v = 0; v < 256; v++) {
      const t = v / 255;
      wS[v] = Math.exp(-(t*t) / (2*sig*sig));
      wM[v] = Math.exp(-((t-0.5)*(t-0.5)) / (2*sig*sig));
      wH[v] = Math.exp(-((1-t)*(1-t)) / (2*sig*sig));
    }
    for (let i = 0; i < d.length; i += 4) {
      let r = d[i], g = d[i+1], b = d[i+2];
      const Y = (0.2126*r + 0.7152*g + 0.0722*b) | 0;
      const ws = wS[Y], wm = wM[Y], wh = wH[Y];
      r += sR*ws + mR*wm + hR*wh;
      g += sG*ws + mG*wm + hG*wh;
      b += sB*ws + mB*wm + hB*wh;
      d[i]   = r < 0 ? 0 : r > 255 ? 255 : r;
      d[i+1] = g < 0 ? 0 : g > 255 ? 255 : g;
      d[i+2] = b < 0 ? 0 : b > 255 ? 255 : b;
    }
    octx.putImageData(img, 0, 0);
    return out;
  }

  return out;
}


/**
 * Apply a combined Levels + Color Balance pass to a layer in-place via
 * its `layer.adjustments` field. Cached on `layer._adjCache` keyed by
 * `cacheKey` so repeated composite passes don't re-run the math.
 *
 * Returns the cached output canvas.
 *
 * @param {{
 *   canvas: HTMLCanvasElement,
 *   adjustments: object,
 *   _adjCache?: HTMLCanvasElement,
 *   _adjCacheKey?: string,
 * }} layer
 * @param {string} cacheKey  Stable signature of `layer.adjustments`.
 */
export function renderLayerPixelAdjustments(layer, cacheKey) {
  const adj = layer.adjustments;
  if (layer._adjCache && layer._adjCacheKey === cacheKey) return layer._adjCache;
  if (!layer._adjCache) {
    layer._adjCache = document.createElement('canvas');
  }
  const out = layer._adjCache;
  out.width = layer.canvas.width;
  out.height = layer.canvas.height;
  const octx = out.getContext('2d');
  octx.clearRect(0, 0, out.width, out.height);
  octx.drawImage(layer.canvas, 0, 0);
  const img = octx.getImageData(0, 0, out.width, out.height);
  const d = img.data;

  // Single 256-entry LUT for the Levels portion (applied per R/G/B
  // channel identically — luma-style isn't right when colour balance
  // follows, per-channel is fine here).
  const l = adj.levels || { inBlack: 0, inWhite: 255, gamma: 1, outBlack: 0, outWhite: 255 };
  const inLow  = Math.max(0, Math.min(254, l.inBlack));
  const inHigh = Math.max(inLow + 1, Math.min(255, l.inWhite));
  const gamma  = Math.max(0.1, l.gamma || 1);
  const outLow  = Math.max(0, Math.min(255, l.outBlack));
  const outHigh = Math.max(outLow, Math.min(255, l.outWhite));
  const inv = 1.0 / gamma;
  const span = (outHigh - outLow);
  const lut = new Uint8ClampedArray(256);
  for (let v = 0; v < 256; v++) {
    let t = (v - inLow) / (inHigh - inLow);
    if (t < 0) t = 0; else if (t > 1) t = 1;
    t = Math.pow(t, inv);
    lut[v] = Math.round(t * span + outLow);
  }

  // Color Balance bell-curve weights (see applyAdjustment).
  const cb = adj.colorBalance || { shadows: {r:0,g:0,b:0}, midtones: {r:0,g:0,b:0}, highlights: {r:0,g:0,b:0} };
  const s = cb.shadows || {r:0,g:0,b:0};
  const m = cb.midtones || {r:0,g:0,b:0};
  const h = cb.highlights || {r:0,g:0,b:0};
  const scale = 0.6;
  const sR = s.r * scale, sG = s.g * scale, sB = s.b * scale;
  const mR = m.r * scale, mG = m.g * scale, mB = m.b * scale;
  const hR = h.r * scale, hG = h.g * scale, hB = h.b * scale;

  const wS = new Float32Array(256);
  const wM = new Float32Array(256);
  const wH = new Float32Array(256);
  for (let v = 0; v < 256; v++) {
    const t = v / 255;
    const dS = t, wsig = 0.25;
    const dM = t - 0.5;
    const dH = 1 - t;
    wS[v] = Math.exp(-(dS * dS) / (2 * wsig * wsig));
    wM[v] = Math.exp(-(dM * dM) / (2 * wsig * wsig));
    wH[v] = Math.exp(-(dH * dH) / (2 * wsig * wsig));
  }

  for (let i = 0; i < d.length; i += 4) {
    let r = lut[d[i]];
    let g = lut[d[i + 1]];
    let b = lut[d[i + 2]];
    const Y = (0.2126 * r + 0.7152 * g + 0.0722 * b) | 0;
    const ws = wS[Y], wm = wM[Y], wh = wH[Y];
    r += sR * ws + mR * wm + hR * wh;
    g += sG * ws + mG * wm + hG * wh;
    b += sB * ws + mB * wm + hB * wh;
    d[i]     = r < 0 ? 0 : r > 255 ? 255 : r;
    d[i + 1] = g < 0 ? 0 : g > 255 ? 255 : g;
    d[i + 2] = b < 0 ? 0 : b > 255 ? 255 : b;
  }
  octx.putImageData(img, 0, 0);
  layer._adjCacheKey = cacheKey;
  return out;
}


/**
 * Render the committed adjustment stack, or replace the edited entry in
 * place for a view-owned preview. New previews append to the stack.
 * Committed results use the layer cache; previews use the popup cache.
 *
 * If the stack is empty AND nothing is staged, returns the layer's own
 * canvas unchanged (no allocation).
 *
 * @param {{canvas: HTMLCanvasElement, adjLayers?: Array<object>}} layer
 * @param {{staged: object, editingId: string|null, revision: number, cache: object}|null} preview
 * The optional preview and its cache are owned by the rendering view.
 * @returns {HTMLCanvasElement}
 */
export function renderLayerWithAdjLayers(layer, preview = null) {
  const editingId = preview?.editingId || null;
  const committed = layer.adjLayers || [];
  const stack = committed.map(adj => editingId && adj.id === editingId ? {...adj, params:preview.staged.params} : adj).filter(adj => adj.visible);
  if (preview?.staged && !editingId) stack.push({...preview.staged, visible:true, opacity:1});
  const signature = items => items.map(a => `${a.id || ''}:${a.visible?1:0}:${a.opacity}:${a.type}:${JSON.stringify(a.params)}`).join('|');
  const sig = `${signature(committed)}#${signature(stack)}`;
  const cache = preview?.cache;
  if (preview && cache && cache.canvas === layer.canvas && cache.width === layer.canvas.width && cache.height === layer.canvas.height && cache.revision === preview.revision && cache.signature === sig) return cache.result;
  if (!preview && layer._adjFinal && layer._adjFinalKey === sig) return layer._adjFinal;
  if (!stack.length) {
    if (!preview) layer._adjFinalKey = '';
    if (cache) Object.assign(cache,{canvas:layer.canvas,width:layer.canvas.width,height:layer.canvas.height,revision:preview.revision,signature:sig,result:layer.canvas});
    return layer.canvas;
  }
  let cur = layer.canvas;
  const w = layer.canvas.width, h = layer.canvas.height;
  for (const adj of stack) {
    const adjOut = applyAdjustment(cur, adj);
    if (adj.opacity >= 0.999) {
      cur = adjOut;
    } else {
      const blend = document.createElement('canvas');
      blend.width = w; blend.height = h;
      const bctx = blend.getContext('2d');
      bctx.drawImage(cur, 0, 0);
      bctx.globalAlpha = adj.opacity;
      bctx.drawImage(adjOut, 0, 0);
      bctx.globalAlpha = 1;
      cur = blend;
    }
  }
  if (cache) Object.assign(cache,{canvas:layer.canvas,width:layer.canvas.width,height:layer.canvas.height,revision:preview.revision,signature:sig,result:cur});
  if (!preview) { layer._adjFinal = cur; layer._adjFinalKey = sig; }
  return cur;
}
