// Browser-local presentation only. No file, account or indexing authority.
export const GRAPHICS_MODE_KEY = 'openclank-graph-files-editor-rendering';
export const GRAPHICS_MODE_EVENT = 'openclank:graphical-rendering-changed';
export const normalizeGraphicsMode = value => value === 'ordinary' ? 'ordinary' : 'auto';

export function createPresentationPolicy({storage, events} = {}) {
  let current = 'auto';
  const listeners = new Set();
  try { current = normalizeGraphicsMode(storage?.getItem(GRAPHICS_MODE_KEY)); } catch (_) {}
  const changed = value => {
    const next = normalizeGraphicsMode(value);
    if (next === current) return false;
    current = next;
    for (const listener of [...listeners]) { try { listener(next); } catch (_) {} }
    return true;
  };
  const onStorage = event => { if (event.key === GRAPHICS_MODE_KEY || event.key === null) {
    try { changed(storage?.getItem(GRAPHICS_MODE_KEY)); } catch (_) { changed('auto'); }
  } };
  events?.addEventListener?.('storage', onStorage);
  return {
    get mode() { return current; },
    set(value) {
      const next = normalizeGraphicsMode(value);
      try { storage?.setItem(GRAPHICS_MODE_KEY, next); } catch (_) {}
      changed(next);
      return next;
    },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    dispose() { listeners.clear(); events?.removeEventListener?.('storage', onStorage); },
  };
}

let browserPolicy;
export function graphicalPresentationPolicy() {
  if (!browserPolicy) {
    let storage; try { storage = globalThis.localStorage; } catch (_) {}
    browserPolicy = createPresentationPolicy({storage, events:globalThis.window});
  }
  return browserPolicy;
}
