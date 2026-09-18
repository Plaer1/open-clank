// Pure Files/Code entry presentation helpers. This module never performs I/O,
// reads storage, or makes an authorization decision.
import { fileIconKey } from '../langIcons.js';

export function isDirectory(entry) {
  const kind = String(entry?.kind || entry?.type || '').toLowerCase();
  return kind === 'directory' || kind === 'folder' || kind.includes('directory');
}

export function entryName(entry) {
  return String(entry?.name || entry?.filename || entry?.path || '');
}

export function entryIdentity(entry) {
  return String(entry?.resource_ref?.id || entry?.resource_id || entry?.id || entry?.entry_id || `${entry?.kind || ''}:${entryName(entry)}`);
}

export function entryIconKey(entry = {}) {
  return fileIconKey({ ...entry, mimeType: entry.mimeType || entry.mime_type || entry.media_type });
}

const COLLATION_VERSION = 'open-clank-v1';
export const SORT_KEYS = Object.freeze(['name', 'kind', 'modified', 'size']);

export function normalizeSortKeys(keys, { fallback = SORT_KEYS } = {}) {
  if (!Array.isArray(keys)) return [...fallback];
  const normalized = [...new Set(keys.map(key => String(key || '').trim().toLowerCase()))]
    .filter(key => SORT_KEYS.includes(key));
  return normalized.includes('name') ? normalized : ['name'];
}

const LANGUAGE_BY_EXTENSION = Object.freeze({
  js:'JavaScript', jsx:'JavaScript JSX', mjs:'JavaScript', cjs:'JavaScript',
  ts:'TypeScript', tsx:'TypeScript JSX', py:'Python', rs:'Rust', go:'Go',
  java:'Java', kt:'Kotlin', kts:'Kotlin', c:'C', h:'C/C++ header', cc:'C++', cpp:'C++',
  hpp:'C++ header', cs:'C#', rb:'Ruby', php:'PHP', swift:'Swift', sh:'Shell',
  bash:'Shell', zsh:'Shell', json:'JSON', jsonc:'JSON', yaml:'YAML', yml:'YAML', toml:'TOML',
  xml:'XML', html:'HTML', htm:'HTML', css:'CSS', scss:'SCSS', md:'Markdown', sql:'SQL',
  mmd:'Mermaid', mermaid:'Mermaid',
});

const TEXT_EXTENSIONS = new Set([
  ...Object.keys(LANGUAGE_BY_EXTENSION),
  'cfg', 'conf', 'config', 'csv', 'env', 'ini', 'log', 'properties', 'text', 'txt', 'tsv', 'mmd', 'mermaid',
]);

const TEXT_FILE_NAMES = Object.freeze({
  '.bash_profile':'Shell', '.bashrc':'Shell', '.editorconfig':'Plain text', '.env':'Plain text',
  '.dockerignore':'Plain text', '.gitattributes':'Plain text', '.gitignore':'Plain text',
  '.npmrc':'Plain text', '.nvmrc':'Plain text', '.prettierignore':'Plain text',
  '.profile':'Shell', '.zprofile':'Shell', '.zshrc':'Shell',
  dockerfile:'Dockerfile', gemfile:'Ruby', justfile:'Plain text', license:'Plain text',
  makefile:'Plain text', procfile:'Plain text', rakefile:'Ruby', readme:'Plain text',
});

export function normalizeSortSpec(spec = {}) {
  const key = SORT_KEYS.includes(String(spec.key || '').toLowerCase()) ? String(spec.key).toLowerCase() : 'name';
  const direction = String(spec.direction || '').toLowerCase() === 'desc' ? 'desc' : 'asc';
  return {
    key,
    direction,
    directoriesFirst: (spec.directoriesFirst ?? spec.directories_first) !== false,
    collation: String(spec.collation || COLLATION_VERSION),
  };
}

export function constrainSortSpec(spec = {}, supportedKeys = SORT_KEYS) {
  const normalized = normalizeSortSpec(spec);
  const keys = normalizeSortKeys(supportedKeys);
  return keys.includes(normalized.key)
    ? normalized
    : { ...normalized, key: keys[0] || 'name' };
}

function collate(value) {
  return String(value || '').normalize('NFKD').toLocaleLowerCase('en-US');
}

function knownNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

export function compareEntries(a, b, spec = {}) {
  const sort = normalizeSortSpec(spec);
  const ad = isDirectory(a);
  const bd = isDirectory(b);
  if (sort.directoriesFirst && ad !== bd) return ad ? -1 : 1;
  let result = 0;
  if (sort.key === 'kind') {
    const kindValue = entry => isDirectory(entry)
      ? (entry?.kind || entry?.type || 'directory')
      : (entry?.sort_kind || entry?.media_type || entry?.mime_type || entry?.mimeType || entry?.kind || entry?.type || 'file');
    result = collate(kindValue(a)).localeCompare(collate(kindValue(b)), 'en-US');
  }
  else if (sort.key === 'modified') {
    const av = knownNumber(a?.modified_unix_ms ?? a?.modified ?? a?.modified_ms ?? a?.mtime);
    const bv = knownNumber(b?.modified_unix_ms ?? b?.modified ?? b?.modified_ms ?? b?.mtime);
    if (av === null && bv !== null) result = 1;
    else if (av !== null && bv === null) result = -1;
    else if (av !== null && bv !== null) result = av - bv;
  } else if (sort.key === 'size') {
    // Directory sizes are deliberately not recursive. Their deterministic tie is
    // their name/identity, while files with missing size sort last.
    const av = ad ? null : knownNumber(a?.size);
    const bv = bd ? null : knownNumber(b?.size);
    if (av === null && bv !== null) result = 1;
    else if (av !== null && bv === null) result = -1;
    else if (av !== null && bv !== null) result = av - bv;
  }
  if (!result) result = collate(entryName(a)).localeCompare(collate(entryName(b)), 'en-US');
  if (!result) result = entryIdentity(a).localeCompare(entryIdentity(b), 'en-US');
  if (sort.direction === 'desc' && result) result *= -1;
  return result;
}

export function sortEntries(entries, spec = {}) {
  return [...(entries || [])].sort((a, b) => compareEntries(a, b, spec));
}

function pathParts(path) {
  const leaf = String(path || '').replace(/\\/g, '/').split('/').pop()?.toLowerCase() || '';
  const lastDot = leaf.lastIndexOf('.');
  const ext = lastDot > 0 ? leaf.slice(lastDot + 1) : '';
  return { leaf, ext };
}

export function languageForPath(path) {
  const { leaf, ext } = pathParts(path);
  if (leaf.startsWith('dockerfile.')) return 'Dockerfile';
  return TEXT_FILE_NAMES[leaf] || LANGUAGE_BY_EXTENSION[ext] || 'Plain text';
}

/** Extension/name hint only. The server remains authoritative when content is read. */
export function isTextPath(path) {
  const { leaf, ext } = pathParts(path);
  return !!TEXT_FILE_NAMES[leaf]
    || leaf.startsWith('dockerfile.')
    || TEXT_EXTENSIONS.has(ext);
}

function unixMilliseconds(value) {
  if (value == null || value === '') return null;
  const numeric = typeof value === 'number' ? value : Number(String(value).trim());
  if (Number.isFinite(numeric)) {
    if (numeric <= 0) return null;
    // Copal loose-file snapshots use Unix seconds; browser/provider APIs
    // generally return milliseconds or ISO-8601 strings.
    return Math.round(numeric < 100_000_000_000 ? numeric * 1000 : numeric);
  }
  let timestamp = String(value).trim();
  // Existing SQLAlchemy library routes serialize naive UTC datetimes. Treat
  // those as UTC rather than shifting them through the viewer's local zone.
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(timestamp)) timestamp += 'Z';
  const parsed = Date.parse(timestamp);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function providerTimestamp(row = {}) {
  for (const value of [
    row.modified_unix_ms, row.modified_ms, row.updated_at, row.updatedAt,
    row.modified_at, row.modified, row.mtime, row.ts, row.created_at, row.createdAt,
  ]) {
    const parsed = unixMilliseconds(value);
    if (parsed !== null) return parsed;
  }
  return null;
}

function mediaTypeFromName(value) {
  const { ext } = pathParts(String(value || '').split(/[?#]/, 1)[0]);
  const known = {
    png:'image/png', jpg:'image/jpeg', jpeg:'image/jpeg', gif:'image/gif', webp:'image/webp', avif:'image/avif',
    mp3:'audio/mpeg', wav:'audio/wav', flac:'audio/flac', ogg:'audio/ogg', opus:'audio/ogg', m4a:'audio/mp4',
    md:'text/markdown', txt:'text/plain', json:'application/json', pdf:'application/pdf',
    zip:'application/zip', csv:'text/csv', html:'text/html', xml:'text/xml',
  };
  return known[ext] || '';
}

function providerMediaType(row, source) {
  const supplied = String(row?.media_type || row?.mime_type || row?.mimeType || row?.mime || '').trim();
  if (supplied) return supplied;
  const inferred = mediaTypeFromName(row?.filename || row?.name || row?.title || row?.url);
  if (inferred) return inferred;
  const language = String(row?.language || '').trim().toLowerCase();
  if (language === 'pdf') return 'application/pdf';
  if (language === 'markdown') return 'text/markdown';
  if (language === 'json') return 'application/json';
  if (language === 'html') return 'text/html';
  if (source === 'gallery') return 'image/*';
  if (source === 'copal') return ['note', 'wiki', 'markdown'].includes(String(row?.kind || '').toLowerCase())
    ? 'text/markdown'
    : 'application/json';
  if (source === 'library' && language) return language === 'text' ? 'text/plain' : `text/x-${language}`;
  return 'application/octet-stream';
}

function mediaSortKind(mediaType) {
  const media = String(mediaType || '').toLowerCase();
  if (media.startsWith('image/')) return 'image';
  if (media.startsWith('audio/')) return 'audio';
  if (media === 'application/pdf') return 'pdf';
  if (media.includes('zip') || media.includes('archive')) return 'archive';
  if (media.includes('json')) return 'json';
  if (media.startsWith('text/')) return 'text';
  return media || 'file';
}

/** Normalize finite managed-provider metadata without granting preview access. */
export function providerEntryMetadata(row = {}, source = '') {
  const provider = String(source || '').toLowerCase();
  const mediaType = providerMediaType(row, provider);
  const metadata = {
    modified_unix_ms: providerTimestamp(row),
    media_type: mediaType,
    sort_kind: String(row.sort_kind || row.language || (provider === 'copal' ? row.kind : '') || mediaSortKind(mediaType)),
  };
  if (provider === 'copal') metadata.preview_kind = 'text';
  return metadata;
}
