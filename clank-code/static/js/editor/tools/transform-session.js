/**
 * Transform-tool session lifecycle + floating popup wiring.
 *
 *   _startTransform        snapshot the active layer + open popup
 *   _openTransformPopup    build the W/H/rotation popup, wire inputs
 *   _wireTransformDrag     header drag, mobile + desktop position handling
 *   _reapplyTransform      live preview re-render from the snapshot
 *   _confirmTransform      commit + clear session state
 *   _cancelTransform       restore via undo() + clear session state
 *
 * Handle-drag interactions on the CANVAS (corner / rotation grip) live
 * in `editor/tools/transform-drag.js` — those mutate the same staged
 * `state.transformPending*` fields that the popup inputs do, so both
 * surfaces stay in sync via `_reapplyTransform()`.
 *
 * @param {{
 *   activeLayer:           () => object | null,
 *   saveState:             (label?: string) => void,
 *   composite:             () => void,
 *   fitZoom:               () => void,
 *   drawTransformHandles:  () => void,
 *   showCanvasLoading:     (label: string) => void,
 *   hideCanvasLoading:     () => void,
 *   undo:                  () => void,
 *   uiModule:              object | null,
 * }} deps
 *
 * @returns {{
 *   startTransform, openTransformPopup, closeTransformPopup,
 *   reapplyTransform, confirmTransform, cancelTransform,
 * }}
 */
import { state as legacyState } from '../state.js';
import {
  transformPopupHTML,
  attachSpinRepeat,
} from '../build/transform-popup.js';

export function createTransformSession({ state = legacyState, root = null, lifetime = null,
  scopeIds = node => node, activeLayer, saveState, composite, fitZoom, drawTransformHandles,
  showCanvasLoading, hideCanvasLoading, undo, uiModule, markSceneChanged = () => {}, sceneRevision = () => state.sceneRevision, gesture = {begin:() => true, owns:() => true, end:() => {}},
}) {
  if (state !== legacyState && (!root || typeof lifetime?.add !== 'function')) throw new TypeError('isolated transform requires root and lifetime');
  let popupCleanup = () => {};
  let disposed = false;
  let original = null;
  lifetime?.add(() => { if (state.transformActive) cancelTransform(); else closeTransformPopup(); disposed = true; });
  function startTransform() {
    if (disposed) return;
    const layer = activeLayer();
    if (!layer || layer.locked) { uiModule.showToast('Select an unlocked layer'); return; }
    if (state.transformActive) { cancelTransform(); return; } // toggle off
    if (!gesture.begin()) return;
    try {
    state.transformActive = true;
    state.transformLayer = layer;
    state.transformOrigW = layer.canvas.width;
    state.transformOrigH = layer.canvas.height;
    state.transformPendingW = state.transformOrigW;
    state.transformPendingH = state.transformOrigH;
    state.transformPendingRot = 0;
    state.transformPendingFlipH = false;
    state.transformPendingFlipV = false;
    // Snapshot the layer so live preview can re-derive from the
    // original pixels on every keystroke instead of stacking
    // destructive edits.
    state.transformOrigCanvas = (state.container?.ownerDocument || document).createElement('canvas');
    state.transformOrigCanvas.width = state.transformOrigW;
    state.transformOrigCanvas.height = state.transformOrigH;
    state.transformOrigCanvas.getContext('2d').drawImage(layer.canvas, 0, 0);
    state.transformOrigOffset = { ...(state.layerOffsets.get(layer.id) || { x: 0, y: 0 }) };
    const offset = { ...state.transformOrigOffset };
    const previousHistory = state.undoStack?.at(-1);
    saveState();
    const history = state.undoStack?.at(-1);
    original = { layer, canvas: state.transformOrigCanvas, offset, history: history !== previousHistory ? history : null, revision: sceneRevision() };
    // Fit canvas to viewport so the corner handles are visible —
    // without this, a layer larger than the viewport leaves the grab
    // markers off-screen.
    try { fitZoom(); } catch {}
    composite();
    drawTransformHandles();
    openTransformPopup();
    } catch (error) { cancelTransform(); throw error; }
  }

  function closeTransformPopup() {
    popupCleanup();
    popupCleanup = () => {};
    if (state.transformPopup) {
      try { state.transformPopup.remove(); } catch {}
      state.transformPopup = null;
    }
  }

  function borrowMove(layer) {
    if (!original || state.transformLayer!==layer || !gesture.owns() || sceneRevision()!==original.revision) return null;
    const base={...state.transformOrigOffset};
    return Object.freeze({owns:() => !!original && state.transformLayer===layer && gesture.owns() && sceneRevision()===original.revision,
      move:(dx,dy) => {state.transformOrigOffset={x:base.x+dx,y:base.y+dy};}});
  }

  // Floating Transform popup — horizontal layout, draggable via its
  // header, anchored over the right panel (layers area) by default
  // so it doesn't cover the canvas. Lets the user type exact W/H/Rot
  // and flip via negative values.
  function openTransformPopup() {
    closeTransformPopup();
    if (!state.container) return;
    const pop = (state.container?.ownerDocument || document).createElement('div');
    pop.className = 'ge-transform-popup';
    pop.innerHTML = transformPopupHTML();
    scopeIds(pop);
    state.container.appendChild(pop);
    state.transformPopup = pop;
    const stopDrag = wireTransformDrag(pop);
    const removers = [];
    const frames = new Set();
    const win = pop.ownerDocument.defaultView;
    const listen = (target, type, fn) => { if (!target) return; const guarded = (...args) => { if (!disposed && state.transformPopup === pop) fn(...args); }; target.addEventListener(type, guarded); removers.push(() => target.removeEventListener(type, guarded)); };
    const stopPopup = () => { for (const id of frames) win.cancelAnimationFrame(id); if (frames.size) hideCanvasLoading(); frames.clear(); for (const remove of removers.splice(0)) remove(); stopDrag(); };
    popupCleanup = stopPopup;
    const wInput = pop.querySelector(':is(#ge-transform-w,[data-ge-id="ge-transform-w"])');
    const hInput = pop.querySelector(':is(#ge-transform-h,[data-ge-id="ge-transform-h"])');
    const rotInput = pop.querySelector(':is(#ge-transform-rot,[data-ge-id="ge-transform-rot"])');
    const aspectBtn = pop.querySelector(':is(#ge-transform-aspect,[data-ge-id="ge-transform-aspect"])');
    wInput.value = String(state.transformOrigW);
    hInput.value = String(state.transformOrigH);
    rotInput.value = '0';
    aspectBtn.classList.toggle('active', state.transformAspectLock);
    aspectBtn.setAttribute('aria-pressed', state.transformAspectLock ? 'true' : 'false');

    // Aspect-lock follower model: while the lock is engaged, ONE
    // field is the "driver" and the other is read-only + dimmed.
    // Driver = whichever field the user last typed in. Toggling the
    // chain releases the follower.
    let driver = null;
    const applyAspectVisuals = () => {
      if (!state.transformAspectLock || !driver) {
        wInput.readOnly = false;
        hInput.readOnly = false;
        wInput.classList.remove('ge-transform-input-locked');
        hInput.classList.remove('ge-transform-input-locked');
        return;
      }
      const followerW = driver === 'h';
      const followerH = driver === 'w';
      wInput.readOnly = followerW;
      hInput.readOnly = followerH;
      wInput.classList.toggle('ge-transform-input-locked', followerW);
      hInput.classList.toggle('ge-transform-input-locked', followerH);
    };
    const refresh = () => {
      let w = parseInt(wInput.value, 10);
      let h = parseInt(hInput.value, 10);
      const rot = parseInt(rotInput.value, 10) || 0;
      state.transformPendingFlipH = w < 0;
      state.transformPendingFlipV = h < 0;
      w = Math.abs(w || state.transformOrigW);
      h = Math.abs(h || state.transformOrigH);
      state.transformPendingW = Math.max(1, w);
      state.transformPendingH = Math.max(1, h);
      state.transformPendingRot = rot;
      reapplyTransform();
    };
    listen(wInput, 'input', () => {
      if (state.transformAspectLock) {
        driver = 'w';
        const w = parseInt(wInput.value, 10);
        if (!Number.isNaN(w) && state.transformOrigW > 0) {
          const sign = (parseInt(hInput.value, 10) || 1) < 0 ? -1 : 1;
          const newH = Math.round((Math.abs(w) / state.transformOrigW) * state.transformOrigH) * sign;
          hInput.value = String(newH);
        }
        applyAspectVisuals();
      }
      refresh();
    });
    listen(hInput, 'input', () => {
      if (state.transformAspectLock) {
        driver = 'h';
        const h = parseInt(hInput.value, 10);
        if (!Number.isNaN(h) && state.transformOrigH > 0) {
          const sign = (parseInt(wInput.value, 10) || 1) < 0 ? -1 : 1;
          const newW = Math.round((Math.abs(h) / state.transformOrigH) * state.transformOrigW) * sign;
          wInput.value = String(newW);
        }
        applyAspectVisuals();
      }
      refresh();
    });
    listen(rotInput, 'input', refresh);
    listen(aspectBtn, 'click', () => {
      state.transformAspectLock = !state.transformAspectLock;
      aspectBtn.classList.toggle('active', state.transformAspectLock);
      aspectBtn.setAttribute('aria-pressed', state.transformAspectLock ? 'true' : 'false');
      // Reset follower the moment the user breaks the lock so both
      // fields go editable; re-engaging means "next type sets the driver".
      driver = null;
      applyAspectVisuals();
    });
    listen(pop.querySelector(':is(#ge-transform-apply,[data-ge-id="ge-transform-apply"])'), 'click', () => confirmTransform());
    listen(pop.querySelector(':is(#ge-transform-cancel,[data-ge-id="ge-transform-cancel"])'), 'click', () => cancelTransform());
    listen(pop.querySelector(':is(#ge-transform-cancel-btn,[data-ge-id="ge-transform-cancel-btn"])'), 'click', () => cancelTransform());
    // Minimise — collapses the body so only the header is visible.
    listen(pop.querySelector(':is(#ge-transform-min,[data-ge-id="ge-transform-min"])'), 'click', (e) => {
      e.stopPropagation();
      pop.classList.toggle('ge-transform-popup-minimised');
    });
    // Quick actions: flip W/H via sign so the reapply pipeline picks
    // up the new orientation. Rotate-90 nudges rotation ±90°.
    listen(pop.querySelector(':is(#ge-transform-flip-h,[data-ge-id="ge-transform-flip-h"])'), 'click', () => {
      const wIn = pop.querySelector(':is(#ge-transform-w,[data-ge-id="ge-transform-w"])');
      const cur = parseInt(wIn.value, 10) || state.transformOrigW;
      wIn.value = String(-cur);
      wIn.dispatchEvent(new Event('input', { bubbles: true }));
    });
    listen(pop.querySelector(':is(#ge-transform-flip-v,[data-ge-id="ge-transform-flip-v"])'), 'click', () => {
      const hIn = pop.querySelector(':is(#ge-transform-h,[data-ge-id="ge-transform-h"])');
      const cur = parseInt(hIn.value, 10) || state.transformOrigH;
      hIn.value = String(-cur);
      hIn.dispatchEvent(new Event('input', { bubbles: true }));
    });
    listen(pop.querySelector(':is(#ge-transform-rot-90,[data-ge-id="ge-transform-rot-90"])'), 'click', (e) => {
      const rIn = pop.querySelector(':is(#ge-transform-rot,[data-ge-id="ge-transform-rot"])');
      const cur = parseInt(rIn.value, 10) || 0;
      const delta = e.shiftKey ? -90 : 90;
      let next = cur + delta;
      while (next > 180) next -= 360;
      while (next <= -180) next += 360;
      rIn.value = String(next);
      // Big images: rotation pass blocks UI ~0.5–2 s. Show a spinner
      // so the user sees something happen. rAF defers the heavy work
      // past the current frame so the overlay paints first.
      showCanvasLoading('Rotating…');
      const frame = win.requestAnimationFrame(() => {
        frames.delete(frame);
        try { if (!disposed && state.transformPopup === pop) rIn.dispatchEvent(new Event('input', { bubbles: true })); }
        finally { hideCanvasLoading(); }
      });
      frames.add(frame);
    });
    const stopSpin = attachSpinRepeat(pop);
    popupCleanup = () => { stopPopup(); stopSpin(); };
  }

  // Header-drag for the Transform popup. Default position: over the
  // right panel (layers area). Mobile pins via stylesheet so we use
  // setProperty 'important' to override during drag.
  function wireTransformDrag(pop) {
    const doc = pop.ownerDocument;
    const win = doc.defaultView;
    const isMobile = win.matchMedia('(max-width: 820px)').matches;
    const removers = [];
    const listen = (target, type, fn, options) => { target.addEventListener(type, fn, options); removers.push(() => target.removeEventListener(type, fn, options)); };
    const defaultRight = 20;
    const defaultTop = 60;
    if (isMobile) {
      pop.style.setProperty('position', 'fixed', 'important');
    } else {
      pop.style.position = 'absolute';
      pop.style.right = defaultRight + 'px';
      pop.style.top = defaultTop + 'px';
      pop.style.left = 'auto';
    }
    const dragSource = pop.querySelector('[data-transform-drag]') || pop;
    let dragging = false;
    let startX = 0, startY = 0, originLeft = 0, originTop = 0;
    const NON_DRAG = 'input,button,select,textarea,a,[contenteditable]';

    const setPos = (x, y) => {
      if (isMobile) {
        pop.style.setProperty('left', x + 'px', 'important');
        pop.style.setProperty('top', y + 'px', 'important');
        pop.style.setProperty('right', 'auto', 'important');
        pop.style.setProperty('bottom', 'auto', 'important');
        pop.style.setProperty('width', 'auto', 'important');
        pop.style.setProperty('max-width', 'calc(100vw - 16px)', 'important');
      } else {
        pop.style.left = x + 'px';
        pop.style.top = y + 'px';
        pop.style.right = 'auto';
      }
    };

    const beginDrag = (clientX, clientY) => {
      dragging = true;
      const rect = pop.getBoundingClientRect();
      if (isMobile) {
        originLeft = rect.left;
        originTop = rect.top;
      } else {
        const parentRect = state.container.getBoundingClientRect();
        originLeft = rect.left - parentRect.left;
        originTop = rect.top - parentRect.top;
      }
      startX = clientX;
      startY = clientY;
      setPos(originLeft, originTop);
      pop.classList.add('ge-transform-popup-dragging');
      doc.body.style.userSelect = 'none';
    };

    const moveDrag = (clientX, clientY) => {
      if (!dragging) return;
      const dx = clientX - startX;
      const dy = clientY - startY;
      let nx = originLeft + dx;
      let ny = originTop + dy;
      if (isMobile) {
        const rect = pop.getBoundingClientRect();
        nx = Math.max(0, Math.min(win.innerWidth - rect.width, nx));
        ny = Math.max(0, Math.min(win.innerHeight - rect.height, ny));
      }
      setPos(nx, ny);
    };

    const endDrag = () => {
      if (!dragging) return;
      dragging = false;
      doc.body.style.userSelect = '';
      pop.classList.remove('ge-transform-popup-dragging');
    };

    listen(dragSource, 'mousedown', (e) => {
      if (e.target.closest(NON_DRAG)) return;
      e.preventDefault();
      beginDrag(e.clientX, e.clientY);
    });
    listen(doc, 'mousemove', (e) => moveDrag(e.clientX, e.clientY));
    listen(doc, 'mouseup', endDrag);

    listen(dragSource, 'touchstart', (e) => {
      if (e.target.closest(NON_DRAG)) return;
      if (!e.touches || e.touches.length !== 1) return;
      e.preventDefault();
      beginDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });
    listen(doc, 'touchmove', (e) => {
      if (!dragging) return;
      if (!e.touches || e.touches.length !== 1) return;
      e.preventDefault();
      moveDrag(e.touches[0].clientX, e.touches[0].clientY);
    }, { passive: false });
    listen(doc, 'touchend', endDrag);
    listen(doc, 'touchcancel', endDrag);
    return () => { endDrag(); for (const remove of removers.splice(0)) remove(); };
  }

  // Re-derive the active layer's pixels from the original snapshot
  // with the popup's current W/H/flip/rotation applied. Cheap —
  // paints into an off-screen canvas of the final size.
  function reapplyTransform() {
    if (!original || !gesture.owns() || sceneRevision() !== original.revision) { cancelTransform(); return; }
    const layer = state.transformLayer;
    if (!layer || !state.transformOrigCanvas) return;
    const w = state.transformPendingW;
    const h = state.transformPendingH;
    const rotDeg = state.transformPendingRot;
    const rotRad = (rotDeg * Math.PI) / 180;
    const cos = Math.abs(Math.cos(rotRad));
    const sin = Math.abs(Math.sin(rotRad));
    // Bounding box of the rotated W×H — canvas grows so corners
    // don't clip.
    const finalW = Math.max(1, Math.round(w * cos + h * sin));
    const finalH = Math.max(1, Math.round(w * sin + h * cos));
    const tmp = (state.container?.ownerDocument || document).createElement('canvas');
    tmp.width = finalW; tmp.height = finalH;
    const tCtx = tmp.getContext('2d');
    tCtx.imageSmoothingEnabled = true;
    tCtx.imageSmoothingQuality = 'high';
    tCtx.save();
    tCtx.translate(finalW / 2, finalH / 2);
    if (rotDeg) tCtx.rotate(rotRad);
    tCtx.scale(state.transformPendingFlipH ? -1 : 1, state.transformPendingFlipV ? -1 : 1);
    tCtx.drawImage(state.transformOrigCanvas, -w / 2, -h / 2, w, h);
    tCtx.restore();
    layer.canvas.width = finalW;
    layer.canvas.height = finalH;
    layer.ctx.clearRect(0, 0, finalW, finalH);
    layer.ctx.drawImage(tmp, 0, 0);
    // Recenter the layer so the rotation pivot stays put visually.
    const origCenterX = state.transformOrigOffset.x + state.transformOrigW / 2;
    const origCenterY = state.transformOrigOffset.y + state.transformOrigH / 2;
    state.layerOffsets.set(layer.id, {
      x: Math.round(origCenterX - finalW / 2),
      y: Math.round(origCenterY - finalH / 2),
    });
    markSceneChanged();
    original.revision = sceneRevision();
    composite();
    drawTransformHandles();
  }

  function confirmTransform() {
    if (original && (!gesture.owns() || sceneRevision() !== original.revision)) { cancelTransform(); return; }
    if (state.transformActive) markSceneChanged();
    closeTransformPopup();
    original = null;
    state.transformOrigCanvas = null;
    state.transformOrigOffset = null;
    state.transformActive = false;
    state.transformLayer = null;
    state.transformHandle = null;
    gesture.end();
    composite();
    uiModule.showToast('Transform applied');
  }

  function cancelTransform() {
    closeTransformPopup();
    const stale = original && (!gesture.owns() || sceneRevision() !== original.revision);
    if (original && !stale && state.layers.includes(original.layer)) {
      const { layer, canvas, offset, history } = original;
      layer.canvas.width = canvas.width; layer.canvas.height = canvas.height;
      layer.ctx.clearRect(0, 0, canvas.width, canvas.height);
      layer.ctx.drawImage(canvas, 0, 0);
      state.layerOffsets.set(layer.id, { ...offset });
      markSceneChanged();
      if (history && state.undoStack?.at(-1) === history) state.undoStack.pop();
    }
    original = null;
    state.transformOrigCanvas = null;
    state.transformOrigOffset = null;
    state.transformActive = false;
    state.transformLayer = null;
    state.transformHandle = null;
    gesture.end();
    composite();
    if (stale) uiModule.showToast('Transform ended because the image changed');
  }

  const methods = { startTransform, openTransformPopup, closeTransformPopup, reapplyTransform, confirmTransform, cancelTransform, borrowMove };
  return Object.fromEntries(Object.entries(methods).map(([name, fn]) => [name, (...args) => disposed ? undefined : fn(...args)]));
}
