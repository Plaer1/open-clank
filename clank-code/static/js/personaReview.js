import { wireDialog } from './copal/overlays.js';

export async function reviewPersonaChange(id, current) {
  if (!current()) return;
  const dialog = document.createElement('dialog');
  dialog.className = 'copal-dialog';
  const element = (tag, text) => {
    const node = document.createElement(tag);
    node.textContent = text;
    dialog.append(node);
    return node;
  };
  element('h3', 'Review persona change');
  element('p', 'Use this observation to revise your default persona for chat, assistant and reminders.');
  const evidence = element('blockquote', '');
  const label = element('label', 'Proposed instructions');
  const draft = document.createElement('textarea');
  draft.maxLength = 10000;
  draft.rows = 8;
  label.append(draft);
  const status = element('p', 'Loading source and persona…');
  status.setAttribute('role','status');
  const save = element('button', 'Save revised persona');
  save.type = 'button';
  save.disabled = true;
  const close = element('button', 'Cancel');
  close.type = 'button';
  document.body.append(dialog);
  wireDialog(dialog);
  dialog.showModal();
  const controller = new AbortController();
  const options = {credentials:'same-origin',cache:'no-store',signal:controller.signal};
  const active = () => current() && dialog.isConnected && dialog.open;
  const retire = () => { if (dialog.open) dialog.close(); };
  close.addEventListener('click', retire);
  for (const event of ['workspace-change','openclank:auth-context-changed','openclank-account-changed','pagehide']) {
    for (const target of [window,document]) target.addEventListener(event,retire,{signal:controller.signal});
  }
  dialog.addEventListener('close', () => { controller.abort(); dialog.remove(); }, {once:true});
  async function source() {
    const response = await fetch('/api/memory/knowledge-snapshot/export/preview', {...options,method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({curated_id:id})});
    if (!response.ok) throw Error();
    const result = await response.json();
    if (!/^[a-f0-9]{64}$/.test(result.review_sha256) || typeof result.package?.knowledge?.text !== 'string') throw Error();
    return result;
  }
  try {
    const [observed,response] = await Promise.all([source(),fetch('/api/presets/default-persona',options)]);
    if (!response.ok) throw Error();
    const {name,system_prompt} = await response.json();
    if (!active()) return retire();
    evidence.textContent = `Memery record ${id} · ${observed.package.knowledge.text}`;
    draft.value = system_prompt + '\n\n' + observed.package.knowledge.text;
    status.textContent = 'Edit the proposed instructions before saving.';
    save.disabled = false;
    save.addEventListener('click', async () => {
      if (!active() || save.disabled) return;
      const proposed = draft.value.trim();
      if (!proposed || proposed.length > 10000) { status.textContent = 'Use 1 to 10,000 characters.'; return; }
      save.disabled = true;
      try {
        if ((await source()).review_sha256 !== observed.review_sha256) throw Error();
        if (!active()) return retire();
        const response = await fetch('/api/presets/default-persona',{...options,method:'PUT',
          headers:{'Content-Type':'application/json'},body:JSON.stringify({name,system_prompt:proposed,expected:{name,system_prompt}})});
        if (!response.ok) throw Error();
        const saved = await response.json();
        if (saved.success !== true) throw Error();
        if (!active()) return retire();
        window.dispatchEvent(new CustomEvent('default-persona-changed',{detail:saved}));
        status.textContent = 'Persona updated.';
        draft.readOnly = true;
        close.textContent = 'Close';
      } catch { if (active()) status.textContent = 'Could not confirm the update. Reopen this review to check the source and current persona.'; }
    });
  } catch { if (active()) status.textContent = 'Source or default persona unavailable.'; }
}
