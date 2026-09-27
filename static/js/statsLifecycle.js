const DIRTY_EVENTS = Object.freeze([
  'openclank:stats-dirty',
  'openclank:provider-changed',
  'openclank:session-changed',
]);

function boundedCadence(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return 300;
  return Math.min(86400, Math.max(30, Math.trunc(numeric)));
}

/**
 * Own the Stats refresh lifecycle for one mounted Usage window.
 *
 * Every source funnels through one single-flight request.  Events received
 * while hidden or in flight set one dirty bit; they cannot multiply timers,
 * listeners, or network work.
 */
export function createStatsRefreshLifecycle({
  refresh,
  target = globalThis.window,
  visibility = globalThis.document,
  timers = globalThis,
  cadenceSeconds = 300,
} = {}) {
  if (typeof refresh !== 'function') throw new TypeError('Stats refresh callback is required');
  let started = false;
  let stopped = false;
  let dirty = false;
  let interval = null;
  let cadence = boundedCadence(cadenceSeconds);
  let active = null;
  let controller = null;
  let generation = 0;
  let refreshCount = 0;

  const visible = () => !visibility || visibility.visibilityState !== 'hidden';

  const request = reason => {
    if (!started || stopped) return Promise.resolve(false);
    if (!visible()) {
      dirty = true;
      return Promise.resolve(false);
    }
    if (active) {
      dirty = true;
      return active;
    }
    dirty = false;
    const ownGeneration = ++generation;
    controller = new AbortController();
    refreshCount += 1;
    const operation = Promise.resolve().then(() => refresh({ reason, signal: controller.signal, generation: ownGeneration }));
    active = operation.finally(() => {
      if (active !== operation && active !== wrapped) return;
      active = null;
      controller = null;
      if (started && !stopped && dirty && visible()) {
        dirty = false;
        queueMicrotask(() => request('coalesced'));
      }
    });
    const wrapped = active;
    return wrapped;
  };

  const markDirty = event => request(event?.type || 'event');
  const onVisibility = () => { if (visible() && dirty) request('visibility'); };

  const armTimer = () => {
    if (interval != null) timers.clearInterval(interval);
    interval = timers.setInterval(() => request('timer'), cadence * 1000);
  };

  const start = ({ immediate = true } = {}) => {
    if (started && !stopped) return false;
    started = true;
    stopped = false;
    for (const type of DIRTY_EVENTS) target?.addEventListener?.(type, markDirty);
    target?.addEventListener?.('online', markDirty);
    visibility?.addEventListener?.('visibilitychange', onVisibility);
    armTimer();
    if (immediate) request('open');
    return true;
  };

  const stop = () => {
    if (!started || stopped) return false;
    stopped = true;
    started = false;
    dirty = false;
    if (interval != null) timers.clearInterval(interval);
    interval = null;
    for (const type of DIRTY_EVENTS) target?.removeEventListener?.(type, markDirty);
    target?.removeEventListener?.('online', markDirty);
    visibility?.removeEventListener?.('visibilitychange', onVisibility);
    controller?.abort();
    controller = null;
    active = null;
    generation += 1;
    return true;
  };

  const setCadence = value => {
    cadence = boundedCadence(value);
    if (started && !stopped) armTimer();
    return cadence;
  };

  return {
    start,
    stop,
    request,
    setCadence,
    diagnostics: () => ({ started: started && !stopped, dirty, inFlight: Boolean(active), cadenceSeconds: cadence, refreshCount }),
  };
}

export { boundedCadence };
