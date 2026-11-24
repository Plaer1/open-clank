/** Scene-only history pixel capture and restore; DOM views subscribe separately. */
import { createImageSceneAuthority } from './state.js';
import { cloneArtProject } from './artProjectMetadata.js';

// Private immutable backing is copied once per live sidecar identity, never PNG-encoded per history entry.
const sidecarHandles=new WeakMap(), sidecarBacking=new WeakMap();
const signature = source => [source.ai,source.base,source.mask,source.padPx,...['ai','base','mask'].flatMap(key=>[source[key].width,source[key].height])];
const remember = (source,token) => sidecarHandles.set(source,{token,signature:signature(source)});
function copyCanvas(source, document) {const canvas=document.createElement('canvas'); canvas.width=source.width; canvas.height=source.height; canvas.getContext('2d').drawImage(source,0,0); return canvas;}
function captureSidecar(source) {
  if (!source || !['ai','base','mask'].every(key=>source[key]?.getContext)) return null;
  const prior=sidecarHandles.get(source), current=signature(source); if (prior && current.every((value,index)=>value===prior.signature[index])) return prior.token;
  const document=source.ai.ownerDocument || globalThis.document; if (!document?.createElement) throw new Error('Inpaint history canvas is unavailable');
  const token=Object.freeze({}); const backing=Object.freeze({padPx:source.padPx,...Object.fromEntries(['ai','base','mask'].map(key=>[key,copyCanvas(source[key],document)]))});
  remember(source,token); sidecarBacking.set(token,backing); return token;
}
function restoreSidecar(token,document) {
  const backing=token && sidecarBacking.get(token); if (!backing) return null;
  const source=Object.freeze({padPx:backing.padPx,...Object.fromEntries(['ai','base','mask'].map(key=>[key,copyCanvas(backing[key],document)]))}); remember(source,token); return source;
}

export function createImageScene({state, document, limit, persistence = null, onChange = () => {}, ...ownership}) {
  if (!state || !document?.createElement) throw new TypeError('image scene dependencies required');
  const scene = createImageSceneAuthority({state, ...ownership, onChange:revision => { state.editEpoch += 1; onChange(revision); },
    ...(persistence ? {isDirty:() => persistence.dirty,canDispose:() => persistence.canDispose(),onDispose:() => clearImageScene(state)} : {})});
  scene.installHistory({limit, capture:() => captureImageScene(state),
    restore:snapshot => { const dimsChanged = restoreImageScene(state,snapshot,document); scene.project({kind:'historyRestore',snapshot,dimsChanged}); },
    onChanged:() => scene.project({kind:'historyChanged'}),
  });
  return scene;
}

function clearImageScene(state) {
  if (state.persistTimer) { clearTimeout(state.persistTimer); state.persistTimer=null; }
  state.layers=[]; state.undoStack=[]; state.redoStack=[]; state.layerOffsets.clear();
  state.wandMask=null; state.wandLayerId=null; state.wandLastSeed=null; state.lastInpaintLayerId=null; state._editableExtras=null;
  state.imageId=null; state.imageRef=null; state.managedProvider=null; state.managedResourceId=null; state.projectId=null; state.projectRevision=0; state.expectedImageRevision='';
}
export function captureImageScene(state) {
  let wand = null;
  if (state.wandMask) {
    try {
      const wctx = state.wandMask.getContext('2d');
      wand = {
        layerId: state.wandLayerId,
        w: state.wandMask.width,
        h: state.wandMask.height,
        imageData: wctx.getImageData(0, 0, state.wandMask.width, state.wandMask.height),
        seed: state.wandLastSeed ? { ...state.wandLastSeed } : null,
      };
    } catch {}
  }
  const extras = state._editableExtras;
  const artProjectPresent = extras != null && Object.prototype.hasOwnProperty.call(extras, 'artProject');
  const artProjectSnapshot = artProjectPresent
    ? { present: true, value: cloneArtProject(extras.artProject) }
    : { present: false };
  return {
    artProjectSnapshot,
    imgWidth: state.imgWidth,
    imgHeight: state.imgHeight,
    lastInpaintLayerId:state.lastInpaintLayerId || null,
    wand,
    layers: state.layers.map(l => {
      // getImageData throws on a 0-sized canvas — guard so a single
      // broken layer/mask can't take down the whole snapshot (which
      // would silently break undo/redo for brush strokes etc.).
      let imageData = null;
      try {
        if (l.canvas.width > 0 && l.canvas.height > 0) {
          imageData = l.ctx.getImageData(0, 0, l.canvas.width, l.canvas.height);
        }
      } catch (_) { /* keep imageData=null, restore will skip */ }
      return {
        id: l.id, name: l.name, visible: l.visible, opacity: l.opacity, locked: l.locked,
        canvasW: l.canvas.width,
        canvasH: l.canvas.height,
        imageData,
        inpaintHandle:captureSidecar(l.inpaintSource),
        editableExtras:l._editableExtras || null,
        offset: { ...(state.layerOffsets.get(l.id) || { x: 0, y: 0 }) },
        // Deep-clone defensively — a non-serializable / circular value here
        // would throw out of the whole snapshot (and historically aborted
        // every mutating op). Fall back to [] rather than blow up.
        adjLayers: (() => {
          try { return l.adjLayers ? JSON.parse(JSON.stringify(l.adjLayers)) : []; }
          catch (e) { console.error('[imps] adjLayers not serializable, dropping from snapshot:', e); return []; }
        })(),
        masks: (l.masks || []).map(m => {
          let mImageData = null;
          try {
            if (m.canvas.width > 0 && m.canvas.height > 0) {
              mImageData = m.ctx.getImageData(0, 0, m.canvas.width, m.canvas.height);
            }
          } catch (_) {}
          return {
            id: m.id,
            name: m.name,
            visible: m.visible !== false,
            canvasW: m.canvas.width,
            canvasH: m.canvas.height,
            imageData: mImageData,
          };
        }),
        activeMaskId: l.activeMaskId || null,
        isBase: !!l.isBase,
      };
    }),
  };
}


export function restoreImageScene(state, snap, document) {
  // Clone before changing the scene so a failed copy cannot partly restore it.
  // Older snapshots have no marker and leave current metadata untouched.
  const hasArtProjectSnapshot = Object.prototype.hasOwnProperty.call(snap, 'artProjectSnapshot');
  let artProjectSnapshot = null;
  if (hasArtProjectSnapshot) {
    const saved = snap.artProjectSnapshot;
    if (!saved || typeof saved !== 'object' || Array.isArray(saved) || typeof saved.present !== 'boolean' ||
        Object.keys(saved).sort().join(',') !== (saved.present ? 'present,value' : 'present'))
      throw new TypeError('Invalid ArtProject history snapshot');
    artProjectSnapshot = saved.present ? {present:true, value:cloneArtProject(saved.value)} : {present:false};
  }
  // Restore canvas dimensions first so layer imageData fits cleanly. This
  // is what makes Ctrl+Z work for crops (which change the main canvas
  // size) in addition to paint strokes.
  const dimsChanged = snap.imgWidth && snap.imgHeight &&
    (snap.imgWidth !== state.imgWidth || snap.imgHeight !== state.imgHeight);
  if (snap.imgWidth && snap.imgHeight) {
    state.imgWidth = snap.imgWidth;
    state.imgHeight = snap.imgHeight;
  }
  const layerStates = snap.layers || snap;
  // Rebuild the state.layers array from the snapshot order. This lets Ctrl+Z
  // restore deleted layers (previously the loop only updated existing
  // ones and silently dropped any layer the snapshot still knew about).
  // Layers absent from the snapshot are dropped — that's the desired
  // behavior for undoing an "+Add" or a paste.
  const _existingById = new Map(state.layers.map(l => [l.id, l]));
  const _rebuilt = [];
  for (const s of layerStates) {
    let layer = _existingById.get(s.id);
    if (!layer) {
      // Layer was deleted (or merged away). Recreate it from the
      // snapshot's ImageData so Ctrl+Z brings it back.
      const c = document.createElement('canvas');
      c.width = s.canvasW || state.imgWidth;
      c.height = s.canvasH || state.imgHeight;
      layer = { id: s.id, name: s.name, canvas: c, ctx: c.getContext('2d'),
                visible: true, opacity: 1, locked: false };
    } else {
      _existingById.delete(s.id);
    }
    layer.name = s.name;
    layer.visible = s.visible;
    layer.opacity = s.opacity;
    layer.locked = s.locked;
    if (s.canvasW && s.canvasH) {
      layer.canvas.width = s.canvasW;
      layer.canvas.height = s.canvasH;
    }
    try { if (s.imageData) layer.ctx.putImageData(s.imageData, 0, 0); } catch (_) {}
    layer.inpaintSource=restoreSidecar(s.inpaintHandle,document);
    layer._editableExtras=s.editableExtras || null;
    state.layerOffsets.set(layer.id, { ...s.offset });
    // Restore adjustment sub-layers + invalidate the composite cache
    // so the live render reflects the rolled-back FX state.
    layer.adjLayers = s.adjLayers ? JSON.parse(JSON.stringify(s.adjLayers)) : [];
    if (s.isBase !== undefined) layer.isBase = s.isBase;
    // Restore mask sub-layers — rebuild each mask's canvas from the
    // snapshot's imageData. We don't reuse old mask canvases (snapshot
    // dims might differ after a transform) so a fresh canvas is safer.
    layer.masks = (s.masks || []).map(ms => {
      const mc = document.createElement('canvas');
      mc.width = ms.canvasW || state.imgWidth;
      mc.height = ms.canvasH || state.imgHeight;
      const mctx = mc.getContext('2d');
      try { if (ms.imageData) mctx.putImageData(ms.imageData, 0, 0); } catch {}
      return { id: ms.id, name: ms.name, canvas: mc, ctx: mctx, visible: ms.visible !== false };
    });
    layer.activeMaskId = s.activeMaskId || (layer.masks[0]?.id ?? null);
    layer._adjFinal = null;
    layer._adjFinalKey = null;
    layer._stagedAdj = null;
    layer._editingAdjId = null;
    _rebuilt.push(layer);
  }
  // Drop any layer that's no longer in the snapshot.
  for (const lost of _existingById.values()) state.layerOffsets.delete(lost.id);
  state.layers = _rebuilt;
  state.lastInpaintLayerId=_rebuilt.some(layer=>layer.id===snap.lastInpaintLayerId && layer.inpaintSource) ? snap.lastInpaintLayerId : null;
  if (!state.layers.find(l => l.id === state.activeLayerId) && state.layers.length) {
    state.activeLayerId = state.layers[state.layers.length - 1].id;
  }
  // Restore wand selection (or clear it if the snapshot had none).
  if (snap.wand && snap.wand.imageData) {
    const mc = document.createElement('canvas');
    mc.width = snap.wand.w;
    mc.height = snap.wand.h;
    mc.getContext('2d').putImageData(snap.wand.imageData, 0, 0);
    state.wandMask = mc;
    state.wandLayerId = snap.wand.layerId;
    state.wandLastSeed = snap.wand.seed ? { ...snap.wand.seed } : null;
  } else {
    state.wandMask = null;
    state.wandLayerId = null;
    state.wandLastSeed = null;
  }
  if (hasArtProjectSnapshot) {
    if (artProjectSnapshot.present) {
      if (state._editableExtras == null) state._editableExtras = {};
      state._editableExtras.artProject = artProjectSnapshot.value;
    } else if (state._editableExtras != null) {
      delete state._editableExtras.artProject;
    }
  }
  return dimsChanged;
}

