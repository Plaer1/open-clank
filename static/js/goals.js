const button = document.getElementById('goal-panel-btn');
const dialog = document.getElementById('goal-dialog');
const closeButton = document.getElementById('goal-dialog-close');
const status = document.getElementById('goal-dialog-status');
const activeRoot = document.getElementById('goal-active');
const queueRoot = document.getElementById('goal-queue');
const historyRoot = document.getElementById('goal-history');
const analyticsRoot = document.getElementById('goal-analytics');
const clearHistoryButton = document.getElementById('goal-clear-history');
const createForm = document.getElementById('goal-create-form');
const objectiveInput = document.getElementById('goal-objective');

let sessionId = null;
let view = null;
let busy = false;

function currentSession() {
  const sessions = window.sessionModule?.getSessions?.() || [];
  const id = window.sessionModule?.getCurrentSessionId?.() || null;
  return sessions.find(item => item.id === id) || null;
}

function setStatus(message) {
  status.textContent = message || '';
}

function target(goal) {
  return { goalID: goal.id, expectedRevision: goal.revision };
}

function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function actionButton(label, action, goal) {
  const control = document.createElement('button');
  control.type = 'button';
  control.textContent = label;
  control.dataset.goalAction = action;
  control.disabled = busy;
  control.setAttribute('aria-label', `${label} active goal`);
  control.addEventListener('click', () => mutate({ action, target: target(goal) }));
  return control;
}

function formatLimit(used, maximum) {
  return `${used ?? 0} / ${maximum ?? 'no limit'}`;
}

function recordDetails(goal) {
  const details = document.createElement('div');
  details.className = 'goal-record-details';
  const budget = goal.budget || {};
  const budgetText = [
    `turns ${formatLimit(budget.usedTurns, budget.maxTurns)}`,
    `tokens ${formatLimit(budget.usedTokens, budget.maxTokens)}`,
    `tool calls ${formatLimit(budget.usedToolCalls, budget.maxToolCalls)}`,
  ];
  if (budget.maxWallMs !== undefined) {
    budgetText.push(`wall limit ${Math.round(budget.maxWallMs / 1000)}s`);
  }
  if (budget.deadline !== undefined) {
    budgetText.push(`deadline ${new Date(budget.deadline).toLocaleString()}`);
  }
  details.appendChild(textNode('p', `Budget: ${budgetText.join(' · ')}`));

  const required = Array.isArray(goal.requiredEvidence) ? goal.requiredEvidence : [];
  const evidence = Array.isArray(goal.evidence) ? goal.evidence : [];
  details.appendChild(textNode(
    'p',
    required.length
      ? `Required evidence: ${required.join(', ')} · attached ${evidence.length}`
      : `Evidence: none required · attached ${evidence.length}`,
  ));
  if (evidence.length) {
    const list = document.createElement('ul');
    list.className = 'goal-evidence-list';
    for (const item of evidence) {
      list.appendChild(textNode(
        'li',
        `${item.kind || 'evidence'}: ${item.subject || item.sourceRef || 'attached'}`,
      ));
    }
    details.appendChild(list);
  }
  if (goal.lastOutcome?.code) {
    details.appendChild(textNode(
      'p',
      `Outcome: ${goal.lastOutcome.code}${goal.lastOutcome.reason ? ` — ${goal.lastOutcome.reason}` : ''}`,
    ));
  }
  return details;
}

function editControl(goal) {
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Edit objective';
  button.disabled = busy;
  button.setAttribute('aria-label', 'Edit active goal objective');
  button.setAttribute('aria-expanded', 'false');

  const form = document.createElement('form');
  form.className = 'goal-edit-form';
  form.hidden = true;
  const input = document.createElement('input');
  input.value = goal.objective;
  input.maxLength = 4000;
  input.required = true;
  input.setAttribute('aria-label', 'Goal objective');
  const save = document.createElement('button');
  save.type = 'submit';
  save.textContent = 'Save objective';
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.textContent = 'Cancel edit';
  cancel.addEventListener('click', () => {
    form.hidden = true;
    button.setAttribute('aria-expanded', 'false');
    button.focus();
  });
  form.append(input, save, cancel);
  form.addEventListener('submit', event => {
    event.preventDefault();
    const objective = input.value.trim();
    if (objective && objective !== goal.objective) {
      mutate({ action: 'edit', target: target(goal), objective });
    }
  });
  button.addEventListener('click', () => {
    form.hidden = false;
    button.setAttribute('aria-expanded', 'true');
    input.focus();
    input.select();
  });
  return { button, form };
}

function render(data) {
  view = data;
  const state = data?.state || {};
  const active = state.active;
  activeRoot.replaceChildren();
  queueRoot.replaceChildren();
  historyRoot.replaceChildren();
  analyticsRoot.replaceChildren();

  if (!active) {
    activeRoot.appendChild(textNode('p', 'No active goal.'));
  } else {
    const card = document.createElement('div');
    card.className = 'goal-active-card';
    card.appendChild(textNode('div', active.objective, 'goal-objective'));
    card.appendChild(textNode(
      'div',
      `${active.status} · revision ${active.revision}`,
      'goal-meta',
    ));
    card.appendChild(recordDetails(active));
    const actions = document.createElement('div');
    actions.className = 'goal-active-actions';
    const edit = editControl(active);
    actions.appendChild(edit.button);
    if (['paused', 'blocked'].includes(active.status)) {
      actions.appendChild(actionButton('Resume', 'resume', active));
    } else if (['active', 'awaiting_verification', 'verification_degraded'].includes(active.status)) {
      actions.appendChild(actionButton('Pause', 'pause', active));
    }
    if (['active', 'awaiting_verification', 'verification_degraded'].includes(active.status)) {
      actions.appendChild(actionButton('Verify now', 'verify', active));
    }
    if (!['completed', 'cancelled', 'expired'].includes(active.status)) {
      actions.appendChild(actionButton('Cancel', 'cancel', active));
    }
    card.appendChild(actions);
    card.appendChild(edit.form);
    activeRoot.appendChild(card);
  }

  const queue = Array.isArray(state.queue) ? state.queue : [];
  if (!queue.length) {
    queueRoot.appendChild(textNode('li', 'Nothing queued.'));
  } else {
    for (const goal of queue) {
      const item = document.createElement('li');
      item.appendChild(textNode('div', goal.objective, 'goal-objective'));
      item.appendChild(textNode(
        'div',
        `${goal.status} · revision ${goal.revision}`,
        'goal-meta',
      ));
      item.appendChild(recordDetails(goal));
      queueRoot.appendChild(item);
    }
  }

  const history = Array.isArray(state.history) ? state.history : [];
  if (!history.length) {
    historyRoot.appendChild(textNode('li', 'No completed goals.'));
  } else {
    for (const goal of history) {
      const item = document.createElement('li');
      item.appendChild(textNode('div', goal.objective, 'goal-objective'));
      item.appendChild(textNode(
        'div',
        `${goal.status} · revision ${goal.revision}`,
        'goal-meta',
      ));
      item.appendChild(recordDetails(goal));
      historyRoot.appendChild(item);
    }
  }
  clearHistoryButton.disabled = busy || !history.length;

  const analytics = data?.analytics || {};
  const entries = Object.entries(analytics).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.length) {
    analyticsRoot.appendChild(textNode('dt', 'No outcomes yet'));
    analyticsRoot.appendChild(textNode('dd', '0'));
  } else {
    for (const [name, count] of entries) {
      analyticsRoot.appendChild(textNode('dt', name.replaceAll('_', ' ')));
      analyticsRoot.appendChild(textNode('dd', String(count)));
    }
  }
}

async function request(method, payload) {
  const response = await fetch(`/api/session/${encodeURIComponent(sessionId)}/goal`, {
    method,
    headers: payload ? { 'Content-Type': 'application/json' } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || data.error || `Goal request failed (${response.status})`);
  }
  return data;
}

async function reload() {
  if (!sessionId) return;
  setStatus('Loading goals…');
  try {
    render(await request('GET'));
    setStatus('');
  } catch (error) {
    setStatus(error.message);
  }
}

async function mutate(payload) {
  if (busy || !sessionId) return false;
  busy = true;
  render(view);
  setStatus(payload.action === 'verify' ? 'Verifying…' : 'Saving…');
  try {
    render(await request('POST', payload));
    setStatus(payload.action === 'verify' ? 'Verification finished.' : 'Saved.');
    return true;
  } catch (error) {
    setStatus(error.message);
    if (/changed|revision|stale/i.test(error.message)) await reload();
    return false;
  } finally {
    busy = false;
    render(view);
  }
}

function syncButton() {
  const session = currentSession();
  const available = session?.endpoint_url === 'mimo://acp';
  button.hidden = !available;
  if (!available && dialog.open) dialog.close();
}

button?.addEventListener('click', () => {
  const session = currentSession();
  if (!session || session.endpoint_url !== 'mimo://acp') return;
  sessionId = session.id;
  dialog.showModal();
  reload();
});

closeButton?.addEventListener('click', () => dialog.close());
dialog?.addEventListener('click', event => {
  if (event.target === dialog) dialog.close();
});
createForm?.addEventListener('submit', event => {
  event.preventDefault();
  const objective = objectiveInput.value.trim();
  if (!objective) return;
  mutate({ action: 'create', objective }).then(saved => {
    if (saved) objectiveInput.value = '';
  });
});
clearHistoryButton?.addEventListener('click', () => {
  const history = view?.state?.history;
  if (!Array.isArray(history) || !history.length) return;
  if (!window.confirm('Clear completed goal history for this session?')) return;
  mutate({
    action: 'clear_history',
    expectedEnvelopeRevision: view.state.revision,
  });
});
document.addEventListener('odysseus:session-selected', syncButton);
document.addEventListener('DOMContentLoaded', syncButton);
