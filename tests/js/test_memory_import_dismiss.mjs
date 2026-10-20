import assert from 'node:assert/strict';
import test from 'node:test';

import { dismissAllPendingImportBatches } from '../../static/js/memoryImportDismiss.js';

function okResponse(payload = {}) {
  return { ok: true, status: 200, json: async () => payload };
}

function listResponse(batches) {
  return okResponse({ contract: 'openclank.memory-import-batch-list/v1', batches });
}

test('dismiss POSTs every awaiting_review batch and skips active ones', async () => {
  const calls = [];
  const toasts = [];
  const errors = [];
  const result = await dismissAllPendingImportBatches({
    origin: 'https://example.test',
    fetchImpl: async (url, opts) => {
      calls.push([url, opts]);
      if (url.endsWith('/api/memory/import-batches')) {
        return listResponse([
          { batch_id: 'batch_one', state: 'awaiting_review' },
          { batch_id: 'batch_two', state: 'awaiting_review' },
          { batch_id: 'batch_busy', state: 'active' },
        ]);
      }
      return okResponse();
    },
    showToast: (msg) => toasts.push(msg),
    showError: (msg) => errors.push(msg),
  });
  assert.equal(result, true);
  assert.deepEqual(calls, [
    ['https://example.test/api/memory/import-batches', { credentials: 'same-origin' }],
    ['https://example.test/api/memory/import-batches/batch_one/dismiss', { method: 'POST', credentials: 'same-origin' }],
    ['https://example.test/api/memory/import-batches/batch_two/dismiss', { method: 'POST', credentials: 'same-origin' }],
  ], 'every review-waiting batch is dismissed; the still-processing batch is left alone');
  assert.deepEqual(toasts, ['Dismissed 2 import batches']);
  assert.deepEqual(errors, []);
});

test('dismiss toasts the decoded typed error on rejection', async () => {
  const toasts = [];
  const errors = [];
  const result = await dismissAllPendingImportBatches({
    origin: 'https://example.test',
    fetchImpl: async (url) => {
      if (url.endsWith('/api/memory/import-batches')) {
        return listResponse([{ batch_id: 'batch_one', state: 'awaiting_review' }]);
      }
      return {
        ok: false,
        status: 409,
        headers: { get: () => 'application/json' },
        text: async () => JSON.stringify({ detail: {
          code: 'invalid_batch_state',
          message: 'Only an import batch waiting for review can be dismissed.',
          retryable: false,
        } }),
      };
    },
    showToast: (msg) => toasts.push(msg),
    showError: (msg) => errors.push(msg),
  });
  assert.equal(result, false);
  assert.deepEqual(errors, ['Only an import batch waiting for review can be dismissed.']);
  assert.deepEqual(toasts, [], 'failure never reads as a success toast');
});

test('a network failure falls back to the thrown message', async () => {
  const errors = [];
  const result = await dismissAllPendingImportBatches({
    origin: 'https://example.test',
    fetchImpl: async () => { throw new TypeError('fetch failed'); },
    showError: (msg) => errors.push(msg),
  });
  assert.equal(result, false);
  assert.deepEqual(errors, ['fetch failed']);
});

test('an empty pending list is a quiet success', async () => {
  const toasts = [];
  const errors = [];
  const result = await dismissAllPendingImportBatches({
    origin: 'https://example.test',
    fetchImpl: async () => listResponse([]),
    showToast: (msg) => toasts.push(msg),
    showError: (msg) => errors.push(msg),
  });
  assert.equal(result, true);
  assert.deepEqual(toasts, []);
  assert.deepEqual(errors, []);
});
