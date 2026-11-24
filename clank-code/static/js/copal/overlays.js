// Shared dismissal wiring for Copal overlays.
//
// Native <dialog showModal> already gives correct topmost-one-per-press
// Escape semantics for stacked dialogs; the global Escape arbiter in ui.js
// yields to any open modal dialog. This module adds the rest of the overlay
// contract: backdrop dismissal, removal on close, focus restoration, and a
// non-dismissable mode for status overlays. `<details>` popover menus reuse
// the app-wide escMenuStack so Escape and outside clicks close the topmost
// transient before anything behind it.

import { bindMenuDismiss } from '../escMenuStack.js';
import {
  clearActiveModal,
  registerInputContext,
  setActiveModal,
  unregisterInputContext,
} from './inputContext.js';

let overlaySequence = 0;
const overlayContexts = new WeakMap();

function wireOverlayContext(element, kind) {
  const modalId = `${kind}:${element.id || ++overlaySequence}`;
  const context = {
    windowId: modalId,
    paneId: modalId,
    modalId,
    blockingModal: true,
    capabilities: { keyboard: true, pointer: true, wheel: true },
  };
  const unregister = registerInputContext(element, context);
  const activate = () => setActiveModal(modalId);
  element.addEventListener('focusin', activate, true);
  element.addEventListener('pointerdown', activate, true);
  overlayContexts.set(element, { modalId, unregister });
  return { modalId, unregister };
}

export function wireDialog(dialog, { dismissable = true, restoreFocus = true } = {}) {
  const previous = restoreFocus ? document.activeElement : null;
  const overlay = wireOverlayContext(dialog, 'dialog');
  const openState = new MutationObserver(() => {
    if (dialog.open) setActiveModal(overlay.modalId);
    else clearActiveModal(overlay.modalId);
  });
  openState.observe(dialog, { attributes:true, attributeFilter:['open'] });
  if (dialog.open) setActiveModal(overlay.modalId);
  // dismissable:false is best-effort: the platform's close watcher only honors
  // preventDefault on cancel after user activation, so a determined user can
  // always escape. Callers must therefore stay correct when the dialog closes
  // early (e.g. an import keeps running and reports through status instead).
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (dismissable) dialog.close();
  });
  dialog.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !dialog.open || event.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    if (dismissable) dialog.close();
  });
  if (dismissable) {
    dialog.addEventListener('pointerdown', (event) => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      const outside = event.clientX < rect.left || event.clientX > rect.right
        || event.clientY < rect.top || event.clientY > rect.bottom;
      if (outside) dialog.close();
    });
  }
  dialog.addEventListener('close', () => {
    openState.disconnect();
    clearActiveModal(overlay.modalId);
    overlay.unregister();
    dialog.remove();
    if (previous?.isConnected) previous.focus?.({ preventScroll:true });
  }, { once:true });
  return dialog;
}

export function wirePopover(details) {
  let release = null;
  const overlay = wireOverlayContext(details, 'menu');
  // The native `toggle` notification is queued after `<summary>` changes the
  // open state. Handle an immediate Escape on the focused popover itself so a
  // fast key press cannot arrive before the shared dismissal stack registers.
  details.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape' || !details.open || event.isComposing) return;
    event.preventDefault();
    event.stopPropagation();
    if (release) release();
    else details.open = false;
  });
  details.addEventListener('toggle', () => {
    if (details.open && !release) {
      setActiveModal(overlay.modalId);
      release = bindMenuDismiss(details, () => {
        release = null;
        details.open = false;
      });
    } else if (!details.open && release) {
      clearActiveModal(overlay.modalId);
      const done = release;
      release = null;
      done();
    }
    if (!details.open) clearActiveModal(overlay.modalId);
  });
  return details;
}
