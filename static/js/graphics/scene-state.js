/**
 * Graphics scene state — CPU-side document/image state.
 *
 * Scene state lives entirely outside GPU resources. Backends, caches and
 * textures may be created, swapped or destroyed without mutating this object.
 * That is what lets context loss and accelerated/fallback transitions keep
 * the visible scene intact.
 *
 * Identity vs style (T08–T10): palette, intensity and other UI-only controls
 * are updates that do not reset trajectories, seeds or animation phase. A
 * topology change (new pattern/application, geometry identity) rebuilds the
 * document while retaining the same scene object and animation owner.
 */

function stableId(prefix = 'scene') {
  return `${prefix}-${Math.random().toString(36).slice(2, 10)}`;
}

/**
 * @param {object} options
 * @param {string} [options.id] Stable scene identity (topology).
 * @param {number} [options.seed] Deterministic seed for procedural content.
 * @param {number} [options.width]
 * @param {number} [options.height]
 * @param {number} [options.dpr]
 * @param {object} [options.style] Palette/intensity/UI-only bag.
 * @param {object} [options.documentState] Initial document/image state.
 */
export function createGraphicsScene(options = {}) {
  const {
    id = stableId(),
    seed = 1,
    width = 1,
    height = 1,
    dpr = 1,
    style = {},
    documentState = {},
  } = options;

  let disposed = false;
  let invalidated = true;
  let revision = 0;
  let geometry = {
    width: Math.max(1, Math.floor(width)),
    height: Math.max(1, Math.floor(height)),
    dpr: Math.max(1, Number(dpr) || 1),
  };
  let styleBag = { ...style };
  let documentBag = { ...documentState };
  let phase = 0; // animation phase in ms, owned by the scene owner

  function ensureAlive() {
    return !disposed;
  }

  return {
    id,
    seed,
    get disposed() { return disposed; },
    get invalidated() { return invalidated; },
    get revision() { return revision; },
    get width() { return geometry.width; },
    get height() { return geometry.height; },
    get dpr() { return geometry.dpr; },
    get phase() { return phase; },
    get style() { return { ...styleBag }; },
    get documentState() { return { ...documentBag }; },

    /** Lifecycle hook: first-time setup. Idempotent. */
    initialize(init = {}) {
      if (!ensureAlive()) return false;
      if (init.documentState) documentBag = { ...documentBag, ...init.documentState };
      if (init.style) styleBag = { ...styleBag, ...init.style };
      invalidated = true;
      revision += 1;
      return true;
    },

    /**
     * Lifecycle hook: resize / DPR change. Geometry only — this is not a
     * topology reset. Trajectories and seeds survive.
     */
    resize(nextWidth, nextHeight, nextDpr) {
      if (!ensureAlive()) return false;
      const w = Math.max(1, Math.floor(Number(nextWidth) || 1));
      const h = Math.max(1, Math.floor(Number(nextHeight) || 1));
      const d = Math.max(1, Number(nextDpr) || 1);
      const changed = geometry.width !== w || geometry.height !== h || geometry.dpr !== d;
      geometry = { width: w, height: h, dpr: d };
      if (changed) {
        invalidated = true;
        revision += 1;
      }
      return changed;
    },

    /**
     * Lifecycle hook: non-topology update (palette, intensity, UI controls,
     * data that mutates document state in place). Never resets identity.
     */
    update(patch = {}) {
      if (!ensureAlive()) return false;
      if (patch.style) styleBag = { ...styleBag, ...patch.style };
      if (patch.documentState) documentBag = { ...documentBag, ...patch.documentState };
      if (patch.phase != null && Number.isFinite(Number(patch.phase))) {
        phase = Number(patch.phase);
      }
      invalidated = true;
      revision += 1;
      return true;
    },

    /**
     * Lifecycle hook: rebuild document for a new topology while keeping the
     * same scene identity and owner. Seeds stay deterministic unless replaced.
     */
    resetTopology({ seed: nextSeed, documentState, style } = {}) {
      if (!ensureAlive()) return false;
      if (nextSeed != null && Number.isFinite(Number(nextSeed))) {
        // seed is const-like on the instance; rebuild document bag only.
        documentBag = { ...documentBag, __seed: Number(nextSeed) };
      }
      if (documentState) documentBag = { ...documentState };
      if (style) styleBag = { ...styleBag, ...style };
      phase = 0;
      invalidated = true;
      revision += 1;
      return true;
    },

    /** Mark the scene as needing a repaint without mutating content. */
    invalidate() {
      if (!ensureAlive()) return false;
      invalidated = true;
      return true;
    },

    /** Called by the scene owner after a successful paint. */
    markPainted() {
      invalidated = false;
    },

    /** Advance animation phase. Reduced motion freezes phase at 0. */
    advance(deltaMs, reducedMotion = false) {
      if (!ensureAlive()) return;
      if (reducedMotion) {
        phase = 0;
        return;
      }
      const delta = Math.max(0, Math.min(34, Number(deltaMs) || 0));
      phase += delta;
    },

    /**
     * Snapshot for diagnostics/tests. Never includes GPU handles.
     */
    snapshot() {
      return {
        id,
        seed: documentBag.__seed != null ? documentBag.__seed : seed,
        revision,
        invalidated,
        disposed,
        geometry: { ...geometry },
        style: { ...styleBag },
        documentState: { ...documentBag },
        phase,
      };
    },

    /** Lifecycle hook: free CPU scene state. Safe after backend dispose. */
    dispose() {
      if (disposed) return;
      disposed = true;
      documentBag = {};
      styleBag = {};
      invalidated = false;
    },
  };
}

/**
 * Shared batch collector. Consumers record rects/lines/circles/glyphs once and
 * flush to whichever backend is live, so accelerated and fallback paths share
 * one draw list and stay visually equivalent.
 */
export function createDrawBatch() {
  const rects = [];
  const lines = [];
  const circles = [];
  const glyphs = [];
  return {
    get size() { return rects.length + lines.length + circles.length + glyphs.length; },
    clear() {
      rects.length = 0;
      lines.length = 0;
      circles.length = 0;
      glyphs.length = 0;
    },
    rect(x, y, w, h, color, alpha = 1) {
      rects.push({ x, y, w, h, color, alpha });
    },
    line(x1, y1, x2, y2, color, width = 1, alpha = 1) {
      lines.push({ x1, y1, x2, y2, color, width, alpha });
    },
    circle(x, y, r, color, alpha = 1, stroke = null, strokeWidth = 1) {
      circles.push({ x, y, r, color, alpha, stroke, strokeWidth });
    },
    glyph(text, x, y, size, weight, color, alpha = 1, align, baseline, font) {
      glyphs.push({ text, x, y, size, weight, color, alpha, align, baseline, font });
    },
    /** Flush the batch into a backend adapter and clear. */
    flush(backend) {
      if (!backend) {
        this.clear();
        return;
      }
      if (rects.length) backend.drawRects(rects.slice());
      if (lines.length) backend.drawLines(lines.slice());
      if (circles.length) backend.drawCircles(circles.slice());
      if (glyphs.length) backend.drawGlyphs(glyphs.slice());
      this.clear();
    },
  };
}
