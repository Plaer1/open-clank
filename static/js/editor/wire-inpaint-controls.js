import { editorIdSelector } from './dom-identity.js';
/**
 * Inpaint panel controls — the non-AI side-panel UI for the inpaint
 * tool (the AI Generate/Remove/Outpaint buttons live in
 * editor/ai-inpaint.js).
 *
 *   Pre-gen sliders (Feather + Strength swatch previews):
 *     #ge-strength-slider     just-updates-the-label-and-swatch
 *
 *   Post-gen live edge tuners — alpha-blur + dilate/erode on the most
 *   recent Inpaint Result layer, rAF-throttled so dragging stays
 *   smooth on big canvases:
 *     #ge-feather-slider       calls applyInpaintFeather + composite
 *     #ge-edgestroke-slider    same
 *
 *   Mask controls:
 *     #ge-mask-vis             toggle red-overlay visibility
 *     #ge-inpaint-invert       invert the active mask sub-layer
 *     #ge-inpaint-clear        wipe the active mask
 *     #ge-inpaint-mode-paint   set persistent paint mode
 *     #ge-inpaint-mode-erase   set persistent erase mode
 *
 *   Mask tint pickers (wired to keep both visually in sync):
 *     .ge-inpaint-mask-color   (inpaint section)
 *     #ge-topbar-mask-color    (topbar swatch — HSV picker attached)
 *
 * @param {{
 *   composite:                () => void,
 *   applyInpaintFeather:      (layer: object, featherPx: number, edgeShiftPx: number) => void,
 *   autoMatchInpaint:         () => void,
 *   syncToolClearIndicators:  () => void,
 *   attachColorPicker:        (el: HTMLInputElement) => void,
 *   uiModule:                 object,
 * }} deps
 */
import { state as legacyState } from './state.js';

const EYE_OPEN_SM = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF_SM  = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><line x1="8" y1="16" x2="16" y2="8"/><line x1="8" y1="8" x2="16" y2="16"/></svg>';

export function wireInpaintControls({ scopeIds = node => node, state = legacyState, root = null, lifetime = null,
  composite, markSceneChanged = () => {}, applyInpaintFeather, autoMatchInpaint, syncToolClearIndicators,
  captureOrigin = () => null, originCurrent = () => true, withSceneMutation = (_name,fn) => fn(), saveState = () => {},
  attachColorPicker, uiModule,
}) {
  if (state !== legacyState && (!root || !lifetime)) throw new TypeError('isolated controls require ownership');
  const ownerRoot = root || state.container;
  const win = ownerRoot?.ownerDocument?.defaultView || window;
  const find = id => ownerRoot?.querySelector(editorIdSelector(id));
  function syncInpaintAvailability() {
    const layer=state.layers.find(item=>item.id===state.lastInpaintLayerId), source=layer?.inpaintSource;
    const usable=!!source && layer.canvas?.width===state.imgWidth && layer.canvas?.height===state.imgHeight && ['ai','base','mask'].every(key=>source[key]?.width===state.imgWidth && source[key]?.height===state.imgHeight && typeof source[key]?.getContext==='function');
    for (const id of ['ge-inpaint-postfeather-row','ge-inpaint-edgestroke-row','ge-inpaint-automatch-row']) {const node=find(id); if (node) node.style.display=usable ? '' : 'none';}
    const hint=find('ge-inpaint-postedge-hint'); if (hint) hint.style.display=usable ? 'none' : '';
    const slider=find('ge-edgestroke-slider'); if (slider && usable) {slider.min=String(-source.padPx); slider.max=String(source.padPx);}
    return usable;
  }
  const removers = [];
  const listen = (node, type, fn) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded); removers.push(() => node.removeEventListener(type, guarded)); };
  const frames = new Set();
  const frame = fn => { const id = win.requestAnimationFrame(() => { frames.delete(id); if (!lifetime?.disposed) fn(); }); frames.add(id); return id; };
  lifetime?.add(() => { for (const id of frames) win.cancelAnimationFrame(id); for (const remove of removers.splice(0)) remove(); });

  // ── Feather + Strength preview swatches ──
  const featherPrev = find('ge-feather-preview');
  const strengthPrev = find('ge-strength-preview');
  function syncFeatherPreview(v) {
    if (!featherPrev) return;
    const inner = Math.max(0, 50 - v * 1.25);
    featherPrev.style.background = `radial-gradient(circle, var(--fg) 0%, var(--fg) ${inner}%, transparent 75%)`;
  }
  function syncStrengthPreview(v) {
    if (!strengthPrev) return;
    strengthPrev.style.opacity = (v / 100).toFixed(2);
  }

  // ── Post-inpaint live edge tuner ──
  // Alpha-blur (Feather) + dilate/erode (Edge Stroke) on the last
  // Inpaint Result layer. rAF-throttled so dragging stays smooth.
  let featherRafPending = false, featherRequest = null, featherGroup = null, featherEndPending = false;
  function scheduleInpaintEdgeRefresh() {
    const layer=state.layers.find(item=>item.id===state.lastInpaintLayerId);
    if (!layer || layer.locked || !syncInpaintAvailability()) return;
    featherRequest={layer, source:layer.inpaintSource, canvas:layer.canvas, origin:captureOrigin(), feather:parseInt(find('ge-feather-slider')?.value || '0',10), edge:parseInt(find('ge-edgestroke-slider')?.value || '0',10)};
    if (featherRafPending) return;
    featherRafPending=true;
    frame(() => {
      featherRafPending=false;
      const request=featherRequest; featherRequest=null;
      const current=()=>request && originCurrent(request.origin) && state.layers.includes(request.layer) && state.lastInpaintLayerId===request.layer.id && request.layer.inpaintSource===request.source && request.layer.canvas===request.canvas;
      if (!current()) return;
      withSceneMutation('inpaint-feather',()=>{
        if (!current() || request.layer.locked) return false;
        const staged=ownerRoot.ownerDocument.createElement('canvas'); staged.width=request.canvas.width; staged.height=request.canvas.height;
        const ctx=staged.getContext('2d'); applyInpaintFeather({...request.layer,canvas:staged,ctx},request.feather,request.edge);
        if (!featherGroup || featherGroup.layer !== request.layer || featherGroup.source !== request.source || featherGroup.revision !== state.sceneRevision) saveState('Adjust inpaint edge');
        request.layer.ctx.clearRect(0,0,staged.width,staged.height); request.layer.ctx.drawImage(staged,0,0);
        request.layer._adjCacheKey=null; request.layer._adjFinalKey=null; markSceneChanged(); featherGroup={layer:request.layer,source:request.source,revision:state.sceneRevision}; if (featherEndPending) { featherGroup=null; featherEndPending=false; } composite(); return true;
      });
    });
  }
  for (const id of ['ge-feather-slider','ge-edgestroke-slider']) {
    listen(find(id), 'pointerdown', () => { featherGroup=null; featherEndPending=false; });
    for (const event of ['change','pointercancel']) listen(find(id), event, () => { if (featherRafPending) featherEndPending=true; else featherGroup=null; });
  }
  listen(find('ge-feather-slider'), 'input', (e) => {
    const v = parseInt(e.target.value, 10);
    find('ge-feather-label').textContent = v + 'px';
    syncFeatherPreview(v);
    scheduleInpaintEdgeRefresh();
  });
  listen(find('ge-edgestroke-slider'), 'input', (e) => {
    const v = parseInt(e.target.value, 10);
    const label = find('ge-edgestroke-label');
    if (label) label.textContent = (v > 0 ? '+' : '') + v + 'px';
    const prev = find('ge-edgestroke-preview');
    if (prev) {
      // Visualise direction: dilate (+) → green, erode (−) → red.
      const dir = v === 0 ? 'transparent' : (v > 0 ? 'rgba(120,200,120,0.5)' : 'rgba(200,120,120,0.5)');
      prev.style.background = dir;
      prev.style.opacity = Math.min(1, Math.abs(v) / 80).toFixed(2);
    }
    scheduleInpaintEdgeRefresh();
  });
  listen(find('ge-strength-slider'), 'input', (e) => {
    find('ge-strength-label').textContent = (e.target.value / 100).toFixed(2);
    syncStrengthPreview(parseInt(e.target.value, 10));
  });
  listen(find('ge-inpaint-automatch'), 'click', () => {
    if (typeof autoMatchInpaint === 'function') autoMatchInpaint();
  });
  syncFeatherPreview(0);
  syncStrengthPreview(75);

  // ── Mask vis / invert / clear ──
  listen(find('ge-mask-vis'), 'click', () => {
    state.maskVisible = !state.maskVisible;
    const btn = find('ge-mask-vis');
    if (!btn) { composite(); return; }
    btn.innerHTML = `${state.maskVisible ? EYE_OPEN_SM : EYE_OFF_SM}<span id="ge-mask-vis-label">${state.maskVisible ? 'Hide' : 'Show'}</span>`;
    scopeIds(btn);
    btn.title = state.maskVisible ? 'Hide mask' : 'Show mask';
    btn.classList.toggle('visible', state.maskVisible);
    composite();
  });
  listen(find('ge-inpaint-invert'), 'click', () => {
    const canvas=state.maskCanvas, ctx=state.maskCtx, origin=captureOrigin();
    if (!canvas || !ctx) return;
    withSceneMutation('invert-inpaint-mask',()=>{
      if (!originCurrent(origin) || state.maskCanvas!==canvas || state.maskCtx!==ctx) return false;
      const imgData=ctx.getImageData(0,0,canvas.width,canvas.height), d=imgData.data;
      for (let i=0;i<d.length;i+=4) {const value=d[i+3]>0 ? 0 : 255; d[i]=value; d[i+1]=value; d[i+2]=value; d[i+3]=value;}
      saveState('Invert mask'); ctx.putImageData(imgData,0,0); markSceneChanged(); composite(); syncToolClearIndicators(); uiModule.showToast('Mask inverted'); return true;
    });
  });
  listen(find('ge-inpaint-clear'), 'click', () => {
    const canvas=state.maskCanvas, ctx=state.maskCtx, origin=captureOrigin();
    if (!canvas || !ctx) return;
    withSceneMutation('clear-inpaint-mask',()=>{
      if (!originCurrent(origin) || state.maskCanvas!==canvas || state.maskCtx!==ctx) return false;
      saveState('Clear mask'); ctx.clearRect(0,0,canvas.width,canvas.height); markSceneChanged(); composite(); syncToolClearIndicators(); return true;
    });
  });

  // ── Paint / Erase segmented toggle ──
  function setInpaintMode(eraseMode) {
    state.inpaintEraseMode = !!eraseMode;
    const paintBtn = find('ge-inpaint-mode-paint');
    const eraseBtn = find('ge-inpaint-mode-erase');
    if (paintBtn) paintBtn.classList.toggle('active', !state.inpaintEraseMode);
    if (eraseBtn) eraseBtn.classList.toggle('active', state.inpaintEraseMode);
  }
  listen(find('ge-inpaint-mode-paint'), 'click', () => setInpaintMode(false));
  listen(find('ge-inpaint-mode-erase'), 'click', () => setInpaintMode(true));

  // ── Mask color picker ──
  // Updates state.maskTintColor live so the user can pick a colour
  // that contrasts with their photo. Wire both the topbar picker AND
  // the inpaint-section picker so changing one syncs the other.
  function applyMaskTintFromHex(hex) {
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    state.maskTintColor = `rgba(${r}, ${g}, ${b}, 1)`;
    const inpaintPicker = ownerRoot.querySelector('.ge-inpaint-mask-color');
    const topbarPicker = find('ge-topbar-mask-color');
    if (inpaintPicker && inpaintPicker.value !== hex) inpaintPicker.value = hex;
    if (topbarPicker && topbarPicker.value !== hex) topbarPicker.value = hex;
    composite();
  }
  listen(ownerRoot.querySelector('.ge-inpaint-mask-color'), 'input', (e) => applyMaskTintFromHex(e.target.value));
  listen(find('ge-topbar-mask-color'), 'input', (e) => applyMaskTintFromHex(e.target.value));
  // Use the in-house HSV picker for the topbar swatch.
  const topbarMaskColor = find('ge-topbar-mask-color');
  if (topbarMaskColor) {
    try { attachColorPicker(topbarMaskColor); topbarMaskColor.value = topbarMaskColor.value; } catch {}
  }
  syncInpaintAvailability();
  return {syncInpaintAvailability};
}
