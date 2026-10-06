// One shell-owned, account-aware achievement notification lifecycle.
import ui from './ui.js';

const BASE = '/api/copal/treehouse/achievements';
let accountId = '', mounted = false, generation = 0, timer = null, busy = false;
const shownInMemory = new Set();
function shownKey(owner) { return `openclank-achievement-shown:scope:${owner}`; }
function alreadyShown(owner, id) {
  if (shownInMemory.has(id)) return true;
  try { return JSON.parse(localStorage.getItem(shownKey(owner)) || '[]').includes(id); } catch (_) { return false; }
}
function rememberShown(owner, id) {
  shownInMemory.add(id);
  try {
    const previous = JSON.parse(localStorage.getItem(shownKey(owner)) || '[]');
    localStorage.setItem(shownKey(owner), JSON.stringify([...new Set([...previous, id])].slice(-200)));
  } catch (_) {}
}
const channel = typeof BroadcastChannel === 'function' ? new BroadcastChannel('openclank-achievements') : null;

async function api(path, body) {
  const response = await fetch(BASE + path, {
    credentials: 'same-origin', cache: 'no-store',
    ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.detail?.message || `Achievement request failed (${response.status})`);
  return result;
}

function current(owner, epoch) { return mounted && owner && owner === accountId && epoch === generation; }
function announceReset(owner) {
  if (owner !== accountId) return;
  generation += 1;
  shownInMemory.clear();
  try { localStorage.removeItem(shownKey(owner)); } catch (_) {}
  document.dispatchEvent(new CustomEvent('openclank:achievements-reset', { detail: { accountId: owner } }));
}
if (channel) channel.onmessage = (event) => {
  if (event.data?.type === 'reset') announceReset(event.data.accountId);
};

function present(note, owner, epoch) {
  const openOriginal = () => {
    if (!current(owner, epoch)) return;
    window.focus();
    if (note.sessionId) window.sessionModule?.selectSession?.(note.sessionId);
  };
  const title = note.title || note.achievementKey || 'Achievement';
  const message = `${note.rarity === 'ultra' ? 'Ultra rare earned' : 'Achievement earned'}: ${title}`;
  ui.showToast(message, {
    duration: 12000,
    action: note.sessionId ? 'Open task' : undefined,
    onAction: note.sessionId ? openOriginal : undefined,
  });
  // Respect the system permission already granted by the user.
  try {
    if (typeof Notification !== 'undefined' && Notification.permission === 'granted') {
      const notification = new Notification(message, { tag: note.outboxId, icon: '/static/favicon.ico' });
      notification.onclick = openOriginal;
    }
  } catch (_) {}
}

// Permission is browser-local and changes only from an explicit user action.
export function mountAchievementNotificationSettings(root) {
  if (!root) return;
  if (root._refreshAchievementNotifications) {
    root._refreshAchievementNotifications();
    return;
  }
  const status = root.querySelector('[data-achievement-notification-status]');
  const enable = root.querySelector('[data-achievement-notification-enable]');
  const test = root.querySelector('[data-achievement-notification-test]');
  if (!status || !enable || !test) return;
  let requesting = false;
  const permission = () => typeof Notification === 'function'
    ? Notification.permission : 'unsupported';
  const refresh = () => {
    const state = permission();
    status.textContent = {
      granted: 'System notifications are allowed in this browser. Use Test to check delivery; your browser and OS notification settings may still suppress popups.',
      default: 'System notifications are not enabled in this browser. Choose Enable to request permission.',
      denied: 'System notifications are blocked in this browser. Allow notifications in this site’s browser permissions, then return here to test.',
      unsupported: 'System notifications are unavailable in this browser. Achievement notices still appear in the app.',
    }[state] || 'System notification permission is unavailable. Achievement notices still appear in the app.';
    enable.hidden = state === 'granted';
    enable.disabled = requesting || state !== 'default' || typeof Notification.requestPermission !== 'function';
    test.disabled = requesting || state !== 'granted';
  };
  root._refreshAchievementNotifications = refresh;
  enable.addEventListener('click', async () => {
    if (requesting || permission() !== 'default') return;
    requesting = true;
    try {
      // Invoke before awaiting anything so the browser receives the user's gesture.
      const decision = Notification.requestPermission();
      refresh();
      await decision;
      refresh();
    } catch (_) {
      refresh();
      status.textContent = 'The browser could not request notification permission. Check this site’s browser permissions. Achievement notices still appear in the app.';
    } finally {
      requesting = false;
      enable.disabled = permission() !== 'default';
      test.disabled = permission() !== 'granted';
    }
  });
  test.addEventListener('click', () => {
    refresh();
    if (permission() !== 'granted') return;
    try {
      const notification = new Notification('Open Clank achievement notifications', {
        body: 'Test notification. Earned achievements also appear in the app.',
        tag: 'openclank-achievement-notification-test', icon: '/static/favicon.ico',
      });
      notification.onclick = () => window.focus();
      status.textContent = 'Test sent to the browser. If no popup appears, check your browser and OS notification settings. Achievement notices still appear in the app.';
    } catch (_) {
      status.textContent = 'The browser could not send a system notification. Achievement notices still appear in the app; check your browser and OS notification settings.';
    }
  });
  window.addEventListener('focus', refresh);
  refresh();
}

export async function drainAchievementNotifications() {
  if (!mounted || !accountId || busy) return;
  busy = true;
  const owner = accountId, epoch = generation;
  const drain = async () => {
    if (!current(owner, epoch)) return;
    const data = await api('/notifications?limit=20');
    if (!current(owner, epoch) || data.accountId !== owner) return;
    for (const note of data.notifications || []) {
      if (!current(owner, epoch)) return;
      const claim = await api(`/notifications/${encodeURIComponent(note.outboxId)}/claim`, { accountId: owner });
      if (!claim.claimed || claim.accountId !== owner) continue;
      if (!current(owner, epoch)) return;
      try {
        if (!alreadyShown(owner, note.outboxId)) {
          present(note, owner, epoch);
          rememberShown(owner, note.outboxId);
          document.dispatchEvent(new CustomEvent('openclank:achievement-unlocked', { detail: { accountId: owner, achievementId: note.achievementId } }));
        }
      } catch (_) {
        await api(`/notifications/${encodeURIComponent(note.outboxId)}/failed`, { accountId: owner }).catch(() => {});
        continue;
      }
      await api(`/notifications/${encodeURIComponent(note.outboxId)}/delivered`, { accountId: owner }).catch(() => {});
      // A toast is a single visible slot. Keep each newly earned award readable.
      break;
    }
  };
  try {
    if (navigator.locks?.request) {
      await navigator.locks.request(`openclank-achievements:${owner}`, { ifAvailable: true }, lock => lock ? drain() : undefined);
    } else await drain();
  } catch (_) { /* Durable outbox remains available after transient failures. */ }
  finally { busy = false; }
}

export function adoptAchievementAccount(context) {
  const username = String(context?.username || '').trim();
  const candidate = String(context?.accountId || '').trim();
  const systemAccount = /^(?:local-installation|installer|maintenance|template[_-]seed|test[_-]fixture|import|background[_-]maintenance)$/i;
  const next = username && candidate && !systemAccount.test(username) && !systemAccount.test(candidate) ? candidate : '';
  if (next === accountId) return;
  accountId = next;
  generation += 1;
  if (timer) clearInterval(timer);
  timer = null;
  if (accountId && mounted) {
    void drainAchievementNotifications();
    timer = setInterval(drainAchievementNotifications, 15000);
  }
}
export function startAchievementLifecycle() {
  if (mounted) return;
  mounted = true;
  if (accountId) {
    void drainAchievementNotifications();
    timer = setInterval(drainAchievementNotifications, 15000);
  }
}
export async function resetMyAchievements() {
  const owner = accountId;
  if (!owner) throw new Error('Sign in to reset your achievements.');
  const data = await api('/reset', { confirm: 'reset-achievements', accountId: owner });
  if (data.accountId !== owner) throw new Error('The signed-in account changed.');
  announceReset(owner);
  channel?.postMessage({ type: 'reset', accountId: owner });
  return data;
}
document.addEventListener('openclank:auth-context-changed', event => adoptAchievementAccount(event.detail));
window.addEventListener('focus', () => void drainAchievementNotifications());
window.addEventListener('online', () => void drainAchievementNotifications());
