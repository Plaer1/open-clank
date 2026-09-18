// Presentation-only internal splitter shared by Files and Code.
export function createResizablePane({
  container,
  sidebar,
  main,
  separator,
  side = 'left',
  cssVar = '--oc-explorer-sidebar-width',
  defaultWidth = 240,
  minWidth = 180,
  maxWidth = 420,
  mainMin = 320,
  compactBreakpoint = 680,
  storageKey = '',
  getOwner = () => '',
  onChange = null,
} = {}) {
  const finite = (value, fallback) => {
    const number = Number(value);
    return Number.isFinite(number) ? number : fallback;
  };
  const minimum = Math.max(0, Math.round(finite(minWidth, 180)));
  const maximum = Math.max(minimum, Math.round(finite(maxWidth, 420)));
  const fallbackWidth = Math.max(minimum, Math.min(maximum, Math.round(finite(defaultWidth, 240))));
  const requiredMainWidth = Math.max(0, Math.round(finite(mainMin, 320)));
  const compactAt = Math.max(0, finite(compactBreakpoint, 680));
  const noOp = {
    apply() { return fallbackWidth; },
    refresh() {},
    destroy() {},
  };
  if (!container || !sidebar || !main || !separator) return noOp;

  let destroyed = false;
  let activePointerId = null;
  let startX = 0;
  let startWidth = fallbackWidth;
  let preferredWidth = fallbackWidth;
  let renderedWidth = null;
  let observer = null;
  let lastSeparatorWidth = 0;

  const owner = () => {
    try {
      const value = getOwner?.();
      return value == null ? '' : String(value);
    } catch (_) {
      return '';
    }
  };
  const scopedKey = ownerName => `${storageKey}:${encodeURIComponent(ownerName)}`;
  const read = ownerName => {
    if (!storageKey || !ownerName) return null;
    try {
      const raw = globalThis.localStorage?.getItem(scopedKey(ownerName));
      if (raw == null || String(raw).trim() === '') return null;
      const value = Number(raw);
      return Number.isFinite(value) ? value : null;
    } catch (_) {
      return null;
    }
  };
  const write = (ownerName, value) => {
    if (!storageKey || !ownerName) return;
    try { globalThis.localStorage?.setItem(scopedKey(ownerName), String(value)); } catch (_) {}
  };
  const rectWidth = element => {
    try {
      const value = Number(element.getBoundingClientRect?.().width);
      return Number.isFinite(value) && value > 0 ? value : 0;
    } catch (_) {
      return 0;
    }
  };
  const constraints = () => {
    const available = rectWidth(container);
    const compact = available <= compactAt;
    const measuredSeparator = rectWidth(separator);
    if (measuredSeparator > 0) lastSeparatorWidth = measuredSeparator;
    const separatorWidth = compact ? 0 : lastSeparatorWidth;
    const availableMaximum = available > 0
      ? Math.floor(available - requiredMainWidth - separatorWidth)
      : minimum;
    return {
      compact,
      min: minimum,
      max: Math.max(minimum, Math.min(maximum, availableMaximum)),
    };
  };
  const render = value => {
    if (destroyed) return renderedWidth ?? fallbackWidth;
    const bounds = constraints();
    const requested = finite(value, fallbackWidth);
    const width = Math.round(Math.max(bounds.min, Math.min(bounds.max, requested)));
    container.style.setProperty(cssVar, `${width}px`);
    separator.setAttribute('aria-valuemin', String(bounds.min));
    separator.setAttribute('aria-valuemax', String(bounds.max));
    separator.setAttribute('aria-valuenow', String(width));
    separator.setAttribute('aria-disabled', bounds.compact ? 'true' : 'false');
    separator.toggleAttribute('hidden', bounds.compact);
    const changed = renderedWidth !== width;
    renderedWidth = width;
    if (changed) onChange?.(width);
    return width;
  };
  const apply = (value, persist = true) => {
    if (destroyed) return renderedWidth ?? fallbackWidth;
    const width = render(value);
    preferredWidth = width;
    if (persist) write(owner(), width);
    return width;
  };
  const reclamp = () => {
    // A zero-width rect means the outer Open Clank window is not currently
    // participating in layout (for example, while minimized). It is not a real
    // compact breakpoint and must not replace the last useful geometry.
    if (!destroyed && rectWidth(container) > 0) render(preferredWidth);
  };

  const removeDragListeners = () => {
    separator.removeEventListener('pointermove', onPointerMove);
    separator.removeEventListener('pointerup', onPointerFinish);
    separator.removeEventListener('pointercancel', onPointerFinish);
    separator.removeEventListener('lostpointercapture', onLostPointerCapture);
  };
  const finishDrag = ({ release = true } = {}) => {
    const pointerId = activePointerId;
    activePointerId = null;
    removeDragListeners();
    try { delete separator.dataset.pointerId; } catch (_) {}
    if (release && pointerId != null) {
      try { separator.releasePointerCapture?.(pointerId); } catch (_) {}
    }
  };
  const ownsPointer = event => activePointerId != null
    && (event.pointerId == null || event.pointerId === activePointerId);
  function onPointerMove(event) {
    if (!ownsPointer(event) || destroyed) return;
    const direction = side === 'right' ? -1 : 1;
    apply(startWidth + direction * (finite(event.clientX, startX) - startX));
  }
  function onPointerFinish(event) {
    if (ownsPointer(event)) finishDrag();
  }
  function onLostPointerCapture(event) {
    if (ownsPointer(event)) finishDrag({ release: false });
  }
  const onPointerDown = event => {
    if (destroyed || separator.hidden || activePointerId != null) return;
    if (event.isPrimary === false || (event.button != null && event.button !== 0)) return;
    event.preventDefault();
    activePointerId = event.pointerId;
    startX = finite(event.clientX, 0);
    startWidth = renderedWidth ?? fallbackWidth;
    try { separator.dataset.pointerId = String(activePointerId); } catch (_) {}
    separator.addEventListener('pointermove', onPointerMove);
    separator.addEventListener('pointerup', onPointerFinish);
    separator.addEventListener('pointercancel', onPointerFinish);
    separator.addEventListener('lostpointercapture', onLostPointerCapture);
    try { separator.setPointerCapture?.(activePointerId); } catch (_) {}
  };
  const onKey = event => {
    if (destroyed || separator.hidden) return;
    let next = null;
    const step = 12;
    const current = renderedWidth ?? fallbackWidth;
    if (event.key === 'ArrowLeft') next = current + (side === 'right' ? step : -step);
    else if (event.key === 'ArrowRight') next = current + (side === 'right' ? -step : step);
    else if (event.key === 'Home') next = minimum;
    else if (event.key === 'End') next = maximum;
    if (next == null) return;
    event.preventDefault();
    apply(next);
  };
  const onReset = event => {
    if (destroyed || separator.hidden) return;
    event.preventDefault?.();
    apply(fallbackWidth);
  };

  if (!separator.hasAttribute?.('role')) separator.setAttribute('role', 'separator');
  if (!separator.hasAttribute?.('aria-orientation')) separator.setAttribute('aria-orientation', 'vertical');
  if (!separator.hasAttribute?.('tabindex')) separator.setAttribute('tabindex', '0');
  separator.addEventListener('pointerdown', onPointerDown);
  separator.addEventListener('keydown', onKey);
  separator.addEventListener('dblclick', onReset);
  preferredWidth = read(owner()) ?? fallbackWidth;
  render(preferredWidth);
  if (typeof globalThis.ResizeObserver !== 'undefined') {
    observer = new globalThis.ResizeObserver(reclamp);
    observer.observe(container);
  }

  return {
    apply,
    // Owner identity is often learned asynchronously after the shell mounts.
    // Re-read the scoped preference once that identity is available instead of
    // leaving the pane at the anonymous default for the rest of the session.
    refresh() {
      if (destroyed) return;
      preferredWidth = read(owner()) ?? fallbackWidth;
      if (renderedWidth == null || rectWidth(container) > 0) render(preferredWidth);
    },
    destroy() {
      if (destroyed) return;
      destroyed = true;
      finishDrag();
      observer?.disconnect();
      observer = null;
      separator.removeEventListener('pointerdown', onPointerDown);
      separator.removeEventListener('keydown', onKey);
      separator.removeEventListener('dblclick', onReset);
    },
  };
}
