/**
 * Transform-drag tool — handle drag interactions for the Transform
 * tool (resize via corner/edge handles, rotation via the rot grip).
 *
 * The transform UI runs in TWO modes: the floating popup (W/H/rot
 * numeric inputs, lives elsewhere) AND direct drag on the canvas
 * handles. Both ultimately mutate `state.transformPendingW/H/Rot` and
 * call `reapplyTransform()` to redraw. This module owns the drag
 * branch.
 *
 * The dispatcher in imps.js calls `tryBegin/tryContinue/
 * tryEnd` which return `true` when the event was for the transform
 * tool and was handled (so the dispatcher can short-circuit).
 *
 * @param {{
 *   beginMove:             (e: Event) => void,
 *   composite:              () => void,
 *   drawTransformHandles:   () => void,
 *   reapplyTransform:       () => void,
 *   getTransformHandle:     (x: number, y: number) => string | null,
 *   cursorForHandle:        (id: string | null) => string,
 * }} deps
 */
import { state as legacyState } from '../state.js';
import { canvasCoords } from '../canvas-coords.js';

export function createTransformDragTool({ state = legacyState, root = null, lifetime = null,
  beginMove, composite, drawTransformHandles, reapplyTransform,
  getTransformHandle, cursorForHandle,
}) {
  if (state !== legacyState && (!root || typeof lifetime?.add !== 'function')) throw new TypeError('isolated tool requires root and lifetime');
  let disposed = false;
  let dragW = 0, dragH = 0;
  lifetime?.add(() => { disposed = true; state.transformHandle = null; });
  const tool = {
    /**
     * Called on pointerdown. Returns true if the transform tool handled
     * the event (the dispatcher should NOT fall through to other tools).
     */
    tryBegin(e) {
      if (!state.transformActive) return false;
      const coords = canvasCoords(e, state.mainCanvas);
      state.transformHandle = getTransformHandle(coords.x, coords.y);
      if (state.transformHandle) {
        state.transformStartX = coords.x;
        state.transformStartY = coords.y;
        // Snapshot offset + size at drag-start so each frame computes
        // "start + dx" (correct delta) rather than accumulating off the
        // running offset, which was making top/left grabs drift.
        const layer = state.transformLayer;
        const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
        state.transformStartOffX = off.x;
        state.transformStartOffY = off.y;
        dragW = layer.canvas.width;
        dragH = layer.canvas.height;
        return true;
      }
      // No corner hit — if click inside the layer's bounding box, act
      // like Move so the user can drag the layer around without
      // switching tools.
      if (state.transformLayer) {
        const off = state.layerOffsets.get(state.transformLayer.id) || { x: 0, y: 0 };
        const w = state.transformLayer.canvas.width;
        const h = state.transformLayer.canvas.height;
        if (coords.x >= off.x && coords.x <= off.x + w &&
            coords.y >= off.y && coords.y <= off.y + h) {
          beginMove(e,{borrowed:true});
          return true;
        }
      }
      return false;
    },

    /**
     * Called on pointermove. Returns true if handled.
     *
     * When transformActive but no handle is grabbed, updates the
     * hover cursor + pulse. When a handle is grabbed, drives the
     * resize / rotation pipeline.
     */
    tryContinue(e) {
      if (!state.transformActive) return false;
      // No drag in progress — just hover-cursor + pulse.
      if (!state.transformHandle && state.mainCanvas) {
        const coords = canvasCoords(e, state.mainCanvas);
        const hovered = getTransformHandle(coords.x, coords.y);
        state.mainCanvas.style.cursor = hovered ? cursorForHandle(hovered) : 'default';
        if (hovered !== state.hoveredHandle) {
          state.hoveredHandle = hovered;
          composite();
        }
        return false; // didn't fully consume the event
      }
      if (!state.transformHandle) return false;
      e.preventDefault();
      const coords = canvasCoords(e, state.mainCanvas);
      // Rotation grip — angle measured from the layer's geometric
      // centre to the cursor. Mirror into the popup if it's open.
      if (state.transformHandle === 'rot') {
        const layer = state.transformLayer;
        const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
        const cx = off.x + layer.canvas.width / 2;
        const cy = off.y + layer.canvas.height / 2;
        const rad = Math.atan2(coords.y - cy, coords.x - cx) + Math.PI / 2;
        let deg = Math.round((rad * 180) / Math.PI);
        if (e.shiftKey) deg = Math.round(deg / 15) * 15; // 15° snap
        while (deg > 180) deg -= 360;
        while (deg <= -180) deg += 360;
        state.transformPendingRot = deg;
        reapplyTransform();
        if (state.transformPopup) {
          const rotIn = state.transformPopup.querySelector(':is(#ge-transform-rot,[data-ge-id="ge-transform-rot"])');
          if (rotIn) rotIn.value = String(deg);
        }
        return true;
      }
      // Resize via corner / edge handle.
      const dx = coords.x - state.transformStartX;
      const dy = coords.y - state.transformStartY;
      const layer = state.transformLayer;
      let newW = layer.canvas.width;
      let newH = layer.canvas.height;
      if (state.transformHandle.includes('r')) newW = dragW + dx;
      if (state.transformHandle.includes('l')) newW = dragW - dx;
      if (state.transformHandle.includes('b')) newH = dragH + dy;
      if (state.transformHandle.includes('t')) newH = dragH - dy;
      // Shift = lock aspect ratio. Use whichever axis moved more
      // (relative to the original) as the driver.
      if (e.shiftKey && dragW > 0 && dragH > 0) {
        const aspect = dragW / dragH;
        const wDelta = Math.abs(newW - dragW);
        const hDelta = Math.abs(newH - dragH);
        if (wDelta >= hDelta) {
          newH = Math.max(1, Math.round(newW / aspect));
        } else {
          newW = Math.max(1, Math.round(newH * aspect));
        }
      }
      newW = Math.max(1, Math.round(newW));
      newH = Math.max(1, Math.round(newH));
      // Route through the popup-driven pipeline so popup + drag stay
      // in sync. Anchor the opposite corner via transformOrigOffset so
      // handles don't slide while the user drags.
      state.transformPendingW = newW;
      state.transformPendingH = newH;
      const anchorOffX = state.transformStartOffX +
        (state.transformHandle.includes('l') ? (dragW - newW) : 0);
      const anchorOffY = state.transformStartOffY +
        (state.transformHandle.includes('t') ? (dragH - newH) : 0);
      state.transformOrigOffset = {
        x: anchorOffX + newW / 2 - state.transformOrigW / 2,
        y: anchorOffY + newH / 2 - state.transformOrigH / 2,
      };
      reapplyTransform();
      // Mirror the new W/H into the popup if it's open.
      if (state.transformPopup) {
        const wIn = state.transformPopup.querySelector(':is(#ge-transform-w,[data-ge-id="ge-transform-w"])');
        const hIn = state.transformPopup.querySelector(':is(#ge-transform-h,[data-ge-id="ge-transform-h"])');
        if (wIn) wIn.value = String(state.transformPendingFlipH ? -newW : newW);
        if (hIn) hIn.value = String(state.transformPendingFlipV ? -newH : newH);
      }
      return true;
    },

    /**
     * Called on pointerup. Returns true if handled.
     */
    tryEnd() {
      if (!(state.transformActive && state.transformHandle)) return false;
      state.transformHandle = null;
      composite();
      drawTransformHandles();
      return true;
    },
  };
  for (const key of Object.keys(tool)) {
    const invoke = tool[key];
    tool[key] = (...args) => disposed ? false : invoke(...args);
  }
  return tool;
}
