/**
 * Editor state store — independent instances for injected core contexts,
 * plus the legacy mutable compatibility instance still used by Imps tools.
 *
 * Migration: imps.js used to own ~110 module-scope `let`
 * declarations and capture them via closure. Tool modules can't import
 * a `let` binding's mutations across module boundaries, so we move the
 * state into a single exported OBJECT whose properties are freely
 * mutated by anyone holding a reference. Read/write `state.transformW`
 * exactly the way the old code wrote `_transformW`.
 *
 * Slices land here one tool at a time; this file grows as more state
 * migrates out of imps.js. Defaults match the legacy
 * module-scope initializers verbatim — every `state.foo = …` reset
 * site in imps.js still works unchanged.
 */
export function createEditorState() {
  return {
  sceneRevision: 0,
  editEpoch: 0,
  // ── Transform tool ──
  // Drag-resize / rotate session state. While `transformActive` is
  // false every field below should be considered stale.
  transformActive: false,
  transformLayer: null,
  transformOrigW: 0,
  transformOrigH: 0,
  // Which corner/edge handle the user is currently dragging. One of
  // 'tl' | 'tr' | 'bl' | 'br' | 'rot' | null.
  transformHandle: null,
  // Which handle is currently under the cursor (no drag). Drives the
  // hover cursor lookup; lives next to `transformHandle` because both
  // come from `_getTransformHandle`.
  hoveredHandle: null,
  // Snapshot of the layer canvas + offset at transform start so Cancel
  // can restore exactly without re-fetching from the layer.
  transformOrigCanvas: null,
  transformOrigOffset: null,
  // In-progress dimensions / rotation / flips committed on Apply.
  transformPendingW: 0,
  transformPendingH: 0,
  transformPendingRot: 0,
  transformPendingFlipH: false,
  transformPendingFlipV: false,
  transformAspectLock: true,
  // Floating Transform popup element + drag-start offsets.
  transformPopup: null,
  transformStartX: 0,
  transformStartY: 0,
  transformStartOffX: 0,
  transformStartOffY: 0,
  // Transform overlay canvas — separate canvas positioned over the
  // main canvas with extra slack for handle rendering. Created by
  // _buildEditor; the move/transform tools draw their handle layer
  // onto its 2D context.
  transformOverlay: null,
  transformOverlayCtx: null,

  // ── Magic Wand tool ──
  // Binary selection mask + the layer it was sampled from. `wandMask`
  // is a canvas the size of `wandLayer`'s pixels, white where selected,
  // transparent elsewhere. `wandLastSeed` remembers the last click so
  // tolerance retunes can re-run the flood-fill without re-prompting.
  wandMask: null,
  wandLayerId: null,
  wandTolerance: 24,
  wandMaskVisible: true,
  wandMode: 'replace',
  wandLiveRetune: false,
  wandLastSeed: null,
  // Cached layer pixel data (getImageData is O(pixels) — expensive for
  // 4K layers; invalidated when the active layer changes).
  wandSrcCache: null,

  // ── Brush / Eraser / Clone tools ──
  // Shared paint color (brush picks up the swatch; eraser and clone
  // ignore color but reuse the same picker control).
  color: '#e06c75',
  // Brush diameter in canvas pixels. Persisted across tool switches;
  // bumped to a mask-friendly default on first inpaint entry.
  brushSize: 8,
  // Per-tool stroke modifiers — opacity + flow + softness. Each tool
  // owns its own row so users can dial them in independently.
  brushOpacity: 100,
  brushFlow: 100,
  brushSoftness: 100,
  eraserOpacity: 100,
  eraserFlow: 100,
  eraserSoftness: 100,
  cloneOpacity: 100,
  cloneFlow: 100,
  cloneSoftness: 100,
  // Clone-stamp source point (set via Alt-click or double-tap). Null
  // means no source picked yet — clicking with the clone tool no-ops
  // until a source is set.
  cloneSourceX: null,
  cloneSourceY: null,
  // Stroke-start offsets so the source moves WITH the brush, keeping
  // the source→destination offset constant across the stroke.
  cloneStrokeStartX: null,
  cloneStrokeStartY: null,
  // Frozen snapshot of the source layer's pixels at stroke start so
  // moving the source over previously-painted pixels samples the
  // original, not the in-progress stamp ring.
  cloneSourceSnapshot: null,
  cloneSourceLayerId: null,
  // Mobile: double-tap detection for "set source" since Alt-click
  // isn't an option without a keyboard.
  cloneLastTapTime: 0,
  cloneLastTapX: 0,
  cloneLastTapY: 0,

  // ── Inpaint + mask ──
  // Active mask canvas + its 2D context. Re-pointed to the active
  // mask sub-layer whenever the user picks a different mask in the
  // layer panel.
  maskCanvas: null,
  maskCtx: null,
  maskVisible: true,
  // Reused canvas for the union-of-masks tint pass (saves repeated
  // allocation on every composite).
  compositeMaskUnion: null,
  // Visual tint applied to mask pixels in the composite — purely
  // cosmetic; the AI model still sees a hard binary mask.
  maskTintColor: 'rgba(255, 110, 110, 1)',
  maskTintOpacity: 0.28,
  // Inpaint-tool paint vs erase modes (Ctrl+Alt flips for a single
  // stroke; UI buttons toggle the persistent setting).
  inpaintEraseMode: false,
  inpaintEraseStroke: false,
  // First-entry guard: bump brush size to the mask-friendly default
  // the first time the user opens inpaint per session.
  inpaintBrushInitialised: false,
  // Last successful inpaint result layer — drives the live edge
  // feather / stroke sliders (those only apply to the most recent
  // result).
  lastInpaintLayerId: null,
  // Captured handlers so we can detach them on close without leaking.
  inpaintDismissHandlers: null,
  // Background-remove tool state — pristine snapshot so the edge
  // cleanup sliders can live-rebuild alpha without re-running rembg.
  rembgLiveLayer: null,
  rembgLiveSnap: null,
  // Memoised "is rembg installed on the server?" probe.
  rembgInstalledCache: null,

  // ── Stroke drag state ──
  // Generic in-progress-stroke flags shared by brush/eraser/clone/
  // inpaint. `lastX/Y` are the last mouse position used to interpolate
  // a continuous line through fast-moving cursor samples.
  drawing: false,
  lastX: 0,
  lastY: 0,

  // ── Move tool ──
  moving: false,
  moveStartX: 0,
  moveStartY: 0,
  // Layer offset at drag start so we can compute the new offset by
  // (mouse - startMouse) + startOffset rather than accumulating delta.
  moveLayerOffsetX: 0,
  moveLayerOffsetY: 0,
  // Snap guides drawn during a move-tool drag (Ctrl held). Each entry
  // is a vertical / horizontal line in canvas space.
  activeSnapGuides: null,

  // ── Crop tool ──
  cropping: false,
  cropStart: null,
  cropEnd: null,
  cropRect: null,
  cropAspectLock: null,
  // True while the user drags the inside of an already-finished crop
  // rect to reposition it.
  cropMoving: false,
  cropMoveStart: null,

  // ── Lasso tool ──
  // Freehand selection polygon in canvas pixels. Empty when no lasso
  // is in progress or staged.
  lassoPoints: [],
  lassoActive: false,

  // In-editor copy/paste — separate from the OS clipboard so we can
  // round-trip layer alpha and metadata losslessly.
  internalClipboard: null,

  // ── Editor DOM refs ──
  // Root container that openEditor mounts into.
  container: null,
  // Main image canvas + its 2D context. Re-created on every openEditor
  // so the editor can reopen with fresh dimensions.
  mainCanvas: null,
  mainCtx: null,

  // ── Document + layers ──
  layers: [],
  activeLayerId: null,
  // Active tool ID — one of move/crop/transform/brush/eraser/clone/
  // lasso/wand/inpaint/rembg/harmonize/sharpen/upscale/style.
  tool: 'move',
  // Display zoom (1 = 100%). pan{X,Y} translate the canvas inside the
  // viewport.
  zoom: 1,
  panX: 0,
  panY: 0,
  // Document dimensions in canvas pixels.
  imgWidth: 0,
  imgHeight: 0,
  // Gallery image id this editor session is editing, or null for
  // blank-canvas drafts.
  imageId: null,
  // Files opaque resource ref for the writable original (set by
  // openResource). Save resolves the managed project through this.
  imageRef: null,
  // Managed Imps project identity + CAS revisions. Save/load go through
  // /api/imps/projects/* with these; null project means "create one".
  managedProvider: null,
  managedResourceId: null,
  projectId: null,
  projectRevision: 0,
  expectedImageRevision: '',
  // Original file extension so save-over-original re-encodes in the
  // same format (JPEG vs PNG matters: JPEG cuts upload size 5-10× for
  // camera photos over remote tunnels).
  originalExt: 'png',
  // True between openEditor / closeEditor — guards async callbacks
  // that fire after the user closes the editor (don't draw onto a
  // dead canvas, don't re-mount the spinner).
  editorOpen: false,
  // Document-level click-away handlers registered for the current
  // session. Tracked so closeEditor can detach them all cleanly.
  // Mutated in place (push / length = 0); the reference never changes.
  editorDocClickHandlers: [],
  suppressLayerTapUntil: 0,

  // ── Undo / redo ──
  undoStack: [],
  redoStack: [],

  // ── Layer offsets + id allocation ──
  // Map<layerId, {x, y}> — kept in a Map so we can serialise it
  // separately from the layer's own canvas. Mutated in place.
  layerOffsets: new Map(),
  nextLayerId: 1,

  // ── Popup / panel handles ──
  fxPopupEl: null,
  fxPopupLayerId: null,
  fxMenuEl: null,
  adjPopupEl: null,
  // rAF-throttled live preview while sliders are dragged in adj popups.
  adjRafPending: false,
  historyPanelEl: null,
  // Custom brush-cursor overlay element (circle following the mouse).
  cursorEl: null,
  // Hover-preview thumbnail floating element (singleton, repositioned).
  layerThumbEl: null,
  // Loading-overlay element (whirlpool + label).
  editorLoadingEl: null,

  // ── Draft persistence ──
  draftId: null,
  draftName: '',
  persistTimer: null,
  // Current PUT/POST promise so concurrent saves can chain.
  persistInFlight: null,
  // True when an edit happened during an in-flight save — triggers a
  // follow-up persist after the current one finishes.
  persistDirty: false,
  };
}

// Saved scene and persistence fields are shared; every other existing field
// remains owned by the one current view until its tool family is migrated.
const IMAGE_SCENE_FIELDS = Object.freeze(['sceneRevision','editEpoch','layers','activeLayerId','imgWidth','imgHeight','wandMask','wandLayerId','wandLastSeed','lastInpaintLayerId','imageId','imageRef','managedProvider','managedResourceId','projectId','projectRevision','expectedImageRevision','originalExt','undoStack','redoStack','layerOffsets','nextLayerId','draftId','draftName','persistTimer','persistInFlight','persistDirty','_editableExtras']);

export function createImageStateParts({scene = null} = {}) {
  const defaults = createEditorState(), shared = scene || {}, view = {}, state = {};
  const fields = new Set(IMAGE_SCENE_FIELDS);
  for (const key of new Set([...Object.keys(defaults), ...IMAGE_SCENE_FIELDS])) {
    const target = fields.has(key) ? shared : view;
    if (!Object.prototype.hasOwnProperty.call(target,key)) target[key] = defaults[key];
    Object.defineProperty(state,key,{enumerable:true,configurable:false,get:() => target[key],set:value => { target[key] = value; }});
  }
  return Object.freeze({scene:shared,view,state});
}

/** Legacy singleton stays the active ingress until every dependent family is injected. */
export const state = createEditorState();

/** Owned listeners and callbacks are retired exactly once with their view. */
export function createEditorLifetime() {
  let disposed = false;
  const cleanups = new Set();
  const add = (cleanup) => {
    if (typeof cleanup !== 'function') throw new TypeError('cleanup must be a function');
    if (disposed) { cleanup(); return () => {}; }
    cleanups.add(cleanup);
    return () => { if (cleanups.delete(cleanup)) cleanup(); };
  };
  return Object.freeze({
    get disposed() { return disposed; },
    add,
    listen(target, type, handler, options) {
      if (disposed) return () => {};
      target.addEventListener(type, handler, options);
      return add(() => target.removeEventListener(type, handler, options));
    },
    dispose() {
      if (disposed) return false;
      disposed = true;
      for (const cleanup of [...cleanups].reverse()) { cleanups.delete(cleanup); try { cleanup(); } catch (_) {} }
      return true;
    },
  });
}

/** History is session-owned; capture/restore remain the runtime's pixel authority. */
export function createEditorHistory({ state, capture, restore, limit = 20, onChanged = () => {}, now = Date.now, lifetime = null }) {
  if (!state || typeof capture !== 'function' || typeof restore !== 'function') throw new TypeError('history dependencies are required');
  const live = () => !lifetime?.disposed;
  const current = () => { const snapshot = capture(); snapshot._label = 'Current'; snapshot._ts = now(); return snapshot; };
  return Object.freeze({
    save(label) {
      if (!live()) return false;
      const snapshot = capture(); snapshot._label = label || 'Edit'; snapshot._ts = now();
      state.undoStack.push(snapshot);
      if (state.undoStack.length > limit) state.undoStack.shift();
      state.redoStack = [];
      return true;
    },
    undo() {
      if (!live() || !state.undoStack.length) return false;
      state.redoStack.push(current()); restore(state.undoStack.pop()); onChanged(); return true;
    },
    redo() {
      if (!live() || !state.redoStack.length) return false;
      state.undoStack.push(current()); restore(state.redoStack.pop()); onChanged(); return true;
    },
  });
}

/** Signal a committed saved-scene mutation without serializing the canvas. */
export function createSceneMutationSignal({ state, onChange = () => {} }) {
  if (!state || typeof onChange !== 'function') throw new TypeError('scene mutation dependencies required');
  return () => { state.sceneRevision += 1; onChange(state.sceneRevision); return state.sceneRevision; };
}

const sceneOwners = new WeakMap();

/** One scene/history writer with independent view invalidation subscribers. */
export function createImageSceneAuthority({state, sceneKey = state, onChange = () => {}, onDispose = () => {}, canDispose = () => true, isDirty = () => false}) {
  if (!state || !sceneKey || typeof onChange !== 'function' || typeof onDispose !== 'function' || typeof canDispose !== 'function' || typeof isDirty !== 'function' || sceneOwners.has(sceneKey)) throw new TypeError('image scene must have one owner');
  const views = new Set(), gestureEndListeners = new Set(); let history = null, disposed = false, gesture = null;
  const signal = createSceneMutationSignal({state,onChange:revision => {
    onChange(revision);
    for (const view of [...views]) { try { view(revision); } catch (error) { console.error('[imps] view invalidation failed', error); } }
  }});
  const scene = Object.freeze({
    get state() { return state; },
    get revision() { return state.sceneRevision; },
    get dirty() { return !disposed && isDirty(); },
    get history() { return history; },
    beginGesture(view, operation) { if (disposed || gesture || !view || !operation) return null; gesture = Object.freeze({view,operation}); return gesture; },
    ownsGesture(token) { return !disposed && !!token && gesture === token; },
    endGesture(token) { if (!token || gesture !== token) return false; gesture = null; for (const listener of [...gestureEndListeners]) { try { listener(state.sceneRevision); } catch (error) { console.error('[imps] gesture listener failed', error); } } return true; },
    subscribeGestureEnd(listener) { if (disposed || typeof listener !== 'function') throw new TypeError('live gesture listener required'); gestureEndListeners.add(listener); return () => gestureEndListeners.delete(listener); },
    canApplyAt(revision) { return !disposed && !gesture && state.sceneRevision === revision; },
    canDispose() { return disposed || (!gesture && canDispose()); },
    markChanged() { if (disposed) return false; return signal(); },
    undo() { if (disposed || gesture || !history?.undo()) return false; signal(); return true; },
    redo() { if (disposed || gesture || !history?.redo()) return false; signal(); return true; },
    project(event) { if (disposed) return false; for (const view of [...views]) { try { view(state.sceneRevision,event); } catch (error) { console.error('[imps] view projection failed', error); } } return true; },
    installHistory(options) { if (disposed || history) throw new Error('image history already installed or scene retired'); const lifetime = {get disposed() { return disposed || options.lifetime?.disposed === true; }}; history = createEditorHistory({...options,state,lifetime}); return history; },
    subscribeView(invalidate) { if (disposed || typeof invalidate !== 'function') throw new TypeError('live view callback required'); views.add(invalidate); return () => views.delete(invalidate); },
    dispose() { if (disposed || gesture || !canDispose()) return false; disposed = true; views.clear(); gestureEndListeners.clear(); try { onDispose(); } finally { if (sceneOwners.get(sceneKey) === scene) sceneOwners.delete(sceneKey); } return true; },
  });
  sceneOwners.set(sceneKey,scene); return scene;
}
