// Shell entry bridge: keeps Usage lazy even when optional app modules fail to initialize.
let usagePromise = null;
let delegatedOpener = null;
function openUsage(options = {}) {
  if (typeof delegatedOpener === 'function') return delegatedOpener(options);
  usagePromise ||= import('./statsUsage.js');
  return usagePromise.then(module => module.openStatsUsage(options));
}
function bindUsageEntries() {
  for (const button of [document.getElementById('tool-usage-btn'), document.getElementById('rail-usage')].filter(Boolean)) {
    if (button.dataset.usageEntryBound === '1') continue;
    button.dataset.usageEntryBound = '1';
    button.addEventListener('click', openUsage);
    button.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); openUsage(); }
    });
  }
  if (location.pathname === '/usage') openUsage();
}
window.addEventListener('openclank:session-open', async event => {
  const handle = event.detail?.handle;
  if (typeof handle !== 'string' || !/^(session_[a-f0-9]{24}|[a-f0-9]{16})$/i.test(handle)) return;
  try {
    const response = await fetch('/api/stats/v1/sessions/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ handle }) });
    if (!response.ok) return;
    const data = await response.json();
    if (data.session_id && typeof window.sessionModule?.selectSession === 'function') await window.sessionModule.selectSession(data.session_id, { keepSidebar: true });
  } catch {}
});
window.addEventListener('openclank:open-usage', openUsage);
window.addEventListener('openclank:ui-control', event => {
  const detail = event.detail || {};
  const action = detail.action || detail.name || detail.panel || detail.value;
  if (detail.type === 'ui_control' && (action === 'usage' || action === 'stats' || detail.name === 'usage')) openUsage();
});
window.addEventListener('popstate', () => { if (location.pathname === '/usage') openUsage(); });
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindUsageEntries, { once: true });
else bindUsageEntries();
delegatedOpener = typeof window.__openStatsUsage === 'function' ? window.__openStatsUsage : null;
window.__openStatsUsage = openUsage;
