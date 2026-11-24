// Operation-first media menu model (pure). The server reports operations and their compatible configured profiles;
// the provider family never appears: a profile is only its label, id and revision.
export const NEUTRAL_OPERATIONS = ['image.generate', 'mesh.generate', 'animation.generate'];

/** catalog = { operations: [{operation, label?, profiles[], partial?, reason?}] }. Unknown operations are kept, labelled by id. */
export function buildMenu(catalog, only = null) {
  const rows = Array.isArray(catalog?.operations) ? catalog.operations : [];
  return rows.filter(row => typeof row?.operation === 'string' && (!only || only.includes(row.operation))).map(row => {
    const profiles = Array.isArray(row.profiles) ? row.profiles.filter(p => p && typeof p.profile_id === 'string' && p.operation === row.operation).slice(0, 20) : [];
    const state = profiles.length === 0 ? 'unavailable' : profiles.length === 1 ? 'ready' : 'choose';
    return { operation: row.operation, label: typeof row.label === 'string' && row.label ? row.label : row.operation, state, profiles,
      partial: row.partial === true, reason: state === 'unavailable' ? String(row.reason || 'No configured profile can perform this operation.') : null };
  });
}

export function menuPrompt(items) {
  return items.map((item, i) => `${i + 1}. ${item.label}${item.state === 'unavailable' ? ` (unavailable: ${item.reason})` : ''}`).join('\n');
}

export function pickOperation(items, answer) {
  const item = items[Number(String(answer).trim()) - 1];
  if (!item) throw new Error('Choose a listed operation.');
  if (item.state === 'unavailable') throw new Error(`${item.label} is unavailable: ${item.reason}`);
  return item;
}

export function profilePrompt(item) {
  return item.profiles.map((p, i) => `${i + 1}. ${String(p.label || p.profile_id).slice(0, 120)}`).join('\n') + (item.partial ? '\nSome profiles are unavailable.' : '');
}

/** One compatible profile needs no question; several need one; none is an error carrying the reason. */
export function needsProfileQuestion(item) { return item.state === 'choose'; }
export function pickProfile(item, answer = null) {
  if (item.state === 'unavailable') throw new Error(`${item.label} is unavailable: ${item.reason}`);
  if (item.state === 'ready') return item.profiles[0];
  const profile = item.profiles[Number(String(answer).trim()) - 1];
  if (!profile) throw new Error('Choose a listed profile.');
  return profile;
}
