// Server-side import-batch dismissal (memory-import-review-fixes S01,
// RF-D03). The banner's dismiss button POSTs the dismiss endpoint for every
// pending batch so the queue leaves list_pending across reloads instead of
// hiding client-side and reappearing on the next Memory panel load. A wipe
// that predates the import-queue reset fix can leave several awaiting_review
// batches behind, so dismissal covers the whole list in one click; batches
// still processing (state active) are left for the banner to keep watching.
// fetch/toasts/origin are injected so the node harness in tests/js drives it
// without a browser — the same injection idiom as memoryExportDialog.js.
// memory.js wires it with the real globals.

import { responseError } from './util/httpError.js';

async function dismissOne(fetchImpl, origin, batchId) {
  const response = await fetchImpl(
    `${origin}/api/memory/import-batches/${encodeURIComponent(batchId)}/dismiss`,
    { method: 'POST', credentials: 'same-origin' },
  );
  if (!response.ok) throw new Error((await responseError(response, 'Import dismiss failed')).message);
}

export async function dismissAllPendingImportBatches({
  fetchImpl = fetch,
  origin = '',
  showToast = () => {},
  showError = () => {},
} = {}) {
  try {
    const listResponse = await fetchImpl(
      `${origin}/api/memory/import-batches`,
      { credentials: 'same-origin' },
    );
    if (!listResponse.ok) {
      throw new Error((await responseError(listResponse, 'Import dismiss failed')).message);
    }
    const batches = (await listResponse.json())?.batches;
    const dismissable = (Array.isArray(batches) ? batches : [])
      .filter((batch) => batch?.batch_id && String(batch?.state) === 'awaiting_review');
    for (const batch of dismissable) {
      await dismissOne(fetchImpl, origin, batch.batch_id);
    }
    if (dismissable.length) {
      showToast(`Dismissed ${dismissable.length} import batch${dismissable.length === 1 ? '' : 'es'}`);
    }
    return true;
  } catch (error) {
    // Failure keeps the banner visible so the owner can retry dismissal.
    showError(error?.message || 'Import dismiss failed');
    return false;
  }
}
