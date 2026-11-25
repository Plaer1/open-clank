// A submitted Add may have committed before its response was lost. Share the
// original request across the composer and applet until its outcome is known.
const pending = new Map();

export function explicitMemoryCommandText(input) {
  return /^\/(?:host:)?(?:remember|(?:m|memory)\s+(?:add|echo))\s+([\s\S]+)$/i.exec(input.trim())?.[1].trim() || '';
}

export function memoryAddMessage(result) {
  if (result.state === 'terminal_erased') return 'Original Add was erased';
  return result.pending_review || result.state === 'staged' ? 'Sent to Candidates for review' : 'Memory added';
}

export async function saveExplicitMemory(text, category, current) {
  if (!text) throw new Error('Memory text cannot be empty');
  const account = document.body?.dataset?.accountId;
  if (!account) throw new Error('Account is unavailable.');
  if (!current()) return;
  const old = pending.get(account);
  if (old && (old.text !== text || old.category !== category)) {
    throw new Error('Check the original Add outcome before changing its text or category.');
  }
  const original = old || {text, category, request_key: crypto.randomUUID()};
  if (original.busy) return;
  const action = old && !original.retrySubmit ? 'status' : 'submit';
  original.retrySubmit = false;
  original.busy = true;
  pending.set(account, original);
  try {
    const response = await fetch('/api/memory/add', {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, category, request_key: original.request_key, action}),
    });
    if (document.body?.dataset?.accountId !== account) return;
    const result = await response.json().catch(() => null);
    if (document.body?.dataset?.accountId !== account) return;
    if (!response.ok) {
      if (!old && [400, 401, 403, 404, 422].includes(response.status)) {
        pending.delete(account);
        throw new Error(typeof result?.detail === 'string' ? result.detail : 'Memory add was rejected.');
      }
      if (result?.reason === 'memory_preparing' && action === 'submit') {
        original.retrySubmit = true;
        throw new Error('Memory is still being prepared. Try again in a moment.');
      }
      throw new Error('Original Add outcome unavailable; retry to check status.');
    }
    if (result?.state === 'absent') {
      original.retrySubmit = true;
      throw new Error('No saved receipt yet. Retry to submit the same memory again.');
    }
    const guarded = ['applied','staged','terminal_erased'].includes(result?.state);
    const legacy = !old && result?.ok === true && !Object.hasOwn(result, 'state')
      && (typeof result.memory_id === 'string' || (Number.isSafeInteger(result.count) && result.message === 'Memory already exists'));
    if (!guarded && !legacy) throw new Error('Original Add outcome unavailable.');
    pending.delete(account);
    if (current()) return result;
  } finally {
    original.busy = false;
  }
}
