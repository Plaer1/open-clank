import assert from 'node:assert/strict';
import test from 'node:test';

import { createTreeHouseFeature, hasTreeHouseRole, moveTreeHouseItem, prepareTreeHouseLessonAttachment, profileRoles, treeHouseCommandId, treeHouseHandle, treeHouseSourceClassification, validateTreeHouseFilesDrop } from '../../static/js/copal/treehouse.js';
import { configureCopalStorage } from '../../static/js/copal/storage.js';

test('role checks are explicit and do not infer instructor from learner', () => {
  const learner = { roles: ['learner'] };
  assert.equal(hasTreeHouseRole(learner, 'learner'), true);
  assert.equal(hasTreeHouseRole(learner, 'admin', 'instructor'), false);
  assert.deepEqual([...profileRoles({ roles: ['admin', 'learner'] })], ['admin', 'learner']);
});

test('command IDs are namespaced and unique for idempotent backend handling', () => {
  const first = treeHouseCommandId('course.create');
  const second = treeHouseCommandId('course.create');
  assert.match(first, /^course\.create:/);
  assert.notEqual(first, second);
});

test('ordering creates derived arrays and respects boundaries', () => {
  const source = ['a', 'b', 'c'];
  assert.deepEqual(moveTreeHouseItem(source, 'b', -1), ['b', 'a', 'c']);
  assert.deepEqual(moveTreeHouseItem(source, 'a', -1), source);
  assert.deepEqual(source, ['a', 'b', 'c']);
});

const lessonHandle = treeHouseHandle({ courseId:'course-1', moduleId:'module-1', activityId:'activity-1', lessonId:'lesson-1', accountId:'acct-a', workspace:'school', capability:'edit', grantRevision:7, catalogueRevision:12 });
assert.equal(lessonHandle.kind, 'treehouse-handle');
assert.equal(lessonHandle.capability, 'edit');
assert.equal(treeHouseSourceClassification({ resource_ref:'sealed-ref', capabilities:{ read:true } }).supported, true);
assert.equal(treeHouseSourceClassification({ kind:'directory', resource_ref:'sealed-ref' }).supported, false);
const lessonDrop = { version:1, type:'openclank/files-transfer', kind:'copy', owner:'acct-a', workspace:'school', pane:'files-window', column:'', provider:'host', parent_ref:'host-root', query:'', generation:4, policy_generation:4, selection_epoch:2, sources:[{ item_id:'item-1', resource_key:'host:key-1', resource_ref:'sealed-ref', revision:{ kind:'provider', value:'r1' } }] };
assert.equal(validateTreeHouseFilesDrop(JSON.stringify(lessonDrop), { workspace:'school', generation:4 }).ok, true);
assert.equal(validateTreeHouseFilesDrop(JSON.stringify({ ...lessonDrop, workspace:'other' }), { workspace:'school' }).ok, false);
assert.equal(validateTreeHouseFilesDrop(JSON.stringify({ ...lessonDrop, owner:'acct-b' }), { accountId:'acct-a', workspace:'school' }).reason, 'This resource belongs to another account.');
assert.equal(validateTreeHouseFilesDrop(JSON.stringify(lessonDrop), { accountId:'acct-a', workspace:'school', selectionEpoch:3 }).reason, 'This Files selection is no longer active.');

const lessonCalls = [];
let lessonMutations = 0;
const lessonFiles = {
  roots: async () => { lessonCalls.push('roots'); return { policy_generation: 4 }; },
  stat: async () => { lessonCalls.push('stat'); return { resource:{ ref:'host-ref-12345678', kind:'file', capabilities:{ read:true }, revision:{ kind:'provider', value:'r2' } } }; },
  prepareAttachment: async (body) => { lessonCalls.push(['prepare', body.target.kind, body.target.courseId, body.target.lessonId]); return { preparation_receipt_id:'prep-lesson-1' }; },
};
await prepareTreeHouseLessonAttachment({ handle:lessonHandle, currentHandle:lessonHandle, source:{ provider:'host', resource_ref:'host-ref-12345678', expected_revision:{ kind:'provider', value:'r2' } }, filesClient:lessonFiles, operationId:'lesson-op-1', applyAttachment:async () => { lessonMutations += 1; } });
assert.deepEqual(lessonCalls, ['roots', 'stat', ['prepare', 'treehouse_lesson', 'course-1', 'lesson-1']]);
assert.equal(lessonMutations, 1, 'TreeHouse preparation hands one receipt to one semantic mutation');

class FakeNode {
  constructor(tag, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.textContent = attrs.text || '';
    this.onclick = attrs.onclick; this.classList = { add() {}, remove() {} }; this.style = {};
  }
  append(...children) { this.children.push(...children.filter((child) => child != null)); }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
  focus() {}
  get value() { return this._value || ''; }
  set value(value) { this._value = value; }
  click() { return this.onclick?.(); }
}

function fakeH(tag, attrs = {}, ...children) {
  const node = new FakeNode(tag, attrs); node.append(...children); return node;
}

function walk(node) {
  return [node, ...(node?.children || []).flatMap((child) => typeof child === 'object' ? walk(child) : [])];
}

test('mounted authenticated UI restores account and mode context and previews scoped reset', async () => {
  const storage = new Map(); const session = new Map(); const confirms = [];
  configureCopalStorage('mounted-treehouse-test');
  globalThis.localStorage = { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)) };
  globalThis.sessionStorage = { getItem: (key) => session.get(key) || null, setItem: (key, value) => session.set(key, String(value)) };
  globalThis.window = { styledConfirm: async (message) => { confirms.push(message); return false; }, location: { href: 'https://example.test/copal/treehouse?workspace=school' }, history: { replaceState() {} } };
  globalThis.document = { body: new FakeNode('body') };
  const snapshot = {
    accountId: 'acct-bob', workspace: 'school', actor: { id: 'acct-bob', displayName: 'Bob' },
    permissions: { admin: true, author: true, learner: true, analytics: true, grade: true },
    courseCapabilities: { 'course:shared': { learn: true, edit: false, owner: false, author: false } },
    state: {
      revision: 1, profiles: { 'acct-bob': { id: 'acct-bob', roles: ['admin', 'instructor', 'learner'], active: true } },
      courses: { 'course:shared': { id: 'course:shared', title: 'Shared', description: 'Read only', status: 'published', moduleIds: [] } },
      modules: {}, activities: {}, assignments: {}, skills: {}, badges: {}, quests: {}, courseGrants: {}, enrollments: {}, submissions: {}, evidence: {}, events: [],
    },
    projection: { eventCount: 0, learners: { 'acct-bob': { points: 0, badges: [], quests: [], courses: {}, skills: {}, pointEvidence: [] } }, leaderboard: [], courses: {} },
  };
  const feature = createTreeHouseFeature({ h: fakeH, api: async () => snapshot, setStatus() {}, renderMarkdown: (text) => text, openDocument() {} });
  feature.loadState(); const body = new FakeNode('main'); await feature.render(body);
  const labels = () => walk(body).filter((node) => node.tagName === 'BUTTON').map((node) => node.textContent);
  assert.equal(labels().includes('Edit'), false); assert.equal(labels().includes('Delete'), false); assert.equal(labels().includes('Share'), false);
  const reset = walk(body).find((node) => node.textContent === 'Reset my progress'); await reset.click();
  assert.match(confirms[0], /1 visible course/); assert.match(confirms[0], /Bob in school/); assert.match(confirms[0], /curricula and other learners stay intact/);

  await walk(body).find((node) => node.textContent === 'Open').click();
  await walk(body).find((node) => node.textContent === 'Admin').click();
  await walk(body).find((node) => node.textContent === 'Analytics').click();
  await walk(body).find((node) => node.textContent === 'Learner').click();
  assert.equal(storage.get('odysseus-treehouse-section:acct-bob:learner:scope:mounted-treehouse-test:school'), 'courses');
  assert.equal(storage.get('odysseus-treehouse-course:acct-bob:learner:scope:mounted-treehouse-test:school'), 'course:shared');
  await walk(body).find((node) => node.textContent === 'Admin').click();
  assert.equal(storage.get('odysseus-treehouse-section:acct-bob:admin:scope:mounted-treehouse-test:school'), 'analytics');
  assert.equal(storage.get('odysseus-treehouse-course:acct-bob:admin:scope:mounted-treehouse-test:school'), '');
});

test('mounted lesson exposes a delivered surface, disposable practice, and active contextual help', async () => {
  const storage = new Map(); const events = [];
  configureCopalStorage('mounted-treehouse-help');
  globalThis.localStorage = { getItem: (key) => storage.get(key) || null, setItem: (key, value) => storage.set(key, String(value)) };
  globalThis.sessionStorage = { getItem: () => null, setItem() {} };
  globalThis.window = {
    location: { href: 'https://example.test/copal/treehouse' }, history: { replaceState() {} },
    dispatchEvent: (event) => { events.push(event.detail); return true; },
  };
  globalThis.document = { body: new FakeNode('body'), activeElement: null };
  const activity = {
    id: 'activity:fg-editor:lesson-1', fieldGuideKey: 'fg-document-pilot', title: 'Editor practice', activityType: 'lesson', status: 'published', points: 10,
    content: 'Edit the disposable note.', moduleId: 'module:editor', skillIds: [],
    surface: { key: 'editor', label: 'Editor', href: '/copal/editor', locator: '[data-copal-view=notes]' },
    practiceFixture: 'field-guide/fg-editor', practice: { title: 'Disposable note', seed: '# Practice\n- [ ] Check', expectedEvidence: 'The note reopens with the heading and checklist.' },
    verifierSpec: { kind: 'editor_markdown_revision', evidence: 'The note reopens with the heading and checklist.' },
  };
  const snapshot = {
    accountId: 'acct-bob', workspace: 'school', actor: { id: 'acct-bob', displayName: 'Bob' },
    permissions: { admin: false, author: false, learner: true, analytics: false, grade: false }, courseCapabilities: { 'course:editor': { learn: true, edit: false } },
    state: {
      revision: 1, profiles: { 'acct-bob': { id: 'acct-bob', roles: ['learner'], active: true } },
      courses: { 'course:editor': { id: 'course:editor', title: 'Editor', description: 'Practice', status: 'published', moduleIds: ['module:editor'] } },
      modules: { 'module:editor': { id: 'module:editor', courseId: 'course:editor', title: 'Practice', activityIds: [activity.id], assignmentIds: [] } }, activities: { [activity.id]: activity }, assignments: {}, skills: {}, badges: {}, quests: {}, courseGrants: {},
      enrollments: { 'enrollment:editor:bob': { id: 'enrollment:editor:bob', courseId: 'course:editor', profileId: 'acct-bob' } }, submissions: {}, evidence: {}, events: [],
    },
    projection: { eventCount: 0, learners: { 'acct-bob': { points: 0, badges: [], quests: [], courses: {}, skills: {}, pointEvidence: [] } }, leaderboard: [], courses: {} },
  };
  const feature = createTreeHouseFeature({ h: fakeH, api: async () => snapshot, setStatus() {}, renderMarkdown: (text) => text, openDocument() {} });
  feature.loadState(); const body = new FakeNode('main'); await feature.render(body);
  await walk(body).find((node) => node.textContent === 'Open').click();
  assert.ok(walk(body).some((node) => node.textContent === 'Practice fixture: Disposable note'));
  assert.ok(walk(body).some((node) => node.textContent === 'Open Editor'));
  await walk(body).find((node) => node.textContent === 'Ask for help').click();
  assert.equal(events[0].accountId, 'acct-bob'); assert.equal(events[0].workspace, 'school');
  assert.equal(events[0].resourceKind, 'treehouse-lesson'); assert.equal(events[0].courseId, 'course:editor');
  assert.equal(events[0].surface, 'treehouse');
  assert.equal(events[0].lessonSurface.href, '/copal/editor');
});
