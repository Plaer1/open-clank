/**
 * Image-import wiring — covers all four entry points that drop an
 * image as a new layer:
 *
 *   #ge-import-topbar    topbar "+ Import" button
 *   #ge-import-file      File button in the Import section
 *   #ge-import-paste     Clipboard button (uses async clipboard API)
 *   #ge-import-gallery   Files picker — browses the Gallery folder
 *                        and shows a thumbnail grid overlay
 *
 * Plus the shared `handleImportedImage(img)` sink — scales to canvas,
 * centres, creates a new layer, switches to Move tool, hides the
 * import section, refreshes the panel. Returned so the drag-and-drop
 * + paste paths (wired in editor/clipboard-and-drop.js) can use the
 * same sink.
 *
 * @param {{
 *   container:        HTMLElement,
 *   saveState:        (label?: string) => void,
 *   createLayer:      (name, w, h) => object,
 *   composite:        () => void,
 *   renderLayerPanel: () => void,
 *   uiModule:         object,
 * }} deps
 *
 * @returns {{ handleImportedImage: (img: HTMLImageElement) => void }}
 */
import { state as legacyState } from './state.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { openFilesImageBrowser } from '../filesImageBrowser.js';
import { decodeCanvasImportBlob, readBoundedImportBlob, createImportAbortController, CANVAS_MAX_IMPORT_BYTES, CANVAS_MAX_IMPORT_DIMENSION, CANVAS_MAX_IMPORT_PIXELS } from './clipboard-and-drop.js';

export function wireImport({ state = legacyState, container, saveState, createLayer, composite, renderLayerPanel, uiModule, markSceneChanged = () => {}, lifetime = null, captureImport = () => () => 'current', startImport = null }) {
  const doc = container.ownerDocument;
  const win = doc.defaultView;
  const controller = createImportAbortController();
  const removers = [];
  const pendingImports = new Set();
  const reserve = check => {const operation=startImport?.(check); if (operation) pendingImports.add(operation); return operation;};
  const release = operation => {if (operation) pendingImports.delete(operation);};
  let disposed = false;
  const listen = (target, type, fn) => { if (!target) return; target.addEventListener(type, fn); removers.push(() => target.removeEventListener(type, fn)); };
  const limits = {maxDimension: CANVAS_MAX_IMPORT_DIMENSION, maxPixels: CANVAS_MAX_IMPORT_PIXELS};
  const decode = async (blob, check, reserved = null) => {
    const operation=reserved || reserve(check);
    try {
      if (!blob || !String(blob.type || '').toLowerCase().startsWith('image/') || blob.size > CANVAS_MAX_IMPORT_BYTES) throw new Error('Canvas rejected that image.');
      if (operation) await operation.receive(blob,{label:'Imported'});
      const image = await decodeCanvasImportBlob(blob, limits, {signal: controller?.signal});
      if (operation) { if (!operation.apply(() => handleImportedImage(image))) uiModule?.showToast?.('Image retained for this session.'); return; }
      if (disposed || check() === 'retired') return;
      if (check() === 'edited') { uiModule?.showToast?.('Image import stopped because this image was edited; choose it again.'); return; }
      handleImportedImage(image);
    } catch (error) { if (error?.name!=='AbortError') operation?.cancel(true); else operation?.cancel(); throw error; } finally {release(operation);}
  };
  const report = error => { if (!disposed && error?.name !== 'AbortError') uiModule?.showToast?.(error?.message || 'Image import failed'); };
  const dispose = () => { if (disposed) return; disposed = true; controller?.abort(); for (const operation of pendingImports) operation.cancel(); pendingImports.clear(); for (const remove of removers.splice(0)) remove(); importFileInput.remove(); };
  // Hidden <input type="file"> the topbar + File buttons both click.
  const importFileInput = doc.createElement('input');
  importFileInput.type = 'file';
  importFileInput.accept = 'image/*';
  importFileInput.style.display = 'none';
  container.appendChild(importFileInput);
  lifetime?.add(dispose);
  if (disposed) return { handleImportedImage, dispose };
  listen(doc, 'openclank:auth-context-changed', dispose);

  function handleImportedImage(img) {
    if (disposed || !state.editorOpen) return;
    saveState('Import image');
    // Scale down if larger than canvas.
    let w = img.naturalWidth || img.width;
    let h = img.naturalHeight || img.height;
    if (w > state.imgWidth || h > state.imgHeight) {
      const scale = Math.min(state.imgWidth / w, state.imgHeight / h);
      w = Math.round(w * scale);
      h = Math.round(h * scale);
    }
    const layer = createLayer('Imported', state.imgWidth, state.imgHeight);
    // Centre on the canvas.
    const ox = Math.round((state.imgWidth - w) / 2);
    const oy = Math.round((state.imgHeight - h) / 2);
    layer.ctx.drawImage(img, ox, oy, w, h);
    state.layers.push(layer);
    markSceneChanged();
    state.activeLayerId = layer.id;
    // Switch to move tool so the imported layer is immediately
    // repositionable.
    state.tool = 'move';
    const tb = container.querySelector('.ge-toolbar');
    if (tb) tb.querySelectorAll('.ge-tool-btn').forEach(b => b.classList.toggle('active', b.dataset.tool === 'move'));
    // Hide the import section now that the import is done.
    const importSec = container.querySelector(':is(#ge-import-section,[data-ge-id="ge-import-section"])');
    if (importSec) importSec.style.display = 'none';
    composite();
    renderLayerPanel();
    if (uiModule) uiModule.showToast('Image imported — drag to position');
  }

  let pickerCheck = null, pickerOperation = null;
  const cancelPicker = () => {pickerOperation?.cancel(); release(pickerOperation); pickerOperation=null; pickerCheck=null;};
  listen(importFileInput, 'change', event => {
    const file = event.target.files[0];
    const check=pickerCheck, operation=pickerOperation; pickerCheck=null; pickerOperation=null;
    if (file && check) void decode(file, check, operation).catch(report); else {operation?.cancel(); release(operation);}
    importFileInput.value = '';
  });
  listen(importFileInput,'cancel',cancelPicker);
  const pickFile = () => {try {cancelPicker(); pickerCheck=captureImport(); pickerOperation=reserve(pickerCheck); importFileInput.click();} catch(error) {cancelPicker(); report(error);} };
  listen(container.querySelector(':is(#ge-import-topbar,[data-ge-id="ge-import-topbar"])'), 'click', pickFile);
  listen(container.querySelector(':is(#ge-import-file,[data-ge-id="ge-import-file"])'), 'click', pickFile);
  listen(container.querySelector(':is(#ge-import-paste,[data-ge-id="ge-import-paste"])'), 'click', async () => {
    const check = captureImport();
    let operation;
    try {
      operation=reserve(check);
      const items = await win.navigator.clipboard.read();
      if (disposed) return;
      for (const item of items) {
        const type = item.types.find(value => value.startsWith('image/'));
        if (type) { const blob = await item.getType(type); if (!disposed) await decode(blob, check, operation); else operation?.cancel(); return; }
      }
      uiModule?.showToast?.('No image found in clipboard');
    } catch (error) { operation?.cancel(); report(error); } finally {if (operation && pendingImports.has(operation)) {operation.cancel(); release(operation);}}
  });
  listen(container.querySelector(':is(#ge-import-gallery,[data-ge-id="ge-import-gallery"])'), 'click', async () => {
    const check = captureImport();
    let operation;
    try {
      operation=reserve(check);
      await openFilesImageBrowser({ signal:controller?.signal, onCancel() {operation?.cancel(); release(operation);}, onPick(item) {
        if (disposed) {operation?.cancel(); release(operation); return;}
        void (async () => {
          const response = await win.fetch(filesFacadeClient.contentUrl(item.ref, {purpose:'preview'}), {credentials:'same-origin', ...(typeof AbortSignal === 'function' && controller.signal instanceof AbortSignal ? {signal:controller.signal} : {})});
          if (!response.ok || Number(response.headers.get('content-length') || 0) > CANVAS_MAX_IMPORT_BYTES) throw new Error('Files image is unavailable or too large.');
          const blob = await readBoundedImportBlob(response, CANVAS_MAX_IMPORT_BYTES, controller.signal);
          if (!disposed) await decode(blob, check, operation); else operation?.cancel();
        })().catch(error=>{operation?.cancel(); report(error);}).finally(()=>release(operation));
      }});
    } catch (error) {operation?.cancel(); release(operation); report(error); }
  });

  return { handleImportedImage, dispose };
}
