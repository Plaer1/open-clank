// Private provenance of models created by the pinned bundled factory.
const sdks = new WeakSet<object>()
const models = new WeakSet<object>()
export function notePinnedAnthropicSDK(value: object) { sdks.add(value) }
export function notePinnedAnthropicModel(sdk: object, model: object) { if (sdks.has(sdk)) models.add(model) }
export function isPinnedAnthropicModel(model: object) { return models.has(model) }
