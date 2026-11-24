const DECISION_STATES = new Set(['unselected', 'selected', 'adopted', 'rejected']);

function note(value, name) {
  const text = String(value ?? '');
  if (text.length > 2000 || text.includes('\0')) throw new TypeError(`${name} is too long or contains an invalid character.`);
  return text;
}

/** Optional decision-board adapter. Generic reports may use any bounded JSON value. */
export function normalizeDecisionValue(value) {
  if (typeof value === 'boolean') {
    return { state: value ? 'selected' : 'unselected', adoption_note: '', rejection_note: '' };
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)
    || Object.keys(value).some(key => !['state', 'selected', 'adoption_note', 'rejection_note', 'note'].includes(key))) {
    throw new TypeError('Decision progress item is invalid.');
  }
  let state = value.state;
  let adoptionNote = value.adoption_note;
  let rejectionNote = value.rejection_note;
  if (state == null && typeof value.selected === 'boolean') {
    state = value.selected ? 'selected' : 'unselected';
    if (value.note != null) {
      if (value.selected) adoptionNote = value.note;
      else rejectionNote = value.note;
    }
  } else if (value.selected != null || value.note != null) {
    throw new TypeError('Decision progress mixes legacy and current fields.');
  }
  if (!DECISION_STATES.has(state)) throw new TypeError('Decision progress state is unsupported.');
  return {
    state,
    adoption_note: note(adoptionNote, 'Adoption note'),
    rejection_note: note(rejectionNote, 'Dislike or rejection note'),
  };
}

/** Convert legacy ID-only selections into explicit per-item states without dropping item identity. */
export function normalizeLegacySelections(selectedIds, manifestItems) {
  if (!Array.isArray(selectedIds) || !Array.isArray(manifestItems)) throw new TypeError('Legacy selections are invalid.');
  const selected = new Set(selectedIds.map(String));
  if (selected.size !== selectedIds.length) throw new TypeError('Legacy selections contain duplicate IDs.');
  const ids = new Set();
  const normalized = manifestItems.map(item => {
    const id = String(item?.id || '');
    if (!id || ids.has(id)) throw new TypeError('Decision item manifest contains duplicate or empty IDs.');
    ids.add(id);
    return { id, digest: String(item.digest || ''), value: normalizeDecisionValue(selected.has(id)) };
  });
  const unknown = [...selected].filter(id => !ids.has(id));
  if (unknown.length) throw new TypeError(`Legacy selections contain unknown item IDs: ${unknown.join(', ')}.`);
  return normalized;
}

export function normalizeDecisionItems(items) {
  if (!Array.isArray(items)) throw new TypeError('Decision items must be an array.');
  return items.map(item => {
    if (!item || typeof item !== 'object' || Array.isArray(item) || typeof item.id !== 'string' || typeof item.digest !== 'string') {
      throw new TypeError('Decision item envelope is invalid.');
    }
    return { id: item.id, digest: item.digest, value: normalizeDecisionValue(item.value) };
  });
}
