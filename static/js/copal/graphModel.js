import { cloneEnvelope, resourceKeyId } from './resourceModel.js';

// Graph, Galaxy and Mind are presentations of one scoped window.  Mind keeps
// its own local tree selection/camera state while sharing this mode envelope.
export const GRAPH_MODES = ['documents', 'galaxy', 'mind'];
const KINDS = ['note', 'wiki', 'base', 'event', 'track'];
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
  return { text, lines, newline, entries };
}

export function headingEntries(text) {
  return markdownLineMap(text).entries.map(({ line, level, text:headingText }) => ({ line, level, text:headingText }));
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
  for (const mode of GRAPH_MODES) {
    const saved = source?.modes?.[mode] || {};
    const camera = Object.fromEntries(Object.entries(CAMERA).map(([key, fallback]) => {
      const value = Number(saved.camera?.[key]);
      return [key, Number.isFinite(value) && (!['w', 'h'].includes(key) || value >= 10 && value <= 100000) ? value : fallback];
    }));
    modes[mode] = {
      camera,
      selection: saved.selection && typeof saved.selection === 'object' ? cloneEnvelope(saved.selection) : null,
      navigation: mode === 'mind' ? {
        docId: saved.navigation?.docId ? String(saved.navigation.docId) : null,
        collapsed: Array.isArray(saved.navigation?.collapsed) ? [...new Set(saved.navigation.collapsed.map(Number).filter(Number.isSafeInteger))] : [],
        selectedLine: Number.isSafeInteger(Number(saved.navigation?.selectedLine)) ? Number(saved.navigation.selectedLine) : null,
        editingLine: Number.isSafeInteger(Number(saved.navigation?.editingLine)) ? Number(saved.navigation.editingLine) : null,
        draggingLine: null,
        draggingPayload: null,
      } : null,
      filters:{
        search:String(saved.filters?.search || '').slice(0, 512),
        kinds:Array.isArray(saved.filters?.kinds) ? [...new Set(saved.filters.kinds.filter((kind) => KINDS.includes(kind)))] : mode === 'galaxy' ? ['track', 'event'] : ['note', 'wiki', 'base', 'event'],
      },
    };
  }
  // One source envelope belongs to the scoped Graph family.  Mode-specific
  // selection remains in `modes`, while this identity survives a Documents →
  // Mind projection switch and can explicitly describe an unavailable source.
  const sharedSource = source?.source && typeof source.source === 'object' ? cloneEnvelope(source.source) : null;
  return { version:1, mode:GRAPH_MODES.includes(source?.mode) ? source.mode : 'documents', source:sharedSource, modes };
}

export function graphStorageKey(accountId, workspace) {
  if (!accountId || !workspace) return null;
  return `copal-graph:v1:${encodeURIComponent(accountId)}:${encodeURIComponent(workspace)}`;
}

function documentKind(doc) {
  return doc.kind === 'wiki' ? 'wiki' : doc.kind === 'base' ? 'base' : doc.kind === 'copal-event' ? 'event' : 'note';
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
