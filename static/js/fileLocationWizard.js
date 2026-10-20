import { glyphIcon } from './langIcons.js';
import { topPortalZ } from './toolWindowZOrder.js';

let dialog = null;
let active = null;

function element(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'text') node.textContent = value;
    else if (key === 'class') node.className = value;
    else if (key.startsWith('on') && typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (value != null) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child != null) node.append(child.nodeType ? child : document.createTextNode(String(child)));
  return node;
}

function icon(name) {
  const node = element('span', { class: 'file-location-wizard-glyph', 'aria-hidden': 'true' });
  node.innerHTML = glyphIcon(name, 16, { className: 'files-glyph-svg' });
  return node;
}

function setStatus(message = '', error = false) {
  const status = dialog?.querySelector('[data-location-wizard-status]');
  if (!status) return;
  status.textContent = String(message || '');
  status.classList.toggle('error', Boolean(error));
}

function updateReview() {
  if (!dialog) return;
  const form = dialog.querySelector('form');
  const kind = form.elements.kind.value;
  const path = form.elements.path.value.trim();
  const caps = [form.elements.read.checked ? 'Browse / download' : '', form.elements.write.checked ? 'Modify' : ''].filter(Boolean);
  const agent = form.elements.agent.checked ? 'Agents may use it' : 'Agents cannot use it';
  const label = kind === 'whole_root' ? 'Whole disk / volume' : kind === 'exact_file' ? 'Exact file' : 'Folder and descendants';
  dialog.querySelector('[data-location-wizard-review]').textContent = `${label} · ${path || 'Choose a host path'} · ${caps.join(' + ') || 'No access selected'} · ${agent}`;
}

function chooseKind(kind) {
  const form = dialog.querySelector('form');
  form.elements.kind.value = kind;
  dialog.querySelectorAll('[data-location-kind]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.locationKind === kind));
  });
  const browse = dialog.querySelector('[data-location-browse]');
  if (browse) browse.disabled = kind === 'whole_root';
  updateReview();
}

function close(result = false) {
  if (!dialog || dialog.hidden) return;
  dialog.hidden = true;
  const resolve = active?.resolve;
  const restore = active?.restore;
  active = null;
  restore?.focus?.();
  resolve?.(result);
}

function mount() {
  if (dialog) return dialog;
  const form = element('form', { class: 'file-location-wizard-card' });
  const heading = element('h2', { id: 'file-location-wizard-title', text: 'Add host Location' });
  const intro = element('p', {
    class: 'file-location-wizard-copy',
    text: 'Choose what Open Clank may see on its host. Adding a Location does not make it the default folder or share it with another person.',
  });
  const kindGrid = element('div', { class: 'file-location-wizard-kinds', role: 'group', 'aria-label': 'Location type' });
  for (const [kind, glyph, label, description] of [
    ['directory', 'folder', 'Folder', 'This folder and its descendants'],
    ['exact_file', 'file', 'Exact file', 'One file; no parent listing'],
    ['whole_root', 'volume', 'Whole disk', 'The OS decides what remains inaccessible'],
  ]) {
    const button = element('button', {
      type: 'button',
      class: 'file-location-wizard-kind',
      'data-location-kind': kind,
      'aria-pressed': 'false',
      onclick: async () => {
        chooseKind(kind);
        if (kind !== 'whole_root' || !active?.resolveWholeRoot) return;
        setStatus('Resolving host volume…');
        try {
          const path = await active.resolveWholeRoot();
          if (!active || !path) throw new Error('Host volume is unavailable');
          form.elements.path.value = path;
          setStatus('');
          updateReview();
        } catch (error) { setStatus(error.message || 'Host volume is unavailable', true); }
      },
    });
    button.append(icon(glyph), element('span', { class: 'file-location-wizard-kind-copy' },
      element('strong', { text: label }), element('small', { text: description })));
    kindGrid.append(button);
  }
  const kindInput = element('input', { type: 'hidden', name: 'kind', value: 'directory' });
  const pathInput = element('input', {
    name: 'path', type: 'text', autocomplete: 'off', spellcheck: 'false', required: 'true',
    class: 'settings-select file-location-wizard-path', placeholder: '/path/on-the-open-clank-host',
    'aria-label': 'Host path',
  });
  const home = element('button', { type: 'button', class: 'admin-btn-sm', 'data-location-home': '', text: 'Home' });
  const browse = element('button', { type: 'button', class: 'admin-btn-sm', 'data-location-browse': '', text: 'Browse host folders…' });
  const pathRow = element('div', { class: 'file-location-wizard-path-row' }, pathInput, home, browse);
  const access = element('fieldset', { class: 'file-location-wizard-access' },
    element('legend', { text: 'Access' }),
    element('label', {}, element('input', { type: 'checkbox', name: 'read', checked: 'true' }), ' Browse / download'),
    element('label', {}, element('input', { type: 'checkbox', name: 'write' }), ' Modify'),
    element('label', {}, element('input', { type: 'checkbox', name: 'agent', checked: 'true' }), ' Agents may use this Location'));
  const agentNote = element('p', {
    class: 'file-location-wizard-copy',
    text: 'Agent access is explicit and can still be narrowed per workspace or chat. Whole disk delegates file safety to the host OS.',
  });
  const review = element('div', { class: 'file-location-wizard-review', 'data-location-wizard-review': '', 'aria-live': 'polite' });
  const status = element('div', { class: 'file-location-wizard-status', 'data-location-wizard-status': '', role: 'status', 'aria-live': 'polite' });
  const cancel = element('button', { type: 'button', class: 'admin-btn-sm', text: 'Cancel', onclick: () => close(false) });
  const submit = element('button', { type: 'submit', class: 'admin-btn-add', 'data-location-submit': '', text: 'Add Location' });
  form.append(heading, intro, kindGrid, kindInput, pathRow, access, agentNote, review, status,
    element('div', { class: 'file-location-wizard-actions' }, cancel, submit));
  dialog = element('div', {
    class: 'file-location-wizard-backdrop', hidden: 'true', role: 'presentation',
  }, element('section', {
    role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': 'file-location-wizard-title',
  }, form));
  dialog.addEventListener('pointerdown', event => { if (event.target === dialog) close(false); });
  dialog.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    close(false);
  });
  pathInput.addEventListener('input', updateReview);
  access.addEventListener('change', updateReview);
  home.addEventListener('click', async () => {
    if (!active?.resolveHome) return;
    setStatus('Resolving host home…');
    try {
      const path = await active.resolveHome();
      if (!active || !path) throw new Error('Host home is unavailable');
      chooseKind('directory');
      pathInput.value = path;
      setStatus('');
      updateReview();
      pathInput.focus();
    } catch (error) { setStatus(error.message || 'Host home is unavailable', true); }
  });
  browse.addEventListener('click', async () => {
    if (!active?.browseFolder) return;
    try {
      const selected = await active.browseFolder(pathInput.value.trim());
      if (!active || !selected) return;
      chooseKind('directory');
      pathInput.value = selected;
      setStatus('');
      updateReview();
    } catch (error) { setStatus(error.message || 'Host folder could not be selected', true); }
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!active?.submit) return;
    const path = pathInput.value.trim();
    const capabilities = [form.elements.read.checked ? 'read' : '', form.elements.write.checked ? 'write' : ''].filter(Boolean);
    if (!path || !capabilities.length) {
      setStatus('Enter a host path and select at least one access capability.', true);
      return;
    }
    submit.disabled = true;
    cancel.disabled = true;
    setStatus('Adding Location…');
    try {
      const result = await active.submit({
        path,
        kind: form.elements.kind.value,
        capabilities,
        agentAccess: form.elements.agent.checked,
      });
      close(result || true);
    } catch (error) {
      setStatus(error.message || 'Location could not be added', true);
    } finally {
      submit.disabled = false;
      cancel.disabled = false;
    }
  });
  document.body.append(dialog);
  chooseKind('directory');
  return dialog;
}

export function openFileLocationWizardDialog({
  suggestedPath = '',
  resolveHome = null,
  resolveWholeRoot = null,
  browseFolder = null,
  submit,
} = {}) {
  if (active) close(false);
  mount();
  const form = dialog.querySelector('form');
  form.reset();
  form.elements.read.checked = true;
  form.elements.agent.checked = true;
  form.elements.path.value = String(suggestedPath || '');
  chooseKind('directory');
  setStatus('');
  updateReview();
  dialog.style.zIndex = String(topPortalZ({ exclude: dialog }));
  dialog.hidden = false;
  const restore = document.activeElement;
  return new Promise(resolve => {
    active = { resolve, restore, resolveHome, resolveWholeRoot, browseFolder, submit };
    requestAnimationFrame(() => form.elements.path.focus());
  });
}

export function closeFileLocationWizardDialog() { close(false); }

export default { openFileLocationWizardDialog, closeFileLocationWizardDialog };
