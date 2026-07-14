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
