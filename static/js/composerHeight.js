// Manual sizing sets a floor. Longer messages can still grow naturally and
// an existing draft can always shrink back to its measured height on reset.
export function mountComposerHeight(textarea, autoResize) {
  const pane = textarea.closest('#chat-container');
  const bar = textarea.closest('.chat-input-bar');
  const handle = bar?.querySelector('.composer-height-handle');
  if (!pane || !handle || handle.dataset.ready) return;
  handle.dataset.ready = 'true';
  const welcome = pane.querySelector('#welcome-screen');
  let preferred = 0;
  let drag = null;
  function update() {
    const usable = Math.min(pane.clientHeight, window.visualViewport?.height || window.innerHeight);
    pane.style.setProperty('--composer-pane-height', usable + 'px');
    const compact = usable <= 540;
    pane.toggleAttribute('data-compact-welcome', compact);
    let limit = usable * .48;
    if (pane.classList.contains('welcome-active') && welcome?.offsetHeight) {
      const style = getComputedStyle(welcome);
      const freeSpace = parseFloat(style.marginTop) + parseFloat(style.marginBottom);
      const clipped = Math.max(0, welcome.scrollHeight - welcome.clientHeight);
      // Leave breathing room on both sides of the centered welcome group.
      limit = Math.min(limit, textarea.offsetHeight + freeSpace - clipped - (compact ? 16 : 64));
    }
    const maximum = Math.max(24, Math.floor(limit));
    const minimum = Math.min(preferred, maximum);
    textarea.style.setProperty('--composer-height-limit', maximum + 'px');
    textarea.style.setProperty('--composer-height-floor', minimum + 'px');
    autoResize(textarea);
    handle.setAttribute('aria-valuemax', String(maximum));
    handle.setAttribute('aria-valuenow', String(Math.round(textarea.getBoundingClientRect().height)));
  }
  handle.addEventListener('pointerdown', event => {
    if (!event.isPrimary || event.button !== 0) return;
    event.stopPropagation();
    handle.setPointerCapture(event.pointerId);
    drag = {id:event.pointerId, y:event.clientY, height:textarea.getBoundingClientRect().height};
  });
  window.addEventListener('pointermove', event => {
    if (!drag || drag.id !== event.pointerId) return;
    preferred = Math.max(24, Math.min(Number(handle.getAttribute('aria-valuemax')), drag.height + drag.y - event.clientY));
    update();
  });
  const endDrag = event => { if (event.pointerId === drag?.id) drag = null; };
  window.addEventListener('pointerup', endDrag);
  window.addEventListener('pointercancel', endDrag);
  handle.addEventListener('lostpointercapture', endDrag);
  handle.addEventListener('dblclick', event => {
    event.stopPropagation();
    preferred = 0;
    update();
  });
  handle.addEventListener('keydown', event => {
    const delta = {ArrowUp:24, ArrowDown:-24}[event.key];
    if (!delta && event.key !== 'Home') return;
    event.preventDefault();
    event.stopPropagation();
    preferred = delta ? Math.min(Number(handle.getAttribute('aria-valuemax')), Math.max(24, textarea.offsetHeight + delta)) : 0;
    update();
  });
  const observer = new ResizeObserver(update);
  observer.observe(pane);
  observer.observe(bar);
  if (welcome) {
    observer.observe(welcome);
    for (const child of welcome.children) {
      if (child instanceof HTMLElement && child.tagName !== 'SCRIPT') observer.observe(child);
    }
  }
  window.visualViewport?.addEventListener('resize', update);
  textarea.addEventListener('input', () => requestAnimationFrame(() => handle.setAttribute('aria-valuenow', String(textarea.offsetHeight))));
  update();
}
