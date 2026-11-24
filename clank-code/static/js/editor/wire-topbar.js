import { editorIdSelector } from './dom-identity.js';
/**
 * Topbar wiring — undo/redo/history, Save dropdown, zoom buttons,
 * Save/Export/Download/Project, Edge popup, and the cross-dropdown
 * coordination (close-others + global outside-click).
 *
 *   #ge-undo / #ge-redo / #ge-history-btn
 *   #ge-save-menu-btn + #ge-save-menu  (Save / Save as / Download /
 *                                       Save project / Load project)
 *   #ge-zoom-out / #ge-zoom-in / #ge-zoom-fit / #ge-zoom-100
 *   #ge-export-gallery / #ge-retry-save-copy / #ge-download
 *   #ge-save-project / #ge-load-project
 *   #ge-edge-menu-btn + #ge-edge-menu (Width input + Feather / Delete
 *                                      action buttons)
 *
 * Dropdown coordination: every menu hides any sibling menu when it
 * opens (closeOtherTopbarMenus), and a global outside-click handler
 * closes every open menu if the user clicks anywhere outside.
 *
 * @param {{
 *   undo:                 () => void,
 *   redo:                 () => void,
 *   toggleHistoryPanel:   () => void,
 *   fitZoom:              () => void,
 *   applyZoom:            () => void,
 *   exportToGallery:      () => void,
 *   retryPendingSaveCopy:  () => Promise<void>,
 *   downloadPNG:          () => void,
 *   saveProject:          () => void,
 *   loadProjectPrompt:    () => void,
 *   activeLayer:          () => object | null,
 *   saveState:            (label?: string) => void,
 *   applyEdgeFeather:     (layer: object, width: number, hardDelete: boolean) => void,
 *   composite:            () => void,
 *   registerDocClickAway: (handler: (e: Event) => void) => void,
 *   uiModule:             object,
 * }} deps
 */
import { state as legacyState } from './state.js';

const TOPBAR_MENU_IDS = ['ge-image-menu', 'ge-filter-menu', 'ge-resize-menu', 'ge-save-menu'];
const TOPBAR_TRIGGER_IDS = ['ge-image-menu-btn', 'ge-filter-menu-btn', 'ge-resize-menu-btn', 'ge-save-menu-btn'];

/**
 * Close every topbar dropdown except an optional "keep open" one.
 * Exported so the Image / Filter / Resize menus (wired elsewhere)
 * can call it from their own open handlers.
 */
export function closeOtherTopbarMenus(keepId, root = globalThis.document) {
  for (const id of TOPBAR_MENU_IDS) {
    if (id === keepId) continue;
    const m = root?.querySelector(editorIdSelector(id));
    if (m && !m.hidden) m.hidden = true;
  }
}

export function wireTopbar(deps) {
  const {state = legacyState, root = null, lifetime = null} = deps;
  if (state !== legacyState && (!root || !lifetime)) throw new TypeError('isolated topbar requires ownership');
  const ownerRoot = root || state.container, doc = ownerRoot.ownerDocument, win = doc.defaultView;
  let ownedSaveMenu = null;
  const find = id => id === 'ge-save-menu' && ownedSaveMenu ? ownedSaveMenu : ownerRoot.querySelector(editorIdSelector(id)) || ownedSaveMenu?.querySelector(editorIdSelector(id));
  const removers = [], timers = new Set();
  const listen = (node, type, fn, options) => { if (!node) return; const guarded = (...args) => { if (!lifetime?.disposed) fn(...args); }; node.addEventListener(type, guarded, options); removers.push(() => node.removeEventListener(type, guarded, options)); };
  lifetime?.add(() => { for (const remove of removers.splice(0)) remove(); for (const id of timers) clearTimeout(id); ownedSaveMenu?.remove(); });
  const {
    undo, redo, toggleHistoryPanel,
    fitZoom, applyZoom,
    exportToGallery, retryPendingSaveCopy, downloadPNG, saveProject, loadProjectPrompt,
    activeLayer, saveState, applyEdgeFeather, composite,
    registerDocClickAway, registerPortal = () => {}, portal = null, uiModule,
  } = deps;

  // Undo / Redo / History.
  listen(find('ge-undo'), 'click', undo);
  listen(find('ge-redo'), 'click', redo);
  listen(find('ge-history-btn'), 'click', toggleHistoryPanel);

  // Save dropdown — "Save ▾" toggles a small menu (Save / Save-as /
  // Download / Save project / Load project). Inner items keep their
  // original IDs so the standalone handlers below wire to them
  // unchanged.
  {
    const editorRoot = ownerRoot;
    const saveBtn = editorRoot.querySelector(':is(#ge-save-menu-btn,[data-ge-id="ge-save-menu-btn"])');
    const saveWrap = saveBtn?.closest('.ge-save-wrap');
    const saveMenu = saveWrap?.querySelector(':is(#ge-save-menu,[data-ge-id="ge-save-menu"])');
    if (saveBtn && saveMenu) {
      const saveTopbar = saveBtn.closest('.ge-topbar');
      // Reparent the menu to <body>. Without this, the menu inherits
      // the editor overlay's containing block (the overlay applies a
      // `transform: scale(...)` for its enter animation — and any
      // non-`none` transform on an ancestor makes that ancestor the
      // containing block for `position: fixed` descendants, even after
      // the animation lands on identity). The JS math below assumes
      // viewport-relative coords, so without the reparent the menu
      // ends up "way off" the button on desktop.
      ownedSaveMenu = saveMenu;
      registerPortal(saveMenu);
      const portalParent = state === legacyState ? doc.body : portal;
      if (!portalParent) throw new Error('isolated topbar portal unavailable');
      if (saveMenu.parentNode !== portalParent) {
        portalParent.appendChild(saveMenu);
      }
      const setSaveMenuOpen = (open) => {
        saveMenu.hidden = !open;
        saveTopbar?.classList.toggle('ge-topbar-menu-open', !!open);
      };
      const positionSaveMenu = () => {
        const r = saveBtn.getBoundingClientRect();
        saveMenu.style.top = `${r.bottom + 2}px`;
        saveMenu.style.right = `${Math.max(8, win.innerWidth - r.right)}px`;
        saveMenu.style.left = 'auto';
      };
      listen(saveBtn, 'click', (e) => {
        e.stopPropagation();
        const willOpen = saveMenu.hidden;
        setSaveMenuOpen(willOpen);
        if (willOpen) positionSaveMenu();
      });
      listen(saveMenu, 'click', () => { setSaveMenuOpen(false); });
      listen(win, 'resize', () => { if (!saveMenu.hidden) positionSaveMenu(); });
      listen(doc, 'pointerdown', (e) => {
        if (!saveMenu.hidden && !saveMenu.contains(e.target) && !saveBtn.contains(e.target)) {
          setSaveMenuOpen(false);
        }
      });
    }
  }

  // Zoom buttons.
  listen(find('ge-zoom-fit'), 'click', fitZoom);
  listen(find('ge-zoom-100'), 'click', () => { state.zoom = 1; applyZoom(); });
  listen(find('ge-zoom-in'), 'click', () => { state.zoom = Math.min(5, state.zoom * 1.25); applyZoom(); });
  listen(find('ge-zoom-out'), 'click', () => { state.zoom = Math.max(0.1, state.zoom / 1.25); applyZoom(); });

  // Export / Download / Project Save / Project Load.
  listen(find('ge-export-gallery'), 'click', exportToGallery);
  listen(find('ge-retry-save-copy'), 'click', async () => {
    const button = find('ge-retry-save-copy');
    if (button) button.disabled = true;
    try {
      await retryPendingSaveCopy();
      if (lifetime?.disposed) return;
      if (button) button.hidden = true;
      uiModule?.showToast?.('Saved copy binding completed.', 3500);
    } catch (error) {
      uiModule?.showToast?.(`Retry failed: ${error?.message || 'binding unavailable'}`, 5000);
    } finally {
      if (button && !lifetime?.disposed) button.disabled = false;
    }
  });
  listen(find('ge-download'), 'click', downloadPNG);
  listen(find('ge-save-project'), 'click', saveProject);
  listen(find('ge-load-project'), 'click', loadProjectPrompt);

  // Global outside-click — closes EVERY editor dropdown when the
  // user clicks anywhere that isn't a menu or trigger button. Each
  // menu has its own click-away handler too; this is a defence-in-
  // depth net for cross-menu clicks / mobile touches that miss the
  // individual handlers.
  listen(doc, 'pointerdown', (e) => {
    for (const id of TOPBAR_MENU_IDS.concat(TOPBAR_TRIGGER_IDS)) {
      const el = find(id);
      if (el && el.contains(e.target)) return;
    }
    for (const id of TOPBAR_MENU_IDS) {
      const m = find(id);
      if (m && !m.hidden) m.hidden = true;
    }
  });

  // Edge popup — Width input + Feather / Delete action buttons.
  function applyEdgeAction(hardDelete) {
    const layer = activeLayer();
    if (!layer || layer.locked) { uiModule.showToast('Select an unlocked layer'); return; }
    const widthInput = find('ge-edge-width');
    const width = parseInt(widthInput?.value || '8');
    if (isNaN(width) || width < 1) { uiModule.showToast('Invalid width'); return; }
    if (applyEdgeFeather(layer, width, hardDelete) === false) return;
    composite();
    uiModule.showToast(hardDelete ? `Edges deleted ${width}px` : `Edges feathered ${width}px`);
  }
  {
    const btn = find('ge-edge-menu-btn');
    const menu = find('ge-edge-menu');
    if (btn && menu) {
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        const willOpen = menu.hidden;
        if (willOpen) closeOtherTopbarMenus('ge-edge-menu', ownerRoot);
        menu.hidden = !menu.hidden;
        if (!menu.hidden) {
          // Autofocus the width input so users can type immediately.
          const id = setTimeout(() => { timers.delete(id); if (!lifetime?.disposed) find('ge-edge-width')?.select(); }, 0); timers.add(id);
        }
      });
      listen(find('ge-edge-feather'), 'click', () => {
        menu.hidden = true;
        applyEdgeAction(false);
      });
      listen(find('ge-edge-delete'), 'click', () => {
        menu.hidden = true;
        applyEdgeAction(true);
      });
      listen(find('ge-edge-width'), 'keydown', (e) => {
        if (e.key === 'Enter') {
          menu.hidden = true;
          applyEdgeAction(false);
        }
      });
      listen(doc, 'pointerdown', (e) => {
        if (!menu.hidden && !menu.contains(e.target) && e.target !== btn) menu.hidden = true;
      });
    }
  }
}
