/** Inactive until an image runtime supplies a live session and authority epoch. */
export function captureImportOperation({ session, lifetime, readAuthorityEpoch, readEditGeneration }) {
  if (typeof session?.captureTarget !== 'function' || !lifetime || typeof readAuthorityEpoch !== 'function' || typeof readEditGeneration !== 'function') throw new TypeError('import origin is required');
  const target = session.captureTarget();
  const epoch = readAuthorityEpoch();
  const generation = readEditGeneration();
  if (!target?.scope?.accountId || !target.scope.workspaceId || epoch == null || !Number.isSafeInteger(generation)) throw new TypeError('import authority is unavailable');
  const identity = JSON.stringify(target);
  const classify = () => {
    if (lifetime.disposed || readAuthorityEpoch() !== epoch) return 'retired';
    try { if (JSON.stringify(session.captureTarget()) !== identity) return 'retired'; } catch (_) { return 'retired'; }
    return readEditGeneration() === generation ? 'apply' : 'recoverable';
  };
  return Object.freeze({
    session, target, generation, classify,
    settle(value, apply) {
      const outcome = classify();
      if (outcome === 'apply') { apply(value, session); return { status: 'applied' }; }
      if (outcome === 'recoverable') return { status: 'recoverable', session, value };
      return { status: 'retired' };
    },
  });
}
