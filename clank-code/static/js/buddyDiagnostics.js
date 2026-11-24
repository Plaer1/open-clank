import { getWorkspaceId } from './workspace.js';

const ACTOR_STATES = {
  working: 'Working',
  waiting: 'Waiting',
  inputneeded: 'Waiting for input',
  error: 'Error reported',
  idle: 'Idle',
  unknown: 'State unavailable',
};
const MASCOT_REF = 'static/icons/treehouse-achievements/kit/mascot.svg';
const registry = new WeakMap();

async function read(url, signal) {
  const response = await fetch(url, { credentials: 'same-origin', cache: 'no-store', signal });
  if (!response.ok) throw new Error('Buddy diagnostics unavailable');
  return response.json();
}

function reportLine(text) {
  const row = document.createElement('p');
  row.textContent = text;
  return row;
}

function labelFor(state) {
  return Object.hasOwn(ACTOR_STATES, state) ? ACTOR_STATES[state] : 'State unavailable';
}

function observedTime(value) {
  if (!Number.isSafeInteger(value) || value < 0 || value > 8640000000000000) return 'Time unavailable';
  return new Date(value).toLocaleString();
}

function token(value) {
  return typeof value === 'string' && /^[A-Za-z0-9_.:@-]{1,200}$/.test(value) ? value : '';
}

function renderObservation(value, output) {
  const source = value?.source;
  if (value?.version !== 1 || value.scope?.kind !== 'verified_task_observation_page' || value.scope.complete !== false
      || source?.kind !== 'native_task' || source.connection !== 'connected' || !Array.isArray(value.actors) || value.actors.length > 32
      || !Array.isArray(value.tasks) || value.tasks.length > 32
      || !Array.isArray(value.requests?.items) || value.requests.items.length > 64) {
    output.replaceChildren(reportLine('Work observation is unavailable for this chat.'));
    return;
  }
  const rows = [];
  if (value.mascotRef === MASCOT_REF) {
    const mascot = document.createElement('img');
    mascot.src = '/' + MASCOT_REF;
    mascot.alt = 'Open Clank mascot';
    mascot.width = 56;
    mascot.height = 56;
    rows.push(mascot);
  }
  const scope = source.availability === 'partial' || source.availability === 'available'
    ? 'Partial current chat observation' : 'Task source unavailable';
  rows.push(reportLine(`${scope} · ${observedTime(source.observedAt)}`));
  for (const item of value.actors) {
    const ref = item?.sourceRef;
    const actor = token(ref?.nativeId);
    const session = token(ref?.sessionId);
    if (ref?.kind !== 'native_actor' || !actor || !session
        || !Number.isSafeInteger(ref.executionRevision) || ref.executionRevision < 0) continue;
    rows.push(reportLine(`Actor ${actor} · session ${session} · execution ${ref.executionRevision} · ${labelFor(item.state)} · ${observedTime(item.observedAt)}`));
  }
  const taskCounts = value.summary?.taskCounts || {};
  const taskFacts = ['working', 'waiting', 'inputneeded', 'error', 'idle', 'unknown'].flatMap(state => {
    const count = taskCounts[state];
    return Number.isSafeInteger(count) && count >= 0 && count <= 32 ? [`${labelFor(state)} ${count}`] : [];
  });
  rows.push(reportLine(taskFacts.length ? `Task rows · ${taskFacts.join(' · ')}` : 'Task totals unavailable.'));
  const requests = value.requests.availability === 'partial' ? value.requests.items.filter(item => item?.state === 'inputneeded') : null;
  if (requests === null) rows.push(reportLine('Request observation unavailable.'));
  else if (requests.length) {
    for (const item of requests) {
      const actor = token(item.actorRef?.nativeId);
      rows.push(reportLine(`${actor ? `Actor ${actor}` : 'A reported actor'} is waiting for input · ${observedTime(item.observedAt)}`));
    }
  } else rows.push(reportLine('No pending input in the observed request rows.'));
  output.replaceChildren(...rows);
}

export function clearBuddyDiagnostics(panel) {
  registry.get(panel)?.();
}

export function mountBuddyDiagnostics(panel) {
  if (!panel || registry.has(panel)) return;
  const modal = panel.closest('#settings-modal');
  const visible = () => panel.isConnected && !panel.classList.contains('hidden') && modal && !modal.classList.contains('hidden');
  const account = document.body.dataset.accountId || '';
  if (!account || !visible()) return;
  const lifetime = new AbortController();
  const options = { signal: lifetime.signal };
  let root;
  let currentRead;
  const clear = () => {
    lifetime.abort();
    currentRead?.abort();
    root?.remove();
    registry.delete(panel);
  };
  registry.set(panel, clear);
  document.addEventListener('openclank:auth-context-changed', clear, options);
  window.addEventListener('pagehide', clear, options);
  window.addEventListener('openclank:presentation-visibility-changed', event => {
    if (event.detail?.source === 'settings' && event.detail.visible === false) clear();
  }, options);

  void read('/api/diagnostics/buddy', lifetime.signal).then(gate => {
    if (gate?.available !== true || lifetime.signal.aborted || !visible() || document.body.dataset.accountId !== account) {
      clear();
      return;
    }
    root = document.createElement('details');
    root.className = 'admin-card';
    const summary = document.createElement('summary');
    summary.textContent = 'Buddy inspector';
    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'admin-btn-sm';
    check.textContent = 'Read current chat';
    const output = document.createElement('div');
    output.setAttribute('role', 'status');
    root.append(summary, check, output);
    panel.append(root);

    const reset = () => {
      currentRead?.abort();
      currentRead = null;
      check.disabled = false;
      output.replaceChildren();
    };
    root.addEventListener('toggle', () => { if (!root.open) reset(); }, options);
    document.addEventListener('odysseus:session-selected', reset, options);
    window.addEventListener('workspace-change', reset, options);
    check.addEventListener('click', async () => {
      if (!root.open || !visible()) return;
      const currentChat = () => window.sessionModule?.getCurrentSessionId?.() || '';
      const chat = currentChat();
      const workspace = getWorkspaceId();
      if (!chat) {
        output.replaceChildren(reportLine('Choose a chat to inspect its admitted work observations.'));
        return;
      }
      currentRead?.abort();
      const request = new AbortController();
      currentRead = request;
      check.disabled = true;
      output.replaceChildren(reportLine('Reading current chat…'));
      const current = () => !lifetime.signal.aborted && currentRead === request && !request.signal.aborted
        && root.open && visible() && document.body.dataset.accountId === account && currentChat() === chat && getWorkspaceId() === workspace;
      try {
        const gate = await read('/api/diagnostics/buddy', request.signal);
        if (!current()) return;
        if (gate?.available !== true) { clear(); return; }
        const preference = await read('/api/prefs/companion_enabled', request.signal);
        if (!current()) return;
        if (preference?.account_id !== account || preference.value !== true) {
          output.replaceChildren(reportLine('Work observation is unavailable. This inspector uses your existing Clankshop Live states choice.'));
          return;
        }
        const observed = await read(`/api/orchestration/${encodeURIComponent(chat)}/companion-observation`, request.signal);
        if (current()) renderObservation(observed, output);
      } catch (_) {
        if (current()) output.replaceChildren(reportLine('Work observation is unavailable for this chat.'));
      } finally {
        if (currentRead === request) {
          currentRead = null;
          check.disabled = false;
        }
      }
    }, options);
  }).catch(() => { if (!lifetime.signal.aborted) clear(); });
}
