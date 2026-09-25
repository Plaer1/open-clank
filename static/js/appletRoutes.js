// static/js/appletRoutes.js — one browser-target registry for the shell.
//
// Sidebar links, rich app links, initial loading, pushState/replaceState and
// Back/Forward all resolve through this module so a target has exactly one
// canonical address. Legacy `/copal/*` and older Notes/Code/Bases, Mind/Galaxy
// addresses stay as compatibility aliases; they never become destinations.

/** Canonical shell paths that always serve the SPA (server + SW agree). */
export const SHELL_PATHS = Object.freeze([
  '/',
  '/editor',
  '/files',
  '/wiki',
  '/graph',
  '/treehouse',
  '/timeline',
  '/todo',
  '/calendar',
  '/notes',
  '/code',
  '/bases',
  '/mind',
  '/galaxy',
  '/email',
  '/memory',
  '/gallery',
  '/tasks',
  '/library',
  '/cookbook',
  '/settings',
]);

/** Path prefixes that also serve the SPA (`/settings/<panel>`, `/copal/<view>`). */
export const SHELL_PATH_PREFIXES = Object.freeze(['/settings/', '/copal/']);

// Direct applet path for each canonical target. Editor-family views share the
// Editor workspace; they only differ by view/mode intent.
const DIRECT = Object.freeze({
  editor: '/editor',
  wiki: '/wiki',
  graph: '/graph',
  treehouse: '/treehouse',
  timeline: '/timeline',
  todo: '/todo',
  files: '/files',
  calendar: '/calendar',
  email: '/email',
  memory: '/memory',
  gallery: '/gallery',
  tasks: '/tasks',
  library: '/library',
  cookbook: '/cookbook',
  settings: '/settings',
});

// Map a bare path segment (no leading slash) to a target + view + optional
// graph mode. Unknown segments resolve to null so callers can 404 meaningfully.
const SEGMENTS = {
  editor:    { target: 'editor', view: 'notes' },
  notes:     { target: 'editor', view: 'notes' },
  code:      { target: 'editor', view: 'notes' },
  bases:     { target: 'editor', view: 'notes', openBases: true },
  wiki:      { target: 'editor', view: 'wiki' },
  timeline:  { target: 'editor', view: 'timeline' },
  todo:      { target: 'editor', view: 'todo' },
  graph:     { target: 'editor', view: 'graph' },
  mind:      { target: 'editor', view: 'graph', mode: 'mind' },
  galaxy:    { target: 'editor', view: 'graph', mode: 'galaxy' },
  treehouse: { target: 'editor', view: 'treehouse' },
  files:     { target: 'files' },
  calendar:  { target: 'calendar' },
  email:     { target: 'email' },
  memory:    { target: 'memory' },
  gallery:   { target: 'gallery' },
  tasks:     { target: 'tasks' },
  library:   { target: 'library' },
  cookbook:  { target: 'cookbook' },
  settings:  { target: 'settings' },
};

// Editor-family view -> canonical address.
const VIEW_PATH = Object.freeze({
  notes: '/editor',
  wiki: '/wiki',
  timeline: '/timeline',
  todo: '/todo',
  graph: '/graph',
  treehouse: '/treehouse',
});

function normalizeSearch(search) {
  const params = search instanceof URLSearchParams
    ? new URLSearchParams(search)
    : new URLSearchParams(search || '');
  // TreeHouse share acceptance consumes only its token parameter; other
  // query/hash state stays in the address.
  params.delete('treehouseShare');
  return params;
}

function withQuery(path, params) {
  const q = params.toString();
  return `${path}${q ? `?${q}` : ''}`;
}

/**
 * Canonical address for an applet. Always a direct path — never /copal/*.
 * @param {string} name editor|wiki|graph|treehouse|timeline|todo|files|calendar|
 *                      email|memory|gallery|tasks|library|cookbook|settings|
 *                      notes|code|bases|mind|galaxy
 * @param {{doc?: string, mode?: string, panel?: string, search?: string|URLSearchParams}} [opts]
 */
export function appletPath(name, opts = {}) {
  const key = String(name || '').toLowerCase();
  const seg = SEGMENTS[key];
  if (!seg) return '/';
  let path;
  if (seg.target === 'editor') {
    path = VIEW_PATH[seg.view] || '/editor';
  } else if (seg.target === 'settings' && opts.panel) {
    path = `/settings/${encodeURIComponent(String(opts.panel))}`;
  } else {
    path = DIRECT[seg.target] || '/';
  }
  const params = normalizeSearch(opts.search);
  if (opts.doc) params.set('doc', String(opts.doc));
  const mode = opts.mode || seg.mode;
  if (mode && seg.view === 'graph') params.set('mode', String(mode));
  return withQuery(path, params);
}

/**
 * Resolve a browser location to one shell target.
 * Accepts direct applet paths and legacy aliases (`/copal/*`, /notes, /code,
 * /bases, /mind, /galaxy). Returns null for non-applet paths so unknown URLs
 * keep meaningful errors instead of a blanket shell catch-all.
 */
export function resolveAppletLocation(pathname, search) {
  const raw = String(pathname || '/');
  const path = raw.replace(/\/+$/, '') || '/';
  let segment = path.replace(/^\//, '');
  let legacy = false;

  if (path === '/copal' || path.startsWith('/copal/')) {
    legacy = true;
    segment = path.slice('/copal'.length).replace(/^\//, '') || 'notes';
  }

  if (path === '/') return null;

  // /settings/<panel> is one target with a panel hint; the segment itself is
  // not a leaf name in SEGMENTS.
  let settingsPanel = null;
  if (path === '/settings' || path.startsWith('/settings/')) {
    settingsPanel = path.startsWith('/settings/')
      ? decodeURIComponent(path.slice('/settings/'.length))
      : '';
    segment = 'settings';
  }

  const seg = SEGMENTS[segment];
  if (!seg) return null;

  const params = normalizeSearch(search);
  const mode = params.get('mode') || seg.mode || null;
  const doc = params.get('doc');

  let canonicalPath;
  if (seg.target === 'editor') {
    const view = seg.view || 'notes';
    const q = new URLSearchParams(params);
    if (seg.openBases) q.set('open', 'bases');
    if (doc) q.set('doc', doc);
    if (mode && view === 'graph') q.set('mode', mode);
    q.delete('treehouseShare');
    canonicalPath = withQuery(VIEW_PATH[view] || '/editor', q);
  } else if (seg.target === 'settings') {
    canonicalPath = settingsPanel ? `/settings/${encodeURIComponent(settingsPanel)}` : '/settings';
  } else {
    canonicalPath = withQuery(DIRECT[seg.target] || '/', params);
  }

  return {
    target: seg.target,
    view: seg.view || null,
    mode,
    doc,
    panel: settingsPanel,
    openBases: !!seg.openBases,
    legacy,
    canonicalPath,
  };
}

/** True when a navigation request should be answered by the SPA shell. */
export function isShellNavigation(pathname) {
  const path = String(pathname || '/').replace(/\/+$/, '') || '/';
  if (SHELL_PATHS.includes(path)) return true;
  return SHELL_PATH_PREFIXES.some((prefix) => path === prefix.replace(/\/$/, '') || path.startsWith(prefix));
}

export default { SHELL_PATHS, SHELL_PATH_PREFIXES, appletPath, resolveAppletLocation, isShellNavigation };
