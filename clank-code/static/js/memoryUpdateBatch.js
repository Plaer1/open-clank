import { wireDialog } from './copal/overlays.js';

const node = (tag, text = '', className = '') => { const n = document.createElement(tag); n.textContent = text; if (className) n.className = className; return n; };
const PATH = '/value/value';

// One review dialog for 1..100 selected memories: edit text, review Before/After per row, apply all in one request.
// Item binding values come from the same per-memory mirror-context the single edit uses.
export async function openMemoryUpdateBatch({ memories, onDone, notify }) {
  const say = typeof notify === 'function' ? notify : () => {};
  const rows = memories.slice(0, 100).map(m => ({ id: String(m.id), before: m.raw_text || m.text || '', draft: m.raw_text || m.text || '' }));
  if (!rows.length) return;
  const dialog = node('dialog', '', 'copal-dialog memory-update-batch-dialog');
  const title = node('h3', `Edit ${rows.length} selected ${rows.length === 1 ? 'memory' : 'memories'}`);
  const notice = node('p', 'Change the text, then review every Before and After. All changes apply together or not at all.', 'admin-toggle-sub');
  const status = node('p', '', 'admin-toggle-sub'); status.setAttribute('role', 'status');
  const list = node('div', '', 'memory-update-batch-list');
  const actions = node('div', '', 'copal-dialog-actions');
  const cancel = node('button', 'Cancel'), review = node('button', 'Review changes'), applyAll = node('button', 'Apply all'), check = node('button', 'Check status');
  for (const b of [cancel, review, applyAll, check]) { b.type = 'button'; b.className = 'memory-toolbar-btn'; }
  applyAll.hidden = true; check.hidden = true;
  actions.append(cancel, check, review, applyAll);
  dialog.append(title, notice, status, list, actions);
  document.body.append(dialog);
  wireDialog(dialog);
  let busy = false, prepared = null, pending = null;
  const setBusy = value => { busy = value; for (const b of [cancel, review, applyAll, check]) b.disabled = value && b !== cancel; };
  const editors = [];
  for (const row of rows) {
    const wrap = node('div', '', 'memory-update-batch-row');
    const area = node('textarea'); area.rows = 2; area.value = row.draft; area.setAttribute('aria-label', 'Memory text');
    area.addEventListener('input', () => { row.draft = area.value; prepared = null; applyAll.hidden = true; });
    wrap.append(area); list.append(wrap); editors.push({ row, wrap, area });
  }
  dialog.addEventListener('close', () => dialog.remove(), { once: true });
  cancel.addEventListener('click', () => dialog.close());
  const json = async (url, init) => {
    const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', ...init });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  };
  const finish = async count => { say(`${count} ${count === 1 ? 'memory' : 'memories'} updated`); dialog.close(); await onDone?.(); };

  review.addEventListener('click', async () => {
    if (busy) return;
    const changed = rows.filter(r => r.draft.trim() && r.draft !== r.before);
    if (!changed.length) { status.textContent = 'Nothing has changed yet.'; return; }
    setBusy(true); status.textContent = 'Preparing the review…'; applyAll.hidden = true; prepared = null;
    try {
      const items = [];
      for (const row of changed) {
        const ctx = await json(`/api/memory/${encodeURIComponent(row.id)}/mirror-context`);
        if (!ctx.ok || ctx.data.mode !== 'guarded') throw new Error('Editing several memories at once needs the guarded memory store.');
        items.push({ memory_id: row.id, candidate_id: ctx.data.candidate_id, origin_request_key: ctx.data.origin_request_key,
          prior_receipt_id: ctx.data.prior_receipt_id, expected_revision: ctx.data.expected_revision, selected_paths: [PATH],
          payload: { value: { type: 'string', value: row.draft } } });
      }
      const preview = await json('/api/memory/update-batch/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) });
      if (!preview.ok || preview.data.state !== 'preview' || !Array.isArray(preview.data.items) || preview.data.items.length !== items.length) throw new Error('The review is unavailable. Nothing was changed.');
      for (const editor of editors) editor.wrap.querySelector('.memory-update-batch-diff')?.remove();
      preview.data.items.forEach((entry, index) => {
        const row = changed[index];
        const change = Array.isArray(entry.changes) ? entry.changes.find(c => c.path === PATH) : null;
        if (entry.memory_id !== row.id || !change || typeof change.before?.value !== 'string' || change.after?.value !== row.draft) throw new Error('The review does not match your edit. Nothing was changed.');
        const diff = node('div', '', 'memory-update-batch-diff');
        diff.append(node('div', `Before: ${change.before.value}`), node('div', `After: ${change.after.value}`));
        editors.find(e => e.row === row).wrap.append(diff);
      });
      prepared = { items, preview_sha256: preview.data.preview_sha256, request_key: crypto.randomUUID() };
      applyAll.hidden = false; status.textContent = `Review ${items.length} ${items.length === 1 ? 'change' : 'changes'}, then choose Apply all.`;
    } catch (error) { status.textContent = error.message || 'The review is unavailable.'; }
    finally { setBusy(false); }
  });

  applyAll.addEventListener('click', async () => {
    if (busy || !prepared) return;
    setBusy(true); pending = prepared; status.textContent = 'Applying…';
    try {
      const result = await json('/api/memory/update-batch/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(pending) });
      if (result.ok && result.data.state === 'committed') { await finish(pending.items.length); return; }
      status.textContent = 'The batch was not applied (a memory may have changed since you opened this, or it is unavailable). Use Check status to see whether anything was recorded.';
    } catch { status.textContent = 'The outcome is not known. Use Check status; do not apply again.'; }
    check.hidden = false; applyAll.hidden = true; setBusy(false);
  });

  check.addEventListener('click', async () => {
    if (busy || !pending) return;
    setBusy(true);
    try {
      const q = new URLSearchParams({ request_key: pending.request_key, preview_sha256: pending.preview_sha256 });
      const result = await json(`/api/memory/update-batch/status?${q}`);
      if (result.ok && result.data.state === 'committed') { await finish(pending.items.length); return; }
      status.textContent = result.ok && result.data.state === 'unknown' ? 'No batch was recorded for this review. Nothing was changed; you can review again.' : 'The status is unavailable. Nothing is assumed.';
      if (result.ok && result.data.state === 'unknown') { check.hidden = true; pending = null; prepared = null; }
    } catch { status.textContent = 'The status is unavailable.'; }
    finally { setBusy(false); }
  });
  dialog.showModal();
}
