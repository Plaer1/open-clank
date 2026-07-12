import assert from 'node:assert/strict';

import {
  NOTES_WORKSPACE_VERSION,
  activateWorkspaceLeaf,
  closeWorkspaceGroup,
  closeWorkspaceLeaf,
  closeWorkspaceOtherLeaves,
  findWorkspaceLeaf,
  moveWorkspaceLeaf,
  normalizeNotesWorkspace,
  noteViewType,
  openWorkspaceDocument,
  resizeWorkspaceSplit,
  serializeNotesWorkspace,
  splitWorkspaceGroup,
  workspaceGroups,
  workspaceLeaves,
} from '../../static/js/copal/notesWorkspace.js';
import {
  fuzzyScore,
  linkedMentions,
  moveHeadingSection,
  moveHeadingSectionTo,
  moveFrontmatterProperty,
  outlineEntries,
  parseCanvasDocument,
  parseFrontmatter,
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
];

const clean = normalizeNotesWorkspace({}, docs, 'a');
assert.equal(clean.version, NOTES_WORKSPACE_VERSION);
assert.equal(clean.settings.previewLayout, 'inline');
assert.equal(clean.settings.lineNumbers, false);
assert.equal(clean.right.open, false);
assert.equal(clean.left.sort, 'name');
assert.equal(findWorkspaceLeaf(clean)?.docId, 'a');

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

const headingText = '# One\na\n## Child\nb\n# Two\nc\n# Three\nd';
assert.deepEqual(outlineEntries(headingText).map((entry) => [entry.line, entry.level, entry.text]), [
  [1, 1, 'One'], [3, 2, 'Child'], [5, 1, 'Two'], [7, 1, 'Three'],
]);
assert.equal(moveHeadingSection(headingText, 5, -1), '# Two\nc\n# One\na\n## Child\nb\n# Three\nd');
assert.equal(moveHeadingSection(headingText, 5, 1), '# One\na\n## Child\nb\n# Three\nd\n# Two\nc');
assert.equal(moveHeadingSectionTo(headingText, 1, 7), '# Two\nc\n# Three\nd\n# One\na\n## Child\nb');

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
assert(fuzzyScore('Projects/Alpha.md', 'pam') > 0);
assert.equal(fuzzyScore('Projects/Alpha.md', 'zzz'), -1);

console.log('copal notes workspace tests passed');

function groupFor(workspace, leaf) {
  return workspaceGroups(workspace).find((candidate) => candidate.tabs.some((item) => item.id === leaf.id));
}
