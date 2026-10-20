import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import fs from 'node:fs/promises';
import { NATIVE_SURFACE_ACTIONS } from '../../static/js/nativeSurfaceCatalog.js';
import { buildCommandCatalog, resolveCatalogCommand, filterCommandCatalog } from '../../static/js/commandCatalog.js';
import { restoreValidatedComposerDraft } from '../../static/js/composerDraftRestore.js';

function element() {
  const listeners = new Map();
  return { children: [], listeners, style: {}, dataset: {}, attributes: {}, value: '', classList: { add() {}, remove() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; }, append(...children) { this.children.push(...children); },
    addEventListener(name, callback) { listeners.set(name, callback); }, setAttribute(name, value) { this.attributes[name] = value; },
    querySelectorAll() { return []; }, querySelector() { return null; }, getBoundingClientRect() { return { left: 0, width: 500, top: 500, bottom: 540 }; },
    focus() {}, setSelectionRange() {}, dispatchEvent(event) { listeners.get(event.type)?.(event); }, contains(child) { return this.children.includes(child); },
    get textContent() { return this._text || this.innerHTML?.replace(/<[^>]+>/g, '') || this.children.map(c => c.textContent).join(''); },
    set textContent(value) { this._text = value; }, remove() {},
  };
}
async function shipped() {
  const history = element(); const textarea = element(); const calls = []; const settingsCalls = []; const selected = []; const copied = []; const toasts = []; const timers = [];
  let sid = 'session-a';
  let operationError = '';
  let operationErrorTarget = '';
  let revision = 7;
  const refreshed = [];
  const refreshOptions = []; const opened = [];
  let restoreReceipt = null;
  let fetchGate = null;
  const nodes = { 'chat-history': history, message: textarea, 'chat-form': element() };
  const listeners = new Map();
  const doc = { body: element(), getElementById(id) { return nodes[id] || doc.body.children.find(e => e.id === id) || null; }, createElement: element, dispatchEvent(event) { for (const callback of listeners.get(event.type) || []) callback(event); }, querySelectorAll() { return []; },
    addEventListener(event, callback) { if (!listeners.has(event)) listeners.set(event, []); listeners.get(event).push(callback); } };
  const state = { available: true, modes: { availableModes: [{ id: 'reviewer', name: 'Reviewer' }] }, commands: [{ name: 'goal', description: 'Manage goal', origin: 'builtin' }, { name: 'collide', description: 'Native collision' }, { name: 'native-only', description: 'Native only' }] };
  let skills = [{ name: 'collide', help: 'Skill collision' }, ...Array.from({ length: 65 }, (_, i) => ({ name: `skill-${i}`, help: `Skill number ${i}` }))];
  const draftState = () => ({ sid, epoch: 0, version: 0, text: textarea.value, attachments: null, pending: [] });
  const context = vm.createContext({ document: doc, window: { innerHeight: 900, addEventListener() {}, open: (...args) => opened.push(args), chatModule: { captureComposerDraftState: draftState, restoreComposerDraft(receipt, options) { return restoreValidatedComposerDraft(receipt, { ...options, read: draftState, commit: value => { textarea.value = value.text; } }); } }, __odysseusSetChatMode: mode => selected.push(mode) }, console, Date, URL, URLSearchParams, Event,
    setTimeout(callback) { timers.push(callback); }, setInterval() {}, clearInterval() {}, requestAnimationFrame() {},
    fetch: async (url, options) => {
      calls.push({ url, options });
      if (fetchGate && url.includes(fetchGate.pattern)) { const gate = fetchGate; fetchGate = null; gate.enter(); await gate.paused; }
      const payload = options?.body ? (() => { try { return JSON.parse(options.body); } catch { return {}; } })() : {};
      const failure = url.includes('mimo-operation') && operationError && (!operationErrorTarget || operationErrorTarget === payload.operation);
      let data;
      if (failure) data = { detail: operationError };
      else if (url.includes('mimo-operation')) {
        data = payload.operation === 'btw' ? { result: { answer: 'Side answer' } }
          : payload.operation === 'recovery' ? { result: [{ assistantMessageID: 'msg_A1', created: 'today' }] }
          : payload.operation === 'timeline' ? { result: { transcript_revision: revision, source_map: [{ host_message_id: 'host-u1', native_message_id: 'msgN' }] } }
          : ['fork','fork-reconcile'].includes(payload.operation) ? { result: { status: 'settled', id: 'child-host', transcript_revision: revision } }
          : ['undo','redo','history-reconcile'].includes(payload.operation) ? { result: { status: 'settled', transcript_revision: ++revision, restore_draft: restoreReceipt, history_operation: { status: 'settled', operation: payload.operation, outcome: 'confirmed', file_snapshot_available: false } } }
          : { result: true };
      } else data = url.includes('/invoke') ? { message: 'Pinned skill request' } : url.includes('slash-catalog') ? { skills } : url.includes('mimo-state') ? state : url === '/api/memory' ? { memory: [] } : {};
      return { ok: !failure, json: async () => data };
    } });
  const stub = { addMessage() {}, hideWelcomeScreen() {}, getCurrentSessionId: () => sid, selectSession: async (id, options) => { refreshed.push(id); refreshOptions.push(options); }, getSessions: () => [], getWorkspace: () => '/workspace', esc: text => String(text), scrollHistory() {},
    showToast: text => toasts.push(text), copyMessageText: message => message.visibleText || '', copyToClipboard: text => copied.push(text), open: (...args) => settingsCalls.push(args), openSettings: (...args) => settingsCalls.push(args), getCommandCatalogEntries: () => [{ identity: 'setting:compaction.max_context', token: 'settings ai compaction.max_context', destination: { panel: 'ai', field: 'compaction.max_context' }, help: 'Context threshold' }] };
  const modules = new Map();
  const load = async name => {
    if (modules.has(name)) return modules.get(name);
    let module;
    if (['slashCommands.js', 'slashAutocomplete.js', 'commandCatalog.js', 'nativeSurfaceCatalog.js'].includes(name)) module = new vm.SourceTextModule(await fs.readFile(new URL(`../../static/js/${name}`, import.meta.url), 'utf8'), { context, identifier: name });
    else module = new vm.SyntheticModule(['default','EVAL_PROMPTS'], function() { this.setExport('default', stub); this.setExport('EVAL_PROMPTS', []); }, { context, identifier: name });
    modules.set(name,module);
    if (module instanceof vm.SourceTextModule) await module.link(spec => load(spec.split('/').at(-1).split('?')[0]));
    return module;
  };
  const commands = await load('slashCommands.js'); await commands.evaluate();
  const autocomplete = await load('slashAutocomplete.js'); await autocomplete.evaluate();
  const replies = () => history.children.map(e => e.children.find(c => c.className === 'body')?.innerHTML || '').join('\n');
  return { commands: commands.namespace, autocomplete: autocomplete.namespace, state, opened, refreshOptions, setRestoreReceipt(value) { restoreReceipt = value; }, setSkills(value) { skills = value; }, setSid(value) { sid = value; }, setOperationError(value, target = '') { operationError = value; operationErrorTarget = target; }, textarea, doc, listeners, calls, settingsCalls, selected, copied, toasts, refreshed, form: nodes['chat-form'], runTimers() { while (timers.length) timers.shift()(); }, holdFetch(pattern) { let enter, release; const entered = new Promise(resolve => { enter = resolve; }); const paused = new Promise(resolve => { release = resolve; }); fetchGate = { pattern, enter, paused }; return { entered, release }; }, setTranslator(translate) { context.window.openClankI18n = { t: translate }; }, setTranscript(messages) { history.querySelectorAll = () => messages; }, replies };
}

test('resolved identity reserves modes, canonical names, aliases and qualified collisions', () => {
  const catalog = buildCommandCatalog({ commands: { workspace: { alias: ['ws'], handler() {} }, search: { alias: ['websearch'], handler() {} }, chats: { subs: { info: { handler() {} } } } },
    legacyAliases: { memories: { parent: 'chats', sub: 'info' } }, skills: [{ name: 'ws' }, { name: 'same' }], nativeState: { available: true, commands: [{ name: 'same' }, { name: 'chat' }] } });
  for (const [input, identity] of [['/chat','mode:chat'], ['/ws','host:workspace'], ['/skill:ws','skill:ws'], ['/same','skill:same'], ['/mimo:same','native:native:same'], ['/memories','host:chats:info']]) assert.equal(resolveCatalogCommand(catalog,input).entry.identity, identity);
  const rows = filterCommandCatalog(catalog,'same').entries.filter(entry => entry.name === 'same');
  assert.equal(rows.length,2); assert.equal(rows[0].token,'/same'); assert.equal(rows[1].token,'/mimo:same');
});

test('shipped dispatcher handles advertised collisions, precise skill args and deliberate fallback', async () => {
  const app = await shipped();
  assert.equal(await app.commands.handleSlashCommand('/chat'),true); assert.deepEqual(app.selected,['chat']);
  assert.equal(await app.commands.handleSlashCommand('/ws'),true); assert.match(app.replies(),/Workspace/);
  assert.equal(await app.commands.handleSlashCommand('/memories'),true); assert.ok(app.calls.some(c => c.url === '/api/memory'));
  assert.equal(await app.commands.handleSlashCommand('/collide one  two\nthree'),true);
  const invocation = app.calls.find(c => c.url.includes('/collide/invoke'));
  assert.equal(JSON.parse(invocation.options.body).request,'one  two\nthree');
  assert.equal(await app.commands.handleSlashCommand('/mimo:collide one  two\nthree'),false);
  assert.equal(await app.commands.handleSlashCommand('/native-only'),false);
  assert.equal(await app.commands.handleSlashCommand('/unknown-command'),true); assert.match(app.replies(),/Unknown command/);
  assert.equal(await app.commands.handleSlashCommand('/literal /unknown-command'),false);
  app.setSkills([]); assert.equal(await app.commands.handleSlashCommand('/collide'),false);
  assert.equal(await app.commands.handleSlashCommand('/skill:collide'),true); // Revoked skill cannot fall through to native.
});

test('shipped help paginates the complete catalog and reports native availability', async () => {
  const app = await shipped();
  await app.commands.handleSlashCommand('/commands skill- --page 2');
  assert.match(app.replies(),/65 matches/); assert.match(app.replies(),/page 2\/2/); assert.match(app.replies(),/skill-64/);
  app.state.available = false;
  await app.commands.handleSlashCommand('/mimo:native-only');
  // State object cached by reference in fixture; availability must be checked again at dispatch.
  assert.match(app.replies(),/unavailable/);
});

test('mounted shipped autocomplete uses resolved winners, keyboard insertion and refresh', async () => {
  const app = await shipped();
  app.autocomplete.initSlashAutocomplete(app.textarea);
  await new Promise(resolve => setImmediate(resolve));
  app.textarea.value = '/ws'; app.textarea.listeners.get('input')();
  const popup = app.doc.getElementById('slash-autocomplete');
  assert.match(popup.innerHTML,/Set the folder/); assert.doesNotMatch(popup.innerHTML,/Skill collision/);
  app.textarea.value = '/native-o'; app.textarea.listeners.get('input')();
  let prevented = false;
  app.textarea.listeners.get('keydown')({ key: 'Tab', preventDefault() { prevented = true; } });
  assert.equal(prevented,true); assert.equal(app.textarea.value,'/native-only ');
  app.state.commands = [{ name: 'fresh-command', description: 'New MCP prompt' }];
  for (const fn of app.listeners.get('odysseus:mcp-changed')) await fn();
  app.textarea.value = '/fresh'; app.textarea.listeners.get('input')();
  assert.match(popup.innerHTML,/fresh-command/); assert.match(popup.innerHTML,/New MCP prompt/);
});

 test('exact Settings field destination and offline native defaults remain truthful', async () => {
  const app = await shipped();
  assert.equal(await app.commands.handleSlashCommand('/settings ai compaction.max_context'), true);
  assert.equal(app.settingsCalls[0][0], 'ai');
  assert.equal(app.settingsCalls[0][1].field, 'compaction.max_context');
  const offline = buildCommandCatalog({ nativeState: { available: false, reason: 'No active session' } });
  assert.equal(resolveCatalogCommand(offline, '/goal').entry.availability, 'unavailable');
  assert.match(resolveCatalogCommand(offline, '/goal').entry.help, /empty cancels/);
  assert.equal(resolveCatalogCommand(offline, '/exit').entry.executor, 'unavailable');
  assert.match(resolveCatalogCommand(offline, '/exit').entry.reason, /terminal/i);
});

test('shipped session operations call exact owner endpoint and handle busy rejection', async () => {
  const app = await shipped();
  assert.equal(await app.commands.handleSlashCommand('/btw first  line\nsecond'), true);
  let call = app.calls.find(call => call.url.includes('mimo-operation'));
  assert.equal(JSON.parse(call.options.body).question, 'first  line\nsecond');
  assert.match(app.replies(), /Side answer/);
  await app.commands.handleSlashCommand('/recover');
  assert.match(app.replies(), /recover msg_A1/);
  await app.commands.handleSlashCommand('/recover msg_A1');
  const resume = app.calls.filter(call => call.url.includes('mimo-operation')).at(-1);
  assert.equal(JSON.parse(resume.options.body).assistantMessageID, 'msg_A1');
  app.setOperationError('Session is busy');
  await app.commands.handleSlashCommand('/btw question');
  assert.match(app.replies(), /Session is busy/);
  app.setSid('');
  assert.equal(await app.commands.handleSlashCommand('/recover'), true);
  assert.match(app.replies(), /No active session/);
  assert.equal(await app.commands.handleSlashCommand('/mimo:goal'), true);
});

test('group aliases resolve to leaf identity and productive hidden aliases remain discoverable', () => {
  const catalog = buildCommandCatalog({ commands: { chats: { alias: ['s'], subs: { info: { alias: ['stat'], help: 'Chat details', handler() {} } } }, toggle: { hidden: true, subs: { web: { help: 'Toggle web', handler() {} } } } }, legacyAliases: { web: { parent: 'toggle', sub: 'web' } } });
  assert.equal(resolveCatalogCommand(catalog, '/s stat --help').entry.identity, 'host:chats:info');
  assert.equal(resolveCatalogCommand(catalog, '/s stat --help').arguments, '--help');
  assert.equal(filterCommandCatalog(catalog, '/web').entries[0].identity, 'host:toggle:web');
  assert.equal(filterCommandCatalog(catalog, '/web').entries[0].token, '/web');
});

test('shipped transcript copy uses rendered display owner and session list is host-owned', async () => {
  const app = await shipped();
  app.setTranscript([
    { visibleText: 'Visible question', querySelector: () => ({ textContent: 'User' }) },
    { visibleText: 'Displayed answer', querySelector: () => ({ textContent: 'Assistant' }) },
  ]);
  assert.equal(await app.commands.handleSlashCommand('/copy'), true);
  assert.equal(app.copied[0], 'User:\nVisible question\n\nAssistant:\nDisplayed answer');
  assert.equal(await app.commands.handleSlashCommand('/sessions'), true);
  assert.match(app.replies(), /No active sessions/);
  assert.equal(await app.commands.handleSlashCommand('/resume'), true);
  assert.equal(app.calls.some(call => call.url.includes('mimo-operation')), false);
});

test('coverage inventory classifies every current TUI registration; new source rows fail review', async () => {
  const registered = new Set();
  const root = '../../packages/mimo-code/packages/opencode/src/cli/cmd/tui/';
  for (const file of ['app.tsx','routes/session/index.tsx','component/prompt/index.tsx','feature-plugins/system/plugins.tsx','feature-plugins/home/tips.tsx']) {
    const source = await fs.readFile(new URL(root + file, import.meta.url), 'utf8');
    for (const match of source.matchAll(/value:\s*"([a-z][a-z_.]*\.[a-z_.]+)"/g)) registered.add(match[1]);
  }
  assert.deepEqual([...registered].sort(), NATIVE_SURFACE_ACTIONS.map(row => row.id).sort(), 'New registrations must receive an explicit disposition before parity is claimed');
});

test('cold canonical native route permits explicit defaults and operations without help starting it', async () => {
  const cold = buildCommandCatalog({ nativeState: { available: false, native_route_selected: true, reason: 'SESSION_NOT_READY' } });
  assert.equal(resolveCatalogCommand(cold, '/goal').entry.availability, 'requires-start');
  assert.equal(resolveCatalogCommand(cold, '/btw question').entry.availability, 'requires-start');
  assert.equal(resolveCatalogCommand(cold, '/loops').entry.availability, 'unavailable');
  assert.equal(resolveCatalogCommand(cold, '/mimo:unknown').entry, null);
  const app = await shipped();
  app.state.available = false; app.state.native_route_selected = true; app.state.commands = [];
  await app.commands.handleSlashCommand('/commands goal');
  assert.equal(app.calls.some(call => call.url.includes('refresh=true') || call.url.includes('mimo-operation')), false);
  assert.equal(await app.commands.handleSlashCommand('/mimo:goal status'), false);
  assert.equal(await app.commands.handleSlashCommand('/btw question'), true);
});

test('localized authored discovery keeps tokens and native user text stable through mounted UI and dispatch', async () => {
  const translations = { 'Set the folder the agent works in': '选择工作文件夹', 'Read-only answers and safe context': '只读聊天模式', 'Interaction modes': '交互模式' };
  const translate = value => translations[value] || value;
  const catalog = buildCommandCatalog({ translate, commands: { workspace: { help: 'Set the folder the agent works in', handler() {} } }, nativeState: { available: true, commands: [{ name: 'custom', description: 'Set the folder the agent works in' }] } });
  assert.equal(filterCommandCatalog(catalog, '工作文件夹').entries[0].token, '/workspace');
  assert.equal(resolveCatalogCommand(catalog, '/custom').entry.help, 'Set the folder the agent works in');
  assert.equal(resolveCatalogCommand(catalog, '/chat').entry.help, '只读聊天模式');
  const app = await shipped(); app.setTranslator(translate);
  await app.commands.handleSlashCommand('/commands 工作文件夹');
  assert.match(app.replies(), /选择工作文件夹/); assert.match(app.replies(), /\/workspace/);
  app.autocomplete.initSlashAutocomplete(app.textarea);
  await new Promise(resolve => setImmediate(resolve));
  app.textarea.value = '/工作文件夹'; app.textarea.listeners.get('input')();
  assert.match(app.doc.getElementById('slash-autocomplete').innerHTML, /\/workspace/);
  assert.equal(await app.commands.handleSlashCommand('/workspace'), true);
  assert.match(app.replies(), /Workspace/);
});

test('static product dispositions are plain and source counterparts keep qualified identities', () => {
  const catalog = buildCommandCatalog({ commands: { copy: { help: 'Copy loaded visible transcript', handler() {} }, doc: { help: 'Toggle document editor', handler() {} } }, nativeState: { available: true } });
  assert.equal(resolveCatalogCommand(catalog, '/tui:session.copy').entry.identity, 'host:copy');
  const docs = resolveCatalogCommand(catalog, '/tui:docs.open').entry;
  assert.equal(docs.executor, 'unavailable');
  assert.match(docs.reason, /native terminal/);
  assert.doesNotMatch(JSON.stringify(NATIVE_SURFACE_ACTIONS), /qualification|reconciliation|owner-backed|app:[0-9]|session:[0-9]/);
});

test('shipped dispatcher fences selected-chat races and delayed prompts preserve new drafts', async () => {
  const app = await shipped();
  let gate = app.holdFetch('slash-catalog');
  let pending = app.commands.handleSlashCommand('/native-only');
  await gate.entered; app.setSid('session-b'); gate.release();
  assert.equal(await pending, true);
  assert.match(app.toasts.at(-1), /Chat changed/);
  gate = app.holdFetch('mimo-operation');
  pending = app.commands.handleSlashCommand('/btw question');
  await gate.entered; app.setSid('session-c'); gate.release();
  await pending;
  assert.doesNotMatch(app.replies(), /Side answer/);
  gate = app.holdFetch('/collide/invoke');
  pending = app.commands.handleSlashCommand('/collide question');
  await gate.entered; app.setSid('session-d'); gate.release();
  await pending;
  assert.match(app.toasts.at(-1), /skill prompt was not submitted/);
  let submitted = 0; app.form.addEventListener('submit', () => submitted++);
  app.textarea.value = '/chat question';
  await app.commands.handleSlashCommand('/chat question');
  app.textarea.value = 'New draft'; app.runTimers();
  assert.equal(submitted, 0); assert.equal(app.textarea.value, 'New draft');
});

test('native display actions reach verified browser Settings destinations without native toggles', async () => {
  const app = await shipped();
  for (const [token, field] of [
    ['/timestamps','show-timestamps'], ['/thinking','show-thinking'],
    ['/tui:session.toggle.actions','show-tool-details'],
    ['/tui:session.toggle.generic_tool_output','show-tool-output'],
    ['/tui:app.toggle.diffwrap','wrap-diffs'],
  ]) {
    assert.equal(await app.commands.handleSlashCommand(token), true);
    assert.equal(app.settingsCalls.at(-1)[0], 'appearance');
    assert.equal(app.settingsCalls.at(-1)[1].field, field);
  }
  assert.equal(app.calls.some(call => call.url.includes('mimo-operation')), false);
});

test('shipped native history commands use fresh host revision and settled owner acknowledgments', async () => {
  const app = await shipped();
  await app.commands.handleSlashCommand('/timeline');
  assert.match(app.replies(), /host-u1/); assert.doesNotMatch(app.replies(), /msgN/);
  await app.commands.handleSlashCommand('/undo host-u1');
  let mutation = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body)).find(payload => payload.operation === 'undo');
  assert.deepEqual(mutation, { operation: 'undo', expected_transcript_revision: 7, host_message_id: 'host-u1' });
  assert.match(app.replies(), /No native file snapshot/);
  await app.commands.handleSlashCommand('/redo');
  mutation = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body)).find(payload => payload.operation === 'redo');
  assert.equal(mutation.expected_transcript_revision, 8);
  assert.deepEqual(app.refreshed, ['session-a','session-a']);
});

test('history stale and pending errors never auto-retry or reconcile', async () => {
  const app = await shipped();
  app.setOperationError({ code: 'HISTORY_REVISION_CONFLICT', message: 'History changed' }, 'undo');
  assert.equal(await app.commands.handleSlashCommand('/undo'), true);
  assert.match(app.replies(), /Reload \/timeline/); assert.equal(app.refreshed.length, 0);
  app.setOperationError({ code: 'HISTORY_RECONCILIATION_REQUIRED', message: 'Pending history change' }, 'redo');
  await app.commands.handleSlashCommand('/redo');
  assert.match(app.replies(), /Use \/history-reconcile/);
  let mutations = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body));
  assert.equal(mutations.filter(payload => payload.operation === 'history-reconcile').length, 0);
  app.setOperationError(''); await app.commands.handleSlashCommand('/history-reconcile');
  mutations = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body));
  assert.equal(mutations.filter(payload => payload.operation === 'history-reconcile').length, 1);
});


test('shipped export sends supported formats to the canonical owner', async () => {
  const app = await shipped();
  for (const format of ['json','txt','html','md']) await app.commands.handleSlashCommand('/export ' + format);
  assert.deepEqual(app.opened.map(([url]) => new URL(url,'http://host').searchParams.get('fmt')), ['json','txt','html','md']);
  assert.ok(app.opened.every(([url]) => url.startsWith('/api/session/session-a/export?')));
});

test('history dispatch atomically hands off receipt and preserves a newer draft', async () => {
  const app = await shipped();
  const receipt = {available:true,raw_content:'{"saved":"literal"}',content_kind:'text',attachment_refs:[]};
  app.setRestoreReceipt(receipt); app.textarea.value = '/undo';
  await app.commands.handleSlashCommand('/undo');
  assert.equal(app.textarea.value,receipt.raw_content); assert.equal(app.refreshOptions[0].preserveComposer,true);
  app.textarea.value = '/undo';
  const gate = app.holdFetch('mimo-operation'); const pending = app.commands.handleSlashCommand('/undo');
  await gate.entered; app.textarea.value = 'new typed draft'; gate.release(); await pending;
  assert.equal(app.textarea.value,'new typed draft'); assert.match(app.replies(),/newer draft was preserved/);
  app.setRestoreReceipt({...receipt,available:false}); await app.commands.handleSlashCommand('/undo');
  assert.equal(app.textarea.value,'new typed draft');
});

test('native fork navigates only settled host candidate; explicit reconcile uses fresh source revision', async () => {
  const app = await shipped();
  await app.commands.handleSlashCommand('/tui:session.fork host-u1');
  let requests = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body));
  assert.deepEqual(requests.find(request => request.operation === 'fork'), {operation:'fork',expected_transcript_revision:7,host_message_id:'host-u1'});
  assert.deepEqual(app.refreshed,['child-host']);
  app.setOperationError({code:'FORK_RECONCILIATION_REQUIRED',message:'Fork pending',candidate_id:'pending-child'},'fork');
  await app.commands.handleSlashCommand('/native-fork'); assert.deepEqual(app.refreshed,['child-host']);
  app.setOperationError(''); await app.commands.handleSlashCommand('/fork-reconcile pending-child');
  requests = app.calls.filter(call => call.url.includes('mimo-operation')).map(call => JSON.parse(call.options.body));
  assert.deepEqual(requests.find(request => request.operation === 'fork-reconcile'), {operation:'fork-reconcile',expected_transcript_revision:7,child_session_id:'pending-child'});
});

test('named profile helper lists eligibility and patches profile without changing public modes', async () => {
  const app = await shipped();
  app.state.modes.availableModes.push({ id: 'plan', name: 'Reserved Plan' }, { id: 'chat', name: 'Reserved Chat' }, { id: 'build', name: 'Build' });
  await app.commands.handleSlashCommand('/agent-profile'); assert.match(app.replies(),/reviewer/);
  assert.match(app.replies(),/build/); assert.doesNotMatch(app.replies(),/Reserved Plan|Reserved Chat/);
  await app.commands.handleSlashCommand('/agent-profile plan');
  await app.commands.handleSlashCommand('/agent-profile chat');
  assert.equal(app.calls.filter(call=>call.url.includes('mimo-config')).length,0);
  await app.commands.handleSlashCommand('/agent-profile reviewer');
  const patch = app.calls.find(call => call.url.includes('mimo-config'));
  assert.equal(patch.options.method,'PATCH'); assert.deepEqual(JSON.parse(patch.options.body),{config_id:'agent_profile',value:'reviewer'});
  assert.deepEqual(app.selected,[]); assert.match(app.replies(),/next Agent turn/);
  await app.commands.handleSlashCommand('/agent-profile revoked'); assert.equal(app.calls.filter(call=>call.url.includes('mimo-config')).length,1);
  await app.commands.handleSlashCommand('/agent-profile build');
  assert.equal(JSON.parse(app.calls.filter(call=>call.url.includes('mimo-config')).at(-1).options.body).value,'build');
  await app.commands.handleSlashCommand('/agent'); assert.deepEqual(app.selected,['agent']);
});

test('provider action discovery navigates existing credential owner and preserves explicit connection choice', async () => {
  const app = await shipped();
  await app.commands.handleSlashCommand('/tui:provider.login');
  await app.commands.handleSlashCommand('/tui:provider.connect');
  await app.commands.handleSlashCommand('/tui:provider.logout');
  assert.deepEqual(app.settingsCalls.map(([panel, options]) => [panel, options.field]), [['services','mimo-provider-directory'],['services','mimo-provider-directory'],['added-models','mimo-connected-provider-list']]);
  assert.equal(app.calls.some(call => call.options?.method === 'DELETE'),false);
  await app.commands.handleSlashCommand('/tui:agent.list'); assert.match(app.replies(),/reviewer/);
});
