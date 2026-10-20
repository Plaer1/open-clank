// Memory export query assembly + asset URLs (memory-export-controls S01).
// Pure functions — the presentation-side mirror of `_parse_export_filters`
// in routes/memory/memory_routes.py; keep the two in lockstep. Every
// validation rejection raises MemoryExportFilterError with the same typed
// shape the backend returns as a 422. Node-testable (no DOM).

// Top-level keys the Rust `memory_export` payload can emit — must match
// `_EXPORT_SECTIONS` in routes/memory/memory_routes.py.
export const MEMORY_EXPORT_SECTIONS = [
  'owner', 'workspace_id', 'retention', 'raw', 'candidates', 'curated',
  'quarantine', 'graph_nodes', 'graph_edges', 'graph_cues', 'tombstones',
  'digest',
];

// Record/graph sections the export dialog offers as checkboxes. Scalar
// metadata sections (owner, retention, digest…) ride along unless the owner
// narrows the export to an explicit subset.
export const MEMORY_EXPORT_SECTION_CHOICES = [
  'raw', 'candidates', 'curated', 'quarantine',
  'graph_nodes', 'graph_edges', 'graph_cues', 'tombstones',
];

// Kind filter choices — the signal-filter kind list minus 'all'
// (static/js/memory.js _SIGNAL_FILTER_DEFS).
export const MEMORY_EXPORT_KINDS = [
  'instruction', 'persona', 'fact', 'episodic', 'fabric', 'wiki', 'raw', 'unknown',
];

export class MemoryExportFilterError extends Error {
  constructor(message) {
    super(message);
    this.name = 'MemoryExportFilterError';
    // Same envelope the backend 422 carries: {"code","message","retryable"}.
    this.problem = { code: 'MEMORY_EXPORT_FILTER_INVALID', message, retryable: false };
  }
}

function _csvTerms(values, name) {
  const terms = (Array.isArray(values) ? values : [])
    .map((value) => String(value || '').trim())
    .filter(Boolean);
  if (!terms.length) {
    throw new MemoryExportFilterError(`${name} must name at least one comma-separated value.`);
  }
  return terms;
}

// datetime.fromisoformat accepts date-only and full timestamp forms; a
// trailing 'Z' and naive values are treated as UTC server-side. Mirror that
// acceptance here and reject everything else before the request leaves.
const _ISO_8601 = /^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$/;

function _moment(value, name) {
  const text = String(value || '').trim();
  const parseable = _ISO_8601.test(text)
    && !Number.isNaN(Date.parse(text.includes('T') ? text : text.replace(' ', 'T')));
  if (!parseable) {
    throw new MemoryExportFilterError(
      `${name} must be an ISO-8601 timestamp, e.g. 2026-08-21T00:00:00Z.`,
    );
  }
  return text;
}

// Assemble the query string for GET /api/memory/export. Defaults reproduce
// today's one-click path exactly: format 'bundle' with every other option at
// its pass-through default yields '?format=bundle' and nothing else.
//
// options:
//   format          'bundle' (default) | 'json' (absent format param)
//   sections        array of section keys, or null/undefined for all
//   kinds           array of kind values, or null/undefined for all
//   since / until   ISO-8601 strings, or null/undefined
//   q               content substring; blank is dropped
//   includeArchived false drops archived curated; default true (param omitted)
//   assets          'all' (default, omitted) | 'images' | 'none'
//   assetsOnly      true keeps only asset bytes in the bundle (requires bundle)
export function buildMemoryExportQuery(options = {}) {
  const opts = options && typeof options === 'object' ? options : {};
  const bundle = opts.format == null || opts.format === 'bundle' || opts.format === 'v3';
  if (!bundle && opts.format !== 'json') {
    throw new MemoryExportFilterError('format must be bundle or json.');
  }
  const params = new URLSearchParams();
  if (bundle) params.set('format', 'bundle');

  if (opts.sections != null) {
    const requested = _csvTerms(opts.sections, 'sections');
    if (requested.some((term) => !MEMORY_EXPORT_SECTIONS.includes(term))) {
      throw new MemoryExportFilterError(
        'sections must be chosen from: ' + [...MEMORY_EXPORT_SECTIONS].sort().join(', '),
      );
    }
    params.set('sections', requested.join(','));
  }
  if (opts.kinds != null) {
    params.set('kinds', _csvTerms(opts.kinds, 'kinds').join(','));
  }
  if (opts.since != null && String(opts.since).trim()) {
    params.set('since', _moment(opts.since, 'since'));
  }
  if (opts.until != null && String(opts.until).trim()) {
    params.set('until', _moment(opts.until, 'until'));
  }
  const q = String(opts.q || '').trim();
  if (q) params.set('q', q);
  if (opts.includeArchived === false) params.set('include_archived', 'false');

  const assets = opts.assets == null ? 'all' : String(opts.assets).trim().toLowerCase();
  if (!['all', 'images', 'none'].includes(assets)) {
    throw new MemoryExportFilterError('assets must be all, images, or none.');
  }
  if (assets !== 'all') params.set('assets', assets);

  const assetsOnly = opts.assetsOnly === true;
  if (assetsOnly && !bundle) {
    throw new MemoryExportFilterError('assets_only requires format=bundle.');
  }
  if (assetsOnly) params.set('assets_only', 'true');

  const query = params.toString();
  return query ? `?${query}` : '';
}

export function memoryExportFilename(options = {}) {
  const bundle = options?.format == null || options.format === 'bundle' || options.format === 'v3';
  return bundle ? 'open-clank-memory-bundle-v3.zip' : 'open-clank-memory-export.json';
}

// Single-asset download endpoint shipped in S00
// (GET /api/memory/assets/{asset_id}, owner-scoped, attachment).
export function memoryAssetUrl(assetId) {
  const id = String(assetId || '').trim();
  if (!id) throw new Error('A photo asset id is required.');
  return `/api/memory/assets/${encodeURIComponent(id)}`;
}
