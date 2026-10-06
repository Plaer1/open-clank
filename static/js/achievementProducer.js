// Minimal receipts reuse the authenticated event owner and its idempotent ledger.
let owner = '';
let delivery = null;
const volatile = new Map();
const key = account => `openclank:achievement-pending:${account}`;
const read = account => { if (volatile.has(account)) return volatile.get(account); try { const value = JSON.parse(localStorage.getItem(key(account)) || '[]'); return Array.isArray(value) ? value : []; } catch (_) { return []; } };
const write = (account, items) => { try { localStorage.setItem(key(account), JSON.stringify(items)); } catch (_) { volatile.set(account, items); /* Storage denial still permits immediate delivery. */ } };
export function achievementOwner() { return owner; }
export async function flushAchievementReceipts() {
  if (!owner) return false;
  if (delivery) { await delivery; if (!owner || !read(owner).length) return true; }
  const account = owner;
  delivery = (async () => {
    try {
      await fetch('/api/copal/achievements/resume', { method:'POST', credentials:'same-origin' });
      for (const item of read(account)) {
        if (owner !== account) return false;
        const response = await fetch(item.url || '/api/copal/treehouse/achievements/events', {
          method:'POST', credentials:'same-origin', headers:{ 'Content-Type':'application/json' }, body:JSON.stringify(item.body),
        });
        if (!response.ok) {
          // A deleted/stale link occurrence is terminal, never replay it as success.
          if (![400, 404, 409, 422].includes(response.status)) return false;
        }
        write(account, read(account).filter(pending => pending.id !== item.id));
      }
      return true;
    } catch (_) { return false; }
  })();
  try { return await delivery; } finally { delivery = null; }
}
document.addEventListener('openclank:auth-context-changed', event => {
  owner = String(event.detail?.accountId || '');
  if (owner) void flushAchievementReceipts();
});
window.addEventListener('online', () => { void flushAchievementReceipts(); });
export async function queueReceipt(id, body, { accountId = owner, url = null } = {}) {
  if (!accountId || accountId !== owner) return false;
  const pending = read(accountId);
  if (!pending.some(item => item.id === id)) {
    // Bound presentation storage; flush before admitting an overflow.
    if (pending.length >= 256) { await flushAchievementReceipts(); if (read(accountId).length >= 256) return false; }
    write(accountId, [...read(accountId), { id, url, body }]);
  }
  return flushAchievementReceipts();
}
export async function recordPresentation(family, facts, { occurrenceId = crypto.randomUUID(), accountId = owner, workspaceId = null, kind = 'U' } = {}) {
  const id = `ui:${occurrenceId}:${family}`;
  return queueReceipt(id, { accountId, events:[{ source_event_id:id, event_family:family,
    kind, result:kind === 'R' ? 'committed' : 'acknowledged', actor_kind:'user',
    occurred_at:new Date().toISOString(), workspace_id:workspaceId, facts }] }, { accountId });
}
export async function activityDigest(value) {
  const canonical = item => Array.isArray(item) ? item.map(canonical) : item && typeof item === 'object'
    ? Object.fromEntries(Object.keys(item).sort().map(name => [name, canonical(item[name])])) : item;
  const bytes = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(canonical(value))));
  return [...new Uint8Array(bytes)].map(v => v.toString(16).padStart(2, '0')).join('');
}
export async function visiblePresentation(element) {
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  return document.visibilityState === 'visible' && !!element?.isConnected && !!element.getClientRects().length;
}
export function acknowledgeVisible(element, family, facts, options = {}) {
  const accountId = options.accountId || owner;
  void visiblePresentation(element).then(visible => visible && recordPresentation(family, facts, { ...options, accountId }));
}

document.addEventListener('openclank:achievements-reset', event => {
  const account = String(event.detail?.accountId || owner || '');
  if (account) write(account, []);
});
