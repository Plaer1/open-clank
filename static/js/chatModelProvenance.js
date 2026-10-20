/** Keep requested and actual model identity together across streamed rounds. */
export function applyModelRouteEventState(event, holder, roundHolder, defaultModel = '', sameModelName = (left, right) => left === right) {
  const target = typeof event?.round === 'number' && roundHolder ? roundHolder : holder;
  if (!target) return null;
  target._requestedModel = event.requested_model || event.selected_model || target._requestedModel || defaultModel;
  const hasResolvedActual = target._actualModel && !sameModelName(target._actualModel, target._requestedModel);
  target._actualModel = event.model || (hasResolvedActual ? target._actualModel : event.answered_by) || target._actualModel || target._requestedModel;
  return target;
}

/** Copy model identity into a newly-created continuation bubble. */
export function inheritModelRouteState(holder, roundHolder, target, defaultModel = '') {
  if (!target) return null;
  const source = roundHolder || holder;
  target._requestedModel = source?._requestedModel || defaultModel;
  target._actualModel = source?._actualModel || target._requestedModel;
  return target;
}

/** Apply final metrics to the active agent round, rather than its first bubble. */
export function applyModelMetricsState(metrics, holder, roundHolder, defaultModel = '') {
  const target = roundHolder || holder;
  if (!target || !metrics) return target || null;
  const roundModels = Array.isArray(metrics.round_models) ? metrics.round_models : [];
  const roundModel = roundHolder && roundModels.length ? roundModels[roundModels.length - 1] : null;
  target._requestedModel = metrics.requested_model || target._requestedModel || defaultModel;
  target._actualModel = roundModel || metrics.model || target._actualModel || target._requestedModel;
  return target;
}
