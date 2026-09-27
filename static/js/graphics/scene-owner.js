/**
 * Single scene owner — one animation loop per canvas.
 *
 * Hex: a duplicate animation loop or canvas remount presents as flicker.
 * This module is the only place allowed to schedule requestAnimationFrame for
 * a graphics surface. Palette/intensity updates and backend swaps reuse the
 * same owner; they never start a second loop.
 *
 * Honors visibility and prefers-reduced-motion. Reduced motion still paints
 * one frame after a setting/data change, then stops the loop. dispose()
 * cancels frames, timers and listeners and frees the owner slot.
 *
 * Checkpoint B: this registry (`__openClankGraphicsSceneOwners`) and theme.js
 * `__openClankBackgroundOwner` are separate single-owner guards. When theme
 * scenes adopt this consumer, integration must reconcile them so one surface
 * never keeps both owners live — a duplicate loop or canvas remount flickers.
 */

const OWNER_KEY = '__openClankGraphicsSceneOwners';

function ownerRegistry(host = null) {
  const root = host && typeof host === 'object' ? host : globalThis;
  if (!root[OWNER_KEY]) root[OWNER_KEY] = new Map();
  return root[OWNER_KEY];
}

/**
 * @param {object} options
 * @param {string} options.id Stable owner slot key (usually the canvas id).
 * @param {(time:number, reduced:boolean, scene:object|null)=>void} options.paint
 * @param {()=>boolean} [options.isAlive] Return false to auto-dispose.
 * @param {object} [options.scene] Scene state (optional, passed to paint).
 * @param {object} [options.host] Global/host object holding the owner registry.
 * @param {object} [options.win] Window-like object (tests inject).
 * @param {object} [options.doc] Document-like object (tests inject).
 */
export function createSceneOwner(options = {}) {
  const {
    id,
    paint,
    isAlive = () => true,
    scene = null,
    host = globalThis,
    win = typeof window !== 'undefined' ? window : globalThis,
    doc = typeof document !== 'undefined' ? document : null,
  } = options;

  if (!id) throw new Error('scene owner requires an id');
  if (typeof paint !== 'function') throw new Error('scene owner requires a paint function');

  const registry = ownerRegistry(host);
  // Enforce a single running owner per id: any previous owner is disposed first.
  const previous = registry.get(id);
  if (previous && typeof previous.dispose === 'function') {
    try { previous.dispose(); } catch (_) { /* previous owner already gone */ }
  }

  const motionQuery = typeof win.matchMedia === 'function'
    ? win.matchMedia('(prefers-reduced-motion: reduce)')
    : null;
  const reducedMotionOf = () => !!(motionQuery && motionQuery.matches);

  let animationFrame = 0;
  let paintOnceFrame = 0;
  let paintOnceToken = 0;
  let disposed = false;
  let suspended = false;
  let started = false;
  let painting = false;
  let repaintAfterPaint = false;
  let lastTime = 0;
  let frames = 0;

  function cancelFrame() {
    if (animationFrame && typeof win.cancelAnimationFrame === 'function') {
      win.cancelAnimationFrame(animationFrame);
    }
    animationFrame = 0;
    if (paintOnceFrame && typeof win.cancelAnimationFrame === 'function') {
      win.cancelAnimationFrame(paintOnceFrame);
    }
    paintOnceFrame = 0;
    paintOnceToken += 1;
  }

  function scheduleFrame() {
    if (disposed || suspended) return;
    if (animationFrame) return; // already scheduled — never stack loops
    if (reducedMotionOf()) return;
    if (typeof win.requestAnimationFrame !== 'function') return;
    animationFrame = win.requestAnimationFrame(frame);
  }

  function frame(time = 0) {
    animationFrame = 0;
    if (disposed) return;
    if (typeof isAlive === 'function' && !isAlive()) {
      dispose();
      return;
    }
    if (suspended) return;
    if (doc && doc.hidden) return;
    const reduced = reducedMotionOf();
    if (!reduced && lastTime && scene && typeof scene.advance === 'function') {
      scene.advance(Math.min(34, Math.max(0, time - lastTime)), false);
    } else if (reduced && scene && typeof scene.advance === 'function') {
      scene.advance(0, true);
    }
    lastTime = time;
    painting = true;
    try {
      paint(time, reduced, scene);
      frames += 1;
      if (scene && typeof scene.markPainted === 'function') scene.markPainted();
    } finally {
      painting = false;
    }
    if (reduced && repaintAfterPaint && !disposed && !suspended) {
      repaintAfterPaint = false;
      paintOnce();
    } else {
      repaintAfterPaint = false;
    }
    scheduleFrame();
  }

  /**
   * Reduced motion still needs one repaint after a setting/data change.
   * This is the only path that paints without starting a continuous loop.
   */
  function paintOnce() {
    if (disposed) return;
    if (typeof isAlive === 'function' && !isAlive()) {
      dispose();
      return;
    }
    const reduced = reducedMotionOf();
    if (reduced) {
      if (painting) {
        repaintAfterPaint = true;
        return;
      }
      // Defer the one-shot paint. Consumers can call owner.start() while
      // their draw callback is still being assigned; a synchronous reduced
      // paint would re-enter that callback before construction completes.
      if (paintOnceFrame) return;
      if (typeof win.requestAnimationFrame !== 'function') {
        const token = ++paintOnceToken;
        paintOnceFrame = 1;
        Promise.resolve().then(() => {
          if (token !== paintOnceToken || disposed || suspended || !reducedMotionOf()) return;
          paintOnceFrame = 0;
          frame(performanceNow());
        });
        return;
      }
      paintOnceFrame = win.requestAnimationFrame((time) => {
        paintOnceFrame = 0;
        if (disposed || suspended || !reducedMotionOf()) return;
        frame(time);
      });
      return;
    }
    scheduleFrame();
  }

  function performanceNow() {
    if (win.performance && typeof win.performance.now === 'function') return win.performance.now();
    return Date.now();
  }

  function handleVisibilityChange() {
    if (disposed) return;
    if (doc && doc.hidden) {
      cancelFrame();
      lastTime = 0;
      return;
    }
    lastTime = 0;
    if (reducedMotionOf()) paintOnce();
    else scheduleFrame();
  }

  function handleMotionChange() {
    if (disposed) return;
    cancelFrame();
    lastTime = 0;
    if (reducedMotionOf()) paintOnce();
    else scheduleFrame();
  }

  function handleHostEvent() {
    if (disposed) return;
    paintOnce();
    if (!reducedMotionOf()) scheduleFrame();
  }

  const owner = {
    id,
    get disposed() { return disposed; },
    get suspended() { return suspended; },
    get running() { return !!animationFrame && !disposed && !suspended; },
    get frames() { return frames; },
    get reducedMotion() { return reducedMotionOf(); },

    /** Start the single loop (idempotent). */
    start() {
      if (disposed || started) {
        if (!disposed) scheduleFrame();
        return owner;
      }
      started = true;
      registry.set(id, owner);
      if (doc && typeof doc.addEventListener === 'function') {
        doc.addEventListener('visibilitychange', handleVisibilityChange);
      }
      if (motionQuery) {
        if (typeof motionQuery.addEventListener === 'function') {
          motionQuery.addEventListener('change', handleMotionChange);
        } else if (typeof motionQuery.addListener === 'function') {
          motionQuery.addListener(handleMotionChange);
        }
      }
      // Settings/data changes request a repaint; consumers call invalidate().
      scheduleFrame();
      if (reducedMotionOf()) paintOnce();
      return owner;
    },

    /** Request a repaint without creating another owner or loop. */
    invalidate() {
      if (disposed) return owner;
      if (scene && typeof scene.invalidate === 'function') scene.invalidate();
      if (reducedMotionOf()) paintOnce();
      else scheduleFrame();
      return owner;
    },

    /** Suspend the loop (keep owner and state). */
    suspend() {
      if (disposed) return owner;
      suspended = true;
      cancelFrame();
      lastTime = 0;
      return owner;
    },

    /** Resume the same owner (never a second loop). */
    resume() {
      if (disposed) return owner;
      if (!suspended) {
        scheduleFrame();
        return owner;
      }
      suspended = false;
      lastTime = 0;
      if (reducedMotionOf()) paintOnce();
      else scheduleFrame();
      return owner;
    },

    /** Cancel loops/listeners and free the owner slot. */
    dispose() {
      if (disposed) return;
      disposed = true;
      cancelFrame();
      if (doc && typeof doc.removeEventListener === 'function') {
        doc.removeEventListener('visibilitychange', handleVisibilityChange);
      }
      if (motionQuery) {
        if (typeof motionQuery.removeEventListener === 'function') {
          motionQuery.removeEventListener('change', handleMotionChange);
        } else if (typeof motionQuery.removeListener === 'function') {
          motionQuery.removeListener(handleMotionChange);
        }
      }
      if (registry.get(id) === owner) registry.delete(id);
    },
  };

  // Expose handleHostEvent for consumers that repaint on custom host events.
  owner.onHostSignal = handleHostEvent;
  registry.set(id, owner);
  return owner;
}

/** Read the live owner for an id, if any (diagnostics/tests). */
export function getSceneOwner(id, host = globalThis) {
  return ownerRegistry(host).get(id) || null;
}

/** Count live owners (must stay 0 or 1 per surface). */
export function countSceneOwners(host = globalThis) {
  return ownerRegistry(host).size;
}
