// Tiny observable store — replaces zustand. One state object, subscribers
// get (state, changedKeys) after every set().

export function createStore(initial = {}) {
  let state = { ...initial };
  const subs = new Set();
  return {
    get: () => state,
    set(patch) {
      state = { ...state, ...patch };
      const keys = Object.keys(patch);
      for (const fn of subs) fn(state, keys);
    },
    subscribe(fn) {
      subs.add(fn);
      return () => subs.delete(fn);
    },
  };
}
