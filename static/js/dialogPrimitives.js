// Small, browser-only-on-invocation dialog primitives.
//
// Keep this module free of imports and top-level DOM access.  Copal's model
// and save helpers are also used by Node based tests and tooling; importing a
// text prompt must not initialize the application's large UI module graph.

/**
 * Open a themed text prompt and resolve with the trimmed value, or null when
 * the user cancels, presses Escape, or clicks the backdrop.
 */
export function styledPrompt(message, {
  title = 'Name',
  defaultValue = '',
  placeholder = '',
  confirmText = 'Save',
  cancelText = 'Cancel',
  maxLength = 80,
} = {}) {
  // A model-only import should remain usable in Node and other non-DOM
  // renderers.  There is no meaningful prompt result in those environments;
  // resolve as a cancellation instead of throwing at module-import time.
  if (typeof document === 'undefined') return Promise.resolve(null);

  return new Promise(resolve => {
    let overlay = document.getElementById('styled-prompt-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'styled-prompt-overlay';
      overlay.className = 'modal';
      overlay.innerHTML =
        '<div class="modal-content styled-confirm-box styled-prompt-box" role="dialog" aria-modal="true" aria-labelledby="styled-prompt-title" aria-describedby="styled-prompt-msg">' +
          '<div class="modal-header"><h4 id="styled-prompt-title"></h4></div>' +
          '<div class="modal-body">' +
            '<p id="styled-prompt-msg"></p>' +
            '<input type="text" id="styled-prompt-input" class="styled-prompt-input" />' +
          '</div>' +
          '<div class="modal-footer">' +
            '<button id="styled-prompt-cancel" class="confirm-btn confirm-btn-secondary"></button>' +
            '<button id="styled-prompt-ok" class="confirm-btn confirm-btn-primary"></button>' +
          '</div>' +
        '</div>';
      (document.body || document.documentElement).appendChild(overlay);
    }

    const titleEl = overlay.querySelector('#styled-prompt-title');
    const msgEl = overlay.querySelector('#styled-prompt-msg');
    const input = overlay.querySelector('#styled-prompt-input');
    const okBtn = overlay.querySelector('#styled-prompt-ok');
    const cancelBtn = overlay.querySelector('#styled-prompt-cancel');
    if (!titleEl || !msgEl || !input || !okBtn || !cancelBtn) {
      resolve(null);
      return;
    }

    titleEl.textContent = title;
    msgEl.textContent = message || '';
    msgEl.style.display = message ? '' : 'none';
    input.value = defaultValue || '';
    input.placeholder = placeholder || '';
    input.maxLength = maxLength;
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;

    const previousFocus = document.activeElement;
    overlay.classList.remove('hidden');
    overlay.style.display = '';

    let settled = false;
    function cleanup(result) {
      if (settled) return;
      settled = true;
      overlay.classList.add('hidden');
      overlay.style.display = 'none';
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      overlay.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      input.removeEventListener('keydown', onInputKey);
      try { previousFocus?.focus?.({ preventScroll: true }); } catch (_) {
        try { previousFocus?.focus?.(); } catch (_) {}
      }
      resolve(result);
    }
    function onOk() { cleanup((input.value || '').trim()); }
    function onCancel() { cleanup(null); }
    function onBackdrop(event) {
      if (event.target === overlay) cleanup(null);
    }
    function onKey(event) {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation?.();
        cleanup(null);
      } else if (event.key === 'Tab') {
        event.preventDefault();
        const focusables = [input, cancelBtn, okBtn];
        const index = focusables.indexOf(document.activeElement);
        const next = event.shiftKey
          ? (index <= 0 ? focusables.length - 1 : index - 1)
          : (index >= focusables.length - 1 ? 0 : index + 1);
        focusables[next].focus();
      }
    }
    function onInputKey(event) {
      if (event.key === 'Enter') {
        event.preventDefault();
        onOk();
      }
    }

    okBtn.addEventListener('click', onOk);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    input.addEventListener('keydown', onInputKey);

    const raf = document.defaultView?.requestAnimationFrame;
    if (typeof raf === 'function') raf(() => { input.focus(); input.select(); });
    else { input.focus(); input.select(); }
  });
}

/**
 * Open a themed confirmation dialog and resolve with true, false, or
 * "alternate" when the optional third action is selected.
 *
 * This mirrors the application dialog contract without importing ui.js.  A
 * non-browser caller gets a safe cancellation result, keeping Copal model
 * imports usable in Node tests and tooling.
 */
export function styledConfirm(message, {
  confirmText = 'Confirm',
  cancelText = 'Cancel',
  alternateText = '',
  title = 'Confirm',
  danger = false,
} = {}) {
  if (typeof document === 'undefined') return Promise.resolve(false);
  // The full application exposes its established implementation globally.
  // Reuse that owner when present so mounted pages never create two overlays
  // with the same IDs or attach competing listeners to one dialog.
  if (typeof window !== 'undefined' && typeof window.styledConfirm === 'function' && window.styledConfirm !== styledConfirm) {
    return window.styledConfirm(message, { confirmText, cancelText, alternateText, title, danger });
  }

  return new Promise(resolve => {
    let overlay = document.getElementById('styled-confirm-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = 'styled-confirm-overlay';
      overlay.className = 'modal';
      overlay.innerHTML =
        '<div class="modal-content styled-confirm-box" role="dialog" aria-modal="true" aria-labelledby="styled-confirm-title" aria-describedby="styled-confirm-msg">' +
          '<div class="modal-header"><h4 id="styled-confirm-title"></h4></div>' +
          '<div class="modal-body"><p id="styled-confirm-msg"></p></div>' +
          '<div class="modal-footer">' +
            '<button id="styled-confirm-cancel"></button>' +
            '<button id="styled-confirm-alt" style="display:none;"></button>' +
            '<button id="styled-confirm-ok"></button>' +
          '</div>' +
        '</div>';
      (document.body || document.documentElement).appendChild(overlay);
    }

    const msgEl = overlay.querySelector('#styled-confirm-msg');
    const titleEl = overlay.querySelector('#styled-confirm-title');
    const okBtn = overlay.querySelector('#styled-confirm-ok');
    const cancelBtn = overlay.querySelector('#styled-confirm-cancel');
    const altBtn = overlay.querySelector('#styled-confirm-alt');
    if (!msgEl || !titleEl || !okBtn || !cancelBtn || !altBtn) {
      resolve(false);
      return;
    }

    titleEl.textContent = title || 'Confirm';
    msgEl.textContent = message || '';
    okBtn.textContent = confirmText;
    cancelBtn.textContent = cancelText;
    altBtn.textContent = alternateText || '';
    okBtn.className = danger ? 'confirm-btn confirm-btn-danger' : 'confirm-btn confirm-btn-primary';
    cancelBtn.className = 'confirm-btn confirm-btn-secondary';
    altBtn.className = 'confirm-btn confirm-btn-secondary';
    altBtn.style.display = alternateText ? '' : 'none';

    const previousFocus = document.activeElement;
    overlay.classList.remove('hidden');
    overlay.style.display = '';

    let settled = false;
    function cleanup(result) {
      if (settled) return;
      settled = true;
      overlay.classList.add('hidden');
      overlay.style.display = 'none';
      okBtn.removeEventListener('click', onOk);
      cancelBtn.removeEventListener('click', onCancel);
      altBtn.removeEventListener('click', onAlt);
      overlay.removeEventListener('click', onBackdrop);
      document.removeEventListener('keydown', onKey);
      try { previousFocus?.focus?.({ preventScroll: true }); } catch (_) {
        try { previousFocus?.focus?.(); } catch (_) {}
      }
      resolve(result);
    }
    function onOk() { cleanup(true); }
    function onAlt() { cleanup('alternate'); }
    function onCancel() { cleanup(false); }
    function onBackdrop(event) {
      if (event.target === overlay) cleanup(false);
    }
    function onKey(event) {
      const focusables = alternateText ? [cancelBtn, altBtn, okBtn] : [cancelBtn, okBtn];
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        const index = focusables.indexOf(document.activeElement);
        const direction = event.key === 'ArrowRight' ? 1 : -1;
        focusables[(index + direction + focusables.length) % focusables.length].focus();
      } else if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        event.stopImmediatePropagation?.();
        cleanup(false);
      } else if (event.key === 'Tab') {
        event.preventDefault();
        const index = focusables.indexOf(document.activeElement);
        const next = event.shiftKey
          ? (index <= 0 ? focusables.length - 1 : index - 1)
          : (index >= focusables.length - 1 ? 0 : index + 1);
        focusables[next].focus();
      }
    }

    okBtn.addEventListener('click', onOk);
    altBtn.addEventListener('click', onAlt);
    cancelBtn.addEventListener('click', onCancel);
    overlay.addEventListener('click', onBackdrop);
    document.addEventListener('keydown', onKey);
    okBtn.focus();
  });
}

export default { styledConfirm, styledPrompt };
