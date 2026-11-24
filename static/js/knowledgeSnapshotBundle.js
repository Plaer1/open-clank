// Client-side bundle of independent knowledge snapshot packages. Pure module:
// all I/O (HTTP, ids, approval, liveness) is injected. Packages stay opaque bytes.
export const BUNDLE_SCHEMA = 'openclank.knowledge-snapshot-bundle.v1';
export const MAX_ITEMS = 20;
export const MAX_BUNDLE_BYTES = 1572864;
export const MAX_PACKAGE_BYTES = 65536;

const HEX64 = /^[0-9a-f]{64}$/;
const B64 = /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

export async function sha256Hex(bytes) {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest), (v) => v.toString(16).padStart(2, '0')).join('');
}

export function bytesToBase64(bytes) {
  let bin = '';
  for (let i = 0; i < bytes.length; i += 8192) bin += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(bin);
}

function base64ToBytes(text) {
  if (typeof text !== 'string' || !B64.test(text)) throw new Error('invalid base64');
  const bin = atob(text);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// Canonical serialisation: fixed key order, no whitespace. parseBundle accepts
// only these exact bytes, which also refuses duplicate JSON keys (JSON.parse
// keeps the last duplicate, so the re-serialisation would differ).
function serialise(entries) {
  return new TextEncoder().encode(JSON.stringify({
    schema: BUNDLE_SCHEMA,
    items: entries.map((e) => ({ package_b64: e.package_b64, package_sha256: e.package_sha256 })),
  }));
}

export async function buildBundle(packages) {
  if (!Array.isArray(packages) || packages.length < 1 || packages.length > MAX_ITEMS) throw new Error('bundle needs 1 to 20 items');
  const entries = [];
  const seen = new Set();
  for (const bytes of packages) {
    if (!(bytes instanceof Uint8Array) || bytes.length < 1 || bytes.length > MAX_PACKAGE_BYTES) throw new Error('package must be 1 to 65536 bytes');
    const hash = await sha256Hex(bytes);
    if (seen.has(hash)) throw new Error('duplicate package');
    seen.add(hash);
    entries.push({ package_b64: bytesToBase64(bytes), package_sha256: hash });
  }
  const out = serialise(entries);
  if (out.length > MAX_BUNDLE_BYTES) throw new Error('bundle too large');
  return out;
}

export async function parseBundle(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.length < 1 || bytes.length > MAX_BUNDLE_BYTES) throw new Error('bundle size invalid');
  const text = new TextDecoder('utf-8', { fatal: true, ignoreBOM: true }).decode(bytes);
  const value = JSON.parse(text);
  if (value === null || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid bundle');
  const keys = Object.keys(value);
  if (keys.length !== 2 || !keys.includes('schema') || !keys.includes('items') || value.schema !== BUNDLE_SCHEMA) throw new Error('invalid bundle');
  if (!Array.isArray(value.items) || value.items.length < 1 || value.items.length > MAX_ITEMS) throw new Error('bundle needs 1 to 20 items');
  const items = [];
  const seen = new Set();
  for (const entry of value.items) {
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry)) throw new Error('invalid bundle item');
    const k = Object.keys(entry);
    if (k.length !== 2 || !k.includes('package_b64') || !k.includes('package_sha256')) throw new Error('invalid bundle item');
    if (typeof entry.package_sha256 !== 'string' || !HEX64.test(entry.package_sha256)) throw new Error('invalid item hash');
    const pkg = base64ToBytes(entry.package_b64);
    if (pkg.length < 1 || pkg.length > MAX_PACKAGE_BYTES) throw new Error('package must be 1 to 65536 bytes');
    if (await sha256Hex(pkg) !== entry.package_sha256) throw new Error('item hash mismatch');
    if (seen.has(entry.package_sha256)) throw new Error('duplicate package');
    seen.add(entry.package_sha256);
    items.push({ bytes: pkg, sha256: entry.package_sha256 });
  }
  const canonical = serialise(value.items);
  if (canonical.length !== bytes.length || canonical.some((b, i) => b !== bytes[i])) throw new Error('bundle is not canonical');
  return { items };
}

// post(phase, body) resolves to a Response-like { ok, json() }; it may reject.
async function readJson(response) {
  if (!response || response.ok !== true) return null;
  try { return await response.json(); } catch { return null; }
}

function mapState(value) {
  if (value?.state === 'staged') return 'staged';
  if (value?.state === 'unknown') return 'unknown';
  return 'not_imported';
}

function validPreview(v) {
  return v !== null && typeof v === 'object' && typeof v.text === 'string' && typeof v.category === 'string'
    && typeof v.review_sha256 === 'string' && HEX64.test(v.review_sha256);
}

// Sequential, in order. Confirm is never retried; a failed confirm is 'unknown'.
// approve(item, preview) -> boolean|Promise<boolean>; item = {index, bytes, sha256}.
export async function importBundle({ items, post, newActionId, current, approve, onItem }) {
  const results = [];
  let stopped = false;
  const push = (r) => { results.push(r); if (onItem) onItem(r); };
  for (let index = 0; index < items.length; index++) {
    const item = items[index];
    const base = { index, sha256: item.sha256 };
    if (stopped || current() !== true) { stopped = true; push({ ...base, state: 'skipped' }); continue; }
    const actionId = newActionId();
    const body = { action_id: actionId, package_b64: bytesToBase64(item.bytes) };
    let preview = null;
    try { preview = await readJson(await post('preview', body)); } catch { preview = null; }
    if (!validPreview(preview)) { push({ ...base, state: 'invalid' }); continue; }
    const approved = current() === true && approve ? (await approve({ index, ...item }, preview)) === true : false;
    if (current() !== true) stopped = true;
    if (!approved || stopped) { push({ ...base, state: 'skipped' }); continue; }
    const extra = { actionId, reviewSha256: preview.review_sha256 };
    let state = 'unknown';
    try { state = mapState(await readJson(await post('confirm', { ...body, review_sha256: preview.review_sha256 })) ?? { state: 'unknown' }); } catch { state = 'unknown'; }
    push({ ...base, state, ...extra });
  }
  return results;
}

export async function checkBundleItemStatus({ item, post, actionId, reviewSha256 }) {
  const body = { action_id: actionId, package_b64: bytesToBase64(item.bytes), review_sha256: reviewSha256 };
  let state = 'unknown';
  try {
    const value = await readJson(await post('status', body));
    state = value === null ? 'unknown' : mapState(value);
  } catch { state = 'unknown'; }
  return { sha256: item.sha256, state };
}
