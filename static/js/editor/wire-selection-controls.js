import { editorIdSelector } from './dom-identity.js';
/**
 * Lasso + Magic Wand panel controls — sliders, mode toggles, and the
 * panel action buttons (Invert / Clear / Delete / Copy / To Mask /
 * Bg Remove). The actual selection algorithms live in their tool
 * modules (editor/tools/lasso.js, editor/tools/wand.js); this file
 * just wires the side-panel UI to them.
 *
 *   Lasso section:
 *     #ge-lasso-feather       slider, updates label + preview, recomposites
 *     #ge-lasso-grow          slider, updates label + recomposites
 *     #ge-lasso-invert        → invertSelection
 *     #ge-lasso-delete        → lassoDeleteSelection
 *     #ge-lasso-copy          → lassoCopyToLayer
 *     #ge-lasso-mask          → lassoToMask
 *
 *   Wand section:
 *     #ge-wand-feather        slider, updates label + recomposites
 *     #ge-wand-grow           slider, updates label + recomposites
 *     #ge-wand-tolerance      slider, updates future wand-click tolerance
 *     #ge-wand-live           opt-in rAF-coalesced live retune while dragging
 *     .ge-wand-mode-btn       segmented toggle (New / Add / Subtract)
 *     #ge-wand-vis            toggle the translucent red overlay
 *     #ge-wand-clear / -invert / -delete / -copy / -mask / -rembg
 *
 * @param {{
 *   composite:               () => void,
 *   invertSelection:         () => boolean,
 *   lassoDeleteSelection:    () => void,
 *   lassoCopyToLayer:        () => void,
 *   lassoToMask:             () => void,
 *   runMagicWand:            (x: number, y: number, mode: string, opts?: object) => void,
 *   wandClear:               () => void,
 *   wandDeleteSelection:     () => void,
 *   wandCopyToNewLayer:      () => void,
 *   wandToMask:              () => void,
 *   buildSelectionHintMask:  () => string | null,
 *   applyImageTool:          (endpoint, payload, name, btn, opts?) => Promise<void>,
 *   uiModule:                object,
 * }} deps
 */
import { state as legacyState } from './state.js';

const EYE_OPEN = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_OFF  = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><line x1="8" y1="16" x2="16" y2="8"/><line x1="8" y1="8" x2="16" y2="16"/></svg>';

export function wireSelectionControls({ state = legacyState, root = null, lifetime = null,
  composite,
  invertSelection,
  lassoDeleteSelection, lassoCopyToLayer, lassoToMask,
  runMagicWand, captureWandOrigin = () => null,
  wandClear, wandDeleteSelection, wandCopyToNewLayer, wandToMask,
  buildSelectionHintMask, applyImageTool,
  uiModule,
}) {
  if (state !== legacyState && (!root || !lifetime)) throw new TypeError('isolated controls require ownership');
  const ownerRoot = root || state.container;
  const win = ownerRoot?.ownerDocument?.defaultView || window;
  const find = id => ownerRoot?.querySelector(editorIdSelector(id));
  const removers = [];
  const listen = (node, type, fn) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded); removers.push(() => node.removeEventListener(type, guarded)); };
  const frames = new Set();
  const frame = fn => { const id = win.requestAnimationFrame(() => { frames.delete(id); if (!lifetime?.disposed) fn(); }); frames.add(id); return id; };
  lifetime?.add(() => { for (const id of frames) win.cancelAnimationFrame(id); for (const remove of removers.splice(0)) remove(); });

  // ── Lasso section ──
  const lassoFPrev = find('ge-lasso-feather-preview');
  function syncLassoFeather(v) {
    if (!lassoFPrev) return;
    const inner = Math.max(0, 50 - v * 1.0);
    lassoFPrev.style.background = `radial-gradient(circle, var(--fg) 0%, var(--fg) ${inner}%, transparent 75%)`;
  }
  syncLassoFeather(0);
  listen(find('ge-lasso-feather'), 'input', (e) => {
    find('ge-lasso-feather-label').textContent = e.target.value + 'px';
    syncLassoFeather(parseInt(e.target.value, 10));
    composite();
  });
  listen(find('ge-lasso-grow'), 'input', (e) => {
    find('ge-lasso-grow-label').textContent = e.target.value + 'px';
    composite();
  });
  listen(find('ge-lasso-delete'), 'click', () => {
    if (state.lassoPoints.length >= 3 && !state.lassoActive) lassoDeleteSelection();
    else if (state.wandMask) wandDeleteSelection();
  });
  listen(find('ge-lasso-copy'), 'click', () => {
    if (state.lassoPoints.length >= 3 && !state.lassoActive) lassoCopyToLayer();
    else if (state.wandMask) wandCopyToNewLayer();
  });
  listen(find('ge-lasso-mask'), 'click', () => {
    if (state.lassoPoints.length >= 3 && !state.lassoActive) lassoToMask();
    else if (state.wandMask) wandToMask();
  });
  listen(find('ge-lasso-invert'), 'click', invertSelection);

  // ── Wand section ──
  listen(find('ge-wand-feather'), 'input', (e) => {
    const v = parseInt(e.target.value, 10) || 0;
    find('ge-wand-feather-label').textContent = v + 'px';
    const prev = find('ge-wand-feather-preview');
    if (prev) prev.style.setProperty('--feather-blur', Math.min(v / 14, 8) + 'px');
    composite();
  });
  listen(find('ge-wand-grow'), 'input', (e) => {
    find('ge-wand-grow-label').textContent = e.target.value + 'px';
    composite();
  });

  // Tolerance slider fires `input` rapidly — coalesce to one wand run
  // per frame with rAF. Label updates synchronously so the number
  // tracks the cursor even when the flood-fill runs at ~60fps.
  let wandRetuneRaf = null;
  const retuneWand = () => {
    if (!state.wandLastSeed || !state.wandMask) return;
    if (wandRetuneRaf) return;
    const expected=captureWandOrigin(), seed={...state.wandLastSeed};
    wandRetuneRaf = frame(() => {
      wandRetuneRaf = null;
      runMagicWand(seed.x, seed.y, 'replace', { retune: true, expected });
    });
  };
  const liveBtn = find('ge-wand-live');
  listen(liveBtn, 'click', () => {
    state.wandLiveRetune = !state.wandLiveRetune;
    liveBtn.classList.toggle('active', state.wandLiveRetune);
    liveBtn.setAttribute('aria-pressed', state.wandLiveRetune ? 'true' : 'false');
    if (state.wandLiveRetune) retuneWand();
  });
  listen(find('ge-wand-tolerance'), 'input', (e) => {
    state.wandTolerance = parseInt(e.target.value, 10);
    const lbl = find('ge-wand-tol-label');
    if (lbl) lbl.textContent = state.wandTolerance;
    const wp = find('ge-wand-tol-preview');
    if (wp) wp.style.opacity = (state.wandTolerance / 100).toFixed(2);
    if (state.wandLiveRetune) retuneWand();
  });

  // Wand mode segmented toggle (New / Add / Subtract).
  ownerRoot.querySelectorAll('.ge-wand-mode-btn').forEach(btn => {
    listen(btn, 'click', () => {
      const mode = btn.dataset.wandMode;
      if (!mode) return;
      state.wandMode = mode;
      ownerRoot.querySelectorAll('.ge-wand-mode-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.wandMode === mode);
      });
    });
  });

  // Toggle the translucent red overlay for the wand selection.
  listen(find('ge-wand-vis'), 'click', () => {
    state.wandMaskVisible = !state.wandMaskVisible;
    const btn = find('ge-wand-vis');
    if (btn) {
      btn.innerHTML = state.wandMaskVisible ? EYE_OPEN : EYE_OFF;
      btn.title = state.wandMaskVisible ? 'Hide selection overlay' : 'Show selection overlay';
      btn.classList.toggle('visible', state.wandMaskVisible);
    }
    composite();
  });

  listen(find('ge-wand-clear'), 'click', wandClear);
  listen(find('ge-wand-invert'), 'click', invertSelection);
  listen(find('ge-wand-delete'), 'click', wandDeleteSelection);
  listen(find('ge-wand-copy'), 'click', wandCopyToNewLayer);
  listen(find('ge-wand-mask'), 'click', wandToMask);
  // Selection-constrained Bg Remove — reuses the same path the toolbar
  // Bg Remove button does. buildSelectionHintMask picks the active
  // wand/lasso selection, so this just kicks off the existing flow.
  listen(find('ge-wand-rembg'), 'click', async () => {
    const btn = find('ge-wand-rembg');
    const hint = buildSelectionHintMask();
    if (!hint) { if (uiModule) uiModule.showToast('Click to make a wand selection first'); return; }
    const result = await applyImageTool('/api/image/remove-bg', { hint_mask: hint }, 'BG Removed', btn);
    if (!lifetime?.disposed && result?.status === 'applied') wandClear();
  });

  // Live tolerance preview (just opacity-tracking like sharpen).
  const wandTolPrev = find('ge-wand-tol-preview');
  if (wandTolPrev) wandTolPrev.style.opacity = (state.wandTolerance / 100).toFixed(2);
}
