// Explicit owner-scoped inventory; this panel has no deletion authority.
export function mountStorageInventory(root) {
  let controller = null, epoch = 0;
  const account = () => document.body?.dataset?.accountId || window.currentAccountId || '';
  const title = document.createElement('h3'); title.textContent = 'Storage inventory';
  const hint = document.createElement('p'); hint.className = 'settings-hint'; hint.textContent = 'Recorded sizes are not physical disk usage. Capture cleanup is available in the retention controls above.';
  const button = document.createElement('button'); button.type = 'button'; button.className = 'admin-btn-sm'; button.textContent = 'Refresh storage inventory';
  const output = document.createElement('div'); output.className = 'logging-source'; output.textContent = 'Not measured.';
  root.replaceChildren(title, hint, button, output);
  const clear = () => { epoch++; controller?.abort(); controller = null; output.textContent = 'Not measured.'; };
  const render = value => {
    const capture = value.capture || {}, artifacts = value.artifacts || {};
    const rows = [
      ['As of', value.as_of || 'unknown'],
      ['Capture bodies, uncompressed recorded bytes', capture.body_uncompressed_bytes],
      ['Capture events, serialized bytes', capture.event_serialized_bytes],
      ['Capture metadata records', capture.metadata_records],
      ['Capture metadata, estimated bytes', capture.metadata_estimated_bytes],
      ['Pending captures', capture.pending_captures],
      ['Owner artifacts, staged records / recorded bytes', `${artifacts.owner?.staged?.records ?? 'unknown'} / ${artifacts.owner?.staged?.recorded_bytes ?? 'unknown'}`],
      ['Owner artifacts, acknowledged records / recorded bytes', `${artifacts.owner?.acknowledged?.records ?? 'unknown'} / ${artifacts.owner?.acknowledged?.recorded_bytes ?? 'unknown'}`],
      ['Account media, staged records / recorded bytes', `${artifacts.account_media?.staged?.records ?? 'unknown'} / ${artifacts.account_media?.staged?.recorded_bytes ?? 'unknown'}`],
      ['Account media, acknowledged records / recorded bytes', `${artifacts.account_media?.acknowledged?.records ?? 'unknown'} / ${artifacts.account_media?.acknowledged?.recorded_bytes ?? 'unknown'}`],
      ['Physical unique disk usage', 'not measured'],
      ['Files and vault, Lore, chat archive, native memory and code index', 'not measured'],
    ];
    output.replaceChildren(...rows.map(([label, amount]) => { const row = document.createElement('p'); row.textContent = `${label}: ${amount ?? 'unknown'}`; return row; }));
  };
  const refresh = async () => {
    clear(); const serial = epoch, originalAccount = account();
    if (!originalAccount) { output.textContent = 'Account unavailable.'; return; }
    controller = new AbortController(); output.textContent = 'Measuring…';
    try {
      const response = await fetch('/api/diagnostics/storage', {credentials:'same-origin', cache:'no-store', signal:controller.signal});
      if (!response.ok) throw new Error('Storage inventory unavailable');
      const value = await response.json();
      if (serial !== epoch || originalAccount !== account() || !root.isConnected) return;
      render(value);
    } catch (error) {
      if (serial === epoch && originalAccount === account() && error.name !== 'AbortError' && root.isConnected) output.textContent = 'Storage inventory unavailable.';
    }
  };
  button.addEventListener('click', refresh);
  document.addEventListener('openclank:auth-context-changed', clear);
  window.addEventListener('openclank:auth-context-changed', clear);
  return () => { clear(); button.removeEventListener('click', refresh); document.removeEventListener('openclank:auth-context-changed', clear); window.removeEventListener('openclank:auth-context-changed', clear); };
}
