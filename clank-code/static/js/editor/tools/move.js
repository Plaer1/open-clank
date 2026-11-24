/**
 * Move tool — drag a layer around the canvas, with optional snap-on-Ctrl
 * to other layers' edges/centers and to canvas edges/center.
 *
 * Owns its own input handlers (begin/drag/end) and reads/writes the
 * shared `state` store directly. The factory takes a small dependency
 * bag for things that still live in imps.js — `activeLayer`,
 * `saveState`, `composite` — so this module doesn't have to know about
 * the orchestrator.
 *
 * @param {{
 *   activeLayer: () => {id: string, canvas: HTMLCanvasElement, locked?: boolean} | null,
 *   saveState:   (label?: string) => void,
 *   composite:   () => void,
 * }} deps
 * @returns {{ begin: (e: Event) => void, drag: (e: Event) => void, end: () => void }}
 */
import { state as legacyState } from '../state.js';
import { canvasCoords } from '../canvas-coords.js';
import { computeSnap as computeSnapImpl } from '../snap.js';

export function createMoveTool({ state = legacyState, root = null, lifetime = null, activeLayer, saveState, composite, markSceneChanged = () => {}, gesture = {begin:()=>true,owns:()=>true,end:()=>{}}, sceneRevision = () => state.sceneRevision, borrowTransform = () => null }) {
  function computeSnap(layer, nx, ny) {
    return computeSnapImpl(layer, nx, ny, {
      zoom: state.zoom,
      canvasW: state.imgWidth,
      canvasH: state.imgHeight,
      otherLayers: state.layers.map(l => ({
        visible: l.visible,
        id: l.id,
        canvas: l.canvas,
        offset: state.layerOffsets.get(l.id) || { x: 0, y: 0 },
      })),
    });
  }

  if (state !== legacyState && (!root || typeof lifetime?.add !== 'function')) throw new TypeError('isolated tool requires root and lifetime');
  let disposed = false;
  let moved = false;
  let target=null, revision=null, borrowed=null;
  const owned=() => borrowed ? borrowed.owns() : gesture.owns() && sceneRevision()===revision;
  const finish=() => {if (state.moving && moved && !borrowed && owned()) markSceneChanged(); if (!borrowed) gesture.end(); borrowed=null; target=null; moved=false; state.moving=false; state.activeSnapGuides=null;};
  lifetime?.add(() => { finish(); disposed = true; });
  const tool = {
    begin(e, options = {}) {
      const layer = activeLayer();
      if (!layer || layer.locked) return;
      if (state.moving) return;
      borrowed=options.borrowed ? borrowTransform(layer) : null;
      if (options.borrowed ? !borrowed : !gesture.begin()) {borrowed=null; return;}
      target=layer; revision=sceneRevision();
      try {
      if (!borrowed) saveState();
      state.moving = true;
      moved = false;
      const coords = canvasCoords(e, state.mainCanvas);
      const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
      state.moveStartX = coords.x;
      state.moveStartY = coords.y;
      state.moveLayerOffsetX = off.x;
      state.moveLayerOffsetY = off.y;
      } catch(error) {finish(); throw error;}
    },
    drag(e) {
      if (!state.moving || !owned() || !state.layers.includes(target)) return;
      e.preventDefault();
      const layer = target;
      const coords = canvasCoords(e, state.mainCanvas);
      const dx = coords.x - state.moveStartX;
      const dy = coords.y - state.moveStartY;
      let nx = state.moveLayerOffsetX + dx;
      let ny = state.moveLayerOffsetY + dy;
      // Ctrl held = snap to canvas edges/center and to every other
      // visible layer's edges/center. Opt-in to avoid a "sticky" feel
      // during normal drags.
      if (e.ctrlKey || e.metaKey) {
        const snapped = computeSnap(layer, nx, ny);
        nx = snapped.x;
        ny = snapped.y;
        state.activeSnapGuides = snapped.guides;
      } else {
        state.activeSnapGuides = null;
      }
      const previous = state.layerOffsets.get(layer.id) || {x:0, y:0};
      if (previous.x !== nx || previous.y !== ny) moved = true;
      state.layerOffsets.set(layer.id, { x: nx, y: ny });
      borrowed?.move(nx - state.moveLayerOffsetX, ny - state.moveLayerOffsetY);
      composite();
    },
    end() {
      finish();
    },
  };
  for (const key of Object.keys(tool)) {
    const invoke = tool[key];
    tool[key] = (...args) => disposed ? false : invoke(...args);
  }
  return tool;
}
