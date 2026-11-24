import { getWorkspaceId } from '../workspace.js';

const ROOT = '/api/browser-control/connection';

function newClientId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  return [...bytes].map(value => value.toString(16).padStart(2, '0')).join('');
}

export function mountBrowserConnectionPanel(header, { currentSession, status }) {
  const clientId = newClientId();
  let generation = 0;
  let closed = false;
  let request = null;
  let readbackTimer = null;
  let selectedTargetId = null;
  let targetView = null;
  let contextPreview = null;
  let selectedSession = currentSession() || '';
  const host = document.createElement('details');
  host.className = 'browser-applet-connection';
  const summary = document.createElement('summary');
  summary.textContent = 'Connection';
  const body = document.createElement('div');
  body.className = 'browser-applet-connection-menu';
  const state = document.createElement('p');
  state.setAttribute('role', 'status');
  state.textContent = 'Disconnected';
  const modeLabel = document.createElement('label');
  modeLabel.textContent = 'Installed Chrome profile directory';
  const profileHelp = document.createElement('p');
  profileHelp.textContent = 'Use an existing Chrome profile where the Playwright extension is already installed. This does not install or select a default profile.';
  const profile = document.createElement('input');
  profile.type = 'text';
  profile.autocomplete = 'off';
  profile.maxLength = 64;
  profile.placeholder = 'Enter an existing profile name';
  modeLabel.append(profile);
  const isolated = document.createElement('button');
  isolated.type = 'button';
  isolated.textContent = 'Start isolated';
  const attach = document.createElement('button');
  attach.type = 'button';
  attach.textContent = 'Attach installed extension';
  const tabs = document.createElement('div');
  tabs.className = 'browser-applet-connection-tabs';
  const selection = document.createElement('p');
  const capabilities = document.createElement('p');
  const receipt = document.createElement('p');
  receipt.setAttribute('role', 'status');
  const stopTab = document.createElement('button');
  stopTab.type = 'button';
  stopTab.textContent = 'Stop control of this tab';
  stopTab.hidden = true;
  const pause = document.createElement('button');
  pause.type = 'button';
  pause.textContent = 'Take over for sign-in';
  pause.hidden = true;
  const context = document.createElement('button');
  context.type = 'button';
  context.textContent = 'Preview page context';
  context.hidden = true;
  const excerptBox = document.createElement('textarea');
  excerptBox.readOnly = true;
  excerptBox.rows = 6;
  excerptBox.cols = 34;
  excerptBox.hidden = true;
  excerptBox.setAttribute('aria-label', 'Page structure preview');
  const addContext = document.createElement('button');
  addContext.type = 'button';
  addContext.textContent = 'Add selected text to chat';
  addContext.hidden = true;
  const contextOrigin = document.createElement('p');
  contextOrigin.hidden = true;
  const disconnect = document.createElement('button');
  disconnect.type = 'button';
  disconnect.textContent = 'Disconnect';
  const refreshButton = document.createElement('button');
  refreshButton.type = 'button';
  refreshButton.textContent = 'Refresh tabs';
  const generationFor = () => ++generation;
  const current = (expected, sessionId) => !closed && generation === expected
    && currentSession() === sessionId && selectedSession === sessionId;

  async function jsonRequest(path, { method = 'GET', payload, signal } = {}) {
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (signal?.aborted) controller.abort();
    else signal?.addEventListener('abort', abort, { once: true });
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; controller.abort(); }, method === 'GET' ? 5000 : 30000);
    try {
      const response = await fetch(path, {
        method, credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
        headers: payload ? { 'Content-Type': 'application/json' } : undefined,
        body: payload ? JSON.stringify(payload) : undefined,
      });
      const value = await response.json().catch(error => {
        if (error?.name === 'AbortError') throw error;
        return {};
      });
      if (timedOut) throw new Error('Browser connection request timed out; its server outcome may still be pending. Disconnect or refresh before retrying.');
      if (!response.ok) throw new Error(value?.detail?.message || value?.detail || `Browser request failed (${response.status})`);
      return value;
    } catch (error) {
      if (timedOut && !signal?.aborted) throw new Error('Browser connection request timed out; its server outcome may still be pending. Disconnect or refresh before retrying.');
      throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }

  function clearContext() {
    contextPreview = null;
    excerptBox.value = '';
    excerptBox.hidden = true;
    addContext.hidden = true;
    contextOrigin.hidden = true;
  }

  function showReadback(value) {
    const target = value.selectedTarget;
    selectedTargetId = target?.targetId || null;
    targetView = target || null;
    stopTab.hidden = !selectedTargetId;
    const adapter = value.adapter;
    pause.hidden = !selectedTargetId || adapter?.mode !== 'extension';
    context.hidden = !selectedTargetId || !adapter?.observationActions?.includes('snapshot');
    if (contextPreview && (contextPreview.targetId !== selectedTargetId ||
        contextPreview.navigationGeneration !== target?.navigationGeneration)) clearContext();
    const connectionLabel = adapter?.mode === 'extension'
      ? `Chrome extension · profile ${adapter.profile}`
      : 'Isolated browser on the host';
    state.textContent = value.connected ? `${adapter?.name || 'Browser'} · ${connectionLabel}` : 'Disconnected';
    const selectionMessages = {
      operator_stopped: 'Tab control stopped. Select a tab to allow control again.',
      sign_in_paused: 'Browser control paused. Sign in directly in the attached browser. Then refresh and select the tab to resume.',
      parent_retired: 'Parent job ended. Select a tab to allow control again.',
      actor_retired: 'Browser worker finished. Select a tab to allow control again.',
      stale: 'Tab changed or became unavailable. Refresh tabs and select it again.',
      unselected: 'Select a tab for this chat.',
    };
    selection.textContent = target
      ? `Selected tab · ${target.title || target.origin || 'Untitled tab'}${value.controllingActorId ? ` · Controller ${value.controllingActorId}` : ''}`
      : (value.connected ? selectionMessages[value.selectionState] || '' : '');
    const effective = value.effectiveActions;
    const actions = value.controllingActorId && effective
      ? [...effective.observe, ...effective.control]
      : [...(adapter?.observationActions || []), ...(adapter?.controlActions || [])];
    capabilities.textContent = target && adapter
      ? `${value.controllingActorId ? 'Current agent actions' : 'Adapter actions'} · ${actions.join(', ') || 'None'}`
      : '';
    const latest = value.latestAction;
    const outcomes = {
      dispatching: 'sent to the browser',
      completed: 'completed by the adapter',
      outcome_unknown: 'outcome unknown. Check the tab before another action.',
    };
    const transfer = latest?.transfer;
    const transferDetail = transfer?.direction === 'download'
      ? ` · Browser reported download activity (${transfer.completedReported} completed); Files result unavailable`
      : '';
    receipt.textContent = latest
      ? `Last action · ${latest.action} · ${latest.origin} · ${outcomes[latest.state] || latest.state}${transferDetail}`
      : '';
  }

  function showTabs(value, expected, sessionId) {
    if (!current(expected, sessionId)) return;
    tabs.replaceChildren();
    for (const tab of value.tabs || []) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = `Select ${tab.title || 'Untitled tab'} · ${tab.url}`;
      button.title = tab.url;
      button.addEventListener('click', async () => {
        const next = generationFor();
        const controller = new AbortController();
        request?.abort(); request = controller;
        try {
          await jsonRequest(`${ROOT}/select`, {
            method: 'POST', signal: controller.signal,
            payload: { session_id: sessionId, client_id: clientId, client_generation: next, tab_index: tab.index, selection_witness: tab.selectionWitness },
          });
          if (current(next, sessionId)) await refresh(next, sessionId);
        } catch (error) { if (current(next, sessionId)) state.textContent = error.message; }
      });
      tabs.append(button);
    }
    showReadback(value);
  }

  async function refresh(expected = generation, sessionId = currentSession()) {
    if (!sessionId || !current(expected, sessionId)) return;
    try {
      const value = await jsonRequest(`${ROOT}?session_id=${encodeURIComponent(sessionId)}`, { signal: request?.signal });
      showTabs(value, expected, sessionId);
    } catch (error) { if (current(expected, sessionId)) state.textContent = error.message; }
  }

  function scheduleReadback() {
    clearTimeout(readbackTimer);
    if (closed || !host.open) return;
    readbackTimer = setTimeout(async () => {
      const expected = generation;
      const sessionId = currentSession();
      try {
        if (!sessionId || !current(expected, sessionId)) return;
        const value = await jsonRequest(
          `${ROOT}?session_id=${encodeURIComponent(sessionId)}&readback_only=true`,
          { signal: request?.signal },
        );
        if (host.open && current(expected, sessionId)) showReadback(value);
      } catch (error) {
        if (host.open && current(expected, sessionId) && error?.name !== 'AbortError') {
          receipt.textContent = 'Browser action status is unavailable.';
        }
      } finally {
        scheduleReadback();
      }
    }, 2500);
  }

  async function stopSelectedTab(intent = 'stop') {
    const sessionId = currentSession();
    if (!sessionId || sessionId !== selectedSession || !selectedTargetId) return;
    const targetId = selectedTargetId;
    const next = generationFor();
    request?.abort();
    const controller = new AbortController(); request = controller;
    stopTab.disabled = true;
    pause.disabled = true;
    clearContext();
    try {
      const value = await jsonRequest(`${ROOT}/stop-tab`, {
        method: 'POST', signal: controller.signal,
        payload: { session_id: sessionId, client_id: clientId, client_generation: next, target_id: targetId, intent },
      });
      if (current(next, sessionId)) {
        showReadback(value);
        if (!value.stopped) state.textContent = 'The selected tab already changed.';
      }
    } catch (error) {
      if (current(next, sessionId)) state.textContent = error.message;
    } finally {
      stopTab.disabled = false;
      pause.disabled = false;
    }
  }

  function sameDraft(expected) {
    const live = window.chatModule?.captureCodeEvidenceDraft?.();
    return Boolean(expected?.owner && live && live.owner === expected.owner && live.sid === expected.sid
      && live.epoch === expected.epoch && live.authEpoch === expected.authEpoch && live.version === expected.version
      && live.text === expected.text && live.attachments === expected.attachments
      && live.pending.length === expected.pending.length && live.pending.every((file, i) => file === expected.pending[i])
      && getWorkspaceId() === expected.workspace);
  }

  async function previewContext() {
    const sessionId = currentSession();
    const target = targetView;
    const draft = window.chatModule?.captureCodeEvidenceDraft?.();
    if (!target || !draft?.owner || draft.sid !== sessionId || sessionId !== selectedSession) return;
    draft.workspace = getWorkspaceId();
    const expected = generationFor();
    request?.abort();
    const controller = new AbortController(); request = controller;
    clearContext();
    context.disabled = true;
    try {
      const result = await jsonRequest(`${ROOT}/preview-context`, {
        method: 'POST', signal: controller.signal,
        payload: {
          session_id: sessionId, target_id: target.targetId,
          connection_generation: target.connectionGeneration,
          navigation_generation: target.navigationGeneration, observation_generation: target.observationGeneration,
        },
      });
      if (!current(expected, sessionId) || !sameDraft(draft) || result.targetId !== target.targetId
          || result.connectionGeneration !== target.connectionGeneration || result.navigationGeneration !== target.navigationGeneration
          || result.kind !== 'accessibility_structure' || typeof result.text !== 'string' || !result.text
          || result.text.length > 3000 || !Number.isFinite(result.observedAt)) return;
      contextPreview = { ...result, draft, generation: expected, workspace: draft.workspace };
      excerptBox.value = result.text;
      excerptBox.hidden = false;
      contextOrigin.textContent = `Partial page structure from ${result.origin} · ${new Date(result.observedAt * 1000).toLocaleString()}`;
      contextOrigin.hidden = false;
      addContext.hidden = false;
      excerptBox.focus();
      excerptBox.setSelectionRange(0, 0);
    } catch (error) {
      if (current(expected, sessionId)) state.textContent = error.message;
    } finally {
      context.disabled = false;
    }
  }

  async function appendSelectedContext() {
    const preview = contextPreview;
    const selected = excerptBox.value.slice(excerptBox.selectionStart, excerptBox.selectionEnd).trim();
    const input = document.getElementById('message');
    if (!selected || !preview || !input || input.disabled || !current(preview.generation, preview.draft.sid)
        || preview.targetId !== selectedTargetId || !sameDraft(preview.draft)) {
      state.textContent = selected ? 'The chat or draft changed. Preview page context again.' : 'Select text in the preview first.';
      return;
    }
    addContext.disabled = true;
    try {
      const currentState = await jsonRequest(
        `${ROOT}?session_id=${encodeURIComponent(preview.draft.sid)}&readback_only=true`,
        { signal: request?.signal },
      );
      const target = currentState.selectedTarget;
      if (contextPreview !== preview || !current(preview.generation, preview.draft.sid) || !sameDraft(preview.draft)
          || target?.targetId !== preview.targetId || target.navigationGeneration !== preview.navigationGeneration) {
        throw new Error('The tab or draft changed. Preview page context again.');
      }
    } catch (error) {
      if (contextPreview === preview) state.textContent = error.message;
      return;
    } finally {
      addContext.disabled = false;
    }
    const source = `From ${preview.origin} · ${new Date(preview.observedAt * 1000).toISOString()}`;
    input.value += `${input.value ? '\n\n' : ''}${source}\n${selected}`;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    clearContext();
    state.textContent = 'Selected text added to the chat draft.';
  }

  async function connect(mode) {
    const sessionId = currentSession();
    if (!sessionId) { state.textContent = 'Select a managed chat first.'; return; }
    selectedSession = sessionId;
    const expected = generationFor();
    request?.abort();
    const controller = new AbortController(); request = controller;
    state.textContent = mode === 'isolated' ? 'Starting isolated Browser…' : 'Connecting to the installed extension…';
    try {
      const value = await jsonRequest(`${ROOT}/open`, {
        method: 'POST', signal: controller.signal,
        payload: {
          session_id: sessionId, client_id: clientId, client_generation: expected, mode,
          ...(mode === 'extension' ? { profile_dir_name: profile.value.trim() } : {}),
        },
      });
      if (!current(expected, sessionId)) return;
      showTabs({ connected: true, ...value }, expected, sessionId);
      status(`Browser connected (${mode}).`);
    } catch (error) {
      if (current(expected, sessionId)) state.textContent = error?.name === 'AbortError' ? 'Connection request was cancelled.' : error.message;
    }
  }

  async function closeCurrent() {
    const sessionId = selectedSession || currentSession();
    const next = generationFor();
    request?.abort(); request = null;
    selectedSession = sessionId || '';
    if (!sessionId) { showReadback({ connected: false }); return; }
    state.textContent = 'Disconnecting…';
    try {
      await jsonRequest(`${ROOT}/close`, { method: 'POST', payload: { session_id: sessionId, client_id: clientId, client_generation: next } });
      if (current(next, sessionId)) { showReadback({ connected: false }); tabs.replaceChildren(); }
    } catch (error) { if (current(next, sessionId)) state.textContent = error.message; }
  }

  isolated.addEventListener('click', () => void connect('isolated'));
  attach.addEventListener('click', () => void connect('extension'));
  disconnect.addEventListener('click', () => void closeCurrent());
  stopTab.addEventListener('click', () => void stopSelectedTab());
  pause.addEventListener('click', () => void stopSelectedTab('pause_for_sign_in'));
  context.addEventListener('click', () => void previewContext());
  addContext.addEventListener('click', appendSelectedContext);
  refreshButton.addEventListener('click', () => {
    const expected = generationFor();
    request?.abort();
    request = new AbortController();
    void refresh(expected, currentSession());
  });
  host.addEventListener('toggle', () => {
    scheduleReadback();
    if (!host.open) return;
    const sessionId = currentSession();
    if (!sessionId) { state.textContent = 'Select a managed chat first.'; return; }
    if (sessionId !== selectedSession) {
      request?.abort(); request = null;
      selectedSession = sessionId;
      generationFor();
      tabs.replaceChildren();
    }
    void refresh(generation, sessionId);
  });
  body.append(state, isolated, profileHelp, modeLabel, attach, refreshButton, tabs, selection, capabilities,
    stopTab, pause, context, contextOrigin, excerptBox, addContext, receipt, disconnect);
  host.append(summary, body);
  header.append(host);
  const onSession = event => {
    const nextSession = String(event.detail?.sessionId || '');
    if (nextSession === selectedSession) return;
    const oldSession = selectedSession;
    const next = generationFor();
    request?.abort(); request = null;
    selectedSession = nextSession;
    tabs.replaceChildren(); showReadback({ connected: false });
    if (oldSession) void jsonRequest(`${ROOT}/close`, {
      method: 'POST', payload: { session_id: oldSession, client_id: clientId, client_generation: next },
    }).catch(() => {});
  };
  document.addEventListener('odysseus:session-selected', onSession);
  return Object.freeze({
    refresh: () => refresh(),
    close: async () => {
      closed = true;
      clearTimeout(readbackTimer);
      document.removeEventListener('odysseus:session-selected', onSession);
      request?.abort();
      await closeCurrent();
      host.remove();
    },
  });
}
