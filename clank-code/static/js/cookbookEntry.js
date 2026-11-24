// Keep the Cookbook launcher and background-job restore available on the shell.
let cookbook, bootState;
async function loadCookbook() {
  cookbook = (await import('./cookbook.js')).default;
  return cookbook;
}

async function fetchCookbookState() {
  const response = await fetch('/api/cookbook/state', {credentials:'same-origin'});
  return response.ok ? response.json() : null;
}

// Share the initial read with the real monitor if existing work wakes it.
export function readCookbookState() {
  return bootState || fetchCookbookState();
}

export async function restoreCookbookJobs() {
  if (cookbook || bootState) return;
  bootState = fetchCookbookState().catch(() => null);
  try {
    let localTasks;
    try { localTasks = JSON.parse(localStorage.getItem('cookbook-tasks')); } catch {}
    const state = await bootState;
    if (localTasks?.length || state?.tasks?.length) await loadCookbook();
  } finally {
    bootState = null;
  }
}

export default {
  open: async (...args) => (await loadCookbook()).open(...args),
  close: () => cookbook?.close(),
  isVisible: () => cookbook?.isVisible() || false,
  startBackgroundMonitor: async () => (await loadCookbook()).startBackgroundMonitor(),
};
