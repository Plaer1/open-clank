/**
 * Paste + drag-and-drop import handlers. Both add an image to the
 * editor as a new layer:
 *
 *   - Paste (Ctrl+V): checks `state.internalClipboard` first (set by
 *     lasso copy/cut), then falls back to the system clipboard's
 *     `image/*` items. Layer is named "Pasted Selection" or "Pasted"
 *     and becomes active; the tool snaps to Move so the user can
 *     reposition it immediately.
 *   - Drop: any `image/*` file dragged from the OS / another tab.
 *     Shows a "Drop image to add as new layer" overlay mid-drag. Each
 *     dropped image is routed through `handleImportedImage` so canvas-
 *     resize prompts + undo history work the same as the toolbar
 *     Import button.
 *
 * Both gated by `state.editorOpen` so they're inert when the editor
 * is closed (other listeners on the page get first dibs).
 *
 * @param {{
 *   container:            HTMLElement,
 *   saveState:            (label?: string) => void,
 *   createLayer:          (name: string, w: number, h: number) => object,
 *   renderLayerPanel:     () => void,
 *   composite:            () => void,
 *   handleImportedImage:  (img: HTMLImageElement) => void,
 *   uiModule:             object,
 * }} deps
 */
import { state } from './state.js';

export const CANVAS_MAX_IMPORT_BYTES = 32 * 1024 * 1024;
export const CANVAS_MAX_IMPORT_DIMENSION = 8192;
export const CANVAS_MAX_IMPORT_PIXELS = 40_000_000;
export const FILES_CANVAS_IMAGE_MIME = 'application/vnd.openclank.files-image+json';
const CANVAS_PAYLOAD_MAX_BYTES = 64 * 1024;
const CANVAS_MAX_FIELD = 2048;
const CANVAS_MAX_EPOCH = 0x7fffffff;
const CANVAS_PAYLOAD_KEYS = new Set([
  'version', 'type', 'resource_key', 'resource_ref', 'revision', 'mime_type',
  'size', 'provider', 'capabilities', 'account', 'workspace', 'operation_id',
  'item_id', 'gesture_context',
]);
const GESTURE_KEYS = new Set([
  'commandId', 'generation', 'policyGeneration', 'selectionEpoch', 'owner',
  'workspace', 'pane', 'provider', 'parent', 'scopeKey', 'selectedKeys',
  'sourceCapabilities', 'allowCopy', 'allowMove',
]);
const CAPABILITY_KEYS = new Set(['read', 'open', 'download', 'export']);

const _imageMime = value => String(value || '').toLowerCase().startsWith('image/');
const _boundedText = value => String(value || '').replace(/[\r\n]+/g, ' ').slice(0, 120);

function _plain(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const proto = Object.getPrototypeOf(value);
  return proto === Object.prototype || proto === null;
}

function _safeText(value, label, { required = true, max = CANVAS_MAX_FIELD } = {}) {
  if (typeof value !== 'string' || value.length > max || /[\u0000-\u001f\u007f]/u.test(value)) {
    throw new TypeError(`${label} is invalid`);
  }
  if (required && !value) throw new TypeError(`${label} is required`);
  return value;
}

function _safeInteger(value, label, { min = 0, max = CANVAS_MAX_EPOCH } = {}) {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < min || value > max) {
    throw new TypeError(`${label} is invalid`);
  }
  return value;
}

function _exactKeys(value, allowed, label) {
  if (!_plain(value) || Object.keys(value).some(key => !allowed.has(key))) throw new TypeError(`${label} is invalid`);
}

function _freeze(value) {
  if (!value || typeof value !== 'object' || Object.isFrozen(value)) return value;
  for (const child of Object.values(value)) _freeze(child);
  return Object.freeze(value);
}

function _strictRevision(value, label = 'revision') {
  _exactKeys(value, new Set(['kind', 'value']), label);
  return _freeze({ kind: _safeText(value.kind, `${label}.kind`), value: _safeText(value.value, `${label}.value`, { max: 512 }) });
}

function _strictCapabilities(value, label = 'capabilities') {
  if (!Array.isArray(value) || value.length < 2 || value.length > CAPABILITY_KEYS.size) throw new TypeError(`${label} is invalid`);
  const seen = new Set();
  const result = [];
  for (const item of value) {
    const capability = _safeText(item, `${label} item`, { max: 32 }).toLowerCase();
    if (!CAPABILITY_KEYS.has(capability) || seen.has(capability)) throw new TypeError(`${label} is invalid`);
    seen.add(capability); result.push(capability);
  }
  if (!seen.has('read') || !(seen.has('export') || seen.has('download'))) throw new TypeError(`${label} is unauthorized`);
  return _freeze(result);
}

function _strictGesture(value, { account, workspace, provider, resourceKey } = {}) {
  _exactKeys(value, GESTURE_KEYS, 'gesture context');
  const result = {
    commandId: _safeText(value.commandId, 'gesture command'),
    generation: _safeInteger(value.generation, 'gesture generation'),
    policyGeneration: _safeInteger(value.policyGeneration, 'gesture policy generation'),
    selectionEpoch: _safeInteger(value.selectionEpoch, 'gesture selection epoch'),
    owner: _safeText(value.owner, 'gesture owner'),
    workspace: _safeText(value.workspace, 'gesture workspace'),
    pane: _safeText(value.pane, 'gesture pane'),
    provider: _safeText(value.provider, 'gesture provider'),
    parent: _safeText(value.parent, 'gesture parent'),
    scopeKey: _safeText(value.scopeKey, 'gesture scope'),
    selectedKeys: null,
    sourceCapabilities: null,
    allowCopy: value.allowCopy,
    allowMove: value.allowMove,
  };
  if (typeof value.allowCopy !== 'boolean' || typeof value.allowMove !== 'boolean') throw new TypeError('gesture capabilities are invalid');
  if (result.owner !== account || result.workspace !== workspace || result.provider !== provider) throw new TypeError('gesture identity is invalid');
  if (!Array.isArray(value.selectedKeys) || value.selectedKeys.length !== 1 || typeof value.selectedKeys[0] !== 'string' || value.selectedKeys[0] !== resourceKey || value.selectedKeys.some(item => item.length > CANVAS_MAX_FIELD || /[\u0000-\u001f\u007f]/u.test(item))) throw new TypeError('gesture selection is invalid');
  result.selectedKeys = _freeze([value.selectedKeys[0]]);
  if (!_plain(value.sourceCapabilities) || Object.keys(value.sourceCapabilities).length !== 1 || !Object.hasOwn(value.sourceCapabilities, resourceKey)) throw new TypeError('gesture source capabilities are invalid');
  const capability = value.sourceCapabilities[resourceKey];
  _exactKeys(capability, CAPABILITY_KEYS, 'gesture source capabilities');
  if (Object.keys(capability).length !== CAPABILITY_KEYS.size || [...CAPABILITY_KEYS].some(key => !Object.hasOwn(capability, key)) || Object.values(capability).some(item => typeof item !== 'boolean') || capability.read !== true || !(capability.export === true || capability.download === true)) throw new TypeError('gesture source capabilities are invalid');
  result.sourceCapabilities = _freeze({ [resourceKey]: _freeze({ read: capability.read, open: capability.open, download: capability.download, export: capability.export }) });
  return _freeze(result);
}

function _canonicalPayload(value, { maxBytes = CANVAS_MAX_IMPORT_BYTES } = {}) {
  if (!_plain(value)) throw new TypeError('Files image payload is invalid');
  _exactKeys(value, CANVAS_PAYLOAD_KEYS, 'Files image payload');
  if (value.version !== 1 || value.type !== FILES_CANVAS_IMAGE_MIME) throw new TypeError('Files image payload is invalid');
  const account = _safeText(value.account, 'Files account');
  const workspace = _safeText(value.workspace, 'Files workspace');
  const provider = _safeText(value.provider, 'Files provider').toLowerCase();
  const resourceKey = _safeText(value.resource_key, 'Files resource key');
  const resourceRef = _safeText(value.resource_ref, 'Files resource ref');
  const mime = _safeText(value.mime_type, 'Files MIME type', { max: 128 }).toLowerCase();
  if (!/^image\/[a-z0-9][a-z0-9.+-]*$/u.test(mime)) throw new TypeError('Files image MIME type is invalid');
  const scopedPrefix = `${account}|${workspace}|${provider}:`;
  if (!resourceKey.startsWith(scopedPrefix) || resourceKey.slice(scopedPrefix.length).length < 1) throw new TypeError('Files resource key is not fully scoped');
  if (typeof value.size === 'number' && Number.isSafeInteger(value.size) && value.size > maxBytes) {
    throw new TypeError('Files image payload is oversized');
  }
  const size = _safeInteger(value.size, 'Files image size', { max: maxBytes });
  const capabilities = _strictCapabilities(value.capabilities);
  const revision = _strictRevision(value.revision);
  const operationId = _safeText(value.operation_id, 'Files operation id', { max: 128 });
  const itemId = _safeText(value.item_id, 'Files item id', { max: 128 });
  const gesture = _strictGesture(value.gesture_context, { account, workspace, provider, resourceKey });
  const sourceCapability = gesture.sourceCapabilities[resourceKey];
  if (sourceCapability.read !== capabilities.includes('read') || sourceCapability.open !== capabilities.includes('open') || sourceCapability.download !== capabilities.includes('download') || sourceCapability.export !== capabilities.includes('export')) throw new TypeError('gesture source capabilities do not match payload');
  return _freeze({ version: 1, type: FILES_CANVAS_IMAGE_MIME, resource_key: resourceKey, resource_ref: resourceRef, revision, mime_type: mime, size, provider, capabilities, account, workspace, operation_id: operationId, item_id: itemId, gesture_context: gesture });
}

function _duplicateFreeJson(raw) {
  let index = 0;
  const skip = () => { while (/\s/u.test(raw[index] || '')) index += 1; };
  const string = () => {
    const start = index;
    if (raw[index++] !== '"') throw new Error('invalid JSON');
    while (index < raw.length) {
      const ch = raw[index++];
      if (ch === '\\') { index += 1; continue; }
      if (ch === '"') return JSON.parse(raw.slice(start, index));
      if (ch < ' ') throw new Error('invalid JSON');
    }
    throw new Error('invalid JSON');
  };
  const value = () => {
    skip();
    if (raw[index] === '{') { index += 1; skip(); const keys = new Set(); if (raw[index] === '}') { index += 1; return; } while (true) { skip(); const key = string(); if (keys.has(key)) throw new Error('duplicate key'); keys.add(key); skip(); if (raw[index++] !== ':') throw new Error('invalid JSON'); value(); skip(); if (raw[index] === '}') { index += 1; return; } if (raw[index++] !== ',') throw new Error('invalid JSON'); } }
    if (raw[index] === '[') { index += 1; skip(); if (raw[index] === ']') { index += 1; return; } while (true) { value(); skip(); if (raw[index] === ']') { index += 1; return; } if (raw[index++] !== ',') throw new Error('invalid JSON'); } }
    if (raw[index] === '"') { string(); return; }
    const start = index; while (index < raw.length && !/[\s,\]}]/u.test(raw[index])) index += 1; if (start === index) throw new Error('invalid JSON');
  };
  value(); skip(); if (index !== raw.length) throw new Error('invalid JSON');
}

function _parsePayload(raw, limits) {
  if (typeof raw !== 'string' || new TextEncoder().encode(raw).byteLength > CANVAS_PAYLOAD_MAX_BYTES) throw new TypeError('Files image payload is oversized');
  _duplicateFreeJson(raw);
  return _canonicalPayload(JSON.parse(raw), limits);
}

/** Pure, O(1) classification used before a browser decoder or canvas alloc. */
export function classifyCanvasDrop(input, {
  maxBytes = CANVAS_MAX_IMPORT_BYTES,
  maxDimension = CANVAS_MAX_IMPORT_DIMENSION,
  maxPixels = CANVAS_MAX_IMPORT_PIXELS,
} = {}) {
  const dataTransfer = input?.dataTransfer || input || {};
  const types = Array.from(dataTransfer.types || []).map(value => String(value));
  const hasCanonical = types.includes(FILES_CANVAS_IMAGE_MIME);
  const hasLegacy = types.some(type => type === 'application/x-openclank-files-image' || type === 'application/vnd.openclank.files-image');
  if (hasCanonical || hasLegacy) {
    if (!hasCanonical || hasLegacy || types.filter(type => type === FILES_CANVAS_IMAGE_MIME).length !== 1) return { ok: false, kind: 'files-image', code: 'invalid_payload', reason: 'Canvas could not read that Files image handoff.' };
    try {
      const payload = _canonicalPayload(typeof dataTransfer.getData?.(FILES_CANVAS_IMAGE_MIME) === 'string' ? _parsePayload(dataTransfer.getData(FILES_CANVAS_IMAGE_MIME), { maxBytes }) : dataTransfer.getData?.(FILES_CANVAS_IMAGE_MIME), { maxBytes });
      return { ok: true, kind: 'files-image', payload, ref: payload.resource_ref, mime: payload.mime_type, maxDimension, maxPixels };
    } catch (error) {
      const code = /oversized|size/u.test(String(error?.message)) ? 'oversize' : /unauthorized|capabilities/u.test(String(error?.message)) ? 'unauthorized' : 'invalid_payload';
      return { ok: false, kind: 'files-image', code, reason: code === 'oversize' ? 'Canvas rejected the image because it exceeds the import limit.' : code === 'unauthorized' ? 'This Files image is no longer available to Canvas.' : 'Canvas could not read that Files image handoff.' };
    }
  }
  const items = Array.from(dataTransfer.items || []);
  const files = Array.from(dataTransfer.files || []);
  const directory = items.some(item => {
    try { return item.webkitGetAsEntry?.()?.isDirectory === true; } catch (_) { return false; }
  });
  if (directory) return { ok: false, kind: 'directory', code: 'directory', reason: 'Canvas accepts image resources only; folders are not imported.' };
  if (!files.length && items.some(item => String(item.kind || '').toLowerCase() === 'string')) {
    return { ok: false, kind: 'text', code: 'text', reason: 'Canvas accepts image resources only; text and code are not imported.' };
  }
  if (!files.length) return { ok: false, kind: 'unsupported', code: 'unsupported', reason: 'Canvas accepts image resources only.' };
  if (files.length !== 1) return { ok: false, kind: 'unsupported', code: 'multiple', reason: 'Canvas accepts one image at a time.' };
  const imageFiles = files.filter(file => _imageMime(file.type));
  if (imageFiles.length !== files.length) return { ok: false, kind: 'unsupported', code: 'unsupported', reason: 'Canvas accepts image resources only; that media type is unsupported.' };
  const oversized = imageFiles.find(file => Number(file.size) > maxBytes);
  if (oversized) return { ok: false, kind: 'oversize', code: 'oversize', reason: 'Canvas rejected the image because it exceeds the import limit.' };
  return { ok: true, kind: 'os-image', files: imageFiles, maxDimension, maxPixels };
}

export function canvasDropOperationKey(payload, index = 0) {
  if (payload?.operation_id) return `files:${String(payload.operation_id)}:${String(payload.item_id || index)}`;
  const file = payload?.files?.[index];
  if (!file) return '';
  return `os:${String(file.name || 'image')}:${Number(file.size) || 0}:${Number(file.lastModified) || 0}:${String(file.type || '')}`;
}

const _osDropReservations = new WeakMap();
let _osDropSequence = 0;

function osDropOperationKey(file, dataTransfer, index) {
  if (!dataTransfer || (typeof dataTransfer !== 'object' && typeof dataTransfer !== 'function')) return canvasDropOperationKey({ files: [file] }, 0);
  let reservation = _osDropReservations.get(dataTransfer);
  if (!reservation) { reservation = `os-event-${++_osDropSequence}`; _osDropReservations.set(dataTransfer, reservation); }
  return `${reservation}:${index}`;
}

/** Build the dedicated, unambiguous drag payload used by an authorized Files
 * image source.  This is a DTO helper; it does not grant or renew authority. */
export function createFilesCanvasImagePayload(resource, {
  account, workspace, operationId, itemId, context = null,
} = {}) {
  const value = resource && typeof resource === 'object' ? resource : {};
  if (!_plain(value)) throw new TypeError('an authorized Files image resource is required');
  const raw = {
    version: 1,
    type: FILES_CANVAS_IMAGE_MIME,
    resource_key: value.resource_key || value.resourceKey,
    resource_ref: value.resource_ref || value.ref,
    revision: value.revision,
    mime_type: value.mime_type || value.mime,
    size: value.size,
    provider: value.provider,
    capabilities: value.capabilities,
    account,
    workspace,
    operation_id: operationId,
    item_id: itemId,
    gesture_context: context,
  };
  try { return _canonicalPayload(raw); } catch (error) { throw new TypeError(error?.message || 'an authorized Files image resource is required'); }
}

export function canonicalGestureContext(context, identity = {}) {
  try {
    return _strictGesture(context, {
      account: identity.account ?? context?.owner,
      workspace: identity.workspace ?? context?.workspace,
      provider: identity.provider ?? context?.provider,
      resourceKey: identity.resourceKey ?? context?.selectedKeys?.[0],
    });
  } catch (_) { return null; }
}

export function sameGestureContext(actual, expected) {
  const a = canonicalGestureContext(actual);
  const b = canonicalGestureContext(expected);
  if (!a || !b) return false;
  const scalar = ['commandId', 'generation', 'policyGeneration', 'selectionEpoch', 'owner', 'workspace', 'pane', 'provider', 'parent', 'scopeKey', 'allowCopy', 'allowMove'];
  if (scalar.some(key => a[key] !== b[key])) return false;
  if (a.selectedKeys.length !== b.selectedKeys.length || a.selectedKeys.some((key, index) => key !== b.selectedKeys[index])) return false;
  const aCaps = a.sourceCapabilities[a.selectedKeys[0]], bCaps = b.sourceCapabilities[b.selectedKeys[0]];
  return CAPABILITY_KEYS.size === 4 && [...CAPABILITY_KEYS].every(key => aCaps[key] === bCaps[key]);
}

function _checkDecodedDimensions(width, height, { maxDimension, maxPixels }) {
  const w = Number(width), h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w < 1 || h < 1 || w > maxDimension || h > maxDimension || w * h > maxPixels) {
    throw Object.assign(new Error('Canvas rejected the image because its dimensions exceed the import limit.'), { code: 'oversize_dimensions' });
  }
  return { width: Math.round(w), height: Math.round(h) };
}

async function _blobBounded(response, maxBytes, signal = null) {
  if (!response?.ok) throw Object.assign(new Error('The image could not be read by the current account.'), { code: 'source_unavailable' });
  const advertised = Number(response.headers?.get?.('content-length') || 0);
  if (advertised > maxBytes) throw Object.assign(new Error('Canvas rejected the image because it exceeds the import limit.'), { code: 'oversize' });
  if (!response.body?.getReader) {
    const blob = await response.blob();
    if (blob.size > maxBytes) throw Object.assign(new Error('Canvas rejected the image because it exceeds the import limit.'), { code: 'oversize' });
    return blob;
  }
  const reader = response.body.getReader(), chunks = [];
  let total = 0;
  let complete = false;
  try {
    while (true) {
      if (signal?.aborted) throw Object.assign(new Error('Canvas image import was cancelled.'), { name: 'AbortError' });
      const next = await reader.read();
      if (next.done) break;
      total += next.value?.byteLength || 0;
      if (total > maxBytes) throw Object.assign(new Error('Canvas rejected the image because it exceeds the import limit.'), { code: 'oversize' });
      chunks.push(next.value);
    }
    complete = true;
  } finally {
    if (!complete || signal?.aborted) { try { await reader.cancel(); } catch (_) {} }
    try { reader.releaseLock(); } catch (_) {}
  }
  return new Blob(chunks, { type: response.headers?.get?.('content-type') || 'application/octet-stream' });
}

function _decodeBlob(blob, limits, { signal = null } = {}) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(Object.assign(new Error('Canvas image import was cancelled.'), { name: 'AbortError' }));
    if (typeof Image !== 'function' || typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') return reject(new Error('Canvas image decoder is unavailable.'));
    const url = URL.createObjectURL(blob);
    const img = new Image();
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      try { URL.revokeObjectURL(url); } catch (_) {}
      if (error) reject(error); else resolve(value);
    };
    img.onload = () => {
      if (signal?.aborted) return finish(Object.assign(new Error('Canvas image import was cancelled.'), { name: 'AbortError' }));
      try { _checkDecodedDimensions(img.naturalWidth || img.width, img.naturalHeight || img.height, limits); finish(null, img); }
      catch (error) { finish(error); }
    };
    img.onerror = () => finish(Object.assign(new Error('Canvas could not decode that image.'), { code: 'decode_failed' }));
    if (signal) signal.addEventListener('abort', () => finish(Object.assign(new Error('Canvas image import was cancelled.'), { name: 'AbortError' })), { once: true });
    img.src = url;
  });
}

/**
 * Files image → Canvas.  This is deliberately a read handoff: it never calls
 * a Files mutation and the local layer id is never sent as a ResourceRef.
 */
export async function importFilesImageToCanvas(payload, {
  filesClient,
  fetchImpl = globalThis.fetch?.bind(globalThis),
  signal = null,
  context = null,
  maxBytes = CANVAS_MAX_IMPORT_BYTES,
  maxDimension = CANVAS_MAX_IMPORT_DIMENSION,
  maxPixels = CANVAS_MAX_IMPORT_PIXELS,
  onImage,
  getContext = null,
} = {}) {
  const classification = classifyCanvasDrop({ types: [FILES_CANVAS_IMAGE_MIME], getData: () => JSON.stringify(payload) }, { maxBytes, maxDimension, maxPixels });
  if (!classification.ok) throw Object.assign(new Error(classification.reason), { code: classification.code });
  if (!filesClient || typeof filesClient.openResource !== 'function' || typeof filesClient.contentUrl !== 'function') throw new Error('Files image handoff is unavailable.');
  const canonical = classification.payload;
  const capturedContext = canonical.gesture_context;
  const captured = { account: canonical.account, workspace: canonical.workspace, provider: canonical.provider, resourceKey: canonical.resource_key, resourceRef: canonical.resource_ref, revision: canonical.revision, mime: canonical.mime_type, capabilities: canonical.capabilities };
  const assertLive = (stage) => {
    if (signal?.aborted) throw Object.assign(new Error('Canvas image import was cancelled.'), { name: 'AbortError' });
    if (typeof getContext !== 'function') throw Object.assign(new Error('File access changed; choose the image again.'), { code: stage === 'gesture' ? 'stale_gesture' : 'stale_context' });
    const live = getContext();
    if (!live || !sameGestureContext(capturedContext, live)) throw Object.assign(new Error('The Files gesture changed; choose the image again.'), { code: 'stale_gesture' });
    if (live.owner !== captured.account || live.workspace !== captured.workspace || live.provider !== captured.provider) throw Object.assign(new Error('File access changed; choose the image again.'), { code: 'stale_context' });
  };
  assertLive('before-open');
  const opened = await filesClient.openResource(classification.ref, { signal });
  assertLive('after-open');
  const resource = opened?.resource || {};
  const currentRef = typeof resource.ref === 'string' ? resource.ref.trim() : '';
  const currentMimeValue = resource.mime_type || resource.mime || opened?.payload?.mime_type || classification.mime;
  const currentMime = typeof currentMimeValue === 'string' ? currentMimeValue.toLowerCase() : '';
  const currentProvider = typeof resource.provider === 'string' ? resource.provider.trim() : '';
  const currentStableValue = resource.resource_key || resource.resourceKey || resource.id || resource.resource_id;
  const currentStableId = typeof currentStableValue === 'string' ? currentStableValue.trim() : '';
  const currentKey = currentStableId.startsWith(`${captured.account}|${captured.workspace}|${captured.provider}:`)
    ? currentStableId : `${captured.account}|${captured.workspace}|${captured.provider}:${currentStableId}`;
  const caps = Array.isArray(resource.capabilities) ? resource.capabilities.map(value => typeof value === 'string' ? value.toLowerCase() : '') : [];
  const uniqueCaps = new Set(caps);
  if (!currentRef || currentKey !== captured.resourceKey || currentProvider !== captured.provider || currentMime !== captured.mime || uniqueCaps.size !== caps.length || caps.some(value => !CAPABILITY_KEYS.has(value)) || caps.length !== captured.capabilities.length || caps.some(value => !captured.capabilities.includes(value))) {
    throw Object.assign(new Error('This Files image is no longer available to Canvas.'), { code: 'unauthorized' });
  }
  let currentRevision;
  try { currentRevision = _strictRevision(resource.revision || opened?.payload?.revision, 'Files current revision'); }
  catch (_) { throw Object.assign(new Error('This Files image changed; choose it again.'), { code: 'stale_revision' }); }
  if (currentRevision.kind !== captured.revision.kind || currentRevision.value !== captured.revision.value) throw Object.assign(new Error('This Files image changed; choose it again.'), { code: 'stale_revision' });
  const response = await (fetchImpl || fetch)(filesClient.contentUrl(currentRef, { purpose: 'download' }), { credentials: 'same-origin', signal });
  const blob = await _blobBounded(response, maxBytes, signal);
  assertLive('after-fetch');
  if (!_imageMime(blob.type || currentMime)) throw Object.assign(new Error('Canvas accepts image resources only.'), { code: 'unsupported' });
  const img = await _decodeBlob(blob, { maxDimension, maxPixels }, { signal });
  assertLive('after-decode');
  const provenance = Object.freeze({ domain: 'files', source: 'authorized-files-image', resource_ref: currentRef, resource_key: captured.resourceKey, provider: captured.provider, account: captured.account, workspace: captured.workspace, revision: currentRevision, operation_id: String(payload.operation_id || '') });
  assertLive('before-layer');
  if (typeof onImage === 'function') onImage(img, provenance);
  return { status: 'imported', resourceRef: currentRef, bytes: blob.size, provenance, image: img };
}

/** Explicit Canvas layer → Files export.  The destination operation is the
 * only path that persists pixels; local layer ids are never authorization. */
const _canvasExportReservations = new Map();

function _strictImportReceipt(receipt, operation, item, generation) {
  if (!receipt || typeof receipt !== 'object' || receipt.operation_id !== operation || receipt.generation !== generation || !['complete', 'partial', 'pending'].includes(receipt.state) || !Array.isArray(receipt.items) || receipt.items.length !== 1 || receipt.items[0]?.item_id !== item) {
    throw Object.assign(new Error('Files import receipt did not account for this Canvas export.'), { code: 'receipt_unconfirmed', receipt });
  }
  const result = receipt.items[0];
  if (!['committed', 'unchanged', 'conflict', 'denied', 'failed', 'pending', 'stale'].includes(result.outcome)) throw Object.assign(new Error('Files import receipt is invalid.'), { code: 'receipt_invalid', receipt });
  if ((receipt.state === 'pending' && result.outcome !== 'pending') || (receipt.state === 'complete' && result.outcome === 'pending')) throw Object.assign(new Error('Files import receipt state is invalid.'), { code: 'receipt_invalid', receipt });
  return receipt;
}

function _canvasExportStatus(receipt, operation, item, bytes, provenance) {
  const result = receipt?.items?.find(value => value?.item_id === item);
  const outcome = result?.outcome;
  const status = outcome === 'committed' || outcome === 'unchanged' ? 'committed'
    : outcome === 'pending' ? 'pending'
      : outcome === 'conflict' ? 'conflict'
        : outcome === 'denied' ? 'denied'
          : outcome === 'failed' || outcome === 'stale' ? 'failed' : 'unknown';
  return { status, outcome: outcome || 'unknown', operation_id: operation, item_id: item, bytes, provenance, receipt };
}

export async function exportCanvasLayerToFiles(layer, {
  filesClient,
  destinationRef,
  generation = 0,
  operationId = null,
  itemId = null,
  name = 'canvas-export.png',
  collision = 'fail',
  account = null,
  workspace = null,
  provider = null,
  destinationResourceKey = null,
  destinationRevision = null,
  signal = null,
  maxBytes = CANVAS_MAX_IMPORT_BYTES,
  getContext = null,
} = {}) {
  const destination = String(destinationRef || '').trim();
  if (!destination || !filesClient || typeof filesClient.importFile !== 'function') throw new TypeError('authorized Files destination is required');
  if (!layer?.canvas || typeof layer.canvas.toBlob !== 'function') throw new TypeError('Canvas layer is unavailable');
  const operation = String(operationId || `canvas-export-${Date.now()}-${Math.random().toString(16).slice(2)}`).slice(0, 128);
  const item = String(itemId || `layer-${String(layer.id || 'local')}`).slice(0, 128);
  const reservationKey = `${operation}:${item}`;
  if (_canvasExportReservations.has(reservationKey)) return _canvasExportReservations.get(reservationKey);
  const task = (async () => {
    const currentGeneration = _safeInteger(Number(generation), 'Files policy generation');
    const expectedDestinationKey = _safeText(String(destinationResourceKey || ''), 'Files destination key');
    const expectedDestinationRevision = _strictRevision(destinationRevision, 'Files destination revision');
    if (!['fail', 'rename'].includes(String(collision || 'fail').toLowerCase())) throw new TypeError('Files collision policy is invalid');
    const assertLive = () => {
      if (signal?.aborted) throw Object.assign(new Error('Canvas export was cancelled.'), { name: 'AbortError' });
      if (typeof getContext !== 'function') throw Object.assign(new Error('File access changed; the Canvas export was stopped.'), { code: 'stale_context' });
      const current = getContext();
      if (!current || String(current.account ?? current.account_id ?? '') !== String(account ?? '') || String(current.workspace ?? current.workspace_id ?? '') !== String(workspace ?? '') || String(current.provider ?? '') !== String(provider ?? '') || Number(current.generation ?? current.policyGeneration) !== currentGeneration || Number(current.policyGeneration ?? current.generation) !== currentGeneration) {
        throw Object.assign(new Error('File access changed; the Canvas export was stopped.'), { code: 'stale_context' });
      }
    };
    const validateDestination = async () => {
      if (typeof filesClient.stat !== 'function') return;
      const current = await filesClient.stat(destination, { signal });
      const currentResource = current?.resource || {};
      const currentCaps = _capSet(currentResource.capabilities || current?.capabilities);
      const stableId = String(currentResource.resource_key || currentResource.resourceKey || currentResource.id || '').trim();
      const currentKey = stableId === expectedDestinationKey || stableId.startsWith(`${account}|${workspace}|${provider}:`) ? stableId : `${account}|${workspace}|${provider}:${stableId}`;
      if (String(currentResource.ref || '').trim() !== destination || (provider && String(currentResource.provider || '') !== String(provider)) || currentKey !== expectedDestinationKey || !_strictRevision(currentResource.revision, 'Files destination revision') || currentResource.revision.kind !== expectedDestinationRevision.kind || currentResource.revision.value !== expectedDestinationRevision.value || !currentCaps.has('write') || !(currentCaps.has('children') || currentCaps.has('import'))) throw Object.assign(new Error('This Canvas export destination is no longer available.'), { code: 'destination_denied' });
    };
    await validateDestination();
    assertLive();
    const bytes = await new Promise((resolve, reject) => layer.canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error('Canvas could not encode the layer.')), 'image/png'));
    assertLive();
    await validateDestination();
    assertLive();
    if (bytes.size > maxBytes) throw Object.assign(new Error('Canvas export exceeds the Files import limit.'), { code: 'oversize' });
    const filename = String(name || 'canvas-export.png').replace(/[\\/\0]/g, '_').slice(0, 240) || 'canvas-export.png';
    const file = typeof File === 'function' ? new File([bytes], filename, { type: 'image/png' }) : Object.assign(bytes, { name: filename, lastModified: Date.now() });
    const provenance = Object.freeze({ domain: 'canvas', layer_id: String(layer.id || ''), account, workspace, provider, destination_ref: destination, operation_id: operation });
    let receipt;
    try {
      receipt = await filesClient.importFile(file, { operationId: operation, itemId: item, generation: currentGeneration, destinationRef: destination, name: filename, collision, signal });
    } catch (error) {
      if (error?.name === 'AbortError') throw error;
      if (typeof filesClient.operationReceipt !== 'function') return { status: 'unknown', outcome: 'unknown', operation_id: operation, item_id: item, bytes: bytes.size, provenance, receipt: null };
      try {
        receipt = _strictImportReceipt(await filesClient.operationReceipt(operation, { signal }), operation, item, currentGeneration);
      } catch (_) {
        return { status: 'unknown', outcome: 'unknown', operation_id: operation, item_id: item, bytes: bytes.size, provenance, receipt: null };
      }
    }
    _strictImportReceipt(receipt, operation, item, currentGeneration);
    assertLive();
    return _canvasExportStatus(receipt, operation, item, bytes.size, provenance);
  })();
  _canvasExportReservations.set(reservationKey, task);
  return task;
}

function wireClipboardAndDropLegacy({
  container, saveState, createLayer, renderLayerPanel, composite,
  handleImportedImage, uiModule,
}) {
  // ── Paste ──
  window.addEventListener('paste', (e) => {
    if (!state.editorOpen || !container.getClientRects().length) return;

    function pasteAsLayer(imgSource, label) {
      if (!state.editorOpen) return; // user closed mid-paste
      saveState();
      const layer = createLayer(label || 'Pasted', imgSource.width, imgSource.height);
      layer.ctx.drawImage(imgSource, 0, 0);
      state.layers.push(layer);
      state.activeLayerId = layer.id;
      state.tool = 'move';
      const tb = state.container?.querySelector('.ge-toolbar');
      if (tb) tb.querySelectorAll('.ge-tool-btn').forEach(b => b.classList.toggle('active', b.dataset.tool === 'move'));
      renderLayerPanel();
      composite();
      uiModule.showToast('Pasted as new layer');
    }

    // Check internal clipboard first (from Ctrl+C lasso/wand).
    if (state.internalClipboard) {
      e.preventDefault();
      e.stopImmediatePropagation();
      pasteAsLayer(state.internalClipboard, 'Pasted Selection');
      return;
    }

    // Fall back to system clipboard.
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (!item.type.startsWith('image/')) continue;
      e.preventDefault();
      e.stopImmediatePropagation();
      const blob = item.getAsFile();
      const url = URL.createObjectURL(blob);
      const img = new Image();
      img.onload = () => { pasteAsLayer(img, 'Pasted'); URL.revokeObjectURL(url); };
      img.src = url;
      break;
    }
  }, true);  // capture phase so we beat chat input

  // ── Drag-and-drop ──
  // Visual drop-zone overlay appears mid-drag; routes via
  // handleImportedImage so the import respects canvas resizing rules
  // + saves history (same path as the toolbar Import button).
  const dropZone = container;
  if (!dropZone) return;
  let dragDepth = 0;
  const hasFileType = (dt) => dt && Array.from(dt.types || []).some(t => t === 'Files');
  const showOverlay = () => {
    if (!state.editorOpen) return;
    let ov = dropZone.querySelector('.ge-drop-overlay');
    if (!ov) {
      ov = document.createElement('div');
      ov.className = 'ge-drop-overlay';
      ov.innerHTML = '<div class="ge-drop-overlay-msg">Drop image to add as new layer</div>';
      dropZone.appendChild(ov);
    }
    ov.style.display = '';
  };
  const hideOverlay = () => {
    const ov = dropZone.querySelector('.ge-drop-overlay');
    if (ov) ov.style.display = 'none';
  };
  dropZone.addEventListener('dragenter', (e) => {
    if (!state.editorOpen || !hasFileType(e.dataTransfer)) return;
    e.preventDefault();
    dragDepth++;
    showOverlay();
  });
  dropZone.addEventListener('dragover', (e) => {
    if (!state.editorOpen || !hasFileType(e.dataTransfer)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  dropZone.addEventListener('dragleave', () => {
    if (!state.editorOpen) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideOverlay();
  });
  dropZone.addEventListener('drop', (e) => {
    if (!state.editorOpen) return;
    dragDepth = 0;
    hideOverlay();
    const files = Array.from(e.dataTransfer?.files || []).filter(f => f.type.startsWith('image/'));
    if (!files.length) return;
    e.preventDefault();
    e.stopPropagation();
    for (const f of files) {
      const url = URL.createObjectURL(f);
      const img = new Image();
      img.onload = () => { handleImportedImage(img); URL.revokeObjectURL(url); };
      img.onerror = () => URL.revokeObjectURL(url);
      img.src = url;
    }
  });
}

/** Hardened mounted wiring. The legacy function above is retained as a
 * compatibility reference; production callers use this disposable adapter. */
export function wireClipboardAndDrop({
  container, saveState, createLayer, renderLayerPanel, composite,
  handleImportedImage, uiModule, filesClient = null, context = null,
  limits = {}, getContext = null,
}) {
  if (!container || typeof container.addEventListener !== 'function') return { dispose() {} };
  const maxBytes = Number(limits.maxBytes || CANVAS_MAX_IMPORT_BYTES);
  const maxDimension = Number(limits.maxDimension || CANVAS_MAX_IMPORT_DIMENSION);
  const maxPixels = Number(limits.maxPixels || CANVAS_MAX_IMPORT_PIXELS);
  const controller = typeof AbortController === 'function' ? new AbortController() : null;
  const seen = new Set();
  const objectUrls = new Set();
  let dragDepth = 0;
  let disposed = false;
  const signal = controller?.signal;
  const listen = (target, type, listener, options = {}) => {
    target.addEventListener(type, listener, { ...options, ...(signal ? { signal } : {}) });
  };
  const toast = message => { try { uiModule?.showToast?.(_boundedText(message)); } catch (_) {} };
  const annotate = (before, provenance) => {
    if (!provenance || state.layers.length <= before) return;
    const layer = state.layers[state.layers.length - 1];
    if (layer) layer.provenance = provenance;
  };
  const addImage = (img, label, provenance = null) => {
    if (disposed || !state.editorOpen) return false;
    const before = state.layers.length;
    if (typeof handleImportedImage === 'function') handleImportedImage(img, { provenance });
    else {
      saveState?.('Import image');
      const dims = _checkDecodedDimensions(img.naturalWidth || img.width, img.naturalHeight || img.height, { maxDimension, maxPixels });
      const layer = createLayer(label || 'Imported', dims.width, dims.height);
      layer.ctx?.drawImage(img, 0, 0);
      layer.provenance = provenance;
      state.layers.push(layer);
      state.activeLayerId = layer.id;
      renderLayerPanel?.(); composite?.();
    }
    annotate(before, provenance);
    return true;
  };
  const decodeAndAdd = async (blob, label, provenance) => {
    try {
      const img = await _decodeBlob(blob, { maxDimension, maxPixels }, { signal });
      addImage(img, label, provenance);
    } catch (error) {
      if (error?.name !== 'AbortError') toast(error?.message || 'Canvas could not import that image.');
    }
  };
  const showOverlay = message => {
    if (!state.editorOpen) return;
    let overlay = container.querySelector('.ge-drop-overlay');
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.className = 'ge-drop-overlay';
      overlay.innerHTML = '<div class="ge-drop-overlay-msg"></div>';
      container.appendChild(overlay);
    }
    const text = overlay.querySelector('.ge-drop-overlay-msg');
    if (text) text.textContent = message || 'Drop image to add as new layer';
    overlay.style.display = '';
  };
  const hideOverlay = () => { const overlay = container.querySelector('.ge-drop-overlay'); if (overlay) overlay.style.display = 'none'; };

  const onPaste = event => {
    if (disposed || !state.editorOpen || !container.getClientRects().length) return;
    if (state.internalClipboard) {
      event.preventDefault(); event.stopImmediatePropagation();
      const clip = state.internalClipboard;
      let dims;
      try { dims = _checkDecodedDimensions(clip.naturalWidth || clip.width, clip.naturalHeight || clip.height, { maxDimension, maxPixels }); }
      catch (error) { toast(error?.message || 'Canvas rejected the clipboard image.'); return; }
      saveState?.('Paste selection');
      const layer = createLayer('Pasted Selection', dims.width, dims.height);
      layer.ctx?.drawImage(clip, 0, 0); layer.provenance = Object.freeze({ domain: 'canvas', source: 'internal-clipboard' });
      state.layers.push(layer); state.activeLayerId = layer.id; renderLayerPanel?.(); composite?.(); toast('Pasted as new layer');
      return;
    }
    const items = Array.from(event.clipboardData?.items || []);
    const item = items.find(candidate => _imageMime(candidate.type));
    if (!item) return;
    event.preventDefault(); event.stopImmediatePropagation();
    const blob = item.getAsFile?.();
    if (!blob) { toast('Clipboard image is unavailable.'); return; }
    if (Number(blob.size) > maxBytes) { toast('Canvas rejected the image because it exceeds the import limit.'); return; }
    void decodeAndAdd(blob, 'Pasted', Object.freeze({ domain: 'canvas', source: 'os-clipboard', mime_type: String(blob.type || ''), bytes: Number(blob.size) || 0 }));
  };
  listen(window, 'paste', onPaste, { capture: true });

  const onDragEnter = event => {
    if (disposed || !state.editorOpen) return;
    const classification = classifyCanvasDrop(event, { maxBytes, maxDimension, maxPixels });
    if (!classification.ok || classification.kind === 'os-image' || classification.kind === 'files-image') {
      event.preventDefault(); event.stopPropagation(); dragDepth += 1;
      showOverlay(classification.ok ? 'Drop image to add as new layer' : classification.reason);
    }
  };
  const onDragOver = event => {
    if (disposed || !state.editorOpen) return;
    event.preventDefault(); event.stopPropagation();
    const classification = classifyCanvasDrop(event, { maxBytes, maxDimension, maxPixels });
    if (classification.ok) { event.dataTransfer.dropEffect = 'copy'; showOverlay('Drop image to add as new layer'); }
    else { event.dataTransfer.dropEffect = 'none'; showOverlay(classification.reason); }
  };
  const onDragLeave = event => {
    if (disposed || !state.editorOpen) return;
    dragDepth = Math.max(0, dragDepth - 1);
    if (dragDepth === 0) hideOverlay();
  };
  const onFilesCanvasRequest = event => {
    if (disposed || !state.editorOpen) return;
    const payload = event.detail?.payload;
    if (!payload || payload.type !== FILES_CANVAS_IMAGE_MIME) return;
    const key = canvasDropOperationKey(payload);
    if (!key || seen.has(key)) return;
    seen.add(key);
    void importFilesImageToCanvas(payload, {
      filesClient, context: event.detail?.context || context, getContext, signal,
      maxBytes, maxDimension, maxPixels,
      onImage: (img, provenance) => addImage(img, 'Imported', provenance),
    }).catch(error => {
      seen.delete(key);
      if (error?.name !== 'AbortError') toast(error?.message || 'This Files image is no longer available to Canvas.');
    });
  };
  listen(window, 'openclank-files-image-to-canvas', onFilesCanvasRequest);

  const onDrop = event => {
    if (disposed || !state.editorOpen) return;
    event.preventDefault(); event.stopPropagation(); dragDepth = 0; hideOverlay();
    const classification = classifyCanvasDrop(event, { maxBytes, maxDimension, maxPixels });
    if (!classification.ok) { toast(classification.reason); return; }
    if (classification.kind === 'files-image') {
      const payload = classification.payload;
      const key = canvasDropOperationKey({ operation_id: payload.operation_id, item_id: payload.item_id });
      if (seen.has(key)) return;
      seen.add(key);
      void importFilesImageToCanvas(payload, { filesClient, context, getContext, signal, maxBytes, maxDimension, maxPixels, onImage: (img, provenance) => addImage(img, 'Imported', provenance) }).catch(error => {
        seen.delete(key);
        if (error?.name !== 'AbortError') toast(error?.message || 'This Files image is no longer available to Canvas.');
      });
      return;
    }
    for (const file of classification.files) {
      const key = osDropOperationKey(file, event.dataTransfer, 0);
      if (seen.has(key)) continue;
      seen.add(key);
      void decodeAndAdd(file, 'Imported', Object.freeze({ domain: 'canvas', source: 'os-drop', name: _boundedText(file.name), mime_type: String(file.type || ''), bytes: Number(file.size) || 0 }));
    }
  };
  listen(container, 'dragenter', onDragEnter);
  listen(container, 'dragover', onDragOver);
  listen(container, 'dragleave', onDragLeave);
  listen(container, 'drop', onDrop);

  const dispose = () => {
    if (disposed) return;
    disposed = true; try { controller?.abort(); } catch (_) {}
    for (const url of objectUrls) { try { URL.revokeObjectURL(url); } catch (_) {} }
    objectUrls.clear(); seen.clear(); hideOverlay(); dragDepth = 0;
    try { observer?.disconnect?.(); } catch (_) {}
  };
  // Gallery closes by clearing and hiding this container. Observe that
  // existing lifecycle seam so the window-level paste listener is removed at
  // close without adding a document-wide lifecycle listener.
  const observer = typeof MutationObserver === 'function' ? new MutationObserver(() => {
    if (!state.editorOpen && (container.style.display === 'none' || !container.isConnected)) dispose();
  }) : null;
  observer?.observe(container, { attributes: true, attributeFilter: ['style'], childList: true });
  return Object.freeze({ dispose, classify: input => classifyCanvasDrop(input, { maxBytes, maxDimension, maxPixels }) });
}
