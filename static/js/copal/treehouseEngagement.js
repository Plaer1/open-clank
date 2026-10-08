// Engagement estimates only: no points, completion, verification or answer text.
export const ENGAGEMENT_POLICY = Object.freeze({ version:'foreground-idle-v1', idleMs:30000, sampleMs:5000, flushMs:30000, maxIntervalMs:60000, maxPending:64 });

export function createForegroundClock(now = 0, policy = ENGAGEMENT_POLICY) {
  let sampled = now, interacted = -Infinity, previouslyEligible = false;
  return {
    sample(eligible, at) {
      const elapsed = at - sampled;
      const active = eligible && previouslyEligible && elapsed >= 0 && elapsed <= policy.sampleMs * 2
        ? Math.max(0, Math.min(at, interacted + policy.idleMs) - sampled) : 0;
      sampled = at; previouslyEligible = eligible;
      return active;
    },
    interact(at) { interacted = at; },
  };
}

export function createTreeHouseEngagement({ getContext, send, storage = globalThis.localStorage } = {}) {
  let context = null, contextId = '', clock = null, chunkStart = 0, startedAt = '', activeMs = 0;
  let timer = null, lastDelivery = 0, delivering = false, sequence = 0, queue = [], attached = false;
  const session = crypto.randomUUID();
  const keyFor = ctx => `treehouse-engagement-v1:${encodeURIComponent(ctx.accountId)}:${encodeURIComponent(ctx.workspace)}`;
  const identity = ctx => ctx ? JSON.stringify([ctx.accountId,ctx.workspace,ctx.courseId,ctx.curriculumDigest,ctx.generation]) : '';
  const current = () => {
    const ctx = getContext?.();
    return ctx?.accountId && ctx?.workspace && ctx?.courseId && ctx?.curriculumDigest && ctx?.body?.isConnected ? ctx : null;
  };
  function eligible(ctx) {
    if (!ctx || document.visibilityState !== 'visible' || !document.hasFocus() || !ctx.body.getClientRects().length) return false;
    const applet = ctx.body.closest('.copal-workspace') || ctx.body.closest('[role="dialog"]') || ctx.body;
    if (applet.classList.contains('hidden') || applet.classList.contains('modal-minimized')) return false;
    return applet.contains(document.activeElement);
  }
  function persist() {
    if (!context) return;
    try { storage?.setItem(keyFor(context), JSON.stringify(queue)); } catch { /* Memory-only queue: delivery coverage is explicitly partial. */ }
  }
  function load(ctx) {
    try {
      const stored = JSON.parse(storage?.getItem(keyFor(ctx)) || '[]');
      return Array.isArray(stored) ? stored.filter(item => item?.receiptId && item?.courseId && item?.policyVersion === ENGAGEMENT_POLICY.version).slice(-ENGAGEMENT_POLICY.maxPending) : [];
    } catch { return []; }
  }
  function sample(at = performance.now()) {
    if (!context || !clock) return;
    activeMs += clock.sample(eligible(context), at);
    // Long suspension is never counted; interval remains bounded as well.
    if (at - chunkStart > ENGAGEMENT_POLICY.maxIntervalMs) finish(at);
  }
  function finish(at = performance.now()) {
    if (!context) return;
    const intervalMs = Math.round(Math.min(ENGAGEMENT_POLICY.maxIntervalMs, Math.max(0,at-chunkStart)));
    const durationMs = Math.min(intervalMs, Math.round(activeMs));
    if (durationMs > 0) {
      queue.push({ receiptId:`${session}:${++sequence}`, courseId:context.courseId,
        curriculumRevision:context.curriculumRevision, curriculumDigest:context.curriculumDigest, generation:context.generation,
        durationMs, intervalMs, occurredAt:startedAt, policyVersion:ENGAGEMENT_POLICY.version });
      // Overflow is a declared coverage gap; never turn this bounded queue into a lifetime claim.
      if (queue.length > ENGAGEMENT_POLICY.maxPending) queue.shift();
      persist();
    }
    chunkStart = at; startedAt = new Date().toISOString(); activeMs = 0;
  }
  async function deliver() {
    if (delivering || !context || !queue.length) return;
    delivering = true;
    const scope = context, key = keyFor(scope);
    try {
      // Bounded batch, at most once per 30s or on a lifecycle transition.
      for (let sent = 0; sent < 5 && queue.length; sent++) {
        if (keyFor(current() || {accountId:'',workspace:''}) !== key) break;
        const receipt = queue[0];
        try { await send(receipt, scope); }
        catch (error) {
          const status = Number(error?.status || error?.statusCode);
          const code = error?.code || error?.detail?.code;
          if ([400,403,409,422].includes(status) || ['forbidden','stats_source_conflict','stale_engagement_context','expired_engagement_interval'].includes(code)) {
            if (context && keyFor(context) === key && queue[0] === receipt) { queue.shift(); persist(); }
            continue;
          }
          break; // Network, auth, unavailable or rate limit: retain stable ID for later delivery.
        }
        if (!context || keyFor(context) !== key || queue[0] !== receipt) break;
        queue.shift(); persist();
      }
    } finally { delivering = false; }
  }
  const onInput = event => {
    if (!context?.body.contains(event.target) || !event.isTrusted) return;
    const at = performance.now(); sample(at); clock?.interact(at);
  };
  const onBoundary = () => { sample(); finish(); void deliver(); };
  function attach() {
    if (attached) return; attached = true;
    for (const event of ['pointerdown','keydown','wheel','touchstart']) document.addEventListener(event,onInput,{capture:true,passive:true});
    document.addEventListener('visibilitychange',onBoundary);
    window.addEventListener('blur',onBoundary); window.addEventListener('pagehide',onBoundary);
  }
  function detach() {
    if (!attached) return; attached = false;
    for (const event of ['pointerdown','keydown','wheel','touchstart']) document.removeEventListener(event,onInput,true);
    document.removeEventListener('visibilitychange',onBoundary);
    window.removeEventListener('blur',onBoundary); window.removeEventListener('pagehide',onBoundary);
  }
  function refresh() {
    const ctx = current(), nextId = identity(ctx), at = performance.now();
    sample(at);
    if (nextId !== contextId) {
      finish(at); void deliver();
      const sameQueue = context && ctx && keyFor(context) === keyFor(ctx);
      context = ctx; contextId = nextId;
      if (ctx) {
        queue = sameQueue ? queue : load(ctx); clock = createForegroundClock(at);
        chunkStart = at; startedAt = new Date().toISOString(); activeMs = 0;
        // Initial foreground reading gets one bounded idle window; actual
        // trusted input is required to extend it beyond thirty seconds.
        clock.interact(at); clock.sample(eligible(ctx),at);
      } else { clock = null; queue = []; }
    } else if (ctx) { context = ctx; }
    if (ctx && !timer) {
      attach(); lastDelivery = at;
      timer = setInterval(() => {
        refresh(); sample();
        const tick = performance.now();
        if (tick-lastDelivery >= ENGAGEMENT_POLICY.flushMs) { finish(tick); lastDelivery = tick; void deliver(); }
      },ENGAGEMENT_POLICY.sampleMs);
    } else if (!ctx && timer) { clearInterval(timer); timer = null; detach(); }
  }
  function stop() {
    sample(); finish(); void deliver();
    if (timer) clearInterval(timer); timer = null; detach();
    context = null; contextId = ''; clock = null; activeMs = 0; queue = [];
  }
  return { refresh, stop };
}
