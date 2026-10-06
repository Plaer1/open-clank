// Shared window-resize helper. Companion to makeWindowDraggable: gives every
// draggable tool window (Library, Notes, Tasks, Calendar, Gallery, Email,
// Cookbook, Memory, Settings, Theme, Compare, Research, Sessions) edge- and
// corner-resize, the same way a native desktop window resizes — grab any of
// the four edges or four corners and drag.
//
// Why edge-proximity detection instead of injected handle elements:
//   The windows differ structurally. `.modal-content` scrolls its own body
//   (overflow:auto) while `.notes-pane` keeps overflow:hidden and scrolls an
//   inner element. Absolutely-positioned handle children would scroll away
//   with the content in the first case. Detecting pointer proximity to the
//   window's border works uniformly regardless of the overflow model and
//   matches the user's mental model ("drag the edges or corners").
//
// API:
//   makeWindowResizable(content, {
//     modal,        // optional wrapping .modal (for id-based size persistence)
//     mobileSkip,   // viewport width at/below which resize is disabled (sheets)
//     isLocked,     // () => bool — skip while fullscreen / docked
//     minWidth, minHeight,
//     storageKey,   // localStorage key to persist {w,h}; null disables
//     onResizeEnd,  // ({rect}) => void
//   })

const EDGE = 7;          // px proximity to a border that arms a resize grip
const MIN_W = 320;       // smallest a window may be dragged to
const MIN_H = 200;
export const WINDOW_SIZE_VERSION = 2;
// Controls that must keep their own click/drag behaviour even when they sit
// within EDGE px of the window border (close buttons, sliders, inputs, links).
const INTERACTIVE = 'button, input, select, textarea, a, [contenteditable=""], [contenteditable="true"]';

export function normalizeWindowSizeRecord(value, viewport = {}) {
  if (!value || typeof value !== 'object') return null;
  const rawWidth = Number(value.width ?? value.w);
  const rawHeight = Number(value.height ?? value.h);
  const viewportWidth = Number(viewport.width);
  const viewportHeight = Number(viewport.height);
  if (![rawWidth, rawHeight, viewportWidth, viewportHeight].every(Number.isFinite)
      || rawWidth <= 0 || rawHeight <= 0 || viewportWidth <= 0 || viewportHeight <= 0) return null;
  const requestedMinWidth = Number.isFinite(Number(viewport.minWidth)) ? Number(viewport.minWidth) : MIN_W;
  const requestedMinHeight = Number.isFinite(Number(viewport.minHeight)) ? Number(viewport.minHeight) : MIN_H;
  const minimumWidth = Math.min(viewportWidth, Math.max(1, requestedMinWidth));
  const minimumHeight = Math.min(viewportHeight, Math.max(1, requestedMinHeight));
  return {
    version:WINDOW_SIZE_VERSION,
    width:Math.round(Math.min(viewportWidth, Math.max(minimumWidth, rawWidth))),
    height:Math.round(Math.min(viewportHeight, Math.max(minimumHeight, rawHeight))),
  };
}

// Desktop applet geometry shares the measured navigation boundary. Mobile
// navigation is an overlay drawer, so sheets retain the full viewport.
export function windowViewportBounds() {
  const width = window.innerWidth, height = window.innerHeight;
  let left = 0, right = width;
  const desktop = width > 768;
  if (desktop) {
    for (const nav of [document.getElementById('sidebar'), document.getElementById('icon-rail')]) {
      if (!nav || nav.classList.contains('hidden') || nav.classList.contains('rail-hidden')) continue;
      const style = getComputedStyle(nav), rect = nav.getBoundingClientRect();
      if (style.display === 'none' || style.visibility === 'hidden' || rect.width <= 0) continue;
      if (nav.classList.contains('right-side')) right = Math.min(right, rect.left);
      else left = Math.max(left, rect.right);
    }
    left = Math.max(0, Math.min(left, width - 9));
    right = Math.max(left + 9, Math.min(width, right));
  }
  const margin = desktop ? Math.min(4, (right - left - 1) / 2, (height - 1) / 2) : 0;
  left += margin; right -= margin;
  const top = margin, bottom = height - margin;
  return { left, top, right, bottom, width:right - left, height:bottom - top };
}

// The accessibility enhancer also sets aria-modal on ordinary legacy content.
// Lifecycle blocking belongs to the outer root/context; real confirmations and
// native dialogs remain excluded even after that generic enhancement.
export function isBlockingAppletFrame(content) {
  if (!content) return true;
  if (content.closest('dialog, .styled-confirm-box, .styled-prompt-box, .copal-modal-overlay, [role="alertdialog"]')) return true;
  const modal = content.closest('.modal, .research-overlay, .notes-pane');
  if (!modal) return true;
  if (modal.__openClankWindow?.inputContext?.blockingModal || modal.__copalWindow?.inputContext?.blockingModal) return true;
  for (let node = content; node; node = node.parentElement) {
    if (node.getAttribute('aria-modal') !== 'true') continue;
    // a11y.js marks only modal-content/Notes, never an outer .modal root.
    if (node === content && node.dataset.a11yDialog === '1' && !node.matches('.modal')) continue;
    return true;
  }
  return false;
}

export function markAppletFrame(modal, content) {
  if (!modal || !content || (!modal.matches('.modal, .research-overlay, .notes-pane') && !content.matches('.notes-pane')) || modal.id === 'chat-workspace'
      || content.closest('.settings-theme-surface-content')
      || isBlockingAppletFrame(content)) return false;
  content.classList.add('oc-applet-frame');
  return true;
}

// One bounded observer publishes centering properties and notifies the existing
// geometry owners. Observers follow nav mount/unmount; no polling is required.
const workspaceListeners = new Set();
let workspaceObserverStarted = false, workspaceFrame = 0, workspaceSignature = '';
export function observeWindowWorkspace(listener) {
  workspaceListeners.add(listener);
  startWorkspaceObserver();
  return () => workspaceListeners.delete(listener);
}
function startWorkspaceObserver() {
  if (workspaceObserverStarted || typeof document === 'undefined') return;
  if (!document.body) {
    document.addEventListener('DOMContentLoaded', startWorkspaceObserver, { once:true });
    return;
  }
  workspaceObserverStarted = true;
  let watched = [];
  const schedule = () => {
    if (workspaceFrame) return;
    workspaceFrame = requestAnimationFrame(() => {
      workspaceFrame = 0;
      const bounds = windowViewportBounds();
      const signature = JSON.stringify(bounds);
      if (signature === workspaceSignature) return;
      workspaceSignature = signature;
      for (const key of ['left', 'top', 'width', 'height']) document.documentElement.style.setProperty('--oc-workspace-' + key, bounds[key] + 'px');
      document.documentElement.style.setProperty('--oc-workspace-right', (window.innerWidth - bounds.right) + 'px');
      for (const callback of workspaceListeners) { try { callback(bounds); } catch (error) { console.warn('Applet workspace update failed', error); } }
    });
  };
  const navObserver = new MutationObserver(schedule);
  const sizeObserver = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(schedule) : null;
  const sync = () => {
    const next = [document.getElementById('sidebar'), document.getElementById('icon-rail')].filter(Boolean);
    if (next.length === watched.length && next.every((nav, index) => nav === watched[index])) return;
    navObserver.disconnect(); sizeObserver?.disconnect(); watched = next;
    for (const nav of watched) {
      navObserver.observe(nav, { attributes:true, attributeFilter:['class', 'style'] });
      sizeObserver?.observe(nav);
    }
    schedule();
  };
  new MutationObserver(sync).observe(document.body, { childList:true, subtree:true });
  new MutationObserver(schedule).observe(document.documentElement, { attributes:true, attributeFilter:['class'] });
  new MutationObserver(schedule).observe(document.body, { attributes:true, attributeFilter:['class'] });
  document.addEventListener('transitionend', event => { if (watched.includes(event.target)) schedule(); });
  window.addEventListener('resize', schedule);
  window.visualViewport?.addEventListener('resize', schedule);
  sync(); schedule();
}
if (typeof document !== 'undefined') startWorkspaceObserver();

export function visibleWindowBounds(target) {
  const viewport = windowViewportBounds();
  const content = target?.closest?.('.modal-content,dialog[open],.notes-pane')
    || target?.closest?.('.modal')?.querySelector(':scope > .modal-content');
  if (!content) return viewport;
  const rect = content.getBoundingClientRect();
  const left = Math.max(viewport.left, rect.left), top = Math.max(viewport.top, rect.top);
  const right = Math.min(viewport.right, rect.right), bottom = Math.min(viewport.bottom, rect.bottom);
  return right > left && bottom > top ? { left, top, right, bottom, width:right - left, height:bottom - top } : viewport;
}

export function clampFloatingWindow(content, options = {}) {
  if (!content?.isConnected || content.closest('.hidden,[hidden],.modal-minimized,.modal-closing')
      || !content.getClientRects().length || options.isLocked?.()) return;
  const modal = content.closest('.modal') || content;
  if (modal.classList.contains('modal-left-docked') || modal.classList.contains('modal-right-docked')
      || content.dataset._tileZone || options.fsClass && modal.classList.contains(options.fsClass)) return;
  // Restore replays the entrance scale. Like the drag helper, cancel the
  // window's own finite animation BEFORE measuring/pinning its final box.
  // A smaller animated rect is not the width/height that remains afterward.
  try {
    content.getAnimations().filter(animation => animation.playState !== 'finished'
      && animation.effect?.getTiming().iterations !== Infinity).forEach(animation => animation.cancel());
  } catch (_) {}
  // A restored pixel minimum (e.g. min-height:780px) wins over a smaller
  // height/max-height. Keep that minimum, but cap it responsively so it cannot
  // force the box past a narrower/shorter viewport, including mobile sheets.
  const computed = getComputedStyle(content);
  for (const [property, value, unit] of [['min-width', computed.minWidth, 'vw'], ['min-height', computed.minHeight, 'dvh']]) {
    if (/^\d+(?:\.\d+)?px$/.test(value) && parseFloat(value) > 0
        && !content.style.getPropertyValue(property).startsWith('min(')) {
      content.style.setProperty(property, `min(${value}, var(--oc-workspace-${property === 'min-width' ? 'width' : 'height'}, 100${unit}))`, content.style.getPropertyPriority(property));
    }
  }
  for (const [property, axis] of [['min-width', 'width'], ['min-height', 'height']]) {
    const legacy = /^min\((\d+(?:\.\d+)?px),\s*100(?:vw|dvh)\)$/.exec(content.style.getPropertyValue(property));
    if (legacy) content.style.setProperty(property, `min(${legacy[1]}, var(--oc-workspace-${axis}))`, content.style.getPropertyPriority(property));
  }
  const bounds = windowViewportBounds(), rect = content.getBoundingClientRect();
  const size = normalizeWindowSizeRecord({ width:rect.width, height:rect.height }, {
    width:bounds.width, height:bounds.height, minWidth:options.minWidth, minHeight:options.minHeight,
  });
  if (!size) return;
  const fixed = getComputedStyle(content).position === 'fixed';
  const outside = rect.left < bounds.left || rect.top < bounds.top || rect.right > bounds.right || rect.bottom > bounds.bottom;
  if (Math.round(rect.width) !== size.width || Math.round(rect.height) !== size.height) {
    content.style.width = size.width + 'px'; content.style.height = size.height + 'px';
    content.style.maxWidth = 'none'; content.style.maxHeight = 'none';
  }
  if (fixed || outside) {
    content.style.position = 'fixed'; content.style.margin = '0'; content.style.transform = 'none';
    content.style.left = Math.max(bounds.left, Math.min(rect.left, bounds.right - size.width)) + 'px';
    content.style.top = Math.max(bounds.top, Math.min(rect.top, bounds.bottom - size.height)) + 'px';
  }
}

export function makeWindowResizable(content, options = {}) {
  if (!content) return;
  const modal = options.modal || null;
  const mobileSkip = (typeof options.mobileSkip === 'number') ? options.mobileSkip : 768;
  const minW = options.minWidth || MIN_W;
  const minH = options.minHeight || MIN_H;
  const isLocked = options.isLocked || (() => false);
  const onResizeEnd = options.onResizeEnd || null;
  const storageKey = options.storageKey || null;

  const _skip = () => (mobileSkip > 0 && window.innerWidth <= mobileSkip) || isLocked();

  // Which borders is (cx,cy) within EDGE px of? Only counts when the pointer
  // is also within the window's span on the perpendicular axis, so the corners
  // resolve to true diagonal grips rather than the whole side.
  function edgesAt(cx, cy) {
    const r = content.getBoundingClientRect();
    const within = (cy >= r.top - EDGE && cy <= r.bottom + EDGE && cx >= r.left - EDGE && cx <= r.right + EDGE);
    if (!within) return { l: false, r: false, t: false, b: false, rect: r };
    const onY = cy >= r.top - EDGE && cy <= r.bottom + EDGE;
    const onX = cx >= r.left - EDGE && cx <= r.right + EDGE;
    return {
      l: Math.abs(cx - r.left) <= EDGE && onY,
      r: Math.abs(cx - r.right) <= EDGE && onY,
      t: Math.abs(cy - r.top) <= EDGE && onX,
      b: Math.abs(cy - r.bottom) <= EDGE && onX,
      rect: r,
    };
  }

  function cursorFor(e) {
    if ((e.l && e.t) || (e.r && e.b)) return 'nwse-resize';
    if ((e.r && e.t) || (e.l && e.b)) return 'nesw-resize';
    if (e.l || e.r) return 'ew-resize';
    if (e.t || e.b) return 'ns-resize';
    return '';
  }

  let hoverCursor = false;
  function clearHoverCursor() {
    if (hoverCursor) { content.style.cursor = ''; hoverCursor = false; }
  }
  function onHover(ev) {
    if (resizing) return;
    if (_skip()) { clearHoverCursor(); return; }
    if (ev.target && ev.target.closest && ev.target.closest(INTERACTIVE)) { clearHoverCursor(); return; }
    const c = cursorFor(edgesAt(ev.clientX, ev.clientY));
    if (c) { content.style.cursor = c; hoverCursor = true; }
    else clearHoverCursor();
  }

  let resizing = false;
  let active = null;
  let startRect = null, startX = 0, startY = 0;

  function begin(cx, cy, edges) {
    resizing = true;
    active = edges;
    // Kill the modal/pane open-animation (a scale transform that runs for the
    // first ~200-250ms) BEFORE measuring. Done as a permanent inline style
    // rather than a toggled class on purpose: a class that flips animation
    // off→on would re-trigger the scale-in on mouseup, mis-measuring the final
    // size and visibly popping the window. The open animation is a one-shot,
    // so killing it for this instance is harmless (it replays on next open).
    content.style.animation = 'none';
    content.classList.add('window-resizing');
    clampFloatingWindow(content, { minWidth:minW, minHeight:minH });
    const r = content.getBoundingClientRect();
    startRect = { left: r.left, top: r.top, width: r.width, height: r.height };
    startX = cx; startY = cy;
    // Pin to fixed with explicit box, same as the drag helper does, so the
    // centering transform / margin stops fighting the new dimensions. Drop the
    // max-width/height caps (e.g. 85vh) so the window can actually grow.
    content.style.position = 'fixed';
    content.style.margin = '0';
    content.style.transform = 'none';
    content.style.left = r.left + 'px';
    content.style.top = r.top + 'px';
    content.style.width = r.width + 'px';
    content.style.height = r.height + 'px';
    content.style.maxWidth = 'none';
    content.style.maxHeight = 'none';
    document.body.classList.add('window-resizing-active');
    document.body.style.cursor = cursorFor(edges);
  }

  function move(cx, cy) {
    if (!resizing) return;
    const dx = cx - startX, dy = cy - startY;
    let { left, top, width, height } = startRect;
    const bounds = windowViewportBounds(), vw = bounds.right, vh = bounds.bottom;
    const minimumW = Math.min(minW, bounds.width), minimumH = Math.min(minH, bounds.height);
    if (active.r) width = startRect.width + dx;
    if (active.b) height = startRect.height + dy;
    if (active.l) { width = startRect.width - dx; left = startRect.left + dx; }
    if (active.t) { height = startRect.height - dy; top = startRect.top + dy; }
    // Min-size clamps — keep the opposite edge anchored when pulling from
    // the left/top so the window doesn't jump.
    if (width < minimumW) { if (active.l) left = startRect.left + (startRect.width - minimumW); width = minimumW; }
    if (height < minimumH) { if (active.t) top = startRect.top + (startRect.height - minimumH); height = minimumH; }
    // Keep the window on-screen and never larger than the viewport.
    if (active.l && left < bounds.left) { width += left - bounds.left; left = bounds.left; }
    if (active.t && top < bounds.top) { height += top - bounds.top; top = bounds.top; }
    if (left + width > vw) width = Math.max(minimumW, vw - left);
    if (top + height > vh) height = Math.max(minimumH, vh - top);
    content.style.left = left + 'px';
    content.style.top = top + 'px';
    content.style.width = width + 'px';
    content.style.height = height + 'px';
    clampFloatingWindow(content, { minWidth:minW, minHeight:minH });
  }

  function end() {
    if (!resizing) return;
    resizing = false;
    content.classList.remove('window-resizing');
    document.body.classList.remove('window-resizing-active');
    document.body.style.cursor = '';
    clearHoverCursor();
    clampFloatingWindow(content, { minWidth:minW, minHeight:minH });
    const r = content.getBoundingClientRect();
    if (storageKey) {
      try {
        const record = normalizeWindowSizeRecord(
          { width:r.width, height:r.height },
          { width:windowViewportBounds().width, height:windowViewportBounds().height, minWidth:minW, minHeight:minH },
        );
        if (record) localStorage.setItem(storageKey, JSON.stringify(record));
      } catch (_) {}
    }
    if (onResizeEnd) { try { onResizeEnd({ rect: r }); } catch (_) {} }
  }

  function armFrom(target, cx, cy) {
    if (_skip()) return false;
    if (target && target.closest && target.closest(INTERACTIVE)) return false;
    const edges = edgesAt(cx, cy);
    if (!(edges.l || edges.r || edges.t || edges.b)) return false;
    begin(cx, cy, edges);
    return true;
  }

  // Capture phase: pre-empt the header's drag listener (which lives on a
  // descendant and fires in the bubble phase) when the grab lands on a border.
  content.addEventListener('mousedown', (ev) => {
    if (ev.button !== 0) return;
    if (!armFrom(ev.target, ev.clientX, ev.clientY)) return;
    ev.preventDefault();
    ev.stopPropagation();
    const mu = () => {
      end();
      document.removeEventListener('mousemove', mm);
      document.removeEventListener('mouseup', mu);
    };
    // Self-heal a missed mouseup (released outside the window, dropped event,
    // window blur): a move with no buttons pressed means the drag is over —
    // finish instead of running away on every subsequent mousemove.
    const mm = (e) => {
      if (e.buttons === 0) { mu(); return; }
      move(e.clientX, e.clientY);
    };
    document.addEventListener('mousemove', mm);
    document.addEventListener('mouseup', mu);
  }, true);

  content.addEventListener('mousemove', onHover);
  content.addEventListener('mouseleave', clearHoverCursor);

  content.addEventListener('touchstart', (ev) => {
    const t = ev.touches[0];
    if (!t) return;
    if (!armFrom(ev.target, t.clientX, t.clientY)) return;
    ev.preventDefault();
    ev.stopPropagation();
    const tm = (e) => { const tt = e.touches[0]; if (tt) move(tt.clientX, tt.clientY); };
    const te = () => {
      end();
      document.removeEventListener('touchmove', tm);
      document.removeEventListener('touchend', te);
      document.removeEventListener('touchcancel', te);
    };
    document.addEventListener('touchmove', tm, { passive: false });
    document.addEventListener('touchend', te);
    document.addEventListener('touchcancel', te);
  }, true);

  // Restore a previously chosen size on (re)open. Applying width/height inline
  // while the window is still centered by its overlay keeps it centered at the
  // new size; once dragged/resized it pins to fixed as usual.
  //
  // Deferred one frame on purpose: some windows (e.g. Notes) snap to an edge
  // dock or fullscreen synchronously right AFTER this helper is wired. Waiting a
  // frame lets that settle so we can re-check _skip() and NOT stretch a
  // docked/fullscreen window to a stale windowed size. The open animation masks
  // the one-frame delay, so there is no visible jump.
  function clampToViewport() {
    // mobileSkip disables resize gestures, not the fit check after restoring
    // a desktop window or reducing the viewport. Locked layouts own their box.
    if (isLocked() || !content.isConnected) return;
    clampFloatingWindow(content, { minWidth:minW, minHeight:minH });
  }

  let clampFrame = 0;
  const scheduleClamp = () => {
    cancelAnimationFrame(clampFrame);
    clampFrame = requestAnimationFrame(clampToViewport);
  };
  window.addEventListener('resize', scheduleClamp);
  const stopWorkspaceWatch = observeWindowWorkspace(() => {
    if (!content.isConnected) { stopWorkspaceWatch(); return; }
    scheduleClamp();
  });
  window.addEventListener('odysseus:modal-opened', event => { if (event.detail?.modal === modal) scheduleClamp(); });
  window.visualViewport?.addEventListener('resize', scheduleClamp);

  requestAnimationFrame(() => {
    if (isLocked() || !content.isConnected) return;
    if (storageKey && !_skip()) {
      try {
        const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
        const record = normalizeWindowSizeRecord(saved, {
          width:windowViewportBounds().width,
          height:windowViewportBounds().height,
          minWidth:minW,
          minHeight:minH,
        });
        if (record) {
          content.style.width = record.width + 'px';
          content.style.height = record.height + 'px';
          content.style.maxWidth = 'none';
          content.style.maxHeight = 'none';
          localStorage.setItem(storageKey, JSON.stringify(record));
        }
      } catch (_) {}
    }
    scheduleClamp();
  });
}
