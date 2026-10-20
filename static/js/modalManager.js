import { markAppletFrame } from './windowResize.js';
import { uiIcon, hasUiIcon } from './uiIcons.js';
/**
 * ModalManager — unified open/minimize/close behavior for tool modals.
 *
 * Goals:
 *  - Tab-down (swipe) and the `_` button MINIMIZE: modal hidden, JS state preserved.
 *  - The ✕ button CLOSES: tears down via the registered closeFn.
 *  - Sidebar/rail click handler: closed → open, minimized → restore, open → minimize.
 *  - Rail icon shows a "minimized" badge when state is held.
 *
 * Usage from a tool module:
 *
 *   import * as Modals from './modalManager.js';
 *
 *   // After building the modal element and adding it to the body:
 *   Modals.register('tool-modal', {
 *     railBtnId: 'tool-button',
 *     restoreFn: () => { ...whatever the tool needs to do when un-hiding... },
 *     closeFn:   () => { ...full teardown — remove modal element etc... },
 *   });
 *
 *   // From the sidebar/rail button click handler:
 *   if (!Modals.toggle('tool-modal')) {
 *     // No registered modal — build and open it fresh.
 *     openTool();
 *   }
 */

import { previewZoneAt, clearPreview, snapModalToZone } from './tileManager.js';
import { suspendDock, resumeDock, clearRightDock, applyEdgeDock } from './modalSnap.js';
import { dismissOrRemove } from './escMenuStack.js';
import { nextToolWindowZ } from './toolWindowZOrder.js';

const _state = new Map(); // id -> { restoreFn, closeFn, railBtnId, isMinimized, restoreMinHeight }

const _rememberedDockKey = (id) => `odysseus-modal-remembered-dock-${id}`;
function _rememberDock(id, side) {
  if (!id || !side) return;
  try { localStorage.setItem(_rememberedDockKey(id), side); } catch (_) {}
}
function _forgetDock(id) {
  if (!id) return;
  try { localStorage.removeItem(_rememberedDockKey(id)); } catch (_) {}
}
function _getRememberedDock(id) {
  try {
    const side = localStorage.getItem(_rememberedDockKey(id));
    return (side === 'left' || side === 'right') ? side : null;
  } catch (_) {
    return null;
  }
}
function _applyRememberedDock(id) {
  const side = _getRememberedDock(id);
  if (!side) return;
  const modal = document.getElementById(id);
  if (!modal || modal.classList.contains('hidden') || modal.classList.contains('modal-minimized')) return;
  try { applyEdgeDock(modal, side); } catch (e) { console.warn('apply remembered dock failed', e); }
}

// Monotonic stacking counter so the most-recently-surfaced tool window always
// sits on top. Tool modals otherwise carry fixed CSS z-indexes (base .modal
// = 250, cookbook/theme = 260, …), so restoring one from the dock could leave
// it BEHIND an already-open tool with a higher static z-index. Start above
// those statics and bump on every bring-to-front.
let _modalTopZ = 300;
function _bringToFront(modal) {
  if (!modal) return;
  const z = nextToolWindowZ({
    exclude: modal,
    current: getComputedStyle(modal).zIndex,
    floor: _modalTopZ,
  });
  _modalTopZ = Math.max(_modalTopZ, z);
  modal.style.setProperty('z-index', String(z), 'important');
}

function _emitModalOpened(id, modal) {
  try {
    window.dispatchEvent(new CustomEvent('odysseus:modal-opened', {
      detail: { id, modal },
    }));
  } catch (_) {}
}

function _emitModalClosed(id, modal) {
  try {
    window.dispatchEvent(new CustomEvent('odysseus:modal-closed', {
      detail: { id, modal },
    }));
  } catch (_) {}
}

function _emitModalLifecycle(type, id, modal) {
  try {
    window.dispatchEvent(new CustomEvent(`odysseus:modal-${type}`, {
      detail: { id, modal },
    }));
  } catch (_) {}
}

function _captureRestoreHeight(modal, state) {
  if (!modal || !state) return;
  const content = modal.querySelector('.modal-content');
  if (!content) return;
  if (modal.id === 'email-lib-modal'
      && (modal.classList.contains('modal-left-docked')
          || modal.classList.contains('email-snap-left')
          || document.body.classList.contains('email-doc-split-active'))) {
    delete state.restoreMinHeight;
    return;
  }
  const rect = content.getBoundingClientRect();
  if (!rect || rect.height < 120) return;
  const maxHeight = Math.max(180, window.innerHeight - 24);
  const minHeight = modal.id === 'email-lib-modal' && window.innerWidth > 768
    ? Math.min(560, maxHeight)
    : 0;
  state.restoreMinHeight = `${Math.round(Math.max(minHeight, Math.min(rect.height, maxHeight)))}px`;
}

function _applyRestoreHeight(modal, state) {
  if (!modal || !state?.restoreMinHeight) return;
  const content = modal.querySelector('.modal-content');
  if (!content) return;
  const maxHeight = Math.max(180, window.innerHeight - 24);
  const requested = parseInt(state.restoreMinHeight, 10);
  const minHeight = modal.id === 'email-lib-modal' && window.innerWidth > 768
    ? Math.min(560, maxHeight)
    : 0;
  const height = Number.isFinite(requested) ? Math.max(minHeight, Math.min(requested, maxHeight)) : null;
  if (height) content.style.minHeight = `${height}px`;
}

function _setBadge(btnIds, on) {
  if (!btnIds) return;
  const ids = Array.isArray(btnIds) ? btnIds : [btnIds];
  for (const id of ids) {
    const btn = document.getElementById(id);
    if (btn) btn.classList.toggle('rail-minimized', on);
  }
}

// ── Bottom dock — visible chip per minimized modal ──

const _LABELS = {
  'cookbook-modal':    { label: 'Cookbook',  icon: "cookbook" },
  'calendar-modal':    { label: 'Calendar',  icon: "calendar" },
  'tasks-modal':       { label: 'Clanker Tasks', icon: "tasks" },
  'doclib-modal':      { label: 'Library',   icon: "library" },
  'memory-modal':      { label: 'Memery',     icon: "memory" },
  'notes-panel':       { label: 'Notes',     icon: "code" },
  'email-lib-modal':   { label: 'Email',     icon: "email" },
  'custom-preset-modal': { label: 'Prompt',  icon: "sparkles" },
  'research-overlay':  { label: 'Research',  icon: "research" },
  'theme-modal':       { label: 'Theme',     icon: "hue" },
  'compare-model-overlay': { label: 'Compare',  icon: "compare" },
  'settings-modal':    { label: 'Settings',  icon: "settings" },
  'ge-shortcuts-modal':{ label: 'Shortcuts', icon: "keyboard" },
  // Virtual id — the doc editor pane isn't a modal, but it minimizes to a
  // chip via the same dock infrastructure.
  'doc-panel':         { label: 'Document', icon: "document" },
};

function _ensureDock() {
  let dock = document.getElementById('minimized-dock');
  if (dock) return dock;
  dock = document.createElement('div');
  dock.id = 'minimized-dock';
  document.body.appendChild(dock);
  _loadDockState();
  if (typeof ResizeObserver !== 'undefined') {
    new ResizeObserver(() => _syncDockSpace(dock)).observe(dock);
  }
  return dock;
}

// Manual order users can rearrange via drag.
let _dockOrder = [];
// Positions exist only during a gesture; every completed gesture settles
// back into the shared row. Old preference coordinates remain recoverable.
const _chipPositions = new Map();
let _dockPos = null;
const _DOCK_ORDER_KEY = 'odysseus.minimizedDockOrder.v1';
let _dockStateLoaded = false;

function _saveDockState() {
  // Keep the exact legacy mobileDockState bytes untouched for recovery.
  // Position preferences have no authority over the shared row.
  try { localStorage.setItem(_DOCK_ORDER_KEY, JSON.stringify(_dockOrder)); } catch {}
}

function _loadDockState() {
  if (_dockStateLoaded) return;
  _dockStateLoaded = true;
  try {
    const order = JSON.parse(localStorage.getItem(_DOCK_ORDER_KEY) || '[]');
    if (Array.isArray(order)) {
      _dockOrder = [...new Set(order.filter(id => typeof id === 'string'))];
    }
  } catch {}
}

function _syncDockSpace(dock) {
  const visible = dock.style.display !== 'none' && dock.childElementCount > 0;
  document.documentElement.style.setProperty('--minimized-dock-height', visible ? `${dock.offsetHeight}px` : '0px');
  document.body.classList.toggle('has-minimized-dock', visible);
}

function _applyDockPos(dock) {
  _dockPos = null;
  for (const property of ['left', 'top', 'right', 'bottom', 'transform']) {
    dock.style.removeProperty(property);
  }
}

if (typeof window !== 'undefined') window.addEventListener('resize', () => {
  const dock = document.getElementById('minimized-dock');
  if (!dock || window._chipDragging) return;
  _applyDockPos(dock);
  _syncDockSpace(dock);
});

function _renderDock() {
  const dock = document.getElementById('minimized-dock');
  if (!dock) return;
  const minimizedIds = [..._state.entries()].filter(([_, s]) => s.isMinimized).map(([id]) => id);
  _chipPositions.clear();
  const allIds = minimizedIds;
  // Keep _dockOrder for every modal still alive in _state — even when it's
  // currently restored (not in allIds). That way re-minimizing a chip lands
  // back in its original slot instead of being pushed to the right edge.
  // Ids only fall out of _dockOrder once the modal is fully closed
  // (close() → _state.delete()).
  _dockOrder = _dockOrder.filter(id => _state.has(id));
  for (const id of allIds) {
    if (!_dockOrder.includes(id)) _dockOrder.push(id);
  }

  // Capture any custom data-* attributes (e.g. data-tab-num) BEFORE we
  // remove old chips, so they can be restored on the rebuilt chips.
  // Without this, external systems that stamp attributes on chips
  // (like emailLibrary's slot-number badge) see the attribute wiped on
  // every re-render — most visibly after a chain drag, when chips are
  // at body level and get swept by the next render.
  const oldData = new Map();
  document.querySelectorAll('.minimized-dock-chip').forEach(c => {
    const id = c.dataset.modalId;
    if (!id) return;
    const data = {};
    for (const a of c.attributes) {
      if (a.name.startsWith('data-') && a.name !== 'data-modal-id') {
        data[a.name] = a.value;
      }
    }
    if (Object.keys(data).length) oldData.set(id, data);
  });

  // Sweep any free-positioned chips currently on <body> first — they'll be
  // recreated below if still alive, but if _dockOrder ended up empty (e.g.
  // the chain close-all just finished) we need to clear them here too.
  // Previously this sweep only ran in the non-empty branch, leaving the
  // last-rendered chip orphaned on body after the final close.
  document.querySelectorAll('body > .minimized-dock-chip').forEach(c => c.remove());

  // _dockOrder keeps every alive modal's slot (so order is stable across
  // restore→minimize cycles), but we only render chips for ids currently
  // in allIds (minimized or persistent).
  const renderIds = _dockOrder.filter(id => allIds.includes(id));

  if (!renderIds.length) {
    dock.innerHTML = '';
    // Scrub ALL drag/animation inline styles, then re-apply display:none so
    // the empty dock stays hidden until new chips arrive.
    dock.style.cssText = '';
    dock.style.display = 'none';
    _syncDockSpace(dock);
    return;
  }

  // FLIP: capture old positions
  const oldRects = new Map();
  dock.querySelectorAll('.minimized-dock-chip').forEach(c => {
    oldRects.set(c.dataset.modalId, c.getBoundingClientRect());
  });

  dock.style.display = '';
  // Clear transient drag positioning before populating the settled row.
  _applyDockPos(dock);
  dock.innerHTML = '';
  for (const id of renderIds) {
    const meta = _LABELS[id] || { label: id, icon: '' };
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'minimized-dock-chip';
    chip.dataset.modalId = id;
    chip.title = `Restore ${meta.label}`;
    // Restore any external data-* attributes the previous chip carried
    // (e.g. emailLibrary's data-tab-num slot-number badge).
    const prevAttrs = oldData.get(id);
    if (prevAttrs) {
      for (const [name, val] of Object.entries(prevAttrs)) {
        chip.setAttribute(name, val);
      }
    }
    // Shared IDs cover built-ins; custom registrants can still pass path-d or complete
    // <svg>...</svg> markup (custom registrants like FX popups).
    const iconHtml = hasUiIcon(meta.icon) ? uiIcon(meta.icon, 14)
      : (typeof meta.icon === 'string' && meta.icon.includes('<'))
      ? meta.icon
      : `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${meta.icon}"/></svg>`;
    chip.innerHTML = `
      ${iconHtml}
      <span class="minimized-dock-label">${meta.label}</span>
      <span class="minimized-dock-x" title="Close">${uiIcon("close", 12)}</span>
    `;
    chip.addEventListener('click', (e) => {
      if (chip._wasDragging) { chip._wasDragging = false; return; }
      if (e.target.closest('.minimized-dock-x')) {
        e.stopPropagation();
        close(id);
        return;
      }
      // Tap toggles: if the modal is currently minimized, restore it. If
      // it's already open (chip is being kept around because it was free-
      // positioned on mobile), minimize it.
      const s = _state.get(id);
      if (s && !s.isMinimized) {
        minimize(id);
      } else {
        restore(id);
      }
    });
    _wireChipDrag(chip, dock);
    // Visually mark whether the modal is currently open (chip-active) so
    // the user can see at a glance which floating chip belongs to the
    // visible modal.
    const st = _state.get(id);
    if (st && !st.isMinimized) chip.classList.add('chip-active');
    dock.appendChild(chip);
  }

  _applyDockPos(dock);
  _syncDockSpace(dock);

  // FLIP: animate from old → new positions
  dock.querySelectorAll('.minimized-dock-chip').forEach(c => {
    const oldRect = oldRects.get(c.dataset.modalId);
    if (!oldRect) return;
    const newRect = c.getBoundingClientRect();
    const dx = oldRect.left - newRect.left;
    const dy = oldRect.top - newRect.top;
    if (dx || dy) {
      c.style.transform = `translate(${dx}px, ${dy}px)`;
      c.style.transition = 'none';
      requestAnimationFrame(() => {
        c.style.transition = 'transform 0.28s cubic-bezier(0.34, 1.56, 0.64, 1)';
        c.style.transform = '';
      });
    }
  });


}

// Lazy-build the magnetic close target. Horizontally centered; its vertical
// edge is set per-drag in _positionTrashZoneOpposite so the X always lands
// on the side opposite the chip the user is dragging.
function _ensureTrashZone() {
  let z = document.getElementById('dock-trash-zone');
  if (z) return z;
  z = document.createElement('div');
  z.id = 'dock-trash-zone';
  z.innerHTML =
    '<span class="whirlpool"></span>' +
    uiIcon('close', 32);
  document.body.appendChild(z);
  return z;
}

// Trigger the burst animation on the trash zone, then clean up the class so
// the next drag-in shows the normal spinning ring rather than a stuck burst.
function _trashBurst() {
  const z = document.getElementById('dock-trash-zone');
  if (!z) return;
  z.classList.add('dropping');
  setTimeout(() => { z.classList.remove('dropping'); }, 360);
}

// Place the X on the opposite vertical half from the chip so the user always
// has somewhere to drag toward. Locked at drag-start so it doesn't flip while
// the user is mid-gesture.
function _positionTrashZoneOpposite(z, chipTop, chipHeight) {
  const chipMid = chipTop + chipHeight / 2;
  if (chipMid > window.innerHeight / 2) {
    z.style.top = 'max(24px, env(safe-area-inset-top))';
    z.style.bottom = 'auto';
    z.dataset.side = 'top';
  } else {
    z.style.top = 'auto';
    z.style.bottom = 'max(24px, env(safe-area-inset-bottom))';
    z.dataset.side = 'bottom';
  }
}

// ── Drag behavior ──
// • Mobile dock chips → drag the entire dock as a unit; long-press peels one chip out.
// • Mobile free-floating chips → free-drag puck (drag UP to the trash zone to close).
// • Desktop middle chips → reorder within the dock (FLIP magnetic slide)
// • Desktop edge chips (or single chip) → drag the entire dock as a unit
const LONG_PRESS_MS = 380;

function _detachToFreeDrag(chip, dock, chipStartLeft, chipStartTop) {
  if (chip.parentElement === dock) {
    chip.style.setProperty('position', 'fixed', 'important');
    chip.style.setProperty('left', `${chipStartLeft}px`, 'important');
    chip.style.setProperty('top', `${chipStartTop}px`, 'important');
    document.body.appendChild(chip);
  }
  chip.classList.add('chip-free-drag');
}

// ── Chain physics ──
// When the user drags one chip from a multi-chip dock, the other chips
// spring-follow it like a snake. Each follower targets its predecessor's
// position plus a fixed offset (their original spacing), so the chain
// stretches and bunches naturally as the head moves.
function _initChainPhysics(grabbedChip, dock, startX, startY) {
  // Use the canonical _dockOrder so the chain works whether chips are
  // currently parented to the dock or living free at body level.
  const chipEls = _dockOrder
    .map(id => document.querySelector(`.minimized-dock-chip[data-modal-id="${id}"]`))
    .filter(Boolean);
  // Fallback to dock children if _dockOrder is somehow empty.
  const dockChips = chipEls.length >= 2 ? chipEls : [...dock.querySelectorAll('.minimized-dock-chip')];
  const grabbedIdx = dockChips.indexOf(grabbedChip);
  if (grabbedIdx < 0 || dockChips.length < 2) return null;

  const links = dockChips.map((c, i) => {
    const r = c.getBoundingClientRect();
    return {
      chip: c, dockIdx: i,
      width: r.width, height: r.height,
      origX: r.left, origY: r.top,
      x: r.left, y: r.top,
      vx: 0, vy: 0,
      pred: null,
    };
  });
  // Build a single-line chain ordered outward from the head, alternating
  // sides so chips closer to the grabbed one come first. Each link's
  // predecessor is the chip immediately before it in chain order — that
  // makes the whole chain a single tail rather than a Y where two strands
  // dangle from the head.
  const order = [grabbedIdx];
  let lo = grabbedIdx - 1, hi = grabbedIdx + 1;
  while (lo >= 0 || hi < links.length) {
    if (lo >= 0) { order.push(lo); lo--; }
    if (hi < links.length) { order.push(hi); hi++; }
  }
  for (let pos = 1; pos < order.length; pos++) {
    links[order[pos]].pred = links[order[pos - 1]];
  }

  // Detach every chip to body so the dock element can't fight our positioning.
  for (const l of links) {
    l.chip.style.position = 'fixed';
    l.chip.style.left = `${l.x}px`;
    l.chip.style.top = `${l.y}px`;
    l.chip.style.margin = '0';
    l.chip.style.transition = 'none';
    l.chip.style.animation = 'none';
    l.chip.style.zIndex = (l.dockIdx === grabbedIdx) ? '10001' : '10000';
    document.body.appendChild(l.chip);
  }
  // Hide the now-empty dock element itself so it doesn't show a blank pad.
  dock.style.opacity = '0';

  const head = links[grabbedIdx];
  // Use a uniform link spacing — slightly tighter than a chip-width so the
  // chain looks like a connected strand rather than a sparse row.
  const linkSpacing = head.width + 2;
  return {
    links, grabbedIdx, order,
    linkSpacing,
    fingerOffsetX: startX - head.origX,
    fingerOffsetY: startY - head.origY,
    targetX: head.origX, targetY: head.origY,
    // Seed the trail direction with the original layout direction (chips
    // were arranged left-to-right; chain at rest points right).
    trailDirX: 1, trailDirY: 0,
    raf: 0, overTrash: false,
  };
}

function _stepChain(state, trashZone, captureRadius) {
  const HEAD_EASE = 0.85;
  // Followers use critically-damped easing (direct lerp, no velocity) rather
  // than spring + damp — springs oscillate when chasing a moving target, and
  // with a chain of 3+ each link amplifies the wobble.
  const FOLLOWER_EASE = 0.32;
  // Trail direction tracks the head's RECENT movement (smoothed). Reversing
  // direction mid-drag flips the trail; pausing keeps the last orientation.
  // Seeded with the original horizontal layout in _initChainPhysics so the
  // chain has a sensible direction even before the first frame.
  const head = state.links[state.grabbedIdx];
  const hVx = (state.targetX - head.x) * HEAD_EASE;
  const hVy = (state.targetY - head.y) * HEAD_EASE;
  const vMag = Math.hypot(hVx, hVy);
  // Dead zone — micro-velocities from finger drift don't reorient the trail.
  // Only intentional, sustained motion updates direction.
  if (vMag > 2.0) {
    const nx = -hVx / vMag;
    const ny = -hVy / vMag;
    const EASE = 0.12;
    state.trailDirX += (nx - state.trailDirX) * EASE;
    state.trailDirY += (ny - state.trailDirY) * EASE;
  }
  // Renormalize so spacing stays consistent even mid-rotation.
  const tMag = Math.hypot(state.trailDirX, state.trailDirY) || 1;
  const dirX = state.trailDirX / tMag;
  const dirY = state.trailDirY / tMag;
  const spacing = state.linkSpacing;
  for (const i of state.order) {
    const l = state.links[i];
    if (i === state.grabbedIdx) {
      l.x += (state.targetX - l.x) * HEAD_EASE;
      l.y += (state.targetY - l.y) * HEAD_EASE;
    } else {
      const tx = l.pred.x + dirX * spacing;
      const ty = l.pred.y + dirY * spacing;
      l.x += (tx - l.x) * FOLLOWER_EASE;
      l.y += (ty - l.y) * FOLLOWER_EASE;
    }
  }
  for (const l of state.links) {
    l.chip.style.left = `${l.x}px`;
    l.chip.style.top = `${l.y}px`;
  }
  if (trashZone) {
    const head = state.links[state.grabbedIdx];
    const tz = trashZone.getBoundingClientRect();
    const tzcx = tz.left + tz.width / 2;
    const tzcy = tz.top + tz.height / 2;
    const hcx = head.x + head.width / 2;
    const hcy = head.y + head.height / 2;
    const dist = Math.hypot(hcx - tzcx, hcy - tzcy);
    // Trash zone is shown for the whole drag; only .engaged tracks proximity.
    const inZone = dist < captureRadius;
    if (inZone !== state.overTrash) {
      state.overTrash = inZone;
      trashZone.classList.toggle('engaged', inZone);
    }
  }
}

function _wireChipDrag(chip, dock) {
  let startX = 0, startY = 0, dragging = false;
  let dragMode = null; // 'reorder' | 'move-dock' | 'free' | 'chain'
  let dockStartLeft = 0, dockStartTop = 0;
  let chipStartLeft = 0, chipStartTop = 0;
  let trashZone = null;
  let overTrash = false;
  let activePointerId = null;
  let longPressTimer = null;
  let longPressVisual = null;
  let chainState = null;
  let chipSnapZone = null;   // desktop: snap zone under the cursor while dragging a chip
  const CAPTURE_RADIUS = 70;

  const cancelLongPress = () => {
    if (longPressTimer) { clearTimeout(longPressTimer); longPressTimer = null; }
    if (longPressVisual) { clearTimeout(longPressVisual); longPressVisual = null; }
    chip.classList.remove('chip-long-press');
  };

  const onPointerDown = (e) => {
    if (e.target.closest('.minimized-dock-x')) return;
    if (e.button !== 0 && e.pointerType === 'mouse') return;
    if (activePointerId !== null) return;
    startX = e.clientX; startY = e.clientY; dragging = false;
    activePointerId = e.pointerId;
    // Flag global "a chip is being touched" so other touch handlers (e.g.
    // the chat container's edge-swipe-to-open-sidebar) know to stand down.
    // Set on pointerdown rather than waiting for the drag threshold so the
    // chat container's touchstart, which fires roughly simultaneously, sees
    // it during its own decision pass.
    window._chipDragging = true;

    const cr = chip.getBoundingClientRect();
    chipStartLeft = cr.left;
    chipStartTop = cr.top;

    const onTouch = (e.pointerType === 'touch' || window.innerWidth <= 768);
    if (onTouch) {
      const isFree = _chipPositions.has(chip.dataset.modalId);
      trashZone = _ensureTrashZone();
      overTrash = false;
      // Decide drag mode purely by chip count, not by whether this chip is
      // currently dock-resident or free. As long as there are 2+ chips, the
      // chain owns the gesture so the group stays grouped.
      // Count every currently-rendered chip, whether it lives in the dock
      // or free-positioned at body level after a chain drop. Counting only
      // dock children meant a chained-and-released group could no longer
      // re-activate chain mode on the next drag.
      const totalChips =
        dock.querySelectorAll('.minimized-dock-chip').length +
        document.querySelectorAll('body > .minimized-dock-chip').length;
      if (totalChips >= 2) {
        dragMode = 'chain';
        _positionTrashZoneOpposite(trashZone, chipStartTop, chip.offsetHeight);
        // Long-press a chained chip to peel it off as a single free puck —
        // movement before the timer fires cancels and starts chain physics.
        // Defer the visual pulse so quick taps don't see a scale-up bounce.
        longPressVisual = setTimeout(() => {
          longPressVisual = null;
          chip.classList.add('chip-long-press');
        }, 180);
        longPressTimer = setTimeout(() => {
          longPressTimer = null;
          if (dragging) return; // chain already engaged
          dragMode = 'free';
          chip.classList.remove('chip-long-press');
          _detachToFreeDrag(chip, dock, chipStartLeft, chipStartTop);
          _chipPositions.set(chip.dataset.modalId, { left: chipStartLeft, top: chipStartTop });
          _saveDockState();
          chip._wasDragging = true;
          setTimeout(() => { chip._wasDragging = false; }, 350);
          if (navigator.vibrate) { try { navigator.vibrate(15); } catch {} }
        }, LONG_PRESS_MS);
      } else if (isFree) {
        // Lone free chip — single-puck drag.
        dragMode = 'free';
        _positionTrashZoneOpposite(trashZone, chipStartTop, chip.offsetHeight);
      } else {
        // Lone dock chip — keep move-dock so the user can reposition the
        // dock pad (long-press still promotes to free-drag).
        dragMode = 'move-dock';
        const dr = dock.getBoundingClientRect();
        dockStartLeft = dr.left;
        dockStartTop = dr.top;
        _positionTrashZoneOpposite(trashZone, dr.top, dr.height);
        longPressVisual = setTimeout(() => {
          longPressVisual = null;
          chip.classList.add('chip-long-press');
        }, 180);
        longPressTimer = setTimeout(() => {
          longPressTimer = null;
          if (dragging) return; // committed to move-dock already
          dragMode = 'free';
          chip.classList.remove('chip-long-press');
          _detachToFreeDrag(chip, dock, chipStartLeft, chipStartTop);
          _positionTrashZoneOpposite(trashZone, chipStartTop, chip.offsetHeight);
          // Stick the chip where it was so it stays detached even if the user
          // lifts their finger without dragging.
          _chipPositions.set(chip.dataset.modalId, { left: chipStartLeft, top: chipStartTop });
          _saveDockState();
          // Suppress the trailing click so this hold doesn't immediately
          // restore/minimize the modal under the finger.
          chip._wasDragging = true;
          setTimeout(() => { chip._wasDragging = false; }, 350);
          if (navigator.vibrate) { try { navigator.vibrate(15); } catch {} }
        }, LONG_PRESS_MS);
      }
      document.addEventListener('pointermove', onPointerMove);
      document.addEventListener('pointerup', onPointerUp, { once: true });
      document.addEventListener('pointercancel', onPointerUp, { once: true });
      return;
    }

    // Desktop — reorder vs move-dock
    const chips = [...dock.querySelectorAll('.minimized-dock-chip')];
    const idx = chips.indexOf(chip);
    const isEdge = idx === 0 || idx === chips.length - 1;
    dragMode = (isEdge && chips.length >= 2) ? 'move-dock' : (chips.length >= 2 ? 'reorder' : 'move-dock');
    if (chips.length === 1) dragMode = 'move-dock';
    if (dragMode === 'move-dock') {
      const dr = dock.getBoundingClientRect();
      dockStartLeft = dr.left;
      dockStartTop = dr.top;
    }
    chip.setPointerCapture(e.pointerId);
    chip.addEventListener('pointermove', onPointerMove);
    chip.addEventListener('pointerup', onPointerUp, { once: true });
    chip.addEventListener('pointercancel', onPointerUp, { once: true });
  };

  const onPointerMove = (e) => {
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    // Touch fingers drift a few pixels even on a "still" tap, so the touch
    // threshold is generous — otherwise a tap-to-restore reads as a drag
    // and the click gets eaten when the chain settles.
    const DRAG_THRESHOLD = (e.pointerType === 'touch' || window.innerWidth <= 768) ? 14 : 5;
    if (!dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (!dragging) {
      dragging = true;
      cancelLongPress();
      // Reveal the trash X as soon as a drag begins so the user always
      // sees the close target, regardless of distance. The .engaged
      // state still tracks proximity to the zone center.
      if (trashZone) trashZone.classList.add('visible');
      if (dragMode === 'reorder') {
        chip.classList.add('dragging');
      } else if (dragMode === 'free') {
        chip.classList.add('chip-free-drag');
      } else if (dragMode === 'chain') {
        chainState = _initChainPhysics(chip, dock, startX, startY);
        if (chainState) {
          const stepLoop = () => {
            if (!chainState) return;
            _stepChain(chainState, trashZone, CAPTURE_RADIUS);
            overTrash = chainState.overTrash;
            chainState.raf = requestAnimationFrame(stepLoop);
          };
          chainState.raf = requestAnimationFrame(stepLoop);
        } else {
          // Init failed for some reason — fall back to move-dock so the
          // user's gesture isn't dropped on the floor.
          dragMode = 'move-dock';
          dock.classList.add('dock-dragging');
        }
      } else {
        dock.classList.add('dock-dragging');
      }
    }

    // Desktop: dragging a chip into a screen snap zone previews restoring the
    // window + snapping it there (top → maximize/fullscreen, right → right
    // dock). Releasing in the zone commits it (see onPointerUp).
    if (e.pointerType !== 'touch' && window.innerWidth > 768) {
      const z = previewZoneAt(e.clientX, e.clientY, document.getElementById(chip.dataset.modalId));
      // Ignore the bottom zone — the dock lives at the bottom, so horizontal
      // chip reordering must not get hijacked into a bottom-half snap.
      chipSnapZone = (z && z.name !== 'bottom-half') ? z : null;
      if (z && !chipSnapZone) clearPreview();
      if (chipSnapZone) {
        chip.style.opacity = '0.35';
        return;  // aiming at a snap zone — suppress reorder/move-dock
      }
      chip.style.opacity = '';
    }

    if (dragMode === 'chain' && chainState) {
      // Pointermove just updates the head's target — the RAF loop drives the
      // actual position update for both head and followers.
      chainState.targetX = e.clientX - chainState.fingerOffsetX;
      chainState.targetY = e.clientY - chainState.fingerOffsetY;
      e.preventDefault && e.preventDefault();
      return;
    }

    if (dragMode === 'free') {
      const tz = trashZone.getBoundingClientRect();
      const tzcx = tz.left + tz.width / 2;
      const tzcy = tz.top + tz.height / 2;
      const dist = Math.hypot(e.clientX - tzcx, e.clientY - tzcy);
      const inZone = dist < CAPTURE_RADIUS;
      // Trash X stays visible for the entire drag; only .engaged tracks
      // when the chip is close enough to capture.
      let tx = e.clientX - (chipStartLeft + chip.offsetWidth / 2);
      let ty = e.clientY - (chipStartTop + chip.offsetHeight / 2);
      if (inZone) {
        const pull = 1 - (dist / CAPTURE_RADIUS);
        const sx = tzcx - (chipStartLeft + chip.offsetWidth / 2);
        const sy = tzcy - (chipStartTop + chip.offsetHeight / 2);
        tx = tx * (1 - pull) + sx * pull;
        ty = ty * (1 - pull) + sy * pull;
      }
      // !important needed because the chip's class-level transform/transition
      // (the FLIP reorder animation + spring transition) outranks plain
      // inline styles set via .style on some Safari versions.
      chip.style.setProperty('transition', 'none', 'important');
      chip.style.setProperty('transform', `translate(${tx}px, ${ty}px) scale(${inZone ? 1.12 : 1.05})`, 'important');
      chip.style.setProperty('z-index', '10030', 'important');
      chip.style.setProperty('position', 'fixed', 'important');
      chip.style.setProperty('left', `${chipStartLeft}px`, 'important');
      chip.style.setProperty('top', `${chipStartTop}px`, 'important');
      chip.style.setProperty('pointer-events', 'none', 'important');
      if (inZone !== overTrash) {
        overTrash = inZone;
        trashZone.classList.toggle('engaged', overTrash);
      }
      e.preventDefault && e.preventDefault();
      return;
    }

    if (dragMode === 'reorder') {
      chip.style.transition = 'none';
      chip.style.transform = `translate(${dx}px, ${dy}px) scale(1.05)`;
      chip.style.zIndex = '10030';

      // Find sibling under cursor and swap
      const siblings = [...dock.querySelectorAll('.minimized-dock-chip:not(.dragging)')];
      for (const sib of siblings) {
        const r = sib.getBoundingClientRect();
        if (e.clientX >= r.left && e.clientX <= r.right) {
          const myIdx = _dockOrder.indexOf(chip.dataset.modalId);
          const sibIdx = _dockOrder.indexOf(sib.dataset.modalId);
          if (myIdx !== sibIdx) {
            _dockOrder.splice(myIdx, 1);
            _dockOrder.splice(sibIdx, 0, chip.dataset.modalId);
            chip.classList.remove('dragging');
            _renderDock();
            return;
          }
        }
      }
    } else {
      // Move-dock: reposition the entire dock element. On touch, the whole
      // chain also interacts with the trash zone — drop on X to close every
      // chip in the dock.
      let newLeft = Math.max(8, Math.min(window.innerWidth  - dock.offsetWidth  - 8, dockStartLeft + dx));
      let newTop  = Math.max(8, Math.min(window.innerHeight - dock.offsetHeight - 8, dockStartTop  + dy));

      if (trashZone) {
        const tz = trashZone.getBoundingClientRect();
        const tzcx = tz.left + tz.width / 2;
        const tzcy = tz.top + tz.height / 2;
        const dockCx = newLeft + dock.offsetWidth / 2;
        const dockCy = newTop + dock.offsetHeight / 2;
        const dist = Math.hypot(dockCx - tzcx, dockCy - tzcy);
        const inZone = dist < CAPTURE_RADIUS;
        // Trash X stays visible for the entire drag — only .engaged
        // tracks proximity to the capture point.
        if (inZone) {
          const pull = 1 - (dist / CAPTURE_RADIUS);
          newLeft = newLeft + (tzcx - dockCx) * pull;
          newTop  = newTop  + (tzcy - dockCy) * pull;
        }
        if (inZone !== overTrash) {
          overTrash = inZone;
          trashZone.classList.toggle('engaged', overTrash);
        }
      }

      dock.style.left = `${newLeft}px`;
      dock.style.top  = `${newTop}px`;
      dock.style.right = 'auto';
      dock.style.bottom = 'auto';
      dock.style.transform = 'none';
      _dockPos = { left: newLeft, top: newTop };
      _saveDockState();
    }
  };

  const onPointerUp = () => {
    const trashDrop = dragging && overTrash;
    document.removeEventListener('pointermove', onPointerMove);
    chip.removeEventListener('pointermove', onPointerMove);
    activePointerId = null;
    cancelLongPress();
    // Clear the global drag flag so the chat container's edge-swipe handler
    // can resume opening the sidebar on plain swipes.
    setTimeout(() => { window._chipDragging = false; }, 0);
    // Desktop snap-on-drop: released over a snap zone → restore the window and
    // snap it there (instead of the normal chip reorder/dock drop).
    if (chipSnapZone) {
      const zone = chipSnapZone; chipSnapZone = null;
      clearPreview();
      chip.style.opacity = '';
      dock.classList.remove('dock-dragging');
      const id = chip.dataset.modalId;
      restore(id);
      const modal = document.getElementById(id);
      if (modal) requestAnimationFrame(() => requestAnimationFrame(() => snapModalToZone(modal, zone)));
      return;
    }
    if (dragMode === 'chain' && chainState) {
      cancelAnimationFrame(chainState.raf);
      const state = chainState;
      chainState = null;
      if (state.overTrash && dragging) {
        // Drop on X: animate every link toward the trash zone, then close.
        const ids = state.links.map(l => l.chip.dataset.modalId);
        const tz = trashZone ? trashZone.getBoundingClientRect() : null;
        for (const l of state.links) {
          if (tz) {
            const dx = (tz.left + tz.width / 2) - (l.x + l.width / 2);
            const dy = (tz.top + tz.height / 2) - (l.y + l.height / 2);
            l.chip.classList.add('chip-trashing');
            l.chip.style.transition = 'transform 0.32s cubic-bezier(0.45, 0, 0.25, 1), opacity 0.3s ease-in, left 0.32s cubic-bezier(0.45, 0, 0.25, 1), top 0.32s cubic-bezier(0.45, 0, 0.25, 1)';
            // Whirlpool: spin + shrink so the chip swirls into the X.
            l.chip.style.transform = 'scale(0.15) rotate(720deg)';
            l.chip.style.left = `${l.x + dx}px`;
            l.chip.style.top = `${l.y + dy}px`;
          }
          l.chip.style.opacity = '0';
        }
        _trashBurst();
        setTimeout(() => {
          for (const id of ids) close(id);
          dock.style.cssText = '';
          _renderDock();
          _saveDockState();
        }, 320);
      } else if (dragging) {
        // The gesture may roam freely; the settled owner is always the row.
        dock.style.opacity = '';
        _saveDockState();
        _renderDock();
      } else {
        // Touch without movement: nothing to do (RAF never started, dock
        // never modified). Let the click handler restore as normal.
      }
      if (trashZone) trashZone.classList.remove('visible', 'engaged');
      overTrash = false;
      dock.classList.remove('dock-dragging');
      dragging = false;
      dragMode = null;
      return;
    }
    if (dragMode === 'free') {
      // If the user never actually dragged (just tapped), do nothing here —
      // let the click handler restore the modal as normal. Skipping past
      // the drop logic also avoids saving the chip's position and re-
      // rendering, which was destroying the click target before it fired.
      if (!dragging) {
        const detached = _chipPositions.size > 0;
        dragging = false;
        dragMode = null;
        if (trashZone) trashZone.classList.remove('visible', 'engaged');
        if (detached) _renderDock();
        return;
      }
      if (overTrash) {
        // Animate into the X — the inline transforms set during drag have
        // `!important`, so the close animation needs setProperty(...important)
        // too or the styles don't apply and the chip just snaps.
        const cur = chip.style.transform || 'translate(0,0)';
        chip.classList.add('chip-trashing');
        chip.style.setProperty('transition', 'transform 0.32s cubic-bezier(0.45, 0, 0.25, 1), opacity 0.3s ease-in', 'important');
        // Whirlpool: spin + shrink as the chip swirls into the X.
        chip.style.setProperty('transform', `${cur} scale(0.15) rotate(720deg)`, 'important');
        chip.style.setProperty('opacity', '0', 'important');
        _trashBurst();
        const id = chip.dataset.modalId;
        setTimeout(() => { close(id); _renderDock(); }, 320);
      } else {
        _saveDockState();
        _renderDock();
      }
      if (trashZone) trashZone.classList.remove('visible', 'engaged');
      overTrash = false;
    } else if (dragMode === 'move-dock' && dragging && overTrash) {
      // Dropped the whole chain onto the X — close every chipped modal.
      // Only the ids that actually have a rendered chip (i.e. currently
      // minimized or free-positioned); restored open modals are skipped.
      const ids = [...dock.querySelectorAll('.minimized-dock-chip')]
        .map(c => c.dataset.modalId)
        .filter(Boolean);
      _trashBurst();
      // Whirlpool: spin + shrink the whole dock as it spirals into the X.
      dock.style.transition = 'transform 0.32s cubic-bezier(0.45, 0, 0.25, 1), opacity 0.3s ease-in';
      dock.style.opacity = '0';
      dock.style.transform = 'scale(0.2) rotate(720deg)';
      setTimeout(() => {
        for (const id of ids) close(id);
        // The animation left the dock at opacity:0 / scale(0.2) rotated and
        // the drag left it pinned wherever the user landed. Scrub all of
        // that so the NEXT minimized modal renders into a visible, default-
        // positioned dock instead of an invisible one stuck near the trash.
        dock.style.removeProperty('opacity');
        dock.style.removeProperty('transform');
        dock.style.removeProperty('transition');
        dock.style.removeProperty('left');
        dock.style.removeProperty('top');
        dock.style.removeProperty('right');
        dock.style.removeProperty('bottom');
        _renderDock();
        _saveDockState();
      }, 320);
      if (trashZone) trashZone.classList.remove('visible', 'engaged');
      overTrash = false;
      dock.classList.remove('dock-dragging');
    } else if (dragMode === 'reorder') {
      chip.classList.remove('dragging');
      chip.style.transition = 'transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)';
      chip.style.transform = '';
      chip.style.zIndex = '';
    } else {
      dock.classList.remove('dock-dragging');
      if (trashZone) trashZone.classList.remove('visible', 'engaged');
      overTrash = false;
    }
    if (dragging) {
      chip._wasDragging = true;
      setTimeout(() => { chip._wasDragging = false; }, 50);
    }
    if (dragging && !trashDrop) {
      _applyDockPos(dock);
      _syncDockSpace(dock);
      _saveDockState();
    }
    dragging = false;
    dragMode = null;
  };
  chip.addEventListener('pointerdown', onPointerDown);
}

// Tracks which _LABELS entries were created by `register(..., {label, icon})`
// (vs. the built-in static ones). Only these should be removed in
// `unregister` — built-in labels stay for the lifetime of the page.
const _customLabelIds = new Set();

export function register(id, { restoreFn, closeFn, railBtnId, sidebarBtnId, label, icon } = {}) {
  // railBtnId can be a single id or an array; we accept both rail and sidebar separately too.
  const btnIds = [];
  if (railBtnId) btnIds.push(...(Array.isArray(railBtnId) ? railBtnId : [railBtnId]));
  if (sidebarBtnId) btnIds.push(...(Array.isArray(sidebarBtnId) ? sidebarBtnId : [sidebarBtnId]));
  _state.set(id, {
    restoreFn: restoreFn || (() => {}),
    closeFn:   closeFn   || (() => {}),
    btnIds,
    isMinimized: false,
    restoreMinHeight: '',
  });
  // Auto-stack: whichever modal becomes visible last sits on top of any
  // already-open modals. The various tool open() functions (calendar,
  // memory/brain, tasks, etc.) all just toggle `.hidden` or `display` —
  // observe both and bump the z-index on the visible→hidden→visible
  // transition. Idempotent on re-register.
  const _modalEl = document.getElementById(id);
  if (_modalEl) markAppletFrame(_modalEl, _modalEl.querySelector(':scope > .modal-content, :scope > .research-pane') || (_modalEl.matches('.notes-pane') ? _modalEl : null));
  if (_modalEl && !_modalEl._mmAutoStackObs) {
    const _isVisible = () => !_modalEl.classList.contains('hidden')
        && getComputedStyle(_modalEl).display !== 'none';
    _modalEl._mmAutoStackLast = _isVisible();
    const obs = new MutationObserver(() => {
      const vis = _isVisible();
      if (vis && !_modalEl._mmAutoStackLast) {
        _bringToFront(_modalEl);
        _applyRememberedDock(id);
        _emitModalOpened(id, _modalEl);
      }
      _modalEl._mmAutoStackLast = vis;
    });
    obs.observe(_modalEl, { attributes: true, attributeFilter: ['class', 'style'] });
    _modalEl._mmAutoStackObs = obs;
    // If it's already visible at register time (e.g. modal opened before
    // register completes), bump it once now too.
    if (_modalEl._mmAutoStackLast) {
      _bringToFront(_modalEl);
      _applyRememberedDock(id);
      _emitModalOpened(id, _modalEl);
    }
  }
  // Allow callers to supply their own chip label/icon (semantic ID, path d="..." or
  // full <svg>...</svg>) so ephemeral things like FX popups can dock
  // into the same chain without needing an entry in the built-in
  // _LABELS table. Track the id so `unregister` can drop the entry
  // and avoid an unbounded-growth leak (v2 review HIGH-3).
  if (label || icon) {
    _LABELS[id] = { label: label || id, icon: icon || '' };
    _customLabelIds.add(id);
  }
  // If a docked window was minimized and its chip was closed, reopen the
  // window in the same side dock next time. Defer until the caller finishes
  // removing `.hidden` / applying initial display styles.
  if (_getRememberedDock(id)) {
    requestAnimationFrame(() => requestAnimationFrame(() => _applyRememberedDock(id)));
  }
}

export function unregister(id) {
  const s = _state.get(id);
  _emitModalClosed(id, document.getElementById(id));
  if (s) _setBadge(s.btnIds, false);
  _state.delete(id);
  _chipPositions.delete(id);
  // Drop any per-popup _LABELS entry created at register-time.
  if (_customLabelIds.has(id)) {
    delete _LABELS[id];
    _customLabelIds.delete(id);
  }
  // Also prune the dock-order list so a re-rendered dock doesn't try
  // to draw a chip for a now-dead id.
  const idx = _dockOrder.indexOf(id);
  if (idx >= 0) _dockOrder.splice(idx, 1);
  _saveDockState();
  _renderDock();
}

export function isRegistered(id)  { return _state.has(id); }
export function isMinimized(id)   { return _state.get(id)?.isMinimized === true; }

export function minimize(id) {
  // Lazy-register if a known modal isn't yet registered (e.g. user clicked `_`
  // on a tool that doesn't pre-register itself).
  if (!_state.has(id) && _AUTO_WIRE[id]) _autoRegister(id);
  const s = _state.get(id);
  if (!s) return false;
  // The id may refer to a virtual tool (e.g. the document panel) that has no
  // actual modal element — in that case we just track the minimized state
  // and let the chip drive restore/close via the registered functions.
  const modal = document.getElementById(id);
  if (modal) {
    _captureRestoreHeight(modal, s);
    // If this window is edge-docked (right/left), SUSPEND the dock: release
    // the body push so the chat returns to full width while the window is
    // minimized, but keep the dock so restoring the chip snaps it back in.
    if (modal.classList.contains('modal-right-docked')
        || modal.classList.contains('modal-left-docked')
        || modal.classList.contains('email-snap-left')) {
      try { suspendDock(modal); } catch (e) { console.warn('suspendDock on minimize failed', e); }
    }
    modal.classList.add('hidden');
    modal.classList.add('modal-minimized');
    const content = modal.querySelector('.modal-content');
    if (content) {
      content.classList.remove('sheet-ready', 'modal-closing');
      content.style.transform = '';
      content.style.transition = '';
      content.style.animation = '';
    }
  }
  s.isMinimized = true;
  _emitModalLifecycle('minimized', id, modal);
  _setBadge(s.btnIds, true);
  _ensureDock();
  _renderDock();
  return true;
}

export function restore(id) {
  const s = _state.get(id);
  if (!s) return false;
  const modal = document.getElementById(id);
  if (modal) {
    modal.classList.remove('hidden', 'modal-minimized');
    modal.style.display = '';
    _applyRestoreHeight(modal, s);
    // Surface above any already-open tool window — restoring from the dock
    // should bring this tool to the front, not leave it stuck behind one with
    // a higher static z-index.
    _bringToFront(modal);
    // If the window was edge-docked when minimized, re-apply the dock so the
    // chat nudges back in and the window returns exactly where it was.
    try { resumeDock(modal); } catch (e) { console.warn('resumeDock on restore failed', e); }
    _emitModalOpened(id, modal);
  }
  s.isMinimized = false;
  _emitModalLifecycle('restored', id, modal);
  _setBadge(s.btnIds, false);
  _renderDock();
  try { s.restoreFn(); } catch (e) { console.error('restoreFn:', e); }
  return true;
}

/**
 * If the modal is currently MINIMIZED, restore it and return true.
 * Otherwise return false so the caller falls through to its own
 * open/close handling. We deliberately do NOT minimize on toggle —
 * that's the `_` button's job, not the rail/sidebar button's job.
 */
export function toggle(id) {
  const s = _state.get(id);
  if (!s) return false;
  const modal = document.getElementById(id);
  if (!modal) { _state.delete(id); return false; }
  if (s.isMinimized) return restore(id);
  return false;
}

/** Full close — honors a synchronous or asynchronous close veto before cleanup. */
export function close(id) {
  const s = _state.get(id);
  if (!s) return false;
  const modalBeforeClose = document.getElementById(id);
  const contentBeforeClose = modalBeforeClose?.querySelector?.('.modal-content');
  const suspendedDockSide = contentBeforeClose?._dockSuspended
    || (modalBeforeClose?.classList?.contains('modal-left-docked') ? 'left'
        : modalBeforeClose?.classList?.contains('modal-right-docked') ? 'right'
          : null);
  const shouldRememberDock = s.isMinimized && !!suspendedDockSide;
  const finish = () => {
    // A close callback is allowed to unregister itself. Do not tear down a
    // newer registration that appeared while an async decision was pending.
    if (_state.get(id) !== s) return true;
    if (shouldRememberDock) _rememberDock(id, suspendedDockSide);
    else _forgetDock(id);
    // Some tools animate their close before adding .hidden. Force the modal
    // into a fully-closed state only after its close gate has allowed it.
    const modal = document.getElementById(id);
    _emitModalClosed(id, modal);
    if (modal) {
      if (modal.classList.contains('modal-right-docked') || modal.classList.contains('modal-left-docked')) {
        try { clearRightDock(modal); } catch (e) { console.warn('clearRightDock on close failed', e); }
      }
      modal.classList.add('hidden');
      modal.classList.remove('modal-minimized');
      const content = modal.querySelector('.modal-content');
      if (content) {
        content.classList.remove('modal-closing', 'sheet-ready');
        content.style.transform = '';
        content.style.transition = '';
        content.style.animation = '';
        content.style.opacity = '';
      }
    }
    _setBadge(s.btnIds, false);
    _state.delete(id);
    _chipPositions.delete(id);
    _saveDockState();
    _renderDock();
    return true;
  };

  let decision;
  try { decision = s.closeFn(); }
  catch (e) {
    console.error('closeFn:', e);
    return false;
  }
  if (decision && typeof decision.then === 'function') {
    return Promise.resolve(decision).then((allowed) => {
      if (allowed === false) return false;
      return finish();
    }).catch((e) => {
      console.error('closeFn:', e);
      return false;
    });
  }
  if (decision === false) return false;
  return finish();
}

/** Inject a minimize (`_`) button next to the close button in a modal.
 * Skips if a minimize button already exists (any class containing "minimize"). */
export function injectMinimizeButton(modal, modalId) {
  markAppletFrame(modal, modal.querySelector(':scope > .modal-content, :scope > .research-pane') || (modal.matches('.notes-pane') ? modal : null));
  const header = modal.querySelector('.modal-header');
  if (!header) return;
  if (header.querySelector('.modal-minimize-btn, .minimize-btn, [data-minimize]')) {
    // An existing minimize button is present — wire it to the manager instead
    const existing = header.querySelector('.minimize-btn, [data-minimize]');
    if (existing && !existing.dataset._modalsBound) {
      existing.dataset._modalsBound = '1';
      existing.addEventListener('click', (e) => {
        e.stopPropagation();
        minimize(modalId);
      }, true);
    }
    return;
  }
  const closeBtn = header.querySelector('.close-btn, .modal-close');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'modal-minimize-btn';
  btn.title = 'Minimize';
  btn.setAttribute('aria-label', `Minimize ${modal.getAttribute('aria-label') || _LABELS[modalId]?.label || 'window'}`);
  btn.innerHTML = uiIcon('remove', 14);
  // Anchor the _/X pair to the right edge regardless of the header's
  // justify-content. Some headers (cookbook) use `space-between`, which
  // would otherwise distribute three children as left/center/right and
  // strand the `_` in the middle. `margin-left:auto` eats the free space
  // to the left so `_` + close sit snug at the right.
  btn.style.flexShrink = '0';
  btn.style.marginLeft = 'auto';
  if (closeBtn) {
    // The close button may carry its own left margin (e.g. compare's inline
    // "margin-left:8px") meant to separate it from the title when it stood
    // alone. Now that `_` sits to its left, that margin becomes a stray gap
    // between the two buttons — zero it. The minimize button's own
    // margin-right (2px, from .modal-minimize-btn) provides the gap.
    closeBtn.style.marginLeft = '0';
    closeBtn.style.flexShrink = '0';
  }
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    minimize(modalId);
  });
  if (closeBtn && closeBtn.parentNode) closeBtn.parentNode.insertBefore(btn, closeBtn);
  else header.appendChild(btn);
}

// ── Auto-wire fallback for modals not explicitly registered ──
// Maps modal-id → { rail btn id, sidebar btn id }. Used to auto-register any
// modal that gets swipe-dismissed so the rail/sidebar shows the badge and
// clicking the same button restores it. Tools that need rebuild-on-restore
// can still register explicitly with custom restoreFn/closeFn.
const _AUTO_WIRE = {
  'cookbook-modal':       { rail: 'rail-cookbook',  sidebar: 'tool-cookbook-btn' },
  'calendar-modal':       { rail: 'rail-calendar',  sidebar: 'tool-calendar-btn' },
  'tasks-modal':          { rail: 'rail-tasks',     sidebar: 'tool-tasks-btn' },
  'memory-modal':         { rail: null,             sidebar: 'tool-memory-btn' },
  'notes-panel':          { rail: 'rail-notes',     sidebar: 'tool-notes-btn' },
  // Email already has its own #email-unread-dot inline next to the title —
  // don't add a second modalManager badge that lands at the right edge.
  'email-lib-modal':      { rail: null,             sidebar: null },
  'research-overlay':     { rail: 'rail-research',  sidebar: 'tool-research-btn' },
  'theme-modal':          { rail: null,             sidebar: 'tool-theme-btn' },
  'settings-modal':       { rail: null,             sidebar: 'tool-settings-btn' },
  'compare-model-overlay':{ rail: 'rail-compare',   sidebar: 'tool-compare-btn' },
  'ge-shortcuts-modal':   { rail: null,             sidebar: null },
  // Prompt window opens from the overflow menu (no rail/sidebar button), but
  // wiring it here makes tab-down use the new .minimized-dock-chip instead of
  // the legacy .modal-dock-item.
  'custom-preset-modal':  { rail: null,             sidebar: null },
};

function _autoRegister(id) {
  if (_state.has(id)) return _state.get(id);
  const wire = _AUTO_WIRE[id];
  if (!wire) return null;
  // Default close: try to invoke the tool's own close button (so it tears down
  // properly), then hide as a fallback.
  register(id, {
    railBtnId: wire.rail,
    sidebarBtnId: wire.sidebar,
    closeFn: () => {
      const m = document.getElementById(id);
      if (!m) return;
      const closeBtn = m.querySelector('.close-btn, .modal-close, [data-close]');
      if (closeBtn) {
        closeBtn.click();
      } else {
        m.classList.add('hidden');
        m.style.display = 'none';
      }
    },
    restoreFn: () => {},
  });
  return _state.get(id);
}

// Watch the document for tool modals being added/shown and inject the `_`
// button next to the close button. We do NOT pre-register here — only inject
// the button. Registration happens when the modal is actually minimized,
// either via the `_` button click or via swipe-dismiss.
function _scanAndWire() {
  for (const id of Object.keys(_AUTO_WIRE)) {
    const modal = document.getElementById(id);
    if (!modal) continue;
    injectMinimizeButton(modal, id);
  }
}
const _scanTimer = typeof document !== 'undefined' ? setInterval(_scanAndWire, 1000) : null;
// First scan after DOM ready. Keep the module importable by non-DOM
// renderer tests; the timer and observers are browser-only wiring.
if (typeof document !== 'undefined' && document.readyState !== 'loading') {
  setTimeout(_scanAndWire, 100);
} else if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => setTimeout(_scanAndWire, 100));
}

function _clearEmailSplitAfterMinimize() {
  document.body.classList.remove('email-doc-split-active', 'email-front');
  document.documentElement.style.removeProperty('--email-doc-split-left-x');
  document.documentElement.style.removeProperty('--email-doc-split-email-w');
  document.documentElement.style.removeProperty('--email-doc-split-right-x');
  const docPane = document.getElementById('doc-editor-pane');
  if (docPane) {
    [
      'position', 'left', 'right', 'top', 'bottom', 'width', 'max-width',
      'height', 'z-index', 'transform',
    ].forEach(prop => docPane.style.removeProperty(prop));
  }
  const divider = document.getElementById('doc-divider');
  if (divider) divider.style.display = '';
  requestAnimationFrame(() => window.dispatchEvent(new Event('resize')));
  setTimeout(() => window.dispatchEvent(new Event('resize')), 80);
}

// A registered applet keeps its live state in the same dock as the explicit
// Minimize control. Blocking dialogs keep their normal dismiss/close gate.
export function minimizeOnSwipe(id) {
  const modal = document.getElementById(id);
  if (!modal || modal.getAttribute('aria-modal') === 'true'
      || modal.__openClankWindow?.inputContext?.blockingModal
      || modal.__copalWindow?.inputContext?.blockingModal) return false;
  if (!_state.has(id)) _autoRegister(id);
  if (!_state.has(id)) return false;
  if (id === 'cookbook-modal') {
    document.querySelectorAll(
      '.cookbook-task-dropdown, .cookbook-gpu-split-menu, .hwfit-cached-dropdown, .cookbook-saved-menu, .cookbook-dep-menu'
    ).forEach(dismissOrRemove);
  }
  if (id === 'email-lib-modal' || id.startsWith('email-reader-')) _clearEmailSplitAfterMinimize();
  return minimize(id);
}

// Compatibility for callers that already emitted the legacy dismiss event.
if (typeof window !== 'undefined') window.addEventListener('modal-dismissed', (e) => {
  const id = e.detail?.id;
  if (!id || !minimizeOnSwipe(id)) return;
  // Stop legacy listeners that reset internal `_open` state
  e.stopImmediatePropagation();
});

// Capture-phase intercept: if user clicks a sidebar/rail button whose
// associated modal is currently MINIMIZED, restore it and stop the click
// before the tool's own toggle handler runs (which would try to re-open or
// close it).
if (typeof document !== 'undefined') document.addEventListener('click', (e) => {
  const btn = e.target.closest('[id]');
  if (!btn) return;
  const btnId = btn.id;
  for (const [modalId, s] of _state.entries()) {
    if (!s.isMinimized) continue;
    if (s.btnIds.includes(btnId)) {
      restore(modalId);
      e.stopImmediatePropagation();
      e.preventDefault();
      return;
    }
  }
}, true);

export default { register, unregister, isRegistered, isMinimized, minimize, minimizeOnSwipe, restore, toggle, close, injectMinimizeButton };
