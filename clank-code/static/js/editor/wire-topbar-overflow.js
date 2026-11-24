/**
 * Topbar overflow handler — keeps lightweight labels updated and hides
 * only low-priority AI model controls when the editor window gets narrow.
 *
 * Plus the small canvas-size display label updater (since it sits in
 * the topbar too).
 *
 * Import and Canvas stay as real topbar buttons; there is intentionally
 * no "More" overflow menu here.
 *
 * @param {{
 *   container:            HTMLElement,
 *   registerDocClickAway: (handler: (e: Event) => void) => void,
 * }} deps
 */
import { state as legacyState } from './state.js';

export function wireTopbarOverflow({ container, state = legacyState, lifetime = null }) {
  const win = container.ownerDocument.defaultView;
  if (state !== legacyState && !lifetime) throw new TypeError('isolated overflow requires lifetime');
  // Canvas-size badge updater (kept simple — it lives in the topbar).
  const sizeLabel = container.querySelector(':is(#ge-canvas-size,[data-ge-id="ge-canvas-size"])');
  function updateSizeLabel() {
    if (sizeLabel) sizeLabel.textContent = `${state.imgWidth}×${state.imgHeight}`;
  }
  updateSizeLabel();

  const topbar = container.querySelector('.ge-topbar');
  // The Gen control + its "Gen" label span — collapse as a group when
  // narrow. The Inpaint model selector moved into the side panel.
  const aiGroup = [
    container.querySelector(':is(#ge-ai-model,[data-ge-id="ge-ai-model"])'),
    ...container.querySelectorAll('.ge-topbar span[style*="font-size:9px"]'),
  ].filter(Boolean);

  function syncOverflow() {
    if (!topbar) return;
    aiGroup.forEach(el => { el.style.display = ''; });
    if (topbar.scrollWidth > topbar.clientWidth) {
      // Hide AI group first — bulky and least essential at narrow widths.
      aiGroup.forEach(el => { el.style.display = 'none'; });
    }
  }

  if (topbar && win.ResizeObserver) {
    const ro = new win.ResizeObserver(() => syncOverflow());
    ro.observe(topbar);
    lifetime?.add(() => ro.disconnect());
  }
  // Initial pass after layout settles.
  const frame = win.requestAnimationFrame(() => { if (!lifetime?.disposed) syncOverflow(); });
  lifetime?.add(() => win.cancelAnimationFrame(frame));
}
