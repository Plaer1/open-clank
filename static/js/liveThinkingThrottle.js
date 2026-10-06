/** Pure trailing-edge coalescer for thinking-state presentation. */
export function createLiveThinkingThrottle(commit, {
  delay = 100,
  prepare = (value) => String(value ?? ''),
  schedule = (callback, ms) => setTimeout(callback, ms),
  cancel = (timer) => clearTimeout(timer),
} = {}) {
  let timer = null;
  let latest = null;
  let dirty = false;
  const commitLatest = () => {
    timer = null;
    if (!dirty) return false;
    dirty = false;
    commit(prepare(latest));
    return true;
  };
  return {
    update(value) {
      latest = value;
      dirty = true;
      if (timer !== null) cancel(timer);
      timer = schedule(commitLatest, delay);
    },
    flush() {
      if (timer !== null) { cancel(timer); timer = null; }
      return commitLatest();
    },
    cancel() {
      if (timer !== null) cancel(timer);
      timer = null;
      dirty = false;
    },
  };
}

export default createLiveThinkingThrottle;
