// Canonical catalog adapter. Legacy endpoints without `catalog` keep working.

export const MODEL_STATE_KEYS = Object.freeze([
  'openclank-model-recent',
  'openclank-model-favorites',
  'openclank-model-collapsed',
  'openclank-models-collapsed',
  'openclank-model-usage',
  'openclank-model-sort',
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

export function bindModelStateOwner(username) {
  const owner = _normalizedOwner(username);
  if (owner) globalThis.__openClankAuthenticatedUser = owner;
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
        optionRouteId: entry.option_route_id || entry.provider_model_route_id || null,
        catalogRevision: entry.catalog_revision ?? null,
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
  const matches = new Map();
  for (const item of (Array.isArray(items) ? items : [])) {
    if (!item || item.offline) continue;
    const itemEndpointId = String(item.endpoint_id || '');
    const itemUrl = String(item.url || '').replace(/\/+$/, '');
    if (targetEndpointId) {
      if (targetIsMimoSelector ? !(itemEndpointId === 'mimo' || itemEndpointId.startsWith('mimo:')) : itemEndpointId !== targetEndpointId) continue;
    } else if (targetUrl && itemUrl !== targetUrl) continue;
    if (!itemEndpointId && !itemUrl) continue;
    for (const entry of catalogEntries(item)) {
      if (entry.mid !== modelId) continue;
      const key = modelChoiceKey(modelId, itemEndpointId, itemUrl);
      const route = JSON.stringify([entry.providerModelId || null, entry.optionRouteId || null]);
      if (!matches.has(key)) matches.set(key, new Set());
      matches.get(key).add(route);
    }
  }
  if ([...matches.values()].some(routes => routes.size !== 1)) return false;
  return targetIsMimoSelector ? matches.size > 0 : matches.size === 1;
}

export function modelChoiceKey(model, endpointId, url) {
  const mid = typeof model === 'string' ? model : (model?.mid || model?.model_id || '');
  const route = endpointId || (typeof model === 'object' && model ? model.endpointId : '')
    || url || (typeof model === 'object' && model ? model.url : '') || 'unknown';
  return `endpoint:${encodeURIComponent(route)}:${encodeURIComponent(mid)}`;
}

export function resolveStoredModelChoice(stored, models) {
  const choices = Array.isArray(models) ? models : [];
  const keys = new Map();
  for (const model of choices) {
    if (!model || !model.mid || (!model.endpointId && !model.url)) continue;
    const key = modelChoiceKey(model);
    if (stored !== key && (String(stored).startsWith('endpoint:') || stored !== model.mid)) continue;
    const route = JSON.stringify([model.providerModelId || null, model.optionRouteId || null]);
    if (!keys.has(key)) keys.set(key, new Map());
    keys.get(key).set(route, model);
  }
  if (keys.size !== 1) return null;
  const routes = keys.values().next().value;
  return routes.size === 1 ? routes.values().next().value : null;
}

export function resolveStoredModelChoices(stored, models) {
  return [...new Set((Array.isArray(stored) ? stored : []).map(value => {
    const match = resolveStoredModelChoice(value, models);
    return match ? modelChoiceKey(match) : null;
  }).filter(Boolean))];
}
