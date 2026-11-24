import assert from 'node:assert/strict';
import test from 'node:test';
import {
  activeSheetView,
  cellEditorKind,
  copySelection,
  createSheetState,
  clearSelection,
  isReadOnlyColumn,
  moveSelection,
  normalizeSheetDefinition,
  pastePreview,
  rowIdentity,
  selectCell,
  selectionBounds,
  sheetTitle,
  visibleColumnIndexes,
} from '../../static/js/copal/sheetModel.js';
import { createSheetController, sameDefinitionRevision } from '../../static/js/copal/sheetController.js';
import { parseTypedEditorValue } from '../../static/js/copal/sheetView.js';

const definition = normalizeSheetDefinition({
  extensions:{ title:'Projects/To Watch.base' },
  views:[{ id:'table', name:'Table', columns:[{ property:'file.name' }, { property:'file.tags' }, { property:'tags' }, { property:'status', label:'State', type:'text' }, { property:'priority', type:'number' }] }],
});
const rows = [
  { documentId:'doc-1', name:'Alpha', values:{ tags:['work'], status:'open', priority:1 } },
  { documentId:'doc-2', name:'Beta', values:{ tags:['home'], status:'done', priority:2 } },
];
let state = createSheetState({ resourceKey:{ provider:'copal', accountId:'acct', workspaceId:'ws', resourceId:'base-1' }, definition, rows, counts:{ matched:7, limited:5, truncated:true }, scope:{ accountId:'acct', workspace:'ws', epoch:3 } });
assert.equal(activeSheetView(state).columns[0].label, 'Name');

test('sheet columns canonicalize editable aliases and keep derived formula columns read-only', () => {
  const view = normalizeSheetDefinition({ views:[{ columns:[{ property:'note.status' }, { property:'properties.tags' }, { property:'formula.total' }, { property:'file.tags' }] }] }).views[0];
  assert.deepEqual(view.columns.map((column) => column.property), ['status', 'tags', 'formula.total', 'file.tags']);
  assert.equal(isReadOnlyColumn(view.columns[0]), false);
  assert.equal(isReadOnlyColumn(view.columns[1]), false);
  assert.equal(isReadOnlyColumn(view.columns[2]), true);
  assert.equal(isReadOnlyColumn(view.columns[3]), true);
});

test('typed cell editors distinguish valid values, malformed values, and explicit clears', () => {
  assert.deepEqual(parseTypedEditorValue('number', '42.5'), { ok:true, value:42.5 });
  assert.deepEqual(parseTypedEditorValue('number', ''), { ok:true, value:null });
  assert.equal(parseTypedEditorValue('number', 'not-a-number').ok, false);
  assert.deepEqual(parseTypedEditorValue('date', '2026-09-13'), { ok:true, value:'2026-09-13' });
  assert.equal(parseTypedEditorValue('date', '2026-02-31').ok, false);
  assert.equal(parseTypedEditorValue('date', 'tomorrow').ok, false);
  assert.deepEqual(parseTypedEditorValue('date', ''), { ok:true, value:null });
  assert.deepEqual(parseTypedEditorValue('datetime', '2026-09-13T10:30'), { ok:true, value:'2026-09-13T10:30' });
  assert.equal(parseTypedEditorValue('datetime', '2026-09-13 nope').ok, false);
});

test('view switches retain a leaf selection when its row and property remain visible', async () => {
  const viewDefinition = normalizeSheetDefinition({ views:[
    { id:'table', columns:[{ property:'status' }, { property:'score' }] },
    { id:'list', columns:[{ property:'status' }] },
  ] });
  const controller = createSheetController({
    definition:viewDefinition,
    viewId:'table',
    rows:[{ documentId:'view-row', values:{ status:'open', score:2 } }],
    query:async () => ({ rows:[{ documentId:'view-row', values:{ status:'open', score:2 } }], matchedCount:1 }),
  });
  controller.select(0, 0);
  await controller.setView('list');
  assert.deepEqual(controller.getState().selected, { anchor:{ rowKey:'view-row', columnKey:'status' }, focus:{ rowKey:'view-row', columnKey:'status' } });
  await controller.setView('table');
  assert.deepEqual(controller.getState().selected, { anchor:{ rowKey:'view-row', columnKey:'status' }, focus:{ rowKey:'view-row', columnKey:'status' } });
  controller.close();
});

test('definition commands serialize across sibling leaves sharing one resource', async () => {
  const resourceKey = { provider:'copal', accountId:'queue-acct', workspaceId:'queue-ws', resourceId:'queue-base' };
  const scope = { accountId:'queue-acct', workspace:'queue-ws', epoch:1 };
  const order = []; let active = 0; let maximumActive = 0;
  const command = async ({ command }) => {
    active += 1; maximumActive = Math.max(maximumActive, active); order.push(`${command}:start`);
    await new Promise((resolve) => setTimeout(resolve, 5));
    order.push(`${command}:finish`); active -= 1;
    return { outcome:'applied' };
  };
  const first = createSheetController({ resourceKey, scope, definition, onDefinitionCommand:command });
  const second = createSheetController({ resourceKey, scope, definition, onDefinitionCommand:command });
  await Promise.all([first.command('sort'), second.command('resize')]);
  assert.equal(maximumActive, 1, 'sibling definition commands never overlap');
  assert.deepEqual(order, ['sort:start', 'sort:finish', 'resize:start', 'resize:finish']);
  first.close(); second.close();
});

test('a rejected resource command does not poison the next command', async () => {
  let calls = 0;
  const controller = createSheetController({
    resourceKey:{ provider:'copal', accountId:'reject-acct', workspaceId:'reject-ws', resourceId:'reject-base' },
    scope:{ accountId:'reject-acct', workspace:'reject-ws', epoch:1 }, definition,
    onDefinitionCommand:async () => {
      calls += 1;
      if (calls === 1) throw new Error('first command failed');
      return { outcome:'applied' };
    },
  });
  await assert.rejects(controller.command('first'), /first command failed/);
  assert.deepEqual(await controller.command('second'), { outcome:'applied' });
  assert.equal(calls, 2);
  controller.close();
});
assert.equal(activeSheetView(state).columns[1].label, 'Tags');
assert.equal(activeSheetView(state).columns[3].label, 'State');
assert.equal(state.definition.extensions.title, 'Projects/To Watch.base');
assert.equal(sheetTitle(state.definition.extensions.title), 'To Watch');
assert.equal(isReadOnlyColumn(activeSheetView(state).columns[0]), true);
assert.equal(isReadOnlyColumn(activeSheetView(state).columns[1]), true);
assert.equal(isReadOnlyColumn(activeSheetView(state).columns[2]), false);
assert.equal(cellEditorKind(rows[0], activeSheetView(state).columns[4]), 'number');

state = selectCell(state, 0, 2);
state = selectCell(state, 1, 4, { extend:true });
assert.deepEqual(selectionBounds(state), { rowStart:0, rowEnd:1, columnStart:2, columnEnd:4 });
assert.equal(copySelection(state), 'work\topen\t1\nhome\tdone\t2');
state = selectCell(state, 0, 2);
const preview = pastePreview(state, 'new\t3\nnext\t4');
assert.deepEqual(preview.cells.map(({ rowKey, columnKey, value }) => [rowKey, columnKey, value]), [
  ['doc-1', 'tags', 'new'], ['doc-1', 'status', '3'], ['doc-2', 'tags', 'next'], ['doc-2', 'status', '4'],
]);
assert.deepEqual(preview.rejected.map(({ rowKey, columnKey, reason }) => [rowKey, columnKey, reason]), []);
const invalidTypedPaste = pastePreview(selectCell(state, 0, 4), 'bad-number');
assert.equal(invalidTypedPaste.cells.length, 0);
assert.equal(invalidTypedPaste.rejected[0].reason, 'invalid number');
const resourceRows = rows.map((row) => ({ ...row, resourceKey:{ provider:'copal', accountId:'acct', workspaceId:'ws', resourceId:row.documentId } }));
const resourceState = createSheetState({ definition, rows:resourceRows });
const resourceSelected = selectCell(resourceState, 0, 2);
const resourcePaste = pastePreview(resourceSelected, 'queued');
assert.deepEqual(rowIdentity(resourceRows[0]), { documentId:'doc-1', resourceKey:{ provider:'copal', accountId:'acct', workspaceId:'ws', resourceId:'doc-1' } });
assert.deepEqual(resourcePaste.cells[0], {
  key:'{"provider":"copal","accountId":"acct","workspaceId":"ws","resourceId":"doc-1"}::tags',
  documentId:'doc-1', resourceKey:{ provider:'copal', accountId:'acct', workspaceId:'ws', resourceId:'doc-1' },
  rowKey:'{"provider":"copal","accountId":"acct","workspaceId":"ws","resourceId":"doc-1"}', columnKey:'tags', value:'queued', editorKind:'list',
});
assert.deepEqual(clearSelection(resourceSelected)[0], {
  key:'{"provider":"copal","accountId":"acct","workspaceId":"ws","resourceId":"doc-1"}::tags',
  documentId:'doc-1', resourceKey:{ provider:'copal', accountId:'acct', workspaceId:'ws', resourceId:'doc-1' },
  rowKey:'{"provider":"copal","accountId":"acct","workspaceId":"ws","resourceId":"doc-1"}', columnKey:'tags', value:null, clear:true,
});
assert.deepEqual((await import('../../static/js/copal/sheetModel.js')).parseTsv('"a\tb"\t"line1\nline2"\t"q""uote"').rows, [['a\tb', 'line1\nline2', 'q"uote']]);
assert.equal((await import('../../static/js/copal/sheetModel.js')).parseTsv('x'.repeat(1024 * 1024 + 1)).truncated, true);
state = selectCell(state, 0, 0);
assert.equal(pastePreview(state, 'replacement').rejected[0].reason, 'read-only');
state = selectCell(state, 0, 2);
assert.equal(clearSelection(state).find((cell) => cell.columnKey === 'tags').clear, true);
assert.equal((await import('../../static/js/copal/sheetModel.js')).parseTsv(Array.from({ length:1001 }, (_, index) => String(index)).join('\t')).truncated, true);

const hiddenState = createSheetState({ definition:normalizeSheetDefinition({ views:[{ columns:[{ property:'status' }, { property:'priority', visible:false }, { property:'tags' }] }] }), rows:[{ documentId:'hidden-row', values:{ status:'open', priority:1, tags:['work'] } }] });
assert.deepEqual(visibleColumnIndexes(activeSheetView(hiddenState)), [0, 2]);
const hiddenSelected = selectCell(hiddenState, 0, 0);
assert.equal(moveSelection(hiddenSelected, 0, 1).selected.focus.columnKey, 'tags', 'keyboard movement skips hidden columns');
assert.equal(copySelection(selectCell(hiddenSelected, 0, 2)), 'work');
assert.deepEqual(pastePreview(selectCell(hiddenSelected, 0, 0), 'open\tnew').cells.map((cell) => cell.columnKey), ['status', 'tags'], 'paste follows visible column order');

const requests = [];
const edits = [];
const controllers = [];
const controller = createSheetController({
  resourceKey:state.resourceKey, scope:state.scope, definition, query:async (context) => {
    requests.push(context);
    if (requests.length > 2) return { rows, matchedCount:2, total:7, resultLimited:true, resultLimit:5 };
    return await new Promise((resolve) => controllers.push(resolve));
  },
  onCellEdit:async (change) => { edits.push(change); return { outcome:'applied', revision:{ kind:'copalHead', value:'h2' } }; },
});
const first = controller.refresh('first');
const second = controller.setQuery({ text:'second' });
controllers[0]({ rows:[{ documentId:'stale', values:{ status:'stale' } }], matchedCount:1 });
assert.equal((await first).outcome, 'ignored');
controllers[1]({ rows, matchedCount:2, total:7, resultLimited:true, resultLimit:5 });
assert.equal((await second).outcome, 'applied');
assert.equal(controller.getState().rows[0].documentId, 'doc-1');
assert.deepEqual(controller.getState().counts, { shown:2, matched:2, limited:5, total:7, truncated:false });
controller.select(0, 4);
const editResult = await controller.editCell(rows[0], activeSheetView(controller.getState()).columns[4], 9);
assert.equal(editResult.outcome, 'applied');
assert.equal(edits[0].column.property, 'priority');
assert.equal(edits[0].scope.accountId, 'acct');
controller.close();
assert.equal((await controller.refresh('after-close')).outcome, 'unavailable');
assert.equal(requests[0].resourceKey.resourceId, 'base-1');

assert.equal(sameDefinitionRevision('base-head-1', 'base-head-1'), true);
assert.equal(sameDefinitionRevision(4, '4'), true);
assert.equal(sameDefinitionRevision('base-head-1', 'base-head-2'), false);
let opaqueRevisionCall = 0;
const opaqueRevisionController = createSheetController({
  definition, definitionRevision:'base-head-1', query:async () => {
    opaqueRevisionCall += 1;
    return { rows:[{ documentId:`opaque-${opaqueRevisionCall}`, values:{ status:'fresh' } }], matchedCount:1, definitionRevision:opaqueRevisionCall === 1 ? 'base-head-1' : 'base-head-2' };
  },
});
assert.equal((await opaqueRevisionController.refresh('opaque-equal')).outcome, 'applied');
assert.equal(opaqueRevisionController.getState().rows[0].documentId, 'opaque-1');
opaqueRevisionController.invalidateQueries();
assert.equal((await opaqueRevisionController.refresh('opaque-unequal')).outcome, 'ignored');
assert.equal(opaqueRevisionController.getState().rows[0].documentId, 'opaque-1', 'unequal opaque head cannot replace rows');
opaqueRevisionController.close();

let releaseCoalesced;
let coalescedCalls = 0;
const coalescedController = createSheetController({ definition, query:async () => { coalescedCalls += 1; await new Promise((resolve) => { releaseCoalesced = resolve; }); return { rows, matchedCount:2 }; } });
const pendingRefresh = coalescedController.refresh('same-key');
const joinedRefresh = coalescedController.refresh('same-key');
assert.equal(coalescedCalls, 1);
releaseCoalesced();
assert.equal((await pendingRefresh).outcome, 'applied');
assert.equal((await joinedRefresh).outcome, 'coalesced');
let invalidationCalls = 0;
const invalidationController = createSheetController({ definition, query:async () => { invalidationCalls += 1; return { rows:[{ documentId:`fresh-${invalidationCalls}`, values:{ status:'fresh' } }], matchedCount:1 }; } });
await invalidationController.refresh('initial');
assert.equal((await invalidationController.refresh('cached')).outcome, 'cached');
await invalidationController.refresh('cell-edit');
assert.equal(invalidationCalls, 2, 'cell edits bypass an otherwise identical cached query');
assert.equal(invalidationController.getState().rows[0].documentId, 'fresh-2');
invalidationController.close();
const indexingController = createSheetController({ definition, query:async () => ({ rows:[], status:'indexing', matchedCount:0 }) });
await indexingController.refresh('index');
assert.equal(indexingController.getState().status, 'indexing');
indexingController.close();
const emptyController = createSheetController({ definition, query:async () => ({ rows:[], matchedCount:0, total:0 }) });
await emptyController.refresh('empty');
assert.equal(emptyController.getState().status, 'ready');
assert.equal(emptyController.getState().rows.length, 0, 'empty result remains an explicit ready state');
emptyController.close();
const permissionController = createSheetController({ definition, query:async () => { throw Object.assign(new Error('denied'), { status:403, code:'permission-denied' }); } });
assert.equal((await permissionController.refresh('permission')).outcome, 'failed');
assert.equal(permissionController.getState().error.status, 403, 'permission failures retain their status for the mounted label');
permissionController.close();
const invalidController = createSheetController({ definition, query:async () => { throw Object.assign(new Error('invalid definition'), { status:422, code:'invalid-definition' }); } });
assert.equal((await invalidController.refresh('invalid')).outcome, 'failed');
assert.equal(invalidController.getState().error.code, 'invalid-definition', 'invalid definitions retain their code for the mounted label');
invalidController.close();
coalescedController.invalidateQueries();
coalescedController.close();

const sharedDefinition = { value:normalizeSheetDefinition({ views:[{ id:'table', name:'Table', columns:[{ property:'status' }] }, { id:'cards', name:'Cards', type:'card', columns:[{ property:'status' }] }] }), listeners:new Set() };
sharedDefinition.subscribe = (listener) => { sharedDefinition.listeners.add(listener); return () => sharedDefinition.listeners.delete(listener); };
sharedDefinition.set = (value, revision = null) => { sharedDefinition.value = value; if (revision != null) sharedDefinition.revision = revision; for (const listener of sharedDefinition.listeners) listener(value, revision); };
const firstLeaf = createSheetController({ definitionStore:sharedDefinition, definition:sharedDefinition.value, viewId:'table' });
const secondLeaf = createSheetController({ definitionStore:sharedDefinition, definition:sharedDefinition.value, viewId:'cards' });
const changedDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Renamed', columns:[{ property:'status' }] }, { id:'cards', name:'Cards', type:'card', columns:[{ property:'status' }] }] });
const definitionCommand = createSheetController({ definitionStore:sharedDefinition, definition:sharedDefinition.value, onDefinitionCommand:async () => ({ outcome:'applied', definition:changedDefinition }) });
await definitionCommand.command('rename-view', { viewId:'table', name:'Renamed' });
assert.equal(firstLeaf.getState().definition.views[0].name, 'Renamed');
assert.equal(secondLeaf.getState().viewId, 'cards', 'sibling leaves retain independent active views while sharing definition');
firstLeaf.setPresentation({ density:'comfortable' }); secondLeaf.setPresentation({ density:'compact', wrap:true });
assert.equal(firstLeaf.getState().density, 'comfortable'); assert.equal(secondLeaf.getState().density, 'compact');
definitionCommand.close(); firstLeaf.close(); secondLeaf.close();

const staleBaseDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Stale', columns:[
  { property:'file.name' }, { property:'file.tags' }, { property:'status' },
  { property:'done' }, { property:'score' }, { property:'priority' },
] }] });
const authoritativeBaseDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Authoritative', columns:[
  { property:'file.name' }, { property:'file.tags' }, { property:'status' },
  { property:'priority', visible:false }, { property:'done' }, { property:'due', type:'date' }, { property:'score', type:'number' },
] }] });
const definitionStore = { value:staleBaseDefinition, listeners:new Set() };
definitionStore.subscribe = (listener) => { definitionStore.listeners.add(listener); return () => definitionStore.listeners.delete(listener); };
definitionStore.set = (value, revision = null) => { definitionStore.value = value; if (revision != null) definitionStore.revision = revision; for (const listener of definitionStore.listeners) listener(value, revision); };
let authoritativeCalls = 0;
const authoritativeController = createSheetController({
  definitionStore, definition:staleBaseDefinition,
  rows:[{ documentId:'source-1', values:{ status:'open' } }],
    query:async () => { authoritativeCalls += 1; return { definition:authoritativeBaseDefinition, definitionRevision:'head-2', rows:[{ documentId:'source-1', values:{ status:'open', due:'2026-09-13', score:1 } }], matchedCount:1 }; },
});
authoritativeController.select(0, 2);
assert.equal((await authoritativeController.refresh('authoritative')).outcome, 'applied');
assert.equal(authoritativeCalls, 1, 'publishing a query definition must not recursively refresh its owner');
assert.equal(definitionStore.value.views[0].columns.some((column) => column.property === 'due'), true, 'authoritative query definition must replace stale shared state');
assert.deepEqual(authoritativeController.getState().selected, { anchor:{ rowKey:'source-1', columnKey:'status' }, focus:{ rowKey:'source-1', columnKey:'status' } }, 'definition publication must retain the owner selection');
authoritativeController.close();
const recreatedController = createSheetController({
  definitionStore, definition:staleBaseDefinition,
  query:async () => ({ definitionRevision:'head-3', rows:[{ documentId:'source-1', values:{ status:'open', due:'2026-09-13', score:1 } }], matchedCount:1 }),
});
await recreatedController.refresh('preview-without-definition');
const recreatedColumns = recreatedController.getState().definition.views[0].columns;
assert.equal(recreatedColumns.some((column) => column.property === 'due'), true, 'recreated controller must retain due from the shared authoritative definition');
assert.equal(recreatedColumns.filter((column) => column.visible !== false).length, 6, 'recreated controller must retain all six visible semantic columns');
assert.equal(recreatedController.getState().definitionRevision, 'head-2'); assert.equal(definitionStore.revision, 'head-2', 'definition-less preview cannot infer that an opaque head is newer');
recreatedController.close();

const atomicInitialDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Table', columns:[{ property:'status' }, { property:'due' }] }] });
const atomicNextDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Renamed', columns:[{ property:'status' }, { property:'due' }, { property:'score' }] }] });
const atomicStore = { value:atomicInitialDefinition, revision:'head-1', listeners:new Set() };
atomicStore.subscribe = (listener) => { atomicStore.listeners.add(listener); return () => atomicStore.listeners.delete(listener); };
atomicStore.set = (value, revision = null) => { atomicStore.value = value; if (revision != null) atomicStore.revision = revision; for (const listener of atomicStore.listeners) listener(value, revision); };
let atomicQueries = 0;
const atomicRows = [{ documentId:'source-1', values:{ status:'open', due:'2026-09-13', score:1 } }];
const atomicQuery = async () => { atomicQueries += 1; return { definition:atomicNextDefinition, definitionRevision:'head-2', rows:atomicRows, matchedCount:1 }; };
const atomicCommand = createSheetController({ definitionStore:atomicStore, definition:atomicInitialDefinition, definitionRevision:'head-1', rows:atomicRows, query:atomicQuery, onDefinitionCommand:async () => ({ outcome:'applied', definition:atomicNextDefinition, definitionRevision:'head-2' }) });
const atomicSibling = createSheetController({ definitionStore:atomicStore, definition:atomicInitialDefinition, definitionRevision:'head-1', rows:atomicRows, query:atomicQuery });
atomicCommand.select(0, 0); atomicSibling.select(0, 0);
await atomicCommand.command('rename-view', { viewId:'table', name:'Renamed' });
await new Promise((resolve) => setImmediate(resolve));
assert.equal(atomicQueries, 2, 'definition command must refresh the owner and sibling once');
assert.equal(atomicCommand.getState().status, 'ready'); assert.equal(atomicSibling.getState().status, 'ready');
assert.equal(atomicCommand.getState().definitionRevision, 'head-2'); assert.equal(atomicSibling.getState().definitionRevision, 'head-2', 'atomic definition publication must advance sibling revision before its refresh');
assert.equal(atomicSibling.getState().definition.views[0].name, 'Renamed');
assert.deepEqual(atomicCommand.getState().selected, { anchor:{ rowKey:'source-1', columnKey:'status' }, focus:{ rowKey:'source-1', columnKey:'status' } });
assert.deepEqual(atomicSibling.getState().selected, { anchor:{ rowKey:'source-1', columnKey:'status' }, focus:{ rowKey:'source-1', columnKey:'status' } }, 'sibling definition handoff must retain valid selection');
atomicCommand.close(); atomicSibling.close();

const revisionInitialDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Table', columns:[{ property:'status' }, { property:'due', type:'date' }] }] });
const revisionNextDefinition = normalizeSheetDefinition({ views:[{ id:'table', name:'Renamed', columns:[{ property:'status' }, { property:'due', type:'date', visible:false }, { property:'score', type:'number' }] }] });
const revisionStore = { value:revisionInitialDefinition, revision:'head-1', listeners:new Set() };
revisionStore.subscribe = (listener) => { revisionStore.listeners.add(listener); return () => revisionStore.listeners.delete(listener); };
revisionStore.set = (value, revision = null) => { revisionStore.value = value; if (revision != null) revisionStore.revision = revision; for (const listener of revisionStore.listeners) listener(value, revision); };
const revisionOldRows = [{ documentId:'source-1', values:{ status:'old', due:'2026-09-13' } }];
const revisionNewRows = [{ documentId:'source-1', values:{ status:'new', due:'2026-09-13', score:2 } }];
const delayedOlderResponses = [];
let revisionQueries = 0;
const revisionQuery = async () => {
  revisionQueries += 1;
  if (revisionQueries <= 2) return new Promise((resolve) => delayedOlderResponses.push(resolve));
  return { definition:revisionNextDefinition, definitionRevision:'head-2', rows:revisionNewRows, matchedCount:1 };
};
const revisionOrigin = createSheetController({ definitionStore:revisionStore, definition:revisionInitialDefinition, definitionRevision:'head-1', rows:revisionOldRows, query:revisionQuery, onDefinitionCommand:async () => ({ outcome:'applied', definition:revisionNextDefinition, definitionRevision:'head-2' }) });
const revisionSibling = createSheetController({ definitionStore:revisionStore, definition:revisionInitialDefinition, definitionRevision:'head-1', rows:revisionOldRows, query:revisionQuery });
revisionOrigin.select(0, 1); revisionSibling.select(0, 1);
const oldOriginRefresh = revisionOrigin.refresh('initial'); const oldSiblingRefresh = revisionSibling.refresh('initial');
await revisionOrigin.command('rename-view', { viewId:'table', name:'Renamed' });
for (const resolve of delayedOlderResponses) resolve({ definition:revisionInitialDefinition, definitionRevision:'head-1', rows:revisionOldRows, matchedCount:1 });
await Promise.allSettled([oldOriginRefresh, oldSiblingRefresh]);
await new Promise((resolve) => setImmediate(resolve));
assert.equal(revisionQueries, 4, 'both leaves replace their delayed pre-revision query after the atomic handoff');
assert.equal(revisionOrigin.getState().status, 'ready'); assert.equal(revisionSibling.getState().status, 'ready');
assert.equal(revisionOrigin.getState().definitionRevision, 'head-2'); assert.equal(revisionSibling.getState().definitionRevision, 'head-2');
assert.equal(revisionOrigin.getState().rows[0].values.status, 'new'); assert.equal(revisionSibling.getState().rows[0].values.status, 'new', 'delayed head-1 data cannot overwrite head-2');
assert.equal(revisionOrigin.getState().selected, null, 'selection clears when its property becomes hidden');
assert.equal(revisionSibling.getState().selected, null, 'sibling selection clears when its property becomes hidden');
revisionOrigin.close(); revisionSibling.close();

const delayedCommandStore = { value:revisionInitialDefinition, revision:'head-2', listeners:new Set() };
delayedCommandStore.subscribe = (listener) => { delayedCommandStore.listeners.add(listener); return () => delayedCommandStore.listeners.delete(listener); };
delayedCommandStore.set = (value, revision = null) => { delayedCommandStore.value = value; if (revision != null) delayedCommandStore.revision = revision; for (const listener of delayedCommandStore.listeners) listener(value, revision); };
let resolveDelayedCommand; let delayedRefreshes = 0;
const delayedCommand = createSheetController({ definitionStore:delayedCommandStore, definition:revisionInitialDefinition, definitionRevision:'head-2', query:async () => { delayedRefreshes += 1; return { definition:revisionInitialDefinition, definitionRevision:'head-2', rows:revisionOldRows, matchedCount:1 }; }, onDefinitionCommand:() => new Promise((resolve) => { resolveDelayedCommand = resolve; }) });
const pendingDelayedCommand = delayedCommand.command('rename-view', { viewId:'table', name:'Old command' });
delayedCommand.close();
delayedCommandStore.set(revisionNextDefinition, 'head-3');
resolveDelayedCommand({ outcome:'applied', definition:revisionInitialDefinition, definitionRevision:'head-1' });
assert.equal((await pendingDelayedCommand).outcome, 'ignored', 'a closed leaf cannot publish a delayed definition command');
assert.equal(delayedCommandStore.revision, 'head-3'); assert.equal(delayedCommandStore.value, revisionNextDefinition, 'a delayed closed command cannot overwrite the newer shared definition');
assert.equal(delayedRefreshes, 0, 'a delayed closed command cannot launch a refresh');

const seededStore = { value:revisionNextDefinition, revision:'head-2', listeners:new Set() };
seededStore.subscribe = (listener) => { seededStore.listeners.add(listener); return () => seededStore.listeners.delete(listener); };
seededStore.set = (value, revision = null) => { seededStore.value = value; if (revision != null) seededStore.revision = revision; for (const listener of seededStore.listeners) listener(value, revision); };
const seededLeaf = createSheetController({ definitionStore:seededStore, definition:revisionInitialDefinition, rows:revisionOldRows, query:async () => ({ definition:revisionInitialDefinition, definitionRevision:'head-1', rows:revisionOldRows, matchedCount:1 }) });
assert.equal(seededLeaf.getState().definitionRevision, 'head-2', 'a recreated leaf seeds its state from the shared definition revision');
await seededLeaf.refresh('stale-recreate');
assert.equal(seededLeaf.getState().definitionRevision, 'head-2', 'an old recreated-leaf response cannot roll back the shared revision');
assert.equal(seededLeaf.getState().status, 'ready', 'an ignored old response restores a usable ready state');
assert.equal(seededStore.revision, 'head-2', 'the shared definition envelope remains at the authoritative head');
assert.equal(seededStore.value.views[0].columns.some((column) => column.property === 'score'), true);
seededLeaf.close();
const equalSeededLeaf = createSheetController({ definitionStore:seededStore, definition:revisionNextDefinition, rows:revisionNewRows, query:async () => ({ definition:revisionNextDefinition, definitionRevision:'head-1', rows:revisionOldRows, matchedCount:1 }) });
await equalSeededLeaf.refresh('equal-definition-old-head');
assert.equal(equalSeededLeaf.getState().definitionRevision, 'head-2', 'equal definition content cannot authorize an opaque head rollback');
assert.equal(equalSeededLeaf.getState().rows[0].values.status, 'new', 'equal-definition stale rows are ignored');
assert.equal(equalSeededLeaf.getState().status, 'ready');
equalSeededLeaf.close();

const commandAuthorityDefinition = normalizeSheetDefinition({
  extensions:{ title:'Command authority' },
  views:[{ id:'table', columns:[{ property:'status' }] }],
});
const commandAuthorityNewDefinition = normalizeSheetDefinition({
  extensions:{ title:'Raw local authority' },
  views:[{ id:'table', columns:[{ property:'status' }] }],
});
const commandAuthorityStore = {
  value:commandAuthorityDefinition,
  revision:'head-1',
  authoritativeLocal:null,
  listeners:new Set(),
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); },
  set(value, revision = null) {
    this.value = value; if (revision != null) this.revision = revision;
    this.authoritativeLocal = null;
    for (const listener of [...this.listeners]) listener(value, revision);
  },
  publishLocal(value, revision, scope) {
    this.value = value; this.revision = revision;
    this.authoritativeLocal = { definition:value, revision, scope, source:'raw-editor' };
    for (const listener of [...this.listeners]) listener(value, revision);
  },
};
let resolveStaleCommand;
let authorityRefreshes = 0;
const authorityController = createSheetController({
  definitionStore:commandAuthorityStore,
  definition:commandAuthorityDefinition,
  definitionRevision:'head-1',
  query:async () => { authorityRefreshes += 1; return { rows:[], definitionRevision:'head-2' }; },
  onDefinitionCommand:() => new Promise((resolve) => { resolveStaleCommand = resolve; }),
});
const pendingStaleCommand = authorityController.command('rename-view');
commandAuthorityStore.publishLocal(commandAuthorityNewDefinition, 2, { workspace:'ws' });
const refreshesAfterAuthorityAdvance = authorityRefreshes;
resolveStaleCommand({ outcome:'applied', definition:commandAuthorityDefinition, definitionRevision:'head-1' });
assert.equal((await pendingStaleCommand).reason, 'definition-authority-advanced', 'an open sibling cannot publish a stale command reply');
assert.equal(commandAuthorityStore.revision, 2, 'a local revision barrier remains authoritative');
assert.equal(commandAuthorityStore.value, commandAuthorityNewDefinition, 'the newer local definition remains installed');
assert.equal(authorityController.getState().definitionRevision, 2, 'the controller adopts the newer local revision');
assert.equal(authorityController.getState().definition.extensions.title, 'Raw local authority');
assert.equal(authorityRefreshes, refreshesAfterAuthorityAdvance, 'a stale command reply does not trigger an additional refresh');
authorityController.close();

const rebaseAuthorityStore = {
  value:commandAuthorityDefinition,
  revision:1,
  authoritativeLocal:null,
  listeners:new Set(),
  subscribe(listener) { this.listeners.add(listener); return () => this.listeners.delete(listener); },
  set(value, revision = null) {
    this.value = value; if (revision != null) this.revision = revision;
    this.authoritativeLocal = null;
    for (const listener of [...this.listeners]) listener(value, revision);
  },
  publishLocal(value, revision, scope) {
    this.value = value; this.revision = revision;
    this.authoritativeLocal = { definition:value, revision, scope, source:'raw-editor' };
    for (const listener of [...this.listeners]) listener(value, revision);
  },
};
let resolveRebaseRefresh;
let rebaseQueries = 0;
let resolveRebasedCommand;
const rebasedController = createSheetController({
  definitionStore:rebaseAuthorityStore,
  definition:commandAuthorityDefinition,
  definitionRevision:1,
  query:async () => {
    rebaseQueries += 1;
    if (rebaseQueries === 1) return new Promise((resolve) => { resolveRebaseRefresh = resolve; });
    return { definition:commandAuthorityNewDefinition, definitionRevision:3, rows:[], matchedCount:0 };
  },
  onDefinitionCommand:() => new Promise((resolve) => { resolveRebasedCommand = resolve; }),
});
const pendingRebasedCommand = rebasedController.command('rename-view');
rebaseAuthorityStore.publishLocal(commandAuthorityDefinition, 2, { workspace:'ws' });
rebaseAuthorityStore.set(commandAuthorityDefinition, 2);
resolveRebasedCommand({ outcome:'applied', definition:commandAuthorityNewDefinition, definitionRevision:3, baseLocalRevision:2 });
resolveRebaseRefresh?.({ definition:commandAuthorityDefinition, definitionRevision:1, rows:[], matchedCount:0 });
assert.equal((await pendingRebasedCommand).outcome, 'applied', 'a command rebased over a newer local revision is accepted');
assert.equal(rebaseAuthorityStore.revision, 3, 'the rebased command advances the local revision');
assert.equal(rebaseAuthorityStore.value.extensions.title, 'Raw local authority', 'the rebased definition remains in the shared store');
assert.equal(rebasedController.getState().definitionRevision, 3, 'the controller converges on the rebased revision');
assert.equal(rebasedController.getState().definition.extensions.title, 'Raw local authority');
assert.equal(rebaseQueries >= 2, true, 'the rebased command performs an authoritative refresh after aborting its older sibling query');
rebasedController.close();

console.log('copal sheet model/controller tests passed');
