// Canonical catalog adapter. Legacy endpoints without `catalog` keep working.

export const MODEL_STATE_KEYS = Object.freeze([
  'odysseus-model-recent',
  'odysseus-model-favorites',
  'odysseus-model-collapsed',
  'odysseus-models-collapsed',
  'odysseus-model-usage',
  'odysseus-model-sort',
  'models-order',
]);

function _normalizedOwner(username) {
  return String(username || '').trim().toLowerCase();
}

export function modelStateKey(base, username) {
  let owner = username;
  if (owner === undefined) {
    try { owner = globalThis.__openClankAuthenticatedUser; } catch { owner = ''; }
  }
  return `${base}:scope:${encodeURIComponent(_normalizedOwner(owner) || 'pending')}`;
}

export function bindModelStateOwner(username, previousUsername = '') {
  const owner = _normalizedOwner(username);
  if (!owner) return;
  const previous = _normalizedOwner(previousUsername);
  try {
    globalThis.__openClankAuthenticatedUser = owner;
    MODEL_STATE_KEYS.forEach(base => {
      const legacy = localStorage.getItem(base);
      if (legacy === null) return;
      const scoped = modelStateKey(base, owner);
      if ((!previous || previous === owner) && localStorage.getItem(scoped) === null) {
        localStorage.setItem(scoped, legacy);
      }
      localStorage.removeItem(base);
    });
  } catch { /* storage may be unavailable in private mode */ }
}

export function catalogEntries(item) {
  if (Array.isArray(item?.catalog) && item.catalog.length) {
    return item.catalog
      .filter(entry => entry && entry.model_id && entry.hidden !== true
        && entry.entitled !== false && entry.compatible !== false)
      .map(entry => ({
        mid: entry.model_id,
        displayName: entry.display_name || entry.model_id,
        baseModelId: entry.base_model_id || null,
        preset: entry.preset || null,
        variant: entry.variant || null,
        family: entry.family || null,
        providerFamilyId: entry.provider_family_id || null,
        providerDisplayName: entry.provider_display_name || null,
        extra: entry.curated === false,
        stale: entry.stale === true,
        entitled: entry.entitled,
        compatible: entry.compatible,
        capabilities: entry.capabilities || {},
        operations: entry.operations || [],
        providerModelId: entry.provider_model_id || null,
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

export function catalogHasModelChoice(items, modelId, endpointId = '', url = '') {
  if (!modelId) return false;
  const targetEndpointId = String(endpointId || '');
  const targetIsMimoSelector = targetEndpointId === 'mimo' || targetEndpointId === 'mimo:auto';
  const targetUrl = String(url || '').replace(/\/+$/, '');
  return (Array.isArray(items) ? items : []).some(item => {
    if (!item || item.offline || !catalogModelIds(item).includes(modelId)) return false;
    if (targetEndpointId) {
      const itemEndpointId = String(item.endpoint_id || '');
      if (targetIsMimoSelector) return itemEndpointId === 'mimo' || itemEndpointId.startsWith('mimo:');
      return itemEndpointId === targetEndpointId;
    }
    if (targetUrl) return String(item.url || '').replace(/\/+$/, '') === targetUrl;
    return true;
  });
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
    if (byKey.has(value)) return value;
    if (String(value).startsWith('endpoint:')) return null;
    const match = byLegacyId.get(value);
    return match ? modelChoiceKey(match) : null;
  }).filter(Boolean))];
}
