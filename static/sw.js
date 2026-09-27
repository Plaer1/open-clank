// static/sw.js — Odysseus PWA Service Worker
// Strategy:
//   - HTML (navigation): network-first, cache fallback. The HTML and module
//     graph must move together after an update.
//   - JS/CSS (/static/*.js|.css): network-first, cache fallback for offline.
//     (So code/style edits show up on a normal reload, no manual cache clear.)
//   - Other static assets (images/fonts/libs): cache-first with bg refresh.
//   - API / non-GET: never cached.
// Bump CACHE_NAME whenever the precache list or SW logic changes.
const CACHE_NAME = 'open-clank-v357-usage-workspace';

// Mirror of static/js/appletRoutes.js SHELL_PATHS + SHELL_PATH_PREFIXES.
// The service worker cannot import the ES module, so keep this list in sync
// with the registry (pinned by tests/test_shell_applet_routes_s13.py).
const SHELL_NAV_PATHS = [
  '/', '/editor', '/files', '/wiki', '/graph', '/treehouse', '/timeline',
  '/todo', '/calendar', '/notes', '/code', '/bases', '/mind', '/galaxy',
  '/email', '/memory', '/gallery', '/tasks', '/library', '/cookbook', '/usage', '/settings',
];
const SHELL_NAV_PREFIXES = ['/settings/', '/copal/'];

function isShellNavigation(pathname) {
  const path = pathname.replace(/\/+$/, '') || '/';
  if (SHELL_NAV_PATHS.includes(path)) return true;
  return SHELL_NAV_PREFIXES.some((prefix) => path === prefix.replace(/\/$/, '') || path.startsWith(prefix));
}

// Core shell precached on install so repeat opens are instant without any
// network wait. Keep this list in sync with the <script type="module"> tags
// and <link rel="stylesheet"> in index.html.
const PRECACHE = [
  '/',
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

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      // addAll is atomic — if any item fails, none are cached. Use individual
      // puts so a single 404 can't block the whole install.
      Promise.all(
        PRECACHE.map(url =>
          fetch(url, { cache: 'reload' })
            .then(res => res.ok ? cache.put(url, res) : null)
            .catch(() => null)
        )
      )
    )
  );
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);

  // Never touch API calls or non-GET.
  if (url.pathname.startsWith('/api/') || e.request.method !== 'GET') return;

  // HTML navigation: network-first for the app shell — but ONLY for known
  // shell/applet paths (direct applet addresses, /settings/<panel>, legacy
  // /copal/*). Other navigations (e.g. a deep-linked /static/*.html page)
  // must go to the network/static handlers below; otherwise every navigation
  // was served the app index, replacing the page the user actually asked for.
  // Unknown applet-ish URLs are deliberately NOT shell-caught so they keep
  // meaningful errors.
  if (e.request.mode === 'navigate' && isShellNavigation(url.pathname)) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const cached = await cache.match('/');
        try {
          const network = await fetch(e.request);
          if (network && network.ok) cache.put('/', network.clone());
          return network;
        } catch {
          return cached || Response.error();
        }
      })
    );
    return;
  }

  // JS/CSS: network-first — always try the network so code/style edits show up
  // on a normal reload; fall back to cache only when offline.
  if (url.pathname.startsWith('/static/') && /\.(js|css)(\?|$)/.test(url.pathname + url.search)) {
    e.respondWith(
      fetch(e.request).then(res => {
        if (res && res.ok) {
          const copy = res.clone();
          caches.open(CACHE_NAME).then(cache => cache.put(e.request, copy));
        }
        return res;
      }).catch(() => caches.match(e.request))
    );
    return;
  }

  // Other static assets (images, fonts, libs): cache-first with background refresh.
  if (url.pathname.startsWith('/static/')) {
    e.respondWith(
      caches.open(CACHE_NAME).then(async cache => {
        const cached = await cache.match(e.request);
        const fetching = fetch(e.request).then(res => {
          if (res && res.ok) cache.put(e.request, res.clone());
          return res;
        }).catch(() => cached);
        return cached || fetching;
      })
    );
    return;
  }
});
