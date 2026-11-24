// Open Clank permission-mode control.
// This module deliberately owns its own lifecycle so an unrelated app
// initializer failure cannot leave a visible, dead button.

const MODES = Object.freeze(['manual', 'yolo', 'auto']);
const LABELS = Object.freeze({ manual: 'Manual', yolo: 'Yolo', auto: 'Auto' });
const TOASTS = Object.freeze({
  manual: 'Permission mode: manual — approve each action',
  yolo: 'Permission mode: yolo — auto-approve, questions allowed',
  auto: 'Permission mode: auto — no prompts, no questions',
});

function normalize(value) {
  const mode = String(value || '').trim().toLowerCase();
  return MODES.includes(mode) ? mode : 'manual';
}

export function initPermissionModeControl({
  documentRef = document,
  fetchImpl = window.fetch.bind(window),
  showToast = window.uiModule?.showToast,
} = {}) {
  const button = documentRef.getElementById('permission-mode-btn');
  const label = documentRef.getElementById('permission-mode-label');
  if (!button) return { destroy() {} };

  const previous = button.__openClankPermissionControl;
  if (previous && typeof previous.destroy === 'function') previous.destroy();

  let committed = 'manual';
  let generation = 0;
  let controller = null;
  let destroyed = false;

  const render = ({ pending = false, failed = false } = {}) => {
    if (destroyed) return;
    if (label) label.textContent = pending ? `${LABELS[committed]}…` : LABELS[committed];
    button.classList.toggle('active', committed !== 'manual');
    button.classList.toggle('permission-mode-failed', failed);
    button.disabled = pending;
    button.setAttribute('aria-busy', String(pending));
    button.setAttribute('aria-label', `Permission mode: ${LABELS[committed]}`);
    button.setAttribute('title', `${TOASTS[committed]}`);
    button.dataset.permissionMode = committed;
  };

  const notify = (message, duration = 2400) => {
    try { if (typeof showToast === 'function') showToast(message, duration); } catch (_) {}
  };

  const read = async () => {
    const ticket = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    render({ pending: true });
    try {
      const response = await fetchImpl('/api/prefs/permission_mode', {
        credentials: 'same-origin',
        cache: 'no-store',
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`permission_mode_get_${response.status}`);
      const data = await response.json();
      if (destroyed || ticket !== generation) return;
      committed = normalize(data && data.value);
      render();
    } catch (error) {
      if (destroyed || ticket !== generation || error?.name === 'AbortError') return;
      committed = 'manual';
      render({ failed: true });
    }
  };

  const write = async (next) => {
    if (destroyed || button.disabled) return;
    const prior = committed;
    const ticket = ++generation;
    if (controller) controller.abort();
    controller = new AbortController();
    committed = normalize(next);
    render({ pending: true });
    try {
      const response = await fetchImpl('/api/prefs/permission_mode', {
        method: 'PUT',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ value: committed }),
        signal: controller.signal,
      });
      if (!response.ok) throw new Error(`permission_mode_put_${response.status}`);
      const data = await response.json();
      if (destroyed || ticket !== generation) return;
      committed = normalize(data && data.value);
      render();
      notify(TOASTS[committed], 2000);
    } catch (error) {
      if (destroyed || ticket !== generation || error?.name === 'AbortError') return;
      committed = prior;
      render({ failed: true });
      notify('Permission mode could not be saved; the previous mode remains active.', 3000);
    }
  };

  const onClick = () => {
    const index = MODES.indexOf(committed);
    void write(MODES[(index + 1) % MODES.length]);
  };
  button.addEventListener('click', onClick);
  button.__openClankPermissionControl = {
    destroy() {
      if (destroyed) return;
      destroyed = true;
      generation += 1;
      if (controller) controller.abort();
      button.removeEventListener('click', onClick);
      delete button.__openClankPermissionControl;
    },
  };
  render();
  void read();
  return button.__openClankPermissionControl;
}

export { LABELS, MODES, normalize };
export default initPermissionModeControl;
