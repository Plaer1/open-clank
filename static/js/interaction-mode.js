// Single Open Clank interaction-mode picker. Provider-native mode names stay
// behind the server/ACP adapter and are never rendered here.

const MODES = Object.freeze({
  chat: { label: 'Chat', description: 'Read-only answers and safe context' },
  plan: { label: 'Plan', description: 'Investigate and propose a plan' },
  agent: { label: 'Agent', description: 'Use the enabled agent tools' },
});

export function initInteractionModeControl({
  documentRef = document,
  getMode = () => 'agent',
  setMode = () => {},
} = {}) {
  const root = documentRef.getElementById('chat-mode-picker');
  const button = documentRef.getElementById('chat-mode-picker-btn');
  const label = documentRef.getElementById('chat-mode-picker-label');
  const menu = documentRef.getElementById('chat-mode-picker-menu');
  if (!root || !button || !label || !menu) return { destroy() {} };

  if (root.__openClankInteractionMode?.destroy) {
    root.__openClankInteractionMode.destroy();
  }

  let open = false;
  let destroyed = false;
  const options = Array.from(menu.querySelectorAll('[data-interaction-mode]'));

  const normalize = (mode) => Object.hasOwn(MODES, mode) ? mode : 'agent';
  const render = (mode = getMode()) => {
    const normalized = normalize(mode);
    const info = MODES[normalized];
    label.textContent = info.label;
    button.dataset.mode = normalized;
    button.title = `${info.label} mode — ${info.description}`;
    button.setAttribute('aria-label', `Interaction mode: ${info.label}`);
    options.forEach((option) => {
      const selected = option.dataset.interactionMode === normalized;
      option.setAttribute('aria-selected', String(selected));
      option.classList.toggle('selected', selected);
    });
  };
  const close = (restoreFocus = false) => {
    open = false;
    menu.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    if (restoreFocus) button.focus();
  };
  const toggle = () => {
    open = !open;
    menu.hidden = !open;
    button.setAttribute('aria-expanded', String(open));
    if (open) options.find((item) => item.getAttribute('aria-selected') === 'true')?.focus();
    else button.focus();
  };
  const choose = (mode) => {
    const normalized = normalize(mode);
    setMode(normalized);
    render(normalized);
    close(true);
  };
  const onButtonClick = () => toggle();
  const onDocumentClick = (event) => {
    if (open && !root.contains(event.target)) close();
  };
  const onKeyDown = (event) => {
    if (!open && (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      toggle();
      return;
    }
    if (!open) return;
    const current = Math.max(0, options.indexOf(documentRef.activeElement));
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown') {
      event.preventDefault();
      options[(current + 1) % options.length]?.focus();
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      options[(current - 1 + options.length) % options.length]?.focus();
    } else if (event.key === 'Home') {
      event.preventDefault();
      options[0]?.focus();
    } else if (event.key === 'End') {
      event.preventDefault();
      options.at(-1)?.focus();
    } else if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      choose(documentRef.activeElement?.dataset.interactionMode);
    }
  };
  const onMenuClick = (event) => {
    const option = event.target.closest?.('[data-interaction-mode]');
    if (option) choose(option.dataset.interactionMode);
  };
  button.addEventListener('click', onButtonClick);
  button.addEventListener('keydown', onKeyDown);
  menu.addEventListener('keydown', onKeyDown);
  menu.addEventListener('click', onMenuClick);
  documentRef.addEventListener('click', onDocumentClick);
  window.__odysseusInteractionModeSync = render;
  render();

  const handle = {
    destroy() {
      if (destroyed) return;
      destroyed = true;
      button.removeEventListener('click', onButtonClick);
      button.removeEventListener('keydown', onKeyDown);
      menu.removeEventListener('keydown', onKeyDown);
      menu.removeEventListener('click', onMenuClick);
      documentRef.removeEventListener('click', onDocumentClick);
      if (window.__odysseusInteractionModeSync === render) {
        delete window.__odysseusInteractionModeSync;
      }
      close();
    },
    render,
  };
  root.__openClankInteractionMode = handle;
  return handle;
}

export { MODES };
export default initInteractionModeControl;
