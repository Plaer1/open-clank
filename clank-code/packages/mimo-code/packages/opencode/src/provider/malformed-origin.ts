/** Private witnesses for the pinned managed Responses factory and its models. */
const sdks = new WeakSet<object>()
const models = new WeakSet<object>()
export function notePinnedResponsesSDK(value: object) { sdks.add(value) }
export function notePinnedResponsesModel(sdk: object, model: object) { if (sdks.has(sdk)) models.add(model) }
export function isPinnedResponsesModel(model: object) { return models.has(model) }

/** Separate official Chat factory identity; never inferred from model labels. */
const chatSDKs = new WeakSet<object>()
const chatModels = new WeakSet<object>()
export function notePinnedChatSDK(value:object) { chatSDKs.add(value) }
export function notePinnedChatModel(sdk:object, model:object) { if (chatSDKs.has(sdk)) chatModels.add(model) }
export function isPinnedChatModel(model:object) { return chatModels.has(model) }

/** Separate bundled compatible-chat factory/model; endpoint is protocol evidence only. */
const compatibleChatSDKs = new WeakMap<object, string>()
const compatibleChatModels = new WeakMap<object, string>()
export function notePinnedCompatibleChatSDK(value: object, endpoint: string) { compatibleChatSDKs.set(value, endpoint) }
export function notePinnedCompatibleChatModel(sdk: object, model: object) { const endpoint=compatibleChatSDKs.get(sdk); if (endpoint) compatibleChatModels.set(model, endpoint) }
export function pinnedCompatibleChatEndpoint(model: object) { return compatibleChatModels.get(model) }
