/**
 * Graphics capability probe.
 *
 * Detects whether a WebGL2 accelerated backend can be created for a canvas,
 * queries GPU limits once at initialization, and reports the ordinary
 * Canvas2D fallback when acceleration is unavailable. Detection never throws;
 * every failure collapses to a usable fallback with a recorded reason.
 */

export const BACKEND_WEBGL2 = 'webgl2';
export const BACKEND_CANVAS2D = 'canvas2d';

/** Hard ceilings applied on top of whatever the device reports. */
export const DEFAULT_LIMITS = {
  maxTextureSize: 2048,
  maxRenderbufferSize: 2048,
  maxViewportDims: 2048,
  maxTexturePixels: 2048 * 2048,
  maxDpr: 2,
  maxGlyphCacheEntries: 512,
  maxParticleBudget: 4096,
  maxPaintCells: 20000,
};

function clampInt(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, Math.floor(n)));
}

/**
 * Merge device-reported limits with the hard ceilings.
 * The smaller of (device, ceiling) wins so a huge GPU cannot defeat the budget.
 */
export function resolveLimits(device = {}) {
  const d = DEFAULT_LIMITS;
  return {
    maxTextureSize: clampInt(device.maxTextureSize, d.maxTextureSize, 64, d.maxTextureSize),
    maxRenderbufferSize: clampInt(device.maxRenderbufferSize, d.maxRenderbufferSize, 64, d.maxRenderbufferSize),
    maxViewportDims: clampInt(device.maxViewportDims, d.maxViewportDims, 64, d.maxViewportDims),
    maxTexturePixels: clampInt(device.maxTexturePixels, d.maxTexturePixels, 4096, d.maxTexturePixels),
    maxDpr: clampInt(device.maxDpr, d.maxDpr, 1, d.maxDpr),
    maxGlyphCacheEntries: clampInt(device.maxGlyphCacheEntries, d.maxGlyphCacheEntries, 16, d.maxGlyphCacheEntries),
    maxParticleBudget: clampInt(device.maxParticleBudget, d.maxParticleBudget, 16, d.maxParticleBudget),
    maxPaintCells: clampInt(device.maxPaintCells, d.maxPaintCells, 16, d.maxPaintCells),
  };
}

function readWebGLLimits(gl) {
  if (!gl || typeof gl.getParameter !== 'function') return {};
  const read = (key, fallbackKey) => {
    try {
      const value = gl.getParameter(gl[key]);
      return value == null ? undefined : value;
    } catch (_) {
      return undefined;
    }
  };
  const viewport = (() => {
    try {
      const value = gl.getParameter(gl.MAX_VIEWPORT_DIMS);
      if (!value || value.length < 2) return undefined;
      return Math.min(value[0], value[1]);
    } catch (_) {
      return undefined;
    }
  })();
  return {
    maxTextureSize: read('MAX_TEXTURE_SIZE'),
    maxRenderbufferSize: read('MAX_RENDERBUFFER_SIZE'),
    maxViewportDims: viewport,
    maxTexturePixels: undefined,
    maxDpr: undefined,
    maxGlyphCacheEntries: undefined,
    maxParticleBudget: undefined,
    maxPaintCells: undefined,
  };
}

/**
 * Probe graphics capability for a canvas.
 *
 * @param {object} options
 * @param {HTMLCanvasElement} options.canvas
 * @param {boolean} [options.allowWebGL2=true] Force the ordinary path when false.
 * @param {boolean} [options.forceCanvas2D=false] Hard-fallback (tests, reduced risk).
 * @param {object} [options.device] Override device limit reporting (tests).
 * @param {function} [options.createWebGL2] Injected context factory (tests).
 * @returns {{
 *   backend: 'webgl2'|'canvas2d',
 *   limits: object,
 *   reason: string|null,
 *   webgl2: WebGL2RenderingContext|null,
 *   accelerated: boolean,
 * }}
 */
export function probeGraphicsCapability(options = {}) {
  const {
    canvas,
    allowWebGL2 = true,
    forceCanvas2D = false,
    device = null,
    createWebGL2 = null,
  } = options;

  if (!canvas) {
    return {
      backend: BACKEND_CANVAS2D,
      limits: resolveLimits(device || {}),
      reason: 'no-canvas',
      webgl2: null,
      accelerated: false,
    };
  }

  if (forceCanvas2D || !allowWebGL2) {
    return {
      backend: BACKEND_CANVAS2D,
      limits: resolveLimits(device || {}),
      reason: forceCanvas2D ? 'forced-canvas2d' : 'webgl2-disabled',
      webgl2: null,
      accelerated: false,
    };
  }

  let gl = null;
  let reason = null;
  try {
    if (typeof createWebGL2 === 'function') {
      gl = createWebGL2(canvas);
    } else if (typeof canvas.getContext === 'function') {
      gl = canvas.getContext('webgl2', {
        alpha: true,
        antialias: false,
        depth: false,
        stencil: false,
        premultipliedAlpha: true,
        preserveDrawingBuffer: false,
        powerPreference: 'low-power',
        failIfMajorPerformanceCaveat: false,
      });
    }
  } catch (error) {
    gl = null;
    reason = `webgl2-throw:${error && error.name ? error.name : 'Error'}`;
  }

  if (!gl) {
    return {
      backend: BACKEND_CANVAS2D,
      limits: resolveLimits(device || {}),
      reason: reason || 'webgl2-unavailable',
      webgl2: null,
      accelerated: false,
    };
  }

  const deviceLimits = device || readWebGLLimits(gl);
  const limits = resolveLimits(deviceLimits);
  return {
    backend: BACKEND_WEBGL2,
    limits,
    reason: null,
    webgl2: gl,
    accelerated: true,
  };
}

/**
 * Attach context-loss listeners. Returns a detach function.
 * `onLost` must switch the consumer to the ordinary backend with state intact.
 * `onRestored` is optional and must never start a second animation owner.
 */
export function watchContextLoss(canvas, { onLost, onRestored } = {}) {
  if (!canvas || typeof canvas.addEventListener !== 'function') return () => {};
  const lost = (event) => {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    if (typeof onLost === 'function') onLost(event);
  };
  const restored = (event) => {
    if (typeof onRestored === 'function') onRestored(event);
  };
  canvas.addEventListener('webglcontextlost', lost, false);
  canvas.addEventListener('webglcontextrestored', restored, false);
  return () => {
    if (typeof canvas.removeEventListener !== 'function') return;
    canvas.removeEventListener('webglcontextlost', lost, false);
    canvas.removeEventListener('webglcontextrestored', restored, false);
  };
}
