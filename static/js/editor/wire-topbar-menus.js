import { editorIdSelector } from './dom-identity.js';
/**
 * Topbar dropdown menus — Image, Filter, and Resize.
 *
 *   Image menu (#ge-image-menu-btn → #ge-image-menu):
 *     resize, selection (edge feather/delete), fill, rotate 90/180,
 *     flip horizontal/vertical.
 *
 *   Filter menu (#ge-filter-menu-btn → #ge-filter-menu):
 *     Blur sub-menu — Gaussian, Zoom.
 *
 *   Resize menu (#ge-resize-menu-btn → #ge-resize-menu):
 *     preset W×H items (data-resize-w/-h) apply immediately;
 *     [data-resize-custom] opens a themed prompt for arbitrary sizes.
 *
 * Returns the resize helpers so the keyboard-shortcuts module can
 * call them too (Ctrl+Shift+T opens the custom prompt).
 *
 * @param {{
 *   closeOtherTopbarMenus: (keepId: string) => void,
 *   registerDocClickAway:  (handler: (e: Event) => void) => void,
 *   saveState:             (label?: string) => void,
 *   composite:             () => void,
 *   fitZoom:               () => void,
 *   promptCanvasSize:      (opts: object) => Promise<{w, h} | null>,
 *   doFillSelection:       () => void,
 *   rotateAllLayers:       (deg: number) => void,
 *   flipAllLayers:         (axis: 'h' | 'v') => void,
 *   applyGaussianBlur:     () => void,
 *   applyZoomBlur:         () => void,
 *   uiModule:              object,
 * }} deps
 *
 * @returns {{
 *   applyResize:         (newW: number, newH: number) => void,
 *   resizeCustomPrompt:  () => Promise<void>,
 * }}
 */
import { state as legacyState } from './state.js';

export function wireTopbarMenus({ state = legacyState, root = null, lifetime = null,
  closeOtherTopbarMenus, registerDocClickAway,
  saveState, composite, fitZoom, markSceneChanged = () => {},
  promptCanvasSize, doFillSelection, captureResizeOrigin, resizeBounds,
  rotateAllLayers, flipAllLayers,
  applyGaussianBlur, applyZoomBlur,
  uiModule,
}) {
  if (state !== legacyState && (!root || !lifetime)) throw new TypeError('isolated menus require ownership');
  const ownerRoot = root || state.container, doc = ownerRoot.ownerDocument;
  const find = id => ownerRoot.querySelector(editorIdSelector(id));
  const removers = [];
  const listen = (node, type, fn, options) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded, options); removers.push(() => node.removeEventListener(type, guarded, options)); };
  lifetime?.add(() => { for (const remove of removers.splice(0)) remove(); });

  // ── Resize canvas ──
  // Extracted so both the popup presets and the Ctrl+Shift+T shortcut
  // can call it.
  function applyResize(newW, newH, origin = captureResizeOrigin()) {
    try {
      if (!resizeBounds(newW,newH,origin)) { uiModule.showToast('Canvas resize was not applied.'); return false; }
      uiModule.showToast(`Canvas resized to ${newW}×${newH}`); return true;
    } catch (error) { uiModule.showToast(error?.message || 'Canvas resize was not applied.'); return false; }
  }

  async function resizeCustomPrompt() {
    const origin=captureResizeOrigin();
    const result = await promptCanvasSize({ title:'Canvas size', okLabel:'Apply', initialW:state.imgWidth, initialH:state.imgHeight });
    if (!result || lifetime?.disposed) return;
    applyResize(result.w,result.h,origin);
  }

  // ── Image menu ──
  {
    const btn = find('ge-image-menu-btn');
    const menu = find('ge-image-menu');
    if (btn && menu) {
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        const willOpen = menu.hidden;
        if (willOpen) closeOtherTopbarMenus('ge-image-menu');
        menu.hidden = !menu.hidden;
      });
      listen(menu, 'click', (e) => {
        const item = e.target.closest('[data-image-action]');
        if (!item || item.disabled) return;
        menu.hidden = true;
        const action = item.dataset.imageAction;
        if (action === 'resize') resizeCustomPrompt();
        else if (action === 'selection') find('ge-edge-menu-btn')?.click();
        else if (action === 'fill') doFillSelection();
        else if (action === 'rotate-90') rotateAllLayers(90);
        else if (action === 'rotate-180') rotateAllLayers(180);
        else if (action === 'flip-h') flipAllLayers('h');
        else if (action === 'flip-v') flipAllLayers('v');
      });
      listen(doc, 'pointerdown', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) menu.hidden = true;
      });
    }
  }

  // ── Filter menu (Blur sub-menu — Gaussian / Zoom) ──
  {
    const btn = find('ge-filter-menu-btn');
    const menu = find('ge-filter-menu');
    if (btn && menu) {
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        const willOpen = menu.hidden;
        if (willOpen) closeOtherTopbarMenus('ge-filter-menu');
        menu.hidden = !menu.hidden;
      });
      listen(menu, 'click', (e) => {
        const item = e.target.closest('[data-filter-action]');
        if (!item) return;
        menu.hidden = true;
        const action = item.dataset.filterAction;
        if (action === 'blur-gaussian') applyGaussianBlur();
        else if (action === 'blur-zoom') applyZoomBlur();
      });
      listen(doc, 'pointerdown', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) menu.hidden = true;
      });
    }
  }

  // ── Resize popup (preset items + Custom… → resizeCustomPrompt) ──
  {
    const btn = find('ge-resize-menu-btn');
    const menu = find('ge-resize-menu');
    if (btn && menu) {
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        const willOpen = menu.hidden;
        if (willOpen) closeOtherTopbarMenus('ge-resize-menu');
        menu.hidden = !menu.hidden;
      });
      menu.querySelectorAll('[data-resize-w]').forEach(item => {
        listen(item, 'click', () => {
          menu.hidden = true;
          applyResize(parseInt(item.dataset.resizeW, 10), parseInt(item.dataset.resizeH, 10));
        });
      });
      listen(menu.querySelector('[data-resize-custom]'), 'click', () => {
        menu.hidden = true;
        resizeCustomPrompt();
      });
      listen(doc, 'pointerdown', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) menu.hidden = true;
      });
    }
  }

  return { applyResize, resizeCustomPrompt };
}
