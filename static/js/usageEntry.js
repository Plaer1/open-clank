// Shell entry bridge: keeps Usage lazy even when optional app modules fail to initialize.
let usagePromise = null;
let delegatedOpener = null;
function openUsage(options = {}) {
  if (options instanceof Event) options = options.detail || {};
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
window.addEventListener('openclank:session-open', event => {
  const detail = event.detail || {};
  const reject = typeof detail.reject === 'function' ? detail.reject : error => console.error('Unable to open saved conversation:', error);
  void (async () => {
    const handle = detail.handle;
    if (typeof handle !== 'string' || !/^(session_[a-f0-9]{24}|[a-f0-9]{16})$/i.test(handle)) throw new Error('The saved conversation handle is invalid.');
    const response = await fetch('/api/stats/v1/sessions/open', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ handle }) });
    let data = {};
    try { data = await response.json(); } catch {}
    if (!response.ok) {
      const detailMessage = data.detail?.message || data.detail?.error || data.detail;
      throw new Error(typeof detailMessage === 'string' ? detailMessage : `Session open request failed (${response.status}).`);
    }
    if (!data.session_id) throw new Error('This saved conversation has no openable host Session.');
    const selectSession = window.sessionModule?.selectSession;
    if (typeof selectSession !== 'function') throw new Error('The host session selector is unavailable.');
    const result = await selectSession(data.session_id, { keepSidebar: true });
    if (result === false) throw new Error('The host session selector could not open this conversation.');
    if (typeof detail.resolve === 'function') detail.resolve(result);
  })().catch(error => reject(error instanceof Error ? error : new Error(String(error))));
});
window.addEventListener('openclank:open-usage', openUsage);
window.addEventListener('openclank:ui-control', event => {
  const detail = event.detail || {};
  const action = detail.action || detail.name || detail.panel || detail.value;
  if (detail.type === 'ui_control' && (action === 'usage' || action === 'stats' || detail.name === 'usage')) openUsage();
});
window.addEventListener('popstate', () => { if (location.pathname === '/usage' && !document.getElementById('stats-usage-window')) openUsage(); });
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', bindUsageEntries, { once: true });
else bindUsageEntries();
delegatedOpener = typeof window.__openStatsUsage === 'function' ? window.__openStatsUsage : null;
window.__openStatsUsage = openUsage;
