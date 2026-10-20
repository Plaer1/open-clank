// Explicit Settings read/save only; never starts or polls an indexing job.
export function parseMaxThreads(value) {
  if (typeof value !== 'string' || !/^(?:[1-9]|[12][0-9]|3[0-2])$/.test(value)) return null;
  return Number(value);
}

const integer = value => Number.isSafeInteger(value) && value >= 0 ? String(value) : 'unknown';
export function schedulerSummary(data) {
  const scheduler = data?.code_index_scheduler;
  if (!scheduler || !['available', 'unavailable'].includes(scheduler.native_status)
      || !['pending', 'draining', 'applied'].includes(scheduler.status)) return 'Indexing status unavailable. Refresh to try again.';
  const desired = Number.isInteger(data.code_index_max_threads) && data.code_index_max_threads >= 1 && data.code_index_max_threads <= 32
    ? data.code_index_max_threads : 'unknown';
  return `Saved/requested: ${desired}. ${scheduler.status === 'applied' ? 'Applied' : scheduler.status === 'draining' ? 'Draining active work before applying' : 'Pending application'}. Native: ${scheduler.native_status}. Effective: ${integer(scheduler.effective)}; active: ${integer(scheduler.active)}; queued: ${integer(scheduler.queued)}. Revision: ${integer(data.code_index_settings_revision)}; native revision: ${integer(scheduler.native_revision)}; effective revision: ${integer(scheduler.effective_revision)}. Drain pending: ${scheduler.drain_pending === true ? 'yes' : scheduler.drain_pending === false ? 'no' : 'unknown'}.`;
}

export function mountCodeIndexSettings(root, {fetcher = globalThis.fetch} = {}) {
  if (!root || root.dataset.codeIndexMounted) return;
  root.dataset.codeIndexMounted = '1';
  const input = root.querySelector('[data-code-index-threads]');
  const save = root.querySelector('[data-code-index-save]');
  const refresh = root.querySelector('[data-code-index-refresh]');
  const status = root.querySelector('[data-code-index-settings-status]');
  let busy = false;
  let forbidden = false;
  const controls = () => { input.disabled = busy || forbidden; save.disabled = busy || forbidden; refresh.disabled = busy; };
  const request = async (method) => {
    if (busy) return;
    const value = method === 'POST' ? parseMaxThreads(input.value) : null;
    if (method === 'POST' && value === null) {
      status.textContent = 'Enter a whole number from 1 to 32.';
      input.setAttribute('aria-invalid', 'true'); input.focus(); return;
    }
    input.removeAttribute('aria-invalid');
    busy = true; controls();
    status.textContent = method === 'POST' ? 'Saving indexing preference…' : 'Reading indexing status…';
    try {
      const response = await fetcher('/api/auth/settings', {
        method, credentials: 'same-origin',
        ...(method === 'POST' ? {headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code_index_max_threads: value})} : {}),
      });
      if (!response.ok) {
        if (response.status === 403) forbidden = true;
        status.textContent = response.status === 403 ? 'Only an administrator can change this app-wide preference.'
          : response.status === 401 ? 'Sign in to read indexing settings.'
            : response.status === 400 ? 'The indexing preference was rejected. Refresh and enter a whole number from 1 to 32.'
              : 'Indexing settings unavailable. Refresh to try again.';
        return;
      }
      const data = await response.json();
      if (Number.isInteger(data.code_index_max_threads) && data.code_index_max_threads >= 1 && data.code_index_max_threads <= 32) input.value = String(data.code_index_max_threads);
      status.textContent = schedulerSummary(data);
    } catch (_) { status.textContent = 'Indexing settings unavailable. Refresh to try again.'; }
    finally { busy = false; controls(); }
  };
  save.addEventListener('click', () => void request('POST'));
  refresh.addEventListener('click', () => void request('GET'));
  input.addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); void request('POST'); } });
  void request('GET');
}
