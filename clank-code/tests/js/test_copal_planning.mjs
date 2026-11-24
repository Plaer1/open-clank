import assert from 'node:assert/strict';
import { createPlanningFeature, patchTimelineEventNodes, prepareTimelineAttachment, timelineEventNodeIndex, validateTimelineFilesDrop } from '../../static/js/copal/planning.js';
import { styledConfirm, styledPrompt } from '../../static/js/dialogPrimitives.js';
import {
  DEFAULT_DAY_WIDTH,
  MAX_DAY_WIDTH,
  MIN_DAY_WIDTH,
  MAX_RECURRENCE_EXPANSION,
  WARM_HISTORY_DAYS,
  addDays,
  daysBetween,
  dragPatch,
  effectiveTrackState,
  eventLayout,
  expandRecurrence,
  flattenTrackPreorder,
  formatLocalDate,
  glyphFor,
  manipulationGate,
  normalizeTrackHierarchy,
  parseLocalDate,
  reparentTrackSubtree,
  trackBreadcrumb,
  trackDescendantIds,
  timelineEventHandle,
  timelineEventResourceKey,
  timelineSemanticCommand,
  timelineSourceClassification,
} from '../../static/js/copal/planningModel.js';

assert.equal(typeof createPlanningFeature, 'function', 'Planning remains importable without a browser UI bootstrap');
assert.equal(await styledConfirm('Node import'), false, 'confirm primitive is inert outside the browser');
assert.equal(await styledPrompt('Node import'), null, 'prompt primitive is inert outside the browser');

assert.equal(MIN_DAY_WIDTH, 8);
assert.equal(DEFAULT_DAY_WIDTH, 18);
assert.equal(MAX_DAY_WIDTH, 56);
assert.equal(WARM_HISTORY_DAYS, 60);

const eventHandle = timelineEventHandle({ id:'event-1', head:'head-7', trackId:'track-a' }, { accountId:'acct-a', workspace:'school' });
assert.equal(eventHandle.kind, 'copal-planning-event');
assert.deepEqual(timelineEventResourceKey(eventHandle), { provider:'copal', account_id:'acct-a', workspace_id:'school', resource_id:'event-1' });
assert.deepEqual(timelineSemanticCommand('timeline.event.nudge', { eventId:'event-1', delta:1 }), { type:'timeline.event.nudge', payload:{ eventId:'event-1', delta:1 } });
assert.deepEqual(timelineSourceClassification({ resource_ref:'sealed-ref', provider:'host', capabilities:{ read:true } }).supported, true);
assert.equal(timelineSourceClassification({ kind:'folder', resource_ref:'sealed-ref' }).reason, 'Folders cannot be attached to an event.');
const timelineDrop = {
  version:1, type:'openclank/files-transfer', kind:'copy', owner:'acct-a', workspace:'school', pane:'files-window', column:'', provider:'host', parent_ref:'host-root', query:'', generation:4, policy_generation:4, selection_epoch:2,
  sources:[{ item_id:'item-1', resource_key:'host:key-1', resource_ref:'sealed-ref', revision:{ kind:'provider', value:'r1' } }],
};
assert.equal(validateTimelineFilesDrop(JSON.stringify(timelineDrop), { accountId:'acct-a', workspace:'school', generation:4 }).ok, true);
assert.equal(validateTimelineFilesDrop(JSON.stringify({ ...timelineDrop, generation:5 }), { accountId:'acct-a', workspace:'school', generation:4 }).ok, false);
assert.equal(validateTimelineFilesDrop(JSON.stringify({ ...timelineDrop, owner:'acct-b' }), { accountId:'acct-a', workspace:'school' }).reason, 'This resource belongs to another account.');
assert.equal(validateTimelineFilesDrop(JSON.stringify(timelineDrop), { accountId:'acct-a', workspace:'school', provider:'gallery' }).reason, 'This Files provider is no longer active.');
assert.equal(validateTimelineFilesDrop(JSON.stringify(timelineDrop), { accountId:'acct-a', workspace:'school', policyGeneration:5 }).reason, 'File policy changed; select the resource again.');

const timelineCalls = [];
let timelineMutations = 0;
const timelineFiles = {
  roots: async () => { timelineCalls.push('roots'); return { policy_generation: 4 }; },
  resolveResource: async () => { timelineCalls.push('resolve'); return { ref:'copal-ref-12345678', revision:{ kind:'copalHead', value:'head-7' } }; },
  stat: async () => { timelineCalls.push('stat'); return { resource:{ ref:'host-ref-12345678', kind:'file', capabilities:{ read:true }, revision:{ kind:'provider', value:'r2' } } }; },
  prepareAttachment: async (body) => { timelineCalls.push(['prepare', body.target.kind, body.mode]); return { preparation_receipt_id:'prep-timeline-1', insertion:{ format:'markdown', link_target:'asset.md' }, source_revision:{ kind:'provider', value:'r2' }, target_revision:{ kind:'copalHead', value:'head-7' } }; },
};
await prepareTimelineAttachment({ event:{ id:'event-1', head:'head-7', trackId:'track-a' }, accountId:'acct-a', workspace:'school', source:{ provider:'host', resource_ref:'host-ref-12345678', expected_revision:{ kind:'provider', value:'r2' } }, filesClient:timelineFiles, operationId:'timeline-op-1', applyAttachment:async () => { timelineMutations += 1; } });
assert.deepEqual(timelineCalls, ['roots', 'resolve', 'stat', ['prepare', 'copal_document', 'link']]);
assert.equal(timelineMutations, 1, 'Timeline preparation hands one receipt to one semantic mutation');

const dstStart = parseLocalDate('2026-03-07');
assert.equal(formatLocalDate(addDays(dstStart, 2)), '2026-03-09');
assert.equal(daysBetween('2026-03-07', '2026-03-09'), 2);
assert.equal(formatLocalDate(addDays('2024-02-28', 1)), '2024-02-29');
assert.equal(formatLocalDate(addDays('2026-12-31', 1)), '2027-01-01');

const hard = { startDate:'2026-07-10', dueDate:'2026-07-12' };
assert.deepEqual(dragPatch(hard, 'move', 2), { startDate:'2026-07-12', dueDate:'2026-07-14' });
assert.deepEqual(dragPatch(hard, 'left', 1), { startDate:'2026-07-11' });
assert.deepEqual(dragPatch(hard, 'right', -1), { dueDate:'2026-07-11' });
assert.equal(manipulationGate(hard, {}).movable, true);
assert.equal(manipulationGate({ ...hard, startDate:'FUZZY', fuzzy:{ anchorStart:'2026-07-10' } }, {}).movable, false);
assert.equal(manipulationGate(hard, { special:true }).resizeRight, false);

const fuzzy = eventLayout({ startDate:'FUZZY', fuzzy:{ anchorStart:'2026-07-10', anchorEnd:'2026-07-15' } }, parseLocalDate('2026-01-01'));
assert.equal(formatLocalDate(fuzzy.start), '2026-07-10');
assert.equal(formatLocalDate(fuzzy.end), '2026-07-15');
assert.equal(glyphFor('car'), '🚗');
assert.equal(glyphFor('🦕'), '🦕');

const firstLane = { dataset:{ taskId:'shared-event' } };
const secondLane = { dataset:{ taskId:'shared-event' } };
const otherEvent = { dataset:{ taskId:'other-event' } };
const nodeIndex = timelineEventNodeIndex([firstLane, secondLane, otherEvent]);
assert.deepEqual(nodeIndex.get('shared-event'), [firstLane, secondLane], 'shared event retains every rendered lane instance');
assert.deepEqual(nodeIndex.get('other-event'), [otherEvent]);

function fakeEventNode() {
  const attrs = new Map();
  const classes = new Set(['copal-event']);
  const label = { textContent:'' };
  const surface = {
    progress:null,
    querySelector(selector) { return selector === '.copal-event-progress' ? this.progress : null; },
    append(node) { this.progress = node; },
  };
  const node = {
    dataset:{ taskId:'shared-event' },
    style:{ left:'90px', width:'180px' },
    title:'old',
    ownerDocument:{ createElement() { return { className:'', style:{ setProperty(name, value) { this[name] = value; } }, remove() { surface.progress = null; } }; } },
    classList:{ toggle(name, enabled) { enabled ? classes.add(name) : classes.delete(name); } },
    querySelector(selector) { return selector === '.copal-event-label' ? label : selector === '.copal-event-surface' ? surface : null; },
    setAttribute(name, value) { attrs.set(name, value); },
    getAttribute(name) { return attrs.get(name); },
    _classes:classes,
    _attrs:attrs,
    _label:label,
    _surface:surface,
  };
  return node;
}

const duplicateA = fakeEventNode();
const duplicateB = fakeEventNode();
const focusedNode = duplicateB;
const scrollState = { scrollLeft:127, scrollTop:19 };
const originalGeometry = [duplicateA.style.left, duplicateA.style.width, duplicateB.style.left, duplicateB.style.width];
const changedEvent = { id:'shared-event', title:'Updated title', status:'done', startDate:'2026-07-10', dueDate:'2026-07-12', fuzzy:{ anchorStart:'2026-07-10' }, stages:[{ id:'stage-1', done:true }, { id:'stage-2', done:false }] };
assert.equal(patchTimelineEventNodes([duplicateA, duplicateB], changedEvent), true);
for (const node of [duplicateA, duplicateB]) {
  assert.equal(node._label.textContent, 'Updated title');
  assert.ok(node._classes.has('done'));
  assert.ok(node._classes.has('fuzzy'));
  assert.equal(node._attrs.get('aria-label'), 'Updated title, 2026-07-10 to 2026-07-12');
  assert.match(node.title, /^Updated title\n/);
  assert.equal(node._surface.progress.style['--progress'], '50%');
}
assert.deepEqual([duplicateA.style.left, duplicateA.style.width, duplicateB.style.left, duplicateB.style.width], originalGeometry);
assert.equal(focusedNode, duplicateB);
assert.deepEqual(scrollState, { scrollLeft:127, scrollTop:19 });
assert.equal(patchTimelineEventNodes([duplicateA, duplicateB], { ...changedEvent, title:'No stages', status:'pending', fuzzy:undefined, stages:[] }), true);
for (const node of [duplicateA, duplicateB]) {
  assert.equal(node._label.textContent, 'No stages');
  assert.ok(!node._classes.has('done'));
  assert.ok(!node._classes.has('fuzzy'));
  assert.equal(node._surface.progress, null);
}
assert.equal(patchTimelineEventNodes([], changedEvent), false, 'incomplete cache index forces a rebuild');

// ── Track hierarchy ────────────────────────────────────────────

const mixedTracks = [
  { id:'grandchild', name:'Kitchen', parentTrackId:'child', color:'#333333', extra:{ keep:true } },
  { id:'other', name:'Other', parentTrackId:null, color:'#444444' },
  { id:'child', name:'Packing', parentTrackId:'root', color:'#222222' },
  { id:'root', name:'Move', parentTrackId:null, color:'#111111' },
];
assert.deepEqual(flattenTrackPreorder(mixedTracks).map((track) => track.id), ['other','root','child','grandchild']);
assert.equal(trackBreadcrumb(mixedTracks, 'root'), 'Move');
assert.equal(trackBreadcrumb(mixedTracks, 'child'), 'Move / Packing');
assert.equal(trackBreadcrumb(mixedTracks, 'grandchild'), 'Move / Packing / Kitchen');
assert.deepEqual(trackDescendantIds(mixedTracks, 'root'), ['child','grandchild']);

const originalTracks = structuredClone(mixedTracks);
const movedBelowOther = reparentTrackSubtree(mixedTracks, 'child', 'other');
assert.deepEqual(movedBelowOther.map((track) => track.id), ['other','child','grandchild','root']);
assert.equal(movedBelowOther.find((track) => track.id === 'child').parentTrackId, 'other');
assert.equal(movedBelowOther.find((track) => track.id === 'grandchild').parentTrackId, 'child');
assert.deepEqual(mixedTracks, originalTracks, 'reparent must not mutate its input');
const movedToRoot = reparentTrackSubtree(movedBelowOther, 'child', null);
assert.deepEqual(movedToRoot.map((track) => track.id), ['other','root','child','grandchild']);
assert.equal(movedToRoot.find((track) => track.id === 'child').parentTrackId, null);
assert.deepEqual(reparentTrackSubtree(mixedTracks, 'child', 'root'), mixedTracks, 'same-parent move preserves exact source order and shape');

assert.throws(() => reparentTrackSubtree(mixedTracks, 'root', 'root'), /cannot parent itself/);
assert.throws(() => reparentTrackSubtree(mixedTracks, 'root', 'grandchild'), /descendant/);
assert.throws(() => reparentTrackSubtree(mixedTracks, 'root', 'missing'), /Unknown target parent/);
assert.throws(() => normalizeTrackHierarchy([{ id:'a', parentTrackId:'missing' }]), /unknown parent/);
assert.throws(() => normalizeTrackHierarchy([{ id:'a', parentTrackId:'b' }, { id:'b', parentTrackId:'a' }]), /cycle/);

const explicitState = effectiveTrackState(mixedTracks, 'grandchild', {
  hiddenTracks:new Set(['grandchild']), collapsedTrackGroups:new Set(),
});
assert.deepEqual({ ownHidden:explicitState.ownHidden, suppressedBy:explicitState.suppressedBy, visible:explicitState.visible }, { ownHidden:true, suppressedBy:'grandchild', visible:false });
const hiddenAncestor = effectiveTrackState(mixedTracks, 'grandchild', {
  hiddenTracks:new Set(['root']), collapsedTrackGroups:new Set(),
});
assert.deepEqual({ ownHidden:hiddenAncestor.ownHidden, ancestorHiddenBy:hiddenAncestor.ancestorHiddenBy, visible:hiddenAncestor.visible }, { ownHidden:false, ancestorHiddenBy:'root', visible:false });
const collapsedAncestor = effectiveTrackState(mixedTracks, 'grandchild', {
  hiddenTracks:new Set(), collapsedTrackGroups:new Set(['child']),
});
assert.equal(collapsedAncestor.ancestorCollapsedBy, 'child');
assert.equal(collapsedAncestor.visible, false);
const disabledTracks = mixedTracks.map((track) => track.id === 'child' ? { ...track, enabled:false } : track);
const disabledAncestor = effectiveTrackState(disabledTracks, 'grandchild');
assert.equal(disabledAncestor.ancestorDisabledBy, 'child');
assert.equal(disabledAncestor.visible, false);
assert.equal(effectiveTrackState(disabledTracks, 'root').visible, true);

const deepTracks = [];
for (let index = 0; index < 4000; index++) deepTracks.push({ id:`deep-${index}`, name:`Deep ${index}`, parentTrackId:index ? `deep-${index - 1}` : null });
assert.equal(flattenTrackPreorder(deepTracks).length, 4000);
assert.equal(trackDescendantIds(deepTracks, 'deep-0').length, 3999);

// ── Recurrence expansion (JS) ─────────────────────────────────────────

const weekly = { id:'E1', title:'Standup', startDate:'2026-07-07', dueDate:'2026-07-07', recurrence:{ frequency:'weekly', interval:1, count:4 } };
const wOCC = expandRecurrence(weekly, parseLocalDate('2026-07-01'), parseLocalDate('2026-08-15'));
assert.equal(wOCC.length, 4);
assert.equal(wOCC[0].occurrenceKey, '2026-07-07');
assert.equal(wOCC[3].occurrenceKey, '2026-07-28');

const daily = { id:'E2', title:'Meditate', startDate:'2026-07-10', recurrence:{ frequency:'daily', interval:1, until:'2026-07-13' } };
const dOCC = expandRecurrence(daily, parseLocalDate('2026-07-01'), parseLocalDate('2026-07-31'));
assert.deepEqual(dOCC.map((o) => o.occurrenceKey), ['2026-07-10','2026-07-11','2026-07-12','2026-07-13']);

const monthly = { id:'E3', title:'Rent', startDate:'2026-01-15', recurrence:{ frequency:'monthly', interval:1, count:3 } };
const mOCC = expandRecurrence(monthly, parseLocalDate('2026-01-01'), parseLocalDate('2026-12-31'));
assert.deepEqual(mOCC.map((o) => o.occurrenceKey), ['2026-01-15','2026-02-15','2026-03-15']);

const noRec = { id:'E7', title:'One-shot', startDate:'2026-07-10' };
assert.equal(expandRecurrence(noRec, parseLocalDate('2026-07-01'), parseLocalDate('2026-07-31')).length, 0);

const skip = { id:'E5', title:'Yoga', startDate:'2026-07-07', recurrence:{ frequency:'weekly', interval:1, count:4, exceptionDates:['2026-07-14','2026-07-28'] } };
const sOCC = expandRecurrence(skip, parseLocalDate('2026-07-01'), parseLocalDate('2026-08-15'));
assert.equal(sOCC.length, 2);
assert.ok(!sOCC.some((o) => o.occurrenceKey === '2026-07-14'));
assert.ok(!sOCC.some((o) => o.occurrenceKey === '2026-07-28'));

const cap = { id:'E6', title:'Freq', startDate:'2020-01-01', recurrence:{ frequency:'daily', interval:1, count:1000 } };
assert.equal(expandRecurrence(cap, parseLocalDate('2020-01-01'), parseLocalDate('2025-12-31')).length, MAX_RECURRENCE_EXPANSION);

const dur = { id:'E9', title:'Trip', startDate:'2026-07-10', dueDate:'2026-07-12', recurrence:{ frequency:'monthly', interval:1, count:2 } };
const durOcc = expandRecurrence(dur, parseLocalDate('2026-07-01'), parseLocalDate('2026-12-31'));
assert.equal(durOcc[0].startDate, '2026-07-10');
assert.equal(durOcc[0].dueDate, '2026-07-12');
assert.equal(durOcc[1].startDate, '2026-08-10');
assert.equal(durOcc[1].dueDate, '2026-08-12');

console.log('copal planning helpers: ok');
