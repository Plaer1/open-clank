import { normalizeImageRecovery } from '../editor/imageSession.js';

export const NOTES_WORKSPACE_VERSION = 3;
export const NOTES_SELECTION_VERSION = 1;

const UNKNOWN_WORKSPACE_STATE = Symbol('unknown-workspace-state');
const MODES = new Set(['live', 'source', 'reading']);
const PREVIEW_LAYOUTS = new Set(['inline', 'side-by-side']);
const FILE_SORTS = new Set(['name', 'modified']);

// Panel registry: id → { label, allowedSides, defaultSide, defaultOrder }
// Files and Search are left-locked (drag/drop and search-focus semantics);
// others are movable between sides.
const PANELS = Object.freeze({
  files:      { label:'Explorer',      allowedSides:['left'],             defaultSide:'left',  defaultOrder:0 },
  search:     { label:'Search',     allowedSides:['left'],             defaultSide:'left',  defaultOrder:1 },
  recent:     { label:'Recent',     allowedSides:['left','right'],     defaultSide:'left',  defaultOrder:4 },
  properties: { label:'Details', allowedSides:['right'],     defaultSide:'right', defaultOrder:0 },
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

function normalizeSelection(value, length) {
  const max = Math.max(0, Number.isFinite(Number(length)) ? Number(length) : Number.MAX_SAFE_INTEGER);
  const bounded = (candidate) => {
    const number = Number(candidate);
    return Number.isFinite(number) ? Math.min(max, Math.max(0, Math.trunc(number))) : 0;
  };
  const source = Array.isArray(value?.ranges) && value.ranges.length
    ? value.ranges
    : [{ anchor:value?.anchor, head:value?.head }];
  const requestedMain = Math.max(0, Math.min(source.length - 1, Math.trunc(Number(value?.mainIndex) || 0)));
  const indexed = source.map((range, index) => ({
    index, anchor:bounded(range?.anchor), head:bounded(range?.head),
  })).sort((a, b) => Math.min(a.anchor, a.head) - Math.min(b.anchor, b.head) || Math.max(a.anchor, a.head) - Math.max(b.anchor, b.head) || a.index - b.index);
  const ranges = [];
  let mainIndex = 0;
  for (const candidate of indexed) {
    const previous = ranges[ranges.length - 1];
    if (previous && Math.min(candidate.anchor, candidate.head) < Math.max(previous.anchor, previous.head)) continue;
    if (candidate.index === requestedMain) mainIndex = ranges.length;
    ranges.push({ anchor:candidate.anchor, head:candidate.head });
  }
  if (!ranges.length) ranges.push({ anchor:0, head:0 });
  if (mainIndex >= ranges.length) mainIndex = Math.min(ranges.length - 1, requestedMain);
  const main = ranges[mainIndex];
  return { version:NOTES_SELECTION_VERSION, ranges, mainIndex, anchor:main.anchor, head:main.head };
}

export function normalizeNoteSelection(value, length = Number.MAX_SAFE_INTEGER) {
  return normalizeSelection(value, length);
}

export function normalizeNotesSettings(raw = {}) {
  const settings = raw && typeof raw === 'object' ? raw : {};
  return {
    previewLayout:PREVIEW_LAYOUTS.has(settings.previewLayout) ? settings.previewLayout : 'inline',
    lineNumbers:settings.lineNumbers === true,
    readableLineWidth:settings.readableLineWidth !== false,
    ribbon:settings.ribbon === true,
    completedVisibility:settings.completedVisibility === 'hide' ? 'hide' : 'show',
    showTutorialFolder:settings.showTutorialFolder !== false,
    // Template preferences are ordinary workspace state. Keep paths and IDs
    // opaque strings; Files/host authorization remains provider-owned.
    templateFolder:typeof settings.templateFolder === 'string' ? settings.templateFolder.trim().replace(/\\/g, '/').replace(/^\/+|\/+$/g, '') : '',
    // The visible path is only a presentation hint.  A configured template
    // folder remains bound to this opaque Files-v1 identity and revision.
    ...(settings.templateFolderRef && typeof settings.templateFolderRef === 'object' ? { templateFolderRef:{
        resourceRef:String(settings.templateFolderRef.resourceRef || settings.templateFolderRef.ref || '').trim(),
        resourceKey:String(settings.templateFolderRef.resourceKey || '').trim(),
        provider:String(settings.templateFolderRef.provider || '').trim(),
        kind:String(settings.templateFolderRef.kind || 'folder').trim() || 'folder',
        logicalPath:String(settings.templateFolderRef.logicalPath || settings.templateFolderRef.logical_path || '').trim(),
        revision:settings.templateFolderRef.revision && typeof settings.templateFolderRef.revision === 'object'
          ? { kind:String(settings.templateFolderRef.revision.kind || ''), value:String(settings.templateFolderRef.revision.value || '') }
          : null,
        capabilities:Array.isArray(settings.templateFolderRef.capabilities)
          ? [...new Set(settings.templateFolderRef.capabilities.map(String))]
          : Object.fromEntries(Object.entries(settings.templateFolderRef.capabilities || {}).filter(([, value]) => value === true).map(([key]) => [key, true])),
        ...(settings.templateFolderRef.accountId || settings.templateFolderRef.account_id || settings.templateFolderRef.accountScope
          ? { accountId:String(settings.templateFolderRef.accountId || settings.templateFolderRef.account_id || settings.templateFolderRef.accountScope) } : {}),
        ...(settings.templateFolderRef.workspaceId || settings.templateFolderRef.workspace_id || settings.templateFolderRef.workspaceScope
          ? { workspaceId:String(settings.templateFolderRef.workspaceId || settings.templateFolderRef.workspace_id || settings.templateFolderRef.workspaceScope) } : {}),
        ...(settings.templateFolderRef.generation != null ? { generation:Number(settings.templateFolderRef.generation) } : {}),
        ...(settings.templateFolderRef.policyGeneration != null ? { policyGeneration:Number(settings.templateFolderRef.policyGeneration) } : {}),
      } } : {}),
    dailyTemplateId:typeof settings.dailyTemplateId === 'string' ? settings.dailyTemplateId.trim() : '',
  };
}

export function noteViewType(doc = {}) {
  const kind = String(doc.kind || '').toLowerCase();
  const name = String(doc.name || '').toLowerCase();
  if (kind === 'image-session' || kind === 'derived-iwi-preview') return 'image';
  if (kind === 'host-video') return 'video';
  if (kind === 'timeline') return 'timeline';
  if (kind === 'note') return 'note';
  if (kind === 'copal-event') return 'event';
  // Native Wiki articles keep a typed Editor view. A Wiki resource must never
  // fall through to the Markdown leaf merely because its body is readable text.
  if (kind === 'wiki' || kind === 'meme') return 'wiki';
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
  const selection = source.selection && typeof source.selection === 'object'
    ? normalizeSelection(source.selection, String(doc.text || '').length)
    : null;
  const leaf = {
    type: 'leaf',
    id: typeof source.id === 'string' && source.id ? source.id : nextId('leaf'),
    docId: doc.id,
    view,
    mode:view === 'note' && source.mode === 'source' ? 'live' : MODES.has(source.mode) ? source.mode : 'live',
    pinned: source.pinned === true,
    rawSource: source.rawSource === true,
    selection,
    scrollTop: safeNumber(source.scrollTop, 0, 0, Number.MAX_SAFE_INTEGER),
  };
  if (doc.kind === 'image-session' && doc.imageRecovery) {
    const recovery = normalizeImageRecovery(doc.imageRecovery);
    const saved = source.image && source.image.version === 1 && source.image.sessionId === recovery.sessionId ? source.image : {};
    leaf.image = { version:1, sessionId:recovery.sessionId,
      viewId:typeof saved.viewId === 'string' && saved.viewId.length > 0 && saved.viewId.length <= 256 && !/^data:/i.test(saved.viewId) && !/[\u0000-\u001f\u007f]/.test(saved.viewId) ? saved.viewId : nextId('image-view'),
      zoom:safeNumber(saved.zoom, 1, 0.01, 64), recovery };
    leaf.selection = null;
  }
  return leaf;
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

function normalizeLeaf(raw, docsById, usedLeafIds, usedViewIds) {
  if (!raw || raw.type !== 'leaf' || typeof raw.docId !== 'string') return null;
  const doc = docsById.get(raw.docId);
  if (!doc) return null;
  const leaf = makeLeaf(doc, raw);
  if (usedLeafIds.has(leaf.id)) leaf.id = nextId('leaf');
  usedLeafIds.add(leaf.id);
  if (leaf.image) {
    if (usedViewIds.has(leaf.image.viewId)) leaf.image.viewId = nextId('image-view');
    usedViewIds.add(leaf.image.viewId);
  }
  return leaf;
}

function normalizeNode(raw, docsById, usedNodeIds, usedLeafIds, usedViewIds, depth = 0, ancestors = new Set()) {
  if (!raw || typeof raw !== 'object' || depth > 16 || ancestors.has(raw)) return null;
  const nextAncestors = new Set(ancestors);
  nextAncestors.add(raw);
  if (raw.type === 'group') {
    const tabs = (Array.isArray(raw.tabs) ? raw.tabs : [])
      .map((leaf) => normalizeLeaf(leaf, docsById, usedLeafIds, usedViewIds))
      .filter(Boolean);
    const group = makeGroup(tabs, raw);
    if (usedNodeIds.has(group.id)) group.id = nextId('group');
    usedNodeIds.add(group.id);
    return group;
  }
  if (raw.type !== 'split') return null;
  const children = (Array.isArray(raw.children) ? raw.children : [])
    .map((child) => normalizeNode(child, docsById, usedNodeIds, usedLeafIds, usedViewIds, depth + 1, nextAncestors))
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

export function clearExplorerBinding(left) {
  Object.assign(left,{
    resourceRoot:null, folderWorkspaceRoot:null, folderWorkspaceId:'', folderWorkspaceExplicit:false,
    copalWorkspacePinned:false, copalFolderRoot:[], binderContext:null,
    resourceRows:[], resourceCursor:null, resourceQuery:'', selected:[],
  });
}

function createEmptyWorkspace(fallbackDoc) {
  const leaf = fallbackDoc ? makeLeaf(fallbackDoc) : null;
  const root = makeGroup(leaf ? [leaf] : []);
  return {
    version:NOTES_WORKSPACE_VERSION, root, activeLeafId:leaf?.id || null,
    left:{ open:true, width:224, tab:'files', sort:'name', expanded:[], selected:[], showDotFolders:false, resourceRoot:null, folderWorkspaceRoot:null, folderWorkspaceId:'', folderWorkspaceExplicit:false, copalWorkspacePinned:false, copalFolderRoot:[], binderContext:null, resourceRows:[], resourceCursor:null, resourceQuery:'' },
    right:{ open:false, width:280, tab:'properties', pinnedDocId:null },
    panels:defaultPanels(), settings:normalizeNotesSettings(), bookmarks:[], recent:leaf ? [leaf.docId] : [], closed:[], closedImageRecovery:{},
  };
}

export function normalizeNotesWorkspace(raw, docs = [], selected = null) {
  const preserved = raw?.[UNKNOWN_WORKSPACE_STATE];
  const visibleDocs = docs.filter((doc) => doc && typeof doc.id === 'string');
  const docsById = new Map(visibleDocs.map((doc) => [doc.id, doc]));
  const fallbackDoc = docsById.get(selected) || visibleDocs[0] || null;
  if (!raw || raw.version !== NOTES_WORKSPACE_VERSION || !raw.root) {
    const fallback = createEmptyWorkspace(fallbackDoc);
    if (raw && Number.isSafeInteger(raw.version) && raw.version > NOTES_WORKSPACE_VERSION) {
      try { Object.defineProperty(fallback, UNKNOWN_WORKSPACE_STATE, { value:preserved || JSON.stringify(raw), configurable:true }); } catch (_) {}
    }
    return fallback;
  }
  // Image leaves are recoverable without inventing a text document or storing pixels.
  let hasFutureImage = false;
  const seen = new Set();
  const collect = (node, depth = 0) => {
    if (!node || typeof node !== 'object' || depth > 16 || seen.has(node)) return;
    seen.add(node);
    if (node.type === 'group') for (const leaf of Array.isArray(node.tabs) ? node.tabs : []) {
      if (leaf?.image?.version > 1 || leaf?.image?.recovery?.version > 1) { hasFutureImage = true; continue; }
      try {
        const recovery = normalizeImageRecovery(leaf?.image?.recovery);
        if (leaf.image.version === 1 && leaf.image.sessionId === recovery.sessionId && leaf.docId === `image-session:${recovery.sessionId}`) {
          docsById.set(leaf.docId, { id:leaf.docId, kind:'image-session', imageRecovery:recovery });
        }
      } catch (_) { if (leaf?.image) hasFutureImage=true; }
    }
    if (node.type === 'split') for (const child of Array.isArray(node.children) ? node.children : []) collect(child, depth + 1);
  };
  collect(raw.root);
  const closedImageRecovery={};
  const rawRecoveries=raw.closedImageRecovery;
  if (rawRecoveries != null && (typeof rawRecoveries !== 'object' || Array.isArray(rawRecoveries))) hasFutureImage=true;
  if (rawRecoveries && typeof rawRecoveries==='object' && !Array.isArray(rawRecoveries)) {
    for (const [id,value] of Object.entries(rawRecoveries)) {
      if (!id.startsWith('image-session:')) { hasFutureImage=true; continue; }
      try {
        const recovery=normalizeImageRecovery(value);
        if (id!==`image-session:${recovery.sessionId}`) throw new Error('invalid recovery');
        closedImageRecovery[id]=recovery;
        if (!docsById.has(id)) docsById.set(id,{id,kind:'image-session',imageRecovery:recovery});
      } catch (_) {
        hasFutureImage=true;
        closedImageRecovery[id]={unavailable:true};
        if (!docsById.has(id)) docsById.set(id,{id,kind:'image-session',imageRecoveryUnavailable:true});
      }
    }
  }
  let root = pruneNode(normalizeNode(raw.root, docsById, new Set(), new Set(), new Set()) || makeGroup());
  let leaves = flattenLeaves(root);
  if (selected && !Object.hasOwn(closedImageRecovery,selected) && docsById.has(selected) && !leaves.some((leaf) => leaf.docId === selected)) {
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
  const visibleOn = (side) => Object.entries(panels).filter(([, item]) => item.side === side && !item.hidden).sort(([, a], [, b]) => a.order - b.order).map(([id]) => id);
  const leftVisible = visibleOn('left');
  const rightVisible = visibleOn('right');
  const workspace = {
    version: NOTES_WORKSPACE_VERSION,
    root,
    activeLeafId:active,
    left: {
      open:left.open !== false,
      width:safeNumber(left.width, 224, 150, 420),
      tab:leftVisible.includes(left.tab) ? left.tab : 'files',
      sort:FILE_SORTS.has(left.sort) ? left.sort : 'name',
      expanded:uniqueStrings(left.expanded),
      selected:uniqueStrings(left.selected).filter((id) => docsById.has(id)),
      showDotFolders:left.showDotFolders === true,
      resourceRoot:left.resourceRoot && typeof left.resourceRoot === 'object' ? left.resourceRoot : null,
      folderWorkspaceRoot:left.folderWorkspaceRoot && typeof left.folderWorkspaceRoot === 'object' ? left.folderWorkspaceRoot : null,
      copalWorkspacePinned:left.copalWorkspacePinned === true,
      copalFolderRoot:Array.isArray(left.copalFolderRoot) ? left.copalFolderRoot.filter(part=>typeof part==='string' && part && !part.includes('/')).slice(0,64) : [],
      folderWorkspaceExplicit:left.folderWorkspaceExplicit === true,
      folderWorkspaceId:typeof left.folderWorkspaceId === 'string' ? left.folderWorkspaceId.trim() : '',
      binderContext:left.binderContext && typeof left.binderContext === 'object' && typeof left.binderContext.accountId === 'string' && typeof left.binderContext.workspace === 'string' && Number.isSafeInteger(left.binderContext.catalogRevision) && left.binderContext.catalogRevision >= 0 && left.binderContext.descriptor && typeof left.binderContext.descriptor === 'object' && typeof left.binderContext.descriptor.id === 'string' ? structuredClone(left.binderContext) : null,
      resourceRows:Array.isArray(left.resourceRows) ? left.resourceRows.slice(0, 5001) : [],
      resourceCursor:left.resourceCursor || null,
      resourceQuery:typeof left.resourceQuery === 'string' ? left.resourceQuery : '',
    },
    right: {
      open:right.open === true,
      width:safeNumber(right.width, 280, 190, 480),
      tab:rightVisible.includes(right.tab) ? right.tab : right.tab==='tags' && rightVisible.includes('properties') ? 'properties' : rightVisible[0] || null,
      pinnedDocId:typeof right.pinnedDocId === 'string' && docsById.has(right.pinnedDocId) ? right.pinnedDocId : null,
    },
    panels,
    settings,
    bookmarks:uniqueStrings(raw.bookmarks),
    recent:uniqueStrings(raw.recent).filter((id) => docsById.has(id)).slice(0, 40),
    closed:uniqueStrings(raw.closed).filter((id) => docsById.has(id)).slice(0, 20),
    closedImageRecovery,
  };
  if (!leaves.length && !workspace.left.folderWorkspaceExplicit) clearExplorerBinding(workspace.left);
  if (hasFutureImage || preserved) {
    try { Object.defineProperty(workspace, UNKNOWN_WORKSPACE_STATE, { value:preserved || JSON.stringify(raw), configurable:true }); } catch (_) {}
  }
  return workspace;
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

// Explicit navigation intent for document opens. `current` replaces the
// active tab's document (retaining the leaf id so buffer bindings survive);
// `newTab` always creates a tab; split intents create a sibling group.
export const NAVIGATION_INTENTS = Object.freeze(['current', 'newTab', 'splitRight', 'splitBelow']);

/** Derive navigation intent from a click/keyboard event's modifiers. */
export function navigationIntentFromEvent(event = null, { mac = undefined } = {}) {
  if (!event) return 'current';
  const isMac = mac ?? (typeof navigator !== 'undefined' && /Mac|iPhone|iPad/i.test(navigator.platform || navigator.userAgent || ''));
  // Ctrl on Windows/Linux and Command on macOS open a new tab. Shift splits
  // right and Alt splits below, matching the quick-switcher chooser.
  if (event.altKey) return 'splitBelow';
  if (event.shiftKey) return 'splitRight';
  if (isMac ? event.metaKey : event.ctrlKey) return 'newTab';
  return 'current';
}

function normalizeNavigationIntent(options = {}) {
  if (typeof options.intent === 'string' && NAVIGATION_INTENTS.includes(options.intent)) return options.intent;
  // Legacy `reuse:false` is newTab; `reuse:true`/absent keeps find-or-create.
  return options.reuse === false ? 'newTab' : null;
}

export function openWorkspaceDocument(workspace, doc, options = {}) {
  const intent = normalizeNavigationIntent(options);
  let group = findWorkspaceGroup(workspace, options.groupId);
  if (!group) group = groupForLeaf(workspace, workspace.activeLeafId) || workspaceGroups(workspace)[0];
  if (!group) {
    group = makeGroup();
    workspace.root = group;
  }
  if (intent === 'splitRight' || intent === 'splitBelow') {
    return splitWorkspaceGroup(workspace, group.id, doc, intent === 'splitBelow' ? 'vertical' : 'horizontal');
  }
  if (intent === 'current') {
    const active = group.tabs.find((item) => item.id === group.activeLeafId) || group.tabs[0] || null;
    // Pinned leaves keep their document (close paths skip pins the same way);
    // fall through to find-or-create instead of evicting a pin.
    if (active && !active.pinned) {
      if (active.docId === doc.id) {
        // Same-document current keeps the leaf's selection and scroll; only an
        // explicit mode request may change presentation.
        if (active.view !== 'note' && options.mode && MODES.has(options.mode)) active.mode = options.mode;
        activateWorkspaceLeaf(workspace, group.id, active.id);
        return active;
      }
      // Replace the active tab's document in place. The leaf id is retained
      // so note-buffer and editor-view bindings keyed by leaf stay coherent;
      // per-document dirty buffers live on the doc id and are not discarded.
      active.docId = doc.id;
      active.view = noteViewType(doc);
      active.mode = active.view === 'note' ? 'live' : MODES.has(options.mode) ? options.mode : 'live';
      active.selection = null;
      active.scrollTop = 0;
      active.rawSource = false;
      delete active.image;
      if (doc.kind === 'image-session' && doc.imageRecovery) active.image = makeLeaf(doc).image;
      activateWorkspaceLeaf(workspace, group.id, active.id);
      return active;
    }
    // Empty group or pinned active: fall through and create/activate a tab.
  }
  let leaf = intent === 'newTab' ? null : group.tabs.find((item) => item.docId === doc.id);
  if (!leaf) {
    leaf = makeLeaf(doc, { mode:options.mode });
    group.tabs.push(leaf);
  }
  activateWorkspaceLeaf(workspace, group.id, leaf.id);
  return leaf;
}

/** Add an image session to the existing leaf/split tree; mounting stays with the adapter. */
export function refreshWorkspaceImage(workspace, session) {
  const recovery = normalizeImageRecovery(session.serializeRecovery());
  for (const leaf of workspaceLeaves(workspace)) {
    if (leaf.image?.sessionId === recovery.sessionId) leaf.image.recovery = recovery;
  }
  return recovery;
}

export function openWorkspaceImage(workspace, session, options = {}) {
  const recovery = refreshWorkspaceImage(workspace, session);
  return openWorkspaceDocument(workspace, { id:`image-session:${recovery.sessionId}`, kind:'image-session', imageRecovery:recovery }, options);
}

/** Restore one adapter per source and map each saved split to that session. */
export function restoreWorkspaceImageSessions(workspace, registry) {
  const remapped = new Map();
  for (const leaf of workspaceLeaves(workspace)) {
    if (!leaf.image) continue;
    const oldDocId = leaf.docId;
    const session = registry.restore(leaf.image.recovery);
    const recovery = session.serializeRecovery();
    leaf.docId = `image-session:${recovery.sessionId}`;
    leaf.image.sessionId = recovery.sessionId;
    leaf.image.recovery = recovery;
    remapped.set(oldDocId, leaf.docId);
  }
  for (const field of ['recent', 'closed', 'bookmarks']) {
    workspace[field] = uniqueStrings((workspace[field] || []).map(id => remapped.get(id) || id));
  }
  return registry;
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

export function closeWorkspaceLeaf(workspace, leafId, {remember = true, pruneRecovery = true} = {}) {
  const group = groupForLeaf(workspace, leafId);
  const leaf = group?.tabs.find((item) => item.id === leafId);
  if (!group || !leaf || leaf.pinned) return null;
  const index = group.tabs.indexOf(leaf);
  group.tabs.splice(index, 1);
  if (remember) workspace.closed = [leaf.docId, ...workspace.closed.filter((id) => id !== leaf.docId)].slice(0, 20);
  if (pruneRecovery) pruneClosedImageRecovery(workspace);
  if (group.activeLeafId === leafId) group.activeLeafId = group.tabs[Math.min(index, group.tabs.length - 1)]?.id || null;
  workspace.root = pruneNode(workspace.root);
  if (workspace.activeLeafId === leafId) workspace.activeLeafId = group.activeLeafId || workspaceLeaves(workspace)[0]?.id || null;
  if (!workspace.activeLeafId) clearExplorerBinding(workspace.left);
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

function pruneClosedImageRecovery(workspace) {
  if (workspace[UNKNOWN_WORKSPACE_STATE] || !workspace.closedImageRecovery) return;
  const keep=new Set(workspace.closed);
  for (const id of Object.keys(workspace.closedImageRecovery)) if (!keep.has(id)) delete workspace.closedImageRecovery[id];
}

function preserveRawWorkspace(workspace, raw) {
  Object.defineProperty(workspace, UNKNOWN_WORKSPACE_STATE, {value:JSON.stringify(raw),configurable:true});
}

export function canRetainClosedImageRecovery(workspace) {
  const preserved=workspace?.[UNKNOWN_WORKSPACE_STATE];
  return !!workspace && (!preserved || JSON.parse(preserved).version === NOTES_WORKSPACE_VERSION);
}

export function retainClosedImageRecovery(workspace,value,{pruneRecovery=true}={}) {
  if (!canRetainClosedImageRecovery(workspace)) return false;
  const recovery=normalizeImageRecovery(value);
  const id=`image-session:${recovery.sessionId}`;
  workspace.closed=[id,...workspace.closed.filter(item=>item!==id)].slice(0,20);
  workspace.closedImageRecovery ||= {};
  workspace.closedImageRecovery[id]=recovery;
  if (pruneRecovery) pruneClosedImageRecovery(workspace);
  return true;
}

export function removeClosedImageRecovery(workspace,id) {
  if (workspace?.[UNKNOWN_WORKSPACE_STATE]) return false;
  if (workspace?.closedImageRecovery) delete workspace.closedImageRecovery[id];
  return true;
}

function copyWorkspaceLayout(workspace) {
  const next={...workspace,left:{...workspace.left},root:structuredClone(workspace.root),recent:[...workspace.recent],closed:[...workspace.closed],
    closedImageRecovery:{...workspace.closedImageRecovery}};
  const preserved=workspace[UNKNOWN_WORKSPACE_STATE];
  if (preserved) Object.defineProperty(next, UNKNOWN_WORKSPACE_STATE, {value:preserved,configurable:true});
  return next;
}

/** Edit only addressed known leaves and recovery records in a preserved current-format layout. */
export function stageWorkspaceClose(workspace, {remove, retain, forget, refresh = []}) {
  const next=copyWorkspaceLayout(workspace);
  const latest=new Map(refresh.map(recovery=>[recovery.sessionId,recovery]));
  for (const leaf of workspaceLeaves(next)) {
    if (latest.has(leaf.image?.sessionId)) leaf.image.recovery=latest.get(leaf.image.sessionId);
  }
  const preserved=workspace[UNKNOWN_WORKSPACE_STATE];
  for (const leaf of remove) {
    const hasSibling=leaf.image && workspaceLeaves(workspace).some(other=>other.docId===leaf.docId && !remove.some(item=>item.id===other.id));
    if (!closeWorkspaceLeaf(next,leaf.id,{remember:!hasSibling && leaf.remember !== false,pruneRecovery:false})) throw new Error('The selected tab changed. Reopen Close and try again.');
  }
  for (const id of forget) delete next.closedImageRecovery[id];
  for (const value of retain) if (!retainClosedImageRecovery(next,value,{pruneRecovery:false})) throw new Error('This layout requires a newer Editor to retain recovery.');
  if (!preserved || !remove.length && !retain.length && !forget.length && !refresh.length) return next;

  const raw=JSON.parse(preserved);
  if (raw.version!==NOTES_WORKSPACE_VERSION) throw new Error('This layout requires a newer Editor to close these tabs.');
  const requested=new Map(remove.map(leaf=>[leaf.id,leaf]));
  const removed=new Set();
  const visit=node=>{
    if (!node || typeof node!=='object') return;
    if (node.type==='split') {
      if (Array.isArray(node.children)) node.children.forEach(visit);
      return;
    }
    if (node.type!=='group' || !Array.isArray(node.tabs)) return;
    node.tabs=node.tabs.filter(leaf=>{
      const match=requested.get(leaf?.id);
      if (!match) {
        const recovery=latest.get(leaf?.image?.sessionId);
        if (recovery && leaf.image.version===1 && leaf.image.recovery?.version===1 &&
            leaf.docId===`image-session:${recovery.sessionId}`) leaf.image.recovery={...leaf.image.recovery,...recovery};
        return true;
      }
      if (removed.has(match.id) || leaf.docId!==match.docId || leaf.pinned===true ||
          (leaf.image?.sessionId || null)!==(match.image?.sessionId || null) ||
          (leaf.image && (leaf.image.version!==1 || leaf.image.recovery?.version!==1))) {
        throw new Error('The preserved layout has a different tab identity.');
      }
      removed.add(match.id);
      return false;
    });
    if (removed.has(node.activeLeafId)) node.activeLeafId=node.tabs[0]?.id || null;
  };
  visit(raw.root);
  if (removed.has(raw.activeLeafId)) raw.activeLeafId=next.activeLeafId;
  if (remove.length && !next.activeLeafId && raw.left && typeof raw.left==='object') clearExplorerBinding(raw.left);
  const retainedIds=new Set(retain.map(item=>`image-session:${item.sessionId}`));
  const newlyClosed=next.closed.filter(id=>retainedIds.has(id) || remove.some(leaf=>leaf.docId===id && leaf.remember!==false));
  if (newlyClosed.length) raw.closed=uniqueStrings([...newlyClosed,...(Array.isArray(raw.closed) ? raw.closed : [])]);
  if (retain.length || forget.length) {
    if (raw.closedImageRecovery==null) raw.closedImageRecovery={};
    if (typeof raw.closedImageRecovery!=='object' || Array.isArray(raw.closedImageRecovery)) throw new Error('Existing image recovery records need a newer Editor.');
    for (const id of [...retainedIds,...forget]) {
      const prior=raw.closedImageRecovery[id];
      if (prior && prior.version!==1) throw new Error('An image recovery record needs a newer Editor.');
    }
    for (const id of forget) delete raw.closedImageRecovery[id];
    for (const id of retainedIds) raw.closedImageRecovery[id]={...raw.closedImageRecovery[id],...next.closedImageRecovery[id]};
  }
  preserveRawWorkspace(next,raw);
  return next;
}

export function stageWorkspaceImageReopen(workspace, doc) {
  const next=copyWorkspaceLayout(workspace);
  const prior=next.closedImageRecovery[doc.id];
  if (!prior || prior.unavailable || !doc.imageRecovery || prior!==doc.imageRecovery) throw new Error('Saved image recovery is unavailable.');
  const existing=workspaceLeaves(next).find(leaf=>leaf.docId===doc.id);
  const leaf=existing || openWorkspaceDocument(next,doc,{intent:'newTab'});
  const group=groupForLeaf(next,leaf.id);
  if (existing) activateWorkspaceLeaf(next,group.id,leaf.id);
  next.closed=next.closed.filter(id=>id!==doc.id);
  delete next.closedImageRecovery[doc.id];
  const preserved=workspace[UNKNOWN_WORKSPACE_STATE];
  if (preserved) {
    const raw=JSON.parse(preserved);
    const source=raw.closedImageRecovery?.[doc.id];
    const target=findWorkspaceGroup(raw,group.id);
    if (raw.version!==NOTES_WORKSPACE_VERSION || source?.version!==1 || source.sessionId!==prior.sessionId || !Array.isArray(target?.tabs)) {
      throw new Error('A newer Editor is required to reopen the preserved image recovery.');
    }
    if (!existing) target.tabs.push({...leaf,image:{...leaf.image,recovery:source}});
    else if (!target.tabs.some(candidate=>candidate.id===leaf.id && candidate.docId===doc.id)) throw new Error('The preserved image view changed.');
    target.activeLeafId=leaf.id;
    raw.activeLeafId=leaf.id;
    if (Array.isArray(raw.closed)) raw.closed=raw.closed.filter(id=>id!==doc.id);
    delete raw.closedImageRecovery[doc.id];
    preserveRawWorkspace(next,raw);
  }
  return next;
}

export function commitWorkspaceClose(workspace,next) {
  const surviving=new Map(workspaceLeaves(workspace).map(leaf=>[leaf.id,leaf]));
  for (const group of workspaceGroups(next)) group.tabs=group.tabs.map(staged=>{
    const leaf=surviving.get(staged.id);
    if (!leaf || leaf.docId!==staged.docId) return staged;
    if (leaf.image && staged.image && JSON.stringify(leaf.image.recovery)!==JSON.stringify(staged.image.recovery)) {
      leaf.image.recovery=staged.image.recovery;
    }
    return leaf;
  });
  for (const field of ['root','activeLeafId','recent','closed','closedImageRecovery']) workspace[field]=next[field];
  if (!next.activeLeafId) clearExplorerBinding(workspace.left);
  if (next[UNKNOWN_WORKSPACE_STATE]) Object.defineProperty(workspace, UNKNOWN_WORKSPACE_STATE,
    {value:next[UNKNOWN_WORKSPACE_STATE],configurable:true});
}

export function serializeNotesWorkspace(workspace) {
  return workspace?.[UNKNOWN_WORKSPACE_STATE] || JSON.stringify(workspace);
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
  for (const id of ['files', 'search']) {
    panels[id].side = 'left';
    panels[id].hidden = false;
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
