import { get as getStored, KEYS } from './storage.js';

// Discovery refresh leases an existing native owner; it never starts a worker.
export async function refreshReadyNativeCommands({ reloadSkills = false, changedEvent = 'odysseus:command-catalog-changed', fetcher = window.fetch.bind(window) } = {}) {
  document.dispatchEvent(new CustomEvent(changedEvent));
  const sid = getStored(KEYS.CURRENT_SESSION, '');
  if (!sid) return { refreshed: false, reason: 'no-session' };
  try {
    const response = await fetcher(`/api/session/${encodeURIComponent(sid)}/mimo-operation`, {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation: 'commands-refresh', reload_skills: reloadSkills === true }),
    });
    if (!response.ok) return { refreshed: false, reason: 'not-ready' };
    if (getStored(KEYS.CURRENT_SESSION, '') !== sid) return { refreshed: false, reason: 'chat-changed' };
    document.dispatchEvent(new CustomEvent('odysseus:command-catalog-changed'));
    return { refreshed: true };
  } catch { return { refreshed: false, reason: 'unavailable' }; }
}
