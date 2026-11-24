/**
 * Misc AI-tool wiring — the three AI tools that don't share the
 * inpaint pipeline:
 *
 *   Harmonize: Reinhard color transfer on a body mask (no AI redraw)
 *              + an optional narrow inpaint on a seam mask if the
 *              "Seam fix" slider > 0.
 *   Canvas 2×/4× Upscale: in-browser bicubic resampling, no server.
 *   AI Upscale and Style Transfer: managed image operations selected from
 *   the owner's normalized Images routes.
 *
 * Plus the small `_addEmptyLayer` helper and its toolbar wiring,
 * since it lived next to these.
 *
 * @param {{
 *   apiBase:             string,
 *   buildLayerBodyMask:  (featherPx: number) => string | null,
 *   buildSeamMask:       (featherPx: number) => string | null,
 *   applyImageTool:      (endpoint, payload, layerName, btn, opts?) => Promise<void>,
 *   flatten:             () => HTMLCanvasElement,
 *   saveState:           (label?: string) => void,
 *   fitZoom:             () => void,
 *   composite:           () => void,
 *   createLayer:         (name, w, h) => object,
 *   renderLayerPanel:    () => void,
 *   spinnerModule:       object,
 *   uiModule:            object,
 * }} deps
 *
 * @returns {{ addEmptyLayer: () => void }}
 */
import { editorIdSelector } from './dom-identity.js';
import { state as legacyState } from './state.js';

export function wireAIToolsMisc({ state = legacyState, root = null, lifetime = null, captureOrigin = null,
  apiBase, buildLayerBodyMask, buildSeamMask, applyImageTool,
  flatten, saveState, fitZoom, composite, createLayer, renderLayerPanel,
  spinnerModule, uiModule, scaleCanvas = () => false, withSceneMutation = (_name, fn) => fn(), markSceneChanged = () => {}, resultHooks = null, beginMutation = () => true, endMutation = () => {}, canCapture = () => true,
}) {
  if (state !== legacyState && (!root || !lifetime || typeof captureOrigin !== 'function')) throw new TypeError('isolated AI wiring requires ownership');
  captureOrigin ||= () => () => 'current';
  const ownerRoot = root || state.container;
  const ownerDocument = ownerRoot?.ownerDocument || globalThis.document;
  const find = id => ownerRoot?.querySelector(editorIdSelector(id));
  const removers = [];
  const listen = (node, type, fn) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded); removers.push(() => node.removeEventListener(type, guarded)); };
  lifetime?.add(() => { for (const remove of removers.splice(0)) remove(); });

  const decode = (data, controller) => new Promise((resolve, reject) => {
    const image = new Image();
    const finish = (error) => { controller.signal.removeEventListener('abort', abort); image.onload = null; image.onerror = null; error ? reject(error) : resolve(image); };
    const abort = () => { image.src = ''; finish(Object.assign(new Error('retired'), {name:'AbortError'})); };
    if (controller.signal.aborted) return abort();
    controller.signal.addEventListener('abort', abort, {once:true});
    image.onload = () => finish(); image.onerror = () => finish(new Error('Failed to decode result'));
    image.src = 'data:image/png;base64,' + data;
  });
  // ── Harmonize sliders — Color match + Seam fix ──
  const harmColorPrev = find('ge-harmonize-color-preview');
  const harmSeamPrev = find('ge-harmonize-seam-preview');
  listen(find('ge-harmonize-color'), 'input', (e) => {
    find('ge-harmonize-color-label').textContent = (e.target.value / 100).toFixed(2);
    if (harmColorPrev) harmColorPrev.style.opacity = (parseInt(e.target.value, 10) / 100).toFixed(2);
  });
  listen(find('ge-harmonize-seam'), 'input', (e) => {
    find('ge-harmonize-seam-label').textContent = (e.target.value / 100).toFixed(2);
    if (harmSeamPrev) harmSeamPrev.style.opacity = (parseInt(e.target.value, 10) / 100).toFixed(2);
  });

  // Harmonize button — two-stage:
  //   1) Reinhard color transfer on body mask (no AI redraw)
  //   2) Optional narrow inpaint on seam mask (if seam_fix > 0)
  listen(find('ge-harmonize-run'), 'click', () => {
    if (!canCapture()) {uiModule?.showToast('Finish the current image edit first.'); return;}
    const prompt = find('ge-harmonize-prompt')?.value?.trim() || 'photorealistic, natural lighting, seamless blend';
    const color_match = (parseInt(find('ge-harmonize-color')?.value || '65')) / 100;
    const seam_fix = (parseInt(find('ge-harmonize-seam')?.value || '0')) / 100;
    const bodyFeather = Math.max(6, Math.round(Math.min(state.imgWidth, state.imgHeight) * 0.012));
    const seamFeather = Math.max(8, Math.round(Math.min(state.imgWidth, state.imgHeight) * 0.015));
    const body_mask = buildLayerBodyMask(bodyFeather);
    const seam_mask = seam_fix > 0.01 ? buildSeamMask(seamFeather) : null;
    // Harmonize needs a non-base layer to color-match against the
    // background. Without one, the server would fall back to legacy
    // whole-image img2img — i.e. regenerate the whole photo. Block
    // that and tell the user what's missing.
    if (!body_mask) {
      if (uiModule) uiModule.showToast('Harmonize needs a second layer pasted/imported over the base photo — nothing to color-match against.', 6000);
      return;
    }
    const payload = { prompt, color_match, seam_fix, body_mask };
    if (seam_mask) payload.seam_mask = seam_mask;
    applyImageTool('/api/image/harmonize', payload, 'Harmonized', find('ge-harmonize-run'));
  });

  // ── Canvas upscale (bicubic) ──
  function canvasUpscale(factor) {
    try { if (!scaleCanvas(factor)) { uiModule.showToast('Upscale could not be applied.'); return; } uiModule.showToast(`Upscaled ${factor}× to ${state.imgWidth}×${state.imgHeight}`); }
    catch (error) { uiModule.showToast(error?.message || 'Upscale could not be applied.'); }
  }
  listen(find('ge-upscale-2x'), 'click', () => canvasUpscale(2));
  listen(find('ge-upscale-4x'), 'click', () => canvasUpscale(4));

  // ── AI upscale (Real-ESRGAN, no diffusion server required) ──
  listen(find('ge-upscale-ai'), 'click', async () => {
    if (!canCapture()) {uiModule?.showToast('Finish the current image edit first.'); return;}
    const check = captureOrigin(), controller = new AbortController();
    let resultId=null, submitted=false, received=false;
    let upWp = null, spinnerCleaned = false;
    const cleanSpinner = () => { if (spinnerCleaned) return; spinnerCleaned = true; try { upWp?.destroy(); } catch {} };
    const unregister = lifetime?.add(() => { cleanSpinner(); controller.abort(); }) || (() => {});
    const btn = find('ge-upscale-ai');
    const origHTML = btn.innerHTML;
    btn.disabled = true;
    try {
      upWp = spinnerModule.createWhirlpool(14);
      upWp.element.style.cssText = 'display:inline-block;vertical-align:middle;position:relative;top:1px;margin-right:6px;width:14px;height:14px;';
      btn.innerHTML = '';
      btn.appendChild(upWp.element);
      const lbl = ownerDocument.createElement('span');
      lbl.textContent = 'Upscaling…';
      btn.appendChild(lbl);
    } catch (_) { btn.textContent = 'Upscaling…'; }
    try {
      resultId=resultHooks?.reserve();
      const flat = flatten();
      const imageB64 = flat.toDataURL('image/png').split(',')[1];
      if (resultId) {resultHooks.submit(resultId); submitted=true;}
      const res = await fetch('/api/image/upscale-local', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image: imageB64, scale: 2 }),
      });
      if (!res.ok) {if (resultId) {resultHooks.reject(resultId); submitted=false;} throw new Error('Server returned ' + res.status);}
      const data = await res.json();
      if (data.image) {
        if (resultId) {resultHooks.receive(resultId,{bytes:data.image,mime:'image/png',metadata:{label:'AI Upscaled',fullSize:true}}); received=true;}
        if (check() !== 'current' || lifetime?.disposed) {if (!lifetime?.disposed && check()!=='retired') uiModule?.showToast('AI result retained for this image.'); return;}
        const img = await decode(data.image, controller);
        if (check() !== 'current') throw new Error('Image changed while AI was working; run it again.');
        const token=beginMutation(); if (!token) {uiModule?.showToast('AI result retained for this image.'); return;}
        try {
          const newW = img.width, newH = img.height;
          if (!Number.isSafeInteger(newW) || !Number.isSafeInteger(newH) || newW<1 || newH<1 || newW>16384 || newH>16384 || newW*newH>67108864) throw new Error('AI result exceeds canvas limits');
          saveState();
          const layer = createLayer('AI Upscaled', newW, newH);
          layer.ctx.drawImage(img, 0, 0);
          state.layers.push(layer);
          state.activeLayerId = layer.id;
          markSceneChanged(); if (resultId) resultHooks.resolve(resultId);
          composite();
          renderLayerPanel();
          uiModule.showToast('AI upscaled image added as a layer');
        } finally {endMutation(token);}

      } else {
        throw new Error(data.error || 'No image returned');
      }
    } catch (e) {
      if (resultId && !received) {try {if (submitted) resultHooks.unknown(resultId); else resultHooks.resolve(resultId);} catch (_) {}}
      if (!lifetime?.disposed && check() !== 'retired' && e.name !== 'AbortError') uiModule.showToast(received ? 'AI result retained for this image.' : 'AI upscale failed: ' + e.message);
    } finally { unregister(); cleanSpinner(); if (!lifetime?.disposed && check() !== 'retired') { btn.disabled = false; btn.innerHTML = origHTML; } }
  });

  // ── Style transfer ──
  listen(find('ge-style-strength'), 'input', (e) => {
    find('ge-style-strength-label').textContent = (parseInt(e.target.value) / 100).toFixed(2);
  });
  listen(find('ge-style-run'), 'click', async () => {
    if (!canCapture()) {uiModule?.showToast('Finish the current image edit first.'); return;}
    const check = captureOrigin(), controller = new AbortController();
    let resultId=null, submitted=false, received=false;
    const unregister = lifetime?.add(() => controller.abort()) || (() => {});
    const selectedRoute = ownerRoot.querySelector('select[data-ge-tool-model="style"]')?.value || '';
    const btn = find('ge-style-run');
    const prompt = find('ge-style-prompt').value.trim();
    if (!prompt) { unregister(); uiModule.showToast('Enter a style prompt'); return; }
    const strength = parseInt(find('ge-style-strength').value) / 100;
    btn.disabled = true; btn.textContent = 'Applying...';
    try {
      resultId=resultHooks?.reserve();
      const flat = flatten();
      const blob = await new Promise(r => flat.toBlob(r, 'image/png'));
      if (check() !== 'current') throw new Error('Image changed while AI was working; run it again.');
      const fd = new FormData();
      fd.append('image', blob, 'style.png');
      fd.append('prompt', prompt);
      fd.append('strength', String(strength));
      if (selectedRoute) fd.append('model_route_id', selectedRoute);
      if (resultId) {resultHooks.submit(resultId); submitted=true;}
      const res = await fetch(`${apiBase}/api/imps/style-transfer`, { method: 'POST', credentials: 'same-origin', body: fd, signal: controller.signal });
      if (!res.ok) {if (resultId) {resultHooks.reject(resultId); submitted=false;} throw new Error('Server returned ' + res.status);}
      const data = await res.json();
      if (data.image) {
        if (resultId) {resultHooks.receive(resultId,{bytes:data.image,mime:'image/png',metadata:{label:'Styled result'}}); received=true;}
        if (check() !== 'current' || lifetime?.disposed) {if (!lifetime?.disposed && check()!=='retired') uiModule?.showToast('AI result retained for this image.'); return;}
        const img = await decode(data.image, controller);
        if (check() !== 'current') throw new Error('Image changed while AI was working; run it again.');
        const token=beginMutation(); if (!token) {uiModule?.showToast('AI result retained for this image.'); return;}
        try {
          saveState();
          const layer = createLayer('Styled: ' + prompt.substring(0, 20), state.imgWidth, state.imgHeight);
          layer.ctx.drawImage(img, 0, 0, state.imgWidth, state.imgHeight);
          state.layers.push(layer);
          markSceneChanged();
          if (resultId) resultHooks.resolve(resultId);
          state.activeLayerId = layer.id;
          composite();
          renderLayerPanel();
          uiModule.showToast('Style applied');
        } finally {endMutation(token);}

      } else {
        throw new Error(data.error || 'No image returned');
      }
    } catch (e) {
      if (resultId && !received) {try {if (submitted) resultHooks.unknown(resultId); else resultHooks.resolve(resultId);} catch (_) {}}
      if (!lifetime?.disposed && check() !== 'retired' && e.name !== 'AbortError') uiModule.showToast(received ? 'AI result retained for this image.' : 'Style transfer failed: ' + e.message);
    } finally { unregister(); if (!lifetime?.disposed && check() !== 'retired') { btn.disabled = false; btn.textContent = 'Apply Style'; } }
  });

  // ── Add empty layer (used by the layer-panel header button + the
  // Ctrl+Alt+J keyboard shortcut). Returned so keyboard-shortcuts.js
  // can call it through the same path. ──
  function addEmptyLayer() {
    return withSceneMutation('add-empty-layer',()=>{
      saveState('Add layer');
      const layer = createLayer('Layer ' + state.layers.length, state.imgWidth, state.imgHeight);
      state.layers.push(layer); state.activeLayerId = layer.id;
      markSceneChanged(); renderLayerPanel(); composite(); return true;
    });
  }
  listen(find('ge-add-layer'), 'click', addEmptyLayer);

  return { addEmptyLayer };
}
