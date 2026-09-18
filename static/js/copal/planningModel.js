export const MIN_DAY_WIDTH = 8;
export const DEFAULT_DAY_WIDTH = 18;
export const MAX_DAY_WIDTH = 56;
export const MIN_LANE_HEIGHT = 32;
export const MAX_LANE_HEIGHT = 160;
export const RANGE_CHUNK_DAYS = 60;
export const WARM_HISTORY_DAYS = 60;
export const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
export const ICONS = { cat:'🐱', dog:'🐶', truck:'🚚', car:'🚗', bug:'🐛', sun:'☀️', fence:'🚧', ant:'🐜', toad:'🐸', hammock:'🛶', bucket:'🪣', broom:'🧹', box:'📦', clown:'🤡' };

export function parseLocalDate(value) {
  if (!DATE_RE.test(String(value || ''))) return null;
  const [year, month, day] = String(value).split('-').map(Number);
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : null;
}

export function formatLocalDate(value) {
  const date = value instanceof Date ? value : parseLocalDate(value);
  if (!date) return '';
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
}

export function addDays(value, amount) {
  const date = value instanceof Date ? new Date(value) : parseLocalDate(value);
  if (!date) return null;
  date.setDate(date.getDate() + amount);
  return date;
}

export function daysBetween(from, to) {
  const a = from instanceof Date ? from : parseLocalDate(from);
  const b = to instanceof Date ? to : parseLocalDate(to);
  if (!a || !b) return 0;
  return Math.round((Date.UTC(b.getFullYear(), b.getMonth(), b.getDate()) - Date.UTC(a.getFullYear(), a.getMonth(), a.getDate())) / 86400000);
}

export function glyphFor(icon) {
  const value = String(icon || '').trim();
  return ICONS[value] || value || '•';
}

// Timeline is a semantic owner.  These small DTO helpers deliberately keep
// Files references out of the planning model; a ResourceKey is resolved by
// the Files facade only at the point an attachment is prepared.
export function timelineEventHandle(event, { accountId, workspace = 'default', expectedHead = null } = {}) {
  const eventId = String(event?.id || event?.documentId || '').trim();
  const account = String(accountId || '').trim();
  const scope = String(workspace || 'default').trim();
  if (!eventId || eventId.length > 256) throw new TypeError('Timeline event id is required');
  if (!account || account.length > 256) throw new TypeError('Timeline account is required');
  if (!/^[A-Za-z0-9._-]{1,64}$/.test(scope)) throw new TypeError('Timeline workspace is invalid');
  const head = expectedHead ?? event?.head ?? null;
  if (head != null && (typeof head !== 'string' && typeof head !== 'number')) throw new TypeError('Timeline event head is invalid');
  const resourceKey = event?.resourceKey || event?.resource_key || null;
  if (resourceKey != null && (typeof resourceKey !== 'object' || Array.isArray(resourceKey))) throw new TypeError('Timeline resource key is invalid');
  return Object.freeze({
    kind: 'copal-planning-event', eventId, accountId: account, workspace: scope,
    expectedHead: head == null ? null : String(head), expectedRevision: head == null ? null : String(head), currentTrackId: event?.trackId || null,
    currentEventIdentity: Object.freeze({ id: eventId, trackId: event?.trackId || null }),
    resourceKey: resourceKey ? Object.freeze({ ...resourceKey }) : null,
  });
}

export function timelineEventResourceKey(handle) {
  if (!handle || handle.kind !== 'copal-planning-event' || !handle.eventId) throw new TypeError('Timeline event handle is invalid');
  if (handle.resourceKey) return { ...handle.resourceKey };
  // This is a canonical Copal key, not a Files ref.  S01 must resolve it
  // before any writable operation is attempted.
  return { provider: 'copal', account_id: handle.accountId, workspace_id: handle.workspace, resource_id: handle.eventId };
}

export function timelineSemanticCommand(type, payload = {}) {
  const command = String(type || '').trim();
  if (!['timeline.event.reposition', 'timeline.event.resize', 'timeline.event.nudge', 'timeline.track.reorder', 'timeline.viewport.pan'].includes(command)) {
    throw new TypeError('Timeline command is unsupported');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new TypeError('Timeline command payload is invalid');
  return Object.freeze({ type: command, payload: Object.freeze({ ...payload }) });
}

export function timelineSourceClassification(source) {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return { supported: false, reason: 'This source cannot be attached to an event.' };
  const kind = String(source.kind || source.type || '').trim().toLowerCase();
  if (source.isDirectory || source.directory || kind === 'directory' || kind === 'folder') return { supported: false, reason: 'Folders cannot be attached to an event.' };
  const ref = String(source.resourceRef || source.resource_ref || '').trim();
  const provider = String(source.provider || source.resource?.provider || '').trim().toLowerCase();
  const readable = source.readable ?? source.capabilities?.read ?? source.capabilities?.open ?? source.capabilities?.download;
  if (!ref || (readable === false) || (!['host', 'copal', 'gallery', 'library'].includes(provider) && provider)) return { supported: false, reason: 'This resource cannot be read by the current account.' };
  if (!ref) return { supported: false, reason: 'Choose a readable Files resource.' };
  return { supported: true, resourceRef: ref, provider: provider || null, expectedRevision: source.expectedRevision || source.expected_revision || null };
}

function cloneTracks(tracks) {
  return structuredClone(tracks);
}

export function normalizeTrackHierarchy(tracks) {
  if (!Array.isArray(tracks)) throw new Error('tracks must be a list');
  const normalized = cloneTracks(tracks);
  const byId = new Map();
  for (const track of normalized) {
    if (!track || typeof track !== 'object' || Array.isArray(track)) throw new Error('Each track must be an object');
    const id = String(track.id || '').trim();
    if (!id || byId.has(id)) throw new Error('Tracks need unique ids');
    track.id = id;
    if (track.parentTrackId == null) track.parentTrackId = null;
    else if (typeof track.parentTrackId !== 'string' || !track.parentTrackId.trim()) throw new Error(`Track ${id} parentTrackId must be a non-empty string or null`);
    else track.parentTrackId = track.parentTrackId.trim();
    byId.set(id, track);
  }
  for (const track of normalized) {
    if (track.parentTrackId === track.id) throw new Error(`Track ${track.id} cannot parent itself`);
    if (track.parentTrackId !== null && !byId.has(track.parentTrackId)) throw new Error(`Track ${track.id} has unknown parent ${track.parentTrackId}`);
  }
  const settled = new Set();
  for (const track of normalized) {
    let current = track.id;
    const chain = new Set();
    while (current !== null && !settled.has(current)) {
      if (chain.has(current)) throw new Error('Track hierarchy contains a cycle');
      chain.add(current);
      current = byId.get(current).parentTrackId;
    }
    for (const id of chain) settled.add(id);
  }
  return normalized;
}

export function flattenTrackPreorder(tracks) {
  const normalized = normalizeTrackHierarchy(tracks);
  const children = new Map([[null, []]]);
  for (const track of normalized) {
    if (!children.has(track.parentTrackId)) children.set(track.parentTrackId, []);
    children.get(track.parentTrackId).push(track);
  }
  const ordered = [];
  const stack = [...children.get(null)].reverse();
  while (stack.length) {
    const track = stack.pop();
    ordered.push(track);
    const descendants = children.get(track.id) || [];
    for (let index = descendants.length - 1; index >= 0; index--) stack.push(descendants[index]);
  }
  return ordered;
}

export function trackDescendantIds(tracks, trackId) {
  const normalized = normalizeTrackHierarchy(tracks);
  const id = String(trackId || '').trim();
  if (!normalized.some((track) => track.id === id)) throw new Error(`Unknown track: ${id}`);
  const children = new Map();
  for (const track of normalized) {
    if (track.parentTrackId === null) continue;
    if (!children.has(track.parentTrackId)) children.set(track.parentTrackId, []);
    children.get(track.parentTrackId).push(track.id);
  }
  const result = [];
  const stack = [...(children.get(id) || [])].reverse();
  while (stack.length) {
    const childId = stack.pop();
    result.push(childId);
    const nested = children.get(childId) || [];
    for (let index = nested.length - 1; index >= 0; index--) stack.push(nested[index]);
  }
  return result;
}

export function reparentTrackSubtree(tracks, movedTrackId, parentTrackId) {
  const original = cloneTracks(tracks);
  const ordered = flattenTrackPreorder(tracks);
  const movedId = String(movedTrackId || '').trim();
  const byId = new Map(ordered.map((track) => [track.id, track]));
  if (!byId.has(movedId)) throw new Error(`Unknown moved track: ${movedId}`);
  const parentId = parentTrackId == null ? null : typeof parentTrackId === 'string' ? parentTrackId.trim() : undefined;
  if (parentId === undefined || parentId === '') throw new Error('Target parentTrackId must be a non-empty string or null');
  if (parentId !== null && !byId.has(parentId)) throw new Error(`Unknown target parent: ${parentId}`);
  const subtreeIds = [movedId, ...trackDescendantIds(ordered, movedId)];
  const subtreeSet = new Set(subtreeIds);
  if (parentId === movedId) throw new Error(`Track ${movedId} cannot parent itself`);
  if (parentId !== null && subtreeSet.has(parentId)) throw new Error(`Track ${movedId} cannot move beneath its descendant ${parentId}`);
  if (byId.get(movedId).parentTrackId === parentId) return original;

  byId.get(movedId).parentTrackId = parentId;
  const subtree = subtreeIds.map((id) => byId.get(id));
  const remaining = ordered.filter((track) => !subtreeSet.has(track.id));
  if (parentId === null) return flattenTrackPreorder([...remaining, ...subtree]);
  const targetSubtree = new Set([parentId, ...trackDescendantIds(remaining, parentId)]);
  let insertAt = 0;
  for (let index = 0; index < remaining.length; index++) if (targetSubtree.has(remaining[index].id)) insertAt = index + 1;
  return flattenTrackPreorder([...remaining.slice(0, insertAt), ...subtree, ...remaining.slice(insertAt)]);
}

export function trackBreadcrumb(tracks, trackId) {
  const normalized = normalizeTrackHierarchy(tracks);
  const byId = new Map(normalized.map((track) => [track.id, track]));
  let current = byId.get(String(trackId || '').trim());
  if (!current) throw new Error(`Unknown track: ${trackId}`);
  const names = [];
  while (current) {
    names.push(String(current.name || current.id));
    current = current.parentTrackId === null ? null : byId.get(current.parentTrackId);
  }
  return names.reverse().join(' / ');
}

export function effectiveTrackState(tracks, trackId, { hiddenTracks = new Set(), collapsedTrackGroups = new Set() } = {}) {
  const normalized = normalizeTrackHierarchy(tracks);
  const byId = new Map(normalized.map((track) => [track.id, track]));
  const track = byId.get(String(trackId || '').trim());
  if (!track) throw new Error(`Unknown track: ${trackId}`);
  const hidden = hiddenTracks instanceof Set ? hiddenTracks : new Set(hiddenTracks || []);
  const collapsed = collapsedTrackGroups instanceof Set ? collapsedTrackGroups : new Set(collapsedTrackGroups || []);
  let ancestor = track.parentTrackId === null ? null : byId.get(track.parentTrackId);
  let ancestorDisabledBy = null;
  let ancestorHiddenBy = null;
  let ancestorCollapsedBy = null;
  while (ancestor) {
    if (!ancestorDisabledBy && ancestor.enabled === false) ancestorDisabledBy = ancestor.id;
    if (!ancestorHiddenBy && hidden.has(ancestor.id)) ancestorHiddenBy = ancestor.id;
    if (!ancestorCollapsedBy && collapsed.has(ancestor.id)) ancestorCollapsedBy = ancestor.id;
    ancestor = ancestor.parentTrackId === null ? null : byId.get(ancestor.parentTrackId);
  }
  const ownEnabled = track.enabled !== false;
  const ownHidden = hidden.has(track.id);
  const suppressedBy = !ownEnabled ? track.id : ownHidden ? track.id : ancestorDisabledBy || ancestorHiddenBy || ancestorCollapsedBy;
  return {
    ownEnabled,
    ownHidden,
    collapsed: collapsed.has(track.id),
    ancestorDisabledBy,
    ancestorHiddenBy,
    ancestorCollapsedBy,
    suppressedBy,
    visible: suppressedBy === null,
  };
}

export function eventLayout(event, fallbackStart, autoStart = null) {
  let start = parseLocalDate(event.startDate);
  if (event.startDate === 'FUZZY') start = parseLocalDate(event.fuzzy?.anchorStart);
  if (event.startDate === 'AUTO') start = autoStart;
  start ||= parseLocalDate(event.fuzzy?.anchorStart) || fallbackStart;
  let end = parseLocalDate(event.dueDate) || parseLocalDate(event.fuzzy?.anchorEnd);
  end ||= start;
  return { start, end };
}

export function manipulationGate(event, track) {
  const fuzzy = event.startDate === 'FUZZY' || !!event.fuzzy?.anchorEnd || !!event.fuzzy?.whiskerStart;
  const hardStart = DATE_RE.test(String(event.startDate || ''));
  const hammock = !!track?.special;
  return {
    movable: hardStart && !fuzzy && !hammock,
    resizeLeft: hardStart && !fuzzy && !hammock,
    resizeRight: hardStart && DATE_RE.test(String(event.dueDate || '')) && !fuzzy && !hammock,
    reason: hammock ? 'Hammock events are edited in the event popup.' : fuzzy ? 'Fuzzy/whisker events are edited in the event popup.' : !hardStart ? 'Choose an exact start date in the event popup.' : '',
  };
}

export function dragPatch(event, mode, deltaDays) {
  const start = parseLocalDate(event.startDate);
  if (!start) return {};
  const due = parseLocalDate(event.dueDate);
  if (mode === 'move') return { startDate: formatLocalDate(addDays(start, deltaDays)), ...(due ? { dueDate: formatLocalDate(addDays(due, deltaDays)) } : {}) };
  if (mode === 'left') {
    const next = addDays(start, deltaDays);
    return due && next > due ? { startDate: formatLocalDate(due) } : { startDate: formatLocalDate(next) };
  }
  if (mode === 'right' && due) {
    const next = addDays(due, deltaDays);
    return { dueDate: formatLocalDate(next < start ? start : next) };
  }
  return {};
}

export const MAX_RECURRENCE_EXPANSION = 500;

function monthEnd(year, month) {
  return new Date(year, month + 1, 0).getDate();
}

function nextRecurrenceDate(current, frequency, interval) {
  const d = new Date(current);
  if (frequency === 'daily') { d.setDate(d.getDate() + interval); }
  else if (frequency === 'weekly') { d.setDate(d.getDate() + interval * 7); }
  else if (frequency === 'monthly') {
    let year = d.getFullYear() + Math.floor((d.getMonth() + interval) / 12);
    let month = (d.getMonth() + interval) % 12;
    d.setDate(Math.min(d.getDate(), monthEnd(year, month)));
    d.setFullYear(year);
    d.setMonth(month);
  }
  return d;
}

export function expandRecurrence(event, windowStart, windowEnd) {
  const rec = event.recurrence;
  if (!rec) return [];
  const seriesStart = parseLocalDate(event.startDate);
  if (!seriesStart) return [];
  const durationDays = event.dueDate ? daysBetween(event.startDate, event.dueDate) : 0;
  const exceptions = new Set(rec.exceptionDates || []);
  const occurrences = [];
  let current = new Date(seriesStart);
  let count = 0;
  const until = rec.until ? parseLocalDate(rec.until) : null;
  while (occurrences.length < MAX_RECURRENCE_EXPANSION) {
    if (rec.count != null && count >= rec.count) break;
    if (until && current > until) break;
    if (current >= windowStart && current <= windowEnd) {
      const key = formatLocalDate(current);
      if (!exceptions.has(key)) {
        const due = durationDays ? addDays(current, durationDays) : null;
        occurrences.push({
          eventId: event.id,
          occurrenceKey: key,
          startDate: key,
          dueDate: due ? formatLocalDate(due) : null,
          allDay: !!rec.allDay,
        });
      }
    }
    current = nextRecurrenceDate(current, rec.frequency || 'weekly', rec.interval || 1);
    count++;
    if (current > windowEnd && (rec.count != null || until)) break;
  }
  return occurrences;
}
