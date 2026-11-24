/** Process-local invalidations are hints. Only readCurrent supplies content. */
export function openSharedTextStream({publicationID, isCurrent, readCurrent, onSnapshot, onStatus, eventSourceFactory}) {
  if (typeof publicationID !== 'string' || !publicationID || publicationID.length > 128 ||
      typeof isCurrent !== 'function' || typeof readCurrent !== 'function' || typeof onSnapshot !== 'function')
    throw new TypeError('Shared text stream unavailable');
  const notify = typeof onStatus === 'function' ? onStatus : () => {};
  const make = eventSourceFactory || (url => new EventSource(url, {withCredentials:true}));
  let closed = false, serial = 0, running = false, pending = false;
  const source = make('/api/files-v1/shared-text/events?publication_id=' + encodeURIComponent(publicationID));
  const close = () => { if (closed) return; closed = true; serial++; pending = false; source.close(); };
  const current = () => {
    if (closed) return false;
    try { if (isCurrent() === true) return true; } catch (_) {}
    close(); return false;
  };
  const refresh = () => {
    if (!current()) return;
    serial++; pending = true;
    if (running) return;
    running = true;
    void (async () => {
      try {
        while (pending && current()) {
          pending = false;
          const original = serial;
          try {
            const value = await readCurrent();
            if (current() && serial === original) { onSnapshot(value); notify('current'); }
          } catch (_) { if (current() && serial === original) notify('unavailable'); }
        }
      } finally { running = false; }
    })();
  };
  source.onopen = () => { if (current()) { notify('refreshing'); refresh(); } };
  for (const kind of ['invalidate','gap']) source.addEventListener(kind, event => {
    if (!current()) return;
    try { const data=JSON.parse(event.data); if (data?.version !== 1 || data.kind !== kind) throw Error(); }
    catch (_) { notify('stale'); refresh(); return; }
    notify('stale'); refresh();
  });
  source.onerror = () => { if (current()) { serial++; pending=false; notify('disconnected'); } };
  return Object.freeze({close});
}
