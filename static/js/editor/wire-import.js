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
import { state } from './state.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { openFilesImageBrowser } from '../filesImageBrowser.js';

export function wireImport({ container, saveState, createLayer, composite, renderLayerPanel, uiModule }) {
  // Hidden <input type="file"> the topbar + File buttons both click.
  const importFileInput = document.createElement('input');
  importFileInput.type = 'file';
  importFileInput.accept = 'image/*';
  importFileInput.style.display = 'none';
  container.appendChild(importFileInput);

  function handleImportedImage(img) {
    if (!state.editorOpen) return;
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
    state.activeLayerId = layer.id;
    // Switch to move tool so the imported layer is immediately
    // repositionable.
    state.tool = 'move';
    const tb = container.querySelector('.ge-toolbar');
    if (tb) tb.querySelectorAll('.ge-tool-btn').forEach(b => b.classList.toggle('active', b.dataset.tool === 'move'));
    // Hide the import section now that the import is done.
    const importSec = document.getElementById('ge-import-section');
    if (importSec) importSec.style.display = 'none';
    composite();
    renderLayerPanel();
    if (uiModule) uiModule.showToast('Image imported — drag to position');
  }

  importFileInput.addEventListener('change', (e) => {
    const file = e.target.files[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = (ev) => {
      const img = new Image();
      img.onload = () => handleImportedImage(img);
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
    importFileInput.value = '';
  });

  document.getElementById('ge-import-topbar')?.addEventListener('click', () => importFileInput.click());
  document.getElementById('ge-import-file')?.addEventListener('click', () => importFileInput.click());

  document.getElementById('ge-import-paste')?.addEventListener('click', async () => {
    try {
      const clipItems = await navigator.clipboard.read();
      let blob = null;
      for (const item of clipItems) {
        const imgType = item.types.find(t => t.startsWith('image/'));
        if (imgType) { blob = await item.getType(imgType); break; }
      }
      if (!blob) { if (uiModule) uiModule.showToast('No image found in clipboard'); return; }
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => { handleImportedImage(img); URL.revokeObjectURL(url); };
      img.onerror = () => { URL.revokeObjectURL(url); if (uiModule) uiModule.showToast('Failed to load clipboard image'); };
      img.src = url;
    } catch (e) {
      if (uiModule) uiModule.showToast('Clipboard access denied or no image available');
    }
  });

  // Import from Files while preserving Gallery's ordinary folders and pages.
  document.getElementById('ge-import-gallery')?.addEventListener('click', async () => {
    try {
      await openFilesImageBrowser({
        onPick(item) {
          const img = new Image();
          img.crossOrigin = 'anonymous';
          img.onload = () => handleImportedImage(img);
          img.onerror = () => { if (uiModule) uiModule.showToast('Failed to load Files image'); };
          img.src = filesFacadeClient.contentUrl(item.ref, { purpose: 'preview' });
        },
      });
    } catch (error) {
      if (uiModule) uiModule.showToast('Failed to load Files: ' + error.message);
    }
  });

  return { handleImportedImage };
}
