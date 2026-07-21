export const NOTES_WORKSPACE_VERSION = 2;

const MODES = new Set(['live', 'source', 'reading']);
const PREVIEW_LAYOUTS = new Set(['inline', 'side-by-side']);
const LEFT_TABS = new Set(['files', 'search', 'tags', 'bookmarks', 'recent']);
const RIGHT_TABS = new Set(['properties', 'links', 'outline']);
const FILE_SORTS = new Set(['name', 'modified']);

// Panel registry: id → { label, allowedSides, defaultSide, defaultOrder }
// Files and Search are left-locked (drag/drop and search-focus semantics);
// others are movable between sides.
const PANELS = Object.freeze({
  files:      { label:'Files',      allowedSides:['left'],             defaultSide:'left',  defaultOrder:0 },
  search:     { label:'Search',     allowedSides:['left'],             defaultSide:'left',  defaultOrder:1 },
  tags:       { label:'Tags',       allowedSides:['left','right'],     defaultSide:'left',  defaultOrder:2 },
  bookmarks:  { label:'Bookmarks',  allowedSides:['left','right'],     defaultSide:'left',  defaultOrder:3 },
  recent:     { label:'Recent',     allowedSides:['left','right'],     defaultSide:'left',  defaultOrder:4 },
  properties: { label:'Properties', allowedSides:['left','right'],     defaultSide:'right', defaultOrder:0 },
  links:      { label:'Links',      allowedSides:['left','right'],     defaultSide:'right', defaultOrder:1 },
  outline:    { label:'Outline',    allowedSides:['left','right'],     defaultSide:'right', defaultOrder:2 },
});
export const NOTES_PANELS = PANELS;

const PANEL_IDS = new Set(Object.keys(PANELS));

function defaultPanels() {
  const panels = {};
  for (const [id, def] of Object.entries(PANELS)) {
    panels[id] = { side:def.defaultSide, order:def.defaultOrder, hidden:false };
  }
  return panels;
}
let idSequence = 0;

function nextId(prefix) {
  idSequence += 1;
  return `${prefix}-${Date.now().toString(36)}-${idSequence.toString(36)}`;
}

function uniqueStrings(values) {
  return [...new Set((Array.isArray(values) ? values : []).filter((value) => typeof value === 'string' && value))];
}

function safeNumber(value, fallback, minimum, maximum) {
  const number = Number(value);
  return Number.isFinite(number) ? Math.min(maximum, Math.max(minimum, number)) : fallback;
}

export function normalizeNotesSettings(raw = {}) {
  const settings = raw && typeof raw === 'object' ? raw : {};
  return {
    previewLayout:PREVIEW_LAYOUTS.has(settings.previewLayout) ? settings.previewLayout : 'inline',
    lineNumbers:settings.lineNumbers === true,
    readableLineWidth:settings.readableLineWidth !== false,
    ribbon:settings.ribbon === true,
    completedVisibility:settings.completedVisibility === 'hide' ? 'hide' : 'show',
  };
}

export function noteViewType(doc = {}) {
  const kind = String(doc.kind || '').toLowerCase();
  const name = String(doc.name || '').toLowerCase();
  if (kind === 'timeline') return 'timeline';
  if (kind === 'note') return 'note';
  if (kind === 'copal-event') return 'event';
  if (kind === 'canvas' || name.endsWith('.canvas')) return 'canvas';
  if (kind === 'base' || name.endsWith('.base')) return 'base';
  if (kind === 'asset') {
    if (/\.(png|jpe?g|gif|webp|svg|avif)$/i.test(name)) return 'image';
    if (/\.(mp3|wav|ogg|m4a|flac)$/i.test(name)) return 'audio';
    if (/\.(mp4|webm|ogv|mov)$/i.test(name)) return 'video';
    if (/\.pdf$/i.test(name)) return 'pdf';
    return 'asset';
  }
  return 'markdown';
}

function makeLeaf(doc, source = {}) {
  const view = noteViewType(doc);
  return {
    type: 'leaf',
    id: typeof source.id === 'string' && source.id ? source.id : nextId('leaf'),
    docId: doc.id,
    view,
    mode:view === 'note' && source.mode === 'source' ? 'live' : MODES.has(source.mode) ? source.mode : 'live',
    pinned: source.pinned === true,
    rawSource: source.rawSource === true,
    selection: source.selection && typeof source.selection === 'object'
      ? {
          anchor: safeNumber(source.selection.anchor, 0, 0, Number.MAX_SAFE_INTEGER),
          head: safeNumber(source.selection.head, 0, 0, Number.MAX_SAFE_INTEGER),
        }
      : null,
    scrollTop: safeNumber(source.scrollTop, 0, 0, Number.MAX_SAFE_INTEGER),
  };
}

function makeGroup(leaves = [], source = {}) {
  const tabs = leaves.filter(Boolean);
  const active = tabs.some((leaf) => leaf.id === source.activeLeafId)
    ? source.activeLeafId
    : tabs[0]?.id || null;
  return {
    type: 'group',
    id: typeof source.id === 'string' && source.id ? source.id : nextId('group'),
    tabs,
    activeLeafId: active,
  };
}

function normalizeLeaf(raw, docsById, usedLeafIds) {
  if (!raw || raw.type !== 'leaf' || typeof raw.docId !== 'string') return null;
  const doc = docsById.get(raw.docId);
  if (!doc) return null;
  const leaf = makeLeaf(doc, raw);
  if (usedLeafIds.has(leaf.id)) leaf.id = nextId('leaf');
  usedLeafIds.add(leaf.id);
  return leaf;
}

function normalizeNode(raw, docsById, usedNodeIds, usedLeafIds, depth = 0, ancestors = new Set()) {
  if (!raw || typeof raw !== 'object' || depth > 16 || ancestors.has(raw)) return null;
  const nextAncestors = new Set(ancestors);
  nextAncestors.add(raw);
  if (raw.type === 'group') {
    const tabs = (Array.isArray(raw.tabs) ? raw.tabs : [])
      .map((leaf) => normalizeLeaf(leaf, docsById, usedLeafIds))
      .filter(Boolean);
    const group = makeGroup(tabs, raw);
    if (usedNodeIds.has(group.id)) group.id = nextId('group');
    usedNodeIds.add(group.id);
    return group;
  }
  if (raw.type !== 'split') return null;
  const children = (Array.isArray(raw.children) ? raw.children : [])
    .map((child) => normalizeNode(child, docsById, usedNodeIds, usedLeafIds, depth + 1, nextAncestors))
    .filter(Boolean);
  if (!children.length) return null;
  if (children.length === 1) return children[0];
  let id = typeof raw.id === 'string' && raw.id ? raw.id : nextId('split');
  if (usedNodeIds.has(id)) id = nextId('split');
  usedNodeIds.add(id);
  const sourceSizes = Array.isArray(raw.sizes) ? raw.sizes : [];
  const sizes = children.map((_, index) => safeNumber(sourceSizes[index], 100 / children.length, 8, 92));
  const total = sizes.reduce((sum, value) => sum + value, 0) || 1;
  return {
    type: 'split',
    id,
    orientation: raw.orientation === 'vertical' ? 'vertical' : 'horizontal',
    children,
    sizes: sizes.map((value) => (value / total) * 100),
  };
}

function flattenLeaves(node, output = []) {
  if (!node) return output;
  if (node.type === 'group') output.push(...node.tabs);
  else for (const child of node.children || []) flattenLeaves(child, output);
  return output;
}

function migrateLegacy(raw, docsById, fallbackDoc) {
  const ids = uniqueStrings(raw?.tabs).filter((id) => docsById.has(id));
  if (typeof raw?.selected === 'string' && docsById.has(raw.selected) && !ids.includes(raw.selected)) ids.push(raw.selected);
  if (!ids.length && fallbackDoc) ids.push(fallbackDoc.id);
  const pinned = new Set(uniqueStrings(raw?.pinned));
  const cursors = raw?.cursors && typeof raw.cursors === 'object' ? raw.cursors : {};
  const mode = MODES.has(raw?.mode) ? raw.mode : 'live';
  const leaves = ids.map((id) => makeLeaf(docsById.get(id), { mode, pinned:pinned.has(id), selection:cursors[id] }));
  const main = makeGroup(leaves, { activeLeafId:leaves.find((leaf) => leaf.docId === raw?.selected)?.id });
  let root = main;
  if (typeof raw?.split === 'string' && docsById.has(raw.split)) {
    const splitLeaf = makeLeaf(docsById.get(raw.split), { mode, selection:cursors[`${raw.split}:split`] });
    root = { type:'split', id:nextId('split'), orientation:'horizontal', children:[main, makeGroup([splitLeaf])], sizes:[50, 50] };
  }
  const activeLeaf = flattenLeaves(root).find((leaf) => leaf.docId === raw?.selected) || flattenLeaves(root)[0] || null;
  return {
    version: NOTES_WORKSPACE_VERSION,
    root,
    activeLeafId: activeLeaf?.id || null,
    left: { open:raw?.explorerOpen !== false, width:224, tab:'files', sort:'name', expanded:uniqueStrings(raw?.expanded), selected:[], showDotFolders:raw?.showDotFolders === true },
    right: {
      open:raw?.sidebarOpen === true,
      width:280,
      tab:RIGHT_TABS.has(raw?.sidebar) ? raw.sidebar : 'properties',
      pinnedDocId:null,
    },
    panels:defaultPanels(),
    settings:normalizeNotesSettings(),
    bookmarks:uniqueStrings(raw?.bookmarks || raw?.pinned).filter((id) => docsById.has(id)),
    recent:ids.slice().reverse(),
    closed:[],
  };
}

export function normalizeNotesWorkspace(raw, docs = [], selected = null) {
  const visibleDocs = docs.filter((doc) => doc && typeof doc.id === 'string');
  const docsById = new Map(visibleDocs.map((doc) => [doc.id, doc]));
  const fallbackDoc = docsById.get(selected) || visibleDocs[0] || null;
  if (!raw || raw.version !== NOTES_WORKSPACE_VERSION || !raw.root) return migrateLegacy(raw || {}, docsById, fallbackDoc);

  let root = normalizeNode(raw.root, docsById, new Set(), new Set()) || makeGroup();
  let leaves = flattenLeaves(root);
  if (selected && docsById.has(selected) && !leaves.some((leaf) => leaf.docId === selected)) {
    const leaf = makeLeaf(docsById.get(selected));
    const group = (() => {
      const groups = [];
      const visit = (node) => { if (node.type === 'group') groups.push(node); else for (const child of node.children || []) visit(child); };
      visit(root);
      return groups[0] || null;
    })();
    if (group) { group.tabs.push(leaf); group.activeLeafId = leaf.id; }
    else root = makeGroup([leaf]);
    leaves = flattenLeaves(root, []);
  }
  const requested = leaves.find((leaf) => leaf.docId === selected);
  const active = requested?.id || (leaves.some((leaf) => leaf.id === raw.activeLeafId) ? raw.activeLeafId : leaves[0]?.id || null);
  if (requested) {
    const activateGroup = (node) => {
      if (node.type === 'group') {
        if (node.tabs.some((leaf) => leaf.id === requested.id)) node.activeLeafId = requested.id;
        return;
      }
      for (const child of node.children || []) activateGroup(child);
    };
    activateGroup(root);
  }
  const left = raw.left && typeof raw.left === 'object' ? raw.left : {};
  const right = raw.right && typeof raw.right === 'object' ? raw.right : {};
  const settings = normalizeNotesSettings(raw.settings);
  const panels = normalizePanels(raw.panels, left, right);
  return {
    version: NOTES_WORKSPACE_VERSION,
    root,
    activeLeafId:active,
    left: {
      open:left.open !== false,
      width:safeNumber(left.width, 224, 150, 420),
      tab:LEFT_TABS.has(left.tab) ? left.tab : 'files',
      sort:FILE_SORTS.has(left.sort) ? left.sort : 'name',
      expanded:uniqueStrings(left.expanded),
      selected:uniqueStrings(left.selected).filter((id) => docsById.has(id)),
      showDotFolders:left.showDotFolders === true,
    },
    right: {
      open:right.open === true,
      width:safeNumber(right.width, 280, 190, 480),
      tab:RIGHT_TABS.has(right.tab) ? right.tab : 'properties',
      pinnedDocId:typeof right.pinnedDocId === 'string' && docsById.has(right.pinnedDocId) ? right.pinnedDocId : null,
    },
    panels,
    settings,
    bookmarks:uniqueStrings(raw.bookmarks).filter((id) => docsById.has(id)),
    recent:uniqueStrings(raw.recent).filter((id) => docsById.has(id)).slice(0, 40),
    closed:uniqueStrings(raw.closed).filter((id) => docsById.has(id)).slice(0, 20),
  };
}

export function workspaceGroups(workspace) {
  const groups = [];
  const visit = (node) => {
    if (!node) return;
    if (node.type === 'group') groups.push(node);
    else for (const child of node.children || []) visit(child);
  };
  visit(workspace.root);
  return groups;
}

export function workspaceLeaves(workspace) {
  return flattenLeaves(workspace.root, []);
}

export function findWorkspaceLeaf(workspace, leafId = workspace.activeLeafId) {
  return workspaceLeaves(workspace).find((leaf) => leaf.id === leafId) || null;
}

export function findWorkspaceGroup(workspace, groupId) {
  return workspaceGroups(workspace).find((group) => group.id === groupId) || null;
}

export function groupForLeaf(workspace, leafId) {
  return workspaceGroups(workspace).find((group) => group.tabs.some((leaf) => leaf.id === leafId)) || null;
}

export function activateWorkspaceLeaf(workspace, groupId, leafId) {
  const group = findWorkspaceGroup(workspace, groupId);
  if (!group?.tabs.some((leaf) => leaf.id === leafId)) return false;
  group.activeLeafId = leafId;
  workspace.activeLeafId = leafId;
  const leaf = findWorkspaceLeaf(workspace, leafId);
  if (leaf) workspace.recent = [leaf.docId, ...workspace.recent.filter((id) => id !== leaf.docId)].slice(0, 40);
  return true;
}

export function openWorkspaceDocument(workspace, doc, options = {}) {
  let group = findWorkspaceGroup(workspace, options.groupId);
  if (!group) group = groupForLeaf(workspace, workspace.activeLeafId) || workspaceGroups(workspace)[0];
  if (!group) {
    group = makeGroup();
    workspace.root = group;
  }
  let leaf = options.reuse === false ? null : group.tabs.find((item) => item.docId === doc.id);
  if (!leaf) {
    leaf = makeLeaf(doc, { mode:options.mode });
    group.tabs.push(leaf);
  }
  activateWorkspaceLeaf(workspace, group.id, leaf.id);
  return leaf;
}

function replaceNode(node, wantedId, replacement) {
  if (node.id === wantedId) return replacement;
  if (node.type !== 'split') return node;
  node.children = node.children.map((child) => replaceNode(child, wantedId, replacement));
  return node;
}

export function splitWorkspaceGroup(workspace, groupId, doc, orientation = 'horizontal') {
  const group = findWorkspaceGroup(workspace, groupId);
  if (!group) return null;
  const leaf = makeLeaf(doc);
  const sibling = makeGroup([leaf]);
  workspace.root = replaceNode(workspace.root, group.id, {
    type:'split', id:nextId('split'), orientation:orientation === 'vertical' ? 'vertical' : 'horizontal',
    children:[group, sibling], sizes:[50, 50],
  });
  workspace.activeLeafId = leaf.id;
  workspace.recent = [doc.id, ...workspace.recent.filter((id) => id !== doc.id)].slice(0, 40);
  return leaf;
}

function pruneNode(node) {
  if (node.type !== 'split') return node;
  const entries = node.children
    .map((child, index) => ({ child:pruneNode(child), size:Number(node.sizes?.[index]) || 1 }))
    .filter(({ child }) => child.type !== 'group' || child.tabs.length > 0);
  if (!entries.length) return makeGroup();
  if (entries.length === 1) return entries[0].child;
  const total = entries.reduce((sum, entry) => sum + entry.size, 0) || entries.length;
  node.children = entries.map((entry) => entry.child);
  node.sizes = entries.map((entry) => (entry.size / total) * 100);
  return node;
}

export function closeWorkspaceLeaf(workspace, leafId) {
  const group = groupForLeaf(workspace, leafId);
  const leaf = group?.tabs.find((item) => item.id === leafId);
  if (!group || !leaf || leaf.pinned) return null;
  const index = group.tabs.indexOf(leaf);
  group.tabs.splice(index, 1);
  workspace.closed = [leaf.docId, ...workspace.closed.filter((id) => id !== leaf.docId)].slice(0, 20);
  if (group.activeLeafId === leafId) group.activeLeafId = group.tabs[Math.min(index, group.tabs.length - 1)]?.id || null;
  workspace.root = pruneNode(workspace.root);
  if (workspace.activeLeafId === leafId) workspace.activeLeafId = group.activeLeafId || workspaceLeaves(workspace)[0]?.id || null;
  return leaf;
}

export function closeWorkspaceOtherLeaves(workspace, leafId) {
  const group = groupForLeaf(workspace, leafId);
  if (!group) return [];
  const closed = [];
  for (const leaf of [...group.tabs]) {
    if (leaf.id === leafId || leaf.pinned) continue;
    const removed = closeWorkspaceLeaf(workspace, leaf.id);
    if (removed) closed.push(removed);
  }
  activateWorkspaceLeaf(workspace, group.id, leafId);
  return closed;
}

export function closeWorkspaceGroup(workspace, groupId) {
  const group = findWorkspaceGroup(workspace, groupId);
  if (!group) return [];
  const closed = [];
  for (const leaf of [...group.tabs]) {
    if (leaf.pinned) continue;
    const removed = closeWorkspaceLeaf(workspace, leaf.id);
    if (removed) closed.push(removed);
  }
  return closed;
}

export function moveWorkspaceLeaf(workspace, leafId, targetGroupId, targetIndex = null) {
  const source = groupForLeaf(workspace, leafId);
  const target = findWorkspaceGroup(workspace, targetGroupId);
  const leaf = source?.tabs.find((item) => item.id === leafId);
  if (!source || !target || !leaf) return false;
  source.tabs = source.tabs.filter((item) => item.id !== leafId);
  if (source.activeLeafId === leafId) source.activeLeafId = source.tabs[0]?.id || null;
  const index = targetIndex == null ? target.tabs.length : Math.max(0, Math.min(target.tabs.length, targetIndex));
  target.tabs.splice(index, 0, leaf);
  target.activeLeafId = leafId;
  workspace.activeLeafId = leafId;
  workspace.root = pruneNode(workspace.root);
  return true;
}

export function resizeWorkspaceSplit(workspace, splitId, firstSize) {
  const visit = (node) => {
    if (node.type !== 'split') return false;
    if (node.id === splitId && node.children.length === 2) {
      const bounded = safeNumber(firstSize, 50, 15, 85);
      node.sizes = [bounded, 100 - bounded];
      return true;
    }
    return node.children.some(visit);
  };
  return visit(workspace.root);
}

export function setWorkspaceLeafMode(workspace, leafId, mode) {
  const leaf = findWorkspaceLeaf(workspace, leafId);
  if (!leaf || !MODES.has(mode)) return false;
  leaf.mode = mode;
  return true;
}

export function serializeNotesWorkspace(workspace) {
  return JSON.stringify(workspace);
}

function normalizePanels(rawPanels, left, right) {
  const panels = defaultPanels();
  if (rawPanels && typeof rawPanels === 'object') {
    for (const [id, entry] of Object.entries(rawPanels)) {
      if (!PANEL_IDS.has(id) || !entry || typeof entry !== 'object') continue;
      const def = PANELS[id];
      const side = def.allowedSides.includes(entry.side) ? entry.side : def.defaultSide;
      panels[id] = {
        side,
        order: safeNumber(entry.order, def.defaultOrder, 0, 99),
        hidden: entry.hidden === true,
      };
    }
  }
  // Ensure no two panels share the same (side, order) — break ties by
  // stable panel-id sort so the result is deterministic.
  for (const side of ['left', 'right']) {
    const entries = Object.entries(panels).filter(([, p]) => p.side === side).sort(([a], [b]) => a.localeCompare(b));
    entries.sort(([, a], [, b]) => a.order - b.order);
    entries.forEach(([, p], index) => { p.order = index; });
  }
  return panels;
}

// Returns the ordered, visible panel ids for a given side.
export function workspacePanelsForSide(workspace, side) {
  const panels = workspace.panels || defaultPanels();
  return Object.entries(panels)
    .filter(([, p]) => p.side === side && !p.hidden)
    .sort(([, a], [, b]) => a.order - b.order)
    .map(([id]) => id);
}

// Validates and applies a placement patch for a panel.
export function setWorkspacePanelPlacement(workspace, id, patch) {
  if (!PANEL_IDS.has(id) || !workspace.panels) return false;
  const def = PANELS[id];
  const current = workspace.panels[id];
  if (patch && typeof patch === 'object') {
    if (typeof patch.side === 'string' && def.allowedSides.includes(patch.side)) current.side = patch.side;
    if (typeof patch.order === 'number') current.order = Math.max(0, Math.min(99, patch.order));
    if (typeof patch.hidden === 'boolean') current.hidden = patch.hidden;
  }
  // Re-normalize orders for the side to prevent collisions.
  for (const side of ['left', 'right']) {
    const entries = Object.entries(workspace.panels).filter(([, p]) => p.side === side).sort(([a], [b]) => a.localeCompare(b));
    entries.sort(([, a], [, b]) => a.order - b.order);
    entries.forEach(([, p], index) => { p.order = index; });
  }
  return true;
}
