/** Shared opaque cross-app handoff into the unified Files window. */
import { filesFacadeClient } from './filesFacadeClient.js';

export async function showResourceInFiles(resourceRef) {
  const value = String(resourceRef || '').trim();
  if (!value) throw new TypeError('Files resource reference is required');
  const installed = globalThis.filesModule;
  const files = installed ? null : await import('./files.js');
  const reveal = installed?.revealResource || files?.revealResource || files?.default?.revealResource;
  if (typeof reveal !== 'function') throw new Error('Files integration is unavailable');
  // Files reveal validates the current exact opaque identity in its facade.
  // Its stale-ref fallback renews eligible managed resources; Host refs have
  // no renewal lane and must reach reveal directly while still authorized.
  return Boolean(await reveal(value));
}

/** Mint a current ref after the provider reauthorizes an exact managed item. */
export async function reissueExactResourceRef(resourceRef) {
  const value = String(resourceRef || '').trim();
  if (!value) throw new TypeError('Files resource reference is required');
  const response = await filesFacadeClient.reissue(value);
  const renewed = String(response?.resource?.ref || '').trim();
  if (!renewed) throw new Error('Files resource could not be renewed');
  return renewed;
}

/**
 * Return the bounded, read-only URLs for an exact Files resource.  Callers
 * must use the returned ref for both URLs; the original ref is intentionally
 * not retained as an authorization substitute.
 */
export async function exactResourceUrls(resourceRef) {
  const renewed = await reissueExactResourceRef(resourceRef);
  return Object.freeze({
    resourceRef: renewed,
    resource_ref: renewed,
    previewUrl: filesFacadeClient.contentUrl(renewed, { purpose: 'preview' }),
    downloadUrl: filesFacadeClient.contentUrl(renewed, { purpose: 'download' }),
  });
}

/** Download an exact-view resource through a just-in-time current ref. */
export async function downloadExactResource(resourceRef, filename = 'download') {
  const renewed = await reissueExactResourceRef(resourceRef);
  const link = document.createElement('a');
  link.href = filesFacadeClient.contentUrl(renewed, { purpose: 'download' });
  link.download = String(filename || 'download');
  link.rel = 'noopener';
  document.body.append(link);
  link.click();
  link.remove();
  return renewed;
}

export default { showResourceInFiles, reissueExactResourceRef, exactResourceUrls, downloadExactResource };
