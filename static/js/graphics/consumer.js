/**
 * Shared graphics consumer — accelerated and fallback backends behind one
 * lifecycle. Theme scenes, Graph and Imps mount through this surface so they
 * never branch on backend type and never own a second animation loop.
 *
 * Initialization failure and WebGL context loss both fall back to the ordinary
 * Canvas2D path with scene state intact and the same scene owner. Because a
 * canvas that has held a WebGL context cannot later yield a 2D context, the
 * fallback after loss replaces the canvas element while keeping the host
 * node, owner id and scene object unchanged. There is no retry loop.
 */

import { probeGraphicsCapability, watchContextLoss, BACKEND_WEBGL2, BACKEND_CANVAS2D } from './capability.js';
import { createCanvas2DBackend } from './backend-canvas2d.js';
import { createWebGL2Backend } from './backend-webgl2.js';
import { clampCanvasAllocation, createResourceBudget } from './resources.js';
import { createGraphicsScene, createDrawBatch } from './scene-state.js';
import { createSceneOwner } from './scene-owner.js';

function defaultCreateCanvas(doc) {
  const source = doc || (typeof document !== 'undefined' ? document : null);
  if (!source || typeof source.createElement !== 'function') {
    return { width: 1, height: 1, style: {}, setAttribute() {}, remove() {}, getContext: () => null };
  }
  return source.createElement('canvas');
}

/**
 * @param {object} options
 * @param {HTMLElement} [options.mount] Host element. Required when a canvas is
 *   not supplied. The consumer does not own host DOM beyond its canvas.
 * @param {HTMLCanvasElement} [options.canvas] Existing canvas to adopt.
 * @param {string} [options.id] Canvas/owner id (generated when omitted).
 * @param {object} [options.scene] Existing scene state (created when omitted).
 * @param {(args:{backend:object, scene:object, batch:object, time:number, reduced:boolean})=>void} options.draw
 *   Consumer paint function. Must use the provided backend/batch only.
 * @param {boolean} [options.allowWebGL2=true]
 * @param {boolean} [options.forceCanvas2D=false]
 * @param {object} [options.ownerHost] Registry host (tests).
 * @param {object} [options.win] Window-like object (tests).
 * @param {object} [options.doc] Document-like object (tests).
 * @param {object} [options.createWebGL2] Injected GL factory (tests).
 */
export function createGraphicsConsumer(options = {}) {
  const {
    mount = null,
    canvas: initialCanvas = null,
    id = `graphics-${Math.random().toString(36).slice(2, 10)}`,
    scene: initialScene = null,
    draw,
    allowWebGL2 = true,
    forceCanvas2D = false,
    ownerHost = globalThis,
    win = typeof window !== 'undefined' ? window : globalThis,
    doc = typeof document !== 'undefined' ? document : null,
    createWebGL2 = null,
  } = options;

  if (typeof draw !== 'function') {
    throw new Error('graphics consumer requires a draw function');
  }

  const scene = initialScene || createGraphicsScene({ id: `${id}-scene` });
  const batch = createDrawBatch();
  const budget = createResourceBudget({
    maxParticles: 4096,
    maxPaintCells: 20000,
  });

  let canvas = initialCanvas;
  if (!canvas && mount && typeof mount.appendChild === 'function') {
    canvas = defaultCreateCanvas(doc);
    if (canvas && canvas.style) {
      canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;pointer-events:none;';
    }
    if (canvas && typeof canvas.setAttribute === 'function') {
      canvas.setAttribute('aria-hidden', 'true');
      canvas.dataset = canvas.dataset || {};
      canvas.dataset.graphicsSurface = id;
    }
    mount.appendChild(canvas);
    canvas.__graphicsOwned = true;
  }
  if (!canvas) throw new Error('graphics consumer requires a canvas or mount');

  let backend = null;
  let backendKind = null;
  let accelerated = false;
  let fallbackReason = null;
  let detachContextWatch = () => {};
  let disposed = false;
  let initialized = false;

  /**
   * Swap in a replacement canvas after WebGL taint (context loss or init
   * failure). DOM swap runs for caller-supplied canvases too — otherwise the
   * replacement is invisible and the tainted original stays mounted. Ownership
   * only gates dispose-time removal, not the swap itself.
   */
  function adoptCanvas(nextCanvas) {
    const previous = canvas;
    if (previous && previous !== nextCanvas && typeof nextCanvas.getContext === 'function') {
      // Preserve layout box from the old surface.
      if (nextCanvas.style && previous.style) {
        nextCanvas.style.cssText = previous.style.cssText;
      }
      // Carry backing-store size so the swap is not blank until the next resize.
      if (typeof previous.width === 'number' && previous.width > 0) {
        nextCanvas.width = previous.width;
      }
      if (typeof previous.height === 'number' && previous.height > 0) {
        nextCanvas.height = previous.height;
      }
      nextCanvas.id = previous.id || id;
      if (previous.parentNode && typeof previous.parentNode.replaceChild === 'function') {
        previous.parentNode.replaceChild(nextCanvas, previous);
      }
    }
    canvas = nextCanvas;
    // The replacement was created here, so this consumer owns it for dispose.
    canvas.__graphicsOwned = true;
  }

  function createBackendFor(currentCanvas, probe) {
    if (probe.backend === BACKEND_WEBGL2 && probe.webgl2) {
      try {
        const glBackend = createWebGL2Backend({
          canvas: currentCanvas,
          limits: probe.limits,
          gl: probe.webgl2,
        });
        return { backend: glBackend, kind: BACKEND_WEBGL2, accelerated: true, limits: probe.limits };
      } catch (error) {
        return {
          backend: null,
          kind: BACKEND_CANVAS2D,
          accelerated: false,
          limits: probe.limits,
          reason: `webgl2-init-failed:${error && error.message ? error.message : 'Error'}`,
        };
      }
    }
    return {
      backend: null,
      kind: BACKEND_CANVAS2D,
      accelerated: false,
      limits: probe.limits,
      reason: probe.reason || 'webgl2-unavailable',
    };
  }

  function installCanvas2D(currentCanvas, limits, reason) {
    if (backend && typeof backend.dispose === 'function') {
      try { backend.dispose(); } catch (_) { /* already gone */ }
    }
    detachContextWatch();
    detachContextWatch = () => {};
    // A canvas that previously hosted WebGL cannot yield a 2D context.
    let target = currentCanvas;
    try {
      const probe2d = currentCanvas.getContext && currentCanvas.getContext('2d');
      if (!probe2d) {
        target = defaultCreateCanvas(doc);
        adoptCanvas(target);
      }
    } catch (_) {
      target = defaultCreateCanvas(doc);
      adoptCanvas(target);
    }
    backend = createCanvas2DBackend({ canvas: target, limits: limits || {} });
    backendKind = BACKEND_CANVAS2D;
    accelerated = false;
    fallbackReason = reason || fallbackReason || 'canvas2d';
    return backend;
  }

  function handleContextLost() {
    if (disposed) return;
    // State stays; drop only GPU resources and continue on the ordinary path.
    fallbackReason = 'webgl2-context-lost';
    const limits = (backend && backend.limits) || {};
    if (backend && typeof backend.dispose === 'function') {
      try { backend.markContextLost && backend.markContextLost(); } catch (_) { /* optional */ }
      try { backend.dispose(); } catch (_) { /* already gone */ }
    }
    backend = null;
    installCanvas2D(canvas, limits, 'webgl2-context-lost');
    owner.invalidate();
  }

  function initialize() {
    if (disposed || initialized) return backend;
    initialized = true;
    const probe = probeGraphicsCapability({
      canvas,
      allowWebGL2,
      forceCanvas2D,
      createWebGL2,
    });
    const created = createBackendFor(canvas, probe);
    if (created.backend) {
      backend = created.backend;
      backendKind = created.kind;
      accelerated = created.accelerated;
      fallbackReason = null;
      detachContextWatch = watchContextLoss(canvas, {
        onLost: handleContextLost,
        onRestored: () => {
          // Controlled recovery is opt-in later; never start a second owner.
        },
      });
    } else {
      installCanvas2D(canvas, created.limits, created.reason);
    }
    return backend;
  }

  const owner = createSceneOwner({
    id,
    scene,
    host: ownerHost,
    win,
    doc,
    isAlive: () => !disposed,
    paint: (time, reduced) => {
      if (disposed || !backend) return;
      batch.clear();
      backend.beginFrame();
      try {
        draw({ backend, scene, batch, time, reduced, budget });
        batch.flush(backend);
      } finally {
        backend.endFrame();
      }
    },
  });

  initialize();
  owner.start();

  return {
    id,
    scene,
    batch,
    budget,
    owner,
    get canvas() { return canvas; },
    get backend() { return backend; },
    get backendKind() { return backendKind; },
    get accelerated() { return accelerated; },
    get fallbackReason() { return fallbackReason; },
    get disposed() { return disposed; },

    /** Resize/DPR via capability-clamped allocation. */
    resize(cssWidth, cssHeight, dpr = 1) {
      if (disposed) return null;
      const limits = (backend && backend.limits) || {};
      const allocation = clampCanvasAllocation(cssWidth, cssHeight, dpr, limits);
      scene.resize(allocation.width, allocation.height, allocation.dpr);
      if (backend && typeof backend.resize === 'function') {
        backend.resize(allocation.width, allocation.height, allocation.dpr);
      }
      owner.invalidate();
      return allocation;
    },

    /** UI-only / data update — never a topology reset (T08–T10). */
    update(patch) {
      if (disposed) return false;
      const changed = scene.update(patch);
      owner.invalidate();
      return changed;
    },

    invalidate() {
      if (disposed) return false;
      scene.invalidate();
      owner.invalidate();
      return true;
    },

    suspend() {
      if (disposed) return false;
      owner.suspend();
      return true;
    },

    resume() {
      if (disposed) return false;
      owner.resume();
      return true;
    },

    /**
     * Tear down the consumer: cancel the single owner, dispose the backend
     * (GPU resources only) and drop the scene if this consumer created it.
     */
    dispose({ disposeScene = initialScene == null } = {}) {
      if (disposed) return;
      disposed = true;
      owner.dispose();
      detachContextWatch();
      detachContextWatch = () => {};
      if (backend && typeof backend.dispose === 'function') {
        try { backend.dispose(); } catch (_) { /* context already lost */ }
      }
      backend = null;
      budget.reset();
      batch.clear();
      if (disposeScene && scene && typeof scene.dispose === 'function') scene.dispose();
      if (canvas && canvas.__graphicsOwned && typeof canvas.remove === 'function') {
        try { canvas.remove(); } catch (_) { /* host already gone */ }
      }
    },
  };
}
