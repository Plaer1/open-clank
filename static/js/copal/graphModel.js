import { cloneEnvelope, resourceKeyId } from './resourceModel.js';

// Graph, Galaxy and structure are presentations of one scoped window.  The
// structure projection keeps its own local tree selection/camera state while
// sharing this mode envelope.  `mind` is a legacy saved-state alias that
// normalizes to `structure`; it is not a separate product identity.
export const GRAPH_MODES = ['documents', 'structure', 'galaxy'];
export const LEGACY_MODE_ALIAS = { mind:'structure' };
const DEFAULT_MODE = 'documents';
const CAMERA = { x:0, y:0, w:1000, h:650 };

function markdownLineMap(source) {
  const text = String(source ?? '');
  const newline = text.match(/\r\n|\n|\r/)?.[0] || '\n';
  const lines = text.split(/\r\n|\n|\r/);
  const blocked = new Set();
  let frontmatter = false;
  let fence = null;
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const trimmed = line.trim();
    const indent = (line.match(/^[ \t]*/) || [''])[0];
    if (index === 0 && /^\uFEFF?\s*---\s*$/.test(line)) { frontmatter = true; blocked.add(index); continue; }
    if (frontmatter) { blocked.add(index); if (/^\s*(---|\.\.\.)\s*$/.test(line)) frontmatter = false; continue; }
    if (fence) {
      blocked.add(index);
      const close = new RegExp(`^[ \\t]{0,3}${fence.char}{${fence.length},}[ \\t]*$`);
      if (close.test(line)) fence = null;
      continue;
    }
    if (/^[ \t]{4}/.test(line) || /^\t/.test(line)) { blocked.add(index); continue; }
    const opening = /^[ \t]{0,3}(`{3,}|~{3,})(?:[^`~]*)?$/.exec(line);
    if (opening) { fence = { char:opening[1][0], length:opening[1].length }; blocked.add(index); continue; }
    if (/^\s*>/.test(line)) blocked.add(index);
  }
  const entries = [];
  for (let index = 0; index < lines.length; index += 1) {
    if (blocked.has(index)) continue;
    const atx = /^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?)[ \t]*)?$/.exec(lines[index]);
    if (atx) {
      entries.push({ line:index + 1, level:atx[1].length, text:String(atx[2] || '').replace(/[ \t]+#+[ \t]*$/, '').trim(), kind:'atx' });
      continue;
    }
    if (index + 1 < lines.length && !blocked.has(index + 1) && lines[index].trim() && /^[ \t]*(=+|-+)[ \t]*$/.test(lines[index + 1])) {
      entries.push({ line:index + 1, level:lines[index + 1].trimStart().startsWith('=') ? 1 : 2, text:lines[index].trim(), kind:'setext' });
      blocked.add(index + 1);
    }
  }
  return { text, lines, newline, entries, blocked };
}

export function headingEntries(text) {
  return markdownLineMap(text).entries.map(({ line, level, text:headingText }) => ({ line, level, text:headingText }));
}

/**
 * Source-mapped document structure: headings plus ordinary list items.
 *
 * Bullets nest by their leading-indent depth so a document's branching map can
 * show nested bullet structure beside headings.  Ordered and unordered markers
 * are accepted.  Code fences, frontmatter/properties and literal code are
 * excluded; inside an open list a deeper indent is list nesting, not an
 * indented code block (CommonMark list continuation).
 */
export function structureEntries(text) {
  const map = markdownLineMap(text);
  const structure = [];
  // Fence/frontmatter bands are tracked here rather than taken from the shared
  // line map: that map also blocks indented code, which would wrongly drop
  // legitimately nested list items.
  const openList = [];
  const indentWidth = (line) => (line.match(/^[ \t]*/) || [''])[0].replace(/\t/g, '    ').length;
  // Bullets nest beneath the enclosing heading in one tree: a bullet's tree
  // level is the heading's level plus its indent depth, so structureTree can
  // stack headings and bullets through a single level comparison.
  let headingLevel = 0;
  let frontmatter = false;
  let fence = null;
  for (let index = 0; index < map.lines.length; index += 1) {
    const line = map.lines[index];
    if (index === 0 && /^\uFEFF?\s*---\s*$/.test(line)) { frontmatter = true; openList.length = 0; continue; }
    if (frontmatter) { if (/^\s*(---|\.\.\.)\s*$/.test(line)) frontmatter = false; continue; }
    if (fence) {
      const close = new RegExp(`^[ \\t]{0,3}${fence.char}{${fence.length},}[ \\t]*$`);
      if (close.test(line)) { fence = null; openList.length = 0; }
      continue;
    }
    const opening = /^[ \t]{0,3}(`{3,}|~{3,})(?:[^`~]*)?$/.exec(line);
    if (opening) { fence = { char:opening[1][0], length:opening[1].length }; openList.length = 0; continue; }
    if (/^\s*>/.test(line)) continue;
    const trimmed = line.trim();
    if (!trimmed) continue;
    const indent = indentWidth(line);
    const atx = /^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?)[ \t]*)?$/.exec(line);
    if (atx) {
      openList.length = 0;
      headingLevel = atx[1].length;
      structure.push({ line:index + 1, level:headingLevel, text:String(atx[2] || '').replace(/[ \t]+#+[ \t]*$/, '').trim(), kind:'heading', marker:'atx' });
      continue;
    }
    const bullet = /^([ \t]*)([-*+]|\d{1,9}[.)])([ \t]+)(.*)$/.exec(line);
    if (bullet) {
      // Inside an open list a deeper indent nests.  Outside one, 4+ spaces is
      // literal code and must not become a fake bullet.
      const inList = openList.length > 0 && indent > openList[openList.length - 1];
      if (indent >= 4 && !inList) continue;
      while (openList.length && openList[openList.length - 1] >= indent) openList.pop();
      openList.push(indent);
      structure.push({
        line:index + 1,
        level:headingLevel + 1 + Math.floor(indent / 2),
        text:String(bullet[4] || '').trim(),
        kind:'bullet',
        ordered:/^\d/.test(bullet[2]),
        indent,
      });
      continue;
    }
    if (indent >= 4 && openList.length === 0) continue;
    openList.length = 0;
    if (index + 1 < map.lines.length && /^[ \t]*(=+|-+)[ \t]*$/.test(map.lines[index + 1]) && indent < 4) {
      const underline = map.lines[index + 1].trimStart();
      headingLevel = underline.startsWith('=') ? 1 : 2;
      structure.push({ line:index + 1, level:headingLevel, text:trimmed, kind:'heading', marker:'setext' });
    }
  }
  const seen = new Set();
  return structure.filter((entry) => (seen.has(entry.line) ? false : (seen.add(entry.line), true))).sort((left, right) => left.line - right.line);
}

export function structureTree(entries) {
  const roots = [];
  const stack = [];
  for (const entry of entries || []) {
    const node = { ...entry, children:[] };
    // Headings nest by heading level; bullets nest by indent depth under the
    // nearest enclosing node so one tree can hold both without a second parse.
    const level = entry.kind === 'bullet' ? entry.level : entry.level;
    while (stack.length && stack[stack.length - 1].level >= level) stack.pop();
    if (stack.length) stack[stack.length - 1].children.push(node);
    else roots.push(node);
    stack.push(node);
  }
  return roots;
}

export function headingTree(entries) {
  const roots = [];
  const stack = [];
  for (const entry of entries || []) {
    const node = { ...entry, children:[] };
    while (stack.length && stack[stack.length - 1].level >= node.level) stack.pop();
    if (stack.length) stack[stack.length - 1].children.push(node);
    else roots.push(node);
    stack.push(node);
  }
  return roots;
}

// Heading mutations operate on the source text itself so newline style,
// Unicode, and Setext markers remain part of one atomic buffer transaction.
function sourceLines(source) {
  const text = String(source ?? '');
  const newline = text.match(/\r\n|\n|\r/)?.[0] || '\n';
  return { lines:text.split(/\r\n|\n|\r/), newline };
}

function headingAt(lines, line) {
  const mapped = markdownLineMap(lines.join('\n')).entries.find((entry) => entry.line === Number(line));
  if (!mapped) return null;
  const index = mapped.line - 1;
  if (mapped.kind === 'setext') return { ...mapped, index, underline:index + 1 };
  const atx = /^[ \t]{0,3}(#{1,6})(?:[ \t]+(.*?)[ \t]*)?$/.exec(lines[index]);
  return atx ? { ...mapped, index, prefix:(lines[index].match(/^[ \t]{0,3}/) || [''])[0] } : null;
}

function sectionEnd(entries, index, lineCount) {
  const entry = entries[index];
  for (let next = index + 1; next < entries.length; next += 1) {
    if (entries[next].level <= entry.level) return entries[next].line - 1;
  }
  return lineCount;
}

function renderHeading(lines, heading, text, level) {
  const nextLevel = Math.max(1, Math.min(6, Number(level) || heading.level));
  if (heading.kind === 'setext' && nextLevel <= 2) {
    lines[heading.index] = String(text).trim();
    lines[heading.underline] = '='.repeat(nextLevel === 1 ? Math.max(3, lines[heading.underline].trim().length) : Math.max(3, lines[heading.underline].trim().length)).replace(/./g, nextLevel === 1 ? '=' : '-');
    return;
  }
  // A Setext level change beyond level 2 becomes ATX and removes its marker
  // in the same source rewrite; this prevents a stale underline becoming a
  // paragraph rule after a toolbar or keyboard level change.
  if (heading.kind === 'setext') {
    lines.splice(heading.index, 2, `${'#'.repeat(nextLevel)} ${String(text).trim()}`);
  } else {
    lines[heading.index] = `${heading.prefix || ''}${'#'.repeat(nextLevel)} ${String(text).trim()}`;
  }
}

export function renameHeading(source, line, text) {
  const { lines, newline } = sourceLines(source);
  const heading = headingAt(lines, line);
  if (!heading) return String(source ?? '');
  renderHeading(lines, heading, text, heading.level);
  return lines.join(newline);
}

export function changeHeadingLevel(source, line, level) {
  const { lines, newline } = sourceLines(source);
  const heading = headingAt(lines, line);
  if (!heading) return String(source ?? '');
  renderHeading(lines, heading, heading.text, level);
  return lines.join(newline);
}

export function deleteHeadingSection(source, line) {
  const { lines, newline } = sourceLines(source);
  const entries = headingEntries(lines.join('\n'));
  const index = entries.findIndex((entry) => entry.line === Number(line));
  if (index < 0) return String(source ?? '');
  const heading = headingAt(lines, line);
  const start = heading?.index ?? Number(line) - 1;
  const end = sectionEnd(entries, index, lines.length);
  lines.splice(start, Math.max(1, end - start));
  return lines.join(newline);
}

function shiftSectionLevels(lines, start, end, delta) {
  const text = lines.slice(start, end);
  const entries = headingEntries(text.join('\n'));
  for (const entry of [...entries].reverse()) {
    const local = headingAt(text, entry.line);
    if (!local) continue;
    renderHeading(text, local, local.text, Math.max(1, Math.min(6, local.level + delta)));
  }
  lines.splice(start, end - start, ...text);
}

/** Move a complete heading branch below a target, rejecting descendant cycles. */
export function reparentHeadingSection(source, fromLine, toLine) {
  const { lines, newline } = sourceLines(source);
  const entries = headingEntries(lines.join('\n'));
  const fromIndex = entries.findIndex((entry) => entry.line === Number(fromLine));
  const toIndex = entries.findIndex((entry) => entry.line === Number(toLine));
  if (fromIndex < 0 || toIndex < 0 || fromIndex === toIndex) return String(source ?? '');
  const fromStart = headingAt(lines, fromLine)?.index ?? Number(fromLine) - 1;
  const fromEnd = sectionEnd(entries, fromIndex, lines.length);
  const targetStart = headingAt(lines, toLine)?.index ?? Number(toLine) - 1;
  if (targetStart >= fromStart && targetStart < fromEnd) return String(source ?? '');
  const branch = lines.slice(fromStart, fromEnd);
  const target = headingAt(lines, toLine);
  const delta = Math.max(1, Math.min(6, (target?.level || entries[toIndex].level) + 1)) - entries[fromIndex].level;
  shiftSectionLevels(branch, 0, branch.length, delta);
  lines.splice(fromStart, fromEnd - fromStart);
  // The target may be the heading immediately following the moved branch;
  // account for the removed range at its start boundary as well as later.
  const adjustedTarget = targetStart >= fromStart ? targetStart - (fromEnd - fromStart) : targetStart;
  const targetEntries = headingEntries(lines.join('\n'));
  const adjustedTargetIndex = targetEntries.findIndex((entry) => entry.line === adjustedTarget + 1);
  const contentEnd = (() => { let end = lines.length; while (end > 0 && lines[end - 1] === '') end -= 1; return end; })();
  const insertion = adjustedTargetIndex >= 0 ? Math.min(sectionEnd(targetEntries, adjustedTargetIndex, lines.length), contentEnd) : Math.min(adjustedTarget, contentEnd);
  lines.splice(insertion, 0, ...branch);
  return lines.join(newline);
}

export function moveHeadingSection(source, line, direction) {
  const { lines, newline } = sourceLines(source);
  const entries = headingEntries(lines.join('\n'));
  const index = entries.findIndex((entry) => entry.line === Number(line));
  if (index < 0) return String(source ?? '');
  const start = headingAt(lines, line)?.index ?? Number(line) - 1;
  const end = sectionEnd(entries, index, lines.length);
  const siblingLevel = entries[index].level;
  let targetIndex = index + (Number(direction) < 0 ? -1 : 1);
  while (targetIndex >= 0 && targetIndex < entries.length && entries[targetIndex].level > siblingLevel) targetIndex += Number(direction) < 0 ? -1 : 1;
  if (targetIndex < 0 || targetIndex >= entries.length || entries[targetIndex].level !== siblingLevel) return String(source ?? '');
  const targetStart = headingAt(lines, entries[targetIndex].line)?.index ?? entries[targetIndex].line - 1;
  const targetEnd = sectionEnd(entries, targetIndex, lines.length);
  const branch = lines.splice(start, end - start);
  const adjusted = targetStart > start ? targetStart - (end - start) : targetStart;
  const insertion = Number(direction) < 0 ? adjusted : (targetEnd - (targetStart > start ? end - start : 0));
  lines.splice(Math.max(0, insertion), 0, ...branch);
  return lines.join(newline);
}

export function normalizeGraphState(source = {}) {
  const modes = {};
  // Legacy `mind` saved state projects onto structure mode so older entry
  // points keep their camera/selection instead of losing the workspace.
  const savedModes = source?.modes || {};
  const legacyMind = savedModes.mind || null;
  for (const mode of GRAPH_MODES) {
    const saved = savedModes[mode] || (mode === 'structure' ? legacyMind : null) || {};
    const camera = Object.fromEntries(Object.entries(CAMERA).map(([key, fallback]) => {
      const value = Number(saved.camera?.[key]);
      return [key, Number.isFinite(value) && (!['w', 'h'].includes(key) || value >= 10 && value <= 100000) ? value : fallback];
    }));
    modes[mode] = {
      camera,
      selection: saved.selection && typeof saved.selection === 'object' ? cloneEnvelope(saved.selection) : null,
      navigation: mode === 'structure' ? {
        docId: saved.navigation?.docId ? String(saved.navigation.docId) : null,
        collapsed: Array.isArray(saved.navigation?.collapsed) ? [...new Set(saved.navigation.collapsed.map(Number).filter(Number.isSafeInteger))] : [],
        selectedLine: Number.isSafeInteger(Number(saved.navigation?.selectedLine)) ? Number(saved.navigation.selectedLine) : null,
        editingLine: Number.isSafeInteger(Number(saved.navigation?.editingLine)) ? Number(saved.navigation.editingLine) : null,
        draggingLine: null,
        draggingPayload: null,
      } : null,
      filters:{
        search:String(saved.filters?.search || '').slice(0, 512),
        // Kinds are validated against the live facet snapshot when it loads;
        // unknown values are kept here and reconciled by `reconcileFilters`
        // so a renamed/removed value degrades instead of hard-failing.
        kinds:Array.isArray(saved.filters?.kinds) ? [...new Set(saved.filters.kinds.map(String))] : mode === 'galaxy' ? ['track', 'event'] : ['note', 'wiki', 'base', 'event'],
        folders:Array.isArray(saved.filters?.folders) ? [...new Set(saved.filters.folders.map(String))] : [],
        tags:Array.isArray(saved.filters?.tags) ? [...new Set(saved.filters.tags.map(String))] : [],
        properties:saved.filters?.properties && typeof saved.filters.properties === 'object' && !Array.isArray(saved.filters.properties)
          ? Object.fromEntries(Object.entries(saved.filters.properties).map(([key, values]) => [String(key), Array.isArray(values) ? [...new Set(values.map(String))] : []]))
          : {},
        includeOfficial:saved.filters?.includeOfficial === true,
      },
    };
  }
  // One source envelope belongs to the scoped Graph family.  Mode-specific
  // selection remains in `modes`, while this identity survives a Documents →
  // structure projection switch and can explicitly describe an unavailable source.
  const sharedSource = source?.source && typeof source.source === 'object' ? cloneEnvelope(source.source) : null;
  const requestedMode = LEGACY_MODE_ALIAS[source?.mode] || source?.mode;
  return { version:1, mode:GRAPH_MODES.includes(requestedMode) ? requestedMode : DEFAULT_MODE, source:sharedSource, modes };
}

/**
 * Reconcile a saved filter set against a freshly derived facet snapshot.
 *
 * Values that no longer exist in the scoped corpus are dropped so a renamed or
 * deleted folder/tag/property cannot silently hide the whole graph.  Absent
 * facet categories leave their saved values untouched (still recoverable) so
 * a partial or in-flight snapshot does not discard user intent.
 */
export function reconcileFilters(filters = {}, facets = null) {
  const next = {
    search: String(filters.search || '').slice(0, 512),
    kinds: Array.isArray(filters.kinds) ? [...new Set(filters.kinds.map(String))] : [],
    folders: Array.isArray(filters.folders) ? [...new Set(filters.folders.map(String))] : [],
    tags: Array.isArray(filters.tags) ? [...new Set(filters.tags.map(String))] : [],
    properties: filters.properties && typeof filters.properties === 'object' && !Array.isArray(filters.properties)
      ? Object.fromEntries(Object.entries(filters.properties).map(([key, values]) => [String(key), Array.isArray(values) ? [...new Set(values.map(String))] : []]))
      : {},
    includeOfficial: filters.includeOfficial === true,
  };
  if (!facets || typeof facets !== 'object') return next;
  const prune = (values, known) => {
    if (!Array.isArray(known)) return values;
    const allowed = new Set(known.map(String));
    return values.filter((value) => allowed.has(String(value)));
  };
  if (Array.isArray(facets.kinds)) next.kinds = prune(next.kinds, facets.kinds.map((item) => (item && item.value !== undefined ? item.value : item)));
  if (Array.isArray(facets.folders)) next.folders = prune(next.folders, facets.folders.map((item) => (item && item.value !== undefined ? item.value : item)));
  if (Array.isArray(facets.tags)) next.tags = prune(next.tags, facets.tags.map((item) => (item && item.value !== undefined ? item.value : item)));
  if (facets.properties && typeof facets.properties === 'object') {
    const allowedByKey = new Map(Object.entries(facets.properties).map(([key, values]) => [String(key), new Set((Array.isArray(values) ? values : []).map((item) => String(item && item.value !== undefined ? item.value : item)))]));
    next.properties = Object.fromEntries(Object.entries(next.properties)
      .filter(([key]) => allowedByKey.has(String(key)))
      .map(([key, values]) => [key, values.filter((value) => allowedByKey.get(String(key)).has(String(value)))]));
  }
  return next;
}

export function graphStorageKey(accountId, workspace) {
  if (!accountId || !workspace) return null;
  return `copal-graph:v1:${encodeURIComponent(accountId)}:${encodeURIComponent(workspace)}`;
}

/** Cache identity for a derived facet snapshot. */
export function facetCacheKey(accountId, workspace, generation) {
  if (!accountId || !workspace) return null;
  return `copal-graph-facets:v1:${encodeURIComponent(accountId)}:${encodeURIComponent(workspace)}:${encodeURIComponent(String(generation ?? '0'))}`;
}

/** Top-level folder path of a document name, or '' for root-level names. */
export function documentFolder(name) {
  const raw = String(name || '');
  const index = raw.lastIndexOf('/');
  return index > 0 ? raw.slice(0, index) : '';
}

/**
 * Official / provisioned documentation is recognized by its real identity
 * metadata — the builtin flag and the shipped `product: open-clank` marker —
 * never by an English name, a dot prefix, or read-only status.
 */
export function isOfficialDocument(doc) {
  if (!doc || typeof doc !== 'object') return false;
  if (doc.builtin === true) return true;
  const properties = doc.properties && typeof doc.properties === 'object' ? doc.properties : {};
  const product = String(properties.product ?? doc.product ?? '').trim().toLowerCase();
  return product === 'open-clank' || properties.builtin === true;
}

/**
 * The provisioned official-docs root folder, derived from the real identities
 * of the shipped documents rather than a hardcoded folder name.  Returns null
 * when the scoped corpus contains no provisioned documentation.
 */
export function officialDocsRoot(documents) {
  const roots = new Map();
  for (const doc of documents || []) {
    if (!isOfficialDocument(doc)) continue;
    const folder = documentFolder(doc.name).split('/')[0] || '';
    if (!folder) continue;
    roots.set(folder, (roots.get(folder) || 0) + 1);
  }
  if (!roots.size) return null;
  // Prefer the root that covers the most provisioned documents; ties resolve
  // lexicographically so the choice is deterministic across refreshes.
  return [...roots.entries()].sort((left, right) => right[1] - left[1] || left[0].localeCompare(right[0]))[0][0];
}

function facetValue(value) {
  return String(value ?? '').trim();
}

function addFacet(map, value, doc) {
  const key = facetValue(value);
  if (!key) return;
  const entry = map.get(key) || { value:key, count:0, documents:[] };
  entry.count += 1;
  entry.documents.push(doc.id ?? doc.name ?? null);
  map.set(key, entry);
}

/**
 * Derive scoped filter facets from real metadata, folders and supported values.
 *
 * Kinds, folders, tags and free-form property values are all discovered from
 * the corpus itself — nothing is forced into a fixed category allowlist.  The
 * result is a plain serializable snapshot so it can be cached by
 * `facetCacheKey` and invalidated when the index generation changes.
 */
export function deriveFacets(documents, { generation = 0, officialRoot = undefined } = {}) {
  const docs = Array.isArray(documents) ? documents : [];
  const kinds = new Map();
  const folders = new Map();
  const tags = new Map();
  const properties = new Map();
  const root = officialRoot === undefined ? officialDocsRoot(docs) : officialRoot;
  for (const doc of docs) {
    const kind = documentKind(doc);
    addFacet(kinds, kind, doc);
    addFacet(folders, documentFolder(doc.name), doc);
    const docTags = Array.isArray(doc.tags) ? doc.tags : [];
    for (const tag of docTags) addFacet(tags, tag, doc);
    const props = doc.properties && typeof doc.properties === 'object' ? doc.properties : {};
    for (const [key, value] of Object.entries(props)) {
      const propertyKey = facetValue(key);
      if (!propertyKey) continue;
      const bucket = properties.get(propertyKey) || new Map();
      const values = Array.isArray(value) ? value : [value];
      for (const item of values) {
        if (item === null || item === undefined || item === '') continue;
        if (typeof item === 'object') continue;
        addFacet(bucket, item, doc);
      }
      properties.set(propertyKey, bucket);
    }
  }
  const sorted = (map) => [...map.values()].sort((left, right) => right.count - left.count || left.value.localeCompare(right.value));
  return {
    version:1,
    generation:String(generation ?? '0'),
    officialRoot:root,
    totalDocuments:docs.length,
    kinds:sorted(kinds),
    folders:sorted(folders),
    tags:sorted(tags),
    properties:Object.fromEntries([...properties.entries()].map(([key, bucket]) => [key, sorted(bucket)])),
  };
}

/**
 * Real filter predicate over a document and its derived facets.  All filter
 * dimensions are AND-ed; within a dimension any selected value matches so the
 * user can widen a facet without adding a separate control.
 *
 * Official documentation is excluded through the general folder filter bound to
 * the derived official root.  Dot-folders are never treated as official/system
 * provenance, so personal `.events/` or `.memes/` content stays governed by the
 * ordinary filters rather than being silently dropped.
 */
export function matchesFilters(doc, filters = {}, facets = null) {
  const search = String(filters.search || '').trim().toLowerCase();
  if (search) {
    const haystack = [doc.name, doc.text, ...(Array.isArray(doc.tags) ? doc.tags : [])].map((value) => String(value ?? '').toLowerCase());
    if (!haystack.some((value) => value.includes(search))) return false;
  }
  const kind = documentKind(doc);
  const kinds = Array.isArray(filters.kinds) ? filters.kinds : [];
  if (kinds.length && !kinds.includes(kind)) return false;
  const folder = documentFolder(doc.name);
  const folders = Array.isArray(filters.folders) ? filters.folders : [];
  if (folders.length && !folders.includes(folder)) return false;
  const docTags = new Set((Array.isArray(doc.tags) ? doc.tags : []).map(String));
  const tags = Array.isArray(filters.tags) ? filters.tags : [];
  if (tags.length && !tags.some((tag) => docTags.has(String(tag)))) return false;
  const properties = filters.properties && typeof filters.properties === 'object' && !Array.isArray(filters.properties) ? filters.properties : {};
  const docProps = doc.properties && typeof doc.properties === 'object' ? doc.properties : {};
  for (const [key, values] of Object.entries(properties)) {
    const wanted = Array.isArray(values) ? values.map(String) : [];
    if (!wanted.length) continue;
    const raw = docProps[key];
    const actual = (Array.isArray(raw) ? raw : [raw]).map((value) => String(value ?? ''));
    if (!wanted.some((value) => actual.includes(value))) return false;
  }
  if (filters.includeOfficial !== true) {
    const root = facets && typeof facets === 'object' ? facets.officialRoot : undefined;
    const officialRoot = root === undefined ? null : root;
    const top = folder.split('/')[0] || '';
    // Folder-based default: the provisioned official-docs root is hidden until
    // the user opts in. Identity metadata still marks the docs themselves.
    if (officialRoot && top === officialRoot) return false;
    if (!officialRoot && isOfficialDocument(doc)) return false;
  }
  return true;
}

function documentKind(doc) {
  return doc.kind === 'wiki' ? 'wiki' : doc.kind === 'base' ? 'base' : doc.kind === 'copal-event' ? 'event' : 'note';
}

/** Apply real facet filters to a scoped document list. */
export function filterDocuments(documents, filters = {}, facets = null) {
  return (Array.isArray(documents) ? documents : []).filter((doc) => matchesFilters(doc, filters, facets));
}

export function documentGraph(documents, resolveLink) {
  const nodes = documents.map((doc) => ({
    id:`document:${doc.resource?.key ? resourceKeyId(doc.resource.key) : doc.id}`,
    label:doc.name, kind:documentKind(doc), doc,
    selection:{ kind:'document', id:doc.id, ...(doc.resource?.key ? { resourceKey:doc.resource.key } : {}) },
  }));
  const byId = new Map(nodes.map((node) => [node.doc.id, node]));
  const byResourceId = new Map(nodes.flatMap((node) => {
    const resourceId = node.doc.resource?.key?.resourceId;
    return resourceId ? [[String(resourceId), node]] : [];
  }));
  const edgeMap = new Map();
  const add = (source, target, type) => {
    const from = byId.get(source)?.id, to = byId.get(target)?.id;
    if (!from || !to) return;
    const id = JSON.stringify([from, to, type]);
    if (!edgeMap.has(id)) edgeMap.set(id, { id, from, to, type });
  };
  for (const doc of documents) {
    for (const link of doc.links || []) {
      const direct = byId.get(String(link)) || byResourceId.get(String(link));
      add(doc.id, direct?.doc.id || resolveLink(link, doc)?.id, 'link');
    }
    for (const relation of doc.relations || []) {
      const target = byId.get(String(relation.targetDocumentId || ''))?.doc.id
        || byResourceId.get(String(relation.targetDocumentId || ''))?.doc.id
        || resolveLink(relation.target, doc)?.id;
      add(doc.id, target, relation.kind || 'relation');
    }
  }
  return { version:1, datasetRevision:JSON.stringify(documents.map((doc) => [doc.id, doc.head])), nodes, edges:[...edgeMap.values()] };
}

export function galaxyGraph(tracks, events) {
  const nodes = new Map(tracks.map((track) => [`track:${track.id}`, {
    id:`track:${track.id}`, label:track.name, kind:'track', track,
    selection:{ kind:'track', id:track.id },
  }]));
  const edges = new Map();
  for (const event of events) {
    const sharedTrackIds = Array.isArray(event.sharedTrackIds) ? event.sharedTrackIds : [];
    // A primary-only event is still a first-class Galaxy event.  Shared
    // memberships are projected through one set so duplicate IDs become one
    // typed edge each.
    if (!event.primaryTrackId && !sharedTrackIds.length) continue;
    const id = `event:${event.id}`;
    if (!nodes.has(id)) nodes.set(id, { id, label:event.title, kind:'event', hub:true, task:event, selection:{ kind:'event', id:event.id } });
    const memberships = new Set([event.primaryTrackId, ...sharedTrackIds].filter(Boolean));
    for (const trackId of memberships) {
      const track = `track:${trackId}`;
      if (!nodes.has(track)) continue;
      const type = trackId === event.primaryTrackId ? 'primary' : 'shared';
      const edgeId = JSON.stringify([track, id, type]);
      edges.set(edgeId, { id:edgeId, from:track, to:id, type });
    }
  }
  return {
    version:1,
    datasetRevision:JSON.stringify([
      tracks.map((track) => [track.id, track.head, track.name]),
      [...nodes.values()].filter((node) => node.task).map(({ task }) => [task.id, task.head, task.title, task.startDate, task.dueDate]),
      [...edges.keys()],
    ]),
    nodes:[...nodes.values()], edges:[...edges.values()],
  };
}
