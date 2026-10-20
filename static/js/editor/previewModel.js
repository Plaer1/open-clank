// Preview presentation helpers shared by Files and future provider adapters.
// This module is deliberately authority-neutral: server-supplied preview
// descriptors are authoritative, while MIME/extension classification is only a
// UI hint for deciding which control to offer.

import { isTextPath } from './entryModel.js';

const IMAGE_EXTENSIONS = /\.(png|jpe?g|gif|webp|avif|bmp|tiff?)$/i;
const AUDIO_EXTENSIONS = /\.(mp3|wav|flac|ogg|opus|m4a|aac|wma)$/i;

export function previewMime(entry = {}) {
  return String(entry.mime_type || entry.mimeType || entry.media_type || entry.mime || '').trim().toLowerCase();
}

export function previewDescriptor(entry = {}) {
  const descriptor = entry.preview || entry.preview_descriptor || entry.previewDescriptor;
  return descriptor && typeof descriptor === 'object' && !Array.isArray(descriptor) ? descriptor : null;
}

export function previewKind(entry, path = '') {
  const descriptor = previewDescriptor(entry);
  const explicit = String(descriptor?.kind || entry.preview_kind || '').toLowerCase();
  if (descriptor?.allowed === false || entry.previewable === false) return null;
  if (explicit === 'image' || explicit === 'audio' || explicit === 'text') return explicit;
  const mime = previewMime(entry);
  const name = String(entry?.name || path || '').toLowerCase();
  if (mime.startsWith('image/') || IMAGE_EXTENSIONS.test(name)) return 'image';
  if (mime.startsWith('audio/') || AUDIO_EXTENSIONS.test(name)) return 'audio';
  if (mime.startsWith('text/') || isTextPath(name)) return 'text';
  return null;
}

export function isPreviewable(entry, path = '') {
  return previewKind(entry, path) !== null;
}

export function boundedTextPreview(text, metadata = {}, limit = 320_000) {
  const value = String(text || '');
  if (value.length <= limit) return { text: value, truncated: false };
  const head = Math.floor(limit * 0.72);
  const tail = Math.max(0, limit - head);
  const size = Number(metadata.size || value.length);
  const formatted = Number.isFinite(size) && size >= 1024
    ? `${(size / (1024 ** Math.floor(Math.log(size) / Math.log(1024)))).toFixed(1)} ${['B', 'KB', 'MB', 'GB', 'TB'][Math.min(4, Math.floor(Math.log(size) / Math.log(1024)))]}`
    : `${size || 0} B`;
  return {
    text: `${value.slice(0, head)}\n\n… [preview truncated; ${formatted} total] …\n\n${value.slice(-tail)}`,
    truncated: true,
  };
}

function abortError() {
  return new DOMException('The operation was aborted.', 'AbortError');
}

function managedTextEncoding(bytes, contentType = '') {
  if (bytes.length >= 3 && bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf) return 'utf-8';
  if (bytes.length >= 2 && bytes[0] === 0xff && bytes[1] === 0xfe) return 'utf-16le';
  if (bytes.length >= 2 && bytes[0] === 0xfe && bytes[1] === 0xff) return 'utf-16be';
  const charset = /charset\s*=\s*["']?([^;"'\s]+)/i.exec(String(contentType || ''))?.[1];
  return charset || 'utf-8';
}

function sampledTextIsBinary(text) {
  for (const character of String(text || '')) {
    const code = character.codePointAt(0);
    if (code === 0) return true;
    if (code < 0x20 && ![0x09, 0x0a, 0x0c, 0x0d, 0x1b].includes(code)) return true;
    if (code === 0x7f) return true;
  }
  return false;
}

/**
 * Consume a managed-provider text response without ever retaining an
 * unbounded Blob/ArrayBuffer in the renderer. A response without a readable
 * stream is refused so the caller can offer the normal download path.
 */
export async function readBoundedTextResponse(response, { limit = 320_000, signal = null } = {}) {
  const byteLimit = Math.max(1, Math.min(512 * 1024, Number(limit) || 320_000));
  if (signal?.aborted) throw abortError();
  if (!response?.body?.getReader) {
    throw new Error('Streaming preview is unavailable; download the file to view it safely.');
  }
  const reader = response.body.getReader();
  const retained = new Uint8Array(byteLimit);
  let retainedBytes = 0;
  let streamDone = false;
  let truncated = false;
  const contentLengthValue = Number(response.headers?.get?.('content-length'));
  const contentEncoding = String(response.headers?.get?.('content-encoding') || '').trim().toLowerCase();
  const declaredSize = (!contentEncoding || contentEncoding === 'identity')
    && Number.isSafeInteger(contentLengthValue) && contentLengthValue >= 0
    ? contentLengthValue
    : null;
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal?.addEventListener('abort', abort, { once: true });
  try {
    while (!streamDone) {
      const chunk = await reader.read();
      if (signal?.aborted) throw abortError();
      streamDone = Boolean(chunk.done);
      if (streamDone) break;
      const bytes = chunk.value instanceof Uint8Array ? chunk.value : new Uint8Array(chunk.value || 0);
      const remaining = byteLimit - retainedBytes;
      if (bytes.byteLength > remaining) {
        retained.set(bytes.subarray(0, remaining), retainedBytes);
        retainedBytes += remaining;
        truncated = true;
        await reader.cancel('text preview byte limit reached').catch(() => {});
        break;
      }
      retained.set(bytes, retainedBytes);
      retainedBytes += bytes.byteLength;
      if (retainedBytes === byteLimit && declaredSize !== null && declaredSize > byteLimit) {
        truncated = true;
        await reader.cancel('text preview byte limit reached').catch(() => {});
        break;
      }
      if (retainedBytes === byteLimit) {
        // With no trustworthy length, one additional stream observation is
        // required to distinguish an exact-limit response from truncation.
        const probe = await reader.read();
        if (signal?.aborted) throw abortError();
        streamDone = Boolean(probe.done);
        truncated = !streamDone;
        if (truncated) await reader.cancel('text preview byte limit reached').catch(() => {});
      }
    }
  } finally {
    signal?.removeEventListener('abort', abort);
  }

  const bytes = retained.subarray(0, retainedBytes);
  const encoding = managedTextEncoding(bytes, response.headers?.get?.('content-type'));
  let text;
  try {
    // A truncated stream may end partway through a code point. `stream: true`
    // preserves the valid prefix without replacing an incomplete suffix;
    // invalid bytes in the retained sample still fail because `fatal` is set.
    text = new TextDecoder(encoding, { fatal: true }).decode(bytes, { stream: truncated });
  } catch {
    throw new Error('This managed file is not supported as text; download it instead.');
  }
  if (sampledTextIsBinary(text)) {
    throw new Error('This managed file appears to be binary; download it instead.');
  }
  return {
    text,
    encoding,
    truncated,
    tailIncluded: false,
    bytesRead: retainedBytes,
    size: truncated ? declaredSize : retainedBytes,
  };
}
