// Behavioral open-path coverage for the TreeHouse one-shot share token.
//
// Drives the production deep-link sequence — both the new direct address
// (`/treehouse?treehouseShare=…`) and the legacy alias
// (`/copal/treehouse?treehouseShare=…`) — through the real shell registry and
// the real TreeHouse consumer, then asserts the token is delivered to the
// consumer and stripped only after acceptance. Not a source-text pin: the
// open-path rewrite and `load()` are actually executed.
import assert from 'node:assert/strict';
import test from 'node:test';

import { appletPath, resolveAppletLocation, updateAppletRoute } from '../../static/js/appletRoutes.js';
import { createTreeHouseFeature } from '../../static/js/copal/treehouse.js';
import { configureCopalStorage } from '../../static/js/copal/storage.js';

class FakeNode {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase();
    this.attrs = attrs;
    this.children = [];
    this.textContent = attrs.text || '';
    this.onclick = attrs.onclick;
    this.classList = { add() {}, remove() {}, toggle() {}, contains: () => false };
    this.style = {};
  }
  append(...children) { this.children.push(...children.filter((child) => child != null)); }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  removeEventListener() {}
  focus() {}
  remove() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get value() { return this._value || ''; }
  set value(value) { this._value = value; }
  click() { return this.onclick?.(); }
}

function fakeH(tag, attrs = {}, ...children) {
  const node = new FakeNode(tag, attrs);
  node.append(...children);
  return node;
}

function makeSnapshot() {
  return {
    accountId: 'acct-bob',
    workspace: 'school',
    actor: { id: 'acct-bob', displayName: 'Bob' },
    permissions: { admin: true, author: true, learner: true, analytics: true, grade: true },
    courseCapabilities: { 'course:shared': { learn: true, edit: false, owner: false, author: false } },
    state: {
      revision: 1,
      profiles: { 'acct-bob': { id: 'acct-bob', roles: ['admin', 'instructor', 'learner'], active: true } },
      courses: {
        'course:shared': {
          id: 'course:shared', title: 'Shared', description: 'Read only',
          status: 'published', moduleIds: [],
        },
      },
      modules: {}, activities: {}, assignments: {}, skills: {}, badges: {},
      quests: {}, courseGrants: {}, enrollments: {}, submissions: {}, evidence: {}, events: [],
    },
    projection: {
      eventCount: 0,
      learners: { 'acct-bob': { points: 0, badges: [], quests: [], courses: {}, skills: {}, pointEvidence: [] } },
      leaderboard: [], courses: {},
    },
  };
}

// Installs the browser globals the open path and the consumer touch, with a
// history that keeps `location` in sync exactly like the DOM does.
function installBrowserGlobals(initialHref) {
  const storage = new Map();
  const session = new Map();
  const location = new URL(initialHref);
  const historyCalls = [];
  const history = {
    replaceState(state, title, url) {
      historyCalls.push({ kind: 'replace', url: String(url) });
      const next = new URL(String(url), location.href);
      location.href = next.href;
      location.pathname = next.pathname;
      location.search = next.search;
      location.hash = next.hash;
    },
    pushState(state, title, url) {
      historyCalls.push({ kind: 'push', url: String(url) });
      const next = new URL(String(url), location.href);
      location.href = next.href;
      location.pathname = next.pathname;
      location.search = next.search;
      location.hash = next.hash;
    },
  };
  globalThis.localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
    removeItem: (key) => storage.delete(key),
  };
  globalThis.sessionStorage = {
    getItem: (key) => session.get(key) ?? null,
    setItem: (key, value) => session.set(key, String(value)),
    removeItem: (key) => session.delete(key),
  };
  globalThis.location = location;
  globalThis.history = history;
  globalThis.document = { body: new FakeNode('body'), activeElement: null, createElement: (tag) => new FakeNode(tag) };
  globalThis.window = {
    location, history, document: globalThis.document,
    localStorage: globalThis.localStorage, sessionStorage: globalThis.sessionStorage,
    styledConfirm: async () => false,
    dispatchEvent: () => true,
    addEventListener() {},
    removeEventListener() {},
  };
  return { location, historyCalls, storage, session };
}

function makeApi(log) {
  return async (path, options = {}) => {
    const method = String(options.method || 'GET').toUpperCase();
    if (method === 'GET') {
      // Record the live address at the moment the consumer's `load()` is
      // fetching: this is the read window the shell rewrite must not close.
      log.reads.push({ path: String(path), search: globalThis.location.search });
      return makeSnapshot();
    }
    const body = JSON.parse(options.body || '{}');
    log.writes.push({ path: String(path), type: body.type, payload: body.payload });
    if (body.type === 'course.accept_share') return { ...makeSnapshot(), result: { accepted: true } };
    return { ...makeSnapshot(), result: {} };
  };
}

// The production cold deep-link sequence:
//   app.js `_openResolvedTarget`  -> resolveAppletLocation + replaceState(canonicalPath)
//   copal.js `open` -> `show` -> `onActivate` -> `updateRoute` -> updateAppletRoute
//   copal.js `renderTreeHouse` -> treehouse `render` -> async `load()` consumer
async function runOpenPath({ href, expectedToken, expectedView }) {
  const { location, historyCalls } = installBrowserGlobals(href);
  configureCopalStorage(`share-open-path-${expectedToken}-${expectedView}`);

  const log = { reads: [], writes: [] };
  const feature = createTreeHouseFeature({
    h: fakeH,
    api: makeApi(log),
    setStatus() {},
    renderMarkdown: (text) => text,
    openDocument() {},
  });
  feature.loadState();

  // 1. _openResolvedTarget: normalize legacy aliases onto the canonical
  //    direct address without adding a history entry. The token must survive.
  const resolved = resolveAppletLocation(location.pathname, location.search);
  assert.ok(resolved, `deep link ${href} resolves to a shell target`);
  const current = location.pathname + location.search;
  if (resolved.canonicalPath && resolved.canonicalPath !== current) {
    history.replaceState({}, '', resolved.canonicalPath);
  }

  // 2. onActivate -> updateRoute -> updateAppletRoute: the synchronous
  //    window-show rewrite. This is the regression being pinned — the token
  //    must still be on the live address after it. `doc` comes from the live
  //    address the way copal.js's route opener seeds the window selection.
  const doc = new URLSearchParams(location.search).get('doc');
  const rewritten = updateAppletRoute(expectedView, { doc: doc || undefined }, true);
  assert.ok(
    new URLSearchParams(location.search).has('treehouseShare'),
    `onActivate rewrite kept treehouseShare for ${href} (got ${location.search})`,
  );
  assert.ok(
    String(rewritten).includes(`treehouseShare=${expectedToken}`),
    `updateAppletRoute emitted the token for ${href} (got ${rewritten})`,
  );
  assert.equal(new URLSearchParams(location.search).get('treehouseShare'), expectedToken);

  // 3. The consumer runs after the rewrite (production: show() is sync, load()
  //    is async). It must see the token, accept it, and only then strip it.
  const body = new FakeNode('main');
  await feature.render(body);

  const accept = log.writes.find((entry) => entry.type === 'course.accept_share');
  assert.ok(accept, `consumer accepted the share for ${href}`);
  assert.equal(accept.payload?.shareToken, expectedToken, `accept_share payload for ${href}`);

  // The token was present on the live URL at the moment load() read it.
  const read = log.reads[0];
  assert.ok(read, `consumer fetched its snapshot for ${href}`);
  assert.ok(
    new URLSearchParams(read.search).get('treehouseShare') === expectedToken,
    `load() saw treehouseShare=${expectedToken} for ${href} (read with ${read.search})`,
  );

  // Consumer-only strip: the token is gone, unrelated query state stays.
  assert.equal(
    new URLSearchParams(location.search).has('treehouseShare'),
    false,
    `consumer stripped treehouseShare for ${href} (left ${location.search})`,
  );
  const searchAfterConsumer = String(location.search);

  // A later shell rewrite must not resurrect a consumed token. Production
  // updateRoute passes `doc` from the window selection (seeded from the live
  // address at open), so mirror that here rather than rewriting with {}.
  const after = updateAppletRoute(expectedView, { doc: doc || undefined }, true);
  assert.equal(
    String(after).includes('treehouseShare'),
    false,
    `consumed token does not come back for ${href} (got ${after})`,
  );

  return { location, historyCalls, rewritten, after, resolved, log, searchAfterConsumer };
}

test('deep-link /treehouse?treehouseShare= delivers the token to the consumer, which alone strips it', async () => {
  const result = await runOpenPath({
    href: 'https://example.test/treehouse?treehouseShare=TOK-A&doc=d1',
    expectedToken: 'TOK-A',
    expectedView: 'treehouse',
  });
  // Unrelated query state survives the consumer strip.
  assert.equal(new URLSearchParams(result.searchAfterConsumer).get('doc'), 'd1');
  assert.equal(new URLSearchParams(result.location.search).get('doc'), 'd1');
  assert.equal(result.resolved.legacy, false);
  assert.equal(result.resolved.canonicalPath.startsWith('/treehouse?'), true);
});

test('legacy /copal/treehouse?treehouseShare= delivers the token to the consumer, which alone strips it', async () => {
  const result = await runOpenPath({
    href: 'https://example.test/copal/treehouse?treehouseShare=TOK-B',
    expectedToken: 'TOK-B',
    expectedView: 'treehouse',
  });
  assert.equal(result.resolved.legacy, true);
  // The alias was normalized onto the direct address and the token survived
  // that rewrite too.
  assert.equal(result.resolved.canonicalPath, '/treehouse?treehouseShare=TOK-B');
  assert.equal(result.location.pathname, '/treehouse');
});

test('appletPath still builds fresh destinations without one-shot tokens', () => {
  // Consumers of appletPath for hrefs (contextualHelp lesson links, editor
  // pushState) must never inherit a share token from an explicit search.
  assert.equal(appletPath('treehouse', { search: 'treehouseShare=tok&doc=d' }), '/treehouse?doc=d');
  assert.equal(appletPath('editor', { search: 'treehouseShare=tok' }), '/editor');
});

test('updateAppletRoute carries only unconsumed one-shot tokens from the live search', async () => {
  const { location } = installBrowserGlobals('https://example.test/treehouse?treehouseShare=TOK-C&doc=d9');
  assert.equal(
    updateAppletRoute('treehouse', { doc: 'd9' }, true),
    '/treehouse?doc=d9&treehouseShare=TOK-C',
  );
  assert.equal(new URLSearchParams(location.search).get('treehouseShare'), 'TOK-C');
  // A view that is not the consumer still keeps the unconsumed token: the
  // consumer is the only stripper, so an abandoned share cannot be eaten by
  // a shell rewrite.
  assert.equal(
    updateAppletRoute('notes', {}, true),
    '/editor?treehouseShare=TOK-C',
  );
});
