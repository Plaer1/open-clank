import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { withCopalBrowser } from './helpers/copal_browser_fixture.mjs';

// This fixture deliberately serves production Copal and Files modules. The
// only fake boundary is the HTTP/native provider response; a real Files row
// dispatches through the real facade client into the real Copal ResourceHandle
// and CodeMirror buffer owner.
const page = `<!doctype html><meta charset="utf-8"><body>
<nav aria-label="Editor compatibility routes">
  <a href="/copal/editor" data-copal-view="notes">Editor</a>
  <a href="/code" data-copal-launcher="code" data-copal-compat="true" style="display:none">Editor</a>
  <a href="/notes" data-copal-launcher="notes" data-copal-compat="true" style="display:none">Editor</a>
</nav>
<main id="app"></main>
<script type="module">
  const hostMetadata = Object.freeze({ encoding:'utf-16-le', newline:'\\r\\n', bomBytes:2, mode:'markdown', language:'markdown' });
  const hostResource = (ref = 'rr1.host-readme-open') => ({
    ref, id:'resource-host-readme', provider:'host', kind:'file', name:'README.md',
    mime_type:'text/markdown', capabilities:['stat','open','preview'],
  });
  const payload = (ref = 'rr1.host-readme-open', revision = 'fp-1', text = '# Host resource\\r\\n') => ({
    target:{ app:'editor' }, exact:true, resource:hostResource(ref),
    payload:{
      name:'README.md', kind:'text', corpus:'host', text,
      representation:'markdown',
      resource:{
        ref, key:{ accountId:'account-owner', workspaceId:'host', provider:'host', resourceId:'resource-host-readme' },
        revision:{ kind:'hostFingerprint', value:revision }, representation:'markdown',
        locator:{ displayName:'README.md', locationLabel:'README.md', opaqueRef:ref },
        metadata:hostMetadata,
        capabilities:{ read:true, edit:true, rename:false, delete:false, watch:false },
      },
    },
  });
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers:{ 'Content-Type':'application/json' } });
  const failure = (code, message, status) => json({ detail:{ code, message } }, status);
  class FixtureEventSource { addEventListener() {} close() {} }
  window.EventSource = FixtureEventSource;
  window.__account = 'owner';
  window.__saveMode = 'applied';
  window.__saveRequests = [];
  window.__openRequests = [];
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), location.origin);
    const method = String(init.method || 'GET').toUpperCase();
    const body = init.body ? JSON.parse(init.body) : {};
    if (url.pathname === '/api/auth/status') return json({ ok:true, username:window.__account, is_admin:true });
    if (url.pathname === '/api/copal/status') return json({ storage_namespace:'fixture-copal-' + window.__account, account_id:'account-' + window.__account });
    if (url.pathname === '/api/copal/documents') return json({ docs:[] });
    if (url.pathname === '/api/copal/planning') return json({ tracks:[], floatingTodos:[] });
    if (url.pathname === '/api/prefs/copal_entry_visibility') return json({ value:{} });
    if (url.pathname === '/api/files-v1/roots') return json({
      version:1, policy_generation:3,
      entries:[{ id:'host-root', ref:'rr1.host-root', provider:'host', kind:'provider_root', name:'Host locations', capabilities:['children','stat'], sort_keys:['name'] }],
    });
    if (url.pathname === '/api/files-v1/children') return json({
      entries: body.parent_ref === 'rr1.host-root'
        ? [{ id:'resource-host-readme', ref:'rr1.host-readme', provider:'host', kind:'file', name:'README.md', mime_type:'text/markdown', capabilities:['stat','open','preview'], sort_keys:['name'] }]
        : [], next_cursor:null,
    });
    if (url.pathname === '/api/files-v1/places') return json({ entries:[] });
    if (url.pathname === '/api/files-v1/workspaces') return json({ entries:[] });
    if (url.pathname === '/api/files-v1/action' && method === 'POST' && body.action === 'open') {
      window.__openRequests.push({ kind:'action', body });
      return json({ resource:hostResource('rr1.host-readme-open'), target:{ app:'editor' }, exact:true });
    }
    if (url.pathname === '/api/files-v1/open-resource' && method === 'POST') {
      window.__openRequests.push({ kind:'open-resource', body });
      return json(payload());
    }
    if (url.pathname === '/api/files-v1/save-resource' && method === 'POST') {
      window.__saveRequests.push(body);
      if (window.__saveMode === 'conflict') return json({ outcome:'conflict', remote:{
        revision:{ kind:'hostFingerprint', value:'fp-remote' }, envelope:{ text:'REMOTE AUTHORITATIVE\\r\\n', metadata:hostMetadata },
      } });
      if (window.__saveMode === 'failure') return failure('provider_unavailable', 'fixture save outage', 503);
      if (window.__saveMode === 'revoked') return failure('resource_unavailable', 'fixture resource revoked', 404);
      return json({ outcome:'applied', revision:{ kind:'hostFingerprint', value:'fp-2' }, snapshot:{
        revision:{ kind:'hostFingerprint', value:'fp-2' }, envelope:{ text:body.text, metadata:hostMetadata },
      } });
    }
    if (url.pathname === '/api/copal/events') return json({});
    return failure('not_found', 'unexpected fixture request: ' + url.pathname, 404);
  };

  window.__run = async () => {
    const copalModule = await import('/static/js/copal.js?editor-route-fixture');
    const filesModule = await import('/static/js/files.js?editor-route-fixture');
    window.copalModule = copalModule.default;
    window.filesModule = filesModule.default;
    await copalModule.init(location.origin);
    await filesModule.default.open();
    return true;
  };
  window.__flushEditor = () => window.copalModule.flushActiveCopalResource?.() || window.copalModule.flushActiveAgentResource();
  window.__clickSave = () => {
    const menu = document.querySelector('.copal-notes-window:not(.hidden) .copal-leaf-menu:not(.copal-group-menu)');
    if (!menu) return false;
    menu.open = true;
    const save = [...menu.querySelectorAll('button')].find(button => button.textContent.trim() === 'Save');
    save?.click();
    return Boolean(save);
  };
  window.__setSourceMode = () => {
    const menu = document.querySelector('.copal-notes-window:not(.hidden) .copal-leaf-menu:not(.copal-group-menu)');
    const source = [...(menu?.querySelectorAll('button') || [])].find(button => button.textContent.trim() === 'Source mode');
    source?.click();
    return Boolean(source);
  };
  window.__editorState = () => ({
    text:document.querySelector('.copal-notes-window:not(.hidden) .cm-content')?.textContent || '',
    windows:[...document.querySelectorAll('.copal-notes-window:not(.hidden)')].length,
    saveStates:[...document.querySelectorAll('.copal-notes-window:not(.hidden) .copal-save-state')].map(node => node.textContent),
  });
  window.__openViaCodeCompatibility = async ref => {
    const codeModule = await import('/static/js/codeEditor.js?editor-route-code');
    return codeModule.openResource(ref);
  };
</script>`;

await withCopalBrowser({ page }, async ({ cdp, evaluate, until }) => {
  await until('window.__run');
  await evaluate('window.__run()');
  await until("document.querySelector('.files-entry.file')?.textContent.includes('README.md')");

  // Keep the launcher gate tied to the shipped static shell as well as this
  // isolated fixture. The legacy rail node remains wired for compatibility
  // but must not create a second visible Editor launcher.
  const staticIndex = fs.readFileSync(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../static/index.html'), 'utf8');
  const railEditor = staticIndex.match(/<button[^>]*data-copal-view="notes"[^>]*title="Editor"/g) || [];
  const legacyRail = staticIndex.match(/<button[^>]*id="rail-notes"[^>]*>/g) || [];
  assert.equal(railEditor.length, 1);
  assert.equal(legacyRail.length, 1);
  assert.match(legacyRail[0], /display\s*:\s*none/);

  const visibleEditorLaunchers = await evaluate("Array.from(document.querySelectorAll('[data-copal-view=\\\"notes\\\"], [data-copal-launcher=\\\"code\\\"], [data-copal-launcher=\\\"notes\\\"]')).filter(node => getComputedStyle(node).display !== 'none' && !node.hidden).map(node => ({ href:node.getAttribute('href'), text:node.textContent.trim() }))");
  assert.deepEqual(visibleEditorLaunchers, [{ href:'/copal/editor', text:'Editor' }]);
  assert.equal(await evaluate("document.querySelectorAll('[data-copal-compat=\"true\"]').length"), 2);

  // This is the production Files double-click listener and exact-open action.
  await evaluate("document.querySelector('.files-entry.file').dispatchEvent(new MouseEvent('dblclick', { bubbles:true }))");
  await until("document.querySelector('.copal-notes-window:not(.hidden) .cm-content')?.textContent.includes('Host resource')");
  const opened = await evaluate('({ action:window.__openRequests[0], exact:window.__openRequests[1], state:window.__editorState() })');
  assert.equal(opened.action.body.action, 'open');
  assert.equal(opened.exact.body.resource_ref, 'rr1.host-readme-open');
  assert.equal(opened.state.windows, 1);

  // The legacy /code handoff calls the same Copal owner and does not create a
  // second visible window or a second ResourceHandle buffer.
  await evaluate("window.__openViaCodeCompatibility('rr1.host-readme-open')");
  await until('window.__openRequests.length >= 3');
  assert.equal(await evaluate("document.querySelectorAll('.copal-notes-window:not(.hidden)').length"), 1);
  // Host Markdown opens in Live Markdown by default. Exercise that path
  // before switching presentations: both modes share the same CAS owner.
  await evaluate("document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()");
  await cdp('Input.insertText', { text:'LOCAL-LIVE' });
  assert.equal(await evaluate('window.__saveMode = "applied"; window.__clickSave()'), true);
  await until('window.__saveRequests.length === 1');
  const applied = await evaluate('window.__saveRequests[0]');
  assert.equal(applied.resource_ref, 'rr1.host-readme-open');
  assert.equal(applied.expected_revision.kind, 'hostFingerprint');
  assert.equal(applied.expected_revision.value, 'fp-1');
  assert.equal(Object.hasOwn(applied, 'path'), false);
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-LIVE")'), true);

  assert.equal(await evaluate('window.__setSourceMode()'), true);
  await until("document.querySelector('.copal-notes-window:not(.hidden) .copal-codemirror-host')?.dataset.syntaxReady === 'ready'");
  await evaluate("document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()");
  await cdp('Input.insertText', { text:'LOCAL-SOURCE' });
  await evaluate("window.__openViaCodeCompatibility('rr1.host-readme-open')");
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-SOURCE")'), true, 'double-open preserves dirty text');
  await evaluate('window.__saveMode = "applied"; window.__clickSave()');
  await until('window.__saveRequests.length === 2');
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-SOURCE")'), true);

  await evaluate("document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()");
  await cdp('Input.insertText', { text:'LOCAL-CONFLICT' });
  await evaluate('window.__saveMode = "conflict"; window.__clickSave()');
  await until('window.__saveRequests.length === 3');
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-CONFLICT")'), true);
  assert.equal(await evaluate('window.__editorState().saveStates.includes("Conflict")'), true);

  await evaluate("document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()");
  await cdp('Input.insertText', { text:'LOCAL-FAILURE' });
  await evaluate('window.__saveMode = "failure"; window.__clickSave()');
  await until('window.__saveRequests.length === 4');
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-FAILURE")'), true);

  await evaluate("document.querySelector('.copal-notes-window:not(.hidden) .cm-content').focus()");
  await cdp('Input.insertText', { text:'LOCAL-REVOKED' });
  await evaluate('window.__saveMode = "revoked"; window.__clickSave()');
  await until('window.__saveRequests.length === 5');
  assert.equal(await evaluate('window.__editorState().text.includes("LOCAL-REVOKED")'), true);

  // init() is the real account-scope teardown. The prior window and editor
  // are destroyed before the new account receives its empty workspace.
  await evaluate('window.__account = "other"; window.copalModule.init(location.origin)');
  await until('document.querySelectorAll(".copal-notes-window:not(.hidden)").length === 1');
  assert.equal(await evaluate('document.querySelector(".copal-notes-window:not(.hidden) .cm-content")?.textContent || ""'), '');
  assert.equal(await evaluate('window.__saveRequests.at(-1).resource_ref'), 'rr1.host-readme-open');

  console.log('Production Editor route: Files exact-open, /code handoff, one CodeMirror owner, host CAS applied/conflict/failure/revocation draft retention, launcher dedupe, and account teardown passed.');
});
