import assert from 'node:assert/strict';
import { copyTemplateProperties, createNotesFeature, expandTemplate, expandTemplateVariables, formatTemplateDate, mergeTemplateProperties, rebaseTemplateLinks } from '../../static/js/copal/notesFeature.js';
import { normalizeNotesSettings } from '../../static/js/copal/notesWorkspace.js';
import { createBufferRegistry } from '../../static/js/copal/documentBuffers.js';
import { serializeEditorSource } from '../../static/js/copal/codemirror.js';

const pending = [];

const fixed = new Date('2026-01-02T03:04:05.000Z');
assert.equal(formatTemplateDate('YYYY/MM/DD HH:mm:ss', fixed, 'UTC'), '2026/01/02 03:04:05');
const expanded = expandTemplate('## {{title}}\n{{date}} {{time}} {{date:YYYY/MM}} {{missing}}', { title:'Daily', now:fixed, timeZone:'UTC' });
assert.equal(expanded.text, '## Daily\n2026-01-02 03:04 2026/01 {{missing}}');
assert.match(expanded.diagnostics.join(' '), /Unsupported template variable/);
assert.equal(expandTemplateVariables('{{title}} {{datetime}}', { title:'Entry', now:fixed, timeZone:'UTC' }), 'Entry 2026-01-02T03:04:05');
const unsafe = expandTemplate('{{date:YYYY/INVALID}} <% await doSomething() %>', { now:fixed, timeZone:'UTC' });
assert.equal(unsafe.text, '{{date:YYYY/INVALID}} <% await doSomething() %>');
assert.match(unsafe.diagnostics.join(' '), /Unsupported template date format/);
assert.match(unsafe.diagnostics.join(' '), /Executable template expressions/);
// Notes' compatibility exports must be byte/result compatible with the S04
// model used by Insert Template, including long Moment tokens and timezone.
assert.equal(formatTemplateDate('MMMM MMM M D DD HH H mm m ss s', fixed, 'UTC'), 'January Jan 1 2 02 03 3 04 4 05 5');
const paritySource = '日本語\r\n{{title}}\r\n{{date:MMMM}} {{time:HH:mm}}\r\n{{date:YYYY}} {{unknown}} <% inert() %>';
const canonicalParity = (await import('../../static/js/copal/templateModel.js')).expandTemplate(paritySource, { title:'題名', now:fixed, timeZone:'UTC' });
assert.deepEqual(expandTemplate(paritySource, { title:'題名', now:fixed, timeZone:'UTC' }), canonicalParity);
const dstBefore = new Date('2026-03-08T09:00:00.000Z');
const dstAfter = new Date('2026-03-08T10:00:00.000Z');
assert.equal(formatTemplateDate('YYYY-MM-DD HH:mm', dstBefore, 'America/Los_Angeles'), '2026-03-08 01:00');
assert.equal(formatTemplateDate('YYYY-MM-DD HH:mm', dstAfter, 'America/Los_Angeles'), '2026-03-08 03:00');
assert.deepEqual(copyTemplateProperties({ type:'template', sourceDocumentId:'source-1', template:'template-1', owner:'template', tags:['starter'] }), { owner:'template', tags:['starter'] });
const mixedLineEndings = 'first\r\nsecond\nthird\r\nfourth';
assert.equal(serializeEditorSource(mixedLineEndings.replace(/\r\n?|\n/g, '\n'), mixedLineEndings), mixedLineEndings, 'unchanged source retains mixed line-ending bytes');
assert.equal(serializeEditorSource('first\nsecond\nthird\nfourth\nfifth', mixedLineEndings), 'first\r\nsecond\r\nthird\r\nfourth\r\nfifth', 'edited source follows the original dominant separator');
assert.deepEqual(mergeTemplateProperties({ owner:'local', keep:'yes' }, { owner:'template', color:'blue', type:'template' }), {
  properties:{ owner:'local', keep:'yes', color:'blue' }, collisions:[{ key:'owner', destination:'local', template:'template' }],
});
assert.equal(rebaseTemplateLinks('![asset](../assets/one.png) [wiki](../Wiki/Entry.md#part) ![[../assets/two.png]]', 'Templates/Daily.md', 'Archive/Notes/Today.md'), '![asset](../../assets/one.png) [wiki](../../Wiki/Entry.md#part) ![[../../assets/two.png]]');
assert.deepEqual(normalizeNotesSettings({ templateFolder:'\\Templates\\', dailyTemplateId:'daily-1' }), {
  previewLayout:'inline', lineNumbers:false, readableLineWidth:true, ribbon:false, completedVisibility:'show', templateFolder:'Templates', dailyTemplateId:'daily-1',
});
const state = {
  accountId:'account-a', workspace:'workspace-a', storageNamespace:'user:mutable-name', contextEpoch:1,
  docs:[], saveTimers:new Map(), noteEditors:new Set(), windows:new Map(),
};
const notesContext = { noteDrafts:new Map(), noteBuffers:new Map(), noteSaveRuns:new Map(), noteLeafViews:new Map() };
state.windows.set('notes', notesContext);
const saved = [];
const feature = createNotesFeature({
  state,
  saveDocument:async (doc, content, rerender, view, options) => {
    saved.push({ doc, content, options });
    await new Promise((resolve) => pending.push(resolve));
    return { outcome:'applied', revision:{ kind:'copalHead', value:`head-${saved.length}` }, doc:{ ...doc, head:`head-${saved.length}`, text:content } };
  },
});
const resource = {
  key:{ accountId:'account-a', workspaceId:'workspace-a', provider:'copal', resourceId:'note-origin' },
  revision:{ kind:'copalHead', value:'head-0' },
  locator:{ displayName:'Note', locationLabel:'Note' }, representation:'nativeNote',
  capabilities:{ read:true, edit:true },
};
const doc = { id:'doc-1', name:'Note', kind:'note', head:'head-0', text:'one', properties:{state:'one'}, relations:[], resource };
state.docs.push(doc);
notesContext.noteAcceptedEnvelopes = new Map([['doc-1', { text:'one', properties:{ state:'initial' }, relations:[] }]]);

feature.queueSave({ ...doc, properties:{ state:'one' } }, 'one');
assert.equal(feature.getDraftSnapshot('doc-1').localRevision, 1);
assert.equal(feature.getDraftSnapshot('doc-1').scope.accountId, 'account-a');
assert.equal(feature.getDraftSnapshot('doc-1').scope.workspace, 'workspace-a');
// A Files rename rotates the opaque locator and asynchronously refreshes the
// resource. That refresh may observe a remote head the dirty draft has never
// seen; retaining the old CAS base is required until an explicit comparison
// or write acknowledgement proves the handoff safe.
assert.equal(feature.rotateResourceRef('doc-1', 'rr1.note-renamed', {
  ...resource,
  revision:{ kind:'copalHead', value:'head-unseen-remote' },
  locator:{ displayName:'Renamed Note', locationLabel:'Renamed Note', opaqueRef:'rr1.note-renamed' },
}), true);
const rotatedDraft = feature.getDraftSnapshot('doc-1');
assert.equal(rotatedDraft.expectedRevision.value, 'head-0', 'dirty ResourceRef rotation preserves the original CAS base');
assert.equal(state.docs[0].resource.locator.opaqueRef, 'rr1.note-renamed', 'same leaf follows the rotated locator');
assert.equal(feature.acceptSavedDocument('doc-1', 'remote', { expectedLocalRevision:0 }), false, 'stale conflict decision cannot discard a newer draft');
const firstFlush = feature.flushAll();
assert.equal(saved.length, 1);
feature.queueSave({ ...doc, properties:{state:'two'} }, 'one');
pending.shift()();
await new Promise((resolve) => setTimeout(resolve, 0));
assert.equal(saved.length, 2);
assert.deepEqual(saved[0].options.snapshot.envelope.properties, { state:'one' });
assert.deepEqual(saved[1].options.snapshot.envelope.properties, { state:'two' });
pending.shift()();
await firstFlush;
assert.equal(notesContext.noteDrafts.size, 0);
const acknowledged = feature.getAuthoritativeSnapshot('doc-1');
assert.equal(acknowledged.expectedRevision.value, 'head-2', 'clean buffers expose their latest acknowledged CAS head');
assert.equal(acknowledged.envelope.properties.state, 'two', 'authoritative snapshot keeps the acknowledged envelope');
assert.equal(feature.getDraftSnapshot('doc-1'), null, 'clean buffers remain hidden from the dirty-draft accessor');

feature.queueSave({ ...doc, properties:{state:'three'} }, 'one');
assert.equal(notesContext.noteDrafts.get('doc-1').localRevision, 3);
const thirdFlush = feature.flushAll();
pending.shift()();
await thirdFlush;

feature.queueSave({ ...doc, properties:{state:'old-owner'} }, 'one');
const staleFlush = feature.flushAll();
assert.equal(saved.length, 4);
state.accountId = 'account-b'; state.contextEpoch = 2;
feature.queueSave({ ...doc, properties:{state:'new-owner'} }, 'one');
pending.shift()();
await staleFlush;
assert.equal(notesContext.noteDrafts.get('doc-1').scope.includes('account-b'), true);
assert.equal(notesContext.noteDrafts.get('doc-1').envelope.properties.state, 'new-owner');

// A clean sibling can subscribe before its first edit creates a buffer. The
// feature-local creation seam must deliver that exact shared instance once,
// and disposal must stop later notifications.
const lateDoc = { ...doc, id:'doc-late-buffer', resource:{ ...resource, key:{ ...resource.key, resourceId:'late-buffer' } }, text:'late' };
state.docs.push(lateDoc);
let lateBufferNotifications = 0;
const stopLateBuffer = feature.subscribeToDocumentBuffer('doc-late-buffer', (buffer) => {
  lateBufferNotifications += 1;
  assert.equal(buffer, notesContext.noteBuffers.get('doc-late-buffer'));
});
feature.queueSave(lateDoc, 'late draft');
assert.equal(lateBufferNotifications, 1, 'first buffer creation reaches a clean sibling');
stopLateBuffer();
feature.queueSave(lateDoc, 'later draft');
assert.equal(lateBufferNotifications, 1, 'disposed sibling no longer receives buffer notifications');

let sheetInvalidations = 0;
let sheetRefreshes = 0;
notesContext.noteLeafViews.set('sheet-leaf', {
  docId:'doc-1',
  sheetController:{
    getState:() => ({ rows:[{ documentId:'source-1' }] }),
    invalidateQueries:() => { sheetInvalidations += 1; },
    refresh:async (reason) => { sheetRefreshes += reason === 'watch' ? 1 : 0; return { outcome:'applied' }; },
  },
});
assert.equal(await feature.invalidateBaseLeaves('doc-1'), 1, 'watch refresh reaches the affected mounted Base');
assert.equal(sheetInvalidations, 1);
assert.equal(sheetRefreshes, 1);
notesContext.noteLeafViews.delete('sheet-leaf');

console.log('copal notes buffer queue tests passed');

const recoveryStore = new Map();
const recoveryStorage = { setItem:(key, value) => recoveryStore.set(key, value), getItem:(key) => recoveryStore.get(key) || null, removeItem:(key) => recoveryStore.delete(key) };
const recoveryRegistry = createBufferRegistry({ storage:recoveryStorage });
const recoveryDoc = { ...doc, id:'doc-recovery', resource:{ ...resource, key:{ ...resource.key, resourceId:'recovery' } }, properties:{ state:'accepted' } };
const recoverySeed = recoveryRegistry.acquire(recoveryDoc.resource, { text:'accepted', properties:{ state:'accepted' }, relations:[] }, { actorId:'account-a:workspace-a', epoch:1 });
recoverySeed.apply({ text:'recovered', properties:{ state:'draft' }, relations:[] });
recoveryRegistry.persistDraft(recoverySeed);
const recoveryState = { ...state, accountId:'account-a', contextEpoch:1, docs:[recoveryDoc], windows:new Map(), saveTimers:new Map(), noteEditors:new Set() };
recoveryState.windows.set('notes', { noteDrafts:new Map(), noteBuffers:new Map(), noteSaveRuns:new Map(), noteLeafViews:new Map() });
const recoveryFeature = createNotesFeature({ state:recoveryState, resourceBufferRegistry:recoveryRegistry, saveDocument:async () => ({ outcome:'applied', revision:{ kind:'copalHead', value:'head-recovered' } }) });
recoveryFeature.getSettings();
assert.equal(recoveryState.windows.get('notes').noteDrafts.get('doc-recovery').value, 'recovered', 'workspace initialization restores persisted draft');
const persistedLayout = recoveryState.windows.get('notes').noteWorkspace;
const layoutState = { ...recoveryState, windows:new Map(), contextEpoch:3 };
layoutState.windows.set('notes', { noteDrafts:new Map(), noteBuffers:new Map(), noteSaveRuns:new Map(), noteLeafViews:new Map() });
const layoutFeature = createNotesFeature({ state:layoutState, resourceBufferRegistry:createBufferRegistry({ storage:recoveryStorage }), saveDocument:async () => ({ outcome:'applied', revision:{ kind:'copalHead', value:'head-layout' } }) });
layoutFeature.loadSaved(layoutState.windows.get('notes'), { ...persistedLayout, settings:{ lineNumbers:true, readableLineWidth:false } });
assert.equal(layoutFeature.getSettings().lineNumbers, true, 'first scope retains loaded layout settings');
const oldScopeBuffer = layoutState.windows.get('notes').noteBuffers.get('doc-recovery');
assert.equal(layoutFeature.suspendScope(), true);
assert.equal(layoutState.windows.get('notes').noteBuffers.size, 0, 'scope suspension detaches old buffers');
assert.equal(layoutState.windows.get('notes').noteDrafts.size, 0, 'scope suspension detaches old drafts after persistence');
layoutState.contextEpoch = 4;
layoutFeature.getSettings();
assert.notEqual(layoutState.windows.get('notes').noteBuffers.get('doc-recovery'), oldScopeBuffer, 'new scope cannot reacquire old in-memory buffer');

// The Base-facing queue seam keeps the complete receipt for new callers while
// existing flushAll/saveDraft callers retain their boolean contract.
const receiptState = {
  ...state, accountId:'account-receipts', contextEpoch:1, docs:[doc], windows:new Map(), saveTimers:new Map(), noteEditors:new Set(), noteBuffers:new Map(), noteDrafts:new Map(), noteSaveRuns:new Map(), noteLeafViews:new Map(), noteAcceptedEnvelopes:new Map([['doc-1', { text:'one', properties:{}, relations:[] }]]),
};
receiptState.windows.set('notes', receiptState);
const receiptOptions = [];
const receiptFeature = createNotesFeature({
  state:receiptState,
  saveDocument:async (savedDoc, content, rerender, view, options) => { receiptOptions.push(options); return { outcome:'applied', revision:{ kind:'copalHead', value:'receipt-head' }, projections:{ tasks:{ status:'pending' } }, doc:{ ...doc, head:'receipt-head', text:content } }; },
});
const receipt = await receiptFeature.queueDocumentSave(doc, 'receipt text');
assert.equal(receipt.outcome, 'applied');
assert.equal(receipt.revision.value, 'receipt-head');
assert.deepEqual(receipt.projections.tasks, { status:'pending' });
assert.equal((await receiptFeature.retryDocumentSave('missing-doc')).outcome, 'failed', 'a missing draft cannot fabricate an applied replay receipt');
receiptFeature.queueSave(doc, 'legacy text');
assert.deepEqual(await receiptFeature.flushAll(), [true], 'legacy queue callers still receive booleans');
receiptFeature.queueSave(doc, 'sheet text', { sheet:true });
assert.equal((await receiptFeature.flushDocument(doc.id, { returnReceipt:true })).outcome, 'applied');
assert.equal(receiptOptions.at(-1).sheet, true, 'sheet provenance reaches buffered saveDocument');
receiptFeature.queueSave(doc, 'ordinary text');
assert.equal((await receiptFeature.flushDocument(doc.id, { returnReceipt:true })).outcome, 'applied');
assert.equal(receiptOptions.at(-1).sheet, false, 'ordinary edits do not inherit sheet conflict suppression');
assert.equal((await receiptFeature.retryDocumentSave(doc.id)).outcome, 'failed', 'a clean buffer cannot be retried as an uncertain save');

// A stale reviewed snapshot must not replace a newer queued edit when the
// explicit rebase check rejects it.
receiptFeature.queueSave(doc, 'first reviewed draft');
const reviewed = receiptFeature.getDraftSnapshot(doc.id);
receiptFeature.queueSave(doc, 'newer queued draft');
assert.throws(() => receiptFeature.queueSave(doc, 'stale reviewed draft', { rebase:reviewed }), /reviewed version is no longer current/);
assert.equal(receiptFeature.getDraftSnapshot(doc.id).envelope.text, 'newer queued draft');
await receiptFeature.flushAll();

// Leaf cleanup must release the editor context adapter exactly once, even if
// destroy is repeated by both window close and scope teardown.
const cleanupState = { accountId:'cleanup-account', workspace:'cleanup-workspace', storageNamespace:'cleanup', contextEpoch:1, view:'notes', docs:[], saveTimers:new Map(), noteEditors:new Set(), windows:new Map() };
const cleanupContext = { noteDrafts:new Map(), noteBuffers:new Map(), noteSaveRuns:new Map(), noteLeafViews:new Map(), noteShellCache:{ key:'stale', shell:{} } };
cleanupState.windows.set('notes', cleanupContext);
let contextMenuDisposals = 0; let editorDestructions = 0;
cleanupContext.noteLeafViews.set('leaf-1', { contextMenuDispose:() => { contextMenuDisposals += 1; }, editor:{ destroy:() => { editorDestructions += 1; } } });
const cleanupFeature = createNotesFeature({ state:cleanupState, saveDocument:async () => ({ outcome:'applied', revision:{ kind:'copalHead', value:'cleanup-head' } }) });
cleanupFeature.destroy(); cleanupFeature.destroy();
assert.equal(contextMenuDisposals, 1, 'context menu adapter is disposed once');
assert.equal(editorDestructions, 1, 'editor destruction remains idempotent');
assert.equal(cleanupContext.noteShellCache, null, 'destroy invalidates the stale shell cache');
