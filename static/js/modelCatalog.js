// Canonical catalog adapter. Legacy endpoints without `catalog` keep working.

export function catalogEntries(item) {
  if (Array.isArray(item?.catalog) && item.catalog.length) {
    return item.catalog
      .filter(entry => entry && entry.model_id && entry.hidden !== true
        && entry.entitled !== false && entry.compatible !== false)
      .map(entry => ({
        mid: entry.model_id,
        displayName: entry.display_name || entry.model_id,
        family: entry.family || null,
        extra: entry.curated === false,
        stale: entry.stale === true,
        entitled: entry.entitled,
        compatible: entry.compatible,
        capabilities: entry.capabilities || {},
      }));
  }
  const models = item?.models || [];
  const extras = item?.models_extra || [];
  const displays = item?.models_display || models;
  const extraDisplays = item?.models_extra_display || extras;
  return models.map((mid, i) => ({ mid, displayName: displays[i] || mid, extra: false }))
    .concat(extras.map((mid, i) => ({ mid, displayName: extraDisplays[i] || mid, extra: true })));
}

export function catalogModelIds(item) {
  return catalogEntries(item).map(entry => entry.mid);
}

export function modelChoiceKey(model, endpointId, url) {
  const mid = typeof model === 'string' ? model : (model?.mid || model?.model_id || '');
  const route = endpointId || (typeof model === 'object' && model ? model.endpointId : '')
    || url || (typeof model === 'object' && model ? model.url : '') || 'unknown';
  return `endpoint:${encodeURIComponent(route)}:${encodeURIComponent(mid)}`;
}

export function resolveStoredModelChoices(stored, models) {
  const choices = Array.isArray(models) ? models : [];
  const byKey = new Map(choices.map(model => [modelChoiceKey(model), model]));
  const byLegacyId = new Map();
  choices.forEach(model => {
    if (!byLegacyId.has(model.mid)) byLegacyId.set(model.mid, model);
  });
  return [...new Set((Array.isArray(stored) ? stored : []).map(value => {
    if (byKey.has(value) || String(value).startsWith('endpoint:')) return value;
    const match = byLegacyId.get(value);
    return match ? modelChoiceKey(match) : value;
  }))];
}
