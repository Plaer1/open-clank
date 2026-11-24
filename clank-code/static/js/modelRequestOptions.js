// Narrow UI contract for route-bound model request options.
// No selection changes provider/model route identity.

function descriptor(model, id) {
  const catalogRevision = model?.catalogRevision ?? model?.catalog_revision;
  const optionRouteId = String(model?.optionRouteId || model?.option_route_id
    || model?.provider_model_route_id || '').trim();
  if (!optionRouteId || !Number.isSafeInteger(catalogRevision) || catalogRevision < 1) return null;
  const entries = model?.capabilities?.request_options;
  if (!Array.isArray(entries)) return null;
  const found = entries.find(item => item && item.id === id && item.type === 'enum'
    && item.catalog_revision === catalogRevision);
  if (!found || !Array.isArray(found.values)) return null;
  const values = [...new Set(found.values.filter(value => (
    typeof value === 'string' && value.length > 0 && value.length <= 32
      && value !== 'inherit' && !/[\u0000-\u001f]/u.test(value)
  )))].slice(0, 32);
  if (!values.length) return null;
  return {
    id,
    type: 'enum',
    label: String(found.label || 'Thinking'),
    values,
    default: 'inherit',
    providerDefault: values.includes(found.provider_default) ? found.provider_default : null,
    routeId: optionRouteId,
    catalogRevision,
  };
}

export function reasoningEffortOption(model) {
  return descriptor(model, 'reasoning_effort');
}

export function serializeReasoningEffortSelection(model, value) {
  const option = reasoningEffortOption(model);
  if (!option) throw new Error('Thinking options are unavailable for this model route.');
  if (value === undefined || value === null || value === 'default' || value === 'inherit') {
    return { route_id: option.routeId, catalog_revision: option.catalogRevision, options: {} };
  }
  if (typeof value !== 'string' || !option.values.includes(value)) {
    throw new Error('Thinking option is not supported by this model route.');
  }
  return {
    route_id: option.routeId,
    catalog_revision: option.catalogRevision,
    options: { reasoning_effort: value },
  };
}

export function restoreReasoningEffortSelection(model, selection) {
  const option = reasoningEffortOption(model);
  if (!option) return { status: 'unsupported', value: 'inherit' };
  if (selection == null) return { status: 'default', value: 'inherit' };
  if (selection.route_id !== option.routeId
    || !Number.isSafeInteger(selection.catalog_revision)
    || selection.catalog_revision !== option.catalogRevision) {
    return { status: 'stale', value: 'inherit' };
  }
  if (selection.options && typeof selection.options === 'object'
    && !Array.isArray(selection.options)
    && Object.keys(selection.options).length === 0) {
    return { status: 'default', value: 'inherit' };
  }
  const value = selection.options?.reasoning_effort;
  if (typeof value !== 'string' || !option.values.includes(value)) {
    return { status: 'stale', value: 'inherit' };
  }
  return { status: 'selected', value };
}
