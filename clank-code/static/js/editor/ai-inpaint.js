import { editorIdSelector } from './dom-identity.js';
/**
 * AI inpaint subsystem — Generate, Remove, and Outpaint variants
 * all share a single `runInpaint` core; only the prompt, strength,
 * and button-target differ. Returns a wireInpaintButtons() function
 * to attach handlers to the three buttons (#ge-inpaint-run,
 * #ge-inpaint-remove, #ge-inpaint-outpaint).
 *
 *   runInpaint:
 *     - Build a union mask from every visible mask sub-layer (across
 *       all parent layers) — the model sees the COMBINED region,
 *       not just the currently-active mask.
 *     - Dilate the mask ~padPx so the model fills a buffer zone the
 *       post-gen Feather/Edge slider can fade into.
 *     - POST flattened canvas + dilated mask to /api/image/inpaint.
 *     - Drop the result as a new layer, snapshot the AI image + hard
 *       mask on the layer for live edge tuning, hide every
 *       contributing mask sub-layer, reveal the post-gen Feather +
 *       Edge Stroke sliders capped at ±padPx.
 *
 *   Remove: sends one provider-neutral semantic request. The managed engine
 *     adapter owns provider-specific normalization.
 *
 *   Outpaint: auto-generates a mask covering empty (transparent)
 *     regions of the flattened composite, dilates it 12px inward
 *     so the model sees adjacent opaque pixels as context, runs
 *     inpaint, then restores the user's previous mask drawing.
 *
 * @param {{
 *   buildMergedMaskCanvas:  () => HTMLCanvasElement | null,
 *   dilateMask:             (src: HTMLCanvasElement, px: number) => HTMLCanvasElement,
 *   applyInpaintFeather:    (layer: object, featherPx: number, edgeShiftPx: number) => void,
 *   getSelectedAIEndpoint:  (type: string) => { modelRouteId?: string },
 *   ensureActiveMaskLayer:  () => object | null,
 *   saveState:              (label?: string) => void,
 *   createLayer:            (name: string, w: number, h: number) => object,
 *   composite:              () => void,
 *   renderLayerPanel:       () => void,
 *   spinnerModule:          object,
 *   uiModule:               object | null,
 * }} deps
 */
import { state as legacyState } from './state.js';

export function wireInpaintButtons({ state = legacyState, root = null, lifetime = null, captureOrigin = null,
  buildMergedMaskCanvas, dilateMask, applyInpaintFeather,
  getSelectedAIEndpoint, ensureActiveMaskLayer,
  saveState, createLayer, composite, renderLayerPanel,
  spinnerModule, uiModule, markSceneChanged = () => {}, resultHooks = null, beginMutation = () => true, endMutation = () => {}, canCapture = () => true, sceneRevision = () => state.sceneRevision,
}) {
  if (state !== legacyState && (!root || !lifetime || typeof captureOrigin !== 'function')) throw new TypeError('isolated AI wiring requires ownership');
  captureOrigin ||= () => () => 'current';
  const ownerRoot = root || state.container;
  const ownerDocument = ownerRoot?.ownerDocument || globalThis.document;
  const find = id => ownerRoot?.querySelector(editorIdSelector(id));
  const removers = [];
  const listen = (node, type, fn) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded); removers.push(() => node.removeEventListener(type, guarded)); };
  lifetime?.add(() => { for (const remove of removers.splice(0)) remove(); });

  // Shared inpaint runner — used by Generate, Remove, and Outpaint.
  async function runInpaint({ prompt, strength, btnId, labelId, idleLabel, busyLabel, maskOverride = null }) {
    if (!canCapture()) {uiModule?.showToast('Finish the current image edit first.'); return;}
    const check = captureOrigin(), controller = new AbortController();
    let resultId=null, submitted=false, received=false;
    let image = null, rejectDecode = null;
    let spinnersCleaned = false;
    const cleanSpinners = () => { if (spinnersCleaned) return; spinnersCleaned = true; try { runWp?.destroy(); canvasWp?.destroy(); canvasWpEl?.remove(); } catch {} };
    const unregister = lifetime?.add(() => { cleanSpinners(); controller.abort(); if (image) { image.onload = null; image.onerror = null; image.src = ''; } rejectDecode?.(Object.assign(new Error('retired'), {name:'AbortError'})); }) || (() => {});
    const requireCurrent = () => { if (lifetime?.disposed || check() !== 'current') throw Object.assign(new Error('Image changed while AI was working; run it again.'), {code:'origin_changed'}); };
    const route = getSelectedAIEndpoint('inpaint').modelRouteId || undefined;
    // Pre-check: build the union mask the AI will receive and verify
    // at least one pixel is painted.
    const preMerged = maskOverride || buildMergedMaskCanvas();
    if (!preMerged) { unregister(); if (uiModule) uiModule.showToast('Draw the area you want to inpaint first'); return; }
    const pmCtx = preMerged.getContext('2d');
    const maskData = pmCtx.getImageData(0, 0, preMerged.width, preMerged.height).data;
    let hasMask = false;
    for (let i = 3; i < maskData.length; i += 4) { if (maskData[i] > 0) { hasMask = true; break; } }
    if (!hasMask) { unregister(); if (uiModule) uiModule.showToast('Draw the area you want to inpaint first'); return; }
    try { resultId=resultHooks?.reserve(); } catch(error) {unregister(); uiModule?.showToast(error.message); return;}
    const btn = find(btnId);
    const btnLabel = labelId ? find(labelId) : null;
    btn.disabled = true;
    if (btnLabel) btnLabel.textContent = busyLabel;
    let runWp = null;
    try {
      runWp = spinnerModule.createWhirlpool(14);
      runWp.element.style.cssText = 'margin:0;flex-shrink:0;';
      btn.appendChild(runWp.element);
    } catch (_) { /* spinner is optional */ }
    // Canvas-overlay whirlpool — visual feedback right where the
    // user's working, since the run button lives in the side panel
    // and may be out of view at high zoom. Positioned over the
    // mask's centroid in viewport coords.
    let canvasWp = null;
    let canvasWpEl = null;
    try {
      const area = state.container && state.container.querySelector('.ge-canvas-area');
      const mainRect = state.mainCanvas.getBoundingClientRect();
      if (area && mainRect.width && mainRect.height) {
        // Find the mask's bbox so we can centre the whirlpool over it.
        let cx = state.imgWidth / 2, cy = state.imgHeight / 2;
        try {
          const merged = buildMergedMaskCanvas();
          if (merged) {
            const d = merged.getContext('2d').getImageData(0, 0, merged.width, merged.height).data;
            let minX = merged.width, maxX = 0, minY = merged.height, maxY = 0;
            for (let y = 0; y < merged.height; y += 4) {
              for (let x = 0; x < merged.width; x += 4) {
                if (d[(y * merged.width + x) * 4 + 3] > 0) {
                  if (x < minX) minX = x; if (x > maxX) maxX = x;
                  if (y < minY) minY = y; if (y > maxY) maxY = y;
                }
              }
            }
            if (maxX >= minX) { cx = (minX + maxX) / 2; cy = (minY + maxY) / 2; }
          }
        } catch {}
        const scaleX = mainRect.width / state.mainCanvas.width;
        const scaleY = mainRect.height / state.mainCanvas.height;
        const vpX = mainRect.left + cx * scaleX;
        const vpY = mainRect.top  + cy * scaleY;
        canvasWp = spinnerModule.create('', 'clean', 'whirlpool');
        canvasWpEl = canvasWp.createElement();
        canvasWpEl.style.cssText = `position:fixed;left:${vpX}px;top:${vpY}px;transform:translate(-50%,-50%);z-index:12;pointer-events:none;`;
        ownerDocument.body.appendChild(canvasWpEl);
        canvasWp.start();
      }
    } catch (_) { /* overlay is decorative */ }
    try {
      requireCurrent();
      // Flatten current image.
      const flatCanvas = ownerDocument.createElement('canvas');
      flatCanvas.width = state.imgWidth; flatCanvas.height = state.imgHeight;
      const flatCtx = flatCanvas.getContext('2d');
      for (const layer of state.layers) {
        if (!layer.visible) continue;
        flatCtx.globalAlpha = layer.opacity;
        const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
        flatCtx.drawImage(layer.canvas, off.x, off.y);
      }
      flatCtx.globalAlpha = 1;
      // Dilate the user's brush mask before sending to the model.
      // The AI fills a small buffer zone around the brush, so the
      // post-gen Edge feather slider has AI content to fade INTO
      // instead of fading straight to the original. The ORIGINAL
      // (un-dilated) mask is cached on the layer — the feather blur
      // expands outward from that boundary into the dilated AI region.
      const padPx = Math.min(80, Math.max(20, Math.round(Math.min(state.imgWidth, state.imgHeight) * 0.04)));
      // Merge every visible mask sub-layer (across all parent
      // layers) into a single union mask before sending to the AI.
      // This way, if the user built up the inpaint region across
      // multiple masks, the final generation sees the combined
      // region instead of just the currently-active mask.
      const mergedMask = maskOverride || buildMergedMaskCanvas() || state.maskCanvas;
      const maskSnap=ownerDocument.createElement('canvas'); maskSnap.width=mergedMask.width; maskSnap.height=mergedMask.height; maskSnap.getContext('2d').drawImage(mergedMask,0,0);
      const width=state.imgWidth, height=state.imgHeight, revision=sceneRevision();
      const participants=maskOverride ? [] : state.layers.flatMap(layer => (layer.masks || []).filter(mask=>mask.visible && mask.canvas?.width && mask.canvas?.height).map(mask => ({layer:String(layer.id),mask:String(mask.id)})));
      const metadata={label:'Inpaint result',inpaint:{width,height,pad:padPx,revision,participants}};
      if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) || width<1 || height<1 || width>16384 || height>16384 || participants.some(item=>item.layer.length>256 || item.mask.length>256) || JSON.stringify(metadata).length>16384) throw new Error('Inpaint source metadata exceeds the recovery limit');
      const dilatedMask = dilateMask(mergedMask, padPx);
      const imageB64 = flatCanvas.toDataURL('image/png').split(',')[1];
      const maskB64 = dilatedMask.toDataURL('image/png').split(',')[1];
      const baseSnap = ownerDocument.createElement('canvas');
      baseSnap.width = state.imgWidth;
      baseSnap.height = state.imgHeight;
      baseSnap.getContext('2d').drawImage(flatCanvas, 0, 0);
      const sidecars={base:imageB64,mask:maskSnap.toDataURL('image/png').split(',')[1]};
      if (resultId) {resultHooks.submit(resultId); submitted=true;}
      const res = await fetch('/api/image/inpaint', {
        method: 'POST', credentials: 'same-origin', signal: controller.signal,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify((() => {
          return { image: imageB64, mask: maskB64, prompt, width: state.imgWidth, height: state.imgHeight, strength, feather: 0, model_route_id: route };
        })()),
      });
      if (!res.ok) {
        if (resultId) {resultHooks.reject(resultId); submitted=false;}
        let errDetail = res.statusText;
        try { const errBody = await res.json(); errDetail = errBody.detail || errBody.error || errDetail; } catch {}
        throw new Error(errDetail);
      }
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      if (!data.image) throw new Error('No image returned from inpaint endpoint');
      if (resultId) {resultHooks.receive(resultId,{bytes:data.image,mime:'image/png',metadata,sidecars}); received=true;}
      if (lifetime?.disposed || check()!=='current') {if (!lifetime?.disposed && check()!=='retired') uiModule?.showToast('Inpaint result retained for this image.'); return;}
      // Load result as a new layer and clip with the user-drawn mask
      // so only the inpainted region is visible. Cache the
      // unfeathered (AI image + hard mask) on the layer so the live
      // Feather slider can re-derive the alpha on each input event
      // without re-running the model.
      const resultImg = image = new Image();
      await new Promise((resolve, reject) => {
      rejectDecode = reject;
      resultImg.onload = () => {
        try { requireCurrent(); } catch (error) { reject(error); return; }
        const mutation=beginMutation(); if (!mutation) {uiModule?.showToast('Inpaint result retained for this image.'); resolve(); return;}
        try {
          saveState('Inpaint result');
          // OpenAI returns at one of its allowed sizes (1024²,
          // 1024×1536, 1536×1024) which often differs from our
          // canvas. Scale to canvas size with smoothing so the
          // inpaint blends in regardless of source dims.
          const shortPrompt = (prompt || '').trim().replace(/\s+/g, ' ').slice(0, 40);
          const layerName = shortPrompt ? `Inpaint: ${shortPrompt}` : 'Inpaint Result';
          const resultLayer = createLayer(layerName, state.imgWidth, state.imgHeight);
          resultLayer.ctx.imageSmoothingEnabled = true;
          resultLayer.ctx.imageSmoothingQuality = 'high';
          resultLayer.ctx.drawImage(resultImg, 0, 0, state.imgWidth, state.imgHeight);
          // Snapshot the AI result + hard mask used for this run.
          const aiSnap = ownerDocument.createElement('canvas');
          aiSnap.width = state.imgWidth; aiSnap.height = state.imgHeight;
          aiSnap.getContext('2d').drawImage(resultLayer.canvas, 0, 0);
          resultLayer.inpaintSource = { ai: aiSnap, mask: maskSnap, base: baseSnap, padPx };
          // Apply initial alpha = hard mask (no feather, no edge shift).
          applyInpaintFeather(resultLayer, 0, 0);
          state.layers.push(resultLayer);
          state.activeLayerId = resultLayer.id;
          state.lastInpaintLayerId = resultLayer.id;
          // Hide every mask sub-layer that contributed to the
          // generation so the red overlay doesn't cover the result —
          // but KEEP the mask pixels intact, and reflect "hidden"
          // on each sub-row's eye icon.
          for (const ly of state.layers) {
            if (!ly.masks || !ly.masks.length) continue;
            for (const mk of ly.masks) if (participants.some(item=>item.layer===String(ly.id) && item.mask===String(mk.id))) mk.visible = false;
          }
          markSceneChanged();
          if (resultId) resultHooks.resolve(resultId);
          composite();
          renderLayerPanel();
          // Reveal post-generation Feather + Edge Stroke sliders.
          // Cap Edge Stroke at ±padPx so the slider can't ask for
          // more AI buffer than we generated.
          const fRow = find('ge-inpaint-postfeather-row');
          const fSlider = find('ge-feather-slider');
          const fLabel = find('ge-feather-label');
          // Divider + heading are always visible; once Generate
          // succeeds we hide the "Available after Generate" hint.
          const divEl = find('ge-inpaint-postedge-divider');
          const titleEl = find('ge-inpaint-postedge-title');
          const hintEl = find('ge-inpaint-postedge-hint');
          if (divEl) divEl.style.display = '';
          if (titleEl) titleEl.style.display = '';
          if (hintEl) hintEl.style.display = 'none';
          if (fRow) fRow.style.display = '';
          if (fSlider) fSlider.value = '0';
          if (fLabel) fLabel.textContent = '0px';
          const eRow = find('ge-inpaint-edgestroke-row');
          const eSlider = find('ge-edgestroke-slider');
          const eLabel = find('ge-edgestroke-label');
          const autoRow = find('ge-inpaint-automatch-row');
          if (eRow) eRow.style.display = '';
          if (autoRow) autoRow.style.display = '';
          if (eSlider) {
            eSlider.max = String(padPx);
            eSlider.min = String(-padPx);
            eSlider.value = '0';
          }
          if (eLabel) eLabel.textContent = '0px';
          if (uiModule) uiModule.showToast('Inpaint complete — drag Edge feather / Edge stroke to blend', 5000);
          resolve();
        } catch (renderErr) {
          console.error('[inpaint] render error', renderErr);
          reject(renderErr);
        } finally {endMutation(mutation);}
      };
      resultImg.onerror = () => reject(new Error('Inpaint result failed to decode'));
      resultImg.src = 'data:image/png;base64,' + data.image;
      });
      image = null; rejectDecode = null;
    } catch (e) {
      if (resultId && !received) {try {if (submitted) resultHooks.unknown(resultId); else resultHooks.resolve(resultId);} catch (_) {}}
      if (received) {if (!lifetime?.disposed && check()!=='retired') uiModule?.showToast('Inpaint result retained for this image.',6000); return;}
      if (!lifetime?.disposed && check() !== 'retired' && e.name !== 'AbortError') uiModule?.showToast(e.code === 'origin_changed' ? e.message : 'Inpaint failed: ' + e.message, 6000);
    } finally {
      unregister();
      if (!lifetime?.disposed && check() !== 'retired') { btn.disabled = false; if (btnLabel) btnLabel.textContent = idleLabel; }
      cleanSpinners();
      if (!lifetime?.disposed && check() !== 'retired') window.dispatchEvent(new CustomEvent('ge:inpaint-done'));
    }
  }

  // Generate.
  listen(find('ge-inpaint-run'), 'click', async () => {
    const prompt = find('ge-inpaint-prompt')?.value?.trim();
    if (!prompt) { if (uiModule) uiModule.showToast('Enter a prompt for inpainting'); return; }
    const strength = (parseInt(find('ge-strength-slider')?.value || '75')) / 100;
    await runInpaint({
      prompt, strength,
      btnId: 'ge-inpaint-run',
      labelId: 'ge-inpaint-run-label',
      idleLabel: 'Generate', busyLabel: 'Generating',
    });
  });

  // Remove is expressed semantically; provider adapters and registered local
  // recipes own any backend-specific request normalization.
  listen(find('ge-inpaint-remove'), 'click', async () => {
    const userP = find('ge-inpaint-prompt')?.value?.trim();
    const prompt = userP
      ? `Remove ${userP}. Fill seamlessly with the surrounding background, photorealistic, no objects, no people.`
      : 'Remove the masked area. Fill seamlessly with the surrounding background, photorealistic, no objects, no people.';
    const strength = (parseInt(find('ge-strength-slider')?.value || '75')) / 100;
    await runInpaint({
      prompt, strength,
      btnId: 'ge-inpaint-remove',
      labelId: 'ge-inpaint-remove-label',
      idleLabel: 'Remove', busyLabel: 'Removing',
    });
  });

  // Outpaint — auto-generate a mask covering empty (transparent)
  // areas of the flattened composite, then run inpaint to fill them
  // seamlessly. Mask is dilated ~12px so the AI sees adjacent
  // opaque pixels as context. Ignores the user's drawn mask.
  listen(find('ge-inpaint-outpaint'), 'click', async () => {
    if (!canCapture()) {uiModule?.showToast('Finish the current image edit first.'); return;}
    // 1) Flatten visible layers to detect alpha=0 (empty) regions.
    const flat = ownerDocument.createElement('canvas');
    flat.width = state.imgWidth; flat.height = state.imgHeight;
    const fctx = flat.getContext('2d');
    for (const layer of state.layers) {
      if (!layer.visible) continue;
      fctx.globalAlpha = layer.opacity;
      const off = state.layerOffsets.get(layer.id) || { x: 0, y: 0 };
      fctx.drawImage(layer.canvas, off.x, off.y);
    }
    fctx.globalAlpha = 1;
    const flatData = fctx.getImageData(0, 0, state.imgWidth, state.imgHeight).data;
    // 2) White wherever the composite is transparent.
    const maskRaw = ownerDocument.createElement('canvas');
    maskRaw.width = state.imgWidth; maskRaw.height = state.imgHeight;
    const mrCtx = maskRaw.getContext('2d');
    const mrImg = mrCtx.createImageData(state.imgWidth, state.imgHeight);
    let emptyCount = 0;
    for (let i = 0; i < flatData.length; i += 4) {
      if (flatData[i + 3] === 0) {
        mrImg.data[i] = 255;
        mrImg.data[i + 1] = 255;
        mrImg.data[i + 2] = 255;
        mrImg.data[i + 3] = 255;
        emptyCount++;
      }
    }
    if (emptyCount === 0) {
      if (uiModule) uiModule.showToast('No empty areas to outpaint — canvas is fully covered.');
      return;
    }
    mrCtx.putImageData(mrImg, 0, 0);
    // 3) Dilate the mask outward 12px so it overlaps a band of
    //    opaque pixels — context for the model to blend cleanly.
    const expanded = ownerDocument.createElement('canvas');
    expanded.width = state.imgWidth; expanded.height = state.imgHeight;
    const ectx = expanded.getContext('2d');
    ectx.filter = 'blur(12px)';
    ectx.drawImage(maskRaw, 0, 0);
    ectx.filter = 'none';
    const expData = ectx.getImageData(0, 0, state.imgWidth, state.imgHeight);
    for (let i = 0; i < expData.data.length; i += 4) {
      const a = expData.data[i + 3];
      const v = a > 6 ? 255 : 0;
      expData.data[i] = v;
      expData.data[i + 1] = v;
      expData.data[i + 2] = v;
      expData.data[i + 3] = v;
    }
    ectx.putImageData(expData, 0, 0);
    // Pass the generated mask as request input without changing saved mask pixels.
    const userP = find('ge-inpaint-prompt')?.value?.trim();
    const prompt = userP || 'seamless natural continuation of the surrounding image, photorealistic, matching style, no objects, no people, no text';
    await runInpaint({
      prompt, strength: 0.99, maskOverride: expanded,
      btnId: 'ge-inpaint-outpaint', labelId: 'ge-inpaint-outpaint-label',
      idleLabel: 'Outpaint', busyLabel: 'Outpainting',
    });
  });
}
