// The one selected-worker inspector (O02 on top of the O10-A passive read). It is mounted by the Tasks Workload
// into its detail region, which is also what the shared chat-visible panel hosts. Every server string goes in via
// textContent. Viewing only issues GET /inspection (no ensure_session, no receipt/upload mutation); the only write
// is the explicit follow-up, delegated to the existing O07 durable-input path.
import { deliveryStatus } from './chatInputDelivery.js';

const node = (doc, tag, text, className) => {
  const el = doc.createElement(tag);
  if (text != null) el.textContent = text;
  if (className) el.className = className;
  return el;
};
const button = (doc, label, onClick, className) => {
  const b = node(doc, 'button', label, className); b.type = 'button'; b.addEventListener('click', onClick); return b;
};

const REASONS = {
  ACTOR_NOT_FOUND: 'This worker is no longer registered. Nothing was sent and no replacement worker was started.',
  ACTOR_CHAT_BINDING_MISMATCH: 'This worker belongs to a different chat.',
  ACTOR_NOT_READY: 'This worker has not finished registering yet. Try again shortly.',
  CHILD_SESSION_NOT_MAPPED: 'The worker session is no longer part of this chat.',
};

async function refusalReason(response) {
  let detail = null;
  try { detail = (await response.json())?.detail; } catch { /* no body */ }
  const code = typeof detail === 'object' ? detail?.code : '';
  if (REASONS[code]) return REASONS[code];
  if (response.status === 409) return 'The worker or chat changed since this view was opened.';
  if (response.status === 404) return 'This worker could not be found.';
  if (response.status === 403) return 'Not permitted.';
  return `The worker is unavailable (${response.status}).`;
}

const refKey = ref => ref?.kind && ref.sessionId && ref.nativeId
  ? JSON.stringify([ref.kind, ref.sessionId, ref.nativeId]) : null;
const sameRef = (a, b) => refKey(a) !== null && refKey(a) === refKey(b);
const parentRef = row => (row.links || []).find(l => l.kind === 'actor_parent'
  && l.target?.kind === 'native_actor' && refKey(l.target))?.target;
const sessionRef = sessionId => ({ kind: 'session', sessionId, nativeId: sessionId });

function actorEntries(relations, selectedRow) {
  const entries = new Map();
  for (const entry of relations?.actors || []) {
    const ref = entry.row.sourceRef;
    const key = refKey(ref);
    const previous = entries.get(key)?.row.sourceRef;
    if (ref?.kind === 'native_actor' && key && (!previous || ref.executionRevision > previous.executionRevision)) {
      entries.set(key, entry);
    }
  }
  if (selectedRow) {
    const key = refKey(selectedRow.sourceRef);
    const entry = entries.get(key);
    if (key) entries.set(key, { ...entry, row: selectedRow });
  }
  return entries;
}

/** Only recorded links involving this worker or its session, drawn from bounded observation pages. */
export function observedEdges(relations, actorId, sessionId, selectedRow) {
  const { tasks = [], workflows = [], rootSessionId } = relations || {};
  const actors = actorEntries(relations, selectedRow);
  const selected = { kind: 'native_actor', sessionId, nativeId: actorId };
  const self = actors.get(refKey(selected));
  const edges = [];
  let unknownCoverage = !self;
  for (const { row } of actors.values()) {
    const actor = row.sourceRef;
    const parent = parentRef(row);
    if (sameRef(actor, selected) || sameRef(parent, selected)) {
      if (parent) edges.push({ kind: 'actor_parent', from: parent, to: actor });
      if (row.linkCoverage === 'unknown' && (!parent || !actors.has(refKey(parent)))) unknownCoverage = true;
    }
  }
  if (self?.peer && rootSessionId && rootSessionId !== sessionId) {
    edges.push({ kind: 'peer_session', from: sessionRef(rootSessionId), to: sessionRef(sessionId) });
  }
  const assigned = new Set();
  for (const row of tasks) {
    const task = row.sourceRef;
    if (task.sessionId !== sessionId || !refKey(task)) continue;
    if ((row.links || []).some(link => link.kind === 'assigned_to' && sameRef(link.target, selected))) {
      edges.push({ kind: 'assigned_to', from: task, to: selected });
      assigned.add(refKey(task));
      if (row.linkCoverage === 'unknown') unknownCoverage = true;
    }
  }
  for (const row of tasks) {
    const task = row.sourceRef;
    for (const link of row.links || []) {
      const parent = link.target;
      if (link.kind === 'task_parent' && parent?.kind === 'native_task' && parent.sessionId === task.sessionId
          && refKey(parent) && (assigned.has(refKey(task)) || assigned.has(refKey(parent)))) {
        edges.push({ kind: 'task_parent', from: parent, to: task });
      }
    }
  }
  for (const row of workflows) {
    if (row.sourceRef.sessionId === sessionId && sessionId === rootSessionId) {
      edges.push({ kind: 'workflow_session', from: row.sourceRef, to: sessionRef(sessionId) });
    }
  }
  return { edges, unknownCoverage };
}

/** Effective model of the selected observation, when Native reports it. */
export function effectiveModelOf(row) {
  const m = row?.effectiveModel;
  if (!m || typeof m !== 'object') return null;
  const provider_id = m.provider_id || m.providerID, model_id = m.model_id || m.modelID;
  return provider_id && model_id ? { provider_id, model_id } : null;
}

/** Accounting for the selected execution and its observed, session-qualified parent and children. */
export function accountingFor(relations, row) {
  const selected = row.sourceRef;
  const selectedParent = parentRef(row);
  const actors = [];
  for (const { row: related } of actorEntries(relations, row).values()) {
    const ref = related.sourceRef;
    const parent = parentRef(related);
    if (!sameRef(ref, selected) && !sameRef(parent, selected) && !sameRef(ref, selectedParent)) continue;
    const effective = effectiveModelOf(related);
    actors.push({
      id: ref.nativeId, session_id: ref.sessionId, execution_revision: ref.executionRevision,
      ...(parent ? { parent_id: parent.nativeId, parent_session_id: parent.sessionId } : {}),
      description: related.title, status: related.nativeStatus,
      ...(related.requestedModel ? { requested_model: related.requestedModel } : {}),
      ...(effective ? { effective_model: effective } : {}),
    });
  }
  return { state: 'partial', total: actors.length, actors };
}

/**
 * Mount the inspector in `container`. Options:
 *  scope {chat, mapRevision}, row (observation row of the selected actor), relations() (observed rows),
 *  current() (original scope + selection still current), renderAccounting, sendFollowUp, onSelectActor(entry),
 *  onReturnToChat(), onRefresh(), onClose(), opener, fetchImpl, doc.
 */
export function openAgentRunInspector(options) {
  const { container, scope, row, relations, current, renderAccounting, sendFollowUp, onSelectActor, onReturnToChat, onRefresh, onClose, opener } = options;
  const doc = options.doc || document;
  const fetcher = options.fetchImpl || ((...args) => fetch(...args));
  const ref = row.sourceRef;
  const controller = new AbortController();
  const live = () => !controller.signal.aborted && (typeof current !== 'function' || current() === true);
  const base = `/api/orchestration/${encodeURIComponent(scope.chat)}/actors/${encodeURIComponent(ref.nativeId)}/inspection`;
  const query = extra => new URLSearchParams({ map_revision: String(scope.mapRevision), actor_session_id: ref.sessionId,
    execution_revision: String(ref.executionRevision), ...extra });
  const get = async extra => {
    const response = await fetcher(`${base}?${query(extra)}`, { credentials: 'same-origin', cache: 'no-store', signal: controller.signal });
    if (!response.ok) throw Object.assign(new Error('refused'), { reason: await refusalReason(response) });
    return response.json();
  };

  const panel = node(doc, 'section', null, 'task-workload-detail agent-run-inspector');
  panel.setAttribute('role', 'region'); panel.setAttribute('aria-label', `Worker: ${row.title || ref.nativeId}`);
  const head = node(doc, 'div', null, 'ari-head');
  const nav = node(doc, 'div', null, 'ari-nav');
  const status = node(doc, 'p', `Execution status: ${row.nativeStatus || 'unknown'} · revision ${ref.executionRevision}`, 'ari-status');
  const model = node(doc, 'div', null, 'ari-model');
  const tree = node(doc, 'div', null, 'ari-tree');
  const note = node(doc, 'p', 'Loading worker transcript…', 'task-workload-coverage ari-note');
  note.setAttribute('role', 'status');
  const list = node(doc, 'div', null, 'ari-transcript');
  const receiptsBox = node(doc, 'div', null, 'ari-receipts');
  const communicationBox = node(doc, 'div', null, 'ari-receipts');
  const more = node(doc, 'div', null, 'ari-more');
  const follow = node(doc, 'div', null, 'ari-follow');

  const close = () => { controller.abort(); panel.remove?.(); onClose?.(); opener?.focus?.(); };
  const dispose = () => controller.abort();
  head.append(node(doc, 'h4', row.title || 'Worker'),
    node(doc, 'small', `Actor ${ref.nativeId} · Session ${ref.sessionId}`), button(doc, 'Close details', close));
  // Root chat navigation and child worker navigation are separate controls with separate labels.
  nav.append(node(doc, 'span', 'Root chat', 'ari-label'),
    button(doc, 'Return to chat', () => { if (live()) onReturnToChat?.(); }, 'ari-root-nav'));

  const rel = typeof relations === 'function' ? relations() : relations;
  // Model state: reuse chatRenderer.renderActorAccounting for the accounting line; the UI never picks or offers a model.
  const wrap = node(doc, 'div', null, 'ari-accounting'); model.append(wrap);
  if (typeof renderAccounting === 'function') { try { renderAccounting(wrap, accountingFor(rel, row)); } catch { wrap.textContent = 'Actor accounting unavailable.'; } }
  else wrap.textContent = 'Actor accounting unavailable.';
  const effective = effectiveModelOf(row);
  model.append(node(doc, 'p', row.requestedModel || effective
    ? `Requested model ${row.requestedModel || 'unreported'} · Effective model ${effective ? `${effective.provider_id}/${effective.model_id}` : 'unreported'}`
    : 'Requested and effective models unavailable in this observation.', 'task-workload-coverage'));

  function renderTree() {
    tree.replaceChildren(node(doc, 'h5', 'Observed relations'));
    tree.append(node(doc, 'p', 'Partial coverage of loaded root and direct-peer pages.', 'task-workload-coverage'));
    const { edges, unknownCoverage } = observedEdges(rel, ref.nativeId, ref.sessionId, row);
    if (!edges.length) tree.append(node(doc, 'p', 'No relations observed in these pages.', 'task-workload-coverage'));
    const actors = actorEntries(rel, row);
    const label = target => target.kind === 'session' ? target.sessionId : `${target.nativeId} (session ${target.sessionId})`;
    for (const edge of edges) {
      const line = node(doc, 'div', null, `ari-edge ari-edge-${edge.kind}`);
      line.append(node(doc, 'code', edge.kind), node(doc, 'span', ` ${label(edge.from)} → ${label(edge.to)}`));
      if (edge.kind === 'actor_parent') {
        const other = sameRef(edge.to, ref) ? edge.from : edge.to;
        const target = actors.get(refKey(other));
        if (target && !sameRef(other, ref) && Number.isSafeInteger(target.row.sourceRef.executionRevision)) {
          line.append(button(doc, 'Inspect worker', () => { if (live()) onSelectActor?.(target); }, 'ari-child-nav'));
        }
      }
      tree.append(line);
    }
    if (unknownCoverage) tree.append(node(doc, 'p', 'Link coverage: unknown for records outside the loaded pages or awaiting verification.', 'task-workload-coverage'));
  }
  renderTree();

  // Paged public transcript: bounded text and tool state, with explicit omissions.
  let cursor = null, busy = false, stale = false, loaded = 0;
  const seen = new Set();
  function renderReceipts(items) {
    receiptsBox.replaceChildren();
    if (items?.length) receiptsBox.append(node(doc, 'h5', 'Input states'));
    for (const receipt of items || []) receiptsBox.append(node(doc, 'p', `Input ${receipt.requestID}: ${receipt.state}`, 'ari-receipt'));
  }
  function renderCommunication(communication) {
    const available = communication?.coverage === 'partial';
    communicationBox.replaceChildren(node(doc, 'h5', 'Addressed communication'), node(doc, 'p', available
      ? 'Partial history of recent incoming receipts from the root and selected sessions. Receipts can span worker executions.'
      : 'Communication history is unavailable from this engine.', 'task-workload-coverage'));
    if (!available) return;
    if (!communication.items?.length) communicationBox.append(node(doc, 'p', 'No addressed receipts observed in this page.'));
    for (const item of communication.items || []) {
      const entry = node(doc, 'div', null, 'ari-edge');
      entry.append(node(doc, 'p', `${item.senderActorID} (session ${item.senderSessionID}) → ${ref.nativeId} (session ${ref.sessionId})`),
        node(doc, 'small', [item.kind, item.state, new Date(item.time).toLocaleString(), `receipt ${item.id}`,
          item.inputMessageID && `input ${item.inputMessageID}`,
          item.deliveryMessageID && `delivery message ${item.deliveryMessageID}`].filter(Boolean).join(' · ')));
      communicationBox.append(entry);
    }
  }
  function markStale(reason) {
    stale = true; send.disabled = true; text.disabled = true;
    note.textContent = `${reason} Refresh the Workload to select the current worker; follow-up is disabled.`;
    more.replaceChildren(button(doc, 'Refresh', () => { if (live() || !controller.signal.aborted) onRefresh?.(); }, 'ari-refresh'));
  }
  async function loadPage() {
    if (busy || stale) return; busy = true; more.replaceChildren();
    const first = loaded === 0;
    note.textContent = first ? 'Loading worker transcript…' : 'Loading older transcript…';
    try {
      const value = await get(cursor ? { message_cursor: cursor } : {});
      if (!live()) return;
      if (value.actorID !== ref.nativeId || value.sessionID !== ref.sessionId || value.executionRevision !== ref.executionRevision) throw Object.assign(new Error('mismatch'), { reason: 'The worker changed since this view was opened.' });
      status.textContent = `Execution status: ${value.status} · revision ${value.executionRevision}` +
        (row.nativeOutcome && value.status === 'idle' ? ` · last outcome: ${typeof row.nativeOutcome === 'string' ? row.nativeOutcome : 'recorded'}` : '');
      for (const message of value.messages || []) {
        if (seen.has(message.id)) continue; seen.add(message.id); loaded++;
        const item = node(doc, 'div', null, 'ari-message');
        const when = Number.isSafeInteger(message.time) ? new Date(message.time).toLocaleString() : 'time unavailable';
        item.append(node(doc, 'small', `${message.role || 'unknown'} · ${when} · message ${message.messageID || 'unknown'} · part ${message.id}`, 'ari-message-meta'));
        const content = message.kind === 'tool' ? `Tool ${message.tool || 'unavailable'} · ${message.state || 'state unavailable'}`
          : message.kind === 'omitted' ? `Content omitted (${message.omission || 'unavailable'})` : (message.text ?? 'Content omitted');
        item.append(node(doc, 'p', content)); list.append(item);
      }
      renderReceipts(value.receipts?.items);
      renderCommunication(value.communication);
      cursor = value.messageCursor || null;
      if (cursor) { note.textContent = 'Partial transcript: public text and tool states; private and oversized content omitted.'; more.append(button(doc, 'Older transcript', loadPage, 'ari-older')); }
      else note.textContent = loaded ? 'Start of available transcript (partial: public text and tool states; some content omitted).' : 'Partial transcript · No public text or tool states available.';
      if (value.status === 'idle') follow.dataset.idle = '1';
    } catch (error) {
      if (error?.name === 'AbortError' || !live()) return;
      if (error.reason) markStale(error.reason);
      else { note.textContent = 'Network error. Loaded text is kept.'; more.append(button(doc, 'Retry', loadPage, 'ari-retry')); }
    } finally { busy = false; }
  }

  // Follow-up retains the inspected chat, map and actor execution through the durable receipt.
  // The Host checks that expectation before admission; a later Native CAS protects the subsequent handoff.
  const text = node(doc, 'textarea'); text.rows = 2; text.setAttribute('aria-label', 'Follow-up for this worker'); text.placeholder = 'Follow-up for this same worker';
  const result = node(doc, 'p', '', 'task-workload-coverage'); result.setAttribute('role', 'status');
  const send = button(doc, 'Send follow-up', async () => {
    const body = String(text.value || '').trim();
    if (stale || send.disabled || !body || typeof sendFollowUp !== 'function' || !live()) return;
    send.disabled = true;
    let submitting = false;
    try {
      const check = await get({});
      if (!live()) return;
      if (check.actorID !== ref.nativeId || check.sessionID !== ref.sessionId || check.executionRevision !== ref.executionRevision) throw Object.assign(new Error('mismatch'), { reason: 'The worker changed since this view was opened.' });
      submitting = true;
      const payload = await sendFollowUp({ actorID: ref.nativeId, actorSessionID: ref.sessionId, text: body, chat: scope.chat, mapRevision: scope.mapRevision, executionRevision: ref.executionRevision });
      if (!live()) return;
      const state = deliveryStatus(payload);
      if (state !== 'unknown') text.value = '';
      result.textContent = ({
        'queue-delivered': 'Delivery recorded for this worker.',
        queued: 'Input accepted. Delivery confirmation is unavailable.',
        'pending-boundary': 'Input queued for the next turn boundary.',
        persisted: 'Input recorded. Delivery confirmation is unavailable.',
        cancelled: 'The input was cancelled.',
        unavailable: 'Delivery is unavailable. See the input receipt in chat.',
        unknown: 'Delivery is unconfirmed. Check the input receipt in chat before sending again.',
      })[state];
      send.disabled = state === 'unknown';
    } catch (error) {
      if (error?.name === 'AbortError' || !live()) return;
      if (error.reason) { markStale(error.reason); result.textContent = submitting ? '' : 'Nothing was sent.'; }
      else if (submitting) result.textContent = 'Delivery is unconfirmed. Check chat for an input receipt before sending again.';
      else { result.textContent = 'The worker check is unavailable. Retry when ready.'; send.disabled = false; }
    }
  }, 'ari-send');
  follow.append(node(doc, 'h5', 'Follow up'), text, send, result,
    node(doc, 'p', 'Uses the chat’s durable input for this worker and execution.', 'task-workload-coverage'));

  panel.append(head, nav, status, model, tree, note, list, receiptsBox, communicationBox, more, follow);
  container.append(panel);
  void loadPage();
  return { close, dispose, panel };
}
