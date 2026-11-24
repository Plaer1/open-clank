import { startProductPicker } from './buddyExplain.js';
import { matchAnnotatorTarget } from './annotatorTargets.js';
import { wireDialog } from './copal/overlays.js';
import Storage from './storage.js';
import { topPortalZ } from './toolWindowZOrder.js';

let account = '';
let epoch = 0;
let available = false;
let control = null;
let presentation = null;
let availabilityRequest = null;

const MAX_COMMENT = 4000;
const preferenceKey = owner => `openclank-annotator:scope:${encodeURIComponent(owner)}`;

function node(tag, label = '') {
  const element = document.createElement(tag);
  if (label) element.textContent = label;
  return element;
}

function button(label, type = 'button') {
  const element = node('button', label);
  element.type = type;
  return element;
}

function loadStyles() {
  if (document.getElementById('openclank-buddy-style')) return;
  const link = node('link');
  link.id = 'openclank-buddy-style';
  link.rel = 'stylesheet';
  link.href = '/static/css/interactive-buddy.css';
  document.head.append(link);
}

function submissionId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = bytes[6] & 0x0f | 0x40;
  bytes[8] = bytes[8] & 0x3f | 0x80;
  const hex = Array.from(bytes, byte=>byte.toString(16).padStart(2,'0')).join('');
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

function openFeedback(item, opener, lifetime, isCurrent) {
  if (!isCurrent()) return;
  const dialog = node('dialog');
  dialog.id = 'openclank-annotator-feedback';
  dialog.className = 'copal-dialog annotator-feedback';
  dialog.setAttribute('aria-labelledby','annotator-feedback-title');
  const heading = node('h2','Feedback');
  heading.id = 'annotator-feedback-title';
  const target = node('p',item.label);
  target.id = 'annotator-feedback-target';
  dialog.setAttribute('aria-describedby',target.id);

  const form = node('form');
  const label = node('label','Comment');
  const comment = node('textarea');
  comment.rows = 5;
  comment.required = true;
  comment.maxLength = MAX_COMMENT;
  label.append(comment);
  const status = node('p');
  status.setAttribute('role','status');
  const actions = node('footer');
  actions.className = 'copal-dialog-actions';
  const cancel = button('Cancel');
  const save = button('Save','submit');
  cancel.className = 'copal-btn';
  save.className = 'copal-btn primary';
  actions.append(cancel,save);
  form.append(label,status,actions);
  dialog.append(heading,target,form);
  wireDialog(dialog,{restoreFocus:false});

  let attempt = null;
  let pending = null;
  let conflict = false;
  let highlighted = false;
  const abort = () => dialog.close();
  cancel.addEventListener('click',abort);
  dialog.addEventListener('close',()=>{
    lifetime.removeEventListener('abort',abort);
    pending?.abort();
    if (highlighted) item.element.classList.remove('bound-buddy-target');
    if (opener.isConnected) opener.focus({preventScroll:true});
  },{once:true});
  lifetime.addEventListener('abort',abort,{once:true});

  form.addEventListener('submit',async event=>{
    event.preventDefault();
    if (pending || conflict || !isCurrent() || !dialog.open) return;
    const text = comment.value.trim();
    if (!text || text.length > MAX_COMMENT) {
      status.textContent = 'Enter a comment of up to 4,000 characters.';
      comment.focus();
      return;
    }
    const request = new AbortController();
    attempt ||= {submission_id:submissionId(),page:item.page,element:item.id,comment:text};
    pending = request;
    save.disabled = true;
    comment.readOnly = true;
    status.textContent = 'Saving feedback…';
    try {
      const response = await fetch('/api/annotator/feedback',{
        method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},
        signal:request.signal,body:JSON.stringify(attempt),
      });
      const result = await response.json().catch(()=>null);
      if (!isCurrent() || !dialog.open) return;
      if (response.ok && result?.submission_id === attempt.submission_id
          && ['recorded','already_recorded'].includes(result.status)) {
        dialog.close();
        window.uiModule?.showToast?.('Feedback saved.');
        return;
      }
      if ([400,413,422].includes(response.status)) {
        attempt = null;
        comment.readOnly = false;
        save.textContent = 'Save';
        status.textContent = 'Feedback was rejected. Review the comment and save again.';
      } else if (response.status === 409) {
        conflict = true;
        status.textContent = 'This feedback ID is already in use. Close this box and select the control again.';
      } else {
        throw new Error('Feedback receipt unavailable');
      }
    } catch {
      if (request.signal.aborted || !isCurrent() || !dialog.open) return;
      status.textContent = 'The save could not be confirmed. Retry to check the same feedback.';
      save.textContent = 'Retry';
    } finally {
      pending = null;
      if (isCurrent() && dialog.open && !conflict) save.disabled = false;
    }
  });
  document.body.append(dialog);
  if (!item.element.classList.contains('bound-buddy-target')) {
    item.element.classList.add('bound-buddy-target');
    highlighted = true;
  }
  dialog.showModal();
  comment.focus();
}

function createPresentation(owner) {
  const lifetime = new AbortController();
  const {signal} = lifetime;
  const root = node('aside');
  root.className = 'bound-buddy openclank-annotator';
  root.setAttribute('aria-label','Annotator');
  const launcher = button();
  launcher.className = 'bound-buddy-play annotator-launcher';
  launcher.setAttribute('aria-label','Annotator actions');
  launcher.setAttribute('aria-expanded','false');
  launcher.setAttribute('aria-controls','annotator-actions');
  const emoji = node('span','🤓');
  emoji.setAttribute('aria-hidden','true');
  launcher.append(emoji);
  const actions = node('div');
  actions.className = 'bound-buddy-actions';
  actions.id = 'annotator-actions';
  actions.hidden = true;
  const feedback = button('Feedback');
  actions.append(feedback);
  root.append(launcher,actions);

  const isCurrent = () => !signal.aborted && account === owner && document.body.dataset.accountId === owner;
  const raise = () => { root.style.zIndex = topPortalZ(); };
  const showActions = show => {
    actions.hidden = !show;
    launcher.setAttribute('aria-expanded',String(show));
  };
  let cancelPicking = null;
  launcher.addEventListener('click',event=>{
    raise();
    if (root.classList.contains('bound-buddy-picking')) {
      cancelPicking?.();
      launcher.focus();
      return;
    }
    showActions(actions.hidden);
    if (!actions.hidden && !event.detail) feedback.focus();
  },{signal});
  feedback.addEventListener('click',()=>{
    showActions(false);
    cancelPicking?.();
    cancelPicking = startProductPicker({
      root,opener:launcher,signal,matchTarget:matchAnnotatorTarget,
      initial:'Select the pane or control you want to annotate. Escape cancels.',
      onSelect:item=>openFeedback(item,launcher,signal,isCurrent),
    });
  },{signal});
  root.addEventListener('keydown',event=>{
    if (event.key !== 'Escape' || actions.hidden) return;
    event.stopPropagation();
    showActions(false);
    launcher.focus();
  },{signal});
  document.addEventListener('pointerdown',event=>{
    raise();
    if (!root.contains(event.target)) showActions(false);
  },{signal,capture:true});
  document.body.append(root);
  raise();
  return {remove:()=>{ lifetime.abort(); root.remove(); }};
}

function render() {
  const enabled = available && !!account;
  const show = enabled && Storage.getJSON(preferenceKey(account),false) === true;
  if (control) {
    control.disabled = !enabled;
    control.checked = show;
    control.closest('[data-annotator-settings]').hidden = !enabled;
  }
  if (show && !presentation) {
    loadStyles();
    presentation = createPresentation(account);
  } else if (!show && presentation) {
    presentation.remove();
    presentation = null;
  }
}

async function checkAvailability() {
  if (!account || availabilityRequest) return;
  const request = new AbortController();
  availabilityRequest = request;
  try {
    const response = await fetch('/api/annotator/availability',{
      credentials:'same-origin',cache:'no-store',signal:request.signal,
    });
    const state = response.ok ? await response.json() : null;
    if (!request.signal.aborted) available = state?.available === true;
  } catch {
    if (!request.signal.aborted) available = false;
  } finally {
    if (availabilityRequest === request) {
      availabilityRequest = null;
      render();
    }
  }
}

function adoptAccount(detail) {
  const owner = typeof detail?.accountId === 'string' ? detail.accountId : '';
  if (owner === account) return;
  availabilityRequest?.abort();
  availabilityRequest = null;
  presentation?.remove();
  presentation = null;
  account = owner;
  available = false;
  render();
  void checkAvailability();
}

export function mountAnnotatorSettings(toggle) {
  if (!toggle) return;
  if (control !== toggle) {
    control = toggle;
    toggle.addEventListener('change',()=>{
      if (available && account) Storage.setJSON(preferenceKey(account),toggle.checked);
      render();
    });
  }
  render();
  if (!available) void checkAvailability();
}

if (typeof document !== 'undefined' && typeof window !== 'undefined') {
  document.addEventListener('openclank:auth-context-changed',event=>{
    epoch++;
    adoptAccount(event.detail);
  });
  document.addEventListener('click',event=>{
    if (!event.target.closest?.('#settings-logout-btn')) return;
    epoch++;
    adoptAccount(null);
  },{capture:true});
  window.addEventListener('pagehide',()=>{
    epoch++;
    adoptAccount(null);
  });
  window.addEventListener('pageshow',event=>{
    if (!event.persisted) return;
    const revision = epoch;
    void fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store'})
      .then(response=>response.ok ? response.json() : null)
      .then(data=>{ if (revision === epoch) adoptAccount({accountId:data?.account_id}); })
      .catch(()=>{});
  });
  window.addEventListener('storage',event=>{
    if (account && event.key === preferenceKey(account)) render();
  });
  const revision = epoch;
  window.__odysseusAuthContextPromise?.then(detail=>{
    if (revision === epoch) adoptAccount(detail);
  }).catch(()=>{});
}
