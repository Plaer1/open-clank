import { createOpenClankWindow } from './copal/windows.js';
import { mountTaskWorkload } from './taskWorkload.js';
import sessionModule from './sessions.js';
import { getWorkspaceId } from './workspace.js';

let authEpoch = 0, panelEpoch = 0, panel = null, workload = null, bound = null, answer = null, attention = null;
const hints = new Map();
const current = () => ({ account: document.body?.dataset?.accountId || '', chat: sessionModule.getCurrentSessionId() || '', workspace: getWorkspaceId() || '', authEpoch, panelEpoch });
export function captureContinuityScope() { return current(); }
export function continuityScopeCurrent(scope, { chat = true, panelLifetime = false } = {}) {
  const now = current();
  return !!scope?.account && scope.account === now.account && scope.authEpoch === now.authEpoch &&
    (!panelLifetime || scope.panelEpoch === now.panelEpoch) && (!chat || (scope.chat === now.chat && scope.workspace === now.workspace));
}
function dispose() { workload?.dispose(); workload = null; bound = null; answer = null; attention = null; hints.clear(); if (panel) panel.body.replaceChildren(); }
function invalidate() { if (panel?.visible && bound && !continuityScopeCurrent(bound)) panel.requestClose(); }
document.addEventListener('openclank:auth-context-changed', () => { authEpoch++; invalidate(); });
window.addEventListener('openclank:auth-context-changed', () => { authEpoch++; invalidate(); });
document.addEventListener('odysseus:session-selected', invalidate);
window.addEventListener('workspace-change', invalidate);

/** Focus only the exact current question card; this is not an answer or a liveness claim. */
export function focusCurrentQuestion(scope, hostChat, requestID, cardID) {
  if (!continuityScopeCurrent(scope) || scope.chat !== hostChat) return false;
  const card = [...document.querySelectorAll('#chat-history .ask-user-card')].find(item =>
    item.dataset.sessionId === hostChat && item.dataset.hostRequestId === requestID &&
    item.dataset.requestId === cardID && item.dataset.interactive !== 'false' && item.classList.contains('mimo-question-card'));
  if (!card || !card.isConnected) return false;
  card.tabIndex = -1; card.scrollIntoView({block:'center'}); card.focus({preventScroll:true}); return true;
}

function renderAttention() {
  if (!attention) return;
  attention.replaceChildren();
  if (hints.size) { const title=document.createElement('h4'); title.textContent='Live requests (partial)'; attention.append(title); }
  for (const hint of hints.values()) {
    const button=document.createElement('button'); button.type='button'; button.textContent=hint.kind === 'question' ? 'Show question' : 'Show permission request';
    button.addEventListener('click', () => {
      if (!panel?.visible || !bound || !continuityScopeCurrent(bound) || hint.chat !== current().chat) return;
      if (hint.kind === 'question') {
        if (!focusCurrentQuestion(bound, hint.chat, hint.requestID, hint.cardID)) button.textContent='Request card unavailable';
        return;
      }
      const card=[...document.querySelectorAll('#chat-history .ask-user-card')].find(item => item.dataset.requestId === hint.cardID &&
        item.dataset.sessionId === hint.chat && item.classList.contains('permission-card'));
      if (!card || !card.isConnected) { button.textContent='Request card unavailable'; return; }
      card.tabIndex = -1; card.scrollIntoView({block:'center'}); card.focus({preventScroll:true});
    });
    attention.append(button);
  }
}
export function publishLiveAttention(scope, kind, hostChat, value) {
  if (!continuityScopeCurrent(scope) || hostChat !== scope.chat || value?.session_id !== hostChat ||
      !['question', 'permission'].includes(kind)) return;
  const valid = v => typeof v === 'string' && v.length > 0 && v.length <= 256;
  const requestID=value.request_id, cardID=kind === 'question' && value.native_request_id ? value.native_request_id : requestID;
  if (!valid(requestID) || !valid(cardID)) return;
  document.dispatchEvent(new CustomEvent('openclank:work-attention', { detail: { sessionId: hostChat } }));
  if (!panel?.visible || !bound || !continuityScopeCurrent(bound)) return;
  const optionalString = v => valid(v) ? v : undefined;
  const optionalRevision = v => Number.isSafeInteger(v) && v >= 0 ? v : undefined;
  const hint={kind,chat:hostChat,requestID,cardID,actorID:optionalString(value.actor_id),
    actorSessionID:optionalString(value.actor_session_id),questionRevision:optionalRevision(value.question_revision),
    turnID:optionalString(value.turn_id),revision:optionalRevision(value.revision)};
  const key=JSON.stringify([hostChat,kind,requestID,cardID]); hints.delete(key); hints.set(key,hint);
  if (hints.size > 128) hints.delete(hints.keys().next().value);
  renderAttention();
}
document.addEventListener('odysseus:interactive-resolved', event => {
  const d=event.detail || {};
  if (!panel?.visible || !bound || !continuityScopeCurrent(bound) || d.sessionId !== bound.chat ||
      !d.requestId || !['question','permission'].includes(d.kind)) return;
  for (const [key,hint] of hints) if (hint.chat === d.sessionId && hint.kind === d.kind &&
    (hint.requestID === d.requestId || hint.cardID === d.requestId)) hints.delete(key);
  renderAttention();
});

export function openContinuityPanel(scope = current()) {
  if (!scope.chat || !continuityScopeCurrent(scope)) return false;
  const fresh = !panel;
  if (!panel) panel = createOpenClankWindow({ id:'chat-continuity-panel', label:'Chat work', subtitle:'Current chat',
    chatVisibleMaximize:true, navigationChrome:false, accountScope:scope.account, workspaceScope:scope.workspace,
    onClosed:() => { panelEpoch++; const closed = panel; dispose(); panel = null; closed?.destroy(); } });
  if (!workload || !bound || !continuityScopeCurrent(bound)) {
    dispose(); bound = scope;
    const back = document.createElement('button'); back.type = 'button'; back.textContent = 'Return to chat';
    back.addEventListener('click', () => { if (continuityScopeCurrent(bound)) { panel.requestClose(); document.querySelector('#message')?.focus(); } });
    answer = document.createElement('section'); answer.setAttribute('aria-label','Side question answer');
    attention = document.createElement('section'); attention.setAttribute('aria-label','Live request hints');
    const area = document.createElement('section'); panel.body.append(back, answer, attention, area); workload = mountTaskWorkload(area);
  }
  panel.show(); if (fresh) panel.dockBesideChat(); return true;
}
export function showContinuityAnswer(scope, text) {
  if (typeof text !== 'string' || !continuityScopeCurrent(scope, {panelLifetime:true}) || !openContinuityPanel(scope)) return false;
  answer.textContent = text; return true;
}
