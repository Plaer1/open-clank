const PREFIX = 'openclank:chat-input-delivery:v1'
const MAX_LOCAL_RECEIPTS = 100

function part(value, name) {
  const text = String(value ?? '').trim()
  if (!text) throw new TypeError(`${name} is required`)
  return encodeURIComponent(text)
}

export function receiptStorageKey(owner, chatID, requestID) {
  return `${PREFIX}:${part(owner, 'owner')}:${part(chatID, 'chatID')}:${part(requestID, 'requestID')}`
}

export function ownerStoragePrefix(owner) {
  return `${PREFIX}:${part(owner, 'owner')}:`
}

export function createRequestID(cryptoObject = globalThis.crypto) {
  if (cryptoObject && typeof cryptoObject.randomUUID === 'function') return cryptoObject.randomUUID()
  if (cryptoObject && typeof cryptoObject.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    cryptoObject.getRandomValues(bytes)
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  throw new Error('Secure request ID generation is unavailable')
}

function validReceipt(value) {
  return !!value && typeof value === 'object'
    && typeof value.owner === 'string' && value.owner.trim() === value.owner && value.owner.length > 0
    && typeof value.chatID === 'string' && value.chatID.trim() === value.chatID && value.chatID.length > 0
    && value.chatID.length <= 200
    && typeof value.requestID === 'string' && value.requestID.trim() === value.requestID && value.requestID.length > 0
    && value.requestID.length <= 200
    && typeof value.text === 'string' && value.text.length <= 200_000
    && Array.isArray(value.uploadIDs) && value.uploadIDs.length <= 100
    && value.uploadIDs.every((item) => typeof item === 'string' && item.trim() === item && item.length > 0 && item.length <= 200)
    && (value.questionRefs === undefined || (Array.isArray(value.questionRefs) && value.questionRefs.length <= 20
      && value.questionRefs.every((item) => typeof item === 'string' && item.length <= 300)))
    && (value.questionAnswers === undefined || (Array.isArray(value.questionAnswers) && value.questionAnswers.length <= 20
      && value.questionAnswers.every((item) => item && typeof item === 'object' && typeof item.requestID === 'string'
        && Number.isSafeInteger(item.questionRevision) && (item.disposition === 'answered' || item.disposition === 'dismissed')
        && (item.answers === undefined || (Array.isArray(item.answers) && item.answers.length <= 20
          && item.answers.every((answer) => Array.isArray(answer) && answer.length <= 32
            && answer.every((value) => typeof value === 'string' && value.length <= 2000)))))))
    && (value.turnPolicy === undefined || value.turnPolicy === null || validTurnPolicy(value.turnPolicy))
    && ((value.expectedMapRevision === undefined && value.expectedExecutionRevision === undefined) ||
        (Number.isSafeInteger(value.expectedMapRevision) && value.expectedMapRevision >= 0 && Number.isSafeInteger(value.expectedExecutionRevision) && value.expectedExecutionRevision >= 0))
    && (value.mode === 'steer' || value.mode === 'after-turn')
}

function cleanQueueDisposition(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  if (value.state === 'unknown') return {state: 'unknown'}
  if (!['accepted', 'claimed', 'settled', 'cancelled', 'rejected'].includes(value.state)
      || typeof value.receiptID !== 'string' || !value.receiptID || value.receiptID.length > 256
      || typeof value.delivered !== 'boolean') return null
  if (value.deliveryMessageID !== undefined && (typeof value.deliveryMessageID !== 'string' || !value.deliveryMessageID || value.deliveryMessageID.length > 256)) return null
  if (value.outcome !== undefined && !['success', 'assistant_error', 'interrupted', 'never_ran'].includes(value.outcome)) return null
  return {state: value.state, receiptID: value.receiptID, delivered: value.delivered,
    ...(value.outcome !== undefined ? {outcome: value.outcome} : {}),
    ...(value.deliveryMessageID !== undefined ? {deliveryMessageID: value.deliveryMessageID} : {})}
}

function validTurnPolicy(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const keys = Object.keys(value).sort()
  const expected = ['allowBash', 'allowWebSearch', 'compareMode', 'incognito', 'mode', 'noMemory', 'useResearch', 'useWeb', 'version'].sort()
  return keys.length === expected.length && keys.every((key, index) => key === expected[index])
    && value.version === 1
    && ['agent', 'chat', 'plan'].includes(value.mode)
    && (value.allowBash === null || typeof value.allowBash === 'boolean')
    && (value.allowWebSearch === null || typeof value.allowWebSearch === 'boolean')
    && ['useWeb', 'useResearch', 'compareMode', 'incognito', 'noMemory'].every((key) => typeof value[key] === 'boolean')
}

export function saveReceipt(storage, value) {
  if (!validReceipt(value)) throw new TypeError('Invalid chat input receipt')
  const safe = {
    version: 1,
    owner: value.owner,
    chatID: value.chatID,
    requestID: value.requestID,
    text: value.text,
    uploadIDs: [...value.uploadIDs],
    mode: value.mode,
    state: String(value.state || 'persisted'),
    deliveryState: String(value.deliveryState || ''),
    createdAt: Number.isSafeInteger(value.createdAt) ? value.createdAt : Date.now(),
    updatedAt: Date.now(),
    ...(cleanQueueDisposition(value.queueDisposition) ? { queueDisposition: cleanQueueDisposition(value.queueDisposition) } : {}),
    ...(typeof value.handoffID === 'string' && value.handoffID ? { handoffID: value.handoffID } : {}),
    ...(typeof value.deliveryReason === 'string' && value.deliveryReason ? { deliveryReason: value.deliveryReason.slice(0, 240) } : {}),
    ...(validModelOptions(value.modelOptions) ? { modelOptions: {
      route_id: value.modelOptions.route_id,
      catalog_revision: value.modelOptions.catalog_revision,
      options: { ...value.modelOptions.options },
    } } : {}),
    ...(Array.isArray(value.questionRefs) ? { questionRefs: [...value.questionRefs] } : {}),
    ...(Array.isArray(value.questionAnswers) ? { questionAnswers: structuredClone(value.questionAnswers) } : {}),
    ...(typeof value.actorID === 'string' && value.actorID ? { actorID: value.actorID } : {}),
    ...(typeof value.actorSessionID === 'string' && value.actorSessionID ? { actorSessionID: value.actorSessionID } : {}),
    ...(Number.isSafeInteger(value.expectedMapRevision) && Number.isSafeInteger(value.expectedExecutionRevision) ? { expectedMapRevision: value.expectedMapRevision, expectedExecutionRevision: value.expectedExecutionRevision } : {}),
    ...(value.turnPolicy && validTurnPolicy(value.turnPolicy) ? { turnPolicy: structuredClone(value.turnPolicy) } : {}),
  }
  storage.setItem(receiptStorageKey(safe.owner, safe.chatID, safe.requestID), JSON.stringify(safe))
  return safe
}

function validModelOptions(value) {
  if (value === undefined || value === null) return false
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  if (typeof value.route_id !== 'string' || !value.route_id || !Number.isSafeInteger(value.catalog_revision) || value.catalog_revision < 1) return false
  if (!value.options || typeof value.options !== 'object' || Array.isArray(value.options)) return false
  const keys = Object.keys(value.options)
  return keys.length <= 1 && keys.every((key) => key === 'reasoning_effort' && typeof value.options[key] === 'string' && value.options[key].length <= 32)
}

export function readReceipts(storage, owner, chatID) {
  const prefix = `${ownerStoragePrefix(owner)}${part(chatID, 'chatID')}:`
  const receipts = []
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (!key || !key.startsWith(prefix)) continue
    try {
      const value = JSON.parse(storage.getItem(key))
      if (validReceipt(value) && value.owner === owner && value.chatID === chatID) receipts.push(value)
    } catch (_) {}
  }
  return receipts.sort((a, b) => a.createdAt - b.createdAt).slice(-MAX_LOCAL_RECEIPTS)
}

export function removeReceipt(storage, owner, chatID, requestID) {
  storage.removeItem(receiptStorageKey(owner, chatID, requestID))
}

export function clearOwnerReceipts(storage, owner) {
  const prefix = ownerStoragePrefix(owner)
  const keys = []
  for (let index = 0; index < storage.length; index++) {
    const key = storage.key(index)
    if (key?.startsWith(prefix)) keys.push(key)
  }
  for (const key of keys) storage.removeItem(key)
  return keys.length
}

export function deliveryStatus(payload) {
  const receipt = payload?.receipt || {}
  const deliveryState = String(payload?.deliveryState || '')
  const queue = receipt.queueDisposition
  if (receipt.state === 'consumed') {
    if (queue?.state === 'cancelled' && queue?.outcome === 'never_ran' && queue?.delivered === false && typeof queue?.receiptID === 'string' && queue.receiptID) return 'cancelled'
    if (['accepted', 'claimed', 'settled'].includes(queue?.state) && queue?.delivered === true && typeof queue?.receiptID === 'string' && queue.receiptID && typeof queue?.deliveryMessageID === 'string' && queue.deliveryMessageID) return 'queue-delivered'
    return 'queued'
  }
  if (deliveryState === 'consumed') return 'queued'
  if (receipt.state === 'cancelled' || deliveryState === 'cancelled') return 'cancelled'
  if (receipt.state === 'failed' || deliveryState === 'unavailable') return 'unavailable'
  if (deliveryState === 'pending-boundary') return 'pending-boundary'
  if (payload?.delivery === 'started' || payload?.delivery === 'acknowledged') return 'persisted'
  return 'unknown'
}
