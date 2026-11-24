import { openContextActionMenu, registerAdapter } from './custom-context-menu.js';

const otherControls = 'button, a[href], input, textarea, select, label, [contenteditable], [draggable="true"], img, video, audio';

export function bindCardActionMenu(card, {
  button, items, current = () => true, allowGesture = () => true, exclude = '',
}) {
  const isCurrent = () => card.isConnected && !card.closest('.hidden, [hidden], .modal-minimized, .modal-closing') &&
    (!button || (button.isConnected && card.contains(button))) && current();
  const identity = () => [
    document.body?.dataset.accountId,
    window.chatModule?.ratingAccountScope?.()?.authEpoch,
  ];
  const capture = () => {
    const account = identity();
    const valid = () => isCurrent() && identity().every((value, i) => value === account[i]);
    return {
      valid,
      items: items(valid).map(item => ({ shortcut: '', ...item })),
    };
  };
  registerAdapter(card, {
    hitTest(target, event) {
      if (!isCurrent() || !allowGesture(event)) return false;
      if (target === card || button?.contains(target)) return true;
      const control = target.closest(otherControls);
      if (control && control !== card && card.contains(control)) return false;
      return exclude && target.closest(exclude) ? 'native' : true;
    },
    capture,
    isCurrent: ({ adapterContext }) => adapterContext?.valid() === true,
    commands: ({ adapterContext }) => adapterContext?.items ?? [],
    execute(id, { adapterContext }) {
      return adapterContext.items.find(item => item.id === id)?.run?.();
    },
    editingCommands: false,
    focusOrigin: ({ rawTarget }) => button?.contains(rawTarget) ? button : card,
  });
  if (!card.matches('button, a[href], [tabindex]')) card.tabIndex = 0;
  if (button) {
    button.setAttribute('aria-haspopup', 'menu');
    button.addEventListener('click', event => {
      event.stopPropagation();
      if (!isCurrent()) return;
      const request = capture();
      openContextActionMenu({ anchor: button, event, items: request.items, isCurrent: request.valid });
    });
  }
}
