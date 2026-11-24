export const REPORT_CHANNEL = 'openclank.report.v1';
export const MAX_REPORT_BYTES = 512 * 1024;
export const MAX_REPORT_ITEMS = 1000;

const textEncoder = new TextEncoder();
const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const SHA256 = /^sha256:[a-f0-9]{64}$/;

function plainObject(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
    && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
}

function jsonBytes(value) {
  const encoded = JSON.stringify(value);
  if (typeof encoded !== 'string' || textEncoder.encode(encoded).byteLength > MAX_REPORT_BYTES) {
    throw new TypeError('Report data exceeds the 512 KiB limit.');
  }
  return encoded;
}

function copyJsonValue(value, depth = 0, budget = { nodes: 0 }) {
  budget.nodes += 1;
  if (budget.nodes > 10_000 || depth > 32) throw new TypeError('Report data is too deeply nested or contains too many values.');
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (Array.isArray(value)) return value.map(item => copyJsonValue(item, depth + 1, budget));
  if (!plainObject(value)) throw new TypeError('Report values must contain JSON data only.');
  const copied = {};
  for (const [key, item] of Object.entries(value)) {
    if (key === '__proto__' || key === 'prototype' || key === 'constructor' || key.length > 256) throw new TypeError('Report data contains an invalid key.');
    copied[key] = copyJsonValue(item, depth + 1, budget);
  }
  return copied;
}

function validateItems(items, manifest) {
  if (!Array.isArray(items) || items.length > MAX_REPORT_ITEMS) throw new TypeError('Report items are invalid or exceed the item limit.');
  const conflicts = [];
  const accepted = [];
  const seen = new Set();
  for (const item of items) {
    if (!plainObject(item) || Object.keys(item).some(key => !['id', 'digest', 'value'].includes(key))
      || typeof item.id !== 'string' || !SAFE_ID.test(item.id) || !SHA256.test(String(item.digest || ''))
      || !Object.hasOwn(item, 'value') || seen.has(item.id)) throw new TypeError('Report item envelope is malformed.');
    seen.add(item.id);
    const expected = manifest.get(item.id);
    if (!expected) conflicts.push({ id: item.id, reason: 'unknown_item' });
    else if (expected !== item.digest) conflicts.push({ id: item.id, reason: 'changed_item_digest' });
    else accepted.push({ id: item.id, digest: item.digest, value: copyJsonValue(item.value) });
  }
  return { accepted, conflicts };
}

export function validateReportRegistration(value, session) {
  if (!plainObject(value) || value.channel !== REPORT_CHANNEL || value.type !== 'register'
    || Object.keys(value).some(key => !['channel', 'type', 'session_id', 'generation', 'report_id', 'source_digest', 'schema_version', 'items'].includes(key))
    || value.session_id !== session.session_id || value.generation !== session.generation
    || typeof value.report_id !== 'string' || !SAFE_ID.test(value.report_id)
    || value.source_digest !== session.digest || !SHA256.test(String(value.source_digest || ''))
    || !Number.isSafeInteger(value.schema_version) || value.schema_version < 1 || value.schema_version > 1000
    || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > MAX_REPORT_ITEMS) {
    throw new TypeError('Report registration is invalid or does not match this preview.');
  }
  const manifest = new Map();
  for (const item of value.items) {
    if (!plainObject(item) || Object.keys(item).some(key => !['id', 'digest'].includes(key))
      || typeof item.id !== 'string' || !SAFE_ID.test(item.id) || !SHA256.test(String(item.digest || ''))
      || manifest.has(item.id)) throw new TypeError('Report item manifest is invalid.');
    manifest.set(item.id, item.digest);
  }
  const registration = Object.freeze({
    reportId: value.report_id,
    sourceDigest: value.source_digest,
    schemaVersion: value.schema_version,
    manifest,
  });
  return registration;
}

export function validateProgressEnvelope(value, registration, { allowSourceDigestChange = false } = {}) {
  if (!plainObject(value) || Object.keys(value).some(key => !['version', 'report_id', 'source_digest', 'schema_version', 'items'].includes(key))
    || value.version !== 1 || value.report_id !== registration.reportId
    || !SHA256.test(String(value.source_digest || ''))
    || !Number.isSafeInteger(value.schema_version) || value.schema_version < 1
    || Object.keys(value).length === 0) throw new TypeError('Progress backup envelope is invalid.');
  const conflicts = [];
  if (value.schema_version !== registration.schemaVersion) conflicts.push({ id: '*', reason: 'incompatible_schema' });
  if (value.source_digest !== registration.sourceDigest) {
    conflicts.push({ id: '*', reason: 'changed_source_digest' });
    if (!allowSourceDigestChange) return { accepted: [], conflicts, sourceDigestChanged: true };
  }
  const rows = validateItems(value.items, registration.manifest);
  conflicts.push(...rows.conflicts);
  const accepted = value.schema_version === registration.schemaVersion ? rows.accepted : [];
  const normalized = { version: 1, report_id: registration.reportId, source_digest: value.source_digest,
    schema_version: value.schema_version, items: accepted };
  jsonBytes(normalized);
  return { ...rows, accepted, conflicts, sourceDigestChanged: value.source_digest !== registration.sourceDigest, normalized };
}

export function makeProgressEnvelope(registration, items) {
  const result = validateProgressEnvelope({
    version: 1, report_id: registration.reportId, source_digest: registration.sourceDigest,
    schema_version: registration.schemaVersion, items,
  }, registration);
  if (result.conflicts.length) throw new TypeError('Progress contains items that do not match the registered report.');
  return result.normalized;
}

export function validateSavedProgressEnvelope(value) {
  if (!plainObject(value) || Object.keys(value).some(key => !['version', 'report_id', 'source_digest', 'schema_version', 'items'].includes(key))
    || value.version !== 1 || typeof value.report_id !== 'string' || !SAFE_ID.test(value.report_id)
    || !SHA256.test(String(value.source_digest || '')) || !Number.isSafeInteger(value.schema_version)
    || value.schema_version < 1 || !Array.isArray(value.items) || value.items.length > MAX_REPORT_ITEMS) {
    throw new TypeError('Saved progress envelope is invalid.');
  }
  const manifest = new Map(value.items.map(item => [String(item?.id || ''), String(item?.digest || '')]));
  if (manifest.size !== value.items.length || [...manifest].some(([id, digest]) => !SAFE_ID.test(id) || !SHA256.test(digest))) {
    throw new TypeError('Saved progress item manifest is invalid.');
  }
  const checked = validateProgressEnvelope(value, {
    reportId: value.report_id, sourceDigest: value.source_digest,
    schemaVersion: value.schema_version, manifest,
  });
  if (checked.conflicts.length) throw new TypeError('Saved progress is malformed.');
  return checked.normalized;
}

export function makeBackupFile(envelope, format = 'json', date = new Date()) {
  const normalized = validateSavedProgressEnvelope(envelope);
  const serialized = jsonBytes(normalized);
  if (format === 'json') return new File([serialized], `${normalized.report_id}-progress.json`, { type: 'application/json' });
  if (format !== 'markdown') throw new TypeError('Backup format is unsupported.');
  const safeJson = serialized.replace(/```/g, '\\u0060\\u0060\\u0060');
  const markdown = [
    `# ${normalized.report_id} progress backup`, '',
    `- Exported: ${date.toISOString()}`,
    `- Source digest: ${normalized.source_digest}`,
    `- Schema version: ${normalized.schema_version}`, '',
    '```json', safeJson, '```', '',
  ].join('\n');
  if (textEncoder.encode(markdown).byteLength > MAX_REPORT_BYTES + 4096) throw new TypeError('Markdown backup exceeds the size limit.');
  return new File([markdown], `${normalized.report_id}-progress.md`, { type: 'text/markdown' });
}

export async function parseBackupFile(file) {
  const markdownFile = /\.md$/i.test(String(file?.name || '')) || String(file?.type || '').toLowerCase().includes('markdown');
  const maxFileBytes = MAX_REPORT_BYTES + (markdownFile ? 4096 : 0);
  if (!file || !Number.isSafeInteger(Number(file.size)) || file.size < 1 || file.size > maxFileBytes) {
    throw new TypeError('Backup file is empty or exceeds the size limit.');
  }
  const source = await file.text();
  if (textEncoder.encode(source).byteLength > maxFileBytes) throw new TypeError('Backup file exceeds the size limit.');
  let json = source;
  if (markdownFile) {
    const match = source.match(/^# [^\n]{1,256} progress backup\n(?:[^\n]*\n)*?```json\n([\s\S]*?)\n```\s*$/);
    if (!match) throw new TypeError('Markdown backup does not contain a supported JSON data block.');
    json = match[1].replace(/\\u0060/g, '\\u0060');
  }
  let value;
  try { value = JSON.parse(json); } catch { throw new TypeError('Backup JSON is malformed.'); }
  if (textEncoder.encode(JSON.stringify(value)).byteLength > MAX_REPORT_BYTES) throw new TypeError('Backup data exceeds the size limit.');
  return value;
}

export function createProgressStore(storage = globalThis.localStorage) {
  return Object.freeze({
    load(key) {
      try {
        const value = storage.getItem(String(key));
        if (value == null) return null;
        const parsed = JSON.parse(value);
        return parsed?.storage_version === 1 && plainObject(parsed.progress) ? parsed.progress : parsed;
      } catch { return null; }
    },
    save(key, envelope) {
      const serialized = jsonBytes(envelope);
      try {
        storage.setItem(String(key), JSON.stringify({ storage_version:1, saved_at:Date.now(), progress:JSON.parse(serialized) }));
        return true;
      }
      catch { return false; }
    },
    list(prefix) {
      const startsWith = String(prefix);
      const rows = [];
      try {
        const limit = Math.min(Number(storage.length) || 0, 10_000);
        for (let index = 0; index < limit; index += 1) {
          const key = storage.key(index);
          if (!key || !String(key).startsWith(startsWith)) continue;
          const raw = storage.getItem(key);
          if (raw == null) continue;
          const parsed = JSON.parse(raw);
          const wrapped = parsed?.storage_version === 1 && plainObject(parsed.progress);
          rows.push({ key:String(key), envelope:wrapped ? parsed.progress : parsed, savedAt:wrapped && Number.isFinite(parsed.saved_at) ? parsed.saved_at : 0 });
        }
      } catch { return []; }
      return rows.sort((left, right) => right.savedAt - left.savedAt);
    },
    prune(prefix, keep = 10) {
      const rows = this.list(prefix);
      for (const row of rows.slice(Math.max(1, Number(keep) || 10))) this.remove(row.key);
    },
    remove(key) { try { storage.removeItem(String(key)); return true; } catch { return false; } },
  });
}
