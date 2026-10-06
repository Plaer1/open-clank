/**
 * WebGL2 accelerated graphics backend.
 *
 * Implements the same adapter surface as the Canvas2D fallback so scene code
 * never branches on backend type. All draw calls accept CPU-side arrays; the
 * backend owns only GL objects (programs, buffers, textures) and is safe to
 * drop at any time without touching scene state.
 *
 * Glyphs rasterize through a bounded 2D atlas. No synchronous GPU readbacks
 * occur per frame — the only CPU/GPU traffic is buffer/texture uploads.
 */

import { createGlyphAtlas, createBoundedCache } from './resources.js';

const FLOATS_PER_VERTEX = 6; // x, y, r, g, b, a
const MAX_VERTICES = 4096;

const COLOR_VS = `#version 300 es
layout(location=0) in vec2 aPos;
layout(location=1) in vec4 aColor;
uniform vec2 uResolution;
out vec4 vColor;
void main() {
  vec2 clip = (aPos / uResolution) * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  vColor = aColor;
}`;

const COLOR_FS = `#version 300 es
precision mediump float;
in vec4 vColor;
out vec4 outColor;
void main() {
  outColor = vColor;
}`;

const TEXTURE_VS = `#version 300 es
layout(location=0) in vec2 aPos;
layout(location=1) in vec2 aUv;
layout(location=2) in vec4 aColor;
uniform vec2 uResolution;
out vec2 vUv;
out vec4 vColor;
void main() {
  vec2 clip = (aPos / uResolution) * 2.0 - 1.0;
  gl_Position = vec4(clip.x, -clip.y, 0.0, 1.0);
  vUv = aUv;
  vColor = aColor;
}`;

const TEXTURE_FS = `#version 300 es
precision mediump float;
in vec2 vUv;
in vec4 vColor;
uniform sampler2D uTexture;
out vec4 outColor;
void main() {
  vec4 tex = texture(uTexture, vUv);
  outColor = tex * vColor;
}`;

function compileProgram(gl, vsSource, fsSource) {
  const compile = (type, source) => {
    const shader = gl.createShader(type);
    if (!shader) return null;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
      gl.deleteShader(shader);
      return null;
    }
    return shader;
  };
  const vs = compile(gl.VERTEX_SHADER, vsSource);
  const fs = compile(gl.FRAGMENT_SHADER, fsSource);
  if (!vs || !fs) {
    if (vs) gl.deleteShader(vs);
    if (fs) gl.deleteShader(fs);
    return null;
  }
  const program = gl.createProgram();
  if (!program) {
    gl.deleteShader(vs);
    gl.deleteShader(fs);
    return null;
  }
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  gl.deleteShader(vs);
  gl.deleteShader(fs);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    gl.deleteProgram(program);
    return null;
  }
  return program;
}

function hexToRgb(color, fallback = [1, 1, 1]) {
  if (typeof color !== 'string') return fallback;
  const value = color.trim();
  if (value.startsWith('#')) {
    const hex = value.slice(1);
    const full = hex.length === 3
      ? hex.split('').map((ch) => ch + ch).join('')
      : hex.slice(0, 6);
    if (full.length !== 6) return fallback;
    const n = parseInt(full, 16);
    if (!Number.isFinite(n)) return fallback;
    return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255];
  }
  const rgba = value.match(/rgba?\(([^)]+)\)/i);
  if (rgba) {
    const parts = rgba[1].split(',').map((part) => Number.parseFloat(part.trim()));
    if (parts.length >= 3 && parts.every((part) => Number.isFinite(part))) {
      return [parts[0] / 255, parts[1] / 255, parts[2] / 255];
    }
  }
  return fallback;
}

function clamp01(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return 1;
  return Math.max(0, Math.min(1, n));
}

// Emoji rain uses native-color glyphs. Treat a complete, short emoji sequence
// as one atlas sprite regardless of its requested CSS size; the quad's existing
// metric scale restores that size without multiplying cache entries by buckets.
const NATIVE_EMOJI_PATTERN = (() => {
  try {
    return new RegExp('^(?:\\p{Extended_Pictographic}(?:\\uFE0E|\\uFE0F|\\p{Emoji_Modifier}|\\u20E3|\\u200D\\p{Extended_Pictographic})*|(?:\\p{Regional_Indicator}){2}|[0-9#*]\\uFE0F?\\u20E3)$', 'u');
  } catch (_) {
    return /^(?:[\u{1F000}-\u{1FAFF}]|[\u{1F1E6}-\u{1F1FF}]{2}|[0-9#*]\uFE0F?\u20E3)$/u;
  }
})();

function isNativeEmojiGlyph(value) {
  const text = String(value == null ? '' : value);
  return text.length > 0 && text.length <= 32 && NATIVE_EMOJI_PATTERN.test(text);
}

/**
 * Create the WebGL2 backend. Throws when a usable WebGL2 context cannot be
 * created so callers can fall back to the ordinary Canvas2D backend.
 */
export function createWebGL2Backend({ canvas, limits = {}, gl: injectedGl = null } = {}) {
  if (!canvas) throw new Error('webgl2 backend requires a canvas');
  const gl = injectedGl || canvas.getContext('webgl2', {
    alpha: true,
    antialias: false,
    depth: false,
    stencil: false,
    premultipliedAlpha: true,
    preserveDrawingBuffer: false,
    powerPreference: 'low-power',
    failIfMajorPerformanceCaveat: false,
  });
  if (!gl) throw new Error('webgl2 context unavailable');

  const colorProgram = compileProgram(gl, COLOR_VS, COLOR_FS);
  const textureProgram = compileProgram(gl, TEXTURE_VS, TEXTURE_FS);
  if (!colorProgram || !textureProgram) {
    if (colorProgram) gl.deleteProgram(colorProgram);
    if (textureProgram) gl.deleteProgram(textureProgram);
    throw new Error('webgl2 shaders unavailable');
  }

  const colorUResolution = gl.getUniformLocation(colorProgram, 'uResolution');
  const textureUResolution = gl.getUniformLocation(textureProgram, 'uResolution');
  const textureUTexture = gl.getUniformLocation(textureProgram, 'uTexture');

  const vertexData = new Float32Array(MAX_VERTICES * FLOATS_PER_VERTEX);
  const indexData = new Uint16Array(MAX_VERTICES * 3);
  let vertexCount = 0;
  let indexCount = 0;

  const colorVao = gl.createVertexArray();
  const colorBuffer = gl.createBuffer();
  const colorIndexBuffer = gl.createBuffer();
  gl.bindVertexArray(colorVao);
  gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, vertexData.byteLength, gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, FLOATS_PER_VERTEX * 4, 0);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 4, gl.FLOAT, false, FLOATS_PER_VERTEX * 4, 8);
  // ELEMENT_ARRAY_BUFFER binding is VAO state in WebGL2. Keep a distinct
  // index buffer with the color VAO so drawElements has valid element data.
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, colorIndexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indexData.byteLength, gl.DYNAMIC_DRAW);
  gl.bindVertexArray(null);

  const textureVao = gl.createVertexArray();
  const textureBuffer = gl.createBuffer();
  const textureIndexBuffer = gl.createBuffer();
  // Texture path uses interleaved x,y,u,v,r,g,b,a (8 floats).
  const textureStride = 8 * 4;
  const textureData = new Float32Array(MAX_VERTICES * 8);
  const textureIndexData = new Uint16Array(MAX_VERTICES * 3);
  let textureVertexCount = 0;
  let textureIndexCount = 0;
  gl.bindVertexArray(textureVao);
  gl.bindBuffer(gl.ARRAY_BUFFER, textureBuffer);
  gl.bufferData(gl.ARRAY_BUFFER, MAX_VERTICES * 8 * 4, gl.DYNAMIC_DRAW);
  gl.enableVertexAttribArray(0);
  gl.vertexAttribPointer(0, 2, gl.FLOAT, false, textureStride, 0);
  gl.enableVertexAttribArray(1);
  gl.vertexAttribPointer(1, 2, gl.FLOAT, false, textureStride, 8);
  gl.enableVertexAttribArray(2);
  gl.vertexAttribPointer(2, 4, gl.FLOAT, false, textureStride, 16);
  // Textured glyph/image draws need their own VAO-associated element buffer.
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, textureIndexBuffer);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, textureIndexData.byteLength, gl.DYNAMIC_DRAW);
  gl.bindVertexArray(null);

  let width = canvas.width || 1;
  let height = canvas.height || 1;
  let logicalWidth = width;
  let logicalHeight = height;
  let disposed = false;
  let lost = false;
  let currentTexture = null;
  let atlasCanvas = null;
  let atlasCtx = null;
  let atlasTexture = null;
  let atlasCursorX = 0;
  let atlasCursorY = 0;
  let atlasRowHeight = 0;
  let atlasDirty = false;

  const glyphAtlas = createGlyphAtlas({
    maxEntries: limits.maxGlyphCacheEntries || 512,
    maxPixels: 1024 * 1024,
  });
  const imageCache = createBoundedCache({
    maxEntries: Math.max(8, Math.min(256, limits.maxTextureSize ? 256 : 64)),
  });
  // Artwork shares atlas pages so painter order stays intact while random
  // sprite identities remain in one GPU submission. Pages have a4M-pixel cap.
  const imagePages = [];
  const imageAtlasSize = Math.max(256, Math.min(1024, limits.maxTextureSize || 1024));
  const maxImagePages = Math.max(1, Math.floor(4 * 1024 * 1024 / (imageAtlasSize * imageAtlasSize)));

  function releaseImagePages() {
    for (const page of imagePages) {
      gl.deleteTexture(page.texture);
      page.canvas.width = page.canvas.height = 0;
    }
    imagePages.length = 0;
  }

  function uploadImagePage(page) {
    if (!page?.dirty || !alive()) return;
    gl.bindTexture(gl.TEXTURE_2D, page.texture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, page.canvas);
    page.dirty = false;
  }

  function imageAtlasEntry(source) {
    const sw = Number(source.naturalWidth || source.width) || 0;
    const sh = Number(source.naturalHeight || source.height) || 0;
    if (!sw || !sh || sw + 2 > imageAtlasSize || sh + 2 > imageAtlasSize) return null;
    let page = imagePages.at(-1);
    if (page && page.x + sw + 2 > imageAtlasSize) { page.x = 0; page.y += page.row; page.row = 0; }
    if (!page || page.y + sh + 2 > imageAtlasSize) {
      if (imagePages.length >= maxImagePages) {
        // Draw queued old-page quads before freeing or overwriting any pixels.
        flushTexture();
        imageCache.clear();
        releaseImagePages();
        currentTexture = null;
      }
      const surface = canvas.ownerDocument.createElement('canvas');
      surface.width = surface.height = imageAtlasSize;
      const ctx = surface.getContext('2d');
      const texture = gl.createTexture();
      if (!ctx || !texture) return null;
      gl.bindTexture(gl.TEXTURE_2D, texture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      page = { canvas: surface, ctx, texture, x: 0, y: 0, row: 0, dirty: true };
      imagePages.push(page);
    }
    const x = page.x + 1;
    const y = page.y + 1;
    page.ctx.drawImage(source, x, y, sw, sh);
    page.x += sw + 2;
    page.row = Math.max(page.row, sh + 2);
    page.dirty = true;
    return { texture: page.texture, page, uv: [x / imageAtlasSize, y / imageAtlasSize,
      (x + sw) / imageAtlasSize, (y + sh) / imageAtlasSize] };
  }

  function alive() {
    return !disposed && !lost && !!gl;
  }

  function flushColor() {
    if (!alive() || indexCount === 0) return;
    gl.useProgram(colorProgram);
    gl.uniform2f(colorUResolution, logicalWidth, logicalHeight);
    gl.bindVertexArray(colorVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, colorBuffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, vertexData.subarray(0, vertexCount * FLOATS_PER_VERTEX));
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, colorIndexBuffer);
    gl.bufferSubData(gl.ELEMENT_ARRAY_BUFFER, 0, indexData.subarray(0, indexCount));
    gl.drawElements(gl.TRIANGLES, indexCount, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);
    vertexCount = 0;
    indexCount = 0;
  }

  function flushTexture() {
    if (!alive() || textureIndexCount === 0 || !currentTexture) return;
    if (currentTexture === atlasTexture) uploadDirtyAtlas();
    const imagePage = imagePages.find(page => page.texture === currentTexture);
    if (imagePage) uploadImagePage(imagePage);
    gl.useProgram(textureProgram);
    gl.uniform2f(textureUResolution, logicalWidth, logicalHeight);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, currentTexture);
    gl.uniform1i(textureUTexture, 0);
    gl.bindVertexArray(textureVao);
    gl.bindBuffer(gl.ARRAY_BUFFER, textureBuffer);
    gl.bufferSubData(gl.ARRAY_BUFFER, 0, textureData.subarray(0, textureVertexCount * 8));
    gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, textureIndexBuffer);
    gl.bufferSubData(gl.ELEMENT_ARRAY_BUFFER, 0, textureIndexData.subarray(0, textureIndexCount));
    gl.drawElements(gl.TRIANGLES, textureIndexCount, gl.UNSIGNED_SHORT, 0);
    gl.bindVertexArray(null);
    textureVertexCount = 0;
    textureIndexCount = 0;
  }

  function pushColorVertex(x, y, rgb, alpha) {
    if (vertexCount >= MAX_VERTICES) flushColor();
    const base = vertexCount * FLOATS_PER_VERTEX;
    vertexData[base] = x;
    vertexData[base + 1] = y;
    vertexData[base + 2] = rgb[0];
    vertexData[base + 3] = rgb[1];
    vertexData[base + 4] = rgb[2];
    vertexData[base + 5] = alpha;
    vertexCount += 1;
  }

  function pushColorQuad(x, y, w, h, rgb, alpha) {
    if (indexCount + 6 > indexData.length || vertexCount + 4 > MAX_VERTICES) flushColor();
    const start = vertexCount;
    pushColorVertex(x, y, rgb, alpha);
    pushColorVertex(x + w, y, rgb, alpha);
    pushColorVertex(x + w, y + h, rgb, alpha);
    pushColorVertex(x, y + h, rgb, alpha);
    indexData[indexCount++] = start;
    indexData[indexCount++] = start + 1;
    indexData[indexCount++] = start + 2;
    indexData[indexCount++] = start;
    indexData[indexCount++] = start + 2;
    indexData[indexCount++] = start + 3;
  }

  function pushColorTri(x1, y1, x2, y2, x3, y3, rgb, alpha) {
    if (indexCount + 3 > indexData.length || vertexCount + 3 > MAX_VERTICES) flushColor();
    const start = vertexCount;
    pushColorVertex(x1, y1, rgb, alpha);
    pushColorVertex(x2, y2, rgb, alpha);
    pushColorVertex(x3, y3, rgb, alpha);
    indexData[indexCount++] = start;
    indexData[indexCount++] = start + 1;
    indexData[indexCount++] = start + 2;
  }

  function ensureAtlas() {
    if (atlasCanvas) return atlasCanvas;
    const size = Math.max(256, Math.min(1024, limits.maxTextureSize || 1024));
    const source = typeof document !== 'undefined' && document.createElement
      ? document.createElement('canvas')
      : { width: size, height: size, getContext: () => null };
    source.width = size;
    source.height = size;
    atlasCanvas = source;
    atlasCtx = typeof source.getContext === 'function' ? source.getContext('2d') : null;
    if (atlasCtx) {
      atlasCtx.clearRect(0, 0, size, size);
      atlasTexture = gl.createTexture();
      gl.bindTexture(gl.TEXTURE_2D, atlasTexture);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, atlasCanvas);
    }
    return atlasCanvas;
  }

  function uploadDirtyAtlas() {
    if (!atlasDirty || !atlasTexture || !atlasCanvas || !alive()) return;
    gl.bindTexture(gl.TEXTURE_2D, atlasTexture);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, atlasCanvas);
    atlasDirty = false;
  }

  function rasterizeGlyph(glyph, rasterSize = glyph.size) {
    ensureAtlas();
    if (!atlasCtx || !atlasTexture) return null;
    const bucketSize = glyphAtlas.quantizeSize(rasterSize || 12);
    const weight = glyph.weight || 400;
    const family = glyph.font || 'sans-serif';
    const text = String(glyph.text);
    const font = `${weight} ${bucketSize}px ${family}`;
    atlasCtx.font = font;
    atlasCtx.textAlign = 'left';
    atlasCtx.textBaseline = 'alphabetic';
    const metrics = atlasCtx.measureText(text);
    const metric = (name, fallback) => {
      const value = Number(metrics[name]);
      return Number.isFinite(value) && value >= 0 ? value : fallback;
    };
    const advance = metric('width', bucketSize * 0.6 * text.length);
    const inkLeft = metric('actualBoundingBoxLeft', 0);
    const inkRight = metric('actualBoundingBoxRight', Math.max(advance, bucketSize * 0.6 * text.length));
    const fontAscent = metric('fontBoundingBoxAscent', metric('actualBoundingBoxAscent', bucketSize * 0.8));
    const fontDescent = metric('fontBoundingBoxDescent', metric('actualBoundingBoxDescent', bucketSize * 0.2));
    const inkAscent = metric('actualBoundingBoxAscent', fontAscent);
    const inkDescent = metric('actualBoundingBoxDescent', fontDescent);
    const pad = 1;
    // Atlas bounds are actual ink, not an arbitrary 1.3× em square. Keep
    // offset/advance metrics so the scaled quad can reconstruct Canvas2D's
    // left/alphabetic anchor, overhangs, and descenders at any request size.
    const w = Math.max(1, Math.ceil(inkLeft + inkRight) + pad * 2);
    const h = Math.max(1, Math.ceil(inkAscent + inkDescent) + pad * 2);
    if (atlasCursorX + w + pad * 2 > atlasCanvas.width) {
      atlasCursorX = 0;
      atlasCursorY += atlasRowHeight + pad;
      atlasRowHeight = 0;
    }
    if (atlasCursorY + h + pad * 2 > atlasCanvas.height) {
      // Queued quads still reference the old atlas. Upload and draw them
      // before clearing the backing canvas or invalidating cached UVs.
      flushTexture();
      atlasCtx.clearRect(0, 0, atlasCanvas.width, atlasCanvas.height);
      atlasCursorX = 0;
      atlasCursorY = 0;
      atlasRowHeight = 0;
      glyphAtlas.clear();
      atlasDirty = true;
    }
    const x = atlasCursorX + pad;
    const y = atlasCursorY + pad;
    atlasCtx.clearRect(x, y, w, h);
    atlasCtx.fillStyle = '#ffffff';
    atlasCtx.fillText(text, x + pad + inkLeft, y + pad + inkAscent);
    atlasCursorX += w + pad * 2;
    atlasRowHeight = Math.max(atlasRowHeight, h + pad * 2);
    // Defer the full atlas upload until its queued quads flush. UVs address
    // the glyph rect, and batching avoids one full texture upload per glyph.
    atlasDirty = true;
    return {
      u0: x / atlasCanvas.width,
      v0: y / atlasCanvas.height,
      u1: (x + w) / atlasCanvas.width,
      v1: (y + h) / atlasCanvas.height,
      w,
      h,
      bucketSize,
      advance,
      inkLeft,
      inkAscent,
      fontAscent,
      fontDescent,
      pad,
    };
  }

  function pushTexturedQuad(texture, x, y, w, h, u0, v0, u1, v1, rgb, alpha) {
    if (textureVertexCount + 4 > MAX_VERTICES || textureIndexCount + 6 > textureIndexData.length) {
      flushTexture();
    }
    if (currentTexture !== texture) {
      flushTexture();
      currentTexture = texture;
    }
    const start = textureVertexCount;
    const corners = [
      [x, y, u0, v0],
      [x + w, y, u1, v0],
      [x + w, y + h, u1, v1],
      [x, y + h, u0, v1],
    ];
    for (const [cx, cy, u, v] of corners) {
      const base = textureVertexCount * 8;
      textureData[base] = cx;
      textureData[base + 1] = cy;
      textureData[base + 2] = u;
      textureData[base + 3] = v;
      textureData[base + 4] = rgb[0];
      textureData[base + 5] = rgb[1];
      textureData[base + 6] = rgb[2];
      textureData[base + 7] = alpha;
      textureVertexCount += 1;
    }
    textureIndexData[textureIndexCount++] = start;
    textureIndexData[textureIndexCount++] = start + 1;
    textureIndexData[textureIndexCount++] = start + 2;
    textureIndexData[textureIndexCount++] = start;
    textureIndexData[textureIndexCount++] = start + 2;
    textureIndexData[textureIndexCount++] = start + 3;
  }

  return {
    kind: 'webgl2',
    accelerated: true,
    limits: limits || {},
    get width() { return width; },
    get height() { return height; },
    get disposed() { return disposed; },
    get contextLost() { return lost; },

    markContextLost() {
      lost = true;
    },

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
      // The viewport is backing pixels; CSS-space geometry is transformed by
      // the shader resolution so DPR/clamping never changes its visible extent.
      gl.viewport(0, 0, width, height);
    },

    beginFrame() {
      if (!alive()) return;
      gl.disable(gl.DEPTH_TEST);
      gl.enable(gl.BLEND);
      gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      vertexCount = 0;
      indexCount = 0;
      textureVertexCount = 0;
      textureIndexCount = 0;
      currentTexture = null;
    },

    clear(r = 0, g = 0, b = 0, a = 0) {
      if (!alive()) return;
      flushColor();
      flushTexture();
      gl.clearColor(r, g, b, a);
      gl.clear(gl.COLOR_BUFFER_BIT);
    },

    setAlpha(alpha) {
      // Multiplied into each primitive's alpha at push time by consumers;
      // retained for API parity with the Canvas2D backend.
      this.__globalAlpha = clamp01(alpha);
    },

    setComposite(operation) {
      if (!alive()) return;
      flushColor();
      flushTexture();
      const mode = operation || 'source-over';
      if (mode === 'lighter') {
        gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      } else if (mode === 'destination-out') {
        gl.blendFuncSeparate(gl.ZERO, gl.ONE_MINUS_SRC_ALPHA, gl.ZERO, gl.ONE_MINUS_SRC_ALPHA);
      } else {
        gl.blendFuncSeparate(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA, gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
      }
    },

    drawRects(rects) {
      if (!alive() || !rects || !rects.length) return;
      flushTexture();
      const globalAlpha = clamp01(this.__globalAlpha == null ? 1 : this.__globalAlpha);
      for (const rect of rects) {
        if (!rect) continue;
        const rgb = hexToRgb(rect.color);
        pushColorQuad(rect.x, rect.y, rect.w, rect.h, rgb, clamp01(rect.alpha == null ? 1 : rect.alpha) * globalAlpha);
      }
    },

    drawLines(lines) {
      if (!alive() || !lines || !lines.length) return;
      flushTexture();
      const globalAlpha = clamp01(this.__globalAlpha == null ? 1 : this.__globalAlpha);
      for (const line of lines) {
        if (!line) continue;
        const rgb = hexToRgb(line.color);
        const alpha = clamp01(line.alpha == null ? 1 : line.alpha) * globalAlpha;
        const half = (line.width == null ? 1 : line.width) * 0.5;
        const dx = line.x2 - line.x1;
        const dy = line.y2 - line.y1;
        const length = Math.hypot(dx, dy) || 1;
        const nx = (-dy / length) * half;
        const ny = (dx / length) * half;
        if (indexCount + 6 > indexData.length || vertexCount + 4 > MAX_VERTICES) flushColor();
        const start = vertexCount;
        pushColorVertex(line.x1 + nx, line.y1 + ny, rgb, alpha);
        pushColorVertex(line.x2 + nx, line.y2 + ny, rgb, alpha);
        pushColorVertex(line.x2 - nx, line.y2 - ny, rgb, alpha);
        pushColorVertex(line.x1 - nx, line.y1 - ny, rgb, alpha);
        indexData[indexCount++] = start;
        indexData[indexCount++] = start + 1;
        indexData[indexCount++] = start + 2;
        indexData[indexCount++] = start;
        indexData[indexCount++] = start + 2;
        indexData[indexCount++] = start + 3;
      }
    },

    drawCircles(circles) {
      if (!alive() || !circles || !circles.length) return;
      flushTexture();
      const globalAlpha = clamp01(this.__globalAlpha == null ? 1 : this.__globalAlpha);
      for (const circle of circles) {
        if (!circle) continue;
        const rgb = hexToRgb(circle.color);
        const alpha = clamp01(circle.alpha == null ? 1 : circle.alpha) * globalAlpha;
        const r = Math.max(0, circle.r || 0);
        const segments = Math.max(8, Math.min(32, Math.ceil(r * 1.5) + 8));
        const start = vertexCount;
        pushColorVertex(circle.x, circle.y, rgb, alpha);
        for (let i = 0; i <= segments; i += 1) {
          const angle = (i / segments) * Math.PI * 2;
          pushColorVertex(
            circle.x + Math.cos(angle) * r,
            circle.y + Math.sin(angle) * r,
            rgb,
            alpha,
          );
        }
        for (let i = 0; i < segments; i += 1) {
          if (indexCount + 3 > indexData.length) break;
          indexData[indexCount++] = start;
          indexData[indexCount++] = start + 1 + i;
          indexData[indexCount++] = start + 2 + i;
        }
        if (circle.stroke) {
          const strokeRgb = hexToRgb(circle.stroke);
          const strokeAlpha = clamp01(circle.alpha == null ? 1 : circle.alpha) * globalAlpha;
          const strokeHalf = (circle.strokeWidth == null ? 1 : circle.strokeWidth) * 0.5;
          const inner = Math.max(0, r - strokeHalf);
          const outer = r + strokeHalf;
          for (let i = 0; i < segments; i += 1) {
            const a0 = (i / segments) * Math.PI * 2;
            const a1 = ((i + 1) / segments) * Math.PI * 2;
            pushColorTri(
              circle.x + Math.cos(a0) * inner, circle.y + Math.sin(a0) * inner,
              circle.x + Math.cos(a0) * outer, circle.y + Math.sin(a0) * outer,
              circle.x + Math.cos(a1) * outer, circle.y + Math.sin(a1) * outer,
              strokeRgb, strokeAlpha,
            );
            pushColorTri(
              circle.x + Math.cos(a0) * inner, circle.y + Math.sin(a0) * inner,
              circle.x + Math.cos(a1) * outer, circle.y + Math.sin(a1) * outer,
              circle.x + Math.cos(a1) * inner, circle.y + Math.sin(a1) * inner,
              strokeRgb, strokeAlpha,
            );
          }
        }
      }
    },

    drawGlyphs(glyphs) {
      if (!alive() || !glyphs || !glyphs.length) return;
      flushColor();
      const globalAlpha = clamp01(this.__globalAlpha == null ? 1 : this.__globalAlpha);
      ensureAtlas();
      for (const glyph of glyphs) {
        if (!glyph || glyph.text == null) continue;
        const emoji = isNativeEmojiGlyph(glyph.text);
        const rasterSize = emoji ? 24 : glyph.size;
        const key = glyphAtlas.key(glyph.text, rasterSize, glyph.weight, glyph.font);
        let entry = glyphAtlas.get(key);
        if (!entry || !entry.uv) {
          const uv = rasterizeGlyph(glyph, rasterSize);
          if (!uv) continue;
          entry = glyphAtlas.set(key, { uv }, uv.w * uv.h);
        }
        const rgb = hexToRgb(glyph.color);
        const alpha = clamp01(glyph.alpha == null ? 1 : glyph.alpha) * globalAlpha;
        const { uv } = entry;
        const requestedSize = Math.max(1, Number(glyph.size) || uv.bucketSize);
        const scale = requestedSize / uv.bucketSize;
        const advance = uv.advance * scale;
        let anchorX = glyph.x;
        if (glyph.align === 'center') anchorX -= advance / 2;
        else if (glyph.align === 'right' || glyph.align === 'end') anchorX -= advance;
        // Canvas2D positions text by an anchor point, while atlas UVs cover
        // ink plus padding. Reconstruct that anchor using measured font/ink
        // metrics, then scale the bucket raster to this glyph's requested size.
        let baselineY = glyph.y;
        if (glyph.baseline === 'top') baselineY += uv.fontAscent * scale;
        else if (glyph.baseline === 'middle') baselineY += (uv.fontAscent - uv.fontDescent) * scale / 2;
        else if (glyph.baseline === 'bottom') baselineY -= uv.fontDescent * scale;
        else if (glyph.baseline === 'hanging') baselineY += uv.fontAscent * scale * 0.8;
        else if (glyph.baseline === 'ideographic') baselineY -= uv.fontDescent * scale;
        const quadW = uv.w * scale;
        const quadH = uv.h * scale;
        const x = anchorX - (uv.inkLeft + uv.pad) * scale;
        const y = baselineY - (uv.inkAscent + uv.pad) * scale;
        // Head-glyph glow. The batched atlas path has no shadowBlur, so the
        // pre-S24 leading-glyph halo is approximated with a slightly larger,
        // translucent copy underneath (drawn first).
        const glow = glyph.glow;
        if (glow && glow.blur > 0) {
          const glowRgb = hexToRgb(glow.color || glyph.color);
          const pad = Math.max(1, Math.round(glow.blur * 0.35));
          pushTexturedQuad(
            atlasTexture,
            x - pad,
            y - pad,
            quadW + pad * 2,
            quadH + pad * 2,
            uv.u0,
            uv.v0,
            uv.u1,
            uv.v1,
            glowRgb,
            alpha * 0.35,
          );
        }
        pushTexturedQuad(atlasTexture, x, y, quadW, quadH, uv.u0, uv.v0, uv.u1, uv.v1, rgb, alpha);
      }
    },

    drawImage(source, dx, dy, dw, dh, alpha = 1) {
      if (!alive() || !source) return;
      const requestedAlpha = Number(alpha);
      const opacity = clamp01(Number.isFinite(requestedAlpha) ? requestedAlpha : 1);
      flushColor();
      let entry = imageCache.get(source);
      if (!entry || !entry.texture) {
        entry = imageAtlasEntry(source);
        if (entry) imageCache.set(source, entry);
      }
      if (!entry || !entry.texture) {
        const texture = gl.createTexture();
        if (!texture) return;
        gl.bindTexture(gl.TEXTURE_2D, texture);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
        gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
        try {
          gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, source);
        } catch (_) {
          gl.deleteTexture(texture);
          return;
        }
        entry = { texture, dispose: () => gl.deleteTexture(texture) };
        imageCache.set(source, entry);
      }
      const sw = source.width || dw || 1;
      const sh = source.height || dh || 1;
      const w = dw == null ? sw : dw;
      const h = dh == null ? sh : dh;
      pushTexturedQuad(
        entry.texture,
        dx,
        dy,
        w,
        h,
        ...(entry.uv || [0, 0, 1, 1]),
        [1, 1, 1],
        clamp01(this.__globalAlpha == null ? 1 : this.__globalAlpha) * opacity,
      );
    },

    endFrame() {
      if (!alive()) return;
      flushColor();
      flushTexture();
    },

    dispose() {
      if (disposed) return;
      disposed = true;
      imageCache.clear();
      releaseImagePages();
      glyphAtlas.clear();
      try {
        if (colorBuffer) gl.deleteBuffer(colorBuffer);
        if (colorIndexBuffer) gl.deleteBuffer(colorIndexBuffer);
        if (textureBuffer) gl.deleteBuffer(textureBuffer);
        if (textureIndexBuffer) gl.deleteBuffer(textureIndexBuffer);
        if (colorVao) gl.deleteVertexArray(colorVao);
        if (textureVao) gl.deleteVertexArray(textureVao);
        if (colorProgram) gl.deleteProgram(colorProgram);
        if (textureProgram) gl.deleteProgram(textureProgram);
        if (atlasTexture) gl.deleteTexture(atlasTexture);
      } catch (_) {
        /* context may already be lost */
      }
      atlasTexture = null;
      atlasCanvas = null;
      atlasCtx = null;
    },
  };
}
