// Clankshop pure model: visible chats, actor stations and stable worker colours.
// No DOM, no fetch, no timers. The inputs are the shapes the existing reads already return:
//   chat list rows (sessions.js), GET /api/orchestration/{chat}/observations/native_actor,
//   GET /api/orchestration/{chat}/companion-observation, and permission_request payloads from the chat stream.

export const MAX_CLANKS = 8;
export const BODY_COLOURS = Object.freeze(['#F6BE48', '#FF776E', '#ED6AB0', '#B7A7E8']);
export const ROOM_LABELS = Object.freeze({
  intake: 'Intake and scanning', assembly: 'Assembly', calibration: 'Calibration', bench: 'Workbench',
  dispatch: 'Dispatch', attention: 'Attention', rest: 'Recharge', repair: 'Service and repair',
});
export const ROOMS = Object.freeze(Object.keys(ROOM_LABELS));
// Lower number = shown first when more than MAX_CLANKS exist: things that need the owner come first.
const PRIORITY = Object.freeze({ permission: 0, question: 1, failed: 2, working: 3, starting: 4, done: 5, idle: 6, unavailable: 7 });
export const STATE_ROOM = Object.freeze({
  permission: 'attention', question: 'attention', failed: 'repair', working: 'bench',
  starting: 'dispatch', done: 'rest', idle: 'rest', unavailable: 'rest',
});
const TOOL_STATIONS = new Map([
  ['read', 'intake'], ['grep', 'intake'], ['glob', 'intake'],
  ['write', 'assembly'], ['edit', 'assembly'], ['bash', 'calibration'],
]);

/** The Archive currently projects an allowlisted tool label and its provenance. */
export function toolStation(label, labelBasis) {
  if (labelBasis !== 'reported_tool_name' || typeof label !== 'string' || !label.startsWith('Tool: ')) return 'bench';
  return TOOL_STATIONS.get(label.slice(6)) || 'bench';
}

export const stationFor = unit => unit.state === 'working' && unit.currentStation
  ? unit.currentStation : STATE_ROOM[unit.state] || 'rest';
export const STATE_TEXT = Object.freeze({
  permission: 'waiting for you to allow or reject a request',
  question: 'waiting for your answer to a question',
  failed: 'last run failed',
  working: 'working',
  starting: 'getting ready',
  done: 'finished its last run',
  idle: 'resting',
  unavailable: 'not reporting (engine not running or no live data)',
});

/** Stable colour from an id: the same id always gets the same body colour. */
export function colourFor(id) {
  let hash = 2166136261;
  for (const ch of String(id ?? '')) { hash ^= ch.codePointAt(0); hash = Math.imul(hash, 16777619) >>> 0; }
  return BODY_COLOURS[hash % BODY_COLOURS.length];
}

const NOT_SHOWN_NAMES = new Set(['nobody', 'incognito']);
const stamp = (s) => s.last_message_at || s.updated_at || s.created_at || '';

/**
 * The owner's chats the scene may show: never archived, incognito or assistant-folder chats.
 * Favourites ("important") first, then the most recent. `incognitoIds` is the browser's own list.
 */
export function chooseChats(rows, { incognitoIds = [], limit = MAX_CLANKS } = {}) {
  const hidden = new Set(incognitoIds);
  const usable = (Array.isArray(rows) ? rows : []).filter((s) => s && typeof s.id === 'string' && s.id
    && !s.archived && !s.incognito && !hidden.has(s.id) && s.folder !== 'Assistant'
    && !NOT_SHOWN_NAMES.has(String(s.name || '').trim().toLowerCase()));
  const byRecent = [...usable].sort((a, b) => (stamp(a) < stamp(b) ? 1 : stamp(a) > stamp(b) ? -1 : 0));
  const ordered = [...byRecent.filter((s) => s.is_important), ...byRecent.filter((s) => !s.is_important)];
  return { shown: ordered.slice(0, limit), more: Math.max(0, ordered.length - limit) };
}

const actorItems = (obs) => (obs?.sources?.native_actor?.items || []).filter((i) => i?.sourceRef?.kind === 'native_actor' && typeof i.sourceRef.nativeId === 'string' && typeof i.sourceRef.sessionId === 'string' && Number.isSafeInteger(i.sourceRef.executionRevision));
const actorKey = (ref) => JSON.stringify([ref.sessionId, ref.nativeId]);
const companionActors = (companion) => new Map((companion?.actors || []).filter((a) => a?.sourceRef?.nativeId && a.sourceRef.sessionId).map((a) => [actorKey(a.sourceRef), a]));
const questionActors = (companion) => new Map((companion?.requests?.items || [])
  .filter((r) => r?.observedState === 'inputneeded' && r.actorRef?.nativeId && r.actorRef.sessionId).map((r) => [actorKey(r.actorRef), r.requestRef]));

/** One actor's scene state. Permission and question outrank everything; then failure; then what the engine reports. */
export function unitState({ status, outcome, companionState, question = false, permission = false }) {
  if (permission) return 'permission';
  if (question || companionState === 'inputneeded') return 'question';
  if (companionState === 'error' || outcome === 'failure') return 'failed';
  const live = companionState === 'working' || status === 'running' ? 'working'
    : companionState === 'waiting' || status === 'pending' ? 'starting'
    : companionState === 'idle' || status === 'idle' ? 'idle' : 'unavailable';
  return live === 'idle' && outcome === 'success' ? 'done' : live;
}

/**
 * Units for one chat. Without live data (switch off, or a read failed) the chat is one resting clank
 * carrying the chat title. With it, one clank per engine actor, named by the actor display name.
 */
export function chatUnits(chat, { actors = null, companion = null, permission = null, account = '' } = {}) {
  const base = { chatId: chat.id, chatTitle: String(chat.name || 'Untitled chat') };
  const scope = actors?.scope, companionSource = companion?.source;
  // Independent reads must describe the same binding; a rebind cannot join stale actor and request data.
  const joined = scope?.hostChatId === chat.id && companionSource && scope.mapRevision === companionSource.mapRevision
    && scope.mappingRevision === companionSource.bindingRevision;
  const items = joined ? actorItems(actors) : [];
  if (!items.length) {
    return [{ ...base, id: JSON.stringify([account, chat.id, 'unavailable']), name: base.chatTitle, state: permission ? 'permission' : 'unavailable', colour: colourFor(chat.id), live: false }];
  }
  const live = companionActors(companion), asked = questionActors(companion);
  return items.map((item) => {
    const ref = item.sourceRef, key = actorKey(ref), actorId = ref.nativeId;
    const id = JSON.stringify([account, chat.id, ref.sessionId, actorId]);
    const current = live.get(key);
    const sameRun = current?.sourceRef?.executionRevision === ref.executionRevision;
    const display = item.actorDisplay && typeof item.actorDisplay.label === 'string' ? item.actorDisplay.label : '';
    const parent = (item.links || []).find((link) => link.kind === 'actor_parent' && link.target?.kind === 'native_actor' && link.target.sessionId)?.target || null;
    const permit = permission && permission.actor_id === actorId && permission.session_id === ref.sessionId;
    return { ...base, id, accountId: account, actorId, sessionId: ref.sessionId, permissionRef: permit ? permission.request_id : null, executionRevision: ref.executionRevision,
      parentRef: parent, requestRef: sameRun ? asked.get(key) || null : null,
      name: String(display || actorId).slice(0, 80), live: true, colour: colourFor(id),
      state: unitState({ status: item.nativeStatus, outcome: item.nativeOutcome, companionState: sameRun ? current.state : null,
        question: sameRun && asked.has(key), permission: !!permit }) };
  });
}

/** Scene units: sorted by need, capped; everything beyond the cap is counted and still listed in text. */
export function sceneUnits(allUnits, cap = MAX_CLANKS) {
  const sorted = allUnits.map((u, i) => ({ u, i })).sort((a, b) => (PRIORITY[a.u.state] - PRIORITY[b.u.state]) || (a.i - b.i)).map((x) => x.u);
  return { drawn: sorted.slice(0, cap), extra: Math.max(0, sorted.length - cap), all: allUnits };
}

/** Keep a worker's slot while it stays in the same station. Free slots are reusable. */
export function placeUnits(drawn, previous = []) {
  const prior = new Map(previous.map(unit => [unit.id, unit]));
  const stationSlots = new Map(ROOMS.map(room => [room, new Set()]));
  const placed = drawn.map(unit => {
    const room = stationFor(unit), old = prior.get(unit.id);
    const slot = old?.room === room ? old.slot : null;
    if (slot !== null) stationSlots.get(room).add(slot);
    return { ...unit, room, slot };
  });
  for (const unit of placed) {
    if (unit.slot !== null) continue;
    const occupied = stationSlots.get(unit.room);
    let slot = 0;
    while (occupied.has(slot)) slot++;
    occupied.add(slot);
    unit.slot = slot;
  }
  return placed;
}

/** Live-state line shown above the scene. */
export function liveLine({ enabled, loaded, failures }) {
  if (!enabled) return 'Live observation is off. Turn it on to load worker states, or use Debug to preview activity.';
  if (!loaded) return 'Live observation is on. Waiting for a scoped snapshot.';
  return failures ? `Live states on. ${failures} chat${failures === 1 ? '' : 's'} did not report (engine not running).` : 'Live states on.';
}
