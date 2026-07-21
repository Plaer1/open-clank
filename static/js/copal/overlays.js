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

export function wireDialog(dialog, { dismissable = true, restoreFocus = true } = {}) {
  const previous = restoreFocus ? document.activeElement : null;
  // dismissable:false is best-effort: the platform's close watcher only honors
  // preventDefault on cancel after user activation, so a determined user can
  // always escape. Callers must therefore stay correct when the dialog closes
  // early (e.g. an import keeps running and reports through status instead).
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
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
    dialog.remove();
    if (previous?.isConnected) previous.focus?.({ preventScroll:true });
  }, { once:true });
  return dialog;
}

export function wirePopover(details) {
  let release = null;
  details.addEventListener('toggle', () => {
    if (details.open && !release) {
      release = bindMenuDismiss(details, () => {
        release = null;
        details.open = false;
      });
    } else if (!details.open && release) {
      const done = release;
      release = null;
      done();
    }
  });
  return details;
}
