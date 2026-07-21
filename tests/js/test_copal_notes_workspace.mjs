import assert from 'node:assert/strict';

import {
  NOTES_PANELS,
  NOTES_WORKSPACE_VERSION,
  activateWorkspaceLeaf,
  closeWorkspaceGroup,
  closeWorkspaceLeaf,
  closeWorkspaceOtherLeaves,
  findWorkspaceLeaf,
  moveWorkspaceLeaf,
  normalizeNotesSettings,
  normalizeNotesWorkspace,
  noteViewType,
  openWorkspaceDocument,
  resizeWorkspaceSplit,
  serializeNotesWorkspace,
  setWorkspacePanelPlacement,
  splitWorkspaceGroup,
  workspaceGroups,
  workspaceLeaves,
  workspacePanelsForSide,
} from '../../static/js/copal/notesWorkspace.js';
import {
  coercePropertyValue,
  databaseRelations,
  flattenTree,
  fuzzyScore,
  linkedMentions,
  moveHeadingSection,
  moveHeadingSectionTo,
  moveFrontmatterProperty,
  outlineEntries,
  outlineTree,
  parseCanvasDocument,
  parseFrontmatter,
  reparentHeading,
  renameFrontmatterProperty,
  removeFrontmatterProperty,
  resolveDocumentLink,
  setFrontmatterProperty,
  unlinkedMentions,
} from '../../static/js/copal/notesModel.js';

const docs = [
  { id:'a', name:'Projects/Alpha.md', kind:'markdown', text:'# Alpha' },
  { id:'b', name:'Canvas.canvas', kind:'canvas', text:'{"nodes":[],"edges":[]}' },
  { id:'c', name:'Tasks.base', kind:'base', text:'{}' },
  { id:'d', name:'Photo.png', kind:'asset', text:'' },
  { id:'n', name:'Ideas/Native Note', kind:'note', text:'A database note', properties:{ tags:['native'] } },
  { id:'t', name:'Timeline', kind:'timeline', virtual:true, text:'' },
];

const clean = normalizeNotesWorkspace({}, docs, 'a');
assert.equal(clean.version, NOTES_WORKSPACE_VERSION);
assert.equal(clean.settings.previewLayout, 'inline');
assert.equal(clean.settings.lineNumbers, false);
assert.equal(clean.right.open, false);
assert.equal(clean.left.sort, 'name');
assert.deepEqual(clean.bookmarks, []);
assert.equal(findWorkspaceLeaf(clean)?.docId, 'a');
assert.deepEqual(normalizeNotesSettings({ previewLayout:'side-by-side', lineNumbers:true, readableLineWidth:false, ribbon:true }), {
  previewLayout:'side-by-side', lineNumbers:true, readableLineWidth:false, ribbon:true, completedVisibility:'show',
});
assert.deepEqual(normalizeNotesSettings({ previewLayout:'invalid', lineNumbers:'yes' }), {
  previewLayout:'inline', lineNumbers:false, readableLineWidth:true, ribbon:false, completedVisibility:'show',
});
assert.deepEqual(normalizeNotesSettings({ completedVisibility:'hide' }), {
  previewLayout:'inline', lineNumbers:false, readableLineWidth:true, ribbon:false, completedVisibility:'hide',
});
assert.deepEqual(normalizeNotesSettings({ completedVisibility:'invalid' }), {
  previewLayout:'inline', lineNumbers:false, readableLineWidth:true, ribbon:false, completedVisibility:'show',
});

const legacy = normalizeNotesWorkspace({
  tabs:['a', 'missing'], selected:'a', split:'b', mode:'reading', sidebarOpen:true,
  sidebar:'outline', expanded:['Projects'], pinned:['a'], cursors:{ a:{ anchor:2, head:4 } },
}, docs);
assert.equal(workspaceGroups(legacy).length, 2);
assert.equal(workspaceLeaves(legacy).length, 2);
assert.equal(legacy.right.open, true);
assert.equal(legacy.right.tab, 'outline');
assert.equal(workspaceLeaves(legacy)[0].mode, 'reading');
assert.equal(workspaceLeaves(legacy)[0].pinned, true);
assert.equal(legacy.settings.previewLayout, 'inline');
assert.equal(normalizeNotesWorkspace({ version:999, root:{ type:'group', tabs:[] }, settings:{ previewLayout:'side-by-side' } }, docs, 'a').settings.previewLayout, 'inline');
const cyclic = { type:'split', id:'cycle', children:[], sizes:[] }; cyclic.children.push(cyclic);
assert.equal(workspaceLeaves(normalizeNotesWorkspace({ version:NOTES_WORKSPACE_VERSION, root:cyclic }, docs, 'a')).length, 1);

const restored = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(legacy)), docs);
assert.equal(workspaceGroups(restored).length, 2);
assert.equal(workspaceLeaves(restored).length, 2);
const routed = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(restored)), docs, 'd');
assert.equal(findWorkspaceLeaf(routed)?.docId, 'd');

const group = workspaceGroups(clean)[0];
const opened = openWorkspaceDocument(clean, docs[1], { groupId:group.id });
assert.equal(opened.view, 'canvas');
assert.equal(group.tabs.length, 2);
assert(activateWorkspaceLeaf(clean, group.id, opened.id));
const split = splitWorkspaceGroup(clean, group.id, docs[2], 'vertical');
assert.equal(split.view, 'base');
assert.equal(workspaceGroups(clean).length, 2);
assert(resizeWorkspaceSplit(clean, clean.root.id, 63));
assert.deepEqual(clean.root.sizes.map(Math.round), [63, 37]);
const secondGroup = workspaceGroups(clean)[1];
assert(moveWorkspaceLeaf(clean, opened.id, secondGroup.id, 0));
assert.equal(secondGroup.tabs[0].id, opened.id);
opened.pinned = true;
assert.equal(closeWorkspaceLeaf(clean, opened.id), null);
opened.pinned = false;
assert.equal(closeWorkspaceLeaf(clean, opened.id)?.id, opened.id);
const keep = findWorkspaceLeaf(clean);
openWorkspaceDocument(clean, docs[1], { groupId:groupFor(clean, keep).id, reuse:false });
assert(closeWorkspaceOtherLeaves(clean, keep.id).length >= 1);
const disposableGroup = workspaceGroups(clean).find((item) => item.id !== groupFor(clean, keep).id);
if (disposableGroup) { const count = disposableGroup.tabs.filter((leaf) => !leaf.pinned).length; assert.equal(closeWorkspaceGroup(clean, disposableGroup.id).length, count); }

assert.equal(noteViewType(docs[0]), 'markdown');
assert.equal(noteViewType(docs[1]), 'canvas');
assert.equal(noteViewType(docs[2]), 'base');
assert.equal(noteViewType(docs[3]), 'image');
assert.equal(noteViewType(docs[4]), 'note');
assert.equal(noteViewType(docs[5]), 'timeline');
const timelineWorkspace = normalizeNotesWorkspace({}, docs, 't');
assert.equal(findWorkspaceLeaf(timelineWorkspace)?.view, 'timeline');
assert.equal(findWorkspaceLeaf(normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(timelineWorkspace)), docs, 't'))?.docId, 't');
assert.equal(openWorkspaceDocument(timelineWorkspace, docs[5]).id, findWorkspaceLeaf(timelineWorkspace).id);
const nativeWorkspace = normalizeNotesWorkspace({ ...clean, bookmarks:['n'], root:clean.root }, docs, 'n');
assert.deepEqual(nativeWorkspace.bookmarks, ['n']);
assert.equal(findWorkspaceLeaf(nativeWorkspace)?.view, 'note');
findWorkspaceLeaf(nativeWorkspace).mode = 'source';
assert.equal(findWorkspaceLeaf(normalizeNotesWorkspace(nativeWorkspace, docs, 'n'))?.mode, 'live');

const frontmatter = '---\ntitle: Alpha\ncount: 2\ndone: false\ntags: ["one", "two"]\n---\n# Alpha\n';
const parsed = parseFrontmatter(frontmatter);
assert.equal(parsed.valid, true);
assert.equal(parsed.entries.find((entry) => entry.key === 'count').value, 2);
assert.equal(parsed.entries.find((entry) => entry.key === 'done').type, 'checkbox');
assert.deepEqual(parsed.entries.find((entry) => entry.key === 'tags').value, ['one', 'two']);
const changed = setFrontmatterProperty(frontmatter, 'count', 7, 'number');
assert.match(changed, /count: 7/);
const added = setFrontmatterProperty(changed, 'due', '2026-07-11', 'date');
assert.match(added, /due: 2026-07-11\n---/);
assert.doesNotMatch(removeFrontmatterProperty(added, 'done'), /^done:/m);
assert.throws(() => setFrontmatterProperty('---\nno close', 'x', 'y'), /Unterminated/);
const renamed = renameFrontmatterProperty(frontmatter, 'count', 'score');
assert.match(renamed, /^score: 2$/m);
assert.match(moveFrontmatterProperty(renamed, 'score', -1), /^score: 2\ntitle: Alpha$/m);
assert.throws(() => renameFrontmatterProperty(frontmatter, 'count', 'title'), /already exists/);
assert.equal(coercePropertyValue('7', 'number'), 7);
assert.deepEqual(coercePropertyValue('#one, two', 'tags'), ['one', 'two']);
assert.throws(() => coercePropertyValue('{bad json', 'object'), SyntaxError);

const headingText = '# One\na\n## Child\nb\n# Two\nc\n# Three\nd';
assert.deepEqual(outlineEntries(headingText).map((entry) => [entry.line, entry.level, entry.text]), [
  [1, 1, 'One'], [3, 2, 'Child'], [5, 1, 'Two'], [7, 1, 'Three'],
]);
assert.equal(moveHeadingSection(headingText, 5, -1), '# Two\nc\n# One\na\n## Child\nb\n# Three\nd');
assert.equal(moveHeadingSection(headingText, 5, 1), '# One\na\n## Child\nb\n# Three\nd\n# Two\nc');
assert.equal(moveHeadingSectionTo(headingText, 1, 7), '# Two\nc\n# Three\nd\n# One\na\n## Child\nb');

// ── Slice 10: reparentHeading ──
const reparentSrc = '# A\ncontent\n## B\ncontent\n### C\ncontent\n# D\ncontent';
assert.equal(reparentHeading(reparentSrc, 3, 1), '# A\ncontent\n# B\ncontent\n### C\ncontent\n# D\ncontent');
assert.equal(reparentHeading(reparentSrc, 3, 3), '# A\ncontent\n### B\ncontent\n### C\ncontent\n# D\ncontent');
assert.equal(reparentHeading(reparentSrc, 5, 1), '# A\ncontent\n## B\ncontent\n# C\ncontent\n# D\ncontent');
assert.equal(reparentHeading(reparentSrc, 99, 2), reparentSrc); // invalid line, no-op
assert.equal(reparentHeading(reparentSrc, 3, 9), '# A\ncontent\n###### B\ncontent\n### C\ncontent\n# D\ncontent'); // clamps to 6

// ── Slice 10: outlineTree + flattenTree ──
const treeEntries = outlineEntries(headingText);
const tree = outlineTree(treeEntries);
assert.equal(tree.length, 3, 'tree has 3 root nodes');
assert.equal(tree[0].text, 'One');
assert.equal(tree[0].children.length, 1);
assert.equal(tree[0].children[0].text, 'Child');
assert.equal(tree[1].text, 'Two');
assert.equal(tree[1].children.length, 0);
assert.equal(tree[2].text, 'Three');
const flat = flattenTree(tree);
assert.equal(flat.length, 4, 'flattenTree returns all 4 nodes');
assert.deepEqual(flat.map((n) => n.text), ['One', 'Child', 'Two', 'Three']);

// Empty outline
assert.deepEqual(outlineTree([]), []);

// Deep hierarchy
const deepText = '# A\n## B\n### C\n#### D\n##### E\n###### F';
const deepTree = outlineTree(outlineEntries(deepText));
assert.equal(deepTree[0].children[0].children[0].children[0].children[0].children[0].text, 'F');
assert.equal(flattenTree(deepTree).length, 6);

const canvas = parseCanvasDocument(JSON.stringify({
  nodes:[{ id:'n', text:'Hello', x:10, y:20 }], edges:[{ fromNode:'n', toNode:'n' }],
}));
assert.equal(canvas.valid, true);
assert.equal(canvas.nodes[0].label, 'Hello');
assert.equal(parseCanvasDocument('{').valid, false);

const mentions = unlinkedMentions([
  docs[0],
  { id:'e', name:'Other.md', text:'Alpha is mentioned here without a link.' },
  { id:'f', name:'Linked.md', text:'[[Alpha]] is linked.' },
  { id:'g', name:'Prefix.md', text:'Alphabet soup is not the note name.' },
], docs[0]);
assert.deepEqual(mentions.map((item) => item.doc.id), ['e']);
const aliased = { ...docs[0], frontmatter:{ aliases:['A-note'] } };
assert.equal(resolveDocumentLink([aliased], 'A-note#Heading')?.id, 'a');
assert.deepEqual(linkedMentions([{ id:'x', name:'Linker.md', links:['A-note'], text:'See [[A-note]].' }], aliased).map((item) => item.doc.id), ['x']);
assert.deepEqual(databaseRelations('See [[A-note#Proof]] and ![[Other]].', [aliased]), [
  { kind:'link', target:'A-note', targetDocumentId:'a', fragment:'Proof' },
  { kind:'embed', target:'Other', targetDocumentId:null },
]);
assert(fuzzyScore('Projects/Alpha.md', 'pam') > 0);
assert.equal(fuzzyScore('Projects/Alpha.md', 'zzz'), -1);

// ── Slice 05: final-close, empty-workspace persistence, no fabricated tabs ──
const emptying = normalizeNotesWorkspace({}, docs, 'a');
const lastLeaf = findWorkspaceLeaf(emptying);
assert.equal(closeWorkspaceLeaf(emptying, lastLeaf.id)?.id, lastLeaf.id);
assert.equal(workspaceLeaves(emptying).length, 0);
assert.equal(emptying.activeLeafId, null);
assert.equal(emptying.root.type, 'group');
assert.deepEqual(emptying.closed, ['a']);

// An empty v2 workspace round-trips with no selected id and stays empty.
const emptyRestored = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(emptying)), docs, null);
assert.equal(workspaceLeaves(emptyRestored).length, 0);
assert.equal(emptyRestored.activeLeafId, null);
assert.deepEqual(emptyRestored.closed, ['a']);

// An explicit selection still opens into the empty workspace on request.
const requestedIntoEmpty = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(emptying)), docs, 'b');
assert.equal(findWorkspaceLeaf(requestedIntoEmpty)?.docId, 'b');

// A corrupt v2 root yields an empty workspace, not a fabricated tab.
const corrupt = normalizeNotesWorkspace({ version:NOTES_WORKSPACE_VERSION, root:{ type:'bogus' } }, docs, null);
assert.equal(workspaceLeaves(corrupt).length, 0);
assert.equal(corrupt.root.type, 'group');
assert.equal(corrupt.activeLeafId, null);

// Closing into a split prunes to the surviving group, never an invalid tree.
const splitting = normalizeNotesWorkspace({}, docs, 'a');
const baseGroup = workspaceGroups(splitting)[0];
const other = splitWorkspaceGroup(splitting, baseGroup.id, docs[1], 'horizontal');
assert.equal(closeWorkspaceLeaf(splitting, other.id)?.id, other.id);
assert.equal(splitting.root.type, 'group');
assert.equal(workspaceLeaves(splitting).length, 1);
assert.equal(findWorkspaceLeaf(splitting)?.docId, 'a');

// Reopening an already-open document into its group reuses the leaf.
const reopening = normalizeNotesWorkspace({}, docs, 'a');
const reopenGroup = workspaceGroups(reopening)[0];
const existingLeaf = findWorkspaceLeaf(reopening);
assert.equal(openWorkspaceDocument(reopening, docs[0], { groupId:reopenGroup.id }).id, existingLeaf.id);
assert.equal(reopenGroup.tabs.length, 1);

// ── Slice 06: panel registry, placement, keyboard nav ──
const panelWs = normalizeNotesWorkspace({}, docs, 'a');
assert.ok(panelWs.panels, 'workspace has panels map');
assert.equal(panelWs.panels.files.side, 'left');
assert.equal(panelWs.panels.search.side, 'left');
assert.equal(panelWs.panels.tags.side, 'left');
assert.equal(panelWs.panels.outline.side, 'right');
assert.equal(panelWs.panels.properties.side, 'right');
assert.deepEqual(workspacePanelsForSide(panelWs, 'left'), ['files', 'search', 'tags', 'bookmarks', 'recent']);
assert.deepEqual(workspacePanelsForSide(panelWs, 'right'), ['properties', 'links', 'outline']);

// Move tags to the right side
assert(setWorkspacePanelPlacement(panelWs, 'tags', { side:'right' }));
assert.deepEqual(workspacePanelsForSide(panelWs, 'left'), ['files', 'search', 'bookmarks', 'recent']);
// tags and outline both have default order 2; alphabetical tie-break: outline, tags
assert.deepEqual(workspacePanelsForSide(panelWs, 'right'), ['properties', 'links', 'outline', 'tags']);

// Hide a panel
assert(setWorkspacePanelPlacement(panelWs, 'bookmarks', { hidden:true }));
assert.deepEqual(workspacePanelsForSide(panelWs, 'left'), ['files', 'search', 'recent']);

// Invalid panel id rejected
assert.equal(setWorkspacePanelPlacement(panelWs, 'nonexistent', { side:'left' }), false);

// Invalid side for locked panel rejected (files can only be left)
assert(setWorkspacePanelPlacement(panelWs, 'files', { side:'right' }));
assert.equal(panelWs.panels.files.side, 'left');

// Panels round-trip through serialize/deserialize
const panelRestored = normalizeNotesWorkspace(JSON.parse(serializeNotesWorkspace(panelWs)), docs);
assert.deepEqual(workspacePanelsForSide(panelRestored, 'left'), ['files', 'search', 'recent']);
assert.equal(panelRestored.panels.tags.side, 'right');

// Legacy v2 workspace without panels gets default panels
const legacyPanels = normalizeNotesWorkspace({ tabs:['a'] }, docs);
assert.ok(legacyPanels.panels);
assert.deepEqual(workspacePanelsForSide(legacyPanels, 'left'), ['files', 'search', 'tags', 'bookmarks', 'recent']);

// Moving outline to the left
assert(setWorkspacePanelPlacement(panelWs, 'outline', { side:'left' }));
const leftPanels = workspacePanelsForSide(panelWs, 'left');
assert.ok(leftPanels.includes('outline'));
assert.ok(!workspacePanelsForSide(panelWs, 'right').includes('outline'));

console.log('copal notes workspace tests passed');

function groupFor(workspace, leaf) {
  return workspaceGroups(workspace).find((candidate) => candidate.tabs.some((item) => item.id === leaf.id));
}
