/**
 * FX / adjustment-popup machinery — the per-layer Brightness/Contrast,
 * Hue/Saturation, Levels, and Color-Balance editor.
 *
 * Self-contained subsystem with three external touchpoints:
 *
 *  - `composite()`         redraw the canvas after every staged change
 *  - `saveState(label)`    push an undo entry on Apply
 *  - `renderLayerPanel()`  refresh the layer panel after add/edit
 *
 * Lifecycle:
 *
 *   FX button on layer row → openFxPopup(layer, anchor)
 *     → small chooser menu (B/C, H/S, Levels, Color Balance)
 *     → openAdjPopup(layer, type, anchor[, existingAdj])
 *       → buildAdjBody renders the type-specific sliders + histogram
 *       → sliders / histogram handles mutate `popEl._stagedAdj.params`
 *       → composite() previews live via the adjLayers stack
 *       → Apply commits to layer.adjLayers + saveState() + renderLayerPanel()
 *       → Cancel / Esc drops the staged state
 *
 * Popups can be minimised → modalManager dock chip → click chip to
 * restore. Re-opening a committed sub-layer (from the layer panel's
 * adj-row click) calls `editAdjLayer` which re-opens openAdjPopup
 * with the existing sub-layer's params staged for editing.
 *
 * @param {{
 *   composite:        () => void,
 *   saveState:        (label?: string) => void,
 *   renderLayerPanel: () => void,
 * }} deps
 *
 * @returns {{
 *   openFxPopup, openAdjPopup, editAdjLayer,
 *   closeFxPopup, closeFxMenu, closeAdjPopup,
 *   ensureFxDock, ensureAdjustments,
 *   syncFxPanelToActiveLayerIfPresent,
 *   minimiseAdjPopup,
 * }}
 */
import { state as legacyState } from '../state.js';
import legacyModalManager from '../../modalManager.js';
import {
  ADJ_ICONS,
  adjLayerLabel,
  defaultAdjParams,
} from '../layer-helpers.js';
import { drawHistogram } from './histogram.js';

export function createAdjPopupSystem({ composite, saveState, renderLayerPanel, markSceneChanged = () => {}, withSceneMutation = (_name, fn) => fn(), captureOrigin = () => null, originCurrent = () => true, onRefused = () => {}, state = legacyState, root = null, lifetime = null, modalManager = null }) {
  if (state !== legacyState && (!root || !lifetime || !modalManager)) throw new TypeError('isolated adjustment popup requires ownership');
  modalManager ||= legacyModalManager;
  const ownerRoot = root || state.container;
  const doc = ownerRoot?.ownerDocument || document, win = doc.defaultView || window;
  const portal = state === legacyState ? doc.body : ownerRoot;
  let dock = null, backdrop = null, frame = null, activePreview = null;
  const minimized = new Set(), removers = [];
  const listen = (node, type, fn, options) => { if (!node) return; const wrapped = (...args) => { if (!lifetime?.disposed && node.isConnected) fn(...args); }; node.addEventListener(type, wrapped, options); const remove = () => node.removeEventListener(type, wrapped, options); remove.node = node; removers.push(remove); };
  const removeOwned = owner => { for (let i = removers.length - 1; i >= 0; i--) if (removers[i].node && (removers[i].node === owner || owner.contains(removers[i].node))) removers.splice(i, 1)[0](); };
  lifetime?.add(() => { for (const remove of removers.splice(0)) remove(); closeFxMenu(); closeAdjPopup(); closeFxPopup(); for (const pop of minimized) { if (pop._escHandler) doc.removeEventListener('keydown', pop._escHandler, true); if (pop._modalId) modalManager.unregister(pop._modalId); pop.remove(); } minimized.clear(); dock?.remove(); if (frame != null) win.cancelAnimationFrame(frame); state.adjRafPending = false; });
  function suppressLayerGhostTap() {
    if (state === legacyState) win.__geSuppressLayerTapUntil = Date.now() + 650;
    else state.suppressLayerTapUntil = Date.now() + 650;
  }

  function closeFxPopup() {
    if (state.fxPopupEl) {
      state.fxPopupEl.remove();
      state.fxPopupEl = null;
      state.fxPopupLayerId = null;
    }
  }

  function ensureAdjustments(layer) {
    const before = JSON.stringify(layer.adjustments ?? null);
    // Older layers (loaded from saved projects) may be missing the
    // adjustments structure entirely. Pad with identity values.
    if (!layer.adjustments) layer.adjustments = {};
    const a = layer.adjustments;
    if (a.brightness === undefined) a.brightness = 1;
    if (a.contrast === undefined) a.contrast = 1;
    if (a.saturation === undefined) a.saturation = 1;
    if (a.hue === undefined) a.hue = 0;
    if (!a.levels) a.levels = { inBlack: 0, inWhite: 255, gamma: 1.0, outBlack: 0, outWhite: 255 };
    if (!a.colorBalance) a.colorBalance = {
      shadows: { r: 0, g: 0, b: 0 },
      midtones: { r: 0, g: 0, b: 0 },
      highlights: { r: 0, g: 0, b: 0 },
    };
    if (JSON.stringify(a) !== before) markSceneChanged();
    return a;
  }

  // Floating dock for minimised FX popups — lives at bottom-right.
  function ensureFxDock() {
    if (dock?.isConnected) return dock;
    if (!dock?.isConnected) {
      dock = doc.createElement('div');
      if (state === legacyState) dock.id = 'ge-fx-dock';
      else dock.className = 'ge-fx-dock';
      portal.appendChild(dock);
    }
    return dock;
  }

  function closeFxMenu() {
    if (state.fxMenuEl) {
      removeOwned(state.fxMenuEl);
      if (state.fxMenuEl._escHandler) {
        doc.removeEventListener('keydown', state.fxMenuEl._escHandler, true);
      }
      if (state.fxMenuEl._awayHandler) {
        doc.removeEventListener('pointerdown', state.fxMenuEl._awayHandler, true);
      }
      state.fxMenuEl.remove();
      state.fxMenuEl = null;
    }
    if (backdrop) { removeOwned(backdrop); backdrop.remove(); backdrop = null; }
  }

  function openFxPopup(layer, anchorEl) {
    // Toggle off ONLY if a menu for this layer is genuinely on-screen.
    // `state` is a shared singleton that survives editor close/reopen,
    // so a stale `fxMenuEl` from a previous session (whose detached
    // element still carries a now-recycled `_layerId`) used to make
    // this guard fire and silently swallow the first click. Verify the
    // element is still in the document before treating it as "open".
    if (state.fxMenuEl && portal.contains(state.fxMenuEl) &&
        state.fxMenuEl._layerId === layer.id) { closeFxMenu(); return; }
    closeFxMenu();
    backdrop = doc.createElement('div');
    if (state === legacyState) backdrop.id = 'ge-fx-menu-backdrop';
    else backdrop.className = 'ge-fx-menu-backdrop';
    backdrop.style.cssText = 'position:fixed;inset:0;z-index:10001;background:transparent;pointer-events:auto;touch-action:none;';
    portal.appendChild(backdrop);
    listen(backdrop, 'pointerdown', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      closeFxMenu();
    }, true);
    listen(backdrop, 'click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
    }, true);
    const menu = doc.createElement('div');
    menu.className = 'ge-fx-menu ge-frosted';
    menu._layerId = layer.id;
    menu._ignoreActivationUntil = Date.now() + 350;
    menu.style.zIndex = '10002';
    menu.style.pointerEvents = 'auto';
    const items = [
      { type: 'brightness-contrast', label: 'Brightness / Contrast' },
      { type: 'hue-saturation',      label: 'Hue / Saturation' },
      { type: 'levels',              label: 'Levels' },
      { type: 'color-balance',       label: 'Color Balance' },
    ];
    menu.innerHTML = items.map(i =>
      `<button class="ge-fx-menu-item" data-fx-type="${i.type}"><span class="ge-fx-menu-icon">${ADJ_ICONS[i.type] || ''}</span><span>${i.label}</span></button>`
    ).join('');
    portal.appendChild(menu);
    state.fxMenuEl = menu;
    const activateMenuItem = (btn, ev) => {
      ev?.preventDefault?.();
      ev?.stopPropagation?.();
      if (Date.now() < (menu._ignoreActivationUntil || 0)) return;
      if (!btn || btn.dataset.opening === '1') return;
      btn.dataset.opening = '1';
      const type = btn.dataset.fxType;
      closeFxMenu();
      openAdjPopup(layer, type, anchorEl);
    };
    listen(menu, 'pointerdown', (ev) => {
      ev.stopPropagation();
    }, true);
    listen(menu, 'pointerup', (ev) => {
      const btn = ev.target.closest('.ge-fx-menu-item');
      if (btn) activateMenuItem(btn, ev);
      else ev.stopPropagation();
    }, true);
    listen(menu, 'click', (ev) => {
      const btn = ev.target.closest('.ge-fx-menu-item');
      if (btn) activateMenuItem(btn, ev);
      else ev.stopPropagation();
    }, true);

    const isMobile = win.matchMedia('(max-width: 820px)').matches;
    const r = isMobile ? null : anchorEl?.getBoundingClientRect?.();
    if (isMobile) {
      menu.style.left = '';
      menu.style.top = '';
      menu.style.right = '';
      menu.style.bottom = '';
    } else if (r) {
      const menuW = 220;
      const menuH = menu.offsetHeight || 200;
      const rightX = r.right + 4;
      const leftX  = r.left - menuW - 4;
      const fitsRight = rightX + menuW <= win.innerWidth - 8;
      let left = fitsRight ? rightX : Math.max(8, leftX);
      left = Math.min(win.innerWidth - menuW - 8, Math.max(8, left));
      menu.style.left = left + 'px';
      let top = r.top;
      if (top + menuH > win.innerHeight - 8) top = r.bottom - menuH;
      top = Math.min(win.innerHeight - menuH - 8, Math.max(8, top));
      menu.style.top = top + 'px';
    }
    menu.querySelectorAll('.ge-fx-menu-item').forEach(btn => {
      const activate = (ev) => {
        activateMenuItem(btn, ev);
      };
      listen(btn, 'pointerup', activate);
      listen(btn, 'click', activate);
    });
    // Esc closes the menu, capture-phase + stopPropagation so the
    // editor overlay's own Esc handler doesn't fire too.
    const onKey = (ev) => {
      if (state !== legacyState && !ownerRoot.contains(doc.activeElement)) return;
      if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        closeFxMenu();
        doc.removeEventListener('keydown', onKey, true);
      }
    };
    doc.addEventListener('keydown', onKey, true);
    menu._escHandler = onKey;
  }

  // Hide an adj popup and drop a chip into the FX dock. Click the chip
  // to restore the popup in its previous position with staged state
  // intact (we do NOT clear staged on minimise).
  function minimiseAdjPopup(pop) {
    if (!pop) return;
    const type = pop._type;
    const r = pop.getBoundingClientRect();
    pop._stashLeft = r.left;
    pop._stashTop  = r.top;
    pop.style.display = 'none';
    if (state.adjPopupEl === pop) state.adjPopupEl = null;
    const popupId = pop._modalId || `ge-fx-popup-${Math.random().toString(36).slice(2, 8)}`;
    pop._modalId = popupId;
    minimized.add(pop);
    if (activePreview === pop) { if (frame != null) { win.cancelAnimationFrame(frame); frame = null; state.adjRafPending = false; } activePreview = null; composite(); }
    modalManager.register(popupId, {
      label: adjLayerLabel(type),
      icon: ADJ_ICONS[type] || '',
      restoreFn: () => {
        if (lifetime?.disposed) return;
        minimized.delete(pop);
        pop.style.left = pop._stashLeft + 'px';
        pop.style.top  = pop._stashTop  + 'px';
        pop.style.display = '';
        if (state.adjPopupEl && state.adjPopupEl !== pop) {
          const other = state.adjPopupEl;
          state.adjPopupEl = other;
          closeAdjPopup();
        }
        state.adjPopupEl = pop; activePreview = pop; composite();
      },
      closeFn: () => {
        minimized.delete(pop);
        closeAdjPopup(pop);
        modalManager.unregister(popupId);
      },
    });
    modalManager.minimize(popupId);
  }

  // Re-open an existing committed adjustment sub-layer for editing.
  // Pre-loads its params as the staged state; Apply updates in place.
  function editAdjLayer(layer, adj, anchorEl) {
    openAdjPopup(layer, adj.type, anchorEl, adj);
  }

  function closeAdjPopup(pop = state.adjPopupEl) {
    if (!pop) return;
    removeOwned(pop);
    if (activePreview === pop && frame != null) { win.cancelAnimationFrame(frame); frame = null; state.adjRafPending = false; }
    suppressLayerGhostTap();
    if (activePreview === pop) { activePreview = null; composite(); }
    if (pop._escHandler) doc.removeEventListener('keydown', pop._escHandler, true);
    if (pop._modalId) { try { modalManager.unregister(pop._modalId); } catch {} }
    pop._preview = null;
    pop.remove();
    if (state.adjPopupEl === pop) state.adjPopupEl = null;
  }

  function openAdjPopup(layer, type, anchorEl, existingAdj) {
    closeAdjPopup();
    // Editing an existing sub-layer? Pre-load its params as the staged
    // preview and mark the popup so Apply updates instead of appending.
    const editing = !!existingAdj;
    const startParams = editing
      ? JSON.parse(JSON.stringify(existingAdj.params))
      : defaultAdjParams(type);
    const pop = doc.createElement('div');
    pop._stagedAdj = { type, params: startParams };
    pop._origin = captureOrigin();
    pop._editingId = existingAdj?.id || null;
    pop._preview = {staged:pop._stagedAdj, editingId:pop._editingId, revision:0, cache:{}};
    activePreview = pop;
    pop.className = 'ge-adj-popup ge-frosted';
    pop.style.zIndex = '10003';
    pop._layer = layer;
    pop._type = type;
    pop._anchorEl = anchorEl;
    pop._existingAdj = existingAdj || null;
    pop.innerHTML = `
    <div class="ge-adj-head" data-adj-drag>
      <span class="ge-adj-icon">${ADJ_ICONS[type] || ''}</span>
      <span class="ge-adj-title">${adjLayerLabel(type)}</span>
      <span class="ge-head-btns">
        <button class="ge-adj-min" type="button" title="Minimise">&minus;</button>
      </span>
    </div>
    <div class="ge-adj-body" data-adj-body></div>
    <div class="ge-adj-foot">
      <button class="ge-btn ge-btn-sm ge-adj-cancel-btn" data-adj-action="cancel">Cancel</button>
      <button class="ge-btn ge-btn-sm ge-btn-primary ge-adj-apply-btn" data-adj-action="ok">Apply</button>
    </div>
    `;
    portal.appendChild(pop);
    state.adjPopupEl = pop;

    const r = anchorEl?.getBoundingClientRect?.();
    const pw = type === 'color-balance' ? 340 : 320;
    // Prefer right of anchor; fall back to left if no room.
    let left;
    if (r) {
      const rightX = r.right + 8;
      const leftX  = r.left - pw - 8;
      const fitsRight = rightX + pw <= win.innerWidth - 8;
      left = fitsRight ? rightX : Math.max(8, leftX);
    } else {
      left = (win.innerWidth - pw) / 2;
    }
    const top = r ? Math.max(8, r.top - 20) : 60;
    pop.style.left = left + 'px';
    pop.style.top  = top  + 'px';

    const body = pop.querySelector('[data-adj-body]');
    buildAdjBody(layer, type, body, pop);

    listen(pop.querySelector('.ge-adj-close'), 'click', () => closeAdjPopup(pop));
    listen(pop.querySelector('.ge-adj-min'), 'click', () => minimiseAdjPopup(pop));
    // Drag by head — anywhere except buttons. Mobile pins via !important
    // rules; setProperty with 'important' lets inline styles win during drag.
    const head = pop.querySelector('[data-adj-drag]');
    if (head) {
      const isMobile = win.matchMedia('(max-width: 820px)').matches;
      const setPos = (x, y) => {
        if (isMobile) {
          pop.style.setProperty('left', x + 'px', 'important');
          pop.style.setProperty('top', y + 'px', 'important');
          pop.style.setProperty('right', 'auto', 'important');
          pop.style.setProperty('bottom', 'auto', 'important');
          pop.style.setProperty('width', 'auto', 'important');
          pop.style.setProperty('max-width', 'calc(100vw - 16px)', 'important');
        } else {
          pop.style.left = x + 'px';
          pop.style.top = y + 'px';
        }
      };
      head.style.touchAction = 'none';
      listen(head, 'pointerdown', (e) => {
        if (e.target.closest('button')) return;
        e.preventDefault();
        const startX = e.clientX, startY = e.clientY;
        const r0 = pop.getBoundingClientRect();
        head.setPointerCapture(e.pointerId);
        head.style.cursor = 'grabbing';
        const onMove = (ev) => {
          const nx = Math.max(0, Math.min(win.innerWidth - 60, r0.left + (ev.clientX - startX)));
          const ny = Math.max(0, Math.min(win.innerHeight - 30, r0.top  + (ev.clientY - startY)));
          setPos(nx, ny);
        };
        const onUp = () => {
          head.releasePointerCapture(e.pointerId);
          head.style.cursor = '';
          head.removeEventListener('pointermove', onMove);
          head.removeEventListener('pointerup', onUp);
        };
        head.addEventListener('pointermove', onMove);
        head.addEventListener('pointerup', onUp);
        const cleanup = () => { try { onUp(); } catch {} head.removeEventListener('pointermove', onMove); head.removeEventListener('pointerup', onUp); }; cleanup.node = head; removers.push(cleanup);
      });
    }
    // Esc closes; capture-phase + stopPropagation so the editor overlay's
    // own Esc handler doesn't fire too.
    const onKey = (ev) => {
      if (state.adjPopupEl !== pop || (state !== legacyState && !ownerRoot.contains(doc.activeElement))) return;
      if (ev.key === 'Escape') {
        ev.preventDefault();
        ev.stopPropagation();
        closeAdjPopup(pop);
        doc.removeEventListener('keydown', onKey, true);
      }
    };
    doc.addEventListener('keydown', onKey, true);
    pop._escHandler = onKey;
    listen(pop.querySelector('[data-adj-action="cancel"]'), 'click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      closeAdjPopup();
    });
    listen(pop.querySelector('[data-adj-action="ok"]'), 'click', (e) => {
      e.preventDefault();
      e.stopPropagation();
      suppressLayerGhostTap();
      const current = () => !lifetime?.disposed && originCurrent(pop._origin) && state.layers.includes(layer) && (!editing || layer.adjLayers?.includes(existingAdj));
      if (!current()) { onRefused(); return; }
      const accepted = withSceneMutation('adjustment-apply', () => {
        if (!current()) return false;
        const params = JSON.parse(JSON.stringify(pop._stagedAdj.params));
        saveState(editing ? `Edit ${adjLayerLabel(type)}` : `Add ${adjLayerLabel(type)}`);
        if (editing) existingAdj.params = params;
        else { if (!layer.adjLayers) layer.adjLayers = []; layer.adjLayers.push({ id: 'adj-' + Math.random().toString(36).slice(2, 9), type, name: adjLayerLabel(type), visible: true, opacity: 1, params }); }
        layer._adjFinalKey = null; markSceneChanged(); renderLayerPanel(); return true;
      });
      if (accepted !== true) { onRefused(); return; }

      closeAdjPopup();
    });
  }

  // rAF-throttled live preview while sliders are dragged.
  function scheduleAdjRefresh(layer, pop) {
    if (state.adjRafPending) return;
    state.adjRafPending = true;
    frame = win.requestAnimationFrame(() => {
      frame = null; state.adjRafPending = false;
      if (lifetime?.disposed || activePreview !== pop || !state.layers.includes(layer)) return;
      composite();
    });
  }

  function buildAdjBody(layer, type, body, popEl) {
    const p = popEl._stagedAdj.params;
    const revertIcon = '<svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>';
    const sliderRow = (key, label, min, max, value, suffix) => `
      <div class="ge-adj-row" data-adj-key="${key}">
        <label>${label}</label>
        <input type="range" min="${min}" max="${max}" value="${value}" data-key="${key}" />
        <span class="ge-adj-value">${value}${suffix || ''}</span>
        <button class="ge-adj-revert" type="button" title="Reset this slider" data-revert-key="${key}">${revertIcon}</button>
      </div>
    `;
    if (type === 'brightness-contrast') {
      const bSlider = Math.round((p.brightness - 1) * 100);
      const cSlider = Math.round((p.contrast - 1) * 100);
      body.innerHTML = `
      ${sliderRow('brightness', 'Brightness', -100, 100, bSlider, '')}
      ${sliderRow('contrast',   'Contrast',   -100, 100, cSlider, '')}
    `;
    } else if (type === 'hue-saturation') {
      const hSlider = Math.round(p.hue);
      const sSlider = Math.round((p.saturation - 1) * 100);
      body.innerHTML = `
      ${sliderRow('hue',        'Hue',        -180, 180, hSlider, ' °')}
      ${sliderRow('saturation', 'Saturation', -100, 100, sSlider, '')}
    `;
    } else if (type === 'levels') {
      // Histogram canvas + sliders. Histogram is computed from the
      // layer's pixel data (after any adjLayers below this one) so
      // the user is matching levels against what they're really seeing.
      // <details> wrapper is collapsed by default on mobile to save
      // vertical space; open by default on desktop.
      const isMobile = win.matchMedia('(max-width: 820px)').matches;
      body.innerHTML = `
      <details class="ge-adj-hist-details"${isMobile ? '' : ' open'}>
        <summary>Histogram</summary>
        <div class="ge-adj-hist-wrap">
          <canvas class="ge-adj-histogram" width="280" height="80"></canvas>
          <div class="ge-adj-hist-handles">
            <div class="ge-adj-hist-handle hist-h-black"  data-handle="inBlack"  title="Input black — drag"></div>
            <div class="ge-adj-hist-handle hist-h-gamma"  data-handle="gamma"    title="Gamma — drag"></div>
            <div class="ge-adj-hist-handle hist-h-white"  data-handle="inWhite"  title="Input white — drag"></div>
          </div>
        </div>
      </details>
      ${sliderRow('inBlack',  'Input black',  0, 254, p.inBlack, '')}
      ${sliderRow('inWhite',  'Input white',  1, 255, p.inWhite, '')}
      ${sliderRow('gamma',    'Gamma',        10, 990, Math.round((p.gamma || 1) * 100), 'γ')}
      ${sliderRow('outBlack', 'Output black', 0, 255, p.outBlack, '')}
      ${sliderRow('outWhite', 'Output white', 0, 255, p.outWhite, '')}
    `;
      const hist = body.querySelector('.ge-adj-histogram');
      drawHistogram(hist, layer);
      wireHistogramHandles(body, layer, type, popEl);
      // Redraw histogram when the user opens the disclosure (canvas
      // dimensions are layout-dependent).
      listen(body.querySelector('.ge-adj-hist-details'), 'toggle', (e) => {
        if (e.target.open) drawHistogram(hist, layer);
      });
    } else if (type === 'color-balance') {
      // Color-tinted slider ends so the user sees what direction does what.
      const cbRow = (key, leftCol, rightCol, label, value) => `
      <div class="ge-adj-row ge-adj-cb-row" data-adj-key="${key}">
        <span class="ge-adj-cb-dot" style="background:${leftCol}"></span>
        <input type="range" min="-100" max="100" value="${value}" data-key="${key}" />
        <span class="ge-adj-cb-dot" style="background:${rightCol}"></span>
        <span class="ge-adj-value">${value}</span>
        <button class="ge-adj-revert" type="button" title="Reset this slider" data-revert-key="${key}">${revertIcon}</button>
      </div>
    `;
      // Tone picker: one tone group visible at a time. Remember the
      // last picked tone on the popup so re-renders (revert button
      // etc.) keep it.
      const tone = popEl._cbTone || 'shadows';
      popEl._cbTone = tone;
      const toneSliders = (t) => `
      ${cbRow(`${t}-r`, '#00d2d2', '#ff5555', 'Cyan ↔ Red',      p[t].r)}
      ${cbRow(`${t}-g`, '#d855d8', '#55d855', 'Magenta ↔ Green', p[t].g)}
      ${cbRow(`${t}-b`, '#e6e64a', '#4a78ff', 'Yellow ↔ Blue',   p[t].b)}
    `;
      body.innerHTML = `
      <div class="ge-adj-cb-tone-picker">
        <select class="ge-adj-cb-tone-select">
          <option value="shadows"${tone === 'shadows' ? ' selected' : ''}>Shadows</option>
          <option value="midtones"${tone === 'midtones' ? ' selected' : ''}>Midtones</option>
          <option value="highlights"${tone === 'highlights' ? ' selected' : ''}>Highlights</option>
        </select>
      </div>
      <div class="ge-adj-cb-sliders" data-cb-tone="${tone}">
        ${toneSliders(tone)}
      </div>
    `;
      listen(body.querySelector('.ge-adj-cb-tone-select'), 'change', (e) => {
        popEl._cbTone = e.target.value;
        body.innerHTML = '';
        buildAdjBody(layer, type, body, popEl);
      });
    }
    // Wire all sliders.
    body.querySelectorAll('input[type="range"]').forEach(sl => {
      listen(sl, 'input', () => onAdjSliderInput(layer, type, sl, popEl));
    });
    // Per-slider revert buttons.
    body.querySelectorAll('.ge-adj-revert').forEach(btn => {
      listen(btn, 'click', (e) => {
        e.stopPropagation();
        const key = btn.dataset.revertKey;
        revertAdjKey(layer, type, key, popEl);
        // Rebuild body so values + histogram refresh.
        body.innerHTML = '';
        buildAdjBody(layer, type, body, popEl);
      });
    });
  }

  // Reset a single slider key back to identity. Updates staged params
  // and triggers a composite refresh.
  function revertAdjKey(layer, type, key, popEl) {
    const defaults = defaultAdjParams(type);
    const p = popEl._stagedAdj.params;
    if (type === 'brightness-contrast' || type === 'hue-saturation') {
      p[key] = defaults[key];
    } else if (type === 'levels') {
      p[key] = defaults[key];
    } else if (type === 'color-balance') {
      const [tone, ch] = key.split('-');
      p[tone][ch] = defaults[tone][ch];
    }
    if (activePreview === popEl) composite();
  }

  function onAdjSliderInput(layer, type, sl, popEl) {
    const key = sl.dataset.key;
    const raw = parseInt(sl.value, 10);
    const valEl = sl.parentElement.querySelector('.ge-adj-value');
    const p = popEl._stagedAdj.params;
    let display = String(raw);
    if (type === 'brightness-contrast' || type === 'hue-saturation') {
      if (key === 'brightness' || key === 'contrast' || key === 'saturation') {
        p[key] = 1 + raw / 100;
      } else if (key === 'hue') {
        p.hue = raw; display = raw + ' °';
      }
    } else if (type === 'levels') {
      if (key === 'gamma') {
        p.gamma = raw / 100; display = (raw / 100).toFixed(2) + 'γ';
      } else {
        p[key] = raw;
      }
    } else if (type === 'color-balance') {
      const [tone, ch] = key.split('-');
      p[tone][ch] = raw;
    }
    if (valEl) valEl.textContent = display;
    scheduleAdjRefresh(layer, popEl);
  }

  // Position the three histogram triangle handles by current staged
  // values + wire pointer drags.
  function wireHistogramHandles(bodyEl, layer, type, popEl) {
    const wrap = bodyEl.querySelector('.ge-adj-hist-wrap');
    const canvas = bodyEl.querySelector('.ge-adj-histogram');
    if (!wrap || !canvas) return;
    const handles = bodyEl.querySelectorAll('.ge-adj-hist-handle');
    const placeHandles = () => {
      const w = canvas.getBoundingClientRect().width;
      const p = popEl._stagedAdj.params;
      const xB = (p.inBlack  / 255) * w;
      const xW = (p.inWhite  / 255) * w;
      // Gamma handle sits at a fraction of the (xB..xW) span, mapped
      // from gamma's log scale (1 = midpoint, 0.1 = far right, 10 = far left).
      const gammaT = 1 - (Math.log(p.gamma || 1) / Math.log(10) * 0.5 + 0.5);
      const xG = xB + (xW - xB) * gammaT;
      const set = (sel, x) => {
        const el = bodyEl.querySelector(sel);
        if (el) el.style.left = (x - 6) + 'px';
      };
      set('.hist-h-black', xB);
      set('.hist-h-gamma', xG);
      set('.hist-h-white', xW);
    };
    placeHandles();
    handles.forEach(h => {
      listen(h, 'pointerdown', (e) => {
        e.preventDefault();
        e.stopPropagation();
        h.setPointerCapture(e.pointerId);
        const which = h.dataset.handle;
        const rect = canvas.getBoundingClientRect();
        const onMove = (ev) => {
          const x = Math.max(0, Math.min(rect.width, ev.clientX - rect.left));
          const v = Math.round((x / rect.width) * 255);
          const p = popEl._stagedAdj.params;
          if (which === 'inBlack') {
            p.inBlack = Math.min(p.inWhite - 1, v);
          } else if (which === 'inWhite') {
            p.inWhite = Math.max(p.inBlack + 1, v);
          } else if (which === 'gamma') {
            const xB = (p.inBlack / 255) * rect.width;
            const xW = (p.inWhite / 255) * rect.width;
            const span = Math.max(1, xW - xB);
            let t = (x - xB) / span;
            t = Math.max(0.01, Math.min(0.99, t));
            // Invert the placeHandles mapping: t = 1 - (log10(g)*0.5+0.5).
            const log10g = -((t - 0.5) * 2);
            p.gamma = Math.pow(10, log10g);
          }
          placeHandles();
          // Update visible slider rows + value labels.
          const updateRow = (key, displayVal) => {
            const sl = bodyEl.querySelector(`input[type="range"][data-key="${key}"]`);
            if (sl) sl.value = String(key === 'gamma' ? Math.round(popEl._stagedAdj.params.gamma * 100) : popEl._stagedAdj.params[key]);
            const val = sl?.parentElement.querySelector('.ge-adj-value');
            if (val) val.textContent = displayVal;
          };
          if (which === 'inBlack') updateRow('inBlack', String(popEl._stagedAdj.params.inBlack));
          if (which === 'inWhite') updateRow('inWhite', String(popEl._stagedAdj.params.inWhite));
          if (which === 'gamma')   updateRow('gamma',   popEl._stagedAdj.params.gamma.toFixed(2) + 'γ');
          drawHistogram(canvas, layer);
          scheduleAdjRefresh(layer, popEl);
        };
        const onUp = () => {
          h.releasePointerCapture(e.pointerId);
          h.removeEventListener('pointermove', onMove);
          h.removeEventListener('pointerup', onUp);
        };
        h.addEventListener('pointermove', onMove);
        h.addEventListener('pointerup', onUp);
        const cleanup = () => { try { onUp(); } catch {} h.removeEventListener('pointermove', onMove); h.removeEventListener('pointerup', onUp); }; cleanup.node = h; removers.push(cleanup);
      });
    });
  }

  // Legacy sidebar-FX panel sync — FX now lives in a per-layer popup;
  // stubbed so any stale callers don't error.
  function syncFxPanelToActiveLayerIfPresent() { /* no-op */ }

  return {
    openFxPopup, openAdjPopup, editAdjLayer,
    closeFxPopup, closeFxMenu, closeAdjPopup,
    ensureFxDock, ensureAdjustments,
    syncFxPanelToActiveLayerIfPresent,
    minimiseAdjPopup,
    invalidatePreview(layer) { for (const pop of [activePreview, ...minimized]) if (pop?._layer === layer && pop._preview) pop._preview.cache = {}; },
    previewFor(layer) { if (activePreview?._layer !== layer || lifetime?.disposed || !originCurrent(activePreview._origin) || !state.layers.includes(layer)) return null; activePreview._preview.revision = state.sceneRevision; return activePreview._preview; },
  };
}
