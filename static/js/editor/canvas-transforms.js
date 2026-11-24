/** Whole-document transforms, staged before an owned scene mutation. */
import { state as legacyState } from './state.js';
import { validateInpaintSidecarGeometry } from './imageSessionPersistence.js';

export function createCanvasTransforms({ saveState, composite, fitZoom, showCanvasLoading, hideCanvasLoading, markSceneChanged = () => {}, withSceneMutation = (_name, fn) => fn(), captureOrigin = () => null, originCurrent = () => true, clearPreview = () => {}, onResize = () => {}, state = legacyState, root = null, lifetime = null }) {
  if (state !== legacyState && (!root || !lifetime)) throw new TypeError('isolated canvas transforms require root and lifetime');
  const currentRoot = () => root || state.container;
  const ownerDocument = () => currentRoot()?.ownerDocument || document;
  const activeLifetime = () => typeof lifetime === 'function' ? lifetime() : lifetime;
  const isLive = () => { const owner = activeLifetime(); if (state !== legacyState && typeof owner?.add !== 'function') throw new TypeError('isolated transform lifetime is unavailable'); return !owner?.disposed; };
  let pending = null;
  const stopPending = () => { if (!pending) return; const item = pending; pending = null; item.cancel?.(item.frame); item.release?.(); hideCanvasLoading(item.overlay); };
  const capture = () => ({origin:captureOrigin(), owner:activeLifetime(), layers:[...state.layers], width:state.imgWidth, height:state.imgHeight});
  const current = value => isLive() && value.owner === activeLifetime() && originCurrent(value.origin) && value.width === state.imgWidth && value.height === state.imgHeight && value.layers.length === state.layers.length && value.layers.every((layer,index) => state.layers[index] === layer);
  function stageCanvas(source, operation) {
    if (!source || !source.width || !source.height) throw new Error('Cannot transform an empty image canvas');
    const swap = operation === 90 || operation === 270;
    const target = ownerDocument().createElement('canvas'); target.width = swap ? source.height : source.width; target.height = swap ? source.width : source.height;
    const ctx = target.getContext('2d'); if (!ctx) throw new Error('Cannot create transform canvas');
    if (typeof operation === 'number') { ctx.translate(target.width / 2, target.height / 2); ctx.rotate(operation * Math.PI / 180); ctx.drawImage(source, -source.width / 2, -source.height / 2); }
    else { ctx.translate(operation === 'h' ? source.width : 0, operation === 'v' ? source.height : 0); ctx.scale(operation === 'h' ? -1 : 1, operation === 'v' ? -1 : 1); ctx.drawImage(source, 0, 0); }
    return target;
  }
  function apply(operation, snapshot) {
    if (!current(snapshot)) return false;
    return withSceneMutation('document-transform', () => {
      if (!current(snapshot)) return false;
      const {width:w,height:h,layers} = snapshot, swap = operation === 90 || operation === 270;
      const newW = swap ? h : w, newH = swap ? w : h;
      const radians = typeof operation === 'number' ? operation * Math.PI / 180 : 0, cos = Math.cos(radians), sin = Math.sin(radians);
      // Build all new pixels first. No history or source canvas changes if staging fails.
      const staged = layers.map(layer => {
        const canvas = stageCanvas(layer.canvas, operation), old = state.layerOffsets.get(layer.id) || {x:0,y:0};
        let offset;
        if (typeof operation === 'number') { const dx=old.x+layer.canvas.width/2-w/2, dy=old.y+layer.canvas.height/2-h/2; offset={x:Math.round(dx*cos-dy*sin+newW/2-canvas.width/2),y:Math.round(dx*sin+dy*cos+newH/2-canvas.height/2)}; }
        else offset=operation === 'h' ? {x:w-old.x-layer.canvas.width,y:old.y} : {x:old.x,y:h-old.y-layer.canvas.height};
        const masks=(layer.masks || []).map(mask => ({mask, canvas:stageCanvas(mask.canvas, operation)}));
        const src=layer.inpaintSource;
        const sidecar=src ? {...src, ai:stageCanvas(src.ai,operation), base:stageCanvas(src.base,operation), mask:stageCanvas(src.mask,operation)} : null;
        return {layer,canvas,offset,masks,sidecar};
      });
      const wand = state.wandMask ? stageCanvas(state.wandMask,operation) : null;
      const selectedMask = staged.flatMap(entry => entry.masks).find(item => item.mask.canvas === state.maskCanvas);
      const looseMask = state.maskCanvas && !selectedMask ? stageCanvas(state.maskCanvas,operation) : null;
      saveState(typeof operation === 'number' ? `Rotate ${operation}°` : operation === 'h' ? 'Flip horizontal' : 'Flip vertical');
      for (const entry of staged) {
        entry.layer.canvas=entry.canvas; entry.layer.ctx=entry.canvas.getContext('2d');
        entry.layer._adjCacheKey=null; entry.layer._adjFinalKey=null;
        state.layerOffsets.set(entry.layer.id,entry.offset);
        for (const item of entry.masks) { item.mask.canvas=item.canvas; item.mask.ctx=item.canvas.getContext('2d'); }
        if (entry.sidecar) entry.layer.inpaintSource=entry.sidecar;
      }
      if (selectedMask || looseMask) { state.maskCanvas=selectedMask?.canvas || looseMask; state.maskCtx=state.maskCanvas.getContext('2d'); }
      if (wand) { state.wandMask=wand; state.wandLastSeed=null; state.wandSrcCache=null; }
      state.imgWidth=newW; state.imgHeight=newH;
      if (state.mainCanvas) { state.mainCanvas.width=newW; state.mainCanvas.height=newH; }
      clearPreview();
      const sizeLabel=currentRoot()?.querySelector(':is(#ge-canvas-size,[data-ge-id="ge-canvas-size"])'); if (sizeLabel) sizeLabel.textContent=`${newW}×${newH}`;
      markSceneChanged(); if (swap) fitZoom(); composite(); return true;
    });
  }
  function scaleAll(factor) {
    if (![2,4].includes(factor) || !isLive() || !state.layers.length) return false;
    stopPending(); const snapshot=capture(), newW=snapshot.width*factor, newH=snapshot.height*factor;
    const valid=(w,h)=>Number.isSafeInteger(w)&&Number.isSafeInteger(h)&&w>0&&h>0&&w<=16384&&h<=16384&&w*h<=67108864;
    if (!valid(newW,newH)) return false;
    return withSceneMutation('document-upscale',()=>{
      if (!current(snapshot)) return false;
      const sources=[]; for(const layer of snapshot.layers) { sources.push(layer.canvas,...(layer.masks||[]).map(mask=>mask.canvas)); const src=layer.inpaintSource; if(src) { validateInpaintSidecarGeometry(src,factor); sources.push(src.ai,src.base,src.mask); } } if(state.wandMask)sources.push(state.wandMask); if(state.maskCanvas)sources.push(state.maskCanvas);
      if(sources.some(source=>!source || !valid(source.width*factor,source.height*factor))) throw new Error('Upscaled image exceeds canvas limits');
      const scale = source => { const w=source?.width*factor,h=source?.height*factor; if (!valid(w,h)) throw new Error('Upscaled image exceeds canvas limits'); const out=ownerDocument().createElement('canvas'); out.width=w;out.height=h; const ctx=out.getContext('2d'); if(!ctx) throw new Error('Cannot create upscale canvas'); ctx.imageSmoothingEnabled=true;ctx.imageSmoothingQuality='high';ctx.drawImage(source,0,0,w,h);return out; };
      const staged=snapshot.layers.map(layer=>({layer,canvas:scale(layer.canvas),offset:{x:(state.layerOffsets.get(layer.id)?.x || 0)*factor,y:(state.layerOffsets.get(layer.id)?.y || 0)*factor},masks:(layer.masks||[]).map(mask=>({mask,canvas:scale(mask.canvas)})),sidecar:layer.inpaintSource ? {...layer.inpaintSource,ai:scale(layer.inpaintSource.ai),base:scale(layer.inpaintSource.base),mask:scale(layer.inpaintSource.mask),padPx:layer.inpaintSource.padPx*factor} : null}));
      const wand=state.wandMask ? scale(state.wandMask) : null;
      const selected=staged.flatMap(item=>item.masks).find(item=>item.mask.canvas===state.maskCanvas);
      const loose=state.maskCanvas && !selected ? scale(state.maskCanvas) : null;
      saveState(`Upscale ${factor}×`);
      for (const item of staged) { item.layer.canvas=item.canvas; item.layer.ctx=item.canvas.getContext('2d'); item.layer._adjCacheKey=null;item.layer._adjFinalKey=null;state.layerOffsets.set(item.layer.id,item.offset); for(const mask of item.masks){mask.mask.canvas=mask.canvas;mask.mask.ctx=mask.canvas.getContext('2d');} if(item.sidecar)item.layer.inpaintSource=item.sidecar; }
      if (selected||loose) {state.maskCanvas=selected?.canvas || loose;state.maskCtx=state.maskCanvas.getContext('2d');}
      if (wand) {state.wandMask=wand;state.wandLastSeed=null;state.wandSrcCache=null;}
      state.imgWidth=newW;state.imgHeight=newH;if(state.mainCanvas){state.mainCanvas.width=newW;state.mainCanvas.height=newH;}
      clearPreview();const label=currentRoot()?.querySelector(':is(#ge-canvas-size,[data-ge-id="ge-canvas-size"])');if(label)label.textContent=`${newW}×${newH}`;
      markSceneChanged();fitZoom();composite();return true;
    });
  }
  function resizeBounds(width,height,snapshot) {
    const valid=(w,h)=>Number.isSafeInteger(w)&&Number.isSafeInteger(h)&&w>0&&h>0&&w<=16384&&h<=16384&&w*h<=67108864;
    if (!snapshot || !valid(width,height) || !current(snapshot) || (width===snapshot.width && height===snapshot.height)) return false;
    return withSceneMutation('resize-canvas-bounds',()=>{
      if (!current(snapshot)) return false;
      const all=snapshot.layers.flatMap(layer=>(layer.masks || []).map(mask=>({mask,canvas:mask.canvas})));
      const originals=new Set(all.map(item=>item.canvas));
      if(state.maskCanvas && !originals.has(state.maskCanvas)) all.push({mask:null,canvas:state.maskCanvas});
      const staged=all.map(item=>{const source=item.canvas; if (!source || !valid(source.width,source.height)) throw new Error('Invalid mask canvas'); const w=Math.max(source.width,width),h=Math.max(source.height,height); if (!valid(w,h)) throw new Error('Mask exceeds canvas limits'); return {...item,w,h};});
      for(const item of staged) if(item.w!==item.canvas.width || item.h!==item.canvas.height) {const next=ownerDocument().createElement('canvas');next.width=item.w;next.height=item.h;const ctx=next.getContext('2d');if(!ctx)throw new Error('Cannot create mask canvas');ctx.drawImage(item.canvas,0,0);item.next=next;}
      const active=staged.find(item=>item.canvas===state.maskCanvas);
      saveState('Resize canvas');
      for(const item of staged) if(item.mask && item.next) {item.mask.canvas=item.next;item.mask.ctx=item.next.getContext('2d');}
      if(active?.next){state.maskCanvas=active.next;state.maskCtx=active.next.getContext('2d');}
      state.imgWidth=width;state.imgHeight=height;if(state.mainCanvas){state.mainCanvas.width=width;state.mainCanvas.height=height;}
      clearPreview();const label=currentRoot()?.querySelector(':is(#ge-canvas-size,[data-ge-id="ge-canvas-size"])');if(label)label.textContent=`${width}×${height}`;
      markSceneChanged();onResize();fitZoom();composite();return true;
    });
  }
  return {
    scaleAll, captureBounds: capture, resizeBounds,
    rotateAll(deg) {
      if (![90,180,270].includes(deg) || !isLive() || !state.layers.length) return false;
      stopPending(); const snapshot=capture(), win=ownerDocument().defaultView;
      const frame=win?.requestAnimationFrame?.bind(win) || requestAnimationFrame, cancel=win?.cancelAnimationFrame?.bind(win) || cancelAnimationFrame;
      const overlay=showCanvasLoading('Rotating…');
      const item={frame:null,cancel,release:null,overlay}; pending=item;
      item.release=snapshot.owner?.add(() => { if (pending === item) stopPending(); }) || null;
      item.frame=frame(() => { if (pending !== item) return; pending=null; item.release?.(); try { apply(deg,snapshot); } finally { hideCanvasLoading(item.overlay); } });
      return true;
    },
    flipAll(axis) { if (!['h','v'].includes(axis) || !isLive() || !state.layers.length) return false; stopPending(); return apply(axis,capture()); },
  };
}
