import { assignEventLanes } from './timeline.js';
import {
  DATE_RE,
  DEFAULT_DAY_WIDTH,
  ICONS,
  MAX_DAY_WIDTH,
  MAX_LANE_HEIGHT,
  MIN_DAY_WIDTH,
  MIN_LANE_HEIGHT,
  RANGE_CHUNK_DAYS,
  addDays,
  daysBetween,
  dragPatch,
  eventLayout,
  formatLocalDate,
  glyphFor,
  manipulationGate,
  parseLocalDate,
} from './planningModel.js';
import { createCopalWindow } from './windows.js';

export { MIN_DAY_WIDTH, DEFAULT_DAY_WIDTH, MAX_DAY_WIDTH, MIN_LANE_HEIGHT, MAX_LANE_HEIGHT, RANGE_CHUNK_DAYS, WARM_HISTORY_DAYS, addDays, daysBetween, dragPatch, eventLayout, formatLocalDate, glyphFor, manipulationGate, parseLocalDate } from './planningModel.js';
const MAX_WINDOW_DAYS = 1460;
const LABEL_WIDTH = 190;
const INITIAL_HISTORY_DAYS = 3;
const RANGE_STATE_VERSION = 2;
const COLORS = ['#f97316','#84cc16','#ec4899','#a855f7','#14b8a6','#eab308','#0ea5e9','#b45309','#f59e0b','#6b7280','#dc2626','#22c55e','#10b981','#0891b2','#06b6d4','#f43f5e','#8b5cf6','#facc15'];

function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
function uniqueEvents(data) {
  const values = new Map();
  for (const track of data.tracks || []) for (const event of track.tasks || []) values.set(event.id, { ...event, trackId: event.trackId || track.id });
  for (const event of data.floatingTodos || []) values.set(event.id, event);
  return [...values.values()];
}
function trackMap(data) { return new Map((data.tracks || []).map((track) => [track.id, track])); }
function randomId(prefix) { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`; }
function reducedMotion() { return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches; }

function control(tag, attrs = {}, value = '') {
  const node = document.createElement(tag);
  for (const [key, item] of Object.entries(attrs)) {
    if (key === 'class') node.className = item;
    else if (key === 'text') node.textContent = item;
    else if (item != null) node.setAttribute(key, String(item));
  }
  if (tag === 'textarea') node.value = value ?? '';
  else if ('value' in node) node.value = value ?? '';
  return node;
}

export function createPlanningFeature({ h, api, getPlanning, refresh, setStatus, projectionChanged, openDocument }) {
  const timeline = {
    dayWidth: DEFAULT_DAY_WIDTH,
    laneHeight: 56,
    mode: 'regular',
    condensedStyle: 'dots',
    rangeStart: null,
    rangeEnd: null,
    hiddenTracks: new Set(),
    expandedTracks: new Set(),
    extending: false,
  };
  let workspace = 'default';
  let eventEditor = null;
  let trackEditor = null;
  let editorState = { eventId: null, draft: null, dirty: false, trigger: null };
  let trackState = { trackId: null, draft: null, dirty: false, trigger: null };
  let timelineBody = null;
  let timelineTodayKey = '';
  let timelineRefreshTimer = null;

  function storageKey() { return `odysseus-copal-timeline-v2:${workspace}`; }
  function loadState(nextWorkspace) {
    workspace = nextWorkspace || 'default';
    try {
      const saved = JSON.parse(localStorage.getItem(storageKey()) || '{}');
      timeline.dayWidth = clamp(Number(saved.dayWidth) || DEFAULT_DAY_WIDTH, MIN_DAY_WIDTH, MAX_DAY_WIDTH);
      timeline.laneHeight = clamp(Number(saved.laneHeight) || 56, MIN_LANE_HEIGHT, MAX_LANE_HEIGHT);
      timeline.mode = saved.mode === 'condensed' ? 'condensed' : 'regular';
      timeline.condensedStyle = ['dots','waves','tree'].includes(saved.condensedStyle) ? saved.condensedStyle : 'dots';
      timeline.rangeStart = Number(saved.rangeVersion) === RANGE_STATE_VERSION ? parseLocalDate(saved.rangeStart) : null;
      timeline.rangeEnd = Number(saved.rangeVersion) === RANGE_STATE_VERSION ? parseLocalDate(saved.rangeEnd) : null;
      timeline.hiddenTracks = new Set(Array.isArray(saved.hiddenTracks) ? saved.hiddenTracks : []);
      timeline.expandedTracks = new Set(Array.isArray(saved.expandedTracks) ? saved.expandedTracks : []);
    } catch (_) {}
  }
  function persist(anchorDate = null) {
    let priorAnchor;
    try { priorAnchor = JSON.parse(localStorage.getItem(storageKey()) || '{}').anchorDate; } catch (_) {}
    localStorage.setItem(storageKey(), JSON.stringify({
      dayWidth: timeline.dayWidth,
      laneHeight: timeline.laneHeight,
      mode: timeline.mode,
      condensedStyle: timeline.condensedStyle,
      rangeVersion: RANGE_STATE_VERSION,
      rangeStart: formatLocalDate(timeline.rangeStart),
      rangeEnd: formatLocalDate(timeline.rangeEnd),
      hiddenTracks: [...timeline.hiddenTracks],
      expandedTracks: [...timeline.expandedTracks],
      anchorDate: anchorDate ? formatLocalDate(anchorDate) : priorAnchor,
    }));
  }

  async function patchEvent(event, patch) {
    setStatus('Saving event…');
    try {
      const result = await api(`/planning/events/${encodeURIComponent(event.id)}`, {
        method: 'PATCH', body: JSON.stringify({ patch, base: event.head }),
      });
      projectionChanged(result);
      await refresh();
      setStatus('Event saved');
      return result.event;
    } catch (error) {
      await refresh();
      setStatus(error.status === 409 ? 'Event changed elsewhere. Reloaded the authoritative version.' : error.message, true);
      throw error;
    }
  }

  async function createEvent(defaults = {}) {
    const data = getPlanning();
    const firstTrack = (data.tracks || []).find((track) => track.enabled !== false);
    const event = {
      title: 'Untitled event', description: '', startDate: formatLocalDate(new Date()), dueDate: formatLocalDate(new Date()),
      status: 'pending', priority: 'medium', trackId: firstTrack?.id || null, sharedTrackIds: [], tags: [], stages: [], ...defaults,
    };
    const result = await api('/planning/events', { method: 'POST', body: JSON.stringify({ event }) });
    projectionChanged(result);
    await refresh();
    openEventEditor(result.event?.id || result.doc?.id);
  }

  function ensureEventEditor() {
    if (eventEditor) return eventEditor;
    eventEditor = createCopalWindow({
      id: 'copal-event-editor-modal', label: 'Edit event', subtitle: 'Canonical Redb note', minWidth: 430, minHeight: 520,
      sizeKey: 'odysseus-copal-event-editor-size', className: 'copal-event-editor-window',
      onBeforeClose: () => !editorState.dirty || window.confirm('Discard unsaved event changes?'),
      onClosed: () => { editorState = { eventId:null, draft:null, dirty:false, trigger:null }; },
    });
    eventEditor.actions.replaceChildren();
    return eventEditor;
  }

  function field(label, input, hint = '') {
    const wrapper = h('label', { class: 'copal-form-field' }, h('span', { text: label }), input);
    if (hint) wrapper.append(h('small', { text: hint }));
    return wrapper;
  }

  function markEditorDirty() { editorState.dirty = true; ensureEventEditor().setStatus('Unsaved'); }

  function renderEventEditor() {
    const win = ensureEventEditor();
    const data = getPlanning();
    const source = uniqueEvents(data).find((event) => event.id === editorState.eventId);
    if (!source) {
      win.body.replaceChildren(h('div', { class:'copal-empty', text:'Event no longer exists.' }));
      return;
    }
    const draft = editorState.draft || structuredClone(source);
    editorState.draft = draft;
    win.setTitle(`Edit event · ${draft.title || 'Untitled'}`);
    const tracks = data.tracks || [];
    const form = h('form', { class:'copal-event-form', onsubmit:(event) => event.preventDefault() });
    const badges = h('div', { class:'copal-event-badges' },
      h('span', { class:`copal-badge priority-${draft.priority}`, text:draft.priority || 'medium' }),
      h('span', { class:'copal-badge', text:draft.status || 'pending' }));
    if ((draft.sharedTrackIds || []).length) badges.append(h('span', { class:'copal-badge shared', text:`🔗 ${(draft.sharedTrackIds || []).length + 1} tracks` }));
    if (draft.startDate === 'FUZZY' || draft.fuzzy) badges.append(h('span', { class:'copal-badge fuzzy', text:'? fuzzy' }));
    if ((draft.stages || []).length) badges.append(h('span', { class:'copal-badge', text:`${(draft.stages || []).filter((stage) => stage.done).length}/${draft.stages.length} stages` }));
    form.append(badges);

    const title = control('input', { type:'text', required:'', 'aria-label':'Event title' }, draft.title);
    title.addEventListener('input', () => { draft.title = title.value; markEditorDirty(); win.setTitle(`Edit event · ${title.value || 'Untitled'}`); });
    const description = control('textarea', { rows:'4', 'aria-label':'Event description' }, draft.description);
    description.addEventListener('input', () => { draft.description = description.value; markEditorDirty(); });
    form.append(field('Title', title), field('Description', description));

    const startMode = draft.startDate === 'FUZZY' ? (draft.fuzzy?.fadeIn ? 'fadein' : 'fuzzy') : draft.startDate === 'AUTO' ? 'auto' : 'exact';
    const startDate = control('input', { type:'date', 'aria-label':'Start or anchor date' }, startMode === 'exact' ? draft.startDate : draft.fuzzy?.anchorStart || '');
    startDate.addEventListener('change', () => {
      if (startMode === 'exact') draft.startDate = startDate.value;
      else { draft.fuzzy = { ...(draft.fuzzy || {}), anchorStart:startDate.value }; draft.startDate = startMode === 'auto' ? 'AUTO' : 'FUZZY'; }
      markEditorDirty();
    });
    const modes = h('div', { class:'copal-segmented', role:'group', 'aria-label':'Start date mode' });
    for (const [mode, label] of [['exact','Date'],['fuzzy','? fuzzy'],['fadein','Fade in'],['auto','Auto']]) modes.append(h('button', {
      type:'button', class:`copal-btn${startMode === mode ? ' active' : ''}`, text:label,
      onclick:() => {
        const anchor = startDate.value || formatLocalDate(new Date());
        if (mode === 'exact') { draft.startDate = anchor; draft.fuzzy = draft.fuzzy?.anchorEnd ? { anchorEnd:draft.fuzzy.anchorEnd } : null; }
        else if (mode === 'auto') { draft.startDate = 'AUTO'; draft.fuzzy = draft.fuzzy || {}; }
        else { draft.startDate = 'FUZZY'; draft.fuzzy = { ...(draft.fuzzy || {}), anchorStart:anchor, fadeIn:mode === 'fadein' }; }
        markEditorDirty(); renderEventEditor();
      },
    }));
    const due = control('input', { type:'date', 'aria-label':'Due date' }, draft.dueDate || '');
    due.disabled = draft.dueDate === null;
    due.addEventListener('change', () => { draft.dueDate = due.value || null; markEditorDirty(); });
    const infinite = control('input', { type:'checkbox', 'aria-label':'Fade out with no fixed end' });
    infinite.checked = draft.dueDate === null;
    infinite.addEventListener('change', () => { draft.dueDate = infinite.checked ? null : due.value || formatLocalDate(new Date()); markEditorDirty(); renderEventEditor(); });
    form.append(h('div', { class:'copal-form-grid' },
      h('div', {}, field(startMode === 'exact' ? 'Start date' : 'Anchor date', startDate), modes),
      h('div', {}, field('Due date', due), h('label', { class:'copal-check' }, infinite, 'Fade out (∞)'))));

    const mainTrack = control('select', { 'aria-label':'Main track' });
    mainTrack.append(h('option', { value:'', text:'Unscheduled' }));
    for (const track of tracks) mainTrack.append(h('option', { value:track.id, text:`${glyphFor(track.icon)} ${track.name}` }));
    mainTrack.value = draft.trackId || '';
    mainTrack.addEventListener('change', () => { draft.trackId = mainTrack.value || null; draft.sharedTrackIds = (draft.sharedTrackIds || []).filter((id) => id !== draft.trackId); markEditorDirty(); renderEventEditor(); });
    const shared = h('div', { class:'copal-track-chip-list', role:'group', 'aria-label':'Additional shared tracks' });
    for (const track of tracks.filter((item) => item.id !== draft.trackId)) {
      const active = (draft.sharedTrackIds || []).includes(track.id);
      shared.append(h('button', { type:'button', class:`copal-track-chip${active ? ' active' : ''}`, style:`--track-color:${track.color}`, text:`${glyphFor(track.icon)} ${track.name}`, onclick:() => {
        const ids = new Set(draft.sharedTrackIds || []); active ? ids.delete(track.id) : ids.add(track.id); draft.sharedTrackIds = [...ids]; markEditorDirty(); renderEventEditor();
      } }));
    }
    form.append(field('Main track', mainTrack), field('Also on (shared)', shared));

    const priority = control('select', { 'aria-label':'Priority' });
    for (const value of ['low','medium','high','critical']) priority.append(h('option', { value, text:value }));
    priority.value = draft.priority || 'medium'; priority.addEventListener('change', () => { draft.priority = priority.value; markEditorDirty(); });
    const status = control('select', { 'aria-label':'Status' });
    for (const value of ['pending','in-progress','done','ongoing']) status.append(h('option', { value, text:value }));
    status.value = draft.status || 'pending'; status.addEventListener('change', () => { draft.status = status.value; markEditorDirty(); });
    const tags = control('input', { type:'text', 'aria-label':'Tags', placeholder:'vet, car-ride' }, (draft.tags || []).join(', '));
    tags.addEventListener('input', () => { draft.tags = tags.value.split(',').map((item) => item.trim()).filter(Boolean); markEditorDirty(); });
    form.append(h('div', { class:'copal-form-grid' }, field('Priority', priority), field('Status', status)), field('Tags (comma separated)', tags));

    const stages = h('section', { class:'copal-stage-editor' }, h('header', {}, h('strong', { text:'Stages (sub-events)' }), h('button', { type:'button', class:'copal-btn', text:'+ Add', onclick:() => { (draft.stages ||= []).push({ id:randomId('stage'), title:'New stage', done:false, date:null }); markEditorDirty(); renderEventEditor(); } })));
    for (const [index, stage] of (draft.stages || []).entries()) {
      const done = control('input', { type:'checkbox', 'aria-label':`Complete ${stage.title}` }); done.checked = !!stage.done;
      done.addEventListener('change', () => { stage.done = done.checked; markEditorDirty(); });
      const name = control('input', { type:'text', 'aria-label':`Stage ${index + 1} title` }, stage.title);
      name.addEventListener('input', () => { stage.title = name.value; markEditorDirty(); });
      const date = control('input', { type:'date', 'aria-label':`Stage ${index + 1} date` }, stage.date || '');
      date.addEventListener('change', () => { stage.date = date.value || null; markEditorDirty(); });
      stages.append(h('div', { class:'copal-stage-row' }, done, name, date,
        h('button', { type:'button', class:'copal-btn', text:'↑', disabled:index === 0, 'aria-label':'Move stage up', onclick:() => { [draft.stages[index - 1], draft.stages[index]] = [draft.stages[index], draft.stages[index - 1]]; markEditorDirty(); renderEventEditor(); } }),
        h('button', { type:'button', class:'copal-btn', text:'↓', disabled:index === draft.stages.length - 1, 'aria-label':'Move stage down', onclick:() => { [draft.stages[index + 1], draft.stages[index]] = [draft.stages[index], draft.stages[index + 1]]; markEditorDirty(); renderEventEditor(); } }),
        h('button', { type:'button', class:'copal-btn danger', text:'×', 'aria-label':'Remove stage', onclick:() => { draft.stages.splice(index, 1); markEditorDirty(); renderEventEditor(); } })));
    }
    form.append(stages);

    const advanced = h('details', { class:'copal-event-advanced' }, h('summary', { text:'Compatibility and fade details' }));
    for (const [key, label, type] of [['linkId','Linked event id','text'],['fadeDays','Fade days','number'],['titleStart','Start label','text'],['titleEnd','End label','text']]) {
      const input = control('input', { type, min:type === 'number' ? '0' : null }, draft[key] ?? '');
      input.addEventListener('input', () => { draft[key] = type === 'number' ? Number(input.value || 0) : input.value || null; markEditorDirty(); });
      advanced.append(field(label, input));
    }
    form.append(advanced);

    const actions = h('footer', { class:'copal-dialog-actions copal-event-actions' },
      h('button', { type:'button', class:'copal-btn danger', text:'Delete event', onclick:async() => {
        if (!window.confirm(`Move “${source.title}” to Copal trash?`)) return;
        const result = await api(`/planning/events/${encodeURIComponent(source.id)}`, { method:'DELETE' }); projectionChanged(result); editorState.dirty = false; win.requestClose(); await refresh();
      } }),
      h('button', { type:'button', class:'copal-btn', text:'Open note', onclick:() => openDocument(source.id, 'notes') }),
      h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => win.requestClose() }),
      h('button', { type:'button', class:'copal-btn primary', text:'Save', onclick:async(event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const patch = Object.fromEntries(['title','description','startDate','dueDate','status','priority','trackId','sharedTrackIds','tags','linkId','fuzzy','fadeDays','titleStart','titleEnd','stages','floating'].map((key) => [key, draft[key] ?? null]));
          await patchEvent(source, patch); editorState.dirty = false; const fresh = uniqueEvents(getPlanning()).find((item) => item.id === source.id); editorState.draft = fresh ? structuredClone(fresh) : null; renderEventEditor();
        } finally { button.disabled = false; }
      } }));
    form.append(actions);
    win.body.replaceChildren(form);
    win.setStatus(editorState.dirty ? 'Unsaved' : `Revision ${String(source.head || '').slice(0, 8)}`);
  }

  function openEventEditor(eventId, trigger = document.activeElement) {
    const source = uniqueEvents(getPlanning()).find((event) => event.id === eventId);
    if (!source) return;
    if (editorState.dirty && editorState.eventId !== eventId && !window.confirm('Discard unsaved event changes?')) return;
    editorState = { eventId, draft:structuredClone(source), dirty:false, trigger };
    renderEventEditor(); ensureEventEditor().show(trigger);
  }

  function ensureTrackEditor() {
    if (trackEditor) return trackEditor;
    trackEditor = createCopalWindow({
      id:'copal-track-editor-modal', label:'Edit track', subtitle:'Canonical track registry', minWidth:390, minHeight:430,
      sizeKey:'odysseus-copal-track-editor-size', className:'copal-track-editor-window',
      onBeforeClose:() => !trackState.dirty || window.confirm('Discard unsaved track changes?'),
      onClosed:() => { trackState = { trackId:null, draft:null, dirty:false, trigger:null }; },
    });
    return trackEditor;
  }

  async function saveTracks(nextTracks, base) {
    const data = getPlanning();
    const metadata = Object.fromEntries(Object.entries(data).filter(([key]) => !['tracks','floatingTodos','trackRegistry','migration','diagnostics','canonical','migrationRequired','schemaVersion'].includes(key)));
    const result = await api('/planning/tracks', { method:'PUT', body:JSON.stringify({ tracks:nextTracks.map(({ tasks, ...track }) => track), metadata, base }) });
    projectionChanged(result); await refresh(); return result;
  }

  function unicodeGlyph(value) {
    const match = String(value || '').trim().match(/^U\+([0-9a-f]{1,6})$/i);
    if (!match) return value;
    const point = Number.parseInt(match[1], 16);
    return point <= 0x10ffff && !(point >= 0xd800 && point <= 0xdfff) ? String.fromCodePoint(point) : value;
  }

  function renderTrackEditor() {
    const win = ensureTrackEditor(); const data = getPlanning(); const current = (data.tracks || []).find((track) => track.id === trackState.trackId);
    const draft = trackState.draft || structuredClone(current || { id:randomId('track'), name:'', color:COLORS[0], icon:'📦', enabled:true }); trackState.draft = draft;
    win.setTitle(current ? `Edit track · ${draft.name}` : 'Add track');
    const form = h('form', { class:'copal-track-form', onsubmit:(event) => event.preventDefault() });
    const name = control('input', { type:'text', required:'', placeholder:'Track name' }, draft.name);
    name.addEventListener('input', () => { draft.name = name.value; trackState.dirty = true; win.setStatus('Unsaved'); });
    const colors = h('div', { class:'copal-color-palette', role:'group', 'aria-label':'Track color' });
    for (const color of COLORS) colors.append(h('button', { type:'button', class:`copal-color${draft.color === color ? ' active' : ''}`, style:`--track-color:${color}`, 'aria-label':`Use ${color}`, onclick:() => { draft.color = color; trackState.dirty = true; renderTrackEditor(); } }));
    const emoji = control('input', { type:'text', maxlength:'32', placeholder:'Emoji, icon key, or U+1F4E6', 'aria-label':'Track emoji or Unicode' }, draft.icon);
    emoji.addEventListener('input', () => { draft.icon = unicodeGlyph(emoji.value); trackState.dirty = true; win.setStatus('Unsaved'); });
    const picker = h('div', { class:'copal-emoji-picker', role:'group', 'aria-label':'Common track emoji' });
    for (const glyph of [...Object.values(ICONS), '🧠','📚','🛠️','🏠','💼','❤️','⭐','🌱']) picker.append(h('button', { type:'button', text:glyph, 'aria-label':`Use ${glyph}`, onclick:() => { draft.icon = glyph; trackState.dirty = true; renderTrackEditor(); } }));
    const enabled = control('input', { type:'checkbox', 'aria-label':'Track enabled' }); enabled.checked = draft.enabled !== false;
    enabled.addEventListener('change', () => { draft.enabled = enabled.checked; trackState.dirty = true; win.setStatus('Unsaved'); });
    form.append(field('Name', name), field('Color', colors), field('Icon / emoji / Unicode', h('div', { class:'copal-emoji-field' }, h('span', { class:'copal-emoji-preview', text:glyphFor(draft.icon) }), emoji)), picker, h('label', { class:'copal-check' }, enabled, 'Enabled in planning projections'));
    form.append(h('p', { class:'copal-form-help', text:'Temporary visibility is controlled in Timeline. Disabling a track is durable and does not delete its events.' }));
    form.append(h('footer', { class:'copal-dialog-actions' }, h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => win.requestClose() }), h('button', { type:'button', class:'copal-btn primary', text:'Save', onclick:async(event) => {
      if (!draft.name.trim()) { win.setStatus('Track name is required', true); name.focus(); return; }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const next = (data.tracks || []).map(({ tasks, ...track }) => track);
        const index = next.findIndex((track) => track.id === draft.id);
        index >= 0 ? next.splice(index, 1, draft) : next.push(draft);
        await saveTracks(next, data.trackRegistry?.head); trackState.dirty = false; win.requestClose();
      } catch (error) { win.setStatus(error.message, true); }
      finally { button.disabled = false; }
    } })));
    win.body.replaceChildren(form); win.setStatus(trackState.dirty ? 'Unsaved' : 'Name, color, emoji, and enabled state');
  }

  function openTrackEditor(trackId = null, trigger = document.activeElement) {
    const current = (getPlanning().tracks || []).find((track) => track.id === trackId);
    if (trackState.dirty && !window.confirm('Discard unsaved track changes?')) return;
    trackState = { trackId, draft:structuredClone(current || { id:randomId('track'), name:'', color:COLORS[Math.floor(Math.random() * COLORS.length)], icon:'📦', enabled:true }), dirty:false, trigger };
    renderTrackEditor(); ensureTrackEditor().show(trigger);
  }

  function initialRange(data, events) {
    const today = parseLocalDate(formatLocalDate(new Date()));
    const dates = events.flatMap((event) => [parseLocalDate(event.startDate), parseLocalDate(event.dueDate), parseLocalDate(event.fuzzy?.anchorStart), parseLocalDate(event.fuzzy?.anchorEnd)]).filter(Boolean);
    const earliest = dates.length ? new Date(Math.min(...dates.map(Number))) : today;
    const latest = dates.length ? new Date(Math.max(...dates.map(Number))) : today;
    if (!timeline.rangeStart || !timeline.rangeEnd || timeline.rangeEnd <= timeline.rangeStart) {
      timeline.rangeStart = addDays(today, -INITIAL_HISTORY_DAYS);
      timeline.rangeEnd = new Date(Math.max(Number(addDays(latest, 60)), Number(addDays(today, 240))));
    }
    return { today, earliest, latest };
  }

  function ensureTimelineClock(body) {
    timelineBody = body;
    if (timelineRefreshTimer) return;
    timelineTodayKey = formatLocalDate(new Date());
    timelineRefreshTimer = setInterval(() => {
      if (!timelineBody?.isConnected) {
        clearInterval(timelineRefreshTimer);
        timelineRefreshTimer = null;
        return;
      }
      const nextToday = formatLocalDate(new Date());
      if (nextToday && nextToday !== timelineTodayKey) {
        timelineTodayKey = nextToday;
        renderTimeline(timelineBody);
      }
    }, 60_000);
  }

  function monthSegments(start, totalDays) {
    const values = []; let offset = 0;
    while (offset < totalDays) {
      const date = addDays(start, offset); const next = new Date(date.getFullYear(), date.getMonth() + 1, 1);
      const span = Math.min(totalDays - offset, Math.max(1, daysBetween(date, next)));
      values.push({ offset, span, label:date.toLocaleDateString(undefined, { month:'short', year:'numeric' }) }); offset += span;
    }
    return values;
  }

  function savedAnchor() {
    try { return parseLocalDate(JSON.parse(localStorage.getItem(storageKey()) || '{}').anchorDate); } catch (_) { return null; }
  }

  function eventGesture(node, event, track, scroll, guide, start, dayWidth, rerender, gate) {
    const startDate = parseLocalDate(event.startDate); const dueDate = parseLocalDate(event.dueDate);
    const originalStart = daysBetween(start, startDate); const originalEnd = dueDate ? daysBetween(start, dueDate) + 1 : originalStart + 1;
    const originalLeft = node.style.left; const originalWidth = node.style.width; const originalLabel = node.getAttribute('aria-label');
    let drag = null;
    const escape = (keyboard) => {
      if (keyboard.key !== 'Escape' || !drag) return;
      keyboard.preventDefault(); keyboard.stopPropagation(); cancel();
    };
    const begin = (pointer, mode) => {
      const allowed = mode === 'move' ? gate.movable : mode === 'left' ? gate.resizeLeft : gate.resizeRight;
      if (!allowed || pointer.button !== 0) return;
      pointer.preventDefault(); pointer.stopPropagation(); node.setPointerCapture(pointer.pointerId);
      drag = { mode, x:pointer.clientX, s:originalStart, e:originalEnd, moved:false, preview:null, pointerId:pointer.pointerId };
      node.classList.add('dragging'); node.focus({ preventScroll:true }); window.addEventListener('blur', cancel, { once:true }); window.addEventListener('keydown', escape, true);
    };
    const cancel = () => {
      if (!drag) return;
      const pointerId = drag.pointerId; drag = null; window.removeEventListener('blur', cancel); window.removeEventListener('keydown', escape, true); guide.hidden = true; node.classList.remove('dragging');
      try { if (node.hasPointerCapture(pointerId)) node.releasePointerCapture(pointerId); } catch (_) {}
      node.style.left = originalLeft; node.style.width = originalWidth; node.setAttribute('aria-label', originalLabel);
    };
    if (!gate.movable && !gate.resizeLeft && !gate.resizeRight) {
      node.addEventListener('click', () => openEventEditor(event.id, node));
      node.addEventListener('keydown', (keyboard) => { if (keyboard.key === 'Enter' || keyboard.key === ' ') { keyboard.preventDefault(); openEventEditor(event.id, node); } });
      node.title = [event.title, `${event.startDate || '?'} → ${event.dueDate || '∞'}`, gate.reason || 'Edit this event in the popup.'].join('\n');
      return;
    }
    node.addEventListener('pointerdown', (eventPointer) => begin(eventPointer, eventPointer.target.closest('.copal-resize-left') ? 'left' : eventPointer.target.closest('.copal-resize-right') ? 'right' : 'move'));
    node.addEventListener('pointermove', (pointer) => {
      if (!drag) return; const delta = Math.round((pointer.clientX - drag.x) / dayWidth); if (Math.abs(pointer.clientX - drag.x) > 3) drag.moved = true;
      let s = drag.s; let e = drag.e;
      if (drag.mode === 'move') { s += delta; e += delta; }
      else if (drag.mode === 'left') s = Math.min(drag.s + delta, e - 1);
      else e = Math.max(s + 1, drag.e + delta);
      drag.preview = { s, e };
      node.style.left = `${LABEL_WIDTH + s * dayWidth + 1}px`; node.style.width = `${Math.max(dayWidth, (e - s) * dayWidth) - 2}px`;
      const boundary = drag.mode === 'right' ? e : s; const labelDay = drag.mode === 'right' ? e - 1 : s;
      guide.hidden = false; guide.style.left = `${LABEL_WIDTH + boundary * dayWidth}px`; guide.querySelector('span').textContent = formatLocalDate(addDays(start, labelDay));
      node.setAttribute('aria-label', `${event.title}: ${formatLocalDate(addDays(start, s))} to ${formatLocalDate(addDays(start, e - 1))}`);
    });
    const finish = async (pointer) => {
      if (!drag) return; const current = drag; drag = null; window.removeEventListener('blur', cancel); window.removeEventListener('keydown', escape, true); guide.hidden = true; node.classList.remove('dragging');
      try { if (node.hasPointerCapture(pointer.pointerId)) node.releasePointerCapture(pointer.pointerId); } catch (_) {}
      if (!current.moved || !current.preview) { openEventEditor(event.id, node); rerender(); return; }
      const delta = current.mode === 'move' || current.mode === 'left' ? current.preview.s - current.s : current.preview.e - current.e;
      node.classList.add('saving'); node.setAttribute('aria-busy', 'true');
      try { await patchEvent(event, dragPatch(event, current.mode, delta)); } catch (_) {}
    };
    node.addEventListener('pointerup', finish);
    node.addEventListener('pointercancel', cancel);
    node.addEventListener('lostpointercapture', () => { if (drag) cancel(); });
    node.addEventListener('keydown', async (keyboard) => {
      if (keyboard.key === 'Escape' && drag) { keyboard.preventDefault(); cancel(); return; }
      if (keyboard.key === 'Enter' || keyboard.key === ' ') { keyboard.preventDefault(); openEventEditor(event.id, node); return; }
      if (!keyboard.altKey || !['ArrowLeft','ArrowRight'].includes(keyboard.key)) return;
      keyboard.preventDefault(); const delta = keyboard.key === 'ArrowLeft' ? -1 : 1; const mode = keyboard.shiftKey ? 'right' : 'move';
      if ((mode === 'move' && gate.movable) || (mode === 'right' && gate.resizeRight)) await patchEvent(event, dragPatch(event, mode, delta));
      else setStatus(gate.reason || 'That event cannot be adjusted directly.', true);
    });
    node.title = [event.title, `${event.startDate || '?'} → ${event.dueDate || '∞'}`, gate.reason || 'Drag to move · drag edges to resize · Alt+Arrow adjusts by one day'].join('\n');
  }

  function renderTimeline(body, options = {}) {
    ensureTimelineClock(body);
    const previousScroll = body.querySelector('.copal-timeline-scroll');
    const previousTop = previousScroll?.scrollTop || 0;
    const previousAnchor = previousScroll && timeline.rangeStart
      ? addDays(timeline.rangeStart, Math.floor(Math.max(0, previousScroll.scrollLeft + previousScroll.clientWidth / 2 - LABEL_WIDTH) / timeline.dayWidth))
      : null;
    const data = getPlanning(); const allEvents = uniqueEvents(data); const byTrack = trackMap(data); const { today } = initialRange(data, allEvents);
    const start = timeline.rangeStart; const end = timeline.rangeEnd; const totalDays = Math.max(1, daysBetween(start, end) + 1); const dayWidth = timeline.dayWidth;
    const dayStep = dayWidth < 10 ? 3 : dayWidth < 15 ? 2 : 1;
    const maxKnownEnd = allEvents.map((event) => parseLocalDate(event.dueDate) || parseLocalDate(event.fuzzy?.anchorEnd) || parseLocalDate(event.startDate) || parseLocalDate(event.fuzzy?.anchorStart)).filter(Boolean).sort((a,b) => b-a)[0];
    const autoStart = maxKnownEnd ? addDays(maxKnownEnd, 1) : today;
    const toolbar = h('div', { class:'copal-timeline-toolbar copal-timeline-controls' });
    const button = (text, label, click) => h('button', { class:'copal-btn', type:'button', text, 'aria-label':label, title:label, onclick:click });
    let scroll;
    const rerender = (restore = {}) => renderTimeline(body, restore);
    const zoom = (delta) => {
      const old = timeline.dayWidth; const anchorX = (scroll?.scrollLeft || 0) + (scroll?.clientWidth || 0) / 2 - LABEL_WIDTH; const anchor = addDays(start, Math.max(0, Math.floor(anchorX / old)));
      timeline.dayWidth = clamp(old + delta, MIN_DAY_WIDTH, MAX_DAY_WIDTH); persist(anchor); rerender({ anchorDate:anchor });
    };
    toolbar.append(
      button('+ Event', 'Add event', () => createEvent()), button('+ Track', 'Add track', () => openTrackEditor()),
      button('Today', 'Center today', () => {
        if (today < timeline.rangeStart || today > timeline.rangeEnd) { timeline.rangeStart = addDays(today, -INITIAL_HISTORY_DAYS); timeline.rangeEnd = addDays(today, 365); rerender({ anchorDate:today }); }
        else scroll.scrollTo({ left:Math.max(0, LABEL_WIDTH + daysBetween(start, today) * dayWidth - scroll.clientWidth / 2), behavior:reducedMotion() ? 'auto' : 'smooth' });
      }),
      button('−', 'Zoom out', () => zoom(-2)), h('output', { class:'copal-resolution', text:`${dayWidth}px/day`, 'aria-live':'polite' }), button('+', 'Zoom in', () => zoom(2)),
      button('H−', 'Shorter tracks', () => { timeline.laneHeight = clamp(timeline.laneHeight - 8, MIN_LANE_HEIGHT, MAX_LANE_HEIGHT); persist(); rerender({ left:scroll.scrollLeft }); }),
      h('output', { class:'copal-resolution', text:`${timeline.laneHeight}px` }), button('H+', 'Taller tracks', () => { timeline.laneHeight = clamp(timeline.laneHeight + 8, MIN_LANE_HEIGHT, MAX_LANE_HEIGHT); persist(); rerender({ left:scroll.scrollLeft }); }),
    );
    const mode = control('select', { 'aria-label':'Timeline mode', class:'copal-control-select' });
    mode.append(h('option', { value:'regular', text:'Regular' }), h('option', { value:'condensed', text:'Condensed' })); mode.value = timeline.mode;
    mode.addEventListener('change', () => { timeline.mode = mode.value; persist(); rerender({ left:scroll.scrollLeft }); }); toolbar.append(mode);
    if (timeline.mode === 'condensed') {
      const style = control('select', { 'aria-label':'Condensed timeline style', class:'copal-control-select' });
      for (const value of ['dots','waves','tree']) style.append(h('option', { value, text:value[0].toUpperCase() + value.slice(1) })); style.value = timeline.condensedStyle;
      style.addEventListener('change', () => { timeline.condensedStyle = style.value; persist(); rerender({ left:scroll.scrollLeft }); }); toolbar.append(style);
    }
    const filters = h('details', { class:'copal-track-filters' }, h('summary', { text:'Tracks' }));
    for (const track of data.tracks || []) { const checkbox = control('input', { type:'checkbox' }); checkbox.checked = !timeline.hiddenTracks.has(track.id); checkbox.addEventListener('change', () => { checkbox.checked ? timeline.hiddenTracks.delete(track.id) : timeline.hiddenTracks.add(track.id); persist(); rerender({ left:scroll.scrollLeft }); }); filters.append(h('label', { class:'copal-track-filter' }, checkbox, `${glyphFor(track.icon)} ${track.name}`)); }
    toolbar.append(filters);

    scroll = h('div', { class:'copal-timeline-scroll', tabindex:'0', 'aria-label':'Scrollable timeline. Drag the background, use Shift+mouse wheel, or arrow keys to pan.' });
    const canvas = h('div', { class:'copal-timeline copal-timeline-v2', style:`width:${LABEL_WIDTH + totalDays * dayWidth}px;--copal-day-width:${dayWidth}px;--copal-label-width:${LABEL_WIDTH}px` });
    const corner = h('div', { class:'copal-timeline-corner', text:data.title || 'Timeline' });
    const header = h('header', { class:'copal-date-header' }, corner);
    const months = h('div', { class:'copal-month-row', style:`left:${LABEL_WIDTH}px;width:${totalDays * dayWidth}px` });
    for (const month of monthSegments(start, totalDays)) months.append(h('span', { style:`left:${month.offset * dayWidth}px;width:${month.span * dayWidth}px`, text:month.label }));
    const days = h('div', { class:'copal-day-row', style:`left:${LABEL_WIDTH}px;width:${totalDays * dayWidth}px` });
    for (let offset = 0; offset < totalDays; offset += dayStep) { const date = addDays(start, offset); const isToday = daysBetween(date, today) === 0; days.append(h('span', { class:`${isToday ? 'today ' : ''}${[0,6].includes(date.getDay()) ? 'weekend' : ''}`, style:`left:${offset * dayWidth}px;width:${dayWidth * dayStep}px`, text:String(date.getDate()), title:formatLocalDate(date) })); }
    header.append(months, days); canvas.append(header);
    const todayOffset = daysBetween(start, today);
    if (todayOffset >= 0 && todayOffset < totalDays) canvas.append(h('div', { class:'copal-timeline-today', style:`left:${LABEL_WIDTH + (todayOffset + .5) * dayWidth}px` }, h('span', { text:'TODAY' })));
    const guide = h('div', { class:'copal-resize-guide', hidden:true, role:'status', 'aria-live':'polite' }, h('span'));
    canvas.append(guide);

    const visibleTracks = (data.tracks || []).filter((track) => track.enabled !== false && !timeline.hiddenTracks.has(track.id));
    const rows = timeline.mode === 'regular' ? visibleTracks : [{ id:'__condensed__', name:data.title || 'All events', color:'#14b8a6', icon:'timeline', enabled:true, condensed:true }];
    for (const track of rows) {
      const candidates = (track.condensed ? allEvents : allEvents.filter((event) => event.trackId === track.id || (event.sharedTrackIds || []).includes(track.id))).map((event) => {
        const layout = eventLayout(event, start, autoStart); const startDay = daysBetween(start, layout.start); const endDay = Math.max(startDay, daysBetween(start, layout.end));
        return { event, task:event, startDay, endDay, stableId:event.id };
      }).filter((item) => item.endDay >= 0 && item.startDay < totalDays);
      const lanes = assignEventLanes(candidates); const expanded = !track.condensed && lanes.laneCount > 1 && timeline.expandedTracks.has(track.id);
      const rowHeight = track.condensed ? Math.max(72, timeline.laneHeight) : expanded ? Math.max(timeline.laneHeight, 12 + lanes.laneCount * (timeline.laneHeight - 12)) : timeline.laneHeight;
      const label = h('div', { class:'copal-track-label' }, h('span', { class:'copal-track-dot', style:`--track-color:${track.color}` }), h('span', { class:'copal-track-glyph', text:glyphFor(track.icon) }), h('span', { class:'copal-track-name', text:track.name }));
      if (!track.condensed) label.append(h('button', { class:'copal-track-edit', type:'button', text:'✎', title:`Edit ${track.name}`, 'aria-label':`Edit ${track.name}`, onclick:(event) => openTrackEditor(track.id, event.currentTarget) }));
      if (lanes.laneCount > 1 && !track.condensed) label.append(h('button', { class:'copal-track-overlap', type:'button', text:`${expanded ? '▾' : '▸'} ${lanes.laneCount}`, 'aria-expanded':String(expanded), 'aria-label':`${expanded ? 'Collapse' : 'Expand'} ${track.name}: ${lanes.laneCount} overlapping lanes`, onclick:() => { expanded ? timeline.expandedTracks.delete(track.id) : timeline.expandedTracks.add(track.id); persist(); rerender({ left:scroll.scrollLeft }); } }));
      const row = h('section', { class:`copal-track${expanded ? ' expanded' : ''}${track.condensed ? ` condensed ${timeline.condensedStyle}` : ''}`, style:`height:${rowHeight}px`, 'data-track-id':track.id, 'data-lanes':String(lanes.laneCount), 'aria-label':`${track.name}, ${candidates.length} events, ${lanes.laneCount} lanes` }, label);
      for (const item of lanes.items) {
        const event = item.event; const eventTrack = byTrack.get(event.trackId) || track; const top = track.condensed ? 16 + (item.lane % 3) * 14 : expanded ? 7 + item.lane * (timeline.laneHeight - 12) : 8 + Math.min(item.lane, 5) * 3;
        const left = LABEL_WIDTH + item.startDay * dayWidth; const width = Math.max(dayWidth, (item.endDay - item.startDay + 1) * dayWidth);
        const gate = manipulationGate(event, eventTrack);
        const eventChildren = [h('span', { class:'copal-event-label', text:`${(event.sharedTrackIds || []).length ? '🔗 ' : ''}${event.title}` })];
        if (gate.resizeLeft) eventChildren.unshift(h('span', { class:'copal-resize-handle copal-resize-left', 'aria-hidden':'true' }));
        if (gate.resizeRight) eventChildren.push(h('span', { class:'copal-resize-handle copal-resize-right', 'aria-hidden':'true' }));
        const node = h('div', {
          class:`copal-event${event.startDate === 'FUZZY' || event.fuzzy ? ' fuzzy' : ''}${event.status === 'done' ? ' done' : ''}`,
          role:'button', tabindex:'0', 'data-task-id':event.id, 'data-lane':String(item.lane), style:`left:${left + 1}px;top:${top}px;width:${width - 2}px;--event-color:${eventTrack.color || '#14b8a6'};--stack-index:${item.lane}`,
          'aria-label':`${event.title}, ${event.startDate || 'unscheduled'} to ${event.dueDate || 'open end'}`,
        }, ...eventChildren);
        if ((event.stages || []).length) node.append(h('span', { class:'copal-event-progress', style:`--progress:${Math.round((event.stages.filter((stage) => stage.done).length / event.stages.length) * 100)}%` }));
        eventGesture(node, event, eventTrack, scroll, guide, start, dayWidth, rerender, gate); row.append(node);
      }
      canvas.append(row);
    }
    scroll.append(canvas);
    const extendBackward = () => {
      if (timeline.extending || !timeline.rangeStart) return;
      timeline.extending = true;
      const inserted = RANGE_CHUNK_DAYS;
      timeline.rangeStart = addDays(timeline.rangeStart, -inserted);
      if (daysBetween(timeline.rangeStart, timeline.rangeEnd) > MAX_WINDOW_DAYS) timeline.rangeEnd = addDays(timeline.rangeEnd, -inserted);
      const compensatedLeft = scroll.scrollLeft + inserted * dayWidth;
      persist();
      requestAnimationFrame(() => rerender({ left:compensatedLeft, extensionComplete:true }));
    };
    let pan = null;
    scroll.addEventListener('pointerdown', (event) => { if (event.pointerType === 'touch' || event.target.closest('button,[role="button"],input,select,textarea,a')) return; pan = { x:event.clientX, left:scroll.scrollLeft, id:event.pointerId }; scroll.setPointerCapture(event.pointerId); scroll.classList.add('dragging'); });
    scroll.addEventListener('pointermove', (event) => {
      if (!pan) return;
      const nextLeft = pan.left - (event.clientX - pan.x);
      scroll.scrollLeft = Math.max(0, nextLeft);
      if (nextLeft < 0) extendBackward();
    });
    const endPan = (event) => { if (!pan) return; try { if (scroll.hasPointerCapture(event.pointerId)) scroll.releasePointerCapture(event.pointerId); } catch (_) {} pan = null; scroll.classList.remove('dragging'); const center = addDays(start, Math.floor(Math.max(0, scroll.scrollLeft + scroll.clientWidth / 2 - LABEL_WIDTH) / dayWidth)); persist(center); };
    scroll.addEventListener('pointerup', endPan); scroll.addEventListener('pointercancel', endPan);
    scroll.addEventListener('keydown', (event) => {
      const amount = event.key === 'PageUp' || event.key === 'PageDown' ? scroll.clientWidth * .8 : 80;
      if (['ArrowLeft','PageUp'].includes(event.key)) { event.preventDefault(); if (scroll.scrollLeft <= 0) extendBackward(); else scroll.scrollLeft -= amount; }
      if (['ArrowRight','PageDown'].includes(event.key)) { event.preventDefault(); scroll.scrollLeft += amount; }
      if (event.key === 'Home') { event.preventDefault(); scroll.scrollLeft = 0; extendBackward(); }
    });
    scroll.addEventListener('wheel', (event) => {
      const towardPast = event.deltaX < 0 || (event.shiftKey && event.deltaY < 0);
      if (towardPast && scroll.scrollLeft <= 2) extendBackward();
    }, { passive:true });
    scroll.addEventListener('scroll', () => {
      if (timeline.extending || scroll.scrollLeft >= Math.max(260, scroll.clientWidth * .35)) return;
      extendBackward();
    }, { passive:true });
    body.replaceChildren(toolbar, scroll);
    scroll.scrollTop = Number.isFinite(options.top) ? options.top : previousTop;
    if (Number.isFinite(options.left)) {
      scroll.scrollLeft = options.left;
      if (options.extensionComplete) requestAnimationFrame(() => { timeline.extending = false; });
    } else {
      const anchor = options.anchorDate || previousAnchor || savedAnchor() || today;
      scroll.scrollLeft = Math.max(0, LABEL_WIDTH + daysBetween(start, anchor) * dayWidth - scroll.clientWidth / 2);
    }
  }

  function renderTodo(body, markdownItems = []) {
    const data = getPlanning(); const byTrack = trackMap(data); const events = uniqueEvents(data);
    const root = h('section', { class:'copal-meatbag-tasks copal-pane' }, h('header', { class:'copal-pane-header' }, h('strong', { text:`Meatbag Tasks · ${events.length + markdownItems.length}` }), h('button', { class:'copal-btn primary', text:'+ Task', onclick:() => createEvent({ startDate:null, dueDate:null, trackId:null, floating:true }) })));
    for (const event of events) {
      const checkbox = control('input', { type:'checkbox', 'aria-label':`Complete ${event.title}` }); checkbox.checked = event.status === 'done'; checkbox.addEventListener('change', async() => { checkbox.disabled = true; try { await patchEvent(event, { status:checkbox.checked ? 'done' : 'pending' }); } catch (_) { checkbox.checked = !checkbox.checked; } });
      root.append(h('div', { class:`copal-task-row${checkbox.checked ? ' done' : ''}` }, checkbox, h('button', { class:'copal-task-title', text:event.title, onclick:(click) => openEventEditor(event.id, click.currentTarget) }), h('small', { text:event.trackId ? `${glyphFor(byTrack.get(event.trackId)?.icon)} ${byTrack.get(event.trackId)?.name || 'Unknown track'}` : 'Unscheduled' })));
    }
    for (const item of markdownItems) root.append(item);
    if (!events.length && !markdownItems.length) root.append(h('div', { class:'copal-empty', text:'No Meatbag Tasks yet.' }));
    body.replaceChildren(root);
  }

  return { loadState, renderTimeline, renderTodo, openEventEditor, openTrackEditor, createEvent, patchEvent, glyphFor, get timelineState() { return timeline; } };
}
