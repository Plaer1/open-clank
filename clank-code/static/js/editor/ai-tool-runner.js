/**
 * Shared AI-tool runner. Used by Sharpen / Harmonize / Upscale / Style /
 * Bg-Remove / etc. — every tool that flattens the document, POSTs the
 * PNG to a server-side image endpoint, and drops the result back in
 * as a new layer.
 *
 * Handles all the orchestration around the request:
 *
 *  - Button busy state: swap label for "<verbing>…" + whirlpool
 *    spinner, lock width so the button doesn't visually jump.
 *  - Stable managed model-route selection from the tool's own picker (or
 *    the global fallback). Provider URLs and credentials never enter it.
 *  - Response handling: decode the returned PNG, push it as a new
 *    layer, save state, composite, refresh the layer panel.
 *  - Error reporting: surface failures via toast. Detects "needs
 *    img2img server" and "package not installed" failure modes and
 *    surfaces an action-toast that opens Cookbook to fix.
 *
 * @param {{
 *   flatten:                    () => HTMLCanvasElement,
 *   saveState:                  (label?: string) => void,
 *   createLayer:                (name: string, w: number, h: number) => object,
 *   composite:                  () => void,
 *   renderLayerPanel:           () => void,
 *   deriveBusyLabel:            (layerName: string) => string,
 *   getSelectedAIEndpoint:      (type: string | null) => { modelRouteId?: string },
 *   openCookbookForDependency:  (pkg: string) => void,
 *   openCookbookForImg2img:     () => void,
 *   spinnerModule:              object,
 *   uiModule:                   object | null,
 * }} deps
 *
 * @returns {(endpoint: string, extraPayload: object, layerName: string, btn: HTMLButtonElement, opts?: { busyLabel?: string }) => Promise<void>}
 */
import { state as legacyState } from './state.js';

const KNOWN_DEPS = ['realesrgan', 'rembg'];

export function createApplyImageTool({
  flatten, saveState, createLayer, composite, renderLayerPanel,
  deriveBusyLabel, getSelectedAIEndpoint,
  openCookbookForDependency, openCookbookForImg2img,
  spinnerModule, uiModule, markSceneChanged = () => {}, state = legacyState, root = null, lifetime = null, captureOrigin = null, resultHooks = null, beginMutation = () => true, endMutation = () => {}, fetchImpl = globalThis.fetch, ImageClass = globalThis.Image,
}) {
  if (state !== legacyState && (!root || !lifetime || typeof captureOrigin !== 'function')) throw new TypeError('isolated AI runner requires ownership');
  captureOrigin ||= () => () => 'current';
  return async function applyImageTool(endpoint, extraPayload, layerName, btn, opts) {
    if (lifetime?.disposed) return {status:'retired'};
    const check = captureOrigin();
    const resultId = resultHooks?.reserve();
    let submitted = false, received = false;
    const controller = new AbortController();
    let pendingImage = null;
    let rejectDecode = null;
    let spinnerDestroyed = false;
    const destroySpinner = () => { if (spinnerDestroyed) return; spinnerDestroyed = true; try { btnSpinner?.destroy(); } catch {} };
    const retire = () => { destroySpinner(); controller.abort(); if (pendingImage) { pendingImage.onload = null; pendingImage.onerror = null; pendingImage.src = ''; pendingImage = null; } rejectDecode?.(Object.assign(new Error('AI operation retired'), {name:'AbortError'})); };
    const unregister = lifetime?.add(retire) || (() => {});
    const current = () => !lifetime?.disposed && check() === 'current';
    const requireCurrent = () => { if (!current()) throw Object.assign(new Error('Image changed while AI was working; run it again.'), {code:'origin_changed'}); };
    const payload = { ...(extraPayload || {}) };
    const origHTML = btn.innerHTML;
    const origWidth = btn.offsetWidth;  // lock width so the button doesn't jump
    btn.disabled = true;
    btn.classList.add('ge-btn-processing');
    btn.style.minWidth = origWidth + 'px';
    // Swap label for a "<verbing>…" text + whirlpool while the
    // request runs. Falls back to deriving a busy label from
    // layerName when the caller didn't supply one.
    const busyLabel = (opts && opts.busyLabel) || deriveBusyLabel(layerName);
    btn.innerHTML = '';
    let btnSpinner = null;
    try {
      btnSpinner = spinnerModule.create('', 'clean', 'whirlpool');
      const sp = btnSpinner.createElement();
      btn.appendChild(sp);
      const txt = (root?.ownerDocument || document).createElement('span');
      txt.className = 'ge-btn-busy-label';
      txt.textContent = busyLabel;
      btn.appendChild(txt);
      btnSpinner.start();
    } catch { btn.textContent = busyLabel; }
    // Tool-specific managed route picker. A blank selection delegates to the
    // owner's ordered Images bindings in the operation router.
    if (!payload.model_route_id) {
      const m = /\/api\/image\/([\w-]+)/.exec(endpoint || '');
      const type = m ? m[1].replace('upscale-ai', 'upscale').replace('remove-bg', 'rembg') : null;
      const sel = getSelectedAIEndpoint(type);
      if (sel.modelRouteId) payload.model_route_id = sel.modelRouteId;
    }
    try {
      requireCurrent();
      const flatCanvas = flatten();
      const imageB64 = flatCanvas.toDataURL('image/png').split(',')[1];
      const body = { image: imageB64, ...payload };
      if (resultId) { resultHooks.submit(resultId); submitted = true; }
      const res = await fetchImpl(endpoint, {
        signal: controller.signal,
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        if (resultId) {resultHooks.reject(resultId); submitted=false;}
        let err = res.statusText;
        try { const e = await res.json(); err = e.detail || e.error || err; } catch {}
        throw new Error(err);
      }
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      if (!data.image) throw new Error('No image returned');
      if (resultId) {resultHooks.receive(resultId,{bytes:data.image,mime:'image/png',metadata:{label:layerName}}); received=true;}
      if (!current()) {uiModule?.showToast('Result retained for this image.',6000); return {status:'retained'};}
      const img = await new Promise((resolve, reject) => {
        rejectDecode = reject;
        pendingImage = new ImageClass();
        pendingImage.onload = () => { const image = pendingImage; pendingImage = null; rejectDecode = null; resolve(image); };
        pendingImage.onerror = () => { pendingImage = null; rejectDecode = null; reject(new Error('Failed to load result')); };
        pendingImage.src = 'data:image/png;base64,' + data.image;
      });
      requireCurrent();
      const mutation=beginMutation(); if (!mutation) {uiModule?.showToast('Result retained for this image.',6000); return {status:'retained'};}
      try {
      saveState();
      const layer = createLayer(layerName, state.imgWidth, state.imgHeight);
      layer.ctx.drawImage(img, 0, 0);
      state.layers.push(layer);
      if (opts?.onApplied) { const value=opts.onApplied(layer); if (value && typeof value.then==='function') throw new Error('Image result callback must be synchronous'); }
      markSceneChanged();
      if (resultId) resultHooks.resolve(resultId);
      state.activeLayerId = layer.id;
      composite(); renderLayerPanel();
      uiModule?.showToast(layerName + ' complete', 4500);
      return { status: 'applied', layer };
      } finally {endMutation(mutation);}
    } catch (e) {
      if (resultId && !received) { try { if (submitted) resultHooks.unknown(resultId); else resultHooks.resolve(resultId); } catch (_) {} }
      if (received) { if (!lifetime?.disposed && check() !== 'retired') uiModule?.showToast('Result retained for this image.',6000); return {status:'retained'}; }
      if (e?.name === 'AbortError' || lifetime?.disposed || check() === 'retired') return {status:'retired'};
      if (e?.code === 'origin_changed') { uiModule?.showToast(e.message, 6000); return {status:'changed'}; }
      // Detect known failure modes and surface an action-toast.
      const msg = (e?.message || '').toLowerCase();
      const needsImg2Img = (
        msg.includes('img2img') ||
        msg.includes('diffusion server') ||
        msg.includes("doesn't expose")
      );
      let depMatch = null;
      for (const pkg of KNOWN_DEPS) {
        if (msg.includes(`${pkg} not installed`) || msg.includes(`no module named '${pkg}'`)) {
          depMatch = pkg; break;
        }
      }
      if (uiModule) {
        if (depMatch && uiModule.showToast.length >= 2) {
          uiModule.showToast(layerName + ' failed: ' + depMatch + ' is not installed on the server.', {
            duration: 9000,
            action: `Install ${depMatch}`,
            onAction: () => openCookbookForDependency(depMatch),
          });
        } else if (needsImg2Img && uiModule.showToast.length >= 2) {
          uiModule.showToast(layerName + ' failed: ' + e.message, {
            duration: 9000,
            action: 'Open Cookbook',
            onAction: () => openCookbookForImg2img(),
          });
        } else {
          uiModule.showToast(layerName + ' failed: ' + e.message, 6000);
        }
      }
    } finally {
      unregister();
      destroySpinner();
      if (!lifetime?.disposed && check() !== 'retired') {
        btn.disabled = false; btn.classList.remove('ge-btn-processing'); btn.innerHTML = origHTML; btn.style.minWidth = '';
      }
    }
  };
}
