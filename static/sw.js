// static/sw.js — Odysseus PWA Service Worker
// Strategy:
//   - Protected HTML: network only; a cached app shell cannot prove a session.
//   - JS/CSS (/static/*.js|.css): network-first, cache fallback for offline.
//     (So code/style edits show up on a normal reload, no manual cache clear.)
//   - Other allowed static assets (icons/fonts/libs/translations): network-first, offline fallback.
//   - API / non-GET: never cached.
// Bump CACHE_NAME whenever the precache list or SW logic changes.
const CACHE_NAME = 'open-clank-v360-wiki-app';

// Mirror of static/js/appletRoutes.js SHELL_PATHS + SHELL_PATH_PREFIXES.
// The service worker cannot import the ES module, so keep this list in sync
// with the registry (pinned by tests/test_shell_applet_routes_s13.py).
const SHELL_NAV_PATHS = [
  '/', '/editor', '/wiki', '/files', '/graph', '/treehouse', '/timeline',
  '/todo', '/calendar', '/notes', '/code', '/bases', '/mind', '/galaxy',
  '/email', '/memory', '/tasks', '/library', '/cookbook', '/usage', '/settings',
];
const SHELL_NAV_PREFIXES = ['/settings/', '/copal/'];

function isShellNavigation(pathname) {
  const path = pathname.replace(/\/+$/, '') || '/';
  if (SHELL_NAV_PATHS.includes(path)) return true;
  return SHELL_NAV_PREFIXES.some((prefix) => path === prefix.replace(/\/$/, '') || path.startsWith(prefix));
}

// Cache static code/assets for offline use; protected HTML is never precached.
const PRECACHE = [
  '/static/style.css',
  '/static/app.js',
  '/static/js/i18n.js',
  '/static/js/custom-context-menu.js',
  '/static/js/contextualHelp.js',
  '/static/js/statsUsage.js',
  '/static/js/chatWorkspace.js',
  '/static/i18n/registry.json',
  '/static/i18n/en.json',
  '/static/i18n/zh-Hans.json',
  '/static/i18n/ja.json',
  '/static/i18n/ko.json',
  '/static/i18n/es.json',
  '/static/i18n/hi.json',
  '/static/i18n/ar.json',
  '/static/i18n/ru.json',
  '/static/i18n/pt.json',
  '/static/i18n/id.json',
  '/static/i18n/pa-Guru.json',
  '/static/i18n/bn.json',
  '/static/i18n/sw.json',
  '/static/i18n/ur.json',
  '/static/i18n/fa.json',
  '/static/i18n/bg.json',
  '/static/i18n/bs.json',
  '/static/i18n/hr.json',
  '/static/i18n/cs.json',
  '/static/i18n/da.json',
  '/static/i18n/de.json',
  '/static/i18n/el.json',
  '/static/i18n/es-419.json',
  '/static/i18n/fi.json',
  '/static/i18n/fr.json',
  '/static/i18n/hu.json',
  '/static/i18n/it.json',
  '/static/i18n/ms.json',
  '/static/i18n/nl.json',
  '/static/i18n/no.json',
  '/static/i18n/pl.json',
  '/static/i18n/pt-BR.json',
  '/static/i18n/ro.json',
  '/static/i18n/sv.json',
  '/static/i18n/th.json',
  '/static/i18n/tr.json',
  '/static/i18n/uk.json',
  '/static/i18n/vi.json',
  '/static/i18n/zh-Hant.json',
  '/static/manifest.en.json',
  '/static/manifest.zh-Hans.json',
  '/static/manifest.ja.json',
  '/static/manifest.ko.json',
  '/static/manifest.es.json',
  '/static/manifest.hi.json',
  '/static/manifest.ar.json',
  '/static/manifest.ru.json',
  '/static/manifest.pt.json',
  '/static/manifest.id.json',
  '/static/manifest.pa-Guru.json',
  '/static/manifest.bn.json',
  '/static/manifest.sw.json',
  '/static/manifest.ur.json',
  '/static/manifest.fa.json',
  '/static/js/storage.js',
  '/static/js/ui.js',
  '/static/js/markdown.js',
  '/static/js/dragSort.js',
  '/static/js/sessions.js',
  '/static/js/memory.js',
  '/static/js/skills.js',
  '/static/js/tourHints.js',
  '/static/js/fileHandler.js',
  '/static/js/voiceRecorder.js',
  '/static/js/models.js',
  '/static/js/rag.js',
  '/static/js/presets.js',
  '/static/js/search.js',
  '/static/js/spinner.js',
  '/static/js/tts-ai.js',
  '/static/js/document.js',
  '/static/js/chatRenderer.js',
  '/static/js/codeRunner.js',
  '/static/js/chatStream.js',
  '/static/js/chat.js',
  '/static/js/chatModelProvenance.js',
  '/static/js/chatStreamErrors.js',
  '/static/js/liveThinkingThrottle.js',
  '/static/js/cookbook.js',
  '/static/js/search-chat.js',
  '/static/js/compare/index.js',
  '/static/js/theme.js',
  '/static/js/censor.js',
  '/static/js/settings.js',
  '/static/js/admin.js',
  '/static/js/init.js',
  '/static/js/slashCommands.js',
  '/static/js/emailInbox.js',
  '/static/js/emailLibrary/utils.js',
  '/static/js/emailLibrary/signatureFold.js',
  '/static/js/emailLibrary/state.js',
  '/static/js/notes.js',
  '/static/js/tasks.js',
  '/static/js/calendar.js',
  '/static/js/calendar/utils.js',
  '/static/js/calendar/reminders.js',
  '/static/js/group.js',
  '/static/js/keyboard-shortcuts.js',
  '/static/js/permission-mode.js',
  '/static/js/interaction-mode.js',
  '/static/js/sidebar-layout.js',
  '/static/js/section-management.js',
  '/static/lib/shiki.bundle.js',
  '/static/js/highlighter.js',
  '/static/lib/mermaid.min.js',
];

function cacheableAsset(path) {
  if (!path.startsWith('/static/') || path.split('/').some(p => p === '.' || p === '..')) return false;
  return /\.(js|css|woff2?|ttf|otf)$/.test(path)
    || /^\/static\/icons\/[^/]+\.(png|svg|ico)$/.test(path)
    || /^\/static\/i18n\/[A-Za-z0-9_-]+\.json$/.test(path)
    || /^\/static\/manifest(?:\.[A-Za-z0-9_-]+)?\.json$/.test(path);
}

function safeAssetResponse(response) {
  return response && response.ok && !response.redirected
    && !/text\/html/i.test(response.headers.get('content-type') || '');
}

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE_NAME).then(cache => Promise.all(
    PRECACHE.filter(path => cacheableAsset(path)).map(path =>
      fetch(path, { cache: 'reload' })
        .then(response => safeAssetResponse(response) ? cache.put(path, response) : null)
        .catch(() => null)
    )
  )));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.map(async key => {
    if (key !== CACHE_NAME) return caches.delete(key);
    const cache = await caches.open(key);
    // Purge shells/private resources even if they were placed in this cache
    // by an older worker or a partially completed installation.
    const requests = await cache.keys();
    await Promise.all(requests.filter(request => !cacheableAsset(new URL(request.url).pathname))
      .map(request => cache.delete(request)));
  }))).then(() => self.clients.claim()));
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin || url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;

  // App HTML always reaches the authenticated server, including direct static
  // HTML and old shell aliases. Public login remains available from the server.
  if (e.request.mode === 'navigate' || /\.html?$/.test(url.pathname)) {
    e.respondWith(fetch(e.request, { cache: 'no-store' }).catch(() => Response.error()));
    return;
  }
  if (!cacheableAsset(url.pathname)) return;

  // Preserve the PWA's offline static assets. Authentication redirects or HTML
  // never enter an asset cache and cannot become a cached app shell.
  e.respondWith(fetch(e.request).then(async response => {
    if (safeAssetResponse(response)) {
      const cache = await caches.open(CACHE_NAME);
      await cache.put(e.request, response.clone());
    }
    return response;
  }).catch(async () => {
    const cached = await caches.match(e.request);
    return safeAssetResponse(cached) ? cached : Response.error();
  }));
});
