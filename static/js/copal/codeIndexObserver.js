// One disposable subscriber to the existing index owner; never query-side admission.
export function indexPresentation(index = {}) {
  const lifecycle = String(index.lifecycle || 'unavailable');
  const pending = ['starting', 'admitted', 'queued', 'building', 'running'].includes(lifecycle);
  const usable = Boolean(index.head_generation) && ['ready', 'failed'].includes(lifecycle);
  const partial = index.freshness === 'partial';
  const recovering = lifecycle === 'unavailable' && index.recovering === true;
  const progress = index.progress || {};
  const detail = index.error?.message ? ` ${index.error.message}` : '';
  const code = index.error?.code ? ` (${index.error.code})` : '';
  const messages = {
    starting:'Starting code index', admitted:'Starting code index', queued:'Code index queued',
    building:'Indexing code', running:'Indexing code',
    ready:partial ? 'Ready with partial coverage; indexed evidence is not verified-current source' : 'Code index ready; source actions revalidate current bytes',
    failed:usable ? 'Rebuild failed; previous indexed head remains available and is not current' : 'Code indexing failed; no usable head',
    missing:'Code index is unindexed; retry indexing or choose another folder',
    erased:'Code index was erased; only explicit readmission can index this folder again',
    denied:'Workspace access denied or revoked; choose an authorized folder',
    cancelled:'Code indexing was cancelled; retry explicitly or choose another folder',
    unavailable:recovering ? 'Code index status temporarily unavailable; rechecking observation' : 'Code index provider unavailable; recheck or choose another folder',
  };
  const completed = Number(progress.completed_files), total = Number(progress.total_files);
  const progressText = pending && Number.isFinite(completed) && Number.isFinite(total) && total > 0
    ? ` · ${completed}/${total} files` : '';
  return { lifecycle, pending, usable, partial, recovering,
    text:(messages[lifecycle] || `Code index ${lifecycle}`) + progressText + code + detail };
}

export function observeCodeIndex({ request, isCurrent = () => true, onStatus = () => {},
  onDispose = () => {},
  observe = true, schedule = (fn, delay) => setTimeout(fn, delay), unschedule = clearTimeout,
  initialDelay = 1000, maxDelay = 10000 }) {
  let disposed = false, timer = null, inFlight = false, delay = initialDelay, latest = null;
  let pendingObserved = false;
  const lifecycles = new Set(['starting','admitted','queued','building','running','ready','failed','missing','erased','denied','cancelled','unavailable']);
  const validStatus = index => index && typeof index === 'object' && !Array.isArray(index) && lifecycles.has(index.lifecycle);
  const transientStatusError = error => !['denied','revoked','access_revoked','permission_denied','erased','cancelled','failed','invalid_response'].includes(error?.code)
    && ([408,429,502,503,504].includes(error?.status) || (error instanceof TypeError && error.status == null));
  const releaseRequest = () => { void Promise.resolve().then(() => request('release')).catch(() => {}); };
  const release = () => {
    if (disposed) return;
    disposed = true;
    if (timer !== null) unschedule(timer);
    timer = null;
    releaseRequest();
    onDispose();
  };
  const active = () => {
    if (!disposed && !isCurrent()) release();
    return !disposed;
  };
  const errorStatus = error => ({ lifecycle:[401,403,404].includes(error.status) ? 'denied' : 'unavailable',
    error:{code:error.code || 'index_unavailable',message:error.message || 'Code index unavailable'} });
  async function perform(event) {
    if (!active() || inFlight) return;
    inFlight = true;
    let index, failed = false;
    try {
      try { index = await request(event); }
      catch (error) {
        // An erased ensure is rejected. Status distinguishes its tombstone
        // without re-admitting or issuing a second ensure.
        if (event === 'open' && error.status === 409) index = await request('status');
        else throw error;
      }
    } catch (error) {
      failed = true;
      index = errorStatus(error);
      // Only observation of a previously witnessed pending build may recover.
      // Keep the same subscription; never repeat open/admit or infer a head.
      if (event === 'status' && pendingObserved && transientStatusError(error)) {
        // A transport outage is not a native job deadline. Continue at capped
        // backoff until a real status, revocation, scope loss or disposal.
        index.recovering = true;
      } else pendingObserved = false;
    }
    finally { inFlight = false; }
    if (!active()) {
      // Disposal can reach the owner before the in-flight open/admit does.
      if (event === 'open' || event === 'admit') releaseRequest();
      return;
    }
    if (!validStatus(index)) {
      index = {lifecycle:'unavailable',error:{code:'invalid_response',message:'Invalid index status'}};
      pendingObserved = false;
    } else if (!failed) {
      pendingObserved = indexPresentation(index).pending;
      // A server response cannot grant the local transport-recovery marker.
      index = {...index};delete index.recovering;
    }
    latest = index;
    onStatus(latest);
    if (!active()) return;
    if (observe && (indexPresentation(latest).pending || latest.recovering === true)) {
      timer = schedule(() => { timer = null; void perform('status'); }, delay);
      delay = Math.min(maxDelay, delay * 1.5);
    }
  }
  release.refresh = () => { if (timer !== null) unschedule(timer); timer = null; return perform('status'); };
  // This is intentionally a separate user action, never part of polling.
  release.readmit = () => latest?.lifecycle === 'erased' ? perform('admit') : Promise.resolve();
  void perform('open');
  return release;
}
