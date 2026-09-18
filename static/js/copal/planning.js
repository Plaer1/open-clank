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
  flattenTrackPreorder,
  formatLocalDate,
  glyphFor,
  manipulationGate,
  normalizeTrackHierarchy,
  parseLocalDate,
  reparentTrackSubtree,
  timelineEventHandle,
  timelineEventResourceKey,
  timelineSemanticCommand,
  timelineSourceClassification,
  trackDescendantIds,
} from './planningModel.js';
import { filesFacadeClient } from '../filesFacadeClient.js';
import { FILES_TRANSFER_MIME, parseInternalDragPayload } from '../filesSelectionModel.js';
import { createCopalWindow } from './windows.js';
import { copalStorageKey } from './storage.js';
import { activateInputContext, canHandleInput, hasInputContexts, isComposingEvent, isEditableTarget, registerInputContext, resolveInputContext } from './inputContext.js';
import { styledConfirm, styledPrompt } from '../dialogPrimitives.js';

export { MIN_DAY_WIDTH, DEFAULT_DAY_WIDTH, MAX_DAY_WIDTH, MIN_LANE_HEIGHT, MAX_LANE_HEIGHT, RANGE_CHUNK_DAYS, WARM_HISTORY_DAYS, addDays, daysBetween, dragPatch, effectiveTrackState, eventLayout, flattenTrackPreorder, formatLocalDate, glyphFor, manipulationGate, normalizeTrackHierarchy, parseLocalDate, reparentTrackSubtree, timelineEventHandle, timelineEventResourceKey, timelineSemanticCommand, timelineSourceClassification, trackBreadcrumb, trackDescendantIds } from './planningModel.js';
const MAX_WINDOW_DAYS = 1460;
const LABEL_WIDTH = 190;
const INITIAL_HISTORY_DAYS = 3;
const RANGE_STATE_VERSION = 2;
const COLORS = ['#f97316','#84cc16','#ec4899','#a855f7','#14b8a6','#eab308','#0ea5e9','#b45309','#f59e0b','#6b7280','#dc2626','#22c55e','#10b981','#0891b2','#06b6d4','#f43f5e','#8b5cf6','#facc15'];
const THEME_TRACK_COLOR = 'var(--accent-primary, var(--accent, var(--red)))';

function clamp(value, min, max) { return Math.max(min, Math.min(max, value)); }
function uniqueEvents(data) {
  const values = new Map();
  for (const track of data.tracks || []) for (const event of track.tasks || []) values.set(event.id, { ...event, trackId: event.trackId || track.id });
  for (const event of data.floatingTodos || []) values.set(event.id, event);
  return [...values.values()];
}
function eventAttachments(event) {
  return Array.isArray(event?.attachments) ? event.attachments : Array.isArray(event?.copal_extra?.attachments) ? event.copal_extra.attachments : [];
}
function trackMap(data) { return new Map((data.tracks || []).map((track) => [track.id, track])); }
function randomId(prefix) { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`; }
function reducedMotion() { return window.matchMedia?.('(prefers-reduced-motion: reduce)').matches; }
function timelineInputAllowed(event) {
  if (event.defaultPrevented || isComposingEvent(event) || isEditableTarget(event.target)) return false;
  if (!hasInputContexts()) return true;
  const context = resolveInputContext(event);
  return !!context && canHandleInput(event, context);
}

function resourceReadable(resource) {
  const capabilities = resource?.capabilities;
  if (Array.isArray(capabilities)) return capabilities.some((capability) => ['read', 'open', 'download', 'copy'].includes(String(capability).toLowerCase()));
  if (capabilities && typeof capabilities === 'object') return ['read', 'open', 'download', 'copy'].some((capability) => capabilities[capability] === true);
  return resource?.readable !== false;
}

export function validateTimelineFilesDrop(raw, { owner = '', accountId = '', workspace = '', pane = '', provider = '', resourceKey = '', resourceRef = '', sourceRevision = null, generation = null, policyGeneration = null, selectionEpoch = null } = {}) {
  const payload = parseInternalDragPayload(raw);
  if (!payload) return { ok: false, reason: 'This Files drop is not a supported resource.' };
  if (payload.sources.length !== 1) return { ok: false, reason: 'Attach one readable Files resource at a time.' };
  const expectedOwner = String(owner || accountId || '').trim();
  if (expectedOwner && payload.owner !== expectedOwner) return { ok: false, reason: 'This resource belongs to another account.' };
  if (workspace && payload.workspace !== String(workspace)) return { ok: false, reason: 'This resource belongs to another workspace.' };
  if (pane && payload.pane !== String(pane)) return { ok: false, reason: 'This Files gesture is no longer active.' };
  if (generation != null && Number(payload.generation) !== Number(generation)) return { ok: false, reason: 'File access changed; select the resource again.' };
  const source = payload.sources[0];
  if (provider && payload.provider !== String(provider)) return { ok: false, reason: 'This Files provider is no longer active.' };
  if (resourceKey && source.resource_key !== String(resourceKey)) return { ok: false, reason: 'This resource selection is no longer active.' };
  if (resourceRef && source.resource_ref !== String(resourceRef)) return { ok: false, reason: 'This resource reference is no longer active.' };
  if (sourceRevision && JSON.stringify(source.revision || null) !== JSON.stringify(sourceRevision)) return { ok: false, reason: 'This resource revision is stale; select it again.' };
  if (policyGeneration != null && Number(payload.policy_generation) !== Number(policyGeneration)) return { ok: false, reason: 'File policy changed; select the resource again.' };
  if (selectionEpoch != null && Number(payload.selection_epoch) !== Number(selectionEpoch)) return { ok: false, reason: 'This Files selection is no longer active.' };
  return { ok: true, payload, source };
}

export async function prepareTimelineAttachment({ event, source, accountId, workspace = 'default', mode = 'link', filesClient = filesFacadeClient, applyAttachment, signal = null, operationId = null, expectedPolicyGeneration = null } = {}) {
  if (!['link', 'embed'].includes(String(mode || '').trim().toLowerCase())) throw new Error('Attachment mode is unsupported.');
  const classification = timelineSourceClassification(source);
  if (!classification.supported) throw new Error(classification.reason);
  if (typeof applyAttachment !== 'function') throw new TypeError('Timeline attachment mutation is required');
  const handle = timelineEventHandle(event, { accountId, workspace });
  const key = timelineEventResourceKey(handle);
  const roots = await filesClient.roots({ copalWorkspace: handle.workspace, signal });
  const generation = Number(roots?.policy_generation);
  if (!Number.isSafeInteger(generation) || generation < 0) throw new Error('File access generation is unavailable; retry the attachment.');
  if (expectedPolicyGeneration != null && generation !== Number(expectedPolicyGeneration)) throw new Error('File policy changed; select the resource again.');
  const resolved = await filesClient.resolveResource(key, { signal });
  const resolvedResource = resolved?.resource || resolved;
  const targetRef = String(resolvedResource?.ref || resolvedResource?.resource_ref || '').trim();
  const targetRevision = resolvedResource?.revision || resolved?.revision;
  if (!targetRef || !targetRevision) throw new Error('The Timeline event target is unavailable; retry the attachment.');
  const sourceStat = await filesClient.stat(classification.resourceRef, { signal });
  const authorized = sourceStat?.resource || sourceStat;
  if (!authorized || authorized.kind === 'folder' || authorized.kind === 'directory' || !resourceReadable(authorized)) throw new Error('This resource cannot be read by the current account.');
  const op = String(operationId || `timeline-attachment-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`}`);
  const prepared = await filesClient.prepareAttachment({
    operationId: op, generation,
    source: { resourceRef: String(authorized.ref || classification.resourceRef), ...(classification.expectedRevision ? { expectedRevision: classification.expectedRevision } : {}) },
    target: { kind: 'copal_document', resourceRef: targetRef, expectedRevision: targetRevision }, mode, workspace: handle.workspace,
  }, { signal });
  const attachment = Object.freeze({ operationId: op, preparationReceiptId: prepared.preparation_receipt_id, mode, sourceRevision: prepared.source_revision || classification.expectedRevision || null, targetRevision: prepared.target_revision || targetRevision, asset: prepared.asset || null, insertion: prepared.insertion || null });
  await applyAttachment(handle, attachment, { prepared, generation, source: authorized });
  return { outcome: 'attached', operationId: op, preparation: prepared, attachment };
}

function trackHierarchyView(tracks, { hiddenTracks = new Set(), collapsedTrackGroups = new Set() } = {}) {
  const ordered = flattenTrackPreorder(tracks);
  const byId = new Map(ordered.map((track) => [track.id, track]));
  const depths = new Map();
  const paths = new Map();
  const childCounts = new Map();
  const states = new Map();
  for (const track of ordered) {
    const parent = track.parentTrackId === null ? null : byId.get(track.parentTrackId);
    const parentState = parent ? states.get(parent.id) : null;
    const depth = parent ? depths.get(parent.id) + 1 : 0;
    const path = parent ? `${paths.get(parent.id)} / ${track.name}` : track.name;
    const ancestorDisabledBy = parent?.enabled === false ? parent.id : parentState?.ancestorDisabledBy || null;
    const ancestorHiddenBy = parent && hiddenTracks.has(parent.id) ? parent.id : parentState?.ancestorHiddenBy || null;
    const ancestorCollapsedBy = parent && collapsedTrackGroups.has(parent.id) ? parent.id : parentState?.ancestorCollapsedBy || null;
    const ownEnabled = track.enabled !== false;
    const ownHidden = hiddenTracks.has(track.id);
    const suppressedBy = !ownEnabled ? track.id : ownHidden ? track.id : ancestorDisabledBy || ancestorHiddenBy || ancestorCollapsedBy;
    depths.set(track.id, depth);
    paths.set(track.id, path);
    states.set(track.id, { ownEnabled, ownHidden, collapsed:collapsedTrackGroups.has(track.id), ancestorDisabledBy, ancestorHiddenBy, ancestorCollapsedBy, suppressedBy, visible:suppressedBy === null });
    if (parent) childCounts.set(parent.id, (childCounts.get(parent.id) || 0) + 1);
  }
  return { ordered, byId, depths, paths, childCounts, states };
}

// Geometry and lane placement are independent of the DOM surface. Keep a
// compact content signature so warm rerenders can reuse that projection while
// still invalidating when an event mutation changes its visible shape.
function timelineEventSignature(event) {
  return [
    event.id, event.trackId, event.startDate, event.dueDate,
    event.fuzzy?.anchorStart, event.fuzzy?.anchorEnd, event.fuzzy?.whiskerStart, event.title,
    event.status, event.priority, (event.sharedTrackIds || []).join(','),
    (event.stages || []).map((stage) => `${stage.id}:${stage.done ? 1 : 0}`).join(','),
  ].map((value) => String(value ?? '')).join('\u001f');
}

function timelineEventGeometrySignature(event) {
  return [event.trackId, (event.sharedTrackIds || []).join(','), event.startDate, event.dueDate, event.fuzzy?.anchorStart, event.fuzzy?.anchorEnd, event.fuzzy?.whiskerStart].map((value) => String(value ?? '')).join('\u001f');
}

function patchTimelineEventNode(node, event) {
  if (!node) return false;
  const label = node.querySelector('.copal-event-label');
  if (label) label.textContent = `${(event.sharedTrackIds || []).length ? '🔗 ' : ''}${event.title}`;
  node.classList.toggle('done', event.status === 'done');
  node.classList.toggle('fuzzy', event.startDate === 'FUZZY' || !!event.fuzzy);
  node.setAttribute('aria-label', `${event.title}, ${event.startDate || 'unscheduled'} to ${event.dueDate || 'open end'}`);
  node.title = [event.title, `${event.startDate || '?'} → ${event.dueDate || '∞'}`, 'Drag to move · drag edges to resize · Alt+Arrow adjusts by one day'].join('\n');
  const surface = node.querySelector('.copal-event-surface');
  const stages = event.stages || [];
  let progress = surface?.querySelector('.copal-event-progress');
  if (stages.length) {
    if (!progress && surface) { progress = node.ownerDocument.createElement('span'); progress.className = 'copal-event-progress'; surface.append(progress); }
    progress?.style.setProperty('--progress', `${Math.round((stages.filter((stage) => stage.done).length / stages.length) * 100)}%`);
  } else progress?.remove();
  return true;
}

// A task can occur in several visible lanes (for example when it is shared
// across tracks). Keep every DOM instance indexed so a warm metadata patch
// cannot leave one lane stale. The nodes themselves remain authoritative for
// geometry and event handlers; this index only broadens the existing cache.
export function timelineEventNodeIndex(nodes) {
  const index = new Map();
  for (const node of nodes || []) {
    const id = node?.dataset?.taskId;
    if (!id) continue;
    const instances = index.get(id) || [];
    instances.push(node);
    index.set(id, instances);
  }
  return index;
}

export function patchTimelineEventNodes(nodes, event) {
  if (!Array.isArray(nodes) || nodes.length === 0) return false;
  return nodes.every((node) => patchTimelineEventNode(node, event));
}

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

export function createPlanningFeature({ h, api, getPlanning, refresh, setStatus, projectionChanged, openDocument, filesClient = filesFacadeClient, getScope = null, openMarkdownTask = null, createMarkdownTask = null, patchMarkdownTask = null, queryMarkdownTasks = null, loadMoreMarkdownTasks = null, canLoadMoreMarkdownTasks = () => false }) {
  const timeline = {
    dayWidth: DEFAULT_DAY_WIDTH,
    laneHeight: 56,
    mode: 'regular',
    condensedStyle: 'dots',
    rangeStart: null,
    rangeEnd: null,
    hiddenTracks: new Set(),
    expandedTracks: new Set(),
    collapsedTrackGroups: new Set(),
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
  let timelineProjectionCache = null;
  let timelineSurfaceCache = null;
  const timelineInstances = new WeakMap();
  const timelineRoots = new Set();
  let timelineObserver = null;
  const attachmentOperations = new Map();
  const attachmentControllers = new Map();

  function planningScope() {
    const supplied = typeof getScope === 'function' ? getScope() : (typeof window !== 'undefined' ? window.__odysseusGetActiveCopalContext?.() : null);
    return supplied && typeof supplied === 'object' ? supplied : {};
  }

  async function attachTimelineResource(event, source, { mode = 'link', gestureContext = null, operationId = null, signal = null } = {}) {
    const scope = planningScope();
    const accountId = String(scope.accountId || '').trim();
    const workspaceId = String(scope.workspace || workspace || 'default').trim();
    const context = gestureContext || source?.gestureContext || null;
    if (context) {
      const check = validateTimelineFilesDrop(context.raw || context.payload || context, { owner: context.owner || accountId, workspace: context.workspace || workspaceId, pane: context.pane || '', generation: context.generation, policyGeneration: context.policyGeneration, selectionEpoch: context.selectionEpoch, provider: context.provider, resourceKey: context.resourceKey, resourceRef: context.resourceRef, sourceRevision: context.sourceRevision });
      if (!check.ok) throw new Error(check.reason);
      source = check.source;
    }
    const key = String(operationId || `timeline-attachment-${event?.id || 'event'}-${source?.item_id || source?.itemId || source?.resource_ref || 'source'}`);
    if (attachmentOperations.has(key)) return attachmentOperations.get(key);
    const controller = typeof AbortController === 'function' ? new AbortController() : null;
    const relayAbort = () => controller?.abort();
    if (signal?.aborted) controller?.abort();
    else signal?.addEventListener?.('abort', relayAbort, { once:true });
    attachmentControllers.set(key, controller);
    const pending = prepareTimelineAttachment({ event, source, accountId: accountId || context?.owner || 'local-installation', workspace: workspaceId, mode, filesClient, signal:controller?.signal || signal, operationId: key, expectedPolicyGeneration: context?.policyGeneration ?? null, applyAttachment: async (handle, attachment) => {
      const current = uniqueEvents(getPlanning()).find((item) => item.id === handle.eventId);
      if (!current || current.head !== handle.expectedHead) throw new Error('The Timeline event changed; preparation is recoverable and the attachment was not applied.');
      const prior = eventAttachments(current);
      if (prior.some((item) => item?.operationId === attachment.operationId || item?.preparationReceiptId === attachment.preparationReceiptId)) return;
      await patchEvent(current, { attachments: [...prior, attachment] });
    }}).then((result) => { attachmentOperations.set(key, result); setStatus('Resource attached to event'); return result; }).catch((error) => { attachmentOperations.delete(key); setStatus(error?.message || 'Resource attachment failed; preparation can be retried.', true); throw error; }).finally(() => { attachmentControllers.delete(key); signal?.removeEventListener?.('abort', relayAbort); });
    attachmentOperations.set(key, pending);
    return pending;
  }

  function disposeTimelineBody(body) {
    const instance = timelineInstances.get(body);
    if (instance) instance.dispose();
    timelineInstances.delete(body);
    timelineRoots.delete(body);
  }

  function ensureTimelineObserver() {
    if (timelineObserver || typeof MutationObserver === 'undefined' || !document.body) return;
    timelineObserver = new MutationObserver(() => {
      for (const body of [...timelineRoots]) if (!body.isConnected) disposeTimelineBody(body);
    });
    timelineObserver.observe(document.body, { childList:true, subtree:true });
  }

  function suspendScope() {
    for (const controller of attachmentControllers.values()) controller?.abort();
    attachmentControllers.clear();
    attachmentOperations.clear();
    for (const body of [...timelineRoots]) disposeTimelineBody(body);
    timelineObserver?.disconnect(); timelineObserver = null;
    if (timelineRefreshTimer) { clearInterval(timelineRefreshTimer); timelineRefreshTimer = null; }
    timelineBody = null;
    eventEditor?.destroy(); eventEditor = null;
    trackEditor?.destroy(); trackEditor = null;
    editorState = { eventId:null, draft:null, dirty:false, trigger:null };
    trackState = { trackId:null, draft:null, dirty:false, trigger:null };
  }

  function storageKey() { return copalStorageKey('odysseus-copal-timeline-v2', workspace); }
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
      timeline.collapsedTrackGroups = new Set(Array.isArray(saved.collapsedTrackGroups) ? saved.collapsedTrackGroups : []);
    } catch (_) {}
  }
  function persist(anchorDate = null) {
    let priorAnchor;
    try { priorAnchor = JSON.parse(localStorage.getItem(storageKey()) || '{}').anchorDate; } catch (_) {}
    const trackIds = new Set((getPlanning()?.tracks || []).map((track) => track.id));
    timeline.collapsedTrackGroups = new Set([...timeline.collapsedTrackGroups].filter((id) => trackIds.has(id)));
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
      collapsedTrackGroups: [...timeline.collapsedTrackGroups],
      anchorDate: anchorDate ? formatLocalDate(anchorDate) : priorAnchor,
    }));
  }

  // ── Task view state persistence ──────────────────────────────────────
  const DEFAULT_VIEWS = [
    { id:'all-open', name:'All Open', config:{ sourceFilter:'all', dateFilter:'all', learningFilter:'all', statusFilter:'pending', priorityFilter:'all', sortKey:'start', hideDone:true, groupBy:'none', columns:null } },
    { id:'due-today', name:'Due Today', config:{ sourceFilter:'all', dateFilter:'due', learningFilter:'all', statusFilter:'all', priorityFilter:'all', sortKey:'due', hideDone:false, groupBy:'none', columns:null } },
    { id:'by-track', name:'By Track', config:{ sourceFilter:'all', dateFilter:'all', learningFilter:'all', statusFilter:'all', priorityFilter:'all', sortKey:'start', hideDone:false, groupBy:'track', columns:null } },
    { id:'high-priority', name:'High Priority', config:{ sourceFilter:'all', dateFilter:'all', learningFilter:'all', statusFilter:'all', priorityFilter:'high', sortKey:'priority', hideDone:false, groupBy:'none', columns:null } },
  ];
  const DEFAULT_COLUMNS = [
    { id:'title', label:'Title', visible:true, order:0 },
    { id:'status', label:'Status', visible:true, order:1 },
    { id:'priority', label:'Priority', visible:true, order:2 },
    { id:'start', label:'Start', visible:true, order:3 },
    { id:'due', label:'Due', visible:true, order:4 },
    { id:'track', label:'Track', visible:true, order:5 },
    { id:'tags', label:'Tags', visible:false, order:6 },
    { id:'stages', label:'Stages', visible:false, order:7 },
  ];
  function taskStorageKey() { return copalStorageKey('odysseus-copal-taskview-v1', workspace); }
  function loadTaskSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(taskStorageKey()) || '{}');
      return {
        columns: Array.isArray(saved.columns) ? saved.columns : null,
        views: Array.isArray(saved.views) ? saved.views : null,
      };
    } catch (_) { return { columns:null, views:null }; }
  }
  function persistTaskSettings(settings) {
    localStorage.setItem(taskStorageKey(), JSON.stringify({
      columns: settings.columns,
      views: settings.views,
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
      sizeKey: copalStorageKey('odysseus-copal-event-editor-size'), className: 'copal-event-editor-window',
      onBeforeClose: async () => !editorState.dirty || await styledConfirm('Discard unsaved event changes?', {
        title: 'Discard event changes', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true,
      }),
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
    const hierarchy = trackHierarchyView(data.tracks || []);
    const tracks = hierarchy.ordered;
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
    for (const track of tracks) {
      const path = hierarchy.paths.get(track.id);
      mainTrack.append(h('option', { value:track.id, text:`${glyphFor(track.icon)} ${path}${track.enabled === false ? ' (disabled)' : ''}` }));
    }
    mainTrack.value = draft.trackId || '';
    mainTrack.addEventListener('change', () => { draft.trackId = mainTrack.value || null; draft.sharedTrackIds = (draft.sharedTrackIds || []).filter((id) => id !== draft.trackId); markEditorDirty(); renderEventEditor(); });
    const shared = h('div', { class:'copal-track-chip-list', role:'group', 'aria-label':'Additional shared tracks' });
    for (const track of tracks.filter((item) => item.id !== draft.trackId)) {
      const active = (draft.sharedTrackIds || []).includes(track.id);
      const path = hierarchy.paths.get(track.id);
      shared.append(h('button', { type:'button', class:`copal-track-chip${active ? ' active' : ''}`, style:`--track-color:${track.color || THEME_TRACK_COLOR}`, text:`${glyphFor(track.icon)} ${track.name}${track.enabled === false ? ' (disabled)' : ''}`, title:path, 'aria-label':`${active ? 'Remove' : 'Add'} shared track ${path}${track.enabled === false ? ', disabled' : ''}`, onclick:() => {
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

    const recurrenceSection = h('section', { class:'copal-recurrence-editor' });
    const hasRecurrence = !!draft.recurrence;
    const recurrenceToggle = h('button', { type:'button', class:`copal-btn${hasRecurrence ? ' active' : ''}`, text:hasRecurrence ? 'Recurs' : 'Repeat…', 'aria-label':'Toggle recurrence' });
    recurrenceToggle.addEventListener('click', () => {
      if (draft.recurrence) { draft.recurrence = null; } else { draft.recurrence = { frequency:'weekly', interval:1, count:0, until:null, exceptionDates:[], allDay:false }; }
      markEditorDirty(); renderEventEditor();
    });
    recurrenceSection.append(recurrenceToggle);

    if (draft.recurrence) {
      const rec = draft.recurrence;
      const freq = control('select', { 'aria-label':'Frequency' });
      for (const [value, label] of [['daily','Daily'],['weekly','Weekly'],['monthly','Monthly']]) freq.append(h('option', { value, text:label }));
      freq.value = rec.frequency || 'weekly';
      freq.addEventListener('change', () => { rec.frequency = freq.value; markEditorDirty(); });

      const interval = control('input', { type:'number', min:'1', max:'99', 'aria-label':'Interval' }, String(rec.interval || 1));
      interval.addEventListener('input', () => { rec.interval = Math.max(1, parseInt(interval.value) || 1); markEditorDirty(); });

      const boundedBy = rec.count != null ? 'count' : 'until';
      const countInput = control('input', { type:'number', min:'0', max:'500', 'aria-label':'Repeat count (0 = forever)' }, boundedBy === 'count' ? String(rec.count ?? 10) : '');
      const untilInput = control('input', { type:'date', 'aria-label':'Repeat until' }, boundedBy === 'until' ? (rec.until || '') : '');
      countInput.addEventListener('input', () => { if (countInput.value !== '') { rec.count = parseInt(countInput.value) || 0; rec.until = null; } markEditorDirty(); });
      untilInput.addEventListener('change', () => { if (untilInput.value) { rec.until = untilInput.value; rec.count = null; } markEditorDirty(); });

      const excDates = h('div', { class:'copal-recurrence-exceptions' });
      for (const exc of (rec.exceptionDates || [])) {
        const chip = h('span', { class:'copal-recurrence-chip' }, exc, h('button', { type:'button', class:'copal-btn', text:'×', 'aria-label':`Remove exception ${exc}`, onclick:() => { rec.exceptionDates = rec.exceptionDates.filter((d) => d !== exc); markEditorDirty(); renderEventEditor(); } }));
        excDates.append(chip);
      }
      const addExc = control('input', { type:'date', 'aria-label':'Add exception date' });
      addExc.addEventListener('change', () => {
        if (addExc.value && !(rec.exceptionDates || []).includes(addExc.value)) { rec.exceptionDates = [...(rec.exceptionDates || []), addExc.value]; addExc.value = ''; markEditorDirty(); renderEventEditor(); }
      });

      const allDayCheck = control('input', { type:'checkbox', 'aria-label':'All-day events' }); allDayCheck.checked = !!rec.allDay;
      allDayCheck.addEventListener('change', () => { rec.allDay = allDayCheck.checked; markEditorDirty(); });

      recurrenceSection.append(
        h('div', { class:'copal-form-grid' }, field('Every', h('div', { class:'copal-recurrence-interval' }, interval, h('span', { text: rec.frequency === 'daily' ? 'day(s)' : rec.frequency === 'weekly' ? 'week(s)' : 'month(s)' }))),
          field('Ends', h('div', { class:'copal-recurrence-end' }, h('label', { class:'copal-check' }, h('input', { type:'radio', name:'rec-end', value:'count', checked: boundedBy === 'count' ? '' : null }), 'After'), countInput, h('span', { class:'copal-recurrence-hint', text:(rec.count === 0 || rec.count === null) ? '(0 = forever)' : 'occurrences' }), h('label', { class:'copal-check' }, h('input', { type:'radio', name:'rec-end', value:'until', checked: boundedBy === 'until' ? '' : null }), 'On'), untilInput))),
        field('Skip dates', h('div', { class:'copal-recurrence-skip' }, excDates, addExc)),
        h('label', { class:'copal-check' }, allDayCheck, 'All day (no timezone)')
      );
    }
    form.append(recurrenceSection);

    const advanced = h('details', { class:'copal-event-advanced' }, h('summary', { text:'Compatibility and fade details' }));
    for (const [key, label, type] of [['linkId','Linked event id','text'],['fadeDays','Fade days','number'],['titleStart','Start label','text'],['titleEnd','End label','text']]) {
      const input = control('input', { type, min:type === 'number' ? '0' : null }, draft[key] ?? '');
      input.addEventListener('input', () => { draft[key] = type === 'number' ? Number(input.value || 0) : input.value || null; markEditorDirty(); });
      advanced.append(field(label, input));
    }
    form.append(advanced);

    const actions = h('footer', { class:'copal-dialog-actions copal-event-actions' },
      h('button', { type:'button', class:'copal-btn danger', text:'Delete event', onclick:async() => {
        if (!await styledConfirm(`Move “${source.title}” to Copal trash?`, {
          title: 'Move event to trash', confirmText: 'Move to trash', cancelText: 'Keep event', danger: true,
        })) return;
        const result = await api(`/planning/events/${encodeURIComponent(source.id)}`, { method:'DELETE' }); projectionChanged(result); editorState.dirty = false; win.requestClose(); await refresh();
      } }),
      h('button', { type:'button', class:'copal-btn', text:'Open note', onclick:() => openDocument(source.id, 'notes') }),
      h('button', { type:'button', class:'copal-btn', text:'Attach resource', 'aria-label':'Attach Files resource', title:'Drop one readable Files resource onto this event', onclick:() => win.setStatus?.('Drop one readable Files resource onto this event to attach it.') }),
      h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => win.requestClose() }),
      h('button', { type:'button', class:'copal-btn primary', text:'Save', onclick:async(event) => {
        const button = event.currentTarget;
        button.disabled = true;
        try {
          const patch = Object.fromEntries(['title','description','startDate','dueDate','status','priority','trackId','sharedTrackIds','tags','linkId','fuzzy','fadeDays','titleStart','titleEnd','stages','floating','recurrence','allDay'].map((key) => [key, draft[key] ?? null]));
          await patchEvent(source, patch); editorState.dirty = false; const fresh = uniqueEvents(getPlanning()).find((item) => item.id === source.id); editorState.draft = fresh ? structuredClone(fresh) : null; renderEventEditor();
        } finally { button.disabled = false; }
      } }));
    form.append(actions);
    win.body.replaceChildren(form);
    win.setStatus(editorState.dirty ? 'Unsaved' : `Revision ${String(source.head || '').slice(0, 8)}`);
  }

  async function openEventEditor(eventId, trigger = document.activeElement) {
    if (editorState.dirty && editorState.eventId !== eventId && !await styledConfirm('Discard unsaved event changes?', {
      title: 'Discard event changes', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true,
    })) return;
    // Timeline rows can be painted from the latest SSE payload while the
    // feature's planning snapshot is still one refresh behind. Refresh before
    // building the editor so newly-created nested tracks are present in the
    // Main track/shared-track controls as soon as the event is opened.
    try { await refresh(); } catch (_) { /* keep the locally rendered event usable */ }
    const source = uniqueEvents(getPlanning()).find((event) => event.id === eventId);
    if (!source) return;
    editorState = { eventId, draft:structuredClone(source), dirty:false, trigger };
    renderEventEditor(); ensureEventEditor().show(trigger);
  }

  function ensureTrackEditor() {
    if (trackEditor) return trackEditor;
    trackEditor = createCopalWindow({
      id:'copal-track-editor-modal', label:'Edit track', subtitle:'Canonical track registry', minWidth:390, minHeight:430,
      sizeKey:copalStorageKey('odysseus-copal-track-editor-size'), className:'copal-track-editor-window',
      onBeforeClose: async () => !trackState.dirty || await styledConfirm('Discard unsaved track changes?', {
        title: 'Discard track changes', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true,
      }),
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
    const draft = trackState.draft || structuredClone(current || { id:randomId('track'), name:'', color:COLORS[0], icon:'📦', enabled:true, parentTrackId:null }); trackState.draft = draft;
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
    const parent = control('select', { 'aria-label':'Parent track' });
    parent.append(h('option', { value:'', text:'Top level' }));
    const excluded = current ? new Set([current.id, ...trackDescendantIds(data.tracks || [], current.id)]) : new Set();
    const hierarchy = trackHierarchyView(data.tracks || []);
    for (const track of hierarchy.ordered) {
      if (!excluded.has(track.id)) parent.append(h('option', { value:track.id, text:hierarchy.paths.get(track.id) }));
    }
    parent.value = draft.parentTrackId || '';
    parent.addEventListener('change', () => { draft.parentTrackId = parent.value || null; trackState.dirty = true; win.setStatus('Unsaved'); });
    form.append(field('Name', name), field('Parent track', parent), field('Color', colors), field('Icon / emoji / Unicode', h('div', { class:'copal-emoji-field' }, h('span', { class:'copal-emoji-preview', text:glyphFor(draft.icon) }), emoji)), picker, h('label', { class:'copal-check' }, enabled, 'Enabled in planning projections'));
    form.append(h('p', { class:'copal-form-help', text:'Temporary visibility is controlled in Timeline. Disabling a track is durable and does not delete its events.' }));
    form.append(h('footer', { class:'copal-dialog-actions' }, h('button', { type:'button', class:'copal-btn', text:'Cancel', onclick:() => win.requestClose() }), h('button', { type:'button', class:'copal-btn primary', text:'Save', onclick:async(event) => {
      if (!draft.name.trim()) { win.setStatus('Track name is required', true); name.focus(); return; }
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const next = (data.tracks || []).map(({ tasks, ...track }) => track);
        const index = next.findIndex((track) => track.id === draft.id);
        const { tasks:ignoredTasks, ...cleanDraft } = draft;
        const selectedParent = cleanDraft.parentTrackId || null;
        let candidate;
        if (index >= 0) {
          const currentParent = next[index].parentTrackId || null;
          next.splice(index, 1, { ...cleanDraft, parentTrackId:currentParent });
          candidate = currentParent === selectedParent ? next : reparentTrackSubtree(next, cleanDraft.id, selectedParent);
        } else {
          next.push({ ...cleanDraft, parentTrackId:null });
          candidate = selectedParent === null ? next : reparentTrackSubtree(next, cleanDraft.id, selectedParent);
        }
        const unchanged = index >= 0 && JSON.stringify(candidate) === JSON.stringify((data.tracks || []).map(({ tasks, ...track }) => track));
        if (!unchanged) await saveTracks(candidate, data.trackRegistry?.head);
        trackState.dirty = false; win.requestClose();
      } catch (error) { win.setStatus(error.status === 409 ? 'Track hierarchy changed elsewhere. Reopen the editor to use the authoritative version.' : error.message, true); }
      finally { button.disabled = false; }
    } })));
    win.body.replaceChildren(form); win.setStatus(trackState.dirty ? 'Unsaved' : 'Name, color, emoji, and enabled state');
  }

  async function openTrackEditor(trackId = null, trigger = document.activeElement) {
    const current = (getPlanning().tracks || []).find((track) => track.id === trackId);
    if (trackState.dirty && !await styledConfirm('Discard unsaved track changes?', {
      title: 'Discard track changes', confirmText: 'Discard changes', cancelText: 'Keep editing', danger: true,
    })) return;
    trackState = { trackId, draft:structuredClone(current || { id:randomId('track'), name:'', color:COLORS[Math.floor(Math.random() * COLORS.length)], icon:'📦', enabled:true, parentTrackId:null }), dirty:false, trigger };
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
    let drag = null; let suppressClick = false;
    const renderedScale = () => { const logicalWidth = Number.parseFloat(node.style.width); const renderedWidth = node.getBoundingClientRect().width; return logicalWidth > 0 && renderedWidth > 0 ? renderedWidth / logicalWidth : 1; };
    const escape = (keyboard) => {
      if (keyboard.key !== 'Escape' || !drag) return;
      keyboard.preventDefault(); keyboard.stopPropagation(); cancel();
    };
    const begin = (pointer, mode) => {
      const allowed = mode === 'move' ? gate.movable : mode === 'left' ? gate.resizeLeft : gate.resizeRight;
      if (!allowed || pointer.button !== 0 || !timelineInputAllowed(pointer)) return;
      pointer.preventDefault(); pointer.stopPropagation(); node.setPointerCapture(pointer.pointerId);
      drag = { mode, x:pointer.clientX, s:originalStart, e:originalEnd, moved:false, preview:null, pointerId:pointer.pointerId, pxPerDay:dayWidth * renderedScale() };
      node.classList.add('dragging'); node.focus({ preventScroll:true }); window.addEventListener('blur', cancel, { once:true }); window.addEventListener('keydown', escape, true);
    };
    const cancel = () => {
      if (!drag) return;
      suppressClick = true;
      const pointerId = drag.pointerId; drag = null; window.removeEventListener('blur', cancel); window.removeEventListener('keydown', escape, true); guide.hidden = true; node.classList.remove('dragging');
      try { if (node.hasPointerCapture(pointerId)) node.releasePointerCapture(pointerId); } catch (_) {}
      node.style.left = originalLeft; node.style.width = originalWidth; node.setAttribute('aria-label', originalLabel);
    };
    if (!gate.movable && !gate.resizeLeft && !gate.resizeRight) {
      node.addEventListener('click', () => openEventEditor(event.id, node));
      node.addEventListener('keydown', (keyboard) => { if (keyboard.key === 'Enter' || keyboard.key === ' ') { if (!timelineInputAllowed(keyboard)) return; keyboard.preventDefault(); openEventEditor(event.id, node); } });
      node.title = [event.title, `${event.startDate || '?'} → ${event.dueDate || '∞'}`, gate.reason || 'Edit this event in the popup.'].join('\n');
      return;
    }
    node.addEventListener('pointerdown', (eventPointer) => begin(eventPointer, eventPointer.target.closest('.copal-resize-left') ? 'left' : eventPointer.target.closest('.copal-resize-right') ? 'right' : 'move'));
    node.addEventListener('pointermove', (pointer) => {
      if (!drag) return; const delta = Math.round((pointer.clientX - drag.x) / drag.pxPerDay); if (Math.abs(pointer.clientX - drag.x) > 3) drag.moved = true;
      let s = drag.s; let e = drag.e;
      if (drag.mode === 'move') { s += delta; e += delta; }
      else if (drag.mode === 'left') s = Math.min(drag.s + delta, e - 1);
      else e = Math.max(s + 1, drag.e + delta);
      drag.preview = { s, e };
      node.style.left = `${LABEL_WIDTH + s * dayWidth}px`; node.style.width = `${Math.max(dayWidth, (e - s) * dayWidth)}px`;
      const boundary = drag.mode === 'right' ? e : s; const labelDay = drag.mode === 'right' ? e - 1 : s;
      guide.hidden = false; guide.style.left = `${LABEL_WIDTH + boundary * dayWidth}px`; guide.querySelector('span').textContent = formatLocalDate(addDays(start, labelDay));
      node.setAttribute('aria-label', `${event.title}: ${formatLocalDate(addDays(start, s))} to ${formatLocalDate(addDays(start, e - 1))}`);
    });
    const finish = async (pointer) => {
      if (!drag) return; const current = drag; drag = null; window.removeEventListener('blur', cancel); window.removeEventListener('keydown', escape, true); guide.hidden = true; node.classList.remove('dragging');
      try { if (node.hasPointerCapture(pointer.pointerId)) node.releasePointerCapture(pointer.pointerId); } catch (_) {}
      if (!current.moved || !current.preview) { suppressClick = true; openEventEditor(event.id, node); rerender(); return; }
      const delta = current.mode === 'move' || current.mode === 'left' ? current.preview.s - current.s : current.preview.e - current.e;
      node.classList.add('saving'); node.setAttribute('aria-busy', 'true');
      try { await patchEvent(event, dragPatch(event, current.mode, delta)); } catch (_) {}
    };
    // Keep movable events keyboard- and automation-clickable as well as
    // pointer-drag editable. Pointer-up with no movement is followed by the
    // browser click event, so opening here avoids a second editor invocation.
    node.addEventListener('click', () => {
      if (suppressClick) { suppressClick = false; return; }
      if (!node.classList.contains('saving')) openEventEditor(event.id, node);
    });
    node.addEventListener('pointerup', finish);
    node.addEventListener('pointercancel', cancel);
    node.addEventListener('lostpointercapture', () => { if (drag) cancel(); });
    node.addEventListener('keydown', async (keyboard) => {
      if (!timelineInputAllowed(keyboard)) return;
      if (keyboard.key === 'Escape' && drag) { keyboard.preventDefault(); cancel(); return; }
      if (keyboard.key === 'Enter' || keyboard.key === ' ') { if (!timelineInputAllowed(keyboard)) return; keyboard.preventDefault(); openEventEditor(event.id, node); return; }
      if (!keyboard.altKey || !['ArrowLeft','ArrowRight'].includes(keyboard.key)) return;
      keyboard.preventDefault(); keyboard.stopPropagation();
      const delta = keyboard.key === 'ArrowLeft' ? -1 : 1; const mode = keyboard.shiftKey ? 'right' : 'move';
      if ((mode === 'move' && gate.movable) || (mode === 'right' && gate.resizeRight)) await patchEvent(event, dragPatch(event, mode, delta));
      else setStatus(gate.reason || 'That event cannot be adjusted directly.', true);
    });
    node.title = [event.title, `${event.startDate || '?'} → ${event.dueDate || '∞'}`, gate.reason || 'Drag to move · drag edges to resize · Alt+Arrow adjusts by one day'].join('\n');
  }

  function renderTimeline(body, options = {}) {
    const data = getPlanning();
    const allEvents = uniqueEvents(data);
    const byTrack = trackMap(data);
    const { today } = initialRange(data, allEvents);
    const contentSignature = allEvents.map(timelineEventSignature).join('\u001e');
    const trackSignature = (data.tracks || []).map((track) => [track.id, track.name, track.color, track.icon, track.enabled, track.parentTrackId, track.special].join('\u001f')).join('\u001e');
    const staticRenderSignature = [
      trackSignature, data.title || '', today?.getTime(), timeline.rangeStart?.getTime(), timeline.rangeEnd?.getTime(),
      timeline.dayWidth, timeline.laneHeight, timeline.mode, timeline.condensedStyle,
      [...timeline.hiddenTracks].sort().join(','), [...timeline.expandedTracks].sort().join(','), [...timeline.collapsedTrackGroups].sort().join(','),
      options.left ?? '', options.top ?? '', options.anchorDate?.getTime?.() ?? '', options.extensionComplete ? 1 : 0,
    ].join('\u001d');
    const renderSignature = `${contentSignature}\u001d${staticRenderSignature}`;
    const cachedSurface = timelineSurfaceCache;
    if (cachedSurface?.body === body && cachedSurface.staticSignature === staticRenderSignature && body.isConnected && body.firstElementChild) {
      const changedIds = allEvents.filter((event) => cachedSurface.eventSignatures.get(event.id) !== timelineEventSignature(event)).map((event) => event.id);
      if (!changedIds.length && cachedSurface.eventSignatures.size === allEvents.length) return;
      if (changedIds.length === 1 && cachedSurface.eventSignatures.size === allEvents.length) {
        const event = allEvents.find((item) => item.id === changedIds[0]);
        const prior = cachedSurface.events.get(changedIds[0]);
        const priorGeometry = cachedSurface.geometrySignatures.get(changedIds[0]);
        if (event && prior && priorGeometry === timelineEventGeometrySignature(event) && patchTimelineEventNodes(cachedSurface.eventNodes.get(changedIds[0]), event)) {
          cachedSurface.events.set(changedIds[0], Object.freeze({
            ...event,
            sharedTrackIds:Array.isArray(event.sharedTrackIds) ? [...event.sharedTrackIds] : [],
            fuzzy:event.fuzzy && typeof event.fuzzy === 'object' ? { ...event.fuzzy } : event.fuzzy,
            stages:Array.isArray(event.stages) ? event.stages.map((stage) => ({ ...stage })) : [],
          }));
          cachedSurface.eventSignatures.set(changedIds[0], timelineEventSignature(event));
          cachedSurface.geometrySignatures.set(changedIds[0], timelineEventGeometrySignature(event));
          cachedSurface.signature = renderSignature;
          return;
        }
      }
    }
    disposeTimelineBody(body);
    if (timelineSurfaceCache?.body === body) timelineSurfaceCache = null;
    ensureTimelineObserver();
    ensureTimelineClock(body);
    const previousScroll = body.querySelector('.copal-timeline-scroll');
    const previousTop = previousScroll?.scrollTop || 0;
    const previousAnchor = previousScroll && timeline.rangeStart
      ? addDays(timeline.rangeStart, Math.floor(Math.max(0, previousScroll.scrollLeft + previousScroll.clientWidth / 2 - LABEL_WIDTH) / timeline.dayWidth))
      : null;
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
    const hierarchy = trackHierarchyView(data.tracks || [], timeline);
    const orderedTracks = hierarchy.ordered;
    const trackDepths = hierarchy.depths;
    const trackPaths = hierarchy.paths;
    const childCounts = hierarchy.childCounts;
    const trackStates = hierarchy.states;
    // Index events by their owning/shared tracks once per projection. A warm
    // render still rebuilds the accessible DOM and handlers, but reuses date
    // geometry and deterministic lane placement until content or range
    // changes. This keeps mutations and zoom/extension behavior fresh while
    // avoiding 4,000 eventLayout calls on every redraw.
    const projectionKey = [
      start?.getTime(), end?.getTime(), autoStart?.getTime(),
      contentSignature,
    ].join('\u001d');
    if (!timelineProjectionCache || timelineProjectionCache.key !== projectionKey) {
      const eventsByTrack = new Map();
      for (const event of allEvents) {
        const ids = new Set([event.trackId, ...(event.sharedTrackIds || [])]);
        for (const id of ids) { if (!id) continue; if (!eventsByTrack.has(id)) eventsByTrack.set(id, []); eventsByTrack.get(id).push(event); }
      }
      const lanesByTrack = new Map();
      const buildLanes = (events) => assignEventLanes(events.map((event) => {
        const layout = eventLayout(event, start, autoStart);
        const startDay = daysBetween(start, layout.start);
        const endDay = Math.max(startDay, daysBetween(start, layout.end));
        return { event, task:event, startDay, endDay, stableId:event.id };
      }).filter((item) => item.endDay >= 0 && item.startDay < totalDays));
      for (const [trackId, events] of eventsByTrack) lanesByTrack.set(trackId, buildLanes(events));
      timelineProjectionCache = { key:projectionKey, eventsByTrack, lanesByTrack, condensedLanes:buildLanes(allEvents) };
    }
    const { eventsByTrack, lanesByTrack, condensedLanes } = timelineProjectionCache;
    const filters = h('details', { class:'copal-track-filters' }, h('summary', { text:'Tracks' }));
    for (const [index, track] of orderedTracks.entries()) {
      const state = trackStates.get(track.id);
      const inheritedBy = state.ancestorHiddenBy || state.ancestorDisabledBy || state.ancestorCollapsedBy;
      const inheritedTrack = inheritedBy ? byTrack.get(inheritedBy) : null;
      const inheritedReason = state.ancestorHiddenBy ? 'hidden' : state.ancestorDisabledBy ? 'disabled' : state.ancestorCollapsedBy ? 'collapsed' : '';
      const descriptionId = inheritedBy ? `copal-track-suppression-${index}` : null;
      const checkbox = control('input', { type:'checkbox', 'aria-label':`Show ${trackPaths.get(track.id)}`, 'aria-describedby':descriptionId });
      checkbox.checked = !timeline.hiddenTracks.has(track.id);
      checkbox.disabled = !!state.ancestorHiddenBy;
      checkbox.addEventListener('change', () => { checkbox.checked ? timeline.hiddenTracks.delete(track.id) : timeline.hiddenTracks.add(track.id); persist(); rerender({ left:scroll.scrollLeft }); });
      const label = h('label', { title:trackPaths.get(track.id) }, checkbox, h('span', { class:'copal-track-filter-glyph', text:glyphFor(track.icon) }), h('span', { class:'copal-track-filter-name', text:track.name }));
      const filterDepth = Math.min(trackDepths.get(track.id), 12);
      const item = h('div', { class:`copal-track-filter${track.enabled === false ? ' disabled' : ''}`, style:`--track-depth:${filterDepth};--track-indent:${filterDepth * 10}px`, 'data-track-id':track.id, 'data-track-depth':String(trackDepths.get(track.id)), 'data-track-path':trackPaths.get(track.id) }, label);
      if (inheritedTrack) item.append(h('small', { id:descriptionId, class:'copal-track-suppressed', text:`${inheritedReason} by ${inheritedTrack.name}` }));
      else if (track.enabled === false) item.append(h('small', { class:'copal-track-suppressed', text:'disabled' }));
      item.append(h('button', { class:'copal-track-filter-edit', type:'button', text:'Edit', 'aria-label':`Edit ${trackPaths.get(track.id)}`, onclick:(event) => openTrackEditor(track.id, event.currentTarget) }));
      filters.append(item);
    }
    toolbar.append(filters);

    scroll = h('div', { class:'copal-timeline-scroll', tabindex:'0', 'aria-label':'Scrollable timeline. Drag the background, use Shift+mouse wheel, or arrow keys to pan.' });
    const startDayParity = Math.abs(Math.floor(Date.UTC(start.getFullYear(), start.getMonth(), start.getDate()) / 86400000)) % 2;
    const canvas = h('div', { class:'copal-timeline copal-timeline-v2', style:`width:${LABEL_WIDTH + totalDays * dayWidth}px;--copal-day-width:${dayWidth}px;--copal-label-width:${LABEL_WIDTH}px;--copal-band-offset:${startDayParity ? dayWidth : 0}px` });
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

    const visibleTracks = orderedTracks.filter((track) => trackStates.get(track.id).visible);
    const rows = timeline.mode === 'regular' ? visibleTracks : [{ id:'__condensed__', name:data.title || 'All events', color:THEME_TRACK_COLOR, icon:'timeline', enabled:true, condensed:true }];
    for (const track of rows) {
      const lanes = track.condensed ? condensedLanes : (lanesByTrack.get(track.id) || { items:[], laneCount:0 });
      const candidates = lanes.items;
      const expanded = !track.condensed && lanes.laneCount > 1 && timeline.expandedTracks.has(track.id);
      const rowHeight = track.condensed ? Math.max(72, timeline.laneHeight) : expanded ? Math.max(timeline.laneHeight, 12 + lanes.laneCount * (timeline.laneHeight - 12)) : timeline.laneHeight;
      const path = track.condensed ? track.name : trackPaths.get(track.id);
      const depth = track.condensed ? 0 : trackDepths.get(track.id);
      const hasChildren = !track.condensed && childCounts.has(track.id);
      const collapsed = hasChildren && timeline.collapsedTrackGroups.has(track.id);
      const boundedDepth = Math.min(depth, 12);
      const label = h('div', { class:'copal-track-label', style:`--track-depth:${boundedDepth};--track-indent:${boundedDepth * 10}px`, title:path });
      if (!track.condensed) {
        label.append(hasChildren
          ? h('button', { class:'copal-track-disclosure', type:'button', text:collapsed ? '▸' : '▾', 'aria-expanded':String(!collapsed), 'aria-label':`${collapsed ? 'Expand' : 'Collapse'} ${path}`, onclick:() => { collapsed ? timeline.collapsedTrackGroups.delete(track.id) : timeline.collapsedTrackGroups.add(track.id); persist(); rerender({ left:scroll.scrollLeft }); } })
          : h('span', { class:'copal-track-disclosure-spacer', 'aria-hidden':'true' }));
      }
      label.append(h('span', { class:'copal-track-dot', style:`--track-color:${track.color || THEME_TRACK_COLOR}` }), h('span', { class:'copal-track-glyph', text:glyphFor(track.icon) }), h('span', { class:'copal-track-name', text:track.name }));
      if (!track.condensed) label.append(h('button', { class:'copal-track-edit', type:'button', text:'✎', title:`Edit ${track.name}`, 'aria-label':`Edit ${track.name}`, onclick:(event) => openTrackEditor(track.id, event.currentTarget) }));
      if (lanes.laneCount > 1 && !track.condensed) label.append(h('button', { class:'copal-track-overlap', type:'button', text:`${expanded ? '▾' : '▸'} ${lanes.laneCount}`, 'aria-expanded':String(expanded), 'aria-label':`${expanded ? 'Collapse' : 'Expand'} ${track.name}: ${lanes.laneCount} overlapping lanes`, onclick:() => { expanded ? timeline.expandedTracks.delete(track.id) : timeline.expandedTracks.add(track.id); persist(); rerender({ left:scroll.scrollLeft }); } }));
      const row = h('section', { class:`copal-track${expanded ? ' expanded' : ''}${track.condensed ? ` condensed ${timeline.condensedStyle}` : ''}`, style:`height:${rowHeight}px`, 'data-track-id':track.id, 'data-copal-context-object':'track', 'data-track-depth':String(depth), 'data-track-path':path, 'data-has-children':String(hasChildren), 'data-lanes':String(lanes.laneCount), 'aria-label':`${path}, ${candidates.length} events, ${lanes.laneCount} lanes` }, label);
      for (const item of lanes.items) {
        const event = item.event; const eventTrack = byTrack.get(event.trackId) || track; const top = track.condensed ? 16 + (item.lane % 3) * 14 : expanded ? 7 + item.lane * (timeline.laneHeight - 12) : 8 + Math.min(item.lane, 5) * 3;
        const left = LABEL_WIDTH + item.startDay * dayWidth; const width = Math.max(dayWidth, (item.endDay - item.startDay + 1) * dayWidth);
        const gate = manipulationGate(event, eventTrack);
        const eventChildren = [h('span', { class:'copal-event-label', text:`${(event.sharedTrackIds || []).length ? '🔗 ' : ''}${event.title}` })];
        const resizeHandles = [];
        if (gate.resizeLeft) resizeHandles.push(h('span', { class:'copal-resize-handle copal-resize-left', 'aria-hidden':'true' }));
        if (gate.resizeRight) resizeHandles.push(h('span', { class:'copal-resize-handle copal-resize-right', 'aria-hidden':'true' }));
        const eventSurface = h('span', { class:'copal-event-surface' }, ...eventChildren);
        const node = h('div', {
          class:`copal-event${event.startDate === 'FUZZY' || event.fuzzy ? ' fuzzy' : ''}${event.status === 'done' ? ' done' : ''}`,
          role:'button', tabindex:'0', 'data-task-id':event.id, 'data-timeline-event-id':event.id, 'data-copal-context-object':'event', 'data-lane':String(item.lane), style:`left:${left}px;top:${top}px;width:${width}px;--event-color:${eventTrack.color || THEME_TRACK_COLOR};--stack-index:${item.lane}`,
          'aria-label':`${event.title}, ${event.startDate || 'unscheduled'} to ${event.dueDate || 'open end'}`,
        }, eventSurface, ...resizeHandles);
        if ((event.stages || []).length) eventSurface.append(h('span', { class:'copal-event-progress', style:`--progress:${Math.round((event.stages.filter((stage) => stage.done).length / event.stages.length) * 100)}%` }));
        eventGesture(node, event, eventTrack, scroll, guide, start, dayWidth, rerender, gate); row.append(node);
      }
      canvas.append(row);
    }
    scroll.append(canvas);
    // One delegated Files handoff per mounted Timeline body. Event moves,
    // resize and pan remain local Timeline commands and never enter this
    // branch. A typed S02 payload is consumed even when unsupported so the
    // browser cannot navigate or open a local file as a page.
    const filesDropTypes = (event) => [...(event.dataTransfer?.types || [])].includes(FILES_TRANSFER_MIME);
    const onFilesDragOver = (event) => {
      const target = event.target?.closest?.('[data-timeline-event-id]');
      if (!target || (!filesDropTypes(event) && !(event.dataTransfer?.files?.length))) return;
      event.preventDefault();
      if (!filesDropTypes(event)) { event.dataTransfer.dropEffect = 'none'; setStatus('Choose a readable Files resource to attach to this event.', true); return; }
      const checked = validateTimelineFilesDrop(event.dataTransfer.getData(FILES_TRANSFER_MIME), { workspace, pane: planningScope().pane || '', generation: null });
      event.dataTransfer.dropEffect = checked.ok ? 'copy' : 'none';
      if (!checked.ok) setStatus(checked.reason, true);
    };
    const onFilesDrop = (event) => {
      const target = event.target?.closest?.('[data-timeline-event-id]');
      if (!target) return;
      event.preventDefault(); event.stopPropagation();
      const eventId = target.dataset.timelineEventId;
      const current = uniqueEvents(getPlanning()).find((item) => item.id === eventId);
      if (!current) { setStatus('This Timeline event is no longer available.', true); return; }
      if (!filesDropTypes(event)) { setStatus('Browser files must be explicitly imported before they can be attached.', true); return; }
      const scope = planningScope();
      const checked = validateTimelineFilesDrop(event.dataTransfer.getData(FILES_TRANSFER_MIME), { owner: scope.owner || '', workspace: scope.workspace || workspace, pane: scope.pane || '', generation: null });
      if (!checked.ok) { setStatus(checked.reason, true); return; }
      void attachTimelineResource(current, checked.source, { gestureContext: { raw: event.dataTransfer.getData(FILES_TRANSFER_MIME), accountId: scope.accountId || '', workspace: scope.workspace || workspace, pane: scope.pane || '', generation: checked.payload.generation, policyGeneration: checked.payload.policy_generation, selectionEpoch: checked.payload.selection_epoch, provider: checked.payload.provider, resourceKey: checked.source.resource_key, resourceRef: checked.source.resource_ref, sourceRevision: checked.source.revision } });
    };
    body.addEventListener('dragover', onFilesDragOver);
    body.addEventListener('drop', onFilesDrop);
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
    const cancelPan = () => {
      if (!pan) return;
      const current = pan;
      pan = null;
      scroll.classList.remove('dragging');
      window.removeEventListener('blur', cancelPan);
      document.removeEventListener('visibilitychange', cancelPan);
      try { if (scroll.hasPointerCapture(current.id)) scroll.releasePointerCapture(current.id); } catch (_) {}
    };
    scroll.addEventListener('pointerdown', (event) => {
      if (!timelineInputAllowed(event) || event.pointerType === 'touch' || event.target.closest('button,[role="button"],input,select,textarea,a')) return;
      const renderedWidth = scroll.getBoundingClientRect().width; const logicalWidth = scroll.clientWidth; const scale = renderedWidth > 0 && logicalWidth > 0 ? renderedWidth / logicalWidth : 1;
      pan = { x:event.clientX, left:scroll.scrollLeft, id:event.pointerId, scale }; scroll.setPointerCapture(event.pointerId); scroll.classList.add('dragging');
      window.addEventListener('blur', cancelPan); document.addEventListener('visibilitychange', cancelPan);
    });
    scroll.addEventListener('pointermove', (event) => {
      if (!pan) return;
      const nextLeft = pan.left - (event.clientX - pan.x) / pan.scale;
      scroll.scrollLeft = Math.max(0, nextLeft);
      if (nextLeft < 0) extendBackward();
    });
    const endPan = (event) => { if (!pan) return; const center = addDays(start, Math.floor(Math.max(0, scroll.scrollLeft + scroll.clientWidth / 2 - LABEL_WIDTH) / dayWidth)); cancelPan(); if (event.type === 'pointerup') persist(center); };
    scroll.addEventListener('pointerup', endPan); scroll.addEventListener('pointercancel', endPan); scroll.addEventListener('lostpointercapture', cancelPan);
    scroll.addEventListener('keydown', (event) => {
      if (!timelineInputAllowed(event)) return;
      const amount = event.key === 'PageUp' || event.key === 'PageDown' ? scroll.clientWidth * .8 : 80;
      if (['ArrowLeft','PageUp'].includes(event.key)) { event.preventDefault(); if (scroll.scrollLeft <= 0) extendBackward(); else scroll.scrollLeft -= amount; }
      if (['ArrowRight','PageDown'].includes(event.key)) { event.preventDefault(); scroll.scrollLeft += amount; }
      if (event.key === 'Home') { event.preventDefault(); scroll.scrollLeft = 0; extendBackward(); }
    });
    scroll.addEventListener('wheel', (event) => {
      // Wheel is itself an activation gesture.  A freshly opened Timeline
      // can receive it before focus has moved from the launcher, so claim the
      // owning context before applying the edge-extension policy.
      activateInputContext(timelineContext);
      if (!timelineInputAllowed(event)) return;
      const towardPast = event.deltaX < 0 || (event.shiftKey && event.deltaY < 0);
      if (towardPast && scroll.scrollLeft <= 2) extendBackward();
    }, { passive:true });
    scroll.addEventListener('scroll', () => {
      // Hidden/overlapping Timeline windows can have no laid-out width while
      // their initial scroll position is restored. Do not treat that zero
      // geometry as the history edge or schedule an endless extension loop.
      if (!scroll.clientWidth || timeline.extending || scroll.scrollLeft >= Math.max(260, scroll.clientWidth * .35)) return;
      extendBackward();
    }, { passive:true });
    const windowRoot = body.closest?.('.copal-tool-modal, [data-window-id]');
    const onTimelineFocus = () => activateInputContext(timelineContext);
    const onTimelinePointer = () => activateInputContext(timelineContext);
    const timelineContext = {
      windowId: windowRoot?.id || windowRoot?.dataset?.windowId || 'copal-planning',
      // The Timeline canvas is the window body’s active input surface.  Use
      // the same pane identity as the owning Copal window so a wheel/key
      // gesture delivered immediately after opening is accepted before the
      // canvas receives its first focus event.
      paneId: windowRoot?.id ? `${windowRoot.id}:body` : 'timeline',
      blockingModal: false,
      capabilities: { keyboard:true, pointer:true, wheel:true },
    };
    const unregisterTimelineContext = registerInputContext(body, timelineContext);
    body.addEventListener('focusin', onTimelineFocus, true);
    body.addEventListener('pointerdown', onTimelinePointer, true);
    timelineInstances.set(body, { dispose: () => { cancelPan(); body.removeEventListener('focusin', onTimelineFocus, true); body.removeEventListener('pointerdown', onTimelinePointer, true); body.removeEventListener('dragover', onFilesDragOver); body.removeEventListener('drop', onFilesDrop); unregisterTimelineContext(); } });
    timelineRoots.add(body);
    body.replaceChildren(toolbar, scroll);
    scroll.scrollTop = Number.isFinite(options.top) ? options.top : previousTop;
    if (Number.isFinite(options.left)) {
      scroll.scrollLeft = options.left;
      if (options.extensionComplete) requestAnimationFrame(() => { timeline.extending = false; });
    } else if (previousScroll && previousScroll.clientWidth === 0 && !options.anchorDate) {
      // A sibling Copal window can rerender Timeline while this surface is
      // hidden. Its zero layout width cannot produce a meaningful anchor;
      // retain the leaf's exact horizontal position until it is visible.
      scroll.scrollLeft = previousScroll.scrollLeft;
    } else {
      // On first load (no previousScroll), left-align with today ~3 days from left edge.
      // On re-renders, prefer explicit anchor → previous position → saved anchor → today.
      const anchor = options.anchorDate || previousAnchor || (previousScroll ? savedAnchor() : null) || today;
      const leftPad = previousScroll
        ? LABEL_WIDTH + (daysBetween(start, anchor) + .5) * dayWidth - scroll.clientWidth / 2
        : daysBetween(start, addDays(anchor, -INITIAL_HISTORY_DAYS)) * dayWidth;
      scroll.scrollLeft = Math.max(0, leftPad);
    }
    timelineSurfaceCache = {
      body,
      signature:renderSignature,
      staticSignature:staticRenderSignature,
      events:new Map(allEvents.map((event) => [event.id, Object.freeze({
        ...event,
        sharedTrackIds:Array.isArray(event.sharedTrackIds) ? [...event.sharedTrackIds] : [],
        fuzzy:event.fuzzy && typeof event.fuzzy === 'object' ? { ...event.fuzzy } : event.fuzzy,
        stages:Array.isArray(event.stages) ? event.stages.map((stage) => ({ ...stage })) : [],
      })])),
      eventSignatures:new Map(allEvents.map((event) => [event.id, timelineEventSignature(event)])),
      geometrySignatures:new Map(allEvents.map((event) => [event.id, timelineEventGeometrySignature(event)])),
      eventNodes:timelineEventNodeIndex(body.querySelectorAll('.copal-event')),
    };
  }

  function renderTodo(body, markdownItems = [], projectionMeta = {}) {
    const data = getPlanning(); const byTrack = trackMap(data); const events = uniqueEvents(data);
    const allItems = [...events.map((event) => ({ type:'event', event, title:event.title, description:event.description || '', status:event.status || 'pending', priority:event.priority || 'medium', startDate:event.startDate, dueDate:event.dueDate, trackId:event.trackId, tags:event.tags || [], stages:event.stages || [] })),
      ...markdownItems.map((item) => ({ type:'markdown', title:item.task?.text || '', description:'', status:item.task?.done ? 'done' : 'pending', priority:'medium', startDate:null, dueDate:null, trackId:null, tags:[], stages:[], mdItem:item }))];

    const saved = loadTaskSettings();
    let activeViewId = 'all-open';
    let columns = saved.columns || JSON.parse(JSON.stringify(DEFAULT_COLUMNS));
    let views = saved.views || JSON.parse(JSON.stringify(DEFAULT_VIEWS));
    let sourceFilter = 'all', dateFilter = 'all', learningFilter = 'all';
    let statusFilter = 'all', priorityFilter = 'all', sortKey = 'start', hideDone = false;
    let groupBy = 'none', focusedIdx = -1;

    // Apply default view config
    const applyView = (viewConfig) => {
      sourceFilter = viewConfig.sourceFilter || 'all';
      dateFilter = viewConfig.dateFilter || 'all';
      learningFilter = viewConfig.learningFilter || 'all';
      statusFilter = viewConfig.statusFilter || 'all';
      priorityFilter = viewConfig.priorityFilter || 'all';
      sortKey = viewConfig.sortKey || 'start';
      hideDone = !!viewConfig.hideDone;
      groupBy = viewConfig.groupBy || 'none';
      if (viewConfig.columns) columns = JSON.parse(JSON.stringify(viewConfig.columns));
    };
    applyView(views.find((v) => v.id === activeViewId) || views[0]);

    const root = h('section', { class:'copal-meatbag-tasks copal-pane' });
    const header = h('header', { class:'copal-pane-header' });
    const count = h('strong', { text:`Meatbag Tasks · ${allItems.length}` });
    header.append(count, h('button', { class:'copal-btn primary', text:'+ Task', onclick:() => createMarkdownTask ? createMarkdownTask() : createEvent({ startDate:null, dueDate:null, trackId:null, floating:true }) }));
    if (loadMoreMarkdownTasks) header.append(h('button', { class:'copal-btn', text:'Load more', disabled:!canLoadMoreMarkdownTasks(), onclick:() => loadMoreMarkdownTasks() }));

    // ── Controls row 1: search + core filters ──
    const controls = h('div', { class:'copal-tasks-controls' });
    const search = h('input', { type:'search', placeholder:'Search tasks…', 'aria-label':'Search tasks', style:'flex:1;min-width:120px' });
    const statusSel = h('select', { 'aria-label':'Status filter' },
      h('option', { value:'all', text:'All status' }), h('option', { value:'pending', text:'Pending' }), h('option', { value:'in-progress', text:'In progress' }), h('option', { value:'done', text:'Done' }));
    const prioritySel = h('select', { 'aria-label':'Priority filter' },
      h('option', { value:'all', text:'All priority' }), h('option', { value:'high', text:'High' }), h('option', { value:'medium', text:'Medium' }), h('option', { value:'low', text:'Low' }));
    const sortSel = h('select', { 'aria-label':'Sort tasks' },
      h('option', { value:'start', text:'Sort by start' }), h('option', { value:'due', text:'Sort by due' }), h('option', { value:'priority', text:'Sort by priority' }));
    const hideDoneCheck = h('input', { type:'checkbox', 'aria-label':'Hide done tasks' });
    const hideLabel = h('label', { class:'copal-check' }, hideDoneCheck, h('span', { text:'Hide done' }));
    controls.append(search, statusSel, prioritySel, sortSel, hideLabel);

    // ── Controls row 2: source / date / learning / group ──
    const controls2 = h('div', { class:'copal-tasks-controls' });
    const sourceSel = h('select', { 'aria-label':'Source filter' },
      h('option', { value:'all', text:'All sources' }), h('option', { value:'vault', text:'Vault' }), h('option', { value:'timeline', text:'Timeline' }));
    const dateSel = h('select', { 'aria-label':'Date filter' },
      h('option', { value:'all', text:'All dates' }), h('option', { value:'due', text:'Due' }), h('option', { value:'scheduled', text:'Scheduled' }), h('option', { value:'undated', text:'Undated' }), h('option', { value:'overdue', text:'Overdue' }));
    const learningSel = h('select', { 'aria-label':'Learning filter' },
      h('option', { value:'all', text:'All learning' }), h('option', { value:'course', text:'Course' }), h('option', { value:'skill', text:'Skill' }));
    const groupSel = h('select', { 'aria-label':'Group by' },
      h('option', { value:'none', text:'No grouping' }), h('option', { value:'track', text:'Group: Track' }), h('option', { value:'status', text:'Group: Status' }), h('option', { value:'priority', text:'Group: Priority' }));
    controls2.append(sourceSel, dateSel, learningSel, groupSel);

    // ── Controls row 3: view selector + columns button ──
    const controls3 = h('div', { class:'copal-tasks-controls' });
    const viewSel = h('select', { 'aria-label':'Saved views' });
    for (const v of views) viewSel.append(h('option', { value:v.id, text:v.name }));
    const saveViewBtn = h('button', { class:'copal-btn', text:'Save view', title:'Save current filters as a view' });
    const colsBtn = h('button', { class:'copal-btn', text:'Columns', title:'Toggle column visibility' });
    controls3.append(viewSel, saveViewBtn, colsBtn);

    // Server-backed Markdown filters are part of the view state. Rebuilding the
    // list after a query must leave the visible controls at the values that
    // produced this page instead of silently restoring the saved default view.
    const serverQuery = projectionMeta.query || {};
    if (serverQuery.query != null) search.value = String(serverQuery.query);
    if (serverQuery.sourceFilter) sourceFilter = String(serverQuery.sourceFilter);
    if (serverQuery.statusFilter) statusFilter = String(serverQuery.statusFilter);
    if (serverQuery.hideDone != null) hideDone = !!serverQuery.hideDone;
    const selectedTaskId = projectionMeta.selectedTaskId == null ? null : String(projectionMeta.selectedTaskId);

    // ── Column chooser panel ──
    const colsPanel = h('div', { class:'copal-tasks-cols-panel' });
    colsPanel.style.display = 'none';
    const buildColsPanel = () => {
      colsPanel.replaceChildren();
      const sorted = [...columns].sort((a, b) => a.order - b.order);
      for (const col of sorted) {
        const cb = h('input', { type:'checkbox', 'aria-label':`Show ${col.label} column` });
        cb.checked = col.visible;
        cb.addEventListener('change', () => { col.visible = cb.checked; persistTaskSettings({ columns, views }); draw(); });
        colsPanel.append(h('label', { class:'copal-check' }, cb, h('span', { text:col.label })));
      }
    };
    buildColsPanel();
    colsBtn.addEventListener('click', () => { colsPanel.style.display = colsPanel.style.display === 'none' ? '' : 'none'; });

    // ── Save current state as a view ──
    saveViewBtn.addEventListener('click', async () => {
      const name = await styledPrompt('Give this task view a name.', {
        title: 'Save task view', placeholder: 'View name', confirmText: 'Save view', maxLength: 80,
      });
      if (!name?.trim()) return;
      const id = `custom-${Date.now().toString(36)}`;
      views.push({ id, name:name.trim(), config:{ sourceFilter, dateFilter, learningFilter, statusFilter, priorityFilter, sortKey, hideDone, groupBy, columns:JSON.parse(JSON.stringify(columns)) } });
      persistTaskSettings({ columns, views });
      viewSel.append(h('option', { value:id, text:name.trim() }));
      viewSel.value = id;
    });

    // ── View selector ──
    viewSel.addEventListener('change', () => {
      const v = views.find((item) => item.id === viewSel.value);
      if (v) { activeViewId = v.id; applyView(v.config); syncControlsFromState(); draw(); }
    });

    const syncControlsFromState = () => {
      statusSel.value = statusFilter;
      prioritySel.value = priorityFilter;
      sortSel.value = sortKey;
      hideDoneCheck.checked = hideDone;
      sourceSel.value = sourceFilter;
      dateSel.value = dateFilter;
      learningSel.value = learningFilter;
      groupSel.value = groupBy;
      buildColsPanel();
    };
    syncControlsFromState();

    const list = h('div', { class:'copal-tasks-list', tabindex:'0', role:'grid', 'aria-label':'Task list' });
    // Keep large projections responsive by mounting only the visible window.
    // Small filtered sets retain the ordinary DOM so keyboard/context behavior
    // remains exact and the list still exposes every row to assistive tooling.
    const virtualThreshold = 300;
    const virtualRowHeight = 44;
    const virtualOverscan = 12;
    let virtualFrame = 0;
    let virtualActive = false;
    const scheduleVirtualDraw = () => {
      if (!virtualActive || virtualFrame) return;
      virtualFrame = requestAnimationFrame(() => { virtualFrame = 0; draw(); });
    };
    list.addEventListener('scroll', scheduleVirtualDraw, { passive:true });
    const doneCount = h('span', { class:'copal-tasks-done-count' });

    const buildRow = (item) => {
      const isEvent = item.type === 'event';
      const visibleCols = columns.filter((c) => c.visible).sort((a, b) => a.order - b.order);
      const checkbox = control('input', { type:'checkbox', 'aria-label':`Complete ${item.title}`, tabindex:'-1' });
      checkbox.checked = item.status === 'done';
      checkbox.addEventListener('change', async() => {
        checkbox.disabled = true;
        try {
          if (isEvent) await patchEvent(item.event, { status:checkbox.checked ? 'done' : 'pending' });
          else if (patchMarkdownTask) await patchMarkdownTask(item.mdItem, checkbox.checked);
        }
        catch (_) { checkbox.checked = !checkbox.checked; }
        checkbox.disabled = false;
      });
      const trackName = item.trackId ? `${glyphFor(byTrack.get(item.trackId)?.icon)} ${byTrack.get(item.trackId)?.name || 'Unknown track'}` : '';
      const trackText = isEvent ? (trackName || 'Unscheduled') : (item.mdItem?.label || '');
      const startLabel = item.startDate ? (item.startDate === 'FUZZY' ? '?' : item.startDate) : '';
      const dueLabel = item.dueDate || '';
      const cellMap = {
        title: h('button', { class:'copal-task-title', tabindex:'-1', text:item.title || 'Untitled', onclick:() => isEvent ? openEventEditor(item.event.id) : openMarkdownTask ? openMarkdownTask(item.mdItem) : item.mdItem?.doc && openDocument(item.mdItem.doc.id, 'notes') }),
        status: h('span', { class:`copal-task-status status-${item.status}`, text:item.status }),
        priority: item.priority && item.priority !== 'medium' ? h('span', { class:`copal-badge priority-${item.priority}`, text:item.priority }) : h('span', { class:'copal-task-status', text:'medium' }),
        start: h('small', { class:'copal-task-dates', text:startLabel || '—' }),
        due: h('small', { class:'copal-task-dates', text:dueLabel || '—' }),
        track: h('small', { class:'copal-task-track', text:trackText }),
        tags: h('small', { class:'copal-task-tags', text:(item.tags || []).join(', ') || '—' }),
        stages: h('small', { class:'copal-task-stages', text:(item.stages || []).join(', ') || '—' }),
      };
      const cells = [h('div', { class:'copal-task-cell copal-task-cell-check' }, checkbox)];
      for (const col of visibleCols) cells.push(h('div', { class:`copal-task-cell copal-task-cell-${col.id}` }, cellMap[col.id] || h('span', { text:'' })));
      const row = h('div', { class:`copal-task-row${checkbox.checked ? ' done' : ''}`, role:'row', tabindex:'-1', 'data-copal-context-object':'task' }, ...cells);
      row.dataset.taskId = String(item.mdItem?.id || item.event?.id || '');
      row.setAttribute('aria-selected', String(selectedTaskId != null && row.dataset.taskId === selectedTaskId));
      row._taskItem = item;
      return row;
    };

    const priorityOrder = { high:0, medium:1, low:2 };
    const today = formatLocalDate(new Date());

    const draw = () => {
      focusedIdx = -1;
      const query = search.value.trim().toLowerCase();
      statusFilter = statusSel.value;
      priorityFilter = prioritySel.value;
      sortKey = sortSel.value;
      hideDone = hideDoneCheck.checked;
      sourceFilter = sourceSel.value;
      dateFilter = dateSel.value;
      learningFilter = learningSel.value;
      groupBy = groupSel.value;

      const filtered = allItems.filter((item) => {
        if (hideDone && item.status === 'done') return false;
        if (statusFilter !== 'all' && item.status !== statusFilter) return false;
        if (priorityFilter !== 'all' && item.priority !== priorityFilter) return false;
        // E1: source filter
        if (sourceFilter !== 'all' && (sourceFilter === 'vault' ? item.type !== 'markdown' : sourceFilter === 'timeline' ? item.type !== 'event' : item.type !== sourceFilter)) return false;
        // E2: date filter
        if (dateFilter === 'due' && !item.dueDate) return false;
        if (dateFilter === 'scheduled' && !item.startDate) return false;
        if (dateFilter === 'undated' && (item.startDate || item.dueDate)) return false;
        if (dateFilter === 'overdue' && !(item.dueDate && item.dueDate < today && item.status !== 'done')) return false;
        // E3: learning filter
        if (learningFilter !== 'all') {
          const prefix = learningFilter + '/';
          if (!item.tags.some((t) => t.startsWith(prefix))) return false;
        }
        // E4: search scope — title + description + tags
        if (query) {
          const haystack = `${item.title} ${item.description} ${(item.tags || []).join(' ')}`.toLowerCase();
          if (!haystack.includes(query)) return false;
        }
        return true;
      });

      filtered.sort((a, b) => {
        if (sortKey === 'priority') return (priorityOrder[a.priority] ?? 1) - (priorityOrder[b.priority] ?? 1);
        const av = sortKey === 'due' ? a.dueDate : a.startDate;
        const bv = sortKey === 'due' ? b.dueDate : b.startDate;
        if (!av && !bv) return 0;
        if (!av) return 1;
        if (!bv) return -1;
        return av.localeCompare(bv);
      });

      const doneTotal = allItems.filter((item) => item.status === 'done').length;
      doneCount.textContent = doneTotal ? `${doneTotal} done` : '';
      const projectedTotal = Number.isFinite(Number(projectionMeta.total)) ? Number(projectionMeta.total) : null;
      const hasExactServerTotal = projectedTotal !== null && projectionMeta.totalExact !== false;
      const localEventsIncluded = sourceFilter === 'all' && !query && statusFilter === 'all' && !hideDone;
      const totalLabel = hasExactServerTotal ? projectedTotal + (localEventsIncluded ? events.length : 0) : allItems.length;
      count.textContent = `Meatbag Tasks · ${filtered.length}${filtered.length !== totalLabel ? ` of ${totalLabel}` : ''}`;
      const useVirtualWindow = groupBy === 'none' && filtered.length > virtualThreshold;
      virtualActive = useVirtualWindow;
      const priorScrollTop = list.scrollTop;
      if (useVirtualWindow) {
        list.style.maxHeight = '640px';
        list.style.overflowY = 'auto';
      } else {
        list.style.maxHeight = '';
        list.style.overflowY = '';
      }
      list.replaceChildren();

      if (!filtered.length) {
        list.append(h('div', { class:'copal-empty', text:allItems.length ? 'No tasks match the current filters.' : 'No Meatbag Tasks yet.' }));
        return;
      }

      // E7: grouping
      if (groupBy !== 'none') {
        const groupKey = groupBy;
        const groupMap = new Map();
        for (const item of filtered) {
          let key;
          if (groupKey === 'track') key = item.trackId ? (byTrack.get(item.trackId)?.name || 'Unknown') : 'Unscheduled';
          else if (groupKey === 'status') key = item.status;
          else if (groupKey === 'priority') key = item.priority;
          else key = 'Other';
          if (!groupMap.has(key)) groupMap.set(key, []);
          groupMap.get(key).push(item);
        }
        for (const [groupName, groupItems] of groupMap) {
          const header = h('div', { class:'copal-task-group-header', role:'row' },
            h('span', { class:'copal-task-group-toggle', text:'▸' }),
            h('span', { text:`${groupName} (${groupItems.length})` }));
          const body = h('div', { class:'copal-task-group-body' });
          for (const item of groupItems) body.append(buildRow(item));
          header.addEventListener('click', () => {
            const collapsed = body.style.display === 'none';
            body.style.display = collapsed ? '' : 'none';
            header.querySelector('.copal-task-group-toggle').textContent = collapsed ? '▸' : '▾';
          });
          list.append(header, body);
        }
      } else {
        let start = 0;
        let end = filtered.length;
        if (useVirtualWindow) {
          const viewport = Math.max(1, Math.ceil((list.clientHeight || 640) / virtualRowHeight));
          start = Math.max(0, Math.floor(priorScrollTop / virtualRowHeight) - virtualOverscan);
          end = Math.min(filtered.length, start + viewport + virtualOverscan * 2);
          if (start > 0) list.append(h('div', { 'aria-hidden':'true', style:`height:${start * virtualRowHeight}px` }));
        }
        for (const item of filtered.slice(start, end)) list.append(buildRow(item));
        if (useVirtualWindow && end < filtered.length) list.append(h('div', { 'aria-hidden':'true', style:`height:${(filtered.length - end) * virtualRowHeight}px` }));
        if (useVirtualWindow) list.scrollTop = priorScrollTop;
      }
      if (selectedTaskId != null && document.activeElement === document.body) {
        list.querySelector(`[data-task-id="${CSS.escape(selectedTaskId)}"]`)?.focus({ preventScroll:true });
      }
    };

    // Wire filter change events
    const refreshMarkdownQuery = () => {
      if (!queryMarkdownTasks) { draw(); return; }
      const serverSource = sourceSel.value === 'vault' ? 'vault' : 'all';
      const completed = hideDoneCheck.checked ? false : statusSel.value === 'done' ? true : statusSel.value === 'pending' ? false : null;
      queryMarkdownTasks({ query:search.value.trim(), completed, source:serverSource, sourceFilter:sourceSel.value, statusFilter:statusSel.value, hideDone:hideDoneCheck.checked });
    };
    search.addEventListener('input', refreshMarkdownQuery);
    statusSel.addEventListener('change', refreshMarkdownQuery);
    prioritySel.addEventListener('change', draw);
    sortSel.addEventListener('change', draw);
    hideDoneCheck.addEventListener('change', refreshMarkdownQuery);
    sourceSel.addEventListener('change', refreshMarkdownQuery);
    dateSel.addEventListener('change', draw);
    learningSel.addEventListener('change', draw);
    groupSel.addEventListener('change', draw);

    // E8: keyboard grid navigation
    list.addEventListener('keydown', (e) => {
      const rows = [...list.querySelectorAll('.copal-task-row')];
      if (!rows.length) return;
      const key = e.key;
      if (key === 'ArrowDown') { e.preventDefault(); focusedIdx = Math.min(focusedIdx + 1, rows.length - 1); rows[focusedIdx]?.focus(); }
      else if (key === 'ArrowUp') { e.preventDefault(); focusedIdx = Math.max(focusedIdx - 1, 0); rows[focusedIdx]?.focus(); }
      else if (key === 'Enter') {
        e.preventDefault();
        const row = rows[focusedIdx] || rows[0];
        if (row?._taskItem) {
          const item = row._taskItem;
          if (item.type === 'event') openEventEditor(item.event.id);
          else if (openMarkdownTask) openMarkdownTask(item.mdItem);
          else item.mdItem?.doc && openDocument(item.mdItem.doc.id, 'notes');
        }
      }
      else if (key === ' ') {
        e.preventDefault();
        const row = rows[focusedIdx] || rows[0];
        const cb = row?.querySelector('input[type="checkbox"]');
        if (cb) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); }
      }
    });
    list.addEventListener('focusin', (e) => {
      const row = e.target.closest('.copal-task-row');
      if (row) {
        const rows = [...list.querySelectorAll('.copal-task-row')];
        focusedIdx = rows.indexOf(row);
        rows.forEach((r, i) => r.classList.toggle('copal-task-focused', i === focusedIdx));
        projectionMeta.onSelect?.(row.dataset.taskId || null);
      }
    });

    root.append(header, controls, controls2, controls3, colsPanel, list, doneCount);
    draw();
    body.replaceChildren(root);
  }

  async function handleContextCommand(command, target) {
    const row = target?.closest?.('.copal-task-row') || target;
    const item = row?._taskItem;
    if (command === 'edit-track') {
      const trackId = target?.dataset?.trackId || target?.closest?.('[data-track-id]')?.dataset?.trackId;
      if (!trackId) return false;
      openTrackEditor(trackId, target);
      return true;
    }
    if (command === 'edit-event') {
      const eventId = target?.dataset?.taskId || target?.closest?.('[data-task-id]')?.dataset?.taskId;
      if (!eventId) return false;
      await openEventEditor(eventId, target);
      return true;
    }
    if (command !== 'toggle-task' && command !== 'open-task') return false;
    if (!item) return false;
    if (command === 'open-task') {
      if (item.type === 'event') await openEventEditor(item.event.id, target);
      else if (openMarkdownTask) openMarkdownTask(item.mdItem);
      else if (item.mdItem?.doc) openDocument(item.mdItem.doc.id, 'notes');
      return true;
    }
    const nextChecked = item.type === 'event' ? item.status !== 'done' : !item.mdItem?.task?.done;
    if (item.type === 'event') await patchEvent(item.event, { status:nextChecked ? 'done' : 'pending' });
    else if (patchMarkdownTask) await patchMarkdownTask(item.mdItem, nextChecked);
    return true;
  }

  return { loadState, renderTimeline, renderTodo, openEventEditor, openTrackEditor, createEvent, patchEvent, attachTimelineResource, handleContextCommand, suspendScope, glyphFor, get timelineState() { return timeline; } };
}
