/**
 * Bounded, scope-aware local navigation for a Copal tool window.
 *
 * Entries are presentation descriptors.  Bodies, bytes, credentials and
 * arbitrary large values are deliberately omitted before they reach history.
 * Restoration is transactional: a rejected/failed async restore leaves the
 * cursor where it was.
 */

const OMIT_KEYS = new Set(['body', 'content', 'contents', 'bytes', 'data', 'document', 'documentBody', 'raw', 'credential', 'token']);
const MAX_DEPTH = 8;
const MAX_KEYS = 100;
const MAX_ITEMS = 100;
const MAX_STRING = 2048;

function safeClone(value, key = '', depth = 0, seen = new WeakSet()) {
  if (OMIT_KEYS.has(key)) return undefined;
  if (value == null || typeof value === 'number' || typeof value === 'boolean') return value;
  if (typeof value === 'string') return value.slice(0, MAX_STRING);
  if (depth >= MAX_DEPTH) return '[truncated]';
  if (typeof value !== 'object') return undefined;
  if (seen.has(value)) return '[Circular]';
  seen.add(value);
  if (Array.isArray(value)) return value.slice(0, MAX_ITEMS).map((item) => safeClone(item, '', depth + 1, seen)).filter((item) => item !== undefined);
  if (typeof value !== 'object') return undefined;
  const output = {};
  for (const [childKey, childValue] of Object.entries(value).slice(0, MAX_KEYS)) {
    if (OMIT_KEYS.has(childKey)) continue;
    const cloned = safeClone(childValue, childKey, depth + 1, seen);
    if (cloned !== undefined) output[childKey] = cloned;
  }
  return output;
}

function copyEntry(entry) {
  return safeClone(entry || {}) || {};
}

function scopeOf(scope) {
  if (scope == null) return null;
  if (typeof scope === 'string') return { account: scope, workspace: null };
  return { account: scope.account ?? scope.accountScope ?? null, workspace: scope.workspace ?? scope.workspaceScope ?? null };
}

function sameScope(left, right) {
  const a = scopeOf(left);
  const b = scopeOf(right);
  return a?.account === b?.account && a?.workspace === b?.workspace;
}

export function createWindowNavigation({
  scope = null,
  maxEntries = 100,
  restore = null,
  onChange = null,
} = {}) {
  const limit = Math.max(1, Math.min(100, Math.floor(Number(maxEntries) || 100)));
  let currentScope = scopeOf(scope);
  let entries = [];
  let cursor = -1;
  let generation = 0;
  let pending = null;
  let disposed = false;

  const notify = () => { try { onChange?.(snapshot()); } catch (_) {} };
  const valid = () => !disposed;
  const inScope = (entry) => sameScope(entry?.scope ?? entry, currentScope);

  function snapshot() {
    return {
      scope: currentScope ? { ...currentScope } : null,
      cursor,
      length: entries.length,
      canGoBack: cursor > 0,
      canGoForward: cursor >= 0 && cursor < entries.length - 1,
      entries: entries.map(copyEntry),
      pending: !!pending,
    };
  }

  function capture(entry) {
    if (!valid()) return null;
    const copy = copyEntry(entry);
    if (!Object.prototype.hasOwnProperty.call(copy, 'scope')) copy.scope = currentScope ? { ...currentScope } : null;
    return copy;
  }

  function commit(entry) {
    if (!valid()) return false;
    const copy = capture(entry);
    if (!copy || !inScope(copy)) return false;
    // A failed/late restore cannot be followed by a commit from the old
    // scope.  New committed navigation truncates the forward branch.
    pending = null;
    entries = entries.slice(0, cursor + 1);
    const previous = entries.at(-1);
    if (!previous || JSON.stringify(previous) !== JSON.stringify(copy)) entries.push(copy);
    if (entries.length > limit) entries.splice(0, entries.length - limit);
    cursor = entries.length - 1;
    generation += 1;
    notify();
    return true;
  }

  async function move(delta) {
    if (!valid() || pending) return false;
    const target = cursor + delta;
    if (target < 0 || target >= entries.length || target === cursor) return false;
    const sourceGeneration = generation;
    const targetEntry = copyEntry(entries[target]);
    if (!inScope(targetEntry)) return false;
    const token = { generation: sourceGeneration, target, scope: currentScope };
    pending = token;
    let result = true;
    try {
      result = restore ? await restore(targetEntry, { direction: delta < 0 ? 'back' : 'forward', generation: sourceGeneration, scope: currentScope }) : true;
    } catch (_) {
      result = false;
    }
    if (pending !== token || !valid() || generation !== sourceGeneration || !sameScope(token.scope, currentScope)) return false;
    pending = null;
    if (result === false) { notify(); return false; }
    cursor = target;
    generation += 1;
    notify();
    return true;
  }

  function setScope(nextScope, { clear = true } = {}) {
    currentScope = scopeOf(nextScope);
    generation += 1;
    pending = null;
    // A caller may retain presentation history when switching providers, but
    // entries from the old account/workspace must never be restorable.
    entries = clear ? [] : entries.filter(inScope);
    cursor = entries.length ? Math.min(cursor, entries.length - 1) : -1;
    notify();
    return snapshot();
  }

  function reset(nextScope = currentScope) {
    currentScope = scopeOf(nextScope);
    entries = [];
    cursor = -1;
    generation += 1;
    pending = null;
    notify();
    return true;
  }

  return {
    capture,
    commit,
    back: () => move(-1),
    forward: () => move(1),
    canGoBack: () => valid() && !pending && cursor > 0,
    canGoForward: () => valid() && !pending && cursor >= 0 && cursor < entries.length - 1,
    reset,
    setScope,
    snapshot,
    get generation() { return generation; },
    dispose() { disposed = true; pending = null; entries = []; cursor = -1; },
  };
}

export const createBoundedWindowHistory = createWindowNavigation;
