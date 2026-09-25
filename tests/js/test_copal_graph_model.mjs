import assert from 'node:assert/strict';
import { documentGraph, galaxyGraph, graphStorageKey, normalizeGraphState, headingEntries, headingTree, renameHeading, changeHeadingLevel, deleteHeadingSection, reparentHeadingSection, GRAPH_MODES, structureEntries, structureTree, deriveFacets, matchesFilters, filterDocuments, reconcileFilters, officialDocsRoot, isOfficialDocument, documentFolder, facetCacheKey } from '../../static/js/copal/graphModel.js';

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

// S20: Mind joins Graph. Legacy `mind` saved state projects onto structure
// mode and there is no separate Mind identity in the mode vocabulary.
assert.deepEqual(GRAPH_MODES, ['documents', 'structure', 'galaxy'], 'Graph carries both views plus Galaxy; Mind is not a mode');
const legacyMindState = normalizeGraphState({ mode:'mind', modes:{ mind:{ camera:{ x:12, y:34, w:800, h:520 }, navigation:{ docId:'doc-7', selectedLine:4 }, filters:{ search:'old' } } } });
assert.equal(legacyMindState.mode, 'structure', 'legacy Mind identity maps to Graph structure mode');
assert.equal(legacyMindState.modes.structure.camera.x, 12);
assert.equal(legacyMindState.modes.structure.navigation.docId, 'doc-7');
assert.equal(legacyMindState.modes.structure.navigation.selectedLine, 4);
assert.equal(legacyMindState.modes.structure.filters.search, 'old');
assert.equal(legacyMindState.modes.mind, undefined, 'no standalone Mind mode state is created');

// S20: structure entries cover nested bullets beside headings, excluding
// code fences, frontmatter/properties and literal (indented) code.
const mixed = '---\ntitle: props\n---\n# Root\n- top item\n  - nested item\n    - deep item\n1. ordered\n```md\n- fake bullet\n# fake heading\n```\n    - literal indented\n## Child\n* star bullet\nPlain prose\n';
const structure = structureEntries(mixed);
assert.deepEqual(structure.map((entry) => [entry.kind, entry.text]), [
  ['heading', 'Root'], ['bullet', 'top item'], ['bullet', 'nested item'], ['bullet', 'deep item'],
  ['bullet', 'ordered'], ['heading', 'Child'], ['bullet', 'star bullet'],
], 'structure shows headings and nested bullets, not fences/frontmatter/literals');
const structureMap = structureTree(structure);
assert.deepEqual(structureMap.map((node) => node.text), ['Root'], 'one root heading holds the document structure');
assert.deepEqual(structureMap[0].children.map((node) => node.text), ['top item', 'ordered', 'Child'], 'top-level bullets and child headings share one tree under the root');
assert.deepEqual(structureMap[0].children[0].children.map((node) => node.text), ['nested item'], 'indented bullet nests under its parent bullet');
assert.deepEqual(structureMap[0].children[0].children[0].children.map((node) => node.text), ['deep item'], 'deeply indented bullets keep nesting');
assert.deepEqual(structureMap[0].children[2].children.map((node) => node.text), ['star bullet'], 'bullets nest under their enclosing child heading');
const noStructure = structureEntries('Just a paragraph.\nAnother line.\n');
assert.equal(noStructure.length, 0, 'a source without headings or bullets yields a useful empty structure');

// S20: facets are derived from real scoped metadata, folders and values.
const corpus = [
  { id:'n1', name:'Notes/Alpha.md', kind:'note', tags:['alpha', 'shared'], properties:{ status:'open', owner:'sam' }, text:'Alpha body' },
  { id:'n2', name:'Notes/Beta.md', kind:'note', tags:['beta'], properties:{ status:'done', owner:'kim' }, text:'Beta body' },
  { id:'w1', name:'Notes/Guide.md', kind:'wiki', tags:['shared'], properties:{ status:'open' }, text:'Guide body' },
  { id:'e1', name:'.events/Standup.md', kind:'note', tags:['calendar'], properties:{}, text:'Standup' },
  { id:'o1', name:'OpenClank/Start Here', kind:'note', tags:['builtin'], properties:{ product:'open-clank', builtin:true }, builtin:true, text:'Official' },
];
assert.equal(documentFolder('Notes/Alpha.md'), 'Notes');
assert.equal(documentFolder('Alpha.md'), '');
assert.ok(isOfficialDocument(corpus[4]), 'provisioned doc is recognized by identity metadata');
assert.ok(!isOfficialDocument(corpus[0]), 'personal note is not official');
assert.ok(!isOfficialDocument(corpus[3]), 'dot-folder personal event is not official documentation');
assert.equal(officialDocsRoot(corpus), 'OpenClank', 'official root derives from provisioned identities, not a hardcoded English name');
const facets = deriveFacets(corpus, { generation:'gen-1' });
assert.equal(facets.generation, 'gen-1');
assert.equal(facets.officialRoot, 'OpenClank');
assert.deepEqual(facets.kinds.map((item) => item.value).sort(), ['note', 'wiki']);
assert.deepEqual(facets.folders.map((item) => item.value).sort(), ['.events', 'Notes', 'OpenClank']);
assert.deepEqual(facets.tags.map((item) => item.value).sort(), ['alpha', 'beta', 'builtin', 'calendar', 'shared']);
assert.deepEqual(facets.properties.status.map((item) => item.value).sort(), ['done', 'open']);
assert.deepEqual(facets.properties.owner.map((item) => item.value).sort(), ['kim', 'sam']);
assert.equal(facets.tags.find((item) => item.value === 'shared').count, 2, 'facet counts reflect real usage');

// Official docs are excluded by default through the folder filter and can be
// explicitly included; dot-folder personal content stays governed by ordinary
// filters rather than being silently dropped.
const defaultVisible = filterDocuments(corpus, {}, facets);
assert.ok(!defaultVisible.some((doc) => doc.id === 'o1'), 'official docs hidden by default');
assert.ok(defaultVisible.some((doc) => doc.id === 'e1'), 'personal dot-folder event remains filterable');
const withOfficial = filterDocuments(corpus, { includeOfficial:true }, facets);
assert.ok(withOfficial.some((doc) => doc.id === 'o1'), 'official docs are explicitly includable');
assert.ok(matchesFilters(corpus[3], { tags:['calendar'] }, facets), 'dot-folder content responds to ordinary tag filters');
assert.ok(!matchesFilters(corpus[0], { folders:['.events'] }, facets), 'folder filter applies to real folder paths');
assert.ok(matchesFilters(corpus[0], { properties:{ status:['open'] } }, facets), 'property facet filter matches real metadata values');
assert.ok(!matchesFilters(corpus[1], { properties:{ status:['open'] } }, facets), 'property filter excludes non-matching values');

// Saved filters reconcile renamed/deleted values without imaginary hardcoding.
const stale = reconcileFilters({ search:'keep', kinds:['note','gone-kind'], folders:['Notes','Deleted'], tags:['alpha','missing-tag'], properties:{ status:['open','ghost'], vanished:['x'] }, includeOfficial:true }, facets);
assert.equal(stale.search, 'keep', 'search text is preserved');
assert.deepEqual(stale.kinds, ['note'], 'unknown kind values are dropped against live facets');
assert.deepEqual(stale.folders, ['Notes'], 'deleted folders recover without hardcoding');
assert.deepEqual(stale.tags, ['alpha'], 'renamed tags recover against live facets');
assert.deepEqual(stale.properties, { status:['open'] }, 'unknown property keys/values are reconciled away');
assert.equal(stale.includeOfficial, true, 'explicit official inclusion survives reconciliation');
assert.deepEqual(reconcileFilters({ kinds:['anything'] }, null).kinds, ['anything'], 'a missing snapshot leaves saved intent untouched');

// Facet caches are scoped by account/workspace/index generation.
assert.notEqual(facetCacheKey('a', 'w', 'g1'), facetCacheKey('a', 'w', 'g2'));
assert.notEqual(facetCacheKey('a', 'w', 'g1'), facetCacheKey('b', 'w', 'g1'));
assert.equal(facetCacheKey('', 'w', 'g1'), null);

console.log('Copal Graph/Galaxy projection and state tests passed');
