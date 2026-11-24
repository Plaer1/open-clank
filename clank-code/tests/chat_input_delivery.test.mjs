import assert from 'node:assert/strict'
import test from 'node:test'
import {
  clearOwnerReceipts,
  createRequestID,
  deliveryStatus,
  readReceipts,
  receiptStorageKey,
  saveReceipt,
} from '../static/js/chatInputDelivery.js'

class MemoryStorage {
  values = new Map()
  get length() { return this.values.size }
  key(index) { return [...this.values.keys()][index] ?? null }
  getItem(key) { return this.values.get(key) ?? null }
  setItem(key, value) { this.values.set(String(key), String(value)) }
  removeItem(key) { this.values.delete(String(key)) }
}

test('receipt storage is scoped by owner, chat and stable request ID', () => {
  const storage = new MemoryStorage()
  saveReceipt(storage, { owner: 'alice', chatID: 'chat-a', requestID: 'same-id', text: 'one', uploadIDs: [], mode: 'steer' })
  saveReceipt(storage, { owner: 'alice', chatID: 'chat-b', requestID: 'same-id', text: 'two', uploadIDs: [], mode: 'steer' })
  saveReceipt(storage, { owner: 'bob', chatID: 'chat-a', requestID: 'same-id', text: 'three', uploadIDs: [], mode: 'steer' })

  assert.equal(readReceipts(storage, 'alice', 'chat-a').length, 1)
  assert.equal(readReceipts(storage, 'alice', 'chat-a')[0].text, 'one')
  assert.equal(readReceipts(storage, 'alice', 'chat-b')[0].text, 'two')
  assert.equal(readReceipts(storage, 'bob', 'chat-a')[0].text, 'three')
  assert.notEqual(receiptStorageKey('alice', 'chat-a', 'same-id'), receiptStorageKey('bob', 'chat-a', 'same-id'))
})

test('unknown outcome retry retains one request ID, text and upload IDs', () => {
  const storage = new MemoryStorage()
  const original = saveReceipt(storage, {
    owner: 'alice', chatID: 'chat-a', requestID: 'stable', text: 'keep this draft',
    uploadIDs: ['upload-1'], mode: 'steer', state: 'unknown',
  })
  saveReceipt(storage, { ...original, state: 'pending-boundary', deliveryState: 'pending-boundary' })
  const [retry] = readReceipts(storage, 'alice', 'chat-a')

  assert.equal(retry.requestID, 'stable')
  assert.equal(retry.text, 'keep this draft')
  assert.deepEqual(retry.uploadIDs, ['upload-1'])
  assert.equal(retry.state, 'pending-boundary')
})

test('turn policy choices survive local retry while legacy receipts remain absent', () => {
  const storage = new MemoryStorage()
  const turnPolicy = {
    version: 1, mode: 'plan', allowBash: false, allowWebSearch: false,
    useWeb: true, useResearch: false, compareMode: false, incognito: false, noMemory: false,
  }
  saveReceipt(storage, {
    owner: 'alice', chatID: 'chat-a', requestID: 'policy-id', text: 'plan this',
    uploadIDs: [], mode: 'after-turn', turnPolicy,
  })
  saveReceipt(storage, {
    owner: 'alice', chatID: 'chat-a', requestID: 'legacy-id', text: 'old',
    uploadIDs: [], mode: 'steer', turnPolicy: null,
  })
  const receipts = readReceipts(storage, 'alice', 'chat-a')
  assert.deepEqual(receipts.find((item) => item.requestID === 'policy-id').turnPolicy, turnPolicy)
  assert.equal(Object.hasOwn(receipts.find((item) => item.requestID === 'legacy-id'), 'turnPolicy'), false)
  assert.throws(() => saveReceipt(storage, {
    owner: 'alice', chatID: 'chat-a', requestID: 'bad', text: 'bad', uploadIDs: [], mode: 'steer',
    turnPolicy: { ...turnPolicy, unexpected: true },
  }), /Invalid chat input receipt/)
})

test('authentication change cleanup is owner-scoped', () => {
  const storage = new MemoryStorage()
  saveReceipt(storage, { owner: 'alice', chatID: 'chat-a', requestID: 'a', text: 'a', uploadIDs: [], mode: 'steer' })
  saveReceipt(storage, { owner: 'bob', chatID: 'chat-a', requestID: 'b', text: 'b', uploadIDs: [], mode: 'steer' })

  assert.equal(clearOwnerReceipts(storage, 'alice'), 1)
  assert.deepEqual(readReceipts(storage, 'alice', 'chat-a'), [])
  assert.equal(readReceipts(storage, 'bob', 'chat-a').length, 1)
})

test('request IDs require a secure random source and delivery state stays explicit', () => {
  const uuid = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'
  assert.equal(createRequestID({ randomUUID: () => uuid }), uuid)
  assert.throws(() => createRequestID({}), /Secure request ID/)
  assert.equal(deliveryStatus({ delivery: 'persisted', deliveryState: 'pending-boundary' }), 'pending-boundary')
  assert.equal(deliveryStatus({ receipt: { state: 'consumed' } }), 'queued')
  assert.equal(deliveryStatus({ receipt: { state: 'consumed', queueDisposition: {state: 'claimed', receiptID: 'q', delivered: true, deliveryMessageID: 'm'} } }), 'queue-delivered')
  assert.equal(deliveryStatus({ receipt: { state: 'consumed', queueDisposition: {state: 'cancelled', receiptID: 'q', delivered: false, outcome: 'never_ran'} } }), 'cancelled')
  assert.equal(deliveryStatus({ receipt: { state: 'consumed', queueDisposition: {state: 'claimed', receiptID: 'q', delivered: true} } }), 'queued')
  assert.equal(deliveryStatus({ delivery: 'started' }), 'persisted')
  assert.equal(deliveryStatus({}), 'unknown')
})
