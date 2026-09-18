import assert from 'node:assert/strict';
import { documentGraph, galaxyGraph, graphStorageKey, normalizeGraphState, headingEntries, headingTree, renameHeading, changeHeadingLevel, deleteHeadingSection, reparentHeadingSection } from '../../static/js/copal/graphModel.js';

const docs = [
  { id:'a', name:'A', kind:'markdown', links:['B'], relations:[{ targetDocumentId:'b', kind:'embed' }, { targetDocumentId:'missing', kind:'link' }] },
  { id:'b', name:'B', kind:'wiki', links:[] },
];
const graph = documentGraph(docs, (name) => docs.find((doc) => doc.name === name));
assert.equal(graph.nodes[0].kind, 'note');
assert.equal(graph.nodes[1].kind, 'wiki');
assert.deepEqual(graph.edges.map((edge) => edge.type), ['link', 'embed'], 'parallel relationship types survive');
assert.equal(new Set(graph.edges.map((edge) => edge.id)).size, 2);

const event = { id:'shared', title:'Shared', primaryTrackId:'a', sharedTrackIds:['b', 'b', 'gone'] };
const galaxy = galaxyGraph([{ id:'a', name:'A' }, { id:'b', name:'B' }], [event, event]);
assert.equal(galaxy.nodes.length, 3, 'shared event has one hub');
assert.deepEqual(galaxy.edges.map((edge) => edge.type), ['primary', 'shared']);
assert.equal(galaxy.nodes[2].kind, 'event');
assert.equal(galaxy.nodes[0].selection.kind, 'track');
const primaryOnly = galaxyGraph([{ id:'a', name:'A' }], [{ id:'only', title:'Primary only', primaryTrackId:'a', sharedTrackIds:[] }]);
assert.deepEqual(primaryOnly.edges.map((edge) => edge.type), ['primary'], 'primary-only events remain graphable');

const state = normalizeGraphState();
state.modes.documents.camera.x = 35;
state.modes.documents.filters.kinds = [];
state.modes.galaxy.camera.w = 2000;
state.modes.galaxy.selection = { kind:'event', id:'shared' };
state.mode = 'galaxy';
const restored = normalizeGraphState(JSON.parse(JSON.stringify(state)));
assert.equal(restored.mode, 'galaxy');
assert.equal(restored.modes.documents.camera.x, 35);
assert.deepEqual(restored.modes.documents.filters.kinds, []);
assert.equal(restored.modes.galaxy.camera.w, 2000);
assert.deepEqual(restored.modes.galaxy.selection, { kind:'event', id:'shared' });
state.source = { kind:'document', docId:'doc-a', resourceKey:{ provider:'copal', resourceId:'r-a' }, revision:{ kind:'copalHead', value:'7' }, state:'selected' };
const scopedSource = normalizeGraphState(JSON.parse(JSON.stringify(state))).source;
assert.deepEqual(scopedSource, state.source, 'shared source envelope survives mode projection');
assert.notEqual(graphStorageKey('account-a', 'study'), graphStorageKey('account-b', 'study'));
assert.notEqual(graphStorageKey('account-a', 'study'), graphStorageKey('account-a', 'other'));
assert.equal(graphStorageKey('', 'study'), null);
assert.equal(normalizeGraphState({ modes:{ documents:{ camera:{ w:-2, x:'invalid' } } } }).modes.documents.camera.w, 1000);
const source = 'Intro\r\nTitle\r\n---\r\nBody\r\n# Branch\r\n## Child\r\n# Tail\r\n';
assert.equal(renameHeading(source, 2, '\u4e16\u754c').split('\r\n')[2], '---', 'Setext rename preserves its underline');
assert.match(changeHeadingLevel(source, 2, 3), /### Title\r\nBody/, 'Setext level conversion removes the underline atomically');
assert.match(deleteHeadingSection(source, 5), /# Tail/, 'nested branch deletion uses a flat section lookup');
assert.equal(reparentHeadingSection('# A\n## A1\n# B\n', 1, 3), '# B\n## A\n### A1\n', 'reparent moves a whole branch and updates levels');
assert.equal(reparentHeadingSection('# A\n## A1\n# B\n', 1, 2), '# A\n## A1\n# B\n', 'reparent rejects descendant cycles');
assert.equal(reparentHeadingSection('# A\n# B\nbody B\n', 1, 2), '# B\nbody B\n## A\n', 'reparent keeps target body outside moved branch');
assert.equal(reparentHeadingSection('# A\r\n# B\r\nbody B\r\n', 1, 2), '# B\r\nbody B\r\n## A\r\n', 'reparent preserves CRLF and trailing newline');
const headings = headingEntries('# A\r\n## A1\r\nA setext\r\n---\r\n# B\r\n### B1\r\n### \u4e16\u754c');
assert.deepEqual(headings.map((entry) => [entry.text, entry.level]), [['A', 1], ['A1', 2], ['A setext', 2], ['B', 1], ['B1', 3], ['\u4e16\u754c', 3]]);
const tree = headingTree(headings);
assert.deepEqual(tree.map((node) => node.text), ['A', 'B']);
assert.deepEqual(tree[0].children.map((node) => node.text), ['A1', 'A setext']);
assert.deepEqual(tree[1].children.map((node) => node.text), ['B1', '\u4e16\u754c']);
const adversarial = '---\ntitle: frontmatter\n---\n# Real\n```md\n# Fake\n```\n~~~txt\n## Also fake\n~~~\n    # Indented code\n# Real two\nSetext text\n---\n';
assert.deepEqual(headingEntries(adversarial).map((entry) => [entry.line, entry.text]), [[4, 'Real'], [12, 'Real two'], [13, 'Setext text']], 'frontmatter, fences, and indented code are excluded');
assert.match(renameHeading(adversarial, 4, 'Renamed'), /```md\n# Fake\n```/);
assert.equal(changeHeadingLevel(adversarial, 6, 3), adversarial, 'fake fenced heading cannot be transformed');
assert.equal(deleteHeadingSection(adversarial, 6), adversarial, 'fake fenced heading cannot be deleted');
assert.equal(reparentHeadingSection('# A\n```md\n# fake\n```\n# B\n', 1, 5), '# B\n## A\n```md\n# fake\n```\n', 'reparent keeps fenced bytes and order');
console.log('Copal Graph/Galaxy projection and state tests passed');
